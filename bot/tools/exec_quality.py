#!/usr/bin/env python3
"""MEASURED execution quality, per leg and per group. Pure functions: no I/O, no network, no clock, no venue import.

Why. Every entry today is taker/taker; whether a maker-first or staged policy would have earned more net dollars for the SAME economic strategy cannot be decided from a model, only from what the
venue actually did with real orders. This module turns the raw facts one leg produced (what we intended, the reference quote we saw, when we sent and when the venue acknowledged, every venue
execution, the cancel round-trip) into ONE canonical record with ONE sign convention, and aggregates records by policy. It invents nothing: a missing field is the string ``UNKNOWN`` (or None in
numeric slots, with the field named in ``unknown``), never zero, and a maker classification comes only from a venue execution's own ``isMaker`` flag.

SIGN CONVENTION: every cost is POSITIVE when it cost us money relative to the reference, negative when we were paid / improved on it.
    price cost vs mid (USD)  = side_sign * (fill_price - ref_mid) * qty,   side_sign = +1 Buy, -1 Sell      (contains the half spread and any walk: the fill price ALREADY has them)
    walk vs touch (USD)      = side_sign * (fill_price - ref_touch) * qty,  touch = ask for a Buy, bid for a Sell
    half spread (USD)        = (ref_ask - ref_bid) / 2 * qty                                               (price cost vs mid - walk vs touch, for a touch-priced taker)
    fee (USD)                = the venue's signed execution fee (a rebate is NEGATIVE: it is not clamped away)
    total execution cost     = price cost vs mid + fee                                                     (a fee is a SEPARATE charge; it is not in the fill price)
A cost already inside the fill price is therefore counted ONCE (as price cost vs mid); the fee is the only additive charge. A resting order with no execution has no cost and no liquidity class
(NONE): a quote is not a fill and a resting order is not a maker execution. A cancel that timed out is ``UNKNOWN``, never ``CANCELLED``.

Clock basis: ``sent_ms`` / ``acked_ms`` / ``cancel_*_ms`` are the local clock, execution times are the venue's; the session refuses to trade when the two differ by more than 2 s, so a comparison
across the two bases is reported with that tolerance (``clock_tolerance_ms``) and never as exact.
"""
from __future__ import annotations

from decimal import Decimal as D
from statistics import median
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

UNKNOWN = "UNKNOWN"
BPS = D(10000)
CLOCK_TOLERANCE_MS = 2000
MIN_COMPARABLE_ORDERS = 30            # below this a policy comparison is labelled INSUFFICIENT_SAMPLE: no winner, ever
ONE_SIDE = {"Buy": D(1), "Sell": D(-1)}


def _d(x: Any) -> Optional[D]:
    if x is None or isinstance(x, bool) or x == "":
        return None
    try:
        v = D(str(x))
    except Exception:                                                           # noqa: BLE001
        return None
    return v if v.is_finite() else None


def _s(x: Optional[D], places: int = 8) -> Optional[str]:
    if x is None:
        return None
    return format(x.quantize(D(1).scaleb(-places)), "f")


def _i(x: Any) -> Optional[int]:
    try:
        return None if x is None or isinstance(x, bool) else int(x)
    except (TypeError, ValueError):
        return None


#: the oldest reference quote a cost may be measured against, by order kind (ms). An entry is priced against the quote seen as it left; an exit may follow a hold, so it gets a longer bound.
REFERENCE_MAX_AGE_MS = {"ENTRY": 5_000, "EXIT": 60_000}


#: A spot BUY's fee is taken from the coin it receives: the order row's cumulative quantity is GROSS and the execution NET, so a gap this small on a SPOT leg is the fee, not a missing fill. The same
#: constant ``ms_execution`` uses to drop the matching phantom remainder (tests/test_exec_quality.py pins that they are one number).
FEE_IN_BASE_MAX_FRACTION = D("0.002")


