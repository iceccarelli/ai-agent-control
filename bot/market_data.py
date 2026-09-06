"""market_data.py — validating loader and microstructure feature layer.

WHY THIS EXISTS
===============
Everything above this module has been taught to fail closed. `risk_management`
has zero ``except: return True``. `bybit_connection` never fabricates an
instrument filter. `pure_indicators` raises rather than returning a padded
array, because a zero ATR is not a small stop — it is no stop.

Market data was the one remaining place where a plausible-looking number could
be invented and nobody downstream would know. A mid computed from a one-sided
book is a number. A spread computed from a crossed book is a number. Slippage
extrapolated past the last level of a book is a number. All three arrive at the
risk gates looking exactly like a measurement, and all three are guesses. This
module is where they are refused.

THE RULE
--------
**Every quantity here is either measured or ``None``.** Never interpolated,
never extrapolated, never defaulted. If the book is empty, one-sided, crossed,
or too thin to absorb the size being asked about, the answer is ``None`` and
the caller decides what to do with not knowing. That is strictly more useful
than a confident number, because the caller can branch on ``None``; it cannot
branch on "this 43.2 bps is fictional".

REJECTIONS ARE DATA
-------------------
``load_ohlcv`` does not silently drop bad rows. Silent drops are how a series
acquires a hole that later reads as a gap, a volatility spike, or a stop that
never triggered. Every rejected row comes back in :class:`OhlcvLoad.rejections`
with its line number, its reason, and the raw text — and the result object
deliberately does **not** behave like a list, so a caller cannot write
``for bar in load_ohlcv(path)`` and never learn that a fifth of the file was
discarded. You have to ask for ``.bars``, which is the moment you also see
``.rejections`` sitting next to it.

NO LOOKAHEAD, STRUCTURALLY
--------------------------
:func:`replay` is the only sanctioned way to walk bars and book together. It
holds one forward cursor over the event stream and advances it to each bar's
close before yielding — it is not possible for it to see a later event, because
it has not read one. It cuts on ``time_coinapi``, not ``time_exchange``: the
question a backtest must answer is not "when did this happen" but "when could
this process have known", and the gap between those is exactly the collection
latency the corpus models.

DEPENDENCY DIRECTION
--------------------
Imports are stdlib + numpy + ``config``, and nothing else from the repaired
stack. In particular this module does **not** import ``backtest``, even though
:class:`Bar` is deliberately field-compatible with ``backtest.Bar`` — importing
it would drag in ``persistence``, ``risk_management``, ``position_sizing``,
``bybit_connection``, ``technical_analysis`` and ``trading_engine`` through
that one line, and the arrows in INTEGRATION_MAP §1 would stop pointing
downward. Compatibility here is structural, not inherited:
``Bar(start_ms, open, high, low, close, volume)`` positionally matches
``backtest.Bar``, so bars loaded here feed the backtester directly, and
``tests/test_market_data.py`` asserts the field order still matches so the
compatibility cannot rot silently.
"""

from __future__ import annotations

import csv
import gzip
import hashlib
import io
import json
import logging
import math
import os
import re
from dataclasses import dataclass, field
from typing import (
    Any, Dict, Iterable, Iterator, List, Mapping, NamedTuple, Optional,
    Sequence, Tuple,
)

import numpy as np

import config as _config

logger = logging.getLogger("market_data")

__all__ = [
    "MarketDataError",
    "ManifestError",
    "BookIntegrityError",
    "RejectionReason",
    "Rejection",
    "Bar",
    "OhlcvLoad",
    "Quote",
    "QuoteLoad",
    "BookEvent",
    "BookEventLoad",
    "BookLevel",
    "BookState",
    "BookFeatures",
    "ReplayStep",
    "ManifestReport",
    "DATA_ROOT",
    "UPDATE_TYPES",
    "PRICE_DECIMALS",
    "load_ohlcv",
    "load_quotes",
    "load_book_events",
    "replay",
    "verify_manifest",
    "parse_timestamp",
    "format_timestamp",
    "normalize_price",
    "spread_bps",
    "is_spread_anomalous",
]


class MarketDataError(ValueError):
    """A data file could not be used. Raised, never returned as a sentinel."""


class ManifestError(MarketDataError):
    """The shipped manifest is missing, malformed, or disagrees with the disk."""


class BookIntegrityError(MarketDataError):
    """An L2 event cannot be applied to the book without inventing something.

    Subtracting more size than a level holds, or touching a level that does not
    exist, means the consumer's book and the exchange's book have diverged. The
    tempting response is to clamp at zero and carry on; that produces a book
    which is quietly wrong in an unknown direction for an unknown duration,
    which is worse than a loud stop.
    """


#: Where the shipped corpus lives, relative to this file. Not read from the
#: environment: `config.load()` is the only environment reader in this
#: codebase (README "The invariants"), and a data path is not worth an
#: exception to that rule. Every entry point takes an explicit ``root``.
DATA_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")

#: The CoinAPI L2 vocabulary. Anything else is rejected rather than guessed at:
#: an unrecognised update type applied as a no-op leaves the book silently
#: stale, and applied as a SET corrupts it.
UPDATE_TYPES = frozenset({"SNAPSHOT", "ADD", "SUB", "MATCH", "DELETE", "SET"})

#: Prices and sizes are normalised to 9 decimals throughout. Level identity in
#: the book is a float key, and two events meaning the same price must produce
#: the same key or the second one silently creates a phantom level.
PRICE_DECIMALS = 9

_OHLCV_COLUMNS = (
    "time_period_start", "time_period_end", "time_open", "time_close",
    "price_open", "price_high", "price_low", "price_close",
    "volume_traded", "trades_count",
)
_QUOTE_COLUMNS = (
    "symbol_id", "time_exchange", "time_coinapi",
    "ask_px", "ask_sx", "bid_px", "bid_sx",
)
_BOOK_COLUMNS = (
    "symbol_id", "time_exchange", "time_coinapi", "is_buy",
    "entry_px", "entry_sx", "update_type",
)

#: Strict ISO 8601 UTC. Deliberately not `datetime.fromisoformat`: before 3.11
#: it rejects the trailing ``Z``, and in every version it rejects CoinAPI's
#: seven fractional digits. A parser that is lenient about the timezone marker
#: is a parser that will one day read a local-time file as UTC and shift the
#: whole series by hours without a word.
_TIMESTAMP_RE = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,9}))?"
    r"(?:Z|\+00:?00)$"
)

_DAYS_BEFORE_MONTH = (0, 0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334)


class RejectionReason:
    """The vocabulary of refusals.

    Constants rather than free-form strings so a test can assert on the exact
    reason and a caller can branch on it. A rejection whose reason is prose is
    a rejection nobody can act on programmatically.
    """

    MISSING_COLUMN = "MISSING_COLUMN"
    MALFORMED_ROW = "MALFORMED_ROW"
    BAD_TIMESTAMP = "BAD_TIMESTAMP"
    BAD_NUMBER = "BAD_NUMBER"
    NON_FINITE_VALUE = "NON_FINITE_VALUE"
    NEGATIVE_VALUE = "NEGATIVE_VALUE"
    NON_POSITIVE_PRICE = "NON_POSITIVE_PRICE"
    DUPLICATE_TIMESTAMP = "DUPLICATE_TIMESTAMP"
    NON_MONOTONIC_TIMESTAMP = "NON_MONOTONIC_TIMESTAMP"
    HIGH_LOW_NOT_BRACKETING = "HIGH_LOW_NOT_BRACKETING"
    PERIOD_NOT_POSITIVE = "PERIOD_NOT_POSITIVE"
    INTERVAL_MISMATCH = "INTERVAL_MISMATCH"
    TRADE_TIME_OUTSIDE_PERIOD = "TRADE_TIME_OUTSIDE_PERIOD"
    CROSSED_BOOK = "CROSSED_BOOK"
    SPREAD_ANOMALY = "SPREAD_ANOMALY"
    ZERO_SIZE = "ZERO_SIZE"
    UNKNOWN_UPDATE_TYPE = "UNKNOWN_UPDATE_TYPE"
    NEGATIVE_LATENCY = "NEGATIVE_LATENCY"
    BAD_BOOLEAN = "BAD_BOOLEAN"

    #: Every reason this module can produce. Asserted complete by the tests, so
    #: a new reason cannot be added without appearing here.
    ALL = frozenset({
        MISSING_COLUMN, MALFORMED_ROW, BAD_TIMESTAMP, BAD_NUMBER,
        NON_FINITE_VALUE, NEGATIVE_VALUE, NON_POSITIVE_PRICE,
        DUPLICATE_TIMESTAMP, NON_MONOTONIC_TIMESTAMP, HIGH_LOW_NOT_BRACKETING,
        PERIOD_NOT_POSITIVE, INTERVAL_MISMATCH, TRADE_TIME_OUTSIDE_PERIOD,
        CROSSED_BOOK,
        SPREAD_ANOMALY, ZERO_SIZE, UNKNOWN_UPDATE_TYPE, NEGATIVE_LATENCY,
        BAD_BOOLEAN,
    })


@dataclass(frozen=True)
class Rejection:
    """One refused row, with everything needed to go and look at it.

    ``line`` is the 1-based line number *in the file*, header included, because
    that is the number an editor shows. ``raw`` is the row as read, so a
    rejection can be diagnosed without re-opening the source.
    """

    path: str
    line: int
    reason: str
    detail: str
    raw: Tuple[str, ...] = ()

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: {self.reason}: {self.detail}"


