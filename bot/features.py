"""features.py — the one place a feature is defined.

WHY THIS EXISTS
===============
A model trained on features computed one way and served features computed
another way is the single most common way a machine-learning trading system
fails silently. Nothing crashes. The offline metrics stay excellent. The live
system simply trades on numbers that do not mean what the model learned, and
the difference shows up only in the equity curve, weeks later, as "the edge
decayed".

The defect is never in the model. It is in having two implementations of
"compute the features": a vectorised pandas one for training, and a
scalar-at-the-last-bar one for serving. They agree on the easy features and
disagree on the ones that matter — the path-dependent filters, the
normalisations, the handling of the first bars of a buffer.

This module removes the possibility. There is exactly one function that turns
candles into a feature vector, :func:`compute_features`, and the batch builder
for training, :func:`build_dataset`, calls it once per bar. The offline path
*is* the online path; ``tests/test_features.py`` asserts row *i* of a dataset
equals the single-bar computation at bar *i*, exactly, bit for bit.

FOUR RULES, ENFORCED RATHER THAN DOCUMENTED
-------------------------------------------
1. **The future is unreachable, not merely unused.** :func:`compute_features`
   slices its input to ``candles[lo:index + 1]`` on its second statement and
   works only with the slice. A bar after ``index`` is not "carefully avoided";
   it is not in the data the arithmetic can see. Guarding by convention is how
   lookahead gets reintroduced by the next edit.

2. **Nothing is imputed.** A feature that cannot be measured is ``None`` and is
   named in :attr:`FeatureVector.missing`. Not ``0.0`` — a zero MACD histogram
   means "no momentum", which is a *measurement*, and a model cannot tell it
   apart from "we did not have 133 bars yet". Missing book features are the
   sharp case here: the real corpus has no order book at all, and a book
   feature that degraded to zero would teach a model that this venue always has
   a zero spread and a perfectly balanced book.

3. **Every feature is scale-free.** A ratio, a z-score, a percentile, or an
   already-bounded oscillator. Never a price. The corpus in ``data/real`` spans
   BTC from about $3,200 to about $109,000; a model that saw raw prices in
   training has learned a level, and the level will never be seen again.
   ``TestScaleInvariance`` multiplies every price by ten and requires the
   vector to be unchanged.

4. **Warm-up is declared, not assumed.** Wilder smoothing, EMAs and the
   volatility percentile are path dependent: their value at bar *i* depends on
   where the window began. So the window is *fixed* at :data:`MAX_LOOKBACK`
   bars, always, and a vector computed with less history is marked
   ``warm=False``. Short history is not merely less information — it is a
   different number, and a live buffer that has just been filled must be told
   so rather than quietly producing off-distribution inputs.

WHAT A SCHEMA IS FOR
--------------------
:data:`FEATURE_SCHEMA` is data, not the incidental consequence of the order of
lines in a function. It is ordered (column *k* is always the same feature),
versioned (:data:`FEATURE_SCHEMA_VERSION`) and fingerprinted
(:data:`FEATURE_SCHEMA_DIGEST`). A model artefact records both; serving calls
:func:`require_schema_version` and refuses to run against a pipeline that has
moved underneath it. The digest exists because the version is a human promise
and humans forget to bump it — the digest is derived from the schema itself, so
adding, removing or reordering a feature changes it whether or not anyone
remembered.

LABELS, AND WHERE BACKTESTS GET POISONED
----------------------------------------
:func:`triple_barrier_label` answers: starting from this bar's close, does
price touch the take-profit barrier, the stop barrier, or neither, within a
maximum horizon? Three details are load-bearing:

* Barriers are set in ATR multiples measured from bars **at or before** the
  feature bar, so the barrier width scales with the instrument and contains no
  future information.
* If one bar's range touches **both** barriers, the label is the **stop**. A
  bar reports its range, not its path. Choosing the favourable order is the
  classic way a backtest flatters itself, and this is the same conservative
  rule ``backtest.Backtester._resolve_bar`` applies to a live simulated fill —
  a label that disagreed with the simulator would train the model to expect
  fills the simulator will not give it.
* A bar too near the end of the series to resolve is **unresolved and
  excluded**, never defaulted to "loss". Defaulting would attach a systematic
  negative label to whatever regime happens to end the sample.

Each label carries :attr:`Label.bars_to_resolution`, because a triple-barrier
sample's outcome window overlaps its neighbours' — and a cross-validation split
that ignores that overlap lets the model read its own answers out of the
training set. Purging needs the number, so the number is recorded at labelling
time rather than reconstructed later from assumptions.

DEPENDENCY DIRECTION
--------------------
Imports are stdlib, numpy, ``pure_indicators`` and ``market_data``. Not
``config`` (a feature is not configurable — if it were, two deployments would
disagree about what "rsi_norm" means), not ``persistence``, not the engine, not
sklearn. This module is pure computation over inputs it is handed, which is
what makes it testable offline and identical in both directions.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

import numpy as np

import market_data as md
import pure_indicators as pi

__all__ = [
    # schema
    "FEATURE_SCHEMA_VERSION",
    "FEATURE_SCHEMA",
    "FEATURE_SCHEMA_DIGEST",
    "FEATURE_NAMES",
    "PRICE_FEATURE_NAMES",
    "BOOK_FEATURE_NAMES",
    "SPEC_BY_NAME",
    "FEATURE_INDEX",
    "MAX_LOOKBACK",
    "WARMUP_BARS",
    "FeatureSpec",
    "SchemaMismatch",
    "require_schema_version",
    "schema_digest",
    # single-bar computation
    "FeatureVector",
    "compute_features",
    # labels
    "BarrierSpec",
    "Label",
    "LABEL_TAKE_PROFIT",
    "LABEL_STOP",
    "LABEL_TIMEOUT",
    "LABEL_NAMES",
    "triple_barrier_label",
    "triple_barrier_labels",
    # batch
    "Dataset",
    "DatasetReport",
    "build_dataset",
]


# ---------------------------------------------------------------------------
# parameters
#
# These are module constants and deliberately not configuration. A feature is a
# definition, not a setting: if RSI_PERIOD could be set per deployment then
# "rsi_norm" would name two different quantities, the schema digest would not
# change, and a model artefact would be served features it was never trained
# on. Changing any number here is a schema change and must bump
# FEATURE_SCHEMA_VERSION.
# ---------------------------------------------------------------------------

#: Return horizons in bars. 1 is the immediate move; 72 is three days of hourly
#: bars, long enough to carry a swing without becoming a trend label.
RETURN_HORIZONS: Tuple[int, ...] = (1, 4, 12, 24, 72)

VOL_FAST = 24               # realised-vol window, "the last day" on 1h bars
VOL_SLOW = 72               # ... and "the last three days"
VOL_REGIME_LOOKBACK = 240   # the trailing distribution vol is ranked against

ATR_PERIOD = 14
RSI_PERIOD = 14
ADX_PERIOD = 14

MACD_FAST, MACD_SLOW, MACD_SIGNAL = 12, 26, 9
#: Bars of MACD histogram used to measure the histogram's own scale. The
#: histogram is in price units; dividing by its own trailing dispersion is what
#: makes it comparable between $3k BTC and $100k BTC.
MACD_SCALE_WINDOW = 100
#: ``len(macd().histogram) == len(closes) - _MACD_HIST_OFFSET``. Derived from
#: pure_indicators' alignment rather than hard-coded, so it cannot drift.
_MACD_HIST_OFFSET = MACD_SLOW + MACD_SIGNAL - 2

BB_PERIOD = 20
BB_NUM_STD = 2.0

MA_FAST, MA_MEDIUM, MA_SLOW = 20, 50, 100
MA_SLOPE_LOOKBACK = 5

VOLUME_WINDOW = 50
VOLUME_FAST_WINDOW = 6

#: Book depth levels summed for the imbalance feature. Matches the default of
#: ``market_data.BookFeatures.from_state`` so the number a model trains on is
#: the number ``risk_management._gate_book_liquidity`` sees in production.
BOOK_LEVELS = 10

_MS_PER_DAY = 86_400_000
#: 1970-01-01 was a Thursday; Monday is day 0 in this encoding.
_EPOCH_WEEKDAY_OFFSET = 3

#: Denominators smaller than this are treated as "no measurement" rather than
#: divided by. Not an epsilon for float noise — a genuine statement that a
#: ratio against a vanishing denominator carries no information.
_MIN_DENOMINATOR = 1e-12


class SchemaMismatch(ValueError):
    """A model artefact and this module disagree about what a feature is.

    Raised, never warned. A mismatch means the column a model reads as
    ``rsi_norm`` may now hold ``adx_norm``; there is no degraded mode in which
    that is safe to continue from.
    """


@dataclass(frozen=True)
class FeatureSpec:
    """One declared feature.

    ``min_bars`` is the number of closed candles the feature needs before it
    can be measured at all, and it is the *only* authority on that: the
    computation below asks the spec rather than repeating a magic number, so a
    change to a period cannot leave the declared warm-up stale.

    ``bounds`` documents a hard mathematical range where one exists (an
    oscillator, a fraction). It is not enforced by clamping — clamping hides
    the bug that produced the out-of-range value.
    """

    name: str
    description: str
    min_bars: int
    needs_book: bool = False
    bounds: Optional[Tuple[float, float]] = None


#: Bumped whenever the set, the order, or the definition of any feature
#: changes. A model artefact records this string; serving compares it.
FEATURE_SCHEMA_VERSION = "1.0.0"


FEATURE_SCHEMA: Tuple[FeatureSpec, ...] = (
    # -- returns and momentum ------------------------------------------------
    FeatureSpec(
        "ret_1", "Log return over 1 bar. The immediate move, sign and size.", 2,
    ),
    FeatureSpec(
        "ret_4", "Log return over 4 bars — short-horizon momentum.", 5,
    ),
    FeatureSpec(
        "ret_12", "Log return over 12 bars — half-day momentum on 1h bars.", 13,
    ),
    FeatureSpec(
        "ret_24", "Log return over 24 bars — one day of momentum.", 25,
    ),
    FeatureSpec(
        "ret_72", "Log return over 72 bars — the multi-day swing.", 73,
    ),
    FeatureSpec(
        "ret_24_vol_norm",
        "24-bar return divided by its own expected size under the trailing "
        "volatility. A 3% move is enormous in a quiet regime and noise in a "
        "violent one; the raw return cannot tell a model which it is.",
        max(25, VOL_FAST + 1),
    ),
    # -- volatility ----------------------------------------------------------
    FeatureSpec(
        "rv_24",
        "Realised volatility: standard deviation of log returns over 24 bars. "
        "A fraction, so it is comparable across price levels and instruments.",
        VOL_FAST + 1,
    ),
    FeatureSpec(
        "rv_72", "Realised volatility over 72 bars — the slower reference.",
        VOL_SLOW + 1,
    ),
    FeatureSpec(
        "vol_ratio_24_72",
        "rv_24 / rv_72. Above 1 means volatility is expanding right now, "
        "which changes what a given stop distance is worth.",
        VOL_SLOW + 1,
    ),
    FeatureSpec(
        "vol_regime_pct",
        "Rank of the current rv_24 within its own trailing 240-bar "
        "distribution, in [0, 1]. The regime measure: a ratio to its own "
        "history, so it means the same thing in 2018 and 2024, which an "
        "absolute volatility threshold does not.",
        VOL_FAST + VOL_REGIME_LOOKBACK,
        bounds=(0.0, 1.0),
    ),
    FeatureSpec(
        "atr_pct",
        "Wilder ATR(14) divided by the close. Typical bar range as a fraction "
        "of price — the unit every stop distance in this codebase is quoted "
        "in.",
        ATR_PERIOD + 1,
    ),
    # -- oscillators and trend ----------------------------------------------
    FeatureSpec(
        "rsi_norm",
        "Wilder RSI(14) mapped to [-1, 1] as (rsi - 50) / 50. Centred so that "
        "'neutral' is zero rather than a magic 50.",
        RSI_PERIOD + 1,
        bounds=(-1.0, 1.0),
    ),
    FeatureSpec(
        "macd_hist_z",
        "MACD histogram divided by the standard deviation of its own last 100 "
        "values. The raw histogram is in dollars and is therefore ten times "
        "larger at $100k BTC than at $10k for identical behaviour.",
        _MACD_HIST_OFFSET + MACD_SCALE_WINDOW,
    ),
    FeatureSpec(
        "adx_norm",
        "Wilder ADX(14) / 100, in [0, 1]. Trend strength without direction — "
        "the gate on whether a directional feature should be believed.",
        2 * ADX_PERIOD + 1,
        bounds=(0.0, 1.0),
    ),
    FeatureSpec(
        "di_spread",
        "(+DI - -DI) / (+DI + -DI), in [-1, 1]. Directional balance, "
        "normalised so it does not co-vary with volatility the way the raw "
        "DI difference does.",
        2 * ADX_PERIOD + 1,
        bounds=(-1.0, 1.0),
    ),
    FeatureSpec(
        "bb_position",
        "Position of the close within the Bollinger(20, 2) band: 0 at the "
        "lower band, 1 at the upper. Deliberately unclamped — a value above 1 "
        "is a real breakout and clamping would erase it.",
        BB_PERIOD,
    ),
    FeatureSpec(
        "bb_width",
        "(upper - lower) / middle. Band width as a fraction of price — the "
        "squeeze/expansion signal, scale-free by construction.",
        BB_PERIOD,
    ),
    FeatureSpec(
        "dist_sma20_atr",
        "(close - SMA20) / ATR14. Distance from the fast mean in units of "
        "what this instrument actually moves in a bar, which is the only way "
        "'far from the mean' transfers between regimes.",
        max(MA_FAST, ATR_PERIOD + 1),
    ),
    FeatureSpec(
        "dist_sma50_atr", "(close - SMA50) / ATR14 — the medium-term stretch.",
        max(MA_MEDIUM, ATR_PERIOD + 1),
    ),
    FeatureSpec(
        "dist_sma100_atr", "(close - SMA100) / ATR14 — the slow-trend stretch.",
        max(MA_SLOW, ATR_PERIOD + 1),
    ),
    FeatureSpec(
        "sma20_slope_atr",
        "Per-bar slope of SMA20 over the last 5 bars, in ATR units. Trend "
        "direction that is independent of how far price has already travelled.",
        max(MA_FAST + MA_SLOPE_LOOKBACK, ATR_PERIOD + 1),
    ),
    # -- volume --------------------------------------------------------------
    FeatureSpec(
        "volume_z_50",
        "z-score of this bar's volume against the last 50 bars. Volume in base "
        "units is meaningless across instruments and eras; its z-score is not.",
        VOLUME_WINDOW,
    ),
    FeatureSpec(
        "volume_trend",
        "log(mean volume over 6 bars / mean volume over 50 bars). Whether "
        "participation is building or draining, as a ratio.",
        VOLUME_WINDOW,
    ),
    # -- candle shape --------------------------------------------------------
    FeatureSpec(
        "body_fraction",
        "(close - open) / (high - low), in [-1, 1]. How much of the bar's "
        "range the market actually kept — conviction, not just direction.",
        1, bounds=(-1.0, 1.0),
    ),
    FeatureSpec(
        "upper_wick_fraction",
        "(high - max(open, close)) / (high - low), in [0, 1]. Rejected upside.",
        1, bounds=(0.0, 1.0),
    ),
    FeatureSpec(
        "lower_wick_fraction",
        "(min(open, close) - low) / (high - low), in [0, 1]. Rejected downside.",
        1, bounds=(0.0, 1.0),
    ),
    FeatureSpec(
        "close_location",
        "((close - low) - (high - close)) / (high - low), in [-1, 1]. Where "
        "the bar closed within its range, the classic close-location value.",
        1, bounds=(-1.0, 1.0),
    ),
    FeatureSpec(
        "range_atr",
        "(high - low) / ATR14. Whether this specific bar was large or small "
        "for this instrument right now.",
        ATR_PERIOD + 1,
    ),
    # -- time, cyclically encoded -------------------------------------------
    FeatureSpec(
        "hour_sin",
        "sin(2*pi * fraction of the UTC day at the bar's start). Paired with "
        "hour_cos because an integer hour tells a tree that 23 and 0 are 23 "
        "apart when they are adjacent.",
        1, bounds=(-1.0, 1.0),
    ),
    FeatureSpec(
        "hour_cos", "cos of the same angle as hour_sin.", 1, bounds=(-1.0, 1.0),
    ),
    FeatureSpec(
        "dow_sin",
        "sin(2*pi * weekday / 7), Monday = 0. Crypto trades at the weekend but "
        "does not trade the same way; the encoding keeps Sunday next to Monday.",
        1, bounds=(-1.0, 1.0),
    ),
    FeatureSpec(
        "dow_cos", "cos of the same angle as dow_sin.", 1, bounds=(-1.0, 1.0),
    ),
    # -- order book, when there is one --------------------------------------
    FeatureSpec(
        "book_spread_bps",
        "Touch spread in basis points. A ratio, and the cost every edge "
        "estimate is measured against. None without a usable book.",
        1, needs_book=True,
    ),
    FeatureSpec(
        "book_imbalance",
        "(bid depth - ask depth) / total depth over 10 levels, in [-1, 1]. "
        "Measured by market_data so training and the live liquidity gate share "
        "one implementation.",
        1, needs_book=True, bounds=(-1.0, 1.0),
    ),
    FeatureSpec(
        "book_microprice_dev_bps",
        "(microprice - mid) / mid in basis points. Which side the touch sizes "
        "are leaning, expressed as a ratio rather than a price.",
        1, needs_book=True,
    ),
)


def _validate_schema(schema: Sequence[FeatureSpec]) -> None:
    """Structural checks on the schema, run at import.

    A duplicated or empty name would make ``FEATURE_INDEX`` silently
    lose a column, and the loss would surface as a model reading the wrong
    number rather than as an error.
    """
    if not schema:
        raise ValueError("FEATURE_SCHEMA must not be empty")
    seen = set()
    for spec in schema:
        if not spec.name or not spec.name.strip():
            raise ValueError("every feature needs a name")
        if spec.name in seen:
            raise ValueError(f"duplicate feature name: {spec.name!r}")
        seen.add(spec.name)
        if spec.min_bars < 1:
            raise ValueError(f"{spec.name}: min_bars must be >= 1")
        if not spec.description.strip():
            raise ValueError(f"{spec.name}: a feature without a description "
                             "is a column nobody can audit")


_validate_schema(FEATURE_SCHEMA)

FEATURE_NAMES: Tuple[str, ...] = tuple(spec.name for spec in FEATURE_SCHEMA)
PRICE_FEATURE_NAMES: Tuple[str, ...] = tuple(
    spec.name for spec in FEATURE_SCHEMA if not spec.needs_book
)
BOOK_FEATURE_NAMES: Tuple[str, ...] = tuple(
    spec.name for spec in FEATURE_SCHEMA if spec.needs_book
)
#: Mapping proxy, not a dict: the schema is a contract and a caller that could
#: mutate it could make training and serving disagree at runtime.
SPEC_BY_NAME: Mapping[str, FeatureSpec] = MappingProxyType(
    {spec.name: spec for spec in FEATURE_SCHEMA}
)
FEATURE_INDEX: Mapping[str, int] = MappingProxyType(
    {spec.name: i for i, spec in enumerate(FEATURE_SCHEMA)}
)

#: The fixed window handed to the arithmetic. Every feature is computed from
#: exactly this many bars once they exist, so a feature's value does not depend
#: on how much history the process happens to be holding.
MAX_LOOKBACK: int = max(spec.min_bars for spec in FEATURE_SCHEMA)
#: Bars needed before a vector is fully comparable with the training
#: distribution. Same number, named for what a caller actually asks about.
WARMUP_BARS: int = MAX_LOOKBACK


def schema_digest(schema: Sequence[FeatureSpec] = FEATURE_SCHEMA,
                  version: str = FEATURE_SCHEMA_VERSION) -> str:
    """Fingerprint of the schema's identity: order, names, warm-up, book-ness.

    The version string is a promise a human makes and can forget to keep. This
    is derived from the schema itself, so a reordering that nobody thought
    worth a version bump still changes the digest, and
    :func:`require_schema_version` still refuses to serve.

    Descriptions are excluded on purpose: rewording a docstring must not
    invalidate a trained model, because it does not change any number.
    """
    payload = [version]
    for i, spec in enumerate(schema):
        payload.append(f"{i}:{spec.name}:{spec.min_bars}:{int(spec.needs_book)}")
    text = "\n".join(payload).encode("utf-8")
    return hashlib.sha256(text).hexdigest()[:16]


FEATURE_SCHEMA_DIGEST: str = schema_digest()


def require_schema_version(version: str, digest: Optional[str] = None) -> None:
    """Fail closed unless the caller was built against this exact schema.

    Called by whatever loads a model artefact, before the first prediction.
    Passing the digest as well as the version is strictly better and is why
    both are recorded: the version catches an intentional change, the digest
    catches an unintentional one.
    """
    if version != FEATURE_SCHEMA_VERSION:
        raise SchemaMismatch(
            f"feature schema version mismatch: artefact was built against "
            f"{version!r}, this module produces {FEATURE_SCHEMA_VERSION!r}. "
            "Retrain or pin the module; there is no safe way to map one onto "
            "the other."
        )
    if digest is not None and digest != FEATURE_SCHEMA_DIGEST:
        raise SchemaMismatch(
            f"feature schema digest mismatch: artefact {digest!r} vs "
            f"module {FEATURE_SCHEMA_DIGEST!r}. The version string was not "
            "bumped but the schema changed — the column order a model reads "
            "is not the column order this module writes."
        )


# ---------------------------------------------------------------------------
# small guarded arithmetic
#
# Everything a feature value passes through on its way out. There is no other
# route from arithmetic into a FeatureVector, which is what makes "no NaN, no
# inf, no imputed value" a property of the module rather than a hope.
# ---------------------------------------------------------------------------


def _num(value: Any) -> Optional[float]:
    """A finite float, or ``None``.

    ``nan`` and ``inf`` are not values, they are failures that happen to be
    typed as floats. A nan in a feature column silently drops a row in some
    trainers, poisons a whole matrix in others, and compares false to itself in
    every assertion written to catch it.
    """
    if value is None:
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _ratio(numerator: Any, denominator: Any) -> Optional[float]:
    """``numerator / denominator``, or ``None`` when the answer is not a
    measurement. A vanishing denominator does not mean a large feature."""
    num = _num(numerator)
    den = _num(denominator)
    if num is None or den is None or abs(den) < _MIN_DENOMINATOR:
        return None
    return _num(num / den)


def _std(values: np.ndarray) -> Optional[float]:
    """Population standard deviation, or ``None`` if there is nothing to
    measure. ddof=0 to match ``pure_indicators.bollinger``."""
    if values.size < 2:
        return None
    return _num(float(values.std(ddof=0)))


# ---------------------------------------------------------------------------
# the feature vector
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FeatureVector:
    """One bar's features, plus what could not be measured and why.

    ``values`` is positional and aligned with :data:`FEATURE_SCHEMA`; entry *k*
    is ``FEATURE_NAMES[k]``. ``missing`` names every entry that is ``None``,
    so a caller learns *which* features were unavailable without scanning for
    ``None`` and guessing — the difference between "the book was absent" and
    "we are 40 bars into a cold start" changes what the caller should do.

    ``warm`` is the quieter of the two warnings and the more dangerous one to
    ignore: every value can be present and the vector still be off-distribution
    because the path-dependent filters were seeded 30 bars ago instead of 264.
    """

    schema_version: str
    schema_digest: str
    index: int
    timestamp_ms: int
    values: Tuple[Optional[float], ...]
    missing: Tuple[str, ...]
    warm: bool
    window_bars: int
    book_available: bool

    @property
    def names(self) -> Tuple[str, ...]:
        return FEATURE_NAMES

    @property
    def is_complete(self) -> bool:
        """True when every declared feature was measured, book included."""
        return not self.missing

    @property
    def price_features_complete(self) -> bool:
        """True when everything computable without a book was measured.

        The useful predicate on a corpus like ``data/real``, which has no book
        at all: those rows are complete in the only sense available to them,
        and reporting them as incomplete would discard the entire dataset.
        """
        missing = set(self.missing)
        return not (missing & set(PRICE_FEATURE_NAMES))

    def as_dict(self) -> Dict[str, Optional[float]]:
        return dict(zip(FEATURE_NAMES, self.values))

    def get(self, name: str) -> Optional[float]:
        """Value by name. Raises on an unknown name rather than returning
        ``None`` — an unknown name is a typo, and a typo that returns "missing"
        is a feature silently dropped from a model."""
        if name not in FEATURE_INDEX:
            raise KeyError(f"{name!r} is not a declared feature")
        return self.values[FEATURE_INDEX[name]]

    def to_array(self, fill: float = float("nan")) -> np.ndarray:
        """Dense float64 row, with ``fill`` where the value is missing.

        ``nan`` by default and that is the intended value: in a float matrix
        ``nan`` is the only encoding of "not measured" that cannot be mistaken
        for a measurement. Passing ``fill=0.0`` is imputation, it is available
        only because a caller may knowingly want it, and it is never done by
        :func:`build_dataset`.
        """
        return np.array(
            [fill if v is None else v for v in self.values], dtype=np.float64
        )


# ---------------------------------------------------------------------------
# inputs
# ---------------------------------------------------------------------------


def _candle_arrays(
    window: Sequence[Any],
) -> Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Pull OHLCV out of the window and refuse anything unusable.

    Raises rather than skipping a bad bar. ``market_data.load_ohlcv`` already
    rejects non-finite and non-positive rows and reports them as data; a bar
    that reaches here broken means something upstream fabricated it, and
    quietly dropping it would put a hole in a window whose length is part of
    every feature's definition.
    """
    rows = []
    for bar in window:
        try:
            rows.append((
                float(bar.open), float(bar.high), float(bar.low),
                float(bar.close), float(bar.volume),
            ))
        except AttributeError as exc:      # not a candle at all
            raise TypeError(
                "candles must expose open/high/low/close/volume "
                f"(market_data.Bar or backtest.Bar); got {type(bar).__name__}"
            ) from exc
    data = np.asarray(rows, dtype=np.float64)
    if not np.all(np.isfinite(data)):
        raise ValueError("candle window contains non-finite OHLCV")
    if np.any(data[:, :4] <= 0.0):
        raise ValueError("candle window contains a non-positive price")
    if np.any(data[:, 4] < 0.0):
        raise ValueError("candle window contains a negative volume")
    return (data[:, 0].copy(), data[:, 1].copy(), data[:, 2].copy(),
            data[:, 3].copy(), data[:, 4].copy())


