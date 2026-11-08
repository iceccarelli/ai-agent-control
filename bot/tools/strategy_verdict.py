"""The economic verdict engine: a strategy candidate, the evidence ladder it must climb, and the ONE verdict it is allowed to hold.

    python3 tools/strategy_verdict.py --candidate cand.json --evidence evidence.jsonl [--json]

It is built to KILL strategies, not to promote them. The verdicts, weakest to strongest:

    UNTESTED              the spec is incomplete/invalid, or nothing has been tested yet
    INSUFFICIENT_DATA     evidence exists but cannot support a claim (too little data, no multiple-testing accounting, unlabelled or unknown costs)
    REJECTED              a stage FAILED or a declared rejection condition fired. Terminal: a rejected candidate is only re-opened as a NEW candidate (new id, new registration)
    PROMISING             historical + out-of-sample + cost + capacity all PASS. Offline evidence only; says nothing about the live market
    PROSPECTIVE           PROMISING and an owner-attested, registered prospective observation is under way or complete (not yet execution-validated)
    QUALIFIED             prospective + execution evidence that the OWNER ATTESTED to (HMAC under a key this repository never holds), on an ANCHORED registry
    OWNER_PROFIT_EVIDENCED  QUALIFIED and an owner-attested RECONCILED owner-account result with a positive net after every cost

The LADDER (each rung needs the one before it; evidence recorded for a later rung without the earlier one is ignored and reported, never counted):
    hypothesis -> historical_test -> oos_test -> cost_model -> capacity_test -> prospective_observation -> execution_validation -> reconciled_owner_result

A backtest return alone can never promote: ``historical_test`` PASS with nothing after it is INSUFFICIENT_DATA. Evidence must name its provenance; the offline stages must carry a
data hash; a PASS that does not state its multiple-testing accounting (number of variants tried, adjustment applied) does not count; a cost PASS with any UNKNOWN cost input or a
non-positive net edge does not count; a capacity PASS needs a stated capacity at least the declared minimum. Unknown is not zero; estimated is not observed; modelled is not realised.

WHAT THE TOP RUNGS PROVE, AND WHAT THEY DO NOT. Every check in this file other than the owner attestation is a keyless digest or a plausibility rule: anyone who can write the evidence
can also recompute it, so on their own they prove nothing about the world (red-team round 3 reached OWNER_PROFIT_EVIDENCED from forged evidence in under a second). The two top
rungs therefore additionally require an owner attestation (``owner_attest``: HMAC-SHA256 over the evidence digest + candidate_id + spec_digest, keyed by ``OWNER_ATTESTATION_KEY``,
read at verification time and never stored) and a registry the owner anchored outside the repository/volume. What QUALIFIED / OWNER_PROFIT_EVIDENCED mean is therefore
"the claims are internally consistent AND the holder of the owner key signed them". They are NOT an independent verification that any fill, fee, settlement or profit happened.
Whoever holds the key can sign a lie. With the key unset or the registry unanchored the verdict is capped below QUALIFIED.

What this is NOT: a statistics package (``statistical_power``/``carry_prospective`` do the testing and hand this engine their result), a profitability claim, or a trading gate. The
registered frozen BTC experiment is not changed by it: ``btc_carry_candidate()`` only DESCRIBES it so the engine can state where it stands (UNTESTED / INSUFFICIENT_DATA, never promoted
by a rate of return).
"""
from __future__ import annotations

import argparse
import dataclasses
import hashlib
import hmac
import json
import math
import os
import re
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

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
    bias = spec.get("bias_declarations") if isinstance(spec.get("bias_declarations"), dict) else {}
    if isinstance(rd, list):
        for i, item in enumerate(rd):
            if not isinstance(item, dict) or not item.get("name") or item.get("point_in_time") is not True:
                problems.append(f"REQUIRED_DATA[{i}]_NOT_DECLARED_POINT_IN_TIME")
            elif item.get("repo_has") is False:                            # a declaration cannot out-claim the data the spec itself says is missing
                keys = item.get("declaration_keys", [])
                for k in (keys if isinstance(keys, list) else []):
                    if bias.get(k) is True:
                        problems.append(f"BIAS_DECLARED_WITHOUT_DATA:{k}:REQUIRED_DATA[{i}]")
    elif _nonempty(rd):
        problems.append("REQUIRED_DATA_MUST_BE_A_LIST_OF_{name,point_in_time}")
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
    if isinstance(cap, dict) and not _finite_pos(cap.get("min_viable_capacity_usd")):
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
    candidate_id: Optional[str] = None      # which candidate this evidence is ABOUT
    spec_digest: Optional[str] = None       # the exact specification it was gathered against (a spec edited afterwards invalidates it)
    attachment: Optional[Mapping[str, Any]] = None   # reconciled_owner_result only: the sealed owner_import report the figure is read from (re-verified by counted())
    attestation: Optional[str] = None                # hex HMAC the OWNER made over ``evidence_digest(self)`` (observed stages); NEVER the key
    report_attestation: Optional[str] = None         # reconciled_owner_result only: the owner's HMAC over the attached report's ``report_sha256``

    def __post_init__(self) -> None:
        if self.attachment is not None:
            try:
                att = json.loads(json.dumps(dict(self.attachment), allow_nan=False))
            except (TypeError, ValueError) as exc:
                raise SpecError(f"evidence attachment is not plain finite JSON: {type(exc).__name__}") from exc
            object.__setattr__(self, "attachment", att)
        # Metrics are normalised ONCE into a plain JSON dict: a dict/Mapping subclass whose .get lies, or a NaN/inf/non-JSON value, cannot reach the verdict logic.
        try:
            plain = json.loads(json.dumps(dict(self.metrics), allow_nan=False))
        except (TypeError, ValueError) as exc:
            raise SpecError(f"evidence metrics are not plain finite JSON: {type(exc).__name__}") from exc
        if not isinstance(plain, dict):
            raise SpecError("evidence metrics must be an object")
        object.__setattr__(self, "metrics", plain)


def evidence_from_dict(d: Mapping[str, Any]) -> EvidenceItem:
    if not isinstance(d, Mapping) or d.get("stage") not in LADDER or d.get("result") not in (PASS, FAIL, INCONCLUSIVE):
        raise SpecError(f"not an evidence item: {d!r}")
    return EvidenceItem(stage=d["stage"], result=d["result"], provenance=str(d.get("provenance") or ""), metrics=dict(d.get("metrics") or {}),
                        data_sha256=d.get("data_sha256"), recorded_ms=int(d.get("recorded_ms") or 0), status=d.get("status"),
                        candidate_id=d.get("candidate_id"), spec_digest=d.get("spec_digest"), attachment=d.get("attachment"),
                        attestation=d.get("attestation"), report_attestation=d.get("report_attestation"))


