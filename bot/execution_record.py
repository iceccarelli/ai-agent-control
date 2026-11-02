"""Decision-time execution record: what the strategy DECIDED at, what it GOT.

Pure computations, no I/O, no clock reads. The inputs are a `DecisionContext`
(what the engine saw when it decided) and a fill (what the venue returned);
the output is one JSON-safe dict that the ledger persists next to the fill
(`Entry.fee_raw["execution"]`) and whose `reference_price` is what
`Entry.decision_mark` carries, so `slippage_section` measures it.

Definitions (the only ones; nothing is counted twice)
-----------------------------------------------------
sign        positive = cost to the book, negative = price improvement.
executable  the side of the decision-time book the order would hit: ask for a
            Buy, bid for a Sell.
mid         (bid + ask) / 2 at decision time.
spread cost half-spread: (executable - mid) * filled_qty = (ask - bid)/2 * q.
            Needs a decision-time bid AND ask; otherwise unavailable.
slippage    (fill - executable) * q for a Buy, (executable - fill) * q for a
            Sell, measured against the decision-time EXECUTABLE price. Spread
            is therefore NOT inside it. Without a book the reference falls
            back to the decision MARK and `reference_basis` says "mark": the
            slippage then contains the half-spread, spread cost is left
            unavailable, and the two are still never added together.
shortfall   spread cost + slippage = (fill - mid) * q (Buy), when a book exists.
actual cost shortfall + confirmed fee (USD). Unconfirmed fee -> unavailable.
expected    the engine's own pre-trade estimate (fee / total cost), pro-rated
            to the filled share of the decided quantity so several orders
            under one decision sum to the estimate rather than multiply it.
delays      decision -> submit and submit -> fill, in ms, from stamps taken at
            the time (never reconstructed). Negative = contradictory = n/a.

depth       OPTIONAL. When the decision-time book came from a depth read
            (`/v5/market/orderbook`), the record also carries the executable
            VWAP of the order's requested quantity walked through the side it
            would hit, the levels used, and whether the shown depth could
            support the quantity at all (`depth.supported`). A record without
            depth is still a valid top-of-book sample; depth never changes the
            reference price, the slippage or the sample policy.

Only FILLED quantity is ever measured. Unfilled quantity is reported as
unfilled and contributes nothing. Prices/timestamps that are non-finite,
non-positive, stale, in the future, or observed after the decision make the
price section UNAVAILABLE; a value is never invented or taken from a later
observation.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

SCHEMA = 1
MEASURED = "MEASURED"
UNAVAILABLE = "UNAVAILABLE"
NO_FILL = "NO_FILL"

#: Decision older than this at submit time is not "the decision" any more.
MAX_DECISION_AGE_MS = 60_000
#: Book/mark observation older than this at decision time is stale.
MAX_OBSERVATION_AGE_MS = 5_000
#: Clock skew allowed before a stamp counts as being in the future.
FUTURE_SKEW_MS = 2_000
#: A mark further than this from the book mid is contradictory data. (Was 5%;
#: a perp mark sits within a few bps of the mid, so 1% is already generous.)
MAX_MARK_MID_DEVIATION = 0.01
#: A fill BETTER than the whole book by more than this is not a fill of that
#: book (a Sell above the ask, a Buy below the bid): the context does not
#: describe the order. Latency allows the book to move a few bps, not more.
MAX_FAVORABLE_FILL_BPS = 10.0
#: A fill WORSE than the executable side by more than this is garbage or a
#: different market, not slippage a 0.01-BTC market order can have.
MAX_ADVERSE_FILL_BPS = 200.0
#: Quantity noise tolerated when comparing a fill with the order.
QTY_REL_TOL = 1e-6
QTY_ABS_TOL = 1e-9

BUY, SELL = "Buy", "Sell"


def _num(x: Any) -> Optional[float]:
    """A finite float, else None (bool and text are not numbers here)."""
    if isinstance(x, bool) or x is None:
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _price(x: Any) -> Optional[float]:
    v = _num(x)
    return v if v is not None and v > 0 else None


def _ms(x: Any) -> Optional[int]:
    v = _num(x)
    return int(v) if v is not None and v > 0 else None


@dataclass(frozen=True)
class DecisionContext:
    """What the engine knew when it decided to send an order."""
    venue: str
    symbol: str
    product: str
    side: str
    decision_ms: int
    qty: float
    mark: Optional[float] = None
    bid: Optional[float] = None
    ask: Optional[float] = None
    #: When the mark/book were OBSERVED (defaults to the decision time).
    observed_ms: Optional[int] = None
    order_type: str = "market"
    #: The engine's estimate for the WHOLE decided quantity, in USD.
    expected_fee_usd: Optional[float] = None
    expected_cost_usd: Optional[float] = None
    #: Where the book came from: "tick_snapshot" (the tick's own touch),
    #: "orderbook" (a public depth read), "ticker_touch" (a public ticker's
    #: best bid/ask). Empty = not stated. Provenance only; never a price.
    book_source: str = ""
    #: The venue's own timestamp for the book (ms), when it stated one.
    book_venue_ts_ms: Optional[int] = None
    #: Depth ladders, best first, from a depth read: ((price, size), ...).
    bids: Optional[Tuple[Tuple[float, float], ...]] = None
    asks: Optional[Tuple[Tuple[float, float], ...]] = None

    def to_dict(self) -> Dict[str, Any]:
        return {k: getattr(self, k) for k in (
            "venue", "symbol", "product", "side", "decision_ms", "qty",
            "mark", "bid", "ask", "observed_ms", "order_type",
            "expected_fee_usd", "expected_cost_usd", "book_source",
            "book_venue_ts_ms")}


@dataclass
class Prices:
    """The validated decision-time prices (or the reasons there are none)."""
    ok: bool
    reference: Optional[float] = None
    basis: str = ""                       # "executable_side" | "mark"
    mid: Optional[float] = None
    executable: Optional[float] = None
    mark: Optional[float] = None
    bid: Optional[float] = None
    ask: Optional[float] = None
    reasons: List[str] = field(default_factory=list)


def executable_price(side: str, bid: Optional[float],
                     ask: Optional[float]) -> Optional[float]:
    if side == BUY:
        return ask
    if side == SELL:
        return bid
    return None


def parse_ladder(levels: Any, *, descending: bool
                 ) -> Optional[Tuple[Tuple[float, float], ...]]:
    """A validated book side, best first, or None.

    Every level must be a `[price, size]` pair of finite positive numbers and
    the prices must be STRICTLY monotone (bids falling, asks rising). One bad
    level makes the whole side unusable: a ladder is never repaired, sorted or
    trimmed into shape, because a venue that sends a broken book has not told
    us what the book was."""
    if not isinstance(levels, (list, tuple)) or not levels:
        return None
    out: List[Tuple[float, float]] = []
    for lv in levels:
        if not isinstance(lv, (list, tuple)) or len(lv) < 2:
            return None
        px, qty = _price(lv[0]), _price(lv[1])
        if px is None or qty is None:
            return None
        if out and not (px < out[-1][0] if descending else px > out[-1][0]):
            return None
        out.append((px, qty))
    return tuple(out)


def walk_book(side: str, qty: Any,
              bids: Optional[Sequence[Tuple[float, float]]],
              asks: Optional[Sequence[Tuple[float, float]]]
              ) -> Optional[Dict[str, Any]]:
    """The executable VWAP of `qty` taken from the side of the book `side`
    hits (asks for a Buy, bids for a Sell), or None when there is no usable
    depth to walk.

    `supported` is False when the levels shown hold less than `qty`: the VWAP
    is then None. It is never extrapolated past the last level."""
    q = _price(qty)
    ladder = asks if side == BUY else bids if side == SELL else None
    if q is None or not ladder:
        return None
    taken = notional = 0.0
    used = 0
    for px, size in ladder:
        if taken >= q * (1.0 - QTY_REL_TOL):
            break
        take = min(size, q - taken)
        taken += take
        notional += take * px
        used += 1
    supported = taken >= q * (1.0 - QTY_REL_TOL) - QTY_ABS_TOL
    available = sum(size for _, size in ladder)
    return {"requested_qty": q,
            "supported": bool(supported),
            "vwap_price": (notional / taken if supported and taken > 0
                           else None),
            "levels_used": used, "levels_seen": len(ladder),
            "available_qty": available}


def validate_decision(ctx: Optional[DecisionContext], *, now_ms: int,
                      submit_ms: Optional[int] = None,
                      max_decision_age_ms: int = MAX_DECISION_AGE_MS,
                      max_observation_age_ms: int = MAX_OBSERVATION_AGE_MS,
                      expect: Optional[Dict[str, Any]] = None
                      ) -> Prices:
    """Accept the decision-time prices only if they can be trusted.

    `expect` names the order the prices are about (`symbol`, `product`,
    `side`, `venue`): a context for another instrument, side or venue does not
    describe this fill and is refused, however well-formed its prices are."""
    if ctx is None:
        return Prices(False, reasons=["NO_DECISION_CONTEXT"])
    why: List[str] = []
    for key, wanted in (expect or {}).items():
        if wanted not in (None, "") and getattr(ctx, key, None) != wanted:
            why.append(f"CONTEXT_{key.upper()}_MISMATCH: decision was for "
                       f"{getattr(ctx, key, None)!r}, the fill is {wanted!r}")
    if ctx.side not in (BUY, SELL):
        why.append(f"BAD_SIDE: {ctx.side!r}")
    d_ms = _ms(ctx.decision_ms)
    if d_ms is None:
        why.append("DECISION_TIME_INVALID")
    else:
        if d_ms > now_ms + FUTURE_SKEW_MS:
            why.append("DECISION_TIME_IN_FUTURE")
        if submit_ms is not None and d_ms > submit_ms + FUTURE_SKEW_MS:
            why.append("DECISION_AFTER_SUBMIT")
        ref_ms = submit_ms if submit_ms is not None else now_ms
        if ref_ms - d_ms > max_decision_age_ms:
            why.append("DECISION_STALE")
    has_book = ctx.bid is not None or ctx.ask is not None
    if ctx.observed_ms is None and has_book:
        # A book with no observation time cannot be called fresh. (A bare mark
        # is still stamped with the decision time, as before.)
        why.append("BOOK_UNTIMESTAMPED")
        o_ms = None
    else:
        obs = ctx.observed_ms if ctx.observed_ms is not None else ctx.decision_ms
        o_ms = _ms(obs)
        if o_ms is None:
            why.append("OBSERVATION_TIME_INVALID")
        elif d_ms is not None:
            if o_ms > d_ms:
                # Observed AFTER the decision (no clock-skew allowance: the
                # observation is stamped by the same clock that stamps the
                # decision): that is a later observation, and a decision
                # price is never rebuilt from one.
                why.append("OBSERVATION_AFTER_DECISION")
            elif d_ms - o_ms > max_observation_age_ms:
                why.append("OBSERVATION_STALE")
    mark = _price(ctx.mark)
    if ctx.mark is not None and mark is None:
        why.append("MARK_INVALID")
    bid, ask = _price(ctx.bid), _price(ctx.ask)
    if (ctx.bid is not None and bid is None) or (
            ctx.ask is not None and ask is None):
        why.append("BOOK_INVALID")
        bid = ask = None
    if (bid is None) != (ask is None):
        why.append("BOOK_ONE_SIDED")
        bid = ask = None
    if bid is not None and ask is not None and bid >= ask:
        # Crossed OR locked: a live book has a positive spread, and a zero one
        # is a stale or synthetic quote, not a touch an order could be priced on.
        why.append("BOOK_CROSSED" if bid > ask else "BOOK_LOCKED")
        bid = ask = None
    mid = (bid + ask) / 2.0 if bid is not None and ask is not None else None
    if mid is not None and mark is not None and abs(mark - mid) / mid \
            > MAX_MARK_MID_DEVIATION:
        why.append("MARK_CONTRADICTS_BOOK")
    if why:
        return Prices(False, reasons=why)
    exe = executable_price(ctx.side, bid, ask)
    if exe is not None:
        return Prices(True, exe, "executable_side", mid, exe, mark, bid, ask)
    if mark is not None:
        return Prices(True, mark, "mark", None, None, mark, None, None)
    return Prices(False, reasons=["NO_DECISION_PRICE"])


def fill_vs_book(side: str, fill: float, bid: float, ask: float
                 ) -> Optional[str]:
    """Why `fill` cannot be a fill of THIS book, or None when it can.

    Favorable beyond the whole book (a Sell above the ask, a Buy below the bid,
    allowing `MAX_FAVORABLE_FILL_BPS` for the book moving in the order's
    latency) is a fill of some other book. Adverse beyond
    `MAX_ADVERSE_FILL_BPS` of the executable side is garbage. Ordinary
    slippage, good or bad, lies between and is measured, not judged."""
    if side == SELL:
        best, worst = ask, bid
        if fill > best * (1.0 + MAX_FAVORABLE_FILL_BPS / 1e4):
            return (f"FILL_OUTSIDE_BOOK: Sell filled {fill} above the whole "
                    f"book (ask {ask})")
        if fill < worst * (1.0 - MAX_ADVERSE_FILL_BPS / 1e4):
            return (f"FILL_OUTSIDE_BOOK: Sell filled {fill}, "
                    f"{MAX_ADVERSE_FILL_BPS:.0f} bps below the bid {bid}")
    elif side == BUY:
        best, worst = bid, ask
        if fill < best * (1.0 - MAX_FAVORABLE_FILL_BPS / 1e4):
            return (f"FILL_OUTSIDE_BOOK: Buy filled {fill} below the whole "
                    f"book (bid {bid})")
        if fill > worst * (1.0 + MAX_ADVERSE_FILL_BPS / 1e4):
            return (f"FILL_OUTSIDE_BOOK: Buy filled {fill}, "
                    f"{MAX_ADVERSE_FILL_BPS:.0f} bps above the ask {ask}")
    return None


def cost_usd(side: str, fill: float, ref: float, qty: float) -> float:
    """Positive = adverse. The one place the sign is decided."""
    return ((fill - ref) if side == BUY else (ref - fill)) * qty


def _depth_section(ctx: Optional[DecisionContext], px: Prices, side: str,
                   requested: Optional[float], filled: float
                   ) -> Optional[Dict[str, Any]]:
    """The optional depth summary, or None when the decision had no depth.

    Only a ladder whose touch IS the validated top of book is used: a depth
    read that disagrees with the bid/ask the decision was priced on is
    dropped, not blended. The quantity walked is the order's REQUESTED
    quantity (what it would have taken), falling back to the filled one."""
    if ctx is None or not ctx.bids or not ctx.asks:
        return None
    bids = parse_ladder(ctx.bids, descending=True)
    asks = parse_ladder(ctx.asks, descending=False)
    if bids is None or asks is None:
        return {"status": "DEPTH_INVALID"}
    if bids[0][0] != px.bid or asks[0][0] != px.ask:
        return {"status": "DEPTH_TOUCH_MISMATCH"}
    qty = requested if requested is not None else (filled or None)
    walk = walk_book(side, qty, bids, asks)
    if walk is None:
        return {"status": "DEPTH_NOT_WALKED"}
    return dict(walk, status="MEASURED" if walk["supported"]
                else "DEPTH_INSUFFICIENT", basis="requested_qty"
                if requested is not None else "filled_qty")


def assess_fill(ctx: Optional[DecisionContext], *, side: str, symbol: str,
                product: str, venue: str, order_type: str,
                requested_qty: Optional[float], filled_qty: Any,
                avg_price: Any, now_ms: int,
                fee_usd: Optional[float] = None, fee_confirmed: bool = False,
                order_link_id: str = "", submit_ms: Optional[int] = None,
                fill_ms: Optional[int] = None, fill_ms_basis: str = "",
                environment: str = "",
                max_decision_age_ms: int = MAX_DECISION_AGE_MS,
                max_observation_age_ms: int = MAX_OBSERVATION_AGE_MS
                ) -> Dict[str, Any]:
    """One order's execution record. Never raises; failures become reasons."""
    reasons: List[str] = []
    filled = _num(filled_qty)
    avg = _num(avg_price)
    req = _price(requested_qty)
    qty_ok = True
    if filled is None or filled < 0:
        reasons.append("FILLED_QTY_INVALID")
        qty_ok = False
        filled = 0.0
    if req is None:
        reasons.append("REQUESTED_QTY_UNKNOWN")
    elif filled > req * (1.0 + QTY_REL_TOL) + QTY_ABS_TOL:
        reasons.append("FILL_EXCEEDS_ORDER")      # contradictory: not measured
        qty_ok = False
    if filled > 0 and (avg is None or avg <= 0):
        reasons.append("FILL_PRICE_INVALID")
        qty_ok = False
    # Unfilled is never executed: clamp at zero, never negative.
    unfilled = max(0.0, req - filled) if req is not None and qty_ok else None

    sub = _ms(submit_ms)
    fil = _ms(fill_ms)
    d_ms = _ms(ctx.decision_ms) if ctx is not None else None
    d2s = s2f = None
    if sub is not None and sub > now_ms + FUTURE_SKEW_MS:
        reasons.append("SUBMIT_TIME_IN_FUTURE")
        sub = None
    if fil is not None and fil > now_ms + FUTURE_SKEW_MS:
        reasons.append("FILL_TIME_IN_FUTURE")
        fil = None
    if d_ms is not None and sub is not None:
        if sub >= d_ms:
            d2s = sub - d_ms
        else:
            reasons.append("SUBMIT_BEFORE_DECISION")
    if sub is not None and fil is not None:
        if fil >= sub:
            s2f = fil - sub
        else:
            reasons.append("FILL_BEFORE_SUBMIT")

    px = validate_decision(ctx, now_ms=now_ms, submit_ms=sub,
                           max_decision_age_ms=max_decision_age_ms,
                           max_observation_age_ms=max_observation_age_ms,
                           expect={"symbol": symbol, "product": product,
                                   "side": side, "venue": venue})
    rec: Dict[str, Any] = {
        "schema": SCHEMA, "venue": venue, "symbol": symbol, "product": product,
        "side": side, "order_type": order_type,
        "order_link_id": order_link_id,
        "decision_ms": d_ms, "submit_ms": sub, "fill_ms": fil,
        "fill_ms_basis": fill_ms_basis if fil is not None else "",
        "decision_to_submit_ms": d2s, "submit_to_fill_ms": s2f,
        "requested_qty": req, "filled_qty": filled if qty_ok else None,
        "unfilled_qty": unfilled,
        "fill_ratio": (filled / req if req and qty_ok else None),
        "avg_price": avg if (filled and qty_ok) else None,
        "fee_usd": fee_usd if fee_confirmed else None,
        "decision_bid": None, "decision_ask": None, "decision_mark": None,
        "reference_price": None, "reference_basis": "",
        "slippage_usd": None, "slippage_bps": None,
        "spread_cost_usd": None, "shortfall_vs_mid_usd": None,
        "expected_fee_usd": None, "expected_cost_usd": None,
        "actual_cost_usd": None, "cost_variance_usd": None,
        # OPTIONAL context (a record without any of it is still valid):
        "environment": str(environment or ""),
        "notional_usd": (filled * avg if qty_ok and filled > 0
                         and avg is not None else None),
        "book_source": "", "book_age_ms": None, "book_venue_ts_ms": None,
        "mid_price": None, "spread_abs": None, "spread_bps": None,
        "depth": None,
    }
    if ctx is not None:
        rec["observed_ms"] = _ms(ctx.observed_ms if ctx.observed_ms is not None
                                 else ctx.decision_ms)
        rec["book_source"] = str(ctx.book_source or "")
        rec["book_venue_ts_ms"] = _ms(ctx.book_venue_ts_ms)
        if rec["observed_ms"] is not None and d_ms is not None:
            # How old the book was when the decision was taken. Negative =
            # observed after the decision, which validation refuses.
            rec["book_age_ms"] = d_ms - rec["observed_ms"]
    if not px.ok:
        reasons.extend(px.reasons)
    else:
        rec.update(decision_bid=px.bid, decision_ask=px.ask,
                   decision_mark=px.mark, reference_price=px.reference,
                   reference_basis=px.basis)
        if px.mid is not None:
            rec["mid_price"] = px.mid
            rec["spread_abs"] = px.ask - px.bid
            rec["spread_bps"] = (px.ask - px.bid) / px.mid * 1e4
            rec["depth"] = _depth_section(ctx, px, side, req, filled)

    if qty_ok and filled == 0.0:
        rec["status"], rec["reasons"] = NO_FILL, reasons
        return rec
    measurable = qty_ok and px.ok and avg is not None
    if measurable and px.basis == "executable_side":
        implausible = fill_vs_book(side, avg, px.bid, px.ask)
        if implausible:
            reasons.append(implausible)
            measurable = False
    if not measurable:
        rec["status"], rec["reasons"] = UNAVAILABLE, reasons
        rec["reference_price"] = None     # not journalled as a decision mark
        return rec

    q, ref = filled, px.reference
    slip = cost_usd(side, avg, ref, q)
    rec["slippage_usd"] = slip
    rec["slippage_bps"] = slip / (ref * q) * 1e4
    if px.mid is not None:
        spread = abs(px.executable - px.mid) * q    # = half-spread * q
        rec["spread_cost_usd"] = spread
        rec["shortfall_vs_mid_usd"] = spread + slip
    else:
        reasons.append("SPREAD_UNAVAILABLE: no decision-time bid/ask; "
                       "slippage is against the mark and includes the "
                       "half-spread")
    # expected vs actual
    share = None
    if ctx is not None and _price(ctx.qty):
        share = min(1.0, q / float(ctx.qty))
    if share is not None:
        for src, dst in (("expected_fee_usd", "expected_fee_usd"),
                         ("expected_cost_usd", "expected_cost_usd")):
            v = _num(getattr(ctx, src))
            if v is not None:
                rec[dst] = v * share
    if rec["shortfall_vs_mid_usd"] is not None and fee_confirmed \
            and fee_usd is not None and _num(fee_usd) is not None:
        rec["actual_cost_usd"] = rec["shortfall_vs_mid_usd"] + float(fee_usd)
        if rec["expected_cost_usd"] is not None:
            rec["cost_variance_usd"] = (rec["actual_cost_usd"]
                                        - rec["expected_cost_usd"])
    elif rec["expected_cost_usd"] is not None:
        reasons.append("ACTUAL_COST_UNAVAILABLE: needs a decision-time book "
                       "and a confirmed fee")
    rec["status"], rec["reasons"] = MEASURED, reasons
    return rec


