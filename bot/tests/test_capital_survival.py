"""Offline leverage and portfolio-survival simulation: hand-computed numbers, labels, and boundaries.

Fixture rules (NOT a venue document): haircut 5%, initial margin 2%, maintenance margin 0.5%,
liquidation fee 50 bps. Capital C = 100,000. At leverage L the hand arithmetic is
  N = L C, financed F = (L-1) C, margin equity E0 = N (1-h) - F = C (1 - 0.05 L),
  IM = 0.02 N, MM = 0.005 N, and the reserve is 5% of N, outside E0.
"""
import ast
import copy
import dataclasses
import itertools
import json
import math
import os
import random
import re
import subprocess
import sys

import pytest

import capital_survival as CS
import evidence_stats as ES
import trade_economics as TE

HERE = os.path.dirname(os.path.abspath(__file__))
BOT = os.path.dirname(HERE)
SRC = os.path.join(BOT, "capital_survival.py")

C = 100_000.0
GRID = (1.0, 1.25, 1.5, 2.0, 3.0, 5.0, 10.0)
BORN_SOURCE = "unit-test fixture, not a venue document"
RATES = {"low": 0.04, "base": 0.08, "high": 0.12}


def rules(**kw):
    d = dict(btc_collateral_haircut=0.05, initial_margin_rate=0.02, maintenance_margin_rate=0.005,
             liquidation_fee_bps=50.0, verified=True, source=BORN_SOURCE)
    d.update(kw)
    return CS.VenueRules(**d)


def costs(**kw):
    d = dict(perp_entry_fee_bps=5.5, perp_exit_fee_bps=5.5, spread_bps=0.2, slippage_bps=0.3, impact_bps=0.1,
             latency_bps=0.0, conversion_bps=0.0, transfer_usd=0.0, custody_apr=0.0, operational_usd=0.0,
             fee_provenance="ACCOUNT_VERIFIED")
    d.update(kw)
    return TE.Costs(**d)


def zero_costs(**kw):
    d = dict(perp_entry_fee_bps=0.0, perp_exit_fee_bps=0.0, spread_bps=0.0, slippage_bps=0.0, impact_bps=0.0,
             latency_bps=0.0, conversion_bps=0.0, transfer_usd=0.0, custody_apr=0.0, operational_usd=0.0,
             fee_provenance="ACCOUNT_VERIFIED")
    d.update(kw)
    return TE.Costs(**d)


def inputs(**kw):
    d = dict(capital_usd=C, hold_days=30.0, funding_bps_path=[0.65] * 90, costs=costs(),
             reserve_frac_of_notional=0.05, venue_rules=rules(), financing_apr_scenarios=None)
    d.update(kw)
    return CS.CarryInputs(**d)


def book(L, **kw):
    return CS.leverage_book(inputs(**kw), L)


def hit(L, scenario, apr=None, **kw):
    kw.setdefault("costs", zero_costs())
    return CS.evaluate_scenario(book(L, **kw), scenario, financing_apr=apr)


def history(seed=11, n_days=120, fund_mean=0.0, fund_sd=0.5, basis_sd=15.0, ret_sd=0.03):
    rnd = random.Random(seed)
    return {"funding_bps": [rnd.gauss(fund_mean, fund_sd) for _ in range(3 * n_days)],
            "basis_bps": [rnd.gauss(0.0, basis_sd) for _ in range(n_days)],
            "daily_returns": [rnd.gauss(0.0, ret_sd) for _ in range(n_days)]}


def constant_history(funding=1.0, basis=5.0, ret=0.0, n_days=40):
    return {"funding_bps": [funding] * (3 * n_days), "basis_bps": [basis] * n_days, "daily_returns": [ret] * n_days}


def gap(pct, rebalanced=True):
    return CS.StressScenario(f"gap_{pct}_{rebalanced}", gap_pct=pct, hedge_rebalanced=rebalanced)


def basis(bps):
    return CS.StressScenario(f"basis_{bps}", basis_bps=bps)


S = CS.StressScenario

# ---------------------------------------------------------------------------------------------
# Leverage definition and the capital stack
# ---------------------------------------------------------------------------------------------


def test_the_leverage_grid_is_the_seven_levels_and_the_table_covers_all_of_them():
    assert CS.LEVERAGES == GRID
    t = CS.survival_table(inputs())
    assert [b["leverage"] for b in t["books"]] == list(GRID)


def test_1x_book_capital_stack_by_hand():
    b = book(1.0)
    assert b["capital"] == {"hedge_capital_usd": 100_000.0, "hedged_notional_usd": 100_000.0,
                            "financed_notional_usd": 0.0, "reserve_usd": 5_000.0, "economic_capital_usd": 105_000.0,
                            "notional_over_hedge_capital": 1.0, "notional_over_economic_capital": pytest.approx(100_000 / 105_000)}
    m = b["margin"]
    assert m["collateral_haircut_usd"] == pytest.approx(5_000.0) and m["collateral_credit_usd"] == pytest.approx(95_000.0)
    assert m["venue_equity_usd"] == pytest.approx(95_000.0)
    assert m["required_initial_margin_usd"] == pytest.approx(2_000.0) and m["maintenance_margin_usd"] == pytest.approx(500.0)
    assert m["initial_margin_surplus_usd"] == pytest.approx(93_000.0) and m["opens"] is True


def test_2x_book_capital_stack_by_hand():
    b = book(2.0)
    assert b["capital"]["hedged_notional_usd"] == 200_000.0 and b["capital"]["financed_notional_usd"] == 100_000.0
    assert b["capital"]["reserve_usd"] == 10_000.0 and b["capital"]["economic_capital_usd"] == 110_000.0
    m = b["margin"]
    assert m["collateral_haircut_usd"] == pytest.approx(10_000.0) and m["collateral_credit_usd"] == pytest.approx(190_000.0)
    assert m["venue_equity_usd"] == pytest.approx(90_000.0)            # 190,000 credit - 100,000 borrowed
    assert m["required_initial_margin_usd"] == pytest.approx(4_000.0) and m["maintenance_margin_usd"] == pytest.approx(1_000.0)


def test_10x_book_capital_stack_by_hand():
    b = book(10.0)
    assert b["capital"]["financed_notional_usd"] == 900_000.0 and b["capital"]["economic_capital_usd"] == 150_000.0
    m = b["margin"]
    assert m["venue_equity_usd"] == pytest.approx(50_000.0)            # 950,000 - 900,000
    assert m["required_initial_margin_usd"] == pytest.approx(20_000.0) and m["maintenance_margin_usd"] == pytest.approx(5_000.0)
    assert m["opens"] is True


@pytest.mark.parametrize("L", GRID)
def test_with_no_reserve_leverage_is_exactly_notional_over_economic_capital(L):
    b = book(L, reserve_frac_of_notional=0.0)
    assert b["capital"]["notional_over_economic_capital"] == pytest.approx(L)
    assert b["capital"]["economic_capital_usd"] == pytest.approx(C)


def test_a_reserve_lowers_the_literal_ratio_and_the_return_on_capital_but_not_the_margin_equity():
    flat, rsv = book(2.0, reserve_frac_of_notional=0.0), book(2.0, reserve_frac_of_notional=0.05)
    assert rsv["capital"]["notional_over_economic_capital"] < flat["capital"]["notional_over_economic_capital"]
    assert rsv["margin"]["venue_equity_usd"] == pytest.approx(flat["margin"]["venue_equity_usd"])
    f = flat["returns"]["before_financing"]["annualised_pct_on_economic_capital"]
    r = rsv["returns"]["before_financing"]["annualised_pct_on_economic_capital"]
    assert r < f


@pytest.mark.parametrize("bad", [0.99, 0.5, 0.0, -1.0, float("nan"), float("inf"), None, True, "2"])
def test_leverage_below_one_or_not_a_number_is_refused(bad):
    with pytest.raises(ValueError):
        CS.leverage_book(inputs(), bad)


@pytest.mark.parametrize("kw", [dict(capital_usd=0.0), dict(capital_usd=-5.0), dict(capital_usd=float("nan")),
                                dict(hold_days=0.0), dict(reserve_frac_of_notional=-0.01),
                                dict(reserve_frac_of_notional=None), dict(funding_bps_path=[]),
                                dict(funding_bps_path=[0.5, float("nan")]), dict(costs="costs"),
                                dict(venue_rules=42), dict(financing_apr_scenarios=[0.08]),
                                dict(alternative_return_apr_pct=float("inf"))])
def test_malformed_inputs_are_refused(kw):
    with pytest.raises(ValueError):
        inputs(**kw)


# ---------------------------------------------------------------------------------------------
# Liquidation headroom
# ---------------------------------------------------------------------------------------------
def test_1x_liquidation_headroom_by_hand():
    h = book(1.0)["headroom"]
    # (E0 - MM) / (N (1 + mm)) = (95,000 - 500) / (100,000 x 1.005)
    assert h["perp_up_vs_collateral_pct"] == pytest.approx(94_500 / 100_500 * 100)
    # mirror: (E0 - MM) / (N (1 - h)) = 94,500 / 95,000
    assert h["collateral_down_vs_perp_pct"] == pytest.approx(94_500 / 95_000 * 100)
    assert h["liquidation_headroom_pct"] == pytest.approx(94_500 / 100_500 * 100) and 94.0 < h["liquidation_headroom_pct"] < 94.1


def test_2x_liquidation_headroom_by_hand():
    h = book(2.0)["headroom"]
    # (90,000 - 1,000) / (200,000 x 1.005)
    assert h["perp_up_vs_collateral_pct"] == pytest.approx(89_000 / 201_000 * 100)
    assert h["collateral_down_vs_perp_pct"] == pytest.approx(89_000 / 190_000 * 100)
    assert h["liquidation_headroom_pct"] == pytest.approx(89_000 / 201_000 * 100) and 44.27 < h["liquidation_headroom_pct"] < 44.29


def test_10x_liquidation_headroom_by_hand():
    h = book(10.0)["headroom"]
    assert h["liquidation_headroom_pct"] == pytest.approx(45_000 / 1_005_000 * 100) and 4.47 < h["liquidation_headroom_pct"] < 4.48


def test_headroom_strictly_decreases_with_leverage():
    hs = [book(L)["headroom"]["liquidation_headroom_pct"] for L in GRID]
    assert all(a > b for a, b in zip(hs, hs[1:])), hs
    assert hs[0] > 90.0 > 5.0 > hs[-1] > 0.0


