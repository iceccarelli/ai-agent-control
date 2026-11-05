"""The supervisor: the monitor is primary, the recorder is restarted, signals are forwarded."""
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

import runner_supervisor as RS  # noqa: E402

PY = sys.executable


def sup(monitor, recorder, tmp_path, **kw):
    kw.setdefault("poll_s", 0.02)
    kw.setdefault("backoff_base_s", 0.05)
    kw.setdefault("backoff_max_s", 0.2)
    kw.setdefault("grace_s", 3.0)
    return RS.Supervisor([PY, "-c", monitor], None if recorder is None else [PY, "-c", recorder],
                         status_dir=str(tmp_path), **kw)


def test_the_supervisor_exits_with_the_monitors_code_and_stops_the_recorder(tmp_path):
    s = sup("import sys,time; time.sleep(0.3); sys.exit(7)", "import time; time.sleep(60)", tmp_path)
    assert s.run() == 7
    assert s.recorder is None or s.recorder.poll() is not None


def test_a_dying_recorder_is_restarted_and_never_disturbs_the_monitor(tmp_path):
    s = sup("import time; time.sleep(1.5)", "import sys; sys.exit(3)", tmp_path)
    rc = s.run()
    assert rc == 0
    assert s.recorder_restarts >= 2 and s.recorder_last_exit == 3
    status = json.load(open(os.path.join(str(tmp_path), RS.STATUS_FILE)))
    assert status["recorder_restarts"] == s.recorder_restarts and status["recorder_enabled"] is True


def test_a_recorder_that_cannot_even_start_does_not_stop_the_monitor(tmp_path):
    s = RS.Supervisor([PY, "-c", "import time; time.sleep(0.6)"], ["/no/such/binary"],
                      status_dir=str(tmp_path), poll_s=0.02, backoff_base_s=0.05, backoff_max_s=0.1)
    assert s.run() == 0


def test_backoff_grows_then_resets_after_a_stable_run(tmp_path):
    t = {"now": 0.0}
    s = RS.Supervisor([PY, "-c", "pass"], [PY, "-c", "pass"], backoff_base_s=5, backoff_max_s=40,
                      stable_after_s=100, mono=lambda: t["now"])
    delays = []
    for lived in (1, 1, 1, 1, 1, 1):
        s._schedule_restart(lived)
        delays.append(s.recorder_next_start - t["now"])
    assert delays == [5, 10, 20, 40, 40, 40]
    s._schedule_restart(500.0)                      # it ran stably: the penalty is forgiven
    assert s.recorder_failures == 0 and s.recorder_next_start - t["now"] == 5


def test_sigterm_is_forwarded_to_both_children_and_the_supervisor_reports_143(tmp_path):
    marker = tmp_path / "m"
    child = ("import signal,sys,time,os;"
             f"signal.signal(signal.SIGTERM, lambda *a: (open(r'{marker}'+sys.argv[1],'w').write('t'), sys.exit(0)));"
             "time.sleep(60)")
    mon = child.replace("sys.argv[1]", "'mon'")
    rec = child.replace("sys.argv[1]", "'rec'")
    s = sup(mon, rec, tmp_path)
    t = threading.Timer(0.6, s.request_stop)
    t.start()
    assert s.run() == RS.EXIT_SIGNALLED
    assert (tmp_path / "mmon").exists() and (tmp_path / "mrec").exists()


def test_a_child_that_ignores_sigterm_is_killed_after_the_grace_period(tmp_path):
    stubborn = "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)"
    s = sup(stubborn, None, tmp_path, grace_s=0.4)
    threading.Timer(0.5, s.request_stop).start()
    t0 = time.time()
    assert s.run() == RS.EXIT_SIGNALLED
    assert time.time() - t0 < 10 and s.monitor.poll() is not None


def test_without_the_recorder_flag_it_replaces_itself_with_the_monitor(tmp_path):
    out = subprocess.run([PY, os.path.join(BOT, "tools", "runner_supervisor.py"), "--", PY, "-c",
                          "import os,sys; print('ok', os.getpid()); sys.exit(5)"], capture_output=True, text=True)
    assert out.returncode == 5 and out.stdout.startswith("ok")


def test_usage_errors_exit_3():
    assert RS.main([]) == 3
    assert RS.main(["--recorder"]) == 3
    assert RS.main(["--"]) == 3
