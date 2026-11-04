"""Overlay economics, prospective protocol v3: the false GO / false NO_GO cases
that broke the earlier decision rule, each as a test.

Synthetic observations only. v2 (`overlay_economics`, schema overlay_economics/1)
is untouched and its pinned hash is asserted here."""
from __future__ import annotations

import ast
import json
import math
import os
import random
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
BOT = os.path.dirname(HERE)
for p in (BOT, os.path.join(BOT, "tools"), HERE):
    if p not in sys.path:
        sys.path.insert(0, p)

import overlay_economics as v2                                  # noqa: E402
import overlay_economics_v3 as v3                               # noqa: E402
from test_overlay_economics import PINNED as V2_PINNED, book, series   # noqa: E402

T0 = v3.EVALUATION_START_S + 1
KW = dict(planned_notional_usd=100.0, owner_capital_apr=0.05,
          committed_capital_fraction=0.2, capital_cost_basis="owner: BTC lent at 5%",
          min_annual_net_usd=1.0, owner_drawdown_limit_usd=20.0,
          maintenance_margin_rate=0.0033, collateral_ratio=0.85,
          funding_cap_bps=33.3, price_limit_ratio=0.02)


def _rehash(rows):
    import hashlib
    for r in rows:
        r["hash"] = hashlib.sha256(
            json.dumps(r["obs"], sort_keys=True, default=str).encode()).hexdigest()
    return rows


def gen(seed=1, *, pos=(8, 14), neg=3, amp=(15, 35), drift=0.0, vol=0.004,
        basis_noise=0.0005, bk=None, t0=T0, spot=None, days=None, step=1, prints=None):
    """~one horizon of rows (2-hourly): funding that opens the gate for several days
    then turns negative; a random-walk spot with optional drift; a small noisy basis."""
    rng = random.Random(seed)
    horizon_h = (days or v3.ANALYSIS_HORIZON_DAYS) * 24 + 100
    if prints is None:
        prints = []
        while len(prints) * 8 < horizon_h:
            prints += [rng.uniform(*amp) for _ in range(rng.randint(*pos))]
            prints += [-rng.uniform(1, 4) for _ in range(neg)]
    hours = len(prints) * 8
    px = [100000.0]
    for _h in range(hours + 1):
        px.append(px[-1] * (1 + drift / 24 + vol * rng.gauss(0, 1) / (24 ** 0.5)))
    nz = [rng.gauss(0, 1) for _ in range(hours + 2)]
    sp = spot or (lambda h: px[h])
    rows = series(prints, t0=t0, hours=hours, spot_fn=sp,
                  perp_fn=lambda h: sp(h) * (1 + basis_noise * nz[h]),
                  bk=bk or book(i10=0.3, i100=2.0))
    return _rehash(rows[::step])


def due(rows):
    """The rows a first look after the horizon sees: everything up to and including
    the first observation at or after it."""
    horizon = v3.EVALUATION_START_S + v3.ANALYSIS_HORIZON_DAYS * 86400
    out, seen = [], False
    for r in rows:
        if seen:
            break
        out.append(r)
        seen = r["obs"]["observed_at_s"] >= horizon
    return out


def until(rows, days):
    return [r for r in rows if r["obs"]["observed_at_s"] <= T0 + days * 86400]


@pytest.fixture(scope="module")
def go_report():
    return v3.evaluate(gen(1), **KW)


# -- the frozen v2 is untouched, and v3 is its own protocol -------------------------------------

def test_v2s_pinned_hash_is_unchanged_and_v3_has_its_own():
    assert v2.protocol_hash() == V2_PINNED
    assert v3.protocol_hash() != v2.protocol_hash()
    assert v3.SCHEMA == "overlay_economics/v3" and v2.SCHEMA == "overlay_economics/1"
    assert v3.EVALUATION_START_S > v2.EVALUATION_START_S


def test_v3_hash_is_pinned_against_silent_change():
    assert v3.protocol_hash() == PINNED_V3


def test_this_module_imports_nothing_that_can_place_an_order():
    tree = ast.parse(open(os.path.join(BOT, "tools", "overlay_economics_v3.py")).read())
    mods = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            mods |= {a.name.split(".")[0] for a in n.names}
        elif isinstance(n, ast.ImportFrom):
            mods.add((n.module or "").split(".")[0])
    assert mods <= {"__future__", "argparse", "hashlib", "json", "math", "os", "random",
                    "sys", "typing", "mainnet_public", "overlay_economics"}


def test_the_horizon_is_a_whole_number_of_blocks():
    assert v3.ANALYSIS_HORIZON_DAYS % v3.BLOCK_DAYS == 0
    assert v3.N_BLOCKS == v3.ANALYSIS_HORIZON_DAYS // v3.BLOCK_DAYS == 18


# -- the GO path, and every owner input it needs --------------------------------------------------

def test_a_clean_covered_window_reaches_go_only_with_every_input(go_report):
    assert go_report["verdict"] == "GO", go_report["verdict_reasons"]
    assert go_report["cash"] is False and go_report["class"] == v3.SIMULATED
    assert go_report["evidence"]["realised_cash_usd"] is None
    assert go_report["blocks"]["complete"] == 18          # eighteen, anchored to the declared start


