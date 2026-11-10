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
    _inject_event(rec)                                                         # a record lands
    clock["ms"] += 1000
    moved = rec.write_status()
    assert moved["progress"]["growth_age_s"] == 0.0 and moved["progress"]["newest_record_id"] > first["progress"]["newest_record_id"]


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