@pytest.mark.parametrize("L", [1.0, 2.0, 10.0])
def test_headroom_is_exactly_the_move_that_exhausts_equity(L):
    b = book(L, costs=zero_costs())
    x = b["headroom"]["perp_up_vs_collateral_pct"]
    edge = CS.evaluate_scenario(b, S("edge", gap_pct=x, hedge_rebalanced=False))
    assert abs(edge["margin_buffer_usd"]) < 1e-5                      # equity == maintenance margin at the headroom
    assert CS.evaluate_scenario(b, S("inside", gap_pct=x * 0.99, hedge_rebalanced=False))["survives"] is True
    assert CS.evaluate_scenario(b, S("beyond", gap_pct=x * 1.01, hedge_rebalanced=False))["liquidated"] is True


def test_the_reserve_widens_the_what_if_headroom_but_is_not_counted_in_the_survival_headroom():
    h = book(2.0)["headroom"]
    assert h["with_reserve_topup_pct"] == pytest.approx((90_000 + 10_000 - 1_000) / 201_000 * 100)
    assert h["with_reserve_topup_pct"] > h["perp_up_vs_collateral_pct"] == h["liquidation_headroom_pct"]
    assert book(2.0, reserve_frac_of_notional=0.0)["headroom"]["liquidation_headroom_pct"] == pytest.approx(h["liquidation_headroom_pct"])


def test_a_book_whose_margin_equity_is_below_maintenance_has_zero_headroom_and_does_not_open():
    b = book(10.0, venue_rules=rules(btc_collateral_haircut=0.10))   # E0 = C (1 - 0.1 x 10) = 0
    assert b["margin"]["venue_equity_usd"] == pytest.approx(0.0, abs=1e-6)
    assert b["margin"]["opens"] is False and b["headroom"]["liquidation_headroom_pct"] == 0.0


# ---------------------------------------------------------------------------------------------
# Returns (trade_economics is reused) and opportunity cost
# ---------------------------------------------------------------------------------------------
def test_returns_are_trade_economics_compute_on_the_same_position():
    b = book(1.0)
    pos = TE.Position(notional_usd=100_000.0, hold_days=30.0, btc_inventory_usd=100_000.0, btc_was_owned=True,
                      margin_posted_usd=0.0, collateral_haircut=0.05, reserve_usd=5_000.0,
                      maintenance_margin_rate=0.005, initial_margin_rate=0.02)
    ref = TE.compute(pos, dataclasses.replace(costs(), borrow_apr=0.0, borrowed_usd=0.0), [0.65] * 90, funding_source="SCENARIO")
    got = b["returns"]["before_financing"]
    assert got["net_pnl_usd"] == ref["net_pnl_usd"] and got["reconciles"] is True and ref["reconciles"] is True
    assert got["annualised_pct_on_economic_capital"] == ref["returns"]["annualised_pct_on_economic_capital"]
    assert got["annualised_pct_on_notional"] == ref["returns"]["annualised_pct_on_notional"]
    assert got["components_usd"] == ref["components_usd"]


def test_1x_returns_by_hand():
    r = book(1.0)["returns"]["before_financing"]
    # funding 100,000 x 0.65 x 90 / 1e4 = 585; costs 55 + 55 + 2 + 3 + 1 = 116
    assert r["net_pnl_usd"] == pytest.approx(469.0)
    assert r["annualised_pct_on_notional"] == pytest.approx(469 / 100_000 * 365 / 30 * 100)
    assert r["annualised_pct_on_economic_capital"] == pytest.approx(469 / 105_000 * 365 / 30 * 100)
    assert r["annualised_pct_on_economic_capital"] < r["annualised_pct_on_notional"]
    assert [h["hurdle_apr_pct"] for h in r["hurdles"]] == [10.0, 15.0, 20.0]
    assert all(h["status"] == TE.PROVISIONAL and h["meets"] is False for h in r["hurdles"])


@pytest.mark.parametrize("L", GRID)
def test_leverage_adds_no_edge_the_return_on_notional_is_the_same_at_every_level(L):
    one = book(1.0)["returns"]["before_financing"]["annualised_pct_on_notional"]
    assert book(L)["returns"]["before_financing"]["annualised_pct_on_notional"] == pytest.approx(one)


def test_costs_that_are_not_priced_are_listed_not_zeroed_silently():
    b = book(1.0, costs=costs(spread_bps=None, slippage_bps=None))
    assert {"spread", "slippage"} <= set(b["unpriced_inputs"])
    assert b["returns"]["before_financing"]["hurdles"][0]["incomplete"] is True
    assert book(1.0)["unpriced_inputs"] == []


def test_custody_on_financed_btc_is_flagged_because_trade_economics_charges_it_on_own_capital_only():
    assert "custody_on_financed_btc" in book(2.0, costs=costs(custody_apr=0.01))["unpriced_inputs"]
    assert "custody_on_financed_btc" not in book(1.0, costs=costs(custody_apr=0.01))["unpriced_inputs"]


def test_opportunity_cost_is_unspecified_and_the_excess_unknown_unless_an_alternative_is_supplied():
    oc = book(1.0)["opportunity_cost"]
    assert oc["alternative_return_apr_pct"] == "UNSPECIFIED" and oc["opportunity_cost_usd"] == "UNKNOWN"
    assert oc["excess_over_alternative_pct_by_financing"] == {"NO_FINANCING": "UNKNOWN"}
    assert book(1.0)["returns"]["after_financing"]["NO_FINANCING"]["excess_over_alternative_pct"] == "UNKNOWN"


def test_opportunity_cost_with_a_supplied_alternative_by_hand():
    b = book(1.0, alternative_return_apr_pct=4.0)
    oc = b["opportunity_cost"]
    assert oc["alternative_return_apr_pct"] == 4.0
    assert oc["opportunity_cost_usd"] == pytest.approx(0.04 * 105_000 * 30 / 365)         # 345.205...
    ann = 469 / 105_000 * 365 / 30 * 100
    assert oc["excess_over_alternative_pct_by_financing"]["NO_FINANCING"] == pytest.approx(ann - 4.0)


def test_excess_over_alternative_stays_unknown_while_financing_is_unknown():
    oc = book(2.0, alternative_return_apr_pct=4.0)["opportunity_cost"]
    assert oc["excess_over_alternative_pct_by_financing"] == {"FINANCING_UNKNOWN": "UNKNOWN"}
    assert oc["opportunity_cost_usd"] == pytest.approx(0.04 * 110_000 * 30 / 365)         # the tied-up capital is known


# ---------------------------------------------------------------------------------------------
# Financing: UNKNOWN unless supplied, never defaulted
# ---------------------------------------------------------------------------------------------
def test_1x_needs_no_financing():
    b = book(1.0)
    assert b["financing"]["status"] == "NOT_REQUIRED" and b["financing"]["financed_notional_usd"] == 0.0
    assert list(b["returns"]["after_financing"]) == ["NO_FINANCING"]


@pytest.mark.parametrize("L", [1.25, 1.5, 2.0, 3.0, 5.0, 10.0])
@pytest.mark.parametrize("scen", [None, {}])
def test_every_level_above_1x_without_a_rate_is_financing_unknown_and_has_no_net_return(L, scen):
    b = book(L, financing_apr_scenarios=scen)
    assert b["financing"]["status"] == "FINANCING_UNKNOWN" and b["financing"]["scenario_rates"] == {}
    assert b["returns"]["after_financing"] == {}
    assert "EXCLUDES" in b["returns"]["before_financing_note"]
    assert b["label"] == "NOT_RECOMMENDABLE"


def test_financing_unknown_is_carried_into_stress_results_and_the_table():
    t = CS.survival_table(inputs())
    assert t["financing"] == {"scenario_rates_supplied": False, "levels_financing_unknown": [1.25, 1.5, 2.0, 3.0, 5.0, 10.0]}
    for b in t["books"]:
        want = "NOT_REQUIRED" if b["leverage"] == 1.0 else "FINANCING_UNKNOWN"
        assert {r["financing_status"] for r in b["stress"]["results"].values()} == {want}
    one = next(b for b in t["books"] if b["leverage"] == 2.0)["stress"]["results"]["funding_reversal_5bps_x9"]
    assert one["financing_accrual_included"] is False


def test_2x_financed_return_by_hand_for_a_supplied_scenario_rate():
    b = book(2.0, financing_apr_scenarios={"ten": 0.10})
    assert b["financing"]["status"] == "SCENARIO_RATES_SUPPLIED"
    v = b["returns"]["after_financing"]["ten"]
    borrow = 100_000 * 0.10 * 30 / 365                              # 821.9178...
    assert v["components_usd"]["borrow_usd"] == pytest.approx(-borrow)
    # funding 200,000 x 0.65 x 90 / 1e4 = 1,170; costs 2 x 116 = 232  ->  938 before, 938 - 821.92 after
    assert b["returns"]["before_financing"]["net_pnl_usd"] == pytest.approx(938.0)
    assert v["net_pnl_usd"] == pytest.approx(938.0 - borrow) and v["net_pnl_usd"] == pytest.approx(116.0822, abs=1e-3)
    assert v["annualised_pct_on_economic_capital"] == pytest.approx((938.0 - borrow) / 110_000 * 365 / 30 * 100)
    assert v["financing_apr"] == 0.10 and v["reconciles"] is True


def test_a_higher_financing_rate_always_lowers_the_net_and_can_make_it_negative():
    b = book(3.0, financing_apr_scenarios=RATES)
    nets = [b["returns"]["after_financing"][k]["net_pnl_usd"] for k in ("low", "base", "high")]
    assert nets[0] > nets[1] > nets[2]
    assert b["returns"]["before_financing"]["net_pnl_usd"] > nets[0]
    loss = book(3.0, financing_apr_scenarios={"x": 0.30})["returns"]["after_financing"]["x"]
    assert loss["net_pnl_usd"] < 0.0 and all(h["meets"] is False for h in loss["hurdles"])


def test_every_supplied_scenario_is_reported_by_name():
    assert set(book(2.0, financing_apr_scenarios=RATES)["returns"]["after_financing"]) == {"low", "base", "high"}


@pytest.mark.parametrize("rate", [8.0, 1.5, -0.01, float("nan"), "0.08", None])
def test_a_financing_rate_that_looks_like_a_percent_or_is_malformed_is_refused(rate):
    with pytest.raises(ValueError):
        inputs(financing_apr_scenarios={"x": rate})


def test_there_is_no_default_financing_rate_anywhere():
    f = CS.CarryInputs.__dataclass_fields__
    assert f["financing_apr_scenarios"].default is None and f["reserve_frac_of_notional"].default is dataclasses.MISSING
    unknown = book(2.0)
    supplied = book(2.0, financing_apr_scenarios={"zero": 0.0})
    assert unknown["returns"]["after_financing"] == {} and "zero" in supplied["returns"]["after_financing"]
    assert supplied["returns"]["after_financing"]["zero"]["net_pnl_usd"] == pytest.approx(unknown["returns"]["before_financing"]["net_pnl_usd"])


