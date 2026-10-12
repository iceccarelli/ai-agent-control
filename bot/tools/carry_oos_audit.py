#!/usr/bin/env python3
"""Chronological out-of-sample audit of the PRODUCTION carry engine.

READ THIS FIRST - what this is and is not
=========================================
`tools/carry_hold_audit.py` is IN-SAMPLE: one pass over the whole corpus with
the rule already fixed. This tool splits the same corpus into a history-only
warm-up and several contiguous forward blocks and runs the real `CarryEngine`
(through `tools/carry_replay.replay`, the same driver the hold audit uses) on
each block with nothing but the past to start from.

It is NOT independent validation. The production gate's constants were settled
with the whole corpus visible (see `tools/carry_walkforward.py`), so a block of
that corpus is not a fresh sample, and no verdict here can ever say "edge
established". The strongest outcome is `CHRONOLOGICALLY_CONSISTENT_NOT_INDEPENDENT`;
the usual honest outcome with ~80 trades is `INCONCLUSIVE`. Every number is a
MODELLED historical simulation: no venue was read, no order was placed, nothing
touches a ledger, a state database or the network.

The frozen protocol (declared in `PROTOCOL`, hashed into the report)
---------------------------------------------------------------------
* corpus sorted by validated timestamp; a duplicate or out-of-order stamp
  REFUSES the run (no silent resolution);
* WARM-UP = the first `warmup_days`: never a test interval, only the source of
  the up-to-8 prints a block's engine is seeded with;
* the remaining interval is cut into `folds` equal-duration blocks by
  TIMESTAMP, fixed before any result exists; no shuffling, no search;
* each block runs a FRESH production engine seeded by `warm_funding_history`
  with the settled prints that precede the block (what production does at
  start-up) and fed only the block's own prints;
* a position still open at a block's end is CENSORED - marked, never closed,
  never counted as an engine exit;
* a position that, in one continuous run, was already open across a block's
  start is a PRE-BOUNDARY position; a fresh engine re-entering on the block's
  first print would present it as a new trade, so that entry is PURGED and
  reported separately;
* adequacy (`min_completed_per_fold`, `min_adequate_folds`,
  `min_total_completed`) and the bootstrap (seed, resamples, level) are fixed
  constants. Below them the verdict is INCONCLUSIVE.

Frozen production values are read from the engine and gate, never set: the
baseline is always `ASSUMED_HOLD_DAYS` as shipped. Alternative amortisation
horizons are DIAGNOSTIC COUNTERFACTUALS declared in advance, reported without
ranking, and never feed the verdict.

    python3 tools/carry_oos_audit.py --repo .
    python3 tools/carry_oos_audit.py --repo . --json /tmp/oos.json
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import math
import os
import random
import statistics
import subprocess
import sys
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

DAY_MS = 86_400_000
SCHEMA = "carry_oos_audit/1"

#: Everything that shapes the evaluation, fixed BEFORE any result is read.
PROTOCOL: Dict[str, Any] = {
    "warmup_days": 90,
    "folds": 4,
    "split": "equal duration by timestamp after the warm-up; expanding past",
    "min_completed_per_fold": 20,
    "min_adequate_folds": 3,
    "min_total_completed": 60,
    # Sequential trades are serially dependent (funding regimes persist), so the
    # interval resamples CONSECUTIVE blocks of trades; an i.i.d. resample would
    # understate the width. The i.i.d. interval is reported for comparison only.
    "bootstrap": {"resamples": 2000, "seed": 20261012, "level": 0.95,
                  "block_length": 5},
    "notional_usd": 100_000.0,
    "execution_mode": "overlay",
    "execution_style": "taker",
    "borrow_apr": 0.0,
    # Diagnostic only; declared here so they cannot be chosen after the fact.
    "counterfactual_hold_days": [7.0, 14.0],
    "annualisation": "completed-trade net / notional / years of the interval",
    "purge_rule": ("entry on a block's first print is purged when a "
                   "continuous run held a position across that block's start"),
    "censor_rule": "open at block end -> censored, marked, never an exit",
}


def protocol_id() -> str:
    """Hash of the protocol AS CONSTRUCTED; it travels with every report."""
    return hashlib.sha256(
        json.dumps(PROTOCOL, sort_keys=True).encode()).hexdigest()[:16]


PROTOCOL_ID = protocol_id()

VERDICTS = ("INCONCLUSIVE", "CHRONOLOGICALLY_CONSISTENT_NOT_INDEPENDENT",
            "NEGATIVE_OUT_OF_SAMPLE")


class CorpusError(ValueError):
    """The corpus cannot be evaluated; nothing is written."""


# -- corpus ----------------------------------------------------------------

def validate_corpus(rows: Sequence[Any]) -> None:
    """Strictly increasing, finite, positive. Duplicates REFUSE (declared rule)."""
    if len(rows) < 2:
        raise CorpusError("fewer than two settlements")
    prev = None
    for n, r in enumerate(rows):
        vals = (r.ms, r.perp, r.spot, r.rate)
        if not all(math.isfinite(float(v)) for v in vals):
            raise CorpusError(f"row {n} has a non-finite value")
        if r.perp <= 0 or r.spot <= 0:
            raise CorpusError(f"row {n} has a non-positive price")
        if prev is not None and r.ms <= prev:
            raise CorpusError(
                f"row {n} stamp {r.ms} is not after {prev}: duplicate or "
                "out-of-order settlements are refused, not resolved")
        prev = r.ms


def fold_boundaries(first_ms: int, last_ms: int,
                    protocol: Dict[str, Any] = PROTOCOL
                    ) -> Tuple[int, List[Tuple[int, int]]]:
    """``(warmup_end, [(start, end), ...])`` - half-open ``[start, end)`` blocks.

    A pure function of the first and last TIMESTAMP and the protocol: no rate,
    price or outcome enters, so a boundary cannot be moved by a result.
    """
    warm_end = int(first_ms) + int(protocol["warmup_days"]) * DAY_MS
    end = int(last_ms) + 1
    span = end - warm_end
    k = int(protocol["folds"])
    if span <= 0:
        return warm_end, []
    cuts = [warm_end + (span * i) // k for i in range(k)] + [end]
    return warm_end, list(zip(cuts[:-1], cuts[1:]))


# -- evaluation ------------------------------------------------------------

@contextlib.contextmanager
def hold_horizon(days: Optional[float]) -> Iterator[None]:
    """Run the gate with a different amortisation horizon, then restore it.

    A diagnostic counterfactual only. The production constant is never
    assigned to; the call is wrapped for the duration of the context.
    """
    import carry_costs
    if days is None:
        yield
        return
    original = carry_costs.evaluate_entry

    def wrapped(**kw: Any):
        kw["hold_days"] = float(days)
        return original(**kw)

    with mock.patch.object(carry_costs, "evaluate_entry", wrapped):
        yield


def _stats(values: Sequence[float]) -> Dict[str, Any]:
    if not values:
        return {"n": 0, "mean": None, "median": None, "stdev": None,
                "sem": None, "losing_share": None,
                "reason": "no completed trades"}
    n = len(values)
    sd = statistics.stdev(values) if n > 1 else None
    return {"n": n, "mean": statistics.fmean(values),
            "median": statistics.median(values), "stdev": sd,
            "sem": (sd / math.sqrt(n)) if sd is not None else None,
            "losing_share": sum(1 for v in values if v < 0) / n}


def bootstrap_mean_ci(values: Sequence[float], *, resamples: int, seed: int,
                      level: float, block_length: int = 1
                      ) -> Optional[Tuple[float, float]]:
    """Percentile bootstrap of the mean, deterministic for a given seed.

    ``block_length`` > 1 resamples runs of consecutive values (moving blocks),
    which respects serial dependence; 1 is the i.i.d. bootstrap.
    """
    if len(values) < 2:
        return None
    rng = random.Random(seed)
    n = len(values)
    bl = max(1, min(int(block_length), n))
    starts = n - bl + 1
    means = []
    for _ in range(resamples):
        out: List[float] = []
        while len(out) < n:
            i = rng.randrange(starts)
            out.extend(values[i:i + bl])
        means.append(statistics.fmean(out[:n]))
    means.sort()
    lo = int(((1 - level) / 2) * resamples)
    hi = int((1 - (1 - level) / 2) * resamples) - 1
    return means[lo], means[hi]


def _drawdown(nets_in_close_order: Sequence[float]) -> float:
    peak = cum = worst = 0.0
    for v in nets_in_close_order:
        cum += v
        peak = max(peak, cum)
        worst = max(worst, peak - cum)
    return worst


def _crossing(cont_trades: Sequence[Dict[str, Any]], boundary: int) -> bool:
    return any(t["opened_ms"] < boundary <= t["closed_ms"]
               for t in cont_trades)


def evaluate_fold(rows: Sequence[Any], a: int, b: int, *, impact_bps: float,
                  cont_trades: Sequence[Dict[str, Any]],
                  trace: Optional[List[Dict[str, Any]]] = None,
                  hold_days: Optional[float] = None) -> Dict[str, Any]:
    """One block ``[a, b)`` through a fresh production engine."""
    import carry_costs as cc
    import carry_hold_audit as ha
    import carry_replay as cr
    from carry_engine import CarryEngine

    p = PROTOCOL
    test = [r for r in rows if a <= r.ms < b]
    warm = [r for r in rows if r.ms < a][-CarryEngine.FUNDING_HISTORY:]
    out: Dict[str, Any] = {
        "start_ms": a, "end_ms": b, "test_prints": len(test),
        "test_start_first_print_ms": test[0].ms if test else None,
        "warmup_prints_used": len(warm),
        "warmup_prints_latest_ms": warm[-1].ms if warm else None}
    if len(test) < 2:
        out.update({"status": "NO_TEST_DATA", "completed": 0, "censored": 0,
                    "purged": 0, "entries": 0, "stats": _stats([]),
                    "reason": "fewer than two prints in the block"})
        return out
    with hold_horizon(hold_days):
        rep = cr.replay(
            test, notional=p["notional_usd"], borrow_apr=p["borrow_apr"],
            execution_mode=p["execution_mode"],
            execution_style=p["execution_style"], impact_bps=impact_bps,
            warm_prints=warm, trace=trace)
    perp_bps, spot_bps = cc.TAKER_BPS_PERP, cc.TAKER_BPS_SPOT
    idx = {int(r.ms): i for i, r in enumerate(rows)}

    def prints_at(i: int) -> List[float]:
        h = CarryEngine.FUNDING_HISTORY
        return [x.rate * 1e4 for x in rows[max(0, i - h + 1): i + 1]]

    spanning = _crossing(cont_trades, a)
    first_ms = test[0].ms
    completed, censored, purged = [], [], []
    for t in rep["trade_rows"]:
        e = ha._trade_net(t, taker_perp_bps=perp_bps, taker_spot_bps=spot_bps,
                          mode=p["execution_mode"])
        item = (t, e)
        # PURGE FIRST: a re-entry on the block's first print is the continuation
        # of a position that pre-dates the block, whether or not it is also
        # still open at the end. It is neither a test trade nor test exposure.
        if spanning and int(t["opened_ms"]) == first_ms:
            purged.append(item)
        elif t.get("open_at_end"):
            censored.append(item)
        else:
            completed.append(item)
    nets = [e["net"] for _t, e in completed]
    days = [(t["closed_ms"] - t["opened_ms"]) / DAY_MS for t, _e in completed]
    predicted = earned = 0.0
    for t, _e in completed:
        i0 = idx[int(t["opened_ms"])]
        sm = cc.ewma_funding_bps(prints_at(i0)) or 0.0
        d = (t["closed_ms"] - t["opened_ms"]) / DAY_MS
        predicted += (sm * cc.FUNDING_PERIODS_PER_DAY / 1e4) * (
            t["qty"] * t["entry_perp"]) * d
        earned += float(t["funding"])
    years = max((b - a) / DAY_MS, 1.0) / 365.25
    ordered = sorted(completed, key=lambda it: it[0]["closed_ms"])
    exits: Dict[str, int] = {}
    for t, _e in completed:
        exits[t["reason"]] = exits.get(t["reason"], 0) + 1
    gross = {k: sum(e[k] for _t, e in completed)
             for k in ("funding", "basis", "fees", "net")}
    out.update({
        "status": "EVALUATED",
        "entries": len(rep["trade_rows"]),
        "completed": len(completed), "censored": len(censored),
        "purged": len(purged),
        "pre_boundary_position_in_continuous_run": spanning,
        "completed_first_open_ms": (min(t["opened_ms"] for t, _e in completed)
                                    if completed else None),
        "completed_last_close_ms": (max(t["closed_ms"] for t, _e in completed)
                                    if completed else None),
        "refusals": dict(rep["reasons"]),
        "engine_exit_reasons": exits,
        "hold_days": ha.hold_distribution(days) if days else None,
        "gate_predicted_funding_usd": predicted if completed else None,
        "funding_earned_usd": earned if completed else None,
        "funding_earned_vs_predicted": (earned / predicted
                                        if completed and predicted else None),
        "gross_modelled": gross if completed else None,
        "stats": _stats(nets),
        "_nets": nets,
        "net_annualised_pct": (100.0 * gross["net"] / p["notional_usd"] / years
                               if completed else None),
        "realised_trade_drawdown_usd": (_drawdown(
            [e["net"] for _t, e in ordered]) if completed else None),
        "market_exposure_pct_incl_censored": rep["market_exposure_pct"],
        "censored_exposure": [{
            "opened_ms": t["opened_ms"], "prints_held": t["prints_held"],
            "funding_collected_usd_modelled": e["funding"],
            "basis_marked_usd_estimate": e["basis"],
            "note": "marked at the block end; not an engine exit, not realised"}
            for t, e in censored],
        "purged_boundary_reentries": [{
            "opened_ms": t["opened_ms"], "closed_ms": t["closed_ms"],
            "net_usd_modelled": e["net"]} for t, e in purged],
        "costs": {
            "fees": "modelled taker fees from carry_costs rates",
            "slippage_bps_per_leg": impact_bps,
            "borrow": {"value_usd": None,
                       "reason": "declared 0.0% APR input; not a measured "
                                 "cost, so not reported as a zero cost"},
            "not_modelled": rep["not_modelled"]},
    })
    return out


def run_oos(rows: Sequence[Any], *, meta: Optional[Dict[str, Any]] = None,
            impact_bps: float = 0.0, impact_source: str = "declared",
            counterfactuals: bool = True) -> Dict[str, Any]:
    """The whole audit over validated ``rows``. Pure: no clock, no I/O."""
    import carry_costs as cc
    import carry_replay as cr
    from carry_engine import CarryEngine

    validate_corpus(rows)
    p = PROTOCOL
    first_ms, last_ms = int(rows[0].ms), int(rows[-1].ms)
    warm_end, blocks = fold_boundaries(first_ms, last_ms)
    frozen = {
        "ASSUMED_HOLD_DAYS": cc.ASSUMED_HOLD_DAYS,
        "FUNDING_HISTORY": CarryEngine.FUNDING_HISTORY,
        "EWMA_MIN_PRINTS": cc.EWMA_MIN_PRINTS,
        "TAKER_BPS_PERP": cc.TAKER_BPS_PERP,
        "TAKER_BPS_SPOT": cc.TAKER_BPS_SPOT,
    }

    def variant(hold_days: Optional[float]) -> Dict[str, Any]:
        with hold_horizon(hold_days):
            cont = cr.replay(
                rows, notional=p["notional_usd"], borrow_apr=p["borrow_apr"],
                execution_mode=p["execution_mode"],
                execution_style=p["execution_style"], impact_bps=impact_bps)
        cont_trades = cont["trade_rows"]
        folds = []
        for n, (a, b) in enumerate(blocks):
            f = evaluate_fold(rows, a, b, impact_bps=impact_bps,
                              cont_trades=cont_trades, hold_days=hold_days)
            f["fold"] = n + 1
            folds.append(f)
        return {"folds": folds, "continuous": cont}

    base = variant(None)
    folds = base["folds"]
    pooled_nets: List[float] = []
    totals = {"funding": 0.0, "basis": 0.0, "fees": 0.0, "net": 0.0}
    adequate = 0
    for f in folds:
        if f.get("status") != "EVALUATED":
            continue
        f["adequate"] = f["completed"] >= p["min_completed_per_fold"]
        adequate += bool(f["adequate"])
        pooled_nets.extend(f["_nets"])
        if f["gross_modelled"]:
            for k in totals:
                totals[k] += f["gross_modelled"][k]
    test_span_years = max((last_ms + 1 - warm_end) / DAY_MS, 1.0) / 365.25 \
        if blocks else None
    # How the blocks relate to ONE continuous run of the same engine. The two
    # differ by design: a position crossing a block end is censored here, and
    # the fresh engine of the next block may re-enter (purged). Showing both
    # keeps the censoring from silently deleting where the profit sat.
    import carry_costs as _cc
    import carry_hold_audit as _ha
    cont_trades = base["continuous"]["trade_rows"]

    def _net(t: Dict[str, Any]) -> float:
        return _ha._trade_net(t, taker_perp_bps=_cc.TAKER_BPS_PERP,
                              taker_spot_bps=_cc.TAKER_BPS_SPOT,
                              mode=p["execution_mode"])["net"]
    link = []
    for f in folds:
        if f.get("status") != "EVALUATED":
            continue
        inside = [t for t in cont_trades
                  if f["start_ms"] <= t["opened_ms"] < f["end_ms"]]
        crossers = [t for t in inside if t["closed_ms"] >= f["end_ms"]]
        link.append({
            "fold": f["fold"],
            "continuous_run_entries_in_block": len(inside),
            "fold_run_entries_kept": f["completed"] + f["censored"],
            "fold_run_entries_purged": f["purged"],
            "continuous_run_entries_crossing_block_end": len(crossers),
            "continuous_run_net_of_those_crossers_usd_modelled": (
                sum(_net(t) for t in crossers) if crossers else None),
            "continuous_run_net_of_block_entries_usd_modelled": (
                sum(_net(t) for t in inside) if inside else None)})
    warm_trades = [t for t in cont_trades if t["opened_ms"] < warm_end]
    censored_total = {
        "positions": sum(f["censored"] for f in folds
                         if f.get("status") == "EVALUATED"),
        "funding_collected_usd_modelled": sum(
            c["funding_collected_usd_modelled"] for f in folds
            if f.get("status") == "EVALUATED" for c in f["censored_exposure"]),
        "basis_marked_usd_estimate": sum(
            c["basis_marked_usd_estimate"] for f in folds
            if f.get("status") == "EVALUATED" for c in f["censored_exposure"]),
        "purged_reentries": sum(f["purged"] for f in folds
                                if f.get("status") == "EVALUATED"),
        "purged_reentries_net_usd_modelled": sum(
            x["net_usd_modelled"] for f in folds
            if f.get("status") == "EVALUATED"
            for x in f["purged_boundary_reentries"]),
        "note": "excluded from every completed-trade statistic; long holds "
                "that cross a block end are exactly where an in-sample run "
                "concentrates its profit, so completed-only figures are "
                "biased DOWNWARD relative to the continuous run"}

    bs = p["bootstrap"]
    ci = bootstrap_mean_ci(pooled_nets, resamples=bs["resamples"],
                           seed=bs["seed"], level=bs["level"],
                           block_length=bs["block_length"])
    ci_iid = bootstrap_mean_ci(pooled_nets, resamples=bs["resamples"],
                               seed=bs["seed"], level=bs["level"])
    completed_total = len(pooled_nets)
    fold_positive = [f for f in folds if f.get("status") == "EVALUATED"
                     and f.get("adequate") and f["gross_modelled"]
                     and f["gross_modelled"]["net"] > 0]

    verdict, why = _verdict(blocks, folds, adequate, completed_total,
                            pooled_nets, ci, fold_positive)

    diagnostics = []
    if counterfactuals and blocks:
        for hd in p["counterfactual_hold_days"]:
            v = variant(hd)
            nets = [x for f in v["folds"] if f.get("status") == "EVALUATED"
                    for x in f.pop("_nets")]
            diagnostics.append({
                "label": "DIAGNOSTIC_COUNTERFACTUAL",
                "assumed_hold_days": hd,
                "production_value_unchanged": cc.ASSUMED_HOLD_DAYS,
                "completed": len(nets),
                "net_usd": sum(nets) if nets else None,
                "stats": _stats(nets),
                "per_fold": [{"fold": f["fold"], "completed": f["completed"],
                              "censored": f["censored"],
                              "net_usd": (f["gross_modelled"]["net"]
                                          if f.get("gross_modelled") else None)}
                             for f in v["folds"]
                             if f.get("status") == "EVALUATED"],
                "note": "not used by the verdict; not a recommendation; the "
                        "same blocks are NOT independent evidence for changing "
                        "the production constant"})

    for f in folds:
        f.pop("_nets", None)
    return {
        "schema": SCHEMA,
        "evidence_class": "MODELLED_HISTORICAL_SIMULATION",
        "venue_observed_pnl": None,
        "venue_observed_pnl_reason": (
            "this tool reads no venue and no ledger; modelled results are "
            "never venue-observed or attributable income"),
        "protocol": PROTOCOL, "protocol_id": protocol_id(),
        "corpus": dict(meta or {}, records=len(rows), start_ms=first_ms,
                       end_ms=last_ms),
        "frozen_production": frozen,
        "slippage": {"impact_bps_per_leg": impact_bps, "source": impact_source},
        "warmup": {"start_ms": first_ms, "end_ms": warm_end,
                   "role": "history only; never a test interval"},
        "folds": folds,
        "combined": {
            "method": ("pool completed (non-censored, non-purged) trades of "
                       "all blocks; blocks are disjoint in time; annualised "
                       "over the whole test interval on fixed notional"),
            "completed": completed_total,
            "censored": sum(f["censored"] for f in folds
                            if f.get("status") == "EVALUATED"),
            "purged": sum(f["purged"] for f in folds
                          if f.get("status") == "EVALUATED"),
            "adequate_folds": adequate,
            "gross_modelled": totals if completed_total else None,
            "stats": _stats(pooled_nets),
            "mean_net_bootstrap_ci": ({
                "method": "moving-block bootstrap (used by the verdict)",
                "block_length": bs["block_length"],
                "level": bs["level"], "low": ci[0],
                "high": ci[1]} if ci else None),
            "mean_net_bootstrap_ci_iid_comparison_only": ({
                "method": "i.i.d. bootstrap; ignores serial dependence, "
                          "NOT used by the verdict",
                "level": bs["level"], "low": ci_iid[0],
                "high": ci_iid[1]} if ci_iid else None),
            "net_annualised_pct": (
                100.0 * totals["net"] / p["notional_usd"] / test_span_years
                if completed_total and test_span_years else None),
        },
        "continuous_run_link": {
            "blocks": link,
            "warmup_entries_excluded": len(warm_trades),
            "warmup_entries_net_usd_modelled": (
                sum(_net(t) for t in warm_trades) if warm_trades else None)},
        "censored_exposure_total": censored_total,
        "counterfactuals": diagnostics,
        "verdict": verdict, "verdict_reasons": why,
        "independence": ("NOT_INDEPENDENT: the production constants were "
                         "settled with this whole corpus visible, so no "
                         "block is a fresh sample"),
        "limitations": [
            "chronological partition of a fixed rule, not independent data",
            "about 80 modelled trades over the corpus: per-block samples are "
            "small and the per-trade distribution is strongly skewed",
            "candle replay: no latency, queue position, partial fills or "
            "real slippage; slippage is a declared walk through measured depth",
            "no venue-observed settlement or fill supports any figure here",
            "mark-to-market of censored exposure is an estimate",
            "censoring removes long boundary-crossing holds from the "
            "completed-trade statistics; compare continuous_run_link",
            "trades are serially dependent; the block bootstrap reduces but "
            "cannot remove that, and ~80 trades cannot support inference "
            "about a skewed, regime-driven distribution",
        ],
    }


def _verdict(blocks: Sequence[Any], folds: Sequence[Dict[str, Any]],
             adequate: int, completed: int, nets: Sequence[float],
             ci: Optional[Tuple[float, float]],
             fold_positive: Sequence[Dict[str, Any]]) -> Tuple[str, List[str]]:
    p = PROTOCOL
    if not blocks:
        return "INCONCLUSIVE", ["NO_TEST_INTERVAL_AFTER_WARMUP"]
    why: List[str] = []
    if completed < p["min_total_completed"]:
        why.append(f"TOTAL_COMPLETED_{completed}_BELOW_{p['min_total_completed']}")
    if adequate < p["min_adequate_folds"]:
        why.append(f"ADEQUATE_FOLDS_{adequate}_BELOW_{p['min_adequate_folds']}")
    if why:
        return "INCONCLUSIVE", why + ["INSUFFICIENT_SAMPLE"]
    if ci is None:
        return "INCONCLUSIVE", ["NO_UNCERTAINTY_ESTIMATE"]
    if ci[1] < 0:
        return "NEGATIVE_OUT_OF_SAMPLE", ["BOOTSTRAP_CI_UPPER_BOUND_BELOW_ZERO"]
    if ci[0] > 0 and len(fold_positive) == adequate:
        return ("CHRONOLOGICALLY_CONSISTENT_NOT_INDEPENDENT",
                ["EVERY_ADEQUATE_FOLD_POSITIVE", "CI_LOWER_BOUND_ABOVE_ZERO",
                 "PARAMETERS_NOT_INDEPENDENT_OF_CORPUS"])
    return "INCONCLUSIVE", ["CI_INCLUDES_ZERO_OR_FOLDS_DISAGREE"]


# -- provenance and output -------------------------------------------------

def corpus_meta(repo: str) -> Dict[str, Any]:
    import carry_backtest as cb
    files = {}
    h = hashlib.sha256()
    for rel in (cb.BYBIT_SPOT_4H, cb.BYBIT_PERP_4H, cb.BYBIT_FUNDING):
        path = os.path.join(repo, rel)
        with open(path, "rb") as fh:
            digest = hashlib.sha256(fh.read()).hexdigest()
        files[rel] = digest
        h.update(rel.encode() + digest.encode())
    return {"files_sha256": files, "corpus_sha256": h.hexdigest(),
            "source_dir": cb.BYBIT_DIR}


def code_revision(repo: str) -> Dict[str, Any]:
    def git(*a: str) -> Optional[str]:
        try:
            return subprocess.run(["git", "-C", repo, *a], capture_output=True,
                                  text=True, timeout=20, check=True
                                  ).stdout.strip()
        except Exception:                                   # noqa: BLE001
            return None
    rev = git("rev-parse", "HEAD")
    status = git("status", "--porcelain", "--untracked-files=no")
    return {"git_rev": rev or "unavailable",
            "tracked_changes": (bool(status) if status is not None else None)}


def write_atomic(path: str, report: Dict[str, Any]) -> None:
    """Serialise fully first; a failure leaves no file, never a partial one."""
    text = json.dumps(report, indent=2, sort_keys=True, allow_nan=False)
    tmp = f"{path}.tmp.{os.getpid()}"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def build_report(repo: str = ROOT) -> Dict[str, Any]:
    import carry_backtest as cb
    rows, meta = cb.load_bybit_settlements(repo)
    impact, detail = cb.impact_bps_per_leg(repo, PROTOCOL["notional_usd"])
    m = dict(corpus_meta(repo), label=meta.get("label"),
             same_venue=meta.get("same_venue"))
    report = run_oos(rows, meta=m, impact_bps=impact,
                     impact_source=detail.get("depth_source", "declared"))
    report["code"] = code_revision(repo)
    return report


def _print(r: Dict[str, Any]) -> None:
    c = r["combined"]
    print("=" * 74)
    print("CARRY OOS AUDIT — chronological blocks, production engine, MODELLED")
    print("=" * 74)
    print(f"  protocol {r['protocol_id']}   corpus "
          f"{r['corpus'].get('corpus_sha256', 'n/a')[:16]}   "
          f"{r['corpus']['records']} prints")
    for f in r["folds"]:
        if f.get("status") != "EVALUATED":
            print(f"  fold {f['fold']}  {f.get('status')}")
            continue
        net = f["gross_modelled"]["net"] if f["gross_modelled"] else None
        print(f"  fold {f['fold']}  completed {f['completed']:>3}  censored "
              f"{f['censored']}  purged {f['purged']}  "
              f"adequate {f['adequate']}  net "
              f"{'n/a' if net is None else f'${net:,.0f}'}")
    print(f"  combined: {c['completed']} completed, adequate folds "
          f"{c['adequate_folds']}/{PROTOCOL['folds']}")
    print(f"  VERDICT {r['verdict']}: {', '.join(r['verdict_reasons'])}")
    print(f"  {r['independence']}")
    print("  MODELLED only: no venue, ledger or order was touched.")


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", default=ROOT)
    ap.add_argument("--json", default="", help="write the report atomically")
    args = ap.parse_args(argv)
    import logging
    logging.getLogger("carry_engine").setLevel(logging.ERROR)  # warm-up chatter
    try:
        report = build_report(args.repo)
        if args.json:
            write_atomic(args.json, report)
    except (CorpusError, SystemExit) as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2
    _print(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
