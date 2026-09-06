"""pure_indicators.py — correct indicator math, no I/O, no state.

WHAT REPLACED WHAT
==================
The as-received module (3,470 lines, retained at
``_dead/pure_indicators_legacy.py``) was not "pure" at all: it contained a
1,100-line AWS Bedrock LLM client pasted inside it, whose uncapped output fed
signal confidence. That client is gone. This module now does exactly what its
name says — take arrays of closed-candle prices, return numbers — with no
network, no threads, no configuration and no global state.

THE MATH ERRORS THAT WERE FIXED
-------------------------------
1. **Wilder smoothing, not span-EMA.** RSI, ATR and ADX are defined by Wilder
   with ``alpha = 1/period``. The legacy code used pandas' ``ewm(span=period)``,
   which is ``alpha = 2/(period+1)`` — a materially different filter:

   * RSI(14) with span smoothing behaves like an RSI of roughly period 7.5, so
     it crosses 30/70 far more often than the thresholds assume;
   * ATR(14) comes out about **36% too small**, which makes every ATR-derived
     stop too tight and — because size is risk/stop-distance — every
     ATR-scaled position correspondingly too large.

2. **ATR seeding.** Wilder's ATR seeds with a simple mean of the first ``n``
   true ranges and then smooths recursively. The legacy version applied the
   filter from the first bar, which biases the whole series.

3. **ATR returned all-zeros on error.** A zero ATR means either an
   instantly-triggered stop (distance 0) or a division by zero in sizing. This
   module raises instead.

4. **Inverted WMA weights.** The legacy weighting gave the *oldest* bar the
   largest weight, which inverts the indicator's meaning.

5. **Bollinger and Supertrend tuples were unpacked in the wrong order** at two
   call sites each. Both now return named tuples, so a mis-ordered unpack is a
   ``AttributeError`` at the call site rather than silently swapped values.

CONVENTIONS
-----------
* Every function takes **closed candles only**. Nothing here knows about time,
  so it cannot drop an unclosed bar itself — that happens once, in
  ``BybitClient.get_klines``, and is asserted by its tests.
* Insufficient data raises ``InsufficientData``. It never returns a padded,
  zero-filled or repeated-value array, because those flow downstream as if they
  were real measurements.
* Every function is deterministic and side-effect free.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import List, NamedTuple, Sequence, Tuple

import numpy as np

__all__ = [
    "InsufficientData",
    "sma",
    "ema",
    "wma",
    "wilder_smooth",
    "true_range",
    "atr",
    "rsi",
    "adx",
    "macd",
    "bollinger",
    "supertrend",
    "MACDResult",
    "BollingerResult",
    "SupertrendResult",
    "ADXResult",
]


class InsufficientData(ValueError):
    """Not enough closed candles to compute the indicator.

    Raised rather than returning a padded array. A zero-filled ATR became a
    zero stop distance in the legacy sizing path, which is either an instant
    stop-out or a division by zero.
    """


def _as_array(values: Sequence[float], name: str = "values") -> np.ndarray:
    arr = np.asarray(list(values), dtype=float)
    if arr.ndim != 1:
        raise ValueError(f"{name} must be one-dimensional")
    if arr.size and not np.all(np.isfinite(arr)):
        raise ValueError(f"{name} contains non-finite entries")
    return arr


def _require(arr: np.ndarray, n: int, what: str) -> None:
    if arr.size < n:
        raise InsufficientData(
            f"{what} needs at least {n} closed candles, got {arr.size}"
        )


# ---------------------------------------------------------------------------
# moving averages
# ---------------------------------------------------------------------------


def sma(values: Sequence[float], period: int) -> np.ndarray:
    """Simple moving average. Returns ``len(values) - period + 1`` points."""
    arr = _as_array(values)
    if period < 1:
        raise ValueError("period must be >= 1")
    _require(arr, period, f"SMA({period})")
    weights = np.ones(period) / period
    return np.convolve(arr, weights, mode="valid")


def ema(values: Sequence[float], period: int) -> np.ndarray:
    """Exponential moving average with the standard ``alpha = 2/(period+1)``.

    Seeded with the SMA of the first ``period`` values, which is the usual
    convention and avoids the startup bias of seeding with a single point.

    Note this is the *span* EMA. It is correct for MACD, and **wrong** for
    RSI/ATR/ADX — those use :func:`wilder_smooth`. Confusing the two is the
    single most consequential math error in the legacy module.
    """
    arr = _as_array(values)
    if period < 1:
        raise ValueError("period must be >= 1")
    _require(arr, period, f"EMA({period})")
    alpha = 2.0 / (period + 1.0)
    out = np.empty(arr.size - period + 1, dtype=float)
    out[0] = arr[:period].mean()
    for i in range(1, out.size):
        out[i] = alpha * arr[period + i - 1] + (1.0 - alpha) * out[i - 1]
    return out


def wma(values: Sequence[float], period: int) -> np.ndarray:
    """Linearly weighted moving average — **most recent bar weighted highest**.

    The legacy implementation had the weight vector reversed, so the oldest bar
    in the window dominated. That inverts what the indicator means.
    """
    arr = _as_array(values)
    if period < 1:
        raise ValueError("period must be >= 1")
    _require(arr, period, f"WMA({period})")
    weights = np.arange(1, period + 1, dtype=float)   # 1 = oldest, period = newest
    weights /= weights.sum()
    # np.convolve reverses the kernel, so flip to keep "newest gets the most".
    return np.convolve(arr, weights[::-1], mode="valid")


def wilder_smooth(values: Sequence[float], period: int) -> np.ndarray:
    """Wilder's smoothing: ``alpha = 1/period``, seeded with a simple mean.

    This is the filter RSI, ATR and ADX are *defined* with. Substituting a span
    EMA (``alpha = 2/(period+1)``) makes the indicator respond roughly twice as
    fast, which is why the legacy RSI(14) behaved like an RSI(7.5) and the
    legacy ATR(14) read about 36% low.
    """
    arr = _as_array(values)
    if period < 1:
        raise ValueError("period must be >= 1")
    _require(arr, period, f"Wilder smoothing({period})")
    out = np.empty(arr.size - period + 1, dtype=float)
    out[0] = arr[:period].mean()
    for i in range(1, out.size):
        out[i] = out[i - 1] + (arr[period + i - 1] - out[i - 1]) / period
    return out


# ---------------------------------------------------------------------------
# volatility
# ---------------------------------------------------------------------------


def true_range(
    high: Sequence[float], low: Sequence[float], close: Sequence[float]
) -> np.ndarray:
    """True range. Length is ``len(close) - 1`` (the first bar has no prior close)."""
    h, l, c = _as_array(high, "high"), _as_array(low, "low"), _as_array(close, "close")
    if not (h.size == l.size == c.size):
        raise ValueError("high, low and close must be the same length")
    _require(c, 2, "true range")
    prev_close = c[:-1]
    return np.maximum.reduce([
        h[1:] - l[1:],
        np.abs(h[1:] - prev_close),
        np.abs(l[1:] - prev_close),
    ])


def atr(
    high: Sequence[float],
    low: Sequence[float],
    close: Sequence[float],
    period: int = 14,
) -> np.ndarray:
    """Average true range, Wilder-smoothed and Wilder-seeded.

    Raises on insufficient data rather than returning zeros. The legacy version
    returned an all-zero array on any error, and a zero ATR is not a small stop
    — it is *no* stop, or a division by zero in sizing.
    """
    tr = true_range(high, low, close)
    if tr.size < period:
        raise InsufficientData(
            f"ATR({period}) needs at least {period + 1} closed candles, "
            f"got {len(list(close))}"
        )
    return wilder_smooth(tr, period)


def atr_last(
    high: Sequence[float],
    low: Sequence[float],
    close: Sequence[float],
    period: int = 14,
) -> float:
    """Most recent ATR value. Guaranteed positive or an exception."""
    value = float(atr(high, low, close, period)[-1])
    if not math.isfinite(value) or value <= 0.0:
        raise InsufficientData(f"ATR({period}) resolved to {value}; refusing to use it")
    return value


# ---------------------------------------------------------------------------
# momentum
# ---------------------------------------------------------------------------


def rsi(close: Sequence[float], period: int = 14) -> np.ndarray:
    """Wilder's RSI, in [0, 100].

    Gains and losses are smoothed with :func:`wilder_smooth`. A zero average
    loss yields 100 (not a division by zero).
    """
    c = _as_array(close, "close")
    _require(c, period + 1, f"RSI({period})")
    delta = np.diff(c)
    gains = np.where(delta > 0.0, delta, 0.0)
    losses = np.where(delta < 0.0, -delta, 0.0)
    avg_gain = wilder_smooth(gains, period)
    avg_loss = wilder_smooth(losses, period)
    out = np.empty_like(avg_gain)
    for i in range(out.size):
        if avg_loss[i] == 0.0:
            out[i] = 100.0 if avg_gain[i] > 0.0 else 50.0
        else:
            rs = avg_gain[i] / avg_loss[i]
            out[i] = 100.0 - (100.0 / (1.0 + rs))
    return out


class MACDResult(NamedTuple):
    """Named so a mis-ordered unpack fails loudly instead of swapping values."""

    macd: np.ndarray
    signal: np.ndarray
    histogram: np.ndarray


def macd(
    close: Sequence[float], fast: int = 12, slow: int = 26, signal: int = 9
) -> MACDResult:
    """MACD using span EMAs, which is how MACD is defined.

    All three series are aligned to the shortest, so ``histogram[i]`` always
    corresponds to ``macd[i]`` and ``signal[i]``.
    """
    if fast >= slow:
        raise ValueError("fast period must be shorter than slow")
    c = _as_array(close, "close")
    _require(c, slow + signal, f"MACD({fast},{slow},{signal})")
    fast_ema = ema(c, fast)
    slow_ema = ema(c, slow)
    fast_ema = fast_ema[-slow_ema.size:]          # align to the shorter series
    macd_line = fast_ema - slow_ema
    signal_line = ema(macd_line, signal)
    macd_line = macd_line[-signal_line.size:]
    return MACDResult(macd_line, signal_line, macd_line - signal_line)


class ADXResult(NamedTuple):
    adx: np.ndarray
    plus_di: np.ndarray
    minus_di: np.ndarray


def adx(
    high: Sequence[float],
    low: Sequence[float],
    close: Sequence[float],
    period: int = 14,
) -> ADXResult:
    """Wilder's ADX with +DI / -DI, all Wilder-smoothed."""
    h, l, c = _as_array(high, "high"), _as_array(low, "low"), _as_array(close, "close")
    if not (h.size == l.size == c.size):
        raise ValueError("high, low and close must be the same length")
    _require(c, 2 * period + 1, f"ADX({period})")

    up_move = h[1:] - h[:-1]
    down_move = l[:-1] - l[1:]
    plus_dm = np.where((up_move > down_move) & (up_move > 0.0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0.0), down_move, 0.0)

    tr = true_range(h, l, c)
    atr_series = wilder_smooth(tr, period)
    plus_di = 100.0 * wilder_smooth(plus_dm, period) / np.where(
        atr_series == 0.0, np.nan, atr_series
    )
    minus_di = 100.0 * wilder_smooth(minus_dm, period) / np.where(
        atr_series == 0.0, np.nan, atr_series
    )
    denominator = plus_di + minus_di
    dx = 100.0 * np.abs(plus_di - minus_di) / np.where(
        denominator == 0.0, np.nan, denominator
    )
    dx = np.nan_to_num(dx, nan=0.0)
    adx_series = wilder_smooth(dx, period)
    return ADXResult(
        adx_series,
        np.nan_to_num(plus_di, nan=0.0)[-adx_series.size:],
        np.nan_to_num(minus_di, nan=0.0)[-adx_series.size:],
    )


