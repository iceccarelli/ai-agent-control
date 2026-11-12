"""The dedicated Bybit TESTNET transport for the multi-symbol execution path (TESTNET_EXECUTOR role). Importing this module never touches the network and never reads the
environment: credentials arrive ONLY as an explicit mapping handed in by the owner session tool.

Facts checked against the current official Bybit V5 documentation (bybit-exchange.github.io/docs/v5, fetched 2026-11-08):
  * testnet REST base is ``https://api-testnet.bybit.com`` (guide); signed headers are X-BAPI-API-KEY / X-BAPI-TIMESTAMP / X-BAPI-SIGN / X-BAPI-RECV-WINDOW (default 5000 ms); the
    signature is HMAC_SHA256 hex over ``timestamp + apiKey + recvWindow + (queryString | jsonBody)`` (that is ``BybitClient._sign``: reused, not re-implemented here);
  * ``GET /v5/user/query-api`` reports ``readOnly`` (0 read+write, 1 read only), ``permissions`` (an object of arrays; ``Wallet`` lists AccountTransfer / SubMemberTransfer /
    Withdraw, an empty array = not granted) and ``ips``;
  * US / Mainland-China IPs get HTTP 403 (this is why nothing here can be run from the build container).

What this transport adds on top of the repo's machinery (``runtime_capability.CapabilityTransport`` wrapping any inner ``request(method, url, headers=, body=, timeout=)``):
  * the host must be EXACTLY the registry's testnet host (``venues``), https, no port / userinfo / trailing dot / upper case / IP literal, canonical path under ``/v5/``;
  * only the headers the signed client sends are allowed (no ``Host`` / ``X-Forwarded-*`` / ``Proxy-*`` re-routing);
  * a 3xx answer is REFUSED (the real inner transport does not follow redirects, trusts no environment proxy and sends no ``.netrc``);
  * credentials come only from ``BYBIT_TESTNET_API_KEY`` / ``BYBIT_TESTNET_API_SECRET``; the generic ``BYBIT_API_KEY`` / ``BYBIT_API_SECRET`` are IGNORED, never a fallback.
"""
from __future__ import annotations

import os
import sys
from typing import Any, Dict, List, Mapping, Optional, Tuple
from urllib.parse import urlsplit

BOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (BOT, os.path.join(BOT, "tools")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import bybit_connection as bc  # noqa: E402
import ms_capital_anchor as AN  # noqa: E402
import runtime_capability as RC  # noqa: E402
import venues as _venues  # noqa: E402

KEY_ENV = "BYBIT_TESTNET_API_KEY"
SECRET_ENV = "BYBIT_TESTNET_API_SECRET"
GENERIC_NAMES = ("BYBIT_API_KEY", "BYBIT_API_SECRET")

SETUP_INSTRUCTIONS = (
    "Dedicated Bybit TESTNET credentials are missing. Create a key on https://testnet.bybit.com (API management) with ONLY 'Contract - Orders & Positions' and 'Spot - Trade' "
    "(no Wallet/transfer/withdraw, read-write), then add two GitHub Codespaces secrets (GitHub > Settings > Codespaces > Secrets, repository access: this repository): "
    f"{KEY_ENV} and {SECRET_ENV}. Restart the codespace so they are exported. The generic BYBIT_API_KEY / BYBIT_API_SECRET are never used for testnet execution.")

_ALLOWED_HEADERS = frozenset({"content-type", "accept", "user-agent", "x-bapi-api-key", "x-bapi-timestamp", "x-bapi-recv-window", "x-bapi-sign", "x-bapi-sign-type"})


class TestnetTransportRefused(Exception):
    """The request / response was refused by the testnet transport (a stable ``code``). Carries no credential."""
    __test__ = False

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code


class TestnetCredentialsMissing(Exception):
    """The dedicated testnet credential variables are not both present. The message is the exact setup procedure; it never names a value."""
    __test__ = False

    def __init__(self, missing: List[str]) -> None:
        super().__init__(f"missing: {', '.join(missing)}. {SETUP_INSTRUCTIONS}")
        self.missing = list(missing)


class TestnetCredentials:
    """The dedicated key/secret. repr/str/pickle never show them. Built only by ``from_mapping`` (an explicit mapping; this module never reads ``os.environ``)."""
    __test__ = False
    __slots__ = ("_key", "_secret")

    def __init__(self, key: str, secret: str) -> None:
        object.__setattr__(self, "_key", key)
        object.__setattr__(self, "_secret", secret)

    def __setattr__(self, *_a: Any) -> None:
        raise AttributeError("immutable")

    def __repr__(self) -> str:
        return "<TestnetCredentials key=present secret=present>"

    __str__ = __repr__

    def __reduce__(self) -> Any:
        raise TypeError("TestnetCredentials are not serialisable")

    def __copy__(self) -> Any:
        raise TypeError("TestnetCredentials are not copyable")

    def __deepcopy__(self, _m: Any) -> Any:
        raise TypeError("TestnetCredentials are not copyable")

    @staticmethod
    def presence(env: Optional[Mapping[str, Any]]) -> Dict[str, bool]:
        e = env or {}
        return {"dedicated_key_present": bool(str(e.get(KEY_ENV, "") or "").strip()), "dedicated_secret_present": bool(str(e.get(SECRET_ENV, "") or "").strip())}

    @classmethod
    def from_mapping(cls, env: Optional[Mapping[str, Any]]) -> "TestnetCredentials":
        p = cls.presence(env)
        missing = [n for n, ok in ((KEY_ENV, p["dedicated_key_present"]), (SECRET_ENV, p["dedicated_secret_present"])) if not ok]
        if missing:
            raise TestnetCredentialsMissing(missing)           # NEVER a fall back to BYBIT_API_KEY / BYBIT_API_SECRET
        assert env is not None
        return cls(str(env[KEY_ENV]).strip(), str(env[SECRET_ENV]).strip())

    def _reveal(self) -> Tuple[str, str]:
        return self._key, self._secret


def testnet_host() -> str:
    v = _venues.get("testnet")
    if v.is_live or v.name != "testnet":
        raise TestnetTransportRefused("REGISTRY_TESTNET_NOT_SANDBOX")
    return v.host


class TestnetTransport(RC.CapabilityTransport):
    """CapabilityTransport for the TESTNET_EXECUTOR role with extra host / header / redirect refusals. The ONLY class ``ms_authority`` admits as a real testnet transport."""
    __test__ = False

    def __init__(self, inner: Any, capability: Optional[RC.Capability] = None) -> None:
        cap = capability or RC.Capability.for_role(RC.TESTNET_EXECUTOR, venue="testnet", credential_class=RC.TESTNET_CREDENTIAL_CLASS)
        if cap.role != RC.TESTNET_EXECUTOR or cap.venue != "testnet" or cap.credential_class != RC.TESTNET_CREDENTIAL_CLASS or cap.host != testnet_host():
            raise TestnetTransportRefused("CAPABILITY_NOT_TESTNET_EXECUTOR")
        super().__init__(inner, cap)
        self.credential_names = (KEY_ENV, SECRET_ENV)
        self.requests_sent = 0

    def request(self, method: Any, url: Any, *, headers: Any = None, body: Optional[str] = None, timeout: Any = None, **kw: Any) -> Any:
        if not isinstance(url, str):
            raise TestnetTransportRefused("URL_NOT_TEXT")
        p = urlsplit(url)
        host = testnet_host()
        if (p.scheme != "https" or p.netloc != host or (p.hostname or "") != host or url.split("/", 3)[2:3] != [host] or not p.path.startswith("/v5/")
                or p.username or p.password or p.port is not None):
            raise TestnetTransportRefused("HOST_NOT_EXACT_TESTNET_REST_HOST")
        names = []
        if isinstance(headers, Mapping):
            names = [str(k).casefold() for k in headers]
        elif headers:
            raise TestnetTransportRefused("HEADERS_NOT_A_MAPPING")
        extra = sorted(n for n in names if n not in _ALLOWED_HEADERS)
        if extra:
            raise TestnetTransportRefused("HEADER_NOT_ALLOWED", ",".join(extra))
        self.requests_sent += 1
        status, text = super().request(method, url, headers=headers, body=body, timeout=timeout, **kw)
        if isinstance(status, int) and 300 <= status < 400:
            raise TestnetTransportRefused("REDIRECT_REFUSED", f"HTTP {status}")
        return status, text


def is_testnet_executor_transport(t: Any) -> bool:
    """True only for an instance of EXACTLY ``TestnetTransport`` with the TESTNET_EXECUTOR capability (a subclass or a look-alike is not it)."""
    try:
        cap = t.capability
        return (type(t) is TestnetTransport and cap.role == RC.TESTNET_EXECUTOR and cap.venue == "testnet" and cap.credential_class == RC.TESTNET_CREDENTIAL_CLASS
                and cap.host == testnet_host() and t.credential_names == (KEY_ENV, SECRET_ENV))
    except Exception:                                                  # noqa: BLE001
        return False


class NoRedirectInner(bc.RequestsTransport):
    """The REAL inner transport (built only by the owner session tool): the repo's pooled ``requests`` session with redirects off, no environment proxy, no netrc."""

    def __init__(self) -> None:
        super().__init__()
        self._session.trust_env = False
        self._session.proxies = {}
        self._session.max_redirects = 0

    def request(self, method: str, url: str, *, headers: Mapping[str, str], params: Optional[Mapping[str, Any]] = None, body: Optional[str] = None, timeout: float = 10.0) -> Tuple[int, str]:
        r = self._session.request(method.upper(), url, headers=dict(headers), params=params, data=body, timeout=timeout, allow_redirects=False)
        self.last_headers = {k.lower(): v for k, v in r.headers.items()}
        return r.status_code, r.text


class _Config:
    """The config object ``BybitClient`` wants. Sandbox flags are stated TRUE; no live acknowledgement exists on it. repr hides the credential."""

    def __init__(self, creds: TestnetCredentials) -> None:
        k, s = creds._reveal()
        self.BYBIT_VENUE = "testnet"
        self.USE_TESTNET = True
        self.CATEGORY = "linear"
        self.BYBIT_API_KEY = k
        self.BYBIT_API_SECRET = s
        self.DEFAULT_LEVERAGE = 1
        self.REQUEST_TIMEOUT_SECONDS = 10.0
        self.BYBIT_RECV_WINDOW_MS = 5000

    def __repr__(self) -> str:
        return "<testnet config; credentials hidden>"


class _NullStore:
    """BybitClient wants a store; the multi-symbol path keeps its own journal and never uses it."""

    def __repr__(self) -> str:
        return "<no store>"


def build_client(creds: TestnetCredentials, inner: Any) -> Tuple[Any, TestnetTransport]:
    """(BybitClient, TestnetTransport) for the dedicated credentials over ``inner`` (the real ``NoRedirectInner`` or, in tests, the in-process fake). The client carries the
    TESTNET_EXECUTOR capability so ``_request`` checks the role before anything is signed."""
    if not isinstance(creds, TestnetCredentials):
        raise TestnetTransportRefused("CREDENTIALS_NOT_DEDICATED")
    transport = TestnetTransport(inner)
    client = bc.BybitClient(config=_Config(creds), store=_NullStore(), transport=transport)
    if client.venue != "testnet" or not client.testnet:
        raise TestnetTransportRefused("CLIENT_NOT_TESTNET")
    client.capability = transport.capability
    return client, transport


#: ``Derivatives.DerivativesTrade`` is how a UNIFIED account's key reports Bybit's "Unified Trading - Trade" permission (seen on the first real Testnet preflight, 2026-11-09). It is a TRADE
#: permission of the same class as ContractTrade, not a transfer, withdrawal, convert or history right; nothing else under ``Derivatives`` is accepted.
_ALLOWED_PERMS = {"ContractTrade": {"Order", "Position"}, "Spot": {"SpotTrade"}, "Derivatives": {"DerivativesTrade"}}
_DANGER_WORDS = ("withdraw", "transfer", "deposit", "submember", "subaccount", "api")


def key_permission_findings(result: Any) -> Tuple[List[str], Dict[str, Any]]:
    """(refusal codes, safe summary) for a ``/v5/user/query-api`` result. Least privilege: only ContractTrade[Order, Position], Derivatives[DerivativesTrade] (a unified account's trade
    permission) and Spot[SpotTrade] may be granted; any Wallet /
    transfer / withdraw permission, any other non-empty category, a read-only key (cannot trade) or an unreadable shape is refused. The summary never contains the key id or key."""
    if not isinstance(result, Mapping):
        return ["KEY_INFO_UNREADABLE"], {}
    bad: List[str] = []
    ro = result.get("readOnly")
    if ro not in (0, 1) or isinstance(ro, bool):
        bad.append("KEY_READONLY_UNREADABLE")
    elif ro == 1:
        bad.append("KEY_IS_READ_ONLY")
    perms = result.get("permissions")
    granted: List[str] = []
    if not isinstance(perms, Mapping):
        bad.append("KEY_PERMISSIONS_UNREADABLE")
    else:
        for cat, lst in perms.items():
            if not isinstance(lst, list) or any(not isinstance(x, str) for x in lst):
                bad.append(f"KEY_PERMISSIONS_UNREADABLE:{str(cat)[:20]}")
                continue
            for p in lst:
                granted.append(f"{cat}.{p}")
                low = f"{cat}.{p}".lower()
                if any(w in low for w in _DANGER_WORDS) or str(cat) == "Wallet":
                    bad.append(f"KEY_HAS_TRANSFER_OR_WITHDRAW:{cat}.{p}"[:80])
                elif p not in _ALLOWED_PERMS.get(str(cat), set()):
                    bad.append(f"KEY_HAS_EXTRA_PERMISSION:{cat}.{p}"[:80])
    ips = result.get("ips")
    return list(dict.fromkeys(bad)), {"read_only": ro, "granted": sorted(granted), "ip_restricted": bool(ips) and ips != ["*"] if isinstance(ips, list) else None}


def check_api_key(client: Any) -> Tuple[bool, str, Dict[str, Any]]:
    """Read ``/v5/user/query-api`` (testnet-only signed read) and judge it. A key the TESTNET host accepts is a testnet key (a mainnet key is rejected there)."""
    try:
        payload = client._request("GET", "/v5/user/query-api", signed=True, retries=1)
    except bc.BybitAPIError as exc:
        return False, f"KEY_CHECK_FAILED:retCode={exc.ret_code}:{type(exc).__name__}", {}
    result = (payload or {}).get("result")
    bad, summary = key_permission_findings(result)
    # WHICH account these credentials belong to, as the VENUE states it (``userID``): only its hash leaves this function (the capital book is bound to it; the raw id and the key never are). None when the
    # venue did not state one: the capital book then refuses, it never guesses.
    summary["account_ref"] = AN.account_ref_of("testnet", result.get("userID")) if isinstance(result, Mapping) else None
    summary["key_id_account_ref"] = AN.account_ref_of_key("testnet", result.get("id")) if isinstance(result, Mapping) else None      # the owner-chosen FALLBACK identity (see ms_capital_anchor); a hash, never the id
    return (not bad), ("ok" if not bad else ",".join(bad)), summary
