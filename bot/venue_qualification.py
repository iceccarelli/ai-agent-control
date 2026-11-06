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

#: Sanity bounds so that a mistyped or pasted-as-percent number cannot "qualify" an account.
MAX_PLAUSIBLE_TAKER_RATE = 0.005        # 50 bps: far above any Bybit tier, far below a percent-as-fraction slip
MIN_PLAUSIBLE_TAKER_RATE = 0.00001      # 0.1 bps: below this is a typo, not a fee tier
MIN_BTC_COLLATERAL_RATIO = 0.10
#: The ONLY accepted provenance strings. A free-text `source` (a URL, a sentence, a copied query string) proves
#: nothing about where a number came from and is where credentials get pasted by accident.
FEE_RATE_SOURCE = "GET /v5/account/fee-rate"
COLLATERAL_SOURCE = "GET /v5/account/collateral-info"

QUALIFIED = "QUALIFIED"
NOT_QUALIFIED = "NOT_QUALIFIED"
UNVERIFIED = "UNVERIFIED"

EU_REST_HOST = "api.bybit.eu"
_SECRET_SHAPED = re.compile(r"(api[_-]?key|secret|token|passw|signature|private[_-]?key|x-bapi)", re.I)
_SECRET_VALUE = re.compile(r"(api[_-]?key|secret|signature|x-bapi[\w-]*|passw\w*|private[_-]?key|\bsign|\btoken)\s*[=:]", re.I)
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
    elif isinstance(obj, str) and _SECRET_VALUE.search(obj):
        out.append(path or "<root>")              # a credential pasted into a VALUE (api_key=..., sign=...)
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
        rc = inst.get("retCode")
        if not (type(rc) is int and rc == 0):                # False == 0 in Python: a boolean is not a return code
            reasons.append(f"LINEAR_INSTRUMENT_RETCODE_NOT_ZERO:{rc!r}")
        elif inst.get("symbol") not in (None, "BTCUSDT"):
            reasons.append(f"LINEAR_INSTRUMENT_WRONG_SYMBOL:{inst.get('symbol')!r}")
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
            def _items(v: Any) -> List[str]:
                if isinstance(v, str):
                    return [v]
                return [str(x) for x in v] if isinstance(v, (list, tuple)) else []
            if any("withdraw" in x.lower() for k, v in perms.items() for x in [str(k)] + _items(v) if v):
                reasons.append("WITHDRAW_PERMISSION_PRESENT")
        ips = keyp.get("ips")
        if keyp.get("ip_restricted") is not True or not (isinstance(ips, (list, tuple)) and ips):
            reasons.append("KEY_NOT_IP_RESTRICTED")
        if keyp.get("read_only") is not True:
            missing.append("key_permissions.read_only_qualification_key")

    fee = evidence.get("fee_rates")
    if not isinstance(fee, Mapping):
        missing.append("fee_rates")
    else:
        tk, mk = fee.get("taker_fee_rate"), fee.get("maker_fee_rate")
        fin = lambda x: isinstance(x, (int, float)) and not isinstance(x, bool) and x == x and abs(x) != float("inf")   # noqa: E731
        if fee.get("source") != FEE_RATE_SOURCE:
            reasons.append(f"FEE_RATES_INVALID: source must be exactly {FEE_RATE_SOURCE!r} (free text proves nothing)")
        elif not (fin(tk) and fin(mk)) or not (MIN_PLAUSIBLE_TAKER_RATE <= tk <= MAX_PLAUSIBLE_TAKER_RATE) \
                or not (-MAX_PLAUSIBLE_TAKER_RATE <= mk <= tk):
            reasons.append("FEE_RATES_INVALID: taker must be a fraction between "
                           f"{MIN_PLAUSIBLE_TAKER_RATE} and {MAX_PLAUSIBLE_TAKER_RATE} (0.00055 is 5.5 bps; a percent pasted as a "
                           "fraction is refused) and maker a fraction no greater than taker (a rebate is negative)")
        elif fee.get("symbol") != "BTCUSDT" or fee.get("category") != "linear":
            reasons.append("FEE_RATES_FOR_WRONG_INSTRUMENT")

    col = evidence.get("collateral_rules")
    if not isinstance(col, Mapping):
        missing.append("collateral_rules")
    else:
        ratio = col.get("collateral_ratio")
        if col.get("coin") != "BTC" or col.get("source") != COLLATERAL_SOURCE \
                or col.get("margin_collateral") is not True \
                or not (isinstance(ratio, (int, float)) and not isinstance(ratio, bool) and MIN_BTC_COLLATERAL_RATIO <= ratio <= 1):
            reasons.append("COLLATERAL_RULES_INVALID: BTC must be usable as margin collateral (margin_collateral true) with a "
                           f"collateral ratio in [{MIN_BTC_COLLATERAL_RATIO}, 1], read from GET /v5/account/collateral-info")

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
    "fee_rates": "a READ-ONLY key, signed GET https://api.bybit.eu/v5/account/fee-rate?category=linear&symbol=BTCUSDT  # the account's REAL taker/maker rates; paste only those numbers",
    "collateral_rules": "a READ-ONLY key, signed GET https://api.bybit.eu/v5/account/collateral-info?currency=BTC  # collateral ratio and whether BTC can be margin collateral",
    "support_confirmation": "Bybit EU support ticket asking in writing whether BTCUSDT linear perpetual is available to this account via API",
}
