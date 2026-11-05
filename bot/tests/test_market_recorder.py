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


def test_old_capacity_files_are_pruned_by_retention_but_today_is_kept(tmp_path):
    rec, _, wall = make(tmp_path, retention_days=1)
    d = str(tmp_path / "md")
    for day in ("20200101", "20200102"):
        open(os.path.join(d, f"capacity-{day}.jsonl"), "w").write("{}\n")
    rec.capture_capacity()
    removed = rec.prune_capacity()
    assert sorted(removed) == ["capacity-20200101.jsonl", "capacity-20200102.jsonl"]
    assert os.path.exists(rec._capacity_path(wall))
    rec.retention_days = 0
    assert rec.prune_capacity() == []


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