def evidence_to_dict(item: EvidenceItem) -> Dict[str, Any]:
    return {"stage": item.stage, "result": item.result, "provenance": item.provenance, "metrics": dict(item.metrics), "data_sha256": item.data_sha256, "recorded_ms": item.recorded_ms,
            "status": item.status, "candidate_id": item.candidate_id, "spec_digest": item.spec_digest, "attachment": item.attachment, "attestation": item.attestation,
            "report_attestation": item.report_attestation}


# ---------------------------------------------------------------- owner attestation
# A keyless digest proves nothing (anyone can recompute it). The attestation is an HMAC under a secret the repository never contains: it proves only that the KEY HOLDER signed
# this evidence for this candidate and spec. It is not independent verification of the world, and a key holder can sign a falsehood.

def env_key_provider() -> Optional[str]:
    """The default key provider: the owner's secret from the environment at verification time. Returns None when unset. The value is never logged or stored."""
    v = os.environ.get(OWNER_ATTESTATION_KEY_ENV)
    return v if isinstance(v, str) and v else None


def _key_bytes(key: Any) -> Optional[bytes]:
    if isinstance(key, str):
        key = key.encode("utf-8")
    if not isinstance(key, (bytes, bytearray)) or len(key) < MIN_ATTESTATION_KEY_CHARS or len(set(bytes(key))) < MIN_ATTESTATION_KEY_DISTINCT:
        return None                                                    # an unset, short or low-entropy-looking key cannot attest anything
    return bytes(key)


def owner_attest(digest: str, candidate_id: str, spec_digest: str, key: Any) -> str:
    """The owner's attestation: hex HMAC-SHA256 over (digest, candidate_id, spec_digest) under ``key``. For the OWNER'S OWN tooling (``strategy_verdict.py attest``): the key is
    read by the caller from somewhere outside this repository. Proves 'the key holder signed these claims', nothing more. Raises SpecError for a missing/short key."""
    kb = _key_bytes(key)
    if kb is None:
        raise SpecError(f"an attestation key of at least {MIN_ATTESTATION_KEY_CHARS} characters with at least {MIN_ATTESTATION_KEY_DISTINCT} distinct characters is required")
    msg = json.dumps(["owner-attest/1", str(digest), str(candidate_id), str(spec_digest)], separators=(",", ":")).encode("utf-8")
    return hmac.new(kb, msg, hashlib.sha256).hexdigest()


def evidence_digest(item: EvidenceItem) -> str:
    """Canonical SHA-256 of what an evidence item CLAIMS (everything but the attestations; an attached report enters as its report_sha256, attested separately)."""
    att = item.attachment
    body = {"stage": item.stage, "result": item.result, "provenance": item.provenance, "metrics": item.metrics, "data_sha256": item.data_sha256, "status": item.status,
            "candidate_id": item.candidate_id, "spec_digest": item.spec_digest, "report_sha256": att.get("report_sha256") if isinstance(att, Mapping) else None}
    if item.stage in OBSERVED_STAGES:
        body["recorded_ms"] = item.recorded_ms                 # recorded_ms gates timing (prospective: recorded >= end), so it is part of what the owner signs
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()


def attest_item(item: EvidenceItem, key: Any) -> EvidenceItem:
    """A copy of ``item`` carrying the owner's attestation(s). An owner-result item also gets ``report_attestation`` over its attached report's hash."""
    cid, sd = str(item.candidate_id), str(item.spec_digest)
    rep_att = None
    if isinstance(item.attachment, Mapping) and isinstance(item.attachment.get("report_sha256"), str):
        rep_att = owner_attest(item.attachment["report_sha256"], cid, sd, key)
    return dataclasses.replace(item, attestation=owner_attest(evidence_digest(item), cid, sd, key), report_attestation=rep_att)


def _check_attestation(att: Any, digest: str, spec: Mapping[str, Any], key: Optional[bytes]) -> bool:
    if key is None or not isinstance(att, str) or not att or not att.isascii():
        return False
    want = owner_attest(digest, str(spec.get("candidate_id")), spec_digest(spec), key)
    return hmac.compare_digest(want.encode("ascii"), att.encode("ascii"))


def _attestation_problem(item: EvidenceItem, spec: Mapping[str, Any], key_provider: Optional[Callable[[], Any]]) -> Optional[str]:
    """None if the owner's attestation verifies for this item (and, for an owner result, for its attached report); else why not. The key is fetched here, used, and dropped."""
    try:
        key = _key_bytes((key_provider or env_key_provider)())
    except Exception:                                                    # noqa: BLE001 - a failing provider is an absent key
        key = None
    if key is None:
        return "OWNER_ATTESTATION_UNVERIFIABLE(no owner key is available to this process)"
    if not isinstance(item.attestation, str) or not item.attestation:
        return "OWNER_ATTESTATION_UNVERIFIABLE(evidence carries no owner attestation)"
    if not _check_attestation(item.attestation, evidence_digest(item), spec, key):
        return "OWNER_ATTESTATION_UNVERIFIABLE(attestation does not verify)"
    if item.stage == "reconciled_owner_result":
        rep = item.attachment
        if not isinstance(rep, Mapping) or not _check_attestation(item.report_attestation, str(rep.get("report_sha256")), spec, key):
            return "OWNER_ATTESTATION_UNVERIFIABLE(owner report is not attested)"
    return None


def _is_hex64(x: Any) -> bool:
    return isinstance(x, str) and len(x) == 64 and all(c in "0123456789abcdef" for c in x)


WEAK_PROVENANCE = frozenset({"unknown", "assumed", "modelled", "modeled", "estimated", "n/a", "none", "todo", "tbd", "x"})
MAX_PLAUSIBLE_EDGE_BPS = 500.0          # a per-round-trip NET edge above this after real costs is a unit slip or a forgery until proven otherwise
OBSERVED_STAGES = ("prospective_observation", "execution_validation", "reconciled_owner_result")
#: Floors for observed evidence. A candidate's registered protocol can RAISE them, never lower them.
DAY_MS = 86_400_000
MIN_PROSPECTIVE_DURATION_MS = 30 * DAY_MS      # a registered protocol may ask for longer, never shorter (red-team 3: min_duration_ms=1 was accepted)
DEFAULT_FUNDING_INTERVAL_MS = 8 * 3_600_000
MIN_FUNDING_INTERVAL_MS = 3_600_000
SETTLEMENT_FIT_TOLERANCE = 1.05                # observed_settlements * interval may exceed the window by this factor (+ one interval) and no more
MIN_PROSPECTIVE_SETTLEMENTS = 30
MIN_PROSPECTIVE_COVERAGE = 0.90
OWNER_ATTESTATION_KEY_ENV = "OWNER_ATTESTATION_KEY"
MIN_ATTESTATION_KEY_CHARS = 32
MIN_ATTESTATION_KEY_DISTINCT = 12       # 'aaaaaaaaaaaaaaaa' (red-team 4) is long enough to look like a key and is not one
#: The cost-chain declarations stored in the registry beside the registered spec digest (so a drift can be NAMED, not only detected).
REGISTERED_DECLARATION_KEYS = ("fee_model", "slippage_model", "funding_model", "capacity_model", "execution_model", "holding_period", "eligibility_rules", "OOS_protocol", "parameter_grid")
MIN_EXECUTION_FILLS = 20
#: Statuses an owner-import economic layer may carry for its figure to be read at all (STALE / UNAVAILABLE / DISPUTED / INCOMPLETE are never a figure).
OWNER_FIGURE_OK_STATUSES = ("ESTIMATED", "PROVISIONAL", "RECONCILED", "VERIFIED")


