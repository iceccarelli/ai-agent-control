"""An offline stand-in for the Bybit V5 linear venue, for the order-identity
experiment harness (`tools/close_testnet_experiment.py`).

It implements the same `_request(method, endpoint, *, params, body, signed,
retries, timeout_s)` interface as `bybit_connection.BybitClient`, raises the
real `PermanentAPIError` / `TransientAPIError` for non-zero retCodes, and
RECORDS every request in `.requests`, so a test can assert what the harness did
and did not send.

Nothing here is evidence about the real exchange: how duplicate ids, fills and
history are answered is CONFIGURABLE (`dup_policy` and the misbehaviour
switches) precisely so the harness can be shown to record and to halt for every
variant. No network is touched.
"""
from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from typing import Any, Dict, List, Mapping, Optional

import bybit_connection as bc

FAKE_API_KEY = "FAKEKEY-do-not-leak-7c1e"
FAKE_API_SECRET = "FAKE-SECRET-VALUE-do-not-leak-9f3a"


class FakeClock:
    """Deterministic time; `sleep` advances it, so polling loops are instant."""

    def __init__(self, start_ms: int = 1_700_000_000_000) -> None:
        self._mono = 1000.0
        self._base_ms = start_ms

    def monotonic(self) -> float:
        return self._mono

    def now_ms(self) -> int:
        return int(self._base_ms + (self._mono - 1000.0) * 1000)

    def sleep(self, seconds: float) -> None:
        self._mono += max(0.0, float(seconds))

    def advance(self, seconds: float) -> None:
        self._mono += float(seconds)


def fake_cfg(**overrides: Any) -> SimpleNamespace:
    base = dict(USE_TESTNET=True, BYBIT_VENUE="testnet", PAPER_TRADING=False,
                LIVE_TRADING_ACK="", BYBIT_API_KEY=FAKE_API_KEY,
                BYBIT_API_SECRET=FAKE_API_SECRET)
    base.update(overrides)
    return SimpleNamespace(**base)


