"""Offline tests for tools/exec_evidence.py (outbox, REST client, drain, lease, heartbeat).

HTTP goes to a fake PostgREST on 127.0.0.1 (http.server in a thread) through the constructor flag
``allow_insecure_loopback_for_tests=True``, which the client refuses for any other host. The SQL semantics of the
lease and of exec_event_put are proven against real Postgres in test_exec_evidence_migration.py; the fake mirrors them
only to exercise the client's behaviour.
"""
import ast
import json
import logging
import os
import sys
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE / "tools"))

import exec_evidence as X  # noqa: E402

KEY = "sbtest-" + "Zq7" * 12 + "-SENTINEL-KEY"
OWNER = str(uuid.uuid4())


# --------------------------------------------------------------------------- fake PostgREST
class Fake:
    def __init__(self):
        self.lock = threading.Lock()
        self.events = {}            # event_id -> (owner, sha)
        self.hbs = {}
        self.requests = []          # (path, headers dict, body)
        self.script = []            # status codes to answer next (consumed in order); 0/None = normal
        self.redirect = False
        self.now = 1000.0
        self.lease = {}             # owner -> list of dict rows
        self.reject_kind = None     # kind answered with a 400

    def handle(self, path, headers, body):
        fn = path.rsplit("/", 1)[-1]
        a = json.loads(body)
        with self.lock:
            self.requests.append((path, {k.lower(): v for k, v in headers.items()}, a))
            if self.redirect:
                return 302, None
            if self.script:
                s = self.script.pop(0)
                if s:
                    return s, {"code": "XX000", "message": "boom " + headers.get("apikey", "")}
            if fn == "exec_event_put":
                r = a["p_row"]
                if self.reject_kind and r["kind"] == self.reject_kind:
                    return 400, {"code": "23514", "message": "check"}
                cur = self.events.get(r["event_id"])
                if cur is None and any(v[1] == r["content_sha256"] for v in self.events.values()):
                    return 200, "CONFLICT"
                if cur is None:
                    self.events[r["event_id"]] = (r["owner_id"], r["content_sha256"])
                    return 200, "INSERTED"
                return 200, "UNCHANGED" if cur == (r["owner_id"], r["content_sha256"]) else "CONFLICT"
            if fn == "exec_heartbeat_put":
                r = a["p_row"]
                k = (r["owner_id"], r["worker_id"], r["run_id"], r["seq"])
                if k not in self.hbs:
                    self.hbs[k] = r["content_sha256"]
                    return 200, "INSERTED"
                return 200, "UNCHANGED" if self.hbs[k] == r["content_sha256"] else "CONFLICT"
            rows = self.lease.setdefault(a["p_owner"], [])
            cur = rows[-1] if rows else None
            live = cur and cur["action"] != "RELEASE" and cur["exp"] > self.now
            if fn == "exec_lease_acquire":
                if live and cur["holder"] != a["p_holder"]:
                    return 200, None
                tok = max([r["tok"] for r in rows], default=0) + 1
                rows.append({"holder": a["p_holder"], "tok": tok, "action": "ACQUIRE", "exp": self.now + a["p_ttl_seconds"]})
                return 200, tok
            if fn == "exec_lease_renew":
                if live and cur["holder"] == a["p_holder"] and cur["tok"] == a["p_token"]:
                    rows.append({**cur, "action": "RENEW", "exp": self.now + a["p_ttl_seconds"]})
                    return 200, True
                return 200, False
            if fn == "exec_lease_release":
                if cur and cur["action"] != "RELEASE" and cur["holder"] == a["p_holder"] and cur["tok"] == a["p_token"]:
                    rows.append({**cur, "action": "RELEASE", "exp": self.now})
                    return 200, True
                return 200, False
        return 404, {"code": "PGRST202"}


@pytest.fixture()
def fake():
    f = Fake()

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            status, out = f.handle(self.path, self.headers, body)
            if status == 302:
                self.send_response(302)
                self.send_header("Location", "http://127.0.0.1:9/elsewhere")
                self.end_headers()
                return
            data = json.dumps(out).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    f.url = f"http://127.0.0.1:{srv.server_address[1]}"
    f.srv = srv
    yield f
    srv.shutdown()
    srv.server_close()


