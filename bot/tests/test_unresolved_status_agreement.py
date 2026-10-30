"""Everything that asks 'is this order unresolved?' agrees with the state machine.

The set of unresolved statuses was written out by hand in three places
(persistence.unresolved_orders, carry_performance, and the machine itself) and
the 'contradicted' state added to the machine was missed by the report, so a
mainnet ledger with a contradicted fill could be classed ACTUAL_RECONCILED. The
machine's `UNRESOLVED` is now the single source; this pins every reader to it.
"""
from __future__ import annotations

import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "tools"))

import order_lifecycle as ol                                 # noqa: E402
import carry_performance as perf                             # noqa: E402
from persistence import StateStore                           # noqa: E402


def test_the_report_counts_exactly_the_machines_unresolved_states():
    assert set(perf._UNRESOLVED_ORDER_STATUSES) == set(ol.UNRESOLVED)


@pytest.mark.parametrize("state", sorted(ol.UNRESOLVED))
def test_the_store_lists_every_unresolved_state_as_unresolved(tmp_path, state):
    s = StateStore(str(tmp_path / "s.db"))
    try:
        s.record_order("o-1", "BTCUSDT", "Sell", "Market", 1.0,
                       purpose="carry-linear", status=state)
        assert [o["order_link_id"] for o in s.unresolved_orders()] == ["o-1"]
        assert [o["order_link_id"] for o in s.unresolved_carry_orders()] == ["o-1"]
    finally:
        s.close()


@pytest.mark.parametrize("state", sorted(ol.TERMINAL))
def test_the_store_never_lists_a_terminal_state_as_unresolved(tmp_path, state):
    s = StateStore(str(tmp_path / "s.db"))
    try:
        s.record_order("o-1", "BTCUSDT", "Sell", "Market", 1.0,
                       purpose="carry-linear", status=state)
        assert s.unresolved_orders() == []
    finally:
        s.close()


def test_a_contradicted_order_stops_a_mainnet_ledger_being_called_realised_cash(tmp_path):
    import ledger as L
    db = str(tmp_path / "s.db")
    s = StateStore(db)
    try:
        s.record_execution_environment("mainnet")
        j = L.Journal()
        s.save_ledger(j.to_rows())
        s.record_order("o-9", "BTCUSDT", "Sell", "Market", 1.0,
                       purpose="carry-linear", status="contradicted")
    finally:
        s.close()
    rep = perf.build_report(db, environment="mainnet", now_ms=1_700_000_000_000)
    assert rep["unresolved_carry_orders"] == 1
    assert rep["sections"]["realized_cash"]["evidence_class"] != "ACTUAL_RECONCILED"