def _finite_pos(x: Any) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) and x > 0


def _registry_entry(registry: Optional[Mapping[str, Any]], cid: str) -> Optional[Mapping[str, Any]]:
    if registry is None:
        return None
    for t in registry.get("trials", []):
        if t.get("trial_id") == cid:
            return t
    return None


def _verified_entry(registry: Optional[Mapping[str, Any]], spec: Mapping[str, Any]) -> Optional[Mapping[str, Any]]:
    """The candidate's registry entry, but only from a registry that passes its own integrity check (else nothing can be bound to it)."""
    entry = _registry_entry(registry, str(spec.get("candidate_id")))
    if registry is None or entry is None:
        return None
    import hypothesis_registry as hr
    return None if hr.verify(registry) else entry


def _registered_spec_problem(registry: Optional[Mapping[str, Any]], spec: Mapping[str, Any]) -> Optional[str]:
    """None if the spec in hand is the one that was registered; ``SPEC_DIFFERS_FROM_REGISTERED(...)`` if the registered digest differs (names the declarations that drifted);
    ``REGISTERED_SPEC_DIGEST_MISSING`` for a registration made before digests were stored (unbound); None when there is no entry to compare with."""
    entry = _registry_entry(registry, str(spec.get("candidate_id")))
    if entry is None:
        return None
    want = entry.get("spec_digest")
    if not _is_hex64(want):
        return "REGISTERED_SPEC_DIGEST_MISSING"
    if want != spec_digest(spec):
        decl = entry.get("spec_declarations") if isinstance(entry.get("spec_declarations"), Mapping) else {}
        drift = [k for k in REGISTERED_DECLARATION_KEYS if k in decl and decl[k] != spec.get(k)]
        return "SPEC_DIFFERS_FROM_REGISTERED(" + (",".join(drift) if drift else "other fields") + "); register a NEW candidate id/version for a changed spec"
    return None


def _anchored_variants(registry: Mapping[str, Any], entry: Mapping[str, Any]) -> int:
    """The least number of variants a registered candidate can honestly have tried: every registered trial in its multiple-testing family, and every cell of its registered grid."""
    fam = entry.get("multiple_testing_family")
    n_family = sum(1 for t in registry.get("trials", []) if fam is not None and t.get("multiple_testing_family") == fam)
    grid = entry.get("parameter_grid")
    n_grid = 1
    if isinstance(grid, Mapping):
        for v in grid.values():
            if isinstance(v, list) and v:
                n_grid *= len(v)
    return max(1, n_family, n_grid)


def _implied_variants(m: Mapping[str, Any]) -> int:
    """The number of variants the evidence's OWN metrics show were tried (named variants listed, or an explicit attempted count)."""
    out = 0
    for k in ("variant_names", "variant_p_values", "variants_tested_names"):
        v = m.get(k)
        if isinstance(v, list):
            out = max(out, len(v))
    for k in ("n_variants_attempted", "n_variants_registered", "n_variants_carried"):
        v = m.get(k)
        if isinstance(v, int) and not isinstance(v, bool):
            out = max(out, v)
    return out


