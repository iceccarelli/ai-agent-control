"""Recorder liveness (Phase A, Priority Zero). Each test reproduces ONE way the status file went stale while data collection was alive (or looked alive while it was not),
and pins the correction. Offline: the recorder runs against scripted sockets in tmp_path (never bot/state)."""
import json
import os
import sys
import threading
import time

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
BOT = os.path.dirname(HERE)
sys.path[:0] = [HERE, BOT, os.path.join(BOT, "tools")]

import market_recorder as MR  # noqa: E402
import runner_monitor as RM  # noqa: E402
from test_market_recorder import make, _inject_event  # noqa: E402


def read_status(rec):
    with open(os.path.join(rec.dir, MR.STATUS_FILE), encoding="utf-8") as fh:
        return json.load(fh)


def run_in_thread(rec, **kw):
    stop = threading.Event()
    kw.setdefault("status_every_s", 0.01)
    kw.setdefault("capacity_every_s", 3600)
    t = threading.Thread(target=rec.run, args=(stop,), kwargs=kw, daemon=True)
    t.start()
    return stop, t


def wait_for(cond, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.01)
    return False


def real_clock(rec):
    """make() freezes both clocks; a RUNNING loop needs them to move (the heartbeat schedules itself on the monotonic clock)."""
    rec.wall_ms = lambda: int(time.time() * 1000)
    rec.mono = time.monotonic


def test_a_failing_status_write_is_counted_and_never_ends_the_run_loop(tmp_path, monkeypatch):
    """REPRODUCED: an OSError (a full volume) while writing the status file propagated out of Recorder.run, ended the process, and the supervisor's backoff left the old file to go STALE."""
    rec, store, _ = make(tmp_path)
    real_clock(rec)
    real = MR._atomic_write
    calls = {"n": 0}

    def flaky(path, obj):
        calls["n"] += 1
        if calls["n"] <= 3:
            raise OSError(28, "No space left on device")
        return real(path, obj)
    monkeypatch.setattr(MR, "_atomic_write", flaky)
    rec.feed.start()
    stop, t = run_in_thread(rec)
    assert wait_for(lambda: os.path.exists(os.path.join(rec.dir, MR.STATUS_FILE))), "no status file was ever written after the transient faults"
    assert t.is_alive(), "the run loop died on a status-write error"
    st = read_status(rec)
    assert st["heartbeat"]["status_errors"] >= 3 and "No space left" in st["heartbeat"]["last_status_error"]
    stop.set()
    t.join(5)
    rec.stop()


def test_a_failing_prune_or_capture_never_ends_the_run_loop_and_is_reported(tmp_path, monkeypatch):
    rec, store, _ = make(tmp_path)
    real_clock(rec)
    monkeypatch.setattr(rec, "prune_capacity", lambda: (_ for _ in ()).throw(OSError(5, "I/O error")))
    rec.feed.start()
    stop, t = run_in_thread(rec, prune_every_s=0.01)
    assert wait_for(lambda: os.path.exists(os.path.join(rec.dir, MR.STATUS_FILE)) and read_status(rec)["heartbeat"]["maintenance_errors"] >= 1)
    assert t.is_alive()
    assert "I/O error" in read_status(rec)["heartbeat"]["last_maintenance_error"]
    stop.set()
    t.join(5)
    rec.stop()


def test_a_blocked_capacity_capture_does_not_stop_the_status_heartbeat(tmp_path, monkeypatch):
    """REPRODUCED: capture_capacity ran on the SAME thread as the status write, so one slow capture (engine-lock wait, chain repair, a slow volume) froze the heartbeat and the monitor read STALE
    while the feed was still recording."""
    rec, store, _ = make(tmp_path)
    real_clock(rec)
    gate = threading.Event()
    monkeypatch.setattr(rec, "capture_capacity", lambda: gate.wait(20))
    rec.feed.start()
    stop, t = run_in_thread(rec, capacity_every_s=0.01)
    assert wait_for(lambda: os.path.exists(os.path.join(rec.dir, MR.STATUS_FILE)))
    t1 = read_status(rec)["now_ms"]
    assert wait_for(lambda: read_status(rec)["now_ms"] > t1 + 150), "the heartbeat froze while a capacity capture was blocked"
    gate.set()
    stop.set()
    t.join(8)
    assert not t.is_alive()
    rec.stop()


