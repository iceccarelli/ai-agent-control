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
        assert e.event_id == f"execution:{pwc.deterministic_id('execution', 'E-1')}"
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


# ---------------------------------------------------------------------------
# WS evidence — wired into the existing tools/venue_evidence.py mechanism
# ---------------------------------------------------------------------------

import venue_evidence as ve  # noqa: E402


class _EvidenceStubClient(_StubClient):
    """`_StubClient` plus the two attributes `_write_evidence` reads:
    `venue` (the environment name evidence validates against) and
    `ws_private_url` (checked against `venue_evidence.WS_VENUE_HOSTS`)."""

    def __init__(self, store, *, venue="testnet",
                ws_private_url="wss://stream-testnet.bybit.com/v5/private"):
        super().__init__(store)
        self.venue = venue
        self.ws_private_url = ws_private_url


def _consumer_with_evidence(tmp_path, *, venue="testnet",
                            ws_private_url="wss://stream-testnet.bybit.com/v5/private",
                            assurance_mode=False):
    evidence_path = str(tmp_path / "ws_evidence.jsonl")
    client = _EvidenceStubClient(_StubStore(), venue=venue,
                                 ws_private_url=ws_private_url)
    consumer = pwc.WSPrivateConsumer(
        client=client, transport_factory=lambda: None,
        evidence_path=evidence_path, assurance_mode=assurance_mode)
    return consumer, evidence_path


def _read_records(path):
    with open(path, encoding="utf-8") as h:
        return [json.loads(ln) for ln in h if ln.strip()]


class TestWSEvidenceCapture:
    def test_order_event_produces_an_evidence_record(self, tmp_path):
        consumer, path = _consumer_with_evidence(tmp_path)
        consumer.handle_raw_message(ORDER_MSG)
        consumer.drain_evidence()

        records = _read_records(path)
        assert len(records) == 1
        record = records[0]
        assert record["transport"] == "ws"
        assert record["environment"] == "testnet"
        assert record["order_link_id"] == "BB-entry-1"
        assert record["venue_event_id"].startswith("order:")
        assert record["request"]["duplicate"] is False
        assert ve.verify_chain(path) == []

    def test_execution_and_position_events_produce_records(self, tmp_path):
        consumer, path = _consumer_with_evidence(tmp_path)
        consumer.handle_raw_message(EXECUTION_MSG)
        consumer.handle_raw_message(POSITION_MSG)
        consumer.drain_evidence()

        records = _read_records(path)
        assert [r["request"]["topic"] for r in records] == ["execution", "position"]
        assert ve.verify_chain(path) == []

    def test_control_messages_produce_records(self, tmp_path):
        consumer, path = _consumer_with_evidence(tmp_path)
        consumer.handle_raw_message(json.dumps({"op": "auth", "success": True}))
        consumer.handle_raw_message(json.dumps({"op": "subscribe", "success": True}))
        consumer.handle_raw_message(json.dumps({"op": "pong"}))
        consumer.drain_evidence()

        records = _read_records(path)
        assert [r["request"]["topic"] for r in records] == [
            "control.auth", "control.subscribe", "control.pong"]
        assert ve.verify_chain(path) == []

    def test_unknown_and_malformed_messages_produce_records(self, tmp_path):
        consumer, path = _consumer_with_evidence(tmp_path)
        consumer.handle_raw_message(json.dumps({"topic": "wallet", "data": [{}]}))
        consumer.handle_raw_message("{not json")
        consumer.drain_evidence()

        records = _read_records(path)
        assert len(records) == 2
        assert all(r["venue_event_id"].startswith("unknown:")
                  or r["venue_event_id"].startswith("unparseable:")
                  for r in records)
        assert ve.verify_chain(path) == []

    def test_secrets_in_the_raw_payload_are_redacted(self, tmp_path):
        consumer, path = _consumer_with_evidence(tmp_path)
        msg = json.dumps({"topic": "order", "data": [{
            "symbol": "BTCUSDT", "orderId": "V-1", "orderLinkId": "BB-1",
            "orderStatus": "Filled", "updatedTime": "1",
            "api_key": "super-secret", "sign": "also-secret",
        }]})
        consumer.handle_raw_message(msg)
        consumer.drain_evidence()

        record = _read_records(path)[0]
        assert record["response"]["api_key"] == ve._REDACTED
        assert record["response"]["sign"] == ve._REDACTED

    def test_environment_is_validated_against_the_ws_url(self, tmp_path):
        """A client claiming venue='testnet' while its actual WS URL is
        the mainnet stream host must be refused — the same law
        venue_evidence enforces on REST evidence, now enforced on WS."""
        consumer, path = _consumer_with_evidence(
            tmp_path, venue="testnet",
            ws_private_url="wss://stream.bybit.com/v5/private")  # mainnet host

        consumer.handle_raw_message(ORDER_MSG)
        consumer.drain_evidence()

        assert not os.path.exists(path) or _read_records(path) == []
        assert consumer.evidence_capture_failed is True
        assert consumer.needs_reconciliation is True

    def test_ws_hosts_are_recognized_for_both_mainnet_and_testnet(self):
        assert ve.infer_environment_from_url(
            "wss://stream-testnet.bybit.com/v5/private") == "testnet"
        assert ve.infer_environment_from_url(
            "wss://stream.bybit.com/v5/private") == "mainnet"


