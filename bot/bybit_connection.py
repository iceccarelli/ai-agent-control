"""bybit_connection.py — one Bybit V5 client, spot-correct.

WHAT REPLACED WHAT
==================
The as-received module (14,570 lines, retained at
``_dead/bybit_connection_legacy.py``) contained **six** client stacks, four
instrument-filter caches that nothing ever wrote, and **21** mutually
incompatible order-id generators. Its two "live" stacks were both broken before
the wire:

* ``BybitConnection.create_order_v5`` died on ``self.get_filters(...)`` — a
  method the class does not have — and, past that, on
  ``from decimal import _dec`` (an unconditional ``ImportError``). Both were
  swallowed into ``{"retCode": -1}``.
* ``BybitConnection._make_v5_request`` returned ``_v5_request``'s ``(json, meta)``
  **tuple** while ~25 call sites did ``resp.get(...)``, and fifteen POST sites
  passed the body positionally as ``params`` — which the transport only appends
  to the URL for GET. Every POST was therefore signed over ``"{}"`` and sent
  with no parameters. The bot could not cancel or amend anything.

VERIFIED AGAINST THE BYBIT V5 DOCS (2026-07)
--------------------------------------------
Three legacy assumptions were checked against the vendor documentation and are
wrong:

1. ``POST /v5/position/trading-stop`` supports **linear, inverse, option — not
   spot**. The legacy code posted spot stop-losses there, inside ``except:
   pass``, at twelve call sites. Every position was downside-naked with no log
   evidence. Spot protective stops are placed here as **conditional orders**
   (``orderFilter="StopOrder"`` with ``triggerPrice``/``triggerDirection``).
2. The TP/SL mode parameter is spelled ``tpslMode``, not ``tpSlMode``. The
   legacy trailing-stop payload used the latter, so the field was ignored.
3. Private WebSocket auth signs ``"GET/realtime" + expires`` where ``expires``
   is an epoch-millisecond timestamp in the near future. The legacy code signed
   ``f"{ts}{api_key}"`` with ``perf_counter()*1000`` (≈ seconds since process
   start), so auth could never succeed and fill events never arrived.

Sources:
  https://bybit-exchange.github.io/docs/v5/order/create-order
  https://bybit-exchange.github.io/docs/v5/position/trading-stop
  https://bybit-exchange.github.io/docs/v5/ws/connect
  https://bybit-exchange.github.io/docs/v5/enum

THE RULES THIS MODULE ENFORCES
------------------------------
1. **No fabricated exchange data.** Instrument filters come from
   ``/v5/market/instruments-info`` or the call fails. There is no offline seed.
2. **Idempotent orders.** ``orderLinkId`` is derived from a *persisted* counter
   plus a hash of the order intent, so a retry after a timeout — including
   after a restart — reuses the same id and cannot double-fill.
3. **Epoch milliseconds everywhere.** ``perf_counter()`` is used only for
   measuring elapsed time, never as a clock.
4. **Balance checks reject.** No ``float('inf')``, no forced ``isLeverage=1``.
5. **A sub-minimum sell is skipped, never upgraded to "sell everything".**
6. **Retries only on genuinely transient codes**, and always with the same
   ``orderLinkId``.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import math
import threading
import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

import config as _config
import market_data as _market_data
from persistence import StateStore
from position_sizing import InstrumentFilters, snap_price, snap_qty_down
from risk_management import normalize_side

logger = logging.getLogger("bybit")

__all__ = [
    "BybitClient",
    "BillionaireBybitEngine",
    "BybitConnection",
    "BybitAPIError",
    "TransientAPIError",
    "PermanentAPIError",
    "OrderResult",
    "Transport",
    "RequestsTransport",
    "build_order_link_id",
    "TRANSIENT_RET_CODES",
    "TERMINAL_ORDER_STATUS",
    "OPEN_ORDER_STATUS",
    "MAINNET_REST",
    "TESTNET_REST",
    "DEMO_REST",
    "VENUE_REST",
    "MAINNET_WS_PRIVATE",
    "TESTNET_WS_PRIVATE",
]

MAINNET_REST = "https://api.bybit.com"
TESTNET_REST = "https://api-testnet.bybit.com"
#: Bybit DEMO TRADING (0046). A separate module from testnet: it runs against
#: REAL mainnet market data with simulated matching, and its wallet is funded
#: by `POST /v5/account/demo-apply-money` rather than a 24-hour web faucet.
#: Real marks, real basis, real funding prints, no real money.
DEMO_REST = "https://api-demo.bybit.com"

#: The only mapping from a venue name to a URL. One table, so there is exactly
#: one answer to "which exchange am I pointed at".
VENUE_REST = {"mainnet": MAINNET_REST, "testnet": TESTNET_REST,
              "demo": DEMO_REST}
MAINNET_WS_PRIVATE = "wss://stream.bybit.com/v5/private"
TESTNET_WS_PRIVATE = "wss://stream-testnet.bybit.com/v5/private"
DEMO_WS_PRIVATE = "wss://stream-demo.bybit.com/v5/private"
VENUE_WS_PRIVATE = {"mainnet": MAINNET_WS_PRIVATE, "testnet": TESTNET_WS_PRIVATE,
                    "demo": DEMO_WS_PRIVATE}

#: The one hostname each venue may be reached on. The request path checks the
#: URL it is ACTUALLY about to send against this, so a base_url that was
#: reassigned, subclassed or monkey-patched after construction cannot carry a
#: testnet-labelled client to the production host (or the reverse).
VENUE_HOSTS = {"mainnet": "api.bybit.com", "testnet": "api-testnet.bybit.com",
               "demo": "api-demo.bybit.com"}

#: Endpoints that can open or enlarge exposure. On mainnet these leave the
#: process only when live trading is authorised in the config the client was
#: built from - checked at the one transport every caller shares, including
#: `carry_broker`, which calls `_request` directly and so never passes through
#: `_submit`. Cancels and protective stops are deliberately NOT here: refusing
#: to cancel an order on an unarmed mainnet client would be the unsafe choice.
EXPOSURE_ENDPOINTS = frozenset({"/v5/order/create", "/v5/position/set-leverage"})


def resolve_client_venue(cfg: Any) -> str:
    """Which venue this config means, or a refusal. Never a guess.

    * ``BYBIT_VENUE`` (when stated) must be a known venue, and ``USE_TESTNET``
      (when stated) must agree with it: a duck-typed or hand-built config does
      not pass through ``config.load``, which is where the same contradiction
      is refused for the real environment.
    * With no venue, ``USE_TESTNET`` alone decides, and only a stated
      boolean counts: absent, ``None`` and the empty string are refused rather
      than read as "not testnet", which is how an unset flag becomes mainnet.
    """
    import config as _config

    raw = getattr(cfg, "BYBIT_VENUE", None)
    stated = getattr(cfg, "USE_TESTNET", None)
    sandbox: Optional[bool] = None
    if stated is not None and str(stated).strip() != "":
        try:
            sandbox = _config.parse_safety_bool(stated, "USE_TESTNET", True)
        except _config.ConfigError as exc:
            raise ValueError(f"USE_TESTNET is unreadable ({exc}); refusing "
                             "rather than guessing the venue") from exc
    if raw is None or str(raw).strip() == "":
        if sandbox is None:
            raise ValueError(
                "neither BYBIT_VENUE nor USE_TESTNET is stated; refusing "
                "rather than choosing an exchange")
        return "testnet" if sandbox else "mainnet"
    venue = str(raw).strip().lower()
    if venue not in VENUE_REST:
        raise ValueError(
            f"BYBIT_VENUE={raw!r} is not one of {sorted(VENUE_REST)}; "
            "refusing rather than guessing which exchange to authenticate "
            "against")
    if sandbox is not None and sandbox != (venue != "mainnet"):
        raise ValueError(
            f"BYBIT_VENUE={venue!r} and USE_TESTNET={stated!r} disagree about "
            "whether this is a sandbox; refusing")
    return venue

#: Bybit retCodes that are worth retrying with the SAME orderLinkId.
#:
#: Deliberately short. The legacy transport retried 110001 ("order does not
#: exist") and 110003 ("insufficient balance") five times with backoff — both are
#: permanent conditions, so the retries were pure latency. Four separate error
#: classifiers existed elsewhere in the file with mutually contradictory tables,
#: and one of them (`classify_retcode`) had the mapping inverted: it called
#: 10003 "invalid API key" retryable and rate-limit 10006 a validation error.
TRANSIENT_RET_CODES = frozenset({
    10002,   # request not supported at this time / server busy
    10006,   # rate limit exceeded
    10016,   # server error, please try again later
    10018,   # request ip rate limit
    130150,  # system busy
})

#: retCodes after which an order's fate is UNKNOWN, not refused: 0 is "no
#: retCode was stated" (PermanentAPIError's default), 10000 a server timeout,
#: 170007 a timeout waiting for the backend.
UNKNOWN_OUTCOME_RET_CODES = frozenset({0, 10000, 170007})

#: HTTP statuses worth retrying.
TRANSIENT_HTTP = frozenset({408, 429, 500, 502, 503, 504,
                            520, 521, 522, 523, 524})

#: Verified against https://bybit-exchange.github.io/docs/v5/enum
#: retCodes Bybit returns when an orderLinkId was already used. Seen on a
#: RETRY after a transport timeout whose first attempt actually landed. The
#: rejection means "the order exists", not "the order failed", and must be
#: resolved by querying the id, never by recording a rejection.
#:   110072 - linear/inverse "OrderLinkedID is duplicate"
#:   170130 - spot "Duplicate orderLinkId" (also what tests/fake_bybit.py emits)
#:   170141 - spot "Duplicate clientOrderId" (seen on a real testnet rejection;
#:             Bybit's own V5 docs use "clientOrderId" and "orderLinkId"
#:             interchangeably for this field, so this is the same condition
#:             as 170130 under a different code/wording)
#: The message is matched as well because the code table is not under our
#: control; a miss here degrades to the previous behaviour (row 'rejected').
DUPLICATE_LINK_ID_RET_CODES = frozenset({110072, 170130, 170141})

TERMINAL_ORDER_STATUS = frozenset({
    "Filled", "Cancelled", "Rejected", "PartiallyFilledCanceled", "Deactivated",
})
OPEN_ORDER_STATUS = frozenset({"New", "PartiallyFilled", "Untriggered", "Triggered"})

#: The two product categories this client implements.
#:
#: They are NOT interchangeable, and pretending otherwise is what makes a bot
#: send orders a venue silently ignores. The differences that actually reach
#: the wire:
#:
#: =========================  ==========================  ==========================
#: concern                    spot                        linear (USDT perpetual)
#: =========================  ==========================  ==========================
#: short a symbol             impossible without margin   native
#: qty step field             ``basePrecision``           ``qtyStep``
#: min notional field         ``minOrderAmt``             ``minNotionalValue``
#: market-order qty unit      ``marketUnit="baseCoin"``   base units implicitly
#: leverage flag              ``isLeverage``              ``positionIdx`` + set-leverage
#: protective stop            conditional ``StopOrder``   ``/v5/position/trading-stop``
#: funding                    none                        charged every 8h
#: what "balance" means       the asset you are selling   USDT initial margin
#: =========================  ==========================  ==========================
#:
#: ``/v5/position/trading-stop`` is valid for linear/inverse/option and **not**
#: for spot; the legacy code posted spot stops there at twelve call sites and
#: never placed a single stop. The dispatch below is the whole reason this
#: constant exists rather than a bare string.
SUPPORTED_CATEGORIES = ("spot", "linear")


# ---------------------------------------------------------------------------
# errors
# ---------------------------------------------------------------------------


class BybitAPIError(RuntimeError):
    """Base for exchange errors. Carries the retCode so callers can classify."""

    def __init__(self, message: str, ret_code: int = 0, payload: Any = None) -> None:
        super().__init__(message)
        self.ret_code = int(ret_code)
        self.payload = payload


class TransientAPIError(BybitAPIError):
    """Worth retrying with the same orderLinkId."""


class PermanentAPIError(BybitAPIError):
    """Never retry. Retrying an insufficient-balance error just wastes time."""


# ---------------------------------------------------------------------------
# transport
# ---------------------------------------------------------------------------


class Transport:
    """HTTP seam. Tests inject a fake; production injects :class:`RequestsTransport`.

    Keeping this abstract is what makes the whole client testable offline. The
    legacy module reached for ``requests`` from inside twenty different methods.
    """

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        params: Optional[Mapping[str, Any]] = None,
        body: Optional[str] = None,
        timeout: float = 10.0,
    ) -> Tuple[int, str]:
        raise NotImplementedError


class RequestsTransport(Transport):
    """Real HTTP via ``requests``, with a pooled session."""

    def __init__(self) -> None:
        import requests  # imported lazily so the module works without network deps

        self._session = requests.Session()
        adapter = requests.adapters.HTTPAdapter(
            pool_connections=8, pool_maxsize=16, max_retries=0
        )
        self._session.mount("https://", adapter)

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        params: Optional[Mapping[str, Any]] = None,
        body: Optional[str] = None,
        timeout: float = 10.0,
    ) -> Tuple[int, str]:
        response = self._session.request(
            method.upper(), url, headers=dict(headers), params=params,
            data=body, timeout=timeout,
        )
        #: The headers of the LAST response (rate-limit reset stamp lives here).
        self.last_headers = {k.lower(): v for k, v in response.headers.items()}
        return response.status_code, response.text


# ---------------------------------------------------------------------------
# results
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OrderResult:
    """Outcome of an order submission.

    ``ok`` defaults to **False**. The legacy code returned ``{"retCode": 0,
    "success": False, "skipped": True}`` from several paths — a shape that reads
    as success to any caller checking ``retCode``, for an order that was never
    sent. An exit reported that way is a position the bot believes it closed.
    """

    ok: bool = False
    order_id: str = ""
    order_link_id: str = ""
    reason: str = "NOT_SUBMITTED"
    ret_code: int = 0
    raw: Dict[str, Any] = field(default_factory=dict)

    def __bool__(self) -> bool:
        return self.ok


# ---------------------------------------------------------------------------
# idempotency
# ---------------------------------------------------------------------------


def build_order_link_id(
    *,
    seq: int,
    symbol: str,
    side: str,
    qty: str,
    purpose: str = "entry",
    price: str = "",
    session_token: str = "",
    prefix: str = "BB",
) -> str:
    """Deterministic, restart-safe ``orderLinkId`` (<= 36 chars).

    The id is a function of a **persisted** sequence number, the order intent,
    and ``session_token`` — a random value minted once per database file (see
    ``StateStore.session_boot_token``). Same intent + same sequence number +
    same session token => same id, in this process or the next one. That is
    what makes a retry after a timed-out submit safe: the exchange rejects the
    duplicate id instead of opening a second position.

    ``session_token`` is what keeps that safe-retry property from becoming an
    accidental cross-session collision: ``seq`` only counts up *within* one
    database file, so a fresh file (new checkout, wiped state dir, new
    container) restarts it at 1. Without a session-distinguishing input, two
    unrelated sessions submitting the same intent (same symbol/side/qty/price/
    purpose at seq=1) would mint the exact same id and the second one would be
    rejected by the exchange as a duplicate of the first (Bybit retCode
    170141) even though they are unrelated orders. The token is not a random
    *retry* — it is fixed for the lifetime of one database file, so retries
    within a session are unaffected; only a genuinely new session changes it.

    The legacy module had 21 generators. Among them: ``perf_counter()//30``
    buckets (reset to 0 on restart), ``uuid4``, ``secrets.token_hex``, salted
    ``hash()`` (different every process), and one using
    ``time.strftime(gmtime(perf_counter()))``, which always produced
    ``"19700101"``. None survived a restart.
    """
    seed = f"{seq}|{symbol}|{side}|{qty}|{price}|{purpose}|{session_token}"
    digest = hashlib.blake2b(seed.encode("utf-8"), digest_size=12).hexdigest()
    oid = f"{prefix}-{purpose[:4]}-{seq}-{digest}"
    if len(oid) > 36:
        oid = oid[:36]
    return oid


# ---------------------------------------------------------------------------
# the client
# ---------------------------------------------------------------------------


class BybitClient:
    """One Bybit V5 spot client.

    All state that must survive a restart lives in :class:`persistence.StateStore`;
    this object holds only caches and connection handles.
    """

    def __init__(
        self,
        config: Any = None,
        store: Optional[StateStore] = None,
        transport: Optional[Transport] = None,
        recv_window_ms: Optional[int] = None,
    ) -> None:
        self.cfg = config if config is not None else _config.get_config_object()
        self.store = store if store is not None else StateStore()
        self.transport = transport if transport is not None else RequestsTransport()

        #: How many order-create requests have actually left this process.
        #:
        #: Counted at the transport, not inferred from the decision journal: the
        #: journal records what was *decided*, and the only honest answer to
        #: "did this paper session submit an order?" is what crossed the wire.
        #: A stand-down run (`ENTRIES_ENABLED=0`) must end with this at 0, and
        #: `session_log.PaperSessionLog` reports it verbatim.
        self.orders_submitted = 0
        self._entry_gate: Optional[Callable[[], Tuple[bool, str]]] = None

        # The venue is resolved ONCE, centrally, and refused when the config
        # contradicts itself or states nothing (resolve_client_venue). USE_TESTNET
        # is then the DERIVED sandbox flag every safety gate already reads.
        venue = resolve_client_venue(self.cfg)
        self.testnet = venue != "mainnet"
        self.venue = venue
        self.base_url = VENUE_REST[venue]
        # By venue, like REST: demo credentials are only valid on the demo
        # stream, which is not the testnet one.
        self.ws_private_url = VENUE_WS_PRIVATE[venue]
        #: "spot" or "linear". THE fork this client is built around — see the
        #: class docstring. Read from config once, here, so no call site can
        #: disagree with another about which product it is trading.
        self.category = str(getattr(self.cfg, "CATEGORY", "spot")).lower()
        if self.category not in SUPPORTED_CATEGORIES:
            raise PermanentAPIError(
                f"category {self.category!r} is not implemented "
                f"(supported: {', '.join(SUPPORTED_CATEGORIES)})"
            )
        self.is_spot = self.category == "spot"
        self.is_linear = self.category == "linear"
        #: Spot has no native short. Derived, never configured.
        self.allow_shorts = self.is_linear
        self.leverage = int(getattr(self.cfg, "DEFAULT_LEVERAGE", 1) or 1)
        self.api_key = str(self.cfg.BYBIT_API_KEY or "")
        self.api_secret = str(self.cfg.BYBIT_API_SECRET or "")
        self.recv_window_ms = int(
            recv_window_ms if recv_window_ms is not None
            else getattr(self.cfg, "BYBIT_RECV_WINDOW_MS", 5000)
        )
        self.timeout_s = float(getattr(self.cfg, "REQUEST_TIMEOUT_SECONDS", 10.0))
        #: Spot margin is OFF unless configured. The legacy code hardcoded
        #: `margin_enabled = True`, discarding the env read two lines above, so
        #: every order carried isLeverage=1 and oversized buys auto-borrowed.
        self.use_leverage = bool(getattr(self.cfg, "USE_LEVERAGE", False))

        self._lock = threading.RLock()
        self._filters: Dict[str, Tuple[float, InstrumentFilters]] = {}
        self._book_warned: set = set()
        #: True for a live feed: a book that cannot be read is then reported by
        #: ``book_feed_unavailable`` so the liquidity gate can refuse, rather
        #: than read as "no book feed". The backtester clears it (a replay
        #: without a recorded book has no feed to be unavailable).
        self.book_feed_required: bool = True
        self._book_failed: Dict[str, bool] = {}
        self._filters_ttl_s = float(getattr(self.cfg, "SYMBOL_FILTERS_TTL", 3600))
        #: Difference between exchange time and local time, in ms.
        self._time_skew_ms = 0

    # -- clock -------------------------------------------------------------

    def _assert_endpoint_matches_venue(self, url: str) -> None:
        """The URL about to be sent must be https on this venue's one host.

        Checked against the string the transport will receive, not against the
        label the client was built with.
        """
        from urllib.parse import urlsplit
        parts = urlsplit(url)
        if parts.scheme != "https" or parts.hostname != VENUE_HOSTS.get(
                self.venue):
            raise PermanentAPIError(
                f"endpoint host {parts.hostname!r} is not the {self.venue} "
                f"host {VENUE_HOSTS.get(self.venue)!r}; nothing was sent")

    def describe(self) -> Dict[str, Any]:
        """What this client is pointed at, with no secret in it.

        Credentials are reported as present or absent; neither the key, the
        secret, a signature nor a credential-bearing URL appears here.
        """
        from urllib.parse import urlsplit
        import config as _config
        armed, _why = _config.is_live_authorized(self.cfg)
        return {"venue": self.venue, "host": urlsplit(self.base_url).hostname,
                "sandbox": self.testnet, "category": self.category,
                "credentials_present": bool(self.api_key and self.api_secret),
                "live_authorized": armed is True,
                "orders_submitted": self.orders_submitted}

    def __repr__(self) -> str:
        return (f"<BybitClient venue={self.venue} host="
                f"{VENUE_HOSTS.get(self.venue)} credentials="
                f"{'present' if self.api_key else 'absent'}>")

    def _now_ms(self) -> int:
        """Epoch milliseconds, skew-corrected.

        ``time.time()``, never ``perf_counter()``. The legacy module used
        ``perf_counter()`` as a clock in at least eight places — request signing,
        data-freshness checks, order-id buckets and server-time fallbacks — which
        made every freshness check pass (it compared an epoch timestamp against
        seconds-since-process-start) and every signature invalid.
        """
        return int(time.time() * 1000) + self._time_skew_ms

    def sync_time(self) -> int:
        """Measure clock skew against the exchange. Returns the skew in ms."""
        self._assert_endpoint_matches_venue(f"{self.base_url}/v5/market/time")
        try:
            status, text = self.transport.request(
                "GET", f"{self.base_url}/v5/market/time", headers={},
                timeout=self.timeout_s,
            )
        except Exception as exc:  # noqa: BLE001 - network layer
            # Never let a raw transport exception escape the client. Callers
            # handle BybitAPIError; a bare ConnectionError propagating out of
            # here skipped `startup()`'s guard and crashed the process instead
            # of refusing to trade.
            raise TransientAPIError(f"time sync transport error: {exc}") from exc
        if status != 200:
            raise TransientAPIError(f"time sync failed: HTTP {status}")
        try:
            payload = json.loads(text)
        except Exception as exc:  # noqa: BLE001
            raise PermanentAPIError(f"unparseable time response: {exc}") from exc
        nano = payload.get("time") or payload.get("result", {}).get("timeNano")
        if nano is None:
            second = payload.get("result", {}).get("timeSecond")
            if second is None:
                raise PermanentAPIError("time response has no timestamp")
            server_ms = int(second) * 1000
        else:
            server_ms = int(int(nano) // 1_000_000) if int(nano) > 10**14 else int(nano)
        self._time_skew_ms = server_ms - int(time.time() * 1000)
        logger.info("clock skew vs exchange: %d ms", self._time_skew_ms)
        return self._time_skew_ms

    # -- signing -----------------------------------------------------------

    def _sign(self, timestamp: str, payload: str) -> str:
        """HMAC-SHA256 over ``timestamp + apiKey + recvWindow + payload``.

        For GET, ``payload`` is the exact query string that will be sent. For
        POST it is the exact JSON body bytes. "Exact" is load-bearing: signing a
        re-serialised copy of the body produces a valid-looking signature for a
        different string, which the exchange rejects with a confusing error.
        """
        if not self.api_secret:
            raise PermanentAPIError("cannot sign: no API secret configured")
        pre = f"{timestamp}{self.api_key}{self.recv_window_ms}{payload}"
        return hmac.new(
            self.api_secret.encode("utf-8"), pre.encode("utf-8"), hashlib.sha256
        ).hexdigest()

    @staticmethod
    def _query_string(params: Mapping[str, Any]) -> str:
        """Deterministic query string. Order must match what is sent."""
        return "&".join(
            f"{k}={v}" for k, v in params.items() if v is not None and v != ""
        )

    def _request(
        self,
        method: str,
        endpoint: str,
        *,
        params: Optional[Mapping[str, Any]] = None,
        body: Optional[Mapping[str, Any]] = None,
        signed: bool = False,
        retries: int = 3,
    ) -> Dict[str, Any]:
        """One transport for every call. Returns the WHOLE parsed v5 envelope
        on ``retCode == 0`` - ``{"retCode", "retMsg", "result": {...}, ...}`` -
        never just the ``result`` object. Every caller must unwrap
        ``payload.get("result", {})`` itself (see ``get_ticker``, ``get_wallet``,
        ``get_open_orders`` below, and ``carry_broker.CarryBroker._v5_result``/
        ``_v5_list``, the one unwrap that module's readers share). A prior
        version of this docstring said "the parsed ``result`` envelope",
        which read as though this method already unwrapped it; it did not,
        and that exact mismatch was a live production bug in
        ``carry_broker.py`` (0037, 0038) before this line was corrected to
        match the code rather than the code being bent to match the words.

        Raises :class:`TransientAPIError` or :class:`PermanentAPIError`; never
        returns a sentinel dict. The legacy transport turned crashes into
        ``{"retCode": -1}``, which callers could not distinguish from a genuine
        API rejection.

        Note the keyword-only ``params``/``body``: the legacy signature allowed
        a body to be passed positionally into the ``params`` slot, which is how
        fifteen POST call sites ended up sending an empty body.
        """
        method = method.upper()
        url = f"{self.base_url}{endpoint}"
        self._assert_endpoint_matches_venue(url)
        if self.venue == "mainnet" and endpoint in EXPOSURE_ENDPOINTS:
            import config as _config
            armed, why = _config.is_live_authorized(self.cfg)
            if armed is not True:
                logger.critical("%s REFUSED at the transport: mainnet without "
                                "live authorisation (%s)", endpoint, why)
                raise PermanentAPIError(
                    f"LIVE_NOT_AUTHORIZED: {endpoint} on mainnet ({why})")
        # Count order submissions at the one place every call passes through.
        # Incremented before the attempt, not after a success: an order whose
        # response was lost still left the process, and a session log that
        # under-counts those is the one that would lie.
        if endpoint == "/v5/order/create":
            self.orders_submitted += 1
        if (method == "GET" and endpoint in self.COOLDOWN_READ_ENDPOINTS
                and time.monotonic() < self._read_cooldown_until.get(endpoint, 0.0)):
            # After a 403 (Bybit's IP-ban response) the READS stand down for a
            # short while instead of hammering a banned address. Writes are
            # never failed fast here: a refused write must still take the
            # normal unknown-outcome path, and an order that was never sent
            # must not be recorded as one that may have been.
            raise PermanentAPIError(
                "HTTP 403 cooldown: reads paused after an auth/ban response",
                payload="")
        attempt = 0
        last_error: Optional[BybitAPIError] = None

        while attempt <= retries:
            attempt += 1
            timestamp = str(self._now_ms())
            headers: Dict[str, str] = {"Content-Type": "application/json"}
            query = self._query_string(params or {})
            # The body is serialised ONCE and both signed and sent verbatim.
            body_str = json.dumps(body, separators=(",", ":")) if body else ""

            if signed:
                payload = query if method == "GET" else body_str
                headers.update({
                    "X-BAPI-API-KEY": self.api_key,
                    "X-BAPI-TIMESTAMP": timestamp,
                    "X-BAPI-RECV-WINDOW": str(self.recv_window_ms),
                    "X-BAPI-SIGN": self._sign(timestamp, payload),
                    "X-BAPI-SIGN-TYPE": "2",
                })

            try:
                status, text = self.transport.request(
                    method,
                    url + (f"?{query}" if (method == "GET" and query) else ""),
                    headers=headers,
                    body=body_str if method != "GET" else None,
                    timeout=self.timeout_s,
                )
            except Exception as exc:  # noqa: BLE001 - network layer
                last_error = TransientAPIError(f"transport error: {exc}")
                self._backoff(attempt)
                continue

            if status in TRANSIENT_HTTP:
                last_error = TransientAPIError(f"HTTP {status}", payload=text)
                if status == 429:
                    self._rate_limit_backoff(attempt)
                else:
                    self._backoff(attempt)
                continue
            if status == 401 or status == 403:
                if (status == 403 and method == "GET"
                        and endpoint in self.COOLDOWN_READ_ENDPOINTS):
                    # Per ENDPOINT, and only for a refused READ: a 403 on a
                    # write must not block the reads that resolve its
                    # outcome, and one read path being refused must not
                    # silence the safety reads (position, order, wallet).
                    self._read_cooldown_until[endpoint] = (
                        time.monotonic() + self.READ_COOLDOWN_AFTER_403_S)
                raise PermanentAPIError(f"auth rejected: HTTP {status}", payload=text)
            if status != 200:
                raise PermanentAPIError(f"HTTP {status}", payload=text)

            try:
                payload_json = json.loads(text)
            except Exception as exc:  # noqa: BLE001
                # Garbage in a 200 is not a reason to trade on nothing.
                raise PermanentAPIError(f"unparseable response: {exc}", payload=text)

            # A 200 whose body does not state a retCode is not an answer from
            # the venue's matching engine (an upstream or proxy body): whether
            # the request executed is unknown, exactly as for an unparseable
            # body. A made-up retCode (-1) used to read as a definite refusal.
            try:
                ret_code = int(payload_json["retCode"])
            except (TypeError, KeyError, ValueError):
                raise PermanentAPIError(
                    "response carries no usable retCode", payload=text)
            ret_msg = str(payload_json.get("retMsg", ""))
            if ret_code == 0:
                return payload_json

            if ret_code == 10002:
                # Timestamp outside recv_window: resync and retry once more.
                try:
                    self.sync_time()
                except Exception:  # noqa: BLE001
                    pass
            if ret_code in TRANSIENT_RET_CODES:
                last_error = TransientAPIError(ret_msg, ret_code, payload_json)
                if ret_code in (10006, 10018):
                    self._rate_limit_backoff(attempt)
                else:
                    self._backoff(attempt)
                continue

            raise PermanentAPIError(ret_msg, ret_code, payload_json)

        assert last_error is not None
        raise last_error

    RATE_LIMIT_MAX_WAIT_S = 5.0
    READ_COOLDOWN_AFTER_403_S = 30.0
    #: The ONLY reads a 403 pauses: market-data and cost reads that merely
    #: inform ENTRIES. Everything else (position, order, execution, closed-pnl,
    #: wallet, instrument filters, tickers) is never failed locally: a refused
    #: read-back of a just-placed stop is read as "not live" and would flatten
    #: the position and trip the kill switch, where a direct request might
    #: simply have succeeded. An allowlist, so a new endpoint defaults safe.
    COOLDOWN_READ_ENDPOINTS = frozenset({
        "/v5/market/kline", "/v5/market/orderbook",
        "/v5/market/funding/history", "/v5/account/fee-rate"})

    @property
    def _read_cooldown_until(self) -> Dict[str, float]:
        return self.__dict__.setdefault("_read_cooldowns", {})

    def _rate_limit_backoff(self, attempt: int) -> None:
        """Wait until the venue says the window resets (capped), not a guess.

        Bybit reports the reset instant in `X-Bapi-Limit-Reset-Timestamp`
        (epoch ms). Retrying inside the window just spends the retries.
        """
        wait = min(2.0, 0.15 * (2 ** (attempt - 1)))
        try:
            headers = getattr(self.transport, "last_headers", None) or {}
            reset = headers.get("x-bapi-limit-reset-timestamp")
            if reset is not None:
                until = (float(reset) - self._now_ms()) / 1000.0
                if until == until and until > wait:
                    wait = min(until + 0.05, self.RATE_LIMIT_MAX_WAIT_S)
        except Exception:  # noqa: BLE001
            pass
        time.sleep(wait)

    @staticmethod
    def _backoff(attempt: int) -> None:
        """Exponential backoff. Actually sleeps.

        The legacy ``nb_sleep`` helper had a 213-line function accidentally
        indented inside its body, so the function returned before ever sleeping.
        Every backoff loop in the codebase was a hot spin.
        """
        time.sleep(min(2.0, 0.15 * (2 ** (attempt - 1))))

    # -- market data -------------------------------------------------------

    def get_instrument_filters(
        self, symbol: str, *, force: bool = False
    ) -> InstrumentFilters:
        """Real instrument filters, TTL-cached. Raises if the exchange won't say.

        Audit C14: the legacy ``get_filters`` returned a hardcoded
        ``{tickSize: 0.01, stepSize: 0.001, minNotional: 5.0}`` for **every**
        symbol, read from a cache that no code path ever wrote. Four such caches
        existed; none had a writer. Real values differ per symbol — ADA steps by
        0.1, some symbols tick below a cent — so orders were mis-sized or
        rejected, including exits.

        For ``category=spot`` the quantity step is ``basePrecision`` (not
        ``qtyStep``, which is linear/inverse) and the minimum notional is
        ``minOrderAmt``.
        """
        now = time.monotonic()
        with self._lock:
            cached = self._filters.get(symbol)
            if cached and not force and (now - cached[0]) < self._filters_ttl_s:
                return cached[1]

        payload = self._request(
            "GET", "/v5/market/instruments-info",
            params={"category": self.category, "symbol": symbol},
        )
        rows = payload.get("result", {}).get("list") or []
        if not rows:
            raise PermanentAPIError(f"no instrument info for {symbol}")

        row = rows[0]
        lot = row.get("lotSizeFilter") or {}
        price_filter = row.get("priceFilter") or {}
        merged = {
            "tickSize": price_filter.get("tickSize"),
            # Spot: basePrecision. Linear/inverse: qtyStep. Accept either so the
            # same client can be pointed at a futures category later, but never
            # invent one.
            "qtyStep": lot.get("basePrecision") or lot.get("qtyStep"),
            "minOrderQty": lot.get("minOrderQty"),
            "minOrderAmt": lot.get("minOrderAmt") or lot.get("minNotionalValue"),
        }
        filters = InstrumentFilters.parse(symbol, merged, from_exchange=True)
        with self._lock:
            self._filters[symbol] = (now, filters)
        logger.info(
            "filters %s tick=%s step=%s minQty=%s minAmt=%s",
            symbol, filters.tick_size, filters.qty_step,
            filters.min_qty, filters.min_notional,
        )
        return filters

    def get_ticker(self, symbol: str) -> Dict[str, Any]:
        payload = self._request(
            "GET", "/v5/market/tickers",
            params={"category": self.category, "symbol": symbol},
        )
        rows = payload.get("result", {}).get("list") or []
        if not rows:
            raise PermanentAPIError(f"no ticker for {symbol}")
        return rows[0]

    def get_quote(self, symbol: str) -> Dict[str, Any]:
        """Best bid/ask with a measured age, for the spread and freshness gates.

        Returns ``{"bid", "ask", "mid", "spread_bps", "age_seconds"}``.

        ``age_seconds`` is derived from the exchange's own timestamp against our
        skew-corrected clock, so it measures data staleness rather than local
        latency. The legacy freshness check compared an epoch timestamp against
        ``perf_counter()`` and therefore always reported "fresh" — including
        during an outage.
        """
        payload = self._request(
            "GET", "/v5/market/tickers",
            params={"category": self.category, "symbol": symbol},
        )
        rows = payload.get("result", {}).get("list") or []
        if not rows:
            raise PermanentAPIError(f"no ticker for {symbol}")
        row = rows[0]

        bid = float(row.get("bid1Price") or 0.0)
        ask = float(row.get("ask1Price") or 0.0)
        if bid <= 0.0 or ask <= 0.0:
            # Some spot tickers omit level-1 quotes; fall back to last price but
            # say so, rather than fabricating a spread of zero.
            last = float(row.get("lastPrice") or 0.0)
            if last <= 0.0:
                raise PermanentAPIError(f"no usable quote for {symbol}")
            return {
                "bid": last, "ask": last, "mid": last,
                "spread_bps": 0.0, "age_seconds": None, "degraded": True,
            }

        server_ms = int(payload.get("time") or 0)
        age = max(0.0, (self._now_ms() - server_ms) / 1000.0) if server_ms else None
        mid = (bid + ask) / 2.0
        return {
            "bid": bid, "ask": ask, "mid": mid,
            "spread_bps": (ask - bid) / mid * 10_000.0,
            "age_seconds": age, "degraded": False,
        }

    def get_last_price(self, symbol: str) -> float:
        """Last traded price. Raises rather than returning a fallback.

        The legacy path returned ``1.0`` on failure, which made a BTCUSDT
        position size come out roughly 50,000x too large.
        """
        price = float(self.get_ticker(symbol).get("lastPrice") or 0.0)
        if price <= 0.0:
            raise PermanentAPIError(f"non-positive last price for {symbol}")
        return price

    def get_klines(
        self, symbol: str, interval: str = "60", limit: int = 200
    ) -> List[List[str]]:
        """Klines, oldest-first, **closed candles only**.

        Bybit returns newest-first and includes the in-progress candle. Dropping
        it here means no downstream indicator can repaint (audit C19): the whole
        codebase sees the same closed-candle view.
        """
        payload = self._request(
            "GET", "/v5/market/kline",
            params={
                "category": self.category, "symbol": symbol,
                "interval": interval, "limit": int(limit),
            },
        )
        rows = payload.get("result", {}).get("list") or []
        rows = list(reversed(rows))       # oldest-first
        return rows[:-1] if rows else []  # drop the unclosed candle

    # -- account -----------------------------------------------------------

    def get_wallet(self, account_type: str = "UNIFIED") -> Dict[str, Any]:
        payload = self._request(
            "GET", "/v5/account/wallet-balance",
            params={"accountType": account_type}, signed=True,
        )
        rows = payload.get("result", {}).get("list") or []
        if not rows:
            raise PermanentAPIError("wallet-balance returned no accounts")
        return rows[0]

    def get_equity(self, account_type: str = "UNIFIED") -> float:
        """Total account EQUITY in USD.

        Equity, not available balance: opening a position reduces available
        balance, so a drawdown measured from it rises the instant you enter a
        trade. Raises rather than returning a phantom $10,000.
        """
        wallet = self.get_wallet(account_type)
        for key in ("totalEquity", "totalWalletBalance"):
            raw = wallet.get(key)
            if raw not in (None, ""):
                value = float(raw)
                if value >= 0.0:
                    return value
        raise PermanentAPIError("wallet-balance has no usable equity field")

    def get_coin_balance(self, coin: str, account_type: str = "UNIFIED",
                         total: bool = False) -> float:
        """Free coin by default; ``total=True`` is the whole wallet balance
        INCLUDING what resting orders reserve (take-profit legs reserve the
        coin they offer, so 'is the position still held?' must ask for total)."""
        wallet = self.get_wallet(account_type)
        keys = (("walletBalance",) if total
                else ("availableToWithdraw", "free", "walletBalance"))
        for entry in wallet.get("coin") or []:
            if str(entry.get("coin", "")).upper() == coin.upper():
                if total and entry.get("walletBalance") in (None, ""):
                    # Total was asked for and the row does not state it:
                    # returning 0.0 (or the free amount) would be a lie.
                    raise PermanentAPIError(
                        f"wallet row for {coin} has no walletBalance")
                for key in keys:
                    raw = entry.get(key)
                    if raw not in (None, ""):
                        return float(raw)
        return 0.0

    def get_open_orders(
        self, symbol: Optional[str] = None, *, order_filter: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Open orders for `symbol` (or the whole category if omitted).

        `order_filter` is optional and defaults to omitted, which is exactly
        the previous behaviour of this method (every existing caller keeps
        its original contract). Passing one of `_SPOT_CANCEL_ALL_ORDER_
        FILTERS` ("Order", "StopOrder", "tpslOrder") narrows the read the
        same way it narrows `cancel_all` — see that constant's docstring for
        why a single unfiltered spot read does not already cover all three.
        This is a read (`GET /v5/order/realtime`), never a mutation.
        """
        params: Dict[str, Any] = {"category": self.category}
        if symbol:
            params["symbol"] = symbol
        elif self.is_linear:
            # settleCoin is a linear/inverse filter used to ask for "everything
            # settled in USDT". Spot has no settle currency and rejects it.
            params["settleCoin"] = "USDT"
        if order_filter:
            params["orderFilter"] = order_filter
        payload = self._request(
            "GET", "/v5/order/realtime", params=params, signed=True
        )
        return payload.get("result", {}).get("list") or []

    def get_order(
        self, *, symbol: str, order_link_id: str = "", order_id: str = "",
        strict: bool = False,
    ) -> Optional[Dict[str, Any]]:
        """Query a single order, open or recently closed.

        Checks the realtime endpoint first, then history. The legacy
        ``get_order`` searched only *open* orders, so a filled market order was
        always a 404 — which is why ``place_and_confirm_order`` could never
        confirm and re-placed the same order up to three times.
        """
        if not (order_link_id or order_id):
            raise ValueError("need order_link_id or order_id")
        params: Dict[str, Any] = {"category": self.category, "symbol": symbol}
        if order_link_id:
            params["orderLinkId"] = order_link_id
        if order_id:
            params["orderId"] = order_id

        unanswered: Optional[PermanentAPIError] = None
        for endpoint in ("/v5/order/realtime", "/v5/order/history"):
            try:
                payload = self._request("GET", endpoint, params=params, signed=True)
            except PermanentAPIError as exc:
                if exc.ret_code in UNKNOWN_OUTCOME_RET_CODES:
                    unanswered = exc
                continue
            rows = payload.get("result", {}).get("list") or []
            if rows:
                return rows[0]
        if strict and unanswered is not None:
            # `None` means the venue ANSWERED and has no such order. A lookup
            # that got no usable answer (a proxy body, a timeout, an auth
            # failure) says nothing about whether the order exists.
            raise unanswered
        return None

    # -- orders ------------------------------------------------------------

    def _next_order_link_id(
        self, *, symbol: str, side: str, qty: str, purpose: str, price: str = ""
    ) -> str:
        seq = self.store.next_order_seq()
        return build_order_link_id(
            seq=seq, symbol=symbol, side=side, qty=qty,
            session_token=self.store.session_boot_token(),
            purpose=purpose, price=price,
            prefix=str(getattr(self.cfg, "ORDERLINK_PREFIX", "BB")),
        )

    @property
    def entry_gate(self) -> Optional[Callable[[], Tuple[bool, str]]]:
        """Asked before any order that can ADD exposure; never for an exit.
        `build_bot` wires it to the store's kill switch. None = no extra gate
        (tools that build their own client)."""
        return self._entry_gate

    @entry_gate.setter
    def entry_gate(self, gate: Optional[Callable[[], Tuple[bool, str]]]) -> None:
        if gate is not None and not callable(gate):
            raise TypeError("entry_gate must be callable -> (may_send, reason)")
        self._entry_gate = gate

    def _entry_refusal(self, direction: str, reduce_only: bool) -> Optional[str]:
        """A reason string when this order may not go, else None.

        Exits are never asked: a reduce-only order, and on spot a SELL (spot
        has no short, so a sell can only reduce a held long)."""
        if self._entry_gate is None or reduce_only:
            return None
        if self.is_spot and direction == "Sell":
            return None
        try:
            allowed, reason = self._entry_gate()
        except Exception as exc:  # noqa: BLE001 - an unreadable gate is closed
            return f"ENTRY_GATE_REFUSED: gate unreadable ({exc!r})"
        if allowed is not True:
            return f"ENTRY_GATE_REFUSED: {reason}"
        return None

    def place_order(
        self,
        *,
        symbol: str,
        side: str,
        qty: Decimal | float | str,
        order_type: str = "Market",
        price: Optional[Decimal | float | str] = None,
        time_in_force: Optional[str] = None,
        purpose: str = "entry",
        order_link_id: Optional[str] = None,
        reduce_only: bool = False,
        filters: Optional[InstrumentFilters] = None,
    ) -> OrderResult:
        """Submit one spot order. Idempotent, quantised, balance-checked.

        Keyword-only throughout: the legacy positional signatures are how a body
        ended up in the ``params`` slot.
        """
        direction = normalize_side(side)
        if direction is None:
            return OrderResult(reason=f"UNKNOWN_SIDE:{side!r}")
        refused = self._entry_refusal(direction, reduce_only)
        if refused is not None:
            return OrderResult(reason=refused)

        try:
            filters = filters or self.get_instrument_filters(symbol)
        except BybitAPIError as exc:
            # No filters => cannot quantise => cannot submit a legal order.
            # Guessing here is audit C14.
            return OrderResult(reason=f"NO_FILTERS: {exc}")

        snapped_qty = snap_qty_down(float(qty), filters.qty_step)
        if snapped_qty <= 0:
            return OrderResult(reason="QTY_BELOW_STEP")
        if snapped_qty < filters.min_qty:
            # Skip. The legacy code's alternative was to sell the entire holding.
            return OrderResult(reason="QTY_BELOW_MIN")

        price_str = ""
        if order_type.lower() == "limit":
            if price is None:
                return OrderResult(reason="LIMIT_WITHOUT_PRICE")
            price_str = str(snap_price(float(price), filters.tick_size,
                                       side=direction))

        reference_price = float(price_str) if price_str else None
        if reference_price is None:
            try:
                reference_price = self.get_last_price(symbol)
            except BybitAPIError as exc:
                return OrderResult(reason=f"NO_PRICE: {exc}")

        notional = Decimal(str(snapped_qty)) * Decimal(str(reference_price))
        if notional < filters.min_notional:
            return OrderResult(reason="BELOW_MIN_NOTIONAL")

        # -- a short on spot is not a trade, it is a mistake -----------------
        # Spot has no borrow in this codebase, so a Sell can only reduce an
        # existing long. Rejecting it HERE, by name, is better than letting it
        # reach the balance check and come back as "INSUFFICIENT_BTC" — which
        # is technically true and tells the operator nothing.
        if direction == "Sell" and self.is_spot and not reduce_only:
            held = 0.0
            try:
                held = self.get_coin_balance(self._base_asset(symbol))
            except BybitAPIError:
                return OrderResult(reason="BALANCE_UNREADABLE")
            if held + 1e-12 < float(snapped_qty):
                return OrderResult(
                    reason="SHORT_NOT_AVAILABLE_ON_SPOT: selling "
                           f"{float(snapped_qty):.8f} with {held:.8f} held. "
                           "Spot cannot short; set CATEGORY=linear for that."
                )

        # -- balance check that actually rejects ---------------------------
        blocked = self._check_balance(
            symbol=symbol, side=direction, qty=snapped_qty,
            reference_price=reference_price, reduce_only=reduce_only,
        )
        if blocked is not None:
            return OrderResult(reason=blocked)

        qty_str = format(snapped_qty.normalize(), "f")
        oid = order_link_id or self._next_order_link_id(
            symbol=symbol, side=direction, qty=qty_str,
            purpose=purpose, price=price_str,
        )

        # Record the intent BEFORE submitting. If we crash between here and the
        # exchange acknowledging, the restart reconciler finds this row and
        # queries the exchange for that exact id instead of re-sending blind.
        is_new = self.store.record_order(
            oid, symbol, direction, order_type, float(snapped_qty),
            float(reference_price), purpose=purpose, status="pending",
        )
        if not is_new:
            existing = self.store.get_order(oid)
            logger.warning("orderLinkId %s already submitted (%s); not resending",
                           oid, (existing or {}).get("status"))
            return OrderResult(
                ok=False, order_link_id=oid, reason="DUPLICATE_INTENT",
                raw=existing or {},
            )

        body: Dict[str, Any] = {
            "category": self.category,
            "symbol": symbol,
            "side": direction,
            "orderType": order_type,
            "qty": qty_str,
            "orderLinkId": oid,
        }
        body.update(self._category_order_fields(order_type))
        if price_str:
            body["price"] = price_str
        if time_in_force:
            body["timeInForce"] = time_in_force
        # `reduceOnly` is a POSITION concept — valid for linear/inverse/
        # option, where it lets the venue itself refuse an order that
        # would flip or grow a position instead of shrinking it. Spot has
        # no position at all (see `get_position`'s own docstring: "a
        # spot 'position' is a coin balance"), so this field is never
        # sent there — internal `reduce_only` still selects the intent
        # (skip the balance check above; the spot-short guard already
        # ran), it just never becomes a REST body field on spot.
        if reduce_only and not self.is_spot:
            body["reduceOnly"] = True

        return self._submit(body, oid, purpose)

    def _category_order_fields(self, order_type: str) -> Dict[str, Any]:
        """The per-category fields every order body needs. Defined once.

        Spot and linear disagree about how to say "this quantity is in base
        units" and about how leverage is expressed. Scattering that ``if`` over
        four order-building methods is how one of them ends up missing it, and a
        spot market BUY without ``marketUnit`` is interpreted as a *quote*
        amount — off by the price, which for BTC is a factor of ~100,000.
        """
        if self.is_spot:
            fields: Dict[str, Any] = {
                # Margin only when configured. Never forced on. The legacy code
                # hardcoded margin_enabled = True, so every order auto-borrowed.
                "isLeverage": 1 if self.use_leverage else 0,
            }
            if order_type.lower() == "market":
                fields["marketUnit"] = "baseCoin"
            return fields
        # Linear, one-way mode. positionIdx 0 == one-way; 1/2 are the hedge-mode
        # long/short slots. This client does not implement hedge mode, so it
        # states one-way explicitly rather than relying on the account default —
        # an account left in hedge mode would otherwise reject every order.
        return {"positionIdx": 0}

    def _submit(self, body: Dict[str, Any], oid: str, purpose: str) -> OrderResult:
        """Send an order, mapping every outcome onto a truthful OrderResult."""
        if self.venue == "mainnet":
            # Defence in depth at the transport: the startup gate and the
            # per-order gate of the carry book are not the only things between
            # a caller and the real exchange. A mainnet order leaves this
            # process only when live trading is authorised in the config this
            # client was built from.
            import config as _config
            armed, why = _config.is_live_authorized(self.cfg)
            if armed is not True:
                self.store.update_order_status(oid, "rejected")
                logger.critical("order %s REFUSED at the transport: mainnet "
                                "without live authorisation (%s)", oid, why)
                return OrderResult(order_link_id=oid,
                                   reason="LIVE_NOT_AUTHORIZED")
        try:
            payload = self._request(
                "POST", "/v5/order/create", body=body, signed=True
            )
        except PermanentAPIError as exc:
            if self._looks_like_duplicate_link_id(exc):
                # Attempt N-1 landed and the transport lost the reply. The
                # exchange is the truth; ask it before writing anything.
                recovered = self._recover_landed_order(
                    symbol=str(body.get("symbol", "")), oid=oid, purpose=purpose)
                if recovered is not None:
                    return recovered
            if exc.ret_code in UNKNOWN_OUTCOME_RET_CODES:
                # No retCode at all (an unparseable or retCode-less 200, a
                # non-200) or a timeout the venue itself reported: the order
                # may have executed. Recording it "rejected" takes it out of the
                # unresolved set startup reconciliation checks against the
                # venue. Same rule as the carry broker's classifier.
                self.store.update_order_status(oid, "unknown")
                logger.error("order %s outcome unknown (%s): %s", oid,
                             exc.ret_code, exc)
                return OrderResult(
                    order_link_id=oid, reason="OUTCOME_UNKNOWN",
                    ret_code=exc.ret_code)
            self.store.update_order_status(oid, "rejected")
            logger.error("order %s rejected (%s): %s", oid, exc.ret_code, exc)
            # The id is returned even on failure: the caller needs it to look the
            # intent up, and a reconciler cannot chase an id it was never told.
            return OrderResult(
                order_link_id=oid, reason=f"REJECTED:{exc.ret_code}",
                ret_code=exc.ret_code,
            )
        except TransientAPIError as exc:
            # Retries are exhausted and we do NOT know whether it landed.
            # Leave the intent 'pending' so reconciliation resolves it against
            # the exchange; never resubmit blind.
            self.store.update_order_status(oid, "unknown")
            logger.error("order %s outcome unknown after retries: %s", oid, exc)
            return OrderResult(
                order_link_id=oid, reason="OUTCOME_UNKNOWN", ret_code=exc.ret_code
            )

        result = payload.get("result", {}) or {}
        order_id = str(result.get("orderId", ""))
        self.store.update_order_status(oid, "submitted", exchange_id=order_id)
        logger.info("order %s submitted (%s) %s", oid, order_id, purpose)
        return OrderResult(
            ok=True, order_id=order_id, order_link_id=oid,
            reason="SUBMITTED", raw=result,
        )

    @staticmethod
    def _looks_like_duplicate_link_id(exc: PermanentAPIError) -> bool:
        if exc.ret_code in DUPLICATE_LINK_ID_RET_CODES:
            return True
        msg = str(exc).lower().replace(" ", "")
        if "duplicate" not in msg:
            return False
        return "orderlink" in msg or "clientorder" in msg

    def _recover_landed_order(self, *, symbol: str, oid: str,
                              purpose: str) -> Optional[OrderResult]:
        """After a duplicate-id rejection, record what the exchange holds.

        Returns an OrderResult mirroring a normal submit when the order is
        found. Returns ``None`` when it cannot be found or queried, in which
        case the caller falls back to 'rejected' - the exchange said the id
        exists, so a lookup miss is itself suspicious and is logged at error.
        Never resubmits.
        """
        try:
            remote = self.get_order(symbol=symbol, order_link_id=oid)
        except BybitAPIError as exc:
            logger.error("order %s: duplicate-id rejection but lookup failed: %s",
                         oid, exc)
            self.store.update_order_status(oid, "unknown")
            return OrderResult(order_link_id=oid, reason="OUTCOME_UNKNOWN",
                               ret_code=getattr(exc, "ret_code", None))
        if remote is None:
            logger.error("order %s: exchange reports duplicate id but returns "
                         "no order; leaving 'unknown' for reconciliation", oid)
            self.store.update_order_status(oid, "unknown")
            return OrderResult(order_link_id=oid, reason="OUTCOME_UNKNOWN")
        order_id = str(remote.get("orderId", ""))
        status = str(remote.get("orderStatus", ""))
        local = "filled" if status == "Filled" else (
            status.lower() if status in TERMINAL_ORDER_STATUS else "submitted")
        self.store.update_order_status(oid, local, exchange_id=order_id)
        logger.warning("order %s had already landed (%s, %s) %s - recorded from "
                       "the exchange, not resubmitted", oid, order_id, status,
                       purpose)
        return OrderResult(ok=True, order_id=order_id, order_link_id=oid,
                           reason="SUBMITTED", raw=remote)

    def _check_balance(
        self, *, symbol: str, side: str, qty: Decimal, reference_price: float,
        reduce_only: bool = False,
    ) -> Optional[str]:
        """Return a block reason, or None if the balance is sufficient.

        Audit C16: the legacy check set ``usdt_balance = float('inf')`` in both
        branches of a try/except, so the comparison against required margin was
        permanently False and the rejection path was unreachable.

        The question is a different one per category. On **spot** it is "do I
        hold the asset I am about to give away" — quote currency for a Buy, base
        for a Sell. On **linear** neither side gives anything away: both post
        *initial margin* in USDT, so a short is checked exactly like a long.
        Applying the spot rule to a perp would reject every short for lack of a
        coin balance that perps do not use.
        """
        if reduce_only:
            # Closing never requires new funds, and on linear it releases them.
            # Blocking an exit on a balance check is how a position becomes
            # unclosable at the worst moment.
            return None
        try:
            if self.is_linear:
                leverage = max(1, self.leverage)
                required = (float(qty) * reference_price) / leverage
                available = self.get_coin_balance("USDT")
                if available < required:
                    return (
                        f"INSUFFICIENT_MARGIN: have {available:.8f} USDT, "
                        f"need {required:.8f} at {leverage}x"
                    )
                return None
            if side == "Buy":
                quote = self._quote_asset(symbol)
                available = self.get_coin_balance(quote)
                required = float(qty) * reference_price
                if available < required:
                    return (
                        f"INSUFFICIENT_{quote}: have {available:.8f}, "
                        f"need {required:.8f}"
                    )
            else:
                base = self._base_asset(symbol)
                available = self.get_coin_balance(base)
                if available < float(qty):
                    return (
                        f"INSUFFICIENT_{base}: have {available:.8f}, "
                        f"need {float(qty):.8f}"
                    )
        except BybitAPIError as exc:
            # Cannot read the balance => cannot prove the order is funded.
            return f"BALANCE_UNREADABLE: {exc}"
        return None

    @staticmethod
    def _quote_asset(symbol: str) -> str:
        for quote in ("USDT", "USDC", "USD", "EUR", "BTC", "ETH"):
            if symbol.upper().endswith(quote):
                return quote
        raise PermanentAPIError(f"cannot determine quote asset for {symbol}")

    @classmethod
    def _base_asset(cls, symbol: str) -> str:
        quote = cls._quote_asset(symbol)
        return symbol.upper()[: -len(quote)]

    def cancel_order(
        self, *, symbol: str, order_link_id: str = "", order_id: str = ""
    ) -> bool:
        """Cancel one order. Returns True only on exchange confirmation.

        This is one of the calls that could never work in the legacy module: the
        body was passed positionally into the ``params`` slot, so the request was
        signed over ``"{}"`` and sent with no parameters.
        """
        if not (order_link_id or order_id):
            raise ValueError("need order_link_id or order_id")
        body: Dict[str, Any] = {"category": self.category, "symbol": symbol}
        if order_link_id:
            body["orderLinkId"] = order_link_id
        if order_id:
            body["orderId"] = order_id
        try:
            self._request("POST", "/v5/order/cancel", body=body, signed=True)
        except PermanentAPIError as exc:
            if exc.ret_code in (110001, 170213):  # already gone
                self._mark_cancelled(order_link_id or order_id)
                return True
            logger.error("cancel failed for %s: %s", order_link_id or order_id, exc)
            return False
        except TransientAPIError as exc:
            logger.error("cancel unresolved for %s: %s", order_link_id or order_id, exc)
            return False
        if order_link_id:
            self._mark_cancelled(order_link_id)
        return True

    def _mark_cancelled(self, order_link_id: str) -> None:
        """Record a cancel locally -- except for an ENTRY, whose true outcome
        (it may have executed before the cancel landed, and "order not found"
        also means "already filled") only the venue's own record can say. An
        entry row is left for the fill path / resolver / reconcile to settle
        from that record, after anything it executed has been put in the book."""
        row = self.store.get_order(order_link_id) if order_link_id else None
        if row is not None and str(row.get("purpose")) == "entry":
            return
        self.store.update_order_status(order_link_id, "cancelled")

    #: On SPOT, `/v5/order/cancel-all`'s `orderFilter` accepts exactly one
    #: of these per call and DEFAULTS TO "Order" ALONE when omitted —
    #: verified against
    #: https://bybit-exchange.github.io/docs/api-explorer/v5/trade/cancel-all.
    #: A single unfiltered call therefore never touches a resting
    #: `StopOrder` (the mechanism `place_stop_order` uses for a spot
    #: protective stop — see that method's own docstring) or a
    #: `tpslOrder`. Calling it once per filter is the only way to
    #: actually clear everything; there is no combined/"all filters" value
    #: to pass instead. Linear/inverse have no such per-filter narrowing
    #: (a protective stop there is a POSITION field, set by
    #: `_place_position_stop`, not a separate cancellable order at all),
    #: so a single unfiltered call already covers everything there.
    _SPOT_CANCEL_ALL_ORDER_FILTERS = ("Order", "StopOrder", "tpslOrder")

    def cancel_all(self, symbol: Optional[str] = None) -> bool:
        """Cancel every resting order for `symbol` (or the whole category
        if omitted). See `_SPOT_CANCEL_ALL_ORDER_FILTERS` above for why
        this is more than one request on spot.
        """
        order_filters: Tuple[Optional[str], ...] = (
            self._SPOT_CANCEL_ALL_ORDER_FILTERS if self.is_spot else (None,))
        ok = True
        for order_filter in order_filters:
            body: Dict[str, Any] = {"category": self.category}
            if symbol:
                body["symbol"] = symbol
            if order_filter:
                body["orderFilter"] = order_filter
            try:
                self._request("POST", "/v5/order/cancel-all", body=body, signed=True)
            except BybitAPIError as exc:
                logger.error("cancel-all(orderFilter=%s) failed: %s",
                            order_filter, exc)
                ok = False
        return ok

    # -- spot-valid protective stop ---------------------------------------

    def place_stop_order(
        self,
        *,
        symbol: str,
        side: str,
        qty: Decimal | float | str,
        trigger_price: float,
        filters: Optional[InstrumentFilters] = None,
        order_link_id: Optional[str] = None,
    ) -> OrderResult:
        """Place a protective stop for SPOT as a conditional order.

        **This is the correction the audit's C11 describes.** The legacy code
        posted spot stops to ``POST /v5/position/trading-stop`` at twelve call
        sites, every one wrapped in ``except: pass``. That endpoint supports
        ``linear``, ``inverse`` and ``option`` — **not** ``spot`` — so no stop
        was ever placed and no error was ever logged. Verified against
        https://bybit-exchange.github.io/docs/v5/position/trading-stop

        The spot mechanism is a conditional order: ``orderFilter="StopOrder"``
        with a ``triggerPrice`` and a ``triggerDirection``.

        ``triggerDirection``: 1 = trigger when the last price RISES to
        triggerPrice, 2 = when it FALLS. A long's protective stop is a Sell that
        fires on a fall, so direction 2.

        **On linear this dispatches to a different mechanism entirely** —
        ``/v5/position/trading-stop`` — because that endpoint is valid there and
        attaches the stop to the *position* rather than leaving it as a separate
        order that can be orphaned. See :meth:`_place_position_stop`.
        """
        direction = normalize_side(side)
        if direction is None:
            return OrderResult(reason=f"UNKNOWN_SIDE:{side!r}")

        if self.is_linear:
            return self._place_position_stop(
                symbol=symbol, exit_side=direction, trigger_price=trigger_price,
                filters=filters,
            )

        try:
            filters = filters or self.get_instrument_filters(symbol)
        except BybitAPIError as exc:
            return OrderResult(reason=f"NO_FILTERS: {exc}")

        snapped_qty = snap_qty_down(float(qty), filters.qty_step)
        if snapped_qty <= 0 or snapped_qty < filters.min_qty:
            return OrderResult(reason="STOP_QTY_BELOW_MIN")

        # snap_price speaks in POSITION sides; this order is the exit.
        position_side = "Buy" if direction == "Sell" else "Sell"
        trigger = snap_price(
            float(trigger_price), filters.tick_size, side=position_side,
            is_stop=True
        )
        # A Sell stop protects a long and fires on a fall.
        trigger_direction = 2 if direction == "Sell" else 1

        qty_str = format(snapped_qty.normalize(), "f")
        oid = order_link_id or self._next_order_link_id(
            symbol=symbol, side=direction, qty=qty_str, purpose="stop",
            price=str(trigger),
        )
        if not self.store.record_order(
            oid, symbol, direction, "Market", float(snapped_qty), float(trigger),
            purpose="stop", status="pending",
        ):
            return OrderResult(order_link_id=oid, reason="DUPLICATE_INTENT")

        body = {
            "category": self.category,
            "symbol": symbol,
            "side": direction,
            "orderType": "Market",
            "qty": qty_str,
            "orderLinkId": oid,
            "orderFilter": "StopOrder",
            "triggerPrice": str(trigger),
            "triggerDirection": trigger_direction,
            "isLeverage": 1 if self.use_leverage else 0,
            "marketUnit": "baseCoin",
        }
        return self._submit(body, oid, "stop")

    def _place_position_stop(
        self,
        *,
        symbol: str,
        exit_side: str,
        trigger_price: float,
        filters: Optional[InstrumentFilters] = None,
    ) -> OrderResult:
        """The LINEAR protective stop: attached to the position itself.

        Verified against https://bybit-exchange.github.io/docs/v5/position/trading-stop —
        valid for ``linear``, ``inverse`` and ``option``. **Not** for spot; the
        spot path above exists precisely because this one is unavailable there.

        Why this rather than a conditional reduce-only order, which linear also
        supports: a position-attached stop cannot be orphaned. It resizes with
        the position automatically, which removes an entire class of bug — the
        one where a partial take-profit leaves a stop still sized for the
        original quantity, so it is rejected at the moment it is needed. That
        defect was found and fixed on the spot path; on linear it cannot occur.

        The parameter is ``tpslMode``, not ``tpSlMode``. Bybit ignores unknown
        parameters silently, so the wrong casing produces a 200 OK with no stop
        set — which is exactly how the legacy code believed it was protected.
        """
        try:
            filters = filters or self.get_instrument_filters(symbol)
        except BybitAPIError as exc:
            return OrderResult(reason=f"NO_FILTERS: {exc}")

        # A Sell exit protects a long, so its stop sits BELOW; snap away from
        # the market on the conservative side.
        # snap_price speaks in POSITION sides; this order is the exit. A long's
        # stop (Sell exit) rounds UP, toward entry: never a wider stop than the
        # one the size was budgeted for.
        position_side = "Buy" if normalize_side(exit_side) == "Sell" else "Sell"
        trigger = snap_price(
            float(trigger_price), filters.tick_size, side=position_side,
            is_stop=True
        )
        if float(trigger) <= 0:
            return OrderResult(reason="STOP_PRICE_NON_POSITIVE")

        oid = self._next_order_link_id(
            symbol=symbol, side=exit_side, qty="position",
            purpose="stop", price=str(trigger),
        )
        if not self.store.record_order(
            oid, symbol, exit_side, "Market", 0.0, float(trigger),
            purpose="stop", status="pending",
        ):
            return OrderResult(order_link_id=oid, reason="DUPLICATE_INTENT")

        body = {
            "category": self.category,
            "symbol": symbol,
            "stopLoss": str(trigger),
            # "Full" == the stop covers the entire position and tracks its size.
            "tpslMode": "Full",
            "slTriggerBy": "LastPrice",
            "slOrderType": "Market",
            "positionIdx": 0,
        }
        try:
            self._request("POST", "/v5/position/trading-stop", body=body, signed=True)
        except PermanentAPIError as exc:
            if exc.ret_code in UNKNOWN_OUTCOME_RET_CODES:
                # No venue retCode: the request may have landed. "rejected"
                # would take the row out of what reconciliation checks.
                self.store.update_order_status(oid, "unknown")
                logger.error("position stop outcome unknown for %s: %s",
                             symbol, exc)
                return OrderResult(order_link_id=oid, reason="OUTCOME_UNKNOWN")
            self.store.update_order_status(oid, "rejected")
            logger.error("position stop rejected for %s: %s", symbol, exc)
            return OrderResult(
                order_link_id=oid, reason=f"REJECTED:{exc.ret_code}",
                ret_code=exc.ret_code,
            )
        except TransientAPIError as exc:
            self.store.update_order_status(oid, "unknown")
            logger.error("position stop unresolved for %s: %s", symbol, exc)
            return OrderResult(order_link_id=oid, reason="OUTCOME_UNKNOWN")

        self.store.update_order_status(oid, "submitted")
        # Deliberately NOT reporting success on the 200 alone. The caller's next
        # step is verify_stop(), which reads the position back. A 200 from this
        # endpoint means "accepted", not "in force".
        return OrderResult(
            ok=True, order_link_id=oid, reason="SUBMITTED",
            raw={"stopLoss": str(trigger), "mechanism": "position"},
        )

    def clear_position_stop(self, *, symbol: str) -> OrderResult:
        """Cancel a LINEAR position's protective stop at the venue.

        The mirror of :meth:`_place_position_stop`: same endpoint, same
        ``tpslMode``, ``stopLoss="0"`` instead of a trigger price. Linear
        only — spot has no position-attached stop to clear.

        Nothing in the trading engine calls this to manage a live position;
        clearing a live stop on purpose is not something the safety model
        ever wants. It exists so an INDUCED-INCIDENT drill (Item 3 of
        ``linear_protective_stop_verified`` — a stop lost at the venue while
        a position is open) can simulate that loss through the one real
        client dialect, instead of a drill hand-rolling its own request.
        """
        if not self.is_linear:
            return OrderResult(reason="SPOT_HAS_NO_POSITION_STOP_TO_CLEAR")

        oid = self._next_order_link_id(
            symbol=symbol, side="Clear", qty="position",
            purpose="stop_clear", price="0",
        )
        body = {
            "category": self.category,
            "symbol": symbol,
            "stopLoss": "0",
            "tpslMode": "Full",
            "positionIdx": 0,
        }
        try:
            self._request("POST", "/v5/position/trading-stop", body=body, signed=True)
        except PermanentAPIError as exc:
            logger.error("clear position stop rejected for %s: %s", symbol, exc)
            return OrderResult(
                order_link_id=oid, reason=f"REJECTED:{exc.ret_code}",
                ret_code=exc.ret_code,
            )
        except TransientAPIError as exc:
            logger.error("clear position stop unresolved for %s: %s", symbol, exc)
            return OrderResult(order_link_id=oid, reason="OUTCOME_UNKNOWN")

        return OrderResult(
            ok=True, order_link_id=oid, reason="SUBMITTED",
            raw={"stopLoss": "0", "mechanism": "position"},
        )

    def verify_stop(
        self, *, symbol: str, order_link_id: str
    ) -> Tuple[bool, str]:
        """Read the protective stop back from the exchange. ``(live, detail)``.

        This is the single most important read in the codebase, and it is the
        one the legacy module never made. It dispatches by category because the
        two mechanisms are visible in different places: a spot conditional stop
        is an *order*, a linear stop is a *field on the position*.

        Fails closed: any exception, any missing record, any unexpected status
        is reported as "not live". A stop that cannot be proven is no stop.
        """
        try:
            if self.is_linear:
                position = self.get_position(symbol)
                if position is None:
                    return False, "NO_POSITION"
                raw = str(position.get("stopLoss", "") or "")
                if raw in ("", "0", "0.0"):
                    return False, "POSITION_HAS_NO_STOP_LOSS"
                return True, f"stopLoss={raw}"

            confirmed = self.get_order(symbol=symbol, order_link_id=order_link_id)
            if not confirmed:
                return False, "STOP_ORDER_NOT_VISIBLE"
            status = str(confirmed.get("orderStatus", ""))
            if status not in {"Untriggered", "New"}:
                return False, f"STOP_ORDER_NOT_LIVE:{status}"
            return True, f"orderStatus={status}"
        except BybitAPIError as exc:
            return False, f"STOP_UNVERIFIABLE: {exc}"

    def find_live_spot_stop(self, symbol: str) -> Optional[Dict[str, Any]]:
        """The already-live SPOT protective stop order for ``symbol``, if any.

        RECOVERY INVARIANT this exists to hold (crash boundary: after a
        protective stop is accepted at the venue, before this process
        durably records that fact): ``place_stop_order`` mints a FRESH
        ``orderLinkId`` from ``store.next_order_seq()`` on every call, so
        two calls are never deduplicated as "the same submission" the way
        ``place_order``'s (or a retried ``record_order``) idempotent-intent
        insert is for entries. On LINEAR that is harmless — the protective
        stop is a field on the position (``_place_position_stop``), and
        re-setting it just overwrites the same field. On SPOT it is a
        distinct standing conditional order every time, so calling
        ``place_stop_order`` again for a position that already has one
        resting at the venue creates a SECOND, independent live stop for
        the same quantity instead of recognizing the first.

        This is a read-only venue query (``GET /v5/order/realtime`` via
        ``get_open_orders``, ``orderFilter="StopOrder"``), never a
        mutation. Returns ``None`` on linear (that path never needs it —
        see above) and when spot genuinely has no live stop for the
        symbol, so a caller can tell "already protected, nothing to do"
        from "actually naked, must protect".
        """
        if self.is_linear:
            return None
        for row in self.get_open_orders(symbol, order_filter="StopOrder"):
            status = str(row.get("orderStatus", ""))
            if status in {"Untriggered", "New", "PartiallyFilled"}:
                return dict(row)
        return None

    # -- linear-only account surface ---------------------------------------

    def get_position(self, symbol: str) -> Optional[Dict[str, Any]]:
        """The exchange's view of an open position. ``None`` when flat.

        Linear only: spot has no position endpoint — holdings are coin balances,
        which is exactly why "a position" means something different there and
        why the two categories cannot share one code path.
        """
        if not self.is_linear:
            raise PermanentAPIError(
                "get_position is linear-only; on spot a position is a coin balance"
            )
        payload = self._request(
            "GET", "/v5/position/list",
            params={"category": self.category, "symbol": symbol}, signed=True,
        )
        # "flat" must be STATED: a reply with no `list` at all is an unreadable
        # reply, not an empty one (it used to read as flat and book a full
        # phantom exit over a live position).
        rows = (payload.get("result") or {}).get("list")
        if not isinstance(rows, list):
            raise PermanentAPIError("position list carries no list; not "
                                    "treating it as flat")
        for row in rows:
            if float(row.get("size", 0) or 0) > 0:
                return row
        return None

    def get_funding_history_fractions(
        self, symbol: str, limit: int = 200
    ) -> List[Tuple[int, float]]:
        """Settled funding prints as ``(epoch_ms, FRACTION)``, oldest first.

        THE UNIT IS IN THE NAME ON PURPOSE. `carry_broker.get_funding_history`
        returns the same endpoint in BASIS POINTS, and the rule this feeds
        compares against ``FUND_ABS = 0.0001``, a fraction. Mixing the two is a
        factor of 10,000 that does not raise, does not look wrong, and produces
        ZERO setups — which reads as a market finding rather than a unit bug.
        `tests/test_funding_carry_fade_v1.py` opens by recording that exact
        failure happening once already.

        Settled prints only: this is the same `/v5/market/funding/history` the
        carry book reads, and every row it returns has already paid.
        """
        # LINEAR, not self.category. Funding exists only on perpetuals, and
        # `/v5/market/funding/history` answers "Illegal category" for spot —
        # which a caller would experience as an exception, or worse, as the
        # shadow path standing aside forever because it catches provider
        # failures and treats them as "no setup". `carry_broker` hardcodes the
        # same constant for the same reason.
        payload = self._request(
            "GET", "/v5/market/funding/history",
            params={"category": "linear", "symbol": symbol,
                    "limit": max(1, min(int(limit), 200))},
        )
        rows = (payload.get("result", {}) or {}).get("list") or []
        out: List[Tuple[int, float]] = []
        for row in rows:
            try:
                rate = float(row["fundingRate"])          # already a FRACTION
                stamp = int(row["fundingRateTimestamp"])
            except (KeyError, TypeError, ValueError):
                continue
            if math.isfinite(rate) and stamp > 0:
                out.append((stamp, rate))
        out.sort(key=lambda pair: pair[0])
        # Strictly increasing: FundingSeries does a binary search and refuses a
        # series that is not, so a duplicated stamp must be dropped here rather
        # than raising deep inside the rule.
        deduped: List[Tuple[int, float]] = []
        for stamp, rate in out:
            if deduped and stamp <= deduped[-1][0]:
                continue
            deduped.append((stamp, rate))
        return deduped

    EXECUTION_PAGE_CAP = 20

    def get_executions(self, symbol: str, order_link_id: str = "", *,
                       order_id: str = "", start_ms: int = 0, end_ms: int = 0,
                       limit: int = 100) -> List[Dict[str, Any]]:
        """The venue's own execution rows (fills) of ONE order, every page.

        ``GET /v5/execution/list`` filtered by ``orderLinkId`` or ``orderId``
        (a venue-triggered stop/TP has no usable link id; the closed-pnl
        ``orderId`` is the join key). Rows carry ``execId``, ``orderId``,
        ``orderLinkId``, ``symbol``, ``side``, ``execPrice``, ``execQty``,
        ``execFee``, ``feeCurrency``, ``execType`` and ``execTime``. Follows
        ``nextPageCursor`` until it is exhausted and RAISES if the cap is hit
        or a cursor repeats: a truncated fill list would fail the exact
        quantity match anyway, and must never look complete. Read-only.
        """
        if not (order_link_id or order_id):
            raise PermanentAPIError(
                "get_executions needs an order link id or an order id")
        params: Dict[str, Any] = {"category": self.category, "symbol": symbol,
                                  "limit": int(limit)}
        if order_id:
            params["orderId"] = order_id
        else:
            params["orderLinkId"] = order_link_id
        if start_ms:
            params["startTime"] = int(start_ms)
        if end_ms:
            params["endTime"] = int(end_ms)
        out: List[Dict[str, Any]] = []
        seen_cursors: set = set()
        for _page in range(self.EXECUTION_PAGE_CAP):
            payload = self._request("GET", "/v5/execution/list", signed=True,
                                    params=dict(params))
            result = payload.get("result") or {}
            rows = result.get("list")
            if not isinstance(rows, list):
                raise PermanentAPIError("execution list carries no list")
            out.extend(rows)
            cursor = result.get("nextPageCursor")
            if not cursor:
                return out
            if cursor in seen_cursors:
                raise PermanentAPIError("execution list cursor repeated")
            seen_cursors.add(cursor)
            params["cursor"] = cursor
        raise PermanentAPIError("execution list did not finish within the "
                                "page cap; not treating it as complete")

    def get_closed_pnl(self, symbol: str, *, since_ms: int = 0,
                       limit: int = 50) -> List[Dict[str, Any]]:
        """Closed-PnL records for ``symbol`` newer than ``since_ms``. Linear only.

        https://bybit-exchange.github.io/docs/v5/position/close-pnl - each row
        carries ``orderId``, ``side`` (the CLOSING side), ``qty``,
        ``avgExitPrice``, ``closedPnl`` (fees already netted) and
        ``updatedTime`` (ms). Oldest first on return. Raises on any failure:
        an exit the bot cannot read is not an exit it may invent.
        """
        if not self.is_linear:
            raise PermanentAPIError("get_closed_pnl is linear-only")
        params: Dict[str, Any] = {"category": self.category, "symbol": symbol,
                                  "limit": int(limit)}
        if since_ms > 0:
            params["startTime"] = int(since_ms)
        payload = self._request("GET", "/v5/position/closed-pnl", params=params,
                                signed=True)
        rows = [r for r in (payload.get("result", {}).get("list") or [])
                if int(r.get("updatedTime", 0) or 0) > since_ms]
        rows.sort(key=lambda r: int(r.get("updatedTime", 0) or 0))
        return rows

    def get_book_features(self, symbol: str) -> Optional[Dict[str, Any]]:
        """Live L2 microstructure for the liquidity gate. ``None`` when unread.

        Verified against https://bybit-exchange.github.io/docs/v5/market/orderbook —
        ``result.b`` and ``result.a`` are ``[[price, size], ...]``, best first,
        and the limit is category-dependent (spot tops out at 200).

        The measurement itself is delegated to :mod:`market_data`, which is the
        same code the backtester uses. That is deliberate: a depth or imbalance
        figure computed one way in simulation and another way in production is a
        gate that cannot be trusted in either.

        Returns ``None`` rather than an empty or half-filled dict when the book
        cannot be read. The gate treats ``None`` as "no book supplied" and
        proceeds; it treats a dict with missing fields as a broken feed and
        blocks. Those are genuinely different situations and must not collapse
        into one.
        """
        self._book_failed[symbol] = False
        if not bool(getattr(self.cfg, "USE_ORDERBOOK", True)):
            return None
        self._book_failed[symbol] = True        # until a usable book is returned
        levels = int(getattr(self.cfg, "BOOK_DEPTH_LEVELS", 25))
        try:
            payload = self._request(
                "GET", "/v5/market/orderbook",
                params={"category": self.category, "symbol": symbol,
                        "limit": min(200, max(1, levels))},
            )
        except BybitAPIError as exc:
            # Warn ONCE per symbol, then fall to debug. An unavailable book is
            # worth an operator's attention the first time; repeating it every
            # loop iteration for the life of the process turns the log into
            # noise and buries the events that matter.
            if symbol not in self._book_warned:
                self._book_warned.add(symbol)
                logger.warning("orderbook unavailable for %s: %s "
                               "(further occurrences at debug level)", symbol, exc)
            else:
                logger.debug("orderbook unavailable for %s: %s", symbol, exc)
            return None

        result = payload.get("result", {}) or {}
        bids, asks = result.get("b") or [], result.get("a") or []
        if not bids and not asks:
            return None
        try:
            state = _market_data.BookState.from_ladders(
                bids, asks, symbol_id=symbol, strict=False
            )
            features = state.features(levels=levels, cfg=self.cfg)
        except Exception as exc:  # noqa: BLE001
            logger.warning("could not read the book for %s: %s", symbol, exc)
            return None

        self._book_failed[symbol] = False
        # Only the scalar measurements cross this boundary. The gate needs
        # numbers, not level ladders, and shipping the ladders would invite a
        # caller to re-derive a figure that has already been derived correctly.
        return {
            "best_bid": features.best_bid,
            "best_ask": features.best_ask,
            "mid": features.mid,
            "microprice": features.microprice,
            "spread_bps": features.spread_bps,
            "depth_bid_usd": features.depth_bid_usd,
            "depth_ask_usd": features.depth_ask_usd,
            "imbalance": features.imbalance,
            "usable": features.usable,
            "reason": features.reason,
            "anomalous_spread": features.anomalous_spread,
            "levels": features.levels,
        }

    def get_book_ladders(self, symbol: str) -> Optional[Tuple[list, list]]:
        """The raw ``(bids, asks)`` ladders for ``symbol``, or None when unread.

        What the model's feature code accepts (it measures the book with the
        same ``market_data`` code the backtester uses). ``get_book_features``
        returns only the scalar summary the liquidity gate needs, which that
        code cannot rebuild a book from.
        """
        if not bool(getattr(self.cfg, "USE_ORDERBOOK", True)):
            return None
        levels = int(getattr(self.cfg, "BOOK_DEPTH_LEVELS", 25))
        try:
            payload = self._request(
                "GET", "/v5/market/orderbook",
                params={"category": self.category, "symbol": symbol,
                        "limit": min(200, max(1, levels))},
            )
        except BybitAPIError as exc:
            logger.debug("orderbook unavailable for %s: %s", symbol, exc)
            return None
        result = payload.get("result", {}) or {}
        bids, asks = result.get("b") or [], result.get("a") or []
        if not bids and not asks:
            return None
        return list(bids), list(asks)

    def book_feed_unavailable(self, symbol: str) -> bool:
        """True when the last ``get_book_features`` for ``symbol`` was meant to
        read a book and could not (request failure, empty or unparseable book)."""
        return bool(self.book_feed_required and self._book_failed.get(symbol))

    def set_leverage(self, symbol: str, leverage: Optional[int] = None) -> bool:
        """Set leverage before trading a linear symbol.

        Bybit returns retCode 110043 when the leverage is already what you asked
        for. That is a success, not a failure — treating it as an error is how a
        startup sequence refuses to begin for a reason that is not a problem.
        """
        if not self.is_linear:
            return True
        value = int(leverage if leverage is not None else self.leverage)
        body = {
            "category": self.category, "symbol": symbol,
            "buyLeverage": str(value), "sellLeverage": str(value),
        }
        try:
            self._request("POST", "/v5/position/set-leverage", body=body, signed=True)
            return True
        except PermanentAPIError as exc:
            if exc.ret_code == 110043:      # leverage not modified
                return True
            logger.error("set-leverage failed for %s: %s", symbol, exc)
            return False
        except TransientAPIError as exc:
            logger.error("set-leverage unresolved for %s: %s", symbol, exc)
            return False

    def get_funding_rate(self, symbol: str) -> Optional[float]:
        """Current funding rate as a FRACTION per interval. ``None`` on spot.

        Returned as a fraction, not bps and not a percent, because every rate in
        this codebase is a fraction. ``None`` — never 0.0 — when it cannot be
        read: a funding rate that is genuinely zero and one that is unknown must
        not look the same to the gate that decides whether the carry is
        affordable.
        """
        if not self.is_linear:
            return None
        try:
            payload = self._request(
                "GET", "/v5/market/tickers",
                params={"category": self.category, "symbol": symbol},
            )
        except BybitAPIError:
            return None
        rows = payload.get("result", {}).get("list") or []
        if not rows:
            return None
        raw = rows[0].get("fundingRate")
        if raw in (None, ""):
            return None
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return None
        return value if math.isfinite(value) else None

    def place_take_profit(
        self,
        *,
        symbol: str,
        side: str,
        qty: Decimal | float | str,
        limit_price: float,
        filters: Optional[InstrumentFilters] = None,
        order_link_id: Optional[str] = None,
    ) -> OrderResult:
        """A take-profit leg: a resting reduce-side limit order.

        Partial take-profits are simply several of these at different prices —
        which is why the leg is its own method rather than a flag.
        """
        return self.place_order(
            symbol=symbol, side=side, qty=qty, order_type="Limit",
            price=limit_price, time_in_force="GTC", purpose="tp",
            filters=filters, order_link_id=order_link_id,
            # A leg that outlives its position (the stop fired first) must not
            # open the opposite side when price comes back to it. Spot has no
            # reduce-only; it also keeps the balance check a sell needs.
            reduce_only=self.is_linear,
        )

    # -- reconciliation ----------------------------------------------------

    def reconcile_on_startup(self, symbols: Sequence[str] = ()) -> Dict[str, Any]:
        """Resolve persisted intents against the exchange before trading.

        Audit C18: the legacy bot had no reconciliation at all. After a restart
        it forgot its positions, reset its position count to zero, and stacked
        new positions on top of forgotten ones.

        Returns a summary. Any order whose fate cannot be determined leaves the
        caller responsible for halting — this method does not decide to trade.
        """
        summary: Dict[str, Any] = {
            "checked": 0, "resolved": 0, "still_open": 0, "unknown": 0,
            "naked_positions": [], "orphan_positions": [],
        }
        # Rows left 'unknown' by an exhausted retry loop were previously never
        # revisited: pending_orders() excludes them by design (shutdown must
        # not try to cancel an order that may not exist). Reconciliation is
        # exactly the place they must be chased.
        for row in self.store.unresolved_orders():
            summary["checked"] += 1
            oid = str(row["order_link_id"])
            try:
                remote = self.get_order(symbol=str(row["symbol"]), order_link_id=oid,
                                        strict=True)
            except BybitAPIError as exc:
                logger.error("reconcile: cannot resolve %s: %s", oid, exc)
                summary["unknown"] += 1
                continue

            if remote is None:
                # The exchange has never heard of it, so the submit never landed.
                self.store.update_order_status(oid, "never_sent")
                summary["resolved"] += 1
                continue

            status = str(remote.get("orderStatus", ""))
            if status in TERMINAL_ORDER_STATUS:
                self._settle_terminal_order(row, remote, summary)
            elif status in OPEN_ORDER_STATUS:
                if str(row.get("purpose")) == "entry":
                    # A resting entry from a dead process has nobody to watch
                    # it: when it fills there is a position with no stop and
                    # no ledger row. Cancel it, and book what it executed.
                    try:
                        self.cancel_order(symbol=str(row["symbol"]),
                                          order_link_id=oid)
                        after = self.get_order(symbol=str(row["symbol"]),
                                               order_link_id=oid, strict=True)
                    except BybitAPIError as exc:
                        logger.error("reconcile: cannot cancel entry %s: %s",
                                     oid, exc)
                        after = None
                    after_status = str((after or {}).get("orderStatus", ""))
                    if after_status in TERMINAL_ORDER_STATUS:
                        self._settle_terminal_order(row, after, summary)
                        continue
                self.store.update_order_status(
                    oid, "submitted", exchange_id=str(remote.get("orderId", ""))
                )
                summary["still_open"] += 1
            else:
                summary["unknown"] += 1

        summary["naked_positions"] = [
            str(p.get("symbol")) for p in self.store.positions_without_stops()
        ]
        if symbols and self.is_linear:
            # The ledger only knows positions it wrote. A fill confirmed after
            # _await_fill's deadline, a manual trade, or a lost create reply all
            # produce a venue position the ledger never saw. Ask the venue.
            known = {str(p.get("symbol")) for p in self.store.open_positions()}
            for symbol in symbols:
                try:
                    remote = self.get_position(str(symbol))
                except BybitAPIError as exc:
                    logger.error("reconcile: cannot read venue position for %s: %s",
                                 symbol, exc)
                    summary["unknown"] += 1
                    continue
                if remote is not None and str(symbol) not in known:
                    summary["orphan_positions"].append(str(symbol))
                    logger.critical(
                        "reconcile: venue holds a %s position of size %s that the "
                        "ledger does not know; a human must resolve it",
                        symbol, remote.get("size"))
        logger.info("startup reconciliation: %s", summary)
        return summary

    def _settle_terminal_order(self, row: Mapping[str, Any],
                               remote: Mapping[str, Any],
                               summary: Dict[str, Any]) -> None:
        """Record a terminal order -- an entry that executed only AFTER it has
        been put in the book. Writing the terminal status first would drop it
        out of ``unresolved_orders`` with the coin unrecorded, and a crash or an
        error between the two steps would lose it for good."""
        oid = str(row["order_link_id"])
        status = str(remote.get("orderStatus", ""))
        is_entry = str(row.get("purpose")) == "entry"
        try:
            executed = float(remote.get("cumExecQty") or 0.0)
        except (TypeError, ValueError):
            executed = float("nan")
        if is_entry and status == "Filled" and not executed > 0.0:
            # "Filled" with no executed quantity is an anomaly, not proof that
            # nothing was bought.
            logger.error("reconcile: %s is Filled with cumExecQty=%r; left "
                         "unresolved", oid, remote.get("cumExecQty"))
            summary["unknown"] += 1
            return
        if (str(row.get("purpose")) in ("close", "emergency_close")
                and executed > 0.0
                and str(row["symbol"]) in {str(p.get("symbol")) for p in
                                           self.store.open_positions()}):
            # A close that executed while the process was down: marking it
            # terminal here would drop it from the unresolved list with the
            # sale unbooked and the position still on the book. The engine
            # books it from its own fill (`_settle_pending_closes`).
            logger.warning("reconcile: close %s executed %s while down; left "
                           "unresolved so the exit is booked", oid, executed)
            # (not "unknown": that refuses to start, and the engine can only
            # book this once it is running)
            summary["closes_to_settle"] = summary.get("closes_to_settle", 0) + 1
            return
        try:
            adopted = self._adopt_filled_spot_entry(row, remote, summary)
        except Exception:  # noqa: BLE001
            logger.exception("reconcile: could not adopt %s", oid)
            adopted = False
        if adopted is False:
            summary["unknown"] += 1           # executed but not recorded
            return
        self.store.update_order_status(
            oid, status.lower(), exchange_id=str(remote.get("orderId", "")))
        summary["resolved"] += 1

    def _adopt_filled_spot_entry(self, row: Mapping[str, Any],
                                 remote: Mapping[str, Any],
                                 summary: Dict[str, Any]) -> Optional[bool]:
        """Give the book the position an unconfirmed spot entry created.

        An entry whose fill was never confirmed in-process (a poll that timed
        out, a crash after the submit) can fill at the venue afterwards. On
        spot there is no position list to reveal it, so without this the coin
        is held with no ledger row and no stop, and the next signal stacks a
        second entry on it. The row is written with no stop, so the naked
        handling that follows reconciliation protects it (or flattens it).
        Linear is left to the orphan check, which fails closed.

        Returns ``None`` when there is nothing to adopt (not a spot entry, or
        nothing executed), ``True`` when the entry is now in the book (or its
        remainder booked as dust), ``False`` when it SHOULD have been adopted
        and was not -- the caller must then leave the order unresolved so it is
        looked at again, never mark it filled with the coin unrecorded.
        """
        if not self.is_spot or str(row.get("purpose")) != "entry":
            return None
        if normalize_side(str(row.get("side", ""))) != "Buy":
            return None
        try:
            qty = float(remote.get("cumExecQty") or 0.0)
            price = float(remote.get("avgPrice") or 0.0)
            if price <= 0 and qty > 0:
                # No average price, but the executed value is the same fact.
                price = float(remote.get("cumExecValue") or 0.0) / qty
        except (TypeError, ValueError):
            return False
        symbol = str(row["symbol"])
        if qty <= 0:
            return None
        if price <= 0:
            return False
        held = {str(p.get("symbol")): p for p in self.store.open_positions()}
        if symbol in held:
            # Already this entry's position (adopted before) is done; a
            # DIFFERENT position on the symbol means this lot cannot be booked
            # without overwriting it.
            return str(held[symbol].get("order_link_id")) == str(row["order_link_id"])
        import venue_fees
        fees = venue_fees.extract_order_fees(remote)
        # Size to what the buy left in the account (its fee is taken in the base
        # coin), or the stop placed for it would exceed the balance.
        net = venue_fees.spot_buy_net_qty(symbol, qty, fees)
        try:
            filters = self.get_instrument_filters(symbol)
            net = float(snap_qty_down(net, filters.qty_step)) if net > 0 else 0.0
            if net < float(filters.min_qty):
                self.store.add_dust(symbol, qty)
                self.store.journal(symbol, "ENTRY_DUST",
                                   "adopted entry below minimum net of fee",
                                   {"gross_filled": qty})
                return True
        except BybitAPIError:
            logger.warning("reconcile: filters unreadable for %s; adopting the "
                           "entry at the fill less its fee, unsnapped", symbol)
        if net <= 0.0:
            self.store.add_dust(symbol, qty)
            self.store.journal(symbol, "ENTRY_DUST",
                               "adopted entry nothing left net of fee",
                               {"gross_filled": qty})
            return True
        qty = net
        self.store.upsert_position(
            symbol, "Buy", qty, price, stop_price=0.0,
            order_link_id=str(row["order_link_id"]),
            meta={"entry_fees": fees.to_dict(),
                  "entry_fee_ref": {"order_link_id": str(row["order_link_id"]),
                                    "order_id": str(remote.get("orderId") or "")},
                  "adopted_at_reconcile": True})
        self.store.journal(symbol, "ENTRY_ADOPTED_AT_RECONCILE",
                           "unconfirmed spot entry had filled", {
                               "order_link_id": str(row["order_link_id"]),
                               "qty": qty, "avg_price": price})
        logger.critical("reconcile: unconfirmed spot entry %s had filled "
                        "(%s %s @ %s); adopted into the book for protection",
                        row["order_link_id"], qty, symbol, price)
        return True

    # -- private websocket -------------------------------------------------

    def ws_auth_message(self, expires_ms: Optional[int] = None) -> Dict[str, Any]:
        """Build the private-WS auth frame.

        Verified against https://bybit-exchange.github.io/docs/v5/ws/connect —
        the signed string is ``"GET/realtime" + expires``, where ``expires`` is
        an epoch-millisecond timestamp slightly in the future.

        The legacy implementation signed ``f"{ts}{api_key}"`` using
        ``perf_counter()*1000`` (a few thousand — seconds since process start).
        Auth could never succeed, so order and execution events never arrived and
        the socket reconnected every ~40 seconds forever. The bot never learned
        about a single fill.
        """
        if not self.api_key or not self.api_secret:
            raise PermanentAPIError("cannot authenticate websocket without credentials")
        expires = int(expires_ms if expires_ms is not None else self._now_ms() + 5_000)
        signature = hmac.new(
            self.api_secret.encode("utf-8"),
            f"GET/realtime{expires}".encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return {"op": "auth", "args": [self.api_key, expires, signature]}

    @staticmethod
    def ws_ping_message() -> Dict[str, str]:
        """Heartbeat frame. Bybit expects one every 20 seconds."""
        return {"op": "ping"}


# Names other modules import. One implementation, several labels.
BillionaireBybitEngine = BybitClient
BybitConnection = BybitClient