def _prospective_floors(entry: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    """The machine-readable prospective window the candidate REGISTERED: {min_duration_ms, min_settlements, min_coverage}, or None when the registered protocol is free text."""
    pp = entry.get("prospective_period")
    dur: Optional[int] = None
    ms, mc = MIN_PROSPECTIVE_SETTLEMENTS, MIN_PROSPECTIVE_COVERAGE
    interval = DEFAULT_FUNDING_INTERVAL_MS
    if isinstance(pp, Mapping):
        d = pp.get("min_duration_ms")
        if isinstance(d, int) and not isinstance(d, bool) and d > 0:
            dur = max(d, MIN_PROSPECTIVE_DURATION_MS)
        fi = pp.get("funding_interval_ms")
        if isinstance(fi, int) and not isinstance(fi, bool) and fi >= MIN_FUNDING_INTERVAL_MS:
            interval = fi
        v = pp.get("min_settlements")
        if isinstance(v, int) and not isinstance(v, bool):
            ms = max(ms, v)
        v = pp.get("min_coverage")
        if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and v <= 1:
            mc = max(mc, float(v))
    elif isinstance(pp, str):
        mt = re.match(r"^\s*(\d{10,})\.\.(\d{10,})", pp)
        if mt and int(mt.group(2)) > int(mt.group(1)):
            dur = max(int(mt.group(2)) - int(mt.group(1)), MIN_PROSPECTIVE_DURATION_MS)
    return None if dur is None else {"min_duration_ms": dur, "min_settlements": ms, "min_coverage": mc, "funding_interval_ms": interval}


def _candidate_binding(item: EvidenceItem, spec: Mapping[str, Any]) -> Optional[str]:
    if item.candidate_id != spec.get("candidate_id"):
        return "EVIDENCE_NOT_ABOUT_THIS_CANDIDATE"
    if item.spec_digest != spec_digest(spec):
        return "EVIDENCE_GATHERED_AGAINST_A_DIFFERENT_SPEC"
    return None


def _registry_binding(item: EvidenceItem, spec: Mapping[str, Any], registry: Optional[Mapping[str, Any]], now: Optional[int] = None) -> Optional[str]:
    """For an OBSERVED stage: a registry that verifies, holds the candidate, and PRE-DATES the observation. The one place this is decided: counted() and the
    pending-prospective check both use it, and neither reads 'registered_before_observation' from metrics."""
    entry = _registry_entry(registry, str(spec.get("candidate_id")))
    if registry is None or entry is None:
        return "NEEDS_A_REGISTERED_CANDIDATE(registry not supplied or candidate not in it)"
    import hypothesis_registry as hr
    if hr.verify(registry):
        return "REGISTRY_FAILS_ITS_INTEGRITY_CHECK"
    drift = _registered_spec_problem(registry, spec)
    if drift:
        return drift
    reg_ms = _utc_ms(entry.get("registered_utc"))
    start = item.metrics.get("observation_started_ms")
    if reg_ms is None or isinstance(start, bool) or not isinstance(start, int) or start <= reg_ms:
        return "OBSERVATION_DID_NOT_START_AFTER_REGISTRATION"
    if now is not None and start > now:
        return "OBSERVATION_STARTS_IN_THE_FUTURE"
    return None


def _num(x: Any) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def _int(x: Any) -> bool:
    return isinstance(x, int) and not isinstance(x, bool)


def _verify_owner_report(report: Any) -> List[str]:
    import supabase_sync as SS
    return SS.verify_import_report(report)


def owner_verdict_figure(report: Mapping[str, Any]) -> Tuple[Optional[float], Optional[str]]:
    """The ONE figure an owner result is allowed to carry: ``layers.economic_total.net_economic_pnl_usd`` (realised cash + owned-BTC economic P&L + change in perp unrealised),
    never ``headline_realised_cash_usd``. (value, None) or (None, why): a missing layer, a STALE/UNAVAILABLE/DISPUTED/INCOMPLETE layer, or a None figure yields no figure."""
    layers = report.get("layers") if isinstance(report.get("layers"), Mapping) else {}
    econ = layers.get("economic_total") if isinstance(layers.get("economic_total"), Mapping) else None
    if econ is None:
        return None, "ECONOMIC_TOTAL_LAYER_MISSING"
    if econ.get("status") not in OWNER_FIGURE_OK_STATUSES:
        return None, f"ECONOMIC_TOTAL_{econ.get('status')}"
    net = econ.get("net_economic_pnl_usd")
    if not _num(net):
        return None, "ECONOMIC_TOTAL_NONE"
    return float(net), None


def evidence_from_import_report(report: Mapping[str, Any], spec: Mapping[str, Any]) -> EvidenceItem:
    """The ONLY way to build ``reconciled_owner_result`` evidence. The report must be a valid, sealed ``owner_import`` report (``report_sha256`` and the lineage digests are RECOMPUTED
    by ``supabase_sync.verify_import_report``); the figure, status, hash and window start are READ FROM THE REPORT, never supplied by the caller. The figure is the ECONOMIC total.
    PASS only if the report is RECONCILED and the economic total is a positive number on a usable layer; a RECONCILED report with a non-positive total is a FAIL (a real result);
    anything else is INCONCLUSIVE. The report is attached so ``counted`` re-verifies it every time.

    LIMIT: the report's seal and lineage digests are KEYLESS, so a forger who can write a report can reseal it; ``verify_import_report`` shows internal consistency, not that the
    owner's account did what the report says. The owner result only counts once the owner ATTESTS the item and the report (``attest_item``), and that attestation proves only
    that the key holder signed these claims."""
    problems = _verify_owner_report(report)
    if problems:
        raise SpecError("the owner import report fails verification: " + ", ".join(problems[:4]))
    fig, why = owner_verdict_figure(report)
    win = (report.get("ledger") or {}).get("window") or {}
    start = win.get("opening_ts_ms") if isinstance(win, Mapping) else None
    status = report["status"]
    if status != "RECONCILED" or fig is None:
        result = INCONCLUSIVE
    else:
        result = PASS if fig > 0 else FAIL
    metrics = {"net_usd_after_all_costs": fig, "owner_figure_basis": "economic_total", "import_report_sha256": report["report_sha256"], "import_status": status,
               "observation_started_ms": start if _int(start) else None}
    return EvidenceItem(stage="reconciled_owner_result", result=result, provenance=f"owner_import report {report.get('import_id')}", metrics=metrics, status=status,
                        candidate_id=spec.get("candidate_id"), spec_digest=spec_digest(spec), attachment=report)


COST_SPEC_MODELS = ("fee_model", "slippage_model", "funding_model")


def cost_chain_digest(spec: Mapping[str, Any]) -> Optional[str]:
    """``economic_model.cost_chain_digest(spec)`` (None if economic_model is unavailable). A cost_model / capacity_test PASS must carry this as ``chain_digest``: it binds the
    claim to the labels the spec actually states. It is a KEYLESS binding (anyone can recompute it): it stops a cost claim made against different inputs, not a forgery."""
    try:
        import economic_model as EM
        return EM.cost_chain_digest(spec)
    except Exception:                                                    # noqa: BLE001
        return None


def _all_cost_inputs_assumed(spec: Mapping[str, Any]) -> bool:
    labels = []
    for model, comps in COST_COMPONENTS.items():
        m = spec.get(model) if isinstance(spec.get(model), Mapping) else {}
        for c in comps:
            e = m.get(c)
            labels.append(e.get("provenance") if isinstance(e, Mapping) else None)
    return bool(labels) and all(x == "assumed" for x in labels)


def _funding_type(spec: Mapping[str, Any]) -> bool:
    """A candidate whose edge is (or is priced in) funding: it names funding in its data, family / id / hypothesis, or models a per-settlement funding rate.
    Such evidence must STATE ``universe_funding_normalisation_known is True`` (absence is not knowledge)."""
    if not isinstance(spec, Mapping):
        return True                                                      # unknown shape: the stricter rule
    text = " ".join(str(spec.get(k, "")) for k in ("candidate_id", "family", "hypothesis")).lower()
    names = " ".join(str(d.get("name", "")) for d in (spec.get("required_data") or []) if isinstance(d, Mapping)).lower()
    fm = spec.get("funding_model")
    return "funding" in text or "funding" in names or (isinstance(fm, Mapping) and "expected_bps_per_settlement" in fm)


def counted(item: EvidenceItem, spec: Mapping[str, Any], registry: Optional[Mapping[str, Any]] = None, *, now_ms: Optional[int] = None,
            key_provider: Optional[Callable[[], Any]] = None) -> Tuple[bool, Optional[str]]:
    """Does this item count as a PASS/FAIL at its stage? (ok, why_not). A FAIL always counts (killing is easy); a PASS must earn it: it must be about THIS candidate and THIS spec,
    carry real provenance, and (for the observed stages) be backed by a registration that verifies and PRE-DATES the observation. ``now_ms`` (default: the clock) bounds the end of
    any observation: an observation cannot end in the future. An observed-stage PASS additionally needs the owner's attestation (``key_provider``, default the
    OWNER_ATTESTATION_KEY environment variable); without a verifiable key it is NOT counted (OWNER_ATTESTATION_UNVERIFIABLE)."""
    if item.result == INCONCLUSIVE:
        return False, "INCONCLUSIVE"
    if item.result == FAIL:
        # killing is easy, but only for THIS candidate and THIS spec (and the registered one): evidence about another candidate/spec is not a kill of this one
        bad = _candidate_binding(item, spec)
        drift = _registered_spec_problem(registry, spec)
        if bad or (drift and drift.startswith("SPEC_DIFFERS")):
            return False, bad or drift
        return True, None
    now = int(time.time() * 1000) if now_ms is None else int(now_ms)
    prov = item.provenance.strip()
    if len(prov) < 6 or prov.lower() in WEAK_PROVENANCE:
        return False, "PASS_WITHOUT_REAL_PROVENANCE"
    bad = _candidate_binding(item, spec)
    if bad:
        return False, bad
    m = item.metrics
    s = item.stage
    drift = _registered_spec_problem(registry, spec)
    if drift and drift.startswith("SPEC_DIFFERS"):
        return False, drift
    entry = _verified_entry(registry, spec)
    if s in OFFLINE_STAGES and (not _is_hex64(item.data_sha256) or len(set(item.data_sha256)) < 8):
        return False, "OFFLINE_PASS_WITHOUT_REAL_DATA_HASH"
    if s in OFFLINE_STAGES and entry is not None:
        registered = entry.get("input_data_sha256")
        if not isinstance(registered, Mapping) or item.data_sha256 not in {str(v) for v in registered.values()}:
            return False, "OFFLINE_DATA_HASH_NOT_IN_REGISTERED_INPUTS"
    if s in ("historical_test", "oos_test"):
        n = m.get("independent_observations")
        if isinstance(n, bool) or not isinstance(n, int) or n < MIN_OBSERVATIONS[s]:
            return False, f"TOO_FEW_INDEPENDENT_OBSERVATIONS(need {MIN_OBSERVATIONS[s]})"
        nv = m.get("n_variants_tested")
        if isinstance(nv, bool) or not isinstance(nv, int) or nv < 1 or m.get("multiple_testing_adjusted") is not True:
            return False, "NO_MULTIPLE_TESTING_ACCOUNTING"
        if nv < _implied_variants(m):
            return False, "N_VARIANTS_BELOW_WHAT_THE_EVIDENCE_IMPLIES"
        if entry is not None and nv < _anchored_variants(registry, entry):
            return False, "N_VARIANTS_BELOW_REGISTERED_COUNT"
        if m.get("uses_future_information") is not False:
            return False, "LOOK_AHEAD_NOT_RULED_OUT"
        if m.get("universe_point_in_time") is not True:
            return False, "UNIVERSE_NOT_POINT_IN_TIME"
        if m.get("universe_pit_basis") != "RAW_RESPONSES_VERIFIED":
            return False, "UNIVERSE_PIT_NOT_RAW_VERIFIED"           # a boolean is a claim: point in time must rest on raw captures the harness re-verified
        if _funding_type(spec) and m.get("universe_funding_normalisation_known") is not True:
            return False, "FUNDING_INTERVAL_NORMALISATION_UNKNOWN"   # per-symbol fundingInterval not recorded (or the flag ABSENT / None / truthy-but-not-True): per-print funding is not comparable
    if s == "cost_model":
        unk = m.get("unknown_cost_inputs")
        if type(unk) is not int or unk != 0:                           # exactly the integer 0: not False, not 0.0, not None
            return False, "UNKNOWN_COST_INPUTS"
        edge = m.get("net_edge_bps_after_costs")
        if not _finite_pos(edge):
            return False, "NET_EDGE_NOT_POSITIVE_AFTER_COSTS"
        if edge > MAX_PLAUSIBLE_EDGE_BPS:
            return False, "NET_EDGE_IMPLAUSIBLY_LARGE"
        want_chain = cost_chain_digest(spec)
        if want_chain is None or m.get("chain_digest") != want_chain:
            return False, "COST_CHAIN_DIGEST_NOT_RECOMPUTED_FROM_THE_SPEC"
        if _all_cost_inputs_assumed(spec):
            return False, "ALL_COST_INPUTS_ASSUMED"                       # an assumed input can never PASS, whatever number the metric states
        if m.get("assumed_names") not in (None, []):
            return False, "ASSUMED_COST_INPUTS_CANNOT_PASS"
    if s == "capacity_test":
        cap, need = m.get("capacity_usd"), (spec.get("capacity_model") or {}).get("min_viable_capacity_usd")
        if not _finite_pos(cap) or not _finite_pos(need) or cap < need:
            return False, "CAPACITY_BELOW_MINIMUM_VIABLE"
        if m.get("min_viable_capacity_usd") != need:
            return False, "CAPACITY_EVIDENCE_NOT_TIED_TO_THE_SPECS_MINIMUM"
        want_chain = cost_chain_digest(spec)
        if want_chain is None or m.get("chain_digest") != want_chain:
            return False, "COST_CHAIN_DIGEST_NOT_RECOMPUTED_FROM_THE_SPEC"
    if s in OBSERVED_STAGES:
        bad = _registry_binding(item, spec, registry, now)
        if bad:
            return False, bad
        entry = _registry_entry(registry, str(spec.get("candidate_id")))
    if s == "prospective_observation":
        if m.get("complete") is not True:
            return False, "PROSPECTIVE_NOT_COMPLETE"
        win = _prospective_floors(entry or {})
        if win is None:
            return False, "PROSPECTIVE_WINDOW_NOT_MACHINE_READABLE"
        start, end = m.get("observation_started_ms"), m.get("observation_ended_ms")
        if not _int(start) or not _int(end) or end <= start or end > now or start > now:
            return False, "OBSERVATION_END_INVALID_OR_IN_THE_FUTURE"
        if end - start < win["min_duration_ms"]:
            return False, "PROSPECTIVE_DURATION_SHORTER_THAN_REGISTERED_WINDOW"
        n_obs = m.get("observed_settlements")
        if not _int(n_obs) or n_obs < win["min_settlements"]:
            return False, f"TOO_FEW_OBSERVED_SETTLEMENTS(need {win['min_settlements']})"
        if n_obs * win["funding_interval_ms"] > (end - start) * SETTLEMENT_FIT_TOLERANCE + win["funding_interval_ms"]:
            return False, "OBSERVED_SETTLEMENTS_CANNOT_FIT_IN_THE_OBSERVATION_WINDOW"
        if not _int(item.recorded_ms) or item.recorded_ms < end:
            return False, "EVIDENCE_RECORDED_BEFORE_THE_OBSERVATION_IT_CLAIMS_ENDED"
        cov = m.get("coverage")
        if not _num(cov) or not (win["min_coverage"] <= cov <= 1.0):
            return False, f"COVERAGE_BELOW_FLOOR(need {win['min_coverage']})"
    if s == "execution_validation":
        if m.get("venue") not in ("testnet", "mainnet") or m.get("fills_measured") is not True or m.get("fees_measured") is not True:
            return False, "EXECUTION_NOT_MEASURED_AT_A_VENUE"
        h = m.get("execution_log_sha256")
        if (not _int(m.get("fills_count")) or m["fills_count"] < MIN_EXECUTION_FILLS or not _int(m.get("fee_observations_count")) or m["fee_observations_count"] < 1
                or not _is_hex64(h) or len(set(h)) < 8 or m.get("candidate_id") != spec.get("candidate_id") or m.get("spec_digest") != spec_digest(spec)):
            return False, "EXECUTION_NOT_MEASURED_COUNTS_AND_LOG_HASH_NOT_TIED_TO_THE_CANDIDATE"
    if s == "reconciled_owner_result":
        if item.status != "RECONCILED":
            return False, "OWNER_RESULT_NOT_RECONCILED_OR_NOT_POSITIVE"
        rep = item.attachment
        if not isinstance(rep, Mapping):
            return False, "OWNER_RESULT_HAS_NO_ATTACHED_IMPORT_REPORT"
        problems = _verify_owner_report(rep)
        if problems:
            return False, "OWNER_REPORT_FAILS_VERIFICATION:" + ",".join(problems[:3])
        if rep.get("status") != "RECONCILED":
            return False, "OWNER_RESULT_NOT_RECONCILED_OR_NOT_POSITIVE"
        if m.get("import_report_sha256") != rep.get("report_sha256"):
            return False, "OWNER_HASH_NOT_RECOMPUTED_FROM_THE_ATTACHED_REPORT"
        fig, why = owner_verdict_figure(rep)
        if fig is None or fig <= 0:
            return False, "OWNER_ECONOMIC_TOTAL_NOT_POSITIVE" + (f"({why})" if why else "")
        if m.get("net_usd_after_all_costs") != fig:
            return False, "OWNER_FIGURE_NOT_THE_REPORTS_ECONOMIC_TOTAL"
    if s in OBSERVED_STAGES:
        why_att = _attestation_problem(item, spec, key_provider)
        if why_att:
            return False, why_att
    return True, None


def _utc_ms(text: Any) -> Optional[int]:
    import datetime
    try:
        return int(datetime.datetime.strptime(str(text), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc).timestamp() * 1000)
    except ValueError:
        return None


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


def evaluate(spec: Mapping[str, Any], evidence: Sequence[EvidenceItem], *, registry: Optional[Mapping[str, Any]] = None, now_ms: Optional[int] = None,
             anchor: Optional[str] = None, key_provider: Optional[Callable[[], Any]] = None) -> VerdictResult:
    """The verdict for a candidate given ALL its evidence. Deterministic; the same inputs give the same answer; later evidence never rescues an earlier FAIL.

    Caps (each reported in ``reasons``): nothing reaches PROMISING without a verifying registry that holds the candidate (so evidence-only runs stop at INSUFFICIENT_DATA);
    an observed-stage PASS needs the owner's attestation (``key_provider``, default env OWNER_ATTESTATION_KEY) or it is not counted; QUALIFIED and OWNER_PROFIT_EVIDENCED need a
    registry the owner ANCHORED (``anchor`` = ``hypothesis_registry.anchor_digest`` recorded outside the repository/volume), else the verdict stays PROSPECTIVE
    (REGISTRY_NOT_ANCHORED). An ``anchor`` that does not match the registry raises SpecError."""
    anchored = False
    if anchor is not None:
        if registry is None:
            raise SpecError("an anchor was supplied without a registry to check it against")
        import hypothesis_registry as hr
        bad_anchor = hr.verify(registry, anchor=anchor)
        if bad_anchor:
            raise SpecError("the registry does not match the owner-held anchor: " + "; ".join(bad_anchor[:3]))
        anchored = True
    problems = spec_problems(spec)
    stages: Dict[str, str] = {s: "MISSING" for s in LADDER}
    ignored: List[str] = []
    reasons: List[str] = []
    if problems:
        return VerdictResult(UNTESTED, ["the candidate specification is invalid or incomplete"] + problems[:10], "hypothesis", stages, ignored, problems)
    stages["hypothesis"] = PASS                                                  # a valid, complete specification IS the registered hypothesis
    drift = _registered_spec_problem(registry, spec)
    if drift and drift.startswith("SPEC_DIFFERS"):
        # The spec in hand is not the one that was registered (a cost label flipped, a floor loosened...): nothing gathered or signed can be bound to it.
        return VerdictResult(INSUFFICIENT_DATA, [drift], "registration", stages, ignored, problems)
    if drift:
        reasons.append(drift + ": the registration holds no spec digest, so the spec cannot be bound to it and observed evidence is not counted (verdict stays below QUALIFIED)")
    by_stage: Dict[str, List[EvidenceItem]] = {s: [] for s in LADDER}
    for e in evidence:
        by_stage[e.stage].append(e)
    # declared rejection conditions that fired are reported as FAIL evidence at a stage named 'rejection' (any stage's metrics may carry them)
    for e in evidence:
        fired = e.metrics.get("rejection_conditions_fired")
        if fired not in (None, [], "", 0, False, {}):                    # ANY non-empty claim kills (a string or a count is not a way to hide one)
            reasons.append("REJECTION_CONDITION_FIRED:" + (("|".join(str(x) for x in fired)) if isinstance(fired, list) else str(fired)))
        if e.stage == "hypothesis" and e.result == FAIL:
            reasons.append("REJECTION_CONDITION_FIRED:hypothesis FAILED")
    for stage in LADDER[1:]:
        verdicts = []
        notes = []
        for e in by_stage[stage]:
            ok, why = counted(e, spec, registry, now_ms=now_ms, key_provider=key_provider)
            if ok:
                verdicts.append(e.result)
            else:
                notes.append(why or "NOT_COUNTED")
                if why and why.startswith("OWNER_ATTESTATION_UNVERIFIABLE") and not any(r.startswith("OWNER_ATTESTATION_UNVERIFIABLE") for r in reasons):
                    reasons.append("OWNER_ATTESTATION_UNVERIFIABLE: observed-stage evidence is not counted without a verifiable owner attestation; the verdict is capped below the observed rungs")
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
    if _verified_entry(registry, spec) is None:
        # PROMISING is a claim about registered, hash-bound offline evidence. Without a verifying registry holding the candidate the data hashes and variant counts are
        # unverified and nothing here can be bound to a pre-registration, so the evidence can never exceed INSUFFICIENT_DATA (this is what a CLI --evidence-only run is).
        return VerdictResult(INSUFFICIENT_DATA, ["NO_VERIFYING_REGISTRY_HOLDS_THIS_CANDIDATE: offline evidence is complete but unbound (data hashes and variant counts unverified); "
                                                 "without a registry the verdict cannot exceed INSUFFICIENT_DATA"] + reasons, "registration", stages, ignored, problems)
    if "prospective_observation" not in have:
        # 'pending' needs the same candidate / spec-digest / registered-before-start binding as counted(), a start that is not in the future, and the owner's attestation of the
        # running-observation marker; the metric's own 'registered_before_observation' claim is never read.
        now = int(time.time() * 1000) if now_ms is None else int(now_ms)
        pend = any(e.stage == "prospective_observation" and e.result == INCONCLUSIVE and e.metrics.get("complete") is False and _candidate_binding(e, spec) is None
                   and _registry_binding(e, spec, registry, now) is None and _attestation_problem(e, spec, key_provider) is None for e in evidence)
        return VerdictResult(PROSPECTIVE if pend else PROMISING,
                             ["offline evidence is complete; the live market has not confirmed anything" if not pend else "an owner-attested, registered prospective observation is under way"]
                             + reasons, "prospective_observation", stages, ignored, problems)
    if "execution_validation" not in have:
        return VerdictResult(PROSPECTIVE, ["prospective observation passed; execution at a venue is not yet validated"] + reasons, "execution_validation", stages, ignored, problems)
    if not anchored:
        return VerdictResult(PROSPECTIVE, ["REGISTRY_NOT_ANCHORED: the registry seal is keyless (anyone can delete a FAIL and reseal), so QUALIFIED needs the owner-held head digest "
                                           "(hypothesis_registry.anchor_digest, recorded outside the repository/volume) supplied as the anchor"] + reasons, "registry_anchor", stages, ignored, problems)
    if "reconciled_owner_result" not in have:
        return VerdictResult(QUALIFIED, ["prospective + execution evidence in hand, signed by the owner key and on an anchored registry (consistent claims the key holder signed; "
                                         "not independent verification); no reconciled owner result yet"] + reasons, "reconciled_owner_result", stages, ignored, problems)
    return VerdictResult(OWNER_PROFIT_EVIDENCED, ["reconciled owner result with a positive net after all costs, signed by the owner key (consistent claims the key holder signed; "
                                                  "not independent verification)"] + reasons, None, stages, ignored, problems)


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
        costs={k: spec[k] for k in ("fee_model", "funding_model", "slippage_model")}, spec_digest=spec_digest(spec),
        spec_declarations={k: spec[k] for k in REGISTERED_DECLARATION_KEYS if k in spec}, statistical_design=spec["OOS_protocol"],
        train_test=spec["OOS_protocol"]["train_test_boundary"], multiple_testing_family=str(spec.get("multiple_testing_family") or spec["candidate_id"]),
        prospective_period=spec["prospective_protocol"], source="tools/strategy_verdict.py")


