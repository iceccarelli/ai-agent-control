"""The book states what it is waiting for, from the same gate it enters on."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import carry_costs as cc                                     # noqa: E402
from test_paired_leg_recovery import FeeVenue, MARK, modern, stack, warm_and_open  # noqa: E402

KW = dict(perp=60000.0, spot=60000.0, borrow_apr=0.05, round_trip_bps=30.0)


def passes(f, **kw):
    return bool(cc.entry_gate(funding_prints_bps=[f] * 3, **{**KW, **kw}))


def test_breakeven_is_the_edge_of_the_production_gate():
    need = cc.breakeven_funding_bps(**KW)
    assert need is not None and need > 0
    assert passes(need + 1e-6) and not passes(need - 1e-3)


def test_higher_costs_need_more_funding():
    base = cc.breakeven_funding_bps(**KW)
    assert cc.breakeven_funding_bps(**{**KW, "round_trip_bps": 60.0}) > base
    assert cc.breakeven_funding_bps(**{**KW, "borrow_apr": 0.15}) > base
    assert cc.breakeven_funding_bps(**{**KW, "opportunity_cost_apr": 0.05}) > base


def test_no_funding_clears_an_absurd_cost():
    assert cc.breakeven_funding_bps(**{**KW, "round_trip_bps": 5000.0}) is None


def test_outlook_waits_when_funding_is_below_the_need(tmp_path):
    store, venue, broker, engine = stack(str(tmp_path / "s.db"),
                                         FeeVenue(modern, spot_qty=0.0))
    engine._funding_history = [0.1, 0.1, 0.1]
    o = engine.opportunity_outlook(mark=MARK, spot=MARK)
    assert o["status"] == "WAITING_FOR_FUNDING"
    assert o["gap_bps_per_print"] > 0 and not o["holding"]
    assert "negative funding prints" in o["exit_rule"]
    store.close()


def test_outlook_says_conditions_met_when_the_gate_would_pass(tmp_path):
    store, venue, broker, engine = stack(str(tmp_path / "s.db"),
                                         FeeVenue(modern, spot_qty=0.0))
    engine._funding_history = [3.0, 3.0, 3.0]
    o = engine.opportunity_outlook(mark=MARK, spot=MARK)
    assert o["status"] == "ENTRY_CONDITIONS_MET"
    store.close()


def test_outlook_holding_and_unreadable(tmp_path):
    store, venue, broker, engine = stack(str(tmp_path / "s.db"),
                                         FeeVenue(modern, spot_qty=0.0))
    assert engine.opportunity_outlook(mark=MARK, spot=None)["why"] == \
        "SPOT_OR_MARK_UNAVAILABLE"
    warm_and_open(engine)
    assert engine.opportunity_outlook(mark=MARK, spot=MARK)["status"] == "HOLDING"
    store.close()
