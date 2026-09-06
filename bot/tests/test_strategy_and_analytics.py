"""Strategy-layer and analytics contract tests.

The properties defended here are the ones that made the legacy signal output
unusable as an input to anything: unbounded confidence, neutral mapping to BUY,
errors *increasing* confluence, and fee-blind metrics feeding Kelly.
"""
from __future__ import annotations

import ast
import math
import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import performance_analytics as pa  # noqa: E402
import technical_analysis as ta  # noqa: E402
from persistence import StateStore, TradeRecord, utc_now_epoch  # noqa: E402



def code_only(filename: str) -> str:
    """Source with all docstrings removed.

    Every "is X gone?" check must scan CODE, not prose. The modules deliberately
    document what was removed and why, so a naive text search finds the very
    words it is asserting the absence of — which makes the test assert the
    opposite of what it means.
    """
    path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), filename
    )
    tree = ast.parse(open(path, encoding="utf-8").read())
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef)):
            body = getattr(node, "body", [])
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                body.pop(0)
    return ast.unparse(tree)


def make_candles(n=200, seed=3, drift=0.001, noise=0.005, timeframe="60"):
    rng = np.random.default_rng(seed)
    close = 100 * np.cumprod(1 + rng.normal(drift, noise, n))
    high = close * (1 + np.abs(rng.normal(0, noise / 2, n)))
    low = close * (1 - np.abs(rng.normal(0, noise / 2, n)))
    opens = np.concatenate([[close[0]], close[:-1]])
    return ta.Candles(
        tuple(float(x) for x in opens), tuple(float(x) for x in high),
        tuple(float(x) for x in low), tuple(float(x) for x in close),
        tuple(1000.0 for _ in range(n)), timeframe,
    )


class Cfg:
    MIN_CONFIDENCE = 0.60
    MIN_RISK_REWARD_RATIO = 2.0
    STOP_LOSS_PCT = 0.02
    STOP_ATR_MULTIPLE = 2.0
    MIN_COMPONENT_AGREEMENT = 0.6
    INDICATOR_THRESHOLDS: dict = {}


# ---------------------------------------------------------------------------
# candle hygiene
# ---------------------------------------------------------------------------


class TestCandles:
    def test_ragged_series_is_rejected(self):
        with pytest.raises(ValueError):
            ta.Candles((1.0, 2.0), (1.0,), (1.0, 2.0), (1.0, 2.0), (1.0, 2.0))

    def test_high_below_low_is_rejected(self):
        with pytest.raises(ValueError):
            ta.Candles((1.0,), (0.5,), (2.0,), (1.0,), (1.0,))

    def test_non_positive_price_is_rejected(self):
        with pytest.raises(ValueError):
            ta.Candles((1.0,), (1.0,), (1.0,), (0.0,), (1.0,))

    def test_klines_conversion_preserves_order(self):
        rows = [[str(i), "1", "2", "0.5", str(1.0 + i), "10", "0"] for i in range(5)]
        candles = ta.candles_from_klines(rows, "60")
        assert candles.close == (1.0, 2.0, 3.0, 4.0, 5.0)

    def test_empty_klines_raises(self):
        with pytest.raises(Exception):
            ta.candles_from_klines([])


# ---------------------------------------------------------------------------
# confidence is a probability
# ---------------------------------------------------------------------------


class TestConfidenceIsAProbability:
    def test_signal_rejects_confidence_above_one(self):
        """Audit C6: legacy confidence routinely exceeded 2.0."""
        with pytest.raises(ValueError):
            ta.Signal(symbol="BTCUSDT", action="BUY", confidence=1.5)

    def test_signal_rejects_negative_confidence(self):
        with pytest.raises(ValueError):
            ta.Signal(symbol="BTCUSDT", action="BUY", confidence=-0.1)

    def test_signal_rejects_nan_confidence(self):
        with pytest.raises(ValueError):
            ta.Signal(symbol="BTCUSDT", action="BUY", confidence=float("nan"))

    @pytest.mark.parametrize("seed", range(12))
    def test_confidence_is_always_in_range_across_random_markets(self, seed):
        """Property test: no market shape may produce an out-of-range value."""
        analyser = ta.TechnicalAnalysis(config=Cfg())
        candles = make_candles(240, seed=seed, drift=(seed - 6) * 0.001,
                               noise=0.002 + seed * 0.002)
        signal = analyser.analyse("BTCUSDT", candles)
        assert 0.0 <= signal.confidence <= 1.0

    def test_no_multiplier_or_booster_in_code(self):
        """Legacy stacked volume x regime x a 2.5x booster onto confidence."""
        code = code_only("technical_analysis.py")
        for banned in ("BOOST", "booster", "CONFIDENCE_MULTIPLIER",
                       "SIGNAL_STRENGTH_MULTIPLIER", "AGGRESSIVE_MULTIPLIER"):
            assert banned not in code, f"{banned} present in code"