def client(fake, **kw):
    kw.setdefault("sleep", lambda s: None)
    return X.RestClient(fake.url, KEY, allow_insecure_loopback_for_tests=True, **kw)


@pytest.fixture()
def box(tmp_path):
    b = X.Outbox(str(tmp_path / "ob"), clock=lambda: 1_700_000_000_000)
    yield b
    b.close()


def ev(box, n, kind="DECISION", **kw):
    return box.append(kind, {"n": n, "note": "x"}, session_id="s-1", occurred_at_ms=1_700_000_000_000 + n, **kw)


# --------------------------------------------------------------------------- module hygiene
def test_module_reads_no_environment_and_is_stdlib_only():
    src = (HERE / "tools" / "exec_evidence.py").read_text()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in ("environ", "getenv", "putenv"):
            pytest.fail("exec_evidence must not read the environment")
    mods = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            mods.add((node.module or "").split(".")[0])
    third_party = mods - set(sys.stdlib_module_names) - {"supabase_sync"}
    assert not third_party, third_party


# --------------------------------------------------------------------------- outbox
def test_append_assigns_monotonic_seq_and_immutable_ids(box):
    a, b = ev(box, 1), ev(box, 2)
    assert (a["seq"], b["seq"]) == (1, 2)
    assert a["event_id"] != b["event_id"] and len(a["content_sha256"]) == 64
    assert set(a) == {"event_id", "kind", "session_id", "seq", "payload", "content_sha256", "occurred_at_ms"}
    assert box.pending == 2


def test_duplicate_is_suppressed_and_returns_the_stored_event(box):
    a = ev(box, 1)
    again = ev(box, 1)
    assert again["event_id"] == a["event_id"] and again["seq"] == a["seq"]
    assert box.pending == 1 and box.last_seq == 1


def test_same_event_id_with_different_content_is_refused_and_quarantined(box):
    box.append("PLAN", {"a": 1}, occurred_at_ms=1_700_000_000_001, event_id="plan:fixed-id-0001")
    with pytest.raises(X.EventConflict):
        box.append("PLAN", {"a": 2}, occurred_at_ms=1_700_000_000_001, event_id="plan:fixed-id-0001")
    assert box.pending == 1
    q = box.quarantined()
    assert len(q) == 1 and q[0]["reason"] == "LOCAL_CONFLICT"


def test_restart_resumes_pending_seq_and_dedup(tmp_path):
    d = str(tmp_path / "ob")
    b1 = X.Outbox(d)
    ids = [ev(b1, i)["event_id"] for i in range(5)]
    b1.close()
    b2 = X.Outbox(d)
    assert b2.pending == 5 and b2.last_seq == 5
    assert ev(b2, 99)["seq"] == 6
    assert ev(b2, 2)["event_id"] == ids[2] and b2.pending == 6     # duplicate across the restart
    b2.close()


def test_seq_survives_a_fully_drained_and_restarted_outbox(tmp_path, fake):
    d = str(tmp_path / "ob")
    b = X.Outbox(d, segment_bytes=512)
    for i in range(10):
        ev(b, i)
    rep = X.EvidenceSync(b, client(fake), OWNER).drain()
    assert rep.counts[X.INSERTED] == 10 and b.pending == 0
    b.close()
    b2 = X.Outbox(d, segment_bytes=512)
    assert b2.last_seq == 10 and ev(b2, 50)["seq"] == 11
    b2.close()


def test_torn_trailing_line_is_repaired_and_a_bad_middle_line_is_not_skipped(tmp_path):
    d = str(tmp_path / "ob")
    b = X.Outbox(d)
    ev(b, 1)
    ev(b, 2)
    b.close()
    seg = Path(d) / "seg-00000001.jsonl"
    seg.write_bytes(seg.read_bytes() + b'{"event_id":"torn')        # crash mid-append
    b2 = X.Outbox(d)
    assert b2.pending == 2 and seg.read_bytes().endswith(b"\n")
    b2.close()
    lines = seg.read_bytes().split(b"\n")
    seg.write_bytes(b"\n".join([b"{not json}", lines[1], b""]))
    with pytest.raises(X.OutboxCorrupt):
        X.Outbox(d)


