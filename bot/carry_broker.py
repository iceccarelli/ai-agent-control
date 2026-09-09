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


@dataclass
class LegFill:
    filled_qty: float = 0.0
    avg_price: float = 0.0
    order_link_id: str = ""
    was_duplicate: bool = False
    detail: Dict[str, Any] = field(default_factory=dict)

    def as_engine_result(self) -> Dict[str, Any]:
        """The shape `CarryEngine._fire` consumes."""
        return {"filled_qty": self.filled_qty, "avg_price": self.avg_price,
                "order_link_id": self.order_link_id}


class CarryBroker:
    """Places paired legs. Idempotent. Reports incidents rather than papering.

    `client` is a BybitClient-shaped object exposing `_request`, and
    `sequence_source` is a callable returning a persisted, monotonic integer.
    Both are injected so this module can be exercised without a venue.
    """

    def __init__(self, *, client: Any, sequence_source: Any,
                 link_id_builder: Any = None,
                 duplicate_ret_codes: Tuple[int, ...] = (110072, 170130)) -> None:
        self.client = client
        self.sequence_source = sequence_source
        self.duplicate_ret_codes = frozenset(duplicate_ret_codes)
        if link_id_builder is None:
            from bybit_connection import build_order_link_id
            link_id_builder = build_order_link_id
        self._build_link_id = link_id_builder

    # -- the engine's three calls -----------------------------------------

    def place_market(self, *, symbol: str, side: str, qty: float,
                     product: str) -> Optional[Dict[str, Any]]:
        """One leg. Deterministic id, so a retry cannot double it."""
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
            purpose=f"carry-{product}")

        try:
            response = self.client._request(
                "POST", "/v5/order/create", signed=True,
                body={"category": product, "symbol": symbol, "side": side,
                      "orderType": "Market", "qty": qty_text,
                      "orderLinkId": link_id})
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

    def get_margin_multiple(self, symbol: str) -> float:
        """Maintenance-margin headroom on the SHORT leg. Raises if unreadable.

        Raising is deliberate. `CarryEngine._margin_multiple` turns an exception
        into a halt, and an unreadable margin is a margin call you cannot see.
        Returning a comfortable default here would be the single most dangerous
        line in this file.
        """
        result = self.client._request(
            "GET", "/v5/position/list", signed=True,
            params={"category": LINEAR, "symbol": symbol})
        rows = (result or {}).get("list") or []
        if not rows:
            raise PairIncident(
                f"no linear position row for {symbol}; margin headroom unknown")
        row = rows[0]
        margin = float(row.get("positionIM", 0) or 0)
        maintenance = float(row.get("positionMM", 0) or 0)
        if maintenance <= 0:
            raise PairIncident(
                f"maintenance margin for {symbol} is {maintenance}; refusing to "
                "compute headroom from a zero denominator")
        return margin / maintenance

    def get_mark(self, symbol: str) -> float:
        result = self.client._request(
            "GET", "/v5/market/tickers",
            params={"category": LINEAR, "symbol": symbol})
        rows = (result or {}).get("list") or []
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
        rows = (result or {}).get("list") or []
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
        rows = (result or {}).get("list") or []
        if not rows:
            raise PairIncident(f"no ticker for {symbol}; funding unknown")
        raw = rows[0].get("fundingRate")
        if raw is None:
            raise PairIncident(f"ticker for {symbol} carries no fundingRate")
        rate = float(raw)
        if not math.isfinite(rate):
            raise PairIncident(f"funding for {symbol} is {raw!r}")
        return rate * 1e4

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
            rows = (result or {}).get("list") or []
            if not rows:
                return 0.0
            coins = rows[0].get("coin") or []
            return float(coins[0].get("walletBalance", 0) or 0) if coins else 0.0
        result = self.client._request(
            "GET", "/v5/position/list", signed=True,
            params={"category": LINEAR, "symbol": symbol})
        rows = (result or {}).get("list") or []
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
        rows = (result or {}).get("list") or []
        if not rows:
            return None
        row = rows[0]
        return LegFill(
            filled_qty=float(row.get("cumExecQty", 0) or 0),
            avg_price=float(row.get("avgPrice", 0) or 0),
            order_link_id=link_id, detail=dict(row))

    @staticmethod
    def _fill_from(response: Optional[Dict[str, Any]],
                   link_id: str) -> LegFill:
        row = response or {}
        return LegFill(
            filled_qty=float(row.get("cumExecQty", 0) or 0),
            avg_price=float(row.get("avgPrice", 0) or 0),
            order_link_id=str(row.get("orderLinkId", link_id) or link_id),
            detail=dict(row))

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
