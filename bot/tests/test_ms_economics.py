"""ms_economics: breakeven with provenance, base vs pessimistic, maker-first incl. no-fill, stress set, leverage ceiling. Offline, pure."""
import inspect
import math
import os
import sys

import pytest

BOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BOT)
sys.path.insert(0, os.path.join(BOT, "tools"))

import carry_costs as CC  # noqa: E402
import ms_economics as E  # noqa: E402
from ms_economics import Inp  # noqa: E402


def inputs(**kw):
    fi = E.fee_inputs("overlay_owned_spot", "taker")
    d = dict(position_class="overlay_owned_spot", style="taker", hold_days=Inp(10.0, "DERIVED"), funding_bps_per_print=Inp(2.0, "MEASURED"), funding_interval_min=480,
             taker_fee_round_trip_bps=fi["fee_round_trip_bps"], spread_round_trip_bps=Inp(0.5, "MEASURED"), slippage_round_trip_bps=Inp(1.0, "MEASURED"),
             hedge_cost_bps=Inp(0.0, "DERIVED"), financing_apr=Inp(0.0, "DERIVED"), opportunity_cost_apr=Inp(0.0, "DERIVED"), op_failure_prob=Inp(0.01, "MODELLED"),
             op_failure_loss_bps=Inp(50.0, "MODELLED"), liquidation_prob=Inp(0.001, "MODELLED"), liquidation_loss_bps=Inp(500.0, "MODELLED"))
    d.update(kw)
    return E.EconInputs(**d)


def maker_inputs(**kw):
    m = E.fee_inputs("overlay_owned_spot", "maker")
    d = dict(style="maker", maker_fee_round_trip_bps=m["fee_round_trip_bps"], adverse_selection_bps=Inp(1.0, "MODELLED"), p_fill_per_leg=Inp(0.8, "MODELLED"), n_legs=1,
             cancel_cost_bps=Inp(0.0, "MODELLED"), missed_edge_bps=Inp(5.0, "MODELLED"))
    d.update(kw)
    return inputs(**d)


# ---------------------------------------------------------------- fee provenance
def test_fee_inputs_come_from_carry_costs_and_are_venue_stated():
    t = E.fee_inputs("overlay_owned_spot", "taker")
    assert t["fee_round_trip_bps"] == Inp(CC.ROUND_TRIP_FEE_BPS_PERP_OVERLAY, "VENUE_STATED") and t["spread_round_trip_bps"] == Inp(CC.ASSUMED_ROUND_TRIP_SPREAD_BPS, "ASSUMED")
    a = E.fee_inputs("acquire_spot_perp", "taker")
    assert a["fee_round_trip_bps"].value == CC.ROUND_TRIP_FEE_BPS_SPOT_PERP
    m = E.fee_inputs("overlay_owned_spot", "maker")
    assert m["fee_round_trip_bps"] == Inp(CC.ROUND_TRIP_FEE_BPS_PERP_OVERLAY_MAKER_MODELLED, "MODELLED")
    assert E.fee_inputs("acquire_spot_perp", "maker")["fee_round_trip_bps"].known is False            # no spot maker figure: unknown, not invented


def test_measured_fee_overrides_and_rebate_is_refused():
    assert E.fee_inputs("overlay_owned_spot", "taker", measured_round_trip_fee_bps=9.0)["fee_round_trip_bps"] == Inp(9.0, "MEASURED")
    for bad in (-0.1, float("nan"), True, "9"):
        with pytest.raises(ValueError):
            E.fee_inputs("overlay_owned_spot", "taker", measured_round_trip_fee_bps=bad)


