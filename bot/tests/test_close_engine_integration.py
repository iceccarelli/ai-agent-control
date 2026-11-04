"""The staged close wired into CarryEngine: what the BOOK does with each verdict.

The executor's own safety is pinned in test_close_execution_faults. Here the
question is only whether the engine maps it onto the book honestly: FLAT only
on an explained, booked, venue-confirmed flat; UNWINDING while the close is in
flight (written to disk each tick); HALT with the residual recorded; and a
restart that picks the same close up instead of resetting or ignoring it.
"""
from __future__ import annotations

import json
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
for p in (os.path.dirname(HERE), HERE):
    if p not in sys.path:
        sys.path.insert(0, p)

import close_plan as CP                                              # noqa: E402
from carry_engine import (OVERLAY, ACQUIRE, BookState, CarryEngine,  # noqa: E402
                          CarryPosition, ColdStart, Leg)
from close_exec_harness import Rig                                    # noqa: E402

MARK = 100_000.0
POL = CP.Policy(max_attempts=3, qty_step=0.001, min_qty=0.001, max_waits=30, max_lag_waits=3)
OPENED = 1_700_000_000_000


class Persist:
    def __init__(self):
        self.states = []

    def __call__(self, state):
        self.states.append(json.loads(json.dumps(state, default=str)))


def make(rig, *, mode=OVERLAY, with_executor=True, persist=None, qty=0.01):
    persist = persist or Persist()
    tripped = []
    broker = rig.venue
    broker.get_mark = lambda s: MARK
    eng = CarryEngine(
        broker=broker, kill_switch=tripped.append, borrow_apr=0.05,
        execution_mode=mode, persist=persist,
        close_executor=rig.executor() if with_executor else None)
    eng.position = CarryPosition(
        spot=Leg(symbol="BTCUSDT", side="Buy", product="spot", requested_qty=qty,
                 filled_qty=qty, avg_price=MARK),
        perp=Leg(symbol="BTCUSDT", side="Sell", product="linear", requested_qty=qty,
                 filled_qty=qty, avg_price=MARK),
        opened_ms=OPENED)
    eng.state = BookState.HEDGED
    rig.cid = eng.position.cycle_id
    return eng, persist, tripped


def tick(eng, rig, n=1):
    out = None
    for _ in range(n):
        out = eng.on_candle(mark=MARK, funding_bps=1.0, spot=MARK, timestamp_ms=OPENED + 10_000)
        rig.clock.advance(10.0)
    return out


def test_without_an_executor_the_original_single_order_close_is_untouched(tmp_path):
    rig = Rig(tmp_path, 0.01, policy=POL)
    eng, _, _ = make(rig, with_executor=False)
    rig.venue.get_perp_position = lambda s: abs(rig.venue.net)
    eng._unwind(MARK, "TEST")
    assert rig.venue.placed and not rig.attempts()      # the close log was never used


def test_acquire_mode_never_uses_the_staged_close(tmp_path):
    rig = Rig(tmp_path, 0.01, policy=POL)
    eng, _, _ = make(rig, mode=ACQUIRE)
    rig.venue.get_perp_position = lambda s: abs(rig.venue.net)
    try:
        eng._unwind(MARK, "TEST")
    except Exception:
        pass
    assert not rig.attempts()


def test_a_booked_full_close_makes_the_book_flat(tmp_path):
    rig = Rig(tmp_path, 0.01, policy=POL)
    eng, persist, tripped = make(rig)
    d = eng._unwind(MARK, "FUNDING_FLIP")
    assert d.state is BookState.FLAT and eng.position is None and not tripped
    assert rig.venue.net == pytest.approx(0.0)
    assert persist.states[-1]["book_state"] == "FLAT"


def test_a_close_in_flight_keeps_the_book_unwinding_and_writes_it_down_each_tick(tmp_path):
    rig = Rig(tmp_path, 0.01, policy=POL, modes=["live"])
    eng, persist, tripped = make(rig)
    d = eng._unwind(MARK, "FUNDING_FLIP")
    assert eng.state is BookState.UNWINDING and eng.position is not None
    assert persist.states[-1]["book_state"] == "UNWINDING"
    for _ in range(3):
        tick(eng, rig)
        assert eng.state is BookState.UNWINDING and not tripped
    assert sum(rig.venue.sends.values()) == 1            # no second order beside the live one
    rig.venue.finish_live(CP.link_id_for(rig.cid, 1))
    tick(eng, rig)
    # it ended at the venue, but this process never saw the fill: not booked
    assert eng.state is BookState.HALTED and tripped == ["UNWIND_FILL_UNBOOKED"]


