"""Backtest of the owner-requested crypto strategies on real Alpaca crypto bars (2026-11-27).

L3_btc_15m_trend (BTC, 15-minute) and L_crypto_trend (hourly, BTC/ETH/SOL/PAXG), signal-level (fixed $ per
trade), next-bar-open fills, Alpaca taker fee 0.25% per side, spread + slippage from CostModel, costs also at 2x.
Dev 2024-01-01..2024-12-31, holdout 2025-01-01..end. Nothing was tuned: default parameters only.

Run: python -m qt.research.crypto_intraday
"""
from __future__ import annotations

import datetime as dt
import json
import os

from ..backtest.engine import BTConfig, run_backtest
from ..backtest.metrics import trade_metrics
from ..costs import CostModel
from ..data.historical import HistoricalData
from ..strategies.features import hourly_crypto_features
from ..strategies.library import BY_NAME

SPLITS = {"dev": (dt.date(2024, 1, 1), dt.date(2024, 12, 31)), "holdout": (dt.date(2025, 1, 1), None)}


def frames(hd, symbols, tf, start, end):
    raw = hd.bars(symbols, tf, start, end, asset_class="crypto")
    out = {}
    for s, d in raw.items():
        if d.empty:
            continue
        f = hourly_crypto_features(d)
        f["session"] = f.index.date
        out[s] = f
    return out


def run(end: dt.date | None = None) -> dict:
    end = end or dt.date.today() - dt.timedelta(days=1)
    hd = HistoricalData(os.environ["ALPACA_PAPER_API_KEY"], os.environ["ALPACA_PAPER_API_SECRET"],
                        os.environ.get("QT_CACHE", "data/cache"), "sip")
    jobs = {"L3_btc_15m_trend": ("15Min", ["BTC/USD"]),
            "L_crypto_trend": ("1Hour", ["BTC/USD", "ETH/USD", "SOL/USD", "PAXG/USD"])}
    res = {}
    for name, (tf, syms) in jobs.items():
        fr = frames(hd, syms, tf, dt.date(2023, 10, 1), end)
        for split, (a, b) in SPLITS.items():
            for stress in (1.0, 2.0):
                cfg = BTConfig(capital_constrained=False, costs=CostModel(stress=stress), start=a, end=b or end)
                r = run_backtest(BY_NAME[name](), fr, cfg)
                m = trade_metrics(r.trades_df())
                res[f"{name}|{split}|x{stress:g}"] = {k: m.get(k) for k in
                                                       ("trades", "expectancy_r", "win_rate", "profit_factor",
                                                        "trades_per_session", "net_pnl")}
    return res


if __name__ == "__main__":
    print(json.dumps(run(), indent=1, default=str))
