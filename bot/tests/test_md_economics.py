"""Carry entry economics from the real order book: exact walks, four honest
classes of number, and refusal when anything required is missing."""
from __future__ import annotations

import math

import pytest

import md_economics as ME
from test_md_integration import complete_state

BIDS = ((100.0, 1.0), (99.0, 2.0), (98.0, 5.0))
ASKS = ((101.0, 1.0), (102.0, 2.0), (103.0, 5.0))
DEPTH = {"perp_bids": BIDS, "perp_asks": ASKS, "spot_bids": BIDS, "spot_asks": ASKS}


# -- walking the book --------------------------------------------------------------------------------

def test_a_walk_matches_hand_arithmetic():
    w = ME.walk_leg("x", "buy", 2.0, BIDS, ASKS)                     # 1@101 + 1@102
    assert w.status == ME.MEASURED and w.avg_price == pytest.approx(101.5) and w.touch == 101.0
    mid = 100.5
    assert w.half_spread_bps == pytest.approx(0.5 / mid * 1e4)
    assert w.slippage_bps == pytest.approx(0.5 / 101.0 * 1e4)
    s = ME.walk_leg("y", "sell", 1.5, BIDS, ASKS)                     # 1@100 + 0.5@99
    assert s.avg_price == pytest.approx((100 + 49.5) / 1.5) and s.levels_used == 2


@pytest.mark.parametrize("qty,reason", [(0, "QTY_NOT_POSITIVE"), (-1, "QTY_NOT_POSITIVE"),
                                        (float("nan"), "QTY_NOT_POSITIVE"), (True, "QTY_NOT_POSITIVE"),
                                        (9.0, "DEPTH_INSUFFICIENT")])
def test_unusable_quantities_and_thin_books_are_unavailable_not_extrapolated(qty, reason):
    w = ME.walk_leg("x", "buy", qty, BIDS, ASKS)
    assert w.status == ME.UNAVAILABLE and w.reason == reason and w.avg_price is None


def test_empty_crossed_and_invalid_books_are_unavailable():
    assert ME.walk_leg("x", "buy", 1.0, (), ASKS).reason == "BOOK_ONE_SIDED_OR_EMPTY"
    assert ME.walk_leg("x", "buy", 1.0, ((101.0, 1.0),), ((100.0, 1.0),)).reason == "BOOK_CROSSED"
    assert ME.walk_leg("x", "buy", 1.0, ((100.0, float("nan")),), ASKS).reason == "BOOK_LEVEL_INVALID"
    assert ME.walk_leg("x", "buy", 1.0, ((-1.0, 1.0),), ASKS).reason == "BOOK_LEVEL_INVALID"


# -- round-trip costs ------------------------------------------------------------------------------------

def test_overlay_crosses_two_perp_legs_and_acquire_crosses_four():
    ov = ME.depth_costs(DEPTH, mode="overlay", notional_usd=201.0, ref_price=100.5, latency_allowance_bps=1.0)
    ac = ME.depth_costs(DEPTH, mode="acquire", notional_usd=201.0, ref_price=100.5, latency_allowance_bps=1.0)
    assert len(ov.legs) == 2 and len(ac.legs) == 4
    assert ac.spread_bps == pytest.approx(2 * ov.spread_bps) and ac.slippage_bps == pytest.approx(2 * ov.slippage_bps)
    assert ov.status == ME.MEASURED and ov.source == ME.DEPTH_SOURCE


def test_without_a_latency_allowance_the_book_walk_is_not_called_a_cost():
    d = ME.depth_costs(DEPTH, mode="overlay", notional_usd=100.0, ref_price=100.5)
    assert d.status == ME.UNAVAILABLE and "LATENCY_ALLOWANCE_NOT_SUPPLIED" in d.reasons
    assert d.spread_bps is not None and d.slippage_bps is not None          # still shown, not hidden
    bad = ME.depth_costs(DEPTH, mode="overlay", notional_usd=100.0, ref_price=100.5, latency_allowance_bps=-1.0)
    assert bad.status == ME.UNAVAILABLE and "LATENCY_ALLOWANCE_INVALID" in bad.reasons


def test_a_size_larger_than_the_displayed_depth_is_refused_at_the_leg_that_fails():
    d = ME.depth_costs(DEPTH, mode="overlay", notional_usd=100.5 * 20, ref_price=100.5, latency_allowance_bps=1.0)
    assert d.status == ME.UNAVAILABLE and d.reasons == ("perp_sell_entry:DEPTH_INSUFFICIENT",
                                                        "perp_buy_exit:DEPTH_INSUFFICIENT")


@pytest.mark.parametrize("kw", [dict(mode="margin"), dict(notional_usd=0), dict(notional_usd=float("inf")),
                                dict(ref_price=0), dict(ref_price=float("nan"))])
def test_bad_inputs_never_produce_a_number(kw):
    args = dict(mode="overlay", notional_usd=100.0, ref_price=100.5, latency_allowance_bps=1.0)
    args.update(kw)
    assert ME.depth_costs(DEPTH, **args).status == ME.UNAVAILABLE


# -- the four classes ---------------------------------------------------------------------------------------

def econ(view=None, **kw):
    if view is None:
        st, now, wall = complete_state()
        view = st.view(now, wall)
    args = dict(mode="overlay", notional_usd=100.0, funding_history_bps=[1.0, 0.9, 1.2, 0.8, 1.1, 1.0],
                borrow_apr=0.0, opportunity_cost_apr=0.0, latency_allowance_bps=2.0)
    args.update(kw)
    return ME.build_entry_economics(view, **args)


