"""A durable order lifecycle: every state, every transition, and the races.

An order's fate is a state machine written to the store, not a variable in a
running process. These tests drive the real StateStore (a temp file, reopened to
prove restarts) through the legal paths and every awkward one: a lost
acknowledgement, a duplicate event, a stale report, a cancel that loses to a
fill, a fill after a cancel, an overfill, a request that goes unanswered. The
rules they pin: a timeout is never a rejection; fills only move forward; a
terminal order never silently changes; evidence that contradicts the record
(a fill on a "cancelled" order) turns it into RECONCILIATION_REQUIRED instead of
being absorbed; a retry is refused unless the order was never sent. Offline.
"""
from __future__ import annotations

import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import order_lifecycle as ol                         # noqa: E402
from persistence import StateStore                   # noqa: E402

LINK = "BB-carr-1-abc"


@pytest.fixture()
def path(tmp_path):
    return str(tmp_path / "s.db")


@pytest.fixture()
def store(path):
    s = StateStore(path)
    yield s
    try:
        s.close()
    except sqlite3.ProgrammingError:
        pass                      # the test closed it to prove a restart


def intent(store, link=LINK, qty=1.0, leg="spot", cycle="cyc-1", side="Buy"):
    return store.record_order_intent(link, "BTCUSDT", side, qty, leg=leg,
                                     cycle_id=cycle)


def ev(store, kind, link=LINK, **kw):
    return store.apply_order_event(link, kind, **kw)


def status(store, link=LINK):
    return store.get_order(link)["status"]


# -- the pure machine ------------------------------------------------------------------

def test_the_unresolved_and_terminal_sets_partition_the_states():
    assert ol.TERMINAL.isdisjoint(ol.UNRESOLVED)
    assert ol.TERMINAL | ol.UNRESOLVED == set(ol.ALL_STATES)
    assert {"filled", "cancelled", "rejected", "never_sent", "abandoned"} == ol.TERMINAL
    assert {"pending", "submitted", "acknowledged", "partial", "cancel_requested",
            "timed_out", "unknown", "reconciliation_required"} == ol.UNRESOLVED


@pytest.mark.parametrize("state", sorted(ol.TERMINAL))
def test_a_terminal_state_has_no_ordinary_exit(state):
    for kind in (ol.SUBMIT_ATTEMPT, ol.ACK, ol.REJECT, ol.TIMEOUT, ol.CANCEL_REQUEST,
                 ol.CANCEL_CONFIRM, ol.NEVER_SENT, ol.ABANDON, ol.UNKNOWN_OUTCOME):
        t = ol.decide(state, kind, recorded_qty=0.0, requested_qty=1.0)
        assert t.new_status is None, (state, kind)


# -- intent --------------------------------------------------------------------------------

def test_an_intent_is_durable_and_unique(store, path):
    assert intent(store) is True
    assert intent(store) is False                 # the same id is the same order
    row = store.get_order(LINK)
    assert row["status"] == "pending" and row["filled_qty"] == 0.0
    assert row["cycle_id"] == "cyc-1" and row["leg"] == "spot"
    store.close()
    again = StateStore(path)
    try:
        assert again.get_order(LINK)["status"] == "pending"
        assert [o["order_link_id"] for o in again.unresolved_carry_orders()] == [LINK]
    finally:
        again.close()


# -- the happy path, step by step ---------------------------------------------------------------

def test_the_full_path_to_filled(store):
    intent(store)
    assert ev(store, ol.SUBMIT_ATTEMPT).status == "submitted"
    assert ev(store, ol.ACK, exchange_id="ex-9").status == "acknowledged"
    assert store.get_order(LINK)["exchange_id"] == "ex-9"
    assert ev(store, ol.FILL_REPORT, filled_qty=0.4, avg_price=100.0).status == "partial"
    assert ev(store, ol.FILL_REPORT, filled_qty=1.0, avg_price=101.0).status == "filled"
    row = store.get_order(LINK)
    assert row["filled_qty"] == pytest.approx(1.0) and row["avg_price"] == pytest.approx(101.0)
    assert store.unresolved_carry_orders() == []