# ---------------------------------------------------------------------------
# primitives
# ---------------------------------------------------------------------------


def parse_timestamp(text: str) -> int:
    """ISO 8601 UTC -> epoch **microseconds**. Raises on anything else.

    Accepts one to nine fractional digits (CoinAPI writes seven, for 100ns
    ticks). Sub-microsecond digits are truncated, not rounded: rounding could
    move an event across a bar boundary, and a boundary crossing is precisely
    what the no-lookahead cut depends on. 100ns of truncation cannot matter to
    an hourly strategy; a silently rounded-up event can.

    Microseconds, not a ``datetime``: integers compare and sort exactly, and
    the whole module's ordering guarantees rest on that.

    **UTC only, and only the spellings that mean UTC and nothing else**:
    ``Z``, ``+00:00`` and ``+0000``. A non-zero offset is still rejected — this
    parser does not convert, and a series half in one zone would be silently
    misaligned rather than loudly broken. ``-00:00`` is rejected too: RFC 3339
    gives it the distinct meaning "the local offset is unknown", which is not a
    claim of UTC.

    The ``+00:00`` spelling was added in slice 38, after a human-supplied
    extension to the Bitstamp daily corpus wrote it and all 571 new bars were
    silently dropped as BAD_TIMESTAMP while the intake report still called the
    corpus eligible. Widening the parser to a second spelling of the same
    instant is a correctness fix; widening it to offsets would not be.
    """
    if not isinstance(text, str):
        raise MarketDataError(f"timestamp must be a string, got {type(text).__name__}")

    # Fast path for the canonical 28-character form the corpus (and CoinAPI)
    # actually writes. This function runs twice per row over hundreds of
    # thousands of book events, and the regex dominated that loop. The
    # separators are checked explicitly rather than assumed, so this path
    # accepts strictly less than the regex does and anything it does not
    # recognise falls through to the general path below — a fast path that
    # could accept something the strict parser would reject would be a way to
    # smuggle a malformed timestamp in through a performance optimisation.
    if (len(text) == 28 and text[4] == "-" and text[7] == "-"
            and text[10] == "T" and text[13] == ":" and text[16] == ":"
            and text[19] == "." and text[27] == "Z"):
        try:
            year = int(text[0:4])
            month = int(text[5:7])
            day = int(text[8:10])
            hour = int(text[11:13])
            minute = int(text[14:16])
            second = int(text[17:19])
            nanos = int(text[20:27]) * 100
        except ValueError:
            pass
        else:
            if (1 <= month <= 12 and 1 <= day <= _days_in_month(year, month)
                    and hour <= 23 and minute <= 59 and second <= 60):
                return (
                    (_days_from_civil(year, month, day) * 86_400
                     + hour * 3_600 + minute * 60 + second) * 1_000_000
                    + nanos // 1_000
                )

    match = _TIMESTAMP_RE.match(text.strip())
    if match is None:
        raise MarketDataError(
            f"not an ISO 8601 UTC timestamp: {text!r} "
            f"(expected e.g. 2024-03-01T00:00:00.0000000Z)"
        )
    year, month, day, hour, minute, second = (int(match.group(i)) for i in range(1, 7))
    if not 1 <= month <= 12:
        raise MarketDataError(f"month out of range in {text!r}")
    if not 1 <= day <= _days_in_month(year, month):
        raise MarketDataError(f"day out of range in {text!r}")
    if hour > 23 or minute > 59 or second > 60:
        raise MarketDataError(f"time out of range in {text!r}")
    fraction = match.group(7) or ""
    nanos = int((fraction + "000000000")[:9]) if fraction else 0
    days = _days_from_civil(year, month, day)
    return (
        (days * 86_400 + hour * 3_600 + minute * 60 + second) * 1_000_000
        + nanos // 1_000
    )


def _days_in_month(year: int, month: int) -> int:
    if month == 2:
        leap = year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)
        return 29 if leap else 28
    return 30 if month in (4, 6, 9, 11) else 31


def _days_from_civil(year: int, month: int, day: int) -> int:
    """Days since 1970-01-01, proleptic Gregorian. Pure arithmetic, no tz."""
    leaps = 0
    if year >= 1970:
        leaps = _leaps_before(year) - _leaps_before(1970)
        days = (year - 1970) * 365 + leaps
    else:                                       # pragma: no cover - corpus is post-1970
        leaps = _leaps_before(1970) - _leaps_before(year)
        days = -((1970 - year) * 365 + leaps)
    days += _DAYS_BEFORE_MONTH[month] + (day - 1)
    if month > 2 and (year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)):
        days += 1
    return days


def _leaps_before(year: int) -> int:
    y = year - 1
    return y // 4 - y // 100 + y // 400


def format_timestamp(micros: int) -> str:
    """Inverse of :func:`parse_timestamp`, in the corpus's seven-digit form."""
    if micros < 0:
        raise MarketDataError("timestamps before the epoch are not supported")
    days, rest = divmod(int(micros), 86_400_000_000)
    seconds, fraction = divmod(rest, 1_000_000)
    hour, remainder = divmod(seconds, 3_600)
    minute, second = divmod(remainder, 60)
    year, month, day = _civil_from_days(days)
    return (
        f"{year:04d}-{month:02d}-{day:02d}T{hour:02d}:{minute:02d}:{second:02d}"
        f".{fraction * 10:07d}Z"
    )


def _civil_from_days(days: int) -> Tuple[int, int, int]:
    year = 1970 + days // 366
    while _days_from_civil(year + 1, 1, 1) <= days:
        year += 1
    remaining = days - _days_from_civil(year, 1, 1)
    month = 1
    while month < 12 and remaining >= _days_in_month(year, month):
        remaining -= _days_in_month(year, month)
        month += 1
    return year, month, remaining + 1


def normalize_price(value: float) -> float:
    """Round to :data:`PRICE_DECIMALS`. The book's level identity depends on it.

    ``62000.1`` parsed from two different rows must land on the same float or
    the second row creates a phantom level next to the first. Rounding to a
    fixed place is what makes ``dict[float]`` a safe index here.
    """
    return round(float(value), PRICE_DECIMALS)


def spread_bps(bid: float, ask: float) -> Optional[float]:
    """(ask - bid) / mid in basis points, or ``None`` if the quote is unusable.

    ``None`` for a crossed or non-positive quote rather than a negative number:
    a negative spread flows into a cost calculation as a *credit*, which turns
    a broken feed into a reason to trade more.
    """
    if not (math.isfinite(bid) and math.isfinite(ask)):
        return None
    if bid <= 0.0 or ask <= 0.0 or ask <= bid:
        return None
    return (ask - bid) / ((ask + bid) / 2.0) * 10_000.0


def is_spread_anomalous(bid: float, ask: float,
                        cfg: Optional[Any] = None) -> bool:
    """True if this quote should not be traded on at all.

    Uses ``cfg.MAX_SPREAD_ANOMALY_PCT`` (a **fraction**, default 0.67 — the
    repo-wide convention from README "The invariants": every ``*_PCT`` is a
    fraction and >1.0 is rejected at load time). This is a much blunter
    instrument than ``risk_management.MAX_SPREAD_BPS``, and the two are not
    redundant: the risk gate asks "is this spread too expensive to cross?",
    which is a trading decision at tens of bps. This asks "is this quote a
    quote at all?", which is a data-integrity decision at tens of *percent*. A
    67%-of-mid spread is not an expensive market, it is a broken feed, an
    auction, or a delisted pair, and it must never reach a sizing calculation.

    Fails closed: anything non-finite, non-positive or crossed is anomalous.
    """
    limit = float(getattr(cfg or _config.get_config_object(),
                          "MAX_SPREAD_ANOMALY_PCT", 0.67))
    if not (math.isfinite(bid) and math.isfinite(ask)):
        return True
    if bid <= 0.0 or ask <= 0.0 or ask <= bid:
        return True
    mid = (ask + bid) / 2.0
    return (ask - bid) / mid > limit


def _open_text(path: str) -> io.TextIOBase:
    """Open plain or gzipped CSV transparently, by extension."""
    if path.endswith(".gz"):
        return gzip.open(path, "rt", encoding="utf-8", newline="")
    return open(path, "rt", encoding="utf-8", newline="")


def _check_header(path: str, header: Optional[Sequence[str]],
                  expected: Sequence[str]) -> None:
    """A wrong header is fatal, not a per-row rejection.

    Every row would fail for the same reason, and 5,000 identical rejections
    hide the one fact that matters: the file is not the file you think it is.
    Column *order* is required as well as membership, because these loaders
    read by position for speed and a reordered file would parse cleanly into
    nonsense.
    """
    if header is None:
        raise MarketDataError(f"{path}: file is empty")
    got = tuple(name.strip().lstrip("﻿") for name in header)
    if got != tuple(expected):
        missing = [name for name in expected if name not in got]
        raise MarketDataError(
            f"{path}: header is not the expected schema. "
            f"missing={missing or 'none'} got={list(got)}"
        )


def _finite(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"non-finite: {value!r}")
    return number


