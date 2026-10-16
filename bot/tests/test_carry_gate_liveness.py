"""The operator can see when the liquidation gate has stopped being read.

A held pair's protection is the liquidation-distance exit run on every tick.
If the tick keeps skipping (an unusable market view, a read that fails before
the gate) the pair sits un-evaluated and nothing said so: health stayed green.
`carry_status` now reports when the gate last got a usable read and warns -
without making health unhealthy, so a transient never restarts the machine -
when a required gate has not read for longer than the loop can explain.
Real `TradingBot.carry_status`, real `CarryEngine` for the stamp; offline.
"""
from __future__ import annotations

import os
import sys
import time
from types import SimpleNamespace
from unittest import mock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import main as _main  # noqa: E402
from test_carry_venue_direction_integration import (  # noqa: E402
    broker_with_rows, held_engine)

POS = {"side": "Sell", "size": "0.001", "positionIdx": 0, "markPrice": "100000",
       "positionIM": "20", "positionMM": "5", "liqPrice": "130000"}


def status_for(engine, *, loop_s=60.0):
    bot = _main.TradingBot.__new__(_main.TradingBot)
    bot.carry = engine
    bot.carry_journal = None
    bot.cfg = SimpleNamespace(LOOP_INTERVAL_SECONDS=loop_s, USE_TESTNET=True,
                              PAPER_TRADING=True)
    return bot.carry_status()


def gated(row=POS):
    engine = held_engine(broker_with_rows(row))
    engine.require_liquidation_check = True
    return engine


def test_the_engine_stamps_each_usable_gate_read():
    engine = gated()
    assert engine.last_risk_read_ms is None
    before = int(time.time() * 1000)
    assert engine._check_margin() is None
    assert before <= engine.last_risk_read_ms <= int(time.time() * 1000)


def test_an_unreadable_gate_does_not_stamp():
    engine = gated(dict(POS, liqPrice="nan"))
    engine._check_margin()
    assert engine.last_risk_read_ms is None


def test_a_fresh_read_reports_its_age_and_no_warning():
    engine = gated()
    engine._check_margin()
    s = status_for(engine)
    gate = s["liquidation_gate"]
    assert gate["required"] is True and gate["stale"] is False
    assert 0 <= gate["age_s"] < 5
    assert s["warnings"] == []
    assert s["blocking_fault"] == ""


def test_a_held_pair_whose_gate_never_read_is_warned_about():
    s = status_for(gated())
    assert s["liquidation_gate"]["last_read_ms"] is None
    assert s["liquidation_gate"]["stale"] is True
    assert "LIQUIDATION_GATE_NOT_READ" in s["warnings"]


def test_a_gate_not_read_for_longer_than_the_loop_explains_is_warned_about():
    engine = gated()
    engine._check_margin()
    engine.last_risk_read_ms = int(time.time() * 1000) - 10 * 60 * 1000
    s = status_for(engine, loop_s=60.0)
    assert s["liquidation_gate"]["stale"] is True
    assert "LIQUIDATION_GATE_NOT_READ" in s["warnings"]
    assert s["blocking_fault"] == ""           # informative, never a restart


def test_one_missed_tick_is_not_a_warning():
    engine = gated()
    engine._check_margin()
    engine.last_risk_read_ms = int(time.time() * 1000) - 90 * 1000
    assert status_for(engine, loop_s=60.0)["warnings"] == []


def test_a_slower_loop_widens_the_bound():
    engine = gated()
    engine._check_margin()
    engine.last_risk_read_ms = int(time.time() * 1000) - 5 * 60 * 1000
    assert status_for(engine, loop_s=600.0)["warnings"] == []
    assert "LIQUIDATION_GATE_NOT_READ" in status_for(
        engine, loop_s=30.0)["warnings"]


def test_a_flat_book_has_nothing_to_warn_about():
    engine = gated()
    engine.position = None
    s = status_for(engine)
    assert s["warnings"] == []
    assert s["liquidation_gate"]["required"] is True


def test_an_engine_that_does_not_require_the_gate_is_not_warned():
    engine = gated()
    engine.require_liquidation_check = False
    s = status_for(engine)
    assert s["liquidation_gate"]["required"] is False
    assert s["warnings"] == []


def test_health_stays_healthy_on_a_warning():
    engine = gated()
    bot = _main.TradingBot.__new__(_main.TradingBot)
    bot.carry, bot.carry_journal = engine, None
    bot.cfg = SimpleNamespace(LOOP_INTERVAL_SECONDS=60.0, USE_TESTNET=True,
                              PAPER_TRADING=True)
    carry = bot.carry_status()
    assert "LIQUIDATION_GATE_NOT_READ" in carry["warnings"]
    assert carry["blocking_fault"] == ""


def test_an_unusable_loop_interval_cannot_blank_the_status():
    """A diagnostic must not be able to take the whole carry status down: a
    config without a usable interval falls back to the default one."""
    engine = gated()
    engine._check_margin()
    for bad in (mock.Mock(), "soon", None, float("nan"), -5.0, 0.0):
        bot = _main.TradingBot.__new__(_main.TradingBot)
        bot.carry, bot.carry_journal = engine, None
        bot.cfg = SimpleNamespace(LOOP_INTERVAL_SECONDS=bad, USE_TESTNET=True,
                                  PAPER_TRADING=True)
        s = bot.carry_status()
        assert s.get("blocking_fault") != "STATUS_UNREADABLE", bad
        assert s["liquidation_gate"]["stale"] is False
