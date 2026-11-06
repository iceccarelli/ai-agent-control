"""An offline SYNTHETIC LINEAR-PERP VENUE: a ``Transport`` the REAL ``BybitClient`` talks to.

WHY THIS EXISTS
===============
``fake_bybit.FakeBybit`` answers a handful of reads and fills every order whole, at once.
The carry book's hard cases (a partial fill, an acknowledgement lost AFTER the venue
accepted, an order the venue cannot show yet, a cancel that loses a race with a fill,
a process killed between two writes) all live in the gaps between those answers, and
the older real-stack tests replace the client with a stand-in, so signing, the host
assertion and the client's retry/ambiguity mapping never run in them.

This venue sits BELOW the client, at the ``Transport`` seam: bytes in, bytes out. The
real ``BybitClient`` signs, asserts the host, retries and maps errors; the real
``CarryBroker`` parses what comes back. Nothing here knows about the bot.

WHAT IS MODELLED (and nothing else)
===================================
* Orders keyed by ``orderLinkId``. A second create with a known link is refused with
  ``110072`` (linear) / ``170130`` (spot); the refused request never creates anything.
* Order states New / PartiallyFilled / Filled / Cancelled / Rejected, ``cumExecQty``,
  ``avgPrice``, ``cumFeeDetail`` (the shape ``venue_fees`` parses first), and an
  execution row per fill chunk (``execFee`` + ``feeCurrency``).
* Positions per symbol (size, side, avgPrice) updated by fills; one-way mode.
* A wallet (USDT, BTC) that fills, fees, realised PnL and funding move.
* Funding: ``settle_funding`` pays/charges every held position and writes the
  ``SETTLEMENT`` row (``funding`` / ``change`` / ``cashFlow`` / ``fee`` / signed ``size``)
  that ``/v5/account/transaction-log`` serves, exactly the shape ``carry_broker`` validates.
* Behaviour is SCRIPTED, never random: per link, per nth create, or by predicate
  (``Plan``). See ``Plan`` for every knob.

WHAT IS NOT MODELLED
====================
Matching, queue position, margin calls, liquidation, auto-deleveraging, rate limits, order
amendment, conditional orders, hedge mode, the demo faucet, WebSockets, and any ``/v5/asset``
path (any such request is recorded in ``unhandled`` and answered with an error: a test that
must never touch it asserts ``venue.unhandled == []``). A test passing here proves the bot's
handling of THESE semantics, not the exchange's: see ``EDGE.md`` / the testnet drill for that.

TIME
====
The venue reads the process clock (``time.time``) at each call, or a ``clock_ms`` you pass.
The tests replace ``time.time`` with one fake clock so the venue, the journal and the engine
agree on "now" and a funding settlement can be placed hours after the fill without sleeping.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

import bybit_connection as bc
from fake_bybit import API_KEY, API_SECRET

HOST = bc.TESTNET_REST

#: ``BaseException`` on purpose: ``BybitClient._request`` and ``CarryBroker`` catch
#: ``Exception`` to map failures to "outcome unknown"; a crash of the whole process is
#: not an ``Exception`` and must travel through all of them untouched.
class VenueKill(BaseException):
    """The simulated process died at a chosen point of the flow."""


PRIVATE_PATHS = frozenset({
    "/v5/order/create", "/v5/order/cancel", "/v5/order/cancel-all",
    "/v5/order/realtime", "/v5/order/history", "/v5/execution/list",
    "/v5/position/list", "/v5/position/closed-pnl", "/v5/position/set-leverage",
    "/v5/account/wallet-balance", "/v5/account/fee-rate", "/v5/account/info",
    "/v5/account/transaction-log"})

OPEN_STATES = ("New", "PartiallyFilled", "Untriggered")
TERMINAL_STATES = ("Filled", "Cancelled", "Rejected", "Deactivated", "PartiallyFilledCanceled")


@dataclass
class Plan:
    """How ONE order behaves. Every field is deterministic.

    ``ack``
        What the CLIENT is told about the create request:
        ``"ok"``                 the venue answers normally;
        ``"5xx"``                HTTP 502 (the client retries);
        ``"timeout"``            the transport raises ``TimeoutError``;
        ``"drop_after_accept"``  the transport raises ``ConnectionResetError``.
        The bad ack applies to the first ``ack_failures`` create ATTEMPTS for the link.
        ``accepted_on_failure`` says whether the venue created the order before the ack
        failed (default: False for ``5xx``, True for ``timeout`` / ``drop_after_accept``).
    ``fills``
        Sizes of the fill chunks, in order (base coin). ``None`` = one chunk of the full
        quantity for a market order, and no fill at all for a resting limit order. The
        first chunk lands when the order is created (market) or when the first state read
        happens (limit); each later chunk lands after ``reads_between_fills`` state
        reads of the link. After the last chunk, an unfilled remainder is cancelled
        (``remainder="cancel"``, IOC-like) or keeps resting (``"rest"``).
    ``visibility_delay``
        The order is ABSENT from ``order/realtime`` and ``order/history`` for this many
        state reads (a read = a realtime request carrying the link, plus the history
        request that follows it).
    ``cancel_fills_first``
        A cancel of this order loses the race: the rest fills, the cancel is answered
        ``110001`` and the order ends Filled.
    ``duplicate_exec_rows`` / ``reverse_exec_rows``
        ``execution/list`` serves every row twice (same ``execId``) / newest first.
    ``fee_currency``
        Override the currency the fee is charged in (default: the venue's rule).
    ``reject``
        ``(retCode, retMsg)``: the venue refuses the create outright; nothing is created.
    """

    ack: str = "ok"
    ack_failures: int = 1
    accepted_on_failure: Optional[bool] = None
    fills: Optional[Sequence[float]] = None
    price: Optional[float] = None
    reads_between_fills: int = 1
    remainder: str = "cancel"
    visibility_delay: int = 0
    cancel_fills_first: bool = False
    duplicate_exec_rows: bool = False
    reverse_exec_rows: bool = False
    fee_currency: Optional[str] = None
    reject: Optional[Tuple[int, str]] = None

    def accepted(self) -> bool:
        if self.accepted_on_failure is not None:
            return bool(self.accepted_on_failure)
        return self.ack in ("timeout", "drop_after_accept")


@dataclass
class _Order:
    link: str
    order_id: str
    category: str
    symbol: str
    side: str
    order_type: str
    qty: float
    price: Optional[float]
    tif: str
    reduce_only: bool
    plan: Plan
    created_ms: int
    status: str = "New"
    cum_qty: float = 0.0
    cum_value: float = 0.0
    cum_fee: Dict[str, float] = field(default_factory=dict)
    chunks: List[float] = field(default_factory=list)       # chunks still to land
    reads: int = 0                                         # state reads that saw it
    hidden_left: int = 0
    last_read_hidden: Optional[bool] = None
    updated_ms: int = 0
    exec_rows: List[Dict[str, Any]] = field(default_factory=list)


class SyntheticVenue(bc.Transport):
    """See the module docstring. ``request`` is the whole surface the client uses."""

    def __init__(self, *, clock_ms: Optional[Callable[[], int]] = None,
                 mark: float = 50_000.0, spot: Optional[float] = None,
                 usdt: float = 10_000.0, btc: float = 1.0,
                 funding_rate: float = 0.0006,
                 perp_taker: float = 0.00055, perp_maker: float = 0.0002,
                 spot_taker: float = 0.001, spot_maker: float = 0.001,
                 spot_buy_fee_in: str = "BTC",
                 margin_mode: str = "REGULAR_MARGIN") -> None:
        self._clock = clock_ms or (lambda: int(time.time() * 1000))
        self._last_ms = 0
        self.mark = float(mark)
        self.spot = float(spot if spot is not None else mark)
        self.half_spread = 0.0001           # bid/ask = price * (1 -/+ 1 bp)
        self.funding_rate = float(funding_rate)     # the NEXT settlement's rate (ticker)
        self.fee = {("linear", "taker"): perp_taker, ("linear", "maker"): perp_maker,
                    ("spot", "taker"): spot_taker, ("spot", "maker"): spot_maker}
        self.spot_buy_fee_in = spot_buy_fee_in
        self.margin_mode = margin_mode
        self.leverage = 10.0
        self.liq_factor = 1.6               # a short's liqPrice = avgPrice * this
        self.qty_step, self.min_qty, self.min_notional = "0.001", "0.001", "5"
        self.wallet: Dict[str, float] = {"USDT": float(usdt), "BTC": float(btc)}
        self.collateral = {"BTC": True, "USDT": True}
        #: signed base-coin size (short negative) and VWAP entry per linear symbol
        self.position: Dict[str, float] = {}
        self.avg_price: Dict[str, float] = {}
        self.realised: Dict[str, float] = {}
        self.orders: Dict[str, _Order] = {}
        self.funding_prints: List[Tuple[int, float]] = []         # (ms, rate), settled
        self.settlements: List[Dict[str, Any]] = []
        self.closed_pnl: List[Dict[str, Any]] = []
        self.executions: List[Dict[str, Any]] = []
        # -- scripting
        self.default_plan = Plan()
        self._by_link: Dict[str, Plan] = {}
        self._by_nth: Dict[int, Plan] = {}
        self._by_predicate: List[Tuple[Callable[[Mapping[str, Any]], bool], Plan]] = []
        self._kills: Dict[str, Tuple[int, BaseException]] = {}
        self._injected: List[Tuple[str, int, Any]] = []
        self.signature_must_verify = True
        #: called as ``fn(venue, order)`` right after an order is ACCEPTED (and its first
        #: chunk has landed), before any response: lets a test change the venue mid-flow.
        self.on_accept: List[Callable[["SyntheticVenue", _Order], None]] = []
        self._frozen_positions: Optional[Dict[str, Dict[str, Any]]] = None
        # -- observation
        #: every private request the venue refused for authentication, as (path, retCode)
        self.auth_failures: List[Tuple[str, int]] = []
        self.requests: List[Dict[str, Any]] = []
        self.requests_by_path: Counter = Counter()
        #: ACCEPTED orders per link: what the venue really created (a refused duplicate
        #: is not one). A link with 2 here is a double order.
        self.creates_by_link: Counter = Counter()
        #: create REQUESTS per link, including retries and refused duplicates.
        self.create_attempts_by_link: Counter = Counter()
        self.unhandled: List[str] = []
        self._creates_seen = 0
        self._distinct_links = 0
        self._plan_of_link: Dict[str, Plan] = {}
        self._ids = 0
        self._exec_ids = 0

    # ------------------------------------------------------------------ scripting

    def script_link(self, link: str, plan: Plan) -> None:
        self._by_link[link] = plan

    def script_nth_create(self, n: int, plan: Plan) -> None:
        """The plan for the n-th DISTINCT new link the venue sees (1-based)."""
        self._by_nth[int(n)] = plan

    def script_when(self, predicate: Callable[[Mapping[str, Any]], bool], plan: Plan) -> None:
        """The plan for the first new order whose create body satisfies ``predicate``."""
        self._by_predicate.append((predicate, plan))

    def kill_at(self, point: str, *, nth: int = 1,
                exc: Optional[BaseException] = None) -> None:
        """Raise a ``BaseException`` (default ``VenueKill``) at a chosen point:

        ``"create:before_accept"``   the nth create REQUEST dies before the venue sees it;
        ``"create:after_accept"``    the venue books the order (and its fills), then the
                                     process dies before any response is read;
        ``"path:/v5/..."``           the nth request to that path dies before it is handled.
        """
        self._kills[point] = (int(nth), exc if exc is not None else VenueKill(point))

    def freeze_positions(self) -> None:
        """From now on ``position/list`` serves the rows as they are NOW (a lagging replica)
        until ``unfreeze_positions``; the position itself keeps moving."""
        self._frozen_positions = {s: self._position_row(s) for s in set(self.position) | {"BTCUSDT"}}

    def unfreeze_positions(self) -> None:
        self._frozen_positions = None

    def inject(self, path: str, answer: Any, *, times: int = 1) -> None:
        """Answer the next ``times`` requests to ``path`` with ``answer`` instead of the
        real handler: an ``(status, text)`` pair, or an exception instance to raise."""
        self._injected.append((path, int(times), answer))

    # ------------------------------------------------------------------ state setters

    def set_mark(self, price: float, spot: Optional[float] = None) -> None:
        self.mark = float(price)
        self.spot = float(spot if spot is not None else price)

    def add_funding_print(self, rate: float, at_ms: int) -> None:
        self.funding_prints.append((int(at_ms), float(rate)))
        self.funding_prints.sort()

    def settle_funding(self, rate: Optional[float] = None, *, at_ms: Optional[int] = None
                       ) -> List[Dict[str, Any]]:
        """Pay or charge every held linear position at ``mark`` and the given rate.

        A positive rate: longs pay, shorts receive. ``usd = -signed_size * mark * rate``.
        Returns the SETTLEMENT rows written (one per held position)."""
        rate = self.funding_rate if rate is None else float(rate)
        at = int(at_ms if at_ms is not None else self._now())
        rows = []
        for symbol, size in sorted(self.position.items()):
            if abs(size) <= 1e-12:
                continue
            usd = -size * self.mark * rate
            self.wallet["USDT"] += usd
            row = {"id": f"settle-{len(self.settlements) + 1}", "symbol": symbol,
                   "category": "linear", "type": "SETTLEMENT", "currency": "USDT",
                   "side": "Sell" if size < 0 else "Buy",
                   "size": _num(size), "funding": _num(usd), "change": _num(usd),
                   "cashFlow": "0", "fee": "0", "qty": "0", "feeRate": "0",
                   "tradePrice": _num(self.mark), "transactionTime": str(at)}
            self.settlements.append(row)
            rows.append(row)
        self.add_funding_print(rate, at)
        return rows

    # ------------------------------------------------------------------ views for assertions

    def net_position(self, symbol: str = "BTCUSDT") -> float:
        """Signed linear size at the VENUE (short negative)."""
        return self.position.get(symbol, 0.0)

    def orders_for(self, *, category: Optional[str] = None, side: Optional[str] = None,
                   symbol: Optional[str] = None) -> List[_Order]:
        return [o for o in self.orders.values()
                if (category is None or o.category == category)
                and (side is None or o.side == side)
                and (symbol is None or o.symbol == symbol)]

    def open_orders(self) -> List[_Order]:
        return [o for o in self.orders.values() if o.status in OPEN_STATES]

    def create_bodies(self) -> List[Dict[str, Any]]:
        return [r["body_json"] for r in self.requests
                if r["path"] == "/v5/order/create" and r["body_json"]]

    def urls(self) -> List[str]:
        return [r["url"] for r in self.requests]

    # ------------------------------------------------------------------ the Transport

    def request(self, method: str, url: str, *, headers: Mapping[str, str],
                params: Optional[Mapping[str, Any]] = None, body: Optional[str] = None,
                timeout: float = 10.0) -> Tuple[int, str]:
        path, _, query = url.partition("?")
        path = path[len(HOST):] if path.startswith(HOST) else path
        q = dict(kv.split("=", 1) for kv in query.split("&") if "=" in kv)
        q.update({k: str(v) for k, v in (params or {}).items()})
        body_json = json.loads(body) if body else None
        self.requests.append({"method": method.upper(), "url": url, "path": path,
                              "params": q, "body": body, "body_json": body_json,
                              "headers": dict(headers)})
        self.requests_by_path[path] += 1
        self._kill_check(f"path:{path}", self.requests_by_path[path])
        injected = self._take_injected(path)
        if injected is not None:
            if isinstance(injected, BaseException):
                raise injected
            return injected
        if path in PRIVATE_PATHS:
            denied = self._authenticate(headers, method, query, body)
            if denied is not None:
                self.auth_failures.append((path, json.loads(denied[1])["retCode"]))
                return denied
        handler = {
            "/v5/market/time": self._time,
            "/v5/market/tickers": self._tickers,
            "/v5/market/instruments-info": self._instruments,
            "/v5/market/orderbook": self._orderbook,
            "/v5/market/funding/history": self._funding_history,
            "/v5/account/fee-rate": self._fee_rate,
            "/v5/account/wallet-balance": self._wallet,
            "/v5/account/info": self._account_info,
            "/v5/account/transaction-log": self._transaction_log,
            "/v5/position/list": self._position_list,
            "/v5/position/closed-pnl": self._closed_pnl,
            "/v5/position/set-leverage": lambda q, b: self._ok({}),
            "/v5/order/realtime": lambda q, b: self._order_read(q, history=False),
            "/v5/order/history": lambda q, b: self._order_read(q, history=True),
            "/v5/execution/list": self._execution_list,
            "/v5/order/cancel": self._cancel,
        }.get(path)
        if path == "/v5/order/create":
            return self._create(q, body_json or {})
        if handler is None:
            self.unhandled.append(path)
            return self._err(10001, f"synthetic venue: no such endpoint {path}")
        return handler(q, body_json or {})

    # ------------------------------------------------------------------ plumbing

    def _now(self) -> int:
        self._last_ms = max(self._last_ms, int(self._clock()))
        return self._last_ms

    @staticmethod
    def _ok(result: Any) -> Tuple[int, str]:
        return 200, json.dumps({"retCode": 0, "retMsg": "OK", "result": result})

    @staticmethod
    def _err(code: int, msg: str) -> Tuple[int, str]:
        return 200, json.dumps({"retCode": code, "retMsg": msg, "result": {}})

    def _kill_check(self, point: str, count: int) -> None:
        armed = self._kills.get(point)
        if armed and count >= armed[0]:
            del self._kills[point]
            raise armed[1]

    def _take_injected(self, path: str) -> Any:
        for i, (p, times, answer) in enumerate(self._injected):
            if p == path:
                if times <= 1:
                    del self._injected[i]
                else:
                    self._injected[i] = (p, times - 1, answer)
                return answer
        return None

    def _authenticate(self, headers: Mapping[str, str], method: str, query: str,
                      body: Optional[str]) -> Optional[Tuple[int, str]]:
        sign = headers.get("X-BAPI-SIGN")
        if not sign:
            return self._err(10007, "unsigned request to a private endpoint")
        if headers.get("X-BAPI-API-KEY") != API_KEY:
            return self._err(10003, "API key is invalid")
        if not self.signature_must_verify:
            return None
        ts = headers.get("X-BAPI-TIMESTAMP", "")
        recv = headers.get("X-BAPI-RECV-WINDOW", "")
        payload = query if method.upper() == "GET" else (body or "")
        expected = hmac.new(API_SECRET.encode(), f"{ts}{API_KEY}{recv}{payload}".encode(),
                            hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, sign):
            return self._err(10004, "error sign!")
        try:
            if abs(int(ts) - self._now()) > int(recv):
                return self._err(10002, "timestamp outside recv_window")
        except ValueError:
            return self._err(10002, "bad timestamp")
        return None

    # ------------------------------------------------------------------ market data

    def _time(self, q, b):
        now = self._now()
        return self._ok({"timeSecond": str(now // 1000), "timeNano": str(now * 1_000_000)})

    def _px(self, category: str) -> float:
        return self.mark if category == "linear" else self.spot

    def _tickers(self, q, b):
        category, symbol = q.get("category", ""), q.get("symbol", "")
        if symbol != "BTCUSDT" or category not in ("linear", "spot"):
            return self._ok({"category": category, "list": []})
        last = self._px(category)
        row = {"symbol": symbol, "lastPrice": _num(last),
               "bid1Price": _num(last * (1 - self.half_spread)), "bid1Size": "10",
               "ask1Price": _num(last * (1 + self.half_spread)), "ask1Size": "10"}
        if category == "linear":
            row.update({"markPrice": _num(self.mark), "indexPrice": _num(self.spot),
                        "fundingRate": _num(self.funding_rate),
                        "nextFundingTime": str(self._now() + 8 * 3600 * 1000)})
        return self._ok({"category": category, "list": [row]})

    def _instruments(self, q, b):
        category, symbol = q.get("category", ""), q.get("symbol", "")
        if symbol != "BTCUSDT":
            return self._ok({"category": category, "list": []})
        if category == "linear":
            row = {"symbol": symbol, "status": "Trading",
                   "priceFilter": {"tickSize": "0.10"},
                   "lotSizeFilter": {"qtyStep": self.qty_step, "minOrderQty": self.min_qty,
                                     "maxOrderQty": "100", "minNotionalValue": self.min_notional}}
        else:
            row = {"symbol": symbol, "status": "Trading",
                   "priceFilter": {"tickSize": "0.01"},
                   "lotSizeFilter": {"basePrecision": "0.000001", "minOrderQty": "0.000048",
                                     "minOrderAmt": "5"}}
        return self._ok({"category": category, "list": [row]})

    def _orderbook(self, q, b):
        category, symbol = q.get("category", ""), q.get("symbol", "")
        px = self._px(category)
        bid, ask = px * (1 - self.half_spread), px * (1 + self.half_spread)
        return self._ok({"s": symbol, "ts": self._now(), "u": 1,
                         "b": [[_num(bid - i), "5"] for i in range(3)],
                         "a": [[_num(ask + i), "5"] for i in range(3)]})

    def _funding_history(self, q, b):
        lo, hi = int(q.get("startTime", 0) or 0), int(q.get("endTime", 1 << 62) or (1 << 62))
        limit = max(1, min(int(q.get("limit", 200) or 200), 200))
        rows = [{"symbol": q.get("symbol", "BTCUSDT"), "fundingRate": _num(rate),
                 "fundingRateTimestamp": str(ts)}
                for ts, rate in sorted(self.funding_prints, reverse=True) if lo <= ts <= hi]
        return self._ok({"category": "linear", "list": rows[:limit]})

    # ------------------------------------------------------------------ account

    def _fee_rate(self, q, b):
        category = q.get("category", "")
        return self._ok({"list": [{"symbol": q.get("symbol", ""),
                                   "takerFeeRate": _num(self.fee[(category, "taker")]),
                                   "makerFeeRate": _num(self.fee[(category, "maker")])}]})

    def _account_info(self, q, b):
        return self._ok({"marginMode": self.margin_mode, "unifiedMarginStatus": 4,
                         "isMasterTrader": False, "spotHedgingStatus": "OFF"})

    def _notional(self, symbol: str) -> float:
        return abs(self.position.get(symbol, 0.0)) * self.mark

    def equity(self) -> float:
        unreal = sum(size * (self.mark - self.avg_price.get(s, self.mark))
                     for s, size in self.position.items())
        return self.wallet["USDT"] + self.wallet["BTC"] * self.spot + unreal

    def _wallet(self, q, b):
        coins = []
        want = q.get("coin")
        for coin in ("BTC", "USDT"):
            if want and coin != want:
                continue
            bal = self.wallet[coin]
            usd = bal * (self.spot if coin == "BTC" else 1.0)
            coins.append({"coin": coin, "walletBalance": _num(bal), "locked": "0",
                          "equity": _num(bal), "usdValue": _num(usd),
                          "marginCollateral": self.collateral[coin],
                          "collateralSwitch": self.collateral[coin], "colRes": "",
                          "availableToWithdraw": ""})
        im = sum(self._notional(s) / self.leverage for s in self.position)
        return self._ok({"list": [{
            "accountType": "UNIFIED", "totalEquity": _num(self.equity()),
            "totalWalletBalance": _num(self.equity()),
            "totalAvailableBalance": _num(max(0.0, self.equity() - im)),
            "accountMMRate": "0.0100", "accountIMRate": "0.0500", "coin": coins}]})

    def _position_row(self, symbol: str) -> Dict[str, Any]:
        size = self.position.get(symbol, 0.0)
        if abs(size) <= 1e-12:
            return {"symbol": symbol, "side": "", "size": "0", "positionIdx": 0, "avgPrice": "0",
                    "markPrice": _num(self.mark), "positionIM": "0", "positionMM": "0",
                    "liqPrice": "", "leverage": _num(self.leverage), "positionValue": "0",
                    "unrealisedPnl": "0", "cumRealisedPnl": _num(self.realised.get(symbol, 0.0))}
        avg = self.avg_price[symbol]
        notional = abs(size) * self.mark
        return {"symbol": symbol, "side": "Sell" if size < 0 else "Buy", "size": _num(abs(size)),
                "positionIdx": 0, "avgPrice": _num(avg), "markPrice": _num(self.mark),
                "positionValue": _num(abs(size) * avg),
                "positionIM": _num(notional / self.leverage),
                "positionMM": _num(notional * 0.01),
                "liqPrice": _num(avg * self.liq_factor) if size < 0 else _num(avg / self.liq_factor),
                "leverage": _num(self.leverage),
                "unrealisedPnl": _num(size * (self.mark - avg)),
                "cumRealisedPnl": _num(self.realised.get(symbol, 0.0))}

    def _position_list(self, q, b):
        symbol = q.get("symbol", "BTCUSDT")
        row = (self._frozen_positions or {}).get(symbol) if self._frozen_positions is not None \
            else None
        return self._ok({"category": "linear", "list": [row or self._position_row(symbol)],
                         "nextPageCursor": ""})

    def _closed_pnl(self, q, b):
        rows = [r for r in self.closed_pnl if r["symbol"] == q.get("symbol")]
        return self._ok({"category": "linear", "list": rows, "nextPageCursor": ""})

    def _transaction_log(self, q, b):
        lo, hi = int(q.get("startTime", 0) or 0), int(q.get("endTime", 1 << 62) or (1 << 62))
        rows = [r for r in self.settlements
                if q.get("type") in (None, "", r["type"]) and lo <= int(r["transactionTime"]) <= hi]
        limit = max(1, min(int(q.get("limit", 50) or 50), 50))
        start = int(q.get("cursor") or 0)
        page = rows[start:start + limit]
        nxt = str(start + limit) if start + limit < len(rows) else ""
        return self._ok({"list": page, "nextPageCursor": nxt})

    # ------------------------------------------------------------------ orders

    def _plan_for(self, body: Mapping[str, Any]) -> Plan:
        """The plan for a link, decided ONCE (at its first create attempt) and kept: a retry
        of the same link meets the same script."""
        link = body["orderLinkId"]
        if link in self._plan_of_link:
            return self._plan_of_link[link]
        plan = self.default_plan
        if link in self._by_link:
            plan = self._by_link[link]
        else:
            self._distinct_links += 1
            if self._distinct_links in self._by_nth:
                plan = self._by_nth[self._distinct_links]
            else:
                for i, (pred, candidate) in enumerate(self._by_predicate):
                    if pred(body):
                        plan = candidate
                        del self._by_predicate[i]
                        break
        self._plan_of_link[link] = plan
        return plan

    def _create(self, q, body) -> Tuple[int, str]:
        link = str(body.get("orderLinkId", ""))
        self.create_attempts_by_link[link] += 1
        self._creates_seen += 1
        self._kill_check("create:before_accept", self._creates_seen)
        for required in ("category", "symbol", "side", "orderType", "qty"):
            if not body.get(required):
                return self._err(10001, f"missing {required}")
        if not link or len(link) > 36:
            return self._err(10001, "orderLinkId missing or longer than 36")
        category = body["category"]
        if link in self.orders:
            code = 110072 if category == "linear" else 170130
            return self._err(code, "OrderLinkedID is duplicate")
        plan = self._plan_for(body)
        bad_ack = plan.ack != "ok" and plan.ack_failures > 0
        if plan.reject is not None:
            return self._err(*plan.reject)
        if bad_ack and not plan.accepted():
            return self._ack_failure(plan, link)
        refusal = self._pre_trade_refusal(body)
        if refusal is not None:
            return refusal
        order = self._accept(body, plan)
        for hook in list(self.on_accept):
            hook(self, order)
        self._kill_check("create:after_accept", self._creates_seen)
        if bad_ack:
            return self._ack_failure(plan, link)
        return self._ok({"orderId": order.order_id, "orderLinkId": link})

    def _ack_failure(self, plan: Plan, link: str) -> Tuple[int, str]:
        # Consume one failing attempt of this link: later attempts behave normally.
        plan.ack_failures = max(0, plan.ack_failures - 1)
        if plan.ack == "5xx":
            return 502, "bad gateway"
        if plan.ack == "timeout":
            raise TimeoutError("synthetic venue: read timed out")
        raise ConnectionResetError("synthetic venue: connection reset by peer")

    def _pre_trade_refusal(self, body) -> Optional[Tuple[int, str]]:
        category, symbol, side = body["category"], body["symbol"], body["side"]
        qty = float(body["qty"])
        if category == "linear" and body.get("reduceOnly") in (True, "true"):
            size = self.position.get(symbol, 0.0)
            held_short = -size if size < 0 else 0.0
            if side != "Buy" or qty > held_short + 1e-12:
                return self._err(110017, "Reduce-only rule not satisfied")
        if category == "spot" and side == "Buy":
            px = self._px("spot")
            if self.wallet["USDT"] + 1e-9 < qty * px:
                return self._err(170131, "Insufficient balance")
        if category == "spot" and side == "Sell" and self.wallet["BTC"] + 1e-12 < qty:
            return self._err(170131, "Insufficient balance")
        return None

    def _accept(self, body, plan: Plan) -> _Order:
        self._ids += 1
        link, qty = body["orderLinkId"], float(body["qty"])
        is_limit = body["orderType"] == "Limit"
        o = _Order(link=link, order_id=f"syn-{self._ids}", category=body["category"],
                   symbol=body["symbol"], side=body["side"], order_type=body["orderType"],
                   qty=qty, price=float(body["price"]) if body.get("price") else None,
                   tif=body.get("timeInForce", "IOC" if not is_limit else "GTC"),
                   reduce_only=body.get("reduceOnly") in (True, "true"), plan=plan,
                   created_ms=self._now(), hidden_left=plan.visibility_delay)
        o.updated_ms = o.created_ms
        self.orders[link] = o
        self.creates_by_link[link] += 1
        if plan.fills is not None:
            o.chunks = [float(x) for x in plan.fills]
        elif not is_limit:
            o.chunks = [qty]
        if is_limit and o.tif == "PostOnly":
            px, side = o.price, o.side
            bid, ask = self.mark * (1 - self.half_spread), self.mark * (1 + self.half_spread)
            if (side == "Sell" and px <= bid + 1e-9) or (side == "Buy" and px >= ask - 1e-9):
                o.status, o.chunks = "Cancelled", []          # would take liquidity
                return o
        if not is_limit and o.chunks:
            self._land_next_chunk(o)                           # a market order starts filling at once
        elif not is_limit:
            self._settle_status(o)                             # a market order with nothing scheduled
        return o

    def _fill_price(self, o: _Order) -> float:
        if o.plan.price is not None:
            return o.plan.price
        if o.order_type == "Limit" and o.price is not None:
            return o.price
        return self._px(o.category)

    def _land_next_chunk(self, o: _Order) -> None:
        if not o.chunks or o.status not in OPEN_STATES:
            return
        left = o.qty - o.cum_qty
        qty = min(o.chunks.pop(0), left)
        if qty > 1e-12:
            self._execute(o, qty)
        self._settle_status(o)

    def _settle_status(self, o: _Order) -> None:
        if o.status not in OPEN_STATES:
            return
        if o.cum_qty >= o.qty - 1e-12:
            o.status = "Filled"
        elif o.chunks:
            o.status = "PartiallyFilled" if o.cum_qty > 0 else "New"
        elif o.order_type == "Market" or (o.plan.fills is not None and o.plan.remainder == "cancel"):
            o.status = "Cancelled"                  # nothing more is scheduled: the venue cancels the rest
        else:
            o.status = "PartiallyFilled" if o.cum_qty > 0 else "New"
        o.updated_ms = self._now()

    def _fee_ccy_and_amount(self, o: _Order, qty: float, px: float) -> Tuple[str, float]:
        maker = o.order_type == "Limit"
        rate = self.fee[(o.category, "maker" if maker else "taker")]
        if o.category == "spot" and o.side == "Buy":
            ccy = o.plan.fee_currency or self.spot_buy_fee_in
            return ccy, (qty * rate if ccy == "BTC" else qty * px * rate)
        return (o.plan.fee_currency or "USDT"), qty * px * rate

    def _execute(self, o: _Order, qty: float) -> None:
        px = self._fill_price(o)
        ccy, fee = self._fee_ccy_and_amount(o, qty, px)
        o.cum_value += qty * px
        o.cum_qty += qty
        o.cum_fee[ccy] = o.cum_fee.get(ccy, 0.0) + fee
        sign = -1.0 if o.side == "Sell" else 1.0
        if o.category == "linear":
            self._apply_linear_fill(o, sign * qty, px)
        else:
            self._apply_spot_fill(o, qty, px)
        self.wallet[ccy] -= fee
        self._exec_ids += 1
        row = {"execId": f"syn-exec-{self._exec_ids}", "orderId": o.order_id,
               "orderLinkId": o.link, "symbol": o.symbol, "category": o.category,
               "side": o.side, "execQty": _num(qty), "execPrice": _num(px),
               "execValue": _num(qty * px), "execFee": _num(fee), "feeCurrency": ccy,
               "isMaker": o.order_type == "Limit", "execType": "Trade",
               "execTime": str(self._now())}
        o.exec_rows.append(row)
        self.executions.append(row)
        o.updated_ms = self._now()

    def _apply_linear_fill(self, o: _Order, signed: float, px: float) -> None:
        sym = o.symbol
        cur = self.position.get(sym, 0.0)
        new = cur + signed
        if abs(cur) > 1e-12 and cur * signed < 0:               # reduces (or flips) the position
            closed = min(abs(signed), abs(cur))
            pnl = (self.avg_price[sym] - px) * closed if cur < 0 else (px - self.avg_price[sym]) * closed
            self.wallet["USDT"] += pnl
            self.realised[sym] = self.realised.get(sym, 0.0) + pnl
            self.closed_pnl.append({"symbol": sym, "side": "Buy" if cur < 0 else "Sell",
                                    "qty": _num(closed), "avgEntryPrice": _num(self.avg_price[sym]),
                                    "avgExitPrice": _num(px), "closedPnl": _num(pnl),
                                    "updatedTime": str(self._now()), "createdTime": str(self._now())})
            if abs(new) <= 1e-12:
                new, self.avg_price[sym] = 0.0, 0.0
            elif cur * new < 0:
                self.avg_price[sym] = px
        elif abs(cur) <= 1e-12:
            self.avg_price[sym] = px
        else:                                                     # adds: VWAP
            self.avg_price[sym] = (abs(cur) * self.avg_price[sym] + abs(signed) * px) / abs(new)
        self.position[sym] = new

    def _apply_spot_fill(self, o: _Order, qty: float, px: float) -> None:
        if o.side == "Buy":
            self.wallet["BTC"] += qty
            self.wallet["USDT"] -= qty * px
        else:
            self.wallet["BTC"] -= qty
            self.wallet["USDT"] += qty * px

    # -- reads of orders

    def _order_row(self, o: _Order) -> Dict[str, Any]:
        avg = o.cum_value / o.cum_qty if o.cum_qty > 0 else 0.0
        return {"orderId": o.order_id, "orderLinkId": o.link, "symbol": o.symbol,
                "category": o.category, "side": o.side, "orderType": o.order_type,
                "qty": _num(o.qty), "price": _num(o.price) if o.price else "0",
                "timeInForce": o.tif, "reduceOnly": o.reduce_only, "orderStatus": o.status,
                "cumExecQty": _num(o.cum_qty), "cumExecValue": _num(o.cum_value),
                "avgPrice": _num(avg) if o.cum_qty > 0 else "0",
                "cumFeeDetail": {c: _num(v) for c, v in o.cum_fee.items()},
                "leavesQty": _num(max(0.0, o.qty - o.cum_qty)),
                "createdTime": str(o.created_ms), "updatedTime": str(o.updated_ms)}

    def _advance_on_read(self, o: _Order) -> None:
        """A visible state read: the next scheduled chunk lands once enough reads have passed."""
        o.reads += 1
        if o.chunks and o.status in OPEN_STATES and o.reads > o.plan.reads_between_fills:
            self._land_next_chunk(o)

    def _order_read(self, q, *, history: bool) -> Tuple[int, str]:
        link, symbol, category = q.get("orderLinkId"), q.get("symbol"), q.get("category")
        if link:
            o = self.orders.get(link)
            if o is None:
                return self._ok({"category": category, "list": [], "nextPageCursor": ""})
            if not history:
                if o.hidden_left > 0:
                    o.hidden_left -= 1
                    o.last_read_hidden = True
                else:
                    o.last_read_hidden = False
            elif o.last_read_hidden is None and o.hidden_left > 0:
                o.hidden_left -= 1
                o.last_read_hidden = True
            if o.last_read_hidden:
                return self._ok({"category": category, "list": [], "nextPageCursor": ""})
            if not history:
                self._advance_on_read(o)
            rows = [o]
        else:
            rows = [x for x in self.orders.values() if x.hidden_left <= 0]
            rows = [x for x in rows if (not symbol or x.symbol == symbol)
                    and (not category or x.category == category)]
        keep = [x for x in rows if (x.status in TERMINAL_STATES) == history]
        return self._ok({"category": category, "list": [self._order_row(x) for x in keep],
                         "nextPageCursor": ""})

    def _execution_list(self, q, b):
        link, oid = q.get("orderLinkId"), q.get("orderId")
        if not (link or oid):
            return self._err(10001, "orderId or orderLinkId required")
        rows = [dict(r) for r in self.executions
                if r["symbol"] == q.get("symbol")
                and (not link or r["orderLinkId"] == link) and (not oid or r["orderId"] == oid)]
        o = self.orders.get(link) if link else next(
            (x for x in self.orders.values() if x.order_id == oid), None)
        if o is not None:
            if o.plan.duplicate_exec_rows:
                rows = [r for row in rows for r in (row, dict(row))]
            if o.plan.reverse_exec_rows:
                rows.reverse()
        limit = max(1, min(int(q.get("limit", 50) or 50), 100))
        start = int(q.get("cursor") or 0)
        page = rows[start:start + limit]
        nxt = str(start + limit) if start + limit < len(rows) else ""
        return self._ok({"category": q.get("category", ""), "list": page, "nextPageCursor": nxt})

    def _cancel(self, q, body) -> Tuple[int, str]:
        link = body.get("orderLinkId", "")
        o = self.orders.get(link)
        if o is None or o.status not in OPEN_STATES:
            return self._err(110001, "Order does not exist or too late to cancel")
        if o.plan.cancel_fills_first:
            left = o.qty - o.cum_qty
            o.chunks = []
            if left > 1e-12:
                self._execute(o, left)
            o.status = "Filled"
            o.updated_ms = self._now()
            return self._err(110001, "Order does not exist or too late to cancel")
        o.status, o.chunks = "Cancelled", []
        o.updated_ms = self._now()
        return self._ok({"orderId": o.order_id, "orderLinkId": o.link})


def _num(x: float) -> str:
    """A venue-style decimal string, never scientific notation."""
    s = f"{float(x):.10f}".rstrip("0").rstrip(".")
    return s if s not in ("", "-0") else "0"
