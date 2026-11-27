import datetime as dt

import numpy as np
import pandas as pd
import pytest

from qt.config import (LIVE_CONFIRMATION_PHRASE, ConfigError, RiskLimits, check_within_ceiling,
                       load_settings)
from qt.costs import CostModel
from qt.guardian import AccountState, CapitalGuardian, Proposal
from qt.ledger import Ledger, LedgerViolation
from qt.markettime import is_early_close, next_trading_day, pt_schedule_for, settlement_date

UTC = dt.timezone.utc
T0 = dt.datetime(2026, 11, 24, 15, 0, tzinfo=UTC)  # a Tuesday


# ---------------- config / mode isolation ----------------
def test_default_is_paper_and_requires_keys():
    with pytest.raises(ConfigError):
        load_settings({})
    s = load_settings({"ALPACA_PAPER_API_KEY": "PKX", "ALPACA_PAPER_API_SECRET": "y"})
    assert s.is_paper and s.mode == "paper"


def test_ambiguous_mode_fails_closed():
    with pytest.raises(ConfigError):
        load_settings({"TRADING_MODE": "Paper-ish", "ALPACA_PAPER_API_KEY": "PK", "ALPACA_PAPER_API_SECRET": "s"})


def test_live_requires_all_three_switches_and_separate_keys():
    base = {"TRADING_MODE": "live", "ALPACA_LIVE_API_KEY": "AKL", "ALPACA_LIVE_API_SECRET": "s"}
    with pytest.raises(ConfigError):
        load_settings(base)
    with pytest.raises(ConfigError):
        load_settings({**base, "LIVE_TRADING_ENABLED": "true"})
    with pytest.raises(ConfigError):  # paper-looking key in live slot
        load_settings({**base, "ALPACA_LIVE_API_KEY": "PK1", "LIVE_TRADING_ENABLED": "true",
                       "LIVE_TRADING_CONFIRMATION": LIVE_CONFIRMATION_PHRASE})
    with pytest.raises(ConfigError):  # identical paper/live keys
        load_settings({**base, "ALPACA_PAPER_API_KEY": "AKL", "LIVE_TRADING_ENABLED": "true",
                       "LIVE_TRADING_CONFIRMATION": LIVE_CONFIRMATION_PHRASE})
    s = load_settings({**base, "LIVE_TRADING_ENABLED": "true", "LIVE_TRADING_CONFIRMATION": LIVE_CONFIRMATION_PHRASE})
    assert s.mode == "live"


def test_live_key_in_paper_slot_rejected():
    with pytest.raises(ConfigError):
        load_settings({"ALPACA_PAPER_API_KEY": "AKREAL", "ALPACA_PAPER_API_SECRET": "s"})


def test_redacted_never_contains_secrets():
    s = load_settings({"ALPACA_PAPER_API_KEY": "PKSECRET", "ALPACA_PAPER_API_SECRET": "TOPSECRET"})
    txt = str(s.redacted())
    assert "TOPSECRET" not in txt and "PKSECRET" not in txt


def test_risk_ceiling_blocks_loosening():
    ceil = RiskLimits()
    looser = RiskLimits(risk_per_trade_pct=1.0, max_positions=5)
    errs = check_within_ceiling(looser, ceil)
    assert len(errs) == 2
    tighter = RiskLimits(risk_per_trade_pct=0.25, max_positions=1)
    assert check_within_ceiling(tighter, ceil) == []


# ---------------- calendar ----------------
def test_settlement_t_plus_1_skips_weekend_and_holiday():
    assert settlement_date(dt.date(2026, 11, 20)) == dt.date(2026, 11, 23)  # Fri -> Mon
    assert settlement_date(dt.date(2026, 11, 25)) == dt.date(2026, 11, 27)  # Thanksgiving skipped
    assert settlement_date(dt.date(2026, 11, 25), "crypto") == dt.date(2026, 11, 25)
    assert next_trading_day(dt.date(2026, 12, 24)) == dt.date(2026, 12, 28)


def test_early_close_and_pt_schedule():
    assert is_early_close(dt.date(2026, 11, 27))
    sch = pt_schedule_for(dt.date(2026, 11, 27))
    assert sch["close"].strftime("%H:%M") == "10:00"  # 1 PM ET = 10 AM PT
    assert sch["open"].strftime("%H:%M") == "06:30"
    summer = pt_schedule_for(dt.date(2026, 7, 7))  # DST
    assert summer["open"].strftime("%H:%M") == "06:30"
    assert pt_schedule_for(dt.date(2026, 11, 26)) == {"trading_day": False}