class TestWSEvidenceDeterministicIdentity:
    def test_no_python_hash_in_source(self):
        """No bare `hash(...)` CALL may remain anywhere in this module —
        an AST walk, not a text search, so a docstring or comment
        mentioning `hash()` (there are several, explaining exactly why it
        must not be used) can never produce a false positive or, worse,
        hide a real one."""
        import ast
        import inspect

        tree = ast.parse(inspect.getsource(pwc))
        offenders = [
            node.lineno for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "hash"
        ]
        assert offenders == [], (
            f"private_ws_consumer.py calls Python's hash() at line(s) "
            f"{offenders} — durable identity must use deterministic_id")

    def test_same_input_reproduces_the_same_id_across_processes(self, tmp_path):
        """The literal adversarial case: a SEPARATE process (simulated via
        a fresh subprocess importing this module fresh) must derive the
        same event_id from the same order message — proving the identity
        does not depend on PYTHONHASHSEED or any other process-local
        randomization."""
        import subprocess

        script = tmp_path / "derive_id.py"
        script.write_text(
            "import sys, json\n"
            f"sys.path.insert(0, {str(REPO)!r})\n"
            "import private_ws_consumer as pwc\n"
            f"events = pwc.parse_ws_message({ORDER_MSG!r})\n"
            "print(events[0].event_id)\n"
        )
        ids = set()
        for seed in ("0", "1", "random"):
            env = dict(os.environ)
            env["PYTHONHASHSEED"] = seed
            result = subprocess.run(
                [sys.executable, str(script)], capture_output=True,
                text=True, env=env, timeout=30)
            assert result.returncode == 0, result.stderr
            ids.add(result.stdout.strip())
        assert len(ids) == 1, (
            f"event_id changed across PYTHONHASHSEED values: {ids}")

    def test_deterministic_id_ignores_process_hash_randomization(self):
        """Same check as above, without the subprocess cost: calling
        deterministic_id twice in THIS process must already agree, and it
        must not be `str`'s randomized `hash()` under the hood."""
        a = pwc.deterministic_id("order", "V-1", "Filled", "1000")
        b = pwc.deterministic_id("order", "V-1", "Filled", "1000")
        assert a == b
        assert a != str(hash("order|V-1|Filled|1000"))

    def test_distinct_lifecycle_transitions_get_distinct_ids(self):
        def _order(status, updated):
            return json.dumps({"topic": "order", "data": [{
                "symbol": "BTCUSDT", "orderId": "V-1", "orderLinkId": "BB-1",
                "orderStatus": status, "updatedTime": updated}]})

        new = pwc.parse_ws_message(_order("New", "1"))[0]
        partial = pwc.parse_ws_message(_order("PartiallyFilled", "2"))[0]
        filled = pwc.parse_ws_message(_order("Filled", "3"))[0]
        ids = {new.event_id, partial.event_id, filled.event_id}
        assert len(ids) == 3, "distinct lifecycle transitions collapsed"

    def test_same_order_same_status_same_timestamp_different_payload_differs(self):
        """The exact adversarial case named in the mission: identical
        orderId/status/updatedTime but different other content must NOT
        collapse into one identity."""
        def _order(cum_qty):
            return json.dumps({"topic": "order", "data": [{
                "symbol": "BTCUSDT", "orderId": "V-1", "orderLinkId": "BB-1",
                "orderStatus": "PartiallyFilled", "updatedTime": "5",
                "cumExecQty": cum_qty}]})

        a = pwc.parse_ws_message(_order("0.01"))[0]
        b = pwc.parse_ws_message(_order("0.02"))[0]
        assert a.event_id != b.event_id

    def test_identical_payload_is_a_true_duplicate(self):
        a = pwc.parse_ws_message(ORDER_MSG)[0]
        b = pwc.parse_ws_message(ORDER_MSG)[0]
        assert a.event_id == b.event_id