def test_the_heartbeat_never_scans_the_log(tmp_path, monkeypatch):
    """REPRODUCED: every status write called store.count() = flush + SELECT COUNT(*) over every segment (a full scan of a ~1.5 GB/day log) on the heartbeat thread."""
    rec, store, _ = make(tmp_path)
    scans = {"n": 0}
    real = store.count
    monkeypatch.setattr(store, "count", lambda: scans.__setitem__("n", scans["n"] + 1) or real())
    rec.write_status()
    rec.write_status()
    rec.write_status()
    assert scans["n"] == 0, "the status write scanned the log"


def test_progress_is_the_newest_record_id_and_a_log_that_stops_growing_shows_its_age(tmp_path):
    rec, store, _ = make(tmp_path)
    clock = {"ms": 1_700_000_000_000}
    rec.wall_ms = lambda: clock["ms"]
    rec.started_ms = clock["ms"]
    first = rec.write_status()
    assert first["progress"]["growth_age_s"] == 0.0
    clock["ms"] += 400_000                                                     # 400 s pass, nothing is recorded
    stalled = rec.write_status()
    assert stalled["progress"]["growth_age_s"] == pytest.approx(400.0) and stalled["progress"]["newest_record_id"] == first["progress"]["newest_record_id"]
    rec.engine.input_count += 1                                                # a market input record lands (the counter the engine increments on a committed frame / REST append)
    clock["ms"] += 1000
    moved = rec.write_status()
    assert moved["progress"]["growth_age_s"] == 0.0 and moved["progress"]["market_inputs"] == first["progress"]["market_inputs"] + 1


# ---------------------------------------------------------------- what the monitor reports
def status_file(tmp_path, *, age_s, growth_age_s=0.0, threads_alive=True, fault=None, now=1_700_000_000.0, with_progress=True):
    st = {"now_ms": int((now - age_s) * 1000), "feed": {"threads_alive": threads_alive}, "store": {"records": 5, "free_bytes": 5 * 1048576 * 100, "fault": fault},
          "capacity": {"rows": 3}}
    if with_progress:
        st["progress"] = {"newest_record_id": 10, "growth_age_s": growth_age_s}
    p = tmp_path / "recorder_status.json"
    p.write_text(json.dumps(st))
    return str(p), now


def test_a_stale_status_says_how_stale_and_what_the_supervisor_knows(tmp_path):
    """REPRODUCED: state STALE carried no detail at all, so the alert read 'no detail recorded'."""
    p, now = status_file(tmp_path, age_s=1830)
    (tmp_path / "supervisor_status.json").write_text(json.dumps({"recorder_pid": None, "recorder_restarts": 7, "recorder_consecutive_failures": 5, "recorder_last_exit": 1}))
    obs = RM.recorder_observation(p, now=now)
    assert obs["state"] == "STALE" and obs["age_s"] == pytest.approx(1830.0)
    assert "1830" in obs["detail"] and "supervisor" in obs["detail"] and "restarts 7" in obs["detail"] and "last exit 1" in obs["detail"] and "not running" in obs["detail"]


def test_a_stale_status_without_a_supervisor_file_still_says_the_age(tmp_path):
    p, now = status_file(tmp_path, age_s=900)
    obs = RM.recorder_observation(p, now=now)
    assert obs["state"] == "STALE" and "900" in obs["detail"] and "no supervisor status" in obs["detail"]


def test_a_live_heartbeat_over_a_log_that_stopped_growing_is_not_persisting_not_recording(tmp_path):
    p, now = status_file(tmp_path, age_s=10, growth_age_s=900)
    obs = RM.recorder_observation(p, now=now)
    assert obs["state"] == "NOT_PERSISTING" and obs["growth_age_s"] == pytest.approx(900.0) and "900" in obs["detail"]


def test_a_healthy_recorder_is_still_recording_and_an_old_status_format_is_not_misread(tmp_path):
    p, now = status_file(tmp_path, age_s=10, growth_age_s=3)
    assert RM.recorder_observation(p, now=now)["state"] == "RECORDING"
    p2, now = status_file(tmp_path, age_s=10, with_progress=False)             # a recorder from before this change: no progress block, never NOT_PERSISTING
    assert RM.recorder_observation(p2, now=now)["state"] == "RECORDING"


def test_precedence_stale_over_fault_over_not_persisting(tmp_path):
    p, now = status_file(tmp_path, age_s=400, growth_age_s=999, threads_alive=False)
    assert RM.recorder_observation(p, now=now)["state"] == "STALE"
    p, now = status_file(tmp_path, age_s=10, growth_age_s=999, threads_alive=False)
    assert RM.recorder_observation(p, now=now)["state"] == "FAULT"


