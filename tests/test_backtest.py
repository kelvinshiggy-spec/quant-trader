"""Backtester realism tests on hand-built bars (synthetic fixtures, not results)."""
import datetime as dt
from dataclasses import dataclass, field

import numpy as np
import pandas as pd
import pytest

from qt.backtest.engine import BTConfig, run_backtest
from qt.costs import CostModel
from qt.data.historical import to_rth
from qt.strategies.base import ExitPlan, Strategy, frame
from qt.strategies.features import intraday_features
from qt.strategies.library import ALL_STRATEGIES

ZERO = CostModel(stress=0.0, sec_fee_rate=0, taf_per_share=0)


def bars(day: dt.date, closes, opens=None, highs=None, lows=None, vol=1e6):
    """5-min RTH bars starting 9:30 ET on `day`."""
    start = pd.Timestamp(day.isoformat() + " 09:30", tz="America/New_York").tz_convert("UTC")
    idx = pd.date_range(start, periods=len(closes), freq="5min")
    c = np.array(closes, float)
    o = np.array(opens if opens is not None else c, float)
    h = np.array(highs if highs is not None else np.maximum(o, c), float)
    lo = np.array(lows if lows is not None else np.minimum(o, c), float)
    df = pd.DataFrame({"open": o, "high": h, "low": lo, "close": c, "volume": vol}, index=idx)
    df["session"] = day
    n = len(df)
    df["atr"] = 1.0
    df["adv"] = 1e12  # tiny spread tier
    df["bars_left"] = np.arange(n)[::-1].astype(float) + (78 - n)
    return df


@dataclass
class Scripted(Strategy):
    """Enters on bars flagged in `entries` with a given stop/target."""
    name: str = "scripted"
    defaults: dict = field(default_factory=lambda: {"entries": (), "stop": 0.0, "target": np.nan,
                                                    "trail": 0.0, "flat": True, "exits": ()})

    def exit_plan(self):
        return ExitPlan(trail_atr=self.p("trail"), trail_activate_r=1.0, flatten_eod=self.p("flat"))

    def signals(self, f):
        e = np.zeros(len(f), bool)
        e[list(self.p("entries"))] = True
        x = np.zeros(len(f), bool)
        x[list(self.p("exits"))] = True
        return frame(f, e, self.p("stop"), target=np.full(len(f), self.p("target")), exit_sig=x)


def cfg(**kw):
    c = BTConfig(costs=ZERO, capital_constrained=False, fixed_notional=50.0)
    for k, v in kw.items():
        setattr(c, k, v)
    return c


D = dt.date(2026, 11, 24)


def test_entry_fills_next_bar_open_not_signal_close():
    df = bars(D, [100, 101, 102, 103, 104], opens=[100, 100.5, 101.7, 103, 104])
    r = run_backtest(Scripted(params={"entries": (0,), "stop": 90.0}), {"X": df}, cfg())
    t = r.trades[0]
    assert t.entry_price == pytest.approx(100.5)  # bar 1 open, not bar 0 close (100)
    assert t.signal_time < t.entry_time


def test_gap_through_stop_fills_at_open():
    df = bars(D, [100, 100, 95, 95], opens=[100, 100, 94, 95], lows=[99.5, 99.5, 93, 94])
    r = run_backtest(Scripted(params={"entries": (0,), "stop": 98.0}), {"X": df}, cfg())
    t = r.trades[0]
    assert t.exit_reason == "stop" and t.exit_price == pytest.approx(94.0)


def test_stop_assumed_before_target_in_same_bar():
    df = bars(D, [100, 100, 100], highs=[100, 100, 110], lows=[100, 100, 90])
    r = run_backtest(Scripted(params={"entries": (0,), "stop": 95.0, "target": 105.0}), {"X": df}, cfg())
    assert r.trades[0].exit_reason == "stop"


def test_target_needs_trade_through():
    df = bars(D, [100, 100, 105, 106] + [106] * 3, highs=[100, 100, 105.0, 107] + [106] * 3)
    r = run_backtest(Scripted(params={"entries": (0,), "stop": 90.0, "target": 105.0}), {"X": df}, cfg())
    t = r.trades[0]
    assert t.exit_reason == "target" and t.exit_time.startswith(str(df.index[3])[:16])


def test_eod_flatten_and_no_overnight_for_intraday():
    df = bars(D, [100] * 78)
    r = run_backtest(Scripted(params={"entries": (60,), "stop": 90.0}), {"X": df}, cfg())
    assert r.trades[0].exit_reason == "eod_flatten"
    assert pd.Timestamp(r.trades[0].exit_time).tz_convert("America/New_York").strftime("%H:%M") == "15:45"


def test_signal_in_last_30_minutes_is_ignored():
    df = bars(D, [100] * 78)
    r = run_backtest(Scripted(params={"entries": (74,), "stop": 90.0}), {"X": df}, cfg())
    assert r.trades == []


