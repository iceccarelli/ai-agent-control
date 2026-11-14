#!/usr/bin/env python3
"""The deterministic gate a strategy must pass before it may trade: ELIGIBLE or NO_TRADE, never anything in between.

Round 22 (the owner's directive: "the strategy must earn the right to trade"). The software working is not a reason to trade; the strategy is a hypothesis. The gate is a CONJUNCTION of eight terms:

    DATA_VALID + COSTS_KNOWN + CAPITAL_KNOWN + EXECUTION_MODEL_VALID + EDGE_POSITIVE_AFTER_COSTS
    + STATISTICAL_EVIDENCE_SUFFICIENT + RISK_LIMITS_ACCEPTABLE + AUTHORITY_VALID  =  ELIGIBLE      (otherwise NO_TRADE)

Each term is PASS, FAIL or UNKNOWN. **UNKNOWN is never PASS**: a fact nobody established, a missing input, a malformed value and an unknown cost all leave the conjunction false. Nothing here weakens the gate to
make an executor active; a Testnet MEASUREMENT session has its own, separately named outcome (ELIGIBLE_MEASUREMENT_ONLY) and its own named waivers, which can never be read as eligibility to trade for profit.

Profiles
  PROFIT_SEEKING  all eight terms must PASS -> ``ELIGIBLE``. Today no candidate can pass STATISTICAL_EVIDENCE_SUFFICIENT or EXECUTION_MODEL_VALID (nothing is QUALIFIED; nothing is calibrated against a
                  measured session), so the honest result is NO_TRADE. That is the finding, not a defect.
  MEASUREMENT     a bounded Testnet MECHANICS session (fake money: it measures lifecycle, fees and fills and is never an opportunity). EDGE_POSITIVE_AFTER_COSTS and STATISTICAL_EVIDENCE_SUFFICIENT are WAIVED
                  with a stated reason (the expected net is NOT claimed positive; the loss is bounded by RISK_LIMITS_ACCEPTABLE). The other six must PASS -> ``ELIGIBLE_MEASUREMENT_ONLY``.

Pure: no I/O, no clock, no network, no environment. The caller assembles ``inputs`` from facts it already holds; the result is canonical JSON with a digest, so the same facts always give the same answer."""
from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

SCHEMA = "eligibility_gate/v1"
PASS, FAIL, UNKNOWN, WAIVED = "PASS", "FAIL", "UNKNOWN", "WAIVED"
TERMS = ("DATA_VALID", "COSTS_KNOWN", "CAPITAL_KNOWN", "EXECUTION_MODEL_VALID", "EDGE_POSITIVE_AFTER_COSTS", "STATISTICAL_EVIDENCE_SUFFICIENT", "RISK_LIMITS_ACCEPTABLE", "AUTHORITY_VALID")
PROFIT_SEEKING, MEASUREMENT = "PROFIT_SEEKING", "MEASUREMENT"
ELIGIBLE, ELIGIBLE_MEASUREMENT_ONLY, NO_TRADE = "ELIGIBLE", "ELIGIBLE_MEASUREMENT_ONLY", "NO_TRADE"
#: the only terms a MEASUREMENT session may waive, each with the reason printed in the result
MEASUREMENT_WAIVERS = {
    "EDGE_POSITIVE_AFTER_COSTS": "a mechanics session does not claim a positive expected net edge: it measures lifecycle, fees and fills (fake money); the loss it can cost is bounded by RISK_LIMITS_ACCEPTABLE",
    "STATISTICAL_EVIDENCE_SUFFICIENT": "a mechanics session is not evidence about a strategy and is never counted as one",
}
#: candidate statuses (candidate_evaluation/v1) that are evidence of a tradeable edge; only QUALIFIED is sufficient
EVIDENCE_QUALIFIED = "QUALIFIED"
EVIDENCE_NEGATIVE = ("NEGATIVE", "INSUFFICIENT", "DATA_INVALID")
EVIDENCE_UNTESTED = ("UNTESTED",)
#: the minimum number of measured sessions before the execution model is called calibrated (exec_quality.MIN_COMPARABLE_ORDERS is the same notion; stated here so the gate stays a leaf module)
MIN_CALIBRATION_SESSIONS = 30
UNKNOWN_FACT = "UNKNOWN"


