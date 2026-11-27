"""Broker abstraction + a deterministic simulated broker for tests and dry runs."""
from __future__ import annotations

import datetime as dt
import itertools
from dataclasses import dataclass, field

TERMINAL = {"filled", "canceled", "expired", "rejected", "replaced", "done_for_day"}


class BrokerError(RuntimeError):
    def __init__(self, msg: str, uncertain: bool = False, code: int | None = None):
        super().__init__(msg)
        self.uncertain = uncertain   # True: the order MAY have reached the broker
        self.code = code


@dataclass
class OrderSpec:
    client_order_id: str
    symbol: str
    side: str            # buy | sell
    type: str            # market | limit | stop | stop_limit
    qty: float | None = None
    notional: float | None = None
    limit_price: float | None = None
    stop_price: float | None = None
    time_in_force: str = "day"
    asset_class: str = "us_equity"


def round_price(p: float) -> float:
    """Alpaca sub-penny rule: >= $1 max 2 decimals, < $1 max 4."""
    return round(p, 2) if p >= 1 else round(p, 4)


def round_qty(q: float) -> float:
    """Fractional qty: up to 9 decimals; always round DOWN so we never oversell."""
    import math
    return math.floor(q * 1e9) / 1e9


class Broker:
    """Interface. Return plain dicts with normalised keys."""
    def get_account(self) -> dict: ...
    def get_clock(self) -> dict: ...
    def list_positions(self) -> list[dict]: ...
    def list_open_orders(self) -> list[dict]: ...
    def get_order_by_client_id(self, cid: str) -> dict | None: ...
    def submit_order(self, spec: OrderSpec) -> dict: ...
    def cancel_order(self, broker_id: str) -> None: ...
    def replace_order(self, broker_id: str, stop_price: float | None = None, qty: float | None = None) -> dict: ...
    def get_asset(self, symbol: str) -> dict: ...
    def latest_quotes(self, symbols: list[str]) -> dict[str, dict]: ...