def _coerce_book(book: Any) -> Optional[md.BookFeatures]:
    """Accept any shape the rest of the stack hands around, or refuse it.

    Four shapes are legitimate because four places produce a book:
    ``None`` (there isn't one), ``BookFeatures`` (already measured),
    ``BookState`` (a replayed or live book), and a ``(bids, asks)`` ladder pair
    (what ``market_data.load_corpus`` returns per bar, and what Bybit's REST
    snapshot looks like). All four funnel into ``market_data``'s single
    measurement, so a book feature in training is computed by the same code as
    the book number the live liquidity gate reads.

    Anything else raises. Guessing at an unknown shape is how a model ends up
    trained against a book that was silently never parsed.
    """
    if book is None:
        return None
    if isinstance(book, md.BookFeatures):
        return book
    if isinstance(book, md.BookState):
        return book.features(levels=BOOK_LEVELS)
    if isinstance(book, (tuple, list)) and len(book) == 2:
        bids, asks = book
        if isinstance(bids, (tuple, list)) and isinstance(asks, (tuple, list)):
            state = md.BookState.from_ladders(bids, asks, strict=False)
            return state.features(levels=BOOK_LEVELS)
    raise TypeError(
        "book must be None, a market_data.BookFeatures, a market_data."
        f"BookState, or a (bids, asks) ladder pair; got {type(book).__name__}"
    )


