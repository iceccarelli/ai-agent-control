"""Overlay economics: what it measures, what it refuses, and the one verdict.

Every observation here is synthetic. Nothing reads the network, an account, a
runtime directory or a credential."""
from __future__ import annotations

import ast
import json
import math
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
BOT = os.path.dirname(HERE)
for p in (BOT, os.path.join(BOT, "tools"), HERE):
    if p not in sys.path:
        sys.path.insert(0, p)

import mainnet_public as mp                                     # noqa: E402
import overlay_economics as oe                                  # noqa: E402

H = 3600
START = oe.EVALUATION_START_S
PRINT_S = 8 * H


def book(i10=0.3, i100=2.0, spread=0.1, buy10=None, sell10=None):
    return {"best_bid": 99_999.0, "best_ask": 100_001.0, "mid": 100_000.0,
            "spread_bps": spread, "levels": {"bids": 50, "asks": 50},
            "impact_bps": {"10000": {"buy": i10 if buy10 is None else buy10,
                                     "sell": i10 if sell10 is None else sell10},
                           "100000": {"buy": i100, "sell": i100}}}


def _hashed(rows):
    """Give synthetic rows a chain-style hash (the real reader supplies one)."""
    import hashlib
    for r in rows:
        r["hash"] = hashlib.sha256(
            json.dumps(r["obs"], sort_keys=True, default=str).encode()).hexdigest()
    return rows


