import datetime as dt

import pytest

from qt.broker.base import OrderSpec, SimBroker
from qt.db import DB
from qt.execution import TradeManager, TradePlan
from qt.ledger import Ledger

NOW = dt.datetime(2026, 11, 24, 15, 0, tzinfo=dt.timezone.utc)


class Clock:
    def __init__(self):
        self.t = NOW

    def __call__(self):
        return self.t

    def advance(self, s):
        self.t += dt.timedelta(seconds=s)


@pytest.fixture
def env(tmp_path):
    clock = Clock()
    b = SimBroker(prices={"SPY": 500.0, "QQQ": 400.0})
    db = DB(tmp_path / "t.db")
    led = Ledger(100.0, "cash")
    tm = TradeManager(b, db, led, clock=clock, sleep=lambda s: None)
    return b, db, led, tm, clock


def plan(tid="T1", sym="SPY", notional=50.0, stop=495.0, ref=500.0, **kw):
    d = dict(trade_id=tid, symbol=sym, asset_class="us_equity", strategy="A_momentum", strategy_version="1.0.0",
             notional=notional, entry_ref=ref, stop=stop, target=None, trail_atr=2.0, trail_activate_r=1.0,
             breakeven_r=0.0, max_bars=0, flatten_eod=True, atr=1.0, score=70, regime="bull", thesis="test",
             exit_plan="trail", planned_risk=0.5)
    d.update(kw)
    return TradePlan(**d)


def test_entry_fill_then_broker_stop_placed(env):
    b, db, led, tm, clock = env
    assert tm.open_trade(plan())
    t = tm.trade("T1")
    assert t["status"] == "open"
    assert led.positions["SPY"].qty == pytest.approx(50 / (500 * 1.0015), rel=1e-6)
    assert led.settled_cash == pytest.approx(100 - led.positions["SPY"].qty * 500)
    stop = tm.current_stop_order("T1")
    assert stop and stop["type"] == "stop" and stop["stop_price"] == 495.0
    assert led.reserved == 0


def test_virtual_balance_blocks_oversized_entry(env):
    b, db, led, tm, clock = env
    assert not tm.open_trade(plan(notional=150.0))
    assert db.one("SELECT COUNT(*) c FROM orders")["c"] == 0


def test_one_trade_per_symbol(env):
    b, db, led, tm, clock = env
    assert tm.open_trade(plan("T1"))
    assert not tm.open_trade(plan("T2"))


def test_stop_fill_closes_trade_and_unsettles_proceeds(env):
    b, db, led, tm, clock = env
    tm.open_trade(plan())
    b.tick("SPY", 494.0)
    tm.manage({"SPY": 494.0}, {}, clock(), False)
    t = tm.trade("T1")
    assert t["status"] == "closed" and t["exit_reason"] == "stop"
    assert t["pnl"] < 0
    assert "SPY" not in led.positions
    assert led.unsettled_cash > 0  # T+1
    assert tm.consecutive_losses == 1


def test_trailing_stop_moves_up_only(env):
    b, db, led, tm, clock = env
    tm.open_trade(plan())
    b.tick("SPY", 510.0)
    tm.manage({"SPY": 510.0}, {"SPY": {"close": 510.0, "atr": 1.0}}, clock(), False)
    assert tm.trade("T1")["stop_price"] == pytest.approx(508.0)
    assert tm.current_stop_order("T1")["stop_price"] == pytest.approx(508.0)
    tm.manage({"SPY": 505.0}, {"SPY": {"close": 505.0, "atr": 1.0}}, clock(), False)
    assert tm.trade("T1")["stop_price"] == pytest.approx(508.0)


def test_exit_cancels_stop_then_sells(env):
    b, db, led, tm, clock = env
    tm.open_trade(plan())
    tm.manage({"SPY": 501.0}, {"SPY": {"close": 501.0, "atr": 1.0, "exit_sig": True}}, clock(), False)
    t = tm.trade("T1")
    assert t["status"] == "closed" and t["exit_reason"] == "thesis_invalidated"
    kinds = {r["kind"]: r["status"] for r in db.query("SELECT kind,status FROM orders")}
    assert kinds["stop"] == "canceled" and kinds["exit"] == "filled"


def test_eod_flatten(env):
    b, db, led, tm, clock = env
    tm.open_trade(plan())
    tm.manage({"SPY": 500.0}, {}, clock(), True)
    assert tm.trade("T1")["exit_reason"] == "eod_flatten"
    assert not led.positions