# ---------------------------------------------------------------------------
# bands
# ---------------------------------------------------------------------------


class BollingerResult(NamedTuple):
    """Named. The legacy tuple was unpacked ``(lower, mid, upper)`` at one call
    site and ``(upper, mid, lower)`` at another — silently inverting the bands."""

    upper: np.ndarray
    middle: np.ndarray
    lower: np.ndarray


def bollinger(
    close: Sequence[float], period: int = 20, num_std: float = 2.0
) -> BollingerResult:
    """Bollinger bands using the **population** standard deviation (ddof=0)."""
    c = _as_array(close, "close")
    _require(c, period, f"Bollinger({period})")
    middle = sma(c, period)
    windows = np.lib.stride_tricks.sliding_window_view(c, period)
    deviation = windows.std(axis=-1, ddof=0)
    return BollingerResult(
        middle + num_std * deviation, middle, middle - num_std * deviation
    )


class SupertrendResult(NamedTuple):
    trend: np.ndarray        # +1 uptrend, -1 downtrend
    line: np.ndarray         # the active band


def supertrend(
    high: Sequence[float],
    low: Sequence[float],
    close: Sequence[float],
    period: int = 10,
    multiplier: float = 3.0,
) -> SupertrendResult:
    """Supertrend with the standard band-ratchet rule.

    Returns a named tuple so ``(trend, line)`` cannot be unpacked backwards, as
    it was at two legacy call sites.
    """
    h, l, c = _as_array(high, "high"), _as_array(low, "low"), _as_array(close, "close")
    atr_series = atr(h, l, c, period)
    offset = c.size - atr_series.size
    hl2 = (h[offset:] + l[offset:]) / 2.0
    upper = hl2 + multiplier * atr_series
    lower = hl2 - multiplier * atr_series
    closes = c[offset:]

    trend = np.ones(closes.size, dtype=int)
    line = np.empty(closes.size, dtype=float)
    final_upper, final_lower = upper[0], lower[0]
    line[0] = final_lower

    for i in range(1, closes.size):
        # Bands only ratchet in the favourable direction.
        final_upper = (
            min(upper[i], final_upper)
            if closes[i - 1] <= final_upper else upper[i]
        )
        final_lower = (
            max(lower[i], final_lower)
            if closes[i - 1] >= final_lower else lower[i]
        )
        if closes[i] > final_upper:
            trend[i] = 1
        elif closes[i] < final_lower:
            trend[i] = -1
        else:
            trend[i] = trend[i - 1]
        line[i] = final_lower if trend[i] == 1 else final_upper
    return SupertrendResult(trend, line)
