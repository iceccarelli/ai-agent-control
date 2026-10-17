"""0037 — a restart must not open a second hedge.

INVENTORY D3: `CarryEngine.position` lived in memory. A container that died
holding a hedged pair came back FLAT, `CarryRisk.has_open_pair` read the empty
engine and said no pair was open, and the book opened a second one against the
same margin and the same liquidation price. `reconcile_pair` existed and had
zero call sites; the directional orphan check runs only for a linear-category
client and the carry client is spot.

Two halves, and neither is optional:

  the LEDGER   what this process believed, written the moment it changed
  the VENUE    what is actually there, read before the first tick

They agree, or the book does not start. The ledger is never edited to make
them agree — that is the one thing reconciliation may not do.
"""
from __future__ import annotations

import json
import os
import sys
import time
from unittest import mock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import main as _main  # noqa: E402
from carry_engine import (ACQUIRE, OVERLAY, BookState, CarryEngine,  # noqa: E402
                          ColdStart, plan_cold_start)
from carry_risk import CarryRisk  # noqa: E402
from market_snapshot import MarketSnapshot  # noqa: E402
from persistence import StateStore  # noqa: E402

MARK = 100_000.0
H8 = 8 * 3600 * 1000


class Venue:
    def __init__(self, *, inventory=5.0, perp=0.0, open_orders=()):
        self.inventory = inventory
        self.perp = perp
        self.open_orders = list(open_orders)
        self.orders = []

    def place_market(self, *, symbol, side, qty, product):
        self.orders.append((side, product, qty))
        if product == "linear":
            # A faithful venue: its perp position moves with the fills. The
            # engine now compares it with the book on every held tick.
            self.perp += qty if side == "Sell" else -qty
        return {"filled_qty": qty, "avg_price": MARK, "order_link_id": "x"}

    def get_margin_multiple(self, symbol):
        return 5.0

    def get_mark(self, symbol):
        return MARK

    def get_lot_rules(self, symbol, product):
        return {"qty_step": 1e-6, "min_qty": 1e-6, "min_notional": 0.0}

    def get_fee_rates(self, symbol, product):
        return {"maker_bps": 2.0,
                "taker_bps": 10.0 if product == "spot" else 5.5}

    def get_spot_inventory(self, symbol):
        return self.inventory

    def get_perp_position(self, symbol):
        return self.perp

    def get_open_carry_orders(self, spot_symbol, perp_symbol):
        return list(self.open_orders)


def engine(venue, *, mode=OVERLAY, persist=None, cap=100.0):
    e = CarryEngine(broker=venue, max_notional_usd=cap, borrow_apr=0.0,
                    execution_mode=mode, persist=persist or (lambda s: None))
    e.pair_risk = CarryRisk(max_notional_usd=cap)
    e.snapshot = MarketSnapshot(perp_mark=MARK, spot_mark=MARK,
                                funding_bps=3.0, margin_multiple=5.0,
                                observed_at_s=time.time())
    for k in (1, 2):
        e.on_candle(mark=MARK, funding_bps=3.0, spot=MARK,
                    funding_print_ms=k * H8)
    return e


def opened(venue, **kw):
    e = engine(venue, **kw)
    d = e.on_candle(mark=MARK, funding_bps=3.0, spot=MARK,
                    funding_print_ms=3 * H8, timestamp_ms=3 * H8)
    assert d.action == "opened", d.reason
    return e


