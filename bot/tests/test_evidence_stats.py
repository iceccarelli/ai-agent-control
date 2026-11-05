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


def _coverage(rho, n, trials=400, truth=0.0):
    hit = 0
    for t in range(trials):
        rng = random.Random(1000 + t)
        x, xs = rng.gauss(0, 1) / math.sqrt(1 - rho * rho), []
        for _ in range(n):
            x = rho * x + rng.gauss(0, 1)
            xs.append(x)
        ci = ES.block_bootstrap_ci(xs, B=200)
        if ci["status"] != "OK":
            hit += 1                                  # an interval that is withheld makes no false claim
        elif ci["lo"] <= truth <= ci["hi"]:
            hit += 1
    return hit / trials


@pytest.mark.parametrize("rho,n,floor", [(0.0, 90, 0.90), (0.5, 90, 0.88), (0.7, 120, 0.82)])
def test_the_interval_actually_covers_the_true_mean_at_roughly_its_nominal_rate(rho, n, floor):
    """Measured by simulation, not asserted: the reviewer found the earlier block rule covering 39 % at rho 0.9."""
    assert _coverage(rho, n) >= floor


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
