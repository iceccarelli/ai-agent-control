"""The market-data feed's storage policy: a free-space reserve and bounded retention.

Measured: ~200 bytes per input and ~1.5 GB per day on mainnet BTCUSDT (depth 50, both
products). The feed shares its volume with the trading ledger on a one-volume
deployment, so these tests pin that the feed gives up writing BEFORE the disk is
full, that the failure is the already-defined one (entries stop, management of a held
position does not), that it recovers on its own with a marked gap, and that retention
removes whole old days without breaking the chain of the rest.
"""
from __future__ import annotations

import json
import os

import pytest

import md_engine as EN
import md_events as E
import md_feed as F
import md_state as S
import md_store as ST
from md_helpers import FakeClock, Factory, ScriptedWS
from test_md_store_replay_feed import snap_frame

DAY = 86_400_000
BASE = 1_793_750_400_000                      # 2026-11-04 00:00 UTC


def test_a_store_below_its_reserve_refuses_to_write_and_says_why(tmp_path, monkeypatch):
    store = ST.MarketStore(str(tmp_path / "md"), commit_every=1, environment="mainnet",
                           min_free_bytes=500 * 1024 * 1024)
    monkeypatch.setattr(store, "free_bytes", lambda: 100 * 1024 * 1024)
    with pytest.raises(ST.StoreError, match="reserve"):
        store.append(ST.FRAME, wall_ms=BASE, mono_s=1.0, category="spot", payload="{}")
    assert store.count() == 0                               # nothing half-written


def test_an_unreadable_volume_is_a_refusal_not_a_guess_that_there_is_room(tmp_path, monkeypatch):
    store = ST.MarketStore(str(tmp_path / "md"), commit_every=1, environment="mainnet", min_free_bytes=1)

    def boom():
        raise OSError("statvfs failed")
    monkeypatch.setattr(store, "free_bytes", boom)
    with pytest.raises(ST.StoreError):
        store.append(ST.FRAME, wall_ms=BASE, mono_s=1.0, category="spot", payload="{}")


def test_no_reserve_means_no_check(tmp_path, monkeypatch):
    store = ST.MarketStore(str(tmp_path / "md"), commit_every=1, environment="mainnet")
    monkeypatch.setattr(store, "free_bytes", lambda: 0)
    store.append(ST.FRAME, wall_ms=BASE, mono_s=1.0, category="spot", payload="{}")
    assert store.count() == 1


def test_running_into_the_reserve_stops_entries_not_management_and_recovers_with_a_gap(tmp_path, monkeypatch):
    clock = FakeClock()
    store = ST.MarketStore(str(tmp_path / "md"), commit_every=1, environment="mainnet", min_free_bytes=1024)
    room = {"ok": True}
    monkeypatch.setattr(store, "free_bytes", lambda: 10**9 if room["ok"] else 0)
    eng = EN.Engine(S.MarketState("BTCUSDT"), store)
    cfg = F.FeedConfig(book_silence_s=1000)
    sockets = [ScriptedWS([snap_frame(clock)]), ScriptedWS([snap_frame(clock, u=500, seq=5000)])]
    w = F.StreamWorker(E.LINEAR, cfg, eng, Factory(*sockets), mono=clock.mono, wall_ms=clock.wall_ms,
                       sleep=clock.sleep, rng=__import__("random").Random(1))
    w.step(); w.step()
    assert eng.state.books[E.LINEAR].synced and eng.state.store_fault is None

    room["ok"] = False
    store._free_checked_at = float("-inf")                   # the store looks again (it throttles on real time)
    sockets[0].push(("raise", OSError("reset")))
    w.step()
    assert eng.state.store_fault and "reserve" in eng.state.store_fault
    view = eng.state.view(clock.m, clock.w)
    assert any(r.startswith("store:WRITE_FAILED") for r in view.refusals(S.ENTRY))
    assert not any(r.startswith("store") for r in view.refusals(S.MONITOR))

    room["ok"] = True
    store._free_checked_at = float("-inf")
    clock.advance(70.0)
    w.step(); w.step()
    assert eng.state.store_fault is None
    gaps = [json.loads(r.payload).get("event") for r in store.iter_records(kinds=(ST.CONN,))]
    assert "store_gap" in gaps
    eng.log_view(clock.m, clock.w)
    rep = EN.replay(store)
    assert rep.deterministic and rep.view_mismatches == []


def _three_days(directory):
    store = ST.MarketStore(directory, commit_every=1, environment="mainnet")
    for i in range(3):
        for j in range(3):
            store.append(ST.FRAME, wall_ms=BASE + i * DAY + j * 1000, mono_s=float(i * 10 + j),
                         category="spot", payload=f'{{"d":{i},"j":{j}}}')
    return store


