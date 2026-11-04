"""Regression tests for the independent adversarial review of the market-data
pipeline. Each test reproduces the reviewer's failing scenario."""
from __future__ import annotations

import json
import os
import sqlite3
import threading

import pytest

import md_engine as EN
import md_events as E
import md_feed as F
import md_rest as R
import md_state as S
import md_store as ST
import md_transport as T
import md_wire as W
import ws_transport as WT
from md_helpers import FakeClock, Factory, ScriptedWS, book_frame, load_frames, load_study, ticker_frame
from test_md_integration import bare_bot, broker_for, complete_state
from test_md_store_replay_feed import BOOK, TS0, make_feed, new_engine, run_fixture, snap_frame
from test_md_wire_book_state import CTX, feed_fixture

BOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# -- H1: the settled funding print must keep being refreshed -------------------------------------------

class FakeVenueRest:
    """Answers the public endpoints the way the venue would, with settlements every 8h."""

    def __init__(self, clock):
        self.clock, self.calls = clock, {}

    def __call__(self, url, timeout):
        from urllib.parse import parse_qs, urlsplit
        u = urlsplit(url)
        self.calls[u.path] = self.calls.get(u.path, 0) + 1
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        wall = self.clock.w
        if u.path == "/v5/market/time":
            body = {"retCode": 0, "result": {"timeSecond": str(wall // 1000), "timeNano": str(wall * 1_000_000)}}
        elif u.path == "/v5/market/instruments-info":
            body = load_study("instrument_linear.json.gz" if q["category"] == "linear" else "instrument_spot.json.gz")
        elif u.path == "/v5/market/funding/history":
            last = wall - wall % (8 * 3600 * 1000)
            body = {"retCode": 0, "result": {"category": "linear", "list": [
                {"symbol": "BTCUSDT", "fundingRate": "0.0001", "fundingRateTimestamp": str(last - i * 8 * 3600 * 1000)}
                for i in range(5)]}}
        elif u.path == "/v5/market/orderbook":
            body = {"retCode": 0, "result": {"s": "BTCUSDT", "b": [["100.0", "1"]], "a": [["100.1", "1"]], "ts": wall}}
        else:
            raise AssertionError(u.path)
        return 200, json.dumps(body), {}


def test_the_settled_print_and_the_instrument_rules_keep_being_refreshed_for_days():
    clock = FakeClock(wall_ms=1_793_750_400_000 + 3 * 3600 * 1000)          # 03:00 UTC
    eng = EN.Engine(S.MarketState("BTCUSDT"))
    sink = []
    eng.record = lambda kind, **kw: sink.append((kind, kw)) or eng.apply(  # type: ignore[assignment]
        ST.Record(len(sink), kind, kw["wall_ms"](), kw["mono_s"](), kw.get("category", ""),
                  kw.get("conn_id", ""), kw.get("topic", ""), kw["payload"]))
    net = FakeVenueRest(clock)
    client = R.PublicRest(net, clock=clock.mono, sleep=clock.sleep, rate_per_s=1000, burst=1000)
    w = F.RestWorker(F.FeedConfig(), eng, client, mono=clock.mono, wall_ms=clock.wall_ms, sleep=clock.sleep)
    prints_seen = set()
    for _ in range(3 * 24 * 120):                                            # 3 days in 30 s steps
        w.step()
        if eng.state.last_print:
            prints_seen.add(eng.state.last_print.settled_ms)
        clock.advance(30.0)
    assert net.calls["/v5/market/funding/history"] >= 3 * 3 - 1            # about one per settlement
    assert len(prints_seen) >= 9                                           # the newest print kept advancing
    assert clock.w - eng.state.last_print.settled_ms < 8 * 3600 * 1000 + 120_000     # never a missed settlement
    assert net.calls["/v5/market/instruments-info"] >= 2 * 70              # hourly, both products
    v = eng.state.view(clock.m, clock.w)
    assert "funding_print:NO_SETTLED_PRINT" not in v.refusals(S.ENTRY)


# -- H2: a demo engine is fed MAINNET data --------------------------------------------------------------------

@pytest.mark.parametrize("venue,testnet,expected", [("demo", True, "mainnet"), ("testnet", True, "testnet"),
                                                     ("mainnet", False, "mainnet"), (None, True, "testnet"),
                                                     (None, False, "mainnet"), ("weird", True, "testnet")])
def test_the_feed_environment_follows_the_venue_not_the_sandbox_flag(tmp_path, venue, testnet, expected):
    bot, _ = bare_bot(tmp_path, testnet=testnet)
    if venue is not None:
        bot.cfg.BYBIT_VENUE = venue
    assert bot._market_feed_environment() == expected


# -- M2: a worker outlives a failing log and a very long outage ----------------------------------------------------

class BrokenStore:
    environment = "mainnet"

    def __init__(self, inner):
        self.inner, self.fail = inner, False
        self._next_id = 1

    def append(self, *a, **kw):
        if self.fail:
            raise sqlite3.OperationalError("database or disk is full")
        return self.inner.append(*a, **kw)

    def flush(self):
        pass

    def __getattr__(self, name):
        return getattr(self.inner, name)


def test_a_full_disk_stops_entries_but_not_the_worker_and_recovery_is_replayable(tmp_path):
    clock = FakeClock()
    inner = ST.MarketStore(str(tmp_path / "md"), commit_every=1, environment="mainnet")
    store = BrokenStore(inner)
    eng = EN.Engine(S.MarketState("BTCUSDT"), store)
    cfg = F.FeedConfig(book_silence_s=1000)
    sockets = [ScriptedWS([snap_frame(clock)]), ScriptedWS([snap_frame(clock, u=500, seq=5000)])]
    w = F.StreamWorker(E.LINEAR, cfg, eng, Factory(*sockets), mono=clock.mono, wall_ms=clock.wall_ms,
                       sleep=clock.sleep, rng=__import__("random").Random(1))
    w.step(); w.step()
    assert eng.state.books[E.LINEAR].synced and eng.state.store_fault is None
    store.fail = True
    sockets[0].push(("raise", OSError("reset")))
    w.step()                                           # the drop itself must log, and the log is down
    assert w.ws is None and not eng.state.connected[E.LINEAR]
    assert eng.state.store_fault and "disk is full" in eng.state.store_fault
    assert w.ws is None                                  # the drop completed despite the dead log
    v = eng.state.view(clock.m, clock.w)
    assert any(r.startswith("store:WRITE_FAILED") for r in v.refusals(S.ENTRY))
    assert not any(r.startswith("store") for r in v.refusals(S.MONITOR))   # management is untouched
    clock.advance(70.0)
    w.step()                                           # reconnect while still failing: no exception
    assert w.ws is not None
    store.fail = False
    w.step()                                           # a frame arrives and the log is writable again
    assert eng.state.store_fault is None
    kinds = [json.loads(r.payload).get("event") for r in inner.iter_records(kinds=(ST.CONN,))]
    assert "store_gap" in kinds                                      # the unlogged span is marked
    eng.log_view(clock.m, clock.w)
    rep = EN.replay(inner)
    assert rep.deterministic and rep.view_mismatches == []


def test_the_backoff_is_finite_however_long_the_venue_is_down():
    import random
    cfg = F.FeedConfig()
    for failures in (1, 5, 17, 1024, 1025, 10**6):
        d = F._backoff(cfg, failures, random.Random(1))
        assert 0 < d <= cfg.backoff_max_s
    clock = FakeClock()
    eng, store = new_engine(_tmp())
    w = F.StreamWorker(E.LINEAR, cfg, eng, Factory(ScriptedWS(fail_connect=OSError("blocked"))),
                       mono=clock.mono, wall_ms=clock.wall_ms, sleep=clock.sleep)
    w.failures = 5000
    w.step()
    assert w.failures == 5001 and w.next_attempt > clock.m


def _tmp():
    import tempfile, pathlib
    return pathlib.Path(tempfile.mkdtemp())


# -- M3: replay across a process restart --------------------------------------------------------------------------------

def test_a_store_that_spans_a_restart_replays_exactly_as_it_ran(tmp_path):
    d = str(tmp_path / "md")
    store = ST.MarketStore(d, commit_every=1, environment="mainnet")
    eng = EN.Engine(S.MarketState("BTCUSDT"), store)
    eng.start_session(lambda: TS0, lambda: 1.0)
    frames = load_frames()
    t0 = frames[0]["recv_ms"]
    for c in (E.LINEAR, E.SPOT):
        eng.record(ST.CONN, wall_ms=t0, mono_s=0.0, category=c, conn_id="c-" + c, payload=json.dumps({"event": "connected"}))
    for fr in frames[:200]:
        eng.record(ST.FRAME, wall_ms=fr["recv_ms"], mono_s=(fr["recv_ms"] - t0) / 1000.0, category=fr["category"],
                   conn_id="c-" + fr["category"], payload=fr["raw"])
    eng.record(ST.REST, wall_ms=t0 + 1000, mono_s=1.0, category="linear", conn_id="rest",
               topic="/v5/market/instruments-info",
               payload=json.dumps({"endpoint": "/v5/market/instruments-info", "params": {"category": "linear", "symbol": "BTCUSDT"},
                                   "http_status": 200, "body": json.dumps(load_study("instrument_linear.json.gz"))}))
    assert eng.state.specs
    eng.log_view(2.0, t0 + 2000)
    store.close()
    # --- the process restarts: a fresh engine/state over the same store ---
    store2 = ST.MarketStore(d, commit_every=1, environment="mainnet")
    eng2 = EN.Engine(S.MarketState("BTCUSDT"), store2)
    eng2.start_session(lambda: t0 + 3000, lambda: 3.0)
    live = eng2.log_view(3.5, t0 + 3500)
    assert any(r.startswith("instruments:NO_SPEC") for r in live.refusals(S.ENTRY))     # nothing carried over
    rep = EN.replay(store2)
    assert rep.deterministic, rep.view_mismatches


def test_the_policy_symbol_depth_and_environment_travel_in_the_log(tmp_path):
    store = ST.MarketStore(str(tmp_path / "md"), commit_every=1, environment="testnet")
    policy = S.MarketPolicy(book_max_age_s=1.5, max_leg_span_s=2.0)
    eng = EN.Engine(S.MarketState("BTCUSDT", policy, 50, "testnet"), store)
    eng.start_session(lambda: TS0, lambda: 1.0)
    rec = next(store.iter_records(kinds=(ST.CONN,)))
    info = json.loads(rec.payload)
    assert info["event"] == "session_start" and info["environment"] == "testnet"
    assert info["policy"]["book_max_age_s"] == 1.5 and info["depth"] == 50
    rep = EN.replay(store, policy=S.MarketPolicy())                  # a caller's default policy is overridden
    # the replayed state took its parameters from the log
    eng2 = EN.Engine(S.MarketState("BTCUSDT"))
    eng2.apply(rec)
    assert eng2.state.policy.book_max_age_s == 1.5 and eng2.state.environment == "testnet"
    assert rep.deterministic


# -- M4: clocks sampled inside the lock; a stepped wall clock cannot interleave segments ---------------------------------

def test_a_wall_clock_stepped_back_across_midnight_cannot_split_the_chain(tmp_path):
    store = ST.MarketStore(str(tmp_path / "md"), commit_every=1, environment="mainnet")
    mid = 1_793_836_800_000                                          # 2026-11-05 00:00 UTC
    for w in (mid - 5, mid + 1, mid - 1, mid + 2, mid + 3):
        store.append(ST.FRAME, wall_ms=w, mono_s=float(w % 1000), category="spot", payload="{}")
    store.close()
    s2 = ST.MarketStore(str(tmp_path / "md"), environment="mainnet")
    assert [r.id for r in s2.iter_records()] == [1, 2, 3, 4, 5]
    assert s2.verify() == []
    assert s2.segments() == ["md-20261104.db", "md-20261105.db"]


def test_concurrent_writers_keep_the_log_ordered_and_verifiable(tmp_path):
    eng, store = new_engine(tmp_path)
    clock = FakeClock()
    def hammer(cat):
        for i in range(60):
            eng.record(ST.CONN, wall_ms=clock.wall_ms, mono_s=clock.mono, category=cat, conn_id=cat,
                       payload=json.dumps({"event": "connected" if i % 2 == 0 else "disconnected"}))
            clock.advance(0.01)
    threads = [threading.Thread(target=hammer, args=(c,)) for c in (E.LINEAR, E.SPOT, E.LINEAR, E.SPOT)]
    [t.start() for t in threads]; [t.join() for t in threads]
    recs = list(store.iter_records())
    assert [r.id for r in recs] == list(range(1, 241))
    assert all(a.mono_s <= b.mono_s for a, b in zip(recs, recs[1:]))          # sampled under the lock
    assert store.verify() == [] and EN.replay(store).deterministic


# -- M5: a spot outage must not blind the management of the perp ----------------------------------------------------------------

def test_spot_trouble_never_blocks_monitoring_but_does_block_entries():
    st, now, wall = complete_state()
    st.on_disconnected(E.SPOT)
    v = st.view(now, wall)
    assert v.refusals(S.MONITOR) == []
    assert "spot_connection:DISCONNECTED" in v.refusals(S.ENTRY)
    b, _ = broker_for(st, now, wall)
    mark = b.get_mark("BTCUSDT")                                     # a held pair is still viewable
    assert b.get_spot_mark("BTCUSDT") == v.perp_index                 # the observed index stands in
    assert b.snapshot_detail()["spot_reference"] == "PERP_INDEX_FALLBACK"
    assert "spot_connection:DISCONNECTED" in b.snapshot_detail()["entry_refusals"]


def test_a_stale_spot_book_alone_keeps_the_held_pair_viewable(frozen=None):
    st, now, wall = complete_state()
    # keep the perp side fresh, let only the spot book age out
    ts = wall + 5000
    st.ingest(W.parse_frame(book_frame(BOOK, "delta", ts, [], [], st.books[E.LINEAR].update_id + 1,
                                       st.books[E.LINEAR].seq + 1),
                            W.FrameContext(9, E.LINEAR, "c", ts, now + 5.0)), category=E.LINEAR)
    st.ingest(W.parse_frame(ticker_frame("delta", ts, markPrice="80001", indexPrice="80020"),
                            W.FrameContext(10, E.LINEAR, "c", ts, now + 5.0)), category=E.LINEAR)
    v = st.view(now + 5.0, ts)
    assert v.refusals(S.MONITOR) == [] and any(r.startswith("spot_book:STALE") for r in v.refusals(S.ENTRY))
    b, _ = broker_for(st, now + 5.0, ts)
    b.get_mark("BTCUSDT")
    assert b.get_spot_mark("BTCUSDT") == pytest.approx(v.perp_index)


# -- M6: each moving price must be stated recently --------------------------------------------------------------------------------

def test_unrelated_ticker_deltas_do_not_keep_a_frozen_mark_alive():
    st, now, wall = complete_state()
    t = now
    for i in range(1, 13):
        t = now + i
        st.ingest(W.parse_frame(ticker_frame("delta", wall + i * 1000, lastPrice=str(80000 + i), openInterest="67000"),
                                W.FrameContext(100 + i, E.LINEAR, "c", wall + i * 1000, t)), category=E.LINEAR)
        st.ingest(W.parse_frame(book_frame(BOOK, "delta", wall + i * 1000, [], [], st.books[E.LINEAR].update_id + 1,
                                           st.books[E.LINEAR].seq + 1),
                                W.FrameContext(200 + i, E.LINEAR, "c", wall + i * 1000, t)), category=E.LINEAR)
    v = st.view(t, wall + 12_000)
    status = v.status("perp_ticker")
    assert not status.ok and "markPrice" in status.reason and v.perp_mark is None


# -- M7: a backlog of old frames is not a fresh book ---------------------------------------------------------------------------------

def test_a_burst_of_old_venue_frames_is_stale_even_though_it_just_arrived():
    st, now, wall = complete_state()
    for i in range(1, 31):                                           # delivered now, stamped 20 s ago
        recv = wall + i
        st.ingest(W.parse_frame(book_frame(BOOK, "delta", recv - 20_000, [], [], st.books[E.LINEAR].update_id + 1,
                                           st.books[E.LINEAR].seq + 1),
                                W.FrameContext(300 + i, E.LINEAR, "c", recv, now + i * 0.001)), category=E.LINEAR)
    v = st.view(now + 0.05, wall + 50)
    s = v.status("perp_book")
    assert not s.ok and s.reason.startswith("STALE_VENUE_TIME"), s
    assert v.perp_bid is None


# -- M8: instrument rules age out ------------------------------------------------------------------------------------------------------

def test_instrument_rules_older_than_a_day_are_not_current():
    st, now, wall = complete_state()
    spec = st.specs[E.LINEAR]
    assert st.specs_mono                                             # the arrival time was kept
    v = st.view(now + 2.0, wall + 2000)
    assert "instruments:SPEC_OLD:perp,spot" not in v.refusals(S.ENTRY)
    st.specs_mono = {c: t - 90_000 for c, t in st.specs_mono.items()}
    v = st.view(now, wall)
    assert any(r.startswith("instruments:SPEC_OLD") for r in v.refusals(S.ENTRY))


# -- M9: a dead order-book topic is healed even while the socket looks busy -------------------------------------------------------------

def test_a_silent_book_topic_is_resubscribed_then_the_connection_is_replaced(tmp_path):
    clock = FakeClock()
    ws = ScriptedWS([snap_frame(clock)])
    w, eng, store, factory, _ = make_feed(tmp_path, [ws, ScriptedWS([])], clock=clock)
    w.step(); w.step()
    def tick(n):
        for _ in range(n):
            clock.advance(1.0)
            ws.push(("text", ticker_frame("delta", clock.w, markPrice="80000", indexPrice="80010")))
            w.step()
    tick(11)                                                          # tickers flow, the book is silent
    assert [m for m in ws.sent if m["op"] == "unsubscribe"]            # first the cheap cure
    assert w.ws is ws
    tick(12)
    assert ws.closed and len(factory.made) == 2 and w.ws is not ws      # then a new connection
    reasons = [json.loads(r.payload).get("reason") for r in store.iter_records(kinds=(ST.CONN,))]
    assert any("RESYNC_TIMEOUT" in (x or "") or "BOOK_TOPIC_SILENT" in (x or "") for x in reasons)


# -- M10: the store cannot be aimed at the source tree by any route ---------------------------------------------------------------------

def test_symlinks_and_the_whole_repository_tree_are_refused(tmp_path):
    link = tmp_path / "link"
    link.symlink_to(os.path.join(BOT, "tests"))
    repo = os.path.dirname(BOT)
    before = set(os.listdir(os.path.join(BOT, "tests")))
    for target in (str(link), os.path.join(repo, "docs", "md"), os.path.join(repo, "md_store_x"),
                   os.path.join(BOT, "..", "md_y"), os.path.join(BOT, "tests", "x")):
        with pytest.raises(ST.StoreError):
            ST.MarketStore(target, environment="mainnet")
    assert set(os.listdir(os.path.join(BOT, "tests"))) == before            # nothing was created
    assert not os.path.exists(os.path.join(repo, "docs", "md")) and not os.path.exists(os.path.join(repo, "md_y"))


# -- L1/L2: read-only handles cannot write; one writer per directory --------------------------------------------------------------------------

def test_a_handle_opened_without_an_environment_is_read_only(tmp_path):
    d = str(tmp_path / "md")
    ST.MarketStore(d, environment="mainnet").close()
    ro = ST.MarketStore(d)
    assert ro.environment == "mainnet"
    with pytest.raises(ST.StoreError):
        ro.append(ST.FRAME, wall_ms=TS0, mono_s=0.0, payload="{}")


def test_a_second_writer_is_refused_but_readers_are_not(tmp_path):
    d = str(tmp_path / "md")
    w1 = ST.MarketStore(d, commit_every=1, environment="mainnet")
    w1.append(ST.FRAME, wall_ms=TS0, mono_s=0.0, payload="{}")
    with pytest.raises(ST.StoreError, match="already has a writer"):
        ST.MarketStore(d, environment="mainnet")
    assert [r.id for r in ST.MarketStore(d).iter_records()] == [1]
    w1.close()
    ST.MarketStore(d, environment="mainnet").close()                 # released on close


# -- L3: a rejected book frame is an incident, not a silent skip ---------------------------------------------------------------------------------

def test_a_rejected_book_delta_unsyncs_the_book_and_asks_for_a_resync():
    st = S.MarketState("BTCUSDT")
    st.on_connected(E.LINEAR)
    st.ingest(W.parse_frame(book_frame(BOOK, "snapshot", TS0 - 1, [(100.0, 1)], [(101.0, 1)], 10, 100), CTX), category=E.LINEAR)
    assert st.books[E.LINEAR].synced
    bad = json.dumps({"topic": BOOK, "type": "delta", "ts": TS0, "data": {"s": "BTCUSDT", "b": [["NaN", "1"]],
                                                                          "a": [["100.5", "1"]], "u": 11, "seq": 101}})
    inc = st.ingest(W.parse_frame(bad, CTX), category=E.LINEAR)
    assert not st.books[E.LINEAR].synced and st.books[E.LINEAR].unsynced_reason == "REJECTED_FRAME"
    assert [i.kind for i in inc] == [E.RESYNC_REQUESTED]


# -- L4: a failed subscribe does not leak a socket ------------------------------------------------------------------------------------------------

def test_a_subscribe_that_fails_closes_the_socket(tmp_path):
    class Flaky(ScriptedWS):
        def send_text(self, text):
            raise WT.WSClosed("send failed")
    bad = Flaky()
    w, eng, store, factory, clock = make_feed(tmp_path, [bad, ScriptedWS([])])
    w.step()
    assert bad.closed and w.ws is None and not eng.state.connected[E.LINEAR]


# -- L5: fragments ----------------------------------------------------------------------------------------------------------------------------------------

import socket
from test_md_rest_transport import frame


def test_a_stall_between_fragments_closes_the_connection_instead_of_idling(monkeypatch):
    srv, cli = socket.socketpair()
    ws = T.PublicWebSocket("wss://x/y")
    ws._sock = cli
    ws.MID_FRAME_TIMEOUT_S = 0.05
    srv.sendall(frame(T.OP_TEXT, b"ab", fin=False))
    with pytest.raises(WT.WSClosed):
        ws.recv_message(timeout=1)
    srv.close(); cli.close()


def test_control_frames_inside_a_fragmented_message_are_validated_and_fragments_are_capped():
    srv, cli = socket.socketpair()
    ws = T.PublicWebSocket("wss://x/y")
    ws._sock = cli
    srv.sendall(frame(T.OP_TEXT, b"ab", fin=False) + frame(T.OP_PING, b"x" * 200, fin=False)[:2] + b"\x00" * 0)
    srv.close()
    cli2_srv, cli2 = socket.socketpair()
    ws2 = T.PublicWebSocket("wss://x/y")
    ws2._sock = cli2
    ws2.MAX_FRAGMENTS = 5
    cli2_srv.sendall(frame(T.OP_TEXT, b"a", fin=False) + frame(T.OP_CONT, b"", fin=False) * 10 + frame(T.OP_CONT, b"z"))
    with pytest.raises(WT.WSProtocolError, match="too many fragments"):
        ws2.recv_message(timeout=1)
    cli2_srv.close(); cli2.close(); cli.close()


# -- L7: binary frames are not traffic ---------------------------------------------------------------------------------------------------------------------

def test_binary_frames_do_not_keep_a_dead_feed_alive(tmp_path):
    clock = FakeClock()
    ws = ScriptedWS([snap_frame(clock)], auto_pong=False)
    w, eng, store, factory, _ = make_feed(tmp_path, [ws, ScriptedWS([])], clock=clock)
    w.step(); w.step()
    for _ in range(40):
        clock.advance(1.0)
        w.engine.last_pong_mono[E.LINEAR] = clock.m
        ws.push(("binary", b"\x00\x01"))
        w.step()
        if w.ws is None:
            break
    assert w.ws is None and w.binary_frames > 5
    reasons = [json.loads(r.payload).get("reason") for r in store.iter_records(kinds=(ST.CONN,))]
    assert any("STALE_FEED" in (x or "") for x in reasons)