def series(print_bps, *, t0=START + 1, hours=None, spot=100_000.0,
           perp_off=0.0, spot_fn=None, perp_fn=None, bk=None):
    """Hourly observations; one funding print every 8 hours."""
    n_prints = len(print_bps)
    hours = hours if hours is not None else n_prints * 8
    rows = []
    for h in range(hours + 1):
        t = t0 + h * H
        k = (h // 8)                       # prints settled so far
        settled = [[print_bps[m], int((t0 + (m + 1) * PRINT_S - 1) * 1000)]
                   for m in range(min(k, n_prints))][-8:]
        sp = spot_fn(h) if spot_fn else spot
        pp = perp_fn(h) if perp_fn else sp + perp_off
        rows.append({"obs": {
            "origin": mp.ORIGIN, "venue": mp.VENUE, "observed_at_s": float(t),
            "perp_mark": pp, "spot_mark": sp,
            "funding_bps": print_bps[min(k, n_prints) - 1] if k else 0.0,
            "funding_print_bps": settled[-1][0] if settled else 0.0,
            "funding_print_ms": settled[-1][1] if settled else 0,
            "settled_prints": settled,
            "book": {"linear": bk or book(), "spot": bk or book()}}})
    return _hashed(rows)


# -- execution cost: measured sizes, bounded between, nothing beyond ------------

def test_cost_is_exact_at_the_measured_sizes():
    b = book(i10=0.3, i100=2.0)
    r = oe.leg_impact_bounds(b, "buy", 10_000)
    assert r == {"status": "MEASURED", "lower_bps": 0.3, "upper_bps": 0.3}
    assert oe.leg_impact_bounds(b, "sell", 100_000)["upper_bps"] == 2.0


def test_cost_below_the_smallest_size_is_bounded_by_half_the_spread():
    r = oe.leg_impact_bounds(book(i10=0.3, spread=0.2), "buy", 100)
    assert r["status"] == "BOUNDED"
    assert r["lower_bps"] == pytest.approx(0.1) and r["upper_bps"] == 0.3


def test_cost_between_measured_sizes_is_bracketed_by_them():
    r = oe.leg_impact_bounds(book(i10=0.3, i100=2.0), "buy", 50_000)
    assert (r["status"], r["lower_bps"], r["upper_bps"]) == ("BOUNDED", 0.3, 2.0)


def test_cost_above_the_largest_measured_size_is_unavailable_not_extrapolated():
    r = oe.leg_impact_bounds(book(), "buy", 100_001)
    assert r["status"] == oe.UNAVAILABLE
    assert r["reason"] == "ABOVE_LARGEST_MEASURED_SIZE"
    assert "upper_bps" not in r


@pytest.mark.parametrize("bad", [0, -5, float("nan"), float("inf"), None, "x"])
def test_a_nonsense_notional_is_unavailable(bad):
    assert oe.leg_impact_bounds(book(), "buy", bad)["status"] == oe.UNAVAILABLE


def test_a_book_too_thin_to_price_a_size_is_unavailable():
    b = book(buy10=None)
    b["impact_bps"]["10000"]["buy"] = None
    assert oe.leg_impact_bounds(b, "buy", 10_000)["status"] == oe.UNAVAILABLE
    assert oe.leg_impact_bounds(b, "buy", 100)["status"] == oe.UNAVAILABLE


def test_a_missing_book_is_unavailable():
    assert oe.leg_impact_bounds(None, "buy", 100)["status"] == oe.UNAVAILABLE
    assert oe.leg_impact_bounds({}, "buy", 100)["status"] == oe.UNAVAILABLE


def test_the_overlay_round_trip_is_two_perp_legs_and_no_spot():
    rows = series([2.0] * 3)
    c = oe.overlay_cost_bounds(rows[0]["obs"], rows[-1]["obs"], 100)
    assert (c["legs"], c["spot_legs"]) == (2, 0)
    assert c["fees_bps"] == pytest.approx(2 * oe.TAKER_FEE_BPS) == 11.0
    assert c["total_upper_bps"] == pytest.approx(11.0 + 0.6)
    assert c["total_lower_bps"] == pytest.approx(11.0 + 0.1)   # 2 * half-spread


def test_one_unavailable_leg_makes_the_round_trip_unavailable():
    rows = series([2.0] * 3)
    rows[0]["obs"]["book"]["linear"] = None
    assert oe.overlay_cost_bounds(rows[0]["obs"], rows[-1]["obs"],
                                  100)["status"] == oe.UNAVAILABLE


def test_the_overlay_cost_is_below_the_four_leg_acquire_structure():
    """The frozen report prices four legs; an overlay trades two."""
    rows = series([2.0] * 3, bk=book(i10=0.5, i100=3.0))
    c = oe.overlay_cost_bounds(rows[0]["obs"], rows[-1]["obs"], 10_000)
    four_leg = 4 * 0.5 + 2 * 2 * oe.TAKER_FEE_BPS       # same impact, two more legs
    assert c["total_upper_bps"] < four_leg


# -- one episode: the owner's BTC is the benchmark -------------------------------

def episode(spot_move=0.0, basis_move=0.0, prints=(4.0, 4.0, 4.0)):
    n = len(prints)
    rows = series(list(prints), hours=n * 8,
                  spot_fn=lambda h: 100_000.0 * (1 + spot_move * h / (n * 8)),
                  perp_fn=lambda h: 100_000.0 * (1 + spot_move * h / (n * 8))
                  + basis_move * 100_000.0 * h / (n * 8))
    return oe.episode_result(rows, 0, len(rows) - 1, 1_000.0)


def test_the_btc_leg_is_identical_for_overlay_and_passive_and_not_added_twice():
    e = episode(spot_move=0.10)
    assert e["overlay_btc_pnl_usd"] == e["passive_btc_pnl_usd"]
    assert e["passive_btc_pnl_usd"] == pytest.approx(100.0)
    # increment = perp + funding - cost; the BTC leg cancels
    assert e["incremental_vs_passive_usd"] == pytest.approx(
        e["perp_pnl_marks_usd"] + e["funding_usd"]
        - e["cost"]["total_upper_bps"] * 1_000.0 / 1e4)
    assert e["overlay_total_usd"] - e["passive_btc_pnl_usd"] == pytest.approx(
        e["incremental_vs_passive_usd"])


def test_a_rally_with_constant_basis_changes_the_directional_part_not_the_carry():
    # no funding, so the only thing that differs is the price path
    flat = episode(spot_move=0.0, prints=(0.0, 0.0, 0.0))
    rally = episode(spot_move=0.25, prints=(0.0, 0.0, 0.0))
    assert rally["directional_component_usd"] == pytest.approx(-250.0)
    assert rally["carry_usd"] == pytest.approx(flat["carry_usd"], abs=1e-6)
    assert rally["incremental_vs_passive_usd"] < flat["incremental_vs_passive_usd"]


def test_a_basis_blow_out_is_carry_loss():
    flat = episode(prints=(0.0, 0.0, 0.0))
    wide = episode(basis_move=0.01, prints=(0.0, 0.0, 0.0))
    assert wide["carry_usd"] == pytest.approx(flat["carry_usd"] - 10.0, abs=1e-6)


def test_funding_is_counted_once_per_print_not_once_per_observation():
    e = episode(prints=(4.0, 4.0, 4.0))
    qty = 1_000.0 / 100_000.0
    assert e["settlements_held"] == 3
    assert e["funding_usd"] == pytest.approx(3 * 4.0 / 1e4 * qty * 100_000.0)


def test_a_negative_print_is_paid_not_received():
    e = episode(prints=(4.0, -2.0, -2.0))
    assert e["funding_usd"] == pytest.approx(0.0, abs=1e-9)


def test_an_unpriceable_episode_has_no_carry_number():
    rows = series([4.0] * 3)
    rows[-1]["obs"]["book"]["linear"] = None
    e = oe.episode_result(rows, 0, len(rows) - 1, 100.0)
    assert e["carry_usd"] is None and e["incremental_vs_passive_usd"] is None


def test_nothing_in_an_episode_is_cash():
    e = episode()
    assert e["cash"] is False and e["class"] == oe.SIMULATED


# -- capital: unset is not zero --------------------------------------------------

def test_an_unset_capital_cost_is_insufficient_owner_input_with_a_sensitivity():
    m = oe.capital_model(None, [10.0, 12.0, 8.0], [3.0, 3.0, 3.0])
    assert m["status"] == oe.INSUFFICIENT_OWNER_INPUT
    assert m["applied_apr"] is None and m["result"] is None
    assert set(m["sensitivity"]) == {"0.00", "0.02", "0.05", "0.10"}


def test_an_explicit_zero_is_a_supplied_value_and_applies_nothing():
    z = oe.capital_model(0.0, [10.0, 12.0], [3.0, 3.0], 0.5)
    assert z["status"] == "OWNER_SUPPLIED" and z["applied_apr"] == 0.0
    assert z["result"]["mean"] == pytest.approx(11.0)


def test_a_positive_capital_cost_is_charged_by_days_held():
    m = oe.capital_model(0.10, [10.0], [36.5], 1.0)
    assert m["result"]["mean"] == pytest.approx(10.0 - 0.10 * 36.5 / 365 * 1e4)


@pytest.mark.parametrize("bad", [-0.01, float("nan"), "x", True, 11.0])
def test_a_bad_capital_cost_is_invalid_not_zero(bad):
    assert oe.capital_model(bad, [1.0], [1.0], 1.0)["status"] == "INVALID_OWNER_INPUT"


@pytest.mark.parametrize("bad", [-0.1, 1.5, float("nan"), "x"])
def test_a_bad_committed_fraction_is_invalid(bad):
    assert oe.capital_model(0.05, [1.0], [1.0], bad)["status"] == "INVALID_OWNER_INPUT"


def test_a_missing_committed_fraction_is_insufficient_owner_input_not_the_full_notional():
    """The owner's BTC is held with or without the overlay: charging its
    opportunity cost again would count the passive benchmark twice."""
    m = oe.capital_model(0.05, [10.0, 12.0], [3.0, 3.0], None)
    assert m["status"] == oe.INSUFFICIENT_OWNER_INPUT and m["result"] is None
    assert "committed_capital_fraction" in m["needs"]


def test_capital_cost_is_charged_on_the_committed_part_only():
    full = oe.capital_model(0.10, [100.0] * 3, [30.0] * 3, 1.0)["result"]["mean"]
    tenth = oe.capital_model(0.10, [100.0] * 3, [30.0] * 3, 0.1)["result"]["mean"]
    none = oe.capital_model(0.10, [100.0] * 3, [30.0] * 3, 0.0)["result"]["mean"]
    assert none == pytest.approx(100.0)
    assert (100.0 - tenth) == pytest.approx(0.1 * (100.0 - full))


def test_break_even_capital_cost_is_where_the_lower_bound_reaches_zero():
    c = [100.0, 110.0, 90.0, 105.0, 95.0] * 8
    m = oe.capital_model(0.0, c, [30.0] * 40, 1.0)
    be = oe.capital_model(0.0, c, [30.0] * 40, 1.0)["break_even_capital_apr"]
    assert be is not None and be > 0
    just_below = oe.capital_model(be * 0.99, c, [30.0] * 40, 1.0)["result"]["lower95"]
    just_above = oe.capital_model(be * 1.01, c, [30.0] * 40, 1.0)["result"]["lower95"]
    assert just_below > 0 >= just_above
    neg = oe.capital_model(0.0, [-5.0, -4.0, -6.0] * 5, [3.0] * 15, 1.0)
    assert neg["break_even_capital_apr"] == 0.0


# -- statistics -------------------------------------------------------------------

def test_bounds_need_two_values_and_widen_for_small_samples():
    assert oe.mean_bounds([1.0])["lower95"] is None
    small = oe.mean_bounds([1.0, 2.0, 3.0])
    big = oe.mean_bounds([1.0, 2.0, 3.0] * 20)
    assert (small["upper95"] - small["mean"]) > (big["upper95"] - big["mean"])
    assert oe.mean_bounds([float("nan"), 1.0, 3.0])["n"] == 2


# -- stress and capacity: unknowns stay unknown ------------------------------------

def test_stress_names_every_input_it_does_not_have():
    s = oe.stress_table(100.0)
    assert s["perp_loss_usd_by_rally"]["100%"] == 100.0
    for k in ("funding_inversion_cost_3_prints_usd", "gap_exit_slippage_bound_usd",
              "liquidation_rally_pct_usdt_margin_only",
              "liquidation_rally_pct_btc_collateral"):
        assert s[k]["status"] == oe.UNAVAILABLE and s[k]["needs"]


def test_stress_with_inputs_gives_labelled_numbers():
    s = oe.stress_table(100.0, leverage=1.0, maintenance_margin_rate=0.0033,
                        collateral_ratio=0.85, funding_cap_bps=33.3,
                        price_limit_ratio=0.02)
    assert s["funding_inversion_cost_3_prints_usd"]["usd"] == pytest.approx(0.999)
    assert s["gap_exit_slippage_bound_usd"]["usd"] == pytest.approx(2.0)
    assert s["liquidation_rally_pct_usdt_margin_only"]["pct"] == pytest.approx(99.34, abs=0.05)
    assert s["liquidation_rally_pct_usdt_margin_only"]["status"] == "ILLUSTRATIVE"
    assert s["liquidation_rally_pct_btc_collateral"]["pct"] == pytest.approx(552, abs=2)


def test_capacity_is_bracketed_and_never_extrapolated():
    rows = series([4.0] * 3, bk=book(i10=0.5, i100=4.0))
    cap = oe.capacity_bounds(rows, impact_budget_bps=1.0, planned_notional_usd=5_000)
    assert cap["status"] == "BRACKETED"
    assert cap["supported_notional_lower_usd"] == 10_000.0 and \
        cap["supported_notional_upper_usd"] == 100_000.0
    assert cap["above_largest_measured_usd"] == oe.UNAVAILABLE
    assert cap["planned_inside_measured_capacity"] is True


def test_capacity_with_every_size_inside_budget_has_no_upper_bound():
    rows = series([4.0] * 3, bk=book(i10=0.5, i100=0.9))
    cap = oe.capacity_bounds(rows, impact_budget_bps=1.0, planned_notional_usd=50_000)
    assert cap["status"] == "LOWER_BOUND_ONLY"
    assert cap["supported_notional_upper_usd"] is None
    assert cap["supported_notional_lower_usd"] == 100_000.0
    assert cap["planned_inside_measured_capacity"] is True
    # a size above everything measured is neither approved nor refused
    assert oe.capacity_bounds(rows, 1.0, 150_000)["planned_inside_measured_capacity"] is None


def test_an_unpriced_side_counts_against_a_size():
    rows = series([4.0] * 3, bk=book(i10=0.5, i100=0.9))
    rows[0]["obs"]["book"]["linear"]["impact_bps"]["100000"]["buy"] = None
    cap = oe.capacity_bounds(rows, impact_budget_bps=1.0, planned_notional_usd=50_000)
    assert cap["by_size"]["100000"]["within_budget"] is False


def test_capacity_without_any_measurement_is_unavailable():
    assert oe.capacity_bounds([], 1.0, 100.0)["status"] == oe.UNAVAILABLE


# -- the frozen strategy, replayed ---------------------------------------------------

def cyc(pos, k_pos=3):
    """k_pos positive prints then three negative ones, repeated."""
    return ([pos] * k_pos + [-1.0] * 3)


def test_the_replay_enters_on_the_gate_and_exits_on_three_negative_prints():
    rows = series(cyc(6.0, 5) * 2, bk=book(i10=0.3, i100=2.0))
    sim = oe.simulate_episodes(rows, 100.0)
    assert len(sim["closed"]) >= 1
    for e in sim["closed"]:
        assert e["settlements_held"] >= 3
        assert (e["cost"]["legs"], e["cost"]["spot_legs"]) == (2, 0)


def test_an_episode_still_open_at_the_end_is_reported_and_not_counted():
    rows = series([6.0] * 12)
    sim = oe.simulate_episodes(rows, 100.0)
    assert sim["closed"] == [] and sim["open_at_end"] is True


# -- forward-only, independence, and the verdict -------------------------------------

def test_184_hourly_observations_are_about_23_independent_windows():
    rows = series([2.0] * 23, hours=183)
    assert len(rows) == 184
    rep = oe.evaluate(rows, planned_notional_usd=100.0)
    assert rep["observations_forward"] == 184
    assert rep["distinct_settled_prints"] <= 23
    assert rep["verdict"] == "INSUFFICIENT"
    assert any("DISTINCT_PRINTS" in r for r in rep["verdict_reasons"])
    assert "not counts of independent" in rep["independence_note"]


def test_rows_before_the_declaration_cannot_decide_anything():
    rows = series(cyc(6.0, 5) * 40, t0=START - 400 * 86400)
    rep = oe.evaluate(rows, planned_notional_usd=100.0, owner_capital_apr=0.0,
                      owner_drawdown_limit_usd=20.0)
    assert rep["observations_forward"] == 0
    assert rep["observations_before_declaration_excluded"] == len(rows)
    assert rep["verdict"] == "INSUFFICIENT"


def test_a_rich_adequate_forward_sample_is_go_only_with_every_owner_input():
    rows = series(cyc(30.0, 5) * 40, bk=book(i10=0.3, i100=2.0))
    kw = dict(planned_notional_usd=100.0)
    base = oe.evaluate(rows, **kw)
    assert base["episodes"]["priced"] >= 30 and base["span_days"] >= 60
    assert base["verdict"] == "INSUFFICIENT"            # no owner capital cost
    assert base["verdict_reasons"] == [oe.INSUFFICIENT_OWNER_INPUT]
    no_dd = oe.evaluate(rows, owner_capital_apr=0.0,
                        committed_capital_fraction=0.1, **kw)
    assert no_dd["verdict"] == "INSUFFICIENT"
    assert "DRAWDOWN" in no_dd["verdict_reasons"][0]
    no_margin = oe.evaluate(rows, owner_capital_apr=0.0,
                            committed_capital_fraction=0.1,
                            owner_drawdown_limit_usd=20.0, **kw)
    assert no_margin["verdict_reasons"] == [oe.INSUFFICIENT_OWNER_INPUT + ":MARGIN_FACTS"]
    go = oe.evaluate(rows, owner_capital_apr=0.0, committed_capital_fraction=0.1,
                     owner_drawdown_limit_usd=20.0,
                     maintenance_margin_rate=0.0033, collateral_ratio=0.85, **kw)
    assert go["verdict"] == "GO", go["verdict_reasons"]
    assert go["cash"] is False and go["class"] == oe.SIMULATED


def test_a_high_capital_cost_turns_a_marginal_go_into_insufficient():
    rows = series(cyc(30.0, 5) * 40, bk=book(i10=0.3, i100=2.0))
    rep = oe.evaluate(rows, planned_notional_usd=100.0, owner_capital_apr=5.0,
                      committed_capital_fraction=1.0,
                      owner_drawdown_limit_usd=20.0, maintenance_margin_rate=0.0033,
                      collateral_ratio=0.85)
    assert rep["verdict"] == "INSUFFICIENT"
    assert "AFTER_CAPITAL_COST" in rep["verdict_reasons"][0]


def test_an_adequate_sample_that_cannot_cover_costs_is_no_go():
    rows = series(cyc(1.0, 5) * 40, bk=book(i10=0.3, i100=2.0))
    rep = oe.evaluate(rows, planned_notional_usd=100.0, owner_capital_apr=0.0,
                      owner_drawdown_limit_usd=20.0)
    assert rep["episodes"]["priced"] >= 30
    assert rep["verdict"] == "NO_GO", rep["verdict_reasons"]


def test_a_size_above_the_measured_books_cannot_be_priced_or_approved():
    rows = series(cyc(30.0, 5) * 40, bk=book())
    rep = oe.evaluate(rows, planned_notional_usd=500_000.0, owner_capital_apr=0.0,
                      owner_drawdown_limit_usd=20.0)
    assert rep["episodes"]["priced"] == 0
    assert rep["verdict"] == "INSUFFICIENT"


def test_unreadable_evidence_is_insufficient_whatever_the_rows_say():
    rows = series(cyc(30.0, 5) * 40)
    rep = oe.evaluate(rows, planned_notional_usd=100.0, owner_capital_apr=0.0,
                      owner_drawdown_limit_usd=20.0, chain_issues=["line 3: broken"])
    assert (rep["verdict"], rep["verdict_reasons"]) == (
        "INSUFFICIENT", ["EVIDENCE_UNREADABLE"])


def test_there_are_exactly_three_verdicts_and_each_has_a_meaning():
    assert oe.PROTOCOL["verdicts"] == ["GO", "NO_GO", "INSUFFICIENT"]
    assert set(oe.PROTOCOL["meaning"]) == {"GO", "NO_GO", "INSUFFICIENT"}
    assert oe.PROTOCOL["cash"] is False


def test_the_report_reads_a_real_evidence_file_and_flags_a_broken_chain(tmp_path):
    db = str(tmp_path / "m.jsonl")
    for r in series([2.0] * 3, hours=4):
        mp.append(db, r["obs"])
    rep = oe.report(db, planned_notional_usd=100.0)
    assert rep["chain_issues"] == [] and rep["observations_total"] == 5
    lines = open(db).read().splitlines()
    lines[2] = lines[2].replace('"perp_mark":100000.0', '"perp_mark":100001.0')
    open(db, "w").write("\n".join(lines) + "\n")
    bad = oe.report(db, planned_notional_usd=100.0)
    assert bad["chain_issues"] and bad["verdict"] == "INSUFFICIENT"


# -- the frozen v1 protocol is untouched, and this module cannot trade -----------------

def test_the_frozen_v1_protocol_hash_is_unchanged():
    import test_mainnet_protocol as tmp
    assert mp.protocol_hash() == tmp.DECLARED_HASH


def test_the_v2_protocol_hash_is_pinned():
    assert oe.protocol_hash() == PINNED


def test_this_module_imports_nothing_that_can_place_an_order():
    tree = ast.parse(open(os.path.join(BOT, "tools", "overlay_economics.py")).read())
    mods = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            mods |= {a.name.split(".")[0] for a in n.names}
        elif isinstance(n, ast.ImportFrom):
            mods.add((n.module or "").split(".")[0])
    assert mods <= {"__future__", "argparse", "hashlib", "json", "math", "os",
                    "sys", "typing", "random", "carry_costs", "mainnet_public"}


# -- review findings: each one a failing case before it was fixed -------------------

GO_KW = dict(planned_notional_usd=100.0, owner_capital_apr=0.0,
             committed_capital_fraction=0.1,
             owner_drawdown_limit_usd=20.0, maintenance_margin_rate=0.0033,
             collateral_ratio=0.85)


def rich(**kw):
    return series(cyc(30.0, 5) * 40, bk=kw.pop("bk", book(i10=0.3, i100=2.0)), **kw)


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -5.0, 0.0, "20"])
def test_a_malformed_drawdown_limit_never_reaches_go(bad):
    kw = dict(GO_KW, owner_drawdown_limit_usd=bad)
    rep = oe.evaluate(rich(), **kw)
    assert rep["verdict"] == "INSUFFICIENT"
    assert "DRAWDOWN_LIMIT" in rep["verdict_reasons"][0]