def _optional_trades_count(value: str) -> int:
    """``trades_count`` only: an empty cell means "not reported", so it is 0.

    This is the single column in the OHLCV shape that carries no decision. It
    is 0 for every one of the 2,564 bars of the Bitstamp daily corpus — the
    1-minute archive it was built from does not report trade counts — so a
    blank is the same information the corpus has always had, spelled honestly
    instead of as a false zero. Nothing reads it: no gate, no fill, no feature.

    It is deliberately NOT applied to the other nine columns. A blank price or
    a blank volume is a corrupt row and must stay fatal: an empty close
    silently becoming 0.0 is precisely the class of bug the loader exists to
    catch, and a blank column is indistinguishable from a truncated write.

    Slice 38: the human's Bitstamp extension writes ``""`` here while the older
    rows write ``"0"``. All 571 new bars were being rejected as BAD_NUMBER,
    which is how a corpus can pass its intake report and still lose every bar
    that mattered.
    """
    text = value.strip()
    if not text:
        return 0
    return int(float(text))


# ---------------------------------------------------------------------------
# OHLCV
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Bar:
    """One closed OHLCV candle.

    The first six fields are positionally identical to ``backtest.Bar``
    (``start_ms, open, high, low, close, volume``) so a loaded series drops
    straight into the backtester, without this module importing it and
    inverting the dependency graph. The extra fields carry what CoinAPI gives
    that Bybit's kline does not, and all default, so the positional
    compatibility is not disturbed.

    ``start_ms`` is epoch **milliseconds** to match ``backtest.Bar``;
    ``start_us``/``end_us`` keep the microsecond precision the file actually
    carries, because the book is timestamped in microseconds and the
    no-lookahead cut is made at a bar boundary.
    """

    start_ms: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    end_us: int = 0
    start_us: int = 0
    trades: int = 0

    @property
    def open_us(self) -> int:
        """Bar start in microseconds, falling back to ``start_ms``.

        A ``Bar`` built the ``backtest.Bar`` way — six positional arguments —
        leaves ``start_us`` at its default of 0. Without this fallback every
        such bar would claim to start at the epoch, and :func:`replay` would
        reject the series as non-monotonic. Everything that needs a bar's
        position in time goes through here rather than reading the field.
        """
        return self.start_us or self.start_ms * 1_000

    @property
    def close_us(self) -> int:
        """Bar end in microseconds, falling back to the start.

        Falling back to the *start*, not to start + some assumed interval: this
        module does not know the bar's period unless the file said so, and
        inventing one would move the no-lookahead cut to a time the data never
        claimed. Erring toward the earlier cut can only ever show the replay
        less information, which is the safe direction to be wrong in.
        """
        return self.end_us or self.open_us

    @property
    def end_ms(self) -> int:
        return self.close_us // 1_000

    def as_kline_row(self) -> List[str]:
        """Bybit V5 kline row shape — same as ``backtest.Bar.as_kline_row``."""
        return [
            str(self.start_ms), str(self.open), str(self.high),
            str(self.low), str(self.close), str(self.volume), "0",
        ]


@dataclass(frozen=True)
class OhlcvLoad:
    """Bars **and** the rows that were refused, deliberately not a list.

    This type does not implement ``__iter__``. That is the whole design: if it
    were iterable, ``for bar in load_ohlcv(path)`` would work, and the day a
    vendor file arrives with 900 malformed rows nobody would find out. Asking
    for ``.bars`` costs one attribute access and puts ``.rejections`` in the
    same line of sight.
    """

    path: str
    bars: List[Bar]
    rejections: List[Rejection]
    rows_read: int

    @property
    def ok(self) -> bool:
        return not self.rejections

    @property
    def rejection_reasons(self) -> Dict[str, int]:
        """Reason -> count, for logging a summary without dumping every row."""
        counts: Dict[str, int] = {}
        for rejection in self.rejections:
            counts[rejection.reason] = counts.get(rejection.reason, 0) + 1
        return counts

    def raise_if_rejected(self) -> "OhlcvLoad":
        """For callers that want a hard failure. Returns self so it chains."""
        if self.rejections:
            raise MarketDataError(
                f"{self.path}: {len(self.rejections)} rejected row(s): "
                f"{self.rejection_reasons}; first is {self.rejections[0]}"
            )
        return self

    def closes(self) -> np.ndarray:
        return np.array([bar.close for bar in self.bars], dtype=float)

    def highs(self) -> np.ndarray:
        return np.array([bar.high for bar in self.bars], dtype=float)

    def lows(self) -> np.ndarray:
        return np.array([bar.low for bar in self.bars], dtype=float)


def load_ohlcv(path: str, *, strict: bool = False,
               expected_interval_seconds: Optional[int] = None) -> OhlcvLoad:
    """Load a CoinAPI-shaped OHLCV file, refusing every row that cannot be trusted.

    Enforced per row, each with its own reason:

    * the ten columns parse as numbers, and every number is finite;
    * prices are strictly positive and volume is not negative;
    * ``high >= max(open, close)`` and ``low <= min(open, close)`` — a bar whose
      extremes do not bracket its body is not a bar, and the backtester's
      intrabar stop fill reads exactly those two fields, so a bad one produces
      a fill at a price that never traded;
    * ``time_period_end > time_period_start``;
    * the trade times fall inside the period;
    * timestamps strictly increase, so duplicates and out-of-order rows are
      both caught and are reported as *different* reasons — a duplicate is
      usually a double-delivered message, an inversion is usually a sort that
      was never applied, and the fixes are not the same.

    Rows are **not** sorted before checking. Sorting first would silently
    repair an out-of-order file and destroy the evidence that the source is
    unordered — and an unordered source is a source that may also be
    incomplete.

    Set ``strict=True`` to raise on the first problem instead of collecting.
    """
    bars: List[Bar] = []
    rejections: List[Rejection] = []
    rows_read = 0
    last_start: Optional[int] = None
    seen: Dict[int, int] = {}

    def reject(line: int, reason: str, detail: str, raw: Sequence[str]) -> None:
        rejection = Rejection(path, line, reason, detail, tuple(raw))
        if strict:
            raise MarketDataError(str(rejection))
        rejections.append(rejection)

    with _open_text(path) as handle:
        reader = csv.reader(handle)
        _check_header(path, next(reader, None), _OHLCV_COLUMNS)
        for line, row in enumerate(reader, start=2):
            if not row or all(not cell.strip() for cell in row):
                continue
            rows_read += 1
            if len(row) != len(_OHLCV_COLUMNS):
                reject(line, RejectionReason.MALFORMED_ROW,
                       f"expected {len(_OHLCV_COLUMNS)} columns, got {len(row)}", row)
                continue
            try:
                start_us = parse_timestamp(row[0])
                end_us = parse_timestamp(row[1])
                open_us = parse_timestamp(row[2])
                close_us = parse_timestamp(row[3])
            except MarketDataError as exc:
                reject(line, RejectionReason.BAD_TIMESTAMP, str(exc), row)
                continue
            try:
                opened = _finite(row[4])
                high = _finite(row[5])
                low = _finite(row[6])
                closed = _finite(row[7])
                volume = _finite(row[8])
                trades = _optional_trades_count(row[9])
            except ValueError as exc:
                reason = (RejectionReason.NON_FINITE_VALUE
                          if "non-finite" in str(exc)
                          or _looks_non_finite(row[4:9])
                          else RejectionReason.BAD_NUMBER)
                reject(line, reason, str(exc), row)
                continue

            if end_us <= start_us:
                reject(line, RejectionReason.PERIOD_NOT_POSITIVE,
                       f"period end {row[1]} is not after start {row[0]}", row)
                continue
            if min(opened, high, low, closed) <= 0.0:
                reject(line, RejectionReason.NON_POSITIVE_PRICE,
                       f"price <= 0 in O={opened} H={high} L={low} C={closed}", row)
                continue
            if volume < 0.0 or trades < 0:
                reject(line, RejectionReason.NEGATIVE_VALUE,
                       f"volume={volume} trades={trades}", row)
                continue
            if high < max(opened, closed) or low > min(opened, closed):
                reject(line, RejectionReason.HIGH_LOW_NOT_BRACKETING,
                       f"H={high} L={low} do not bracket O={opened} C={closed}", row)
                continue
            if not (start_us <= open_us <= end_us and start_us <= close_us <= end_us):
                reject(line, RejectionReason.TRADE_TIME_OUTSIDE_PERIOD,
                       f"first/last trade {row[2]}/{row[3]} outside "
                       f"[{row[0]}, {row[1]}]", row)
                continue
            if start_us in seen:
                reject(line, RejectionReason.DUPLICATE_TIMESTAMP,
                       f"period start {row[0]} already seen at line {seen[start_us]}",
                       row)
                continue
            if last_start is not None and start_us < last_start:
                reject(line, RejectionReason.NON_MONOTONIC_TIMESTAMP,
                       f"period start {row[0]} precedes the previous row's "
                       f"{format_timestamp(last_start)}", row)
                continue
            if (expected_interval_seconds is not None
                    and end_us - start_us != expected_interval_seconds * 1_000_000):
                reject(line, RejectionReason.INTERVAL_MISMATCH,
                       f"period is {(end_us - start_us) / 1e6:g}s, expected "
                       f"{expected_interval_seconds}s", row)
                continue

            seen[start_us] = line
            last_start = start_us
            bars.append(Bar(
                start_ms=start_us // 1_000,
                open=normalize_price(opened), high=normalize_price(high),
                low=normalize_price(low), close=normalize_price(closed),
                volume=normalize_price(volume),
                end_us=end_us, start_us=start_us, trades=trades,
            ))

    result = OhlcvLoad(path, bars, rejections, rows_read)
    if rejections:
        logger.warning("%s: kept %d bars, rejected %d rows: %s",
                       path, len(bars), len(rejections), result.rejection_reasons)
    return result


