"""private_ws_consumer.py: normalize/correlate/deduplicate/apply, and the
connect->auth->subscribe->receive->reconnect loop, all against a fake
transport (no socket, no network — ws_transport.py's own protocol
correctness is proven separately in tests/test_ws_transport.py).
"""
from __future__ import annotations

import json
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

import private_ws_consumer as pwc  # noqa: E402


ORDER_MSG = json.dumps({
    "topic": "order",
    "data": [{
        "symbol": "BTCUSDT", "orderId": "V-1", "orderLinkId": "BB-entry-1",
        "orderStatus": "Filled", "updatedTime": "1000",
    }],
})

EXECUTION_MSG = json.dumps({
    "topic": "execution",
    "data": [{
        "symbol": "BTCUSDT", "orderId": "V-1", "orderLinkId": "BB-entry-1",
        "execId": "E-1", "execType": "Trade",
    }],
})

POSITION_MSG = json.dumps({
    "topic": "position",
    "data": [{"symbol": "BTCUSDT", "size": "0.01", "updatedTime": "2000"}],
})


class TestParseWsMessage:
    def test_order_message_normalizes(self):
        events = pwc.parse_ws_message(ORDER_MSG)
        assert len(events) == 1
        e = events[0]
        assert e.kind == "order"
        assert e.order_link_id == "BB-entry-1"
        assert e.venue_order_id == "V-1"
        assert e.status == "filled"

    def test_execution_message_normalizes(self):
        events = pwc.parse_ws_message(EXECUTION_MSG)
        assert len(events) == 1
        e = events[0]
        assert e.kind == "execution"
        assert e.event_id == "execution:E-1"
        assert e.status == "filled"

    def test_position_message_normalizes(self):
        events = pwc.parse_ws_message(POSITION_MSG)
        assert len(events) == 1
        assert events[0].kind == "position"
        assert events[0].symbol == "BTCUSDT"

    def test_unrecognized_topic_becomes_unknown_not_dropped(self):
        msg = json.dumps({"topic": "wallet", "data": [{"coin": "USDT"}]})
        events = pwc.parse_ws_message(msg)
        assert len(events) == 1
        assert events[0].kind == "unknown"

    def test_missing_topic_is_unknown(self):
        events = pwc.parse_ws_message(json.dumps({"data": []}))
        assert events[0].kind == "unknown"

    def test_malformed_json_is_unknown_not_an_exception(self):
        events = pwc.parse_ws_message("{not json")
        assert events[0].kind == "unknown"

    def test_distinct_order_status_transitions_have_distinct_ids(self):
        """New -> PartiallyFilled -> Filled are three real events, not
        duplicates of each other, even though the order id is the same."""
        new = json.loads(ORDER_MSG)
        new["data"][0]["orderStatus"] = "New"
        new["data"][0]["updatedTime"] = "900"
        e1 = pwc.parse_ws_message(json.dumps(new))[0]
        e2 = pwc.parse_ws_message(ORDER_MSG)[0]
        assert e1.event_id != e2.event_id


class TestDeduplicator:
    def test_first_sighting_is_not_a_duplicate(self):
        d = pwc._Deduplicator()
        assert d.seen("a") is False

    def test_second_sighting_is_a_duplicate(self):
        d = pwc._Deduplicator()
        d.seen("a")
        assert d.seen("a") is True

    def test_bounded_size_evicts_oldest(self):
        d = pwc._Deduplicator(max_size=2)
        d.seen("a")
        d.seen("b")
        d.seen("c")  # over capacity -> evicts "a" (the oldest)
        assert d.seen("c") is True   # "c" is still tracked
        assert d.seen("a") is False  # "a" was evicted, so this is new again


class _StubStore:
    def __init__(self):
        self.updates = []

    def update_order_status(self, order_link_id, status, exchange_id=""):
        self.updates.append((order_link_id, status, exchange_id))


class _StubClient:
    def __init__(self, store):
        self.store = store

    def ws_auth_message(self):
        return {"op": "auth", "args": ["key", 123, "sig"]}

    def ws_ping_message(self):
        return {"op": "ping"}