def _time_features(timestamp_ms: int) -> Tuple[float, float, float, float]:
    """Cyclical UTC time-of-day and day-of-week from epoch milliseconds.

    Integer arithmetic, no calendar library and no float division of the
    timestamp: the encoding must be bit-identical between a training run and a
    live process, and ``ms / 1000`` for a 2024 timestamp already costs
    precision. Uses the bar's **start**, which every candle carries; the close
    time is optional on a positionally-built ``Bar`` and using it would shift
    the encoding by one interval depending on how the bar was loaded.
    """
    ms_of_day = timestamp_ms % _MS_PER_DAY
    day_fraction = ms_of_day / _MS_PER_DAY
    day_angle = 2.0 * math.pi * day_fraction

    days = timestamp_ms // _MS_PER_DAY
    weekday = (days + _EPOCH_WEEKDAY_OFFSET) % 7      # 0 = Monday
    week_angle = 2.0 * math.pi * (weekday / 7.0)
    return (
        math.sin(day_angle), math.cos(day_angle),
        math.sin(week_angle), math.cos(week_angle),
    )


# ---------------------------------------------------------------------------
# the one computation
# ---------------------------------------------------------------------------


def compute_features(
    candles: Sequence[Any],
    index: Optional[int] = None,
    book: Any = None,
) -> FeatureVector:
    """Compute the feature vector at one bar, from that bar and its past.

    ``candles`` is any sequence of objects with ``open/high/low/close/volume``
    and ``start_ms`` — ``market_data.Bar`` and ``backtest.Bar`` both qualify,
    which is deliberate: the backtester and the loader must not need adapting
    to feed this.

    ``index`` defaults to the last bar, which is the live case: the buffer ends
    at the bar that just closed. Negative indices follow Python's convention.

    ``book`` must be the book **as of this bar's close**. Supplying a later
    book is the one lookahead this function cannot detect, because it cannot
    see the book's timestamps; ``market_data.replay`` — and therefore
    ``load_corpus`` — produces per-bar books that are structurally incapable of
    being late, which is why :func:`build_dataset` takes that list directly.

    THE SLICE IS THE POINT
    ----------------------
    The second statement of the body cuts ``candles`` down to
    ``[index - MAX_LOOKBACK + 1 : index + 1]`` and nothing below it ever sees
    ``candles`` again. The future is not avoided by discipline; it is not
    present. That is what ``TestNoLookahead`` verifies from the outside, by
    proving that truncating the input at ``index`` changes nothing.

    Returns a :class:`FeatureVector` in which any feature that could not be
    measured is ``None`` and named in ``missing``. Nothing is imputed, and no
    returned value is ``nan`` or ``inf``.
    """
    total = len(candles)
    if total == 0:
        raise ValueError("candles is empty; there is nothing to compute")
    if index is None:
        index = total - 1
    index = int(index)
    if index < 0:
        index += total
    if not (0 <= index < total):
        raise IndexError(f"index {index} is outside 0..{total - 1}")

    # --- the no-lookahead cut. Nothing below reads `candles`. ---------------
    low_bound = max(0, index - MAX_LOOKBACK + 1)
    window = tuple(candles[low_bound:index + 1])
    del candles

    opens, highs, lows, closes, volumes = _candle_arrays(window)
    n = closes.size
    last = window[-1]
    timestamp_ms = int(getattr(last, "start_ms", 0))
    features = _compute_from_window(
        opens, highs, lows, closes, volumes, timestamp_ms, _coerce_book(book)
    )

    # Declared and produced must be the same set. Asserted rather than trusted:
    # a feature computed but not declared never reaches a model, and a feature
    # declared but not computed would shift every column after it.
    if set(features) != set(FEATURE_NAMES):
        raise AssertionError(
            "computed features do not match the schema; "
            f"extra={sorted(set(features) - set(FEATURE_NAMES))} "
            f"absent={sorted(set(FEATURE_NAMES) - set(features))}"
        )

    values = tuple(features[name] for name in FEATURE_NAMES)
    if any(v is not None and not math.isfinite(v) for v in values):
        raise AssertionError("a non-finite value escaped _num(); refusing")
    missing = tuple(
        name for name, value in zip(FEATURE_NAMES, values) if value is None
    )
    return FeatureVector(
        schema_version=FEATURE_SCHEMA_VERSION,
        schema_digest=FEATURE_SCHEMA_DIGEST,
        index=index,
        timestamp_ms=timestamp_ms,
        values=values,
        missing=missing,
        warm=n >= WARMUP_BARS,
        window_bars=n,
        book_available=book is not None,
    )


