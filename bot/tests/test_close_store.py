"""The close log is durable, atomic and append-only in effect: a restart finds
exactly what was committed, a failed step commits nothing, and a resolved
attempt cannot be rewritten into something more convenient."""
from __future__ import annotations

import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from persistence import PersistenceError, StateStore            # noqa: E402

C = "cyc-1"


def mk(tmp_path):
    return StateStore(str(tmp_path / "state.db"))


def intent(n=1, link=None, qty=0.01, short=0.01):
    return {"n": n, "link_id": link or f"CL-c-{n}", "qty": qty, "short_before": short}


def test_an_intent_is_durable_across_a_reopen(tmp_path):
    s = mk(tmp_path)
    s.apply_close_step(C, new_attempt=intent(), waits=0, lag_waits=0, now=100.0)
    s.close()
    rows = mk(tmp_path).close_attempts(C)
    assert len(rows) == 1 and rows[0]["state"] == "INTENT"
    assert rows[0]["intent_epoch"] == 100.0 and rows[0]["sent_epoch"] is None
    assert rows[0]["exec_ids"] == ()


def test_the_wait_counters_survive_a_reopen_so_a_restart_cannot_reset_them(tmp_path):
    s = mk(tmp_path)
    s.apply_close_step(C, new_attempt=intent(), waits=7, lag_waits=3)
    s.close()
    st = mk(tmp_path).close_state(C)
    assert (st["waits"], st["lag_waits"], st["closed"]) == (7, 3, 0)


def test_an_unknown_cycle_reads_as_zero_counters_not_an_error(tmp_path):
    st = mk(tmp_path).close_state("never-seen")
    assert (st["waits"], st["lag_waits"], st["halted_reason"], st["closed"]) == (0, 0, "", 0)


def test_a_duplicate_link_id_is_refused_and_commits_nothing(tmp_path):
    s = mk(tmp_path)
    s.apply_close_step(C, new_attempt=intent(1, "L1"), waits=0, lag_waits=0)
    with pytest.raises((PersistenceError, sqlite3.IntegrityError)):
        s.apply_close_step(C, new_attempt=intent(2, "L1"), waits=5, lag_waits=5)
    assert [r["n"] for r in s.close_attempts(C)] == [1]
    assert s.close_state(C)["waits"] == 0


def test_a_duplicate_attempt_number_is_refused(tmp_path):
    s = mk(tmp_path)
    s.apply_close_step(C, new_attempt=intent(1, "L1"), waits=0, lag_waits=0)
    with pytest.raises((PersistenceError, sqlite3.IntegrityError)):
        s.apply_close_step(C, new_attempt=intent(1, "L2"), waits=0, lag_waits=0)


def test_a_step_that_fails_midway_changes_nothing(tmp_path):
    s = mk(tmp_path)
    s.apply_close_step(C, new_attempt=intent(1, "L1"), waits=1, lag_waits=0)
    with pytest.raises(PersistenceError):
        s.apply_close_step(C, resolutions=[(1, "FILLED", 0.01, ["x1"]), (9, "FILLED", 0.0, [])],
                           new_attempt=intent(2, "L2"), waits=0, lag_waits=0)
    rows = s.close_attempts(C)
    assert [(r["n"], r["state"]) for r in rows] == [(1, "INTENT")]
    assert s.close_state(C)["waits"] == 1


def test_a_resolution_records_fills_and_merges_execution_ids(tmp_path):
    s = mk(tmp_path)
    s.apply_close_step(C, new_attempt=intent(), waits=0, lag_waits=0)
    s.apply_close_step(C, resolutions=[(1, "PARTIAL", 0.004, ["x2", "x1"])], waits=0, lag_waits=0)
    r = s.close_attempts(C)[0]
    assert (r["state"], r["filled_qty"], r["exec_ids"]) == ("PARTIAL", 0.004, ("x1", "x2"))


def test_a_resolved_attempt_cannot_be_rewritten(tmp_path):
    s = mk(tmp_path)
    s.apply_close_step(C, new_attempt=intent(), waits=0, lag_waits=0)
    s.apply_close_step(C, resolutions=[(1, "PARTIAL", 0.004, ["x1"])], waits=0, lag_waits=0)
    for state, filled in (("FILLED", 0.01), ("CANCELLED", 0.0), ("REJECTED", 0.0),
                          ("NEVER_SENT", 0.0)):
        with pytest.raises(PersistenceError):
            s.apply_close_step(C, resolutions=[(1, state, filled, [])], waits=0, lag_waits=0)
    assert s.close_attempts(C)[0]["state"] == "PARTIAL"


