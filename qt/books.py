"""Per-strategy simulated $100 portfolios ("books"): the forward test.

Every strategy (swing and day, whatever its research status) gets its own simulated $100 account. All books
see the SAME live prices at the SAME moments, use the SAME risk limits, sizing rules, settlement model and cost
model, so their results are directly comparable. Books never send orders; the real paper account trades only
strategies that research validated, the owner approved as experiments, or that earned promotion here.

Fills: entries at the live price plus half the estimated spread and slippage (CostModel.buy_price); exits at
the live price minus the same (a stop that the price gapped through fills at the worse live price, not at the
stop). Regulatory fees as in research. Cash account: sale proceeds settle T+1.

Promotion / demotion rules are fixed here in code BEFORE any forward data exists (2026-11-27) and are never
tuned on forward results. They use forward trades only, with a Holm correction across all books.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import uuid

import numpy as np

from .config import RiskLimits
from .costs import CostModel
from .guardian import AccountState, CapitalGuardian, Proposal
from .ledger import Ledger, LedgerViolation
from .markettime import ET

log = logging.getLogger("books")
BOOK_CAPITAL = 100.0

SCHEMA = """
CREATE TABLE IF NOT EXISTS book_trades (
  trade_id TEXT PRIMARY KEY, strategy TEXT, engine TEXT, symbol TEXT, status TEXT, entry_time TEXT, entry_price REAL,
  qty REAL, notional REAL, stop REAL, initial_stop REAL, target REAL, atr REAL, trail_atr REAL, trail_activate_r REAL,
  breakeven_r REAL, max_bars INTEGER, bar_minutes INTEGER, flatten_eod INTEGER, exit_time TEXT, exit_price REAL,
  pnl REAL, r REAL, fees REAL, exit_reason TEXT, regime TEXT, score REAL, cost_bps REAL);
