"""The recorder service: it records, bounds itself, measures capacity and reports its health."""
import json
import os
import signal
import subprocess
import sys
import threading
import time

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
BOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(BOT, "tools"))

import md_engine as EN  # noqa: E402
import md_feed as F  # noqa: E402
import md_state as S  # noqa: E402
import md_store as ST  # noqa: E402
import market_recorder as MR  # noqa: E402
from md_helpers import Factory, ScriptedWS  # noqa: E402
from test_md_integration import complete_state  # noqa: E402


def make(tmp_path, **kw):
    st, now, wall = complete_state()
    store = ST.MarketStore(str(tmp_path / "md"), commit_every=1, environment="mainnet",
                           min_free_bytes=kw.pop("min_free_bytes", 0))
    eng = EN.Engine(st, store)
    feed = F.Feed(F.FeedConfig(retention_days=kw.get("retention_days", 0)), eng,
                  Factory(ScriptedWS([("timeout",)] * 5), ScriptedWS([("timeout",)] * 5)), None,
                  mono=lambda: now, wall_ms=lambda: wall)
    rec = MR.Recorder(eng, feed, str(tmp_path / "md"), environment="mainnet",
                      min_free_bytes=store.min_free_bytes, retention_days=kw.get("retention_days", 0),
                      wall_ms=lambda: wall, mono=lambda: now)
    return rec, store, wall


def test_a_capacity_row_is_appended_by_day_and_carries_the_views_own_refusals(tmp_path):
    rec, store, wall = make(tmp_path)
    row = rec.capture_capacity()
    assert row is not None and rec.capacity_rows == 1 and rec.capacity_errors == 0
    path = rec._capacity_path(wall)
    lines = open(path).read().splitlines()
    assert len(lines) == 1
    saved = json.loads(lines[0])
    assert saved["symbol"] == "BTCUSDT" and "curve" in saved and isinstance(saved["entry_refusals"], list)
    rec.capture_capacity()
    assert len(open(path).read().splitlines()) == 2


def test_a_failing_capacity_capture_is_counted_not_raised(tmp_path, monkeypatch):
    rec, _, _ = make(tmp_path)
    monkeypatch.setattr(MR.MC, "capacity_row", lambda *a, **k: (_ for _ in ()).throw(ValueError("boom")))
    assert rec.capture_capacity() is None
    assert rec.capacity_errors == 1 and "ValueError" in rec.last_capacity_error


def test_status_is_atomic_complete_and_secret_free(tmp_path):
    rec, store, wall = make(tmp_path, min_free_bytes=1024, retention_days=2)
    rec.capture_capacity()
    st = rec.write_status()
    on_disk = json.load(open(os.path.join(str(tmp_path / "md"), MR.STATUS_FILE)))
    assert on_disk["schema"] == MR.SCHEMA and on_disk["environment"] == "mainnet"
    assert on_disk["store"]["min_free_bytes"] == 1024 and on_disk["store"]["retention_days"] == 2
    assert on_disk["store"]["fault"] is None and on_disk["capacity"]["rows"] == 1
    assert "threads_alive" in on_disk["feed"]
    assert not [f for f in os.listdir(str(tmp_path / "md")) if ".tmp." in f]
    text = json.dumps(on_disk).lower()
    for word in ("secret", "apikey", "api_key", "signature", "token"):
        assert word not in text


def test_capacity_files_outlive_the_depth_log_and_are_pruned_by_their_own_retention(tmp_path):
    """The depth log keeps 1 day here; the capacity curve (the persistence record) must not follow it."""
    rec, _, wall = make(tmp_path, retention_days=1)
    d = str(tmp_path / "md")
    yesterday = ST._utc_day(wall - 86_400_000)
    ancient = "20200101"
    for day in (ancient, yesterday):
        open(os.path.join(d, f"capacity-{day}.jsonl"), "w").write("{}\n")
    rec.capture_capacity()
    assert rec.capacity_retention_days == 365 and rec.retention_days == 1
    removed = rec.prune_capacity()
    assert removed == [f"capacity-{ancient}.jsonl"]              # only the truly old one
    assert os.path.exists(os.path.join(d, f"capacity-{yesterday}.jsonl"))
    assert os.path.exists(rec._capacity_path(wall))
    rec.capacity_retention_days = 0
    assert rec.prune_capacity() == []                            # 0 keeps everything


