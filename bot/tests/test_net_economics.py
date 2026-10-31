"""Net economics of a carry book: every cost a number with its evidence or
UNAVAILABLE (never silently 0), a plain verdict when funding is positive but
the NET is not, no cost counted twice, and no annualising a tiny sample.

Figures are hand-computed FIXTURES, not observations."""
from __future__ import annotations

import json
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "tools"))

import ledger as L                      # noqa: E402
import carry_performance as perf        # noqa: E402
import performance_analytics as PA      # noqa: E402

DAY = 86_400_000
T0 = 1_700_000_000_000


def cyc(j, d0, d1, *, funding, close=100_000.0, tag, fee_status="confirmed",
        basis="venue", mark=None):
    L.perp_open(j, ms=T0 + d0 * DAY, ref=f"{tag}-o", qty=1.0, price=100_000.0,
                fee_usd=55.0, fee_status=fee_status, decision_mark=mark)
    L.funding(j, ms=T0 + ((d0 + d1) // 2) * DAY, ref=f"{tag}-f", usd=funding,
              basis=basis)
    L.perp_close(j, ms=T0 + d1 * DAY, ref=f"{tag}-c", qty=1.0, price=close,
                 entry_price=100_000.0, fee_usd=55.0, fee_status=fee_status,
                 decision_mark=mark)


def economics(j, *, env="paper", total=None, apr=None, obs=(), now_days=400):
    cycles = perf.cycles_from_journal(j)
    perf.attach_capital(cycles, list(obs))
    e = perf.classify_environment(env, {})
    recon = {c["cycle_id"]: {"reconciled": True, "lifecycles": [],
                             "reasons": []} for c in cycles}
    for c in cycles:
        perf._classify_cycle(c, e, recon, [])
    import performance_analytics as pa
    opp = {o["cycle_id"]: o for o in (
        pa.overlay_opportunity_cost(c, benchmark_apr_pct=apr)
        for c in cycles if c["closed"] and c["outcome"] == "CLOSED")}
    summ = perf.summarise(cycles)
    return perf.net_economics(cycles, e, journal=j, opp_by_cycle=opp,
                              period_days=summ.get("period_days"),
                              total_capital_usd=total, cash_apr_pct=apr,
                              now_ms=T0 + now_days * DAY), cycles


def test_funding_positive_but_net_negative_is_stated_plainly():
    j = L.Journal()
    cyc(j, 0, 10, funding=300.0, close=101_000.0, tag="a")   # +300-110-1000
    ne, _ = economics(j)
    v = ne["verdict"]
    assert v["code"] == "FUNDING_POSITIVE_BUT_NET_NEGATIVE"
    assert v["funding_exceeds_explicit_costs"] is True
    assert v["net_usd"] == pytest.approx(-810.0)
    assert "LOSES money net" in v["statement"]
    assert ne["headline"]["net_usd"] == pytest.approx(-810.0)
    assert ne["headline"]["gross_funding_is_not_the_headline"] == pytest.approx(300.0)
    # the headline is the NET, not the funding
    assert ne["headline"]["basis"] == "NET_AFTER_BOOKED_COSTS"


def test_unconfirmed_fee_booked_as_zero_never_reads_as_profit():
    j = L.Journal()
    cyc(j, 0, 10, funding=300.0, tag="a", fee_status="unknown")
    ne, cycles = economics(j)
    assert ne["verdict"]["code"] == "NET_POSITIVE_UNVERIFIED"
    assert ne["headline"]["complete"] is False
    fees = ne["cycles"][0]["components"]["fees"]
    assert fees["status"] == "INCOMPLETE_LOWER_BOUND"
    s = perf.summarise(cycles)
    assert s["totals_are_bounds"] is True and s["cycles_with_unstated_fees"] == 1


def test_missing_costs_are_unavailable_not_zero():
    j = L.Journal()
    cyc(j, 0, 10, funding=300.0, tag="a")
    ne, _ = economics(j)
    comp = ne["cycles"][0]["components"]
    for name in ("spread", "maker_fee", "taker_fee", "conversion_cost",
                 "partial_execution", "slippage", "capital_reservation"):
        assert comp[name]["value"] is None, name
        assert comp[name]["evidence_class"] == perf.UNAVAILABLE, name
        assert comp[name]["why"], name
    # financing: no borrow booked is labelled, not claimed as verified zero
    assert comp["financing"]["status"] == "NONE_BOOKED"


def test_slippage_is_inside_the_price_leg_and_never_added_twice():
    j = L.Journal()
    cyc(j, 0, 10, funding=300.0, close=100_100.0, tag="a", mark=100_000.0)
    ne, cycles = economics(j)
    cc = ne["cycles"][0]
    sl = cc["components"]["slippage"]
    assert sl["value"] is not None and sl["additive"] is False
    assert sl["embedded_in"] == "price_leg"
    # additive lines reproduce the cycle net exactly; adding slippage would not
    assert cc["additive_components_sum_usd"] == pytest.approx(
        cc["net_after_booked_costs_usd"])
    assert cc["net_after_booked_costs_usd"] - sl["value"] != pytest.approx(
        cc["net_after_booked_costs_usd"])
    flags = {k for k, v in cc["components"].items() if v["additive"]}
    assert flags == set(perf.ADDITIVE_COMPONENTS)
    assert perf.check_net_components(cycles)["ok"]


def test_a_cost_counted_twice_breaks_the_component_invariant():
    j = L.Journal()
    cyc(j, 0, 10, funding=300.0, tag="a")
    cycles = perf.cycles_from_journal(j)
    cycles[0]["fees_usd"] += 25.0          # a fee also booked as slippage
    assert not perf.check_net_components(cycles)["ok"]


def test_funding_received_and_paid_are_split_and_sum_to_net_funding():
    j = L.Journal()
    L.perp_open(j, ms=T0, ref="o", qty=1.0, price=100_000.0, fee_usd=55.0,
                fee_status="confirmed")
    L.funding(j, ms=T0 + DAY, ref="f1", usd=40.0, basis="venue")
    L.funding(j, ms=T0 + 2 * DAY, ref="f2", usd=-15.0, basis="venue")
    L.perp_close(j, ms=T0 + 3 * DAY, ref="c", qty=1.0, price=100_000.0,
                 entry_price=100_000.0, fee_usd=55.0, fee_status="confirmed")
    c = perf.cycles_from_journal(j)[0]
    assert c["funding_received_usd"] == pytest.approx(40.0)
    assert c["funding_paid_usd"] == pytest.approx(15.0)
    assert perf.check_net_components([c])["ok"]


def test_annualising_a_tiny_sample_is_refused():
    j = L.Journal()
    cyc(j, 0, 10, funding=300.0, tag="a")
    ne, _ = economics(j, obs=[{"cycle_opened_ms": T0, "position_im_usd": 10_000.0}])
    m = ne["metrics"]
    assert m["annualised_return_on_committed_capital_pct"]["value"] is None
    assert "SAMPLE_TOO_SMALL" in m["annualised_return_on_committed_capital_pct"]["why"]
    assert m["net_return_per_capital_day_pct"]["value"] is not None
    assert ne["verdict"]["code"] == "NET_POSITIVE_SMALL_SAMPLE"


def test_annualised_when_sample_and_duration_rule_met():
    assert PA.annualise_simple(1.0, 10.0, 8)[0] is None            # duration
    assert PA.annualise_simple(1.0, 60.0, 4)[0] is None            # cycles
    assert PA.annualise_simple(1.0, 60.0, 5)[0] == pytest.approx(365.0 / 60.0)
    assert PA.annualise_simple(None, 60.0, 9)[0] is None


def test_distribution_metrics_and_largest_winner_exclusion():
    j = L.Journal()
    cyc(j, 0, 10, funding=300.0, tag="a")                 # +190
    cyc(j, 12, 14, funding=-20.0, close=101_000.0, tag="b")  # -1130
    cyc(j, 20, 40, funding=600.0, tag="c")                # +490
    ne, _ = economics(j)
    m = ne["metrics"]
    assert m["net_closed_cycles_usd"]["value"] == pytest.approx(-450.0)
    assert m["net_excluding_largest_winner_usd"]["value"] == pytest.approx(-940.0)
    assert m["profit_concentration"]["value"] == pytest.approx(100 * 490 / 680)
    assert m["max_drawdown_usd"]["value"] == pytest.approx(1130.0)
    assert m["downside_deviation_usd"]["value"] == pytest.approx(
        (1130.0 ** 2 / 3) ** 0.5)
    assert m["expectancy_after_costs_usd"]["value"] == pytest.approx(-150.0)
    assert m["funding_received_usd"]["value"] == pytest.approx(900.0)
    assert m["funding_paid_usd"]["value"] == pytest.approx(20.0)
    for dim in ("regime", "symbol", "venue"):
        assert m[f"performance_by_{dim}"]["value"] is None
        assert m[f"performance_by_{dim}"]["evidence_class"] == perf.UNAVAILABLE


def test_positive_net_that_needs_its_best_cycle_is_flagged():
    j = L.Journal()
    cyc(j, 0, 10, funding=1000.0, tag="a")                # +890
    cyc(j, 12, 14, funding=-20.0, close=100_500.0, tag="b")  # -630
    ne, _ = economics(j)
    assert ne["verdict"]["code"] == "NET_POSITIVE_DEPENDS_ON_LARGEST_WINNER"


def test_capital_metrics_need_observed_capital_and_operator_total():
    j = L.Journal()
    cyc(j, 0, 10, funding=300.0, tag="a")
    ne, _ = economics(j)
    for k in ("return_on_committed_capital_pct", "return_on_total_capital_pct",
              "net_return_per_capital_day_pct", "capital_utilization_pct",
              "capital_occupation_usd_days"):
        assert ne["metrics"][k]["value"] is None, k
        assert ne["metrics"][k]["why"]
    obs = [{"cycle_opened_ms": T0, "position_im_usd": 10_000.0}]
    ne, _ = economics(j, obs=obs, total=500_000.0)
    m = ne["metrics"]
    cap = 110_000.0          # margin + hedged inventory at entry
    assert m["capital_occupation_usd_days"]["value"] == pytest.approx(cap * 10)
    assert m["return_on_committed_capital_pct"]["value"] == pytest.approx(
        100 * 190 / cap)
    assert m["net_return_per_capital_day_pct"]["value"] == pytest.approx(
        100 * 190 / (cap * 10))
    assert m["return_on_total_capital_pct"]["value"] == pytest.approx(
        100 * 190 / 500_000.0)
    assert m["capital_utilization_pct"]["value"] == pytest.approx(
        100 * cap * 10 / (500_000.0 * 10))


def test_open_position_costs_are_visible_not_dropped():
    j = L.Journal()
    cyc(j, 0, 10, funding=300.0, tag="a")
    L.perp_open(j, ms=T0 + 12 * DAY, ref="b-o", qty=1.0, price=100_000.0,
                fee_usd=55.0, fee_status="confirmed")
    ne, cycles = economics(j)
    m = ne["metrics"]
    assert m["net_open_positions_booked_usd"]["value"] == pytest.approx(-55.0)
    assert m["net_open_positions_booked_usd"]["evidence_class"] == \
        perf.OPEN_MARK_TO_MARKET
    assert m["net_open_positions_booked_usd"]["mark_to_market_usd"][
        "value"] is None
    assert m["net_total_including_open_booked_usd"]["value"] == pytest.approx(135.0)
    assert perf.summarise(cycles)["open_cycle_booked_costs_usd"] == pytest.approx(55.0)


def test_benchmarks_only_when_supplied_and_reliable():
    j = L.Journal()
    cyc(j, 0, 10, funding=300.0, tag="a")
    obs = [{"cycle_opened_ms": T0, "position_im_usd": 10_000.0}]
    ne, _ = economics(j, obs=obs)
    assert ne["metrics"]["vs_benchmark_rate"]["value"] is None
    ne, _ = economics(j, obs=obs, apr=5.0)
    b = ne["metrics"]["vs_benchmark_rate"]
    assert b["value"] == pytest.approx(110_000.0 * 0.05 * 10 / 365)
    assert b["evidence_class"] == perf.HISTORICAL_SIMULATION
    assert ne["metrics"]["vs_unhedged_btc_hold"]["value"] == pytest.approx(0.0)


def test_net_positive_below_benchmark_is_not_called_profit():
    j = L.Journal()
    cyc(j, 0, 10, funding=300.0, tag="a")          # net +190 on 110k
    for i in range(1, 6):
        cyc(j, 10 + 11 * i, 20 + 11 * i, funding=300.0, tag=f"x{i}")
    obs = [{"cycle_opened_ms": T0 + 0, "position_im_usd": 10_000.0}]
    cycles = perf.cycles_from_journal(j)
    obs = [{"cycle_opened_ms": perf._cycle_ms(c["cycle_id"]),
            "position_im_usd": 10_000.0} for c in cycles]
    ne, _ = economics(j, obs=obs, apr=50.0)
    assert ne["verdict"]["code"] == "NET_POSITIVE_BELOW_BENCHMARK_RATE"


def test_report_carries_a_top_level_verdict_and_stays_valid(tmp_path, capsys):
    from persistence import StateStore
    j = L.Journal()
    cyc(j, 0, 10, funding=300.0, close=101_000.0, tag="a")
    db = str(tmp_path / "s.db")
    st = StateStore(db)
    st.save_ledger(j.to_rows())
    st.close()
    r = perf.build_report(db, environment="paper", now_ms=T0 + 30 * DAY)
    assert r["net_economics_verdict"]["code"] == "FUNDING_POSITIVE_BUT_NET_NEGATIVE"
    assert r["headline"]["basis"] == "NET_AFTER_BOOKED_COSTS"
    assert any(i["name"] == "net_components_sum_to_cycle_net" and i["ok"]
               for i in r["invariants"])
    sec = r["sections"]["net_economics"]
    assert sec["evidence_class"] == perf.PAPER_EXECUTION
    assert sec["metrics"]["reservation_duration"]["value"] is None
    json.dumps(r, default=str, allow_nan=False)
    perf.validate_report(r)
    assert perf.main(["--db", db, "--environment", "paper"]) == 0
    assert "NET ECONOMICS [FUNDING_POSITIVE_BUT_NET_NEGATIVE]" in capsys.readouterr().out