def test_retention_removes_whole_old_days_and_the_rest_still_verifies(tmp_path):
    store = _three_days(str(tmp_path / "md"))
    assert store.segments() == ["md-20261104.db", "md-20261105.db", "md-20261106.db"]
    now = BASE + 2 * DAY + 5_000                             # during 2026-11-06
    assert store.apply_retention(now, 0) == []               # 0 = keep everything
    removed = store.apply_retention(now, 1)                  # keep 6 Nov and the day before
    assert removed == ["md-20261104.db"]
    assert store.segments() == ["md-20261105.db", "md-20261106.db"]
    assert store.verify() == []
    assert store.apply_retention(now, 1) == []               # idempotent


def test_retention_never_removes_the_segment_being_written(tmp_path):
    store = _three_days(str(tmp_path / "md"))
    removed = store.apply_retention(BASE + 2 * DAY + 5_000, 0 + 1)
    assert "md-20261106.db" not in removed
    assert store.verify() == []


def test_a_wall_clock_that_jumps_forward_cannot_delete_the_log(tmp_path):
    """The cutoff is also bounded by the newest segment already on disk, so a clock
    stepped 400 days ahead removes what the policy would have removed anyway, no more."""
    store = _three_days(str(tmp_path / "md"))
    removed = store.apply_retention(BASE + 400 * DAY, 1)
    assert removed == ["md-20261104.db"]
    assert store.segments() == ["md-20261105.db", "md-20261106.db"] and store.verify() == []


def test_pruning_reads_record_counts_from_the_index_not_by_scanning(tmp_path):
    store = _three_days(str(tmp_path / "md"))
    store.apply_retention(BASE + 2 * DAY + 5_000, 1)
    manifest = [json.loads(l) for l in open(store._manifest_path()) if l.strip()]
    assert manifest[0]["segment"] == "md-20261104.db" and manifest[0]["records"] == 3


def test_the_feed_applies_retention_under_the_engine_lock_and_counts_a_failure(tmp_path, monkeypatch):
    store = _three_days(str(tmp_path / "md"))
    eng = EN.Engine(S.MarketState("BTCUSDT"), store)
    clock = FakeClock(wall_ms=BASE + 2 * DAY + 5_000)
    feed = F.Feed(F.FeedConfig(retention_days=1), eng, Factory(), None, mono=clock.mono, wall_ms=clock.wall_ms)
    assert feed.apply_retention() == ["md-20261104.db"]

    def boom(*a, **k):
        raise OSError("disk error")
    monkeypatch.setattr(store, "apply_retention", boom)
    assert feed.apply_retention() == [] and feed.retention_errors == 1
    assert feed.status()["retention_errors"] == 1


def test_a_feed_with_no_retention_configured_removes_nothing(tmp_path):
    store = _three_days(str(tmp_path / "md"))
    eng = EN.Engine(S.MarketState("BTCUSDT"), store)
    clock = FakeClock(wall_ms=BASE + 400 * DAY)
    feed = F.Feed(F.FeedConfig(), eng, Factory(), None, mono=clock.mono, wall_ms=clock.wall_ms)
    assert feed.apply_retention() == [] and len(store.segments()) == 3


def test_the_runtime_passes_the_reserve_and_retention_to_the_store_and_feed(tmp_path):
    from test_md_integration import bare_bot
    bot, _ = bare_bot(tmp_path, testnet=True)
    bot.cfg.MARKET_FEED_MIN_FREE_MB = 300
    bot.cfg.MARKET_FEED_RETENTION_DAYS = 2
    bot._market_feed_factory = Factory(ScriptedWS([("timeout",)] * 3), ScriptedWS([("timeout",)] * 3))
    bot._start_market_feed()
    try:
        assert bot._market_feed_state == "RUNNING"
        assert bot.market_feed.engine.store.min_free_bytes == 300 * 1024 * 1024
        assert bot.market_feed.cfg.retention_days == 2
    finally:
        bot._stop_market_feed()


def test_the_config_defaults_and_bounds():
    import config as C
    cfg = C.load({})
    assert cfg.MARKET_FEED_MIN_FREE_MB == 256 and cfg.MARKET_FEED_RETENTION_DAYS == 1
    cfg = C.load({"MARKET_FEED_MIN_FREE_MB": "0", "MARKET_FEED_RETENTION_DAYS": "0"})
    assert cfg.MARKET_FEED_MIN_FREE_MB == 0 and cfg.MARKET_FEED_RETENTION_DAYS == 0
    for bad in ({"MARKET_FEED_RETENTION_DAYS": "-1"}, {"MARKET_FEED_MIN_FREE_MB": "abc"}):
        with pytest.raises(C.ConfigError):
            C.load(bad)


