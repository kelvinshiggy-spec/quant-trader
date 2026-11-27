"""Event-driven, multi-asset bar backtester.

Realism rules (each is tested in tests/test_backtest.py):
* Signals are computed on CONFIRMED bars; orders fill at the NEXT bar's open
  (no same-candle execution). The next bar must be in the same session for
  intraday strategies, otherwise the order expires.
* Stops are checked against each bar's low; a gap through the stop fills at the
  open (worse than the stop). If a stop and a target are both touched in one
  bar, the STOP is assumed to fill first (conservative).
* Limit targets fill only if price trades THROUGH the target.
* Every fill pays estimated half-spread + slippage; sells pay SEC/TAF fees;
  crypto pays 0.25% taker on both sides.
* In capital-constrained mode the real Ledger (T+1 settlement in a cash
  account) and the real CapitalGuardian decide every entry, exactly like live.
* Optional random order rejection models broker rejects.
"""
from __future__ import annotations

import datetime as dt
import random
from dataclasses import asdict, dataclass, field

import numpy as np
import pandas as pd

from ..config import RiskLimits
from ..costs import CostModel
from ..guardian import AccountState, CapitalGuardian, Proposal
from ..ledger import Ledger, LedgerViolation
from ..markettime import ET
from ..strategies.base import Strategy


@dataclass
class BTConfig:
    starting_cash: float = 100.0
    account_type: str = "cash"
    limits: RiskLimits = field(default_factory=RiskLimits)
    costs: CostModel = field(default_factory=CostModel)
    capital_constrained: bool = True
    fixed_notional: float = 50.0          # signal-level (unconstrained) mode
    reject_prob: float = 0.0
    seed: int = 7
    flatten_bars_left: int = 2            # exit at close of the bar ending 10 min before the close
    latch_drawdown_halt: bool = True
    regime_by_session: dict | None = None # session -> regime label
    allowed_regimes: set | None = None    # None = trade in all regimes
    start: dt.date | None = None          # only open trades on/after
    end: dt.date | None = None            # only open trades on/before


@dataclass
class Trade:
    trade_id: str
    symbol: str
    strategy: str
    signal_time: str
    entry_time: str
    entry_price: float
    qty: float
    notional: float
    stop0: float
    planned_risk: float
    exit_time: str = ""
    exit_price: float = 0.0
    exit_reason: str = ""
    fees: float = 0.0
    pnl: float = 0.0
    r: float = 0.0
    mfe_r: float = 0.0
    mae_r: float = 0.0
    bars_held: int = 0
    regime: str = ""
    strength: float = 0.0
    session: str = ""


@dataclass
class _Open:
    trade: Trade
    sym: str
    row: int
    stop: float
    target: float
    entry: float
    R: float
    hi: float
    lo: float
    pending_exit: str = ""
    order_key: str = ""


@dataclass
class BTResult:
    trades: list[Trade]
    equity_daily: pd.Series
    rejected: int
    guardian_rejections: dict
    halted_on: str | None
    config: dict

    def trades_df(self) -> pd.DataFrame:
        return pd.DataFrame([asdict(t) for t in self.trades])


class _Arr:
    __slots__ = ("ts", "o", "h", "l", "c", "atr", "adv", "sess", "bars_left", "entry", "stop", "target",
                 "exit_sig", "strength", "asset_class", "pos")

    def __init__(self, f: pd.DataFrame, sig: pd.DataFrame, asset_class: str):
        self.ts = f.index.values
        self.o, self.h, self.l, self.c = (f[k].to_numpy(float) for k in ("open", "high", "low", "close"))
        self.atr = f["atr"].to_numpy(float)
        adv = f["adv"] if "adv" in f else f.get("dollar_vol20", pd.Series(np.nan, index=f.index))
        self.adv = adv.to_numpy(float)
        self.sess = f["session"].to_numpy() if "session" in f else np.array([pd.Timestamp(x).date() for x in f.index])
        self.bars_left = f["bars_left"].to_numpy(float) if "bars_left" in f else np.full(len(f), 1e9)
        self.entry = sig["entry"].to_numpy(bool)
        self.stop = sig["stop"].to_numpy(float)
        self.target = sig["target"].to_numpy(float)
        self.exit_sig = sig["exit_sig"].to_numpy(bool)
        self.strength = sig["strength"].to_numpy(float)
        self.asset_class = asset_class
        self.pos = {t: i for i, t in enumerate(self.ts)}


