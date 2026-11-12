"""The Supabase witness for the capital lineage (``ms_capital_witness``), against a fake PostgREST on 127.0.0.1.

What is proven here, and what is not. The fake mirrors the SQL semantics of ``carry.exec_event_put`` (INSERTED / UNCHANGED / CONFLICT by event_id and by content hash) and of the PostgREST read the witness issues; those semantics
were executed on a real Postgres 16 by the round-10 capital specialist in a scratch copy (all migrations applied, the service role, the same filter), and are NOT a CI test here. What is NOT provable offline: that the owner's
project exposes schema ``carry`` to PostgREST and that the key can see the rows (the witness proves the latter itself with a canary and refuses otherwise).
"""
from __future__ import annotations

import json
import os
import sys
import threading
import urllib.parse
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
BOT = os.path.dirname(HERE)
for _p in (HERE, BOT, os.path.join(BOT, "tools")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import exec_evidence as XE  # noqa: E402
import ms_capital as CC  # noqa: E402
import ms_capital_anchor as AN  # noqa: E402
import ms_capital_witness as W  # noqa: E402

KEY = "sbtest-" + "Zq7" * 12 + "-SENTINEL-KEY"
OWNER = str(uuid.uuid4())
ACCT = AN.account_ref_of("testnet", 100000001)


class FakePostgrest:
    """exec_event_put + GET exec_events with the filters the witness uses. ``invisible``: every GET answers [] (a key whose RLS scope sees nothing)."""

    def __init__(self):
        self.lock = threading.Lock()
        self.rows: dict = {}                    # event_id -> row
        self.requests: list = []
        self.invisible = False
        self.fail = 0                           # next N requests answer 500 (with the apikey echoed, to prove it is scrubbed)
        self.redirect = False

    def put(self, row):
        cur = self.rows.get(row["event_id"])
        if cur is None and any(r["content_sha256"] == row["content_sha256"] for r in self.rows.values()):
            return "CONFLICT"
        if cur is None:
            self.rows[row["event_id"]] = row
            return "INSERTED"
        return "UNCHANGED" if (cur["owner_id"], cur["content_sha256"]) == (row["owner_id"], row["content_sha256"]) else "CONFLICT"

    def get(self, query):
        q = {k: v[0] for k, v in urllib.parse.parse_qs(query, keep_blank_values=True).items()}
        rows = [r for r in self.rows.values() if r["owner_id"] == q.get("owner_id", "eq.").removeprefix("eq.")]
        if "event_id" in q:
            rows = [r for r in rows if r["event_id"] == q["event_id"].removeprefix("eq.")]
        if "kind" in q:
            rows = [r for r in rows if r["kind"] == q["kind"].removeprefix("eq.")]
        for k, v in q.items():
            if k.startswith("payload->>"):
                f = k.split(">>", 1)[1]
                rows = [r for r in rows if str(r["payload"].get(f)) == v.removeprefix("eq.")]
        if q.get("order") == "seq.desc":
            rows.sort(key=lambda r: -r["seq"])
        rows = rows[: int(q.get("limit", "1000"))]
        return [] if self.invisible else [{"seq": r["seq"], "payload": r["payload"], "event_id": r["event_id"]} for r in rows]


@pytest.fixture()
def pg():
    f = FakePostgrest()

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, status, obj):
            data = json.dumps(obj).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _common(self, method):
            f.requests.append((method, self.path, {k.lower(): v for k, v in self.headers.items()}))
            if f.redirect:
                self.send_response(302)
                self.send_header("Location", "http://127.0.0.1:9/elsewhere")
                self.end_headers()
                return False
            if f.fail:
                f.fail -= 1
                self._send(500, {"code": "XX000", "message": "boom " + self.headers.get("apikey", "")})
                return False
            return True

        def do_GET(self):
            if self._common("GET"):
                path, _, query = self.path.partition("?")
                with f.lock:
                    self._send(200, f.get(query) if path.endswith("/exec_events") else [])

        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            if self._common("POST"):
                with f.lock:
                    row = json.loads(body)["p_row"]
                    self._send(200, f.put(row))

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    f.url = f"http://127.0.0.1:{srv.server_address[1]}"
    yield f
    srv.shutdown()
    srv.server_close()