def test_infinity_is_a_config_error_not_an_overflow():
    import config as C
    with pytest.raises(C.ConfigError):
        C.load({"MARKET_FEED_RETENTION_DAYS": "inf"})


# ---------------------------------------------------------------- a STALLED store (volume below the write reserve) must be able to recover at the day boundary
def _stall(store, *, free=10, reserve=1000):
    """The volume is below the write reserve (the state the live recorder is in)."""
    store.min_free_bytes = reserve
    store.free_bytes = lambda: free


def _four_days(directory):
    store = ST.MarketStore(directory, commit_every=1, environment="mainnet")
    for i in range(4):
        for j in range(3):
            store.append(ST.FRAME, wall_ms=BASE + i * DAY + j * 1000, mono_s=float(i * 10 + j), category="spot", payload=f'{{"d":{i},"j":{j}}}')
    return store


def test_a_stalled_store_follows_the_wall_clock_by_one_day_and_only_then(tmp_path):
    """Not stalled: the cutoff is bounded by the newest segment (the existing guard). Stalled: the day that is now past retention is dropped, which frees space."""
    store = _three_days(str(tmp_path / "md"))                                         # segments 4, 5, 6 Nov; the log's newest day is the 6th
    now = BASE + 3 * DAY + 5_000                                                      # the wall clock is already in 7 Nov (the log could not open that segment)
    store.min_free_bytes, store.free_bytes = 1000, lambda: 10_000                      # healthy: free space above the reserve
    assert store.apply_retention(now, 2) == [] and store.segments() == ["md-20261104.db", "md-20261105.db", "md-20261106.db"]
    _stall(store)
    assert store.apply_retention(now, 2) == ["md-20261104.db"]                        # 4 Nov is past retention (7 Nov - 2 days = 5 Nov): dropped
    assert store.segments() == ["md-20261105.db", "md-20261106.db"] and store.verify() == []   # the retained log still verifies from its checkpoint
    assert store.apply_retention(now, 2) == []                                        # idempotent: nothing inside the window is ever removed


def test_a_stalled_store_with_a_clock_stepped_far_ahead_loses_at_most_one_extra_segment(tmp_path):
    """Bounded: 4 segments, retention 2, a clock 400 days ahead. The policy bound (newest day - 2) would remove only the 4th; stalled, exactly ONE more (the 5th) goes, never the log."""
    store = _four_days(str(tmp_path / "md"))                                          # 4, 5, 6, 7 Nov
    _stall(store)
    assert store.apply_retention(BASE + 400 * DAY, 2) == ["md-20261104.db", "md-20261105.db"]
    assert store.segments() == ["md-20261106.db", "md-20261107.db"] and store.verify() == []
    assert store.apply_retention(BASE + 400 * DAY, 2) == []                           # the newest segment is never touched, and the slack does not apply twice
    healthy = _four_days(str(tmp_path / "md2"))
    healthy.min_free_bytes, healthy.free_bytes = 1000, lambda: 10_000
    assert healthy.apply_retention(BASE + 400 * DAY, 2) == ["md-20261104.db"]         # the same clock on a healthy volume removes only what the policy always did


def test_a_stall_that_lasts_drops_one_more_expired_day_for_every_day_it_lasts(tmp_path, monkeypatch):
    """REPRODUCED (Round 16): two retained days that ONE dropped day cannot free never recovered, whatever the
    wall clock did (every later call removed nothing). The allowance now grows by one day per day of MONOTONIC
    stall, so a stalled recorder can recover by itself; a stepped wall clock alone still cannot grow it."""
    store = _four_days(str(tmp_path / "md"))                                          # 4, 5, 6, 7 Nov
    clock = {"t": 5_000.0}
    monkeypatch.setattr(ST.time, "monotonic", lambda: clock["t"])
    _stall(store)
    day8 = BASE + 4 * DAY + 5_000
    assert store.apply_retention(day8, 2) == ["md-20261104.db", "md-20261105.db"]     # the one-day allowance, as before
    assert store.segments() == ["md-20261106.db", "md-20261107.db"]
    # the wall clock steps a day ahead but the stall has NOT lasted a day
    day9 = day8 + DAY
    assert store.apply_retention(day9, 2) == []                                       # a stepped clock cannot grow the allowance
    clock["t"] += 86_400.0 + 1.0                                                       # a full day of real stall
    assert store.apply_retention(day9, 2) == ["md-20261106.db"]                       # now 6 Nov is past retention (9 Nov - 2 = 7 Nov) and goes
    assert store.segments() == ["md-20261107.db"] and store.verify() == []
    clock["t"] += 30 * 86_400.0                                                        # however long it lasts, the newest segment is never touched
    assert store.apply_retention(day9 + 30 * DAY, 2) == []
    assert store.segments() == ["md-20261107.db"]


