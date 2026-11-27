"""Market Regime Engine.

Daily regime labels are computed from data available BEFORE the session opens
(previous completed daily bars), so they can gate trading without look-ahead:

  trend:      bull (SPY > SMA50 > SMA200, SMA50 rising) | bear (SPY < SMA50 < SMA200) | sideways
  volatility: high (20d realized vol in top 30% of trailing year) | low (bottom 30%) | normal
  selloff:    SPY fell > 2% on the prior day or is > 8% below its 52-week high with falling breadth
  breadth:    share of sector ETFs above their SMA50

A strategy may only be gated by regime after research shows (on training data,
re-checked out-of-sample) that its expectancy differs by regime.
Intraday buckets (opening / midday / closing) are reported for analysis.
"""
from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd

from . import indicators as ind


def daily_regimes(spy_daily: pd.DataFrame, sector_daily: dict[str, pd.DataFrame] | None = None) -> pd.DataFrame:
    d = spy_daily.copy()
    d.index = pd.Index([x.date() if hasattr(x, "date") else x for x in d.index])
    c = d["close"]
    sma50, sma200 = ind.sma(c, 50), ind.sma(c, 200)
    rv = ind.realized_vol(c, 20)
    rv_rank = rv.rolling(252, min_periods=120).apply(lambda a: (a <= a[-1]).mean(), raw=True)
    ret1 = c.pct_change()
    off_high = c / c.rolling(252, min_periods=60).max() - 1
    trend = np.where((c > sma50) & (sma50 > sma200) & (sma50 > sma50.shift(10)), "bull",
                     np.where((c < sma50) & (sma50 < sma200), "bear", "sideways"))
    vol = np.where(rv_rank >= 0.7, "high", np.where(rv_rank <= 0.3, "low", "normal"))
    breadth = pd.Series(np.nan, index=d.index)
    if sector_daily:
        above = []
        for s, x in sector_daily.items():
            x = x.copy()
            x.index = pd.Index([y.date() if hasattr(y, "date") else y for y in x.index])
            above.append((x["close"] > ind.sma(x["close"], 50)).astype(float).rename(s))
        breadth = pd.concat(above, axis=1).reindex(d.index).mean(axis=1)
    selloff = (ret1 < -0.02) | ((off_high < -0.08) & (breadth < 0.4))
    out = pd.DataFrame({"trend": trend, "vol": vol, "selloff": selloff.fillna(False), "breadth": breadth,
                        "rv20": rv, "spy_close": c}, index=d.index)
    out["label"] = np.where(out["selloff"], "selloff", out["trend"] + "_" + out["vol"])
    out.loc[sma200.isna(), "label"] = "unknown"
    # shift: the regime for session D uses data through D-1
    shifted = out.shift(1)
    shifted["label"] = shifted["label"].fillna("unknown")
    return shifted


def regime_for_sessions(regimes: pd.DataFrame, sessions) -> dict:
    lab = regimes["label"]
    return {s: lab.get(s, "unknown") for s in sessions}


def coarse(label: str) -> str:
    """Collapse to a few buckets so per-regime samples stay large enough."""
    if label in ("unknown", None) or not isinstance(label, str):
        return "unknown"
    if label == "selloff":
        return "selloff"
    trend, vol = label.split("_")
    if vol == "high":
        return "high_vol"
    return trend


def intraday_bucket(minutes_since_open: float) -> str:
    if minutes_since_open < 60:
        return "opening"
    if minutes_since_open >= 330:
        return "closing"
    return "midday"


def describe_current(regimes: pd.DataFrame, today: dt.date) -> dict:
    row = regimes.loc[:today].iloc[-1] if len(regimes.loc[:today]) else None
    if row is None:
        return {"label": "unknown"}
    return {"label": row["label"], "coarse": coarse(row["label"]), "trend": row["trend"], "vol": row["vol"],
            "selloff": bool(row["selloff"]), "breadth": None if pd.isna(row["breadth"]) else float(row["breadth"])}