class TestWSEvidenceDuplicateAndOutOfOrder:
    def test_duplicate_delivery_is_recorded_as_duplicate_and_applied_once(
            self, tmp_path):
        consumer, path = _consumer_with_evidence(tmp_path)
        consumer.handle_raw_message(ORDER_MSG)
        consumer.handle_raw_message(ORDER_MSG)  # replayed, e.g. post-reconnect
        consumer.drain_evidence()

        records = _read_records(path)
        assert len(records) == 2, "each delivery gets its own evidence record"
        assert [r["request"]["duplicate"] for r in records] == [False, True]
        assert consumer.duplicate_event_count == 1
        assert consumer.applied_event_count == 1

    def test_out_of_order_delivery_each_captured_and_applied(self, tmp_path):
        def _order(status, updated):
            return json.dumps({"topic": "order", "data": [{
                "symbol": "BTCUSDT", "orderId": "V-1", "orderLinkId": "BB-1",
                "orderStatus": status, "updatedTime": updated}]})

        consumer, path = _consumer_with_evidence(tmp_path)
        # "Filled" arrives before "New" -- a real possibility with
        # independent delivery paths/retries at the venue.
        consumer.handle_raw_message(_order("Filled", "3"))
        consumer.handle_raw_message(_order("New", "1"))
        consumer.drain_evidence()

        records = _read_records(path)
        assert len(records) == 2
        assert all(not r["request"]["duplicate"] for r in records)
        assert consumer.applied_event_count == 2
        assert ve.verify_chain(path) == []


class TestWSEvidenceReconnect:
    def test_connect_and_disconnect_produce_evidence(self, tmp_path):
        evidence_path = str(tmp_path / "ws_evidence.jsonl")
        client = _EvidenceStubClient(_StubStore())

        class _DeadTransport:
            def connect(self):
                pass

            def recv(self, timeout=None):
                raise wt.WSClosed("simulated drop")

            def send_text(self, text):
                pass

            def close(self):
                pass

        consumer = pwc.WSPrivateConsumer(
            client=client, transport_factory=lambda: _DeadTransport(),
            evidence_path=evidence_path, sleep=lambda s: None)

        class _StopAfterOne:
            def __init__(self):
                self.calls = 0

            def is_set(self):
                self.calls += 1
                return self.calls > 1

        consumer.run_forever(_StopAfterOne())
        consumer.drain_evidence()

        records = _read_records(evidence_path)
        topics = [r["request"]["topic"] for r in records]
        assert "connection.connected" in topics
        assert "connection.disconnected" in topics
        assert ve.verify_chain(evidence_path) == []


