"""The Bybit TESTNET exchange adapter for ``ms_execution.ExecutionEngine``: ``send_order`` / ``cancel_order`` / ``venue_state`` over a ``BybitClient`` whose transport is the
dedicated ``ms_testnet_transport.TestnetTransport``. It builds NO HTTP of its own: every request is ``client._request`` (signing, role capability, host check, the entry authority
at the final boundary). Nothing here opens a connection or reads the environment; importing it is offline.

Bybit V5 facts relied on (official docs, fetched 2026-11-08) and where this differs from what the repo assumed:
  * orderLinkId <= 36 chars, [A-Za-z0-9_-], UNIQUE: a duplicate create answers retCode 110072 (so a resend is idempotent at the venue; we still query first and never resend blind).
    ``/v5/order/create`` only acknowledges receipt (asynchronous): the state is read back from ``/v5/order/realtime`` and ``/v5/order/history``.
  * ``GET /v5/order/realtime``: for linear one of symbol/baseCoin/settleCoin is required; ``openOnly=0`` (default) returns ONLY open orders, ``openOnly=1`` also the most recent
    <=500 closed orders; the parameter is ignored when ``orderLinkId`` is given (a lookup by link id sees closed orders too). ``limit`` 1-50 with ``nextPageCursor``.
  * ``GET /v5/execution/list``: newest first, ``limit`` 1-100, cursor paged, default window 7 days; fields execId/execQty/execPrice/execFee/feeCurrency/isMaker/execTime/execType.
  * ``GET /v5/position/list`` (linear): symbol or settleCoin required, ``size`` is always positive and ``side`` carries the direction, ``positionIdx`` 0 = one-way, limit <= 200.
  * Spot instruments-info has NO qtyStep / minNotionalValue / pagination: the qty grid is ``lotSizeFilter.basePrecision`` and the minimum value ``minOrderAmt``.
  * DIVERGENCE: a spot MARKET BUY's ``qty`` is QUOTE value by default (base only with ``marketUnit=baseCoin``) while ``entry_authority`` refuses any ``marketUnit`` on an entry, so a
    spot market BUY is refused locally (a spot entry must be a Limit order). A spot market SELL's qty is base coin: allowed as an exit.
  * Spot BUY fees may be charged in the received base coin: such an execution is reported net of the fee (so the engine's quantity equals the venue balance delta).
  * 10006 / 10018 are rate limits, 10002 a timestamp outside recvWindow, 110017 / 110094 / 170130 / 170136 / 170140 / 110007 permanent rejects (the docs' wording of 110017 differs by
    page: it is treated as a permanent reject either way). HTTP 403 is also the US / mainland-China IP block.
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from decimal import Decimal as D
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

BOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (BOT, os.path.join(BOT, "tools")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import bybit_connection as bc  # noqa: E402
import ms_types as T  # noqa: E402
from ms_execution import Ack, OrderRequest  # noqa: E402

CREATE = "/v5/order/create"
CANCEL = "/v5/order/cancel"
MAX_PAGES = 20
QUOTE = "USDT"
CLOSED_STATUSES = ("Filled", "Cancelled", "PartiallyFilledCanceled", "Rejected", "Deactivated")


class AckUnknown(Exception):
    """The fate of an order create is UNKNOWN (no answer, or an answer that is not the venue's). The engine records ACK_LOST and reconciles; nothing is resent here."""


class ExecutorError(Exception):
    """A read the executor needs failed or was not understood (never trade on it)."""


@dataclass(frozen=True)
class Filters:
    category: str
    symbol: str
    status: str
    base_coin: str
    tick: D
    qty_step: D
    min_qty: D
    max_qty: D
    min_notional: D
    funding_interval_min: Optional[int]


def _ms(x: Any) -> Optional[int]:
    """A venue millisecond timestamp (a string of digits) as an int; anything else is None (UNKNOWN), never zero."""
    try:
        v = int(str(x))
    except (TypeError, ValueError):
        return None
    return v if v > 0 else None


def _d(x: Any, what: str) -> D:
    try:
        v = D(str(x))
    except Exception:                                                  # noqa: BLE001
        raise ExecutorError(f"{what} is not a number") from None
    if not v.is_finite():
        raise ExecutorError(f"{what} is not finite")
    return v


def fmt(x: D) -> str:
    return format(x.normalize(), "f") if x != 0 else "0"


def parse_filters(category: str, row: Mapping[str, Any]) -> Filters:
    try:
        lot, pf = row["lotSizeFilter"], row["priceFilter"]
        if category == "linear":
            step, minq = _d(lot["qtyStep"], "qtyStep"), _d(lot["minOrderQty"], "minOrderQty")
            maxq, minn = _d(lot.get("maxOrderQty", "0"), "maxOrderQty"), _d(lot.get("minNotionalValue", "0"), "minNotionalValue")
            fi = row.get("fundingInterval")
            fi = int(fi) if fi not in (None, "") else None
        else:                                                           # spot: no qtyStep / minNotionalValue (docs): basePrecision is the grid, minOrderAmt the value floor
            step, minq = _d(lot["basePrecision"], "basePrecision"), D(0)
            maxq, minn, fi = _d(lot.get("maxLimitOrderQty", "0"), "maxLimitOrderQty"), _d(lot.get("minOrderAmt", "0"), "minOrderAmt"), None
        tick = _d(pf["tickSize"], "tickSize")
        if step <= 0 or tick <= 0:
            raise ExecutorError("non-positive step/tick")
        return Filters(category, str(row["symbol"]), str(row.get("status", "")), str(row.get("baseCoin", "")), tick, step, minq, maxq, minn, fi)
    except (KeyError, TypeError, ValueError) as exc:
        raise ExecutorError(f"instrument row not understood: {type(exc).__name__}") from None


def filter_violations(f: Filters, *, side: str, order_type: str, qty: str, price: Optional[str], ref_price: Optional[D]) -> List[str]:
    """Every way an order breaks the instrument's rules (empty = it may be sent). Checked BEFORE sending."""
    out: List[str] = []
    if f.status != "Trading":
        out.append(f"INSTRUMENT_NOT_TRADING:{f.status}")
    try:
        q = D(qty)
    except Exception:                                                  # noqa: BLE001
        return out + ["QTY_NOT_A_NUMBER"]
    if q <= 0:
        out.append("QTY_NOT_POSITIVE")
    elif q % f.qty_step != 0:
        out.append("QTY_NOT_ON_STEP")
    if q < f.min_qty:
        out.append("QTY_BELOW_MIN")
    if f.max_qty > 0 and q > f.max_qty:
        out.append("QTY_ABOVE_MAX")
    px: Optional[D] = None
    if order_type == "Limit":
        try:
            px = D(price) if price is not None else None
        except Exception:                                              # noqa: BLE001
            px = None
        if px is None or px <= 0:
            out.append("PRICE_INVALID")
        elif px % f.tick != 0:
            out.append("PRICE_NOT_ON_TICK")
    use = px if px is not None else ref_price
    if f.min_notional > 0:
        if use is None:
            out.append("NOTIONAL_UNVERIFIABLE")
        elif q * use < f.min_notional:
            out.append("NOTIONAL_BELOW_MIN")
    if f.category == "spot" and order_type == "Market" and side == "Buy":
        out.append("SPOT_MARKET_BUY_UNIT_AMBIGUOUS")
    return out


def parse_wallet(res: Any) -> Tuple[D, Dict[str, D]]:
    """(totalEquity, {coin: walletBalance}) from a ``/v5/account/wallet-balance`` result, or ``ExecutorError``. An empty list, a missing ``coin`` list, a non-numeric figure or a
    non-object row is MALFORMED, never 'no balance' (an empty answer must not read as a flat spot position)."""
    try:
        accts = res.get("list") if isinstance(res, Mapping) else None
        if not isinstance(accts, list) or not accts or not isinstance(accts[0], Mapping):
            raise ExecutorError("wallet answer has no account row")
        acct = accts[0]
        rows = acct["coin"]
        if not isinstance(rows, list) or any(not isinstance(c, Mapping) for c in rows):
            raise ExecutorError("wallet coin list malformed")
        return _d(acct["totalEquity"], "totalEquity"), {str(c["coin"]): _d(c["walletBalance"], "walletBalance") for c in rows}
    except (KeyError, TypeError, IndexError):
        raise ExecutorError("wallet answer malformed") from None


def _wallet_usd_values(res: Any) -> Dict[str, D]:
    """{coin: usdValue} from a wallet-balance result (the venue's own valuation, the basis of totalEquity); a coin without a usable figure is simply absent."""
    out: Dict[str, D] = {}
    try:
        for c in res["list"][0]["coin"]:
            v = c.get("usdValue")
            if v not in (None, ""):
                out[str(c["coin"])] = D(str(v))
    except (KeyError, TypeError, IndexError, ArithmeticError, ValueError):
        return {}
    return out


class TestnetExecutor:
    __test__ = False

    def __init__(self, client: Any, *, symbols: List[str], spot_symbols: Optional[List[str]] = None, clock_ms: Callable[[], int],
                 kill_reader: Optional[Callable[[], Tuple[bool, str]]] = None, read_retries: int = 2) -> None:
        if getattr(client, "venue", None) != "testnet" or not getattr(client, "testnet", False):
            raise ExecutorError("client is not the testnet venue")
        self.client = client
        self.symbols = list(symbols)
        self.spot_symbols = list(spot_symbols if spot_symbols is not None else symbols)
        self.clock = clock_ms
        self._kill = kill_reader
        self.read_retries = read_retries
        self.halted: Optional[str] = None
        self._filters: Dict[Tuple[str, str], Filters] = {}
        self.sent_creates: List[str] = []                              # every link id that left for /v5/order/create (a resend would repeat one)
        self.unattributed: List[Dict[str, Any]] = []
        self._read_unattributed: List[Dict[str, Any]] = []
        self.foreign_open_orders: List[str] = []
        self.last_view_ms: Optional[int] = None
        self._seen_exec: set = set()
        #: MEASUREMENTS per order link id (local clock): what we saw and when. Pure observation: nothing here feeds a decision, a gate or a retry.
        self.meas: Dict[str, Dict[str, Any]] = {}
        self._quotes: Dict[Tuple[str, str], Dict[str, Any]] = {}      # (category, symbol) -> the latest ticker quote this executor read

    # ---------------------------------------------------------------- reads
    def _get(self, path: str, params: Mapping[str, Any], *, signed: bool) -> Mapping[str, Any]:
        try:
            payload = self.client._request("GET", path, params=dict(params), signed=signed, retries=self.read_retries)
        except bc.BybitAPIError as exc:
            raise ExecutorError(f"read {path} failed: {type(exc).__name__} retCode={exc.ret_code}") from None
        res = payload.get("result") if isinstance(payload, Mapping) else None
        if not isinstance(res, Mapping):
            raise ExecutorError(f"read {path}: no result object")
        return res

    def _paged(self, path: str, params: Mapping[str, Any], limit: int) -> List[Mapping[str, Any]]:
        rows: List[Mapping[str, Any]] = []
        cursor = ""
        for _ in range(MAX_PAGES):
            p = dict(params, limit=limit)
            if cursor:
                p["cursor"] = cursor
            res = self._get(path, p, signed=True)
            lst = res.get("list")
            if not isinstance(lst, list) or any(not isinstance(r, Mapping) for r in lst):
                raise ExecutorError(f"read {path}: list malformed")
            rows += lst
            cursor = str(res.get("nextPageCursor") or "")
            if not cursor:
                return rows
        raise ExecutorError(f"read {path}: still paging after {MAX_PAGES} pages (incomplete)")

    def filters(self, category: str, symbol: str, *, refresh: bool = False) -> Filters:
        k = (category, symbol)
        if refresh or k not in self._filters:
            res = self._get("/v5/market/instruments-info", {"category": category, "symbol": symbol}, signed=False)
            rows = res.get("list")
            if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], Mapping) or rows[0].get("symbol") != symbol:
                raise ExecutorError(f"instruments-info for {category}:{symbol} not understood")
            self._filters[k] = parse_filters(category, rows[0])
        return self._filters[k]

    def qty_steps(self) -> Dict[str, str]:
        out: Dict[str, str] = {}
        for s in self.symbols:
            out[f"linear:{s}"] = fmt(self.filters("linear", s).qty_step)
        for s in self.spot_symbols:
            out[f"spot:{s}"] = fmt(self.filters("spot", s).qty_step)
        return out

    # ---------------------------------------------------------------- orders
    def halt(self, reason: str) -> None:
        """No NEW exposure leaves after this (reduce-only exits and cancels still do)."""
        self.halted = self.halted or reason

    def unhalt(self, reason: str) -> bool:
        """Lift ONE halt reason (only the reason that was raised; another stays)."""
        if self.halted == reason:
            self.halted = None
            return True
        return False

    @staticmethod
    def _is_exit(leg: T.LegPlan) -> bool:
        return bool(leg.reduce_only) or (leg.category == "spot" and leg.side == "Sell")

    def body_for(self, leg: T.LegPlan, link: str) -> Dict[str, Any]:
        b: Dict[str, Any] = {"category": leg.category, "symbol": leg.symbol, "side": leg.side, "orderType": leg.order_type, "qty": leg.qty, "orderLinkId": link}
        if leg.order_type == "Limit":
            b["price"] = leg.price
            b["timeInForce"] = "PostOnly" if leg.post_only else leg.time_in_force
        elif leg.time_in_force == "IOC":
            b["timeInForce"] = "IOC"                                       # docs: a market order is always IOC; stated only when the plan says so
        if leg.reduce_only:
            b["reduceOnly"] = True
        return b

    def send_order(self, req: OrderRequest) -> Ack:
        leg, link = req.leg, req.link_id
        if link != f"{req.group_id}-{leg.leg_id}":
            return Ack(link, "REJECTED", None, "LINK_ID_NOT_GROUP_LEG")
        exit_ = self._is_exit(leg)
        if not exit_:
            if self.halted:
                return Ack(link, "REJECTED", None, f"EXECUTOR_HALTED:{self.halted}")
            if self._kill is not None:
                try:
                    engaged, why = self._kill()
                except Exception:                                      # noqa: BLE001
                    engaged, why = True, "unreadable"
                if engaged:
                    self.halt("KILL_SWITCH")
                    return Ack(link, "REJECTED", None, f"KILL_SWITCH:{why}")
        try:
            f = self.filters(leg.category, leg.symbol)
            ref = None
            if leg.order_type == "Market" or leg.price is None:
                ref = self._ref_price(leg)
            elif not exit_:
                self._quote_row(leg.category, leg.symbol)                 # a public read for the MEASUREMENT only (entries only: an exit is never made slower)
            bad = filter_violations(f, side=leg.side, order_type=leg.order_type, qty=leg.qty, price=leg.price, ref_price=ref)
        except ExecutorError as exc:
            return Ack(link, "REJECTED", None, f"FILTERS_UNAVAILABLE:{exc}"[:160])        # nothing was sent
        if bad:
            return Ack(link, "REJECTED", None, "FILTER:" + ",".join(bad))
        body = self.body_for(leg, link)
        m = self.meas.setdefault(link, {})
        m["reference"] = self._reference_for(leg, int(self.clock()), fresh=not exit_)
        try:
            self.sent_creates.append(link)
            m["sent_ms"] = int(self.clock())                           # immediately before the create request leaves
            payload = self.client._request("POST", CREATE, body=body, signed=True, retries=0)
            m["acked_ms"] = int(self.clock())
        except bc.OrderNotSent as exc:                                 # refused LOCALLY (authority / capability): definitely not at the venue
            self.sent_creates.pop()
            m.pop("sent_ms", None)
            return Ack(link, "REJECTED", None, ("NOT_SENT:" + str(exc))[:200])
        except bc.PermanentAPIError as exc:
            code = exc.ret_code
            if code == 110072:                                         # duplicate orderLinkId: the order EXISTS
                return self._resolve(req)
            if code in bc.UNKNOWN_OUTCOME_RET_CODES:                   # 0 = no retCode stated (HTTP error body, garbage, proxy), 10000/170007 = timeouts
                return self._resolve(req)
            m["acked_ms"] = int(self.clock())                          # a definitive venue refusal IS an answer: its arrival time is a real timestamp
            return Ack(link, "REJECTED", None, f"RET:{code}:{str(exc)[:80]}")
        except bc.TransientAPIError:                                   # timeout / 5xx / rate limit / clock window: the order MAY exist
            return self._resolve(req)
        res = payload.get("result") if isinstance(payload, Mapping) else None
        oid = res.get("orderId") if isinstance(res, Mapping) else None
        if not isinstance(oid, str) or not oid or (res.get("orderLinkId") not in (None, "", link)):
            return self._resolve(req)                                  # an acknowledgement that is not an acknowledgement of THIS order
        return Ack(link, "ACCEPTED", oid)

    def _ref_price(self, leg: T.LegPlan) -> Optional[D]:
        row = self._quote_row(leg.category, leg.symbol)
        if row is None:
            return None
        try:
            return D(str(row["lastPrice"]))
        except (KeyError, TypeError, ValueError, ArithmeticError):
            return None

    def _quote_row(self, category: str, symbol: str) -> Optional[Mapping[str, Any]]:
        """One public ticker read; remembers the touch (bid1/ask1) and WHEN it was read, so an order's reference quote can be recorded. Never raises."""
        try:
            r = self._get("/v5/market/tickers", {"category": category, "symbol": symbol}, signed=False)
            row = r["list"][0]
            self._quotes[(category, symbol)] = {"bid": row.get("bid1Price"), "ask": row.get("ask1Price"), "ts_ms": int(self.clock()), "source": "TICKER_TOUCH"}
            return row
        except (ExecutorError, KeyError, IndexError, TypeError, ValueError, ArithmeticError):
            return None

    def _reference_for(self, leg: T.LegPlan, now_ms: int, fresh: bool) -> Optional[Dict[str, Any]]:
        """The quote to record against this order: for an ENTRY the ticker read just made (age 0); for an EXIT (which must never be slowed by a read) the latest quote already held, with its
        age, or None. The record states which; an unknown reference stays unknown."""
        q = self._quotes.get((leg.category, leg.symbol))
        if q is None or not q.get("bid") or not q.get("ask"):
            return None
        return {**q, "age_ms_at_send": max(0, now_ms - int(q["ts_ms"])), "source": q["source"] if fresh else "LATEST_HELD_QUOTE"}

    def lookup(self, category: str, symbol: str, link: str) -> Optional[Mapping[str, Any]]:
        """The order under ``link`` (open or closed) or None when the venue answers 'no such order'. Raises ``ExecutorError`` when the venue cannot be read."""
        for path in ("/v5/order/realtime", "/v5/order/history"):
            res = self._get(path, {"category": category, "symbol": symbol, "orderLinkId": link}, signed=True)
            lst = res.get("list")
            if not isinstance(lst, list):
                raise ExecutorError("order list malformed")
            rows = [r for r in lst if isinstance(r, Mapping) and r.get("orderLinkId") == link]
            if rows:
                return rows[0]
        return None

    def _resolve(self, req: OrderRequest) -> Ack:
        """A lost / unreadable acknowledgement: ask the venue under the SAME link id. Found => adopt. Not found => the fate is still unknown (visibility lag): raise, the engine
        journals ACK_LOST and its reconcile (a COMPLETE read, then ONE idempotent resend) decides. This method never sends a create."""
        try:
            row = self.lookup(req.leg.category, req.leg.symbol, req.link_id)
        except ExecutorError as exc:
            raise AckUnknown(str(exc)) from None
        if row is None:
            raise AckUnknown("no order under the link id yet")
        if row.get("orderStatus") == "Rejected":
            return Ack(req.link_id, "REJECTED", None, "VENUE_REJECTED")
        oid = row.get("orderId")
        if not isinstance(oid, str) or not oid:
            raise AckUnknown("order row has no orderId")
        return Ack(req.link_id, "ACCEPTED", oid, "ADOPTED_BY_LOOKUP")

    def cancel_order(self, req: OrderRequest) -> str:
        body = {"category": req.leg.category, "symbol": req.leg.symbol, "orderLinkId": req.link_id}
        m = self.meas.setdefault(req.link_id, {})
        m["cancel_sent_ms"] = int(self.clock())
        try:
            self.client._request("POST", CANCEL, body=body, signed=True, retries=0)
            m["cancel_acked_ms"], m["cancel_result"] = int(self.clock()), "CANCELLED"
            return "CANCELLED"
        except bc.OrderNotSent:
            raise
        except bc.PermanentAPIError as exc:
            if exc.ret_code in bc.ORDER_NOT_FOUND_RET_CODES:           # racing a fill / an earlier cancel: ask what the order became
                try:
                    row = self.lookup(req.leg.category, req.leg.symbol, req.link_id)
                except ExecutorError:
                    raise
                m["cancel_acked_ms"] = int(self.clock())
                if row is None:
                    m["cancel_result"] = "NOT_FOUND"
                    return "NOT_FOUND"
                st = row.get("orderStatus")
                res = "TOO_LATE" if st == "Filled" else "CANCELLED" if st in CLOSED_STATUSES else "TOO_LATE"
                m["cancel_result"] = res
                return res
            raise
        except bc.TransientAPIError:
            raise

    def set_leverage(self, symbol: str, leverage: int) -> bool:
        """Set one-way leverage (bounded by the authority's policy at the client). 110043 'not modified' is success."""
        try:
            self.client._request("POST", "/v5/position/set-leverage", body={"category": "linear", "symbol": symbol, "buyLeverage": str(leverage), "sellLeverage": str(leverage)},
                                 signed=True, retries=0)
            return True
        except bc.PermanentAPIError as exc:
            return exc.ret_code == 110043

    # ---------------------------------------------------------------- venue state
    def venue_state(self) -> Dict[str, Any]:
        """{'orders','executions','positions','complete', ...} for ``ExecutionEngine``. Never raises. ``positions`` is None when the position read failed or is not one-way;
        ``complete`` is False when ANY read failed, was malformed or was still paging."""
        complete = True
        positions: Optional[Dict[str, float]] = {}
        orders: Dict[str, Dict[str, Any]] = {}
        execs: List[Dict[str, Any]] = []
        self.foreign_open_orders = []
        self._read_unattributed = []
        try:
            prow = self._paged("/v5/position/list", {"category": "linear", "settleCoin": QUOTE}, 200)
            assert positions is not None
            for r in prow:
                size, side, idx = _d(r["size"], "size"), r.get("side", ""), r.get("positionIdx")
                if idx != 0:                                           # docs: positionIdx 0 = one-way; 1/2 = hedge mode (the authority refuses positionIdx != 0)
                    raise ExecutorError("hedge-mode position")
                if size != 0 and side not in ("Buy", "Sell"):
                    raise ExecutorError("position side unreadable")
                positions[f"linear:{r['symbol']}"] = float(size if side == "Buy" else -size)
        except (ExecutorError, KeyError, TypeError):
            positions, complete = None, False
        try:
            if positions is not None and self.spot_symbols:
                coins = {self.filters("spot", s).base_coin for s in self.spot_symbols}
                res = self._get("/v5/account/wallet-balance", {"accountType": "UNIFIED", "coin": ",".join(sorted(coins))}, signed=True)
                _eq, have = parse_wallet(res)
                for s in self.spot_symbols:
                    base = self.filters("spot", s).base_coin
                    if base not in have:                                   # an asked-for coin the venue did not answer for is UNKNOWN, not zero
                        raise ExecutorError(f"wallet answer has no row for {base}")
                    positions[f"spot:{s}"] = float(have[base])
        except (ExecutorError, KeyError, TypeError):
            positions, complete = None, False
        self._fee_in_qty: Dict[str, D] = {}
        for cat, syms in (("linear", self.symbols), ("spot", self.spot_symbols)):
            for s in syms:
                try:
                    for r in self._paged("/v5/order/realtime", {"category": cat, "symbol": s, "openOnly": 1}, 50):
                        self._take_order(r, cat, orders)
                    for r in self._paged("/v5/execution/list", {"category": cat, "symbol": s}, 100):
                        self._take_exec(r, cat, execs)
                except (ExecutorError, KeyError, TypeError, ValueError):
                    complete = False
        for link, fee_qty in self._fee_in_qty.items():                  # the order's cumExecQty is GROSS: keep it consistent with the net executions
            if link in orders:
                orders[link]["filled_qty"] = float(D(str(orders[link]["filled_qty"])) - fee_qty)
                if orders[link]["status"] == "Filled":                  # complete at the venue, but net of its fee it is a hair below the planned quantity: terminal, not 'still working'
                    orders[link]["status"] = "PartiallyFilledCanceled"
        if complete:
            self.last_view_ms = int(self.clock())
        return {"orders": orders, "executions": execs, "positions": positions, "complete": complete, "read_ms": int(self.clock()),
                "foreign_open_orders": list(self.foreign_open_orders), "unattributed": list(self._read_unattributed)}

    def _take_order(self, r: Mapping[str, Any], cat: str, out: Dict[str, Dict[str, Any]]) -> None:
        link, st = r.get("orderLinkId"), r.get("orderStatus")
        if not isinstance(link, str) or not link:
            if st in ("New", "PartiallyFilled", "Untriggered", "Created"):
                self.foreign_open_orders.append(str(r.get("orderId", "?")))      # an open order that is not ours
            return
        avg = r.get("avgPrice") or "0"
        out[link] = {"order_id": r["orderId"], "status": st, "filled_qty": float(_d(r.get("cumExecQty") or "0", "cumExecQty")), "avg_price": float(_d(avg, "avgPrice")),
                     "symbol": r.get("symbol"), "category": cat, "side": r.get("side"), "qty": r.get("qty"),
                     "order_type": r.get("orderType"), "tif": r.get("timeInForce"), "created_ms": _ms(r.get("createdTime")), "updated_ms": _ms(r.get("updatedTime")),
                     "cum_fee": r.get("cumExecFee")}

    def _take_exec(self, r: Mapping[str, Any], cat: str, out: List[Dict[str, Any]]) -> None:
        eid = r["execId"]
        if eid in self._seen_exec or any(x["fill_id"] == eid for x in out):
            return                                                     # duplicate execution event: dropped by execId
        typ, link = r.get("execType", "Trade"), r.get("orderLinkId") or ""
        qty, px, fee = _d(r["execQty"], "execQty"), _d(r["execPrice"], "execPrice"), _d(r.get("execFee") or "0", "execFee")
        side, sym = r["side"], r["symbol"]
        if typ != "Trade" or not link:                                  # funding, ADL, bust, settle, or a trade under no client link id: SURFACED, never booked as our fill
            row = {"exec_id": eid, "type": typ, "symbol": sym, "fee": str(fee)}
            self._read_unattributed.append(row)
            if not any(u["exec_id"] == eid for u in self.unattributed):
                self.unattributed.append(row)
            return
        fee_ccy = r.get("feeCurrency") or QUOTE
        fee_signed = fee
        if cat == "spot" and fee_ccy != QUOTE:                           # fee taken from the received base coin: net quantity, fee in USD at the fill price
            if side == "Buy":
                qty = qty - fee
                self._fee_in_qty[link] = self._fee_in_qty.get(link, D(0)) + fee
            fee = fee * px
            fee_signed = fee
        out.append({"fill_id": eid, "link_id": link, "qty": float(qty), "price": float(px), "fee_usd": float(max(fee, D(0))), "fee_ccy": QUOTE,
                    "liquidity": "MAKER" if r.get("isMaker") in (True, "true") else "TAKER", "ts_ms": int(r["execTime"]), "symbol": sym, "category": cat, "side": side,
                    "prov": "MEASURED", "fee_signed_usd": float(fee_signed), "fee_rate": r.get("feeRate") if r.get("feeRate") not in (None, "") else None,
                    "fee_ccy_venue": fee_ccy, "is_maker_reported": r.get("isMaker") is not None})

    # ---------------------------------------------------------------- account / market reads for the authority
    def account_state(self, baseline: Optional[Mapping[str, float]] = None, open_groups: int = 0) -> Dict[str, Any]:
        """The ``ms_risk`` account state from FRESH reads (raises ``ExecutorError`` if any read fails: no guess). ``baseline`` = spot balances the client held before the session."""
        view_pos: Dict[str, Any] = {}
        prow = self._paged("/v5/position/list", {"category": "linear", "settleCoin": QUOTE}, 200)
        marks = {s: self.mark(s) for s in self.symbols}
        for r in prow:
            size, side = _d(r["size"], "size"), r.get("side", "")
            if size == 0:
                continue
            sym = str(r["symbol"])
            m = marks.get(sym)
            if m is None or side not in ("Buy", "Sell"):
                raise ExecutorError(f"position {sym} cannot be valued")
            view_pos[sym] = {"category": "linear", "notional_usd": float(size * m * (1 if side == "Buy" else -1)), "qty": float(size)}
        w = self._get("/v5/account/wallet-balance", {"accountType": "UNIFIED"}, signed=True)
        eq, coins = parse_wallet(w)
        equity = float(eq)
        # THE LOSS MEASURE IS THE SESSION'S, NOT THE OWNER'S WHOLE ACCOUNT: spot the client held BEFORE the session (``baseline``) is valued at the wallet read's own implied price
        # (coin usdValue / walletBalance, the same basis as totalEquity) and taken out, so its price swings cancel exactly. Without this a $10 move in BTC on a 0.1 BTC holding
        # (about $11 would be enough on the real account) trips the 1.11 loss limit before any order exists. A real loss (fees, slippage, a fill below the unwind price) still shows in full.
        usd_by_coin = _wallet_usd_values(w)
        for s in self.spot_symbols:
            f = self.filters("spot", s)
            b_qty = D(str((baseline or {}).get(f"spot:{s}", 0)))
            if b_qty > 0:
                bal = coins.get(f.base_coin, D(0))
                usd = usd_by_coin.get(f.base_coin)
                px = (usd / bal) if (usd is not None and bal > 0 and usd > 0) else self.spot_mark(s)
                equity -= float(b_qty * px)
        for s in self.spot_symbols:
            f = self.filters("spot", s)
            held = coins.get(f.base_coin, D(0)) - D(str((baseline or {}).get(f"spot:{s}", 0)))
            if held > 0:
                sm = self.spot_mark(s)
                view_pos["SPOT:" + s] = {"category": "spot", "notional_usd": float(held * sm), "qty": float(held)}
        now = int(self.clock())
        self.last_view_ms = now
        return {"equity_usd": equity, "positions": view_pos, "open_groups": int(open_groups), "account_ts_ms": now, "market_ts_ms": now}

    def mark(self, symbol: str) -> D:
        r = self._get("/v5/market/tickers", {"category": "linear", "symbol": symbol}, signed=False)
        row = r["list"][0]
        return _d(row.get("markPrice") or row["lastPrice"], "markPrice")

    def spot_mark(self, symbol: str) -> D:
        r = self._get("/v5/market/tickers", {"category": "spot", "symbol": symbol}, signed=False)
        return _d(r["list"][0]["lastPrice"], "lastPrice")

    def snapshot(self, symbol: str) -> T.MarketSnapshot:
        """A MarketSnapshot from fresh public reads: mark, spot mark, the top of the perp book and its age (``now - book ts`` in the venue's clock offset to ours)."""
        now = int(self.clock())
        t = self._get("/v5/market/tickers", {"category": "linear", "symbol": symbol}, signed=False)["list"][0]
        ob = self._get("/v5/market/orderbook", {"category": "linear", "symbol": symbol, "limit": 5}, signed=False)
        bids = tuple(T.BookLevel(float(p), float(q)) for p, q in ob.get("b", []))
        asks = tuple(T.BookLevel(float(p), float(q)) for p, q in ob.get("a", []))
        ts = ob.get("ts")
        age = max(0, now - int(ts)) if isinstance(ts, int) else None
        fr = t.get("fundingRate")
        return T.MarketSnapshot(symbol, now, mark=float(t.get("markPrice") or t["lastPrice"]), index=float(t["indexPrice"]) if t.get("indexPrice") else None, bids=bids, asks=asks,
                                book_age_ms=age, last_funding_bps_per_print=float(fr) * 1e4 if fr not in (None, "") else None,
                                funding_interval_min=self.filters("linear", symbol).funding_interval_min, spot_mark=float(self.spot_mark(symbol)))
