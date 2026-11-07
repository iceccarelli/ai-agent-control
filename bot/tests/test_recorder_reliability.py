import pytest

import recorder_reliability as RR
from capacity_helpers import base_row, series, START

BASE = base_row()


def rows(n=3, **kw):
    return series(BASE, n, lambda T, t: 0.6, lambda T: 0.6, **kw)


def test_a_recorder_collecting_at_cadence_and_current_is_healthy():
    rs = rows()
    r = RR.report(rs, now_ms=rs[-1]["at_ms"] + 60_000)
    assert r["verdict"] == "HEALTHY" and r["missing_intervals"] == 0 and r["completeness"] == pytest.approx(1.0)
    assert r["receive_minus_exchange_ms"]["median"] == 40


def test_a_recorder_that_stopped_is_stale_however_good_its_old_rows_were():
    rs = rows()
    r = RR.report(rs, now_ms=rs[-1]["at_ms"] + 3 * 3_600_000)
    assert r["verdict"] == "STALE" and r["stale"] and "not collecting" in r["why"]


def test_gaps_are_measured_and_degrade_the_verdict():
    rs = rows(skip=lambda i, t: 40 <= i <= 60)
    r = RR.report(rs, now_ms=rs[-1]["at_ms"] + 60_000)
    assert r["verdict"] == "DEGRADED" and r["gaps"] and r["missing_intervals"] >= 20 and r["max_gap_s"] > 3000
    assert r["uptime_fraction_in_window"] < 1.0


def test_no_rows_is_no_data_never_healthy():
    assert RR.report([], now_ms=1)["verdict"] == "NO_DATA"


def test_store_faults_supervisor_and_disk_are_reported_as_measured():
    rs = rows()
    rs[-1]["recorder"].update(store_fault="OSError: disk is full", linear_reconnects=4)
    r = RR.report(rs, now_ms=rs[-1]["at_ms"] + 1000, supervisor={"recorder_restarts": 2, "recorder_last_exit": 1,
                                                                 "recorder_consecutive_failures": 0})
    assert r["verdict"] == "DEGRADED" and r["feed"]["rows_with_store_fault"] == 1
    assert r["feed"]["linear_reconnects_in_window"] == 4 and r["supervisor"]["recorder_restarts"] == 2
    assert r["disk"]["free_bytes_last"] == 4_000_000_000


def test_a_future_dated_newest_row_is_not_healthy_and_not_silently_fresh():
    """A negative age used to mean 'never stale': rows dated ahead of now (wrong clock, copied evidence) looked healthy."""
    from capacity_helpers import base_row, series
    rows = series(base_row(), 3, lambda T, t: 0.6, lambda T: 0.6)
    r = RR.report(rows, now_ms=rows[-1]["at_ms"] - 3_600_000)
    assert r["verdict"] == "CLOCK_AHEAD" and r["clock_ahead"] and "future" in r["why"]
    ok = RR.report(rows, now_ms=rows[-1]["at_ms"] + 60_000)
    assert ok["verdict"] == "HEALTHY" and not ok["clock_ahead"]


def test_an_old_deploy_gap_stays_in_the_list_but_does_not_make_todays_evidence_degraded_forever():
    """The review: one 15-minute gap anywhere in the retained window made the verdict DEGRADED, and the opportunity engine
    refuses anything but HEALTHY, so a single deploy blocked all evidence for a year."""
    from capacity_helpers import base_row, series
    cad = 300_000
    day = 24 * 3_600_000
    rows_ = series(base_row(), 9, lambda T, t: 0.6, lambda T: 0.6, skip=lambda i, t: 100 <= i <= 103)     # a ~20 min hole, early
    r = RR.report(rows_, now_ms=rows_[-1]["at_ms"] + 60_000)
    assert r["gaps"] and r["max_gap_s"] > 600
    assert r["verdict"] == "HEALTHY" and r["recent_gap"] is False and r["completeness"] > 0.99
    recent = series(base_row(), 3, lambda T, t: 0.6, lambda T: 0.6, skip=lambda i, t: 150 <= i <= 180)    # a 2.5 h hole inside the last day
    r2 = RR.report(recent, now_ms=recent[-1]["at_ms"] + 60_000)
    assert r2["verdict"] == "DEGRADED" and r2["recent_gap"] is True


# ---------------------------------------------------------------- duplicates and ordering (reproduced: duplicates masked a gap; unsorted input gave a negative window)

def _rows_at(ks, cadence_s=300.0, base=1_700_000_000_000):
    return [{"at_ms": int(base + k * cadence_s * 1000), "times": {}, "recorder": {}} for k in ks]


def test_duplicate_rows_do_not_count_as_completeness_and_are_reported():
    import recorder_reliability as RR
    ks = [0, 1, 1, 1, 1, 4, 5]
    now = 1_700_000_000_000 + 5 * 300_000 + 1000
    rep = RR.report(_rows_at(ks), now_ms=now)
    assert rep["duplicate_rows"] == 3 and rep["completeness"] < 1.0 and rep["verdict"] == "DEGRADED"
    clean = RR.report(_rows_at([0, 1, 2, 3, 4, 5]), now_ms=now)               # positive control
    assert clean["verdict"] == "HEALTHY" and clean["duplicate_rows"] == 0 and clean["completeness"] == 1.0


def test_unsorted_rows_are_ordered_before_judging_and_flagged():
    import recorder_reliability as RR
    now = 1_700_000_000_000 + 2 * 300_000 + 1000
    rep = RR.report(_rows_at([2, 0, 1]), now_ms=now)
    assert rep["window_hours"] > 0 and rep["last_row_age_s"] < 5 and rep["rows_out_of_order"] is True and rep["verdict"] == "DEGRADED"
