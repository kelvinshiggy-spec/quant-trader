"""Builds feature datasets for research from the cached Alpaca history."""
from __future__ import annotations

import datetime as dt
import logging
from pathlib import Path

import numpy as np
import pandas as pd

from ..data.historical import HistoricalData, to_rth
from ..strategies.features import daily_features, hourly_crypto_features, intraday_features
from ..universe import CRYPTO, DAILY_UNIVERSE, INTRADAY_UNIVERSE, SECTOR_ETFS

log = logging.getLogger(__name__)


class Datasets:
    def __init__(self, hd: HistoricalData, start: dt.date, end: dt.date, daily_start: dt.date,
                 feat_dir: Path | str = "data/features"):
        self.hd, self.start, self.end, self.daily_start = hd, start, end, daily_start
        self.feat_dir = Path(feat_dir)
        self.feat_dir.mkdir(parents=True, exist_ok=True)
        self._daily = None

    def daily_raw(self) -> dict[str, pd.DataFrame]:
        if self._daily is None:
            self._daily = self.hd.bars(DAILY_UNIVERSE, "1Day", self.daily_start, self.end)
        return self._daily

    def daily_feats(self) -> dict[str, pd.DataFrame]:
        raw = self.daily_raw()
        out = {s: daily_features(d) for s, d in raw.items() if not d.empty}
        # cross-sectional sector momentum rank (known at each close)
        rets = pd.DataFrame({s: out[s]["ret63"] for s in SECTOR_ETFS if s in out})
        ranks = rets.rank(axis=1, ascending=False)
        for s in rets.columns:
            out[s]["sector_rank"] = ranks[s]
        for s, f in out.items():
            f["session"] = [x.tz_convert("America/New_York").date() for x in f.index]
            f["adv"] = f["dollar_vol20"].shift(1)
        return out

    def crypto_feats(self) -> dict[str, pd.DataFrame]:
        raw = self.hd.bars(CRYPTO, "1Hour", self.start, self.end, asset_class="crypto")
        out = {}
        for s, d in raw.items():
            if d.empty:
                continue
            f = hourly_crypto_features(d)
            f["session"] = f.index.date
            out[s] = f
        return out

    def intraday_feats(self, symbols: list[str] | None = None) -> dict[str, pd.DataFrame]:
        symbols = symbols or INTRADAY_UNIVERSE
        daily = self.daily_raw()
        spy = self._intraday_one("SPY", daily.get("SPY"), None)
        out = {}
        for s in symbols:
            out[s] = spy if s == "SPY" else self._intraday_one(s, daily.get(s), spy)
        return out

    def _intraday_one(self, sym, daily, bench) -> pd.DataFrame:
        p = self.feat_dir / f"{sym}_5Min_{self.start}_{self.end}.parquet"
        if p.exists():
            f = pd.read_parquet(p)
            f["session"] = pd.to_datetime(f["session"]).dt.date
            return f
        raw = self.hd.bars([sym], "5Min", self.start, self.end)[sym]
        rth = to_rth(raw, 5)
        f = intraday_features(rth, daily, bench, 5)
        num = f.select_dtypes(include=[np.float64]).columns
        f[num] = f[num].astype(np.float32)
        f.to_parquet(p)
        return f