def test_every_event_is_logged_with_before_and_after(store):
    intent(store)
    ev(store, ol.SUBMIT_ATTEMPT)
    ev(store, ol.ACK, exchange_id="x")
    log = store.order_events(LINK)
    assert [(e["kind"], e["status_before"], e["status_after"]) for e in log] == [
        ("submit_attempt", "pending", "submitted"),
        ("ack", "submitted", "acknowledged")]


# -- duplicates, stale reports, ordering -----------------------------------------------------------

def test_a_duplicate_event_changes_nothing(store):
    intent(store)
    ev(store, ol.SUBMIT_ATTEMPT)
    first = ev(store, ol.FILL_REPORT, filled_qty=0.5, avg_price=100.0, event_key="e1")
    second = ev(store, ol.FILL_REPORT, filled_qty=0.5, avg_price=100.0, event_key="e1")
    assert first.applied and not second.applied and second.reason == "DUPLICATE_EVENT"
    assert len(store.order_events(LINK)) == 2


def test_a_stale_lower_fill_report_never_moves_fills_backwards(store):
    intent(store)
    ev(store, ol.SUBMIT_ATTEMPT)
    ev(store, ol.FILL_REPORT, filled_qty=0.7, avg_price=100.0)
    r = ev(store, ol.FILL_REPORT, filled_qty=0.3, avg_price=99.0)
    assert not r.applied and r.reason == "STALE_FILL_REPORT"
    assert store.get_order(LINK)["filled_qty"] == pytest.approx(0.7)
    assert status(store) == "partial"


def test_a_repeat_of_the_same_cumulative_fill_is_a_no_op(store):
    intent(store)
    ev(store, ol.SUBMIT_ATTEMPT)
    ev(store, ol.FILL_REPORT, filled_qty=1.0, avg_price=100.0)
    r = ev(store, ol.FILL_REPORT, filled_qty=1.0, avg_price=100.0)
    assert not r.applied and status(store) == "filled"


def test_an_overfill_is_not_absorbed(store):
    intent(store)
    ev(store, ol.SUBMIT_ATTEMPT)
    r = ev(store, ol.FILL_REPORT, filled_qty=1.5, avg_price=100.0)
    assert r.status == "reconciliation_required"
    assert store.get_order(LINK)["filled_qty"] == pytest.approx(1.5)   # kept, not hidden


# -- the unanswered request -------------------------------------------------------------------------

def test_a_timeout_is_not_a_rejection_and_stays_unresolved(store):
    intent(store)
    ev(store, ol.SUBMIT_ATTEMPT)
    assert ev(store, ol.TIMEOUT).status == "timed_out"
    assert [o["order_link_id"] for o in store.unresolved_carry_orders()] == [LINK]
    # the venue turns out to have filled it: that is a legal, recorded outcome
    assert ev(store, ol.FILL_REPORT, filled_qty=1.0, avg_price=100.0).status == "filled"


def test_an_unknown_outcome_resolves_either_way(store):
    for link, kind, kw, final in (
            ("a", ol.REJECT, {}, "rejected"),
            ("b", ol.FILL_REPORT, {"filled_qty": 1.0, "avg_price": 5.0}, "filled"),
            ("c", ol.ACK, {"exchange_id": "x"}, "acknowledged")):
        intent(store, link=link)
        ev(store, ol.SUBMIT_ATTEMPT, link=link)
        ev(store, ol.UNKNOWN_OUTCOME, link=link)
        assert status(store, link) == "unknown"
        assert ev(store, kind, link=link, **kw).status == final


def test_a_retry_of_a_sent_order_is_refused(store):
    intent(store)
    ev(store, ol.SUBMIT_ATTEMPT)
    ev(store, ol.TIMEOUT)
    r = ev(store, ol.SUBMIT_ATTEMPT)
    assert not r.applied and r.reason.startswith("ILLEGAL_TRANSITION")
    assert status(store) == "timed_out"


