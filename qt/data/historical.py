"""Historical market data from Alpaca with a local parquet cache.

Free plan facts verified 2026-11-27 with this account:
* SIP history is available back to 2016, except the most recent 15 minutes.
* IEX is the free real-time feed (a single exchange, ~2-3% of volume).
* Crypto bars are available (hourly back to 2021 tested).
Research uses split+dividend adjusted SIP bars. Live features use IEX, and
volume features are always normalised within one feed (relative volume), so
the scale difference between feeds does not leak into signals.
"""
from __future__ import annotations

import datetime as dt
import logging
import time
from pathlib import Path

import pandas as pd

from ..markettime import ET, session

log = logging.getLogger(__name__)

_TF = {"1Min": (1, "Minute"), "5Min": (5, "Minute"), "15Min": (15, "Minute"),
       "30Min": (30, "Minute"), "1Hour": (1, "Hour"), "1Day": (1, "Day")}


def _timeframe(tf: str):
    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
    n, unit = _TF[tf]
    return TimeFrame(n, getattr(TimeFrameUnit, unit))


class HistoricalData:
    def __init__(self, api_key: str, api_secret: str, cache_dir: Path | str = "data/cache",
                 feed: str = "sip"):
        from alpaca.data.historical import CryptoHistoricalDataClient, StockHistoricalDataClient
        self.stock = StockHistoricalDataClient(api_key, api_secret)
        self.crypto = CryptoHistoricalDataClient(api_key, api_secret)
        self.cache = Path(cache_dir)
        self.cache.mkdir(parents=True, exist_ok=True)
        self.feed = feed

    def _index(self) -> dict:
        import json
        p = self.cache / "index.json"
        return json.loads(p.read_text()) if p.exists() else {}

    def _index_set(self, name: str, start: dt.date) -> None:
        import json
        idx = self._index()
        prev = idx.get(name)
        idx[name] = min(prev, start.isoformat()) if prev else start.isoformat()
        (self.cache / "index.json").write_text(json.dumps(idx))

    def _path(self, symbol: str, tf: str, feed: str) -> Path:
        return self.cache / f"{symbol.replace('/', '-')}_{tf}_{feed}.parquet"

    def bars(self, symbols: list[str], tf: str, start: dt.date, end: dt.date,
             asset_class: str = "us_equity", feed: str | None = None, use_cache: bool = True) -> dict[str, pd.DataFrame]:
        """Incremental cache: one file per symbol/timeframe/feed; only missing ranges are downloaded."""
        feed = feed or self.feed
        fk = feed if asset_class != "crypto" else "crypto"
        out = {}
        need_full, need_tail = [], {}
        for s in symbols:
            p = self._path(s, tf, fk)
            if use_cache and p.exists():
                df = pd.read_parquet(p)
                out[s] = df
                first, last = df.index.min().date(), df.index.max().date()
                covered = dt.date.fromisoformat(self._index().get(p.name, first.isoformat()))
                if min(first, covered) > start + dt.timedelta(days=7):
                    need_full.append(s)
                elif last < end:
                    need_tail[s] = last
            else:
                need_full.append(s)
        for chunk in [need_full[i:i + 10] for i in range(0, len(need_full), 10)]:
            got = self._fetch(chunk, tf, start, end, asset_class, feed)
            for s in chunk:
                out[s] = got.get(s, pd.DataFrame())
                if not out[s].empty:
                    out[s].to_parquet(self._path(s, tf, fk))
                    self._index_set(self._path(s, tf, fk).name, start)
        for s, last in need_tail.items():
            got = self._fetch([s], tf, last, end, asset_class, feed).get(s)
            if got is not None and not got.empty:
                df = pd.concat([out[s], got])
                df = df[~df.index.duplicated(keep="last")].sort_index()
                df.to_parquet(self._path(s, tf, fk))
                out[s] = df
        for s in symbols:
            df = out.get(s, pd.DataFrame())
            if not df.empty:
                lo = pd.Timestamp(start, tz="UTC")
                hi = pd.Timestamp(end, tz="UTC") + pd.Timedelta(days=1)
                out[s] = df[(df.index >= lo) & (df.index < hi)]
        return out

    def _fetch(self, symbols, tf, start, end, asset_class, feed) -> dict[str, pd.DataFrame]:
        from alpaca.data.enums import Adjustment, DataFeed
        from alpaca.data.requests import CryptoBarsRequest, StockBarsRequest
        s_dt = dt.datetime.combine(start, dt.time(0), tzinfo=dt.timezone.utc)
        e_dt = dt.datetime.combine(end, dt.time(23, 59), tzinfo=dt.timezone.utc)
        for attempt in range(5):
            try:
                if asset_class == "crypto":
                    req = CryptoBarsRequest(symbol_or_symbols=symbols, timeframe=_timeframe(tf), start=s_dt, end=e_dt)
                    df = self.crypto.get_crypto_bars(req).df
                else:
                    req = StockBarsRequest(symbol_or_symbols=symbols, timeframe=_timeframe(tf), start=s_dt,
                                           end=e_dt, adjustment=Adjustment.ALL,
                                           feed=DataFeed.SIP if feed == "sip" else DataFeed.IEX)
                    df = self.stock.get_stock_bars(req).df
                break
            except Exception as e:  # noqa: BLE001
                wait = 2 ** attempt * 3
                log.warning("bars fetch failed (%s); retry in %ss", e, wait)
                time.sleep(wait)
        else:
            raise RuntimeError(f"could not fetch bars for {symbols}")
        res = {}
        if df is None or df.empty:
            return res
        for s in symbols:
            if s in df.index.get_level_values(0):
                d = df.xs(s, level=0).copy()
                d.index = pd.DatetimeIndex(d.index).tz_convert("UTC")
                d = d[["open", "high", "low", "close", "volume"] + [c for c in ("vwap", "trade_count") if c in d.columns]]
                res[s] = d.sort_index()
        return res


def to_rth(df: pd.DataFrame, bar_minutes: int) -> pd.DataFrame:
    """Keep regular-session bars only (handles early closes) and add `session` (ET date).

    A bar timestamp is the bar START. A bar is RTH if it starts at/after the open
    and ends at/before the close.
    """
    if df.empty:
        return df.assign(session=pd.Series(dtype=object))
    idx = df.index
    et_dates = idx.tz_convert(ET).date
    keep = []
    for ts, d in zip(idx, et_dates):
        s = session(d)
        if s is None:
            keep.append(False)
            continue
        o, c = s
        end = ts + pd.Timedelta(minutes=bar_minutes)
        keep.append(ts >= o and end <= c)
    out = df[keep].copy()
    out["session"] = out.index.tz_convert(ET).date
    return out


def daily_from_intraday_sessions(df: pd.DataFrame) -> pd.DataFrame:
    g = df.groupby("session")
    return pd.DataFrame({"open": g["open"].first(), "high": g["high"].max(), "low": g["low"].min(),
                         "close": g["close"].last(), "volume": g["volume"].sum()})
