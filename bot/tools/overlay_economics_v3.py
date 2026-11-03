#!/usr/bin/env python3
"""Overlay economics, prospective protocol v3 (schema `overlay_economics/v3`).

WHY A NEW PROTOCOL. `overlay_economics` (protocol `overlay_economics/1`, "v2" in
the project's own numbering) is merged, pinned and frozen; its historical
meaning does not change and this module does not edit it. A red-team of that
rule (seeded Monte Carlo on the repo's own helpers) showed the decision rule
itself can be driven to an unjustified GO or NO_GO:

  * episode counts overstate information: persistent funding regimes gave a
    22% false-GO rate on a ZERO-edge process (nominal 5%), and the lag-1
    correction barely engaged (measured rho 0.14-0.20)
  * weekly looks (optional stopping) gave 29% any-look vs 21% final-look
  * an unseen tail is invisible to a bootstrap: a rare large loss sized for a
    zero mean gave 55% false GO at n=30
  * the risk-adjusted benchmark compared two POINT estimates, wrong in both
    directions (23% pass when the overlay truly trails; 45% block when equal)
  * a zero-variance sample, or a time-shifted replay of the best funding block,
    produced GO
  * mean-of-episodes ignores duration; carry and "absolute overlay portfolio
    return" were the same object under two names

THE RULE HERE (declared 2026-11-03, before any observation it judges):
  1. independence is CALENDAR TIME: fixed non-overlapping BLOCKS of 14 days, each
     episode allocated to blocks pro rata by the days it spans; a block value is
     net bps per calendar day on the notional, idle days included (so a
     duration is never an unweighted mean)
  2. a SINGLE analysis, due when the declared horizon is reached; earlier looks
     answer INSUFFICIENT (analysis not due), later rows are excluded
  3. an unseen tail is charged: the stress loss (3 negative prints at the venue
     cap plus a gap exit at the price band) times the exact upper bound on how
     often such a loss could occur given none (rule of three) or k were seen
  4. the comparison with passive BTC is by UNCERTAINTY, not point estimates, and
     its five facets are separate keys: positive carry after costs; the absolute
     portfolio return of the combined position (idle time unhedged); the
     incremental return versus passive; risk-adjusted versus passive; and what
     is cash (nothing) versus a simulated mark
  5. degenerate samples are refused by name (zero variance, frozen price, a
     repeated funding sequence, too few distinct values)
  6. an explicit zero capital cost needs a stated basis; capital is charged on the
     committed fraction scaled by how far price has grown; a materiality floor in
     USD per year is an owner input
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

#: Declared 2026-11-04 00:00 UTC (a day after v2's, so no row v2 judged is judged here).
EVALUATION_START_S = 1_793_750_400
BLOCK_DAYS = 14
#: The single analysis is due at this horizon; rows after it are excluded.
ANALYSIS_HORIZON_DAYS = 126                      # nine complete blocks
MIN_COMPLETE_BLOCKS = 6
MIN_CLOSED_EPISODES = 20
MAX_UNPRICED_FRACTION = 0.10
#: Degenerate-sample thresholds.
MIN_BLOCK_SD_BPS_PER_DAY = 1e-6
PRICE_FROZEN_SHARE = 0.95
REPEAT_WINDOW_PRINTS = 24
MIN_DISTINCT_WINDOW_SHARE = 0.90
MIN_DISTINCT_PRINT_VALUES = 8
#: Bootstrap of block-level statistics: fixed seed and size.
BOOT_SEED = 20261104
BOOT_N = 4000

PROTOCOL = {
    "schema": SCHEMA,
    "declared": "2026-11-03, before any observation it judges was read",
    "supersedes": "nothing: overlay_economics/1 is preserved and keeps its meaning",
    "mode": "overlay: two perp legs only; the owner's BTC is never traded",
    "question": ("Does the frozen entry gate and exit rule earn a positive NET carry "
                 "per calendar day, after measured costs, an unseen-tail charge and "
                 "the owner's cost of capital, without the hedge adding risk to the "
                 "BTC the owner already holds, and is it material?"),
    "unit_of_independence": f"a non-overlapping {BLOCK_DAYS}-day calendar block",
    "evaluation_start_s": EVALUATION_START_S,
    "analysis": ("ONE analysis, due at analysis_horizon_days; earlier looks are "
                 "INSUFFICIENT (ANALYSIS_NOT_YET_DUE); later rows are excluded"),
    "analysis_horizon_days": ANALYSIS_HORIZON_DAYS,
    "block_days": BLOCK_DAYS,
    "adequacy": {"min_complete_blocks": MIN_COMPLETE_BLOCKS,
                 "min_closed_episodes": MIN_CLOSED_EPISODES},
    "max_unpriced_fraction": MAX_UNPRICED_FRACTION,
    "inference": ("one-sided 95% bound on the mean block value: the wider of the "
                  "dependence-inflated Student-t interval and a fixed-seed "
                  "whole-block bootstrap"),
    "tail": ("an unseen-tail charge = stress_loss_bps x max(0, upper95 frequency of "
             "episodes at least that bad - observed frequency), the upper bound "
             "being the exact Poisson 95% bound (rule of three when none was seen), "
             "expressed per calendar day; stress_loss_bps = 3 x funding cap + price "
             "band, from owner/venue inputs; missing = INSUFFICIENT_OWNER_INPUT"),
    "benchmark": ("GO needs: lower95 of the net carry per day > 0 after the tail "
                  "charge; the HEDGE shown to work as a hedge (per closed episode, the "
                  "SD of the basis residual over the SD of the price move it hedged, "
                  "upper95 of the bootstrap < 1); and the incremental return versus "
                  "passive NOT significantly negative (upper95 >= 0). The block-level "
                  "SD ratio and the risk-adjusted difference are reported with their "
                  "uncertainty and do not gate: occupancy is a property of the entry "
                  "gate, not of the hedge. Noise alone never blocks or passes a GO."),
    "degenerate": ["BLOCK_VALUES_CONSTANT", "PASSIVE_RETURN_CONSTANT",
                   "PRICE_FEED_FROZEN", "FUNDING_SEQUENCE_REPEATED",
                   "TOO_FEW_DISTINCT_FUNDING_VALUES"],
    "capital": ("charged on committed_capital_fraction x notional x the mean price "
                "growth since entry; an explicit zero of either input needs a "
                "stated basis; a materiality floor in USD per year is required"),
    "verdicts": ["GO", "NO_GO", "INSUFFICIENT"],
    "meaning": {
        "GO": ("Over one pre-declared window, the dependence-adjusted lower 95% bound "
               "of the net carry per calendar day, after execution costs, the "
               "unseen-tail charge and the owner's cost of capital, is above zero "
               "and above the owner's materiality floor; the hedge did not add risk "
               "to the BTC held and did not significantly lose wealth versus holding "
               "it; capacity, drawdown and margin facts hold. Evidence for a small "
               "pilot under the owner's signed charter, not a forecast, not cash."),
        "NO_GO": ("Adequate sample, one analysis, and even the most favourable reading "
                  "(lower impact bound, zero capital cost) has an upper 95% bound at "
                  "or below zero."),
        "INSUFFICIENT": ("Anything else: unreadable or degenerate evidence, analysis "
                         "not yet due, too few blocks or episodes, a missing owner "
                         "input, or a result that is neither clearly positive nor "
                         "clearly absent."),
    },
    "never": ["a verdict before the analysis date", "re-testing the same window as it grows",
              "treating a missing owner input as zero", "calling a simulation cash",
              "extrapolating cost beyond the measured sizes"],
    "cash": False,
    "resolved_parameters": {
        "block_days": BLOCK_DAYS, "boot": [BOOT_SEED, BOOT_N],
        "degenerate": [MIN_BLOCK_SD_BPS_PER_DAY, PRICE_FROZEN_SHARE, REPEAT_WINDOW_PRINTS,
                       MIN_DISTINCT_WINDOW_SHARE, MIN_DISTINCT_PRINT_VALUES],
        "v2_inference": v2.PROTOCOL["resolved_parameters"],
        "v2_entry_gate_and_exit": v2.PROTOCOL["resolved_parameters"]["entry_gate"],
    },
}


def protocol_hash() -> str:
    return hashlib.sha256(json.dumps(PROTOCOL, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


_finite = v2._finite

# -- statistics over BLOCK values ------------------------------------------------------------

def _boot(values: Sequence[float], stat, rng: random.Random, n: int = BOOT_N) -> List[float]:
    k = len(values)
    out = []
    for _ in range(n):
        out.append(stat([values[rng.randrange(k)] for _ in range(k)]))
    out.sort()
    return out


def block_bounds(values: Sequence[float]) -> Dict[str, Any]:
    """Mean of block values with the v2 dependence-adjusted bounds, widened by a
    fixed-seed whole-block bootstrap (blocks are the independent unit)."""
    b = v2.mean_bounds(values)
    vals = [float(x) for x in values if _finite(x)]
    if len(vals) >= 3 and b["lower95"] is not None:
        bs = _boot(vals, lambda xs: sum(xs) / len(xs), random.Random(BOOT_SEED))
        b = dict(b, lower95=min(b["lower95"], bs[int(0.05 * BOOT_N)]),
                 upper95=max(b["upper95"], bs[int(0.95 * BOOT_N) - 1]),
                 method="block_bootstrap_widened")
    return b


def poisson_upper95(k: int) -> float:
    """Exact one-sided 95% upper bound on a Poisson mean given k events seen
    (3.0 for none: the rule of three)."""
    table = {0: 2.996, 1: 4.744, 2: 6.296, 3: 7.754, 4: 9.154, 5: 10.513,
             6: 11.842, 7: 13.148, 8: 14.435, 9: 15.705, 10: 16.962}
    if k in table:
        return table[k]
    return k + 1.645 * math.sqrt(k) + 1.0


def unseen_tail_charge_bps_per_episode(net_bps: Sequence[float], stress_loss_bps: float
                                       ) -> Dict[str, Any]:
    """What an episode must pay for tail losses the sample did not show."""
    n = len(net_bps)
    if n == 0 or not _finite(stress_loss_bps) or stress_loss_bps <= 0:
        return {"status": UNAVAILABLE, "charge_bps_per_episode": None}
    k = sum(1 for x in net_bps if x <= -stress_loss_bps)
    ub = poisson_upper95(k) / n
    extra = max(0.0, ub - k / n)
    return {"status": "MEASURED", "observed_tail_episodes": k, "episodes": n,
            "upper95_frequency": ub, "stress_loss_bps": float(stress_loss_bps),
            "charge_bps_per_episode": extra * float(stress_loss_bps)}


# -- degenerate samples ---------------------------------------------------------------------

def degenerate_reasons(rows: Sequence[Dict[str, Any]]) -> List[str]:
    """Named reasons the observations cannot carry an economic conclusion."""
    out: List[str] = []
    if not rows:
        return out
    spots = [round(float(r["obs"]["spot_mark"]), 6) for r in rows]
    top = max((spots.count(s) for s in set(spots)), default=0)
    if len(spots) > 10 and top / len(spots) >= PRICE_FROZEN_SHARE:
        out.append("PRICE_FEED_FROZEN")
    seq: Dict[int, float] = {}
    for r in rows:
        for bps, ms in r["obs"].get("settled_prints", []):
            seq.setdefault(int(ms), round(float(bps), 9))
    vals = [seq[k] for k in sorted(seq)]
    if len(set(vals)) < MIN_DISTINCT_PRINT_VALUES and len(vals) >= 24:
        out.append("TOO_FEW_DISTINCT_FUNDING_VALUES")
    w = REPEAT_WINDOW_PRINTS
    if len(vals) >= 2 * w:
        windows = [tuple(vals[i:i + w]) for i in range(0, len(vals) - w + 1, w // 2)]
        if len(set(windows)) / len(windows) < MIN_DISTINCT_WINDOW_SHARE:
            out.append("FUNDING_SEQUENCE_REPEATED")
    return out


# -- episodes -> block values -----------------------------------------------------------------

def _growth(rows: Sequence[Dict[str, Any]], e: Dict[str, Any]) -> float:
    """Mean spot over the episode relative to entry: margin on a fixed BTC
    quantity grows with price."""
    ts = [r["obs"]["observed_at_s"] for r in rows]
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
                   if r["obs"]["observed_at_s"] <= b1), None)
        out.append(((s1 / s0 - 1.0) * 1e4 / BLOCK_DAYS) if s0 and s1 else float("nan"))
    return out


def _sd(xs: Sequence[float]) -> float:
    n = len(xs)
    if n < 2:
        return 0.0
    m = sum(xs) / n
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (n - 1))


def paired_ratio_bounds(num: Sequence[float], den: Sequence[float]) -> Dict[str, Any]:
    """sd(num)/sd(den) with a paired whole-block bootstrap."""
    k = len(num)
    if k < 3 or _sd(den) <= 0:
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
        "provenance": v2._provenance(fwd, dict(inputs, protocol=SCHEMA))}
    span_days = ((fwd[-1]["obs"]["observed_at_s"] - fwd[0]["obs"]["observed_at_s"]) / 86400.0
                 if len(fwd) > 1 else 0.0)
    out["span_days"] = span_days
    out["distinct_settled_prints"] = len({int(p[1]) for r in fwd for p in r["obs"].get("settled_prints", [])})
    out["analysis_due"] = (bool(fwd) and fwd[-1]["obs"]["observed_at_s"]
                           >= EVALUATION_START_S + ANALYSIS_HORIZON_DAYS * 86400 - 3600 * 3)
    deg = degenerate_reasons(fwd) if fwd and not chain_issues else []
    out["degenerate"] = deg
    sim = (v2.simulate_episodes(fwd, planned_notional_usd, taker_fee_bps)
           if fwd and not chain_issues else
           {"closed": [], "open_at_end": False, "open_marked": None})
    closed = sim["closed"]
    priced = [e for e in closed if _finite(e.get("carry_bps")) and _finite(e.get("carry_bps_most_favourable"))]
    unpriced = [e for e in closed if e not in priced]
    out["episodes"] = {"closed": len(closed), "priced": len(priced), "unpriced": len(unpriced),
                       "unpriced_fraction": len(unpriced) / len(closed) if closed else 0.0,
                       "open_at_end": sim["open_at_end"]}

    # capital: an explicit zero needs a stated basis; fractions validated
    cap_status = "OWNER_SUPPLIED"
    if owner_capital_apr is None or committed_capital_fraction is None:
        cap_status = INSUFFICIENT_OWNER_INPUT
    elif not (_finite(owner_capital_apr) and 0 <= owner_capital_apr <= 10
              and _finite(committed_capital_fraction) and 0 <= committed_capital_fraction <= 1):
        cap_status = "INVALID_OWNER_INPUT"
    elif (owner_capital_apr == 0 or committed_capital_fraction == 0) and not (
            isinstance(capital_cost_basis, str) and capital_cost_basis.strip()):
        cap_status = "ZERO_CAPITAL_COST_NEEDS_A_STATED_BASIS"
    out["capital"] = {"status": cap_status, "applied_apr": owner_capital_apr,
                      "committed_fraction": committed_capital_fraction,
                      "basis": capital_cost_basis}

    n_blocks = int(span_days // BLOCK_DAYS) if fwd else 0
    out["blocks"] = {"complete": n_blocks, "block_days": BLOCK_DAYS}
    if not (priced and n_blocks >= 1):
        out["verdict"], out["verdict_reasons"] = _verdict(out, None, owner_drawdown_limit_usd,
                                                          min_annual_net_usd)
        return out

    # adverse / favourable valuation of unpriced episodes, by rate per day
    def rate(e: Dict[str, Any], key: str) -> float:
        return e[key] / max(e["hold_days"], 1e-9)
    worst_rate = min(rate(e, "carry_bps") for e in priced)
    best_rate = max(rate(e, "carry_bps_most_favourable") for e in priced)
    # an input that is missing or invalid is never used in the arithmetic (it
    # would poison every statistic before the refusal could be reported)
    supplied = cap_status == "OWNER_SUPPLIED"
    apr = float(owner_capital_apr) if supplied else 0.0
    frac = float(committed_capital_fraction) if supplied else 0.0
    scale = planned_notional_usd / 1e4
    enriched = []
    for e in closed:
        g = _growth(fwd, e)
        capital_bps = apr * frac * g * e["hold_days"] / 365.0 * 1e4
        adv = e["carry_bps"] if e in priced else worst_rate * e["hold_days"]
        fav = e["carry_bps_most_favourable"] if e in priced else best_rate * e["hold_days"]
        passive = e.get("passive_return_bps", 0.0)
        enriched.append(dict(e, net_adv=adv - capital_bps, net_fav=fav, carry_adv=adv,
                             incremental_adv=(adv - capital_bps) - passive,
                             combined_adv=None))
    om = sim.get("open_marked")
    if om is not None and _finite(om.get("carry_bps")):
        g = _growth(fwd, om)
        cbps = apr * frac * g * om["hold_days"] / 365.0 * 1e4
        enriched.append(dict(om, net_adv=om["carry_bps"] - cbps, net_fav=om["carry_bps"],
                             carry_adv=om["carry_bps"],
                             incremental_adv=(om["carry_bps"] - cbps) - om.get("passive_return_bps", 0.0),
                             open=True))
    start_s = float(EVALUATION_START_S if fwd[0]["obs"]["observed_at_s"] <= EVALUATION_START_S
                    else fwd[0]["obs"]["observed_at_s"])
    start_s = float(fwd[0]["obs"]["observed_at_s"])
    carry_pd = allocate_to_blocks(enriched, start_s, n_blocks, "carry_adv")
    net_pd = allocate_to_blocks(enriched, start_s, n_blocks, "net_adv")
    fav_pd = allocate_to_blocks(enriched, start_s, n_blocks, "net_fav")
    incr_pd = allocate_to_blocks(enriched, start_s, n_blocks, "incremental_adv")
    passive_pd = _passive_blocks(fwd, start_s, n_blocks)
    combined_pd = [p + i for p, i in zip(passive_pd, incr_pd)]

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
        resid.append(-(basis_out - basis_in) / float(a["spot_mark"]) * 1e4)   # the hedge's basis P&L
        moved.append(float(e["passive_return_bps"]))                          # the price move it hedged
    out["hedge_effectiveness"] = dict(
        paired_ratio_bounds(resid, moved), episodes=len(resid),
        note="per closed episode: SD of the basis residual / SD of the price move hedged")

    stress = None
    if _finite(funding_cap_bps) and _finite(price_limit_ratio) and funding_cap_bps > 0 and price_limit_ratio > 0:
        stress = 3.0 * float(funding_cap_bps) + float(price_limit_ratio) * 1e4
    tail = unseen_tail_charge_bps_per_episode([e["net_adv"] for e in enriched if not e.get("open")],
                                              stress or 0.0)
    eps_per_day = len(closed) / max(span_days, 1e-9)
    tail_pd = ((tail["charge_bps_per_episode"] or 0.0) * eps_per_day
               if tail["status"] == "MEASURED" else None)
    out["tail"] = dict(tail, charge_bps_per_day=tail_pd, episodes_per_day=eps_per_day)

    net_after_tail = [x - (tail_pd or 0.0) for x in net_pd]
    out["positive_carry_after_costs_bps_per_day"] = dict(block_bounds(carry_pd),
        note="after execution costs, before capital cost and before the unseen-tail charge")
    out["net_of_capital_bps_per_day"] = block_bounds(net_pd)
    out["net_after_tail_charge_bps_per_day"] = block_bounds(net_after_tail)
    out["most_favourable_bps_per_day"] = block_bounds(fav_pd)
    out["absolute_overlay_portfolio_return_bps_per_day"] = dict(block_bounds(combined_pd),
        note="the owner's BTC with the overlay on only while the gate was open (idle time unhedged)")
    out["passive_btc_return_bps_per_day"] = block_bounds(passive_pd)
    out["incremental_vs_passive_bps_per_day"] = dict(block_bounds(incr_pd),
        note="includes the BTC drift forgone while hedged; not an edge, a cost of hedging")
    out["hedge_risk_ratio"] = dict(paired_ratio_bounds(combined_pd, passive_pd),
        note="sd of the combined position's block returns / sd of passive BTC's")
    out["risk_adjusted_vs_passive"] = dict(sharpe_diff_bounds(combined_pd, passive_pd),
        note="difference in block mean/sd; reported, with its uncertainty, not a gate on its own")
    ann_bps = [x * 365.0 for x in net_after_tail]
    nb = block_bounds(net_after_tail)
    out["annualised"] = {"net_bps_of_notional_lower95": (nb["lower95"] * 365.0 if nb["lower95"] is not None else None),
                         "net_usd_lower95": (nb["lower95"] * 365.0 * scale if nb["lower95"] is not None else None),
                         "capital_utilisation": sum(e["hold_days"] for e in closed) / max(span_days, 1e-9),
                         "note": "idle time is in the denominator; not a forecast"}
    # path / capacity / margin reuse the v2 definitions
    worst_p = min((e["worst_path_usd"] for e in priced if _finite(e.get("worst_path_usd"))), default=None)
    out["worst_episode_usd"] = worst_p
    budget = ((sum(e["carry_adv"] + (e.get("impact_upper_bps") or 0.0) for e in enriched if not e.get("open"))
               / max(1, len([e for e in enriched if not e.get("open")]))) / 2.0)
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
    if rep.get("degenerate"):
        return "INSUFFICIENT", list(rep["degenerate"])
    if not rep.get("analysis_due"):
        return "INSUFFICIENT", ["ANALYSIS_NOT_YET_DUE"]
    if rep["blocks"]["complete"] < MIN_COMPLETE_BLOCKS:
        why.append(f"FEWER_THAN_{MIN_COMPLETE_BLOCKS}_COMPLETE_BLOCKS")
    ep = rep["episodes"]
    if ep["priced"] < MIN_CLOSED_EPISODES:
        why.append(f"FEWER_THAN_{MIN_CLOSED_EPISODES}_PRICED_CLOSED_EPISODES")
    if ep["unpriced_fraction"] > MAX_UNPRICED_FRACTION:
        why.append("TOO_MANY_EPISODES_UNPRICEABLE")
    if why:
        return "INSUFFICIENT", why
    for key in ("net_of_capital_bps_per_day", "passive_btc_return_bps_per_day"):
        if key not in rep:
            return "INSUFFICIENT", ["NO_BLOCK_STATISTICS"]
    if rep["passive_btc_return_bps_per_day"]["sd"] in (None, 0.0) or \
            rep["passive_btc_return_bps_per_day"]["sd"] < MIN_BLOCK_SD_BPS_PER_DAY:
        return "INSUFFICIENT", ["PASSIVE_RETURN_CONSTANT"]
    sd_net = rep["net_after_tail_charge_bps_per_day"]["sd"]
    if sd_net is None or sd_net < MIN_BLOCK_SD_BPS_PER_DAY:
        return "INSUFFICIENT", ["BLOCK_VALUES_CONSTANT"]
    fav = rep["most_favourable_bps_per_day"]
    if fav["upper95"] is not None and fav["upper95"] <= 0:
        return "NO_GO", ["UPPER_95_BOUND_NOT_ABOVE_ZERO_EVEN_AT_BEST_CASE"]
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
    if he["upper95"] >= 1.0:
        return "INSUFFICIENT", ["HEDGE_NOT_SHOWN_TO_REMOVE_PRICE_RISK"]
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
