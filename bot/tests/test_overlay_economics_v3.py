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


def gen(seed=1, *, cycles=27, pos=(8, 14), neg=3, amp=(15, 35), drift=0.0, vol=0.004,
        basis_noise=0.0, bk=None, t0=T0, spot=None):
    """~126 days of hourly rows: funding that opens the gate for several days then
    turns negative; a random-walk spot with optional drift; an optional noisy basis."""
    rng = random.Random(seed)
    prints = []
    for _ in range(cycles):
        prints += [rng.uniform(*amp) for _ in range(rng.randint(*pos))]
        prints += [-rng.uniform(1, 4) for _ in range(neg)]
    hours = len(prints) * 8
    px = [100000.0]
    for _h in range(hours + 1):
        px.append(px[-1] * (1 + drift / 24 + vol * rng.gauss(0, 1) / (24 ** 0.5)))
    nz = [rng.gauss(0, 1) for _ in range(hours + 2)]
    rows = series(prints, t0=t0, hours=hours,
                  spot_fn=(spot or (lambda h: px[h])),
                  perp_fn=(lambda h: (spot or (lambda k: px[k]))(h) * (1 + basis_noise * nz[h])),
                  bk=bk or book(i10=0.3, i100=2.0))
    return rows


def due(rows):
    horizon = v3.EVALUATION_START_S + v3.ANALYSIS_HORIZON_DAYS * 86400
    return [r for r in rows if r["obs"]["observed_at_s"] <= horizon]


@pytest.fixture(scope="module")
def go_report():
    return v3.evaluate(gen(1), **KW)


# -- the frozen v2 is untouched, and v3 is its own protocol -------------------------------------

def test_v2s_pinned_hash_is_unchanged_and_v3_has_its_own():
    assert v2.protocol_hash() == V2_PINNED
    assert v3.protocol_hash() != v2.protocol_hash()
    assert v3.SCHEMA == "overlay_economics/v3" and v2.SCHEMA == "overlay_economics/1"
    assert v3.EVALUATION_START_S > v2.EVALUATION_START_S         # no row v2 judged is judged here


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


# -- the GO path, and every owner input it needs --------------------------------------------------

def test_a_clean_adequate_window_reaches_go_only_with_every_input(go_report):
    assert go_report["verdict"] == "GO", go_report["verdict_reasons"]
    assert go_report["cash"] is False and go_report["class"] == v3.SIMULATED
    assert go_report["evidence"]["realised_cash_usd"] is None


@pytest.mark.parametrize("drop,reason_has", [
    ("owner_capital_apr", "INSUFFICIENT_OWNER_INPUT"),
    ("committed_capital_fraction", "INSUFFICIENT_OWNER_INPUT"),
    ("min_annual_net_usd", "MIN_ANNUAL_NET_USD"),
    ("owner_drawdown_limit_usd", "DRAWDOWN_LIMIT"),
    ("collateral_ratio", "MARGIN_FACTS"),
    ("funding_cap_bps", "STRESS_LOSS"),
    ("price_limit_ratio", "STRESS_LOSS"),
])
def test_every_missing_owner_input_is_a_named_refusal(drop, reason_has):
    rep = v3.evaluate(gen(1), **dict(KW, **{drop: None}))
    assert rep["verdict"] == "INSUFFICIENT"
    assert reason_has in rep["verdict_reasons"][0], rep["verdict_reasons"]


def test_an_explicit_zero_capital_cost_needs_a_stated_basis():
    zero = dict(KW, owner_capital_apr=0.0, capital_cost_basis=None)
    assert v3.evaluate(gen(1), **zero)["verdict_reasons"] == ["ZERO_CAPITAL_COST_NEEDS_A_STATED_BASIS"]
    zero_f = dict(KW, committed_capital_fraction=0.0, capital_cost_basis="  ")
    assert v3.evaluate(gen(1), **zero_f)["verdict_reasons"] == ["ZERO_CAPITAL_COST_NEEDS_A_STATED_BASIS"]
    stated = dict(KW, owner_capital_apr=0.0, capital_cost_basis="owner states: idle BTC earns nothing")
    assert v3.evaluate(gen(1), **stated)["verdict"] == "GO"