def test_stress_accrues_financing_on_the_financed_notional_only_when_a_rate_is_supplied():
    s = S("run", funding_bps_per_print=-10.0, funding_prints=9)       # 3 days
    with_rate = hit(2.0, s, apr=0.12, financing_apr_scenarios={"hi": 0.12})
    assert with_rate["components_usd"]["financing_accrual"] == pytest.approx(-100_000 * 0.12 * 3 / 365)
    assert with_rate["equity_after_usd"] == pytest.approx(90_000 - 1_800 - 100_000 * 0.12 * 3 / 365)
    assert with_rate["financing_accrual_included"] is True
    without = hit(2.0, s)
    assert without["components_usd"]["financing_accrual"] == 0.0 and without["equity_after_usd"] == pytest.approx(88_200.0)
    assert without["financing_accrual_included"] is False and without["financing_status"] == "FINANCING_UNKNOWN"
    assert hit(1.0, s, apr=0.12)["components_usd"]["financing_accrual"] == 0.0           # nothing financed at 1x


def test_the_table_uses_the_highest_supplied_rate_for_stress_accrual():
    t = CS.survival_table(inputs(financing_apr_scenarios=RATES), scenarios=[S("run", funding_bps_per_print=-10.0, funding_prints=9)])
    b2 = next(b for b in t["books"] if b["leverage"] == 2.0)
    assert b2["stress"]["financing_apr_used_for_accrual"] == 0.12
    assert b2["stress"]["results"]["run"]["components_usd"]["financing_accrual"] == pytest.approx(-100_000 * 0.12 * 3 / 365)


# ---------------------------------------------------------------------------------------------
# Venue rules: ASSUMED unless verified, and the label travels
# ---------------------------------------------------------------------------------------------
def all_outputs(vr):
    inp = inputs(venue_rules=vr, financing_apr_scenarios=RATES)
    t = CS.survival_table(inp)
    p = CS.path_risk(inp, history(), horizon_days=10, n_paths=200)
    rec = CS.recommend_exposure(t, edge_lower_bound=0.2, funding_evidence_status="OK")
    b = CS.leverage_book(inp, 2.0)
    sc = CS.evaluate_scenario(b, gap(5.0))
    return b, sc, t, p, rec


def test_default_rules_are_assumed_and_the_label_reaches_every_output():
    b, sc, t, p, rec = all_outputs(None)
    for out in (b, t):
        assert out["venue_rules_status"] == "ASSUMED_NOT_VENUE_VERIFIED"
        assert out["venue_rules"]["status"] == "ASSUMED_NOT_VENUE_VERIFIED"
    assert sc["venue_rules_status"] == p["venue_rules_status"] == rec["venue_rules_status"] == "ASSUMED_NOT_VENUE_VERIFIED"
    assert all(bk["venue_rules_status"] == "ASSUMED_NOT_VENUE_VERIFIED" for bk in t["books"])
    assert CS.venue_rules_status(None) == "ASSUMED_NOT_VENUE_VERIFIED"


def test_verified_rules_with_a_source_are_carried_as_verified_into_every_output():
    b, sc, t, p, rec = all_outputs(rules())
    for out in (b, sc, t, p, rec):
        assert out["venue_rules_status"] == "VENUE_VERIFIED"
    assert b["venue_rules"]["source"] == BORN_SOURCE and b["venue_rules"]["verified"] is True


@pytest.mark.parametrize("vr", [
    rules(verified=False),
    rules(source=""),
    rules(source="   "),
    rules(source=None),
    rules(verified=1),
    rules(verified="yes"),
    dataclasses.replace(CS.assumed_venue_rules(), verified=True, source="looks official"),
    {"btc_collateral_haircut": 0.05, "initial_margin_rate": 0.02, "maintenance_margin_rate": 0.005,
     "liquidation_fee_bps": 50.0, "verified": "true", "source": "s"},
    {"btc_collateral_haircut": 0.05, "initial_margin_rate": 0.02, "maintenance_margin_rate": 0.005,
     "liquidation_fee_bps": 50.0, "source": "s"},
    {"btc_collateral_haircut": 0.05, "initial_margin_rate": 0.02, "maintenance_margin_rate": 0.005,
     "liquidation_fee_bps": 50.0, "verified": True},
])
def test_a_verified_claim_without_proof_is_never_silently_upgraded(vr):
    assert CS.venue_rules_status(vr) == "ASSUMED_NOT_VENUE_VERIFIED"
    assert CS.leverage_book(inputs(venue_rules=vr), 1.0)["venue_rules_status"] == "ASSUMED_NOT_VENUE_VERIFIED"


def test_a_mapping_marked_verified_with_a_source_is_verified():
    d = {"btc_collateral_haircut": 0.05, "initial_margin_rate": 0.02, "maintenance_margin_rate": 0.005,
         "liquidation_fee_bps": 50.0, "verified": True, "source": "owner export"}
    assert CS.venue_rules_status(d) == "VENUE_VERIFIED"
    assert CS.leverage_book(inputs(venue_rules=d), 1.0)["margin"]["rates"]["btc_collateral_haircut"] == 0.05


@pytest.mark.parametrize("bad", [
    dict(btc_collateral_haircut=1.0), dict(btc_collateral_haircut=-0.1), dict(initial_margin_rate=0.0),
    dict(maintenance_margin_rate=0.03), dict(liquidation_fee_bps=-1.0), dict(initial_margin_rate=float("nan"))])
def test_nonsense_venue_rules_are_refused(bad):
    with pytest.raises(ValueError):
        rules(**bad)


def test_a_mapping_with_a_misspelt_key_is_refused_rather_than_silently_assumed():
    with pytest.raises(ValueError):
        CS.coerce_venue_rules({"btc_collateral_haircut": 0.05, "initial_margin_rate": 0.02, "maintenance_margin_rate": 0.005,
                               "liquidation_fee_bps": 50.0, "verifed": True})
    with pytest.raises(ValueError):
        CS.coerce_venue_rules({"btc_collateral_haircut": 0.05})


def test_under_the_assumed_placeholders_10x_cannot_even_open_and_says_so():
    t = CS.survival_table(inputs(venue_rules=None))
    b10 = next(b for b in t["books"] if b["leverage"] == 10.0)
    assert b10["margin"]["opens"] is False
    assert {r["status"] for r in b10["stress"]["results"].values()} == {"NOT_OPENABLE"}
    assert all(r["survives"] is False for r in b10["stress"]["results"].values())


# ---------------------------------------------------------------------------------------------
# Stress scenarios, by hand (zero costs unless the exit cost is the point)
# ---------------------------------------------------------------------------------------------
def test_basis_shock_up_450bps_liquidates_10x_and_not_1x():
    ten, one = hit(10.0, basis(450.0)), hit(1.0, basis(450.0))
    assert ten["equity_after_usd"] == pytest.approx(5_000.0)          # 50,000 - 1,000,000 x 0.045
    assert ten["maintenance_margin_after_usd"] == pytest.approx(5_225.0)      # 0.005 x 1,000,000 x 1.045
    assert ten["liquidated"] is True and ten["status"] == "LIQUIDATED"
    assert one["equity_after_usd"] == pytest.approx(90_500.0) and one["survives"] is True
    assert hit(10.0, basis(400.0))["survives"] is True                 # 10,000 > 5,200


def test_basis_shock_in_both_directions_only_the_perp_rich_side_hurts_the_short():
    up, down = hit(2.0, basis(200.0)), hit(2.0, basis(-200.0))
    assert up["equity_after_usd"] == pytest.approx(86_000.0) and down["equity_after_usd"] == pytest.approx(94_000.0)
    assert up["equity_after_usd"] < 90_000.0 < down["equity_after_usd"]
    assert up["components_usd"]["perp_mark"] == pytest.approx(-4_000.0) and down["components_usd"]["perp_mark"] == pytest.approx(4_000.0)
    assert down["maintenance_margin_after_usd"] == pytest.approx(1_000.0)      # the margin does not shrink on a favourable move


def test_funding_reversal_run_by_hand():
    r = hit(2.0, S("rev", funding_bps_per_print=-10.0, funding_prints=9))
    assert r["components_usd"]["funding_run"] == pytest.approx(-1_800.0)       # 200,000 x -10 x 9 / 1e4
    assert r["equity_after_usd"] == pytest.approx(88_200.0) and r["survives"] is True


def test_a_long_funding_reversal_liquidates_10x_not_1x():
    s = S("rev", funding_bps_per_print=-20.0, funding_prints=24)
    assert hit(10.0, s)["equity_after_usd"] == pytest.approx(2_000.0) and hit(10.0, s)["liquidated"] is True
    assert hit(1.0, s)["equity_after_usd"] == pytest.approx(90_200.0) and hit(1.0, s)["survives"] is True


def test_a_gap_that_cannot_be_rebalanced_liquidates_a_10x_book_that_a_1x_book_survives():
    s = gap(5.0, rebalanced=False)
    one, ten = hit(1.0, s), hit(10.0, s)
    assert one["equity_after_usd"] == pytest.approx(90_000.0) and one["survives"] is True
    assert one["maintenance_margin_after_usd"] == pytest.approx(525.0) and one["margin_buffer_usd"] == pytest.approx(89_475.0)
    assert ten["equity_after_usd"] == pytest.approx(0.0, abs=1e-6)             # 50,000 - 1,000,000 x 0.05
    assert ten["maintenance_margin_after_usd"] == pytest.approx(5_250.0)
    assert ten["liquidated"] is True and ten["survives"] is False
    assert ten["liquidation_cost_usd"] == pytest.approx(50 / 1e4 * 1_000_000 * 1.05)         # 5,250


def test_the_same_gap_with_a_working_hedge_is_survived_by_10x_so_the_stress_is_the_unrebalanced_one():
    ten = hit(10.0, gap(10.0, rebalanced=True))
    assert ten["components_usd"]["perp_mark"] == pytest.approx(-100_000.0) and ten["components_usd"]["collateral_credit_change"] == pytest.approx(95_000.0)
    assert ten["equity_after_usd"] == pytest.approx(45_000.0) and ten["survives"] is True      # -h N g = -5,000
    assert hit(10.0, gap(10.0, rebalanced=False))["liquidated"] is True


