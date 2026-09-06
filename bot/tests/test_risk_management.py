"""Contract tests for the repaired risk gate chain.

Fully offline. No network, no real exchange, no sleeping.

The central claim these tests defend: **every gate blocks when it cannot prove
the trade is safe.** The legacy module had fifteen gates that returned a
permissive value on exception, including the kill switch itself. Several tests
below deliberately break the state store and assert that the answer is "no".
"""
from __future__ import annotations

import ast
import inspect
import math
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import risk_management as rm  # noqa: E402
from persistence import StateStore, TradeRecord, utc_now_epoch  # noqa: E402

EQUITY = 10_000.0


@pytest.fixture()
def store(tmp_path):
    s = StateStore(str(tmp_path / "state.db"))
    s.update_equity(EQUITY)
    yield s
    s.close()


@pytest.fixture()
def manager(store):
    return rm.BillionaireRiskManager(store=store)


@pytest.fixture()
def linear_config():
    """A USDT-perpetual config: the venue where a short is a real trade."""
    import config as _config

    return _config.load({"CATEGORY": "linear", "MIN_EDGE_BPS": "20"})


@pytest.fixture()
def linear_manager(store, linear_config):
    return rm.BillionaireRiskManager(config=linear_config, store=store)


def good_order(**over):
    """A trade that passes every gate, so each test can break exactly one thing."""
    base = dict(
        symbol="BTCUSDT",
        side="Buy",
        entry_price=50_000.0,
        stop_loss=49_500.0,   # 1% stop
        quantity=0.001,       # $50 notional, $0.50 risk = 0.005% of equity
        account_equity=EQUITY,
    )
    base.update(over)
    return base


# ---------------------------------------------------------------------------
# the equity-space conversion
# ---------------------------------------------------------------------------


class TestEquityRiskMath:
    def test_risk_is_size_aware(self):
        """The defect in one line: the legacy check had no quantity term.

        Two trades with an identical 2% stop but 100x different size must not
        produce the same risk number.
        """
        small = rm.equity_risk_fraction(
            qty=0.001, entry_price=50_000, stop_price=49_000, equity=EQUITY
        )
        large = rm.equity_risk_fraction(
            qty=0.1, entry_price=50_000, stop_price=49_000, equity=EQUITY
        )
        assert large == pytest.approx(small * 100)
        assert small == pytest.approx(0.0001)
        assert large == pytest.approx(0.01)

    def test_leveraged_position_shows_leveraged_risk(self):
        """A 9x-equity notional with a 2% stop risks 18% of equity, not 2%."""
        notional = EQUITY * 9
        qty = notional / 50_000
        frac = rm.equity_risk_fraction(
            qty=qty, entry_price=50_000, stop_price=49_000, equity=EQUITY
        )
        assert frac == pytest.approx(0.18)

    @pytest.mark.parametrize(
        "kwargs",
        [
            dict(qty=1, entry_price=0, stop_price=1, equity=EQUITY),
            dict(qty=1, entry_price=1, stop_price=0, equity=EQUITY),
            dict(qty=1, entry_price=1, stop_price=1, equity=0),
            dict(qty=1, entry_price=1, stop_price=1, equity=-5),
            dict(qty=float("nan"), entry_price=1, stop_price=1, equity=EQUITY),
            dict(qty=float("inf"), entry_price=1, stop_price=1, equity=EQUITY),
        ],
    )
    def test_bad_inputs_raise_rather_than_return_a_number(self, kwargs):
        with pytest.raises(ValueError):
            rm.equity_risk_fraction(**kwargs)

    def test_max_qty_round_trips_with_risk_fraction(self):
        q = rm.max_qty_for_risk_budget(
            entry_price=50_000, stop_price=49_000, equity=EQUITY, risk_fraction=0.005
        )
        back = rm.equity_risk_fraction(
            qty=q, entry_price=50_000, stop_price=49_000, equity=EQUITY
        )
        assert back == pytest.approx(0.005)

    def test_zero_stop_distance_gives_zero_size(self):
        """No stop distance means no protection; the only safe size is none."""
        assert rm.max_qty_for_risk_budget(
            entry_price=50_000, stop_price=50_000, equity=EQUITY, risk_fraction=0.01
        ) == 0.0

    def test_leverage_adjusted_stop_tightens_under_leverage(self):
        """Restored legacy math: 10x leverage turns a 2% stop into 20% of account."""
        stop, reason = rm.leverage_adjusted_stop_fraction(
            base_stop_fraction=0.02, leverage=10.0, max_account_risk_fraction=0.10
        )
        assert reason == "LEVERAGE_RISK_LIMIT"
        assert stop == pytest.approx(0.01)
        assert stop * 10.0 == pytest.approx(0.10)

    def test_leverage_adjusted_stop_left_alone_when_within_budget(self):
        stop, reason = rm.leverage_adjusted_stop_fraction(
            base_stop_fraction=0.005, leverage=1.0, max_account_risk_fraction=0.10
        )
        assert reason == "NO_ADJUSTMENT_NEEDED"
        assert stop == pytest.approx(0.005)


