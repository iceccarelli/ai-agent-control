"""Application-side writer: maps programme ARTIFACTS onto the existing ``carry.*`` tables (supabase/migrations). Offline, order-less, credential-less.

It is NOT a second financial truth. It writes registrations, evidence, manifests, hashes, status words and receipts, and nothing else: no raw owner rows, no
balances, no account identifiers, no orders or fills (the execution/order/fill/reconciliation tables are deliberately not mapped). The venue's own reads and the
owner's reconciled accounting stay authoritative.

    rows  = builders (below)            -> Put(table, row)       one canonicalisation, provenance on every row
    p     = plan(owner_id, rows, ...)   -> Plan                  validated, secret-scanned, de-duplicated, FK-ordered list of ``carry.put_row`` calls
    rep   = write(p, client)            -> WriteReport           client is INJECTED; this module never builds one

CLIENT PROTOCOL (``ServiceRoleClient``, an abstract class defined here: the client must be an INSTANCE of it). The owner supplies an object that talks to the owner's OWN project with the service role, server-side:
    role: str                                                    must be exactly "service_role" (anon / authenticated / publishable semantics are refused)
    put_row(table: str, row: Mapping) -> bool                    carry.put_row: True = inserted, False = an equal key already existed
    find_content_hashes(table, owner_id, key: Mapping) -> Sequence[str]   service-role READ of content_sha256 for rows with that natural key;
                                                                 compare each value IN THE COLUMN'S TYPE (an ISO string vs a timestamptz, a JSON int vs integer)
``put_row`` cannot tell an identical re-send from a CONFLICTING duplicate (both return False); ``write`` asks ``find_content_hashes`` and reports the difference:
UNCHANGED (stored hash equals ours) or CONFLICT (different content under the same key, or the content hash is already used under another key). A conflict is
never retried, overwritten or dropped silently. This module never reads environment variables and never holds a key.

Canonicalisation: ``content_sha256`` = sha256 of sorted-key compact JSON (``allow_nan=False``, ASCII-escaped) of the row's content. Floats and Decimals are
written as EXACT decimal strings (never binary-float text), so money and limits hash identically on every machine. Which fields are hashed per table is
``HASH_RULE``; registrations use the registry's own ``entry_digest``, risk policies the digest of the policy document, imports use import_id == content hash.

SECRET GUARD: every row is scanned (keys and values, recursively) and the whole plan is refused if anything looks like a key / secret / token / JWT / private key /
credentialed URL, or a field is named like an account identifier (apiKey, secret, uid, accountId, email, ip ...). Findings name table, path and rule, never the value.
"""
from __future__ import annotations

import abc
import dataclasses
import datetime as _dt
import hashlib
import json
import math
import os
import re
import sys
import unicodedata
import ipaddress
from decimal import Context, Decimal, InvalidOperation, ROUND_HALF_EVEN
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

SCHEMA_VERSION = 1
SOURCE_PREFIX = "tools/"

OWNER_STATUSES = ("VERIFIED", "RECONCILED", "ESTIMATED", "PROVISIONAL", "STALE", "DISPUTED", "INCOMPLETE", "UNAVAILABLE")
EVIDENCE_LEVELS = ("RESEARCH_CANDIDATE", "BACKTEST", "OUT_OF_SAMPLE", "PROSPECTIVE", "SIMULATED_EXECUTION", "TESTNET_MECHANICS", "OWNER_ACCOUNT_VERIFIED")
SEVERITIES = ("INFO", "WARN", "HIGH", "CRITICAL")
APPROVAL_SUBJECTS = ("risk_policy_version", "execution_session", "import_reconciliation", "data_batch")
RISK_LIMIT_FIELDS = ("per_trade_loss_usd", "daily_loss_usd", "strategy_drawdown_usd", "portfolio_drawdown_usd", "max_hold_hours", "max_exposure_usd", "max_leverage",
                     "min_liquidity_usd", "max_unhedged_usd", "min_margin_multiple", "min_liquidation_distance_pct", "max_data_age_s", "max_correlated_exposure_usd",
                     "min_recent_funding_bps")


class SyncError(ValueError):
    """A builder or plan refused its input (bad shape, wrong vocabulary, missing field)."""


class SecretGuardError(SyncError):
    def __init__(self, findings: Sequence[str]):
        self.findings = tuple(findings)
        super().__init__("secret guard refused the plan (value never echoed): " + "; ".join(self.findings[:20]))


class ClientRefused(SyncError):
    """The injected client is not a server-side service-role writer."""


# ------------------------------------------------------------------ canonicalisation (the ONE)

_DEC_RE = re.compile(r"^-?(0|[1-9]\d*)(\.\d+)?([eE][+-]?\d+)?$")
#: Numeric bounds: a value outside them is a refusal, never an overflow, a silent rounding or a gigabyte string.
MAX_DECIMAL_TEXT = 64          # characters of a decimal STRING
MAX_DECIMAL_DIGITS = 40        # significant digits (so the 60-digit context below never rounds)
MAX_DECIMAL_EXP = 40           # |adjusted exponent|
MAX_INT_BITS = 160
MAX_DEPTH = 8                  # nesting of any JSON value
_CTX = Context(prec=60, rounding=ROUND_HALF_EVEN, traps=[InvalidOperation])


def canon_decimal(v: Any) -> str:
    """Exact, locale- and platform-independent decimal string. Floats go through their shortest repr, so 0.1 is "0.1"; NaN/inf/bool are refused. EVERY numeric failure
    (huge exponent or length, overflow, bad syntax) is a SyncError, never a decimal/arithmetic exception."""
    try:
        return _canon_decimal(v)
    except SyncError:
        raise
    except (ArithmeticError, ValueError, TypeError) as exc:      # decimal.Overflow / InvalidOperation / OverflowError / RecursionError-free by the bounds
        raise SyncError(f"not a representable number ({type(exc).__name__})") from None


def _canon_decimal(v: Any) -> str:
    if isinstance(v, bool) or v is None:
        raise SyncError(f"not a number: {v!r}")
    if isinstance(v, float):
        if not math.isfinite(v):
            raise SyncError("NaN/inf is not a number")
        d = Decimal(repr(v))
    elif isinstance(v, int):
        if v.bit_length() > MAX_INT_BITS:
            raise SyncError("integer out of range")
        d = Decimal(v)
    elif isinstance(v, Decimal):
        d = v
    elif isinstance(v, str):
        t = v.strip()
        if len(v) > MAX_DECIMAL_TEXT or not _DEC_RE.match(t):
            raise SyncError("not a number")
        d = Decimal(t)
    else:
        raise SyncError(f"not a number: {type(v).__name__}")
    if not d.is_finite():
        raise SyncError("NaN/inf is not a number")
    if d == 0:
        return "0"
    if len(d.as_tuple().digits) > MAX_DECIMAL_DIGITS or abs(d.adjusted()) > MAX_DECIMAL_EXP:
        raise SyncError("number out of the supported range")
    return format(d.normalize(_CTX), "f")


def _safe_name(k: Any) -> str:
    """A key as it may appear in a refusal message: only when it is a short plain identifier that no guard rule flags (a key can itself be the secret)."""
    s = str(k)
    return s if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,40}", s) and not key_rule(s) and not _shape_rules(s) else "<key>"


def normalise(obj: Any, _depth: int = 0) -> Any:
    """Plain JSON types only: float/Decimal -> exact decimal string, tuple -> list, str keys only. Anything else is refused (no ``default=str``). Depth and integer size are bounded."""
    if _depth > MAX_DEPTH:
        raise SyncError("value nested too deeply")
    if obj is None or isinstance(obj, (bool, str)):
        return obj
    if isinstance(obj, int):
        if obj.bit_length() > MAX_INT_BITS:
            raise SyncError("integer out of range")
        return obj
    if isinstance(obj, (float, Decimal)):
        return canon_decimal(obj)
    if isinstance(obj, Mapping):
        out: Dict[str, Any] = {}
        for k, v in obj.items():
            if not isinstance(k, str):
                raise SyncError("non-string key")
            out[k] = normalise(v, _depth + 1)
        return out
    if isinstance(obj, (list, tuple)):
        return [normalise(x, _depth + 1) for x in obj]
    raise SyncError(f"unsupported value type {type(obj).__name__}")


def canonical_json(obj: Any) -> str:
    return json.dumps(normalise(obj), sort_keys=True, separators=(",", ":"), allow_nan=False)


