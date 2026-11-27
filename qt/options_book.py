"""Simulated $100 options book (owner's choice 2026-11-27: options are forward-tested here only, never ordered).

What it does: when a swing strategy that the bot trades for real (validated, owner-approved experimental or
forward-promoted) signals a stock, this book buys ONE listed call on that stock instead of the shares, if a
contract fits the rules below. It is a forward test of "the same signal, expressed with a call", run on live
Alpaca option quotes (indicative feed on the free data plan, which is an approximation of the real market).

Why a separate book with its own limits: an option contract is 100 shares and cannot be bought fractionally,
so one contract usually costs $20-$300 and can lose its whole premium. That cannot fit the 0.5%-of-equity risk
limit of the real account, so this book has its own (looser) limits and never sends an order.

Rules, fixed in code before any forward result (2026-11-27). Never tune them on forward results.
- Contract: call, 21-60 days to expiry, delta 0.30-0.70, bid > 0, bid/ask spread at most 15% of mid.
  Among those that fit the budget, the delta closest to 0.50 (then the nearest expiry).
- Budget: premium x 100 + fees at most 50% of book equity and at most the settled cash. At most 2 open.
- Fills: buy at the ASK, sell at the BID (the realistic worst side of the quote). $0.05 per contract per
  side for regulatory/clearing fees (Alpaca charges no options commission).
- Mandatory stop: exit when the bid falls to 50% of the entry premium. Once the bid reaches 2x the entry
  premium the stop rises to the entry premium (no profit cap above that).
- Other exits: the stock closes below the signal's stop (thesis broken); 7 days or fewer to expiry (no
  expiry/assignment risk); 20 trading days held.
- Cash account: sale proceeds settle T+1 (options settle T+1).
"""
from __future__ import annotations

import datetime as dt
import logging
import re
import uuid

import numpy as np

from .books import _bootstrap_p
from .ledger import Ledger, LedgerViolation
from .markettime import ET

log = logging.getLogger("options_book")
NAME = "OPT_calls_on_swing"
CAPITAL = 100.0
RULES = {"dte_min": 21, "dte_max": 60, "delta_min": 0.30, "delta_max": 0.70, "target_delta": 0.50,
         "max_spread_pct": 15.0, "max_premium_frac": 0.50, "max_open": 2, "stop_frac": 0.50,
         "breakeven_after_mult": 2.0, "exit_dte": 7, "max_hold_days": 20, "fee_per_contract": 0.05}

SCHEMA = """
CREATE TABLE IF NOT EXISTS options_trades (
  trade_id TEXT PRIMARY KEY, signal_strategy TEXT, underlying TEXT, contract TEXT, status TEXT, entry_time TEXT,
  entry_premium REAL, contracts INTEGER, cost REAL, stop_premium REAL, initial_stop_premium REAL,
  underlying_entry REAL, underlying_stop REAL, expiry TEXT, strike REAL, delta REAL, iv REAL, exit_time TEXT,
  exit_premium REAL, pnl REAL, r REAL, fees REAL, exit_reason TEXT, last_bid REAL);
CREATE INDEX IF NOT EXISTS ix_options_trades ON options_trades(status);
"""
_OCC = re.compile(r"^([A-Z.]+)(\d{6})([CP])(\d{8})$")


def parse_occ(sym: str) -> tuple[str, dt.date, str, float] | None:
    m = _OCC.match(sym)
    if not m:
        return None
    return m.group(1), dt.datetime.strptime(m.group(2), "%y%m%d").date(), m.group(3), int(m.group(4)) / 1000.0