CREATE INDEX IF NOT EXISTS ix_book_trades ON book_trades(strategy, status);
"""

# ---- pre-registered forward rules (do not tune on forward results) ----
PROMOTE = {"min_trades": 40, "max_holm_p": 0.05, "min_pf": 1.3, "min_mean_r": 0.10}
DEMOTE = {"min_trades": 30, "max_mean_r": 0.0, "max_drawdown_pct": 25.0}


def _bootstrap_p(r: np.ndarray, n_boot: int = 4000, seed: int = 7) -> float:
    """One-sided p-value that the mean R is <= 0 (bootstrap of the mean)."""
    if len(r) < 2:
        return 1.0
    rng = np.random.default_rng(seed)
    means = rng.choice(r, size=(n_boot, len(r)), replace=True).mean(axis=1)
    return float((means <= 0).mean())


def _holm(p: dict[str, float]) -> dict[str, float]:
    items = sorted(p.items(), key=lambda x: x[1])
    m, out, run = len(items), {}, 0.0
    for i, (k, v) in enumerate(items):
        run = max(run, min(1.0, (m - i) * v))
        out[k] = run
    return out


class Books:
    def __init__(self, db, limits: RiskLimits, account_type: str = "cash", costs: CostModel | None = None):
        self.db = db
        self.limits = RiskLimits.from_dict({**limits.to_dict(), "starting_capital": BOOK_CAPITAL})
        self.guardian = CapitalGuardian(self.limits)
        self.account_type = account_type
        self.costs = costs or CostModel()
        for stmt in SCHEMA.strip().split(";"):
            if stmt.strip():
                self.db.execute(stmt)

    # ------------------------------------------------------------ state
    def _key(self, strategy: str) -> str:
        return f"book:{strategy}"

    def state(self, strategy: str) -> dict:
        st = self.db.kv_get(self._key(strategy))
        if not st:
            st = {"ledger": Ledger(BOOK_CAPITAL, self.account_type).to_dict(), "marks": {}, "consec": 0,
                  "last_loss_at": None, "created": None}
        return st

    def _save(self, strategy: str, st: dict) -> None:
        self.db.kv_set(self._key(strategy), st)

    def open_trades(self, strategy: str | None = None) -> list[dict]:
        if strategy:
            return self.db.query("SELECT * FROM book_trades WHERE status='open' AND strategy=?", (strategy,))
        return self.db.query("SELECT * FROM book_trades WHERE status='open'")

    def symbols(self) -> set[str]:
        return {r["symbol"] for r in self.db.query("SELECT DISTINCT symbol FROM book_trades WHERE status='open'")}

    def equity(self, strategy: str, prices: dict) -> float:
        led = Ledger.from_dict(self.state(strategy)["ledger"])
        return led.equity({s: prices.get(s) or p.avg_price for s, p in led.positions.items()})

    def _marks(self, st: dict, eq: float, now: dt.datetime) -> dict:
        m = st["marks"]
        day = now.astimezone(ET).date()
        if m.get("day") != day.isoformat():
            m.update(day=day.isoformat(), day_start=eq, session_peak=eq)
            if day.weekday() == 0 or not m.get("week_start"):
                m["week_start"] = eq
        m["peak"] = max(m.get("peak", eq), eq)
        m["session_peak"] = max(m.get("session_peak", eq), eq)
        m["max_dd_pct"] = max(m.get("max_dd_pct", 0.0), (m["peak"] - eq) / m["peak"] * 100 if m["peak"] else 0.0)
        return m

    def account_state(self, strategy: str, now: dt.datetime, prices: dict) -> tuple[AccountState, Ledger, dict]:
        st = self.state(strategy)
        led = Ledger.from_dict(st["ledger"])
        led.settle(now.astimezone(ET).date())
        px = {s: prices.get(s) or p.avg_price for s, p in led.positions.items()}
        eq = led.equity(px)
        m = self._marks(st, eq, now)
        opens = self.open_trades(strategy)
        risk = sum(max(0.0, (t["entry_price"] - t["stop"]) * t["qty"]) for t in opens)
        last = st.get("last_loss_at")
        s = AccountState(now=now, equity=eq, day_start_equity=m["day_start"], week_start_equity=m["week_start"],
                         peak_equity=m["peak"], session_peak_equity=m["session_peak"], buying_power=led.buying_power(),
                         exposure=led.exposure(px), open_positions=len(opens), open_risk=risk,
                         open_symbols=tuple(t["symbol"] for t in opens), consecutive_losses=st.get("consec", 0),
                         last_loss_at=dt.datetime.fromisoformat(last) if last else None)
        return s, led, st

    # ------------------------------------------------------------ entries
    def consider(self, o, exit_plan, engine: str, now: dt.datetime, prices: dict, bar_minutes: int = 5,
                 asset_class: str = "us_equity") -> str | None:
        """Simulated entry for one signal. Returns the trade id, or None with the reason logged."""
        if not (o.signal and o.stop and o.price and o.stop < o.price):
            return None
        if any(t["symbol"] == o.symbol for t in self.open_trades(o.strategy)):
            return None
        s, led, st = self.account_state(o.strategy, now, prices)
        prop = Proposal(o.symbol, asset_class, o.strategy, o.price, o.stop, o.est_cost_bps, o.spread_bps,
                        o.data_age_s, o.atr or 0.0, getattr(o, "max_corr", 0.0) or 0.0, None)
        d = self.guardian.evaluate(prop, s)
        if not d.approved:
            return None
        spread = self.costs.spread_bps(asset_class, getattr(o, "adv", None), o.symbol)
        fill = self.costs.buy_price(o.price, asset_class, spread)
        notional = min(d.notional, led.buying_power() * 0.999)
        if notional < self.limits.min_order_notional:
            return None
        qty = notional / fill
        fee = self.costs.fee("buy", qty, notional, asset_class)
        tid = f"bk-{now.strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:6]}"
        try:
            led.apply_buy_fill(o.symbol, qty, fill, fee, now, o.strategy, tid, asset_class=asset_class)
        except LedgerViolation as e:
            log.info("book %s: %s", o.strategy, e)
            return None
        st["ledger"] = led.to_dict()
        st.setdefault("created", now.isoformat())
        self._save(o.strategy, st)
        self.db.insert("book_trades", {
            "trade_id": tid, "strategy": o.strategy, "engine": engine, "symbol": o.symbol, "status": "open",
            "entry_time": now.isoformat(), "entry_price": fill, "qty": qty, "notional": notional, "stop": o.stop,
            "initial_stop": o.stop, "target": o.target, "atr": o.atr, "trail_atr": exit_plan.trail_atr or 0.0,
            "trail_activate_r": exit_plan.trail_activate_r or 1.0, "breakeven_r": exit_plan.breakeven_r or 0.0,
            "max_bars": exit_plan.max_bars or 0, "bar_minutes": bar_minutes, "flatten_eod": int(bool(exit_plan.flatten_eod)),
            "fees": fee, "regime": o.regime, "score": o.score, "cost_bps": o.est_cost_bps})
        return tid

    # ------------------------------------------------------------ exits
    def _close(self, t: dict, ref_px: float, reason: str, now: dt.datetime, prices: dict, spread_bps: float = 5.0):
        s, led, st = self.account_state(t["strategy"], now, prices)
        ac = "crypto" if "/" in t["symbol"] else "us_equity"
        if ac == "crypto":
            spread_bps = max(self.costs.spread_bps("crypto", None, t["symbol"]), spread_bps or 0.0)
        px = self.costs.sell_price(ref_px, ac, spread_bps)
        fee = self.costs.fee("sell", t["qty"], t["qty"] * px, ac)
        pos = led.positions.get(t["symbol"])
        if pos is None:
            self.db.execute("UPDATE book_trades SET status='closed', exit_reason='missing_position', exit_time=? "
                            "WHERE trade_id=?", (now.isoformat(), t["trade_id"]))
            return
        pnl = led.apply_sell_fill(t["symbol"], min(t["qty"], pos.qty), px, fee, now)
        risk = max((t["entry_price"] - t["initial_stop"]) * t["qty"], 1e-9)
        st["ledger"] = led.to_dict()
        if pnl < 0:
            st["consec"] = st.get("consec", 0) + 1
            st["last_loss_at"] = now.isoformat()
        else:
            st["consec"] = 0
        self._marks(st, led.equity(prices), now)
        self._save(t["strategy"], st)
        self.db.execute("UPDATE book_trades SET status='closed', exit_time=?, exit_price=?, pnl=?, r=?, fees=fees+?, "
                        "exit_reason=? WHERE trade_id=?",
                        (now.isoformat(), px, pnl, pnl / risk, fee, reason, t["trade_id"]))

    def manage(self, prices: dict, bars: dict, now: dt.datetime, flatten: bool, spreads: dict | None = None,
               crypto_only: bool = False, stocks_only: bool = False) -> None:
        """Every engine cycle: stops, targets, trailing, breakeven, intraday/hourly exits. Stocks only while the
        market is open (crypto_only=True outside it); crypto 24/7."""
        spreads = spreads or {}
        for t in self.open_trades():
            if (crypto_only and "/" not in t["symbol"]) or (stocks_only and "/" in t["symbol"]):
                continue
            px = prices.get(t["symbol"])
            if not px:
                continue
            sp = min(spreads.get(t["symbol"], 5.0) or 5.0, 50.0)
            stop = t["stop"]
            R = max(t["entry_price"] - t["initial_stop"], 1e-9)
            if px <= stop:
                self._close(t, px, "stop", now, prices, sp)
                continue
            if t["target"] and px >= t["target"]:
                self._close(t, px, "target", now, prices, sp)
                continue
            if t["flatten_eod"] and flatten:
                self._close(t, px, "eod_flatten", now, prices, sp)
                continue
            if t["max_bars"] and (t["bar_minutes"] or 5) < 1440:
                held = (now - dt.datetime.fromisoformat(t["entry_time"])).total_seconds() / 60
                if held >= t["max_bars"] * (t["bar_minutes"] or 5):
                    self._close(t, px, "time_exit", now, prices, sp)
                    continue
            new = stop
            b = bars.get(t["symbol"]) or {}
            atr = b.get("atr") if (t["bar_minutes"] or 5) < 1440 else None  # swing trails: daily ATR in daily_update
            if t["breakeven_r"] and px - t["entry_price"] >= t["breakeven_r"] * R:
                new = max(new, t["entry_price"])
            if t["trail_atr"] and atr and px - t["entry_price"] >= t["trail_activate_r"] * R:
                new = max(new, px - t["trail_atr"] * atr)
            if new > stop + 1e-9:
                self.db.execute("UPDATE book_trades SET stop=? WHERE trade_id=?", (new, t["trade_id"]))

    def signal_exit(self, strategy: str, symbol: str, now: dt.datetime, prices: dict,
                    spread_bps: float | None = None) -> None:
        """The strategy's own exit signal fired on a confirmed bar (hourly crypto)."""
        for t in self.open_trades(strategy):
            if t["symbol"] == symbol and prices.get(symbol):
                self._close(t, prices[symbol], "thesis_invalidated", now, prices, min(spread_bps or 5.0, 50.0))

    def daily_update(self, strategy: str, symbol: str, close: float, atr: float, exit_sig: bool, held_days: int,
                     now: dt.datetime, prices: dict) -> None:
        """Once per session for swing books, on the confirmed daily bar (same rules as the live swing path)."""
        for t in self.open_trades(strategy):
            if t["symbol"] != symbol or (t["bar_minutes"] or 5) < 1440:
                continue
            px = prices.get(symbol) or close
            if exit_sig:
                self._close(t, px, "thesis_invalidated", now, prices)
            elif t["max_bars"] and held_days >= t["max_bars"]:
                self._close(t, px, "time_exit", now, prices)
            else:
                R = max(t["entry_price"] - t["initial_stop"], 1e-9)
                if t["trail_atr"] and atr and close - t["entry_price"] >= t["trail_activate_r"] * R:
                    new = close - t["trail_atr"] * atr
                    if new > t["stop"]:
                        self.db.execute("UPDATE book_trades SET stop=? WHERE trade_id=?", (new, t["trade_id"]))

    # ------------------------------------------------------------ reporting / selection
    def stats(self, prices: dict | None = None) -> list[dict]:
        prices = prices or {}
        rows = self.db.query("SELECT strategy, engine FROM book_trades GROUP BY strategy")
        names = {r["strategy"]: r["engine"] for r in rows}
        for k in [r["k"] for r in self.db.query("SELECT k FROM kv WHERE k LIKE 'book:%'")]:
            names.setdefault(k[5:], None)
        out = []
        for name, engine in sorted(names.items()):
            st = self.state(name)
            led = Ledger.from_dict(st["ledger"])
            eq = led.equity({s: prices.get(s) or p.avg_price for s, p in led.positions.items()})
            closed = self.db.query("SELECT r, pnl, exit_time FROM book_trades WHERE strategy=? AND status='closed' "
                                   "AND exit_reason != 'missing_position' ORDER BY exit_time", (name,))
            r = np.array([c["r"] for c in closed if c["r"] is not None], dtype=float)
            wins, losses = r[r > 0].sum(), -r[r < 0].sum()
            out.append({
                "strategy": name, "engine": engine, "equity": round(eq, 4), "return_pct": round((eq / BOOK_CAPITAL - 1) * 100, 3),
                "realized": round(led.realized_pnl, 4), "fees": round(led.fees_paid, 4),
                "closed_trades": int(len(r)), "open_trades": len(self.open_trades(name)),
                "win_rate": round(float((r > 0).mean()) * 100, 1) if len(r) else None,
                "mean_r": round(float(r.mean()), 3) if len(r) else None,
                "profit_factor": round(float(wins / losses), 2) if losses > 0 else (None if not len(r) else 99.0),  # 99 = no losers
                "max_dd_pct": round(st["marks"].get("max_dd_pct", 0.0), 2), "p_value": round(_bootstrap_p(r), 4) if len(r) >= 2 else None,
                "since": st.get("created")})
        return out

    def evaluate(self, now: dt.datetime) -> dict:
        """Apply the pre-registered promotion / demotion rules. Result stored in kv 'forward_status'."""
        stats = self.stats()
        eligible = {s["strategy"]: s["p_value"] for s in stats
                    if s["closed_trades"] >= PROMOTE["min_trades"] and s["p_value"] is not None}
        holm = _holm(eligible) if eligible else {}
        prev = self.db.kv_get("forward_status", {}) or {}
        out = {}
        for s in stats:
            name, n = s["strategy"], s["closed_trades"]
            hp = holm.get(name)
            status, why = prev.get(name, {}).get("status", "testing"), None
            if (n >= PROMOTE["min_trades"] and hp is not None and hp <= PROMOTE["max_holm_p"]
                    and (s["profit_factor"] or 0) >= PROMOTE["min_pf"] and (s["mean_r"] or 0) >= PROMOTE["min_mean_r"]
                    and s["return_pct"] > 0):
                status, why = "promoted", f"{n} forward trades, mean R {s['mean_r']}, PF {s['profit_factor']}, Holm p {hp:.3f}"
            elif n >= DEMOTE["min_trades"] and ((s["mean_r"] or 0) <= DEMOTE["max_mean_r"]
                                                or s["max_dd_pct"] >= DEMOTE["max_drawdown_pct"]):
                status, why = "demoted", f"{n} forward trades, mean R {s['mean_r']}, max drawdown {s['max_dd_pct']}%"
            elif status not in ("promoted", "demoted"):
                status, why = "testing", f"{n} forward trades so far"
            else:
                why = prev.get(name, {}).get("why")
            out[name] = {"status": status, "why": why, "holm_p": hp, "mean_r": s["mean_r"], "trades": n,
                         "at": now.isoformat()}
            if prev.get(name, {}).get("status") != status and status in ("promoted", "demoted"):
                self.db.event("important", "forward_" + status, f"{name} {status} by forward test: {why}")
        self.db.kv_set("forward_status", out)
        return out
