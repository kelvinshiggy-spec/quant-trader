"""End-to-end: scanner -> strategy -> scoring -> guardian -> order -> stop -> flatten,
through the real Engine with a simulated broker (synthetic bars; not a result)."""
import datetime as dt
from dataclasses import dataclass, field

import numpy as np
import pandas as pd
import pytest

from qt.broker.base import SimBroker
from qt.db import DB
from qt.engine import Engine
from qt.strategies import library
from qt.strategies.base import ExitPlan, Strategy, frame
from tests.test_engine_api import FakeHist, settings


@dataclass
class AlwaysOnLastBar(Strategy):
    name: str = "Z_test"
    description: str = "test strategy: signals on every confirmed bar"
    defaults: dict = field(default_factory=dict)

    def exit_plan(self):
        return ExitPlan(trail_atr=2.0, trail_activate_r=1.0)

    def signals(self, f):
        return frame(f, np.ones(len(f), bool), f["close"] * 0.99, strength=pd.Series(1.0, index=f.index))


def synthetic_bars(end_day: dt.date, n_sessions=30, until="12:00"):
    rng = np.random.default_rng(5)
    days = [d.date() for d in pd.bdate_range(end=end_day, periods=n_sessions + 3)][-n_sessions:]
    frames, px = [], 500.0
    for d in days:
        start = pd.Timestamp(f"{d} 09:30", tz="America/New_York").tz_convert("UTC")
        n = 78 if d != end_day else int((pd.Timestamp(f"{d} {until}", tz="America/New_York") - pd.Timestamp(f"{d} 09:30", tz="America/New_York")).total_seconds() // 300)
        c = px * np.cumprod(1 + rng.normal(0, 0.001, n))
        o = np.r_[px, c[:-1]]
        frames.append(pd.DataFrame({"open": o, "high": np.maximum(o, c) * 1.0005, "low": np.minimum(o, c) * 0.9995,
                                    "close": c, "volume": 1e5}, index=pd.date_range(start, periods=n, freq="5min")))
        px = c[-1]
    return pd.concat(frames)


@pytest.fixture
def e2e(tmp_path, monkeypatch):
    monkeypatch.setitem(library.BY_NAME, "Z_test", AlwaysOnLastBar)
    import qt.engine as engmod
    monkeypatch.setitem(engmod.BY_NAME, "Z_test", AlwaysOnLastBar)
    s = settings(tmp_path)
    t = [dt.datetime(2026, 11, 24, 17, 0, 15, tzinfo=dt.timezone.utc)]  # 12:00:15 ET
    bars = synthetic_bars(dt.date(2026, 11, 24))
    b = SimBroker(prices={"SPY": float(bars["close"].iloc[-1])}, now=t[0], bars_data={"SPY": bars})
    db = DB(s.db_path)
    db.upsert("strategy_status", {"strategy": "Z_test", "version": "1.0.0", "params": "{}", "status": "validated",
                                  "approved": 1, "oos_metrics": {"expectancy_r": 0.2, "trades": 300, "holm_p": 0.01},
                                  "allowed_regimes": None}, "strategy")
    clock = lambda: t[0]  # noqa: E731
    e = Engine(s, broker=b, db=db, hist=FakeHist(), notifier=lambda *a, **k: None, clock=clock)
    e.watchlist = ["SPY"]
    return e, b, db, t


def test_full_trade_lifecycle(e2e):
    e, b, db, t = e2e
    e.startup()
    e.cycle()
    opp = db.query("SELECT status, reason FROM opportunities WHERE strategy='Z_test'")
    assert opp and opp[-1]["status"] == "EXECUTE", opp
    tr = db.one("SELECT * FROM trades")
    assert tr["status"] == "open" and tr["notional"] <= 50.0
    assert db.one("SELECT * FROM orders WHERE kind='stop'")["status"] == "new"
    assert e.tm.ledger.settled_cash < 100.0
    # explanation is recorded from actual decision data
    assert "guardian" in tr["explanation"] and "competing_opportunities" in tr["explanation"]
    # second cycle at the same bar must not open a second trade in SPY
    e.cycle()
    assert db.one("SELECT COUNT(*) c FROM trades")["c"] == 1
    # jump to 10 minutes before the close: flatten
    t[0] = dt.datetime(2026, 11, 24, 20, 52, tzinfo=dt.timezone.utc)
    b.now = t[0]
    e.cycle()
    tr = db.one("SELECT * FROM trades")
    assert tr["status"] == "closed" and tr["exit_reason"] == "eod_flatten"
    assert not e.tm.ledger.positions
    assert e.tm.ledger.unsettled_cash > 0  # T+1 proceeds


def test_no_entries_when_paused_or_after_cutoff(e2e):
    e, b, db, t = e2e
    e.startup()
    e.set_flag("paused", True)
    e.cycle()
    assert db.one("SELECT COUNT(*) c FROM trades")["c"] == 0
    rej = db.query("SELECT reason FROM opportunities WHERE strategy='Z_test'")
    assert any("paused" in r["reason"] for r in rej)


def test_stale_data_rejects(e2e):
    e, b, db, t = e2e
    e.startup()
    e.limits.stale_data_seconds = 1
    t[0] = t[0] + dt.timedelta(seconds=20)
    e.cycle()
    assert db.one("SELECT COUNT(*) c FROM trades")["c"] == 0
