"""The close state machine under deterministic fault injection.

No network, no client, no engine: a pure planner against a fake venue that
loses responses, fills partially, fills late, reads stale and restarts."""
from __future__ import annotations

import ast
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
from close_fault_venue import Timeout, Venue, drive              # noqa: E402

# Semantics DELIBERATELY changed (see close_plan.py STATUS): the planner no longer
# re-sends an unseen attempt under the same link id unless the policy opts in. The
# tests below that exercise the re-send do so with this explicit opt-in; the
# default behaviour is covered by test_close_plan_absence_proof.py.
POL = CP.Policy(max_attempts=3, qty_step=0.001, min_qty=0.001, resend_same_link_id=True)


def lid(n):
    return CP.link_id_for("c1", n)


def plan(attempts, net, orders=None, **kw):
    return CP.plan_close("c1", attempts, CP.Observation(net_qty=net, orders=orders or {},
                                                        order_read_failed=kw.pop("failed", ())),
                         POL, **kw)


# -- the planner, case by case ----------------------------------------------------------------

def test_flat_means_nothing_to_do():
    assert plan([], 0.0).action == CP.NOOP_FLAT


def test_a_short_is_closed_with_a_deterministic_reduce_only_attempt_sized_from_the_venue():
    p = plan([], -0.0127, book_short_qty=0.5)          # the BOOK says 0.5; the venue says 0.0127
    assert (p.action, p.qty, p.reduce_only) == (CP.SEND, 0.012, True)
    assert p.link_id == lid(1) and p.attempt == 1


def test_the_quantity_never_exceeds_the_short_the_venue_reports():
    for short in (0.001, 0.0019, 0.5, 3.1415):
        p = plan([], -short)
        assert p.action == CP.SEND and p.qty <= short + 1e-12


def test_an_unreadable_position_proposes_nothing():
    p = plan([], None)
    assert (p.action, p.reason) == (CP.WAIT, CP.R_UNREADABLE)


def test_a_net_long_position_is_never_closed_with_a_buy():
    p = plan([], +0.002)
    assert (p.action, p.reason) == (CP.HALT, CP.R_NET_LONG)


def test_a_live_close_order_blocks_every_other_order():
    a = CP.Attempt(1, lid(1), 0.01, CP.SENT)
    for status in ("New", "PartiallyFilled"):
        p = plan([a], -0.01, {lid(1): CP.OrderObs(status, 0.004, ("x1",))})
        assert (p.action, p.reason) == (CP.WAIT, CP.R_LIVE)


def test_an_attempt_the_venue_does_not_know_is_resent_under_the_same_link_id_only():
    a = CP.Attempt(1, lid(1), 0.01, CP.INTENT)
    p = plan([a], -0.01, {})
    assert (p.action, p.link_id, p.attempt) == (CP.RESEND, lid(1), 1)
    assert p.qty == 0.01


def test_a_resend_is_clamped_to_the_short_now_held():
    a = CP.Attempt(1, lid(1), 0.01, CP.SENT)
    p = plan([a], -0.004, {})
    assert p.action == CP.RESEND and p.qty == 0.004


def test_an_unknown_attempt_with_nothing_left_to_close_is_moot_not_resent():
    a = CP.Attempt(1, lid(1), 0.01, CP.SENT)
    p = plan([a], 0.0, {})
    assert p.action == CP.NOOP_FLAT


def test_an_unreadable_order_lookup_waits_it_does_not_guess():
    a = CP.Attempt(1, lid(1), 0.01, CP.SENT)
    p = plan([a], -0.01, {}, failed=(lid(1),))
    assert (p.action, p.reason) == (CP.WAIT, CP.R_ORDER_UNREADABLE)


def test_a_filled_attempt_that_left_a_remainder_starts_the_next_attempt_from_the_venue():
    a = CP.Attempt(1, lid(1), 0.01, CP.SENT)
    done = {lid(1): CP.OrderObs("Filled", 0.01, ("x1",))}
    p = plan([a], -0.0037, done)
    assert (p.action, p.attempt, p.qty, p.link_id) == (CP.SEND, 2, 0.003, lid(2))
    assert p.resolve == ((1, CP.FILLED, 0.01, ("x1",)),)


