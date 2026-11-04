"""Public Bybit v5 REST: bootstrap, cross-check and recovery for the live feed.

Public and read-only. No key, no signature, no private endpoint, no order path:
the only HTTP verb is GET and the endpoint allow-list below is the whole of what
this module can call.

Two halves:

* **Parsers** (pure): turn a stored REST exchange into typed values, or into a
  :class:`~md_events.Rejection`. HTTP 403 from the venue's CDN country block is
  its own named reason (``GEO_BLOCKED``): it is an access decision by the
  venue, reported as such, and nothing here tries to route around it.
* **Client** (I/O): a rate-limited GET with injectable transport, clock and
  sleep. On an IP-level limit (HTTP 429, or retCode 10006) it stops calling for
  the venue's documented ten-minute ban window instead of retrying into it.

The WebSocket feed gives books, tickers and trades; REST gives what the socket
cannot: instrument rules (tick, quantity step, minimums), the last SETTLED
funding print, a REST order-book touch for cross-checking the socket, and open
interest history.
"""
from __future__ import annotations

import json
import math
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

import md_events as E

#: The only endpoints this module may call. All are public market data.
ALLOWED_ENDPOINTS = (
    "/v5/market/time",
    "/v5/market/instruments-info",
    "/v5/market/tickers",
    "/v5/market/orderbook",
    "/v5/market/funding/history",
    "/v5/market/open-interest",
    "/v5/market/recent-trade",
)

MAINNET = "https://api.bybit.com"
TESTNET = "https://api-testnet.bybit.com"

#: Bybit's documented IP-limit ban window after exceeding the rate limit.
IP_BAN_WINDOW_S = 600.0
RET_IP_RATE_LIMIT = 10006


class RestTransportError(RuntimeError):
    """The request did not produce an HTTP answer at all (DNS, TLS, timeout)."""


class RestBlocked(RuntimeError):
    """The client is inside a back-off window and will not call."""


@dataclass(frozen=True)
class RestParsed:
    specs: Tuple[E.InstrumentSpec, ...] = ()
    prints: Tuple[E.FundingPrint, ...] = ()
    #: (category, bid, ask, venue_ts_ms): a REST order-book touch.
    touch: Optional[Tuple[str, float, float, Optional[int]]] = None
    server_time_ms: Optional[int] = None
    open_interest: Tuple[Tuple[int, float], ...] = ()
    rejections: Tuple[E.Rejection, ...] = ()


# -- pure parsing ---------------------------------------------------------------------------

_REST_NUM = __import__("re").compile(r"^[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?$")


def _f(x: Any, name: str, *, positive: bool = False, nonneg: bool = False) -> float:
    if isinstance(x, bool) or x is None or x == "":
        raise ValueError(f"{name} is {x!r}")
    if isinstance(x, str) and not _REST_NUM.match(x):
        raise ValueError(f"{name} is {x[:40]!r}")
    v = float(x)
    if not math.isfinite(v):
        raise ValueError(f"{name} is not finite")
    if positive and not v > 0:
        raise ValueError(f"{name} must be > 0, got {x!r}")
    if nonneg and v < 0:
        raise ValueError(f"{name} must be >= 0, got {x!r}")
    return v


def _opt(x: Any, name: str, **kw) -> Optional[float]:
    return None if x in (None, "") else _f(x, name, **kw)


def _prov(endpoint: str, ctx, body_ts: Optional[int] = None) -> E.Provenance:
    return E.Provenance(raw_id=ctx.raw_id, source=E.SOURCE_REST, conn_id="rest", topic=endpoint,
                        recv_wall_ms=ctx.recv_wall_ms, recv_mono_s=ctx.recv_mono_s,
                        exch_ts_ms=body_ts)


