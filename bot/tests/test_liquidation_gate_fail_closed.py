"""The held-position risk gates, on the inputs a venue read can actually get wrong.

Mutation testing (one-line flips and deleted guards in `CarryEngine
._check_margin` and `_check_liquidation_distance`) left a dozen survivors that
`test_liquidation_distance.py` and `test_carry_engine.py` do not kill: the
exact floors, a non-finite distance, a malformed or negative account rate, an
unknown margin mode, a liquidation view that is not a mapping, and the whole
isolated-margin branch.

One of those survivors was a real fail-open, not just a missing test. For
ISOLATED margin the code's own comment says "a measured per-position
liquidation distance is mandatory", but a view that carried NO distance and a
reason other than the two the code recognised fell through to the branch that
reads a missing distance as "unreachable liquidation price, the safest state
there is" and the book held the short. A missing distance is only that
reading when the venue says the position is beyond its price bounds, and for
isolated margin (no account-level cover) even that is halted. Anything else
unreadable is a margin call the book cannot see.

Only refusals are added. Nothing here moves a floor or a constant.
"""
from __future__ import annotations

import math
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from carry_engine import (MAX_ACCOUNT_MM_RATE, MIN_LIQUIDATION_DISTANCE_PCT,  # noqa: E402
                          MIN_MARGIN_MULTIPLE, CarryEngine)
from carry_risk import CarryRisk  # noqa: E402
from market_snapshot import MarketSnapshot  # noqa: E402

MARK = 100_000.0
H8 = 8 * 3600 * 1000
REGULAR = "REGULAR_MARGIN"
ISOLATED = "ISOLATED_MARGIN"


class Venue:
    """A book that can place orders (so an unwind is visible) and whose
    liquidation view and margin multiple the test controls."""

    def __init__(self):
        self.view = None
        self.margin = 5.0
        self.orders = []

    def place_market(self, *, symbol, side, qty, product):
        self.orders.append((side, product, qty))
        return {"filled_qty": qty, "avg_price": MARK, "order_link_id": "x",
                "fee": qty * MARK * 5.5 / 1e4}

    def get_margin_multiple(self, symbol):
        if isinstance(self.margin, Exception):
            raise self.margin
        return self.margin

    def get_mark(self, symbol):
        return MARK

    def get_lot_rules(self, symbol, product):
        return {"qty_step": 1e-6, "min_qty": 1e-6, "min_notional": 0.0}

    def get_fee_rates(self, symbol, product):
        return {"maker_bps": 2.0, "taker_bps": 10.0 if product == "spot" else 5.5}

    def get_spot_inventory(self, symbol):
        return 5.0

    def get_liquidation_view(self, symbol):
        if isinstance(self.view, Exception):
            raise self.view
        return self.view


def healthy_view(**over):
    view = {"size": 0.001, "side": "Sell", "mark": MARK, "liq_price": MARK * 2,
            "distance_pct": 100.0, "account_mm_rate": 0.05,
            "margin_mode": REGULAR, "reason": ""}
    view.update(over)
    return view


def opened():
    venue = Venue()
    venue.view = healthy_view()
    e = CarryEngine(broker=venue, max_notional_usd=100.0, borrow_apr=0.0,
                    execution_mode="overlay", persist=lambda s: None,
                    require_liquidation_check=True)
    e.pair_risk = CarryRisk(max_notional_usd=100.0)
    e.snapshot = MarketSnapshot(perp_mark=MARK, spot_mark=MARK, funding_bps=3.0,
                                margin_multiple=5.0, observed_at_s=time.time())
    for k in (1, 2):
        e.on_candle(mark=MARK, funding_bps=3.0, spot=MARK,
                    funding_print_ms=k * H8, timestamp_ms=k * H8)
    d = e.on_candle(mark=MARK, funding_bps=3.0, spot=MARK,
                    funding_print_ms=3 * H8, timestamp_ms=3 * H8)
    assert d.action == "opened", d.reason
    venue.orders.clear()
    return e, venue


def tick(e):
    return e.on_candle(mark=MARK, funding_bps=3.0, spot=MARK,
                       funding_print_ms=4 * H8, timestamp_ms=4 * H8)


_UNSET = object()


def verdict_for(view=_UNSET, margin=_UNSET):
    e, venue = opened()
    if view is not _UNSET:
        venue.view = view
    if margin is not _UNSET:
        venue.margin = margin
    return tick(e), venue


# -- ISOLATED margin: the distance is mandatory ------------------------------

@pytest.mark.parametrize("view", [
    {"margin_mode": ISOLATED},
    {"margin_mode": ISOLATED, "distance_pct": None, "reason": ""},
    {"margin_mode": ISOLATED, "distance_pct": None, "reason": "SOMETHING_NEW"},
    {"margin_mode": ISOLATED, "distance_pct": None,
     "reason": "BEYOND_VENUE_PRICE_BOUNDS"},
    {"margin_mode": ISOLATED, "distance_pct": None,
     "reason": "LIQ_PRICE_UNUSABLE"},
], ids=["no-keys", "none-no-reason", "none-unknown-reason", "beyond-bounds",
        "price-unusable"])