def test_a_drawdown_limit_below_the_worst_path_blocks_go():
    rep = oe.evaluate(rich(), **dict(GO_KW, owner_drawdown_limit_usd=0.01))
    assert rep["verdict_reasons"] == ["WORST_EPISODE_EXCEEDS_DRAWDOWN_LIMIT"]
    assert rep["worst_episode_usd"] < -0.01


def test_the_worst_episode_is_the_lowest_point_of_the_path_not_its_end():
    """Basis spikes against the short mid-episode and recovers before exit."""
    spike = lambda h: 100_000.0 + (500.0 if 8 <= h <= 12 else 0.0)
    rows = series([4.0] * 3, hours=24, perp_fn=spike)
    e = oe.episode_result(rows, 0, len(rows) - 1, 1_000.0)
    assert e["carry_usd"] > e["worst_path_usd"]
    assert e["worst_path_usd"] < -4.0            # 500 * 0.01 BTC = 5 USD, plus cost


def test_a_missing_margin_fact_or_a_near_liquidation_cannot_be_go():
    miss = oe.evaluate(rich(), **dict(GO_KW, collateral_ratio=None))
    assert miss["verdict_reasons"] == [oe.INSUFFICIENT_OWNER_INPUT + ":MARGIN_FACTS"]
    near = oe.evaluate(rich(), **dict(GO_KW, collateral_ratio=0.10))
    assert near["verdict_reasons"] == ["LIQUIDATION_DISTANCE_BELOW_ENGINE_FLOOR"]


