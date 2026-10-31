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

Only FILLED quantity is ever measured. Unfilled quantity is reported as
unfilled and contributes nothing. Prices/timestamps that are non-finite,
non-positive, stale, in the future, or observed after the decision make the
price section UNAVAILABLE; a value is never invented or taken from a later
observation.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Tuple

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
#: A mark further than this from the book mid is contradictory data.
MAX_MARK_MID_DEVIATION = 0.05
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

    def to_dict(self) -> Dict[str, Any]:
        return {k: getattr(self, k) for k in (
            "venue", "symbol", "product", "side", "decision_ms", "qty",
            "mark", "bid", "ask", "observed_ms", "order_type",
            "expected_fee_usd", "expected_cost_usd")}


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


def validate_decision(ctx: Optional[DecisionContext], *, now_ms: int,
                      submit_ms: Optional[int] = None,
                      max_decision_age_ms: int = MAX_DECISION_AGE_MS,
                      max_observation_age_ms: int = MAX_OBSERVATION_AGE_MS
                      ) -> Prices:
    """Accept the decision-time prices only if they can be trusted."""
    if ctx is None:
        return Prices(False, reasons=["NO_DECISION_CONTEXT"])
    why: List[str] = []
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
    obs = ctx.observed_ms if ctx.observed_ms is not None else ctx.decision_ms
    o_ms = _ms(obs)
    if o_ms is None:
        why.append("OBSERVATION_TIME_INVALID")
    elif d_ms is not None:
        if o_ms > d_ms + FUTURE_SKEW_MS:
            # Observed AFTER the decision: that is a later observation, and a
            # decision price is never rebuilt from one.
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
    if bid is not None and ask is not None and bid > ask:
        why.append("BOOK_CROSSED")
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


def cost_usd(side: str, fill: float, ref: float, qty: float) -> float:
    """Positive = adverse. The one place the sign is decided."""
    return ((fill - ref) if side == BUY else (ref - fill)) * qty


def assess_fill(ctx: Optional[DecisionContext], *, side: str, symbol: str,
                product: str, venue: str, order_type: str,
                requested_qty: Optional[float], filled_qty: Any,
                avg_price: Any, now_ms: int,
                fee_usd: Optional[float] = None, fee_confirmed: bool = False,
                order_link_id: str = "", submit_ms: Optional[int] = None,
                fill_ms: Optional[int] = None, fill_ms_basis: str = "",
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
                           max_observation_age_ms=max_observation_age_ms)
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
    }
    if ctx is not None:
        rec["observed_ms"] = _ms(ctx.observed_ms if ctx.observed_ms is not None
                                 else ctx.decision_ms)
    if not px.ok:
        reasons.extend(px.reasons)
    else:
        rec.update(decision_bid=px.bid, decision_ask=px.ask,
                   decision_mark=px.mark, reference_price=px.reference,
                   reference_basis=px.basis)

    if qty_ok and filled == 0.0:
        rec["status"], rec["reasons"] = NO_FILL, reasons
        return rec
    measurable = qty_ok and px.ok and avg is not None
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
                      order_link_id: str = "") -> Dict[str, Any]:
    """A submission that returned nothing or raised: nothing was executed."""
    return {"schema": SCHEMA, "venue": venue, "symbol": symbol,
            "product": product, "side": side, "order_type": order_type,
            "order_link_id": order_link_id, "status": NO_FILL,
            "reasons": [why], "requested_qty": _price(requested_qty),
            "filled_qty": 0.0, "unfilled_qty": _price(requested_qty),
            "decision_ms": _ms(ctx.decision_ms) if ctx else None}


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