def _truth(x: Any) -> Optional[bool]:
    return x if isinstance(x, bool) else None


def _num(x: Any) -> Optional[float]:
    if isinstance(x, bool) or x is None:
        return None
    try:
        v = float(str(x))
    except (TypeError, ValueError):
        return None
    return v if v == v and v not in (float("inf"), float("-inf")) else None


def _term(name: str, verdict: str, reasons: Sequence[str], **facts: Any) -> Dict[str, Any]:
    return {"term": name, "verdict": verdict, "reasons": [str(r)[:200] for r in reasons][:6], **({"facts": facts} if facts else {})}


def _all_true(flags: Mapping[str, Any]) -> Tuple[str, List[str]]:
    """PASS when every flag is True; FAIL when any is False; else UNKNOWN (a missing flag is an unestablished fact, never a pass)."""
    bad = [k for k, v in flags.items() if _truth(v) is False]
    unk = [k for k, v in flags.items() if _truth(v) is None]
    if bad:
        return FAIL, [f"{k.upper()}_FALSE" for k in bad]
    if unk:
        return UNKNOWN, [f"{k.upper()}_NOT_ESTABLISHED" for k in unk]
    return PASS, []


def _data_valid(inputs: Mapping[str, Any]) -> Dict[str, Any]:
    d = inputs.get("data") if isinstance(inputs.get("data"), Mapping) else {}
    v, why = _all_true({"venue_identity": d.get("venue_identity"), "books_fresh": d.get("books_fresh"), "funding_observed": d.get("funding_observed")})
    return _term("DATA_VALID", v, why, data_scope=d.get("scope"))


def _costs_known(inputs: Mapping[str, Any], profile: str) -> Dict[str, Any]:
    e = inputs.get("edge") if isinstance(inputs.get("edge"), Mapping) else None
    if e is None:
        return _term("COSTS_KNOWN", UNKNOWN, ["NO_EDGE_VERDICT"])
    costs = e.get("costs_bps")
    if not isinstance(costs, Mapping) or not costs:
        return _term("COSTS_KNOWN", UNKNOWN, ["NO_COST_LINES"])
    unknown = sorted(k for k, v in costs.items() if _num(v) is None)             # None, NaN, inf, a word: none of them is a known cost
    if unknown:
        return _term("COSTS_KNOWN", UNKNOWN, [f"COST_UNKNOWN:{k}" for k in unknown[:4]], unknown_lines=unknown)
    assumed = [str(a) for a in (e.get("assumed_inputs") or [])]
    prov = e.get("provenance") if isinstance(e.get("provenance"), Mapping) else {}
    uncal = [str(u) for u in (prov.get("uncalibrated_modelled") or [])]
    facts = {"assumed_inputs": assumed[:8], "uncalibrated_modelled": uncal[:8]}
    if profile == MEASUREMENT:
        return _term("COSTS_KNOWN", PASS, [], **facts)                   # every line is a number; the assumed ones are exactly what the session exists to measure
    # a strategy that trades for profit must not rest on an ASSUMED input or an uncalibrated typed constant: unmeasured is not known
    if assumed or uncal:
        return _term("COSTS_KNOWN", UNKNOWN, [f"COST_BASIS_NOT_MEASURED:{x}" for x in (assumed + uncal)[:4]], **facts)
    return _term("COSTS_KNOWN", PASS, [], **facts)


def _capital_known(inputs: Mapping[str, Any], profile: str) -> Dict[str, Any]:
    e = inputs.get("edge") if isinstance(inputs.get("edge"), Mapping) else None
    cap = (e or {}).get("capital") if isinstance((e or {}).get("capital"), Mapping) else None
    if cap is None:
        return _term("CAPITAL_KNOWN", UNKNOWN, ["NO_CAPITAL_BLOCK"])
    committed = _num(cap.get("committed_usd"))
    if committed is None or committed <= 0:
        return _term("CAPITAL_KNOWN", UNKNOWN, ["CAPITAL_COMMITTED_UNKNOWN"])
    if profile == PROFIT_SEEKING and cap.get("state") != "STATED":
        return _term("CAPITAL_KNOWN", UNKNOWN, [f"COST_OF_CAPITAL_{cap.get('state') or 'UNSTATED'}"], committed_usd=committed)
    return _term("CAPITAL_KNOWN", PASS, [], committed_usd=committed, cost_of_capital_state=cap.get("state"))


