"""Scalping test (owner request 2026-11-27): 1-minute strategies on liquid stocks and on BTC/ETH.

Pre-registered before running (no parameter search; defaults only):
- SC1 vwap_pullback: price above session VWAP, EMA20 > EMA50, the bar dipped to EMA20 and closed up.
  Stop = lowest low of the last 3 bars (at least 1 ATR below), target 1.5R, max 15 bars, flat by the close.
- SC2 rsi2_scalp: above VWAP and EMA50 rising, RSI(2) < 5 on the previous bar, this bar closes up.
  Stop 2 ATR, exit when RSI(2) > 70, max 10 bars.
- SC3 orb_1min: first 15 minutes, close breaks the 5-minute opening-range high with relative volume > 1.5.
  Stop = max(OR low, close - 2 ATR), target 2R, max 30 bars.
Fills at the NEXT bar's open; stops checked on bar lows (gaps fill worse); stop before target in the same bar.
Costs: CostModel spread tier by ADV + 2 bps slippage per side for stocks (SEC/TAF on sells); crypto 0.25% taker
per side + spread + 5 bps slippage. Also at 2x costs.
Pass (same bar as the research runner, not loosened): dev 2024 >= 60 trades, PF >= 1.3, Holm p <= 0.05 across the
6 tests (3 strategies x stocks/crypto); holdout 2025+ >= 20 trades and PF >= 1.1; and still positive at 2x costs.

Run: python -m qt.research.scalping
"""
from __future__ import annotations

import datetime as dt
import json
import os
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .. import indicators as ind
from ..backtest.engine import BTConfig, run_backtest
from ..backtest.metrics import holm, trade_metrics, ttest_gt0
from ..costs import CostModel
from ..data.historical import HistoricalData, to_rth
from ..strategies.base import ExitPlan, Strategy, frame
from ..strategies.features import intraday_features

STOCKS = ["SPY", "QQQ", "NVDA", "TSLA", "AAPL", "AMD"]
CRYPTO = ["BTC/USD", "ETH/USD"]
DEV = (dt.date(2024, 1, 1), dt.date(2024, 12, 31))
HOLD = (dt.date(2025, 1, 1), None)


@dataclass
class SC1(Strategy):
    name: str = "SC1_vwap_pullback"
    timeframe: str = "1Min"
    defaults: dict = field(default_factory=dict)

    def exit_plan(self):
        return ExitPlan(max_bars=15)

    def signals(self, f):
        low3 = f["low"].rolling(3).min()
        stop = np.minimum(low3, f["close"] - f["atr"])
        cond = ((f["close"] > f["vwap"]) & (f["ema20"] > f["ema50"]) & (f["low"] <= f["ema20"])
                & (f["close"] > f["open"]) & (f["close"] > f["ema20"]))
        return frame(f, cond, stop, target=f["close"] + 1.5 * (f["close"] - stop))


@dataclass
class SC2(Strategy):
    name: str = "SC2_rsi2_scalp"
    timeframe: str = "1Min"
    defaults: dict = field(default_factory=dict)

    def exit_plan(self):
        return ExitPlan(max_bars=10)

    def signals(self, f):
        cond = ((f["close"] > f["vwap"]) & (f["ema50"] > f["ema50"].shift(10)) & (f["rsi2"].shift(1) < 5)
                & (f["close"] > f["open"]))
        return frame(f, cond, f["close"] - 2 * f["atr"], exit_sig=f["rsi2"] > 70)


@dataclass
class SC3(Strategy):
    name: str = "SC3_orb_1min"
    timeframe: str = "1Min"
    defaults: dict = field(default_factory=dict)

    def exit_plan(self):
        return ExitPlan(max_bars=30)

    def signals(self, f):
        cond = ((f["minutes"] >= 5) & (f["minutes"] < 15) & (f["close"] > f["or5h"])
                & (f["close"].shift(1) <= f["or5h"]) & (f["relvol"] > 1.5))
        stop = np.maximum(f["or5l"], f["close"] - 2 * f["atr"])
        return frame(f, cond, stop, target=f["close"] + 2 * (f["close"] - stop))


def stock_frames(hd, start, end):
    out = {}
    raw = hd.bars(STOCKS, "1Min", start, end)
    for s, d in raw.items():
        r = to_rth(d, 1)
        f = intraday_features(r, None, None, bar_minutes=1)
        h, lo = ind.opening_range(f, 5)
        f["or5h"], f["or5l"] = h, lo
        out[s] = f
    return out


def crypto_frames(hd, start, end):
    out = {}
    raw = hd.bars(CRYPTO, "1Min", start, end, asset_class="crypto")
    for s, d in raw.items():
        f = d.copy()
        f["session"] = f.index.tz_convert("America/New_York").date
        c = f["close"]
        f["atr"] = ind.atr(f, 14)
        f["ema20"], f["ema50"] = ind.ema(c, 20), ind.ema(c, 50)
        f["rsi2"] = ind.rsi(c, 2)
        f["vwap"] = ind.session_vwap(f)
        f["adv"] = (c * f["volume"]).rolling(1440 * 5, min_periods=600).mean() * 1440
        f["minutes"], f["relvol"], f["or5h"], f["or5l"] = -1, 0.0, np.inf, 0.0  # no opening range in 24/7 markets
        out[s] = f
    return out


def run(end: dt.date | None = None) -> dict:
    end = end or dt.date.today() - dt.timedelta(days=1)
    hd = HistoricalData(os.environ["ALPACA_PAPER_API_KEY"], os.environ["ALPACA_PAPER_API_SECRET"],
                        os.environ.get("QT_CACHE", "data/cache"), "sip")
    groups = {"stocks": stock_frames(hd, DEV[0], end), "crypto": crypto_frames(hd, DEV[0], end)}
    res, pvals = {}, {}
    for g, fr in groups.items():
        ac = "crypto" if g == "crypto" else "us_equity"
        for cls in (SC1, SC2, SC3):
            if g == "crypto" and cls is SC3:
                continue
            st = cls()
            st.asset_class = ac
            for split, (a, b) in (("dev", DEV), ("holdout", HOLD)):
                for stress in (1.0, 2.0):
                    cfg = BTConfig(capital_constrained=False, costs=CostModel(stress=stress), start=a, end=b or end,
                                   flatten_bars_left=2 if g == "stocks" else -1)
                    t = run_backtest(st, fr, cfg).trades_df()
                    m = trade_metrics(t)
                    key = f"{g}|{st.name}|{split}|x{stress:g}"
                    res[key] = {k: m.get(k) for k in ("trades", "expectancy_r", "win_rate", "profit_factor",
                                                       "trades_per_session")}
                    if split == "dev" and stress == 1.0:
                        pvals[f"{g}|{st.name}"] = ttest_gt0(t["r"].to_numpy(float)) if len(t) > 2 else 1.0
    hp = holm(pvals)
    verdict = {}
    for k, p in hp.items():
        d, h, d2, h2 = (res.get(f"{k}|{s}") or {} for s in ("dev|x1", "holdout|x1", "dev|x2", "holdout|x2"))
        ok = ((d.get("trades") or 0) >= 60 and (d.get("profit_factor") or 0) >= 1.3 and p <= 0.05
              and (h.get("trades") or 0) >= 20 and (h.get("profit_factor") or 0) >= 1.1
              and (h2.get("expectancy_r") or -1) > 0)
        verdict[k] = {"holm_p": p, "pass": ok}
    return {"results": res, "verdict": verdict}


if __name__ == "__main__":
    out = run()
    print(json.dumps(out, indent=1, default=str))
