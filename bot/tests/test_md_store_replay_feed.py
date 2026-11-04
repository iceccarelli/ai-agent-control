"""Durable log, deterministic replay, and the live feed's failure handling.

The venue is a scripted fake (deterministic software testing only); the frames
inside it are the real captured ones where possible. Nothing here touches a
network."""
from __future__ import annotations

import json
import os
import random
import sqlite3

import pytest

import md_engine as EN
import md_events as E
import md_feed as F
import md_state as S
import md_store as ST
from md_helpers import FakeClock, Factory, ScriptedWS, book_frame, load_frames, ticker_frame

BOOK = "orderbook.50.BTCUSDT"
TS0 = 1_793_820_000_000


def new_engine(tmp_path, **kw):
    store = ST.MarketStore(str(tmp_path / "md"), commit_every=1)
    return EN.Engine(S.MarketState("BTCUSDT"), store), store


def run_fixture(eng, *, view_every=40):
    """The real captured session through the LIVE path: connection events, every
    frame appended then applied, and a view logged periodically."""
    frames = load_frames()
    t0 = frames[0]["recv_ms"]
    for c in (E.LINEAR, E.SPOT):
        eng.record(ST.CONN, wall_ms=t0, mono_s=0.0, category=c, conn_id="c-" + c,
                   payload=json.dumps({"event": "connected"}))
    last = 0.0
    for i, fr in enumerate(frames):
        mono = (fr["recv_ms"] - t0) / 1000.0
        eng.record(ST.FRAME, wall_ms=fr["recv_ms"], mono_s=mono, category=fr["category"],
                   conn_id="c-" + fr["category"], payload=fr["raw"])
        last = mono
        if i % view_every == 0:
            eng.log_view(mono, fr["recv_ms"])
    eng.log_view(last, frames[-1]["recv_ms"])
    return frames


# -- store ---------------------------------------------------------------------------------------

def test_store_refuses_the_runtime_database_and_tracked_trees(tmp_path):
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for bad in (os.path.join(here, "state"), os.path.join(here, "state", "trading_state.db"),
                os.path.join(here, "artifacts", "md"), here):
        with pytest.raises(ST.StoreError):
            ST.MarketStore(bad)
    ST.MarketStore(str(tmp_path / "ok")).close()


def test_append_read_back_exactly_and_the_chain_verifies(tmp_path):
    store = ST.MarketStore(str(tmp_path / "md"), commit_every=1)
    raw = '{"exact":"text  with   odd spacing","n":1.10}'
    a = store.append(ST.FRAME, wall_ms=TS0, mono_s=1.5, category="linear", conn_id="c", payload=raw)
    store.append(ST.CONN, wall_ms=TS0 + 1, mono_s=1.6, category="linear", payload='{"event":"x"}')
    recs = list(store.iter_records())
    assert [r.id for r in recs] == [1, 2] and recs[0].payload == raw      # byte-exact provenance
    assert recs[0].chain == a.chain and store.verify() == []
    assert store.count() == 2


def test_the_log_is_append_only_and_tampering_is_detected(tmp_path):
    store = ST.MarketStore(str(tmp_path / "md"), commit_every=1)
    for i in range(5):
        store.append(ST.FRAME, wall_ms=TS0 + i, mono_s=float(i), category="spot", payload=f'{{"i":{i}}}')
    seg = os.path.join(store.dir, store.segments()[0])
    conn = sqlite3.connect(seg)
    with pytest.raises(sqlite3.DatabaseError):
        conn.execute("UPDATE log SET payload = x'00' WHERE id = 3")
    with pytest.raises(sqlite3.DatabaseError):
        conn.execute("DELETE FROM log WHERE id = 3")
    # an attacker who drops the trigger is caught by the hash chain instead
    conn.execute("DROP TRIGGER log_no_update")
    conn.execute("UPDATE log SET payload = ? WHERE id = 3", (ST._compress(b'{"i":999}'),))
    conn.commit()
    conn.close()
    problems = store.verify()
    assert any("record 3: chain mismatch" in p for p in problems)