# ---------------------------------------------------------------------------
# fail-closed behaviour  (the module's reason for existing)
# ---------------------------------------------------------------------------


class BrokenStore:
    """A store where every read raises, simulating a corrupt or locked DB."""

    def __getattr__(self, name):
        def boom(*a, **k):
            raise RuntimeError(f"state store unavailable ({name})")
        return boom


class TestFailClosed:
    def test_baseline_order_is_approved(self, manager):
        """Guards the other tests: if this failed, blocks would prove nothing."""
        assert manager.gate_order(**good_order()).ok is True

    def test_every_gate_blocks_when_state_is_unreadable(self, manager):
        """Each gate, individually, with a store that raises."""
        manager.store = BrokenStore()
        gates = [
            ("_gate_kill_switch", ()),
            ("_gate_breaker", ()),
            ("_gate_symbol_cooldown", ("BTCUSDT",)),
            ("_gate_consecutive_losses", ()),
            ("_gate_position_count", ()),
            ("_gate_naked_positions", ()),
        ]
        for name, args in gates:
            decision = getattr(manager, name)(*args)
            assert decision.ok is False, f"{name} failed OPEN with an unreadable store"

    def test_daily_loss_and_drawdown_gates_block_when_unreadable(self, manager):
        manager.store = BrokenStore()
        assert manager._gate_daily_loss(EQUITY).ok is False
        assert manager._gate_drawdown(EQUITY).ok is False

    def test_full_chain_blocks_when_state_is_unreadable(self, manager):
        manager.store = BrokenStore()
        decision = manager.gate_order(**good_order())
        assert decision.ok is False

    def test_should_halt_returns_true_when_state_is_unreadable(self, manager):
        manager.store = BrokenStore()
        assert manager.should_halt_trading() is True

    def test_can_trade_returns_false_when_state_is_unreadable(self, manager):
        manager.store = BrokenStore()
        assert manager.can_trade("BTCUSDT") is False

    def test_pretrade_check_blocks_when_state_is_unreadable(self, manager):
        manager.store = BrokenStore()
        assert manager.pretrade_check({"symbol": "BTCUSDT", "qty": 0.001}) is False

    def test_maker_qty_quotes_nothing_when_state_is_unreadable(self, manager):
        manager.store = BrokenStore()
        assert manager.maker_qty("BTCUSDT", 50_000.0) == 0.0

    def test_kill_active_treats_unreadable_as_engaged(self):
        assert rm.kill_active(store=BrokenStore()) is True

    def test_risk_decision_defaults_to_blocked(self):
        """A decision object constructed with no arguments must not allow."""
        assert rm.RiskDecision().ok is False
        assert bool(rm.RiskDecision()) is False
        assert rm.GateOutcome().ok is False

    def test_no_fail_open_except_handlers_in_source(self):
        """Mechanical guard against the legacy module's dominant bug class.

        Walks the AST for any `except` handler that returns a truthy constant,
        which is the shape of `except Exception: return True`.
        """
        path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "risk_management.py",
        )
        source = open(path, encoding="utf-8").read()
        tree = ast.parse(source)

        # In two functions, True *is* the blocking answer: `should_halt_trading`
        # returns "yes, halt" and `kill_active` returns "yes, the kill switch is
        # on". Returning True there on exception is fail-CLOSED. Everywhere else
        # `except: return True` is the legacy fail-open pattern.
        TRUE_MEANS_BLOCK = {"should_halt_trading", "kill_active"}

        offenders = []
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if fn.name in TRUE_MEANS_BLOCK:
                continue
            for node in ast.walk(fn):
                if not isinstance(node, ast.ExceptHandler):
                    continue
                for sub in ast.walk(node):
                    if isinstance(sub, ast.Return) and isinstance(sub.value, ast.Constant):
                        if sub.value.value is True:
                            offenders.append((fn.name, sub.lineno))
        assert offenders == [], (
            f"fail-open `except: return True` in {offenders}"
        )


