"""A non-finite position size or venue clock is a refusal, never "flat" or "unchecked".

`take_snapshot` decided "flat, nothing to measure" with `held > 0`; a NaN size
compares False, so a broker that returned NaN skipped the margin read on what
may be a live short, and `assert_fresh` never looked at `perp_held`. Likewise a
NaN venue clock made `skew > limit` False and the clock gate silently passed.
"""
from __future__ import annotations

import math
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from market_snapshot import MarketSnapshot, StaleMarket, take_snapshot  # noqa: E402



class Broker:
    def __init__(self, held=0.0, venue_time=None, margin=3.0):
        self.held, self.margin = held, margin
        self.venue_time = time.time() if venue_time is None else venue_time
        self.margin_reads = 0

    def get_mark(self, s): return 100_000.0
    def get_spot_mark(self, s): return 100_000.0
    def get_funding_bps(self, s): return 1.0
    def get_funding_print(self, s): return 1.0, int(time.time() * 1000) - 1000
    def get_perp_position(self, s): return self.held
    def get_venue_time_s(self): return self.venue_time

    def get_margin_multiple(self, s):
        self.margin_reads += 1
        return self.margin


def snap(broker):
    return take_snapshot(broker, perp_symbol="BTCUSDT", spot_symbol="BTCUSDT")


@pytest.mark.parametrize("held", [float("nan"), float("inf"), float("-inf")])
def test_a_non_finite_position_size_is_refused_not_read_as_flat(held):
    with pytest.raises(StaleMarket):
        snap(Broker(held=held)).assert_fresh()


@pytest.mark.parametrize("held", [float("nan"), float("inf")])
def test_a_non_finite_size_never_skips_the_margin_read_silently(held):
    b = Broker(held=held)
    try:
        s = snap(b)
    except Exception:
        return                      # refused at the read: fine
    with pytest.raises(StaleMarket):
        s.assert_fresh()


@pytest.mark.parametrize("venue", [float("nan"), float("inf"), -1.0])
def test_a_non_finite_venue_clock_is_not_an_unchecked_clock(venue):
    s = MarketSnapshot(perp_mark=1.0, spot_mark=1.0, funding_bps=0.0,
                       margin_multiple=None, observed_at_s=time.time(),
                       venue_time_s=venue)
    with pytest.raises(StaleMarket):
        s.assert_fresh()


def test_a_venue_that_will_not_say_the_time_is_still_recorded_unchecked():
    class NoClock(Broker):
        def get_venue_time_s(self): raise RuntimeError("down")
    s = snap(NoClock())
    assert s.venue_time_s is None and s.detail["clock_checked"] is False


def test_a_flat_book_and_a_held_book_still_read():
    now = time.time()
    for held, margin in ((0.0, None), (0.01, 3.0)):
        s = take_snapshot(Broker(held=held, venue_time=now),
                          perp_symbol="BTCUSDT", spot_symbol="BTCUSDT",
                          now_s=now)
        assert s.margin_multiple == margin and s.perp_held == held
        s.assert_fresh()