def test_ids_continue_across_reopen_and_utc_day_segments(tmp_path):
    d = str(tmp_path / "md")
    s1 = ST.MarketStore(d, commit_every=1)
    day1 = 1_793_836_700_000                   # 2026-11-04 23:58:20 UTC
    s1.append(ST.FRAME, wall_ms=day1, mono_s=1.0, category="spot", payload="a")
    s1.append(ST.FRAME, wall_ms=day1 + 300_000, mono_s=2.0, category="spot", payload="b")   # next UTC day
    s1.close()
    assert len(s1.segments()) == 2
    s2 = ST.MarketStore(d, commit_every=1)
    r = s2.append(ST.FRAME, wall_ms=day1 + 300_001, mono_s=3.0, category="spot", payload="c")
    assert r.id == 3 and [x.payload for x in s2.iter_records()] == ["a", "b", "c"]
    assert s2.verify() == []


def test_a_preset_dictionary_makes_small_frames_much_smaller(tmp_path):
    store = ST.MarketStore(str(tmp_path / "md"), commit_every=1)
    frames = [fr["raw"] for fr in load_frames()[:300]]
    raw = sum(len(f.encode()) for f in frames)
    packed = sum(len(ST._compress(f.encode())) for f in frames)
    assert packed < 0.4 * raw
    for f in frames[:5]:
        assert ST._decompress(ST._compress(f.encode())).decode() == f


def test_pruning_whole_old_segments_keeps_the_rest_verifiable(tmp_path):
    d = str(tmp_path / "md")
    store = ST.MarketStore(d, commit_every=1)
    day = 86_400_000
    base = 1_793_750_400_000                       # 2026-11-04 00:00 UTC
    for i in range(3):
        for j in range(4):
            store.append(ST.FRAME, wall_ms=base + i * day + j * 1000, mono_s=float(i * 10 + j),
                         category="spot", payload=f'{{"d":{i},"j":{j}}}')
    assert len(store.segments()) == 3 and store.verify() == []
    removed = store.prune_segments_before("20261105")           # only the 4th Nov segment is older
    assert removed == ["md-20261104.db"] and len(store.segments()) == 2
    assert store.verify() == []                                  # checkpointed by pruned.jsonl
    reopened = ST.MarketStore(d, commit_every=1)
    r = reopened.append(ST.FRAME, wall_ms=base + 3 * day, mono_s=99.0, category="spot", payload="{}")
    assert r.id == 13 and reopened.verify() == []
    assert store.prune_segments_before("20250101") == []         # nothing is older
    # `store` is still writing 20261106 and never removes the segment it has open
    assert store.prune_segments_before("29991231") == ["md-20261105.db"]
    assert store.segments() == ["md-20261106.db", "md-20261107.db"] and store.verify() == []


def test_pruning_never_touches_the_newest_segment_and_a_missing_prefix_is_caught(tmp_path):
    d = str(tmp_path / "md")
    store = ST.MarketStore(d, commit_every=1)
    day = 86_400_000
    base = 1_793_750_400_000
    for i in range(3):
        store.append(ST.FRAME, wall_ms=base + i * day, mono_s=float(i), category="spot", payload="{}")
    store.prune_segments_before("29991231")
    assert store.segments() == ["md-20261106.db"]                # the newest is never removed
    assert store.verify() == []
    # a deleted segment with no prune record is a hole, and is reported
    d2 = str(tmp_path / "md2")
    s2 = ST.MarketStore(d2, commit_every=1)
    for i in range(3):
        s2.append(ST.FRAME, wall_ms=base + i * day, mono_s=float(i), category="spot", payload="{}")
    s2.close()
    os.remove(os.path.join(d2, "md-20261105.db"))
    assert any("does not continue" in p or "id gap" in p for p in ST.MarketStore(d2).verify())


def test_unknown_kinds_and_non_text_payloads_are_refused(tmp_path):
    store = ST.MarketStore(str(tmp_path / "md"))
    with pytest.raises(ST.StoreError):
        store.append("mystery", wall_ms=TS0, mono_s=0.0, payload="x")
    with pytest.raises(ST.StoreError):
        store.append(ST.FRAME, wall_ms=TS0, mono_s=0.0, payload=b"bytes")


