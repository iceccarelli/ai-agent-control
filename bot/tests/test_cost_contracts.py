"""Cross-module COST CONTRACTS: one fee / spread / funding / interval / net-edge vocabulary, agreed by every module that prices carry.

Why this file exists. Four bare numbers circulated for "the round trip": 11.0, 11.4, 31.0 and 4.0. They are different QUANTITIES (see the block
comment in carry_costs.py), not rival estimates:

    11.0  = 2 x 5.5   perp taker FEE only (overlay)          venue_stated   what the entry gate prices
    11.4  = 11.0 + 0.4  fee + ASSUMED spread allowance        assumed        what the teardown and the FROZEN 1.131 hurdle price
    31.0  = 2 x (10 + 5.5)  spot + perp taker FEE (acquire)   venue_stated
     4.0  = 2 x 2.0   perp MAKER fee, every leg rests+fills   modelled       a SCENARIO, never an observation

Every expected number below is a LITERAL written by hand, so changing any constant (a fee, the 0.4, the maker fee, the intervals per day) makes a test
fail: that is the mutation guard. ``tools/frozen_proof.py`` pins the frozen strategy; ``carry_prospective.PINNED_DIGESTS`` pins the plan, and
``test_prospective_round_trip_is_the_authoritative_total`` makes any drift between the two fail loudly instead of silently re-pricing the hurdle.
"""
from __future__ import annotations

import os
import re
from decimal import Decimal as D

import pytest

from test_economic_model import INPUT_SHA, SPEC, book, overlay, perp, q, spot, tier  # noqa: F401,E402  (first: it sets sys.path / the mutation override)
import carry_costs as CC  # noqa: E402
import carry_prospective as CP  # noqa: E402
import carry_teardown as CT  # noqa: E402
import economic_model as EM  # noqa: E402
import execution_economics as XE  # noqa: E402
import mainnet_public as MP  # noqa: E402
import opportunity_map as OM  # noqa: E402
import strategy_verdict as SV  # noqa: E402
import trade_economics as TE  # noqa: E402

BOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ---------------------------------------------------------------- 1. the vocabulary itself (literals: any edit fails)
def test_authoritative_constants_are_pinned_literals():
    assert CC.TAKER_BPS_PERP == 5.5 and CC.TAKER_BPS_SPOT == 10.0
    assert CC.ROUND_TRIP_FEE_BPS_PERP_OVERLAY == 11.0
    assert CC.ROUND_TRIP_FEE_BPS_SPOT_PERP == 31.0 == CC.ROUND_TRIP_BPS
    assert CC.ASSUMED_ROUND_TRIP_SPREAD_BPS == 0.4
    assert CC.ROUND_TRIP_TOTAL_COST_BPS_PERP_OVERLAY == 11.4
    assert CC.MAKER_BPS_PERP_MODELLED == 2.0 and CC.ROUND_TRIP_FEE_BPS_PERP_OVERLAY_MAKER_MODELLED == 4.0


def test_total_is_fee_plus_assumed_spread_and_is_not_the_fee():
    assert CC.ROUND_TRIP_TOTAL_COST_BPS_PERP_OVERLAY == pytest.approx(CC.ROUND_TRIP_FEE_BPS_PERP_OVERLAY + CC.ASSUMED_ROUND_TRIP_SPREAD_BPS, abs=1e-12)
    assert CC.ROUND_TRIP_TOTAL_COST_BPS_PERP_OVERLAY > CC.ROUND_TRIP_FEE_BPS_PERP_OVERLAY       # the gate (fee) is NOT the hurdle (fee + spread)
    assert CC.ROUND_TRIP_FEE_BPS_PERP_OVERLAY == 2 * CC.TAKER_BPS_PERP                             # per LEG x 2 legs = per ROUND TRIP
    assert CC.ROUND_TRIP_FEE_BPS_SPOT_PERP == 2 * (CC.TAKER_BPS_SPOT + CC.TAKER_BPS_PERP)
    assert CC.ROUND_TRIP_FEE_BPS_PERP_OVERLAY_MAKER_MODELLED == 2 * CC.MAKER_BPS_PERP_MODELLED


