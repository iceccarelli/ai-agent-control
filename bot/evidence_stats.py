"""Honest statistics for serially dependent market evidence. Pure standard library.

Five-minute snapshots of one funding forecast are NOT independent observations: the forecast
moves slowly, and every snapshot between two settlements shares the same settlement. So this
module reports four different numbers and never lets the largest pass for the evidence:

  raw snapshots            how many rows exist
  unique settlements       how many distinct funding clocks they span
  independent events       settlements with a usable observation (what a test may count)
  effective sample size    n / (1 + 2 * sum of positive autocorrelations), of the per-settlement
                           series: how many independent draws the dependence leaves

and gives confidence intervals only from a moving-block bootstrap with a fixed seed, which
preserves the dependence the naive standard error ignores. Under 8 independent points no
interval is offered: a wide-looking number from 5 points is a false precision.
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
    if len(set(xs)) == 1:
        return 1.0
    s = 0.0
    for k in range(1, n // 2 + 1):
        rho = autocorr(xs, k)
        if rho is None or rho <= 0:
            break
        s += (1 - k / n) * rho
    return max(1.0, min(float(n), n / (1.0 + 2.0 * s)))


def block_bootstrap_ci(xs: Sequence[float], stat: Callable[[Sequence[float]], float] = lambda v: sum(v) / len(v),
                       *, B: int = 2000, seed: int = DEFAULT_SEED, alpha: float = 0.05,
                       block: Optional[int] = None) -> Dict[str, Any]:
    """Moving-block bootstrap percentile interval. Deterministic for a given seed."""
    n = len(xs)
    if n < MIN_FOR_CI:
        return {"status": "INSUFFICIENT", "n": n, "reason": f"fewer than {MIN_FOR_CI} independent points"}
    L = block or max(1, round(n ** (1.0 / 3.0)))
    rng = random.Random(seed)
    starts = n - L + 1
    nblocks = math.ceil(n / L)
    vals: List[float] = []
    for _ in range(B):
        sample: List[float] = []
        for _b in range(nblocks):
            i = rng.randrange(starts)
            sample.extend(xs[i:i + L])
        vals.append(stat(sample[:n]))
    vals.sort()
    lo = vals[int((alpha / 2) * (B - 1))]
    hi = vals[int((1 - alpha / 2) * (B - 1))]
    return {"status": "OK", "n": n, "block": L, "B": B, "seed": seed, "alpha": alpha,
            "estimate": stat(list(xs)), "lo": lo, "hi": hi}


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
        "reading": "snapshots are correlated; only independent events count toward any claim",
    }