@pytest.mark.parametrize("drop,reason_has", [
    ("owner_capital_apr", "INSUFFICIENT_OWNER_INPUT"),
    ("committed_capital_fraction", "INSUFFICIENT_OWNER_INPUT"),
    ("min_annual_net_usd", "MIN_ANNUAL_NET_USD"),
    ("owner_drawdown_limit_usd", "DRAWDOWN_LIMIT"),
    ("collateral_ratio", "MARGIN_FACTS"),
    (("funding_cap_bps", "price_limit_ratio"), "STRESS_LOSS"),
])
def test_every_missing_owner_input_is_a_named_refusal(drop, reason_has):
    drops = (drop,) if isinstance(drop, str) else drop
    rep = v3.evaluate(gen(1), **dict(KW, **{d: None for d in drops}))
    assert rep["verdict"] == "INSUFFICIENT"
    assert reason_has in rep["verdict_reasons"][0], rep["verdict_reasons"]


def test_a_capital_basis_is_always_required_not_only_for_a_zero():
    assert v3.evaluate(gen(1), **dict(KW, capital_cost_basis=None))["verdict_reasons"] == \
        ["CAPITAL_COST_NEEDS_A_STATED_BASIS"]
    tiny = dict(KW, committed_capital_fraction=1e-9, capital_cost_basis="  ")
    assert v3.evaluate(gen(1), **tiny)["verdict_reasons"] == ["CAPITAL_COST_NEEDS_A_STATED_BASIS"]
    zero = dict(KW, owner_capital_apr=0.0, capital_cost_basis="owner states: idle BTC earns nothing")
    assert v3.evaluate(gen(1), **zero)["verdict"] == "GO"


@pytest.mark.parametrize("bad", [dict(owner_capital_apr=-0.1), dict(owner_capital_apr=float("nan")),
                                 dict(committed_capital_fraction=1.5), dict(owner_capital_apr=11.0)])
def test_invalid_capital_inputs_never_reach_go(bad):
    assert v3.evaluate(gen(1), **dict(KW, **bad))["verdict_reasons"] == ["INVALID_OWNER_INPUT"]


def test_giving_only_one_of_the_two_stress_inputs_is_invalid_not_ignored():
    for k in ("funding_cap_bps", "price_limit_ratio"):
        rep = v3.evaluate(gen(1), **dict(KW, **{k: None}))
        assert rep["verdict"] == "INSUFFICIENT"
        assert rep["verdict_reasons"] == ["INVALID_OWNER_INPUT:funding_cap_bps/price_limit_ratio"]


@pytest.mark.parametrize("bad", [dict(taker_fee_bps=-50.0), dict(taker_fee_bps=1000.0),
                                 dict(taker_fee_bps=float("nan")), dict(planned_notional_usd=-100.0),
                                 dict(planned_notional_usd=0.0), dict(planned_notional_usd=float("nan")),
                                 dict(funding_cap_bps=1.0, price_limit_ratio=0.001),
                                 dict(funding_cap_bps=-5.0)])
def test_invalid_numeric_inputs_are_refused_by_name_not_by_accident(bad):
    rep = v3.evaluate(gen(1), **dict(KW, **bad))
    assert rep["verdict"] == "INSUFFICIENT"
    assert rep["verdict_reasons"][0].startswith("INVALID_OWNER_INPUT:"), rep["verdict_reasons"]


def test_a_materiality_floor_above_the_edge_is_insufficient_not_go(go_report):
    huge = go_report["annualised"]["net_usd_lower95"] * 10
    rep = v3.evaluate(gen(1), **dict(KW, min_annual_net_usd=huge))
    assert rep["verdict_reasons"] == ["BELOW_THE_OWNERS_MATERIALITY_FLOOR"]


# -- ONE analysis: no optional stopping, no early look, no re-anchoring --------------------------------

def test_any_look_before_the_analysis_date_answers_not_yet_due():
    rows = gen(1)
    for days in (60, 150, 240, 251):
        rep = v3.evaluate(until(rows, days), **KW)
        assert rep["verdict"] == "INSUFFICIENT"
        assert rep["verdict_reasons"] == ["ANALYSIS_NOT_YET_DUE"], (days, rep["verdict_reasons"])


def test_a_look_a_few_hours_early_is_not_a_second_analysis():
    rows = gen(1)
    end = v3.EVALUATION_START_S + v3.ANALYSIS_HORIZON_DAYS * 86400
    early = [r for r in rows if r["obs"]["observed_at_s"] <= end - 3 * 3600]
    assert v3.evaluate(early, **KW)["verdict_reasons"] == ["ANALYSIS_NOT_YET_DUE"]
    final = v3.evaluate(due(rows), **KW)
    assert final["verdict"] == "GO"


def test_rows_after_the_horizon_are_excluded_so_the_window_cannot_be_re_tested_as_it_grows():
    rows = gen(1, days=300)
    a = v3.evaluate(rows, **KW)
    b = v3.evaluate(due(rows), **KW)
    assert a["observations_after_horizon_excluded"] > 0
    assert a["verdict"] == b["verdict"]
    assert a["provenance"]["inputs_digest"] == b["provenance"]["inputs_digest"]
    assert (a["net_after_tail_charge_bps_per_day"]["mean"]
            == b["net_after_tail_charge_bps_per_day"]["mean"])


