"""A bug in one cycle must not end the runner.

The loop used to catch only a stop signal. Any other exception out of a cycle
ended the process; Fly restarts a crashed machine only `retries = 10` times for
the whole life of the deployment, after which it stays stopped, and a stopped
process cannot page anyone. Now a cycle that crashes is recorded as a
MONITOR_FAILED history line (the existing failed-streak alert then pages), the
loop sleeps its interval and tries again. A stop signal still ends it with 143,
and nothing here can send an order: the monitor never could.
"""
from __future__ import annotations

import json
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
BOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(BOT, "tools"))
sys.path.insert(0, BOT)

import runner_alerts as RA  # noqa: E402
import runner_monitor as RM  # noqa: E402
import unattended_drill as UD  # noqa: E402
from test_runner_alerts import ENV, NOW, Log, Wire  # noqa: E402

SECRET = "SUPERSECRETKEY-0123456789"


class Cycles:
    """A fake `once`: each call does what the script says."""

    clock = staticmethod(lambda: NOW)

    def __init__(self, *script):
        self.script, self.calls = list(script), 0

    def __call__(self, run_dir, *, env, history_root, **_):
        self.calls += 1
        step = self.script.pop(0) if self.script else "ok"
        if isinstance(step, BaseException):
            raise step
        os.makedirs(run_dir, exist_ok=True)
        with open(os.path.join(history_root, "monitor.jsonl"), "a") as fh:
            fh.write(json.dumps({"at": _stamp(self.clock() - 30.0), "outcome": "ENTRY_NOT_ELIGIBLE",
                                 "order_sent": False, "reasons": []}) + "\n")
        return 0


def _stamp(t):
    import time
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


def run(tmp_path, once, *, max_runs, notifier=None, prune=None, monkeypatch=None):
    os.makedirs(str(tmp_path), exist_ok=True)
    t = {"v": NOW}

    def clock():
        return t["v"]
    if isinstance(once, Cycles):
        once.clock = clock                      # its history lines use the loop's time

    def sleep(s):
        t["v"] += s
    code = RM.loop(str(tmp_path), env={}, interval_s=3600.0, max_runs=max_runs,
                   clock=clock, sleep=sleep, once=once, notifier=notifier)
    return code


def history(tmp_path):
    p = tmp_path / "monitor.jsonl"
    return [json.loads(l) for l in p.read_text().splitlines()] if p.exists() else []


def log(tmp_path):
    p = tmp_path / "orchestrator.log"
    return p.read_text() if p.exists() else ""


class TestACrashedCycleIsRecordedAndSurvived:
    def test_the_loop_keeps_going_after_an_unexpected_exception(self, tmp_path):
        once = Cycles("ok", RuntimeError("boom"), "ok")
        code = run(tmp_path, once, max_runs=3)
        assert once.calls == 3 and code == 0

    def test_the_crash_is_a_monitor_failed_history_line_that_says_so(self, tmp_path):
        run(tmp_path, Cycles("ok", RuntimeError("boom"), "ok"), max_runs=3)
        bad = [r for r in history(tmp_path) if r["outcome"] == "MONITOR_FAILED"]
        assert len(bad) == 1
        assert bad[0]["order_sent"] is False
        assert bad[0]["reasons"][0].startswith("the monitor cycle crashed: RuntimeError")
        assert "test_monitor_self_heal.py" in bad[0]["reasons"][0]      # where, not what

    def test_the_exception_message_is_never_recorded(self, tmp_path):
        run(tmp_path, Cycles(RuntimeError(f"api key={SECRET}")), max_runs=1)
        assert SECRET not in (tmp_path / "monitor.jsonl").read_text()
        assert SECRET not in log(tmp_path)

    def test_the_log_names_the_crash_and_that_the_loop_continues(self, tmp_path):
        run(tmp_path, Cycles(ValueError("x")), max_runs=1)
        assert "cycle crashed (ValueError)" in log(tmp_path)

    def test_a_crash_on_the_last_cycle_gives_a_failing_exit_code(self, tmp_path):
        assert run(tmp_path, Cycles("ok", RuntimeError("x")), max_runs=2) == RM.EXIT_FAILED

    def test_a_crash_every_cycle_still_never_ends_the_loop_early(self, tmp_path):
        once = Cycles(*[RuntimeError("x")] * 5)
        assert run(tmp_path, once, max_runs=5) == RM.EXIT_FAILED and once.calls == 5

    def test_it_still_waits_the_interval_after_a_crash(self, tmp_path):
        naps = []
        os.makedirs(str(tmp_path), exist_ok=True)
        RM.loop(str(tmp_path), env={}, interval_s=3600.0, max_runs=2,
                clock=lambda: NOW, sleep=naps.append,
                once=Cycles(RuntimeError("x"), "ok"))
        assert sum(naps) >= 3600.0              # no hot crash loop

    def test_a_stop_signal_inside_a_cycle_still_ends_the_loop_with_143(self, tmp_path):
        assert run(tmp_path, Cycles(UD.Shutdown("SIGTERM")), max_runs=3) == RM.EXIT_SIGNALLED

    def test_a_keyboard_interrupt_is_not_swallowed(self, tmp_path):
        with pytest.raises(KeyboardInterrupt):
            run(tmp_path, Cycles(KeyboardInterrupt()), max_runs=3)


class TestItPagesThroughTheExistingAlerts:
    def test_two_crashes_in_a_row_raise_the_failed_streak_alert(self, tmp_path):
        wire = Wire()
        n = RA.Notifier(ENV, transport=wire, say=Log(), sleep=lambda s: None)
        run(tmp_path, Cycles(RuntimeError("a"), RuntimeError("b")), max_runs=2, notifier=n)
        pages = [m[1]["text"] for m in wire.sent if "[WARNING]" in m[1]["text"]]
        assert len(pages) == 1 and "crashed" in pages[0]

    def test_one_crash_is_a_blip_and_does_not_page(self, tmp_path):
        wire = Wire()
        n = RA.Notifier(ENV, transport=wire, say=Log(), sleep=lambda s: None)
        run(tmp_path, Cycles("ok", RuntimeError("a"), "ok"), max_runs=3, notifier=n)
        assert not [m for m in wire.sent if "[WARNING]" in m[1]["text"]]


class TestTheRestOfTheCycleIsProtectedToo:
    def test_a_failure_pruning_old_runs_does_not_end_the_loop(self, tmp_path, monkeypatch):
        monkeypatch.setattr(UD, "prune_runs", lambda root: (_ for _ in ()).throw(OSError("disk")))
        once = Cycles("ok", "ok")
        assert run(tmp_path, once, max_runs=2) == 0 and once.calls == 2
        assert "prune failed (OSError)" in log(tmp_path)

    def test_writing_the_crash_line_to_a_full_disk_does_not_end_the_loop(self, tmp_path, monkeypatch):
        monkeypatch.setattr(RM, "_history_line", lambda root, record: False)
        once = Cycles(RuntimeError("x"), "ok")
        assert run(tmp_path, once, max_runs=2) == 0 and once.calls == 2
        assert "could not be written" in log(tmp_path)
