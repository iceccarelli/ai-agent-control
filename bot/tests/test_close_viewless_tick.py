"""A staged close must keep stepping when a tick has no usable market view.

The audit found that `check_venue_position_without_a_view` compared the venue's
short to the size the book recorded at the OPEN. Mid-close, a partial fill makes
the venue's short smaller on purpose, so a transient loss of the market view
(a REST hiccup, or the live feed going quiet) halted the book with
VENUE_POSITION_DRIFT and stranded the remainder of the short. The viewless watch
must hand an in-flight staged close to the same executor the normal tick uses.
"""
from __future__ import annotations

import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
for p in (os.path.dirname(HERE), HERE):
    if p not in sys.path:
        sys.path.insert(0, p)

import close_plan as CP                                               # noqa: E402
from carry_engine import BookState                                    # noqa: E402
from close_exec_harness import Rig                                    # noqa: E402
from test_close_engine_integration import MARK, POL, make, tick       # noqa: E402


def rig_with_partial_close(tmp_path, modes):
    rig = Rig(tmp_path, 0.01, policy=POL, modes=modes)
    eng, persist, tripped = make(rig)
    rig.venue.get_perp_position = lambda s: abs(rig.venue.net)
    eng._unwind(MARK, "FUNDING_FLIP")                      # attempt 1 fills half
    return rig, eng, tripped


def test_a_partial_close_with_no_market_view_does_not_halt_on_drift(tmp_path):
    rig, eng, tripped = rig_with_partial_close(tmp_path, ["partial:0.5", "full"])
    assert eng.state is BookState.UNWINDING
    assert eng.position.perp.filled_qty == pytest.approx(0.01)      # the book's recorded open size
    assert -rig.venue.net == pytest.approx(0.005)                   # the venue is, correctly, smaller
    rig.clock.advance(10.0)
    d = eng.check_venue_position_without_a_view()                    # a tick with no usable view
    assert tripped == [] and eng.state is not BookState.HALTED, d.reason
    assert d is not None and d.reason != "VENUE_POSITION_DRIFT"


def test_the_viewless_watch_finishes_the_close_it_would_have_stranded(tmp_path):
    rig, eng, tripped = rig_with_partial_close(tmp_path, ["partial:0.5", "full"])
    for _ in range(40):
        rig.clock.advance(10.0)
        eng.check_venue_position_without_a_view()
        if eng.state is not BookState.UNWINDING:
            break
    assert rig.venue.net == pytest.approx(0.0)                       # flat at the venue
    assert eng.state in (BookState.FLAT, BookState.HALTED)
    # whatever it ended as, it was never the drift halt
    assert "VENUE_POSITION_DRIFT" not in tripped


def test_viewless_and_viewful_ticks_send_the_same_orders(tmp_path):
    a_rig, a, _ = rig_with_partial_close(tmp_path / "a", ["partial:0.5", "full"])
    b_rig, b, _ = rig_with_partial_close(tmp_path / "b", ["partial:0.5", "full"])
    for _ in range(6):
        a_rig.clock.advance(10.0)
        a.check_venue_position_without_a_view()
        tick(b, b_rig)
    assert [p[0] for p in a_rig.venue.placed] == [p[0] for p in b_rig.venue.placed]


def test_a_book_that_is_not_closing_is_still_checked_for_drift(tmp_path):
    rig = Rig(tmp_path, 0.01, policy=POL)
    eng, _, tripped = make(rig)
    rig.venue.get_perp_position = lambda s: 0.003             # the venue holds less than the book says
    eng.state = BookState.HEDGED
    d = eng.check_venue_position_without_a_view()
    assert d.reason == "VENUE_POSITION_DRIFT" and tripped == ["VENUE_POSITION_DRIFT"]


def test_an_engine_without_the_staged_close_is_unchanged(tmp_path):
    rig = Rig(tmp_path, 0.01, policy=POL)
    eng, _, tripped = make(rig, with_executor=False)
    rig.venue.get_perp_position = lambda s: 0.003
    eng.state = BookState.UNWINDING
    assert eng.check_venue_position_without_a_view().reason == "VENUE_POSITION_DRIFT"