def sha256_text(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def content_sha256(obj: Any) -> str:
    return sha256_text(canonical_json(obj))


def utc(v: Any) -> str:
    """Canonical UTC timestamp string 'YYYY-MM-DDTHH:MM:SS[.ffffff]Z'. Accepts ms-since-epoch ints, aware datetimes and ISO strings WITH a zone; naive times are refused.
    Out-of-range values are a SyncError (never OverflowError/OSError)."""
    try:
        return _utc(v)
    except SyncError:
        raise
    except (ArithmeticError, ValueError, OSError, TypeError):
        raise SyncError("timestamp out of range or unparseable") from None


def _utc(v: Any) -> str:
    if isinstance(v, bool):
        raise SyncError("not a timestamp")
    if isinstance(v, int):
        if not 0 <= v <= 253402300799999:
            raise SyncError("timestamp out of range")
        t = _dt.datetime.fromtimestamp(v / 1000.0, tz=_dt.timezone.utc)
    elif isinstance(v, _dt.datetime):
        if v.tzinfo is None:
            raise SyncError("naive datetime refused: state the zone")
        t = v.astimezone(_dt.timezone.utc)
    elif isinstance(v, str):
        s = v.strip()
        if len(s) > 40 or not re.search(r"(Z|[+-]\d\d:?\d\d)$", s) or not re.match(r"^\d{4}-\d\d-\d\d[T ]\d\d:\d\d:\d\d", s):
            raise SyncError("timestamp must be ISO-8601 with an explicit zone")
        t = _dt.datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(_dt.timezone.utc)
    else:
        raise SyncError(f"not a timestamp: {type(v).__name__}")
    base = t.strftime("%Y-%m-%dT%H:%M:%S")
    frac = f".{t.microsecond:06d}".rstrip("0") if t.microsecond else ""
    return base + frac + "Z"


# ------------------------------------------------------------------ table specs (parity with supabase/migrations is a TEST, not a comment)
# kind: uuid, nb (non-blank text), text, code (UPPER_SNAKE reason code, no free text), hex (sha256), codesha, ts, int, uint (>=0), num (>=0 decimal string/int), dec (decimal string), bool, json (non-empty object/array),
#       obj (object), arr (non-empty list of non-blank text), enum:A|B. A trailing '?' marks a column the row may omit (nullable or defaulted).
_SPEC_TEXT = {
    "strategy_hypotheses": "owner_id:uuid hypothesis_id:nb mechanism:nb family:nb statement:nb source:nb schema_version:uint content_sha256:hex",
    "experiment_registrations": ("owner_id:uuid trial_id:nb hypothesis_id:nb mechanism:nb family:nb strategy:nb universe:json input_data_sha256:hex code_sha:codesha "
                                 "data_window:json parameter_grid:json costs:json statistical_design:json train_test:json multiple_testing_family:nb "
                                 "prospective_period:nb registered_utc:ts note:text? source:nb schema_version:uint content_sha256:hex"),
    "experiment_evidence": ("owner_id:uuid trial_id:nb level:enum:" + "|".join(EVIDENCE_LEVELS) + " result:nb provenance:nb details:obj? recorded_utc:ts source:nb "
                            "schema_version:uint content_sha256:hex"),
    "experiment_amendments": "owner_id:uuid trial_id:nb what:nb why:nb recorded_utc:ts source:nb schema_version:uint content_sha256:hex",
    "experiment_abandonments": "owner_id:uuid trial_id:nb reason:nb recorded_utc:ts source:nb schema_version:uint content_sha256:hex",
    "experiment_supersessions": "owner_id:uuid trial_id:nb superseded_by:nb reason:nb recorded_utc:ts source:nb schema_version:uint content_sha256:hex",
    "instrument_universe_snapshots": ("owner_id:uuid snapshot_id:nb venue:nb captured_utc:ts manifest_sha256:hex manifest_ref:nb instrument_count:uint source:nb "
                                      "schema_version:uint content_sha256:hex"),
    "data_batches": ("owner_id:uuid batch_id:nb dataset:nb venue:nb batch_sha256:hex byte_count:uint row_count:uint? segment_ids:arr window_start_utc:ts "
                     "window_end_utc:ts storage_ref:nb source:nb schema_version:uint content_sha256:hex"),
    "md_seal_receipts": ("owner_id:uuid seal_id:nb batch_id:nb manifest_sha256:hex manifest_rows:uint receipt_sha256:hex sealed_utc:ts receipt:obj? source:nb "
                         "schema_version:uint content_sha256:hex"),
    "collector_status": ("owner_id:uuid collector_name:nb state:enum:RUNNING|STALLED|STOPPED|ERROR|UNKNOWN observed_utc:ts last_record_utc:ts? lag_seconds:num? "
                         "detail:obj? source:nb schema_version:uint content_sha256:hex"),
    "owner_account_imports": ("owner_id:uuid import_id:hex source_sha256:hex kind:nb period_start_utc:ts? period_end_utc:ts? row_count:uint? storage_ref:nb "
                              "status:enum:" + "|".join(OWNER_STATUSES) + " report_sha256:hex? imported_utc:ts source:nb schema_version:uint content_sha256:hex"),
    "owner_account_import_status_events": ("owner_id:uuid import_id:hex status:enum:" + "|".join(OWNER_STATUSES) + " reason:code? evidence_ref:hex? recorded_utc:ts "
                                           "source:nb schema_version:uint content_sha256:hex"),
    "risk_policy_versions": ("owner_id:uuid policy_name:nb version:int policy:json effective_from_utc:ts source:nb schema_version:uint content_sha256:hex"),
    "approval_records": ("owner_id:uuid approval_id:nb subject_kind:enum:" + "|".join(APPROVAL_SUBJECTS) + " subject_ref:nb approved_by_owner:bool approver_id:uuid? "
                         "statement_sha256:hex? note:text? decided_utc:ts source:nb schema_version:uint content_sha256:hex"),
    "incidents": ("owner_id:uuid incident_id:nb severity:enum:" + "|".join(SEVERITIES) + " title:nb runbook_ref:text? opened_utc:ts source:nb schema_version:uint "
                  "content_sha256:hex"),
    "incident_events": ("owner_id:uuid incident_id:nb state:enum:OPENED|ACKNOWLEDGED|MITIGATED|RESOLVED|REOPENED note:text? recorded_utc:ts source:nb "
                        "schema_version:uint content_sha256:hex"),
    "system_health": ("owner_id:uuid component:nb state:enum:OK|DEGRADED|DOWN|UNKNOWN observed_utc:ts detail:obj? source:nb schema_version:uint content_sha256:hex"),
    "backup_receipts": ("owner_id:uuid backup_id:nb target:nb artifact_sha256:hex byte_count:uint storage_ref:nb taken_utc:ts source:nb schema_version:uint "
                        "content_sha256:hex"),
    "restore_receipts": ("owner_id:uuid restore_id:nb backup_id:nb outcome:enum:PASSED|FAILED verification_sha256:hex checks:obj? restored_utc:ts source:nb "
                         "schema_version:uint content_sha256:hex"),
}
#: The non-content unique key of each table (what ``put_row`` can collide on besides (owner_id, content_sha256)); () = content hash is the only key.
NATURAL_KEY: Dict[str, Tuple[str, ...]] = {
    "strategy_hypotheses": ("hypothesis_id",), "experiment_registrations": ("trial_id",), "experiment_evidence": (), "experiment_amendments": (),
    "experiment_abandonments": ("trial_id",), "experiment_supersessions": ("trial_id",), "instrument_universe_snapshots": ("snapshot_id",),
    "data_batches": ("batch_id",), "md_seal_receipts": ("seal_id",), "collector_status": ("collector_name", "observed_utc"), "owner_account_imports": ("import_id",),
    "owner_account_import_status_events": (), "risk_policy_versions": ("policy_name", "version"), "approval_records": ("approval_id",), "incidents": ("incident_id",),
    "incident_events": (), "system_health": ("component", "observed_utc"), "backup_receipts": ("backup_id",), "restore_receipts": ("restore_id",),
}
#: Parents first (foreign keys). Evidence before abandonment/supersession (the DB refuses NEW evidence on an abandoned trial).
TABLE_ORDER = ("strategy_hypotheses", "experiment_registrations", "experiment_evidence", "experiment_amendments", "experiment_abandonments", "experiment_supersessions",
               "instrument_universe_snapshots", "data_batches", "md_seal_receipts", "collector_status", "owner_account_imports", "owner_account_import_status_events",
               "risk_policy_versions", "approval_records", "incidents", "incident_events", "system_health", "backup_receipts", "restore_receipts")
#: How content_sha256 is formed. row: over the whole row except owner_id/content_sha256. import: also except import_id, which EQUALS the hash (SQL check).
#: policy: the policy document. registry: the registry's entry_digest (cannot be recomputed from the row; checked against the entry at build time).
HASH_RULE = {t: "row" for t in TABLE_ORDER}
HASH_RULE.update({"owner_account_imports": "import", "risk_policy_versions": "policy", "experiment_registrations": "registry"})


@dataclasses.dataclass(frozen=True)
class Col:
    name: str
    kind: str
    optional: bool
    enum: Tuple[str, ...] = ()


# ---- WHITELISTED, TYPED field schemas for every object-valued column (no free-form objects). A field that is not listed is REFUSED (never stripped silently).
# nested kinds: uint, num (int or exact decimal string), unum (>= 0), bool, hex64, code (UPPER_SNAKE), word (short identifier), status (owner status word),
#               metric (bool | int | decimal string | word | list of words), verdict (ok|fail|skipped|PASS|FAIL), "?" suffix is accepted for every kind: None = absent.
EVIDENCE_METRICS = ("capacity_usd", "complete", "fees_measured", "fills_measured", "independent_observations", "multiple_testing_adjusted", "n_variants_tested",
                    "net_edge_bps_after_costs", "point_in_time", "registered_before_observation", "universe_point_in_time", "unknown_cost_inputs",
                    "uses_future_information", "venue", "rejection_conditions_fired", "sharpe", "n", "sortino", "max_drawdown_pct", "hit_rate", "trades", "observations",
                    "observation_started_ms", "observation_ended_ms", "observed_settlements", "coverage", "fills_count", "fee_observations_count")
#: Supabase carries NO second financial truth: the owner's P&L figure (``net_usd_after_all_costs``) is deliberately NOT a whitelisted metric. It lives in the hashed import report
#: the owner holds; an evidence row can only name that report by its sha256 (``import_report_sha256``) or an execution log by its sha256 (``execution_log_sha256``).
EVIDENCE_BINDING_HASHES = ("import_report_sha256", "execution_log_sha256")
#: Evidence levels that make a claim about real money / a real venue: they are refused unless the row names the hashed artifact it is bound to.
LEVEL_REQUIRES_HASH = {"OWNER_ACCOUNT_VERIFIED": "import_report_sha256", "TESTNET_MECHANICS": "execution_log_sha256"}
#: Keys of a strategy_verdict / registry evidence ``details`` that are deliberately NOT synced (mapped away, and reported): the owner report attachment (privacy), the candidate
#: binding words, and the owner's figure and its labels (no second financial truth).
EVIDENCE_DROPPED_DETAILS = ("attachment", "candidate_id", "spec_digest")
EVIDENCE_DROPPED_METRICS = ("net_usd_after_all_costs", "owner_figure_basis", "import_status", "candidate_id", "spec_digest")
OBJ_SCHEMAS: Dict[Tuple[str, str], Dict[str, Any]] = {
    ("experiment_evidence", "details"): {**{m: "metric" for m in EVIDENCE_METRICS}, "metrics": {**{m: "metric" for m in EVIDENCE_METRICS}, **{h: "hex64" for h in EVIDENCE_BINDING_HASHES}},
                                         "data_sha256": "hex64", "status": "status", **{h: "hex64" for h in EVIDENCE_BINDING_HASHES}},
    ("md_seal_receipts", "receipt"): {"segment": "word", "records": "uint", "first_id": "uint", "last_id": "uint", "last_chain": "hex64", "sha256": "hex64"},
    ("collector_status", "detail"): {"segments": "uint", "records": "uint", "gaps": "uint", "restarts": "uint", "reconnects": "uint", "lag_seconds": "unum",
                                     "last_error_code": "code", "batch_sha256": "hex64"},
    ("system_health", "detail"): {"latency_ms": "unum", "queue_depth": "uint", "error_count": "uint", "uptime_s": "unum", "version": "word", "last_error_code": "code"},
    ("restore_receipts", "checks"): {"chain": "verdict", "schema": "verdict", "sha256_match": "bool", "rows_match": "bool", "row_count": "uint", "restore_seconds": "unum"},
    ("risk_policy_versions", "policy"): {k: "unum" for k in RISK_LIMIT_FIELDS},
}
_CODE_RE = re.compile(r"^[A-Z][A-Z0-9_]{2,63}$")
_WORD_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,47}$")
_MAX_UINT = 2 ** 63 - 1


def _dec_ok(v: Any, nonneg: bool = False) -> bool:
    if isinstance(v, bool):
        return False
    if isinstance(v, int):
        return abs(v) < 2 ** MAX_INT_BITS and (v >= 0 or not nonneg)
    if isinstance(v, str) and len(v) <= MAX_DECIMAL_TEXT and _DEC_RE.match(v):
        try:
            canon_decimal(v)
        except SyncError:
            return False
        return not (nonneg and v.startswith("-") and canon_decimal(v) != "0")
    return False


def _nested_ok(kind: str, v: Any) -> bool:
    if kind == "uint":
        return isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= _MAX_UINT
    if kind == "num":
        return _dec_ok(v)
    if kind == "unum":
        return _dec_ok(v, True)
    if kind == "bool":
        return isinstance(v, bool)
    if kind == "hex64":
        return isinstance(v, str) and bool(_HEX64.match(v))
    if kind == "code":
        return isinstance(v, str) and bool(_CODE_RE.match(v))
    if kind == "word":
        return isinstance(v, str) and bool(_WORD_RE.match(v))
    if kind == "status":
        return isinstance(v, str) and v in OWNER_STATUSES
    if kind == "verdict":
        return v in ("ok", "fail", "skipped", "PASS", "FAIL")
    if kind == "metric":
        if isinstance(v, list):
            return len(v) <= 32 and all(isinstance(x, str) and bool(_WORD_RE.match(x)) for x in v)
        return isinstance(v, bool) or _dec_ok(v) or (isinstance(v, str) and bool(_WORD_RE.match(v)))
    return False


