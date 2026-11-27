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
from .db import DB
from .execution import TradeManager, TradePlan
from .guardian import AccountState, CapitalGuardian, Proposal
from .ledger import Ledger
from .markettime import ET, PT, fmt_pt, now_utc, pt_schedule_for, session
from .notify import Notifier
from .regime import coarse, daily_regimes, describe_current
from .reporting import build_report, render_text, save_report, to_csv
from .scanner import build_discovery_universe, choose_watchlist, correlation_with, default_universe, stage1_rank
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
        self.marks = self.db.kv_get("marks", {})
        self.bars: dict[str, pd.DataFrame] = {}
        self.daily: dict[str, pd.DataFrame] = {}
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
            d = self.hist._fetch(syms, "1Day", today - dt.timedelta(days=420), today - dt.timedelta(days=1),
                                 "us_equity", "sip")
            self.daily.update(d)
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
    def active_strategies(self) -> tuple[list[dict], list[dict]]:
        rows = self.db.query("SELECT * FROM strategy_status")
        active, shadow = [], []
        for r in rows:
            base = r["strategy"].split("+")[0]
            cls = BY_NAME.get(base)
            if cls is None:
                continue
            inst = cls(params=json.loads(r["params"] or "{}"))
            if inst.timeframe != "5Min":
                continue  # daily / crypto execution requires separate approval (overnight / 24-7)
            r["_inst"] = inst
            r["_oos"] = json.loads(r["oos_metrics"] or "{}")
            r["_allowed"] = set(json.loads(r["allowed_regimes"])) if r["allowed_regimes"] else None
            if r["status"] == "validated" and r["approved"]:
                active.append(r)
            elif r["status"] in ("validated", "shadow"):
                shadow.append(r)
        return active, shadow

    def analyze(self, now: dt.datetime) -> list[Opportunity]:
        """Stage 2 on confirmed bars. Returns scored + classified opportunities."""
        active, shadow = self.active_strategies()
        strategies = active + shadow
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
                                         "bar_minutes": BAR_MIN, "exit_sig": False}
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
            if o.signal and o.strategy_status == "validated" and o.strategy_approved and o.stop:
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
                thesis=f"{inst.description} | score {o.score}", exit_plan=ep.describe(),
                explanation={"opportunity": o.to_row(), "competing_opportunities": competitors,
                             "guardian": d.to_dict()}, est_cost_bps=o.est_cost_bps, planned_risk=d.risk_dollars)
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

    def _shadow(self, opps, now):
        """Record signals of non-approved strategies as shadow trades (forward test without orders)."""
        for o in opps:
            if o.signal and not (o.strategy_status == "validated" and o.strategy_approved) and o.stop:
                key = f"sh-{o.strategy}-{o.symbol}-{now.astimezone(ET).date()}"
                if self.db.one("SELECT 1 FROM shadow_trades WHERE trade_id LIKE ?", (key + "%",)):
                    continue
                self.db.insert("shadow_trades", {"trade_id": key + now.strftime("%H%M"), "strategy": o.strategy,
                                                 "symbol": o.symbol, "entry_time": now.isoformat(), "entry_price": o.price,
                                                 "status": "open", "data": {"stop": o.stop, "target": o.target,
                                                                            "atr": o.atr}})

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
            elif flatten:
                reason = "eod_flatten"
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
        self.refresh_quotes(list(dict.fromkeys(held + self.watchlist)) if sess else held)
        self.update_marks(now)
        # stale-data protection for held symbols
        for s in held:
            ts = self.quote_ts.get(s)
            if sess and sess[0] < now < sess[1] and (ts is None or (now - ts).total_seconds() > self.limits.stale_data_seconds):
                self.integrity[f"stale:{s}"] = "market data stale for a held position (possible halt)"
            else:
                self.integrity.pop(f"stale:{s}", None)
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
            self.publish_status(now)
            return
        self.ensure_daily(today)
        # Stage 1 scan (premarket and every 10 min in session)
        if time.time() - self.last_scan > 600 and self.universe:
            self.stage1(now, open_, close_)
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
        self.tm.manage(self.prices, self.last_closed_bar, now, flatten)
        self.manage_shadow(now, flatten)
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
        self.db.event("info", "startup", f"engine {VERSION} starting in {self.s.mode.upper()} mode; reconciling first")
        self.reconcile()
        self.last_recon = time.monotonic()
        if self.integrity:
            self.db.event("warning", "startup", "integrity issues at startup; entries blocked until resolved",
                          self.integrity)
        # intraday positions left over from a previous session (e.g. crash before flatten)
        today = self.clock().astimezone(ET).date().isoformat()
        for t in self.tm.active_trades():
            if t["session"] and t["session"] < today:
                self.db.event("warning", "stale_position", f"{t['symbol']} from {t['session']} will be flattened")
                self.tm.request_exit(t, "stale_intraday_position")
        if not self.universe or self.db.kv_get("universe_date") != today:
            try:
                self.universe = build_discovery_universe(self.broker, self.hist)
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
