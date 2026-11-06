"""The ledger must not flatter an incomplete or inconsistent trade.

Three defects found by an independent economics red team, each reproduced before it was fixed:

* an unpriced cost was counted as ZERO in the net, the returns and the break-even, with the "incomplete" mark only on the hurdle
  rows, so the headline number was an optimistic bound presented as a figure;
* ``cost_bps_of_notional_round_trip`` added funding PAID and an ADVERSE basis to the costs and ignored funding received and a
  favourable basis, while the break-even netted the basis (two definitions of "cost" in one record);
* the annualisation used the declared holding period without checking it against the funding path (a 30-day hold with 3 prints
  read -1 %/yr; a 3-day hold with 90 prints read +52 %/yr).

All fields added are additive; the legacy keys keep their values.
"""
from __future__ import annotations

import math
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import trade_economics as TE  # noqa: E402


def pos(hold: float = 30.0, **kw):
    base = dict(notional_usd=100_000.0, hold_days=hold, btc_inventory_usd=100_000.0, btc_was_owned=True,
                margin_posted_usd=0.0, collateral_haircut=0.1, reserve_usd=10_000.0,
                maintenance_margin_rate=0.005, initial_margin_rate=0.1)
    base.update(kw)
    return TE.Position(**base)


def complete_costs(**kw):
    base = dict(perp_entry_fee_bps=5.5, perp_exit_fee_bps=5.5, spread_bps=0.0, slippage_bps=0.0, impact_bps=0.0, latency_bps=0.0,
                conversion_bps=0.0, transfer_usd=0.0, custody_apr=0.0, operational_usd=0.0, borrow_apr=0.0)
    base.update(kw)
    return TE.Costs(**base)


def open_costs():
    return TE.Costs(perp_entry_fee_bps=5.5, perp_exit_fee_bps=5.5)


# ---- unknown costs ---------------------------------------------------------------------------------------------------------

def test_incomplete_net_is_labelled_an_optimistic_bound():
    r = TE.compute(pos(), open_costs(), [0.65] * 90)
    assert r["unknowns"]
    assert r["net_is_optimistic_bound"] is True
    assert r["breakeven_is_lower_bound"] is True
    assert "OPTIMISTIC BOUND" in r["net_reading"] and "LOWER" in r["net_reading"]


def test_complete_net_is_not_labelled_a_bound():
    r = TE.compute(pos(), complete_costs(), [0.65] * 90)
    assert r["unknowns"] == []
    assert r["net_is_optimistic_bound"] is False and r["breakeven_is_lower_bound"] is False
    assert r["net_reading"] == "net of every declared cost"


def test_the_bound_is_really_optimistic_pricing_the_unknowns_only_lowers_net():
    open_ = TE.compute(pos(), open_costs(), [0.65] * 90)
    priced = TE.compute(pos(), TE.Costs(perp_entry_fee_bps=5.5, perp_exit_fee_bps=5.5, spread_bps=1.0, slippage_bps=1.0, impact_bps=1.0,
                                        latency_bps=1.0, conversion_bps=0.0, transfer_usd=0.0, custody_apr=0.0, operational_usd=0.0,
                                        borrow_apr=0.0), [0.65] * 90)
    assert priced["net_pnl_usd"] < open_["net_pnl_usd"]
    assert priced["breakeven_funding_bps_per_settlement"] > open_["breakeven_funding_bps_per_settlement"]


def test_hurdle_verdicts_distinguish_a_conclusive_failure_from_an_inconclusive_pass():
    # the unknown-cost ledger at 0.65 bps clears nothing at 10 %: a failure on an OPTIMISTIC bound is conclusive.
    fail = TE.compute(pos(), open_costs(), [0.65] * 90)["hurdles"]
    assert all(h["verdict"] == "FAILS_EVEN_ON_AN_OPTIMISTIC_BOUND" and h["conclusive"] is True for h in fail)
    assert all(h["meets"] is False for h in fail)
    # a rich funding path passes on the modelled costs but a pass with unknowns is NOT conclusive
    rich = TE.compute(pos(), open_costs(), [4.0] * 90)["hurdles"]
    assert all(h["meets"] is True and h["verdict"] == "PASSES_ONLY_ON_MODELLED_COSTS" and h["conclusive"] is False for h in rich)
    # fully priced: both outcomes are conclusive
    pass_full = TE.compute(pos(), complete_costs(), [4.0] * 90)["hurdles"]
    assert all(h["verdict"] == "MEETS_ON_ALL_DECLARED_COSTS" and h["conclusive"] is True for h in pass_full)
    fail_full = TE.compute(pos(), complete_costs(), [0.3] * 90)["hurdles"]
    assert all(h["verdict"] == "FAILS_ON_ALL_DECLARED_COSTS" and h["conclusive"] is True for h in fail_full)


def test_legacy_fields_keep_their_values():
    r = TE.compute(pos(), open_costs(), [0.65] * 90)
    ann = r["returns"]["annualised_pct_on_economic_capital"]
    assert ann == pytest.approx((r["net_pnl_usd"] / 110_000.0) * 365.0 / 30.0 * 100.0)
    assert r["breakeven_funding_bps_per_settlement"] == pytest.approx(11.0 / 90.0)
    for h in r["hurdles"]:
        assert {"hurdle_apr_pct", "status", "annualised_pct_on_economic_capital", "meets", "meets_means", "incomplete",
                "unpriced_inputs", "margin_pct_points"} <= set(h)


