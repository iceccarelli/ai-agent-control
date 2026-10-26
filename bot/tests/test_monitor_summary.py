"""`monitor_summary.py` and `scripts/runner_status.sh`: the operator's read-only
health report of the deployed runner.

Offline. The history is scripted; `fly` is a fake on PATH that serves canned
output, and nothing here deploys, starts, stops or orders anything.
"""
from __future__ import annotations

import calendar
import json
import os
import stat
import subprocess
import sys
import time

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
BOT = os.path.dirname(HERE)
ROOT = os.path.dirname(BOT)
sys.path.insert(0, os.path.join(BOT, "tools"))
sys.path.insert(0, HERE)

import monitor_summary as MS  # noqa: E402
from test_fly_machines import MACHINE_LIST, VOLUMES_ONE  # noqa: E402

NOW = calendar.timegm((2026, 10, 26, 12, 0, 0))


def stamp(minutes_ago):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(NOW - minutes_ago * 60))


def line(minutes_ago, outcome="ENTRY_NOT_ELIGIBLE", **kw):
    row = {"at": stamp(minutes_ago), "outcome": outcome, "order_sent": False,
           "reasons": [kw.pop("reason", "ENTRY_BASIS_EXCEEDS_CARRY_BUDGET")],
           "gate_reason": "ENTRY_BASIS_EXCEEDS_CARRY_BUDGET",
           "shadow_state": "STEP_RECORDED", "basis_bps": 100.0,
           "basis_budget_bps": 79.0, "smoothed_funding_bps": 1.0}
    row.update(kw)
    return row


def text(*rows):
    return "\n".join(json.dumps(r) for r in rows) + "\n"


def summary(*rows, **kw):
    return MS.summarise(MS.parse_lines(text(*rows)), now=NOW, **kw)


class TestHealth:
    def test_a_recent_clean_history_is_ok(self):
        s = summary(line(130), line(70), line(10))
        assert s["health"] == "OK" and s["exit"] == 0 and s["problems"] == []
        assert s["cycles_total"] == 3 and s["last_cycle_age_minutes"] == 10.0

    def test_a_stale_history_means_the_runner_is_down_or_stuck(self):
        s = summary(line(400), line(340))
        assert s["exit"] == 1 and "STALE" in s["problems"][0]
        assert summary(line(400), stale_minutes=500)["exit"] == 0

    def test_a_failed_last_cycle_is_a_problem_with_its_reasons(self):
        s = summary(line(70), line(5, "MONITOR_FAILED", reason="equity unreadable"))
        assert s["exit"] == 1 and "equity unreadable" in " ".join(s["problems"])

    def test_a_failed_cycle_earlier_is_history_not_a_problem(self):
        assert summary(line(130, "MONITOR_FAILED"), line(5))["exit"] == 0

    @pytest.mark.parametrize("state", ["FAULT", "REFUSED"])
    def test_a_broken_shadow_step_is_a_problem(self, state):
        s = summary(line(5, shadow_state=state, shadow_detail="No module named x"))
        assert s["exit"] == 1 and "No module named x" in " ".join(s["problems"])

    def test_a_line_that_does_not_say_no_order_was_sent_is_an_alarm(self):
        s = summary(line(70, order_sent=True), line(5))
        assert s["exit"] == 3 and s["health"] == "ALARM"
        missing = line(5)
        del missing["order_sent"]
        assert summary(missing)["exit"] == 3

    def test_no_readable_cycles_is_exit_2(self):
        assert MS.summarise([], now=NOW)["exit"] == 2
        assert MS.parse_lines("") == [] and MS.parse_lines("not json\n{}\n[]\n") == []

    def test_a_torn_last_line_is_skipped_not_guessed(self):
        rows = MS.parse_lines(text(line(70), line(5)) + '{"at": "2026-10-26T11')
        assert len(rows) == 2


class TestWhatItReports:
    def test_the_gate_open_fraction_ignores_failed_cycles(self):
        s = summary(line(300, "MONITOR_READY", gate_reason="CARRY_CLEARS_COSTS_AND_BASIS"),
                    line(240), line(180, "MONITOR_FAILED"), line(120), line(5))
        assert s["gate_judged_cycles"] == 4 and s["gate_open_cycles"] == 1
        assert s["gate_open_fraction"] == pytest.approx(0.25)
        assert s["outcomes"] == {"MONITOR_READY": 1, "ENTRY_NOT_ELIGIBLE": 3,
                                 "MONITOR_FAILED": 1}

    def test_refusal_reasons_basis_funding_and_shadow_states_are_counted(self):
        s = summary(line(130, basis_bps=50.0), line(70, basis_bps=150.0),
                    line(10, gate_reason="BREAKEVEN_HOLD_EXCEEDS_DEMONSTRATED_HOLD"))
        assert s["refusal_reasons"] == {"ENTRY_BASIS_EXCEEDS_CARRY_BUDGET": 2,
                                        "BREAKEVEN_HOLD_EXCEEDS_DEMONSTRATED_HOLD": 1}
        assert s["basis_bps"] == {"n": 3, "min": 50.0, "median": 100.0, "max": 150.0}
        assert s["smoothed_funding_bps"]["median"] == 1.0
        assert s["shadow_states"] == {"STEP_RECORDED": 3}

    def test_the_window_limits_the_statistics_but_not_the_health(self):
        rows = [line(1000 - 10 * i) for i in range(50)] + [line(5)]
        s = summary(*rows, last_n=5)
        assert s["cycles_in_window"] == 5 and s["cycles_total"] == 51

    def test_missing_basis_fields_from_older_lines_are_fine(self):
        old = line(5)
        del old["basis_bps"], old["basis_budget_bps"]
        s = summary(old)
        assert s["basis_bps"] is None and s["exit"] == 0

    def test_the_text_says_it_is_not_evidence_of_profit(self):
        out = MS.render(summary(line(5)))
        assert "HEALTH OK" in out and "none is evidence of profit" in out
        assert "gate open  0/1" in out and "basis bps" in out
        assert "HEALTH ALARM" in MS.render(summary(line(5, order_sent=True)))