def test_provenance_labels_are_declared_and_the_assumed_term_makes_the_total_assumed():
    pv = CC.COST_PROVENANCE
    assert set(pv.values()) <= set(EM.PROVENANCE)                       # only the six economic_model labels
    assert pv["ASSUMED_ROUND_TRIP_SPREAD_BPS"] == "assumed"             # no measurement behind it
    assert pv["MAKER_BPS_PERP_MODELLED"] == pv["ROUND_TRIP_FEE_BPS_PERP_OVERLAY_MAKER_MODELLED"] == "modelled"
    assert pv["TAKER_BPS_PERP"] == pv["ROUND_TRIP_FEE_BPS_PERP_OVERLAY"] == "venue_stated"
    assert pv["ROUND_TRIP_TOTAL_COST_BPS_PERP_OVERLAY"] == "assumed"
    # the label is not hand-set: it is what economic_model's own propagation gives for fee (venue_stated) + spread (assumed)
    fee = q(str(CC.ROUND_TRIP_FEE_BPS_PERP_OVERLAY), "venue_stated", "bps")
    spread = q(str(CC.ASSUMED_ROUND_TRIP_SPREAD_BPS), "assumed", "bps")
    assert (fee + spread).provenance == pv["ROUND_TRIP_TOTAL_COST_BPS_PERP_OVERLAY"]
    assert (fee + spread).value == D("11.4")
    assert not (fee + spread).observed and fee.observed


# ---------------------------------------------------------------- 2. every module that prices a round trip uses the same names
def test_prospective_round_trip_is_the_authoritative_total():
    # the FROZEN plan's own constant (its digest is pinned) must equal the single authoritative total: drift on either side fails here
    assert CP.ROUND_TRIP_BPS == CC.ROUND_TRIP_TOTAL_COST_BPS_PERP_OVERLAY == 11.4
    assert CP.PARAMETERS["round_trip_bps"] == 11.4 and CP.PARAMETERS["round_trip_bps_status"] == "ASSUMED_UNVERIFIED_OWNER_FEE_TIER"
    assert CT.DEFAULT_ROUND_TRIP_BPS == CC.ROUND_TRIP_TOTAL_COST_BPS_PERP_OVERLAY
    assert CT.COST_SCENARIOS_BPS == (6.0, 11.4, 17.0)
    for h in CP.HURDLES_PCT:                                               # the registered plans still digest-match
        assert CP.registered_plan(h) is not None


def test_gate_round_trip_is_the_fee_only_figure_everywhere():
    assert MP.GATE_ROUND_TRIP_BPS == CC.ROUND_TRIP_FEE_BPS_PERP_OVERLAY    # mainnet_public replays the gate with the FEE, not fee + spread
    with open(os.path.join(BOT, "tools", "carry_core.py"), encoding="utf-8") as f:
        src = f.read()
    literals = {float(m) for m in re.findall(r"overlay=True\)|round_trip_bps=([0-9.]+), gated=True, overlay=True", src) if m}
    assert literals == {CC.ROUND_TRIP_FEE_BPS_PERP_OVERLAY}                # carry_core's overlay bench literal is the fee only