def test_the_liquidation_floor_is_the_engines_own():
    import carry_engine
    assert oe.PROTOCOL["min_liquidation_distance_pct"] == \
        carry_engine.MIN_LIQUIDATION_DISTANCE_PCT


def test_a_nan_funding_print_makes_an_episode_unpriced_not_silently_dropped():
    rows = rich()
    # poison one settled print everywhere it appears
    target = rows[300]["obs"]["settled_prints"][-1][1]
    for r in rows:
        for pr in r["obs"]["settled_prints"]:
            if pr[1] == target:
                pr[0] = float("nan")
    rep = oe.evaluate(rows, **GO_KW)
    ep = rep["episodes"]
    assert ep["priced"] + ep["unpriced_unavailable"] == ep["closed"]
    assert rep["carry_bps_per_episode"]["n"] == ep["priced"]       # same sample


def test_an_episode_across_an_observation_gap_is_not_priced():
    rows = rich()
    ep_before = oe.evaluate(rows, **GO_KW)["episodes"]
    gap = 200 * 3600
    for r in rows[100:]:
        r["obs"]["observed_at_s"] += gap
    after = oe.evaluate(rows, **GO_KW)["episodes"]
    assert after["unpriced_unavailable"] >= 1
    assert after["unpriced_unavailable"] > ep_before["unpriced_unavailable"]


