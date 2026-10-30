"""MarketSnapshot timing defects, fixed at the root.

* an observation stamped in the future read as age 0 (perfectly fresh);
* a NaN / inf observation time read as age 0 as well (max(0, nan) is 0);
* a non-numeric funding rate raised TypeError, not the StaleMarket refusal;
* the legs of one snapshot carried no record of how far apart they were read,
  so a read that stalled between perp and spot still looked like one instant.
"""
from __future__ import annotations

import math
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import market_snapshot as M  # noqa: E402
from market_snapshot import (MAX_CLOCK_SKEW_S, MAX_LEG_SPAN_S,  # noqa: E402
                             MAX_SNAPSHOT_AGE_S, MarketSnapshot, StaleMarket,
                             take_snapshot)


def snap(**kw):
    base = dict(perp_mark=100_000.0, spot_mark=100_000.0, funding_bps=1.0,
                margin_multiple=None, observed_at_s=time.time())
    base.update(kw)
    return MarketSnapshot(**base)


class Broker:
    def __init__(self, stall_s=0.0, clock=None):
        self.stall_s, self.clock = stall_s, clock

    def get_mark(self, s):
        return 100_030.0

    def get_spot_mark(self, s):
        # the stall sits BETWEEN the perp and spot legs
        if self.clock is not None:
            self.clock.advance(self.stall_s)
        return 100_000.0

    def get_funding_bps(self, s):
        return 1.0

    def get_funding_print(self, s):
        h8 = 8 * 3600 * 1000
        return 1.0, int(time.time() * 1000) // h8 * h8

    def get_perp_position(self, s):
        return 0.0


class Mono:
    def __init__(self):
        self.t = 1000.0

    def advance(self, s):
        self.t += s

    def monotonic(self):
        return self.t


def test_the_limits_are_not_loosened():
    assert MAX_SNAPSHOT_AGE_S == 90.0
    assert MAX_CLOCK_SKEW_S == 30.0
    assert MAX_LEG_SPAN_S == 10.0


@pytest.mark.parametrize("ahead", [MAX_CLOCK_SKEW_S + 5, 3600.0, 86400.0 * 365])
def test_a_future_stamped_snapshot_is_refused(ahead):
    s = snap(observed_at_s=time.time() + ahead)
    assert s.age_s == 0.0                      # what used to pass it
    with pytest.raises(StaleMarket, match="in the future"):
        s.assert_fresh()


def test_a_stamp_a_few_seconds_ahead_is_ordinary_clock_jitter():
    snap(observed_at_s=time.time() + 2.0).assert_fresh()


@pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf, -1.0,
                                 None, "now", True])
def test_a_non_time_observation_stamp_is_refused(bad):
    with pytest.raises(StaleMarket, match="observed_at_s"):
        snap(observed_at_s=bad).assert_fresh()


@pytest.mark.parametrize("bad", ["1.0", None, True])
def test_a_non_numeric_funding_rate_is_a_refusal_not_a_type_error(bad):
    with pytest.raises(StaleMarket, match="funding_bps"):
        snap(funding_bps=bad).assert_fresh()


def test_take_snapshot_records_how_far_apart_its_legs_were_read(monkeypatch):
    mono = Mono()
    monkeypatch.setattr(M.time, "monotonic", mono.monotonic)
    view = take_snapshot(Broker(stall_s=3.0, clock=mono),
                         perp_symbol="BTCUSDT", spot_symbol="BTCUSDT")
    assert view.leg_span_s == pytest.approx(3.0)
    view.assert_fresh()
    assert view.as_dict()["leg_span_s"] == pytest.approx(3.0)


def test_legs_read_too_far_apart_are_not_one_instant(monkeypatch):
    mono = Mono()
    monkeypatch.setattr(M.time, "monotonic", mono.monotonic)
    view = take_snapshot(Broker(stall_s=MAX_LEG_SPAN_S + 1, clock=mono),
                         perp_symbol="BTCUSDT", spot_symbol="BTCUSDT")
    with pytest.raises(StaleMarket, match="legs were read"):
        view.assert_legs_coherent()          # the ENTRY question
    view.assert_fresh()                      # management of a held pair is not blocked
    with pytest.raises(StaleMarket, match="legs were read"):
        view.assert_fresh(max_leg_span_s=MAX_LEG_SPAN_S)


@pytest.mark.parametrize("bad", [math.nan, -1.0, math.inf])
def test_a_garbage_leg_span_is_refused(bad):
    with pytest.raises(StaleMarket, match="leg_span_s"):
        snap(leg_span_s=bad).assert_fresh()


def test_a_hand_built_snapshot_without_a_span_is_not_checked_for_one():
    snap().assert_fresh()


# -- the leg-span limit judges ENTRIES, never the management of a held pair ----------

def _snap_with_span(span):
    import time as _t
    import market_snapshot as MS
    return MS.MarketSnapshot(perp_mark=100.0, spot_mark=100.0, funding_bps=1.0,
                             margin_multiple=5.0, observed_at_s=_t.time(),
                             leg_span_s=span)


def test_a_wide_leg_span_does_not_make_a_snapshot_stale_for_management():
    _snap_with_span(60.0).assert_fresh()                  # held-pair path: passes


def test_a_wide_leg_span_is_refused_when_the_entry_asks():
    import market_snapshot as MS
    with pytest.raises(MS.StaleMarket):
        _snap_with_span(60.0).assert_legs_coherent()
    _snap_with_span(2.0).assert_legs_coherent()


def test_the_entry_gate_refuses_a_wide_span_but_a_held_pair_is_unaffected():
    from carry_risk import CarryRisk
    gate = CarryRisk(max_notional_usd=100.0).gate_open(
        notional_usd=100.0, snapshot=_snap_with_span(60.0), has_open_pair=False)
    assert not gate and gate.reason == "MARKET_VIEW_STALE"
