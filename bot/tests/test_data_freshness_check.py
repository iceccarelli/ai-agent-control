"""tools/data_freshness_check.py: only CLOSED rows strictly after t1 are new data; a stale corpus is labelled STALE, never current.
Offline: the pure counting function only (the tool's fetch path is never exercised here)."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))

import data_freshness_check as dfc  # noqa: E402

DAY = dfc.DAY_MS
T1 = 1_786_000_000_000 // DAY * DAY            # a UTC day boundary, ms


def us(ms):
    return ms * 1000


def test_a_bar_not_yet_closed_is_not_new_data():
    checked = T1 + 3 * DAY + 5 * 3_600_000                       # mid-day: the bar opened at T1 + 3d is still open
    rows = [us(T1 + k * DAY) for k in range(0, 4)]               # T1 itself, +1d, +2d, +3d (open)
    out = dfc.count_new_closed(rows, t1_ms=T1, checked_ms=checked, bar_ms=DAY)
    assert out == {"closed_after_t1": 2, "open_or_future": 1}    # +1d and +2d closed; +3d is open; t1 itself is not "after"


def test_t1_itself_and_earlier_rows_never_count():
    out = dfc.count_new_closed([us(T1 - DAY), us(T1)], t1_ms=T1, checked_ms=T1 + 10 * DAY, bar_ms=DAY)
    assert out == {"closed_after_t1": 0, "open_or_future": 0}


def test_a_row_dated_in_the_future_is_flagged_not_counted():
    out = dfc.count_new_closed([us(T1 + 100 * DAY)], t1_ms=T1, checked_ms=T1 + 2 * DAY, bar_ms=DAY)
    assert out == {"closed_after_t1": 0, "open_or_future": 1}


def test_a_funding_print_has_no_open_state():
    checked = T1 + DAY + 3_600_000
    out = dfc.count_new_closed([us(T1 + DAY), us(T1 + DAY + 3_600_000), us(T1 + DAY + 2 * 3_600_000)], t1_ms=T1, checked_ms=checked, bar_ms=0)
    assert out == {"closed_after_t1": 2, "open_or_future": 1}    # settled at or before `checked` counts; one later does not
