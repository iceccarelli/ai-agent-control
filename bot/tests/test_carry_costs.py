"""The three gates the engine currently does not have.

CarryEngine opens on one condition: the last funding print cleared a threshold.
That is a reflex, not a decision. It ignores what the money costs, what the
basis will take back, and whether one print represents the next eight hours.

0018 showed what that costs: borrow was 57% of gross income, mean entry basis
was +6 bps against a +29 bps exit, and 15 of 20 trades lost money.
"""
from __future__ import annotations

import math
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import carry_costs as cc  # noqa: E402


def flat(perp: float = 100_000.0):
    return {"perp": perp, "spot": 100_000.0}


def at_basis(bps: float):
    return {"perp": 100_000.0 * (1 + bps / 1e4), "spot": 100_000.0}


class TestTheCaseThatMustRefuse:
    def test_thin_funding_into_a_wide_basis_is_refused(self):
        """+0.6 bps funding, +30 bps basis. Named explicitly in the spec."""
        v = cc.evaluate_entry(round_trip_bps=cc.ROUND_TRIP_BPS, borrow_apr=0.05, funding_prints_bps=[0.6, 0.6, 0.6],
                              **at_basis(30.0))
        assert v.allowed is False
        assert bool(v) is False

    def test_the_refusal_names_the_binding_constraint(self):
        v = cc.evaluate_entry(round_trip_bps=cc.ROUND_TRIP_BPS, borrow_apr=0.05, funding_prints_bps=[0.6, 0.6, 0.6],
                              **at_basis(30.0))
        assert v.reason == "CARRY_BELOW_COST_OF_CAPITAL"
        assert v.net_edge_bps_per_day < 0


class TestGateOneBasis:
    def test_a_basis_beyond_the_budget_is_refused(self):
        v = cc.evaluate_entry(round_trip_bps=cc.ROUND_TRIP_BPS, borrow_apr=0.05, funding_prints_bps=[2.0] * 3, **at_basis(150.0))
        assert v.allowed is False
        assert v.reason == "ENTRY_BASIS_EXCEEDS_CARRY_BUDGET"

    def test_a_basis_inside_the_budget_is_allowed(self):
        v = cc.evaluate_entry(round_trip_bps=cc.ROUND_TRIP_BPS, borrow_apr=0.05, funding_prints_bps=[2.0] * 3, **at_basis(20.0))
        assert v.allowed is True

    def test_the_budget_is_net_edge_not_gross_carry(self):
        """Paying basis out of money already spoken for by borrow and fees is
        how a positive-carry trade closes negative."""
        v = cc.evaluate_entry(round_trip_bps=cc.ROUND_TRIP_BPS, borrow_apr=0.05, funding_prints_bps=[2.0] * 3, **flat())
        gross = v.expected_carry_bps_per_day * cc.ASSUMED_HOLD_DAYS
        assert v.basis_budget_bps < gross

    def test_basis_is_perp_over_spot(self):
        assert cc.basis_bps(101_000.0, 100_000.0) == pytest.approx(100.0)
        assert cc.basis_bps(99_000.0, 100_000.0) == pytest.approx(-100.0)

    @pytest.mark.parametrize("spot", [0.0, -1.0, float("nan")])
    def test_an_unusable_spot_raises(self, spot):
        with pytest.raises(ValueError):
            cc.basis_bps(100_000.0, spot)

    def test_an_unreadable_basis_refuses_rather_than_assuming(self):
        v = cc.evaluate_entry(round_trip_bps=cc.ROUND_TRIP_BPS, borrow_apr=0.05, funding_prints_bps=[5.0] * 3, perp=100_000.0,
                              spot=float("nan"))
        assert v.allowed is False
        assert v.reason == "BASIS_UNREADABLE"


class TestGateTwoCostOfCapital:
    def test_carry_below_borrow_plus_fees_is_refused(self):
        v = cc.evaluate_entry(round_trip_bps=cc.ROUND_TRIP_BPS, borrow_apr=0.05, funding_prints_bps=[0.3] * 3, **flat())
        assert v.allowed is False
        assert v.reason == "CARRY_BELOW_COST_OF_CAPITAL"

    def test_a_higher_borrow_rate_shrinks_the_edge(self):
        cheap = cc.evaluate_entry(round_trip_bps=cc.ROUND_TRIP_BPS, funding_prints_bps=[1.2] * 3, **flat(),
                                  borrow_apr=0.0)
        dear = cc.evaluate_entry(round_trip_bps=cc.ROUND_TRIP_BPS, funding_prints_bps=[1.2] * 3, **flat(),
                                 borrow_apr=0.08)
        assert cheap.net_edge_bps_per_day > dear.net_edge_bps_per_day

    def test_an_absurd_borrow_rate_kills_any_entry(self):
        v = cc.evaluate_entry(round_trip_bps=cc.ROUND_TRIP_BPS, funding_prints_bps=[2.0] * 3, **flat(),
                              borrow_apr=1.0)
        assert v.allowed is False

    def test_the_round_trip_is_amortised_over_the_hold(self):
        short = cc.evaluate_entry(round_trip_bps=cc.ROUND_TRIP_BPS, borrow_apr=0.05, funding_prints_bps=[2.0] * 3, **flat(),
                                  hold_days=7.0)
        long = cc.evaluate_entry(round_trip_bps=cc.ROUND_TRIP_BPS, borrow_apr=0.05, funding_prints_bps=[2.0] * 3, **flat(),
                                 hold_days=90.0)
        assert (short.amortised_round_trip_bps_per_day
                > long.amortised_round_trip_bps_per_day)

    def test_a_seven_day_hold_cannot_pay_for_its_own_entry(self):
        """At threshold carry the round trip alone exceeds the income. This is
        why ASSUMED_HOLD_DAYS is load-bearing rather than cosmetic."""
        v = cc.evaluate_entry(round_trip_bps=cc.ROUND_TRIP_BPS, borrow_apr=0.05, funding_prints_bps=[0.2] * 3, **flat(),
                              hold_days=7.0)
        assert v.amortised_round_trip_bps_per_day > \
            v.expected_carry_bps_per_day
        assert v.allowed is False


