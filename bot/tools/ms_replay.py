"""ms_replay: the maker-first REPLAY / PAPER engine for the multi-symbol portfolio (simulated transport only; no network, no wall clock, no credentials).

It walks captured history (``SymbolSeries`` from candidate_readiness.load_history_root via ``from_symbol_series``, or injected ``ReplaySeries``) and runs the SAME code a paper
run would: scanner (injected) -> ms_portfolio.allocate -> risk (injected) -> ms_execution lifecycle -> a simulated transport driven by ms_fillmodel. Paper = this loop with a
live-like injected clock; every fill is labelled MODELLED/SIMULATED.

What is simulated, in order, at every step ``t``: (1) due faults, (2) point-in-time snapshots (only data with timestamp <= t), (3) resting maker orders advance through the trades
since the last step, (4) liquidation check, (5) funding settlement of every print with ts in (prev, t] at its ACTUAL timestamp, credited/debited by position side
(``amount = -signed_qty * mark * rate``: a short with a positive rate RECEIVES), (6) lifecycle step (timeouts, reconcile), (7) exits, (8) mark-to-market equity / drawdown,
(9) on a decision tick: scan, allocate, risk, submit.

Honesty rules: observations are counted as INDEPENDENT only when their [entry, exit + embargo] intervals do not overlap any already-counted one; nothing here is ever promotable
(``promotion_eligible`` is False with explicit blockers while any input is ASSUMED/MODELLED or the independent count is small); the number of variants ever evaluated is
carried in a persistent trials ledger and widens the reported interval.
"""
from __future__ import annotations

import bisect
import hashlib
import json
import math
import os
import statistics
import sys
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

BOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (BOT, os.path.join(BOT, "tools")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import ms_execution as EX  # noqa: E402
import ms_fillmodel as FM  # noqa: E402
import ms_portfolio as PF  # noqa: E402
import ms_types as T  # noqa: E402
from ms_types import GroupState as G  # noqa: E402

MS_H = 3_600_000
MIN_INDEPENDENT_FOR_ANY_CLAIM = 30


# ------------------------------------------------------------------ inputs

@dataclass(frozen=True)
class ReplaySeries:
    symbol: str
    funding: Tuple[Tuple[int, float], ...]                 # (settlement ts, bps per print), ascending
    mark: Tuple[Tuple[int, float], ...]                    # (observation ts, price), ascending
    interval_min: Optional[int]                            # None = UNKNOWN (never defaulted to 480): the snapshot carries None and the scanner / allocator block the symbol
    base_coin: str = ""
    launch_ms: int = 0
    delivery_ms: Optional[int] = None
    spot_mark: Optional[Tuple[Tuple[int, float], ...]] = None
    books: Optional[Tuple[Tuple[int, Tuple[T.BookLevel, ...], Tuple[T.BookLevel, ...]], ...]] = None     # MEASURED books; None => synthetic (ASSUMED)
    ticks: Optional[Tuple[FM.Tick, ...]] = None                                                          # MEASURED trades; None => bars from marks
    funding_intervals: Optional[Tuple[Tuple[int, int, int], ...]] = None
    instrument: Optional[T.Instrument] = None
    spot_listed: Optional[bool] = True
    oi_usd: Optional[float] = None
    mark_stale_ms: Optional[int] = None                    # a mark older than this at the decision is UNKNOWN (None = unbounded, the legacy behaviour; ms_pipeline always bounds it)


def from_symbol_series(ss: Any, *, instrument: Optional[T.Instrument] = None) -> ReplaySeries:
    """Adapter from candidate_readiness.SymbolSeries (the loader's output). No book/trade data exists in that capture => synthetic books (ASSUMED), bars from marks."""
    return ReplaySeries(ss.symbol, tuple((int(t), float(r)) for t, r in ss.funding), tuple((int(t), float(p)) for t, p in ss.mark), (int(ss.interval_min) if ss.interval_min else None),
                        ss.symbol[:-4] if ss.symbol.endswith("USDT") else ss.symbol, int(ss.launch_ms or 0), ss.delivery_ms, None, None, None,
                        tuple(tuple(x) for x in ss.funding_intervals) if ss.funding_intervals else None, instrument, ss.spot_listed, ss.oi_usd)


@dataclass(frozen=True)
class Fault:
    ts_ms: int
    kind: str          # LOST_ACK LOST_REQUEST DUPLICATE_ACK DUPLICATE_FILL STALE_BOOK STALE_ACCOUNT POSITIONS_UNKNOWN INSTRUMENT_HALT INTERVAL_CHANGE RISK_BREACH RESTART CANCEL_LOST
    symbol: str = ""
    until_ms: Optional[int] = None
    value: Optional[int] = None


@dataclass(frozen=True)
class ReplayConfig:
    start_ms: int
    end_ms: int
    step_ms: int = MS_H
    decision_every_ms: int = 8 * MS_H
    hold_ms: int = 3 * 86_400_000
    exit_funding_below_bps: Optional[float] = None
    equity0: float = 100_000.0
    fill: FM.FillParams = FM.FillParams()
    portfolio: PF.PortfolioPolicy = PF.PortfolioPolicy()
    exec_policy: EX.ExecPolicy = EX.ExecPolicy(reconcile_every_step=True)
    synthetic_spread_bps: float = 2.0
    synthetic_depth_usd: float = 2_000_000.0
    default_tick: str = "0.01"
    default_qty_step: str = "0.01"
    default_min_qty: str = "0.01"
    default_max_qty: str = "1000000"
    default_min_notional: str = "5"
    liq_fee_bps: float = 50.0
    embargo_ms: int = 0
    variant_id: str = "v0"
    spec_digest: str = "spec"
    faults: Tuple[Fault, ...] = ()
    regime_low_bps_day: float = 1.0
    regime_high_bps_day: float = 5.0
    stale_book_age_ms: int = 600_000
    exit_book_max_age_ms: int = 3_600_000                  # EXPLICIT wider book-age cap for REDUCE-ONLY taker exits (an entry never gets it: it uses fill.stale_quote_ms); every exit that needed it is counted


# ------------------------------------------------------------------ the local P&L ledger (contract-compatible; swap for ms_accounting via ledger_factory)

class LocalLedger:
    """record_fill / record_funding / mark / pnl. THE net definition (every ledger behind this contract must give the same one, build_report asserts it):
        price_leg = actual-fill price leg + taker slippage          (AT MID: the fill prices already contain the taker walk, so it is added back here ...)
        slippage  = taker slippage + adverse selection              (... and charged exactly once here; adverse selection is a post-fill markout that is NOT in any fill price)
        net       = price_leg + funding - fees - slippage
    realised/unrealised split price_leg by whether the group is flat."""

    def __init__(self, exit_fee_bps_perp: float = 0.0, exit_fee_bps_spot: float = 0.0) -> None:
        self.fills: List[T.Fill] = []
        self.funding_events: List[Tuple[str, int, float, str]] = []
        self.marks: Dict[str, float] = {}
        self.books: Dict[str, Tuple[float, float]] = {}            # key -> (best bid, best ask) of the latest mark that carried a book; absent => valued at the mark (MID_FALLBACK)
        self.exit_fee_bps = {"linear": float(exit_fee_bps_perp), "spot": float(exit_fee_bps_spot)}
        self.taker_slip = 0.0
        self.adverse = 0.0
        self.unknown_funding = 0
        self._ids: set = set()
        self._cat: Dict[str, str] = {}

    def record_fill(self, f: T.Fill, category: str = "linear") -> None:
        if f.fill_id in self._ids:
            return
        self._ids.add(f.fill_id)
        self.fills.append(f)
        self._cat[f.fill_id] = category

    def record_funding(self, symbol: str, ts_ms: int, amount_usd: Optional[float], prov: str) -> None:
        if amount_usd is None:                                     # unknown amount: counted, never summed as zero
            self.unknown_funding += 1
            return
        self.funding_events.append((symbol, ts_ms, amount_usd, prov))

    def record_slippage(self, usd: float, kind: str, symbol: Optional[str] = None) -> None:
        if kind == "adverse":
            self.adverse += usd
        else:
            self.taker_slip += usd

    def mark(self, symbol: str, ts_ms: int, price: float, bid: Optional[float] = None, ask: Optional[float] = None) -> None:
        """``bid`` / ``ask`` (optional): an OPEN position is valued at what CLOSING it would yield (a long at the bid, a short at the ask, less the taker exit fee), never at mid."""
        self.marks[symbol] = price
        if bid is not None and ask is not None and 0 < float(bid) <= float(ask) and math.isfinite(float(ask)):
            self.books[symbol] = (float(bid), float(ask))
        else:
            self.books.pop(symbol, None)

    def pnl(self) -> Dict[str, float]:
        cat = self._cat
        cash = 0.0
        pos: Dict[str, float] = {}
        avg: Dict[str, float] = {}                                 # average entry price of the open quantity per key (average-cost accounting)
        fees = 0.0
        zero_fee_unverified = False
        for f in self.fills:
            s = 1.0 if f.side == "Buy" else -1.0
            cash -= s * f.qty * f.price
            k = f.symbol if cat.get(f.fill_id, "linear") == "linear" else "SPOT:" + f.symbol
            before = pos.get(k, 0.0)
            after = before + s * f.qty
            if abs(before) < 1e-12 or before * s > 0:              # opening or adding in the same direction: the average cost moves
                avg[k] = (abs(before) * avg.get(k, f.price) + f.qty * f.price) / (abs(before) + f.qty)
            elif before * after < 0:                               # the fill flipped the position: the remainder was opened at this price
                avg[k] = f.price
            pos[k] = after                                         # (a reduction keeps the average cost of what remains)
            fees += f.fee_usd
            if f.fee_usd == 0 and f.prov != "MEASURED":
                zero_fee_unverified = True                       # a free fill nobody measured is not a free fill (the canonical ledger books the fee as unknown)
        unreal_mtm = 0.0
        mid_fallback = False
        missing_marks: List[str] = []
        for k, q in pos.items():
            if abs(q) < 1e-9:
                continue
            bk = self.books.get(k)
            if bk is None:
                mid_fallback = True
                m = self.marks.get(k)
                if m is None:                                    # an UNKNOWN price is not zero: a short valued at 0 would book its whole sale as profit. Valued at cost (no mark-to-market P&L), and flagged
                    missing_marks.append(k)
                    unreal_mtm += q * avg.get(k, 0.0)
                else:
                    unreal_mtm += q * m
                continue
            px = bk[0] if q > 0 else bk[1]
            unreal_mtm += q * px - abs(q) * px * self.exit_fee_bps["spot" if k.startswith("SPOT:") else "linear"] / 1e4
        price_actual = cash + unreal_mtm
        funding = sum(x[2] for x in self.funding_events)
        price_leg = price_actual + self.taker_slip
        slippage = self.taker_slip + self.adverse
        flat = all(abs(q) < 1e-9 for q in pos.values())
        return {"price_leg": price_leg, "funding": funding, "fees": fees, "slippage": slippage, "adverse_selection": self.adverse, "taker_slippage": self.taker_slip,
                "realised": price_leg if flat else cash + self.taker_slip, "unrealised": 0.0 if flat else unreal_mtm, "net": price_leg + funding - fees - slippage,
                "unknown_funding_events": self.unknown_funding, "valuation": "MID_FALLBACK" if mid_fallback else "EXIT_SIDE",
                "flags": (["MID_FALLBACK"] if mid_fallback else []) + [f"MARK_MISSING:{k}" for k in sorted(missing_marks)],
                "fee_flags": ["ZERO_FEE_UNVERIFIED"] if zero_fee_unverified else []}


# ------------------------------------------------------------------ the world: point-in-time lookups over the series

class World:
    def __init__(self, series: Sequence[ReplaySeries], cfg: ReplayConfig) -> None:
        self.s: Dict[str, ReplaySeries] = {x.symbol: x for x in series}
        self.cfg = cfg
        self._mk = {x.symbol: [t for t, _ in x.mark] for x in series}
        self._fk = {x.symbol: [t for t, _ in x.funding] for x in series}
        self._sk = {x.symbol: [t for t, _ in (x.spot_mark or ())] for x in series}
        self.assumed: set = set()
        self.measured: set = set()

    @staticmethod
    def _at(seq: Sequence[Tuple[int, float]], keys: Sequence[int], t: int) -> Optional[Tuple[int, float]]:
        i = bisect.bisect_right(keys, t) - 1
        return seq[i] if i >= 0 else None

    def mark(self, sym: str, t: int) -> Optional[float]:
        r = self._at(self.s[sym].mark, self._mk[sym], t)
        if r is None:
            return None
        bound = self.s[sym].mark_stale_ms
        if bound is not None and t - r[0] > bound:
            return None                                        # a gap in the marks is an unknown price, not the last one forever
        return r[1]

    def spot_mark(self, sym: str, t: int) -> Optional[float]:
        x = self.s[sym]
        if x.spot_mark:
            r = self._at(x.spot_mark, self._sk[sym], t)
            return r[1] if r else None
        self.assumed.add("SPOT_PRICE_EQUALS_PERP_MARK")
        return self.mark(sym, t)

    def interval_min(self, sym: str, t: int) -> Optional[int]:
        x = self.s[sym]
        if x.funding_intervals:
            for a, b, iv in x.funding_intervals:
                if a <= t <= b:
                    return iv
            past = [iv for a, b, iv in x.funding_intervals if a <= t]
            return past[-1] if past else x.interval_min
        return x.interval_min

    def trailing_funding(self, sym: str, t: int, n: int = 12) -> Tuple[float, ...]:
        x = self.s[sym]
        i = bisect.bisect_right(self._fk[sym], t)
        return tuple(r for _, r in x.funding[max(0, i - n):i])

    def last_print_ts(self, sym: str, t: int) -> int:
        i = bisect.bisect_right(self._fk[sym], t)
        return self._fk[sym][i - 1] if i else t

    def print_interval_min(self, sym: str, ts: int) -> Optional[int]:
        """The funding interval IN FORCE AT the print timestamp ``ts`` (never the decision-time interval): the manifest segment that contains the print; with no segments the series'
        single known interval; None when that is unknown (a print outside every segment included)."""
        x = self.s[sym]
        if x.funding_intervals:
            for a, b, iv in x.funding_intervals:
                if a <= ts <= b:
                    return iv
            return None
        return x.interval_min

    def trailing_funding_intervals(self, sym: str, t: int, n: int = 12) -> Tuple[int, ...]:
        """One interval per trailing print (same window as ``trailing_funding``). If ANY print's interval is unknown the tuple is EMPTY (the consumer then cannot normalise and must
        exclude the symbol); it is never filled with the current interval."""
        x = self.s[sym]
        i = bisect.bisect_right(self._fk[sym], t)
        ivs = [self.print_interval_min(sym, ts) for ts, _ in x.funding[max(0, i - n):i]]
        return tuple(ivs) if ivs and all(isinstance(v, int) and v > 0 for v in ivs) else ()

    def next_funding(self, sym: str, t: int) -> Optional[int]:
        x = self.s[sym]
        i = bisect.bisect_right(self._fk[sym], t)
        if i == 0:
            return None
        last = x.funding[i - 1][0]
        ivm = self.interval_min(sym, t)
        if not ivm:
            return None                                        # unknown interval: the next print time is unknown too
        iv = ivm * 60_000
        k = (t - last) // iv + 1
        return last + k * iv

    def book(self, sym: str, category: str, t: int) -> Tuple[Tuple[T.BookLevel, ...], Tuple[T.BookLevel, ...], Optional[int]]:
        x = self.s[sym]
        if category == "linear" and x.books:
            keys = [b[0] for b in x.books]
            i = bisect.bisect_right(keys, t) - 1
            if i >= 0:
                self.measured.add("BOOK")
                return x.books[i][1], x.books[i][2], t - x.books[i][0]
            return (), (), None
        px = self.mark(sym, t) if category == "linear" else self.spot_mark(sym, t)
        if px is None:
            return (), (), None
        self.assumed.add("SYNTHETIC_BOOK_DEPTH_AND_SPREAD")
        h = self.cfg.synthetic_spread_bps / 2e4 * px
        q = self.cfg.synthetic_depth_usd / px
        return (T.BookLevel(px - h, q),), (T.BookLevel(px + h, q),), 0

    def trades(self, sym: str, category: str, a: int, b: int, order: FM.SimOrder) -> List[FM.Tick]:
        x = self.s[sym]
        if category == "linear" and x.ticks is not None:
            self.measured.add("TRADES")
            return [t for t in x.ticks if a < t.ts_ms <= b]
        seq = x.mark if category == "linear" else (x.spot_mark or x.mark)
        keys = self._mk[sym] if category == "linear" else (self._sk[sym] if x.spot_mark else self._mk[sym])
        lo = bisect.bisect_left(keys, a)
        hi = bisect.bisect_right(keys, b)
        window = [p for _, p in seq[max(0, lo - 1):hi]]            # include the last mark at/before a: the path started there
        if len(window) < 1 or hi <= lo:
            return []
        self.assumed.add("BARS_FROM_MARKS_NO_TRADE_VOLUME")
        return FM.bar_ticks(FM.Bar(b, min(window), max(window), None), order, self.cfg.fill)


# ------------------------------------------------------------------ the simulated transport (fill model behind the lifecycle's send/cancel/venue-read)

class ReplayTransport:
    def __init__(self, world: World, cfg: ReplayConfig, venue: Optional[EX.SimVenue] = None, order_gate: Optional[Callable[[EX.OrderRequest], Optional[str]]] = None) -> None:
        self.w, self.cfg = world, cfg
        self.order_gate = order_gate                               # an authority at the venue-request boundary: a refusal reason => the order never reaches the venue
        self.venue = venue or EX.SimVenue()
        self.sim: Dict[str, FM.SimOrder] = {}
        self.now = 0
        self.pending: List[T.Fill] = []
        self.dones: List[Tuple[str, str]] = []
        self.cat: Dict[str, str] = {}                              # fill_id -> category
        self.slip: List[Tuple[str, float, str, str]] = []         # ("adverse"|"taker", usd, group_id, symbol)
        self.halted: set = set()
        self.fault_next: List[str] = []
        self.stats = {"orders_placed": 0, "refused": {}, "maker_orders": 0, "taker_orders": 0}
        self.order_fills: Dict[str, int] = {}
        self.queue_bases: Dict[str, int] = {}

    def send_order(self, req: EX.OrderRequest) -> EX.Ack:
        leg, link = req.leg, req.link_id
        if self.order_gate is not None:
            why = self.order_gate(req)
            if why:
                self.stats["refused"]["AUTHORITY:" + why.split(":", 1)[0]] = self.stats["refused"].get("AUTHORITY:" + why.split(":", 1)[0], 0) + 1
                return EX.Ack(link, "REJECTED", None, "AUTHORITY_REFUSED:" + why)
        if link in self.venue.orders:
            return self.venue.send_order(req)                      # an idempotent resend: the venue returns the existing order
        if leg.symbol in self.halted:
            return EX.Ack(link, "REJECTED", None, "INSTRUMENT_HALTED")
        bids, asks, age = self.w.book(leg.symbol, leg.category, self.now)
        if self.fault_next and self.fault_next[0] == "STALE_BOOK_ONCE":
            age = self.cfg.fill.stale_quote_ms + 1
            self.fault_next.pop(0)
        if leg.order_type != "Limit":                                  # a taker walks the book it SEES: a book of any age is not a quote
            cap = max(self.cfg.exit_book_max_age_ms, self.cfg.fill.stale_quote_ms) if leg.reduce_only else self.cfg.fill.stale_quote_ms
            if age is None or age > cap:
                self.stats["refused"]["STALE_QUOTE"] = self.stats["refused"].get("STALE_QUOTE", 0) + 1
                if leg.reduce_only:
                    self.stats["exits_refused_stale_quote"] = self.stats.get("exits_refused_stale_quote", 0) + 1
                return EX.Ack(link, "REJECTED", None, "STALE_QUOTE")
            if leg.reduce_only and age > self.cfg.fill.stale_quote_ms:
                self.stats["exits_on_wider_book_cap"] = self.stats.get("exits_on_wider_book_cap", 0) + 1
        self.stats["orders_placed"] += 1
        fault = self.fault_next.pop(0) if self.fault_next and self.fault_next[0] in ("LOST_ACK", "LOST_REQUEST") else None
        if leg.order_type == "Limit":
            o, why = FM.place_maker(order_id=link, group_id=req.group_id, leg_id=leg.leg_id, symbol=leg.symbol, category=leg.category, side=leg.side, qty=float(leg.qty),
                                    price=float(leg.price), bids=bids, asks=asks, book_age_ms=age, now_ms=self.now, params=self.cfg.fill)
            if o is None:
                self.stats["refused"][why] = self.stats["refused"].get(why, 0) + 1
                return EX.Ack(link, "REJECTED", None, why)
            self.stats["maker_orders"] += 1
            if fault == "LOST_REQUEST":
                self.venue.lose_request.add(link)
            else:
                self.sim[link] = o
                self.queue_bases[o.queue_basis] = self.queue_bases.get(o.queue_basis, 0) + 1
                if fault == "LOST_ACK":
                    self.venue.drop_ack.add(link)
            return self.venue.send_order(req)
        # Market: a taker walk of the visible book, now
        self.stats["taker_orders"] += 1
        r = FM.taker_fill(fill_id=f"{link}:T0", group_id=req.group_id, leg_id=leg.leg_id, symbol=leg.symbol, category=leg.category, side=leg.side, qty=float(leg.qty),
                          bids=bids, asks=asks, ts_ms=self.now, params=self.cfg.fill)
        if fault == "LOST_REQUEST":
            self.venue.lose_request.add(link)
        elif fault == "LOST_ACK":
            self.venue.drop_ack.add(link)
        try:
            ack = self.venue.send_order(req)
        finally:
            if link in self.venue.orders and r.fills:
                sf = r.fills[0]
                f = self.venue.execute(link, sf.fill.qty, sf.fill.price, self.now, liquidity="TAKER", fee_usd=sf.fill.fee_usd, fill_id=sf.fill.fill_id)
                if f is None:                                      # the venue's own book had nothing to reduce: no execution exists (reduce-only never flips a position)
                    self.stats["reduce_only_clamped_to_zero"] = self.stats.get("reduce_only_clamped_to_zero", 0) + 1
                    self.dones.append((req.group_id, leg.leg_id))
                else:
                    self.cat[f.fill_id] = leg.category
                    self.pending.append(f)
                    self.slip.append(("taker", sf.slippage_usd * (f.qty / sf.fill.qty if sf.fill.qty > 0 else 1.0), req.group_id, leg.symbol))
            if link in self.venue.orders and r.unfilled_qty > 1e-12:
                o = self.venue.orders[link]
                o["status"] = "PartiallyFilledCanceled" if o["filled_qty"] > 0 else "Cancelled"
                self.dones.append((req.group_id, leg.leg_id))
        return ack

    def cancel_order(self, req: EX.OrderRequest) -> str:
        if self.fault_next and self.fault_next[0] == "CANCEL_LOST":
            self.fault_next.pop(0)
            self.venue.cancel_lost.add(req.link_id)
        o = self.sim.get(req.link_id)
        try:
            res = self.venue.cancel_order(req)
        finally:
            self.venue.cancel_lost.discard(req.link_id)                       # a lost cancel is a one-off, not a permanent venue property
        if o is not None and res == "CANCELLED":
            self.sim[req.link_id] = FM.request_cancel(o, self.now)           # live until the cancel takes effect: trades in that window still fill it
        return res

    def advance(self, t: int) -> None:
        for link in sorted(self.sim):
            o = self.sim[link]
            if o.closed:
                continue
            ticks = self.w.trades(o.symbol, o.category, o.last_ms, t, o)
            o2, fills = FM.advance(o, ticks, t, self.cfg.fill)
            self.sim[link] = o2
            for sf in fills:
                if sf.fill.qty < 1e-9:                             # a float residue of a completed order (qty ~1e-14): not an execution (ms_accounting refuses a zero-quantity fill)
                    self.stats["micro_fills_dropped"] = self.stats.get("micro_fills_dropped", 0) + 1
                    continue
                f = self.venue.execute(link, sf.fill.qty, sf.fill.price, sf.fill.ts_ms, liquidity="MAKER", fee_usd=sf.fill.fee_usd, fill_id=sf.fill.fill_id)
                if f is None:
                    self.stats["reduce_only_clamped_to_zero"] = self.stats.get("reduce_only_clamped_to_zero", 0) + 1
                    continue
                self.cat[f.fill_id] = o.category
                self.pending.append(f)
                self.slip.append(("adverse", sf.markout_usd * (f.qty / sf.fill.qty), o.group_id, o.symbol))
                self.order_fills[link] = self.order_fills.get(link, 0) + 1

    def venue_state(self) -> Dict[str, Any]:
        return self.venue.venue_state()


# ------------------------------------------------------------------ the run

@dataclass
class GroupRecord:
    group_id: str
    candidate_id: str
    symbols: Tuple[str, ...]
    entry_ts: Optional[int] = None
    exit_ts: Optional[int] = None
    notional_usd: float = 0.0
    regime: str = "ALL"
    kind: str = "NORMAL"
    price_usd: float = 0.0
    mid_fallback: bool = False                                      # an open leg at the end had no book: valued at the mark, flagged
    fees_usd: float = 0.0
    funding_usd: float = 0.0
    adverse_usd: float = 0.0
    taker_slip_usd: float = 0.0
    net_usd: float = 0.0
    closed: bool = False


@dataclass
class ReplayResult:
    cfg: ReplayConfig
    equity_curve: List[Tuple[int, float]]
    groups: Dict[str, GroupRecord]
    pnl: Dict[str, float]
    stats: Dict[str, Any]
    fills: List[T.Fill]
    fills_cat: Dict[str, str]
    decisions: List[Dict[str, Any]]
    fault_log: List[Dict[str, Any]]
    assumed: List[str]
    measured: List[str]
    journal_head: str
    states: Dict[str, str]
    engine: Any = None
    venue: Any = None


def _utc_year(ts_ms: int) -> int:
    return datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc).year


