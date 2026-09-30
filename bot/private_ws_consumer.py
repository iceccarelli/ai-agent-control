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

THIS CONSUMER NEVER WRITES `StateStore` ITSELF
==================================================
`persistence.StateStore` enforces one writer thread (`claim_writer()`/
`_assert_writer()` — see persistence.py's own docstring on why: a lock
alone still lets two threads interleave a read-modify-write). This
consumer runs on its own background thread, which is NOT the writer
thread `main.TradingBot.startup()` claims. An earlier version of this
module called `store.update_order_status()` directly from `_apply()` and
it raised `PersistenceError` in exactly the orchestrator-level test this
boundary exists to catch — confirmed while integrating this into
`TradingBot`, not assumed.

So: `order`/`execution` topic events are normalized, deduplicated, and
placed on `self._pending` (a `queue.Queue`, thread-safe by construction).
`drain_and_apply(store)` — called ONLY from the writer thread, i.e. from
`TradingBot.tick()` — pops everything pending and calls
`store.update_order_status(..., source="ws")` there, the same method
`BybitClient.reconcile_on_startup()` calls (with `source="rest"`, its
default) — a second OBSERVER of it rather than a second writer, but NOT
given the same unconditional authority: `source="ws"` puts the write
through `StateStore`'s monotonic-status guard, so a stale/out-of-order WS
event can never regress an order already recorded as terminal (filled/
cancelled/rejected), while REST reconciliation is unaffected and remains
fully authoritative. See `persistence.update_order_status`'s docstring.
`position` topic events still never touch the store at all, from any
thread: a `position` message only sets `needs_reconciliation` and
records which symbol changed (`dirty_symbols`), so the next REST-driven
cycle (`observe_exits`/`check_naked_positions`) knows to look there
sooner. This is "fast observation", not "fast authority" — the
distinction the WS/REST split exists to preserve.

CORRELATION AND DEDUPLICATION
================================
Every event's identity is the venue's own durable id (`execId` for a fill,
`orderId` for an order-state change), never a timestamp/symbol/side
tuple, and NEVER Python's built-in `hash()` (see `deterministic_id` below
— `hash()` on a string is salted per-process by default and is not
reproducible across processes, which durable identity must be).
`_Deduplicator` is a bounded LRU of event ids: replaying the exact same
message twice (e.g. after a reconnect resubscribes and the venue resends
recent events) is a no-op the second time, but two DIFFERENT state
transitions of the same order (New -> PartiallyFilled -> Filled), or two
messages for the same order/status/timestamp that differ in their other
fields, are each their own id and are each applied.

WS EVENT EVIDENCE
====================
Every observed message — order, execution, position, control (auth ack,
subscribe ack, pong), connection (connect, disconnect), and unknown/
unparseable — produces one record via `tools/venue_evidence.py`'s
existing mechanism (`build_ws_event_record`/`append_evidence`): the same
redaction, environment validation, hash chaining, and git provenance
already proven for REST evidence, tagged `transport="ws"` so the two
sources are distinguishable in a chain reader without guessing.

