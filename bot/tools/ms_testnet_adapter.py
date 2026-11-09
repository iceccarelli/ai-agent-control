"""The DISABLED multi-symbol testnet adapter: an owner-session CAPABILITY DESCRIPTION, not an executor.

    python3 tools/ms_testnet_adapter.py generate --candidate-id ID --spec-digest HEX --symbols BTCUSDT,ETHUSDT   # prints the manifest; authorisation is ALWAYS NOT_GRANTED
    python3 tools/ms_testnet_adapter.py validate FILE                                                           # exit 1 on any refusal
    python3 tools/ms_testnet_adapter.py status                                                                  # is the owner capability file present and valid (no: enabled=false)

THERE IS NO CODE PATH IN THIS MODULE THAT SENDS AN ORDER OR OPENS A CONNECTION. It imports no transport, no client, no HTTP/socket library (a test parses the module and fails on
any such import or on any send-like name). What it does:
  * builds the exact machine-readable ACTION MANIFEST for a bounded multi-symbol testnet qualification (closed schema, like ``tools/testnet_action_manifest.py``): the symbols and
    strategies allowed (explicit lists, no wildcard), directions, order types, execution styles, an AGGREGATE notional ceiling of ``AGGREGATE_CEILING_USD`` ($500, Testnet only; independent of the shared ``shadow.SHADOW_MAX_NOTIONAL_USD`` = $100) across ALL
    symbols (never per symbol), gross/net/unhedged/active-group bounds, the persistent kill-switch file semantics, the reconciliation and reduce-only recovery plan, the evidence to
    capture and the DEDICATED testnet credential variable names (the generic ``BYBIT_API_KEY``/``BYBIT_API_SECRET`` are refused);
  * bounds can be LOWERED, never raised: the validator refuses a manifest above any ceiling;
  * authorisation is ``NOT_GRANTED`` from the generator, always. The validator accepts ``GRANTED_BY_OWNER:<reference>`` only because the owner may mark a manifest later; that enables
    nothing by itself;
  * ``enabled()`` is False unless an OWNER-SET capability file exists at ``DEFAULT_CAPABILITY_PATH`` (or the path given) holding a manifest that validates, is authorised, and whose
    digest the file restates. This repository never creates that file. The file is SELF-ATTESTED (a same-user process could write it; the digest is not a signature) and must be owned by the current
    user with mode 0600 (no group/other bits), otherwise it is refused (``CAPABILITY_FILE_NOT_OWNED_BY_CURRENT_USER`` / ``CAPABILITY_FILE_MODE_NOT_OWNER_ONLY_0600``). Even when ``enabled()`` is True this module still sends nothing: it is one of three conditions
    ``ms_authority`` requires before it can be bound to a non-simulated transport (the others: a sandbox venue and a dedicated testnet credential name).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sys
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

BOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BOT)
sys.path.insert(0, os.path.join(BOT, "tools"))

import ms_risk  # noqa: E402
import shadow  # noqa: E402
import testnet_action_manifest as TAM  # noqa: E402
import runtime_capability as RC  # noqa: E402

SCHEMA = "ms_testnet_action_manifest/1"
CAPABILITY_SCHEMA = "ms_testnet_capability/1"
NOT_GRANTED = TAM.NOT_GRANTED
_GRANTED = TAM._GRANTED
DEFAULT_CAPABILITY_PATH = os.path.join(os.path.expanduser("~"), ".ai-agent-control", "ms_testnet_capability.json")

#: The variable names the owner session tool (``ms_testnet_session``) reads: BYBIT_TESTNET_API_KEY / BYBIT_TESTNET_API_SECRET (derived from the manifest defaults; names only, never values).
SESSION_CREDENTIAL_ENV = dict(api_key=TAM.DEFAULT_KEY_ENV.replace("DEDICATED_", ""), api_secret=TAM.DEFAULT_SECRET_ENV.replace("DEDICATED_", ""))

#: The multi-symbol TESTNET session's OWN aggregate ceiling (owner decision, 2026-11-09: $500 of fake money on Testnet). It is deliberately NOT ``shadow.SHADOW_MAX_NOTIONAL_USD`` ($100): that
#: shared constant caps the frozen BTC experiment, the venue drills, the entry authority and the live-gate tests, and stays 100 (a test pins both numbers). Mainnet remains unreachable from here.
AGGREGATE_CEILING_USD = 500.0                                       # across ALL symbols, hedged pair counted once; never a per-symbol allowance
#: The loss ceiling keeps the ratio the old $100 drill used (1.11 on 100 = 1.11%), so the stop scales with the size: 5.55 on 500. A $500 hedged pair pays about $1.6 in taker fees + assumed spread alone.
MAX_LOSS_CEILING_USD = round(0.0111 * AGGREGATE_CEILING_USD, 2)
MAX_SYMBOLS = 5
MAX_ACTIVE_GROUPS = 1                                              # ONE active group (ceiling AND default); the session tool enforces it and refusals() rejects more
MAX_ENTRIES_PER_UTC_DAY = 4
MAX_LEVERAGE = 3.0
MAX_MARKET_AGE_MS = 5000
MAX_ACCOUNT_AGE_MS = 10000
MAX_LIFECYCLE_HOURS = TAM.MAX_LIFECYCLE_HOURS
MIN_AGGREGATE_USD = TAM.MIN_NOTIONAL_USD
_STRATEGY = re.compile(r"[a-z][a-z0-9_]{2,63}")
_DIGEST = re.compile(r"[0-9a-f]{64}")
_CANDIDATE = re.compile(r"(?=.{3,64}\Z)[A-Za-z0-9_.-]+(?:@[A-Za-z0-9_.-]+)?")      # ONE '@' with text on both sides is allowed because every registered candidate id is '<family>@spec-N' (cash_and_carry@spec-1); nothing else shell- or path-significant is

SIDES = {"linear": ["Sell", "Buy"], "spot": ["Buy", "Sell"]}
ORDER_TYPES = ["Limit", "Market"]
EXECUTION_STYLES = ["MAKER_FIRST", "TAKER"]

KILL_SWITCH = {"file": "MS_KILL", "semantics": "PERSISTENT: the file's presence means STOP; read before every order and every reconcile; an unreadable path is an engaged switch; "
                                              "only the owner removes it; engaging never needs a running process"}
# The words below are what the owner signs, so they describe THIS tool (ms_testnet_session), not the older single-symbol drill they were first copied from: the kill file is MS_KILL (not
# DRILL_KILL), the entry bound is bounds.max_entries_per_utc_day (not "one entry"), and the evidence is the files the session tool really writes (no drill flags).
_DRILL_ONLY_STOPS = ("DRILL_KILL file present", "more than one entry in the UTC day")
STOP_CONDITIONS = tuple(c for c in TAM.STOP_CONDITIONS if c not in _DRILL_ONLY_STOPS) + (
                                          "MS_KILL file present or unreadable in the session state dir", "more entries in the UTC day than bounds.max_entries_per_utc_day", "aggregate notional ceiling reached", "a leg of a paired order is unfilled or unknown beyond max_wait", "venue and local position disagree")
RECONCILIATION = {
    "source_of_truth": "the venue's own position, order and execution reads; never the local journal or fill arithmetic",
    "before_any_resend": "query the order by orderLinkId; a lost acknowledgement is UNKNOWN_OUTCOME until the venue answers",
    "cadence": "before the first order, after every group state change, and on every restart",
    "flat_rule": TAM.RECOVERY["flat_rule"],
}
RECOVERY_PLAN = (
    "engage the kill switch file",
    "cancel the group's open orders by orderLinkId (cancel only; symbols the group opened)",
    "read every position from the venue",
    "close each non-flat symbol with a reduce-only order bound to a symbol the group opened",
    "re-read positions from the venue: every allowed symbol must be flat",
    "capture the evidence list",
    "if any position remains: HUMAN MUST ACT",
)
EVIDENCE = ("testnet_session.json (orders, executions, final venue position, exchange-reported fees)", "plan.json and preflight.json (their digests bind the session)",
            "engine_journal.jsonl (every group state change)", "ms_groups.jsonl (registered groups and the reservation ledger)")

#: The reads ``ms_testnet_session preflight`` really makes (a test drives it against the fake testnet and checks every endpoint here is requested). The drill's list also declares
#: /v5/account/fee-rate and /v5/order/history, which this preflight does not call, so they are not declared here.
PREFLIGHT_READS = tuple(r for r in TAM.PREFLIGHT_READS if r[1] not in ("/v5/account/fee-rate", "/v5/order/history"))

_TOP = ("schema", "venue", "category_scope", "mode", "credential_class", "credential_env", "strategy_allowlist", "symbol_allowlist", "allowed_sides", "allowed_order_types",
        "execution_styles", "bounds", "kill_switch", "preflight_reads", "stop_conditions", "reconciliation", "recovery_plan", "evidence_capture", "authorisation", "plan_sha256")
_BOUNDS = ("aggregate_max_notional_usd", "max_notional_per_symbol_usd", "max_gross_exposure_usd", "max_net_exposure_usd", "max_unhedged_usd", "max_active_groups",
           "max_entries_per_utc_day", "max_lifecycle_hours", "max_loss_usd", "max_leverage", "max_market_age_ms", "max_account_age_ms")


class ManifestError(RuntimeError):
    pass


def plan_digest(m: Mapping[str, Any]) -> str:
    body = {k: v for k, v in m.items() if k not in ("plan_sha256", "authorisation")}
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode("ascii")).hexdigest()


def _is_num(x: Any) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def _is_int(x: Any) -> bool:
    return type(x) is int


def _ceilings() -> Dict[str, float]:
    agg = AGGREGATE_CEILING_USD
    return {"aggregate_max_notional_usd": agg, "max_notional_per_symbol_usd": 2 * agg, "max_gross_exposure_usd": 2 * agg, "max_net_exposure_usd": agg, "max_unhedged_usd": agg,
            "max_active_groups": MAX_ACTIVE_GROUPS, "max_entries_per_utc_day": MAX_ENTRIES_PER_UTC_DAY, "max_lifecycle_hours": MAX_LIFECYCLE_HOURS,
            "max_loss_usd": MAX_LOSS_CEILING_USD, "max_leverage": MAX_LEVERAGE, "max_market_age_ms": MAX_MARKET_AGE_MS, "max_account_age_ms": MAX_ACCOUNT_AGE_MS}


def generate(*, candidate_id: str, spec_digest: str, symbols: Sequence[str], strategy: str = "multi_symbol_funding_carry",
             lower: Optional[Mapping[str, float]] = None, credential_env: Optional[Mapping[str, str]] = None) -> Dict[str, Any]:
    """The manifest for ``symbols`` / one candidate. ``lower`` may LOWER bounds; it cannot raise them (a raised bound makes ``refusals`` non-empty). Authorisation: NOT_GRANTED."""
    bounds: Dict[str, Any] = dict(_ceilings())
    for k, v in (lower or {}).items():
        if k not in bounds:
            raise ManifestError(f"unknown bound {k!r}")
        bounds[k] = v
    m: Dict[str, Any] = {
        "schema": SCHEMA, "venue": "testnet", "category_scope": ["linear", "spot"], "mode": "multi_symbol_hedged_carry", "credential_class": RC.TESTNET_CREDENTIAL_CLASS,
        "credential_env": dict(credential_env) if credential_env is not None else {"api_key": TAM.DEFAULT_KEY_ENV, "api_secret": TAM.DEFAULT_SECRET_ENV},
        "strategy_allowlist": [{"strategy": strategy, "candidate_id": candidate_id, "spec_digest": spec_digest}],
        "symbol_allowlist": list(symbols), "allowed_sides": {k: list(v) for k, v in SIDES.items()}, "allowed_order_types": list(ORDER_TYPES),
        "execution_styles": list(EXECUTION_STYLES), "bounds": bounds, "kill_switch": dict(KILL_SWITCH),
        "preflight_reads": [{"fact": f, "endpoint": e, "signed": s} for f, e, s in PREFLIGHT_READS],
        "stop_conditions": list(STOP_CONDITIONS), "reconciliation": dict(RECONCILIATION), "recovery_plan": list(RECOVERY_PLAN), "evidence_capture": list(EVIDENCE),
        "authorisation": NOT_GRANTED,
    }
    if m["authorisation"] != "NOT_GRANTED":                          # literal on purpose: this tool never generates an authorised manifest
        raise ManifestError("the generator may only emit authorisation NOT_GRANTED")
    m["plan_sha256"] = plan_digest(m)
    return m


def refusals(m: Any) -> List[str]:
    """Every reason this manifest is refused (empty = valid). Pure."""
    out: List[str] = []
    if not isinstance(m, dict):
        return ["manifest is not a JSON object"]
    extra, missing = sorted(set(m) - set(_TOP)), sorted(set(_TOP) - set(m))
    if extra:
        out.append(f"unknown fields: {extra}")
    if missing:
        out.append(f"missing fields: {missing}")
    if extra or missing:
        return out
    for k, want in (("schema", SCHEMA), ("venue", "testnet"), ("mode", "multi_symbol_hedged_carry"), ("credential_class", RC.TESTNET_CREDENTIAL_CLASS)):
        if m[k] != want or not isinstance(m[k], str):
            out.append(f"{k} must be exactly {want!r}")
    if m["category_scope"] != ["linear", "spot"]:
        out.append("category_scope must be exactly ['linear', 'spot']")
    ce = m["credential_env"]
    if not isinstance(ce, dict) or set(ce) != {"api_key", "api_secret"}:
        out.append("credential_env must be exactly {api_key, api_secret}")
    else:
        TAM._check_name("api_key", ce["api_key"], out)
        TAM._check_name("api_secret", ce["api_secret"], out)
        if ce["api_key"] == ce["api_secret"]:
            out.append("credential_env: key and secret names must differ")
    sa = m["strategy_allowlist"]
    if not isinstance(sa, list) or not sa or any(not isinstance(r, dict) or set(r) != {"strategy", "candidate_id", "spec_digest"} for r in sa):
        out.append("strategy_allowlist must be a non-empty list of {strategy, candidate_id, spec_digest}")
    else:
        for r in sa:
            if not (isinstance(r["strategy"], str) and _STRATEGY.fullmatch(r["strategy"]) and isinstance(r["candidate_id"], str) and _CANDIDATE.fullmatch(r["candidate_id"])
                    and isinstance(r["spec_digest"], str) and _DIGEST.fullmatch(r["spec_digest"])):
                out.append("strategy_allowlist entry malformed (strategy name, candidate id, 64-hex spec digest)")
        if len({r.get("strategy") for r in sa if isinstance(r, dict)}) != len(sa):
            out.append("strategy_allowlist: duplicate strategy")
    sy = m["symbol_allowlist"]
    if (not isinstance(sy, list) or not sy or len(sy) > MAX_SYMBOLS or any(type(s) is not str or not ms_risk.SYMBOL_RE.fullmatch(s) or s in ms_risk.WILDCARD_WORDS for s in sy)
            or len(set(sy)) != len(sy)):
        out.append(f"symbol_allowlist must be 1..{MAX_SYMBOLS} distinct explicit symbols (no wildcard)")
    if m["allowed_sides"] != SIDES:
        out.append("allowed_sides must be exactly the pinned per-category sides")
    if m["allowed_order_types"] != ORDER_TYPES:
        out.append("allowed_order_types must be exactly ['Limit', 'Market']")
    if m["execution_styles"] != EXECUTION_STYLES:
        out.append("execution_styles must be exactly ['MAKER_FIRST', 'TAKER']")
    b = m["bounds"]
    if not isinstance(b, dict) or set(b) != set(_BOUNDS):
        out.append(f"bounds must be exactly {list(_BOUNDS)}")
    else:
        cap = _ceilings()
        for k in _BOUNDS:
            v = b[k]
            if k in ("max_active_groups", "max_entries_per_utc_day", "max_market_age_ms", "max_account_age_ms"):
                if not _is_int(v) or not (1 <= v <= cap[k]):
                    out.append(f"bounds.{k} must be an integer in [1, {cap[k]}]")
            elif k == "max_leverage":
                if not _is_num(v) or not (1 <= v <= cap[k]):
                    out.append(f"bounds.{k} must be a number in [1, {cap[k]}]")
            elif k == "aggregate_max_notional_usd":
                if not _is_num(v) or not (MIN_AGGREGATE_USD <= v <= cap[k]):
                    out.append(f"bounds.{k} must be a number in [{MIN_AGGREGATE_USD}, {cap[k]}] (ONE ceiling across all symbols)")
            elif not _is_num(v) or not (0 < v <= cap[k]):
                out.append(f"bounds.{k} must be a number in (0, {cap[k]}]")
        typed_ok = not out                                          # relations are only meaningful once every bound is a number inside its ceiling
        if typed_ok and b["max_net_exposure_usd"] > b["aggregate_max_notional_usd"]:
            out.append("bounds.max_net_exposure_usd may not exceed the aggregate ceiling")
        if typed_ok and b["max_unhedged_usd"] > b["aggregate_max_notional_usd"]:
            out.append("bounds.max_unhedged_usd may not exceed the aggregate ceiling")
        if typed_ok and b["max_gross_exposure_usd"] > 2 * b["aggregate_max_notional_usd"]:
            out.append("bounds.max_gross_exposure_usd may not exceed twice the aggregate ceiling")
    if m["kill_switch"] != KILL_SWITCH:
        out.append("kill_switch must be exactly the persistent-file semantics")
    want_reads = [{"fact": f, "endpoint": e, "signed": s} for f, e, s in PREFLIGHT_READS]
    pr = m["preflight_reads"]
    if not isinstance(pr, list) or any(not isinstance(r, dict) or set(r) != {"fact", "endpoint", "signed"} for r in pr):
        out.append("preflight_reads must be a list of {fact, endpoint, signed}")
    elif pr != want_reads or any(type(r["signed"]) is not bool for r in pr):
        out.append("preflight_reads must be exactly the required read list")
    else:
        monitor = RC.Capability.for_role(RC.READ_ONLY_MONITOR, venue="testnet")
        for r in pr:
            code = monitor.check("GET", r["endpoint"], r["signed"], monitor.host)
            if code is not None:
                out.append(f"preflight read {r['fact']}: read_only_monitor may not perform it ({code})")
    if m["stop_conditions"] != list(STOP_CONDITIONS):
        out.append("stop_conditions must be exactly the required list (a stop condition lost, added or reworded)")
    if m["reconciliation"] != RECONCILIATION:
        out.append("reconciliation must be exactly the required rules")
    if m["recovery_plan"] != list(RECOVERY_PLAN):
        out.append("recovery_plan must be exactly the required ordered steps")
    if m["evidence_capture"] != list(EVIDENCE):
        out.append("evidence_capture must be exactly the required list")
    a = m["authorisation"]
    if not isinstance(a, str) or not (a == NOT_GRANTED or _GRANTED.fullmatch(a)):
        out.append("authorisation must be 'NOT_GRANTED' or 'GRANTED_BY_OWNER:<reference>'")
    try:
        digest_ok = m["plan_sha256"] == plan_digest(m)
    except (TypeError, ValueError):
        digest_ok = False
    if not digest_ok:
        out.append("plan_sha256 does not match the content: the manifest was edited")
    return out


def session_warnings(m: Any) -> List[str]:
    """Things that are VALID for the adapter but make ``ms_testnet_session`` refuse the manifest. Today: credential variable names other than the ones the session tool reads
    (the default names are the older drill's BYBIT_TESTNET_DEDICATED_*). A warning never changes ``valid`` or ``enabled`` (the authority's conditions are untouched)."""
    try:
        if m["credential_env"] != SESSION_CREDENTIAL_ENV:
            return ["CREDENTIAL_ENV_NOT_SESSION_NAMES: the manifest names " + "/".join(str(m["credential_env"][k]) for k in ("api_key", "api_secret")) + " but ms_testnet_session reads "
                    + "/".join(SESSION_CREDENTIAL_ENV[k] for k in ("api_key", "api_secret")) + ": its preflight will FAIL (manifest_credential_names); regenerate with --session-credentials"]
    except (KeyError, TypeError):
        pass
    return []


def validate(m: Any, env: Optional[Mapping[str, str]] = None) -> Dict[str, Any]:
    """Refusals plus a presence-only record of the named credentials (booleans; values never enter the report). ``env`` None = presence not checked (None, never False)."""
    ref = refusals(m)
    presence: Dict[str, Optional[bool]] = {"api_key": None, "api_secret": None}
    if not ref and env is not None:                                  # this module never reads the process environment: presence is checked only against a mapping the caller passes
        presence = {k: bool(str(env.get(m["credential_env"][k], "") or "").strip()) for k in presence}
    return {"valid": not ref, "refusals": ref, "authorised": (not ref) and m["authorisation"] != NOT_GRANTED, "credential_presence": presence,
            "plan_sha256": m.get("plan_sha256") if isinstance(m, dict) else None, "warnings": [] if ref else session_warnings(m),
            "note": "validation enables nothing; an authorised manifest is a record of the owner's instruction, not a switch"}


def policy_from_manifest(m: Mapping[str, Any], *, strategy_index: int = 0) -> Dict[str, Any]:
    """The ``ms_risk`` policy a validated manifest implies (every bound the manifest states, nothing wider). Raises if the manifest is refused."""
    bad = refusals(dict(m))
    if bad:
        raise ManifestError("manifest refused: " + "; ".join(bad))
    b, s = m["bounds"], m["strategy_allowlist"][strategy_index]
    return {"policy_version": "ms_testnet:" + m["plan_sha256"][:16], "candidate_id": s["candidate_id"], "spec_digest": s["spec_digest"], "venue": "testnet",
            "allowed_symbols": list(m["symbol_allowlist"]), "allowed_sides": {k: list(v) for k, v in m["allowed_sides"].items()},
            "allowed_order_types": list(m["allowed_order_types"]), "max_notional_per_symbol_usd": b["max_notional_per_symbol_usd"],
            "max_aggregate_notional_usd": b["aggregate_max_notional_usd"], "max_gross_exposure_usd": b["max_gross_exposure_usd"], "max_net_exposure_usd": b["max_net_exposure_usd"],
            "max_active_groups": b["max_active_groups"], "max_unhedged_usd": b["max_unhedged_usd"], "max_daily_loss_usd": float(b["max_loss_usd"]), "max_leverage": float(b["max_leverage"]),
            "max_market_age_ms": b["max_market_age_ms"], "max_account_age_ms": b["max_account_age_ms"], "execution_styles": list(m["execution_styles"])}


# ---------------------------------------------------------------------------------------------------- persistent kill switch (file semantics)

def kill_switch_engaged(path: str) -> tuple:
    """(engaged, why). The switch is a FILE: present (of any kind, even a dangling symlink or a directory) = engaged. Any error other than 'does not exist' = engaged."""
    try:
        os.lstat(path)
    except FileNotFoundError:
        return False, "absent"
    except (OSError, ValueError, TypeError) as exc:                  # a path that cannot even be stat'ed is not 'absent'
        return True, f"unreadable:{type(exc).__name__}"
    return True, "present"


def engage_kill_switch(path: str, reason: str = "") -> bool:
    """Create the kill-switch file (exclusive; an existing one stays). True if it was created. Needs no running process and no network."""
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    except FileExistsError:
        return False
    try:
        os.write(fd, (reason or "engaged").encode("utf-8")[:512])
        os.fsync(fd)
    finally:
        os.close(fd)
    return True


# ---------------------------------------------------------------------------------------------------- the capability flag

def capability_status(path: Optional[str] = None) -> Dict[str, Any]:
    """Whether the OWNER has enabled this capability. Never raises. SELF-ATTESTED: the file is a statement the owner's account made about itself. Nothing here can prove a human
    wrote it (any process running as the same user can create a file with a granted manifest and a matching digest), and the digest is not a signature (it is recomputable from the
    content). What it does check is that the file is not writable or readable by anyone but its owner: a regular, non-symlink file OWNED BY THE CURRENT USER with mode 0600
    (no group/other bits), read through the descriptor that was checked. It is one condition among several for ``ms_authority`` and enables nothing alone.
    Enabled only if ``path`` is such a file holding {schema, manifest, manifest_plan_sha256}
    where the manifest is valid, authorised ``GRANTED_BY_OWNER:*`` and its digest equals both the recomputed digest and ``manifest_plan_sha256``."""
    p = DEFAULT_CAPABILITY_PATH if path is None else path
    try:
        if os.path.islink(p) or not os.path.isfile(p):
            return {"enabled": False, "reasons": ["CAPABILITY_FILE_ABSENT"]}
        fd = os.open(p, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            st = os.fstat(fd)                                       # judged on the OPEN descriptor: the file checked is the file read
            if (st.st_mode & 0o170000) != 0o100000:
                return {"enabled": False, "reasons": ["CAPABILITY_FILE_NOT_REGULAR"]}
            if st.st_uid != os.getuid():
                return {"enabled": False, "reasons": ["CAPABILITY_FILE_NOT_OWNED_BY_CURRENT_USER"]}
            if st.st_mode & 0o7177:                                  # any group/other bit (so never group- or world-writable), setuid/setgid/sticky, or owner-execute
                return {"enabled": False, "reasons": ["CAPABILITY_FILE_MODE_NOT_OWNER_ONLY_0600"]}
            with os.fdopen(fd, encoding="utf-8") as fh:
                fd = -1
                doc = json.loads(fh.read(), object_pairs_hook=TAM._no_dupes, parse_constant=TAM._refuse_const)
        finally:
            if fd >= 0:
                os.close(fd)
        if not isinstance(doc, dict) or set(doc) != {"schema", "manifest", "manifest_plan_sha256"} or doc["schema"] != CAPABILITY_SCHEMA:
            return {"enabled": False, "reasons": ["CAPABILITY_FILE_MALFORMED"]}
        m = doc["manifest"]
        ref = refusals(m)
        if ref:
            return {"enabled": False, "reasons": ["MANIFEST_REFUSED"] + ref}
        if m["authorisation"] == NOT_GRANTED:
            return {"enabled": False, "reasons": ["MANIFEST_NOT_GRANTED"]}
        if doc["manifest_plan_sha256"] != m["plan_sha256"] or m["plan_sha256"] != plan_digest(m):
            return {"enabled": False, "reasons": ["DIGEST_NOT_SIGNED_OFF"]}
        out: Dict[str, Any] = {"enabled": True, "reasons": [], "plan_sha256": m["plan_sha256"]}
        w = session_warnings(m)
        if w:                                                       # enabled for the authority, yet unusable by the session tool: never a bare enabled:true
            out["warnings"] = w
        return out
    except Exception as exc:                                       # noqa: BLE001 - any doubt is disabled
        return {"enabled": False, "reasons": [f"CAPABILITY_UNREADABLE:{type(exc).__name__}"]}


def enabled(path: Optional[str] = None) -> bool:
    return capability_status(path)["enabled"] is True


def _resolve_candidate(path: Optional[str], cid: Optional[str], dig: Optional[str]) -> Tuple[str, str]:
    """(candidate_id, spec_digest) for ``generate``: from a candidate file (the digest is ``strategy_verdict.spec_digest``, the SAME one ms_pipeline binds a run to) or both typed."""
    if path is None:
        if not cid or not dig:
            raise ManifestError("give --candidate FILE (id and digest are derived) or both --candidate-id and --spec-digest")
        return cid, dig
    import strategy_verdict as SV                                   # pure; imported only here
    try:
        with open(path, encoding="utf-8") as fh:
            spec = json.loads(fh.read(), parse_constant=TAM._refuse_const)
    except (OSError, ValueError) as exc:
        raise ManifestError(f"CANDIDATE_UNREADABLE: {exc}") from None
    if not isinstance(spec, dict) or not isinstance(spec.get("candidate_id"), str):
        raise ManifestError("CANDIDATE_UNREADABLE: no candidate_id in the spec")
    derived = SV.spec_digest(spec)
    registry = os.path.realpath(os.path.join(BOT, "research", "candidates"))
    if os.path.dirname(os.path.realpath(path)) != registry and dig is None:
        raise ManifestError("SPEC_DIGEST_REQUIRED: a candidate file outside research/candidates must come with --spec-digest (as for ms_pipeline)")
    if cid is not None and cid != spec["candidate_id"]:
        raise ManifestError(f"CANDIDATE_ID_MISMATCH: --candidate-id {cid!r} but the file says {spec['candidate_id']!r}")
    if dig is not None and dig != derived:
        raise ManifestError(f"SPEC_DIGEST_MISMATCH: --spec-digest does not equal the file's spec digest {derived}")
    return spec["candidate_id"], derived


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Multi-symbol testnet action manifest (offline; sends nothing).")
    sub = ap.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("generate")
    g.add_argument("--candidate-id", default=None)
    g.add_argument("--spec-digest", default=None)
    g.add_argument("--candidate", default=None, metavar="FILE",
                   help="a candidate spec JSON (e.g. research/candidates/cash_and_carry.json): the id and the spec digest ms_pipeline binds are DERIVED from it; --candidate-id/--spec-digest, if "
                        "also given, must equal them; a file outside research/candidates needs --spec-digest")
    g.add_argument("--symbols", required=True)
    g.add_argument("--session-credentials", action="store_true",
                   help="name the dedicated variables the owner session tool reads: BYBIT_TESTNET_API_KEY / BYBIT_TESTNET_API_SECRET (the names are part of the digest)")
    v = sub.add_parser("validate")
    v.add_argument("file")
    sub.add_parser("status")
    a = ap.parse_args(argv)
    if a.cmd == "generate":
        try:
            cid, dig = _resolve_candidate(a.candidate, a.candidate_id, a.spec_digest)
        except ManifestError as exc:
            print(f"REFUSED: {exc}", file=sys.stderr)
            return 2
        m = generate(candidate_id=cid, spec_digest=dig, symbols=[s for s in a.symbols.split(",") if s],
                     credential_env=SESSION_CREDENTIAL_ENV if a.session_credentials else None)
        rep = validate(m, env={})
        print(json.dumps(m, indent=2, sort_keys=True))
        for w in rep["warnings"]:
            print("WARNING: " + w, file=sys.stderr)
        if not rep["valid"]:                                         # the manifest above is NOT usable: say so where a redirect to a file cannot hide it
            print("REFUSED: the manifest just written does not validate: " + "; ".join(rep["refusals"]), file=sys.stderr)
        return 0 if rep["valid"] else 1
    if a.cmd == "status":
        st = capability_status()
        print(json.dumps(st, indent=2, sort_keys=True))
        for w in st.get("warnings", []):
            print("WARNING: " + w, file=sys.stderr)
        return 0
    try:
        with open(a.file, encoding="utf-8") as fh:
            m = json.loads(fh.read(), object_pairs_hook=TAM._no_dupes, parse_constant=TAM._refuse_const)
    except (OSError, ValueError) as exc:
        print(json.dumps({"valid": False, "refusals": [f"unreadable manifest: {exc}"]}, indent=2))
        return 1
    rep = validate(m)
    print(json.dumps(rep, indent=2, sort_keys=True))
    for w in rep["warnings"]:
        print("WARNING: " + w, file=sys.stderr)
    return 0 if rep["valid"] else 1


if __name__ == "__main__":
    sys.exit(main())