def _execution_model_valid(inputs: Mapping[str, Any], profile: str) -> Dict[str, Any]:
    ex = inputs.get("execution") if isinstance(inputs.get("execution"), Mapping) else {}
    e = inputs.get("edge") if isinstance(inputs.get("edge"), Mapping) else None
    has_lines = bool(e and isinstance(e.get("costs_bps"), Mapping) and e["costs_bps"])
    if profile == MEASUREMENT:
        return _term("EXECUTION_MODEL_VALID", PASS if has_lines else UNKNOWN, [] if has_lines else ["NO_COST_MODEL"], basis="walk-the-book model; this session is what measures it")
    status = ex.get("model_vs_measured_status")
    n = _num(ex.get("sessions_measured"))
    surprise = _num(ex.get("mean_cost_surprise_bps"))
    if status != "ENOUGH" or n is None or n < MIN_CALIBRATION_SESSIONS:
        return _term("EXECUTION_MODEL_VALID", UNKNOWN, [f"MODEL_NOT_CALIBRATED:measured_sessions={None if n is None else int(n)}<{MIN_CALIBRATION_SESSIONS}"], status=status)
    bound = _num(ex.get("max_acceptable_surprise_bps"))
    if surprise is None:
        return _term("EXECUTION_MODEL_VALID", UNKNOWN, ["COST_SURPRISE_UNKNOWN"])
    if bound is None:
        return _term("EXECUTION_MODEL_VALID", UNKNOWN, ["NO_ACCEPTABLE_SURPRISE_BOUND"], mean_cost_surprise_bps=surprise)
    if surprise > bound:
        return _term("EXECUTION_MODEL_VALID", FAIL, ["MODEL_UNDER_CHARGES_EXECUTION"], mean_cost_surprise_bps=surprise, max_acceptable_surprise_bps=bound)
    return _term("EXECUTION_MODEL_VALID", PASS, [], mean_cost_surprise_bps=surprise, sessions_measured=int(n))


def _edge_positive(inputs: Mapping[str, Any], profile: str) -> Dict[str, Any]:
    if profile == MEASUREMENT:
        return _term("EDGE_POSITIVE_AFTER_COSTS", WAIVED, [MEASUREMENT_WAIVERS["EDGE_POSITIVE_AFTER_COSTS"]])
    e = inputs.get("edge") if isinstance(inputs.get("edge"), Mapping) else None
    if e is None:
        return _term("EDGE_POSITIVE_AFTER_COSTS", UNKNOWN, ["NO_EDGE_VERDICT"])
    net = _num(e.get("net_edge_bps"))
    cls = e.get("session_class")
    if net is None:
        return _term("EDGE_POSITIVE_AFTER_COSTS", UNKNOWN, ["NET_EDGE_UNKNOWN"] + [str(r) for r in (e.get("reasons") or [])[:2]])
    if cls == "PROFIT_CANDIDATE" and net > 0:                       # the edge gate's own class already includes the hurdle and the funding haircut
        return _term("EDGE_POSITIVE_AFTER_COSTS", PASS, [], net_edge_bps=net, expected_net_usd=e.get("expected_net_usd"))
    return _term("EDGE_POSITIVE_AFTER_COSTS", FAIL, [f"EDGE_{cls}", f"NET_EDGE_BPS_{net}"] + [str(r) for r in (e.get("reasons") or [])[:2]], net_edge_bps=net)