def test_opportunity_map_uses_the_authoritative_fee_and_maker_figures():
    t = OM.fee_tier()
    assert (t["perp_taker_bps"], t["perp_maker_bps"], t["spot_taker_bps"]) == (5.5, 2.0, 10.0)
    assert OM.PERP_MAKER_BPS == CC.MAKER_BPS_PERP_MODELLED
    ov_t = OM.round_trip_bps(mode="overlay", style="taker", tier=t, slippage_bps_per_leg=0.0)
    ov_m = OM.round_trip_bps(mode="overlay", style="maker", tier=t, slippage_bps_per_leg=0.0)
    ac_t = OM.round_trip_bps(mode="acquire", style="taker", tier=t, slippage_bps_per_leg=0.0)
    assert ov_t["total_bps"] == CC.ROUND_TRIP_FEE_BPS_PERP_OVERLAY == 11.0
    assert ov_m["total_bps"] == CC.ROUND_TRIP_FEE_BPS_PERP_OVERLAY_MAKER_MODELLED == 4.0
    assert ac_t["total_bps"] == CC.ROUND_TRIP_FEE_BPS_SPOT_PERP == CC.ROUND_TRIP_BPS == 31.0
    # slippage is paid per taker cross (2 perp legs overlay, 4 legs acquire) and never on a resting maker leg
    assert OM.round_trip_bps(mode="overlay", style="taker", tier=t, slippage_bps_per_leg=0.2)["total_bps"] == pytest.approx(11.0 + 2 * 0.2)
    assert OM.round_trip_bps(mode="overlay", style="maker", tier=t, slippage_bps_per_leg=0.2)["slippage_bps"] == 0.0
    ac_m = OM.round_trip_bps(mode="acquire", style="maker", tier=t, slippage_bps_per_leg=0.0)
    assert ac_m["total_bps"] == 2 * 2.0 + 2 * 10.0 == 24.0 and OM.SPOT_MAKER_BPS == 10.0     # Bybit spot has no maker discount in this tier
    # the default cost levels of the persistence study: maker scenario, fee-only gate, then an unexplained 13.0 sensitivity level
    assert OM.funding_persistence.__defaults__[0][:2] == (4.0, 11.0)


def test_execution_economics_maker_is_a_modelled_scenario_with_the_authoritative_fee():
    assert XE.ASSUMED_MAKER_BPS == CC.MAKER_BPS_PERP_MODELLED == 2.0
    a = XE.LegAssumptions(taker_bps=CC.TAKER_BPS_PERP, fill_probability=1.0)         # every leg rests and fills, no adverse selection: the 4.0 best case
    out = XE.compare(a, a)
    assert out["round_trip"]["taker_bps"] == CC.ROUND_TRIP_FEE_BPS_PERP_OVERLAY
    assert out["round_trip"]["maker_first_bps"] == CC.ROUND_TRIP_FEE_BPS_PERP_OVERLAY_MAKER_MODELLED
    assert out["measured_fill_data_used"] is False and "NOT A MEASUREMENT" in out["label"]       # provenance label travels with the number
    worse = XE.compare(XE.LegAssumptions(taker_bps=5.5, fill_probability=0.0), XE.LegAssumptions(taker_bps=5.5, fill_probability=0.0))
    assert worse["round_trip"]["maker_first_bps"] >= worse["round_trip"]["taker_bps"]            # a never-filling maker is never cheaper than a taker


def test_economic_model_fee_chain_agrees_with_carry_costs_and_opportunity_map():
    fee = EM.evaluate_chain(overlay()).components["fees"]
    assert float(fee.value) == CC.ROUND_TRIP_FEE_BPS_PERP_OVERLAY
    two = EM.evaluate_chain(overlay(legs=(EM.Leg("spot", spot(), "buy", book(), tier(taker="0.0010")), EM.Leg("perp", perp(), "sell", book(), tier()))))
    assert float(two.components["fees"].value) == CC.ROUND_TRIP_FEE_BPS_SPOT_PERP
    maker = EM.evaluate_chain(overlay(legs=(EM.Leg("perp", perp(), "sell", book(), tier(), role="maker"),),
                                      maker_fill_probability=q("1", "modelled"), adverse_selection_bps=q("0", "modelled")))
    assert float(maker.components["fees"].value) == CC.ROUND_TRIP_FEE_BPS_PERP_OVERLAY_MAKER_MODELLED


# ---------------------------------------------------------------- 3. units: bps vs fraction, per leg / round trip / settlement / day / trade
def test_bps_versus_fraction_units():
    assert EM.BPS == D(10000)
    assert EM.round_trip_fee_bps([q("0.00055", "venue_stated", "fraction")]).value == D("11.00000")        # 0.00055 per leg -> 11 bps per round trip
    assert float(EM.round_trip_fee_bps([q("0.00055", "venue_stated"), q("0.0010", "venue_stated")]).value) == CC.ROUND_TRIP_FEE_BPS_SPOT_PERP
    with pytest.raises(EM.EconomicModelError):
        tier(taker="5.5")                                                  # 5.5 read as a FRACTION is a 550 % fee: a unit slip, refused
    with pytest.raises(EM.EconomicModelError):
        tier(taker="0.055")                                                # a percent pasted as a fraction (5.5 %) is refused too
    assert CT.net_return(0.0, 30.0, 11.4, 0.1)["net_bps"] == pytest.approx(-11.4)       # a cost in bps of NOTIONAL, charged once per trade