def test_the_stall_clock_resets_when_the_volume_recovers(tmp_path, monkeypatch):
    store = _four_days(str(tmp_path / "md"))
    clock = {"t": 5_000.0}
    monkeypatch.setattr(ST.time, "monotonic", lambda: clock["t"])
    _stall(store)
    store.apply_retention(BASE + 4 * DAY + 5_000, 2)
    clock["t"] += 3 * 86_400.0
    store.min_free_bytes, store.free_bytes = 1000, lambda: 10_000                      # the volume has room again
    assert store.apply_retention(BASE + 7 * DAY, 2) == []
    assert store._stalled_since is None
    _stall(store)                                                                      # a NEW stall starts from one day of allowance, not three
    store.apply_retention(BASE + 7 * DAY, 2)
    assert store._stalled_since == clock["t"]


def test_a_failed_free_space_look_never_enables_a_deletion(tmp_path):
    """The WRITER treats a failed look as 'low' (never assume room); the RETENTION must treat it as 'not stalled' (an error must never enable a deletion)."""
    store = _three_days(str(tmp_path / "md"))
    store.min_free_bytes = 1000

    def boom():
        raise OSError("statvfs failed")
    store.free_bytes = boom
    assert store.low_space() is True                                                  # the writer's reading is unchanged
    assert store.apply_retention(BASE + 3 * DAY + 5_000, 2) == [] and len(store.segments()) == 3


