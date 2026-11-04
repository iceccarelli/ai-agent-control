"""The close planner's DEFAULT: never re-send an order id; replace an attempt
only once it is PROVEN never to have landed.

Whether a venue rejects a repeated orderLinkId (and for how long) is not
something this repository has measured. The default policy therefore must not
rely on it: an attempt the venue does not show is waited on, and only the
caller's proof of absence (past the acceptance window) lets a NEW attempt under
a NEW link id follow. Same-id re-send exists only as an explicit opt-in.
"""
from __future__ import annotations

import os
import random
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
BOT = os.path.dirname(HERE)
for p in (BOT, HERE):
    if p not in sys.path:
        sys.path.insert(0, p)

import close_plan as CP                                         # noqa: E402
from close_fault_venue import Timeout, Venue, drive            # noqa: E402

DEFAULT = CP.Policy(max_attempts=3, qty_step=0.001, min_qty=0.001)


def lid(n):
    return CP.link_id_for("c1", n)


def plan(attempts, net, *, orders=None, proven=(), policy=DEFAULT, waits=0):
    return CP.plan_close(
        "c1", attempts,
        CP.Observation(net_qty=net, orders=orders or {}, absence_proven=tuple(proven),
                       waits=waits),
        policy)


def sent(n=1, qty=0.01, **kw):
    return CP.Attempt(n, lid(n), qty, CP.SENT, short_before=0.01, **kw)


def test_the_default_policy_does_not_resend_the_same_link_id():
    assert CP.Policy().resend_same_link_id is False


def test_an_unseen_attempt_without_proof_waits_and_sends_nothing():
    p = plan([sent()], -0.01)
    assert (p.action, p.reason, p.attempt) == (CP.WAIT, CP.R_UNPROVEN, 1)
    assert p.link_id == lid(1) and p.qty == 0.0


def test_an_unseen_attempt_is_never_resent_however_often_it_is_asked():
    for waits in range(0, DEFAULT.max_waits):
        assert plan([sent()], -0.01, waits=waits).action == CP.WAIT


def test_the_wait_is_bounded_and_ends_in_a_halt():
    p = plan([sent()], -0.01, waits=DEFAULT.max_waits)
    assert (p.action, p.reason) == (CP.HALT, CP.R_WAIT_EXHAUSTED)


def test_a_proven_absent_attempt_is_resolved_never_sent_and_replaced_under_a_new_id():
    p = plan([sent()], -0.01, proven=[lid(1)])
    assert p.action == CP.SEND and p.attempt == 2
    assert p.link_id == lid(2) != lid(1)
    assert p.reduce_only and p.qty == 0.01
    assert [(n, st, f) for n, st, f, _ in p.resolve] == [(1, CP.NEVER_SENT, 0.0)]


def test_the_resolution_and_the_new_intent_apply_together():
    log = (sent(),)
    p = plan(list(log), -0.01, proven=[lid(1)])
    after = CP.apply_plan(log, p)
    assert [(a.n, a.state) for a in after] == [(1, CP.NEVER_SENT), (2, CP.INTENT)]
    assert after[1].short_before == pytest.approx(0.01)


def test_proof_for_an_attempt_that_left_a_trace_is_not_believed():
    with_fill = sent(filled_qty=0.004)
    with_exec = sent(exec_ids=("x1",))
    for a in (with_fill, with_exec):
        p = plan([a], -0.006, proven=[lid(1)])
        assert (p.action, p.reason) == (CP.WAIT, CP.R_UNPROVEN), a


def test_a_visible_order_outranks_a_claimed_absence():
    live = CP.OrderObs("New", 0.0)
    p = plan([sent()], -0.01, orders={lid(1): live}, proven=[lid(1)])
    assert (p.action, p.reason) == (CP.WAIT, CP.R_LIVE)


def test_proof_for_one_attempt_does_not_clear_another():
    a1 = sent(1)
    a2 = CP.Attempt(2, lid(2), 0.01, CP.SENT, short_before=0.01)
    p = plan([a1, a2], -0.01, proven=[lid(1)])
    assert (p.action, p.reason) == (CP.WAIT, CP.R_UNPROVEN) and p.attempt == 2


def test_a_never_sent_attempt_still_spends_the_attempt_budget():
    log = [CP.Attempt(n, lid(n), 0.01, CP.NEVER_SENT) for n in (1, 2)] + [sent(3)]
    p = plan(log, -0.01, proven=[lid(3)])
    assert (p.action, p.reason) == (CP.HALT, CP.R_EXHAUSTED)