# ---------------------------------------------------------------- breakeven
def test_positive_control_pass_and_the_arithmetic():
    r = E.assess(inputs())
    b = r["base"]
    assert r["verdict"] == "PASS" and r["prov"] == "MODELLED"
    assert b["components"]["entry_fee"] == pytest.approx(5.5) and b["components"]["exit_fee"] == pytest.approx(5.5)
    assert b["one_time_cost_bps"] == pytest.approx(11.0 + 0.5 + 1.0 + 0.5 + 0.5)
    assert b["funding_prints_in_hold"] == 30 and b["funding_bps_per_day"] == pytest.approx(6.0)
    assert b["expected_funding_bps_horizon"] == pytest.approx(60.0) and b["expected_net_bps"] == pytest.approx(60.0 - 13.5)
    assert b["breakeven_gross_edge_bps_per_interval"] == pytest.approx(13.5 / 30)
    assert b["breakeven_gross_edge_bps_per_day"] == pytest.approx(1.35)


def test_base_and_pessimistic_side_by_side_and_pessimistic_is_worse():
    r = E.assess(inputs())
    b, p = r["base"], r["pessimistic"]
    assert p["one_time_cost_bps"] > b["one_time_cost_bps"] and p["expected_net_bps"] < b["expected_net_bps"] and r["scenario_prov"] == "ASSUMED"
    assert p["breakeven_gross_edge_bps_per_interval"] > b["breakeven_gross_edge_bps_per_interval"]


def test_pessimistic_failure_fails_the_verdict_even_when_base_passes():
    r = E.assess(inputs(funding_bps_per_print=Inp(0.7, "MEASURED")))              # base 21 - 13.5 > 0 ; pessimistic 10.5 - ~21 < 0
    assert r["base"]["expected_net_bps"] > 0 and r["pessimistic"]["expected_net_bps"] < 0 and r["verdict"] == "FAIL"
    assert "PESSIMISTIC_NET_NOT_POSITIVE" in r["reasons"]


def test_base_failure_is_fail():
    assert E.assess(inputs(funding_bps_per_print=Inp(0.1, "MEASURED")))["verdict"] == "FAIL"
    assert E.assess(inputs(funding_bps_per_print=Inp(-1.0, "MEASURED")))["verdict"] == "FAIL"


def test_assumed_spread_default_can_never_pass():
    i = inputs(spread_round_trip_bps=E.fee_inputs("overlay_owned_spot", "taker")["spread_round_trip_bps"])
    r = E.assess(i)
    assert r["verdict"] == "NOT_PROVEN" and r["prov"] == "ASSUMED" and any(x.startswith("WEAKEST_PROVENANCE") for x in r["reasons"])


@pytest.mark.parametrize("name", ["hold_days", "funding_bps_per_print", "taker_fee_round_trip_bps", "spread_round_trip_bps", "slippage_round_trip_bps", "hedge_cost_bps",
                                  "financing_apr", "opportunity_cost_apr", "op_failure_prob", "op_failure_loss_bps", "liquidation_prob", "liquidation_loss_bps"])
def test_unknown_input_gives_unknown_verdict_and_no_number(name):
    r = E.assess(inputs(**{name: E.UNKNOWN}))
    assert r["verdict"] == "UNKNOWN" and r["base"]["expected_net_bps"] is None
    assert r["base"]["unknown"]


def test_zero_cost_execution_is_never_a_pass():
    zero = inputs(taker_fee_round_trip_bps=Inp(0.0, "ASSUMED"), spread_round_trip_bps=Inp(0.0, "ASSUMED"), slippage_round_trip_bps=Inp(0.0, "ASSUMED"),
                  op_failure_prob=Inp(0.0, "ASSUMED"), liquidation_prob=Inp(0.0, "ASSUMED"))
    r = E.assess(zero)
    assert r["verdict"] != "PASS" and "ZERO_FEE_NOT_MEASURED" in r["reasons"]
    zero_stated = inputs(taker_fee_round_trip_bps=Inp(0.0, "VENUE_STATED"))
    assert E.assess(zero_stated)["verdict"] != "PASS"
    assert E.assess(inputs(taker_fee_round_trip_bps=Inp(0.0, "MEASURED")))["verdict"] == "PASS"       # a MEASURED zero tier is a fact, allowed


