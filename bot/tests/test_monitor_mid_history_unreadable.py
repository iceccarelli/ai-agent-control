"""An unreadable line in the MIDDLE of monitor.jsonl is a PROBLEM (exit 1).

Only a torn FINAL non-blank line (a write cut by a restart) is tolerated. A
garbage line with readable rows after it means the history was edited or
corrupted, which must not summarise as healthy. An unreadable line claiming an
order was sent stays an ALARM (exit 3). Offline; nothing is sent anywhere.
"""
from __future__ import annotations

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
BOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(BOT, "tools"))
sys.path.insert(0, HERE)

import monitor_summary as MS  # noqa: E402
from test_monitor_summary import NOW, line  # noqa: E402


def raw(*parts):
    return "\n".join(p if isinstance(p, str) else json.dumps(p) for p in parts) + "\n"


def summ(text):
    return MS.summarise(MS.parse_lines(text), now=NOW, raw_text=text)


def test_garbage_in_the_middle_is_a_problem():
    s = summ(raw(line(20), "GARBAGE", line(10)))
    assert s["health"] == "PROBLEM" and s["exit"] == 1
    assert any("unreadable" in p for p in s["problems"])
    assert s["unreadable_lines"] == 1


def test_torn_final_line_alone_stays_tolerated():
    s = summ(raw(line(20), line(10), '{"at": "2026-10-26T11'))
    assert s["health"] == "OK" and s["exit"] == 0


def test_torn_final_line_with_trailing_blank_lines_stays_tolerated():
    s = summ(raw(line(20), line(10), "GARBAGE") + "\n\n")
    assert s["health"] == "OK" and s["exit"] == 0


def test_garbage_in_the_middle_and_order_sent_claim_is_an_alarm():
    s = summ(raw(line(20), '{"order_sent": true, torn', line(10)))
    assert s["health"] == "ALARM" and s["exit"] == 3


def test_two_garbage_lines_one_mid_one_final_counts_only_the_middle():
    s = summ(raw(line(20), "GARBAGE", line(10), "torn"))
    assert s["exit"] == 1
    assert any("1 unreadable" in p for p in s["problems"])


def test_two_garbage_lines_both_mid():
    s = summ(raw(line(30), "BAD1", line(20), "BAD2", line(10)))
    assert s["exit"] == 1
    assert any("2 unreadable" in p for p in s["problems"])


def test_garbage_only_is_still_no_data():
    s = summ(raw("GARBAGE"))
    assert s["health"] == "NO_DATA" and s["exit"] == 2


def test_cli_exit_codes(tmp_path, capsys):
    p = tmp_path / "monitor.jsonl"
    p.write_text(raw(line(20), "GARBAGE", line(10)), encoding="utf-8")
    assert MS.main([str(p), "--now", str(NOW)]) == 1
    p.write_text(raw(line(20), line(10), "GARBAGE"), encoding="utf-8")
    assert MS.main([str(p), "--now", str(NOW)]) == 0
    p.write_text(raw(line(20), "order_sent: true garbage", line(10)), encoding="utf-8")
    assert MS.main([str(p), "--now", str(NOW)]) == 3
    capsys.readouterr()
