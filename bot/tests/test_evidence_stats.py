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


# ------------------------------------------------------------------------------------------------ the Student-t critical value behind every interval
# Two-sided critical values P(|T| > t) = alpha from the standard published tables (each also checked against scipy.stats.t.ppf when this was written).
T_TWO_SIDED = [
    (0.10, 1, 6.313752), (0.10, 2, 2.919986), (0.10, 3, 2.353363), (0.10, 4, 2.131847), (0.10, 5, 2.015048), (0.10, 6, 1.943180),
    (0.10, 10, 1.812461), (0.10, 20, 1.724718), (0.10, 30, 1.697261), (0.10, 120, 1.657651),
    (0.05, 1, 12.706205), (0.05, 2, 4.302653), (0.05, 3, 3.182446), (0.05, 5, 2.570582), (0.05, 10, 2.228139), (0.05, 30, 2.042272),
    (0.01, 2, 9.924843), (0.01, 5, 4.032143), (0.01, 10, 3.169273),
]


@pytest.mark.parametrize("alpha,df,expected", T_TWO_SIDED)
def test_the_t_critical_value_is_the_exact_two_sided_one_from_the_published_tables(alpha, df, expected):
    """The interval code used a Cornish-Fisher expansion clamped at df >= 2: 2.762 for the 90% interval at df = 2 (the table says 2.920: the old value was 5.4% too small) and,
    because of the clamp, 2.762 at df = 1 where the table says 6.314. Every few-batch interval was narrower than it claimed."""
    assert abs(ES._t_crit(df, alpha) - expected) < 2e-6


def test_the_t_critical_value_agrees_with_the_independent_exact_implementation_in_statistical_power():
    import statistical_power as SP
    for df in (1, 1.5, 2, 2.5, 3, 3.5, 4, 5.5, 7.3, 12, 33.3, 120, 1000, 99999):
        for alpha in (0.2, 0.1, 0.05, 0.02, 0.005):
            assert abs(ES._t_crit(df, alpha) - SP.student_t_isf(alpha / 2.0, df)) < 1e-9, (df, alpha)


def test_the_t_critical_value_is_never_below_the_normal_one_and_falls_with_the_degrees_of_freedom():
    from statistics import NormalDist
    z = NormalDist().inv_cdf(0.95)
    prev = math.inf
    hand = ES._T_EXPANSION_DF
    for df in (1, 1.5, 2, 3, 5, 8, 13, 21, 55, 144, 1000, hand - 1, hand + 1, 10 * hand, 10 ** 7):
        t = ES._t_crit(df, 0.10)
        assert z - 1e-12 <= t <= prev + 1e-10, (df, t, prev)           # 1e-10: the exact path below the hand-over wobbles by up to ~3e-11
        prev = t
    assert abs(ES._t_crit(10 ** 9, 0.10) - z) < 1e-6 and ES._t_crit(float("inf"), 0.10) == pytest.approx(z, abs=1e-12)


# scipy.stats.t.ppf(1 - alpha / 2, df) at full precision, for the degrees of freedom where the three-term expansion takes over from the exact evaluation.
T_LARGE_DF = [
    (1e5, 0.10, 1.6448688647849696), (1e5, 0.05, 1.9599877075346095), (1e5, 0.005, 2.8070960818387443),
    (1e6, 0.10, 1.6448551507220404), (1e6, 0.05, 1.959966356814107), (1e6, 0.005, 2.8070399995776443),
    (1e7, 0.10, 1.6448537793284017), (1e7, 0.05, 1.959964221767205), (1e7, 0.005, 2.807034391466038),
    (1e7, 1e-12, 7.130516090054259), (1e7, 1e-15, 8.026872012580194), (1e8, 1e-9, 6.1094107902256765),    # a tiny alpha: the normal quantile must come from the tail, not from 1 - alpha / 2
]


@pytest.mark.parametrize("df,alpha,expected", T_LARGE_DF)
def test_the_expansion_that_takes_over_above_the_hand_over_matches_full_precision_reference_values(df, alpha, expected):
    assert abs(ES._t_crit(df, alpha) - expected) < 1e-12