def test_wrong_interval_conversion_is_unknown_and_equivalent_intervals_agree():
    assert E.assess(inputs(funding_interval_min=7))["verdict"] == "UNKNOWN"
    assert E.assess(inputs(funding_interval_min=None))["verdict"] == "UNKNOWN"
    assert E.assess(inputs(funding_interval_min=True))["verdict"] == "UNKNOWN"
    a = E.assess(inputs(funding_bps_per_print=Inp(2.0, "MEASURED"), funding_interval_min=480))["base"]
    b = E.assess(inputs(funding_bps_per_print=Inp(1.0, "MEASURED"), funding_interval_min=240))["base"]
    c = E.assess(inputs(funding_bps_per_print=Inp(0.25, "MEASURED"), funding_interval_min=60))["base"]
    assert a["expected_net_bps"] == pytest.approx(b["expected_net_bps"]) == pytest.approx(c["expected_net_bps"])
    assert a["funding_prints_in_hold"] == 30 and b["funding_prints_in_hold"] == 60 and c["funding_prints_in_hold"] == 240
    assert b["breakeven_gross_edge_bps_per_interval"] == pytest.approx(a["breakeven_gross_edge_bps_per_interval"] / 2)       # per-print halves with the interval
    naive_wrong = 2.0 * 3 * 10                                                                                          # an 8h assumption applied to a 60m symbol
    assert E.assess(inputs(funding_bps_per_print=Inp(2.0, "MEASURED"), funding_interval_min=60))["base"]["expected_funding_bps_horizon"] != pytest.approx(naive_wrong)


def test_breakeven_hold_days_whole_prints_and_never():
    r = E.assess(inputs())["base"]
    assert r["breakeven_hold_days"] == pytest.approx(math.ceil(13.5 / 2.0) / 3.0)           # 7 prints at 3/day
    r2 = E.assess(inputs(funding_bps_per_print=Inp(0.1, "MEASURED"), financing_apr=Inp(0.5, "ASSUMED")))["base"]
    assert r2["breakeven_hold_days"] is None                                                # financing exceeds funding: never


def test_financing_is_charged_per_day_from_carry_costs_convention():
    r = E.assess(inputs(financing_apr=Inp(0.365, "MEASURED"), opportunity_cost_apr=Inp(0.0, "DERIVED")))["base"]
    assert r["financing_bps_per_day"] == pytest.approx(10.0) and r["financing_bps_horizon"] == pytest.approx(100.0)
    assert r["expected_net_bps"] == pytest.approx(60.0 - 13.5 - 100.0)


def test_breakeven_table_over_holding_periods():
    t = E.breakeven_table(inputs(), (1, 3, 10))
    assert [r["funding_prints_in_hold"] for r in t] == [3, 9, 30]
    assert t[0]["breakeven_gross_edge_bps_per_interval"] == pytest.approx(13.5 / 3) and t[2]["breakeven_gross_edge_bps_per_interval"] == pytest.approx(13.5 / 30)
    assert E.breakeven_table(inputs(), (0.1,))[0]["breakeven_gross_edge_bps_per_interval"] is None        # no print inside the hold: undefined, not zero


def test_unhedged_class_is_never_a_pass():
    r = E.assess(inputs(position_class="perp_only_unhedged"))
    assert r["verdict"] == "NOT_PROVEN" and "NOT_A_CARRY:unhedged_price_leg" in r["reasons"]


def test_invalid_style_or_class_raises_and_inp_provenance_is_checked():
    with pytest.raises(ValueError):
        E.assess(inputs(style="market"))
    with pytest.raises(ValueError):
        Inp(1.0, "GUESS")
    assert Inp(float("nan"), "MEASURED").known is False and Inp(True, "MEASURED").known is False