def _compute_from_window(
    opens: np.ndarray,
    highs: np.ndarray,
    lows: np.ndarray,
    closes: np.ndarray,
    volumes: np.ndarray,
    timestamp_ms: int,
    book: Optional[md.BookFeatures],
) -> Dict[str, Optional[float]]:
    """Every feature, from a window that ends at the bar being described.

    Split out from :func:`compute_features` only so that the slice above is the
    boundary: this function is handed arrays and cannot obtain more of them.

    Each block asks ``have(name)`` — which reads ``min_bars`` off the schema —
    before computing. That is what keeps the declared warm-up and the actual
    warm-up the same number, and it is why ``TestDeclaredMinimumBars`` can
    assert the boundary for every feature generically.
    """
    n = closes.size
    out: Dict[str, Optional[float]] = {}

    def have(name: str) -> bool:
        return n >= SPEC_BY_NAME[name].min_bars

    log_closes = np.log(closes)
    log_returns = np.diff(log_closes) if n >= 2 else np.empty(0)

    # -- returns -------------------------------------------------------------
    for horizon in RETURN_HORIZONS:
        name = f"ret_{horizon}"
        out[name] = (
            _num(log_closes[-1] - log_closes[-1 - horizon]) if have(name) else None
        )

    # -- realised volatility --------------------------------------------------
    rv_fast = _std(log_returns[-VOL_FAST:]) if have("rv_24") else None
    rv_slow = _std(log_returns[-VOL_SLOW:]) if have("rv_72") else None
    out["rv_24"] = rv_fast
    out["rv_72"] = rv_slow
    out["vol_ratio_24_72"] = (
        _ratio(rv_fast, rv_slow) if have("vol_ratio_24_72") else None
    )
    # A 24-bar return's expected magnitude under an iid random walk is
    # rv * sqrt(24). Dividing by it asks "how big was this move *for this
    # regime*", which is the question a threshold on a raw return cannot ask.
    out["ret_24_vol_norm"] = (
        _ratio(out["ret_24"], (rv_fast or 0.0) * math.sqrt(VOL_FAST))
        if have("ret_24_vol_norm") and rv_fast is not None else None
    )

    if have("vol_regime_pct"):
        windows = np.lib.stride_tricks.sliding_window_view(log_returns, VOL_FAST)
        rv_series = windows.std(axis=-1, ddof=0)[-VOL_REGIME_LOOKBACK:]
        current = rv_series[-1]
        if np.all(np.isfinite(rv_series)):
            # Rank *including* the current observation, so the value is in
            # (0, 1] and a fresh all-time-high volatility reads exactly 1.0
            # rather than an ambiguous 0.996.
            out["vol_regime_pct"] = _num(
                float(np.count_nonzero(rv_series <= current)) / rv_series.size
            )
        else:
            out["vol_regime_pct"] = None
    else:
        out["vol_regime_pct"] = None

    # -- ATR, and everything quoted in ATR units ------------------------------
    atr_value: Optional[float] = None
    if have("atr_pct"):
        try:
            atr_value = _num(float(pi.atr(highs, lows, closes, ATR_PERIOD)[-1]))
        except pi.InsufficientData:
            atr_value = None
        # A zero ATR is not a small ATR. Everything below divides by it.
        if atr_value is not None and atr_value <= 0.0:
            atr_value = None
    out["atr_pct"] = _ratio(atr_value, closes[-1]) if atr_value is not None else None

    # -- RSI ------------------------------------------------------------------
    if have("rsi_norm"):
        try:
            rsi_value = float(pi.rsi(closes, RSI_PERIOD)[-1])
            out["rsi_norm"] = _num((rsi_value - 50.0) / 50.0)
        except pi.InsufficientData:
            out["rsi_norm"] = None
    else:
        out["rsi_norm"] = None

    # -- MACD, normalised by its own dispersion -------------------------------
    if have("macd_hist_z"):
        try:
            hist = pi.macd(closes, MACD_FAST, MACD_SLOW, MACD_SIGNAL).histogram
        except pi.InsufficientData:
            hist = np.empty(0)
        if hist.size >= MACD_SCALE_WINDOW:
            scale = _std(hist[-MACD_SCALE_WINDOW:])
            out["macd_hist_z"] = _ratio(hist[-1], scale)
        else:
            out["macd_hist_z"] = None
    else:
        out["macd_hist_z"] = None

    # -- ADX / directional strength -------------------------------------------
    if have("adx_norm"):
        try:
            adx_result = pi.adx(highs, lows, closes, ADX_PERIOD)
        except pi.InsufficientData:
            adx_result = None
    else:
        adx_result = None
    if adx_result is not None and adx_result.adx.size:
        out["adx_norm"] = _num(float(adx_result.adx[-1]) / 100.0)
        plus = float(adx_result.plus_di[-1])
        minus = float(adx_result.minus_di[-1])
        # Both DIs zero means there was no directional movement to divide up.
        # Reporting 0.0 would claim a measured balance; there was no measurement.
        out["di_spread"] = _ratio(plus - minus, plus + minus)
    else:
        out["adx_norm"] = None
        out["di_spread"] = None

    # -- Bollinger ------------------------------------------------------------
    if have("bb_position"):
        try:
            bands = pi.bollinger(closes, BB_PERIOD, BB_NUM_STD)
        except pi.InsufficientData:
            bands = None
    else:
        bands = None
    if bands is not None:
        upper = float(bands.upper[-1])
        lower = float(bands.lower[-1])
        middle = float(bands.middle[-1])
        out["bb_position"] = _ratio(closes[-1] - lower, upper - lower)
        out["bb_width"] = _ratio(upper - lower, middle)
    else:
        out["bb_position"] = None
        out["bb_width"] = None

    # -- distance from the moving averages, in ATR units ----------------------
    for name, period in (
        ("dist_sma20_atr", MA_FAST),
        ("dist_sma50_atr", MA_MEDIUM),
        ("dist_sma100_atr", MA_SLOW),
    ):
        if have(name) and atr_value is not None:
            try:
                mean = float(pi.sma(closes, period)[-1])
                out[name] = _ratio(closes[-1] - mean, atr_value)
            except pi.InsufficientData:
                out[name] = None
        else:
            out[name] = None

    if have("sma20_slope_atr") and atr_value is not None:
        try:
            fast_ma = pi.sma(closes, MA_FAST)
        except pi.InsufficientData:
            fast_ma = np.empty(0)
        if fast_ma.size > MA_SLOPE_LOOKBACK:
            rise = float(fast_ma[-1] - fast_ma[-1 - MA_SLOPE_LOOKBACK])
            out["sma20_slope_atr"] = _ratio(rise, MA_SLOPE_LOOKBACK * atr_value)
        else:
            out["sma20_slope_atr"] = None
    else:
        out["sma20_slope_atr"] = None

    # -- volume ---------------------------------------------------------------
    if have("volume_z_50"):
        recent = volumes[-VOLUME_WINDOW:]
        out["volume_z_50"] = _ratio(volumes[-1] - float(recent.mean()), _std(recent))
    else:
        out["volume_z_50"] = None

    if have("volume_trend"):
        fast_mean = float(volumes[-VOLUME_FAST_WINDOW:].mean())
        slow_mean = float(volumes[-VOLUME_WINDOW:].mean())
        # Zero volume over a whole window is a real thing on a thin venue, and
        # log(0) is not a large negative number, it is an absent measurement.
        if fast_mean > 0.0 and slow_mean > 0.0:
            out["volume_trend"] = _num(math.log(fast_mean / slow_mean))
        else:
            out["volume_trend"] = None
    else:
        out["volume_trend"] = None

    # -- candle shape ---------------------------------------------------------
    bar_open = float(opens[-1])
    bar_high = float(highs[-1])
    bar_low = float(lows[-1])
    bar_close = float(closes[-1])
    bar_range = bar_high - bar_low
    if bar_range > 0.0:
        out["body_fraction"] = _ratio(bar_close - bar_open, bar_range)
        out["upper_wick_fraction"] = _ratio(
            bar_high - max(bar_open, bar_close), bar_range
        )
        out["lower_wick_fraction"] = _ratio(
            min(bar_open, bar_close) - bar_low, bar_range
        )
        out["close_location"] = _ratio(
            (bar_close - bar_low) - (bar_high - bar_close), bar_range
        )
    else:
        # A zero-range bar has no shape. Every fraction is 0/0 — undefined, not
        # neutral, and a 0.5 here would read as a perfectly balanced bar.
        out["body_fraction"] = None
        out["upper_wick_fraction"] = None
        out["lower_wick_fraction"] = None
        out["close_location"] = None
    # ... but its *size* is defined, and zero is the correct answer.
    out["range_atr"] = (
        _ratio(bar_range, atr_value) if atr_value is not None else None
    )

    # -- time -----------------------------------------------------------------
    hour_sin, hour_cos, dow_sin, dow_cos = _time_features(timestamp_ms)
    out["hour_sin"] = _num(hour_sin)
    out["hour_cos"] = _num(hour_cos)
    out["dow_sin"] = _num(dow_sin)
    out["dow_cos"] = _num(dow_cos)

    # -- order book -----------------------------------------------------------
    # Absent, one-sided or crossed book -> None on every book feature. market_data
    # has already refused to invent a mid; this must not invent one either, and
    # 0.0 would tell a model this venue trades at a zero spread.
    if book is None:
        out["book_spread_bps"] = None
        out["book_imbalance"] = None
        out["book_microprice_dev_bps"] = None
    else:
        out["book_spread_bps"] = _num(book.spread_bps)
        out["book_imbalance"] = _num(book.imbalance)
        if book.microprice is not None and book.mid is not None:
            out["book_microprice_dev_bps"] = _ratio(
                (book.microprice - book.mid) * 10_000.0, book.mid
            )
        else:
            out["book_microprice_dev_bps"] = None

    return out