class TestTheCommand:
    def run(self, tmp_path, rows_text, *args, tz=None):
        f = tmp_path / "m.jsonl"
        f.write_text(rows_text)
        env = dict(os.environ)
        if tz:
            env["TZ"] = tz
        return subprocess.run(
            [sys.executable, os.path.join(BOT, "tools", "monitor_summary.py"),
             str(f), "--now", str(NOW), *args], capture_output=True, text=True,
            timeout=60, env=env)

    @pytest.mark.parametrize("tz", ["UTC", "America/Los_Angeles", "Asia/Kolkata"])
    def test_the_age_does_not_depend_on_the_local_timezone(self, tmp_path, tz):
        r = self.run(tmp_path, text(line(10)), "--json", tz=tz)
        assert r.returncode == 0, r.stderr
        assert json.loads(r.stdout)["last_cycle_age_minutes"] == 10.0

    def test_exit_codes(self, tmp_path):
        assert self.run(tmp_path, text(line(10))).returncode == 0
        assert self.run(tmp_path, text(line(900))).returncode == 1
        assert self.run(tmp_path, "").returncode == 2
        assert self.run(tmp_path, text(line(10, order_sent=True))).returncode == 3

    def test_a_missing_file_is_exit_2(self, tmp_path):
        r = subprocess.run([sys.executable, os.path.join(BOT, "tools", "monitor_summary.py"),
                            str(tmp_path / "nope.jsonl")], capture_output=True, text=True)
        assert r.returncode == 2


# -- the status script against a fake fly ----------------------------------------

REPORT = {"book": {"book_id": "post_gate_v1", "rule_id": "carry_entry_gate_v1@BTCUSDT",
                   "boundary_utc": "2026-10-25T18:06:27Z"},
          "summary": {"observations": 5, "window_days": 0.2,
                      "entry_gate_refusals": {"ENTRY_BASIS_EXCEEDS_CARRY_BUDGET": 5},
                      "simulated_entries": 0, "closed_trades": 0,
                      "verdict": "INSUFFICIENT_FORWARD_SAMPLE",
                      "pnl_claim_allowed": False}}


def fake_fly(tmp_path, *, history, report=None, reach="HTTP 200", machines=MACHINE_LIST):
    (tmp_path / "hist.txt").write_text(history)
    (tmp_path / "report.txt").write_text(json.dumps(report) if report else "")
    (tmp_path / "machines.txt").write_text(machines)
    (tmp_path / "volumes.txt").write_text(VOLUMES_ONE)
    fly = tmp_path / "fly"
    fly.write_text(f"""#!/usr/bin/env bash
case "$1 $2" in
  "machine list") cat {tmp_path}/machines.txt ;;
  "volumes list") cat {tmp_path}/volumes.txt ;;
  "secrets list") printf 'BYBIT_API_KEY\\nBYBIT_API_SECRET\\n' ;;
  "ssh console") case "$*" in
      *market/time*) echo "{reach}" ;;
      *monitor.jsonl*) cat {tmp_path}/hist.txt ;;
      *forward-report*) cat {tmp_path}/report.txt ;;
      *) echo "/data/runs/run-1" ;; esac ;;
  *) echo "unexpected fly call: $*" >&2; exit 99 ;;
esac
""")
    fly.chmod(fly.stat().st_mode | stat.S_IEXEC)
    return str(fly)


def status(tmp_path, **kw):
    fly = fake_fly(tmp_path, **kw)
    return subprocess.run(["bash", os.path.join(ROOT, "scripts", "runner_status.sh")],
                          capture_output=True, text=True, timeout=120,
                          env={**os.environ, "FLY": fly})


