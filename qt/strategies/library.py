"""Competing, independent long-only strategies (no shorting initially).

Each has a deliberately SMALL parameter grid to limit overfitting. Every
strategy must earn eligibility through walk-forward + holdout validation in
the research engine; inclusion here is not a claim that it works.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .base import ExitPlan, Strategy, frame
from ..universe import ASSET_CLASS_SET, DAILY_UNIVERSE, INDEX_ETFS, LEGACY_DAILY, SECTOR_ETFS

MIN_ADV = 5e7          # $ average daily dollar volume for intraday trading
CUTOFF_BARS = 6        # no new intraday entries in the last 30 minutes


def _intraday_ok(f: pd.DataFrame, min_minutes: int = 5) -> pd.Series:
    return (f["bars_left"] >= CUTOFF_BARS) & (f["minutes"] >= min_minutes) & (f["adv"] >= MIN_ADV)


# ---------------- A. Momentum ----------------
@dataclass
class Momentum(Strategy):
    name: str = "A_momentum"
    letter: str = "A"
    description: str = "Directional move with acceleration, volume and price above VWAP / rising EMAs."
    defaults: dict = field(default_factory=lambda: {"relvol": 1.5, "roc_atr": 2.0, "trail": 2.0})
    grid: dict = field(default_factory=lambda: {"relvol": [1.5, 2.5], "roc_atr": [1.5, 2.5]})

    def exit_plan(self):
        return ExitPlan(trail_atr=self.p("trail"), trail_activate_r=1.0)

    def signals(self, f):
        move = f["roc6"] * f["close"]  # 30-minute $ move
        cond = (_intraday_ok(f, 15) & (move > self.p("roc_atr") * f["atr"]) & (f["close"] > f["vwap"])
                & (f["ema9"] > f["ema20"]) & (f["relvol"] > self.p("relvol")) & (f["rsi14"] < 80))
        stop = f["close"] - 1.5 * f["atr"]
        return frame(f, cond, stop, exit_sig=f["close"] < f["ema20"], strength=move / f["atr"])

    def explain(self, r):
        return {"30m move (ATR)": round(r["roc6"] * r["close"] / r["atr"], 2), "above VWAP": bool(r["close"] > r["vwap"]),
                "EMA9>EMA20": bool(r["ema9"] > r["ema20"]), "relative volume": round(r["relvol"], 2)}


# ---------------- B. Opening range breakout ----------------
@dataclass
class OpeningRangeBreakout(Strategy):
    name: str = "B_orb"
    letter: str = "B"
    description: str = "Break of the 5/15/30-minute opening range with volume; failed breakouts exit."
    defaults: dict = field(default_factory=lambda: {"or_bars": 3, "stop": "mid", "relvol": 1.2})
    grid: dict = field(default_factory=lambda: {"or_bars": [1, 3, 6], "stop": ["low", "mid"]})

    def exit_plan(self):
        return ExitPlan(trail_atr=2.0, trail_activate_r=1.0)

    def signals(self, f):
        n = self.p("or_bars")
        h, lo = f[f"orh{n}"], f[f"orl{n}"]
        prev_close = f["close"].groupby(f["session"]).shift(1)
        first_break = (f["close"] > h) & ~(prev_close > h)
        already = first_break.groupby(f["session"]).cumsum() > 1  # only the first breakout of the day
        cond = (_intraday_ok(f, 5 * n) & first_break & ~already & (f["minutes"] <= 150)
                & (f["relvol"] > self.p("relvol")) & (f["close"] > f["vwap"]))
        stop = lo if self.p("stop") == "low" else (h + lo) / 2
        stop = np.minimum(stop, f["close"] - 0.5 * f["atr"])
        failed = f["close"] < h - 0.25 * f["atr"]
        return frame(f, cond, stop, exit_sig=failed, strength=f["relvol"])


# ---------------- C1. VWAP reclaim continuation ----------------
@dataclass
class VWAPReclaim(Strategy):
    name: str = "C1_vwap_reclaim"
    letter: str = "C"
    description: str = "In an up day, price dips below VWAP then closes back above it with volume."
    defaults: dict = field(default_factory=lambda: {"relvol": 1.0, "lookback": 6})
    grid: dict = field(default_factory=lambda: {"relvol": [1.0, 1.5], "lookback": [3, 6]})

    def exit_plan(self):
        return ExitPlan(trail_atr=1.5, trail_activate_r=1.0)

    def signals(self, f):
        lb = self.p("lookback")
        was_below = (f["close"] < f["vwap"]).groupby(f["session"]).transform(
            lambda s: s.shift(1).rolling(lb, min_periods=1).max()).fillna(0).astype(bool)
        up_day = (f["close"] > f["day_open"]) & (f["ema20"] > f["ema20"].shift(3))
        cond = (_intraday_ok(f, 30) & was_below & (f["close"] > f["vwap"]) & up_day
                & (f["relvol"] > self.p("relvol")) & (f["close"] > f["open"]))
        swing_low = f["low"].rolling(lb + 1, min_periods=1).min()
        stop = np.minimum(swing_low - 0.1 * f["atr"], f["close"] - 0.75 * f["atr"])
        return frame(f, cond, stop, exit_sig=f["close"] < f["vwap"] - 0.5 * f["atr"],
                     strength=(f["close"] - f["vwap"]) / f["atr"])


# ---------------- C2. VWAP reversion ----------------
@dataclass
class VWAPReversion(Strategy):
    name: str = "C2_vwap_reversion"
    letter: str = "C"
    description: str = "Stretch far below VWAP with oversold RSI when the bigger trend is not down; target VWAP."
    defaults: dict = field(default_factory=lambda: {"k": 2.5, "rsi": 25})
    grid: dict = field(default_factory=lambda: {"k": [2.0, 3.0], "rsi": [20, 30]})

    def exit_plan(self):
        return ExitPlan(max_bars=12)

    def signals(self, f):
        stretched = f["close"] < f["vwap"] - self.p("k") * f["vwap_sd"]
        trend_ok = f.get("d_sma50", pd.Series(np.nan, index=f.index))
        trend_ok = (f["prev_close"] > trend_ok) | trend_ok.isna()
        cond = _intraday_ok(f, 30) & stretched & (f["rsi14"] < self.p("rsi")) & trend_ok & (f["close"] > f["open"])
        stop = f["close"] - 1.5 * f["atr"]
        return frame(f, cond, stop, target=f["vwap"], strength=(f["vwap"] - f["close"]) / f["atr"])


# ---------------- D. Mean reversion (Bollinger / RSI2) ----------------
@dataclass
class MeanReversion(Strategy):
    name: str = "D_mean_reversion"
    letter: str = "D"
    description: str = "Close below the lower Bollinger band with RSI(2) washout, only in daily uptrends; target mid-band."
    defaults: dict = field(default_factory=lambda: {"band": "bb_lo", "rsi2": 5})
    grid: dict = field(default_factory=lambda: {"rsi2": [5, 10]})

    def exit_plan(self):
        return ExitPlan(max_bars=18)

    def signals(self, f):
        uptrend = f.get("d_sma50", pd.Series(np.nan, index=f.index))
        uptrend = (f["prev_close"] > uptrend).fillna(False)
        cond = (_intraday_ok(f, 30) & (f["close"] < f["bb_lo"]) & (f["rsi2"] < self.p("rsi2")) & uptrend
                & (f["adx"] < 30))
        stop = f["close"] - 1.5 * f["atr"]
        return frame(f, cond, stop, target=f["bb_mid"], strength=(f["bb_mid"] - f["close"]) / f["atr"])


# ---------------- E. Trend following (daily, multi-day) ----------------
@dataclass
class TrendFollowing(Strategy):
    name: str = "E_trend_following"
    letter: str = "E"
    timeframe: str = "1Day"
    overnight: bool = True
    description: str = "Donchian 20-day breakout in a SMA50>SMA200 uptrend with ADX; chandelier trail; exit below SMA50."
    universe: tuple = tuple(LEGACY_DAILY)
    defaults: dict = field(default_factory=lambda: {"adx": 20, "trail": 3.0})
    grid: dict = field(default_factory=lambda: {"adx": [15, 25], "trail": [2.5, 3.5]})

    def exit_plan(self):
        return ExitPlan(trail_atr=self.p("trail"), trail_activate_r=0.0, flatten_eod=False)

    def signals(self, f):
        cond = ((f["close"] > f["hh20"]) & (f["sma50"] > f["sma200"]) & (f["adx"] > self.p("adx"))
                & (f["dollar_vol20"] > MIN_ADV))
        stop = f["close"] - 2.0 * f["atr"]
        return frame(f, cond, stop, exit_sig=f["close"] < f["sma50"], strength=f["adx"])


# ---------------- F. Relative strength (intraday vs SPY) ----------------
@dataclass
class RelativeStrength(Strategy):
    name: str = "F_relative_strength"
    letter: str = "F"
    description: str = "Outperforming SPY over 30 min while both are above VWAP and volume is elevated."
    defaults: dict = field(default_factory=lambda: {"rs": 0.004, "relvol": 1.2})
    grid: dict = field(default_factory=lambda: {"rs": [0.003, 0.006], "relvol": [1.0, 1.5]})

    def exit_plan(self):
        return ExitPlan(trail_atr=2.0, trail_activate_r=1.0)

    def signals(self, f):
        if "rs6" not in f:
            return frame(f, False, np.nan)
        cond = (_intraday_ok(f, 30) & (f["rs6"] > self.p("rs")) & (f["close"] > f["vwap"])
                & (f["spy_close"] > f["spy_vwap"]) & (f["relvol"] > self.p("relvol")))
        stop = f["close"] - 1.5 * f["atr"]
        return frame(f, cond, stop, exit_sig=(f["rs6"] < 0) & (f["close"] < f["vwap"]), strength=f["rs6"] * 100)


# ---------------- G. Volume breakout ----------------
@dataclass
class VolumeBreakout(Strategy):
    name: str = "G_volume_breakout"
    letter: str = "G"
    description: str = "Unusual volume with a wide bar closing near its high, breaking the prior hour's high."
    defaults: dict = field(default_factory=lambda: {"relvol": 3.0, "range_atr": 1.5})
    grid: dict = field(default_factory=lambda: {"relvol": [2.5, 4.0], "range_atr": [1.2, 2.0]})

    def exit_plan(self):
        return ExitPlan(trail_atr=2.0, trail_activate_r=1.0, max_bars=24)

    def signals(self, f):
        cond = (_intraday_ok(f, 15) & (f["relvol"] > self.p("relvol")) & (f["rng"] > self.p("range_atr") * f["atr"].shift(1))
                & (f["close_pos"] > 0.75) & (f["close"] > f["hh12"]))
        stop = np.minimum(f["low"], f["close"] - 0.5 * f["atr"])
        return frame(f, cond, stop, strength=f["relvol"])


# ---------------- H. Pullback continuation ----------------
@dataclass
class PullbackContinuation(Strategy):
    name: str = "H_pullback"
    letter: str = "H"
    description: str = "Strong intraday trend (EMA stack, ADX); pullback to EMA20 then resumption."
    defaults: dict = field(default_factory=lambda: {"adx": 25})
    grid: dict = field(default_factory=lambda: {"adx": [20, 30]})

    def exit_plan(self):
        return ExitPlan(trail_atr=2.0, trail_activate_r=1.0)

    def signals(self, f):
        stack = (f["ema9"] > f["ema20"]) & (f["ema20"] > f["ema50"])
        touched = f["low"].shift(1) <= f["ema20"].shift(1)
        cond = (_intraday_ok(f, 60) & stack & (f["adx"] > self.p("adx")) & touched & (f["close"] > f["high"].shift(1))
                & (f["close"] > f["vwap"]))
        stop = np.minimum(f["ema50"], f["close"] - 1.0 * f["atr"])
        return frame(f, cond, stop, exit_sig=f["close"] < f["ema50"], strength=f["adx"])


# ---------------- I. Intraday reversal ----------------
@dataclass
class IntradayReversal(Strategy):
    name: str = "I_intraday_reversal"
    letter: str = "I"
    description: str = "After a large selloff from the day high and oversold RSI, a bullish reversal bar with volume; target VWAP."
    defaults: dict = field(default_factory=lambda: {"drop_atr": 4.0, "rsi": 25})
    grid: dict = field(default_factory=lambda: {"drop_atr": [3.0, 5.0], "rsi": [25, 35]})

    def exit_plan(self):
        return ExitPlan(max_bars=24, breakeven_r=1.0)

    def signals(self, f):
        drop = (f["day_high"] - f["close"]) / f["atr"]
        rev_bar = (f["close"] > f["open"]) & (f["close"] > f["high"].shift(1))
        cond = (_intraday_ok(f, 30) & (drop > self.p("drop_atr")) & (f["rsi14"].shift(1) < self.p("rsi")) & rev_bar
                & (f["relvol"] > 1.5) & (f["close"] < f["vwap"]))
        stop = f["day_low"] - 0.2 * f["atr"]
        return frame(f, cond, stop, target=f["vwap"], strength=drop)


# ---------------- J. Volatility expansion ----------------
@dataclass
class VolatilityExpansion(Strategy):
    name: str = "J_vol_expansion"
    letter: str = "J"
    description: str = "Bollinger squeeze (low bandwidth percentile) resolving upward with volume."
    defaults: dict = field(default_factory=lambda: {"squeeze": 0.2, "relvol": 1.5})
    grid: dict = field(default_factory=lambda: {"squeeze": [0.1, 0.25], "relvol": [1.2, 2.0]})

    def exit_plan(self):
        return ExitPlan(trail_atr=2.0, trail_activate_r=1.0)

    def signals(self, f):
        was_squeezed = f["bbw_rank"].shift(1).rolling(6, min_periods=1).min() < self.p("squeeze")
        cond = (_intraday_ok(f, 30) & was_squeezed & (f["close"] > f["bb_hi"]) & (f["relvol"] > self.p("relvol")))
        stop = np.minimum(f["bb_mid"], f["close"] - 0.75 * f["atr"])
        return frame(f, cond, stop, exit_sig=f["close"] < f["bb_mid"], strength=f["relvol"])


# ---------------- K. Sector rotation (daily, multi-day) ----------------
@dataclass
class SectorRotation(Strategy):
    name: str = "K_sector_rotation"
    letter: str = "K"
    timeframe: str = "1Day"
    overnight: bool = True
    description: str = "Hold sector ETFs ranked top-N by 3-month momentum and above their SMA100; exit when rank drops."
    universe: tuple = tuple(SECTOR_ETFS)
    defaults: dict = field(default_factory=lambda: {"top": 2})
    grid: dict = field(default_factory=lambda: {"top": [2, 3]})

    def exit_plan(self):
        return ExitPlan(flatten_eod=False)

    def signals(self, f):
        if "sector_rank" not in f:
            return frame(f, False, np.nan)
        top = self.p("top")
        in_top = (f["sector_rank"] <= top) & (f["close"] > f["sma100"])
        newly = in_top & ~in_top.shift(1, fill_value=False)
        stop = f["close"] - 3.0 * f["atr"]
        return frame(f, newly, stop, exit_sig=(f["sector_rank"] > top + 1) | (f["close"] < f["sma100"]),
                     strength=f["ret63"])


# ---------------- L. Crypto (hourly, 24/7) ----------------
@dataclass
class CryptoTrend(Strategy):
    name: str = "L_crypto_trend"
    letter: str = "L"
    timeframe: str = "1Hour"
    asset_class: str = "crypto"
    overnight: bool = True
    description: str = "48-hour breakout in an EMA24>EMA72 uptrend; wide ATR trail to survive 0.25% taker fees."
    defaults: dict = field(default_factory=lambda: {"trail": 3.0, "volrel": 1.0})
    grid: dict = field(default_factory=lambda: {"trail": [2.5, 4.0], "volrel": [1.0, 1.5]})

    def exit_plan(self):
        return ExitPlan(trail_atr=self.p("trail"), trail_activate_r=0.5, flatten_eod=False, max_bars=24 * 7)

    def signals(self, f):
        cond = ((f["close"] > f["hh48"]) & (f["ema24"] > f["ema72"]) & (f["close"] > f["ema200"])
                & (f["vol_rel"] > self.p("volrel")))
        stop = f["close"] - 2.5 * f["atr"]
        return frame(f, cond, stop, exit_sig=f["close"] < f["ema72"], strength=(f["close"] / f["hh48"] - 1) * 100)


@dataclass
class BTC15mTrend(CryptoTrend):
    """Owner request 2026-11-27 ("BTC 15 min"). Same rules as L_crypto_trend on 15-minute bars (windows in bars:
    12-hour breakout, 6h/18h EMAs). Not tuned on any data."""
    name: str = "L3_btc_15m_trend"
    timeframe: str = "15Min"
    universe: tuple = ("BTC/USD",)
    description: str = "BTC 15-minute: 12-hour breakout in a 6h>18h EMA uptrend, ATR stop and trail; 0.25% taker fees."

    def exit_plan(self):
        return ExitPlan(trail_atr=self.p("trail"), trail_activate_r=0.5, flatten_eod=False, max_bars=96)


@dataclass
class CryptoMeanReversion(Strategy):
    name: str = "L2_crypto_reversion"
    letter: str = "L"
    timeframe: str = "1Hour"
    asset_class: str = "crypto"
    overnight: bool = True
    version: str = "1.1.0"  # 1.0.0 had contradictory filters (RSI<25 above hourly EMA200): ~0 signals
    description: str = "Hourly RSI washout while the ~20-day EMA is rising; target EMA24."
    defaults: dict = field(default_factory=lambda: {"rsi": 25})
    grid: dict = field(default_factory=lambda: {"rsi": [20, 30]})

    def exit_plan(self):
        return ExitPlan(max_bars=48, flatten_eod=False)

    def signals(self, f):
        cond = (f["rsi14"].shift(1) < self.p("rsi")) & (f["ema480"] > f["ema480"].shift(24)) & (f["close"] > f["open"])
        stop = f["close"] - 2.5 * f["atr"]
        return frame(f, cond, stop, target=f["ema24"], strength=50 - f["rsi14"])


# ---------------- M. Swing (daily, multi-day) ----------------
@dataclass
class SwingPullback(Strategy):
    name: str = "M_swing_rsi2"
    letter: str = "M"
    timeframe: str = "1Day"
    overnight: bool = True
    description: str = "Daily RSI(2) pullback above SMA200; exit on close above SMA5 or after N days."
    universe: tuple = tuple(LEGACY_DAILY)
    defaults: dict = field(default_factory=lambda: {"rsi2": 10, "days": 10})
    grid: dict = field(default_factory=lambda: {"rsi2": [5, 15]})

    def exit_plan(self):
        return ExitPlan(max_bars=self.p("days"), flatten_eod=False)

    def signals(self, f):
        cond = (f["close"] > f["sma200"]) & (f["rsi2"] < self.p("rsi2")) & (f["dollar_vol20"] > MIN_ADV)
        stop = f["close"] - 2.5 * f["atr"]
        return frame(f, cond, stop, exit_sig=f["close"] > f["sma5"], strength=self.p("rsi2") - f["rsi2"])


# ---------------- N. Market intraday momentum (Gao, Han, Li & Zhou, 2018, JFE) ----------------
@dataclass
class IntradayMomentumLastHalfHour(Strategy):
    name: str = "N_intraday_momentum"
    letter: str = "N"
    description: str = ("Research-backed: the first half-hour return (incl. overnight) predicts the last half-hour "
                        "return for index ETFs. Buy ~30 min before close if positive; flat at the close.")
    defaults: dict = field(default_factory=lambda: {"min_ret": 0.0})
    grid: dict = field(default_factory=lambda: {"min_ret": [0.0, 0.0025]})

    def exit_plan(self):
        return ExitPlan(flatten_eod=True)

    def signals(self, f):
        # return from previous close to the end of the first 30 minutes (bar 5 close)
        first30 = f["close"].where(f["bod"] == 5).groupby(f["session"]).transform("max")
        r1 = first30 / f["prev_close"] - 1
        # entry decided at the close of the bar ending 30 min before the close; executes next bar open
        at_time = f["bars_left"] == 6
        cond = at_time & (r1 > self.p("min_ret")) & (f["adv"] >= 1e9) & (f["bod"] > 5)
        stop = f["close"] - 2.0 * f["atr"]
        return frame(f, cond, stop, strength=r1 * 100)


# ======== Round 2 (pre-registered 2026-11-27): daily / monthly hypotheses from published research ========
MIN_ADV_ETF = 2e7


def _month_end(f):
    return f["td_to_month_end"] == 0


# ---------------- O. Time-series momentum (Moskowitz, Ooi & Pedersen, 2012, JFE) ----------------
@dataclass
class TimeSeriesMomentum(Strategy):
    name: str = "O_tsmom"
    letter: str = "O"
    timeframe: str = "1Day"
    overnight: bool = True
    research_round: int = 2
    universe: tuple = tuple(ASSET_CLASS_SET)
    description: str = ("Monthly: hold each asset-class ETF whose trailing 6/12-month return is positive; "
                        "re-check at each month end; wide ATR stop.")
    defaults: dict = field(default_factory=lambda: {"lookback": 252, "stop_atr": 4.0})
    grid: dict = field(default_factory=lambda: {"lookback": [126, 252], "stop_atr": [3.0, 5.0]})

    def exit_plan(self):
        return ExitPlan(flatten_eod=False)

    def signals(self, f):
        r = f[f"ret{self.p('lookback')}"]
        me = _month_end(f)
        cond = me & (r > 0) & (f["dollar_vol20"] > MIN_ADV_ETF)
        stop = f["close"] - self.p("stop_atr") * f["atr"]
        return frame(f, cond, stop, exit_sig=me & (r < 0), strength=r / f["rv20"].replace(0, np.nan))


# ---------------- P. Dual momentum rotation (Antonacci, 2014) ----------------
@dataclass
class DualMomentum(Strategy):
    name: str = "P_dual_momentum"
    letter: str = "P"
    timeframe: str = "1Day"
    overnight: bool = True
    research_round: int = 2
    universe: tuple = tuple(ASSET_CLASS_SET)
    description: str = ("Monthly: hold the top-N asset-class ETFs by 6/12-month return, only if that return is "
                        "positive (absolute momentum); rotate at month end.")
    defaults: dict = field(default_factory=lambda: {"top": 2, "lookback": 252})
    grid: dict = field(default_factory=lambda: {"top": [1, 2], "lookback": [126, 252]})

    def exit_plan(self):
        return ExitPlan(flatten_eod=False)

    def signals(self, f):
        lb = self.p("lookback")
        if f"ac_rank{lb}" not in f:
            return frame(f, False, np.nan)
        r, rk, top = f[f"ret{lb}"], f[f"ac_rank{lb}"], self.p("top")
        me = _month_end(f)
        cond = me & (rk <= top) & (r > 0) & (f["dollar_vol20"] > MIN_ADV_ETF)
        stop = f["close"] - 4.0 * f["atr"]
        return frame(f, cond, stop, exit_sig=me & ((rk > top + 1) | (r < 0)), strength=r)


# ---------------- Q. Turn of the month (Lakonishok & Smidt, 1988; McConnell & Xu, 2008) ----------------
@dataclass
class TurnOfMonth(Strategy):
    name: str = "Q_turn_of_month"
    letter: str = "Q"
    timeframe: str = "1Day"
    overnight: bool = True
    research_round: int = 2
    universe: tuple = tuple(INDEX_ETFS)
    description: str = ("Buy index ETFs at the open of the last trading day of the month; sell after the first "
                        "few trading days of the new month.")
    defaults: dict = field(default_factory=lambda: {"hold": 3, "trend": 0})
    grid: dict = field(default_factory=lambda: {"hold": [3, 5], "trend": [0, 1]})

    def exit_plan(self):
        return ExitPlan(max_bars=self.p("hold"), flatten_eod=False)

    def signals(self, f):
        cond = (f["td_to_month_end"] == 1) & (f["dollar_vol20"] > MIN_ADV)
        if self.p("trend"):
            cond = cond & (f["close"] > f["sma200"])
        stop = f["close"] - 3.0 * f["atr"]
        return frame(f, cond, stop, strength=pd.Series(1.0, index=f.index))


# ---------------- R. Internal bar strength reversion (Pagonidis, 2013) ----------------
@dataclass
class IBSReversion(Strategy):
    name: str = "R_ibs_reversion"
    letter: str = "R"
    timeframe: str = "1Day"
    overnight: bool = True
    research_round: int = 2
    universe: tuple = tuple(INDEX_ETFS + SECTOR_ETFS)
    description: str = ("Buy an index or sector ETF that closed near its daily low (low IBS) in a long-term "
                        "uptrend; sell on a close above the prior day's high or after 5 days.")
    defaults: dict = field(default_factory=lambda: {"ibs": 0.15})
    grid: dict = field(default_factory=lambda: {"ibs": [0.1, 0.2]})

    def exit_plan(self):
        return ExitPlan(max_bars=5, flatten_eod=False)

    def signals(self, f):
        cond = (f["ibs"] < self.p("ibs")) & (f["close"] > f["sma200"]) & (f["dollar_vol20"] > MIN_ADV)
        stop = f["close"] - 2.5 * f["atr"]
        return frame(f, cond, stop, exit_sig=f["close"] > f["prev_high"], strength=self.p("ibs") - f["ibs"])


# ---------------- S. 52-week-high momentum (George & Hwang, 2004, JF) ----------------
@dataclass
class FiftyTwoWeekHigh(Strategy):
    name: str = "S_52w_high"
    letter: str = "S"
    timeframe: str = "1Day"
    overnight: bool = True
    research_round: int = 2
    universe: tuple = tuple(DAILY_UNIVERSE)
    description: str = ("Buy a fresh close above the prior 52-week high in a SMA50>SMA200 uptrend; ATR trailing "
                        "stop; exit on a close below SMA50.")
    defaults: dict = field(default_factory=lambda: {"trail": 3.0})
    grid: dict = field(default_factory=lambda: {"trail": [3.0, 4.0]})

    def exit_plan(self):
        return ExitPlan(trail_atr=self.p("trail"), trail_activate_r=0.0, flatten_eod=False)

    def signals(self, f):
        brk = (f["close"] > f["hh252"]) & ~(f["close"].shift(1) > f["hh252"].shift(1))
        cond = brk & (f["sma50"] > f["sma200"]) & (f["dollar_vol20"] > MIN_ADV)
        stop = f["close"] - 2.5 * f["atr"]
        return frame(f, cond, stop, exit_sig=f["close"] < f["sma50"], strength=f["ret126"])


ALL_STRATEGIES: list[type[Strategy]] = [
    Momentum, OpeningRangeBreakout, VWAPReclaim, VWAPReversion, MeanReversion, TrendFollowing,
    RelativeStrength, VolumeBreakout, PullbackContinuation, IntradayReversal, VolatilityExpansion,
    SectorRotation, CryptoTrend, CryptoMeanReversion, SwingPullback, IntradayMomentumLastHalfHour,
    TimeSeriesMomentum, DualMomentum, TurnOfMonth, IBSReversion, FiftyTwoWeekHigh,
]

BY_NAME = {cls().name: cls for cls in ALL_STRATEGIES}
# live-only (forward tested in its book and on paper; the research runner has no 15-minute crypto dataset)
BY_NAME[BTC15mTrend().name] = BTC15mTrend


def make(name: str, params: dict | None = None) -> Strategy:
    return BY_NAME[name](params=params or {})
