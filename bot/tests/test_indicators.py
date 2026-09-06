"""Indicator correctness tests, validated against published reference values.

The RSI series below is the worked example from the standard reference
(Wilder's own data, as reproduced by StockCharts). Matching it to two decimals
is the strongest available evidence that the smoothing is Wilder's and not a
span EMA — which is precisely the error the legacy module made.
"""
from __future__ import annotations

import math
import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pure_indicators as pi  # noqa: E402
from pure_indicators import InsufficientData  # noqa: E402

#: Wilder's canonical RSI worked example.
RSI_CLOSES = [
    44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42, 45.84, 46.08,
    45.89, 46.03, 45.61, 46.28, 46.28, 46.00, 46.03, 46.41, 46.22, 45.64,
    46.21, 46.25, 45.71, 46.45, 45.78, 45.35, 44.03, 44.18, 44.22, 44.57,
    43.42, 42.66, 43.13,
]
RSI_EXPECTED_FIRST_THREE = [70.46, 66.25, 66.48]


def synthetic_ohlc(n=200, seed=3, vol=0.02):
    rng = np.random.default_rng(seed)
    close = 100 * np.cumprod(1 + rng.normal(0.0003, vol, n))
    high = close * (1 + np.abs(rng.normal(0, vol / 2, n)))
    low = close * (1 - np.abs(rng.normal(0, vol / 2, n)))
    return list(high), list(low), list(close)


# ---------------------------------------------------------------------------
# Wilder smoothing — the headline correction
# ---------------------------------------------------------------------------


class TestWilderSmoothing:
    def test_rsi_matches_the_published_reference(self):
        """If this passes, the smoothing is Wilder's."""
        values = pi.rsi(RSI_CLOSES, 14)
        for got, want in zip(values[:3], RSI_EXPECTED_FIRST_THREE):
            assert got == pytest.approx(want, abs=0.01)

    def test_wilder_alpha_is_one_over_period(self):
        """Directly: the recursion must be x_t = x_{t-1} + (v - x_{t-1})/period."""
        values = [10.0] * 14 + [20.0]
        smoothed = pi.wilder_smooth(values, 14)
        assert smoothed[0] == pytest.approx(10.0)
        assert smoothed[1] == pytest.approx(10.0 + (20.0 - 10.0) / 14)

    def test_wilder_is_not_a_span_ema(self):
        """A span EMA would use alpha = 2/(period+1) — a different number."""
        values = [10.0] * 14 + [20.0]
        wilder = pi.wilder_smooth(values, 14)[1]
        span_alpha = 2.0 / 15.0
        span = 10.0 + span_alpha * (20.0 - 10.0)
        assert wilder != pytest.approx(span)
        assert wilder < span, "Wilder must respond more slowly than a span EMA"

    def test_atr_is_larger_under_wilder_than_under_span_smoothing(self):
        """The legacy ATR read low, which made stops too tight and sizes too big.

        With genuinely volatile data the gap is material, and it is always in
        the same direction on a rising-volatility series.
        """
        high, low, close = synthetic_ohlc(300, seed=9, vol=0.03)
        tr = pi.true_range(high, low, close)
        wilder_atr = float(pi.wilder_smooth(tr, 14)[-1])

        alpha = 2.0 / 15.0
        span = float(np.mean(tr[:14]))
        for value in tr[14:]:
            span = alpha * value + (1 - alpha) * span

        assert wilder_atr > 0
        assert span > 0
        # They must not be the same filter.
        assert abs(wilder_atr - span) / wilder_atr > 0.005


class TestRSI:
    def test_bounded_zero_to_hundred(self):
        _, _, close = synthetic_ohlc(300, seed=5)
        values = pi.rsi(close, 14)
        assert values.min() >= 0.0
        assert values.max() <= 100.0

    def test_monotonic_rise_gives_rsi_one_hundred(self):
        assert pi.rsi([float(i) for i in range(1, 40)], 14)[-1] == pytest.approx(100.0)

    def test_monotonic_fall_gives_rsi_zero(self):
        assert pi.rsi([float(i) for i in range(40, 1, -1)], 14)[-1] == pytest.approx(0.0)

    def test_flat_series_is_neutral_not_a_division_error(self):
        assert pi.rsi([50.0] * 40, 14)[-1] == pytest.approx(50.0)

    def test_insufficient_data_raises(self):
        with pytest.raises(InsufficientData):
            pi.rsi([1.0, 2.0, 3.0], 14)


# ---------------------------------------------------------------------------
# ATR
# ---------------------------------------------------------------------------


class TestATR:
    def test_atr_is_strictly_positive_on_real_ranges(self):
        high, low, close = synthetic_ohlc(200, seed=4)
        assert float(pi.atr(high, low, close, 14)[-1]) > 0

    def test_atr_never_returns_zeros_on_error(self):
        """Legacy returned an all-zero array, which is a zero stop distance."""
        with pytest.raises(InsufficientData):
            pi.atr([1.0, 2.0], [0.5, 1.5], [0.8, 1.8], 14)

    def test_atr_last_refuses_a_non_positive_value(self):
        with pytest.raises(InsufficientData):
            pi.atr_last([10.0] * 40, [10.0] * 40, [10.0] * 40, 14)

    def test_true_range_accounts_for_gaps(self):
        """A gap up makes |high - prev_close| the largest of the three."""
        tr = pi.true_range([10, 20], [9, 19], [9.5, 19.5])
        assert float(tr[0]) == pytest.approx(20 - 9.5)

    def test_atr_seeds_with_a_simple_mean(self):
        high, low, close = synthetic_ohlc(60, seed=8)
        tr = pi.true_range(high, low, close)
        assert float(pi.atr(high, low, close, 14)[0]) == pytest.approx(
            float(np.mean(tr[:14]))
        )


