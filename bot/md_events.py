"""Typed, validated public market events and the provenance every one carries.

WHY THIS EXISTS
===============
The public Bybit feed is the only source of market truth the book is allowed to
trade on. Everything downstream (the local book, the market view, the carry
economics, the entry gate) consumes the types defined here and nothing else, so
a number can only reach a decision if it was parsed from a real frame, validated,
and stamped with where it came from.

Three rules shape these types:

* **Raw provenance is never dropped.** Every event names the stored raw frame it
  was parsed from (`Provenance.raw_id`), the connection, the topic, the local
  receive times (wall and monotonic), and the venue's own timestamps. A replay
  of the stored raw frames must rebuild the same events.
* **Rejections are data.** A frame that fails validation becomes a
  :class:`Rejection` with a machine-readable reason, never an exception that
  kills the stream and never a silent drop.
* **Nothing is invented.** There is no interpolation, no forward fill, no
  default price. A field the venue did not state is absent (``None``), and the
  caller decides what not knowing means.

Pure data: no I/O, no clock, no network.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Tuple

#: The two Bybit products the carry book needs. Same symbol string, different
#: market: the category is part of every event's identity.
SPOT = "spot"
LINEAR = "linear"
CATEGORIES = (SPOT, LINEAR)

SOURCE_WS = "ws"
SOURCE_REST = "rest"

#: A price level: (price, size), both finite, price > 0, size >= 0.
Level = Tuple[float, float]


@dataclass(frozen=True)
class Provenance:
    """Where an event came from. All times are explicit about whose clock."""

    #: Row id of the stored raw frame (0 before it is stored: a test or a
    #: dry parse). A replay reads the raw frames back in this order.
    raw_id: int
    source: str                      # SOURCE_WS | SOURCE_REST
    conn_id: str                     # the venue's connection id (ws) or "rest"
    topic: str                       # e.g. "orderbook.50.BTCUSDT" or the REST path
    recv_wall_ms: int                # this host's wall clock when the frame arrived
    recv_mono_s: float               # this host's monotonic clock, same instant
    exch_ts_ms: Optional[int] = None  # the venue's message timestamp (`ts`)
    exch_cts_ms: Optional[int] = None  # the venue's matching-engine time (`cts`)
    update_id: Optional[int] = None  # book `u`
    seq: Optional[int] = None        # venue cross-sequence

    @property
    def venue_lag_ms(self) -> Optional[int]:
        """Local receive time minus the venue's stamp. Not a latency: it also
        contains the clock offset between the two hosts. Used only to estimate
        skew and to notice a feed that is falling behind."""
        stamp = self.exch_cts_ms if self.exch_cts_ms is not None else self.exch_ts_ms
        return None if stamp is None else self.recv_wall_ms - stamp


@dataclass(frozen=True)
class BookSnapshot:
    category: str
    symbol: str
    depth: int                        # the subscribed depth (1, 50, 200 ...)
    bids: Tuple[Level, ...]           # best first
    asks: Tuple[Level, ...]           # best first
    update_id: int
    seq: Optional[int]
    #: True when the venue says this replaces any local book (a `snapshot` frame,
    #: or `u == 1` after a venue-side restart).
    resets: bool
    prov: Provenance


@dataclass(frozen=True)
class BookDelta:
    category: str
    symbol: str
    depth: int
    #: (price, size) changes; size 0 deletes the level.
    bid_changes: Tuple[Level, ...]
    ask_changes: Tuple[Level, ...]
    update_id: int
    seq: Optional[int]
    prov: Provenance


@dataclass(frozen=True)
class Ticker:
    """A ticker frame. `snapshot` carries every field, a `delta` only the ones
    that changed, so `fields` holds just what this frame stated."""

    category: str
    symbol: str
    is_snapshot: bool
    fields: Dict[str, Any]
    prov: Provenance


@dataclass(frozen=True)
class Trade:
    category: str
    symbol: str
    trade_id: str
    side: str                        # "Buy" | "Sell" (the taker's side)
    price: float
    qty: float
    trade_ts_ms: int
    seq: Optional[int]
    block_trade: bool
    prov: Provenance


@dataclass(frozen=True)
class InstrumentSpec:
    """Exchange-stated trading rules for one instrument (REST instruments-info)."""

    category: str
    symbol: str
    status: str
    tick_size: float
    qty_step: float
    min_qty: float
    max_qty: Optional[float]
    min_notional: Optional[float]
    base_coin: str
    quote_coin: str
    #: Linear only: the funding interval in minutes and the venue's rate bounds.
    funding_interval_min: Optional[int] = None
    upper_funding_rate: Optional[float] = None
    lower_funding_rate: Optional[float] = None
    #: The largest single MARKET order the venue accepts (a close larger than
    #: this must be split); None when the venue did not state one.
    max_market_qty: Optional[float] = None
    prov: Optional[Provenance] = None


@dataclass(frozen=True)
class FundingPrint:
    """One SETTLED funding rate (REST funding history). The only thing in this
    module that is what was actually paid, not a forecast."""

    symbol: str
    rate: float                      # a fraction, e.g. 0.0001 = 1 bp
    settled_ms: int
    prov: Provenance


@dataclass(frozen=True)
class Rejection:
    """A frame (or REST body) that failed validation, and why."""

    reason: str
    detail: str
    prov: Provenance
    category: Optional[str] = None
    symbol: Optional[str] = None


# -- rejection reasons (stable strings: they are persisted and counted) -----------------
NOT_JSON = "NOT_JSON"
NOT_AN_OBJECT = "NOT_AN_OBJECT"
UNKNOWN_TOPIC = "UNKNOWN_TOPIC"
MISSING_FIELD = "MISSING_FIELD"
BAD_NUMBER = "BAD_NUMBER"
BAD_PRICE = "BAD_PRICE"
BAD_QTY = "BAD_QTY"
BAD_TIMESTAMP = "BAD_TIMESTAMP"
BAD_SIDE = "BAD_SIDE"
BAD_SYMBOL = "BAD_SYMBOL"
BAD_SEQUENCE = "BAD_SEQUENCE"
BAD_BOOK_ORDER = "BAD_BOOK_ORDER"
CROSSED_BOOK = "CROSSED_BOOK"
OFF_TICK = "OFF_TICK"
RET_CODE_NOT_OK = "RET_CODE_NOT_OK"
GEO_BLOCKED = "GEO_BLOCKED"
RATE_LIMITED = "RATE_LIMITED"

# -- feed incidents (connection and sequence events the operator must be able to count) --
CONNECTED = "CONNECTED"
DISCONNECTED = "DISCONNECTED"
RECONNECT_BACKOFF = "RECONNECT_BACKOFF"
SUBSCRIBED = "SUBSCRIBED"
SUBSCRIBE_FAILED = "SUBSCRIBE_FAILED"
SEQUENCE_GAP = "SEQUENCE_GAP"
DUPLICATE_FRAME = "DUPLICATE_FRAME"
OUT_OF_ORDER = "OUT_OF_ORDER"
RESYNC_REQUESTED = "RESYNC_REQUESTED"
RESYNCED = "RESYNCED"
STALE_FEED = "STALE_FEED"
HEARTBEAT_LOST = "HEARTBEAT_LOST"
CLOCK_SKEW = "CLOCK_SKEW"
REST_WS_DISAGREE = "REST_WS_DISAGREE"
REST_BOOTSTRAPPED = "REST_BOOTSTRAPPED"
REST_UNAVAILABLE = "REST_UNAVAILABLE"
VENUE_SNAPSHOT_RESET = "VENUE_SNAPSHOT_RESET"


@dataclass(frozen=True)
class Incident:
    """Something the feed did or suffered that is not market data."""

    kind: str
    detail: str
    recv_wall_ms: int
    recv_mono_s: float
    conn_id: str = ""
    topic: str = ""
    data: Dict[str, Any] = field(default_factory=dict)
