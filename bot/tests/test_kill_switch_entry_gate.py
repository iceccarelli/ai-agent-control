"""The kill switch is enforced where orders are built, for entries only.

Before: it was checked in the engine's pre-entry gate and the carry tick, so any
other caller of the broker's order methods went around it. Now the broker takes
an entry gate that is asked before every ENTRY-side order (spot BUY, perp SELL)
and never for an exit (perp reduce-only BUY, spot SELL), so a halted or
switched-off book can always flatten. An unreadable gate refuses entries too.
Real CarryBroker and StateStore, Bybit-shaped fake client. Offline.
"""
from __future__ import annotations

import os
import sys
from unittest import mock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import main as _main                                   # noqa: E402
from carry_broker import CarryBroker, CarryOrderRefused  # noqa: E402
from persistence import StateStore                     # noqa: E402
from test_carry_broker import FakeClient               # noqa: E402


def broker(entry_gate="unset", client=None):
    client = client or FakeClient(mark=100_000.0)
    kw = {} if entry_gate == "unset" else {"entry_gate": entry_gate}
    b = CarryBroker(client=client, order_gate=lambda: (True, "TEST"),
                    sequence_source=lambda *a: 1, **kw)
    return b, client


def orders(client):
    return [c for c in getattr(client, "created", [])]


BLOCK = lambda: (False, "KILL_SWITCH_ENGAGED: test")   # noqa: E731


@pytest.mark.parametrize("product,side", [("spot", "Buy"), ("linear", "Sell")])
def test_entry_orders_are_refused_while_the_gate_is_closed(product, side):
    b, client = broker(BLOCK)
    with pytest.raises(CarryOrderRefused, match="KILL_SWITCH_ENGAGED"):
        b.place_market(symbol="BTCUSDT", side=side, qty=0.001, product=product)
    assert orders(client) == [] and b.sent_order_links == []


@pytest.mark.parametrize("product,side", [("spot", "Sell"), ("linear", "Buy")])
def test_exit_orders_still_go_while_the_gate_is_closed(product, side):
    asked = []
    b, client = broker(lambda: asked.append(1) or (False, "closed"))
    b.place_market(symbol="BTCUSDT", side=side, qty=0.001, product=product)
    assert asked == []                       # an exit never consults the gate
    assert len(orders(client)) == 1


def test_a_resting_entry_is_gated_too():
    b, client = broker(BLOCK)
    with pytest.raises(CarryOrderRefused):
        b.place_post_only(symbol="BTCUSDT", side="Sell", qty=0.001,
                          price=100_000.0, product="linear")
    assert orders(client) == []


@pytest.mark.parametrize("answer", [None, True, (None, "x"), ("yes", "x"), 1])
def test_anything_but_an_explicit_true_refuses_an_entry(answer):
    b, client = broker(lambda: answer)
    with pytest.raises(CarryOrderRefused):
        b.place_market(symbol="BTCUSDT", side="Sell", qty=0.001, product="linear")
    assert orders(client) == []


def test_an_unreadable_gate_refuses_an_entry():
    def boom():
        raise RuntimeError("db locked")
    b, client = broker(boom)
    with pytest.raises(CarryOrderRefused, match="unreadable"):
        b.place_market(symbol="BTCUSDT", side="Sell", qty=0.001, product="linear")
    assert orders(client) == []


def test_an_open_gate_lets_the_entry_through():
    b, client = broker(lambda: (True, "ok"))
    b.place_market(symbol="BTCUSDT", side="Sell", qty=0.001, product="linear")
    assert len(orders(client)) == 1


def test_no_entry_gate_keeps_the_old_behaviour():
    b, client = broker()
    b.place_market(symbol="BTCUSDT", side="Sell", qty=0.001, product="linear")
    assert len(orders(client)) == 1


# -- the production wiring: the real store's switch drives the gate ------------

def test_the_production_entry_gate_follows_the_stores_kill_switch(tmp_path):
    store = StateStore(str(tmp_path / "s.db"))
    try:
        gate = _main.carry_entries_permitted_from(store)
        assert gate() == (True, "kill switch clear")
        store.trip_kill_switch("test trip")
        allowed, why = gate()
        assert allowed is False and "KILL_SWITCH_ENGAGED" in why
    finally:
        store.close()


def test_the_production_entry_gate_fails_closed_on_an_unreadable_switch():
    store = mock.Mock()
    store.is_kill_switch_engaged.side_effect = RuntimeError("corrupt")
    allowed, why = _main.carry_entries_permitted_from(store)()
    assert allowed is False and "UNREADABLE" in why


def test_build_bot_gives_the_carry_broker_the_entry_gate():
    import config as _config
    from test_carry_wiring_authority import _Store
    s = _Store()
    s.is_kill_switch_engaged = lambda: (True, "halted by test")
    cfg = _config.load({"BOOK_MODE": "carry", "CARRY_BORROW_APR": "0.0",
                        "CARRY_EXECUTION_MODE": "overlay"})
    bot = _main.build_bot(config=cfg, store=s, client=mock.Mock(),
                          risk_manager=mock.Mock(), engine=mock.Mock())
    inner = bot.carry.broker.inner
    assert inner.entry_gate is not None
    assert inner.entry_gate()[0] is False