# ---------------------------------------------------------------------------
# labels
# ---------------------------------------------------------------------------

LABEL_TAKE_PROFIT = 1
LABEL_STOP = -1
LABEL_TIMEOUT = 0

LABEL_NAMES: Mapping[int, str] = MappingProxyType({
    LABEL_TAKE_PROFIT: "take_profit",
    LABEL_STOP: "stop",
    LABEL_TIMEOUT: "timeout",
})


@dataclass(frozen=True)
class BarrierSpec:
    """The triple-barrier definition, in ATR multiples.

    ATR multiples rather than percentages because the same percentage is a
    different trade in different regimes: a 1% stop is four hours of noise in
    March 2020 and a week of drift in August 2019. A model trained on
    percentage barriers learns the volatility calendar, not the signal.

    The default 2:1 reward-to-risk over 24 bars is a starting point, not a
    claim: it is roughly what ``risk_management``'s cost gate needs a trade to
    clear on hourly bars, and it is stated here so a caller changing it knows
    they are changing the question, not tuning a knob.
    """

    take_profit_atr: float = 2.0
    stop_atr: float = 1.0
    max_horizon: int = 24
    atr_period: int = ATR_PERIOD
    direction: int = 1          # +1 = long, -1 = short

    def __post_init__(self) -> None:
        if not (self.take_profit_atr > 0.0 and math.isfinite(self.take_profit_atr)):
            raise ValueError("take_profit_atr must be positive and finite")
        if not (self.stop_atr > 0.0 and math.isfinite(self.stop_atr)):
            raise ValueError("stop_atr must be positive and finite")
        if self.max_horizon < 1:
            raise ValueError("max_horizon must be at least 1 bar")
        if self.atr_period < 2:
            raise ValueError("atr_period must be at least 2")
        if self.direction not in (1, -1):
            raise ValueError("direction must be +1 (long) or -1 (short)")

    @property
    def reward_to_risk(self) -> float:
        return self.take_profit_atr / self.stop_atr