def _schema_problems(schema: Mapping[str, Any], v: Any, path: str) -> List[str]:
    if not isinstance(v, dict):
        return [f"{path}:bad_obj"]
    out: List[str] = []
    for k, x in v.items():
        sub = schema.get(k)
        if sub is None:
            out.append(f"{path}.{_safe_name(k)}:not_whitelisted")
        elif x is None:
            continue
        elif isinstance(sub, dict):
            out += _schema_problems(sub, x, f"{path}.{k}")
        elif not _nested_ok(sub, x):
            out.append(f"{path}.{k}:bad_{sub}")
    return out


def _schema_hex_paths(schema: Mapping[str, Any], v: Any, path: str) -> List[str]:
    """Paths of fields that are validly typed sha256 hex: the only nested strings the secret guard lets be long hex."""
    out: List[str] = []
    if isinstance(v, dict):
        for k, sub in schema.items():
            if k in v:
                if isinstance(sub, dict):
                    out += _schema_hex_paths(sub, v[k], f"{path}.{k}")
                elif sub == "hex64" and _nested_ok("hex64", v[k]):
                    out.append(f"{path}.{k}")
    return out


def _parse_spec(text: str) -> Tuple[Col, ...]:
    cols = []
    for tok in text.split():
        optional = tok.endswith("?")
        name, _, kind = tok.rstrip("?").partition(":")
        enum: Tuple[str, ...] = ()
        if kind.startswith("enum:"):
            enum = tuple(kind[5:].split("|"))
            kind = "enum"
        cols.append(Col(name, kind, optional, enum))
    return tuple(cols)


SPECS: Dict[str, Tuple[Col, ...]] = {t: _parse_spec(s) for t, s in _SPEC_TEXT.items()}
assert set(SPECS) == set(TABLE_ORDER) == set(NATURAL_KEY)


def columns(table: str) -> Tuple[str, ...]:
    return tuple(c.name for c in SPECS[table])


def required_columns(table: str) -> Tuple[str, ...]:
    return tuple(c.name for c in SPECS[table] if not c.optional)


# ------------------------------------------------------------------ row validation (mirrors the DB checks, offline)

_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_CODESHA = re.compile(r"^[0-9a-f]{7,64}$")
_TS = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(\.\d{1,6})?Z$")


def _nb(v: Any) -> bool:
    return isinstance(v, str) and bool(v.strip())


def _bounded(v: Any, depth: int = 0, budget: Optional[List[int]] = None) -> bool:
    """A JSON value of bounded depth, node count and string length (no pathological documents)."""
    budget = [2000] if budget is None else budget
    budget[0] -= 1
    if budget[0] < 0 or depth > MAX_DEPTH:
        return False
    if isinstance(v, dict):
        return all(isinstance(k, str) and len(k) <= 128 and _bounded(x, depth + 1, budget) for k, x in v.items())
    if isinstance(v, list):
        return all(_bounded(x, depth + 1, budget) for x in v)
    if isinstance(v, str):
        return len(v) <= 4000
    return v is None or isinstance(v, bool) or (isinstance(v, int) and v.bit_length() <= MAX_INT_BITS)


def _json_nonempty(v: Any) -> bool:
    return isinstance(v, (dict, list)) and len(v) > 0 and _bounded(v)


def _check_kind(c: Col, v: Any) -> Optional[str]:
    k = c.kind
    if k == "uuid":
        ok = isinstance(v, str) and bool(_UUID_RE.match(v))
    elif k == "nb":
        ok = _nb(v)
    elif k == "text":
        ok = isinstance(v, str)
    elif k == "code":
        ok = isinstance(v, str) and bool(_CODE_RE.match(v))
    elif k == "hex":
        ok = isinstance(v, str) and bool(_HEX64.match(v))
    elif k == "codesha":
        ok = isinstance(v, str) and bool(_CODESHA.match(v))
    elif k == "ts":
        ok = isinstance(v, str) and bool(_TS.match(v))
        if ok:
            try:
                _ts_dt(v)
            except ValueError:
                ok = False
    elif k == "int":
        ok = isinstance(v, int) and not isinstance(v, bool) and 1 <= v <= _MAX_UINT
    elif k == "uint":
        ok = isinstance(v, int) and not isinstance(v, bool) and (1 if c.name == "schema_version" else 0) <= v <= _MAX_UINT
    elif k == "num":
        ok = _dec_ok(v, True)
    elif k == "bool":
        ok = isinstance(v, bool)
    elif k == "json":
        ok = _json_nonempty(v)
    elif k == "obj":
        ok = isinstance(v, dict) and _bounded(v)
    elif k == "arr":
        ok = isinstance(v, list) and len(v) > 0 and all(_nb(x) for x in v)
    elif k == "enum":
        ok = isinstance(v, str) and v in c.enum
    else:  # pragma: no cover
        ok = False
    return None if ok else f"{c.name}:{'bad_enum' if k == 'enum' else 'bad_' + k}"


def _ts_dt(s: str) -> _dt.datetime:
    return _dt.datetime.fromisoformat(s.replace("Z", "+00:00"))


def row_hash(table: str, row: Mapping[str, Any]) -> str:
    """The content hash this row must carry (HASH_RULE)."""
    rule = HASH_RULE[table]
    if rule == "policy":
        return content_sha256(row["policy"])
    excl = {"owner_id", "content_sha256"} | ({"import_id"} if rule == "import" else set())
    return content_sha256({k: v for k, v in row.items() if k not in excl})


def _bound_hash(details: Any, name: str) -> Optional[str]:
    """The sha256 an evidence row names under ``name`` (top level or inside ``metrics``): valid hex64 with at least 8 distinct characters (not 64 x 'a'); both places agree if both set."""
    if not isinstance(details, Mapping):
        return None
    vals = [x for x in (details.get(name), (details.get("metrics") or {}).get(name) if isinstance(details.get("metrics"), Mapping) else None) if x is not None]
    if not vals or any(not (isinstance(x, str) and _HEX64.match(x) and len(set(x)) >= 8) for x in vals) or len(set(vals)) != 1:
        return None
    return vals[0]


def row_problems(table: str, row: Mapping[str, Any]) -> List[str]:
    """Why a row would be refused by the table (empty = fine). Unknown columns, missing required columns, kinds, vocabularies, cross-column rules, content hash."""
    if table not in SPECS:
        return [f"{table}:unknown_table"]
    out: List[str] = []
    spec = {c.name: c for c in SPECS[table]}
    for k in row:
        if k not in spec:
            out.append(f"{k}:unknown_column")
    for c in SPECS[table]:
        if c.name not in row or row[c.name] is None:
            if not c.optional:
                out.append(f"{c.name}:missing")
            continue
        bad = _check_kind(c, row[c.name])
        if bad:
            out.append(bad)
        elif (table, c.name) in OBJ_SCHEMAS:
            out += _schema_problems(OBJ_SCHEMAS[(table, c.name)], row[c.name], c.name)
    if out:
        return out
    r = row
    if table == "owner_account_imports":
        if r["import_id"] != r["content_sha256"]:
            out.append("import_id:must_equal_content_sha256")
        if r.get("period_start_utc") and r.get("period_end_utc") and _ts_dt(r["period_end_utc"]) < _ts_dt(r["period_start_utc"]):
            out.append("period:end_before_start")
        if r["status"] in ("VERIFIED", "RECONCILED") and not r.get("report_sha256"):
            out.append("report_sha256:VERIFIED_RECONCILED_requires_the_sealed_report_hash")
    if table == "owner_account_import_status_events":
        if r["status"] in ("VERIFIED", "RECONCILED") and r.get("evidence_ref") != r["import_id"]:
            out.append("evidence_ref:VERIFIED_RECONCILED_requires_the_import_row_hash")
        if r["status"] in ("STALE", "DISPUTED", "INCOMPLETE", "UNAVAILABLE") and not r.get("reason"):
            out.append("reason:required_for_negative_status")
    if table == "approval_records" and r["approved_by_owner"]:
        if r.get("approver_id") != r["owner_id"] or not r.get("statement_sha256"):
            out.append("approval:owner_approver_and_statement_required")
    if table == "data_batches" and _ts_dt(r["window_end_utc"]) < _ts_dt(r["window_start_utc"]):
        out.append("window:end_before_start")
    if table == "experiment_supersessions" and r["trial_id"] == r["superseded_by"]:
        out.append("superseded_by:self")
    if table == "incident_events" and r["state"] in ("MITIGATED", "RESOLVED") and not _nb(r.get("note")):
        out.append("note:required_for_MITIGATED_RESOLVED")
    if table == "experiment_evidence" and r["level"] in LEVEL_REQUIRES_HASH:
        need = LEVEL_REQUIRES_HASH[r["level"]]
        if not _bound_hash(r.get("details"), need):
            out.append(f"details.{need}:{r['level']}_requires_the_sha256_of_the_artifact_it_is_bound_to")
    if table == "experiment_registrations" and not all(_nb(r[k]) for k in ("prospective_period",)):
        out.append("prospective_period:blank")
    if HASH_RULE[table] != "registry" and row_hash(table, r) != r["content_sha256"]:
        out.append("content_sha256:does_not_match_content")
    return out


# ------------------------------------------------------------------ SECRET GUARD

