"""The hedged PAIR's net, break-evens and cost sensitivity.

An overlay hedges the client's own BTC. The live journal books the short's price
leg and never revalues the inventory it hedges, so a cycle's booked net is the
net of an UNHEDGED SHORT: a BTC rally reads as a large loss and a fall as a
large win, neither of which is carry. These tests pin that the report states the
hedged-pair net beside the booked one (additive; the booked net and the verdict
code are unchanged), that every derived number says what it excludes, and that a
missing input is UNAVAILABLE, never zero.

Figures are hand-computed FIXTURES, not observations.
"""
from __future__ import annotations

import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "tools"))

import ledger as L                      # noqa: E402
import carry_performance as perf        # noqa: E402

DAY = 86_400_000
T0 = 1_700_000_000_000
P0 = 100_000.0


def overlay(j, d0, d1, *, tag, funding=300.0, close=P0, revalue=False,
            borrow=0.0):
    L.perp_open(j, ms=T0 + d0 * DAY, ref=f"{tag}-o", qty=1.0, price=P0,
                fee_usd=55.0, fee_status="confirmed")
    L.funding(j, ms=T0 + ((d0 + d1) // 2) * DAY, ref=f"{tag}-f", usd=funding,
              basis="venue")
    if borrow:
        L.borrow(j, ms=T0 + ((d0 + d1) // 2) * DAY + 1, ref=f"{tag}-b",
                 usd=borrow)
    if revalue:
        L.revalue_inventory(j, ms=T0 + d1 * DAY - 1, ref=f"{tag}-rv", qty=1.0,
                            from_price=P0, to_price=close)
    L.perp_close(j, ms=T0 + d1 * DAY, ref=f"{tag}-c", qty=1.0, price=close,
                 entry_price=P0, fee_usd=55.0, fee_status="confirmed")


def acquire(j, d0, d1, *, tag, funding=300.0, close=P0):
    L.spot_buy(j, ms=T0 + d0 * DAY, ref=f"{tag}-sb", qty=1.0, price=P0,
               fee_btc=0.0, fee_usdt=55.0, fee_status="confirmed")
    L.perp_open(j, ms=T0 + d0 * DAY + 1, ref=f"{tag}-o", qty=1.0, price=P0,
                fee_usd=55.0, fee_status="confirmed")
    L.funding(j, ms=T0 + ((d0 + d1) // 2) * DAY, ref=f"{tag}-f", usd=funding,
              basis="venue")
    L.perp_close(j, ms=T0 + d1 * DAY, ref=f"{tag}-c", qty=1.0, price=close,
                 entry_price=P0, fee_usd=55.0, fee_status="confirmed")
    L.spot_sell(j, ms=T0 + d1 * DAY + 1, ref=f"{tag}-ss", qty=1.0, price=close,
                cost_basis=P0, fee_usd=55.0, fee_status="confirmed")


def economics(j, *, obs=(), env="paper"):
    import performance_analytics as pa
    cycles = perf.cycles_from_journal(j)
    perf.attach_capital(cycles, list(obs))
    e = perf.classify_environment(env, {})
    recon = {c["cycle_id"]: {"reconciled": True, "lifecycles": [],
                             "reasons": []} for c in cycles}
    for c in cycles:
        perf._classify_cycle(c, e, recon, [])
    opp = {o["cycle_id"]: o for o in (
        pa.overlay_opportunity_cost(c, benchmark_apr_pct=None)
        for c in cycles if c["closed"] and c["outcome"] == "CLOSED")}
    summ = perf.summarise(cycles)
    return perf.net_economics(cycles, e, journal=j, opp_by_cycle=opp,
                              period_days=summ.get("period_days"),
                              now_ms=T0 + 400 * DAY), cycles


# -- the unbooked inventory leg ----------------------------------------------

def test_a_btc_rally_is_not_a_carry_loss_for_an_overlay():
    j = L.Journal()
    overlay(j, 0, 10, tag="a", close=105_000.0)       # short loses 5000
    ne, cycles = economics(j)
    # unchanged: the BOOKED net is the net of the unhedged short
    assert ne["headline"]["net_usd"] == pytest.approx(300 - 110 - 5000)
    assert ne["verdict"]["code"] == "FUNDING_POSITIVE_BUT_NET_NEGATIVE"
    # new, additive: the pair, with the inventory leg derived and labelled
    m = ne["metrics"]["hedged_pair_net_usd"]
    assert m["value"] == pytest.approx(190.0)
    assert m["evidence_class"] == perf.HISTORICAL_SIMULATION
    assert m["status"] == "DERIVED_NOT_BOOKED"
    assert ne["headline"]["hedged_pair_net_usd"] == pytest.approx(190.0)
    pair = ne["cycles"][0]["components"]["hedged_pair_net"]
    assert pair["value"] == pytest.approx(190.0)
    assert pair["additive"] is False and pair["inventory_leg"] == "DERIVED_FROM_PERP_FILL_PRICES"
    assert pair["inventory_leg_usd"] == pytest.approx(5000.0)
    # the basis residual cannot be measured from perp prices alone
    basis = ne["cycles"][0]["components"]["basis_pnl"]
    assert basis["value"] is None and basis["evidence_class"] == perf.UNAVAILABLE
    assert "basis" in basis["why"].lower()
    # the verdict says the loss is the unbooked hedge leg, with the pair number
    st = ne["verdict"]["statement"]
    assert "never revalued" in st and "190.00" in st
    assert ne["verdict"]["hedged_pair_net_usd"] == pytest.approx(190.0)


def test_a_btc_fall_is_not_a_carry_win_for_an_overlay():
    j = L.Journal()
    overlay(j, 0, 10, tag="a", close=95_000.0)        # short wins 5000
    ne, _ = economics(j)
    assert ne["headline"]["net_usd"] == pytest.approx(300 - 110 + 5000)
    assert ne["metrics"]["hedged_pair_net_usd"]["value"] == pytest.approx(190.0)


def test_a_booked_inventory_revaluation_is_not_added_a_second_time():
    j = L.Journal()
    overlay(j, 0, 10, tag="a", close=105_000.0, revalue=True)
    ne, cycles = economics(j)
    assert cycles[0]["revalue_usd"] == pytest.approx(5000.0)
    # booked net already holds the inventory gain: 300 - 110 - 5000 + 5000
    assert ne["headline"]["net_usd"] == pytest.approx(190.0)
    m = ne["metrics"]["hedged_pair_net_usd"]
    assert m["value"] == pytest.approx(190.0)
    assert m["status"] == "COMPUTED"
    pair = ne["cycles"][0]["components"]["hedged_pair_net"]
    assert pair["inventory_leg"] == "BOOKED"
    # with the inventory booked at its own price the residual IS measured
    assert ne["cycles"][0]["components"]["basis_pnl"]["value"] == pytest.approx(0.0)
    assert "never revalued" not in ne["verdict"]["statement"]


def test_an_acquire_cycle_already_holds_both_legs():
    j = L.Journal()
    acquire(j, 0, 10, tag="a", close=105_000.0)
    ne, cycles = economics(j)
    assert cycles[0]["overlay"] is False
    # funding 300 - four fills at 55 + (perp -5000) + (spot +5000)
    assert ne["headline"]["net_usd"] == pytest.approx(80.0)
    assert ne["metrics"]["hedged_pair_net_usd"]["value"] == pytest.approx(80.0)
    assert ne["cycles"][0]["components"]["hedged_pair_net"]["inventory_leg"] \
        == "SPOT_LEG_BOOKED"


def test_a_cycle_without_readable_perp_prices_gets_no_pair_not_zero():
    j = L.Journal()
    j.post(L.Entry(ms=T0, kind="perp_open", ref="o",
                   lines=[L.Line(L.PERP, 0.0, qty=-1.0), L.Line(L.FEES, 55.0),
                          L.Line(L.USDT, -55.0, qty=-55.0)],
                   memo="not a readable memo", fee_status="confirmed"))
    j.post(L.Entry(ms=T0 + 5 * DAY, kind="perp_close", ref="c",
                   lines=[L.Line(L.PERP, 0.0, qty=1.0), L.Line(L.FEES, 55.0),
                          L.Line(L.USDT, -55.0, qty=-55.0)],
                   memo="not a readable memo", fee_status="confirmed"))
    ne, _ = economics(j)
    m = ne["metrics"]["hedged_pair_net_usd"]
    assert m["value"] is None and m["evidence_class"] == perf.UNAVAILABLE
    assert m["why"]
    for k in ("break_even_funding_bps_per_settlement",
              "min_holding_days_to_cover_costs",
              "execution_cost_sensitivity"):
        assert ne["metrics"][k]["value"] is None, k
        assert ne["metrics"][k]["why"], k


# -- break-evens, edge, efficiency -------------------------------------------

def test_break_even_funding_holding_period_and_required_edge():
    j = L.Journal()
    overlay(j, 0, 10, tag="a", close=105_000.0)       # pair net 190
    ne, _ = economics(j)
    M = ne["metrics"]
    # 30 settlements on a 100k notional: funding 300 = 1.0 bps per settlement
    assert M["observed_funding_bps_per_settlement"]["value"] == pytest.approx(1.0)
    # fees 110 = 11 bps of notional; break-even = 11 / 30 settlements
    be = M["break_even_funding_bps_per_settlement"]
    assert be["value"] == pytest.approx(11.0 / 30.0)
    assert be["basis_pnl_included"] is False
    assert M["funding_margin_over_break_even_bps"]["value"] == pytest.approx(
        1.0 - 11.0 / 30.0)
    # 3 bps/day of funding pays back an 11 bps fixed cost in 11/3 days
    assert M["min_holding_days_to_cover_costs"]["value"] == pytest.approx(11.0 / 3.0)
    # at the observed median hold (10 d) the edge needed per settlement
    assert M["min_required_funding_edge_bps_per_settlement"]["value"] == \
        pytest.approx(11.0 / 30.0)
    # capital efficiency: 190 over 100k x 10 notional-days = 1.9 bps/day
    assert M["net_bps_per_notional_day"]["value"] == pytest.approx(1.9)


def test_funding_that_does_not_cover_financing_has_no_holding_period():
    j = L.Journal()
    overlay(j, 0, 10, tag="a", funding=-20.0)
    ne, _ = economics(j)
    h = ne["metrics"]["min_holding_days_to_cover_costs"]
    assert h["value"] is None and "NEVER" in h["why"]


def test_financing_enters_the_break_even_per_day_not_as_a_fixed_cost():
    j = L.Journal()
    overlay(j, 0, 10, tag="a")
    j2 = L.Journal()
    overlay(j2, 0, 10, tag="a", borrow=100.0)     # same cycle, 100 financing
    base, _ = economics(j)
    fin, _ = economics(j2)
    # break-even funding rises by the financing per settlement: 100/(100k*30)
    d = (fin["metrics"]["break_even_funding_bps_per_settlement"]["value"]
         - base["metrics"]["break_even_funding_bps_per_settlement"]["value"])
    assert d == pytest.approx(100.0 / (P0 * 30) * 1e4)
    # funding net of financing now earns 2 bps/day against the same 11 bps
    assert fin["metrics"]["min_holding_days_to_cover_costs"]["value"] == \
        pytest.approx(11.0 / 2.0)


# -- sensitivity ---------------------------------------------------------------

def test_execution_cost_sensitivity_is_arithmetic_on_stated_shocks():
    j = L.Journal()
    overlay(j, 0, 10, tag="a", close=105_000.0)       # pair net 190, fees 110
    ne, _ = economics(j)
    s = ne["metrics"]["execution_cost_sensitivity"]
    assert s["evidence_class"] == perf.HISTORICAL_SIMULATION
    assert s["status"] == "SENSITIVITY_NOT_MEASUREMENT"
    assert s["base_net_usd"] == pytest.approx(190.0)
    by_bps = {r["extra_round_trip_bps"]: r["net_usd"] for r in s["extra_bps"]}
    assert by_bps[0.0] == pytest.approx(190.0)
    assert by_bps[10.0] == pytest.approx(90.0)
    by_mult = {r["fee_multiple"]: r["net_usd"] for r in s["fee_multiple"]}
    assert by_mult[2.0] == pytest.approx(80.0)
    assert s["extra_round_trip_bps_to_zero_net"] == pytest.approx(19.0)
    assert s["fee_multiple_to_zero_net"] == pytest.approx(1.0 + 190.0 / 110.0)


def test_a_losing_book_has_no_cushion_to_report():
    j = L.Journal()
    overlay(j, 0, 10, tag="a", funding=50.0)          # pair net -60
    ne, _ = economics(j)
    s = ne["metrics"]["execution_cost_sensitivity"]
    assert s["base_net_usd"] == pytest.approx(-60.0)
    assert s["extra_round_trip_bps_to_zero_net"] is None
    assert s["fee_multiple_to_zero_net"] is None


def test_no_closed_cycle_means_every_new_metric_is_unavailable():
    ne, _ = economics(L.Journal())
    for k in ("hedged_pair_net_usd", "break_even_funding_bps_per_settlement",
              "min_holding_days_to_cover_costs",
              "min_required_funding_edge_bps_per_settlement",
              "net_bps_per_notional_day", "execution_cost_sensitivity",
              "observed_funding_bps_per_settlement"):
        assert ne["metrics"][k]["value"] is None, k
        assert ne["metrics"][k]["evidence_class"] == perf.UNAVAILABLE, k
        assert ne["metrics"][k]["why"], k


# -- through the whole report --------------------------------------------------

def test_the_report_carries_the_pair_metrics_under_the_paper_class(tmp_path):
    import json
    from persistence import StateStore
    j = L.Journal()
    overlay(j, 0, 10, tag="a", close=105_000.0)
    db = str(tmp_path / "s.db")
    store = StateStore(db)
    store.save_ledger(j.to_rows())
    store.record_execution_environment("testnet")
    for c in perf.cycles_from_journal(j):
        store.record_risk_observation({
            "ms": perf._cycle_ms(c["cycle_id"]) + 1, "symbol": "BTCUSDT",
            "cycle_opened_ms": perf._cycle_ms(c["cycle_id"]),
            "position_im_usd": 10_000.0})
    store.close()
    r = perf.build_report(db, environment="testnet", now_ms=T0 + 60 * DAY)
    json.dumps(r, allow_nan=False)                       # JSON-safe
    ne = r["sections"]["net_economics"]
    assert ne["evidence_class"] == perf.PAPER_EXECUTION
    assert r["headline"]["hedged_pair_net_usd"] == pytest.approx(190.0)
    assert r["headline"]["net_usd"] == pytest.approx(-4810.0)
    m = ne["metrics"]
    # margin 10k observed: 190 / 10k
    assert m["pair_return_on_posted_margin_pct"]["value"] == pytest.approx(1.9)
    # nothing in it claims cash
    assert r["sections"]["realized_cash"]["evidence_class"] == perf.UNAVAILABLE