class TestGateThreeEwma:
    def test_too_few_prints_refuses(self):
        v = cc.evaluate_entry(round_trip_bps=cc.ROUND_TRIP_BPS, borrow_apr=0.05, funding_prints_bps=[5.0], **flat())
        assert v.allowed is False
        assert v.reason == "INSUFFICIENT_FUNDING_HISTORY"

    def test_too_few_prints_does_not_fall_back_to_the_last_one(self):
        """A book with too little history should not open on it."""
        assert cc.ewma_funding_bps([9.9]) is None
        assert cc.ewma_funding_bps([9.9, 9.9]) is None

    def test_the_newest_print_carries_the_most_weight(self):
        rising = cc.ewma_funding_bps([1.0, 1.0, 5.0])
        falling = cc.ewma_funding_bps([5.0, 1.0, 1.0])
        assert rising > falling

    def test_a_rate_that_has_already_turned_is_damped(self):
        """Three rich prints then a collapse must not read as rich."""
        assert cc.ewma_funding_bps([5.0, 5.0, 5.0, 0.0]) < 5.0

    def test_negative_smoothed_funding_refuses(self):
        v = cc.evaluate_entry(round_trip_bps=cc.ROUND_TRIP_BPS, borrow_apr=0.05, funding_prints_bps=[-1.0] * 3, **flat())
        assert v.allowed is False
        assert v.reason == "FUNDING_NOT_POSITIVE"

    def test_non_finite_prints_are_dropped_not_propagated(self):
        assert cc.ewma_funding_bps([1.0, float("nan"), 1.0, 1.0]) == \
            pytest.approx(1.0)


class TestTheGatesAreOrderedByBindingConstraint:
    def test_cost_of_capital_is_reported_before_basis(self):
        """A refusal must name the binding constraint, not whichever check
        happened to run first."""
        v = cc.evaluate_entry(round_trip_bps=cc.ROUND_TRIP_BPS, borrow_apr=0.05, funding_prints_bps=[0.3] * 3, **at_basis(500.0))
        assert v.reason == "CARRY_BELOW_COST_OF_CAPITAL"

    def test_basis_is_reported_when_the_economics_are_otherwise_fine(self):
        v = cc.evaluate_entry(round_trip_bps=cc.ROUND_TRIP_BPS, borrow_apr=0.05, funding_prints_bps=[2.0] * 3, **at_basis(500.0))
        assert v.reason == "ENTRY_BASIS_EXCEEDS_CARRY_BUDGET"


class TestItDecidesNothingElse:
    def test_the_module_places_no_orders(self):
        with open(os.path.join(os.path.dirname(__file__), "..",
                               "carry_costs.py"), encoding="utf-8") as fh:
            body = fh.read()
        for banned in ("place_market", "place_order", "live_authorized",
                       "openai", "anthropic"):
            assert banned not in body

    def test_the_verdict_is_pure(self):
        """Same inputs, same answer. A risk rule with state cannot be trusted."""
        kw = dict(funding_prints_bps=[1.5] * 3, **flat())
        assert cc.evaluate_entry(round_trip_bps=cc.ROUND_TRIP_BPS, borrow_apr=0.05, **kw) == cc.evaluate_entry(round_trip_bps=cc.ROUND_TRIP_BPS, borrow_apr=0.05, **kw)

    def test_borrow_is_explicit_at_the_declaration(self):
        """+1.77% financed vs +7.96% unfinanced is the difference between a
        business and a hobby, so the rate is never silently absent.

        0033: this test used to pin DEFAULT_BORROW_APR == 0.05 — a default,
        which is the opposite of what its docstring says. It now asserts the
        docstring: evaluate_entry has no default for borrow_apr, and no
        module-level default exists for anyone to reach for."""
        import inspect
        param = inspect.signature(cc.evaluate_entry).parameters["borrow_apr"]
        assert param.default is inspect.Parameter.empty
        assert not hasattr(cc, "DEFAULT_BORROW_APR")
        assert cc.ASSUMED_HOLD_DAYS == 30.0