def test_a_flat_venue_makes_unseen_attempts_moot_not_never_sent():
    p = plan([sent()], 0.0)
    assert p.action == CP.NOOP_FLAT and p.resolve[0][1] == CP.MOOT


def test_a_net_long_venue_still_halts_whatever_is_unseen():
    assert plan([sent()], 0.01, proven=[lid(1)]).reason == CP.R_NET_LONG


# -- fault schedules, default policy ------------------------------------------------------

MODES = ["full", "full", "partial:0.3", "partial:0.7", "lost", "drop", "reject", "late"]


def _venue(seed):
    rng = random.Random(seed)
    return Venue(rng.choice([0.004, 0.01, 0.0127, 0.05]),
                 modes=[rng.choice(MODES) for _ in range(6)],
                 position_lag_reads=rng.choice([0, 0, 2]),
                 order_lag_reads=rng.choice([0, 0, 3]),
                 unreadable_reads=rng.choice([0, 0, 2]),
                 trusting=rng.random() < 0.3,
                 never_shows_orders=rng.random() < 0.15,
                 order_unreadable_reads=rng.choice([0, 0, 2]))


@pytest.mark.parametrize("seed", range(300))
def test_no_fault_schedule_sends_an_id_twice_or_reverses_or_runs_two_live_orders(seed):
    v = _venue(seed)
    rng = random.Random(seed + 1)
    log, last, steps = drive(v, policy=DEFAULT, restart_rng=rng, restart_p=0.25,
                             max_steps=200, durable_counters=bool(seed % 2))
    assert last.action in (CP.NOOP_FLAT, CP.HALT), "the run must end flat or in a named HALT"
    # THE property of the default: no order id is ever put on the wire twice.
    assert all(n <= 1 for n in v.sends_by_link.values()), v.sends_by_link
    assert v.duplicate_sends_seen == 0
    assert v.live_orders() <= 1
    if v.trusting:
        assert v.max_net_seen <= 1e-9, "the position reversed"
    if last.action == CP.NOOP_FLAT:
        assert v.net == pytest.approx(0.0, abs=1e-9)
    assert len(log) <= DEFAULT.max_attempts


def test_a_venue_that_hides_a_rejected_order_ends_in_a_bounded_halt_with_one_send():
    v = Venue(0.0100, modes=["reject"], never_shows_orders=True)
    log, last, steps = drive(v, policy=DEFAULT, max_steps=200)
    assert last.action == CP.HALT and last.reason == CP.R_WAIT_EXHAUSTED
    assert sum(v.sends_by_link.values()) == 1
    assert v.net == pytest.approx(-0.0100)            # the short is still open: a human is called
    assert steps <= DEFAULT.max_waits + 5


def test_a_hidden_order_that_filled_is_settled_by_the_venue_position_alone():
    v = Venue(0.0100, never_shows_orders=True)
    log, last, _ = drive(v, policy=DEFAULT, max_steps=200)
    assert last.action == CP.NOOP_FLAT
    assert log[0].state == CP.MOOT                    # its fills are NOT known from the planner
    assert sum(v.sends_by_link.values()) == 1


def test_a_dropped_request_is_replaced_by_a_new_id_after_proof():
    v = Venue(0.0100, modes=["drop", "full"])
    log, last, _ = drive(v, policy=DEFAULT, max_steps=60)
    assert last.action == CP.NOOP_FLAT
    assert [a.state for a in log][:2] == [CP.NEVER_SENT, CP.FILLED]
    assert len(v.sends_by_link) == 2 and all(n == 1 for n in v.sends_by_link.values())


def test_a_lost_acknowledgement_is_found_by_lookup_and_nothing_is_sent_twice():
    v = Venue(0.0100, modes=["lost"])
    log, last, _ = drive(v, policy=DEFAULT, max_steps=60)
    assert last.action == CP.NOOP_FLAT
    assert sum(v.sends_by_link.values()) == 1


def test_a_venue_that_would_reaccept_an_id_is_never_given_the_chance():
    v = Venue(0.0100, modes=["lost"], order_lag_reads=4, dup_accepts_after_final=True)
    log, last, _ = drive(v, policy=DEFAULT, max_steps=100)
    assert all(n == 1 for n in v.sends_by_link.values())
    assert v.bought <= 0.0100 + 1e-12


def test_the_opt_in_still_resends_the_same_id():
    opt_in = CP.Policy(max_attempts=3, qty_step=0.001, min_qty=0.001,
                       resend_same_link_id=True)
    assert plan([sent()], -0.01, policy=opt_in).action == CP.RESEND
