"""One consistent view of the venue, or a refusal.

main.tick made three independent venue calls and handed the results to the
engine as if they described one instant. The basis is a DIFFERENCE between two
of them, so a forty-second gap does not measure the basis — it measures the
basis plus forty seconds of drift, on a term worth -$4,772 against $42,843 of
gross in 0018.

And none of the three carried a timestamp: a frozen feed looked exactly like a
quiet market.
"""
from __future__ import annotations

import math
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from market_snapshot import (MAX_CLOCK_SKEW_S, MAX_SNAPSHOT_AGE_S,  # noqa: E402
                             MarketSnapshot, StaleMarket, take_snapshot)


def snap(**kw):
    base = dict(perp_mark=100_000.0, spot_mark=100_000.0, funding_bps=1.0,
                margin_multiple=5.0, observed_at_s=time.time())
    base.update(kw)
    return MarketSnapshot(**base)


class FakeBroker:
    def __init__(self, *, perp=100_030.0, spot=100_000.0, funding=1.0,
                 margin=5.0, venue_time=None, boom=None):
        self.perp, self.spot, self.funding = perp, spot, funding
        self.margin, self.venue_time, self.boom = margin, venue_time, boom

    def get_mark(self, s):
        if self.boom == "perp":
            raise RuntimeError("no ticker")
        return self.perp

    def get_spot_mark(self, s):
        if self.boom == "spot":
            raise RuntimeError("no spot ticker")
        return self.spot

    def get_funding_bps(self, s):
        if self.boom == "funding":
            raise RuntimeError("no fundingRate")
        return self.funding

    def get_margin_multiple(self, s):
        if self.boom == "margin":
            raise RuntimeError("no position row")
        return self.margin

    def get_funding_print(self, s):
        # 0034: the settled print and its stamp; the latest settlement.
        if self.boom == "print":
            raise RuntimeError("no funding history")
        h8 = 8 * 3600 * 1000
        return (self.funding, int(time.time() * 1000) // h8 * h8)

    def get_venue_time_s(self):
        if self.venue_time is None:
            raise RuntimeError("no server time")
        return self.venue_time


class TestBasisNeedsOneInstant:
    def test_basis_is_perp_over_spot(self):
        assert snap(perp_mark=100_100.0).basis_bps == pytest.approx(10.0)

    def test_both_legs_come_from_one_read(self):
        s = take_snapshot(FakeBroker(), perp_symbol="BTCUSDT",
                          spot_symbol="BTCUSDT")
        assert s.basis_bps == pytest.approx(3.0)
        assert s.observed_at_s > 0


class TestAFrozenFeedIsCaught:
    def test_a_stale_snapshot_raises(self):
        with pytest.raises(StaleMarket):
            snap(observed_at_s=time.time() - MAX_SNAPSHOT_AGE_S - 10).assert_fresh()

    def test_a_fresh_snapshot_passes(self):
        snap().assert_fresh()

    def test_the_message_explains_why_it_matters(self):
        with pytest.raises(StaleMarket, match="frozen feed"):
            snap(observed_at_s=time.time() - 600).assert_fresh()

    def test_freshness_raises_rather_than_returning_a_flag(self):
        """A flag can be forgotten. This check must not be passable."""
        assert snap().assert_fresh() is None


class TestClockSkew:
    def test_a_skewed_venue_clock_raises(self):
        with pytest.raises(StaleMarket, match="venue clock"):
            snap(venue_time_s=time.time() + MAX_CLOCK_SKEW_S + 60).assert_fresh()

    def test_a_venue_that_will_not_give_the_time_still_trades(self):
        """Not knowing the venue's clock disables the SKEW check. It is not a
        reason to refuse, and the snapshot records that it was not checked."""
        s = take_snapshot(FakeBroker(venue_time=None), perp_symbol="BTCUSDT",
                          spot_symbol="BTCUSDT")
        assert s.venue_time_s is None
        assert s.clock_skew_s is None
        assert s.detail["clock_checked"] is False
        s.assert_fresh()

    def test_an_agreeing_clock_is_recorded(self):
        s = take_snapshot(FakeBroker(venue_time=time.time()),
                          perp_symbol="BTCUSDT", spot_symbol="BTCUSDT")
        assert s.detail["clock_checked"] is True
        assert s.clock_skew_s < MAX_CLOCK_SKEW_S


class TestUnusableNumbersRaise:
    @pytest.mark.parametrize("field", ["perp_mark", "spot_mark",
                                       "margin_multiple"])
    @pytest.mark.parametrize("bad", [0.0, -1.0, float("nan"), float("inf")])
    def test_a_bad_price_or_margin_raises(self, field, bad):
        with pytest.raises(StaleMarket):
            snap(**{field: bad}).assert_fresh()

    def test_nan_funding_raises(self):
        with pytest.raises(StaleMarket):
            snap(funding_bps=float("nan")).assert_fresh()

    def test_negative_funding_is_fine(self):
        """Negative funding is a market state, not a data error."""
        snap(funding_bps=-2.0).assert_fresh()


class TestAPartialReadIsNotASnapshot:
    @pytest.mark.parametrize("leg", ["perp", "spot", "funding", "margin"])
    def test_any_failed_leg_propagates(self, leg):
        """A snapshot missing a field it could not read would be a partial view
        wearing a complete one's clothes. The exception is the information."""
        with pytest.raises(RuntimeError):
            take_snapshot(FakeBroker(boom=leg), perp_symbol="BTCUSDT",
                          spot_symbol="BTCUSDT")


class TestItDecidesNothing:
    def test_the_module_places_no_orders(self):
        with open(os.path.join(os.path.dirname(__file__), "..",
                               "market_snapshot.py"), encoding="utf-8") as fh:
            body = fh.read()
        for banned in ("place_market", "live_authorized", "FUND_ABS"):
            assert banned not in body
