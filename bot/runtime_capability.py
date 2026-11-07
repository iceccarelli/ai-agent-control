"""Runtime capability model: what each deployed ROLE may do, as code that can be tested and consulted before anything is signed or sent.

A deployment that "only collects data" is a claim about config until the process physically cannot sign or POST. This module makes the claim a property of an object:

* ``public_collector``   unsigned GET of a fixed allow-list of PUBLIC market paths on the venue's registry host. Holds no credential: construction REFUSES a
                         key/secret argument and refuses an environment mapping that carries BYBIT_*KEY / BYBIT_*SECRET at all.
* ``read_only_monitor``  signed GET of a fixed allow-list of account READ paths (plus the public market reads it needs, unsigned). Never POST/DELETE; no order,
                         leverage, transfer, withdrawal, asset or API-key path exists in its list.
* ``evidence_backup``    no venue access at all. Every HTTP request is refused. No credentials either.
* ``testnet_executor``   the only role that may POST order endpoints, and only to the registry's TESTNET host, with ``credential_class == "testnet_dedicated"``.
                         Mainnet, demo, unqualified and unknown venues cannot construct it.

``Capability.check(method, endpoint, signed, host)`` returns ``None`` (allowed) or a refusal CODE (a string). It is strict: the endpoint must be canonical
(lower-case ``/v5/...``, no query, fragment, whitespace, double slash, dot segment or trailing slash), the method exactly ``GET`` or ``POST``, the host exactly the
registry host, and an endpoint that is not in the role's list is refused - there is no default-allow and no prefix match.

``CapabilityTransport`` applies the same check to any transport object (``request(method, url, *, headers, body, timeout)``) so a collector built on it cannot sign or
POST even if its own code is buggy: it inspects the URL actually being sent and the headers (an API-key / signature header means "signed").

Pure: standard library plus ``venues``; no I/O, no environment read, nothing that can sign (a test pins the import list).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, FrozenSet, Mapping, Optional, Tuple
from urllib.parse import urlsplit

import venues as _venues

PUBLIC_COLLECTOR = "public_collector"
READ_ONLY_MONITOR = "read_only_monitor"
EVIDENCE_BACKUP = "evidence_backup"
TESTNET_EXECUTOR = "testnet_executor"
ROLES: Tuple[str, ...] = (PUBLIC_COLLECTOR, READ_ONLY_MONITOR, EVIDENCE_BACKUP, TESTNET_EXECUTOR)

TESTNET_CREDENTIAL_CLASS = "testnet_dedicated"

#: Public market data (unsigned GET).
PUBLIC_MARKET_PATHS: FrozenSet[str] = frozenset({
    "/v5/market/time", "/v5/market/tickers", "/v5/market/orderbook", "/v5/market/kline",
    "/v5/market/instruments-info", "/v5/market/funding/history",
})

#: Account READ paths (signed GET). Nothing here can move money or change state.
ACCOUNT_READ_PATHS: FrozenSet[str] = frozenset({
    "/v5/account/wallet-balance", "/v5/account/fee-rate", "/v5/account/info", "/v5/position/list", "/v5/position/closed-pnl",
    "/v5/order/realtime", "/v5/order/history", "/v5/execution/list", "/v5/user/query-api",
})

#: Signed POSTs the executor may send. Amend, batch create/amend, transfers, withdrawals and API-key management are deliberately absent.
EXECUTOR_POST_PATHS: FrozenSet[str] = frozenset({
    "/v5/order/create", "/v5/order/cancel", "/v5/order/cancel-all", "/v5/position/set-leverage", "/v5/position/trading-stop",
})

#: Families that no role may ever reach, named so the refusal says why (they would be refused as unknown anyway).
FORBIDDEN_FAMILIES: Tuple[str, ...] = ("/v5/asset/", "/v5/user/", "/v5/account/withdraw", "/v5/account/transfer", "/v5/spot-margin", "/v5/crypto-loan",
                                       "/v5/lending", "/v5/broker/", "/v5/affiliate/", "/v5/earn/")
_FORBIDDEN_WORDS = ("withdraw", "transfer", "deposit", "sub-member", "create-sub", "api-key", "apikey", "universal")

_CANONICAL = re.compile(r"^/v5(/[a-z0-9]+(-[a-z0-9]+)*)+$")
_CREDENTIAL_ENV = re.compile(r"^BYBIT_.*(KEY|SECRET|TOKEN)", re.IGNORECASE)
_SIGNED_HEADERS = ("x-bapi-api-key", "x-bapi-sign", "x-bapi-sign-type", "x-bapi-timestamp", "authorization", "x-api-key")


class CapabilityRefused(Exception):
    """Raised by ``CapabilityTransport`` / ``Capability.enforce``; ``code`` is the refusal code."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code


class CapabilityConstructionError(ValueError):
    """The role cannot be built from what it was given (credential where none may exist, wrong venue, wrong credential class)."""