def test_a_late_collector_is_not_re_anchored_it_is_insufficient():
    rows = gen(1, t0=T0 + 30 * 86400, days=v3.ANALYSIS_HORIZON_DAYS - 20)
    rep = v3.evaluate(rows, **KW)
    assert rep["verdict"] == "INSUFFICIENT"
    assert "COLLECTION_STARTED_LATE" in rep["verdict_reasons"], rep["verdict_reasons"]


def test_a_block_with_missing_observations_makes_the_window_insufficient():
    rows = gen(1)
    b = 7
    b0 = v3.EVALUATION_START_S + b * v3.BLOCK_DAYS * 86400
    holed = [r for r in rows if not (b0 + 2 * 86400 <= r["obs"]["observed_at_s"] < b0 + 12 * 86400)]
    rep = v3.evaluate(_rehash(holed), **KW)
    assert rep["verdict"] == "INSUFFICIENT"
    assert f"BLOCK_{b}_NOT_COVERED" in rep["verdict_reasons"], rep["verdict_reasons"]


def test_a_collection_with_every_block_present_is_covered(go_report):
    assert go_report["coverage"] == []


# -- the five facets are separate, and add up ----------------------------------------------------------

def test_carry_the_absolute_portfolio_return_the_increment_and_cash_are_separate_keys(go_report):
    for k in ("positive_carry_after_costs_bps_per_day", "net_of_capital_bps_per_day",
              "net_after_tail_charge_bps_per_day", "absolute_overlay_portfolio_return_bps_per_day",
              "passive_btc_return_bps_per_day", "incremental_vs_passive_bps_per_day",
              "risk_adjusted_vs_passive", "hedge_effectiveness", "evidence", "persistence"):
        assert k in go_report, k
    assert "before capital" in go_report["positive_carry_after_costs_bps_per_day"]["note"]
    assert go_report["evidence"]["execution_cost_basis"] == "BOOK_WALK_UPPER_BOUND_NO_LATENCY"


def test_the_combined_return_is_the_passive_return_plus_the_increment(go_report):
    c = go_report["absolute_overlay_portfolio_return_bps_per_day"]["mean"]
    p = go_report["passive_btc_return_bps_per_day"]["mean"]
    i = go_report["incremental_vs_passive_bps_per_day"]["mean"]
    assert c == pytest.approx(p + i, rel=1e-9, abs=1e-9)


def test_idle_time_is_in_the_denominator_and_the_value_is_not_the_per_episode_mean(go_report):
    util = go_report["annualised"]["capital_utilisation"]
    assert 0.5 < util < 1.0                                  # a mostly-open gate, never all of it
    assert go_report["net_of_capital_bps_per_day"]["mean"] < \
        go_report["positive_carry_after_costs_bps_per_day"]["mean"]


# -- degenerate evidence is refused by name ------------------------------------------------------------

def test_a_frozen_price_feed_is_refused():
    rep = v3.evaluate(gen(1, spot=lambda h: 100000.0), **KW)
    assert rep["verdict"] == "INSUFFICIENT" and "PRICE_FEED_FROZEN" in rep["verdict_reasons"]


def test_a_price_feed_with_a_little_jitter_is_still_frozen():
    """P7: 'spot = 100000 + 0.01*((h*7)%13)' defeated a 95%-identical-values test."""
    rep = v3.evaluate(gen(1, spot=lambda h: 100000.0 + 0.01 * ((h * 7) % 13)), **KW)
    assert "PRICE_FEED_FROZEN" in rep["verdict_reasons"], rep["verdict_reasons"]


def test_a_constant_basis_is_a_copied_feed_not_a_perfect_hedge():
    rep = v3.evaluate(gen(1, basis_noise=0.0), **KW)
    assert rep["verdict"] == "INSUFFICIENT" and "BASIS_CONSTANT" in rep["verdict_reasons"]


@pytest.mark.parametrize("jitter", [0.0, 1e-9, 1e-6, 1e-4, 4e-3])
def test_a_time_shifted_replay_of_the_best_funding_block_is_refused(jitter):
    """P8: a 28-day sample tiled with jitter of 1e-4 bps used to pass as 36 weeks."""
    block = gen(7, pos=(8, 10), days=12)
    uniq = {}
    for r in block:
        for bps, ms in r["obs"]["settled_prints"]:
            uniq.setdefault(ms, bps)
    values = [uniq[k] for k in sorted(uniq)][:30]
    rj = random.Random(5)
    tiled = [v + rj.uniform(-jitter, jitter) for v in values * 90]
    rows = _rehash(series(tiled, t0=T0, hours=len(tiled) * 8,
                          spot_fn=lambda h: 100000.0 * (1 + 0.02 * math.sin(h / 53.0)) + 3.0 * (h % 17),
                          perp_fn=lambda h: 100000.0 * (1 + 0.02 * math.sin(h / 53.0)) + 3.0 * (h % 17) + 5.0 * math.sin(h),
                          bk=book()))
    rep = v3.evaluate(rows, **KW)
    assert rep["verdict"] == "INSUFFICIENT"
    assert "FUNDING_SEQUENCE_REPEATED" in rep["verdict_reasons"], rep["verdict_reasons"]


