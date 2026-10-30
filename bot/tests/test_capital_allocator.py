"""Capital allocation: may be zero, never entitled, never invents an input.

Pure function tests (no store, no clock) plus one that proves allocation does
not itself reserve. Every number below is a TEST FIXTURE labelled as such; none
is a claim about any strategy's economics. Offline.
"""
from __future__ import annotations

import math
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import capital_allocator as ca                          # noqa: E402

SRC = "test-fixture"


def S(v):
    return ca.Sourced(v, SRC)


def opp(oid, strategy="carry", group="btc", **over):
    base = dict(
        expected_net_return_usd=10.0, capital_required_usd=100.0,
        funding_and_financing_usd=1.0, margin_requirement_usd=50.0,
        liquidity_usd=1_000.0, execution_cost_usd=0.5, capacity_usd=1_000.0,
        correlation={"book": 0.0}, max_tolerable_loss_usd=20.0,
        drawdown_contribution=0.01, tail_risk_usd=5.0, capital_duration_days=10.0)
    base.update(over)
    inputs = {k: (v if isinstance(v, ca.Sourced) or v is None else S(v))
              for k, v in base.items()}
    return ca.Opportunity(oid, strategy, S(group), inputs)


def limits(**over):
    base = dict(free_capital_usd=S(1_000.0), max_total_usd=1_000.0,
                per_strategy_max_usd={"carry": 1_000.0, "other": 1_000.0},
                max_total_loss_usd=1_000.0, max_total_drawdown_contribution=1.0,
                max_group_usd=1_000.0, book_tail_risk_usd=S(0.0))
    base.update(over)
    return ca.AllocationLimits(**base)


def by_id(alloc):
    return {r.opportunity_id: r for r in alloc.results}


def test_risk_aversion_is_required():
    with pytest.raises(TypeError):
        ca.allocate([opp("a")], limits())  # type: ignore[call-arg]


def test_zero_allocation_when_nothing_offered():
    a = ca.allocate([], limits(), risk_aversion=1.0)
    assert a.total_allocated_usd == 0.0 and a.allocated() == {}


def test_zero_allocation_when_risk_outweighs_return():
    a = ca.allocate([opp("a", expected_net_return_usd=1.0, tail_risk_usd=50.0)],
                    limits(), risk_aversion=1.0)
    r = by_id(a)["a"]
    assert r.allocated_usd == 0.0 and r.reasons == (ca.NO_RISK_ADJUSTED_VALUE,)
    assert a.total_allocated_usd == 0.0


@pytest.mark.parametrize("name", ca.REQUIRED_INPUTS)
def test_every_missing_input_makes_it_ineligible(name):
    a = ca.allocate([opp("a", **{name: None})], limits(), risk_aversion=1.0)
    r = by_id(a)["a"]
    assert not r.eligible and r.allocated_usd == 0.0
    assert f"{ca.MISSING_INPUT}:{name}" in r.reasons


@pytest.mark.parametrize("name", ca.REQUIRED_INPUTS)
def test_every_unsourced_input_makes_it_ineligible(name):
    good = opp("x")
    val = good.inputs[name].value
    a = ca.allocate([opp("a", **{name: ca.Sourced(val, "  ")})], limits(),
                    risk_aversion=1.0)
    assert f"{ca.UNSOURCED_INPUT}:{name}" in by_id(a)["a"].reasons


@pytest.mark.parametrize("over,code", [
    ({"capital_required_usd": float("nan")}, ca.INVALID_INPUT),
    ({"capital_required_usd": 0.0}, ca.INVALID_INPUT),
    ({"tail_risk_usd": -1.0}, ca.INVALID_INPUT),
    ({"correlation": {"book": 1.5}}, ca.INVALID_INPUT),
    ({"correlation": {"other": 0.1}}, ca.MISSING_INPUT),
    ({"expected_net_return_usd": True}, ca.INVALID_INPUT),
    ({"margin_requirement_usd": 200.0}, ca.INCONSISTENT_INPUTS),
    ({"capacity_usd": 50.0}, ca.EXCEEDS_CAPACITY),
    ({"liquidity_usd": 50.0}, ca.EXCEEDS_LIQUIDITY),
])
def test_invalid_inputs_are_ineligible(over, code):
    a = ca.allocate([opp("a", **over)], limits(), risk_aversion=1.0)
    r = by_id(a)["a"]
    assert not r.eligible and any(x.split(":")[0] == code for x in r.reasons)


