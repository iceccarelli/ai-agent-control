"""`monitor_summary.py` and `runner_monitor.py`: what a hand-edited, truncated or
mistyped history, and a mistyped command line, must NOT read as healthy.

Mutation testing of the health report left survivors that `test_monitor_summary.py`
does not kill (a claim other than `true`, a history with no readable row at all,
the exact stale boundary, an unparseable stamp, alarm-over-problem precedence),
and reading the report found three more ways to a green exit:

  * the last cycle's `outcome` was never checked against the three outcomes the
    monitor can produce, so a row saying `CONFORMANCE_PASSED` (which this
    read-only monitor cannot write) or `OK` or nothing readable read as
    "HEALTH OK, exit 0";
  * `--stale-minutes nan` made `age > limit` false for every age, so no runner
    could ever be stale;
  * `runner_monitor --interval-minutes nan` passed the `< 1.0` guard (every
    comparison with NaN is False) and the loop's `waited < interval` was then
    never true: no sleep between cycles, an unbounded stream of venue reads.

Only refusals are added. No threshold or default moves.
"""
from __future__ import annotations

import calendar
import json
import os
import sys
import time

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
BOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(BOT, "tools"))
sys.path.insert(0, BOT)

import monitor_summary as MS  # noqa: E402
import runner_monitor as RM  # noqa: E402

NOW = calendar.timegm((2026, 10, 26, 12, 0, 0))


def stamp(minutes_ago):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(NOW - minutes_ago * 60))


def row(minutes_ago, outcome="ENTRY_NOT_ELIGIBLE", **kw):
    r = {"at": stamp(minutes_ago), "outcome": outcome, "order_sent": False,
         "reasons": ["ENTRY_BASIS_EXCEEDS_CARRY_BUDGET"],
         "gate_reason": "ENTRY_BASIS_EXCEEDS_CARRY_BUDGET"}
    r.update(kw)
    return r


def text(*rows, extra=""):
    return "".join(json.dumps(r) + "\n" for r in rows) + extra


def summarise(*rows, extra="", **kw):
    raw = text(*rows, extra=extra)
    return MS.summarise(MS.parse_lines(raw), now=NOW, raw_text=raw, **kw)


# -- an outcome the monitor cannot produce is not a healthy reading -----------

@pytest.mark.parametrize("outcome", ["CONFORMANCE_PASSED", "CONFORMANCE_FAILED",
                                     "OK", "", "monitor_ready"])
def test_an_outcome_the_monitor_cannot_produce_is_a_problem(outcome):
    s = summarise(row(70), row(10, outcome=outcome))
    assert s["exit"] == 1 and s["health"] == "PROBLEM", s
    assert "outcome" in " ".join(s["problems"]).lower()


def test_an_unknown_outcome_earlier_in_the_history_is_also_reported():
    s = summarise(row(70, outcome="CONFORMANCE_PASSED"), row(10))
    assert s["exit"] == 1 and "outcome" in " ".join(s["problems"]).lower()


@pytest.mark.parametrize("outcome", ["ENTRY_NOT_ELIGIBLE", "MONITOR_READY"])
def test_the_two_correct_readings_stay_healthy(outcome):
    assert summarise(row(70), row(10, outcome=outcome))["exit"] == 0


# -- the order claim: anything but a literal false ----------------------------

@pytest.mark.parametrize("claim", [None, 1, "true", "yes", [], {}, "false", 0])
def test_an_unreadable_row_whose_order_sent_is_not_literally_false_alarms(claim):
    """`false` the string, `0` and `null` are not the boolean the monitor
    writes: a row that cannot be read as a cycle must not be able to hide them."""
    s = summarise(row(70), row(10), extra=json.dumps({"order_sent": claim}) + "\n")
    assert s["exit"] == 3 and s["health"] == "ALARM", claim


def test_a_forged_claim_alarms_even_when_no_row_at_all_can_be_read():
    s = MS.summarise([], now=NOW, raw_text='{"order_sent": true}\n')
    assert s["exit"] == 3 and s["health"] == "ALARM"
    torn = '{"at": "%s", "outcome": "MONITOR_READY", "order_sent": true, "re' % stamp(5)
    s = MS.summarise([], now=NOW, raw_text=torn)
    assert s["exit"] == 3 and s["health"] == "ALARM"


