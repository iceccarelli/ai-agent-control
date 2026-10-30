"""No exposure-increasing order leaves BybitClient while the entry gate is shut.

The carry broker already refuses entries under a kill switch. The directional
book and every tool reach the venue through `BybitClient.place_order`, which
had no gate of its own. It now takes an `entry_gate` (wired by `build_bot` to
the store's kill switch) asked before any order that can add exposure: a spot
BUY, a linear order that is not reduce-only. A spot SELL of held coin and any
reduce-only order are exits and are never asked, so a switched-off book can
always flatten. Anything but an explicit (True, reason) refuses. Offline, on
the repo's strict fake exchange.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import bybit_connection as bc  # noqa: E402
from fake_bybit import FakeBybit  # noqa: E402
from persistence import StateStore  # noqa: E402
from test_bybit_connection import Cfg  # noqa: E402


@pytest.fixture()
def exchange():
    return FakeBybit()


@pytest.fixture()
def store(tmp_path):
    s = StateStore(str(tmp_path / "s.db"))
    yield s
    s.close()


@pytest.fixture()
def client(exchange, store):
    return bc.BybitClient(config=Cfg(), store=store, transport=exchange)


SHUT = lambda: (False, "KILL_SWITCH_ENGAGED: test")   # noqa: E731


def test_a_spot_buy_is_refused_while_the_gate_is_shut(client, exchange):
    client.entry_gate = SHUT
    r = client.place_order(symbol="BTCUSDT", side="Buy", qty=0.001)
    assert r.ok is False and r.reason.startswith("ENTRY_GATE_REFUSED")
    assert "KILL_SWITCH_ENGAGED" in r.reason
    assert exchange.orders == {}
    assert client.orders_submitted == 0


def test_a_spot_sell_of_held_coin_is_an_exit_and_still_goes(client, exchange):
    exchange.balances["BTC"] = 1.0
    asked = []
    client.entry_gate = lambda: asked.append(1) or (False, "shut")
    r = client.place_order(symbol="BTCUSDT", side="Sell", qty=0.001)
    assert r.ok is True, r.reason
    assert asked == []


@pytest.mark.parametrize("side", ["Buy", "Sell"])
def test_a_reduce_only_order_is_an_exit_and_is_never_asked(client, exchange, side):
    exchange.balances["BTC"] = 1.0
    asked = []
    client.entry_gate = lambda: asked.append(1) or (False, "shut")
    client.place_order(symbol="BTCUSDT", side=side, qty=0.001, reduce_only=True,
                       purpose="close")
    assert asked == []


@pytest.mark.parametrize("answer", [None, True, (None, "x"), ("yes", "x"), 1, ()])
def test_anything_but_an_explicit_true_refuses(client, exchange, answer):
    client.entry_gate = lambda: answer
    r = client.place_order(symbol="BTCUSDT", side="Buy", qty=0.001)
    assert r.ok is False and r.reason.startswith("ENTRY_GATE_REFUSED")
    assert exchange.orders == {}


def test_an_unreadable_gate_refuses_an_entry(client, exchange):
    def boom():
        raise RuntimeError("db locked")
    client.entry_gate = boom
    r = client.place_order(symbol="BTCUSDT", side="Buy", qty=0.001)
    assert r.ok is False and "unreadable" in r.reason
    assert exchange.orders == {}


def test_an_open_gate_lets_the_entry_through(client, exchange):
    client.entry_gate = lambda: (True, "kill switch clear")
    assert client.place_order(symbol="BTCUSDT", side="Buy", qty=0.001).ok is True


def test_a_client_without_a_gate_behaves_as_before(client, exchange):
    assert client.entry_gate is None
    assert client.place_order(symbol="BTCUSDT", side="Buy", qty=0.001).ok is True


def test_a_gate_that_is_not_callable_refuses_every_entry(client, exchange):
    client.entry_gate = "yes"
    r = client.place_order(symbol="BTCUSDT", side="Buy", qty=0.001)
    assert r.ok is False and r.reason.startswith("ENTRY_GATE_REFUSED")
    assert exchange.orders == {}


def test_the_refusal_happens_before_any_network_or_ledger_effect(client, exchange, store):
    client.entry_gate = SHUT
    before = len(exchange.requests)
    client.place_order(symbol="BTCUSDT", side="Buy", qty=0.001)
    assert len(exchange.requests) == before        # no filters fetch, no order
    assert client.store.load_open_orders() == [] if hasattr(
        client.store, "load_open_orders") else True


def test_build_bot_wires_the_stores_kill_switch_to_the_client():
    from unittest import mock
    import config as _config
    import main as _main
    from test_carry_wiring_authority import _Store
    s = _Store()
    s.is_kill_switch_engaged = lambda: (True, "halted by test")
    cfg = _config.load({"BOOK_MODE": "carry", "CARRY_BORROW_APR": "0.0",
                        "CARRY_EXECUTION_MODE": "overlay"})
    client = mock.Mock()
    bot = _main.build_bot(config=cfg, store=s, client=client,
                          risk_manager=mock.Mock(), engine=mock.Mock())
    gate = bot.client.entry_gate
    assert callable(gate) and gate()[0] is False
