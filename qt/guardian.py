"""Capital Guardian: independent risk authority. No strategy can bypass it.

Every new entry (backtest AND live) must obtain an approved `Decision` from
`CapitalGuardian.evaluate()`. The order manager refuses orders without one.
Exits are never blocked: reducing risk is always allowed.

All checks use marked-to-market equity including unrealized losses and
pending (reserved) exposure. Planned risk is an estimate: gaps, slippage,
halts and outages can produce losses larger than planned.
"""
from __future__ import annotations

import datetime as dt
import math
from dataclasses import asdict, dataclass, field

from .config import RiskLimits


@dataclass
class AccountState:
    now: dt.datetime
    equity: float
    day_start_equity: float
    week_start_equity: float
    peak_equity: float
    session_peak_equity: float
    buying_power: float
    exposure: float                      # open positions value + reserved buys
    open_positions: int
    open_risk: float = 0.0               # $ at risk to current stops across open positions
    open_symbols: tuple = ()
    consecutive_losses: int = 0
    last_loss_at: dt.datetime | None = None
    manual_pause: bool = False
    emergency: bool = False
    drawdown_halt: bool = False          # latched; needs the owner to reset
    integrity_ok: bool = True            # reconciliation / broker / clock checks
    integrity_reasons: tuple = ()


@dataclass
class Proposal:
    symbol: str
    asset_class: str
    strategy: str
    entry_price: float
    stop_price: float
    round_trip_cost_bps: float
    spread_bps: float
    data_age_s: float
    atr: float
    max_corr_with_open: float = 0.0
    expected_net_r: float | None = None   # from validated research; None = unknown
    min_qty_increment: float = 1e-9
    min_notional: float = 1.0
    fractionable: bool = True
    halted: bool = False


@dataclass
class Decision:
    approved: bool
    notional: float = 0.0
    qty: float = 0.0
    risk_dollars: float = 0.0
    reasons: list[str] = field(default_factory=list)
    checks: dict = field(default_factory=dict)

    def to_dict(self):
        return asdict(self)