@dataclass(frozen=True)
class Capability:
    role: str
    venue: str
    host: str
    credential_class: Optional[str]

    # -- construction ---------------------------------------------------------------------------------------------------------------------------------------
    @staticmethod
    def for_role(role: str, *, venue: str = "testnet", credential_class: Optional[str] = None, api_key: Any = None, api_secret: Any = None,
                 env: Optional[Mapping[str, str]] = None) -> "Capability":
        if role not in ROLES:
            raise CapabilityConstructionError(f"unknown role {role!r}; roles are {ROLES}")
        try:
            v = _venues.get(venue)
        except (_venues.UnknownVenue, _venues.VenueNotQualified) as exc:
            raise CapabilityConstructionError(f"venue refused: {exc}") from None
        if role in (PUBLIC_COLLECTOR, EVIDENCE_BACKUP):
            if api_key or api_secret:
                raise CapabilityConstructionError(f"{role} holds no credentials: a key/secret was supplied")
            if credential_class:
                raise CapabilityConstructionError(f"{role} holds no credentials: credential_class {credential_class!r} was supplied")
            if env is not None:
                bad = sorted(k for k in env if isinstance(k, str) and _CREDENTIAL_ENV.match(k))
                if bad:
                    raise CapabilityConstructionError(f"{role} refuses an environment that carries credentials: {bad}")
        if role == TESTNET_EXECUTOR:
            if v.name != "testnet" or v.is_live:
                raise CapabilityConstructionError(f"{role} may only be built for the registry's testnet venue, not {v.name!r}")
            if credential_class != TESTNET_CREDENTIAL_CLASS:
                raise CapabilityConstructionError(f"{role} needs credential_class={TESTNET_CREDENTIAL_CLASS!r}, got {credential_class!r}")
        return Capability(role=role, venue=v.name, host=v.host, credential_class=credential_class)

    # -- the check ------------------------------------------------------------------------------------------------------------------------------------------
    def check(self, method: Any, endpoint: Any, signed: Any, host: Any) -> Optional[str]:
        """None when this role may send exactly this request; otherwise a refusal code."""
        if self.role not in ROLES:
            return "ROLE_UNKNOWN"
        if self.role == EVIDENCE_BACKUP:
            return "NO_VENUE_ACCESS: evidence_backup may not make any HTTP request"
        if not isinstance(method, str) or method not in ("GET", "POST"):
            return "METHOD_REFUSED"
        if not isinstance(endpoint, str) or not _CANONICAL.match(endpoint):
            return "ENDPOINT_NOT_CANONICAL"
        if not isinstance(signed, bool):
            return "SIGNED_FLAG_NOT_BOOLEAN"
        if not isinstance(host, str) or host != self.host:
            return "HOST_REFUSED"
        if self.role == TESTNET_EXECUTOR and (self.host != _venues.get("testnet").host or self.credential_class != TESTNET_CREDENTIAL_CLASS):
            return "EXECUTOR_NOT_TESTNET"
        if any(endpoint.startswith(f) for f in FORBIDDEN_FAMILIES if f != "/v5/user/") or any(w in endpoint for w in _FORBIDDEN_WORDS):
            return "ENDPOINT_FORBIDDEN"
        if endpoint.startswith("/v5/user/") and endpoint not in ACCOUNT_READ_PATHS:
            return "ENDPOINT_FORBIDDEN"
        if self.role == PUBLIC_COLLECTOR:
            if method != "GET":
                return "COLLECTOR_GET_ONLY"
            if signed:
                return "COLLECTOR_UNSIGNED_ONLY"
            return None if endpoint in PUBLIC_MARKET_PATHS else "ENDPOINT_NOT_ALLOWED"
        if self.role == READ_ONLY_MONITOR:
            if method != "GET":
                return "MONITOR_GET_ONLY"
            if signed:
                return None if endpoint in ACCOUNT_READ_PATHS else "ENDPOINT_NOT_ALLOWED"
            return None if endpoint in PUBLIC_MARKET_PATHS else "ENDPOINT_NOT_ALLOWED"
        # testnet_executor
        if method == "GET":
            if signed:
                return None if endpoint in ACCOUNT_READ_PATHS else "ENDPOINT_NOT_ALLOWED"
            return None if endpoint in PUBLIC_MARKET_PATHS else "ENDPOINT_NOT_ALLOWED"
        if not signed:
            return "POST_MUST_BE_SIGNED"
        return None if endpoint in EXECUTOR_POST_PATHS else "ENDPOINT_NOT_ALLOWED"

    def enforce(self, method: Any, endpoint: Any, signed: Any, host: Any) -> None:
        code = self.check(method, endpoint, signed, host)
        if code is not None:
            raise CapabilityRefused(code, f"role={self.role} {method} {endpoint!r} signed={signed!r} host={host!r}")


def _header_says_signed(headers: Optional[Mapping[str, Any]]) -> bool:
    return any(isinstance(k, str) and k.lower() in _SIGNED_HEADERS for k in (headers or {}))


class CapabilityTransport:
    """Wraps any transport; refuses a request the role may not make BEFORE it reaches the inner transport."""

    def __init__(self, inner: Any, capability: Capability) -> None:
        self._inner = inner
        self.capability = capability

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def request(self, method: Any, url: Any, *, headers: Optional[Mapping[str, str]] = None, body: Optional[str] = None, timeout: Any = None, **kw: Any) -> Any:
        if kw:
            raise CapabilityRefused("UNKNOWN_TRANSPORT_ARGUMENTS", str(sorted(kw)))
        if not isinstance(url, str):
            raise CapabilityRefused("URL_REFUSED", "not a string")
        parts = urlsplit(url)
        host_raw = parts.netloc
        if parts.scheme != "https" or "@" in host_raw or ":" in host_raw or host_raw != host_raw.strip() or parts.fragment or parts.username or parts.password:
            raise CapabilityRefused("URL_REFUSED", f"scheme/userinfo/port/fragment in {url!r}")
        if method == "POST" and parts.query:
            raise CapabilityRefused("URL_REFUSED", "query string on a POST")
        if method == "GET" and body:
            raise CapabilityRefused("URL_REFUSED", "body on a GET")
        self.capability.enforce(method, parts.path, _header_says_signed(headers), host_raw)
        return self._inner.request(method, url, headers=headers if headers is not None else {}, body=body, timeout=timeout)