@dataclass(frozen=True)
class Label:
    """The outcome of one triple-barrier trade, or an honest refusal.

    ``outcome`` is ``None`` exactly when ``resolved`` is False, and ``reason``
    says why. The two states that produce it are a bar too close to the end of
    the series to know the answer, and a bar without enough history to measure
    an ATR — different problems, both of which must be excluded rather than
    labelled.

    ``bars_to_resolution`` is the horizon this sample's outcome actually
    consumed. It is recorded here, at labelling time, because purged
    cross-validation needs it and reconstructing it later means assuming the
    maximum, which over-purges and quietly shrinks the training set.
    """

    index: int
    outcome: Optional[int]
    resolved: bool
    reason: Optional[str]
    bars_to_resolution: Optional[int]
    entry_price: float
    atr_at_entry: Optional[float]
    upper_barrier: Optional[float]
    lower_barrier: Optional[float]
    barrier: Optional[str]

    @property
    def outcome_end_index(self) -> Optional[int]:
        """Last bar this sample's outcome depended on.

        Any sample whose ``[index, outcome_end_index]`` span overlaps a test
        fold must be dropped from the training fold, or the model is fitted on
        bars whose future it is about to be scored on.
        """
        if self.bars_to_resolution is None:
            return None
        return self.index + self.bars_to_resolution


