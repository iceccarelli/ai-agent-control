"""The close executor against a venue that fails the ways a venue can.

Every scenario ends by checking the INVARIANTS, not just the expected result:
  I1 no order id is ever put on the wire twice (default policy)
  I2 the position is never made larger (never net long from a reduce-only Buy)
  I3 at most one close order is live at a time
  I4 an acknowledgement is not a fill: recorded fills equal what the venue's
     order rows and executions say
  I5 FLAT is declared only when the venue's own position reads zero
  I6 a restart never lowers the attempt count or resets the wait counters
  I7 a short that remains is never dropped: the run ends FLAT or HALT, with the
     residual visible to the venue and the history on file
"""
from __future__ import annotations

import os
import random
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
for p in (os.path.dirname(HERE), HERE):
    if p not in sys.path:
        sys.path.insert(0, p)

import close_executor as CE                                      # noqa: E402
import close_plan as CP                                          # noqa: E402
from close_exec_harness import Crash, Rig                        # noqa: E402

DT = 10.0
POL = CP.Policy(max_attempts=3, qty_step=0.001, min_qty=0.001, max_waits=30, max_lag_waits=3)


def invariants(rig, last, *, expect_flat=None):
    v = rig.venue
    assert all(n <= 1 for n in v.sends.values()), f"I1 {v.sends}"
    assert v.max_net <= 1e-9 or v.trusting, f"I2 reversed: {v.max_net}"
    assert v.live_orders() <= 1, "I3"
    rows = rig.attempts()
    for r in rows:                                    # I4: nothing recorded beyond the venue's own rows
        o = v.orders.get(r["link_id"])
        recorded = float(r["filled_qty"])
        truth = o["filled"] if o else 0.0
        assert recorded <= truth + 1e-9, f"I4 {r['link_id']} recorded {recorded} > venue {truth}"
    if last.status == CE.FLAT:                        # I5
        assert abs(v.net) <= 1e-9, "I5 flat declared while the venue holds a position"
    assert last.status in (CE.FLAT, CE.HALT) or expect_flat is False, "I7 ended undecided"
    if last.status == CE.HALT:                        # I7: nothing silently dropped
        assert rig.store.inner.close_state(rig.cid)["halted_reason"] != ""
    if expect_flat is True:
        assert last.status == CE.FLAT


def sends(rig):
    return sum(rig.venue.sends.values())


# -- the ordinary close -----------------------------------------------------------------

def test_a_full_fill_closes_with_one_send_and_the_fill_is_booked_by_the_send(tmp_path):
    rig = Rig(tmp_path, 0.0100, policy=POL)
    last, _ = rig.run(dt=DT)
    invariants(rig, last, expect_flat=True)
    assert sends(rig) == 1 and last.explained and last.unbooked == ()
    assert rig.store.inner.close_state(rig.cid)["closed"] == 1


def test_flat_with_none_of_our_fills_is_reported_unexplained_and_sends_nothing(tmp_path):
    rig = Rig(tmp_path, 0.0, policy=POL)
    last, _ = rig.run(dt=DT)
    assert last.status == CE.FLAT and last.explained is False
    assert last.detail.get("flat_unexplained") is True
    assert sends(rig) == 0


# -- acknowledgement faults ----------------------------------------------------------------

def test_a_lost_acknowledgement_is_found_by_lookup_and_nothing_is_sent_twice(tmp_path):
    rig = Rig(tmp_path, 0.0100, policy=POL, modes=["lost"])
    last, _ = rig.run(dt=DT)
    invariants(rig, last, expect_flat=True)
    assert sends(rig) == 1
    # the fill was found by reading the venue, so the ledger never saw it
    assert last.unbooked == (CP.link_id_for(rig.cid, 1),)


def test_a_lost_ack_with_a_lagging_lookup_and_position_never_sends_a_second_order(tmp_path):
    rig = Rig(tmp_path, 0.0100, policy=POL, modes=["lost"], order_lag_reads=4,
              position_lag_reads=3)
    last, _ = rig.run(dt=DT)
    invariants(rig, last)
    assert sends(rig) == 1