class FakeVenue:
    OPEN = ("New", "PartiallyFilled", "Created")

    def __init__(self, *, clock: Optional[FakeClock] = None,
                 symbol: str = "XRPUSDT", last: str = "0.5",
                 tick: str = "0.0001", step: str = "1", min_qty: str = "1",
                 min_notional: str = "5", max_price: str = "",
                 dup_policy: str = "reject",
                 dup_policy_by_status: Optional[Dict[str, str]] = None,
                 rt_delay: float = 0.0, hist_delay: float = 0.0,
                 fill_delay: float = 0.0, exec_split: int = 1,
                 hedge_mode: bool = False, start_position: str = "0",
                 start_open_link: str = "", unreadable_position: bool = False,
                 unreadable_account: bool = False,
                 hide_orders: bool = False, weird_status: str = "",
                 exec_sum_off: str = "0", position_offset: str = "0",
                 close_ignored: bool = False, partial_fill: bool = False,
                 create_unknown: bool = False, leak_secret: bool = False,
                 base_url: str = bc.TESTNET_REST,
                 fill_limit_orders: bool = False) -> None:
        self.clock = clock or FakeClock()
        self.base_url = base_url
        self.venue, self.testnet = "testnet", True
        self.api_key, self.api_secret = FAKE_API_KEY, FAKE_API_SECRET
        self.symbol, self.last = symbol, Decimal(last)
        self.inst = dict(tick=tick, step=step, min_qty=min_qty,
                         min_notional=min_notional, max_price=max_price)
        self.dup_policy = dup_policy
        self.dup_policy_by_status = dict(dup_policy_by_status or {})
        self.rt_delay, self.hist_delay, self.fill_delay = rt_delay, hist_delay, fill_delay
        self.exec_split = exec_split
        self.hedge_mode = hedge_mode
        self.unreadable_position = unreadable_position
        self.unreadable_account = unreadable_account
        self.hide_orders, self.weird_status = hide_orders, weird_status
        self.exec_sum_off = Decimal(exec_sum_off)
        self.position_offset = Decimal(position_offset)
        self.close_ignored, self.partial_fill = close_ignored, partial_fill
        self.create_unknown, self.leak_secret = create_unknown, leak_secret
        self.fill_limit_orders = fill_limit_orders
        self.position = Decimal(start_position)        # signed, Sell negative
        self.orders: List[Dict[str, Any]] = []
        self.execs: List[Dict[str, Any]] = []
        self.requests: List[Dict[str, Any]] = []
        self._n = 0
        if start_open_link:
            self._add_order({"category": "linear", "symbol": symbol,
                             "side": "Sell", "orderType": "Limit", "qty": "1",
                             "price": "9", "timeInForce": "PostOnly",
                             "orderLinkId": start_open_link, "positionIdx": 0,
                             "reduceOnly": False})

    # -- introspection helpers for tests -----------------------------------

    @property
    def paths(self) -> List[str]:
        return [r["path"] for r in self.requests]

    @property
    def creates(self) -> List[Dict[str, Any]]:
        return [r for r in self.requests if r["path"] == "/v5/order/create"]

    def orders_for(self, link: str) -> List[Dict[str, Any]]:
        return [o for o in self.orders if o["orderLinkId"] == link]

    # -- the interface -----------------------------------------------------

    def _request(self, method: str, endpoint: str, *, params=None, body=None,
                 signed: bool = False, retries: int = 3, timeout_s=None
                 ) -> Dict[str, Any]:
        self.requests.append({"method": method.upper(), "path": endpoint,
                              "params": dict(params or {}),
                              "body": dict(body or {}), "signed": signed,
                              "retries": retries, "url": self.base_url + endpoint,
                              "t": self.clock.monotonic()})
        self._tick()
        handler = {
            ("GET", "/v5/market/instruments-info"): self._instruments,
            ("GET", "/v5/market/tickers"): self._tickers,
            ("GET", "/v5/account/info"): self._account,
            ("GET", "/v5/position/list"): self._positions,
            ("GET", "/v5/order/realtime"): self._realtime,
            ("GET", "/v5/order/history"): self._history,
            ("GET", "/v5/execution/list"): self._executions,
            ("POST", "/v5/order/create"): self._create,
            ("POST", "/v5/order/cancel"): self._cancel,
        }.get((method.upper(), endpoint))
        if handler is None:
            raise bc.PermanentAPIError(f"unknown endpoint {endpoint}", 10001, {})
        return handler(dict(params or {}), dict(body or {}))

    # -- internals ---------------------------------------------------------

    def _ok(self, result: Any, msg: str = "OK") -> Dict[str, Any]:
        if self.leak_secret:
            msg = f"OK key={self.api_key} secret={self.api_secret}"
        return {"retCode": 0, "retMsg": msg, "result": result, "retExtInfo": {},
                "time": self.clock.now_ms()}

    def _err(self, code: int, msg: str) -> bc.PermanentAPIError:
        if self.leak_secret:
            msg = f"{msg} (apiKey={self.api_key} sign={self.api_secret})"
        return bc.PermanentAPIError(msg, code, {"retCode": code, "retMsg": msg,
                                                "result": {}})

    def _newid(self, prefix: str) -> str:
        self._n += 1
        return f"{prefix}-{self._n}"

    def _add_order(self, body: Mapping[str, Any]) -> Dict[str, Any]:
        now = self.clock.monotonic()
        order = {"orderId": self._newid("oid"), "orderLinkId": body["orderLinkId"],
                 "symbol": body["symbol"], "side": body["side"],
                 "orderType": body["orderType"], "qty": str(body["qty"]),
                 "price": str(body.get("price", "0")),
                 "reduceOnly": bool(body.get("reduceOnly")),
                 "orderStatus": "New", "cumExecQty": "0", "avgPrice": "0",
                 "created": now, "terminal_at": None,
                 "fill_at": None, "createdTime": str(self.clock.now_ms()),
                 "updatedTime": str(self.clock.now_ms())}
        if order["orderType"] == "Market" or self.fill_limit_orders:
            order["fill_at"] = now + self.fill_delay
        self.orders.append(order)
        return order

    def _tick(self) -> None:
        now = self.clock.monotonic()
        for o in self.orders:
            if o["orderStatus"] in self.OPEN and o["fill_at"] is not None \
                    and now >= o["fill_at"]:
                self._fill(o, now)

    def _fill(self, o: Dict[str, Any], now: float) -> None:
        qty = Decimal(o["qty"])
        if o["reduceOnly"] and self.close_ignored:
            fill = qty                   # claims a fill, position unchanged
        else:
            fill = qty
        if self.partial_fill and not o["reduceOnly"]:
            fill = qty / 2
        price = self.last if o["orderType"] == "Market" else Decimal(o["price"])
        sign = Decimal(-1) if o["side"] == "Sell" else Decimal(1)
        if not (o["reduceOnly"] and self.close_ignored):
            self.position += sign * fill
        parts = max(1, self.exec_split)
        for k in range(parts):
            q = fill / parts
            if k == 0:
                q -= self.exec_sum_off
            self.execs.append({"execId": self._newid("exec"),
                               "orderId": o["orderId"],
                               "orderLinkId": o["orderLinkId"],
                               "symbol": o["symbol"], "side": o["side"],
                               "execQty": format(q.normalize(), "f"),
                               "execPrice": str(price), "execType": "Trade",
                               "execFee": "0.0001", "execTime": str(self.clock.now_ms())})
        o["cumExecQty"] = format(fill.normalize(), "f")
        o["avgPrice"] = str(price)
        o["orderStatus"] = "PartiallyFilled" if self.partial_fill and \
            not o["reduceOnly"] else "Filled"
        if o["orderStatus"] == "Filled":
            o["terminal_at"] = now
        o["fill_at"] = None
        o["updatedTime"] = str(self.clock.now_ms())

    def _row(self, o: Dict[str, Any]) -> Dict[str, Any]:
        row = {k: v for k, v in o.items() if k not in ("created", "terminal_at",
                                                       "fill_at")}
        if self.weird_status:
            row["orderStatus"] = self.weird_status
        return row

    # -- handlers ----------------------------------------------------------

    def _instruments(self, params, _body):
        i = self.inst
        row = {"symbol": self.symbol, "status": "Trading",
               "contractType": "LinearPerpetual",
               "priceFilter": {"tickSize": i["tick"], "maxPrice": i["max_price"] or "1000000"},
               "lotSizeFilter": {"qtyStep": i["step"], "minOrderQty": i["min_qty"],
                                 "minNotionalValue": i["min_notional"]}}
        return self._ok({"category": "linear", "list": [row]})

    def _tickers(self, params, _body):
        return self._ok({"category": "linear", "list": [
            {"symbol": self.symbol, "lastPrice": str(self.last),
             "markPrice": str(self.last)}]})

    def _account(self, params, _body):
        if self.unreadable_account:
            return self._ok({})
        return self._ok({"marginMode": "REGULAR_MARGIN", "unifiedMarginStatus": 4})

    def _positions(self, params, _body):
        if self.unreadable_position:
            return self._ok({})
        if self.hedge_mode:
            return self._ok({"list": [
                {"symbol": self.symbol, "positionIdx": 1, "size": "0", "side": ""},
                {"symbol": self.symbol, "positionIdx": 2, "size": "0", "side": ""}]})
        shown = self.position
        if self.position != 0 or self.position_offset != 0:
            shown = self.position + (self.position_offset if self.position != 0
                                     else Decimal(0))
        side = "" if shown == 0 else ("Sell" if shown < 0 else "Buy")
        return self._ok({"list": [{"symbol": self.symbol, "positionIdx": 0,
                                   "size": format(abs(shown).normalize(), "f")
                                   if shown != 0 else "0", "side": side}]})

    def _visible(self, o: Dict[str, Any], history: bool) -> bool:
        now = self.clock.monotonic()
        if self.hide_orders and not history and False:
            return False
        if history:
            return o["orderStatus"] not in self.OPEN and o["terminal_at"] is not None \
                and now >= o["terminal_at"] + self.hist_delay
        return o["orderStatus"] in self.OPEN and now >= o["created"] + self.rt_delay

    def _list_orders(self, params, history: bool):
        if self.hide_orders and params.get("orderLinkId"):
            return self._ok({"list": []})
        rows = []
        for o in self.orders:
            if params.get("orderLinkId") and o["orderLinkId"] != params["orderLinkId"]:
                continue
            if params.get("orderId") and o["orderId"] != params["orderId"]:
                continue
            if self._visible(o, history):
                rows.append(self._row(o))
        return self._ok({"category": "linear", "list": rows})

    def _realtime(self, params, _body):
        return self._list_orders(params, False)

    def _history(self, params, _body):
        return self._list_orders(params, True)

    def _executions(self, params, _body):
        rows = [e for e in self.execs
                if (not params.get("orderLinkId")
                    or e["orderLinkId"] == params["orderLinkId"])
                and (not params.get("orderId") or e["orderId"] == params["orderId"])]
        return self._ok({"category": "linear", "list": rows})

    def _policy_for(self, existing: List[Dict[str, Any]]) -> str:
        status = existing[-1]["orderStatus"]
        return self.dup_policy_by_status.get(status, self.dup_policy)

    def _create(self, _params, body):
        if self.create_unknown:
            raise bc.PermanentAPIError("HTTP 500", 0, "")
        if body.get("category") != "linear" or body.get("symbol") != self.symbol:
            raise self._err(10001, "bad category/symbol")
        qty = Decimal(str(body.get("qty", "0")))
        if qty < Decimal(self.inst["min_qty"]):
            raise self._err(10001, "qty below minimum")
        if body.get("reduceOnly"):
            ok = (body["side"] == "Buy" and self.position < 0) or \
                 (body["side"] == "Sell" and self.position > 0)
            if not ok:
                raise self._err(110017, "current position is zero, cannot fix "
                                        "reduce-only order qty")
        existing = self.orders_for(body.get("orderLinkId", ""))
        if existing:
            policy = self._policy_for(existing)
            if policy == "reject":
                raise self._err(110072, "OrderLinkedID is duplicate")
            if policy == "accept_same":
                return self._ok({"orderId": existing[-1]["orderId"],
                                 "orderLinkId": body["orderLinkId"]})
            # accept_new falls through and creates a second order
        order = self._add_order(body)
        self._tick()
        return self._ok({"orderId": order["orderId"],
                         "orderLinkId": order["orderLinkId"]})

    def _cancel(self, _params, body):
        cands = [o for o in self.orders
                 if (body.get("orderLinkId") and o["orderLinkId"] == body["orderLinkId"])
                 or (body.get("orderId") and o["orderId"] == body["orderId"])]
        open_ones = [o for o in cands if o["orderStatus"] in self.OPEN]
        if not open_ones:
            raise self._err(110001, "order not exists or too late to cancel")
        o = open_ones[-1]
        o["orderStatus"] = "Cancelled"
        o["terminal_at"] = self.clock.monotonic()
        return self._ok({"orderId": o["orderId"], "orderLinkId": o["orderLinkId"]})
