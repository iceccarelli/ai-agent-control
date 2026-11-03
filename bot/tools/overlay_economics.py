#!/usr/bin/env python3
"""Does the OVERLAY funding carry make money after what it really costs?

A read-only, offline evaluation of the mainnet PUBLIC evidence file
(`tools/mainnet_public.py`) for the overlay mode: the owner already holds the
BTC, and the only things traded are two perp legs (sell to open, buy to close).
It places no order, reads no account, and every figure it prints is SIMULATED
from public data. None of it is cash and none of it is a P&L.

WHY A SECOND MODULE
  `mainnet_public` is frozen (its protocol hash is pinned) and its report is
  the acquire structure: four legs, spot and perp. Overlay never trades spot, so
  that report overstates overlay costs and says nothing about the passive BTC
  the owner already holds. This module is a separate, versioned protocol
  (`overlay_economics/1`) that reads the same observations but is judged only
  on observations made AFTER its declaration (`EVALUATION_START_S`). Rows
  observed earlier are counted and set aside; they cannot decide the verdict.

THE BENCHMARK IS THE OWNER'S OWN BTC
  An episode's overlay result and the passive result (just holding the same BTC
  over the same window, valued at the same spot) share the BTC leg exactly, so
  the INCREMENTAL result is the perp short's P&L, plus funding, minus costs.
  That increment still contains the price exposure the short removes. It is
  split into a directional part (what hedging gave up or avoided; not an edge)
  and the CARRY part (funding - costs - basis change), and only the carry part
  is judged.

WHAT IT REFUSES TO DO
  Extrapolate execution cost beyond the sizes the book was actually walked at,
  count hourly observations as independent, invent a capital cost, or turn a
  missing input into a favourable number. Each of those is a named UNAVAILABLE /
  INSUFFICIENT_* answer.

    python3 tools/overlay_economics.py report --db /data/mainnet/mainnet_public.jsonl \\
        --notional 100 [--capital-apr 0.05] [--drawdown-limit-usd 20]
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

import carry_costs                                          # noqa: E402
import mainnet_public as mp                                 # noqa: E402

SCHEMA = "overlay_economics/1"
UNAVAILABLE = "UNAVAILABLE"
INSUFFICIENT_OWNER_INPUT = "INSUFFICIENT_OWNER_INPUT"
SIMULATED = "SIMULATED_FROM_PUBLIC_DATA"

#: Declared 2026-11-03 00:00 UTC. Observations made before this are exploratory
#: and are never used for the verdict.
EVALUATION_START_S = 1_793_664_000

#: The sizes the public book was actually walked at (mainnet_public).
MEASURED_NOTIONALS = tuple(float(n) for n in mp.BOOK_NOTIONALS_USD)

#: Per leg, what Bybit linear charges a taker (the repo's own constant).
TAKER_FEE_BPS = carry_costs.TAKER_BPS_PERP

#: Capital-cost sensitivity grid. A GRID, not an assumption: it is printed
#: beside the result so the owner can see where the sign flips.
CAPITAL_APR_GRID = (0.0, 0.02, 0.05, 0.10)

#: The exit rule is the frozen engine's: leave after this many consecutive
#: negative settled prints (carry_engine.NEGATIVE_FUNDING_EXIT_PRINTS).
NEGATIVE_EXIT_PRINTS = 3
#: Funding settles every 8 hours on the proven symbol; a print series that is
#: not spaced like that is evidence we do not understand, not evidence.
PRINT_SPACING_S = 8 * 3600
PRINT_SPACING_TOLERANCE_S = 120
#: A print stamped later than the observation that carries it (beyond clock
#: skew) would be look-ahead.
PRINT_CLOCK_SKEW_S = 120
#: Declared analysis horizon: rows after this are a different test.
ANALYSIS_HORIZON_DAYS = 120
#: Largest tolerated share of unpriced book sides when judging capacity.
MAX_UNPRICED_BOOK_FRACTION = 0.05
#: Block bootstrap of the mean: fixed seed and size, so a report is reproducible.
BOOTSTRAP_SEED = 20261103
BOOTSTRAP_RESAMPLES = 2000
BOOTSTRAP_MIN_N = 8

#: One-sided 95% Student-t critical values by degrees of freedom; between rows
#: the value for the SMALLER df is used (wider, never narrower).
_T95 = ((1, 6.314), (2, 2.920), (3, 2.353), (4, 2.132), (5, 2.015), (6, 1.943),
        (7, 1.895), (8, 1.860), (9, 1.833), (10, 1.812), (15, 1.753),
        (20, 1.725), (30, 1.697), (60, 1.671), (120, 1.658))
_Z95 = 1.645

#: The question, the sample the answer needs, and what each answer means. Written
#: before any forward observation was read; the hash is pinned by tests.
PROTOCOL = {
    "schema": SCHEMA,
    "declared": "2026-11-03, before any observation it judges was read",
    "mode": "overlay: two perp legs only; the owner's BTC is never traded",
    "question": ("After measured execution cost, fees, basis change and an "
                 "owner-supplied cost of capital, does the frozen entry gate "
                 "and exit rule earn more than zero per independent episode, "
                 "and at what size?"),
    "unit_of_independence": ("one CLOSED episode (gate-open entry to its exit). "
                             "Hourly observations are NOT independent: eight "
                             "of them share each 8-hour funding print."),
    "strategy": ("the frozen production rules, unmodified: carry_costs."
                 "entry_gate at the monitor's overlay settings, and an exit "
                 "after three consecutive negative settled prints"),
    "adequacy": {"min_closed_episodes": 30, "min_span_days": 60,
                 "min_distinct_settled_prints": 100},
    "evaluation_start_s": EVALUATION_START_S,
    "analysis_horizon_days": ANALYSIS_HORIZON_DAYS,
    "analysis_rule": ("one analysis: only rows observed in [evaluation_start_s, "
                      "evaluation_start_s + analysis_horizon_days] are judged; "
                      "later rows belong to a new protocol, so the same data "
                      "cannot be re-tested as it grows"),
    "inference": ("one-sided 95% bound on the mean carry per closed episode: the "
                  "wider of a Student-t interval whose standard error is inflated "
                  "by sqrt((1+rho1)/(1-rho1)) for positive lag-1 autocorrelation, "
                  "and a fixed-seed moving-block bootstrap percentile"),
    "unpriceable_episodes": ("never dropped: for the GO path each is valued at "
                             "the worst priced episode, for the NO_GO path at "
                             "the best; more than max_unpriced_fraction of them "
                             "is INSUFFICIENT"),
    "benchmark_rule": ("the hedged overlay must not trail passive BTC per unit "
                       "of risk: mean/sd of the overlay's episode returns must "
                       "be at least that of the same BTC held unhedged"),
    "resolved_parameters": {
        "t95": [list(r) for r in _T95], "z95": _Z95,
        "capital_apr_grid": list(CAPITAL_APR_GRID),
        "negative_exit_prints": NEGATIVE_EXIT_PRINTS,
        "print_spacing_s": PRINT_SPACING_S,
        "print_spacing_tolerance_s": PRINT_SPACING_TOLERANCE_S,
        "print_clock_skew_s": PRINT_CLOCK_SKEW_S,
        "max_unpriced_book_fraction": MAX_UNPRICED_BOOK_FRACTION,
        "bootstrap": [BOOTSTRAP_SEED, BOOTSTRAP_RESAMPLES, BOOTSTRAP_MIN_N],
        "capacity_budget_rule": ("half the mean pre-impact edge per leg; "
                                 "95th-percentile one-way impact per measured size"),
        "entry_gate": {"round_trip_bps": mp.GATE_ROUND_TRIP_BPS,
                       "borrow_apr": mp.GATE_BORROW_APR,
                       "ewma_alpha": carry_costs.EWMA_ALPHA,
                       "ewma_min_prints": carry_costs.EWMA_MIN_PRINTS,
                       "assumed_hold_days": carry_costs.ASSUMED_HOLD_DAYS,
                       "demonstrated_hold_days": carry_costs.DEMONSTRATED_HOLD_DAYS,
                       "opportunity_cost_apr": carry_costs.DEFAULT_OPPORTUNITY_COST_APR},
        "measured_notionals_usd": list(MEASURED_NOTIONALS)},
    "taker_fee_bps_per_leg": TAKER_FEE_BPS,
    "max_gap_between_observations_s": 3 * 3600,
    "max_unpriced_fraction": 0.10,
    "min_liquidation_distance_pct": 15.0,
    "inputs_the_owner_must_supply": [
        "CARRY_OPPORTUNITY_COST_APR", "committed_capital_fraction",
        "drawdown limit (USD)",
        "maintenance_margin_rate", "collateral_ratio (venue values for the "
        "owner's account)"],
    "open_episodes": ("an episode still open at the last observation is marked "
                      "to market as if closed there and GO must hold with it "
                      "included"),
    "confidence": "one-sided 95% (Student t, smaller-df row between table rows)",
    "execution_cost": ("measured from the public book at the two sizes it was "
                       "walked at; between them bounded, above them UNAVAILABLE"),
    "verdicts": ["GO", "NO_GO", "INSUFFICIENT"],
    "meaning": {
        "GO": ("The dependence-adjusted lower 95% bound of the mean CARRY result "
               "per closed episode is above zero after the UPPER execution-cost "
               "bound and the owner's cost of capital (with any open episode "
               "marked and unpriceable episodes valued adversely); the sample is "
               "adequate; the hedged overlay does not trail the same BTC held "
               "unhedged per unit of risk; the planned size is inside measured "
               "capacity; the worst episode path is inside the owner's drawdown "
               "limit; and the owner's venue margin facts put liquidation "
               "beyond the engine's own floor. Evidence to run a small "
               "real-money pilot under the owner's signed charter, not a "
               "forecast of profit."),
        "NO_GO": ("The sample is adequate and even the MOST favourable reading "
                  "(lower execution-cost bound, zero cost of capital) has an "
                  "upper 95% bound at or below zero: there is no edge here at "
                  "this cost structure."),
        "INSUFFICIENT": ("Anything else: too few independent episodes, too "
                         "short a span, unreadable evidence, a missing owner "
                         "input, unmeasurable cost or capacity at the planned "
                         "size, or a result that is neither clearly positive "
                         "nor clearly absent. Nothing is concluded and no "
                         "capital is justified."),
    },
    "never": ["a result from rows observed before the declaration",
              "treating a missing owner input as zero",
              "extrapolating cost beyond the measured sizes",
              "changing the gate, exit rule, thresholds or sample sizes after "
              "seeing the data"],
    "cash": False,
}


def protocol_hash() -> str:
    return hashlib.sha256(json.dumps(PROTOCOL, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


# -- statistics ---------------------------------------------------------------

def t_crit(n: int) -> float:
    """One-sided 95% critical value for a mean of n values. The row for the
    largest tabulated df not above n-1 (wider than the exact value, never
    narrower)."""
    df = n - 1
    if df < 1:
        return float("inf")
    out = _T95[0][1]
    for d, v in _T95:
        if df >= d:
            out = v
    return out


def _lag1(vals: Sequence[float], mean: float) -> float:
    den = sum((v - mean) ** 2 for v in vals)
    if den <= 0 or len(vals) < 3:
        return 0.0
    num = sum((vals[i] - mean) * (vals[i + 1] - mean) for i in range(len(vals) - 1))
    return max(-0.95, min(0.95, num / den))


def _boot_bounds(vals: Sequence[float]) -> Tuple[float, float]:
    """5th / 95th percentile of the mean under a fixed-seed moving-block
    bootstrap (block length n**(1/3)), which keeps serial dependence and skew."""
    n = len(vals)
    rng = random.Random(BOOTSTRAP_SEED)
    block = max(1, int(round(n ** (1.0 / 3.0))))
    means = []
    for _ in range(BOOTSTRAP_RESAMPLES):
        total, count = 0.0, 0
        while count < n:
            st = rng.randrange(0, n - block + 1)
            take = min(block, n - count)
            total += sum(vals[st:st + take])
            count += take
        means.append(total / n)
    means.sort()
    return (means[int(0.05 * BOOTSTRAP_RESAMPLES)],
            means[int(0.95 * BOOTSTRAP_RESAMPLES) - 1])


def mean_bounds(values: Sequence[float]) -> Dict[str, Any]:
    """Mean and one-sided 95% bounds of `values`, TAKEN IN TIME ORDER.

    Episodes are not independent: funding persists and the gate's EWMA windows
    overlap. A plain t-interval then passes a true-zero mean 11-25% of the time
    instead of 5%. So the standard error is inflated by sqrt((1+rho1)/(1-rho1))
    when the lag-1 autocorrelation is positive, and the bound is the wider of
    that and a block-bootstrap percentile (which also respects skew). n < 2 has
    no spread, so no bound."""
    vals = [float(v) for v in values if isinstance(v, (int, float))
            and not isinstance(v, bool) and math.isfinite(v)]
    n = len(vals)
    if n == 0:
        return {"n": 0, "mean": None, "sd": None, "rho1": None,
                "lower95": None, "upper95": None}
    mean = sum(vals) / n
    if n < 2:
        return {"n": n, "mean": mean, "sd": None, "rho1": None,
                "lower95": None, "upper95": None}
    sd = math.sqrt(sum((v - mean) ** 2 for v in vals) / (n - 1))
    # the sample lag-1 autocorrelation is biased towards zero in short series
    # (Kendall); correct it upward, never downward
    raw = _lag1(vals, mean)
    rho = min(0.95, raw + (1.0 + 3.0 * raw) / n) if raw > 0 else raw
    infl = math.sqrt((1.0 + rho) / (1.0 - rho)) if rho > 0 else 1.0
    half = t_crit(n) * sd / math.sqrt(n) * infl
    lower, upper = mean - half, mean + half
    method = "t_inflated"
    if n >= BOOTSTRAP_MIN_N and sd > 0:
        bl, bu = _boot_bounds(vals)
        if bl < lower:
            lower, method = bl, "block_bootstrap"
        upper = max(upper, bu)
    return {"n": n, "mean": mean, "sd": sd, "rho1": rho, "lower95": lower,
            "upper95": upper, "method": method}


# -- execution cost: measured sizes only, bounded between, nothing beyond ------

def leg_impact_bounds(book: Optional[Dict[str, Any]], side: str,
                      notional_usd: float) -> Dict[str, Any]:
    """The cost, in bps of mid, of a market order of `notional_usd` on one side
    of one stored book snapshot.

    The snapshot holds the cost at exactly the declared sizes. A walked book's
    cost never falls as the order grows, so between two measured sizes the cost
    lies between their costs, and below the smallest it lies between half the
    spread (the least any market order can pay) and the smallest measured cost.
    Above the largest measured size nothing is known: UNAVAILABLE."""
    if side not in ("buy", "sell"):
        raise ValueError("side must be buy or sell")
    if not (isinstance(notional_usd, (int, float)) and math.isfinite(notional_usd)
            and notional_usd > 0):
        return {"status": UNAVAILABLE, "reason": "NOTIONAL_NOT_POSITIVE"}
    if not isinstance(book, dict) or not isinstance(book.get("impact_bps"), dict):
        return {"status": UNAVAILABLE, "reason": "NO_BOOK_MEASUREMENT"}
    table: List[Tuple[float, Optional[float]]] = []
    for k, v in book["impact_bps"].items():
        try:
            n = float(k)
        except (TypeError, ValueError):
            continue
        x = v.get(side) if isinstance(v, dict) else None
        table.append((n, float(x) if isinstance(x, (int, float))
                      and math.isfinite(x) else None))
    table.sort()
    if not table:
        return {"status": UNAVAILABLE, "reason": "NO_BOOK_MEASUREMENT"}
    n = float(notional_usd)
    for size, cost in table:
        if abs(size - n) < 1e-9:
            if cost is None:
                return {"status": UNAVAILABLE, "reason": "BOOK_TOO_THIN_AT_SIZE"}
            return {"status": "MEASURED", "lower_bps": cost, "upper_bps": cost}
    if n > table[-1][0]:
        return {"status": UNAVAILABLE, "reason": "ABOVE_LARGEST_MEASURED_SIZE",
                "largest_measured_usd": table[-1][0]}
    hi = next(((s, c) for s, c in table if s > n), None)
    lo = [(s, c) for s, c in table if s < n]
    if hi is None or hi[1] is None:
        return {"status": UNAVAILABLE, "reason": "BOOK_TOO_THIN_AT_SIZE"}
    if lo:
        if lo[-1][1] is None:
            return {"status": UNAVAILABLE, "reason": "BOOK_TOO_THIN_AT_SIZE"}
        lower = lo[-1][1]
    else:
        spread = book.get("spread_bps")
        if not (isinstance(spread, (int, float)) and math.isfinite(spread)
                and spread >= 0):
            return {"status": UNAVAILABLE, "reason": "NO_SPREAD"}
        lower = min(spread / 2.0, hi[1])
    return {"status": "BOUNDED", "lower_bps": lower, "upper_bps": hi[1]}


def overlay_cost_bounds(entry_obs: Dict[str, Any], exit_obs: Dict[str, Any],
                        notional_usd: float,
                        taker_fee_bps: float = TAKER_FEE_BPS) -> Dict[str, Any]:
    """Round-trip execution cost of an overlay: a perp SELL at entry and a perp
    BUY at exit, two taker fees. No spot leg exists in overlay, so none is
    priced. UNAVAILABLE when either leg cannot be bounded."""
    sell = leg_impact_bounds(((entry_obs.get("book") or {}).get("linear")),
                             "sell", notional_usd)
    buy = leg_impact_bounds(((exit_obs.get("book") or {}).get("linear")),
                            "buy", notional_usd)
    if sell["status"] == UNAVAILABLE or buy["status"] == UNAVAILABLE:
        return {"status": UNAVAILABLE,
                "reason": sell.get("reason") or buy.get("reason"),
                "legs": 2, "spot_legs": 0}
    fees = 2.0 * float(taker_fee_bps)
    return {"status": "MEASURED" if sell["status"] == buy["status"] == "MEASURED"
            else "BOUNDED",
            "legs": 2, "spot_legs": 0, "fees_bps": fees,
            "impact_lower_bps": sell["lower_bps"] + buy["lower_bps"],
            "impact_upper_bps": sell["upper_bps"] + buy["upper_bps"],
            "total_lower_bps": fees + sell["lower_bps"] + buy["lower_bps"],
            "total_upper_bps": fees + sell["upper_bps"] + buy["upper_bps"]}


# -- one episode: overlay result minus passive BTC, no double counting ----------

def _worst_path(rows: Sequence[Dict[str, Any]], i: int, j: int, qty: float,
                cost_usd: float) -> float:
    """The lowest the hedged position's carry result stood at any observation:
    basis change so far + funding settled so far - the full round-trip cost.
    Price direction is excluded (the BTC offsets it), so this is the drawdown a
    hedged book feels in its own P&L, not its margin pressure."""
    a = rows[i]["obs"]
    base = float(a["perp_mark"]) - float(a["spot_mark"])
    worst, funding = 0.0 - cost_usd, 0.0
    t_prev = a["observed_at_s"] * 1000.0
    for m in range(i + 1, j + 1):
        o = rows[m]["obs"]
        t = o["observed_at_s"] * 1000.0
        for bps, ms in _prints_between(rows, i, m):
            if t_prev < ms <= t:
                funding += bps / 1e4 * qty * float(o["perp_mark"])
        t_prev = t
        basis_pnl = -qty * ((float(o["perp_mark"]) - float(o["spot_mark"])) - base)
        worst = min(worst, basis_pnl + funding - cost_usd)
    return worst


def _prints_between(rows: Sequence[Dict[str, Any]], i: int, j: int
                    ) -> List[Tuple[float, int]]:
    """Distinct settled prints with entry_time < print_time <= exit_time."""
    t0 = rows[i]["obs"]["observed_at_s"] * 1000.0
    t1 = rows[j]["obs"]["observed_at_s"] * 1000.0
    seen: Dict[int, float] = {}
    for r in rows[i:j + 1]:
        for bps, ms in r["obs"].get("settled_prints", []):
            if t0 < ms <= t1:
                seen[int(ms)] = float(bps)
    return sorted(((b, m) for m, b in seen.items()), key=lambda p: p[1])


def episode_result(rows: Sequence[Dict[str, Any]], i: int, j: int,
                   notional_usd: float,
                   taker_fee_bps: float = TAKER_FEE_BPS) -> Dict[str, Any]:
    """The overlay and the passive holding of the SAME BTC from observation i to
    observation j, valued at the same spot. All SIMULATED.

    qty is the BTC the short hedges. passive = qty * (spot_out - spot_in); the
    overlay holds that same BTC, so its BTC leg is identical and cancels from
    the increment, which is therefore the perp P&L + funding - costs. The
    directional part of the increment is -qty * (spot_out - spot_in): it is not
    an edge, only the price exposure the short removed. What remains is the
    carry result."""
    a, b = rows[i]["obs"], rows[j]["obs"]
    spot_in, spot_out = float(a["spot_mark"]), float(b["spot_mark"])
    perp_in, perp_out = float(a["perp_mark"]), float(b["perp_mark"])
    qty = float(notional_usd) / spot_in
    prints = _prints_between(rows, i, j)
    funding_usd = 0.0
    for bps, ms in prints:
        # valued at the latest observation at or before the print, never later
        mark = perp_in
        for r in rows[i:j + 1]:
            if r["obs"]["observed_at_s"] * 1000.0 <= ms:
                mark = float(r["obs"]["perp_mark"])
        funding_usd += bps / 1e4 * qty * mark
    passive_btc = qty * (spot_out - spot_in)
    perp_pnl = qty * (perp_in - perp_out)
    cost = overlay_cost_bounds(a, b, notional_usd, taker_fee_bps)
    gaps = [rows[m + 1]["obs"]["observed_at_s"] - rows[m]["obs"]["observed_at_s"]
            for m in range(i, j)]
    # Both end books are present, so the cost is known; only the path between
    # them is not. Funding is another matter: fewer prints than the elapsed time
    # allows means some were never seen, and the income cannot be stated.
    gap_spanning = bool(gaps) and max(gaps) > PROTOCOL["max_gap_between_observations_s"]
    expected_prints = int((b["observed_at_s"] - a["observed_at_s"]) // PRINT_SPACING_S)
    if len(prints) < expected_prints:
        cost = {"status": UNAVAILABLE, "reason": "FUNDING_PRINTS_MISSING",
                "legs": 2, "spot_legs": 0}
    hold_days = (b["observed_at_s"] - a["observed_at_s"]) / 86400.0
    out: Dict[str, Any] = {
        "class": SIMULATED, "cash": False,
        "entry_s": a["observed_at_s"], "exit_s": b["observed_at_s"],
        "hold_days": hold_days, "settlements_held": len(prints),
        "notional_usd": float(notional_usd), "qty_btc": qty,
        "passive_btc_pnl_usd": passive_btc,
        "overlay_btc_pnl_usd": passive_btc,          # the same BTC, never added twice
        "perp_pnl_marks_usd": perp_pnl, "funding_usd": funding_usd,
        "gap_spanning": gap_spanning, "cost": cost}
    out["directional_component_usd"] = -qty * (spot_out - spot_in)
    out["inventory_value_in_usd"] = qty * spot_in
    out["passive_return_bps"] = (spot_out / spot_in - 1.0) * 1e4
    out["financing_bps"] = 0.0
    out["financing_note"] = ("overlay borrows nothing and trades no spot: "
                             "mainnet_public.GATE_BORROW_APR is 0.0")
    out["impact_upper_bps"] = cost.get("impact_upper_bps")
    if cost["status"] == UNAVAILABLE:
        out["worst_path_usd"] = None
        out["incremental_vs_passive_usd"] = None
        out["carry_usd"] = None
        out["carry_bps"] = None
        out["carry_bps_most_favourable"] = None
        return out
    scale = float(notional_usd) / 1e4
    cost_hi, cost_lo = cost["total_upper_bps"] * scale, cost["total_lower_bps"] * scale
    # a path across a gap is unobserved: no worst point is claimed for it
    out["worst_path_usd"] = (None if gap_spanning
                             else _worst_path(rows, i, j, qty, cost_hi))
    out["overlay_total_usd"] = passive_btc + perp_pnl + funding_usd - cost_hi
    out["overlay_total_return_bps"] = out["overlay_total_usd"] / scale
    out["incremental_vs_passive_bps"] = (out["overlay_total_usd"] - passive_btc) / scale
    out["incremental_vs_passive_usd"] = out["overlay_total_usd"] - passive_btc
    carry = perp_pnl + funding_usd - cost_hi - out["directional_component_usd"]
    out["carry_usd"] = carry
    out["carry_bps"] = carry / scale
    out["carry_bps_most_favourable"] = (
        perp_pnl + funding_usd - cost_lo - out["directional_component_usd"]) / scale
    return out


# -- the frozen strategy, replayed on forward observations ----------------------

def simulate_episodes(rows: Sequence[Dict[str, Any]], notional_usd: float,
                      taker_fee_bps: float = TAKER_FEE_BPS) -> Dict[str, Any]:
    """Replay the frozen entry gate and the three-negative-prints exit on the
    observations, in order. Entry and exit are taken at the observation that
    triggers them (no latency is modelled: stated, not hidden). An episode still
    open at the last observation is reported, never counted."""
    closed: List[Dict[str, Any]] = []
    open_i: Optional[int] = None
    streak, last_print_ms = 0, 0
    for k, row in enumerate(rows):
        o = row["obs"]
        if open_i is None:
            prints = [p[0] for p in o.get("settled_prints", [])]
            v = carry_costs.entry_gate(
                funding_prints_bps=prints, perp=o["perp_mark"],
                spot=o["spot_mark"], borrow_apr=mp.GATE_BORROW_APR,
                round_trip_bps=mp.GATE_ROUND_TRIP_BPS)
            if v.allowed:
                open_i, streak = k, 0
                last_print_ms = max((int(p[1]) for p in o.get("settled_prints", [])),
                                    default=0)
            continue
        exited = False
        for bps, ms in sorted(((p[0], int(p[1])) for p in o.get("settled_prints", [])),
                              key=lambda p: p[1]):
            if ms > last_print_ms:
                streak = streak + 1 if bps < 0 else 0
                last_print_ms = ms
                if streak >= NEGATIVE_EXIT_PRINTS:   # fires at the Nth print itself
                    exited = True
                    break
        if exited:
            closed.append(episode_result(rows, open_i, k, notional_usd, taker_fee_bps))
            open_i = None
            streak = 0
    open_ep = (episode_result(rows, open_i, len(rows) - 1, notional_usd, taker_fee_bps)
               if open_i is not None and open_i < len(rows) - 1 else None)
    return {"closed": closed,
            "open_at_end": open_i is not None,
            "open_since_s": rows[open_i]["obs"]["observed_at_s"]
            if open_i is not None else None,
            "open_marked": open_ep}


# -- capital: unset is not zero --------------------------------------------------

def capital_model(owner_apr: Optional[float], carry_bps: Sequence[float],
                  hold_days: Sequence[float],
                  committed_fraction: Optional[float] = None) -> Dict[str, Any]:
    """The cost of the capital the overlay commits, in bps of notional.

    The owner's BTC is held with or without the overlay, so its opportunity cost
    is in the passive benchmark already and is NOT charged again. Only capital
    the overlay adds (margin posted, collateral encumbered) is a cost, as
    `committed_fraction` of the notional. Both it and `owner_apr` are the
    OWNER's numbers; None is not zero. Either missing is
    INSUFFICIENT_OWNER_INPUT, and the grid beside it prices the worst reading
    (the whole notional as incremental capital) at several rates so the owner
    can see where the sign flips. An explicit zero is a supplied value."""
    pairs = [(c, d) for c, d in zip(carry_bps, hold_days)
             if c is not None and math.isfinite(c)]

    def after(apr: float, frac: float) -> Dict[str, Any]:
        k = apr * frac / 365.0 * 1e4
        return mean_bounds([c - k * d for c, d in pairs])
    grid = {f"{a:.2f}": after(a, 1.0) for a in CAPITAL_APR_GRID}
    base = {"sensitivity": grid,
            "sensitivity_note": ("whole notional treated as incremental capital: "
                                 "the worst reading, shown only to locate the "
                                 "sign flip")}

    def bad(x: Any, hi: float) -> bool:
        return not (isinstance(x, (int, float)) and not isinstance(x, bool)
                    and math.isfinite(x) and 0 <= x <= hi)
    if owner_apr is None:
        return dict(base, status=INSUFFICIENT_OWNER_INPUT, applied_apr=None,
                    needs="CARRY_OPPORTUNITY_COST_APR, supplied by the owner",
                    result=None)
    if bad(owner_apr, 10.0):
        return dict(base, status="INVALID_OWNER_INPUT", applied_apr=None, result=None)
    if committed_fraction is None:
        return dict(base, status=INSUFFICIENT_OWNER_INPUT, applied_apr=float(owner_apr),
                    needs=("committed_capital_fraction: the share of the notional "
                           "the overlay commits beyond the BTC already held"),
                    result=None)
    if bad(committed_fraction, 1.0):
        return dict(base, status="INVALID_OWNER_INPUT", applied_apr=float(owner_apr),
                    result=None)
    apr, frac = float(owner_apr), float(committed_fraction)
    result = after(apr, frac)
    # the cost of capital at which the lower 95% bound reaches zero: the owner's
    # margin of safety on the one assumption this software cannot know
    be: Optional[float] = None
    lo = after(0.0, frac)["lower95"]
    if lo is not None and lo > 0 and frac > 0:
        hi_apr = 10.0
        top = after(hi_apr, frac)["lower95"]
        if top is not None and top > 0:
            be = None                       # above 1000%/yr: effectively unbounded
        else:
            lo_a, hi_a = 0.0, hi_apr
            for _ in range(60):
                mid = (lo_a + hi_a) / 2.0
                v = after(mid, frac)["lower95"]
                if v is not None and v > 0:
                    lo_a = mid
                else:
                    hi_a = mid
            be = lo_a
    elif lo is not None and lo <= 0:
        be = 0.0
    return dict(base, status="OWNER_SUPPLIED", applied_apr=apr,
                committed_fraction=frac, result=result,
                break_even_capital_apr=be,
                break_even_note=("None: still positive at 1000%/yr or no capital "
                                 "committed; 0.0: not positive even at zero cost"))


# -- stress: arithmetic on the position, with the unknowns named ------------------

def stress_table(notional_usd: float, *, leverage: float = 1.0,
                 maintenance_margin_rate: Optional[float] = None,
                 collateral_ratio: Optional[float] = None,
                 funding_cap_bps: Optional[float] = None,
                 price_limit_ratio: Optional[float] = None) -> Dict[str, Any]:
    """What a hedged short loses on its own, and what the code cannot know.

    The perp loses `notional * move` in a rally and the held BTC gains the same;
    the hedge nets to zero only if the venue lets the BTC back the short. The
    liquidation distance depends on the collateral ratio and the maintenance
    margin the VENUE sets, neither of which this repository reads: each is an
    input, and without it the answer is UNAVAILABLE, never a comfortable
    default."""
    n = float(notional_usd)
    for name, val, lo, hi in (("maintenance_margin_rate", maintenance_margin_rate, 0.0, 1.0),
                              ("collateral_ratio", collateral_ratio, 0.0, 1.0)):
        if val is not None and not _stage(val, lo, hi, open_lo=(name == "maintenance_margin_rate"),
                                          open_hi=(name == "maintenance_margin_rate")):
            return {"status": "INVALID_INPUT", "invalid": name,
                    "liquidation_rally_pct_usdt_margin_only": {"status": "INVALID_INPUT"},
                    "liquidation_rally_pct_btc_collateral": {"status": "INVALID_INPUT"},
                    "perp_loss_usd_by_rally": {}, "held_btc_gain_usd_by_rally": {}}
    moves = (0.10, 0.25, 0.50, 1.00)
    out: Dict[str, Any] = {
        "perp_loss_usd_by_rally": {f"{int(m * 100)}%": n * m for m in moves},
        "held_btc_gain_usd_by_rally": {f"{int(m * 100)}%": n * m for m in moves},
        "hedge_nets_to_zero_only_if": ("the venue counts the owner's BTC as "
                                       "collateral at (nearly) full value")}
    if funding_cap_bps is None:
        out["funding_inversion_cost_3_prints_usd"] = {"status": UNAVAILABLE,
                                                      "needs": "funding_cap_bps"}
    else:
        out["funding_inversion_cost_3_prints_usd"] = {
            "status": "BOUND", "usd": 3.0 * n * funding_cap_bps / 1e4}
    if price_limit_ratio is None:
        out["gap_exit_slippage_bound_usd"] = {"status": UNAVAILABLE,
                                              "needs": "price_limit_ratio"}
    else:
        out["gap_exit_slippage_bound_usd"] = {
            "status": "BOUND", "usd": n * price_limit_ratio,
            "note": "the venue's own price band; the exit is an uncapped market buy"}
    if maintenance_margin_rate is None or not leverage or leverage <= 0:
        out["liquidation_rally_pct_usdt_margin_only"] = {
            "status": UNAVAILABLE, "needs": "maintenance_margin_rate, leverage"}
    else:
        mm = float(maintenance_margin_rate)
        out["liquidation_rally_pct_usdt_margin_only"] = {
            "status": "ILLUSTRATIVE", "pct": ((1 + 1 / leverage) / (1 + mm) - 1) * 100,
            "model": "isolated USDT margin; the venue's number governs"}
    if collateral_ratio is None or maintenance_margin_rate is None:
        out["liquidation_rally_pct_btc_collateral"] = {
            "status": UNAVAILABLE, "needs": "collateral_ratio, maintenance_margin_rate"}
    else:
        denom = 1.0 - float(collateral_ratio) + float(maintenance_margin_rate)
        out["liquidation_rally_pct_btc_collateral"] = {
            "status": "ILLUSTRATIVE",
            "pct": (None if denom <= 0 else (1.0 / denom - 1.0) * 100.0),
            "model": "hedged slice only, no USDT; the venue's number governs"}
    return out


# -- capacity: sizes the book was walked at, nothing else -------------------------

def _p95(values: Sequence[float]) -> Optional[float]:
    vs = sorted(v for v in values if isinstance(v, (int, float)) and math.isfinite(v))
    if not vs:
        return None
    return vs[min(len(vs) - 1, max(0, math.ceil(0.95 * len(vs)) - 1))]


def capacity_bounds(rows: Sequence[Dict[str, Any]], impact_budget_bps: float,
                    planned_notional_usd: float) -> Dict[str, Any]:
    """The largest size whose 95th-percentile one-way impact is inside the
    budget, as a bracket between measured sizes. Above the largest measured
    size the answer is UNAVAILABLE; a book that was too thin to price a size
    counts against it."""
    per: Dict[str, Any] = {}
    for size in MEASURED_NOTIONALS:
        key = str(int(size))
        vals, missing = [], 0
        for r in rows:
            lin = (r["obs"].get("book") or {}).get("linear") or {}
            cell = (lin.get("impact_bps") or {}).get(key) or {}
            for side in ("buy", "sell"):
                x = cell.get(side)
                if isinstance(x, (int, float)) and math.isfinite(x):
                    vals.append(float(x))
                else:
                    missing += 1
        p95 = _p95(vals)
        frac = missing / (missing + len(vals)) if (missing + len(vals)) else 1.0
        per[key] = {"p95_bps": p95, "unpriced_sides": missing, "n": len(vals),
                    "unpriced_fraction": frac,
                    "within_budget": (None if p95 is None else
                                      (p95 <= impact_budget_bps
                                       and frac <= MAX_UNPRICED_BOOK_FRACTION))}
    if not rows or all(v["p95_bps"] is None for v in per.values()):
        return {"status": UNAVAILABLE, "reason": "NO_BOOK_MEASUREMENTS", "by_size": per}
    # A walked book's cost never falls as the order grows, so a size is only
    # supported if every smaller measured size is too; one that is not within
    # budget (or not priced) caps everything above it.
    lower, upper = 0.0, None
    for size in MEASURED_NOTIONALS:
        w = per[str(int(size))]["within_budget"]
        if w is True and upper is None:
            lower = size
        elif upper is None:
            upper = size
    out = {"status": "BRACKETED" if upper is not None else "LOWER_BOUND_ONLY",
           "supported_notional_lower_usd": lower,
           "supported_notional_upper_usd": upper,
           "impact_budget_bps": impact_budget_bps, "by_size": per,
           "above_largest_measured_usd": UNAVAILABLE}
    out["planned_inside_measured_capacity"] = (
        True if planned_notional_usd <= lower
        else (False if upper is not None and planned_notional_usd >= upper else None))
    return out


# -- the report and the one verdict -----------------------------------------------

def _canon(x: Any) -> str:
    return json.dumps(x, sort_keys=True, default=str, separators=(",", ":"))


def _provenance(fwd: Sequence[Dict[str, Any]], run_inputs: Dict[str, Any]
                ) -> Dict[str, Any]:
    """What this answer was computed from, so it can be reproduced from the
    immutable inputs and nothing else: the chain hash AND a content hash of every
    row used, the protocol, and every input. Two runs with the same digest are
    the same experiment; a different digest is a different one, not a re-run."""
    chain = [r.get("hash") for r in fwd]
    content = [hashlib.sha256(_canon(r.get("obs")).encode()).hexdigest() for r in fwd]
    digest = hashlib.sha256(_canon({
        "protocol_hash": protocol_hash(), "chain_hashes": chain,
        "content_hashes": content, "run_inputs": run_inputs}).encode()).hexdigest()
    return {"protocol_hash": protocol_hash(),
            "evaluation_start_s": EVALUATION_START_S,
            "analysis_horizon_days": ANALYSIS_HORIZON_DAYS,
            "forward_rows": len(fwd),
            "first_row_hash": chain[0] if chain else None,
            "last_row_hash": chain[-1] if chain else None,
            "first_observed_at_s": fwd[0]["obs"]["observed_at_s"] if fwd else None,
            "last_observed_at_s": fwd[-1]["obs"]["observed_at_s"] if fwd else None,
            "inputs_digest": digest,
            "rows_missing_a_chain_hash": sum(1 for h in chain if not h)}


def validate_rows(rows: Sequence[Dict[str, Any]]) -> List[str]:
    """Reasons the observations cannot be trusted as a time series. Anything here
    makes the evidence unreadable: a print stamped after the observation that
    carries it is look-ahead, conflicting values for one print are two stories,
    and prints not 8 hours apart are a series this module does not understand."""
    issues: List[str] = []
    seen: Dict[int, float] = {}
    for idx, r in enumerate(rows):
        o = r["obs"]
        t_ms = o["observed_at_s"] * 1000.0
        ps = o.get("settled_prints", [])
        for bps, ms in ps:
            ms = int(ms)
            if not _finite(bps):
                issues.append(f"row {idx}: a funding print is not a finite number")
            if ms > t_ms + PRINT_CLOCK_SKEW_S * 1000:
                issues.append(f"row {idx}: a print is stamped after the observation")
            if ms in seen and _finite(bps) and _finite(seen[ms]) and seen[ms] != bps:
                issues.append(f"row {idx}: print {ms} has two different values")
            seen.setdefault(ms, bps)
        mss = sorted(int(m) for _b, m in ps)
        for x, y in zip(mss, mss[1:]):
            if abs((y - x) / 1000.0 - PRINT_SPACING_S) > PRINT_SPACING_TOLERANCE_S:
                issues.append(f"row {idx}: prints are not 8 hours apart")
                break
        if not r.get("hash"):
            issues.append(f"row {idx}: no chain hash")
        if len(issues) >= 5:
            break
    return issues


def _stage(x: Optional[float], lo: float, hi: float, *, open_lo: bool = False,
           open_hi: bool = False) -> bool:
    if not _finite(x):
        return False
    return ((x > lo) if open_lo else (x >= lo)) and ((x < hi) if open_hi else (x <= hi))


def _ratio(mean: Optional[float], sd: Optional[float]) -> Optional[float]:
    if mean is None:
        return None
    if not sd:
        return math.inf if mean > 0 else (-math.inf if mean < 0 else 0.0)
    return mean / sd


def evaluate(rows: Sequence[Dict[str, Any]], *, planned_notional_usd: float,
             owner_capital_apr: Optional[float] = None,
             owner_drawdown_limit_usd: Optional[float] = None,
             maintenance_margin_rate: Optional[float] = None,
             collateral_ratio: Optional[float] = None,
             committed_capital_fraction: Optional[float] = None,
             chain_issues: Sequence[str] = (),
             taker_fee_bps: float = TAKER_FEE_BPS) -> Dict[str, Any]:
    chain_issues = list(chain_issues)
    total_rows = len(rows)
    ts = [r["obs"].get("observed_at_s") for r in rows]
    bad_i = next((i for i, t in enumerate(ts)
                  if not isinstance(t, (int, float)) or not math.isfinite(t)), None)
    if bad_i is None:
        bad_i = next((i + 1 for i in range(len(ts) - 1) if ts[i + 1] <= ts[i]), None)
    if bad_i is not None:
        chain_issues.append(
            f"OBSERVATIONS_NOT_STRICTLY_INCREASING_IN_TIME (first offending row "
            f"index {bad_i})")
        rows = []
    horizon_end = EVALUATION_START_S + ANALYSIS_HORIZON_DAYS * 86400
    fwd = [r for r in rows
           if EVALUATION_START_S <= r["obs"].get("observed_at_s", 0) <= horizon_end]
    before = sum(1 for r in rows if r["obs"].get("observed_at_s", 0) < EVALUATION_START_S)
    after = sum(1 for r in rows if r["obs"].get("observed_at_s", 0) > horizon_end)
    if fwd:
        chain_issues += ["ROWS: " + m for m in validate_rows(fwd)]
    run_inputs = {"planned_notional_usd": planned_notional_usd,
                  "owner_capital_apr": owner_capital_apr,
                  "owner_drawdown_limit_usd": owner_drawdown_limit_usd,
                  "maintenance_margin_rate": maintenance_margin_rate,
                  "collateral_ratio": collateral_ratio,
                  "committed_capital_fraction": committed_capital_fraction,
                  "taker_fee_bps": taker_fee_bps,
                  "note": ("a verdict is a function of the evidence AND these "
                           "inputs; changing them to re-run is not a new test")}
    out: Dict[str, Any] = {
        "schema": SCHEMA, "protocol_hash": protocol_hash(), "class": SIMULATED,
        "cash": False, "mode": "overlay",
        "planned_notional_usd": planned_notional_usd,
        "observations_total": total_rows, "observations_forward": len(fwd),
        "observations_before_declaration_excluded": before,
        "observations_after_horizon_excluded": after,
        "chain_issues": chain_issues, "run_inputs": run_inputs}
    prints = {int(p[1]) for r in fwd for p in r["obs"].get("settled_prints", [])
              if int(p[1]) >= EVALUATION_START_S * 1000}
    span_days = ((fwd[-1]["obs"]["observed_at_s"] - fwd[0]["obs"]["observed_at_s"])
                 / 86400.0) if len(fwd) > 1 else 0.0
    out.update({"distinct_settled_prints": len(prints), "span_days": span_days,
                "independence_note": ("observations are hourly but a funding "
                                      "print settles every 8 hours: counts of "
                                      "observations are not counts of "
                                      "independent samples")})
    latest = {r["obs"].get("funding_print_ms") for r in fwd
              if isinstance(r["obs"].get("funding_print_ms"), (int, float))}
    out["distinct_latest_funding_prints"] = len(latest)     # what v1 also reports
    out["provenance"] = _provenance(fwd, run_inputs)
    sim = (simulate_episodes(fwd, planned_notional_usd, taker_fee_bps)
           if fwd and not chain_issues else
           {"closed": [], "open_at_end": False, "open_since_s": None,
            "open_marked": None})
    closed = sim["closed"]

    def ok(e: Dict[str, Any]) -> bool:
        return (_finite(e.get("carry_bps"))
                and _finite(e.get("carry_bps_most_favourable")))
    priced = [e for e in closed if ok(e)]
    unpriced = [e for e in closed if not ok(e)]
    out["episodes"] = {
        "closed": len(closed), "priced": len(priced),
        "unpriced_unavailable": len(unpriced),
        "unpriced_fraction": (len(unpriced) / len(closed)) if closed else 0.0,
        "gap_spanning": sum(1 for e in closed if e.get("gap_spanning")),
        "open_at_end": sim["open_at_end"], "open_since_s": sim["open_since_s"],
        "unpriced_valuation": ("GO path: the worst priced episode; NO_GO path: "
                               "the best; never dropped")}
    worst_c = min((e["carry_bps"] for e in priced), default=None)
    best_f = max((e["carry_bps_most_favourable"] for e in priced), default=None)
    worst_p = min((e["worst_path_usd"] for e in priced if _finite(e.get("worst_path_usd"))),
                  default=None)
    scale = planned_notional_usd / 1e4

    def adverse(e: Dict[str, Any]) -> float:
        return e["carry_bps"] if _finite(e.get("carry_bps")) else worst_c

    def favourable(e: Dict[str, Any]) -> float:
        return (e["carry_bps_most_favourable"]
                if _finite(e.get("carry_bps_most_favourable")) else best_f)
    have = bool(priced)
    carry = [adverse(e) for e in closed] if have else []
    fav = [favourable(e) for e in closed] if have else []
    holds = [e["hold_days"] for e in closed] if have else []
    passive = [e["passive_return_bps"] for e in closed] if have else []
    out["carry_bps_per_episode"] = mean_bounds(carry)
    out["carry_bps_most_favourable"] = mean_bounds(fav)
    # overlay total return == carry (the BTC leg cancels); incremental = carry - passive
    out["overlay_total_return_bps"] = out["carry_bps_per_episode"]
    out["passive_return_bps"] = mean_bounds(passive)
    out["incremental_vs_passive_bps"] = mean_bounds(
        [c - p for c, p in zip(carry, passive)])
    out["directional_component_usd"] = mean_bounds(
        [e["directional_component_usd"] for e in closed] if have else [])
    out["incremental_vs_passive_usd"] = mean_bounds(
        [(c - p) * scale for c, p in zip(carry, passive)])
    o_m, p_m = out["carry_bps_per_episode"], out["passive_return_bps"]
    out["risk_adjusted_vs_passive"] = {
        "overlay_mean_over_sd": _ratio(o_m["mean"], o_m["sd"]),
        "passive_mean_over_sd": _ratio(p_m["mean"], p_m["sd"]),
        "overlay_sd_bps": o_m["sd"], "passive_sd_bps": p_m["sd"],
        "note": ("per closed episode, unannualised; the overlay sheds the price "
                 "exposure on purpose, so raw return alone is not the test")}
    held_days = sum(holds)
    out["capital_utilisation"] = (held_days / span_days) if span_days > 0 else None
    out["annualised_carry_bps_of_notional"] = (
        sum(carry) / span_days * 365.0 if carry and span_days > 0 else None)
    out["annualisation_note"] = ("sum of closed-episode carry after execution "
                                 "costs and BEFORE capital cost, over the forward "
                                 "span scaled to 365 days; idle time is in the "
                                 "denominator; unpriced episodes valued "
                                 "adversely; not a forecast")
    worst = worst_p
    om = sim.get("open_marked")
    om_ok = om is not None and _finite(om.get("carry_bps"))
    if om_ok and _finite(om.get("worst_path_usd")):
        worst = om["worst_path_usd"] if worst is None else min(worst, om["worst_path_usd"])
    out["worst_episode_usd"] = worst
    out["open_episode_marked"] = (
        {"carry_bps": om["carry_bps"], "carry_usd": om["carry_usd"],
         "hold_days": om["hold_days"],
         "note": "marked to market at the last observation; not an exit"}
        if om_ok else ({"status": UNAVAILABLE} if sim["open_at_end"] else None))
    out["carry_bps_with_open_episode"] = mean_bounds(
        carry + ([om["carry_bps"]] if om_ok else []))
    out["return_distribution_bps"] = (
        {"status": UNAVAILABLE, "reason": "FEWER_THAN_20_EPISODES"} if len(closed) < 20 or not carry
        else {"status": "MEASURED", "min": min(carry), "p5": _quantile(carry, 0.05),
              "median": _quantile(carry, 0.5), "p95": _quantile(carry, 0.95),
              "max": max(carry)})
    out["capital"] = capital_model(owner_capital_apr, carry, holds,
                                   committed_capital_fraction)
    out["capital_with_open_episode"] = (
        capital_model(owner_capital_apr, carry + [om["carry_bps"]],
                      holds + [om["hold_days"]], committed_capital_fraction)
        if om_ok else None)
    gross = [adverse(e) + (e["impact_upper_bps"] if _finite(e.get("impact_upper_bps"))
                           else 0.0) for e in closed] if have else []
    budget = (sum(gross) / len(gross) / 2.0) if gross else None
    if budget is None or budget <= 0 or not fwd:
        out["capacity"] = {"status": UNAVAILABLE,
                           "reason": "NO_POSITIVE_EDGE_BEFORE_IMPACT_TO_SIZE_AGAINST",
                           "impact_budget_bps": budget}
    else:
        out["capacity"] = capacity_bounds(fwd, budget, planned_notional_usd)
    out["stress"] = stress_table(
        planned_notional_usd, leverage=1.0,
        maintenance_margin_rate=maintenance_margin_rate,
        collateral_ratio=collateral_ratio)
    out["stress"]["read_by_verdict"] = (
        "GO needs the BTC-collateral liquidation distance, from the owner's "
        "venue values, above the engine's own floor")
    out["verdict"], out["verdict_reasons"] = verdict(out, owner_drawdown_limit_usd)
    out["protocol"] = {"adequacy": PROTOCOL["adequacy"],
                       "meaning": PROTOCOL["meaning"][out["verdict"]]}
    return out


def _quantile(values: Sequence[float], q: float) -> float:
    vs = sorted(values)
    return vs[min(len(vs) - 1, max(0, math.ceil(q * len(vs)) - 1))]


def _finite(x: Any) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def verdict(rep: Dict[str, Any], drawdown_limit_usd: Optional[float] = None
            ) -> Tuple[str, List[str]]:
    """Exactly one of GO / NO_GO / INSUFFICIENT, from the report's own figures."""
    why: List[str] = []
    ad = PROTOCOL["adequacy"]
    if rep.get("chain_issues"):
        return "INSUFFICIENT", ["EVIDENCE_UNREADABLE"]
    ep = rep.get("episodes") or {}
    if ep.get("priced", 0) < ad["min_closed_episodes"]:
        why.append(f"FEWER_THAN_{ad['min_closed_episodes']}_PRICED_CLOSED_EPISODES")
    if (rep.get("span_days") or 0.0) < ad["min_span_days"]:
        why.append(f"SPAN_UNDER_{ad['min_span_days']}_DAYS")
    if (rep.get("distinct_settled_prints") or 0) < ad["min_distinct_settled_prints"]:
        why.append(f"FEWER_THAN_{ad['min_distinct_settled_prints']}_DISTINCT_PRINTS")
    if ep.get("unpriced_fraction", 0.0) > PROTOCOL["max_unpriced_fraction"]:
        why.append("TOO_MANY_EPISODES_UNPRICEABLE")
    if why:
        return "INSUFFICIENT", why
    fav = rep["carry_bps_most_favourable"]
    if fav["upper95"] is not None and fav["upper95"] <= 0:
        return "NO_GO", ["UPPER_95_BOUND_NOT_ABOVE_ZERO_EVEN_AT_BEST_CASE"]
    cap = rep["capital"]
    if cap["status"] != "OWNER_SUPPLIED":
        return "INSUFFICIENT", [cap["status"]]
    res = cap["result"]
    if res["lower95"] is None or res["lower95"] <= 0:
        return "INSUFFICIENT", ["LOWER_95_BOUND_NOT_ABOVE_ZERO_AFTER_CAPITAL_COST"]
    if rep.get("open_episode_marked") == {"status": UNAVAILABLE}:
        return "INSUFFICIENT", ["OPEN_EPISODE_CANNOT_BE_MARKED"]
    cwo = rep.get("capital_with_open_episode")
    if rep.get("open_episode_marked") and (
            not cwo or cwo.get("result") is None
            or cwo["result"].get("lower95") is None or cwo["result"]["lower95"] <= 0):
        return "INSUFFICIENT", ["LOWER_95_BOUND_NOT_ABOVE_ZERO_WITH_OPEN_EPISODE"]
    ra = rep.get("risk_adjusted_vs_passive") or {}
    o_r, p_r = ra.get("overlay_mean_over_sd"), ra.get("passive_mean_over_sd")
    if o_r is None or p_r is None or o_r < p_r:
        return "INSUFFICIENT", ["OVERLAY_TRAILS_PASSIVE_BTC_PER_UNIT_OF_RISK"]
    if rep["capacity"].get("planned_inside_measured_capacity") is not True:
        return "INSUFFICIENT", ["PLANNED_SIZE_NOT_INSIDE_MEASURED_CAPACITY"]
    if not _finite(drawdown_limit_usd) or drawdown_limit_usd <= 0:
        return "INSUFFICIENT", [INSUFFICIENT_OWNER_INPUT + ":DRAWDOWN_LIMIT"]
    liq = (rep.get("stress") or {}).get("liquidation_rally_pct_btc_collateral") or {}
    if liq.get("status") != "ILLUSTRATIVE" or not _finite(liq.get("pct")):
        return "INSUFFICIENT", [INSUFFICIENT_OWNER_INPUT + ":MARGIN_FACTS"]
    if liq["pct"] < PROTOCOL["min_liquidation_distance_pct"]:
        return "INSUFFICIENT", ["LIQUIDATION_DISTANCE_BELOW_ENGINE_FLOOR"]
    if rep["worst_episode_usd"] is None:
        return "INSUFFICIENT", ["NO_OBSERVED_EPISODE_PATH"]
    if rep["worst_episode_usd"] < -float(drawdown_limit_usd):
        return "INSUFFICIENT", ["WORST_EPISODE_EXCEEDS_DRAWDOWN_LIMIT"]
    return "GO", ["LOWER_95_BOUND_POSITIVE_AFTER_MEASURED_COSTS_AND_CAPITAL"]