def test_too_many_unpriceable_episodes_make_the_sample_insufficient():
    rows = rich()
    for r in rows[::7]:
        r["obs"]["book"]["linear"] = None
    rep = oe.evaluate(rows, **GO_KW)
    assert rep["verdict"] == "INSUFFICIENT"


def test_observations_out_of_time_order_are_unreadable_evidence():
    rows = rich()
    rows[10], rows[11] = rows[11], rows[10]
    rep = oe.evaluate(rows, **GO_KW)
    assert (rep["verdict"], rep["verdict_reasons"]) == ("INSUFFICIENT", ["EVIDENCE_UNREADABLE"])
    assert any(c.startswith("OBSERVATIONS_NOT_STRICTLY_INCREASING_IN_TIME")
               and "index" in c for c in rep["chain_issues"])
    assert rep["observations_total"] == len(rows)            # the count is not masked


def test_an_open_episode_is_marked_and_a_loss_on_it_blocks_go():
    prints = cyc(30.0, 5) * 40 + [30.0] * 6          # ends inside an open episode
    n = len(prints) * 8
    calm = series(prints, bk=book(i10=0.3, i100=2.0))
    rep = oe.evaluate(calm, **GO_KW)
    assert rep["episodes"]["open_at_end"] is True
    assert rep["open_episode_marked"]["carry_bps"] > 0
    assert rep["carry_bps_with_open_episode"]["n"] == rep["carry_bps_per_episode"]["n"] + 1
    assert rep["verdict"] == "GO"
    # the same history, but the short bleeds hard in the open episode (the basis
    # widens 20% before the last observation)
    bleed = series(prints, bk=book(i10=0.3, i100=2.0),
                   perp_fn=lambda h: 100_000.0 * (1 + (0.30 if h > n - 24 else 0.0)))
    bad = oe.evaluate(bleed, **GO_KW)
    assert bad["open_episode_marked"]["carry_bps"] < 0
    assert bad["verdict"] == "INSUFFICIENT"
    assert bad["verdict"] == "INSUFFICIENT"
    assert bad["verdict_reasons"][0] in (
        "WORST_EPISODE_EXCEEDS_DRAWDOWN_LIMIT",
        "LOWER_95_BOUND_NOT_ABOVE_ZERO_WITH_OPEN_EPISODE")
    assert bad["worst_episode_usd"] < -20.0          # the open episode's path counts
    # and when the open episode only drags the lower bound to zero, that alone blocks GO
    cwo = rep["capital_with_open_episode"]
    cwo["result"] = dict(cwo["result"], lower95=-1.0)
    assert oe.verdict(rep, 20.0)[1] == ["LOWER_95_BOUND_NOT_ABOVE_ZERO_WITH_OPEN_EPISODE"]


def test_an_unmarkable_open_episode_blocks_go():
    rep = oe.evaluate(rich(), **GO_KW)
    rep["open_episode_marked"] = {"status": oe.UNAVAILABLE}
    assert oe.verdict(rep, 20.0)[1] == ["OPEN_EPISODE_CANNOT_BE_MARKED"]


def test_the_exit_fires_on_the_third_negative_print_even_inside_one_observation():
    """Several prints arrive in one observation after a gap: -,-,-,+ must exit."""
    n = [6.0] * 6
    rows = series(n, hours=48)
    open_ms = rows[-1]["obs"]["settled_prints"][-1][1]
    big = json.loads(json.dumps(rows[-1]))
    big["obs"]["observed_at_s"] += 3600
    big["obs"]["settled_prints"] = [[6.0, open_ms],
                                    [-1.0, open_ms + 1], [-1.0, open_ms + 2],
                                    [-1.0, open_ms + 3], [6.0, open_ms + 4]]
    sim = oe.simulate_episodes(rows + [big], 100.0)
    assert len(sim["closed"]) == 1 and sim["open_at_end"] is False


def test_the_critical_value_is_never_narrower_than_the_exact_one():
    exact = {2: 6.314, 3: 2.920, 11: 1.812, 31: 1.697, 121: 1.658}
    for n, v in exact.items():
        assert oe.t_crit(n) >= v - 1e-3
    assert oe.t_crit(1) == float("inf")
    assert oe.t_crit(500) >= 1.658 - 1e-9


def test_capacity_is_monotone_a_failing_small_size_caps_larger_ones():
    rows = series([4.0] * 3, bk=book(i10=2.0, i100=0.5))     # impossible book, odd p95s
    cap = oe.capacity_bounds(rows, impact_budget_bps=1.0, planned_notional_usd=50_000)
    assert cap["supported_notional_lower_usd"] == 0.0
    assert cap["planned_inside_measured_capacity"] is False or \
        cap["planned_inside_measured_capacity"] is None
    assert cap["planned_inside_measured_capacity"] is not True


def test_a_planned_size_outside_capacity_blocks_go():
    rep = oe.evaluate(rich(bk=book(i10=0.3, i100=2.0)), **GO_KW)
    rep["capacity"]["planned_inside_measured_capacity"] = None
    assert oe.verdict(rep, 20.0)[1] == ["PLANNED_SIZE_NOT_INSIDE_MEASURED_CAPACITY"]