# ---- the cost label --------------------------------------------------------------------------------------------------------

def test_explicit_cost_is_costs_alone_whatever_funding_and_basis_do():
    ref = TE.compute(pos(), complete_costs(), [0.0] * 90)["explicit_cost_bps_of_notional"]
    assert ref == pytest.approx(11.0)
    for funding in ([-1.0] * 90, [3.0] * 90):
        for e, x in ((0.0, 0.0), (10.0, 0.0), (0.0, 10.0)):
            r = TE.compute(pos(), complete_costs(), funding, entry_basis_bps=e, exit_basis_bps=x)
            assert r["explicit_cost_bps_of_notional"] == pytest.approx(11.0)


def test_rebates_reduce_explicit_cost():
    r = TE.compute(pos(), complete_costs(rebate_bps=2.0), [0.0] * 90)
    assert r["explicit_cost_bps_of_notional"] == pytest.approx(9.0)


def test_legacy_cost_label_mixes_funding_and_basis_and_says_so():
    r = TE.compute(pos(), complete_costs(), [-1.0] * 90)
    assert r["cost_bps_of_notional_round_trip"] == pytest.approx(101.0)      # 11 fees + 90 paid funding (the legacy meaning)
    assert r["explicit_cost_bps_of_notional"] == pytest.approx(11.0)
    assert "funding PAID" in r["cost_bps_of_notional_round_trip_includes"]


def test_explicit_cost_ties_to_the_closed_form_for_random_inputs():
    import random
    rng = random.Random(20261106)
    for _ in range(200):
        fee_in, fee_out = rng.uniform(0, 8), rng.uniform(0, 8)
        spread, slip = rng.uniform(0, 3), rng.uniform(0, 3)
        rebate = rng.uniform(0, 1)
        f = [rng.uniform(-3, 3) for _ in range(90)]
        r = TE.compute(pos(), complete_costs(perp_entry_fee_bps=fee_in, perp_exit_fee_bps=fee_out, spread_bps=spread,
                                             slippage_bps=slip, rebate_bps=rebate), f,
                       entry_basis_bps=rng.uniform(-5, 5), exit_basis_bps=rng.uniform(-5, 5))
        assert r["explicit_cost_bps_of_notional"] == pytest.approx(fee_in + fee_out + spread + slip - rebate)


# ---- hold / path consistency -----------------------------------------------------------------------------------------------

def test_a_30_day_hold_with_three_prints_is_inconsistent_and_meets_nothing():
    r = TE.compute(pos(30), complete_costs(), [5.0] * 3)
    assert r["consistency"]["funding_path_matches_hold"] is False
    assert r["consistency"]["settlements_expected_from_hold"] == pytest.approx(90.0)
    assert "INCONSISTENT" in r["consistency"]["reading"]
    assert all(h["meets"] is False and h["verdict"] == "INCONSISTENT_INPUT" and h["conclusive"] is False for h in r["hurdles"])


def test_a_3_day_hold_with_90_prints_cannot_report_a_hurdle_as_met():
    r = TE.compute(pos(3), complete_costs(), [0.65] * 90)
    assert r["returns"]["annualised_pct_on_economic_capital"] > 20          # the nonsense annualisation is still shown, not hidden
    assert all(h["meets"] is False and h["verdict"] == "INCONSISTENT_INPUT" for h in r["hurdles"])


@pytest.mark.parametrize("hold,n", [(30, 90), (30, 89), (30, 91), (5.8, 17), (5.8, 18), (7, 21), (1, 3), (90, 270)])
def test_one_settlement_of_slack_is_accepted(hold, n):
    r = TE.compute(pos(hold), complete_costs(), [1.0] * n)
    assert r["consistency"]["funding_path_matches_hold"] is True
    assert math.isclose(r["consistency"]["settlements_expected_from_hold"], hold * 3.0)


@pytest.mark.parametrize("hold,n", [(30, 87), (30, 93), (5.8, 14), (5.8, 21), (7, 18)])
def test_two_or_more_settlements_off_is_inconsistent(hold, n):
    assert TE.compute(pos(hold), complete_costs(), [1.0] * n)["consistency"]["funding_path_matches_hold"] is False


def test_the_sensitivity_grid_builds_consistent_paths_and_every_row_keeps_its_hurdle_marks():
    holds = (7, 14, 30, 60, 90)
    rows = TE.sensitivity(pos(30), complete_costs(), 0.65, holds_days=holds)
    assert len(rows) == len(holds) * len(TE.funding_paths(0.65, 9))
    for d in holds:
        n = int(round(d * TE.SETTLEMENTS_PER_DAY))
        for path in TE.funding_paths(0.65, n).values():
            r = TE.compute(pos(float(d)), complete_costs(), path, funding_source="SCENARIO")
            assert r["consistency"]["funding_path_matches_hold"] is True
    assert all(row["incomplete"] is False for row in rows)