def _looks_non_finite(cells: Sequence[str]) -> bool:
    """Distinguish 'NaN'/'inf' from genuine garbage, for a precise reason code."""
    return any(cell.strip().lower().lstrip("+-") in ("nan", "inf", "infinity")
               for cell in cells)


# ---------------------------------------------------------------------------
# quotes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Quote:
    """One top-of-book snapshot.

    ``time_coinapi_us`` is when this process could have known, and is what the
    replay cut uses; ``time_exchange_us`` is when it happened. Keeping both is
    the point — collapsing them is how a backtest quietly gains a few hundred
    microseconds of prescience.
    """

    symbol_id: str
    time_exchange_us: int
    time_coinapi_us: int
    ask_px: float
    ask_sx: float
    bid_px: float
    bid_sx: float

    @property
    def mid(self) -> Optional[float]:
        if spread_bps(self.bid_px, self.ask_px) is None:
            return None
        return normalize_price((self.bid_px + self.ask_px) / 2.0)

    @property
    def spread_bps(self) -> Optional[float]:
        return spread_bps(self.bid_px, self.ask_px)

    @property
    def microprice(self) -> Optional[float]:
        """Size-weighted mid: the touch leans toward the *thinner* side.

        Weighting bid price by ask size (and vice versa) is not a typo — it is
        the definition. A big bid and a small ask means the next trade is more
        likely to lift the ask, so fair value sits nearer the ask.
        """
        if spread_bps(self.bid_px, self.ask_px) is None:
            return None
        total = self.bid_sx + self.ask_sx
        if total <= 0.0:
            return None
        return normalize_price(
            (self.bid_px * self.ask_sx + self.ask_px * self.bid_sx) / total
        )


@dataclass(frozen=True)
class QuoteLoad:
    path: str
    quotes: List[Quote]
    rejections: List[Rejection]
    rows_read: int

    @property
    def ok(self) -> bool:
        return not self.rejections

    @property
    def rejection_reasons(self) -> Dict[str, int]:
        counts: Dict[str, int] = {}
        for rejection in self.rejections:
            counts[rejection.reason] = counts.get(rejection.reason, 0) + 1
        return counts

    def raise_if_rejected(self) -> "QuoteLoad":
        if self.rejections:
            raise MarketDataError(
                f"{self.path}: {len(self.rejections)} rejected row(s): "
                f"{self.rejection_reasons}; first is {self.rejections[0]}"
            )
        return self


def load_quotes(path: str, *, strict: bool = False,
                cfg: Optional[Any] = None) -> QuoteLoad:
    """Load top-of-book quotes, refusing anything that is not a tradeable quote.

    Beyond the usual parse and finiteness checks:

    * **crossed** (``bid >= ask``) is rejected, not normalised by swapping. A
      swap invents a quote that was never published; the honest reading of a
      crossed book is "I do not know where the market is";
    * **spread beyond ``MAX_SPREAD_ANOMALY_PCT``** is rejected as a broken feed;
    * **zero size on either side** is rejected — a price with no size behind it
      is not a price you can trade, and it is the single most effective way to
      make a slippage model look free;
    * ``time_coinapi < time_exchange`` is rejected as ``NEGATIVE_LATENCY``: a
      collector cannot receive a message before it was sent, so the row is
      evidence of a clock or parse fault somewhere upstream, and every other
      field in it is now suspect.
    """
    quotes: List[Quote] = []
    rejections: List[Rejection] = []
    rows_read = 0
    last_exchange: Optional[int] = None

    def reject(line: int, reason: str, detail: str, raw: Sequence[str]) -> None:
        rejection = Rejection(path, line, reason, detail, tuple(raw))
        if strict:
            raise MarketDataError(str(rejection))
        rejections.append(rejection)

    with _open_text(path) as handle:
        reader = csv.reader(handle)
        _check_header(path, next(reader, None), _QUOTE_COLUMNS)
        for line, row in enumerate(reader, start=2):
            if not row or all(not cell.strip() for cell in row):
                continue
            rows_read += 1
            if len(row) != len(_QUOTE_COLUMNS):
                reject(line, RejectionReason.MALFORMED_ROW,
                       f"expected {len(_QUOTE_COLUMNS)} columns, got {len(row)}", row)
                continue
            try:
                exchange_us = parse_timestamp(row[1])
                coinapi_us = parse_timestamp(row[2])
            except MarketDataError as exc:
                reject(line, RejectionReason.BAD_TIMESTAMP, str(exc), row)
                continue
            try:
                ask_px = _finite(row[3])
                ask_sx = _finite(row[4])
                bid_px = _finite(row[5])
                bid_sx = _finite(row[6])
            except ValueError as exc:
                reason = (RejectionReason.NON_FINITE_VALUE
                          if _looks_non_finite(row[3:7])
                          else RejectionReason.BAD_NUMBER)
                reject(line, reason, str(exc), row)
                continue

            if coinapi_us < exchange_us:
                reject(line, RejectionReason.NEGATIVE_LATENCY,
                       f"time_coinapi {row[2]} precedes time_exchange {row[1]}", row)
                continue
            if ask_px <= 0.0 or bid_px <= 0.0:
                reject(line, RejectionReason.NON_POSITIVE_PRICE,
                       f"bid={bid_px} ask={ask_px}", row)
                continue
            if ask_sx < 0.0 or bid_sx < 0.0:
                reject(line, RejectionReason.NEGATIVE_VALUE,
                       f"bid_sx={bid_sx} ask_sx={ask_sx}", row)
                continue
            if ask_sx == 0.0 or bid_sx == 0.0:
                reject(line, RejectionReason.ZERO_SIZE,
                       f"bid_sx={bid_sx} ask_sx={ask_sx}: a price with no size "
                       f"behind it is not tradeable", row)
                continue
            if bid_px >= ask_px:
                reject(line, RejectionReason.CROSSED_BOOK,
                       f"bid {bid_px} >= ask {ask_px}", row)
                continue
            if is_spread_anomalous(bid_px, ask_px, cfg):
                reject(line, RejectionReason.SPREAD_ANOMALY,
                       f"spread {spread_bps(bid_px, ask_px):.1f} bps exceeds "
                       f"MAX_SPREAD_ANOMALY_PCT", row)
                continue
            if last_exchange is not None and exchange_us < last_exchange:
                reject(line, RejectionReason.NON_MONOTONIC_TIMESTAMP,
                       f"time_exchange {row[1]} precedes the previous row's "
                       f"{format_timestamp(last_exchange)}", row)
                continue

            last_exchange = exchange_us
            quotes.append(Quote(
                row[0], exchange_us, coinapi_us,
                normalize_price(ask_px), normalize_price(ask_sx),
                normalize_price(bid_px), normalize_price(bid_sx),
            ))

    result = QuoteLoad(path, quotes, rejections, rows_read)
    if rejections:
        logger.warning("%s: kept %d quotes, rejected %d rows: %s",
                       path, len(quotes), len(rejections), result.rejection_reasons)
    return result


# ---------------------------------------------------------------------------
# L2 book events
# ---------------------------------------------------------------------------


class BookEvent(NamedTuple):
    """One L2 update. A NamedTuple because millions of these may be held."""

    symbol_id: str
    time_exchange_us: int
    time_coinapi_us: int
    is_buy: bool
    price: float
    size: float
    update_type: str


@dataclass(frozen=True)
class BookEventLoad:
    path: str
    events: List[BookEvent]
    rejections: List[Rejection]
    rows_read: int

    @property
    def ok(self) -> bool:
        return not self.rejections

    @property
    def rejection_reasons(self) -> Dict[str, int]:
        counts: Dict[str, int] = {}
        for rejection in self.rejections:
            counts[rejection.reason] = counts.get(rejection.reason, 0) + 1
        return counts

    def raise_if_rejected(self) -> "BookEventLoad":
        if self.rejections:
            raise MarketDataError(
                f"{self.path}: {len(self.rejections)} rejected row(s): "
                f"{self.rejection_reasons}; first is {self.rejections[0]}"
            )
        return self


_TRUE_TOKENS = frozenset({"true", "t", "1", "yes", "y"})
_FALSE_TOKENS = frozenset({"false", "f", "0", "no", "n"})