def test_the_history_line_carries_the_age_and_the_detail_for_every_non_recording_state(tmp_path):
    p, now = status_file(tmp_path, age_s=1830)
    rec = {"at": "2026-11-10T00:00:00Z", "outcome": "OK", "reasons": [], "recorder": RM.recorder_observation(p, now=now), "decision": {}}
    assert RM._history_line(str(tmp_path), rec) is True
    line = json.loads((tmp_path / "monitor.jsonl").read_text().splitlines()[-1])
    assert line["recorder_state"] == "STALE" and line["recorder_age_s"] == pytest.approx(1830.0) and "1830" in line["recorder_detail"]
    p2, now = status_file(tmp_path, age_s=10, growth_age_s=900)
    rec["recorder"] = RM.recorder_observation(p2, now=now)
    assert RM._history_line(str(tmp_path), rec) is True
    line = json.loads((tmp_path / "monitor.jsonl").read_text().splitlines()[-1])
    assert line["recorder_state"] == "NOT_PERSISTING" and line["recorder_growth_age_s"] == pytest.approx(900.0) and "has not grown" in line["recorder_detail"]


def test_a_stale_recorder_keeps_its_own_explanation_when_an_old_store_fault_is_in_the_file(tmp_path):
    """REPRODUCED (Round 16): a status file that stopped updating still holds the store's last fault.
    The history line wrote `fault or detail`, so the old 'free space below the reserve' fault REPLACED the
    reason the recorder was not reporting (the file's age and the supervisor's restarts), and the alert said
    'full disk' for a recorder whose process or heartbeat was dead. The state's explanation comes first."""
    p, now = status_file(tmp_path, age_s=3 * 86_400,
                         fault="StoreError: free space below the 536870912-byte reserve; not writing")
    (tmp_path / "supervisor_status.json").write_text(json.dumps(
        {"recorder_pid": None, "recorder_restarts": 7, "recorder_consecutive_failures": 5, "recorder_last_exit": 1}))
    rec = {"at": "2026-11-12T18:24:00Z", "outcome": "OK", "reasons": [],
           "recorder": RM.recorder_observation(p, now=now), "decision": {}}
    assert RM._history_line(str(tmp_path), rec) is True
    line = json.loads((tmp_path / "monitor.jsonl").read_text().splitlines()[-1])
    assert line["recorder_state"] == "STALE"
    detail = line["recorder_detail"]
    assert "s old" in detail and "supervisor" in detail, detail        # why it is not reporting
    assert "last fault:" in detail and "free space below" in detail, detail
    assert detail.index("s old") < detail.index("last fault:")


def test_the_history_line_says_where_the_free_space_figure_came_from(tmp_path):
    """A STALE recorder stopped updating its own free-space figure, so the monitor reads the volume itself
    (statvfs): the history must say so, or 510 MiB cannot be told from a dead recorder's last value."""
    p, now = status_file(tmp_path, age_s=1830)
    obs = RM.recorder_observation(p, now=now)
    assert obs["free_mb_source"] == "statvfs"
    rec = {"at": "2026-11-12T18:24:00Z", "outcome": "OK", "reasons": [], "recorder": obs, "decision": {}}
    assert RM._history_line(str(tmp_path), rec) is True
    line = json.loads((tmp_path / "monitor.jsonl").read_text().splitlines()[-1])
    assert line["recorder_free_mb_source"] == "statvfs"
    p2, now = status_file(tmp_path, age_s=10)
    rec["recorder"] = RM.recorder_observation(p2, now=now)
    assert RM._history_line(str(tmp_path), rec) is True
    line = json.loads((tmp_path / "monitor.jsonl").read_text().splitlines()[-1])
    assert line["recorder_free_mb_source"] == "recorder_status"


def test_a_fault_with_no_state_detail_is_still_reported_alone(tmp_path):
    p, now = status_file(tmp_path, age_s=10, fault="OSError: disk is full")
    rec = {"at": "2026-11-12T18:24:00Z", "outcome": "OK", "reasons": [],
           "recorder": RM.recorder_observation(p, now=now), "decision": {}}
    assert RM._history_line(str(tmp_path), rec) is True
    line = json.loads((tmp_path / "monitor.jsonl").read_text().splitlines()[-1])
    assert line["recorder_state"] == "FAULT" and line["recorder_detail"] == "OSError: disk is full"