# ---------------------------------------------------------------- maker-first including no-fill
def test_maker_first_expected_cost_includes_the_chance_of_no_fill():
    s = E.maker_first_expected_cost(maker_side_fee_bps=Inp(2.0, "MODELLED"), taker_side_fee_bps=Inp(5.5, "VENUE_STATED"), spread_side_bps=Inp(0.25, "MEASURED"),
                                    slippage_side_bps=Inp(0.5, "MEASURED"), adverse_selection_side_bps=Inp(0.5, "MODELLED"), p_fill_per_leg=Inp(0.8, "MODELLED"), n_legs=2,
                                    cancel_cost_bps=Inp(0.1, "MODELLED"), missed_edge_bps=Inp(9.0, "MODELLED"))
    assert s["p_fill_all"] == pytest.approx(0.64) and s["p_no_fill"] == pytest.approx(0.36)
    assert s["cost_if_filled_bps"] == pytest.approx(2.5) and s["cost_if_not_filled_bps"] == pytest.approx(0.1 + 5.5 + 0.25 + 0.5)
    assert s["expected_bps"] == pytest.approx(0.64 * 2.5 + 0.36 * 6.35)
    assert s["expected_bps"] > s["cost_if_filled_bps"]                                     # ignoring no-fill would understate cost


def test_maker_first_skip_policy_pays_the_missed_edge_not_the_taker_fee():
    kw = dict(maker_side_fee_bps=Inp(2.0, "MODELLED"), taker_side_fee_bps=E.UNKNOWN, spread_side_bps=E.UNKNOWN, slippage_side_bps=E.UNKNOWN,
              adverse_selection_side_bps=Inp(0.0, "MODELLED"), p_fill_per_leg=Inp(0.5, "MODELLED"), n_legs=1, cancel_cost_bps=Inp(0.0, "MODELLED"),
              missed_edge_bps=Inp(10.0, "MODELLED"))
    s = E.maker_first_expected_cost(unfilled_policy="skip", **kw)
    assert s["expected_bps"] == pytest.approx(0.5 * 2.0 + 0.5 * 10.0)
    assert E.maker_first_expected_cost(unfilled_policy="taker_fallback", **kw)["expected_bps"] is None        # taker inputs unknown
    with pytest.raises(ValueError):
        E.maker_first_expected_cost(unfilled_policy="hope", **kw)


@pytest.mark.parametrize("p", [E.UNKNOWN, Inp(1.5, "MODELLED"), Inp(-0.1, "MODELLED")])
def test_unknown_or_impossible_fill_probability_gives_no_expectation(p):
    s = E.maker_first_expected_cost(maker_side_fee_bps=Inp(2.0, "MODELLED"), taker_side_fee_bps=Inp(5.5, "VENUE_STATED"), spread_side_bps=Inp(0.0, "MEASURED"),
                                    slippage_side_bps=Inp(0.0, "MEASURED"), adverse_selection_side_bps=Inp(0.0, "MODELLED"), p_fill_per_leg=p, n_legs=1,
                                    cancel_cost_bps=Inp(0.0, "MODELLED"), missed_edge_bps=Inp(1.0, "MODELLED"))
    assert s["expected_bps"] is None and s["p_no_fill"] is None


def test_maker_style_assessment_is_costlier_than_the_best_case_and_pessimistic_has_more_no_fill():
    r = E.assess(maker_inputs())
    b, p = r["base"], r["pessimistic"]
    assert b["maker_first_side"]["p_no_fill"] == pytest.approx(0.2) and p["maker_first_side"]["p_no_fill"] == pytest.approx(0.6)
    best_case = CC.ROUND_TRIP_FEE_BPS_PERP_OVERLAY_MAKER_MODELLED
    assert b["components"]["entry_fee"] + b["components"]["exit_fee"] > best_case                     # not-filled and adverse selection are priced
    assert r["verdict"] in ("PASS", "NOT_PROVEN") and r["prov"] == "MODELLED"


def test_maker_with_unknown_fill_probability_is_unknown_not_best_case():
    r = E.assess(maker_inputs(p_fill_per_leg=E.UNKNOWN))
    assert r["verdict"] == "UNKNOWN" and r["base"]["expected_net_bps"] is None


