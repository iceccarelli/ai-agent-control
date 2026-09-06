"""No silent defaults on the carry path. Every dependency is real or it raises.

The first version of the carry wiring had three of them and all three were
invisible:

    getattr(cfg, "MAX_NOTIONAL_USD", 100.0)   the key does not exist, so the
                                              risk cap was INVENTED and was not
                                              connected to the constant the
                                              promotion gate polices
    hasattr(store, "next_order_sequence")     wrong name, so every order got
                                              sequence 0, so the same intent on
                                              a later candle would build the
                                              SAME orderLinkId, the venue would
                                              reject it as a duplicate, and the
                                              adapter would resolve that by
                                              returning the OLD fill. The book
                                              would believe it re-opened a
                                              position it never placed.
    store.engage_kill_switch(...)             wrong name; the lambda raised,
                                              CarryEngine caught it, the book
                                              halted in memory and the
                                              PERSISTED switch was never set —
                                              a restart would resume trading.

Each was hidden by a default or a guard. These tests remove the hiding places.
"""
from __future__ import annotations

import os
import sys
from unittest import mock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import config as _config  # noqa: E402
import main as _main  # noqa: E402
import persistence  # noqa: E402
import shadow  # noqa: E402


class _Store:
    def __init__(self):
        self.tripped = []
        self.seq = 0

    def next_order_seq(self):
        self.seq += 1
        return self.seq

    def trip_kill_switch(self, reason):
        self.tripped.append(reason)

    def open_positions(self):
        return []


def _build(mode="carry", store=None):
    return _main.build_bot(config=_config.load({"BOOK_MODE": mode}),
                           store=store or _Store(), client=mock.Mock(),
                           risk_manager=mock.Mock(), engine=mock.Mock())


class TestTheCapIsTheGatesCap:
    def test_the_carry_cap_equals_the_authoritative_constant(self):
        """One cap, one source. If they can drift, the gate polices a number
        the book does not use."""
        assert _build().carry.max_notional_usd == pytest.approx(
            shadow.SHADOW_MAX_NOTIONAL_USD)

    def test_the_cap_is_not_read_from_config(self):
        """Config has no MAX_NOTIONAL_USD, and adding one would create a second
        authority that could silently disagree with the gate."""
        assert not hasattr(_config.load({}), "MAX_NOTIONAL_USD")

    def test_build_bot_does_not_default_the_cap(self):
        """AST, not text: the comment explaining this rule quotes the old line,
        and a substring check would find its own documentation."""
        import ast
        with open(os.path.join(os.path.dirname(__file__), "..", "main.py"),
                  encoding="utf-8") as fh:
            source = fh.read()
        for node in ast.walk(ast.parse(source)):
            if (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id == "getattr"
                    and len(node.args) >= 2
                    and isinstance(node.args[1], ast.Constant)):
                assert "NOTIONAL" not in str(node.args[1].value), (
                    "a risk cap is being read with a default; it must come "
                    "from shadow.SHADOW_MAX_NOTIONAL_USD")
        assert "SHADOW_MAX_NOTIONAL_USD" in source

    def test_moving_the_constant_moves_the_book(self):
        """Proves they are actually joined, not merely equal today."""
        with mock.patch.object(shadow, "SHADOW_MAX_NOTIONAL_USD", 250.0):
            assert _build().carry.max_notional_usd == pytest.approx(250.0)


class TestTheStoreDependenciesAreReal:
    @pytest.mark.parametrize("name", ["next_order_seq", "trip_kill_switch"])
    def test_the_method_the_wiring_calls_exists_on_the_real_store(self, name):
        assert hasattr(persistence.StateStore, name), (
            f"build_bot calls store.{name}; the real StateStore has no such "
            "method, so the carry book would fail at its first order")

    def test_the_sequence_advances_so_link_ids_differ(self):
        """A frozen sequence means a later candle rebuilds an id the venue has
        already seen, and the duplicate path returns the OLD fill."""
        store = _Store()
        bot = _build(store=store)
        broker = bot.carry.broker
        first = store.next_order_seq()
        second = store.next_order_seq()
        assert second > first
        assert broker.sequence_source("linear", "BTCUSDT", "carry") > second

    def test_the_kill_switch_reaches_the_store_not_a_lambda_that_raises(self):
        store = _Store()
        bot = _build(store=store)
        bot.carry.kill_switch("TEST_REASON")
        assert store.tripped == ["TEST_REASON"], (
            "the halt never reached the PERSISTED switch; a restart would "
            "resume trading")

    def test_the_wiring_is_not_guarded_by_hasattr(self):
        with open(os.path.join(os.path.dirname(__file__), "..", "main.py"),
                  encoding="utf-8") as fh:
            block = fh.read().split("BOOK_MODE ---")[1].split("return bot")[0]
        assert "hasattr(" not in block, (
            "a hasattr guard here swallows a misnamed dependency; an "
            "AttributeError at construction is the correct outcome")


class TestABadModeRefusesRatherThanFallingBack:
    def test_an_unknown_book_mode_raises(self):
        """A typo must not silently select a strategy."""
        with pytest.raises(ValueError):
            _main.build_bot(config=_config.load({"BOOK_MODE": "carrry"}),
                            store=_Store(), client=mock.Mock(),
                            risk_manager=mock.Mock(), engine=mock.Mock())

    @pytest.mark.parametrize("mode", ["carry", "directional"])
    def test_both_real_modes_still_build(self, mode):
        assert _build(mode) is not None


class TestSymbolsComeFromConfigNotADefault:
    def test_the_symbols_are_read_without_a_fallback(self):
        with open(os.path.join(os.path.dirname(__file__), "..", "main.py"),
                  encoding="utf-8") as fh:
            body = fh.read()
        import ast
        for node in ast.walk(ast.parse(body)):
            if (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id == "getattr"
                    and len(node.args) >= 2
                    and isinstance(node.args[1], ast.Constant)):
                assert "CARRY_" not in str(node.args[1].value)
        assert "cfg.CARRY_SPOT_SYMBOL" in body

    def test_a_configured_symbol_reaches_the_book(self):
        cfg = _config.load({"BOOK_MODE": "carry", "CARRY_PERP_SYMBOL": "ETHUSDT"})
        bot = _main.build_bot(config=cfg, store=_Store(), client=mock.Mock(),
                              risk_manager=mock.Mock(), engine=mock.Mock())
        assert bot.carry.perp_symbol == "ETHUSDT"
