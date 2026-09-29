"""Bybit private-WebSocket consumer: connect -> authenticate -> subscribe ->
receive -> parse -> normalize -> correlate -> deduplicate -> apply.

WHY THIS EXISTS
================
`ws_transport.py` opens the socket. `bybit_connection.BybitClient` already
builds the auth frame (`ws_auth_message`) and knows the private URL
(`ws_private_url`), but nothing drives them together into a receive loop —
this closes that gap.

REST REMAINS THE RECONCILIATION/RECOVERY AUTHORITY
=====================================================
This consumer never overwrites position economics (entry price, stop
price) or repairs a naked position — `trading_engine.TradingEngine.
check_naked_positions()`/`.reconcile()` already do that correctly, from
REST, and are already tested (tests/test_naked_position_handling.py).
Duplicating that logic here would be a second writer of the same fact.

What this consumer DOES write, through the SAME `persistence.StateStore`
methods REST already uses (so it is a second OBSERVER of one authority,
not a second authority):
  - `order`/`execution` topic events -> `store.update_order_status()`,
    exactly like `BybitClient.reconcile_on_startup()` does — an order's
    lifecycle status is safe to fast-forward from either source, because
    the update is idempotent and keyed by the durable `order_link_id`.
  - `position` topic events -> NEVER written directly. A `position`
    message only sets `needs_reconciliation` and records which symbol
    changed (`dirty_symbols`), so the next REST-driven cycle
    (`observe_exits`/`check_naked_positions`) knows to look there sooner.
    This is "fast observation", not "fast authority" — the distinction
    the WS/REST split exists to preserve.

CORRELATION AND DEDUPLICATION
================================
Every event's identity is the venue's own durable id (`execId` for a fill,
`orderId` for an order-state change), never a timestamp/symbol/side
tuple. `_Deduplicator` is a bounded LRU of event ids: replaying the exact
same message twice (e.g. after a reconnect resubscribes and the venue
resends recent events) is a no-op the second time, but two DIFFERENT
state transitions of the same order (New -> PartiallyFilled -> Filled)
are each their own id (order id + the venue's own `updatedTime`) and are
each applied.
"""
from __future__ import annotations

import collections
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Set

logger = logging.getLogger("private_ws_consumer")

RECV_TIMEOUT_SECONDS = 25.0  # Bybit pings/expects traffic ~every 20s
PING_INTERVAL_SECONDS = 20.0
RECONNECT_BACKOFF_SECONDS = (1.0, 2.0, 5.0, 10.0, 20.0)
DEDUP_MAX_SIZE = 10_000

#: Order-lifecycle statuses this consumer will fast-forward the ledger to.
#: Anything else (e.g. Untriggered/Triggered for conditional orders) is
#: still recorded as "unknown-ish" via the raw event, never silently
#: dropped, but is not mapped to a `persistence` status string.
_ORDER_STATUS_MAP = {
    "New": "submitted",
    "PartiallyFilled": "partial",
    "Filled": "filled",
    "Cancelled": "cancelled",
    "Rejected": "rejected",
    "PartiallyFilledCanceled": "cancelled",
    "Deactivated": "cancelled",
}


@dataclass(frozen=True)
class NormalizedEvent:
    """One canonical event out of a raw Bybit WS message. `kind` is one of
    "order", "execution", "position", "control", "unknown"."""

    event_id: str
    kind: str
    topic: str
    order_link_id: str = ""
    venue_order_id: str = ""
    symbol: str = ""
    status: str = ""
    raw: Dict[str, Any] = field(default_factory=dict)


class _Deduplicator:
    """Bounded LRU of event ids. `seen()` both checks and records, so a
    caller cannot check-then-forget-to-record."""

    def __init__(self, max_size: int = DEDUP_MAX_SIZE) -> None:
        self._max_size = max_size
        self._ids: "collections.OrderedDict[str, None]" = collections.OrderedDict()

    def seen(self, event_id: str) -> bool:
        if event_id in self._ids:
            self._ids.move_to_end(event_id)
            return True
        self._ids[event_id] = None
        if len(self._ids) > self._max_size:
            self._ids.popitem(last=False)
        return False