EVIDENCE OWNERSHIP: THE WS THREAD NEVER WRITES DURABLE EVIDENCE EITHER
=========================================================================
Same law as "THIS CONSUMER NEVER WRITES StateStore ITSELF" above, applied
to the evidence file: the WS (background) thread only OBSERVES an event
and enqueues a `_PendingEvidence` onto `self._evidence_pending` (a
`queue.Queue`, fast, no I/O). `drain_evidence()` — called ONLY from the
writer thread, via `drain_and_apply()` from `TradingBot.tick()` — pops
everything pending and performs the actual file write
(`_write_evidence_record`). This means the same "one writer thread"
invariant now covers both `StateStore` and the WS evidence log, and it
lets the schema keep "observed" (`observed_at_utc`, stamped when the WS
thread enqueues) distinct from "applied" (`captured_at_utc`, stamped when
the writer thread durably writes). See `drain_evidence`'s docstring for
exactly where the write runs, and "EVIDENCE FAILURE SEMANTICS" further
down for NORMAL vs ASSURANCE mode behaviour on a capture failure.
"""
from __future__ import annotations

import collections
import datetime as dt
import hashlib
import json
import logging
import os
import queue
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Set

HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS = os.path.join(HERE, "tools")
if TOOLS not in sys.path:
    sys.path.insert(0, TOOLS)

logger = logging.getLogger("private_ws_consumer")

#: Default WS evidence log — same directory/convention as
#: tools/testnet_conformance_run.py's DEFAULT_EVIDENCE_PATH, kept as a
#: SEPARATE file (not the same one REST's EvidenceCapturingTransport
#: writes to) so the WS thread and the writer thread never append to the
#: same evidence file concurrently — each file has exactly one writer by
#: construction, which is what makes neither need a lock.
DEFAULT_WS_EVIDENCE_PATH = os.path.join(HERE, "artifacts", "ws_evidence.jsonl")


def deterministic_id(*parts: str) -> str:
    """SHA-256 over a canonical join of `parts` — NEVER Python's built-in
    `hash()`, which is salted per-process (`PYTHONHASHSEED`) for strings
    by default and is not stable across processes or, without disabling
    hash randomization, even across runs of the same process. A durable
    event identity that a second process (a replay, an audit, a re-run of
    this exact WS session tomorrow) cannot reproduce is not a durable
    identity — see this module's evidence-identity tests, which construct
    a *second* process and confirm the same input reproduces the same id.
    """
    canonical = "\x1f".join(parts)  # unit separator: not valid in any part
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

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


@dataclass(frozen=True)
class _PendingEvidence:
    """One evidence record OBSERVED by the WS thread, not yet durably
    written. `observed_at_utc` is stamped here, at observation time, and
    is distinct from `captured_at_utc` on the eventual `EvidenceRecord`,
    which is stamped by `_write_evidence_record` at APPLY time on the
    writer thread — see FIX 1, "preserve the distinction between observed
    and applied"."""

    venue_event_id: str
    topic: str
    order_link_id: str
    payload: Dict[str, Any]
    duplicate: bool
    observed_at_utc: str


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


def _canonical_json(item: Dict[str, Any]) -> str:
    """Stable string form of `item` for hashing into a deterministic id —
    `sort_keys=True` so key order (which the venue does not promise) never
    changes the identity of otherwise-identical content."""
    return json.dumps(item, sort_keys=True, default=str)


def _normalize_order_item(item: Dict[str, Any]) -> NormalizedEvent:
    order_id = str(item.get("orderId", ""))
    order_link_id = str(item.get("orderLinkId", ""))
    updated = str(item.get("updatedTime", ""))
    status_raw = str(item.get("orderStatus", ""))
    if not order_id:
        # No usable venue order identity (`orderId` empty/missing): this
        # cannot become a legitimate durable "order" event by inventing an
        # identity out of empty fields — treat it as an invalid/unknown
        # observation instead (never applied, never a StateStore write),
        # exactly like an unrecognized topic. See FIX 4.
        identity = deterministic_id("order-invalid", _canonical_json(item))
        return NormalizedEvent(
            event_id=f"unknown:{identity}", kind="unknown", topic="order",
            raw=item)
    # order_id + status_raw + updated + a hash of the FULL payload: two
    # order-state messages for the same order, at the same status, at the
    # same venue timestamp, but with different content (e.g. a filled
    # quantity that changed) are still two distinct events, not a
    # duplicate delivery of one — see this module's adversarial identity
    # tests ("same timestamp with different payload").
    identity = deterministic_id(
        "order", order_id, status_raw, updated, _canonical_json(item))
    return NormalizedEvent(
        event_id=f"order:{identity}",
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
    if not exec_id:
        # No usable venue execution identity (`execId` empty/missing) —
        # same treatment as an order with no usable `orderId`: invalid/
        # unknown observation, never a legitimate "execution" event. See
        # FIX 4.
        identity = deterministic_id("execution-invalid", _canonical_json(item))
        return NormalizedEvent(
            event_id=f"unknown:{identity}", kind="unknown", topic="execution",
            raw=item)
    is_maker_or_taker_fill = exec_type in ("Trade", "")
    status = "filled" if is_maker_or_taker_fill else ""
    # execId is already the venue's own durable, unique-per-fill identity
    # — no timestamp or payload hash needed on top of it (the venue never
    # reissues an execId for two different fills).
    identity = deterministic_id("execution", exec_id)
    return NormalizedEvent(
        event_id=f"execution:{identity}",
        kind="execution", topic="execution",
        order_link_id=order_link_id, venue_order_id=order_id,
        symbol=str(item.get("symbol", "")),
        status=status,
        raw=item,
    )


def _normalize_position_item(item: Dict[str, Any]) -> NormalizedEvent:
    symbol = str(item.get("symbol", ""))
    updated = str(item.get("updatedTime", item.get("seq", "")))
    identity = deterministic_id(
        "position", symbol, updated, _canonical_json(item))
    return NormalizedEvent(
        event_id=f"position:{identity}",
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
            event_id=f"unparseable:{deterministic_id('unparseable', raw_text)}",
            kind="unknown", topic="",
            raw={"_parse_error": str(exc), "_raw_text": raw_text})]

    topic = str(message.get("topic", ""))
    base_topic = topic.split(".", 1)[0]
    normalizer = _NORMALIZERS.get(base_topic)
    data = message.get("data")
    if not topic or normalizer is None or not isinstance(data, list):
        return [NormalizedEvent(
            event_id=f"unknown:{deterministic_id('unknown', raw_text)}",
            kind="unknown", topic=topic, raw=message)]
    return [normalizer(item) for item in data if isinstance(item, dict)]


