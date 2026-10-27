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
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import venue_fees

logger = logging.getLogger(__name__)

#: Quantity differences below this fraction of the leg are venue rounding, not
#: exposure. Above it, the pair is unmatched and that is an incident.
PAIR_DUST_FRACTION = 1e-6

#: Products the carry book touches. Anything else is refused rather than
#: guessed at: routing a spot order to the linear endpoint is a naked short.
SPOT = "spot"
LINEAR = "linear"

#: retCodes that report a backend timeout rather than a refusal (10000 server
#: timeout, 170007 timeout waiting for the backend). The order may be live.
_TIMEOUT_RET_CODES = frozenset({10000, 170007})

#: The three values Bybit's `GET /v5/account/info` states in `marginMode`. The
#: account's own statement is the only source of the mode: nothing in this
#: project configures it, and an absent or other value is NEVER read as one of
#: these (`CarryBroker.get_margin_mode` raises instead).
MARGIN_REGULAR = "REGULAR_MARGIN"
MARGIN_PORTFOLIO = "PORTFOLIO_MARGIN"
MARGIN_ISOLATED = "ISOLATED_MARGIN"
MARGIN_MODES = (MARGIN_REGULAR, MARGIN_PORTFOLIO, MARGIN_ISOLATED)
#: Modes where liquidation is decided at ACCOUNT level, so a valid
#: `accountMMRate` is required. Isolated margin is per position: the account
#: rate does not describe it and the per-position liqPrice distance is the gate.
ACCOUNT_LEVEL_MARGIN_MODES = (MARGIN_REGULAR, MARGIN_PORTFOLIO)
VALID_PRODUCTS = frozenset({SPOT, LINEAR})


class PairIncident(RuntimeError):
    """One leg exists that the other does not hedge. Never caught and ignored."""


class OrderOutcomeUnknown(PairIncident):
    """An order was (or may have been) sent and its fate cannot be established.

    The engine treats this as a HALT, never as "the leg did not fill": a
    timeout after submission is exactly when an order exists at the venue that
    the book has no record of. ``outcome_unknown`` is what the engine tests, so
    it needs no import of this module.
    """

    outcome_unknown = True


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
    #: The structured fee (`venue_fees.FeeSet.to_dict()`): every currency the
    #: venue named, the field that said so, and whether it said anything.
    #: `fee` above is only its single-currency total for legacy callers; the
    #: ledger books from THIS, so a BTC amount is never read as USDT.
    fee_detail: Optional[Dict[str, Any]] = None
    detail: Dict[str, Any] = field(default_factory=dict)

    def as_engine_result(self) -> Dict[str, Any]:
        """The shape `CarryEngine._fire` consumes."""
        return {"filled_qty": self.filled_qty, "avg_price": self.avg_price,
                "order_link_id": self.order_link_id, "fee": self.fee,
                "fee_detail": self.fee_detail}


#: Bybit's transaction log refuses a window longer than 7 days.
TXLOG_WINDOW_MS = 7 * 24 * 3600 * 1000 - 1
TXLOG_MAX_PAGES = 40
TXLOG_PAGE_SIZE = 50


def txlog_windows(start_ms: int, end_ms: int) -> List[Tuple[int, int]]:
    """Contiguous request windows of at most 7 days covering ``[start, end]``.

    Adjacent windows share one millisecond: the docs do not say whether
    ``endTime`` is inclusive, so a row exactly on a boundary must fall in at
    least one window under EITHER semantics. Callers dedupe the overlap.
    """
    out: List[Tuple[int, int]] = []
    ws = int(start_ms)
    end = int(end_ms)
    while True:
        we = min(ws + TXLOG_WINDOW_MS, end)
        out.append((ws, we))
        if we >= end:
            return out
        ws = we


@dataclass
class LogPages:
    """What a cursor-paginated read actually returned, and whether it is whole.

    ``complete`` is True only when the venue's cursor ran out normally. Anything
    else (a request error, a malformed page, a repeated cursor, running out of
    pages) leaves ``complete`` False with the reason in ``error``; ``rows`` is
    then diagnostic only and must never be treated as the whole log.
    """

    rows: List[Dict[str, Any]] = field(default_factory=list)
    pages: int = 0
    complete: bool = False
    error: str = ""


def read_log_pages(request: Any, params: Dict[str, Any], *,
                   max_pages: int = TXLOG_MAX_PAGES) -> LogPages:
    """Follow every ``nextPageCursor`` of one transaction-log query.

    ``request(params)`` performs one GET and returns the v5 envelope. Each page
    is validated before it is believed: an envelope with a ``result`` object, a
    ``list`` of objects, and a string cursor. The response cursor becomes the
    next request's ``cursor``. A cursor seen before, or ``max_pages`` pages
    without the cursor ending, is an error: partial success is never reported
    as success. This is the ONE paginator for the log; the production funding
    reader and the operator probe both use it.
    """
    out = LogPages()
    seen: set = set()
    cursor: Optional[str] = None
    while True:
        if out.pages >= max_pages:
            out.error = (f"did not finish within {max_pages} pages; the log "
                         "read is incomplete")
            return out
        page_params = dict(params)
        if cursor:
            page_params["cursor"] = cursor
        try:
            payload = request(page_params)
        except Exception as exc:  # noqa: BLE001
            out.error = f"request failed on page {out.pages + 1}: " \
                        f"{type(exc).__name__}: {exc}"
            return out
        result = payload.get("result") if isinstance(payload, dict) else None
        if not isinstance(result, dict):
            out.error = f"page {out.pages + 1} has no result object"
            return out
        rows = result.get("list")
        if not isinstance(rows, list):
            out.error = f"page {out.pages + 1} carries no list"
            return out
        if any(not isinstance(r, dict) for r in rows):
            out.error = f"page {out.pages + 1} holds a row that is not an object"
            return out
        out.pages += 1
        out.rows.extend(rows)
        nxt = result.get("nextPageCursor")
        if nxt is None or nxt == "":
            out.complete = True
            return out
        if not isinstance(nxt, str):
            out.error = f"page {out.pages} returned a non-string cursor"
            return out
        if nxt == cursor or nxt in seen:
            out.error = f"cursor repeated on page {out.pages}; refusing to loop"
            return out
        seen.add(nxt)
        cursor = nxt


#: Bybit documents at most 200 rows per funding-history request.
FUNDING_HISTORY_PAGE = 200
FUNDING_HISTORY_MAX_REQUESTS = 80
#: A gap between prints this many times the neighbouring interval is a missing
#: observation, not a schedule.
FUNDING_GAP_FACTOR = 1.5
#: Two intervals this close are the same interval (clock jitter in the stamps).
FUNDING_INTERVAL_JITTER_MS = 5 * 60_000

#: The longest settlement interval on a Bybit linear perpetual (BTCUSDT's
#: ``fundingInterval`` is 480 minutes; other contracts settle more often).
#: A VENUE FACT used only to say "a window longer than this must hold a print",
#: never as a trading threshold.
FUNDING_MAX_INTERVAL_MS = 8 * 3600 * 1000


