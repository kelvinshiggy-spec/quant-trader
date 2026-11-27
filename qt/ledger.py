"""Independent virtual portfolio ledger.

Enforces the internal $100 account no matter how much buying power the Alpaca
paper account has. Models a real small account:

* account_type="cash": buys may only use SETTLED cash. US-equity sale proceeds
  settle T+1, so they cannot be reused until the next trading day. This avoids
  good-faith violations by construction.
* account_type="margin_1x": no leverage, but sale proceeds are reusable at once
  (how a sub-$2,000 margin account behaves). Never more than 1x equity.

Buys reserve cash when the order is submitted, so pending orders can never
double-spend. Fills are applied only when the broker confirms them.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import asdict, dataclass, field

from .markettime import settlement_date

EPS = 1e-9


class LedgerViolation(RuntimeError):
    pass


@dataclass
class Position:
    symbol: str
    asset_class: str
    qty: float
    avg_price: float
    cost_basis: float          # includes buy fees
    opened_at: str
    strategy: str
    trade_id: str
    fees: float = 0.0

    def market_value(self, price: float) -> float:
        return self.qty * price


@dataclass
class Unsettled:
    amount: float
    settles_on: str  # ISO date


@dataclass
class Ledger:
    starting_cash: float = 100.0
    account_type: str = "cash"
    settled_cash: float = field(default=None)  # type: ignore[assignment]
    unsettled: list[Unsettled] = field(default_factory=list)
    reservations: dict[str, float] = field(default_factory=dict)
    positions: dict[str, Position] = field(default_factory=dict)
    realized_pnl: float = 0.0
    fees_paid: float = 0.0

    def __post_init__(self):
        if self.settled_cash is None:
            self.settled_cash = float(self.starting_cash)
        if self.account_type not in ("cash", "margin_1x"):
            raise ValueError("account_type must be cash or margin_1x")

    # ---------------- cash views ----------------
    @property
    def unsettled_cash(self) -> float:
        return sum(u.amount for u in self.unsettled)

    @property
    def total_cash(self) -> float:
        return self.settled_cash + self.unsettled_cash

    @property
    def reserved(self) -> float:
        return sum(self.reservations.values())

    def buying_power(self) -> float:
        """Cash that may be committed to a NEW buy right now."""
        base = self.settled_cash if self.account_type == "cash" else self.total_cash
        return max(0.0, base - self.reserved)

    def settle(self, today: dt.date) -> float:
        """Move matured unsettled proceeds into settled cash. Returns amount settled."""
        moved, keep = 0.0, []
        for u in self.unsettled:
            if dt.date.fromisoformat(u.settles_on) <= today:
                moved += u.amount
            else:
                keep.append(u)
        self.unsettled = keep
        self.settled_cash += moved
        return moved

    # ---------------- order reservations ----------------
    def reserve(self, order_key: str, amount: float) -> None:
        if amount <= 0:
            raise LedgerViolation("reservation must be positive")
        if order_key in self.reservations:
            raise LedgerViolation(f"duplicate reservation {order_key}")
        if amount > self.buying_power() + EPS:
            raise LedgerViolation(
                f"insufficient {'settled ' if self.account_type == 'cash' else ''}funds: "
                f"need {amount:.2f}, have {self.buying_power():.2f}")
        self.reservations[order_key] = amount

    def release(self, order_key: str) -> float:
        return self.reservations.pop(order_key, 0.0)

    # ---------------- fills ----------------
    def _spend(self, amount: float) -> None:
        if self.account_type == "cash":
            if amount > self.settled_cash + EPS:
                raise LedgerViolation(f"buy {amount:.4f} exceeds settled cash {self.settled_cash:.4f}")
            self.settled_cash -= amount
        else:
            if amount > self.total_cash + EPS:
                raise LedgerViolation(f"buy {amount:.4f} exceeds cash {self.total_cash:.4f} (no leverage)")
            take = min(self.settled_cash, amount)
            self.settled_cash -= take
            rest = amount - take
            # consume unsettled oldest-first
            while rest > EPS and self.unsettled:
                u = self.unsettled[0]
                use = min(u.amount, rest)
                u.amount -= use
                rest -= use
                if u.amount <= EPS:
                    self.unsettled.pop(0)
        if abs(self.settled_cash) < 1e-7:
            self.settled_cash = 0.0

    def apply_buy_fill(self, symbol: str, qty: float, price: float, fee: float, when: dt.datetime,
                       strategy: str, trade_id: str, asset_class: str = "us_equity",
                       order_key: str | None = None) -> Position:
        if qty <= 0 or price <= 0:
            raise LedgerViolation("buy fill must have positive qty and price")
        cost = qty * price + fee
        if order_key is not None:
            res = self.reservations.get(order_key, 0.0)
            # release the consumed part of the reservation (partial fills keep the rest)
            self.reservations[order_key] = max(0.0, res - cost)
            if self.reservations[order_key] <= EPS:
                self.reservations.pop(order_key, None)
        self._spend(cost)
        self.fees_paid += fee
        p = self.positions.get(symbol)
        if p is None:
            p = Position(symbol, asset_class, qty, price, cost, when.isoformat(), strategy, trade_id, fee)
            self.positions[symbol] = p
        else:
            if p.trade_id != trade_id:
                raise LedgerViolation(f"{symbol}: adding to an existing position is not allowed")
            p.cost_basis += cost
            p.qty += qty
            p.avg_price = (p.cost_basis - p.fees - fee) / p.qty
            p.fees += fee
        return p

    def apply_sell_fill(self, symbol: str, qty: float, price: float, fee: float,
                        when: dt.datetime) -> float:
        """Returns realized P&L (net of buy and sell fees) for the sold quantity."""
        p = self.positions.get(symbol)
        if p is None:
            raise LedgerViolation(f"sell of {symbol} with no ledger position (no shorting)")
        if qty > p.qty + 1e-9:
            raise LedgerViolation(f"sell qty {qty} exceeds position {p.qty} (no shorting)")
        frac = min(1.0, qty / p.qty)
        basis = p.cost_basis * frac
        proceeds = qty * price - fee
        pnl = proceeds - basis
        self.realized_pnl += pnl
        self.fees_paid += fee
        sd = settlement_date(when.date(), p.asset_class)
        if self.account_type == "cash" and sd > when.date():
            self.unsettled.append(Unsettled(proceeds, sd.isoformat()))
        else:
            self.settled_cash += proceeds
        p.qty -= qty
        p.cost_basis -= basis
        p.fees -= p.fees * frac
        if p.qty <= 1e-9:
            del self.positions[symbol]
        return pnl

    # ---------------- valuation ----------------
    def positions_value(self, prices: dict[str, float]) -> float:
        tot = 0.0
        for s, p in self.positions.items():
            px = prices.get(s)
            tot += p.qty * (px if px is not None else p.avg_price)
        return tot

    def equity(self, prices: dict[str, float]) -> float:
        """Marked-to-market equity (cash + positions at current prices)."""
        return self.total_cash + self.positions_value(prices)

    def unrealized_pnl(self, prices: dict[str, float]) -> float:
        return sum(p.qty * prices.get(s, p.avg_price) - p.cost_basis for s, p in self.positions.items())

    def exposure(self, prices: dict[str, float]) -> float:
        return self.positions_value(prices) + self.reserved

    # ---------------- persistence ----------------
    def to_dict(self) -> dict:
        d = asdict(self)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Ledger":
        led = cls(starting_cash=d["starting_cash"], account_type=d["account_type"],
                  settled_cash=d["settled_cash"])
        led.unsettled = [Unsettled(**u) for u in d.get("unsettled", [])]
        led.reservations = dict(d.get("reservations", {}))
        led.positions = {k: Position(**v) for k, v in d.get("positions", {}).items()}
        led.realized_pnl = d.get("realized_pnl", 0.0)
        led.fees_paid = d.get("fees_paid", 0.0)
        return led

    def check_invariants(self) -> list[str]:
        errs = []
        if self.settled_cash < -1e-6:
            errs.append(f"negative settled cash {self.settled_cash}")
        if any(u.amount < -1e-6 for u in self.unsettled):
            errs.append("negative unsettled lot")
        if any(p.qty < 0 for p in self.positions.values()):
            errs.append("negative position (short) detected")
        if self.reserved > self.total_cash + 1e-6:
            errs.append("reservations exceed cash")
        return errs
