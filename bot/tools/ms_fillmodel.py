"""ms_fillmodel: a CONSERVATIVE maker-first fill model for REPLAY and PAPER (pure; no venue, no clock, no network).

Every fill it produces is MODELLED (``Fill.prov == "MODELLED"``): nothing here is an observed execution. The rules are deliberately pessimistic and each says so:

* A post-only limit order fills ONLY when the opposite side trades THROUGH its price STRICTLY (a buy at P needs trades at p < P; a sell at P needs p > P). A quote touch, or a
  trade AT the price, is NOT a fill and earns no queue credit.
* Queue ahead = the visible size at that exact price at placement x ``queue_pessimism`` (>= 1). The cumulative through-volume must first exceed it; only the excess fills us,
  capped at our size (so partial fills are proportional to the volume that really went through). If the price is beyond the visible depth, the whole visible side is assumed ahead.
* A book older than ``stale_quote_ms`` (or of unknown age) refuses the placement. A post-only order that would cross is refused (as the venue does).
* Placement and cancel are not instant: an order is live from ``placed + place_latency_ms``; a cancel takes effect ``cancel_latency_ms`` after it was requested, and trades in that
  window STILL fill it (the cancel race is modelled, not assumed away).
* Adverse selection: each fill carries a markout (price moves against the fill) drawn from a stated deterministic distribution (exponential, mean ``markout_mean_bps``, capped).
* Fees: maker figure is a MODELLED scenario input, taker figures are the carry_costs constants. A rebate is never assumed: ``fee_usd >= 0`` always.
* Taker orders walk the visible book (spread + depth) and pay an extra modelled latency slippage; they can fill short when depth is exhausted.
* COLLECTOR-BACKED PATH (``place_maker_from_micro`` / ``advance_with_trades`` / ``taker_from_micro`` / ``capacity_from_micro``, bottom of this module): a maker order needs a
  contemporaneous VALID recorded snapshot (``ms_microstructure``), takes its queue from the VISIBLE size at the price x ``queue_pessimism``, and is filled ONLY by recorded
  opposing-side trades strictly through the price in excess of that queue. A recorded-tape gap is UNKNOWN, never "no trades": the order is closed and nothing fills (REJECTED,
  TAPE_GAP). Every result is labelled MEASURED_BOOK_TAPE (verified collector data), MODELLED (unverified data or a modelled markout) or REJECTED, with reason codes.
* Bars (high/low without trades) cannot support a queue model. ``bar_policy="NO_FILL"`` (default) fills nothing from a bar; ``ASSUME_THROUGH_MULT`` is an explicit, ASSUMED
  optimism knob and the fill carries that note.
"""
from __future__ import annotations

import hashlib
import math
import os
import sys
from dataclasses import dataclass, replace
from typing import List, Optional, Sequence, Tuple

BOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (BOT, os.path.join(BOT, "tools")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import carry_costs as CC  # noqa: E402
import ms_types as T  # noqa: E402

EPS = 1e-12
MODEL_NOTES = ("MODELLED_FILL", "STRICT_THROUGH_REQUIRED", "QUEUE_AHEAD_PESSIMISTIC", "NO_REBATE_ASSUMED")


@dataclass(frozen=True)
class FillParams:
    queue_pessimism: float = 1.5
    place_latency_ms: int = 150
    cancel_latency_ms: int = 300
    stale_quote_ms: int = 5_000
    markout_mean_bps: float = 1.0
    markout_cap_bps: float = 10.0
    markout_seed: str = "ms-fill-v1"
    maker_bps_perp: float = CC.MAKER_BPS_PERP_MODELLED
    maker_bps_spot: float = CC.TAKER_BPS_SPOT          # spot maker is unmeasured: pessimistically the taker figure
    taker_bps_perp: float = CC.TAKER_BPS_PERP
    taker_bps_spot: float = CC.TAKER_BPS_SPOT
    taker_extra_slip_bps: float = 0.5
    bar_policy: str = "NO_FILL"                       # NO_FILL | ASSUME_THROUGH_MULT
    bar_through_mult: float = 0.0
    bar_volume_frac: float = 0.1

    def __post_init__(self) -> None:
        if not (self.queue_pessimism >= 1.0) or self.place_latency_ms < 0 or self.cancel_latency_ms < 0 or self.stale_quote_ms < 0:
            raise ValueError("fill params: queue_pessimism >= 1 and non-negative latencies required")
        if min(self.maker_bps_perp, self.maker_bps_spot, self.taker_bps_perp, self.taker_bps_spot, self.taker_extra_slip_bps, self.markout_mean_bps, self.markout_cap_bps) < 0:
            raise ValueError("fill params: fees, slippage and markout must be >= 0 (rebates are never assumed)")
        if self.bar_policy not in ("NO_FILL", "ASSUME_THROUGH_MULT"):
            raise ValueError("bar_policy")


@dataclass(frozen=True)
class Tick:
    ts_ms: int
    price: float
    qty: float


@dataclass(frozen=True)
class Bar:
    ts_ms: int                      # the time the bar is KNOWN (its close)
    low: float
    high: float
    volume_base: Optional[float] = None


@dataclass(frozen=True)
class SimOrder:
    order_id: str
    group_id: str
    leg_id: str
    symbol: str
    category: str
    side: str
    qty: float
    price: float
    placed_ms: int
    active_from_ms: int
    queue_ahead: float
    queue_basis: str
    filled: float = 0.0
    through_cum: float = 0.0
    last_ms: int = 0
    n_fills: int = 0
    cancel_req_ms: Optional[int] = None
    closed: bool = False
    micro_ref: str = ""             # "<symbol>@<cycle>:<book ts>" when placed from a recorded snapshot (place_maker_from_micro); "" otherwise

    @property
    def remaining(self) -> float:
        return max(0.0, self.qty - self.filled)

    def cancel_effective_ms(self, p: FillParams) -> Optional[int]:
        return None if self.cancel_req_ms is None else self.cancel_req_ms + p.cancel_latency_ms


@dataclass(frozen=True)
class SimFill:
    fill: T.Fill
    markout_bps: float
    markout_usd: float
    slippage_usd: float = 0.0       # taker only: spread + depth walk + latency, versus the mid
    queue_basis: str = ""
    notes: Tuple[str, ...] = MODEL_NOTES


def fee_bps(liquidity: str, category: str, p: FillParams) -> float:
    if liquidity == "MAKER":
        return p.maker_bps_spot if category == "spot" else p.maker_bps_perp
    return p.taker_bps_spot if category == "spot" else p.taker_bps_perp


def fee_usd(notional_usd: float, liquidity: str, category: str, p: FillParams) -> float:
    return max(0.0, abs(notional_usd) * fee_bps(liquidity, category, p) / 1e4)


def markout_bps(order_id: str, n: int, p: FillParams) -> float:
    """Deterministic adverse markout (bps >= 0): exponential(mean) from a hash of (seed, order, fill index), capped. MODELLED, not measured."""
    h = hashlib.sha256(f"{p.markout_seed}|{order_id}|{n}".encode()).digest()
    u = (int.from_bytes(h[:8], "big") + 0.5) / 2.0 ** 64
    return min(p.markout_cap_bps, -p.markout_mean_bps * math.log(1.0 - u)) if p.markout_mean_bps > 0 else 0.0


def _queue_ahead(side: str, price: float, bids: Sequence[T.BookLevel], asks: Sequence[T.BookLevel], p: FillParams) -> Tuple[float, str]:
    levels = bids if side == "Buy" else asks
    if not levels:
        return float("inf"), "NO_BOOK"
    best = levels[0].price
    improves = price > best + EPS if side == "Buy" else price < best - EPS
    if improves:
        return 0.0, "IMPROVES_TOUCH"
    for lv in levels:
        if abs(lv.price - price) <= EPS:
            return lv.qty * p.queue_pessimism, "VISIBLE_AT_PRICE"
    deepest = levels[-1].price
    beyond = price < deepest - EPS if side == "Buy" else price > deepest + EPS
    if beyond:
        return sum(lv.qty for lv in levels) * p.queue_pessimism, "BEYOND_VISIBLE_DEPTH_PESSIMISTIC"
    return 0.0, "EMPTY_LEVEL_INSIDE_VISIBLE"


def place_maker(*, order_id: str, group_id: str, leg_id: str, symbol: str, category: str, side: str, qty: float, price: float,
                bids: Sequence[T.BookLevel], asks: Sequence[T.BookLevel], book_age_ms: Optional[int], now_ms: int,
                params: FillParams) -> Tuple[Optional[SimOrder], str]:
    """(order, "") or (None, reason). Reasons: BAD_QTY_OR_PRICE, NO_BOOK, STALE_QUOTE, POST_ONLY_WOULD_CROSS."""
    if not (qty > 0 and price > 0) or not math.isfinite(qty) or not math.isfinite(price) or side not in ("Buy", "Sell"):
        return None, "BAD_QTY_OR_PRICE"
    if not bids or not asks:
        return None, "NO_BOOK"
    if book_age_ms is None or book_age_ms > params.stale_quote_ms:
        return None, "STALE_QUOTE"
    if (side == "Buy" and price >= asks[0].price - EPS) or (side == "Sell" and price <= bids[0].price + EPS):
        return None, "POST_ONLY_WOULD_CROSS"
    q, basis = _queue_ahead(side, price, bids, asks, params)
    active = now_ms + params.place_latency_ms
    return SimOrder(order_id, group_id, leg_id, symbol, category, side, float(qty), float(price), now_ms, active, q, basis, last_ms=now_ms), ""


def request_cancel(order: SimOrder, now_ms: int) -> SimOrder:
    return order if order.cancel_req_ms is not None else replace(order, cancel_req_ms=now_ms)


def bar_ticks(bar: Bar, order: SimOrder, p: FillParams) -> List[Tick]:
    """What a bar can say about trades through the order's price. ``bar_policy="NO_FILL"`` => NOTHING, whatever the bar's volume. Under the explicit ASSUMED knob: with a volume the
    through-volume is ``volume * bar_volume_frac`` scaled by the portion of the bar's range that lies THROUGH the order's price (uniform volume over [low, high], the portion below a
    buy / above a sell: a bar that never went through the price has none); with no volume it is ``bar_through_mult * (queue ahead + qty)``."""
    if p.bar_policy != "ASSUME_THROUGH_MULT":
        return []
    buy = order.side == "Buy"
    px = bar.low if buy else bar.high
    if not (px < order.price - EPS if buy else px > order.price + EPS):
        return []                                                                # the bar never traded strictly through the limit
    if bar.volume_base is not None and bar.volume_base > 0:
        span = bar.high - bar.low
        through = (order.price - bar.low) if buy else (bar.high - order.price)
        portion = 1.0 if span <= EPS else min(1.0, max(0.0, through / span))
        q = bar.volume_base * p.bar_volume_frac * portion
    else:
        q = p.bar_through_mult * (order.queue_ahead + order.qty)
    return [Tick(bar.ts_ms, px, q)] if q > 0 else []


def advance(order: SimOrder, ticks: Sequence[Tick], upto_ms: int, params: FillParams) -> Tuple[SimOrder, List[SimFill]]:
    """Process ``ticks`` with last_ms < ts <= upto_ms in time order. Trades before the order is live or at/after its effective cancel never fill it."""
    if order.closed:
        return order, []
    cancel_eff = order.cancel_effective_ms(params)
    o = order
    out: List[SimFill] = []
    for t in sorted((x for x in ticks if o.last_ms < x.ts_ms <= upto_ms), key=lambda x: x.ts_ms):
        if cancel_eff is not None and t.ts_ms >= cancel_eff:
            o = replace(o, closed=True)
            break
        if t.ts_ms < o.active_from_ms or not t.qty > 0:
            continue
        through = t.price < o.price - EPS if o.side == "Buy" else t.price > o.price + EPS          # STRICT: touching is not through
        if not through:
            continue
        cum = o.through_cum + t.qty
        target = min(o.qty, max(0.0, cum - o.queue_ahead))
        delta = target - o.filled
        o = replace(o, through_cum=cum)
        if delta > EPS:
            n = o.n_fills
            mo = markout_bps(o.order_id, n, params)
            notional = delta * o.price
            f = T.Fill(fill_id=f"{o.order_id}:{n}", group_id=o.group_id, leg_id=o.leg_id, symbol=o.symbol, side=o.side, qty=delta, price=o.price,
                       fee_usd=fee_usd(notional, "MAKER", o.category, params), fee_ccy="USDT", liquidity="MAKER", ts_ms=t.ts_ms, prov="MODELLED")
            out.append(SimFill(f, mo, notional * mo / 1e4, 0.0, o.queue_basis))
            o = replace(o, filled=o.filled + delta, n_fills=n + 1)
    last = max(o.last_ms, upto_ms)
    if cancel_eff is not None and upto_ms >= cancel_eff:
        o = replace(o, closed=True)
    if o.remaining <= EPS:
        o = replace(o, closed=True)
    return replace(o, last_ms=last), out


@dataclass(frozen=True)
class TakerResult:
    fills: Tuple[SimFill, ...]
    filled_qty: float
    unfilled_qty: float
    vwap: Optional[float]
    spread_usd: float
    depth_walk_usd: float
    latency_slip_usd: float
    depth_exhausted: bool


def taker_fill(*, fill_id: str, group_id: str, leg_id: str, symbol: str, category: str, side: str, qty: float,
               bids: Sequence[T.BookLevel], asks: Sequence[T.BookLevel], ts_ms: int, params: FillParams) -> TakerResult:
    """Walk the opposite side of the visible book. Pays spread + depth + a modelled latency slippage + the taker fee; short-fills when depth is exhausted."""
    levels = list(asks if side == "Buy" else bids)
    if not (qty > 0) or not levels or not bids or not asks:
        return TakerResult((), 0.0, max(0.0, qty), None, 0.0, 0.0, 0.0, True)
    mid = 0.5 * (bids[0].price + asks[0].price)
    need, got, cost = float(qty), 0.0, 0.0
    for lv in levels:
        take = min(need - got, lv.qty)
        if take > 0:
            got += take
            cost += take * lv.price
        if got >= need - EPS:
            break
    if got <= EPS:
        return TakerResult((), 0.0, float(qty), None, 0.0, 0.0, 0.0, True)
    raw_vwap = cost / got
    sgn = 1.0 if side == "Buy" else -1.0
    vwap = raw_vwap * (1.0 + sgn * params.taker_extra_slip_bps / 1e4)
    touch = levels[0].price
    spread_usd = abs(touch - mid) * got
    depth_usd = abs(raw_vwap - touch) * got
    lat_usd = abs(vwap - raw_vwap) * got
    notional = got * vwap
    f = T.Fill(fill_id=fill_id, group_id=group_id, leg_id=leg_id, symbol=symbol, side=side, qty=got, price=vwap, fee_usd=fee_usd(notional, "TAKER", category, params),
               fee_ccy="USDT", liquidity="TAKER", ts_ms=ts_ms, prov="MODELLED")
    sf = SimFill(f, 0.0, 0.0, abs(vwap - mid) * got, "TAKER_BOOK_WALK")
    return TakerResult((sf,), got, max(0.0, qty - got), vwap, spread_usd, depth_usd, lat_usd, got < need - EPS)


# ====================================================================== collector-backed path (recorded book + trade tape)

PROV_MEASURED = "MEASURED_BOOK_TAPE"
PROV_MODELLED = "MODELLED"
PROV_REJECTED = "REJECTED"
MICRO_NOTES = ("QUEUE_FROM_VISIBLE_SIZE_AT_PLACEMENT", "STRICT_THROUGH_REQUIRED", "OPPOSING_SIDE_TRADES_ONLY", "NO_REBATE_ASSUMED", "TAPE_GAP_IS_UNKNOWN_NOT_ZERO")
MARKOUT_MEASURED = "MEASURED_NEXT_SNAPSHOT_MID"
MARKOUT_MODELLED = "MODELLED"


def _micro_prov(micro: object) -> str:
    return PROV_MEASURED if getattr(micro, "verified", False) else PROV_MODELLED


@dataclass(frozen=True)
class MicroPlacement:
    order: Optional[SimOrder]
    provenance: str                       # MEASURED_BOOK_TAPE | MODELLED | REJECTED
    reasons: Tuple[str, ...]              # empty when accepted
    queue_ahead: Optional[float]
    visible_at_price: Optional[float]
    queue_pessimism: float
    snapshot_ts: Optional[int]
    book_age_ms: Optional[int]


@dataclass(frozen=True)
class MicroFill:
    sim: SimFill
    provenance: str                       # MEASURED_BOOK_TAPE only when the data is verified AND the markout is measured; else MODELLED
    queue_basis: str
    markout_basis: str                    # MEASURED_NEXT_SNAPSHOT_MID | MODELLED
    markout_signed_bps: Optional[float]   # + = adverse (price moved against the fill). sim.markout_bps is max(0, this): a favourable sample is never booked as a gain
    markout_horizon_ms: Optional[int]
    mid_after: Optional[float]


@dataclass(frozen=True)
class MicroAdvance:
    order: SimOrder
    fills: Tuple[MicroFill, ...]
    provenance: str
    reasons: Tuple[str, ...]
    tape_complete: bool
    through_volume: float                 # cumulative recorded opposing-side volume strictly through the price (since placement)
    queue_ahead: float


def _visible_at(levels: Sequence[T.BookLevel], price: float) -> float:
    return sum(lv.qty for lv in levels if abs(lv.price - price) <= EPS)


def place_maker_from_micro(micro: object, *, order_id: str, group_id: str, leg_id: str, symbol: str, category: str, side: str, qty: float, price: float, now_ms: int,
                           params: FillParams, max_age_ms: Optional[int] = None) -> MicroPlacement:
    """Place a post-only maker order against the recorded book valid at ``now_ms`` (``micro.explain_at``). Refused (provenance REJECTED, reasons) when there is no contemporaneous
    valid snapshot (none, future-only, stale, crossed, tampered, discontinuous, ...), the category has no recorded book (only linear is collected), or the base model refuses it."""
    cap = params.stale_quote_ms if max_age_ms is None else max_age_ms
    pessimism = params.queue_pessimism
    if category != "linear":
        return MicroPlacement(None, PROV_REJECTED, ("NO_RECORDED_BOOK_FOR_CATEGORY",), None, None, pessimism, None, None)
    snap, why = micro.explain_at(symbol, now_ms, max_age_ms=min(cap, micro.params.max_age_ms))        # type: ignore[attr-defined]
    if snap is None:
        return MicroPlacement(None, PROV_REJECTED, tuple(why), None, None, pessimism, None, None)
    order, reason = place_maker(order_id=order_id, group_id=group_id, leg_id=leg_id, symbol=symbol, category=category, side=side, qty=qty, price=price,
                                bids=snap.bids, asks=snap.asks, book_age_ms=snap.book_age_ms, now_ms=now_ms, params=params)
    if order is None:
        return MicroPlacement(None, PROV_REJECTED, (reason,), None, None, pessimism, snap.ts, snap.book_age_ms)
    levels = snap.bids if side == "Buy" else snap.asks
    order = replace(order, micro_ref=f"{symbol}@{snap.cycle}:{snap.ts}")
    return MicroPlacement(order, _micro_prov(micro), (), order.queue_ahead, _visible_at(levels, price), pessimism, snap.ts, snap.book_age_ms)


def advance_with_trades(order: SimOrder, micro: object, upto_ms: int, params: FillParams) -> MicroAdvance:
    """Advance a resting order to ``upto_ms`` using the RECORDED tape only. The tape over (max(last, live-from), min(upto, cancel-effective)] must be proven covered
    (``micro.tape_between(...).complete``) and consistent with the recorded books; otherwise the fill status is UNKNOWN: the order is closed, nothing fills, and the result is
    REJECTED with TAPE_GAP. Opposing-side trades strictly through the price deplete the queue ahead (visible size x pessimism); only the excess fills, proportionally."""
    if order.closed:
        return MicroAdvance(order, (), PROV_REJECTED, ("ORDER_CLOSED",), True, order.through_cum, order.queue_ahead)
    ce = order.cancel_effective_ms(params)
    hi = upto_ms if ce is None else min(upto_ms, ce)
    lo = max(order.last_ms, order.active_from_ms - 1)
    ticks: List[Tick] = []
    complete = True
    buy = order.side == "Buy"
    if hi > lo:
        tape = micro.tape_between(order.symbol, lo, hi)                                          # type: ignore[attr-defined]
        if not tape.complete:
            closed = replace(order, closed=True, last_ms=max(order.last_ms, upto_ms))
            return MicroAdvance(closed, (), PROV_REJECTED, tuple(tape.reasons) or ("TAPE_GAP",), False, order.through_cum, order.queue_ahead)
        opp = "Sell" if buy else "Buy"
        ticks = [Tick(ts, px, q) for ts, px, q, sd in tape.trades if sd == opp]
        any_through = any((px < order.price - EPS) if buy else (px > order.price + EPS) for _ts, px, _q, _sd in tape.trades)
        for b in micro.valid_snapshots_between(order.symbol, lo, hi):                            # type: ignore[attr-defined]
            lvl = b.asks if buy else b.bids
            if lvl and ((lvl[0].price < order.price - EPS) if buy else (lvl[0].price > order.price + EPS)) and not any_through:
                closed = replace(order, closed=True, last_ms=max(order.last_ms, upto_ms))
                return MicroAdvance(closed, (), PROV_REJECTED, ("TAPE_GAP", "BOOK_TRADED_THROUGH_WITHOUT_RECORDED_TRADES"), False, order.through_cum, order.queue_ahead)
    o2, sfills = advance(order, ticks, upto_ms, params)
    base = _micro_prov(micro)
    out: List[MicroFill] = []
    for sf in sfills:
        f = sf.fill
        ns = micro.next_valid_after(order.symbol, f.ts_ms)                                       # type: ignore[attr-defined]
        if ns is not None and ns.mid:
            signed = ((f.price - ns.mid) if buy else (ns.mid - f.price)) / f.price * 1e4
            adverse = max(0.0, signed)
            sim = replace(sf, markout_bps=adverse, markout_usd=f.qty * f.price * adverse / 1e4, queue_basis=sf.queue_basis, notes=MICRO_NOTES)
            out.append(MicroFill(sim, base, sf.queue_basis, MARKOUT_MEASURED, signed, (ns.ts - f.ts_ms) if ns.ts is not None else None, ns.mid))
        else:
            out.append(MicroFill(replace(sf, notes=MICRO_NOTES), PROV_MODELLED, sf.queue_basis, MARKOUT_MODELLED, None, None, None))
    prov = base if all(x.provenance == base for x in out) else PROV_MODELLED
    return MicroAdvance(o2, tuple(out), prov, (), complete, o2.through_cum, o2.queue_ahead)


@dataclass(frozen=True)
class MicroTaker:
    result: Optional[TakerResult]
    provenance: str                       # of the book walk (MEASURED_BOOK_TAPE / MODELLED when the data is unverified / REJECTED)
    reasons: Tuple[str, ...]
    leg_id: str
    category: str
    mid: Optional[float]
    half_spread_bps: Optional[float]      # measured
    depth_walk_bps: Optional[float]       # measured: extra price paid by walking past the touch
    latency_slip_bps: Optional[float]     # MODELLED (taker_extra_slip_bps)
    fee_bps: Optional[float]              # stated figure
    total_cost_bps: Optional[float]       # half spread + depth walk + latency slip + fee, versus the mid, on the filled quantity only
    component_prov: Tuple[Tuple[str, str], ...]


def taker_from_micro(micro: object, *, fill_id: str, group_id: str, leg_id: str, symbol: str, category: str, side: str, qty: float, decision_ms: int, params: FillParams,
                     max_age_ms: Optional[int] = None) -> MicroTaker:
    """Taker fill priced by walking the recorded book valid at ``decision_ms``. Spread and depth components are measured; latency slippage and fee are separate stated
    components. A leg whose book was not recorded (spot: the collector records linear only) is REJECTED, never priced from the other leg's book."""
    def rej(*why: str) -> MicroTaker:
        return MicroTaker(None, PROV_REJECTED, tuple(why), leg_id, category, None, None, None, None, None, None, ())
    if category != "linear":
        return rej("NO_RECORDED_BOOK_FOR_CATEGORY")
    cap = params.stale_quote_ms if max_age_ms is None else max_age_ms
    snap, why = micro.explain_at(symbol, decision_ms, max_age_ms=min(cap, micro.params.max_age_ms))   # type: ignore[attr-defined]
    if snap is None:
        return rej(*why)
    tr = taker_fill(fill_id=fill_id, group_id=group_id, leg_id=leg_id, symbol=symbol, category=category, side=side, qty=qty, bids=snap.bids, asks=snap.asks, ts_ms=decision_ms,
                    params=params)
    if tr.filled_qty <= 0:
        return rej("NO_DEPTH")
    mid = snap.mid or 0.0
    base = tr.filled_qty * mid
    bps = lambda usd: usd / base * 1e4                                                          # noqa: E731
    f = tr.fills[0].fill
    fee_b = f.fee_usd / base * 1e4
    h, d, la = bps(tr.spread_usd), bps(tr.depth_walk_usd), bps(tr.latency_slip_usd)
    prov = _micro_prov(micro)
    comps = (("half_spread", prov), ("depth_walk", prov), ("latency_slip", PROV_MODELLED), ("fee", "VENUE_STATED"))
    return MicroTaker(tr, prov, ("DEPTH_EXHAUSTED",) if tr.depth_exhausted else (), leg_id, category, mid, h, d, la, fee_b, h + d + la + fee_b, comps)


def taker_legs_from_micro(micro: object, legs: Sequence[dict], *, decision_ms: int, params: FillParams) -> Tuple[MicroTaker, ...]:
    """One MicroTaker per leg (perp leg, hedge leg, ...), each priced from its own recorded book and NEVER netted against another: costs stay separate."""
    return tuple(taker_from_micro(micro, decision_ms=decision_ms, params=params, **leg) for leg in legs)


@dataclass(frozen=True)
class MicroCapacity:
    qty: float
    usd: float
    binding: str                          # SLIPPAGE_EDGE | DEPTH_FRACTION | REJECTED
    slippage_bps_at_capacity: Optional[float]
    visible_qty: Optional[float]
    provenance: str
    reasons: Tuple[str, ...]


def _walk_bps(levels: Sequence[T.BookLevel], mid: float, buy: bool, qty: float, extra_bps: float) -> float:
    need, got, cost = qty, 0.0, 0.0
    for lv in levels:
        take = min(need - got, lv.qty)
        if take > 0:
            got += take
            cost += take * lv.price
        if got >= need - EPS:
            break
    if got <= EPS:
        return 0.0
    vwap = cost / got
    return ((vwap - mid) if buy else (mid - vwap)) / mid * 1e4 + extra_bps


def capacity_from_micro(micro: object, *, symbol: str, decision_ms: int, side: str, edge_bps: float, params: FillParams, depth_fraction: float = 0.10,
                        max_age_ms: Optional[int] = None) -> MicroCapacity:
    """The largest taker size whose walked slippage (half spread + depth + the modelled latency slip) stays <= ``edge_bps`` AND whose size stays <= ``depth_fraction`` of the
    visible same-side depth. ``binding`` says which limit applied. No valid recorded book => REJECTED."""
    cap = params.stale_quote_ms if max_age_ms is None else max_age_ms
    snap, why = micro.explain_at(symbol, decision_ms, max_age_ms=min(cap, micro.params.max_age_ms))   # type: ignore[attr-defined]
    if snap is None:
        return MicroCapacity(0.0, 0.0, PROV_REJECTED, None, None, PROV_REJECTED, tuple(why))
    if not (edge_bps >= 0 and 0 < depth_fraction <= 1) or side not in ("Buy", "Sell"):
        return MicroCapacity(0.0, 0.0, PROV_REJECTED, None, None, PROV_REJECTED, ("BAD_ARGUMENT",))
    buy = side == "Buy"
    levels = snap.asks if buy else snap.bids
    vis = sum(lv.qty for lv in levels)
    qmax = vis * depth_fraction
    mid = snap.mid or 0.0
    prov = _micro_prov(micro)

    def slip(q: float) -> float:
        return _walk_bps(levels, mid, buy, q, params.taker_extra_slip_bps)
    tiny = min(qmax, levels[0].qty) * 1e-9
    if slip(tiny) > edge_bps:
        return MicroCapacity(0.0, 0.0, "SLIPPAGE_EDGE", slip(tiny), vis, prov, ("EDGE_BELOW_TOUCH_COST",))
    if slip(qmax) <= edge_bps:
        return MicroCapacity(qmax, qmax * mid, "DEPTH_FRACTION", slip(qmax), vis, prov, ())
    lo_q, hi_q = tiny, qmax
    for _ in range(80):
        m = 0.5 * (lo_q + hi_q)
        if slip(m) <= edge_bps:
            lo_q = m
        else:
            hi_q = m
    return MicroCapacity(lo_q, lo_q * mid, "SLIPPAGE_EDGE", slip(lo_q), vis, prov, ())