def test_second_process_cannot_open_the_same_outbox(tmp_path):
    b = X.Outbox(str(tmp_path / "ob"))
    with pytest.raises(X.OutboxLocked):
        X.Outbox(str(tmp_path / "ob"))
    b.close()
    X.Outbox(str(tmp_path / "ob")).close()


def test_capacity_overflow_raises_records_an_incident_and_never_silently_drops(tmp_path, fake):
    b = X.Outbox(str(tmp_path / "ob"), max_bytes=4096, segment_bytes=1024, reserve_bytes=4096)
    stored = 0
    with pytest.raises(X.OutboxOverflow) as ei:
        for i in range(1000):
            b.append("FILL", {"i": i, "pad": "p" * 100}, session_id="s-1", occurred_at_ms=1_700_000_000_000 + i)
            stored += 1
    assert ei.value.dropped == 1 and 0 < stored < 100
    with pytest.raises(X.OutboxOverflow):
        b.append("FILL", {"i": 5000}, occurred_at_ms=1_700_000_005_000)
    assert b.overflow_dropped == 2
    pending = [e for _s, _o, e in b.iter_pending()]
    inc = [e for e in pending if e["kind"] == "INCIDENT"]
    assert len(inc) == 1 and inc[0]["payload"]["incident"] == "OUTBOX_OVERFLOW" and inc[0]["payload"]["state"] == "OPEN"
    # the incident (and everything stored) reaches Supabase; afterwards capacity returns and the loss is closed out explicitly
    rep = X.EvidenceSync(b, client(fake), OWNER).drain()
    assert rep.counts[X.INSERTED] == stored + 1 and b.pending == 0
    b.append("FILL", {"i": 1}, occurred_at_ms=1_700_000_009_000)
    ended = [e for _s, _o, e in b.iter_pending() if e["kind"] == "INCIDENT"]
    assert len(ended) == 1 and ended[0]["payload"] == {"incident": "OUTBOX_OVERFLOW", "state": "ENDED", "dropped_events": 2}
    assert b.overflow_dropped == 0
    b.close()


def test_max_events_is_a_hard_limit_too(tmp_path):
    b = X.Outbox(str(tmp_path / "ob"), max_events=3)
    for i in range(3):
        ev(b, i)
    with pytest.raises(X.OutboxOverflow):
        ev(b, 3)
    b.close()


# --------------------------------------------------------------------------- secret guard
@pytest.mark.parametrize("payload", [
    {"api_secret": "abc"},
    {"note": "ey" + "JhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abcdefghijklmnopqrstuvwxyz012345"},
    {"nested": {"pass" + "word": "hunter2hunter2"}},
])
def test_secret_guard_refuses_payloads_without_echoing_them(box, payload):
    with pytest.raises(X.SecretRefused) as ei:
        box.append("DECISION", payload, occurred_at_ms=1_700_000_000_000)
    assert "hunter2" not in str(ei.value) and "eyJhbGci" not in str(ei.value)
    assert box.pending == 0


def test_unknown_kind_and_bad_ids_are_refused(box):
    with pytest.raises(X.EventRefused):
        box.append("TRADE_NOW", {"a": 1})
    with pytest.raises(X.EventRefused):
        box.append("DECISION", {"a": 1}, event_id="short")
    with pytest.raises(X.EventRefused):
        box.append("DECISION", {"a": 1}, session_id="has space")


# --------------------------------------------------------------------------- REST client
@pytest.mark.parametrize("url", ["http://abc.supabase.co", "https://example.com", "https://abc.supabase.co.evil.com",
                                 "https://evil.com/.supabase.co", "https://u:p@abc.supabase.co", "https://abc.supabase.co/rest",
                                 "https://supabase.co", "https://abc.supabase.co:8443", "ftp://abc.supabase.co", ""])
def test_client_refuses_non_https_and_non_supabase_hosts(url):
    with pytest.raises(X.ClientRefused):
        X.RestClient(url, KEY)


