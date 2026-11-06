import math
import random

import pytest

import evidence_stats as ES
from capacity_helpers import base_row, series


def test_effective_sample_size_of_a_constant_series_is_one_and_of_iid_is_near_n():
    assert ES.effective_sample_size([0.5] * 40) == 1.0
    rng = random.Random(1)
    iid = [rng.gauss(0, 1) for _ in range(400)]
    assert ES.effective_sample_size(iid) > 0.6 * 400


def test_a_persistent_series_has_far_fewer_independent_points_than_observations():
    rng = random.Random(2)
    x, xs = 0.0, []
    for _ in range(400):
        x = 0.95 * x + rng.gauss(0, 1)
        xs.append(x)
    ess = ES.effective_sample_size(xs)
    assert 1.0 <= ess < 0.2 * 400


def test_the_block_bootstrap_is_deterministic_contains_the_mean_and_refuses_tiny_samples():
    rng = random.Random(3)
    xs = [rng.gauss(0.6, 0.3) for _ in range(60)]
    a, b = ES.block_bootstrap_ci(xs), ES.block_bootstrap_ci(xs)
    assert a == b and a["status"] == "OK" and a["lo"] < sum(xs) / len(xs) < a["hi"]
    assert ES.block_bootstrap_ci(xs, seed=1)["bootstrap_lo"] != a["bootstrap_lo"]       # the seed is honoured
    assert ES.block_bootstrap_ci([0.1] * 5)["status"] == "INSUFFICIENT"


def test_block_bootstrap_is_wider_than_naive_for_dependent_data():
    rng = random.Random(4)
    x, xs = 0.0, []
    for _ in range(300):
        x = 0.9 * x + rng.gauss(0, 1)
        xs.append(x)
    ci = ES.block_bootstrap_ci(xs, B=800)
    naive_half = 1.96 * (sum((v - sum(xs) / 300) ** 2 for v in xs) / 299) ** 0.5 / math.sqrt(300)
    assert (ci["hi"] - ci["lo"]) / 2 > 2 * naive_half            # far wider, not merely wider


def _ar1(rho, n, rng, sd=1.0):
    x = rng.gauss(0, 1) / math.sqrt(1 - rho * rho)
    out = []
    for _ in range(n):
        x = rho * x + rng.gauss(0, 1)
        out.append(x * sd)
    return out


GATE_ESS = 30.0          # funding_eval.MIN_ESS


def _coverage_among_gate_passers(make, n, trials=250):
    """(series that pass the same gate funding_eval applies, share of them whose interval holds the true mean 0,
    share whose LOWER bound is above the truth). Unconditional coverage would flatter the interval: the question
    that matters is how often a series the gates ACCEPT yields a wrong claim."""
    passed = hit = over = 0
    for t in range(trials):
        xs = make(random.Random(7000 + t), n)
        ci = ES.block_bootstrap_ci(xs, B=100)
        if ci["status"] != "OK" or ci["ess"] < GATE_ESS:
            continue
        passed += 1
        hit += ci["lo"] <= 0.0 <= ci["hi"]
        over += ci["lo"] > 0.0
    return passed, (hit / passed if passed else None), (over / passed if passed else None)


def test_among_series_the_gate_accepts_the_interval_holds_the_true_mean_at_the_stated_rate():
    """Measured, conditional on the gate (600 trials a cell): rho 0 n 60 -> 0.99; rho .5 n 90 -> 0.95; rho .7 n 120 -> 0.88-0.92
    (the weakest cell, a seed-to-seed range); rho .8 n 200 -> 0.94; pooled ~0.96, lower bound above the truth ~3 %. The
    earlier block rule covered 0.36-0.84 in the same cells, which is what the independent review found. The floors
    below are set under the measured values, not at the nominal 95 %."""
    tot = hit = over = 0
    for rho, n in [(0.0, 60), (0.5, 90), (0.7, 120), (0.8, 200)]:
        passed, cov, ov = _coverage_among_gate_passers(lambda r, n_, rho=rho: _ar1(rho, n_, r), n, trials=600)
        assert passed >= 60, f"too few series passed the gate in ({rho}, {n}): {passed}"
        assert cov >= 0.85, (rho, n, passed, cov)
        tot, hit, over = tot + passed, hit + cov * passed, over + ov * passed
    assert hit / tot >= 0.93 and over / tot <= 0.05, (tot, hit / tot, over / tot)


def test_a_three_settlement_cycle_cannot_inflate_the_effective_sample_size_to_n():
    """From three settlements a day: a cycle makes lag-1 autocorrelation negative, so the acf estimator reported
    ESS ~ n for a series dominated by a persistent level. The batch-means estimator is not fooled. (Measured: at a
    cycle amplitude of 3 over AR(0.9) the acf figure is >= 80 yet the conservative one is < 60 % of it in ~1/3 of runs.)"""
    fooled = 0
    for t in range(60):
        rng = random.Random(9000 + t)
        base = _ar1(0.9, 90, rng)
        xs = [b + 3.0 * (1.0 if i % 3 == 0 else -0.5) for i, b in enumerate(base)]
        acf = ES.effective_sample_size(xs)
        cons = ES.conservative_ess(xs)
        assert cons <= acf
        fooled += (acf >= 80.0 and cons < 0.6 * acf)
    assert fooled >= 5, "the guard never mattered in this sample: the test is not exercising the failure it names"


def test_a_strongly_persistent_series_is_almost_always_refused_by_the_gate_not_given_a_confident_interval():
    passed, _, _ = _coverage_among_gate_passers(lambda r, n_: _ar1(0.9, n_, r), 200, trials=200)
    assert passed <= 0.12 * 200


def test_at_strong_persistence_the_honest_answer_is_a_tiny_ess_that_the_gates_refuse_not_a_confident_interval():
    rng = random.Random(7)
    x, xs = 0.0, []
    for _ in range(90):
        x = 0.9 * x + rng.gauss(0, 1)
        xs.append(x)
    assert ES.effective_sample_size(xs) < 30           # 90 settlements, far fewer independent draws


def test_a_constant_series_gets_no_interval_not_a_zero_width_one():
    ci = ES.block_bootstrap_ci([1.0] * 40)
    assert ci["status"] == "NO_VARIANCE" and "lo" not in ci


def test_raw_snapshots_unique_settlements_and_independent_events_are_different_numbers():
    base = base_row()
    rows = series(base, 12, lambda T, t: 0.6 if (T // 28_800_000) % 2 else -0.2, lambda T: 0.6)
    rep = ES.independence_report(rows)
    assert rep["raw_snapshots"] == len(rows) > 1000
    assert rep["unique_settlements"] == rep["independent_events"] == 12
    assert rep["snapshots_per_settlement_mean"] == pytest.approx(len(rows) / 12)
    assert rep["effective_sample_size"] <= 12
    assert rep["regime_sign_transitions"] >= 5 and rep["time_blocks"]
    assert "correlated" in rep["reading"]