class TestWSEvidenceFailureSemantics:
    def test_normal_mode_capture_failure_degrades_and_continues(self, tmp_path):
        consumer, path = _consumer_with_evidence(tmp_path, assurance_mode=False)
        # A directory in place of the evidence file makes every write fail.
        os.makedirs(path)

        applied = consumer.handle_raw_message(ORDER_MSG)  # must not raise
        assert consumer.evidence_capture_failed is False, (
            "not yet drained -- the failure only surfaces at drain time, "
            "on the writer thread, per FIX 1's ownership model")
        consumer.drain_evidence()  # must not raise either

        assert len(applied) == 1, "trading-relevant processing still happened"
        assert consumer.evidence_capture_failed is True
        assert consumer.needs_reconciliation is True

    def test_assurance_mode_capture_failure_is_flagged_for_the_caller(self, tmp_path):
        """This module never decides pass/fail for assurance mode itself
        — it only raises the flag; the conformance runner is the one that
        must fail closed on it (see tools/testnet_conformance_run.py)."""
        consumer, path = _consumer_with_evidence(tmp_path, assurance_mode=True)
        os.makedirs(path)

        consumer.handle_raw_message(ORDER_MSG)  # must not raise here either
        consumer.drain_evidence()  # or here

        assert consumer.assurance_mode is True
        assert consumer.evidence_capture_failed is True

    def test_no_evidence_path_disables_capture_without_error(self, tmp_path):
        client = _EvidenceStubClient(_StubStore())
        consumer = pwc.WSPrivateConsumer(
            client=client, transport_factory=lambda: None,
            evidence_path=None)
        consumer.handle_raw_message(ORDER_MSG)
        assert consumer.evidence_capture_failed is False
        assert consumer.evidence_records_written == 0


# ---------------------------------------------------------------------------
# FIX 1 — canonical evidence ownership: the WS thread only enqueues; only
# drain_evidence()/drain_and_apply() (the writer thread) performs the write.
# ---------------------------------------------------------------------------


class TestEvidenceWriterThreadOwnership:
    def test_handle_raw_message_never_writes_the_evidence_file(self, tmp_path):
        """The exact FIX 1 acceptance case: observing an event must not,
        by itself, produce a durable evidence write — only draining does."""
        consumer, path = _consumer_with_evidence(tmp_path)
        consumer.handle_raw_message(ORDER_MSG)

        assert not os.path.exists(path) or _read_records(path) == [], (
            "the WS thread must never write durable evidence itself")
        assert consumer.evidence_records_written == 0

    def test_drain_evidence_writes_every_queued_record(self, tmp_path):
        consumer, path = _consumer_with_evidence(tmp_path)
        consumer.handle_raw_message(ORDER_MSG)
        consumer.handle_raw_message(EXECUTION_MSG)

        written = consumer.drain_evidence()

        assert written == 2
        assert len(_read_records(path)) == 2
        assert consumer.evidence_records_written == 2

    def test_drain_and_apply_also_drains_evidence(self, tmp_path):
        """`drain_and_apply` is what `TradingBot.tick()` (the writer
        thread) actually calls — it must drain BOTH the StateStore queue
        AND the evidence queue, in one writer-thread pass."""
        consumer, path = _consumer_with_evidence(tmp_path)
        store = _StubStore()
        consumer.handle_raw_message(ORDER_MSG)

        consumer.drain_and_apply(store)

        assert store.updates == [("BB-entry-1", "filled", "V-1")]
        assert len(_read_records(path)) == 1

    def test_drain_evidence_with_nothing_queued_is_a_noop(self, tmp_path):
        consumer, path = _consumer_with_evidence(tmp_path)
        assert consumer.drain_evidence() == 0
        assert not os.path.exists(path)

    def test_observed_at_is_recorded_distinct_from_captured_at(self, tmp_path):
        """FIX 1: "preserve the distinction between observed and applied".
        `observed_at_utc` (stamped when the WS thread enqueued the event)
        must be present in the durable record, and the record's own
        `captured_at_utc` (stamped when the writer thread applied/wrote
        it) must be present too -- proving both moments are recorded, not
        just one collapsed into the other."""
        consumer, path = _consumer_with_evidence(tmp_path)
        consumer.handle_raw_message(ORDER_MSG)
        consumer.drain_evidence()

        record = _read_records(path)[0]
        assert record["request"]["observed_at_utc"]
        assert record["captured_at_utc"]