def test_isolated_margin_without_a_measured_distance_halts(view):
    d, venue = verdict_for(view=view)
    assert d.action == "halted", (d.action, d.reason)
    assert d.reason == "LIQUIDATION_DISTANCE_UNUSABLE" or "LIQUIDATION" in d.reason
    assert not venue.orders, "a halt places nothing"


def test_isolated_margin_with_a_far_distance_is_left_alone():
    d, _ = verdict_for(view=healthy_view(margin_mode=ISOLATED, distance_pct=60.0))
    assert d.action == "hold"


def test_isolated_margin_flat_on_the_venue_is_the_venue_size_checks_business():
    """NO_POSITION is the broker's own statement that nothing is held; the
    liquidation gate has nothing to measure and must not pre-empt the
    venue-vs-book size check that runs next."""
    d, _ = verdict_for(view=healthy_view(
        margin_mode=ISOLATED, size=0.0, liq_price=None, distance_pct=None,
        reason="NO_POSITION"))
    assert d.reason != "LIQUIDATION_DISTANCE_UNUSABLE"


# -- the liquidation view itself --------------------------------------------

def test_a_liquidation_view_that_is_not_a_mapping_halts():
    d, _ = verdict_for(view=["not", "a", "mapping"])
    assert (d.action, d.reason) == ("halted", "LIQUIDATION_PRICE_UNREADABLE")


def test_a_liquidation_view_that_raises_halts():
    d, _ = verdict_for(view=RuntimeError("position list unreadable"))
    assert (d.action, d.reason) == ("halted", "LIQUIDATION_PRICE_UNREADABLE")


@pytest.mark.parametrize("mode", [None, "", "FUTURE_MODE", "regular_margin"])
def test_an_unknown_margin_mode_halts(mode):
    d, _ = verdict_for(view=healthy_view(margin_mode=mode))
    assert (d.action, d.reason) == ("halted", "MARGIN_MODE_UNKNOWN")


# -- account-level modes: the rate is mandatory ------------------------------

@pytest.mark.parametrize("rate", [None, "abc", float("nan"), float("inf"), -0.1,
                                  True])
def test_an_unusable_account_mm_rate_halts(rate):
    d, _ = verdict_for(view=healthy_view(account_mm_rate=rate))
    assert (d.action, d.reason) == ("halted", "ACCOUNT_MM_RATE_UNUSABLE")


def test_a_missing_account_mm_rate_halts():
    view = healthy_view()
    del view["account_mm_rate"]
    d, _ = verdict_for(view=view)
    assert (d.action, d.reason) == ("halted", "ACCOUNT_MM_RATE_UNUSABLE")


def test_the_account_rate_floor_is_inclusive():
    on, venue = verdict_for(view=healthy_view(account_mm_rate=MAX_ACCOUNT_MM_RATE))
    assert on.action == "unwound" and "ACCOUNT_MM_RATE" in on.reason
    assert venue.orders, "the unwind must actually close the leg"
    below, _ = verdict_for(view=healthy_view(
        account_mm_rate=math.nextafter(MAX_ACCOUNT_MM_RATE, 0.0)))
    assert below.action == "hold"


# -- the distance ------------------------------------------------------------

@pytest.mark.parametrize("distance", [float("nan"), float("inf"),
                                      float("-inf"), "abc"])
def test_a_non_finite_or_malformed_distance_halts(distance):
    d, _ = verdict_for(view=healthy_view(distance_pct=distance))
    assert (d.action, d.reason) == ("halted", "LIQUIDATION_DISTANCE_UNUSABLE")


def test_a_named_unusable_price_is_not_an_unreachable_one():
    d, _ = verdict_for(view=healthy_view(distance_pct=None,
                                         reason="LIQ_PRICE_UNUSABLE"))
    assert (d.action, d.reason) == ("halted", "LIQUIDATION_DISTANCE_UNUSABLE")


def test_the_distance_floor_is_exclusive():
    """The gate unwinds BELOW the floor; exactly on it the book holds."""
    on, _ = verdict_for(view=healthy_view(
        distance_pct=MIN_LIQUIDATION_DISTANCE_PCT))
    assert on.action == "hold"
    below, venue = verdict_for(view=healthy_view(
        distance_pct=math.nextafter(MIN_LIQUIDATION_DISTANCE_PCT, 0.0)))
    assert below.action == "unwound" and venue.orders


# -- the IM/MM headroom floor -------------------------------------------------

def test_the_margin_floor_is_exclusive():
    on, _ = verdict_for(margin=MIN_MARGIN_MULTIPLE)
    assert on.action == "hold"
    below, venue = verdict_for(margin=math.nextafter(MIN_MARGIN_MULTIPLE, 0.0))
    assert below.action == "unwound" and venue.orders


@pytest.mark.parametrize("margin", [float("nan"), float("inf"), float("-inf"),
                                    "abc", None, RuntimeError("unreadable")])
def test_an_unreadable_margin_halts(margin):
    d, venue = verdict_for(margin=margin)
    assert (d.action, d.reason) == ("halted", "MARGIN_UNREADABLE")
    assert not venue.orders
