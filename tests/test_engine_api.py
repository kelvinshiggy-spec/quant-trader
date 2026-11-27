import datetime as dt
import json

import bcrypt
import pytest
from fastapi.testclient import TestClient

from qt.api.app import create_app
from qt.broker.base import SimBroker
from qt.config import load_settings
from qt.db import DB
from qt.engine import Engine
from qt.ledger import Ledger


class FakeHist:
    def _fetch(self, *a, **k):
        return {}


def settings(tmp_path, **extra):
    env = {"ALPACA_PAPER_API_KEY": "PKTEST", "ALPACA_PAPER_API_SECRET": "x", "QT_DATA_DIR": str(tmp_path),
           "SESSION_SECRET": "s" * 40, "DASHBOARD_USER": "admin",
           "DASHBOARD_PASSWORD_HASH": bcrypt.hashpw(b"correct horse battery", bcrypt.gensalt(4)).decode()}
    env.update(extra)
    return load_settings(env)


@pytest.fixture
def eng(tmp_path):
    s = settings(tmp_path)
    clock_t = [dt.datetime(2026, 11, 24, 17, 0, tzinfo=dt.timezone.utc)]  # market open (noon ET)
    b = SimBroker(prices={"SPY": 500.0}, now=clock_t[0])
    db = DB(s.db_path)
    e = Engine(s, broker=b, db=db, hist=FakeHist(), notifier=lambda *a, **k: None, clock=lambda: clock_t[0])
    e.watchlist = ["SPY"]
    return e, b, db, s, clock_t


def test_engine_cycle_heartbeat_and_virtual_ledger(eng):
    e, b, db, s, _ = eng
    e.startup()
    e.cycle()
    hb = db.one("SELECT * FROM heartbeats WHERE component='engine'")
    assert hb and hb["status"] == "ok"
    assert e.tm.ledger.starting_cash == 100.0  # despite the broker's $100,000
    g = db.kv_get("guardian_status")
    assert g["equity"] == pytest.approx(100.0)


def test_unexpected_broker_position_blocks_entries(eng):
    e, b, db, s, _ = eng
    b.positions["TSLA"] = 3.0
    e.startup()
    assert any(k.startswith("unexpected:TSLA") for k in e.integrity)
    assert not e.account_state(e.clock()).integrity_ok


def test_clock_skew_detected(eng):
    e, b, db, s, _ = eng
    b.now = b.now + dt.timedelta(seconds=60)
    e.reconcile()
    assert "clock" in e.integrity


def test_broker_outage_sets_integrity_after_retries(eng):
    e, b, db, s, _ = eng
    b.offline = True
    for _ in range(3):
        e.reconcile()
    assert "broker" in e.integrity


def test_emergency_and_pause_commands(eng):
    e, b, db, s, _ = eng
    db.insert("commands", {"ts": "x", "command": "emergency_shutdown", "args": {}, "requested_by": "t"})
    e.process_commands()
    assert e.flags()["emergency"]
    assert not e.guardian.account_gates(e.account_state(e.clock()))[0]
    db.insert("commands", {"ts": "x", "command": "set_risk_limits", "args": {"risk_per_trade_pct": 2.0}, "requested_by": "t"})
    e.process_commands()
    assert e.limits.risk_per_trade_pct == 0.5  # loosening beyond ceiling refused
    r = db.one("SELECT result FROM commands WHERE command='set_risk_limits'")
    assert r["result"].startswith("refused")


def test_cannot_approve_unvalidated_strategy(eng):
    e, b, db, s, _ = eng
    db.upsert("strategy_status", {"strategy": "A_momentum", "status": "rejected", "approved": 0, "params": "{}"}, "strategy")
    db.insert("commands", {"ts": "x", "command": "approve_strategy", "args": {"strategy": "A_momentum"}, "requested_by": "t"})
    e.process_commands()
    assert db.one("SELECT approved FROM strategy_status")["approved"] == 0


def test_stale_previous_session_position_flattened_on_restart(eng):
    e, b, db, s, clock_t = eng
    from qt.execution import TradePlan
    clock_t[0] = clock_t[0] - dt.timedelta(days=1)
    e.tm.clock = lambda: clock_t[0]
    e.tm.om.clock = e.tm.clock
    e.tm.open_trade(TradePlan("OLD1", "SPY", "us_equity", "A_momentum", "1", 40.0, 500.0, 495.0, None, 2.0, 1.0, 0.0,
                              0, True, 1.0, 60, "bull", "t", "trail", planned_risk=0.4))
    clock_t[0] = clock_t[0] + dt.timedelta(days=1)
    e.startup()
    assert e.tm.trade("OLD1")["status"] == "closed"


def test_api_requires_auth_and_confirmation(tmp_path):
    s = settings(tmp_path)
    db = DB(s.db_path)
    c = TestClient(create_app(s, db))
    assert c.get("/api/overview").status_code == 401
    assert c.post("/api/login", json={"username": "admin", "password": "wrong"}).status_code == 401
    assert c.post("/api/login", json={"username": "admin", "password": "correct horse battery"}).status_code == 200
    assert c.get("/api/overview").status_code == 200
    h = {"X-Requested-With": "quant-trader"}
    assert c.post("/api/command", json={"command": "close_all"}).status_code == 403  # no CSRF header
    assert c.post("/api/command", json={"command": "close_all"}, headers=h).status_code == 400  # no confirmation
    assert c.post("/api/command", json={"command": "close_all", "confirm": "CLOSE ALL"}, headers=h).status_code == 200
    assert c.post("/api/command", json={"command": "enable_live_trading"}, headers=h).status_code == 400
    r = c.post("/api/command", json={"command": "set_risk_limits", "args": {"max_positions": 9}, "confirm": "CHANGE RISK"}, headers=h)
    assert r.status_code == 400
    for path in ("/api/scanner", "/api/positions", "/api/guardian", "/api/tournament", "/api/research", "/api/trades",
                 "/api/system", "/api/reports"):
        assert c.get(path).status_code == 200, path
    assert c.get("/").status_code == 200


def test_api_refuses_weak_config(tmp_path):
    with pytest.raises(RuntimeError):
        create_app(settings(tmp_path, SESSION_SECRET="short"), DB(tmp_path / "x.db"))


def test_dashboard_numbers_match_ledger(tmp_path):
    s = settings(tmp_path)
    db = DB(s.db_path)
    led = Ledger(100.0)
    led.apply_buy_fill("SPY", 0.05, 500.0, 0.0, dt.datetime(2026, 11, 24, 15, tzinfo=dt.timezone.utc), "x", "t1")
    db.kv_set("ledger", led.to_dict())
    db.kv_set("live", {"prices": {"SPY": 510.0}})
    db.upsert("equity", {"ts": "2026-11-24T16:00:00+00:00", "equity": led.equity({"SPY": 510.0})}, "ts")
    c = TestClient(create_app(s, db))
    c.post("/api/login", json={"username": "admin", "password": "correct horse battery"})
    o = c.get("/api/overview").json()
    assert o["unrealized"] == pytest.approx(0.5)
    assert o["settled"] == pytest.approx(75.0)
    assert o["equity"] == pytest.approx(100.5)