@dataclass
class FundingHistory:
    """Public funding prints over ``[start_ms, end_ms]`` and how whole they are.

    ``complete`` is True only when every request was answered with a valid
    envelope, nothing was truncated, no row was malformed, outside the window
    or contradictory, and no gap larger than the neighbouring interval remains.
    Otherwise ``prints`` is diagnostic only and ``errors``/``gaps`` say why.
    """

    start_ms: int = 0
    end_ms: int = 0
    prints: List[Tuple[float, int]] = field(default_factory=list)  # (bps, ms)
    requests: int = 0
    rows_returned: int = 0
    gaps: List[Tuple[int, int]] = field(default_factory=list)
    expected: Optional[int] = None
    errors: List[str] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        return not self.errors and not self.gaps

    def to_dict(self) -> Dict[str, Any]:
        return {"start_ms": self.start_ms, "end_ms": self.end_ms,
                "retrieved": len(self.prints), "expected": self.expected,
                "requests": self.requests, "rows_returned": self.rows_returned,
                "gaps": [list(g) for g in self.gaps],
                "errors": list(self.errors), "complete": self.complete}


def funding_gaps(stamps: List[int], *, start_ms: int, end_ms: int
                 ) -> Tuple[List[Tuple[int, int]], Optional[int]]:
    """``(suspected missing intervals, expected count)`` from the stamps alone.

    The schedule is NOT assumed: the reference for a gap is its neighbouring
    interval(s), so an instrument that moves from 8h to 4h settlements is not
    flagged at the change (a gap equal to a neighbour is a schedule), while an
    8h hole inside a 4h run is. The span before the first print and after the
    last is judged against the nearest interval too, because a retention limit
    shows up as a history that simply starts late. Fewer than three prints give
    no interval to judge by: nothing is claimed about a span over a day then.
    """
    ts = sorted(set(int(t) for t in stamps))
    gaps: List[Tuple[int, int]] = []
    if len(ts) < 3:
        # Fewer than three prints give no interval to judge by, but the venue
        # itself does: a linear perpetual settles at least every
        # FUNDING_MAX_INTERVAL_MS, so a stretch longer than that (plus jitter)
        # with no print in it is not "quiet", it is missing. An EMPTY answer
        # over a window that must contain a settlement is therefore not a
        # complete answer and cannot support "zero funding". Windows shorter
        # than one interval may genuinely hold none, and stay complete.
        limit = FUNDING_MAX_INTERVAL_MS + FUNDING_INTERVAL_JITTER_MS
        edges = [int(start_ms)] + ts + [int(end_ms)]
        for a, b in zip(edges, edges[1:]):
            if b - a > limit:
                gaps.append((a, b))
        return gaps, None
    iv = [b - a for a, b in zip(ts, ts[1:])]
    expected = len(ts)
    for i, d in enumerate(iv):
        nb = [iv[j] for j in (i - 1, i + 1) if 0 <= j < len(iv)]
        if any(abs(d - n) <= FUNDING_INTERVAL_JITTER_MS for n in nb):
            continue
        ref = min(nb)
        if d > FUNDING_GAP_FACTOR * ref:
            gaps.append((ts[i], ts[i + 1]))
            expected += max(1, round(d / ref) - 1)
    first, last = iv[0], iv[-1]
    if ts[0] - start_ms > FUNDING_GAP_FACTOR * first:
        gaps.append((int(start_ms), ts[0]))
        expected += max(1, round((ts[0] - start_ms) / first) - 1)
    if end_ms - ts[-1] > FUNDING_GAP_FACTOR * last:
        gaps.append((ts[-1], int(end_ms)))
        expected += max(1, round((end_ms - ts[-1]) / last) - 1)
    return gaps, expected


def funding_sign_check(funding: float, change: Any, cash_flow: Any = None,
                       fee: Any = None) -> Tuple[str, str]:
    """Cross-check a SETTLEMENT row's ``funding`` sign against its ``change``.

    Field mapping (Bybit v5 ``/v5/account/transaction-log``, Unified account):
    ``funding`` is the funding component of the row and ``change`` is the
    resulting WALLET change, ``change = cashFlow + funding - fee`` (the wallet
    change is unambiguous: positive means the account RECEIVED money).  The
    sign of ``funding`` is therefore trusted only when ``change`` reproduces it:

    * ``confirmed``  -- ``change`` equals ``funding + cashFlow - fee``
    * ``mismatch``   -- ``change`` equals ``-funding + cashFlow - fee``: the
                        two fields disagree about who paid whom
    * ``unverified`` -- ``change`` is missing, not a finite number, or agrees
                        with neither.

    A zero funding amount with a zero change is confirmed (no sign to get
    wrong).  Nothing is assumed: only ``confirmed`` may be booked.
    """
    def num(x: Any) -> Optional[float]:
        if x is None or str(x).strip() == "":
            return None
        try:
            v = float(x)
        except (TypeError, ValueError):
            return None
        return v if math.isfinite(v) else None

    ch = num(change)
    if ch is None:
        return "unverified", "the row carries no usable change field"
    cf = num(cash_flow) or 0.0
    fe = num(fee) or 0.0
    tol = 1e-8 + 1e-6 * max(abs(funding), abs(ch))
    if abs(ch - (funding + cf - fe)) <= tol:
        return "confirmed", ""
    if funding != 0.0 and abs(ch - (-funding + cf - fe)) <= tol:
        return "mismatch", (f"funding {funding!r} but the wallet change is "
                            f"{ch!r}: opposite signs")
    return "unverified", (f"change {ch!r} agrees with neither funding "
                          f"{funding!r} nor its negation")