class TestTheLedgerIsWrittenTheMomentItChanges:
    def test_opening_persists_the_position(self):
        written = []
        e = opened(Venue(), persist=written.append)
        assert written, "the book opened and wrote nothing down"
        state = written[-1]
        assert state["book_state"] == "HEDGED"
        assert state["position"]["perp"]["filled_qty"] == pytest.approx(0.001)
        assert state["position"]["spot"]["order_link_id"] == "INVENTORY"
        assert state["execution_mode"] == OVERLAY

    def test_unwinding_persists_the_flat_book(self):
        written = []
        e = opened(Venue(), persist=written.append)
        for k in (4, 5, 6):
            e.on_candle(mark=MARK, funding_bps=-0.5, spot=MARK,
                        funding_print_ms=k * H8)
        assert written[-1]["book_state"] == "FLAT"
        assert written[-1]["position"] is None

    def test_halting_persists_the_halt(self):
        written = []
        e = opened(Venue(), persist=written.append)
        e.broker.get_margin_multiple = lambda s: (_ for _ in ()).throw(
            RuntimeError("no position row"))
        e.on_candle(mark=MARK, funding_bps=3.0, spot=MARK,
                    funding_print_ms=7 * H8)
        assert written[-1]["book_state"] == "HALTED"

    def test_a_persist_that_fails_halts_the_book(self):
        """A book that cannot record what it just did must not keep trading:
        the next restart would read a ledger that never saw the order."""
        def broken(_state):
            raise RuntimeError("disk full")
        v = Venue()
        e = engine(v, persist=broken)
        d = e.on_candle(mark=MARK, funding_bps=3.0, spot=MARK,
                        funding_print_ms=3 * H8)
        assert d.state is BookState.HALTED
        assert d.reason == "LEDGER_WRITE_FAILED"

    def test_the_engine_cannot_be_built_without_a_ledger(self):
        with pytest.raises(TypeError):
            CarryEngine(broker=Venue(), max_notional_usd=100.0, borrow_apr=0.0,
                        execution_mode=OVERLAY)

    def test_the_state_round_trips(self):
        e = opened(Venue())
        state = e.to_state()
        clone = engine(Venue(), persist=lambda s: None)
        clone.restore(state)
        assert clone.state is BookState.HEDGED
        assert clone.position.perp.filled_qty == pytest.approx(
            e.position.perp.filled_qty)
        assert clone.position.opened_ms == e.position.opened_ms
        assert clone._last_print_ms == e._last_print_ms

    def test_the_state_is_json(self):
        json.dumps(opened(Venue()).to_state())


class TestTheStoreKeepsIt:
    @pytest.fixture()
    def store(self, tmp_path):
        s = StateStore(str(tmp_path / "s.db"))
        yield s
        s.close()

    def test_an_empty_store_has_no_carry_position(self, store):
        assert store.load_carry_position() is None

    def test_it_saves_and_loads(self, store):
        state = {"book_state": "HEDGED", "position": {"perp": {"x": 1}},
                 "execution_mode": OVERLAY}
        store.save_carry_position(state)
        back = store.load_carry_position()
        assert back["book_state"] == "HEDGED"
        assert back["position"] == {"perp": {"x": 1}}
        assert back["updated_epoch"] > 0

    def test_saving_none_records_a_flat_book_not_an_absent_one(self, store):
        store.save_carry_position({"book_state": "FLAT", "position": None})
        back = store.load_carry_position()
        assert back is not None and back["position"] is None

    def test_the_latest_write_wins_and_there_is_only_one_row(self, store):
        for i in range(3):
            store.save_carry_position({"book_state": "HEDGED",
                                       "position": {"n": i}})
        assert store.load_carry_position()["position"] == {"n": 2}

    @pytest.mark.parametrize("payload", ["", "   ", "\n\t", b"", b"{}", 7,
                                         "not json", "[]"])
    def test_an_existing_row_with_an_unusable_payload_is_not_a_first_run(
            self, store, payload):
        from persistence import PersistenceError
        store.save_carry_position({"book_state": "FLAT", "position": None})
        with store._lock:
            store._conn.execute("UPDATE carry_position SET payload = ?",
                                (payload,))
            store._conn.commit()
        with pytest.raises(PersistenceError):
            store.load_carry_position()

    def test_the_process_refuses_to_start_on_a_blanked_ledger_row(self, store):
        venue = Venue()
        store.save_carry_position({"book_state": "FLAT", "position": None})
        with store._lock:
            store._conn.execute("UPDATE carry_position SET payload = ''")
            store._conn.commit()
        bot = _main.TradingBot.__new__(_main.TradingBot)
        bot.store = store
        bot.symbols = ["BTCUSDT"]
        bot.carry = CarryEngine(broker=venue, max_notional_usd=100.0,
                                borrow_apr=0.0, execution_mode=OVERLAY,
                                persist=store.save_carry_position)
        bot.carry.pair_risk = CarryRisk(max_notional_usd=100.0)
        assert bot.carry_cold_start() is False
        assert venue.orders == []