def test_the_alert_names_the_age_and_the_supervisor_instead_of_no_detail_recorded(tmp_path):
    """End to end through the REAL history writer and the REAL alert rules: the alert semantics are unchanged (3 cycles not RECORDING = recorder_down), the text is now diagnostic."""
    import runner_alerts as RA
    p, now = status_file(tmp_path, age_s=1830)
    (tmp_path / "supervisor_status.json").write_text(json.dumps({"recorder_pid": None, "recorder_restarts": 7, "recorder_consecutive_failures": 5, "recorder_last_exit": 1}))
    base = time.mktime(time.strptime("2026-11-10T00:00:00", "%Y-%m-%dT%H:%M:%S"))
    for k in range(3):
        at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(base + 60 * k))
        rec = {"at": at, "outcome": "OK", "reasons": [], "recorder": RM.recorder_observation(p, now=now), "decision": {}}
        assert RM._history_line(str(tmp_path), rec) is True
    text = (tmp_path / "monitor.jsonl").read_text()
    alerts = [a for a in RA.evaluate(text, base + 200) if a.key == "recorder_down"]
    assert len(alerts) == 1, "the recorder_down alert must still fire: a stale recorder is never reported healthy"
    msg = alerts[0].text if hasattr(alerts[0], "text") else str(alerts[0])
    assert "1830 s old" in msg and "restarts 7" in msg and "no detail recorded" not in msg
    # and a RECORDING recorder raises nothing
    ok_p, now2 = status_file(tmp_path, age_s=5, growth_age_s=1)
    (tmp_path / "monitor.jsonl").unlink()
    for k in range(3):
        at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(base + 60 * k))
        assert RM._history_line(str(tmp_path), {"at": at, "outcome": "OK", "reasons": [], "recorder": RM.recorder_observation(ok_p, now=now2), "decision": {}})
    assert not [a for a in RA.evaluate((tmp_path / "monitor.jsonl").read_text(), base + 200) if a.key == "recorder_down"]


# ================================================================ second round: findings from the independent review, each verified in code first
import md_store as ST  # noqa: E402
import md_engine as EN  # noqa: E402


def test_the_heartbeat_does_not_wait_on_the_engine_lock(tmp_path):
    """REPRODUCED by review: status() called feed.view(), which takes engine.lock; a stalled writer (slow fsync, a long retention pass) froze the heartbeat and the watchdog then killed a recorder that was only waiting."""
    rec, store, _ = make(tmp_path)
    held, release = threading.Event(), threading.Event()

    def hold():
        with rec.engine.lock:
            held.set()
            release.wait(10)
    t = threading.Thread(target=hold, daemon=True)
    t.start()
    assert held.wait(3)
    done = threading.Event()
    th = threading.Thread(target=lambda: (rec.write_status(), done.set()), daemon=True)
    th.start()
    assert done.wait(2.0), "the heartbeat blocked on the engine lock"
    release.set()
    t.join(3)


def test_the_view_logger_and_connection_events_are_not_market_input(tmp_path):
    """REPRODUCED by review: the feed appends a VIEW record every 5 s and CONN events on reconnects, so the log grew with both streams DOWN and NOT_PERSISTING could not fire."""
    rec, store, wall = make(tmp_path)
    before_inputs, before_id = rec.engine.input_count, store.newest_id()
    rec.engine.log_view(1.0, wall)                                              # an audit VIEW record
    rec.engine.record(ST.CONN, wall_ms=wall, mono_s=2.0, payload=json.dumps({"event": "disconnected"}))
    assert store.newest_id() > before_id and rec.engine.input_count == before_inputs, "audit/connection records must not count as market input"
    rec.engine.record(ST.FRAME, wall_ms=wall, mono_s=3.0, category="linear", conn_id="c1", topic="orderbook.50.BTCUSDT", payload=json.dumps({"topic": "orderbook.50.BTCUSDT", "type": "delta", "data": {}}))
    assert rec.engine.input_count == before_inputs + 1


