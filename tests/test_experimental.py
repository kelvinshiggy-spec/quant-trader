"""Owner-approved experimental strategies and the paper-only daily-minimum trade (synthetic data, not a result)."""
import dataclasses
import datetime as dt

import pytest

from qt.broker.base import SimBroker
from qt.config import load_settings
from qt.db import DB
from qt.engine import Engine
from qt.strategies import library
from tests.test_e2e import AlwaysOnLastBar, synthetic_bars
from tests.test_engine_api import FakeHist, settings


def make(tmp_path, monkeypatch, status="shadow", **env):
    monkeypatch.setitem(library.BY_NAME, "Z_test", AlwaysOnLastBar)
    import qt.engine as engmod
    monkeypatch.setitem(engmod.BY_NAME, "Z_test", AlwaysOnLastBar)
    s = settings(tmp_path, **env)
    t = [dt.datetime(2026, 11, 24, 17, 0, 15, tzinfo=dt.timezone.utc)]
    bars = synthetic_bars(dt.date(2026, 11, 24))
    b = SimBroker(prices={"SPY": float(bars["close"].iloc[-1])}, now=t[0], bars_data={"SPY": bars})
    db = DB(s.db_path)
    db.upsert("strategy_status", {"strategy": "Z_test", "version": "1.0.0", "params": "{}", "status": status,
                                  "approved": 0, "oos_metrics": {"expectancy_r": 0.2, "trades": 300, "holm_p": 0.5},
                                  "allowed_regimes": None}, "strategy")
    e = Engine(s, broker=b, db=db, hist=FakeHist(), notifier=lambda *a, **k: None, clock=lambda: t[0])
    e.watchlist = ["SPY"]
    return e, db


def test_shadow_strategy_does_not_trade_by_default(tmp_path, monkeypatch):
    e, db = make(tmp_path, monkeypatch)
    e.startup(); e.cycle()
    assert db.one("SELECT COUNT(*) c FROM trades")["c"] == 0


def test_experimental_strategy_trades_and_is_labelled(tmp_path, monkeypatch):
    e, db = make(tmp_path, monkeypatch, EXPERIMENTAL_STRATEGIES="Z_test")
    e.startup(); e.cycle()
    tr = db.one("SELECT * FROM trades")
    assert tr and tr["strategy"] == "Z_test" and tr["thesis"].startswith("EXPERIMENTAL")
    assert db.one("SELECT * FROM orders WHERE kind='stop'")["status"] == "new"


def test_rejected_strategy_never_trades_even_if_listed(tmp_path, monkeypatch):
    e, db = make(tmp_path, monkeypatch, status="rejected", EXPERIMENTAL_STRATEGIES="Z_test")
    e.startup(); e.cycle()
    assert db.one("SELECT COUNT(*) c FROM trades")["c"] == 0


def test_forced_daily_minimum_trade(tmp_path, monkeypatch):
    e, db = make(tmp_path, monkeypatch, status="shadow", MIN_DAILY_TRADES="1", FORCE_TRADE_AFTER_MIN="45")
    e.startup(); e.cycle()
    tr = db.one("SELECT * FROM trades")
    assert tr and tr["strategy"] == "FORCED_daily_min" and "NOT a validated signal" in tr["thesis"]
    assert db.one("SELECT * FROM orders WHERE kind='stop'")["status"] == "new"
    assert tr["notional"] <= 100.0
    e.cycle()  # only one forced trade per day
    assert db.one("SELECT COUNT(*) c FROM trades")["c"] == 1


def test_no_forced_trade_before_the_wait(tmp_path, monkeypatch):
    e, db = make(tmp_path, monkeypatch, status="rejected", MIN_DAILY_TRADES="1", FORCE_TRADE_AFTER_MIN="300")
    e.startup(); e.cycle()
    assert db.one("SELECT COUNT(*) c FROM trades")["c"] == 0


def test_daily_minimum_is_paper_only():
    env = {"TRADING_MODE": "paper", "ALPACA_PAPER_API_KEY": "PKX", "ALPACA_PAPER_API_SECRET": "y", "MIN_DAILY_TRADES": "1"}
    assert load_settings(env).min_daily_trades == 1
    s = load_settings(env)
    live = dataclasses.replace(s, mode="live")
    e = Engine.__new__(Engine)
    e.s = live
    e.force_daily_minimum([], dt.datetime(2026, 11, 24, 17, tzinfo=dt.timezone.utc),
                          dt.datetime(2026, 11, 24, 14, 30, tzinfo=dt.timezone.utc))  # returns before touching state


def test_forced_trade_falls_back_to_scanner(tmp_path, monkeypatch):
    e, db = make(tmp_path, monkeypatch, status="rejected", MIN_DAILY_TRADES="1")
    e.startup()
    e.cycle()  # no strategy candidates at all
    assert db.one("SELECT COUNT(*) c FROM trades")["c"] == 0
    now = e.clock()
    bars = e.bars["SPY"]
    e.bars["QQQ"] = bars
    e.prices["QQQ"] = float(bars["close"].iloc[-1])
    e.quote_ts["QQQ"] = now
    e.spreads["QQQ"] = 2.0
    e.broker.prices["QQQ"] = e.prices["QQQ"]
    db.kv_set("stage1", {"top": [{"symbol": "QQQ", "from_open_pct": 0.8, "stage1_score": 3.1, "adv": 1e10}]})
    e.force_daily_minimum([], now, now - dt.timedelta(hours=2))
    tr = db.one("SELECT * FROM trades")
    assert tr and tr["symbol"] == "QQQ" and tr["strategy"] == "FORCED_daily_min"