def test_a_project_host_is_accepted_without_any_network_call():
    c = X.RestClient("https://abcdefgh.supabase.co", KEY)
    assert "abcdefgh.supabase.co" in repr(c) and KEY not in repr(c) and KEY not in str(c)


@pytest.mark.parametrize("url", ["http://localhost:1", "http://127.0.0.2:1", "http://example.com", "https://abc.supabase.co", "http://[::1]:1"])
def test_insecure_test_flag_is_refused_unless_host_is_127_0_0_1(url):
    with pytest.raises(X.ClientRefused):
        X.RestClient(url, KEY, allow_insecure_loopback_for_tests=True)


def test_bad_keys_are_refused_without_echo():
    for bad in ("", "has space", "line\nbreak", None):
        with pytest.raises(X.ClientRefused) as ei:
            X.RestClient("https://abc.supabase.co", bad)
        assert "has space" not in str(ei.value)


def test_request_carries_the_key_only_as_headers_and_targets_the_carry_schema(fake):
    c = client(fake)
    assert c.rpc("exec_lease_acquire", {"p_owner": OWNER, "p_holder": "h", "p_ttl_seconds": 5}) == 1
    path, headers, body = fake.requests[-1]
    assert path == "/rest/v1/rpc/exec_lease_acquire"
    assert headers["apikey"] == KEY and headers["authorization"] == "Bearer " + KEY and headers["content-profile"] == "carry"
    assert KEY not in json.dumps(body)


def test_redirects_are_refused_and_not_retried(fake):
    fake.redirect = True
    with pytest.raises(X.RestError) as ei:
        client(fake, retries=3).rpc("exec_event_put", {"p_row": {}})
    assert ei.value.kind == "redirect_refused" and not ei.value.retryable
    assert len(fake.requests) == 1


def test_retries_are_bounded_with_backoff_and_succeed_after_transient_errors(fake):
    sleeps = []
    fake.script = [500, 503]
    c = client(fake, retries=3, backoff=0.5, sleep=sleeps.append)
    assert c.rpc("exec_lease_acquire", {"p_owner": OWNER, "p_holder": "h", "p_ttl_seconds": 5}) == 1
    assert sleeps == [0.5, 1.0] and len(fake.requests) == 3
    fake.script = [500] * 10
    n = len(fake.requests)
    with pytest.raises(X.RestError):
        c.rpc("exec_lease_acquire", {"p_owner": OWNER, "p_holder": "h", "p_ttl_seconds": 5})
    assert len(fake.requests) - n == 4        # 1 + retries, never unbounded


def test_the_key_never_appears_in_exceptions_logs_or_reprs(fake, box, caplog):
    caplog.set_level(logging.DEBUG)
    fake.script = [500] * 4 + [401] * 4 + [403]       # the fake even echoes the apikey header in its error body
    c = client(fake, retries=3)
    texts = [repr(c), str(c)]
    with pytest.raises(X.RestError) as ei:
        c.rpc("exec_event_put", {"p_row": {}})
    texts += [str(ei.value), repr(ei.value), repr(ei.value.__dict__)]
    ev(box, 1)
    s = X.EvidenceSync(box, c, OWNER)
    rep = s.drain()
    texts += [repr(rep), rep.stopped or ""]
    lease = X.LeaseClient(c, OWNER, "holder-a")
    assert lease.acquire() is False
    with pytest.raises(X.LeaseNotHeld) as le:
        lease.assert_held()
    texts += [str(le.value), repr(lease.__dict__)]
    dead = X.RestClient("http://127.0.0.1:1", KEY, allow_insecure_loopback_for_tests=True, retries=0)
    with pytest.raises(X.RestError) as de:
        dead.rpc("x", {})
    texts += [str(de.value), repr(de.value.__dict__)]
    texts += [r.getMessage() for r in caplog.records]
    for t in texts:
        assert KEY not in t
    with pytest.raises(TypeError):
        import pickle
        pickle.dumps(c)
    import copy
    with pytest.raises(TypeError):
        copy.deepcopy(c._key)


def test_response_garbage_is_a_failure_not_a_crash(fake):
    # unknown function -> 404 PGRST202: a failure, never a crash
    with pytest.raises(X.RestError):
        client(fake, retries=0).rpc("nope", {})


