"""$100 options book. Owner's choices 2026-11-27: first simulated only, then (same day) "yes" to REAL PAPER option
orders. With OPTIONS_PAPER_ORDERS=true every entry/exit below is sent to the Alpaca paper account as a limit
order (buy at the ask, sell at the bid) and the book is updated only from broker fills; otherwise fills are
simulated at the ask/bid. Its own $100 ledger: options never use the stock account's cash or risk limits,
and the stock account's 0.25% per-trade limit is unchanged. Alpaca has no stop orders for options, so the
stop is software-managed (checked every engine cycle while the market is open).

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
  exit_premium REAL, pnl REAL, r REAL, fees REAL, exit_reason TEXT, last_bid REAL, order_cid TEXT, order_at TEXT,
  exit_tries INTEGER DEFAULT 0);
CREATE INDEX IF NOT EXISTS ix_options_trades ON options_trades(status);
"""
_OCC = re.compile(r"^([A-Z.]+)(\d{6})([CP])(\d{8})$")


def parse_occ(sym: str) -> tuple[str, dt.date, str, float] | None:
    m = _OCC.match(sym)
    if not m:
        return None
    return m.group(1), dt.datetime.strptime(m.group(2), "%y%m%d").date(), m.group(3), int(m.group(4)) / 1000.0


ORDER_TIMEOUT_S = 120