@pytest.mark.parametrize("bad", [dict(owner_capital_apr=-0.1), dict(owner_capital_apr=float("nan")),
                                 dict(committed_capital_fraction=1.5), dict(owner_capital_apr=11.0)])
def test_invalid_capital_inputs_never_reach_go(bad):
    assert v3.evaluate(gen(1), **dict(KW, **bad))["verdict_reasons"] == ["INVALID_OWNER_INPUT"]


def test_a_materiality_floor_above_the_edge_is_insufficient_not_go(go_report):
    huge = go_report["annualised"]["net_usd_lower95"] * 10
    rep = v3.evaluate(gen(1), **dict(KW, min_annual_net_usd=huge))
    assert rep["verdict_reasons"] == ["BELOW_THE_OWNERS_MATERIALITY_FLOOR"]


# -- one analysis, no optional stopping -------------------------------------------------------------

def test_any_look_before_the_analysis_date_answers_not_yet_due():
    rows = gen(1)
    for days in (30, 70, 100, 120):
        cut = [r for r in rows if r["obs"]["observed_at_s"] <= T0 + days * 86400]
        rep = v3.evaluate(cut, **KW)
        assert rep["verdict"] == "INSUFFICIENT"
        assert rep["verdict_reasons"] == ["ANALYSIS_NOT_YET_DUE"], (days, rep["verdict_reasons"])


def test_rows_after_the_horizon_are_excluded_so_the_same_window_cannot_be_re_tested_as_it_grows():
    rows = gen(1, cycles=40)                                   # longer than the horizon
    a = v3.evaluate(rows, **KW)
    b = v3.evaluate(due(rows), **KW)
    assert a["observations_after_horizon_excluded"] > 0
    assert a["verdict"] == b["verdict"]
    assert a["provenance"]["inputs_digest"] == b["provenance"]["inputs_digest"]
    assert (a["net_after_tail_charge_bps_per_day"]["mean"]
            == b["net_after_tail_charge_bps_per_day"]["mean"])


# -- the five facets are separate, and add up ----------------------------------------------------------

def test_carry_the_absolute_portfolio_return_the_increment_and_cash_are_separate_keys(go_report):
    for k in ("positive_carry_after_costs_bps_per_day", "net_of_capital_bps_per_day",
              "net_after_tail_charge_bps_per_day", "absolute_overlay_portfolio_return_bps_per_day",
              "passive_btc_return_bps_per_day", "incremental_vs_passive_bps_per_day",
              "risk_adjusted_vs_passive", "hedge_effectiveness", "evidence"):
        assert k in go_report, k
    assert "BEFORE capital" in go_report["positive_carry_after_costs_bps_per_day"]["note"] \
        or "before capital" in go_report["positive_carry_after_costs_bps_per_day"]["note"]
    assert go_report["evidence"]["execution_cost_basis"] == "BOOK_WALK_UPPER_BOUND_NO_LATENCY"


def test_the_combined_return_is_the_passive_return_plus_the_increment(go_report):
    c = go_report["absolute_overlay_portfolio_return_bps_per_day"]["mean"]
    p = go_report["passive_btc_return_bps_per_day"]["mean"]
    i = go_report["incremental_vs_passive_bps_per_day"]["mean"]
    assert c == pytest.approx(p + i, rel=1e-9, abs=1e-9)


def test_idle_time_is_in_the_denominator():
    rep = v3.evaluate(gen(1), **KW)
    assert 0.0 < rep["annualised"]["capital_utilisation"] < 1.0


# -- degenerate evidence is refused by name -------------------------------------------------------------

