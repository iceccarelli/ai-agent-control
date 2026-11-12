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


class TestThePerpDiscountBound:
    def test_a_discount_beyond_the_budget_is_refused(self):
        v = cc.evaluate_entry(round_trip_bps=cc.ROUND_TRIP_BPS, borrow_apr=0.05, funding_prints_bps=[2.0] * 3, **at_basis(-150.0))
        assert v.allowed is False
        assert v.reason == "ENTRY_BASIS_DISCOUNT_EXCEEDS_CARRY_BUDGET"

    def test_a_small_discount_inside_the_budget_is_allowed(self):
        v = cc.evaluate_entry(round_trip_bps=cc.ROUND_TRIP_BPS, borrow_apr=0.05, funding_prints_bps=[2.0] * 3, **at_basis(-20.0))
        assert v.allowed is True


# ---------------------------------------------------------------------------
# The continuation exit (Round 16): leave when the funding shortfall paid while
# holding reaches the priced round trip (ski-rental / CUSUM). No free parameter.
# ---------------------------------------------------------------------------

class TestTheFundingShortfallAccumulator:
    def test_capital_cost_per_print_is_the_gates_own_daily_unit(self):
        # 4.4%/yr = 4.4/365*1e4 bps/day = 1.2055 bps/day = 0.4018 bps per 8h
        assert cc.capital_cost_bps_per_print(0.0, 0.044) == pytest.approx(
            0.044 / 365.0 * 1e4 / 3.0)
        # borrow and opportunity cost are the same kind of cost, added
        assert cc.capital_cost_bps_per_print(0.05, 0.044) == pytest.approx(
            (0.05 + 0.044) / 365.0 * 1e4 / 3.0)
        assert cc.capital_cost_bps_per_print(0.0, 0.0) == 0.0

    @pytest.mark.parametrize("bad", [None, float("nan"), float("inf"), -0.01,
                                     "x", True])
    def test_an_unusable_capital_cost_is_unknown_never_zero(self, bad):
        assert cc.capital_cost_bps_per_print(bad, 0.0) is None
        assert cc.capital_cost_bps_per_print(0.0, bad) is None

    def test_a_print_below_the_capital_cost_adds_the_gap(self):
        # c = 0.4, f = 0.1: the pair earns 0.3 less than holding costs
        assert cc.exit_shortfall_step(0.0, 0.1, 0.4) == pytest.approx(0.3)
        assert cc.exit_shortfall_step(2.0, -1.0, 0.0) == pytest.approx(3.0)

    def test_a_print_above_the_capital_cost_pays_the_shortfall_down_to_zero(self):
        assert cc.exit_shortfall_step(2.0, 1.5, 0.0) == pytest.approx(0.5)
        assert cc.exit_shortfall_step(2.0, 9.0, 0.0) == 0.0   # never negative

    @pytest.mark.parametrize("args", [(None, 1.0, 0.0), (0.0, float("nan"), 0.0),
                                      (0.0, 1.0, None), (-1.0, 1.0, 0.0),
                                      (0.0, float("inf"), 0.0)])
    def test_an_unusable_input_advances_nothing(self, args):
        assert cc.exit_shortfall_step(*args) is None

    def test_a_recovering_dip_never_costs_a_round_trip(self):
        """Oscillation around zero funding (the churn case) leaves the pair in:
        every dip is paid back before it reaches the round trip."""
        rt, s = 11.0, 0.0
        # a mean-zero pattern: every dip is paid back by the next rich prints
        # (a pattern with net NEGATIVE funding does accumulate, and must)
        for f in [-3.0, -3.0, 6.0, -4.0, -4.0, 8.0] * 20:
            s = cc.exit_shortfall_step(s, f, 0.0)
            assert not cc.continuation_exit(shortfall_bps=s,
                                            round_trip_bps=rt)

    def test_the_bleed_in_one_episode_is_bounded_by_the_round_trip(self):
        """The structural guarantee: whatever the funding does, the shortfall
        paid before the rule fires is below RT plus one print's shortfall."""
        import random
        rng = random.Random(1610)
        rt = 11.0
        for _ in range(200):
            s, paid = 0.0, 0.0
            for _ in range(400):
                f = rng.gauss(-0.5, 2.0)
                s = cc.exit_shortfall_step(s, f, 0.0)
                paid = s
                if cc.continuation_exit(shortfall_bps=s, round_trip_bps=rt):
                    break
            assert paid < rt + 15.0      # RT + the largest single shortfall seen


class TestTheContinuationVerdict:
    def test_it_leaves_exactly_when_the_shortfall_reaches_the_round_trip(self):
        v = cc.continuation_exit(shortfall_bps=11.0, round_trip_bps=11.0)
        assert v.close is True and bool(v) is True
        assert v.reason == "SHORTFALL_REACHED_ROUND_TRIP"
        assert v.switching_cost_bps == 11.0

    def test_it_stays_below_the_round_trip(self):
        v = cc.continuation_exit(shortfall_bps=10.999, round_trip_bps=11.0)
        assert v.close is False and bool(v) is False
        assert v.reason == "SHORTFALL_BELOW_ROUND_TRIP"

    @pytest.mark.parametrize("s,rt", [(None, 11.0), (1.0, None), (1.0, 0.0),
                                      (1.0, -1.0), (float("nan"), 11.0),
                                      (1.0, float("inf")), (-1.0, 11.0)])
    def test_an_unjudgeable_exit_says_so_so_the_caller_falls_back(self, s, rt):
        v = cc.continuation_exit(shortfall_bps=s, round_trip_bps=rt)
        assert v.close is None and bool(v) is False
        assert v.reason == "EXIT_INPUT_INVALID"

    def test_the_policies_are_the_two_the_engine_accepts(self):
        assert cc.EXIT_POLICIES == ("funding_streak", "continuation")
