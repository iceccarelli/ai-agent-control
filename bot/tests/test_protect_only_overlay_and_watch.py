"""Protect-only, overlay mode and the unmanaged-position alert.

* In OVERLAY the spot side is the CLIENT's BTC (the whole wallet balance). It is
  not the book's exposure and a sale of it is never an exit leg. A flat perp
  with a client wallet is FLAT, so an unresolved order there still refuses the
  start and trips the kill switch instead of "continuing protect-only".
* A protect-only process sends no order, so the state must be stated plainly as
  UNMANAGED, and the short's margin is still READ (no order) and shouted about.
"""
import logging
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import order_lifecycle as ol                                  # noqa: E402
import protective_restart as pr                               # noqa: E402
from protective_restart import (CANNOT_PROTECT, PROTECT_ONLY, Exposure,   # noqa: E402
                                OrderFact, VenueSnapshot, assess_restart)
import main as _main                                          # noqa: E402
from test_paired_leg_recovery import FeeVenue, modern, stack, _bot_on  # noqa: E402


def run(venue, **kw):
    base = dict(environment_conflict=None, store_readable=True, orders=[],
                venue=venue, kill_switch_engaged=False, entries_enabled=True)
    base.update(kw)
    return assess_restart(**base)


def test_overlay_never_lists_the_clients_btc_as_an_exit_leg():
    v = run(VenueSnapshot(perp_qty=-0.5, spot_qty=2.0),
            orders=[OrderFact("A", "unknown")], spot_is_client_inventory=True)
    assert v.mode == PROTECT_ONLY
    assert [(l.leg, l.side, l.qty) for l in v.exit_legs] == [("perp", "Buy", 0.5)]
    assert v.spot_qty == 2.0, "still reported, as information"


def test_overlay_flat_perp_with_a_client_wallet_is_flat_not_open():
    v = run(VenueSnapshot(perp_qty=0.0, spot_qty=2.0),
            orders=[OrderFact("A", "unknown")], spot_is_client_inventory=True)
    assert v.exposure is Exposure.FLAT and not v.exit_legs


def test_overlay_an_unreadable_wallet_does_not_make_the_perp_unverified():
    v = run(VenueSnapshot(perp_qty=-0.5, spot_qty=None),
            spot_is_client_inventory=True)
    assert v.position_verified and not v.position_unknown


def test_overlay_an_unreadable_perp_is_still_unknown():
    v = run(VenueSnapshot(perp_qty=None, spot_qty=2.0),
            spot_is_client_inventory=True)
    assert v.exposure is Exposure.UNKNOWN and v.mode == CANNOT_PROTECT


def test_acquire_default_is_unchanged():
    v = run(VenueSnapshot(perp_qty=-0.5, spot_qty=0.5),
            environment_conflict="x")
    assert {l.leg for l in v.exit_legs} == {"perp", "spot"}


@pytest.fixture()
def path(tmp_path):
    return str(tmp_path / "s.db")


@pytest.fixture()
def exits_open(monkeypatch):
    monkeypatch.setattr(_main, "carry_orders_permitted_for",
                        lambda cfg, client, exit=False: (True, "TEST"))


def _unresolved(store):
    store.record_order_intent("BB-carr-4-x", "BTCUSDT", "Sell", 1.0,
                              leg="linear", cycle_id="cyc-4")
    store.apply_order_event("BB-carr-4-x", ol.SUBMIT_ATTEMPT)
    store.apply_order_event("BB-carr-4-x", ol.TIMEOUT)


def test_overlay_flat_perp_and_client_btc_with_an_unresolved_order_trips_the_switch(
        path, exits_open):
    store, venue, broker, engine = stack(path, FeeVenue(modern, perp_qty=0.0,
                                                         spot_qty=2.0))
    engine.execution_mode = "overlay"
    _unresolved(store)
    bot = _bot_on(store, engine)
    assert bot.carry_cold_start() is False
    assert store.is_kill_switch_engaged()[0] is True
    assert not getattr(bot, "protect_only_reason", "")
    store.close()


def test_the_alert_says_the_position_is_unmanaged():
    v = run(VenueSnapshot(perp_qty=-0.5, spot_qty=0.5),
            orders=[OrderFact("A", "unknown")])
    assert "UNMANAGED" in v.alert.message
    assert "no exchange-side stop" in v.alert.message
    assert "NO orders" in v.alert.message or "no orders" in v.alert.message.lower()


def test_a_protect_only_tick_reads_the_margin_and_shouts_when_it_is_thin(
        path, exits_open, caplog):
    store, venue, broker, engine = stack(path, FeeVenue(modern, perp_qty=0.5,
                                                         spot_qty=0.5))
    _unresolved(store)
    bot = _bot_on(store, engine)
    assert bot.carry_cold_start() is True
    created = len(venue.created)
    engine.broker.get_margin_multiple = lambda symbol: 1.01     # below the floor
    caplog.clear()
    with caplog.at_level(logging.CRITICAL):
        bot.tick()
    assert "UNMANAGED" in caplog.text
    assert "margin" in caplog.text.lower() and "1.01" in caplog.text
    assert len(venue.created) == created, "a protect-only tick sends nothing"
    store.close()


def test_a_protect_only_tick_with_an_unreadable_margin_says_so(
        path, exits_open, caplog):
    store, venue, broker, engine = stack(path, FeeVenue(modern, perp_qty=0.5,
                                                         spot_qty=0.5))
    _unresolved(store)
    bot = _bot_on(store, engine)
    assert bot.carry_cold_start() is True

    def boom(symbol):
        raise RuntimeError("venue down")
    engine.broker.get_margin_multiple = boom
    caplog.clear()
    with caplog.at_level(logging.CRITICAL):
        bot.tick()
    assert "UNMANAGED" in caplog.text and "UNREADABLE" in caplog.text
    store.close()
