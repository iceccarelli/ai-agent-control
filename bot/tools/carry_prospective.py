#!/usr/bin/env python3
"""The FROZEN prospective evaluation of BTCUSDT funding carry. Offline: it reads one local file of settled funding prints, makes no request, places no order and
changes no engine status.

    python3 tools/carry_prospective.py --funding PRINTS.jsonl [--hurdle-pct 10] [--now-ms MS] [--json]

This CLI reports PROGRESS and can never evaluate. The ONE supported way to evaluate the closed window is ``tools/trial_journal.py evaluate``: it keeps every
look in a locked, durable, hash-chained journal checked against an external anchor. ``evaluate()`` below is the PURE calculation (it takes whatever ledger
it is handed, so it is for research tests, not for the official look).

Why. Everything the historical teardown says is in-sample, over heavily overlapping starts, with a regime that has since changed. The only evidence that can
change the verdict is data that did not exist when the plan below was written. This module fixes, BEFORE any such data exist:

  * the parameters, the costs, the holds, the hypotheses and the evaluation window (``plan_for``; ``PINNED_DIGESTS`` pins each plan's SHA-256, so a changed
    window, cost or hurdle is a DIFFERENT plan and ``evaluate`` refuses it);
  * the test: one-sided, H0 "mean funding per settlement <= the funding the declared hurdle requires", ESS-adjusted for serial dependence, Holm-adjusted over
    the three declared hurdles, every look recorded in an append-only hash-chained ledger (``statistical_power``);
  * future-only data: a print dated before the window opens is refused (``WindowViolation``), so the historical corpus cannot be fed in;
  * missing observations: a missing settlement is NOT interpolated and NOT dropped silently; coverage is reported, and an evaluation below the declared
    coverage is refused rather than computed on what is left.

What it does not do. It does not model basis P&L (measured at about -0.2 bps per settlement-equivalent with a large spread in the public history: it must be
re-measured from the account or the recorder and is reported separately), the owner's fee tier (11.4 bps is the teardown's illustrative figure, not a verified
account fee) or execution (next-executable-price assumptions are declared in ``PARAMETERS`` and are tested by ``execution_model``, not here). A pass here is
EVIDENCE OF A FUNDING LEVEL that would clear the hurdle. It authorises nothing: capital, leverage and any live order remain separate owner decisions.
"""
from __future__ import annotations

