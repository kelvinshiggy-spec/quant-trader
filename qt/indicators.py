"""Causal technical indicators. Every value at row t uses only rows <= t.

Bars are CONFIRMED (closed) bars; no provisional intrabar values enter here.
Intraday frames carry a `session` column (ET trading date) so session-anchored
features (VWAP, opening range, relative volume by time of day) reset daily.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def sma(s: pd.Series, n: int) -> pd.Series:
    return s.rolling(n, min_periods=n).mean()


def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False, min_periods=n).mean()


def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    d = close.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    rs = up / dn.replace(0, np.nan)
    out = 100 - 100 / (1 + rs)
    return out.where(dn != 0, 100.0)


def true_range(df: pd.DataFrame) -> pd.Series:
    pc = df["close"].shift(1)
    return pd.concat([df["high"] - df["low"], (df["high"] - pc).abs(), (df["low"] - pc).abs()], axis=1).max(axis=1)


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    return true_range(df).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()


def adx(df: pd.DataFrame, n: int = 14) -> pd.Series:
    up = df["high"].diff()
    dn = -df["low"].diff()
    plus_dm = np.where((up > dn) & (up > 0), up, 0.0)
    minus_dm = np.where((dn > up) & (dn > 0), dn, 0.0)
    tr = true_range(df).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    pdi = 100 * pd.Series(plus_dm, index=df.index).ewm(alpha=1 / n, adjust=False, min_periods=n).mean() / tr
    mdi = 100 * pd.Series(minus_dm, index=df.index).ewm(alpha=1 / n, adjust=False, min_periods=n).mean() / tr
    dx = 100 * (pdi - mdi).abs() / (pdi + mdi).replace(0, np.nan)
    return dx.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()


def macd(close: pd.Series, fast=12, slow=26, sig=9) -> tuple[pd.Series, pd.Series]:
    m = ema(close, fast) - ema(close, slow)
    return m, ema(m, sig)


def bollinger(close: pd.Series, n=20, k=2.0) -> tuple[pd.Series, pd.Series, pd.Series]:
    mid = sma(close, n)
    sd = close.rolling(n, min_periods=n).std(ddof=0)
    return mid - k * sd, mid, mid + k * sd


def roc(close: pd.Series, n: int) -> pd.Series:
    return close / close.shift(n) - 1


def realized_vol(close: pd.Series, n: int = 20) -> pd.Series:
    return np.log(close).diff().rolling(n, min_periods=n).std(ddof=0)


def zscore(s: pd.Series, n: int) -> pd.Series:
    m = s.rolling(n, min_periods=n).mean()
    sd = s.rolling(n, min_periods=n).std(ddof=0)
    return (s - m) / sd.replace(0, np.nan)


def donchian_high(high: pd.Series, n: int) -> pd.Series:
    """Highest high of the PREVIOUS n bars (excludes the current bar)."""
    return high.shift(1).rolling(n, min_periods=n).max()


def donchian_low(low: pd.Series, n: int) -> pd.Series:
    return low.shift(1).rolling(n, min_periods=n).min()


def pct_rank(s: pd.Series, n: int) -> pd.Series:
    """Percentile rank of the current value within the trailing n values (inclusive)."""
    return s.rolling(n, min_periods=n).apply(lambda a: (a <= a[-1]).mean(), raw=True)


# ---------------- intraday, session-anchored ----------------

def session_vwap(df: pd.DataFrame) -> pd.Series:
    tp = (df["high"] + df["low"] + df["close"]) / 3
    pv = (tp * df["volume"]).groupby(df["session"]).cumsum()
    vv = df["volume"].groupby(df["session"]).cumsum()
    return pv / vv.replace(0, np.nan)


def session_vwap_std(df: pd.DataFrame, vwap: pd.Series) -> pd.Series:
    tp = (df["high"] + df["low"] + df["close"]) / 3
    dev2 = ((tp - vwap) ** 2 * df["volume"]).groupby(df["session"]).cumsum()
    vv = df["volume"].groupby(df["session"]).cumsum()
    return np.sqrt(dev2 / vv.replace(0, np.nan))


def bar_of_day(df: pd.DataFrame) -> pd.Series:
    return df.groupby("session").cumcount()


def opening_range(df: pd.DataFrame, n_bars: int) -> tuple[pd.Series, pd.Series]:
    """OR high/low known only AFTER the first n_bars of the session complete; NaN before."""
    bod = bar_of_day(df)
    in_or = bod < n_bars
    h = df["high"].where(in_or).groupby(df["session"]).cummax()
    lo = df["low"].where(in_or).groupby(df["session"]).cummin()
    h = h.groupby(df["session"]).ffill()
    lo = lo.groupby(df["session"]).ffill()
    known = bod >= n_bars - 1  # value is final at the close of bar n_bars-1
    return h.where(known), lo.where(known)


def relative_volume_tod(df: pd.DataFrame, lookback_sessions: int = 20) -> pd.Series:
    """Bar volume / mean volume at the same bar-of-day over PRIOR sessions."""
    bod = bar_of_day(df)
    vol = df["volume"]
    prior_mean = vol.groupby(bod).transform(
        lambda s: s.shift(1).rolling(lookback_sessions, min_periods=5).mean())
    return vol / prior_mean.replace(0, np.nan)


def session_high_so_far(df: pd.DataFrame) -> pd.Series:
    return df["high"].groupby(df["session"]).cummax()


def session_low_so_far(df: pd.DataFrame) -> pd.Series:
    return df["low"].groupby(df["session"]).cummin()


def session_open(df: pd.DataFrame) -> pd.Series:
    return df["open"].groupby(df["session"]).transform("first")


def prev_session_close(df: pd.DataFrame) -> pd.Series:
    last = df.groupby("session")["close"].last().shift(1)
    return df["session"].map(last)
