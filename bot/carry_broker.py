"""Venue adapter for the carry book: two products, one atomic intent.

WHAT THIS DOES
==============
`CarryEngine` decides. This places. It implements the three calls the engine
needs — `place_market`, `get_margin_multiple`, `get_mark` — against a real
venue, for BOTH products the carry trade uses:

    spot   BTCUSDT   the long leg, your collateral
    linear BTCUSDT   the short leg, the one that can be liquidated

WHY IT IS A SEPARATE MODULE
===========================
`BybitClient` is a single-product order path built for the directional shell.
The carry trade needs a *paired* intent: two orders, two products, one economic
position, where a leg landing alone is not a partial success but an incident.
Bolting that onto the existing client would put pair semantics inside a class
that has no concept of a pair, and the first person to call `place_order`
directly would bypass it.

THE FAILURE THIS MODULE EXISTS TO PREVENT
=========================================
A submit times out. The order actually landed. You retry. Now you hold two
spot legs against one perp short, and you are long BTC at the size you sized
the CARRY at — a size you would never take directionally.

Defence, reusing what `bybit_connection` already got right:

    orderLinkId is DETERMINISTIC. `build_order_link_id` derives it from a
    persisted sequence number plus the order intent, so the retry after a
    timeout carries the SAME id, and the venue rejects the duplicate rather
    than opening a second leg. Bybit answers 110072 (linear) or 170130 (spot).
    Those codes mean THE ORDER EXISTS. They are resolved by querying the id,
    never by recording a rejection and moving on.

The sequence number is per (product, symbol, purpose) so the spot and perp legs
of the same pair never collide on an id while still being individually
replayable.

RECONCILIATION
==============
`reconcile_pair()` asks the venue what it actually holds on both products and
compares against what the ledger believes. Any mismatch beyond dust is an
INCIDENT, not a discrepancy to be smoothed: it means one leg exists that the
other does not hedge. It returns the drift and the direction so the engine can
flatten, and it never silently rewrites the ledger to match the venue — a book
that edits itself to agree with whatever it finds is a book that cannot detect
theft, a bug, or a fat finger.

WHAT THIS MODULE MAY NOT DO
===========================
Decide. There is no funding logic here, no entry rule, no direction. It takes a
symbol, a side, a quantity and a product, and it reports what the venue did.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

#: Quantity differences below this fraction of the leg are venue rounding, not
#: exposure. Above it, the pair is unmatched and that is an incident.
PAIR_DUST_FRACTION = 1e-6

#: Products the carry book touches. Anything else is refused rather than
#: guessed at: routing a spot order to the linear endpoint is a naked short.
SPOT = "spot"
LINEAR = "linear"
VALID_PRODUCTS = frozenset({SPOT, LINEAR})


class PairIncident(RuntimeError):
    """One leg exists that the other does not hedge. Never caught and ignored."""


class CarryOrderRefused(RuntimeError):
    """The process may not send a carry order to this venue. Nothing was sent.

    Raised by place_market BEFORE any request is built. Until 0033 the carry
    broker called `/v5/order/create` whatever PAPER_TRADING said, against
    whichever base URL USE_TESTNET selected (INVENTORY D2).
    """


@dataclass
class LegFill:
    filled_qty: float = 0.0
    avg_price: float = 0.0
    order_link_id: str = ""
    was_duplicate: bool = False
    #: `cumExecFee` as the venue reported it, in the fee currency (BTC on a
    #: spot BUY, USDT on a linear fill). None means the venue did not say. It
    #: is never 0.0 by default: an unknown fee booked as zero is a cost that
    #: disappears from the ledger.
    fee: Optional[float] = None
    detail: Dict[str, Any] = field(default_factory=dict)

    def as_engine_result(self) -> Dict[str, Any]:
        """The shape `CarryEngine._fire` consumes."""
        return {"filled_qty": self.filled_qty, "avg_price": self.avg_price,
                "order_link_id": self.order_link_id, "fee": self.fee}


class CarryBroker:
    """Places paired legs. Idempotent. Reports incidents rather than papering.

    `client` is a BybitClient-shaped object exposing `_request`, and
    `sequence_source` is a callable returning a persisted, monotonic integer.
    Both are injected so this module can be exercised without a venue.

    `order_gate` is REQUIRED: a callable returning `(may_send, reason)`,
    asked before EVERY order. build_bot derives it from PAPER_TRADING,
    USE_TESTNET and the live authorisation. There is no default, because the
    only safe default would be "never", and a broker that can never trade is
    a misconfiguration that should fail at construction, not at 3am.
    """

    def __init__(self, *, client: Any, sequence_source: Any, order_gate: Any,
                 link_id_builder: Any = None,
                 duplicate_ret_codes: Optional[Tuple[int, ...]] = None,
                 session_token: str = "") -> None:
        if not callable(order_gate):
            raise TypeError("order_gate must be callable -> (may_send, reason)")
        self.client = client
        self.sequence_source = sequence_source
        self.order_gate = order_gate
        #: Mixed into every minted orderLinkId so a fresh sequence_source
        #: (seq restarting at 1 after a wiped/new state database) cannot
        #: collide with an id a *different* session already used at the
        #: exchange. See bybit_connection.build_order_link_id and
        #: StateStore.session_boot_token.
        self.session_token = session_token
        #: Venue rules and the account's own fee tier, fetched once per
        #: process. They are facts about the venue and the account, not
        #: decisions, and they change on a timescale of weeks — but they are
        #: NEVER guessed: an unreadable rule raises and the engine refuses.
        self._lot_rules: Dict[Tuple[str, str], Dict[str, float]] = {}
        self._fee_rates: Dict[Tuple[str, str], Dict[str, float]] = {}
        if link_id_builder is None or duplicate_ret_codes is None:
            import bybit_connection as _bybit_connection
        if link_id_builder is None:
            link_id_builder = _bybit_connection.build_order_link_id
        if duplicate_ret_codes is None:
            # Single source of truth: bybit_connection.DUPLICATE_LINK_ID_RET_CODES.
            # This used to be a separate hardcoded tuple here that silently
            # drifted from the one in bybit_connection.py (it was missing
            # retCode 170141, which a real duplicate clientOrderId rejection
            # then fell through as a hard failure instead of being resolved
            # by querying the exchange).
            duplicate_ret_codes = tuple(_bybit_connection.DUPLICATE_LINK_ID_RET_CODES)
        self.duplicate_ret_codes = frozenset(duplicate_ret_codes)
        self._build_link_id = link_id_builder

    # -- the engine's three calls -----------------------------------------

    def place_market(self, *, symbol: str, side: str, qty: float,
                     product: str) -> Optional[Dict[str, Any]]:
        """One leg. Deterministic id, so a retry cannot double it."""
        self._assert_orders_permitted()
        if product not in VALID_PRODUCTS:
            raise ValueError(
                f"unknown product {product!r}; routing a spot order to the "
                "linear endpoint is a naked short, so this is refused rather "
                "than defaulted")
        if not self._positive_finite(qty):
            return None

        qty_text = self._format_qty(qty)
        link_id = self._build_link_id(
            seq=int(self.sequence_source(product, symbol, "carry")),
            symbol=symbol, side=side, qty=qty_text,
            purpose=f"carry-{product}", session_token=self.session_token)

        body = {"category": product, "symbol": symbol, "side": side,
                "orderType": "Market", "qty": qty_text,
                "orderLinkId": link_id}
        if product == SPOT:
            # Without this a spot market BUY is read as a QUOTE (USDT) amount:
            # 0.00125 would buy 0.00125 USDT of BTC. BybitClient has always set
            # it; this adapter built its own body and did not (INVENTORY F1).
            # Stated on sells too, where it is the venue default, so the unit
            # of every spot qty this module sends is explicit.
            body["marketUnit"] = "baseCoin"
        try:
            response = self.client._request(
                "POST", "/v5/order/create", signed=True, body=body)
        except Exception as exc:  # noqa: BLE001
            ret_code = getattr(exc, "ret_code", None)
            if ret_code in self.duplicate_ret_codes:
                # The id already exists at the venue. That means the FIRST
                # attempt landed and its response was lost. Resolving by query
                # is the only correct move: recording a rejection here would
                # leave a real leg the ledger does not know about.
                logger.warning(
                    "duplicate orderLinkId %s (retCode %s) — the first attempt "
                    "landed; resolving by query", link_id, ret_code)
                resolved = self._query_by_link_id(symbol, link_id, product)
                if resolved is not None:
                    resolved.was_duplicate = True
                    return resolved.as_engine_result()
                raise PairIncident(
                    f"orderLinkId {link_id} exists at the venue but could not "
                    "be read back; a leg may be live and unhedged") from exc
            logger.error("leg %s %s %s failed: %s", side, symbol, product, exc)
            return None

        fill = self._fill_from(response, link_id)
        if fill.filled_qty <= 0:
            # Market orders that report nothing filled are queried once rather
            # than assumed dead: the response and the fill are separate events.
            resolved = self._query_by_link_id(symbol, link_id, product)
            if resolved is not None and resolved.filled_qty > 0:
                return resolved.as_engine_result()
            return None
        return fill.as_engine_result()

    # -- resting orders (0040) --------------------------------------------

    def get_book_top(self, symbol: str, product: str) -> Dict[str, float]:
        """`{bid, ask}` — the touch, as the venue publishes it.

        Raises when the ticker carries no touch. The caller then crosses the
        spread: quoting into a price nobody quoted is worse than paying 0.013
        bps of spread.
        """
        if product not in VALID_PRODUCTS:
            raise ValueError(f"unknown product {product!r}")
        result = self.client._request(
            "GET", "/v5/market/tickers",
            params={"category": product, "symbol": symbol})
        rows = self._v5_list(result) or []
        if not rows:
            raise PairIncident(f"no ticker for {symbol} on {product}")
        row = rows[0]
        try:
            bid = float(row["bid1Price"])
            ask = float(row["ask1Price"])
        except (KeyError, TypeError, ValueError) as exc:
            raise PairIncident(
                f"ticker for {symbol}/{product} carries no touch: {row!r}"
            ) from exc
        if not (math.isfinite(bid) and math.isfinite(ask)) or bid <= 0 \
                or ask <= 0:
            raise PairIncident(
                f"touch for {symbol}/{product} is unusable: "
                f"bid {bid!r} ask {ask!r}")
        return {"bid": bid, "ask": ask}

    def place_post_only(self, *, symbol: str, side: str, qty: float,
                        price: float,
                        product: str) -> Optional[Dict[str, Any]]:
        """One PostOnly limit order. Maker or nothing — it never crosses.

        Returns what the order has done so far plus `resting`, or **None**
        when the order does not exist: a PostOnly that would have crossed is
        rejected by the venue, which means the price moved, not that anything
        is broken. The caller crosses instead.

        Raises `PairIncident` when the order's fate cannot be established.
        That is the dangerous case and it is deliberately loud: a resting
        order the book does not know about, plus the market order the caller
        would send next, is two legs where one was intended.
        """
        self._assert_orders_permitted()
        if product not in VALID_PRODUCTS:
            raise ValueError(
                f"unknown product {product!r}; routing a spot order to the "
                "linear endpoint is a naked short, so this is refused rather "
                "than defaulted")
        if not self._positive_finite(qty) or not self._positive_finite(price):
            return None

        qty_text = self._format_qty(qty)
        price_text = self._format_qty(price)
        link_id = self._build_link_id(
            seq=int(self.sequence_source(product, symbol, "carry")),
            symbol=symbol, side=side, qty=qty_text, price=price_text,
            purpose=f"carry-{product}-maker", session_token=self.session_token)

        body = {"category": product, "symbol": symbol, "side": side,
                "orderType": "Limit", "qty": qty_text, "price": price_text,
                "timeInForce": "PostOnly", "orderLinkId": link_id}
        if product == SPOT:
            body["marketUnit"] = "baseCoin"
        try:
            self.client._request("POST", "/v5/order/create", signed=True,
                                 body=body)
        except Exception as exc:  # noqa: BLE001
            # Whether this was a PostOnly rejection, a duplicate, or a lost
            # response, the same question decides what to do: does the order
            # exist? The venue is asked rather than the error code guessed.
            state = self._order_state(symbol, link_id, product)
            if state is None:
                logger.info(
                    "post-only %s %s %s not accepted (%s); crossing instead",
                    side, symbol, product, exc)
                return None
            return state
        state = self._order_state(symbol, link_id, product)
        if state is None:
            raise PairIncident(
                f"post-only {link_id} was accepted and then could not be read "
                "back; whether it rests is unknown")
        return state

    def cancel_order(self, *, symbol: str, order_link_id: str,
                     product: str) -> Dict[str, Any]:
        """Pull a resting order and report its FINAL total fill.

        A cancel that arrives too late is not an error: it means the order
        filled. Either way the answer comes from reading the order back, never
        from the cancel's own response.
        """
        if product not in VALID_PRODUCTS:
            raise ValueError(f"unknown product {product!r}")
        try:
            self.client._request(
                "POST", "/v5/order/cancel", signed=True,
                body={"category": product, "symbol": symbol,
                      "orderLinkId": order_link_id})
        except Exception as exc:  # noqa: BLE001
            logger.info("cancel of %s did not take (%s); reading it back",
                        order_link_id, exc)
        state = self._order_state(symbol, order_link_id, product)
        if state is None:
            raise PairIncident(
                f"{order_link_id} could not be read back after a cancel; what "
                "it filled is unknown")
        return state

    #: Order statuses that mean the order can still fill. Anything else is
    #: terminal: what it filled is what it will ever fill.
    RESTING_STATUSES = frozenset({"New", "Created", "PartiallyFilled",
                                  "Untriggered"})

    def _order_state(self, symbol: str, link_id: str,
                     product: str) -> Optional[Dict[str, Any]]:
        """What the venue says about one order, or None if it has none.

        None means the venue returned no row for this id. For an id the venue
        has just been asked to create, that means the order was rejected and
        does not exist.
        """
        result = self.client._request(
            "GET", "/v5/order/realtime", signed=True,
            params={"category": product, "symbol": symbol,
                    "orderLinkId": link_id})
        rows = self._v5_list(result) or []
        if not rows:
            return None
        row = rows[0]
        status = str(row.get("orderStatus", ""))
        filled = float(row.get("cumExecQty", 0) or 0)
        resting = status in self.RESTING_STATUSES
        if filled <= 0 and not resting:
            # Rejected, or cancelled by the post-only rule. Nothing exists and
            # nothing filled: the caller is free to cross.
            return None
        return {"filled_qty": filled,
                "avg_price": float(row.get("avgPrice", 0) or 0),
                "order_link_id": link_id, "fee": self._fee(row),
                "maker": True, "resting": resting, "status": status}

    def get_margin_multiple(self, symbol: str) -> float:
        """Maintenance-margin headroom on the SHORT leg. Raises if unreadable.

        Raising is deliberate. `CarryEngine._margin_multiple` turns an exception
        into a halt, and an unreadable margin is a margin call you cannot see.
        Returning a comfortable default here would be the single most dangerous
        line in this file.

        This is a ratio on an OPEN position (INVENTORY F4): Bybit returns a
        position/list row with `positionIM=0`/`positionMM=0` even while FLAT
        (proven live, Bybit testnet) - that is the venue correctly saying
        "there is nothing to compute a ratio about", not a wallet fault. This
        function still raises on it, unchanged and on purpose: it has no way
        to know from here whether size is 0 or genuinely stuck at 0 on a live
        position, and guessing "must be flat" would be exactly the comfortable
        default the docstring above refuses. Callers that know the position
        size - `tools/drill.py`'s `margin` stage, `market_snapshot.take_snapshot`
        - check it FIRST and skip calling this at all while flat, so this
        raise is now reached only when a position genuinely exists (or the
        read is broken), which is the case the error message below assumes.
        """
        result = self.client._request(
            "GET", "/v5/position/list", signed=True,
            params={"category": LINEAR, "symbol": symbol})
        rows = self._v5_list(result) or []
        if not rows:
            raise PairIncident(
                f"no linear position row for {symbol}; margin headroom unknown")
        row = rows[0]
        margin = float(row.get("positionIM", 0) or 0)
        maintenance = float(row.get("positionMM", 0) or 0)
        if maintenance <= 0:
            size = row.get("size", "?")
            raise PairIncident(
                f"maintenance margin for {symbol} is {maintenance} (position "
                f"size {size}); refusing to compute headroom from a zero "
                f"denominator. MM=0 at size 0 means flat - callers must check "
                f"position size before calling this, not treat this raise as "
                f"that check. MM=0 at a nonzero size is a genuine anomaly.")
        return margin / maintenance

    def get_mark(self, symbol: str) -> float:
        result = self.client._request(
            "GET", "/v5/market/tickers",
            params={"category": LINEAR, "symbol": symbol})
        rows = self._v5_list(result) or []
        if not rows:
            raise PairIncident(f"no ticker for {symbol}")
        mark = float(rows[0].get("markPrice", 0) or 0)
        if not self._positive_finite(mark):
            raise PairIncident(f"mark for {symbol} is {mark!r}")
        return mark

    def get_spot_mark(self, symbol: str) -> float:
        """Last traded price on the SPOT product. Required to price the basis.

        Raises when unreadable. CarryEngine turns a missing spot into a refusal
        to open, because an unknown basis is not a small basis — and 0018
        measured the basis at -$4,772 on a $42,843 gross, which is not a term
        anyone should be guessing.
        """
        result = self.client._request(
            "GET", "/v5/market/tickers",
            params={"category": SPOT, "symbol": symbol})
        rows = self._v5_list(result) or []
        if not rows:
            raise PairIncident(f"no spot ticker for {symbol}")
        price = float(rows[0].get("lastPrice", 0) or 0)
        if not self._positive_finite(price):
            raise PairIncident(f"spot mark for {symbol} is {price!r}")
        return price

    def get_funding_bps(self, symbol: str) -> float:
        """Current 8h funding on the perp, in basis points.

        This is the only number that decides whether the book opens. It comes
        from the venue's ticker, not from the corpus: the corpus is history and
        the decision is about the next eight hours.

        Raises when unreadable rather than returning zero. Zero would read as
        "funding is thin, stand aside", which is a DECISION taken on absent
        data — the exact class of quiet failure this system refuses.
        """
        result = self.client._request(
            "GET", "/v5/market/tickers",
            params={"category": LINEAR, "symbol": symbol})
        rows = self._v5_list(result) or []
        if not rows:
            raise PairIncident(f"no ticker for {symbol}; funding unknown")
        raw = rows[0].get("fundingRate")
        if raw is None:
            raise PairIncident(f"ticker for {symbol} carries no fundingRate")
        rate = float(raw)
        if not math.isfinite(rate):
            raise PairIncident(f"funding for {symbol} is {raw!r}")
        return rate * 1e4

    def get_lot_rules(self, symbol: str, product: str) -> Dict[str, float]:
        """`{qty_step, min_qty, min_notional}` — what the VENUE will accept.

        Linear BTCUSDT has qtyStep 0.001 and minOrderQty 0.001. The engine
        sized a $100 book at `cap / mark` — 0.00127 BTC at $79k — which is not
        a multiple of the step, so the perp leg would have been REJECTED on the
        first live order and the book would have bought and sold spot for
        nothing (INVENTORY F5). Spot publishes the same three numbers under
        different names, so both are normalised here.

        Raises rather than guessing: a size the venue will not accept is not a
        smaller size, it is a rejected order.
        """
        key = (symbol, product)
        if key in self._lot_rules:
            return dict(self._lot_rules[key])
        result = self.client._request(
            "GET", "/v5/market/instruments-info",
            params={"category": product, "symbol": symbol})
        rows = self._v5_list(result) or []
        if not rows:
            raise PairIncident(
                f"no instrument rules for {symbol} on {product}")
        lot = (rows[0] or {}).get("lotSizeFilter") or {}
        try:
            if product == SPOT:
                rules = {"qty_step": float(lot["basePrecision"]),
                         "min_qty": float(lot["minOrderQty"]),
                         "min_notional": float(lot["minOrderAmt"])}
            else:
                rules = {"qty_step": float(lot["qtyStep"]),
                         "min_qty": float(lot["minOrderQty"]),
                         "min_notional": float(lot.get("minNotionalValue", 0)
                                                or 0)}
        except (KeyError, TypeError, ValueError) as exc:
            raise PairIncident(
                f"instrument rules for {symbol}/{product} are malformed: "
                f"{lot!r}") from exc
        if rules["qty_step"] <= 0 or rules["min_qty"] <= 0:
            raise PairIncident(
                f"instrument rules for {symbol}/{product} are unusable: {rules}")
        self._lot_rules[key] = dict(rules)
        return dict(rules)

    def get_fee_rates(self, symbol: str, product: str) -> Dict[str, float]:
        """`{maker_bps, taker_bps}` for THIS account, from the venue.

        The round trip is the single biggest cost in this book — 0032 measured
        $27,232 of fees against $39,411 of funding — and it is account
        specific: a VIP tier, a referral, a maker rebate all move it. A
        constant in the source would be a cost model that quietly disagrees
        with the invoice. Raises when unreadable; the engine refuses to open
        on a cost it cannot price.
        """
        key = (symbol, product)
        if key in self._fee_rates:
            return dict(self._fee_rates[key])
        result = self.client._request(
            "GET", "/v5/account/fee-rate", signed=True,
            params={"category": product, "symbol": symbol})
        rows = self._v5_list(result) or []
        if not rows:
            raise PairIncident(f"no fee rate for {symbol} on {product}")
        row = rows[0]
        try:
            rates = {"maker_bps": float(row["makerFeeRate"]) * 1e4,
                     "taker_bps": float(row["takerFeeRate"]) * 1e4}
        except (KeyError, TypeError, ValueError) as exc:
            raise PairIncident(
                f"fee rate for {symbol}/{product} is malformed: {row!r}"
            ) from exc
        if not all(math.isfinite(v) and v >= 0 for v in rates.values()):
            raise PairIncident(
                f"fee rate for {symbol}/{product} is unusable: {rates}")
        self._fee_rates[key] = dict(rates)
        return dict(rates)

    def get_spot_inventory(self, symbol: str) -> float:
        """Base coin the account holds and can hedge, in base units.

        `walletBalance - locked`: the locked part is already committed to open
        spot orders and is not yours to hedge. Deliberately NOT
        availableToWithdraw — under a unified account that field goes to zero
        when the coin is posted as collateral, which is exactly the state an
        overlay client is in.

        Raises when the wallet cannot be read. Reporting zero would look
        identical to "the client holds nothing", and the book would stand
        aside forever without saying why.
        """
        coin = symbol[:-4] if symbol.upper().endswith("USDT") else symbol
        result = self.client._request(
            "GET", "/v5/account/wallet-balance", signed=True,
            params={"accountType": "UNIFIED", "coin": coin})
        rows = self._v5_list(result) or []
        coins = (rows[0].get("coin") if rows else None) or []
        for entry in coins:
            if str(entry.get("coin", "")).upper() != coin.upper():
                continue
            try:
                balance = float(entry.get("walletBalance", 0) or 0)
                locked = float(entry.get("locked", 0) or 0)
            except (TypeError, ValueError) as exc:
                raise PairIncident(
                    f"wallet row for {coin} is malformed: {entry!r}") from exc
            free = balance - locked
            if not math.isfinite(free):
                raise PairIncident(f"inventory for {coin} is {free!r}")
            return max(0.0, free)
        raise PairIncident(
            f"no wallet row for {coin}; inventory unknown (an unreadable "
            "inventory is not an empty one)")

    def get_funding_print(self, symbol: str) -> Tuple[float, int]:
        """The last SETTLED funding print: (rate in bps, settlement epoch ms).

        Not the ticker. The ticker's fundingRate is the rate for the NEXT
        settlement — a forecast (INVENTORY F6). What the short leg was paid or
        charged is the settled record, and its stamp is what lets the engine
        tell a new print from the same one read sixty times.

        Raises when unreadable, like every read the book decides on.
        """
        result = self.client._request(
            "GET", "/v5/market/funding/history",
            params={"category": LINEAR, "symbol": symbol, "limit": 1})
        rows = self._v5_list(result) or []
        if not rows:
            raise PairIncident(f"no settled funding print for {symbol}")
        row = rows[0]
        try:
            rate = float(row["fundingRate"])
            stamp = int(row["fundingRateTimestamp"])
        except (KeyError, TypeError, ValueError) as exc:
            raise PairIncident(
                f"settled funding print for {symbol} is malformed: {row!r}"
            ) from exc
        if not math.isfinite(rate) or stamp <= 0:
            raise PairIncident(
                f"settled funding print for {symbol} is unusable: {row!r}")
        return rate * 1e4, stamp

    def get_liquidation_view(self, symbol: str) -> Dict[str, Any]:
        """How far the price can move before this position is liquidated.

        INVENTORY F4: `positionIM / positionMM` is a leverage / risk-tier
        ratio, not headroom. What the venue actually publishes is `liqPrice`
        on the position and `accountMMRate` on the account, and under UTA
        cross margin liquidation is ACCOUNT level, so both are read.

        `liqPrice` is `""` when it lies outside the venue's price bounds.
        That means there is no REACHABLE liquidation price — the safest state
        there is — and it comes back as `None` with a reason rather than as a
        zero, which would read as "liquidation is at zero" or, worse, get
        arithmetic done to it.

        Raises when the position cannot be read at all.
        """
        result = self.client._request(
            "GET", "/v5/position/list", signed=True,
            params={"category": LINEAR, "symbol": symbol})
        rows = self._v5_list(result)
        if rows is None:
            raise PairIncident(
                f"cannot read the {symbol} position; liquidation distance "
                "unknown, and an unreadable one is a margin call you cannot "
                "see")

        account_mm = None
        try:
            wallet = self.client._request(
                "GET", "/v5/account/wallet-balance", signed=True,
                params={"accountType": "UNIFIED"})
            wrows = self._v5_list(wallet) or []
            raw = wrows[0].get("accountMMRate") if wrows else None
            if raw is not None and str(raw).strip() != "":
                value = float(raw)
                account_mm = value if math.isfinite(value) else None
        except Exception:  # noqa: BLE001
            # The per-position number is the primary measure; the account rate
            # is the cross-check. Losing the cross-check is recorded as None,
            # not as zero, and never as "fine".
            account_mm = None

        if not rows:
            return {"size": 0.0, "side": "", "mark": 0.0, "liq_price": None,
                    "distance_pct": None, "account_mm_rate": account_mm,
                    "reason": "NO_POSITION"}

        row = rows[0]
        try:
            size = abs(float(row.get("size", 0) or 0))
            mark = float(row.get("markPrice", 0) or 0)
        except (TypeError, ValueError) as exc:
            raise PairIncident(
                f"position row for {symbol} is malformed: {row!r}") from exc
        side = str(row.get("side", ""))

        # Bybit returns a position/list row even while FLAT - size 0,
        # liqPrice "" (proven live, Bybit testnet) - not only an empty `list`.
        # Without this, that row's empty liqPrice would fall through to
        # BEYOND_VENUE_PRICE_BOUNDS below, which means "a position exists and
        # its liquidation price is unreachable" - a real claim about a
        # position that does not exist. Same reason, same shape as the
        # `not rows` branch above; this just catches the other way Bybit
        # spells "nothing is open".
        if size <= 0:
            return {"size": 0.0, "side": "", "mark": mark, "liq_price": None,
                    "distance_pct": None, "account_mm_rate": account_mm,
                    "reason": "NO_POSITION"}

        raw_liq = row.get("liqPrice")
        if raw_liq is None or str(raw_liq).strip() == "":
            return {"size": size, "side": side, "mark": mark,
                    "liq_price": None, "distance_pct": None,
                    "account_mm_rate": account_mm,
                    "reason": "BEYOND_VENUE_PRICE_BOUNDS"}
        try:
            liq = float(raw_liq)
        except (TypeError, ValueError) as exc:
            raise PairIncident(
                f"liqPrice for {symbol} is {raw_liq!r}") from exc
        if not (math.isfinite(liq) and liq > 0 and mark > 0):
            return {"size": size, "side": side, "mark": mark,
                    "liq_price": None, "distance_pct": None,
                    "account_mm_rate": account_mm,
                    "reason": "LIQ_PRICE_UNUSABLE"}

        # A short is liquidated ABOVE the mark, a long below. Either way the
        # answer is a positive distance to the bad side.
        distance = (100.0 * (liq - mark) / mark if side == "Sell"
                    else 100.0 * (mark - liq) / mark)
        return {"size": size, "side": side, "mark": mark, "liq_price": liq,
                "distance_pct": distance, "account_mm_rate": account_mm,
                "reason": ""}

    def get_funding_history(self, symbol: str,
                            limit: int = 8) -> List[Tuple[float, int]]:
        """The last `limit` SETTLED prints, oldest first, as `(bps, ms)`.

        Same endpoint as `get_funding_print`, more of it. Read once at
        startup so a freshly deployed book is not blind for a day
        (0045): every row is already settled, so this is catching up
        rather than looking ahead.
        """
        result = self.client._request(
            "GET", "/v5/market/funding/history",
            params={"category": LINEAR, "symbol": symbol,
                    "limit": max(1, min(int(limit), 200))})
        rows = self._v5_list(result) or []
        out: List[Tuple[float, int]] = []
        for row in rows:
            try:
                rate = float(row["fundingRate"])
                stamp = int(row["fundingRateTimestamp"])
            except (KeyError, TypeError, ValueError):
                continue
            if math.isfinite(rate) and stamp > 0:
                out.append((rate * 1e4, stamp))
        out.sort(key=lambda entry: entry[1])
        return out

    def get_perp_position(self, symbol: str) -> float:
        """Size of the perp position at the venue, in base units. 0 when flat.

        Read at cold start, before the first tick, and compared with the
        ledger. Raises when unreadable: a restart that cannot see the venue
        must not assume it is flat.
        """
        result = self.client._request(
            "GET", "/v5/position/list", signed=True,
            params={"category": LINEAR, "symbol": symbol})
        rows = self._v5_list(result)
        if rows is None:
            raise PairIncident(
                f"cannot read the {symbol} position; refusing to assume flat")
        if not rows:
            return 0.0
        try:
            return abs(float(rows[0].get("size", 0) or 0))
        except (TypeError, ValueError) as exc:
            raise PairIncident(
                f"position row for {symbol} is malformed: {rows[0]!r}") from exc

    def get_open_carry_orders(self, spot_symbol: str,
                              perp_symbol: str) -> list:
        """Orders that could still fill into this book.

        EVERY open order on the perp symbol counts: that instrument is the
        book's. On spot, only orders this book placed (their link id carries
        the carry purpose) — an overlay client's own spot orders are their
        business, and halting the hedge because they placed one would be the
        book seizing their wallet.
        """
        out = []
        for category, symbol, mine_only in ((LINEAR, perp_symbol, False),
                                            (SPOT, spot_symbol, True)):
            result = self.client._request(
                "GET", "/v5/order/realtime", signed=True,
                params={"category": category, "symbol": symbol, "openOnly": 0})
            rows = self._v5_list(result)
            if rows is None:
                raise PairIncident(
                    f"cannot read open {category} orders for {symbol}; "
                    "refusing to assume there are none")
            for row in rows:
                link = str(row.get("orderLinkId", ""))
                if mine_only and "carr" not in link:
                    continue
                out.append({"orderLinkId": link, "category": category,
                            "symbol": symbol,
                            "orderStatus": row.get("orderStatus")})
        return out

    # -- reconciliation ---------------------------------------------------

    def reconcile_pair(self, *, spot_symbol: str, perp_symbol: str,
                       expected_qty: float) -> Dict[str, Any]:
        """What the venue holds vs what the book believes. Mismatch = incident.

        Never rewrites the ledger to match the venue. A book that edits itself
        to agree with whatever it finds cannot detect a bug, a partial fill or
        an order somebody placed by hand.
        """
        spot_qty = self._holding(spot_symbol, SPOT)
        perp_qty = self._holding(perp_symbol, LINEAR)
        pair_drift = abs(spot_qty - perp_qty)
        scale = max(abs(spot_qty), abs(perp_qty), 1e-12)
        matched = (pair_drift / scale) <= PAIR_DUST_FRACTION

        expected_drift = max(abs(spot_qty - expected_qty),
                             abs(perp_qty - expected_qty))
        agrees_with_book = (expected_drift / max(abs(expected_qty), 1e-12)
                            ) <= PAIR_DUST_FRACTION

        report = {
            "spot_qty": spot_qty, "perp_qty": perp_qty,
            "expected_qty": expected_qty,
            "legs_match_each_other": matched,
            "venue_agrees_with_book": agrees_with_book,
            "pair_drift": pair_drift,
            "naked_side": ("" if matched
                           else ("spot" if spot_qty > perp_qty else "perp")),
            "verdict": ("PAIRED" if matched and agrees_with_book
                        else "INCIDENT"),
        }
        if report["verdict"] == "INCIDENT":
            logger.critical(
                "PAIR INCIDENT: spot %.10g vs perp %.10g (book expected %.10g); "
                "naked on the %s side", spot_qty, perp_qty, expected_qty,
                report["naked_side"] or "unknown")
        return report

    # -- internals --------------------------------------------------------

    def _holding(self, symbol: str, product: str) -> float:
        if product == SPOT:
            result = self.client._request(
                "GET", "/v5/account/wallet-balance", signed=True,
                params={"accountType": "UNIFIED", "coin": symbol[:-4]})
            rows = self._v5_list(result) or []
            if not rows:
                return 0.0
            coins = rows[0].get("coin") or []
            return float(coins[0].get("walletBalance", 0) or 0) if coins else 0.0
        result = self.client._request(
            "GET", "/v5/position/list", signed=True,
            params={"category": LINEAR, "symbol": symbol})
        rows = self._v5_list(result) or []
        return abs(float(rows[0].get("size", 0) or 0)) if rows else 0.0

    def _query_by_link_id(self, symbol: str, link_id: str,
                          product: str) -> Optional[LegFill]:
        try:
            result = self.client._request(
                "GET", "/v5/order/realtime", signed=True,
                params={"category": product, "symbol": symbol,
                        "orderLinkId": link_id})
        except Exception as exc:  # noqa: BLE001
            logger.error("could not read back %s: %s", link_id, exc)
            return None
        rows = self._v5_list(result) or []
        if not rows:
            return None
        row = rows[0]
        return LegFill(
            filled_qty=float(row.get("cumExecQty", 0) or 0),
            avg_price=float(row.get("avgPrice", 0) or 0),
            order_link_id=link_id, fee=CarryBroker._fee(row),
            detail=dict(row))

    # -- Bybit v5 envelope --------------------------------------------------
    #
    # `client._request` returns the WHOLE v5 envelope on retCode 0 -
    # `{retCode, retMsg, result: {...}, ...}` - never just `result` (see its
    # own docstring in bybit_connection.py, and how `BybitClient.get_ticker` /
    # `get_wallet` / `get_open_orders` all unwrap `payload.get("result",
    # {})`). Every reader in this file goes through `_v5_result` or `_v5_list`
    # instead of reading a `_request` return value directly, so there is
    # exactly one unwrap in this module, not a second dialect that quietly
    # reads the envelope's own top level. `get_mark`/`get_spot_mark`/
    # `get_book_top`/`get_funding_bps` used a second, ad-hoc
    # `.get("result", {}).get("list")` before this helper existed (0037);
    # they now go through the same one.

    @staticmethod
    def _v5_result(payload: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        """The `result` object of a Bybit v5 envelope."""
        return (payload or {}).get("result") or {}

    @staticmethod
    def _v5_list(payload: Optional[Dict[str, Any]]) -> Optional[list]:
        """`result.list`, or `None` when the envelope carries no `list` key.

        `None` and `[]` are different answers. `[]` means the venue read
        fine and reported nothing - flat, no open orders, no position row.
        `None` means the response did not carry a `list` at all, which is
        the unreadable case `get_perp_position` / `get_liquidation_view` /
        `get_open_carry_orders` refuse on rather than assume flat or empty.
        `.get("list")` already returns exactly that distinction; nothing
        here coerces a missing key to `[]`.
        """
        return CarryBroker._v5_result(payload).get("list")

    @staticmethod
    def _fee(row: Dict[str, Any]) -> Optional[float]:
        raw = row.get("cumExecFee")
        if raw is None or str(raw).strip() == "":
            return None
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return None
        return value if math.isfinite(value) else None

    @staticmethod
    def _fill_from(response: Optional[Dict[str, Any]],
                   link_id: str) -> LegFill:
        """`response` is `/v5/order/create`'s full envelope; unwrap `result`
        before reading it, same as every other reader in this file.

        Real Bybit create responses carry only `orderId` in `result` - no
        `cumExecQty`/`avgPrice` - so this legitimately returns a zero fill
        most of the time even after the correct unwrap; that is what sends
        `place_market` to `_query_by_link_id` next. FakeClients in the test
        suite simulate an immediate fill for convenience and now do so
        inside a real envelope's `result`, not at its top level.
        """
        row = CarryBroker._v5_result(response)
        return LegFill(
            filled_qty=float(row.get("cumExecQty", 0) or 0),
            avg_price=float(row.get("avgPrice", 0) or 0),
            order_link_id=str(row.get("orderLinkId", link_id) or link_id),
            fee=CarryBroker._fee(row), detail=dict(row))

    def _assert_orders_permitted(self) -> None:
        """Ask the gate. Anything but an explicit (True, reason) refuses."""
        try:
            allowed, reason = self.order_gate()
        except Exception as exc:  # noqa: BLE001 - an unreadable gate is closed
            raise CarryOrderRefused(
                f"order gate unreadable ({exc!r}); no carry order sent") from exc
        if allowed is not True:
            raise CarryOrderRefused(f"{reason}; no carry order sent")

    @staticmethod
    def _format_qty(qty: float) -> str:
        """Fixed-point, never scientific notation. Bybit rejects `1e-05`."""
        return f"{qty:.8f}".rstrip("0").rstrip(".") or "0"

    @staticmethod
    def _positive_finite(value: Any) -> bool:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return False
        return math.isfinite(number) and number > 0