def test_no_go_is_withheld_on_thin_evidence_even_when_clearly_negative():
    rows = series(cyc(0.9, 5) * 6)
    rep = oe.evaluate(rows, **GO_KW)
    assert rep["episodes"]["priced"] < 30
    assert rep["verdict"] == "INSUFFICIENT"


def test_a_positive_mean_whose_lower_bound_is_not_positive_is_not_go():
    rep = oe.evaluate(rich(), **GO_KW)
    rep["capital"]["result"] = dict(rep["capital"]["result"], mean=1.0, lower95=-0.5)
    assert oe.verdict(rep, 20.0)[1] == ["LOWER_95_BOUND_NOT_ABOVE_ZERO_AFTER_CAPITAL_COST"]


def test_the_v2_protocol_pins_every_input_that_could_move_the_answer():
    for k in ("evaluation_start_s", "taker_fee_bps_per_leg", "adequacy",
              "max_gap_between_observations_s", "max_unpriced_fraction",
              "min_liquidation_distance_pct", "inputs_the_owner_must_supply"):
        assert k in oe.PROTOCOL
    assert oe.PROTOCOL["evaluation_start_s"] == oe.EVALUATION_START_S


# -- stage 4: benchmark consistency, provenance, no look-ahead ----------------------------

def test_every_return_is_expressed_on_the_same_inventory_value():
    e = episode(spot_move=0.10, prints=(0.0, 0.0, 0.0))
    assert e["inventory_value_in_usd"] == pytest.approx(1_000.0)
    assert e["passive_return_bps"] == pytest.approx(1_000.0)           # +10%
    assert e["overlay_total_return_bps"] == pytest.approx(
        e["passive_return_bps"] + e["incremental_vs_passive_bps"])
    # a flat-basis hedge gives up the rally: it underperforms passive by ~10%
    assert e["incremental_vs_passive_bps"] == pytest.approx(
        -1_000.0 + e["carry_bps"], abs=1e-6)


def test_financing_is_stated_as_zero_for_an_overlay_not_silently_omitted():
    e = episode()
    assert e["financing_bps"] == 0.0 and "borrows nothing" in e["financing_note"]


def test_an_overlay_that_trails_passive_btc_per_unit_of_risk_is_not_go():
    """Steady, low-volatility appreciation of the held BTC: unhedged it beats the
    overlay per unit of risk, so the overlay is not a better use of the BTC."""
    steady = lambda h: 100_000.0 * (1 + 0.0001 * h)
    rep = oe.evaluate(rich(spot_fn=steady), **GO_KW)
    assert rep["carry_bps_per_episode"]["lower95"] > 0           # the carry is fine
    ra = rep["risk_adjusted_vs_passive"]
    assert ra["overlay_mean_over_sd"] < ra["passive_mean_over_sd"]
    assert (rep["verdict"], rep["verdict_reasons"]) == (
        "INSUFFICIENT", ["OVERLAY_TRAILS_PASSIVE_BTC_PER_UNIT_OF_RISK"])


def test_a_rally_alone_does_not_decide_the_verdict_it_is_risk_not_raw_return():
    """A mean-return test would make GO depend on the price path (a rally always
    beats a hedge in raw return). The test is per unit of risk."""
    rally = lambda h: 100_000.0 * (1 + 0.0004 * h)
    rep = oe.evaluate(rich(spot_fn=rally), **GO_KW)
    assert rep["incremental_vs_passive_bps"]["mean"] < 0         # trails in raw return
    assert rep["verdict"] == "GO"                                # but not per unit of risk


def test_a_flat_or_falling_window_does_not_trip_the_benchmark_rule():
    assert oe.evaluate(rich(), **GO_KW)["verdict"] == "GO"
    drop = lambda h: 100_000.0 * (1 - 0.0001 * h)
    rep = oe.evaluate(rich(spot_fn=drop), **GO_KW)
    assert rep["verdict"] == "GO" and rep["incremental_vs_passive_bps"]["mean"] > 0


def test_the_distinct_latest_prints_are_reported_as_v1_does_not_the_hourly_count():
    rows = series([2.0] * 23, hours=183)
    rep = oe.evaluate(rows, planned_notional_usd=100.0)
    assert len(rows) == 184 and rep["observations_forward"] == 184
    assert rep["distinct_latest_funding_prints"] == 23


def test_the_report_names_its_inputs_and_they_reproduce_the_same_digest(tmp_path):
    db = str(tmp_path / "m.jsonl")
    for r in series([2.0] * 3, hours=6):
        mp.append(db, r["obs"])
    a = oe.report(db, planned_notional_usd=100.0, owner_capital_apr=0.05)
    b = oe.report(db, planned_notional_usd=100.0, owner_capital_apr=0.05)
    c = oe.report(db, planned_notional_usd=100.0, owner_capital_apr=0.06)
    pa = a["provenance"]
    assert pa["inputs_digest"] == b["provenance"]["inputs_digest"]
    assert pa["inputs_digest"] != c["provenance"]["inputs_digest"]       # an input is part of it
    assert pa["forward_rows"] == 7 and pa["rows_missing_a_chain_hash"] == 0
    assert len(pa["first_row_hash"]) == 64 and pa["protocol_hash"] == oe.protocol_hash()
    assert pa["first_observed_at_s"] < pa["last_observed_at_s"]
    rows, _ = mp.read(db)
    assert pa["last_row_hash"] == rows[-1]["hash"]


def test_a_changed_historical_row_changes_the_digest_and_breaks_the_chain(tmp_path):
    db = str(tmp_path / "m.jsonl")
    for r in series([2.0] * 3, hours=6):
        mp.append(db, r["obs"])
    good = oe.report(db, planned_notional_usd=100.0)
    lines = open(db).read().splitlines()
    lines[1] = lines[1].replace('"spot_mark":100000.0', '"spot_mark":100050.0')
    open(db, "w").write("\n".join(lines) + "\n")
    bad = oe.report(db, planned_notional_usd=100.0)
    assert bad["chain_issues"] and bad["verdict_reasons"] == ["EVIDENCE_UNREADABLE"]
    assert bad["provenance"]["inputs_digest"] != good["provenance"]["inputs_digest"]


def test_no_decision_uses_information_from_the_future():
    """Truncation invariance: replaying only the first k observations must
    reproduce exactly the episodes that had closed by observation k in the full
    replay. A decision that peeked ahead would differ."""
    rows = rich()
    full = oe.simulate_episodes(rows, 100.0)["closed"]
    assert len(full) >= 30
    for k in (97, 211, 480, 1003, len(rows) - 1):
        part = oe.simulate_episodes(rows[:k + 1], 100.0)["closed"]
        expect = [e for e in full if e["exit_s"] <= rows[k]["obs"]["observed_at_s"]]
        assert [(e["entry_s"], e["exit_s"], e["carry_bps"]) for e in part] == \
               [(e["entry_s"], e["exit_s"], e["carry_bps"]) for e in expect]


