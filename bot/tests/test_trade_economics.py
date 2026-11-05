import math

import pytest

import trade_economics as TE


def pos(**kw):
    d = dict(notional_usd=100_000.0, hold_days=30.0, btc_inventory_usd=100_000.0, btc_was_owned=True,
             margin_posted_usd=0.0, collateral_haircut=0.1, reserve_usd=10_000.0,
             maintenance_margin_rate=0.005, initial_margin_rate=0.05)
    d.update(kw)
    return TE.Position(**d)


def costs(**kw):
    d = dict(perp_entry_fee_bps=5.5, perp_exit_fee_bps=5.5, spread_bps=0.2, slippage_bps=0.3, impact_bps=0.1,
             latency_bps=0.5, conversion_bps=0.0, transfer_usd=0.0, custody_apr=0.0, operational_usd=0.0, borrow_apr=0.0)
    d.update(kw)
    return TE.Costs(**d)


def test_every_component_is_separate_and_they_reconcile_to_the_net():
    r = TE.compute(pos(), costs(), [0.65] * 90)
    c = r["components_usd"]
    assert c["funding_income_usd"] == pytest.approx(100_000 * 0.65 * 90 / 1e4)
    assert c["perp_entry_fee_usd"] == pytest.approx(-55.0) and c["perp_exit_fee_usd"] == pytest.approx(-55.0)
    assert r["net_pnl_usd"] == pytest.approx(sum(c.values())) and r["reconciles"]
    for k in ("spread_usd", "slippage_usd", "market_impact_usd", "latency_usd", "basis_pnl_usd", "rebates_usd"):
        assert k in c


def test_unknown_inputs_are_listed_and_never_silently_zero():
    r = TE.compute(pos(), TE.Costs(perp_entry_fee_bps=5.5, perp_exit_fee_bps=5.5), [0.65] * 90)
    assert {"spread", "slippage", "latency", "custody", "borrow", "operational"} <= set(r["unknowns"])
    full = TE.compute(pos(), costs(), [0.65] * 90)
    assert full["unknowns"] == []


def test_two_denominators_and_the_inventory_counts_even_when_already_owned():
    r = TE.compute(pos(), costs(), [0.65] * 90)
    assert r["capital"]["economic_capital_usd"] == 110_000.0 and r["capital"]["btc_was_owned"] is True
    assert r["returns"]["annualised_pct_on_economic_capital"] < r["returns"]["annualised_pct_on_notional"]
    owned = TE.compute(pos(btc_was_owned=True), costs(), [0.65] * 90)
    bought = TE.compute(pos(btc_was_owned=False), costs(), [0.65] * 90)
    assert owned["returns"] == bought["returns"]            # ownership changes nothing economic


def test_leverage_is_not_an_edge():
    base = TE.compute(pos(margin_posted_usd=20_000.0), costs(), [0.65] * 90)
    thin = TE.compute(pos(margin_posted_usd=2_000.0), costs(), [0.65] * 90)
    # the figure that rises with leverage is the one explicitly labelled as not an edge
    assert thin["returns"]["annualised_pct_on_posted_margin"] > 5 * base["returns"]["annualised_pct_on_posted_margin"]
    assert "NOT an edge" in base["returns"]["posted_margin_note"]
    assert abs(thin["returns"]["annualised_pct_on_economic_capital"]
               - base["returns"]["annualised_pct_on_economic_capital"]) < 1.0


def test_hurdles_are_the_three_provisional_screens_with_no_silent_4_percent():
    r = TE.compute(pos(), costs(), [0.65] * 90)
    assert [h["hurdle_apr_pct"] for h in r["hurdles"]] == [10.0, 15.0, 20.0]
    assert all(h["status"] == "PROVISIONAL_SCREENING_HURDLE_NOT_OWNER_APPROVED" for h in r["hurdles"])
    assert r["alternative_return"] == {"apr_pct": "UNSPECIFIED", "currency": "USD",
                                       "capital_denominator": "economic_capital", "risk_premium_pct": "UNSPECIFIED"}
    assert 4.0 not in [h["hurdle_apr_pct"] for h in r["hurdles"]]


def test_a_7_percent_gross_carry_does_not_meet_a_10_percent_screen_on_economic_capital():
    r = TE.compute(pos(), costs(), [0.65] * 90)            # 30 days of +0.65 bps / settlement
    assert 0 < r["returns"]["annualised_pct_on_economic_capital"] < 10.0
    assert not any(h["meets"] for h in r["hurdles"])


