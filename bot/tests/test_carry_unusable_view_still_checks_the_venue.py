"""An unusable market view must not blind a HELD pair.

When `take_snapshot`/`assert_fresh` fails (a venue read down, a missed
settlement, a clock skew), `tick()` logged "the book is NOT touched" and
returned. Every held-pair check lives in `on_candle`, which was never reached,
so a short whose venue size had drifted - or could no longer be read at all -
sat unmonitored for as long as the view stayed unusable, with nothing halted
and nobody paged. The venue-position check is a direct venue read that needs
no market view; it now runs on that path too. No order is sent there.
Real `TradingBot.tick()` -> real engine -> LedgerBroker -> CarryBroker ->
scripted venue; offline.
"""
from __future__ import annotations

import os
import sys
from unittest import mock

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from bybit_connection import TransientAPIError  # noqa: E402
from carry_engine import BookState  # noqa: E402
from market_snapshot import StaleMarket  # noqa: E402
from test_carry_tick_safety import _flat, _hedged  # noqa: E402


def tick_with_unusable_view(bot):
    with mock.patch("market_snapshot.take_snapshot",
                    side_effect=StaleMarket("venue clock differs by 120s")):
        bot.tick()


def test_a_drifted_short_is_caught_even_without_a_view(tmp_path):
    venue, journal, engine, bot = _hedged(tmp_path)
    before = len(venue.created)
    venue.perp_qty = 0.6                        # the short was cut at the venue
    tick_with_unusable_view(bot)
    assert engine.state is BookState.HALTED
    assert bot._last_carry_decision["reason"] == "VENUE_POSITION_DRIFT"
    assert len(venue.created) == before, "an order was sent without a view"


def test_an_unreadable_short_halts_even_without_a_view(tmp_path):
    venue, journal, engine, bot = _hedged(tmp_path)
    real = venue._request

    def unreadable(method, endpoint, **kw):
        if endpoint == "/v5/position/list":
            raise TransientAPIError("timed out")
        return real(method, endpoint, **kw)
    venue._request = unreadable
    before = len(venue.created)
    tick_with_unusable_view(bot)
    assert engine.state is BookState.HALTED
    assert bot._last_carry_decision["reason"] == "VENUE_POSITION_UNREADABLE"
    assert len(venue.created) == before


def test_a_matching_short_stays_hedged_and_untouched(tmp_path):
    venue, journal, engine, bot = _hedged(tmp_path)
    before = len(venue.created)
    tick_with_unusable_view(bot)
    assert engine.state is BookState.HEDGED
    assert bot._last_carry_decision["reason"] == "MARKET_VIEW_UNUSABLE"
    assert len(venue.created) == before


def test_a_flat_book_does_nothing_without_a_view(tmp_path):
    venue, journal, engine, bot = _flat(tmp_path)
    tick_with_unusable_view(bot)
    assert engine.state is BookState.FLAT
    assert bot._last_carry_decision["reason"] == "MARKET_VIEW_UNUSABLE"
    assert venue.created == []
