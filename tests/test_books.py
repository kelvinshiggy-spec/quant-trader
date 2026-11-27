"""Per-strategy simulated $100 books and the pre-registered forward promotion rules (synthetic data)."""
import datetime as dt

from qt.books import BOOK_CAPITAL, Books, PROMOTE
from qt.config import RiskLimits
from qt.db import DB
from tests.test_experimental import make


def test_rejected_strategy_trades_only_in_its_book(tmp_path, monkeypatch):
    e, db = make(tmp_path, monkeypatch, status="rejected")
    e.startup()
    e.cycle()
    assert db.one("SELECT COUNT(*) c FROM trades")["c"] == 0          # no real paper order
    bt = db.one("SELECT * FROM book_trades WHERE strategy='Z_test'")
    assert bt and bt["status"] == "open" and bt["engine"] == "day" and bt["stop"] < bt["entry_price"]
    assert bt["notional"] <= BOOK_CAPITAL
    summ = db.one("SELECT * FROM opportunities WHERE strategy='Z_test'")
    assert summ["status"] != "EXECUTE"
    # the book closes at the end of the day like a real intraday trade
    t = e.clock()
    e.books.manage({"SPY": bt["entry_price"]}, {}, t, flatten=True)
    bt = db.one("SELECT * FROM book_trades WHERE strategy='Z_test'")
    assert bt["status"] == "closed" and bt["exit_reason"] == "eod_flatten" and bt["pnl"] < 0  # costs paid


def test_book_stop_and_gap_fill(tmp_path):
    db = DB(tmp_path / "b.db")
    b = Books(db, RiskLimits())

    class O:
        symbol, strategy, price, stop, target, atr, est_cost_bps, spread_bps = "ABC", "X", 100.0, 97.0, None, 1.0, 5.0, 3.0
        data_age_s, max_corr, regime, score, signal, adv = 1.0, 0.0, "bull", 60.0, True, 1e9

    class EP:
        trail_atr, trail_activate_r, breakeven_r, max_bars, flatten_eod = 0.0, 1.0, 0.0, 0, False

    now = dt.datetime(2026, 11, 24, 15, tzinfo=dt.timezone.utc)
    tid = b.consider(O(), EP(), "swing", now, {"ABC": 100.0})
    assert tid
    b.manage({"ABC": 95.0}, {}, now + dt.timedelta(hours=1), flatten=False)   # gapped through 97
    t = db.one("SELECT * FROM book_trades WHERE trade_id=?", (tid,))
    assert t["exit_reason"] == "stop" and t["exit_price"] < 95.0 and t["r"] < -1.0
    s = b.stats()[0]
    assert s["closed_trades"] == 1 and s["equity"] < BOOK_CAPITAL


def _fake_closed(db, name, rs):
    for i, r in enumerate(rs):
        db.insert("book_trades", {"trade_id": f"{name}{i}", "strategy": name, "engine": "swing", "symbol": "A",
                                  "status": "closed", "entry_price": 10, "initial_stop": 9, "qty": 1, "r": r, "pnl": r,
                                  "exit_time": f"2026-01-{i % 28 + 1:02d}", "exit_reason": "stop"})


def test_promotion_and_demotion_rules(tmp_path):
    db = DB(tmp_path / "b.db")
    b = Books(db, RiskLimits())
    good = [0.9, -0.5, 1.2, 0.4, -0.3] * (PROMOTE["min_trades"] // 5 + 2)
    bad = [-0.5, 0.2, -0.4] * 12
    few = [2.0, 1.0, 1.5]
    _fake_closed(db, "GOOD", good)
    _fake_closed(db, "BAD", bad)
    _fake_closed(db, "FEW", few)
    for n in ("GOOD", "BAD", "FEW"):
        st = b.state(n)
        st["ledger"]["settled_cash"] = 100 + sum({"GOOD": good, "BAD": bad, "FEW": few}[n])
        b._save(n, st)
    out = b.evaluate(dt.datetime(2026, 11, 27, 22, tzinfo=dt.timezone.utc))
    assert out["GOOD"]["status"] == "promoted"
    assert out["BAD"]["status"] == "demoted"
    assert out["FEW"]["status"] == "testing"   # 3 great trades prove nothing


def test_promoted_strategy_trades_and_demoted_does_not(tmp_path, monkeypatch):
    e, db = make(tmp_path, monkeypatch, status="rejected")
    db.kv_set("forward_status", {"Z_test": {"status": "promoted", "mean_r": 0.3, "holm_p": 0.001}})
    e.startup()
    e.cycle()
    tr = db.one("SELECT * FROM trades")
    assert tr and tr["strategy"] == "Z_test"
    e2, db2 = make(tmp_path / "x", monkeypatch, status="validated")
    db2.execute("UPDATE strategy_status SET approved=1")
    db2.kv_set("forward_status", {"Z_test": {"status": "demoted"}})
    e2.startup()
    e2.cycle()
    assert db2.one("SELECT COUNT(*) c FROM trades")["c"] == 0