class TestTheColdStartPlan:
    LEDGER = {"book_state": "HEDGED", "execution_mode": OVERLAY,
              "position": {
                  "perp": {"symbol": "BTCUSDT", "side": "Sell",
                           "product": "linear", "requested_qty": 0.001,
                           "filled_qty": 0.001, "avg_price": MARK},
                  "spot": {"symbol": "BTCUSDT", "side": "Hold",
                           "product": "spot", "requested_qty": 0.001,
                           "filled_qty": 0.001, "avg_price": MARK}}}

    def plan(self, **kw):
        base = dict(ledger=None, venue_perp_qty=0.0, venue_spot_qty=5.0,
                    open_orders=())
        base.update(kw)
        return plan_cold_start(**base)

    def test_nothing_anywhere_is_a_clean_start(self):
        assert self.plan().action is ColdStart.CLEAN

    def test_a_venue_short_the_ledger_never_saw_halts(self):
        d = self.plan(venue_perp_qty=0.004)
        assert d.action is ColdStart.HALT
        assert "perp" in d.naked_side
        assert "0.004" in d.reason

    def test_a_matching_pair_resumes(self):
        d = self.plan(ledger=self.LEDGER, venue_perp_qty=0.001)
        assert d.action is ColdStart.RESUME

    def test_a_ledger_pair_the_venue_does_not_have_halts(self):
        d = self.plan(ledger=self.LEDGER, venue_perp_qty=0.0)
        assert d.action is ColdStart.HALT
        assert d.naked_side == "spot"

    def test_a_size_mismatch_halts(self):
        d = self.plan(ledger=self.LEDGER, venue_perp_qty=0.002)
        assert d.action is ColdStart.HALT

    def test_dust_is_not_a_mismatch(self):
        d = self.plan(ledger=self.LEDGER, venue_perp_qty=0.001 + 1e-9)
        assert d.action is ColdStart.RESUME

    def test_an_overlay_whose_btc_left_halts(self):
        d = self.plan(ledger=self.LEDGER, venue_perp_qty=0.001,
                      venue_spot_qty=0.0)
        assert d.action is ColdStart.HALT
        assert d.naked_side == "perp"

    def test_an_open_order_halts_whatever_else_agrees(self):
        d = self.plan(ledger=self.LEDGER, venue_perp_qty=0.001,
                      open_orders=({"orderLinkId": "BB-carr-9"},))
        assert d.action is ColdStart.HALT
        assert "order" in d.reason.lower()

    def test_a_halted_ledger_stays_halted(self):
        d = self.plan(ledger={"book_state": "HALTED", "position": None},
                      venue_perp_qty=0.0)
        assert d.action is ColdStart.HALT
        assert "halt" in d.reason.lower()

    def test_an_acquire_book_checks_its_spot_leg_too(self):
        ledger = dict(self.LEDGER, execution_mode=ACQUIRE)
        assert self.plan(ledger=ledger, venue_perp_qty=0.001,
                         venue_spot_qty=0.001).action is ColdStart.RESUME
        assert self.plan(ledger=ledger, venue_perp_qty=0.001,
                         venue_spot_qty=0.0).action is ColdStart.HALT

    def test_it_never_edits_the_ledger(self):
        ledger = json.loads(json.dumps(self.LEDGER))
        self.plan(ledger=ledger, venue_perp_qty=0.002)
        assert ledger == self.LEDGER