def test_never_sent_is_only_a_conclusion_not_a_default(store):
    intent(store)
    assert ev(store, ol.NEVER_SENT, detail={"proof": "pending at restart"}).status == "never_sent"
    intent(store, link="b")
    ev(store, ol.SUBMIT_ATTEMPT, link="b")
    ev(store, ol.TIMEOUT, link="b")
    assert status(store, "b") == "timed_out"      # nothing concluded it for us


# -- cancel races -------------------------------------------------------------------------------------

def test_a_cancel_that_loses_to_a_fill_ends_filled(store):
    intent(store)
    ev(store, ol.SUBMIT_ATTEMPT); ev(store, ol.ACK, exchange_id="x")
    assert ev(store, ol.CANCEL_REQUEST).status == "cancel_requested"
    r = ev(store, ol.FILL_REPORT, filled_qty=1.0, avg_price=100.0)
    assert r.status == "filled"


def test_a_confirmed_cancel_keeps_what_had_filled(store):
    intent(store)
    ev(store, ol.SUBMIT_ATTEMPT); ev(store, ol.ACK, exchange_id="x")
    ev(store, ol.FILL_REPORT, filled_qty=0.4, avg_price=100.0)
    ev(store, ol.CANCEL_REQUEST)
    r = ev(store, ol.CANCEL_CONFIRM, filled_qty=0.4)
    assert r.status == "cancelled"
    assert store.get_order(LINK)["filled_qty"] == pytest.approx(0.4)


def test_a_cancel_request_is_not_a_cancellation(store):
    intent(store)
    ev(store, ol.SUBMIT_ATTEMPT); ev(store, ol.ACK, exchange_id="x")
    ev(store, ol.CANCEL_REQUEST)
    assert status(store) == "cancel_requested"
    assert store.unresolved_carry_orders()        # still unresolved


def test_a_fill_after_a_confirmed_cancel_is_a_contradiction_not_a_fact(store):
    intent(store)
    ev(store, ol.SUBMIT_ATTEMPT); ev(store, ol.ACK, exchange_id="x")
    ev(store, ol.CANCEL_REQUEST); ev(store, ol.CANCEL_CONFIRM, filled_qty=0.0)
    r = ev(store, ol.FILL_REPORT, filled_qty=0.6, avg_price=100.0)
    assert r.applied and r.status == "reconciliation_required"
    assert store.get_order(LINK)["filled_qty"] == pytest.approx(0.6)


def test_a_cancel_cannot_be_requested_for_an_order_never_sent(store):
    intent(store)
    r = ev(store, ol.CANCEL_REQUEST)
    assert not r.applied and status(store) == "pending"


# -- rejects, abandonment -------------------------------------------------------------------------------

def test_a_rejection_with_fills_is_a_contradiction(store):
    intent(store)
    ev(store, ol.SUBMIT_ATTEMPT)
    ev(store, ol.FILL_REPORT, filled_qty=0.5, avg_price=100.0)
    r = ev(store, ol.REJECT)
    assert r.status == "reconciliation_required"


def test_exposure_cannot_be_abandoned(store):
    intent(store)
    ev(store, ol.SUBMIT_ATTEMPT)
    ev(store, ol.FILL_REPORT, filled_qty=0.5, avg_price=100.0)
    r = ev(store, ol.ABANDON)
    assert not r.applied and status(store) == "partial"
    intent(store, link="n")
    ev(store, ol.SUBMIT_ATTEMPT, link="n"); ev(store, ol.TIMEOUT, link="n")
    assert ev(store, ol.ABANDON, link="n", detail={"proof": "operator"}).status == "abandoned"


def test_an_unknown_order_id_is_refused_not_created(store):
    r = ev(store, ol.ACK, link="never-recorded", exchange_id="x")
    assert not r.applied and r.reason == "NO_SUCH_ORDER"
    assert store.get_order("never-recorded") is None


