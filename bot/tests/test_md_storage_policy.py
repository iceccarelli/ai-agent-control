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
    removed = store.apply_retention(BASE + 400 * DAY, 1)
    assert "md-20261106.db" not in removed and store.segments() == ["md-20261106.db"]
    assert store.verify() == []


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