def test_a_partial_fill_that_ended_is_resolved_partial_then_the_remainder_is_sent():
    a = CP.Attempt(1, lid(1), 0.01, CP.SENT)
    ended = {lid(1): CP.OrderObs("Cancelled", 0.004, ("x1",))}
    p = plan([a], -0.006, ended)
    assert p.resolve[0][1] == CP.PARTIAL and p.action == CP.SEND and p.qty == 0.006


def test_a_rejected_attempt_is_resolved_and_counts_against_the_bound():
    a1 = CP.Attempt(1, lid(1), 0.01, CP.REJECTED)
    a2 = CP.Attempt(2, lid(2), 0.01, CP.REJECTED)
    a3 = CP.Attempt(3, lid(3), 0.01, CP.SENT)
    p = plan([a1, a2, a3], -0.01, {lid(3): CP.OrderObs("Rejected", 0.0)})
    assert (p.action, p.reason) == (CP.HALT, CP.R_EXHAUSTED)


def test_a_remainder_below_the_minimum_order_halts_for_a_human():
    p = plan([], -0.0004)
    assert (p.action, p.reason) == (CP.HALT, CP.R_DUST)


def test_a_position_read_that_lags_the_fills_waits_instead_of_sending_a_doomed_order():
    a = CP.Attempt(1, lid(1), 0.01, CP.SENT, short_before=0.01)
    done = {lid(1): CP.OrderObs("Filled", 0.01, ("x1",))}
    p = plan([a], -0.01, done)                          # still shows the old short
    assert (p.action, p.reason) == (CP.WAIT, CP.R_LAG)


def test_a_lagging_read_is_waited_out_across_steps_not_just_the_first():
    resolved = CP.Attempt(1, lid(1), 0.01, CP.FILLED, 0.01, ("x1",), 1, 0.01)
    p = plan([resolved], -0.01, {})
    assert (p.action, p.reason) == (CP.WAIT, CP.R_LAG)


def test_the_wait_for_a_lagging_read_is_bounded_then_the_reduce_only_order_is_proposed():
    resolved = CP.Attempt(1, lid(1), 0.01, CP.FILLED, 0.01, ("x1",), 1, 0.01)
    obs = CP.Observation(net_qty=-0.01, lag_waits=POL.max_lag_waits)
    p = CP.plan_close("c1", [resolved], obs, POL)
    assert p.action == CP.SEND and p.qty == 0.01 and p.reduce_only


def test_a_larger_short_than_the_book_is_a_real_excess_and_is_closed():
    a = CP.Attempt(1, lid(1), 0.01, CP.SENT, short_before=0.01)
    done = {lid(1): CP.OrderObs("Filled", 0.01, ("x1",))}
    p = plan([a], -0.02, done, )
    obs = CP.Observation(net_qty=-0.02, orders=done, lag_waits=POL.max_lag_waits)
    p = CP.plan_close("c1", [a], obs, POL)             # after the wait budget it is closed
    assert p.action == CP.SEND and p.qty == 0.02


@pytest.mark.parametrize("log", [
    [CP.Attempt(1, "a", 0.01, CP.SENT), CP.Attempt(1, "b", 0.01, CP.SENT)],   # duplicate n
    [CP.Attempt(1, "a", 0.01, CP.SENT), CP.Attempt(2, "a", 0.01, CP.SENT)],   # duplicate link id
    [CP.Attempt(0, "a", 0.01, CP.SENT)],                                      # bad n
])
def test_a_corrupt_log_halts(log):
    assert plan(log, -0.01).action == CP.HALT


@pytest.mark.parametrize("cycle", ["c1", "cyc-1793664000123", "cyc:with:colons.and.dots",
                                   "x" * 80, "cyc-é-ß", ""])