def _normalize_order_item(item: Dict[str, Any]) -> NormalizedEvent:
    order_id = str(item.get("orderId", ""))
    order_link_id = str(item.get("orderLinkId", ""))
    updated = str(item.get("updatedTime", ""))
    status_raw = str(item.get("orderStatus", ""))
    return NormalizedEvent(
        event_id=f"order:{order_id}:{status_raw}:{updated}",
        kind="order", topic="order",
        order_link_id=order_link_id, venue_order_id=order_id,
        symbol=str(item.get("symbol", "")),
        status=_ORDER_STATUS_MAP.get(status_raw, ""),
        raw=item,
    )


def _normalize_execution_item(item: Dict[str, Any]) -> NormalizedEvent:
    exec_id = str(item.get("execId", ""))
    order_id = str(item.get("orderId", ""))
    order_link_id = str(item.get("orderLinkId", ""))
    exec_type = str(item.get("execType", ""))
    is_maker_or_taker_fill = exec_type in ("Trade", "")
    status = "filled" if is_maker_or_taker_fill else ""
    return NormalizedEvent(
        event_id=f"execution:{exec_id}",
        kind="execution", topic="execution",
        order_link_id=order_link_id, venue_order_id=order_id,
        symbol=str(item.get("symbol", "")),
        status=status,
        raw=item,
    )


def _normalize_position_item(item: Dict[str, Any]) -> NormalizedEvent:
    symbol = str(item.get("symbol", ""))
    updated = str(item.get("updatedTime", item.get("seq", "")))
    return NormalizedEvent(
        event_id=f"position:{symbol}:{updated}",
        kind="position", topic="position",
        symbol=symbol, raw=item,
    )


_NORMALIZERS: Dict[str, Callable[[Dict[str, Any]], NormalizedEvent]] = {
    "order": _normalize_order_item,
    "execution": _normalize_execution_item,
    "position": _normalize_position_item,
}


def parse_ws_message(raw_text: str) -> List[NormalizedEvent]:
    """Every data (`topic`-bearing) message in `raw_text` -> zero or more
    `NormalizedEvent`. A control message (auth/subscribe ack, pong) is not
    a data message and is not this function's job — the consumer checks
    for `op`/`success` before calling this. An unrecognized topic produces
    a single `kind="unknown"` event rather than being silently dropped or
    raising — Section 27's adversarial law applies to the wire format
    itself: an unexpected shape is refused-as-unknown, not guessed at.
    """
    try:
        message = json.loads(raw_text)
    except (ValueError, TypeError) as exc:
        return [NormalizedEvent(
            event_id=f"unparseable:{hash(raw_text)}", kind="unknown",
            topic="", raw={"_parse_error": str(exc), "_raw_text": raw_text})]

    topic = str(message.get("topic", ""))
    base_topic = topic.split(".", 1)[0]
    normalizer = _NORMALIZERS.get(base_topic)
    data = message.get("data")
    if not topic or normalizer is None or not isinstance(data, list):
        return [NormalizedEvent(
            event_id=f"unknown:{hash(raw_text)}", kind="unknown",
            topic=topic, raw=message)]
    return [normalizer(item) for item in data if isinstance(item, dict)]