# ---------------- ledger ----------------
def test_virtual_100_balance_enforced():
    led = Ledger(100.0, "cash")
    with pytest.raises(LedgerViolation):
        led.reserve("o1", 150.0)
    led.reserve("o1", 60.0)
    with pytest.raises(LedgerViolation):
        led.reserve("o2", 50.0)  # only 40 left after reservation
    assert led.buying_power() == pytest.approx(40.0)


def test_cash_account_proceeds_unsettled_until_t1():
    led = Ledger(100.0, "cash")
    led.apply_buy_fill("SPY", 0.1, 500.0, 0.0, T0, "s", "t1")
    assert led.settled_cash == pytest.approx(50.0)
    led.apply_sell_fill("SPY", 0.1, 510.0, 0.01, T0)
    assert led.settled_cash == pytest.approx(50.0)
    assert led.unsettled_cash == pytest.approx(50.99)
    assert led.buying_power() == pytest.approx(50.0)  # proceeds NOT reusable today
    with pytest.raises(LedgerViolation):
        led.apply_buy_fill("QQQ", 0.2, 400.0, 0.0, T0, "s", "t2")  # $80 > settled $50
    led.settle(dt.date(2026, 11, 25))
    assert led.settled_cash == pytest.approx(100.99)
    assert led.realized_pnl == pytest.approx(0.99)


def test_margin_1x_reuses_proceeds_but_no_leverage():
    led = Ledger(100.0, "margin_1x")
    led.apply_buy_fill("SPY", 0.1, 500.0, 0.0, T0, "s", "t1")
    led.apply_sell_fill("SPY", 0.1, 500.0, 0.0, T0)
    assert led.buying_power() == pytest.approx(100.0)
    with pytest.raises(LedgerViolation):
        led.apply_buy_fill("QQQ", 1, 150.0, 0.0, T0, "s", "t2")


def test_no_short_no_adding_and_partial_fills():
    led = Ledger(100.0)
    with pytest.raises(LedgerViolation):
        led.apply_sell_fill("SPY", 1, 10, 0, T0)
    led.reserve("o1", 50)
    led.apply_buy_fill("SPY", 0.04, 500.0, 0.0, T0, "s", "t1", order_key="o1")  # partial: $20
    assert led.reserved == pytest.approx(30.0)
    led.apply_buy_fill("SPY", 0.06, 500.0, 0.0, T0, "s", "t1", order_key="o1")  # rest
    assert led.reserved == pytest.approx(0.0)
    assert led.positions["SPY"].qty == pytest.approx(0.1)
    with pytest.raises(LedgerViolation):
        led.apply_buy_fill("SPY", 0.01, 500.0, 0.0, T0, "s", "OTHER")  # averaging into a new trade
    with pytest.raises(LedgerViolation):
        led.apply_sell_fill("SPY", 0.2, 500, 0, T0)  # more than held
    led.apply_sell_fill("SPY", 0.05, 520, 0, T0)
    assert led.positions["SPY"].qty == pytest.approx(0.05)
    assert led.realized_pnl == pytest.approx(1.0)


def test_ledger_roundtrip_persistence():
    led = Ledger(100.0)
    led.apply_buy_fill("SPY", 0.1, 500.0, 0.0, T0, "s", "t1")
    led.apply_sell_fill("SPY", 0.05, 510.0, 0.0, T0)
    led2 = Ledger.from_dict(led.to_dict())
    assert led2.to_dict() == led.to_dict()
    assert led2.check_invariants() == []


# ---------------- guardian ----------------
def _state(**kw):
    base = dict(now=T0, equity=100.0, day_start_equity=100.0, week_start_equity=100.0, peak_equity=100.0,
                session_peak_equity=100.0, buying_power=100.0, exposure=0.0, open_positions=0)
    base.update(kw)
    return AccountState(**base)


def _prop(**kw):
    base = dict(symbol="SPY", asset_class="us_equity", strategy="x", entry_price=100.0, stop_price=99.0,
                round_trip_cost_bps=5.0, spread_bps=1.0, data_age_s=1.0, atr=0.5)
    base.update(kw)
    return Proposal(**base)


