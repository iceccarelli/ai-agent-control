"""BOOK_MODE=shadow — the cleared rule, finally attached to the runtime.

WHY THIS FILE EXISTS
====================
ROADMAP Stage A item 2. `build_bot` attached `MarketStrategy` unconditionally —
the legacy technical voter, whose Stage-1 verdict is ABSENT. The one rule this
programme has ever cleared was wired into nothing, and `project_status` said so
in as many words: *"nothing wires that signal into the engine and no trading
path consults the field. Wiring it up is a human decision that has not been
taken."*

This is that wiring. It changes what the shell RUNS, not what it is allowed to
do: paper only, BTCUSDT only, one position, one entry per calendar day, 100.00
USD notional, every existing gate unchanged, and the promotion gate still 2 of 8.

THE THREE THINGS THAT WOULD MAKE IT DECORATIVE, EACH PINNED BELOW
=================================================================
1. **A unit mismatch.** `funding_setups` compares against `FUND_ABS = 0.0001`,
   a FRACTION. The other funding reader in this tree returns BASIS POINTS from
   the same endpoint. A 10,000x error does not raise — it yields zero setups,
   which reads as a quiet market. `test_funding_carry_fade_v1`'s docstring
   records that exact bug happening once already.
2. **A missing time exit.** The rule holds for at most `HORIZON` bars. The
   runtime had NO time exit of any kind — `HORIZON` appeared only in the signal
   module and in research tools. A position left to run to its stop or target
   is a cousin of the measured rule, not the measured rule.
3. **A refused intent.** `ShadowStrategy` sets `confidence=None` deliberately
   (that field belongs to a promoted model and none exists). The risk ladder
   only runs `_gate_confidence` when confidence is NOT None, so the intent
   survives — but if that ever changes, every shadow entry is silently blocked
   and the attach becomes theatre.

TWO BUGS OF MINE ARE PINNED HERE TOO, because both would have passed a syntax
check and failed only in the loop: `main.py` used `dt.` with no `datetime`
import, and the horizon exit swallows its own exceptions — so a NameError
inside it would have meant the time exit silently never fired.
"""
from __future__ import annotations

import datetime as dt
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "tools"))

import main                                          # noqa: E402
import shadow as _shadow                             # noqa: E402
import shadow_strategy as ss                         # noqa: E402
from signals import funding_carry_fade_btc_v1 as fb  # noqa: E402

DAY = 86_400_000


