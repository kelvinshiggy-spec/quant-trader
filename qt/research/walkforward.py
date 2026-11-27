"""Walk-forward selection, holdout evaluation, regime gating and statistics.

Protocol (chronological, no shuffling):
  DEV period      : rolling walk-forward. Train 12 months -> choose params by train
                    expectancy (min trade count) -> trade the next 3 months untouched.
  HOLDOUT period  : the params chosen on the whole DEV period are evaluated ONCE.
                    The holdout is never used for selection. Later nightly runs judge
                    new strategy versions on data that arrives after the holdout.
Signal-level trades (fixed $50 notional, one position per symbol) give the
statistical sample; the capital-constrained $100 simulation gives the
realistic account path (guardian + T+1 settlement + 2-position cap).
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

import numpy as np
import pandas as pd

from ..backtest.metrics import bootstrap_mean, trade_metrics
from ..regime import coarse

MIN_TRAIN_TRADES = 30


@dataclass
class Window:
    train_start: dt.date
    train_end: dt.date
    test_start: dt.date
    test_end: dt.date


def make_windows(dev_start: dt.date, dev_end: dt.date, train_months=12, test_months=3) -> list[Window]:
    out = []
    ts = pd.Timestamp(dev_start)
    while True:
        tr_end = ts + pd.DateOffset(months=train_months) - pd.Timedelta(days=1)
        te_start = tr_end + pd.Timedelta(days=1)
        te_end = te_start + pd.DateOffset(months=test_months) - pd.Timedelta(days=1)
        if te_start.date() > dev_end:
            break
        out.append(Window(ts.date(), tr_end.date(), te_start.date(), min(te_end.date(), dev_end)))
        ts = ts + pd.DateOffset(months=test_months)
    return out


def _sess(df: pd.DataFrame) -> pd.Series:
    return pd.to_datetime(df["session"]).dt.date


def slice_trades(df: pd.DataFrame, a: dt.date, b: dt.date) -> pd.DataFrame:
    if df is None or df.empty:
        return df
    s = _sess(df)
    return df[(s >= a) & (s <= b)]


def add_regime(df: pd.DataFrame, regime_by_session: dict) -> pd.DataFrame:
    if df is None or df.empty:
        return df
    df = df.copy()
    df["regime_coarse"] = [coarse(regime_by_session.get(s, "unknown")) for s in _sess(df)]
    return df


def allowed_regimes(train: pd.DataFrame, min_n: int = 20) -> set:
    if train is None or train.empty:
        return set()
    g = train.groupby("regime_coarse")["r"].agg(["mean", "count"])
    return set(g[(g["mean"] > 0) & (g["count"] >= min_n)].index)


def walk_forward(trades_by_param: dict[str, pd.DataFrame], windows: list[Window], default_key: str,
                 gated: bool = False) -> tuple[pd.DataFrame, list[dict]]:
    oos, log = [], []
    for w in windows:
        best, best_score = default_key, -np.inf
        for k, t in trades_by_param.items():
            tr = slice_trades(t, w.train_start, w.train_end)
            if tr is None or len(tr) < MIN_TRAIN_TRADES:
                continue
            sc = tr["r"].mean()
            if sc > best_score:
                best, best_score = k, sc
        te = slice_trades(trades_by_param[best], w.test_start, w.test_end)
        allowed = None
        if gated:
            allowed = allowed_regimes(slice_trades(trades_by_param[best], w.train_start, w.train_end))
            te = te[te["regime_coarse"].isin(allowed)] if te is not None and not te.empty else te
        if te is not None and not te.empty:
            te = te.assign(window=str(w.test_start), chosen=best)
            oos.append(te)
        log.append({"test": f"{w.test_start}..{w.test_end}", "chosen": best,
                    "train_exp_r": None if best_score == -np.inf else float(best_score),
                    "test_trades": 0 if te is None else int(len(te)),
                    "test_exp_r": None if te is None or te.empty else float(te["r"].mean()),
                    "allowed_regimes": sorted(allowed) if allowed is not None else None})
    df = pd.concat(oos) if oos else pd.DataFrame()
    return df, log


def choose_on_dev(trades_by_param: dict[str, pd.DataFrame], dev_start, dev_end, default_key: str) -> str:
    best, best_score = default_key, -np.inf
    for k, t in trades_by_param.items():
        tr = slice_trades(t, dev_start, dev_end)
        if tr is None or len(tr) < MIN_TRAIN_TRADES:
            continue
        if tr["r"].mean() > best_score:
            best, best_score = k, tr["r"].mean()
    return best


def summarize(t: pd.DataFrame) -> dict:
    if t is None or t.empty:
        return {"trades": 0}
    m = trade_metrics(t)
    b = bootstrap_mean(t["r"].to_numpy())
    m.update({"r_ci_lo": b["lo"], "r_ci_hi": b["hi"], "p_value": b["p_gt0"]})
    if "regime_coarse" in t:
        m["by_regime"] = t.groupby("regime_coarse")["r"].agg(["mean", "count"]).round(4).to_dict("index")
    if "entry_time" in t:
        mins = pd.to_datetime(t["entry_time"], utc=True).dt.tz_convert("America/New_York")
        mo = (mins.dt.hour * 60 + mins.dt.minute) - 570
        bucket = np.where(mo < 60, "opening", np.where(mo >= 330, "closing", "midday"))
        m["by_time_of_day"] = t.assign(b=bucket).groupby("b")["r"].agg(["mean", "count"]).round(4).to_dict("index")
    return _clean(m)


def _clean(d):
    if isinstance(d, dict):
        return {str(k): _clean(v) for k, v in d.items()}
    if isinstance(d, (np.floating,)):
        return float(d)
    if isinstance(d, (np.integer,)):
        return int(d)
    if isinstance(d, float) and (np.isnan(d) or np.isinf(d)):
        return None if np.isnan(d) else (1e9 if d > 0 else -1e9)
    return d