# --------------------------------------------------------------------------- drain
def test_drain_inserts_in_order_then_a_second_drain_is_a_noop(box, fake):
    for i in range(5):
        ev(box, i)
    s = X.EvidenceSync(box, client(fake), OWNER)
    rep = s.drain()
    assert rep.counts == {X.INSERTED: 5, X.UNCHANGED: 0, X.CONFLICT: 0, X.FAILED: 0} and rep.ok and box.pending == 0
    assert [r[2]["p_row"]["seq"] for r in fake.requests] == [1, 2, 3, 4, 5]
    row = fake.requests[0][2]["p_row"]
    assert row["owner_id"] == OWNER and row["occurred_at"].endswith("Z") and "received_at" not in row
    assert s.drain().counts == {X.INSERTED: 0, X.UNCHANGED: 0, X.CONFLICT: 0, X.FAILED: 0}


def test_crash_between_remote_insert_and_cursor_commit_resends_as_unchanged(tmp_path, fake, monkeypatch):
    d = str(tmp_path / "ob")
    b = X.Outbox(d)
    for i in range(4):
        ev(b, i)
    monkeypatch.setattr(b, "commit", lambda *a: None)      # the process dies before the cursor is written
    X.EvidenceSync(b, client(fake), OWNER).drain()
    b.close()
    assert len(fake.events) == 4
    b2 = X.Outbox(d)
    assert b2.pending == 4
    rep = X.EvidenceSync(b2, client(fake), OWNER).drain()
    assert rep.counts[X.UNCHANGED] == 4 and rep.counts[X.INSERTED] == 0 and len(fake.events) == 4 and b2.pending == 0
    b2.close()


def test_failure_stops_the_drain_keeps_every_event_and_resumes_in_order(box, fake):
    for i in range(6):
        ev(box, i)
    s = X.EvidenceSync(box, client(fake, retries=0), OWNER, batch=2)
    fake.script = [0, 0, 0, 503]                       # three events succeed, the fourth fails
    rep = s.drain()
    assert rep.counts[X.INSERTED] == 3 and rep.counts[X.FAILED] == 1 and rep.pending == 3 and not rep.ok
    assert rep.stopped and "503" in rep.stopped
    rep2 = s.drain()
    assert rep2.counts[X.INSERTED] == 3 and rep2.counts[X.FAILED] == 0 and box.pending == 0
    assert len(fake.events) == 6


def test_resume_after_restart_continues_from_the_durable_cursor(tmp_path, fake):
    d = str(tmp_path / "ob")
    b = X.Outbox(d)
    for i in range(6):
        ev(b, i)
    fake.script = [0, 0, 503]
    X.EvidenceSync(b, client(fake, retries=0), OWNER).drain()
    b.close()
    b2 = X.Outbox(d)
    assert b2.pending == 4          # the failed one and what followed (the commit covers only the two stored)
    rep = X.EvidenceSync(b2, client(fake), OWNER).drain()
    assert rep.counts[X.INSERTED] == 4 and len(fake.events) == 6 and b2.pending == 0
    b2.close()


def test_conflict_is_reported_quarantined_and_never_retried_or_overwritten(box, fake):
    a = ev(box, 1)
    fake.events[a["event_id"]] = (OWNER, "f" * 64)     # the database already holds that id with other content
    ev(box, 2)
    s = X.EvidenceSync(box, client(fake), OWNER)
    rep = s.drain()
    assert rep.counts[X.CONFLICT] == 1 and rep.counts[X.INSERTED] == 1 and rep.quarantined == 1 and not rep.ok
    assert fake.events[a["event_id"]] == (OWNER, "f" * 64)          # never overwritten
    n = len(fake.requests)
    assert s.drain().counts[X.CONFLICT] == 0 and len(fake.requests) == n    # not retried
    q = box.quarantined()
    assert len(q) == 1 and q[0]["reason"] == "CONFLICT" and q[0]["event"]["event_id"] == a["event_id"]