def test_gap_down_without_rebalancing_only_the_collateral_loss_is_marked():
    one = hit(1.0, gap(-25.0, rebalanced=False))
    assert one["components_usd"]["collateral_credit_change"] == pytest.approx(-23_750.0)      # 0.95 x 100,000 x -0.25
    assert one["components_usd"]["offsetting_gain_withheld"] == pytest.approx(25_000.0)
    assert one["equity_after_usd"] == pytest.approx(71_250.0)
    assert hit(1.0, gap(-25.0, rebalanced=True))["equity_after_usd"] == pytest.approx(96_250.0)   # + h N |g|
    assert hit(5.0, gap(-25.0, rebalanced=False))["liquidated"] is True                        # 75,000 - 118,750
    assert hit(5.0, gap(-25.0, rebalanced=True))["equity_after_usd"] == pytest.approx(81_250.0)


@pytest.mark.parametrize("L", [1.0, 2.0, 5.0, 10.0])
@pytest.mark.parametrize("pct", [-30.0, -10.0, 10.0, 30.0])
def test_a_gap_without_rebalancing_is_never_better_than_the_same_gap_with_it(L, pct):
    assert hit(L, gap(pct, False))["equity_after_usd"] <= hit(L, gap(pct, True))["equity_after_usd"]


def test_the_gap_breaking_point_of_a_10x_book_sits_between_4_and_4_5_percent():
    assert hit(10.0, gap(4.0, False))["survives"] is True              # 50,000 - 40,000 = 10,000 > 5,200
    assert hit(10.0, gap(4.5, False))["liquidated"] is True            # 5,000 <= 5,225
    assert 4.0 < book(10.0)["headroom"]["liquidation_headroom_pct"] < 4.5


def test_liquidity_deterioration_scales_the_modelled_exit_cost_by_hand():
    c = costs()                                                        # exit 5.5 + 0.2 + 0.3 + 0.1 = 6.1 bps
    assert book(1.0, costs=c)["exit_cost"] == {"base_bps": pytest.approx(6.1), "unpriced": []}
    for mult, usd in ((1.0, 61.0), (3.0, 183.0), (10.0, 610.0)):
        r = hit(1.0, S("x", exit_cost_mult=mult), costs=c)
        assert r["components_usd"]["exit_cost"] == pytest.approx(-usd) and r["equity_after_usd"] == pytest.approx(95_000 - usd)


def test_extreme_illiquidity_liquidates_10x_and_not_1x():
    s = S("x100", exit_cost_mult=100.0)
    assert hit(10.0, s, costs=costs())["liquidated"] is True           # 50,000 - 61,000
    assert hit(1.0, s, costs=costs())["survives"] is True              # 95,000 - 6,100


def test_an_unpriced_exit_cost_is_named_so_a_liquidity_stress_cannot_look_stronger_than_it_is():
    r = hit(1.0, S("x3", exit_cost_mult=3.0), costs=costs(spread_bps=None, slippage_bps=None))
    assert r["exit_cost_unpriced"] == ["spread_bps", "slippage_bps"]
    assert r["components_usd"]["exit_cost"] == pytest.approx(-3 * 5.6 * 100_000 / 1e4)


def correlated():
    return CS.combine("corr", gap(10.0, False), basis(100.0), S("f", funding_bps_per_print=-10.0, funding_prints=9),
                      S("x", exit_cost_mult=3.0))


def test_correlated_scenario_by_hand_at_1x():
    r = hit(1.0, correlated(), costs=costs())
    # Pr = 1.10 x 1.01 = 1.111 -> perp -11,100; funding -900; exit 3 x 6.1 bps x 100,000 = -183
    assert r["components_usd"]["perp_mark"] == pytest.approx(-11_100.0)
    assert r["equity_after_usd"] == pytest.approx(95_000 - 11_100 - 900 - 183)


@pytest.mark.parametrize("L", GRID)
def test_correlated_scenario_is_worse_than_each_component_alone(L):
    parts = [gap(10.0, False), basis(100.0), S("f", funding_bps_per_print=-10.0, funding_prints=9), S("x", exit_cost_mult=3.0)]
    combined = hit(L, correlated(), costs=costs())["equity_after_usd"]
    for p in parts:
        assert combined < hit(L, p, costs=costs())["equity_after_usd"], p.name


def test_correlated_scenario_can_liquidate_a_book_that_survives_every_component():
    parts = [gap(3.0, False), basis(150.0), S("f", funding_bps_per_print=-10.0, funding_prints=9), S("x", exit_cost_mult=50.0)]
    big = CS.combine("big", *parts)
    assert all(hit(10.0, p, costs=costs())["survives"] for p in parts)
    assert hit(10.0, big, costs=costs())["liquidated"] is True


def test_combine_refuses_two_parts_that_move_the_same_dimension_and_a_single_part():
    with pytest.raises(ValueError):
        CS.combine("x", gap(5.0), gap(6.0))
    with pytest.raises(ValueError):
        CS.combine("x", basis(5.0), basis(-5.0))
    with pytest.raises(ValueError):
        CS.combine("x", gap(5.0))
    c = CS.combine("ok", gap(5.0, True), S("x", exit_cost_mult=2.0))
    assert c.gap_pct == 5.0 and c.exit_cost_mult == 2.0 and c.hedge_rebalanced is True
    assert CS.combine("ok2", gap(5.0, True), S("y", basis_bps=10.0)).basis_bps == 10.0


@pytest.mark.parametrize("kw", [dict(gap_pct=-100.0), dict(gap_pct=float("nan")), dict(exit_cost_mult=0.5),
                                dict(funding_prints=-1), dict(funding_prints=2.5), dict(funding_prints=True),
                                dict(basis_bps=-10_000.0), dict(hedge_rebalanced="yes"), dict(name="")])
def test_a_malformed_scenario_is_refused(kw):
    with pytest.raises(ValueError):
        S(**{"name": "s", **kw})


def test_the_reserve_topup_view_differs_from_strict_survival():
    r = hit(10.0, basis(450.0))
    assert r["survives"] is False and r["survives_with_reserve_topup"] is True          # 5,000 + 50,000 reserve > 5,225
    assert hit(10.0, basis(450.0), reserve_frac_of_notional=0.0)["survives_with_reserve_topup"] is False


def test_a_book_that_does_not_open_never_survives_whatever_the_scenario():
    r = hit(10.0, S("calm"), venue_rules=rules(btc_collateral_haircut=0.10))
    assert r["opens"] is False and r["survives"] is False and r["status"] == "NOT_OPENABLE" and r["survives_with_reserve_topup"] is False


def test_evaluating_a_scenario_does_not_mutate_the_book():
    b = book(5.0, financing_apr_scenarios=RATES)
    before = copy.deepcopy(b)
    CS.evaluate_scenario(b, correlated(), financing_apr=0.12)
    assert b == before


# ---------------------------------------------------------------------------------------------
# The survival table
# ---------------------------------------------------------------------------------------------
def test_default_scenarios_cover_every_requested_category_and_are_labelled_illustrative():
    names = [s.name for s in CS.default_stress_scenarios()]
    assert len(set(names)) == len(names)
    for needle in ("basis_up", "basis_down", "funding_reversal", "gap_up", "gap_down", "no_rebalance", "exit_cost_x3", "correlated"):
        assert any(needle in n for n in names), needle
    t = CS.survival_table(inputs())
    assert t["stress_params_status"] == "ILLUSTRATIVE_NOT_CALIBRATED" and t["scenario_names"] == names
    assert CS.survival_table(inputs(), scenarios=[gap(5.0)])["stress_params_status"] == "CALLER_SUPPLIED"
    corr = [s for s in CS.default_stress_scenarios() if s.name.startswith("correlated")]
    assert len(corr) == 2 and all(s.gap_pct and s.basis_bps and s.funding_prints and s.exit_cost_mult == 3.0 for s in corr)


def test_survival_is_monotone_in_leverage_for_every_default_scenario():
    t = CS.survival_table(inputs(venue_rules=rules()))
    for name in t["scenario_names"]:
        flags = [b["stress"]["results"][name]["survives"] for b in t["books"]]
        assert flags == sorted(flags, reverse=True), (name, flags)       # once a level fails, every higher one fails
    assert t["books"][0]["stress"]["survives_all"] is True and t["books"][-1]["stress"]["survives_all"] is False


def test_stress_survival_limit_by_hand_for_the_25_percent_unrebalanced_gap():
    # E0 = C (1 - 0.05 L); equity after = C (1 - 0.30 L); MM = 0.005 L C x 1.25  ->  survives iff L < 3.265
    t = CS.survival_table(inputs(costs=zero_costs()), scenarios=[gap(25.0, False)])
    flags = {b["leverage"]: b["stress"]["survives_all"] for b in t["books"]}
    assert flags == {1.0: True, 1.25: True, 1.5: True, 2.0: True, 3.0: True, 5.0: False, 10.0: False}
    assert t["stress_survival_limit"] == 3.0
    assert "NOT a recommendation" in t["stress_survival_limit_note"] and t["recommendation_ceiling"] == 1.0


def test_stress_survival_limit_is_zero_when_even_1x_is_liquidated():
    t = CS.survival_table(inputs(costs=zero_costs()), scenarios=[gap(150.0, False)])
    assert t["stress_survival_limit"] == 0.0 and t["books"][0]["stress"]["failing"] == ["gap_150.0_False"]


def test_table_validation():
    with pytest.raises(ValueError):
        CS.survival_table(inputs(), scenarios=[])
    with pytest.raises(ValueError):
        CS.survival_table(inputs(), scenarios=[gap(5.0), gap(5.0)])
    with pytest.raises(ValueError):
        CS.survival_table(inputs(), scenarios=["gap"])
    with pytest.raises(ValueError):
        CS.survival_table(inputs(), leverages=[0.5])
    with pytest.raises(ValueError):
        CS.survival_table(inputs(), leverages=[])


# ---------------------------------------------------------------------------------------------
# Path risk: seeded block bootstrap of caller-supplied history
# ---------------------------------------------------------------------------------------------
def risky_inputs(**kw):
    return inputs(costs=zero_costs(), financing_apr_scenarios=RATES, **kw)


def test_no_history_is_insufficient_and_nothing_is_invented():
    for h in (None, {}, {"funding_bps": [0.5] * 300}):
        r = CS.path_risk(risky_inputs(), h, horizon_days=30, n_paths=200)
        assert r["status"] == "INSUFFICIENT" and "books" not in r and r["authorizes_trading"] is False
        assert "missing" in r


