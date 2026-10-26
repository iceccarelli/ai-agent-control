"""One monitor cycle and the outcome classifier: evidence from THIS cycle only.

Mutation testing left survivors in `run_outcomes.py` and `runner_monitor.py`
that `test_runner_monitor.py` does not kill (the probe's retry loop, the
"never raises" contract of the classifier, `resolved` being literally true), and
reading `monitor_once` found one way to a green reading from stale evidence:

    run_child opens the preflight log in APPEND mode, and the monitor then reads
    whatever report is in it. If the child cannot be started (the runner
    raises) in a run directory that already holds an earlier cycle's report
    (`--once` against a reused `--run-dir`), that EARLIER report was classified
    as this cycle's: MONITOR_READY / ENTRY_NOT_ELIGIBLE with a fresh timestamp
    and no preflight having run. The deployed loop uses a fresh directory per
    cycle, so this is the operator's `--once` path, not the default.

The log is now emptied before the child runs, and a log that cannot be emptied
fails the cycle. Only refusals are added; nothing else moves.
"""
from __future__ import annotations

import json
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
BOT = os.path.dirname(HERE)
sys.path.insert(0, BOT)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(BOT, "tools"))

import run_outcomes as RO  # noqa: E402
import runner_fixtures as RF  # noqa: E402
import runner_monitor as RM  # noqa: E402
import unattended_drill as UD  # noqa: E402

ENV = {"BYBIT_API_KEY": "k", "BYBIT_API_SECRET": "s", "PATH": "/usr/bin"}


def cycle(tmp_path, runner, *, prober=lambda: 200, sleep=lambda s: None):
    code = RM.monitor_once(str(tmp_path / "run"), env=ENV, prober=prober,
                           runner=runner, sleep=sleep, history_root=str(tmp_path))
    return code, json.load(open(tmp_path / "run" / "monitor.json"))


class Raises:
    calls = 0

    def __call__(self, argv, env, *, log_path, timeout_s=None):
        Raises.calls += 1
        raise OSError("cannot start the preflight")


class Appends:
    """The real run_child opens the log in append mode."""

    def __init__(self, report):
        self.report = report

    def __call__(self, argv, env, *, log_path, timeout_s=None):
        with open(log_path, "a") as fh:
            fh.write(json.dumps(self.report) + "\n")
        return 0


def leave_an_earlier_report(tmp_path, state="PASS"):
    run = tmp_path / "run"
    run.mkdir(parents=True, exist_ok=True)
    (run / "preflight.log").write_text(
        json.dumps(RF.preflight_report(state)) + "\n")


@pytest.mark.parametrize("state", ["PASS", "REFUSED"])
def test_a_child_that_cannot_start_does_not_inherit_an_earlier_report(tmp_path, state):
    leave_an_earlier_report(tmp_path, state)
    code, rec = cycle(tmp_path, Raises())
    assert rec["outcome"] == RO.MONITOR_FAILED, rec
    assert code == RM.EXIT_FAILED


def test_this_cycles_report_is_read_when_the_log_held_an_earlier_one(tmp_path):
    """Append mode plus an earlier report used to make two reports, which the
    parser rightly refuses as ambiguous: a good cycle read as a failure."""
    leave_an_earlier_report(tmp_path, "REFUSED")
    code, rec = cycle(tmp_path, Appends(RF.preflight_report("PASS")))
    assert rec["outcome"] == RO.MONITOR_READY and code == RM.EXIT_OK


def test_a_log_that_cannot_be_emptied_fails_the_cycle(tmp_path, monkeypatch):
    leave_an_earlier_report(tmp_path)
    real_open = open

    def refusing(path, mode="r", *a, **kw):
        if str(path).endswith("preflight.log") and "w" in mode:
            raise PermissionError("read-only")
        return real_open(path, mode, *a, **kw)
    monkeypatch.setattr("builtins.open", refusing)
    code, rec = cycle(tmp_path, Appends(RF.preflight_report("PASS")))
    assert rec["outcome"] == RO.MONITOR_FAILED and code == RM.EXIT_FAILED