# ---------------------------------------------------------------------------
# FIX 4 — invalid venue event identity: no usable orderId/execId must never
# become a legitimate durable "order"/"execution" identity.
# ---------------------------------------------------------------------------


class TestInvalidVenueEventIdentity:
    def test_order_without_order_id_is_unknown_not_a_legitimate_order(self):
        msg = json.dumps({"topic": "order", "data": [{
            "symbol": "BTCUSDT", "orderId": "", "orderLinkId": "BB-1",
            "orderStatus": "Filled", "updatedTime": "1"}]})
        events = pwc.parse_ws_message(msg)
        assert len(events) == 1
        assert events[0].kind == "unknown"

    def test_execution_without_exec_id_is_unknown_not_a_legitimate_execution(self):
        msg = json.dumps({"topic": "execution", "data": [{
            "symbol": "BTCUSDT", "orderId": "V-1", "orderLinkId": "BB-1",
            "execId": "", "execType": "Trade"}]})
        events = pwc.parse_ws_message(msg)
        assert len(events) == 1
        assert events[0].kind == "unknown"

    def test_invalid_order_identity_is_never_queued_or_applied(self):
        store = _StubStore()
        client = _StubClient(store)
        consumer = pwc.WSPrivateConsumer(
            client=client, transport_factory=lambda: None)
        msg = json.dumps({"topic": "order", "data": [{
            "symbol": "BTCUSDT", "orderId": "", "orderLinkId": "BB-1",
            "orderStatus": "Filled", "updatedTime": "1"}]})

        applied = consumer.handle_raw_message(msg)

        assert applied == []
        assert consumer.unknown_message_count == 1
        assert consumer.drain_and_apply(store) == []
        assert store.updates == []

    def test_invalid_execution_identity_is_never_queued_or_applied(self):
        store = _StubStore()
        client = _StubClient(store)
        consumer = pwc.WSPrivateConsumer(
            client=client, transport_factory=lambda: None)
        msg = json.dumps({"topic": "execution", "data": [{
            "symbol": "BTCUSDT", "orderId": "V-1", "orderLinkId": "BB-1",
            "execId": "", "execType": "Trade"}]})

        applied = consumer.handle_raw_message(msg)

        assert applied == []
        assert consumer.unknown_message_count == 1
        assert consumer.drain_and_apply(store) == []
        assert store.updates == []

    def test_invalid_identity_still_produces_evidence_when_assurance_mode(
            self, tmp_path):
        """ASSURANCE/CONFORMANCE mode must fail closed on this -- which it
        does by NEVER producing a legitimate order/execution evidence
        record for it (see verify_evidence_completeness in
        testnet_conformance_run.py), not by a special-case flag here."""
        consumer, path = _consumer_with_evidence(
            tmp_path, assurance_mode=True)
        msg = json.dumps({"topic": "order", "data": [{
            "symbol": "BTCUSDT", "orderId": "", "orderLinkId": "BB-1",
            "orderStatus": "Filled", "updatedTime": "1"}]})

        consumer.handle_raw_message(msg)
        consumer.drain_evidence()

        records = _read_records(path)
        assert len(records) == 1
        assert records[0]["venue_event_id"].startswith("unknown:")
        assert records[0]["request"]["topic"] == "order"


# ---------------------------------------------------------------------------
# FIX 5 — explicit AUTH_OK / SUBSCRIBE_OK: a connected transport alone is
# not sufficient.
# ---------------------------------------------------------------------------