def test_growth_is_measured_on_market_input_so_a_dead_feed_with_a_live_view_logger_is_not_persisting(tmp_path):
    rec, store, _ = make(tmp_path)
    clock = {"ms": 1_700_000_000_000}
    rec.wall_ms = lambda: clock["ms"]
    rec.started_ms = rec._last_growth_ms = clock["ms"]
    rec.write_status()
    for _ in range(3):                                                           # 400 s of view records and reconnect events only
        rec.engine.log_view(1.0, clock["ms"])
        clock["ms"] += 140_000
    st = rec.write_status()
    assert st["progress"]["growth_age_s"] >= 400 and st["progress"]["newest_record_id"] > 0
    rec.engine.input_count += 1                                                  # a market frame lands
    clock["ms"] += 1000
    assert rec.write_status()["progress"]["growth_age_s"] == 0.0


def test_a_failed_commit_rolls_back_so_ids_stay_contiguous_and_nothing_phantom_is_persisted(tmp_path):
    """REPRODUCED by review: _next_id advanced before the commit; a failed commit (ENOSPC) left the id ahead and the row in the open transaction, so a later commit persisted a row the engine had reported unlogged."""
    store = ST.MarketStore(str(tmp_path / "md"), commit_every=1, environment="mainnet")
    store.append(ST.CONN, wall_ms=1_700_000_000_000, mono_s=1.0, payload="{}")
    good = store.newest_id()
    class FailingCommit:
        """A connection whose FIRST commit fails (sqlite3.Connection attributes are read-only, so wrap it)."""

        def __init__(self, conn):
            self._c, self.n = conn, 0

        def commit(self):
            self.n += 1
            if self.n == 1:
                raise OSError(28, "No space left on device")
            return self._c.commit()

        def __getattr__(self, name):
            return getattr(self._c, name)
    store._conn = FailingCommit(store._conn)
    with pytest.raises(OSError):
        store.append(ST.CONN, wall_ms=1_700_000_000_001, mono_s=2.0, payload="{}")
    assert store.newest_id() == good, "the durable id must not advance past a failed commit"
    store.append(ST.CONN, wall_ms=1_700_000_000_002, mono_s=3.0, payload="{}")        # the disk recovered
    assert store.newest_id() == good + 1 and store.count() == good + 1 and store.verify() == []
    store.close()


def test_emergency_prune_frees_the_oldest_closed_segment_keeps_the_chain_and_never_touches_the_newest(tmp_path):
    """REPRODUCED by review: age-based retention keeps retention_days+1 segments; a volume that fills inside that window sat in FAULT until a human acted."""
    store = ST.MarketStore(str(tmp_path / "md"), commit_every=1, environment="mainnet")
    day = 86_400_000
    base = 1_700_000_000_000
    for d in range(4):
        for k in range(3):
            store.append(ST.CONN, wall_ms=base + d * day + k, mono_s=float(d * 10 + k), payload="{}")
    assert len(store.segments()) == 4
    store.min_free_bytes = 10 ** 15                                              # the reserve is breached
    assert store.low_space() is True
    allow = lambda n: True      # noqa: E731 - a verified-backup predicate (default is deny; see test_emergency_prune.py)
    first = store.emergency_prune(allow, min_interval_s=0)
    assert len(first) == 1 and first[0] == min(first) and len(store.segments()) == 3 and store.emergency_prunes == 1
    store.emergency_prune(allow, min_interval_s=0)
    assert len(store.segments()) == 2
    assert store.emergency_prune(allow, min_interval_s=0) == [] and len(store.segments()) == 2, "never prune the newest or the segment being written"
    assert store.verify() == [], "the retained log must still verify from the pruned checkpoint"
    assert os.path.exists(store._manifest_path()) and len(open(store._manifest_path()).read().strip().splitlines()) == 2
    store.close()


def test_the_feed_runs_the_emergency_pass_only_when_the_reserve_is_breached(tmp_path):
    rec, store, _ = make(tmp_path)
    assert rec.feed.emergency_retention() == []                                  # plenty of space: nothing happens
    day = 86_400_000
    for d in range(3):
        store.append(ST.CONN, wall_ms=1_700_000_000_000 + d * day, mono_s=float(d), payload="{}")
    store.min_free_bytes = 10 ** 15
    import dataclasses
    rec.feed.cfg = dataclasses.replace(rec.feed.cfg, retention_days=2)
    # the scenario is a volume that fills INSIDE the retention window (nothing is expired by age): with the wall clock at the newest segment's day, the age pass that
    # now runs first finds nothing, and only the backup-key rule can free a day
    rec.feed.wall_ms = lambda: 1_700_000_000_000 + 2 * day
    assert rec.feed.emergency_retention() == [] and store.emergency_prunes == 0 and store.emergency_prunes_denied == 1      # no verified backup named: refused, counted
    rec.feed.emergency_may_prune = lambda n: True
    assert len(rec.feed.emergency_retention()) >= 1 and store.emergency_prunes >= 1


