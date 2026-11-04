#!/usr/bin/env python3
"""Overlay economics, prospective protocol v3 (schema `overlay_economics/v3`).

WHY A NEW PROTOCOL. `overlay_economics` (schema `overlay_economics/1`, "v2" in the
project's own numbering) is merged, pinned and frozen; this module does not edit
it. A red team of that rule, and then of the first draft of THIS one, produced
unjustified GOs with seeded Monte Carlo on the repo's own helpers:

  * persistent funding regimes: 22% false GO on a zero-edge process (nominal 5%)
    for v2; for the first draft of v3, 8% at a 35-day regime dwell, 17% at 70
    days, 27% at a 150-day OU half-life. A calendar block is NOT independent
    when the thing that drives it persists for longer than the block, and the
    lag-1 correlation of ~9 noisy block values cannot see that.
  * weekly looks (optional stopping); a rare large loss sized for zero mean (55%
    at n=30); a point-estimate benchmark; zero-variance and replayed samples;
    unpriced episodes credited with a positive value; a frozen price defeated
    by jitter; a hedge gate that passed a hedge removing 40% of the variance; a
    3h "due" tolerance that allowed two analyses; blocks anchored to the first
    row, so a late collector was judged on fewer blocks.

THE RULE (declared 2026-11-03, before any observation it judges):
  1. the unit is a fixed 14-day calendar BLOCK anchored to the DECLARED start
     (a late or gappy collection is INSUFFICIENT, never re-anchored); each
     episode is allocated to blocks pro rata by days, idle days in the denominator
  2. PERSISTENCE IS MEASURED ON THE DRIVER, not on the noisy block values: the
     lag-1 autocorrelation of the DAILY funding series, carried over a block,
     gives an effective number of independent blocks K_eff = K(1-rho)/(1+rho);
     the interval uses K_eff, and fewer than MIN_EFFECTIVE_BLOCKS is INSUFFICIENT
     ("the sample is too persistent for its length"). The single analysis is due
     at 252 days (18 blocks) for exactly this reason.
  3. ONE analysis, at the declared horizon (one observation interval of tolerance);
     rows after it are excluded; earlier looks are INSUFFICIENT (not yet due)
  4. an unseen tail is charged: the stress loss, taken as at least twice the worst
     observed episode, times the exact Poisson 95% bound on how often such a loss
     could occur given k seen (rule of three if none). A mirrored credit of
     unseen UPSIDE must be exceeded before any NO_GO.
  5. an episode that cannot be priced is valued at the full stress loss for the
     GO path (and at the best priced rate for NO_GO), never at "the worst priced"
  6. the benchmark is by uncertainty, with five separate facets: positive carry
     after costs; the combined position's absolute return (idle time unhedged);
     the increment versus passive; risk-adjusted versus passive; cash (none)
     versus simulated marks. GO needs the hedge shown to work (per closed
     episode, basis residual SD / price-move SD, bootstrap upper95 < 0.5) and the
     increment not significantly negative
  7. degenerate samples are refused by name (constant block values or passive
     return, a frozen or near-frozen price, a constant basis, a repeated funding
     sequence, too few distinct funding values)
  8. a capital basis is always stated; capital is charged on the committed
     fraction scaled by the mean price growth; a materiality floor in USD per
     year is an owner input; the stress inputs have floors
Everything is SIMULATED_FROM_PUBLIC_DATA. Nothing here is cash, a fill or a P&L.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import random
import sys
from typing import Any, Dict, List, Optional, Sequence, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
if os.path.dirname(HERE) not in sys.path:
    sys.path.insert(0, os.path.dirname(HERE))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import mainnet_public as mp                                     # noqa: E402
import overlay_economics as v2                                  # noqa: E402  (read only)

SCHEMA = "overlay_economics/v3"
UNAVAILABLE = v2.UNAVAILABLE
INSUFFICIENT_OWNER_INPUT = v2.INSUFFICIENT_OWNER_INPUT
SIMULATED = v2.SIMULATED

#: The window opens 2026-11-11 00:00 UTC. The rule was fixed in the commit that
#: introduces this file (2026-11-04) and the window opens a week later, so no
#: observation it judges existed when the rule was last changed. If this file is
#: merged AFTER that instant, EVALUATION_START_S must be re-declared (the hash
#: changes) so that the window still opens after the rule.
EVALUATION_START_S = 1_794_355_200
BLOCK_DAYS = 14
#: The single analysis is due at this horizon; rows after it are excluded.
ANALYSIS_HORIZON_DAYS = 252                      # eighteen complete blocks
N_BLOCKS = ANALYSIS_HORIZON_DAYS // BLOCK_DAYS
MAX_START_DELAY_S = 24 * 3600                    # the collection must begin on time
MIN_BLOCK_COVERAGE = 0.90                        # share of a block's expected rows present
MIN_EFFECTIVE_BLOCKS = 4.0
MIN_CLOSED_EPISODES = 20
MAX_UNPRICED_FRACTION = 0.10
HEDGE_RESIDUAL_RATIO_MAX = 0.5
MIN_STRESS_LOSS_BPS = 100.0
MAX_TAKER_FEE_BPS = 100.0
#: Degenerate-sample thresholds.
MIN_PASSIVE_SD_BPS_PER_DAY = 0.5
MIN_NET_SD_BPS_PER_DAY = 1e-6
MIN_HOURLY_PRICE_RETURN_SD = 1e-5
PRICE_FROZEN_SHARE = 0.95
REPEAT_WINDOW_PRINTS = 24
MIN_DISTINCT_WINDOW_SHARE = 0.90
MIN_DISTINCT_PRINT_VALUES = 8
MIN_DAILY_POINTS_FOR_PERSISTENCE = 30
MAX_DAILY_PERSISTENCE = 0.99
PERSISTENCE_UPPER_Z = 1.645
LONG_RUN_BANDWIDTH_DAYS = 42        # Bartlett window of the long-run-variance K_eff
STRESS_FUNDING_PRINTS = 3.0         # stress loss = this many cap-sized funding prints ...
WORST_LOSS_MULTIPLE = 2.0           # ... and never below this multiple of the worst loss seen
CAPACITY_BUDGET_DIVISOR = 2.0
MIN_ROWS_FOR_FROZEN_CHECK = 10
MIN_PRINTS_FOR_DISTINCT_CHECK = 24
REPEAT_TOLERANCE_BPS = 0.01         # two 24-print windows within this of each other are one
MIN_TAKER_FEE_BPS = 1.0
MAX_STRESS_FUNDING_CAP_BPS = 10_000.0
IMPLEMENTATION_VERSION = "3.1.0"    # bumped with any change to the logic, not only the numbers
BOOT_SEED = 20261104
BOOT_N = 4000

PROTOCOL = {
    "schema": SCHEMA,
    "declared": ("rule fixed 2026-11-04 in the commit that introduces it; the window opens "
                 "2026-11-11 00:00 UTC, after the rule; re-declare the start if merged later"),
    "supersedes": "nothing: overlay_economics/1 is preserved and keeps its meaning",
    "mode": "overlay: two perp legs only; the owner's BTC is never traded",
    "question": ("Does the frozen entry gate and exit rule earn a positive NET carry "
                 "per calendar day, after measured costs, an unseen-tail charge and "
                 "the owner's cost of capital, without the hedge adding risk to the "
                 "BTC the owner already holds, over a sample long enough, relative "
                 "to how persistent funding is, to mean anything, and is it material?"),
    "unit_of_independence": (f"a non-overlapping {BLOCK_DAYS}-day calendar block, "
                             "discounted for the persistence of the funding series itself"),
    "evaluation_start_s": EVALUATION_START_S,
    "analysis": ("ONE analysis, due when the first observation at or after "
                 "analysis_horizon_days exists; it uses only rows up to the horizon, so "
                 "every look after that one reads identical inputs; earlier looks are "
                 "INSUFFICIENT (ANALYSIS_NOT_YET_DUE)"),
    "analysis_horizon_days": ANALYSIS_HORIZON_DAYS,
    "block_days": BLOCK_DAYS,
    "blocks": N_BLOCKS,
    "collection": {"max_start_delay_s": MAX_START_DELAY_S,
                   "min_block_coverage": MIN_BLOCK_COVERAGE,
                   "note": "blocks are anchored to evaluation_start_s, never to the first row"},
    "adequacy": {"min_effective_blocks": MIN_EFFECTIVE_BLOCKS,
                 "min_closed_episodes": MIN_CLOSED_EPISODES},
    "max_unpriced_fraction": MAX_UNPRICED_FRACTION,
    "inference": ("one-sided 95% bound on the mean block value with the effective "
                  "number of independent blocks K_eff = K(1-rho)/(1+rho), rho = the "
                  "bias-corrected lag-1 autocorrelation of the daily funding series "
                  "raised to the block length; Student-t with floor(K_eff) values"),
    "tail": ("stress_loss_bps = max(3 x funding cap + price band, 2 x the worst observed "
             "episode loss) times max(0, upper95 Poisson frequency of such losses - "
             "observed frequency), per calendar day; an episode that cannot be priced "
             "is valued at the full stress loss on the GO path"),
    "upside": ("NO_GO requires the best-case upper bound to stay at or below zero AFTER "
               "crediting an unseen upside of (rule of three / n) x max(best observed "
               "episode, stress loss)"),
    "benchmark": ("GO needs: lower95 of the net carry per day > 0 after the tail charge; "
                  f"the HEDGE shown to work (per closed episode, SD of the basis residual / "
                  f"SD of the price move hedged, bootstrap upper95 < {HEDGE_RESIDUAL_RATIO_MAX}); "
                  "and the incremental return versus passive NOT significantly negative "
                  "(upper95 >= 0). The block-level SD ratio and the risk-adjusted "
                  "difference are reported with their uncertainty and do not gate."),
    "degenerate": ["BLOCK_VALUES_CONSTANT", "PASSIVE_RETURN_CONSTANT", "PRICE_FEED_FROZEN",
                   "BASIS_CONSTANT", "FUNDING_SEQUENCE_REPEATED",
                   "TOO_FEW_DISTINCT_FUNDING_VALUES"],
    "capital": ("a basis is always stated; charged on committed_capital_fraction x "
                "notional x the mean price growth since entry; a materiality floor in "
                "USD per year is required"),
    "inputs_validated": {"taker_fee_bps": [MIN_TAKER_FEE_BPS, MAX_TAKER_FEE_BPS],
                         "funding_cap_bps": [0.0, MAX_STRESS_FUNDING_CAP_BPS],
                         "price_limit_ratio": [0.0, 1.0], "committed_capital_fraction": [0.0, 1.0],
                         "min_stress_loss_bps": MIN_STRESS_LOSS_BPS},
    "verdicts": ["GO", "NO_GO", "INSUFFICIENT"],
    "meaning": {
        "GO": ("Over one pre-declared, fully covered window long enough for how "
               "persistent funding proved to be, the dependence-adjusted lower 95% "
               "bound of the net carry per calendar day, after execution costs, an "
               "unseen-tail charge and the owner's cost of capital, is above zero and "
               "above the owner's materiality floor; the hedge removed most of the "
               "price risk and did not significantly lose wealth versus holding the "
               "BTC; capacity, drawdown and margin facts hold. Evidence for a small "
               "pilot under the owner's signed charter, not a forecast, not cash."),
        "NO_GO": ("Adequate sample, one analysis, and even the most favourable reading "
                  "(lower impact bound, zero capital cost, an unseen-upside credit) has "
                  "an upper 95% bound at or below zero."),
        "INSUFFICIENT": ("Anything else: unreadable, late, uncovered or degenerate "
                         "evidence, analysis not yet due, a sample too persistent for "
                         "its length, a missing or invalid owner input, or a result "
                         "that is neither clearly positive nor clearly absent."),
    },
    "never": ["a verdict before the analysis date", "re-testing the same window as it grows",
              "treating a missing owner input as zero", "calling a simulation cash",
              "extrapolating cost beyond the measured sizes", "re-anchoring blocks to a late start"],
    "cash": False,
    "resolved_parameters": {
        "block_days": BLOCK_DAYS, "n_blocks": N_BLOCKS, "boot": [BOOT_SEED, BOOT_N],
        "due": "the first observation at or after the horizon makes the one analysis due",
        "thresholds": [MIN_EFFECTIVE_BLOCKS, MIN_BLOCK_COVERAGE, HEDGE_RESIDUAL_RATIO_MAX,
                       MIN_STRESS_LOSS_BPS, MIN_PASSIVE_SD_BPS_PER_DAY, MIN_NET_SD_BPS_PER_DAY,
                       MIN_HOURLY_PRICE_RETURN_SD, MAX_DAILY_PERSISTENCE, PERSISTENCE_UPPER_Z,
                       LONG_RUN_BANDWIDTH_DAYS, MIN_DAILY_POINTS_FOR_PERSISTENCE,
                       STRESS_FUNDING_PRINTS, WORST_LOSS_MULTIPLE, CAPACITY_BUDGET_DIVISOR,
                       MIN_TAKER_FEE_BPS, MAX_TAKER_FEE_BPS, MAX_STRESS_FUNDING_CAP_BPS,
                       MIN_ROWS_FOR_FROZEN_CHECK, MIN_PRINTS_FOR_DISTINCT_CHECK,
                       REPEAT_TOLERANCE_BPS],
        "implementation_version": IMPLEMENTATION_VERSION,
        "v2_constants_used": {
            "max_gap_between_observations_s": v2.PROTOCOL["max_gap_between_observations_s"],
            "min_liquidation_distance_pct": v2.PROTOCOL["min_liquidation_distance_pct"],
            "default_taker_fee_bps": v2.TAKER_FEE_BPS},
        "degenerate": [PRICE_FROZEN_SHARE, REPEAT_WINDOW_PRINTS, MIN_DISTINCT_WINDOW_SHARE,
                       MIN_DISTINCT_PRINT_VALUES],
        "v2_inference": v2.PROTOCOL["resolved_parameters"],
        "v2_entry_gate_and_exit": v2.PROTOCOL["resolved_parameters"]["entry_gate"],
    },
}


def protocol_hash() -> str:
    return hashlib.sha256(json.dumps(PROTOCOL, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


_finite = v2._finite


# -- persistence and effective sample size -----------------------------------------------------

def _lag1(vals: Sequence[float]) -> float:
    n = len(vals)
    if n < 3:
        return 0.0
    m = sum(vals) / n
    den = sum((v - m) ** 2 for v in vals)
    if den <= 0:
        return 0.0
    num = sum((vals[i] - m) * (vals[i + 1] - m) for i in range(n - 1))
    return num / den


def daily_funding_series(rows: Sequence[Dict[str, Any]]) -> List[float]:
    """Funding per UTC day (sum of the distinct settled prints that day), in order,
    complete days only (those with the usual number of prints)."""
    seen: Dict[int, float] = {}
    for r in rows:
        for bps, ms in r["obs"].get("settled_prints", []):
            if _finite(bps):
                seen.setdefault(int(ms), float(bps))
    days: Dict[int, float] = {}
    counts: Dict[int, int] = {}
    for ms, bps in seen.items():
        d = ms // 86_400_000
        days[d] = days.get(d, 0.0) + bps
        counts[d] = counts.get(d, 0) + 1
    if not counts:
        return []
    # Only complete days: a window edge holds a part day whose smaller sum is an
    # outlier that would be read as persistence or as a regime.
    modal = max(set(counts.values()), key=list(counts.values()).count)
    return [days[d] for d in sorted(days) if counts[d] == modal]


def effective_blocks(rows: Sequence[Dict[str, Any]], n_blocks: int) -> Dict[str, Any]:
    """K_eff for these rows: see `effective_blocks_from_daily`."""
    return effective_blocks_from_daily(daily_funding_series(rows), n_blocks)


def _long_run_k_eff(daily: Sequence[float], n_blocks: int) -> float:
    """K / (long-run variance of the daily series / its own variance), with a
    Bartlett window. A slow regime under large daily noise has a small lag-1
    autocorrelation but a large long-run variance, so the lag-1 estimate alone
    overstates the independent blocks (measured: a zero edge passed 20-35% of the
    time on such series; the long-run form is at or below nominal)."""
    n = len(daily)
    m = sum(daily) / n
    x = [v - m for v in daily]
    g0 = sum(v * v for v in x) / n
    if g0 <= 0:
        return float(n_blocks)
    lags = min(LONG_RUN_BANDWIDTH_DAYS, n - 2)
    s = g0
    for k in range(1, lags + 1):
        s += 2.0 * (1.0 - k / (lags + 1.0)) * sum(x[i] * x[i + k] for i in range(n - k)) / n
    return max(1.0, min(float(n_blocks), n_blocks / max(s / g0, 1.0)))


def effective_blocks_from_daily(daily: Sequence[float], n_blocks: int) -> Dict[str, Any]:
    """K_eff from the persistence of the DRIVER. phi is the bias-corrected lag-1
    autocorrelation of the daily funding series PLUS a one-sided 95% margin
    (z * (1 - phi^2) / sqrt(n)), because an estimated persistence that is too low
    overstates K_eff: measured, the unmargined estimate passed a zero edge about
    7% of the time at a true daily persistence of 0.9-0.99; with the margin it is
    at or below the nominal 5%. Capped; carried over a block it is
    rho = phi**BLOCK_DAYS; K_eff = K(1-rho)/(1+rho) (never above K). The result is
    the smaller of that and the long-run-variance form (`_long_run_k_eff`), which
    also catches a slow regime hidden under daily noise."""
    if len(daily) < MIN_DAILY_POINTS_FOR_PERSISTENCE:
        return {"status": UNAVAILABLE, "reason": "TOO_FEW_DAYS_TO_MEASURE_PERSISTENCE",
                "daily_points": len(daily), "k_eff": None}
    raw = _lag1(daily)
    n = len(daily)
    phi = max(0.0, min(MAX_DAILY_PERSISTENCE,
                       raw + (1.0 + 3.0 * raw) / n
                       + PERSISTENCE_UPPER_Z * (1.0 - max(raw, 0.0) ** 2) / math.sqrt(n)))
    rho = phi ** BLOCK_DAYS
    k_ar1 = max(1.0, min(float(n_blocks), n_blocks * (1.0 - rho) / (1.0 + rho)))
    k_lr = _long_run_k_eff(daily, n_blocks)
    k_eff = min(k_ar1, k_lr)
    half_life = (math.log(0.5) / math.log(phi)) if 0 < phi < 1 else 0.0
    return {"status": "MEASURED", "daily_points": len(daily), "phi_daily": phi,
            "rho_block": rho, "k": n_blocks, "k_eff": k_eff, "k_eff_ar1": k_ar1,
            "k_eff_long_run": k_lr, "half_life_days": half_life}


def block_bounds(values: Sequence[float], k_eff: Optional[float] = None) -> Dict[str, Any]:
    """Mean of block values with a one-sided 95% bound that uses K_eff when given:
    mean +/- t(floor(K_eff)) * sd / sqrt(K_eff). Without it, the v2 bounds."""
    vals = [float(x) for x in values if _finite(x)]
    n = len(vals)
    if n == 0:
        return {"n": 0, "mean": None, "sd": None, "lower95": None, "upper95": None}
    mean = sum(vals) / n
    if n < 2:
        return {"n": n, "mean": mean, "sd": None, "lower95": None, "upper95": None}
    sd = math.sqrt(sum((v - mean) ** 2 for v in vals) / (n - 1))
    if k_eff is None:
        return dict(v2.mean_bounds(vals), k_eff=None)
    ke = max(1.0, min(float(n), float(k_eff)))
    if ke < 2.0:
        return {"n": n, "mean": mean, "sd": sd, "lower95": None, "upper95": None, "k_eff": ke}
    half = v2.t_crit(max(2, int(math.floor(ke)))) * sd / math.sqrt(ke)
    return {"n": n, "mean": mean, "sd": sd, "lower95": mean - half, "upper95": mean + half,
            "k_eff": ke, "method": "t_with_effective_blocks"}


def poisson_upper95(k: int) -> float:
    """Exact one-sided 95% upper bound on a Poisson mean given k events seen
    (about 3.0 for none: the rule of three)."""
    table = {0: 2.996, 1: 4.744, 2: 6.296, 3: 7.754, 4: 9.154, 5: 10.513,
             6: 11.842, 7: 13.148, 8: 14.435, 9: 15.705, 10: 16.962}
    if k in table:
        return table[k]
    return k + 1.645 * math.sqrt(k) + 1.0


def unseen_tail_charge_bps_per_episode(net_bps: Sequence[float], stress_loss_bps: float
                                       ) -> Dict[str, Any]:
    """What an episode must pay for tail losses the sample did not show. The
    stress loss used is at least twice the worst loss actually observed, so a
    loss bigger than the declared stress is not charged at the stress value."""
    n = len(net_bps)
    if n == 0 or not _finite(stress_loss_bps) or stress_loss_bps <= 0:
        return {"status": UNAVAILABLE, "charge_bps_per_episode": None}
    worst = min(net_bps)
    stress = max(float(stress_loss_bps), WORST_LOSS_MULTIPLE * max(0.0, -worst))
    k = sum(1 for x in net_bps if x <= -stress)
    ub = poisson_upper95(k) / n
    extra = max(0.0, ub - k / n)
    return {"status": "MEASURED", "observed_tail_episodes": k, "episodes": n,
            "upper95_frequency": ub, "stress_loss_bps": stress,
            "declared_stress_loss_bps": float(stress_loss_bps),
            "charge_bps_per_episode": extra * stress}


# -- degenerate samples ---------------------------------------------------------------------

def _distinct_window_share(windows: Sequence[Sequence[float]]) -> float:
    """Share of windows that are not a near-copy (every print within
    REPEAT_TOLERANCE_BPS) of an earlier one. Tolerance, not rounding: a replay
    with a little jitter must not look like new data."""
    reps: Dict[int, List[Sequence[float]]] = {}
    distinct = 0
    step = 4.0 * REPEAT_TOLERANCE_BPS
    for wd in windows:
        key = int(math.floor(wd[0] / step))
        copy = any(max(abs(a - b) for a, b in zip(wd, r)) <= REPEAT_TOLERANCE_BPS
                   for k in (key - 1, key, key + 1) for r in reps.get(k, ()))
        if not copy:
            distinct += 1
            reps.setdefault(key, []).append(wd)
    return distinct / len(windows) if windows else 1.0


def degenerate_reasons(rows: Sequence[Dict[str, Any]]) -> List[str]:
    """Named reasons the observations cannot carry an economic conclusion."""
    out: List[str] = []
    if not rows:
        return out
    spots = [float(r["obs"]["spot_mark"]) for r in rows]
    rounded = [round(s, 6) for s in spots]
    top = max((rounded.count(s) for s in set(rounded)), default=0)
    rets = [math.log(b / a) for a, b in zip(spots, spots[1:]) if a > 0 and b > 0]
    sd = (math.sqrt(sum((x - sum(rets) / len(rets)) ** 2 for x in rets) / (len(rets) - 1))
          if len(rets) > 2 else 0.0)
    if len(spots) > MIN_ROWS_FOR_FROZEN_CHECK and (top / len(spots) >= PRICE_FROZEN_SHARE or sd < MIN_HOURLY_PRICE_RETURN_SD):
        out.append("PRICE_FEED_FROZEN")
    basis = [round(float(r["obs"]["perp_mark"]) - float(r["obs"]["spot_mark"]), 6) for r in rows]
    if len(basis) > MIN_ROWS_FOR_FROZEN_CHECK and len(set(basis)) <= 2:
        out.append("BASIS_CONSTANT")
    seq: Dict[int, float] = {}
    for r in rows:
        for bps, ms in r["obs"].get("settled_prints", []):
            seq.setdefault(int(ms), round(float(bps), 9))
    vals = [seq[k] for k in sorted(seq)]
    if len(set(vals)) < MIN_DISTINCT_PRINT_VALUES and len(vals) >= MIN_PRINTS_FOR_DISTINCT_CHECK:
        out.append("TOO_FEW_DISTINCT_FUNDING_VALUES")
    w = REPEAT_WINDOW_PRINTS
    if len(vals) >= 2 * w:
        windows = [vals[i:i + w] for i in range(0, len(vals) - w + 1, w // 2)]
        if _distinct_window_share(windows) < MIN_DISTINCT_WINDOW_SHARE:
            out.append("FUNDING_SEQUENCE_REPEATED")
    return out


# -- coverage ---------------------------------------------------------------------------------

def coverage_problems(rows: Sequence[Dict[str, Any]]) -> List[str]:
    """Collection that began late, or blocks the observations do not cover. A
    block is covered when its rows span it without a gap beyond the v2 limit and
    its rows are at least MIN_BLOCK_COVERAGE of what that spacing would give."""
    probs: List[str] = []
    if not rows:
        return ["NO_OBSERVATIONS"]
    first = rows[0]["obs"]["observed_at_s"]
    if first > EVALUATION_START_S + MAX_START_DELAY_S:
        probs.append("COLLECTION_STARTED_LATE")
    max_gap = float(v2.PROTOCOL["max_gap_between_observations_s"])
    for b in range(N_BLOCKS):
        b0 = EVALUATION_START_S + b * BLOCK_DAYS * 86400
        b1 = b0 + BLOCK_DAYS * 86400
        ts = [r["obs"]["observed_at_s"] for r in rows if b0 <= r["obs"]["observed_at_s"] < b1]
        if not ts:
            probs.append(f"BLOCK_{b}_NOT_COVERED")
            continue
        edges = [b0] + ts + [b1]
        worst = max(y - x for x, y in zip(edges, edges[1:]))
        covered = sum(min(y - x, max_gap) for x, y in zip(edges, edges[1:])) / (b1 - b0)
        if worst > max_gap * 4 or covered < MIN_BLOCK_COVERAGE:
            probs.append(f"BLOCK_{b}_NOT_COVERED")
    return probs


# -- episodes -> block values -----------------------------------------------------------------

def _growth(rows: Sequence[Dict[str, Any]], e: Dict[str, Any]) -> float:
    """Mean spot over the episode relative to entry: margin on a fixed BTC
    quantity grows with price."""
    inside = [float(r["obs"]["spot_mark"]) for r in rows
              if e["entry_s"] <= r["obs"]["observed_at_s"] <= e["exit_s"]]
    if not inside:
        return 1.0
    return max(1.0, (sum(inside) / len(inside)) / inside[0])


def _overlap_days(a0: float, a1: float, b0: float, b1: float) -> float:
    return max(0.0, min(a1, b1) - max(a0, b0)) / 86400.0


def allocate_to_blocks(episodes: Sequence[Dict[str, Any]], start_s: float, n_blocks: int,
                       key: str) -> List[float]:
    """Each episode's `key` (bps, total over its hold) split across the blocks it
    spans in proportion to the days in each; returns bps per CALENDAR DAY per block."""
    out = [0.0] * n_blocks
    for e in episodes:
        v = e.get(key)
        if not _finite(v) or e["exit_s"] <= e["entry_s"]:
            continue
        span = (e["exit_s"] - e["entry_s"]) / 86400.0
        for b in range(n_blocks):
            b0 = start_s + b * BLOCK_DAYS * 86400.0
            b1 = b0 + BLOCK_DAYS * 86400.0
            d = _overlap_days(e["entry_s"], e["exit_s"], b0, b1)
            if d > 0:
                out[b] += v * (d / span)
    return [x / BLOCK_DAYS for x in out]


def _passive_blocks(rows: Sequence[Dict[str, Any]], start_s: float, n_blocks: int) -> List[float]:
    out = []
    for b in range(n_blocks):
        b0 = start_s + b * BLOCK_DAYS * 86400.0
        b1 = b0 + BLOCK_DAYS * 86400.0
        s0 = next((float(r["obs"]["spot_mark"]) for r in rows if r["obs"]["observed_at_s"] >= b0), None)
        s1 = next((float(r["obs"]["spot_mark"]) for r in reversed(rows)
                   if r["obs"]["observed_at_s"] < b1), None)
        out.append(((s1 / s0 - 1.0) * 1e4 / BLOCK_DAYS) if s0 and s1 else float("nan"))
    return out


def _sd(xs: Sequence[float]) -> float:
    n = len(xs)
    if n < 2:
        return 0.0
    m = sum(xs) / n
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (n - 1))


def paired_ratio_bounds(num: Sequence[float], den: Sequence[float]) -> Dict[str, Any]:
    """sd(num)/sd(den) with a paired bootstrap. A zero denominator, or a numerator
    that is identically zero, is reported as not estimable (a perfect hedge is
    never evidence, it is a copied feed)."""
    k = len(num)
    if k < 3 or _sd(den) <= 0 or _sd(num) <= 0:
        return {"ratio": None, "upper95": None}
    rng = random.Random(BOOT_SEED + 1)
    rs = []
    for _ in range(BOOT_N):
        idx = [rng.randrange(k) for _ in range(k)]
        d = _sd([den[i] for i in idx])
        if d > 0:
            rs.append(_sd([num[i] for i in idx]) / d)
    rs.sort()
    return {"ratio": _sd(num) / _sd(den),
            "upper95": rs[int(0.95 * len(rs)) - 1] if rs else None}


def sharpe_diff_bounds(a: Sequence[float], b: Sequence[float]) -> Dict[str, Any]:
    """Block-level mean/sd of `a` minus that of `b`, paired bootstrap (reported,
    not gating)."""
    k = len(a)

    def sr(xs):
        s = _sd(xs)
        return (sum(xs) / len(xs)) / s if s > 0 else float("nan")
    if k < 3:
        return {"difference": None, "lower95": None}
    rng = random.Random(BOOT_SEED + 2)
    ds = []
    for _ in range(BOOT_N):
        idx = [rng.randrange(k) for _ in range(k)]
        d = sr([a[i] for i in idx]) - sr([b[i] for i in idx])
        if math.isfinite(d):
            ds.append(d)
    ds.sort()
    pt = sr(a) - sr(b)
    return {"difference": pt if math.isfinite(pt) else None,
            "lower95": ds[int(0.05 * len(ds))] if ds else None}


# -- provenance ---------------------------------------------------------------------------------

def _canon(x: Any) -> str:
    return json.dumps(x, sort_keys=True, default=str, separators=(",", ":"))


def provenance(fwd: Sequence[Dict[str, Any]], inputs: Dict[str, Any],
               chain_issues: Sequence[str]) -> Dict[str, Any]:
    """What THIS answer was computed from (v3's own protocol, window and rows),
    so it can be reproduced from immutable inputs and nothing else."""
    chain = [r.get("hash") for r in fwd]
    content = [hashlib.sha256(_canon(r.get("obs")).encode()).hexdigest() for r in fwd]
    digest = hashlib.sha256(_canon({
        "schema": SCHEMA, "protocol_hash": protocol_hash(), "chain_hashes": chain,
        "content_hashes": content, "inputs": inputs,
        "chain_issues": list(chain_issues)}).encode()).hexdigest()
    return {"schema": SCHEMA, "protocol_hash": protocol_hash(),
            "evaluation_start_s": EVALUATION_START_S,
            "analysis_horizon_days": ANALYSIS_HORIZON_DAYS, "forward_rows": len(fwd),
            "first_row_hash": chain[0] if chain else None,
            "last_row_hash": chain[-1] if chain else None,
            "first_observed_at_s": fwd[0]["obs"]["observed_at_s"] if fwd else None,
            "last_observed_at_s": fwd[-1]["obs"]["observed_at_s"] if fwd else None,
            "inputs_digest": digest,
            "rows_missing_a_chain_hash": sum(1 for h in chain if not h)}


# -- the evaluation -------------------------------------------------------------------------

def evaluate(rows: Sequence[Dict[str, Any]], *, planned_notional_usd: float,
             owner_capital_apr: Optional[float] = None,
             committed_capital_fraction: Optional[float] = None,
             capital_cost_basis: Optional[str] = None,
             min_annual_net_usd: Optional[float] = None,
             owner_drawdown_limit_usd: Optional[float] = None,
             maintenance_margin_rate: Optional[float] = None,
             collateral_ratio: Optional[float] = None,
             funding_cap_bps: Optional[float] = None,
             price_limit_ratio: Optional[float] = None,
             chain_issues: Sequence[str] = (),
             taker_fee_bps: float = v2.TAKER_FEE_BPS) -> Dict[str, Any]:
    chain_issues = list(chain_issues)
    total = len(rows)
    ts = [r["obs"].get("observed_at_s") for r in rows]
    bad_i = next((i for i, t in enumerate(ts) if not _finite(t)), None)
    if bad_i is None:
        bad_i = next((i + 1 for i in range(len(ts) - 1) if ts[i + 1] <= ts[i]), None)
    if bad_i is not None:
        chain_issues.append(f"OBSERVATIONS_NOT_STRICTLY_INCREASING_IN_TIME (row {bad_i})")
        rows = []
    horizon_end = EVALUATION_START_S + ANALYSIS_HORIZON_DAYS * 86400
    fwd = [r for r in rows if EVALUATION_START_S <= r["obs"].get("observed_at_s", 0) <= horizon_end]
    before = sum(1 for r in rows if r["obs"].get("observed_at_s", 0) < EVALUATION_START_S)
    after = sum(1 for r in rows if r["obs"].get("observed_at_s", 0) > horizon_end)
    if fwd:
        chain_issues += ["ROWS: " + m for m in v2.validate_rows(fwd)]
        for i, r in enumerate(fwd):
            for k in ("spot_mark", "perp_mark"):
                v = r["obs"].get(k)
                if isinstance(v, bool) or not isinstance(v, (int, float)) or not (_finite(v) and v > 0):
                    chain_issues.append(f"ROWS: {k.upper()}_NOT_A_POSITIVE_NUMBER (row {i})")
                    break
    inputs = {"planned_notional_usd": planned_notional_usd, "owner_capital_apr": owner_capital_apr,
              "committed_capital_fraction": committed_capital_fraction,
              "capital_cost_basis": capital_cost_basis, "min_annual_net_usd": min_annual_net_usd,
              "owner_drawdown_limit_usd": owner_drawdown_limit_usd,
              "maintenance_margin_rate": maintenance_margin_rate,
              "collateral_ratio": collateral_ratio, "funding_cap_bps": funding_cap_bps,
              "price_limit_ratio": price_limit_ratio, "taker_fee_bps": taker_fee_bps}
    out: Dict[str, Any] = {
        "schema": SCHEMA, "protocol_hash": protocol_hash(), "class": SIMULATED, "cash": False,
        "evidence": {"realised_cash_usd": None,
                     "what_is_simulated": ("every figure: book-walk execution cost upper "
                                           "bounds, hourly marks, no latency, no fills"),
                     "execution_cost_basis": "BOOK_WALK_UPPER_BOUND_NO_LATENCY"},
        "observations_total": total, "observations_forward": len(fwd),
        "observations_before_declaration_excluded": before,
        "observations_after_horizon_excluded": after,
        "chain_issues": chain_issues, "run_inputs": inputs,
        "provenance": provenance(fwd, inputs, chain_issues)}
    # owner inputs are validated before they can touch the arithmetic
    bad_inputs = []
    if not (_finite(planned_notional_usd) and planned_notional_usd > 0):
        bad_inputs.append("planned_notional_usd")
    if not (_finite(taker_fee_bps) and MIN_TAKER_FEE_BPS <= taker_fee_bps <= MAX_TAKER_FEE_BPS):
        bad_inputs.append("taker_fee_bps")
    stress = None
    if funding_cap_bps is not None or price_limit_ratio is not None:
        if (_finite(funding_cap_bps) and _finite(price_limit_ratio)
                and 0 < funding_cap_bps <= MAX_STRESS_FUNDING_CAP_BPS
                and 0 < price_limit_ratio <= 1.0):
            stress = STRESS_FUNDING_PRINTS * float(funding_cap_bps) + float(price_limit_ratio) * 1e4
            if stress < MIN_STRESS_LOSS_BPS:
                bad_inputs.append("stress_loss_below_floor")
                stress = None
        else:
            bad_inputs.append("funding_cap_bps/price_limit_ratio")
    out["invalid_inputs"] = bad_inputs
    span_days = ((fwd[-1]["obs"]["observed_at_s"] - fwd[0]["obs"]["observed_at_s"]) / 86400.0
                 if len(fwd) > 1 else 0.0)
    out["span_days"] = span_days
    out["distinct_settled_prints"] = len({int(p[1]) for r in fwd for p in r["obs"].get("settled_prints", [])})
    out["analysis_due"] = bool(fwd) and any(
        t >= horizon_end for t in (r["obs"].get("observed_at_s", 0) for r in rows))
    out["coverage"] = coverage_problems(fwd) if fwd and not chain_issues else []
    out["degenerate"] = degenerate_reasons(fwd) if fwd and not chain_issues else []
    sim = (v2.simulate_episodes(fwd, planned_notional_usd, taker_fee_bps)
           if fwd and not chain_issues and not bad_inputs else
           {"closed": [], "open_at_end": False, "open_marked": None})
    closed = sim["closed"]
    priced = [e for e in closed if _finite(e.get("carry_bps"))
              and _finite(e.get("carry_bps_most_favourable")) and not e.get("gap_spanning")]
    unpriced = [e for e in closed if e not in priced]
    out["episodes"] = {"closed": len(closed), "priced": len(priced), "unpriced": len(unpriced),
                       "unpriced_fraction": len(unpriced) / len(closed) if closed else 0.0,
                       "open_at_end": sim["open_at_end"]}

    # capital: a basis is always stated; fractions validated; invalid input never used
    cap_status = "OWNER_SUPPLIED"
    if owner_capital_apr is None or committed_capital_fraction is None:
        cap_status = INSUFFICIENT_OWNER_INPUT
    elif not (_finite(owner_capital_apr) and 0 <= owner_capital_apr <= 10
              and _finite(committed_capital_fraction) and 0 < committed_capital_fraction <= 1):
        cap_status = "INVALID_OWNER_INPUT"
    elif not (isinstance(capital_cost_basis, str) and capital_cost_basis.strip()):
        cap_status = "CAPITAL_COST_NEEDS_A_STATED_BASIS"
    out["capital"] = {"status": cap_status, "applied_apr": owner_capital_apr,
                      "committed_fraction": committed_capital_fraction, "basis": capital_cost_basis}

    n_blocks = N_BLOCKS if (fwd and not out["coverage"] and not chain_issues) else 0
    out["blocks"] = {"complete": n_blocks, "block_days": BLOCK_DAYS, "anchored_to": EVALUATION_START_S}
    out["persistence"] = (effective_blocks(fwd, n_blocks) if n_blocks else
                          {"status": UNAVAILABLE, "k_eff": None})
    if not (priced and n_blocks and out["persistence"].get("k_eff") and not bad_inputs
            and not out["degenerate"]):
        out["verdict"], out["verdict_reasons"] = _verdict(out, stress, owner_drawdown_limit_usd,
                                                          min_annual_net_usd)
        return out
    k_eff = out["persistence"]["k_eff"]

    supplied = cap_status == "OWNER_SUPPLIED"
    apr = float(owner_capital_apr) if supplied else 0.0
    frac = float(committed_capital_fraction) if supplied else 0.0
    scale = planned_notional_usd / 1e4
    unpriced_adverse = -(stress if stress else MIN_STRESS_LOSS_BPS)
    best_rate = max(e["carry_bps_most_favourable"] / max(e["hold_days"], 1e-9) for e in priced)
    enriched = []
    for e in closed:
        g = _growth(fwd, e)
        cbps = apr * frac * g * e["hold_days"] / 365.0 * 1e4
        adv = e["carry_bps"] if e in priced else unpriced_adverse
        fav = e["carry_bps_most_favourable"] if e in priced else best_rate * e["hold_days"]
        passive = e.get("passive_return_bps", 0.0)
        enriched.append(dict(e, net_adv=adv - cbps, net_fav=fav, carry_adv=adv,
                             incremental_adv=(adv - cbps) - passive))
    om = sim.get("open_marked")
    if om is not None and not _finite(om.get("carry_bps")):
        out["episodes"]["open_unmarkable"] = True
    if om is not None and _finite(om.get("carry_bps")):
        g = _growth(fwd, om)
        cbps = apr * frac * g * om["hold_days"] / 365.0 * 1e4
        fav_o = om.get("carry_bps_most_favourable")
        enriched.append(dict(om, net_adv=om["carry_bps"] - cbps,
                             net_fav=fav_o if _finite(fav_o) else om["carry_bps"],
                             carry_adv=om["carry_bps"],
                             incremental_adv=(om["carry_bps"] - cbps) - om.get("passive_return_bps", 0.0),
                             open=True))
    start_s = float(EVALUATION_START_S)
    carry_pd = allocate_to_blocks(enriched, start_s, n_blocks, "carry_adv")
    net_pd = allocate_to_blocks(enriched, start_s, n_blocks, "net_adv")
    fav_pd = allocate_to_blocks(enriched, start_s, n_blocks, "net_fav")
    incr_pd = allocate_to_blocks(enriched, start_s, n_blocks, "incremental_adv")
    passive_pd = _passive_blocks(fwd, start_s, n_blocks)
    combined_pd = [p + i for p, i in zip(passive_pd, incr_pd)]

    closed_net = [e["net_adv"] for e in enriched if not e.get("open")]
    tail = unseen_tail_charge_bps_per_episode(closed_net, stress or 0.0)
    eps_per_day = len(closed) / ANALYSIS_HORIZON_DAYS
    tail_pd = ((tail["charge_bps_per_episode"] or 0.0) * eps_per_day
               if tail["status"] == "MEASURED" else None)
    out["tail"] = dict(tail, charge_bps_per_day=tail_pd, episodes_per_day=eps_per_day)

    net_after_tail = [x - (tail_pd or 0.0) for x in net_pd]
    out["positive_carry_after_costs_bps_per_day"] = dict(block_bounds(carry_pd, k_eff),
        note="after execution costs, before capital cost and before the unseen-tail charge")
    out["net_of_capital_bps_per_day"] = block_bounds(net_pd, k_eff)
    out["net_after_tail_charge_bps_per_day"] = block_bounds(net_after_tail, k_eff)
    out["most_favourable_bps_per_day"] = block_bounds(fav_pd, k_eff)
    out["absolute_overlay_portfolio_return_bps_per_day"] = dict(block_bounds(combined_pd, k_eff),
        note="the owner's BTC with the overlay on only while the gate was open (idle time unhedged)")
    out["passive_btc_return_bps_per_day"] = block_bounds(passive_pd, k_eff)
    out["incremental_vs_passive_bps_per_day"] = dict(block_bounds(incr_pd, k_eff),
        note="includes the BTC drift forgone while hedged; not an edge, a cost of hedging")
    out["hedge_risk_ratio"] = dict(paired_ratio_bounds(combined_pd, passive_pd),
        note="sd of the combined position's block returns / sd of passive BTC's (reported, not gating)")
    out["risk_adjusted_vs_passive"] = dict(sharpe_diff_bounds(combined_pd, passive_pd),
        note="difference in block mean/sd; reported, with its uncertainty, not a gate on its own")

    # unseen-upside credit before any NO_GO (the mirror of the tail charge)
    best_obs = max((e["net_fav"] for e in enriched if not e.get("open")), default=0.0)
    ref_up = max(best_obs, stress or 0.0, 0.0)
    out["unseen_upside_credit_bps_per_day"] = (
        (poisson_upper95(0) / max(1, len(closed))) * ref_up * eps_per_day)

    nb = block_bounds(net_after_tail, k_eff)
    out["annualised"] = {
        "net_bps_of_notional_lower95": (nb["lower95"] * 365.0 if nb["lower95"] is not None else None),
        "net_usd_lower95": (nb["lower95"] * 365.0 * scale if nb["lower95"] is not None else None),
        "capital_utilisation": sum(e["hold_days"] for e in closed) / ANALYSIS_HORIZON_DAYS,
        "note": "idle time is in the denominator; not a forecast"}

    by_t = {r["obs"]["observed_at_s"]: r["obs"] for r in fwd}
    resid, moved = [], []
    for e in enriched:
        if e.get("open"):
            continue
        a, b = by_t.get(e["entry_s"]), by_t.get(e["exit_s"])
        if not a or not b:
            continue
        basis_in = float(a["perp_mark"]) - float(a["spot_mark"])
        basis_out = float(b["perp_mark"]) - float(b["spot_mark"])
        resid.append(-(basis_out - basis_in) / float(a["spot_mark"]) * 1e4)
        moved.append(float(e["passive_return_bps"]))
    out["hedge_effectiveness"] = dict(
        paired_ratio_bounds(resid, moved), episodes=len(resid),
        note="per closed episode: SD of the basis residual / SD of the price move hedged")

    out["worst_episode_usd"] = min((e["worst_path_usd"] for e in priced if _finite(e.get("worst_path_usd"))),
                                   default=None)
    nclosed = max(1, len([e for e in enriched if not e.get("open")]))
    budget = (sum(e["carry_adv"] + (e.get("impact_upper_bps") or 0.0)
                  for e in enriched if not e.get("open")) / nclosed) / CAPACITY_BUDGET_DIVISOR
    out["capacity"] = (v2.capacity_bounds(fwd, budget, planned_notional_usd) if budget > 0
                       else {"status": UNAVAILABLE, "reason": "NO_POSITIVE_EDGE_BEFORE_IMPACT"})
    out["stress"] = v2.stress_table(planned_notional_usd, leverage=1.0,
                                    maintenance_margin_rate=maintenance_margin_rate,
                                    collateral_ratio=collateral_ratio)
    out["verdict"], out["verdict_reasons"] = _verdict(out, stress, owner_drawdown_limit_usd,
                                                      min_annual_net_usd)
    out["protocol"] = {"meaning": PROTOCOL["meaning"][out["verdict"]]}
    return out


def _verdict(rep: Dict[str, Any], stress: Optional[float], drawdown: Optional[float],
             min_annual: Optional[float]) -> Tuple[str, List[str]]:
    why: List[str] = []
    if rep.get("chain_issues"):
        return "INSUFFICIENT", ["EVIDENCE_UNREADABLE"]
    if rep.get("invalid_inputs"):
        return "INSUFFICIENT", ["INVALID_OWNER_INPUT:" + ",".join(rep["invalid_inputs"])]
    if not rep.get("analysis_due"):
        return "INSUFFICIENT", ["ANALYSIS_NOT_YET_DUE"]
    if rep.get("coverage"):
        return "INSUFFICIENT", list(rep["coverage"])
    if rep.get("degenerate"):
        return "INSUFFICIENT", list(rep["degenerate"])
    per = rep.get("persistence") or {}
    if not per.get("k_eff"):
        return "INSUFFICIENT", ["PERSISTENCE_NOT_MEASURABLE:" + str(per.get("reason", ""))]
    if per["k_eff"] < MIN_EFFECTIVE_BLOCKS:
        return "INSUFFICIENT", ["SAMPLE_TOO_PERSISTENT_FOR_ITS_LENGTH"]
    ep = rep["episodes"]
    if ep["priced"] < MIN_CLOSED_EPISODES:
        why.append(f"FEWER_THAN_{MIN_CLOSED_EPISODES}_PRICED_CLOSED_EPISODES")
    if ep["unpriced_fraction"] > MAX_UNPRICED_FRACTION:
        why.append("TOO_MANY_EPISODES_UNPRICEABLE")
    if ep.get("open_unmarkable"):
        why.append("OPEN_EPISODE_CANNOT_BE_MARKED")
    if why:
        return "INSUFFICIENT", why
    if "net_of_capital_bps_per_day" not in rep:
        return "INSUFFICIENT", ["NO_BLOCK_STATISTICS"]
    sd_p = rep["passive_btc_return_bps_per_day"]["sd"]
    if sd_p is None or sd_p < MIN_PASSIVE_SD_BPS_PER_DAY:
        return "INSUFFICIENT", ["PASSIVE_RETURN_CONSTANT"]
    sd_net = rep["net_after_tail_charge_bps_per_day"]["sd"]
    if sd_net is None or sd_net < MIN_NET_SD_BPS_PER_DAY:
        return "INSUFFICIENT", ["BLOCK_VALUES_CONSTANT"]
    if stress is None:
        return "INSUFFICIENT", [INSUFFICIENT_OWNER_INPUT + ":STRESS_LOSS(funding_cap_bps,price_limit_ratio)"]
    fav = rep["most_favourable_bps_per_day"]
    if (fav["upper95"] is not None
            and fav["upper95"] + rep.get("unseen_upside_credit_bps_per_day", 0.0) <= 0):
        return "NO_GO", ["UPPER_95_BOUND_NOT_ABOVE_ZERO_EVEN_AT_BEST_CASE_WITH_UPSIDE_CREDIT"]
    cap = rep["capital"]
    if cap["status"] != "OWNER_SUPPLIED":
        return "INSUFFICIENT", [cap["status"]]
    if stress is None:
        return "INSUFFICIENT", [INSUFFICIENT_OWNER_INPUT + ":STRESS_LOSS(funding_cap_bps,price_limit_ratio)"]
    nat = rep["net_after_tail_charge_bps_per_day"]
    if nat["lower95"] is None or nat["lower95"] <= 0:
        return "INSUFFICIENT", ["LOWER_95_BOUND_NOT_ABOVE_ZERO_AFTER_COSTS_TAIL_AND_CAPITAL"]
    if not _finite(min_annual) or min_annual <= 0:
        return "INSUFFICIENT", [INSUFFICIENT_OWNER_INPUT + ":MIN_ANNUAL_NET_USD"]
    if rep["annualised"]["net_usd_lower95"] is None or rep["annualised"]["net_usd_lower95"] < min_annual:
        return "INSUFFICIENT", ["BELOW_THE_OWNERS_MATERIALITY_FLOOR"]
    he = rep["hedge_effectiveness"]
    if he["upper95"] is None:
        return "INSUFFICIENT", ["HEDGE_EFFECTIVENESS_NOT_ESTIMABLE"]
    if he["upper95"] >= HEDGE_RESIDUAL_RATIO_MAX:
        return "INSUFFICIENT", ["HEDGE_NOT_SHOWN_TO_REMOVE_MOST_PRICE_RISK"]
    inc = rep["incremental_vs_passive_bps_per_day"]
    if inc["upper95"] is not None and inc["upper95"] < 0:
        return "INSUFFICIENT", ["SIGNIFICANTLY_WORSE_THAN_HOLDING_BTC"]
    if rep["capacity"].get("planned_inside_measured_capacity") is not True:
        return "INSUFFICIENT", ["PLANNED_SIZE_NOT_INSIDE_MEASURED_CAPACITY"]
    if not _finite(drawdown) or drawdown <= 0:
        return "INSUFFICIENT", [INSUFFICIENT_OWNER_INPUT + ":DRAWDOWN_LIMIT"]
    liq = (rep.get("stress") or {}).get("liquidation_rally_pct_btc_collateral") or {}
    if liq.get("status") != "ILLUSTRATIVE" or not _finite(liq.get("pct")):
        return "INSUFFICIENT", [INSUFFICIENT_OWNER_INPUT + ":MARGIN_FACTS"]
    if liq["pct"] < v2.PROTOCOL["min_liquidation_distance_pct"]:
        return "INSUFFICIENT", ["LIQUIDATION_DISTANCE_BELOW_ENGINE_FLOOR"]
    w = rep.get("worst_episode_usd")
    if w is None:
        return "INSUFFICIENT", ["NO_OBSERVED_EPISODE_PATH"]
    if w < -float(drawdown):
        return "INSUFFICIENT", ["WORST_EPISODE_EXCEEDS_DRAWDOWN_LIMIT"]
    return "GO", ["LOWER_95_NET_CARRY_POSITIVE_AFTER_COSTS_TAIL_AND_CAPITAL_AND_MATERIAL"]


def report(db: str, **kw) -> Dict[str, Any]:
    rows, issues = mp.read(db)
    return evaluate(rows, chain_issues=issues, **kw)


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("report")
    p.add_argument("--db", required=True)
    p.add_argument("--notional", type=float, required=True)
    for f in ("capital-apr", "committed-capital-fraction", "min-annual-net-usd",
              "drawdown-limit-usd", "maintenance-margin-rate", "collateral-ratio",
              "funding-cap-bps", "price-limit-ratio"):
        p.add_argument("--" + f, type=float, default=None)
    p.add_argument("--capital-cost-basis", default=None)
    a = ap.parse_args(argv)
    print(json.dumps(report(
        a.db, planned_notional_usd=a.notional, owner_capital_apr=a.capital_apr,
        committed_capital_fraction=a.committed_capital_fraction,
        capital_cost_basis=a.capital_cost_basis, min_annual_net_usd=a.min_annual_net_usd,
        owner_drawdown_limit_usd=a.drawdown_limit_usd,
        maintenance_margin_rate=a.maintenance_margin_rate, collateral_ratio=a.collateral_ratio,
        funding_cap_bps=a.funding_cap_bps, price_limit_ratio=a.price_limit_ratio),
        indent=2, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