def _statistical(inputs: Mapping[str, Any], profile: str) -> Dict[str, Any]:
    if profile == MEASUREMENT:
        return _term("STATISTICAL_EVIDENCE_SUFFICIENT", WAIVED, [MEASUREMENT_WAIVERS["STATISTICAL_EVIDENCE_SUFFICIENT"]])
    ev = inputs.get("evidence") if isinstance(inputs.get("evidence"), Mapping) else {}
    st = ev.get("status")
    if st == EVIDENCE_QUALIFIED:
        return _term("STATISTICAL_EVIDENCE_SUFFICIENT", PASS, [], candidate_id=ev.get("candidate_id"))
    if st in EVIDENCE_NEGATIVE:
        return _term("STATISTICAL_EVIDENCE_SUFFICIENT", FAIL, [f"CANDIDATE_{st}"] + [str(x) for x in (ev.get("reasons") or [])[:2]], candidate_id=ev.get("candidate_id"))
    return _term("STATISTICAL_EVIDENCE_SUFFICIENT", UNKNOWN, [f"CANDIDATE_{st or 'UNTESTED'}"], candidate_id=ev.get("candidate_id"))


def _risk(inputs: Mapping[str, Any], profile: str) -> Dict[str, Any]:
    """PROFIT_SEEKING needs a MODELLED worst-case loss within the cap. A MEASUREMENT session has no modelled worst case (the per-session loss limit is a trigger, not a cap: the unwind that follows it can
    lose more), so it is held to the limit it actually runs under: the enforced loss limit must not exceed the policy ceiling. The worst case then stays UNKNOWN in the facts, never zero."""
    r = inputs.get("risk") if isinstance(inputs.get("risk"), Mapping) else {}
    cap, worst, limit = _num(r.get("loss_cap_usd")), _num(r.get("worst_case_loss_usd")), _num(r.get("loss_limit_usd"))
    committed, available = _num(r.get("capital_committed_usd")), _num(r.get("capital_available_usd"))
    raw_stops = r.get("book_stops")
    if not isinstance(raw_stops, (list, tuple)):
        return _term("RISK_LIMITS_ACCEPTABLE", UNKNOWN, ["BOOK_STOPS_NOT_STATED"])      # whether the capital book holds a stop is a fact the caller must state; absent is not "none"
    stops = [str(s) for s in raw_stops]
    if stops:
        return _term("RISK_LIMITS_ACCEPTABLE", FAIL, [f"BOOK_STOP:{s}" for s in stops[:4]])
    bound_name, bound = ("loss_limit_usd", limit) if profile == MEASUREMENT else ("worst_case_loss_usd", worst)
    missing = [k for k, v in (("loss_cap_usd", cap), (bound_name, bound), ("capital_committed_usd", committed), ("capital_available_usd", available)) if v is None]
    if missing:
        return _term("RISK_LIMITS_ACCEPTABLE", UNKNOWN, [f"{k.upper()}_UNKNOWN" for k in missing])
    why = []
    if bound > cap:
        why.append(f"{bound_name.upper()}_{bound}_EXCEEDS_CAP_{cap}")
    if committed > available:
        why.append(f"CAPITAL_COMMITTED_{committed}_EXCEEDS_AVAILABLE_{available}")
    facts = {"loss_cap_usd": cap, "worst_case_loss_usd": worst if worst is not None else UNKNOWN_FACT, "capital_committed_usd": committed, "capital_available_usd": available}
    facts[bound_name] = bound
    return _term("RISK_LIMITS_ACCEPTABLE", FAIL if why else PASS, why, **facts)


def _authority(inputs: Mapping[str, Any]) -> Dict[str, Any]:
    """The capability and the grant must be VALID, the dedicated credentials present and the executor lease HELD. ``lease`` is HELD, LOST, or NOT_REQUIRED (the caller states that NO global lease is configured
    for this run, e.g. an owner's one-shot command guarded by the local account lock only); anything else, or absent, is UNKNOWN."""
    a = inputs.get("authority") if isinstance(inputs.get("authority"), Mapping) else {}
    lease = a.get("lease")
    flags = {"capability": a.get("capability") == "VALID" if a.get("capability") is not None else None,
             "grant": a.get("grant") == "VALID" if a.get("grant") is not None else None,
             "credentials": _truth(a.get("credentials_present")),
             "lease": True if lease in ("HELD", "NOT_REQUIRED") else (False if lease == "LOST" else None)}
    v, why = _all_true(flags)
    return _term("AUTHORITY_VALID", v, why, lease=lease)