def test_a_failed_status_write_is_visible_on_stderr_and_leaves_no_temp_file(tmp_path, monkeypatch, capsys):
    rec, store, _ = make(tmp_path)
    monkeypatch.setattr(os, "replace", lambda *a, **k: (_ for _ in ()).throw(OSError(28, "No space left on device")))
    for _ in range(11):
        assert rec.write_status_guarded() is None
    monkeypatch.undo()
    err = capsys.readouterr().err
    assert err.count("status write failed") == 2 and "No space left" in err and "(10x)" in err          # the first failure and every 10th: visible in `fly logs`
    assert not [n for n in os.listdir(rec.dir) if ".tmp." in n], "a failed write left a temp file on a full volume"


def test_a_heartbeat_is_written_before_the_slow_start_work(tmp_path, monkeypatch):
    """A slow start must not be a silent one: the status exists even when the feed start never returns usefully."""
    rec, store, _ = make(tmp_path)
    monkeypatch.setattr(rec.feed, "start", lambda: (_ for _ in ()).throw(RuntimeError("feed start failed")))
    with pytest.raises(RuntimeError):
        rec.start()
    assert os.path.exists(os.path.join(rec.dir, MR.STATUS_FILE))


def test_no_runtime_status_file_is_committed_into_the_source_tree():
    root = os.path.dirname(BOT)
    assert not os.path.exists(os.path.join(BOT, "recorder_status.json")), "a stray runtime file in bot/"
    ign = open(os.path.join(root, ".gitignore"), encoding="utf-8").read()
    assert "bot/recorder_status.json" in ign and "bot/supervisor_status.json" in ign


# ---------------------------------------------------------------- the supervisor and the alert rules
def test_a_recorder_that_needed_the_watchdog_is_not_a_stable_run_so_the_backoff_keeps_growing(tmp_path):
    """REPRODUCED by review: a hung recorder lived >= stable_after_s, so _schedule_restart reset the failure count and supervisor_status showed 0-1 failures for a persistent fault."""
    import runner_supervisor as RS
    s = RS.Supervisor([sys.executable, "-c", "import time; time.sleep(3.5)"], [sys.executable, "-c", "import time; time.sleep(60)"], status_dir=str(tmp_path), poll_s=0.02,
                      backoff_base_s=0.05, backoff_max_s=0.3, stable_after_s=0.01, grace_s=2.0, watchdog_stale_s=0.25, watchdog_check_s=0.05)
    assert s.run() == 0
    assert s.recorder_watchdog_kills >= 3 and s.recorder_failures >= 3, (s.recorder_watchdog_kills, s.recorder_failures)


def history_rows(tmp_path, counters, base=1_794_312_000):
    import runner_alerts as RA
    lines = []
    for k, (restarts, kills) in enumerate(counters):
        at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(base + 600 * k))
        lines.append(json.dumps({"at": at, "outcome": "OK", "reasons": [], "recorder_state": "RECORDING", "recorder_restarts": restarts, "recorder_watchdog_kills": kills,
                                 "recorder_consecutive_failures": 2}, sort_keys=True))
    return RA, "\n".join(lines) + "\n", base + 600 * len(counters)


def test_a_recorder_that_is_recording_at_every_sample_but_keeps_being_restarted_alerts(tmp_path):
    RA, text, now = history_rows(tmp_path, [(3, 1), (3, 1), (4, 2), (4, 2), (5, 3)])
    alerts = [a for a in RA.evaluate(text, now) if a.key == "recorder_restarting"]
    assert len(alerts) == 1 and "restarts 3 -> 5" in alerts[0].text and "watchdog kills 1 -> 3" in alerts[0].text and "not healthy" in alerts[0].text


def test_a_steady_restart_counter_or_a_single_sample_raises_no_restart_alert(tmp_path):
    RA, text, now = history_rows(tmp_path, [(3, 1)] * 6)
    assert not [a for a in RA.evaluate(text, now) if a.key == "recorder_restarting"]
    RA, text, now = history_rows(tmp_path, [(9, 9)])
    assert not [a for a in RA.evaluate(text, now) if a.key == "recorder_restarting"]