# ---------------------------------------------------------------------------
# individual gates
# ---------------------------------------------------------------------------


class TestKillSwitch:
    def test_engaged_kill_switch_blocks_everything(self, manager, store):
        store.trip_kill_switch("test")
        assert manager.gate_order(**good_order()).reason == "KILL_SWITCH_ENGAGED"
        assert manager.should_halt_trading() is True

    def test_manager_cannot_clear_the_kill_switch(self, manager, store):
        """Rule 11. The bot may trip it; only a human clears it."""
        store.trip_kill_switch("test")
        clearers = [
            n for n in dir(manager)
            if "clear" in n.lower() and "kill" in n.lower()
        ]
        assert clearers == [], f"risk manager exposes a kill-switch clearer: {clearers}"
        assert store.is_kill_switch_engaged()[0] is True

    def test_human_clear_requires_exact_acknowledgement(self, store):
        store.trip_kill_switch("test")
        for bad in ("", "yes", "clear", "HUMAN_CLEARED", "human_cleared_kill_switch"):
            with pytest.raises(Exception):
                store.clear_kill_switch_by_human(bad)
        assert store.is_kill_switch_engaged()[0] is True
        store.clear_kill_switch_by_human("HUMAN_CLEARED_KILL_SWITCH")
        assert store.is_kill_switch_engaged()[0] is False


class TestStopSanity:
    def test_missing_stop_blocks(self, manager):
        assert manager.gate_order(**good_order(stop_loss=0.0)).reason == "NO_STOP_LOSS"

    def test_buy_stop_above_entry_is_rejected_not_normalised(self, manager):
        """Legacy pushed an inverted stop FURTHER the wrong way. Reject instead."""
        d = manager.gate_order(**good_order(side="Buy", stop_loss=51_000.0))
        assert d.reason == "STOP_ON_WRONG_SIDE"

    def test_sell_stop_below_entry_is_rejected(self, linear_manager):
        d = linear_manager.gate_order(
            **good_order(side="Sell", stop_loss=49_000.0, funding_rate=0.0001)
        )
        assert d.reason == "STOP_ON_WRONG_SIDE"

    def test_sell_with_stop_above_entry_is_accepted(self, linear_manager):
        assert linear_manager.gate_order(
            **good_order(side="Sell", stop_loss=50_500.0, funding_rate=0.0001)
        ).ok

    def test_a_short_needs_a_venue_that_shorts(self, manager):
        """These two tests used to run a SELL through a spot manager.

        They passed, because nothing in the chain knew that spot has no short.
        Slice 7 gave the chain that knowledge, so the correct assertion is now
        two assertions: a spot manager refuses the direction outright, and the
        stop-geometry check is exercised on the venue where a short is real.
        """
        d = manager.gate_order(**good_order(side="Sell", stop_loss=50_500.0))
        assert d.ok is False
        assert d.reason == "SHORTS_NOT_AVAILABLE_ON_SPOT"
        assert "CATEGORY=linear" in d.detail["remedy"]

    @pytest.mark.parametrize("bad_side", ["Sideways", "banana", "", "STRONG_BUY", None])
    def test_unknown_side_blocks(self, manager, bad_side):
        """`"Sideways".startswith("s")` once made this a SELL."""
        assert manager.gate_order(**good_order(side=bad_side)).reason == "UNKNOWN_SIDE"

    @pytest.mark.parametrize(
        "side,expected", [("Buy", "Buy"), ("buy", "Buy"), ("LONG", "Buy"),
                          ("Sell", "Sell"), ("short", "Sell"),
                          ("Sideways", None), ("", None), (None, None)],
    )
    def test_side_normalisation_is_exact_not_prefix(self, side, expected):
        assert rm.normalize_side(side) == expected