class CapitalGuardian:
    def __init__(self, limits: RiskLimits):
        self.L = limits

    # ---------- account-level gates ----------
    def account_gates(self, s: AccountState) -> tuple[bool, list[str], dict]:
        L, reasons, info = self.L, [], {}
        daily_pl = s.equity - s.day_start_equity
        weekly_pl = s.equity - s.week_start_equity
        dd = 0.0 if s.peak_equity <= 0 else (s.peak_equity - s.equity) / s.peak_equity * 100
        info.update(daily_pl=daily_pl, weekly_pl=weekly_pl, drawdown_pct=dd)
        daily_limit = s.day_start_equity * L.max_daily_loss_pct / 100
        weekly_limit = s.week_start_equity * L.max_weekly_loss_pct / 100
        info["daily_budget_left"] = max(0.0, daily_limit + daily_pl)
        info["weekly_budget_left"] = max(0.0, weekly_limit + weekly_pl)
        dd_budget = s.peak_equity * L.max_drawdown_pct / 100 - (s.peak_equity - s.equity)
        info["drawdown_budget_left"] = max(0.0, dd_budget)

        if s.emergency:
            reasons.append("emergency shutdown active")
        if s.manual_pause:
            reasons.append("new entries paused by owner")
        if not s.integrity_ok:
            reasons.append("system integrity not verified: " + "; ".join(s.integrity_reasons))
        if s.drawdown_halt or dd >= L.max_drawdown_pct:
            reasons.append(f"account drawdown {dd:.2f}% reached the {L.max_drawdown_pct}% limit (owner reset required)")
            info["drawdown_halt"] = True
        if -daily_pl >= daily_limit:
            reasons.append(f"daily loss limit reached ({daily_pl:.2f})")
        if -weekly_pl >= weekly_limit:
            reasons.append(f"weekly loss limit reached ({weekly_pl:.2f})")
        if s.consecutive_losses >= L.consecutive_loss_limit and s.last_loss_at is not None:
            until = s.last_loss_at + dt.timedelta(minutes=L.cooldown_minutes)
            if s.now < until:
                reasons.append(f"cooldown after {s.consecutive_losses} consecutive losses until {until.isoformat()}")
        if L.profit_giveback_enabled:
            peak_profit = s.session_peak_equity - s.day_start_equity
            if peak_profit >= L.profit_giveback_min_peak:
                kept = daily_pl / peak_profit
                info["session_profit_kept_pct"] = kept * 100
                if kept <= 1 - L.profit_giveback_pct / 100:
                    reasons.append(f"profit giveback: kept {kept*100:.0f}% of session peak profit {peak_profit:.2f}")
        if L.principal_protection_enabled:
            cushion = s.equity - L.principal_floor
            info["principal_cushion"] = cushion
            if cushion <= 0:
                reasons.append(f"principal protection: equity {s.equity:.2f} has no cushion above floor {L.principal_floor:.2f}")
        if s.open_positions >= L.max_positions:
            reasons.append(f"max simultaneous positions ({L.max_positions}) reached")
        if s.exposure >= s.equity * L.max_total_alloc_pct / 100 - 0.01:
            reasons.append("max total portfolio allocation reached")
        return (not reasons), reasons, info

    def risk_budget(self, s: AccountState, info: dict) -> float:
        L = self.L
        per_trade = s.equity * L.risk_per_trade_pct / 100
        caps = [per_trade,
                info.get("daily_budget_left", per_trade) - s.open_risk,
                info.get("weekly_budget_left", per_trade) - s.open_risk,
                info.get("drawdown_budget_left", per_trade) - s.open_risk]
        if L.principal_protection_enabled:
            cushion = max(0.0, s.equity - L.principal_floor)
            caps.append(cushion * L.principal_profit_risk_fraction - s.open_risk)
        return max(0.0, min(caps))

    # ---------- trade-level evaluation ----------
    def evaluate(self, p: Proposal, s: AccountState) -> Decision:
        L = self.L
        ok, reasons, info = self.account_gates(s)
        checks = {"account": ok}
        if p.symbol in s.open_symbols:
            reasons.append(f"already holding {p.symbol} (no adding / averaging down)")
        if p.halted:
            reasons.append("asset halted / not tradable")
        if p.data_age_s > L.stale_data_seconds:
            reasons.append(f"stale data ({p.data_age_s:.0f}s old)")
        if p.spread_bps > L.max_spread_bps:
            reasons.append(f"spread {p.spread_bps:.1f}bps exceeds {L.max_spread_bps}bps")
        if not (p.stop_price > 0 and p.stop_price < p.entry_price):
            reasons.append("invalid stop (must be below entry for a long)")
        if p.atr > 0 and (p.entry_price - p.stop_price) > L.max_abnormal_move_atr * p.atr:
            reasons.append("stop distance abnormally wide vs ATR")
        if p.max_corr_with_open > L.max_pair_correlation:
            reasons.append(f"correlation {p.max_corr_with_open:.2f} with an open position exceeds {L.max_pair_correlation}")
        if p.expected_net_r is not None and p.expected_net_r <= 0:
            reasons.append("non-positive expected net expectancy")
        checks["trade"] = not reasons
        if reasons:
            return Decision(False, reasons=reasons, checks={**checks, **info})

        budget = self.risk_budget(s, info)
        cost_per_unit = p.entry_price * p.round_trip_cost_bps / 1e4
        risk_per_unit = (p.entry_price - p.stop_price) + cost_per_unit
        if budget <= 0 or risk_per_unit <= 0:
            return Decision(False, reasons=["no remaining risk budget"], checks={**checks, **info})
        qty = budget / risk_per_unit
        notional = qty * p.entry_price
        caps = {
            "risk": notional,
            "position_alloc": s.equity * L.max_position_alloc_pct / 100,
            "total_alloc": s.equity * L.max_total_alloc_pct / 100 - s.exposure,
            "buying_power": s.buying_power,
        }
        binding = min(caps, key=caps.get)
        notional = max(0.0, caps[binding])
        if not p.fractionable:
            qty = math.floor(notional / p.entry_price)
            notional = qty * p.entry_price
        notional = math.floor(notional * 100) / 100  # cents
        qty = notional / p.entry_price
        if p.min_qty_increment > 1e-9:
            qty = math.floor(qty / p.min_qty_increment) * p.min_qty_increment
            notional = math.floor(qty * p.entry_price * 100) / 100
        min_notional = max(L.min_order_notional, p.min_notional)
        if notional < min_notional or qty <= 0:
            return Decision(False, reasons=[f"position too small to size safely (${notional:.2f} < ${min_notional:.2f}, "
                                            f"binding cap: {binding})"], checks={**checks, **info})
        risk = qty * risk_per_unit
        return Decision(True, notional=notional, qty=qty, risk_dollars=risk,
                        reasons=[f"approved; size limited by {binding}"],
                        checks={**checks, **info, "caps": caps, "risk_budget": budget})

    def status(self, s: AccountState) -> dict:
        ok, reasons, info = self.account_gates(s)
        L = self.L
        return {
            "can_open_new": ok, "blocking_reasons": reasons,
            "starting_capital": L.starting_capital, "equity": s.equity, "peak_equity": s.peak_equity,
            "protected_floor": L.principal_floor if L.principal_protection_enabled else None,
            "principal_protection": L.principal_protection_enabled,
            "risk_capacity_per_trade": self.risk_budget(s, info),
            **info,
        }