def test_a_history_that_is_too_short_is_insufficient_and_says_how_short():
    h = history()
    h["daily_returns"] = h["daily_returns"][:29]
    r = CS.path_risk(risky_inputs(), h, horizon_days=30, n_paths=200)
    assert r["status"] == "INSUFFICIENT" and r["short"] == {"daily_returns": {"have": 29, "need": 30}}
    h2 = history()
    h2["funding_bps"] = h2["funding_bps"][:89]
    assert CS.path_risk(risky_inputs(), h2, horizon_days=30, n_paths=200)["short"]["funding_bps"] == {"have": 89, "need": 90}


def test_joint_resampling_needs_aligned_series_and_otherwise_says_insufficient():
    h = history()
    h["funding_bps"] = h["funding_bps"][:200]
    assert CS.path_risk(risky_inputs(), h, horizon_days=10, n_paths=200, joint=True)["status"] == "INSUFFICIENT"
    assert CS.path_risk(risky_inputs(), history(), horizon_days=10, n_paths=200, joint=True)["status"] == "OK"


@pytest.mark.parametrize("bad", [dict(horizon_days=0), dict(horizon_days=2.5), dict(horizon_days=True), dict(n_paths=199),
                                 dict(n_paths=300.0), dict(seed=1.5), dict(leverages=[0.5]), dict(leverages=[]),
                                 dict(block_days=0), dict(block_days=100), dict(entry_basis_bps=float("nan"))])
def test_path_risk_refuses_malformed_arguments(bad):
    kw = dict(horizon_days=10, n_paths=200)
    kw.update(bad)
    with pytest.raises(ValueError):
        CS.path_risk(risky_inputs(), history(), **kw)


def test_non_finite_or_impossible_history_is_refused_loudly():
    h = history()
    h["daily_returns"][5] = float("nan")
    with pytest.raises(ValueError):
        CS.path_risk(risky_inputs(), h, horizon_days=10, n_paths=200)
    h = history()
    h["daily_returns"][3] = -1.0
    with pytest.raises(ValueError):
        CS.path_risk(risky_inputs(), h, horizon_days=10, n_paths=200)
    h = history()
    h["basis_bps"][0] = "x"
    with pytest.raises(ValueError):
        CS.path_risk(risky_inputs(), h, horizon_days=10, n_paths=200)


def test_path_risk_is_deterministic_for_a_seed_and_differs_for_another_and_ignores_global_random_state():
    h = history()
    random.seed(1)
    a = CS.path_risk(risky_inputs(), h, horizon_days=20, n_paths=300, seed=5)
    random.seed(2)
    b = CS.path_risk(risky_inputs(), h, horizon_days=20, n_paths=300, seed=5)
    c = CS.path_risk(risky_inputs(), h, horizon_days=20, n_paths=300, seed=6)
    assert a == b and a != c
    assert a["seed"] == 5 and a["n_paths"] == 300 and a["horizon_days"] == 20


def test_path_risk_does_not_mutate_its_inputs():
    h, inp = history(), risky_inputs()
    h0, inp0 = copy.deepcopy(h), copy.deepcopy(inp)
    CS.path_risk(inp, h, horizon_days=10, n_paths=200)
    assert h == h0 and inp == inp0


def cases(p):
    return [(b["leverage"], k, c) for b in p["books"] for k, c in b["cases"].items() if c["status"] == "OK"]


def test_cvar_is_never_below_var_and_the_99_tail_is_never_milder_than_the_95_tail():
    p = CS.path_risk(risky_inputs(), history(fund_mean=-0.3), horizon_days=30, n_paths=400)
    cs = cases(p)
    assert len(cs) >= 13 and any(c["cvar_95_loss_pct"] > c["var_95_loss_pct"] for _, _, c in cs)
    for L, k, c in cs:
        assert c["cvar_95_loss_pct"] >= c["var_95_loss_pct"], (L, k)
        assert c["cvar_99_loss_pct"] >= c["var_99_loss_pct"], (L, k)
        assert c["var_99_loss_pct"] >= c["var_95_loss_pct"] and c["cvar_99_loss_pct"] >= c["cvar_95_loss_pct"], (L, k)


def test_the_tail_loss_grows_with_leverage_at_a_fixed_financing_scenario():
    p = CS.path_risk(risky_inputs(), history(fund_mean=-0.1), horizon_days=30, n_paths=400)
    by = {b["leverage"]: b["cases"] for b in p["books"]}
    series = [by[1.0]["NO_FINANCING"]["cvar_95_loss_pct"]] + [by[L]["base"]["cvar_95_loss_pct"] for L in GRID[1:]]
    assert all(a < b for a, b in zip(series, series[1:])), series


def test_a_higher_financing_scenario_means_a_larger_loss_at_the_same_leverage():
    p = CS.path_risk(risky_inputs(), history(), horizon_days=30, n_paths=300)
    c3 = next(b for b in p["books"] if b["leverage"] == 3.0)["cases"]
    assert c3["low"]["cvar_95_loss_pct"] < c3["base"]["cvar_95_loss_pct"] < c3["high"]["cvar_95_loss_pct"]
    assert c3["high"]["excludes_financing"] is False and c3["high"]["financing_apr"] == 0.12


def test_path_results_without_financing_scenarios_are_financing_unknown_and_exclude_financing():
    p = CS.path_risk(inputs(costs=zero_costs()), history(), horizon_days=10, n_paths=200)
    by = {b["leverage"]: b for b in p["books"]}
    assert list(by[1.0]["cases"]) == ["NO_FINANCING"] and by[1.0]["cases"]["NO_FINANCING"]["excludes_financing"] is False
    for L in GRID[1:]:
        assert by[L]["financing_status"] == "FINANCING_UNKNOWN"
        assert list(by[L]["cases"]) == ["FINANCING_UNKNOWN"]
        c = by[L]["cases"]["FINANCING_UNKNOWN"]
        assert c["excludes_financing"] is True and c["financing_apr"] is None


def test_constant_history_gives_a_hand_computable_path():
    # 1x, 30 days, +1 bp x 90 prints on 100,000 = +900; economic capital 105,000
    p = CS.path_risk(inputs(costs=zero_costs(), financing_apr_scenarios={"r10": 0.10}), constant_history(), horizon_days=30, n_paths=200)
    one = next(b for b in p["books"] if b["leverage"] == 1.0)["cases"]["NO_FINANCING"]
    want = -900.0 / 105_000 * 100
    for k in ("var_95_loss_pct", "cvar_95_loss_pct", "var_99_loss_pct", "cvar_99_loss_pct"):
        assert one[k] == pytest.approx(want)
    assert one["mean_pnl_pct_of_economic_capital"] == pytest.approx(900.0 / 105_000 * 100)
    assert one["max_drawdown_pct"] == {"median": 0.0, "p95": 0.0, "worst": 0.0} and one["liquidation_fraction"] == 0.0
    two = next(b for b in p["books"] if b["leverage"] == 2.0)["cases"]["r10"]
    assert two["cvar_95_loss_pct"] == pytest.approx(-(1_800.0 - 100_000 * 0.10 * 30 / 365) / 110_000 * 100)
    assert p["entry_basis_source"] == "HISTORY_MEDIAN" and p["entry_basis_bps"] == 5.0


def test_constant_losing_history_has_a_hand_computable_drawdown():
    p = CS.path_risk(inputs(costs=zero_costs()), constant_history(funding=-1.0), horizon_days=30, n_paths=200)
    one = next(b for b in p["books"] if b["leverage"] == 1.0)["cases"]["NO_FINANCING"]
    assert one["var_95_loss_pct"] == pytest.approx(900.0 / 105_000 * 100)
    assert one["max_drawdown_pct"]["worst"] == pytest.approx(900.0 / 105_000 * 100)       # monotone loss: drawdown == loss


def test_fixed_round_trip_costs_enter_the_path_pnl():
    h = constant_history(funding=0.0)
    free = CS.path_risk(inputs(costs=zero_costs()), h, horizon_days=30, n_paths=200)
    paid = CS.path_risk(inputs(costs=costs()), h, horizon_days=30, n_paths=200)
    f = next(b for b in free["books"] if b["leverage"] == 1.0)["cases"]["NO_FINANCING"]
    c = next(b for b in paid["books"] if b["leverage"] == 1.0)["cases"]["NO_FINANCING"]
    assert f["var_95_loss_pct"] == pytest.approx(0.0, abs=1e-12)
    assert c["var_95_loss_pct"] == pytest.approx(116.0 / 105_000 * 100)                   # 55 + 55 + 2 + 3 + 1


def test_drawdown_statistics_are_ordered_and_bounded():
    p = CS.path_risk(risky_inputs(), history(fund_mean=-0.2), horizon_days=30, n_paths=300)
    for _, _, c in cases(p):
        d = c["max_drawdown_pct"]
        assert 0.0 <= d["median"] <= d["p95"] <= d["worst"] <= 100.0 + 1e-9


def test_a_basis_blowout_history_liquidates_paths_at_10x_but_not_at_1x_or_5x():
    h = {"funding_bps": [0.5] * 300, "basis_bps": [0.0] * 50 + [600.0] * 50, "daily_returns": [0.0] * 100}
    p = CS.path_risk(risky_inputs(), h, horizon_days=30, n_paths=300, entry_basis_bps=0.0)
    by = {b["leverage"]: b["cases"] for b in p["books"]}
    assert by[1.0]["NO_FINANCING"]["liquidation_fraction"] == 0.0 and by[5.0]["base"]["liquidation_fraction"] == 0.0
    ten = by[10.0]["base"]
    assert ten["liquidation_fraction"] > 0.3 and ten["n_liquidated"] == round(ten["liquidation_fraction"] * 300)
    assert ten["var_95_loss_pct"] > 20.0                    # a liquidated path pays the 50 bps fee on a 600 bps basis loss
    assert p["entry_basis_source"] == "CALLER"


def test_path_risk_does_not_open_a_book_the_venue_would_refuse():
    p = CS.path_risk(inputs(costs=zero_costs(), venue_rules=None, financing_apr_scenarios=RATES), history(), horizon_days=10, n_paths=200)
    ten = next(b for b in p["books"] if b["leverage"] == 10.0)
    assert ten["opens"] is False and all(c["status"] == "NOT_OPENABLE" and "var_95_loss_pct" not in c for c in ten["cases"].values())