class TestRiskReward:
    def test_low_rr_is_rejected_not_rewritten(self, manager):
        """Audit C7: the legacy path overwrote a failing RR with the minimum."""
        d = manager.gate_order(**good_order(take_profit=50_250.0))  # RR 0.5
        assert d.ok is False
        assert d.reason == "RISK_REWARD_TOO_LOW"
        assert d.detail["rr"] == pytest.approx(0.5)
        assert "rr" not in d.adjustments

    def test_sufficient_rr_passes(self, manager):
        assert manager.gate_order(**good_order(take_profit=51_500.0)).ok  # RR 3

    def test_missing_take_profit_blocks_when_rr_is_checked(self, manager):
        assert manager._gate_risk_reward(
            entry_price=50_000, stop_price=49_500, take_profit_price=None
        ).reason == "NO_TAKE_PROFIT"


class TestConfidence:
    def test_uncapped_confidence_is_rejected(self, manager):
        """Audit C6: legacy confidence could exceed 2.0, so it was not a probability."""
        d = manager.gate_order(**good_order(confidence=2.5))
        assert d.reason == "CONFIDENCE_OUT_OF_RANGE"

    def test_negative_confidence_is_rejected(self, manager):
        assert manager.gate_order(**good_order(confidence=-0.1)).ok is False

    def test_low_confidence_blocks(self, manager):
        d = manager.gate_order(**good_order(confidence=0.01))
        assert d.reason == "CONFIDENCE_TOO_LOW"

    def test_high_confidence_passes(self, manager):
        assert manager.gate_order(**good_order(confidence=0.95)).ok


class TestSizeCaps:
    def test_per_trade_risk_cap_blocks_and_suggests_smaller(self, manager):
        d = manager.gate_order(**good_order(quantity=1.0))
        assert d.ok is False
        assert d.reason in {"PER_TRADE_RISK_EXCEEDED", "POSITION_SIZE_CAP"}
        assert d.adjustments["max_qty"] > 0.0
        assert d.adjustments["max_qty"] < 1.0

    def test_adjustment_never_raises_the_limit(self, manager):
        """An adjustment is a smaller size, never a larger budget."""
        d = manager.gate_order(**good_order(quantity=1.0))
        suggested = d.adjustments["max_qty"]
        frac = rm.equity_risk_fraction(
            qty=suggested, entry_price=50_000.0, stop_price=49_500.0, equity=EQUITY
        )
        assert frac <= manager.max_risk_per_trade_pct + 1e-12

    def test_aggregate_exposure_cap_counts_existing_positions(self, manager, store):
        store.upsert_position("ETHUSDT", "Buy", 1.0, 900.0, stop_price=880.0)
        d = manager._gate_aggregate_exposure(
            qty=0.01, entry_price=50_000.0, equity=EQUITY
        )
        assert d.ok is False
        assert d.reason == "AGGREGATE_EXPOSURE_CAP"

    def test_position_count_cap(self, manager, store):
        for i in range(manager.max_open_positions):
            store.upsert_position(f"SYM{i}USDT", "Buy", 0.001, 100.0, stop_price=99.0)
        assert manager.gate_order(**good_order()).reason == "MAX_OPEN_POSITIONS"