def test_no_readable_row_and_no_claim_is_no_data():
    s = MS.summarise([], now=NOW, raw_text="garbage\n")
    assert s["exit"] == 2 and s["health"] == "NO_DATA"


def test_an_alarm_outranks_a_problem_in_both_label_and_exit_code():
    s = summarise(row(400, order_sent=True), row(300))        # alarm AND stale
    assert s["alarms"] and s["problems"]
    assert (s["health"], s["exit"]) == ("ALARM", 3)


# -- the stale boundary and the stamp ----------------------------------------

def test_exactly_on_the_stale_limit_is_still_current_and_a_second_past_is_not():
    on = MS.summarise(MS.parse_lines(text(row(150))), now=NOW)
    assert on["exit"] == 0
    past = MS.summarise(MS.parse_lines(text(row(150.02))), now=NOW)
    assert past["exit"] == 1 and "STALE" in " ".join(past["problems"])


@pytest.mark.parametrize("bad", ["2026-02-30T00:00:00Z", "yesterday",
                                 "2026-10-26 12:00:00", "2026-10-26T12:00:00"])
def test_a_last_stamp_that_cannot_be_read_is_a_problem(bad):
    s = MS.summarise([{"at": bad, "outcome": "ENTRY_NOT_ELIGIBLE",
                       "order_sent": False}], now=NOW)
    assert s["exit"] == 1 and "unreadable" in " ".join(s["problems"])


# -- the command line ---------------------------------------------------------

@pytest.mark.parametrize("limit", ["nan", "inf", "-inf", "0", "-5"])
def test_a_stale_limit_that_can_never_trip_is_refused(tmp_path, capsys, limit):
    path = tmp_path / "monitor.jsonl"
    path.write_text(text(row(100000)))                       # months old
    code = MS.main([str(path), "--now", str(NOW), f"--stale-minutes={limit}"])
    assert code == 2, "a dead runner must not read healthy because of a typo"
    assert "stale" in capsys.readouterr().err.lower()


def test_the_default_stale_limit_still_reports_a_dead_runner(tmp_path):
    path = tmp_path / "monitor.jsonl"
    path.write_text(text(row(100000)))
    assert MS.main([str(path), "--now", str(NOW)]) == 1


@pytest.fixture()
def creds(monkeypatch):
    monkeypatch.setenv("BYBIT_API_KEY", "k")
    monkeypatch.setenv("BYBIT_API_SECRET", "s")


@pytest.fixture()
def no_loop(monkeypatch):
    """Nothing may run: the loop, the lock and the signal handlers are stubbed,
    and a test that reaches the loop records it instead of looping."""
    reached = []
    monkeypatch.setattr(RM, "loop", lambda root, **kw: reached.append(kw) or 0)
    monkeypatch.setattr(RM, "monitor_once",
                        lambda *a, **kw: reached.append("once") or 0)
    monkeypatch.setattr(RM.UD, "install_signal_handlers", lambda: None)
    monkeypatch.setattr(RM.UD, "acquire_lock",
                        lambda d: reached.append("lock") or None)
    return reached


@pytest.mark.parametrize("interval", ["nan", "inf", "-inf", "0.5"])
def test_a_loop_interval_that_cannot_pace_the_loop_is_refused(creds, no_loop,
                                                              interval):
    code = RM.main(["--run-dir", "/never-created", "--loop",
                    f"--interval-minutes={interval}"])
    assert code == RM.EXIT_REFUSED
    assert no_loop == [], "a refused configuration must not reach the lock or loop"


def test_a_sane_interval_is_not_refused_by_the_argument_check(creds, no_loop,
                                                              monkeypatch, tmp_path):
    """The guard must not refuse a real configuration: 60 minutes reaches the
    loop (stubbed here so nothing runs)."""
    class Lock:
        def close(self):
            pass

    monkeypatch.setattr(RM.UD, "acquire_lock", lambda d: Lock())
    code = RM.main(["--run-dir", str(tmp_path / "root"), "--loop",
                    "--interval-minutes=60"])
    assert code == 0 and no_loop[-1]["interval_s"] == 3600.0