class OptionsBook:
    def __init__(self, db, broker=None, live: bool = False):
        self.db, self.broker, self.live = db, broker, live
        for stmt in SCHEMA.strip().split(";"):
            if stmt.strip():
                self.db.execute(stmt)
        for col in ("order_cid TEXT", "order_at TEXT", "exit_tries INTEGER DEFAULT 0"):  # tables made before
            try:
                self.db.execute(f"ALTER TABLE options_trades ADD COLUMN {col}")
            except Exception:  # noqa: BLE001
                pass

    def contracts_held(self) -> set[str]:
        return {r["contract"] for r in self.db.query(
            "SELECT contract FROM options_trades WHERE status IN ('pending_entry','open','exiting')")}

    # ---------------------------------------------------------------- state
    def state(self) -> dict:
        st = self.db.kv_get("optbook") or {}
        st.setdefault("ledger", Ledger(CAPITAL, "cash").to_dict())
        st.setdefault("marks", {})
        return st

    def _save(self, st: dict) -> None:
        self.db.kv_set("optbook", st)

    def open_trades(self) -> list[dict]:
        return self.db.query("SELECT * FROM options_trades WHERE status IN ('open','pending_entry','exiting') "
                             "ORDER BY entry_time")

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
            cid = f"qt-{tid}-entry"
            try:
                if self.live:
                    led.reserve(cid, c["ask"] * 100 + fee)
                    self._submit(cid, c["symbol"], "buy", c["ask"])
                else:
                    led.apply_buy_fill(c["symbol"], 100.0, c["ask"], fee, now, o.strategy, tid, asset_class="us_option")
            except (LedgerViolation, Exception) as e:  # noqa: BLE001
                led.release(cid)
                out.append({"symbol": o.symbol, "result": f"not bought: {str(e)[:80]}"})
                continue
            stop = c["ask"] * RULES["stop_frac"]
            self.db.insert("options_trades", {
                "trade_id": tid, "signal_strategy": o.strategy, "underlying": o.symbol, "contract": c["symbol"],
                "status": "pending_entry" if self.live else "open", "order_cid": cid if self.live else None,
                "order_at": now.isoformat(),
                "entry_time": now.isoformat(), "entry_premium": c["ask"], "contracts": 1,
                "cost": c["ask"] * 100 + fee, "stop_premium": stop, "initial_stop_premium": stop,
                "underlying_entry": o.price, "underlying_stop": o.stop, "expiry": c["expiry"].isoformat(),
                "strike": c["strike"], "delta": c.get("delta"), "iv": c.get("iv"), "fees": fee, "last_bid": c["bid"]})
            opens.append({"underlying": o.symbol})
            held.add(o.symbol)
            out.append({"symbol": o.symbol, "result": f"{'paper order: buy' if self.live else 'bought'} 1 {c['symbol']} "
                                                      f"at {c['ask']:.2f}"})
        st["ledger"] = led.to_dict()
        st.setdefault("created", now.isoformat())
        self._mark(st, self._equity(led))
        self._save(st)
        self.db.kv_set("optbook_last", {"at": now.isoformat(), "log": out[-20:]})
        return out

    # ---------------------------------------------------------------- exits
    def _submit(self, cid: str, contract: str, side: str, price: float) -> dict:
        from .broker.base import OrderSpec
        spec = OrderSpec(cid, contract, side, "limit" if price > 0 else "market", qty=1.0,
                         limit_price=round(price, 2) if price > 0 else None, time_in_force="day", asset_class="us_option")
        return self.broker.submit_order(spec)

    def _close(self, t: dict, bid: float, reason: str, now: dt.datetime) -> None:
        if self.live:
            if t["status"] != "open":
                return
            n = (t.get("exit_tries") or 0) + 1
            cid = f"qt-{t['trade_id']}-exit{n}"
            try:
                self._submit(cid, t["contract"], "sell", bid)
            except Exception as e:  # noqa: BLE001
                log.warning("option exit order failed for %s: %s", t["contract"], e)
                return
            self.db.execute("UPDATE options_trades SET status='exiting', exit_reason=?, order_cid=?, order_at=?, "
                            "exit_tries=? WHERE trade_id=?", (reason, cid, now.isoformat(), n, t["trade_id"]))
            return
        self._book_sell(t, bid, reason, now)

    def poll(self, now: dt.datetime) -> None:
        """Live mode: apply broker fills to the book; cancel and retry orders that do not fill."""
        if not self.live:
            return
        for t in self.db.query("SELECT * FROM options_trades WHERE status IN ('pending_entry','exiting')"):
            try:
                o = self.broker.get_order_by_client_id(t["order_cid"])
            except Exception as e:  # noqa: BLE001
                log.warning("option order status failed: %s", e)
                continue
            st = self.state()
            led = Ledger.from_dict(st["ledger"])
            if o and o["status"] == "filled":
                px = float(o["filled_avg_price"])
                if t["status"] == "pending_entry":
                    fee = RULES["fee_per_contract"]
                    led.apply_buy_fill(t["contract"], 100.0, px, fee, now, t["signal_strategy"], t["trade_id"],
                                       asset_class="us_option", order_key=t["order_cid"])
                    led.release(t["order_cid"])
                    st["ledger"] = led.to_dict()
                    self._save(st)
                    stop = px * RULES["stop_frac"]
                    self.db.execute("UPDATE options_trades SET status='open', entry_premium=?, cost=?, stop_premium=?, "
                                    "initial_stop_premium=?, entry_time=? WHERE trade_id=?",
                                    (px, px * 100 + fee, stop, stop, now.isoformat(), t["trade_id"]))
                else:
                    self._book_sell(t, px, t["exit_reason"], now)
                continue
            age = (now - dt.datetime.fromisoformat(t["order_at"])).total_seconds()
            dead = o is None or o["status"] in ("canceled", "expired", "rejected", "done_for_day")
            if not dead and age > ORDER_TIMEOUT_S:
                try:
                    self.broker.cancel_order(o["id"])
                except Exception:  # noqa: BLE001
                    pass
                continue  # next poll sees it canceled
            if dead:
                if t["status"] == "pending_entry":
                    led.release(t["order_cid"])
                    st["ledger"] = led.to_dict()
                    self._save(st)
                    self.db.execute("UPDATE options_trades SET status='entry_failed', exit_reason='entry not filled' "
                                    "WHERE trade_id=?", (t["trade_id"],))
                else:  # exit not filled: back to open, the next manage() re-sends at the fresh bid
                    self.db.execute("UPDATE options_trades SET status='open' WHERE trade_id=?", (t["trade_id"],))

    def _book_sell(self, t: dict, bid: float, reason: str, now: dt.datetime) -> None:
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
            if t["status"] != "open":
                continue
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