def test_an_ack_that_claims_a_fill_the_venue_does_not_show_is_not_a_fill(tmp_path):
    rig = Rig(tmp_path, 0.0100, policy=POL, modes=["live"], ack_claims_fill=True)
    ex = rig.executor()
    r = ex.step(rig.cid)
    assert r.status == CE.IN_PROGRESS and r.filled == 0.0
    assert rig.venue.net == pytest.approx(-0.0100)
    for _ in range(5):                              # still live: never a second order beside it
        rig.clock.advance(DT)
        r = ex.step(rig.cid)
        assert r.status == CE.IN_PROGRESS and r.reason == CP.R_LIVE and r.filled == 0.0
    assert sends(rig) == 1
    link = CP.link_id_for(rig.cid, 1)
    rig.venue.finish_live(link)
    rig.clock.advance(DT)
    last, _ = rig.run(ex, dt=DT)
    invariants(rig, last, expect_flat=True)
    assert sends(rig) == 1


def test_a_request_that_never_landed_is_replaced_only_after_the_window_passes(tmp_path):
    rig = Rig(tmp_path, 0.0100, policy=POL, modes=["drop", "full"], window=60.0)
    times = []
    last, _ = rig.run(dt=DT, on_step=lambda i, r: times.append((rig.clock(), r.sent)))
    invariants(rig, last, expect_flat=True)
    rows = rig.attempts()
    assert [r["state"] for r in rows][:2] == [CP.NEVER_SENT, CP.FILLED]
    first_intent = float(rows[0]["intent_epoch"])
    second_intent = float(rows[1]["intent_epoch"])
    assert second_intent - first_intent >= CE.INTENT_TO_SEND_MAX_S + 60.0
    assert len(rig.venue.sends) == 2 and all(n == 1 for n in rig.venue.sends.values())


def test_a_rejected_close_is_resolved_and_the_residual_is_never_dropped(tmp_path):
    rig = Rig(tmp_path, 0.0100, policy=POL, modes=["reject", "reject", "reject"])
    last, _ = rig.run(dt=DT)
    assert last.status == CE.HALT and last.reason == CP.R_EXHAUSTED
    assert rig.venue.net == pytest.approx(-0.0100)          # still short: a human is called
    assert [r["state"] for r in rig.attempts()] == [CP.REJECTED] * 3
    invariants(rig, last)


# -- partial fills --------------------------------------------------------------------------------

def test_a_partial_fill_then_cancel_is_finished_by_a_second_order_for_the_residual(tmp_path):
    rig = Rig(tmp_path, 0.0100, policy=POL, modes=["partial:0.4", "full"])
    last, _ = rig.run(dt=DT)
    invariants(rig, last, expect_flat=True)
    rows = rig.attempts()
    assert rows[0]["state"] == CP.PARTIAL and rows[0]["filled_qty"] == pytest.approx(0.004)
    assert rows[1]["qty"] == pytest.approx(0.006)
    assert sum(float(r["filled_qty"]) for r in rows) == pytest.approx(0.0100)


def test_partial_fills_that_keep_coming_up_short_end_in_a_halt_with_the_residual_on_file(tmp_path):
    rig = Rig(tmp_path, 0.0100, policy=POL,
              modes=["partial:0.1", "partial:0.1", "partial:0.1"])
    last, _ = rig.run(dt=DT)
    assert last.status == CE.HALT and last.reason == CP.R_EXHAUSTED
    assert -rig.venue.net > 0.005
    assert len(rig.attempts()) == 3
    invariants(rig, last)


# -- position-read faults -----------------------------------------------------------------------------

def test_a_stale_position_read_after_a_fill_does_not_trigger_a_second_order(tmp_path):
    pol = CP.Policy(max_attempts=3, qty_step=0.001, min_qty=0.001, max_waits=30,
                    max_lag_waits=10)
    rig = Rig(tmp_path, 0.0100, policy=pol, position_lag_reads=3)
    last, _ = rig.run(dt=DT)
    invariants(rig, last, expect_flat=True)
    assert sends(rig) == 1