def report(db: str, *, planned_notional_usd: float,
           owner_capital_apr: Optional[float] = None,
           owner_drawdown_limit_usd: Optional[float] = None,
           maintenance_margin_rate: Optional[float] = None,
           collateral_ratio: Optional[float] = None,
           committed_capital_fraction: Optional[float] = None) -> Dict[str, Any]:
    rows, issues = mp.read(db)
    return evaluate(rows, planned_notional_usd=planned_notional_usd,
                    owner_capital_apr=owner_capital_apr,
                    owner_drawdown_limit_usd=owner_drawdown_limit_usd,
                    maintenance_margin_rate=maintenance_margin_rate,
                    collateral_ratio=collateral_ratio,
                    committed_capital_fraction=committed_capital_fraction,
                    chain_issues=issues)


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("report")
    p.add_argument("--db", required=True)
    p.add_argument("--notional", type=float, required=True)
    p.add_argument("--capital-apr", type=float, default=None)
    p.add_argument("--drawdown-limit-usd", type=float, default=None)
    p.add_argument("--maintenance-margin-rate", type=float, default=None)
    p.add_argument("--collateral-ratio", type=float, default=None)
    p.add_argument("--committed-capital-fraction", type=float, default=None)
    args = ap.parse_args(argv)
    out = report(args.db, planned_notional_usd=args.notional,
                 owner_capital_apr=args.capital_apr,
                 owner_drawdown_limit_usd=args.drawdown_limit_usd,
                 maintenance_margin_rate=args.maintenance_margin_rate,
                 collateral_ratio=args.collateral_ratio,
                 committed_capital_fraction=args.committed_capital_fraction)
    print(json.dumps(out, indent=2, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
