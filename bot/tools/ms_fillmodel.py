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
