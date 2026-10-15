"""A funding history with a hole in it is not a history the book may enter on.

The EWMA behind the entry gate is a list of RATES with no stamps. If the process
was down (or a poll was skipped) for several settlements, the next print was
appended next to the last one it had seen: days-old rich prints kept 50% or more
of the weight of a smoothed rate that was then read as "the current regime". The
freshness check in `MarketSnapshot.assert_fresh` only vets the NEWEST print.
Offline and synthetic: nothing here is a venue observation.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from carry_engine import CarryEngine  # noqa: E402

MARK = 100_000.0
H8 = 8 * 3600 * 1000


class Venue:
    def get_lot_rules(self, symbol, product):
        return {"qty_step": 1e-6, "min_qty": 1e-6, "min_notional": 0.0}

    def get_fee_rates(self, symbol, product):
        return {"maker_bps": 2.0,
                "taker_bps": 10.0 if product == "spot" else 5.5}


def engine(history, last_print_ms):
    e = CarryEngine(broker=Venue(), max_notional_usd=100.0, borrow_apr=0.05,
                    execution_mode="acquire", persist=lambda s: None)
    e.restore({"position": None, "funding_history": history,
               "last_print_ms": last_print_ms})
    return e


def test_missed_settlements_do_not_leave_stale_rich_prints_in_the_ewma():
    # Four rich prints, then the process is down for six settlements. The one
    # fresh print (0.3 bps) cannot pay for the entry on its own.
    e = engine([10.0] * 4, 4 * H8)
    d = e.on_candle(mark=MARK, funding_bps=0.3, spot=MARK,
                    funding_print_ms=10 * H8)
    assert d.action == "stand_aside", d.reason
    assert d.reason == "INSUFFICIENT_FUNDING_HISTORY"


def test_contiguous_prints_still_smooth_normally():
    # No gap: the next settlement is one interval later. Behaviour unchanged.
    e = engine([10.0] * 4, 4 * H8)
    d = e.on_candle(mark=MARK, funding_bps=0.3, spot=MARK,
                    funding_print_ms=5 * H8)
    assert d.reason != "INSUFFICIENT_FUNDING_HISTORY"
    assert e._funding_history == [10.0] * 4 + [0.3]


def test_a_gap_is_not_papered_over_by_later_contiguous_prints():
    e = engine([10.0] * 4, 4 * H8)
    e.on_candle(mark=MARK, funding_bps=0.3, spot=MARK, funding_print_ms=10 * H8)
    e.on_candle(mark=MARK, funding_bps=0.3, spot=MARK, funding_print_ms=11 * H8)
    d = e.on_candle(mark=MARK, funding_bps=0.3, spot=MARK,
                    funding_print_ms=12 * H8)
    assert 10.0 not in e._funding_history
    assert d.reason != "INSUFFICIENT_FUNDING_HISTORY"   # 3 fresh prints now