def test_an_unreadable_position_sends_nothing_and_ends_in_a_bounded_halt(tmp_path):
    pol = CP.Policy(max_attempts=3, qty_step=0.001, min_qty=0.001, max_waits=5,
                    max_lag_waits=3)
    rig = Rig(tmp_path, 0.0100, policy=pol, position_unreadable_reads=10_000)
    last, _ = rig.run(dt=DT)
    assert last.status == CE.HALT and last.reason == CP.R_WAIT_EXHAUSTED
    assert sends(rig) == 0
    assert rig.venue.net == pytest.approx(-0.0100)


def test_a_position_that_reads_long_halts_and_sends_nothing(tmp_path):
    rig = Rig(tmp_path, 0.0100, policy=POL, position_says_long=True)
    last, _ = rig.run(dt=DT)
    assert last.status == CE.HALT and last.reason == CP.R_NET_LONG
    assert sends(rig) == 0


def test_executions_that_disagree_with_the_order_row_are_not_a_fill(tmp_path):
    pol = CP.Policy(max_attempts=3, qty_step=0.001, min_qty=0.001, max_waits=6,
                    max_lag_waits=3)
    rig = Rig(tmp_path, 0.0100, policy=pol, exec_mismatch=True)
    last, _ = rig.run(dt=DT)
    assert last.status == CE.HALT
    assert sends(rig) == 1                                   # nothing replaces an unreadable fill
    assert float(rig.attempts()[0]["filled_qty"]) == 0.0     # and it was not recorded as one
    invariants(rig, last)


def test_a_close_that_cannot_be_confirmed_halts_and_keeps_the_attempt_unresolved(tmp_path):
    pol = CP.Policy(max_attempts=3, qty_step=0.001, min_qty=0.001, max_waits=6,
                    max_lag_waits=3)
    rig = Rig(tmp_path, 0.0100, policy=pol, modes=["lost"], order_unreadable_reads=10_000)
    last, _ = rig.run(dt=DT)
    assert last.status == CE.HALT
    assert sends(rig) == 1
    assert rig.attempts()[0]["state"] in ("INTENT", "SENT")   # still ambiguous, on file


# -- restart at every transition ------------------------------------------------------------------------

STORE_POINTS = [("apply_close_step", w, n) for w in ("before", "after") for n in (1, 2, 3, 4)] \
    + [("mark_close_sent", w, 1) for w in ("before", "after")]


@pytest.mark.parametrize("method,when,nth", STORE_POINTS)
@pytest.mark.parametrize("modes", [["full"], ["lost"], ["partial:0.5", "full"], ["drop", "full"]])
def test_a_crash_at_any_store_write_never_resends_and_never_resets_history(
        tmp_path, method, when, nth, modes):
    rig = Rig(tmp_path, 0.0100, policy=POL, modes=list(modes))
    rig.store.arm(method, when, nth)
    seen = {"attempts": 0, "waits": 0}

    def watch(i, r):
        n = len(rig.attempts())
        assert n >= seen["attempts"], "I6 attempts went down"
        seen["attempts"] = n
    # the arm is consumed by the first crash; later restarts reopen the file clean
    last, _ = rig.run(dt=DT, on_step=watch, steps=200)
    invariants(rig, last)
    assert last.status in (CE.FLAT, CE.HALT)
    assert rig.restarts <= 1


@pytest.mark.parametrize("link_n", [1, 2])
@pytest.mark.parametrize("modes", [["full", "full"], ["partial:0.5", "full"]])
def test_an_order_the_venue_accepted_just_before_the_crash_is_found_not_resent(
        tmp_path, link_n, modes):
    rig = Rig(tmp_path, 0.0100, policy=POL, modes=list(modes))
    rig.venue.crash_after_accept.add(CP.link_id_for(rig.cid, link_n))
    last, _ = rig.run(dt=DT, steps=200)
    invariants(rig, last)
    crashed = CP.link_id_for(rig.cid, link_n)
    if crashed not in rig.venue.sends:          # the close finished before that attempt existed
        assert rig.restarts == 0
        return
    assert rig.restarts >= 1 and rig.venue.sends[crashed] == 1
    if last.status == CE.FLAT:
        assert crashed in last.unbooked                       # found by reading, never booked