class FakeClient:
    """A venue that answers the two reads the providers make. No network."""

    def __init__(self, rate=0.0005, bars=40):
        self.cfg = None
        self._rate = rate
        self._bars = bars
        self.kline_calls = []

    def get_klines(self, symbol, interval="60", limit=200):
        self.kline_calls.append((symbol, interval, limit))
        epoch = int(dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
                    .timestamp() * 1000)
        return [[epoch + i * DAY, "50000", "50500", "49500", "50100", "10", "0"]
                for i in range(self._bars)]

    def get_funding_history_fractions(self, symbol, limit=200):
        epoch = int(dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
                    .timestamp() * 1000)
        return [(epoch + i * 8 * 3_600_000, self._rate)
                for i in range(limit // 4)]


class TestTheUnitIsAFraction:
    """The 10,000x error that produces 'no setups' instead of an exception."""

    def test_the_provider_yields_fractions_not_basis_points(self):
        _sp, _bp, fp = ss.live_providers(FakeClient(rate=0.0005))
        series = fp("BTCUSDT")
        assert max(abs(r) for r in series.rates) < 0.01, (
            "a rate above 1% is basis points wearing a fraction's name")

    def test_a_fraction_at_the_threshold_is_a_setup(self):
        """End to end: the number the provider yields must clear FUND_ABS."""
        _sp, bp, fp = ss.live_providers(FakeClient(rate=fb.FUND_ABS))
        setups = fb.funding_setups(bp("BTCUSDT"), fp("BTCUSDT"))
        assert setups, "a rate exactly at FUND_ABS produced no setup"

    def test_the_named_client_method_is_the_one_used(self):
        """If this is ever repointed at the bps reader, the unit silently
        changes and nothing else in the stack would notice."""
        import inspect
        assert "get_funding_history_fractions" in inspect.getsource(
            ss.live_providers)
        assert "get_funding_history(" not in inspect.getsource(
            ss.live_providers)


class TestTheBarsAreClosedAndSufficient:
    def test_the_daily_interval_is_requested(self):
        client = FakeClient()
        _sp, bp, _fp = ss.live_providers(client)
        bp("BTCUSDT")
        assert client.kline_calls[0][1] == "D"

    def test_enough_bars_for_the_atr(self):
        """Even at lookback_days=1 the floor must cover the ATR warm-up, or
        `_current_setup` returns None forever and the pilot never proposes."""
        client = FakeClient()
        _sp, bp, _fp = ss.live_providers(client, lookback_days=1)
        bp("BTCUSDT")
        assert client.kline_calls[0][2] >= fb.ATR_PERIOD + 2

    def test_a_live_bar_has_what_close_time_needs(self):
        bar = ss._LiveBar(1_700_000_000_000, 1, 2, 0.5, 1.5)
        assert fb.close_time_ms(bar) == bar.start_ms + DAY - 1


class TestTheHorizonExit:
    """The rule holds at most HORIZON bars. The runtime had no time exit."""

    class _Engine:
        def __init__(self):
            self.closed = []

        def close_position(self, *, symbol, reason, **kw):
            self.closed.append((symbol, reason))
            return type("R", (), {"reason": reason})()

    class _Store:
        def __init__(self, rows):
            self._rows = rows

        def open_positions(self):
            return list(self._rows)

    def _bot(self, days_held):
        bot = main.TradingBot.__new__(main.TradingBot)
        opened = (dt.datetime.now(dt.timezone.utc)
                  - dt.timedelta(days=days_held)).timestamp()
        bot.store = self._Store([{"symbol": "BTCUSDT", "opened_epoch": opened}])
        bot.engine = self._Engine()
        bot.shadow_horizon_days = fb.HORIZON
        return bot

    def test_a_position_at_the_horizon_is_closed(self):
        bot = self._bot(fb.HORIZON)
        bot._expire_at_horizon()
        assert bot.engine.closed == [("BTCUSDT", f"HORIZON_{fb.HORIZON}_BARS")]

    def test_a_younger_position_is_left_alone(self):
        bot = self._bot(fb.HORIZON - 1)
        bot._expire_at_horizon()
        assert bot.engine.closed == []

    def test_an_older_position_is_still_closed(self):
        bot = self._bot(fb.HORIZON + 10)
        bot._expire_at_horizon()
        assert bot.engine.closed

    def test_it_does_nothing_when_no_shadow_book_is_attached(self):
        bot = self._bot(fb.HORIZON)
        bot.shadow_horizon_days = None
        bot._expire_at_horizon()
        assert bot.engine.closed == []

    def test_the_horizon_comes_from_the_frozen_module(self):
        """A second copy of the number is a second thing to forget."""
        import inspect
        source = inspect.getsource(main.build_bot)
        assert "_fb.HORIZON" in source
        assert "shadow_horizon_days = 5" not in source


class TestTheBugsIAlmostShipped:
    def test_main_imports_datetime(self):
        """`_expire_at_horizon` uses `dt.` three times and main.py had NO
        datetime import. It compiled: a missing name is a runtime error. The
        method swallows its own exceptions, so the time exit would have
        silently never fired while logging looked healthy."""
        assert hasattr(main, "dt")
        assert main.dt.timezone.utc is dt.timezone.utc

    def test_the_horizon_exit_logs_rather_than_raises(self):
        """Swallowing is deliberate — a failed close must not kill the loop —
        which is exactly why the NameError above had to be caught by a test."""
        bot = main.TradingBot.__new__(main.TradingBot)
        bot.shadow_horizon_days = fb.HORIZON

        class Exploding:
            def open_positions(self):
                raise RuntimeError("db gone")

        bot.store = Exploding()
        bot.engine = None
        bot._expire_at_horizon()          # must not raise


class TestTheBookIsSelectedDeliberately:
    def test_shadow_is_a_recognised_book(self):
        import inspect
        source = inspect.getsource(main.build_bot)
        assert '"carry", "directional", "shadow"' in source

    def test_a_typo_still_refuses(self):
        import inspect
        source = inspect.getsource(main.build_bot)
        assert "is not a book" in source

    def test_the_caps_are_the_frozen_ones(self):
        assert _shadow.SHADOW_MAX_NOTIONAL_USD == 100.0
        assert _shadow.SHADOW_SYMBOL == "BTCUSDT"
        assert _shadow.SHADOW_SIGNAL == "funding_carry_fade_btc_v1"


class TestItChangesWhatRunsNotWhatIsAllowed:
    def test_the_promotion_gate_is_untouched(self):
        import promotion_gate as pg
        verdict = pg.evaluate_promotion_gate()
        assert verdict.allows_live is False
        assert sum(1 for i in verdict.items if i.complete) == 2

    def test_a_confidence_of_none_is_not_gated(self):
        """ShadowStrategy sets confidence=None on purpose. The ladder runs
        _gate_confidence only when it is NOT None; if that ever changes, every
        shadow entry is blocked and this wiring becomes theatre."""
        import inspect
        import risk_management as rm
        source = inspect.getsource(rm.BillionaireRiskManager.gate_order)
        assert "if confidence is not None:" in source