def test_per_settlement_per_day_per_trade_conversions_agree_across_modules():
    cost, hold, spd = 11.4, 30.0, 3
    n = int(hold * spd)
    be_cc = CC.breakeven_funding_bps_per_settlement(n, round_trip_bps=cost)
    be_em = EM.breakeven_funding_bps(n, "11.4", 0, spd)
    be_om = OM.breakeven_funding(cost, hold)
    assert be_cc == pytest.approx(cost / 90.0) == pytest.approx(float(be_em), abs=1e-12) == pytest.approx(be_om, abs=1e-12)
    assert CT.breakeven(cost, 0.65)["days"] == pytest.approx(cost / 0.65 / spd)                  # per settlement -> settlements -> days
    # per DAY borrow enters per settlement as borrow_bps_per_day / settlements_per_day, identically in all three
    assert CC.breakeven_funding_bps_per_settlement(n, round_trip_bps=cost, borrow_bps_per_day=3.0) == pytest.approx(cost / n + 1.0)
    assert float(EM.breakeven_funding_bps(n, "11.4", 3, spd)) == pytest.approx(cost / n + 1.0)
    # the hurdle: required per-settlement funding x settlements per trade == cost + the return the hurdle needs
    need = CT.required_funding(10.0, hold, cost, 0.10)
    assert need * spd * hold == pytest.approx(cost + 10.0 / 100 * 1.10 * hold / 365 * 1e4)
    assert need == pytest.approx(CP.required_bps(10.0), abs=1e-12) == pytest.approx(float(EM.required_funding_bps_per_settlement(10, 30, "11.4", "0.10", 3)), abs=1e-12)
    assert round(need, 6) == 1.131233
    # fee-only (gate) vs fee + spread (frozen hurdle): the difference is exactly the spread allowance spread over the hold
    fee_only = float(EM.required_funding_bps_per_settlement(10, 30, "11.0", "0.10", 3))
    assert need - fee_only == pytest.approx(CC.ASSUMED_ROUND_TRIP_SPREAD_BPS / (spd * hold))
    assert round(fee_only, 6) == 1.126788