def test_the_expansion_used_above_the_hand_over_matches_the_exact_evaluation_at_the_hand_over(monkeypatch):
    """Above 1e5 degrees of freedom the three-term expansion stands in for the exact evaluation (which loses digits there, to about 1e-9 at 1e6): at the hand-over itself the two
    must agree, so no step appears where one takes over from the other."""
    df = ES._T_EXPANSION_DF
    for alpha in (0.2, 0.1, 0.05, 0.005):
        with monkeypatch.context() as m:
            m.setattr(ES, "_T_EXPANSION_DF", 0.0)               # the expansion everywhere
            expansion = ES._t_crit(df, alpha)
        with monkeypatch.context() as m:
            m.setattr(ES, "_T_EXPANSION_DF", float("inf"))      # the exact evaluation everywhere
            exact = ES._t_crit(df, alpha)
        assert abs(expansion - exact) < 1e-9, (alpha, expansion, exact)


@pytest.mark.parametrize("df,alpha", [(0, 0.1), (-1, 0.1), (float("nan"), 0.1), (2, 0), (2, 1), (2, -0.1), (2, 1.5), (2, float("nan"))])
def test_the_t_critical_value_refuses_what_it_cannot_define_instead_of_returning_a_number(df, alpha):
    with pytest.raises(ValueError):
        ES._t_crit(df, alpha)


def test_a_confidence_so_high_that_no_critical_value_can_be_bracketed_is_refused_not_returned_as_a_huge_number():
    """df 0.001 at alpha 1e-6 has its root beyond 1e121: without the refusal the bracketing loop falls through and returns the last, wrong-sided, bracket end."""
    with pytest.raises(ValueError):
        ES._t_crit(0.001, 1e-6)


def test_an_incomplete_beta_continued_fraction_that_does_not_converge_is_an_error_not_a_number():
    with pytest.raises(ArithmeticError):
        ES._betacf(1e10, 1e10, 0.5)


def _batch_se(xs, k):
    b = len(xs) // k
    tail = list(xs[len(xs) - k * b:])
    bm = [sum(tail[i * b:(i + 1) * b]) / b for i in range(k)]
    mbar = sum(bm) / k
    return math.sqrt(sum((v - mbar) ** 2 for v in bm) / (k - 1) / k)


@pytest.mark.parametrize("n,seed,batches", [(8, 2, (2,)), (12, 1, (3,)), (24, 1, (3, 6))])
def test_every_batch_means_interval_uses_the_exact_t_of_its_own_k_minus_one_degrees_of_freedom(n, seed, batches):
    """k batch means have k - 1 degrees of freedom: 1 for the 2 batches a sample of 8 to 11 gets, 2 for 3, 5 for 6. The old clamp gave all of them at least the df = 2 value."""
    rng = random.Random(seed)
    xs = [rng.gauss(0.5, 2.0) for _ in range(n)]
    ci = ES.block_bootstrap_ci(xs, alpha=0.10, B=200)
    assert ci["status"] == "OK" and [k for k in (2, 3, 6) if f"batch{k}_lo" in ci] == list(batches)
    table = {1: 6.313752, 2: 2.919986, 5: 2.015048}
    m = sum(xs) / n
    for k in batches:
        half = (ci[f"batch{k}_hi"] - ci[f"batch{k}_lo"]) / 2.0
        assert half == pytest.approx(table[k - 1] * _batch_se(xs, k), rel=2e-6)
        assert ci[f"batch{k}_lo"] + half == pytest.approx(m, abs=1e-9)
    assert ci["lo"] <= min(ci[f"batch{k}_lo"] for k in batches) and ci["hi"] >= max(ci[f"batch{k}_hi"] for k in batches)    # the widest-of rule still holds


def test_the_ess_adjusted_t_interval_uses_the_exact_t_of_its_own_degrees_of_freedom_even_when_the_ess_is_small():
    import statistical_power as SP
    for n, seed in ((15, 2), (24, 1)):                                  # ESS 4.5 (df 3.5: the old expansion was 1.3% narrow) and 15.2
        rng = random.Random(seed)
        xs = [rng.gauss(0.5, 2.0) for _ in range(n)]
        ci = ES.block_bootstrap_ci(xs, alpha=0.10, B=200)
        m = sum(xs) / n
        var = sum((x - m) ** 2 for x in xs) / (n - 1)
        half = SP.student_t_isf(0.05, ci["ess"] - 1.0) * math.sqrt(var / ci["ess"])
        assert (ci["ess_t_hi"] - ci["ess_t_lo"]) / 2.0 == pytest.approx(half, rel=1e-9)
