"""Turns owner-collected, READ-ONLY, redacted evidence into a verdict on whether a
venue that is not yet qualified (today: Bybit EU) could carry the strategy.

This module never touches the network, never reads a credential and never
authorises an order. It exists because the question "does this account offer
BTCUSDT linear perpetual through the API?" cannot be answered from documentation
(the official EU documentation that could be read describes no derivatives API)
and must not be answered by inference from the global documentation. The owner
collects the evidence from their own account; this function says, mechanically,
whether the evidence supports qualification, and why not.

Verdicts:
  QUALIFIED      every required fact is present, consistent and favourable. This
                 means only that the EVIDENCE is consistent. Registering the venue
                 in `venues` and arming it are separate, human decisions.
  NOT_QUALIFIED  at least one fact contradicts qualification (a refusal reason is
                 named). Nothing can override this except new evidence.
  UNVERIFIED     nothing contradicts it but a required fact is missing.

Evidence must carry no secrets: a field that looks like a key, secret, token or
signature makes the evidence unusable (the repository is public).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Tuple

QUALIFIED = "QUALIFIED"
NOT_QUALIFIED = "NOT_QUALIFIED"
UNVERIFIED = "UNVERIFIED"

EU_REST_HOST = "api.bybit.eu"
_SECRET_SHAPED = re.compile(r"(api[_-]?key|secret|token|passw|signature|private[_-]?key|x-bapi)", re.I)
_DERIVATIVES_PERMISSIONS = ("ContractTrade", "Derivatives")


@dataclass(frozen=True)
class Qualification:
    verdict: str
    reasons: Tuple[str, ...]          # why NOT_QUALIFIED
    missing: Tuple[str, ...]          # what UNVERIFIED lacks
    #: Always False: a qualification result is not an authorisation to send orders.
    authorises_orders: bool = False


def _secret_fields(obj: Any, path: str = "") -> List[str]:
    out: List[str] = []
    if isinstance(obj, Mapping):
        for k, v in obj.items():
            here = f"{path}.{k}" if path else str(k)
            if _SECRET_SHAPED.search(str(k)):
                out.append(here)
            out += _secret_fields(v, here)
    elif isinstance(obj, (list, tuple)):
        for i, v in enumerate(obj):
            out += _secret_fields(v, f"{path}[{i}]")
    return out


def _non_empty(x: Any) -> bool:
    return isinstance(x, str) and bool(x.strip())


def evaluate_eu(evidence: Any) -> Qualification:
    if not isinstance(evidence, Mapping):
        return Qualification(NOT_QUALIFIED, ("EVIDENCE_NOT_AN_OBJECT",), ())
    leaked = _secret_fields(evidence)
    if leaked:
        return Qualification(NOT_QUALIFIED, ("EVIDENCE_CONTAINS_SECRET_SHAPED_FIELD:" + ",".join(leaked[:5]),), ())
    reasons: List[str] = []
    missing: List[str] = []

    host = evidence.get("rest_host")
    if host is None:
        missing.append("rest_host")
    elif host != EU_REST_HOST:
        reasons.append(f"WRONG_HOST:{host!r} (the EU account's REST host is {EU_REST_HOST}; the global host is never substituted)")

    for key in ("ws_public_host", "ws_private_host"):
        v = evidence.get(key)
        if not _non_empty(v):
            missing.append(key)
        elif not v.endswith(".bybit.eu"):
            reasons.append(f"WRONG_{key.upper()}:{v!r}")

    docs = evidence.get("official_docs_urls")
    if not (isinstance(docs, (list, tuple)) and docs and all(_non_empty(u) and u.startswith("https://") for u in docs)):
        missing.append("official_docs_urls")

    inst = evidence.get("instruments_linear_btcusdt")
    if not isinstance(inst, Mapping):
        missing.append("instruments_linear_btcusdt")
    else:
        if inst.get("retCode") != 0:
            reasons.append(f"LINEAR_INSTRUMENT_RETCODE_NOT_ZERO:{inst.get('retCode')!r}")
        elif inst.get("status") != "Trading":
            reasons.append(f"LINEAR_INSTRUMENT_NOT_TRADING:{inst.get('status')!r}")

    keyp = evidence.get("key_permissions")
    if not isinstance(keyp, Mapping):
        missing.append("key_permissions")
    else:
        perms = keyp.get("permissions")
        if not isinstance(perms, Mapping):
            missing.append("key_permissions.permissions")
        else:
            if not any(perms.get(p) for p in _DERIVATIVES_PERMISSIONS):
                reasons.append("NO_DERIVATIVES_PERMISSION: the key offers no ContractTrade/Derivatives permission")
            if any("Withdraw" in (v or []) for v in perms.values() if isinstance(v, (list, tuple))):
                reasons.append("WITHDRAW_PERMISSION_PRESENT")
        ips = keyp.get("ips")
        if keyp.get("ip_restricted") is not True or not (isinstance(ips, (list, tuple)) and ips):
            reasons.append("KEY_NOT_IP_RESTRICTED")
        if keyp.get("read_only") is not True:
            missing.append("key_permissions.read_only_qualification_key")

    sup = evidence.get("support_confirmation")
    if not (isinstance(sup, Mapping) and _non_empty(sup.get("reference")) and _non_empty(sup.get("statement"))):
        missing.append("support_confirmation")

    if reasons:
        return Qualification(NOT_QUALIFIED, tuple(reasons), tuple(missing))
    if missing:
        return Qualification(UNVERIFIED, (), tuple(missing))
    return Qualification(QUALIFIED, (), ())


OWNER_COMMANDS: Dict[str, str] = {
    "instruments_linear_btcusdt": "curl -sS 'https://api.bybit.eu/v5/market/instruments-info?category=linear&symbol=BTCUSDT'  # public, no key",
    "instruments_spot_btcusdt": "curl -sS 'https://api.bybit.eu/v5/market/instruments-info?category=spot&symbol=BTCUSDT'  # public, no key",
    "key_permissions": "a READ-ONLY key, signed GET https://api.bybit.eu/v5/user/query-api  # run by the owner; paste only the redacted permissions/ips/readOnly fields",
    "support_confirmation": "Bybit EU support ticket asking in writing whether BTCUSDT linear perpetual is available to this account via API",
}