class TestCorrelationBuckets:
    def test_correlated_alts_share_a_bucket(self):
        b = rm.CorrelationBuckets()
        assert b.bucket_of("SOLUSDT") == b.bucket_of("AVAXUSDT") == "L1_ALT"
        assert b.bucket_of("BTCUSDT") == "BTC_BETA"

    def test_unknown_symbol_gets_its_own_bucket(self):
        b = rm.CorrelationBuckets()
        assert b.bucket_of("NEWCOINUSDT").startswith("UNGROUPED:")

    def test_exposure_reads_dicts_not_attributes(self):
        """The legacy crash: `position.position_size_pct` on a dict -> 0.0 -> never blocks."""
        b = rm.CorrelationBuckets()
        exposure = b.exposure_by_bucket([
            {"symbol": "SOLUSDT", "qty": 10.0, "entry_price": 100.0},
            {"symbol": "AVAXUSDT", "qty": 10.0, "entry_price": 50.0},
        ])
        assert exposure["L1_ALT"] == pytest.approx(1500.0)

    def test_correlated_exposure_blocks_a_sixth_correlated_position(self, manager, store):
        # Three L1 alt positions already using ~25% of equity in one bucket.
        for sym, px in (("SOLUSDT", 100.0), ("AVAXUSDT", 50.0), ("ADAUSDT", 1.0)):
            store.upsert_position(sym, "Buy", 10.0, px, stop_price=px * 0.98)
        before = manager._gate_correlation_bucket(
            symbol="DOTUSDT", qty=10.0, entry_price=10.0, equity=EQUITY
        )
        assert before.ok is True, "sanity: a small addition should still fit"

        # A fourth correlated position ($2,000) pushes the L1_ALT bucket to
        # ~35% of equity, past the 30% cap.
        d = manager._gate_correlation_bucket(
            symbol="DOTUSDT", qty=200.0, entry_price=10.0, equity=EQUITY
        )
        assert d.ok is False
        assert d.reason == "CORRELATION_BUCKET_CAP"
        assert d.detail["bucket"] == "L1_ALT"

    def test_uncorrelated_position_of_the_same_size_is_allowed(self, manager, store):
        """The cap must bite on correlation, not merely on size."""
        for sym, px in (("SOLUSDT", 100.0), ("AVAXUSDT", 50.0), ("ADAUSDT", 1.0)):
            store.upsert_position(sym, "Buy", 10.0, px, stop_price=px * 0.98)
        d = manager._gate_correlation_bucket(
            symbol="BTCUSDT", qty=0.01, entry_price=50_000.0, equity=EQUITY
        )
        assert d.ok is True
        assert d.detail["bucket"] == "BTC_BETA"


class TestNakedPositions:
    def test_a_position_without_a_stop_blocks_new_trades(self, manager, store):
        store.upsert_position("ETHUSDT", "Buy", 0.001, 3000.0, stop_price=0.0)
        d = manager.gate_order(**good_order())
        assert d.ok is False
        assert d.reason == "NAKED_POSITION_OPEN"

    def test_protected_positions_do_not_block(self, manager, store):
        store.upsert_position("ETHUSDT", "Buy", 0.001, 3000.0, stop_price=2900.0)
        assert manager.gate_order(**good_order()).ok


# ---------------------------------------------------------------------------
# persistence across restart
# ---------------------------------------------------------------------------


