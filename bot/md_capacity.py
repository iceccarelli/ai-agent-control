"""How big can this trade be, and what does each size cost? From the displayed book only.

The carry earns a small, uncertain amount per settlement. Whether that is worth
anything depends on what it costs to get in and out at the SIZE you would trade, and
on how much size the book will take at all. This turns one validated market view into
that curve, so the question "can this scale, and where does depth stop paying" is
answered by recorded numbers instead of by hope.

Every number keeps its class (see ``md_economics``):

  CALCULATED  spread and book-walk slippage for the overlay round trip (perp sell to
              enter, perp buy to exit), walked over the DISPLAYED levels. A size the
              displayed book cannot fill is UNAVAILABLE, never a worse price.
  ESTIMATED   taker fees (the published tier unless the account's is supplied), and the
              break-even in settlements, which assumes today's funding forecast holds.
  UNKNOWN     latency, borrow, the account's real fee tier: not added, not defaulted.

Costs here are therefore a LOWER BOUND on the cost of trading that size. A curve that
looks cheap proves nothing about profit; one that is expensive or unfillable at a size
is a hard limit on it.

Pure: no I/O, no clock, no network.
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Mapping, Optional, Sequence

import md_economics as ME

#: The capacity record schema. v2 adds provenance (venue/environment/category/symbol, the
#: source market-event identity, exchange and local clocks, funding clock, book depth, fee
#: provenance, model version) so a row can be validated, chained and traced to the log.
#: v1 rows (no `schema` key) predate it and are quarantined by `capacity_schema`.
SCHEMA = "capacity/2"
MODEL_VERSION = "md_capacity/2"
VENUE = "bybit"
CATEGORY = "linear"

#: Position sizes (USD notional) the curve is evaluated at.
DEFAULT_NOTIONALS = (1_000.0, 10_000.0, 50_000.0, 100_000.0, 500_000.0, 1_000_000.0, 5_000_000.0)


def _fin(x: Any) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def capacity_row(view: Any, *, now_ms: int, notionals: Sequence[float] = DEFAULT_NOTIONALS,
                 fees: Optional[ME.FeeSchedule] = None, mode: str = ME.OVERLAY,
                 source: Optional[Mapping[str, Any]] = None,
                 health: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    """One record: the market at this instant and the cost curve across sizes.

    ``view`` is a ``md_state.MarketView``. ``source`` names the market event this view was
    computed at (the log's record id and the books' update ids / sequence numbers) and
    ``health`` is the recorder's own condition at that instant. The record is plain
    JSON-able data."""
    fees = fees or ME.FeeSchedule.published_default()
    ref = view.perp_mid if _fin(getattr(view, "perp_mid", None)) else None
    funding_bps = None if view.funding_rate is None else float(view.funding_rate) * 1e4
    basis_bps = None
    if _fin(view.perp_mark) and _fin(getattr(view, "spot_mid", None)) and view.spot_mid:
        basis_bps = (float(view.perp_mark) - float(view.spot_mid)) / float(view.spot_mid) * 1e4
    perp_legs = 2 if mode == ME.OVERLAY else 2
    fee_bps = perp_legs * fees.perp_taker_bps + (2 * fees.spot_taker_bps if mode == ME.ACQUIRE else 0.0)

    depth = {"perp_bids": view.perp_bids, "perp_asks": view.perp_asks,
             "spot_bids": view.spot_bids, "spot_asks": view.spot_asks}
    curve: List[Dict[str, Any]] = []
    max_fillable = None
    for notional in notionals:
        point: Dict[str, Any] = {"notional_usd": float(notional)}
        if ref is None:
            point.update(status=ME.UNAVAILABLE, reasons=["NO_MID_PRICE"])
            curve.append(point)
            continue
        dc = ME.depth_costs(depth, mode=mode, notional_usd=float(notional), ref_price=ref,
                            latency_allowance_bps=0.0)
        # latency 0.0 is passed only so the walk is MEASURED; it is NOT an allowance and
        # is reported as such below (the real latency is UNKNOWN).
        if dc.status != ME.MEASURED:
            point.update(status=ME.UNAVAILABLE, reasons=list(dc.reasons))
            curve.append(point)
            continue
        book_bps = float(dc.spread_bps) + float(dc.slippage_bps)
        total_bps = book_bps + fee_bps
        point.update(
            status=ME.MEASURED, reasons=[],
            spread_bps=float(dc.spread_bps), slippage_bps=float(dc.slippage_bps),
            book_round_trip_bps=book_bps, fee_round_trip_bps=fee_bps,
            total_round_trip_bps=total_bps,
            breakeven_settlements=(total_bps / funding_bps) if funding_bps and funding_bps > 0 else None)
        max_fillable = float(notional)
        curve.append(point)
    src = dict(source or {})
    rid = src.get("store_record_id")
    lp = getattr(view, "last_funding_print", None)
    row = {
        "schema": SCHEMA, "model_version": MODEL_VERSION, "venue": VENUE,
        "category": CATEGORY, "environment": view.environment, "symbol": view.symbol, "mode": mode,
        "row_id": None if rid is None else f"{view.environment}:{view.symbol}:{int(rid)}",
        "source": {"store_record_id": rid, "perp_update_id": src.get("perp_update_id"),
                   "perp_seq": src.get("perp_seq"), "spot_update_id": src.get("spot_update_id"),
                   "spot_seq": src.get("spot_seq")},
        "at_ms": int(now_ms),                       # local receive/observation time of this row
        "times": {"observed_wall_ms": getattr(view, "observed_wall_ms", None),
                  "perp_book_exch_ts_ms": view.perp_book_exch_ts_ms,
                  "spot_book_exch_ts_ms": view.spot_book_exch_ts_ms,
                  "perp_book_age_s": view.perp_book_age_s, "leg_span_s": view.leg_span_s,
                  "clock_skew_ms": view.clock_skew_ms},
        "funding": {"forecast_bps": funding_bps, "next_settlement_ms": view.next_funding_ms,
                    "interval_h": view.funding_interval_h,
                    "last_print_bps": None if lp is None else float(lp[0]) * 1e4,
                    "last_print_settled_ms": None if lp is None else int(lp[1])},
        "book": {"perp_bid_levels": len(view.perp_bids), "perp_ask_levels": len(view.perp_asks),
                 "spot_bid_levels": len(view.spot_bids), "spot_ask_levels": len(view.spot_asks),
                 "perp_best_bid": view.perp_bid, "perp_best_ask": view.perp_ask},
        "fees": {"source": fees.source, "perp_taker_bps": fees.perp_taker_bps,
                 "spot_taker_bps": fees.spot_taker_bps},
        "perp_mark": view.perp_mark, "perp_mid": ref, "basis_bps": basis_bps,
        "funding_forecast_bps_per_settlement": funding_bps, "next_funding_ms": view.next_funding_ms,
        "fee_source": fees.source, "latency_bps": "UNKNOWN (not added)",
        "max_displayed_fillable_notional_usd": max_fillable,
        "curve": curve,
        "classes": {"spread_slippage": ME.CALCULATED, "fees": ME.ESTIMATED,
                    "breakeven_settlements": ME.ESTIMATED, "latency_borrow_account_tier": ME.UNKNOWN},
        "note": "lower bound on cost; says nothing about profit",
    }
    if health is not None:
        row["recorder"] = dict(health)
    return row


def affordable_notional(row: Mapping[str, Any], budget_bps: float) -> Optional[float]:
    """The largest evaluated size whose TOTAL round-trip cost is within `budget_bps`
    (None if none is). Sizes between points are not interpolated."""
    best = None
    for p in row.get("curve", ()):
        if p.get("status") == ME.MEASURED and p["total_round_trip_bps"] <= budget_bps:
            best = max(best or 0.0, float(p["notional_usd"]))
    return best