class TestHandleRawMessage:
    def _consumer(self):
        store = _StubStore()
        client = _StubClient(store)
        consumer = pwc.WSPrivateConsumer(
            client=client, transport_factory=lambda: None)
        return consumer, store

    def test_order_event_is_queued_then_applied_on_drain(self):
        """`handle_raw_message` (the WS thread) never writes the store
        itself — see module docstring on the single-writer model. Only
        `drain_and_apply` (the writer thread) does."""
        consumer, store = self._consumer()
        applied = consumer.handle_raw_message(ORDER_MSG)
        assert len(applied) == 1
        assert store.updates == [], "handle_raw_message must not write the store"

        drained = consumer.drain_and_apply(store)
        assert len(drained) == 1
        assert store.updates == [("BB-entry-1", "filled", "V-1")]

    def test_execution_event_is_queued_then_applied_on_drain(self):
        consumer, store = self._consumer()
        consumer.handle_raw_message(EXECUTION_MSG)
        assert store.updates == []
        consumer.drain_and_apply(store)
        assert store.updates == [("BB-entry-1", "filled", "V-1")]

    def test_position_event_never_writes_the_store(self):
        """REST remains the authority for position economics/protection —
        see module docstring. A position message may only flag, never
        write, and there is nothing to drain either."""
        consumer, store = self._consumer()
        consumer.handle_raw_message(POSITION_MSG)
        assert store.updates == []
        assert consumer.needs_reconciliation is True
        assert "BTCUSDT" in consumer.dirty_symbols
        assert consumer.drain_and_apply(store) == []

    def test_duplicate_message_is_queued_and_applied_once(self):
        consumer, store = self._consumer()
        consumer.handle_raw_message(ORDER_MSG)
        consumer.handle_raw_message(ORDER_MSG)
        consumer.drain_and_apply(store)
        assert len(store.updates) == 1
        assert consumer.duplicate_event_count == 1
        assert consumer.applied_event_count == 1

    def test_drain_with_nothing_queued_is_a_noop(self):
        consumer, store = self._consumer()
        assert consumer.drain_and_apply(store) == []
        assert store.updates == []

    def test_unknown_message_is_counted_not_queued_and_does_not_raise(self):
        consumer, store = self._consumer()
        applied = consumer.handle_raw_message(
            json.dumps({"topic": "wallet", "data": [{}]}))
        assert applied == []
        assert consumer.drain_and_apply(store) == []
        assert consumer.unknown_message_count == 1

    def test_failed_auth_control_message_sets_needs_reconciliation(self):
        consumer, store = self._consumer()
        consumer.handle_raw_message(
            json.dumps({"op": "auth", "success": False, "ret_msg": "bad sig"}))
        assert consumer.needs_reconciliation is True

    def test_successful_control_message_is_a_noop(self):
        consumer, store = self._consumer()
        consumer.handle_raw_message(
            json.dumps({"op": "subscribe", "success": True}))
        assert consumer.needs_reconciliation is False

    def test_pong_control_message_is_a_noop(self):
        consumer, store = self._consumer()
        consumer.handle_raw_message(json.dumps({"op": "pong"}))
        assert consumer.needs_reconciliation is False

    def test_store_exception_during_drain_is_contained_and_flags_reconciliation(self):
        class ExplodingStore(_StubStore):
            def update_order_status(self, *a, **kw):
                raise RuntimeError("disk full")

        client = _StubClient(_StubStore())
        consumer = pwc.WSPrivateConsumer(
            client=client, transport_factory=lambda: None)
        consumer.handle_raw_message(ORDER_MSG)
        assert consumer.needs_reconciliation is False  # not yet drained

        drained = consumer.drain_and_apply(ExplodingStore())  # must not raise
        assert drained == []
        assert consumer.needs_reconciliation is True


import ws_transport as wt  # noqa: E402


class _ScriptedTransport:
    """connect()/send_text()/recv()/send_pong()/close() with a queue of
    (opcode, payload) to yield from recv(); raises WSClosed (exactly like
    a real dropped connection) once the script runs out."""

    def __init__(self, frames_to_recv):
        self._frames = list(frames_to_recv)
        self.sent = []
        self.connected = False
        self.closed = False

    def connect(self):
        self.connected = True

    def send_text(self, text):
        self.sent.append(text)

    def send_pong(self, payload):
        self.sent.append(("PONG", payload))

    def recv(self, timeout=None):
        if not self._frames:
            raise wt.WSClosed("scripted transport exhausted")
        return self._frames.pop(0)

    def close(self):
        self.closed = True