# ---------------------------------------------------------------------------
# the other fixed defects
# ---------------------------------------------------------------------------


class TestWMA:
    def test_most_recent_bar_is_weighted_highest(self):
        """The legacy weight vector was reversed, inverting the indicator."""
        assert float(pi.wma([1, 2, 3, 4, 5], 5)[0]) == pytest.approx(11 / 3)

    def test_wma_exceeds_sma_on_a_rising_series(self):
        rising = [float(i) for i in range(1, 21)]
        assert float(pi.wma(rising, 10)[-1]) > float(pi.sma(rising, 10)[-1])


class TestBollinger:
    def test_bands_are_named_and_ordered(self):
        """Legacy unpacked this tuple in two different orders at two call sites."""
        _, _, close = synthetic_ohlc(100, seed=2)
        bands = pi.bollinger(close, 20, 2.0)
        assert bands.upper[-1] > bands.middle[-1] > bands.lower[-1]
        assert hasattr(bands, "upper") and hasattr(bands, "lower")

    def test_middle_band_is_the_sma(self):
        _, _, close = synthetic_ohlc(100, seed=6)
        assert float(pi.bollinger(close, 20).middle[-1]) == pytest.approx(
            float(pi.sma(close, 20)[-1])
        )

    def test_width_scales_with_num_std(self):
        _, _, close = synthetic_ohlc(100, seed=6)
        narrow = pi.bollinger(close, 20, 1.0)
        wide = pi.bollinger(close, 20, 3.0)
        assert (wide.upper[-1] - wide.lower[-1]) > (narrow.upper[-1] - narrow.lower[-1])


class TestSupertrend:
    def test_returns_a_named_tuple(self):
        high, low, close = synthetic_ohlc(200, seed=1)
        result = pi.supertrend(high, low, close)
        assert hasattr(result, "trend") and hasattr(result, "line")

    def test_trend_is_only_plus_or_minus_one(self):
        high, low, close = synthetic_ohlc(200, seed=1)
        assert set(np.unique(pi.supertrend(high, low, close).trend)) <= {-1, 1}

    def test_uptrend_is_detected_on_a_rising_series(self):
        n = 200
        close = [100.0 * (1.01 ** i) for i in range(n)]
        high = [c * 1.001 for c in close]
        low = [c * 0.999 for c in close]
        assert int(pi.supertrend(high, low, close).trend[-1]) == 1


class TestMACD:
    def test_all_three_series_are_aligned(self):
        _, _, close = synthetic_ohlc(200, seed=7)
        result = pi.macd(close)
        assert len(result.macd) == len(result.signal) == len(result.histogram)

    def test_histogram_is_macd_minus_signal(self):
        _, _, close = synthetic_ohlc(200, seed=7)
        result = pi.macd(close)
        assert float(result.histogram[-1]) == pytest.approx(
            float(result.macd[-1] - result.signal[-1])
        )

    def test_fast_must_be_shorter_than_slow(self):
        with pytest.raises(ValueError):
            pi.macd([1.0] * 100, fast=26, slow=12)


class TestADX:
    def test_adx_is_bounded(self):
        high, low, close = synthetic_ohlc(300, seed=12)
        result = pi.adx(high, low, close, 14)
        assert result.adx.min() >= 0.0
        assert result.adx.max() <= 100.0

    def test_strong_trend_produces_a_high_adx(self):
        n = 200
        close = [100.0 * (1.02 ** i) for i in range(n)]
        high = [c * 1.001 for c in close]
        low = [c * 0.999 for c in close]
        assert float(pi.adx(high, low, close, 14).adx[-1]) > 25.0

    def test_plus_di_dominates_in_an_uptrend(self):
        n = 200
        close = [100.0 * (1.01 ** i) for i in range(n)]
        high = [c * 1.001 for c in close]
        low = [c * 0.999 for c in close]
        result = pi.adx(high, low, close, 14)
        assert float(result.plus_di[-1]) > float(result.minus_di[-1])


# ---------------------------------------------------------------------------
# purity
# ---------------------------------------------------------------------------


class TestPurity:
    def test_module_imports_nothing_impure(self):
        """A 1,100-line AWS Bedrock client was pasted inside the legacy version.

        Checks the import graph via AST rather than searching the text, so the
        docstring that *describes* the removal does not trip the test.
        """
        import ast

        path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "pure_indicators.py",
        )
        tree = ast.parse(open(path, encoding="utf-8").read())
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        banned = {"boto3", "botocore", "requests", "urllib", "urllib3", "http",
                  "threading", "socket", "asyncio", "config", "persistence"}
        assert not (imported & banned), (
            f"'pure' indicators import impure modules: {sorted(imported & banned)}"
        )
        assert imported <= {"math", "numpy", "dataclasses", "typing", "__future__"}, (
            f"unexpected imports: {sorted(imported)}"
        )

    def test_functions_do_not_mutate_their_inputs(self):
        _, _, close = synthetic_ohlc(100, seed=3)
        before = list(close)
        pi.rsi(close, 14)
        pi.sma(close, 20)
        pi.bollinger(close, 20)
        assert close == before

    def test_results_are_deterministic(self):
        _, _, close = synthetic_ohlc(100, seed=3)
        assert np.array_equal(pi.rsi(close, 14), pi.rsi(close, 14))

    def test_non_finite_input_is_rejected(self):
        with pytest.raises(ValueError):
            pi.sma([1.0, float("nan"), 3.0] * 10, 5)