# -- the reachability probe retries, then gives up ----------------------------

def test_a_probe_that_recovers_within_its_retries_runs_the_cycle(tmp_path):
    answers = iter([503, 0, 200])
    slept = []
    code, rec = cycle(tmp_path, Appends(RF.preflight_report("PASS")),
                      prober=lambda: next(answers), sleep=slept.append)
    assert rec["outcome"] == RO.MONITOR_READY and code == RM.EXIT_OK
    assert slept == [UD.PROBE_PAUSE_S] * 2


def test_a_probe_that_never_answers_gives_up_after_its_tries(tmp_path):
    seen = []

    class Never:
        def __call__(self):
            seen.append(1)
            return 503

    runner = Appends(RF.preflight_report("PASS"))
    code, rec = cycle(tmp_path, runner, prober=Never())
    assert code == RM.EXIT_UNREACHABLE and rec["outcome"] == RO.MONITOR_FAILED
    assert len(seen) == UD.PROBE_TRIES


# -- the classifier never raises, and `resolved` is literally true -------------

@pytest.mark.parametrize("report", [
    {"verdict": "PREFLIGHT_BLOCKED", "blockers": 5},
    {"verdict": "PREFLIGHT_BLOCKED", "blockers": object()},
    {"verdict": "PREFLIGHT_OK", "blockers": [], "venue": "testnet",
     "use_testnet": True, "order_gate_would_allow_orders": False,
     "entry_economics": {"state": ["not", "hashable"], "reason": {}}},
    {"verdict": ["PREFLIGHT_OK"]},
])
def test_classify_preflight_fails_closed_instead_of_raising(report):
    r = RO.classify_preflight(report, 0)
    assert r["outcome"] == RO.MONITOR_FAILED and r["order_sent"] is False


@pytest.mark.parametrize("resolved", [1, "true", "yes", [True], None, 0.5])
def test_a_cycle_is_resolved_only_when_resolved_is_literally_true(resolved):
    row = {"provenance": "VENUE_RECONCILED", "resolved": resolved}
    assert RO.cycle_reconciled(
        {"schema": "evidence_reconciliation/1", "carry_cycle": [row]}) is False


def test_a_cycle_with_resolved_true_is_reconciled():
    row = {"provenance": "VENUE_RECONCILED", "resolved": True}
    assert RO.cycle_reconciled(
        {"schema": "evidence_reconciliation/1", "carry_cycle": [row]}) is True


# -- a history that cannot be written is said so, and a second loop is refused --

def test_a_history_line_that_cannot_be_written_does_not_crash_the_cycle_but_is_said(
        tmp_path):
    """The history is the operator's only view of the runner. Losing a line is
    survivable (staleness will show it), losing it silently is not: the reason
    goes to the cycle's own log."""
    ro = tmp_path / "ro"
    ro.mkdir()
    (ro / "monitor.jsonl").mkdir()            # opening it for append fails
    code = RM.monitor_once(str(tmp_path / "run"), env=ENV, prober=lambda: 200,
                           runner=Appends(RF.preflight_report("PASS")),
                           sleep=lambda s: None, history_root=str(ro))
    assert code == RM.EXIT_OK
    said = open(tmp_path / "run" / "orchestrator.log").read()
    assert "history" in said and "could not be written" in said


def test_a_second_loop_on_the_same_run_root_is_refused(monkeypatch, tmp_path):
    monkeypatch.setenv("BYBIT_API_KEY", "k")
    monkeypatch.setenv("BYBIT_API_SECRET", "s")
    reached = []
    monkeypatch.setattr(RM, "loop", lambda root, **kw: reached.append(root) or 0)
    monkeypatch.setattr(RM.UD, "install_signal_handlers", lambda: None)
    monkeypatch.setattr(RM.UD, "acquire_lock", lambda d: None)
    code = RM.main(["--run-dir", str(tmp_path / "root"), "--loop",
                    "--interval-minutes=60"])
    assert code == RM.EXIT_REFUSED and reached == []