def test_funding_with_almost_no_distinct_values_is_refused():
    prints = [5.0, 6.0, 5.0, 6.0, -2.0, -2.0, -2.0, 5.0, 6.0] * 120
    rows = gen(1, prints=prints)
    rep = v3.evaluate(rows, **KW)
    assert "TOO_FEW_DISTINCT_FUNDING_VALUES" in rep["verdict_reasons"], rep["verdict_reasons"]


def test_constant_block_values_cannot_be_go():
    rep = {"chain_issues": [], "degenerate": [], "analysis_due": True, "coverage": [],
           "invalid_inputs": [], "persistence": {"k_eff": 10.0},
           "episodes": {"priced": 40, "unpriced_fraction": 0.0},
           "net_of_capital_bps_per_day": {}, "passive_btc_return_bps_per_day": {"sd": 30.0},
           "net_after_tail_charge_bps_per_day": {"sd": 0.0, "lower95": 5.0, "upper95": 5.0}}
    assert v3._verdict(rep, 300.0, 20.0, 1.0) == ("INSUFFICIENT", ["BLOCK_VALUES_CONSTANT"])


def test_a_nearly_constant_passive_return_cannot_be_go():
    rep = {"chain_issues": [], "degenerate": [], "analysis_due": True, "coverage": [],
           "invalid_inputs": [], "persistence": {"k_eff": 10.0},
           "episodes": {"priced": 40, "unpriced_fraction": 0.0},
           "net_of_capital_bps_per_day": {}, "passive_btc_return_bps_per_day": {"sd": 0.01},
           "net_after_tail_charge_bps_per_day": {"sd": 3.0, "lower95": 5.0, "upper95": 9.0}}
    assert v3._verdict(rep, 300.0, 20.0, 1.0)[1] == ["PASSIVE_RETURN_CONSTANT"]


# -- persistence: a sample too persistent for its length -------------------------------------------------

def _daily_rows(daily):
    """Rows whose settled prints give exactly this daily funding series (3 prints a day)."""
    prints = []
    for d in daily:
        prints += [d / 3.0] * 3
    # the first print lands on 00:00 UTC, so a UTC day holds exactly its own three prints
    return series(prints, t0=T0 - 8 * 3600, hours=len(prints) * 8, bk=book())


def test_a_persistent_funding_series_has_fewer_effective_blocks():
    """A single draw is noisy, so the ordering is asserted on the mean over seeds."""
    def ar(rng, phi, n=252):
        x, out = 0.0, []
        for _ in range(n):
            x = phi * x + rng.gauss(0, 1)
            out.append(30.0 + 10.0 * x)
        return out
    def mean_k(phi):
        ks = [v3.effective_blocks(_daily_rows(ar(random.Random(100 + s), phi)), 18)["k_eff"]
              for s in range(12)]
        return sum(ks) / len(ks)
    iid, mid, sticky = mean_k(0.0), mean_k(0.9), mean_k(0.99)
    assert iid > mid > sticky
    assert iid > 12 and sticky < v3.MIN_EFFECTIVE_BLOCKS


def test_the_effective_blocks_are_never_more_than_the_blocks():
    rows = _daily_rows([30.0 + (i % 5) for i in range(252)])
    assert v3.effective_blocks(rows, 18)["k_eff"] <= 18.0


def test_a_sample_too_persistent_for_its_length_is_insufficient_not_go():
    rep = {"chain_issues": [], "degenerate": [], "analysis_due": True, "coverage": [],
           "invalid_inputs": [], "persistence": {"k_eff": 2.5}}
    assert v3._verdict(rep, 300.0, 20.0, 1.0)[1] == ["SAMPLE_TOO_PERSISTENT_FOR_ITS_LENGTH"]


def test_too_few_days_to_measure_persistence_is_named():
    assert v3.effective_blocks(_daily_rows([30.0] * 10), 18)["status"] == v3.UNAVAILABLE


@pytest.mark.parametrize("phi", [0.9, 0.97, 0.99])
def test_the_effective_block_inference_does_not_pass_a_zero_edge_through_persistence(phi):
    """The failure the first draft had: 17-27% at a 70-150 day persistence. The
    driver is a daily AR(1) funding level; the 'edge' is the level's own block mean
    (true mean zero); the inference is the real K_eff -> t bound over 18 blocks."""
    rng = random.Random(int(phi * 1000))
    trials, hits = 400, 0
    for _ in range(trials):
        x, daily = 0.0, []
        for _d in range(252):
            x = phi * x + rng.gauss(0, 1)
            daily.append(x)
        k_eff = v3.effective_blocks_from_daily(daily, 18)["k_eff"]
        blocks = [sum(daily[i * 14:(i + 1) * 14]) / 14.0 for i in range(18)]
        b = v3.block_bounds(blocks, k_eff)
        hits += (b["lower95"] is not None and b["lower95"] > 0)
    assert hits / trials <= 0.09, (phi, hits / trials)


# -- the hedge and the benchmark ----------------------------------------------------------------------------

