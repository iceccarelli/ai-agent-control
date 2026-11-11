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
def test_a_stalled_store_follows_the_wall_clock_by_one_day_and_only_then(tmp_path, monkeypatch):
    """Not stalled: the cutoff is bounded by the newest segment (the existing guard). Stalled: the day that is now past retention is dropped, which frees space."""
    store = _three_days(str(tmp_path / "md"))                                         # segments 4, 5, 6 Nov; the log's newest day is the 6th
    now = BASE + 3 * DAY + 5_000                                                      # the wall clock is already in 7 Nov (the log could not open that segment)
    monkeypatch.setattr(store, "low_space", lambda: False)
    assert store.apply_retention(now, 2) == [] and store.segments() == ["md-20261104.db", "md-20261105.db", "md-20261106.db"]
    monkeypatch.setattr(store, "low_space", lambda: True)
    assert store.apply_retention(now, 2) == ["md-20261104.db"]                        # 4 Nov is past retention (7 Nov - 2 days = 5 Nov): dropped
    assert store.segments() == ["md-20261105.db", "md-20261106.db"] and store.verify() == []   # the retained log still verifies from its checkpoint
    assert store.apply_retention(now, 2) == []                                        # idempotent: nothing inside the window is ever removed


def test_a_stalled_store_with_a_clock_stepped_far_ahead_loses_at_most_one_extra_segment(tmp_path, monkeypatch):
    store = _three_days(str(tmp_path / "md"))
    monkeypatch.setattr(store, "low_space", lambda: True)
    removed = store.apply_retention(BASE + 400 * DAY, 1)                              # a clock 400 days ahead while the volume is full
    assert removed == ["md-20261104.db", "md-20261105.db"]                            # the policy would have removed only the 4th: ONE extra segment, never the log
    assert store.segments() == ["md-20261106.db"] and store.verify() == []
    assert store.apply_retention(BASE + 400 * DAY, 1) == []                           # the newest segment is never touched


def _simulate_volume(tmp_path, *, label, stalled_aware, vol=5.0, daily=1.5, days=9, retention=2):
    """The REAL MarketStore on a fake-capacity volume (1 MB == 1 GB). Hourly: write, then the maintenance thread's apply_retention; the emergency prune is default-deny (as deployed)."""
    import random
    unit = 1_000_000
    d = str(tmp_path / label)
    store = ST.MarketStore(d, commit_every=50, environment="mainnet", min_free_bytes=int(0.537 * unit))
    other = int(0.05 * unit)

    def used():
        return sum(os.path.getsize(os.path.join(d, f)) for f in os.listdir(d)) + other
    store.free_bytes = lambda: max(0, int(vol * unit) - used())
    if not stalled_aware:
        store.low_space = lambda: False                                                # the old behaviour: the newest-segment bound always applies
    rnd = random.Random(1)
    base = 1_793_750_400_000                                                           # 2026-11-04 00:00 UTC
    per_row = int(daily * unit / 24 / 20)
    probe = rnd.randbytes(10_000).hex()
    ratio = len(ST._compress(probe.encode())) / len(probe)                             # the compressed size of random hex: calibrate the on-disk bytes per row to the target
    pay_len = max(1, int((per_row - 90) / ratio))
    written_per_day = {}
    for h in range(days * 24):
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
    store.close()
    return written_per_day


def test_a_recorder_on_a_volume_that_fits_the_retention_only_barely_recovers_instead_of_stalling_for_good(tmp_path):
    """Reproduced with the real store (a 5 GB volume, 1.5 GB/day, retention 2: the documented case): before the fix the recorder wrote nothing from the day it first hit the reserve, for ever; now the
    day that falls out of retention is dropped at the next day boundary and writes resume."""
    stuck = _simulate_volume(tmp_path, label="old", stalled_aware=False)
    assert all(stuck.get(day, 0) == 0 for day in (3, 4, 5, 6, 7, 8)), stuck                                 # the permanent stall: nothing written after the reserve was first hit
    ok = _simulate_volume(tmp_path, label="new", stalled_aware=True)
    assert all(ok.get(day, 0) > 0 for day in (3, 4, 5, 6, 7, 8)), ok                                      # every later day records something again
    # an UNDER-SIZED volume (this one is below the 5.6 GB the size model asks for) still loses data near each daily low: the fix turns a permanent stall into gaps, it does not make a small volume big enough
    assert sum(ok.values()) > sum(stuck.values()) and min(ok[d] for d in (5, 8)) < 480