def witness(pg, **kw):
    client = XE.RestClient(pg.url, KEY, allow_insecure_loopback_for_tests=True, sleep=lambda s: None, retries=0)
    return W.SupabaseWitness(client, OWNER, **kw)


def _sha(tag, n):
    import hashlib
    return hashlib.sha256(f"{tag}:{n}".encode()).hexdigest()


def entry(seq, kind="SETTLE", h=None, prev=None, realised="-3.5", pending=None):
    h = h or _sha("h", seq)
    prev = prev or (_sha("h", seq - 1) if seq else "00" * 32)
    return {"seq": seq, "kind": kind, "h": h, "prev": prev, "book_digest": _sha("d", seq), "ts_ms": 1_794_312_000_000 + seq,
            "state": {"realised_usd": realised, "loss_streak": 2, "halted": False, "review_required": None, "pending": pending, "epoch": 0}}


# ---------------------------------------------------------------------------------------------------- the client's read path
def test_select_is_a_plain_get_with_the_schema_header_and_no_write_headers(pg):
    c = XE.RestClient(pg.url, KEY, allow_insecure_loopback_for_tests=True, sleep=lambda s: None, retries=0)
    assert c.select("exec_events", {"select": "seq", "owner_id": f"eq.{OWNER}", "limit": "1"}) == []
    method, path, headers = pg.requests[-1]
    assert method == "GET" and path.startswith("/rest/v1/exec_events?") and "owner_id=eq." in path
    assert headers["accept-profile"] == "carry" and "content-profile" not in headers and "content-type" not in headers


def test_select_refuses_bad_table_names_and_redirects(pg):
    c = XE.RestClient(pg.url, KEY, allow_insecure_loopback_for_tests=True, sleep=lambda s: None, retries=0)
    for bad in ("exec_events; drop", "../x", "A", ""):
        with pytest.raises(XE.ClientRefused):
            c.select(bad, {"select": "seq"})
    pg.redirect = True
    with pytest.raises(XE.RestError) as e:
        c.select("exec_events", {"select": "seq"})
    assert e.value.kind == "redirect_refused"


def test_a_failing_read_never_carries_the_key_in_the_error(pg):
    pg.fail = 1
    w = witness(pg)
    with pytest.raises(AN.WitnessError) as e:
        w.head(ACCT)
    assert KEY not in str(e.value) and "SENTINEL" not in str(e.value)


# ---------------------------------------------------------------------------------------------------- publish and head
def test_publish_then_head_round_trips_the_position_not_the_hash(pg):
    w = witness(pg)
    assert w.head(ACCT) is None                      # a visible key and no rows: genuinely no history
    w.publish(ACCT, entry(0, "GENESIS"))
    w.publish(ACCT, entry(1))
    seq, ref = w.head(ACCT)
    assert seq == 1 and ref == W.ref(entry(1)["h"]) and ref != entry(1)["h"] and len(ref) == 16
    stored = [r for r in pg.rows.values() if r["payload"].get("stage") == "CAPITAL_ANCHOR"]
    assert {r["event_id"] for r in stored} == {W.event_id(ACCT, 0), W.event_id(ACCT, 1)}
    assert all(entry(1)["h"] not in json.dumps(r["payload"]) and ACCT not in json.dumps(r["payload"]) for r in stored), "no raw hash and no account reference leave the machine"


def test_a_retry_after_a_lost_response_is_unchanged_and_a_different_head_at_the_same_seq_is_a_conflict(pg):
    w = witness(pg)
    w.publish(ACCT, entry(0, "GENESIS"))
    w.publish(ACCT, entry(0, "GENESIS"))             # identical content: UNCHANGED, no error
    with pytest.raises(AN.WitnessConflict):
        w.publish(ACCT, entry(0, "GENESIS", h=_sha("forged", 0)))