def test_a_hedge_with_a_noisy_basis_is_not_shown_to_remove_the_price_risk():
    rep = v3.evaluate(gen(1, basis_noise=0.004), **KW)
    he = rep["hedge_effectiveness"]
    assert 0.3 < he["ratio"] < 0.9                           # removes only part of the variance
    assert rep["verdict"] == "INSUFFICIENT"
    assert rep["verdict_reasons"] == ["HEDGE_NOT_SHOWN_TO_REMOVE_MOST_PRICE_RISK"]


def test_a_hedge_that_works_is_shown_to_work(go_report):
    assert go_report["hedge_effectiveness"]["upper95"] < v3.HEDGE_RESIDUAL_RATIO_MAX
    assert go_report["hedge_effectiveness"]["episodes"] >= v3.MIN_CLOSED_EPISODES


def test_positive_carry_does_not_hide_a_significant_loss_of_wealth_versus_holding_btc():
    rep = v3.evaluate(gen(2, drift=0.06, vol=0.004), **KW)
    assert rep["positive_carry_after_costs_bps_per_day"]["lower95"] > 0
    assert rep["incremental_vs_passive_bps_per_day"]["upper95"] < 0
    assert rep["verdict"] == "INSUFFICIENT"
    assert rep["verdict_reasons"] == ["SIGNIFICANTLY_WORSE_THAN_HOLDING_BTC"]


# -- capital -------------------------------------------------------------------------------------------------

def test_capital_is_charged_on_a_margin_that_grows_with_price():
    def cost(rep):
        return (rep["positive_carry_after_costs_bps_per_day"]["mean"]
                - rep["net_of_capital_bps_per_day"]["mean"])
    flat = v3.evaluate(gen(3, drift=0.0, vol=0.0005), **KW)
    up = v3.evaluate(gen(3, drift=0.02, vol=0.0005), **KW)
    assert cost(flat) > 0
    assert cost(up) > cost(flat)


def test_a_larger_committed_fraction_costs_more():
    lo = v3.evaluate(gen(3), **dict(KW, committed_capital_fraction=0.1))
    hi = v3.evaluate(gen(3), **dict(KW, committed_capital_fraction=1.0))
    assert hi["net_of_capital_bps_per_day"]["mean"] < lo["net_of_capital_bps_per_day"]["mean"]


# -- the unseen tail, unpriced episodes, and the upside --------------------------------------------------------

def test_the_poisson_upper_bound_is_the_exact_one_and_zero_is_the_rule_of_three():
    assert v3.poisson_upper95(0) == pytest.approx(3.0, abs=0.01)
    assert v3.poisson_upper95(1) == pytest.approx(4.744, abs=0.001)
    assert v3.poisson_upper95(10) == pytest.approx(16.962, abs=0.001)
    assert v3.poisson_upper95(25) > 25


def test_no_tail_seen_in_n_episodes_is_charged_three_over_n_of_the_stress_loss():
    t = v3.unseen_tail_charge_bps_per_episode([5.0] * 30, 300.0)
    assert t["observed_tail_episodes"] == 0
    assert t["charge_bps_per_episode"] == pytest.approx(300.0 * 2.996 / 30, rel=1e-3)


def test_a_loss_bigger_than_the_declared_stress_raises_the_stress_it_is_charged_at():
    """P5: a loss beyond the stress used to be charged at the stress value only."""
    t = v3.unseen_tail_charge_bps_per_episode([5.0] * 29 + [-1000.0], 300.0)
    assert t["stress_loss_bps"] == 2000.0                      # twice the worst observed
    assert t["observed_tail_episodes"] == 0                    # nothing yet that bad twice over
    assert t["charge_bps_per_episode"] == pytest.approx(2000.0 * 2.996 / 30, rel=1e-3)


def test_a_seen_tail_is_not_charged_twice():
    t = v3.unseen_tail_charge_bps_per_episode([5.0] * 27 + [-300.0] * 3, 300.0)
    assert t["stress_loss_bps"] == 600.0
    t2 = v3.unseen_tail_charge_bps_per_episode([5.0] * 27 + [-301.0] * 3, 100.0)
    assert t2["observed_tail_episodes"] <= 3


def test_without_the_stress_inputs_the_tail_is_unavailable():
    assert v3.unseen_tail_charge_bps_per_episode([1.0] * 30, 0.0)["status"] == v3.UNAVAILABLE


@pytest.mark.parametrize("loss_multiple", [2, 10, 33])
def test_a_rare_loss_far_beyond_the_stress_no_longer_passes(loss_multiple):
    """P5: 600 (2x), 3000 (10x) and 10000 (33x) bps losses sized for a zero mean
    gave 1.6% / 19% / 59% false GO because they were charged at the stress only."""
    stress = 300.0
    loss = stress * loss_multiple
    p_tail = 10.0 / (10.0 + loss)                    # mean zero: p*loss = (1-p)*10
    rng = random.Random(loss_multiple)
    trials, hits = 300, 0
    for _ in range(trials):
        eps = [(-loss if rng.random() < p_tail else 10.0) for _ in range(54)]
        blocks = [sum(eps[i:i + 3]) / v3.BLOCK_DAYS for i in range(0, 54, 3)]
        t = v3.unseen_tail_charge_bps_per_episode(eps, stress)
        per_day = (t["charge_bps_per_episode"] or 0.0) * (54 / 252.0)
        hits += v3.block_bounds([b - per_day for b in blocks], 18.0)["lower95"] > 0
    assert hits / trials <= 0.08, (loss_multiple, hits / trials)