@dataclass
class SimBroker(Broker):
    """In-memory broker. Fills market orders at the current quote; stops trigger on `tick`.
    Behaviour switches let tests inject failures."""
    prices: dict = field(default_factory=dict)
    cash: float = 100000.0
    fail_next_submit: str | None = None     # "reject" | "uncertain_sent" | "uncertain_lost"
    partial_fill_ratio: float = 1.0
    offline: bool = False
    orders: dict = field(default_factory=dict)       # broker_id -> order dict
    positions: dict = field(default_factory=dict)    # symbol -> qty
    avg: dict = field(default_factory=dict)
    now: dt.datetime = field(default_factory=lambda: dt.datetime(2026, 11, 24, 15, 0, tzinfo=dt.timezone.utc))
    _ids: itertools.count = field(default_factory=lambda: itertools.count(1))

    def _check(self):
        if self.offline:
            raise BrokerError("broker unreachable", uncertain=True)

    def get_account(self):
        self._check()
        return {"equity": self.cash, "cash": self.cash, "buying_power": self.cash, "status": "ACTIVE",
                "trading_blocked": False, "account_blocked": False}

    def get_clock(self):
        self._check()
        return {"timestamp": self.now, "is_open": True}

    def list_positions(self):
        self._check()
        return [{"symbol": s, "qty": q, "avg_entry_price": self.avg.get(s, 0), "asset_class": "us_equity"}
                for s, q in self.positions.items() if abs(q) > 1e-12]

    def list_open_orders(self):
        self._check()
        return [dict(o) for o in self.orders.values() if o["status"] not in TERMINAL]

    def get_order_by_client_id(self, cid):
        self._check()
        for o in self.orders.values():
            if o["client_order_id"] == cid:
                return dict(o)
        return None

    def submit_order(self, spec: OrderSpec):
        self._check()
        if any(o["client_order_id"] == spec.client_order_id for o in self.orders.values()):
            raise BrokerError("client_order_id must be unique", code=42210000)
        mode, self.fail_next_submit = self.fail_next_submit, None
        if mode == "reject":
            raise BrokerError("insufficient buying power", code=40310000)
        if spec.side == "sell":
            held = self.positions.get(spec.symbol, 0.0)
            reserved = sum(o["qty"] - o["filled_qty"] for o in self.orders.values()
                           if o["symbol"] == spec.symbol and o["side"] == "sell" and o["status"] not in TERMINAL)
            if (spec.qty or 0) > held - reserved + 1e-9:
                raise BrokerError("insufficient qty available for order", code=40310000)
        bid = str(next(self._ids))
        o = {"id": bid, "client_order_id": spec.client_order_id, "symbol": spec.symbol, "side": spec.side,
             "type": spec.type, "qty": spec.qty, "notional": spec.notional, "limit_price": spec.limit_price,
             "stop_price": spec.stop_price, "status": "new", "filled_qty": 0.0, "filled_avg_price": None,
             "submitted_at": self.now, "updated_at": self.now}
        self.orders[bid] = o
        if spec.type in ("market", "limit"):
            self._try_fill(o)
        if mode == "uncertain_sent":
            raise BrokerError("timeout after send", uncertain=True)
        if mode == "uncertain_lost":
            del self.orders[bid]
            raise BrokerError("timeout before send", uncertain=True)
        return dict(o)

    def _fill(self, o, px, qty):
        side = 1 if o["side"] == "buy" else -1
        prev = self.positions.get(o["symbol"], 0.0)
        new = prev + side * qty
        if side > 0:
            self.avg[o["symbol"]] = (self.avg.get(o["symbol"], 0) * prev + px * qty) / new
        self.positions[o["symbol"]] = new
        tot = o["filled_qty"] + qty
        o["filled_avg_price"] = ((o["filled_avg_price"] or 0) * o["filled_qty"] + px * qty) / tot
        o["filled_qty"] = tot
        want = o["qty"] if o["qty"] is not None else (o["notional"] / px)
        o["status"] = "filled" if tot >= want - 1e-12 else "partially_filled"
        o["updated_at"] = self.now
        if o["qty"] is None:
            o["qty"] = want

    def _try_fill(self, o):
        px = self.prices.get(o["symbol"])
        if px is None:
            return
        if o["type"] == "limit":
            if o["side"] == "buy" and px > o["limit_price"]:
                return
            if o["side"] == "sell" and px < o["limit_price"]:
                return
        want = o["qty"] if o["qty"] is not None else o["notional"] / px
        qty = want * self.partial_fill_ratio if o["status"] == "new" else want - o["filled_qty"]
        self._fill(o, px, qty)

    def tick(self, symbol: str, price: float):
        """Move the price; triggers resting stop orders (fills at the worse of stop and price)."""
        self.prices[symbol] = price
        for o in list(self.orders.values()):
            if o["symbol"] != symbol or o["status"] in TERMINAL:
                continue
            if o["type"] == "stop" and o["side"] == "sell" and price <= o["stop_price"]:
                self._fill(o, min(price, o["stop_price"]), o["qty"] - o["filled_qty"])
            elif o["type"] in ("market", "limit") and o["status"] in ("new", "partially_filled"):
                self._try_fill(o)

    def cancel_order(self, broker_id):
        self._check()
        o = self.orders.get(broker_id)
        if o is None:
            raise BrokerError("order not found", code=40410000)
        if o["status"] in TERMINAL:
            raise BrokerError(f"order is {o['status']}", code=42210000)
        o["status"] = "canceled"
        o["updated_at"] = self.now

    def replace_order(self, broker_id, stop_price=None, qty=None):
        self._check()
        o = self.orders[broker_id]
        if o["status"] in TERMINAL:
            raise BrokerError(f"order is {o['status']}", code=42210000)
        o["status"] = "replaced"
        bid = str(next(self._ids))
        n = dict(o, id=bid, client_order_id=o["client_order_id"] + "r", status="new",
                 stop_price=stop_price if stop_price is not None else o["stop_price"],
                 qty=qty if qty is not None else o["qty"], filled_qty=0.0, filled_avg_price=None)
        self.orders[bid] = n
        return dict(n)

    def get_asset(self, symbol):
        return {"symbol": symbol, "tradable": True, "fractionable": True, "status": "active",
                "asset_class": "crypto" if "/" in symbol else "us_equity", "min_order_size": None,
                "min_trade_increment": None, "price_increment": None}

    def latest_quotes(self, symbols):
        self._check()
        return {s: {"bid": self.prices[s] * 0.9999, "ask": self.prices[s] * 1.0001, "ts": self.now}
                for s in symbols if s in self.prices}

    bars_data: dict = field(default_factory=dict)

    def recent_bars(self, symbols, tf, start):
        self._check()
        return {s: d[d.index >= start] for s, d in self.bars_data.items() if s in symbols}

    def snapshots(self, symbols):
        return {}

    def list_assets(self, asset_class="us_equity"):
        return []