class TestStateSurvivesRestart:
    def test_drawdown_survives_restart(self, tmp_path):
        """Legacy peak_balance reseeded from 0.0, so drawdown read zero exactly
        during a crash-loop."""
        db = str(tmp_path / "s.db")
        s1 = StateStore(db)
        s1.update_equity(10_000.0)
        s1.update_equity(9_000.0)
        assert s1.drawdown_fraction() == pytest.approx(0.10)
        s1.close()

        s2 = StateStore(db)
        assert s2.get_equity_state().peak_equity == pytest.approx(10_000.0)
        assert s2.drawdown_fraction() == pytest.approx(0.10)
        s2.close()

    def test_breaker_and_cooldown_survive_restart(self, tmp_path):
        db = str(tmp_path / "s.db")
        s1 = StateStore(db)
        s1.update_equity(EQUITY)
        s1.trip_breaker("test", 3600)
        s1.set_symbol_cooldown("BTCUSDT", 600)
        s1.close()

        s2 = StateStore(db)
        m2 = rm.BillionaireRiskManager(store=s2)
        assert m2.should_halt_trading() is True
        assert m2.gate_order(**good_order()).ok is False
        assert s2.symbol_cooldown_remaining("BTCUSDT") > 0
        s2.close()

    def test_kill_switch_survives_restart(self, tmp_path):
        db = str(tmp_path / "s.db")
        s1 = StateStore(db)
        s1.trip_kill_switch("drawdown")
        s1.close()
        s2 = StateStore(db)
        assert s2.is_kill_switch_engaged()[0] is True
        s2.close()

    def test_daily_anchor_is_not_reset_by_a_restart(self, tmp_path):
        """A mid-day restart must not re-anchor at drawn-down equity."""
        db = str(tmp_path / "s.db")
        s1 = StateStore(db)
        s1.update_equity(10_000.0)
        s1.close()
        s2 = StateStore(db)
        s2.update_equity(9_500.0)
        assert s2.get_daily_anchor().start_equity == pytest.approx(10_000.0)
        assert s2.daily_loss_fraction(9_500.0) == pytest.approx(0.05)
        s2.close()

    def test_order_sequence_is_monotonic_across_restart(self, tmp_path):
        """Deterministic orderLinkId depends on a counter that does not reset."""
        db = str(tmp_path / "s.db")
        s1 = StateStore(db)
        first = [s1.next_order_seq() for _ in range(3)]
        s1.close()
        s2 = StateStore(db)
        second = [s2.next_order_seq() for _ in range(3)]
        s2.close()
        assert first == [1, 2, 3]
        assert second == [4, 5, 6], "counter reset on restart -> duplicate orders"


# ---------------------------------------------------------------------------
# daily loss / drawdown arithmetic  (audit C9)
# ---------------------------------------------------------------------------


class TestDailyLossArithmetic:
    def test_a_profitable_day_never_trips_the_daily_loss_gate(self, manager, store):
        """Audit C9: `abs()` on a dollar PnL made +$5.01 trip an EMERGENCY STOP."""
        store.update_equity(EQUITY * 1.05)
        d = manager._gate_daily_loss(EQUITY * 1.05)
        assert d.ok is True, "a profitable day must not trip the loss brake"

    def test_loss_is_measured_as_a_fraction_of_start_of_day_equity(self, manager, store):
        assert store.daily_loss_fraction(EQUITY * 0.98) == pytest.approx(0.02)

    def test_daily_loss_limit_blocks_and_trips_the_breaker(self, manager, store):
        breach = EQUITY * (1.0 - manager.max_daily_loss_pct - 0.001)
        d = manager._gate_daily_loss(breach)
        assert d.ok is False
        assert d.reason == "DAILY_LOSS_LIMIT"
        assert store.get_breaker().active is True

    def test_drawdown_limit_trips_the_kill_switch(self, manager, store):
        breach = EQUITY * (1.0 - manager.max_drawdown_pct - 0.001)
        d = manager._gate_drawdown(breach)
        assert d.ok is False
        assert d.reason == "MAX_DRAWDOWN"
        assert store.is_kill_switch_engaged()[0] is True

    def test_scale_independence(self, tmp_path):
        """A 4% day must read the same on a $100 account and a $1M account.

        The legacy comparison of dollars to percentages meant the same threshold
        fired at wildly different real losses depending on account size.
        """
        for size in (100.0, 1_000_000.0):
            s = StateStore(str(tmp_path / f"s{size}.db"))
            s.update_equity(size)
            assert s.daily_loss_fraction(size * 0.96) == pytest.approx(0.04)
            s.close()