def failed_submission(ctx: Optional[DecisionContext], *, side: str,
                      symbol: str, product: str, venue: str, order_type: str,
                      requested_qty: float, why: str,
                      order_link_id: str = "", environment: str = "",
                      recorded_ms: Optional[int] = None) -> Dict[str, Any]:
    """A submission that returned nothing or raised: nothing was executed.

    This is execution-OUTCOME evidence (an attempt that did not fill), not a
    cost sample: its status is NO_FILL and it carries no price, so nothing that
    measures filled-order cost can count it."""
    d_ms = _ms(ctx.decision_ms) if ctx else None
    obs = _ms(ctx.observed_ms if ctx.observed_ms is not None
              else ctx.decision_ms) if ctx else None
    return {"schema": SCHEMA, "venue": venue, "symbol": symbol,
            "product": product, "side": side, "order_type": order_type,
            "order_link_id": order_link_id, "status": NO_FILL,
            "reasons": [why], "requested_qty": _price(requested_qty),
            "filled_qty": 0.0, "unfilled_qty": _price(requested_qty),
            "decision_ms": d_ms, "observed_ms": obs,
            "recorded_ms": _ms(recorded_ms),
            "environment": str(environment or ""),
            "book_source": str(ctx.book_source or "") if ctx else "",
            "book_age_ms": (d_ms - obs if d_ms is not None and obs is not None
                            else None),
            "notional_usd": None}


