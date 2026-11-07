"""The economic verdict engine: a strategy candidate, the evidence ladder it must climb, and the ONE verdict it is allowed to hold.

    python3 tools/strategy_verdict.py --candidate cand.json --evidence evidence.jsonl [--json]

It is built to KILL strategies, not to promote them. The verdicts, weakest to strongest:

    UNTESTED              the spec is incomplete/invalid, or nothing has been tested yet
    INSUFFICIENT_DATA     evidence exists but cannot support a claim (too little data, no multiple-testing accounting, unlabelled or unknown costs)
    REJECTED              a stage FAILED or a declared rejection condition fired. Terminal: a rejected candidate is only re-opened as a NEW candidate (new id, new registration)
    PROMISING             historical + out-of-sample + cost + capacity all PASS. Offline evidence only; says nothing about the live market
    PROSPECTIVE           PROMISING and a registered prospective observation is under way (or complete but not yet execution-validated)
    QUALIFIED             prospective observation PASSED and execution validation PASSED (venue mechanics, measured fills/fees)
    OWNER_PROFIT_EVIDENCED  QUALIFIED and a RECONCILED owner-account result with a positive net after every cost, from the owner importer

The LADDER (each rung needs the one before it; evidence recorded for a later rung without the earlier one is ignored and reported, never counted):
    hypothesis -> historical_test -> oos_test -> cost_model -> capacity_test -> prospective_observation -> execution_validation -> reconciled_owner_result

A backtest return alone can never promote: ``historical_test`` PASS with nothing after it is INSUFFICIENT_DATA. Evidence must name its provenance; the offline stages must carry a
data hash; a PASS that does not state its multiple-testing accounting (number of variants tried, adjustment applied) does not count; a cost PASS with any UNKNOWN cost input or a
non-positive net edge does not count; a capacity PASS needs a stated capacity at least the declared minimum. Unknown is not zero; estimated is not observed; modelled is not realised.

What this is NOT: a statistics package (``statistical_power``/``carry_prospective`` do the testing and hand this engine their result), a profitability claim, or a trading gate. The
registered frozen BTC experiment is not changed by it: ``btc_carry_candidate()`` only DESCRIBES it so the engine can state where it stands (UNTESTED / INSUFFICIENT_DATA, never promoted
by a rate of return).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

BOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BOT)
sys.path.insert(0, os.path.join(BOT, "tools"))

UNTESTED, INSUFFICIENT_DATA, REJECTED, PROMISING, PROSPECTIVE, QUALIFIED, OWNER_PROFIT_EVIDENCED = (
    "UNTESTED", "INSUFFICIENT_DATA", "REJECTED", "PROMISING", "PROSPECTIVE", "QUALIFIED", "OWNER_PROFIT_EVIDENCED")
VERDICTS = (UNTESTED, INSUFFICIENT_DATA, REJECTED, PROMISING, PROSPECTIVE, QUALIFIED, OWNER_PROFIT_EVIDENCED)

LADDER = ("hypothesis", "historical_test", "oos_test", "cost_model", "capacity_test", "prospective_observation", "execution_validation", "reconciled_owner_result")
PASS, FAIL, INCONCLUSIVE = "PASS", "FAIL", "INCONCLUSIVE"
PROVENANCE_LABELS = ("measured", "venue_stated", "derived", "modelled", "assumed", "unknown")
#: What a stage's evidence must carry to be COUNTED (otherwise it is INCONCLUSIVE and the reason is reported).
OFFLINE_STAGES = ("historical_test", "oos_test", "cost_model", "capacity_test")
MIN_OBSERVATIONS = {"historical_test": 100, "oos_test": 50}      # independent observations, not overlapping windows; the evidence states what it counted

REQUIRED_SPEC_FIELDS = ("candidate_id", "hypothesis", "economic_mechanism", "counterparty", "required_data", "eligibility_rules", "entry", "exit", "holding_period", "execution_model",
                        "fee_model", "funding_model", "slippage_model", "capacity_model", "risk_model", "drawdown_model", "OOS_protocol", "prospective_protocol",
                        "rejection_conditions")
#: Declarations about bias. The candidate states them; a False/absent one is a problem, not a default.
BIAS_DECLARATIONS = ("point_in_time_universe", "no_future_funding_knowledge", "no_look_ahead", "fills_modelled_against_real_depth", "hedge_completeness_modelled")
#: A cost model must price each of these (value + provenance label). A zero is allowed ONLY when its label is measured or venue_stated (a real zero, e.g. a stated rebate).
COST_COMPONENTS = {"fee_model": ("maker_bps", "taker_bps"), "slippage_model": ("entry_bps", "exit_bps"), "funding_model": ("expected_bps_per_settlement",)}


class SpecError(ValueError):
    pass


def _nonempty(v: Any) -> bool:
    return v not in (None, "", [], {}, ()) and not (isinstance(v, str) and not v.strip())


def spec_problems(spec: Mapping[str, Any]) -> List[str]:
    """Why a candidate is not a valid, testable specification. Empty = valid. Every field is required; the cost models must label every input; bias declarations must be explicit."""
    problems: List[str] = []
    for k in REQUIRED_SPEC_FIELDS:
        if not _nonempty(spec.get(k)):
            problems.append(f"MISSING:{k}")
    if not isinstance(spec.get("rejection_conditions"), list) or not all(isinstance(c, str) and c.strip() for c in spec.get("rejection_conditions") or []):
        problems.append("REJECTION_CONDITIONS_MUST_BE_A_LIST_OF_FALSIFIABLE_STATEMENTS")
    rd = spec.get("required_data")
    if isinstance(rd, list):
        for i, item in enumerate(rd):
            if not isinstance(item, dict) or not item.get("name") or item.get("point_in_time") is not True:
                problems.append(f"REQUIRED_DATA[{i}]_NOT_DECLARED_POINT_IN_TIME")
    elif _nonempty(rd):
        problems.append("REQUIRED_DATA_MUST_BE_A_LIST_OF_{name,point_in_time}")
    bias = spec.get("bias_declarations") if isinstance(spec.get("bias_declarations"), dict) else {}
    for k in BIAS_DECLARATIONS:
        if bias.get(k) is not True:
            problems.append(f"BIAS_NOT_RULED_OUT:{k}")
    for model, comps in COST_COMPONENTS.items():
        m = spec.get(model)
        if not isinstance(m, dict):
            continue                                                   # MISSING already reported
        for c in comps:
            e = m.get(c)
            if not isinstance(e, dict) or "value" not in e or e.get("provenance") not in PROVENANCE_LABELS:
                problems.append(f"COST_INPUT_UNLABELLED:{model}.{c}")
                continue
            v = e["value"]
            if v is None or isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
                problems.append(f"COST_INPUT_NOT_A_NUMBER:{model}.{c}")
            elif v == 0 and e["provenance"] not in ("measured", "venue_stated"):
                problems.append(f"ZERO_COST_WITHOUT_MEASUREMENT:{model}.{c}")        # a zero fee/slippage must be a real, stated zero
    cap = spec.get("capacity_model")
    if isinstance(cap, dict) and not isinstance(cap.get("min_viable_capacity_usd"), (int, float)):
        problems.append("CAPACITY_MODEL_NEEDS_min_viable_capacity_usd")
    if "OOS_protocol" in spec and isinstance(spec["OOS_protocol"], dict) and not _nonempty(spec["OOS_protocol"].get("train_test_boundary")):
        problems.append("OOS_PROTOCOL_NEEDS_train_test_boundary")
    return problems


@dataclass(frozen=True)
class EvidenceItem:
    stage: str
    result: str
    provenance: str
    metrics: Mapping[str, Any] = field(default_factory=dict)
    data_sha256: Optional[str] = None
    recorded_ms: int = 0
    status: Optional[str] = None            # for reconciled_owner_result: the importer's status word


def evidence_from_dict(d: Mapping[str, Any]) -> EvidenceItem:
    if not isinstance(d, Mapping) or d.get("stage") not in LADDER or d.get("result") not in (PASS, FAIL, INCONCLUSIVE):
        raise SpecError(f"not an evidence item: {d!r}")
    return EvidenceItem(stage=d["stage"], result=d["result"], provenance=str(d.get("provenance") or ""), metrics=dict(d.get("metrics") or {}),
                        data_sha256=d.get("data_sha256"), recorded_ms=int(d.get("recorded_ms") or 0), status=d.get("status"))


def _is_hex64(x: Any) -> bool:
    return isinstance(x, str) and len(x) == 64 and all(c in "0123456789abcdef" for c in x)


def counted(item: EvidenceItem, spec: Mapping[str, Any]) -> Tuple[bool, Optional[str]]:
    """Does this item count as a PASS/FAIL at its stage? (ok, why_not). A FAIL always counts (killing is easy); a PASS must earn it."""
    if item.result == FAIL:
        return True, None
    if item.result == INCONCLUSIVE:
        return False, "INCONCLUSIVE"
    if not item.provenance.strip():
        return False, "PASS_WITHOUT_PROVENANCE"
    m = item.metrics
    s = item.stage
    if s in OFFLINE_STAGES and not _is_hex64(item.data_sha256):
        return False, "OFFLINE_PASS_WITHOUT_DATA_HASH"
    if s in ("historical_test", "oos_test"):
        n = m.get("independent_observations")
        if isinstance(n, bool) or not isinstance(n, int) or n < MIN_OBSERVATIONS[s]:
            return False, f"TOO_FEW_INDEPENDENT_OBSERVATIONS(need {MIN_OBSERVATIONS[s]})"
        nv = m.get("n_variants_tested")
        if isinstance(nv, bool) or not isinstance(nv, int) or nv < 1 or m.get("multiple_testing_adjusted") is not True:
            return False, "NO_MULTIPLE_TESTING_ACCOUNTING"
        if m.get("uses_future_information") is not False:
            return False, "LOOK_AHEAD_NOT_RULED_OUT"
        if m.get("universe_point_in_time") is not True:
            return False, "UNIVERSE_NOT_POINT_IN_TIME"
    if s == "cost_model":
        if m.get("unknown_cost_inputs") != 0:
            return False, "UNKNOWN_COST_INPUTS"
        edge = m.get("net_edge_bps_after_costs")
        if isinstance(edge, bool) or not isinstance(edge, (int, float)) or not math.isfinite(edge) or edge <= 0:
            return False, "NET_EDGE_NOT_POSITIVE_AFTER_COSTS"
    if s == "capacity_test":
        cap, need = m.get("capacity_usd"), (spec.get("capacity_model") or {}).get("min_viable_capacity_usd")
        if not isinstance(cap, (int, float)) or isinstance(cap, bool) or not isinstance(need, (int, float)) or cap < need:
            return False, "CAPACITY_BELOW_MINIMUM_VIABLE"
    if s == "prospective_observation":
        if m.get("registered_before_observation") is not True or m.get("complete") is not True:
            return False, "PROSPECTIVE_NOT_REGISTERED_OR_NOT_COMPLETE"
    if s == "execution_validation":
        if m.get("venue") not in ("testnet", "mainnet") or m.get("fills_measured") is not True or m.get("fees_measured") is not True:
            return False, "EXECUTION_NOT_MEASURED_AT_A_VENUE"
    if s == "reconciled_owner_result":
        net = m.get("net_usd_after_all_costs")
        if item.status != "RECONCILED" or isinstance(net, bool) or not isinstance(net, (int, float)) or not math.isfinite(net) or net <= 0:
            return False, "OWNER_RESULT_NOT_RECONCILED_OR_NOT_POSITIVE"
    return True, None


@dataclass
class VerdictResult:
    verdict: str
    reasons: List[str]
    next_stage: Optional[str]
    stages: Dict[str, str]                 # stage -> PASS | FAIL | NOT_COUNTED:<why> | MISSING
    ignored: List[str]                     # evidence recorded out of order (not counted)
    spec_problems: List[str]

    def as_dict(self) -> Dict[str, Any]:
        return {"verdict": self.verdict, "reasons": self.reasons, "next_stage": self.next_stage, "stages": self.stages, "ignored_evidence": self.ignored,
                "spec_problems": self.spec_problems}


def evaluate(spec: Mapping[str, Any], evidence: Sequence[EvidenceItem]) -> VerdictResult:
    """The verdict for a candidate given ALL its evidence. Deterministic; the same inputs give the same answer; later evidence never rescues an earlier FAIL."""
    problems = spec_problems(spec)
    stages: Dict[str, str] = {s: "MISSING" for s in LADDER}
    ignored: List[str] = []
    reasons: List[str] = []
    if problems:
        return VerdictResult(UNTESTED, ["the candidate specification is invalid or incomplete"] + problems[:10], "hypothesis", stages, ignored, problems)
    stages["hypothesis"] = PASS                                                  # a valid, complete specification IS the registered hypothesis
    by_stage: Dict[str, List[EvidenceItem]] = {s: [] for s in LADDER}
    for e in evidence:
        by_stage[e.stage].append(e)
    # declared rejection conditions that fired are reported as FAIL evidence at a stage named 'rejection' (any stage's metrics may carry them)
    for e in evidence:
        fired = e.metrics.get("rejection_conditions_fired")
        if isinstance(fired, list) and fired:
            reasons.append("REJECTION_CONDITION_FIRED:" + "|".join(str(x) for x in fired))
    for stage in LADDER[1:]:
        verdicts = []
        notes = []
        for e in by_stage[stage]:
            ok, why = counted(e, spec)
            if ok:
                verdicts.append(e.result)
            else:
                notes.append(why or "NOT_COUNTED")
        if FAIL in verdicts:
            stages[stage] = FAIL
        elif PASS in verdicts:
            stages[stage] = PASS
        elif notes:
            stages[stage] = "NOT_COUNTED:" + notes[-1]
    # order enforcement: a rung counts only if every earlier rung PASSED
    first_gap: Optional[str] = None
    for stage in LADDER:
        if stages[stage] != PASS and first_gap is None:
            first_gap = stage
        elif first_gap is not None and stages[stage] in (PASS,):
            ignored.append(f"{stage} PASS recorded while {first_gap} is {stages[first_gap]}: not counted")
            stages[stage] = f"NOT_COUNTED:OUT_OF_ORDER(after {first_gap})"
    failed = [s for s in LADDER if stages[s] == FAIL]
    if failed or any(r.startswith("REJECTION_CONDITION_FIRED") for r in reasons):
        return VerdictResult(REJECTED, reasons + [f"{s} FAILED" for s in failed], None, stages, ignored, problems)
    passed = [s for s in LADDER if stages[s] == PASS]
    have = set(passed)
    offline = {"historical_test", "oos_test", "cost_model", "capacity_test"}
    if not (have & offline):
        has_any = any(by_stage[s] for s in LADDER[1:])
        return VerdictResult(INSUFFICIENT_DATA if has_any else UNTESTED,
                             ["evidence exists but none of it counts yet"] if has_any else ["valid specification; nothing has been tested"], "historical_test", stages, ignored, problems)
    if not offline <= have:
        missing = next(s for s in ("historical_test", "oos_test", "cost_model", "capacity_test") if s not in have)
        return VerdictResult(INSUFFICIENT_DATA, ["a backtest alone never promotes: the offline ladder (historical, OOS, cost, capacity) is incomplete"] + reasons, missing, stages, ignored, problems)
    if "prospective_observation" not in have:
        pend = any(e.stage == "prospective_observation" and e.metrics.get("registered_before_observation") is True and e.metrics.get("complete") is False for e in evidence)
        return VerdictResult(PROSPECTIVE if pend else PROMISING,
                             ["offline evidence is complete; the live market has not confirmed anything" if not pend else "a registered prospective observation is under way"] + reasons,
                             "prospective_observation", stages, ignored, problems)
    if "execution_validation" not in have:
        return VerdictResult(PROSPECTIVE, ["prospective observation passed; execution at a venue is not yet validated"] + reasons, "execution_validation", stages, ignored, problems)
    if "reconciled_owner_result" not in have:
        return VerdictResult(QUALIFIED, ["prospective + execution evidence in hand; no reconciled owner result yet"] + reasons, "reconciled_owner_result", stages, ignored, problems)
    return VerdictResult(OWNER_PROFIT_EVIDENCED, ["reconciled owner result with a positive net after all costs"] + reasons, None, stages, ignored, problems)


# ---------------------------------------------------------------- the registry link and the frozen BTC description

STAGE_TO_REGISTRY_LEVEL = {"hypothesis": "RESEARCH_CANDIDATE", "historical_test": "BACKTEST", "oos_test": "OUT_OF_SAMPLE", "cost_model": "BACKTEST", "capacity_test": "BACKTEST",
                           "prospective_observation": "PROSPECTIVE", "execution_validation": "TESTNET_MECHANICS", "reconciled_owner_result": "OWNER_ACCOUNT_VERIFIED"}


def register_candidate(registry: Dict[str, Any], spec: Mapping[str, Any], *, code_sha: str, input_data_sha256: Mapping[str, str]) -> Dict[str, Any]:
    """Register a candidate in the tamper-evident hypothesis registry BEFORE it is evaluated. Refuses an invalid spec. Maps the spec onto the registry's required fields."""
    import hypothesis_registry as hr
    problems = spec_problems(spec)
    if problems:
        raise SpecError("refusing to register an invalid candidate: " + ", ".join(problems[:6]))
    return hr.register_experiment(
        registry, str(spec["candidate_id"]), hypothesis_id=str(spec["candidate_id"]), mechanism=str(spec["economic_mechanism"]), family=str(spec.get("family") or "unclassified"),
        strategy=str(spec["candidate_id"]), universe=spec["eligibility_rules"], input_data_sha256=dict(input_data_sha256), code_sha=str(code_sha),
        data_window=spec["OOS_protocol"].get("data_window") or spec["OOS_protocol"], parameter_grid=spec.get("parameter_grid") or {"fixed": True},
        costs={k: spec[k] for k in ("fee_model", "funding_model", "slippage_model")}, statistical_design=spec["OOS_protocol"],
        train_test=spec["OOS_protocol"]["train_test_boundary"], multiple_testing_family=str(spec.get("multiple_testing_family") or spec["candidate_id"]),
        prospective_period=spec["prospective_protocol"], source="tools/strategy_verdict.py")