def test_forced_trade_skips_leveraged_funds(tmp_path, monkeypatch):
    e, db = make(tmp_path, monkeypatch, status="rejected", MIN_DAILY_TRADES="1")
    e.startup()
    e.cycle()
    now = e.clock()
    bars = e.bars["SPY"]
    for sym in ("TQQQ", "QQQ"):
        e.bars[sym] = bars
        e.prices[sym] = float(bars["close"].iloc[-1])
        e.quote_ts[sym] = now
        e.spreads[sym] = 2.0
        e.broker.prices[sym] = e.prices[sym]
    db.kv_set("stage1", {"top": [{"symbol": "TQQQ", "from_open_pct": 2.0, "stage1_score": 5.0, "adv": 1e10},
                                 {"symbol": "QQQ", "from_open_pct": 0.8, "stage1_score": 3.1, "adv": 1e10}]})
    e.force_daily_minimum([], now, now - dt.timedelta(hours=2))
    tr = db.one("SELECT * FROM trades")
    assert tr and tr["symbol"] == "QQQ"


# ---------------------------------------------------------------- daily (swing) path with the wide universe
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from dataclasses import dataclass, field  # noqa: E402
from qt.strategies.base import ExitPlan, Strategy, frame  # noqa: E402


@dataclass
class DailyAlways(Strategy):
    name: str = "Z_daily"
    description: str = "test: daily signal on every bar"
    timeframe: str = "1Day"
    overnight: bool = True
    universe: tuple = ("SPY",)
    defaults: dict = field(default_factory=dict)

    def exit_plan(self):
        return ExitPlan(trail_atr=3.0, trail_activate_r=1.0, flatten_eod=False)

    def signals(self, f):
        return frame(f, np.ones(len(f), bool), f["close"] - 2 * f["atr"], strength=pd.Series(1.0, index=f.index))


def _daily(px, vol, n=400, end=dt.date(2026, 11, 23)):
    idx = pd.bdate_range(end=end, periods=n, tz="UTC") + pd.Timedelta(hours=5)
    rng = np.random.default_rng(abs(hash(px)) % 1000)
    c = px * np.cumprod(1 + rng.normal(0.0005, vol, n))
    return pd.DataFrame({"open": c, "high": c * (1 + vol), "low": c * (1 - vol), "close": c, "volume": 1e7}, index=idx)


class DailyHist:
    def __init__(self, data):
        self.data = data

    def _fetch(self, syms, *a, **k):
        return {s: self.data[s] for s in syms if s in self.data}


def test_daily_cycle_trades_wide_universe_and_skips_cash_like(tmp_path, monkeypatch):
    import qt.engine as engmod
    monkeypatch.setitem(library.BY_NAME, "Z_daily", DailyAlways)
    monkeypatch.setitem(engmod.BY_NAME, "Z_daily", DailyAlways)
    s = settings(tmp_path, EXPERIMENTAL_STRATEGIES="Z_daily", ALLOW_OVERNIGHT_HOLDS="true", SWING_WIDE_UNIVERSE="5")
    s = dataclasses.replace(s, swing_wide_strategies=("Z_daily",))
    t = [dt.datetime(2026, 11, 24, 15, 0, tzinfo=dt.timezone.utc)]  # 10:00 ET
    data = {"ABC": _daily(50.0, 0.02), "BILX": _daily(100.0, 0.0002)}
    b = SimBroker(prices={"ABC": float(data["ABC"]["close"].iloc[-1]), "BILX": 100.0}, now=t[0])
    db = DB(s.db_path)
    db.upsert("strategy_status", {"strategy": "Z_daily", "version": "1", "params": "{}", "status": "shadow",
                                  "approved": 0, "oos_metrics": {"expectancy_r": 0.3, "trades": 300},
                                  "allowed_regimes": None}, "strategy")
    e = Engine(s, broker=b, db=db, hist=DailyHist(data), notifier=lambda *a, **k: None, clock=lambda: t[0])
    e.universe = [{"symbol": "ABC", "adv": 5e8}, {"symbol": "BILX", "adv": 5e8}, {"symbol": "TQQQ", "adv": 9e9}]
    e.daily_cycle(t[0])
    opps = {r["symbol"] for r in db.query("SELECT symbol FROM opportunities WHERE strategy='Z_daily'")}
    assert "ABC" in opps and "BILX" not in opps and "TQQQ" not in opps
    tr = db.one("SELECT * FROM trades")
    assert tr and tr["symbol"] == "ABC" and tr["thesis"].startswith("EXPERIMENTAL")
    # same day, same settings: not re-run
    n = db.one("SELECT COUNT(*) c FROM opportunities")["c"]
    e.daily_cycle(t[0])
    assert db.one("SELECT COUNT(*) c FROM opportunities")["c"] == n
