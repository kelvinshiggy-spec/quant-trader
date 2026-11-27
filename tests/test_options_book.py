"""Simulated options book: contract choice, budget, ask/bid fills, mandatory stop (synthetic quotes, not a result)."""
import datetime as dt

from qt.broker.base import SimBroker
from qt.db import DB
from qt.options_book import CAPITAL, OptionsBook, RULES

NOW = dt.datetime(2026, 11, 24, 15, 0, tzinfo=dt.timezone.utc)


class Sig:
    def __init__(self, sym="ABC", px=20.0, stop=18.0, strategy="E_trend_following"):
        self.symbol, self.price, self.stop, self.strategy, self.score = sym, px, stop, strategy, 60.0


def chain():
    return {
        "ABC261231C00020000": {"underlying": "ABC", "bid": 0.42, "ask": 0.45, "delta": 0.52},   # fits, best delta
        "ABC261231C00018000": {"underlying": "ABC", "bid": 2.40, "ask": 2.50, "delta": 0.75},   # delta too high
        "ABC261231C00025000": {"underlying": "ABC", "bid": 0.10, "ask": 0.20, "delta": 0.20},   # delta too low
        "ABC261204C00020000": {"underlying": "ABC", "bid": 0.50, "ask": 0.55, "delta": 0.50},   # expires too soon
        "ABC270115C00021000": {"underlying": "ABC", "bid": 0.30, "ask": 0.60, "delta": 0.45},   # spread too wide
    }


def test_buys_best_call_at_ask_and_stops_at_half_premium(tmp_path):
    db = DB(tmp_path / "o.db")
    ob = OptionsBook(db)
    b = SimBroker(option_data=chain(), now=NOW)
    log = ob.consider([Sig()], b, NOW)
    t = db.one("SELECT * FROM options_trades")
    assert t["contract"] == "ABC261231C00020000" and t["entry_premium"] == 0.45, log
    assert abs(t["stop_premium"] - 0.225) < 1e-9 and t["cost"] <= CAPITAL * RULES["max_premium_frac"]
    assert db.one("SELECT COUNT(*) c FROM orders")["c"] == 0          # never an order
    # option halves: stop exits at the BID
    b.option_data["ABC261231C00020000"].update(bid=0.20, ask=0.24)
    ob.manage(b, {}, NOW + dt.timedelta(days=1), {"ABC": 19.0})
    t = db.one("SELECT * FROM options_trades")
    assert t["status"] == "closed" and t["exit_reason"] == "stop" and t["exit_premium"] == 0.20
    assert t["pnl"] < -(0.45 - 0.20) * 100 and t["r"] < -1.0          # fees included
    s = ob.stats()
    assert s["closed_trades"] == 1 and s["equity"] < CAPITAL


def test_nothing_affordable_or_suitable_is_skipped(tmp_path):
    db = DB(tmp_path / "o.db")
    ob = OptionsBook(db)
    data = {"XYZ261231C00300000": {"underlying": "XYZ", "bid": 7.0, "ask": 7.2, "delta": 0.5}}  # $720 > budget
    out = ob.consider([Sig("XYZ", 300.0, 290.0)], SimBroker(option_data=data, now=NOW), NOW)
    assert db.one("SELECT COUNT(*) c FROM options_trades")["c"] == 0 and "no call fits" in out[0]["result"]


def test_breakeven_after_double_and_thesis_exit(tmp_path):
    db = DB(tmp_path / "o.db")
    ob = OptionsBook(db)
    b = SimBroker(option_data=chain(), now=NOW)
    ob.consider([Sig()], b, NOW)
    b.option_data["ABC261231C00020000"].update(bid=0.95, ask=0.99)
    ob.manage(b, {}, NOW + dt.timedelta(days=2), {"ABC": 23.0})
    t = db.one("SELECT * FROM options_trades")
    assert t["status"] == "open" and t["stop_premium"] == t["entry_premium"]   # no profit cap, stop at breakeven
    ob.manage(b, {}, NOW + dt.timedelta(days=3), {"ABC": 17.5})                # stock closed below the signal stop
    t = db.one("SELECT * FROM options_trades")
    assert t["status"] == "closed" and t["exit_reason"] == "thesis_invalidated" and t["pnl"] > 0


def test_live_mode_sends_paper_orders_and_books_only_fills(tmp_path):
    db = DB(tmp_path / "o.db")
    data = chain()
    b = SimBroker(option_data=data, now=NOW, prices={"ABC261231C00020000": 0.44})
    ob = OptionsBook(db, b, live=True)
    ob.consider([Sig()], b, NOW)
    o = [x for x in b.orders.values() if x["symbol"] == "ABC261231C00020000"][0]
    assert o["side"] == "buy" and o["type"] == "limit" and o["limit_price"] == 0.45 and o["qty"] == 1.0
    t = db.one("SELECT * FROM options_trades")
    assert t["status"] == "pending_entry"
    ob.poll(NOW)                                                       # broker filled at 0.44
    t = db.one("SELECT * FROM options_trades")
    assert t["status"] == "open" and t["entry_premium"] == 0.44 and abs(t["stop_premium"] - 0.22) < 1e-9
    assert ob.contracts_held() == {"ABC261231C00020000"}
    # stop: sell order at the bid, booked when filled
    data["ABC261231C00020000"].update(bid=0.20, ask=0.24)
    b.prices["ABC261231C00020000"] = 0.20
    ob.manage(b, {}, NOW + dt.timedelta(days=1), {"ABC": 19.0})
    assert db.one("SELECT status FROM options_trades")["status"] == "exiting"
    ob.poll(NOW + dt.timedelta(days=1))
    t = db.one("SELECT * FROM options_trades")
    assert t["status"] == "closed" and t["exit_reason"] == "stop" and t["exit_premium"] == 0.20 and t["pnl"] < 0
    assert ob.state()["ledger"]["reservations"] == {}


def test_live_entry_not_filled_is_cancelled_and_cash_released(tmp_path):
    db = DB(tmp_path / "o.db")
    b = SimBroker(option_data=chain(), now=NOW, prices={"ABC261231C00020000": 0.60})  # market ran away
    ob = OptionsBook(db, b, live=True)
    ob.consider([Sig()], b, NOW)
    ob.poll(NOW + dt.timedelta(minutes=5))   # cancels
    ob.poll(NOW + dt.timedelta(minutes=6))   # sees it cancelled
    assert db.one("SELECT status FROM options_trades")["status"] == "entry_failed"
    assert ob.state()["ledger"]["reservations"] == {}
