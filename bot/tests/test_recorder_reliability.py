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