def test_outage_episodes_are_valued_at_the_full_stress_loss_not_at_a_positive_rate():
    """P6: two outage episodes were valued at +200 and +144 bps and the verdict stayed GO."""
    rows = gen(1)
    ok = v3.evaluate(rows, **KW)
    assert ok["episodes"]["unpriced"] == 0
    # blank the books around two episodes' exits (an outage of the book feed)
    sim = v2.simulate_episodes(due(rows), 100.0)["closed"]
    idx = {r["obs"]["observed_at_s"]: i for i, r in enumerate(rows)}
    holed = json.loads(json.dumps(rows))
    for e in sim[10:12]:
        holed[idx[e["exit_s"]]]["obs"]["book"] = {"linear": None, "spot": None}
    rep = v3.evaluate(_rehash(holed), **KW)
    assert rep["episodes"]["unpriced"] >= 1
    assert rep["net_after_tail_charge_bps_per_day"]["mean"] < ok["net_after_tail_charge_bps_per_day"]["mean"]


def test_the_unseen_upside_credit_stops_a_no_go_on_a_rare_big_winner_process():
    base = {"chain_issues": [], "degenerate": [], "analysis_due": True, "coverage": [],
            "invalid_inputs": [], "persistence": {"k_eff": 12.0},
            "episodes": {"priced": 40, "unpriced_fraction": 0.0},
            "net_of_capital_bps_per_day": {}, "passive_btc_return_bps_per_day": {"sd": 30.0},
            "net_after_tail_charge_bps_per_day": {"sd": 3.0, "lower95": -9.0, "upper95": -1.0},
            "most_favourable_bps_per_day": {"upper95": -1.0},
            "capital": {"status": "NOT_PROVIDED_FOR_THIS_TEST"}}
    assert v3._verdict(dict(base, unseen_upside_credit_bps_per_day=0.0), 300.0, 20.0, 1.0)[0] == "NO_GO"
    assert v3._verdict(dict(base, unseen_upside_credit_bps_per_day=5.0), 300.0, 20.0, 1.0)[0] != "NO_GO"


@pytest.mark.parametrize("seed", [1, 2])
def test_an_adequate_clearly_losing_churn_is_no_go(seed):
    """Short episodes whose funding cannot repay the entry and exit cost: the gate
    opens often (75+ priced episodes), and even the best case with the upside credit
    is below zero."""
    rep = v3.evaluate(gen(seed, amp=(0.5, 1.5), pos=(3, 5)), **KW)
    assert rep["verdict"] == "NO_GO", rep["verdict_reasons"]
    assert rep["episodes"]["priced"] >= v3.MIN_CLOSED_EPISODES
    fav = rep["most_favourable_bps_per_day"]["upper95"]
    assert fav + rep["unseen_upside_credit_bps_per_day"] <= 0


def test_a_losing_process_the_upside_credit_could_still_hide_is_insufficient_not_no_go():
    rep = v3.evaluate(gen(1, amp=(0.2, 0.9)), **KW)
    assert rep["verdict"] == "INSUFFICIENT"
    assert rep["verdict_reasons"] == ["LOWER_95_BOUND_NOT_ABOVE_ZERO_AFTER_COSTS_TAIL_AND_CAPITAL"]
    fav = rep["most_favourable_bps_per_day"]["upper95"]
    assert fav < 0 < fav + rep["unseen_upside_credit_bps_per_day"]


def test_no_go_is_never_issued_on_thin_or_unready_evidence():
    assert v3.evaluate(until(gen(1, amp=(0.2, 0.9)), 100), **KW)["verdict"] == "INSUFFICIENT"
    assert v3.evaluate([], **KW)["verdict"] == "INSUFFICIENT"


# -- independence is calendar time -------------------------------------------------------------------------------------

def _ep(entry_d, exit_d, key_bps):
    return {"entry_s": entry_d * 86400.0, "exit_s": exit_d * 86400.0, "carry_adv": key_bps}


def test_an_episode_is_split_across_blocks_in_proportion_to_its_days():
    out = v3.allocate_to_blocks([_ep(10, 20, 100.0)], 0.0, 2, "carry_adv")
    assert out[0] * v3.BLOCK_DAYS == pytest.approx(40.0)
    assert out[1] * v3.BLOCK_DAYS == pytest.approx(60.0)
    assert sum(out) * v3.BLOCK_DAYS == pytest.approx(100.0)


def test_a_block_with_no_episode_is_a_zero_not_a_gap():
    out = v3.allocate_to_blocks([_ep(1, 3, 50.0)], 0.0, 3, "carry_adv")
    assert out[1] == 0.0 and out[2] == 0.0


def test_an_episode_exactly_on_a_boundary_lands_in_one_block():
    out = v3.allocate_to_blocks([_ep(14, 28, 140.0)], 0.0, 3, "carry_adv")
    assert out == pytest.approx([0.0, 10.0, 0.0])


def test_long_and_short_episodes_with_the_same_daily_rate_give_the_same_block_value():
    long_ = v3.allocate_to_blocks([_ep(0, 10, 100.0)], 0.0, 1, "carry_adv")
    short = v3.allocate_to_blocks([_ep(0, 5, 50.0), _ep(5, 10, 50.0)], 0.0, 1, "carry_adv")
    assert long_ == pytest.approx(short)