def test_missing_correlation_group_is_ineligible():
    o = opp("a")
    o = ca.Opportunity("a", "carry", None, o.inputs)
    r = by_id(ca.allocate([o], limits(), risk_aversion=1.0))["a"]
    assert f"{ca.MISSING_INPUT}:correlation_group" in r.reasons


def test_negative_expected_net_gets_nothing():
    a = ca.allocate([opp("neg", expected_net_return_usd=-5.0, tail_risk_usd=0.0),
                     opp("zero", expected_net_return_usd=0.0, tail_risk_usd=0.0)],
                    limits(), risk_aversion=0.0)
    for oid in ("neg", "zero"):
        r = by_id(a)[oid]
        assert r.allocated_usd == 0.0 and ca.NON_POSITIVE_EXPECTED_NET in r.reasons


def test_strategy_without_a_cap_gets_nothing():
    a = ca.allocate([opp("a", strategy="unknown")], limits(), risk_aversion=1.0)
    assert ca.NO_STRATEGY_CAP in by_id(a)["a"].reasons


def test_capital_exhaustion_takes_best_first():
    ops = [opp("a", expected_net_return_usd=10.0),
           opp("b", expected_net_return_usd=20.0),
           opp("c", expected_net_return_usd=15.0)]
    a = ca.allocate(ops, limits(free_capital_usd=S(250.0)), risk_aversion=0.0)
    got = a.allocated()
    assert got == {"b": 100.0, "c": 100.0}
    assert by_id(a)["a"].reasons == (ca.FREE_CAPITAL_EXHAUSTED,)
    assert a.free_capital_remaining_usd == pytest.approx(50.0)


def test_capital_time_basis_prefers_shorter_lockup():
    ops = [opp("long", expected_net_return_usd=20.0, capital_duration_days=30.0),
           opp("short", expected_net_return_usd=10.0, capital_duration_days=5.0)]
    a = ca.allocate(ops, limits(free_capital_usd=S(100.0)), risk_aversion=0.0)
    assert a.allocated() == {"short": 100.0}
    assert by_id(a)["short"].score == pytest.approx(10.0 / (100.0 * 5.0))


def test_correlated_pair_second_is_penalised_and_capped():
    ops = [opp("a", correlation={"book": 0.0, "b": 1.0}, tail_risk_usd=8.0),
           opp("b", correlation={"book": 0.0, "a": 1.0}, tail_risk_usd=8.0,
               expected_net_return_usd=9.0)]
    # a hedging alternative with the same economics as b (the effective rho
    # is the LARGEST of book and pairwise, so both must be -1 to hedge)
    ops_unc = [opp("a", correlation={"book": -1.0, "b": -1.0}, tail_risk_usd=8.0),
               opp("b", correlation={"book": -1.0, "a": -1.0}, tail_risk_usd=8.0,
                   expected_net_return_usd=9.0)]
    a_corr = ca.allocate(ops, limits(), risk_aversion=1.0)
    a_unc = ca.allocate(ops_unc, limits(), risk_aversion=1.0)
    rb_corr, rb_unc = by_id(a_corr)["b"], by_id(a_unc)["b"]
    assert rb_corr.incremental_tail_risk_usd == pytest.approx(8.0)
    assert rb_unc.incremental_tail_risk_usd == pytest.approx(-8.0)
    assert rb_corr.score < rb_unc.score
    # a negative pairwise rho does not override a positive book rho
    ops_mixed = [opp("a", correlation={"book": 0.0, "b": -1.0}, tail_risk_usd=8.0),
                 opp("b", correlation={"book": 0.0, "a": -1.0}, tail_risk_usd=8.0,
                     expected_net_return_usd=9.0)]
    rb_mixed = by_id(ca.allocate(ops_mixed, limits(), risk_aversion=1.0))["b"]
    assert rb_mixed.incremental_tail_risk_usd == pytest.approx(
        math.sqrt(8.0 ** 2 + 8.0 ** 2) - 8.0)
    # concentration cap: same group, cap fits only one
    a_cap = ca.allocate(ops, limits(max_group_usd=150.0), risk_aversion=1.0)
    assert len(a_cap.allocated()) == 1
    assert by_id(a_cap)["b"].reasons == (ca.CONCENTRATION_CAP,)


