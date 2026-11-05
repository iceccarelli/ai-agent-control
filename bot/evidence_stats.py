"""Honest statistics for serially dependent market evidence. Pure standard library.

Five-minute snapshots of one funding forecast are NOT independent observations: the forecast
moves slowly, and every snapshot between two settlements shares the same settlement. So this
module reports four different numbers and never lets the largest pass for the evidence:

  raw snapshots            how many rows exist
  unique settlements       how many distinct funding clocks they span
  settlement events        settlements with a usable observation. These are SERIALLY DEPENDENT (funding
                           is persistent), so this count is an upper bound on independent draws, not
                           the number of them; the ESS below is the figure that gates a claim
  effective sample size    n / (1 + 2 * sum of positive autocorrelations), of the per-settlement
                           series: how many independent draws the dependence leaves

and gives confidence intervals from a moving-block bootstrap with a fixed seed whose block length
follows the measured persistence, widened to an ESS-adjusted t interval for the mean. Under 8
points, an ESS under 3, or a constant series, no interval is offered: a wide-looking number from 5
points, or a zero-width one from a constant, is a false precision.
"""
from __future__ import annotations

import math
import random
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
    if len(set(xs)) == 1 or (max(xs) - min(xs)) <= 1e-12 * max(1.0, abs(sum(xs) / n)):
        return 1.0
    s = 0.0
    for k in range(1, n // 2 + 1):
        rho = autocorr(xs, k)
        if rho is None or rho <= 0:
            break
        s += (1 - k / n) * rho
    return max(1.0, min(float(n), n / (1.0 + 2.0 * s)))


def _mean(v: Sequence[float]) -> float:
    return sum(v) / len(v)


def _t_crit(df: float, z: float = 1.959964) -> float:
    """Two-sided 95 % Student-t critical value by the Cornish-Fisher expansion (stdlib only);
    adequate for df >= 2, and it never returns less than the normal value."""
    df = max(df, 2.0)
    return z + (z ** 3 + z) / (4 * df) + (5 * z ** 5 + 16 * z ** 3 + 3 * z) / (96 * df * df)


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
    independent draws it really rests on, and ``funding_eval`` / ``opportunity_engine`` refuse
    to use an interval whose ESS is below their floor. A constant series has no variance to
    resample: it gets NO interval (a zero-width interval is not a bound)."""
    n = len(xs)
    if n < MIN_FOR_CI:
        return {"status": "INSUFFICIENT", "n": n, "reason": f"fewer than {MIN_FOR_CI} settlement-level points"}
    m = _mean(xs)
    var = sum((x - m) ** 2 for x in xs) / (n - 1)
    if var <= 0 or (max(xs) - min(xs)) <= 1e-12 * max(1.0, abs(m)):      # constant to float precision
        return {"status": "NO_VARIANCE", "n": n, "estimate": m, "ess": 1.0,
                "reason": "constant series: nothing to resample, and a zero-width interval would be a false bound"}
    ess = effective_sample_size(xs) or 1.0
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
    if mean_stat and abs(alpha - 0.05) < 1e-12:
        half = _t_crit(ess - 1.0) * math.sqrt(var / ess)
        out["ess_t_lo"], out["ess_t_hi"] = m - half, m + half
        lo, hi = min(lo, m - half), max(hi, m + half)
        out["method"] = "wider of the moving-block bootstrap and the ESS-adjusted t interval"
    else:
        out["method"] = "moving-block bootstrap only"
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
    ess = effective_sample_size(per_settlement)
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
