"""Live paper-trading engine. Runs forever, independent of Claude and the dashboard.

Loop (every LOOP_SECONDS):
  heartbeat -> owner commands -> integrity/reconciliation -> quotes -> (on each
  confirmed 5-min bar) deep analysis of the watchlist -> guardian -> orders ->
  position management (stops, trails, exits, EOD flatten) -> equity marks.

Failure policy: any exception inside a cycle is logged + alerted, integrity is
marked unverified (no new entries), and the loop keeps running so existing
positions stay managed. Broker-side stop orders protect positions even if this
process dies.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import signal
import threading
import time
import uuid

import numpy as np
import pandas as pd

from .broker.alpaca_broker import AlpacaBroker
from .broker.base import BrokerError
from .config import RiskLimits, Settings, check_within_ceiling, load_settings
from .costs import CostModel
from .data.historical import HistoricalData, to_rth
from .books import Books
from .db import DB
from .execution import TradeManager, TradePlan
from .guardian import AccountState, CapitalGuardian, Proposal
from .ledger import Ledger
from .markettime import ET, PT, fmt_pt, now_utc, pt_schedule_for, session
from .notify import Notifier
from .regime import coarse, daily_regimes, describe_current
from .reporting import build_report, render_text, save_report, to_csv
from .scanner import build_discovery_universe, choose_watchlist, correlation_with, default_universe, stage1_rank, is_leveraged_or_inverse
from .scoring import Opportunity, classify, score
from .strategies.features import intraday_features
from .strategies.library import BY_NAME

log = logging.getLogger("engine")
LOOP_SECONDS = 15
BAR_MIN = 5
VERSION = "1.0.0"


class Engine:
    def __init__(self, settings: Settings, broker=None, db: DB | None = None, hist=None, notifier=None,
                 clock=now_utc):
        self.s = settings
        self.clock = clock
        self.db = db or DB(settings.db_path)
        self.broker = broker or AlpacaBroker(settings.api_key, settings.api_secret, paper=settings.is_paper,
                                             data_feed=settings.data_feed)
        self.hist = hist or HistoricalData(settings.api_key, settings.api_secret, settings.data_dir / "cache", "sip")
        self.notify = notifier or Notifier(settings, self.db)
        self.limits = self._load_limits()
        led = self.db.kv_get("ledger")
        ledger = Ledger.from_dict(led) if led else Ledger(self.limits.starting_capital, settings.account_type)
        self.tm = TradeManager(self.broker, self.db, ledger, self.notify, clock=clock)
        self.guardian = CapitalGuardian(self.limits)
        self.costs = CostModel()
        self.books = Books(self.db, self.limits, settings.account_type, self.costs)
        self._book_only: dict[str, list[dict]] = {}
        self.marks = self.db.kv_get("marks", {})
        self.bars: dict[str, pd.DataFrame] = {}
        self.daily: dict[str, pd.DataFrame] = {}
        self._daily_asof: dict[str, dt.date] = {}   # day each symbol's daily bars were last refreshed
        self.crypto_bars: dict = {}
        self.crypto_last: dict = {}
        self._crypto_hour = None
        self._crypto_universe: list = []
        self._crypto_universe_date = None
        self.daily_date: dt.date | None = None
        self.regimes: pd.DataFrame | None = None
        self.watchlist: list[str] = self.db.kv_get("watchlist", default_universe()[:20])
        self.universe: list[dict] = self.db.kv_get("discovery_universe", [])
        self.last_bar_end: dict[str, pd.Timestamp] = {}
        self.last_scan = 0.0
        self.last_recon = 0.0
        self.broker_failures = 0
        self.integrity: dict[str, str] = {}
        self.vol_pause_until: dt.datetime | None = None
        self.prices: dict[str, float] = {}
        self.quote_ts: dict[str, dt.datetime] = {}
        self.spreads: dict[str, float] = {}
        self.last_closed_bar: dict[str, dict] = {}
        self._stop = threading.Event()
        self.report_done = self.db.kv_get("report_done", "")
        self.started_at = clock()

    # ------------------------------------------------------------------ state
    def _load_limits(self) -> RiskLimits:
        d = self.db.kv_get("risk_limits")
        if d:
            lim = RiskLimits.from_dict(d)
            if not check_within_ceiling(lim, self.s.risk_ceiling):
                return lim
            self.db.event("warning", "risk_limits", "stored limits exceed approved ceiling; using ceiling")
        return self.s.risk_ceiling

    def flags(self) -> dict:
        return self.db.kv_get("flags", {"paused": False, "emergency": False, "drawdown_halt": False,
                                        "ignored_symbols": [], "asset_classes": ["us_equity"]})

    def set_flag(self, k, v):
        f = self.flags()
        f[k] = v
        self.db.kv_set("flags", f)

    def equity(self) -> float:
        return self.tm.ledger.equity(self.prices)

    def update_marks(self, now: dt.datetime) -> None:
        e = self.equity()
        day = now.astimezone(ET).date().isoformat()
        wk = "%d-%02d" % now.astimezone(ET).isocalendar()[:2]
        m = self.marks
        if m.get("day") != day:
            m.update(day=day, day_start=e, session_peak=e)
        if m.get("week") != wk:
            m.update(week=wk, week_start=e)
        m["peak"] = max(m.get("peak", e), e)
        m["session_peak"] = max(m.get("session_peak", e), e)
        dd = (m["peak"] - e) / m["peak"] * 100 if m["peak"] else 0
        if dd >= self.limits.max_drawdown_pct and not self.flags().get("drawdown_halt"):
            self.set_flag("drawdown_halt", True)
            self.notify("risk_threshold", f"Account drawdown {dd:.2f}% hit the limit; new entries halted until you reset")
        self.marks = m
        self.db.kv_set("marks", m)

    def account_state(self, now) -> AccountState:
        f = self.flags()
        m = self.marks
        led = self.tm.ledger
        active = self.tm.active_trades()
        open_risk = sum(max(0.0, ((t["entry_price"] or 0) - (t["stop_price"] or 0)) * (t["qty"] or 0)) for t in active)
        last_loss = self.tm.last_loss_at
        integ = dict(self.integrity)
        integ.update(self.tm.om.integrity_issues)
        return AccountState(
            now=now, equity=self.equity(), day_start_equity=m.get("day_start", self.equity()),
            week_start_equity=m.get("week_start", self.equity()), peak_equity=m.get("peak", self.equity()),
            session_peak_equity=m.get("session_peak", self.equity()), buying_power=led.buying_power(),
            exposure=led.exposure(self.prices), open_positions=len(active), open_risk=open_risk,
            open_symbols=tuple(t["symbol"] for t in active), consecutive_losses=self.tm.consecutive_losses,
            last_loss_at=dt.datetime.fromisoformat(last_loss) if last_loss else None,
            manual_pause=bool(f.get("paused")) or bool(self.vol_pause_until and now < self.vol_pause_until),
            emergency=bool(f.get("emergency")), drawdown_halt=bool(f.get("drawdown_halt")),
            integrity_ok=not integ, integrity_reasons=tuple(f"{k}: {v}" for k, v in list(integ.items())[:5]))

    # ------------------------------------------------------------ integrity
    def reconcile(self) -> None:
        """Broker vs ledger. Never auto-closes positions the bot does not own."""
        issues = {}
        try:
            acct = self.broker.get_account()
            if acct.get("trading_blocked") or acct.get("account_blocked"):
                issues["account"] = "broker reports trading/account blocked"
            clock = self.broker.get_clock()
            skew = abs((clock["timestamp"] - self.clock()).total_seconds())
            if skew > 10:
                issues["clock"] = f"system clock off by {skew:.0f}s vs broker"
            self.tm.om.poll_open()
            bpos = {p["symbol"].replace("/", ""): p for p in self.broker.list_positions()}
            ignored = set(self.flags().get("ignored_symbols", []))
            for sym, p in self.tm.ledger.positions.items():
                b = bpos.get(sym.replace("/", ""))
                bq = b["qty"] if b else 0.0
                if bq + 1e-6 < p.qty:
                    issues[f"pos:{sym}"] = f"ledger holds {p.qty:.6f} but broker holds {bq:.6f}"
            ours = {s.replace("/", "") for s in self.tm.ledger.positions}
            for sym, b in bpos.items():
                if sym not in ours and sym not in ignored and abs(b["qty"]) > 1e-9:
                    issues[f"unexpected:{sym}"] = f"unexpected broker position {b['qty']} {sym}"
            errs = self.tm.ledger.check_invariants()
            if errs:
                issues["ledger"] = "; ".join(errs)
            self.broker_failures = 0
        except BrokerError as e:
            self.broker_failures += 1
            if self.broker_failures >= 3:
                issues["broker"] = f"broker unreachable ({e})"
                self.notify("connection_lost", f"Broker connection problem: {e}")
            else:
                issues.update({k: v for k, v in self.integrity.items()})
        new = set(issues) - set(self.integrity)
        for k in new:
            lvl = "unexpected_position" if k.startswith("unexpected") else "critical"
            self.notify(lvl, f"Integrity: {issues[k]}")
            self.db.event("critical", "integrity", issues[k])
        if self.integrity and not issues:
            self.db.event("info", "integrity", "integrity restored")
        self.integrity = issues

    # ---------------------------------------------------------- market data
    def refresh_quotes(self, symbols: list[str]) -> None:
        if not symbols:
            return
        try:
            q = self.broker.latest_quotes(symbols)
        except BrokerError as e:
            self.broker_failures += 1
            log.warning("quotes failed: %s", e)
            return
        for s, v in q.items():
            bid, ask = v.get("bid") or 0, v.get("ask") or 0
            if bid > 0 and ask > 0 and ask >= bid:
                mid = (bid + ask) / 2
                self.prices[s] = mid
                self.spreads[s] = (ask - bid) / mid * 1e4
                self.quote_ts[s] = v.get("ts") or self.clock()

    def ensure_daily(self, today: dt.date) -> None:
        if self.daily_date == today:
            return
        syms = list(dict.fromkeys(self.watchlist + ["SPY", "XLK", "XLF", "XLV", "XLE", "XLI", "XLY", "XLP", "XLU",
                                                     "XLB", "XLC", "XLRE"]))
        try:
            d = self.hist._fetch(syms, "1Day", today - dt.timedelta(days=560), today - dt.timedelta(days=1),
                                 "us_equity", "sip")
            self.daily.update(d)
            for k in d:
                self._daily_asof[k] = today
            sect = {k: self.daily[k] for k in ("XLK", "XLF", "XLV", "XLE", "XLI", "XLY", "XLP", "XLU", "XLB", "XLC",
                                                "XLRE") if k in self.daily}
            if "SPY" in self.daily:
                self.regimes = daily_regimes(self.daily["SPY"], sect)
            self.daily_date = today
        except Exception as e:  # noqa: BLE001
            log.warning("daily refresh failed: %s", e)

    def fetch_bars(self, symbols: list[str], now: dt.datetime) -> None:
        need_hist = [s for s in symbols if s not in self.bars]
        if need_hist:
            start = now - dt.timedelta(days=45)
            try:
                self.bars.update(self.broker.recent_bars(need_hist, "5Min", start))
            except BrokerError as e:
                log.warning("history fetch failed: %s", e)
        have = [s for s in symbols if s in self.bars]
        if not have:
            return
        try:
            new = self.broker.recent_bars(have, "5Min", now - dt.timedelta(minutes=40))
        except BrokerError as e:
            log.warning("bar refresh failed: %s", e)
            return
        for s, d in new.items():
            old = self.bars.get(s)
            self.bars[s] = pd.concat([old, d]).loc[lambda x: ~x.index.duplicated(keep="last")].sort_index() if old is not None else d
            self.bars[s] = self.bars[s].iloc[-6000:]

    # ----------------------------------------------------------- strategies
    def active_strategies(self, timeframe: str = "5Min") -> tuple[list[dict], list[dict]]:
        """(active, shadow). Daily (overnight) strategies can only be ACTIVE when ALLOW_OVERNIGHT_HOLDS
        is set by the owner; hourly crypto needs ALLOW_CRYPTO_EXECUTION and is not wired to execution yet."""
        rows = self.db.query("SELECT * FROM strategy_status")
        fwd = self.db.kv_get("forward_status", {}) or {}
        active, shadow, book_only = [], [], []
        for r in rows:
            base = r["strategy"].split("+")[0]
            cls = BY_NAME.get(base)
            if cls is None:
                continue
            inst = cls(params=json.loads(r["params"] or "{}"))
            if inst.timeframe != timeframe:
                continue
            r["_inst"] = inst
            r["_oos"] = json.loads(r["oos_metrics"] or "{}")
            r["_allowed"] = set(json.loads(r["allowed_regimes"])) if r["allowed_regimes"] else None
            executable = inst.timeframe == "5Min" or (inst.timeframe == "1Day" and self.s.allow_overnight)
            fs = fwd.get(r["strategy"], {}).get("status")
            if fs == "demoted" and r["status"] != "rejected":
                # forward test failed (pre-registered rule in qt/books.py): keeps running in its book only
                book_only.append(dict(r, status="demoted", approved=0))
            elif fs == "promoted" and executable and r["status"] != "suspended":
                # forward-validated: its expectancy for sizing/gating comes from forward trades, not research
                active.append(dict(r, status="promoted", approved=1,
                                   _oos={**r["_oos"], "expectancy_r": fwd[r["strategy"]].get("mean_r") or 0.1,
                                         "holm_p": fwd[r["strategy"]].get("holm_p") or 0.05}))
            elif r["status"] == "validated" and r["approved"] and executable:
                active.append(r)
            elif (r["strategy"] in self.s.experimental_strategies and r["status"] in ("shadow", "validated")
                  and executable):
                # owner-approved experiment: trades paper although it did not pass validation; a strategy that
                # research rejects or that forward results suspend drops out automatically
                active.append(dict(r, status="experimental", approved=1))
            elif r["status"] in ("validated", "shadow"):
                if r["status"] == "validated" and r["approved"] and not executable:
                    r = dict(r, approved=0)  # approved, but overnight execution is switched off
                shadow.append(r)
            else:
                book_only.append(r)  # rejected / suspended: forward-tested in its own book, never traded
        self._book_only[timeframe] = book_only
        return active, shadow

    def daily_cycle(self, now: dt.datetime) -> None:
        """Once per session shortly after the open: daily (swing) strategies on CONFIRMED daily bars
        through yesterday; entries execute at today's open like the backtest; daily exits/trails."""
        from .strategies.features import daily_panel
        from .universe import DAILY_UNIVERSE
        today = now.astimezone(ET).date()
        active, shadow = self.active_strategies("1Day")
        wide = [u["symbol"] for u in self.universe
                if u.get("adv") and not u.get("leveraged") and not is_leveraged_or_inverse(u["symbol"], u.get("name", ""))
                and u["symbol"] not in DAILY_UNIVERSE][:self.s.swing_wide_n] if self.s.swing_wide_n > 0 else []
        # re-run today if the set of trading daily strategies changed since this morning's run (owner approval)
        stamp = today.isoformat() + "|" + ",".join(sorted(r["strategy"] for r in active)) + f"|wide{len(wide)}"
        done = self.db.kv_get("daily_cycle_done")
        if done == stamp or (done == today.isoformat() and not active):
            return
        scan = list(DAILY_UNIVERSE) + wide
        wide_set = set(wide)
        # refresh every symbol once per day (bars fetched yesterday lack yesterday's close)
        missing = [x for x in scan if self._daily_asof.get(x) != today]
        if missing:
            try:
                fresh = self.hist._fetch(missing, "1Day", today - dt.timedelta(days=560),
                                         today - dt.timedelta(days=1), "us_equity", "sip")
                self.daily.update(fresh)
                for k in fresh:
                    self._daily_asof[k] = today
            except Exception as e:  # noqa: BLE001
                log.warning("daily universe fetch failed: %s", e)
        confirmed = {}
        for sym, d in self.daily.items():
            if sym in scan and len(d) > 210:
                confirmed[sym] = d[d.index.tz_convert(ET).date < today]  # confirmed bars only
        feats = daily_panel(confirmed, min_len=210)
        # wide-universe names must actually move: drops T-bill / cash-like funds (ATR under 0.5% of price)
        feats = {k: f for k, f in feats.items()
                 if k not in wide_set or (f["atr"].iloc[-1] / f["close"].iloc[-1] >= 0.005)}
        regime_label = describe_current(self.regimes, today)["label"] if self.regimes is not None else "unknown"
        reg_c = coarse(regime_label)
        held = {t["symbol"]: t for t in self.tm.active_trades()}
        opps = []
        book_open = {(t["strategy"], t["symbol"]): t for t in self.books.open_trades()}
        for r in active + shadow + self._book_only.get("1Day", []):
            inst = r["_inst"]
            for sym, f in feats.items():
                in_wide = sym in wide_set and r["strategy"].split("+")[0] in self.s.swing_wide_strategies
                if inst.universe and sym not in inst.universe and not in_wide:
                    continue
                sig = inst.signals(f).iloc[-1]
                last = f.iloc[-1]
                bt = book_open.get((r["strategy"], sym))
                if bt:
                    held_days = len(pd.bdate_range(bt["entry_time"][:10], today)) - 1
                    self.books.daily_update(r["strategy"], sym, float(last["close"]), float(last["atr"]),
                                            bool(sig["exit_sig"]), held_days, now, self.prices)
                t = held.get(sym)
                if t and t["strategy"] == r["strategy"]:
                    ep = inst.exit_plan()
                    entry = t["entry_price"] or last["close"]
                    R = max(entry - (t["initial_stop"] or entry * 0.97), 1e-9)
                    if bool(sig["exit_sig"]):
                        self.tm.request_exit(t, "thesis_invalidated")
                        continue
                    held_days = len(pd.bdate_range(t["session"], today)) - 1 if t["session"] else 0
                    if ep.max_bars and held_days >= ep.max_bars:
                        self.tm.request_exit(t, "time_exit")
                        continue
                    if ep.trail_atr and last["close"] - entry >= ep.trail_activate_r * R:
                        self.tm.move_stop(t, float(last["close"] - ep.trail_atr * last["atr"]), "trail")
                    continue
                if not bool(sig["entry"]):
                    continue
                adv = float(last.get("dollar_vol20") or 0) or None
                o = Opportunity(symbol=sym, strategy=r["strategy"], price=float(self.prices.get(sym, last["close"])),
                                change_pct=0.0, relvol=None, momentum=float(last.get("ret63", 0) * 100), regime=regime_label,
                                stop=float(sig["stop"]), target=float(sig["target"]) if pd.notna(sig["target"]) else None,
                                atr=float(last["atr"]), spread_bps=self.spreads.get(sym, 5.0),
                                est_cost_bps=self.costs.round_trip_bps("us_equity", self.costs.spread_bps("us_equity", adv, sym)),
                                data_age_s=0.0, signal=True, strength=float(sig["strength"]),
                                strategy_status=r["status"], strategy_approved=bool(r["approved"]),
                                oos_expectancy_r=r["_oos"].get("expectancy_r"), oos_trades=int(r["_oos"].get("trades", 0)),
                                holm_p=r["_oos"].get("holm_p"), regime_allowed=(r["_allowed"] is None or reg_c in r["_allowed"]),
                                adv=adv)
                o.explanation = {"conditions": {"daily close": float(last["close"])}, "exit_plan": inst.exit_plan().describe(),
                                 "bar": str(f.index[-1].date()), "feed": "SIP daily (confirmed bars)", "timeframe": "1Day"}
                score(o, self.limits.stale_data_seconds, self.limits.max_spread_bps)
                opps.append(o)
        if opps:
            self.refresh_quotes([o.symbol for o in opps])
            for o in opps:
                o.price = float(self.prices.get(o.symbol, o.price))
                o.spread_bps = self.spreads.get(o.symbol, o.spread_bps)
            self.act(opps, now)
            self.feed_books(opps, now, "swing")
        self.db.kv_set("daily_cycle_done", stamp)

    # ------------------------------------------------------------ crypto (24/7, hourly)
    CRYPTO_CORE = ("BTC/USD", "ETH/USD", "SOL/USD", "PAXG/USD")  # PAXG = tokenised gold
    STABLECOINS = ("USDC", "USDT", "USDG", "DAI", "PYUSD", "TUSD", "BUSD", "FDUSD", "USDP")
    CRYPTO_MIN_ADV = 500_000.0  # $/day; thinner pairs have spreads that eat any edge

    def crypto_universe(self, today: dt.date) -> list[str]:
        if self._crypto_universe_date == today and self._crypto_universe:
            return self._crypto_universe
        try:
            assets = self.broker.list_assets("crypto")
            syms = sorted({a["symbol"] for a in assets if a.get("tradable") and a["symbol"].endswith("/USD")
                           and a["symbol"].split("/")[0] not in self.STABLECOINS})
        except Exception as e:  # noqa: BLE001
            log.warning("crypto asset list failed: %s", e)
            syms = []
        self._crypto_universe = list(dict.fromkeys(list(self.CRYPTO_CORE) + syms))
        self._crypto_universe_date = today
        return self._crypto_universe

    def crypto_strategies(self) -> list[dict]:
        active, shadow = self.active_strategies("1Hour")
        rows = active + shadow + self._book_only.get("1Hour", [])
        have = {r["strategy"] for r in rows}
        for name, cls in BY_NAME.items():  # never researched yet: still forward-tested in its own book
            inst = cls()
            if inst.timeframe == "1Hour" and name not in have:
                rows.append({"strategy": name, "status": "rejected", "approved": 0, "_inst": inst, "_oos": {},
                             "_allowed": None})
        return rows

    def crypto_cycle(self, now: dt.datetime) -> None:
        """Crypto never closes: every hour on the CONFIRMED hourly bar, hourly strategies run on liquid
        Alpaca crypto pairs (incl. PAXG gold). Real paper orders need ALLOW_CRYPTO_EXECUTION (not wired), so
        signals go to each strategy's own simulated $100 crypto book with Alpaca's taker fees."""
        from .strategies.features import hourly_crypto_features
        boundary = pd.Timestamp(now).floor("60min")
        if self._crypto_hour == boundary or (now - boundary.to_pydatetime()).total_seconds() < 60:
            return
        self._crypto_hour = boundary
        syms = self.crypto_universe(now.astimezone(ET).date())
        if not syms:
            return
        try:
            need_full = [x for x in syms if x not in self.crypto_bars]
            if need_full:
                self.crypto_bars.update(self.broker.recent_bars(need_full, "1Hour", now - dt.timedelta(days=45)))
            have = [x for x in syms if x in self.crypto_bars]
            if have:
                new = self.broker.recent_bars(have, "1Hour", now - dt.timedelta(hours=6))
                for k, d in new.items():
                    old = self.crypto_bars.get(k)
                    self.crypto_bars[k] = (pd.concat([old, d]).loc[lambda x: ~x.index.duplicated(keep="last")]
                                           .sort_index().iloc[-1500:]) if old is not None else d
        except BrokerError as e:
            log.warning("crypto bars failed: %s", e)
            return
        strategies = self.crypto_strategies()
        book_open = {(t["strategy"], t["symbol"]): t for t in self.books.open_trades() if "/" in t["symbol"]}
        opps, scanned, evaluated = [], 0, 0
        for sym in syms:
            d = self.crypto_bars.get(sym)
            if d is None or d.empty:
                continue
            scanned += 1
            d = d[d.index + pd.Timedelta(hours=1) <= pd.Timestamp(now)]  # confirmed bars only
            if len(d) < 500:
                continue
            f = hourly_crypto_features(d)
            f["session"] = f.index.date
            last = f.iloc[-1]
            adv = float(last["adv"]) if pd.notna(last["adv"]) else 0.0
            self.crypto_last[sym] = {"atr": float(last["atr"]), "close": float(last["close"]), "adv": adv}
            if sym not in self.CRYPTO_CORE and adv < self.CRYPTO_MIN_ADV:
                continue
            evaluated += 1
            for r in strategies:
                inst = r["_inst"]
                sig = inst.signals(f).iloc[-1]
                if (r["strategy"], sym) in book_open:
                    if bool(sig["exit_sig"]):
                        self.books.signal_exit(r["strategy"], sym, now, self.prices, self.spreads.get(sym))
                    continue
                if not bool(sig["entry"]):
                    continue
                spread = max(self.costs.spread_bps("crypto", adv, sym), 0.0)
                o = Opportunity(symbol=sym, strategy=r["strategy"], price=float(last["close"]), change_pct=0.0,
                                relvol=float(last["vol_rel"]) if pd.notna(last["vol_rel"]) else None, momentum=None,
                                regime="crypto", stop=float(sig["stop"]),
                                target=float(sig["target"]) if pd.notna(sig["target"]) else None,
                                atr=float(last["atr"]), spread_bps=spread,
                                est_cost_bps=self.costs.round_trip_bps("crypto", spread), data_age_s=0.0, signal=True,
                                strength=float(sig["strength"]), strategy_status=r["status"], strategy_approved=False,
                                oos_expectancy_r=r["_oos"].get("expectancy_r"), oos_trades=int(r["_oos"].get("trades", 0)),
                                holm_p=r["_oos"].get("holm_p"), regime_allowed=True, adv=adv or None)
                o.explanation = {"conditions": {"hourly close": float(last["close"])},
                                 "exit_plan": inst.exit_plan().describe(), "bar": str(f.index[-1]),
                                 "feed": "Alpaca crypto hourly (confirmed bars)", "timeframe": "1Hour"}
                score(o, self.limits.stale_data_seconds, self.limits.max_spread_bps)
                opps.append(o)
        if opps:
            self.refresh_quotes([o.symbol for o in opps])
            for o in opps:
                o.price = float(self.prices.get(o.symbol, o.price))
                live = self.spreads.get(o.symbol)
                if live is not None:  # real quoted spread when it is worse than the model
                    o.spread_bps = max(o.spread_bps, live)
                    o.est_cost_bps = self.costs.round_trip_bps("crypto", o.spread_bps)
                o.signal = o.signal and o.stop < o.price and o.spread_bps <= self.limits.max_spread_bps
            self.feed_books(opps, now, "crypto")
        self.db.kv_set("crypto_scan", {"at": now.isoformat(), "bar": str(boundary), "pairs": len(syms),
                                       "scanned": scanned, "evaluated": evaluated,
                                       "signals": [{"strategy": o.strategy, "symbol": o.symbol, "price": o.price,
                                                    "stop": o.stop, "spread_bps": round(o.spread_bps, 1),
                                                    "taken": bool(o.signal)} for o in opps],
                                       "strategies": [r["strategy"] for r in strategies]})

    def crypto_manage(self, now: dt.datetime) -> None:
        """Stops/targets/trails for crypto books every cycle, around the clock."""
        syms = sorted(s for s in self.books.symbols() if "/" in s)
        if not syms:
            return
        self.refresh_quotes(syms)
        bars = {s: {"atr": v["atr"]} for s, v in self.crypto_last.items()}
        self.books.manage(self.prices, bars, now, False, self.spreads, crypto_only=True)

    def upcoming_signals(self, today: dt.date) -> None:
        """After the close: swing signals on TODAY's confirmed daily bar = entries planned for the next session."""
        from .strategies.features import daily_panel
        from .universe import DAILY_UNIVERSE
        active, shadow = self.active_strategies("1Day")
        wide = [u["symbol"] for u in self.universe
                if u.get("adv") and not u.get("leveraged") and not is_leveraged_or_inverse(u["symbol"], u.get("name", ""))
                and u["symbol"] not in DAILY_UNIVERSE][:self.s.swing_wide_n] if self.s.swing_wide_n > 0 else []
        wide_set = set(wide)
        raw = self.hist._fetch(list(DAILY_UNIVERSE) + wide, "1Day", today - dt.timedelta(days=560), today,
                               "us_equity", "sip")
        feats = daily_panel({k: v for k, v in raw.items() if len(v) > 210}, min_len=210)
        feats = {k: f for k, f in feats.items()
                 if k not in wide_set or (f["atr"].iloc[-1] / f["close"].iloc[-1] >= 0.005)}
        out = []
        for r in active + shadow + self._book_only.get("1Day", []):
            inst = r["_inst"]
            if "+" in r["strategy"] and r not in active:
                continue
            for sym, f in feats.items():
                in_wide = sym in wide_set and r["strategy"].split("+")[0] in self.s.swing_wide_strategies
                if inst.universe and sym not in inst.universe and not in_wide:
                    continue
                sig = inst.signals(f).iloc[-1]
                if bool(sig["entry"]):
                    out.append({"strategy": r["strategy"], "status": r["status"], "symbol": sym,
                                "close": round(float(f["close"].iloc[-1]), 4), "stop": round(float(sig["stop"]), 4),
                                "real_paper_order": r in active, "bar": str(f.index[-1].date())})
        nxt = today + dt.timedelta(days=1)
        while session(nxt) is None:
            nxt += dt.timedelta(days=1)
        self.db.kv_set("upcoming", {"as_of": today.isoformat(), "next_session": nxt.isoformat(), "signals": out})

    def analyze(self, now: dt.datetime) -> list[Opportunity]:
        """Stage 2 on confirmed bars. Returns scored + classified opportunities."""
        active, shadow = self.active_strategies()
        strategies = active + shadow + self._book_only.get("5Min", [])
        regime_label = "unknown"
        if self.regimes is not None:
            regime_label = describe_current(self.regimes, now.astimezone(ET).date())["label"]
        reg_c = coarse(regime_label)
        state = self.account_state(now)
        held = list(state.open_symbols)
        rets = pd.DataFrame({s: d["close"].pct_change() for s, d in self.bars.items() if len(d) > 100}).tail(500)
        spy_f = None
        opps: list[Opportunity] = []
        feats = {}
        for sym in self.watchlist:
            raw = self.bars.get(sym)
            if raw is None or len(raw) < 200:
                continue
            rth = to_rth(raw, BAR_MIN)
            # only bars that have CLOSED
            rth = rth[rth.index + pd.Timedelta(minutes=BAR_MIN) <= now]
            if rth.empty:
                continue
            if sym == "SPY":
                spy_f = intraday_features(rth, self.daily.get("SPY"), None, BAR_MIN)
                feats[sym] = spy_f
        for sym in self.watchlist:
            raw = self.bars.get(sym)
            if raw is None or len(raw) < 200:
                continue
            if sym not in feats:
                rth = to_rth(raw, BAR_MIN)
                rth = rth[rth.index + pd.Timedelta(minutes=BAR_MIN) <= now]
                if rth.empty:
                    continue
                feats[sym] = intraday_features(rth, self.daily.get(sym), spy_f, BAR_MIN)
            f = feats[sym]
            last = f.iloc[-1]
            if last["session"] != now.astimezone(ET).date():
                continue
            bar_end = f.index[-1] + pd.Timedelta(minutes=BAR_MIN)
            age = (now - bar_end.to_pydatetime()).total_seconds()
            qage = (now - self.quote_ts[sym]).total_seconds() if sym in self.quote_ts else 1e9
            spread = self.spreads.get(sym, 99.0)
            adv = float((self.daily[sym]["close"] * self.daily[sym]["volume"]).tail(20).mean()) \
                if sym in self.daily and len(self.daily[sym]) else None
            self.last_closed_bar[sym] = {"close": float(last["close"]), "atr": float(last["atr"]) if pd.notna(last["atr"]) else 0.0,
                                         "bar_minutes": BAR_MIN, "exit_sig": False, "tf": "5Min"}
            for r in strategies:
                inst = r["_inst"]
                sig = inst.signals(f).iloc[-1]
                if sym in held:
                    t = next((x for x in self.tm.active_trades() if x["symbol"] == sym), None)
                    if t and t["strategy"] == r["strategy"]:
                        self.last_closed_bar[sym]["exit_sig"] = bool(sig["exit_sig"])
                allowed = r["_allowed"]
                o = Opportunity(
                    symbol=sym, strategy=r["strategy"], price=float(self.prices.get(sym, last["close"])),
                    change_pct=float((last["close"] / last["prev_close"] - 1) * 100) if pd.notna(last.get("prev_close")) else 0.0,
                    relvol=float(last["relvol"]) if pd.notna(last.get("relvol")) else None,
                    momentum=float(last["roc6"] * 100) if pd.notna(last.get("roc6")) else None, regime=regime_label,
                    stop=float(sig["stop"]) if pd.notna(sig["stop"]) else None,
                    target=float(sig["target"]) if pd.notna(sig["target"]) else None,
                    atr=float(last["atr"]) if pd.notna(last["atr"]) else 0.0, spread_bps=spread,
                    est_cost_bps=self.costs.round_trip_bps("us_equity", self.costs.spread_bps("us_equity", adv, sym)),
                    data_age_s=max(age, qage if qage < 1e8 else age), signal=bool(sig["entry"]),
                    strength=float(sig["strength"]), strategy_status=r["status"], strategy_approved=bool(r["approved"]),
                    oos_expectancy_r=r["_oos"].get("expectancy_r"), oos_trades=int(r["_oos"].get("trades", 0)),
                    holm_p=r["_oos"].get("holm_p"), regime_allowed=(allowed is None or reg_c in allowed), adv=adv,
                    max_corr=correlation_with(rets, sym, held))
                o.explanation = {"conditions": inst.explain(last), "exit_plan": inst.exit_plan().describe(),
                                 "bar_end": str(bar_end), "feed": "IEX (single exchange)"}
                score(o, self.limits.stale_data_seconds, self.limits.max_spread_bps)
                opps.append(o)
        return opps

    def act(self, opps: list[Opportunity], now: dt.datetime) -> None:
        signals = [o for o in opps if o.signal]
        signals.sort(key=lambda o: -o.score)
        state = self.account_state(now)
        for o in opps:
            gr = None
            if o.signal and self._may_trade(o) and o.stop:
                prop = Proposal(o.symbol, "us_equity", o.strategy, o.price, o.stop, o.est_cost_bps, o.spread_bps,
                                o.data_age_s, o.atr, o.max_corr, o.oos_expectancy_r)
                d = self.guardian.evaluate(prop, state)
                o.explanation["guardian"] = d.to_dict()
                gr = None if d.approved else d.reasons
            classify(o, gr, self.limits.stale_data_seconds, self.limits.max_spread_bps)
        executed = 0
        for o in signals:
            if o.status != "EXECUTE":
                continue
            state = self.account_state(now)  # re-check after each submission
            prop = Proposal(o.symbol, "us_equity", o.strategy, o.price, o.stop, o.est_cost_bps, o.spread_bps,
                            o.data_age_s, o.atr, o.max_corr, o.oos_expectancy_r)
            d = self.guardian.evaluate(prop, state)
            if not d.approved:
                o.status, o.reasons = "REJECT", [f"guardian: {x}" for x in d.reasons]
                continue
            asset = self.broker.get_asset(o.symbol)
            if not asset.get("tradable") or not asset.get("fractionable"):
                o.status, o.reasons = "REJECT", ["asset not tradable/fractionable at Alpaca"]
                continue
            if is_leveraged_or_inverse(o.symbol, asset.get("name", "")):
                o.status, o.reasons = "REJECT", ["leveraged/inverse fund (excluded by owner)"]
                continue
            q = self.broker.latest_quotes([o.symbol]).get(o.symbol, {})
            ask = q.get("ask") or o.price
            inst = BY_NAME[o.strategy.split("+")[0]]()
            ep = inst.exit_plan()
            competitors = [f"{x.symbol}/{x.strategy}:{x.status}({x.score})" for x in signals if x is not o][:8]
            tid = now.strftime("%Y%m%d%H%M%S") + uuid.uuid4().hex[:6]
            plan = TradePlan(
                trade_id=tid, symbol=o.symbol, asset_class="us_equity", strategy=o.strategy, strategy_version=inst.version,
                notional=d.notional, entry_ref=ask, stop=o.stop, target=o.target, trail_atr=ep.trail_atr,
                trail_activate_r=ep.trail_activate_r, breakeven_r=ep.breakeven_r, max_bars=ep.max_bars,
                flatten_eod=ep.flatten_eod, atr=o.atr, score=o.score, regime=o.regime,
                thesis=(("EXPERIMENTAL (unvalidated, owner-approved) | " if o.strategy_status == "experimental" else "")
                        + f"{inst.description} | score {o.score}"), exit_plan=ep.describe(),
                explanation={"opportunity": o.to_row(), "competing_opportunities": competitors,
                             "guardian": d.to_dict(), "experimental": o.strategy_status == "experimental"}, est_cost_bps=o.est_cost_bps, planned_risk=d.risk_dollars,
                timeframe=inst.timeframe)
            if self.tm.open_trade(plan):
                executed += 1
        for o in opps:
            self.db.insert("opportunities", {
                "ts": now.isoformat(), "symbol": o.symbol, "strategy": o.strategy, "price": o.price,
                "change_pct": o.change_pct, "relvol": o.relvol, "momentum": o.momentum, "regime": o.regime,
                "score": o.score, "est_cost_bps": o.est_cost_bps, "status": o.status,
                "reason": "; ".join(o.reasons)[:500], "detail": {"components": o.components, **o.explanation}})
        self.db.kv_set("last_scan_summary", {"ts": now.isoformat(), "evaluated": len(opps), "signals": len(signals),
                                             "executed": executed, "watchlist": self.watchlist})
        self._shadow(opps, now)

    FORCED = "FORCED_daily_min"

    def force_daily_minimum(self, opps: list[Opportunity], now: dt.datetime, open_: dt.datetime) -> None:
        """PAPER ONLY. Owner asked for at least one trade per day: when nothing research-backed has traded by
        FORCE_TRADE_AFTER_MIN after the open, buy the best-ranked opportunity anyway. Every guardian limit still
        applies, the stop is mandatory, and the position is closed by the end of the day. Tagged FORCED_daily_min."""
        if self.s.mode != "paper" or self.s.min_daily_trades <= 0 or self.integrity:
            return
        if now < open_ + dt.timedelta(minutes=self.s.force_trade_after_min):
            return
        today = now.date().isoformat()
        n = self.db.one("SELECT COUNT(*) n FROM trades WHERE session=?", (today,))["n"]
        if n >= self.s.min_daily_trades:
            return
        L = self.limits
        held = {t["symbol"] for t in self.tm.active_trades()}
        cands = [o for o in opps if o.price > 0 and o.atr > 0 and o.symbol not in held
                 and o.data_age_s <= L.stale_data_seconds and o.spread_bps <= L.max_spread_bps]
        cands.sort(key=lambda o: (not o.signal, -o.score))
        if not cands:
            cands = self._scanner_candidates(now, held)
        for o in cands[:10]:
            fallback = o.price - max(3 * o.atr, 0.01 * o.price)
            stop = o.stop if (o.stop and o.stop < o.price * 0.997 and o.stop > o.price * 0.95) else fallback
            state = self.account_state(now)
            prop = Proposal(o.symbol, "us_equity", self.FORCED, o.price, stop, o.est_cost_bps, o.spread_bps,
                            o.data_age_s, o.atr, o.max_corr, None)
            d = self.guardian.evaluate(prop, state)
            if not d.approved:
                log.info("forced trade candidate %s rejected by guardian: %s", o.symbol, d.reasons)
                continue
            asset = self.broker.get_asset(o.symbol)
            if not asset.get("tradable") or not asset.get("fractionable"):
                continue
            if is_leveraged_or_inverse(o.symbol, asset.get("name", "")):
                log.info("forced trade candidate %s skipped: leveraged/inverse fund", o.symbol)
                continue
            q = self.broker.latest_quotes([o.symbol]).get(o.symbol, {})
            tid = now.strftime("%Y%m%d%H%M%S") + uuid.uuid4().hex[:6]
            plan = TradePlan(
                trade_id=tid, symbol=o.symbol, asset_class="us_equity", strategy=self.FORCED, strategy_version="1",
                notional=d.notional, entry_ref=q.get("ask") or o.price, stop=stop, target=None, trail_atr=2.0,
                trail_activate_r=1.0, breakeven_r=1.0, max_bars=0, flatten_eod=True, atr=o.atr, score=o.score,
                regime=o.regime,
                thesis=(f"FORCED daily-minimum trade (owner request; NOT a validated signal) | best-ranked: "
                        f"{o.strategy} score {o.score}{' with entry signal' if o.signal else ', no entry signal'}"),
                exit_plan="stop at entry-%.2f; breakeven at +1R; 2xATR trail from +1R; flat by the close" % (o.price - stop),
                explanation={"opportunity": o.to_row(), "guardian": d.to_dict(), "forced": True},
                est_cost_bps=o.est_cost_bps, planned_risk=d.risk_dollars, timeframe="5Min")
            if self.tm.open_trade(plan):
                self.db.event("important", "forced_trade",
                              f"FORCED daily-minimum paper trade: {o.symbol} ${d.notional:.2f}, stop {stop:.2f} "
                              f"(source {o.strategy}, score {o.score}); not a validated signal")
                self.notify("trade_open", f"Forced daily-minimum paper trade: {o.symbol} (not a validated signal)")
            return

    def _scanner_candidates(self, now: dt.datetime, held: set) -> list[Opportunity]:
        """Fallback for the forced trade when no strategy produced a candidate: the stage-1 scanner's top-ranked
        liquid names that are moving up from the open, with a fresh quote and 5-minute bars for the ATR stop."""
        from .indicators import atr as atr_fn
        top = (self.db.kv_get("stage1") or {}).get("top") or []
        out = []
        for row in top:
            sym = row.get("symbol")
            if not sym or sym in held or sym == "SPY" or (row.get("from_open_pct") or 0) <= 0:
                continue
            bars = self.bars.get(sym)
            px = self.prices.get(sym)
            if bars is None or len(bars) < 20 or not px or sym not in self.quote_ts:
                continue
            a = float(atr_fn(bars.tail(60)).iloc[-1])
            if not a or a != a:
                continue
            age = (now - self.quote_ts[sym]).total_seconds()
            spread = self.spreads.get(sym, 99.0)
            if age > self.limits.stale_data_seconds or spread > self.limits.max_spread_bps:
                continue
            adv = row.get("adv")
            o = Opportunity(symbol=sym, strategy="stage1_scanner", price=float(px),
                            change_pct=float(row.get("change_pct") or 0), relvol=row.get("relvol_iex"),
                            momentum=row.get("from_open_pct"), regime="unknown", stop=None, target=None, atr=a,
                            spread_bps=spread,
                            est_cost_bps=self.costs.round_trip_bps("us_equity", self.costs.spread_bps("us_equity", adv, sym)),
                            data_age_s=age, signal=False, strength=0.0, strategy_status="none", strategy_approved=False,
                            oos_expectancy_r=None, oos_trades=0, holm_p=None, regime_allowed=True, adv=adv)
            o.score = round(float(row.get("stage1_score") or 0), 2)
            out.append(o)
        return out

    def feed_books(self, opps: list[Opportunity], now: dt.datetime, engine: str) -> None:
        """Every signal of every strategy goes to that strategy's own simulated $100 book (forward test)."""
        bar_minutes = {"swing": 1440, "crypto": 60}.get(engine, BAR_MIN)
        asset_class = "crypto" if engine == "crypto" else "us_equity"
        for o in opps:
            if not o.signal or "+" in o.strategy or not o.stop:
                continue  # books track base strategies; regime-gated variants are research-only
            cls = BY_NAME.get(o.strategy)
            if cls is None:
                continue
            try:
                self.books.consider(o, cls().exit_plan(), engine, now, self.prices, bar_minutes,
                                   asset_class=asset_class)
            except Exception:  # noqa: BLE001
                log.exception("book entry failed for %s %s", o.strategy, o.symbol)

    @staticmethod
    def _may_trade(o) -> bool:
        return o.strategy_status in ("validated", "experimental", "promoted") and bool(o.strategy_approved)

    def _shadow(self, opps, now):
        """Record signals of non-approved strategies as shadow trades (forward test without orders)."""
        for o in opps:
            if o.signal and not self._may_trade(o) and o.stop and o.strategy_status in ("validated", "shadow"):
                key = f"sh-{o.strategy}-{o.symbol}-{now.astimezone(ET).date()}"
                if self.db.one("SELECT 1 FROM shadow_trades WHERE trade_id LIKE ?", (key + "%",)):
                    continue
                self.db.insert("shadow_trades", {"trade_id": key + now.strftime("%H%M"), "strategy": o.strategy,
                                                 "symbol": o.symbol, "entry_time": now.isoformat(), "entry_price": o.price,
                                                 "status": "open", "data": {"stop": o.stop, "target": o.target,
                                                                            "atr": o.atr, "overnight": o.explanation.get("timeframe") == "1Day"}})

    def manage_shadow(self, now, flatten):
        for t in self.db.query("SELECT * FROM shadow_trades WHERE status='open'"):
            px = self.prices.get(t["symbol"])
            d = json.loads(t["data"] or "{}")
            if px is None:
                continue
            reason = None
            if px <= d.get("stop", 0):
                reason = "stop"
            elif d.get("target") and px >= d["target"]:
                reason = "target"
            elif flatten and not d.get("overnight"):
                reason = "eod_flatten"
            elif d.get("overnight") and (now - dt.datetime.fromisoformat(t["entry_time"])).days >= 14:
                reason = "time_exit"
            if reason:
                R = max(t["entry_price"] - d["stop"], 1e-9)
                r = (px - t["entry_price"]) / R
                self.db.execute("UPDATE shadow_trades SET status='closed', exit_time=?, exit_price=?, r_multiple=? "
                                "WHERE trade_id=?", (now.isoformat(), px, r, t["trade_id"]))

    # ------------------------------------------------------------- commands
    def process_commands(self) -> None:
        for c in self.db.query("SELECT * FROM commands WHERE status='pending' ORDER BY id"):
            cmd, args = c["command"], json.loads(c["args"] or "{}")
            res = "ok"
            try:
                if cmd == "pause":
                    self.set_flag("paused", True)
                elif cmd == "resume":
                    self.set_flag("paused", False)
                elif cmd == "start_paper":
                    self.set_flag("paused", False)
                    self.set_flag("emergency", False)
                elif cmd == "emergency_shutdown":
                    self.set_flag("emergency", True)
                    self.notify("emergency", "EMERGENCY SHUTDOWN: new entries stopped; open positions stay protected")
                elif cmd == "clear_emergency":
                    self.set_flag("emergency", False)
                elif cmd == "reset_drawdown_halt":
                    self.set_flag("drawdown_halt", False)
                    self.marks["peak"] = self.equity()
                    self.db.kv_set("marks", self.marks)
                elif cmd == "close_all":
                    for t in self.tm.active_trades():
                        self.tm.request_exit(t, "owner_close_all")
                elif cmd == "cancel_pending":
                    for t in self.tm.active_trades():
                        if t["status"] == "pending_entry":
                            self.tm.request_exit(t, "owner_cancel")
                elif cmd == "set_risk_limits":
                    lim = RiskLimits.from_dict({**self.limits.to_dict(), **args})
                    errs = check_within_ceiling(lim, self.s.risk_ceiling)
                    if errs:
                        res = "refused: " + "; ".join(errs)
                    else:
                        self.limits = lim
                        self.guardian = CapitalGuardian(lim)
                        self.db.kv_set("risk_limits", lim.to_dict())
                elif cmd in ("approve_strategy", "disable_strategy", "enable_strategy"):
                    name = args["strategy"]
                    row = self.db.one("SELECT * FROM strategy_status WHERE strategy=?", (name,))
                    if not row:
                        res = "unknown strategy"
                    elif cmd == "approve_strategy":
                        if row["status"] != "validated":
                            res = f"refused: strategy is {row['status']}, only validated strategies can be approved"
                        else:
                            self.db.execute("UPDATE strategy_status SET approved=1, approved_at=? WHERE strategy=?",
                                            (self.clock().isoformat(), name))
                    elif cmd == "disable_strategy":
                        self.db.execute("UPDATE strategy_status SET approved=0 WHERE strategy=?", (name,))
                    else:
                        res = "use approve_strategy (enabling requires validation)"
                elif cmd == "ignore_symbol":
                    f = self.flags()
                    f.setdefault("ignored_symbols", []).append(args["symbol"])
                    self.db.kv_set("flags", f)
                else:
                    res = f"unknown command {cmd}"
            except Exception as e:  # noqa: BLE001
                res = f"error: {e}"
            self.db.execute("UPDATE commands SET status='done', result=?, processed_at=? WHERE id=?",
                            (res, self.clock().isoformat(), c["id"]))
            self.db.event("info", "command", f"{cmd} by {c['requested_by']}: {res}")

    # ----------------------------------------------------------------- cycle
    def cycle(self) -> None:
        now = self.clock()
        self.db.heartbeat("engine", "ok", {"version": VERSION, "mode": self.s.mode, "since": self.started_at.isoformat()})
        self.process_commands()
        today = now.astimezone(ET).date()
        sess = session(today)
        held = [t["symbol"] for t in self.tm.active_trades()]
        if time.monotonic() - self.last_recon > 60:
            self.reconcile()
            self.last_recon = time.monotonic()
        shadow_syms = [r["symbol"] for r in self.db.query("SELECT DISTINCT symbol FROM shadow_trades WHERE status='open'")]
        book_syms = sorted(self.books.symbols())
        self.refresh_quotes(list(dict.fromkeys(held + self.watchlist + shadow_syms + book_syms)) if sess else held)
        self.update_marks(now)
        # stale-data protection for held symbols
        for s in held:
            ts = self.quote_ts.get(s)
            if sess and sess[0] < now < sess[1] and (ts is None or (now - ts).total_seconds() > self.limits.stale_data_seconds):
                self.integrity[f"stale:{s}"] = "market data stale for a held position (possible halt)"
            else:
                self.integrity.pop(f"stale:{s}", None)
        try:
            self.crypto_cycle(now)
            self.crypto_manage(now)
        except Exception:  # noqa: BLE001
            log.exception("crypto cycle failed")
        if sess is None:
            self._equity_snapshot(now)
            self.publish_status(now)
            return
        open_, close_ = sess
        pre = open_ - dt.timedelta(minutes=30)
        if now < pre or now > close_ + dt.timedelta(minutes=30):
            self._equity_snapshot(now)
            if now > close_ and self.report_done != today.isoformat():
                self.daily_report(today)
            if now > close_ and self.db.kv_get("books_evaluated") != today.isoformat():
                try:
                    self.books.evaluate(now)
                    self.upcoming_signals(today)
                except Exception:  # noqa: BLE001
                    log.exception("end-of-day book evaluation failed")
                self.db.kv_set("books_evaluated", today.isoformat())
            self.publish_status(now)
            return
        self.ensure_daily(today)
        # Stage 1 scan (premarket and every 10 min in session)
        if time.time() - self.last_scan > 600 and self.universe:
            self.stage1(now, open_, close_)
        if open_ + dt.timedelta(minutes=2) <= now < close_ - dt.timedelta(minutes=30):
            self.daily_cycle(now)
        flatten = now >= close_ - dt.timedelta(minutes=10)
        cutoff = now >= close_ - dt.timedelta(minutes=30)
        # Stage 2 on each new confirmed 5-minute bar (+10 s for the bar to publish)
        if open_ <= now <= close_:
            boundary = pd.Timestamp(now).floor(f"{BAR_MIN}min")
            if self.last_bar_end.get("_") != boundary and (now - boundary.to_pydatetime()).total_seconds() >= 10:
                self.last_bar_end["_"] = boundary
                self.fetch_bars(list(dict.fromkeys(self.watchlist + held)), now)
                self._volatility_guard(now)
                opps = self.analyze(now)
                if cutoff:
                    for o in opps:
                        o.signal = False if o.signal else o.signal
                self.act(opps, now)
                if not cutoff:
                    self.feed_books(opps, now, "day")
                    self.force_daily_minimum(opps, now, open_)
        self.tm.manage(self.prices, self.last_closed_bar, now, flatten, market_open=open_ <= now <= close_)
        self.manage_shadow(now, flatten)
        if open_ <= now <= close_:
            try:
                self.books.manage(self.prices, self.last_closed_bar, now, flatten, self.spreads, stocks_only=True)
            except Exception:  # noqa: BLE001
                log.exception("book management failed")
        for k in list(self.last_closed_bar):
            self.last_closed_bar[k]["exit_sig"] = False
        self._equity_snapshot(now)
        self.publish_status(now)

    def publish_status(self, now):
        """State the dashboard reads (the API never talks to the broker)."""
        st = self.account_state(now)
        g = self.guardian.status(st)
        g["profit_preservation"] = "armed" if self.limits.profit_giveback_enabled else "off"
        self.db.kv_set("guardian_status", g)
        held = {t["symbol"] for t in self.tm.active_trades()}
        self.db.kv_set("live", {"ts": now.isoformat(), "prices": {s: self.prices.get(s) for s in held},
                                "spreads": {s: self.spreads.get(s) for s in held},
                                "integrity": dict(self.integrity, **self.tm.om.integrity_issues),
                                "broker_failures": self.broker_failures,
                                "quote_age_s": {s: (now - self.quote_ts[s]).total_seconds() for s in self.quote_ts}})

    def stage1(self, now, open_, close_):
        try:
            syms = [u["symbol"] for u in self.universe]
            snaps = self.broker.snapshots(syms)
            ranked = stage1_rank(snaps, {u["symbol"]: u["adv"] for u in self.universe}, now, open_, close_)
            held = [t["symbol"] for t in self.tm.active_trades()]
            self.watchlist = choose_watchlist(ranked, held, 30)
            self.db.kv_set("watchlist", self.watchlist)
            self.db.kv_set("stage1", {"ts": now.isoformat(), "scanned": len(snaps),
                                      "top": ranked.head(30).replace({np.nan: None}).to_dict("records") if not ranked.empty else []})
            self.last_scan = time.time()
        except Exception as e:  # noqa: BLE001
            log.warning("stage1 failed: %s", e)
            self.last_scan = time.time() - 300

    def _volatility_guard(self, now):
        spy = self.bars.get("SPY")
        if spy is None or len(spy) < 30:
            return
        last = spy.iloc[-1]
        rng = (last["high"] - last["low"]) / last["close"]
        med = ((spy["high"] - spy["low"]) / spy["close"]).tail(400).median()
        if med and rng > 6 * med:
            self.vol_pause_until = now + dt.timedelta(minutes=15)
            self.notify("risk_threshold", f"Abnormal SPY volatility (bar range {rng*100:.2f}%); new entries paused 15 min")

    def _equity_snapshot(self, now):
        last = self.db.kv_get("last_equity_ts")
        if last and (now - dt.datetime.fromisoformat(last)).total_seconds() < 300:
            return
        led = self.tm.ledger
        self.db.upsert("equity", {"ts": now.isoformat(), "equity": self.equity(), "cash": led.total_cash,
                                  "settled": led.settled_cash, "unsettled": led.unsettled_cash,
                                  "positions_value": led.positions_value(self.prices), "realized": led.realized_pnl,
                                  "unrealized": led.unrealized_pnl(self.prices), "source": f"{self.s.mode} virtual ledger"},
                       "ts")
        self.db.kv_set("last_equity_ts", now.isoformat())

    def daily_report(self, day: dt.date):
        spy = None
        try:
            d = self.daily.get("SPY")
            snaps = self.broker.snapshots(["SPY"])
            v = snaps.get("SPY", {})
            if v.get("price") and v.get("prev_close"):
                spy = (v["price"] / v["prev_close"] - 1) * 100
        except Exception:  # noqa: BLE001
            pass
        rep = build_report(self.db, "daily", day, day, spy)
        save_report(self.db, rep, to_csv(self.db, day, day))
        if day.weekday() == 4:
            a = day - dt.timedelta(days=day.weekday())
            wk = build_report(self.db, "weekly", a, day)
            save_report(self.db, wk, to_csv(self.db, a, day))
        nxt = day + dt.timedelta(days=1)
        if nxt.month != day.month or not any(session(day + dt.timedelta(days=i)) and (day + dt.timedelta(days=i)).month == day.month for i in range(1, 7)):
            a = day.replace(day=1)
            mo = build_report(self.db, "monthly", a, day)
            save_report(self.db, mo, to_csv(self.db, a, day))
        self.notify("daily_report", render_text(rep)[:3500])
        self.report_done = day.isoformat()
        self.db.kv_set("report_done", self.report_done)

    # ---------------------------------------------------------------- run
    def startup(self) -> None:
        # a slow first start (small server, empty caches) must not look dead to the container health check,
        # or autoheal restarts the engine before it ever reaches the trading loop
        done = threading.Event()

        def beat():
            while not done.wait(30):
                self.db.heartbeat("engine", "starting", {"version": VERSION, "mode": self.s.mode})
        self.db.heartbeat("engine", "starting", {"version": VERSION, "mode": self.s.mode})
        threading.Thread(target=beat, name="startup-heartbeat", daemon=True).start()
        try:
            self._startup()
        finally:
            done.set()

    def _startup(self) -> None:
        log.info("engine %s starting in %s mode; reconciling with the broker", VERSION, self.s.mode)
        self.db.event("info", "startup", f"engine {VERSION} starting in {self.s.mode.upper()} mode; reconciling first")
        self.reconcile()
        log.info("reconciliation done; integrity issues: %s", self.integrity or "none")
        self.last_recon = time.monotonic()
        if self.integrity:
            self.db.event("warning", "startup", "integrity issues at startup; entries blocked until resolved",
                          self.integrity)
        # intraday positions left over from a previous session (e.g. crash before flatten)
        today = self.clock().astimezone(ET).date().isoformat()
        for t in self.tm.active_trades():
            plan = json.loads(t["explanation"] or "{}").get("plan", {})
            if t["session"] and t["session"] < today and plan.get("flatten_eod", True):
                self.db.event("warning", "stale_position", f"{t['symbol']} from {t['session']} will be flattened")
                self.tm.request_exit(t, "stale_intraday_position")
        if not self.universe or self.db.kv_get("universe_date") != today:
            try:
                log.info("building discovery universe")
                self.universe = build_discovery_universe(self.broker, self.hist)
                log.info("discovery universe: %d symbols", len(self.universe))
                self.db.kv_set("discovery_universe", self.universe)
                self.db.kv_set("universe_date", today)
            except Exception as e:  # noqa: BLE001
                log.warning("discovery universe build failed: %s", e)
                self.universe = [{"symbol": s, "adv": None} for s in default_universe()]

    def run_forever(self) -> None:
        self.startup()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                signal.signal(sig, lambda *_: self._stop.set())
            except ValueError:
                pass
        while not self._stop.is_set():
            t0 = time.monotonic()
            try:
                self.cycle()
                self.integrity.pop("engine", None)
            except Exception as e:  # noqa: BLE001
                log.exception("cycle failed")
                self.integrity["engine"] = f"cycle error: {type(e).__name__}"
                self.db.event("error", "cycle", f"{type(e).__name__}: {e}")
                self.notify("error", f"Engine cycle error: {type(e).__name__}: {e}")
                try:
                    self.tm.manage(self.prices, {}, self.clock(), False)  # keep managing exits
                except Exception:  # noqa: BLE001
                    log.exception("position management failed")
            self._stop.wait(max(1.0, LOOP_SECONDS - (time.monotonic() - t0)))
        self.db.event("info", "shutdown", "engine stopped gracefully (broker-side stops remain in place)")
        self.db.heartbeat("engine", "stopped")


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    s = load_settings()
    Engine(s).run_forever()


if __name__ == "__main__":
    main()