# -- deterministic replay ------------------------------------------------------------------------------

def test_a_replayed_session_reproduces_every_logged_view_exactly(tmp_path):
    eng, store = new_engine(tmp_path)
    run_fixture(eng)
    live_digest = eng.state.digest()
    rep = EN.replay(store)
    assert rep.records > 800 and rep.frames == 850
    assert rep.views_checked >= 20
    assert rep.view_mismatches == [] and rep.chain_problems == []
    assert rep.deterministic
    assert rep.final_digest == live_digest              # same books, tickers, lags, connectivity


def test_replay_detects_a_session_that_was_altered_after_the_fact(tmp_path):
    eng, store = new_engine(tmp_path)
    run_fixture(eng, view_every=20)
    store.flush()
    seg = os.path.join(store.dir, store.segments()[0])
    conn = sqlite3.connect(seg)
    conn.execute("DROP TRIGGER log_no_update")
    row = conn.execute("SELECT id, payload FROM log WHERE kind='frame' AND topic='' "
                       "AND id > 400 ORDER BY id LIMIT 1").fetchone()
    raw = ST._decompress(row[1]).decode()
    forged = raw.replace('"b":[[', '"b":[["1.0","999"],[', 1) if '"b":[[' in raw else raw + " "
    conn.execute("UPDATE log SET payload=? WHERE id=?", (ST._compress(forged.encode()), row[0]))
    conn.commit()
    conn.close()
    rep = EN.replay(store)
    assert not rep.deterministic          # the chain no longer verifies, whatever the book did


def test_two_independent_replays_agree(tmp_path):
    eng, store = new_engine(tmp_path)
    run_fixture(eng)
    a, b = EN.replay(store), EN.replay(store)
    assert a.final_digest == b.final_digest and a.rejections == b.rejections


def test_rest_exchanges_are_part_of_the_replayed_state(tmp_path):
    eng, store = new_engine(tmp_path)
    run_fixture(eng)
    body = json.dumps({"retCode": 0, "retMsg": "OK", "result": {"category": "linear", "list": [{
        "symbol": "BTCUSDT", "status": "Trading", "baseCoin": "BTC", "quoteCoin": "USDT",
        "priceFilter": {"tickSize": "0.10"},
        "lotSizeFilter": {"qtyStep": "0.001", "minOrderQty": "0.001", "maxOrderQty": "1500.000",
                          "maxMktOrderQty": "150.000", "minNotionalValue": "5"},
        "fundingInterval": 480, "upperFundingRate": "0.00333", "lowerFundingRate": "-0.00333"}]},
        "time": TS0})
    eng.record(ST.REST, wall_ms=TS0, mono_s=99.0, category=E.LINEAR, conn_id="rest",
               topic="/v5/market/instruments-info",
               payload=json.dumps({"endpoint": "/v5/market/instruments-info",
                                   "params": {"category": "linear", "symbol": "BTCUSDT"},
                                   "http_status": 200, "body": body}))
    assert eng.state.specs[E.LINEAR].tick_size == 0.1
    assert eng.state.books[E.LINEAR].tick_size == 0.1
    assert EN.replay(store).final_digest == eng.state.digest()


# -- the live feed's failure handling (scripted venue, fake clock) ----------------------------------------------

def make_feed(tmp_path, scripts, cfg=None, clock=None, seed=1):
    clock = clock or FakeClock()
    eng, store = new_engine(tmp_path)
    cfg = cfg or F.FeedConfig(ping_interval_s=20, pong_timeout_s=10, silence_timeout_s=15,
                              resync_timeout_s=5, backoff_base_s=1, backoff_max_s=8, recv_timeout_s=0.5)
    factory = Factory(*scripts)
    w = F.StreamWorker(E.LINEAR, cfg, eng, factory, mono=clock.mono, wall_ms=clock.wall_ms,
                       rng=random.Random(seed), sleep=clock.sleep)
    return w, eng, store, factory, clock


def snap_frame(clock, u=100, seq=1000, bids=((100.0, 1.0),), asks=((101.0, 1.0),)):
    return ("text", book_frame(BOOK, "snapshot", clock.w, bids, asks, u, seq))