def test_joint_and_independent_resampling_are_each_deterministic_and_report_their_own_block_structure():
    n = 100
    r, b = [0.0] * n, [0.0] * n
    for i in range(0, n, 10):
        r[i], b[i] = -0.2, 800.0
    h = {"funding_bps": [0.5] * (3 * n), "basis_bps": b, "daily_returns": r}
    kw = dict(horizon_days=10, n_paths=300, entry_basis_bps=0.0, block_days=1)
    joint = CS.path_risk(risky_inputs(), h, joint=True, **kw)
    ind = CS.path_risk(risky_inputs(), h, joint=False, **kw)
    assert joint == CS.path_risk(risky_inputs(), h, joint=True, **kw) and ind == CS.path_risk(risky_inputs(), h, joint=False, **kw)
    assert joint["joint"] is True and joint["history"]["block"] == {"joint_days": 1}
    assert ind["joint"] is False and ind["history"]["block"] == {"funding_prints": 3, "basis_days": 1, "return_days": 1}
    j = next(x for x in joint["books"] if x["leverage"] == 10.0)["cases"]["base"]
    i = next(x for x in ind["books"] if x["leverage"] == 10.0)["cases"]["base"]
    assert j["status"] == i["status"] == "OK" and j["liquidation_fraction"] > 0.0 and i["liquidation_fraction"] > 0.0


def test_joint_blocks_keep_each_day_together_with_its_own_basis_and_funding_prints():
    n = 60
    rets = [float(i) for i in range(n)]
    basis_ = [float(i) for i in range(n)]
    fund = [10.0 * i + k for i in range(n) for k in range(3)]
    rnd = random.Random(4)
    r, b, f = CS._draw_joint(rnd, rets, basis_, fund, 25, 7)
    assert len(r) == len(b) == 25 and len(f) == 75
    for d in range(25):
        assert b[d] == r[d] and f[3 * d:3 * d + 3] == [10.0 * r[d] + k for k in range(3)], d
    assert all(r[d + 1] == r[d] + 1 for d in range(25 - 1) if (d + 1) % 7)          # inside a block the days are consecutive


def test_the_tail_statistics_by_hand():
    losses = [float(i) for i in range(1, 101)]
    assert CS._tail(losses, 0.95) == (95.0, pytest.approx(97.5))           # VaR = the 95th value, CVaR = mean(95..100)
    assert CS._tail(losses, 0.99) == (99.0, pytest.approx(99.5))
    assert CS._tail([3.0], 0.99) == (3.0, 3.0)


def _sim(paths, **kw):
    d = dict(N=100_000.0, E_eco=100_000.0, E0=95_000.0, h=0.05, mm=0.005, liq_bps=50.0, fin_day=0.0, entry_cost=0.0, exit_cost=0.0, H=4)
    d.update(kw)
    return CS._simulate(paths, **d)


def test_simulate_drawdown_is_measured_from_the_running_peak_not_from_the_starting_capital():
    # P&L path 0, +1,000, +2,000, +1,000, 0: the high-water mark is 102,000, so the drawdown at the end is 2,000 / 102,000
    path = ([1.0] * 5, [0.0] * 5, [0.0, 0.01, 0.02, 0.01, 0.0])
    r = _sim([path])
    assert r["max_drawdown_pct"]["worst"] == pytest.approx(2_000 / 102_000 * 100)
    assert r["var_95_loss_pct"] == pytest.approx(0.0, abs=1e-12) and r["liquidation_fraction"] == 0.0


def test_simulate_a_liquidated_path_pays_the_fee_and_stops_accruing():
    # 10x (E0 = 50,000): a +6% basis on day 1 takes margin equity to 50,000 - 1,000,000 x (0.11 - 0.05) = -10,000 -> liquidated
    path = ([1.0, 1.0, 1.0], [0.0, 0.06, 0.0], [0.0, 0.0, 0.5])      # the 0.5 funding on day 2 must never be booked
    r = _sim([path], N=1_000_000.0, E_eco=150_000.0, E0=50_000.0, H=2)
    assert r["n_liquidated"] == 1 and r["liquidation_fraction"] == 1.0
    assert r["var_95_loss_pct"] == pytest.approx((60_000 + 0.005 * 1_000_000 * 1.06) / 150_000 * 100)     # 43.5333...


def test_simulate_charges_entry_at_the_start_exit_at_the_end_and_financing_per_day():
    path = ([1.0] * 3, [0.0] * 3, [0.0] * 3)
    r = _sim([path], H=2, entry_cost=100.0, exit_cost=50.0, fin_day=10.0)
    assert r["var_95_loss_pct"] == pytest.approx((100 + 50 + 20) / 100_000 * 100)
    assert r["max_drawdown_pct"]["worst"] == pytest.approx(170 / 100_000 * 100)


def test_the_history_report_exposes_the_effective_sample_sizes_it_used():
    p = CS.path_risk(risky_inputs(), history(), horizon_days=10, n_paths=200)
    assert p["history"]["n_days"] == 120 and p["history"]["n_funding_prints"] == 360
    assert set(p["history"]["ess"]) == {"funding_bps", "basis_bps", "daily_returns"}
    assert p["history"]["ess"]["daily_returns"] == ES.conservative_ess(history()["daily_returns"])
    assert any("never contained" in s for s in p["limitations"])


def test_unpriced_costs_are_reported_on_the_path_result():
    p = CS.path_risk(inputs(costs=costs(spread_bps=None)), history(), horizon_days=10, n_paths=200)
    assert p["unpriced_costs"] == ["spread"]


# ---------------------------------------------------------------------------------------------
# Kelly: a research input, uncertainty-adjusted
# ---------------------------------------------------------------------------------------------
def test_kelly_fraction_by_hand():
    assert CS.kelly_fraction(0.001, 0.0004) == pytest.approx(2.5)
    assert CS.kelly_fraction(0.0, 0.0004) == 0.0 and CS.kelly_fraction(-0.002, 0.0004) == 0.0


def test_fractional_kelly_scales_the_lower_bound_kelly_linearly():
    full = CS.fractional_kelly(0.0004, 0.0004, 1.0)
    assert full == pytest.approx(1.0)
    assert CS.fractional_kelly(0.0004, 0.0004, 0.5) == pytest.approx(0.5)
    assert CS.fractional_kelly(0.0004, 0.0004, 0.25) == pytest.approx(0.25)


def test_the_lower_bound_not_the_point_estimate_drives_the_fraction():
    point, lower, var = 0.001, 0.0004, 0.0004
    from_lower, from_point = CS.fractional_kelly(lower, var, 0.5), CS.fractional_kelly(point, var, 0.5)
    assert from_lower == pytest.approx(0.5) and from_point == pytest.approx(1.25) and from_lower < from_point


@pytest.mark.parametrize("lb", [0.0, -0.001, None, float("nan"), float("-inf")])
def test_a_non_positive_or_missing_lower_bound_gives_a_zero_fraction_even_if_the_point_mean_is_large(lb):
    assert CS.fractional_kelly(lb, 0.0004, 0.5) == 0.0
    assert CS.kelly_fraction(0.01, 0.0004) > 0.0                      # the point estimate alone would have sized something


@pytest.mark.parametrize("variance", [0.0, -1.0, float("nan"), float("inf"), None])
def test_kelly_refuses_a_non_positive_or_non_finite_variance(variance):
    with pytest.raises(ValueError):
        CS.kelly_fraction(0.001, variance)
    with pytest.raises(ValueError):
        CS.fractional_kelly(0.001, variance, 0.5)


@pytest.mark.parametrize("fraction", [0.0, -0.5, 1.5, float("nan"), None, True])
def test_fractional_kelly_refuses_a_fraction_outside_zero_to_one(fraction):
    with pytest.raises(ValueError):
        CS.fractional_kelly(0.001, 0.0004, fraction)


def test_kelly_research_dict_is_labelled_research_only_with_no_authority():
    r = CS.kelly_research(0.001, 0.0004, 0.0004, 0.5)
    assert r["research_input_only"] is True and r["authorizes_trading"] is False and r["reasons"] == []
    assert r["kelly_full_at_point_mean"] == pytest.approx(2.5) and r["kelly_fractional_at_lower_bound"] == pytest.approx(0.5)
    z = CS.kelly_research(0.001, -0.0001, 0.0004, 0.5)
    assert z["kelly_fractional_at_lower_bound"] == 0.0 and z["reasons"] == ["LOWER_BOUND_NOT_POSITIVE"]


def test_funding_edge_lower_bound_is_below_the_mean_and_feeds_the_evidence_status():
    rnd = random.Random(3)
    xs = [0.8 + rnd.gauss(0, 0.4) for _ in range(240)]
    e = CS.funding_edge_lower_bound(xs, B=500)
    assert e["status"] == "OK" and e["funding_evidence_status"] == "OK" and e["authorizes_trading"] is False
    assert e["mean_lower_bound_bps"] < e["mean_bps"] and e["edge_lower_bound_bps"] == e["mean_lower_bound_bps"]
    assert CS.funding_edge_lower_bound(xs, breakeven_bps_per_settlement=0.5, B=500)["edge_lower_bound_bps"] == pytest.approx(e["mean_lower_bound_bps"] - 0.5)
    assert CS.funding_edge_lower_bound(xs, B=500) == e                       # deterministic


@pytest.mark.parametrize("xs", [[0.8] * 100, [0.8, 0.9, 0.7], []])
def test_funding_edge_lower_bound_is_insufficient_for_a_constant_or_tiny_series(xs):
    e = CS.funding_edge_lower_bound(xs, B=200)
    assert e["status"] == "INSUFFICIENT" and e["funding_evidence_status"] == "INSUFFICIENT" and e["edge_lower_bound_bps"] is None


# ---------------------------------------------------------------------------------------------
# recommend_exposure
# ---------------------------------------------------------------------------------------------
_TABLES = {}
_KINDS = {"good": ({}, None),
          "assumed": (dict(venue_rules=None), None),
          "unverified_fees": (dict(costs=costs(fee_provenance="UNVERIFIED")), None),
          "liquidating": ({}, [gap(150.0, False)]),          # 1x: 95,000 - 150,000 < 0
          "limit_1x": ({}, [gap(85.0, False)])}              # 1x: 9,939 left; 1.25x: 93,750 - 106,250 < 0


def table(kind="good"):
    if kind not in _TABLES:
        kw, scen = _KINDS[kind]
        _TABLES[kind] = CS.survival_table(inputs(**kw), scenarios=scen)
    return copy.deepcopy(_TABLES[kind])


def NE1():
    """N / E of the 1x book: the research ceiling is stated in hedged notional over FULLY ALLOCATED economic capital, so with a
    reserve it is below 1.0 (1 / (1 + reserve fraction)), never above."""
    return next(b for b in table()["books"] if b["leverage"] == 1.0)["capital"]["notional_over_economic_capital"]


def rec(t=None, **kw):
    d = dict(edge_lower_bound=0.2, funding_evidence_status="OK")
    d.update(kw)
    return CS.recommend_exposure(t if t is not None else table(), **d)


