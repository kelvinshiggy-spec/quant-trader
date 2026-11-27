"""Exchange calendar and time helpers. All display times are Pacific Time.

Uses exchange_calendars (XNYS) offline, which knows holidays and early closes,
and can be cross-checked against Alpaca's /v2/calendar in the live engine.
"""
from __future__ import annotations

import datetime as dt
from functools import lru_cache
from zoneinfo import ZoneInfo

import pandas as pd

ET = ZoneInfo("America/New_York")
PT = ZoneInfo("America/Los_Angeles")
UTC = dt.timezone.utc


@lru_cache(maxsize=1)
def _xnys():
    import exchange_calendars as xcals
    return xcals.get_calendar("XNYS", start="2015-01-01")


def now_utc() -> dt.datetime:
    return dt.datetime.now(tz=UTC)


def to_pt(ts) -> dt.datetime:
    if isinstance(ts, pd.Timestamp):
        ts = ts.to_pydatetime()
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return ts.astimezone(PT)


def fmt_pt(ts) -> str:
    if ts is None:
        return "-"
    return to_pt(ts).strftime("%Y-%m-%d %H:%M:%S PT")


@lru_cache(maxsize=4096)
def session(date: dt.date) -> tuple[dt.datetime, dt.datetime] | None:
    """(open, close) in UTC for a trading date, or None if the market is closed."""
    cal = _xnys()
    ts = pd.Timestamp(date)
    if not cal.is_session(ts):
        return None
    o = cal.session_open(ts).to_pydatetime()
    c = cal.session_close(ts).to_pydatetime()
    return o.astimezone(UTC), c.astimezone(UTC)


def is_trading_day(date: dt.date) -> bool:
    return session(date) is not None


def next_trading_day(date: dt.date) -> dt.date:
    d = date + dt.timedelta(days=1)
    for _ in range(15):
        if is_trading_day(d):
            return d
        d += dt.timedelta(days=1)
    raise RuntimeError("no trading day found within 15 days")


def settlement_date(trade_date: dt.date, asset_class: str = "us_equity") -> dt.date:
    """US equities settle T+1 (since 2024-05-28). Crypto at Alpaca settles immediately."""
    if asset_class == "crypto":
        return trade_date
    return next_trading_day(trade_date)


def trading_days(start: dt.date, end: dt.date) -> list[dt.date]:
    cal = _xnys()
    s = cal.sessions_in_range(pd.Timestamp(start), pd.Timestamp(end))
    return [x.date() for x in s]


def is_early_close(date: dt.date) -> bool:
    s = session(date)
    if s is None:
        return False
    return s[1].astimezone(ET).time() < dt.time(16, 0)


def market_is_open(at: dt.datetime | None = None) -> bool:
    at = at or now_utc()
    s = session(at.astimezone(ET).date())
    return bool(s and s[0] <= at < s[1])


def pt_schedule_for(date: dt.date) -> dict:
    """The daily schedule in PT, derived from the real session times."""
    s = session(date)
    if s is None:
        return {"trading_day": False}
    o, c = s
    return {
        "trading_day": True,
        "early_close": is_early_close(date),
        "premarket_research": to_pt(o - dt.timedelta(minutes=30)),
        "open": to_pt(o),
        "entry_cutoff": to_pt(c - dt.timedelta(minutes=30)),
        "flatten": to_pt(c - dt.timedelta(minutes=10)),
        "close": to_pt(c),
        "daily_report": to_pt(c + dt.timedelta(minutes=5)),
        "research": to_pt(c + dt.timedelta(minutes=60)),
    }
