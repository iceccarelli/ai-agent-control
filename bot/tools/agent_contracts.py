#!/usr/bin/env python3
"""The ONLY way a research / challenger / allocation / ops agent (an LLM or a script) can influence the engine: a typed, validated PROPOSAL written to a quarantine file.

Agents may research, rank, challenge and propose. They may NOT: read or expose a credential, create the owner's grant or capability, change the economics hurdle, a risk cap or the
authority policy, call an exchange endpoint, submit an order, change a plan after its digest was confirmed, override a reconciliation failure, or enable Mainnet. A proposal is DATA:
it is never executed, never imported as code and never interpreted as a command. A human (or the deterministic pre-registration process in ``research/``) turns an accepted proposal into
a registered candidate, and a registered candidate still has to pass the economics gate, the risk caps and the authority before any order. This module therefore imports NOTHING that can
reach a venue (a test enforces that by walking its imports) and exposes no function that writes anywhere except ``proposals.jsonl`` in the quarantine directory it is given.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from typing import Any, Dict, List, Mapping, Optional, Tuple

ROLES = ("market_intelligence", "opportunity_research", "quant_challenger", "economics_allocator", "execution_monitor", "accounting_reporting", "infrastructure_ops", "red_team")
KINDS = ("HYPOTHESIS", "CANDIDATE_RANKING", "REJECTION", "CHALLENGE", "ALLOCATION_PROPOSAL", "INCIDENT_REPORT", "DATA_GAP", "INFRASTRUCTURE_FINDING")
MAX_TEXT = 2000
MAX_PARAMS = 20
# Parameter and field NAMES an agent may never set: they are the engine's decision inputs (the owner's, or fixed by code), not research output.
FORBIDDEN_KEYS = re.compile(r"(?i)(secret|token|api[_-]?key|password|credential|grant|capability|authori[sz]|hurdle|min_net_edge|haircut|max_notional|max_loss|cap_|_cap|policy|kill|"
                            r"leverage|live|mainnet|testnet_ack|order|submit|execute|digest|plan_|command|shell|script|exec|eval|url|webhook|endpoint)")
SECRETISH = re.compile(r"(?i)(-----BEGIN|eyJ[A-Za-z0-9_-]{10,}\.|[A-Za-z0-9+/]{40,}={0,2}|\b(?:sk|pk|ghp|xox[bp])[-_][A-Za-z0-9]{16,})")
CODEISH = re.compile(r"(?i)(import\s+\w+|__\w+__|subprocess|os\.system|eval\(|exec\(|curl\s|wget\s|rm\s+-|\bsudo\b|\$\(|`|;\s*\w+\s*\(|api\.bybit\.com)")


class ProposalRefused(ValueError):
    pass


def _check_text(name: str, v: Any, limit: int = MAX_TEXT) -> str:
    if not isinstance(v, str) or not v.strip() or len(v) > limit:
        raise ProposalRefused(f"{name}: a non-empty string of at most {limit} characters is required")
    if SECRETISH.search(v):
        raise ProposalRefused(f"{name}: looks like a secret or a key")
    if CODEISH.search(v):
        raise ProposalRefused(f"{name}: looks like code or a command (proposals are data, never instructions)")
    return v.strip()


def validate(p: Mapping[str, Any]) -> Dict[str, Any]:
    """Strict: a closed set of keys, a known role and kind, bounded text, scalar parameters only, no forbidden key name, no secret-, code- or command-looking text."""
    allowed = {"role", "kind", "symbol", "summary", "rationale", "parameters", "evidence_refs", "created_ms"}
    if not isinstance(p, Mapping) or set(p) - allowed or not {"role", "kind", "summary", "rationale", "created_ms"} <= set(p):
        raise ProposalRefused("shape: unknown or missing keys")
    if p["role"] not in ROLES or p["kind"] not in KINDS:
        raise ProposalRefused("role/kind not allowed")
    if not (isinstance(p["created_ms"], int) and not isinstance(p["created_ms"], bool) and p["created_ms"] > 0):
        raise ProposalRefused("created_ms")
    sym = p.get("symbol")
    if sym is not None and not (isinstance(sym, str) and re.fullmatch(r"[A-Z0-9]{3,20}", sym)):
        raise ProposalRefused("symbol")
    params = p.get("parameters", {})
    if not isinstance(params, Mapping) or len(params) > MAX_PARAMS:
        raise ProposalRefused("parameters: a small mapping of scalars")
    clean_params: Dict[str, Any] = {}
    for k, v in params.items():
        if not isinstance(k, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,40}", k) or FORBIDDEN_KEYS.search(k):
            raise ProposalRefused(f"parameters.{k!r}: a decision input / secret / command-like name is not allowed")
        if isinstance(v, bool) or not isinstance(v, (int, float, str)) or (isinstance(v, str) and len(v) > 200):
            raise ProposalRefused(f"parameters.{k}: scalars only")
        if isinstance(v, float) and v != v:
            raise ProposalRefused(f"parameters.{k}: NaN")
        if isinstance(v, str):
            _check_text(f"parameters.{k}", v, 200)
        clean_params[k] = v
    refs = p.get("evidence_refs", [])
    if not (isinstance(refs, list) and len(refs) <= 20 and all(isinstance(r, str) and re.fullmatch(r"[A-Za-z0-9_./:@-]{1,120}", r) for r in refs)):
        raise ProposalRefused("evidence_refs: up to 20 short identifiers")
    return {"role": p["role"], "kind": p["kind"], "symbol": sym, "summary": _check_text("summary", p["summary"], 300), "rationale": _check_text("rationale", p["rationale"]),
            "parameters": clean_params, "evidence_refs": list(refs), "created_ms": p["created_ms"]}


def submit(quarantine_dir: str, p: Mapping[str, Any]) -> Tuple[bool, str]:
    """Validate and APPEND to ``<quarantine_dir>/proposals.jsonl`` (idempotent by content hash). Returns (stored, proposal_id). The only side effect this module has."""
    c = validate(p)
    body = json.dumps(c, sort_keys=True, separators=(",", ":"))
    pid = "prop_" + hashlib.sha256(body.encode("utf-8")).hexdigest()[:24]
    os.makedirs(quarantine_dir, mode=0o700, exist_ok=True)
    path = os.path.join(quarantine_dir, "proposals.jsonl")
    try:
        with open(path, encoding="utf-8") as fh:
            if any(json.loads(line).get("id") == pid for line in fh if line.strip()):
                return False, pid
    except FileNotFoundError:
        pass
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        os.write(fd, (json.dumps({"id": pid, "status": "PROPOSED_NOT_EXECUTABLE", **c}, sort_keys=True) + "\n").encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    return True, pid
