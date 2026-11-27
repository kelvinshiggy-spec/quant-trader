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