def test_an_empty_answer_is_not_believed_unless_the_key_can_read_back_what_it_wrote(pg):
    pg.invisible = True                              # the key's scope sees nothing: [] is what it gets for everything
    w = witness(pg)
    with pytest.raises(AN.WitnessError) as e:
        w.head(ACCT)
    assert "VISIBILITY_UNPROVEN" in str(e.value), "an invisible table must never read as 'this account has no history' (that would let a reset through)"


def test_a_malformed_row_is_a_witness_error_not_a_head(pg):
    w = witness(pg)
    w.head(ACCT)
    pg.rows["capital:x:000000009"] = {"owner_id": OWNER, "event_id": "capital:x:000000009", "kind": "PROVENANCE", "seq": 9, "content_sha256": "1" * 64,
                                      "payload": {"stage": "CAPITAL_ANCHOR", "book_ref": W.book_ref(ACCT), "seq": 8, "head_ref": "nope"}}
    with pytest.raises(AN.WitnessError):
        w.head(ACCT)


@pytest.mark.parametrize("n", range(0, 3000, 7))
def test_every_entry_the_lineage_can_produce_passes_the_evidence_secret_guard(n):
    """R7: the guard refuses what a witness payload naturally holds (a 16-digit run, a 24-hex token, an 'account' key). The refs are letter-led and short by construction; sweep entries whose hashes start with digits."""
    for kind in sorted(AN.KINDS):
        e = entry(n, kind, h=("%d" % (10 ** 15 + n)).ljust(64, "7"), prev=("%d" % (10 ** 15 + n - 1)).ljust(64, "3"))
        XE.make_event(W.EVENT_KIND, W.payload_of(ACCT, e), occurred_at_ms=e["ts_ms"], event_id=W.event_id(ACCT, e["seq"]), seq=e["seq"])


def test_the_all_digit_hash_prefix_that_breaks_a_bare_ref16_is_safe_here():
    digits_only = "1234567890123456" + "ab" * 24
    e = entry(1, h=digits_only, prev="2345678901234567" + "cd" * 24)
    XE.make_event(W.EVENT_KIND, W.payload_of(ACCT, e), occurred_at_ms=e["ts_ms"], event_id=W.event_id(ACCT, 1), seq=1)
    assert W.ref(digits_only) == "h123456789012345"


# ---------------------------------------------------------------------------------------------------- the witness behind the anchor
@pytest.fixture()
def world(tmp_path):
    class Wd:
        anchor = str(tmp_path / "anchor")
        root = str(tmp_path / "root")
        tmp = tmp_path
    return Wd


def genesis(world, w):
    an = AN.Anchor(world.root, ACCT, "testnet", world.anchor, witness=w)
    CC.CapitalBook(world.root, an).initialise({"test": True})
    return CC.CapitalBook.anchored(world.root, account_ref=ACCT, venue="testnet", anchor_dir=world.anchor, witness=w)


def test_the_anchor_publishes_each_head_through_the_real_witness(pg, world):
    w = witness(pg, required=True)
    book = genesis(world, w)
    book.ensure_baseline("232")
    book.park("s1", "232", None, False, False)
    assert book.barrier() == "OK"
    entries = AN.Lineage(os.path.join(world.anchor, f"lineage-{ACCT}.jsonl"), ACCT, "testnet").read()
    seq, ref = w.head(ACCT)
    assert seq == entries[-1]["seq"] and ref == W.ref(entries[-1]["h"])


def test_a_volume_rolled_back_behind_the_real_witness_is_refused(pg, world):
    import shutil
    w = witness(pg, required=True)
    book = genesis(world, w)
    book.ensure_baseline("232")
    snap = world.tmp / "snap"
    shutil.copytree(world.anchor, snap / "anchor")
    shutil.copytree(world.root, snap / "root")
    book.park("s1", "232", "-9", True, True)
    book.settle("223")
    assert w.head(ACCT)[0] > 2
    shutil.rmtree(world.anchor)
    shutil.rmtree(world.root)
    shutil.copytree(snap / "anchor", world.anchor)
    shutil.copytree(snap / "root", world.root)
    with pytest.raises(CC.BookRefused) as e:
        CC.CapitalBook.anchored(world.root, account_ref=ACCT, venue="testnet", anchor_dir=world.anchor, witness=w)
    assert e.value.code == "LOCAL_BEHIND_WITNESS"


