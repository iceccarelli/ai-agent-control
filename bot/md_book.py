"""A local order book maintained from Bybit public book frames, or a refusal.

The venue sends a `snapshot` and then `delta` frames. On the live feed, observed
over 60 s on both products, every delta's update id `u` was exactly the previous
one plus one and the cross-sequence `seq` rose strictly; so the rules below are
the venue's own, not a guess:

* a `snapshot` replaces the book (``u == 1`` is the venue announcing a restart);
* a `delta` applies only when ``u == last_u + 1`` and ``seq`` rose;
* ``u <= last_u`` is a duplicate or an old frame: dropped and counted;
* ``u > last_u + 1`` is a GAP: the book is no longer trustworthy and is marked
  unsynced until a snapshot arrives. It is never "patched" over;
* after every apply the book must be uncrossed and (when the instrument's tick
  is known) on-tick, else it is unsynced.

Depth queries never extrapolate: a walk that runs out of displayed size returns
``complete=False`` and no average price.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Dict, Optional, Tuple

import md_events as E

APPLIED = "APPLIED"
RESET = "RESET"
DUPLICATE = "DUPLICATE"
OUT_OF_ORDER = "OUT_OF_ORDER"
GAP = "GAP"
CROSSED = "CROSSED"
OFF_TICK = "OFF_TICK"
NOT_SYNCED = "NOT_SYNCED"
OVERSIZE = "OVERSIZE"

#: A book holding more than this many times its subscribed depth is corrupt
#: (the venue sends deletions), not deep.
MAX_DEPTH_MULTIPLE = 4


@dataclass(frozen=True)
class ApplyResult:
    status: str
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.status in (APPLIED, RESET)


@dataclass(frozen=True)
class Walk:
    """The result of consuming displayed size on one side."""

    side: str                  # "buy" consumes asks, "sell" consumes bids
    qty_asked: float
    qty_filled: float
    complete: bool             # the whole quantity was displayed
    avg_price: Optional[float]  # None unless complete
    worst_price: Optional[float]
    best_price: Optional[float]
    levels_used: int


class LocalBook:
    def __init__(self, category: str, symbol: str, depth: int,
                 tick_size: Optional[float] = None) -> None:
        self.category, self.symbol, self.depth = category, symbol, depth
        self.tick_size = tick_size
        self.bids: Dict[float, float] = {}
        self.asks: Dict[float, float] = {}
        self.update_id: Optional[int] = None
        self.seq: Optional[int] = None
        self.synced = False
        self.last_applied_mono_s: Optional[float] = None
        self.last_applied_wall_ms: Optional[int] = None
        self.last_exch_ts_ms: Optional[int] = None
        self.counts: Dict[str, int] = {}
        self.unsynced_reason = "NO_SNAPSHOT_YET"

    # -- applying frames --------------------------------------------------------

    def apply(self, ev) -> ApplyResult:
        if isinstance(ev, E.BookSnapshot):
            result = self._snapshot(ev)
        elif isinstance(ev, E.BookDelta):
            result = self._delta(ev)
        else:
            raise TypeError(f"not a book event: {type(ev).__name__}")
        self.counts[result.status] = self.counts.get(result.status, 0) + 1
        return result

    def _stamp(self, ev) -> None:
        self.last_applied_mono_s = ev.prov.recv_mono_s
        self.last_applied_wall_ms = ev.prov.recv_wall_ms
        self.last_exch_ts_ms = ev.prov.exch_ts_ms

    def _unsync(self, reason: str) -> None:
        self.synced = False
        self.unsynced_reason = reason

    def _snapshot(self, ev: E.BookSnapshot) -> ApplyResult:
        # An orderbook.1 stream is a run of snapshots; one that is not newer than
        # the book already held is old or repeated, unless the venue says it
        # restarted (u == 1).
        if (self.synced and ev.update_id != 1 and ev.seq is not None
                and self.seq is not None):
            if ev.seq == self.seq and ev.update_id == self.update_id:
                return ApplyResult(DUPLICATE, f"snapshot u={ev.update_id} repeated")
            if ev.seq < self.seq:
                return ApplyResult(OUT_OF_ORDER, f"snapshot seq {ev.seq} < {self.seq}")
        if self._off_tick(ev.bids + ev.asks):
            self._unsync("OFF_TICK")
            return ApplyResult(OFF_TICK, "snapshot price is not a multiple of the tick")
        self.bids = {p: q for p, q in ev.bids}
        self.asks = {p: q for p, q in ev.asks}
        self.update_id, self.seq = ev.update_id, ev.seq
        self.synced, self.unsynced_reason = True, ""
        self._stamp(ev)
        return ApplyResult(RESET, "venue restart (u=1)" if ev.update_id == 1 else "")

    def _delta(self, ev: E.BookDelta) -> ApplyResult:
        if not self.synced or self.update_id is None:
            return ApplyResult(NOT_SYNCED, self.unsynced_reason)
        if ev.update_id <= self.update_id:
            kind = DUPLICATE if ev.update_id == self.update_id else OUT_OF_ORDER
            return ApplyResult(kind, f"u {ev.update_id} <= {self.update_id}")
        if ev.update_id != self.update_id + 1:
            self._unsync("GAP")
            return ApplyResult(GAP, f"u jumped {self.update_id} -> {ev.update_id}")
        if ev.seq is not None and self.seq is not None and ev.seq <= self.seq:
            self._unsync("OUT_OF_ORDER")
            return ApplyResult(OUT_OF_ORDER, f"seq {ev.seq} <= {self.seq} with u in order")
        if self._off_tick(ev.bid_changes + ev.ask_changes):
            self._unsync("OFF_TICK")
            return ApplyResult(OFF_TICK, "delta price is not a multiple of the tick")
        bids, asks = dict(self.bids), dict(self.asks)
        for book, changes in ((bids, ev.bid_changes), (asks, ev.ask_changes)):
            for price, size in changes:
                if size == 0:
                    book.pop(price, None)
                else:
                    book[price] = size
        if bids and asks and max(bids) >= min(asks):
            self._unsync("CROSSED")
            return ApplyResult(CROSSED, f"bid {max(bids)} >= ask {min(asks)}")
        if len(bids) > MAX_DEPTH_MULTIPLE * self.depth or len(asks) > MAX_DEPTH_MULTIPLE * self.depth:
            self._unsync("OVERSIZE")
            return ApplyResult(OVERSIZE, f"{len(bids)}/{len(asks)} levels for depth {self.depth}")
        self.bids, self.asks = bids, asks
        self.update_id, self.seq = ev.update_id, ev.seq if ev.seq is not None else self.seq
        self._stamp(ev)
        return ApplyResult(APPLIED)

    def _off_tick(self, levels) -> bool:
        tick = self.tick_size
        if not tick or tick <= 0:
            return False
        for price, _ in levels:
            q = price / tick
            if abs(q - round(q)) > 1e-6 * max(1.0, abs(q)):
                return True
        return False

    def set_tick_size(self, tick: Optional[float]) -> None:
        self.tick_size = tick if tick and tick > 0 else None

    def invalidate(self, reason: str) -> None:
        """The owner of this book says it cannot be trusted (a resubscribe, a
        reconnect, a stale feed). It stays unsynced until the next snapshot."""
        self._unsync(reason)

    # -- reading ------------------------------------------------------------------

    def top(self) -> Optional[Tuple[float, float, float, float]]:
        """(bid, bid_size, ask, ask_size) of a synced, two-sided book, else None."""
        if not self.synced or not self.bids or not self.asks:
            return None
        b, a = max(self.bids), min(self.asks)
        return b, self.bids[b], a, self.asks[a]

    def ladders(self, levels: Optional[int] = None):
        """(bids, asks) best first as tuples of (price, size); None when unsynced."""
        if not self.synced or not self.bids or not self.asks:
            return None
        n = levels or self.depth
        return (tuple((p, self.bids[p]) for p in sorted(self.bids, reverse=True)[:n]),
                tuple((p, self.asks[p]) for p in sorted(self.asks)[:n]))

    def walk(self, side: str, qty: float) -> Optional[Walk]:
        """Consume `qty` of displayed size. ``buy`` takes asks, ``sell`` takes bids.
        None when the book is unsynced; ``complete=False`` (no average price)
        when the displayed size is smaller than `qty`."""
        if side not in ("buy", "sell"):
            raise ValueError(f"side {side!r}")
        if not (isinstance(qty, (int, float)) and not isinstance(qty, bool)
                and math.isfinite(qty) and qty > 0):
            raise ValueError(f"qty must be a finite positive number, got {qty!r}")
        lad = self.ladders()
        if lad is None:
            return None
        levels = lad[1] if side == "buy" else lad[0]
        left, cost, used = float(qty), 0.0, 0
        worst = None
        for price, size in levels:
            take = min(left, size)
            cost += take * price
            left -= take
            used += 1
            worst = price
            if left <= 1e-12:
                left = 0.0
                break
        filled = float(qty) - left
        done = left == 0.0
        return Walk(side, float(qty), filled, done,
                    (cost / filled) if (done and filled > 0) else None,
                    worst if filled > 0 else None,
                    levels[0][0] if levels else None, used)

    def digest(self) -> Tuple:
        """A canonical, comparable image of the book (used to prove replay equality)."""
        return (self.category, self.symbol, self.depth, self.synced, self.update_id, self.seq,
                tuple(sorted(self.bids.items(), reverse=True)),
                tuple(sorted(self.asks.items())))