def test_a_frozen_price_feed_is_refused():
    rep = v3.evaluate(gen(1, spot=lambda h: 100000.0), **KW)
    assert rep["verdict"] == "INSUFFICIENT" and "PRICE_FEED_FROZEN" in rep["verdict_reasons"]


def test_a_time_shifted_replay_of_the_best_funding_block_is_refused():
    """Tile an 8-day block of funding 15 times at shifted timestamps."""
    block = gen(7, cycles=3, pos=(8, 10))
    prints = []
    for r in block:
        for p in r["obs"]["settled_prints"]:
            if p[1] not in {x[1] for x in prints}:
                prints.append(list(p))
    values = [p[0] for p in sorted(prints, key=lambda p: p[1])]
    tiled = values * 15
    rows = series(tiled, t0=T0, hours=len(tiled) * 8,
                  spot_fn=lambda h: 100000.0 + 50.0 * math.sin(h / 53.0) + 0.5 * h % 17,
                  bk=book())
    rep = v3.evaluate(rows, **KW)
    assert rep["verdict"] == "INSUFFICIENT"
    assert "FUNDING_SEQUENCE_REPEATED" in rep["verdict_reasons"], rep["verdict_reasons"]


def test_funding_with_almost_no_distinct_values_is_refused():
    prints = [5.0, 6.0, 5.0, 6.0, -2.0, -2.0, -2.0, 5.0, 6.0] * 45
    rows = series(prints, t0=T0, hours=len(prints) * 8,
                  spot_fn=lambda h: 100000.0 + 40.0 * math.sin(h / 41.0) + (h % 7), bk=book())
    rep = v3.evaluate(due(rows), **KW)
    assert "TOO_FEW_DISTINCT_FUNDING_VALUES" in rep["verdict_reasons"]


def test_constant_block_values_cannot_be_go():
    rep = {"chain_issues": [], "degenerate": [], "analysis_due": True,
           "blocks": {"complete": 9}, "episodes": {"priced": 40, "unpriced_fraction": 0.0},
           "net_of_capital_bps_per_day": {}, "passive_btc_return_bps_per_day": {"sd": 3.0},
           "net_after_tail_charge_bps_per_day": {"sd": 0.0, "lower95": 5.0, "upper95": 5.0}}
    assert v3._verdict(rep, 100.0, 20.0, 1.0) == ("INSUFFICIENT", ["BLOCK_VALUES_CONSTANT"])
    rep["net_after_tail_charge_bps_per_day"]["sd"] = 3e-14
    assert v3._verdict(rep, 100.0, 20.0, 1.0)[1] == ["BLOCK_VALUES_CONSTANT"]


def test_a_constant_passive_return_cannot_be_go():
    rep = {"chain_issues": [], "degenerate": [], "analysis_due": True,
           "blocks": {"complete": 9}, "episodes": {"priced": 40, "unpriced_fraction": 0.0},
           "net_of_capital_bps_per_day": {}, "passive_btc_return_bps_per_day": {"sd": 0.0},
           "net_after_tail_charge_bps_per_day": {"sd": 3.0, "lower95": 5.0, "upper95": 9.0}}
    assert v3._verdict(rep, 100.0, 20.0, 1.0)[1] == ["PASSIVE_RETURN_CONSTANT"]


# -- the hedge and the benchmark ----------------------------------------------------------------------------

def test_a_hedge_with_a_noisy_basis_is_not_shown_to_remove_price_risk():
    rep = v3.evaluate(gen(1, basis_noise=0.05), **KW)
    assert rep["verdict"] == "INSUFFICIENT"
    assert rep["verdict_reasons"][0] in ("HEDGE_NOT_SHOWN_TO_REMOVE_PRICE_RISK",
                                         "LOWER_95_BOUND_NOT_ABOVE_ZERO_AFTER_COSTS_TAIL_AND_CAPITAL")


