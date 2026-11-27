"""Download and cache the research datasets from Alpaca (free plan).

Usage: python -m scripts.fetch_data [--start 2021-01-01] [--end 2026-11-25]
"""
from __future__ import annotations

import argparse
import datetime as dt
import logging
import time

from qt.config import load_settings
from qt.data.historical import HistoricalData
from qt.universe import CRYPTO, DAILY_UNIVERSE, INTRADAY_UNIVERSE

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2021-01-01")
    ap.add_argument("--daily-start", default="2016-01-01")
    ap.add_argument("--end", default=None)
    args = ap.parse_args()
    end = dt.date.fromisoformat(args.end) if args.end else dt.date.today() - dt.timedelta(days=1)
    s = load_settings()
    hd = HistoricalData(s.api_key, s.api_secret, s.data_dir / "cache", feed=s.research_feed)
    t0 = time.time()
    d = hd.bars(DAILY_UNIVERSE, "1Day", dt.date.fromisoformat(args.daily_start), end)
    logging.info("daily: %d symbols, %s rows", len(d), sum(len(x) for x in d.values()))
    c = hd.bars(CRYPTO, "1Hour", dt.date.fromisoformat(args.start), end, asset_class="crypto")
    logging.info("crypto hourly: %s rows", sum(len(x) for x in c.values()))
    for sym in INTRADAY_UNIVERSE:
        x = hd.bars([sym], "5Min", dt.date.fromisoformat(args.start), end)
        logging.info("5Min %s: %d rows (%.0fs)", sym, len(x[sym]), time.time() - t0)


if __name__ == "__main__":
    main()
