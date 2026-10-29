"""A liquidation view with no distance and a reason the engine does not know is
unreadable, whatever the margin mode.

The isolated-margin branch already halted on this; the account-level branch fell
through to ``distance is None -> return None`` (safe). Only the venue's own
"unreachable" spelling (``BEYOND_VENUE_PRICE_BOUNDS``) or an explicit
``NO_POSITION`` may skip the distance gate. Offline: a stub broker.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from carry_engine import BookState  # noqa: E402
from test_carry_unstated_numbers import POS  # noqa: E402
from test_carry_venue_direction_integration import (  # noqa: E402
    broker_with_rows, held_engine)


def _engine_with_view(view):
    engine = held_engine(broker_with_rows(dict(POS, liqPrice="")))
    engine.require_liquidation_check = True
    engine.snapshot = None
    engine.broker.get_liquidation_view = lambda symbol: dict(view)
    return engine


ACCOUNT = {"margin_mode": "REGULAR_MARGIN", "account_mm_rate": 0.1,
           "distance_pct": None}


@pytest.mark.parametrize("reason", [None, "", "FOO", "no_position", 0])
def test_account_level_view_with_unknown_reason_and_no_distance_halts(reason):
    view = dict(ACCOUNT) if reason is None else dict(ACCOUNT, reason=reason)
    d = _engine_with_view(view)._check_liquidation_distance()
    assert d is not None and d.state is BookState.HALTED
    assert d.reason == "LIQUIDATION_DISTANCE_UNUSABLE"


@pytest.mark.parametrize("reason", ["BEYOND_VENUE_PRICE_BOUNDS", "NO_POSITION"])
def test_the_two_stated_reasons_still_skip_the_distance_gate(reason):
    assert _engine_with_view(
        dict(ACCOUNT, reason=reason))._check_liquidation_distance() is None