def test_the_monitor_copies_the_supervisors_counters_into_the_history_line(tmp_path):
    p, now = status_file(tmp_path, age_s=10, growth_age_s=1)
    (tmp_path / "supervisor_status.json").write_text(json.dumps({"recorder_pid": 5, "recorder_restarts": 4, "recorder_watchdog_kills": 2, "recorder_consecutive_failures": 3}))
    rec = {"at": "2026-11-10T00:00:00Z", "outcome": "OK", "reasons": [], "recorder": RM.recorder_observation(p, now=now), "decision": {}}
    assert RM._history_line(str(tmp_path), rec) is True
    line = json.loads((tmp_path / "monitor.jsonl").read_text().splitlines()[-1])
    assert line["recorder_restarts"] == 4 and line["recorder_watchdog_kills"] == 2 and line["recorder_consecutive_failures"] == 3 and "recorder_recorder_restarts" not in line


# ---------------------------------------------------------------- the evaluate exit-code contract and label
def test_the_evaluate_exit_code_tells_a_decision_from_a_failure_to_decide(monkeypatch, capsys):
    import ms_testnet_autopilot as AP
    codes = {}
    for decision in AP.DECISIONS:
        monkeypatch.setattr(AP, "run_once", lambda *a, _d=decision, **k: {"result": "EVALUATED", "why": [], "decision": _d})
        codes[decision] = AP.main(["evaluate"], env={})
    capsys.readouterr()
    assert codes == {"WAIT_FOR_DATA": 3, "INCONCLUSIVE": 3, "SKIP_NO_OPPORTUNITY": 0, "REJECT_UNECONOMIC": 0, "ELIGIBLE_FOR_OWNER_TESTNET_DECISION": 0}
    monkeypatch.setattr(AP, "run_once", lambda *a, **k: {"result": "REFUSED", "why": ["X"]})
    assert AP.main(["evaluate"], env={}) == 3
    capsys.readouterr()


# ---- round 3: a failed commit loses the WHOLE open batch; progress counts only durable inputs
def _failing_commit_store(tmp_path):
    import md_store as ST
    import md_engine as EN
    import md_state as S

    st = ST.MarketStore(str(tmp_path / "fc"), commit_every=5, commit_interval_s=3600, environment="mainnet")
    eng = EN.Engine(S.MarketState("BTCUSDT", environment="mainnet"), st)
    eng.record("conn", wall_ms=1_699_999_999_000, mono_s=0.0, payload="{}")      # opens today's segment so there is a real connection to wrap
    st.flush()
    real = st._conn

    class Proxy:
        fail = False

        def __getattr__(self, n):
            return getattr(real, n)

        def commit(self):
            if Proxy.fail:
                raise OSError("disk full")
            return real.commit()
    st._conn = Proxy()
    return st, eng, Proxy


def test_progress_counts_only_committed_inputs_and_a_rollback_counts_every_lost_row(tmp_path):
    st, eng, proxy = _failing_commit_store(tmp_path)
    rec = lambda i: eng.record("frame", wall_ms=1_700_000_000_000 + i, mono_s=float(i), category="linear", conn_id="c", topic="tickers.BTCUSDT", payload="{}")   # noqa: E731
    rec(0); rec(1)
    assert eng.input_count == 0                       # appended, not yet durable
    proxy.fail = True
    for i in range(2, 5):
        rec(i)                                        # the fifth append triggers the commit, which fails: the WHOLE batch is gone
    assert eng.input_count == 0
    assert eng._unlogged >= 5, eng._unlogged          # every dropped row is accounted for, not just the one in flight
    proxy.fail = False
    rec(5)                                            # recovers: a store_gap marker is written, then the input
    assert eng._unlogged == 0


def test_a_full_volume_does_not_make_the_supervisor_watchdog_kill_a_live_recorder(tmp_path, monkeypatch):
    """Red-team MED: the watchdog measures the status file's mtime; a full disk stops the rewrite, so a perfectly alive recorder looked hung and was killed in a loop. A failed write now
    touches the existing file's mtime (metadata only) while its CONTENT stays stale, so the monitor still reports STALE."""
    rec, store, _ = make(tmp_path)
    assert rec.write_status_guarded() is not None
    path = os.path.join(rec.dir, MR.STATUS_FILE)
    old = time.time() - 4000
    os.utime(path, (old, old))
    before = open(path).read()
    monkeypatch.setattr(os, "replace", lambda *a, **k: (_ for _ in ()).throw(OSError(28, "No space left on device")))
    assert rec.write_status_guarded() is None
    monkeypatch.undo()
    assert time.time() - os.stat(path).st_mtime < 60, "mtime must show the process is alive"
    assert open(path).read() == before, "the content must stay stale: a status that could not be written is not a fresh status"