@dataclass
class SettlementEvidence:
    """What the account transaction log returned for one symbol and interval.

    ``payments`` are validated SETTLEMENT rows (see ``_funding_row``). The
    counters say what was NOT used and why; ``complete`` is False when any
    window failed to read or any row was malformed/out of window, in which case
    absence of a payment proves nothing.
    """

    start_ms: int = 0
    end_ms: int = 0
    payments: List[Dict[str, Any]] = field(default_factory=list)
    windows: int = 0
    pages: int = 0
    rows_returned: int = 0
    other_symbol_rows: int = 0
    rejected: Dict[str, int] = field(default_factory=dict)
    duplicate_rows: int = 0
    errors: List[str] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        return not self.errors

    def to_dict(self) -> Dict[str, Any]:
        return {"start_ms": self.start_ms, "end_ms": self.end_ms,
                "settlement_rows": len(self.payments), "windows": self.windows,
                "pages": self.pages, "rows_returned": self.rows_returned,
                "other_symbol_rows": self.other_symbol_rows,
                "rejected": dict(self.rejected),
                "duplicate_rows": self.duplicate_rows,
                "errors": list(self.errors), "complete": self.complete}


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
        #: (product, symbol, orderLinkId) of every market order this process
        #: put on the wire, in order. Memory only; see `place_market`.
        self.sent_order_links: List[Tuple[str, str, str]] = []

    # -- the engine's three calls -----------------------------------------

    @staticmethod
    def _short_only_guard(body: Dict[str, Any]) -> None:
        """A perp BUY from this book is always a close: it only ever holds a
        short. `reduceOnly` makes the venue refuse any quantity beyond the
        position, so a close sized from a drifted book can never flip the
        account long."""
        if body.get("category") == LINEAR and body.get("side") == "Buy":
            body["reduceOnly"] = True

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
        #: Recorded BEFORE the request goes out: an order whose response is
        #: lost, or whose booking fails, is still an order the venue may hold,
        #: and a final-state read must be able to find it.
        self.sent_order_links.append((product, symbol, link_id))
        if product == SPOT:
            # Without this a spot market BUY is read as a QUOTE (USDT) amount:
            # 0.00125 would buy 0.00125 USDT of BTC. BybitClient has always set
            # it; this adapter built its own body and did not (INVENTORY F1).
            # Stated on sells too, where it is the venue default, so the unit
            # of every spot qty this module sends is explicit.
            body["marketUnit"] = "baseCoin"
        self._short_only_guard(body)
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
                    "duplicate orderLinkId %s (retCode %s) - the first attempt "
                    "landed; resolving by query", link_id, ret_code)
                try:
                    state = self._order_state(symbol, link_id, product)
                except Exception as read_exc:  # noqa: BLE001
                    state = None
                    logger.error("could not read back %s: %s", link_id, read_exc)
                if state is None:
                    raise OrderOutcomeUnknown(
                        f"orderLinkId {link_id} exists at the venue but could "
                        "not be read back; a leg may be live and unhedged"
                    ) from exc
                if self._rejected(state):
                    return None     # it existed, is final, and filled nothing
                if state["resting"]:
                    # The first attempt is still live. "It did not fill" is
                    # not established, and a fresh order on top of it is the
                    # duplicate this branch exists to prevent.
                    raise OrderOutcomeUnknown(
                        f"orderLinkId {link_id} is {state['status']!r} at the "
                        "venue after a duplicate-id rejection; what it will "
                        "fill is unknown") from exc
                return self._engine_result_from_state(state, qty)
            if self._definitively_rejected(exc):
                logger.error("leg %s %s %s rejected: %s",
                             side, symbol, product, exc)
                return None
            # A transport failure, a 5xx, an unparseable 200: the request may
            # have been executed. "It failed" is NOT established. Ask the
            # venue by the id this order carried; never report "no fill" (the
            # engine would hedge or re-send against an order that exists).
            logger.error("leg %s %s %s outcome unknown (%s); querying the venue",
                         side, symbol, product, exc)
            try:
                state = self._order_state(symbol, link_id, product)
            except Exception as read_exc:  # noqa: BLE001
                raise OrderOutcomeUnknown(
                    f"{link_id}: submission failed ({exc}) and the venue could "
                    f"not be asked what happened ({read_exc}); the order may "
                    "be live") from exc
            if state is None:
                # This branch is only reached without a venue retCode. No
                # answer and no row: the request may still be processed.
                raise OrderOutcomeUnknown(
                    f"{link_id}: submission got no answer from the venue "
                    f"({exc}) and the venue shows no row for it yet; the "
                    "request may still land") from exc
            if self._rejected(state):
                return None     # it existed, is final, and filled nothing
            if state["resting"]:
                raise OrderOutcomeUnknown(
                    f"{link_id} is {state['status']} at the venue after a "
                    "failed submission; what it will fill is unknown") from exc
            return self._engine_result_from_state(state, qty)

        # The create call returned, so the order EXISTS. Whatever the response
        # says about its fill is a claim to check, not a fact: text that is
        # not a number, NaN/inf (every comparison against which is False), a
        # negative quantity or more than was asked for are not "no fill" and
        # are not a fill. They send us to the order's own record.
        try:
            fill: Optional[LegFill] = self._fill_from(response, link_id)
        except Exception as parse_exc:  # noqa: BLE001
            logger.error("%s: create response fill unreadable (%s); reading "
                         "the order back", link_id, parse_exc)
            fill = None
        if fill is None or not self._fill_is_sane(
                fill.filled_qty, fill.avg_price, qty) or fill.filled_qty <= 0:
            # Market orders that report nothing filled are queried rather than
            # assumed dead: the response and the fill are separate events. The
            # create response carried an orderId, so the order EXISTS: a read
            # that fails, or finds nothing, is not "no fill".
            try:
                state = self._order_state(symbol, link_id, product)
                # The order is known to exist and a market order settles in
                # milliseconds; a read that races the matching engine (no row
                # yet, still "New") is asked again before the book is halted
                # over a fill that was fine.
                for _ in range(self.ORDER_READBACK_POLLS):
                    if state is not None and not state["resting"]:
                        break
                    time.sleep(self.ORDER_READBACK_SLEEP_S)
                    state = self._order_state(symbol, link_id, product)
            except Exception as read_exc:  # noqa: BLE001
                raise OrderOutcomeUnknown(
                    f"{link_id} was accepted and could not be read back "
                    f"({read_exc}); its fill is unknown") from read_exc
            if state is None:
                raise OrderOutcomeUnknown(
                    f"{link_id} was accepted and the venue then shows no row "
                    "for it; its fill is unknown")
            if self._rejected(state):
                return None            # terminal with nothing filled
            if state["resting"]:
                # Still live, whatever has filled so far: the rest can land
                # after this read, on a leg the engine would already have
                # booked at the partial size and hedged for that size only.
                raise OrderOutcomeUnknown(
                    f"{link_id} is {state['status']} with "
                    f"{state['filled_qty']!r} filled so far; a market order "
                    "that is still live has no final fill to book")
            return self._engine_result_from_state(state, qty)
        return fill.as_engine_result()

    ORDER_READBACK_POLLS = 4
    ORDER_READBACK_SLEEP_S = 0.2

    @staticmethod
    def _definitively_rejected(exc: Exception) -> bool:
        """True only when the VENUE answered 'no' (a non-zero retCode).

        Anything without a venue retCode - a timeout, a connection reset, a
        5xx, a garbled 200 - leaves the order's fate open.
        """
        try:
            code = int(getattr(exc, "ret_code", 0) or 0)
        except (TypeError, ValueError):
            return False
        # A TransientAPIError is the client giving up after retries on a
        # condition the venue itself called retryable (busy, internal error):
        # the request may still have executed. So may one the venue answered
        # with a timeout: it says the backend did not answer in time, not
        # that the order was refused.
        return (code != 0 and code not in _TIMEOUT_RET_CODES
                and type(exc).__name__ != "TransientAPIError")

    @staticmethod
    def _fill_is_sane(filled: Any, avg_price: Any, requested: float) -> bool:
        """A fill the book can account: finite, not negative, priced when it
        is non-zero, and no larger than the order (beyond venue rounding).
        Partial fills are sane; only impossible ones are not."""
        try:
            filled, avg_price = float(filled), float(avg_price)
        except (TypeError, ValueError):
            return False
        if not (math.isfinite(filled) and math.isfinite(avg_price)):
            return False
        if filled < 0 or avg_price < 0:
            return False
        if filled > 0 and avg_price <= 0:
            return False
        return filled <= requested * (1.0 + PAIR_DUST_FRACTION)

    @classmethod
    def _engine_result_from_state(cls, state: Dict[str, Any],
                                  requested: float) -> Dict[str, Any]:
        if not cls._fill_is_sane(state["filled_qty"], state["avg_price"],
                                 requested):
            raise OrderOutcomeUnknown(
                f"{state['order_link_id']} reads back a fill the book cannot "
                f"account (filled {state['filled_qty']!r} at "
                f"{state['avg_price']!r} of {requested!r} asked); its outcome "
                "is unknown")
        return {"filled_qty": state["filled_qty"],
                "avg_price": state["avg_price"],
                "order_link_id": state["order_link_id"],
                "fee": state["fee"], "fee_detail": state["fee_detail"]}

    # -- resting orders (0040) --------------------------------------------

    def get_venue_time_s(self) -> float:
        """The venue's clock, in epoch seconds. PUBLIC `/v5/market/time`: no
        key, no signature.

        `market_snapshot.take_snapshot` checks it against the local clock
        when the broker offers it; without this reader the live book was
        never clock-checked (`clock_checked` always False) while the shadow
        book was. Raises when the venue does not answer with a time; the
        snapshot then records the check as not made rather than assuming
        the clocks agree.
        """
        result = self._v5_result(
            self.client._request("GET", "/v5/market/time", signed=False))
        try:
            seconds = float(result.get("timeSecond"))
        except (TypeError, ValueError) as exc:
            raise PairIncident(
                f"venue time is not a time: {result!r}") from exc
        if not math.isfinite(seconds) or seconds <= 0:
            raise PairIncident(f"venue time is not a time: {result!r}")
        return seconds

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
        self._assert_ticker_symbol(row, symbol)
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

        self.sent_order_links.append((product, symbol, link_id))

        body = {"category": product, "symbol": symbol, "side": side,
                "orderType": "Limit", "qty": qty_text, "price": price_text,
                "timeInForce": "PostOnly", "orderLinkId": link_id}
        # No marketUnit on a LIMIT order: its qty is base coin by definition,
        # the field belongs to market orders, and the spot limit orders this
        # repo has actually sent to testnet (artifacts/testnet_conformance_
        # evidence.jsonl, the take-profit legs) carry none and were accepted.
        self._short_only_guard(body)
        try:
            self.client._request("POST", "/v5/order/create", signed=True,
                                 body=body)
        except Exception as exc:  # noqa: BLE001
            # Whether this was a PostOnly rejection, a duplicate, or a lost
            # response, the same question decides what to do: does the order
            # exist? The venue is asked rather than the error code guessed.
            state = self._order_state(symbol, link_id, product)
            if state is None and not self._definitively_rejected(exc):
                # No venue answer AND no row: the request may be queued and
                # land after this read. Crossing now could make two legs.
                raise PairIncident(
                    f"post-only {link_id} got no answer from the venue "
                    f"({exc}) and the venue shows no row for it yet; the "
                    "request may still land") from exc
            if state is None or self._rejected(state):
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
        if self._rejected(state):
            # Accepted, then cancelled by the post-only rule before it rested:
            # nothing exists and nothing filled. The caller is free to cross.
            return None
        return state

    @staticmethod
    def _rejected(state: Dict[str, Any]) -> bool:
        """A fresh order that is already terminal with nothing filled."""
        return float(state.get("filled_qty", 0) or 0) <= 0 \
            and not state.get("resting")

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
            # No row at all for an id the venue accepted earlier. Nothing can
            # be said about what it filled; a cancelled-with-zero-fill order
            # is NOT this case - it comes back as a row and is a definite 0.
            raise PairIncident(
                f"{order_link_id} could not be read back after a cancel; what "
                "it filled is unknown")
        return state

    @staticmethod
    def _terminal_statuses():
        """The statuses Bybit documents as final, from the one place the
        client keeps them. Anything else - an open status, a status this code
        has never seen, or no status at all - is an order that may still
        fill. The old list here named the RESTING statuses and called
        everything else terminal, so a truncated row read as a dead order."""
        import bybit_connection as _bybit_connection
        return _bybit_connection.TERMINAL_ORDER_STATUS

    def _order_state(self, symbol: str, link_id: str,
                     product: str) -> Optional[Dict[str, Any]]:
        """What the venue says about one order, or None if it has none.

        None means the venue returned no row for this id. For an id the venue
        has just been asked to create, that means the order was rejected and
        does not exist.
        """
        params = {"category": product, "symbol": symbol,
                  "orderLinkId": link_id}
        result = self.client._request("GET", "/v5/order/realtime",
                                      signed=True, params=params)
        rows = self._v5_list(result) or []
        if not rows:
            # Realtime lists OPEN orders. One that filled, was cancelled or
            # was rejected has left it and is in the history list - the
            # directional client learned this live ("a filled market order
            # was always a 404", BybitClient.get_order). Only when neither
            # list has the id is there no row.
            result = self.client._request("GET", "/v5/order/history",
                                          signed=True, params=params)
            rows = self._v5_list(result) or []
            if not rows:
                return None
        row = rows[0]
        status = str(row.get("orderStatus") or "")
        filled = self._stated(row, "cumExecQty", f"order {link_id}")
        if filled < 0:
            # A negative fill is not "filled nothing": `_rejected` would read
            # it as a clean terminal row and the caller would cross again.
            raise PairIncident(
                f"order {link_id} reports a negative fill {filled}: {row!r}")
        if filled > 0:
            avg = self._stated(row, "avgPrice", f"order {link_id}")
            if avg <= 0:
                raise PairIncident(
                    f"order {link_id} filled {filled} with no usable "
                    f"avgPrice: {row!r}; a fill without a price cannot be "
                    "booked")
        # Fail closed: only a documented terminal status is final.
        resting = status not in self._terminal_statuses()
        # A terminal row with nothing filled is still an ANSWER (the order
        # existed, filled nothing, and is no longer live). Only the caller
        # knows whether that means "rejected, cross instead" (a fresh
        # post-only) or "cancelled clean, take the remainder" (a cancel); it
        # is reported as-is here and never folded into "no row".
        return {"filled_qty": filled,
                "avg_price": (float(row.get("avgPrice") or 0)
                              if filled > 0 else 0.0),
                "order_link_id": link_id, "fee": self._fee(row),
                "fee_detail": self._fee_detail(row),
                "maker": True, "resting": resting, "status": status}

    def read_order(self, symbol: str, order_link_id: str,
                   product: str = LINEAR) -> Optional[Dict[str, Any]]:
        """`_order_state` for callers outside this class (a drill's final-state
        read): the venue's own record of one order, or None if it has none."""
        return self._order_state(symbol, order_link_id, product)

    EXECUTION_PAGE_CAP = 20

    def get_order_executions(self, symbol: str, *, order_link_id: str = "",
                             order_id: str = "", product: str = LINEAR
                             ) -> List[Dict[str, Any]]:
        """The venue's own execution rows (fills) of ONE order. Read-only.

        ``GET /v5/execution/list`` for the given product, every page. The
        engine books from the ORDER's cumulative fields and never reads a fill,
        so without this read a drill's fills are only what the order row says
        about itself. Raises ``PairIncident`` when the answer is not a list,
        a cursor repeats or the page cap is hit: a truncated fill list must
        never look complete.
        """
        if not (order_link_id or order_id):
            raise PairIncident("get_order_executions needs an order id or link id")
        if product not in VALID_PRODUCTS:
            raise ValueError(f"unknown product {product!r}")
        params: Dict[str, Any] = {"category": product, "symbol": symbol,
                                  "limit": 100}
        if order_id:
            params["orderId"] = order_id
        else:
            params["orderLinkId"] = order_link_id
        out: List[Dict[str, Any]] = []
        cursors: set = set()
        for _page in range(self.EXECUTION_PAGE_CAP):
            result = self.client._request("GET", "/v5/execution/list",
                                          signed=True, params=dict(params))
            rows = self._v5_list(result)
            if rows is None:
                raise PairIncident("execution list carries no list")
            out.extend(r for r in rows if isinstance(r, dict))
            cursor = (self._v5_result(result) or {}).get("nextPageCursor")
            if not cursor:
                return out
            if cursor in cursors:
                raise PairIncident("execution list cursor repeated")
            cursors.add(cursor)
            params["cursor"] = cursor
        raise PairIncident("execution list did not finish within the page cap; "
                           "not treating it as complete")

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
        row = self._held_row(rows, symbol)
        # Both numbers must be STATED. An absent / null / blank / NaN field is
        # an unreadable margin call, not the 0 that `or 0` used to make of it
        # (a NaN maintenance margin slipped past `<= 0` and became a NaN ratio).
        margin = self._stated(row, "positionIM", f"position row for {symbol}")
        maintenance = self._stated(row, "positionMM",
                                   f"position row for {symbol}")
        if margin < 0:
            raise PairIncident(
                f"initial margin for {symbol} is {margin}; a negative margin "
                "is not a readable one")
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
        self._assert_ticker_symbol(rows[0], symbol)
        mark = self._stated(rows[0], "markPrice", f"ticker for {symbol}")
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
        self._assert_ticker_symbol(rows[0], symbol)
        price = self._stated(rows[0], "lastPrice", f"spot ticker for {symbol}")
        if not self._positive_finite(price):
            raise PairIncident(f"spot mark for {symbol} is {price!r}")
        return price

    @staticmethod
    def _assert_ticker_symbol(row: Any, symbol: str) -> None:
        """A ticker row that names another instrument prices the wrong market.
        A row that names none is the venue's usual silence on a filtered
        request and is accepted; a stated mismatch is refused."""
        named = row.get("symbol") if isinstance(row, dict) else None
        if named is not None and str(named) != symbol:
            raise PairIncident(
                f"ticker row is for {named!r}, not {symbol!r}; refusing to "
                "price one instrument from another's row")

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
        self._assert_ticker_symbol(rows[0], symbol)
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
                # The venue's minimum order VALUE is stated on every linear
                # instrument. Absent / null / blank is not "no minimum".
                rules = {"qty_step": float(lot["qtyStep"]),
                         "min_qty": float(lot["minOrderQty"]),
                         "min_notional": self._stated(
                             lot, "minNotionalValue",
                             f"instrument rules for {symbol}")}
        except (KeyError, TypeError, ValueError) as exc:
            raise PairIncident(
                f"instrument rules for {symbol}/{product} are malformed: "
                f"{lot!r}") from exc
        # NaN compares False with everything, so `<= 0` alone lets it through.
        if not all(math.isfinite(v) for v in rules.values()) \
                or rules["qty_step"] <= 0 or rules["min_qty"] <= 0 \
                or rules["min_notional"] < 0:
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
            balance = self._stated(entry, "walletBalance",
                                   f"wallet row for {coin}")
            # `locked` is what is committed to open orders and is not the
            # book's to hedge. Unstated is not 0: that would overstate the
            # inventory the hedge is sized from.
            locked = self._stated(entry, "locked", f"wallet row for {coin}")
            if locked < 0:
                raise PairIncident(
                    f"wallet row for {coin} locks {locked}; a negative lock "
                    "is not a readable one")
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

    def get_margin_mode(self) -> str:
        """The account's margin mode, as the venue states it
        (`GET /v5/account/info`, `marginMode`). One of `MARGIN_MODES`.

        Raises `PairIncident` when the account cannot be read or states
        anything else: a missing field is never read as a mode.
        """
        result = self.client._request("GET", "/v5/account/info", signed=True)
        body = self._v5_result(result)
        raw = body.get("marginMode") if isinstance(body, dict) else None
        if raw not in MARGIN_MODES:
            raise PairIncident(
                f"account marginMode is {raw!r}, not one of {MARGIN_MODES}; "
                "liquidation semantics unknown, and an unknown margin mode is "
                "not a safe one")
        return raw

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

        margin_mode = self.get_margin_mode()
        account_mm = None
        if margin_mode in ACCOUNT_LEVEL_MARGIN_MODES:
            # Required, not a cross-check: under account-level margin the
            # per-position liqPrice is not the whole answer. A wallet that
            # cannot be read, or a rate that is missing, malformed, non-finite
            # or negative, is an unreadable margin call and raises - the
            # engine halts the held book on it.
            wallet = self.client._request(
                "GET", "/v5/account/wallet-balance", signed=True,
                params={"accountType": "UNIFIED"})
            wrows = self._v5_list(wallet)
            if not wrows or not isinstance(wrows[0], dict):
                raise PairIncident(
                    "wallet balance unreadable; accountMMRate unknown under "
                    f"{margin_mode}, and an unknown account margin rate is a "
                    "margin call you cannot see")
            raw = wrows[0].get("accountMMRate")
            try:
                if raw is None or isinstance(raw, bool) \
                        or str(raw).strip() == "":
                    raise ValueError("absent")
                account_mm = float(raw)
            except (TypeError, ValueError) as exc:
                raise PairIncident(
                    f"accountMMRate is {raw!r} under {margin_mode}; an "
                    "unstated account margin rate is not a safe one") from exc
            if not math.isfinite(account_mm) or account_mm < 0:
                raise PairIncident(
                    f"accountMMRate is {raw!r} under {margin_mode}; an "
                    "unusable account margin rate is not a safe one")

        if not rows:
            return {"size": 0.0, "side": "", "mark": 0.0, "liq_price": None,
                    "distance_pct": None, "account_mm_rate": account_mm,
                    "margin_mode": margin_mode,
                    "reason": "NO_POSITION"}

        row = self._held_row(rows, symbol)
        size = self._stated_size(row, symbol)
        try:
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
            if not math.isfinite(mark):
                mark = 0.0      # a flat row's mark is informational only
            return {"size": 0.0, "side": "", "mark": mark, "liq_price": None,
                    "distance_pct": None, "account_mm_rate": account_mm,
                    "margin_mode": margin_mode,
                    "reason": "NO_POSITION"}

        if not self._positive_finite(mark):
            raise PairIncident(
                f"position row for {symbol} holds {size:.10g} with mark "
                f"{mark!r}; a held position with no usable mark cannot be "
                "judged unreachable")
        # Only the venue's own empty string means "no reachable liquidation
        # price". A key that is absent or null is a row that did not state it
        # (truncated, renamed, partial): unreadable, never the safest state.
        raw_liq = row.get("liqPrice")
        if raw_liq is None:
            raise PairIncident(
                f"position row for {symbol} does not state 'liqPrice': "
                f"{row!r}; only an empty string means unreachable")
        if str(raw_liq).strip() == "":
            return {"size": size, "side": side, "mark": mark,
                    "liq_price": None, "distance_pct": None,
                    "account_mm_rate": account_mm,
                    "margin_mode": margin_mode,
                    "reason": "BEYOND_VENUE_PRICE_BOUNDS"}
        try:
            liq = float(raw_liq)
        except (TypeError, ValueError) as exc:
            raise PairIncident(
                f"liqPrice for {symbol} is {raw_liq!r}") from exc
        if not (math.isfinite(liq) and liq > 0 and mark > 0):
            # The venue named a liquidation price and it cannot be measured
            # (nan, zero, negative, or no usable mark to measure it from).
            # Only "" means unreachable; anything else unusable is an
            # unreadable margin call, and the engine halts on it. It used to
            # come back as distance None, which the engine reads as safe.
            raise PairIncident(
                f"liqPrice {raw_liq!r} with mark {mark!r} for {symbol} cannot "
                "be measured; an unusable liquidation price is not an "
                "unreachable one")

        # A short is liquidated ABOVE the mark, a long below. Either way the
        # answer is a positive distance to the bad side.
        distance = (100.0 * (liq - mark) / mark if side == "Sell"
                    else 100.0 * (mark - liq) / mark)
        return {"size": size, "side": side, "mark": mark, "liq_price": liq,
                "distance_pct": distance, "account_mm_rate": account_mm,
                "margin_mode": margin_mode, "reason": ""}

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

    #: The log is documented as holding up to two years. A request reaching back
    #: further cannot be answered completely, so it is REFUSED, not clipped.
    _TXLOG_MAX_SPAN_MS = 730 * 24 * 3600 * 1000
    _TXLOG_PAGE = TXLOG_PAGE_SIZE
    _TXLOG_MAX_PAGES = TXLOG_MAX_PAGES

    def get_funding_history_range(self, symbol: str, start_ms: int,
                                  end_ms: int) -> FundingHistory:
        """Every public funding print in ``[start_ms, end_ms]``, or a verdict why not.

        `GET /v5/market/funding/history` has no cursor: it returns at most 200
        rows newest first. The window is therefore walked BACKWARDS, each
        request ending at the oldest stamp of the previous one (that stamp is
        read twice on purpose: whether ``endTime`` is inclusive is undocumented,
        and the overlap is deduplicated). A page shorter than the limit ends the
        walk. Every row is validated (symbol, parseable rate, a stamp inside the
        window); a repeated stamp with a different rate, a request that makes no
        progress, and running out of requests all leave the history INCOMPLETE
        rather than returning what was seen as if it were whole.
        """
        out = FundingHistory(start_ms=int(start_ms), end_ms=int(end_ms))
        if end_ms < start_ms:
            out.errors.append("end before start")
            return out
        seen: Dict[int, float] = {}
        cur_end = int(end_ms)
        while True:
            if out.requests >= FUNDING_HISTORY_MAX_REQUESTS:
                out.errors.append(
                    f"not finished within {FUNDING_HISTORY_MAX_REQUESTS} "
                    "requests; the history is incomplete")
                break
            out.requests += 1
            try:
                payload = self.client._request(
                    "GET", "/v5/market/funding/history",
                    params={"category": LINEAR, "symbol": symbol,
                            "startTime": int(start_ms), "endTime": cur_end,
                            "limit": FUNDING_HISTORY_PAGE})
            except Exception as exc:  # noqa: BLE001
                out.errors.append(
                    f"request {out.requests} failed: {type(exc).__name__}: {exc}")
                break
            result = payload.get("result") if isinstance(payload, dict) else None
            rows = result.get("list") if isinstance(result, dict) else None
            if not isinstance(rows, list) or any(
                    not isinstance(r, dict) for r in rows):
                out.errors.append(f"request {out.requests} returned a "
                                  "malformed envelope")
                break
            cat = result.get("category")
            if cat not in (None, "") and str(cat) != LINEAR:
                out.errors.append(f"request {out.requests} answered for "
                                  f"category {cat!r}, not {LINEAR!r}")
                break
            out.rows_returned += len(rows)
            page: List[int] = []
            bad = False
            for row in rows:
                try:
                    rate = float(row["fundingRate"])
                    stamp = int(row["fundingRateTimestamp"])
                except (KeyError, TypeError, ValueError):
                    out.errors.append("a funding row is malformed")
                    bad = True
                    continue
                if not math.isfinite(rate) or stamp <= 0:
                    out.errors.append("a funding row is unusable")
                    bad = True
                    continue
                if str(row.get("symbol", symbol)) != symbol:
                    out.errors.append("a funding row names another symbol")
                    bad = True
                    continue
                if not (start_ms <= stamp <= cur_end):
                    out.errors.append(
                        f"a funding row at {stamp} lies outside the requested "
                        f"window [{start_ms}, {cur_end}]")
                    bad = True
                    continue
                if stamp in seen and seen[stamp] != rate:
                    out.errors.append(
                        f"two different rates were returned for {stamp}")
                    bad = True
                    continue
                seen[stamp] = rate
                page.append(stamp)
            if bad:
                break
            if len(rows) < FUNDING_HISTORY_PAGE:
                break                       # the window is exhausted
            oldest = min(page) if page else None
            if oldest is None or oldest >= cur_end:
                out.errors.append("a full page made no progress; refusing to "
                                  "loop")
                break
            cur_end = oldest
            if cur_end <= start_ms:
                break
        out.prints = sorted(((r * 1e4, t) for t, r in seen.items()),
                            key=lambda x: x[1])
        if not out.errors:
            out.gaps, out.expected = funding_gaps(
                [t for _r, t in out.prints], start_ms=int(start_ms),
                end_ms=int(end_ms))
        return out

    def get_funding_evidence(self, symbol: str, start_ms: int,
                             end_ms: Optional[int] = None
                             ) -> SettlementEvidence:
        """The account's SETTLEMENT rows for `symbol`, validated, with a verdict.

        `GET /v5/account/transaction-log` (Unified), `type=SETTLEMENT`, paged
        through every cursor, in windows of at most 7 days. The server filter is
        NOT trusted: a row must itself say ``type == SETTLEMENT`` (a row with no
        type, or any other, is rejected), its ``category`` if present must be
        linear, and its ``transactionTime`` must lie inside the interval. A
        malformed row or an unreadable window makes the evidence INCOMPLETE; the
        valid rows are still returned, because each is a venue fact on its own,
        but the absence of a payment then proves nothing.
        """
        end = int(end_ms if end_ms is not None else time.time() * 1000)
        start = int(start_ms)
        ev = SettlementEvidence(start_ms=start, end_ms=end)
        if end - start > self._TXLOG_MAX_SPAN_MS:
            # Silently clipping would return a partial history that looks whole.
            ev.errors.append(
                f"funding requested back to {start} but the transaction log "
                f"holds at most {self._TXLOG_MAX_SPAN_MS // 86_400_000} days; "
                "the payments before that cannot be read")
            return ev
        found: Dict[Tuple[str, int, float], Dict[str, Any]] = {}
        for window_start, window_end in txlog_windows(start, end):
            ev.windows += 1
            res = read_log_pages(
                lambda p: self.client._request(
                    "GET", "/v5/account/transaction-log", signed=True, params=p),
                {"accountType": "UNIFIED", "category": LINEAR,
                 "type": "SETTLEMENT", "startTime": window_start,
                 "endTime": window_end, "limit": self._TXLOG_PAGE},
                max_pages=self._TXLOG_MAX_PAGES)
            ev.pages += res.pages
            ev.rows_returned += len(res.rows)
            if not res.complete:
                ev.errors.append(
                    f"window {window_start}..{window_end} unreadable: "
                    f"{res.error}")
            for row in res.rows:
                try:
                    parsed = self._funding_row(row, symbol, start, end)
                except PairIncident as exc:
                    ev.errors.append(str(exc))
                    continue
                if isinstance(parsed, str):
                    if parsed == "other_symbol":
                        ev.other_symbol_rows += 1
                    elif parsed != "no_funding":
                        ev.rejected[parsed] = ev.rejected.get(parsed, 0) + 1
                        if parsed in ("out_of_window",):
                            ev.errors.append(
                                "a settlement row lies outside the requested "
                                "interval")
                    continue
                key = (parsed["id"], parsed["ms"], parsed["usd"])
                if key in found:
                    ev.duplicate_rows += 1
                found[key] = parsed
        ev.payments = sorted(found.values(), key=lambda r: (r["ms"], r["id"]))
        return ev

    def get_funding_payments(self, symbol: str, start_ms: int,
                             end_ms: Optional[int] = None
                             ) -> List[Dict[str, Any]]:
        """Funding the VENUE paid or charged this account, settled rows only.

        Returned as `{id, ms, usd, currency, size, symbol, category}`, oldest
        first, for `symbol` only; `usd` is signed (positive = RECEIVED) and
        `size` is the venue's own signed position size on the row. Raises when
        the evidence is incomplete: a partial list would book some settlements
        and silently drop others. The production path uses
        `get_funding_evidence`, which keeps the verdict instead of raising.
        """
        ev = self.get_funding_evidence(symbol, start_ms, end_ms)
        if not ev.complete:
            raise PairIncident(
                "funding payments are unreadable: " + "; ".join(ev.errors[:3]))
        return ev.payments

    @staticmethod
    def _funding_row(row: Dict[str, Any], symbol: str,
                     start_ms: Optional[int] = None,
                     end_ms: Optional[int] = None
                     ) -> Any:
        """A parsed payment dict, or a short REASON string it was not used."""
        if str(row.get("symbol", "")) != symbol:
            return "other_symbol"
        # The request filters server-side, but a row of any other type is not a
        # settlement whatever its fields say, and a row that does not state its
        # type cannot be proved to be one.
        if str(row.get("type", "")) != "SETTLEMENT":
            return "wrong_type" if "type" in row else "no_type"
        category = row.get("category")
        if category not in (None, "") and str(category) != LINEAR:
            return "wrong_category"
        raw = row.get("funding")
        if raw is None or str(raw).strip() == "":
            return "no_funding"
        try:
            usd = float(raw)
            stamp = int(row["transactionTime"])
            ident = str(row["id"])
        except (KeyError, TypeError, ValueError) as exc:
            raise PairIncident(
                f"funding row is malformed ({exc!r}): {row!r}") from exc
        if not math.isfinite(usd) or not ident or stamp <= 0:
            raise PairIncident(f"funding row is unusable: {row!r}")
        if (start_ms is not None and stamp < start_ms) or (
                end_ms is not None and stamp > end_ms):
            return "out_of_window"
        size = None
        if str(row.get("size", "")).strip() != "":
            try:
                size = float(row["size"])
            except (TypeError, ValueError):
                size = None
            if size is not None and not math.isfinite(size):
                size = None
        sign, why = funding_sign_check(
            usd, row.get("change"), row.get("cashFlow"), row.get("fee"))
        return {"id": ident, "ms": stamp, "usd": usd, "symbol": symbol,
                "currency": str(row.get("currency", "")).upper() or None,
                "size": size, "category": LINEAR,
                "change": row.get("change"), "sign": sign, "sign_why": why}

    def get_position_leverage(self, symbol: str) -> Optional[float]:
        """The leverage the venue reports on this symbol's position row
        (`GET /v5/position/list`, `leverage`), or None when it is not stated.

        READ-ONLY and informational: this process never sets or gates on it.
        It exists so the number is visible, because nothing else records it.
        """
        result = self.client._request(
            "GET", "/v5/position/list", signed=True,
            params={"category": LINEAR, "symbol": symbol})
        rows = self._v5_list(result)
        if not rows or not isinstance(rows[0], dict):
            return None
        try:
            value = float(rows[0].get("leverage"))
        except (TypeError, ValueError):
            return None
        return value if math.isfinite(value) and value > 0 else None

    def get_position_mode(self, symbol: str) -> str:
        """``"hedge"`` iff the venue lists a hedge-mode slot (positionIdx 1 or
        2) for the symbol, else ``"one_way"``. Read-only, same call as the
        position read. An empty list proves nothing about the mode and reads
        as one-way: a mismatched order is then rejected by the venue with no
        exposure. Raises when the response is unreadable.
        """
        result = self.client._request(
            "GET", "/v5/position/list", signed=True,
            params={"category": LINEAR, "symbol": symbol})
        rows = self._v5_list(result)
        if rows is None:
            raise PairIncident(f"cannot read the {symbol} position mode")
        for row in rows:
            if str(row.get("positionIdx")) in ("1", "2"):
                return "hedge"
        return "one_way"

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
        # Bybit reports `size` UNSIGNED with a `side`. This book only ever
        # holds a perp SHORT ("Sell"); a long of the same size is a different
        # exposure and must not read as the hedge. Any row (one-way mode has
        # one, hedge mode two) holding size on another side, or on no stated
        # side, is refused rather than summed into the short.
        total = 0.0
        for row in rows:
            size = self._stated_size(row, symbol)
            if size == 0.0:
                continue
            # positionIdx: 0 one-way, 1 hedge-mode LONG slot, 2 hedge-mode SHORT
            # slot. Size sitting in the long slot is not this book's hedge
            # whatever `side` says, and an unrecognised index is not readable.
            idx = row.get("positionIdx")
            if idx is not None and str(idx) not in ("0", "2"):
                raise PairIncident(
                    f"{symbol} holds {size:.10g} in positionIdx {idx!r}, not "
                    "the one-way (0) or hedge-mode short (2) slot")
            if str(row.get("side", "")) != "Sell":
                raise PairIncident(
                    f"{symbol} holds {size:.10g} on side "
                    f"{row.get('side')!r}, not a short; this book cannot read "
                    "that as its hedge")
            total += size
        return total

    def get_position_slots(self, symbol: str) -> list:
        """The non-empty position rows of ``symbol`` as ``[{positionIdx, side,
        size}]`` (read-only). A caller that must not act on a hedge-mode slot
        asks this; `get_perp_position` accepts the hedge-mode short slot (2)
        because the book's hedge may sit there. Raises when unreadable."""
        result = self.client._request(
            "GET", "/v5/position/list", signed=True,
            params={"category": LINEAR, "symbol": symbol})
        rows = self._v5_list(result)
        if rows is None:
            raise PairIncident(
                f"cannot read the {symbol} position; refusing to assume flat")
        out = []
        for row in rows:
            size = self._stated_size(row, symbol)
            if size != 0.0:
                out.append({"positionIdx": row.get("positionIdx"),
                            "side": row.get("side"), "size": size})
        return out

    #: closed-pnl page size; a full page may be truncated and proves nothing.
    _CLOSED_PNL_PAGE = 50

    def get_closed_short_qty_since(self, symbol: str, since_ms: int
                                   ) -> Optional[float]:
        """Perp short quantity the venue reports CLOSED since `since_ms`
        (`GET /v5/position/closed-pnl`: rows carry `qty`, the closing `side`
        and `updatedTime`, the shape `BybitClient.get_closed_pnl` documents).

        A closed-pnl row exists only because a position was closed - by any
        order, ours, a liquidation or a manual one - so a total here is
        POSITIVE evidence of a close, which a flat-reading position list is
        not (that can lag). None when it cannot be established: a page that may
        be truncated, a row that does not parse, or an unreadable response.
        Raises on a failed request.
        """
        result = self.client._request(
            "GET", "/v5/position/closed-pnl", signed=True,
            params={"category": LINEAR, "symbol": symbol,
                    "startTime": int(since_ms), "limit": self._CLOSED_PNL_PAGE})
        rows = self._v5_list(result)
        if rows is None or len(rows) >= self._CLOSED_PNL_PAGE:
            return None
        total = 0.0
        for row in rows:
            if not isinstance(row, dict):
                return None
            try:
                stamp = int(row.get("updatedTime") or 0)
                qty = float(row.get("qty"))
            except (TypeError, ValueError):
                return None
            if not math.isfinite(qty) or qty < 0:
                return None
            if stamp <= int(since_ms):
                continue
            if str(row.get("side", "")) != "Buy":     # the CLOSING side of a short
                continue
            total += qty
        return total

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
            # ONE page is 20 rows by default; an order pushed onto page two
            # is still a live order, so every page is read or the read fails.
            rows: list = []
            cursor = ""
            seen_cursors: set = set()
            for _page in range(20):
                params = {"category": category, "symbol": symbol,
                          "openOnly": 0, "limit": 50}
                if cursor:
                    params["cursor"] = cursor
                result = self.client._request(
                    "GET", "/v5/order/realtime", signed=True, params=params)
                page = self._v5_list(result)
                if page is None:
                    raise PairIncident(
                        f"cannot read open {category} orders for {symbol}; "
                        "refusing to assume there are none")
                rows.extend(page)
                nxt = CarryBroker._v5_result(result).get("nextPageCursor")
                if not nxt:
                    break
                if nxt in seen_cursors:
                    raise PairIncident(
                        f"open {category} order paging for {symbol} looped; "
                        "refusing to assume the list is complete")
                seen_cursors.add(nxt)
                cursor = str(nxt)
            else:
                raise PairIncident(
                    f"open {category} orders for {symbol} did not finish "
                    "paging; refusing to assume the list is complete")
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
            if not coins:
                return 0.0
            # The request is filtered by coin, but a reply that lists another
            # asset first must not be read as this one's balance.
            mine = [c for c in coins if isinstance(c, dict)
                    and str(c.get("coin", "")).upper() == symbol[:-4].upper()]
            if len(mine) != 1:
                raise PairIncident(
                    f"wallet reply holds {len(mine)} rows for "
                    f"{symbol[:-4]} among {len(coins)}; the balance is "
                    "not readable")
            return self._stated(mine[0], "walletBalance",
                                f"wallet row for {symbol[:-4]}")
        result = self.client._request(
            "GET", "/v5/position/list", signed=True,
            params={"category": LINEAR, "symbol": symbol})
        rows = self._v5_list(result) or []
        return (self._stated_size(self._held_row(rows, symbol), symbol)
                if rows else 0.0)

    def _held_row(self, rows: list, symbol: str) -> Any:
        """The position row the book is actually holding.

        One row (one-way mode): that row, as always. Several (hedge mode lists
        a slot per direction for a symbol, flat ones included, in either
        order): the single row that carries size. `rows[0]` used to be taken
        blindly, so a comfortable flat slot listed first made a short 1% from
        liquidation read as safe, and a flat slot with zero maintenance margin
        made a readable short unreadable (a halt does not close it).

        Every row must STATE its size (an unstated one is refused). None sized
        is flat: the first row, as before. TWO OR MORE sized rows are left
        exactly as they were, `rows[0]`: which direction each holds, and
        whether they sum to the book, is the job of `get_perp_position` and the
        venue-position check, which halt on a long and sum two shorts; making
        this read refuse would change those halts' reasons.
        """
        if len(rows) == 1:
            return rows[0]
        sized = [r for r in rows if self._stated_size(r, symbol) > 0.0]
        return sized[0] if len(sized) == 1 else rows[0]

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
    def _stated(row: Any, key: str, what: str) -> float:
        """A finite number the venue STATES in `row[key]`. Absent, null, blank,
        not a number, or non-finite is unreadable: every one of those used to
        be read as 0 (`float(row.get(key, 0) or 0)`), which is flat, empty or
        "filled nothing" - the permissive answer - for a truncated payload."""
        raw = row.get(key) if isinstance(row, dict) else None
        if raw is None or str(raw).strip() == "":
            raise PairIncident(
                f"{what} does not state {key!r}: {row!r}; an unstated number "
                "is not zero")
        try:
            number = float(raw)
        except (TypeError, ValueError) as exc:
            raise PairIncident(f"{what} is malformed: {row!r}") from exc
        if not math.isfinite(number):
            raise PairIncident(f"{what} is malformed: {row!r}")
        return number

    @staticmethod
    def _stated_size(row: Any, symbol: str) -> float:
        """The unsigned size a position row STATES. Bybit spells flat as the
        string "0" (a flat row is proven live to carry it)."""
        return abs(CarryBroker._stated(row, "size",
                                       f"position row for {symbol}"))

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
    def _fee_detail(row: Dict[str, Any]) -> Dict[str, Any]:
        """The venue's fee for this order, by the one extraction authority
        (`venue_fees`): `cumFeeDetail` first, the deprecated `cumExecFee` only
        when the detail map is absent. Nothing here parses a fee field."""
        return venue_fees.extract_order_fees(row).to_dict()

    @staticmethod
    def _fee(row: Dict[str, Any]) -> Optional[float]:
        """Legacy single number: the fee iff it is one currency, else None.

        Unknown stays None and a multi-currency fee is None too -- a number
        that silently adds two coins is not a fee. The ledger does not use this;
        it books from `fee_detail`.
        """
        return venue_fees.extract_order_fees(row).single_amount

    @staticmethod
    def _fill_from(response: Optional[Dict[str, Any]],
                   link_id: str) -> LegFill:
        """`response` is `/v5/order/create`'s full envelope; unwrap `result`
        before reading it, same as every other reader in this file.

        Real Bybit create responses carry only `orderId` in `result` - no
        `cumExecQty`/`avgPrice` - so this legitimately returns a zero fill
        most of the time even after the correct unwrap; that is what sends
        `place_market` to `_order_state` next. FakeClients in the test
        suite simulate an immediate fill for convenience and now do so
        inside a real envelope's `result`, not at its top level.
        """
        row = CarryBroker._v5_result(response)
        return LegFill(
            filled_qty=float(row.get("cumExecQty", 0) or 0),
            avg_price=float(row.get("avgPrice", 0) or 0),
            order_link_id=str(row.get("orderLinkId", link_id) or link_id),
            fee=CarryBroker._fee(row), fee_detail=CarryBroker._fee_detail(row),
            detail=dict(row))

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