def test_every_link_id_is_venue_legal_deterministic_and_distinct_per_attempt(cycle):
    ids = [CP.link_id_for(cycle, n) for n in range(1, 6)]
    assert all(CP.valid_link_id(i) for i in ids), ids
    assert all(len(i) <= 36 for i in ids)
    assert len(set(ids)) == 5
    assert ids == [CP.link_id_for(cycle, n) for n in range(1, 6)]


def test_different_long_cycles_get_different_ids():
    a, b = "x" * 80 + "a", "x" * 80 + "b"
    assert CP.link_id_for(a, 1) != CP.link_id_for(b, 1)


@pytest.mark.parametrize("bad", ["a:b", "a.b", "", "x" * 37, "a b", "é"])
def test_the_venue_rule_for_link_ids_is_enforced(bad):
    assert not CP.valid_link_id(bad)


def test_the_planner_is_pure():
    a = CP.Attempt(1, lid(1), 0.01, CP.SENT)
    args = ([a], -0.0037, {lid(1): CP.OrderObs("Filled", 0.01, ("x1",))})
    assert plan(*args) == plan(*args)


def test_duplicate_execution_events_add_nothing():
    total, ids = CP.merge_fills((), [("x1", 0.004), ("x2", 0.003), ("x1", 0.004), ("x2", 0.003)])
    assert (total, ids) == (pytest.approx(0.007), ("x1", "x2"))
    again, ids2 = CP.merge_fills(ids, [("x1", 0.004), ("x3", 0.001)], prior_filled=total)
    assert again == pytest.approx(0.008) and ids2 == ("x1", "x2", "x3")


@pytest.mark.parametrize("bad", [("", 0.1), ("x", -1.0), ("x", float("nan"))])
def test_an_unusable_execution_event_is_refused_not_guessed(bad):
    with pytest.raises(ValueError):
        CP.merge_fills((), [bad])


# -- fault injection against the fake venue ------------------------------------------------------

def finish(venue, **kw):
    log, last, steps = drive(venue, **kw)
    return log, last, steps


def test_clean_close():
    v = Venue(0.0100)
    log, last, _ = finish(v)
    assert last.action == CP.NOOP_FLAT and v.net == pytest.approx(0.0)
    assert len(v.placed) == 1


def test_partial_fill_then_the_remainder_is_closed_and_never_overshoots():
    v = Venue(0.0100, modes=["partial:0.4", "full"])
    log, last, _ = finish(v)
    assert last.action == CP.NOOP_FLAT and v.bought == pytest.approx(0.0100)
    assert [round(q, 6) for _l, q, _s in v.placed] == [0.01, 0.006]


def test_a_lost_response_after_the_order_executed_does_not_send_a_second_order():
    v = Venue(0.0100, modes=["lost"])
    log, last, _ = finish(v)
    assert last.action == CP.NOOP_FLAT
    assert len(v.placed) == 1                           # the lookup found it; nothing re-sent
    assert v.duplicate_sends_seen == 0


def test_a_request_that_never_arrived_is_resent_once_under_the_same_link_id():
    v = Venue(0.0100, modes=["drop", "full"])
    log, last, _ = finish(v)
    assert last.action == CP.NOOP_FLAT
    assert [l for l, _q, _s in v.placed] == [CP.link_id_for("cyc-1", 1), CP.link_id_for("cyc-1", 1)]
    assert v.bought == pytest.approx(0.0100)


def test_a_late_fill_is_waited_for_not_doubled():
    v = Venue(0.0100, modes=["late"])
    log, last, _ = finish(v)
    assert last.action == CP.NOOP_FLAT and len(v.placed) == 1


def test_a_reject_is_retried_as_a_new_attempt_within_the_bound():
    v = Venue(0.0100, modes=["reject", "full"])
    log, last, _ = finish(v)
    assert last.action == CP.NOOP_FLAT and len(v.placed) == 2


def test_persistent_rejects_exhaust_the_bound_and_halt_with_the_short_still_known():
    v = Venue(0.0100, modes=["reject"] * 10)
    log, last, _ = finish(v)
    assert last.action == CP.HALT and last.reason == CP.R_EXHAUSTED
    assert len(v.placed) == POL.max_attempts and v.net == pytest.approx(-0.0100)