class TestTheEngineResumesOrRefuses:
    def test_a_resumed_book_holds_the_same_pair(self):
        v = Venue()
        e = opened(v, persist=lambda s: None)
        state = e.to_state()
        fresh = engine(Venue(perp=0.001), persist=lambda s: None)
        d = fresh.cold_start(ledger=state, venue_perp_qty=0.001,
                             venue_spot_qty=5.0, open_orders=())
        assert d.action is ColdStart.RESUME
        assert fresh.state is BookState.HEDGED
        assert fresh.position.perp.filled_qty == pytest.approx(0.001)

    def test_a_resumed_book_does_not_open_a_second_hedge(self):
        v = Venue(perp=0.001)
        e = engine(v, persist=lambda s: None)
        e.cold_start(ledger=opened(Venue()).to_state(), venue_perp_qty=0.001,
                     venue_spot_qty=5.0, open_orders=())
        v.orders.clear()
        d = e.on_candle(mark=MARK, funding_bps=3.0, spot=MARK,
                        funding_print_ms=9 * H8)
        assert v.orders == []
        assert d.action == "hold"

    def test_a_mismatch_halts_the_engine_and_trips_the_switch(self):
        tripped = []
        v = Venue(perp=0.004)
        e = CarryEngine(broker=v, max_notional_usd=100.0, borrow_apr=0.0,
                        execution_mode=OVERLAY, persist=lambda s: None,
                        kill_switch=tripped.append)
        d = e.cold_start(ledger=None, venue_perp_qty=0.004,
                         venue_spot_qty=5.0, open_orders=())
        assert d.action is ColdStart.HALT
        assert e.state is BookState.HALTED
        assert tripped

    def test_a_halted_engine_places_nothing_afterwards(self):
        v = Venue(perp=0.004)
        e = engine(v, persist=lambda s: None)
        e.cold_start(ledger=None, venue_perp_qty=0.004, venue_spot_qty=5.0,
                     open_orders=())
        v.orders.clear()
        e.on_candle(mark=MARK, funding_bps=3.0, spot=MARK,
                    funding_print_ms=9 * H8)
        assert v.orders == []


class TestTheProcessRefusesToStartOnAMismatch:
    def _bot(self, venue, ledger):
        bot = _main.TradingBot.__new__(_main.TradingBot)
        store = mock.Mock()
        store.load_carry_position.return_value = ledger
        store.is_kill_switch_engaged.return_value = (False, "")
        bot.store = store
        bot.symbols = ["BTCUSDT"]
        bot.carry = CarryEngine(broker=venue, max_notional_usd=100.0,
                                borrow_apr=0.0, execution_mode=OVERLAY,
                                persist=lambda s: None,
                                kill_switch=store.trip_kill_switch)
        bot.carry.pair_risk = CarryRisk(max_notional_usd=100.0)
        return bot, store

    def test_a_clean_venue_starts(self):
        bot, _store = self._bot(Venue(), None)
        assert bot.carry_cold_start() is True

    def test_an_unknown_venue_position_refuses_to_start(self):
        bot, store = self._bot(Venue(perp=0.004), None)
        assert bot.carry_cold_start() is False
        assert store.trip_kill_switch.called

    def test_an_unreadable_venue_refuses_to_start(self):
        v = Venue()
        v.get_perp_position = lambda s: (_ for _ in ()).throw(
            RuntimeError("venue down"))
        bot, _store = self._bot(v, None)
        assert bot.carry_cold_start() is False

    def test_startup_calls_it_before_it_reconciles_anything_else(self):
        """A cold start that has to be remembered is not an invariant."""
        import inspect
        body = inspect.getsource(_main.TradingBot.startup)
        assert "carry_cold_start" in body
        assert body.index("carry_cold_start") < body.index("self.engine.reconcile")


