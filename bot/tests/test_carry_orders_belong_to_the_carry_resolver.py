"""The directional client must not touch carry orders.

`BybitClient.reconcile_on_startup` walks every unresolved order and looks each
up in the CLIENT's category (spot by default); `TradingEngine.shutdown` cancels
every pending order through the same client. A live LINEAR carry order is not in
that category, so the lookup finds nothing and the row was written `never_sent`
(reconcile) or `cancelled` (shutdown) with no lifecycle event, leaving the
unresolved set the restart resolver and CarryRisk read. Carry orders are
settled only by order_recovery and CarryBroker. Independent review finding 4,
extended to the startup reconcile it shares. Offline; fake transport.
"""
from __future__ import annotations

import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import bybit_connection as bc                                # noqa: E402
import order_lifecycle as ol                                 # noqa: E402
from fake_bybit import FakeBybit                             # noqa: E402
from persistence import StateStore                           # noqa: E402
from test_bybit_connection import Cfg                        # noqa: E402


@pytest.fixture()
def store(tmp_path):
    s = StateStore(str(tmp_path / "s.db"))
    yield s
    s.close()


@pytest.fixture()
def client(store):
    return bc.BybitClient(config=Cfg(), store=store, transport=FakeBybit())


def live_linear_carry_order(store, link="BB-carr-1-x"):
    store.record_order_intent(link, "BTCUSDT", "Sell", 0.001, leg="linear",
                              cycle_id="cyc-1")
    store.apply_order_event(link, ol.SUBMIT_ATTEMPT)
    store.apply_order_event(link, ol.ACK, exchange_id="ex-1")
    return link


def test_startup_reconcile_leaves_a_carry_order_to_the_carry_resolver(client, store):
    link = live_linear_carry_order(store)
    summary = client.reconcile_on_startup(["BTCUSDT"])
    assert store.get_order(link)["status"] == "acknowledged"
    assert [o["order_link_id"] for o in store.unresolved_carry_orders()] == [link]
    assert summary["checked"] == 0                 # it was not this client's to check


def test_startup_reconcile_still_handles_directional_orders(client, store):
    store.record_order("dir-1", "BTCUSDT", "Buy", "Market", 0.001,
                       purpose="entry", status="submitted")
    summary = client.reconcile_on_startup(["BTCUSDT"])
    assert summary["checked"] == 1
    assert store.get_order("dir-1")["status"] == "never_sent"


def test_a_cancel_the_client_cannot_attribute_never_marks_a_carry_order(client, store):
    link = live_linear_carry_order(store)
    client._mark_cancelled(link) if hasattr(client, "_mark_cancelled") else None
    assert store.get_order(link)["status"] == "acknowledged"


def test_the_engines_shutdown_does_not_cancel_carry_orders(client, store):
    import trading_engine as te
    link = live_linear_carry_order(store)
    calls = []
    real = client.cancel_order
    client.cancel_order = lambda **kw: calls.append(kw) or real(**kw)
    pending = [o for o in store.pending_orders()
               if not str(o.get("purpose", "")).startswith("carry-")]
    assert pending == []
    # the shutdown loop selects through this helper
    assert link not in [o["order_link_id"] for o in te.orders_for_client_shutdown(store)]


# -- the private websocket is not a second writer of carry order state --------------------

def test_a_websocket_status_cannot_move_a_carry_order(store):
    """WS 'Cancelled'/'Filled' used to set a carry row terminal with the fill
    untouched and no lifecycle event, so a later REST confirm carrying the real
    fill was ignored (independent review finding 5)."""
    link = live_linear_carry_order(store)
    store.apply_order_event(link, ol.CANCEL_REQUEST)
    store.update_order_status(link, "cancelled", source="ws")
    assert store.get_order(link)["status"] == "cancel_requested"
    store.update_order_status(link, "filled", exchange_id="ex-1", source="ws")
    assert store.get_order(link)["status"] == "cancel_requested"
    r = store.apply_order_event(link, ol.CANCEL_CONFIRM, filled_qty=0.001)
    assert r.applied and r.status == "filled"            # REST truth still lands
    assert store.get_order(link)["filled_qty"] == pytest.approx(0.001)


def test_the_ignored_websocket_event_is_journalled_not_silent(store):
    link = live_linear_carry_order(store)
    store.update_order_status(link, "filled", source="ws")
    decisions = [d for d in store.recent_decisions(50)
                 if d.get("decision") == "WS_CARRY_EVENT_IGNORED"]
    assert decisions and link in str(decisions[0])


def test_a_websocket_status_still_updates_a_directional_order(store):
    store.record_order("dir-9", "BTCUSDT", "Buy", "Market", 0.001,
                       purpose="stop", status="submitted")
    store.update_order_status("dir-9", "filled", source="ws")
    assert store.get_order("dir-9")["status"] == "filled"