def test_a_crash_before_the_request_leaves_is_replaced_only_after_the_proof(tmp_path):
    rig = Rig(tmp_path, 0.0100, policy=POL)
    rig.venue.crash_before_send.add(CP.link_id_for(rig.cid, 1))
    last, _ = rig.run(dt=DT, steps=200)
    invariants(rig, last, expect_flat=True)
    rows = rig.attempts()
    assert rows[0]["state"] == CP.NEVER_SENT and rows[1]["state"] == CP.FILLED
    assert float(rows[1]["intent_epoch"]) - float(rows[0]["intent_epoch"]) >= 65.0


def test_a_restart_keeps_the_wait_counter_so_patience_cannot_be_reset_by_crashing(tmp_path):
    pol = CP.Policy(max_attempts=3, qty_step=0.001, min_qty=0.001, max_waits=6,
                    max_lag_waits=3)
    rig = Rig(tmp_path, 0.0100, policy=pol, modes=["live"])
    ex = rig.executor()
    for _ in range(3):
        ex.step(rig.cid)
        rig.clock.advance(DT)
    before = rig.store.inner.close_state(rig.cid)["waits"]
    assert before >= 2
    ex = rig.restart()
    assert rig.store.inner.close_state(rig.cid)["waits"] == before
    last, _ = rig.run(ex, dt=DT)
    assert last.status == CE.HALT and last.reason == CP.R_WAIT_EXHAUSTED
    assert sends(rig) == 1


def test_a_stale_intent_left_by_a_dead_process_is_never_sent_from_here(tmp_path):
    rig = Rig(tmp_path, 0.0100, policy=POL)
    rig.store.inner.apply_close_step(
        rig.cid, new_attempt={"n": 1, "link_id": CP.link_id_for(rig.cid, 1),
                              "qty": 0.0100, "short_before": 0.0100},
        waits=0, lag_waits=0, now=rig.clock())
    ex = rig.restart()
    r = ex.step(rig.cid)
    assert sends(rig) == 0 and r.status == CE.IN_PROGRESS and r.reason == CP.R_UNPROVEN


def test_a_clock_jump_between_intent_and_send_aborts_the_send(tmp_path):
    rig = Rig(tmp_path, 0.0100, policy=POL)
    real = rig.store.inner.apply_close_step

    def slow(*a, **kw):
        out = real(*a, **kw)
        if kw.get("new_attempt"):
            rig.clock.advance(CE.INTENT_TO_SEND_MAX_S + 1)
        return out
    rig.store.inner.apply_close_step = slow
    r = rig.executor().step(rig.cid)
    assert sends(rig) == 0 and r.reason == CE.R_STALE_INTENT


# -- halt, refusal, kill switch ----------------------------------------------------------------------------

def test_a_halt_is_sticky_across_steps_and_restarts_and_sends_nothing_more(tmp_path):
    rig = Rig(tmp_path, 0.0100, policy=POL, position_says_long=True)
    ex = rig.executor()
    assert ex.step(rig.cid).status == CE.HALT
    rig.venue.position_says_long = False
    for _ in range(3):
        r = ex.step(rig.cid)
        assert r.status == CE.HALT and r.reason == CE.R_HALTED_BEFORE
    r = rig.restart().step(rig.cid)
    assert r.status == CE.HALT and sends(rig) == 0


def test_a_refused_close_halts_and_leaves_the_short_on_file_not_abandoned(tmp_path):
    rig = Rig(tmp_path, 0.0100, policy=POL, exit_allowed=False)
    last, _ = rig.run(dt=DT)
    assert last.status == CE.HALT and last.reason == CE.R_REFUSED
    assert rig.venue.net == pytest.approx(-0.0100)
    assert [r["state"] for r in rig.attempts()] == [CP.NEVER_SENT]
    assert sends(rig) == 0