def test_a_stale_position_read_never_causes_an_oversized_or_reversing_order():
    v = Venue(0.0100, position_lag_reads=3)
    v._stale_net = -0.0100
    log, last, _ = finish(v)
    assert last.action == CP.NOOP_FLAT and v.net <= 1e-9
    # no order was ever refused as oversized: the lagging read was waited out
    assert not any(o["status"] == "Rejected" for o in v.orders.values())
    assert len(v.placed) == 1


def test_an_order_lookup_that_lags_does_not_create_a_second_order():
    v = Venue(0.0100, order_lag_reads=4)
    log, last, _ = finish(v)
    assert last.action == CP.NOOP_FLAT
    assert v.bought == pytest.approx(0.0100) and v.net <= 1e-9


def test_an_unreadable_position_sends_nothing_until_it_reads():
    v = Venue(0.0100, unreadable_reads=5)
    log, last, _ = finish(v)
    assert last.action == CP.NOOP_FLAT
    assert len(v.placed) == 1 and v.bought == pytest.approx(0.0100)


def test_a_position_that_is_already_net_long_is_never_touched():
    v = Venue(0.0100)
    v.net = +0.0050                                    # a reversal that already happened
    p = CP.plan_close("c1", (), v.observe(()), POL, book_short_qty=0.01)
    assert (p.action, p.reason) == (CP.HALT, CP.R_NET_LONG)
    assert v.placed == []


def test_a_crash_between_the_intent_and_the_send_resumes_by_resending_that_attempt():
    class CrashOnce(random.Random):
        calls = 0

        def random(self):                    # crash exactly at the first send
            CrashOnce.calls += 1
            return 0.0 if CrashOnce.calls == 1 else 1.0
    v = Venue(0.0100)
    log, last, _ = finish(v, restart_rng=CrashOnce(), restart_p=0.5)
    assert last.action == CP.NOOP_FLAT
    assert [l for l, _q, _s in v.placed] == [CP.link_id_for("cyc-1", 1)]      # sent once, after the resume


def test_even_a_lost_attempt_log_cannot_double_buy():
    """Worst case: the process dies and the durable log is GONE. The next run
    numbers its first attempt 1 again, so it reuses the link id; the venue
    refuses the duplicate and the position is read as it really is."""
    v = Venue(0.0100, modes=["lost"])
    drive(v, max_steps=1)                               # sends attempt 1 (executed, response lost)
    _log, last, _ = drive(v)                            # a fresh run with an EMPTY log
    assert last.action == CP.NOOP_FLAT
    assert v.bought == pytest.approx(0.0100) and v.net == pytest.approx(0.0)


# -- randomised fault schedules: the invariants hold for all of them ------------------------------

MODES = ["full", "full", "partial:0.3", "partial:0.7", "lost", "drop", "reject", "late"]


@pytest.mark.parametrize("seed", range(300))
def test_no_fault_schedule_reverses_the_position_or_doubles_an_order(seed):
    rng = random.Random(seed)
    short = rng.choice([0.001, 0.0037, 0.01, 0.0129, 0.25])
    v = Venue(short, modes=[rng.choice(MODES) for _ in range(12)],
              position_lag_reads=rng.choice([0, 0, 2]), order_lag_reads=rng.choice([0, 0, 3]),
              unreadable_reads=rng.choice([0, 0, 2]))
    if v.position_lag_reads:
        v._stale_net = -short
    log, last, steps = drive(v, restart_rng=random.Random(seed + 1), restart_p=0.3)
    # (the invariants - never net long, <=1 live order, never bought more than the
    # short - are asserted at every step inside `drive`)
    assert last.action in (CP.NOOP_FLAT, CP.HALT), (seed, last)
    if last.action == CP.NOOP_FLAT:
        assert v.net == pytest.approx(0.0, abs=1e-9)
    else:
        assert last.reason in (CP.R_EXHAUSTED, CP.R_DUST, CP.R_NET_LONG, CP.R_BAD_INPUT,
                               CP.R_RESEND_EXHAUSTED, CP.R_WAIT_EXHAUSTED)
        assert v.net <= 1e-9                               # whatever remains is a known short
    assert all(n <= POL.max_sends for n in v.sends_by_link.values())   # bounded exposure per id
    assert len({l for l, _q, _s in v.placed}) <= POL.max_attempts + 0


