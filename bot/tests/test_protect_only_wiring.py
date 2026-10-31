"""Startup wiring of the protect-only restart: a held short is never abandoned
by a refusal, never adopted from an uncertain database, never traded into."""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import order_lifecycle as ol                                  # noqa: E402
import main as _main                                          # noqa: E402
import protective_restart as pr                               # noqa: E402
from test_paired_leg_recovery import FeeVenue, modern, stack, _bot_on  # noqa: E402


@pytest.fixture()
def path(tmp_path):
    return str(tmp_path / "s.db")


@pytest.fixture()
def exits_open(monkeypatch):
    """The exit gate as it is on a sandbox config: reduce-only exits permitted."""
    monkeypatch.setattr(_main, "carry_orders_permitted_for",
                        lambda cfg, client, exit=False: (True, "TEST"))


def unresolved_link(store):
    store.record_order_intent("BB-carr-4-x", "BTCUSDT", "Sell", 1.0,
                              leg="linear", cycle_id="cyc-4")
    store.apply_order_event("BB-carr-4-x", ol.SUBMIT_ATTEMPT)
    store.apply_order_event("BB-carr-4-x", ol.TIMEOUT)


def test_the_brokers_unsigned_short_is_read_as_a_short(path):
    store, venue, broker, engine = stack(path, FeeVenue(modern, perp_qty=0.5,
                                                         spot_qty=0.5))
    snap = _bot_on(store, engine)._venue_snapshot()
    assert snap.perp_qty == pytest.approx(-0.5)
    legs = pr.classify_exposure(snap)
    assert str(getattr(legs, "name", legs)).upper().endswith("OPEN")
    store.close()


def test_a_held_short_with_an_unresolved_order_continues_protect_only(path, exits_open):
    store, venue, broker, engine = stack(path, FeeVenue(modern, perp_qty=0.5,
                                                         spot_qty=0.5))
    unresolved_link(store)
    created = len(venue.created)
    bot = _bot_on(store, engine)
    assert bot.carry_cold_start() is True
    assert engine.entries_enabled is False
    assert bot.protect_only_reason
    assert len(venue.created) == created              # nothing re-sent
    assert store.get_order("BB-carr-4-x")["status"] == "reconciliation_required"
    store.close()


def test_a_protect_only_book_refuses_every_entry(path, exits_open):
    store, venue, broker, engine = stack(path, FeeVenue(modern, perp_qty=0.5,
                                                         spot_qty=0.5))
    unresolved_link(store)
    bot = _bot_on(store, engine)
    assert bot.carry_cold_start() is True
    decision = engine._open(60000.0, 60000.0, 3.0, 0)
    assert decision.reason == "ENTRIES_DISABLED"
    assert len(venue.created) == 0
    store.close()


def test_an_unreadable_venue_with_an_unresolved_order_still_refuses(path):
    class Blind(FeeVenue):
        def _request(self, method, path_, **kw):
            if "position" in path_:
                raise RuntimeError("venue down")
            return super()._request(method, path_, **kw)

    store, venue, broker, engine = stack(path, Blind(modern, perp_qty=0.5,
                                                      spot_qty=0.5))
    unresolved_link(store)
    bot = _bot_on(store, engine)
    assert bot.carry_cold_start() is False
    assert not getattr(bot, "protect_only_reason", "")
    store.close()


def test_a_held_short_whose_exit_path_is_refused_is_an_unmistakable_alert(path, caplog):
    """The exit gate refuses (no monkeypatch: the test bot has no config), so
    protection cannot be established: refuse, and say so loudly."""
    store, venue, broker, engine = stack(path, FeeVenue(modern, perp_qty=0.5,
                                                         spot_qty=0.5))
    unresolved_link(store)
    bot = _bot_on(store, engine)
    assert bot.carry_cold_start() is False
    assert "POSITION MAY BE UNMANAGED" in caplog.text
    store.close()


def test_startup_refuses_a_host_whose_clock_is_far_from_the_venues():
    """The clock gate sits in startup's sync_time block, ahead of any order."""
    import inspect
    body = inspect.getsource(_main.TradingBot.startup)
    assert body.index("sync_time()") < body.index("assert_clock_within") \
        < body.index("carry_cold_start")


def test_a_protect_only_tick_writes_nothing_to_the_refused_store(path, exits_open):
    store, venue, broker, engine = stack(path, FeeVenue(modern, perp_qty=0.5,
                                                         spot_qty=0.5))
    unresolved_link(store)
    bot = _bot_on(store, engine)
    assert bot.carry_cold_start() is True

    class Boom:
        def __getattr__(self, name):
            raise AssertionError(f"protect-only touched {name}")

    bot.session = Boom()
    bot.risk = Boom()
    bot.client = Boom()
    bot.tick()                         # must return before session/risk/client