def parse_rest(endpoint: str, params: Mapping[str, Any], http_status: int, body: str, ctx
               ) -> RestParsed:
    """One REST exchange to typed values. Never raises on venue input."""
    def rej(reason: str, detail: str, ts: Optional[int] = None) -> RestParsed:
        return RestParsed(rejections=(E.Rejection(reason, detail[:300], _prov(endpoint, ctx, ts),
                                                  params.get("category"), params.get("symbol")),))
    if http_status == 429:
        return rej(E.RATE_LIMITED, "HTTP 429")
    if http_status == 403:
        low = str(body).lower()
        if "cloudfront" in low or "country" in low:
            return rej(E.GEO_BLOCKED, "HTTP 403 from the venue's CDN: country block")
        return rej(E.RATE_LIMITED, "HTTP 403 (IP limit)")
    try:
        j = json.loads(body)
    except (TypeError, ValueError, RecursionError, MemoryError):
        return rej(E.NOT_JSON, f"HTTP {http_status}: {str(body)[:80]!r}")
    if not isinstance(j, dict):
        return rej(E.NOT_AN_OBJECT, type(j).__name__)
    ts = j.get("time") if isinstance(j.get("time"), int) else None
    if http_status != 200 or j.get("retCode") != 0:
        code = j.get("retCode")
        return rej(E.RATE_LIMITED if code == RET_IP_RATE_LIMIT else E.RET_CODE_NOT_OK,
                   f"HTTP {http_status} retCode {code!r} {j.get('retMsg')!r}", ts)
    result = j.get("result")
    if not isinstance(result, dict):
        return rej(E.MISSING_FIELD, "result is not an object", ts)
    try:
        if endpoint == "/v5/market/time":
            sec = result.get("timeSecond")
            nano = result.get("timeNano")
            ms = int(float(nano) / 1e6) if nano else int(float(sec) * 1000)
            return RestParsed(server_time_ms=ms)
        if endpoint == "/v5/market/instruments-info":
            return RestParsed(specs=tuple(_specs(result, ctx, ts)))
        if endpoint == "/v5/market/funding/history":
            return RestParsed(prints=tuple(_prints(result, ctx, ts)))
        if endpoint == "/v5/market/orderbook":
            return RestParsed(touch=_touch(result, params.get("category", "")))
        if endpoint == "/v5/market/open-interest":
            rows = []
            for r in result.get("list") or []:
                rows.append((int(r["timestamp"]), _f(r["openInterest"], "openInterest", nonneg=True)))
            return RestParsed(open_interest=tuple(rows))
        return RestParsed()
    except Exception as exc:  # noqa: BLE001 - total: no venue body may raise out of the parser
        return rej(E.BAD_NUMBER, f"{endpoint}: {type(exc).__name__}: {exc}", ts)


def _specs(result: Dict[str, Any], ctx, ts) -> List[E.InstrumentSpec]:
    cat = result.get("category")
    if cat not in E.CATEGORIES:
        raise ValueError(f"category {cat!r}")
    out = []
    for row in result.get("list") or []:
        pf, lf = row.get("priceFilter") or {}, row.get("lotSizeFilter") or {}
        linear = cat == E.LINEAR
        out.append(E.InstrumentSpec(
            category=cat, symbol=str(row["symbol"]), status=str(row.get("status", "")),
            tick_size=_f(pf.get("tickSize"), "tickSize", positive=True),
            qty_step=_f(lf.get("qtyStep" if linear else "basePrecision"), "qtyStep", positive=True),
            min_qty=_f(lf.get("minOrderQty"), "minOrderQty", positive=True),
            max_qty=_opt(lf.get("maxOrderQty"), "maxOrderQty", positive=True),
            min_notional=_opt(lf.get("minNotionalValue" if linear else "minOrderAmt"),
                              "minNotional", nonneg=True),
            base_coin=str(row.get("baseCoin", "")), quote_coin=str(row.get("quoteCoin", "")),
            funding_interval_min=(int(row["fundingInterval"]) if linear and row.get("fundingInterval")
                                  else None),
            upper_funding_rate=_opt(row.get("upperFundingRate"), "upperFundingRate") if linear else None,
            lower_funding_rate=_opt(row.get("lowerFundingRate"), "lowerFundingRate") if linear else None,
            max_market_qty=_opt(lf.get("maxMktOrderQty" if linear else "maxMarketOrderQty"),
                                "maxMarketQty", positive=True),
            prov=_prov("/v5/market/instruments-info", ctx, ts)))
    return out


def _prints(result: Dict[str, Any], ctx, ts) -> List[E.FundingPrint]:
    out = []
    for row in result.get("list") or []:
        rate = _f(row["fundingRate"], "fundingRate")
        if abs(rate) > 0.5:
            raise ValueError(f"fundingRate {rate} is not a funding rate")
        settled = int(row["fundingRateTimestamp"])
        if settled <= 0:
            raise ValueError("fundingRateTimestamp")
        out.append(E.FundingPrint(str(row["symbol"]), rate, settled,
                                  _prov("/v5/market/funding/history", ctx, ts)))
    return out


def _touch(result: Dict[str, Any], category: str):
    bids, asks = result.get("b") or [], result.get("a") or []
    if not bids or not asks or category not in E.CATEGORIES:
        raise ValueError("order book has no touch")
    bid = _f(bids[0][0], "bid", positive=True)
    ask = _f(asks[0][0], "ask", positive=True)
    if bid >= ask:
        raise ValueError(f"REST touch crossed: {bid} >= {ask}")
    ts = result.get("ts")
    return category, bid, ask, int(ts) if ts else None