def _label_atr(candles: Sequence[Any], index: int, period: int) -> Optional[float]:
    """ATR at ``index`` from bars at or before it, over the same fixed window
    the features use.

    The same window matters: a barrier width computed over a different history
    than ``atr_pct`` would mean the label and the feature disagreed about how
    volatile the market was at the same instant.
    """
    low_bound = max(0, index - MAX_LOOKBACK + 1)
    window = candles[low_bound:index + 1]
    if len(window) < period + 1:
        return None
    highs = np.asarray([float(b.high) for b in window], dtype=float)
    lows = np.asarray([float(b.low) for b in window], dtype=float)
    closes = np.asarray([float(b.close) for b in window], dtype=float)
    try:
        value = _num(float(pi.atr(highs, lows, closes, period)[-1]))
    except (pi.InsufficientData, ValueError):
        return None
    if value is None or value <= 0.0:
        return None
    return value


def triple_barrier_label(
    candles: Sequence[Any],
    index: int,
    barriers: Optional[BarrierSpec] = None,
) -> Label:
    """Label bar ``index`` by which barrier the *following* bars touch first.

    Entry is ``candles[index].close``. The scan starts at ``index + 1``: the
    feature bar itself can never resolve its own label, because at the moment
    the features are known the bar is closed and its range is already spent.

    THE PESSIMISTIC RULE
    --------------------
    Within one bar, the stop is checked before the take-profit. A candle
    reports its high and its low, not the order in which they happened; when
    the range spans both barriers, either answer is consistent with the data
    and only one of them is safe. ``backtest.Backtester._resolve_bar`` makes
    the identical choice for a simulated fill, and a label that chose otherwise
    would train a model to expect the fills the simulator refuses to give.

    UNRESOLVED IS NOT A LOSS
    ------------------------
    If the scan runs out of bars before either barrier is touched *and* before
    the horizon expires, the answer is unknown: ``resolved=False``,
    ``outcome=None``. Calling it a timeout would put a fake class-0 label on
    every bar in the last day of the sample; calling it a stop would put a fake
    loss there. Both bias whatever regime the data happens to end in.
    """
    spec = barriers or BarrierSpec()
    total = len(candles)
    if total == 0:
        raise ValueError("candles is empty; there is nothing to label")
    index = int(index)
    if index < 0:
        index += total
    if not (0 <= index < total):
        raise IndexError(f"index {index} is outside 0..{total - 1}")

    entry = float(candles[index].close)
    atr_value = _label_atr(candles, index, spec.atr_period)
    if atr_value is None:
        return Label(
            index=index, outcome=None, resolved=False,
            reason="insufficient_history_for_atr", bars_to_resolution=None,
            entry_price=entry, atr_at_entry=None,
            upper_barrier=None, lower_barrier=None, barrier=None,
        )

    if spec.direction == 1:
        profit_price = entry + spec.take_profit_atr * atr_value
        loss_price = entry - spec.stop_atr * atr_value
    else:
        profit_price = entry - spec.take_profit_atr * atr_value
        loss_price = entry + spec.stop_atr * atr_value
    upper = max(profit_price, loss_price)
    lower = min(profit_price, loss_price)

    horizon_end = index + spec.max_horizon
    scan_end = min(horizon_end, total - 1)

    for future in range(index + 1, scan_end + 1):
        bar = candles[future]
        high = float(bar.high)
        low = float(bar.low)
        if spec.direction == 1:
            hit_loss = low <= loss_price
            hit_profit = high >= profit_price
        else:
            hit_loss = high >= loss_price
            hit_profit = low <= profit_price
        # Loss first. This single ordering *is* the pessimistic both-touched
        # rule; writing it as an explicit "if both: stop" branch would leave the
        # ordering below it free to drift.
        if hit_loss:
            return Label(
                index=index, outcome=LABEL_STOP, resolved=True, reason=None,
                bars_to_resolution=future - index, entry_price=entry,
                atr_at_entry=atr_value, upper_barrier=upper,
                lower_barrier=lower, barrier="stop",
            )
        if hit_profit:
            return Label(
                index=index, outcome=LABEL_TAKE_PROFIT, resolved=True,
                reason=None, bars_to_resolution=future - index,
                entry_price=entry, atr_at_entry=atr_value, upper_barrier=upper,
                lower_barrier=lower, barrier="take_profit",
            )

    if scan_end < horizon_end:
        # We ran out of series, not out of horizon. The answer exists; this
        # data set simply does not contain it.
        return Label(
            index=index, outcome=None, resolved=False,
            reason="horizon_extends_past_the_data", bars_to_resolution=None,
            entry_price=entry, atr_at_entry=atr_value, upper_barrier=upper,
            lower_barrier=lower, barrier=None,
        )

    return Label(
        index=index, outcome=LABEL_TIMEOUT, resolved=True, reason=None,
        bars_to_resolution=spec.max_horizon, entry_price=entry,
        atr_at_entry=atr_value, upper_barrier=upper, lower_barrier=lower,
        barrier="timeout",
    )


def triple_barrier_labels(
    candles: Sequence[Any],
    barriers: Optional[BarrierSpec] = None,
    start: int = 0,
    end: Optional[int] = None,
) -> List[Label]:
    """:func:`triple_barrier_label` over a range, one :class:`Label` per bar.

    Returns unresolved labels too rather than filtering them out — the caller
    that drops them should be able to count what it dropped.
    """
    total = len(candles)
    stop = total if end is None else min(int(end), total)
    begin = max(0, int(start))
    return [
        triple_barrier_label(candles, i, barriers) for i in range(begin, stop)
    ]


# ---------------------------------------------------------------------------
# the batch builder
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DatasetReport:
    """What was kept, what was dropped, and for which reason.

    Returned rather than logged. A training run that does not know it silently
    discarded 40% of its rows — or that the entire book half of its schema was
    unmeasurable — is a training run whose metrics describe a different data
    set than the one the author has in mind.
    """

    bars_seen: int
    rows_kept: int
    dropped_cold: int
    dropped_missing_features: int
    dropped_unresolved_label: int
    missing_by_feature: Mapping[str, int]
    label_counts: Mapping[int, int]
    always_missing: Tuple[str, ...]
    rows_with_book: int
    required: Tuple[str, ...]
    schema_version: str
    schema_digest: str
    barriers: BarrierSpec

    @property
    def dropped_total(self) -> int:
        return (self.dropped_cold + self.dropped_missing_features
                + self.dropped_unresolved_label)

    @property
    def kept_fraction(self) -> Optional[float]:
        return _ratio(self.rows_kept, self.bars_seen)

    @property
    def dropped_fraction(self) -> Optional[float]:
        return _ratio(self.dropped_total, self.bars_seen)

    def label_fractions(self) -> Dict[int, Optional[float]]:
        return {
            outcome: _ratio(count, self.rows_kept)
            for outcome, count in self.label_counts.items()
        }


