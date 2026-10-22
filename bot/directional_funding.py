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
                                 (also against an amount booked on another trade)
``AMBIGUOUS_OVERLAPPING_POSITION``  another position of the symbol was alive at
                                 the same time: whose row it is cannot be told
``BOUNDARY_AMBIGUOUS``           a settlement within a minute of the window's
                                 start or end (bot clock vs venue clock)
``SETTLEMENT_SIZE_MISMATCH`` / ``SETTLEMENT_SIZE_UNVERIFIED``  the row's
                                 position size is not what this trade held
``FUNDING_SIGN_MISMATCH``        a row's ``funding`` sign is the opposite of its
                                 wallet ``change`` (who paid whom is unknown)
``FUNDING_SIGN_UNVERIFIED``      a row has no usable ``change``, or it agrees
                                 with neither ``funding`` nor its negation

Sign: a payment is booked only when the row's ``funding`` is REPRODUCED by its
own wallet ``change`` (``change = cashFlow + funding - fee``; see
``carry_broker.funding_sign_check``).  Positive then means the account received
money.  A row that cannot be cross-checked is never booked.  Read-only: no order
is sent.
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

import ledger as L

logger = logging.getLogger("directional_funding")

#: A settlement row can be delivered late. The carry ledger will not judge a
#: closed lifecycle until this long after its end (`ledger.MISSING_GRACE_MS`);
#: nothing here is final sooner either, so a late row cannot be lost to an
#: earlier "no settlement".
SETTLE_GRACE_MS = L.MISSING_GRACE_MS
#: The SHORTEST linear funding interval (some symbols settle hourly). A hold
#: shorter than this can legitimately cross no settlement; a longer one that
#: shows none is suspect, whatever the symbol's usual interval.
MAX_FUNDING_INTERVAL_MS = 3600 * 1000
#: A settlement this close to the start or end of a window is not given to
#: either side: the bot's stamps are its own clock, the row is the venue's.
BOUNDARY_MS = L.ATTRIBUTION_SLACK_MS
#: A settlement row's position size must equal the quantity this trade held.
SIZE_REL_TOL = 0.01


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
    # A position of the same symbol alive at the same time (the bot recorded an
    # exit later than it happened, or the book was adopted): the account has
    # ONE settlement row per instant and no way to say whose it is.
    lifecycle = store.lifecycle_trades(symbol, trade["opened_epoch"])
    last_close = max(float(t["closed_epoch"]) for t in lifecycle)
    if store.overlapping_lifecycles(symbol, trade["opened_epoch"], last_close):
        return left(trade, "AMBIGUOUS_OVERLAPPING_POSITION")
    already = store.booked_funding_ids(symbol)
    mine: List[Dict[str, Any]] = []
    seen: Dict[str, float] = {}
    for p in ev.payments:
        ms = int(p["ms"])
        if not (start_ms < ms <= end_ms):
            continue
        pid = str(p["id"])
        usd = float(p["usd"])
        if pid in seen and seen[pid] != usd:
            return left(trade, "DUPLICATE_ID_CONFLICT", id=pid)
        if pid in already and already[pid] is not None and already[pid] != usd:
            return left(trade, "DUPLICATE_ID_CONFLICT", id=pid,
                        booked_elsewhere=already[pid], now=usd)
        if pid in seen or pid in already:
            continue
        if (ms - start_ms <= BOUNDARY_MS and start_ms == int(
                float(lifecycle[0]["opened_epoch"]) * 1000)) or (
                end_ms - ms <= BOUNDARY_MS):
            return left(trade, "BOUNDARY_AMBIGUOUS", id=pid)
        held = sum(float(t["qty"]) for t in lifecycle
                   if int(float(t["closed_epoch"]) * 1000) >= ms)
        size = p.get("size")
        if size is None:
            return left(trade, "SETTLEMENT_SIZE_UNVERIFIED", id=pid)
        if abs(abs(float(size)) - held) > max(1e-9, SIZE_REL_TOL * held):
            return left(trade, "SETTLEMENT_SIZE_MISMATCH", id=pid,
                        venue_size=float(size), held=held)
        seen[pid] = usd
        mine.append(p)
    signs = {str(p.get("sign") or "unchecked") for p in mine}
    if "mismatch" in signs:
        return left(trade, "FUNDING_SIGN_MISMATCH", ids=[p["id"] for p in mine
                                                         if p.get("sign") == "mismatch"][:5])
    if signs - {"confirmed"}:
        return left(trade, "FUNDING_SIGN_UNVERIFIED",
                    ids=[p["id"] for p in mine if p.get("sign") != "confirmed"][:5])
    evidence = {"window_ms": [start_ms, end_ms], "rows": len(mine),
                "sign": "confirmed against the wallet change (positive=received)"}
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
                                payments=mine,
                                status="BOOKED_FROM_SETTLEMENTS",
                                evidence=evidence):
        out["booked"] += 1