def test_utilisation_and_annualised_carry_are_stated_with_idle_time_in_the_denominator():
    rep = oe.evaluate(rich(), **GO_KW)
    assert 0.0 < rep["capital_utilisation"] < 1.0
    ann = rep["annualised_carry_bps_of_notional"]
    total = sum(rep["carry_bps_per_episode"]["mean"] for _ in range(rep["episodes"]["priced"]))
    assert ann == pytest.approx(total / rep["span_days"] * 365.0, rel=1e-9)
    assert "not a forecast" in rep["annualisation_note"]


def test_go_states_how_much_capital_cost_it_would_survive():
    rep = oe.evaluate(rich(), **GO_KW)
    assert rep["verdict"] == "GO"
    assert rep["capital"]["break_even_capital_apr"] is None or \
        rep["capital"]["break_even_capital_apr"] > 0


def test_an_explicit_zero_capital_cost_is_go_eligible_but_reports_its_margin_of_safety():
    rep = oe.evaluate(rich(), **dict(GO_KW, owner_capital_apr=0.0))
    assert rep["capital"]["status"] == "OWNER_SUPPLIED"
    assert "break_even_capital_apr" in rep["capital"] and "sensitivity" in rep["capital"]


# -- review round 2: each finding as a failing case before it was fixed ---------------------

def _exit_indices(rows):
    out = oe.simulate_episodes(rows, 100.0)["closed"]
    idx = {r["obs"]["observed_at_s"]: i for i, r in enumerate(rows)}
    return [idx[e["exit_s"]] for e in out]


def test_f1_episodes_across_an_outage_are_priced_from_their_end_books_not_dropped():
    """Outages cluster in stress. Deleting the hours before three losing exits
    used to remove those episodes and flip the answer to GO."""
    rows = rich()
    ex = _exit_indices(rows)
    for k in ex[5:15]:
        rows[k]["obs"]["perp_mark"] *= 1.10                    # a squeeze at the exit
    intact = oe.evaluate(_hashed(rows), **GO_KW)
    kill = {m for k in ex[5:15] for m in range(k - 5, k - 1)}
    gappy = oe.evaluate(_hashed([r for i, r in enumerate(rows) if i not in kill]), **GO_KW)
    assert gappy["episodes"]["gap_spanning"] >= 10
    assert gappy["episodes"]["closed"] == intact["episodes"]["closed"]   # none vanished
    assert gappy["episodes"]["unpriced_unavailable"] == 0
    assert gappy["verdict"] != "GO" and intact["verdict"] != "GO"
    assert gappy["carry_bps_per_episode"]["mean"] == pytest.approx(
        intact["carry_bps_per_episode"]["mean"], rel=1e-9)


def test_f1_an_episode_whose_path_is_unobserved_claims_no_worst_point():
    rows = rich()
    k = _exit_indices(rows)[3]
    kill = set(range(k - 5, k - 1))
    e = [x for x in oe.simulate_episodes(
        [r for i, r in enumerate(rows) if i not in kill], 100.0)["closed"]
         if x["gap_spanning"]]
    assert e and all(x["worst_path_usd"] is None for x in e)


def test_f1_an_episode_with_unseen_funding_prints_is_unpriced_and_valued_adversely():
    rows = rich()
    k = _exit_indices(rows)[10]
    # wipe the printed history for the hours of one episode (a long outage)
    kill = set(range(k - 60, k - 1))
    kept = [r for i, r in enumerate(rows) if i not in kill]
    for r in kept:
        r["obs"]["settled_prints"] = [p for p in r["obs"]["settled_prints"]
                                      if not (rows[k - 60]["obs"]["observed_at_s"] * 1000
                                              < p[1] <= rows[k - 2]["obs"]["observed_at_s"] * 1000)]
    rep = oe.evaluate(_hashed(kept), **GO_KW)
    assert rep["episodes"]["unpriced_unavailable"] >= 1
    assert rep["episodes"]["closed"] == rep["episodes"]["priced"] + rep["episodes"]["unpriced_unavailable"]
    assert rep["carry_bps_per_episode"]["n"] == rep["episodes"]["closed"]   # all counted


def test_f2_the_open_episode_check_includes_the_cost_of_capital():
    prints = cyc(12.0, 5) * 40 + [12.0] * 5
    n = len(prints) * 8
    mk = lambda h: 100_000.0 * (1 + (0.004 if h > n - 2 else 0.0))
    rows = series(prints, bk=book(i10=0.3, i100=2.0), perp_fn=mk)
    base = oe.evaluate(rows, **dict(GO_KW, owner_capital_apr=0.0, committed_capital_fraction=1.0))
    be = base["capital"]["break_even_capital_apr"]
    assert be and base["open_episode_marked"]["carry_bps"] is not None
    tight = oe.evaluate(rows, **dict(GO_KW, owner_capital_apr=be * 0.97,
                                     committed_capital_fraction=1.0))
    cwo = tight["capital_with_open_episode"]["result"]["lower95"]
    assert cwo < tight["capital"]["result"]["lower95"] + 1e-9
    if cwo <= 0:
        assert tight["verdict"] != "GO"


def test_f3_the_digest_covers_the_planned_size_and_the_row_contents():
    rows = rich()
    a = oe.evaluate(rows, **GO_KW)["provenance"]["inputs_digest"]
    b = oe.evaluate(rows, **dict(GO_KW, planned_notional_usd=10_000.0))["provenance"]["inputs_digest"]
    assert a != b
    changed = json.loads(json.dumps(rows))
    changed[100]["obs"]["spot_mark"] += 1.0
    for r in changed:
        r.pop("hash", None)
    unhashed = oe.evaluate(changed, **GO_KW)
    assert unhashed["verdict"] == "INSUFFICIENT"                 # no chain hash: unreadable
    assert unhashed["verdict_reasons"] == ["EVIDENCE_UNREADABLE"]
    other = oe.evaluate(_hashed(changed), **GO_KW)["provenance"]["inputs_digest"]
    assert other != a                                            # content is in the digest


