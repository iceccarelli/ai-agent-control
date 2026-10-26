"""An entry whose own arithmetic overflows is REFUSED, in Python and in C++.

Found by differential fuzzing (Python `entry_gate` against the C++ core, 360,000
boundary-weighted cases, all four seeds hit the same class). With a borrow rate
and a funding print both near the largest double:

    carry_per_day  = smoothed * 3            -> inf
    borrow_per_day = borrow / 365 * 1e4      -> inf
    net_edge       = inf - inf               -> NaN

and every comparison against NaN is False, so `evaluate_entry` fell through its
refusals and returned ALLOWED (the failure its own comment says it exists to
prevent: "a NaN anywhere below turns every comparison false"). `entry_gate` then
raised ValueError out of the hold check instead of answering, while the C++ core
refused only by accident (the NaN also failed its break-even comparison).
`hold_clears_round_trip` raised nothing here but mislabelled the cause.

The inputs are not reachable from today's configuration (the borrow rate is a
fraction and a funding print is bounded by the venue). The point is the
contract: a gate that says it fails closed on every unusable input must answer
REFUSED with a reason on these too, and the two implementations must agree on
which reason. The fix only ever refuses more.
"""
from __future__ import annotations

import math
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
BOT = os.path.dirname(HERE)
sys.path.insert(0, BOT)
sys.path.insert(0, os.path.join(BOT, "tools"))

import carry_core as core  # noqa: E402
import carry_costs as cc  # noqa: E402

BIG = 1.7976931348623157e308


@pytest.fixture(scope="module")
def native():
    built = core.build()
    if not built and not core.available():
        if os.environ.get("REQUIRE_NATIVE_CORE") == "1":
            pytest.fail("REQUIRE_NATIVE_CORE=1 and libcarrycore.so is not built")
        pytest.skip("no compiler and no libcarrycore.so")
    return core


CASES = {
    # inf - inf: carry and borrow both overflow
    "nan-net-edge": dict(prints=[BIG] * 3, borrow=BIG, rt=11.0),
    # carry overflows alone: net edge +inf, budget +inf, ANY basis would pass
    "infinite-carry": dict(prints=[BIG] * 3, borrow=0.0, rt=11.0),
    "infinite-carry-with-borrow": dict(prints=[BIG] * 3, borrow=0.05, rt=11.0),
    # one huge print among ordinary ones still drags the smoothed rate to inf
    "one-huge-print": dict(prints=[1.0, 2.0, BIG, BIG], borrow=0.0, rt=11.0),
}


def py_entry(case, perp=150.0, spot=100.0):
    return cc.entry_gate(funding_prints_bps=case["prints"], perp=perp, spot=spot,
                         borrow_apr=case["borrow"], round_trip_bps=case["rt"])


@pytest.mark.parametrize("name", list(CASES))
def test_entry_gate_refuses_and_names_the_cause_instead_of_raising(name):
    verdict = py_entry(CASES[name])
    assert verdict.allowed is False
    assert verdict.reason == "COST_INPUT_INVALID"


@pytest.mark.parametrize("name", list(CASES))
def test_evaluate_entry_alone_does_not_allow_a_non_finite_edge(name):
    c = CASES[name]
    verdict = cc.evaluate_entry(
        funding_prints_bps=c["prints"], perp=150.0, spot=100.0,
        borrow_apr=c["borrow"], round_trip_bps=c["rt"])
    assert verdict.allowed is False
    assert verdict.reason == "COST_INPUT_INVALID"


@pytest.mark.parametrize("name", list(CASES))
def test_cpp_core_gives_the_same_answer(native, name):
    c = CASES[name]
    p = core.params(borrow_apr=c["borrow"], round_trip_bps=c["rt"], gated=True,
                    overlay=True)
    out = core.entry_gate(c["prints"], 150.0, 100.0, p)
    assert (out["allowed"], out["reason"]) == (False, "COST_INPUT_INVALID")


def test_the_hold_check_alone_names_an_overflowing_borrow():
    verdict = cc.hold_clears_round_trip(
        funding_prints_bps=[5.0, 5.0, 5.0], round_trip_bps=11.0, borrow_apr=1e307)
    assert verdict.allowed is False
    assert verdict.reason == "COST_INPUT_INVALID"
    assert verdict.detail["fields"] == ["borrow_apr"]


def test_the_engine_stands_aside_on_such_a_history_without_raising():
    from carry_engine import OVERLAY, CarryEngine

    class Venue:
        def get_fee_rates(self, symbol, product):
            return {"maker_bps": 2.0, "taker_bps": 5.5}

    e = CarryEngine(broker=Venue(), max_notional_usd=100.0, borrow_apr=0.0,
                    execution_mode=OVERLAY, persist=lambda s: None)
    e._funding_history = [BIG, BIG, BIG]
    econ = e.entry_economics(mark=100.0, spot=100.0)
    assert econ.refusal is not None
    assert econ.refusal.action == "stand_aside"
    assert econ.refusal.reason == "COST_INPUT_INVALID"


def test_a_basis_budget_that_overflows_is_refused_not_unbounded(native):
    """net edge 30 bps/day is finite; over a hold of 1e308 days the budget is
    inf, and `basis > inf` is never true: a 50% premium would be admitted."""
    kw = dict(funding_prints_bps=[10.0] * 3, perp=150.0, spot=100.0,
              borrow_apr=0.0, round_trip_bps=11.0, hold_days=1e308)
    for gate in (cc.evaluate_entry, cc.entry_gate):
        verdict = gate(**kw)
        assert (verdict.allowed, verdict.reason) == (False, "COST_INPUT_INVALID")
    p = core.params(borrow_apr=0.0, round_trip_bps=11.0, hold_days=1e308,
                    gated=True, overlay=True)
    out = core.entry_gate([10.0] * 3, 150.0, 100.0, p)
    assert (out["allowed"], out["reason"]) == (False, "COST_INPUT_INVALID")


def test_ordinary_inputs_are_untouched():
    """Nothing finite and ordinary changes: the same grid point as the parity
    suite's known-good entry still opens."""
    verdict = cc.entry_gate(funding_prints_bps=[3.0, 3.0, 3.0], perp=100.0,
                            spot=100.0, borrow_apr=0.0, round_trip_bps=11.0)
    assert verdict.allowed and math.isfinite(verdict.net_edge_bps_per_day)
