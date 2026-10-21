"""Funding the venue settled against a directional linear position.

A perpetual position that is held across a funding time is paid or charged by
the venue; that money is part of the trade's result and, for a short in a
positive-funding market, usually most of it.  The engine books a linear trade's
price P&L and fees; this module books the rest from the account's own
SETTLEMENT records (`CarryBroker.get_funding_evidence`, read-only), once each,
onto the exit row that was still open when the venue settled.

Nothing is estimated.  A row ends in exactly one explicit status:

``BOOKED_FROM_SETTLEMENTS``      settlements inside the hold window, summed
``NO_SETTLEMENT_IN_HOLD_WINDOW`` the log was read completely and holds none
                                 (a hold shorter than the funding interval)
and, left UNBOOKED (so the statement cannot call the result confirmed):
``PENDING_SETTLEMENT_GRACE``     the exit is too recent for late rows
``EVIDENCE_UNREAD`` / ``EVIDENCE_INCOMPLETE``  the log could not be read whole
``SUSPECT_NO_SETTLEMENT_OVER_INTERVAL``  held a full funding interval yet the
                                 venue lists no settlement
``AMBIGUOUS_SHARED_WITH_CARRY``  the symbol is also traded by the carry book,
                                 whose legs settle on the same account row
``DUPLICATE_ID_CONFLICT``        one settlement id with two different amounts

Sign: positive means the account RECEIVED funding (the convention of the carry
reader; unverified against a live account record).  Read-only: no order is sent.
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger("directional_funding")

#: A settlement row can be delivered late; an exit younger than this is waited on.
SETTLE_GRACE_MS = 15 * 60 * 1000
#: Linear funding settles at least every 8 hours.
MAX_FUNDING_INTERVAL_MS = 8 * 3600 * 1000


def reconcile_directional_funding(
        store: Any, broker: Any, *, now_ms: int,
        carry_symbols: Iterable[str] = (), limit: int = 50) -> Dict[str, Any]:
    """Resolve the funding of closed linear trades.  Never raises, never orders."""
    out: Dict[str, Any] = {"booked": 0, "no_settlement": 0, "left": 0,
                           "statuses": {}}
    shared = {str(s) for s in carry_symbols}
    cache: Dict[Tuple[str, int, int], Any] = {}

    def left(trade: Dict[str, Any], status: str, **detail: Any) -> None:
        out["left"] += 1
        out["statuses"][status] = out["statuses"].get(status, 0) + 1
        try:
            store.set_trade_funding_status(int(trade["id"]), status, detail)
        except Exception:  # noqa: BLE001
            logger.exception("funding status could not be written")

    try:
        pending = store.trades_awaiting_funding(limit)
    except Exception:  # noqa: BLE001
        logger.exception("trades awaiting funding unreadable")
        return out
    for trade in pending:
        try:
            _resolve(store, broker, trade, now_ms, shared, cache, out, left)
        except Exception as exc:  # noqa: BLE001
            logger.exception("funding of trade %s not resolved", trade.get("id"))
            left(trade, "EVIDENCE_UNREAD", error=str(exc)[:200])
    return out


def _window(store: Any, trade: Dict[str, Any]) -> Tuple[int, int]:
    """(start_ms, end_ms] this exit row was responsible for: from the previous
    exit of the same position (or its open) to its own close."""
    start = float(trade["opened_epoch"])
    for sib in store.lifecycle_trades(trade["symbol"], trade["opened_epoch"]):
        if int(sib["id"]) == int(trade["id"]):
            break
        start = max(start, float(sib["closed_epoch"]))
    return int(start * 1000), int(float(trade["closed_epoch"]) * 1000)


def _resolve(store: Any, broker: Any, trade: Dict[str, Any], now_ms: int,
             shared: set, cache: Dict, out: Dict[str, Any],
             left: Callable[..., None]) -> None:
    symbol = str(trade["symbol"])
    start_ms, end_ms = _window(store, trade)
    if now_ms < end_ms + SETTLE_GRACE_MS:
        return left(trade, "PENDING_SETTLEMENT_GRACE")
    if symbol in shared:
        return left(trade, "AMBIGUOUS_SHARED_WITH_CARRY")
    key = (symbol, start_ms, end_ms)
    if key not in cache:
        try:
            cache[key] = broker.get_funding_evidence(symbol, start_ms, end_ms)
        except Exception as exc:  # noqa: BLE001
            cache[key] = exc
    ev = cache[key]
    if isinstance(ev, Exception):
        return left(trade, "EVIDENCE_UNREAD", error=str(ev)[:200])
    if not ev.complete:
        return left(trade, "EVIDENCE_INCOMPLETE", errors=list(ev.errors[:2]))
    already = store.booked_funding_ids(symbol)
    mine: List[Dict[str, Any]] = []
    seen: Dict[str, float] = {}
    for p in ev.payments:
        ms = int(p["ms"])
        if not (start_ms < ms <= end_ms):
            continue
        pid = str(p["id"])
        if pid in seen and seen[pid] != float(p["usd"]):
            return left(trade, "DUPLICATE_ID_CONFLICT", id=pid)
        if pid in seen or pid in already:
            continue
        seen[pid] = float(p["usd"])
        mine.append(p)
    evidence = {"window_ms": [start_ms, end_ms], "rows": len(mine),
                "sign": "positive=received"}
    if not mine:
        if end_ms - start_ms >= MAX_FUNDING_INTERVAL_MS:
            return left(trade, "SUSPECT_NO_SETTLEMENT_OVER_INTERVAL",
                        held_ms=end_ms - start_ms)
        if store.book_trade_funding(int(trade["id"]), funding_usd=0.0,
                                    payment_ids=[],
                                    status="NO_SETTLEMENT_IN_HOLD_WINDOW",
                                    evidence=evidence):
            out["no_settlement"] += 1
        return
    total = sum(float(p["usd"]) for p in mine)
    if store.book_trade_funding(int(trade["id"]), funding_usd=total,
                                payment_ids=[str(p["id"]) for p in mine],
                                status="BOOKED_FROM_SETTLEMENTS",
                                evidence=evidence):
        out["booked"] += 1