def test_f4_the_protocol_hash_covers_the_parameters_that_move_the_answer():
    import carry_costs
    rp = oe.PROTOCOL["resolved_parameters"]
    assert rp["entry_gate"]["ewma_alpha"] == carry_costs.EWMA_ALPHA
    assert rp["entry_gate"]["ewma_min_prints"] == carry_costs.EWMA_MIN_PRINTS
    assert rp["entry_gate"]["round_trip_bps"] == mp.GATE_ROUND_TRIP_BPS
    assert rp["negative_exit_prints"] == oe.NEGATIVE_EXIT_PRINTS == 3
    assert rp["t95"] and rp["capital_apr_grid"] and rp["bootstrap"]
    import copy
    tweaked = copy.deepcopy(oe.PROTOCOL)
    tweaked["resolved_parameters"]["entry_gate"]["ewma_alpha"] += 0.01
    import hashlib
    h = hashlib.sha256(json.dumps(tweaked, sort_keys=True,
                                  separators=(",", ":")).encode()).hexdigest()
    assert h != oe.protocol_hash()
    assert "unhedged" in oe.PROTOCOL["meaning"]["GO"] and \
        "liquidation" in oe.PROTOCOL["meaning"]["GO"]


def test_f4_the_exit_rule_uses_the_declared_constant_not_a_literal():
    src = open(os.path.join(BOT, "tools", "overlay_economics.py")).read()
    assert "NEGATIVE_EXIT_PRINTS" in src and "streak >= 3" not in src


def _false_pass(gen, trials=400):
    import random
    rng = random.Random(11)
    return sum(oe.mean_bounds(gen(rng))["lower95"] > 0 for _ in range(trials)) / trials


def test_f5_a_true_zero_mean_rarely_passes_even_with_dependence_and_skew():
    def iid(r): return [r.gauss(0, 1) for _ in range(30)]

    def ar(rho):
        def g(r):
            x, o = 0.0, []
            for _ in range(30):
                x = rho * x + r.gauss(0, 1)
                o.append(x)
            return o
        return g

    def skew(r): return [1.0 if r.random() < 0.9 else -9.0 for _ in range(30)]
    # the plain t-interval measured 4.9%, 16.6%, 24.9% and 18.5% on these
    assert _false_pass(iid) < 0.07
    assert _false_pass(ar(0.5)) < 0.11
    assert _false_pass(ar(0.7)) < 0.13
    assert _false_pass(skew) < 0.10


def test_f5_positive_dependence_widens_the_bound_and_negative_never_narrows_it():
    iid = [1.0, -1.0, 0.5, -0.5, 1.2, -1.1, 0.4, -0.3, 0.9, -0.8] * 3
    persist = sorted(iid)                    # same values, strongly persistent order
    a, b = oe.mean_bounds(iid), oe.mean_bounds(persist)
    assert b["rho1"] > 0.5 > a["rho1"]
    assert (b["upper95"] - b["mean"]) > (a["upper95"] - a["mean"])


def test_f5_the_analysis_horizon_is_declared_and_later_rows_are_a_new_test():
    rows = rich()
    horizon = oe.ANALYSIS_HORIZON_DAYS * 86400
    far = json.loads(json.dumps(rows[-1]))
    far["obs"]["observed_at_s"] = oe.EVALUATION_START_S + horizon + 10 * 86400
    rep = oe.evaluate(_hashed(rows + [far]), **GO_KW)
    assert rep["observations_after_horizon_excluded"] == 1
    assert rep["observations_forward"] == len(rows)


@pytest.mark.parametrize("cr,mm", [(1.5, 0.0033), (-0.1, 0.0033), (0.85, 0.0),
                                   (0.85, 1.0), (0.85, -0.1), (float("nan"), 0.0033),
                                   (0.85, float("inf"))])
def test_f7_out_of_range_margin_facts_never_reach_go(cr, mm):
    rep = oe.evaluate(rich(), **dict(GO_KW, collateral_ratio=cr, maintenance_margin_rate=mm))
    assert rep["verdict"] == "INSUFFICIENT"
    assert rep["verdict_reasons"] == [oe.INSUFFICIENT_OWNER_INPUT + ":MARGIN_FACTS"]
    assert rep["stress"]["status"] == "INVALID_INPUT"


def test_f8_a_print_stamped_after_its_observation_is_look_ahead_and_unreadable():
    rows = rich()
    o = rows[300]["obs"]
    o["settled_prints"] = o["settled_prints"] + [[5.0, int((o["observed_at_s"] + 3600) * 1000)]]
    rep = oe.evaluate(_hashed(rows), **GO_KW)
    assert rep["verdict_reasons"] == ["EVIDENCE_UNREADABLE"]
    assert any("after the observation" in c for c in rep["chain_issues"])


def test_f8_two_values_for_one_print_are_two_stories_and_unreadable():
    rows = rich()
    target = rows[400]["obs"]["settled_prints"][-1][1]
    for r in rows[401:]:                      # row 400 keeps the original value
        for pr in r["obs"]["settled_prints"]:
            if pr[1] == target:
                pr[0] += 0.5
    rep = oe.evaluate(_hashed(rows), **GO_KW)
    assert rep["verdict_reasons"] == ["EVIDENCE_UNREADABLE"]
    assert any("two different values" in c for c in rep["chain_issues"])


def test_f8_prints_that_are_not_eight_hours_apart_are_unreadable():
    rows = rich()
    o = rows[200]["obs"]
    o["settled_prints"] = [[o["settled_prints"][0][0], o["settled_prints"][0][1] - 3 * 3600 * 1000]] \
        + o["settled_prints"][1:]
    rep = oe.evaluate(_hashed(rows), **GO_KW)
    assert rep["verdict_reasons"] == ["EVIDENCE_UNREADABLE"]


def test_f9_one_failed_book_read_does_not_block_capacity_but_many_do():
    rows = rich(bk=book(i10=0.3, i100=0.6))
    rows[10]["obs"]["book"] = {"linear": None, "spot": None}
    few = oe.capacity_bounds(rows, impact_budget_bps=1.0, planned_notional_usd=5_000)
    assert few["planned_inside_measured_capacity"] is True
    for r in rows[::4]:
        r["obs"]["book"] = {"linear": None, "spot": None}
    many = oe.capacity_bounds(rows, impact_budget_bps=1.0, planned_notional_usd=5_000)
    assert many["planned_inside_measured_capacity"] is not True


def test_f11_the_annualised_figure_values_unpriced_episodes_adversely_and_says_pre_capital():
    rep = oe.evaluate(rich(), **GO_KW)
    n = rep["episodes"]["closed"]
    assert rep["carry_bps_per_episode"]["n"] == n
    assert "BEFORE capital cost" in rep["annualisation_note"]
    assert "adversely" in rep["annualisation_note"]


PINNED = "cfa96f68a33c740f4a29e550ad6f5eaeaf5f30442464b1633fa2b094a289760b"