def load_book_events(path: str, *, strict: bool = False) -> BookEventLoad:
    """Load an L2 event stream.

    Zero size is rejected for ``SNAPSHOT``, ``ADD`` and ``MATCH``, and
    **allowed** for ``SET`` and ``DELETE``. That asymmetry is the CoinAPI
    convention and it is load-bearing: ``SET`` with size 0 is how a level is
    removed, so rejecting it would drop real deletions and leave the
    reconstructed book permanently too deep — a book that is too deep makes
    slippage look smaller than it is, which is a failure in the expensive
    direction. A zero-size ``SNAPSHOT`` level, by contrast, carries no
    information at all.

    ``is_buy`` is parsed strictly. A feed that starts writing ``B``/``S``
    instead of ``true``/``false`` would, under a lenient ``bool(str)``, be read
    as *every event is a bid* — a book with no asks and no error message.
    """
    events: List[BookEvent] = []
    rejections: List[Rejection] = []
    rows_read = 0

    def reject(line: int, reason: str, detail: str, raw: Sequence[str]) -> None:
        rejection = Rejection(path, line, reason, detail, tuple(raw))
        if strict:
            raise MarketDataError(str(rejection))
        rejections.append(rejection)

    with _open_text(path) as handle:
        reader = csv.reader(handle)
        _check_header(path, next(reader, None), _BOOK_COLUMNS)
        for line, row in enumerate(reader, start=2):
            if not row or all(not cell.strip() for cell in row):
                continue
            rows_read += 1
            if len(row) != len(_BOOK_COLUMNS):
                reject(line, RejectionReason.MALFORMED_ROW,
                       f"expected {len(_BOOK_COLUMNS)} columns, got {len(row)}", row)
                continue
            update_type = row[6].strip().upper()
            if update_type not in UPDATE_TYPES:
                reject(line, RejectionReason.UNKNOWN_UPDATE_TYPE,
                       f"{row[6]!r} is not one of {sorted(UPDATE_TYPES)}", row)
                continue
            token = row[3].strip().lower()
            if token in _TRUE_TOKENS:
                is_buy = True
            elif token in _FALSE_TOKENS:
                is_buy = False
            else:
                reject(line, RejectionReason.BAD_BOOLEAN,
                       f"is_buy={row[3]!r} is neither true nor false", row)
                continue
            try:
                exchange_us = parse_timestamp(row[1])
                coinapi_us = parse_timestamp(row[2])
            except MarketDataError as exc:
                reject(line, RejectionReason.BAD_TIMESTAMP, str(exc), row)
                continue
            try:
                price = _finite(row[4])
                size = _finite(row[5])
            except ValueError as exc:
                reason = (RejectionReason.NON_FINITE_VALUE
                          if _looks_non_finite(row[4:6])
                          else RejectionReason.BAD_NUMBER)
                reject(line, reason, str(exc), row)
                continue

            if coinapi_us < exchange_us:
                reject(line, RejectionReason.NEGATIVE_LATENCY,
                       f"time_coinapi {row[2]} precedes time_exchange {row[1]}", row)
                continue
            if price <= 0.0:
                reject(line, RejectionReason.NON_POSITIVE_PRICE,
                       f"entry_px={price}", row)
                continue
            if size < 0.0:
                reject(line, RejectionReason.NEGATIVE_VALUE,
                       f"entry_sx={size}", row)
                continue
            if size == 0.0 and update_type in ("SNAPSHOT", "ADD", "MATCH"):
                reject(line, RejectionReason.ZERO_SIZE,
                       f"{update_type} with entry_sx=0 carries no information", row)
                continue

            events.append(BookEvent(
                row[0], exchange_us, coinapi_us, is_buy,
                normalize_price(price), normalize_price(size), update_type,
            ))

    result = BookEventLoad(path, events, rejections, rows_read)
    if rejections:
        logger.warning("%s: kept %d events, rejected %d rows: %s",
                       path, len(events), len(rejections), result.rejection_reasons)
    return result


# ---------------------------------------------------------------------------
# the book
# ---------------------------------------------------------------------------


class BookLevel(NamedTuple):
    price: float
    size: float

    @property
    def notional(self) -> float:
        return self.price * self.size


class BookState:
    """An L2 book rebuilt from an event stream.

    Update semantics, stated once so they cannot drift:

    ==========  ===========================================================
    SNAPSHOT    Replaces the whole book. All rows sharing one
                ``time_exchange_us`` form one snapshot batch; the first row
                of a new batch clears both sides, the rest accumulate.
    SET         The level *is* this size. Size 0 removes it.
    ADD         Increase this level by this size, creating it if absent.
    SUB         Decrease this level by this size.
    MATCH       A trade consumed this size from this level. Same arithmetic
                as SUB, tracked separately because it is information: MATCH
                is flow, SUB is a cancellation.
    DELETE      Remove the level. Size is ignored.
    ==========  ===========================================================

    The batching rule for SNAPSHOT is the subtle one. A snapshot arrives as
    forty rows; treating each as "replace the book" would leave a one-level
    book after every snapshot. Keying the reset on a change of
    ``time_exchange_us`` is what the generator emits and what CoinAPI does.

    **Divergence is fatal by default.** SUB or MATCH against a level that does
    not exist, or for more size than it holds, means this book and the
    exchange's have diverged. ``strict=True`` (the default) raises
    :class:`BookIntegrityError`. ``strict=False`` clamps to zero and counts the
    event in :attr:`integrity_warnings` — offered because a real vendor feed
    with a dropped packet would otherwise be unusable, but it is opt-in,
    because the clamped book is wrong by an unknown amount and only the caller
    knows whether that is acceptable.
    """

    __slots__ = ("symbol_id", "_bids", "_asks", "strict", "_snapshot_batch",
                 "last_exchange_us", "last_coinapi_us", "events_applied",
                 "matched_base", "integrity_warnings", "snapshots_seen")

    def __init__(self, symbol_id: str = "", *, strict: bool = True) -> None:
        self.symbol_id = symbol_id
        self.strict = strict
        self._bids: Dict[float, float] = {}
        self._asks: Dict[float, float] = {}
        self._snapshot_batch: Optional[int] = None
        self.last_exchange_us: Optional[int] = None
        self.last_coinapi_us: Optional[int] = None
        self.events_applied = 0
        self.matched_base = 0.0
        self.integrity_warnings: List[str] = []
        self.snapshots_seen = 0

    # -- mutation ---------------------------------------------------------

    def apply(self, event: BookEvent) -> None:
        """Apply one event. See the class docstring for the semantics."""
        if event.update_type not in UPDATE_TYPES:
            raise BookIntegrityError(
                f"unknown update type {event.update_type!r}; "
                f"load_book_events should have rejected this row"
            )
        if self.symbol_id and event.symbol_id and event.symbol_id != self.symbol_id:
            raise BookIntegrityError(
                f"event for {event.symbol_id} applied to a {self.symbol_id} book"
            )
        if (self.last_exchange_us is not None
                and event.time_exchange_us < self.last_exchange_us):
            raise BookIntegrityError(
                f"event at {format_timestamp(event.time_exchange_us)} precedes "
                f"the book's last event at "
                f"{format_timestamp(self.last_exchange_us)}; replaying a book "
                f"out of order produces a state that never existed"
            )

        side = self._bids if event.is_buy else self._asks
        kind = event.update_type
        # Normalise here as well as in the loaders. Level identity is this
        # class's concern, not the caller's: an event built by hand (a test, a
        # WebSocket feed, a replay tool) whose price is 0.30000000000000004
        # would otherwise create a second level next to the 0.3 already in the
        # book, and the book would show depth that does not exist.
        price = normalize_price(event.price)

        if kind == "SNAPSHOT":
            if self._snapshot_batch != event.time_exchange_us:
                self._bids.clear()
                self._asks.clear()
                self._snapshot_batch = event.time_exchange_us
                self.snapshots_seen += 1
                side = self._bids if event.is_buy else self._asks
            side[price] = event.size
        else:
            self._snapshot_batch = None
            if kind == "SET":
                if event.size > 0.0:
                    side[price] = event.size
                else:
                    side.pop(price, None)
            elif kind == "ADD":
                side[price] = normalize_price(side.get(price, 0.0) + event.size)
            elif kind in ("SUB", "MATCH"):
                self._consume(side, price, event, kind)
            elif kind == "DELETE":
                side.pop(price, None)

        self.last_exchange_us = event.time_exchange_us
        self.last_coinapi_us = (
            event.time_coinapi_us if self.last_coinapi_us is None
            else max(self.last_coinapi_us, event.time_coinapi_us)
        )
        self.events_applied += 1

    def _consume(self, side: Dict[float, float], price: float,
                 event: BookEvent, kind: str) -> None:
        present = side.get(price)
        if present is None or present < event.size - 1e-9:
            message = (
                f"{kind} of {event.size} at {price} but the level holds "
                f"{present if present is not None else 'nothing'} "
                f"at {format_timestamp(event.time_exchange_us)}"
            )
            if self.strict:
                raise BookIntegrityError(message)
            self.integrity_warnings.append(message)
            side.pop(price, None)
        else:
            remaining = normalize_price(present - event.size)
            if remaining > 0.0:
                side[price] = remaining
            else:
                side.pop(price, None)
        if kind == "MATCH":
            self.matched_base += event.size

    def apply_all(self, events: Iterable[BookEvent]) -> "BookState":
        for event in events:
            self.apply(event)
        return self

    @classmethod
    def from_ladders(
        cls,
        bids: Iterable[Sequence[Any]],
        asks: Iterable[Sequence[Any]],
        *,
        symbol_id: str = "",
        strict: bool = True,
    ) -> "BookState":
        """Build a book from two ``[[price, size], ...]`` ladders.

        The event stream above is how a *feed* delivers a book. This is how a
        REST snapshot delivers one — Bybit's ``/v5/market/orderbook`` returns
        exactly this shape in ``result.b`` and ``result.a``. Having both
        entry points share one :class:`BookState` means the microstructure
        measurements are computed by identical code whether they came from a
        historical archive or from a live REST call, so a gate cannot pass in
        backtest and behave differently in production.

        Rows with a non-positive or unparseable size are dropped rather than
        stored: a zero-size level is an absent level, and keeping it would make
        ``len(bids)`` a count of something other than liquidity.
        """
        state = cls(symbol_id, strict=strict)
        for target, rows in ((state._bids, bids), (state._asks, asks)):
            for row in rows:
                try:
                    price = normalize_price(float(row[0]))
                    size = float(row[1])
                except (TypeError, ValueError, IndexError):
                    continue
                if not (math.isfinite(price) and math.isfinite(size)):
                    continue
                if price <= 0.0 or size <= 0.0:
                    continue
                target[price] = target.get(price, 0.0) + size
        return state

    # -- reading ----------------------------------------------------------

    @property
    def bids(self) -> List[BookLevel]:
        """Descending by price — best bid first."""
        return [BookLevel(price, size)
                for price, size in sorted(self._bids.items(), reverse=True)
                if size > 0.0]

    @property
    def asks(self) -> List[BookLevel]:
        """Ascending by price — best ask first."""
        return [BookLevel(price, size)
                for price, size in sorted(self._asks.items())
                if size > 0.0]

    @property
    def best_bid(self) -> Optional[BookLevel]:
        if not self._bids:
            return None
        price = max(self._bids)
        return BookLevel(price, self._bids[price])

    @property
    def best_ask(self) -> Optional[BookLevel]:
        if not self._asks:
            return None
        price = min(self._asks)
        return BookLevel(price, self._asks[price])

    @property
    def is_empty(self) -> bool:
        return not self._bids and not self._asks

    @property
    def is_one_sided(self) -> bool:
        return bool(self._bids) != bool(self._asks)

    @property
    def is_crossed(self) -> bool:
        """Best bid at or above best ask. Locked (equal) counts as crossed.

        A locked book is not tradeable either — there is no spread to cross and
        no mid that is not also a touch — so it is refused on the same terms
        rather than treated as a zero-cost market.
        """
        bid, ask = self.best_bid, self.best_ask
        if bid is None or ask is None:
            return False
        return bid.price >= ask.price

    @property
    def is_usable(self) -> bool:
        """Two-sided, uncrossed, and every touch has size behind it."""
        bid, ask = self.best_bid, self.best_ask
        if bid is None or ask is None:
            return False
        if bid.price >= ask.price:
            return False
        return bid.size > 0.0 and ask.size > 0.0

    def unusable_reason(self) -> Optional[str]:
        """Why the book cannot be priced, or ``None`` if it can.

        Exists so a caller that got ``None`` from a feature can log *which*
        failure it hit without reimplementing the checks.
        """
        if self.is_empty:
            return "book is empty"
        bid, ask = self.best_bid, self.best_ask
        if bid is None:
            return "book has no bid side"
        if ask is None:
            return "book has no ask side"
        if bid.price > ask.price:
            return f"book is crossed: bid {bid.price} > ask {ask.price}"
        if bid.price == ask.price:
            return f"book is locked at {bid.price}"
        if bid.size <= 0.0 or ask.size <= 0.0:
            return "touch has no size behind it"
        return None

    def copy(self) -> "BookState":
        """A detached copy. Used by :func:`replay` so a consumer cannot mutate
        the iterator's own state and change what the next step sees."""
        clone = BookState(self.symbol_id, strict=self.strict)
        clone._bids = dict(self._bids)
        clone._asks = dict(self._asks)
        clone._snapshot_batch = self._snapshot_batch
        clone.last_exchange_us = self.last_exchange_us
        clone.last_coinapi_us = self.last_coinapi_us
        clone.events_applied = self.events_applied
        clone.matched_base = self.matched_base
        clone.snapshots_seen = self.snapshots_seen
        clone.integrity_warnings = list(self.integrity_warnings)
        return clone

    def features(self, levels: int = 10,
                 cfg: Optional[Any] = None) -> "BookFeatures":
        return BookFeatures.from_state(self, levels=levels, cfg=cfg)

    def __repr__(self) -> str:                    # pragma: no cover - debugging aid
        bid, ask = self.best_bid, self.best_ask
        return (f"BookState({self.symbol_id!r}, bids={len(self._bids)}, "
                f"asks={len(self._asks)}, "
                f"touch={bid.price if bid else None}/{ask.price if ask else None})")