class TestExplicitAuthAndSubscribeSuccess:
    def test_auth_ok_and_subscribe_ok_start_false(self):
        client = _StubClient(_StubStore())
        consumer = pwc.WSPrivateConsumer(
            client=client, transport_factory=lambda: None)
        assert consumer.auth_ok is False
        assert consumer.subscribe_ok is False

    def test_explicit_auth_success_sets_auth_ok(self):
        client = _StubClient(_StubStore())
        consumer = pwc.WSPrivateConsumer(
            client=client, transport_factory=lambda: None)
        consumer.handle_raw_message(json.dumps({"op": "auth", "success": True}))
        assert consumer.auth_ok is True

    def test_missing_success_key_never_sets_auth_ok(self):
        """Fail closed: absence of an explicit `success: true` must never
        be read as success."""
        client = _StubClient(_StubStore())
        consumer = pwc.WSPrivateConsumer(
            client=client, transport_factory=lambda: None)
        consumer.handle_raw_message(json.dumps({"op": "auth"}))
        assert consumer.auth_ok is False

    def test_explicit_subscribe_success_sets_subscribe_ok(self):
        client = _StubClient(_StubStore())
        consumer = pwc.WSPrivateConsumer(
            client=client, transport_factory=lambda: None)
        consumer.handle_raw_message(
            json.dumps({"op": "subscribe", "success": True}))
        assert consumer.subscribe_ok is True

    def test_run_once_raises_when_auth_fails(self):
        client = _StubClient(_StubStore())
        transport = _ScriptedTransport([
            _text_frame({"op": "auth", "success": False, "ret_msg": "bad sig"}),
        ])
        consumer = pwc.WSPrivateConsumer(
            client=client, transport_factory=lambda: transport)

        with pytest.raises(RuntimeError, match="AUTH_OK"):
            consumer.run_once()
        assert consumer.auth_ok is False
        assert transport.closed is True

    def test_run_once_raises_when_subscribe_fails(self):
        client = _StubClient(_StubStore())
        transport = _ScriptedTransport([
            _text_frame({"op": "auth", "success": True}),
            _text_frame({"op": "subscribe", "success": False}),
        ])
        consumer = pwc.WSPrivateConsumer(
            client=client, transport_factory=lambda: transport)

        with pytest.raises(RuntimeError, match="SUBSCRIBE_OK"):
            consumer.run_once()
        assert consumer.auth_ok is True
        assert consumer.subscribe_ok is False
        assert transport.closed is True

    def test_auth_failure_triggers_reconnect_with_backoff_in_run_forever(self):
        """run_forever must treat an AUTH_OK/SUBSCRIBE_OK failure exactly
        like any other connection failure -- reconnect with backoff, flag
        needs_reconciliation -- never a silent proceed into the receive
        loop."""
        client = _StubClient(_StubStore())
        attempts = []

        def factory():
            attempts.append(1)
            return _ScriptedTransport([
                _text_frame({"op": "auth", "success": False}),
            ])

        consumer = pwc.WSPrivateConsumer(
            client=client, transport_factory=factory, sleep=lambda s: None)

        class _StopAfterN:
            def __init__(self, n):
                self.n = n
                self.calls = 0

            def is_set(self):
                self.calls += 1
                return self.calls > self.n

        consumer.run_forever(_StopAfterN(1))

        assert len(attempts) >= 1
        assert consumer.needs_reconciliation is True

    def test_auth_ok_resets_false_at_the_start_of_each_connection_attempt(self):
        """A stale success from a PREVIOUS connection must never be
        mistaken for this (new) one's -- auth_ok/subscribe_ok must reset
        at the start of every run_once() call, on the SAME consumer."""
        client = _StubClient(_StubStore())
        transports = [
            _ScriptedTransport([
                _text_frame({"op": "auth", "success": True}),
                _text_frame({"op": "subscribe", "success": True}),
            ]),
            _ScriptedTransport([
                _text_frame({"op": "auth", "success": False}),
            ]),
        ]
        calls = []

        def factory():
            calls.append(1)
            return transports[len(calls) - 1]

        consumer = pwc.WSPrivateConsumer(
            client=client, transport_factory=factory)

        consumer.run_once(max_messages=0)
        assert consumer.auth_ok is True
        assert consumer.subscribe_ok is True

        with pytest.raises(RuntimeError):
            consumer.run_once()
        assert consumer.auth_ok is False, (
            "a stale success from the PREVIOUS connection must not carry "
            "over into this one -- auth_ok must reset at the start of "
            "every run_once() call")