import argparse
import datetime
import json
import math
import os
import sys
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
BOT = os.path.dirname(HERE)
for _p in (BOT, HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import account_review as AR   # noqa: E402  (the guarded local-file reader)
import carry_teardown as CT   # noqa: E402  (the one definition of the required funding)
import statistical_power as SP  # noqa: E402

SETTLE_MS = CT.SETTLE_MS
PER_DAY = CT.PER_DAY
WINDOW_START_MS = int(datetime.datetime(2026, 11, 7, tzinfo=datetime.timezone.utc).timestamp() * 1000)      # the first UTC day after this plan was written
WINDOW_DAYS = 365
WINDOW_END_MS = WINDOW_START_MS + WINDOW_DAYS * 86_400_000
HURDLES_PCT = tuple(CT.HURDLES_PCT)                                  # 10, 15, 20: PROVISIONAL scenarios, not owner-approved
HOLD_DAYS = 30.0
ROUND_TRIP_BPS = CT.DEFAULT_ROUND_TRIP_BPS                           # ASSUMED, unverified against any account
RESERVE = 0.10
MIN_EDGE_BPS = 0.25                                                  # excess over the required funding worth detecting (bps per settlement)
MIN_COVERAGE = 0.92                                                  # above the share (91.05 %) the planning inputs themselves need to reach the required n
MIN_OBSERVATIONS_FOR_OBSERVED_DEPENDENCE = 90
#: planning inputs used only until the window holds enough data of its own: the last 365 days of the public history (ESTIMATED, not a promise)
PLANNING_SD_BPS = 0.45
PLANNING_ESS_RATIO = 0.031

PARAMETERS: Dict[str, Any] = {
    "instrument": "BTCUSDT linear perpetual, short, against owned BTC (overlay: no spot trade)",
    "statistic_scope": "the unconditional mean settled print against the funding the hurdle requires: the engine's entry gate and exit rule are NOT under test here, "
                       "and funding_carry_fade_v1 is not used",
    "observation": "settled funding print, bps per 8h settlement, minus the funding the declared hurdle requires",
    "hold_days": HOLD_DAYS, "round_trip_bps": ROUND_TRIP_BPS, "round_trip_bps_status": "ASSUMED_UNVERIFIED_OWNER_FEE_TIER",
    "reserve_fraction": RESERVE, "hurdles_pct": list(HURDLES_PCT), "hurdles_status": "PROVISIONAL_NOT_OWNER_APPROVED",
    "entry_price_rule": "NEXT EXECUTABLE: the perp/spot reference at the bar after the signal, never the signal bar's own close",
    "missing_observations": "not interpolated, not dropped silently; coverage below min_coverage refuses the evaluation",
    "min_coverage": MIN_COVERAGE, "min_edge_bps_over_required": MIN_EDGE_BPS, "basis_pnl": "excluded from this test; measured separately",
    "retuning": "none: a changed value is a different plan with a different digest",
}


def required_bps(hurdle_pct: float) -> float:
    return CT.required_funding(hurdle_pct, HOLD_DAYS, ROUND_TRIP_BPS, RESERVE)


def plan_for(hurdle_pct: float) -> "SP.EvaluationPlan":
    if hurdle_pct not in HURDLES_PCT:
        raise SP.PlanError(f"hurdle {hurdle_pct!r} is not one of the declared {HURDLES_PCT}: a new hurdle is a new plan")
    return SP.EvaluationPlan(
        strategy_id="btcusdt_funding_carry_overlay", version="prospective-1",
        parameters=dict(PARAMETERS, hurdle_pct=float(hurdle_pct), required_funding_bps=required_bps(hurdle_pct)),
        evaluation_window=(WINDOW_START_MS, WINDOW_END_MS), metric="mean_excess_funding_bps_per_settlement",
        alpha=0.05, power_target=0.8, min_edge=MIN_EDGE_BPS, n_hypotheses_declared=len(HURDLES_PCT), seed=20261106, adjust="holm")


#: SHA-256 of each plan as registered on 2026-11-06. Computed once from ``plan_for`` and pinned here.
PINNED_DIGESTS: Dict[float, str] = {
    10.0: "de6afe80fba7ec37dbf622647ba505bcf1719b30c147351feb2f436fd28bada9",
    15.0: "1d58583367e1445c36b8aaed4d0acdcd86bffdcc2be8431e791f61a67704e95d",
    20.0: "55be467befe95d98812e31b4bc157923bf527706415a09c5c0447471d6d52249",
}


def registered_plan(hurdle_pct: float) -> "SP.EvaluationPlan":
    """The plan, refused unless its digest is the pinned one."""
    plan = plan_for(hurdle_pct)
    if SP.plan_digest(plan) != PINNED_DIGESTS.get(hurdle_pct):
        raise SP.PlanDigestMismatch("the plan in this file is not the one that was registered: a parameter, cost, hurdle or the window was changed")
    return plan


def _clean(prints: Sequence[Tuple[float, float]]) -> List[Tuple[int, float]]:
    out = []
    for i, (ts, bps) in enumerate(prints):
        if isinstance(ts, bool) or isinstance(bps, bool) or not isinstance(ts, (int, float)) or not isinstance(bps, (int, float)) \
                or not math.isfinite(ts) or not math.isfinite(bps) or ts != int(ts):
            raise SP.DataIntegrityError(f"print {i + 1}: needs an integer ms timestamp and a finite bps value")
        if int(ts) % SETTLE_MS != 0:
            raise SP.DataIntegrityError(f"print {i + 1}: {int(ts)} is not on the 8h UTC settlement grid; a settlement is never snapped")
        out.append((int(ts), float(bps)))
    stamps = [t for t, _ in out]
    if len(set(stamps)) != len(stamps):
        raise SP.DataIntegrityError("two prints share one settlement time: which is the print is not decidable, and counting both would double a settlement")
    return out


def observations(prints: Sequence[Tuple[float, float]], hurdle_pct: float) -> List[Tuple[int, float]]:
    req = required_bps(hurdle_pct)
    return [(ts, bps - req) for ts, bps in _clean(prints)]


def coverage(prints: Sequence[Tuple[float, float]], now_ms: int) -> Dict[str, Any]:
    """Settlements that should exist in [window start, min(now, window end)) against those supplied. Nothing is filled in."""
    horizon = min(int(now_ms), WINDOW_END_MS)
    inwin = [ts for ts, _ in _clean(prints) if WINDOW_START_MS <= ts < WINDOW_END_MS]
    expected = max(0, (horizon - WINDOW_START_MS + SETTLE_MS - 1) // SETTLE_MS)        # the settlements whose time has passed
    have = {ts for ts in inwin if ts < horizon}                                           # a print dated in the future is not an observation yet
    missing = [WINDOW_START_MS + i * SETTLE_MS for i in range(expected) if WINDOW_START_MS + i * SETTLE_MS not in have]
    return {"expected": expected, "observed": len(have), "missing": len(missing), "coverage": (len(have) / expected) if expected else None,
            "future_dated_ignored": len(inwin) - len(have), "first_missing": [AR._iso(t) for t in missing[:5]]}


def progress(prints: Sequence[Tuple[float, float]], now_ms: int, hurdle_pct: float = 10.0) -> Dict[str, Any]:
    """Where the registered evaluation stands, WITHOUT evaluating (no trial is recorded, no p-value is computed): how many settlements exist, how many
    the plan needs, and how long that takes. Before the window opens the answer is that nothing of the evaluation exists yet."""
    plan = registered_plan(hurdle_pct)
    cov = coverage(prints, now_ms)
    due = min(int(now_ms), WINDOW_END_MS)
    inwin = sorted((ts, v) for ts, v in observations(prints, hurdle_pct) if WINDOW_START_MS <= ts < min(due, WINDOW_END_MS))
    vals = [v for _, v in inwin]                 # in TIME order: the dependence estimate is meaningless on a shuffled series
    observed_dep = len(vals) >= MIN_OBSERVATIONS_FOR_OBSERVED_DEPENDENCE
    import evidence_stats as ES
    if observed_dep:
        m = sum(vals) / len(vals)
        sd = math.sqrt(sum((x - m) ** 2 for x in vals) / (len(vals) - 1))
        ess_ratio = max(ES.conservative_ess(vals) / len(vals), 1e-3)
        basis = "OBSERVED in the window"
    else:
        sd, ess_ratio, basis = PLANNING_SD_BPS, PLANNING_ESS_RATIO, "ASSUMED from the last 365 days of public history (replaced once 90 settlements exist)"
    need = SP.required_observations(sd, MIN_EDGE_BPS, alpha=plan.alpha, power=plan.power_target, n_hypotheses=plan.n_hypotheses_declared, adjust=plan.adjust,
                                    ess_ratio=ess_ratio, two_sided=False, method="t")
    window_total = WINDOW_DAYS * PER_DAY
    return {
        "plan_digest": SP.plan_digest(plan), "hurdle_pct": hurdle_pct, "required_funding_bps": required_bps(hurdle_pct),
        "window": {"start": AR._iso(WINDOW_START_MS), "end": AR._iso(WINDOW_END_MS), "settlements_in_window": window_total},
        "state": "WINDOW_NOT_OPEN" if now_ms < WINDOW_START_MS else ("WINDOW_CLOSED" if now_ms >= WINDOW_END_MS else "WINDOW_OPEN"),
        "coverage": cov, "dependence_inputs": basis, "sd_bps": sd, "ess_ratio": ess_ratio,
        "needed_settlements_for_min_edge": need.get("required_raw_n"), "needed_status": need.get("status"),
        "needed_exceeds_window": (need.get("required_raw_n") or 0) > window_total,
        "still_required": None if need.get("required_raw_n") is None else max(0, need["required_raw_n"] - cov["observed"]),
        "reading": "A pass would be evidence of a funding LEVEL, not of deployability. If needed_exceeds_window is true the registered window is too short to "
                   "detect min_edge at the planned power: the likeliest outcome is INSUFFICIENT_POWER, and it must be reported as that, never as 'no edge'.",
    }


def _now_ms() -> int:
    return int(time.time() * 1000)


def evaluate(prints: Sequence[Tuple[float, float]], hurdle_pct: float, ledger: "SP.TrialLedger", now_ms: Optional[int] = None) -> Dict[str, Any]:
    """Run the registered test ONCE the window has closed. Every call is recorded as a trial; refusals record nothing. ``now_ms`` is the clock the
    window is judged against (the wall clock when omitted): before the window ends nothing is evaluated, so prints dated in the future cannot be used."""
    # ``now_ms`` can only move the clock EARLIER: it is clamped to the real clock, so passing the window's end cannot manufacture a closed window.
    now = _now_ms() if now_ms is None else min(int(now_ms), _now_ms())
    if now < WINDOW_END_MS:
        raise SP.WindowCoverageError(f"the registered window has not closed (it ends {AR._iso(WINDOW_END_MS)}): an evaluation of part of it is a peek, and is refused")
    plan = registered_plan(hurdle_pct)
    cov = coverage(prints, WINDOW_END_MS)
    if cov["coverage"] is None or cov["coverage"] < MIN_COVERAGE:
        raise SP.DataIntegrityError(f"coverage {cov['coverage']} is below the declared {MIN_COVERAGE}: {cov['missing']} settlements are missing and none is invented")
    return SP.evaluate_under_plan(plan, observations(prints, hurdle_pct), SP.mean_statistic, expected_digest=PINNED_DIGESTS[hurdle_pct], ledger=ledger)


def _load(path: str) -> List[Tuple[int, float]]:
    rows, _digest = AR.read_rows(path)
    out = []
    for i, r in enumerate(rows):
        try:
            out.append((int(float(r["ts_ms"])), float(r["funding_bps"])))
        except (KeyError, ValueError, TypeError, OverflowError) as exc:
            raise AR.RecordError(f"{os.path.basename(path)} row {i + 1}: needs ts_ms and funding_bps") from exc
    return out


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Progress of the frozen prospective evaluation of BTC funding carry. Offline; no request, no order.")
    ap.add_argument("--funding", required=True, help="local .jsonl/.csv of settled prints: ts_ms, funding_bps")
    ap.add_argument("--hurdle-pct", type=float, default=10.0)
    ap.add_argument("--now-ms", type=int, default=None)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    try:
        rep = progress(_load(a.funding), a.now_ms if a.now_ms is not None else int(time.time() * 1000), a.hurdle_pct)
    except (AR.RecordError, SP.StatisticalPowerError) as exc:
        print(f"carry_prospective: {exc}", file=sys.stderr)
        return 2
    if a.json:
        print(json.dumps(rep, indent=2, sort_keys=True, allow_nan=False))
    else:
        c = rep["coverage"]
        print(f"PROSPECTIVE EVALUATION  plan {rep['plan_digest'][:16]}  hurdle {rep['hurdle_pct']:g}%  requires {rep['required_funding_bps']:.3f} bps/settlement")
        print(f"  window {rep['window']['start']} .. {rep['window']['end']}  state {rep['state']}")
        print(f"  settlements observed {c['observed']} of {c['expected']} due ({c['missing']} missing)")
        print(f"  needed for min edge {MIN_EDGE_BPS} bps: {rep['needed_settlements_for_min_edge']}  still required {rep['still_required']}  "
              f"(window holds {rep['window']['settlements_in_window']}; exceeds window: {rep['needed_exceeds_window']})")
        print(f"  dependence: {rep['dependence_inputs']}")
        print("  " + rep["reading"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
