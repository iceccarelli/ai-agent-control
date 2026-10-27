"""A position row that does not STATE `liqPrice` (or a usable mark) is unreadable.

`get_liquidation_view` read an ABSENT `liqPrice` key exactly like Bybit's
spelling of "no reachable liquidation price" (`""`), and a held row with no
usable `markPrice` was returned with mark 0 / NaN and the same "safest state"
reason. On account-level margin the per-position distance is then skipped, so a
truncated or renamed-field payload on a live short passed the liquidation gate.
Only the empty string is the venue saying "unreachable"; absence is not.
Real `CarryBroker` over Bybit-shaped payloads, real `CarryEngine`; offline.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from carry_broker import PairIncident  # noqa: E402
from carry_engine import BookState  # noqa: E402
from test_carry_unstated_numbers import POS, without  # noqa: E402
from test_carry_venue_direction_integration import (  # noqa: E402
    broker_with_rows, held_engine)

HELD = dict(POS, liqPrice="")

UNSTATED = [
    pytest.param(POS, id="liqPrice-key-absent"),
    pytest.param(dict(POS, liqPrice=None), id="liqPrice-null"),
    pytest.param(without(HELD, "markPrice"), id="mark-absent-liq-blank"),
    pytest.param(dict(HELD, markPrice="0"), id="mark-zero-liq-blank"),
    pytest.param(dict(HELD, markPrice="nan"), id="mark-nan-liq-blank"),
    pytest.param(dict(HELD, markPrice="-1"), id="mark-negative-liq-blank"),
]


@pytest.mark.parametrize("row", UNSTATED)
def test_an_unstated_liquidation_field_is_unreadable_not_unreachable(row):
    with pytest.raises(PairIncident):
        broker_with_rows(row).get_liquidation_view("BTCUSDT")


@pytest.mark.parametrize("row", UNSTATED)
def test_the_engine_halts_on_an_unstated_liquidation_field(row):
    engine = held_engine(broker_with_rows(row))
    engine.require_liquidation_check = True
    engine.snapshot = None
    d = engine._check_liquidation_distance()
    assert d is not None and d.state is BookState.HALTED
    assert d.reason == "LIQUIDATION_PRICE_UNREADABLE"


def test_the_venues_own_unreachable_spelling_with_a_real_mark_still_passes():
    v = broker_with_rows(HELD).get_liquidation_view("BTCUSDT")
    assert v["reason"] == "BEYOND_VENUE_PRICE_BOUNDS" and v["mark"] == 100000.0
    engine = held_engine(broker_with_rows(HELD))
    engine.require_liquidation_check = True
    assert engine._check_liquidation_distance() is None


def test_a_flat_row_needs_neither_field():
    flat = dict(POS, size="0")
    v = broker_with_rows(flat).get_liquidation_view("BTCUSDT")
    assert v["reason"] == "NO_POSITION"