class WSPrivateConsumer:
    """Owns one connection's lifecycle: connect, auth, subscribe, receive
    loop, reconnect with backoff. `transport_factory()` is called fresh on
    every (re)connect so a dead socket is never reused.
    """

    TOPICS = ("order", "execution", "position")

    def __init__(self, *, client: Any, transport_factory: Callable[[], Any],
                sleep: Callable[[float], None] = time.sleep) -> None:
        self._client = client
        self._transport_factory = transport_factory
        self._sleep = sleep
        self._dedup = _Deduplicator()
        self.needs_reconciliation = False
        self.dirty_symbols: Set[str] = set()
        self.unknown_message_count = 0
        self.applied_event_count = 0
        self.duplicate_event_count = 0

    # -- the part that is fully unit-testable without a socket ----------

    def handle_raw_message(self, raw_text: str) -> List[NormalizedEvent]:
        """Parse, dedupe, and apply one raw text frame's message(s).
        Returns the events that were newly applied (dedup drops omitted)."""
        message = None
        try:
            message = json.loads(raw_text)
        except (ValueError, TypeError):
            pass
        if isinstance(message, dict) and "op" in message:
            self._handle_control_message(message)
            return []

        events = parse_ws_message(raw_text)
        applied: List[NormalizedEvent] = []
        for event in events:
            if event.kind == "unknown":
                self.unknown_message_count += 1
                logger.warning("unrecognized private-WS message: %r", event.raw)
                continue
            if self._dedup.seen(event.event_id):
                self.duplicate_event_count += 1
                continue
            self._apply(event)
            self.applied_event_count += 1
            applied.append(event)
        return applied

    def _handle_control_message(self, message: Dict[str, Any]) -> None:
        op = message.get("op")
        success = message.get("success")
        if op in ("auth", "subscribe") and success is False:
            logger.error("private-WS %s failed: %s", op, message)
            self.needs_reconciliation = True
        elif op == "pong":
            pass
        else:
            logger.debug("private-WS control message: %s", message)

    def _apply(self, event: NormalizedEvent) -> None:
        store = self._client.store
        if event.kind in ("order", "execution"):
            if event.order_link_id and event.status:
                try:
                    store.update_order_status(
                        event.order_link_id, event.status,
                        exchange_id=event.venue_order_id)
                except Exception:  # noqa: BLE001
                    logger.exception(
                        "could not apply %s event for %s to the store",
                        event.kind, event.order_link_id)
                    self.needs_reconciliation = True
        elif event.kind == "position":
            # Never written directly here — see module docstring. REST
            # remains the authority for position economics/protection.
            self.needs_reconciliation = True
            if event.symbol:
                self.dirty_symbols.add(event.symbol)

    # -- the part that needs a real (or fake) transport ------------------

    def run_once(self, *, max_messages: Optional[int] = None) -> None:
        """Connect, auth, subscribe, and receive until the transport ends
        the connection (raises) or `max_messages` is reached (test-only
        bound — production calls `run_forever`, which never passes this).
        """
        transport = self._transport_factory()
        transport.connect()
        try:
            self._authenticate(transport)
            self._subscribe(transport)
            count = 0
            last_ping = time.time()
            while max_messages is None or count < max_messages:
                if time.time() - last_ping > PING_INTERVAL_SECONDS:
                    transport.send_text(json.dumps(self._client.ws_ping_message()))
                    last_ping = time.time()
                opcode, payload = transport.recv(timeout=RECV_TIMEOUT_SECONDS)
                if opcode == 0x1:  # text
                    self.handle_raw_message(payload.decode("utf-8"))
                    count += 1
                elif opcode == 0x9:  # ping
                    transport.send_pong(payload)
        finally:
            transport.close()

    def _authenticate(self, transport: Any) -> None:
        auth_message = self._client.ws_auth_message()
        transport.send_text(json.dumps(auth_message))
        opcode, payload = transport.recv(timeout=RECV_TIMEOUT_SECONDS)
        if opcode == 0x1:
            self._handle_control_message(json.loads(payload.decode("utf-8")))

    def _subscribe(self, transport: Any) -> None:
        transport.send_text(json.dumps({"op": "subscribe", "args": list(self.TOPICS)}))
        opcode, payload = transport.recv(timeout=RECV_TIMEOUT_SECONDS)
        if opcode == 0x1:
            self._handle_control_message(json.loads(payload.decode("utf-8")))

    def run_forever(self, stop_event: Any) -> None:
        """Reconnect with backoff until `stop_event.is_set()`. Every
        (re)connect implies a possible gap in observation — REST
        reconciliation is the authority that closes it, so a reconnect
        always sets `needs_reconciliation`."""
        attempt = 0
        while not stop_event.is_set():
            try:
                self.run_once()
            except Exception as exc:  # noqa: BLE001
                logger.error("private-WS connection ended: %s: %s",
                            type(exc).__name__, exc)
            self.needs_reconciliation = True
            if stop_event.is_set():
                return
            backoff = RECONNECT_BACKOFF_SECONDS[
                min(attempt, len(RECONNECT_BACKOFF_SECONDS) - 1)]
            self._sleep(backoff)
            attempt += 1
