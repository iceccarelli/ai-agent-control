"""An offline Bybit V5 stand-in.

Good enough to exercise signing, quantisation, idempotency, balance rejection,
reconciliation and the conditional-stop payload without a network. It is
deliberately strict: it *rejects* requests the real exchange would reject, so a
test that passes here is evidence, not decoration.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Any, Dict, List, Mapping, Optional, Tuple

API_KEY = "test-key"
API_SECRET = "test-secret"

SPOT_INSTRUMENTS: Dict[str, Dict[str, Any]] = {
    # Realistic values. Note ADA's 0.1 step and SHIB's sub-cent tick — the exact
    # cases the legacy hardcoded {tick 0.01, step 0.001} filter set got wrong.
    "BTCUSDT": {
        "priceFilter": {"tickSize": "0.01"},
        "lotSizeFilter": {"basePrecision": "0.000001", "minOrderQty": "0.000048",
                          "minOrderAmt": "5"},
    },
    "ADAUSDT": {
        "priceFilter": {"tickSize": "0.0001"},
        "lotSizeFilter": {"basePrecision": "0.1", "minOrderQty": "1",
                          "minOrderAmt": "5"},
    },
    "SHIBUSDT": {
        "priceFilter": {"tickSize": "0.00000001"},
        "lotSizeFilter": {"basePrecision": "1", "minOrderQty": "100",
                          "minOrderAmt": "5"},
    },
}

LAST_PRICE = {"BTCUSDT": 50_000.0, "ADAUSDT": 0.45, "SHIBUSDT": 0.00002}


class FakeBybit:
    """Records every request and answers like the V5 API."""

    def __init__(
        self,
        balances: Optional[Dict[str, float]] = None,
        equity: float = 10_000.0,
    ) -> None:
        self.balances = dict(balances or {"USDT": 10_000.0, "BTC": 1.0, "ADA": 1_000.0})
        self.equity = equity
        self.orders: Dict[str, Dict[str, Any]] = {}
        self.requests: List[Dict[str, Any]] = []
        #: Queue of (status, body) to return instead of the normal answer.
        self.scripted: List[Tuple[int, str]] = []
        self.verify_signature = True
        #: Quoted spread in basis points, so tests can widen it.
        self.spread_bps = 4.0
        self.reject_next_with: Optional[Tuple[int, str]] = None
        #: Linear-only state. ``positions[symbol]`` mirrors what
        #: /v5/position/list returns, including the ``stopLoss`` field that
        #: /v5/position/trading-stop writes. A spot client never touches these.
        self.positions: Dict[str, Dict[str, Any]] = {}
        #: Rows served by /v5/position/closed-pnl (linear). Tests append.
        self.closed_pnl: List[Dict[str, Any]] = []
        #: Funding rate as a FRACTION per 8h, as the tickers endpoint reports it.
        self.funding_rate: float = 0.0001
        #: Set to a retCode to make the next trading-stop call fail.
        self.trading_stop_fails_with: Optional[int] = None
        #: Levels served by /v5/market/orderbook, or None for "no book".
        self.book: Optional[Dict[str, Any]] = None
        self.leverage_calls: List[Dict[str, Any]] = []

    # -- helpers -----------------------------------------------------------

    def _ok(self, result: Any) -> Tuple[int, str]:
        return 200, json.dumps({"retCode": 0, "retMsg": "OK", "result": result})

    def _err(self, code: int, msg: str) -> Tuple[int, str]:
        return 200, json.dumps({"retCode": code, "retMsg": msg, "result": {}})

    def submitted_orders(self, purpose: Optional[str] = None) -> List[Dict[str, Any]]:
        rows = list(self.orders.values())
        if purpose is None:
            return rows
        return [r for r in rows if r.get("_purpose") == purpose]

    def order_create_bodies(self) -> List[Dict[str, Any]]:
        return [
            json.loads(r["body"])
            for r in self.requests
            if r["url"].endswith("/v5/order/create") and r["body"]
        ]

    # -- the transport seam ------------------------------------------------

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
        self.requests.append(
            {"method": method, "url": url, "headers": dict(headers),
             "params": dict(params or {}), "body": body}
        )

        if self.scripted:
            return self.scripted.pop(0)

        path = url.split("?", 1)[0].replace("https://api-testnet.bybit.com", "")
        path = path.replace("https://api.bybit.com", "")
        query = url.split("?", 1)[1] if "?" in url else ""
        # The client appends GET params to the URL (they must match the signed
        # query string exactly), so parse them back out here rather than relying
        # on a separate params kwarg.
        if query:
            parsed = dict(
                kv.split("=", 1) for kv in query.split("&") if "=" in kv
            )
            params = {**parsed, **dict(params or {})}

        if headers.get("X-BAPI-SIGN") and self.verify_signature:
            if not self._signature_valid(headers, method, query, body):
                return 200, json.dumps(
                    {"retCode": 10004, "retMsg": "error sign!", "result": {}}
                )

        if path == "/v5/market/time":
            return 200, json.dumps({"retCode": 0, "result": {
                "timeSecond": str(int(time.time())),
                "timeNano": str(int(time.time() * 1e9)),
            }})

        if path == "/v5/market/instruments-info":
            symbol = (params or {}).get("symbol", "")
            info = SPOT_INSTRUMENTS.get(symbol)
            if info is None:
                return self._ok({"list": []})
            return self._ok({"list": [{"symbol": symbol, **info}]})

        if path == "/v5/market/tickers":
            symbol = (params or {}).get("symbol", "")
            if symbol not in LAST_PRICE:
                return self._ok({"list": []})
            last = LAST_PRICE[symbol]
            half = last * self.spread_bps / 2.0 / 10_000.0
            body = {"retCode": 0, "retMsg": "OK", "time": int(time.time() * 1000),
                    "result": {"list": [{
                        "symbol": symbol, "lastPrice": str(last),
                        "bid1Price": str(last - half), "ask1Price": str(last + half),
                        "fundingRate": str(self.funding_rate),
                    }]}}
            return 200, json.dumps(body)

        if path == "/v5/market/kline":
            # newest-first, including an in-progress candle, like the real API
            rows = [[str(1000 - i), "1", "2", "0.5", "1.5", "10", "15"]
                    for i in range(int((params or {}).get("limit", 5)))]
            return self._ok({"list": rows})

        if path == "/v5/account/wallet-balance":
            return self._ok({"list": [{
                "totalEquity": str(self.equity),
                "coin": [{"coin": c, "availableToWithdraw": str(v),
                          "walletBalance": str(v)}
                         for c, v in self.balances.items()],
            }]})

        if path == "/v5/market/orderbook":
            if self.book is None:
                return self._err(10001, "no order book")
            return self._ok({**self.book,
                             "s": (params or {}).get("symbol", ""),
                             "ts": int(time.time() * 1000), "u": 1})

        if path == "/v5/position/closed-pnl":
            symbol = (params or {}).get("symbol", "")
            rows = [r for r in self.closed_pnl if r.get("symbol") == symbol]
            return self._ok({"list": rows})

        if path == "/v5/position/list":
            symbol = (params or {}).get("symbol", "")
            row = self.positions.get(symbol)
            return self._ok({"list": [row] if row else []})

        if path == "/v5/position/trading-stop":
            payload = json.loads(body or "{}")
            if self.trading_stop_fails_with is not None:
                return self._err(self.trading_stop_fails_with, "trading-stop refused")
            symbol = payload.get("symbol", "")
            row = self.positions.get(symbol)
            if not row:
                return self._err(110017, "position idx not match position mode")
            row["stopLoss"] = str(payload.get("stopLoss", ""))
            row["tpslMode"] = payload.get("tpslMode", "")
            return self._ok({})

        if path == "/v5/position/set-leverage":
            payload = json.loads(body or "{}")
            self.leverage_calls.append(payload)
            return self._ok({})

        if path == "/v5/order/create":
            return self._create_order(json.loads(body or "{}"))

        if path in ("/v5/order/realtime", "/v5/order/history"):
            return self._query_order(params or {}, path)

        if path == "/v5/order/cancel":
            payload = json.loads(body or "{}")
            oid = payload.get("orderLinkId", "")
            if oid in self.orders:
                self.orders[oid]["orderStatus"] = "Cancelled"
                return self._ok({"orderLinkId": oid})
            return self._err(110001, "order not exists or too late to cancel")

        if path == "/v5/order/cancel-all":
            for row in self.orders.values():
                if row["orderStatus"] in ("New", "Untriggered"):
                    row["orderStatus"] = "Cancelled"
            return self._ok({"list": []})

        return self._err(10001, f"unknown endpoint {path}")

    # -- behaviour ---------------------------------------------------------

    def _signature_valid(
        self, headers: Mapping[str, str], method: str, query: str, body: Optional[str]
    ) -> bool:
        ts = headers.get("X-BAPI-TIMESTAMP", "")
        recv = headers.get("X-BAPI-RECV-WINDOW", "")
        payload = query if method.upper() == "GET" else (body or "")
        expected = hmac.new(
            API_SECRET.encode(),
            f"{ts}{headers.get('X-BAPI-API-KEY','')}{recv}{payload}".encode(),
            hashlib.sha256,
        ).hexdigest()
        return hmac.compare_digest(expected, headers.get("X-BAPI-SIGN", ""))

    def _create_order(self, body: Dict[str, Any]) -> Tuple[int, str]:
        if self.reject_next_with is not None:
            code, msg = self.reject_next_with
            self.reject_next_with = None
            return self._err(code, msg)

        oid = body.get("orderLinkId", "")
        if not oid:
            return self._err(10001, "orderLinkId required by this bot's policy")
        if len(oid) > 36:
            return self._err(10001, "orderLinkId exceeds 36 characters")
        if oid in self.orders:
            # The real exchange rejects a duplicate orderLinkId. This is the
            # property that makes a retry safe.
            return self._err(170130, "Duplicate orderLinkId")

        for required in ("category", "symbol", "side", "orderType", "qty"):
            if not body.get(required):
                return self._err(10001, f"missing {required}")

        purpose = "stop" if body.get("orderFilter") == "StopOrder" else (
            "tp" if body.get("orderType") == "Limit" else "entry"
        )
        status = "Untriggered" if purpose == "stop" else (
            "New" if body.get("orderType") == "Limit" else "Filled"
        )
        self.orders[oid] = {
            "orderId": f"ex-{len(self.orders) + 1}",
            "orderLinkId": oid,
            "symbol": body["symbol"],
            "side": body["side"],
            "orderType": body["orderType"],
            "qty": body["qty"],
            "price": body.get("price", ""),
            "orderStatus": status,
            "orderFilter": body.get("orderFilter", "Order"),
            "triggerPrice": body.get("triggerPrice", ""),
            "_purpose": purpose,
            "_body": body,
        }
        return self._ok({"orderId": self.orders[oid]["orderId"], "orderLinkId": oid})

    def _query_order(
        self, params: Mapping[str, Any], path: str
    ) -> Tuple[int, str]:
        oid = params.get("orderLinkId", "")
        order_id = params.get("orderId", "")
        rows = [
            r for r in self.orders.values()
            if (oid and r["orderLinkId"] == oid) or (order_id and r["orderId"] == order_id)
        ]
        if not oid and not order_id:
            rows = [r for r in self.orders.values()
                    if r["orderStatus"] in ("New", "Untriggered", "PartiallyFilled")]
            if params.get("symbol"):
                rows = [r for r in rows if r["symbol"] == params["symbol"]]
        if path.endswith("realtime"):
            rows = [r for r in rows
                    if r["orderStatus"] in ("New", "Untriggered", "PartiallyFilled")]
        return self._ok({"list": rows})