def test_connect_subscribes_in_chunks_and_applies_frames(tmp_path):
    clock = FakeClock()
    ws = ScriptedWS([snap_frame(clock)])
    w, eng, store, factory, _ = make_feed(tmp_path, [ws], clock=clock)
    w.step()                                           # connect + subscribe
    assert factory.urls == ["wss://stream.bybit.com/v5/public/linear"]
    subs = [m for m in ws.sent if m["op"] == "subscribe"]
    assert subs and all(len(m["args"]) <= F.SUBSCRIBE_CHUNK for m in subs)
    assert set(a for m in subs for a in m["args"]) == {BOOK, "tickers.BTCUSDT", "publicTrade.BTCUSDT"}
    w.step()
    assert eng.state.connected[E.LINEAR] and eng.state.books[E.LINEAR].synced
    kinds = [r.kind for r in store.iter_records()]
    assert kinds[0] == ST.CONN and ST.FRAME in kinds


def test_a_dropped_connection_invalidates_backs_off_and_resubscribes(tmp_path):
    clock = FakeClock()
    first = ScriptedWS([snap_frame(clock), ("raise", OSError("connection reset"))])
    second = ScriptedWS([])
    w, eng, store, factory, _ = make_feed(tmp_path, [first, second], clock=clock)
    w.step(); w.step()
    assert eng.state.books[E.LINEAR].synced
    w.step()                                           # the reset
    assert not eng.state.connected[E.LINEAR] and not eng.state.books[E.LINEAR].synced
    assert first.closed and w.ws is None
    # no immediate reconnect: it waits out the backoff
    n_made = len(factory.made)
    w.step()
    assert len(factory.made) == n_made
    clock.advance(2.0)
    second.push(snap_frame(clock, u=500, seq=5000))
    w.step()                                           # reconnect + resubscribe
    assert len(factory.made) == n_made + 1 and any(m["op"] == "subscribe" for m in second.sent)
    assert eng.state.connected[E.LINEAR] and not eng.state.books[E.LINEAR].synced   # not trusted yet
    w.step()
    assert eng.state.books[E.LINEAR].synced and eng.state.books[E.LINEAR].update_id == 500
    incidents = [json.loads(r.payload)["event"] for r in store.iter_records(kinds=(ST.CONN,))]
    assert incidents == ["connected", "disconnected", "connected"]


def test_backoff_grows_then_resets_after_a_stable_connection(tmp_path):
    clock = FakeClock()
    fails = [ScriptedWS(fail_connect=ConnectionRefusedError("no")) for _ in range(4)]
    w, eng, store, factory, _ = make_feed(tmp_path, fails + [ScriptedWS([])], clock=clock)
    delays = []
    for _ in range(4):
        w.step()
        delays.append(w.next_attempt - clock.m)
        clock.advance(w.next_attempt - clock.m + 0.01)
    assert delays[0] < delays[1] < delays[2] <= delays[3] * 2 and max(delays) <= 8.0
    assert all(d > 0 for d in delays)
    w.step()                                           # the fifth attempt connects
    assert w.ws is not None
    clock.advance(61.0)
    w.engine.last_pong_mono[E.LINEAR] = clock.m
    w.ws.push(("raise", OSError("reset after a long healthy run")))
    w.step()
    assert w.failures == 1                             # healthy for > stable_after_s: counter reset


def test_a_sequence_gap_triggers_a_resubscribe_and_the_book_recovers(tmp_path):
    clock = FakeClock()
    ws = ScriptedWS([snap_frame(clock),
                     ("text", book_frame(BOOK, "delta", clock.w, [(100.0, 2.0)], [], 101, 1001)),
                     ("text", book_frame(BOOK, "delta", clock.w, [(100.0, 3.0)], [], 105, 1005))])
    w, eng, store, factory, _ = make_feed(tmp_path, [ws], clock=clock)
    for _ in range(4):
        w.step()
    assert not eng.state.books[E.LINEAR].synced
    ops = [(m["op"], tuple(m["args"])) for m in ws.sent]
    assert ("unsubscribe", (BOOK,)) in ops and ops.count(("subscribe", (BOOK,))) >= 1
    assert w.resync_since is not None
    ws.push(snap_frame(clock, u=300, seq=3000))
    w.step()
    assert eng.state.books[E.LINEAR].synced and w.resync_since is None
    assert E.SEQUENCE_GAP in [i.kind for i in eng.state.incidents]