# -- a broken or tampered window is unreadable --------------------------------------------------------------------------

def test_look_ahead_in_a_print_makes_the_evidence_unreadable():
    rows = gen(1)
    o = rows[500]["obs"]
    o["settled_prints"] = o["settled_prints"] + [[5.0, int((o["observed_at_s"] + 7200) * 1000)]]
    rep = v3.evaluate(_rehash(rows), **KW)
    assert rep["verdict_reasons"] == ["EVIDENCE_UNREADABLE"]


def test_the_provenance_is_v3s_own_and_its_digest_covers_the_protocol_and_every_input():
    rep = v3.evaluate(gen(1), **KW)
    pv = rep["provenance"]
    assert pv["schema"] == "overlay_economics/v3"
    assert pv["protocol_hash"] == v3.protocol_hash() != v2.protocol_hash()
    assert pv["evaluation_start_s"] == v3.EVALUATION_START_S
    assert pv["analysis_horizon_days"] == v3.ANALYSIS_HORIZON_DAYS
    other = v3.evaluate(gen(1), **dict(KW, min_annual_net_usd=2.0))["provenance"]["inputs_digest"]
    assert other != pv["inputs_digest"]
    assert pv["inputs_digest"] == v3.evaluate(gen(1), **KW)["provenance"]["inputs_digest"]
    clean = v3.provenance([], {}, [])["inputs_digest"]
    assert clean != v3.provenance([], {}, ["a chain issue"])["inputs_digest"]


def test_the_digest_changes_with_the_protocol(monkeypatch):
    before = v3.provenance([], {}, [])["inputs_digest"]
    monkeypatch.setitem(v3.PROTOCOL, "block_days", 99)
    assert v3.provenance([], {}, [])["inputs_digest"] != before


def test_an_empty_dataset_is_insufficient():
    rep = v3.evaluate([], **KW)
    assert rep["verdict"] == "INSUFFICIENT" and rep["cash"] is False


# -- second independent review of the rewritten rule --------------------------------------------------------------------

@pytest.mark.parametrize("phi,noise", [(0.99, 10.0), (0.99, 3.0), (0.95, 10.0), (0.90, 3.0)])
def test_a_slow_regime_hidden_under_daily_noise_does_not_pass_a_zero_edge(phi, noise):
    """R1: lag-1 autocorrelation of the daily series was ~0 for these, K_eff stayed
    near 18, and a true-zero edge passed 11-35% of the time."""
    rng = random.Random(int(phi * 100 + noise))
    trials, hits = 400, 0
    for _ in range(trials):
        x, daily = 0.0, []
        for _d in range(252):
            x = phi * x + rng.gauss(0, 3.0)
            daily.append(x + rng.gauss(0, noise))
        k_eff = v3.effective_blocks_from_daily(daily, 18)["k_eff"]
        blocks = [sum(daily[i * 14:(i + 1) * 14]) / 14.0 for i in range(18)]
        b = v3.block_bounds(blocks, k_eff)
        hits += (b["lower95"] is not None and b["lower95"] > 0)
    assert hits / trials <= 0.09, (phi, noise, hits / trials)


def test_the_long_run_form_does_not_cost_power_on_an_uncorrelated_series():
    rng = random.Random(3)
    ks = [v3.effective_blocks_from_daily([rng.gauss(0, 5) for _ in range(252)], 18)["k_eff"]
          for _ in range(30)]
    assert sum(ks) / len(ks) > 12


def test_a_missing_stress_input_never_leaves_the_door_open_to_no_go():
    """R2: without funding_cap_bps/price_limit_ratio the upside credit shrank ~20x and
    a process that is INSUFFICIENT with the inputs became NO_GO."""
    rows = gen(1, amp=(2, 5), pos=(4, 8))
    with_inputs = v3.evaluate(rows, **KW)
    without = v3.evaluate(rows, **dict(KW, funding_cap_bps=None, price_limit_ratio=None))
    assert with_inputs["verdict"] != "NO_GO"
    assert without["verdict"] == "INSUFFICIENT"
    assert without["verdict_reasons"][0].startswith("INSUFFICIENT_OWNER_INPUT")


def test_the_window_opens_after_the_rule_was_fixed():
    import datetime as dt
    assert v3.EVALUATION_START_S == int(dt.datetime(2026, 11, 11, tzinfo=dt.timezone.utc).timestamp())
    assert "2026-11-04" in v3.PROTOCOL["declared"] and "2026-11-11" in v3.PROTOCOL["declared"]
    assert v3.PROTOCOL["evaluation_start_s"] == v3.EVALUATION_START_S


def _numbers(x, out):
    if isinstance(x, bool):
        return
    if isinstance(x, (int, float)):
        out.add(float(x))
    elif isinstance(x, dict):
        for v in x.values():
            _numbers(v, out)
    elif isinstance(x, (list, tuple)):
        for v in x:
            _numbers(v, out)


