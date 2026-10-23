"""The entry gate prices only costs it can read.

Before this, a NaN round trip made every comparison false and the entry was
ALLOWED; a zero or negative one read as free execution, a negative borrow as
income. The Python gate and the C++ core refuse the same inputs."""
from __future__ import annotations

import math
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "tools"))

import carry_costs as cc  # noqa: E402
import carry_core as core  # noqa: E402

RICH = [3.0, 3.2, 3.1, 3.3, 3.0]         # bps per print, comfortably positive
GOOD = dict(funding_prints_bps=RICH, perp=60_000.0, spot=60_000.0,
            borrow_apr=0.0, round_trip_bps=11.0)
NAN, INF = float("nan"), float("inf")


def gate(**kw):
    return cc.evaluate_entry(**{**GOOD, **kw})


def test_the_baseline_inputs_do_open():
    v = gate()
    assert v.allowed and v.reason == "CARRY_CLEARS_COSTS_AND_BASIS"


@pytest.mark.parametrize("field,value", [
    ("round_trip_bps", NAN), ("round_trip_bps", 0.0), ("round_trip_bps", -5.0),
    ("round_trip_bps", None), ("round_trip_bps", INF), ("round_trip_bps", "n/a"),
    ("borrow_apr", NAN), ("borrow_apr", -0.05), ("borrow_apr", None),
    ("hold_days", NAN), ("hold_days", 0.0), ("hold_days", -3.0)])
def test_an_unusable_cost_input_is_refused_and_named(field, value):
    v = gate(**{field: value})
    assert v.allowed is False and v.reason == "COST_INPUT_INVALID"
    assert field in v.detail["fields"]


@pytest.mark.parametrize("field,value", [
    ("perp", NAN), ("perp", INF), ("perp", None), ("spot", NAN), ("spot", 0.0),
    ("spot", -1.0), ("spot", None)])
def test_an_unreadable_price_is_refused_not_crashed(field, value):
    v = gate(**{field: value})
    assert v.allowed is False and v.reason == "BASIS_UNREADABLE"


@pytest.mark.parametrize("perp", [0.0, -1.0])
def test_a_non_positive_perp_never_opens(perp):
    assert gate(perp=perp).allowed is False


def test_a_nan_round_trip_used_to_open_and_no_longer_does():
    """The failure being closed: with the check removed, every comparison on
    a NaN net edge is False and nothing refuses."""
    net = 9.0 - 0.0 - float("nan")
    assert not (net <= 0) and not (0.0 > net * 30.0)      # why it fell open
    assert gate(round_trip_bps=NAN).allowed is False


def test_zero_borrow_is_a_real_input_not_a_missing_one():
    assert gate(borrow_apr=0.0).allowed is True


def test_the_invalid_input_is_reported_before_the_history_is_judged():
    v = gate(funding_prints_bps=[3.0], round_trip_bps=NAN)
    assert v.reason == "COST_INPUT_INVALID"


class _Row:
    def __init__(self, i, rate):
        self.ms = 1_700_006_400_000 + i * 28_800_000
        self.perp = self.spot = self.perp_high = self.perp_low = 60_000.0
        self.rate = rate


@pytest.mark.parametrize("kw", [
    dict(round_trip_bps=NAN), dict(round_trip_bps=0.0),
    dict(round_trip_bps=-1.0), dict(hold_days=0.0), dict(hold_days=NAN),
    dict(borrow_apr=-0.01), dict(borrow_apr=NAN)])
def test_the_native_core_refuses_the_same_inputs(kw):
    if not core.build() and not core.available():
        pytest.skip("no compiler and no libcarrycore.so")
    rows = [_Row(i, 0.0003) for i in range(40)]
    base = dict(notional=60_000.0, borrow_apr=0.0, round_trip_bps=11.0,
                gated=True, overlay=True, latency_rows=0)
    good = core.simulate(rows, core.params(**base))
    assert good["trades"] >= 1
    # core.params validates only what it always did; NaN/zero ride through
    bad = core.simulate(rows, core.params(**{**base, **kw}))
    assert bad["trades"] == 0 and bad["refusals"] > 0
