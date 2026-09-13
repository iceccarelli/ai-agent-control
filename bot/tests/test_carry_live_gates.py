"""The snapshot and the pair gate, in the live cycle.

0022 built both and wired neither. A gate nothing consults is documentation.
"""
from __future__ import annotations

import datetime as dt
import os
import sys
import time
from unittest import mock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import main as _main  # noqa: E402
from carry_engine import BookState, CarryEngine  # noqa: E402
from carry_risk import CarryRisk  # noqa: E402
from market_snapshot import MarketSnapshot  # noqa: E402


class Store:
    def __init__(self, engaged=False):
        self.engaged = engaged
        self.seq = 0

    def is_kill_switch_engaged(self):
        return (self.engaged, "test")

    def next_order_seq(self):
        self.seq += 1
        return self.seq

    def trip_kill_switch(self, reason):
        pass

    def open_positions(self):
        return []


class Broker:
    """Counts reads so a test can prove the cycle takes ONE view."""

    def __init__(self, *, mark=100_000.0, funding=3.0, margin=5.0, spot=None):
        self.mark, self.funding, self.margin = mark, funding, margin
        self.spot = mark if spot is None else spot
        self.orders = []
        self.reads = {"mark": 0, "spot": 0, "funding": 0, "margin": 0,
                      "print": 0}

    def get_mark(self, s):
        self.reads["mark"] += 1
        return self.mark

    def get_spot_mark(self, s):
        self.reads["spot"] += 1
        return self.spot

    def get_funding_bps(self, s):
        self.reads["funding"] += 1
        return self.funding

    def get_margin_multiple(self, s):
        self.reads["margin"] += 1
        return self.margin

    # 0036: the venue's own lot rules and fee tier. A 1e-6 step leaves every
    # size in this file unchanged; the fee table is what the round trip is
    # computed from instead of a constant.
    def get_lot_rules(self, symbol, product):
        return {"qty_step": 1e-6, "min_qty": 1e-6, "min_notional": 0.0}

    def get_fee_rates(self, symbol, product):
        return {"maker_bps": 2.0,
                "taker_bps": 10.0 if product == "spot" else 5.5}

    def get_funding_print(self, s):
        # 0034: the latest settled print, stamped at the last settlement.
        self.reads["print"] += 1
        return (self.funding, latest_settlement_ms())

    def place_market(self, *, symbol, side, qty, product):
        self.orders.append((side, product))
        return {"filled_qty": qty, "avg_price": self.mark, "order_link_id": "x"}


H8 = 8 * 3600 * 1000


def latest_settlement_ms():
    return int(time.time() * 1000) // H8 * H8


def bot_with(broker, *, store=None, warm=3.0, risk=True):
    store = store or Store()
    bot = _main.TradingBot.__new__(_main.TradingBot)
    bot.symbols = ["BTCUSDT"]
    bot.store = store
    bot.strategy = None
    bot._stop = mock.Mock(is_set=lambda: False)
    bot.session = mock.Mock()
    bot.client = mock.Mock(get_equity=lambda: 1_000_000.0)
    bot.risk = mock.Mock(should_halt_trading=lambda: False,
                         update_equity=lambda e: None)
    bot.engine = mock.Mock(observe_exits=lambda: {})
    bot.carry = CarryEngine(broker=broker, max_notional_usd=100.0,
                            borrow_apr=0.05, execution_mode="acquire")
    if risk:
        bot.carry.pair_risk = CarryRisk(store=store, max_notional_usd=100.0)
    # 0034: two earlier SETTLED prints; the tick then reads the latest one.
    for k in (2, 1):
        bot.carry.on_candle(mark=broker.mark, funding_bps=warm,
                            spot=broker.spot,
                            funding_print_ms=latest_settlement_ms() - k * H8)
    return bot


class TestTheCycleTakesOneView:
    def test_a_tick_reads_each_field_once(self):
        """Three loose reads measured the basis plus the drift between calls."""
        broker = Broker()
        bot = bot_with(broker)
        for key in broker.reads:
            broker.reads[key] = 0
        bot.tick()
        assert broker.reads["mark"] == 1
        assert broker.reads["spot"] == 1
        assert broker.reads["funding"] == 1

    def test_the_snapshot_is_kept_for_the_gate(self):
        broker = Broker()
        bot = bot_with(broker)
        bot.tick()
        assert isinstance(bot.carry.snapshot, MarketSnapshot)

    def test_the_pair_and_the_gate_judge_the_same_observation(self):
        broker = Broker()
        bot = bot_with(broker)
        bot.tick()
        assert bot.carry.snapshot.perp_mark == broker.mark