class OptionsBook:
    def __init__(self, db):
        self.db = db
        for stmt in SCHEMA.strip().split(";"):
            if stmt.strip():
                self.db.execute(stmt)

    # ---------------------------------------------------------------- state
    def state(self) -> dict:
        st = self.db.kv_get("optbook") or {}
        st.setdefault("ledger", Ledger(CAPITAL, "cash").to_dict())
        st.setdefault("marks", {})
        return st

    def _save(self, st: dict) -> None:
        self.db.kv_set("optbook", st)

    def open_trades(self) -> list[dict]:
        return self.db.query("SELECT * FROM options_trades WHERE status='open' ORDER BY entry_time")

    def _equity(self, led: Ledger) -> float:
        marks = {t["contract"]: t["last_bid"] for t in self.open_trades() if t["last_bid"] is not None}
        return led.equity({s: marks.get(s, p.avg_price) for s, p in led.positions.items()})

    def _mark(self, st: dict, eq: float) -> None:
        m = st["marks"]
        m["peak"] = max(m.get("peak", CAPITAL), eq)
        m["max_dd_pct"] = max(m.get("max_dd_pct", 0.0), (m["peak"] - eq) / m["peak"] * 100 if m["peak"] else 0.0)

    # ---------------------------------------------------------------- entries
    def choose(self, chain: list[dict], today: dt.date, budget: float) -> dict | None:
        best = None
        for c in chain:
            p = parse_occ(c["symbol"])
            if p is None or p[2] != "C":
                continue
            dte = (p[1] - today).days
            bid, ask, delta = c.get("bid") or 0.0, c.get("ask") or 0.0, c.get("delta")
            if not (RULES["dte_min"] <= dte <= RULES["dte_max"]) or delta is None:
                continue
            if not (RULES["delta_min"] <= delta <= RULES["delta_max"]) or bid <= 0 or ask <= bid:
                continue
            if (ask - bid) / ((ask + bid) / 2) * 100 > RULES["max_spread_pct"]:
                continue
            if ask * 100 + RULES["fee_per_contract"] > budget:
                continue
            key = (abs(delta - RULES["target_delta"]), dte)
            if best is None or key < best[0]:
                best = (key, dict(c, expiry=p[1], strike=p[3], dte=dte))
        return best[1] if best else None

    def consider(self, signals: list, broker, now: dt.datetime) -> list[dict]:
        """signals: swing Opportunity objects (signal=True, with a stop). Returns a log of what happened."""
        out = []
        today = now.astimezone(ET).date()
        st = self.state()
        led = Ledger.from_dict(st["ledger"])
        led.settle(today)
        opens = self.open_trades()
        held = {t["underlying"] for t in opens}
        for o in signals:
            if len(opens) >= RULES["max_open"]:
                out.append({"symbol": o.symbol, "result": "book full"})
                break
            if o.symbol in held or "/" in o.symbol or not o.stop:
                continue
            eq = self._equity(led)
            budget = min(led.buying_power(), eq * RULES["max_premium_frac"])
            try:
                chain = broker.option_chain(o.symbol, today + dt.timedelta(days=RULES["dte_min"]),
                                            today + dt.timedelta(days=RULES["dte_max"]))
            except Exception as e:  # noqa: BLE001
                out.append({"symbol": o.symbol, "result": f"no option data ({str(e)[:60]})"})
                continue
            c = self.choose(chain, today, budget)
            if c is None:
                out.append({"symbol": o.symbol, "result": f"no call fits (budget ${budget:.0f}, {len(chain)} contracts)"})
                continue
            fee = RULES["fee_per_contract"]
            tid = f"op-{now.strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:6]}"
            try:
                led.apply_buy_fill(c["symbol"], 100.0, c["ask"], fee, now, o.strategy, tid, asset_class="us_option")
            except LedgerViolation as e:
                out.append({"symbol": o.symbol, "result": str(e)})
                continue
            stop = c["ask"] * RULES["stop_frac"]
            self.db.insert("options_trades", {
                "trade_id": tid, "signal_strategy": o.strategy, "underlying": o.symbol, "contract": c["symbol"],
                "status": "open", "entry_time": now.isoformat(), "entry_premium": c["ask"], "contracts": 1,
                "cost": c["ask"] * 100 + fee, "stop_premium": stop, "initial_stop_premium": stop,
                "underlying_entry": o.price, "underlying_stop": o.stop, "expiry": c["expiry"].isoformat(),
                "strike": c["strike"], "delta": c.get("delta"), "iv": c.get("iv"), "fees": fee, "last_bid": c["bid"]})
            opens.append({"underlying": o.symbol})
            held.add(o.symbol)
            out.append({"symbol": o.symbol, "result": f"bought 1 {c['symbol']} at {c['ask']:.2f}"})
        st["ledger"] = led.to_dict()
        st.setdefault("created", now.isoformat())
        self._mark(st, self._equity(led))
        self._save(st)
        self.db.kv_set("optbook_last", {"at": now.isoformat(), "log": out[-20:]})
        return out

    # ---------------------------------------------------------------- exits
    def _close(self, t: dict, bid: float, reason: str, now: dt.datetime) -> None:
        st = self.state()
        led = Ledger.from_dict(st["ledger"])
        led.settle(now.astimezone(ET).date())
        fee = RULES["fee_per_contract"] * t["contracts"]
        if t["contract"] not in led.positions:
            self.db.execute("UPDATE options_trades SET status='closed', exit_reason='missing_position', exit_time=? "
                            "WHERE trade_id=?", (now.isoformat(), t["trade_id"]))
            return
        pnl = led.apply_sell_fill(t["contract"], 100.0 * t["contracts"], max(bid, 0.0), fee, now)
        risk = max((t["entry_premium"] - t["initial_stop_premium"]) * 100 * t["contracts"], 1e-9)
        st["ledger"] = led.to_dict()
        self._mark(st, self._equity(led))
        self._save(st)
        self.db.execute("UPDATE options_trades SET status='closed', exit_time=?, exit_premium=?, pnl=?, r=?, "
                        "fees=fees+?, exit_reason=?, last_bid=? WHERE trade_id=?",
                        (now.isoformat(), bid, pnl, pnl / risk, fee, reason, bid, t["trade_id"]))

    def manage(self, broker, underlying_px: dict, now: dt.datetime, underlying_close: dict | None = None) -> None:
        """During regular hours: stops / breakeven / thesis / expiry / time exits on live option quotes."""
        opens = self.open_trades()
        if not opens:
            return
        try:
            q = broker.option_quotes([t["contract"] for t in opens])
        except Exception as e:  # noqa: BLE001
            log.warning("option quotes failed: %s", e)
            return
        today = now.astimezone(ET).date()
        for t in opens:
            v = q.get(t["contract"])
            if not v or not v.get("bid") and not v.get("ask"):
                continue
            bid = v.get("bid") or 0.0
            self.db.execute("UPDATE options_trades SET last_bid=? WHERE trade_id=?", (bid, t["trade_id"]))
            stop = t["stop_premium"]
            if bid >= t["entry_premium"] * RULES["breakeven_after_mult"] and stop < t["entry_premium"]:
                stop = t["entry_premium"]
                self.db.execute("UPDATE options_trades SET stop_premium=? WHERE trade_id=?", (stop, t["trade_id"]))
            dte = (dt.date.fromisoformat(t["expiry"]) - today).days
            held = int(np.busday_count(t["entry_time"][:10], today.isoformat()))
            ucl = (underlying_close or {}).get(t["underlying"])
            if bid <= stop:
                self._close(t, bid, "stop", now)
            elif ucl is not None and ucl < t["underlying_stop"]:
                self._close(t, bid, "thesis_invalidated", now)
            elif dte <= RULES["exit_dte"]:
                self._close(t, bid, "expiry_exit", now)
            elif held >= RULES["max_hold_days"]:
                self._close(t, bid, "time_exit", now)

    # ---------------------------------------------------------------- reporting
    def stats(self) -> dict:
        st = self.state()
        led = Ledger.from_dict(st["ledger"])
        eq = self._equity(led)
        closed = self.db.query("SELECT r FROM options_trades WHERE status='closed' AND exit_reason!='missing_position'")
        r = np.array([c["r"] for c in closed if c["r"] is not None], dtype=float)
        wins, losses = r[r > 0].sum(), -r[r < 0].sum()
        return {"strategy": NAME, "engine": "options", "equity": round(eq, 4), "return_pct": round((eq / CAPITAL - 1) * 100, 3),
                "realized": round(led.realized_pnl, 4), "fees": round(led.fees_paid, 4), "closed_trades": int(len(r)),
                "open_trades": len(self.open_trades()),
                "win_rate": round(float((r > 0).mean()) * 100, 1) if len(r) else None,
                "mean_r": round(float(r.mean()), 3) if len(r) else None,
                "profit_factor": round(float(wins / losses), 2) if losses > 0 else (None if not len(r) else 99.0),
                "max_dd_pct": round(st["marks"].get("max_dd_pct", 0.0), 2),
                "p_value": round(_bootstrap_p(r), 4) if len(r) >= 2 else None, "since": st.get("created")}