# ---------------------------------------------------------------------------
# HOLD is first-class
# ---------------------------------------------------------------------------


class TestHoldIsFirstClass:
    def test_default_signal_is_hold(self):
        assert ta.Signal(symbol="X").action == "HOLD"
        assert ta.Signal(symbol="X").is_actionable is False

    def test_short_history_holds(self):
        analyser = ta.TechnicalAnalysis(config=Cfg())
        signal = analyser.analyse("BTCUSDT", make_candles(30))
        assert signal.action == "HOLD"
        assert signal.reason.startswith("INSUFFICIENT_HISTORY")

    def test_flat_market_holds(self):
        """Legacy mapped a score of exactly 0 to BUY at confidence >= 0.55."""
        analyser = ta.TechnicalAnalysis(config=Cfg())
        flat = make_candles(240, seed=1, drift=0.0, noise=0.0001)
        assert analyser.analyse("BTCUSDT", flat).action == "HOLD"

    def test_a_crashing_component_yields_hold_not_a_direction(self, monkeypatch):
        analyser = ta.TechnicalAnalysis(config=Cfg())
        monkeypatch.setattr(
            analyser, "_vote_rsi",
            lambda c: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        signal = analyser.analyse("BTCUSDT", make_candles(240))
        assert signal.action == "HOLD"
        assert signal.confidence == 0.0

    def test_no_long_bias_on_symmetric_noise(self):
        """Across many symmetric random markets, BUY and SELL must not be lopsided."""
        analyser = ta.TechnicalAnalysis(config=Cfg())
        actions = [
            analyser.analyse("X", make_candles(240, seed=s, drift=0.0, noise=0.006)).action
            for s in range(40)
        ]
        buys = actions.count("BUY")
        sells = actions.count("SELL")
        assert abs(buys - sells) <= max(3, (buys + sells) // 2), (
            f"long bias detected: {buys} BUY vs {sells} SELL"
        )


# ---------------------------------------------------------------------------
# abstentions reduce confluence
# ---------------------------------------------------------------------------


class TestAbstentionsCountAgainst:
    def test_failed_component_stays_in_the_denominator(self):
        """Legacy removed failing components, so one survivor gave confluence 1.0."""
        votes = (
            ta.ComponentVote("a", 1, 1.0, ok=True),
            ta.ComponentVote("b", 0, 0.0, ok=False),
            ta.ComponentVote("c", 0, 0.0, ok=False),
            ta.ComponentVote("d", 0, 0.0, ok=False),
        )
        signal = ta.Signal(symbol="X", action="BUY", confidence=0.5, votes=votes)
        assert signal.agreement == 0.25, "abstentions were dropped from the denominator"

    def test_abstaining_vote_scores_zero(self):
        assert ta.ComponentVote("x", 1, 1.0, ok=False).score == 0.0

    def test_too_few_healthy_components_holds(self, monkeypatch):
        analyser = ta.TechnicalAnalysis(config=Cfg())
        for name in ("_vote_rsi", "_vote_macd", "_vote_bollinger"):
            monkeypatch.setattr(
                analyser, name,
                lambda c, n=name: ta.ComponentVote(n, ok=False, detail="forced"),
            )
        signal = analyser.analyse("BTCUSDT", make_candles(240))
        assert signal.action == "HOLD"
        assert "HEALTHY" in signal.reason

    def test_vote_rejects_out_of_range_strength(self):
        with pytest.raises(ValueError):
            ta.ComponentVote("x", 1, 1.5)

    def test_vote_rejects_invalid_direction(self):
        with pytest.raises(ValueError):
            ta.ComponentVote("x", 2, 0.5)


# ---------------------------------------------------------------------------
# levels
# ---------------------------------------------------------------------------


class TestLevels:
    def test_a_buy_signal_has_a_stop_below_entry(self):
        analyser = ta.TechnicalAnalysis(config=Cfg())
        analyser.min_confidence = 0.0
        analyser.min_agreement = 0.0
        for seed in range(20):
            signal = analyser.analyse("X", make_candles(240, seed=seed, drift=0.002))
            if signal.action == "BUY":
                assert signal.stop_price < signal.entry_price
                assert all(tp > signal.entry_price for tp, _ in signal.take_profits)
                return
        pytest.skip("no BUY produced in the sample")

    def test_a_sell_signal_has_a_stop_above_entry(self):
        analyser = ta.TechnicalAnalysis(config=Cfg())
        analyser.min_confidence = 0.0
        analyser.min_agreement = 0.0
        for seed in range(20):
            signal = analyser.analyse("X", make_candles(240, seed=seed, drift=-0.002))
            if signal.action == "SELL":
                assert signal.stop_price > signal.entry_price
                assert all(tp < signal.entry_price for tp, _ in signal.take_profits)
                return
        pytest.skip("no SELL produced in the sample")

    def test_take_profit_fractions_sum_to_one(self):
        analyser = ta.TechnicalAnalysis(config=Cfg())
        analyser.min_confidence = 0.0
        analyser.min_agreement = 0.0
        for seed in range(20):
            signal = analyser.analyse("X", make_candles(240, seed=seed, drift=0.002))
            if signal.is_actionable:
                assert sum(f for _, f in signal.take_profits) == pytest.approx(1.0)
                return
        pytest.skip("no actionable signal produced")

    def test_stop_distance_is_capped(self):
        """A volatility spike must not produce an unbounded stop distance."""
        analyser = ta.TechnicalAnalysis(config=Cfg())
        analyser.min_confidence = 0.0
        analyser.min_agreement = 0.0
        for seed in range(20):
            candles = make_candles(240, seed=seed, drift=0.003, noise=0.05)
            signal = analyser.analyse("X", candles)
            if signal.is_actionable:
                distance = abs(signal.entry_price - signal.stop_price)
                assert distance <= signal.entry_price * analyser.max_stop_fraction * 1.001


class TestRegime:
    def test_unknown_when_data_is_short(self):
        assert ta.detect_regime(make_candles(20)) == ta.Regime.UNKNOWN

    def test_strong_uptrend_is_detected(self):
        n = 220
        close = [100.0 * (1.01 ** i) for i in range(n)]
        candles = ta.Candles(
            tuple(close), tuple(c * 1.001 for c in close),
            tuple(c * 0.999 for c in close), tuple(close),
            tuple(1000.0 for _ in range(n)),
        )
        assert ta.detect_regime(candles) == ta.Regime.TRENDING_UP

    def test_a_buy_against_a_downtrend_is_dropped(self):
        analyser = ta.TechnicalAnalysis(config=Cfg())
        analyser.min_confidence = 0.0
        analyser.min_agreement = 0.0
        n = 220
        close = [100.0 * (0.99 ** i) for i in range(n)]
        candles = ta.Candles(
            tuple(close), tuple(c * 1.001 for c in close),
            tuple(c * 0.999 for c in close), tuple(close),
            tuple(1000.0 for _ in range(n)),
        )
        signal = analyser.analyse("X", candles)
        assert signal.action != "BUY"


# ---------------------------------------------------------------------------
# analytics
# ---------------------------------------------------------------------------


@pytest.fixture()
def store(tmp_path):
    s = StateStore(str(tmp_path / "s.db"))
    s.update_equity(10_000.0)
    yield s
    s.close()


def add_trade(store, net, gross=None, fee=0.5, day_offset=0):
    gross = net + 2 * fee if gross is None else gross
    ts = utc_now_epoch() - day_offset * 86400
    store.record_trade(TradeRecord(
        symbol="BTCUSDT", side="Buy", qty=0.01, entry_price=50_000.0,
        exit_price=50_000.0 + net, gross_pnl=gross,
        entry_fee=fee, exit_fee=fee,
        opened_epoch=ts - 60, closed_epoch=ts,
    ))


class TestAnalytics:
    def test_no_trades_reports_insufficient_not_zero(self, store):
        report = pa.PerformanceAnalytics(store).report()
        assert report.insufficient_data is True
        assert report.win_rate is None, "a win rate of None is not a win rate of 0"

    def test_ratios_are_none_below_the_sample_floor(self, store):
        for _ in range(10):
            add_trade(store, 5.0)
        report = pa.PerformanceAnalytics(store).report(10_000.0)
        assert report.insufficient_data is True
        assert report.sharpe is None
        assert report.profit_factor is None

    def test_win_rate_is_fee_aware(self, store):
        """A gross win that loses after fees is a LOSS. Legacy counted gross."""
        for _ in range(40):
            add_trade(store, net=-0.1, gross=0.9, fee=0.5)
        report = pa.PerformanceAnalytics(store).report(10_000.0)
        assert report.gross_pnl > 0
        assert report.net_pnl < 0
        assert report.win_rate == 0.0

    def test_summary_never_claims_more_than_it_knows(self, store):
        report = pa.PerformanceAnalytics(store).report()
        assert "insufficient" in report.summary().lower()

    def test_sharpe_uses_daily_aggregation(self, store):
        """Legacy annualised PER-TRADE returns by x365 — meaningless."""
        for day in range(40):
            add_trade(store, 5.0 if day % 2 else -3.0, day_offset=day)
        report = pa.PerformanceAnalytics(store).report(10_000.0)
        assert report.days >= 30
        assert report.sharpe is not None
        # Sanity: with this alternating series the annualised Sharpe must be
        # finite and of plausible magnitude, not an artefact of x365 on trades.
        assert abs(report.sharpe) < 100

    def test_sharpe_is_none_without_starting_equity(self, store):
        for day in range(40):
            add_trade(store, 1.0, day_offset=day)
        assert pa.PerformanceAnalytics(store).report().sharpe is None

    def test_profit_factor_is_none_not_infinite_without_losses(self, store):
        for day in range(40):
            add_trade(store, 5.0, day_offset=day)
        report = pa.PerformanceAnalytics(store).report(10_000.0)
        assert report.profit_factor is None
        assert any("undefined" in n for n in report.notes)

    def test_max_drawdown_is_a_fraction(self, store):
        for day in range(40):
            add_trade(store, -50.0 if day < 20 else 10.0, day_offset=40 - day)
        report = pa.PerformanceAnalytics(store).report(10_000.0)
        assert report.max_drawdown is not None
        assert 0.0 <= report.max_drawdown <= 1.0

    def test_no_deadlock_on_repeated_summaries(self, store):
        """The legacy get_summary() deadlocked on its own non-reentrant lock."""
        for _ in range(40):
            add_trade(store, 1.0)
        analytics = pa.PerformanceAnalytics(store)
        for _ in range(50):
            analytics.report(10_000.0)
        add_trade(store, 1.0)          # recording still works afterwards
        assert analytics.report(10_000.0).trades == 41

    def test_no_random_validation_generator(self):
        """Legacy produced a 'validation report' from random.Random(42)."""
        path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "performance_analytics.py",
        )
        tree = ast.parse(open(path, encoding="utf-8").read())
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        assert "random" not in imported

    def test_enhanced_aws_integration_is_gone(self):
        """Three modules imported this class for alerting; it never existed."""
        assert "EnhancedAWSIntegration" not in code_only("performance_analytics.py")

    def test_size_throttle_defaults_to_one_without_data(self, store):
        assert pa.PerformanceAnalytics(store).size_throttle() == 1.0

    def test_size_throttle_is_bounded_and_can_only_reduce(self, store):
        for day in range(80):
            add_trade(store, 5.0 if day > 40 else -5.0, day_offset=80 - day)
        throttle = pa.PerformanceAnalytics(store).size_throttle(window=20)
        assert 0.0 <= throttle <= 1.0
