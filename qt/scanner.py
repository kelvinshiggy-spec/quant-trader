"""Two-stage market scanner.

Stage 1 (broad discovery, every few minutes): Alpaca snapshots for the liquid
discovery universe (built nightly from asset discovery + SIP daily bars).
Ranks by unusual volume, % move, momentum from the open, proximity to the day
high (breakout), gaps and IEX quote spread.

Stage 2 (deep analysis, on every confirmed 5-minute bar): full feature build
and every strategy's signal function on the top-ranked symbols.

Data honesty: live quotes/bars are IEX (one exchange, free plan). IEX volume
is a small slice of consolidated volume, so relative volume is always computed
within the IEX feed, and IEX spreads are shown as "IEX quote spread".
"""
from __future__ import annotations

import datetime as dt
import logging
import math

import numpy as np
import pandas as pd

from .universe import CORE_ETFS, SECTOR_ETFS, STOCKS

log = logging.getLogger("scanner")

ALWAYS_WATCH = ["SPY", "QQQ", "IWM", "GLD", "SLV", "NVDA", "TSLA", "AMD", "AAPL", "META", "MSFT", "AMZN", "GOOG", "PLTR"]
ALLOWED_EXCHANGES = {"NYSE", "NASDAQ", "ARCA", "AMEX", "BATS"}


def build_discovery_universe(broker, hist, min_adv: float = 5e7, min_price: float = 5.0,
                             max_symbols: int = 600) -> list[dict]:
    """Nightly: tradable + fractionable listed assets ranked by SIP 20-day dollar volume."""
    assets = [a for a in broker.list_assets("us_equity")
              if a["tradable"] and a["fractionable"] and a["exchange"] in ALLOWED_EXCHANGES
              and a["symbol"].isalpha()]
    syms = [a["symbol"] for a in assets]
    end = dt.date.today()
    start = end - dt.timedelta(days=45)
    out = []
    for i in range(0, len(syms), 200):
        chunk = syms[i:i + 200]
        try:
            bars = hist._fetch(chunk, "1Day", start, end - dt.timedelta(days=1), "us_equity", "sip")
        except Exception as e:  # noqa: BLE001
            log.warning("discovery chunk failed: %s", e)
            continue
        for s, d in bars.items():
            if len(d) < 10:
                continue
            adv = float((d["close"] * d["volume"]).tail(20).mean())
            px = float(d["close"].iloc[-1])
            if adv >= min_adv and px >= min_price:
                out.append({"symbol": s, "adv": adv, "price": px})
    out.sort(key=lambda x: -x["adv"])
    return out[:max_symbols]


def stage1_rank(snaps: dict[str, dict], universe: dict[str, float], now: dt.datetime, session_open: dt.datetime,
                session_close: dt.datetime, top_n: int = 25) -> pd.DataFrame:
    rows = []
    frac = max(0.02, min(1.0, (now - session_open).total_seconds() / max(1, (session_close - session_open).total_seconds())))
    for s, v in snaps.items():
        try:
            px, pc, op = v["price"], v["prev_close"], v["day_open"]
            if not px or not pc or not op:
                continue
            spread = ((v["ask"] - v["bid"]) / ((v["ask"] + v["bid"]) / 2) * 1e4) if v.get("ask") and v.get("bid") and v["ask"] > v["bid"] else np.nan
            relvol = (v["day_volume"] / (v["prev_volume"] * frac)) if v.get("prev_volume") else np.nan
            rng = (v["day_high"] - v["day_low"]) or np.nan
            rows.append({"symbol": s, "price": px, "change_pct": (px / pc - 1) * 100, "gap_pct": (op / pc - 1) * 100,
                         "from_open_pct": (px / op - 1) * 100, "relvol_iex": relvol,
                         "near_high": (px - v["day_low"]) / rng if rng == rng else np.nan,
                         "spread_bps_iex": spread, "adv": universe.get(s), "trade_ts": v.get("trade_ts")})
        except Exception:  # noqa: BLE001
            continue
    df = pd.DataFrame(rows)
    if df.empty:
        return df

    def z(x):
        x = x.astype(float)
        sd = x.std(ddof=0)
        return ((x - x.mean()) / sd).clip(-3, 3).fillna(0) if sd and sd > 0 else x * 0

    df["s_volume"] = z(np.log(df["relvol_iex"].clip(lower=0.05)))
    df["s_move"] = z(df["change_pct"].abs())
    df["s_momo"] = z(df["from_open_pct"])
    df["s_break"] = z(df["near_high"])
    df["s_liq"] = z(np.log10(df["adv"].fillna(1e7)))
    df["s_spread"] = -z(df["spread_bps_iex"].fillna(df["spread_bps_iex"].median()))
    df["stage1_score"] = (1.5 * df["s_volume"] + 1.0 * df["s_move"] + 1.0 * df["s_momo"] + 0.7 * df["s_break"]
                          + 0.5 * df["s_liq"] + 0.5 * df["s_spread"])
    df = df.sort_values("stage1_score", ascending=False)
    df["rank"] = np.arange(1, len(df) + 1)
    return df


def choose_watchlist(ranked: pd.DataFrame, held: list[str], max_symbols: int = 30) -> list[str]:
    wl = list(dict.fromkeys(held + ["SPY"]))
    for s in ALWAYS_WATCH:
        if len(wl) >= max_symbols // 2:
            break
        if s not in wl:
            wl.append(s)
    if ranked is not None and not ranked.empty:
        for s in ranked["symbol"]:
            if len(wl) >= max_symbols:
                break
            if s not in wl:
                wl.append(s)
    return wl[:max_symbols]


def default_universe() -> list[str]:
    return list(dict.fromkeys(CORE_ETFS + SECTOR_ETFS + STOCKS))


def correlation_with(returns: pd.DataFrame, sym: str, held: list[str]) -> float:
    if returns is None or sym not in returns or not held:
        return 0.0
    vals = [returns[sym].corr(returns[h]) for h in held if h in returns and h != sym]
    vals = [v for v in vals if v == v]
    return float(max(vals)) if vals else 0.0