def run_backtest(strategy: Strategy, frames: dict[str, pd.DataFrame], cfg: BTConfig,
                 signals: dict[str, pd.DataFrame] | None = None) -> BTResult:
    rng = random.Random(cfg.seed)
    plan = strategy.exit_plan()
    costs = cfg.costs
    arrs: dict[str, _Arr] = {}
    for sym, f in frames.items():
        if f is None or f.empty:
            continue
        sig = signals[sym] if signals is not None else strategy.signals(f)
        ac = "crypto" if "/" in sym else strategy.asset_class
        arrs[sym] = _Arr(f, sig, ac)
    if not arrs:
        return BTResult([], pd.Series(dtype=float), 0, {}, None, {})

    timeline = np.unique(np.concatenate([a.ts for a in arrs.values()]))
    sig_at: dict = {}
    for sym, a in arrs.items():
        for i in np.flatnonzero(a.entry):
            sig_at.setdefault(a.ts[i], []).append((sym, i))

    guardian = CapitalGuardian(cfg.limits)
    ledger = Ledger(cfg.starting_cash, cfg.account_type)
    open_: dict[str, _Open] = {}
    pending: dict[str, tuple] = {}   # sym -> (signal_row, notional, decision_risk, order_key)
    trades: list[Trade] = []
    last_px: dict[str, float] = {}
    eq_daily: dict = {}
    g_rej: dict[str, int] = {}
    rejected = 0
    halted_on = None
    tid = 0
    cur_day = None
    cur_week = None
    day_start = week_start = peak = sess_peak = cfg.starting_cash
    consec = 0
    last_loss_at = None
    dd_latch = False

    def equity() -> float:
        return ledger.equity(last_px) if cfg.capital_constrained else 0.0

    def spread_for(a: _Arr, i: int, sym: str) -> float:
        return costs.spread_bps(a.asset_class, a.adv[i] if np.isfinite(a.adv[i]) else None, sym)

    def close_trade(op: _Open, when, price_ref: float, reason: str, a: _Arr, i: int, is_stop=False):
        nonlocal consec, last_loss_at
        sp = spread_for(a, i, op.sym)
        if is_stop:
            px = costs.stop_sell_price(op.stop, a.o[i], a.asset_class, sp)
        elif reason == "target":
            px = price_ref  # resting limit order: no spread crossing
        else:
            px = costs.sell_price(price_ref, a.asset_class, sp)
        t = op.trade
        fee = costs.fee("sell", t.qty, t.qty * px, a.asset_class)
        when_dt = pd.Timestamp(when).to_pydatetime()
        if cfg.capital_constrained:
            pnl = ledger.apply_sell_fill(op.sym, t.qty, px, fee, when_dt)
        else:
            pnl = t.qty * px - fee - (t.notional + t.fees)
        t.exit_time = str(pd.Timestamp(when))
        t.exit_price = px
        t.exit_reason = reason
        t.fees += fee
        t.pnl = pnl
        t.r = pnl / op.R if op.R > 0 else 0.0
        t.mfe_r = (op.hi - op.entry) * t.qty / op.R if op.R > 0 else 0.0
        t.mae_r = (op.lo - op.entry) * t.qty / op.R if op.R > 0 else 0.0
        trades.append(t)
        if pnl < 0:
            consec += 1
            last_loss_at = when_dt
        else:
            consec = 0
        del open_[op.sym]
        last_px.pop(op.sym, None)

    is_crypto_only = all(a.asset_class == "crypto" for a in arrs.values())
    tl_idx = pd.DatetimeIndex(timeline)
    tl_idx = tl_idx.tz_localize("UTC") if tl_idx.tz is None else tl_idx.tz_convert("UTC")
    tl_days = tl_idx.date if is_crypto_only else tl_idx.tz_convert(ET).date

    for k, t in enumerate(timeline):
        ts = tl_idx[k]
        # ---- day / week rollover (equity marks, settlement) ----
        day = tl_days[k]
        if day != cur_day:
            if cur_day is not None:
                eq_daily[cur_day] = equity() if cfg.capital_constrained else None
            cur_day = day
            if cfg.capital_constrained:
                ledger.settle(day if isinstance(day, dt.date) else pd.Timestamp(day).date())
            e = equity()
            day_start = sess_peak = e
            wk = pd.Timestamp(day).isocalendar()[:2]
            if wk != cur_week:
                cur_week = wk
                week_start = e

        # ---- 1) pending entries fill at this bar's open ----
        for sym in list(pending):
            a = arrs[sym]
            i = a.pos.get(t)
            if i is None:
                continue
            srow, notional, risk_est, okey, strength = pending.pop(sym)
            if (plan.flatten_eod and a.sess[i] != a.sess[srow]) or (cfg.reject_prob and rng.random() < cfg.reject_prob):
                rejected += 1
                if cfg.capital_constrained:
                    ledger.release(okey)
                continue
            sp = spread_for(a, i, sym)
            fill = costs.buy_price(a.o[i], a.asset_class, sp)
            stop0 = a.stop[srow]
            if cfg.capital_constrained:
                # fee for crypto is on notional; keep total within the reservation
                fee_rate = costs.crypto_taker_bps * costs.stress / 1e4 if a.asset_class == "crypto" else 0.0
                qty = (notional / (1 + fee_rate)) / fill
            else:
                qty = notional / fill
            fee = costs.fee("buy", qty, qty * fill, a.asset_class)
            tid += 1
            tr = Trade(f"{strategy.name}-{tid}", sym, strategy.key, str(pd.Timestamp(a.ts[srow])), str(ts), fill,
                       qty, qty * fill, stop0, qty * max(fill - stop0, 1e-9), fees=fee,
                       regime=(cfg.regime_by_session or {}).get(a.sess[srow], ""), strength=strength,
                       session=str(a.sess[i]))
            if cfg.capital_constrained:
                try:
                    ledger.apply_buy_fill(sym, qty, fill, fee, ts.to_pydatetime(), strategy.key, tr.trade_id,
                                          a.asset_class, order_key=okey)
                except LedgerViolation:
                    rejected += 1
                    ledger.release(okey)
                    continue
                ledger.release(okey)
            R = tr.planned_risk
            tgt = a.target[srow]
            if not np.isfinite(tgt) and plan.target_r > 0:
                tgt = fill + plan.target_r * (fill - stop0)
            open_[sym] = _Open(tr, sym, i, stop0, tgt if np.isfinite(tgt) else np.inf, fill, R, fill, fill, order_key=okey)
            last_px[sym] = a.o[i]

        # ---- 2) manage open positions on this bar ----
        for sym in list(open_):
            op = open_[sym]
            a = arrs[sym]
            i = a.pos.get(t)
            if i is None:
                continue
            just_entered = (op.trade.entry_time == str(ts))
            op.row = i
            if not just_entered:
                op.trade.bars_held += 1
            if op.pending_exit and not just_entered:
                close_trade(op, ts, a.o[i], op.pending_exit, a, i)
                continue
            if a.l[i] <= op.stop:
                reason = "stop" if op.stop <= op.trade.stop0 + 1e-12 else ("breakeven_stop" if abs(op.stop - op.entry) < 1e-9 else "trailing_stop")
                close_trade(op, ts, op.stop, reason, a, i, is_stop=True)
                continue
            if a.h[i] > op.target * 1.0001:
                close_trade(op, ts, max(op.target, a.o[i]) if not just_entered else op.target, "target", a, i)
                continue
            op.hi = max(op.hi, a.h[i])
            op.lo = min(op.lo, a.l[i])
            last_px[sym] = a.c[i]
            # end-of-session flatten (intraday) or data gap into next session
            nxt_diff_session = (i + 1 >= len(a.ts)) or (a.sess[i + 1] != a.sess[i])
            if plan.flatten_eod and (a.bars_left[i] <= cfg.flatten_bars_left or nxt_diff_session):
                close_trade(op, ts, a.c[i], "eod_flatten", a, i)
                continue
            per_unit_R = op.R / op.trade.qty
            gain = a.c[i] - op.entry
            if plan.breakeven_r and gain >= plan.breakeven_r * per_unit_R:
                op.stop = max(op.stop, op.entry)
            if plan.trail_atr and gain >= plan.trail_activate_r * per_unit_R and np.isfinite(a.atr[i]):
                op.stop = max(op.stop, a.c[i] - plan.trail_atr * a.atr[i])
            if a.exit_sig[i]:
                op.pending_exit = "thesis_invalidated"
            elif plan.max_bars and op.trade.bars_held >= plan.max_bars:
                op.pending_exit = "time_exit"

        # ---- equity marks / guardian latch ----
        if cfg.capital_constrained:
            e = equity()
            peak = max(peak, e)
            sess_peak = max(sess_peak, e)
            if cfg.latch_drawdown_halt and not dd_latch and peak > 0 and (peak - e) / peak * 100 >= cfg.limits.max_drawdown_pct:
                dd_latch = True
                halted_on = str(ts)

        # ---- 3) new entry signals at this bar's close ----
        cands = sig_at.get(t)
        if not cands:
            continue
        cands = sorted(cands, key=lambda x: -arrs[x[0]].strength[x[1]])
        for sym, i in cands:
            a = arrs[sym]
            if sym in open_ or sym in pending:
                continue
            sd = a.sess[i]
            sdate = sd if isinstance(sd, dt.date) else pd.Timestamp(sd).date()
            if (cfg.start and sdate < cfg.start) or (cfg.end and sdate > cfg.end):
                continue
            if cfg.allowed_regimes is not None:
                reg = (cfg.regime_by_session or {}).get(sd)
                if reg not in cfg.allowed_regimes:
                    continue
            if plan.flatten_eod and a.bars_left[i] < 6:
                continue
            if not cfg.capital_constrained:
                pending[sym] = (i, cfg.fixed_notional, 0.0, "", a.strength[i])
                continue
            sp = spread_for(a, i, sym)
            st = AccountState(
                now=ts.to_pydatetime(), equity=equity(), day_start_equity=day_start, week_start_equity=week_start,
                peak_equity=peak, session_peak_equity=sess_peak, buying_power=ledger.buying_power(),
                exposure=ledger.exposure(last_px), open_positions=len(open_) + len(pending),
                open_risk=sum(max(0.0, (o.entry - o.stop) * o.trade.qty) for o in open_.values()),
                open_symbols=tuple(list(open_) + list(pending)), consecutive_losses=consec, last_loss_at=last_loss_at,
                drawdown_halt=dd_latch)
            prop = Proposal(sym, a.asset_class, strategy.name, a.c[i], a.stop[i],
                            costs.round_trip_bps(a.asset_class, sp), sp, 0.0, a.atr[i] if np.isfinite(a.atr[i]) else 0.0)
            d = guardian.evaluate(prop, st)
            if not d.approved:
                key = d.reasons[0].split("(")[0].strip()[:60]
                g_rej[key] = g_rej.get(key, 0) + 1
                continue
            okey = f"bt-{tid}-{sym}-{i}"
            try:
                ledger.reserve(okey, d.notional)
            except LedgerViolation:
                g_rej["ledger: insufficient settled funds"] = g_rej.get("ledger: insufficient settled funds", 0) + 1
                continue
            pending[sym] = (i, d.notional, d.risk_dollars, okey, a.strength[i])

    if cur_day is not None:
        eq_daily[cur_day] = equity() if cfg.capital_constrained else None
    eq = pd.Series(eq_daily, dtype=float) if cfg.capital_constrained else pd.Series(dtype=float)
    c = asdict(cfg)
    c.pop("regime_by_session", None)
    c["allowed_regimes"] = sorted(cfg.allowed_regimes) if cfg.allowed_regimes else None
    return BTResult(trades, eq, rejected, g_rej, halted_on, c)