def test_all_favourable_inputs_give_the_research_ceiling_of_exactly_1x_and_no_authority():
    r = rec()
    assert r["max_leverage"] == pytest.approx(NE1()) and r["reasons"] == [] and r["zero_exposure"] is False
    assert r["authorizes_trading"] is False and r["levels_above_1x"] == "NOT_RECOMMENDABLE"
    assert r["limiting_factor"] == "HARD_CAP_1X" and r["venue_rules_status"] == "VENUE_VERIFIED"
    assert table()["stress_survival_limit"] == 3.0                     # the book could survive more; the ceiling still stops at 1.0


@pytest.mark.parametrize("edge", [0.0, -0.001, -5.0])
def test_zero_exposure_when_the_lower_bound_edge_is_not_positive(edge):
    r = rec(edge_lower_bound=edge)
    assert r["max_leverage"] == 0.0 and r["label"] == "ZERO_EXPOSURE" and r["zero_exposure"] is True
    assert r["reasons"] == ["EDGE_LOWER_BOUND_NOT_POSITIVE"] and r["authorizes_trading"] is False


@pytest.mark.parametrize("edge", [None, float("nan"), "0.5"])
def test_zero_exposure_when_the_lower_bound_edge_is_not_supplied(edge):
    r = rec(edge_lower_bound=edge)
    assert r["max_leverage"] == 0.0 and r["reasons"] == ["EDGE_LOWER_BOUND_UNSUPPLIED"]


@pytest.mark.parametrize("status", ["INSUFFICIENT", "NO_VARIANCE", None, "ok", ""])
def test_zero_exposure_when_the_funding_evidence_is_insufficient(status):
    r = rec(funding_evidence_status=status)
    assert r["max_leverage"] == 0.0 and r["reasons"] == ["FUNDING_EVIDENCE_INSUFFICIENT"]


def test_zero_exposure_when_a_stress_scenario_liquidates_the_1x_book():
    t = table("liquidating")
    assert t["stress_survival_limit"] == 0.0
    r = rec(t)
    assert r["max_leverage"] == 0.0 and r["reasons"] == ["STRESS_SCENARIO_LIQUIDATES"]
    assert r["stress_failures_at_1x"] == ["gap_150.0_False"]


def test_zero_exposure_when_the_1x_book_was_never_stress_tested():
    t = CS.survival_table(inputs(), leverages=[2.0, 3.0])
    r = rec(t)
    assert r["max_leverage"] == 0.0 and "STRESS_NOT_EVALUATED_AT_1X" in r["reasons"]
    t = table()
    for b in t["books"]:
        b["stress"]["results"] = {}
    assert "STRESS_NOT_EVALUATED_AT_1X" in rec(t)["reasons"] and rec(t)["max_leverage"] == 0.0


def test_stress_that_only_breaks_higher_levels_does_not_zero_the_1x_ceiling():
    t = table()
    assert t["books"][-1]["stress"]["survives_all"] is False and t["books"][0]["stress"]["survives_all"] is True
    assert rec(t)["max_leverage"] == pytest.approx(NE1())


def test_zero_exposure_when_the_venue_rules_are_only_assumed():
    r = rec(table("assumed"))
    assert r["max_leverage"] == 0.0 and r["reasons"] == ["VENUE_RULES_ASSUMED_NOT_VERIFIED"]
    assert r["venue_rules_status"] == "ASSUMED_NOT_VENUE_VERIFIED"


def test_zero_exposure_when_financing_is_unknown():
    r = rec(financing_status="FINANCING_UNKNOWN")
    assert r["max_leverage"] == 0.0 and r["reasons"] == ["FINANCING_UNKNOWN"] and r["financing_status"] == "FINANCING_UNKNOWN"
    assert rec(financing_status="SCENARIO_RATES_SUPPLIED")["max_leverage"] == pytest.approx(NE1())
    assert rec(financing_status="NOT_REQUIRED")["max_leverage"] == pytest.approx(NE1())


def test_zero_exposure_when_the_fee_tier_is_unverified():
    r = rec(table("unverified_fees"))
    assert r["max_leverage"] == 0.0 and r["reasons"] == ["FEE_TIER_UNVERIFIED"]


def test_zero_exposure_when_a_supplied_kelly_fraction_is_zero():
    for k in (0.0, -1.0, float("nan")):
        r = rec(kelly_fractional=k)
        assert r["max_leverage"] == 0.0 and r["reasons"] == ["KELLY_FRACTION_ZERO"]


def test_every_reason_at_once_lists_every_reason():
    t = CS.survival_table(inputs(venue_rules=None, costs=costs(fee_provenance="UNVERIFIED")), scenarios=[gap(150.0, False)])
    r = rec(t, edge_lower_bound=-1.0, funding_evidence_status="INSUFFICIENT", financing_status="FINANCING_UNKNOWN", kelly_fractional=0.0)
    assert set(r["reasons"]) == {"EDGE_LOWER_BOUND_NOT_POSITIVE", "FUNDING_EVIDENCE_INSUFFICIENT", "STRESS_SCENARIO_LIQUIDATES",
                                 "VENUE_RULES_ASSUMED_NOT_VERIFIED", "FINANCING_UNKNOWN", "FEE_TIER_UNVERIFIED", "KELLY_FRACTION_ZERO"}
    assert r["max_leverage"] == 0.0


def test_all_inputs_favourable_but_venue_rules_only_assumed_is_still_zero_end_to_end():
    rnd = random.Random(21)
    xs = [1.2 + rnd.gauss(0, 0.3) for _ in range(300)]
    e = CS.funding_edge_lower_bound(xs, breakeven_bps_per_settlement=0.2, B=500)
    assert e["edge_lower_bound_bps"] > 0.0 and e["funding_evidence_status"] == "OK"
    k = CS.fractional_kelly(e["edge_lower_bound_bps"] / 1e4, 1e-8, 0.5)
    assert k > 1.0                                                   # a huge research number
    t = CS.survival_table(inputs(venue_rules=None))
    assert t["books"][0]["stress"]["survives_all"] is True and t["fee_provenance"] == "ACCOUNT_VERIFIED"
    r = CS.recommend_exposure(t, edge_lower_bound=e["edge_lower_bound_bps"], funding_evidence_status=e["funding_evidence_status"],
                              kelly_fractional=k, financing_status="NOT_REQUIRED")
    assert r["max_leverage"] == 0.0 and r["reasons"] == ["VENUE_RULES_ASSUMED_NOT_VERIFIED"] and r["label"] == "ZERO_EXPOSURE"
    verified = CS.recommend_exposure(CS.survival_table(inputs()), edge_lower_bound=e["edge_lower_bound_bps"],
                                     funding_evidence_status="OK", kelly_fractional=k, financing_status="NOT_REQUIRED")
    assert verified["max_leverage"] == pytest.approx(NE1())                           # same inputs, only the rules differ


def test_a_table_labelled_assumed_stays_assumed_even_if_the_stored_rules_claim_otherwise():
    t = table()
    t["venue_rules_status"] = "ASSUMED_NOT_VENUE_VERIFIED"
    r = rec(t)
    assert r["max_leverage"] == 0.0 and r["reasons"] == ["VENUE_RULES_ASSUMED_NOT_VERIFIED"]


def test_a_hand_edited_venue_label_cannot_lift_an_assumed_table():
    t = table("assumed")
    t["venue_rules_status"] = "VENUE_VERIFIED"
    assert "VENUE_RULES_ASSUMED_NOT_VERIFIED" in rec(t)["reasons"]
    t["venue_rules"].update(status="VENUE_VERIFIED", verified=True, source="trust me")
    r = rec(t)
    assert r["max_leverage"] == 0.0 and "VENUE_RULES_ASSUMED_NOT_VERIFIED" in r["reasons"]
    t2 = table()
    t2["venue_rules"]["source"] = ""
    assert rec(t2)["max_leverage"] == 0.0


@pytest.mark.parametrize("junk", [{}, {"books": []}, {"books": [{"leverage": 1.0}]}, {"venue_rules_status": "VENUE_VERIFIED"}])
def test_a_malformed_table_fails_closed(junk):
    r = rec(junk)
    assert r["max_leverage"] == 0.0 and r["authorizes_trading"] is False and r["reasons"]


@pytest.mark.parametrize("junk", [None, [], "table", 5])
def test_something_that_is_not_a_table_fails_closed(junk):
    r = CS.recommend_exposure(junk, edge_lower_bound=1.0, funding_evidence_status="OK", kelly_fractional=5.0)
    assert r["max_leverage"] == 0.0 and r["authorizes_trading"] is False and "STRESS_NOT_EVALUATED_AT_1X" in r["reasons"]


def test_a_table_whose_survival_limit_was_removed_or_garbled_fails_closed():
    for bad in ("missing", None, "3.0", float("nan")):
        t = table()
        if bad == "missing":
            del t["stress_survival_limit"]
        else:
            t["stress_survival_limit"] = bad
        r = rec(t)
        assert r["max_leverage"] == 0.0 and r["reasons"] == ["STRESS_SURVIVAL_LIMIT_ZERO"], bad


def test_kelly_never_lifts_the_ceiling_above_1x():
    for k in (1.0, 1.0001, 5.0, 1e6, float("inf")):
        r = rec(kelly_fractional=k)
        assert r["max_leverage"] <= 1.0
    r = rec(kelly_fractional=50.0)
    assert r["max_leverage"] == pytest.approx(NE1()) and r["limiting_factor"] == "HARD_CAP_1X" and r["limits"]["kelly_fractional"] == 50.0


def test_a_smaller_kelly_fraction_lowers_the_ceiling_and_is_named_as_the_limit():
    r = rec(kelly_fractional=0.3)
    assert r["max_leverage"] == pytest.approx(0.3) and r["limiting_factor"] == "KELLY" and r["reasons"] == []


def test_kelly_never_lifts_the_ceiling_above_the_stress_survival_limit():
    t = table("liquidating")                                           # limit 0.0: Kelly cannot buy any exposure
    assert rec(t, kelly_fractional=50.0)["max_leverage"] == 0.0
    t = table("limit_1x")                                              # 1x survives, 1.25x does not
    assert t["stress_survival_limit"] == 1.0
    assert rec(t, kelly_fractional=50.0)["max_leverage"] == pytest.approx(NE1())
    capped = table()
    capped["stress_survival_limit"] = 0.5                              # hand-set to test the min(): the ceiling follows it
    r = rec(capped, kelly_fractional=50.0)
    assert r["max_leverage"] == pytest.approx(0.5 * NE1()) and r["limiting_factor"] == "STRESS_SURVIVAL_LIMIT"


