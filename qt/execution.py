"""Order management + position management.

Safety properties (tested in tests/test_execution.py):
* Write-ahead: every order is recorded locally BEFORE it is sent.
* Idempotent: client_order_id is deterministic per trade/action; Alpaca rejects
  duplicates, so a retry after an uncertain send can never double-submit.
* Nothing is treated as filled until the broker reports the fill. Fills are
  applied as deltas of filled_qty, so repeated updates are harmless.
* Uncertain submissions are resolved by looking up the client_order_id. If an
  order's state can't be established, system integrity is flagged and new
  entries stop until reconciliation succeeds.
* One trade per symbol across all strategies (no conflicting orders).
* Exits: cancel the protective stop, confirm the cancel, then sell the
  remaining quantity. If the stop filled meanwhile, the trade is simply closed.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import time
from dataclasses import dataclass, field

from .broker.base import TERMINAL, Broker, BrokerError, OrderSpec, round_price, round_qty
from .db import DB
from .ledger import Ledger, LedgerViolation
from .markettime import now_utc

log = logging.getLogger("execution")


class OrderManager:
    def __init__(self, broker: Broker, db: DB, on_fill, on_terminal, clock=now_utc, sleep=time.sleep):
        self.broker, self.db = broker, db
        self.on_fill, self.on_terminal = on_fill, on_terminal
        self.clock, self.sleep = clock, sleep
        self.integrity_issues: dict[str, str] = {}

    def submit(self, spec: OrderSpec, trade_id: str, kind: str, expected_price: float | None) -> dict | None:
        now = self.clock()
        if self.db.one("SELECT 1 FROM orders WHERE client_order_id=?", (spec.client_order_id,)):
            log.warning("duplicate submit suppressed for %s", spec.client_order_id)
            return self.sync(spec.client_order_id)
        self.db.insert("orders", {
            "client_order_id": spec.client_order_id, "trade_id": trade_id, "symbol": spec.symbol, "side": spec.side,
            "kind": kind, "type": spec.type, "qty": spec.qty, "notional": spec.notional,
            "limit_price": spec.limit_price, "stop_price": spec.stop_price, "status": "pending_submit",
            "submitted_at": now.isoformat(), "updated_at": now.isoformat(), "expected_price": expected_price})
        t0 = time.monotonic()
        for attempt in range(3):
            try:
                o = self.broker.submit_order(spec)
                self.db.execute("UPDATE orders SET ack_latency_ms=? WHERE client_order_id=?",
                                ((time.monotonic() - t0) * 1000, spec.client_order_id))
                self._apply(spec.client_order_id, o)
                return o
            except BrokerError as e:
                if not e.uncertain and "client_order_id must be unique" not in str(e):
                    self._set(spec.client_order_id, status="rejected", error=str(e))
                    self.db.event("warning", "order_rejected", f"{spec.symbol} {spec.side} {kind} rejected: {e}")
                    self.on_terminal(self._row(spec.client_order_id), {"status": "rejected"})
                    return None
                # uncertain or duplicate: find out whether the broker has it
                found = self._lookup(spec.client_order_id)
                if found is not None:
                    self._apply(spec.client_order_id, found)
                    return found
                self.sleep(min(2 ** attempt, 4))
        self._set(spec.client_order_id, status="unknown", error="could not confirm submission")
        self.integrity_issues[spec.client_order_id] = "order submission state unknown"
        self.db.event("critical", "order_unknown", f"{spec.client_order_id}: submission could not be confirmed")
        return None

    def _lookup(self, cid: str) -> dict | None:
        for i in range(3):
            try:
                return self.broker.get_order_by_client_id(cid)
            except BrokerError:
                self.sleep(min(2 ** i, 4))
        return None

    def _row(self, cid: str) -> dict:
        return self.db.one("SELECT * FROM orders WHERE client_order_id=?", (cid,))

    def _set(self, cid: str, **kw) -> None:
        kw["updated_at"] = self.clock().isoformat()
        self.db.execute(f"UPDATE orders SET {','.join(k + '=?' for k in kw)} WHERE client_order_id=?",
                        (*kw.values(), cid))

    def _apply(self, cid: str, o: dict) -> None:
        row = self._row(cid)
        if row is None:
            return
        prev_qty = row["filled_qty"] or 0.0
        prev_avg = row["filled_avg_price"] or 0.0
        new_qty = o.get("filled_qty") or 0.0
        new_avg = o.get("filled_avg_price") or 0.0
        delta = new_qty - prev_qty
        upd = {"broker_order_id": o.get("id"), "status": o.get("status"), "filled_qty": new_qty,
               "filled_avg_price": new_avg or None}
        if delta > 1e-12 and new_avg:
            px = (new_avg * new_qty - prev_avg * prev_qty) / delta
            if row["expected_price"]:
                sign = 1 if row["side"] == "buy" else -1
                upd["slippage_bps"] = sign * (new_avg / row["expected_price"] - 1) * 1e4
            if row["submitted_at"]:
                upd["fill_latency_ms"] = (self.clock() - dt.datetime.fromisoformat(row["submitted_at"])).total_seconds() * 1000
            self._set(cid, **upd)
            self.on_fill(self._row(cid), delta, px, o)
        else:
            self._set(cid, **upd)
        if o.get("status") in TERMINAL and row["status"] not in TERMINAL:
            self.on_terminal(self._row(cid), o)
        self.integrity_issues.pop(cid, None)

    def sync(self, cid: str) -> dict | None:
        o = self._lookup(cid)
        if o is not None:
            self._apply(cid, o)
        return o

    def poll_open(self) -> None:
        for r in self.db.query("SELECT client_order_id FROM orders WHERE status NOT IN "
                               "('filled','canceled','expired','rejected','replaced','done_for_day')"):
            self.sync(r["client_order_id"])

    def on_trade_update(self, order: dict) -> None:
        """Websocket trade_updates handler (normalised order dict)."""
        if order.get("client_order_id") and self._row(order["client_order_id"]):
            self._apply(order["client_order_id"], order)

    def cancel(self, cid: str) -> bool:
        row = self._row(cid)
        if row is None or row["status"] in TERMINAL:
            return True
        try:
            self.broker.cancel_order(row["broker_order_id"])
        except BrokerError as e:
            log.info("cancel %s: %s", cid, e)
        self.sync(cid)
        return self._row(cid)["status"] in TERMINAL


@dataclass
class TradePlan:
    trade_id: str
    symbol: str
    asset_class: str
    strategy: str
    strategy_version: str
    notional: float
    entry_ref: float          # ask at decision time
    stop: float
    target: float | None
    trail_atr: float
    trail_activate_r: float
    breakeven_r: float
    max_bars: int
    flatten_eod: bool
    atr: float
    score: float
    regime: str
    thesis: str
    exit_plan: str
    explanation: dict = field(default_factory=dict)
    est_cost_bps: float = 0.0
    planned_risk: float = 0.0
    max_entry_slippage_bps: float = 15.0
    timeframe: str = "5Min"


class TradeManager:
    ENTRY_TIMEOUT_S = 60
    STOP_GRACE_S = 20

    def __init__(self, broker: Broker, db: DB, ledger: Ledger, notifier=None, clock=now_utc, sleep=time.sleep):
        self.broker, self.db, self.ledger = broker, db, ledger
        self.notify = notifier or (lambda *a, **k: None)
        self.clock = clock
        self.om = OrderManager(broker, db, self._on_fill, self._on_terminal, clock, sleep)
        self.consecutive_losses = db.kv_get("consecutive_losses", 0)
        self.last_loss_at = db.kv_get("last_loss_at")

    # ---------------- persistence ----------------
    def save_ledger(self):
        self.db.kv_set("ledger", self.ledger.to_dict())

    def trade(self, trade_id: str) -> dict | None:
        return self.db.one("SELECT * FROM trades WHERE trade_id=?", (trade_id,))

    def active_trades(self) -> list[dict]:
        return self.db.query("SELECT * FROM trades WHERE status IN ('pending_entry','open','exiting')")

    def _upd(self, trade_id: str, **kw):
        self.db.execute(f"UPDATE trades SET {','.join(k + '=?' for k in kw)} WHERE trade_id=?",
                        (*[json.dumps(v, default=str) if isinstance(v, (dict, list)) else v for v in kw.values()],
                         trade_id))

    # ---------------- entry ----------------
    def open_trade(self, p: TradePlan) -> bool:
        if any(t["symbol"] == p.symbol for t in self.active_trades()):
            log.warning("refusing second trade in %s", p.symbol)
            return False
        cid = f"qt-{p.trade_id}-entry"
        try:
            self.ledger.reserve(cid, p.notional)
        except LedgerViolation as e:
            self.db.event("info", "entry_skipped", f"{p.symbol}: {e}")
            return False
        self.save_ledger()
        now = self.clock()
        self.db.insert("trades", {
            "trade_id": p.trade_id, "symbol": p.symbol, "asset_class": p.asset_class, "strategy": p.strategy,
            "strategy_version": p.strategy_version, "status": "pending_entry", "entry_time": None,
            "stop_price": p.stop, "initial_stop": p.stop, "trail_stop": None, "target": p.target, "qty": 0.0,
            "notional": p.notional, "fees": 0.0, "planned_risk": p.planned_risk, "regime": p.regime, "score": p.score,
            "entry_reason": p.thesis, "thesis": p.thesis, "exit_plan": p.exit_plan,
            "explanation": json.dumps({**p.explanation, "plan": {k: v for k, v in p.__dict__.items() if k != "explanation"},
                                       "decided_at": now.isoformat()}, default=str),
            "session": now.date().isoformat(), "est_cost_bps": p.est_cost_bps, "mfe": 0.0, "mae": 0.0})
        limit = p.entry_ref * (1 + p.max_entry_slippage_bps / 1e4)
        qty = round_qty(p.notional / limit) if p.asset_class != "crypto" else round_qty(p.notional / limit / 1.0025)
        spec = OrderSpec(cid, p.symbol, "buy", "limit", qty=qty, limit_price=round_price(limit),
                         time_in_force="gtc" if p.asset_class == "crypto" else "day", asset_class=p.asset_class)
        o = self.om.submit(spec, p.trade_id, "entry", p.entry_ref)
        if o is None and self.om._row(cid)["status"] == "rejected":
            self._upd(p.trade_id, status="entry_failed", exit_reason="entry order rejected")
            return False
        self.db.event("info", "entry_submitted", f"{p.strategy} BUY {p.symbol} ~${p.notional:.2f} stop {p.stop:.2f}")
        return True

    # ---------------- fills ----------------
    def _on_fill(self, row: dict, qty: float, price: float, o: dict):
        t = self.trade(row["trade_id"])
        if t is None:
            self.db.event("critical", "orphan_fill", f"fill for unknown trade {row['trade_id']}")
            return
        when = self.clock()
        ac = t["asset_class"]
        fee = qty * price * 0.0025 if ac == "crypto" else 0.0
        if row["side"] == "buy":
            if ac == "crypto":
                fee = 0.0  # Alpaca takes crypto buy fees in the asset received; qty already net
            self.ledger.apply_buy_fill(t["symbol"], qty, price, fee, when, t["strategy"], t["trade_id"], ac,
                                       order_key=row["client_order_id"])
            p = self.ledger.positions[t["symbol"]]
            self._upd(t["trade_id"], status="open" if t["status"] == "pending_entry" else t["status"], qty=p.qty,
                      entry_price=p.avg_price, entry_time=t["entry_time"] or when.isoformat(),
                      fees=(t["fees"] or 0) + fee)
            self.notify("trade_open", f"OPEN {t['symbol']} {qty:.6f} @ {price:.2f} ({t['strategy']})")
        else:
            sec_taf = 0.0 if ac == "crypto" else qty * price * 27.8e-6 + min(9.79, qty * 0.000195)
            fee = fee + sec_taf
            pnl = self.ledger.apply_sell_fill(t["symbol"], qty, price, fee, when)
            remaining = self.ledger.positions.get(t["symbol"])
            total_pnl = (t["pnl"] or 0) + pnl
            upd = {"pnl": total_pnl, "fees": (t["fees"] or 0) + fee, "exit_price": price}
            if remaining is None:
                upd.update(status="closed", exit_time=when.isoformat(),
                           exit_reason=t["exit_reason"] or ("stop" if row["kind"] == "stop" else row["kind"]),
                           r_multiple=total_pnl / t["planned_risk"] if t["planned_risk"] else None, qty=t["qty"])
                self._record_close(t, total_pnl, upd["exit_reason"])
            self._upd(t["trade_id"], **upd)
        self.save_ledger()

    def _record_close(self, t, pnl, reason):
        if pnl < 0:
            self.consecutive_losses += 1
            self.last_loss_at = self.clock().isoformat()
        else:
            self.consecutive_losses = 0
        self.db.kv_set("consecutive_losses", self.consecutive_losses)
        self.db.kv_set("last_loss_at", self.last_loss_at)
        kind = "stop_loss" if "stop" in (reason or "") else "trade_close"
        self.notify(kind, f"CLOSE {t['symbol']} {reason}: P&L {pnl:+.2f}")
        self.db.event("info", "trade_closed", f"{t['symbol']} {reason} pnl {pnl:+.4f}", {"trade_id": t["trade_id"]})

    def _on_terminal(self, row: dict, o: dict):
        t = self.trade(row["trade_id"])
        if t is None:
            return
        if row["kind"] == "entry":
            self.ledger.release(row["client_order_id"])
            self.save_ledger()
            pos = self.ledger.positions.get(t["symbol"])
            if pos is None or pos.trade_id != t["trade_id"]:
                self._upd(t["trade_id"], status="entry_failed", exit_reason=f"entry {o.get('status')}")
                return
            self._place_stop(self.trade(t["trade_id"]))

    # ---------------- protective stop ----------------
    def _stop_cid(self, trade_id: str, n: int) -> str:
        return f"qt-{trade_id}-stop{n}"

    def current_stop_order(self, trade_id: str) -> dict | None:
        return self.db.one("SELECT * FROM orders WHERE trade_id=? AND kind='stop' AND status NOT IN "
                           "('canceled','expired','rejected','replaced','done_for_day') ORDER BY submitted_at DESC LIMIT 1",
                           (trade_id,))

    def _place_stop(self, t: dict) -> None:
        pos = self.ledger.positions.get(t["symbol"])
        if pos is None:
            return
        n = (self.db.one("SELECT COUNT(*) c FROM orders WHERE trade_id=? AND kind='stop'", (t["trade_id"],))["c"]) + 1
        if t["asset_class"] == "crypto":
            spec = OrderSpec(self._stop_cid(t["trade_id"], n), t["symbol"], "sell", "stop_limit", qty=round_qty(pos.qty),
                             stop_price=t["stop_price"], limit_price=round_price(t["stop_price"] * 0.99),
                             time_in_force="gtc", asset_class="crypto")
        else:
            spec = OrderSpec(self._stop_cid(t["trade_id"], n), t["symbol"], "sell", "stop", qty=round_qty(pos.qty),
                             stop_price=t["stop_price"], time_in_force="day")
        o = self.om.submit(spec, t["trade_id"], "stop", t["stop_price"])
        if o is None:
            self.db.event("critical", "stop_not_placed",
                          f"{t['symbol']}: broker stop could not be placed; software stop active")
            self.notify("error", f"Protective stop for {t['symbol']} failed; software-managed stop is active")

    def move_stop(self, t: dict, new_stop: float, label: str = "trail") -> None:
        if new_stop <= (t["stop_price"] or 0) + 1e-9:
            return  # never lower a stop
        self._upd(t["trade_id"], stop_price=new_stop, trail_stop=new_stop if label == "trail" else t["trail_stop"])
        so = self.current_stop_order(t["trade_id"])
        if so is None:
            return
        try:
            r = self.broker.replace_order(so["broker_order_id"], stop_price=round_price(new_stop))
            self.db.execute("UPDATE orders SET status='replaced' WHERE client_order_id=?", (so["client_order_id"],))
            self.db.insert("orders", {"client_order_id": r["client_order_id"], "broker_order_id": r["id"],
                                      "trade_id": t["trade_id"], "symbol": t["symbol"], "side": "sell", "kind": "stop",
                                      "type": so["type"], "qty": r.get("qty"), "stop_price": new_stop,
                                      "status": r.get("status"), "submitted_at": self.clock().isoformat(),
                                      "updated_at": self.clock().isoformat(), "expected_price": new_stop,
                                      "filled_qty": 0.0})
        except BrokerError as e:
            log.warning("stop replace failed for %s: %s (software stop still enforced)", t["symbol"], e)

    # ---------------- exits ----------------
    def request_exit(self, t: dict, reason: str) -> None:
        if t["status"] == "pending_entry":
            self.om.cancel(f"qt-{t['trade_id']}-entry")
            return
        if t["status"] != "open":
            return
        self._upd(t["trade_id"], status="exiting", exit_reason=reason)
        so = self.current_stop_order(t["trade_id"])
        if so is not None:
            self.om.cancel(so["client_order_id"])
        self.continue_exit(self.trade(t["trade_id"]))

    def continue_exit(self, t: dict) -> None:
        if t is None or t["status"] != "exiting":
            return
        so = self.current_stop_order(t["trade_id"])
        if so is not None:  # cancel not yet confirmed; the stop may still fill
            self.om.cancel(so["client_order_id"])
            if self.current_stop_order(t["trade_id"]) is not None:
                return
        pos = self.ledger.positions.get(t["symbol"])
        if pos is None:
            return
        pending_sell = self.db.one("SELECT * FROM orders WHERE trade_id=? AND kind='exit' AND status NOT IN "
                                   "('filled','canceled','expired','rejected','replaced','done_for_day')", (t["trade_id"],))
        if pending_sell:
            self.om.sync(pending_sell["client_order_id"])
            return
        n = self.db.one("SELECT COUNT(*) c FROM orders WHERE trade_id=? AND kind='exit'", (t["trade_id"],))["c"] + 1
        ac = t["asset_class"]
        spec = OrderSpec(f"qt-{t['trade_id']}-exit{n}", t["symbol"], "sell", "market", qty=round_qty(pos.qty),
                         time_in_force="gtc" if ac == "crypto" else "day", asset_class=ac)
        q = None
        try:
            q = self.broker.latest_quotes([t["symbol"]]).get(t["symbol"])
        except BrokerError:
            pass
        self.om.submit(spec, t["trade_id"], "exit", q["bid"] if q and q.get("bid") else None)

    # ---------------- periodic management ----------------
    def manage(self, prices: dict[str, float], bars_closed: dict[str, dict], now: dt.datetime,
               flatten_now: bool, market_open: bool = True) -> None:
        """prices: latest trade/mid; bars_closed: symbol -> latest confirmed bar dict with atr/exit_sig.
        market_open=False (pre-open / after-close windows): only bookkeeping. Overnight positions get their
        protective stop re-placed and software exits evaluated on regular-hours prices only, so thin
        extended-hours quotes never trigger a sale and stops are never submitted while the market is shut."""
        self.om.poll_open()
        for t in self.active_trades():
            sym = t["symbol"]
            ecid = f"qt-{t['trade_id']}-entry"
            sub = self.db.one("SELECT submitted_at, status FROM orders WHERE client_order_id=?", (ecid,))
            if sub and sub["status"] not in TERMINAL and \
                    (now - dt.datetime.fromisoformat(sub["submitted_at"])).total_seconds() > self.ENTRY_TIMEOUT_S:
                self.om.cancel(ecid)  # unfilled remainder of the entry expires; partial position is kept
                t = self.trade(t["trade_id"])
            if t["status"] == "pending_entry":
                continue
            if t["status"] == "exiting":
                self.continue_exit(t)
                continue
            px = prices.get(sym)
            if px is None:
                continue
            plan = json.loads(t["explanation"] or "{}").get("plan", {})
            entry = t["entry_price"] or px
            R = max(entry - (t["initial_stop"] or entry * 0.99), 1e-9)
            mfe = max(t["mfe"] or 0.0, (px - entry) / R)
            mae = min(t["mae"] or 0.0, (px - entry) / R)
            self._upd(t["trade_id"], mfe=mfe, mae=mae)
            if not market_open:
                continue
            if flatten_now and plan.get("flatten_eod", True):
                self.request_exit(t, "eod_flatten")
                continue
            # software stop backup (broker stop may be missing or not yet triggered)
            if px <= (t["stop_price"] or 0):
                so = self.current_stop_order(t["trade_id"])
                age = None if so is None else (now - dt.datetime.fromisoformat(so["submitted_at"])).total_seconds()
                if so is None or (age is not None and age > self.STOP_GRACE_S and px < t["stop_price"] * 0.998):
                    self.request_exit(t, "software_stop")
                    continue
            if t["target"] and px >= t["target"]:
                self.request_exit(t, "target")
                continue
            entry_working = (self.db.one("SELECT status FROM orders WHERE client_order_id=?", (ecid,)) or {}).get(
                "status") not in TERMINAL
            if t["status"] == "open" and not entry_working and self.current_stop_order(t["trade_id"]) is None \
                    and not self.db.one("SELECT 1 FROM orders WHERE trade_id=? AND kind='stop' AND status='rejected'",
                                        (t["trade_id"],)):
                self._place_stop(t)
            bar = bars_closed.get(sym)
            if bar and bar.get("tf", "5Min") != plan.get("timeframe", "5Min"):
                bar = None  # never trail a daily trade with intraday ATR (or vice versa)
            if bar:
                atr = bar.get("atr") or 0
                gain = bar["close"] - entry
                if plan.get("breakeven_r") and gain >= plan["breakeven_r"] * R:
                    self.move_stop(t, entry, "breakeven")
                    t = self.trade(t["trade_id"])
                if plan.get("trail_atr") and atr and gain >= plan.get("trail_activate_r", 1.0) * R:
                    new = bar["close"] - plan["trail_atr"] * atr
                    if new > (t["stop_price"] or 0) + 0.1 * atr:
                        self.move_stop(t, new, "trail")
                if bar.get("exit_sig"):
                    self.request_exit(self.trade(t["trade_id"]), "thesis_invalidated")
                    continue
                if plan.get("max_bars") and t["entry_time"] and plan.get("timeframe", "5Min") == "5Min":
                    held_min = (now - dt.datetime.fromisoformat(t["entry_time"])).total_seconds() / 60
                    if held_min >= plan["max_bars"] * bar.get("bar_minutes", 5):
                        self.request_exit(self.trade(t["trade_id"]), "time_exit")
