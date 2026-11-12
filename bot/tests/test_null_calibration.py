"""Null calibration of the p-value every research gate consumes (``research_harness.significance``): measured, with seeded pure-stdlib generators, on series whose true mean is exactly zero.

WHY THIS EXISTS. The ESS-adjusted t p-value is what each Holm gate and each 'independent observations' floor stands on. An independent statistical audit (Round 14) measured its SIZE:
at or below the stated level for iid and autoregressive nulls, and many times above it for REGIME-DRIVEN nulls, a slow hidden level that the autocorrelation estimate cannot see, so the
count of 'independent observations' is overstated. The two halves are pinned here so that (1) a future change cannot silently worsen the first and (2) the second cannot be 'fixed' or made
worse without the documents that state it (docs/MONEY_MISSION.md Round 14, ``statistical_power`` KNOWN LIMITS, ``candidate_readiness`` notes) being updated in the same change.
It is a measurement of a LIMIT, not an endorsement: the bounds are wide enough for the Monte Carlo error (about 1 percentage point at 500 series) and the runtime is a few seconds."""
from __future__ import annotations

import math
import os
import random
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools"))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import research_harness as RH  # noqa: E402

HOLM12 = 0.05 / 12                                                    # the per-test level a 12-variant Holm gate works at


def _iid(rng: random.Random, n: int):
    return [rng.gauss(0.0, 1.0) for _ in range(n)]


def _ar1(rng: random.Random, n: int, rho: float):
    x = rng.gauss(0.0, 1.0 / math.sqrt(1.0 - rho * rho))              # stationary start: the true mean is exactly zero
    out = []
    for _ in range(n):
        x = rho * x + rng.gauss(0.0, 1.0)
        out.append(x)
    return out


def _slow_regime(rng: random.Random, n: int, mu: float = 0.3, p_stay: float = 0.99):
    """Two hidden states with means +mu and -mu (equally likely at the start, so the true mean is zero), unit noise, a mean dwell of 1 / (1 - p_stay) = 100 observations."""
    s = 1.0 if rng.random() < 0.5 else -1.0
    out = []
    for _ in range(n):
        if rng.random() > p_stay:
            s = -s
        out.append(mu * s + rng.gauss(0.0, 1.0))
    return out


def _rates(make, n: int, reps: int, seed: int):
    rng = random.Random(seed)
    hit05 = hit_h = 0
    for _ in range(reps):
        p = RH.significance(make(rng, n), ci=False)["p_one_sided"]
        hit05 += p <= 0.05
        hit_h += p <= HOLM12
    return hit05 / reps, hit_h / reps


@pytest.mark.parametrize("name,make", [("iid", _iid), ("ar0.5", lambda r, n: _ar1(r, n, 0.5))])
def test_the_p_value_is_not_anti_conservative_for_iid_and_autoregressive_nulls(name, make):
    r05, r_h = _rates(make, 100, 500, seed=11)
    assert r05 <= 0.075, (name, r05)                                    # stated 5%: measured about 4% to 5%
    assert r_h <= 0.02, (name, r_h)                                     # stated 0.42%: measured about 0.2% to 0.8%


def test_the_documented_limit_a_slow_regime_null_is_NOT_controlled_by_the_ess_t_p_value():
    """A hidden level that moves once per ~100 observations leaves almost no autocorrelation for the estimator to see (+-0.3 sd under unit noise), so the effective sample size is reported
    near n and the p-value is far too small: measured about 25% at the stated 5% and about 12% at the stated 0.42% (28 times), n = 100. This test pins that the limit is REAL and STATED; if the
    procedure is ever made robust to it, this test fails and the statements in the docs (Round 14) must be updated with it. It is not a licence to rely on the p-value on regime-driven data:
    declare a regime dwell before any holdout is read."""
    r05, r_h = _rates(_slow_regime, 100, 600, seed=12)
    assert r05 >= 0.12, r05                                             # a stated 5% level, measured about 25%
    assert r_h >= 0.04, r_h                                             # a stated 0.42% level, measured about 12%: at least nine times
