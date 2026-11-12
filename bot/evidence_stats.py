"""Honest statistics for serially dependent market evidence. Pure standard library.

Five-minute snapshots of one funding forecast are NOT independent observations: the forecast
moves slowly, and every snapshot between two settlements shares the same settlement. So this
module reports four different numbers and never lets the largest pass for the evidence:

  raw snapshots            how many rows exist
  unique settlements       how many distinct funding clocks they span
  settlement events        settlements with a usable observation. These are SERIALLY DEPENDENT (funding
                           is persistent), so this count is an upper bound on independent draws, not
                           the number of them; the ESS below is the figure that gates a claim
  effective sample size    the LOWER of n / (1 + 2 * sum of positive autocorrelations) and the ESS
                           implied by the variance of batch means: how many independent draws the
                           dependence leaves

and gives confidence intervals from a moving-block bootstrap with a fixed seed whose block length
follows the measured persistence, widened to an ESS-adjusted t interval for the mean. Under 8
points, an ESS under 3, or a constant series, no interval is offered: a wide-looking number from 5
points, or a zero-width one from a constant, is a false precision.
"""
from __future__ import annotations

import math
import random
from operator import mul as _mul
from statistics import NormalDist
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

MIN_FOR_CI = 8
DEFAULT_SEED = 20261105


def mean(xs: Sequence[float]) -> Optional[float]:
    return sum(xs) / len(xs) if xs else None


def autocorr(xs: Sequence[float], k: int) -> Optional[float]:
    n = len(xs)
    if n < k + 2:
        return None
    m = sum(xs) / n
    den = sum((x - m) ** 2 for x in xs)
    if den <= 0:
        return None
    return sum((xs[i] - m) * (xs[i + k] - m) for i in range(n - k)) / den


def effective_sample_size(xs: Sequence[float]) -> Optional[float]:
    """n / (1 + 2 * sum_k (1 - k/n) rho_k), summed over leading positive autocorrelations only.
    Clamped to [1, n]. A constant series carries one independent value."""
    n = len(xs)
    if n < 2:
        return float(n) if n else None
    mean_ = sum(xs) / n
    if len(set(xs)) == 1 or (max(xs) - min(xs)) <= 1e-12 * max(1.0, abs(mean_)):
        return 1.0
    xc = [x - mean_ for x in xs]                      # centred once; each lag is then one C-level pass
    den = sum(map(_mul, xc, xc))
    if den <= 0:
        return 1.0
    s = 0.0
    for k in range(1, n // 2 + 1):
        if n < k + 2:
            break
        rho = sum(map(_mul, xc, xc[k:])) / den
        if rho <= 0:
            break
        s += (1 - k / n) * rho
    return max(1.0, min(float(n), n / (1.0 + 2.0 * s)))


def _mean(v: Sequence[float]) -> float:
    return sum(v) / len(v)


def _betacf(a: float, b: float, x: float) -> float:
    """Continued fraction of the incomplete beta function (modified Lentz). It converges in tens of terms for every argument the t quantile reaches; failing to converge is an
    error, never a number."""
    tiny = 1e-300
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    d = 1.0 / (d if abs(d) > tiny else tiny)
    h = d
    for m in range(1, 5000):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        d = 1.0 / (d if abs(d) > tiny else tiny)
        c = 1.0 + aa / c
        c = c if abs(c) > tiny else tiny
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        d = 1.0 / (d if abs(d) > tiny else tiny)
        c = 1.0 + aa / c
        c = c if abs(c) > tiny else tiny
        de = d * c
        h *= de
        if abs(de - 1.0) < 3e-16:
            return h
    raise ArithmeticError("the incomplete beta continued fraction did not converge")


def _betainc(a: float, b: float, x: float) -> float:
    """Regularised incomplete beta function I_x(a, b)."""
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    front = math.exp(math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b) + a * math.log(x) + b * math.log1p(-x))
    if x < (a + 1.0) / (a + b + 2.0):
        return front * _betacf(a, b, x) / a
    return 1.0 - front * _betacf(b, a, 1.0 - x) / b