def test_basis_and_funding_are_separate_cash_flows():
    flat = TE.compute(pos(), costs(), [0.65] * 90, entry_basis_bps=-3.0, exit_basis_bps=-3.0)
    # basis (perp minus spot) moved -3 -> -8: the perp fell against spot, which FAVOURS a short perp
    fav = TE.compute(pos(), costs(), [0.65] * 90, entry_basis_bps=-3.0, exit_basis_bps=-8.0)
    # basis moved -3 -> +4: the perp rose against spot, which HURTS it
    adv = TE.compute(pos(), costs(), [0.65] * 90, entry_basis_bps=-3.0, exit_basis_bps=4.0)
    assert fav["components_usd"]["basis_pnl_usd"] == pytest.approx(100_000 * 5 / 1e4)
    assert adv["components_usd"]["basis_pnl_usd"] == pytest.approx(-100_000 * 7 / 1e4)
    for r in (fav, adv):
        assert r["components_usd"]["funding_income_usd"] == flat["components_usd"]["funding_income_usd"]
        assert r["net_pnl_usd"] - flat["net_pnl_usd"] == pytest.approx(r["components_usd"]["basis_pnl_usd"])


def test_funding_source_is_recorded_and_only_account_settled_is_cash():
    for src, cash in (("PREDICTED", False), ("PUBLIC_REALISED", False), ("SCENARIO", False), ("ACCOUNT_SETTLED", True)):
        assert TE.compute(pos(), costs(), [0.1] * 3, funding_source=src)["funding"]["is_cash"] is cash
    with pytest.raises(ValueError):
        TE.compute(pos(), costs(), [0.1], funding_source="FORECAST_AS_CASH")


def test_fee_provenance_is_carried_and_unverified_by_default():
    assert TE.compute(pos(), costs(), [0.1] * 3)["fees"] == {"provenance": "UNVERIFIED", "maker_taker": "taker", "verified": False}
    v = TE.compute(pos(), costs(fee_provenance="ACCOUNT_VERIFIED", maker_taker="maker"), [0.1] * 3)
    assert v["fees"]["verified"] is True


def test_break_even_funding_makes_the_net_zero():
    c = costs()
    r = TE.compute(pos(), c, [0.65] * 90)
    f = r["breakeven_funding_bps_per_settlement"]
    z = TE.compute(pos(), c, [f] * 90)
    assert z["net_pnl_usd"] == pytest.approx(0.0, abs=1e-6)


def test_sensitivity_shows_that_a_reversal_or_a_shock_changes_the_answer():
    rows = TE.sensitivity(pos(), costs(), 0.65)
    by = {(r["hold_days"], r["path"]): r for r in rows}
    assert by[(30, "constant")]["net_pnl_usd"] > by[(30, "half")]["net_pnl_usd"] > by[(30, "zero")]["net_pnl_usd"]
    assert by[(30, "reversal")]["net_pnl_usd"] < 0 and by[(30, "one_adverse_shock")]["net_pnl_usd"] < by[(30, "constant")]["net_pnl_usd"]
    assert by[(7, "constant")]["net_pnl_usd"] < by[(90, "constant")]["net_pnl_usd"]


def test_the_costs_of_borrowing_custody_and_operations_reduce_the_net():
    base = TE.compute(pos(), costs(), [0.65] * 90)["net_pnl_usd"]
    worse = TE.compute(pos(), costs(custody_apr=0.01, borrow_apr=0.05, borrowed_usd=50_000.0, operational_usd=20.0,
                                    transfer_usd=5.0, conversion_bps=2.0), [0.65] * 90)["net_pnl_usd"]
    assert worse < base - 20.0


def test_initial_margin_sufficiency_and_a_conservative_liquidation_headroom():
    ok = TE.compute(pos(margin_posted_usd=0.0), costs(), [0.1] * 3)
    assert ok["capital"]["available_collateral_usd"] == 90_000.0 and ok["capital"]["initial_margin_sufficient"]
    bad = TE.compute(pos(btc_inventory_usd=1_000.0, margin_posted_usd=0.0), costs(), [0.1] * 3)
    assert not bad["capital"]["initial_margin_sufficient"]
    assert ok["liquidation"]["conservative_adverse_move_pct"] == pytest.approx((90_000 - 500) / 100_000 * 100)
    assert "CONSERVATIVE" in ok["liquidation"]["note"]


@pytest.mark.parametrize("bad", [dict(notional_usd=0), dict(hold_days=float("nan")), dict(collateral_haircut=1.0),
                                 dict(margin_posted_usd=-1), dict(hedge_ratio=0)])
def test_invalid_inputs_are_refused(bad):
    with pytest.raises(ValueError):
        TE.compute(pos(**bad), costs(), [0.1] * 3)
    with pytest.raises(ValueError):
        TE.compute(pos(), costs(), [float("inf")])