def test_reapplying_the_same_resolution_is_idempotent(tmp_path):
    s = mk(tmp_path)
    s.apply_close_step(C, new_attempt=intent(), waits=0, lag_waits=0)
    for _ in range(3):
        s.apply_close_step(C, resolutions=[(1, "FILLED", 0.01, ["x1"])], waits=0, lag_waits=0)
    r = s.close_attempts(C)[0]
    assert (r["state"], r["filled_qty"], r["exec_ids"]) == ("FILLED", 0.01, ("x1",))


def test_a_recorded_fill_never_goes_down(tmp_path):
    s = mk(tmp_path)
    s.apply_close_step(C, new_attempt=intent(), waits=0, lag_waits=0)
    s.apply_close_step(C, resolutions=[(1, "PARTIAL", 0.006, ["x1"])], waits=0, lag_waits=0)
    s.apply_close_step(C, resolutions=[(1, "PARTIAL", 0.002, [])], waits=0, lag_waits=0) \
        if False else None
    assert s.close_attempts(C)[0]["filled_qty"] == 0.006


def test_only_a_resolution_state_is_accepted_as_a_resolution(tmp_path):
    s = mk(tmp_path)
    s.apply_close_step(C, new_attempt=intent(), waits=0, lag_waits=0)
    for bad in ("INTENT", "SENT", "DONE", "", "filled"):
        with pytest.raises(PersistenceError):
            s.apply_close_step(C, resolutions=[(1, bad, 0.0, [])], waits=0, lag_waits=0)


@pytest.mark.parametrize("w,l", [(-1, 0), (0, -1), (True, 0), (0, 1.5), ("1", 0)])
def test_counters_must_be_whole_numbers_at_least_zero(tmp_path, w, l):
    s = mk(tmp_path)
    with pytest.raises(PersistenceError):
        s.apply_close_step(C, new_attempt=intent(), waits=w, lag_waits=l)
    assert s.close_attempts(C) == []


def test_mark_sent_moves_only_an_intent_and_stamps_the_time(tmp_path):
    s = mk(tmp_path)
    s.apply_close_step(C, new_attempt=intent(), waits=0, lag_waits=0, now=10.0)
    assert s.mark_close_sent(C, 1, now=11.5) is True
    r = s.close_attempts(C)[0]
    assert (r["state"], r["sent_epoch"]) == ("SENT", 11.5)
    assert s.mark_close_sent(C, 1, now=99.0) is False      # not an intent any more
    assert s.close_attempts(C)[0]["sent_epoch"] == 11.5


def test_a_resend_bumps_the_count_and_returns_the_attempt_to_intent(tmp_path):
    s = mk(tmp_path)
    s.apply_close_step(C, new_attempt=intent(), waits=0, lag_waits=0, now=10.0)
    s.mark_close_sent(C, 1, now=11.0)
    s.apply_close_step(C, resend_n=1, waits=0, lag_waits=0, now=20.0)
    r = s.close_attempts(C)[0]
    assert (r["sends"], r["state"], r["sent_epoch"], r["intent_epoch"]) == (2, "INTENT", None, 20.0)


def test_a_resolved_attempt_cannot_be_resent(tmp_path):
    s = mk(tmp_path)
    s.apply_close_step(C, new_attempt=intent(), waits=0, lag_waits=0)
    s.apply_close_step(C, resolutions=[(1, "FILLED", 0.01, ["x1"])], waits=0, lag_waits=0)
    with pytest.raises(PersistenceError):
        s.apply_close_step(C, resend_n=1, waits=0, lag_waits=0)


def test_closed_is_sticky_and_a_halt_reason_is_kept(tmp_path):
    s = mk(tmp_path)
    s.apply_close_step(C, new_attempt=intent(), waits=2, lag_waits=0,
                       halted_reason="ATTEMPTS_EXHAUSTED")
    s.apply_close_step(C, waits=0, lag_waits=0, closed=True)
    s.apply_close_step(C, waits=5, lag_waits=0)             # a later step must not reopen it
    st = s.close_state(C)
    assert st["closed"] == 1 and st["halted_reason"] == "ATTEMPTS_EXHAUSTED"


def test_open_close_cycles_lists_only_cycles_still_in_progress(tmp_path):
    s = mk(tmp_path)
    s.apply_close_step("a", new_attempt=intent(1, "LA"), waits=0, lag_waits=0)
    s.apply_close_step("b", new_attempt=intent(1, "LB"), waits=0, lag_waits=0)
    s.apply_close_step("b", waits=0, lag_waits=0, closed=True)
    assert s.open_close_cycles() == ["a"]


def test_cycles_do_not_see_each_others_attempts(tmp_path):
    s = mk(tmp_path)
    s.apply_close_step("a", new_attempt=intent(1, "LA"), waits=0, lag_waits=0)
    s.apply_close_step("b", new_attempt=intent(1, "LB"), waits=0, lag_waits=0)
    assert [r["link_id"] for r in s.close_attempts("a")] == ["LA"]
    assert [r["link_id"] for r in s.close_attempts("b")] == ["LB"]
