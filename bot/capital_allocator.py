"""Allocate free capital across opportunities. Pure: no store, no clock, no venue.

The answer may be ZERO and often should be. No opportunity is entitled to
capital; it has to beat doing nothing on a risk-adjusted basis AND fit every
cap. This module never reserves: it returns a plan; the caller reserves each
line through ``capital_reservations.CapitalReservations.reserve`` (which
re-checks free capital atomically) before acting on it.

INPUTS (every one CALLER-SUPPLIED with a non-blank source label)
================================================================
Per opportunity, each a ``Sourced(value, source)``:

  expected_net_return_usd   expected P&L over the holding period, ALREADY NET
                            of execution_cost_usd and funding_and_financing_usd
                            (those two are required as evidence that the net
                            figure priced them; they are reported, never
                            subtracted a second time)
  capital_required_usd      capital the opportunity ties up at its stated size (> 0)
  funding_and_financing_usd expected funding/financing cost (+ = pays) over the period
  margin_requirement_usd    initial margin within capital_required (<= it)
  liquidity_usd             size the market can absorb at the priced cost
  execution_cost_usd        expected round-trip execution cost (>= 0)
  capacity_usd              size the strategy itself can run
  correlation               {"book": rho, "<other opportunity id>": rho, ...}
                            each rho in [-1, 1]; "book" is required
  max_tolerable_loss_usd    loss at which the opportunity is abandoned (> 0)
  drawdown_contribution     contribution to account drawdown as a fraction
  tail_risk_usd             tail loss (e.g. expected shortfall) in USD (>= 0)
  capital_duration_days     how long the capital is tied up (> 0)

Any missing value, blank source, non-finite or out-of-domain value makes the
opportunity INELIGIBLE with a reason. Nothing is defaulted.

RANKING: risk-adjusted INCREMENTAL value per capital-day
=========================================================
With ``B`` the tail risk (USD) of the book plus everything already selected in
this plan, ``t`` the opportunity's tail_risk_usd, and ``rho`` its correlation
with that aggregate, the incremental tail risk is

    dR = sqrt(B^2 + t^2 + 2*rho*B*t) - B

(the marginal change of the standard two-position aggregation). ``rho`` is the
largest of rho["book"] and rho[id] for every opportunity already selected; a
selected id with no stated pairwise rho is taken at rho = 1 (the worst case,
a bound, not an estimate). The score is

    score = (expected_net_return_usd - risk_aversion * dR)
            / (capital_required_usd * capital_duration_days)

in USD of risk-adjusted value per USD-day of capital. ``risk_aversion`` is a
REQUIRED argument with no default (USD of value given up per USD of
incremental tail risk). Only score > 0 is allocated.

SELECTION: greedy and deterministic. Each round re-scores every remaining
candidate against the current aggregate, takes the highest score (ties broken
by opportunity id, ascending) that fits every cap, and allocates its full
capital_required_usd (all-or-nothing: scaling a size down would assume
returns are linear in size, which no input states).

CAPS (``AllocationLimits``, all required): free capital after reservations
(a Sourced value), total, per strategy (a strategy with no cap gets nothing),
sum of max_tolerable_loss, sum of drawdown_contribution, and per correlation
group (concentration).
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

REQUIRED_INPUTS = (
    "expected_net_return_usd", "capital_required_usd", "funding_and_financing_usd",
    "margin_requirement_usd", "liquidity_usd", "execution_cost_usd",
    "capacity_usd", "correlation", "max_tolerable_loss_usd",
    "drawdown_contribution", "tail_risk_usd", "capital_duration_days")

# Reason codes
MISSING_INPUT = "MISSING_INPUT"
UNSOURCED_INPUT = "UNSOURCED_INPUT"
INVALID_INPUT = "INVALID_INPUT"
INCONSISTENT_INPUTS = "INCONSISTENT_INPUTS"
NON_POSITIVE_EXPECTED_NET = "NON_POSITIVE_EXPECTED_NET"
EXCEEDS_CAPACITY = "EXCEEDS_CAPACITY"
EXCEEDS_LIQUIDITY = "EXCEEDS_LIQUIDITY"
NO_STRATEGY_CAP = "NO_STRATEGY_CAP"
NO_RISK_ADJUSTED_VALUE = "NO_RISK_ADJUSTED_VALUE"
FREE_CAPITAL_EXHAUSTED = "FREE_CAPITAL_EXHAUSTED"
TOTAL_CAP = "TOTAL_CAP"
STRATEGY_CAP = "STRATEGY_CAP"
TOTAL_LOSS_CAP = "TOTAL_LOSS_CAP"
DRAWDOWN_CAP = "DRAWDOWN_CAP"
CONCENTRATION_CAP = "CONCENTRATION_CAP"
LIMITS_INVALID = "LIMITS_INVALID"
ALLOCATED = "ALLOCATED"


@dataclass(frozen=True)
class Sourced:
    value: Any
    source: str


@dataclass(frozen=True)
class Opportunity:
    opportunity_id: str
    strategy_id: str
    correlation_group: Optional[Sourced]
    inputs: Mapping[str, Optional[Sourced]]


@dataclass(frozen=True)
class AllocationLimits:
    free_capital_usd: Sourced
    max_total_usd: float
    per_strategy_max_usd: Mapping[str, float]
    max_total_loss_usd: float
    max_total_drawdown_contribution: float
    max_group_usd: float
    book_tail_risk_usd: Sourced


@dataclass(frozen=True)
class OpportunityResult:
    opportunity_id: str
    strategy_id: str
    allocated_usd: float
    eligible: bool
    reasons: Tuple[str, ...]
    score: Optional[float] = None
    incremental_tail_risk_usd: Optional[float] = None


@dataclass(frozen=True)
class Allocation:
    results: Tuple[OpportunityResult, ...]
    total_allocated_usd: float
    free_capital_remaining_usd: Optional[float]
    reasons: Tuple[str, ...] = ()

    def allocated(self) -> Dict[str, float]:
        return {r.opportunity_id: r.allocated_usd for r in self.results
                if r.allocated_usd > 0}


def _num(x: Any) -> Optional[float]:
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        return None
    v = float(x)
    return v if math.isfinite(v) else None


def _validate(op: Opportunity) -> Tuple[Dict[str, Any], List[str]]:
    vals: Dict[str, Any] = {}
    reasons: List[str] = []
    for name in REQUIRED_INPUTS:
        s = op.inputs.get(name)
        if s is None or s.value is None:
            reasons.append(f"{MISSING_INPUT}:{name}")
            continue
        if not isinstance(s.source, str) or not s.source.strip():
            reasons.append(f"{UNSOURCED_INPUT}:{name}")
            continue
        if name == "correlation":
            c = s.value
            if not isinstance(c, Mapping) or "book" not in c:
                reasons.append(f"{MISSING_INPUT}:correlation.book")
                continue
            parsed = {}
            for k, v in c.items():
                n = _num(v)
                if n is None or not -1.0 <= n <= 1.0:
                    reasons.append(f"{INVALID_INPUT}:correlation.{k}")
                parsed[str(k)] = n
            vals[name] = parsed
            continue
        n = _num(s.value)
        if n is None:
            reasons.append(f"{INVALID_INPUT}:{name}")
            continue
        vals[name] = n
    if reasons:
        return vals, reasons
    positive = ("capital_required_usd", "capital_duration_days",
                "max_tolerable_loss_usd")
    non_negative = ("margin_requirement_usd", "liquidity_usd", "execution_cost_usd",
                    "capacity_usd", "tail_risk_usd", "drawdown_contribution")
    for name in positive:
        if vals[name] <= 0:
            reasons.append(f"{INVALID_INPUT}:{name}")
    for name in non_negative:
        if vals[name] < 0:
            reasons.append(f"{INVALID_INPUT}:{name}")
    if reasons:
        return vals, reasons
    if vals["margin_requirement_usd"] > vals["capital_required_usd"]:
        reasons.append(f"{INCONSISTENT_INPUTS}:margin_exceeds_capital")
    if vals["capital_required_usd"] > vals["capacity_usd"]:
        reasons.append(EXCEEDS_CAPACITY)
    if vals["capital_required_usd"] > vals["liquidity_usd"]:
        reasons.append(EXCEEDS_LIQUIDITY)
    if vals["expected_net_return_usd"] <= 0:
        reasons.append(NON_POSITIVE_EXPECTED_NET)
    if op.correlation_group is None or not isinstance(op.correlation_group.value, str) \
            or not op.correlation_group.value.strip() \
            or not str(op.correlation_group.source or "").strip():
        reasons.append(f"{MISSING_INPUT}:correlation_group")
    return vals, reasons


def incremental_tail_risk(book: float, tail: float, rho: float) -> float:
    """sqrt(B^2 + t^2 + 2 rho B t) - B, floored at -B (never below removing B)."""
    inside = book * book + tail * tail + 2.0 * rho * book * tail
    return math.sqrt(max(0.0, inside)) - book


def allocate(opportunities: Sequence[Opportunity], limits: AllocationLimits, *,
             risk_aversion: float) -> Allocation:
    """Return a plan (possibly all zero). Reserves NOTHING; see module doc."""
    plan_reasons: List[str] = []
    free = _num(getattr(limits.free_capital_usd, "value", None))
    if free is None or not str(getattr(limits.free_capital_usd, "source", "")).strip():
        plan_reasons.append(f"{LIMITS_INVALID}:free_capital_usd")
    book_tail = _num(getattr(limits.book_tail_risk_usd, "value", None))
    if book_tail is None or book_tail < 0 or \
            not str(getattr(limits.book_tail_risk_usd, "source", "")).strip():
        plan_reasons.append(f"{LIMITS_INVALID}:book_tail_risk_usd")
    for name in ("max_total_usd", "max_total_loss_usd",
                 "max_total_drawdown_contribution", "max_group_usd"):
        v = _num(getattr(limits, name))
        if v is None or v < 0:
            plan_reasons.append(f"{LIMITS_INVALID}:{name}")
    ra = _num(risk_aversion)
    if ra is None or ra < 0:
        plan_reasons.append(f"{LIMITS_INVALID}:risk_aversion")

    validated: Dict[str, Tuple[Opportunity, Dict[str, Any]]] = {}
    results: Dict[str, OpportunityResult] = {}
    seen: set = set()
    for op in sorted(opportunities, key=lambda o: str(o.opportunity_id)):
        oid = str(op.opportunity_id)
        if oid in seen or not oid.strip():
            results[oid + "#dup" if oid in seen else oid] = OpportunityResult(
                oid, op.strategy_id, 0.0, False, (f"{INVALID_INPUT}:opportunity_id",))
            continue
        seen.add(oid)
        vals, reasons = _validate(op)
        cap = _num(limits.per_strategy_max_usd.get(op.strategy_id)) \
            if isinstance(limits.per_strategy_max_usd, Mapping) else None
        if cap is None or cap < 0:
            reasons.append(NO_STRATEGY_CAP)
        if reasons:
            results[oid] = OpportunityResult(oid, op.strategy_id, 0.0, False,
                                             tuple(reasons))
        else:
            validated[oid] = (op, vals)

    if plan_reasons:
        for oid, (op, _v) in validated.items():
            results[oid] = OpportunityResult(oid, op.strategy_id, 0.0, True,
                                             tuple(plan_reasons))
        return Allocation(tuple(results[k] for k in sorted(results)), 0.0,
                          free, tuple(plan_reasons))

    remaining_free = float(free)
    total = 0.0
    loss_sum = 0.0
    dd_sum = 0.0
    by_strategy: Dict[str, float] = {}
    by_group: Dict[str, float] = {}
    aggregate_tail = float(book_tail)
    selected: List[str] = []
    pending = dict(validated)
    last: Dict[str, Tuple[float, float]] = {}

    while pending:
        scored = []
        for oid, (op, v) in pending.items():
            rho = max([v["correlation"]["book"]]
                      + [v["correlation"].get(s, 1.0) for s in selected])
            d_r = incremental_tail_risk(aggregate_tail, v["tail_risk_usd"], rho)
            score = (v["expected_net_return_usd"] - ra * d_r) / (
                v["capital_required_usd"] * v["capital_duration_days"])
            last[oid] = (score, d_r)
            scored.append((-score, oid))
        scored.sort()
        picked = None
        for neg, oid in scored:
            op, v = pending[oid]
            score, d_r = last[oid]
            c = v["capital_required_usd"]
            grp = op.correlation_group.value
            why = None
            if not score > 0:
                why = NO_RISK_ADJUSTED_VALUE
            elif c > remaining_free:
                why = FREE_CAPITAL_EXHAUSTED
            elif total + c > limits.max_total_usd:
                why = TOTAL_CAP
            elif by_strategy.get(op.strategy_id, 0.0) + c > \
                    float(limits.per_strategy_max_usd[op.strategy_id]):
                why = STRATEGY_CAP
            elif loss_sum + v["max_tolerable_loss_usd"] > limits.max_total_loss_usd:
                why = TOTAL_LOSS_CAP
            elif dd_sum + v["drawdown_contribution"] > \
                    limits.max_total_drawdown_contribution:
                why = DRAWDOWN_CAP
            elif by_group.get(grp, 0.0) + c > limits.max_group_usd:
                why = CONCENTRATION_CAP
            if why is None:
                picked = oid
                break
            results[oid] = OpportunityResult(oid, op.strategy_id, 0.0, True,
                                             (why,), score, d_r)
        if picked is None:
            break
        op, v = pending.pop(picked)
        score, d_r = last[picked]
        c = v["capital_required_usd"]
        remaining_free -= c
        total += c
        loss_sum += v["max_tolerable_loss_usd"]
        dd_sum += v["drawdown_contribution"]
        by_strategy[op.strategy_id] = by_strategy.get(op.strategy_id, 0.0) + c
        grp = op.correlation_group.value
        by_group[grp] = by_group.get(grp, 0.0) + c
        aggregate_tail += d_r
        selected.append(picked)
        results[picked] = OpportunityResult(picked, op.strategy_id, c, True,
                                            (ALLOCATED,), score, d_r)
        # Anything refused this round is re-scored next round (the aggregate
        # changed), so its reason is overwritten with the final verdict.
    return Allocation(tuple(results[k] for k in sorted(results)), total,
                      remaining_free, ())