def test_a_zero_survival_limit_with_no_stress_reason_is_still_zero_exposure():
    t = table()
    t["stress_survival_limit"] = 0.0
    r = rec(t)
    assert r["max_leverage"] == 0.0 and r["reasons"] == ["STRESS_SURVIVAL_LIMIT_ZERO"]


def test_the_ceiling_is_never_above_1x_whatever_the_combination_and_zero_whenever_any_reason_fires():
    tables = [table("good"), table("assumed"), table("unverified_fees"), table("liquidating"), table("limit_1x")]
    n = 0
    for t, edge, ev, k, fin in itertools.product(tables, (-1.0, 0.0, 5.0, 1e9), ("OK", "INSUFFICIENT"),
                                                 (None, 0.0, 0.3, 50.0, 1e12), (None, "FINANCING_UNKNOWN")):
        r = rec(t, edge_lower_bound=edge, funding_evidence_status=ev, kelly_fractional=k, financing_status=fin)
        n += 1
        assert 0.0 <= r["max_leverage"] <= CS.MAX_RECOMMENDED_LEVERAGE == 1.0
        assert r["authorizes_trading"] is False
        assert (r["max_leverage"] == 0.0) == (r["reasons"] != [] or r["limits"]["stress_survival_limit"] <= 0.0)
        if r["max_leverage"]:
            assert r["max_leverage"] <= min(1.0, r["limits"]["stress_survival_limit"], k if k is not None else 1.0)
    assert n == 5 * 4 * 2 * 5 * 2


def test_evidence_of_a_great_edge_does_not_scale_the_ceiling_there_is_no_automatic_scaling():
    base = rec(edge_lower_bound=0.01, kelly_fractional=None)["max_leverage"]
    assert rec(edge_lower_bound=1e12, kelly_fractional=1e12)["max_leverage"] == base == pytest.approx(NE1())
    banned = re.compile(r"scale|scaling|auto|size|sizing|order|trade|execute|submit|place|ramp|escalat|promote", re.I)
    public = [n for n in dir(CS) if not n.startswith("_") and callable(getattr(CS, n))
              and getattr(getattr(CS, n), "__module__", None) == "capital_survival"]
    assert public and not [n for n in public if banned.search(n)], public


def test_levels_above_1x_are_labelled_not_recommendable_and_1x_is_not():
    t = CS.survival_table(inputs())
    labels = {b["leverage"]: b["label"] for b in t["books"]}
    assert labels[1.0] == "WITHIN_1X_RESEARCH_CEILING"
    assert all(v == "NOT_RECOMMENDABLE" for k, v in labels.items() if k > 1.0)
    assert rec()["levels_above_1x"] == "NOT_RECOMMENDABLE"


# ---------------------------------------------------------------------------------------------
# No trading authority anywhere; outputs are plain data
# ---------------------------------------------------------------------------------------------
def every_output():
    inp = inputs(financing_apr_scenarios=RATES, alternative_return_apr_pct=4.0)
    t = CS.survival_table(inp)
    b = CS.leverage_book(inp, 3.0)
    sc = CS.evaluate_scenario(b, correlated())
    p = CS.path_risk(inp, history(), horizon_days=10, n_paths=200)
    ins = CS.path_risk(inp, None, horizon_days=10, n_paths=200)
    e = CS.funding_edge_lower_bound([0.5 + 0.1 * math.sin(i) for i in range(100)], B=200)
    return {"table": t, "book": b, "scenario": sc, "path": p, "insufficient": ins, "kelly": CS.kelly_research(0.001, 0.0004, 0.0004, 0.5),
            "edge": e, "rec": rec(t), "rec_zero": rec(t, edge_lower_bound=-1.0)}


def walk(x, path=""):
    if isinstance(x, dict):
        for k, v in x.items():
            yield path + "/" + str(k), k, v
            yield from walk(v, path + "/" + str(k))
    elif isinstance(x, (list, tuple)):
        for i, v in enumerate(x):
            yield from walk(v, f"{path}[{i}]")


def test_no_code_path_returns_trading_authority():
    pat = re.compile(r"authori[sz]|approv|permit|may_trade|enable_trading|live", re.I)
    seen = 0
    for name, out in every_output().items():
        assert out["authorizes_trading"] is False, name
        for where, key, val in walk(out):
            if key == "authorizes_trading":
                assert val is False, where
                seen += 1
            elif pat.search(str(key)):
                assert val is not True, where
    assert seen > 60


def test_outputs_are_plain_finite_json_data():
    for name, out in every_output().items():
        text = json.dumps(out, allow_nan=False, sort_keys=True)
        assert json.loads(text) == json.loads(json.dumps(out, sort_keys=True)), name


def test_the_ceiling_constant_and_the_version_cap_cannot_be_exceeded_by_a_caller_argument():
    import inspect
    assert CS.MAX_RECOMMENDED_LEVERAGE == 1.0
    params = inspect.signature(CS.recommend_exposure).parameters
    assert not [p for p in params if re.search(r"cap|max|ceiling|limit|leverage", p, re.I)], list(params)


# ---------------------------------------------------------------------------------------------
# Structure: pure stdlib + trade_economics + evidence_stats, no side effects
# ---------------------------------------------------------------------------------------------
def tree():
    with open(SRC, encoding="utf-8") as fh:
        return ast.parse(fh.read())


def imported_roots():
    out = set()
    for n in ast.walk(tree()):
        if isinstance(n, ast.Import):
            out |= {a.name.split(".")[0] for a in n.names}
        elif isinstance(n, ast.ImportFrom):
            out.add((n.module or "").split(".")[0])
    return out


def test_the_module_imports_only_the_standard_library_trade_economics_and_evidence_stats():
    got = imported_roots()
    assert got <= {"__future__", "math", "random", "dataclasses", "typing", "evidence_stats", "trade_economics"}, got
    repo = {f[:-3] for f in os.listdir(BOT) if f.endswith(".py")}
    assert (got & repo) == {"evidence_stats", "trade_economics"}
    assert all(m in sys.stdlib_module_names for m in got - {"evidence_stats", "trade_economics"})


def test_nothing_that_can_reach_an_order_a_broker_a_store_a_clock_a_file_or_the_network_is_imported():
    forbidden = {"time", "datetime", "os", "sys", "io", "pathlib", "socket", "ssl", "http", "urllib", "requests", "subprocess",
                 "sqlite3", "threading", "asyncio", "shutil", "tempfile", "secrets", "json", "pickle", "logging", "config",
                 "venues", "carry_broker", "trading_engine", "persistence", "bybit_connection", "main", "ledger", "market_data"}
    assert not (imported_roots() & forbidden)
    names = {n.id for n in ast.walk(tree()) if isinstance(n, ast.Name)} | {n.attr for n in ast.walk(tree()) if isinstance(n, ast.Attribute)}
    assert not (names & {"getenv", "environ", "now", "utcnow", "today", "monotonic", "time_ns", "urlopen", "connect", "system", "popen"})


def test_only_a_seeded_random_generator_is_ever_used_and_no_io_builtin_is_called():
    t = tree()
    for n in ast.walk(t):
        if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id == "random":
            assert n.attr == "Random", n.attr                          # never random.random(), random.seed(), random.shuffle()...
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name):
            assert n.func.id not in {"open", "print", "input", "exec", "eval", "compile", "__import__", "exit", "quit", "breakpoint"}
    rng_calls = [n for n in ast.walk(t) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "Random"]
    assert rng_calls and all(len(c.args) == 1 for c in rng_calls)       # always constructed from an explicit seed


def test_the_module_has_no_import_time_statements_beyond_definitions_and_constants():
    body = tree().body
    for i, n in enumerate(body):
        if i == 0:
            assert isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant) and isinstance(n.value.value, str)     # docstring
        else:
            assert isinstance(n, (ast.Import, ast.ImportFrom, ast.FunctionDef, ast.ClassDef, ast.Assign, ast.AnnAssign)), ast.dump(n)[:80]


def test_importing_it_loads_only_trade_economics_and_evidence_stats_from_the_repo_and_prints_nothing():
    code = ("import sys, json\n"
            f"sys.path.insert(0, {BOT!r})\n"
            "import capital_survival\n"
            "mods = sorted(m for m, v in list(sys.modules.items()) "
            f"if getattr(v, '__file__', None) and v.__file__.startswith({BOT!r}) and '/tests/' not in v.__file__)\n"
            "print(json.dumps(mods))\n")
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60, cwd=HERE)
    assert done.returncode == 0, done.stderr
    assert done.stderr == "" and json.loads(done.stdout) == ["capital_survival", "evidence_stats", "trade_economics"]


def test_module_level_constants_are_the_documented_ones():
    assert CS.ASSUMED_VENUE_RULES == "ASSUMED_NOT_VENUE_VERIFIED" and CS.FINANCING_UNKNOWN == "FINANCING_UNKNOWN"
    assert CS.NOT_RECOMMENDABLE == "NOT_RECOMMENDABLE" and CS.INSUFFICIENT == "INSUFFICIENT"
    assert CS.UNSPECIFIED == TE.UNSPECIFIED == "UNSPECIFIED"
    assert CS.assumed_venue_rules().verified is False and CS.assumed_venue_rules().source == ""
    doc = " ".join(CS.__doc__.split())
    assert "hedged notional / fully allocated economic capital" in doc and "FINANCING" in doc and "ASSUMED_NOT_VENUE_VERIFIED" in doc


def test_the_ceiling_never_permits_more_notional_than_the_one_x_book_holds_per_unit_of_economic_capital():
    """The third review: with a 10 % reserve the old cap of 1.0 permitted N / E = 1.0, i.e. N / C = 1.11 and financed notional. The cap is
    now the 1x book's own N / E (0.909 here), and a larger reserve lowers it further."""
    for rho in (0.0, 0.1, 0.25, 0.5):
        t = CS.survival_table(inputs(reserve_frac_of_notional=rho), scenarios=[CS.StressScenario("calm")])
        r = CS.recommend_exposure(t, edge_lower_bound=0.2, funding_evidence_status="OK", financing_status="NOT_REQUIRED") \
            if False else CS.recommend_exposure(dict(t, venue_rules_status=CS.VERIFIED_VENUE_RULES, fee_provenance=CS.FEES_VERIFIED),
                                                edge_lower_bound=0.2, funding_evidence_status="OK")
        ne1 = next(b for b in t["books"] if b["leverage"] == 1.0)["capital"]["notional_over_economic_capital"]
        assert ne1 == pytest.approx(1.0 / (1.0 + rho))
        assert r["max_leverage"] <= ne1 + 1e-12, (rho, r)
        assert r["limits"]["hard_cap"] == pytest.approx(ne1)