def test_acquire_maker_has_no_invented_spot_maker_fee():
    m = E.fee_inputs("acquire_spot_perp", "maker")
    r = E.assess(maker_inputs(position_class="acquire_spot_perp", maker_fee_round_trip_bps=m["fee_round_trip_bps"]))
    assert r["verdict"] == "UNKNOWN"


# ---------------------------------------------------------------- stress set
POL = {"max_leverage": 3.0}


def book(**kw):
    b = {"equity_usd": 10000.0, "maintenance_margin_frac": 0.01,
         "positions": {"BTCUSDT": {"category": "linear", "notional_usd": -5000.0}, "SPOT:BTCUSDT": {"category": "spot", "notional_usd": 5000.0}}}
    b.update(kw)
    return b


def test_stress_set_has_all_ten_scenarios_and_a_small_hedged_book_survives():
    res = E.stress_set(book(), POL)
    assert tuple(res) == E.STRESS_SCENARIOS and len(res) == 10
    assert E.stress_summary(res) == {"all_survive": True, "failing_or_unknown": []}
    assert res["funding_reversal"]["loss_usd"] == pytest.approx(5000 * 30 / 1e4 * 3)
    assert res["basis_widening"]["loss_usd"] == pytest.approx(5000 * 100 / 1e4)
    assert res["fee_tier_change"]["loss_usd"] == pytest.approx(10000 * 5 * 2 / 1e4)
    assert res["gap"]["loss_usd"] == pytest.approx(0.0) and res["gap"]["margin_drawdown_usd"] == pytest.approx(500.0)       # hedged economically, but margin is cash


def test_oversized_levered_book_does_not_survive_the_gap():
    b = book(equity_usd=500.0, positions={"BTCUSDT": {"category": "linear", "notional_usd": -6000.0}, "SPOT:BTCUSDT": {"category": "spot", "notional_usd": 6000.0}})
    res = E.stress_set(b, POL)
    assert res["gap"]["survives"] is False and "EQUITY_WIPED" in res["gap"]["reasons"]
    ok = E.stress_set(book(equity_usd=2000.0, positions=b["positions"]), POL)
    assert ok["gap"]["survives"] is False and "LEVERAGE_ABOVE_POLICY" in ok["gap"]["reasons"]
    assert E.stress_summary(res)["all_survive"] is False


def test_partial_hedge_and_unhedged_exposure_is_hurt_by_a_move():
    b = book(positions={"BTCUSDT": {"category": "linear", "notional_usd": -5000.0}, "SPOT:BTCUSDT": {"category": "spot", "notional_usd": 1000.0}})
    res = E.stress_set(b, POL)
    assert res["high_volatility"]["loss_usd"] == pytest.approx(4000 * 0.12) and res["partial_hedge"]["loss_usd"] > 0
    assert res["order_ack_uncertainty"]["loss_usd"] == pytest.approx(5000 * 0.05)


def test_missing_stress_param_is_unknown_not_survival():
    p = E.StressParams(**{**E.DEFAULT_STRESS_PARAMS.__dict__, "gap_frac": None})
    res = E.stress_set(book(), POL, p)
    assert res["gap"]["survives"] is None and res["gap"]["loss_usd"] is None and E.stress_summary(res)["all_survive"] is False and "gap" in E.stress_summary(res)["failing_or_unknown"]


@pytest.mark.parametrize("b", [book(equity_usd=None), book(equity_usd=0), book(maintenance_margin_frac=None),
                               book(positions={"BTCUSDT": {"category": "linear", "notional_usd": None}}), book(positions=None)])
def test_unknown_book_inputs_never_survive(b):
    res = E.stress_set(b, POL)
    assert all(v["survives"] is None for v in res.values()) or all(v["survives"] is not True for v in res.values())
    assert E.stress_summary(res)["all_survive"] is False


def test_policy_leverage_unknown_is_not_survival():
    assert E.stress_summary(E.stress_set(book(), {}))["all_survive"] is False