def test_the_disk_warning_fires_from_the_real_history_writer_not_just_from_hand_built_rows(tmp_path):
    """END TO END (the first version read the wrong key and was inert in production): a status file whose volume is within about 1 GiB of the 512 MiB reserve goes through the REAL
    ``recorder_observation`` and ``_history_line`` and the REAL alert rules."""
    import runner_alerts as RA

    def status(free_mib, now=1_700_000_000.0):
        st = {"now_ms": int(now * 1000), "feed": {"threads_alive": True}, "store": {"records": 5, "free_bytes": int(free_mib * 1048576), "fault": None}, "capacity": {"rows": 3},
              "progress": {"newest_record_id": 10, "growth_age_s": 1.0}}
        path = tmp_path / "recorder_status.json"
        path.write_text(json.dumps(st))
        return str(path), now
    base = time.mktime(time.strptime("2026-11-10T00:00:00", "%Y-%m-%dT%H:%M:%S"))
    for free, expect in ((700, True), (4000, False)):
        hist = tmp_path / "monitor.jsonl"
        if hist.exists():
            hist.unlink()
        p, now = status(free)
        for k in range(4):
            at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(base + 60 * k))
            assert RM._history_line(str(tmp_path), {"at": at, "outcome": "OK", "reasons": [], "recorder": RM.recorder_observation(p, now=now), "decision": {}}) is True
        line = json.loads(hist.read_text().splitlines()[-1])
        assert line["recorder_free_mb"] == pytest.approx(float(free)) and "free_mb" not in line       # the key the alert must read
        keys = [a.key for a in RA.evaluate(hist.read_text(), base + 300)]
        assert ("recorder_disk_low" in keys) is expect, (free, keys)


# ---------------------------------------------------------------- the monitor must not report a DEAD recorder's last free-space figure as the volume's current one
class _Vfs:
    def __init__(self, avail_mib, frsize=4096):
        self.f_frsize, self.f_bavail = frsize, int(avail_mib * 1048576 / frsize)


def test_a_stale_status_does_not_freeze_the_free_space_at_the_dead_recorders_last_figure(tmp_path, monkeypatch):
    """A recorder that stopped 6 days ago at 511 MiB free kept reporting 511 MiB as the volume's free space after the owner extended the volume (the history line is what recorder_disk_low reads). The monitor
    shares the recorder's container and volume, so when the status is STALE it reads the volume itself."""
    p, now = status_file(tmp_path, age_s=6 * 86_400)
    doc = json.load(open(p))
    doc["store"]["free_bytes"] = 511 * 1048576
    json.dump(doc, open(p, "w"))
    monkeypatch.setattr(RM.os, "statvfs", lambda path: _Vfs(4096))                     # the owner extended the volume: 4 GiB free now
    obs = RM.recorder_observation(p, now=now)
    assert obs["state"] == "STALE" and obs["free_mb"] == pytest.approx(4096.0) and obs["free_mb_source"] == "statvfs"


def test_a_fresh_status_keeps_the_recorders_own_figure(tmp_path, monkeypatch):
    p, now = status_file(tmp_path, age_s=5)
    doc = json.load(open(p))
    doc["store"]["free_bytes"] = 700 * 1048576
    json.dump(doc, open(p, "w"))
    monkeypatch.setattr(RM.os, "statvfs", lambda path: (_ for _ in ()).throw(AssertionError("a fresh status must not need the volume")))
    obs = RM.recorder_observation(p, now=now)
    assert obs["state"] == "RECORDING" and obs["free_mb"] == pytest.approx(700.0) and obs["free_mb_source"] == "recorder_status"


def test_a_stale_status_on_a_volume_the_monitor_cannot_read_says_unknown_never_the_old_figure(tmp_path, monkeypatch):
    p, now = status_file(tmp_path, age_s=6 * 86_400)
    doc = json.load(open(p))
    doc["store"]["free_bytes"] = 511 * 1048576
    json.dump(doc, open(p, "w"))

    def boom(path):
        raise OSError(5, "I/O error")
    monkeypatch.setattr(RM.os, "statvfs", boom)
    obs = RM.recorder_observation(p, now=now)
    assert obs["state"] == "STALE" and obs["free_mb"] is None and obs["free_mb_source"] == "unavailable"