def test_the_service_flag_sets_the_capacity_retention_and_the_status_reports_it(tmp_path):
    rec = MR.build(str(tmp_path / "svc"), capacity_retention_days=90, use_rest=False)
    try:
        assert rec.capacity_retention_days == 90
        assert rec.status()["store"]["capacity_retention_days"] == 90
    finally:
        rec.engine.store.close()


def test_the_run_loop_writes_status_and_capacity_then_stops_cleanly(tmp_path):
    rec, store, _ = make(tmp_path)
    stop = threading.Event()
    rec.feed.start()
    t = threading.Thread(target=rec.run, args=(stop,), kwargs=dict(status_every_s=0.01, capacity_every_s=0.01))
    t.start()
    for _ in range(100):
        if rec.capacity_rows and os.path.exists(os.path.join(rec.dir, MR.STATUS_FILE)):
            break
        time.sleep(0.02)
    stop.set(); t.join(5)
    assert not t.is_alive() and rec.capacity_rows >= 1
    rec.stop()
    assert store.verify() == []


def test_the_service_runs_as_a_process_and_exits_zero_on_sigterm(tmp_path):
    """No network: the transport is unreachable, which is the realistic outage. The service must
    stay up, write its status, and shut down cleanly."""
    d = str(tmp_path / "svc")
    env = dict(os.environ, PYTHONPATH=BOT)
    p = subprocess.Popen([sys.executable, os.path.join(BOT, "tools", "market_recorder.py"), "--dir", d,
                          "--no-rest", "--status-every", "0.2", "--capacity-every", "0.2",
                          "--min-free-mb", "1", "--proxy-url", "http://127.0.0.1:9"],
                         env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        status = os.path.join(d, MR.STATUS_FILE)
        for _ in range(150):
            if os.path.exists(status):
                break
            time.sleep(0.1)
        assert os.path.exists(status), p.poll()
        assert p.poll() is None
        p.send_signal(signal.SIGTERM)
        assert p.wait(20) == 0
    finally:
        if p.poll() is None:
            p.kill()
    final = json.load(open(status))
    assert final["schema"] == MR.SCHEMA


# -- schema v2, the chain and the externally copied head ----------------------------------------------------

def test_the_recorder_writes_validated_chained_v2_rows_with_source_identity(tmp_path):
    import capacity_schema as CSM
    import evidence_chain as EC
    rec, store, wall = make(tmp_path)
    for _ in range(3):
        rec.wall_ms = (lambda w=wall: w)
        rec.capture_capacity()
    path = rec._capacity_path(wall)
    v = EC.verify(path)
    assert v["ok"] and v["n_rows"] == 3 and rec.chain_head == {"file": os.path.basename(path), "n": 3, "head": v["head"]["head"]}
    rows = EC.rows_of(EC.read_lines(path)[0])
    row = rows[0]
    assert row["schema"] == "capacity/2" and row["venue"] == "bybit" and row["source"]["store_record_id"] >= 0
    assert isinstance(row["source"]["perp_update_id"], int) and row["funding"]["next_settlement_ms"] > row["at_ms"]
    assert row["recorder"]["store_fault"] is None and row["recorder"]["free_bytes"] is not None
    # the same row ids repeat here because the fake clock and store do not move; the validator says so
    assert any(p["code"] == CSM.DUPLICATE_ID for p in CSM.validate_stream(rows)["problems"])


def test_the_status_publishes_the_chain_head_and_the_monitor_copies_it_into_its_history(tmp_path):
    import runner_monitor as RM
    rec, store, wall = make(tmp_path)
    rec.capture_capacity()
    st = rec.write_status()
    assert st["capacity"]["chain_head"]["n"] == 1
    obs = RM.recorder_observation(os.path.join(rec.dir, MR.STATUS_FILE), now=wall / 1000.0 + 1)
    assert obs["chain_head"] == st["capacity"]["chain_head"]["head"] and obs["chain_n"] == 1
    root = str(tmp_path / "hist")
    os.makedirs(root)
    RM._history_line(root, {"at": "t", "outcome": "ENTRY_NOT_ELIGIBLE", "reasons": [], "recorder": obs})
    line = json.loads(open(os.path.join(root, "monitor.jsonl")).read().splitlines()[-1])
    assert line["recorder_chain_head"] == obs["chain_head"] and line["recorder_chain_n"] == 1


def test_a_crash_between_a_row_and_its_chain_entry_is_resealed_visibly_at_start(tmp_path):
    import evidence_chain as EC
    rec, store, wall = make(tmp_path)
    rec.capture_capacity()
    path = rec._capacity_path(wall)
    open(path, "ab").write(EC.canonical({"schema": "capacity/2", "stub": True}) + b"\n")
    assert EC.verify(path)["unchained_tail"] == 1
    rec.start()
    try:
        assert rec._resealed == 1 and EC.verify(path)["ok"]
    finally:
        rec.stop()


def test_pruning_removes_a_days_chain_and_head_with_its_evidence_file(tmp_path):
    rec, _, wall = make(tmp_path, retention_days=1)
    rec.capacity_retention_days = 1
    d = rec.dir
    for suffix in ("", ".chain", ".head"):
        open(os.path.join(d, f"capacity-20200101.jsonl{suffix}"), "w").write("{}")
    removed = rec.prune_capacity()
    assert removed == ["capacity-20200101.jsonl"]
    assert not [f for f in os.listdir(d) if f.startswith("capacity-20200101")]


def test_a_real_recorder_series_validates_as_a_stream_when_the_log_and_clock_advance(tmp_path):
    """The earlier test shows DUPLICATE_ID on a frozen clock; this is the positive control the review asked for:
    rows captured as the log grows and time passes pass the strict stream validator with no problem at all."""
    import capacity_schema as CSM
    import evidence_chain as EC
    rec, store, wall = make(tmp_path)
    t = {"w": wall}
    rec.wall_ms = lambda: t["w"]
    for _ in range(6):
        store._next_id = getattr(store, "_next_id", 1) + 1          # a new market event was logged
        t["w"] += 300_000                                            # five minutes later
        assert rec.capture_capacity() is not None
    path = rec._capacity_path(t["w"])
    rows = EC.rows_of(EC.read_lines(path)[0])
    v = CSM.validate_stream(rows)
    assert v["problems"] == [] and v["stream_ok"] and len(v["accepted"]) == 6 and v["n_rejected"] == 0


def test_capture_after_a_torn_write_repairs_visibly_and_keeps_recording(tmp_path):
    import evidence_chain as EC
    rec, store, wall = make(tmp_path)
    t = {"w": wall}
    rec.wall_ms = lambda: t["w"]
    assert rec.capture_capacity() is not None
    path = rec._capacity_path(t["w"])
    open(path, "ab").write(b'{"torn": tr')                             # power cut mid-row
    store._next_id = getattr(store, "_next_id", 1) + 1
    t["w"] += 300_000
    assert rec.capture_capacity() is not None
    v = EC.verify(path)
    assert v["ok"] and v["n_rows"] == 2 and rec._repairs == 1
    assert open(path + ".torn", "rb").read().startswith(b'{"torn": tr')
    assert rec.status()["capacity"]["repairs_while_running"] == 1
