"""Hedge-mode accounts: refuse at start instead of failing at every order.

The adapter's order bodies carry no ``positionIdx``, which only a one-way
account accepts. On a hedge-mode account (``/v5/position/list`` returns the
positionIdx 1 and 2 rows even when flat) every order is rejected, so a book
that starts there either never opens or, worse, cannot place the reduce-only
buy-back of a short it already holds. Start is refused on positive evidence of
hedge mode, with nothing sent. Offline; rows follow Bybit's documented shape.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from test_carry_venue_direction_integration import (  # noqa: E402
    SHORT, cold_bot)

FLAT_ONE_WAY = {"side": "", "size": "0", "positionIdx": 0}
FLAT_LONG_SLOT = {"side": "", "size": "0", "positionIdx": 1}
FLAT_SHORT_SLOT = {"side": "", "size": "0", "positionIdx": 2}


def test_a_flat_hedge_mode_account_does_not_start():
    bot, client = cold_bot([FLAT_LONG_SLOT, FLAT_SHORT_SLOT])
    assert bot.carry_cold_start() is False
    assert not client.created


def test_a_held_short_in_the_hedge_short_slot_does_not_start_either():
    bot, client = cold_bot([FLAT_LONG_SLOT,
                            dict(SHORT, positionIdx=2)])
    assert bot.carry_cold_start() is False
    assert not client.created


def test_a_flat_one_way_account_starts():
    bot, _ = cold_bot([FLAT_ONE_WAY])
    assert bot.carry_cold_start() is True


def test_an_empty_position_list_still_starts():
    """No row proves nothing about the mode; the venue rejects a mismatched
    order without exposure, so this stays as it was."""
    bot, _ = cold_bot([])
    assert bot.carry_cold_start() is True


@pytest.mark.parametrize("idx", ["1", "2", 1, 2])
def test_either_encoding_of_a_hedge_slot_is_recognised(idx):
    bot, _ = cold_bot([{"side": "", "size": "0", "positionIdx": idx}])
    assert bot.carry_cold_start() is False