def test_funding_rate_fraction_to_bps_per_settlement_is_a_single_conversion():
    # a venue funding rate is a FRACTION per settlement: 0.0001 = 1 bps per settlement, whatever the interval
    for hours in (1, 4, 8):
        i = perp(funding_interval_hours=hours)
        g = EM.carry_gross_edge(i, q("0.0001", "modelled"), hold_days=D(1))
        assert g.value == D(24 // hours) * D(1)                           # bps of notional over one day = settlements/day x 1 bps
    assert EM.to_decimal("0.0001") * EM.BPS == D(1)                      # fraction -> bps is x 10_000, once


# ---------------------------------------------------------------- 4. settlement interval: from the instrument, never assumed 8h
@pytest.mark.parametrize("minutes,per_day", [(480, 3), (240, 6), (60, 24)])
def test_settlements_per_day_come_from_the_instrument_row(minutes, per_day):
    row = {"category": "linear", "symbol": "BTCUSDT", "baseCoin": "BTC", "quoteCoin": "USDT", "status": "Trading", "fundingInterval": minutes,
           "upperFundingRate": "0.0075", "lowerFundingRate": "-0.0075", "priceFilter": {"tickSize": "0.10"},
           "lotSizeFilter": {"qtyStep": "0.001", "minOrderQty": "0.001", "minNotionalValue": "5"}}
    inst = EM.Instrument.from_venue_row(row, expect_symbol="BTCUSDT", expect_category="linear")
    assert inst.settlements_per_day == D(per_day) and inst.interval_ms == minutes * 60_000
    # the same hurdle at the instrument's own cadence: required funding per settlement scales as 1/settlements-per-day
    need = EM.required_funding_bps_per_settlement(10, 30, "11.4", "0.10", inst.settlements_per_day)
    assert float(need) == pytest.approx(CT.required_funding(10.0, 30.0, 11.4, 0.10) * 3 / per_day, abs=1e-12)


def test_the_8h_cadence_is_one_declared_constant_and_other_cadences_are_refused_or_unknown():
    assert TE.SETTLEMENTS_PER_DAY == 3.0
    assert CC.FUNDING_PERIODS_PER_DAY == CT.PER_DAY == CP.PER_DAY == OM.PRINTS_PER_DAY == 3
    assert CP.SETTLE_MS == CT.SETTLE_MS == 8 * 3_600_000
    assert float(perp().settlements_per_day) == CC.FUNDING_PERIODS_PER_DAY
    with pytest.raises(EM.EconomicModelError):
        spot().interval_ms                                                # a spot instrument has no funding interval: never defaulted to 8h
    assert spot().settlements_per_day == 0
    bad = {"category": "linear", "symbol": "BTCUSDT", "baseCoin": "BTC", "quoteCoin": "USDT", "status": "Trading", "fundingInterval": 90,
           "upperFundingRate": "0.0075", "lowerFundingRate": "-0.0075", "priceFilter": {"tickSize": "0.10"},
           "lotSizeFilter": {"qtyStep": "0.001", "minOrderQty": "0.001", "minNotionalValue": "5"}}
    with pytest.raises(EM.EconomicModelError):
        EM.Instrument.from_venue_row(bad, expect_symbol="BTCUSDT", expect_category="linear")        # 90 minutes is not a whole-hour interval


# ---------------------------------------------------------------- 5. net edge agrees across the ledgers
def test_net_edge_agrees_between_teardown_trade_economics_and_the_cost_chain():
    funding, hold = 0.65, 30
    gross = funding * 3 * hold                                            # bps of notional over the hold
    cost = CC.ROUND_TRIP_TOTAL_COST_BPS_PERP_OVERLAY
    assert CT.net_return(funding, hold, cost, 0.10)["net_bps"] == pytest.approx(gross - cost)
    pos = TE.Position(notional_usd=100_000.0, hold_days=float(hold), btc_inventory_usd=100_000.0, btc_was_owned=True, margin_posted_usd=0.0,
                      collateral_haircut=0.0, reserve_usd=10_000.0, maintenance_margin_rate=0.005, initial_margin_rate=0.01)
    costs = TE.Costs(perp_entry_fee_bps=CC.TAKER_BPS_PERP, perp_exit_fee_bps=CC.TAKER_BPS_PERP, spread_bps=CC.ASSUMED_ROUND_TRIP_SPREAD_BPS,
                     slippage_bps=0.0, impact_bps=0.0, latency_bps=0.0, conversion_bps=0.0, transfer_usd=0.0, custody_apr=0.0, operational_usd=0.0,
                     borrow_apr=0.0)
    led = TE.compute(pos, costs, [funding] * (3 * hold), funding_source="PUBLIC_REALISED")
    fees_and_spread_usd = (2 * CC.TAKER_BPS_PERP + CC.ASSUMED_ROUND_TRIP_SPREAD_BPS) / 1e4 * 100_000.0
    assert fees_and_spread_usd == pytest.approx(cost / 1e4 * 100_000.0)                       # trade_economics charges the same 11.4 bps on the same notional
    assert led["net_pnl_usd"] == pytest.approx(gross / 1e4 * 100_000.0 - fees_and_spread_usd) == pytest.approx(471.0)
    assert led["cost_bps_of_notional_round_trip"] == pytest.approx(cost) == pytest.approx(11.4)
    assert led["fees"]["verified"] is False and led["fees"]["provenance"] == "UNVERIFIED"       # the ledger does not upgrade the label either


def test_fee_only_net_edge_is_not_the_hurdle_net_edge():
    # at a funding that clears the 11.0 gate over 90 settlements but not the 11.4 hurdle's cost recovery by the margin of the 0.4
    n = 90
    f = (CC.ROUND_TRIP_FEE_BPS_PERP_OVERLAY + 0.1) / n
    assert f * n - CC.ROUND_TRIP_FEE_BPS_PERP_OVERLAY > 0                    # clears the fee-only gate
    assert f * n - CC.ROUND_TRIP_TOTAL_COST_BPS_PERP_OVERLAY < 0             # does not recover fee + assumed spread


# ---------------------------------------------------------------- 6. assumed / modelled inputs can never produce a PASS
def test_an_assumed_cost_input_can_never_yield_a_pass_and_the_same_number_observed_can():
    control = EM.evaluate_chain(overlay(impact_bps=q(str(CC.ASSUMED_ROUND_TRIP_SPREAD_BPS), "venue_stated")))
    assert control.assumed_inputs == () and EM.cost_model_evidence(control, spec=SPEC, data_sha256=INPUT_SHA).result == SV.PASS
    assumed = EM.evaluate_chain(overlay(impact_bps=q(str(CC.ASSUMED_ROUND_TRIP_SPREAD_BPS), "assumed")))
    assert assumed.assumed_inputs == ("impact",)
    assert assumed.net_edge_bps.value == control.net_edge_bps.value            # same arithmetic: ONLY the label differs, and the label decides
    assert EM.cost_model_evidence(assumed, spec=SPEC, data_sha256=INPUT_SHA).result == SV.INCONCLUSIVE


def test_an_assumed_gross_edge_or_unmeasured_maker_probability_cannot_pass():
    rich = overlay(gross_edge_bps=q("1000", "assumed"))
    ev = EM.cost_model_evidence(EM.evaluate_chain(rich), spec=SPEC, data_sha256=INPUT_SHA)
    assert ev.result == SV.INCONCLUSIVE
    maker_unknown = EM.evaluate_chain(overlay(legs=(EM.Leg("perp", perp(), "sell", book(), tier(), role="maker"),)))
    assert maker_unknown.net_edge_bps.value is None
    assert EM.cost_model_evidence(maker_unknown, spec=SPEC, data_sha256=INPUT_SHA).result == SV.INCONCLUSIVE
    maker_assumed = EM.evaluate_chain(overlay(legs=(EM.Leg("perp", perp(), "sell", book(), tier(), role="maker"),),
                                              maker_fill_probability=q("1", "assumed"), adverse_selection_bps=q("0", "modelled")))
    assert EM.cost_model_evidence(maker_assumed, spec=SPEC, data_sha256=INPUT_SHA).result != SV.PASS


def test_the_prospective_hurdle_labels_its_round_trip_assumed_not_verified():
    assert CP.PARAMETERS["round_trip_bps_status"].startswith("ASSUMED")
    assert "ASSUMED" in CT.TAGS and "ACCOUNT_VERIFIED" in CT.TAGS


# ---------------------------------------------------------------- 7. interval-invariant economics (owner probe: 480m +1.00 bps/print, 240m +0.50, same 3.00 bps/day)
INTERVALS = (480, 240, 60)
BPS_PER_DAY_CLAIM = 3.0


@pytest.mark.parametrize("m", INTERVALS)
def test_same_claim_same_bps_per_day_and_horizon_at_every_interval(m):
    per_print = BPS_PER_DAY_CLAIM / (1440 / m)
    assert CC.settlements_per_day(m) == 1440 / m
    assert CC.funding_bps_per_day(per_print, m) == pytest.approx(3.0)
    assert CC.funding_bps_over_horizon(per_print, m, 30) == pytest.approx(90.0)
    inst = perp(funding_interval_hours=m // 60)
    g = EM.carry_gross_edge(inst, q(str(per_print / 1e4), "modelled"), hold_days=D(30))     # fraction per print -> bps over the horizon
    assert float(g.value) == pytest.approx(90.0)
    # net edge over the same horizon after the same 11.4 bps round trip is the same at every cadence
    assert float(g.value) - CC.ROUND_TRIP_TOTAL_COST_BPS_PERP_OVERLAY == pytest.approx(78.6)
    assert CC.funding_bps_over_horizon(per_print, m, 30) - CC.ROUND_TRIP_TOTAL_COST_BPS_PERP_OVERLAY == pytest.approx(78.6)


def test_the_frozen_8h_hurdle_is_not_reused_for_other_cadences():
    h8 = CP.required_bps(10.0)
    assert CC.equivalent_hurdle_bps_per_print(h8, 480, 480) == pytest.approx(h8)
    assert CC.equivalent_hurdle_bps_per_print(h8, 480, 240) == pytest.approx(h8 / 2)
    assert CC.equivalent_hurdle_bps_per_print(h8, 480, 60) == pytest.approx(h8 / 8)
    assert round(h8, 6) == 1.131233                                          # the frozen figure itself is untouched
    base = EM.required_funding_at_interval(10, 30, "11.4", "0.10", 480)
    assert base.value.quantize(D("0.000001")) == D("1.131233") and base.provenance == "derived"
    assert float(EM.required_funding_at_interval(10, 30, "11.4", "0.10", 240).value) == pytest.approx(h8 / 2, abs=1e-12)
    assert float(EM.required_funding_at_interval(10, 30, "11.4", "0.10", 60).value) == pytest.approx(h8 / 8, abs=1e-12)
    # same bps/day required at every cadence
    days = {m: float(EM.required_funding_at_interval(10, 30, "11.4", "0.10", m).value) * 1440 / m for m in INTERVALS}
    assert days[480] == pytest.approx(days[240]) == pytest.approx(days[60])


@pytest.mark.parametrize("bad", [0, -480, None, True, "480", float("nan"), float("inf"), 7, 100, 91, 480.5, 2880])
def test_an_unusable_interval_is_unknown_never_8h(bad):
    assert CC.settlements_per_day(bad) is None
    assert CC.funding_bps_per_day(1.0, bad) is None and CC.funding_bps_over_horizon(1.0, bad, 30) is None
    assert CC.equivalent_hurdle_bps_per_print(1.131, bad, 480) is None and CC.equivalent_hurdle_bps_per_print(1.131, 480, bad) is None
    r = EM.required_funding_at_interval(10, 30, "11.4", "0.10", bad)
    assert r.value is None and r.provenance == "unknown"


def test_unusable_rate_or_horizon_is_unknown():
    assert CC.funding_bps_per_day(float("nan"), 480) is None and CC.funding_bps_per_day(None, 480) is None and CC.funding_bps_per_day(True, 480) is None
    assert CC.funding_bps_over_horizon(1.0, 480, 0) is None and CC.funding_bps_over_horizon(1.0, 480, -3) is None


# ---------------------------------------------------------------- 8. mixed intervals, net quoting, labels, extrapolation, hedged vs unhedged
GROSS_ARIA = 482.21          # the owner's quoted gross over its window, bps of notional (OWNER-OBSERVED, not verifiable here)


def test_owner_quoted_nets_are_reproducible_from_the_authoritative_totals():
    taker = CC.position_class_costs("overlay_owned_spot", "taker")["total_bps"]
    maker = CC.position_class_costs("overlay_owned_spot", "maker")["total_bps"]
    assert (taker, maker) == (11.4, 4.0)
    assert GROSS_ARIA - taker == pytest.approx(470.81) and GROSS_ARIA - maker == pytest.approx(478.21)
    # a spot+perp pair would have been charged 31.0 fee (+0.4): a different class, a different net
    assert GROSS_ARIA - CC.position_class_costs("acquire_spot_perp")["total_bps"] == pytest.approx(482.21 - 31.4)


@pytest.mark.parametrize("m", (480, 240, 60))
def test_net_over_the_same_horizon_is_interval_independent_and_costs_are_charged_once(m):
    horizon, per_day = 30, 3.0
    per_print = per_day / (1440 / m)
    gross = CC.funding_bps_over_horizon(per_print, m, horizon)
    assert gross == pytest.approx(90.0)
    cost = CC.ROUND_TRIP_TOTAL_COST_BPS_PERP_OVERLAY
    # ONE round trip per trade: not multiplied by the number of funding prints in the hold
    assert gross - cost == pytest.approx(90.0 - 11.4)
    assert gross - CC.ROUND_TRIP_FEE_BPS_PERP_OVERLAY_MAKER_MODELLED == pytest.approx(90.0 - 4.0)
    n_prints = horizon * 1440 / m
    assert gross - n_prints * cost != pytest.approx(gross - cost)            # a per-print charge would be the wrong (interval-dependent) answer


def test_maker_scenario_is_labelled_modelled_taker_fee_venue_stated_spread_assumed():
    pv = CC.COST_PROVENANCE
    assert pv["ROUND_TRIP_FEE_BPS_PERP_OVERLAY"] == "venue_stated" and pv["ASSUMED_ROUND_TRIP_SPREAD_BPS"] == "assumed"
    assert pv["ROUND_TRIP_FEE_BPS_PERP_OVERLAY_MAKER_MODELLED"] == "modelled"
    mk = CC.position_class_costs("overlay_owned_spot", "maker")
    assert mk["provenance"] == "modelled" and mk["fee_provenance"] == "modelled" and mk["spread_bps"] == 0.0
    tk = CC.position_class_costs("overlay_owned_spot", "taker")
    assert tk["fee_provenance"] == "venue_stated" and tk["provenance"] == "assumed" and tk["spread_bps"] == 0.4
    # the scenario assumes passive fills at 2.0 bps/leg, zero adverse selection, zero missed fills: any worse input only raises the cost
    a = XE.LegAssumptions(taker_bps=5.5, fill_probability=1.0)
    assert XE.compare(a, a)["round_trip"]["maker_first_bps"] == 4.0
    for worse in (dict(adverse_selection_bps=1.0), dict(fill_probability=0.8, missed_fill_cost_bps=1.0)):
        w = XE.LegAssumptions(taker_bps=5.5, **{"fill_probability": 1.0, **worse})
        assert XE.compare(w, w)["round_trip"]["maker_first_bps"] > 4.0


def test_annualisation_is_a_labelled_extrapolation_that_can_never_pass():
    r = CC.annualise_bps_from_window(470.81, 30)
    assert r["value_bps_per_year"] == pytest.approx(470.81 * 365 / 30)
    assert r["extrapolated_from_window"] is True and r["may_feed_verdict"] is False and r["provenance"] == "assumed"
    assert r["window_days"] == 30.0 and "EXTRAPOLATION" in r["label"]
    for bad in ((None, 30), (float("nan"), 30), (True, 30), (1.0, 0), (1.0, -5), (1.0, None)):
        assert CC.annualise_bps_from_window(*bad) is None
    # feeding it to the verdict layer as the gross edge yields INCONCLUSIVE, never PASS; the same number labelled observed would PASS (control)
    val = str(round(r["value_bps_per_year"], 4))
    ctrl = EM.cost_model_evidence(EM.evaluate_chain(overlay(gross_edge_bps=q(val, "venue_stated"))), spec=SPEC, data_sha256=INPUT_SHA)
    got = EM.cost_model_evidence(EM.evaluate_chain(overlay(gross_edge_bps=q(val, r["provenance"]))), spec=SPEC, data_sha256=INPUT_SHA)
    assert ctrl.result == SV.PASS and got.result == SV.INCONCLUSIVE


def test_position_classes_hedged_versus_unhedged():
    assert set(CC.POSITION_CLASSES) == {"overlay_owned_spot", "acquire_spot_perp", "perp_only_hedged_elsewhere", "perp_only_unhedged"}
    by = {c: CC.position_class_costs(c) for c in CC.POSITION_CLASSES}
    assert [by[c]["fee_bps"] for c in ("overlay_owned_spot", "perp_only_hedged_elsewhere", "perp_only_unhedged")] == [11.0, 11.0, 11.0]
    assert by["acquire_spot_perp"]["fee_bps"] == 31.0
    assert by["perp_only_unhedged"]["hedged"] is False and by["perp_only_unhedged"]["open_directional_risk"] is True
    assert by["perp_only_hedged_elsewhere"]["hedged"] is True and by["perp_only_hedged_elsewhere"]["hedge_leg_cost_included"] is False
    assert "UNKNOWN" in by["perp_only_hedged_elsewhere"]["note"] and "open risk" in by["perp_only_unhedged"]["note"]
    assert by["overlay_owned_spot"]["hedge_leg_cost_included"] and not by["perp_only_unhedged"]["hedge_leg_cost_included"]
    assert CC.position_class_costs("acquire_spot_perp", "maker")["total_bps"] is None
    with pytest.raises(ValueError):
        CC.position_class_costs("nonsense")
    with pytest.raises(ValueError):
        CC.position_class_costs("overlay_owned_spot", "hybrid")
