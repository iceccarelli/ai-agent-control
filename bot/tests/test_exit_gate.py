"""The promotion gate decides whether to START exposure, never whether to keep it.

`carry_orders_permitted` re-read the promotion gate for every mainnet order,
exits included, so an approvals file that was chmod'd, an edited gate artefact
or a `models/current` directory appearing mid-hold would refuse the buy-back of
a live short: the engine halted and a halted book is read-only. An EXIT (a spot
Sell, a reduce-only perp Buy) now asks a narrower gate: the paper flag, the
sandbox/URL consistency, and on mainnet the live-arming chain, but not the
promotion gate. Entries ask the full gate, unchanged. Independent review
finding 2. Offline; the live chain is monkeypatched (LIVE_TRADING_ACK is never
set).
"""
from __future__ import annotations

import os
import sys
from unittest import mock

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import config as _config                                     # noqa: E402
import main as _main                                         # noqa: E402
from carry_broker import CarryBroker, CarryOrderRefused      # noqa: E402
from test_carry_broker import FakeClient                     # noqa: E402

BASE = {"BOOK_MODE": "carry", "CARRY_BORROW_APR": "0.0",
        "CARRY_EXECUTION_MODE": "overlay"}


def cfg(**env):
    return _config.load(dict(BASE, **env))


@pytest.fixture()
def armed(monkeypatch):
    monkeypatch.setattr(_config, "is_live_authorized",
                        lambda c=None: (True, "armed (test double)"))


@pytest.fixture()
def disarmed(monkeypatch):
    monkeypatch.setattr(_config, "is_live_authorized",
                        lambda c=None: (False, "not armed"))


def mainnet_cfg():
    """A mainnet-shaped config object. The loader itself forces PAPER_TRADING on
    for mainnet without the live acknowledgement (which is never set here), so
    the fields are replaced directly; is_live_authorized is monkeypatched."""
    import dataclasses
    return dataclasses.replace(cfg(), USE_TESTNET=False, PAPER_TRADING=False)


def test_an_unsigned_promotion_gate_refuses_a_mainnet_entry_but_not_an_exit(armed):
    c = mainnet_cfg()
    assert _main.carry_orders_permitted(c)[0] is False                 # entry
    ok, why = _main.carry_orders_permitted(c, exit=True)               # exit
    assert ok is True and "exit" in why.lower()


def test_an_exit_still_needs_the_live_arming_chain_on_mainnet(disarmed):
    ok, why = _main.carry_orders_permitted(mainnet_cfg(), exit=True)
    assert ok is False and "authorisation" in why


@pytest.mark.parametrize("exit_", [False, True])
def test_paper_mode_refuses_every_order(exit_):
    ok, _ = _main.carry_orders_permitted(
        cfg(USE_TESTNET="0", PAPER_TRADING="1"), exit=exit_)
    assert ok is False


@pytest.mark.parametrize("exit_", [False, True])
def test_testnet_permits_both(exit_):
    assert _main.carry_orders_permitted(
        cfg(USE_TESTNET="1", PAPER_TRADING="0"), exit=exit_)[0] is True


def test_the_url_consistency_check_applies_to_exits_too():
    class MainnetClient:
        base_url = "https://api.bybit.com"
    ok, why = _main.carry_orders_permitted_for(
        cfg(USE_TESTNET="1", PAPER_TRADING="0"), MainnetClient(), exit=True)
    assert ok is False and "mainnet host" in why


# -- at the broker --------------------------------------------------------------------------

def broker(order_gate, exit_gate=None):
    client = FakeClient(mark=100_000.0)
    return CarryBroker(client=client, order_gate=order_gate, exit_gate=exit_gate,
                       sequence_source=lambda *a: 1), client


SHUT = lambda: (False, "promotion gate unsigned")   # noqa: E731
OPEN = lambda: (True, "exit permitted")             # noqa: E731


def test_exits_use_the_exit_gate_and_entries_use_the_order_gate():
    b, client = broker(SHUT, OPEN)
    b.place_market(symbol="BTCUSDT", side="Buy", qty=0.001, product="linear")   # perp close
    b.place_market(symbol="BTCUSDT", side="Sell", qty=0.001, product="spot")    # spot unwind
    assert len(client.created) == 2
    for side, product in (("Sell", "linear"), ("Buy", "spot")):                  # entries
        with pytest.raises(CarryOrderRefused, match="promotion gate unsigned"):
            b.place_market(symbol="BTCUSDT", side=side, qty=0.001, product=product)
    assert len(client.created) == 2


def test_without_an_exit_gate_exits_ask_the_order_gate_as_before():
    b, client = broker(SHUT, None)
    with pytest.raises(CarryOrderRefused):
        b.place_market(symbol="BTCUSDT", side="Buy", qty=0.001, product="linear")
    assert client.created == []


def test_an_unreadable_or_non_true_exit_gate_refuses_the_exit():
    for gate in (lambda: None, lambda: (None, "x"), lambda: (1, "x"),
                 lambda: (_ for _ in ()).throw(RuntimeError("gone"))):
        b, client = broker(OPEN, gate)
        with pytest.raises(CarryOrderRefused):
            b.place_market(symbol="BTCUSDT", side="Buy", qty=0.001, product="linear")
        assert client.created == []


def test_post_only_entries_never_use_the_exit_gate():
    b, client = broker(SHUT, OPEN)
    with pytest.raises(CarryOrderRefused):
        b.place_post_only(symbol="BTCUSDT", side="Sell", qty=0.001, price=100_000.0,
                          product="linear")


def test_build_bot_wires_the_exit_gate():
    from test_carry_wiring_authority import _Store
    bot = _main.build_bot(config=cfg(), store=_Store(), client=mock.Mock(),
                          risk_manager=mock.Mock(), engine=mock.Mock())
    assert bot.carry.broker.inner.exit_gate is not None