def test_a_required_witness_that_is_down_refuses_at_open_and_at_the_barrier_but_not_a_plain_commit(pg, world):
    w = witness(pg, required=True)
    book = genesis(world, w)
    pg.fail = 10 ** 6                                # the project is unreachable (500s)
    book.ensure_baseline("232")                      # a commit is the lineage's own durable write: it is never blocked by the witness
    assert book.park("s1", "232", None, False, False)
    with pytest.raises(CC.BookRefused) as e:
        book.barrier()
    assert e.value.code == "WITNESS_UNAVAILABLE"
    with pytest.raises(CC.BookRefused) as e2:
        CC.CapitalBook.anchored(world.root, account_ref=ACCT, venue="testnet", anchor_dir=world.anchor, witness=w)
    assert e2.value.code == "WITNESS_UNAVAILABLE"
    pg.fail = 0
    assert book.barrier() == "OK", "the next barrier publishes the head the outage left behind"
    assert w.head(ACCT)[0] == len(AN.Lineage(os.path.join(world.anchor, f"lineage-{ACCT}.jsonl"), ACCT, "testnet").read()) - 1


def test_a_new_lineage_is_refused_for_an_account_the_witness_already_holds(pg, world):
    import shutil
    w = witness(pg, required=True)
    genesis(world, w)
    shutil.rmtree(world.anchor)
    shutil.rmtree(world.root)                        # the volume was wiped: locally a brand-new account
    an = AN.Anchor(world.root, ACCT, "testnet", world.anchor, witness=w)
    with pytest.raises(CC.BookRefused) as e:
        CC.CapitalBook(world.root, an).initialise({"test": True})
    assert e.value.code == "GENESIS_REFUSED_WITNESS_HAS_HISTORY"


def test_a_forged_history_that_continues_at_the_same_seq_is_a_fork(pg, world):
    w = witness(pg, required=True)
    book = genesis(world, w)
    book.ensure_baseline("232")
    forged = W.event_id(ACCT, 1)
    pg.rows[forged]["payload"] = {**pg.rows[forged]["payload"], "head_ref": "h" + "f" * 15}     # the witness holds another history at seq 1
    with pytest.raises(CC.BookRefused) as e:
        CC.CapitalBook.anchored(world.root, account_ref=ACCT, venue="testnet", anchor_dir=world.anchor, witness=w)
    assert e.value.code == "LINEAGE_FORKED"


# ---------------------------------------------------------------------------------------------------- configuration
def test_witness_from_env_modes():
    assert isinstance(AN.witness_from_env({}), AN.NoWitness)
    assert isinstance(AN.witness_from_env({"CAPITAL_WITNESS": "off"}), AN.NoWitness)
    with pytest.raises(AN.AnchorRefused) as e:
        AN.witness_from_env({"CAPITAL_WITNESS_REQUIRED": "1"})
    assert e.value.code == "WITNESS_MISCONFIGURED", "a witness that was demanded and not configured is a refusal, never a silent NoWitness"
    with pytest.raises(AN.AnchorRefused) as e2:
        AN.witness_from_env({"CAPITAL_WITNESS": "supabase"})
    assert e2.value.code == "WITNESS_MISCONFIGURED"
    with pytest.raises(AN.AnchorRefused):
        AN.witness_from_env({"CAPITAL_WITNESS": "carrier-pigeon"})
    w = AN.witness_from_env({"CAPITAL_WITNESS": "supabase", "CAPITAL_WITNESS_REQUIRED": "1", "SUPABASE_URL": "https://abcdefghij.supabase.co", "SUPABASE_SERVICE_ROLE_KEY": KEY, "SUPABASE_OWNER_ID": OWNER})
    assert w.configured and w.required and repr(w.client).count(KEY) == 0