def test_a_resync_that_gets_no_snapshot_forces_a_reconnect(tmp_path):
    clock = FakeClock()
    ws = ScriptedWS([snap_frame(clock),
                     ("text", book_frame(BOOK, "delta", clock.w, [(100.0, 3.0)], [], 150, 1500))])
    w, eng, store, factory, _ = make_feed(tmp_path, [ws, ScriptedWS([])], clock=clock)
    for _ in range(3):
        w.step()
    assert w.resync_since is not None and w.ws is ws
    clock.advance(6.0)
    w.engine.last_pong_mono[E.LINEAR] = clock.m
    w.step()
    assert w.ws is None and ws.closed
    assert "RESYNC_TIMEOUT" in [json.loads(r.payload).get("reason", "")
                                for r in store.iter_records(kinds=(ST.CONN,))][-1]


def test_a_missing_pong_is_a_lost_heartbeat(tmp_path):
    clock = FakeClock()
    ws = ScriptedWS([snap_frame(clock)], auto_pong=False)
    w, eng, store, factory, _ = make_feed(tmp_path, [ws], clock=clock)
    w.step(); w.step()
    for _ in range(40):
        clock.advance(1.0)
        ws.push(("text", ticker_frame("delta", clock.w, markPrice="80000")))     # data keeps flowing
        w.step()
        if w.ws is None:
            break
    assert w.ws is None
    reasons = [json.loads(r.payload).get("reason") for r in store.iter_records(kinds=(ST.CONN,))]
    assert any("HEARTBEAT_LOST" in (x or "") for x in reasons)


def test_total_silence_is_a_stale_feed(tmp_path):
    clock = FakeClock()
    ws = ScriptedWS([snap_frame(clock)])
    w, eng, store, factory, _ = make_feed(tmp_path, [ws], clock=clock)
    w.step(); w.step()
    for _ in range(30):
        clock.advance(1.0)
        w.engine.last_pong_mono[E.LINEAR] = clock.m          # pongs alone are not data...
        w.step()
        if w.ws is None:
            break
    assert w.ws is None
    reasons = [json.loads(r.payload).get("reason") for r in store.iter_records(kinds=(ST.CONN,))]
    assert any("STALE_FEED" in (x or "") for x in reasons)


def test_a_failed_subscribe_is_an_incident(tmp_path):
    clock = FakeClock()
    ws = ScriptedWS([("text", json.dumps({"success": False, "ret_msg": "error:handler not found",
                                          "op": "subscribe"}))])
    w, eng, store, factory, _ = make_feed(tmp_path, [ws], clock=clock)
    w.step(); w.step()
    assert eng.sub_failures == ["error:handler not found"]


def test_garbage_frames_are_counted_and_do_not_stop_the_worker(tmp_path):
    clock = FakeClock()
    ws = ScriptedWS([snap_frame(clock), ("text", "}{ not json"), ("text", "[]"),
                     ("text", book_frame(BOOK, "delta", clock.w, [(100.0, 5.0)], [], 101, 1001))])
    w, eng, store, factory, _ = make_feed(tmp_path, [ws], clock=clock)
    for _ in range(6):
        w.step()
    assert eng.state.rejections == {E.NOT_JSON: 1, E.NOT_AN_OBJECT: 1}
    assert eng.state.books[E.LINEAR].update_id == 101 and w.ws is ws


def test_the_worker_outlives_an_unexpected_exception(tmp_path):
    clock = FakeClock()
    ws = ScriptedWS([("raise", RuntimeError("bug in the transport"))])
    w, eng, store, factory, _ = make_feed(tmp_path, [ws, ScriptedWS([])], clock=clock)
    w.step()
    with pytest.raises(RuntimeError):
        w.step()                                  # step() lets it out ...
    w.stop.set()
    w.run()                                       # ... run() swallows it, drops the socket and returns
    assert w.ws is None
