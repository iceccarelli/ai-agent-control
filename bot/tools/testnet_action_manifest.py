"""Machine-readable action manifest for the bounded testnet qualification. Offline: no request, no credential value is ever read, nothing is written.

    python3 tools/testnet_action_manifest.py generate            # prints the manifest (JSON); authorisation is ALWAYS "NOT_GRANTED"
    python3 tools/testnet_action_manifest.py validate FILE       # exit 1 on any refusal; prints a report (presence of credentials, never values)

The manifest is the section-C plan of ``tools/testnet_certificate.py`` made strict and checkable. The validator is a closed schema: an unknown field, a missing field,
a wrong type, a number outside its bound, a venue other than ``testnet``, a credential class other than ``testnet_dedicated``, a credential env-var name that is (or
clashes with) the generic ``BYBIT_API_KEY`` / ``BYBIT_API_SECRET``, a preflight read the read-only monitor role may not perform, a lost stop condition or a changed
recovery command is REFUSED. Bounds can be lowered, never raised: notional <= ``shadow.SHADOW_MAX_NOTIONAL_USD``, 1 entry, 9 hours, 1 lifecycle.

Authorisation. The generator has no way to emit anything but ``NOT_GRANTED``. The validator accepts ``GRANTED_BY_OWNER:<reference>`` only because the owner may mark a
manifest later; accepting it enables NOTHING in this repository (no drill, no workflow reads it). ``plan_sha256`` covers every field except ``authorisation`` and itself,
so marking a manifest authorised keeps the digest, while any edit of the plan (a bound, a stop condition, a command, a credential name) changes it and is detected.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sys
from typing import Any, Dict, List, Mapping, Optional, Sequence

BOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BOT)
sys.path.insert(0, os.path.join(BOT, "tools"))

import runtime_capability as RC  # noqa: E402
import shadow  # noqa: E402

SCHEMA = "testnet_action_manifest/1"
NOT_GRANTED = "NOT_GRANTED"
_GRANTED = re.compile(r"^GRANTED_BY_OWNER:[A-Za-z0-9._-]{3,64}$")

GENERIC_CREDENTIAL_NAMES = frozenset({"BYBIT_API_KEY", "BYBIT_API_SECRET"})
DEFAULT_KEY_ENV = "BYBIT_TESTNET_DEDICATED_API_KEY"
DEFAULT_SECRET_ENV = "BYBIT_TESTNET_DEDICATED_API_SECRET"
_ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]{5,63}$")
_BAD_NAME_WORDS = ("MAINNET", "LIVE", "DEMO", "PROD", "EU")

MAX_ENTRIES = 1
MAX_LIFECYCLE_HOURS = 9.0
COMPLETE_LIFECYCLES = 1
MIN_NOTIONAL_USD = 5.0

#: (fact, endpoint, signed). Each must be a read the read_only_monitor role may make on the testnet host.
PREFLIGHT_READS = (
    ("account_identity", "/v5/user/query-api", True),
    ("position_mode", "/v5/position/list", True),
    ("margin_mode", "/v5/account/info", True),
    ("collateral", "/v5/account/wallet-balance", True),
    ("positions", "/v5/position/list", True),
    ("orders", "/v5/order/realtime", True),
    ("instrument_rules", "/v5/market/instruments-info", False),
    ("funding_interval_and_cap", "/v5/market/instruments-info", False),
    ("fee_tier", "/v5/account/fee-rate", True),
    ("clock_drift", "/v5/market/time", False),
    ("order_history_readable", "/v5/order/history", True),
)

#: Pinned (tools/testnet_certificate.section_c must agree: a test pins that).
STOP_CONDITIONS = ("any stage fails", "a position remains after unwind (HUMAN MUST ACT)", "DRILL_KILL file present", "entry ledger refuses",
                   "more than one entry in the UTC day", "clock drift or stale data refusal")
RECOVERY = {
    "close_command": "tools/close_testnet_short.py --arm",
    "reconcile_command": "tools/evidence_reconcile.py",
    "restart_rule": "a restart reads the venue position, never the local journal",
    "flat_rule": "FLAT means the venue's own position read says flat; never inferred from fill arithmetic",
}
EVIDENCE = ("execution records (--execution-records)", "drill evidence file (--evidence-path)", "cash ledger")

_TOP = ("schema", "venue", "symbol", "category", "mode", "credential_class", "credential_env", "bounds", "preflight_reads", "stop_conditions", "recovery",
        "evidence_capture", "authorisation", "plan_sha256")
_BOUNDS = ("max_notional_usd", "max_entries", "max_lifecycle_hours", "complete_lifecycles", "max_loss_usd")
_READ = ("fact", "endpoint", "signed")


class ManifestError(RuntimeError):
    pass


def _max_loss() -> float:
    import testnet_certificate as TC
    return float(TC.section_c()["max_loss_test_currency_usd"])


def plan_digest(m: Mapping[str, Any]) -> str:
    body = {k: v for k, v in m.items() if k not in ("plan_sha256", "authorisation")}
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode("ascii")).hexdigest()


def generate() -> Dict[str, Any]:
    cap = float(shadow.SHADOW_MAX_NOTIONAL_USD)
    m: Dict[str, Any] = {
        "schema": SCHEMA, "venue": "testnet", "symbol": "BTCUSDT", "category": "linear", "mode": "overlay",
        "credential_class": RC.TESTNET_CREDENTIAL_CLASS,
        "credential_env": {"api_key": DEFAULT_KEY_ENV, "api_secret": DEFAULT_SECRET_ENV},
        "bounds": {"max_notional_usd": cap, "max_entries": MAX_ENTRIES, "max_lifecycle_hours": MAX_LIFECYCLE_HOURS, "complete_lifecycles": COMPLETE_LIFECYCLES,
                   "max_loss_usd": _max_loss()},
        "preflight_reads": [{"fact": f, "endpoint": e, "signed": s} for f, e, s in PREFLIGHT_READS],
        "stop_conditions": list(STOP_CONDITIONS), "recovery": dict(RECOVERY), "evidence_capture": list(EVIDENCE),
        "authorisation": NOT_GRANTED,
    }
    if m["authorisation"] != "NOT_GRANTED":                        # literal on purpose: this tool never generates an authorised manifest
        raise ManifestError("the generator may only emit authorisation NOT_GRANTED")
    m["plan_sha256"] = plan_digest(m)
    return m


def _is_num(x: Any) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def _check_name(label: str, name: Any, out: List[str]) -> None:
    if not isinstance(name, str) or not _ENV_NAME.match(name):
        out.append(f"credential_env.{label}: not an upper-case env var name")
        return
    if name.upper() in GENERIC_CREDENTIAL_NAMES:
        out.append(f"credential_env.{label}: {name} is the generic credential; it is never a testnet credential")
    if "TESTNET" not in name:
        out.append(f"credential_env.{label}: {name} does not say TESTNET")
    if any(re.search(rf"(^|_){w}(_|$)", name) for w in _BAD_NAME_WORDS):
        out.append(f"credential_env.{label}: {name} names a non-testnet environment")


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
    if missing or extra:
        return out
    for k, want in (("schema", SCHEMA), ("venue", "testnet"), ("symbol", "BTCUSDT"), ("category", "linear"), ("mode", "overlay"),
                    ("credential_class", RC.TESTNET_CREDENTIAL_CLASS)):
        if m[k] != want or not isinstance(m[k], str):
            out.append(f"{k} must be exactly {want!r}")
    ce = m["credential_env"]
    if not isinstance(ce, dict) or set(ce) != {"api_key", "api_secret"}:
        out.append("credential_env must be exactly {api_key, api_secret}")
    else:
        _check_name("api_key", ce["api_key"], out)
        _check_name("api_secret", ce["api_secret"], out)
        if ce["api_key"] == ce["api_secret"]:
            out.append("credential_env: key and secret names must differ")
    b = m["bounds"]
    if not isinstance(b, dict) or set(b) != set(_BOUNDS):
        out.append(f"bounds must be exactly {list(_BOUNDS)}")
    else:
        cap, loss = float(shadow.SHADOW_MAX_NOTIONAL_USD), _max_loss()
        if not _is_num(b["max_notional_usd"]) or not (MIN_NOTIONAL_USD <= b["max_notional_usd"] <= cap):
            out.append(f"bounds.max_notional_usd must be a number in [{MIN_NOTIONAL_USD}, {cap}]")
        if not isinstance(b["max_entries"], int) or isinstance(b["max_entries"], bool) or b["max_entries"] != MAX_ENTRIES:
            out.append(f"bounds.max_entries must be the integer {MAX_ENTRIES}")
        if not _is_num(b["max_lifecycle_hours"]) or not (0 < b["max_lifecycle_hours"] <= MAX_LIFECYCLE_HOURS):
            out.append(f"bounds.max_lifecycle_hours must be a number in (0, {MAX_LIFECYCLE_HOURS}]")
        if not isinstance(b["complete_lifecycles"], int) or isinstance(b["complete_lifecycles"], bool) or b["complete_lifecycles"] != COMPLETE_LIFECYCLES:
            out.append(f"bounds.complete_lifecycles must be the integer {COMPLETE_LIFECYCLES}")
        if not _is_num(b["max_loss_usd"]) or not (0 < b["max_loss_usd"] <= loss):
            out.append(f"bounds.max_loss_usd must be a number in (0, {loss}]")
    pr = m["preflight_reads"]
    want_reads = [{"fact": f, "endpoint": e, "signed": s} for f, e, s in PREFLIGHT_READS]
    if not isinstance(pr, list) or any(not isinstance(r, dict) or set(r) != set(_READ) for r in pr):
        out.append("preflight_reads must be a list of {fact, endpoint, signed}")
    elif pr != want_reads:
        out.append("preflight_reads must be exactly the required read list (a fact removed, added or re-pointed)")
    else:
        monitor = RC.Capability.for_role(RC.READ_ONLY_MONITOR, venue="testnet")
        for r in pr:
            code = monitor.check("GET", r["endpoint"], r["signed"], monitor.host)
            if code is not None:
                out.append(f"preflight read {r['fact']}: read_only_monitor may not perform it ({code})")
    sc = m["stop_conditions"]
    if not isinstance(sc, list) or sc != list(STOP_CONDITIONS):
        out.append("stop_conditions must be exactly the required list (a stop condition lost, added or reworded)")
    if m["recovery"] != RECOVERY:
        out.append("recovery must be exactly the required recovery commands and rules")
    if m["evidence_capture"] != list(EVIDENCE):
        out.append("evidence_capture must be exactly the required list")
    a = m["authorisation"]
    if not isinstance(a, str) or not (a == NOT_GRANTED or _GRANTED.match(a)):
        out.append("authorisation must be 'NOT_GRANTED' or 'GRANTED_BY_OWNER:<reference>'")
    try:
        digest_ok = m["plan_sha256"] == plan_digest(m)
    except (TypeError, ValueError):
        digest_ok = False
    if not digest_ok:
        out.append("plan_sha256 does not match the content: the manifest was edited")
    return out


def validate(m: Any, env: Optional[Mapping[str, str]] = None) -> Dict[str, Any]:
    """Refusals plus a presence-only record of the named credentials (booleans; the values are never read into the report)."""
    ref = refusals(m)
    presence: Dict[str, Optional[bool]] = {"api_key": None, "api_secret": None}
    if not ref:
        e = os.environ if env is None else env
        presence = {k: bool(str(e.get(m["credential_env"][k], "") or "").strip()) for k in presence}
    return {"valid": not ref, "refusals": ref, "authorised": (not ref) and m["authorisation"] != NOT_GRANTED,
            "credential_presence": presence, "plan_sha256": m.get("plan_sha256") if isinstance(m, dict) else None,
            "note": "validation enables nothing; an authorised manifest is a record of the owner's instruction, not a switch"}


def _no_dupes(pairs: Sequence[Any]) -> Dict[str, Any]:
    d: Dict[str, Any] = {}
    for k, v in pairs:
        if k in d:
            raise ValueError(f"duplicate key {k!r}")
        d[k] = v
    return d


def _refuse_const(x: str) -> Any:
    raise ValueError(f"non-finite constant {x}")


def load(path: str) -> Any:
    with open(path, encoding="utf-8") as fh:
        return json.loads(fh.read(), object_pairs_hook=_no_dupes, parse_constant=_refuse_const)


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Testnet action manifest (offline).")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("generate")
    v = sub.add_parser("validate")
    v.add_argument("file")
    a = ap.parse_args(argv)
    if a.cmd == "generate":
        print(json.dumps(generate(), indent=2, sort_keys=True))
        return 0
    try:
        m = load(a.file)
    except (OSError, ValueError) as exc:
        print(json.dumps({"valid": False, "refusals": [f"unreadable manifest: {exc}"]}, indent=2))
        return 1
    rep = validate(m)
    print(json.dumps(rep, indent=2, sort_keys=True))
    return 0 if rep["valid"] else 1


if __name__ == "__main__":
    sys.exit(main())