def test_policy_leverage_cap_is_part_of_survival():
    b = book(equity_usd=3400.0)
    res = E.stress_set(b, {"max_leverage": 1.0})
    assert any("LEVERAGE_ABOVE_POLICY" in v["reasons"] for v in res.values())


# ---------------------------------------------------------------- leverage ceiling
def test_max_safe_leverage_from_liquidation_geometry():
    assert E.max_safe_leverage(stress_adverse_move_frac=0.15, maintenance_margin_frac=0.01, liquidation_fee_frac=0.005, buffer_frac=0.035) == 5.0
    assert E.max_safe_leverage(stress_adverse_move_frac=0.30, maintenance_margin_frac=0.01) == 3.22            # floor to 0.01 (3.2258)
    assert E.max_safe_leverage(stress_adverse_move_frac=0.5, maintenance_margin_frac=0.1) == 1.66


def test_leverage_is_capped_by_venue_and_policy_and_never_raised_by_them():
    assert E.max_safe_leverage(stress_adverse_move_frac=0.05, maintenance_margin_frac=0.01, venue_max_leverage=10, policy_max_leverage=3) == 3.0
    assert E.max_safe_leverage(stress_adverse_move_frac=0.5, maintenance_margin_frac=0.1, policy_max_leverage=3) == 1.66


def test_leverage_takes_no_return_input_and_unknown_geometry_is_none():
    params = set(inspect.signature(E.max_safe_leverage).parameters)
    assert params == {"stress_adverse_move_frac", "maintenance_margin_frac", "liquidation_fee_frac", "buffer_frac", "venue_max_leverage", "policy_max_leverage"}
    assert not any(w in n for n in params for w in ("return", "edge", "funding", "apr", "pnl", "profit", "target"))
    for kw in (dict(stress_adverse_move_frac=None, maintenance_margin_frac=0.01), dict(stress_adverse_move_frac=0.1, maintenance_margin_frac=None),
               dict(stress_adverse_move_frac=0.0, maintenance_margin_frac=0.0), dict(stress_adverse_move_frac=-0.1, maintenance_margin_frac=0.01),
               dict(stress_adverse_move_frac=float("nan"), maintenance_margin_frac=0.01), dict(stress_adverse_move_frac=0.1, maintenance_margin_frac=0.01, policy_max_leverage=0)):
        assert E.max_safe_leverage(**kw) is None


def test_no_new_fee_literals_all_come_from_carry_costs():
    import re
    src = inspect.getsource(E)
    code = "\n".join(l.split("#")[0] for l in src.splitlines() if not l.strip().startswith(('"""', "#")))
    for lit in (r"\b5\.5\b", r"\b11\.4\b", r"\b31\.0\b", r"\b0\.4\b", r"\b10\.0\b"):
        assert not re.search(lit, code.split("PESSIMISTIC_FACTORS")[0]), lit
    assert E.fee_inputs("overlay_owned_spot", "taker")["fee_round_trip_bps"].value == CC.ROUND_TRIP_FEE_BPS_PERP_OVERLAY


def test_liquidation_is_detected_even_when_leverage_and_equity_look_fine():
    b = book(equity_usd=10000.0, maintenance_margin_frac=0.1,
             positions={"BTCUSDT": {"category": "linear", "notional_usd": -50000.0}, "SPOT:BTCUSDT": {"category": "spot", "notional_usd": 50000.0}})
    r = E.stress_set(b, {"max_leverage": 100.0})["gap"]                  # margin drawdown 5000 -> equity 5000 <= 10% of the 50k perp leg
    assert r["survives"] is False and r["reasons"] == ["LIQUIDATED"] and r["equity_after_usd"] > 0


def test_adverse_selection_is_priced_into_the_maker_expectation():
    lo = E.assess(maker_inputs(adverse_selection_bps=Inp(0.0, "MODELLED")))["base"]["one_time_cost_bps"]
    hi = E.assess(maker_inputs(adverse_selection_bps=Inp(4.0, "MODELLED")))["base"]["one_time_cost_bps"]
    assert hi > lo