class TestTheStatusScript:
    def recent(self):
        t = time.time()
        rows = []
        for k in range(3, 0, -1):
            rows.append(line(0, at=time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                                  time.gmtime(t - k * 600))))
        return text(*rows)

    def test_a_healthy_runner_reports_ok_and_what_it_learned(self, tmp_path):
        r = status(tmp_path, history=self.recent(), report=REPORT)
        assert r.returncode == 0, r.stdout + r.stderr
        out = r.stdout
        assert "STATUS OK" in out and "HEALTH OK" in out
        assert "observations 5" in out and "INSUFFICIENT_FORWARD_SAMPLE" in out
        assert "pnl_claim_allowed: False" in out

    def test_a_stale_history_is_not_ok(self, tmp_path):
        old = text(line(5000))
        r = status(tmp_path, history=old, report=REPORT)
        assert r.returncode == 1 and "STALE" in r.stdout and "STATUS NOT OK" in r.stdout

    def test_an_alarm_keeps_its_own_exit_code(self, tmp_path):
        t = time.time()
        bad = text(line(0, at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t - 60)),
                        order_sent=True))
        assert status(tmp_path, history=bad, report=REPORT).returncode == 3

    def test_a_blocked_testnet_or_stopped_machine_is_not_ok(self, tmp_path):
        r = status(tmp_path, history=self.recent(), report=REPORT, reach="HTTP 403")
        assert r.returncode == 1 and "not reachable" in r.stdout
        r = status(tmp_path, history=self.recent(), report=REPORT,
                   machines=MACHINE_LIST.replace("started", "stopped"))
        assert r.returncode == 1 and "not started" in r.stdout

    def test_a_missing_shadow_book_is_reported_not_fatal(self, tmp_path):
        r = status(tmp_path, history=self.recent(), report=None)
        assert r.returncode == 0 and "forward-report unavailable" in r.stdout

    def test_no_history_is_exit_2(self, tmp_path):
        assert status(tmp_path, history="", report=REPORT).returncode == 2

    def test_the_script_is_read_only(self):
        src = open(os.path.join(ROOT, "scripts", "runner_status.sh"), encoding="utf-8").read()
        code = "\n".join(l for l in src.splitlines() if not l.lstrip().startswith("#"))
        for verb in ("deploy", "launch", "destroy", "machine stop", "machine start",
                     "machine restart", "secrets set", "volumes create", "touch ",
                     "rm ", "kill", "forward-init", "forward-step", "--arm"):
            assert verb not in code, verb


class TestTheAlarmCannotBeHiddenByTheShapeOfTheLine:
    """`order_sent` is the one safety field in the history. A line that carries
    it must never be dropped just because it lacks `outcome`, is torn, or sits
    behind a forged timestamp; each of these used to read HEALTH OK."""

    def scan(self, raw):
        rows, unreadable = MS.scan_lines(raw)
        return MS.summarise(rows, now=NOW, unreadable=unreadable)

    def test_order_sent_true_without_an_outcome_is_an_alarm(self):
        raw = text(line(30)) + json.dumps({"at": stamp(10), "order_sent": True}) + "\n"
        s = self.scan(raw)
        assert s["health"] == "ALARM" and s["exit"] == 3

    def test_a_torn_line_that_was_saying_order_sent_true_is_an_alarm(self):
        raw = (text(line(60)) + '{"at": "%s", "outcome": "X", "order_sent": tr\n'
               % stamp(40) + text(line(10)))
        s = self.scan(raw)
        assert s["health"] == "ALARM" and s["exit"] == 3

    def test_an_unreadable_line_in_the_middle_is_a_problem_not_silence(self):
        raw = text(line(60)) + "not json at all\n" + text(line(10))
        s = self.scan(raw)
        assert s["health"] == "PROBLEM" and s["exit"] == 1
        assert any("unreadable" in p for p in s["problems"])

    def test_a_torn_final_line_alone_is_tolerated(self):
        # a crash mid-write is the one expected way to end a file; it says
        # nothing about orders and must not turn a healthy runner red
        raw = text(line(60), line(10)) + '{"at": "2026-10-26T11:5'
        s = self.scan(raw)
        assert s["health"] == "OK" and s["exit"] == 0

    def test_a_future_dated_last_line_does_not_hide_a_dead_runner(self):
        forged = line(0, at="2099-01-01T00:00:00Z")
        s = summary(line(900), forged)
        assert s["health"] == "PROBLEM" and s["exit"] == 1
        assert any("future" in p.lower() for p in s["problems"])

    def test_small_clock_skew_is_not_a_problem(self):
        s = summary(line(60), line(-2))
        assert s["health"] == "OK"

    def test_an_alarm_with_no_readable_cycle_still_renders_and_exits_3(self, tmp_path):
        path = tmp_path / "m.jsonl"
        path.write_text('{"order_sent": true}\n', encoding="utf-8")
        proc = subprocess.run(
            [sys.executable, os.path.join(BOT, "tools", "monitor_summary.py"),
             str(path), "--now", str(NOW)], capture_output=True, text=True)
        assert proc.returncode == 3, proc.stderr
        assert "ALARM" in proc.stdout and "order_sent" in proc.stdout