def test_trailing_stop_only_ratchets_up():
    closes = [100, 100, 103, 106, 104, 101]
    df = bars(D, closes + [101] * 10, lows=[100, 100, 103, 106, 103.9, 100] + [101] * 10)
    df["atr"] = 1.0
    r = run_backtest(Scripted(params={"entries": (0,), "stop": 99.0, "trail": 2.0}), {"X": df}, cfg())
    t = r.trades[0]
    assert t.exit_reason == "trailing_stop"
    assert t.exit_price == pytest.approx(104.0)  # 106 - 2*ATR, never lowered


def test_thesis_exit_executes_next_open():
    df = bars(D, [100, 100, 100, 100, 100], opens=[100, 100, 100, 101, 100])
    r = run_backtest(Scripted(params={"entries": (0,), "stop": 90.0, "exits": (2,)}), {"X": df}, cfg())
    t = r.trades[0]
    assert t.exit_reason == "thesis_invalidated" and t.exit_price == pytest.approx(101.0)


def test_costs_reduce_pnl():
    df = bars(D, [100] * 10)
    a = run_backtest(Scripted(params={"entries": (0,), "stop": 90.0}), {"X": df}, cfg()).trades[0]
    b = run_backtest(Scripted(params={"entries": (0,), "stop": 90.0}), {"X": df}, cfg(costs=CostModel())).trades[0]
    assert a.pnl == pytest.approx(0.0, abs=1e-9) and b.pnl < 0


def test_constrained_mode_respects_100_and_t1_settlement():
    d1, d2 = dt.date(2026, 11, 23), dt.date(2026, 11, 24)
    df = pd.concat([bars(d1, [100] * 78), bars(d2, [100] * 78)])
    # 4 signals on day 1 (after each exit), 1 on day 2
    st = Scripted(params={"entries": (10, 20, 30, 40, 78 + 10), "stop": 99.0, "exits": (12, 22, 32, 42, 78 + 12)})
    c = BTConfig(costs=ZERO, capital_constrained=True, account_type="cash")
    r = run_backtest(st, {"X": df}, c)
    day1 = [t for t in r.trades if t.session == str(d1)]
    # each trade uses $50; with $100 settled cash only two round trips are possible on day 1
    assert len(day1) == 2
    assert sum(t.notional for t in day1) <= 100.0 + 1e-6
    assert any(t.session == str(d2) for t in r.trades)  # proceeds settled T+1, trading resumes
    c2 = BTConfig(costs=ZERO, capital_constrained=True, account_type="margin_1x")
    r2 = run_backtest(st, {"X": df}, c2)
    assert len([t for t in r2.trades if t.session == str(d1)]) == 4


def test_guardian_caps_simultaneous_positions_in_backtest():
    dfs = {s: bars(D, [100] * 78) for s in ("A", "B", "C")}
    st = Scripted(params={"entries": (10,), "stop": 99.0})
    r = run_backtest(st, dfs, BTConfig(costs=ZERO, capital_constrained=True))
    assert len(r.trades) == 2
    assert sum(t.notional for t in r.trades) <= 80.0 + 1e-6  # max 80% total allocation


def _random_walk(n_days=30, seed=3):
    rng = np.random.default_rng(seed)
    days = pd.bdate_range("2026-01-05", periods=n_days)
    frames = []
    px = 100.0
    for d in days:
        if d.date() in (dt.date(2026, 1, 19), dt.date(2026, 2, 16)):
            continue
        r = rng.normal(0, 0.002, 78)
        c = px * np.cumprod(1 + r)
        o = np.r_[px, c[:-1]]
        h = np.maximum(o, c) * (1 + np.abs(rng.normal(0, 0.0005, 78)))
        lo = np.minimum(o, c) * (1 - np.abs(rng.normal(0, 0.0005, 78)))
        v = rng.integers(1e5, 1e6, 78).astype(float)
        start = pd.Timestamp(d.date().isoformat() + " 09:30", tz="America/New_York").tz_convert("UTC")
        frames.append(pd.DataFrame({"open": o, "high": h, "low": lo, "close": c, "volume": v},
                                   index=pd.date_range(start, periods=78, freq="5min")))
        px = c[-1]
    return to_rth(pd.concat(frames), 5)


def test_features_have_no_lookahead():
    raw = _random_walk()
    full = intraday_features(raw, None, None, 5)
    cut = len(raw) - 100
    part = intraday_features(raw.iloc[:cut], None, None, 5)
    cols = ["atr", "ema20", "rsi14", "vwap", "relvol", "orh3", "bb_lo", "adx", "hh12", "day_high", "bars_left"]
    a = full[cols].iloc[:cut]
    b = part[cols]
    pd.testing.assert_frame_equal(a, b, check_exact=False, rtol=1e-9)


def test_all_strategy_signals_are_causal():
    raw = _random_walk(40, seed=11)
    full = intraday_features(raw, None, None, 5)
    cut = len(raw) - 150
    part = intraday_features(raw.iloc[:cut], None, None, 5)
    for cls in ALL_STRATEGIES:
        st = cls()
        if st.timeframe != "5Min":
            continue
        a = st.signals(full).iloc[:cut]
        b = st.signals(part)
        assert (a["entry"].to_numpy() == b["entry"].to_numpy()).all(), st.name