def test_deterministically_rejected_event_is_quarantined_so_it_cannot_block_the_queue(box, fake):
    box.append("COST", {"a": 1}, occurred_at_ms=1_700_000_000_001)
    ev(box, 2)
    fake.reject_kind = "COST"
    rep = X.EvidenceSync(box, client(fake), OWNER).drain()
    assert rep.counts[X.FAILED] == 1 and rep.counts[X.INSERTED] == 1 and rep.quarantined == 1 and box.pending == 0
    assert box.quarantined()[0]["reason"] == "REJECTED"


def test_unrecognised_server_answer_is_a_retryable_failure(box, fake, monkeypatch):
    ev(box, 1)
    c = client(fake, retries=0)
    monkeypatch.setattr(c, "rpc", lambda *a, **k: "SOMETHING_ELSE")
    rep = X.EvidenceSync(box, c, OWNER).drain()
    assert rep.counts[X.FAILED] == 1 and box.pending == 1


def test_drain_rescans_for_secrets_and_quarantines(box, fake):
    ev(box, 1)
    for p in Path(box.dir).glob("seg-*.jsonl"):       # tamper with the file on disk after the append-time guard
        p.write_text(p.read_text().replace('"note":"x"', '"pass' + 'word":"hunter2hunter2"'))
    rep = X.EvidenceSync(box, client(fake), OWNER).drain()
    assert rep.quarantined == 1 and not fake.requests and "hunter2" not in json.dumps(rep.__dict__)


# --------------------------------------------------------------------------- heartbeat
def hb(**over):
    base = dict(worker_id="w1", app="carry-worker", machine_id="m1", region="fra", code_revision="abc1234", runtime_version="3.11.9",
                run_id="run-1", mode="TESTNET", state="HEDGED", seq=1, outbox_pending=0, disk_free=123, at_ms=1_700_000_000_000)
    base.update(over)
    return X.build_heartbeat(**base)


def test_heartbeat_has_exactly_the_contract_fields():
    h = hb(session_id="s-1", lease_token=3, last_bybit_read_ms=5)
    assert tuple(h) == X.HEARTBEAT_FIELDS and h["lease_token"] == 3 and h["last_market_data_ms"] is None
    assert len(X.ENGINE_STATES) == 17


@pytest.mark.parametrize("over", [{"state": "LIVE"}, {"mode": "LIVE"}, {"seq": -1}, {"outbox_pending": True}, {"worker_id": " "},
                                  {"lease_token": 0}, {"disk_free": -5}, {"at_ms": 0}])
def test_heartbeat_validation_refuses_bad_values(over):
    with pytest.raises(X.EventRefused):
        hb(**over)


def test_extra_or_missing_heartbeat_fields_are_refused():
    h = hb()
    with pytest.raises(X.EventRefused):
        X.validate_heartbeat({**h, "extra": 1})
    h.pop("region")
    with pytest.raises(X.EventRefused):
        X.validate_heartbeat(h)


def test_send_heartbeat_verdicts_and_failure(box, fake):
    s = X.EvidenceSync(box, client(fake, retries=0), OWNER)
    h = hb()
    assert s.send_heartbeat(h) == X.INSERTED and s.send_heartbeat(h) == X.UNCHANGED
    fake.script = [500]
    assert s.send_heartbeat(hb(seq=2)) == X.FAILED


# --------------------------------------------------------------------------- lease
def test_two_holders_race_exactly_one_wins(fake):
    res = {}
    start = threading.Barrier(2)

    def go(name):
        lc = X.LeaseClient(client(fake), OWNER, name, ttl_seconds=30)
        start.wait()
        res[name] = lc.acquire()

    ts = [threading.Thread(target=go, args=(n,)) for n in ("holder-a", "holder-b")]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert sorted(res.values()) == [False, True]


def test_token_strictly_increases_across_reacquisition_and_renew_keeps_it(fake):
    lc = X.LeaseClient(client(fake), OWNER, "holder-a", ttl_seconds=30)
    seen = []
    for _ in range(3):
        assert lc.acquire()
        seen.append(lc.token)
        assert lc.renew() and lc.token == seen[-1]
        assert lc.release() and lc.token is None
    assert seen == sorted(set(seen)) and seen == [1, 2, 3]
    other = X.LeaseClient(client(fake), OWNER, "holder-b")
    assert other.acquire() and other.token == 4