# -- scope, persistence, migration ---------------------------------------------------------------------

def test_only_carry_orders_are_in_the_carry_unresolved_set(store):
    store.record_order("dir-1", "BTCUSDT", "Buy", "Market", 1.0, purpose="entry")
    intent(store)
    assert [o["order_link_id"] for o in store.unresolved_carry_orders()] == [LINK]
    assert "dir-1" in {o["order_link_id"] for o in store.unresolved_orders()}


def test_the_new_states_count_as_unresolved_everywhere(store):
    for n, kinds in enumerate(((ol.SUBMIT_ATTEMPT, ol.TIMEOUT),
                               (ol.SUBMIT_ATTEMPT, ol.ACK),
                               (ol.SUBMIT_ATTEMPT, ol.ACK, ol.CANCEL_REQUEST),
                               (ol.SUBMIT_ATTEMPT, ol.RECONCILE_REQUIRED))):
        link = f"o{n}"
        intent(store, link=link)
        for k in kinds:
            ev(store, k, link=link)
    got = {o["order_link_id"] for o in store.unresolved_orders()}
    assert {"o0", "o1", "o2", "o3"} <= got
    pend = {o["order_link_id"] for o in store.pending_orders()}
    assert {"o1", "o2"} <= pend


def test_state_survives_a_restart_mid_lifecycle(store, path):
    intent(store)
    ev(store, ol.SUBMIT_ATTEMPT); ev(store, ol.ACK, exchange_id="x")
    ev(store, ol.FILL_REPORT, filled_qty=0.4, avg_price=100.0)
    store.close()
    again = StateStore(path)
    try:
        row = again.get_order(LINK)
        assert row["status"] == "partial" and row["filled_qty"] == pytest.approx(0.4)
        assert again.apply_order_event(LINK, ol.FILL_REPORT, filled_qty=1.0,
                                       avg_price=100.5).status == "filled"
        assert len(again.order_events(LINK)) == 4
    finally:
        again.close()


def test_an_older_database_gains_the_columns_additively(tmp_path):
    path = str(tmp_path / "old.db")
    StateStore(path).close()
    con = sqlite3.connect(path)
    con.execute("DROP TABLE orders")
    con.execute("CREATE TABLE orders (order_link_id TEXT PRIMARY KEY, symbol TEXT NOT NULL,"
                " side TEXT NOT NULL, order_type TEXT NOT NULL, qty REAL NOT NULL,"
                " price REAL NOT NULL DEFAULT 0.0, purpose TEXT NOT NULL DEFAULT 'entry',"
                " status TEXT NOT NULL DEFAULT 'pending', exchange_id TEXT NOT NULL DEFAULT '',"
                " created_epoch REAL NOT NULL, updated_epoch REAL NOT NULL,"
                " meta TEXT NOT NULL DEFAULT '{}')")
    con.execute("INSERT INTO orders(order_link_id,symbol,side,order_type,qty,created_epoch,"
                "updated_epoch) VALUES('legacy','BTCUSDT','Buy','Market',1,1,1)")
    con.commit()
    con.close()
    s = StateStore(path)
    try:
        legacy = s.get_order("legacy")
        assert legacy["filled_qty"] == 0.0 and legacy["cycle_id"] == ""
        assert intent(s) is True and status(s) == "pending"
    finally:
        s.close()


def test_two_threads_claiming_one_id_get_exactly_one_intent(path):
    import threading
    s = StateStore(path)
    wins = []
    def claim():
        wins.append(s.record_order_intent("race", "BTCUSDT", "Buy", 1.0, leg="spot",
                                          cycle_id="c"))
    threads = [threading.Thread(target=claim) for _ in range(8)]
    [t.start() for t in threads]; [t.join() for t in threads]
    s.close()
    assert wins.count(True) == 1 and wins.count(False) == 7
