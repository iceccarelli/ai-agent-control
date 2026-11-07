"""Application-side writer: maps programme ARTIFACTS onto the existing ``carry.*`` tables (supabase/migrations). Offline, order-less, credential-less.

It is NOT a second financial truth. It writes registrations, evidence, manifests, hashes, status words and receipts, and nothing else: no raw owner rows, no
balances, no account identifiers, no orders or fills (the execution/order/fill/reconciliation tables are deliberately not mapped). The venue's own reads and the
owner's reconciled accounting stay authoritative.

    rows  = builders (below)            -> Put(table, row)       one canonicalisation, provenance on every row
    p     = plan(owner_id, rows, ...)   -> Plan                  validated, secret-scanned, de-duplicated, FK-ordered list of ``carry.put_row`` calls
    rep   = write(p, client)            -> WriteReport           client is INJECTED; this module never builds one

CLIENT PROTOCOL (``ServiceRoleClient``). The owner supplies an object that talks to the owner's OWN project with the service role, server-side:
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

import dataclasses
import datetime as _dt
import hashlib
import json
import math
import os
import re
import sys
from decimal import Decimal, InvalidOperation
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


def canon_decimal(v: Any) -> str:
    """Exact, locale- and platform-independent decimal string. Floats go through their shortest repr, so 0.1 is "0.1"; NaN/inf/bool are refused."""
    if isinstance(v, bool) or v is None:
        raise SyncError(f"not a number: {v!r}")
    if isinstance(v, float):
        if not math.isfinite(v):
            raise SyncError("NaN/inf is not a number")
        d = Decimal(repr(v))
    elif isinstance(v, int):
        d = Decimal(v)
    elif isinstance(v, Decimal):
        d = v
    elif isinstance(v, str) and _DEC_RE.match(v.strip()):
        d = Decimal(v.strip())
    else:
        raise SyncError(f"not a number: {type(v).__name__}")
    if not d.is_finite():
        raise SyncError("NaN/inf is not a number")
    if d == 0:
        return "0"
    s = format(d.normalize(), "f")
    return s


def normalise(obj: Any) -> Any:
    """Plain JSON types only: float/Decimal -> exact decimal string, tuple -> list, str keys only. Anything else is refused (no ``default=str``)."""
    if obj is None or isinstance(obj, (bool, str, int)):
        return obj
    if isinstance(obj, (float, Decimal)):
        return canon_decimal(obj)
    if isinstance(obj, Mapping):
        out: Dict[str, Any] = {}
        for k, v in obj.items():
            if not isinstance(k, str):
                raise SyncError(f"non-string key {k!r}")
            out[k] = normalise(v)
        return out
    if isinstance(obj, (list, tuple)):
        return [normalise(x) for x in obj]
    raise SyncError(f"unsupported value type {type(obj).__name__}")


def canonical_json(obj: Any) -> str:
    return json.dumps(normalise(obj), sort_keys=True, separators=(",", ":"), allow_nan=False)


def sha256_text(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def content_sha256(obj: Any) -> str:
    return sha256_text(canonical_json(obj))


def utc(v: Any) -> str:
    """Canonical UTC timestamp string 'YYYY-MM-DDTHH:MM:SS[.ffffff]Z'. Accepts ms-since-epoch ints, aware datetimes and ISO strings WITH a zone; naive times are refused."""
    if isinstance(v, bool):
        raise SyncError("not a timestamp")
    if isinstance(v, int):
        t = _dt.datetime.fromtimestamp(v / 1000.0, tz=_dt.timezone.utc)
    elif isinstance(v, _dt.datetime):
        if v.tzinfo is None:
            raise SyncError("naive datetime refused: state the zone")
        t = v.astimezone(_dt.timezone.utc)
    elif isinstance(v, str):
        s = v.strip()
        if not re.search(r"(Z|[+-]\d\d:?\d\d)$", s) or not re.match(r"^\d{4}-\d\d-\d\d[T ]\d\d:\d\d:\d\d", s):
            raise SyncError("timestamp must be ISO-8601 with an explicit zone")
        try:
            t = _dt.datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(_dt.timezone.utc)
        except ValueError as exc:
            raise SyncError("unparseable timestamp") from exc
    else:
        raise SyncError(f"not a timestamp: {type(v).__name__}")
    base = t.strftime("%Y-%m-%dT%H:%M:%S")
    frac = f".{t.microsecond:06d}".rstrip("0") if t.microsecond else ""
    return base + frac + "Z"


# ------------------------------------------------------------------ table specs (parity with supabase/migrations is a TEST, not a comment)
# kind: uuid, nb (non-blank text), text, hex (sha256), codesha, ts, int, uint (>=0), num (>=0 decimal string/int), dec (decimal string), bool, json (non-empty object/array),
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
                              "status:enum:" + "|".join(OWNER_STATUSES) + " imported_utc:ts source:nb schema_version:uint content_sha256:hex"),
    "owner_account_import_status_events": ("owner_id:uuid import_id:hex status:enum:" + "|".join(OWNER_STATUSES) + " reason:text? evidence_ref:text? recorded_utc:ts "
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


def _json_nonempty(v: Any) -> bool:
    return isinstance(v, (dict, list)) and len(v) > 0


def _check_kind(c: Col, v: Any) -> Optional[str]:
    k = c.kind
    if k == "uuid":
        ok = isinstance(v, str) and bool(_UUID_RE.match(v))
    elif k == "nb":
        ok = _nb(v)
    elif k == "text":
        ok = isinstance(v, str)
    elif k == "hex":
        ok = isinstance(v, str) and bool(_HEX64.match(v))
    elif k == "codesha":
        ok = isinstance(v, str) and bool(_CODESHA.match(v))
    elif k == "ts":
        ok = isinstance(v, str) and bool(_TS.match(v))
    elif k == "int":
        ok = isinstance(v, int) and not isinstance(v, bool) and v >= 1
    elif k == "uint":
        ok = isinstance(v, int) and not isinstance(v, bool) and v >= (1 if c.name == "schema_version" else 0)
    elif k == "num":
        ok = isinstance(v, str) and bool(_DEC_RE.match(v)) and not v.startswith("-") or (isinstance(v, int) and not isinstance(v, bool) and v >= 0)
    elif k == "bool":
        ok = isinstance(v, bool)
    elif k == "json":
        ok = _json_nonempty(v)
    elif k == "obj":
        ok = isinstance(v, dict)
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
    if out:
        return out
    r = row
    if table == "owner_account_imports":
        if r["import_id"] != r["content_sha256"]:
            out.append("import_id:must_equal_content_sha256")
        if r.get("period_start_utc") and r.get("period_end_utc") and _ts_dt(r["period_end_utc"]) < _ts_dt(r["period_start_utc"]):
            out.append("period:end_before_start")
    if table == "owner_account_import_status_events":
        if r["status"] in ("VERIFIED", "RECONCILED") and not _nb(r.get("evidence_ref")):
            out.append("evidence_ref:required_for_VERIFIED_RECONCILED")
        if r["status"] in ("STALE", "DISPUTED", "INCOMPLETE", "UNAVAILABLE") and not _nb(r.get("reason")):
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
    if table == "experiment_registrations" and not all(_nb(r[k]) for k in ("prospective_period",)):
        out.append("prospective_period:blank")
    if HASH_RULE[table] != "registry" and row_hash(table, r) != r["content_sha256"]:
        out.append("content_sha256:does_not_match_content")
    return out


# ------------------------------------------------------------------ SECRET GUARD

_KEY_EXACT = frozenset("""apikey secret secretkey apisecret uid accountid userid memberid email emailaddress ip ipaddress password passwd token accesstoken refreshtoken
sessiontoken authorization privatekey servicerolekey anonkey publishablekey clientsecret bearer jwt""".split())
_KEY_PARTS = ("secret", "apikey", "password", "privatekey", "authorization", "servicerole", "publishablekey", "anonkey", "accesstoken")
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
    ("ipv4_address", re.compile(r"(?<![\d.])(?:25[0-5]|2[0-4]\d|1?\d?\d)(?:\.(?:25[0-5]|2[0-4]\d|1?\d?\d)){3}(?![\d.])")),
)
_OPAQUE_RUN = re.compile(r"(?<![A-Za-z0-9])[A-Za-z0-9]{18,}(?![A-Za-z0-9])")


def _opaque_token(s: str) -> bool:
    """A long mixed-case alphanumeric run anywhere in the text (an exchange-style key or secret). Pure hex (hashes, shas) and runs without all of
    lower case, upper case and a digit are not flagged."""
    for m in _OPAQUE_RUN.finditer(s):
        t = m.group(0)
        if not re.fullmatch(r"[0-9a-f]+", t) and re.search(r"[a-z]", t) and re.search(r"[A-Z]", t) and re.search(r"\d", t):
            return True
    return False


def _shape_rules(s: str) -> List[str]:
    hits = [name for name, rx in _VALUE_RULES if rx.search(s)]
    if _opaque_token(s):
        hits.append("opaque_credential_like")
    return hits


def key_rule(k: str) -> Optional[str]:
    n = re.sub(r"[^a-z0-9]", "", k.lower())
    if n in _KEY_EXACT:
        return "account_or_secret_field_name"
    if any(p in n for p in _KEY_PARTS):
        return "secret_field_name"
    return None


def scan_value(obj: Any, path: str = "") -> List[Tuple[str, str]]:
    """(path, rule) findings for one value tree. Never returns the offending text."""
    out: List[Tuple[str, str]] = []
    if isinstance(obj, Mapping):
        for k, v in obj.items():
            p = f"{path}.{k}" if path else str(k)
            ks = str(k)
            r = key_rule(ks)
            if r:
                out.append((p, r))
            for h in _shape_rules(ks):
                out.append((p + "(key)", h))
            out.extend(scan_value(v, p))
    elif isinstance(obj, (list, tuple)):
        for i, v in enumerate(obj):
            out.extend(scan_value(v, f"{path}[{i}]"))
    elif isinstance(obj, str):
        for h in _shape_rules(obj):
            out.append((path, h))
    return out


def scan_row(table: str, row: Mapping[str, Any]) -> List[str]:
    """Findings as 'table.path:rule' strings. The scan covers every column, including nested JSON."""
    return [f"{table}.{p}:{r}" for p, r in scan_value(dict(row))]


# ------------------------------------------------------------------ rows, plans

@dataclasses.dataclass(frozen=True)
class Put:
    table: str
    row: Mapping[str, Any]


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
    bad = row_problems(table, row)
    if bad:
        raise SyncError(f"{table}: {', '.join(bad[:6])}")
    found = scan_row(table, row)
    if found:
        raise SecretGuardError(found)
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


def registration_puts(owner_id: str, entry: Mapping[str, Any], *, source: Optional[str] = None) -> List[Put]:
    """hypothesis row + experiment_registrations row for one registry entry (the registry's own entry_digest is the registration's content_sha256)."""
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
    return [hyp, reg]


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
    return evidence_put(owner_id, trial_id, sv.STAGE_TO_REGISTRY_LEVEL[stage], result=f"{stage}:{g('result')}", provenance=g("provenance") or "none",
                        details={"metrics": dict(g("metrics") or {}), "data_sha256": g("data_sha256"), "status": g("status")}, recorded_utc=int(ms),
                        source=_src(source, "tools/strategy_verdict.py"))


def registry_puts(owner_id: str, registry: Mapping[str, Any], *, source: Optional[str] = None) -> Tuple[List[Put], List[str]]:
    """Every registrable trial with its evidence, amendments, abandonment and supersession. Entries that are not registrations (the legacy seed) are SKIPPED WITH A
    REASON, never silently. A supersession whose successor is not itself registrable is skipped and reported."""
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
        puts += registration_puts(owner_id, t, source=src)
        tid = t["trial_id"]
        for ev in t.get("evidence", []) or []:
            puts.append(evidence_put(owner_id, tid, ev["level"], result=ev["result"], provenance=ev["provenance"], details=ev.get("details"), recorded_utc=ev["recorded_utc"], source=src))
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
def owner_import_puts(owner_id: str, report: Mapping[str, Any], *, imported_utc: Any, storage_ref: str, source_sha256: Optional[str] = None, kind: str = "owner_import",
                      source: Optional[str] = None) -> List[Put]:
    """An ``owner_import`` report -> ONE owner_account_imports row. Only this whitelist is read from the report: ``status`` (must be in the importer's vocabulary),
    ``lineage.files[*].sha256`` (digested into the source hash unless ``source_sha256`` is given), ``ledger.window`` opening/closing ms if both are integers, and the number of
    lineage files. The report's rows, figures, balances, flags, marks and identifiers are never copied. import_id = content hash of the stored metadata."""
    status = report.get("status")
    if status not in OWNER_STATUSES:
        raise SyncError(f"report status {status!r} is not in the status vocabulary")
    if source_sha256 is None:
        files = (report.get("lineage") or {}).get("files") or []
        shas = sorted(str(f.get("sha256")) for f in files if isinstance(f, Mapping))
        if not shas or not all(_HEX64.match(s) for s in shas):
            raise SyncError("report carries no file hashes: pass source_sha256 explicitly")
        source_sha256 = content_sha256(shas)
    win = (report.get("ledger") or {}).get("window") or {}
    lo, hi = (win.get("opening_ts_ms"), win.get("closing_ts_ms")) if isinstance(win, Mapping) else (None, None)
    period = {}
    if isinstance(lo, int) and isinstance(hi, int) and not isinstance(lo, bool) and not isinstance(hi, bool) and hi >= lo:
        period = {"period_start_utc": utc(lo), "period_end_utc": utc(hi)}
    nfiles = len(((report.get("lineage") or {}).get("files")) or [])
    return [_finish("owner_account_imports", owner_id, {"source_sha256": source_sha256, "kind": kind, **period, "row_count": nfiles or None, "storage_ref": storage_ref,
                                                         "status": status, "imported_utc": utc(imported_utc)}, source=_src(source, "tools/owner_import.py"))]


def import_status_event_put(owner_id: str, import_id: str, status: str, recorded_utc: Any, *, reason: Optional[str] = None, evidence_ref: Optional[str] = None,
                            source: Optional[str] = None) -> Put:
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
        bad = row_problems(p.table, p.row)
        if bad:
            raise SyncError(f"{p.table}[{i}]: {', '.join(bad[:6])}")
        findings += [f"{x} (item {i})" for x in scan_row(p.table, p.row)]
    if findings:
        raise SecretGuardError(findings)
    seen_hash: Dict[Tuple[str, str], Tuple[Any, ...]] = {}
    seen_key: Dict[Tuple[str, Tuple[Any, ...]], str] = {}
    kept: List[Put] = []
    conflicts: List[Put] = []
    collapsed = 0
    for p in flat:
        nk = tuple(p.row.get(c) for c in NATURAL_KEY[p.table])
        h = p.row["content_sha256"]
        if (p.table, h) in seen_hash:
            if seen_hash[(p.table, h)] == nk:
                collapsed += 1
            else:
                conflicts.append(p)
            continue
        if NATURAL_KEY[p.table] and (p.table, nk) in seen_key:
            conflicts.append(p)
            continue
        seen_hash[(p.table, h)] = nk
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


def check_client(client: Any) -> None:
    """Refuse anything that is not a server-side service-role writer: wrong/missing ``role``, anon/publishable semantics, or missing methods."""
    role = getattr(client, "role", None)
    if role != "service_role":
        raise ClientRefused(f"client.role must be exactly 'service_role', got {role!r}")
    names = [n for n in dir(client) if not n.startswith("__")]
    if any(re.search(r"anon|publishable", n, re.I) for n in names) or getattr(client, "is_anon", False):
        raise ClientRefused("client exposes anon/publishable semantics")
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
        bad = row_problems(put.table, put.row)
        if bad:
            raise SyncError(f"{put.table}[{i}]: {', '.join(bad[:6])}")
        findings += [f"{x} (item {i})" for x in scan_row(put.table, put.row)]
    if findings:
        raise SecretGuardError(findings)
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