def test_stale_token_renew_fails_and_clears_the_local_lease(fake):
    a = X.LeaseClient(client(fake), OWNER, "holder-a", ttl_seconds=10)
    b = X.LeaseClient(client(fake), OWNER, "holder-b", ttl_seconds=10)
    assert a.acquire()
    fake.now += 11                                      # a's lease expires on the server
    assert b.acquire() and b.token == 2                 # an expired lease can be taken over
    assert a.renew() is False and a.token is None       # stale token: fails and a no longer believes it holds
    with pytest.raises(X.LeaseNotHeld):
        a.assert_held()
    assert b.assert_held() == 2


def test_acquire_refused_while_another_holder_is_live(fake):
    a = X.LeaseClient(client(fake), OWNER, "holder-a")
    b = X.LeaseClient(client(fake), OWNER, "holder-b")
    assert a.acquire() and not b.acquire() and b.token is None
    assert a.release() and b.acquire()


def test_network_error_fails_closed_everywhere(fake):
    t = [0.0]
    lc = X.LeaseClient(client(fake, retries=0), OWNER, "holder-a", ttl_seconds=30, monotonic=lambda: t[0])
    assert lc.acquire() and lc.assert_held() == 1
    fake.script = [500]
    assert lc.renew() is False                          # HTTP error
    with pytest.raises(X.LeaseNotHeld):
        lc.assert_held()
    assert lc.acquire() and lc.assert_held() >= 2
    fake.srv.shutdown()
    fake.srv.server_close()
    assert lc.renew() is False and lc.token is None     # connection refused
    assert lc.acquire() is False and lc.release() is False
    with pytest.raises(X.LeaseNotHeld):
        lc.assert_held()


def test_unparseable_answers_are_not_held(fake, monkeypatch):
    c = client(fake)
    lc = X.LeaseClient(c, OWNER, "holder-a")
    for bad in (True, "7", 0, -1, None, [1], 1.5):
        monkeypatch.setattr(c, "rpc", lambda *a, _b=bad, **k: _b)
        assert lc.acquire() is False and lc.token is None
    monkeypatch.setattr(c, "rpc", lambda *a, **k: 3)
    assert lc.acquire() and lc.token == 3
    for bad in ("true", 1, None, [True]):
        monkeypatch.setattr(c, "rpc", lambda *a, _b=bad, **k: _b)
        lc._token, lc._deadline = 3, 1e18
        assert lc.renew() is False and lc.token is None


def test_assert_held_expires_locally_without_a_round_trip(fake):
    t = [100.0]
    lc = X.LeaseClient(client(fake), OWNER, "holder-a", ttl_seconds=20, margin_seconds=5, monotonic=lambda: t[0])
    assert lc.acquire()
    t[0] += 14.9
    assert lc.assert_held() == 1
    n = len(fake.requests)
    t[0] += 0.2                                          # past ttl - margin: must stop before the server would
    with pytest.raises(X.LeaseNotHeld):
        lc.assert_held()
    assert len(fake.requests) == n


def test_assert_held_can_verify_remotely(fake):
    a = X.LeaseClient(client(fake), OWNER, "holder-a", ttl_seconds=10)
    b = X.LeaseClient(client(fake), OWNER, "holder-b", ttl_seconds=10)
    assert a.acquire()
    assert a.assert_held(verify_remote=True) == 1
    fake.now += 11
    assert b.acquire()
    with pytest.raises(X.LeaseNotHeld):
        a.assert_held(verify_remote=True)


def test_lease_client_refuses_bad_identity():
    c = X.RestClient("https://abc.supabase.co", KEY)
    for kw in ({"owner_id": "nope", "holder": "h"}, {"owner_id": OWNER, "holder": "bad holder"}, {"owner_id": OWNER, "holder": "h", "ttl_seconds": 0}):
        args = dict(owner_id=OWNER, holder="h")
        args.update(kw)
        with pytest.raises(X.ClientRefused):
            X.LeaseClient(c, **args)
