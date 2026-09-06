#!/usr/bin/env python3
"""Multiple-testing correction. The layer this programme never had.

THE HOLE THIS CLOSES
====================
Eleven signal families have been measured against a 95th-percentile bar.
One cleared. There is no multiple-testing correction anywhere in the tree.

`project_status.py:282` is scrupulous about exactly this reasoning for the
predecessor family, at search width THREE:

    "best-of-three at a 95th percentile occurs about 14% of the time under
     a global null"

That is correct: 1 - 0.95**3 = 0.1426. The same arithmetic at width ELEVEN
is 1 - 0.95**11 = 0.4312. A 43% chance that at least one of eleven families
clears a 95th-percentile bar when NONE of them has any edge at all.

`funding_carry_fade_btc_v1` cleared Stage-1 at that search width and then
lost money forward (n=2, mean net R -0.8358). Selection under a global null
is the single most parsimonious explanation of that sequence, and it is a
hypothesis this repository has never been able to state numerically,
because nothing counted the trials.

This module makes it statable. It does not re-score Stage-1, does not move
FUND_ABS, and writes nothing any gate reads.

WHAT IS IMPLEMENTED
===================
`family_wise_false_positive_rate`  1 - (1-alpha)**n_trials. The arithmetic
    the tree already trusts at width 3, available at any width.

`expected_max_sharpe_under_null`   E[max SR] over N independent trials of a
    zero-edge strategy (Bailey & Lopez de Prado 2014). This is the bar an
    observed Sharpe must clear before "it beat the null" means anything: with
    enough trials, the best of them is high BY CONSTRUCTION.

`deflated_sharpe_ratio`            The probability the true Sharpe exceeds
    zero, after correcting for (a) the number of trials, (b) the skew and
    kurtosis of the return stream, and (c) the sample length. Non-normal
    returns matter enormously here: a strategy with negative skew and fat
    tails - which a stop-and-target barrier strategy structurally has - needs
    a HIGHER observed Sharpe to reach the same confidence.

`minimum_track_record_length`      How many observations are needed before a
    given Sharpe could be called significant at all. Answers "is n=41 even
    capable of supporting this claim" without another year of waiting.

Reference: Bailey, D. and Lopez de Prado, M. (2014), "The Deflated Sharpe
Ratio: Correcting for Selection Bias, Backtest Overfitting and
Non-Normality", Journal of Portfolio Management 40(5).

HONEST LIMITS OF THIS MODULE
============================
* DSR assumes trials are independent. Eleven families over one asset, one
  venue and one four-year window are NOT independent - they share the same
  price path. Correlated trials mean the EFFECTIVE number of trials is lower
  than the raw count, so a raw-count DSR is CONSERVATIVE (it over-corrects).
  That is the safe direction, and it is the direction this module errs in
  deliberately. `effective_n_trials` gives a correlation-adjusted count for
  anyone who wants the less conservative reading; it must be justified in
  writing, not applied by default.
* A registry that starts today cannot recover trials nobody recorded. The
  seeded count of 11 is a FLOOR, not a census: every abandoned parameter
  grid, every variant tried and dropped, every re-run with a different
  window is a trial too. The true N is larger, so the true correction is
  harsher.
* Passing DSR does not make a strategy profitable. It removes one specific
  way of being fooled.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
from typing import Dict, List, Optional, Sequence

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

#: Euler-Mascheroni constant, used in the expected-maximum order statistic.
EULER_MASCHERONI = 0.5772156649015329


# --------------------------------------------------------------------------
# normal distribution helpers (no scipy dependency - scipy is optional here)
# --------------------------------------------------------------------------

def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def norm_ppf(p: float) -> float:
    """Inverse normal CDF. Acklam's rational approximation, |err| < 1.15e-9."""
    if not 0.0 < p < 1.0:
        raise ValueError(f"norm_ppf needs 0 < p < 1, got {p}")
    a = [-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02,
         1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00]
    b = [-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02,
         6.680131188771972e+01, -1.328068155288572e+01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00,
         -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00]
    d = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00,
         3.754408661907416e+00]
    plow, phigh = 0.02425, 1 - 0.02425
    if p < plow:
        q = math.sqrt(-2 * math.log(p))
        return (((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / \
               ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)
    if p > phigh:
        q = math.sqrt(-2 * math.log(1 - p))
        return -(((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / \
                ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)
    q = p - 0.5
    r = q * q
    return (((((a[0]*r+a[1])*r+a[2])*r+a[3])*r+a[4])*r+a[5])*q / \
           (((((b[0]*r+b[1])*r+b[2])*r+b[3])*r+b[4])*r+1)


# --------------------------------------------------------------------------
# the corrections
# --------------------------------------------------------------------------

def family_wise_false_positive_rate(n_trials: int, alpha: float = 0.05) -> float:
    """P(at least one of `n_trials` clears an `alpha` bar under a global null).

    The arithmetic project_status.py already applies at width 3.
    """
    if n_trials < 1:
        raise ValueError("n_trials must be >= 1")
    if not 0.0 < alpha < 1.0:
        raise ValueError("alpha must be in (0, 1)")
    return 1.0 - (1.0 - alpha) ** n_trials


def expected_max_sharpe_under_null(n_trials: int,
                                   sharpe_variance: float = 1.0) -> float:
    """E[max Sharpe] across `n_trials` zero-edge trials (Bailey & LdP eq. 5).

    `sharpe_variance` is the variance of the SHARPE ESTIMATOR across trials
    and sets the UNITS of the result. Pass 1.0 for a standardised (z-scale)
    answer; pass 1/(n-1) to get a bar in the same per-observation units as
    `sharpe_from_returns`. `deflated_sharpe_ratio` does the latter by
    default - mixing the two silently reports dsr = 0 for everything. The result is the bar an
    observed Sharpe must beat before "best of N" carries information.
    """
    if n_trials < 1:
        raise ValueError("n_trials must be >= 1")
    if n_trials == 1:
        return 0.0
    sd = math.sqrt(sharpe_variance)
    g = EULER_MASCHERONI
    term_a = (1.0 - g) * norm_ppf(1.0 - 1.0 / n_trials)
    term_b = g * norm_ppf(1.0 - 1.0 / (n_trials * math.e))
    return sd * (term_a + term_b)


def sharpe_from_returns(returns: Sequence[float]) -> float:
    n = len(returns)
    if n < 2:
        raise ValueError("need at least 2 observations")
    mean = sum(returns) / n
    var = sum((r - mean) ** 2 for r in returns) / (n - 1)
    if var <= 0:
        raise ValueError("zero variance: Sharpe undefined")
    return mean / math.sqrt(var)


def _moments(returns: Sequence[float]) -> Dict[str, float]:
    n = len(returns)
    mean = sum(returns) / n
    var = sum((r - mean) ** 2 for r in returns) / (n - 1)
    sd = math.sqrt(var)
    skew = (sum(((r - mean) / sd) ** 3 for r in returns) / n) if sd > 0 else 0.0
    kurt = (sum(((r - mean) / sd) ** 4 for r in returns) / n) if sd > 0 else 3.0
    return {"n": n, "mean": mean, "sd": sd, "skew": skew, "kurtosis": kurt}


def deflated_sharpe_ratio(returns: Sequence[float], n_trials: int,
                          sharpe_variance: Optional[float] = None
                          ) -> Dict[str, float]:
    """P(true Sharpe > 0) after correcting for selection and non-normality.

    Returns a dict with the observed Sharpe, the null bar it had to clear,
    the moments that shaped the correction, and `dsr` in [0, 1]. A `dsr`
    below 0.95 means the observed track record is NOT distinguishable from
    the best of `n_trials` lucky draws at the 95% level.
    """
    n = len(returns)
    if n < 2:
        raise ValueError("need at least 2 observations")
    m = _moments(returns)
    sr = sharpe_from_returns(returns)
    # UNITS MATTER HERE AND GETTING THEM WRONG SILENTLY BREAKS THE TEST.
    # `sr` is a PER-OBSERVATION Sharpe. `expected_max_sharpe_under_null`
    # returns a standardised order statistic (z units). They can only be
    # subtracted if the null bar is expressed in the same per-observation
    # units, which means scaling it by the standard error of the Sharpe
    # estimator - approximately 1/sqrt(n-1) under the null. Defaulting
    # `sharpe_variance` to 1.0 (as a naive reading of the paper suggests)
    # compares a per-observation Sharpe of ~0.3 against a z-scale bar of
    # ~1.66 and reports dsr = 0 for EVERY strategy, which looks like
    # rigour and is actually a broken instrument.
    if sharpe_variance is None:
        sharpe_variance = 1.0 / (n - 1)
    sr0 = expected_max_sharpe_under_null(n_trials, sharpe_variance)
    # Denominator: standard error of the Sharpe estimator under non-normal
    # returns. Negative skew and excess kurtosis INFLATE it, which is the
    # whole point - a stop-and-target strategy is structurally negatively
    # skewed and must clear a higher bar.
    denom_sq = 1.0 - m["skew"] * sr + ((m["kurtosis"] - 1.0) / 4.0) * sr * sr
    if denom_sq <= 0:
        denom_sq = 1e-12
    z = (sr - sr0) * math.sqrt(n - 1) / math.sqrt(denom_sq)
    return {
        "observed_sharpe": sr,
        "expected_max_sharpe_under_null": sr0,
        "n_observations": float(n),
        "n_trials": float(n_trials),
        "skew": m["skew"],
        "kurtosis": m["kurtosis"],
        "dsr": norm_cdf(z),
        "z": z,
    }


def minimum_track_record_length(observed_sharpe: float, skew: float,
                                kurtosis: float, target_sharpe: float = 0.0,
                                confidence: float = 0.95) -> float:
    """Observations needed before `observed_sharpe` could be significant.

    Answers "could a sample this small ever support this claim?" - which for
    n=41 is worth asking before waiting another year for n=20 forward.
    """
    if observed_sharpe <= target_sharpe:
        return float("inf")
    denom_sq = (1.0 - skew * observed_sharpe
                + ((kurtosis - 1.0) / 4.0) * observed_sharpe ** 2)
    if denom_sq <= 0:
        denom_sq = 1e-12
    z = norm_ppf(confidence)
    return 1.0 + denom_sq * (z / (observed_sharpe - target_sharpe)) ** 2


def effective_n_trials(n_trials: int, mean_correlation: float) -> float:
    """Correlation-adjusted trial count. MUST be justified in writing.

    Eleven families over one asset and one window share a price path, so the
    raw count over-corrects. This gives the less conservative number, using
    the standard effective-sample-size form. Using it without a written
    argument for `mean_correlation` is how a correction gets tuned away.
    """
    if not 0.0 <= mean_correlation < 1.0:
        raise ValueError("mean_correlation must be in [0, 1)")
    if n_trials < 1:
        raise ValueError("n_trials must be >= 1")
    return n_trials / (1.0 + (n_trials - 1) * mean_correlation)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--n-trials", type=int, default=11,
                    help="search width (default 11: the families in this tree)")
    ap.add_argument("--alpha", type=float, default=0.05)
    ap.add_argument("--returns-json", default="",
                    help="JSON file containing a list of per-trade net R")
    args = ap.parse_args(argv)

    fw = family_wise_false_positive_rate(args.n_trials, args.alpha)
    print(f"search width                     {args.n_trials}")
    print(f"per-family bar                   {1 - args.alpha:.0%} percentile")
    print(f"P(>=1 clears under global null)  {fw:.2%}")
    print(f"E[max Sharpe] under null         "
          f"{expected_max_sharpe_under_null(args.n_trials):.4f}")
    if fw > args.alpha:
        print("\nREADING: at this search width, clearing the bar once is not "
              "evidence of edge.\n        It is what a global null produces "
              f"{fw:.0%} of the time.")
    if args.returns_json:
        with open(args.returns_json, encoding="utf-8") as fh:
            returns = json.load(fh)
        out = deflated_sharpe_ratio(returns, args.n_trials)
        print()
        for k, v in out.items():
            print(f"{k:32s} {v:.6f}")
        print("\nVERDICT:", "SURVIVES its own search width"
              if out["dsr"] >= 0.95 else
              "DOES NOT survive its own search width (dsr < 0.95)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
