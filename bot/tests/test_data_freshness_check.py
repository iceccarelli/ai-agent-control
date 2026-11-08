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


# ---------------------------------------------------------------- the corpus status uses the newest CLOSED bar, flags future rows and includes funding

def _ms(days_after_t1):
    return T1 + days_after_t1 * DAY


def test_a_future_dated_row_cannot_make_a_stale_corpus_current():
    checked = _ms(40)
    last_closed = (checked // DAY) * DAY - DAY
    linear = [_ms(k) for k in range(0, 2)]                       # newest closed bar is far behind
    funding = [_ms(1) + k * 8 * 3_600_000 for k in range(3)]
    honest = dfc.corpus_freshness(linear, funding, checked_ms=checked, last_closed_ms=last_closed)
    assert honest["corpus_status"] == "STALE" and honest["corpus_lag_days_behind_last_closed_bar"] == 38
    with_future = dfc.corpus_freshness(linear + [_ms(3000)], funding, checked_ms=checked, last_closed_ms=last_closed)     # one row dated far in the future
    assert with_future["corpus_status"] == "DATA_PROBLEM" and with_future["corpus_is_current"] is False
    assert with_future["corpus_lag_days_behind_last_closed_bar"] == 38 and with_future["future_dated_rows"] == {"linear": 1, "funding": 0}
    assert any("future" in p for p in with_future["data_problems"])
    assert dfc.corpus_freshness(linear, funding + [_ms(3000)], checked_ms=checked, last_closed_ms=last_closed)["future_dated_rows"]["funding"] == 1


def test_current_needs_the_newest_closed_bar_and_recent_funding():
    checked = _ms(10) + 3_600_000
    last_closed = (checked // DAY) * DAY - DAY
    linear = [last_closed]                                        # the newest bar that can have closed
    cur = dfc.corpus_freshness(linear, [checked - 3_600_000], checked_ms=checked, last_closed_ms=last_closed)
    assert cur["corpus_status"] == "CURRENT" and cur["corpus_is_current"] is True and cur["data_problems"] == []
    old_funding = dfc.corpus_freshness(linear, [checked - 5 * DAY], checked_ms=checked, last_closed_ms=last_closed)
    assert old_funding["corpus_status"] == "STALE" and old_funding["funding_lag_days"] == 5
    no_funding = dfc.corpus_freshness(linear, [], checked_ms=checked, last_closed_ms=last_closed)
    assert no_funding["corpus_status"] == "STALE" and no_funding["funding_lag_days"] is None
    open_bar_only = dfc.corpus_freshness([last_closed, last_closed + DAY], [checked - 3_600_000], checked_ms=checked, last_closed_ms=last_closed)
    assert open_bar_only["corpus_status"] == "CURRENT"            # the OPEN bar is ignored for lag; the closed one before it is the newest closed
    assert dfc.corpus_freshness([], [], checked_ms=checked, last_closed_ms=last_closed)["corpus_status"] == "UNKNOWN_EMPTY"


def test_main_uses_the_closed_bar_helper(monkeypatch, tmp_path):
    import io
    import json
    import contextlib
    orig = dfc._rows

    def fake(path):
        rows = list(orig(path))
        if path == dfc.LINEAR_FILE:
            r = dict(rows[-1])
            r["time_period_start"] = "2030-01-01T00:00:00.0000000Z"
            rows.append(r)
        return rows
    monkeypatch.setattr(dfc, "_rows", fake)
    out = str(tmp_path / "o.json")
    with contextlib.redirect_stdout(io.StringIO()):
        dfc.main(["--checked-at-utc", "2026-11-08T00:00:00Z", "--no-fetch", "--out", out])
    j = json.load(open(out))["data_freshness"]
    assert j["corpus_status"] == "DATA_PROBLEM" and j["future_dated_rows"]["linear"] >= 1
