"""Sign-in survives redeploys; the dashboard self-test and the client problem report work."""
import threading
import time
import urllib.request

import bcrypt
import pytest
import uvicorn
from fastapi.testclient import TestClient

from deploy.make_env import _password_hash
from qt.api import selftest
from qt.api.app import create_app
from qt.db import DB
from tests.test_engine_api import settings

H = {"X-Requested-With": "quant-trader"}


def test_redeploy_keeps_password_unless_change_is_asked():
    old = bcrypt.hashpw(b"owner-password-1", bcrypt.gensalt(4)).decode()
    assert _password_hash("owner-password-1", old, change=False) == old          # unchanged: same hash kept
    assert _password_hash("stale-password-9", old, change=False) == old          # stale session: not reset
    new = _password_hash("brand-new-pass-2", old, change=True)                   # explicit change applies
    assert new != old and bcrypt.checkpw(b"brand-new-pass-2", new.encode())
    first = _password_hash("first-password-3", "", change=False)                 # first deploy: hash created
    assert bcrypt.checkpw(b"first-password-3", first.encode())


def test_client_problem_report_is_logged(tmp_path):
    s = settings(tmp_path)
    db = DB(s.db_path)
    c = TestClient(create_app(s, db))
    assert c.post("/api/client_error", json={"tab": "overview", "message": "x"}, headers=H).status_code == 401
    assert c.post("/api/login", json={"username": "admin", "password": "correct horse battery"}, headers=H).status_code == 200
    assert c.post("/api/client_error", json={"tab": "overview", "message": "tab rendered blank"}, headers=H).json()["ok"]
    ev = db.query("SELECT * FROM events WHERE kind='dashboard_client'")
    assert ev and "tab rendered blank" in ev[0]["message"]


@pytest.fixture
def live_server(tmp_path, monkeypatch):
    s = settings(tmp_path)
    db = DB(s.db_path)
    server = uvicorn.Server(uvicorn.Config(create_app(s, db), host="127.0.0.1", port=0, log_level="warning"))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    while not server.started:
        time.sleep(0.05)
    port = server.servers[0].sockets[0].getsockname()[1]
    monkeypatch.setattr(selftest, "BASE", f"http://127.0.0.1:{port}")
    monkeypatch.setenv("SESSION_SECRET", s.session_secret)
    monkeypatch.setenv("DASHBOARD_USER", "admin")
    yield port, db
    server.should_exit = True
    t.join(5)


def test_selftest_passes_on_a_healthy_dashboard(live_server):
    assert selftest.run() == []


def test_selftest_catches_a_broken_tab(live_server, monkeypatch):
    port, db = live_server
    db.query("DROP TABLE trades")  # the Overview and Trades tabs can no longer load
    failures = selftest.run()
    assert any("/api/overview" in f for f in failures)
    urllib.request.urlopen(f"http://127.0.0.1:{port}/app.js")  # static files still served