def test_every_numeric_constant_that_can_move_a_verdict_is_inside_the_hashed_protocol():
    """R4: MIN_DAILY_POINTS_FOR_PERSISTENCE, v2's max gap and liquidation floor, the
    stress multiples and others were outside the hash."""
    inside = set()
    _numbers(v3.PROTOCOL, inside)
    skip = {"HERE", "BOOT_SEED", "BOOT_N", "MAX_START_DELAY_S", "MIN_CLOSED_EPISODES",
            "MAX_UNPRICED_FRACTION", "MIN_DISTINCT_WINDOW_SHARE", "UNAVAILABLE"}
    missing = []
    for name, val in vars(v3).items():
        if name.isupper() and isinstance(val, (int, float)) and not isinstance(val, bool) \
                and name not in skip:
            if float(val) not in inside:
                missing.append(name)
    assert missing == [], missing
    for key in ("max_gap_between_observations_s", "min_liquidation_distance_pct"):
        assert float(v2.PROTOCOL[key]) in inside
    assert v3.IMPLEMENTATION_VERSION == v3.PROTOCOL["resolved_parameters"]["implementation_version"]


@pytest.mark.parametrize("bad", [dict(funding_cap_bps=1e308), dict(funding_cap_bps=1e300),
                                 dict(funding_cap_bps=1e5), dict(price_limit_ratio=1e305),
                                 dict(price_limit_ratio=5.0), dict(taker_fee_bps=0.0),
                                 dict(taker_fee_bps=0.5), dict(committed_capital_fraction=0.0)])
def test_overflowing_or_nonsensical_inputs_are_refused_not_silently_zeroed(bad):
    rep = v3.evaluate(gen(1), **dict(KW, **bad))
    assert rep["verdict"] == "INSUFFICIENT"
    assert rep["verdict_reasons"][0].startswith(("INVALID_OWNER_INPUT", "INSUFFICIENT_OWNER_INPUT")) \
        or rep["verdict_reasons"] == ["INVALID_OWNER_INPUT"], rep["verdict_reasons"]


@pytest.mark.parametrize("field", ["spot_mark", "perp_mark"])
@pytest.mark.parametrize("bad", [float("nan"), 0.0, -5.0, float("inf"), "x", None, True])
def test_a_bad_mark_makes_the_evidence_unreadable(field, bad):
    rows = gen(1)
    rows[3000]["obs"][field] = bad
    rep = v3.evaluate(_rehash(rows), **KW)
    assert rep["verdict"] == "INSUFFICIENT"
    assert rep["verdict_reasons"] == ["EVIDENCE_UNREADABLE"], rep["verdict_reasons"]


def test_episodes_that_span_an_observation_gap_are_unpriced_and_cannot_hide_from_the_drawdown_gate():
    """R9: a third of the episodes had no worst path and the gate never saw them."""
    rows = due(gen(1))
    sim = v2.simulate_episodes(rows, 100.0)["closed"]
    idx = {r["obs"]["observed_at_s"]: i for i, r in enumerate(rows)}
    drop = set()
    for e in sim[::3]:
        a, b = idx[e["entry_s"]], idx[e["exit_s"]]
        mid = (a + b) // 2
        drop |= set(range(mid, mid + 10))
    holed = [r for i, r in enumerate(rows) if i not in drop]
    rep = v3.evaluate(_rehash(holed), **KW)
    assert rep["verdict"] == "INSUFFICIENT"
    assert rep["episodes"]["unpriced"] > 0
    assert any(x in rep["verdict_reasons"] for x in ("TOO_MANY_EPISODES_UNPRICEABLE",)) or \
        rep["verdict_reasons"][0].startswith("BLOCK_"), rep["verdict_reasons"]


def test_the_analysis_is_due_at_the_first_row_at_or_after_the_horizon_and_reads_the_same_inputs_forever():
    """R10/R11: cutting at H-1h and at H were both 'due' with different inputs."""
    start = v3.EVALUATION_START_S
    end = start + v3.ANALYSIS_HORIZON_DAYS * 86400
    rows = gen(1, t0=start, days=v3.ANALYSIS_HORIZON_DAYS + 12)
    before = [r for r in rows if r["obs"]["observed_at_s"] <= end - 3600]
    at = [r for r in rows if r["obs"]["observed_at_s"] <= end]
    later = [r for r in rows if r["obs"]["observed_at_s"] <= end + 9 * 86400]
    assert v3.evaluate(before, **KW)["verdict_reasons"] == ["ANALYSIS_NOT_YET_DUE"]
    a, b = v3.evaluate(at, **KW), v3.evaluate(later, **KW)
    assert a["verdict"] == b["verdict"] == "GO"
    assert a["provenance"]["inputs_digest"] == b["provenance"]["inputs_digest"]
    assert a["net_after_tail_charge_bps_per_day"]["mean"] == b["net_after_tail_charge_bps_per_day"]["mean"]


def test_a_three_hour_cadence_with_an_offset_still_becomes_due():
    rows = gen(1)[::3]
    end = v3.EVALUATION_START_S + v3.ANALYSIS_HORIZON_DAYS * 86400
    assert any(r["obs"]["observed_at_s"] >= end for r in rows)
    assert v3.evaluate(rows, **KW)["analysis_due"] is True


PINNED_V3 = "d64e9eec15b32a3562ba25ce3d49fa433504fcf1c14bf28df92986c004627d4d"
