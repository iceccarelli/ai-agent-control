"""The carry book is WIRED. This is the patch that stops it being shelfware.

Before 0015, `CarryEngine` and `CarryBroker` were two well-tested modules that
NOTHING CONSTRUCTED. `build_bot` attached `MarketStrategy(TechnicalAnalysis)` —
the voter whose own Stage-1 verdict is ABSENT — and `tick` never mentioned the
carry book. That is exactly what happened to `ShadowStrategy` for eighty slices:
built, tested, never run.

These tests keep it wired.
"""
from __future__ import annotations

import os
import sys
from unittest import mock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import config as _config  # noqa: E402
import main as _main  # noqa: E402
from carry_engine import BookState  # noqa: E402


class _Store:
    """The stub must carry the REAL StateStore method names.

    An earlier version used next_order_sequence / engage_kill_switch, which do
    not exist on StateStore. The stub passed, production would have failed at
    the first order. A test double that is kinder than reality tests nothing.
    """

    def __init__(self):
        self.killed = []
        self.seq = 0

    def next_order_seq(self):
        self.seq += 1
        return self.seq

    def trip_kill_switch(self, reason):
        self.killed.append(reason)

    def open_positions(self):
        return []


class _Broker:
    """Stands in for CarryBroker with the four calls the cycle needs."""

    def __init__(self, *, mark=100_000.0, funding=1.0, raises=None):
        self.mark = mark
        self.funding = funding
        self.raises = raises
        self.orders = []

    def get_mark(self, symbol):
        if self.raises == "mark":
            raise RuntimeError("venue down")
        return self.mark

    def get_funding_bps(self, symbol):
        if self.raises == "funding":
            raise RuntimeError("no fundingRate in ticker")
        return self.funding

    def get_margin_multiple(self, symbol):
        return 5.0

    def place_market(self, *, symbol, side, qty, product):
        self.orders.append((symbol, side, qty, product))
        return {"filled_qty": qty, "avg_price": self.mark,
                "order_link_id": f"x{len(self.orders)}"}


def _bot_with_carry(broker):
    from carry_engine import CarryEngine
    bot = _main.TradingBot.__new__(_main.TradingBot)
    bot.symbols = ["BTCUSDT"]
    bot.carry = CarryEngine(broker=broker, max_notional_usd=100_000.0)
    bot.strategy = None
    bot.store = _Store()
    bot._stop = mock.Mock(is_set=lambda: False)
    bot.session = mock.Mock()
    bot.client = mock.Mock(get_equity=lambda: 1_000_000.0)
    bot.risk = mock.Mock(should_halt_trading=lambda: False,
                         update_equity=lambda e: None)
    bot.engine = mock.Mock(observe_exits=lambda: {})
    return bot


class TestTheCycleRunsTheCarryBook:
    def test_a_tick_opens_the_pair(self):
        broker = _Broker(funding=1.0)
        bot = _bot_with_carry(broker)
        bot.tick()
        assert len(broker.orders) == 2, "the cycle did not place a pair"
        assert bot.carry.state is BookState.HEDGED

    def test_thin_funding_places_nothing(self):
        broker = _Broker(funding=0.01)
        bot = _bot_with_carry(broker)
        bot.tick()
        assert not broker.orders

    def test_the_directional_strategy_is_never_consulted(self):
        broker = _Broker()
        bot = _bot_with_carry(broker)
        bot.strategy = mock.Mock()
        bot.tick()
        bot.strategy.signal_for.assert_not_called()
        bot.engine.execute.assert_not_called()


class TestUnreadableInputsTouchNothing:
    @pytest.mark.parametrize("broken", ["mark", "funding"])
    def test_a_venue_read_that_raises_sends_no_order(self, broken):
        """A book that trades on absent data is the failure this system exists
        to prevent. No order, no state change, no silent zero."""
        broker = _Broker(raises=broken)
        bot = _bot_with_carry(broker)
        bot.tick()
        assert not broker.orders
        assert bot.carry.state is BookState.FLAT

    def test_funding_is_never_defaulted_to_zero(self):
        """Zero would read as 'thin, stand aside' — a DECISION on absent data."""
        import inspect
        from carry_broker import CarryBroker
        source = inspect.getsource(CarryBroker.get_funding_bps)
        assert "return 0" not in source
        assert "raise PairIncident" in source


class TestTheTwoBooksAreMutuallyExclusive:
    def test_carry_mode_never_constructs_the_technical_voter(self):
        cfg = _config.load({"BOOK_MODE": "carry"})
        with mock.patch("technical_analysis.MarketStrategy") as voter:
            bot = _main.build_bot(config=cfg, store=_Store(),
                                  client=mock.Mock(), risk_manager=mock.Mock(),
                                  engine=mock.Mock())
            voter.assert_not_called()
        assert bot.carry is not None
        assert bot.strategy is None

    def test_directional_mode_attaches_no_carry_book(self):
        cfg = _config.load({"BOOK_MODE": "directional"})
        bot = _main.build_bot(config=cfg, store=_Store(), client=mock.Mock(),
                              risk_manager=mock.Mock(), engine=mock.Mock())
        assert bot.carry is None

    def test_a_process_can_never_hold_both(self):
        """They share one liquidation price. Two strategies fighting over it is
        how a hedged book becomes a directional one without anyone deciding."""
        for mode in ("carry", "directional"):
            cfg = _config.load({"BOOK_MODE": mode})
            bot = _main.build_bot(config=cfg, store=_Store(),
                                  client=mock.Mock(), risk_manager=mock.Mock(),
                                  engine=mock.Mock())
            assert not (bot.carry is not None and bot.strategy is not None)

    def test_the_default_is_still_directional(self):
        """Dozens of tests predate the carry book. Selecting carry is an
        operator act, not something that happens by upgrade."""
        assert _config.load({}).BOOK_MODE == "directional"


class TestTheWiringCannotRegress:
    def test_build_bot_knows_about_the_carry_book(self):
        with open(os.path.join(os.path.dirname(__file__), "..", "main.py"),
                  encoding="utf-8") as fh:
            body = fh.read()
        assert "CarryEngine(" in body, "build_bot no longer constructs the book"
        assert "self.carry.on_candle(" in body, "tick no longer runs the book"

    def test_no_llm_reaches_the_order_path(self):
        with open(os.path.join(os.path.dirname(__file__), "..", "main.py"),
                  encoding="utf-8") as fh:
            carry_block = fh.read().split("if self.carry is not None:")[1] \
                                   .split("if self.strategy is None:")[0]
        for banned in ("openai", "anthropic", "llm", "prompt", "completion"):
            assert banned not in carry_block.lower()