_KEY_EXACT = frozenset("""apikey secret secretkey apisecret uid accountid userid memberid email emailaddress ip ipaddress password passwd token accesstoken refreshtoken
sessiontoken authorization privatekey servicerolekey anonkey publishablekey clientsecret bearer jwt pw pwd passphrase mnemonic seedphrase recoveryphrase""".split())
_KEY_PARTS = ("secret", "apikey", "password", "privatekey", "authorization", "servicerole", "publishablekey", "anonkey", "accesstoken")
#: a key whose WORDS (snake_case, kebab-case, camelCase, spaces) include one of these names an account / balance / identity field: none exists in this app's schemas.
_KEY_TOKENS = frozenset("""acct account accounts balance balances wallet wallets equity position positions pnl address iban phone mobile name ssn passport dob birthday
holder beneficiary""".split())
_VALUE_RULES: Tuple[Tuple[str, "re.Pattern[str]"], ...] = (
    ("jwt", re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}(\.[A-Za-z0-9_-]*)?")),
    ("private_key_block", re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY")),
    ("supabase_key", re.compile(r"sb_(secret|publishable)_[A-Za-z0-9_-]{6,}")),
    ("openai_style_key", re.compile(r"\bsk-[A-Za-z0-9_-]{16,}")),
    ("vendor_token", re.compile(r"\b(gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|AKIA[0-9A-Z]{16}|xox[abprs]-[A-Za-z0-9-]{10,}|AIza[0-9A-Za-z_-]{30,})")),
    ("bearer_credential", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{16,}")),
    ("credentialed_url", re.compile(r"[a-zA-Z][a-zA-Z0-9+.-]*://[^/\s:@]+:[^/\s@]+@")),
    ("url_secret_param", re.compile(r"(?i)[?&](api_?key|apikey|token|access_token|secret|sig|signature|key|auth)=[^&\s]{6,}")),
    ("assigned_secret", re.compile(r"(?i)\b(api[_-]?key|api[_-]?secret|secret|token|password|passwd)\b\s*[:=]\s*[\"']?[A-Za-z0-9._~+/=-]{8,}")),
    ("email_address", re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+\.[A-Za-z0-9.-]+")),
    ("uuid_in_text", re.compile(r"(?<![0-9A-Za-z])[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}(?![0-9A-Za-z])")),
    ("internal_hostname", re.compile(r"(?i)(?<![A-Za-z0-9-])(?:[a-z0-9-]+\.)+(?:internal|intranet|corp|lan|local|localdomain|private|vpc|flycast)(?:\.[a-z0-9-]+)*(?![A-Za-z0-9-])")),
    ("assigned_pw", re.compile(r"""(?i)(?<![a-z0-9])(?:pw|pwd|passphrase|mnemonic)\s*[:=]\s*["']?[^\s"',;]{4,}""")),
    ("plain_english_secret", re.compile(r"(?i)\b(?:password|passwd|passphrase|pwd)\b\s*(?:is|was|=|:|equals|set to|changed to)\s+[\"']?(?P<w>[^\s\"',;]{4,})")),
    ("ipv4_address", re.compile(r"(?<![\d.])(?:25[0-5]|2[0-4]\d|1?\d?\d)(?:\.(?:25[0-5]|2[0-4]\d|1?\d?\d)){3}(?![\d.])")),
    ("long_hex_token", re.compile(r"(?<![0-9A-Za-z])(?:0[xX])?[0-9a-fA-F]{24,}(?![0-9A-Za-z])")),
    ("base32_token", re.compile(r"(?<![A-Za-z0-9])[A-Z2-7]{26,}=*(?![A-Za-z0-9])")),
    ("long_digit_run", re.compile(r"(?<!\d)\d{16,}(?!\d)")),
    ("iban_like", re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{11,30}\b")),
    ("ssn_like", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    ("phone_like", re.compile(r"(?<![\d\w])\+\d{8,15}(?!\d)")),
)
_OPAQUE_RUN = re.compile(r"(?<![A-Za-z0-9])[A-Za-z0-9]{18,}(?![A-Za-z0-9])")
_ALNUM_RUN = re.compile(r"[A-Za-z0-9]{24,}")
_B64_RUN = re.compile(r"[A-Za-z0-9+/=_-]{24,}")
_LETTER_RUN = re.compile(r"(?<![A-Za-z0-9])[A-Za-z]{18,}(?![A-Za-z0-9])")
_IPV6_RUN = re.compile(r"(?<![A-Za-z0-9_:.])[0-9A-Fa-f:.]{3,}(?![A-Za-z0-9_])")
#: words that follow 'password is' in ordinary prose and are not a password
_PROSE_AFTER_PASSWORD = frozenset("""required missing empty blank unset none null stored hidden redacted rotated expired invalid wrong incorrect correct unknown changed reset set
needed not never absent supplied provided managed handled held kept removed disabled enabled long short weak strong""".split())
#: a letters-only token that is nothing but PascalCase / camelCase words of 3+ letters (and short acronyms) is an identifier, not a credential
_WORDY = re.compile(r"^(?:[a-z]{3,})?(?:[A-Z][a-z]{2,}|[A-Z]{2,6}(?![a-z]))+$")


def _fold(s: str) -> str:
    """NFKC-normalise (full-width letters/punctuation become ASCII) and drop every format character (zero-width space/joiner, soft hyphen, bidi marks, BOM) before scanning."""
    return "".join(c for c in unicodedata.normalize("NFKC", s) if unicodedata.category(c) != "Cf")


def _has_ipv6(s: str) -> bool:
    for m in _IPV6_RUN.finditer(s):
        t = m.group(0).strip(".")
        if t.count(":") >= 2:
            try:
                ipaddress.IPv6Address(t)
                return True
            except ValueError:
                continue
    return False


def _plain_password(s: str) -> bool:
    for m in _VALUE_RULES_BY_NAME["plain_english_secret"].finditer(s):
        if m.group("w").lower().strip(".") not in _PROSE_AFTER_PASSWORD:
            return True
    return False


def _letters_only_token(s: str) -> bool:
    """18+ letters of mixed case that are not PascalCase words, or ANY mixed-case letters-only run of 36+: a key with no digits in it."""
    for m in _LETTER_RUN.finditer(s):
        t = m.group(0)
        if re.search(r"[a-z]", t) and re.search(r"[A-Z]", t) and (len(t) >= 36 or not _WORDY.match(t)):
            return True
    return False


def _entropy(t: str) -> float:
    n = len(t)
    return -sum((c / n) * math.log2(c / n) for c in (t.count(ch) for ch in set(t)))


def _opaque_token(s: str) -> bool:
    """An exchange-style key or secret anywhere in the text: a mixed-case alphanumeric run of 18+ with a digit; ANY alphanumeric run of 24+ mixing letters and digits (a
    lowercase+digit token too); or a 24+ base64/base64url-looking run that mixes cases and digits at high entropy. Pure hex and base32 have their own rules."""
    for m in _OPAQUE_RUN.finditer(s):
        t = m.group(0)
        if not re.fullmatch(r"[0-9a-f]+", t) and re.search(r"[a-z]", t) and re.search(r"[A-Z]", t) and re.search(r"\d", t):
            return True
    for m in _ALNUM_RUN.finditer(s):
        t = m.group(0)
        if re.search(r"[A-Za-z]", t) and re.search(r"\d", t):
            return True
    for m in _B64_RUN.finditer(s):
        t = m.group(0)
        if re.search(r"[a-z]", t) and re.search(r"[A-Z]", t) and re.search(r"\d", t) and _entropy(t) >= 4.0:
            return True
    return False


_VALUE_RULES_BY_NAME = dict(_VALUE_RULES)
#: A seed / recovery phrase is 12+ short lowercase words in a row (BIP39: 3-8 letters). Ordinary prose almost always has a function word within twelve; a streak of 12 words with
#: none of these (and at least 10 distinct) is refused. Cheap and deliberately over-eager: a refusal is a fail-closed, a leaked phrase is not recoverable.
_SEED_RUN = re.compile(r"(?<![a-z0-9_])[a-z]{3,8}(?:[ ,;]+[a-z]{3,8}){11,}(?![a-z0-9_])")
_FUNCTION_WORDS = frozenset("the and for that this with are was from not but you your our their its has have had will would can could should may might been being than then into onto over under".split())


def _seed_phrase(s: str) -> bool:
    for m in _SEED_RUN.finditer(s.lower()):
        streak: List[str] = []
        for w in re.split(r"[ ,;]+", m.group(0)) + ["the"]:
            if w in _FUNCTION_WORDS:
                if len(streak) >= 12 and len(set(streak)) >= 10:
                    return True
                streak = []
            else:
                streak.append(w)
    return False


def _shape_rules(s: str) -> List[str]:
    s = _fold(s)
    hits = [name for name, rx in _VALUE_RULES if name != "plain_english_secret" and rx.search(s)]
    if _plain_password(s):
        hits.append("plain_english_secret")
    if _has_ipv6(s):
        hits.append("ipv6_address")
    if _seed_phrase(s):
        hits.append("seed_phrase_like")
    if _opaque_token(s) or _letters_only_token(s):
        hits.append("opaque_credential_like")
    return hits


def key_rule(k: str) -> Optional[str]:
    k = _fold(k)
    n = re.sub(r"[^a-z0-9]", "", k.lower())
    if n in _KEY_EXACT:
        return "account_or_secret_field_name"
    if any(p in n for p in _KEY_PARTS):
        return "secret_field_name"
    words = re.sub(r"[^A-Za-z0-9]+", " ", re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", k)).lower().split()
    if any(w in _KEY_TOKENS for w in words):
        return "account_or_balance_field_name"
    return None


def scan_value(obj: Any, path: str = "", exempt: frozenset = frozenset(), _depth: int = 0) -> List[Tuple[str, str]]:
    """(path, rule) findings for one value tree: every KEY and every string VALUE, recursively. Never returns the offending text (a key that is itself suspicious is shown as
    ``<key>``). ``exempt`` = paths of fields already proven to be exactly a sha256 / UUID / git-sha by their column type: only those skip the value-shape rules."""
    out: List[Tuple[str, str]] = []
    if _depth > 2 * MAX_DEPTH:
        return [(path or "<root>", "nested_too_deeply")]
    if isinstance(obj, Mapping):
        for k, v in obj.items():
            ks = str(k)
            sk = _safe_name(ks)
            p = f"{path}.{sk}" if path else sk
            r = key_rule(ks)
            if r:
                out.append((p, r))
            for h in _shape_rules(ks):
                out.append((p + "(key)", h))
            out.extend(scan_value(v, p, exempt, _depth + 1))
    elif isinstance(obj, (list, tuple)):
        for i, v in enumerate(obj):
            out.extend(scan_value(v, f"{path}[{i}]", exempt, _depth + 1))
    elif isinstance(obj, str):
        if path not in exempt:
            for h in _shape_rules(obj):
                out.append((path, h))
    return out


def _exempt_paths(table: str, row: Mapping[str, Any]) -> frozenset:
    ex: List[str] = []
    spec = {c.name: c for c in SPECS.get(table, ())}
    for k, v in row.items():
        c = spec.get(k)
        if c is None or not isinstance(v, str):
            continue
        if c.kind in ("hex", "uuid", "codesha") and _check_kind(c, v) is None:
            ex.append(k)
    for (t, col), schema in OBJ_SCHEMAS.items():
        if t == table and col in row:
            ex += _schema_hex_paths(schema, row[col], col)
    return frozenset(ex)


def scan_row(table: str, row: Mapping[str, Any]) -> List[str]:
    """Findings as 'table.path:rule' strings. The scan covers every column, including nested JSON, keys and values. A top-level key that is not one of the table's columns is
    scanned as a key like any other (the column names themselves are the schema, not data)."""
    cols = {c.name for c in SPECS.get(table, ())}
    out: List[str] = []
    ex = _exempt_paths(table, row)
    for k, v in dict(row).items():
        ks = str(k)
        findings = scan_value(v, ks, ex) if ks in cols else scan_value({ks: v}, "", ex)
        out += [f"{table}.{p}:{r}" for p, r in findings]
    return out


# ------------------------------------------------------------------ rows, plans

#: Process-private key: only ``registration_puts`` can produce a valid attestation, so a hand-built or ``dataclasses.replace``-edited registration Put cannot carry a digest it did not earn.
_ATTEST_KEY = os.urandom(32)


def _registry_attest(row: Mapping[str, Any]) -> str:
    """Binds a registration row's content to the registry ``entry_digest`` it was built with. Any change to any column, or a different digest, breaks it."""
    body = canonical_json({k: v for k, v in row.items() if k != "content_sha256"})
    return hashlib.sha256(_ATTEST_KEY + b"|registry/1|" + sha256_text(body).encode() + b"|" + str(row.get("content_sha256")).encode()).hexdigest()


@dataclasses.dataclass(frozen=True)
class Put:
    table: str
    row: Mapping[str, Any]
    attest: Optional[str] = None            # registrations only: see _registry_attest


def _put_problems(put: "Put") -> List[str]:
    """Problems that need the Put itself (not just the row): a registration's digest must be the one ``registration_puts`` attested."""
    if put.table == "experiment_registrations" and (not isinstance(put.attest, str) or put.attest != _registry_attest(put.row)):
        return ["content_sha256:registry_digest_not_attested"]
    return []


def _uuid(owner_id: Any) -> str:
    if not isinstance(owner_id, str) or not _UUID_RE.match(owner_id):
        raise SyncError("owner_id is required and must be a lowercase UUID supplied by the caller")
    return owner_id


def _src(source: Optional[str], default: str) -> str:
    s = default if source is None else source
    if not _nb(s):
        raise SyncError("source must be non-blank")
    return s


def _finish(table: str, owner_id: str, cols: Mapping[str, Any], *, source: str, content: Optional[str] = None) -> Put:
    """Add provenance (source, schema_version, content_sha256), drop omitted optionals, validate, secret-scan. The ONLY way a builder makes a row."""
    row: Dict[str, Any] = {"owner_id": _uuid(owner_id)}
    for k, v in cols.items():
        if v is not None:
            row[k] = normalise(v)
    row["source"] = _src(source, source)
    row["schema_version"] = SCHEMA_VERSION
    rule = HASH_RULE[table]
    if content is not None:
        row["content_sha256"] = content
    elif rule == "policy":
        row["content_sha256"] = content_sha256(row["policy"])
    elif rule == "import":
        h = row_hash(table, row)
        row["content_sha256"] = h
        row["import_id"] = h
    else:
        row["content_sha256"] = row_hash(table, row)
    found = scan_row(table, row)                    # the guard runs FIRST and on every key and value, whatever the schema says
    if found:
        raise SecretGuardError(found)
    bad = row_problems(table, row)
    if bad:
        raise SyncError(f"{table}: {', '.join(bad[:6])}")
    return Put(table, row)


def _json_col(v: Any) -> Any:
    """A jsonb column that must be an object/array: a scalar is wrapped, never dropped."""
    return v if isinstance(v, (dict, list)) else {"value": v}


def _hash_or_digest(v: Any, what: str) -> str:
    if isinstance(v, str) and _HEX64.match(v):
        return v
    if isinstance(v, Mapping) and v:
        return content_sha256(v)       # a map of {input name: sha256}: its canonical digest stands for the set
    raise SyncError(f"{what} must be a sha256 hex string or a non-empty map of them")


# ---- hypotheses / registrations / evidence
def _registry_digest(entry: Mapping[str, Any]) -> str:
    import hypothesis_registry as hr
    return hr.entry_digest(dict(entry))


def registrable(entry: Mapping[str, Any]) -> List[str]:
    """What is missing for this registry entry to be an experiment registration (the legacy seed rows are not)."""
    import hypothesis_registry as hr
    miss = [k for k in hr.REQUIRED_REGISTRATION if entry.get(k) in (None, "", [], {})]
    if not entry.get("registered_utc"):
        miss.append("registered_utc")
    return miss


def _verified_registry(registry: Any) -> Mapping[str, Any]:
    """The registry must pass ``hypothesis_registry.verify`` (sealed; no trial altered, backdated, removed or un-sealed; no evidence wiped or edited). Refused by problem name."""
    import hypothesis_registry as hr
    if not isinstance(registry, Mapping) or not isinstance(registry.get("trials"), list):
        raise SyncError("registry is not a registry mapping with a trials list")
    try:
        problems = hr.verify(registry)          # type: ignore[arg-type]
    except (AttributeError, TypeError, ValueError, KeyError):
        raise SyncError("registry is malformed: hypothesis_registry.verify could not read it") from None
    if problems:
        raise SyncError("registry refused (hypothesis_registry.verify): " + "; ".join(str(x)[:160] for x in problems[:4]))
    return registry


def registration_puts(owner_id: str, entry: Mapping[str, Any], *, registry: Mapping[str, Any], source: Optional[str] = None) -> List[Put]:
    """hypothesis row + experiment_registrations row for one registry entry (the registry's own entry_digest is the registration's content_sha256). The REGISTRY the entry lives in
    is required and is verified first (``hypothesis_registry.verify``): an entry cannot be synced out of a registry that was backdated, wiped or never sealed, and ``entry`` must be
    that registry's own, unaltered entry. ``registered_utc`` is still CLIENT-SUPPLIED data (see the migration note): the seal proves it was not edited afterwards, not that the
    clock was honest; consumers must rely on the row's ``created_at`` for when the registration reached the database."""
    import hypothesis_registry as hr
    reg = _verified_registry(registry)
    mine = next((t for t in reg["trials"] if isinstance(t, Mapping) and t.get("trial_id") == entry.get("trial_id")), None)
    if mine is None or hr.entry_digest(dict(mine)) != hr.entry_digest(dict(entry)):
        raise SyncError(f"{entry.get('trial_id')!r} is not an unaltered entry of the verified registry")
    return _registration_puts(owner_id, entry, source=source)


def _registration_puts(owner_id: str, entry: Mapping[str, Any], *, source: Optional[str] = None) -> List[Put]:
    miss = registrable(entry)
    if miss:
        raise SyncError(f"{entry.get('trial_id')!r} is not an experiment registration: missing {miss}")
    src = _src(source, "tools/hypothesis_registry.py")
    hyp = _finish("strategy_hypotheses", owner_id, {
        "hypothesis_id": str(entry["hypothesis_id"]), "mechanism": entry["mechanism"], "family": entry["family"],
        "statement": entry.get("statement") or entry["mechanism"]}, source=src)
    pp = entry["prospective_period"]
    reg = _finish("experiment_registrations", owner_id, {
        "trial_id": entry["trial_id"], "hypothesis_id": str(entry["hypothesis_id"]), "mechanism": entry["mechanism"], "family": entry["family"],
        "strategy": entry["strategy"], "universe": _json_col(entry["universe"]), "input_data_sha256": _hash_or_digest(entry["input_data_sha256"], "input_data_sha256"),
        "code_sha": entry["code_sha"], "data_window": _json_col(entry["data_window"]), "parameter_grid": _json_col(entry["parameter_grid"]),
        "costs": _json_col(entry["costs"]), "statistical_design": _json_col(entry["statistical_design"]), "train_test": _json_col(entry["train_test"]),
        "multiple_testing_family": str(entry["multiple_testing_family"]), "prospective_period": pp if isinstance(pp, str) else canonical_json(pp),
        "registered_utc": utc(entry["registered_utc"]), "note": entry.get("note")}, source=src, content=_registry_digest(entry))
    return [hyp, Put(reg.table, reg.row, attest=_registry_attest(reg.row))]


_IMPORT_REF = re.compile(r"\bimp_[0-9a-f]{32}\b")


def registry_evidence_details(details: Any) -> Tuple[Dict[str, Any], List[str]]:
    """The DELIBERATE mapping of a registry / strategy_verdict evidence ``details`` onto the whitelisted evidence columns. Dropped, and returned by name so the caller can report it:
    ``attachment`` (the owner's whole import report: privacy, never synced), ``candidate_id`` / ``spec_digest`` (binding words, not evidence), and the metrics
    ``net_usd_after_all_costs`` / ``owner_figure_basis`` / ``import_status`` (the owner's figure is not stored in Supabase: it lives in the hashed report; the hash is kept). Everything else
    is passed through and judged by the whitelist as usual: an unlisted field is a REFUSAL, never a silent strip. Hash fields (``import_report_sha256``, ``execution_log_sha256``,
    ``data_sha256``) are typed columns, exempt from the secret guard's long-hex rule only when they are exactly a sha256."""
    if not isinstance(details, Mapping):
        return {}, []
    out = dict(details)
    dropped = [k for k in EVIDENCE_DROPPED_DETAILS if k in out]
    for k in EVIDENCE_DROPPED_DETAILS:
        out.pop(k, None)
    m = out.get("metrics")
    if isinstance(m, Mapping):
        m = dict(m)
        dropped += [f"metrics.{k}" for k in EVIDENCE_DROPPED_METRICS if k in m]
        for k in EVIDENCE_DROPPED_METRICS:
            m.pop(k, None)
        out["metrics"] = m
    return out, dropped


def _evidence_provenance(prov: Any) -> Any:
    """An importer id (``imp_`` + 32 hex) in a provenance line is a hash-shaped token the guard rightly refuses as free text; the bound sha256 is in the typed details field instead."""
    return _IMPORT_REF.sub("imp_ref", prov) if isinstance(prov, str) else prov


def evidence_put(owner_id: str, trial_id: str, level: str, *, result: str, provenance: str, details: Optional[Mapping[str, Any]] = None, recorded_utc: Any,
                 source: Optional[str] = None) -> Put:
    if level not in EVIDENCE_LEVELS:
        raise SyncError(f"evidence level {level!r} is not one of the seven")
    return _finish("experiment_evidence", owner_id, {"trial_id": trial_id, "level": level, "result": result, "provenance": provenance, "details": dict(details or {}),
                                                     "recorded_utc": utc(recorded_utc)}, source=_src(source, "tools/hypothesis_registry.py"))


def verdict_evidence_put(owner_id: str, trial_id: str, item: Any, *, source: Optional[str] = None) -> Put:
    """One strategy_verdict EvidenceItem (or its dict) as registry evidence, mapped exactly as ``strategy_verdict.record_evidence`` maps it."""
    import strategy_verdict as sv
    g = (lambda k, d=None: item.get(k, d)) if isinstance(item, Mapping) else (lambda k, d=None: getattr(item, k, d))
    stage = g("stage")
    if stage not in sv.STAGE_TO_REGISTRY_LEVEL:
        raise SyncError(f"unknown evidence stage {stage!r}")
    ms = g("recorded_ms") or 0
    if not ms:
        raise SyncError("evidence has no recorded_ms: refusing to invent a time")
    details, _dropped = registry_evidence_details({"metrics": dict(g("metrics") or {}), "data_sha256": g("data_sha256"), "status": g("status")})
    return evidence_put(owner_id, trial_id, sv.STAGE_TO_REGISTRY_LEVEL[stage], result=f"{stage}:{g('result')}", provenance=_evidence_provenance(g("provenance") or "none"),
                        details=details, recorded_utc=int(ms), source=_src(source, "tools/strategy_verdict.py"))


def registry_puts(owner_id: str, registry: Mapping[str, Any], *, source: Optional[str] = None) -> Tuple[List[Put], List[str]]:
    """Every registrable trial with its evidence, amendments, abandonment and supersession. Entries that are not registrations (the legacy seed) are SKIPPED WITH A
    REASON, never silently. A supersession whose successor is not itself registrable is skipped and reported."""
    _verified_registry(registry)                    # refuse a backdated / wiped / unsealed registry before anything is mapped
    puts: List[Put] = []
    skipped: List[str] = []
    ok_ids = set()
    trials = [t for t in registry.get("trials", []) if isinstance(t, Mapping)]
    for t in trials:
        miss = registrable(t)
        if miss:
            skipped.append(f"{t.get('trial_id')}: not a registration (missing {','.join(miss[:4])})")
        else:
            ok_ids.add(t["trial_id"])
    src = _src(source, "tools/hypothesis_registry.py")
    for t in trials:
        if t.get("trial_id") not in ok_ids:
            continue
        puts += _registration_puts(owner_id, t, source=src)
        tid = t["trial_id"]
        for i, ev in enumerate(t.get("evidence", []) or []):
            det, dropped = registry_evidence_details(ev.get("details"))
            if dropped:
                skipped.append(f"{tid}: evidence[{i}] not synced by design: {', '.join(dropped)}")
            puts.append(evidence_put(owner_id, tid, ev["level"], result=ev["result"], provenance=_evidence_provenance(ev["provenance"]), details=det, recorded_utc=ev["recorded_utc"], source=src))
        for am in t.get("amendments", []) or []:
            puts.append(_finish("experiment_amendments", owner_id, {"trial_id": tid, "what": am["what"], "why": am["why"], "recorded_utc": utc(am["recorded_utc"])}, source=src))
        ab = t.get("abandoned")
        if ab:
            puts.append(_finish("experiment_abandonments", owner_id, {"trial_id": tid, "reason": ab["reason"], "recorded_utc": utc(ab["recorded_utc"])}, source=src))
        if t.get("superseded_by"):
            if t["superseded_by"] in ok_ids:
                puts.append(_finish("experiment_supersessions", owner_id, {"trial_id": tid, "superseded_by": t["superseded_by"], "reason": t.get("superseded_reason"),
                                                                          "recorded_utc": utc(t.get("superseded_utc"))}, source=src))
            else:
                skipped.append(f"{tid}: supersession by unregistered {t['superseded_by']!r} skipped")
    return puts, skipped


# ---- market-data manifests and receipts
def data_batch_put(owner_id: str, b: Mapping[str, Any], *, source: Optional[str] = None) -> Put:
    return _finish("data_batches", owner_id, {
        "batch_id": b["batch_id"], "dataset": b["dataset"], "venue": b["venue"], "batch_sha256": b["batch_sha256"], "byte_count": b["byte_count"], "row_count": b.get("row_count"),
        "segment_ids": list(b["segment_ids"]), "window_start_utc": utc(b["window_start_utc"]), "window_end_utc": utc(b["window_end_utc"]), "storage_ref": b["storage_ref"]},
        source=_src(source, "tools/md_seal.py"))


def md_seal_receipt_put(owner_id: str, *, seal_id: str, batch_id: str, manifest_sha256: str, manifest_rows: int, sealed_utc: Any, receipt: Optional[Mapping[str, Any]] = None,
                        source: Optional[str] = None) -> Put:
    rc = dict(receipt or {})
    return _finish("md_seal_receipts", owner_id, {"seal_id": seal_id, "batch_id": batch_id, "manifest_sha256": manifest_sha256, "manifest_rows": manifest_rows,
                                                  "receipt_sha256": content_sha256(rc), "sealed_utc": utc(sealed_utc), "receipt": rc}, source=_src(source, "tools/md_seal.py"))


def md_segment_puts(owner_id: str, segment: Mapping[str, Any], *, venue: str, byte_count: int, storage_ref: str, manifest_sha256: str, manifest_rows: int, sealed_ms: int,
                    source: Optional[str] = None) -> List[Put]:
    """A CLOSED md_seal segment description (``md_seal.describe``) -> its data_batches row and md_seal_receipts row. An open segment has no file hash and is refused."""
    name = str(segment.get("segment", ""))
    m = re.fullmatch(r"md-(\d{4})(\d\d)(\d\d)\.db", name)
    if not m or not segment.get("closed") or not _HEX64.match(str(segment.get("sha256", ""))):
        raise SyncError("only a closed, hash-pinned md-YYYYMMDD.db segment can be recorded")
    day = f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    bid = f"md-{m.group(1)}{m.group(2)}{m.group(3)}"
    batch = data_batch_put(owner_id, {"batch_id": bid, "dataset": "md_log", "venue": venue, "batch_sha256": segment["sha256"], "byte_count": byte_count,
                                      "row_count": segment["records"], "segment_ids": [name], "window_start_utc": f"{day}T00:00:00Z", "window_end_utc": f"{day}T23:59:59Z",
                                      "storage_ref": storage_ref}, source=source)
    rc = md_seal_receipt_put(owner_id, seal_id=f"seal-{bid}-{segment['last_chain'][:16]}", batch_id=bid, manifest_sha256=manifest_sha256, manifest_rows=manifest_rows,
                             sealed_utc=sealed_ms, receipt={k: segment[k] for k in ("segment", "records", "first_id", "last_id", "last_chain", "sha256") if k in segment}, source=source)
    return [batch, rc]


def universe_snapshot_put(owner_id: str, manifest: Mapping[str, Any], *, venue: str, manifest_ref: str, manifest_file_sha256: Optional[str] = None,
                          source: Optional[str] = None) -> Put:
    """``universe_capture`` manifest -> one snapshot row (counts, hash, ref only; the pages, rejected rows and instruments stay in the manifest file)."""
    ms = manifest.get("snapshot_ms")
    inst = (manifest.get("universe") or {}).get("instruments")
    if not isinstance(ms, int) or not isinstance(inst, int):
        raise SyncError("not a universe_capture manifest")
    return _finish("instrument_universe_snapshots", owner_id, {
        "snapshot_id": f"universe-{ms}", "venue": venue, "captured_utc": utc(ms), "manifest_sha256": manifest_file_sha256 or content_sha256(manifest), "manifest_ref": manifest_ref,
        "instrument_count": inst}, source=_src(source, "tools/universe_capture.py"))


def collector_status_put(owner_id: str, collector_name: str, state: str, observed_utc: Any, *, last_record_utc: Any = None, lag_seconds: Any = None,
                         detail: Optional[Mapping[str, Any]] = None, source: Optional[str] = None) -> Put:
    return _finish("collector_status", owner_id, {"collector_name": collector_name, "state": state, "observed_utc": utc(observed_utc),
                                                  "last_record_utc": None if last_record_utc is None else utc(last_record_utc),
                                                  "lag_seconds": None if lag_seconds is None else canon_decimal(lag_seconds), "detail": dict(detail or {})},
                   source=_src(source, "collector"))


def system_health_put(owner_id: str, component: str, state: str, observed_utc: Any, *, detail: Optional[Mapping[str, Any]] = None, source: Optional[str] = None) -> Put:
    return _finish("system_health", owner_id, {"component": component, "state": state, "observed_utc": utc(observed_utc), "detail": dict(detail or {})},
                   source=_src(source, "health"))


# ---- owner-account import REPORTS: metadata and the status word only
_PROFIT_STATUSES = ("RECONCILED", "PROVISIONAL", "ESTIMATED")
_IMPORT_ID_RE = re.compile(r"^imp_[0-9a-f]{32}$")
_FIG_KEYS = ("value", "formula", "sources", "derived_digests", "all_records_digest", "empty_set")


def _imp_canon(obj: Any) -> str:
    """tools/owner_import._canon, minus ``default=str`` (a report is plain JSON: anything else is not an importer report)."""
    try:
        return json.dumps(obj, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError, RecursionError, OverflowError) as exc:
        raise SyncError(f"report is not plain JSON ({type(exc).__name__})") from None


def _path_get(rep: Any, path: str) -> Any:
    cur = rep
    for part in path.split("."):
        if not isinstance(cur, Mapping) or part not in cur:
            raise KeyError(path)
        cur = cur[part]
    return cur


#: The importer's own file roles (``tools/owner_import.ROLES``; a test pins parity). A RECONCILED report needs the executions, the transaction log and BOTH wallet snapshots.
IMPORT_ROLES = ("linear_execution", "spot_execution", "transaction_log", "wallet_opening", "wallet_closing")
RECONCILED_ROLES = ("linear_execution", "transaction_log", "wallet_opening", "wallet_closing")
#: A RECONCILED/VERIFIED account window is at least one day (three funding settlements) long, holds at least one perp execution and one funding settlement.
MIN_RECONCILED_WINDOW_DAYS = 1.0
_FIGURE_COMPONENTS = ("perp_realised_usd", "perp_fees_usd", "funding_net_usd", "interest_usd", "other_cash_lines_usd")
_DAY_MS = 86_400_000


def _fnum(v: Any) -> Optional[float]:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) else None


def _close(a: float, b: float) -> bool:
    return math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-6)


def _semantic_problems(report: Mapping[str, Any], status: str, layers: Mapping[str, Any], real: Optional[Mapping[str, Any]]) -> List[str]:
    """Recompute what the digests cannot protect: a digest only proves the report is unaltered SINCE it was sealed, and a forger who edits a figure simply reseals. So the
    arithmetic the importer did is redone here from the report's own parts: realised net = the sum of its five components; economic net = realised net + BTC economic P&L +
    change in perpetual unrealised P&L; partial net = the sum of the known components; a RECONCILED/VERIFIED account has a real window (>= a day, internally consistent), non-zero
    execution and funding counts, and lists the executions, the transaction log and both wallet files. This remains a consistency check: the source files are NOT available here,
    so a forger who edits every figure coherently and reseals still passes it. The owner's files (their sha256 are listed in ``lineage.files``) are what could refute that."""
    out: List[str] = []
    if real is not None:
        figs = real.get("figures")
        net = real.get("net_realised_cash_usd")
        partial = real.get("partial_net_of_known_components_usd")
        if not isinstance(figs, Mapping) or any(k not in figs for k in _FIGURE_COMPONENTS):
            out.append("realised_cash:figures_missing")
        else:
            vals = {k: figs[k] for k in _FIGURE_COMPONENTS}
            if any(v is not None and _fnum(v) is None for v in vals.values()):
                out.append("realised_cash:figure_not_a_number")
            else:
                known = [float(v) for v in vals.values() if v is not None]
                if net is not None:
                    if len(known) != len(vals):
                        out.append("realised_cash:net_present_with_an_unknown_component")
                    elif _fnum(net) is None or not _close(float(net), math.fsum(known)):
                        out.append("realised_cash:net_is_not_the_sum_of_its_components")
                if partial is not None and (_fnum(partial) is None or not _close(float(partial), math.fsum(known))):
                    out.append("realised_cash:partial_net_is_not_the_sum_of_the_known_components")
        if status in ("VERIFIED", "RECONCILED") and net is None:
            out.append(f"realised_cash:no_net_under_{status}")
    econ = layers.get("economic_total") if isinstance(layers.get("economic_total"), Mapping) else None
    inv = layers.get("inventory_valuation") if isinstance(layers.get("inventory_valuation"), Mapping) else None
    if econ is not None and econ.get("net_economic_pnl_usd") is not None:
        en = _fnum(econ.get("net_economic_pnl_usd"))
        parts = [_fnum((real or {}).get("net_realised_cash_usd")), _fnum((inv or {}).get("btc_economic_pnl_usd")), _fnum((inv or {}).get("change_in_perp_unrealised_usd"))]
        if en is None or any(x is None for x in parts):
            out.append("economic_total:net_without_all_of_its_components")
        elif not _close(en, math.fsum(parts)):
            out.append("economic_total:net_is_not_the_sum_of_its_components")
    if status in ("VERIFIED", "RECONCILED"):
        lin = report.get("lineage") if isinstance(report.get("lineage"), Mapping) else {}
        files = lin.get("files") if isinstance(lin.get("files"), list) else []
        roles = [f.get("role") for f in files if isinstance(f, Mapping)]
        if any(r not in IMPORT_ROLES for r in roles):
            out.append("lineage.files:unknown_role")
        missing = [r for r in RECONCILED_ROLES if r not in roles]
        if missing:
            out.append(f"lineage.files:{status}_lacks_{'+'.join(missing)}")
        led = report.get("ledger") if isinstance(report.get("ledger"), Mapping) else {}
        win = led.get("window") if isinstance(led.get("window"), Mapping) else {}
        lo, hi = win.get("opening_ts_ms"), win.get("closing_ts_ms")
        if not all(isinstance(x, int) and not isinstance(x, bool) for x in (lo, hi)) or hi <= lo:
            out.append("ledger.window:missing_or_not_increasing")
        else:
            days = (hi - lo) / _DAY_MS
            if days < MIN_RECONCILED_WINDOW_DAYS:
                out.append(f"ledger.window:shorter_than_{MIN_RECONCILED_WINDOW_DAYS:g}_day_for_{status}")
            if _fnum(win.get("days")) is None or not _close(float(win["days"]), days):
                out.append("ledger.window:days_do_not_match_the_timestamps")
        rec = led.get("records") if isinstance(led.get("records"), Mapping) else {}
        for k in ("perp_executions", "funding_settlements"):
            v = rec.get(k)
            if not isinstance(v, int) or isinstance(v, bool) or v < 1:
                out.append(f"ledger.records.{k}:zero_or_missing_under_{status}")
    return out


def verify_import_report(report: Any) -> List[str]:
    """Why a report is NOT a valid, self-consistent ``owner_import`` report (empty = valid). Recomputes ``report_sha256`` (the importer's ``_seal`` digest of the body), the
    lineage ``digest`` (files, marks, stages, and every figure's value + provenance digest, which is itself recomputed), then checks the status against its own layers
    and headline. A hand-made dict cannot pass: it would have to be a correctly sealed importer report. It proves the report is INTERNALLY consistent and unaltered since sealing; like
    the importer itself, it does not prove the owner's files are the exchange's."""
    if not isinstance(report, Mapping):
        return ["report:not_a_mapping"]
    out: List[str] = []
    status = report.get("status")
    if status not in OWNER_STATUSES:
        return ["status:not_in_the_eight_words"]
    if not isinstance(report.get("import_id"), str) or not _IMPORT_ID_RE.match(report["import_id"]):
        out.append("import_id:malformed")
    rs = report.get("report_sha256")
    if not isinstance(rs, str) or not _HEX64.match(rs):
        return out + ["report_sha256:missing_or_malformed"]
    try:
        if sha256_text(_imp_canon({k: v for k, v in report.items() if k != "report_sha256"})) != rs:
            return out + ["report_sha256:does_not_recompute"]
    except SyncError as exc:
        return out + [f"report:{exc}"]
    lin = report.get("lineage")
    if not isinstance(lin, Mapping):
        return out + ["lineage:missing"]
    files = lin.get("files")
    if not isinstance(files, list) or not files or not all(isinstance(f, Mapping) and isinstance(f.get("role"), str) and _HEX64.match(str(f.get("sha256", ""))) for f in files):
        return out + ["lineage.files:missing_or_malformed"]
    figs = lin.get("figures")
    if not isinstance(figs, Mapping) or not isinstance(lin.get("stages"), Mapping):
        return out + ["lineage:figures_or_stages_missing"]
    fhash: Dict[str, Any] = {}
    for path, e in sorted(figs.items()):
        if not isinstance(e, Mapping):
            return out + ["lineage.figures:malformed"]
        try:
            v = _path_get(report, path)
        except KeyError:
            return out + ["lineage.figures:path_not_in_report"]
        if e.get("value") != v:
            return out + ["lineage.figures:value_differs_from_report"]
        try:
            pd = sha256_text(_imp_canon({k: e.get(k) for k in _FIG_KEYS}))
        except SyncError as exc:
            return out + [f"lineage.figures:{exc}"]
        if e.get("provenance_digest") != pd:
            return out + ["lineage.figures:provenance_digest_does_not_recompute"]
        fhash[path] = [v, pd]
    try:
        ld = sha256_text(_imp_canon({"files": [[f["role"], f["sha256"]] for f in files], "marks": lin.get("marks"), "stages": lin["stages"], "figures": fhash}))
    except SyncError as exc:
        return out + [f"lineage:{exc}"]
    if lin.get("digest") != ld:
        return out + ["lineage.digest:does_not_recompute"]
    layers = report.get("layers")
    layers = layers if isinstance(layers, Mapping) else {}
    for name, layer in layers.items():
        ls = layer.get("status") if isinstance(layer, Mapping) else None
        if ls not in OWNER_STATUSES:
            out.append(f"layers.{_safe_name(name)}:status_not_in_the_eight_words")
        elif ls in ("VERIFIED", "RECONCILED") and status not in ("VERIFIED", "RECONCILED"):
            out.append(f"layers.{_safe_name(name)}:claims_{ls}_under_a_weaker_report_status")
    real = layers.get("realised_cash") if isinstance(layers.get("realised_cash"), Mapping) else None
    if status in ("VERIFIED", "RECONCILED") and real is None:
        out.append(f"status:{status}_without_a_realised_cash_layer")
    if real is not None and real.get("status") != status:
        out.append("status:differs_from_the_realised_cash_layer")
    out += _semantic_problems(report, status, layers, real)
    headline = report.get("headline_realised_cash_usd")
    if headline is not None:
        net = real.get("net_realised_cash_usd") if real is not None else None
        if status not in _PROFIT_STATUSES:
            out.append(f"headline:non_null_under_{status}")
        elif net is None or headline != net:
            out.append("headline:not_the_realised_cash_net")
        if report.get("headline_basis") != "realised_cash_only":
            out.append("headline:basis_not_declared_realised_cash_only")
    return out


def owner_import_puts(owner_id: str, report: Mapping[str, Any], *, imported_utc: Any, storage_ref: str, source_sha256: Optional[str] = None, kind: str = "owner_import",
                      source: Optional[str] = None) -> List[Put]:
    """A VALID, SEALED ``owner_import`` report -> ONE owner_account_imports row. The report is first verified (``verify_import_report``: digests recompute, status in the eight
    words and consistent with its layers and headline); any problem refuses it, by name. Only this whitelist is then read: ``status``, ``report_sha256`` (stored so a VERIFIED/RECONCILED row names the sealed report it comes from; the database requires it), ``lineage.files[*].sha256`` (digested into
    the source hash unless ``source_sha256`` is given), ``ledger.window`` opening/closing ms if both are integers, and the number of lineage files. The report's rows, figures,
    balances, flags, marks and identifiers are never copied. import_id = content hash of the stored metadata."""
    problems = verify_import_report(report)
    if problems:
        raise SyncError("import report refused: " + ", ".join(problems[:6]))
    status = report["status"]
    if source_sha256 is None:
        files = report["lineage"]["files"]
        source_sha256 = content_sha256(sorted(str(f["sha256"]) for f in files))
    win = (report.get("ledger") or {}).get("window") or {}
    lo, hi = (win.get("opening_ts_ms"), win.get("closing_ts_ms")) if isinstance(win, Mapping) else (None, None)
    period = {}
    if isinstance(lo, int) and isinstance(hi, int) and not isinstance(lo, bool) and not isinstance(hi, bool) and hi >= lo:
        period = {"period_start_utc": utc(lo), "period_end_utc": utc(hi)}
    nfiles = len(report["lineage"]["files"])
    return [_finish("owner_account_imports", owner_id, {"source_sha256": source_sha256, "kind": kind, **period, "row_count": nfiles or None, "storage_ref": storage_ref,
                                                         "status": status, "report_sha256": report["report_sha256"], "imported_utc": utc(imported_utc)}, source=_src(source, "tools/owner_import.py"))]


def import_status_event_put(owner_id: str, import_id: str, status: str, recorded_utc: Any, *, reason: Optional[str] = None, evidence_ref: Optional[str] = None,
                            source: Optional[str] = None) -> Put:
    """``evidence_ref`` is a sha256 content hash, never free text; a VERIFIED/RECONCILED event must carry the hash of the import row it names (``import_id``) and the plan must
    contain that row with the same status (``cross_problems``). ``reason`` is an UPPER_SNAKE reason code, never prose."""
    return _finish("owner_account_import_status_events", owner_id, {"import_id": import_id, "status": status, "reason": reason, "evidence_ref": evidence_ref,
                                                                    "recorded_utc": utc(recorded_utc)}, source=_src(source, "tools/owner_import.py"))


# ---- risk policy versions and approval records
def risk_policy_put(owner_id: str, policy_name: str, version: int, policy: Any, effective_from_utc: Any, *, source: Optional[str] = None) -> Put:
    """The policy DOCUMENT is the 14 limits, as exact decimal strings. ``approved_by_owner``/``approval_note`` of ``risk_policy.Policy`` are NOT copied: approval is its own
    record (``approval_put``), and a policy row can never claim it."""
    d = dataclasses.asdict(policy) if dataclasses.is_dataclass(policy) and not isinstance(policy, type) else dict(policy)
    miss = [k for k in RISK_LIMIT_FIELDS if k not in d]
    if miss:
        raise SyncError(f"policy lacks limits {miss}")
    doc = {k: canon_decimal(d[k]) for k in RISK_LIMIT_FIELDS}
    if isinstance(version, bool) or not isinstance(version, int):
        raise SyncError("version must be an integer")
    return _finish("risk_policy_versions", owner_id, {"policy_name": policy_name, "version": version, "policy": doc, "effective_from_utc": utc(effective_from_utc)},
                   source=_src(source, "risk_policy.py"))


def approval_put(owner_id: str, approval_id: str, subject_kind: str, subject_ref: str, approved_by_owner: bool, decided_utc: Any, *, approver_id: Optional[str] = None,
                 statement_sha256: Optional[str] = None, note: Optional[str] = None, source: Optional[str] = None) -> Put:
    """``approved_by_owner`` is REQUIRED and must be a real bool: it is never inferred from a policy object, a config or a default. True needs the owner as approver and
    the hash of the statement approved (the table enforces the same). No subject kind can name the promotion gate."""
    if not isinstance(approved_by_owner, bool):
        raise SyncError("approved_by_owner must be stated as a bool")
    return _finish("approval_records", owner_id, {"approval_id": approval_id, "subject_kind": subject_kind, "subject_ref": subject_ref, "approved_by_owner": approved_by_owner,
                                                  "approver_id": approver_id, "statement_sha256": statement_sha256, "note": note, "decided_utc": utc(decided_utc)},
                   source=_src(source, "owner"))


# ---- incidents, backups, restores
def incident_put(owner_id: str, incident_id: str, severity: str, title: str, opened_utc: Any, *, runbook_ref: Optional[str] = None, source: Optional[str] = None) -> Put:
    return _finish("incidents", owner_id, {"incident_id": incident_id, "severity": severity, "title": title, "runbook_ref": runbook_ref, "opened_utc": utc(opened_utc)},
                   source=_src(source, "RUNBOOKS.md"))


def incident_event_put(owner_id: str, incident_id: str, state: str, recorded_utc: Any, *, note: Optional[str] = None, source: Optional[str] = None) -> Put:
    return _finish("incident_events", owner_id, {"incident_id": incident_id, "state": state, "note": note, "recorded_utc": utc(recorded_utc)}, source=_src(source, "RUNBOOKS.md"))


def backup_receipt_put(owner_id: str, backup_id: str, target: str, artifact_sha256: str, byte_count: int, storage_ref: str, taken_utc: Any, *, source: Optional[str] = None) -> Put:
    return _finish("backup_receipts", owner_id, {"backup_id": backup_id, "target": target, "artifact_sha256": artifact_sha256, "byte_count": byte_count,
                                                 "storage_ref": storage_ref, "taken_utc": utc(taken_utc)}, source=_src(source, "tools/md_seal.py"))


def restore_receipt_put(owner_id: str, restore_id: str, backup_id: str, outcome: str, verification_sha256: str, restored_utc: Any, *, checks: Optional[Mapping[str, Any]] = None,
                        source: Optional[str] = None) -> Put:
    return _finish("restore_receipts", owner_id, {"restore_id": restore_id, "backup_id": backup_id, "outcome": outcome, "verification_sha256": verification_sha256,
                                                  "checks": dict(checks or {}), "restored_utc": utc(restored_utc)}, source=_src(source, "tools/md_seal.py"))


def cross_problems(puts: Iterable[Put]) -> List[str]:
    """Rules that span rows of ONE plan. A VERIFIED/RECONCILED status event needs the import row it names (same import_id) in the same plan, carrying the same status:
    an event can never promote an import the plan does not itself contain."""
    flat = list(puts)
    imports = {p.row.get("import_id"): p.row.get("status") for p in flat if p.table == "owner_account_imports"}
    out: List[str] = []
    for i, p in enumerate(flat):
        if p.table == "owner_account_import_status_events" and p.row.get("status") in ("VERIFIED", "RECONCILED"):
            if imports.get(p.row.get("import_id")) != p.row.get("status"):
                out.append(f"{p.table}[{i}]: {p.row.get('status')} event has no matching import row with that hash and status in the plan")
    return out


# ------------------------------------------------------------------ plan
@dataclasses.dataclass(frozen=True)
class Plan:
    owner_id: str
    puts: Tuple[Put, ...]                   # the exact ordered carry.put_row calls
    conflicts: Tuple[Put, ...] = ()         # in-plan conflicting duplicates: NOT sent, reported by write()
    skipped: Tuple[str, ...] = ()           # inputs the caller flagged as not mappable (reported, never silent)
    collapsed: int = 0                      # identical in-plan duplicates merged

    def calls(self) -> List[Tuple[str, Dict[str, Any]]]:
        return [(p.table, dict(p.row)) for p in self.puts]

    def digest(self) -> str:
        return content_sha256([[p.table, dict(p.row)] for p in self.puts])


def plan(owner_id: str, *groups: Iterable[Put], skipped: Sequence[str] = ()) -> Plan:
    """Validate, secret-scan (whole plan refused on any finding), de-duplicate and FK-order every Put. Identical duplicates collapse; a same-key/different-hash pair keeps the
    first and reports the second as a conflict."""
    _uuid(owner_id)
    flat: List[Put] = [p for g in groups for p in g]
    findings: List[str] = []
    for i, p in enumerate(flat):
        if not isinstance(p, Put) or p.table not in SPECS:
            raise SyncError(f"item {i} is not a Put for a known table")
        if p.row.get("owner_id") != owner_id:
            raise SyncError(f"{p.table}[{i}] belongs to a different owner_id than the plan")
        findings += [f"{x} (item {i})" for x in scan_row(p.table, p.row)]
    if findings:
        raise SecretGuardError(findings)
    for i, p in enumerate(flat):
        bad = row_problems(p.table, p.row) + _put_problems(p)
        if bad:
            raise SyncError(f"{p.table}[{i}]: {', '.join(bad[:6])}")
    cross = cross_problems(flat)
    if cross:
        raise SyncError("; ".join(cross[:6]))
    seen_hash: Dict[Tuple[str, str], Tuple[Any, ...]] = {}
    kept_row: Dict[Tuple[str, str], Mapping[str, Any]] = {}
    seen_key: Dict[Tuple[str, Tuple[Any, ...]], str] = {}
    kept: List[Put] = []
    conflicts: List[Put] = []
    collapsed = 0
    for p in flat:
        nk = tuple(p.row.get(c) for c in NATURAL_KEY[p.table])
        h = p.row["content_sha256"]
        if (p.table, h) in seen_hash:
            if seen_hash[(p.table, h)] == nk and dict(kept_row[(p.table, h)]) == dict(p.row):          # a collapse needs the SAME row, not merely the same digest
                collapsed += 1
            else:
                conflicts.append(p)
            continue
        if NATURAL_KEY[p.table] and (p.table, nk) in seen_key:
            conflicts.append(p)
            continue
        seen_hash[(p.table, h)] = nk
        kept_row[(p.table, h)] = p.row
        if NATURAL_KEY[p.table]:
            seen_key[(p.table, nk)] = h
        kept.append(p)
    order = {t: i for i, t in enumerate(TABLE_ORDER)}
    kept = sorted(kept, key=lambda p: (order[p.table], p.row["content_sha256"]))        # deterministic whatever the input order; rows of one table never depend on each other
    return Plan(owner_id, tuple(kept), tuple(conflicts), tuple(skipped), collapsed)


# ------------------------------------------------------------------ write
UNCHANGED, INSERTED, CONFLICT, FAILED, NOT_SENT = "UNCHANGED", "INSERTED", "CONFLICT", "FAILED", "NOT_SENT"


@dataclasses.dataclass(frozen=True)
class CallResult:
    table: str
    key: Tuple[Any, ...]
    outcome: str
    detail: str = ""


@dataclasses.dataclass(frozen=True)
class WriteReport:
    results: Tuple[CallResult, ...]
    skipped: Tuple[str, ...] = ()

    def count(self, outcome: str) -> int:
        return sum(1 for r in self.results if r.outcome == outcome)

    @property
    def ok(self) -> bool:
        """True only when every row is INSERTED or UNCHANGED: a conflict, failure or unsent row is NOT ok."""
        return all(r.outcome in (INSERTED, UNCHANGED) for r in self.results)

    def summary(self) -> Dict[str, int]:
        return {o: self.count(o) for o in (INSERTED, UNCHANGED, CONFLICT, FAILED, NOT_SENT)}


class ServiceRoleClient(abc.ABC):
    """The ONLY kind of client ``write`` accepts. The owner's server-side code subclasses it (or deliberately ``ServiceRoleClient.register``s a class), implements the two
    methods against the owner's OWN project with the service role, and keeps ``role == "service_role"``. A client that merely has a ``role`` attribute is refused."""
    role: str = "service_role"

    @abc.abstractmethod
    def put_row(self, table: str, row: Mapping[str, Any]) -> bool:
        """carry.put_row: True = inserted, False = an equal key already existed."""

    @abc.abstractmethod
    def find_content_hashes(self, table: str, owner_id: str, key: Mapping[str, Any]) -> Sequence[str]:
        """service-role READ of content_sha256 for rows with that natural key."""


_ANON_NAME = re.compile(r"anon|publishable|public|anyone|guest", re.I)
_ANON_PROBES = ("anon_key", "anon", "is_anon", "publishable_key", "public_key", "is_public", "anon_role", "public")


def check_client(client: Any) -> None:
    """Refuse anything that is not a declared server-side service-role writer: not a ``ServiceRoleClient``, wrong/missing ``role``, any anon/public/publishable attribute (by name,
    or by a catch-all ``__getattr__`` that answers a probe), or missing methods."""
    if not isinstance(client, ServiceRoleClient):
        raise ClientRefused("client must be an instance of tools.supabase_sync.ServiceRoleClient (subclass or register it deliberately)")
    role = getattr(client, "role", None)
    if role != "service_role":
        raise ClientRefused(f"client.role must be exactly 'service_role', got {role!r}")
    try:
        names = [n for n in dir(client) if not n.startswith("__")]
    except Exception:  # noqa: BLE001
        raise ClientRefused("client attributes cannot be listed") from None
    if any(_ANON_NAME.search(n) for n in names):
        raise ClientRefused("client exposes anon/public/publishable semantics")
    for n in _ANON_PROBES:
        try:
            hit = getattr(client, n, None)
        except Exception:  # noqa: BLE001
            hit = None
        if hit:
            raise ClientRefused("client answers an anon/public probe")
    for m in ("put_row", "find_content_hashes"):
        if not callable(getattr(client, m, None)):
            raise ClientRefused(f"client lacks {m}()")


def write(p: Plan, client: Any) -> WriteReport:
    """Send the plan, in order, through the injected service-role client. Everything is re-validated and re-scanned BEFORE the first call (nothing is sent if any row would be
    refused). Per row: INSERTED, UNCHANGED (put_row False and the stored hash equals ours), CONFLICT (put_row False and the stored content differs or the hash is already used
    under another key), FAILED (the client raised; later rows are NOT_SENT since they may depend on it). A conflict is reported and never retried or overwritten."""
    check_client(client)
    _uuid(p.owner_id)
    findings: List[str] = []
    for i, put in enumerate(p.puts):
        if put.table not in SPECS or put.row.get("owner_id") != p.owner_id:
            raise SyncError(f"item {i}: unknown table or foreign owner_id")
        findings += [f"{x} (item {i})" for x in scan_row(put.table, put.row)]
    if findings:
        raise SecretGuardError(findings)
    for i, put in enumerate(p.puts):
        bad = row_problems(put.table, put.row)
        if put.table == "experiment_registrations" and put.attest != _registry_attest(put.row):
            bad = bad + _put_problems(put)
        if bad:
            raise SyncError(f"{put.table}[{i}]: {', '.join(bad[:6])}")
    cross = cross_problems(p.puts)
    if cross:
        raise SyncError("; ".join(cross[:6]))
    results: List[CallResult] = []
    halted = False
    for c in p.conflicts:
        results.append(CallResult(c.table, tuple(c.row.get(k) for k in NATURAL_KEY[c.table]), CONFLICT, "in-plan duplicate key with different content; not sent"))
    for put in p.puts:
        nk = tuple(put.row.get(k) for k in NATURAL_KEY[put.table])
        if halted:
            results.append(CallResult(put.table, nk, NOT_SENT, "an earlier row failed"))
            continue
        try:
            inserted = client.put_row(put.table, dict(put.row))
        except Exception as exc:  # noqa: BLE001  (class name and SQLSTATE only: a database message can quote the row)
            results.append(CallResult(put.table, nk, FAILED, f"{type(exc).__name__} {getattr(exc, 'sqlstate', '') or getattr(exc, 'pgcode', '') or ''}".strip()))
            halted = True
            continue
        if inserted is True:
            results.append(CallResult(put.table, nk, INSERTED))
            continue
        if inserted is not False:
            results.append(CallResult(put.table, nk, FAILED, "put_row returned a non-boolean"))
            halted = True
            continue
        if not NATURAL_KEY[put.table]:
            results.append(CallResult(put.table, nk, UNCHANGED, "same content hash already stored"))
            continue
        try:
            stored = list(client.find_content_hashes(put.table, p.owner_id, {k: put.row[k] for k in NATURAL_KEY[put.table]}))
        except Exception as exc:  # noqa: BLE001
            results.append(CallResult(put.table, nk, FAILED, f"lookup {type(exc).__name__}"))
            halted = True
            continue
        if put.row["content_sha256"] in stored:
            results.append(CallResult(put.table, nk, UNCHANGED, "same key and content already stored"))
        elif stored:
            results.append(CallResult(put.table, nk, CONFLICT, "key exists with DIFFERENT content: stored " + ",".join(h[:12] for h in stored[:3]) + " vs ours " + put.row["content_sha256"][:12]))
        else:
            results.append(CallResult(put.table, nk, CONFLICT, "content hash already stored under a different key"))
    return WriteReport(tuple(results), tuple(p.skipped))