def test_every_number_carries_a_class_and_a_source():
    e = econ()
    classes = e.by_class()
    assert set(classes) == set(ME.CLASSES)
    assert {i["name"] for i in classes[ME.OBSERVED]} >= {"perp_bid", "perp_ask", "perp_mark", "perp_index",
                                                       "funding_rate_forecast", "next_funding_ms"}
    assert {i["name"] for i in classes[ME.CALCULATED]} >= {"perp_spread_bps", "mid_basis_bps",
                                                         "hours_to_next_settlement", "settlements_per_day"}
    est = {i["name"] for i in classes[ME.ESTIMATED]}
    assert {"round_trip_fee_bps", "expected_funding_bps_per_settlement", "income_uncertainty_bps",
            "exit_priced_on_todays_book", "latency_allowance_bps"} <= est
    assert e.items and all(i.source for i in e.items)


def test_what_the_public_feed_cannot_state_is_unknown_and_named():
    e = econ(latency_allowance_bps=None)
    unknown = {i["name"] for i in e.by_class()[ME.UNKNOWN]}
    assert {"latency_allowance_bps", "account_fee_tier", "available_margin_usd"} <= unknown
    assert not e.complete and {"latency_allowance", "available_margin"} <= set(e.missing)
    assert e.decision is None and e.net_benefit_bps is None                    # no verdict on partial inputs


def test_the_default_fee_schedule_is_labelled_unverified_and_account_rates_are_checked():
    e = econ()
    fee = next(i for i in e.items if i.name == "round_trip_fee_bps")
    assert fee.value == pytest.approx(2 * 5.5) and "UNVERIFIED" in fee.source
    ac = econ(mode="acquire", notional_usd=100.0)
    assert next(i for i in ac.items if i.name == "round_trip_fee_bps").value == pytest.approx(2 * 5.5 + 2 * 10.0)
    sched = ME.FeeSchedule.from_account({"perp": {"taker_bps": 4.0}, "spot": {"taker_bps": 8.0}})
    v = econ(fees=sched)
    assert next(i for i in v.items if i.name == "round_trip_fee_bps").source == ME.FEE_ACCOUNT_VERIFIED
    assert "account_fee_tier" not in {i.name for i in v.items if i.klass == ME.UNKNOWN}
    assert "account_fee_tier" in {i.name for i in econ().items if i.klass == ME.UNKNOWN}
    for bad in ({"perp": {"taker_bps": 0}, "spot": {"taker_bps": 8}}, {"perp": {"taker_bps": 4}},
                {"perp": {"taker_bps": float("nan")}, "spot": {"taker_bps": 8}}):
        with pytest.raises((ValueError, KeyError)):
            ME.FeeSchedule.from_account(bad)


def test_an_account_read_turns_collateral_from_unknown_to_observed():
    e = econ(account={"available_margin_usd": 5000.0})
    am = next(i for i in e.items if i.name == "available_margin_usd")
    assert am.klass == ME.OBSERVED and am.value == 5000.0 and "available_margin" not in e.missing


def test_owner_supplied_rates_are_never_defaulted_to_zero():
    e = econ(borrow_apr=None, opportunity_cost_apr=None)
    assert {"borrow_apr", "opportunity_cost_apr"} <= set(e.missing) and e.decision is None
    e0 = econ(borrow_apr=0.0, opportunity_cost_apr=0.0)                          # zero stated, not defaulted
    assert "borrow_apr" not in e0.missing and e0.decision is not None


def test_the_verdict_is_economic_sufficiency_and_evidence_is_still_missing():
    thin = econ(funding_history_bps=[1.0, 0.9, 1.2, 0.8, 1.1, 1.0])
    assert thin.decision.net_benefit_bps == pytest.approx(0.3737, abs=0.01)
    assert any(r.startswith("NET_BENEFIT_WITHIN_UNCERTAINTY") for r in thin.decision.reasons)
    rich = econ(funding_history_bps=[6.0, 5.8, 6.1, 5.9, 6.0, 6.2])
    assert rich.decision.net_benefit_bps > 0 and not any(
        r.startswith("NET_BENEFIT") for r in rich.decision.reasons)
    assert any(r.startswith("STRATEGY_EVIDENCE_NOT_ALLOCATE") for r in rich.decision.reasons)
    assert not rich.decision.allowed                       # no evidence verdict: still not an entry


def test_a_negative_funding_history_cannot_pass():
    e = econ(funding_history_bps=[-1.0, -0.8, -1.2, -0.9, -1.1])
    assert e.decision.net_benefit_bps < 0
    assert any(r.startswith("NET_BENEFIT_NOT_POSITIVE") for r in e.decision.reasons)


def test_too_few_prints_means_no_expected_funding_and_no_verdict():
    e = econ(funding_history_bps=[1.0, 1.0])
    assert "expected_funding" in e.missing and e.decision is None
    assert econ(funding_history_bps=[]).decision is None


def test_a_stale_view_has_no_book_so_no_economics():
    st, now, wall = complete_state()
    view = st.view(now + 5.0, wall + 5000)                  # past the book freshness window
    e = econ(view)
    assert not e.complete and e.decision is None
    assert {"perp_bid", "perp_ask"} <= set(e.missing)
    assert e.depth.status == ME.UNAVAILABLE


def test_the_economics_are_deterministic():
    a, b = econ(), econ()
    assert [i.as_dict() for i in a.items] == [i.as_dict() for i in b.items]
    assert a.net_benefit_bps == b.net_benefit_bps


def test_income_uncertainty_matches_the_gates_convention():
    assert ME.income_uncertainty_bps([1.0], 5.8) is None
    xs = [1.0, 3.0]
    sd = math.sqrt(2.0)
    assert ME.income_uncertainty_bps(xs, 5.8) == pytest.approx(sd * 3 * 5.8)