#: What a stored record must state for its figures to be re-derived.
_VERIFY_FIELDS = ("venue", "symbol", "product", "side", "decision_ms",
                  "observed_ms", "decision_bid", "decision_ask", "filled_qty",
                  "requested_qty", "avg_price")
_VERIFY_FIGURES = ("reference_price", "slippage_usd", "spread_cost_usd")


def verify_record(rec: Any, *, entry_ms: Optional[int] = None
                  ) -> Optional[str]:
    """Why a STORED record cannot be trusted as a book-measured sample, or None.

    A record carries a status and figures it wrote about itself; a reader that
    believes them can be handed any number. This re-runs the producer's own
    validation on the fields the record stores (the decision-time book, the
    timestamps, the fill) and requires the same verdict and the same figures.
    A record that does not state those fields, that no longer validates, or
    whose slippage / spread / reference differ from what its own fields give
    is refused. It cannot prove a record was not forged WITH consistent
    fields; it refuses every one that is merely asserted."""
    if not isinstance(rec, dict):
        return "RECORD_UNVERIFIABLE: not a record"
    missing = [k for k in _VERIFY_FIELDS if rec.get(k) in (None, "")]
    if missing:
        return f"RECORD_UNVERIFIABLE: states no {', '.join(missing)}"
    stamps = [_ms(rec.get(k)) for k in ("decision_ms", "submit_ms", "fill_ms")]
    stamps += [_ms(entry_ms)]
    now_ms = max([t for t in stamps if t is not None] or [0])
    try:
        ctx = DecisionContext(
            venue=rec["venue"], symbol=rec["symbol"], product=rec["product"],
            side=rec["side"], decision_ms=rec["decision_ms"],
            qty=rec["requested_qty"], mark=rec.get("decision_mark"),
            bid=rec["decision_bid"], ask=rec["decision_ask"],
            observed_ms=rec["observed_ms"])
        again = assess_fill(
            ctx, side=rec["side"], symbol=rec["symbol"],
            product=rec["product"], venue=rec["venue"],
            order_type=str(rec.get("order_type") or "market"),
            requested_qty=rec["requested_qty"], filled_qty=rec["filled_qty"],
            avg_price=rec["avg_price"], now_ms=now_ms,
            submit_ms=rec.get("submit_ms"), fill_ms=rec.get("fill_ms"))
    except Exception as exc:  # noqa: BLE001 - a bad record is a refusal
        return f"RECORD_UNVERIFIABLE: {type(exc).__name__}"
    if again.get("status") != MEASURED \
            or again.get("reference_basis") != "executable_side":
        why = [r for r in again.get("reasons", [])
               if not str(r).startswith("SPREAD_UNAVAILABLE")]
        return f"RECORD_UNVERIFIABLE: does not validate ({'; '.join(why)[:200]})"
    for key in _VERIFY_FIGURES:
        stored, fresh = _num(rec.get(key)), _num(again.get(key))
        if stored is None or fresh is None or abs(stored - fresh) > \
                1e-9 * max(1.0, abs(fresh)):
            return (f"RECORD_INCONSISTENT: {key} is {rec.get(key)!r} but its "
                    f"own fields give {again.get(key)!r}")
    return None


