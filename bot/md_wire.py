"""Parse and validate Bybit v5 public WebSocket frames into typed events.

Pure and total: :func:`parse_frame` never raises on venue input. A frame that is
not JSON, not an object, names a topic this module does not know, or carries a
value that is not a usable number becomes a :class:`~md_events.Rejection`; the
stream keeps running and the rejection is counted and stored. Nothing here
repairs, rounds, defaults or fills a value.

The shapes below are the ones observed on the live public feed (not assumed):

  orderbook.<depth>.<SYMBOL>   {"topic","type":"snapshot"|"delta","ts",
                                "data":{"s","b":[[p,q]..],"a":[[p,q]..],"u","seq"},
                                "cts"}
  tickers.<SYMBOL>             linear: one snapshot, then deltas carrying only the
                               changed fields; spot: periodic snapshots with no
                               bid/ask (the touch comes from the book)
  publicTrade.<SYMBOL>         {"data":[{"T","s","S","v","p","i","seq","BT"..}]}
  control                      {"op":"subscribe"|"ping"|..., "success":bool, ...}
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import md_events as E

#: A venue timestamp this far from our receive time is garbage, not skew
#: (skew is judged separately, and far tighter, by the market state).
MAX_ABS_TIMESTAMP_ERROR_MS = 24 * 3600 * 1000

#: |fundingRate| is a fraction (0.0001 = 1 bp). Venue caps are around 0.0033 to
#: 0.02; anything beyond 0.5 is not a funding rate.
MAX_ABS_FUNDING_RATE = 0.5

_TICKER_PRICES = ("lastPrice", "markPrice", "indexPrice", "bid1Price", "ask1Price",
                  "prevPrice24h", "highPrice24h", "lowPrice24h", "prevPrice1h",
                  "usdIndexPrice")
_TICKER_NONNEG = ("bid1Size", "ask1Size", "openInterest", "openInterestValue",
                  "turnover24h", "volume24h", "singleOpenInterest",
                  "singleOpenInterestValue", "fundingCap")
_TICKER_FINITE = ("price24hPcnt",)


class _Bad(Exception):
    def __init__(self, reason: str, detail: str):
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


@dataclass(frozen=True)
class FrameContext:
    """What the transport knows about a frame that the frame itself does not say."""

    raw_id: int
    category: str
    conn_id: str
    recv_wall_ms: int
    recv_mono_s: float
    source: str = E.SOURCE_WS


@dataclass(frozen=True)
class Parsed:
    events: Tuple[Any, ...] = ()
    rejections: Tuple[E.Rejection, ...] = ()
    #: A control frame (subscribe ack, pong ...), parsed JSON, else None.
    control: Optional[Dict[str, Any]] = None
    topic: str = ""


def _num(value: Any, name: str, *, positive: bool = False, nonneg: bool = False) -> float:
    if isinstance(value, bool) or value is None:
        raise _Bad(E.BAD_NUMBER, f"{name} is {value!r}")
    try:
        x = float(value)
    except (TypeError, ValueError):
        raise _Bad(E.BAD_NUMBER, f"{name} is {value!r}") from None
    if not math.isfinite(x):
        raise _Bad(E.BAD_NUMBER, f"{name} is not finite: {value!r}")
    if positive and not x > 0:
        raise _Bad(E.BAD_PRICE, f"{name} must be > 0, got {value!r}")
    if nonneg and x < 0:
        raise _Bad(E.BAD_QTY, f"{name} must be >= 0, got {value!r}")
    return x


def _int(value: Any, name: str, *, positive: bool = True) -> int:
    if isinstance(value, bool) or value is None:
        raise _Bad(E.BAD_NUMBER, f"{name} is {value!r}")
    try:
        x = int(float(value)) if not isinstance(value, int) else value
    except (TypeError, ValueError, OverflowError):
        raise _Bad(E.BAD_NUMBER, f"{name} is {value!r}") from None
    if positive and x <= 0:
        raise _Bad(E.BAD_NUMBER, f"{name} must be > 0, got {value!r}")
    return x


def _ts(value: Any, name: str, ctx: FrameContext) -> int:
    t = _int(value, name)
    if abs(t - ctx.recv_wall_ms) > MAX_ABS_TIMESTAMP_ERROR_MS:
        raise _Bad(E.BAD_TIMESTAMP,
                   f"{name} {t} is {(t - ctx.recv_wall_ms) / 3.6e6:.1f}h from receive time")
    return t


def _levels(raw: Any, name: str, *, ordered: Optional[str]) -> Tuple[E.Level, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise _Bad(E.MISSING_FIELD, f"{name} is not a list")
    out: List[E.Level] = []
    seen = set()
    prev: Optional[float] = None
    for row in raw:
        if not (isinstance(row, (list, tuple)) and len(row) >= 2):
            raise _Bad(E.BAD_PRICE, f"{name} level is {row!r}")
        price = _num(row[0], f"{name}.price", positive=True)
        size = _num(row[1], f"{name}.size", nonneg=True)
        if price in seen:
            raise _Bad(E.BAD_BOOK_ORDER, f"{name} repeats price {price}")
        seen.add(price)
        if ordered is not None and prev is not None:
            if (ordered == "desc" and price >= prev) or (ordered == "asc" and price <= prev):
                raise _Bad(E.BAD_BOOK_ORDER, f"{name} is not strictly {ordered}ending at {price}")
        prev = price
        out.append((price, size))
    return tuple(out)


def _prov(ctx: FrameContext, topic: str, j: Dict[str, Any], *,
          update_id: Optional[int] = None, seq: Optional[int] = None) -> E.Provenance:
    def opt(key: str) -> Optional[int]:
        v = j.get(key)
        try:
            return int(v) if v is not None and not isinstance(v, bool) else None
        except (TypeError, ValueError, OverflowError):
            return None
    return E.Provenance(raw_id=ctx.raw_id, source=ctx.source, conn_id=ctx.conn_id,
                        topic=topic, recv_wall_ms=ctx.recv_wall_ms,
                        recv_mono_s=ctx.recv_mono_s, exch_ts_ms=opt("ts"),
                        exch_cts_ms=opt("cts"), update_id=update_id, seq=seq)


def _split_topic(topic: str) -> Tuple[str, int, str]:
    """('orderbook', depth, symbol) | ('tickers', 0, symbol) | ('publicTrade', 0, symbol)."""
    parts = topic.split(".")
    if parts[0] == "orderbook" and len(parts) == 3 and parts[1].isdigit():
        return "orderbook", int(parts[1]), parts[2]
    if parts[0] in ("tickers", "publicTrade") and len(parts) == 2:
        return parts[0], 0, parts[1]
    raise _Bad(E.UNKNOWN_TOPIC, f"topic {topic!r}")


def _check_symbol(symbol: str) -> None:
    if not symbol or not symbol.isalnum() or symbol != symbol.upper():
        raise _Bad(E.BAD_SYMBOL, f"symbol {symbol!r}")


def parse_frame(raw: str, ctx: FrameContext) -> Parsed:
    """One raw text frame to events. Never raises on venue input."""
    def reject(reason: str, detail: str, topic: str = "", j: Optional[dict] = None,
               symbol: Optional[str] = None) -> Parsed:
        prov = _prov(ctx, topic, j or {})
        return Parsed(rejections=(E.Rejection(reason, detail[:300], prov, ctx.category, symbol),),
                      topic=topic)

    try:
        j = json.loads(raw)
    except (TypeError, ValueError):
        return reject(E.NOT_JSON, f"{str(raw)[:80]!r}")
    if not isinstance(j, dict):
        return reject(E.NOT_AN_OBJECT, f"{type(j).__name__}")
    topic = j.get("topic")
    if topic is None:
        if "op" in j or "success" in j or "ret_msg" in j:
            return Parsed(control=j)
        return reject(E.MISSING_FIELD, "frame has neither topic nor op", j=j)
    if not isinstance(topic, str):
        return reject(E.UNKNOWN_TOPIC, f"topic {topic!r}", j=j)
    try:
        kind, depth, symbol = _split_topic(topic)
        _check_symbol(symbol)
        typ = j.get("type")
        data = j.get("data")
        if kind == "orderbook":
            return _book(j, ctx, topic, depth, symbol, typ, data)
        if kind == "tickers":
            return _ticker(j, ctx, topic, symbol, typ, data)
        return _trades(j, ctx, topic, symbol, data)
    except _Bad as bad:
        sym = None
        try:
            sym = _split_topic(topic)[2]
        except _Bad:
            pass
        return reject(bad.reason, bad.detail, topic, j, sym)


def _book(j, ctx, topic, depth, symbol, typ, data) -> Parsed:
    if typ not in ("snapshot", "delta"):
        raise _Bad(E.MISSING_FIELD, f"book type {typ!r}")
    if not isinstance(data, dict):
        raise _Bad(E.MISSING_FIELD, "book data is not an object")
    if data.get("s") not in (None, symbol):
        raise _Bad(E.BAD_SYMBOL, f"book data names {data.get('s')!r}, topic names {symbol!r}")
    u = _int(data.get("u"), "u")
    seq = _int(data["seq"], "seq") if data.get("seq") is not None else None
    ts = _ts(j.get("ts"), "ts", ctx)
    if j.get("cts") is not None:
        _ts(j.get("cts"), "cts", ctx)
    prov = _prov(ctx, topic, dict(j, ts=ts), update_id=u, seq=seq)
    if typ == "snapshot":
        bids = _levels(data.get("b"), "b", ordered="desc")
        asks = _levels(data.get("a"), "a", ordered="asc")
        if bids and asks and bids[0][0] >= asks[0][0]:
            raise _Bad(E.CROSSED_BOOK, f"snapshot bid {bids[0][0]} >= ask {asks[0][0]}")
        if any(q <= 0 for _, q in bids + asks):
            raise _Bad(E.BAD_QTY, "snapshot level with zero size")
        return Parsed(events=(E.BookSnapshot(ctx.category, symbol, depth, bids, asks, u, seq,
                                             resets=True, prov=prov),), topic=topic)
    bch = _levels(data.get("b"), "b", ordered=None)
    ach = _levels(data.get("a"), "a", ordered=None)
    return Parsed(events=(E.BookDelta(ctx.category, symbol, depth, bch, ach, u, seq, prov),),
                  topic=topic)


def _ticker(j, ctx, topic, symbol, typ, data) -> Parsed:
    if typ not in ("snapshot", "delta"):
        raise _Bad(E.MISSING_FIELD, f"ticker type {typ!r}")
    if not isinstance(data, dict):
        raise _Bad(E.MISSING_FIELD, "ticker data is not an object")
    if data.get("symbol") not in (None, symbol):
        raise _Bad(E.BAD_SYMBOL, f"ticker data names {data.get('symbol')!r}")
    ts = _ts(j.get("ts"), "ts", ctx)
    fields: Dict[str, Any] = {}
    for key, value in data.items():
        if value is None or value == "":
            continue                     # "" is the venue's "not set", never zero
        if key in _TICKER_PRICES:
            fields[key] = _num(value, key, positive=True)
        elif key in _TICKER_NONNEG:
            fields[key] = _num(value, key, nonneg=True)
        elif key in _TICKER_FINITE:
            fields[key] = _num(value, key)
        elif key == "fundingRate":
            rate = _num(value, key)
            if abs(rate) > MAX_ABS_FUNDING_RATE:
                raise _Bad(E.BAD_NUMBER, f"fundingRate {rate} is not a funding rate")
            fields[key] = rate
        elif key == "nextFundingTime":
            fields[key] = _ts(value, key, ctx)
        elif key == "fundingIntervalHour":
            fields[key] = _int(value, key)
    prov = _prov(ctx, topic, dict(j, ts=ts), seq=_opt_int(j.get("cs")))
    return Parsed(events=(E.Ticker(ctx.category, symbol, typ == "snapshot", fields, prov),),
                  topic=topic)


def _opt_int(v: Any) -> Optional[int]:
    try:
        return int(v) if v is not None and not isinstance(v, bool) else None
    except (TypeError, ValueError, OverflowError):
        return None


def _trades(j, ctx, topic, symbol, data) -> Parsed:
    if not isinstance(data, list):
        raise _Bad(E.MISSING_FIELD, "trade data is not a list")
    events: List[Any] = []
    rejections: List[E.Rejection] = []
    for item in data:
        try:
            if not isinstance(item, dict):
                raise _Bad(E.MISSING_FIELD, f"trade is {type(item).__name__}")
            if item.get("s") not in (None, symbol):
                raise _Bad(E.BAD_SYMBOL, f"trade names {item.get('s')!r}")
            side = item.get("S")
            if side not in ("Buy", "Sell"):
                raise _Bad(E.BAD_SIDE, f"side {side!r}")
            tid = item.get("i")
            if not isinstance(tid, (str, int)) or isinstance(tid, bool) or str(tid) == "":
                raise _Bad(E.MISSING_FIELD, f"trade id {tid!r}")
            price = _num(item.get("p"), "p", positive=True)
            qty = _num(item.get("v"), "v", nonneg=True)
            if qty <= 0:
                raise _Bad(E.BAD_QTY, f"trade size must be > 0, got {item.get('v')!r}")
            t_ms = _ts(item.get("T"), "T", ctx)
            seq = _opt_int(item.get("seq"))
            prov = _prov(ctx, topic, j, seq=seq)
            events.append(E.Trade(ctx.category, symbol, str(tid), side, price, qty, t_ms, seq,
                                  bool(item.get("BT")), prov))
        except _Bad as bad:
            rejections.append(E.Rejection(bad.reason, bad.detail[:300], _prov(ctx, topic, j),
                                          ctx.category, symbol))
    return Parsed(events=tuple(events), rejections=tuple(rejections), topic=topic)