def evaluate(inputs: Mapping[str, Any], *, profile: str = PROFIT_SEEKING) -> Dict[str, Any]:
    """Evaluate the eight terms. ``inputs`` keys (each optional; absent or malformed = UNKNOWN):
      data       {venue_identity, books_fresh, funding_observed: bool; scope: str}
      edge       the economic gate's verdict (``EdgeVerdict.as_dict()``): costs_bps, assumed_inputs, provenance, capital, net_edge_bps, session_class, reasons
      execution  {model_vs_measured_status, sessions_measured, mean_cost_surprise_bps, max_acceptable_surprise_bps}  (exec_quality.feedback_report's ``model_vs_measured``)
      evidence   {status: UNTESTED|DATA_INVALID|INSUFFICIENT|NEGATIVE|PROMISING|QUALIFIED, candidate_id, reasons}  (the candidate's evaluation)
      risk       {loss_cap_usd, worst_case_loss_usd (PROFIT_SEEKING), loss_limit_usd (MEASUREMENT: the limit the session runs under), capital_committed_usd, capital_available_usd, book_stops: [..]}
      authority  {capability: VALID|..., grant: VALID|..., credentials_present: bool, lease: HELD|LOST|NOT_REQUIRED}
    Never raises: a malformed input is that term's UNKNOWN."""
    if profile not in (PROFIT_SEEKING, MEASUREMENT):
        raise ValueError(f"unknown profile {profile!r}")
    src = inputs if isinstance(inputs, Mapping) else {}
    builders = (lambda: _data_valid(src), lambda: _costs_known(src, profile), lambda: _capital_known(src, profile), lambda: _execution_model_valid(src, profile), lambda: _edge_positive(src, profile),
                lambda: _statistical(src, profile), lambda: _risk(src, profile), lambda: _authority(src))
    terms: List[Dict[str, Any]] = []
    for name, build in zip(TERMS, builders):
        try:
            terms.append(build())
        except Exception as exc:  # noqa: BLE001 - a malformed input is an unestablished fact, never a pass and never a crash
            terms.append(_term(name, UNKNOWN, [f"INPUT_MALFORMED:{type(exc).__name__}"]))
    blocking = [t for t in terms if t["verdict"] in (FAIL, UNKNOWN)]
    if blocking:
        verdict = NO_TRADE
    else:
        verdict = ELIGIBLE if profile == PROFIT_SEEKING else ELIGIBLE_MEASUREMENT_ONLY
    body = {"schema": SCHEMA, "profile": profile, "verdict": verdict, "terms": terms,
            "failed": [t["term"] for t in terms if t["verdict"] == FAIL], "unknown": [t["term"] for t in terms if t["verdict"] == UNKNOWN], "waived": [t["term"] for t in terms if t["verdict"] == WAIVED],
            "claim": ("every term passed: eligible to trade for profit on this evidence" if verdict == ELIGIBLE else
                      "a bounded Testnet MEASUREMENT session only (fake money): NOT eligibility to trade for profit and not evidence of an edge" if verdict == ELIGIBLE_MEASUREMENT_ONLY else
                      "NO TRADE: at least one term is FAIL or UNKNOWN (UNKNOWN is never PASS)")}
    body["digest"] = hashlib.sha256(json.dumps({k: body[k] for k in ("profile", "verdict", "terms")}, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")).hexdigest()
    return body


def no_trade_reasons(result: Mapping[str, Any], limit: int = 6) -> List[str]:
    """``TERM:REASON`` strings for the terms that block trading, in term order (for a session's ``why`` list)."""
    out: List[str] = []
    for t in result.get("terms", []):
        if t.get("verdict") in (FAIL, UNKNOWN):
            out.append(f"{t['term']}:{(t.get('reasons') or [t['verdict']])[0]}")
    return out[:limit]