class TestAPersistedPositionThatCannotBeTrustedHaltsTheStart:
    """NaN "agrees" with the venue (every comparison is False), abs() hides a
    negative short, True is 1.0 and text used to raise out of startup. None of
    them may resume as a hedge; all of them take the existing HALT path."""

    @staticmethod
    def _ledger(venue, **leg_edits):
        led = opened(venue).to_state()
        for path, value in leg_edits.items():
            role, key = path.split("__")
            if role == "pos":
                led["position"][key] = value
            else:
                led["position"][role][key] = value
        return led

    CASES = {
        "perp_nan": {"perp__filled_qty": float("nan")},
        "perp_inf": {"perp__filled_qty": float("inf")},
        "perp_negative": {"perp__filled_qty": -0.001},
        "perp_bool": {"perp__filled_qty": True},
        "perp_text": {"perp__filled_qty": "abc"},
        "perp_numeric_text": {"perp__filled_qty": "0.001"},
        "spot_nan": {"spot__filled_qty": float("nan")},
        "price_nan": {"perp__avg_price": float("nan")},
        "price_negative": {"perp__avg_price": -1.0},
        "price_missing_for_a_held_qty": {"perp__avg_price": 0.0},
        "fee_nan": {"perp__fee": float("nan")},
        "fee_base_negative": {"spot__fee_base_qty": -1.0},
        "wrong_side": {"perp__side": "Buy"},
        "wrong_product": {"perp__product": "spot"},
        "unknown_field": {"perp__bogus": 1},
        "opened_text": {"pos__opened_ms": "x"},
        "funding_nan": {"pos__funding_collected": float("nan")},
        "streak_negative": {"pos__negative_funding_streak": -1},
        "leg_missing": {"pos__perp": None},
        "position_not_a_pair": {"pos__spot": [1]},
    }

    @pytest.mark.parametrize("name", sorted(CASES))
    def test_it_halts_instead_of_resuming_or_raising(self, name):
        venue = Venue(perp=0.001)
        ledger = self._ledger(venue, **self.CASES[name])
        fresh = Venue(perp=0.001)
        e = engine(fresh, persist=lambda s: None)
        d = e.cold_start(ledger=ledger, venue_perp_qty=0.001,
                         venue_spot_qty=5.0, open_orders=())
        assert d.action is ColdStart.HALT, d.reason
        assert e.position is None and e.state is BookState.HALTED
        assert fresh.orders == []

    def test_a_non_dict_position_is_not_read_as_flat(self):
        for bad in ([], [1], "x", 0, False):
            d = plan_cold_start(ledger={"book_state": "HEDGED",
                                        "position": bad},
                                venue_perp_qty=0.0, venue_spot_qty=5.0)
            assert d.action is ColdStart.HALT, bad

    @pytest.mark.parametrize("bad", [float("nan"), float("inf"), None, "0.001",
                                     True])
    def test_a_venue_read_that_is_not_a_number_halts(self, bad):
        d = plan_cold_start(ledger=TestTheColdStartPlan.LEDGER,
                            venue_perp_qty=bad, venue_spot_qty=5.0)
        assert d.action is ColdStart.HALT
        d = plan_cold_start(ledger=TestTheColdStartPlan.LEDGER,
                            venue_perp_qty=0.001, venue_spot_qty=bad)
        assert d.action is ColdStart.HALT

    def test_the_process_refuses_to_start_and_leaves_the_evidence_alone(
            self, tmp_path):
        store = StateStore(str(tmp_path / "s.db"))
        venue = Venue(perp=0.001)
        led = self._ledger(venue, perp__filled_qty=float("nan"))
        store.save_carry_position(led)
        before = store._query("SELECT payload FROM carry_position")[0]["payload"]
        fresh = Venue(perp=0.001)
        e = engine(fresh, persist=store.save_carry_position)
        bot = _main.TradingBot.__new__(_main.TradingBot)
        bot.store, bot.symbols, bot.carry = store, ["BTCUSDT"], e
        assert bot.carry_cold_start() is False
        after = store._query("SELECT payload FROM carry_position")[0]["payload"]
        assert after == before, "the corrupt evidence was overwritten"
        assert fresh.orders == []
        store.close()

    def test_a_valid_pair_still_resumes_with_a_partial_fill_and_a_fee(self):
        venue = Venue(perp=0.001)
        led = self._ledger(venue, perp__filled_qty=0.001, spot__fee=None,
                           perp__fee=0.0055, spot__fee_base_qty=0.0)
        led["position"]["perp"]["requested_qty"] = 0.002      # partial
        fresh = Venue(perp=0.001)
        e = engine(fresh, persist=lambda s: None)
        d = e.cold_start(ledger=led, venue_perp_qty=0.001,
                         venue_spot_qty=5.0, open_orders=())
        assert d.action is ColdStart.RESUME
        assert e.position.perp.fee == pytest.approx(0.0055)