def test_position_sizing_risk_and_alloc_caps():
    g = CapitalGuardian(RiskLimits())
    d = g.evaluate(_prop(stop_price=98.0), _state())  # risk 0.5$ / ~2.05$ per share -> ~$24 notional
    assert d.approved and d.notional == pytest.approx(24.39, abs=0.05)
    assert d.risk_dollars <= 0.5 + 1e-9
    d2 = g.evaluate(_prop(stop_price=99.9), _state())  # tight stop -> capped at 50% allocation
    assert d2.approved and d2.notional == pytest.approx(50.0)


def test_guardian_limits_block_entries():
    g = CapitalGuardian(RiskLimits())
    assert not g.evaluate(_prop(), _state(equity=97.9)).approved                     # daily -2.1%
    assert not g.evaluate(_prop(), _state(equity=98.5, day_start_equity=98.5, week_start_equity=104)).approved  # weekly
    assert not g.evaluate(_prop(), _state(equity=89.9, day_start_equity=89.9, week_start_equity=89.9)).approved  # drawdown
    assert not g.evaluate(_prop(), _state(open_positions=2)).approved
    assert not g.evaluate(_prop(), _state(exposure=80.0)).approved
    assert not g.evaluate(_prop(), _state(emergency=True)).approved
    assert not g.evaluate(_prop(), _state(manual_pause=True)).approved
    assert not g.evaluate(_prop(), _state(integrity_ok=False, integrity_reasons=("recon mismatch",))).approved
    assert not g.evaluate(_prop(data_age_s=999), _state()).approved
    assert not g.evaluate(_prop(spread_bps=80), _state()).approved
    assert not g.evaluate(_prop(stop_price=101), _state()).approved
    assert not g.evaluate(_prop(), _state(open_symbols=("SPY",))).approved
    assert not g.evaluate(_prop(max_corr_with_open=0.95), _state()).approved
    assert not g.evaluate(_prop(expected_net_r=-0.1), _state()).approved
    cool = _state(consecutive_losses=3, last_loss_at=T0 - dt.timedelta(minutes=10))
    assert not g.evaluate(_prop(), cool).approved
    later = _state(consecutive_losses=3, last_loss_at=T0 - dt.timedelta(minutes=90))
    assert g.evaluate(_prop(), later).approved


def test_profit_giveback_is_protection_not_a_ceiling():
    g = CapitalGuardian(RiskLimits())
    # big gains do NOT stop trading
    assert g.evaluate(_prop(), _state(equity=130, session_peak_equity=130)).approved
    # giving back >50% of a session peak profit pauses new entries
    assert not g.evaluate(_prop(), _state(equity=101.0, session_peak_equity=103.0, peak_equity=103.0)).approved


def test_principal_protection_floor_equal_to_capital_blocks_risk():
    g = CapitalGuardian(RiskLimits(principal_protection_enabled=True, principal_floor=100.0))
    d = g.evaluate(_prop(), _state())
    assert not d.approved and any("principal" in r for r in d.reasons)
    g2 = CapitalGuardian(RiskLimits(principal_protection_enabled=True, principal_floor=99.0))
    d2 = g2.evaluate(_prop(stop_price=98), _state())
    assert d2.approved and d2.risk_dollars <= 0.25 + 1e-9  # 25% of the $1 cushion


def test_too_small_to_size_rejected():
    g = CapitalGuardian(RiskLimits())
    d = g.evaluate(_prop(), _state(buying_power=0.5))
    assert not d.approved and "too small" in d.reasons[0]
    d = g.evaluate(_prop(fractionable=False, entry_price=500, stop_price=490), _state())
    assert not d.approved  # can't buy one whole $500 share inside a $50 cap


# ---------------- costs ----------------
def test_costs_direction_and_stress():
    c = CostModel()
    assert c.buy_price(100, "us_equity", 2) > 100 > c.sell_price(100, "us_equity", 2)
    assert c.stop_sell_price(99, 97, "us_equity", 2) < 97  # gap through stop fills at open, worse
    assert c.fee("buy", 1, 100, "us_equity") == 0
    assert c.fee("sell", 1, 100, "us_equity") > 0
    assert c.fee("buy", 1, 100, "crypto") == pytest.approx(0.25)
    s2 = CostModel(stress=2.0)
    assert s2.round_trip_bps("us_equity", s2.spread_bps("us_equity", 1e8)) > c.round_trip_bps("us_equity", c.spread_bps("us_equity", 1e8))