def test_unstated_pairwise_correlation_is_taken_at_worst_case():
    ops = [opp("a", tail_risk_usd=8.0), opp("b", tail_risk_usd=8.0,
                                            expected_net_return_usd=9.0)]
    a = ca.allocate(ops, limits(), risk_aversion=1.0)
    assert by_id(a)["b"].incremental_tail_risk_usd == pytest.approx(8.0)


@pytest.mark.parametrize("lim,code", [
    ({"max_total_usd": 150.0}, ca.TOTAL_CAP),
    ({"per_strategy_max_usd": {"carry": 150.0}}, ca.STRATEGY_CAP),
    ({"max_total_loss_usd": 30.0}, ca.TOTAL_LOSS_CAP),
    ({"max_total_drawdown_contribution": 0.015}, ca.DRAWDOWN_CAP),
])
def test_caps(lim, code):
    a = ca.allocate([opp("a"), opp("b", expected_net_return_usd=9.0)],
                    limits(**lim), risk_aversion=0.0)
    assert a.allocated() == {"a": 100.0}
    assert by_id(a)["b"].reasons == (code,)


def test_ties_are_broken_deterministically():
    ops = [opp("z"), opp("m"), opp("a")]
    first = ca.allocate(ops, limits(free_capital_usd=S(100.0)), risk_aversion=0.0)
    for perm in ([ops[1], ops[2], ops[0]], [ops[2], ops[0], ops[1]]):
        again = ca.allocate(perm, limits(free_capital_usd=S(100.0)),
                            risk_aversion=0.0)
        assert again == first
    assert first.allocated() == {"a": 100.0}


@pytest.mark.parametrize("lim", [
    {"free_capital_usd": ca.Sourced(1000.0, "")},
    {"free_capital_usd": S(float("nan"))},
    {"book_tail_risk_usd": S(None)},
    {"max_total_usd": float("nan")},
])
def test_invalid_limits_allocate_nothing(lim):
    a = ca.allocate([opp("a")], limits(**lim), risk_aversion=1.0)
    assert a.total_allocated_usd == 0.0 and a.reasons
    assert all(x.startswith(ca.LIMITS_INVALID) for x in a.reasons)


def test_invalid_risk_aversion_allocates_nothing():
    a = ca.allocate([opp("a")], limits(), risk_aversion=float("nan"))
    assert a.total_allocated_usd == 0.0


def test_allocation_does_not_reserve(tmp_path):
    import capital_reservations as cr
    from persistence import StateStore
    from portfolio_state import confirmed
    store = StateStore(str(tmp_path / "s.db"))
    try:
        res = cr.CapitalReservations(store, buffer_usd=0.0, max_available_age_s=60)
        now = time.time()
        avail = confirmed("available_capital", 1000.0, now, "test-fixture")
        free = res.free_capital_usd(avail, "acct", now=now)
        before = store._query(
            "SELECT COUNT(*) AS n FROM capital_reservation")[0]["n"]
        a = ca.allocate([opp("a"), opp("b")],
                        limits(free_capital_usd=S(free.detail["free_usd"])),
                        risk_aversion=0.0)
        assert a.total_allocated_usd == 200.0
        after = store._query(
            "SELECT COUNT(*) AS n FROM capital_reservation")[0]["n"]
        assert before == after == 0
        assert res.live_total_usd("acct") == 0.0
        # the caller reserves each line; the reservation re-checks atomically
        for oid, usd in a.allocated().items():
            assert res.reserve("carry", oid, usd, "acct", "entry",
                               available=avail, ttl_s=60, now=now)
        assert res.live_total_usd("acct") == 200.0
    finally:
        store.close()


def test_incremental_tail_risk_formula():
    assert ca.incremental_tail_risk(0.0, 5.0, 0.3) == pytest.approx(5.0)
    assert ca.incremental_tail_risk(3.0, 4.0, 0.0) == pytest.approx(2.0)
    assert ca.incremental_tail_risk(3.0, 4.0, 1.0) == pytest.approx(4.0)
    assert math.isfinite(ca.incremental_tail_risk(5.0, 5.0, -1.0))
