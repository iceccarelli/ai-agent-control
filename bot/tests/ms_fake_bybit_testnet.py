"""In-process FAKE Bybit V5 testnet for the offline proof of the testnet execution path. It is the INNER transport beneath ``ms_testnet_transport.TestnetTransport`` (so the
real signing, capability, host and entry-authority code all run); it opens no socket. Semantics follow the V5 docs (fetched 2026-11-08): HMAC signature over
``timestamp+apiKey+recvWindow+(query|body)`` (retCode 10004 on a bad one, 10002 outside the recv window), retCode 0 envelope, orderLinkId unique (110072), realtime/history
by orderLinkId, execution list newest first with cursors, reduce-only clamp / 110017 without a position, qty-step / tick / min-notional rejects, spot market BUY read in QUOTE
units unless marketUnit=baseCoin, rate limit 10006, spot fee optionally charged in the received base coin. Fault injection: ``inject(path, kind, times, match)``.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
from decimal import Decimal as D
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import parse_qsl, urlsplit

HOST = "api-testnet.bybit.com"
FAKE_KEY = "CANARY_TESTNET_KEY_aaaa1111"
FAKE_SECRET = "_".join(("CANARY", "TESTNET", "SECRET", "bbbb2222cccc3333"))


class SimulatedCrash(BaseException):
    """The process dies (BaseException: no ``except Exception`` swallows it)."""


def _s(x: Any) -> str:
    return format(D(str(x)).normalize(), "f") if D(str(x)) != 0 else "0"


class FakeBybitTestnet:
    def __init__(self, *, key: str = FAKE_KEY, secret: str = FAKE_SECRET, now_ms: Optional[Callable[[], int]] = None) -> None:
        self.key, self.secret = key, secret
        self.now_ms = now_ms or (lambda: int(time.time() * 1000))     # the DATA clock (execTime, book ts): a test may drive it
        self.wall_ms = lambda: int(time.time() * 1000)                # the AUTH clock: real time, so a simulated clock never invalidates a signature
        self.skew_ms = 0
        self.instruments: Dict[str, Dict[str, Any]] = {
            "BTCUSDT": {"tick": "0.1", "step": "0.001", "min_qty": "0.001", "min_notional": "5", "base": "BTC", "status": "Trading", "funding": 480},
            "ETHUSDT": {"tick": "0.01", "step": "0.01", "min_qty": "0.01", "min_notional": "5", "base": "ETH", "status": "Trading", "funding": 480},
        }
        self.spot_instruments: Dict[str, Dict[str, Any]] = {
            "BTCUSDT": {"tick": "0.01", "base_precision": "0.000001", "min_amt": "1", "base": "BTC", "status": "Trading"},
            "ETHUSDT": {"tick": "0.01", "base_precision": "0.0001", "min_amt": "1", "base": "ETH", "status": "Trading"},
        }
        self.book: Dict[str, Dict[str, str]] = {"BTCUSDT": {"bid": "60000.0", "ask": "60001.0", "mark": "60000.5", "spot_bid": "59999.99", "spot_ask": "60001.01", "spot_mark": "60000.5"},
                                                "ETHUSDT": {"bid": "3000.00", "ask": "3000.10", "mark": "3000.05", "spot_bid": "2999.99", "spot_ask": "3000.11", "spot_mark": "3000.05"}}
        self.wallet: Dict[str, D] = {"USDT": D("10000"), "BTC": D("0"), "ETH": D("0")}
        self.positions: Dict[str, Dict[str, Any]] = {}
        self.leverage: Dict[str, str] = {}
        self.orders: Dict[str, Dict[str, Any]] = {}
        self.by_link: Dict[str, str] = {}
        self.execs: List[Dict[str, Any]] = []
        self.seq = 0
        self.requests: List[Tuple[str, str, Dict[str, str], str]] = []      # (method, path, query, body): NEVER headers
        self.create_calls: Dict[str, int] = {}
        self.faults: List[Dict[str, Any]] = []
        self.api_perms: Dict[str, Any] = {"readOnly": 0, "permissions": {"ContractTrade": ["Order", "Position"], "Spot": ["SpotTrade"]}, "ips": ["*"]}
        self.position_idx_hedge = False
        self.spot_fee_in_base = False
        self.dup_executions = False
        self.hidden_execs: set = set()
        self.page_size = 50
        self.liquidity: Dict[str, D] = {}
        self.rest_filled_on_create: Dict[str, float] = {}
        self.maker_rate, self.taker_rate = D("0.0002"), D("0.00055")
        self.fee_rates: Optional[Dict[str, Tuple[str, str]]] = None             # {"spot": (maker, taker), "linear": (maker, taker)} as fractions; None = the fee-rate read is refused
        self.funding_rate, self.funding_in_ms = "0.0001", 3_600_000            # the perp ticker's funding rate and the time to the next settlement (tests set these)
        self.fill_delay_ms = 0                                       # >0: a crossing order is ACCEPTED now and its fill appears only after this long (asynchronous matching, as on a real venue)
        self.pending: List[Tuple[int, str, D, D]] = []
        self.funding_prints: Optional[List[Tuple[int, str]]] = None   # None = the public funding-history endpoint is not implemented (answers an error); a list = the settlements it reports (ts_ms, rate)
        self.reject: Dict[str, Tuple[int, str]] = {}                  # orderLinkId suffix -> (retCode, retMsg) answered INSTEAD of creating the order

    # ---------------------------------------------------------------- fault injection
    def inject(self, path: str, kind: str, times: int = 1, match: Optional[Callable[[Dict[str, Any]], bool]] = None) -> None:
        """kind: timeout | http500 | http403 | malformed | no_retcode | rate_limit | lose_response (the request EXECUTES, the response is lost) | crash_after (executes, then the
        process dies) | redirect | stale_account (reads answer with an old updatedTime)."""
        self.faults.append({"path": path, "kind": kind, "times": times, "match": match})

    def _fault(self, path: str, data: Dict[str, Any]) -> Optional[str]:
        for f in self.faults:
            if f["path"] == path and f["times"] > 0 and (f["match"] is None or f["match"](data)):
                f["times"] -= 1
                return f["kind"]
        return None

    # ---------------------------------------------------------------- transport entry
    def request(self, method: str, url: str, *, headers: Dict[str, str], params: Any = None, body: Optional[str] = None, timeout: float = 10.0) -> Tuple[int, str]:
        self._release_due()
        p = urlsplit(url)
        assert p.scheme == "https" and p.netloc == HOST, f"fake got a request for {url!r}"
        path, q = p.path, dict(parse_qsl(p.query, keep_blank_values=True))
        data = json.loads(body) if body else {}
        self.requests.append((method, path, q, body or ""))
        view = dict(q) if method == "GET" else dict(data)
        kind = self._fault(path, view)
        if kind == "timeout":
            raise TimeoutError("read timed out")
        if kind == "http500":
            return 500, "<html>internal error</html>"
        if kind == "http403":
            return 403, "forbidden"
        if kind == "redirect":
            return 302, ""
        if kind == "malformed":
            return 200, "{not json"
        if kind == "no_retcode":
            return 200, json.dumps({"result": {}})
        if kind == "rate_limit":
            return 200, json.dumps({"retCode": 10006, "retMsg": "Too many visits!", "result": {}})
        private = not path.startswith("/v5/market/")
        if private:
            err = self._auth(headers, q if method == "GET" else None, body or "")
            if err:
                return 200, json.dumps(err)
        res = self._route(method, path, q, data)
        if kind == "lose_response":
            raise TimeoutError("response lost")
        if kind == "crash_after":
            raise SimulatedCrash(path)
        return 200, json.dumps(res)

    def _auth(self, h: Dict[str, str], q: Optional[Dict[str, str]], body: str) -> Optional[Dict[str, Any]]:
        g = {k.lower(): v for k, v in h.items()}
        if g.get("x-bapi-api-key") != self.key:
            return {"retCode": 10003, "retMsg": "API key is invalid.", "result": {}}
        ts, rw = g.get("x-bapi-timestamp", ""), g.get("x-bapi-recv-window", "5000")
        try:
            tsi, rwi = int(ts), int(rw)
        except ValueError:
            return {"retCode": 10002, "retMsg": "bad timestamp", "result": {}}
        now = self.wall_ms() + self.skew_ms
        if not (now - rwi <= tsi <= now + 1000):
            return {"retCode": 10002, "retMsg": "invalid request, please check your server timestamp or recv_window param", "result": {}}
        payload = "&".join(f"{k}={v}" for k, v in q.items()) if q is not None else body
        want = hmac.new(self.secret.encode(), f"{ts}{self.key}{rw}{payload}".encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(want, g.get("x-bapi-sign", "")):
            return {"retCode": 10004, "retMsg": "error sign!", "result": {}}
        return None

    def _ok(self, result: Any) -> Dict[str, Any]:
        return {"retCode": 0, "retMsg": "OK", "result": result, "retExtInfo": {}, "time": self.wall_ms() + self.skew_ms}

    def _err(self, code: int, msg: str) -> Dict[str, Any]:
        return {"retCode": code, "retMsg": msg, "result": {}, "retExtInfo": {}, "time": self.wall_ms() + self.skew_ms}

    # ---------------------------------------------------------------- routes
    def _route(self, method: str, path: str, q: Dict[str, str], d: Dict[str, Any]) -> Dict[str, Any]:
        if path == "/v5/market/time":
            return self._ok({"timeSecond": str((self.wall_ms() + self.skew_ms) // 1000), "timeNano": str((self.wall_ms() + self.skew_ms) * 1_000_000)})
        if path == "/v5/market/instruments-info":
            return self._instruments(q)
        if path == "/v5/market/tickers":
            return self._tickers(q)
        if path == "/v5/market/orderbook":
            return self._orderbook(q)
        if path == "/v5/user/query-api":
            return self._ok({"id": "1", "apiKey": "<masked>", "readOnly": self.api_perms["readOnly"], "permissions": self.api_perms["permissions"], "ips": self.api_perms["ips"], "type": 1})
        if path == "/v5/account/wallet-balance":
            coins = [{"coin": c, "walletBalance": _s(v), "locked": "0", "equity": _s(v), "usdValue": _s(v * (D(self.book["BTCUSDT"]["mark"]) if c == "BTC" else D(self.book["ETHUSDT"]["mark"]) if c == "ETH" else 1))} for c, v in self.wallet.items()]
            eq = sum(D(c["usdValue"]) for c in coins)
            return self._ok({"list": [{"accountType": "UNIFIED", "totalEquity": _s(eq), "coin": coins}]})
        if path == "/v5/account/info":
            return self._ok({"marginMode": "REGULAR_MARGIN", "unifiedMarginStatus": 4, "updatedTime": str(self.now_ms())})
        if path == "/v5/account/fee-rate":
            if self.fee_rates is None:                                              # the key is not permitted to read it (the real Testnet key's permissions may not allow it)
                return self._err(10005, "permission denied")
            maker, taker = self.fee_rates[q.get("category", "")]
            return self._ok({"category": q.get("category"), "list": [{"symbol": q.get("symbol", ""), "takerFeeRate": taker, "makerFeeRate": maker}]})
        if path == "/v5/position/list":
            return self._position_list(q)
        if path == "/v5/position/set-leverage":
            if d.get("buyLeverage") == self.leverage.get(d.get("symbol")):
                return self._err(110043, "leverage not modified")
            self.leverage[d["symbol"]] = d["buyLeverage"]
            return self._ok({})
        if path == "/v5/market/funding/history":
            if self.funding_prints is None:
                return self._err(10001, "unsupported path /v5/market/funding/history")
            lo, hi = int(q.get("startTime", 0) or 0), int(q.get("endTime", 10 ** 15) or 10 ** 15)
            rows = [{"symbol": q.get("symbol"), "fundingRate": r, "fundingRateTimestamp": str(t)} for t, r in sorted(self.funding_prints, reverse=True) if lo <= t <= hi]
            return self._ok({"category": q.get("category"), "list": rows[: int(q.get("limit", 200) or 200)]})
        if path == "/v5/order/create":
            return self._create(d)
        if path == "/v5/order/cancel":
            return self._cancel(d)
        if path in ("/v5/order/realtime", "/v5/order/history"):
            return self._order_list(path, q)
        if path == "/v5/execution/list":
            return self._exec_list(q)
        return self._err(10001, f"unsupported path {path}")

    def _instruments(self, q: Dict[str, str]) -> Dict[str, Any]:
        cat, sym = q.get("category"), q.get("symbol")
        rows = []
        if cat == "linear":
            for s, i in self.instruments.items():
                if sym in (None, s):
                    rows.append({"symbol": s, "status": i["status"], "baseCoin": i["base"], "quoteCoin": "USDT", "fundingInterval": i["funding"], "upperFundingRate": "0.0075", "lowerFundingRate": "-0.0075",
                                 "priceFilter": {"tickSize": i["tick"]}, "leverageFilter": {"minLeverage": "1", "maxLeverage": "100", "leverageStep": "0.01"},
                                 "lotSizeFilter": {"minNotionalValue": i["min_notional"], "maxOrderQty": "1000", "maxMktOrderQty": "500", "minOrderQty": i["min_qty"], "qtyStep": i["step"]}})
        elif cat == "spot":
            for s, i in self.spot_instruments.items():
                if sym in (None, s):
                    rows.append({"symbol": s, "status": i["status"], "baseCoin": i["base"], "quoteCoin": "USDT", "priceFilter": {"tickSize": i["tick"]},
                                 "lotSizeFilter": {"basePrecision": i["base_precision"], "minOrderAmt": i["min_amt"], "maxLimitOrderQty": "1000", "maxMarketOrderQty": "500"}})
        return self._ok({"category": cat, "list": rows, "nextPageCursor": ""})

    def _tickers(self, q: Dict[str, str]) -> Dict[str, Any]:
        s, cat = q.get("symbol", ""), q.get("category")
        b = self.book.get(s)
        if b is None:
            return self._ok({"category": cat, "list": []})
        if cat == "spot":
            return self._ok({"category": cat, "list": [{"symbol": s, "bid1Price": b["spot_bid"], "ask1Price": b["spot_ask"], "lastPrice": b["spot_mark"], "markPrice": ""}]})
        return self._ok({"category": cat, "list": [{"symbol": s, "bid1Price": b["bid"], "ask1Price": b["ask"], "lastPrice": b["mark"], "markPrice": b["mark"], "indexPrice": b["spot_mark"],
                                                     "fundingRate": self.funding_rate, "nextFundingTime": str(self.now_ms() + self.funding_in_ms)}]})

    def _orderbook(self, q: Dict[str, str]) -> Dict[str, Any]:
        s, cat = q.get("symbol", ""), q.get("category")
        b = self.book.get(s)
        if b is None:
            return self._err(10001, "symbol invalid")
        bid, ask = (b["spot_bid"], b["spot_ask"]) if cat == "spot" else (b["bid"], b["ask"])
        cap = str(self.liquidity.get(s, D("100")))
        return self._ok({"s": s, "b": [[bid, cap]], "a": [[ask, cap]], "ts": self.now_ms(), "u": 1})

    def _position_list(self, q: Dict[str, str]) -> Dict[str, Any]:
        if q.get("category") != "linear" or not (q.get("symbol") or q.get("settleCoin")):
            return self._err(10001, "params error")
        rows = []
        syms = [q["symbol"]] if q.get("symbol") else sorted(set(self.instruments) | set(self.positions))
        for s in syms:
            p = self.positions.get(s)
            idxs = (1, 2) if self.position_idx_hedge else (0,)
            for ix in idxs:
                size = abs(p["size"]) if p else D(0)
                side = ("Buy" if p["size"] > 0 else "Sell") if p and p["size"] != 0 else ""
                if q.get("settleCoin") and size == 0 and s not in self.instruments:
                    continue
                rows.append({"symbol": s, "side": side, "size": _s(size), "positionIdx": ix, "leverage": self.leverage.get(s, "10"), "avgPrice": _s(p["avg"]) if p else "0",
                             "updatedTime": str(self.now_ms())})
        return self._page(rows, q, 200)

    def _page(self, rows: List[Dict[str, Any]], q: Dict[str, str], default: int) -> Dict[str, Any]:
        lim = min(int(q.get("limit", default)), self.page_size)
        start = int(q["cursor"]) if q.get("cursor") else 0
        chunk = rows[start:start + lim]
        nxt = str(start + lim) if start + lim < len(rows) else ""
        return self._ok({"category": q.get("category"), "list": chunk, "nextPageCursor": nxt})

    # ---------------------------------------------------------------- orders
    def _order_row(self, o: Dict[str, Any]) -> Dict[str, Any]:
        return {"orderId": o["orderId"], "orderLinkId": o["orderLinkId"], "symbol": o["symbol"], "side": o["side"], "orderType": o["orderType"], "qty": o["qty"], "price": o.get("price", "0"),
                "orderStatus": o["status"], "cumExecQty": _s(o["cum"]), "avgPrice": _s(o["avg"]) if o["cum"] > 0 else "", "reduceOnly": o["reduceOnly"], "category": o["category"],
                "createdTime": str(o["created"]), "updatedTime": str(self.now_ms())}

    def _create(self, d: Dict[str, Any]) -> Dict[str, Any]:
        link = d.get("orderLinkId", "")
        self.create_calls[link] = self.create_calls.get(link, 0) + 1
        cat, sym, side, ot = d.get("category"), d.get("symbol"), d.get("side"), d.get("orderType")
        if link and link in self.by_link:
            return self._err(110072, "Duplicate orderLinkId")
        for suffix, (code, msg) in self.reject.items():
            if link.endswith(suffix):
                return self._err(code, msg)
        if cat not in ("linear", "spot") or side not in ("Buy", "Sell") or ot not in ("Limit", "Market") or sym not in self.book:
            return self._err(10001, "params error")
        if self.position_idx_hedge and "positionIdx" not in d:
            return self._err(10001, "position idx not match position mode")
        inst = self.instruments[sym] if cat == "linear" else self.spot_instruments[sym]
        try:
            qty = D(d["qty"])
        except Exception:                                              # noqa: BLE001
            return self._err(10001, "qty invalid")
        b = self.book[sym]
        ask, bid = D(b["ask"] if cat == "linear" else b["spot_ask"]), D(b["bid"] if cat == "linear" else b["spot_bid"])
        mark = D(b["mark"] if cat == "linear" else b["spot_mark"])
        if cat == "spot" and ot == "Market" and side == "Buy" and d.get("marketUnit") != "baseCoin":
            if qty < D(inst["min_amt"]):                              # qty is QUOTE value by default for a spot market buy
                return self._err(170140, "Order value exceeded lower limit.")
        elif cat == "linear":
            if qty <= 0 or qty % D(inst["step"]) != 0:
                return self._err(10001, "Qty invalid")
            if qty < D(inst["min_qty"]):
                return self._err(110017, "Qty below minimum")
            if qty * mark < D(inst["min_notional"]):
                return self._err(110094, "Order does not meet minimum order value")
        else:
            if qty <= 0 or qty % D(inst["base_precision"]) != 0:
                return self._err(170130, "Parameter qty invalid")
            if qty * mark < D(inst["min_amt"]):
                return self._err(170140, "Order value exceeded lower limit.")
        price = None
        if ot == "Limit":
            if "price" not in d or D(d["price"]) % D(inst["tick"]) != 0:
                return self._err(10001, "Price invalid")
            price = D(d["price"])
        reduce = d.get("reduceOnly") is True
        pos = self.positions.get(sym, {"size": D(0)})["size"]
        if reduce:
            if cat != "linear" or pos == 0 or (side == "Buy") == (pos > 0):
                return self._err(110017, "Reduce-only rule not satisfied")
        if cat == "spot" and side == "Buy" and ot == "Limit" and qty * price > self.wallet["USDT"]:
            return self._err(170131, "Insufficient balance")
        if cat == "spot" and side == "Sell" and qty > self.wallet[inst["base"]]:
            return self._err(170131, "Insufficient balance")
        self.seq += 1
        oid = f"O{self.seq:06d}"
        o = {"orderId": oid, "orderLinkId": link, "symbol": sym, "side": side, "orderType": ot, "qty": d["qty"], "price": d.get("price"), "status": "New", "cum": D(0), "avg": D(0),
             "reduceOnly": reduce, "category": cat, "tif": d.get("timeInForce", "IOC" if ot == "Market" else "GTC"), "created": self.now_ms(), "qty_d": qty, "marketUnit": d.get("marketUnit")}
        self.orders[oid] = o
        if link:
            self.by_link[link] = oid
        crossing = ot == "Market" or (side == "Buy" and price >= ask) or (side == "Sell" and price <= bid)
        if ot == "Limit" and o["tif"] == "PostOnly" and crossing:
            o["status"] = "Cancelled"
        elif crossing:
            px = ask if side == "Buy" else bid
            if cat == "spot" and ot == "Market" and side == "Buy" and d.get("marketUnit") != "baseCoin":
                qty = qty / px
            if self.fill_delay_ms > 0:
                self.pending.append((self.now_ms() + self.fill_delay_ms, oid, qty, px))
                return self._ok({"orderId": oid, "orderLinkId": link})
            avail = self.liquidity.get(sym)
            fq = min(qty, reduce and abs(pos) or qty, avail if avail is not None else qty)
            fq = (fq // D(inst.get("step", inst.get("base_precision")))) * D(inst.get("step", inst.get("base_precision")))
            if fq > 0:
                self._fill(o, fq, px, taker=True)
            if o["cum"] < o["qty_d"] and (o["tif"] in ("IOC", "FOK") or reduce):
                o["status"] = "PartiallyFilledCanceled" if o["cum"] > 0 else "Cancelled"
        return self._ok({"orderId": oid, "orderLinkId": link})

    def _release_due(self) -> None:
        now = self.now_ms()
        due = [x for x in self.pending if x[0] <= now]
        self.pending = [x for x in self.pending if x[0] > now]
        for _t, oid, qty, px in due:
            o = self.orders[oid]
            if o["status"] != "New":
                continue
            inst = self.instruments[o["symbol"]] if o["category"] == "linear" else self.spot_instruments[o["symbol"]]
            step = D(inst.get("step", inst.get("base_precision")))
            pos = self.positions.get(o["symbol"], {"size": D(0)})["size"]
            fq = min(qty, abs(pos)) if o["reduceOnly"] else qty
            fq = (fq // step) * step
            if fq > 0:
                self._fill(o, fq, px, taker=True)
            if o["cum"] < o["qty_d"] and (o["tif"] in ("IOC", "FOK") or o["reduceOnly"]):
                o["status"] = "PartiallyFilledCanceled" if o["cum"] > 0 else "Cancelled"

    def _fill(self, o: Dict[str, Any], qty: D, px: D, *, taker: bool, dup: bool = False) -> None:
        sym, cat, side = o["symbol"], o["category"], o["side"]
        qty = min(qty, o["qty_d"] - o["cum"])
        if qty <= 0:
            return
        sgn = 1 if side == "Buy" else -1
        if cat == "linear":
            p = self.positions.setdefault(sym, {"size": D(0), "avg": D(0)})
            p["size"] += sgn * qty
            p["avg"] = px
            if p["size"] == 0:
                p["avg"] = D(0)
            fee, fee_ccy = qty * px * (self.taker_rate if taker else self.maker_rate), "USDT"
            self.wallet["USDT"] -= fee
        else:
            base = self.spot_instruments[sym]["base"]
            if side == "Buy":
                self.wallet["USDT"] -= qty * px
                if self.spot_fee_in_base:
                    fee, fee_ccy = qty * D("0.001"), base
                    self.wallet[base] += qty - fee
                else:
                    fee, fee_ccy = qty * px * self.taker_rate, "USDT"
                    self.wallet[base] += qty
                    self.wallet["USDT"] -= fee
            else:
                self.wallet[base] -= qty
                fee, fee_ccy = qty * px * self.taker_rate, "USDT"
                self.wallet["USDT"] += qty * px - fee
        prev = o["cum"] * o["avg"]
        o["cum"] += qty
        o["avg"] = (prev + qty * px) / o["cum"]
        o["status"] = "Filled" if o["cum"] >= o["qty_d"] else "PartiallyFilled"
        e = {"execId": f"E{len(self.execs) + 1:06d}", "orderId": o["orderId"], "orderLinkId": o["orderLinkId"], "symbol": sym, "category": cat, "side": side, "execQty": _s(qty),
             "execPrice": _s(px), "execFee": _s(fee), "feeCurrency": fee_ccy, "isMaker": not taker, "execTime": str(self.now_ms()), "execType": "Trade"}
        self.execs.append(e)

    def fill(self, link: str, qty: Any, price: Any = None) -> None:
        """Test hook: a resting order trades ``qty`` (as a maker at its own price unless ``price``)."""
        o = self.orders[self.by_link[link]]
        self._fill(o, D(str(qty)), D(str(price if price is not None else o["price"])), taker=False)

    def inject_position(self, symbol: str, size: Any) -> None:
        self.positions[symbol] = {"size": D(str(size)), "avg": D("60000")}

    def settle_funding(self, symbol: str, rate: Any, *, ts_ms: Optional[int] = None, convention: str = "H1", exec_id: Optional[str] = None, mark: Optional[Any] = None,
                       publish_print: bool = True, credit_wallet: bool = True) -> D:
        """The venue settles funding on the CURRENT position: cash = - signed_size * mark * rate (a short receives when the rate is positive). The wallet is credited, a ``Funding`` execution
        row is written and (``publish_print``) the public history lists the print. ``convention`` is the UNVERIFIED sign of ``execFee`` on that row: ``H1`` fee = -cash (the Trade-row convention),
        ``H2`` fee = +cash. Returns the cash."""
        size = self.positions.get(symbol, {"size": D(0)})["size"]
        m = D(str(mark)) if mark is not None else D(self.book[symbol]["mark"])
        r = D(str(rate))
        cash = -size * m * r
        if credit_wallet:
            self.wallet["USDT"] += cash
        ts = int(ts_ms if ts_ms is not None else self.now_ms())
        fee = -cash if convention == "H1" else cash
        self.execs.append({"execId": exec_id or f"F{len(self.execs) + 1:06d}", "orderId": "", "orderLinkId": "", "symbol": symbol, "category": "linear", "side": "Sell" if size < 0 else "Buy",
                           "execQty": _s(abs(size)), "execPrice": _s(m), "execFee": _s(fee), "feeCurrency": "USDT", "isMaker": False, "execTime": str(ts), "execType": "Funding", "markPrice": _s(m)})
        if publish_print:
            self.funding_prints = (self.funding_prints or []) + [(ts, _s(r))]
        return cash

    def force_close(self, symbol: str, qty: Any) -> None:
        """The venue itself closes part of a position (liquidation/ADL): an execution under NO client link id."""
        p = self.positions[symbol]
        q = D(str(qty))
        side = "Sell" if p["size"] > 0 else "Buy"
        p["size"] += -q if p["size"] > 0 else q
        self.execs.append({"execId": f"E{len(self.execs) + 1:06d}", "orderId": "ADL", "orderLinkId": "", "symbol": symbol, "category": "linear", "side": side, "execQty": _s(q),
                           "execPrice": _s(D(self.book[symbol]["mark"])), "execFee": "0", "feeCurrency": "USDT", "isMaker": False, "execTime": str(self.now_ms()), "execType": "AdlTrade"})

    def _cancel(self, d: Dict[str, Any]) -> Dict[str, Any]:
        oid = d.get("orderId") or self.by_link.get(d.get("orderLinkId", ""))
        o = self.orders.get(oid or "")
        if o is None or o["status"] not in ("New", "PartiallyFilled"):
            return self._err(110001, "Order does not exist")
        o["status"] = "PartiallyFilledCanceled" if o["cum"] > 0 else "Cancelled"
        return self._ok({"orderId": o["orderId"], "orderLinkId": o["orderLinkId"]})

    def _order_list(self, path: str, q: Dict[str, str]) -> Dict[str, Any]:
        if q.get("category") not in ("linear", "spot"):
            return self._err(10001, "category")
        rows = [o for o in self.orders.values() if o["category"] == q["category"]]
        if q.get("orderLinkId"):
            rows = [o for o in rows if o["orderLinkId"] == q["orderLinkId"]]
        elif q.get("symbol"):
            rows = [o for o in rows if o["symbol"] == q["symbol"]]
        elif q["category"] == "linear" and not q.get("settleCoin"):
            return self._err(10001, "symbol or settleCoin required")
        if path == "/v5/order/realtime" and not q.get("orderLinkId") and q.get("openOnly", "0") == "0":
            rows = [o for o in rows if o["status"] in ("New", "PartiallyFilled")]
        rows = sorted(rows, key=lambda o: o["orderId"], reverse=True)
        return self._page([self._order_row(o) for o in rows], q, 20)

    def _exec_list(self, q: Dict[str, str]) -> Dict[str, Any]:
        rows = [e for e in self.execs if e["category"] == q.get("category") and e["execId"] not in self.hidden_execs]
        if q.get("orderLinkId"):
            rows = [e for e in rows if e["orderLinkId"] == q["orderLinkId"]]
        elif q.get("symbol"):
            rows = [e for e in rows if e["symbol"] == q["symbol"]]
        rows = list(reversed(rows))
        if self.dup_executions:
            rows = [x for e in rows for x in (e, dict(e))]
        return self._page(rows, q, 50)