@dataclass(frozen=True)
class Dataset:
    """(X, y, timestamps) for supervised learning, plus what purging needs.

    ``X`` is ``float64`` with one column per declared feature, in schema order.
    ``nan`` appears only in columns the caller did not require — on a corpus
    with no order book that is the three book columns, and their being ``nan``
    rather than ``0.0`` is the whole point: a trainer that cannot handle them
    should fail on them, not learn from a fabricated zero.

    ``bars_to_resolution`` and :attr:`outcome_end_index` are carried so a
    cross-validation split can purge overlapping samples. They are not
    optional extras; without them a triple-barrier data set trains on rows
    whose outcomes were determined by the bars in the test fold.
    """

    X: np.ndarray
    y: np.ndarray
    timestamps: np.ndarray
    bar_index: np.ndarray
    bars_to_resolution: np.ndarray
    feature_names: Tuple[str, ...]
    schema_version: str
    schema_digest: str
    barriers: BarrierSpec
    report: DatasetReport

    def __len__(self) -> int:
        return int(self.X.shape[0])

    @property
    def n_features(self) -> int:
        return int(self.X.shape[1])

    @property
    def outcome_end_index(self) -> np.ndarray:
        """Last source-bar index each row's label depended on."""
        return self.bar_index + self.bars_to_resolution

    def column(self, name: str) -> np.ndarray:
        if name not in FEATURE_INDEX:
            raise KeyError(f"{name!r} is not a declared feature")
        return self.X[:, FEATURE_INDEX[name]]

    def usable_columns(self) -> Tuple[str, ...]:
        """Feature names with no ``nan`` anywhere — the columns a plain
        estimator can consume without a missing-value strategy."""
        if len(self) == 0:
            return ()
        finite = ~np.isnan(self.X).any(axis=0)
        return tuple(
            name for name, ok in zip(self.feature_names, finite) if bool(ok)
        )


def build_dataset(
    bars: Sequence[Any],
    books: Optional[Sequence[Any]] = None,
    barriers: Optional[BarrierSpec] = None,
    require: Optional[Sequence[str]] = None,
    start: Optional[int] = None,
    end: Optional[int] = None,
    require_warmup: bool = True,
) -> Dataset:
    """Build the training matrices, one bar at a time, through
    :func:`compute_features`.

    WHY BAR BY BAR
    --------------
    A vectorised builder would be faster and would be a second implementation
    of every feature. The moment there are two, they agree on the easy ones and
    diverge on the path-dependent ones, and the divergence is invisible until a
    live model underperforms its backtest by an amount nobody can attribute.
    Row *i* of ``X`` here is literally ``compute_features(bars, i).to_array()``,
    and ``tests/test_features.py`` asserts it element for element.

    ``books`` is index-aligned with ``bars`` — entry *i* is the book at the
    close of bar *i*, or ``None``. That is exactly what
    ``market_data.load_corpus`` returns, and its alignment comes from
    ``market_data.replay``, which cannot read past a bar's close.

    ``require`` names the features a row must have to be kept; the default is
    every feature that does not need a book, which is the correct default for
    an OHLCV-only corpus. Naming a book feature in ``require`` when ``books``
    is ``None`` drops every row — correctly, and the report says so, because
    the alternative is training on a column of zeros.

    Rows are dropped, never patched, for three reasons, counted separately in
    :class:`DatasetReport`: too little history (cold), a required feature that
    could not be measured, and a label that cannot resolve inside the data.
    """
    spec = barriers or BarrierSpec()
    total = len(bars)
    if books is not None and len(books) != total:
        raise ValueError(
            f"books has {len(books)} entries for {total} bars; an unaligned "
            "book would attach one bar's liquidity to another bar's features"
        )

    if require is None:
        required: Tuple[str, ...] = PRICE_FEATURE_NAMES
    else:
        required = tuple(require)
        unknown = [name for name in required if name not in FEATURE_INDEX]
        if unknown:
            raise ValueError(
                f"required features are not in the schema: {unknown}. A typo "
                "here would silently require nothing."
            )
    required_set = set(required)

    begin = 0 if start is None else max(0, int(start))
    stop = total if end is None else min(int(end), total)

    rows: List[np.ndarray] = []
    labels: List[int] = []
    stamps: List[int] = []
    indices: List[int] = []
    horizons: List[int] = []

    dropped_cold = 0
    dropped_missing = 0
    dropped_unresolved = 0
    rows_with_book = 0
    missing_by_feature: Dict[str, int] = {name: 0 for name in FEATURE_NAMES}
    label_counts: Dict[int, int] = {
        LABEL_TAKE_PROFIT: 0, LABEL_STOP: 0, LABEL_TIMEOUT: 0,
    }
    seen = 0

    for i in range(begin, stop):
        seen += 1
        book = books[i] if books is not None else None
        vector = compute_features(bars, i, book)
        for name in vector.missing:
            missing_by_feature[name] += 1
        if vector.book_available and not (
            set(vector.missing) >= set(BOOK_FEATURE_NAMES)
        ):
            rows_with_book += 1

        if require_warmup and not vector.warm:
            dropped_cold += 1
            continue
        if required_set & set(vector.missing):
            dropped_missing += 1
            continue

        label = triple_barrier_label(bars, i, spec)
        if not label.resolved or label.outcome is None:
            dropped_unresolved += 1
            continue

        rows.append(vector.to_array())
        labels.append(int(label.outcome))
        stamps.append(int(vector.timestamp_ms))
        indices.append(i)
        horizons.append(int(label.bars_to_resolution or 0))
        label_counts[int(label.outcome)] += 1

    width = len(FEATURE_NAMES)
    matrix = (
        np.vstack(rows) if rows else np.empty((0, width), dtype=np.float64)
    )
    report = DatasetReport(
        bars_seen=seen,
        rows_kept=len(rows),
        dropped_cold=dropped_cold,
        dropped_missing_features=dropped_missing,
        dropped_unresolved_label=dropped_unresolved,
        missing_by_feature=MappingProxyType(dict(missing_by_feature)),
        label_counts=MappingProxyType(dict(label_counts)),
        always_missing=tuple(
            name for name in FEATURE_NAMES
            if seen and missing_by_feature[name] == seen
        ),
        rows_with_book=rows_with_book,
        required=required,
        schema_version=FEATURE_SCHEMA_VERSION,
        schema_digest=FEATURE_SCHEMA_DIGEST,
        barriers=spec,
    )
    return Dataset(
        X=matrix,
        y=np.asarray(labels, dtype=np.int8),
        timestamps=np.asarray(stamps, dtype=np.int64),
        bar_index=np.asarray(indices, dtype=np.int64),
        bars_to_resolution=np.asarray(horizons, dtype=np.int32),
        feature_names=FEATURE_NAMES,
        schema_version=FEATURE_SCHEMA_VERSION,
        schema_digest=FEATURE_SCHEMA_DIGEST,
        barriers=spec,
        report=report,
    )