def test_a_kill_switch_that_closes_exits_mid_close_stops_after_the_in_flight_order(tmp_path):
    rig = Rig(tmp_path, 0.0100, policy=POL, modes=["partial:0.4"])
    ex = rig.executor()
    ex.step(rig.cid)
    rig.venue.exit_allowed = False                  # the gate closes while the close is in flight
    last, _ = rig.run(ex, dt=DT)
    assert last.status == CE.HALT and last.reason == CE.R_REFUSED
    rows = rig.attempts()
    assert rows[0]["state"] == CP.PARTIAL and rows[1]["state"] == CP.NEVER_SENT
    assert -rig.venue.net == pytest.approx(0.006)
    invariants(rig, last)


def test_an_unwritable_log_means_nothing_is_sent(tmp_path):
    rig = Rig(tmp_path, 0.0100, policy=POL)

    def boom(*a, **kw):
        raise OSError("disk full")
    rig.store.inner.apply_close_step = boom
    r = rig.executor().step(rig.cid)
    assert r.status == CE.HALT and r.reason == CE.R_LOG_UNWRITABLE and sends(rig) == 0


def test_an_unreadable_log_halts_without_sending(tmp_path):
    rig = Rig(tmp_path, 0.0100, policy=POL)

    def boom(*a, **kw):
        raise OSError("corrupt")
    rig.store.inner.close_attempts = boom
    r = rig.executor().step(rig.cid)
    assert r.status == CE.HALT and r.reason == CE.R_LOG_UNREADABLE and sends(rig) == 0


# -- reconciliation disagreeing with the ledger -----------------------------------------------------------------

def test_a_venue_that_shows_less_filled_than_the_log_recorded_is_not_believed_down(tmp_path):
    rig = Rig(tmp_path, 0.0100, policy=POL, modes=["live"])
    ex = rig.executor()
    ex.step(rig.cid)
    link = CP.link_id_for(rig.cid, 1)
    rig.venue.finish_live(link, filled=0.004)
    rig.clock.advance(DT)
    ex.step(rig.cid)
    recorded = float(rig.attempts()[0]["filled_qty"])
    assert recorded == pytest.approx(0.004)
    rig.venue.orders[link]["filled"] = 0.001                # a contradictory later read
    rig.venue.orders[link]["execs"] = [("x1", 0.001)]
    rig.clock.advance(DT)
    ex.step(rig.cid)
    assert float(rig.attempts()[0]["filled_qty"]) >= recorded


# -- the fuzz ---------------------------------------------------------------------------------------------------

MODES = ["full", "full", "partial:0.3", "partial:0.7", "lost", "drop", "reject", "live"]


@pytest.mark.parametrize("seed", range(300))
def test_no_fault_schedule_breaks_an_invariant(tmp_path, seed):
    rng = random.Random(seed)
    short = rng.choice([0.004, 0.01, 0.0127, 0.05])
    rig = Rig(tmp_path, short, policy=POL,
              modes=[rng.choice(MODES) for _ in range(6)],
              position_lag_reads=rng.choice([0, 0, 2]),
              position_unreadable_reads=rng.choice([0, 0, 2]),
              order_lag_reads=rng.choice([0, 0, 3]),
              order_unreadable_reads=rng.choice([0, 0, 2]),
              hide_orders=rng.random() < 0.1)
    for n in (1, 2, 3):
        if rng.random() < 0.2:
            rig.venue.crash_after_accept.add(CP.link_id_for(rig.cid, n))
        elif rng.random() < 0.1:
            rig.venue.crash_before_send.add(CP.link_id_for(rig.cid, n))
    if rng.random() < 0.5:
        rig.store.arm(rng.choice(["apply_close_step", "mark_close_sent"]),
                      rng.choice(["before", "after"]), rng.randint(1, 4))
    state = {"attempts": 0}

    def watch(i, r):
        # a live order is finished by the venue after a while, as a real one is
        for link, o in list(rig.venue.orders.items()):
            if o["status"] in CP.LIVE_STATUSES and rng.random() < 0.4:
                rig.venue.finish_live(link, filled=rng.choice([None, 0.0, 0.002]))
        n = len(rig.attempts())
        assert n >= state["attempts"]
        state["attempts"] = n
    last, _ = rig.run(dt=DT, steps=400, on_step=watch)
    invariants(rig, last)
    assert last.status in (CE.FLAT, CE.HALT)
    assert len(rig.attempts()) <= POL.max_attempts