def _text_frame(obj):
    return (wt.OP_TEXT, json.dumps(obj).encode("utf-8"))


class TestRunOnce:
    def test_authenticates_then_subscribes_then_receives(self):
        store = _StubStore()
        client = _StubClient(store)
        transport = _ScriptedTransport([
            _text_frame({"op": "auth", "success": True}),
            _text_frame({"op": "subscribe", "success": True}),
            _text_frame(json.loads(ORDER_MSG)),
        ])
        consumer = pwc.WSPrivateConsumer(
            client=client, transport_factory=lambda: transport)

        with pytest.raises(wt.WSClosed):
            consumer.run_once()

        assert transport.connected is True
        assert transport.closed is True
        auth_sent = json.loads(transport.sent[0])
        assert auth_sent["op"] == "auth"
        subscribe_sent = json.loads(transport.sent[1])
        assert subscribe_sent["op"] == "subscribe"
        assert set(subscribe_sent["args"]) == set(pwc.WSPrivateConsumer.TOPICS)
        assert store.updates == [], "the WS thread itself must never write"
        consumer.drain_and_apply(store)
        assert store.updates == [("BB-entry-1", "filled", "V-1")]

    def test_max_messages_bound_stops_cleanly_for_tests(self):
        store = _StubStore()
        client = _StubClient(store)
        transport = _ScriptedTransport([
            _text_frame({"op": "auth", "success": True}),
            _text_frame({"op": "subscribe", "success": True}),
            _text_frame(json.loads(ORDER_MSG)),
            _text_frame(json.loads(EXECUTION_MSG)),
        ])
        consumer = pwc.WSPrivateConsumer(
            client=client, transport_factory=lambda: transport)

        consumer.run_once(max_messages=2)

        assert transport.closed is True
        assert store.updates == []
        consumer.drain_and_apply(store)
        assert len(store.updates) == 2

    def test_ping_from_venue_is_answered_with_pong(self):
        """A ping does not count toward max_messages (it carries no data
        event) — the script deliberately ends right after it, so the loop
        answers the ping and then hits WSClosed on the next recv."""
        store = _StubStore()
        client = _StubClient(store)
        transport = _ScriptedTransport([
            _text_frame({"op": "auth", "success": True}),
            _text_frame({"op": "subscribe", "success": True}),
            (wt.OP_PING, b""),
        ])
        consumer = pwc.WSPrivateConsumer(
            client=client, transport_factory=lambda: transport)

        with pytest.raises(wt.WSClosed):
            consumer.run_once(max_messages=1)

        assert any(isinstance(s, tuple) and s[0] == "PONG" for s in transport.sent)


class TestRunForever:
    def test_reconnects_with_backoff_after_disconnect(self):
        store = _StubStore()
        client = _StubClient(store)
        attempts = []

        transports = [
            _ScriptedTransport([
                _text_frame({"op": "auth", "success": True}),
                _text_frame({"op": "subscribe", "success": True}),
                _text_frame(json.loads(ORDER_MSG)),
            ]),
            _ScriptedTransport([
                _text_frame({"op": "auth", "success": True}),
                _text_frame({"op": "subscribe", "success": True}),
            ]),
        ]

        def factory():
            attempts.append(1)
            return transports[len(attempts) - 1]

        consumer = pwc.WSPrivateConsumer(
            client=client, transport_factory=factory,
            sleep=lambda seconds: None)

        class _StopAfterN:
            def __init__(self, n):
                self.n = n
                self.calls = 0

            def is_set(self):
                self.calls += 1
                return self.calls > self.n

        # run_forever checks stop_event.is_set() both at the top of each
        # loop iteration and again right after a connection ends, so
        # reaching 2 real reconnect attempts needs headroom for 4 checks.
        stop = _StopAfterN(3)
        consumer.run_forever(stop)

        assert len(attempts) == 2
        assert consumer.needs_reconciliation is True, (
            "a reconnect must always flag that observation may have a gap")
        assert store.updates == [], "run_forever's own thread must never write"
        consumer.drain_and_apply(store)
        assert store.updates == [("BB-entry-1", "filled", "V-1")]