# -- the client -------------------------------------------------------------------------------

Transport = Callable[[str, float], Tuple[int, str, Dict[str, str]]]


def urllib_transport(proxy_url: Optional[str] = None, ca_file: Optional[str] = None) -> Transport:
    """A GET transport over urllib. `proxy_url` is passed in explicitly by the
    caller (this module reads no environment)."""
    import ssl
    handlers: List[Any] = []
    if proxy_url:
        handlers.append(urllib.request.ProxyHandler({"https": proxy_url, "http": proxy_url}))
    else:
        handlers.append(urllib.request.ProxyHandler({}))
    ctx = ssl.create_default_context(cafile=ca_file) if ca_file else ssl.create_default_context()
    handlers.append(urllib.request.HTTPSHandler(context=ctx))
    opener = urllib.request.build_opener(*handlers)

    def get(url: str, timeout: float) -> Tuple[int, str, Dict[str, str]]:
        req = urllib.request.Request(url, method="GET", headers={"User-Agent": "carry-md/1"})
        try:
            with opener.open(req, timeout=timeout) as resp:
                return resp.status, resp.read().decode("utf-8", "replace"), dict(resp.headers)
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8", "replace"), dict(exc.headers or {})
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise RestTransportError(f"{type(exc).__name__}: {exc}") from exc
    return get


class PublicRest:
    """Rate-limited public GET. Not thread-safe by design: one owner calls it."""

    def __init__(self, transport: Transport, *, base_url: str = MAINNET, rate_per_s: float = 5.0,
                 burst: int = 10, timeout_s: float = 10.0,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        if not base_url.startswith("https://"):
            raise ValueError("public REST is https only")
        self.transport, self.base_url, self.timeout_s = transport, base_url.rstrip("/"), timeout_s
        self._rate, self._burst = float(rate_per_s), float(burst)
        self._tokens, self._clock, self._sleep = float(burst), clock, sleep
        self._last = clock()
        self.blocked_until: float = 0.0
        self.calls = 0

    def _take(self) -> None:
        now = self._clock()
        self._tokens = min(self._burst, self._tokens + (now - self._last) * self._rate)
        self._last = now
        if self._tokens < 1.0:
            wait = (1.0 - self._tokens) / self._rate
            self._sleep(wait)
            self._tokens = 1.0
            self._last = self._clock()
        self._tokens -= 1.0

    def get(self, endpoint: str, params: Mapping[str, Any]) -> Tuple[int, str]:
        """(http_status, body). Raises RestBlocked inside a ban window and
        RestTransportError when there is no HTTP answer."""
        if endpoint not in ALLOWED_ENDPOINTS:
            raise ValueError(f"endpoint {endpoint!r} is not on the public allow-list")
        now = self._clock()
        if now < self.blocked_until:
            raise RestBlocked(f"in a rate-limit window for {self.blocked_until - now:.0f}s more")
        self._take()
        url = f"{self.base_url}{endpoint}?{urllib.parse.urlencode(sorted(params.items()))}"
        self.calls += 1
        status, body, _ = self.transport(url, self.timeout_s)
        if status == 429 or (status == 403 and "cloudfront" not in body.lower()
                             and "country" not in body.lower()):
            self.blocked_until = self._clock() + IP_BAN_WINDOW_S
        else:
            try:
                if json.loads(body).get("retCode") == RET_IP_RATE_LIMIT:
                    self.blocked_until = self._clock() + IP_BAN_WINDOW_S
            except (ValueError, AttributeError):
                pass
        return status, body


def bootstrap_plan(symbol: str, *, history_limit: int = 5) -> List[Tuple[str, Dict[str, Any], str]]:
    """The REST reads that start (or recover) a feed: (endpoint, params, category)."""
    return [
        ("/v5/market/time", {}, ""),
        ("/v5/market/instruments-info", {"category": E.LINEAR, "symbol": symbol}, E.LINEAR),
        ("/v5/market/instruments-info", {"category": E.SPOT, "symbol": symbol}, E.SPOT),
        ("/v5/market/funding/history", {"category": E.LINEAR, "symbol": symbol,
                                        "limit": history_limit}, E.LINEAR),
        ("/v5/market/orderbook", {"category": E.LINEAR, "symbol": symbol, "limit": 1}, E.LINEAR),
        ("/v5/market/orderbook", {"category": E.SPOT, "symbol": symbol, "limit": 1}, E.SPOT),
    ]


def refresh_plan(symbol: str) -> List[Tuple[str, Dict[str, Any], str]]:
    """The cheap periodic cross-check: both REST touches."""
    return [p for p in bootstrap_plan(symbol) if p[0] == "/v5/market/orderbook"]