# ---------------------------------------------------------------------------
# features
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BookFeatures:
    """Microstructure measurements, or ``None`` where there is no measurement.

    Every ``Optional`` field here is ``None`` for exactly one reason: the book
    did not contain the information. None of them fall back to a previous
    value, a configured default, or the other side of the book. A mid derived
    from a one-sided book is the specific failure this codebase exists to
    eliminate — it looks like a price, it prices a position, and it is a guess.

    ``depth_bid_usd``/``depth_ask_usd`` are the exception in one direction:
    they are reported whenever that side has levels, even if the *other* side
    is missing, because a one-sided sum is still a true statement about the
    side it sums. They are ``None`` only when the side is empty.
    """

    levels: int
    best_bid: Optional[float]
    best_ask: Optional[float]
    best_bid_size: Optional[float]
    best_ask_size: Optional[float]
    mid: Optional[float]
    microprice: Optional[float]
    spread_bps: Optional[float]
    depth_bid_usd: Optional[float]
    depth_ask_usd: Optional[float]
    imbalance: Optional[float]
    usable: bool
    reason: Optional[str]
    bid_levels: Tuple[BookLevel, ...] = ()
    ask_levels: Tuple[BookLevel, ...] = ()
    anomalous_spread: bool = False

    @classmethod
    def from_state(cls, state: BookState, levels: int = 10,
                   cfg: Optional[Any] = None) -> "BookFeatures":
        if levels < 1:
            raise ValueError("levels must be >= 1")
        bids = state.bids[:levels]
        asks = state.asks[:levels]
        reason = state.unusable_reason()
        usable = reason is None

        depth_bid = sum(level.notional for level in bids) if bids else None
        depth_ask = sum(level.notional for level in asks) if asks else None

        if not usable:
            # Depth still reported: a one-sided sum is a true statement about
            # the side that exists. Everything derived from *both* sides is not.
            return cls(
                levels=levels,
                best_bid=state.best_bid.price if state.best_bid else None,
                best_ask=state.best_ask.price if state.best_ask else None,
                best_bid_size=state.best_bid.size if state.best_bid else None,
                best_ask_size=state.best_ask.size if state.best_ask else None,
                mid=None, microprice=None, spread_bps=None,
                depth_bid_usd=depth_bid, depth_ask_usd=depth_ask,
                imbalance=None, usable=False, reason=reason,
                bid_levels=tuple(bids), ask_levels=tuple(asks),
                anomalous_spread=True,
            )

        bid, ask = state.best_bid, state.best_ask
        assert bid is not None and ask is not None      # guaranteed by is_usable
        mid = normalize_price((bid.price + ask.price) / 2.0)
        total_touch = bid.size + ask.size
        micro = normalize_price(
            (bid.price * ask.size + ask.price * bid.size) / total_touch
        ) if total_touch > 0 else None

        total_depth = (depth_bid or 0.0) + (depth_ask or 0.0)
        imbalance = (
            ((depth_bid or 0.0) - (depth_ask or 0.0)) / total_depth
            if total_depth > 0 else None
        )
        if imbalance is not None:
            # Arithmetically already in [-1, 1]; clamped anyway because the
            # consumer of this number scales position size with it, and a
            # 1.0000000001 from float error would leak past a `<= 1` guard.
            imbalance = max(-1.0, min(1.0, imbalance))

        return cls(
            levels=levels,
            best_bid=bid.price, best_ask=ask.price,
            best_bid_size=bid.size, best_ask_size=ask.size,
            mid=mid, microprice=micro,
            spread_bps=spread_bps(bid.price, ask.price),
            depth_bid_usd=depth_bid, depth_ask_usd=depth_ask,
            imbalance=imbalance, usable=True, reason=None,
            bid_levels=tuple(bids), ask_levels=tuple(asks),
            anomalous_spread=is_spread_anomalous(bid.price, ask.price, cfg),
        )

    def slippage_bps_for(self, notional: float, side: str) -> Optional[float]:
        """Cost in bps of taking ``notional`` USD immediately, versus mid.

        BUY walks the asks upward; SELL walks the bids downward. The result is
        the volume-weighted execution price expressed as a distance from mid,
        and it is **always non-negative** on an uncrossed book — you cross the
        spread in whichever direction you go. A negative result would mean the
        book paid you to trade, which is the sign error that turns a cost model
        into an edge.

        Returns ``None`` when the book cannot answer:

        * the book is empty, one-sided or crossed — there is no mid to measure
          against;
        * **the visible levels cannot absorb the size.** This is the important
          one. The tempting behaviour is to fill the remainder at the last
          level's price, and that is exactly backwards: running out of book is
          the case where real slippage is *worst*, so extrapolating the last
          price understates the cost precisely when the number matters most. It
          also silently converts "your order is too big for this market" into a
          modest-looking number that a sizing routine will happily act on.

        Note this measures only the levels it was constructed with
        (``self.levels``). A ``BookFeatures(levels=5)`` will report ``None`` for
        a size that ten levels could have absorbed; that is a limit of what was
        measured, not a claim about the market.
        """
        if notional <= 0.0 or not math.isfinite(notional):
            raise ValueError(f"notional must be positive and finite, got {notional!r}")
        normalized = str(side).strip().upper()
        if normalized in ("BUY", "LONG", "B"):
            book, sign = self.ask_levels, 1.0
        elif normalized in ("SELL", "SHORT", "S"):
            book, sign = self.bid_levels, -1.0
        else:
            raise ValueError(f"side must be BUY or SELL, got {side!r}")

        if not self.usable or self.mid is None or not book:
            return None

        remaining = notional
        spent = 0.0
        filled = 0.0
        for level in book:
            available = level.notional
            if available <= 0.0:
                continue
            take = min(remaining, available)
            spent += take
            filled += take / level.price
            remaining -= take
            if remaining <= 1e-9:
                break
        if remaining > 1e-9 or filled <= 0.0:
            return None

        vwap = spent / filled
        return sign * (vwap - self.mid) / self.mid * 10_000.0

    def fillable_notional(self, side: str) -> Optional[float]:
        """Total USD the measured levels could absorb on one side.

        Provided so a caller that got ``None`` from :meth:`slippage_bps_for`
        can find out whether the answer was "the book is broken" or "your order
        is bigger than the visible book", which are different problems.
        """
        normalized = str(side).strip().upper()
        if normalized in ("BUY", "LONG", "B"):
            return self.depth_ask_usd
        if normalized in ("SELL", "SHORT", "S"):
            return self.depth_bid_usd
        raise ValueError(f"side must be BUY or SELL, got {side!r}")