def normalise_fill(x: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    """One venue execution as the canonical dict, or None when it has no id / quantity / price (a row we cannot use is dropped, never guessed). ``liquidity`` is MAKER / TAKER only when the venue
    said so, else None. ``fee`` is the SIGNED venue fee in USD (None when absent)."""
    fid = x.get("fill_id") if x.get("fill_id") is not None else x.get("exec_id")
    qty, px = _d(x.get("qty")), _d(x.get("price"))
    if not fid or qty is None or px is None or qty <= 0 or px <= 0:
        return None
    fee = None if x.get("fee_known") is False else (_d(x.get("fee_signed_usd")) if x.get("fee_signed_usd") is not None else _d(x.get("fee_usd")))      # a blank venue fee is UNKNOWN, never zero
    liq = x.get("liquidity")
    return {"fill_id": str(fid), "qty": qty, "price": px, "fee": fee, "fee_ccy": x.get("fee_ccy") or UNKNOWN,
            "fee_rate": _d(x.get("fee_rate")), "liquidity": liq if liq in ("MAKER", "TAKER") else None, "ts_ms": _i(x.get("ts_ms")),
            "side": x.get("side"), "symbol": x.get("symbol")}


def _liquidity(fills: Sequence[Mapping[str, Any]]) -> str:
    if not fills:
        return "NONE"
    kinds = {f["liquidity"] for f in fills}
    if None in kinds:
        return UNKNOWN
    return next(iter(kinds)) if len(kinds) == 1 else "MIXED"


def leg_quality(*, link_id: str, group_id: str, leg_id: str, kind: str, candidate_id: Optional[str], decision_id: Optional[str], plan: Mapping[str, Any],
                ref: Optional[Mapping[str, Any]], timing: Optional[Mapping[str, Any]], order: Optional[Mapping[str, Any]], fills: Iterable[Mapping[str, Any]]) -> Dict[str, Any]:
    """The canonical record of ONE order. ``plan``: side, category, symbol, order_type, time_in_force, qty, price, reduce_only. ``ref``: bid, ask, ts_ms, source (the quote seen when the order was
    decided/sent; None = UNKNOWN). ``timing``: sent_ms, acked_ms, cancel_sent_ms, cancel_acked_ms, cancel_result. ``order``: the venue's order row (status, created_ms, updated_ms, cum_qty,
    avg_price). ``fills``: venue executions (duplicates by fill_id collapse: the same execution seen twice is one execution)."""
    timing, order = dict(timing or {}), dict(order or {})
    unknown: List[str] = []
    seen, fl = set(), []
    dups = 0
    for raw in fills:
        f = normalise_fill(raw)
        if f is None:
            continue
        if f["fill_id"] in seen:
            dups += 1
            continue
        seen.add(f["fill_id"])
        fl.append(f)
    fl.sort(key=lambda f: ((f["ts_ms"] is None), f["ts_ms"] or 0, f["fill_id"]))
    side = plan.get("side")
    sgn = ONE_SIDE.get(str(side))
    planned = _d(plan.get("qty"))
    filled = sum((f["qty"] for f in fl), D(0))
    notional = sum((f["qty"] * f["price"] for f in fl), D(0))
    vwap = (notional / filled) if filled > 0 else None
    bid, ask = _d((ref or {}).get("bid")), _d((ref or {}).get("ask"))
    mid = ((bid + ask) / 2) if (bid is not None and ask is not None and bid > 0 and ask >= bid) else None
    if ref is None or mid is None:
        unknown.append("reference_quote")
    age = _i((ref or {}).get("age_ms_at_send"))
    if mid is not None and age is not None and age > REFERENCE_MAX_AGE_MS.get(str(kind), REFERENCE_MAX_AGE_MS["ENTRY"]):
        mid = None                                                      # a quote that old is not the market the order met: the cost against it is UNKNOWN, never a number
        unknown.append("reference_stale")
    cost_mid = walk = half = None
    slip_mid_bps = slip_touch_bps = None
    if filled > 0 and vwap is not None and sgn is not None and mid is not None:
        cost_mid = sgn * (vwap - mid) * filled
        slip_mid_bps = sgn * (vwap - mid) / mid * BPS
        touch = ask if sgn > 0 else bid
        walk = sgn * (vwap - touch) * filled
        slip_touch_bps = sgn * (vwap - touch) / touch * BPS
        half = (ask - bid) / 2 * filled
    # The ORDER row says how much the venue filled; the executions are what this record prices. When the order row claims more than the executions show (a lagging or dropped execution list) the fee and the
    # price cost of the missing part are UNKNOWN: "no executions seen" is not "no fee". A venue-confirmed nothing (cum_qty 0, or no order row at all) is still a real zero.
    cum = _d(order.get("cum_qty"))
    incomplete = False
    if cum is not None and cum > 0:
        # only a spot BUY's fee is taken from the coin received (the order row's cumulative quantity is then GROSS and the execution NET): a missing SELL execution is never a fee-sized remainder
        tolerance = cum * FEE_IN_BASE_MAX_FRACTION if (str(plan.get("category")) == "spot" and str(plan.get("side")) == "Buy" and filled > 0) else D(0)
        incomplete = cum - filled > tolerance
    elif filled <= 0 and str(order.get("status") or "") in ("Filled", "PartiallyFilled", "PartiallyFilledCanceled"):
        incomplete = True                                               # the venue says (part of) the order traded, yet neither a cumulative quantity nor an execution row says how much
    if incomplete:
        unknown.append("fills_incomplete")
    fee_known = bool(fl) and all(f["fee"] is not None for f in fl)
    fee = None if incomplete else (sum((f["fee"] for f in fl), D(0)) if fee_known else (D(0) if not fl else None))
    if fl and not fee_known:
        unknown.append("fee")
    fee_bps = (fee / notional * BPS) if (fee is not None and notional > 0) else None
    total = (cost_mid + fee) if (cost_mid is not None and fee is not None) else None
    total_bps = (total / notional * BPS) if (total is not None and notional > 0) else None
    liq = _liquidity(fl)
    if liq == UNKNOWN:
        unknown.append("liquidity")
    ack_lat = (timing["acked_ms"] - timing["sent_ms"]) if _i(timing.get("sent_ms")) is not None and _i(timing.get("acked_ms")) is not None else None
    if ack_lat is None:
        unknown.append("ack_latency")
    if _i(timing.get("sent_ms")) is None:
        unknown.append("sent_ms")
    cancel_sent, cancel_ack = _i(timing.get("cancel_sent_ms")), _i(timing.get("cancel_acked_ms"))
    cres = timing.get("cancel_result")
    cancel: Optional[Dict[str, Any]] = None
    if cancel_sent is not None or cres:
        post = [f for f in fl if f["ts_ms"] is not None and cancel_sent is not None and f["ts_ms"] > cancel_sent + CLOCK_TOLERANCE_MS]
        possible = [f for f in fl if f["ts_ms"] is not None and cancel_sent is not None and cancel_sent - CLOCK_TOLERANCE_MS < f["ts_ms"] <= cancel_sent + CLOCK_TOLERANCE_MS]
        cancel = {"sent_ms": cancel_sent, "acked_ms": cancel_ack, "result": cres if cres in ("CANCELLED", "TOO_LATE", "NOT_FOUND") else UNKNOWN,
                  "latency_ms": (cancel_ack - cancel_sent) if (cancel_sent is not None and cancel_ack is not None) else None,
                  "fills_after_request": len(post), "qty_after_request": _s(sum((f["qty"] for f in post), D(0)), 8),
                  "fills_within_clock_tolerance": len(possible), "clock_tolerance_ms": CLOCK_TOLERANCE_MS}
        if cres not in ("CANCELLED", "TOO_LATE", "NOT_FOUND"):
            unknown.append("cancel_result")
    status = order.get("status") if order.get("status") else UNKNOWN
    if status == UNKNOWN:
        unknown.append("order_status")
    venue_created, venue_updated = _i(order.get("created_ms")), _i(order.get("updated_ms"))
    first_fill = fl[0]["ts_ms"] if fl else None
    last_fill = fl[-1]["ts_ms"] if fl else None
    o2f = (first_fill - venue_created) if (first_fill is not None and venue_created is not None) else None     # both on the VENUE clock
    ratio = (filled / planned) if (planned is not None and planned > 0) else None
    if ratio is not None and ratio > 1:
        ratio = None                                                            # more filled than planned is a data fault, not a ratio; flagged below
        unknown.append("fill_ratio")
    over = planned is not None and filled > planned
    return {
        "schema": "exec_quality.leg.v1", "link_id": link_id, "group_id": group_id, "leg_id": leg_id, "kind": kind, "candidate_id": candidate_id, "decision_id": decision_id,
        "instrument": {"category": plan.get("category"), "symbol": plan.get("symbol")}, "side": side, "intent": "REDUCE" if plan.get("reduce_only") else "OPEN",
        "order_type": plan.get("order_type"), "time_in_force": plan.get("time_in_force") or UNKNOWN, "post_only": bool(plan.get("post_only")),
        "planned_qty": _s(planned), "planned_price": _s(_d(plan.get("price"))),
        "reference": None if mid is None else {"bid": _s(bid), "ask": _s(ask), "mid": _s(mid), "ts_ms": _i((ref or {}).get("ts_ms")), "source": (ref or {}).get("source", UNKNOWN),
                                               "age_ms_at_send": _i((ref or {}).get("age_ms_at_send"))},
        "timing": {"sent_ms": _i(timing.get("sent_ms")), "acked_ms": _i(timing.get("acked_ms")), "ack_latency_ms": ack_lat, "venue_created_ms": venue_created, "venue_updated_ms": venue_updated,
                   "first_fill_ms": first_fill, "last_fill_ms": last_fill, "order_to_first_fill_ms": o2f},
        "order": {"venue_order_ref": order.get("order_id"), "status": status, "cum_qty": _s(_d(order.get("cum_qty"))), "avg_price": _s(_d(order.get("avg_price")))},
        "fills": {"count": len(fl), "duplicate_events_collapsed": dups, "qty": _s(filled), "vwap": _s(vwap), "notional": _s(notional), "fill_ratio": _s(ratio, 6),
                  "overfilled": bool(over), "ids": [f["fill_id"] for f in fl]},
        "liquidity": liq, "cancel": cancel,
        "fee": {"usd_signed": _s(fee), "bps_of_notional": _s(fee_bps, 4), "currencies": sorted({f["fee_ccy"] for f in fl}),
                "rate_provenance": (UNKNOWN if incomplete else "VENUE_EXECUTION_ROW" if fl and all(f["fee_rate"] is not None for f in fl) else UNKNOWN if fl else "NO_FILL")},
        "cost": {"price_vs_mid_usd": _s(cost_mid), "walk_vs_touch_usd": _s(walk), "half_spread_usd": _s(half), "fee_usd": _s(fee), "total_usd": _s(total),
                 "slippage_vs_mid_bps": _s(slip_mid_bps, 4), "slippage_vs_touch_bps": _s(slip_touch_bps, 4), "total_bps": _s(total_bps, 4),
                 "convention": "positive = cost to us relative to the reference; fee is separate from the fill price"},
        "unknown": sorted(set(unknown)),
    }


def group_quality(legs: Sequence[Mapping[str, Any]], *, style: Optional[str], notional_usd: Optional[Any], flat_verified: Optional[bool], decision_id: Optional[str] = None,
                  unwind_ms: Optional[Tuple[Optional[int], Optional[int]]] = None) -> Dict[str, Any]:
    """The whole attempt: summed measured cost over every leg (entries AND exits), the unhedged window and the residual. ``attempt_completed`` is True only when the venue proved flat; an attempt
    that failed its hedge and was unwound still carries its (real) cost, so a policy that fills cheaply but unwinds often is not flattered."""
    entries = [l for l in legs if l.get("kind") == "ENTRY"]
    exits = [l for l in legs if l.get("kind") == "EXIT"]
    totals = [_d(l["cost"]["total_usd"]) for l in legs if l["fills"]["count"] > 0 or "fills_incomplete" in l.get("unknown", [])]      # a leg the venue says filled but whose executions are missing is UNKNOWN, not absent
    complete = all(t is not None for t in totals)
    total = sum((t for t in totals if t is not None), D(0)) if complete else None
    notional_sum = sum((_d(l["fills"]["notional"]) or D(0) for l in entries), D(0))
    n_filled = sum(1 for l in entries if l["fills"]["count"] > 0)
    notional = (notional_sum / n_filled) if n_filled else D(0)        # the POSITION notional: mean filled entry-leg notional (each leg of a hedged pair is ~the same dollars), the basis the cost model's round-trip bps use
    first = {}
    for l in entries:
        f = l["timing"]["first_fill_ms"]
        la = l["timing"]["last_fill_ms"]
        if f is not None and la is not None:
            first[l["leg_id"]] = (f, la)
    window = None
    if len(first) >= 2:
        window = max(v[1] for v in first.values()) - min(v[0] for v in first.values())      # first fill of the first leg to the last fill of the last: the unhedged span
    resid: Dict[str, D] = {}
    for l in legs:
        key = f"{l['instrument']['category']}:{l['instrument']['symbol']}"
        q = _d(l["fills"]["qty"]) or D(0)
        resid[key] = resid.get(key, D(0)) + q * ONE_SIDE.get(str(l["side"]), D(0))
    exit_first = min((l["timing"]["first_fill_ms"] for l in exits if l["timing"]["first_fill_ms"] is not None), default=None)
    exit_last = max((l["timing"]["last_fill_ms"] for l in exits if l["timing"]["last_fill_ms"] is not None), default=None)
    filled_legs = [l for l in entries if l["fills"]["count"] > 0]
    hedged = bool(entries) and len(filled_legs) == len(entries) and all((_d(l["fills"]["fill_ratio"]) or D(0)) > 0 for l in entries)
    return {
        "schema": "exec_quality.group.v1", "group_id": legs[0]["group_id"] if legs else None, "decision_id": decision_id, "style": style or UNKNOWN,
        "planned_notional_usd": _s(_d(notional_usd), 2), "entry_notional_filled_usd": _s(notional_sum, 4), "position_notional_usd": _s(notional, 4), "legs": len(legs), "entry_legs_filled": len(filled_legs),
        "entry_legs_planned": len(entries), "exit_legs": len(exits), "all_entry_legs_filled": hedged, "unhedged_window_ms": window,
        "unwind_first_fill_ms": exit_first if unwind_ms is None else unwind_ms[0], "unwind_last_fill_ms": exit_last if unwind_ms is None else unwind_ms[1],
        "residual_qty_by_instrument": {k: _s(v, 8) for k, v in sorted(resid.items())}, "venue_flat_verified": flat_verified is True,
        "attempt_completed": flat_verified is True and bool(legs),
        "total_execution_cost_usd": _s(total), "total_execution_cost_bps_of_position_notional": _s((total / notional * BPS), 4) if (total is not None and notional > 0) else None,
        "cost_complete": bool(complete and legs), "unknown": sorted({u for l in legs for u in l.get("unknown", [])}),
    }


def session_quality(legs: Sequence[Mapping[str, Any]], *, style: Optional[str], notional_usd: Optional[Any], flat_verified: Optional[bool], decision_id: Optional[str] = None) -> Dict[str, Any]:
    return {"legs": [dict(l) for l in legs], "group": group_quality(legs, style=style, notional_usd=notional_usd, flat_verified=flat_verified, decision_id=decision_id)}


def policy_summary(groups: Sequence[Mapping[str, Any]], *, min_orders: int = MIN_COMPARABLE_ORDERS) -> Dict[str, Any]:
    """Aggregate group attempts by policy (``style``): how many attempts, how many completed, the notional-weighted execution cost in bps of POSITION notional (mean filled entry-leg notional), its dispersion, and the cost per
    COMPLETED attempt. It reports a difference between two policies only as a number with its sample size; ``verdict`` is ``INSUFFICIENT_SAMPLE`` until BOTH have ``min_orders`` completed attempts
    with complete costs, and even then it is a measured cost difference on this sample, not an expectancy claim (Testnet fills are not Mainnet fills; a maker policy's UNFILLED attempts
    are counted in ``completion_rate`` so a cheap-when-filled policy is not flattered)."""
    by: Dict[str, List[Mapping[str, Any]]] = {}
    for g in groups:
        by.setdefault(str(g.get("style") or UNKNOWN), []).append(g)
    rows: Dict[str, Dict[str, Any]] = {}
    for style, gs in sorted(by.items()):
        # the model prices a FULL hedged pair: a one-legged attempt incurred about half that cost per unit of notional, so counting it would flatter the policy (feedback_report already filtered it)
        done = [g for g in gs if g.get("attempt_completed") and g.get("cost_complete") and g.get("all_entry_legs_filled") is True
                and g.get("total_execution_cost_bps_of_position_notional") is not None]
        bps = [_d(g["total_execution_cost_bps_of_position_notional"]) for g in done]
        bps = [b for b in bps if b is not None]
        usd = [_d(g["total_execution_cost_usd"]) for g in done]
        usd = [u for u in usd if u is not None]
        notl = [_d(g["position_notional_usd"]) or D(0) for g in done]
        wsum = sum(notl, D(0))
        wmean = (sum((b * n for b, n in zip(bps, notl)), D(0)) / wsum) if wsum > 0 else None
        mean = (sum(bps, D(0)) / len(bps)) if bps else None
        sd = None
        if len(bps) >= 2 and mean is not None:
            sd = (sum(((b - mean) ** 2 for b in bps), D(0)) / (len(bps) - 1)).sqrt()
        se = (sd / D(len(bps)).sqrt()) if sd is not None else None
        rows[style] = {"attempts": len(gs), "completed_with_complete_costs": len(done), "completion_rate": _s(D(len(done)) / D(len(gs)), 4),
                       "mean_cost_bps": _s(mean, 4), "notional_weighted_cost_bps": _s(wmean, 4), "std_error_bps": _s(se, 4),
                       "mean_cost_usd_per_completed": _s((sum(usd, D(0)) / len(usd)) if usd else None, 6),
                       "median_cost_bps": _s(median(bps) if bps else None, 4)}
    styles = sorted(rows)
    enough = len(styles) >= 2 and all(rows[s]["completed_with_complete_costs"] >= min_orders for s in styles)
    return {"schema": "exec_quality.policy.v1", "policies": rows, "min_orders_per_policy": min_orders, "verdict": "COMPARABLE_SAMPLE" if enough else "INSUFFICIENT_SAMPLE",
            "claim": "measured execution cost on this sample only; Testnet fills are not Mainnet fills and an unfilled maker attempt is not a zero-cost one",
            "policies_with_data": styles}


# ---------------------------------------------------------------------------------------------------- research feedback: measured vs modelled, session by session
#: the model's cost lines that EXECUTION measurement covers (fees, spread, taker walk / slippage). Impact, basis stress, hedge error, adverse selection and borrow are other things: they are not
#: comparable with fees + spread + walk and would bias the "surprise" if summed in.
_EXEC_COST_WORDS = ("fee", "spread", "slip")


def _is_execution_cost_line(name: Any) -> bool:
    low = str(name).lower()
    return any(w in low for w in _EXEC_COST_WORDS)


def feedback_row(*, session: str, at_ms: int, decision_id: Optional[str], quality: Optional[Mapping[str, Any]], funding: Optional[Mapping[str, Any]], edge: Optional[Mapping[str, Any]],
                 flat_verified: Optional[bool]) -> Dict[str, Any]:
    """One chained row per finished session: the MEASURED execution cost of the whole attempt beside the cost the economic model charged for it, and the funding the venue paid beside the funding
    the model expected. Everything is a string / number / None (None = UNKNOWN); nothing is estimated here. ``modelled_cost_bps`` is the sum of the verdict's cost lines that execution measurement covers (fee / spread / slippage lines; None when none exists or any is unknown);
    ``expected_funding_bps`` is the verdict's post-haircut expectation over the hold; ``realised_funding_bps`` exists only when the settlement recognition booked every held settlement."""
    g = dict((quality or {}).get("group") or {})
    allc = dict((edge or {}).get("costs_bps") or {})
    costs = {k: v for k, v in allc.items() if _is_execution_cost_line(k)}        # like for like: the model's lines that a leg's measured fee / spread / walk actually cover
    cv = [_d(v) for v in costs.values()]
    modelled = None if (not costs or any(c is None for c in cv)) else sum(cv, D(0))
    booked, notional = None, _d(g.get("position_notional_usd"))
    for r in list((funding or {}).get("recognitions") or []):
        if isinstance(r, Mapping) and r.get("all_prints_resolved") and r.get("booked_usd") is not None:
            booked = (booked or D(0)) + (_d(r.get("booked_usd")) or D(0))
    realised = (booked / notional * BPS) if (booked is not None and notional is not None and notional > 0) else None
    return {"schema": "exec_quality.feedback.v1", "at_ms": int(at_ms), "session": str(session)[:80], "decision_id": decision_id, "style": g.get("style") or UNKNOWN,
            "flat_verified": flat_verified is True, "attempt_completed": bool(g.get("attempt_completed")), "cost_complete": bool(g.get("cost_complete")),
            "all_entry_legs_filled": g.get("all_entry_legs_filled"),
            "position_notional_usd": g.get("position_notional_usd"), "total_execution_cost_usd": g.get("total_execution_cost_usd"),
            "total_execution_cost_bps_of_position_notional": g.get("total_execution_cost_bps_of_position_notional"), "unhedged_window_ms": g.get("unhedged_window_ms"),
            "modelled_cost_bps": _s(modelled, 4), "expected_funding_bps": _s(_d((edge or {}).get("expected_funding_bps")), 4), "funding_booked_usd": _s(booked, 6),
            "realised_funding_bps": _s(realised, 4)}


def feedback_report(rows: Sequence[Mapping[str, Any]], *, min_orders: int = MIN_COMPARABLE_ORDERS) -> Dict[str, Any]:
    """What the log of sessions says: the policy comparison (never a winner below the sample minimum), how far the cost model is from measurement (positive surprise = the model was too
    optimistic about cost) and how realised funding compares with the expectation. Counts every row it uses; says INSUFFICIENT_SAMPLE instead of an average from a handful."""
    done = [r for r in rows if r.get("attempt_completed") and r.get("cost_complete") and r.get("all_entry_legs_filled") is True        # the model prices a FULL hedged pair: a one-legged attempt incurred about half that cost
            and r.get("total_execution_cost_bps_of_position_notional") is not None and r.get("modelled_cost_bps") is not None]
    sur = [_d(r["total_execution_cost_bps_of_position_notional"]) - _d(r["modelled_cost_bps"]) for r in done]
    fund = [r for r in rows if r.get("realised_funding_bps") is not None and r.get("expected_funding_bps") is not None]
    fs = [_d(r["realised_funding_bps"]) - _d(r["expected_funding_bps"]) for r in fund]
    mean = lambda xs: _s((sum(xs, D(0)) / len(xs)) if xs else None, 4)        # noqa: E731
    return {"schema": "exec_quality.report.v1", "sessions_logged": len(rows), "policy": policy_summary(rows, min_orders=min_orders),
            "model_vs_measured": {"sessions": len(done), "mean_measured_cost_bps": mean([_d(r["total_execution_cost_bps_of_position_notional"]) for r in done]),
                                  "mean_modelled_cost_bps": mean([_d(r["modelled_cost_bps"]) for r in done]), "mean_cost_surprise_bps": mean(sur),
                                  "sessions_costlier_than_modelled": sum(1 for x in sur if x > 0), "status": "ENOUGH" if len(done) >= min_orders else "INSUFFICIENT_SAMPLE",
                                  "reading": "like for like: measured fees + spread + walk against the model's fee / spread / slippage lines (impact, basis, hedge error, adverse selection and borrow are excluded); positive = the model under-charged execution cost"},
            "funding_vs_expected": {"sessions": len(fund), "mean_realised_bps": mean([_d(r["realised_funding_bps"]) for r in fund]), "mean_expected_bps": mean([_d(r["expected_funding_bps"]) for r in fund]),
                                    "mean_shortfall_bps": mean(fs), "status": "ENOUGH" if len(fund) >= min_orders else "INSUFFICIENT_SAMPLE"},
            "claim": "Testnet measurements describe this execution path, not Mainnet expectancy"}