class WSPrivateConsumer:
    """Owns one connection's lifecycle: connect, auth, subscribe, receive
    loop, reconnect with backoff. `transport_factory()` is called fresh on
    every (re)connect so a dead socket is never reused.
    """

    TOPICS = ("order", "execution", "position")

    def __init__(self, *, client: Any, transport_factory: Callable[[], Any],
                sleep: Callable[[float], None] = time.sleep,
                evidence_path: Optional[str] = None,
                assurance_mode: bool = False) -> None:
        self._client = client
        self._transport_factory = transport_factory
        self._sleep = sleep
        self._dedup = _Deduplicator()
        self.needs_reconciliation = False
        self.dirty_symbols: Set[str] = set()
        self.unknown_message_count = 0
        self.applied_event_count = 0
        self.duplicate_event_count = 0
        #: order/execution events queued for the writer thread — see
        #: module docstring, "THIS CONSUMER NEVER WRITES StateStore
        #: ITSELF". `queue.Queue` is thread-safe by construction, which is
        #: what makes handing events from this (background) thread to the
        #: writer thread safe without this module taking a lock itself.
        self._pending: "queue.Queue[NormalizedEvent]" = queue.Queue()
        #: WS evidence — OBSERVED (enqueued) from THIS (the WS) thread,
        #: APPLIED (the actual durable file write) only from the writer
        #: thread via `drain_evidence()`/`drain_and_apply()`. See "WS
        #: EVENT EVIDENCE" below; `None` disables capture entirely.
        self._evidence_path = evidence_path
        self._evidence_pending: "queue.Queue[_PendingEvidence]" = queue.Queue()
        #: NORMAL (False, default): a capture failure degrades
        #: (`needs_reconciliation=True`, logged, counted) and trading
        #: continues per the existing safety policy. ASSURANCE/CONFORMANCE
        #: (True): a capture failure is recorded in
        #: `evidence_capture_failed` for the CALLER (the conformance
        #: runner) to fail the run closed on — see module docstring,
        #: "EVIDENCE FAILURE SEMANTICS".
        self.assurance_mode = assurance_mode
        self.evidence_capture_failed = False
        self.evidence_records_written = 0
        self._connection_seq = 0
        #: Explicit private-stream lifecycle proof (FIX 5): a connected
        #: transport alone is not sufficient — `auth_ok`/`subscribe_ok`
        #: are only ever set True by `_handle_control_message` on an
        #: EXPLICIT `success: true` response to that op, and are reset to
        #: False at the start of every connection attempt (`run_once`), so
        #: a stale success from an earlier connection can never be
        #: mistaken for this one's.
        self.auth_ok = False
        self.subscribe_ok = False

    @property
    def evidence_path(self) -> Optional[str]:
        """Read-only: lets a caller (e.g. the conformance runner, to poll
        for observable evidence) find the WS evidence log without
        reaching into a private attribute."""
        return self._evidence_path

    # -- the part that is fully unit-testable without a socket ----------

    def handle_raw_message(self, raw_text: str) -> List[NormalizedEvent]:
        """Parse, dedupe, capture evidence, and apply one raw text frame's
        message(s). Returns the events that were newly applied (dedup
        drops omitted).

        Every observed event — including a duplicate delivery and an
        unknown/unparseable one — gets an evidence record (see
        `_capture_evidence`): evidence reflects what was literally seen
        on the wire, tagged with whether it was applied or deduped, which
        is a stronger record than only evidencing what canonical state
        changed.
        """
        message = None
        try:
            message = json.loads(raw_text)
        except (ValueError, TypeError):
            pass
        if isinstance(message, dict) and "op" in message:
            self._handle_control_message(message)
            return []

        # Apply BEFORE capturing evidence for each event: `_apply` sets
        # `needs_reconciliation`/queues an update, and `_capture_evidence`
        # (since FIX 1) only enqueues a `_PendingEvidence` — both are fast,
        # no I/O, on this thread. The actual evidence file write happens
        # later, on the writer thread (`drain_evidence()`), so this
        # ordering no longer needs to race an fsync; it is kept anyway so
        # `needs_reconciliation` is set as early as possible regardless of
        # queueing order — the same ordering discipline as
        # `run_forever`'s connection-evidence capture.
        events = parse_ws_message(raw_text)
        applied: List[NormalizedEvent] = []
        for event in events:
            if event.kind == "unknown":
                self.unknown_message_count += 1
                logger.warning("unrecognized private-WS message: %r", event.raw)
                self._capture_evidence(event, duplicate=False)
                continue
            duplicate = self._dedup.seen(event.event_id)
            if duplicate:
                self.duplicate_event_count += 1
                self._capture_evidence(event, duplicate=duplicate)
                continue
            self._apply(event)
            self.applied_event_count += 1
            applied.append(event)
            self._capture_evidence(event, duplicate=duplicate)
        return applied

    def _handle_control_message(self, message: Dict[str, Any]) -> None:
        # Flag first, capture evidence after — same ordering fix and same
        # reason as handle_raw_message/run_forever above.
        op = str(message.get("op") or "")
        success = message.get("success")
        # FIX 5: AUTH_OK/SUBSCRIBE_OK are only ever set True by an EXPLICIT
        # `success: true` on the matching op — anything else (an explicit
        # failure, a malformed response, or the key simply missing) leaves
        # the flag False and fails closed. A connected socket alone must
        # never be read as authenticated or subscribed.
        if op == "auth":
            if success is True:
                self.auth_ok = True
            else:
                self.auth_ok = False
                self.needs_reconciliation = True
                logger.error("private-WS auth failed: %s", message)
        elif op == "subscribe":
            if success is True:
                self.subscribe_ok = True
            else:
                self.subscribe_ok = False
                self.needs_reconciliation = True
                logger.error("private-WS subscribe failed: %s", message)
        elif op == "pong":
            pass
        else:
            logger.debug("private-WS control message: %s", message)
        self._capture_control_evidence(op=op, payload=message)

    def _apply(self, event: NormalizedEvent) -> None:
        """Runs on the WS (background) thread — MUST NOT touch
        `StateStore` (see module docstring). Order/execution events are
        queued for `drain_and_apply()`; position events only ever set
        flags, on any thread."""
        if event.kind in ("order", "execution"):
            if event.order_link_id and event.status:
                self._pending.put(event)
        elif event.kind == "position":
            # Never written, from any thread — see module docstring. REST
            # remains the authority for position economics/protection.
            self.needs_reconciliation = True
            if event.symbol:
                self.dirty_symbols.add(event.symbol)

    def drain_and_apply(self, store: Any) -> List[NormalizedEvent]:
        """Apply every currently-queued order/execution event to `store`,
        then durably write every currently-queued evidence record.

        MUST be called from the store's writer thread — that is the
        entire reason this method (and `drain_evidence()`) exists
        separately from `_apply()`/`_capture_evidence()` (see module
        docstring and FIX 1). `main.TradingBot.tick()` calls this once per
        cycle, before this cycle's REST calls, so a freshly-applied WS
        status is visible to them.
        """
        applied: List[NormalizedEvent] = []
        while True:
            try:
                event = self._pending.get_nowait()
            except queue.Empty:
                break
            try:
                # source="ws": this write is a replayed/observed network
                # event, not the venue's own REST answer, so it goes through
                # persistence.StateStore's monotonic-status guard -- a
                # stale/out-of-order event (possible on reconnect, since
                # `_Deduplicator` above is reset by a restart) cannot regress
                # an order already recorded as terminal. See
                # `update_order_status`'s docstring for the exact invariant.
                store.update_order_status(
                    event.order_link_id, event.status,
                    exchange_id=event.venue_order_id, source="ws")
                applied.append(event)
            except Exception:  # noqa: BLE001
                logger.exception(
                    "could not apply queued %s event for %s to the store",
                    event.kind, event.order_link_id)
                self.needs_reconciliation = True
        self.drain_evidence()
        return applied

    def drain_evidence(self) -> int:
        """Durably write every currently-queued evidence record.

        MUST be called from the store's writer thread (normally only via
        `drain_and_apply()` above) — the WS (background) thread only ever
        OBSERVES an event and enqueues it (`_capture_evidence` and
        friends, below); it never performs the file I/O itself. This is
        the same queue-then-drain ownership `persistence.StateStore`
        already enforces for order/execution updates, extended to durable
        evidence (FIX 1): the WS thread must never write `StateStore` OR
        durable evidence.

        Returns the number of records written, for tests/observability.
        """
        written = 0
        while True:
            try:
                pending = self._evidence_pending.get_nowait()
            except queue.Empty:
                break
            self._write_evidence_record(pending)
            written += 1
        return written

    # -- WS event evidence -------------------------------------------------
    #
    # OWNERSHIP (FIX 1): the WS (background) thread only ever OBSERVES —
    # `_capture_evidence`/`_capture_control_evidence`/
    # `_capture_connection_evidence` build a `_PendingEvidence` and
    # `queue.Queue.put()` it (fast, no I/O, thread-safe by construction).
    # The actual durable write — `_write_evidence_record`, which does the
    # file I/O — is only ever called from `drain_evidence()`, which MUST
    # run on the writer thread (see that method's docstring). This is the
    # exact ownership model `persistence.StateStore` already enforces for
    # order/execution updates (`self._pending`/`drain_and_apply`),
    # extended here to durable evidence: the WS thread must never write
    # `StateStore` OR durable evidence. It remains a SEPARATE file from
    # REST's (venue_evidence.EvidenceCapturingTransport's) evidence log —
    # two single-writer-thread files, never one file with two writers.
    #
    # EVIDENCE FAILURE SEMANTICS: `_write_evidence_record` never raises —
    # a capture bug must not crash the writer thread's tick or block a
    # real trading decision. In NORMAL mode (assurance_mode=False, the
    # default, used by main.TradingBot) a failure only logs, sets
    # `evidence_capture_failed`/`needs_reconciliation`, and the cycle
    # continues — trading proceeds under the existing safety policy, which
    # never depended on evidence capture succeeding. In ASSURANCE/
    # CONFORMANCE mode (assurance_mode=True, used by
    # tools/testnet_conformance_run.py) the same flag is set, but nothing
    # here decides pass/fail — the CALLER (the conformance runner) checks
    # `evidence_capture_failed` after the bounded WS phase and fails the
    # run closed if it is set. That keeps "what counts as success" a
    # decision the conformance runner owns explicitly, not a hidden policy
    # buried in this module.

    def _enqueue_evidence(self, *, venue_event_id: str, topic: str,
                          order_link_id: str, payload: Dict[str, Any],
                          duplicate: bool) -> None:
        if self._evidence_path is None:
            return
        observed_at_utc = dt.datetime.now(dt.timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ")
        self._evidence_pending.put(_PendingEvidence(
            venue_event_id=venue_event_id, topic=topic,
            order_link_id=order_link_id, payload=payload,
            duplicate=duplicate, observed_at_utc=observed_at_utc))

    def _write_evidence_record(self, pending: "_PendingEvidence") -> None:
        """Runs on the writer thread ONLY — see `drain_evidence()`."""
        try:
            import venue_evidence as ve

            environment = str(getattr(self._client, "venue", "unknown"))
            ws_url = str(getattr(self._client, "ws_private_url", ""))
            prev_hash = ve.last_record_hash(self._evidence_path)
            record = ve.build_ws_event_record(
                venue="bybit", environment=environment, ws_url=ws_url,
                venue_event_id=pending.venue_event_id, topic=pending.topic,
                order_link_id=pending.order_link_id, payload=pending.payload,
                duplicate=pending.duplicate, prev_hash=prev_hash,
                observed_at_utc=pending.observed_at_utc)
            ve.append_evidence(self._evidence_path, record)
            self.evidence_records_written += 1
        except Exception:  # noqa: BLE001
            logger.error(
                "WS evidence capture failed for venue_event_id=%s topic=%s "
                "(NORMAL mode: degrading, trading continues; ASSURANCE "
                "mode: the caller must fail this run closed)",
                pending.venue_event_id, pending.topic, exc_info=True)
            self.evidence_capture_failed = True
            self.needs_reconciliation = True

    def _capture_evidence(self, event: NormalizedEvent, *,
                          duplicate: bool) -> None:
        self._enqueue_evidence(
            venue_event_id=event.event_id, topic=event.topic or event.kind,
            order_link_id=event.order_link_id, payload=event.raw,
            duplicate=duplicate)

    def _capture_control_evidence(self, *, op: str,
                                  payload: Dict[str, Any]) -> None:
        event_id = deterministic_id("control", op, _canonical_json(payload))
        self._enqueue_evidence(
            venue_event_id=f"control:{event_id}", topic=f"control.{op}",
            order_link_id="", payload=payload, duplicate=False)

    def _capture_connection_evidence(self, *, connection_event: str,
                                     error: str = "") -> None:
        self._connection_seq += 1
        event_id = deterministic_id(
            "connection", connection_event, error, str(self._connection_seq))
        payload: Dict[str, Any] = {"event": connection_event}
        if error:
            payload["error"] = error
        self._enqueue_evidence(
            venue_event_id=f"connection:{event_id}",
            topic=f"connection.{connection_event}", order_link_id="",
            payload=payload, duplicate=False)

    # -- the part that needs a real (or fake) transport ------------------

    def run_once(self, *, max_messages: Optional[int] = None) -> None:
        """Connect, auth, subscribe, and receive until the transport ends
        the connection (raises) or `max_messages` is reached (test-only
        bound — production calls `run_forever`, which never passes this).

        FIX 5: `auth_ok`/`subscribe_ok` are reset False at the start of
        every connection attempt, then explicitly required after
        `_authenticate`/`_subscribe` — a connected transport alone is not
        sufficient. Either missing raises, which `run_forever` treats
        exactly like any other connection failure (reconnect-with-backoff,
        `needs_reconciliation=True`).
        """
        self.auth_ok = False
        self.subscribe_ok = False
        transport = self._transport_factory()
        transport.connect()
        self._capture_connection_evidence(connection_event="connected")
        try:
            self._authenticate(transport)
            if not self.auth_ok:
                raise RuntimeError(
                    "private-WS authentication did not succeed "
                    "(AUTH_OK not established)")
            self._subscribe(transport)
            if not self.subscribe_ok:
                raise RuntimeError(
                    "private-WS subscription did not succeed "
                    "(SUBSCRIBE_OK not established)")
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
        always sets `needs_reconciliation`.

        `needs_reconciliation = True` is set IMMEDIATELY on a connection
        ending, before anything else (including evidence capture, which
        since FIX 1 only enqueues a `_PendingEvidence` on this thread — the
        actual file write happens later, on the writer thread, via
        `drain_evidence()`). The writer thread's `_absorb_ws_observations`
        reads this flag from a different thread, so setting it first keeps
        it timely regardless of queueing order. Evidence capture for the
        disconnect runs AFTER, and does not affect the flag's timeliness.
        """
        attempt = 0
        while not stop_event.is_set():
            connection_error = ""
            try:
                self.run_once()
            except Exception as exc:  # noqa: BLE001
                logger.error("private-WS connection ended: %s: %s",
                            type(exc).__name__, exc)
                connection_error = f"{type(exc).__name__}: {exc}"
            self.needs_reconciliation = True
            if connection_error:
                self._capture_connection_evidence(
                    connection_event="disconnected", error=connection_error)
            if stop_event.is_set():
                return
            backoff = RECONNECT_BACKOFF_SECONDS[
                min(attempt, len(RECONNECT_BACKOFF_SECONDS) - 1)]
            self._sleep(backoff)
            attempt += 1