# ---------------------------------------------------------------------------
# replay — the no-lookahead walk
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReplayStep:
    """One closed bar and the book as it was known at that bar's close.

    ``book`` is a detached copy, so a consumer that mutates it cannot change
    what the next step sees. ``last_event_us`` is the newest event folded in —
    a test can assert directly that it never exceeds ``cutoff_us``, which is
    the whole no-lookahead claim reduced to one comparison.
    """

    index: int
    bar: Bar
    book: BookState
    features: BookFeatures
    cutoff_us: int
    events_applied: int
    last_event_us: Optional[int]


def replay(bars: Sequence[Bar], events: Sequence[BookEvent], *,
           levels: int = 10, use_coinapi_time: bool = True,
           strict_book: bool = False,
           cfg: Optional[Any] = None) -> Iterator[ReplayStep]:
    """Walk bars and book together with no way to see the future.

    For each bar in order, every event whose timestamp is ``<=`` that bar's
    close is folded into the book, and the bar plus that book is yielded. The
    cursor into ``events`` only ever moves forward and is never read beyond,
    so lookahead is not prevented by a check — it is unreachable.

    ``use_coinapi_time=True`` (the default) cuts on ``time_coinapi``, the
    moment the data was *received*. Cutting on ``time_exchange`` instead grants
    the strategy the collection latency as free foresight. It is a few hundred
    microseconds on this corpus and therefore harmless here, which is exactly
    why it must be right here: the same code against a feed with 400ms of
    latency would be reading the future by a whole trading decision.

    Both inputs must already be sorted; this raises rather than sorting them.
    Sorting inside the replay would mask an unsorted source, and an unsorted
    source is usually an incomplete one.

    ``strict_book`` defaults to ``False`` here, unlike :class:`BookState`
    itself. A replay is a bulk operation over a whole file, and one packet-loss
    artefact in a five-thousand-bar stream should degrade that one book rather
    than abort the run — but every clamp is still counted in
    ``step.book.integrity_warnings`` for the caller to inspect.
    """
    if levels < 1:
        raise ValueError("levels must be >= 1")
    for index in range(1, len(bars)):
        if bars[index].open_us <= bars[index - 1].open_us:
            raise MarketDataError(
                f"bars are not strictly increasing at index {index}: "
                f"{format_timestamp(bars[index - 1].open_us)} then "
                f"{format_timestamp(bars[index].open_us)}"
            )

    def stamp(event: BookEvent) -> int:
        return event.time_coinapi_us if use_coinapi_time else event.time_exchange_us

    for index in range(1, len(events)):
        if stamp(events[index]) < stamp(events[index - 1]):
            raise MarketDataError(
                f"book events are not sorted at index {index}: "
                f"{format_timestamp(stamp(events[index - 1]))} then "
                f"{format_timestamp(stamp(events[index]))}. Sorting them here "
                f"would hide an unordered source."
            )

    symbol = events[0].symbol_id if events else ""
    state = BookState(symbol, strict=strict_book)
    cursor = 0
    total = len(events)
    last_event_us: Optional[int] = None

    for index, bar in enumerate(bars):
        cutoff = bar.close_us
        while cursor < total and stamp(events[cursor]) <= cutoff:
            state.apply(events[cursor])
            last_event_us = stamp(events[cursor])
            cursor += 1
        snapshot = state.copy()
        yield ReplayStep(
            index=index, bar=bar, book=snapshot,
            features=snapshot.features(levels=levels, cfg=cfg),
            cutoff_us=cutoff, events_applied=cursor,
            last_event_us=last_event_us,
        )


# ---------------------------------------------------------------------------
# manifest verification
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ManifestReport:
    """The result of checking the corpus on disk against its shipped hashes."""

    root: str
    checked: int
    problems: List[str]
    recompressed: List[str] = field(default_factory=list)
    synthetic: bool = True

    @property
    def ok(self) -> bool:
        return not self.problems

    def raise_if_bad(self) -> "ManifestReport":
        if self.problems:
            raise ManifestError(
                f"{self.root}: {len(self.problems)} problem(s): "
                + "; ".join(self.problems[:5])
                + ("…" if len(self.problems) > 5 else "")
            )
        return self


def verify_manifest(root: str = DATA_ROOT, *, deep: bool = False) -> ManifestReport:
    """Check every file the manifest lists against its recorded sha256.

    This is not ceremony. The corpus is a plain directory of CSVs that any
    editor can open; the failure it guards against is someone "just fixing" a
    row by hand — or a truncated copy, or a partial download — and every
    subsequent number in the repository quietly describing different data than
    the one the tests were written against. A corpus that has silently changed
    is worse than no corpus, because the results still look reproducible.

    Two distinct outcomes rather than one:

    * a **content** mismatch (the uncompressed bytes differ) is a real problem
      and lands in ``problems``;
    * a **compression-only** mismatch (same content, different gzip) lands in
      ``recompressed`` and is *not* an error. Re-gzipping a file at a different
      level changes every byte without changing a single row, and treating that
      as corruption trains people to ignore this check.

    ``deep=True`` also re-counts the rows, which catches a file whose hash was
    regenerated after an edit — the case a hash alone cannot see.
    """
    manifest_path = os.path.join(root, "MANIFEST.json")
    if not os.path.exists(manifest_path):
        return ManifestReport(root, 0, [f"no MANIFEST.json in {root}"])
    try:
        with open(manifest_path, encoding="utf-8") as handle:
            manifest = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        return ManifestReport(root, 0, [f"MANIFEST.json is unreadable: {exc}"])

    files = manifest.get("files")
    if not isinstance(files, dict) or not files:
        return ManifestReport(root, 0, ["MANIFEST.json lists no files"])

    problems: List[str] = []
    recompressed: List[str] = []
    checked = 0

    for relpath in sorted(files):
        entry = files[relpath]
        full = os.path.join(root, relpath.replace("/", os.sep))
        if not os.path.exists(full):
            problems.append(f"{relpath}: listed in the manifest but missing")
            continue
        try:
            with open(full, "rb") as blob:
                data = blob.read()
        except OSError as exc:
            problems.append(f"{relpath}: unreadable ({exc})")
            continue
        checked += 1
        expected_size = entry.get("size_bytes")

        digest = hashlib.sha256(data).hexdigest()
        if digest == entry.get("sha256"):
            if deep:
                problem = _verify_rows(relpath, full, entry)
                if problem:
                    problems.append(problem)
            continue

        raw_digest = None
        if relpath.endswith(".gz"):
            try:
                raw_digest = hashlib.sha256(gzip.decompress(data)).hexdigest()
            except (OSError, EOFError, gzip.BadGzipFile) as exc:
                problems.append(f"{relpath}: not readable as gzip ({exc})")
                continue
        else:
            raw_digest = digest

        if raw_digest == entry.get("sha256_uncompressed"):
            recompressed.append(relpath)
            if deep:
                problem = _verify_rows(relpath, full, entry)
                if problem:
                    problems.append(problem)
            continue

        # Size is reported only here, never on its own. A file that is
        # content-identical but re-compressed has a different size for a
        # completely benign reason, so a standalone size check would flag it —
        # and a genuinely truncated file fails the hash anyway, so the size
        # adds diagnosis, not detection.
        size_note = ""
        if isinstance(expected_size, int) and len(data) != expected_size:
            size_note = (f" ({len(data)} bytes on disk, manifest says "
                         f"{expected_size})")
        problems.append(
            f"{relpath}: CONTENT CHANGED — sha256 {digest[:12]}… does not match "
            f"the manifest's {str(entry.get('sha256'))[:12]}…, and the "
            f"uncompressed content does not match either{size_note}. This file "
            f"is not the data the tests were written against."
        )

    if recompressed:
        logger.info("%d file(s) re-compressed but content-identical: %s",
                    len(recompressed), ", ".join(recompressed))

    return ManifestReport(root, checked, problems, recompressed,
                          bool(manifest.get("synthetic", True)))