class TestAnUnusableViewTouchesNothing:
    def test_a_failed_read_sends_no_order(self):
        broker = Broker()
        bot = bot_with(broker)
        broker.get_spot_mark = mock.Mock(side_effect=RuntimeError("no ticker"))
        broker.orders.clear()
        bot.tick()
        assert not broker.orders
        assert bot.carry.state is BookState.FLAT

    def test_a_stale_view_sends_no_order(self):
        broker = Broker()
        bot = bot_with(broker)
        broker.orders.clear()
        # Observe the market an hour ago. assert_fresh must refuse it: a frozen
        # feed is indistinguishable from a quiet market without that check.
        import market_snapshot as ms
        real = ms.take_snapshot

        def stale(broker_, **kw):
            snap = real(broker_, **kw)
            return MarketSnapshot(
                perp_mark=snap.perp_mark, spot_mark=snap.spot_mark,
                funding_bps=snap.funding_bps,
                margin_multiple=snap.margin_multiple,
                observed_at_s=time.time() - 3600)

        with mock.patch.object(ms, "take_snapshot", stale):
            bot.tick()
        assert not broker.orders


class TestThePairGateRunsBeforeTheFirstLeg:
    def test_an_engaged_kill_switch_places_nothing(self):
        """A gate that runs after one leg has landed is a post-mortem."""
        broker = Broker()
        bot = bot_with(broker, store=Store(engaged=True))
        broker.orders.clear()
        bot.tick()
        assert not broker.orders
        assert bot.carry.position is None

    def test_a_notional_above_the_cap_places_nothing(self):
        broker = Broker()
        bot = bot_with(broker)
        bot.carry.max_notional_usd = 1_000_000.0     # engine wants more
        broker.orders.clear()
        bot.tick()
        assert not broker.orders, "the engine opened above the gate's cap"
        assert bot.carry.position is None

    def test_an_absent_gate_is_a_refusal_not_a_skip(self):
        """0033 INVERTED this test, deliberately. It used to assert that an
        engine WITHOUT a pair gate opens ("a bare engine in a unit test may
        open"). That was the bypass Phase C exists to remove: an absent gate
        is not a pass. The assertion is now the stricter one — no gate, no
        leg — and the refusal names itself."""
        broker = Broker()
        bot = bot_with(broker, risk=False)
        assert bot.carry.pair_risk is None
        broker.orders.clear()
        bot.tick()
        assert broker.orders == []
        assert bot.carry.position is None


class TestTheDayAllowanceIsSpentOnlyOnSuccess:
    def test_a_landed_pair_records_an_entry(self):
        broker = Broker()
        bot = bot_with(broker)
        bot.tick()
        assert bot.carry.position is not None
        assert len(bot.carry.pair_risk._entries) == 1

    def test_a_refused_pair_records_nothing(self):
        broker = Broker()
        bot = bot_with(broker, store=Store(engaged=True))
        bot.tick()
        assert not bot.carry.pair_risk._entries

    def test_a_second_entry_the_same_day_is_refused(self):
        broker = Broker()
        bot = bot_with(broker)
        bot.tick()                       # opens
        bot.carry.position = None        # pretend it unwound
        bot.carry.state = BookState.FLAT
        broker.orders.clear()
        bot.tick()
        assert not broker.orders
        assert bot.carry.position is None


class TestBuildBotAttachesTheGate:
    def test_carry_mode_gets_a_pair_gate(self):
        import config as _config
        cfg = _config.load({"BOOK_MODE": "carry", "CARRY_BORROW_APR": "0.05",
                            "CARRY_EXECUTION_MODE": "acquire"})
        bot = _main.build_bot(config=cfg, store=Store(), client=mock.Mock(),
                              risk_manager=mock.Mock(), engine=mock.Mock())
        assert bot.carry.pair_risk is not None

    def test_the_gate_cap_matches_the_engine_cap(self):
        import config as _config
        import shadow
        cfg = _config.load({"BOOK_MODE": "carry", "CARRY_BORROW_APR": "0.05",
                            "CARRY_EXECUTION_MODE": "acquire"})
        bot = _main.build_bot(config=cfg, store=Store(), client=mock.Mock(),
                              risk_manager=mock.Mock(), engine=mock.Mock())
        assert bot.carry.pair_risk.max_notional_usd == pytest.approx(
            shadow.SHADOW_MAX_NOTIONAL_USD)
        assert bot.carry.max_notional_usd == pytest.approx(
            bot.carry.pair_risk.max_notional_usd)