def _simulate_volume(tmp_path, *, label, stalled_aware, vol=5.0, daily=1.5, days=9, retention=2):
    """The REAL MarketStore on a fake-capacity volume (1 MB == 1 GB). Hourly: write, then the maintenance thread's apply_retention; the emergency prune is default-deny (as deployed).
    ``daily`` is a NOMINAL rate: the measured on-disk size of a full day here is about 1.9 units (the size model's inputs are therefore 1.9, not 1.5)."""
    import random
    unit = 1_000_000
    d = str(tmp_path / label)
    store = ST.MarketStore(d, commit_every=50, environment="mainnet", min_free_bytes=int(0.537 * unit))
    other = int(0.05 * unit)

    def used():
        return sum(os.path.getsize(os.path.join(d, f)) for f in os.listdir(d)) + other
    store.free_bytes = lambda: max(0, int(vol * unit) - used())
    if not stalled_aware:                                                              # the old behaviour: the newest-segment bound always applies (no stall slack)
        real_apply = store.apply_retention
        store.apply_retention = lambda now_ms, retention_days: real_apply(now_ms, retention_days) if not store.low_space() else _old_apply(store, now_ms, retention_days)
    rnd = random.Random(1)
    base = 1_793_750_400_000                                                           # 2026-11-04 00:00 UTC
    per_row = int(daily * unit / 24 / 20)
    probe = rnd.randbytes(10_000).hex()
    ratio = len(ST._compress(probe.encode())) / len(probe)                             # the compressed size of random hex: calibrate the on-disk bytes per row to the target
    pay_len = max(1, int((per_row - 90) / ratio))
    written_per_day = {}
    # Round 20 (specialist A): the stall slack in apply_retention grows by one day per full MONOTONIC day of stall. This harness runs nine simulated days in real seconds, so the clock never aged and a
    # 3 GB volume read as "stalled for good" where production, whose monotonic clock does advance, recovers one day at a time. The clock now advances one hour per simulated hour.
    import unittest.mock as _mock
    clock = {"t": 10_000.0}
    patcher = _mock.patch.object(ST.time, "monotonic", lambda: clock["t"])
    patcher.start()
    for h in range(days * 24):
        clock["t"] = 10_000.0 + h * 3600.0
        now = base + h * 3_600_000
        for k in range(20):
            store._free_checked_at = float("-inf")
            try:
                store.append(ST.FRAME, wall_ms=now + k * 1000, mono_s=float(h * 100 + k), category="linear", conn_id="l-1", topic="",
                             payload=rnd.randbytes(pay_len // 2 + 1).hex()[:pay_len])
                written_per_day[h // 24] = written_per_day.get(h // 24, 0) + 1
            except ST.StoreError:
                pass
        store.flush()
        if store._conn:
            store._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        store.apply_retention(now + 3_599_000, retention)
    patcher.stop()
    store.close()
    return written_per_day


def _old_apply(store, now_ms, retention_days):
    """apply_retention exactly as it was before the stall slack (the cutoff bounded by the newest segment on disk, always)."""
    from datetime import datetime, timezone
    if retention_days <= 0:
        return []
    cutoff = ST._utc_day(int(now_ms) - int(retention_days) * 86_400_000)
    segs = store.segments()
    if segs:
        newest_ms = int(datetime.strptime(segs[-1][3:11], "%Y%m%d").replace(tzinfo=timezone.utc).timestamp()) * 1000
        cutoff = min(cutoff, ST._utc_day(newest_ms - int(retention_days) * 86_400_000))
    return store.prune_segments_before(cutoff)


def test_a_volume_that_holds_the_retained_days_plus_the_reserve_recovers_instead_of_stalling_for_good(tmp_path):
    """Reproduced with the real store. A full day measures about 1.9 units here, so retention 2 needs about 2 x 1.9 + 0.05 + 0.537 = 4.4 units: a 5.0 volume qualifies but is below the 6.5 the size
    model asks for (3 x 1.9 x 1.042 + 0.59), so it loses most of every third day. Before the fix the recorder wrote nothing from the day it first hit the reserve, for ever; now the day that falls out
    of retention is dropped at the next day boundary and writes resume."""
    stuck = _simulate_volume(tmp_path, label="old", stalled_aware=False)
    assert all(stuck.get(day, 0) == 0 for day in (3, 4, 5, 6, 7, 8)), stuck                                 # the permanent stall: nothing written after the reserve was first hit
    ok = _simulate_volume(tmp_path, label="new", stalled_aware=True)
    assert all(ok.get(day, 0) > 0 for day in (3, 4, 5, 6, 7, 8)), ok                                      # every later day records something again
    assert sum(ok.values()) > sum(stuck.values()) and min(ok[d] for d in (5, 8)) < 480                    # but a volume below the size model's figure still loses most of some days


def test_a_stall_is_permanent_only_when_the_volume_cannot_hold_one_realised_day_above_the_reserve(tmp_path):
    """The limit, stated by a test with a clock that ages as production's does (Round 20, specialist A; the previous version froze the clock and so pinned 'a 3 GB volume stalls for good', which production
    does not do). A 1 GB volume cannot hold one realised day (about 1.9 units here) above the 0.537 reserve: the first segment is never pruned and a refused write never opens the next day, so every later day
    writes NOTHING and a restart does not help. A 3 GB volume holds one day but not the retained days: it recovers one day at a time and loses part of every cycle."""
    tiny = _simulate_volume(tmp_path, label="small1.0", stalled_aware=True, vol=1.0)
    assert all(tiny.get(day, 0) == 0 for day in (3, 4, 5, 6, 7, 8)), tiny
    mid = _simulate_volume(tmp_path, label="small3.0", stalled_aware=True, vol=3.0)
    assert any(mid.get(day, 0) > 0 for day in (3, 4, 5, 6, 7, 8)), mid
    assert sum(mid.values()) > sum(tiny.values()) and min(mid.get(d, 0) for d in (3, 4, 5, 6, 7, 8)) < 480, "a volume below the size model's figure still loses part of its days"


# ---------------------------------------------------------------- a recorder that died while CREATING a segment must restart (an independent review: it looped until a human deleted the file)
def _segment_path(directory, day):
    return os.path.join(str(directory), f"md-{day}.db")


def _two_days_then_a_dead_creation(tmp_path, kind):
    d = tmp_path / "md"
    s = ST.MarketStore(str(d), commit_every=1, environment="mainnet")
    s.append(ST.FRAME, wall_ms=BASE, mono_s=1.0, category="spot", payload='{"a":1}')
    s.append(ST.FRAME, wall_ms=BASE + 1, mono_s=1.1, category="spot", payload='{"a":2}')
    s.close()
    dead = _segment_path(d, "20261105")
    if kind == "zero_bytes":
        open(dead, "wb").close()                                       # the file existed, nothing else did (ENOSPC / kill at the moment of the day rollover)
    elif kind == "meta_only":
        import sqlite3
        c = sqlite3.connect(dead)
        c.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        c.commit()
        c.close()
    return d, dead


@pytest.mark.parametrize("kind", ["zero_bytes", "meta_only"])
def test_a_segment_the_recorder_died_while_creating_does_not_brick_the_next_start(tmp_path, kind):
    d, dead = _two_days_then_a_dead_creation(tmp_path, kind)
    s = ST.MarketStore(str(d), commit_every=1, environment="mainnet")            # raised OperationalError / StoreError forever before
    assert s.count() == 2                                                         # the real records are untouched and the tail is read from the last INITIALISED segment
    s.append(ST.FRAME, wall_ms=BASE + DAY + 5, mono_s=2.0, category="spot", payload='{"a":3}')      # the writer completes the segment it finds half-created, on the same day
    s.close()
    r = ST.MarketStore(str(d))                                                    # read-only handle
    assert r.count() == 3 and r.verify() == [], r.verify()


@pytest.mark.parametrize("kind", ["zero_bytes", "meta_only"])
def test_verify_reports_an_uninitialised_segment_instead_of_crashing_or_hiding_it(tmp_path, kind):
    d, dead = _two_days_then_a_dead_creation(tmp_path, kind)
    r = ST.MarketStore(str(d))
    problems = r.verify()
    assert any("md-20261105.db" in p and "uninitialised" in p for p in problems), problems


def test_retention_removes_an_uninitialised_oldest_segment_without_a_checkpoint_it_has_no_records(tmp_path):
    d = tmp_path / "md"
    os.makedirs(d)
    open(_segment_path(d, "20261101"), "wb").close()                              # an abandoned creation, two days before the real data
    s = ST.MarketStore(str(d), commit_every=1, environment="mainnet")
    for i, off in enumerate((2 * DAY, 3 * DAY, 4 * DAY)):
        s.append(ST.FRAME, wall_ms=BASE + off, mono_s=float(i), category="spot", payload="{}")
    removed = s.apply_retention(BASE + 4 * DAY, 2)
    assert "md-20261101.db" in removed and not os.path.exists(_segment_path(d, "20261101"))
    assert s.verify() == [] or all("uninitialised" not in p for p in s.verify())


def test_a_file_that_is_not_a_database_is_still_refused_not_treated_as_uninitialised(tmp_path):
    d, dead = _two_days_then_a_dead_creation(tmp_path, "none")
    with open(dead, "wb") as fh:
        fh.write(b"this is not a sqlite database at all" * 40)
    with pytest.raises(Exception) as exc:
        ST.MarketStore(str(d), commit_every=1, environment="mainnet")
    assert "not a database" in str(exc.value) or isinstance(exc.value, ST.StoreError)


def test_a_segment_with_records_is_never_treated_as_uninitialised(tmp_path):
    d = tmp_path / "md"
    s = ST.MarketStore(str(d), commit_every=1, environment="mainnet")
    s.append(ST.FRAME, wall_ms=BASE, mono_s=1.0, category="spot", payload="{}")
    s.close()
    assert ST.MarketStore(str(d))._initialised("md-20261104.db") is True


# ---------------------------------------------------------------- second independent review: an unfinished CREATION is only ever the newest segment
def _three_days_two_frames(tmp_path):
    d = tmp_path / "md"
    s = ST.MarketStore(str(d), commit_every=1, environment="mainnet")
    for i, off in enumerate((0, DAY, 2 * DAY)):
        s.append(ST.FRAME, wall_ms=BASE + off, mono_s=float(i), category="spot", payload='{"i":%d}' % i)
        s.append(ST.FRAME, wall_ms=BASE + off + 1, mono_s=float(i) + 0.1, category="spot", payload='{"j":%d}' % i)
    s.close()
    return d


def test_an_emptied_segment_that_has_a_successor_is_damage_every_reader_refuses_it_not_a_hole_to_count_around(tmp_path):
    import sqlite3
    d = _three_days_two_frames(tmp_path)
    segs = sorted(f for f in os.listdir(d) if f.startswith("md-") and f.endswith(".db"))
    assert len(segs) == 3
    open(os.path.join(d, segs[1]), "wb").close()                                   # the MIDDLE closed day lost its content
    r = ST.MarketStore(str(d))
    with pytest.raises(sqlite3.DatabaseError, match="no such table"):
        r.count()                                                                   # it used to return 4 (not 6) without a word
    with pytest.raises(sqlite3.DatabaseError, match="no such table"):
        list(r.iter_records())
    problems = r.verify()
    assert any(segs[1] in p and "not the newest" in p and "LOST" in p for p in problems), problems


def _after_a_dead_creation_and_a_quiet_day(tmp_path, *, quiet_days=2):
    """Day 1 has records; the recorder dies while CREATING day 2 (a 0-byte file); it is restarted ``quiet_days`` later and writes again."""
    d = tmp_path / "md"
    s = ST.MarketStore(str(d), commit_every=1, environment="mainnet")
    for j in range(2):
        s.append(ST.FRAME, wall_ms=BASE + j, mono_s=float(j), category="spot", payload='{"a":%d}' % j)
    s.close()
    dead = _segment_path(d, "20261105")
    open(dead, "wb").close()
    s2 = ST.MarketStore(str(d), commit_every=1, environment="mainnet")
    for j in range(2):
        s2.append(ST.FRAME, wall_ms=BASE + quiet_days * DAY + j, mono_s=float(10 + j), category="spot", payload='{"b":%d}' % j)
    s2.close()
    return d, dead


def test_an_abandoned_creation_followed_by_later_days_does_not_brick_the_readers_and_loses_nothing(tmp_path):
    """Independent re-review, finding 5: the previous rule made EVERY uninitialised segment with a successor damage, so a crash during creation followed by a day without data left
    ``count()`` and ``iter_records()`` raising 'no such table: log' (and the recorder's own refresh_count failing every cycle) until the owner moved the file aside by hand. The hash chain is global:
    when it continues across the file, nothing was ever in it."""
    d, dead = _after_a_dead_creation_and_a_quiet_day(tmp_path)
    r = ST.MarketStore(str(d))
    assert r.segments() == ["md-20261104.db", "md-20261105.db", "md-20261106.db"] and os.path.getsize(dead) == 0
    assert r.count() == 4 and [x.id for x in r.iter_records()] == [1, 2, 3, 4], "every record of the other segments must stay readable"
    problems = r.verify()
    assert len(problems) == 1 and "md-20261105.db" in problems[0] and "uninitialised" in problems[0] and "hash chain continues across it" in problems[0] and "LOST" not in problems[0], problems
    assert "cannot tell the two apart" in problems[0], "the verdict must not claim certainty the store does not have"
    w = ST.MarketStore(str(d), commit_every=1, environment="mainnet")                    # and the recorder goes on writing
    w.append(ST.FRAME, wall_ms=BASE + 2 * DAY + 50, mono_s=99.0, category="spot", payload='{"c":1}')
    assert w.count() == 5 and [x.id for x in w.iter_records()] == [1, 2, 3, 4, 5]
    w.close()


def test_an_abandoned_creation_before_any_record_is_provably_empty_with_or_without_a_prune_checkpoint(tmp_path):
    d = tmp_path / "md"
    os.makedirs(d)
    open(_segment_path(d, "20261101"), "wb").close()                                       # the oldest file: nothing ever came before it
    s = ST.MarketStore(str(d), commit_every=1, environment="mainnet")
    for i, off in enumerate((2 * DAY, 3 * DAY, 4 * DAY)):
        s.append(ST.FRAME, wall_ms=BASE + off, mono_s=float(i), category="spot", payload="{}")
    s.close()
    r = ST.MarketStore(str(d))
    assert r.count() == 3 and len(list(r.iter_records())) == 3 and all("LOST" not in p for p in r.verify())
    # after a prune checkpoint the chain it must continue is the pruned tail: still provable
    removed = r.prune_segments_before("20261106")
    assert "md-20261101.db" not in r.segments() or removed
    r2 = ST.MarketStore(str(d))
    assert all("LOST" not in p for p in r2.verify()) and r2.count() == len(list(r2.iter_records()))


def test_a_trailing_run_of_dead_creations_does_not_stop_the_recorder_from_starting(tmp_path):
    """Independent re-review of the fix, N1: ``[records, 0-byte, 0-byte]`` (creation failing across two midnights, for instance on a full disk) made ``MarketStore(dir)`` itself raise 'no such table: log':
    the recorder could not start, and even a read-only open or verify was refused until the files were moved by hand. A trailing RUN of unfinished creations is treated like the newest one."""
    d = tmp_path / "md"
    s = ST.MarketStore(str(d), commit_every=1, environment="mainnet")
    for j in range(2):
        s.append(ST.FRAME, wall_ms=BASE + j, mono_s=float(j), category="spot", payload='{"a":%d}' % j)
    s.close()
    for day in ("20261105", "20261106"):
        open(_segment_path(d, day), "wb").close()                                           # creation failed on two consecutive days
    r = ST.MarketStore(str(d))                                                              # a read-only style open works
    assert r.count() == 2 and [x.id for x in r.iter_records()] == [1, 2]
    problems = r.verify()
    assert len(problems) == 2 and all("uninitialised" in p for p in problems) and any("trailing run" in p for p in problems) and not any("LOST" in p for p in problems), problems
    w = ST.MarketStore(str(d), commit_every=1, environment="mainnet")                       # and the recorder starts, continuing from the last real record
    w.append(ST.FRAME, wall_ms=BASE + 2 * DAY + 5, mono_s=9.0, category="spot", payload='{"b":1}')
    assert [x.id for x in w.iter_records()] == [1, 2, 3] and w.count() == 3
    w.close()
    # the dead files now sit BEFORE a real segment: the chain proves them empty (the earlier rule), and a trailing run needs no proof
    again = ST.MarketStore(str(d))
    assert again.count() == 3 and all("LOST" not in p for p in again.verify())


def test_a_zeroed_day_followed_only_by_dead_creations_is_reported_not_counted_around_in_silence(tmp_path):
    """The ambiguity a trailing run cannot resolve is stated, not hidden: a closed day that lost its content followed only by a dead creation reads like two unfinished creations. verify() names
    both and says it cannot tell them apart. (Once the writer continues, it re-links the chain from the last readable record, so the files then read as skipped-and-named, with the same caveat.)"""
    d = _three_days_two_frames(tmp_path)
    segs = sorted(f for f in os.listdir(d) if f.startswith("md-") and f.endswith(".db"))
    open(os.path.join(d, segs[1]), "wb").close()                                           # the middle day (with records) loses its content
    open(os.path.join(d, segs[2]), "wb").close()                                           # and the newest is dead too
    r = ST.MarketStore(str(d))
    problems = r.verify()
    assert r.count() == 2 and len(problems) == 2 and all("uninitialised" in p for p in problems), problems
    w = ST.MarketStore(str(d), commit_every=1, environment="mainnet")
    w.append(ST.FRAME, wall_ms=BASE + 5 * DAY, mono_s=50.0, category="spot", payload='{"later":1}')
    w.close()
    r2 = ST.MarketStore(str(d))
    after = r2.verify()
    assert r2.count() == 3 and len(after) == 2 and all("cannot tell the two apart" in p or "trailing run" in p for p in after), after
    assert [x.id for x in r2.iter_records()] == [1, 2, 3], "the new record continues from the last readable one (and any lost ids are reused: stated in verify)"


def test_an_abandoned_creation_that_becomes_the_oldest_file_after_a_prune_is_proven_empty_by_the_pruned_tail(tmp_path):
    d, dead = _after_a_dead_creation_and_a_quiet_day(tmp_path)
    s = ST.MarketStore(str(d), commit_every=1, environment="mainnet")
    removed = s.prune_segments_before("20261105")                                           # day 1 (with records) goes; the dead creation is now the oldest retained file
    assert removed == ["md-20261104.db"] and s.segments() == ["md-20261105.db", "md-20261106.db"]
    s.close()
    r = ST.MarketStore(str(d))
    assert r.count() == 2 and [x.id for x in r.iter_records()] == [3, 4]
    assert all("LOST" not in p for p in r.verify()), r.verify()


def test_a_closed_day_that_held_records_stays_damage_even_when_an_abandoned_creation_sits_beside_it(tmp_path):
    import sqlite3
    d, dead = _after_a_dead_creation_and_a_quiet_day(tmp_path)
    s3 = ST.MarketStore(str(d), commit_every=1, environment="mainnet")                     # a later day, then lose a day that HELD records
    s3.append(ST.FRAME, wall_ms=BASE + 3 * DAY, mono_s=200.0, category="spot", payload='{"d":1}')
    s3.close()
    held = _segment_path(d, "20261106")
    assert os.path.getsize(held) > 0
    open(held, "wb").close()                                                                 # the day with records b0, b1 loses its content
    r = ST.MarketStore(str(d))
    with pytest.raises(sqlite3.DatabaseError, match="no such table"):
        r.count()
    problems = r.verify()
    assert any("md-20261106.db" in p and "LOST" in p and "does NOT continue" in p for p in problems), problems
    # two empty files in a row: the chain break is somewhere across the PAIR, so neither can be proven empty and both are reported (conservative: never count around a possible hole)
    assert any("md-20261105.db" in p and "LOST" in p and "does NOT continue" in p for p in problems), problems
    assert any("id gap" in p or "chain_start does not continue" in p for p in problems), "and the break itself is reported by the chain checks"


def test_the_newest_segment_is_still_the_only_one_an_unfinished_creation_can_be(tmp_path):
    d = _three_days_two_frames(tmp_path)
    segs = sorted(f for f in os.listdir(d) if f.startswith("md-") and f.endswith(".db"))
    open(os.path.join(d, segs[-1]), "wb").close()                                   # the day being created when the recorder died
    r = ST.MarketStore(str(d))
    assert r.count() == 4 and any(segs[-1] in p and "recorder died while creating" in p for p in r.verify())


def test_removing_an_emptied_segment_by_retention_leaves_an_audit_row_not_a_silent_deletion(tmp_path):
    d = tmp_path / "md"
    os.makedirs(d)
    open(_segment_path(d, "20261101"), "wb").close()
    s = ST.MarketStore(str(d), commit_every=1, environment="mainnet")
    for i, off in enumerate((2 * DAY, 3 * DAY, 4 * DAY)):
        s.append(ST.FRAME, wall_ms=BASE + off, mono_s=float(i), category="spot", payload="{}")
    assert "md-20261101.db" in s.apply_retention(BASE + 4 * DAY, 2)
    rows = [json.loads(line) for line in open(os.path.join(d, "pruned.jsonl")) if line.strip()]
    assert [(r["segment"], r.get("uninitialised"), r["records"]) for r in rows if r["segment"] == "md-20261101.db"] == [("md-20261101.db", True, 0)]