def reconcile(records: Iterable[Dict[str, Any]],
              booked: Iterable[Dict[str, Any]], *, qty_tol: float = 1e-9,
              fee_tol: float = 1e-9) -> List[str]:
    """Problems between execution records and booked fills (empty = agrees).

    `booked` rows are `LedgerBroker.fills`-shaped: ref, qty, fee_usdt,
    fee_btc, price. Every record with a fill must match a booked fill by
    order link on quantity and, when both state it, fee; every booked fill
    must have a record.
    """
    problems: List[str] = []
    by_ref = {str(b["ref"]): b for b in booked}
    seen = set()
    for r in records:
        link = str(r.get("order_link_id") or "")
        filled = r.get("filled_qty")
        if not filled:
            continue
        seen.add(link)
        b = by_ref.get(link)
        if b is None:
            problems.append(f"{link}: record fills {filled} but nothing booked")
            continue
        if abs(float(b["qty"]) - float(filled)) > qty_tol:
            problems.append(f"{link}: booked qty {b['qty']} != record {filled}")
        fee = r.get("fee_usd")
        if fee is not None:
            booked_fee = float(b.get("fee_usdt", 0.0)) + float(
                b.get("fee_btc", 0.0)) * float(b["price"])
            if abs(booked_fee - float(fee)) > fee_tol:
                problems.append(f"{link}: booked fee {booked_fee} != "
                                f"record {fee}")
    for ref in by_ref:
        if ref not in seen:
            problems.append(f"{ref}: booked fill has no execution record")
    return problems