def record_evidence(registry: Dict[str, Any], candidate_id: str, item: EvidenceItem) -> None:
    import hypothesis_registry as hr
    hr.add_evidence(registry, candidate_id, STAGE_TO_REGISTRY_LEVEL[item.stage], result=f"{item.stage}:{item.result}", provenance=item.provenance or "none",
                    details={"metrics": dict(item.metrics), "data_sha256": item.data_sha256, "status": item.status})


def spec_digest(spec: Mapping[str, Any]) -> str:
    return hashlib.sha256(json.dumps(spec, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()


def btc_carry_candidate() -> Dict[str, Any]:
    """DESCRIBES the frozen BTC prospective experiment as a candidate so this engine can say where it stands. It changes nothing in the plan (read-only reference to its pinned digests)."""
    import carry_prospective as CP
    lab = lambda v, p: {"value": v, "provenance": p}
    return {
        "candidate_id": "btcusdt_funding_carry_overlay@prospective-1", "family": "funding_carry",
        "hypothesis": "mean excess funding per 8h settlement on BTCUSDT exceeds the hurdle-implied requirement after the plan's costs",
        "economic_mechanism": "a delta-neutral short perpetual against owned BTC receives funding paid by leveraged longs", "counterparty": "perpetual-swap longs paying funding",
        "required_data": [{"name": "bybit_btcusdt_funding_history", "point_in_time": True}],
        "eligibility_rules": ["BTCUSDT linear perpetual only", "8h funding interval only"], "entry": "per carry_prospective registered plan", "exit": "per registered plan",
        "holding_period": "30 days (registered assumption)", "execution_model": "taker, overlay (perp legs only)",
        "fee_model": {"maker_bps": lab(2.0, "assumed"), "taker_bps": lab(5.5, "assumed")},
        "funding_model": {"expected_bps_per_settlement": lab(0.259, "measured")},
        "slippage_model": {"entry_bps": lab(0.2, "assumed"), "exit_bps": lab(0.2, "assumed")},
        "capacity_model": {"min_viable_capacity_usd": 100.0, "note": "capacity not yet measured"},
        "risk_model": "see risk_policy_proposal (funding-only; basis/margin unmeasured)", "drawdown_model": "funding-only 130.5 bps whole sample / 17.6 bps last 365d (measured, in-sample)",
        "OOS_protocol": {"train_test_boundary": "none: prospective only", "data_window": {"start_ms": CP.WINDOW_START_MS, "end_ms": CP.WINDOW_END_MS},
                         "plan_digests": {f"{h:g}": CP.PINNED_DIGESTS[h] for h in CP.HURDLES_PCT}},
        "prospective_protocol": f"{CP.WINDOW_START_MS}..{CP.WINDOW_END_MS}, evaluated only via tools/trial_journal.py",
        "rejection_conditions": ["closed-window evaluation under the registered plan is not supported at any hurdle", "coverage below the registered floor"],
        "bias_declarations": {k: True for k in BIAS_DECLARATIONS},
    }


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Economic verdict for a strategy candidate. Offline. Built to kill, not to promote.")
    ap.add_argument("--candidate", help="candidate JSON (see REQUIRED_SPEC_FIELDS); omit with --btc to describe the frozen BTC experiment")
    ap.add_argument("--evidence", default=None, help="JSONL of evidence items {stage,result,provenance,metrics,data_sha256}")
    ap.add_argument("--btc", action="store_true")
    a = ap.parse_args(argv)
    try:
        spec = btc_carry_candidate() if a.btc else json.load(open(a.candidate, encoding="utf-8"))
        ev = [evidence_from_dict(json.loads(x)) for x in open(a.evidence, encoding="utf-8") if x.strip()] if a.evidence else []
    except (OSError, ValueError, TypeError, SpecError) as exc:
        print(f"strategy_verdict: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    res = evaluate(spec, ev)
    print(json.dumps(res.as_dict(), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