def halt_gate(eng: Any) -> Tuple[Optional[str], frozenset]:
    """What the engine would REFUSE at submit, asked BEFORE allocating: (a reason when new exposure is halted globally -- a risk-breach halt, UNKNOWN_VENUE_POSITION, a position
    mismatch ... -- else None, the symbols that are halted or blocked)."""
    reason = ("ENGINE_HALTED:" + ",".join(sorted(eng.halts))) if eng.halts else None
    return reason, frozenset(set(eng.halted_symbols) | set(eng.blocked_symbols))


def _opp_symbols(o: T.Opportunity) -> set:
    return {s[5:] if s.startswith("SPOT:") else s for s, _ in o.legs}


@dataclass
class WorldProbe:
    """Read-only accessors onto the CURRENT replay world, bound once per run to ``gates.bind``: what an authority's state / snapshot providers need (always the present step)."""
    now: Callable[[], int]
    snapshots: Callable[[], Dict[str, T.MarketSnapshot]]
    risk_state: Callable[[], Dict[str, Any]]
    engine: Callable[[], Any]
    portfolio_state: Optional[Callable[[], Any]] = None          # the PortfolioState at the present step (reconciliation compares it with the venue book)


def run_replay(series: Sequence[ReplaySeries], cfg: ReplayConfig, *, scan_fn: Callable[[int, Mapping[str, T.MarketSnapshot], Mapping[str, T.Instrument]], List[T.Opportunity]],
               risk_assess: Callable[..., Any], risk_policy: Optional[Mapping[str, Any]] = None, ledger_factory: Callable[[], Any] = LocalLedger,
               store: Optional[Any] = None, venue: Optional[EX.SimVenue] = None, clock: Optional[Callable[[], int]] = None, gates: Optional[Any] = None) -> ReplayResult:
    """Walk ``[start_ms, end_ms]`` in ``step_ms`` steps. ``clock`` (paper mode) overrides the grid time with an injected live-like clock; replay uses the grid.

    ``gates`` (optional, duck-typed; ms_pipeline passes the multi-symbol authority): ``bind(WorldProbe)`` once, ``on_plan(plan) -> decision`` (``.approved``/``.reasons``) BEFORE a plan is
    submitted (a refusal leaves the group FAILED, never submitted), ``on_order(OrderRequest) -> Optional[str]`` at every send (a reason = the order never reaches the venue) and
    ``on_step(now)`` after every lifecycle step."""
    if not callable(risk_assess):
        raise ValueError("risk_assess is required: replay never allocates without the risk gate")
    w = World(series, cfg)
    tr = ReplayTransport(w, cfg, venue, order_gate=gates.on_order if gates is not None else None)
    store = store if store is not None else EX.MemoryStore()
    qty_steps = {}
    for x in series:
        st = (x.instrument.qty_step if x.instrument and x.instrument.qty_step else cfg.default_qty_step)
        qty_steps[f"linear:{x.symbol}"] = st
        qty_steps[f"spot:{x.symbol}"] = st
    mk_engine = lambda: EX.ExecutionEngine(store, send_order=tr.send_order, cancel_order=tr.cancel_order, venue_state=tr.venue_state, policy=cfg.exec_policy, qty_steps=qty_steps)  # noqa: E731
    eng = mk_engine()
    led = ledger_factory()
    if isinstance(led, LocalLedger):
        led.exit_fee_bps = {"linear": float(cfg.fill.taker_bps_perp), "spot": float(cfg.fill.taker_bps_spot)}
    instruments: Dict[str, T.Instrument] = {}
    spot_instruments: Dict[str, T.Instrument] = {}
    for x in series:
        base = x.instrument or T.Instrument(x.symbol, None, "Trading", x.launch_ms, x.delivery_ms, x.interval_min, x.base_coin or x.symbol, "USDT", cfg.default_tick, cfg.default_qty_step,
                                            cfg.default_min_qty, cfg.default_max_qty, cfg.default_max_qty, cfg.default_min_notional, "10", x.symbol, "HEDGEABLE_ON_BYBIT" if x.spot_listed else "PERP_ONLY")
        if x.instrument is None:
            w.assumed.add("INSTRUMENT_FILTERS_DEFAULTED")
        instruments[x.symbol] = base
        spot_instruments[x.symbol] = base
    spot_depth = {x.symbol: cfg.synthetic_depth_usd for x in series}
    recs: Dict[str, GroupRecord] = {}
    fills: List[T.Fill] = []
    fills_cat: Dict[str, str] = {}
    seen_fill: set = set()
    decisions: List[Dict[str, Any]] = []
    fault_log: List[Dict[str, Any]] = []
    equity_curve: List[Tuple[int, float]] = []
    stats: Dict[str, Any] = {"liquidations": 0, "max_gross_usd": 0.0, "gross_sum": 0.0, "gross_n": 0, "max_open_groups": 0, "depth_limited": 0, "dup_fills_injected": 0,
                             "orders_unfilled": 0, "orders_partial": 0, "settlements": 0, "opportunities_withheld_by_halt": 0, "groups_submitted": 0, "groups_refused_at_approval": 0,
                             "groups_refused_at_submit": 0, "groups_refused_at_submit_by_halt": 0}
    peak = cfg.equity0
    day = {"ix": cfg.start_ms // 86_400_000, "eq": cfg.equity0}                 # UTC day and the equity at its first step: the state's ``day_start_equity_usd`` (hard daily-loss input)
    pending_faults = sorted(cfg.faults, key=lambda f: (f.ts_ms, f.kind))
    active_stale_book_until = -1
    stale_account_until = -1
    pos_unknown_until = -1
    halted_until: Dict[str, int] = {}
    dup_fill_next = [False]
    dup_ack_next = [False]
    hedged_ts: Dict[str, int] = {}
    ever_unwound: set = set()
    last_t = cfg.start_ms

    def rec_fill(f: T.Fill, cat: str) -> None:
        led.record_fill(f, category=cat)              # every ledger takes the category: a spot fill booked as linear would be paid funding (ms_accounting.Ledger defaults to linear)

    def flush(t: int) -> None:
        guard = 0
        while (tr.pending or tr.dones) and guard < 20:
            guard += 1
            pend, tr.pending = tr.pending, []
            for f in pend:
                if f.fill_id not in seen_fill:
                    seen_fill.add(f.fill_id)
                    fills.append(f)
                    fills_cat[f.fill_id] = tr.cat.get(f.fill_id, "linear")
                    rec_fill(f, fills_cat[f.fill_id])
                eng.on_fill(f, t)
                if dup_fill_next[0]:
                    dup_fill_next[0] = False
                    stats["dup_fills_injected"] += 1
                    eng.on_fill(f, t)
            for kind, usd, gid, sym in tr.slip:
                rs = getattr(led, "record_slippage", None)
                if rs is not None:
                    try:
                        rs(usd, kind, sym)
                    except TypeError:                              # a legacy ledger with the two-argument hook
                        rs(usd, kind)
                if kind == "adverse" and gid in recs:
                    recs[gid].adverse_usd += usd
                elif gid in recs:
                    recs[gid].taker_slip_usd += usd
            tr.slip = []
            dn, tr.dones = tr.dones, []
            for gid, lid in dn:
                eng.on_order_done(gid, lid, t)

    def pos_qty(gid: str, sym: str, category: str, ts: int) -> float:
        q = 0.0
        for f in fills:
            if f.group_id == gid and f.symbol == sym and fills_cat.get(f.fill_id) == category and f.ts_ms < ts:
                q += f.qty if f.side == "Buy" else -f.qty
        return q

    def snapshots(t: int) -> Dict[str, T.MarketSnapshot]:
        out = {}
        for sym, x in w.s.items():
            m = w.mark(sym, t)
            if m is None:
                continue
            bids, asks, age = w.book(sym, "linear", t)
            if t <= active_stale_book_until:
                age = cfg.stale_book_age_ms
            tf = w.trailing_funding(sym, t)
            out[sym] = T.MarketSnapshot(symbol=sym, decision_ms=t, mark=m, index=m, bids=bids, asks=asks, book_age_ms=age, last_funding_bps_per_print=tf[-1] if tf else None,
                                        trailing_funding_bps_per_print=tf, trailing_funding_intervals_min=w.trailing_funding_intervals(sym, t), funding_interval_min=w.interval_min(sym, t),
                                        next_funding_ms=w.next_funding(sym, t), open_interest_usd=x.oi_usd, turnover24h_usd=None, spot_mark=w.spot_mark(sym, t))
        return out

    def portfolio_state(t: int, snaps: Mapping[str, T.MarketSnapshot], account_ts: Optional[int]) -> PF.PortfolioState:
        pnl = led.pnl()
        equity = cfg.equity0 + pnl["net"]
        positions: Dict[str, PF.Position] = {}
        cap, margin = 0.0, 0.0
        lev = max(1.0, min(cfg.portfolio.perp_leverage, 1.0 / (cfg.portfolio.min_liq_distance + cfg.portfolio.maint_margin_frac)))
        for k, q in eng.positions_exp.items():
            cat, sym = k.split(":", 1)
            if q == 0 or sym not in snaps:
                continue
            px = snaps[sym].mark if cat == "linear" else (snaps[sym].spot_mark or snaps[sym].mark)
            n = float(q) * px
            pk = sym if cat == "linear" else "SPOT:" + sym
            positions[pk] = PF.Position(pk, cat, n, snaps[sym].next_funding_ms if cat == "linear" else None, instruments[sym].base_coin or sym, instruments[sym].quote_coin)
            cap += abs(n) if cat == "spot" else abs(n) / lev
            margin += abs(n) / lev if cat == "linear" else 0.0
        return PF.PortfolioState(equity, equity - cap, max(peak, equity), positions, eng.open_groups(), margin, t, account_ts, day_start_equity_usd=day["eq"])

    now = cfg.start_ms
    if gates is not None:
        gates.bind(WorldProbe(now=lambda: now, snapshots=lambda: snapshots(now),
                              risk_state=lambda: portfolio_state(now, snapshots(now), (now - cfg.exec_policy.max_account_age_ms - 1) if now <= stale_account_until else now).to_risk_state(),
                              engine=lambda: eng, portfolio_state=lambda: portfolio_state(now, snapshots(now), now)))
    t = cfg.start_ms
    next_decision = cfg.start_ms
    while t <= cfg.end_ms:
        now = clock() if clock is not None else t
        tr.now = now
        # (1) faults
        while pending_faults and pending_faults[0].ts_ms <= now:
            f = pending_faults.pop(0)
            fault_log.append({"ts_ms": now, "kind": f.kind, "symbol": f.symbol})
            if f.kind in ("LOST_ACK", "LOST_REQUEST", "CANCEL_LOST"):
                tr.fault_next.append(f.kind)
            elif f.kind == "DUPLICATE_FILL":
                dup_fill_next[0] = True
            elif f.kind == "DUPLICATE_ACK":
                dup_ack_next[0] = True
            elif f.kind == "STALE_BOOK":
                active_stale_book_until = f.until_ms if f.until_ms is not None else now
            elif f.kind == "STALE_ACCOUNT":
                stale_account_until = f.until_ms if f.until_ms is not None else now
            elif f.kind == "POSITIONS_UNKNOWN":
                tr.venue.positions_unknown = True
                pos_unknown_until = f.until_ms if f.until_ms is not None else now
            elif f.kind == "INSTRUMENT_HALT":
                tr.halted.add(f.symbol)
                halted_until[f.symbol] = f.until_ms if f.until_ms is not None else now
                eng.on_instrument_halt(f.symbol, now)
            elif f.kind == "INTERVAL_CHANGE":
                eng.on_funding_interval_change(f.symbol, w.interval_min(f.symbol, now), f.value, now)
            elif f.kind == "RISK_BREACH":
                eng.on_risk_breach([f"INJECTED:{f.symbol or 'PORTFOLIO'}"], now)
            elif f.kind == "RESTART":
                eng = mk_engine()
                eng.reconcile(now)
        if pos_unknown_until >= 0 and now > pos_unknown_until:
            tr.venue.positions_unknown = False
            pos_unknown_until = -1
        for sym in [s for s, u in halted_until.items() if now > u]:
            tr.halted.discard(sym)
            halted_until.pop(sym)
            eng.on_instrument_resume(sym, now)
        # (2) snapshots + marks
        snaps = snapshots(now)
        for sym, s in snaps.items():
            led.mark(sym, now, s.mark, bid=s.bids[0].price if s.bids else None, ask=s.asks[0].price if s.asks else None)         # open legs are valued at the EXIT side of the book
            if s.spot_mark is not None:
                sb, sa, _ = w.book(sym, "spot", now)
                led.mark("SPOT:" + sym, now, s.spot_mark, bid=sb[0].price if sb else None, ask=sa[0].price if sa else None)
        # (3) resting orders
        tr.advance(now)
        flush(now)
        # (4) liquidation
        lev = max(1.0, min(cfg.portfolio.perp_leverage, 1.0 / (cfg.portfolio.min_liq_distance + cfg.portfolio.maint_margin_frac)))
        dist = 1.0 / lev - cfg.portfolio.maint_margin_frac
        for gid, g in list(eng.groups.items()):
            if g.state in (G.CLOSED, G.FAILED):
                continue
            for l in g.entry_legs():
                if l.plan.category != "linear" or eng._net(l) <= 0 or l.plan.symbol not in snaps:
                    continue
                entry, m = float(l.avg_px), snaps[l.plan.symbol].mark
                liq_px = entry * (1 + dist) if l.plan.side == "Sell" else entry * (1 - dist)
                if (l.plan.side == "Sell" and m >= liq_px) or (l.plan.side == "Buy" and m <= liq_px):
                    q = float(eng._net(l))
                    side = "Buy" if l.plan.side == "Sell" else "Sell"
                    tr.venue.force_close(l.plan.symbol, "linear", side, q, liq_px, now)
                    ff = T.Fill(f"FORCED:{gid}:{l.plan.leg_id}:{now}", gid, l.plan.leg_id, l.plan.symbol, side, q, liq_px, q * liq_px * cfg.liq_fee_bps / 1e4, "USDT", "TAKER", now, "SIMULATED")
                    seen_fill.add(ff.fill_id)
                    fills.append(ff)
                    fills_cat[ff.fill_id] = "linear"
                    rec_fill(ff, "linear")
                    eng.on_forced_close(gid, l.plan.leg_id, q, now)
                    stats["liquidations"] += 1
        flush(now)
        # (5) funding settlements in (last_t, now]
        for sym, x in w.s.items():
            i0, i1 = bisect.bisect_right(w._fk[sym], last_t), bisect.bisect_right(w._fk[sym], now)
            for ts, rate in x.funding[i0:i1]:
                m = w.mark(sym, ts)
                total, held, unknown = 0.0, False, False
                for gid in list(recs):
                    q = pos_qty(gid, sym, "linear", ts)
                    if abs(q) <= 1e-12:
                        continue
                    held = True
                    if m is None:                                          # a held position whose settlement price is unknown: the amount is UNKNOWN, never skipped, never zero
                        unknown = True
                        continue
                    amt = -q * m * rate / 1e4                              # signed qty: a short (q < 0) with rate > 0 RECEIVES
                    recs[gid].funding_usd += amt
                    total += amt
                    stats["settlements"] += 1
                if held:                                                   # ONE ledger entry per (symbol, print): ms_accounting keys funding on it (a 2nd differing amount is a conflict)
                    led.record_funding(sym, ts, None if unknown else total, "UNKNOWN" if unknown else "DERIVED")
                    stats["funding_unknown"] = stats.get("funding_unknown", 0) + (1 if unknown else 0)
        # (6) lifecycle step
        eng.step(now, market_ts_ms=now, account_ts_ms=(now - cfg.exec_policy.max_account_age_ms - 1) if now <= stale_account_until else now)
        flush(now)
        # (7) exits and bookkeeping
        for gid, g in list(eng.groups.items()):
            rec = recs.get(gid)
            if rec is None:
                continue
            if g.exit_mode == "UNWIND":
                ever_unwound.add(gid)
            if g.state == G.HEDGED and g.exit_mode is None:
                hedged_ts.setdefault(gid, now)
                if rec.regime == "ALL" and g.plan.legs:
                    sym0 = g.plan.legs[0].symbol
                    tf = w.trailing_funding(sym0, now)
                    per_day = T.funding_bps_per_day(tf[-1], w.print_interval_min(sym0, w.last_print_ts(sym0, now))) if tf else None
                    rec.regime = "UNKNOWN" if per_day is None else ("LOW" if abs(per_day) < cfg.regime_low_bps_day else "HIGH" if abs(per_day) >= cfg.regime_high_bps_day else "MID")
                reason = None
                if now - hedged_ts[gid] >= cfg.hold_ms:
                    reason = "HOLD_ELAPSED"
                else:
                    for lp in g.plan.legs:
                        x = w.s.get(lp.symbol)
                        if x and x.delivery_ms is not None and x.delivery_ms - now <= 2 * cfg.step_ms:
                            reason = "DELIVERY_NEAR"
                    if reason is None and cfg.exit_funding_below_bps is not None:
                        sym0 = g.plan.legs[0].symbol
                        tf = w.trailing_funding(sym0, now)
                        perp = next((l for l in g.plan.legs if l.category == "linear"), None)
                        if tf and perp is not None and (tf[-1] * (1 if perp.side == "Sell" else -1)) < cfg.exit_funding_below_bps:
                            reason = "FUNDING_BELOW_EXIT_THRESHOLD"
                if reason:
                    eng.request_exit(gid, now, reason)
                    flush(now)
            if g.state == G.CLOSED and not rec.closed:
                rec.closed = True
                rec.exit_ts = max([f.ts_ms for f in fills if f.group_id == gid] or [now])
                rec.kind = "UNWOUND" if gid in ever_unwound else "NORMAL"
        if gates is not None:
            gates.on_step(now)
        # (8) equity / drawdown
        pnl = led.pnl()
        equity = cfg.equity0 + pnl["net"]
        peak = max(peak, equity)
        if now // 86_400_000 != day["ix"]:
            day["ix"], day["eq"] = now // 86_400_000, equity
        equity_curve.append((now, equity))
        gross = sum(abs(float(q)) * (snaps[k.split(":", 1)[1]].mark if k.startswith("linear") else (snaps[k.split(":", 1)[1]].spot_mark or snaps[k.split(":", 1)[1]].mark))
                    for k, q in eng.positions_exp.items() if k.split(":", 1)[1] in snaps)
        stats["max_gross_usd"] = max(stats["max_gross_usd"], gross)
        stats["gross_sum"] += gross
        stats["gross_n"] += 1
        stats["max_open_groups"] = max(stats["max_open_groups"], eng.open_groups())
        # (9) decisions
        if now >= next_decision:
            next_decision = now + cfg.decision_every_ms
            account_ts = now - cfg.exec_policy.max_account_age_ms - 1 if now <= stale_account_until else now
            st = portfolio_state(now, snaps, account_ts)
            opps = scan_fn(now, snaps, instruments)
            halt_reason, blocked = halt_gate(eng)                  # the allocator is blind to engine halts: never plan a group the engine will only FAIL (wasted allocation, inflated counts)
            if halt_reason is not None:
                withheld = [o for o in opps if o.legs and not o.exclusions]
                opps = []
            else:
                withheld = [o for o in opps if o.legs and not o.exclusions and _opp_symbols(o) & blocked]
                opps = [o for o in opps if o not in withheld]
            if withheld:
                stats["opportunities_withheld_by_halt"] += len(withheld)
                decisions.append({"ts_ms": now, "candidate_id": "*", "action": "WITHHELD_BY_HALT", "binding": "ENGINE_HALT", "notional_usd": 0.0, "n_opportunities": len(withheld),
                                  "reasons": [halt_reason or ("SYMBOL_HALTED_OR_BLOCKED:" + ",".join(sorted(blocked & {s for o in withheld for s in _opp_symbols(o)})))]})
            alloc = PF.allocate(opps, state=st, policy=cfg.portfolio, instruments=instruments, snapshots=snaps, decision_ms=now, spot_instruments=spot_instruments,
                                spot_depth_usd=spot_depth, risk_assess=risk_assess, risk_policy=risk_policy)
            for d in alloc.decisions:
                decisions.append({"ts_ms": now, "candidate_id": d.candidate_id, "action": d.action, "reasons": list(d.reasons), "binding": d.binding, "notional_usd": d.notional_usd})
                if d.action == "SKIP" and d.reasons and d.reasons[0].startswith(("CONSTRAINT_EXHAUSTED:DEPTH", "BELOW_MIN")):
                    stats["depth_limited"] += 1
            for p in alloc.plans:
                rec = recs.setdefault(p.group_id, GroupRecord(p.group_id, p.candidate_id, tuple(sorted({l.symbol for l in p.legs})), notional_usd=p.notional_usd / max(1, len(p.legs))))
                verdict = gates.on_plan(p) if gates is not None else _APPROVED_BY_ALLOCATOR_RISK       # allocate() only returns plans risk_assess approved; an authority may still refuse
                eng.plan_group(p, now)
                eng.approve_group(p.group_id, verdict, now)
                if not getattr(verdict, "approved", False):
                    stats["groups_refused_at_approval"] += 1
                    continue
                pre_halt = halt_gate(eng)
                if eng.submit_group(p.group_id, now, market_ts_ms=now, account_ts_ms=account_ts) == G.FAILED and all(l.attempts == 0 for l in eng.groups[p.group_id].legs.values()):
                    stats["groups_refused_at_submit"] += 1                          # never sent: the engine's own gate (halt, stale data, blocked symbol) refused it
                    if pre_halt[0] is not None or (pre_halt[1] & {l.symbol for l in p.legs}):
                        stats["groups_refused_at_submit_by_halt"] += 1
                else:
                    stats["groups_submitted"] += 1
                flush(now)
                if dup_ack_next[0]:
                    dup_ack_next[0] = False
                    for l in eng.groups[p.group_id].legs.values():
                        if l.order_id:
                            eng.on_ack(EX.Ack(l.link, "ACCEPTED", l.order_id), p.group_id, l.plan.leg_id, now)
                            break
        last_t = now
        t += cfg.step_ms
    # ------ finish: per-group economics
    for gid, rec in recs.items():
        gf = [f for f in fills if f.group_id == gid]
        rec.price_usd = sum((-1 if f.side == "Buy" else 1) * f.qty * f.price for f in gf)
        last_marks = {}
        for f in gf:
            k = (f.symbol, fills_cat.get(f.fill_id))
            last_marks[k] = last_marks.get(k, 0.0) + (f.qty if f.side == "Buy" else -f.qty)
        for (sym, cat), q in last_marks.items():
            if abs(q) > 1e-12:
                m = w.mark(sym, cfg.end_ms) if cat == "linear" else w.spot_mark(sym, cfg.end_ms)
                bb, ba, _ = w.book(sym, cat, cfg.end_ms)
                if bb and ba:                                       # what CLOSING it would yield: a long at the bid, a short at the ask, less the taker exit fee (same as the ledger)
                    px = bb[0].price if q > 0 else ba[0].price
                    rec.price_usd += q * px - abs(q) * px * (cfg.fill.taker_bps_perp if cat == "linear" else cfg.fill.taker_bps_spot) / 1e4
                else:
                    rec.price_usd += q * (m or 0.0)
                    rec.mid_fallback = True
        rec.fees_usd = sum(f.fee_usd for f in gf)
        if gf:
            rec.entry_ts = min(f.ts_ms for f in gf)
            perp = [f for f in gf if fills_cat.get(f.fill_id) == "linear"]
            ent_side = next((l.plan.side for g2 in [eng.groups[gid]] for l in g2.entry_legs() if l.plan.category == "linear"), None)
            ent = [f for f in perp if f.side == ent_side] if ent_side else perp
            rec.notional_usd = sum(f.qty * f.price for f in ent) or rec.notional_usd
    for o in tr.sim.values():
        if o.filled <= 1e-12:
            stats["orders_unfilled"] += 1
        elif o.filled < o.qty - 1e-9:
            stats["orders_partial"] += 1
    pnl = led.pnl()
    return ReplayResult(cfg, equity_curve, recs, pnl, {**stats, "transport": tr.stats, "queue_bases": tr.queue_bases, "ack_dups": sum(g.ack_dups for g in eng.groups.values()),
                                                      "fill_dups": sum(g.fill_dups for g in eng.groups.values()), "halts": sorted(eng.halts)},
                        fills, fills_cat, decisions, fault_log, sorted(w.assumed), sorted(w.measured), eng.journal.head, eng.states(), eng, tr.venue)


class _Approved:
    approved, reasons, limits = True, ("APPROVED_BY_RISK_ASSESS_IN_ALLOCATE",), {}


_APPROVED_BY_ALLOCATOR_RISK = _Approved()


# ------------------------------------------------------------------ observations, independence, trials ledger

@dataclass(frozen=True)
class Observation:
    group_id: str
    symbols: Tuple[str, ...]
    entry_ts: int
    exit_ts: int
    net_usd: float
    net_bps: float
    regime: str
    kind: str


def observations(res: ReplayResult) -> List[Observation]:
    out = []
    for gid, r in sorted(res.groups.items()):
        if not r.closed or r.entry_ts is None or r.exit_ts is None or r.notional_usd <= 0:
            continue
        net = r.price_usd + r.funding_usd - r.fees_usd - r.adverse_usd
        out.append(Observation(gid, r.symbols, r.entry_ts, r.exit_ts, net, net / r.notional_usd * 1e4, r.regime, r.kind))
    return out


def independent_observations(obs: Sequence[Observation], embargo_ms: int = 0) -> List[Observation]:
    """Earliest-finish greedy: keep an observation only if it starts AFTER the last kept one ended (+embargo). Overlapping trades (across ANY symbols: they share the market
    and the same funding regime) are NOT independent and never counted as such."""
    kept: List[Observation] = []
    end = None
    for o in sorted(obs, key=lambda x: (x.exit_ts, x.entry_ts, x.group_id)):
        if end is None or o.entry_ts > end + embargo_ms:
            kept.append(o)
            end = o.exit_ts
    return kept


def record_trials(path: str, keys: Sequence[str]) -> int:
    """Persistent multiple-testing ledger (append-only jsonl of distinct variant keys ever evaluated). Returns the distinct total after adding ``keys``."""
    have = set()
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                if line.strip():
                    have.add(json.loads(line)["key"])
    new = [k for k in dict.fromkeys(keys) if k not in have]
    if new:
        with open(path, "a", encoding="utf-8") as fh:
            for k in new:
                fh.write(json.dumps({"key": k}, sort_keys=True) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
    return len(have) + len(new)


def variant_key(variant_id: str, spec_digest: str) -> str:
    return hashlib.sha256(f"{variant_id}|{spec_digest}".encode()).hexdigest()[:24]


def _clean(x: Any) -> Any:
    if isinstance(x, float):
        return x if math.isfinite(x) else None
    if isinstance(x, dict):
        return {str(k): _clean(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_clean(v) for v in x]
    return x


def _ci(values: Sequence[float], trials: int) -> Dict[str, Any]:
    n = len(values)
    if n < 2:
        return {"n": n, "mean": values[0] if n else None, "lo95": None, "hi95": None, "lo_adj": None, "hi_adj": None, "method": "NONE_N_LT_2", "reliable": False}
    m, sd = statistics.fmean(values), statistics.stdev(values)
    se = sd / math.sqrt(n)
    nd = statistics.NormalDist()
    z, za = nd.inv_cdf(0.975), nd.inv_cdf(1.0 - 0.05 / max(1, trials) / 2.0)
    return {"n": n, "mean": m, "lo95": m - z * se, "hi95": m + z * se, "lo_adj": m - za * se, "hi_adj": m + za * se, "method": "NORMAL_ON_INDEPENDENT_OBS",
            "reliable": n >= MIN_INDEPENDENT_FOR_ANY_CLAIM, "adjusted_for_trials": max(1, trials)}


class ReportIdentityError(AssertionError):
    """The reported net is not gross - fees - slippage (taker + adverse selection): the ledger behind the run states a different net definition."""


IDENTITY_TOL = 1e-6


def assert_net_identity(pnl: Mapping[str, Any], label: str = "") -> None:
    """THE net identity: net == (price_leg at mid + funding) - fees - slippage, slippage == taker slippage + adverse selection, within 1e-6 USD."""
    g = float(pnl["price_leg"]) + float(pnl["funding"])
    slip = float(pnl["taker_slippage"]) + float(pnl["adverse_selection"])
    tol = IDENTITY_TOL + 1e-12 * abs(g)
    if abs(float(pnl["slippage"]) - slip) > tol:
        raise ReportIdentityError(f"{label}slippage {pnl['slippage']!r} != taker {pnl['taker_slippage']!r} + adverse {pnl['adverse_selection']!r}")
    want = g - float(pnl["fees"]) - float(pnl["slippage"])
    if abs(float(pnl["net"]) - want) > tol:
        raise ReportIdentityError(f"{label}net {pnl['net']!r} != gross {g!r} - fees {pnl['fees']!r} - slippage {pnl['slippage']!r} (= {want!r})")


def build_report(results: Sequence[ReplayResult], *, trials_total: Optional[int] = None, trials_this_run: int = 1, persistent_trials: bool = False,
                 variants: Optional[Mapping[str, Any]] = None, extra: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    """Machine-readable ReplayReport (a JSON-safe dict) over one or several (walk-forward test) replays."""
    cfg0 = results[0].cfg
    for i, r in enumerate(results):
        assert_net_identity(r.pnl, f"window {i}: ")
    valuation = "MID_FALLBACK" if any(r.pnl.get("valuation") == "MID_FALLBACK" or any(g.mid_fallback for g in r.groups.values()) for r in results) else "EXIT_SIDE"
    tot = {"price_leg": 0.0, "funding": 0.0, "fees": 0.0, "slippage": 0.0, "adverse_selection": 0.0, "taker_slippage": 0.0, "net": 0.0}
    for r in results:
        for k in tot:
            tot[k] += float(r.pnl.get(k, 0.0))
    fills = [(f, r.fills_cat.get(f.fill_id, "linear")) for r in results for f in r.fills if not f.fill_id.startswith("FORCED")]
    mk = [f for f, _ in fills if f.liquidity == "MAKER"]
    tk = [f for f, _ in fills if f.liquidity == "TAKER"]
    mk_n, tk_n = sum(f.qty * f.price for f in mk), sum(f.qty * f.price for f in tk)
    obs = [o for r in results for o in observations(r)]
    indep = independent_observations(obs, cfg0.embargo_ms)
    trials = trials_total if trials_total is not None else trials_this_run
    ci = _ci([o.net_bps for o in indep], trials)
    # equity-derived risk
    steps_pnl: List[float] = []
    daily: Dict[int, float] = {}
    max_dd, max_dd_frac, under, nsteps = 0.0, 0.0, 0, 0
    for r in results:
        pk, prev = r.cfg.equity0, r.cfg.equity0
        for ts, eq in r.equity_curve:
            d = eq - prev
            steps_pnl.append(d)
            daily[ts // 86_400_000] = daily.get(ts // 86_400_000, 0.0) + d
            prev = eq
            pk = max(pk, eq)
            max_dd = max(max_dd, pk - eq)
            max_dd_frac = max(max_dd_frac, (pk - eq) / pk if pk > 0 else 0.0)
            under += 1 if eq < pk - 1e-9 else 0
            nsteps += 1
    srt = sorted(steps_pnl)
    k = max(1, int(math.ceil(0.05 * len(srt)))) if srt else 0
    es5 = statistics.fmean(srt[:k]) if srt else None
    dv = list(daily.values())
    sharpe = (statistics.fmean(dv) / statistics.stdev(dv) * math.sqrt(365.0)) if len(dv) >= 3 and statistics.pstdev(dv) > 0 else None
    by_year: Dict[str, Dict[str, Any]] = {}
    by_reg: Dict[str, Dict[str, Any]] = {}
    for o in obs:
        for bucket, key in ((by_year, str(_utc_year(o.exit_ts))), (by_reg, o.regime)):
            b = bucket.setdefault(key, {"n": 0, "net_usd": 0.0, "bps": []})
            b["n"] += 1
            b["net_usd"] += o.net_usd
            b["bps"].append(o.net_bps)
    for bucket in (by_year, by_reg):
        for b in bucket.values():
            b["mean_net_bps"] = statistics.fmean(b.pop("bps"))
    assumed = sorted({a for r in results for a in r.assumed})
    prov = T.weakest("MEASURED", "MODELLED", *(["ASSUMED"] if assumed else []))
    stats = [r.stats for r in results]
    ssum = lambda key: sum(s.get(key, 0) for s in stats)  # noqa: E731
    refused: Dict[str, int] = {}
    for s in stats:
        for kk, v in s["transport"]["refused"].items():
            refused[kk] = refused.get(kk, 0) + v
    groups = [g for r in results for g in r.groups.values()]
    no_fill_groups = sum(1 for r in results for gid, g in r.groups.items() if r.states.get(gid) == "FAILED" and not any(f.group_id == gid for f in r.fills))
    blockers = ["MODELLED_FILLS_ARE_NOT_EXECUTION_EVIDENCE"]
    if assumed:
        blockers.append("ASSUMED_INPUTS:" + ",".join(assumed))
    if len(indep) < MIN_INDEPENDENT_FOR_ANY_CLAIM:
        blockers.append(f"INDEPENDENT_OBSERVATIONS_{len(indep)}_LT_{MIN_INDEPENDENT_FOR_ANY_CLAIM}")
    rep = {
        "schema": "ms_replay_report/1", "variant_id": cfg0.variant_id, "spec_digest": cfg0.spec_digest, "mode": "REPLAY_SIMULATED_TRANSPORT",
        "window": {"start_ms": min(r.cfg.start_ms for r in results), "end_ms": max(r.cfg.end_ms for r in results), "step_ms": cfg0.step_ms, "n_windows": len(results)},
        "pnl": {"gross_usd": tot["price_leg"] + tot["funding"], "net_usd": tot["net"], "fees_usd": tot["fees"], "funding_usd": tot["funding"], "price_leg_usd": tot["price_leg"],
                "valuation": valuation, "slippage_usd": tot["slippage"], "taker_slippage_usd": tot["taker_slippage"], "adverse_selection_usd": tot["adverse_selection"],
                "note": "ONE net definition: net = gross - fees - slippage with gross = price leg AT MID + funding; slippage = taker walk + adverse selection, each charged exactly once. The "
                        "taker walk is already inside the fill prices (the price leg adds it back to reach mid, then it is subtracted as slippage); adverse selection is a modelled post-fill "
                        "markout that is in NO fill price, so it is subtracted here and only here. Fees are subtracted once. Funding is credited/debited at the actual settlement timestamp"},
        "execution_mix": {"maker_fills": len(mk), "taker_fills": len(tk), "maker_notional_usd": mk_n, "taker_notional_usd": tk_n,
                          "maker_share_notional": mk_n / (mk_n + tk_n) if (mk_n + tk_n) > 0 else None},
        "fills": {"orders_placed": sum(s["transport"]["orders_placed"] for s in stats), "orders_unfilled": ssum("orders_unfilled"), "orders_partially_filled": ssum("orders_partial"),
                  "groups_planned": len(groups), "groups_submitted": ssum("groups_submitted"), "groups_refused_at_approval": ssum("groups_refused_at_approval"),
                  "groups_refused_at_submit": ssum("groups_refused_at_submit"),
                  "groups_refused_by_halt": ssum("opportunities_withheld_by_halt") + ssum("groups_refused_at_submit_by_halt"),
                  "opportunities_withheld_by_halt": ssum("opportunities_withheld_by_halt"),
                  "groups_note": "groups_planned = plans the allocator produced = submitted + refused_at_approval + refused_at_submit; groups_refused_by_halt counts opportunities WITHHELD before "
                                 "allocation while the engine was halted / the symbol halted or blocked (never planned) plus plans the engine refused at submit because of a halt",
                  "groups_with_no_fill": no_fill_groups, "placement_refused": refused, "queue_bases": {kk: sum(s["queue_bases"].get(kk, 0) for s in stats) for kk in sorted({q for s in stats for q in s["queue_bases"]})}},
        "capacity": {"max_gross_usd": max(s["max_gross_usd"] for s in stats), "mean_gross_usd": (sum(s["gross_sum"] for s in stats) / max(1, sum(s["gross_n"] for s in stats))),
                     "max_open_groups": max(s["max_open_groups"] for s in stats), "allocation_skips_size_or_depth": ssum("depth_limited")},
        "risk": {"max_drawdown_usd": max_dd, "max_drawdown_frac": max_dd_frac, "expected_shortfall_5pct_step_usd": es5, "time_under_water_frac": (under / nsteps) if nsteps else None,
                 "liquidations": ssum("liquidations"), "sharpe_daily_annualised": sharpe},
        "observations": {"closed_round_trips": len(obs), "independent": len(indep), "dropped_as_overlapping": len(obs) - len(indep), "overlap_policy": "GLOBAL_NON_OVERLAP_EARLIEST_FINISH",
                         "embargo_ms": cfg0.embargo_ms, "mean_net_bps_all": statistics.fmean([o.net_bps for o in obs]) if obs else None, "net_bps_independent": _ci([o.net_bps for o in indep], trials),
                         "ci95_independent_net_bps": ci},
        "by_year": by_year, "by_regime": by_reg,
        "multiple_testing": {"trials_this_run": trials_this_run, "trials_total": trials, "persistent_ledger": persistent_trials,
                             "note": "the interval *_adj is widened for trials_total; a non-persistent count UNDERSTATES the search"},
        "variants": dict(variants or {}),
        "faults_injected": [f for r in results for f in r.fault_log],
        "provenance": {"weakest": prov, "assumed_inputs": assumed, "measured_inputs": sorted({a for r in results for a in r.measured}), "fills": "MODELLED"},
        "promotion_eligible": False, "promotion_blockers": blockers,
        "journal_heads": [r.journal_head for r in results],
    }
    if extra:
        rep.update(extra)
    return _clean(rep)


def report_json(report: Mapping[str, Any]) -> str:
    return json.dumps(report, sort_keys=True, separators=(",", ":"), allow_nan=False)


# ------------------------------------------------------------------ purged / embargoed walk-forward

@dataclass(frozen=True)
class Split:
    fold: int
    train_start: int
    train_end: int            # inclusive upper bound of the train replay window
    test_start: int
    test_end: int             # exclusive


def walk_forward_splits(start_ms: int, end_ms: int, *, n_folds: int, test_ms: int, purge_ms: int, embargo_ms: int, min_train_ms: int = 0) -> List[Split]:
    """The last ``n_folds`` consecutive test windows of ``test_ms``; train = [start, test_start - purge - embargo]. The purge removes trades whose holding label could reach into
    the test window, the embargo is an extra gap; folds never overlap in test; train never reaches a later test window."""
    if n_folds < 1 or test_ms <= 0 or purge_ms < 0 or embargo_ms < 0:
        raise ValueError("bad split parameters")
    out = []
    for i in range(n_folds):
        ts = end_ms - (n_folds - i) * test_ms
        te = ts + test_ms
        tr_end = ts - purge_ms - embargo_ms
        if tr_end - start_ms < max(min_train_ms, 1):
            raise ValueError(f"fold {i}: train window too short after purge/embargo")
        out.append(Split(i, start_ms, tr_end, ts, te))
    return out


@dataclass(frozen=True)
class Variant:
    variant_id: str
    cfg: ReplayConfig
    scan_fn: Callable[[int, Mapping[str, T.MarketSnapshot], Mapping[str, T.Instrument]], List[T.Opportunity]]


def default_score(res: ReplayResult) -> float:
    """Train-window selection score: net P&L per unit of drawdown (+1 USD to keep it finite). -inf when there are no closed round trips (nothing to select on)."""
    if not observations(res):
        return float("-inf")
    peak, dd = res.cfg.equity0, 0.0
    for _, eq in res.equity_curve:
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
    return res.pnl["net"] / (1.0 + dd)


def run_walk_forward(series: Sequence[ReplaySeries], variants: Sequence[Variant], splits: Sequence[Split], *, risk_assess: Callable[..., Any],
                     risk_policy: Optional[Mapping[str, Any]] = None, score: Callable[[ReplayResult], float] = default_score, trials_path: Optional[str] = None) -> Dict[str, Any]:
    """Per fold: replay EVERY variant on the train window, select the best by ``score`` (ties by variant_id), then run ONLY the selected variant on the untouched test window."""
    if not variants:
        raise ValueError("no variants")
    for v in variants:
        for sp in splits:
            if sp.test_start - sp.train_end < v.cfg.hold_ms:
                raise ValueError(f"purge too small for variant {v.variant_id}: hold_ms {v.cfg.hold_ms} would reach into the test window")
    keys = [variant_key(v.variant_id, v.cfg.spec_digest) for v in variants]
    total = record_trials(trials_path, keys) if trials_path else len(set(keys))
    folds, tests = [], []
    for sp in splits:
        scored = []
        for v in sorted(variants, key=lambda x: x.variant_id):
            res = run_replay(series, replace(v.cfg, start_ms=sp.train_start, end_ms=sp.train_end), scan_fn=v.scan_fn, risk_assess=risk_assess, risk_policy=risk_policy)
            scored.append((score(res), v.variant_id, v))
        scored.sort(key=lambda x: (-x[0], x[1]))
        best = scored[0]
        entry = {"fold": sp.fold, "train": [sp.train_start, sp.train_end], "test": [sp.test_start, sp.test_end],
                 "scores": {vid: (None if not math.isfinite(sc) else sc) for sc, vid, _ in scored}}
        if not math.isfinite(best[0]):
            entry.update(selected=None, rejected=[vid for _, vid, _ in scored], note="NO_VARIANT_HAD_A_CLOSED_TRAIN_ROUND_TRIP")
        else:
            v = best[2]
            test = run_replay(series, replace(v.cfg, start_ms=sp.test_start, end_ms=sp.test_end - 1), scan_fn=v.scan_fn, risk_assess=risk_assess, risk_policy=risk_policy)
            tests.append(test)
            entry.update(selected=v.variant_id, rejected=[vid for _, vid, _ in scored if vid != v.variant_id])
        folds.append(entry)
    var_info = {"n_variants": len(variants), "folds": folds}
    rep = build_report(tests, trials_total=total, trials_this_run=len(set(keys)), persistent_trials=bool(trials_path), variants=var_info) if tests else {
        "schema": "ms_replay_report/1", "mode": "REPLAY_SIMULATED_TRANSPORT", "variants": var_info, "promotion_eligible": False,
        "promotion_blockers": ["NO_TEST_WINDOW_RAN"], "multiple_testing": {"trials_this_run": len(set(keys)), "trials_total": total, "persistent_ledger": bool(trials_path)}}
    return rep