def _verify_rows(relpath: str, full: str, entry: Mapping[str, Any]) -> Optional[str]:
    """Count data rows and compare with the manifest. Used by ``deep=True``."""
    expected = entry.get("rows")
    if not isinstance(expected, int):
        return None
    with _open_text(full) as handle:
        count = sum(1 for line in handle if line.strip()) - 1
    if count != expected:
        return (f"{relpath}: {count} data rows on disk, manifest says {expected}")
    return None


# ---------------------------------------------------------------------------
# the corpus, as the backtester wants it
# ---------------------------------------------------------------------------


#: Filenames in the corpus follow CoinAPI's ``EXCHANGE_TYPE_BASE_QUOTE`` shape,
#: e.g. ``BYBIT_SPOT_BTC_USDT_1H``. The trading symbol is the last two segments
#: before the suffix, concatenated: BTC + USDT.
#: The interval suffixes a corpus file may carry. Hardcoding ``_1H`` here meant
#: `load_corpus` silently found nothing in a 4-hour or daily corpus and raised
#: "no usable OHLCV files" — a message that describes the directory rather than
#: the assumption that produced it.
_OHLCV_SUFFIXES = ("_1M", "_5M", "_15M", "_30M", "_1H", "_4H", "_1D")
_OHLCV_SUFFIX = "_1H.csv.gz"          # retained: the corpus filename template
_BOOK_SUFFIX = "_L2.csv.gz"


def _ohlcv_interval_suffix(name: str) -> Optional[str]:
    """The interval token in an OHLCV filename, or ``None`` if it has none.

    Matched against a closed set rather than by splitting on the last
    underscore. A file named ``..._BTC_USD_DAILY.csv.gz`` should not be loaded
    as though the codebase understood what "DAILY" meant.
    """
    for suffix in _OHLCV_SUFFIXES:
        if name.endswith(suffix + ".csv.gz"):
            return suffix
    return None


def symbol_from_filename(name: str) -> str:
    """``BYBIT_SPOT_BTC_USDT_1H.csv.gz`` -> ``BTCUSDT``.

    Raises on anything that does not fit the convention rather than returning a
    best guess. A corpus file whose symbol cannot be determined would otherwise
    be loaded under a wrong-but-plausible name, and the backtest would trade
    one asset's book against another's candles.
    """
    stem = os.path.basename(name)
    for suffix in (
        *(s + ".csv.gz" for s in _OHLCV_SUFFIXES), _BOOK_SUFFIX, ".csv.gz", ".csv"
    ):
        if stem.endswith(suffix):
            stem = stem[: -len(suffix)]
            break
    parts = stem.split("_")
    if len(parts) < 4:
        raise MarketDataError(
            f"cannot determine a symbol from {name!r}; expected "
            "EXCHANGE_TYPE_BASE_QUOTE[_INTERVAL]"
        )
    return (parts[2] + parts[3]).upper()


#: Called as ``READ_OBSERVER(symbol, first_start_ms, last_start_ms)`` after every
#: successful corpus load. Defaults to None so this module keeps zero knowledge
#: of who is watching. ``reserved_holdout.install()`` sets it; nothing else may.
#: Loading a symbol counts as reading it even when the signal under test ignores
#: that symbol — slice 55 read ETH and SOL, and those negative readings are
#: exactly what steered the next hypothesis toward BTC.
READ_OBSERVER = None


def load_corpus(
    root: str = DATA_ROOT,
    *,
    symbols: Optional[Sequence[str]] = None,
    levels: int = 25,
    cfg: Optional[Any] = None,
    verify: bool = True,
) -> Tuple[Dict[str, List[Bar]], Dict[str, List[Optional[Tuple[List[List[float]], List[List[float]]]]]], List[str]]:
    """Load the whole corpus into the shapes ``Backtester`` consumes.

    Returns ``(bars_by_symbol, books_by_symbol, notes)`` where ``books_by_symbol``
    is **index-aligned with the bars**: entry *i* is the L2 ladder pair
    ``(bids, asks)`` as it stood at the close of bar *i*, or ``None`` if no
    book was recorded there.

    Index alignment is produced by :func:`replay`, which cannot see past a
    bar's close, so the books handed to a backtest are structurally incapable
    of containing future information. That property is the reason this function
    exists rather than each caller zipping the two files together itself.

    ``notes`` carries everything the caller should report rather than swallow:
    the provenance warning, per-file rejection counts, and which symbols have
    no book. It is returned rather than logged because a backtest summary that
    omits "this ran without an order book" is a backtest summary that lies by
    omission.
    """
    notes: List[str] = []
    if verify:
        report = verify_manifest(root)
        if report.problems:
            raise ManifestError(
                "the data corpus does not match its manifest:\n  "
                + "\n  ".join(report.problems)
            )
        if report.synthetic:
            notes.append(
                "SYNTHETIC corpus generated by tools/make_dataset.py. It is not "
                "a market. Results measure the harness, not any edge."
            )

    ohlcv_dir = os.path.join(root, "ohlcv")
    book_dir = os.path.join(root, "orderbook")
    if not os.path.isdir(ohlcv_dir):
        raise MarketDataError(f"no ohlcv/ directory under {root}")

    wanted = {s.upper() for s in symbols} if symbols else None
    bars_by_symbol: Dict[str, List[Bar]] = {}
    books_by_symbol: Dict[str, List[Optional[Tuple[List[List[float]], List[List[float]]]]]] = {}

    for filename in sorted(os.listdir(ohlcv_dir)):
        interval_suffix = _ohlcv_interval_suffix(filename)
        if interval_suffix is None:
            continue
        symbol = symbol_from_filename(filename)
        if wanted is not None and symbol not in wanted:
            continue
        load = load_ohlcv(os.path.join(ohlcv_dir, filename))
        if load.rejections:
            notes.append(
                f"{symbol}: {len(load.rejections)} OHLCV row(s) rejected "
                f"({load.rejection_reasons})"
            )
        if not load.bars:
            notes.append(f"{symbol}: no usable bars; skipped")
            continue
        bars_by_symbol[symbol] = list(load.bars)

        book_path = os.path.join(
            book_dir,
            filename[: -len(interval_suffix + ".csv.gz")] + _BOOK_SUFFIX,
        )
        if not os.path.exists(book_path):
            notes.append(
                f"{symbol}: no order book in the corpus — the liquidity gate "
                "will abstain for this symbol"
            )
            continue

        events = load_book_events(book_path)
        if events.rejections:
            notes.append(
                f"{symbol}: {len(events.rejections)} book event(s) rejected "
                f"({events.rejection_reasons})"
            )
        ladders: List[Optional[Tuple[List[List[float]], List[List[float]]]]] = []
        clamps = 0
        for step in replay(load.bars, events.events, levels=levels, cfg=cfg):
            clamps += len(step.book.integrity_warnings)
            bids = [[lvl.price, lvl.size] for lvl in step.book.bids[:levels]]
            asks = [[lvl.price, lvl.size] for lvl in step.book.asks[:levels]]
            ladders.append((bids, asks) if (bids or asks) else None)
        # replay() yields one step per bar, in order, so the list is aligned by
        # construction. Asserting it anyway: a future change to replay that
        # skipped a bar would otherwise shift every book by one and produce a
        # subtly optimistic result rather than an error.
        if len(ladders) != len(load.bars):
            raise MarketDataError(
                f"{symbol}: {len(ladders)} book snapshots for "
                f"{len(load.bars)} bars — alignment is not guaranteed"
            )
        if clamps:
            notes.append(f"{symbol}: {clamps} book integrity clamp(s) during replay")
        books_by_symbol[symbol] = ladders

    if not bars_by_symbol:
        raise MarketDataError(f"no usable OHLCV files under {ohlcv_dir}")
    # DATA-READ LEDGER (slice 78) — OBSERVER, not an import.
    #
    # This is the one place in the tree that sees every read of a price corpus:
    # every measurement tool routes through load_corpus. Instrumenting HERE
    # rather than in each tool is the whole point, because the slice-57
    # contamination happened by relying on a human to notice that a window had
    # already been looked at, and humans stop noticing.
    #
    # But INTEGRATION_MAP §1 says dependencies point downward only, and
    # test_the_module_imports_only_stdlib_numpy_and_config enforces it. So
    # market_data does NOT import the ledger. It publishes raw facts to an
    # observer that defaults to None, and reserved_holdout.install() sets it.
    # The dependency arrow still points down; the ledger reaches up.
    #
    # Raw epoch-ms is passed deliberately: formatting would need `datetime`,
    # which is not on this module's allowlist either. The observer formats.
    if READ_OBSERVER is not None:
        for _symbol, _bars in bars_by_symbol.items():
            if not _bars:
                continue
            try:
                READ_OBSERVER(_symbol, _bars[0].start_ms, _bars[-1].start_ms)
            except Exception:  # noqa: BLE001 - bookkeeping never kills a run
                pass

    return bars_by_symbol, books_by_symbol, notes
