"""24/7 crypto books: hourly signals on confirmed bars, Alpaca taker fees, never a real order (synthetic data)."""
import datetime as dt
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from qt.broker.base import SimBroker
from qt.db import DB
from qt.engine import Engine
from qt.strategies import library
from qt.strategies.base import ExitPlan, Strategy, frame
from tests.test_engine_api import FakeHist, settings


@dataclass
class CryptoAlways(Strategy):
    name: str = "Z_crypto"
    description: str = "test: hourly crypto signal on every bar"
    timeframe: str = "1Hour"
    asset_class: str = "crypto"
    overnight: bool = True
    defaults: dict = field(default_factory=dict)

    def exit_plan(self):
        return ExitPlan(max_bars=48, flatten_eod=False)

    def signals(self, f):
        return frame(f, np.ones(len(f), bool), f["close"] - 2 * f["atr"], strength=pd.Series(1.0, index=f.index))


def _hourly(end, px=60000.0, n=800):
    idx = pd.date_range(end=end, periods=n, freq="1h", tz="UTC")
    rng = np.random.default_rng(3)
    c = px * np.cumprod(1 + rng.normal(0, 0.004, n))
    o = np.r_[px, c[:-1]]
    return pd.DataFrame({"open": o, "high": np.maximum(o, c) * 1.002, "low": np.minimum(o, c) * 0.998,
                         "close": c, "volume": 50.0}, index=idx)


def test_crypto_book_trades_on_weekend(tmp_path, monkeypatch):
    import qt.engine as engmod
    for k in [k for k, v in library.BY_NAME.items() if v().timeframe == "1Hour"]:
        monkeypatch.delitem(engmod.BY_NAME, k)
    monkeypatch.setitem(engmod.BY_NAME, "Z_crypto", CryptoAlways)
    s = settings(tmp_path)
    t = [dt.datetime(2026, 11, 28, 15, 2, tzinfo=dt.timezone.utc)]  # Saturday: stock market closed
    bars = _hourly(pd.Timestamp("2026-11-28 15:00", tz="UTC"))  # last bar (15:00) is still forming
    px = float(bars["close"].iloc[-2])
    b = SimBroker(prices={"BTC/USD": px}, now=t[0], bars_data={"BTC/USD": bars})
    db = DB(s.db_path)
    e = Engine(s, broker=b, db=db, hist=FakeHist(), notifier=lambda *a, **k: None, clock=lambda: t[0])
    e.startup()
    e.cycle()
    assert db.one("SELECT COUNT(*) c FROM trades")["c"] == 0          # no real order
    bt = db.one("SELECT * FROM book_trades WHERE strategy='Z_crypto'")
    assert bt and bt["engine"] == "crypto" and bt["symbol"] == "BTC/USD" and bt["bar_minutes"] == 60
    assert abs(bt["fees"] - bt["notional"] * 0.0025) < 1e-6          # Alpaca taker fee
    scan = db.kv_get("crypto_scan")
    assert scan["evaluated"] >= 1 and scan["signals"][0]["taken"]
    # same hour: no duplicate entry
    e.cycle()
    assert db.one("SELECT COUNT(*) c FROM book_trades")["c"] == 1
    # crash through the stop on Sunday: closed by the 24/7 manager, loss includes both fees
    t[0] = dt.datetime(2026, 11, 29, 3, 0, tzinfo=dt.timezone.utc)
    b.now = t[0]
    b.prices["BTC/USD"] = bt["stop"] * 0.99
    e.cycle()
    bt = db.one("SELECT * FROM book_trades WHERE strategy='Z_crypto' AND status='closed'")
    assert bt and bt["exit_reason"] == "stop" and bt["r"] < -1.0
    # dashboard: every strategy listed with its own book; scanner shows market status and crypto scan
    from fastapi.testclient import TestClient
    from qt.api.app import create_app
    e._books_published = -1e9
    e.publish_status(t[0])
    c = TestClient(create_app(s, db))
    c.post("/api/login", json={"username": "admin", "password": "correct horse battery"})
    bk = c.get("/api/books").json()
    row = {r["strategy"]: r for r in bk["strategies"]}["Z_crypto"]
    assert row["engine"] == "crypto" and row["closed_trades"] == 1 and row["equity"] < 100 and not row["real_paper_orders"]
    assert any(r["engine"] == "swing" for r in bk["strategies"]) and bk["closed"][0]["exit_reason"] == "stop"
    sc = c.get("/api/scanner").json()
    assert sc["market"]["crypto"] == "open 24/7" and sc["crypto"]["evaluated"] >= 1
    assert "rejection_reasons" in sc and "upcoming" in sc
