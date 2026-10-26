"""CarryEngine: two boundaries mutation testing showed nothing pinned.

* A ZERO funding print is not an inverted one. `funding_bps < 0` counts toward
  the three-print inversion exit; `<= 0` would close a healthy hedge after a
  day of funding that merely paid nothing, at two round trips of fees. The
  simulators already pin this (`r_bps < 0`, in Python and C++); the engine did
  not, and a one-character slip there is invisible to every other test.
* The pair gate must judge the SAME observation the entry was decided on:
  the snapshot's SPOT mark as well as its perp mark. Dropping the spot half of
  the comparison (the mutant) left every test green.

No threshold moves; nothing here is a claim about profitability.
"""
from __future__ import annotations

import os
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from carry_engine import NEGATIVE_FUNDING_EXIT_PRINTS, CarryEngine  # noqa: E402
from carry_risk import CarryRisk  # noqa: E402
from market_snapshot import MarketSnapshot  # noqa: E402

MARK = 100_000.0
H8 = 8 * 3600 * 1000


class Venue:
    def __init__(self):
        self.orders = []

    def place_market(self, *, symbol, side, qty, product):
        self.orders.append((side, product, qty))
        return {"filled_qty": qty, "avg_price": MARK, "order_link_id": "x",
                "fee": qty * MARK * 5.5 / 1e4}

    def get_margin_multiple(self, symbol):
        return 5.0

    def get_mark(self, symbol):
        return MARK

    def get_lot_rules(self, symbol, product):
        return {"qty_step": 1e-6, "min_qty": 1e-6, "min_notional": 0.0}

    def get_fee_rates(self, symbol, product):
        return {"maker_bps": 2.0, "taker_bps": 10.0 if product == "spot" else 5.5}

    def get_spot_inventory(self, symbol):
        return 5.0


def engine(venue, *, snapshot_perp=MARK, snapshot_spot=MARK):
    e = CarryEngine(broker=venue, max_notional_usd=100.0, borrow_apr=0.0,
                    execution_mode="overlay", persist=lambda s: None)
    e.pair_risk = CarryRisk(max_notional_usd=100.0)
    e.snapshot = MarketSnapshot(perp_mark=snapshot_perp, spot_mark=snapshot_spot,
                                funding_bps=3.0, margin_multiple=5.0,
                                observed_at_s=time.time())
    return e


def tick(e, k, funding):
    return e.on_candle(mark=MARK, funding_bps=funding, spot=MARK,
                       funding_print_ms=k * H8, timestamp_ms=k * H8)


def opened():
    venue = Venue()
    e = engine(venue)
    for k in (1, 2):
        tick(e, k, 3.0)
    assert tick(e, 3, 3.0).action == "opened"
    venue.orders.clear()
    return e, venue


# -- a zero print is not an inverted one --------------------------------------

@pytest.mark.parametrize("zero", [0.0, -0.0])
def test_zero_funding_never_counts_toward_the_inversion_exit(zero):
    e, venue = opened()
    for k in range(4, 4 + 2 * NEGATIVE_FUNDING_EXIT_PRINTS):
        d = tick(e, k, zero)
        assert d.action == "hold", (k, d.action, d.reason)
        assert e.position.negative_funding_streak == 0
    assert not venue.orders


def test_the_inversion_exit_needs_exactly_n_consecutive_negative_prints():
    e, venue = opened()
    k = 4
    for _ in range(NEGATIVE_FUNDING_EXIT_PRINTS - 1):
        assert tick(e, k, -0.5).action == "hold"
        k += 1
    assert e.position.negative_funding_streak == NEGATIVE_FUNDING_EXIT_PRINTS - 1
    d = tick(e, k, -0.5)
    assert d.action == "unwound" and d.reason == "FUNDING_INVERTED"
    assert venue.orders


def test_a_zero_print_between_negatives_resets_the_streak():
    e, _ = opened()
    k = 4
    for funding in (-0.5, -0.5, 0.0, -0.5, -0.5):
        assert tick(e, k, funding).action == "hold"
        k += 1
    assert e.position.negative_funding_streak == 2


# -- the pair gate judges the observation the entry was decided on ------------

@pytest.mark.parametrize("which", ["perp", "spot"])
def test_a_snapshot_that_differs_from_the_decision_in_either_mark_refuses(which):
    venue = Venue()
    off = MARK * (1.0 + 1e-9)
    e = engine(venue, snapshot_perp=off if which == "perp" else MARK,
               snapshot_spot=off if which == "spot" else MARK)
    for k in (1, 2):
        tick(e, k, 3.0)
    d = tick(e, 3, 3.0)
    assert (d.action, d.reason) == ("stand_aside", "SNAPSHOT_MISMATCH"), d
    assert not venue.orders and e.position is None


def test_the_same_snapshot_opens():
    venue = Venue()
    e = engine(venue)
    for k in (1, 2):
        tick(e, k, 3.0)
    assert tick(e, 3, 3.0).action == "opened"