def record_evidence(registry: Dict[str, Any], candidate_id: str, item: EvidenceItem) -> None:
    import hypothesis_registry as hr
    hr.add_evidence(registry, candidate_id, STAGE_TO_REGISTRY_LEVEL[item.stage], result=f"{item.stage}:{item.result}", provenance=item.provenance or "none",
                    details={"metrics": dict(item.metrics), "data_sha256": item.data_sha256, "status": item.status, "candidate_id": item.candidate_id or candidate_id,
                             "spec_digest": item.spec_digest, "attachment": item.attachment, "attestation": item.attestation, "report_attestation": item.report_attestation,
                             "recorded_ms": item.recorded_ms})


def evidence_from_registry(registry: Mapping[str, Any], candidate_id: str) -> List[EvidenceItem]:
    """EVERY evidence item recorded for the candidate in the registry (written by ``record_evidence``), with ``recorded_ms`` taken from the registry's own timestamp.
    Dropping a FAIL trips the registry's integrity check ONLY IF the registry is still sealed by its previous seal: the seal is keyless, so whoever can write the file can
    delete the FAIL and reseal. Only an ``anchor`` held outside the repository/volume (``hypothesis_registry.anchor_digest``) detects that; see ``evaluate``."""
    entry = _registry_entry(registry, candidate_id)
    if entry is None:
        raise SpecError(f"candidate {candidate_id!r} is not registered")
    out: List[EvidenceItem] = []
    for ev in entry.get("evidence", []):
        stage, _, result = str(ev.get("result", "")).partition(":")
        d = ev.get("details") or {}
        if stage not in LADDER or result not in (PASS, FAIL, INCONCLUSIVE):
            continue
        reg_ms = _utc_ms(ev.get("recorded_utc")) or 0
        claimed = d.get("recorded_ms")
        # the attested claim of when it was recorded may not be LATER than the registry's own append time (1s resolution): a later claim is clamped, so its attestation fails
        rec = min(claimed, reg_ms + 999) if _int(claimed) and claimed >= 0 else reg_ms
        out.append(EvidenceItem(stage=stage, result=result, provenance=str(ev.get("provenance") or ""), metrics=d.get("metrics") or {}, data_sha256=d.get("data_sha256"),
                                status=d.get("status"), candidate_id=d.get("candidate_id"), spec_digest=d.get("spec_digest"), attachment=d.get("attachment"),
                                recorded_ms=rec, attestation=d.get("attestation"), report_attestation=d.get("report_attestation")))
    return out


