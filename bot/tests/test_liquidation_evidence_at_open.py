"""The liquidation read is evidence from the moment a pair opens.

Before: the gate's read was recorded only on later held ticks, and a view the
venue would not give left the engine halted with NO row saying so. A pair could
exist for a whole tick with no liquidation evidence, and an unreadable view was
invisible in the record. Now: right after both legs land the engine takes one
liquidation read and records it (reason prefixed AT_OPEN:), an unreadable read
is recorded as an explicit row, and none of it changes a trading decision.
Real CarryEngine and StateStore over the file's existing fake venue. Offline.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from carry_engine import BookState                      # noqa: E402
from persistence import StateStore                      # noqa: E402
from test_carry_engine import SPOT, FakeBroker, engine  # noqa: E402


class ViewBroker(FakeBroker):
    def __init__(self, view=None, raises=None, **kw):
        super().__init__(**kw)
        self.view, self.raises, self.view_reads = view, raises, 0

    def get_liquidation_view(self, symbol):
        self.view_reads += 1
        if self.raises is not None:
            raise self.raises
        return self.view


GOOD = {"size": 1.0, "side": "Sell", "mark": 100_000.0, "liq_price": 130_000.0,
        "distance_pct": 30.0, "account_mm_rate": 0.05,
        "margin_mode": "ISOLATED_MARGIN", "reason": ""}


@pytest.fixture()
def store(tmp_path):
    s = StateStore(str(tmp_path / "state.db"))
    yield s
    s.close()


def opened(broker, sink, gate=True):
    eng = engine(broker, require_liquidation_check=gate, risk_observer=sink)
    decision = eng.on_candle(spot=SPOT, mark=100_000.0, funding_bps=1.0,
                             timestamp_ms=1_700_000_000_000)
    return eng, decision


def test_a_read_is_recorded_when_the_pair_opens(store):
    broker = ViewBroker(view=dict(GOOD))
    eng, d = opened(broker, store.record_risk_observation)
    assert d.action == "opened" and eng.state is BookState.HEDGED
    (o,) = store.load_risk_observations()
    assert o["reason"] == "AT_OPEN:"
    assert o["liq_price"] == 130_000.0 and o["distance_pct"] == pytest.approx(30.0)
    assert o["account_mm_rate"] == pytest.approx(0.05)
    assert o["size"] == pytest.approx(1.0)


def test_an_unreadable_view_at_open_is_recorded_not_silent(store):
    broker = ViewBroker(raises=RuntimeError("venue timeout"))
    eng, d = opened(broker, store.record_risk_observation)
    assert d.action == "opened"                     # evidence never decides
    (o,) = store.load_risk_observations()
    assert o["reason"].startswith("AT_OPEN:VIEW_UNREADABLE:RuntimeError")
    assert o["liq_price"] is None and o["distance_pct"] is None


def test_a_view_that_is_not_a_mapping_is_recorded_as_unreadable(store):
    broker = ViewBroker(view="garbage")
    opened(broker, store.record_risk_observation)
    (o,) = store.load_risk_observations()
    assert o["reason"].startswith("AT_OPEN:VIEW_UNREADABLE:str")


def test_a_held_tick_that_cannot_read_the_view_leaves_a_row_and_halts(store):
    broker = ViewBroker(view=dict(GOOD))
    eng, _ = opened(broker, store.record_risk_observation)
    broker.raises = ConnectionError("down")
    eng._last_tick_ms = 1_700_000_100_000
    d = eng._check_liquidation_distance()
    assert d is not None and d.reason == "LIQUIDATION_PRICE_UNREADABLE"
    rows = store.load_risk_observations()
    assert rows[-1]["ms"] == 1_700_000_100_000
    assert rows[-1]["reason"].startswith("VIEW_UNREADABLE:ConnectionError")
    assert rows[-1]["liq_price"] is None


def test_nothing_is_read_or_recorded_when_the_gate_is_off(store):
    broker = ViewBroker(view=dict(GOOD))
    opened(broker, store.record_risk_observation, gate=False)
    assert broker.view_reads == 0
    assert store.load_risk_observations() == []


def test_a_failing_observer_cannot_change_the_open():
    def broken(_o):
        raise RuntimeError("disk full")
    broker = ViewBroker(view=dict(GOOD))
    eng, d = opened(broker, broken)
    assert d.action == "opened" and eng.state is BookState.HEDGED


def test_a_breach_at_open_is_recorded_and_the_next_tick_still_decides(store):
    inside = dict(GOOD, liq_price=105_000.0, distance_pct=5.0)
    broker = ViewBroker(view=inside)
    eng, d = opened(broker, store.record_risk_observation)
    assert d.action == "opened"                     # the open itself is untouched
    (o,) = store.load_risk_observations()
    assert o["distance_pct"] == pytest.approx(5.0)  # but the breach is on record
    eng._last_tick_ms = 1_700_000_100_000
    d2 = eng._check_liquidation_distance()
    assert d2 is not None and d2.reason.startswith("LIQUIDATION_")