#: At and above this many degrees of freedom the three-term Cornish-Fisher expansion stands in for the exact evaluation: its first neglected term is about 3e-11 at 1e4 degrees of
#: freedom (alpha 0.003 to 0.10, shrinking as df^-3), while the direct evaluation loses digits to the cancellation in ``lgamma(a + b) - lgamma(a) - lgamma(b)`` (about 2.5e-11 at
#: 1e4, 1.4e-10 at 1e5 and 1.5e-9 at 1e6), which would also make the value wobble in df.
_T_EXPANSION_DF = 1e4


def _t_crit(df: float, alpha: float = 0.05) -> float:
    """The two-sided Student-t critical value: the t > 0 with P(|T| > t) = alpha for any real df > 0 (stdlib only), never below the normal value. Intended for confidence levels
    of 50% and above (alpha <= 0.5): the repository uses 0.10, 0.05 and the family-wise 0.003125. Measured against scipy (alpha from 1e-6 to 0.5) it agrees to about 2e-12
    (relative) up to 1e3 degrees of freedom, 4e-11 up to 1e4 and 2e-10 above it.

    It was a Cornish-Fisher expansion clamped at df >= 2, and that is anti-conservative exactly where the intervals lean on it: 2.762 for the 90% interval at df = 2 (the
    3-batch interval) where the table says 2.920 (the old value was 5.4% too small; 10.5% at 95%, 27% at alpha 0.01, 40.7% at the opportunity engine's family-wise alpha),
    3.0% too small at df 2.5, 1.8% at 3, 0.8% at 4, and at df = 1 (the 2 batches a sample of 8 to 11 gets) the clamp returned the df = 2 value where the table says 6.314.
    A critical value that is too small is an interval that is too narrow."""
    try:
        df_, alpha_ = float(df), float(alpha)
    except (TypeError, ValueError):
        raise ValueError(f"df and alpha must be numbers, got {df!r} and {alpha!r}") from None
    if not df_ > 0.0:                                                   # also false for NaN
        raise ValueError(f"the degrees of freedom must be positive, got {df!r}")
    if not 0.0 < alpha_ < 1.0:
        raise ValueError(f"alpha must be in (0, 1), got {alpha!r}")
    z = -NormalDist().inv_cdf(alpha_ / 2.0)                             # not inv_cdf(1 - alpha / 2): that rounds to 1 for a tiny alpha and loses the tail
    if df_ >= _T_EXPANSION_DF:
        return z + (z ** 3 + z) / (4.0 * df_) + (5 * z ** 5 + 16 * z ** 3 + 3 * z) / (96.0 * df_ * df_)

    def tail(t: float) -> float:
        return _betainc(df_ / 2.0, 0.5, df_ / (df_ + t * t))            # P(|T| > t)

    lo, hi = z, 2.0 * z                                                 # a t tail is heavier than the normal one, so the root lies above z
    for _ in range(400):
        if tail(hi) <= alpha_:
            break
        lo, hi = hi, hi * 2.0
    else:
        raise ValueError(f"alpha {alpha!r} is too small for a critical value to be found at df {df!r}")
    for _ in range(200):
        mid = 0.5 * (lo + hi)
        if tail(mid) > alpha_:
            lo = mid
        else:
            hi = mid
        if hi - lo <= 1e-14 * hi:
            break
    return 0.5 * (lo + hi)


BATCHES = 6


