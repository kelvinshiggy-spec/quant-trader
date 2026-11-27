"""Shared feature engineering (computed once per symbol, causal)."""
from __future__ import annotations

import numpy as np
import pandas as pd

from .. import indicators as ind
from ..markettime import session


def intraday_features(df: pd.DataFrame, daily: pd.DataFrame | None = None,
                      bench: pd.DataFrame | None = None, bar_minutes: int = 5) -> pd.DataFrame:
    """df: RTH bars with `session`. daily: daily bars for the same symbol (ET-date index).
    bench: intraday features of the benchmark (SPY) for relative-strength context."""
    f = df.copy()
    c = f["close"]
    f["atr"] = ind.atr(f, 14)
    f["atr_pct"] = f["atr"] / c
    f["ema9"] = ind.ema(c, 9)
    f["ema20"] = ind.ema(c, 20)
    f["ema50"] = ind.ema(c, 50)
    f["rsi14"] = ind.rsi(c, 14)
    f["rsi2"] = ind.rsi(c, 2)
    f["adx"] = ind.adx(f, 14)
    f["roc6"] = ind.roc(c, 6)
    f["vwap"] = ind.session_vwap(f)
    f["vwap_sd"] = ind.session_vwap_std(f, f["vwap"])
    f["bod"] = ind.bar_of_day(f)
    f["minutes"] = f["bod"] * bar_minutes
    f["relvol"] = ind.relative_volume_tod(f, 20)
    f["day_open"] = ind.session_open(f)
    f["prev_close"] = ind.prev_session_close(f)
    f["gap"] = f["day_open"] / f["prev_close"] - 1
    f["day_high"] = ind.session_high_so_far(f)
    f["day_low"] = ind.session_low_so_far(f)
    bl, bm, bu = ind.bollinger(c, 20, 2.0)
    f["bb_lo"], f["bb_mid"], f["bb_hi"] = bl, bm, bu
    f["bb_width"] = (bu - bl) / bm
    f["bbw_rank"] = ind.pct_rank(f["bb_width"], 120)
    f["hh12"] = ind.donchian_high(f["high"], 12)
    f["rng"] = f["high"] - f["low"]
    f["close_pos"] = (c - f["low"]) / f["rng"].replace(0, np.nan)
    for n in (1, 3, 6):  # 5, 15, 30 minute opening ranges on 5-min bars
        h, lo = ind.opening_range(f, n)
        f[f"orh{n}"], f[f"orl{n}"] = h, lo
    # bars remaining in session (known from the calendar, not from future data)
    closes = {s: session(s)[1] for s in pd.unique(f["session"]) if session(s)}
    close_ts = pd.to_datetime(f["session"].map(closes), utc=True)
    bar_end = f.index + pd.Timedelta(minutes=bar_minutes)
    f["bars_left"] = ((close_ts - bar_end).dt.total_seconds() // (bar_minutes * 60)).astype(float)
    # adv: average daily dollar volume of PRIOR sessions
    dv = (f["close"] * f["volume"]).groupby(f["session"]).sum()
    adv = dv.shift(1).rolling(20, min_periods=5).mean()
    f["adv"] = f["session"].map(adv)
    if daily is not None and not daily.empty:
        d = daily.copy()
        d.index = pd.Index([x.date() if hasattr(x, "date") else x for x in d.index])
        dc = d["close"]
        ctx = pd.DataFrame({
            "d_sma20": ind.sma(dc, 20), "d_sma50": ind.sma(dc, 50), "d_sma200": ind.sma(dc, 200),
            "d_close": dc, "d_atr": ind.atr(d, 14), "d_ret20": ind.roc(dc, 20),
        }).shift(1)  # only the PREVIOUS completed day is known intraday
        ctx = ctx[~ctx.index.duplicated()]
        for col in ctx.columns:
            f[col] = f["session"].map(ctx[col])
    if bench is not None and not bench.empty:
        b = bench[["close", "vwap", "roc6", "day_open"]].rename(columns=lambda x: "spy_" + x)
        f = f.join(b, how="left")
        f["spy_day_ret"] = f["spy_close"] / f["spy_day_open"] - 1
        f["rs6"] = f["roc6"] - f["spy_roc6"]
    return f


def daily_features(d: pd.DataFrame) -> pd.DataFrame:
    f = d.copy()
    c = f["close"]
    f["atr"] = ind.atr(f, 14)
    f["sma5"] = ind.sma(c, 5)
    f["sma20"] = ind.sma(c, 20)
    f["sma50"] = ind.sma(c, 50)
    f["sma100"] = ind.sma(c, 100)
    f["sma200"] = ind.sma(c, 200)
    f["rsi2"] = ind.rsi(c, 2)
    f["rsi14"] = ind.rsi(c, 14)
    f["adx"] = ind.adx(f, 14)
    f["hh20"] = ind.donchian_high(f["high"], 20)
    f["ll10"] = ind.donchian_low(f["low"], 10)
    f["ret63"] = ind.roc(c, 63)
    f["rv20"] = ind.realized_vol(c, 20)
    f["dollar_vol20"] = (c * f["volume"]).rolling(20, min_periods=5).mean()
    # round 2 features
    f["ret126"] = ind.roc(c, 126)
    f["ret252"] = ind.roc(c, 252)
    f["hh252"] = ind.donchian_high(f["high"], 252)          # prior 252-session high (shifted, excludes today)
    rng = (f["high"] - f["low"]).replace(0, np.nan)
    f["ibs"] = ((c - f["low"]) / rng).fillna(0.5)            # internal bar strength
    f["prev_high"] = f["high"].shift(1)
    cal = month_position([x.tz_convert("America/New_York").date() if getattr(x, "tzinfo", None) else x.date()
                          for x in f.index])
    f["td_of_month"] = cal[0]
    f["td_to_month_end"] = cal[1]
    return f


def month_position(dates) -> tuple[np.ndarray, np.ndarray]:
    """(1-based trading-day index within the month, trading days remaining after this one in the month),
    from the exchange calendar, which is published in advance, so it is not look-ahead."""
    from ..markettime import trading_days
    cache: dict = {}
    of, to_end = np.zeros(len(dates)), np.zeros(len(dates))
    for i, d in enumerate(dates):
        key = (d.year, d.month)
        if key not in cache:
            first = d.replace(day=1)
            last = (pd.Timestamp(first) + pd.offsets.MonthEnd(0)).date()
            cache[key] = trading_days(first, last)
        days = cache[key]
        if d in days:
            k = days.index(d)
            of[i], to_end[i] = k + 1, len(days) - 1 - k
        else:
            of[i], to_end[i] = np.nan, np.nan
    return of, to_end


def daily_panel(raw: dict[str, pd.DataFrame], min_len: int = 0) -> dict[str, pd.DataFrame]:
    """Daily features for every symbol plus the cross-sectional columns, shared by research and the live
    engine so both compute identical signals. Ranks use only values known at each close."""
    from ..universe import ASSET_CLASS_SET, SECTOR_ETFS
    out = {s: daily_features(d) for s, d in raw.items() if d is not None and len(d) > min_len}
    rets = pd.DataFrame({s: out[s]["ret63"] for s in SECTOR_ETFS if s in out})
    ranks = rets.rank(axis=1, ascending=False)
    for s in rets.columns:
        out[s]["sector_rank"] = ranks[s]
    for lb in (126, 252):
        r = pd.DataFrame({s: out[s][f"ret{lb}"] for s in ASSET_CLASS_SET if s in out})
        rk = r.rank(axis=1, ascending=False)
        for s in r.columns:
            out[s][f"ac_rank{lb}"] = rk[s]
    for s, f in out.items():
        f["session"] = [x.tz_convert("America/New_York").date() for x in f.index]
        f["adv"] = f["dollar_vol20"].shift(1)
    return out


def hourly_crypto_features(d: pd.DataFrame) -> pd.DataFrame:
    f = d.copy()
    c = f["close"]
    f["atr"] = ind.atr(f, 24)
    f["ema24"] = ind.ema(c, 24)
    f["ema72"] = ind.ema(c, 72)
    f["ema200"] = ind.ema(c, 200)
    f["ema480"] = ind.ema(c, 480)  # ~20 days
    f["rsi14"] = ind.rsi(c, 14)
    f["hh48"] = ind.donchian_high(f["high"], 48)
    f["vol_rel"] = f["volume"] / f["volume"].shift(1).rolling(72, min_periods=24).mean()
    f["adv"] = (c * f["volume"]).rolling(24 * 20, min_periods=48).mean() * 24
    return f