def test_partial_fill_then_timeout_releases_reservation(env):
    b, db, led, tm, clock = env
    b.partial_fill_ratio = 0.4
    b.prices["SPY"] = 500.0
    tm.open_trade(plan())
    assert tm.trade("T1")["status"] == "open"
    q = led.positions["SPY"].qty
    assert led.reserved > 0  # remainder still reserved while the order works
    b.prices["SPY"] = 520.0  # moves away; remainder will not fill at the limit
    clock.advance(120)
    tm.manage({"SPY": 520.0}, {}, clock(), False)
    assert led.reserved == 0
    assert led.positions["SPY"].qty == pytest.approx(q)
    assert tm.current_stop_order("T1")["qty"] == pytest.approx(q, rel=1e-6)


def test_rejected_entry_releases_cash(env):
    b, db, led, tm, clock = env
    b.fail_next_submit = "reject"
    assert not tm.open_trade(plan())
    assert led.reserved == 0 and led.buying_power() == 100
    assert tm.trade("T1")["status"] == "entry_failed"


def test_uncertain_submission_resolved_without_duplicate(env):
    b, db, led, tm, clock = env
    b.fail_next_submit = "uncertain_sent"  # order reached the broker but the response was lost
    assert tm.open_trade(plan())
    assert len([o for o in b.orders.values() if o["side"] == "buy"]) == 1
    assert tm.trade("T1")["status"] == "open"


def test_uncertain_lost_is_retried_with_same_id(env):
    b, db, led, tm, clock = env
    b.fail_next_submit = "uncertain_lost"  # order never reached the broker
    assert tm.open_trade(plan())
    buys = [o for o in b.orders.values() if o["side"] == "buy"]
    assert len(buys) == 1 and buys[0]["client_order_id"] == "qt-T1-entry"


def test_broker_outage_flags_integrity_and_no_blind_retry(env):
    b, db, led, tm, clock = env
    b.offline = True
    tm.open_trade(plan())
    row = db.one("SELECT * FROM orders WHERE client_order_id='qt-T1-entry'")
    assert row["status"] == "unknown"
    assert tm.om.integrity_issues
    b.offline = False
    tm.om.poll_open()  # reconciliation: the broker never got it
    assert len(b.orders) == 0


def test_duplicate_submit_suppressed(env):
    b, db, led, tm, clock = env
    tm.open_trade(plan())
    spec = OrderSpec("qt-T1-entry", "SPY", "buy", "limit", qty=0.01, limit_price=600)
    tm.om.submit(spec, "T1", "entry", 500)
    assert len([o for o in b.orders.values() if o["side"] == "buy"]) == 1


def test_software_stop_when_broker_stop_missing(env):
    b, db, led, tm, clock = env
    tm.open_trade(plan())
    so = tm.current_stop_order("T1")
    b.cancel_order(so["broker_order_id"])  # broker stop vanished (e.g., expired)
    tm.om.poll_open()
    b.prices["SPY"] = 494.0  # price below stop without the broker stop to catch it
    tm.manage({"SPY": 494.0}, {}, clock(), False)
    t = tm.trade("T1")
    assert t["status"] == "closed" and t["exit_reason"] == "software_stop"


def test_restart_recovers_fills_that_happened_offline(env, tmp_path):
    b, db, led, tm, clock = env
    tm.open_trade(plan())
    # process dies; while down, the stop fills at the broker
    b.tick("SPY", 490.0)
    led2 = Ledger.from_dict(db.kv_get("ledger"))
    tm2 = TradeManager(b, db, led2, clock=clock, sleep=lambda s: None)
    tm2.om.poll_open()
    assert tm2.trade("T1")["status"] == "closed"
    assert "SPY" not in led2.positions


def test_overnight_trade_survives_flatten_and_stop_is_replaced_after_expiry(env):
    """Daily (swing) trades are not flattened at the close; when the DAY stop expires, a new
    protective stop is placed so the position is never left without one during market hours."""
    b, db, led, tm, clock = env
    assert tm.open_trade(plan(flatten_eod=False, timeframe="1Day", max_bars=10))
    first = tm.current_stop_order("T1")
    assert first is not None
    tm.manage({"SPY": 500.0}, {}, clock(), flatten_now=True)
    assert tm.trade("T1")["status"] == "open"            # not flattened
    # the broker expires the DAY stop after the close
    b.orders[first["broker_order_id"]]["status"] = "expired"
    clock.advance(16 * 3600)
    tm.om.poll_open()
    assert tm.current_stop_order("T1") is None
    # pre-open: a thin extended-hours print below the stop must not sell, and no stop is submitted yet
    tm.manage({"SPY": 480.0}, {}, clock(), False, market_open=False)
    assert tm.trade("T1")["status"] == "open" and tm.current_stop_order("T1") is None
    tm.manage({"SPY": 501.0}, {}, clock(), False)
    again = tm.current_stop_order("T1")
    assert again is not None and again["client_order_id"].endswith("-stop2")
    assert again["stop_price"] == first["stop_price"]
    assert led.positions["SPY"].qty > 0