def test_a_hedge_that_works_is_shown_to_work(go_report):
    assert go_report["hedge_effectiveness"]["upper95"] < 1.0
    assert go_report["hedge_effectiveness"]["episodes"] >= v3.MIN_CLOSED_EPISODES


def test_positive_carry_does_not_hide_a_significant_loss_of_wealth_versus_holding_btc():
    """A violent rally: carry is positive, but the hedged episodes gave up far more
    price gain than the carry paid."""
    rep = v3.evaluate(gen(2, drift=0.06, vol=0.004), **KW)
    assert rep["positive_carry_after_costs_bps_per_day"]["lower95"] > 0
    assert rep["incremental_vs_passive_bps_per_day"]["upper95"] < 0
    assert rep["verdict"] == "INSUFFICIENT"
    assert rep["verdict_reasons"] == ["SIGNIFICANTLY_WORSE_THAN_HOLDING_BTC"]


def test_noise_alone_does_not_block_a_go_on_the_benchmark(go_report):
    inc = go_report["incremental_vs_passive_bps_per_day"]
    assert not (inc["upper95"] is not None and inc["upper95"] < 0)


# -- capital: encumbered collateral grows with price ------------------------------------------------------------

def test_capital_is_charged_on_a_margin_that_grows_with_price():
    def cost(rep):                     # carry minus net = what the capital charge took
        return (rep["positive_carry_after_costs_bps_per_day"]["mean"]
                - rep["net_of_capital_bps_per_day"]["mean"])
    flat = v3.evaluate(gen(3, drift=0.0, vol=0.0005), **KW)
    up = v3.evaluate(gen(3, drift=0.02, vol=0.0005), **KW)
    assert cost(flat) > 0
    assert cost(up) > cost(flat)          # same funding path, higher price: more margin tied up


def test_a_larger_committed_fraction_costs_more():
    lo = v3.evaluate(gen(3), **dict(KW, committed_capital_fraction=0.1))
    hi = v3.evaluate(gen(3), **dict(KW, committed_capital_fraction=1.0))
    assert hi["net_of_capital_bps_per_day"]["mean"] < lo["net_of_capital_bps_per_day"]["mean"]


# -- the unseen tail ---------------------------------------------------------------------------------------------------

def test_the_poisson_upper_bound_is_the_exact_one_and_zero_is_the_rule_of_three():
    assert v3.poisson_upper95(0) == pytest.approx(3.0, abs=0.01)
    assert v3.poisson_upper95(1) == pytest.approx(4.744, abs=0.001)
    assert v3.poisson_upper95(10) == pytest.approx(16.962, abs=0.001)
    assert v3.poisson_upper95(25) > 25


def test_no_tail_seen_in_n_episodes_is_charged_three_over_n_of_the_stress_loss():
    t = v3.unseen_tail_charge_bps_per_episode([5.0] * 30, 300.0)
    assert t["observed_tail_episodes"] == 0
    assert t["charge_bps_per_episode"] == pytest.approx(300.0 * 2.996 / 30, rel=1e-3)


def test_a_seen_tail_is_not_charged_twice():
    losses = [5.0] * 27 + [-400.0] * 3
    t = v3.unseen_tail_charge_bps_per_episode(losses, 300.0)
    assert t["observed_tail_episodes"] == 3
    assert t["charge_bps_per_episode"] == pytest.approx(300.0 * (7.754 - 3) / 30, rel=1e-3)


def test_without_the_stress_inputs_the_tail_is_unavailable():
    assert v3.unseen_tail_charge_bps_per_episode([1.0] * 30, 0.0)["status"] == v3.UNAVAILABLE


