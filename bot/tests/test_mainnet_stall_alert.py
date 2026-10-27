"""A real-market evidence stream that has stopped must say so.

The protocol needs 500 observations over 30 days. If Bybit blocks the runner's IP
(or the evidence file breaks) the mainnet read is REFUSED every cycle, the cycle
itself stays healthy, and nothing pages: the 30-day clock stops advancing and
nobody knows for weeks. After N cycles in a row without a recorded observation the
notifier now raises a WARNING. It reads the history only; it cannot change a cycle,
and a runner that never collects mainnet data (no `mainnet_state` at all) is not
accused of stalling.
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
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(BOT, "tools"))
sys.path.insert(0, BOT)

import runner_alerts as RA  # noqa: E402

NOW = float(calendar.timegm((2026, 10, 27, 12, 0, 0)))


def stamp(minutes_ago):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(NOW - minutes_ago * 60))


def line(minutes_ago, state="RECORDED", **kw):
    r = {"at": stamp(minutes_ago), "outcome": "ENTRY_NOT_ELIGIBLE", "order_sent": False,
         "reasons": []}
    if state is not None:
        r["mainnet_state"] = state
    r.update(kw)
    return json.dumps(r) + "\n"


def history(*states, gap=60):
    """Oldest first; the newest is 5 minutes old."""
    n = len(states)
    return "".join(line(5 + gap * (n - 1 - i), s) for i, s in enumerate(states))


def keys(text):
    return [a.key for a in RA.evaluate(text, NOW)]


class TestTheStall:
    def test_the_threshold_is_declared(self):
        assert RA.MAINNET_STALL_CYCLES == 6

    def test_a_healthy_stream_is_silent(self):
        assert keys(history(*["RECORDED"] * 10)) == []

    def test_five_failures_in_a_row_are_not_yet_a_stall(self):
        assert keys(history("RECORDED", *["REFUSED"] * 5)) == []

    def test_six_in_a_row_are_a_stall_and_name_the_last_reason(self):
        text = history("RECORDED", *["REFUSED"] * 5) + line(
            1, "REFUSED", mainnet_detail="MainnetRefusal: HTTP 403 from the venue")
        a = [x for x in RA.evaluate(text, NOW) if x.key == "mainnet_stalled"]
        assert len(a) == 1 and a[0].severity == RA.WARNING
        assert "403" in a[0].text and "6" in a[0].text
        assert "30-day" in a[0].text and "not accumulating" in a[0].text

    def test_faults_count_as_failures_too(self):
        assert "mainnet_stalled" in keys(history(*["FAULT"] * 6))
        assert "mainnet_stalled" in keys(history("REFUSED", "FAULT", "REFUSED",
                                                 "FAULT", "REFUSED", "FAULT"))

    def test_one_recorded_observation_inside_the_window_clears_it(self):
        assert keys(history(*["REFUSED"] * 5, "RECORDED")) == []
        assert keys(history("REFUSED", "RECORDED", *["REFUSED"] * 4)) == []

    def test_only_the_trailing_window_counts(self):
        assert keys(history(*["REFUSED"] * 20, *["RECORDED"] * 6)) == []

    def test_a_runner_that_collects_no_mainnet_data_is_not_accused(self):
        text = "".join(line(5 + 60 * i, None) for i in reversed(range(10)))   # oldest first
        assert keys(text) == []

    def test_rows_from_before_the_feature_are_ignored_not_counted_as_failures(self):
        old = "".join(line(600 + 60 * i, None) for i in reversed(range(10)))
        new = history(*["RECORDED"] * 3)
        assert keys(old + new) == []

    def test_a_mixed_history_only_counts_rows_that_carry_the_field(self):
        mixed = line(400, None) + history(*["REFUSED"] * 5)
        assert keys(mixed) == []             # five rows with the field: not six

    def test_it_is_not_a_health_problem_and_does_not_change_the_exit_code(self):
        import monitor_summary as MS
        text = history(*["REFUSED"] * 8)
        rows, _ = MS.scan(text)
        s = MS.summarise(rows, now=NOW, raw_text=text)
        assert s["exit"] == 0 and s["health"] == "OK"

    def test_the_stalled_alert_is_throttled_and_recovers_like_the_others(self):
        t = RA.Throttle(resend_s=3600)
        stalled = [a for a in RA.evaluate(history(*["REFUSED"] * 6), NOW)]
        assert t.plan(stalled, 0.0) == stalled
        assert t.plan(stalled, 60.0) == []
        rec = t.plan([], 120.0)
        assert [r.key for r in rec] == ["mainnet_stalled:recovered"]

    def test_the_cli_exit_code_still_reflects_it_without_masking_other_alarms(self, tmp_path, capsys):
        p = tmp_path / "h.jsonl"
        p.write_text(history(*["REFUSED"] * 6))
        code = RA.main([str(p), "--now", str(NOW), "--dry-run"], env={})
        out = capsys.readouterr().out
        assert "mainnet_stalled" in out and code == 1