def test_a_fill_found_only_by_reading_the_venue_halts_unbooked_and_keeps_the_position(tmp_path):
    rig = Rig(tmp_path, 0.01, policy=POL, modes=["lost"])
    eng, persist, tripped = make(rig)
    eng._unwind(MARK, "FUNDING_FLIP")
    tick(eng, rig, 3)
    assert eng.state is BookState.HALTED and tripped == ["UNWIND_FILL_UNBOOKED"]
    assert eng.position is not None                      # the journal still shows the hedge
    assert rig.venue.net == pytest.approx(0.0)


def test_flat_that_our_fills_do_not_explain_halts(tmp_path):
    rig = Rig(tmp_path, 0.0, policy=POL)
    eng, _, tripped = make(rig)
    eng._unwind(MARK, "RISK")
    assert eng.state is BookState.HALTED and tripped == ["UNWIND_FLAT_UNEXPLAINED"]
    assert eng.position is not None


def test_exhausted_attempts_halt_with_the_venue_residual_written_into_the_position(tmp_path):
    rig = Rig(tmp_path, 0.01, policy=POL, modes=["partial:0.1"] * 3)
    eng, persist, tripped = make(rig)
    eng._unwind(MARK, "RISK")
    tick(eng, rig, 40)
    assert eng.state is BookState.HALTED and tripped[-1] == "UNWIND_PARTIAL"
    assert eng.position.perp.filled_qty == pytest.approx(-rig.venue.net)
    assert eng.position.perp.filled_qty > 0.005


def test_a_halted_book_sends_nothing_further(tmp_path):
    rig = Rig(tmp_path, 0.01, policy=POL, position_says_long=True)
    eng, _, tripped = make(rig)
    eng._unwind(MARK, "RISK")
    assert eng.state is BookState.HALTED
    n = len(rig.venue.placed)
    tick(eng, rig, 5)
    assert len(rig.venue.placed) == n == 0


def test_the_kill_switch_does_not_stop_a_reduce_only_exit_in_flight(tmp_path):
    rig = Rig(tmp_path, 0.01, policy=POL, modes=["partial:0.5", "full"])
    eng, _, tripped = make(rig)
    eng.kill_switch("OPERATOR_STOP")                      # engaged; the gate for exits stays open
    eng._unwind(MARK, "RISK")
    tick(eng, rig, 10)
    assert eng.state is BookState.FLAT and eng.position is None
    assert rig.venue.net == pytest.approx(0.0) and rig.venue.max_net <= 1e-9


# -- restart ---------------------------------------------------------------------------------

def ledger_of(persist):
    return persist.states[-1]


def test_a_restart_during_a_close_resumes_it_and_does_not_reset_the_attempts(tmp_path):
    rig = Rig(tmp_path, 0.01, policy=POL, modes=["partial:0.4", "live"])
    eng, persist, _ = make(rig)
    eng._unwind(MARK, "RISK")
    tick(eng, rig, 2)
    before = rig.attempts()
    assert len(before) == 2
    ledger = ledger_of(persist)
    assert ledger["book_state"] == "UNWINDING"
    # the process dies; a new engine over the same log and the same venue
    rig.restart()
    eng2, persist2, tripped2 = make(rig, persist=Persist())
    eng2.position = None
    eng2.state = BookState.FLAT
    rig.venue.orders[CP.link_id_for(rig.cid, 2)]["status"] = "New"
    dec = eng2.cold_start(ledger=ledger, venue_perp_qty=abs(rig.venue.net),
                          venue_spot_qty=1.0, open_orders=())
    assert dec.action is ColdStart.RESUME and dec.detail.get("close_resume")
    assert eng2.state is BookState.UNWINDING and eng2.position is not None
    after = rig.attempts()
    assert [a["link_id"] for a in after] == [a["link_id"] for a in before]
    assert sum(rig.venue.sends.values()) == 2             # nothing was re-sent by resuming
    rig.venue.finish_live(CP.link_id_for(rig.cid, 2))
    tick(eng2, rig, 5)
    assert eng2.state in (BookState.FLAT, BookState.HALTED)
    assert max(rig.venue.sends.values()) == 1


def test_a_restart_without_a_close_log_still_halts_on_an_unwinding_ledger(tmp_path):
    rig = Rig(tmp_path, 0.01, policy=POL)
    eng, persist, _ = make(rig)
    ledger = {"book_state": "UNWINDING", "execution_mode": "overlay",
              "position": eng.to_state()["position"]}
    dec = eng.cold_start(ledger=ledger, venue_perp_qty=0.01, venue_spot_qty=1.0)
    assert dec.action is ColdStart.HALT


def test_an_executor_free_engine_still_halts_on_an_unwinding_ledger(tmp_path):
    rig = Rig(tmp_path, 0.01, policy=POL)
    eng, persist, _ = make(rig, with_executor=False)
    ledger = {"book_state": "UNWINDING", "execution_mode": "overlay",
              "position": eng.to_state()["position"]}
    dec = eng.cold_start(ledger=ledger, venue_perp_qty=0.01, venue_spot_qty=1.0)
    assert dec.action is ColdStart.HALT