@pytest.mark.parametrize("p_tail", [0.02, 0.05])
def test_a_rare_large_loss_sized_for_a_zero_mean_no_longer_passes(p_tail):
    """The peso problem: 55% / 20% false GO at n=30 for a plain interval."""
    stress = 300.0
    loss = 10.0 * (1 - p_tail) / p_tail
    rng = random.Random(5)
    plain = charged = 0
    trials = 300
    for _ in range(trials):
        eps = [(-loss if rng.random() < p_tail else 10.0) for _ in range(27)]
        blocks = [sum(eps[i:i + 3]) / v3.BLOCK_DAYS for i in range(0, 27, 3)]
        t = v3.unseen_tail_charge_bps_per_episode(eps, stress)
        per_day = (t["charge_bps_per_episode"] or 0.0) * (27 / (9 * v3.BLOCK_DAYS))
        plain += v3.block_bounds(blocks)["lower95"] > 0
        charged += v3.block_bounds([b - per_day for b in blocks])["lower95"] > 0
    assert plain / trials > 0.10                   # the problem was real
    assert charged / trials <= 0.05                # and the charge removes it


# -- independence is calendar time -------------------------------------------------------------------------------------

def _ep(entry_d, exit_d, key_bps):
    return {"entry_s": entry_d * 86400.0, "exit_s": exit_d * 86400.0, "carry_adv": key_bps}


def test_an_episode_is_split_across_blocks_in_proportion_to_its_days():
    out = v3.allocate_to_blocks([_ep(10, 20, 100.0)], 0.0, 2, "carry_adv")
    assert out[0] * v3.BLOCK_DAYS == pytest.approx(40.0)         # 4 of 10 days in block 0
    assert out[1] * v3.BLOCK_DAYS == pytest.approx(60.0)
    assert sum(out) * v3.BLOCK_DAYS == pytest.approx(100.0)       # nothing lost or invented


def test_a_block_with_no_episode_is_a_zero_not_a_gap():
    out = v3.allocate_to_blocks([_ep(1, 3, 50.0)], 0.0, 3, "carry_adv")
    assert out[1] == 0.0 and out[2] == 0.0


def test_long_and_short_episodes_with_the_same_daily_rate_give_the_same_block_value():
    long_ = v3.allocate_to_blocks([_ep(0, 10, 100.0)], 0.0, 1, "carry_adv")
    short = v3.allocate_to_blocks([_ep(0, 5, 50.0), _ep(5, 10, 50.0)], 0.0, 1, "carry_adv")
    assert long_ == pytest.approx(short)


def test_block_values_with_dependence_do_not_pass_a_zero_mean_often():
    rng = random.Random(9)
    hits = 0
    for _ in range(600):
        x, vals = 0.0, []
        for _k in range(9):
            x = 0.6 * x + rng.gauss(0, 1)
            vals.append(x)
        hits += v3.block_bounds(vals)["lower95"] > 0
    assert hits / 600 < 0.10


# -- a broken or tampered window is unreadable --------------------------------------------------------------------------

def test_look_ahead_in_a_print_makes_the_evidence_unreadable():
    rows = gen(1)
    o = rows[500]["obs"]
    o["settled_prints"] = o["settled_prints"] + [[5.0, int((o["observed_at_s"] + 7200) * 1000)]]
    import hashlib
    for r in rows:
        r["hash"] = hashlib.sha256(json.dumps(r["obs"], sort_keys=True, default=str).encode()).hexdigest()
    rep = v3.evaluate(rows, **KW)
    assert rep["verdict_reasons"] == ["EVIDENCE_UNREADABLE"]


def test_the_digest_covers_every_owner_input_and_the_protocol():
    a = v3.evaluate(gen(1), **KW)["provenance"]["inputs_digest"]
    b = v3.evaluate(gen(1), **dict(KW, min_annual_net_usd=2.0))["provenance"]["inputs_digest"]
    assert a != b
    assert a == v3.evaluate(gen(1), **KW)["provenance"]["inputs_digest"]


def test_an_empty_dataset_is_insufficient():
    rep = v3.evaluate([], **KW)
    assert rep["verdict"] == "INSUFFICIENT" and rep["cash"] is False


PINNED_V3 = "82ab5d0ace20669d005697c4958710f660279072ee134689d9bdadafa94a5a32"