# -- this module is not wired into the order path ----------------------------------------------------

def test_the_live_code_does_not_import_the_planner():
    """It is a design under test, not a behaviour change: wiring it into the
    engine is an owner decision (it is multi-step close behaviour)."""
    offenders = []
    for base, _d, files in os.walk(BOT):
        if os.sep + "tests" in base or any(x in base for x in ("venv", ".venv", "__pycache__")):
            continue
        for f in files:
            if f.endswith(".py") and f != "close_plan.py":
                tree = ast.parse(open(os.path.join(base, f), encoding="utf-8").read())
                for n in ast.walk(tree):
                    mods = ([a.name for a in n.names] if isinstance(n, ast.Import)
                            else [n.module or ""] if isinstance(n, ast.ImportFrom) else [])
                    if any(m.split(".")[0] == "close_plan" for m in mods):
                        offenders.append(f)
    assert offenders == []


def test_the_planner_imports_nothing_that_can_send_an_order():
    tree = ast.parse(open(os.path.join(BOT, "close_plan.py")).read())
    mods = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            mods |= {a.name.split(".")[0] for a in n.names}
        elif isinstance(n, ast.ImportFrom):
            mods.add((n.module or "").split(".")[0])
    assert mods <= {"__future__", "hashlib", "math", "re", "dataclasses", "typing"}


# -- review round: statuses, bounds, ids, and venues that break the assumptions ------------------------------

@pytest.mark.parametrize("status", ["Created", "Untriggered", "Triggered", "Active"])
def test_documented_live_statuses_block_every_other_order(status):
    a = CP.Attempt(1, lid(1), 0.01, CP.SENT, short_before=0.01)
    p = plan([a], -0.01, {lid(1): CP.OrderObs(status, 0.0)})
    assert (p.action, p.reason) == (CP.WAIT, CP.R_LIVE)


@pytest.mark.parametrize("status", ["", "SomethingNew", "ACTIVE?", "filled"])
def test_a_status_that_is_not_understood_waits_and_is_never_resolved_or_replaced(status):
    a = CP.Attempt(1, lid(1), 0.01, CP.SENT, short_before=0.01)
    p = plan([a], -0.01, {lid(1): CP.OrderObs(status, 0.0)})
    assert (p.action, p.reason) == (CP.WAIT, CP.R_UNKNOWN_STATUS)
    assert p.resolve == ()


@pytest.mark.parametrize("status,state", [("PartiallyFilledCanceled", CP.PARTIAL),
                                          ("Deactivated", CP.CANCELLED),
                                          ("Cancelled", CP.CANCELLED), ("Rejected", CP.REJECTED)])
def test_documented_terminal_statuses_resolve(status, state):
    a = CP.Attempt(1, lid(1), 0.01, CP.SENT, short_before=0.01)
    filled = 0.004 if state == CP.PARTIAL else 0.0
    p = plan([a], -(0.01 - filled), {lid(1): CP.OrderObs(status, filled, ("x1",) if filled else ())})
    assert p.resolve[0][1] == state


def test_resending_one_attempt_is_bounded():
    a = CP.Attempt(1, lid(1), 0.01, CP.SENT, sends=POL.max_sends, short_before=0.01)
    p = plan([a], -0.01, {})
    assert (p.action, p.reason) == (CP.HALT, CP.R_RESEND_EXHAUSTED)
    below = CP.Attempt(1, lid(1), 0.01, CP.SENT, sends=POL.max_sends - 1, short_before=0.01)
    assert plan([below], -0.01, {}).action == CP.RESEND