def batch_means_ess(xs: Sequence[float]) -> Optional[float]:
    """ESS implied by the variance of NON-OVERLAPPING BATCH MEANS (the most recent K*b points,
    K <= 6). It needs no assumption about the shape of the autocorrelation, so it is not fooled by a
    seasonal cycle (three settlements a day) that makes the lag-1 autocorrelation negative while a
    persistent level still dominates the mean's variance. It is noisy at small K, which is why it is only
    ever combined with the acf estimate by taking the LOWER of the two."""
    n = len(xs)
    k = min(BATCHES, n // 4)
    if k < 2:
        return None
    b = n // k
    tail = list(xs[n - k * b:])
    bm = [sum(tail[i * b:(i + 1) * b]) / b for i in range(k)]
    m = sum(tail) / len(tail)
    s2 = sum((x - m) ** 2 for x in tail) / (len(tail) - 1)
    mbar = sum(bm) / k
    var_bm = sum((v - mbar) ** 2 for v in bm) / (k - 1)
    if s2 <= 0:
        return 1.0
    if var_bm <= 0:
        return float(n)
    return max(1.0, min(float(n), s2 / (var_bm / k)))


def conservative_ess(xs: Sequence[float]) -> Optional[float]:
    """The LOWER of the autocorrelation-sum ESS and the batch-means ESS. Used wherever an ESS gates a
    claim or sizes an interval."""
    a = effective_sample_size(xs)
    b = batch_means_ess(xs)
    if a is None:
        return b
    return a if b is None else min(a, b)


def block_bootstrap_ci(xs: Sequence[float], stat: Optional[Callable[[Sequence[float]], float]] = None,
                       *, B: int = 2000, seed: int = DEFAULT_SEED, alpha: float = 0.05,
                       block: Optional[int] = None) -> Dict[str, Any]:
    """Confidence interval for the MEAN (or, with ``stat``, a bootstrap-only interval for another
    statistic). Deterministic for a given seed.

    Serial dependence is what makes a naive interval overconfident, so this does not trust one
    method: the moving-block bootstrap uses a block length tied to the measured persistence
    (``2 * n / ESS``, never shorter than n^(1/3)), and for the mean the result is the WIDER of
    that and an effective-sample-size t interval. Even so, a short, persistent series stays
    under-covered: the ESS is reported with every interval so the caller can see how many
    independent draws it really rests on. ``funding_eval`` (and so ``opportunity_engine``'s carry
    evaluator) refuses an interval whose ESS is below its floor; the other ``opportunity_engine``
    evaluators floor sample COUNTS only. COVERAGE IS NOMINAL, not delivered, under strong
    persistence: the Round 14 audit measured the two-sided miscoverage of the nominal 90% interval
    at 40% / 28% / 17% / 8.9% / 5.2% for AR(0.95) at n = 24 / 48 / 100 / 250 / 1000 (a two-state
    regime and a seasonal-masked level are similar), and about five times the stated one-sided rate
    at the family-wise alpha for a regime at n of 12 to 48; iid, heavy-tailed and volatility-clustered
    series are conservative. A constant series has no variance to
    resample: it gets NO interval (a zero-width interval is not a bound)."""
    n = len(xs)
    if n < MIN_FOR_CI:
        return {"status": "INSUFFICIENT", "n": n, "reason": f"fewer than {MIN_FOR_CI} settlement-level points"}
    m = _mean(xs)
    var = sum((x - m) ** 2 for x in xs) / (n - 1)
    if var <= 0 or (max(xs) - min(xs)) <= 1e-12 * max(1.0, abs(m)):      # constant to float precision
        return {"status": "NO_VARIANCE", "n": n, "estimate": m, "ess": 1.0,
                "reason": "constant series: nothing to resample, and a zero-width interval would be a false bound"}
    ess = conservative_ess(xs) or 1.0
    if ess < 3.0:
        return {"status": "INSUFFICIENT", "n": n, "ess": ess, "estimate": m,
                "reason": "effective sample size below 3: the series carries too few independent draws for any interval"}
    mean_stat = stat is None
    f = stat or _mean
    tau = n / ess
    L = block or min(max(1, n // 2), max(round(n ** (1.0 / 3.0)), math.ceil(2.0 * tau)))
    rng = random.Random(seed)
    starts = n - L + 1
    nblocks = math.ceil(n / L)
    vals: List[float] = []
    for _ in range(B):
        sample: List[float] = []
        for _b in range(nblocks):
            i = rng.randrange(starts)
            sample.extend(xs[i:i + L])
        vals.append(f(sample[:n]))
    vals.sort()
    lo = vals[int((alpha / 2) * (B - 1))]
    hi = vals[int((1 - alpha / 2) * (B - 1))]
    out: Dict[str, Any] = {"status": "OK", "n": n, "ess": ess, "block": L, "B": B, "seed": seed, "alpha": alpha,
                           "estimate": f(list(xs)), "bootstrap_lo": lo, "bootstrap_hi": hi}
    if mean_stat:
        half = _t_crit(ess - 1.0, alpha) * math.sqrt(var / ess)
        out["ess_t_lo"], out["ess_t_hi"] = m - half, m + half
        lo, hi = min(lo, m - half), max(hi, m + half)
        for k in sorted({min(BATCHES, n // 4), min(3, n // 4)}):          # 6 and 3 batches: the fewer, the less
            if k < 2:                                                   # the shape of the dependence is assumed
                continue
            b_ = n // k
            tail = list(xs[n - k * b_:])
            bm = [sum(tail[i * b_:(i + 1) * b_]) / b_ for i in range(k)]
            bmm = sum(bm) / k
            se_bm = math.sqrt(sum((v - bmm) ** 2 for v in bm) / (k - 1) / k)
            half_bm = _t_crit(k - 1.0, alpha) * se_bm
            out[f"batch{k}_lo"], out[f"batch{k}_hi"] = m - half_bm, m + half_bm
            lo, hi = min(lo, m - half_bm), max(hi, m + half_bm)
        out["method"] = "widest of the moving-block bootstrap, the ESS-adjusted t interval and the 6- and 3-batch-means t intervals"
    else:
        out["method"] = "moving-block bootstrap only (a custom statistic has no analytic cross-check)"
    out["lo"], out["hi"] = lo, hi
    return out


def settlement_groups(rows: Sequence[Mapping[str, Any]]) -> Dict[int, List[Mapping[str, Any]]]:
    out: Dict[int, List[Mapping[str, Any]]] = {}
    for r in rows:
        ns = (r.get("funding") or {}).get("next_settlement_ms")
        if isinstance(ns, (int, float)):
            out.setdefault(int(ns), []).append(r)
    return out


def independence_report(rows: Sequence[Mapping[str, Any]], *, regime_threshold_bps: float = 0.0) -> Dict[str, Any]:
    """Raw vs unique vs independent vs effective, plus per-settlement and per-day blocks and the
    sign transitions of the per-settlement mean forecast."""
    groups = settlement_groups(rows)
    keys = sorted(groups)
    per_settlement = []
    for k in keys:
        fc = [r["funding"]["forecast_bps"] for r in groups[k]]
        per_settlement.append(sum(fc) / len(fc))
    ess = conservative_ess(per_settlement)
    # forecast changes across a settlement boundary are the only independent information
    transitions = sum(1 for a, b in zip(per_settlement, per_settlement[1:])
                      if (a > regime_threshold_bps) != (b > regime_threshold_bps))
    days: Dict[int, List[float]] = {}
    for r in rows:
        days.setdefault(int(r["at_ms"] // 86_400_000), []).append(r["funding"]["forecast_bps"])
    time_blocks = [{"utc_day_index": d, "n": len(v), "mean_forecast_bps": sum(v) / len(v)} for d, v in sorted(days.items())]
    return {
        "raw_snapshots": len(rows), "unique_settlements": len(keys),
        "independent_events": len(per_settlement),
        "effective_sample_size": ess,
        "snapshots_per_settlement_mean": (len(rows) / len(keys)) if keys else None,
        "per_settlement_mean_forecast_bps": per_settlement,
        "mean_forecast_ci": block_bootstrap_ci(per_settlement),
        "regime_sign_transitions": transitions, "regime_threshold_bps": regime_threshold_bps,
        "time_blocks": time_blocks,
        "reading": "snapshots are correlated and settlements are serially dependent; the effective sample size, not the settlement count, says how much evidence there is",
    }
