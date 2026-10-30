"""A position whose FIRST liquidation-distance read is already below the floor
was opened inside the floor, and it will be again.

At 10x isolated leverage the liquidation price is about 9-10% above entry,
which is below the engine's 15% floor at the moment of entry. The entry gate does
not see that (there is no liquidation price before a position exists), so the
engine opened, the next tick's hold check unwound, the entry gate opened again:
830 round trips on the real replay, each paying fees and basis, net -190%/yr.
A mid-hold breach (price rallied, distance shrank) is a correct risk exit and
stays a plain unwind and a re-entry later is legitimate. The engine halts only
when BOTH hold: it is the first read of the position and the venue's liquidation
price sat inside the floor relative to the ENTRY price. That is a contradiction
between configuration and floor: it flattens, trips the kill switch and waits for
a human instead of repeating. Cadence cannot fake it: a real squeeze that happens
to be the first read has an entry distance above the floor and is a plain unwind.
Offline; stub venue.
"""
from __future__ import annotations

import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "tools"))

from carry_engine import MIN_LIQUIDATION_DISTANCE_PCT, BookState  # noqa: E402
from test_liquidation_gate_fail_closed import (ISOLATED, healthy_view,  # noqa: E402
                                               opened, tick)

BELOW = MIN_LIQUIDATION_DISTANCE_PCT - 5.0
MARK = 100_000.0


def _inside_view():
    """Liquidation price only BELOW% above the entry price: opened inside the floor."""
    return healthy_view(distance_pct=BELOW, margin_mode=ISOLATED,
                        liq_price=MARK * (1.0 + BELOW / 100.0))


def _tripped(engine):
    calls = []
    engine.kill_switch = calls.append
    return calls


def test_a_breach_on_the_first_read_flattens_then_halts_and_trips_the_kill_switch():
    e, venue = opened()
    calls = _tripped(e)
    venue.view = _inside_view()
    d = tick(e)
    assert d.action == "halted"
    assert d.reason == "LIQUIDATION_FLOOR_BREACHED_AT_ENTRY"
    assert venue.orders, "the position must be flattened before the halt"
    assert e.position is None and e.state is BookState.HALTED
    assert calls == ["LIQUIDATION_FLOOR_BREACHED_AT_ENTRY"]


def test_a_real_squeeze_that_is_the_first_read_is_a_plain_unwind_not_a_halt():
    """The replay reads every 8 hours, so a position's first read can follow a
    genuine rally. The liquidation price is 30% above the entry price, so the
    position was NOT opened inside the floor; it is now close to it."""
    e, venue = opened()
    calls = _tripped(e)
    venue.view = healthy_view(distance_pct=BELOW, margin_mode=ISOLATED,
                              liq_price=MARK * 1.30)
    d = tick(e)
    assert d.action == "unwound" and d.reason.startswith("LIQUIDATION_")
    assert e.state is not BookState.HALTED and calls == []


def test_a_breach_after_a_healthy_read_is_an_ordinary_unwind():
    e, venue = opened()
    calls = _tripped(e)
    assert tick(e).action == "hold"                       # a healthy first read
    venue.view = _inside_view()      # even an entry-inside-floor view: it had a healthy read
    d = e.on_candle(mark=100_000.0, funding_bps=3.0, spot=100_000.0,
                    funding_print_ms=5 * 8 * 3600 * 1000,
                    timestamp_ms=5 * 8 * 3600 * 1000)
    assert d.action == "unwound" and d.reason.startswith("LIQUIDATION_")
    assert e.state is not BookState.HALTED and calls == []


def test_a_halted_book_does_not_reopen():
    e, venue = opened()
    venue.view = _inside_view()
    tick(e)
    venue.orders.clear()
    d = e.on_candle(mark=100_000.0, funding_bps=3.0, spot=100_000.0,
                    funding_print_ms=6 * 8 * 3600 * 1000,
                    timestamp_ms=6 * 8 * 3600 * 1000)
    assert d.action == "halted" and not venue.orders


def test_the_floor_itself_is_still_exclusive_on_a_healthy_book():
    e, venue = opened()
    assert tick(e).action == "hold"
    venue.view = healthy_view(distance_pct=MIN_LIQUIDATION_DISTANCE_PCT)
    d = e.on_candle(mark=100_000.0, funding_bps=3.0, spot=100_000.0,
                    funding_print_ms=5 * 8 * 3600 * 1000,
                    timestamp_ms=5 * 8 * 3600 * 1000)
    assert d.action == "hold"


class TestOnTheReplay:
    def test_ten_x_no_longer_churns_the_book_into_ruin(self):
        import carry_replay as rp
        from test_carry_replay import rows
        data = rows(150, price0=30_000.0)
        liq = rp.LiquidationInputs.declared_isolated(10.0, "test scenario")
        report = rp.replay(data, notional=100_000.0, borrow_apr=0.0, liquidation=liq)
        assert report["halted_at_ms"], "the book must stop, not repeat"
        assert report["trades"] <= 2
        assert report["net_usd"] > -5_000.0