def test_each_resend_is_counted_in_the_durable_log():
    log = ()
    p = plan(log, -0.01)
    log = CP.apply_plan(log, p)
    assert log[0].sends == 1 and log[0].short_before == pytest.approx(0.01)
    p2 = plan(log, -0.01, {})
    log = CP.apply_plan(log, p2)
    assert p2.action == CP.RESEND and log[0].sends == 2


def test_a_wait_of_any_kind_is_bounded_then_a_human_is_called():
    for net, orders in ((None, {}), (-0.01, {lid(1): CP.OrderObs("New", 0.0)})):
        a = [CP.Attempt(1, lid(1), 0.01, CP.SENT, short_before=0.01)] if orders else []
        obs = CP.Observation(net_qty=net, orders=orders, waits=POL.max_waits)
        p = CP.plan_close("c1", a, obs, POL)
        assert (p.action, p.reason) == (CP.HALT, CP.R_WAIT_EXHAUSTED)
        obs = CP.Observation(net_qty=net, orders=orders, waits=POL.max_waits - 1)
        assert CP.plan_close("c1", a, obs, POL).action == CP.WAIT


def test_alternating_unreadable_and_lagging_reads_still_end_in_a_halt():
    """P2-4: the lag counter alone never reached its budget when the reasons alternated."""
    v = Venue(0.01, modes=["partial:0.4"] * 20, unreadable_reads=0, position_lag_reads=10_000)
    v._stale_net = -0.01
    real = v.read_position
    flip = {"n": 0}

    def alternating():
        flip["n"] += 1
        return None if flip["n"] % 2 else real()
    v.read_position = alternating
    _log, last, steps = drive(v, max_steps=200)
    assert last.action in (CP.HALT, CP.NOOP_FLAT) and steps < 200


def test_a_partial_fill_with_a_lagging_read_waits_instead_of_exhausting_the_attempts():
    """P2-3: 40% of 0.01 filled; the read still shows 0.01. The next order must wait."""
    v = Venue(0.0100, modes=["partial:0.4", "full", "full"], position_lag_reads=3)
    v._stale_net = -0.0100
    log, last, _ = drive(v)
    assert last.action == CP.NOOP_FLAT and v.net == pytest.approx(0.0)
    assert not any(o["status"] == "Rejected" for o in v.orders.values())
    assert len(v.placed) == 2


def test_the_baseline_of_the_last_attempt_not_the_book_decides_what_should_remain():
    a = CP.Attempt(1, lid(1), 0.01, CP.PARTIAL, 0.004, ("x1",), 1, 0.01)
    assert plan([a], -0.006).action == CP.SEND                   # 0.01 - 0.004 remains: as expected
    assert plan([a], -0.010).action == CP.WAIT                   # read has not caught up


def test_link_ids_never_collide_across_cycles_that_sanitise_alike():
    cycles = ["a:b", "a.b", "a b", "a-b", "é", "ß", "cyc-1", "cyc 1", "x" * 80, "x" * 80 + "y"]
    ids = [CP.link_id_for(c, 1) for c in cycles]
    assert len(set(ids)) == len(ids)
    assert all(CP.valid_link_id(i) for i in ids)


def test_an_attempt_that_turns_out_moot_is_not_recorded_as_cancelled_with_zero_fills():
    a = CP.Attempt(1, lid(1), 0.01, CP.SENT, short_before=0.01)
    p = plan([a], 0.0, {})
    assert p.action == CP.NOOP_FLAT and p.resolve[0][1] == CP.MOOT


def test_a_lost_response_with_a_lagging_lookup_ends_flat_and_the_fills_are_not_called_zero():
    v = Venue(0.0100, modes=["lost"], order_lag_reads=3)
    log, last, _ = drive(v)
    assert last.action == CP.NOOP_FLAT
    assert v.bought == pytest.approx(0.0100)
    assert all(a.state != CP.CANCELLED or a.filled_qty == 0 for a in log)


# -- venues that break the design's assumptions: what still holds -------------------------------------------

def test_a_venue_that_never_shows_the_order_ends_in_a_bounded_halt_not_a_loop():
    v = Venue(0.0100, never_shows_orders=True)
    log, last, steps = drive(v)
    assert last.action in (CP.HALT, CP.NOOP_FLAT) and steps < 120
    assert all(n <= POL.max_sends for n in v.sends_by_link.values())