# ---------------------------------------------------------------------------
# fee-aware trade accounting  (the Kelly input)
# ---------------------------------------------------------------------------


class TestFeeAwareAccounting:
    def test_a_gross_win_that_loses_after_fees_counts_as_a_loss(self, store):
        t = TradeRecord(
            symbol="BTCUSDT", side="Buy", qty=0.01,
            entry_price=50_000.0, exit_price=50_010.0,
            gross_pnl=0.10, entry_fee=0.25, exit_fee=0.25,
            opened_epoch=utc_now_epoch() - 60, closed_epoch=utc_now_epoch(),
        )
        assert t.gross_pnl > 0
        assert t.net_pnl < 0
        assert t.is_win is False

    def test_consecutive_losses_counts_net_not_gross(self, store):
        for _ in range(3):
            store.record_trade(TradeRecord(
                symbol="BTCUSDT", side="Buy", qty=0.01,
                entry_price=50_000.0, exit_price=50_010.0,
                gross_pnl=0.10, entry_fee=0.25, exit_fee=0.25,
                opened_epoch=utc_now_epoch() - 60, closed_epoch=utc_now_epoch(),
            ))
        assert store.consecutive_losses() == 3

    def test_loss_streak_trips_the_breaker(self, manager, store):
        for _ in range(manager.max_consecutive_losses):
            store.record_trade(TradeRecord(
                symbol="BTCUSDT", side="Buy", qty=0.01,
                entry_price=50_000.0, exit_price=49_000.0,
                gross_pnl=-10.0, entry_fee=0.25, exit_fee=0.25,
                opened_epoch=utc_now_epoch() - 60, closed_epoch=utc_now_epoch(),
            ))
        d = manager._gate_consecutive_losses()
        assert d.ok is False
        assert d.reason == "CONSECUTIVE_LOSSES"

    def test_net_returns_are_fee_inclusive(self, store):
        store.record_trade(TradeRecord(
            symbol="BTCUSDT", side="Buy", qty=0.01,
            entry_price=50_000.0, exit_price=50_500.0,
            gross_pnl=5.0, entry_fee=0.5, exit_fee=0.5,
            opened_epoch=utc_now_epoch() - 60, closed_epoch=utc_now_epoch(),
        ))
        returns = store.net_returns()
        assert returns[0] == pytest.approx(4.0 / 500.0)


# ---------------------------------------------------------------------------
# no fabricated data
# ---------------------------------------------------------------------------


class TestNoFabricatedData:
    def test_no_equity_reading_means_no_trade(self, tmp_path):
        """Legacy substituted a phantom $10,000 balance here."""
        s = StateStore(str(tmp_path / "empty.db"))
        m = rm.BillionaireRiskManager(store=s)
        result = m.assess_trade_risk(
            symbol="BTCUSDT", entry_price=50_000.0,
            position_size=0.001, stop_loss_price=49_500.0,
        )
        assert result["approved"] is False
        assert result["reason"] == "EQUITY_UNKNOWN"
        s.close()

    def test_market_regime_reports_unknown_rather_than_guessing(self, manager):
        assert manager.get_market_regime() == "UNKNOWN"

    def test_no_os_getenv_in_module(self):
        """Rule: config is the only reader of the environment."""
        path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "risk_management.py",
        )
        src = open(path, encoding="utf-8").read()
        code = [
            ln for ln in src.splitlines()
            if ("os.getenv" in ln or "os.environ" in ln)
            and not ln.lstrip().startswith("#") and "``" not in ln
        ]
        assert code == [], f"risk_management reads the environment directly: {code}"

    def test_store_refuses_non_finite_equity(self, store):
        for bad in (float("nan"), float("inf"), -1.0):
            with pytest.raises(Exception):
                store.update_equity(bad)


# ---------------------------------------------------------------------------
# structure: one implementation, no shadowing, no dead imports
# ---------------------------------------------------------------------------