def evaluate_registered(registry: Mapping[str, Any], spec: Mapping[str, Any], *, now_ms: Optional[int] = None, anchor: Optional[str] = None,
                        key_provider: Optional[Callable[[], Any]] = None) -> VerdictResult:
    """The verdict from the REGISTRY's evidence only. Refuses a registry that fails its integrity check, or (when ``anchor`` is given) does not match the owner-held anchor.
    WITHOUT an anchor the verdict is capped below QUALIFIED (REGISTRY_NOT_ANCHORED): a keyless-resealed registry is untrusted."""
    import hypothesis_registry as hr
    bad = hr.verify(registry, anchor=anchor)
    if bad:
        raise SpecError("the registry fails its integrity check: " + "; ".join(bad[:3]))
    return evaluate(spec, evidence_from_registry(registry, str(spec["candidate_id"])), registry=registry, now_ms=now_ms, anchor=anchor, key_provider=key_provider)


def spec_digest(spec: Mapping[str, Any]) -> str:
    return hashlib.sha256(json.dumps(spec, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()


def btc_carry_candidate() -> Dict[str, Any]:
    """DESCRIBES the frozen BTC prospective experiment as a candidate so this engine can say where it stands. It changes nothing in the plan (read-only reference to its pinned digests).
    Its bias declarations are TRUTHFUL, not aspirational: depth and basis/margin data are absent, so those two are False and ``spec_problems`` reports them (UNTESTED)."""
    import carry_prospective as CP
    lab = lambda v, p: {"value": v, "provenance": p}
    return {
        "candidate_id": "btcusdt_funding_carry_overlay@prospective-1", "family": "funding_carry",
        "hypothesis": "mean excess funding per 8h settlement on BTCUSDT exceeds the hurdle-implied requirement after the plan's costs",
        "economic_mechanism": "a delta-neutral short perpetual against owned BTC receives funding paid by leveraged longs", "counterparty": "perpetual-swap longs paying funding",
        "required_data": [
            {"name": "bybit_btcusdt_funding_history", "point_in_time": True, "repo_has": True, "declaration_keys": ["point_in_time_universe", "no_future_funding_knowledge", "no_look_ahead"],
             "gap": "BTCUSDT funding prints only; prospective, so nothing before the registered window is evaluated"},
            {"name": "spot price and hedge-leg basis/margin at entry and exit", "point_in_time": True, "repo_has": False, "declaration_keys": ["hedge_completeness_modelled"],
             "gap": "basis and margin are unmeasured (risk_model: funding-only)"},
            {"name": "L2 depth for fills and capacity", "point_in_time": True, "repo_has": False, "declaration_keys": ["fills_modelled_against_real_depth"],
             "gap": "capacity is not yet measured and fees/slippage are assumed, not measured"}],
        "eligibility_rules": ["BTCUSDT linear perpetual only", "8h funding interval only"], "entry": "per carry_prospective registered plan", "exit": "per registered plan",
        "holding_period": "30 days (registered assumption)", "execution_model": "taker, overlay (perp legs only)",
        "fee_model": {"maker_bps": lab(2.0, "assumed"), "taker_bps": lab(5.5, "assumed")},
        "funding_model": {"expected_bps_per_settlement": lab(0.259, "derived"), "note": "in-sample mean of past funding used as a forecast: derived, not a measurement of future funding"},
        "slippage_model": {"entry_bps": lab(0.2, "assumed"), "exit_bps": lab(0.2, "assumed")},
        "capacity_model": {"min_viable_capacity_usd": 100.0, "note": "capacity not yet measured"},
        "risk_model": "see risk_policy_proposal (funding-only; basis/margin unmeasured)", "drawdown_model": "funding-only 130.5 bps whole sample / 17.6 bps last 365d (measured, in-sample)",
        "OOS_protocol": {"train_test_boundary": "none: prospective only", "data_window": {"start_ms": CP.WINDOW_START_MS, "end_ms": CP.WINDOW_END_MS},
                         "plan_digests": {f"{h:g}": CP.PINNED_DIGESTS[h] for h in CP.HURDLES_PCT}},
        "prospective_protocol": f"{CP.WINDOW_START_MS}..{CP.WINDOW_END_MS}, evaluated only via tools/trial_journal.py",
        "rejection_conditions": ["closed-window evaluation under the registered plan is not supported at any hurdle", "coverage below the registered floor"],
        # Truthful: the two declarations whose data the repository lacks (depth, basis/margin) are False, so this description is honestly NOT yet a testable specification.
        "bias_declarations": {"point_in_time_universe": True, "no_future_funding_knowledge": True, "no_look_ahead": True,
                              "fills_modelled_against_real_depth": False, "hedge_completeness_modelled": False},
    }


def _attest_precondition(signed: EvidenceItem, spec: Mapping[str, Any], registry: Optional[Mapping[str, Any]], key: Any, now: int) -> Optional[str]:
    """Why the owner must NOT sign this observed-stage item: the reason ``counted`` (or the pending-prospective check) would refuse it anyway. None = fine to sign.
    A signature must never launder evidence the engine would reject, and must never be given for an unregistered or drifted candidate."""
    if registry is None:
        return "NO_REGISTRY_GIVEN(--registry is required to attest observed evidence)"
    if signed.result == INCONCLUSIVE:                                    # the running-observation marker: same binding as the pending check in evaluate()
        return _candidate_binding(signed, spec) or _registry_binding(signed, spec, registry, now)
    ok, why = counted(signed, spec, registry, now_ms=now, key_provider=lambda: key)
    return None if ok else why


def _attest_main(argv: Sequence[str]) -> int:
    """``strategy_verdict.py attest --candidate C --evidence E.jsonl [--out F]``: the OWNER'S tool. Reads the key from OWNER_ATTESTATION_KEY (never from argv), adds the owner
    attestation to every observed-stage evidence item and prints/writes the JSONL. Run it where the key lives; the key is not stored or echoed."""
    ap = argparse.ArgumentParser(prog="strategy_verdict.py attest", description="Owner attestation of observed-stage evidence (HMAC under OWNER_ATTESTATION_KEY).")
    ap.add_argument("--candidate", required=True)
    ap.add_argument("--evidence", required=True)
    ap.add_argument("--out", default=None)
    ap.add_argument("--registry", default=None, help="the registry the evidence will be evaluated against; REQUIRED to attest observed evidence (the same preconditions as counted())")
    ap.add_argument("--now-ms", type=int, default=None)
    a = ap.parse_args(list(argv))
    key = env_key_provider()
    if _key_bytes(key) is None:
        print(f"strategy_verdict attest: {OWNER_ATTESTATION_KEY_ENV} is unset or too short; nothing was attested", file=sys.stderr)
        return 2
    try:
        spec = json.load(open(a.candidate, encoding="utf-8"))
        items = [evidence_from_dict(json.loads(x)) for x in open(a.evidence, encoding="utf-8") if x.strip()]
        sd, cid = spec_digest(spec), spec.get("candidate_id")
        registry = None
        if a.registry:
            import hypothesis_registry as hr
            registry = hr.load(a.registry)
        now = min(int(time.time() * 1000), a.now_ms) if a.now_ms is not None else int(time.time() * 1000)
        out = []
        for it in items:
            if it.stage in OBSERVED_STAGES:
                if it.candidate_id != cid or it.spec_digest != sd:
                    raise SpecError(f"{it.stage} evidence is not about this candidate/spec; refusing to attest it")
                signed = attest_item(it, key)
                why = _attest_precondition(signed, spec, registry, key, now)
                if why:
                    raise SpecError(f"{it.stage} evidence would not be counted ({why}); refusing to attest it")
                it = signed
            out.append(json.dumps(evidence_to_dict(it), sort_keys=True, allow_nan=False))
    except (OSError, ValueError, TypeError, SpecError) as exc:
        print(f"strategy_verdict attest: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    text = "\n".join(out) + ("\n" if out else "")
    if a.out:
        with open(a.out, "w", encoding="utf-8") as fh:
            fh.write(text)
    else:
        sys.stdout.write(text)
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] == "attest":
        return _attest_main(args[1:])
    ap = argparse.ArgumentParser(description="Economic verdict for a strategy candidate. Offline. Built to kill, not to promote.")
    ap.add_argument("--candidate", help="candidate JSON (see REQUIRED_SPEC_FIELDS); omit with --btc to describe the frozen BTC experiment")
    ap.add_argument("--evidence", default=None, help="JSONL of evidence items {stage,result,provenance,metrics,data_sha256}")
    ap.add_argument("--btc", action="store_true")
    ap.add_argument("--registry", default=None, help="evaluate from the registry's evidence for this candidate (a dropped FAIL is caught by the seal AND, with --anchor, by the owner-held head digest)")
    ap.add_argument("--anchor", default=None, help="owner-held registry head digest (hypothesis_registry.py --print-anchor); without it QUALIFIED is unreachable. Evidence-only runs (no --registry) never exceed INSUFFICIENT_DATA")
    a = ap.parse_args(args)
    try:
        spec = btc_carry_candidate() if a.btc else json.load(open(a.candidate, encoding="utf-8"))
        ev = [evidence_from_dict(json.loads(x)) for x in open(a.evidence, encoding="utf-8") if x.strip()] if a.evidence else []
    except (OSError, ValueError, TypeError, SpecError) as exc:
        print(f"strategy_verdict: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    try:
        if a.registry:
            import hypothesis_registry as hr
            res = evaluate_registered(hr.load(a.registry), spec, anchor=a.anchor)
        else:
            res = evaluate(spec, ev)
    except (SpecError, ValueError, OSError) as exc:
        print(f"strategy_verdict: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(res.as_dict(), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