def test_a_venue_whose_order_lookup_is_unreadable_forever_ends_in_a_halt():
    v = Venue(0.0100, order_unreadable_reads=10 ** 6)
    log, last, steps = drive(v)
    assert last.action == CP.HALT and last.reason in (CP.R_WAIT_EXHAUSTED, CP.R_UNREADABLE)
    assert steps < 120 and len(v.placed) <= 1


def test_a_venue_that_reports_a_status_we_do_not_know_never_gets_a_second_order():
    v = Venue(0.0100, status_alias={"Filled": "WeirdDoneStatus"})
    log, last, steps = drive(v)
    assert len(v.placed) == 1                           # the order filled; its status is not understood
    assert last.action == CP.HALT and last.reason == CP.R_WAIT_EXHAUSTED


def test_a_venue_that_re_accepts_a_link_id_after_the_first_order_is_final_cannot_be_over_bought():
    """The design ASSUMES a duplicate orderLinkId is rejected. Bybit's docs do not
    say for how long. If a venue re-accepted it, the resend count is bounded and
    every resend is clamped to the short observed, so with reduce-only on the
    position still cannot reverse."""
    v = Venue(0.0100, modes=["lost"], order_lag_reads=4, position_lag_reads=2,
              dup_accepts_after_final=True)
    v._stale_net = -0.0100
    log, last, _ = drive(v)
    assert v.net <= 1e-9 and v.bought <= 0.0100 + 1e-9
    assert all(n <= POL.max_sends for n in v.sends_by_link.values())


def test_the_planner_alone_never_reverses_a_venue_that_does_not_enforce_reduce_only():
    """A 'trusting' venue would let an oversize Buy flip the position. With finite
    lags the planner must never propose one; drive() asserts it at every step."""
    for seed in range(120):
        rng = random.Random(seed)
        short = rng.choice([0.001, 0.0037, 0.01, 0.25])
        v = Venue(short, trusting=True, modes=[rng.choice(MODES) for _ in range(10)],
                  position_lag_reads=rng.choice([0, 1, 2, 3]), order_lag_reads=rng.choice([0, 2, 4]))
        v._stale_net = -short
        drive(v, restart_rng=random.Random(seed + 7), restart_p=0.3)
        assert v.max_net_seen <= 1e-9, seed


@pytest.mark.parametrize("seed", range(150))
def test_unbounded_adversity_still_terminates_in_a_named_state(seed):
    rng = random.Random(10_000 + seed)
    v = Venue(rng.choice([0.001, 0.01, 0.25]), modes=[rng.choice(MODES) for _ in range(30)],
              position_lag_reads=rng.choice([0, 3, 10 ** 6]),
              order_lag_reads=rng.choice([0, 3, 10 ** 6]),
              unreadable_reads=rng.choice([0, 3, 10 ** 6]),
              never_shows_orders=rng.random() < 0.2,
              status_alias=rng.choice([{}, {}, {"New": "Weird"}, {"Filled": "Weird"}]),
              dup_accepts_after_final=rng.random() < 0.3)
    if v.position_lag_reads:
        v._stale_net = -v.initial_short
    log, last, steps = drive(v, restart_rng=random.Random(seed), restart_p=0.2, max_steps=200,
                             durable_counters=True)
    assert last.action in (CP.NOOP_FLAT, CP.HALT) and steps < 200, (seed, last)
    assert v.net <= 1e-9
    assert all(n <= POL.max_sends for n in v.sends_by_link.values())


def test_losing_the_wait_counters_on_restart_only_delays_a_halt_it_never_sends_sooner():
    v = Venue(0.0100, unreadable_reads=10 ** 6)
    _l, last, steps = drive(v, restart_rng=random.Random(3), restart_p=0.2, max_steps=300)
    assert v.placed == []                       # nothing was ever sent into an unreadable position
    assert last.action in (CP.WAIT, CP.HALT)