class TestStructure:
    def test_no_duplicate_top_level_definitions(self):
        import collections
        path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "risk_management.py",
        )
        tree = ast.parse(open(path, encoding="utf-8").read())
        names = collections.Counter(
            n.name for n in tree.body
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        )
        assert {k: v for k, v in names.items() if v > 1} == {}

    def test_no_definitions_inside_except_handlers(self):
        """The legacy module hid its only leverage-aware risk math this way."""
        path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "risk_management.py",
        )
        tree = ast.parse(open(path, encoding="utf-8").read())
        trapped = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ExceptHandler):
                for sub in ast.walk(node):
                    if isinstance(sub, (ast.FunctionDef, ast.ClassDef)):
                        trapped.append((sub.name, sub.lineno))
        assert trapped == [], f"definitions trapped in except handlers: {trapped}"

    def test_gate_order_is_keyword_only(self):
        """N1: the legacy patch layer called this positionally, so every call
        raised TypeError and the whole chain returned GATE_EXCEPTION."""
        sig = inspect.signature(rm.BillionaireRiskManager.gate_order)
        positional = [
            p.name for p in sig.parameters.values()
            if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)
            and p.name != "self"
        ]
        assert positional == [], f"gate_order accepts positional args: {positional}"

    def test_gate_order_actually_executes(self, manager):
        """The regression that matters most: it must not return GATE_EXCEPTION."""
        d = manager.gate_order(**good_order())
        assert d.reason != "GATE_EXCEPTION"
        assert d.ok is True

    def test_compatibility_surface_present(self):
        for name in ("BillionaireRiskManager", "GateSet", "GateOutcome",
                     "V5EnhancedRiskManager", "validate_order", "risk_gate",
                     "kill_active"):
            assert hasattr(rm, name), f"risk_management.{name} disappeared"

    def test_v5_subclass_shares_the_base_state(self):
        """The legacy subclass never called super().__init__."""
        s = StateStore(":memory:")
        m = rm.V5EnhancedRiskManager(store=s)
        assert m.store is s
        assert m.max_position_size_pct > 0
        s.close()

    def test_limits_are_validated_at_construction(self, store):
        class BadCfg:
            MAX_POSITION_SIZE_PCT = 70.0     # percent-style: the 100x bug
            MAX_TOTAL_EXPOSURE_PCT = 0.1
            MAX_DAILY_LOSS_PCT = 0.02
            MAX_DRAWDOWN_PCT = 0.10
            STOP_LOSS_PCT = 0.02
            MAX_OPEN_POSITIONS = 2
            MIN_RISK_REWARD_RATIO = 2.0
            MIN_CONFIDENCE = 0.6
        with pytest.raises(ValueError):
            rm.BillionaireRiskManager(config=BadCfg(), store=store)

    def test_sub_one_risk_reward_config_is_rejected(self, store):
        class BadCfg:
            MAX_POSITION_SIZE_PCT = 0.02
            MAX_TOTAL_EXPOSURE_PCT = 0.1
            MAX_DAILY_LOSS_PCT = 0.02
            MAX_DRAWDOWN_PCT = 0.10
            STOP_LOSS_PCT = 0.02
            MAX_OPEN_POSITIONS = 2
            MIN_RISK_REWARD_RATIO = 0.5   # audit C7
            MIN_CONFIDENCE = 0.6
        with pytest.raises(ValueError):
            rm.BillionaireRiskManager(config=BadCfg(), store=store)


class TestDecisionJournal:
    def test_every_block_is_journalled_with_its_reason(self, manager, store):
        manager.gate_order(**good_order(stop_loss=0.0))
        entries = store.recent_decisions()
        assert entries
        assert entries[0]["decision"] == "BLOCK"
        assert entries[0]["reason"] == "NO_STOP_LOSS"

    def test_approvals_are_journalled_too(self, manager, store):
        manager.gate_order(**good_order())
        assert store.recent_decisions()[0]["decision"] == "ALLOW"
