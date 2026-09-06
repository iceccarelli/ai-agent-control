#!/usr/bin/env python3
"""tools/make_dataset.py — build the synthetic market-data corpus under ``data/``.

WHY THIS EXISTS
===============
INTEGRATION_MAP §7 says it plainly: *"No real market data has been used."*
Bybit is unreachable from this sandbox, so every number in TEST_REPORT.md came
from a random walk with no microstructure at all — no book, no spread, no
depth, no latency. That is fine for exercising the order lifecycle and useless
for exercising anything that reads a price *as a tradeable price*.

The cost-awareness gate (`MIN_EDGE_BPS`) is the single most consequential rule
in this codebase, and it is a statement about **spread and slippage**. You
cannot test it honestly against a series that has neither. So this tool
manufactures a corpus that has both, shaped exactly like the flat files the
operator intends to buy from CoinAPI, so that swapping real data in later is a
file copy and not a rewrite.

WHAT THIS IS NOT
----------------
**It is not a market.** It is a stochastic model with the microstructure
features hand-placed. It can falsify code — a loader that accepts a crossed
book, a feature that fabricates a mid from one side, a strategy that silently
looks ahead — and it can falsify *nothing* about edge. Any P&L computed on it
is a property of the generator, not of the strategy. `data/README.md` says this
on its first line and the MANIFEST carries `"synthetic": true` so a downstream
reader cannot lose the provenance.

DETERMINISM — AND WHY IT IS NOT NUMPY'S RNG
-------------------------------------------
Same seed must give byte-identical files, or the shipped sha256s in
`MANIFEST.json` are meaningless and `market_data.verify_manifest()` is
theatre. Three things break byte-identity and all three are handled here:

1. **The RNG stream.** numpy's NEP 19 policy explicitly permits `Generator`
   streams to change in a feature release. `random.Random` (Mersenne Twister)
   is contractually stable across CPython versions, so the stream comes from
   there and normals are drawn with an explicit Box-Muller from two uniforms
   rather than from a library routine whose internals could change. numpy is
   therefore not imported at all: this file does no array math worth the
   dependency, and not importing it removes a whole class of drift.
2. **The gzip header.** `gzip.open(path)` writes the current mtime *and* the
   original filename into the header, so the same content gzips to different
   bytes every run. Everything here goes through `GzipFile(fileobj=...,
   filename="", mtime=0)`.
3. **Wall-clock fields.** There is deliberately no `generated_at` in the
   MANIFEST. A timestamp would make every regeneration a diff, which trains
   people to ignore the diff.

The generator also does **not** import `market_data`. The loader and the writer
are independent implementations of the same timestamp and CSV contract; if they
agreed because they shared a function, the tests that read this corpus back
would be testing nothing.

WHAT IS MODELLED, AND WHY EACH PIECE IS THERE
---------------------------------------------
* **Four labelled regimes** (calm range → strong trend → crash → chop). A
  strategy evaluated on one regime is evaluated on one sample.
* **Spreads that widen and depth that thins in the crash**, with the bid side
  thinning hardest. This is the entire reason to ship a book: liquidity leaves
  exactly when a stop wants to use it, and a backtest that assumes constant
  slippage misses that.
* **A burst of MATCH events in the crash** — the trades that eat the bid.
* **Autocorrelated, clustered volume.** Volume is not i.i.d.; anything that
  normalises by recent volume behaves differently when it is not.
* **Collection latency.** `time_coinapi >= time_exchange` by a few hundred
  microseconds to a few milliseconds, so a consumer that keys off the wrong
  column has something to get wrong. `market_data.replay()` keys off
  `time_coinapi`, because you cannot act on data you have not received yet.
* **Gap-consistent bars.** Each bar's OHLC is measured from an intra-bar tick
  path, and the next bar opens exactly where this one closed, so `high`/`low`
  genuinely bracket `open`/`close` and a bar-to-bar gap check is meaningful.
* **Deliberate defects** in `data/anomalies/`, one class per file, each with
  the reason code the loader is expected to produce recorded in the MANIFEST.
  A rejection path with no fixture is an untested rejection path.

USAGE
-----
    python3 tools/make_dataset.py                     # write data/ with the default seed
    python3 tools/make_dataset.py --seed 7 --out /tmp/d
    python3 tools/make_dataset.py --check             # regenerate to a temp dir and
                                                      # diff hashes against data/MANIFEST.json
"""
from __future__ import annotations

import argparse
import datetime as _dt
import gzip
import hashlib
import io
import json
import math
import os
import random
import shutil
import sys
import tempfile
from typing import Dict, Iterable, List, NamedTuple, Optional, Sequence, Tuple

#: Bumped whenever the *content* of the corpus changes. It is recorded in the
#: MANIFEST so a stale corpus next to a newer generator is visible rather than
#: inferred from hashes that "just happen" not to match.
GENERATOR_VERSION = "1.0.0"

DEFAULT_SEED = 20_240_301
BAR_SECONDS = 3_600
BARS_PER_SYMBOL = 5_000
SUBSTEPS_PER_BAR = 12          # intra-bar tick path resolution
BOOK_LEVELS = 20               # levels per side in a SNAPSHOT

#: 2024-03-01T00:00:00Z, as epoch microseconds. Fixed, not "now": a corpus
#: whose timestamps move with the calendar cannot be hash-pinned.
_EPOCH = _dt.datetime(1970, 1, 1)
START_US = int(
    (_dt.datetime(2024, 3, 1) - _EPOCH).total_seconds() * 1_000_000
)

SYNTHETIC_WARNING = (
    "SYNTHETIC data generated by tools/make_dataset.py. This is a model, not a "
    "market. It exists to exercise loader, microstructure and no-lookahead "
    "code paths. Any P&L measured on it describes the generator, not an edge."
)

OHLCV_COLUMNS = (
    "time_period_start", "time_period_end", "time_open", "time_close",
    "price_open", "price_high", "price_low", "price_close",
    "volume_traded", "trades_count",
)
BOOK_COLUMNS = (
    "symbol_id", "time_exchange", "time_coinapi", "is_buy",
    "entry_px", "entry_sx", "update_type",
)
QUOTE_COLUMNS = (
    "symbol_id", "time_exchange", "time_coinapi",
    "ask_px", "ask_sx", "bid_px", "bid_sx",
)


# ---------------------------------------------------------------------------
# specification of what gets generated
# ---------------------------------------------------------------------------


class Regime(NamedTuple):
    """One labelled market phase.

    ``mu``/``sigma`` are per-bar log-ish drift and volatility. ``mean_rev``
    pulls price back toward a slow anchor, which is what makes "chop" chop
    rather than a low-drift trend. ``depth`` and ``bid_skew`` multiply the
    resting size on each side — the crash thins the bid to roughly a third of
    the ask, which is the asymmetry that makes selling into it expensive.
    """

    name: str
    bars: int
    mu: float
    sigma: float
    mean_rev: float
    spread_bps: float
    depth: float
    bid_skew: float
    match_p: float        # P(a sample carries MATCH events)
    volume_mult: float
    samples_per_bar: int  # book/quote sampling rate inside this regime


#: The drift/volatility pairs are not free parameters. A regime's drift has to
#: beat its own sampling error over its own length, or the label is a lie: the
#: first draft ran CRASH at mu=-0.0020 with sigma=0.0260 over 300 bars, where
#: the standard error of the mean return is 0.0015 — barely one sigma from
#: zero — and the seed duly produced a "crash" that finished **+46%**. The
#: values below keep |mu| at three or more standard errors, and
#: :func:`assert_regimes_are_what_they_claim` re-checks the realised series for
#: every symbol before anything is written.
REGIMES: Tuple[Regime, ...] = (
    # CALM_RANGE is declared flat (mu == 0) rather than given a token +0.00003
    # drift. Over 1,500 bars at sigma 0.0032 the standard error of the mean is
    # 0.000083 — nearly three times that drift — so the drift was not a
    # property of the regime, it was a rounding error with a plus sign. A range
    # is held by mean reversion, which is what `mean_rev` is for.
    Regime("CALM_RANGE",   1500,  0.000000, 0.0032, 0.030,  2.0, 1.00, 1.00, 0.10, 1.0, 2),
    Regime("STRONG_TREND", 1200,  0.000450, 0.0060, 0.000,  3.0, 0.90, 1.05, 0.20, 1.5, 2),
    Regime("CRASH",         300, -0.003500, 0.0150, 0.000, 45.0, 0.30, 0.35, 0.80, 4.5, 8),
    Regime("CHOP",         2000,  0.000000, 0.0090, 0.090,  5.0, 0.80, 1.00, 0.14, 1.1, 2),
)


class SymbolSpec(NamedTuple):
    symbol_id: str            # CoinAPI-style symbol id
    file_stem: str            # basename without extension
    start_price: float
    tick: float               # price increment
    top_size: float           # typical resting size at the touch, base units
    trade_size: float         # typical size of one trade, base units
    price_dp: int             # decimals actually used by this instrument
    size_dp: int
    with_book: bool
    with_quotes: bool


SYMBOLS: Tuple[SymbolSpec, ...] = (
    SymbolSpec("BYBIT_SPOT_BTC_USDT", "BYBIT_SPOT_BTC_USDT",
               62_000.0, 0.1, 1.6, 0.045, 1, 6, True, True),
    SymbolSpec("BYBIT_SPOT_ETH_USDT", "BYBIT_SPOT_ETH_USDT",
               3_400.0, 0.01, 26.0, 0.75, 2, 5, True, False),
    SymbolSpec("BYBIT_SPOT_SOL_USDT", "BYBIT_SPOT_SOL_USDT",
               130.0, 0.001, 420.0, 12.0, 3, 4, False, False),
)


# ---------------------------------------------------------------------------
# deterministic randomness
# ---------------------------------------------------------------------------


def derive_seed(master: int, label: str) -> int:
    """A per-stream seed derived by hashing, not by adding a small offset.

    The first version used ``master + n * 1000`` per symbol and two of the three
    symbols drew a crash segment more than two standard errors above its own
    drift, in the same direction. Mersenne Twister mixes its seed well enough
    that this was probably coincidence — but "probably coincidence" is not a
    property you want underneath a fixture that other tests treat as ground
    truth. Hashing the label makes the streams unrelated by construction, so the
    question does not arise. blake2b, not ``hash()``: the builtin is salted per
    process and would make the corpus irreproducible.
    """
    digest = hashlib.blake2b(
        f"{master}:{label}".encode("utf-8"), digest_size=8
    ).digest()
    return int.from_bytes(digest, "big") % (2 ** 63)


class Rng:
    """A deterministic random source with an explicit, stable normal.

    ``random.Random`` is Mersenne Twister with a documented, version-stable
    stream. The normal is Box-Muller over two of its uniforms rather than
    ``random.gauss`` (which keeps a spare value in instance state, so the
    stream depends on the *interleaving* of calls) or ``normalvariate``
    (rejection sampling — the number of uniforms consumed varies). Both of
    those still reproduce, but only if every call site is preserved exactly;
    Box-Muller consumes exactly two uniforms per draw, which makes the stream
    robust to harmless refactors of the callers.
    """

    __slots__ = ("_r",)

    def __init__(self, seed: int) -> None:
        self._r = random.Random(seed)

    def uniform(self) -> float:
        """Uniform on [0, 1)."""
        return self._r.random()

    def normal(self) -> float:
        """Standard normal, exactly two uniforms consumed."""
        u1 = self._r.random()
        u2 = self._r.random()
        # random() can return exactly 0.0; log(0) is -inf.
        if u1 <= 1e-300:
            u1 = 1e-300
        return math.sqrt(-2.0 * math.log(u1)) * math.cos(2.0 * math.pi * u2)

    def between(self, low: float, high: float) -> float:
        return low + (high - low) * self.uniform()

    def chance(self, p: float) -> bool:
        return self.uniform() < p

    def pick(self, options: Sequence[str], weights: Sequence[float]) -> str:
        """Weighted choice consuming exactly one uniform."""
        total = float(sum(weights))
        target = self.uniform() * total
        running = 0.0
        for option, weight in zip(options, weights):
            running += weight
            if target < running:
                return option
        return options[-1]


# ---------------------------------------------------------------------------
# formatting — the CoinAPI wire contract
# ---------------------------------------------------------------------------


def iso_utc(micros: int) -> str:
    """Epoch microseconds -> ``2024-03-01T00:00:00.0000000Z``.

    Seven fractional digits, because CoinAPI flat files carry 100-nanosecond
    ticks. The corpus only ever populates microsecond resolution, so the last
    digit is always ``0`` — but emitting seven keeps a real CoinAPI file and a
    generated one byte-compatible in shape, which is the whole point of
    matching their schema.
    """
    if micros < 0:
        raise ValueError("timestamps before the epoch are not supported")
    stamp = _EPOCH + _dt.timedelta(microseconds=micros)
    return (
        f"{stamp.year:04d}-{stamp.month:02d}-{stamp.day:02d}"
        f"T{stamp.hour:02d}:{stamp.minute:02d}:{stamp.second:02d}"
        f".{stamp.microsecond * 10:07d}Z"
    )


def fmt_num(value: float, decimals: int) -> str:
    """Fixed-point with trailing zeros trimmed, capped at 9 decimals.

    Nine is the cap the whole pipeline agrees on (`market_data.normalize_price`
    rounds to the same place). Trimming keeps the files small and, more
    usefully, makes a price that needed more precision than the instrument's
    tick visible to the eye in a diff.
    """
    decimals = min(int(decimals), 9)
    if not math.isfinite(value):
        raise ValueError(f"refusing to serialise a non-finite value: {value!r}")
    text = f"{value:.{decimals}f}"
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    if text in ("", "-", "-0"):
        text = "0"
    return text


def round_to_tick(price: float, tick: float) -> float:
    """Snap to the instrument's price increment, then to 9 decimals.

    Two-stage on purpose: ``round(x / tick) * tick`` reintroduces binary
    representation dust (0.1 is not 0.1), and a level keyed on dusty floats
    never matches the level the next event means to update.
    """
    return round(round(price / tick) * tick, 9)


# ---------------------------------------------------------------------------
# deterministic file writing
# ---------------------------------------------------------------------------


class WrittenFile(NamedTuple):
    relpath: str
    rows: int
    sha256: str
    sha256_uncompressed: str
    size_bytes: int


def write_csv_gz(root: str, relpath: str, header: Sequence[str],
                 rows: Iterable[Sequence[str]]) -> WrittenFile:
    """Write a gzipped CSV with a **fixed** gzip header, and hash both forms.

    ``mtime=0`` and ``filename=""`` are what make the output byte-identical
    across runs. Both hashes are recorded: the compressed one is what
    ``verify_manifest`` checks by default, and the uncompressed one lets it
    distinguish "someone edited the data" from the much less alarming "someone
    re-compressed the file with a different gzip".
    """
    buffer = io.StringIO()
    count = 0
    buffer.write(",".join(header))
    buffer.write("\n")
    for row in rows:
        buffer.write(",".join(row))
        buffer.write("\n")
        count += 1
    raw = buffer.getvalue().encode("utf-8")

    packed = io.BytesIO()
    with gzip.GzipFile(fileobj=packed, mode="wb", compresslevel=9,
                       filename="", mtime=0) as handle:
        handle.write(raw)
    blob = packed.getvalue()

    path = os.path.join(root, relpath)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as out:
        out.write(blob)
    return WrittenFile(
        relpath.replace(os.sep, "/"), count,
        hashlib.sha256(blob).hexdigest(),
        hashlib.sha256(raw).hexdigest(),
        len(blob),
    )


def write_csv_plain(root: str, relpath: str, header: Sequence[str],
                    rows: Iterable[Sequence[str]]) -> WrittenFile:
    """Uncompressed CSV, used for the anomaly fixtures.

    They are deliberately small and deliberately readable: a defect fixture you
    cannot open in an editor is a defect fixture nobody checks.
    """
    lines = [",".join(header)]
    count = 0
    for row in rows:
        lines.append(",".join(row))
        count += 1
    raw = ("\n".join(lines) + "\n").encode("utf-8")
    path = os.path.join(root, relpath)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as out:
        out.write(raw)
    digest = hashlib.sha256(raw).hexdigest()
    return WrittenFile(relpath.replace(os.sep, "/"), count, digest, digest, len(raw))


# ---------------------------------------------------------------------------
# the price path
# ---------------------------------------------------------------------------


class GeneratedBar(NamedTuple):
    start_us: int
    end_us: int
    open_us: int
    close_us: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    trades: int
    regime: str
    path: Tuple[float, ...]   # the intra-bar tick path the OHLC was measured from


def regime_at(index: int) -> Tuple[Regime, int]:
    """(regime, index-within-regime) for a bar index."""
    cursor = 0
    for regime in REGIMES:
        if index < cursor + regime.bars:
            return regime, index - cursor
        cursor += regime.bars
    return REGIMES[-1], index - (cursor - REGIMES[-1].bars)


def regime_bounds() -> List[Tuple[Regime, int, int]]:
    """[(regime, first_bar_index, last_bar_index)] — inclusive."""
    out: List[Tuple[Regime, int, int]] = []
    cursor = 0
    for regime in REGIMES:
        out.append((regime, cursor, cursor + regime.bars - 1))
        cursor += regime.bars
    return out


def build_bars(spec: SymbolSpec, rng: Rng, bars: int = BARS_PER_SYMBOL) -> List[GeneratedBar]:
    """Generate the OHLCV series from an explicit intra-bar tick path.

    The OHLC of a bar is *measured*, never invented: ``open`` is the first tick,
    ``close`` the last, ``high``/``low`` the extremes of the path. That is what
    guarantees the bracketing invariant the loader enforces, and it means the
    next bar can open exactly on this bar's close so a bar-to-bar gap check has
    something true to compare against.

    Volume is an AR(1) in log space (so it clusters and is autocorrelated, like
    real volume) multiplied by a term in ``|return|`` (so the biggest bars are
    also the busiest, like real volume). i.i.d. volume would make every
    volume-normalising indicator behave better here than it will live.
    """
    out: List[GeneratedBar] = []
    price = spec.start_price
    anchor = price
    log_volume = math.log(spec.top_size * 900.0)
    substep_count = SUBSTEPS_PER_BAR

    for index in range(bars):
        regime, _ = regime_at(index)
        start_us = START_US + index * BAR_SECONDS * 1_000_000
        end_us = start_us + BAR_SECONDS * 1_000_000

        step_mu = regime.mu / substep_count
        step_sigma = regime.sigma / math.sqrt(substep_count)
        # The anchor is an EMA of price; mean_rev pulls toward it. With
        # mean_rev = 0 this term vanishes and the phase is a pure drift+noise
        # walk, which is what a trend should be.
        #
        # The pull is CLAMPED to half a bar's volatility. Without the clamp the
        # anchor lags a crash by tens of percent, and the chop phase that
        # follows inherits a mechanical +5%/bar recovery drift — which is not
        # chop, it is the strongest trend in the file wearing chop's label.
        # Mean reversion is a nudge; a term that can dominate sigma is a
        # different regime pretending to be a parameter.
        pull = regime.mean_rev * (math.log(anchor / price) if price > 0 else 0.0)
        limit = 0.5 * regime.sigma
        pull = max(-limit, min(limit, pull))

        path: List[float] = [price]
        for _ in range(substep_count):
            shock = step_mu + pull / substep_count + step_sigma * rng.normal()
            price = max(price * math.exp(shock), spec.tick)
            path.append(round(price, 9))

        opened = path[0]
        closed = path[-1]
        high = max(path)
        low = min(path)
        bar_return = (closed - opened) / opened if opened else 0.0
        anchor = anchor * 0.98 + closed * 0.02

        # AR(1) in log space: phi=0.88 gives visible clustering without the
        # series wandering off, and the |return| term couples volume to moves.
        target = math.log(spec.top_size * 900.0 * regime.volume_mult)
        log_volume = 0.88 * log_volume + 0.12 * target + 0.20 * rng.normal()
        volume = math.exp(log_volume) * (1.0 + 7.0 * abs(bar_return))
        trades = max(1, int(volume / spec.trade_size * rng.between(0.8, 1.2)))

        # A bar's first and last trades do not sit exactly on its boundaries.
        open_us = start_us + int(rng.between(5_000, 900_000))
        close_us = end_us - 1 - int(rng.between(5_000, 900_000))

        out.append(GeneratedBar(
            start_us, end_us, open_us, close_us,
            round(opened, 9), round(high, 9), round(low, 9), round(closed, 9),
            round(volume, 8), trades, regime.name, tuple(path),
        ))
    return out


class RegimeMismatch(RuntimeError):
    """A generated regime does not do what its label says.

    Raised *before* anything is written. A corpus whose CRASH phase finished up
    46% would still load, still backtest and still produce a number — a number
    that is wrong in a way no downstream test could detect, because every
    downstream test trusts the label. Refusing to write is the only failure
    mode that surfaces it.
    """


def assert_regimes_are_what_they_claim(symbol: str,
                                       bars: Sequence[GeneratedBar]) -> None:
    """Check each phase's realised drift against the sign its name promises.

    Directional regimes must end up with the **sign** of their target drift and
    a magnitude of at least one standard error — that is, the move has to be
    visible above the phase's own noise. Flat regimes (``mu == 0``) must not
    have drifted more than 2.5 standard errors, which is what separates "chop"
    from "a trend I forgot to label".

    Note what is deliberately *not* checked: that the realised drift lands near
    the target. Over 300 bars at crash volatility the standard error of the
    mean return is a third of the drift itself, so realised and target differ by
    a sigma or two by construction. Requiring them to agree would not be a
    quality bar, it would be an instruction to keep trying seeds until one
    looked tidy — which is the same selection bias that makes strategy
    backtests lie, applied to the fixture instead of the strategy. The sign is
    the part of the label that has to be true; the magnitude is a draw.
    """
    for regime, first, last in regime_bounds():
        if first >= len(bars):
            break
        segment = bars[first:min(last, len(bars) - 1) + 1]
        if len(segment) < 2:
            continue
        returns = [math.log(bar.close / bar.open) for bar in segment
                   if bar.open > 0 and bar.close > 0]
        realised = sum(returns) / len(returns)
        stderr = regime.sigma / math.sqrt(len(returns))
        if regime.mu == 0.0:
            # 2.5 standard errors, not 2: with several flat phases across
            # several symbols, a 2-sigma band fails on roughly one seed in
            # four for no reason worth acting on. A phase that clears 2.5 has
            # genuinely trended, and the right response is a different seed or
            # a different label — not a looser check.
            if abs(realised) > 2.5 * stderr:
                raise RegimeMismatch(
                    f"{symbol}/{regime.name}: labelled flat but drifted "
                    f"{realised:+.6f}/bar ({realised / stderr:+.1f} standard "
                    f"errors); it is a trend wearing a flat label"
                )
            continue
        if realised * regime.mu <= 0.0 or abs(realised) < stderr:
            raise RegimeMismatch(
                f"{symbol}/{regime.name}: target drift {regime.mu:+.6f}/bar, "
                f"realised {realised:+.6f}/bar over {len(returns)} bars "
                f"(stderr {stderr:.6f}). The label does not describe the data; "
                f"raise |mu| or lower sigma rather than shipping it."
            )


def price_at(bars: Sequence[GeneratedBar], micros: int) -> Tuple[float, float, Regime]:
    """(price, recent absolute return, regime) at an arbitrary instant.

    Interpolates *within* the bar's recorded tick path rather than between bar
    closes, so a book sampled mid-bar is consistent with the OHLC the loader
    will read for that same bar. A book that disagrees with its own candles is
    the kind of fixture that makes a real bug look like a fixture bug.
    """
    first = bars[0].start_us
    span = bars[0].end_us - bars[0].start_us
    index = min(max((micros - first) // span, 0), len(bars) - 1)
    bar = bars[index]
    offset = min(max(micros - bar.start_us, 0), span - 1)
    slot = offset * (len(bar.path) - 1) // span
    price = bar.path[slot]
    recent = abs(bar.close - bar.open) / bar.open if bar.open else 0.0
    regime, _ = regime_at(index)
    return price, recent, regime


# ---------------------------------------------------------------------------
# the book model
# ---------------------------------------------------------------------------


def model_quote(spec: SymbolSpec, rng: Rng, mid: float, recent: float,
                regime: Regime) -> Tuple[float, float]:
    """(best_bid, best_ask) for an instant. Always at least one tick apart.

    Spread scales with the regime baseline *and* with how much the bar is
    actually moving, which is the empirical relationship: spread is the market
    charging for the risk of being run over.
    """
    spread_bps = regime.spread_bps * rng.between(0.7, 1.35) * (1.0 + 9.0 * recent)
    half = mid * spread_bps / 20_000.0
    bid = round_to_tick(mid - half, spec.tick)
    ask = round_to_tick(mid + half, spec.tick)
    if ask <= bid:
        ask = round(bid + spec.tick, 9)
    return bid, ask


def model_book(spec: SymbolSpec, rng: Rng, bid: float, ask: float,
               regime: Regime) -> Tuple[Dict[float, float], Dict[float, float]]:
    """The full resting book at an instant, as {price: size} per side.

    Depth decays geometrically away from the touch. In the crash the *bid*
    decays faster and starts smaller (``bid_skew``), so walking the bid with
    size costs far more than walking the ask — which is the whole point of
    modelling a book: a stop-loss is a market sell into exactly that.
    """
    # 4 bps between levels, so 20 levels span ~80 bps of price. That is chosen
    # against the sampling rate, not for looks: if the book is shallower than a
    # typical move between two samples, every sample invalidates every level and
    # the "incremental" stream degenerates into a snapshot in disguise —
    # thirteen events per sample instead of five, and no staleness left to test.
    step = max(spec.tick, round_to_tick(bid * 4.0e-4, spec.tick))
    # The touch sits exactly where the quote put it (tick-aligned). Everything
    # *behind* it sits on a fixed price GRID rather than at fixed offsets from
    # the touch, because real resting orders are at absolute prices and do not
    # follow the mid around. Anchoring every level to the touch instead means a
    # one-tick move renames all forty levels, so every sample rewrites the whole
    # book and the event stream stops carrying any information about which
    # prices actually changed.
    bid_grid = math.floor(round(bid / step, 9)) * step
    ask_grid = math.ceil(round(ask / step, 9)) * step

    bid_prices = [bid]
    ask_prices = [ask]
    for level in range(BOOK_LEVELS * 2):
        candidate = round(bid_grid - level * step, 9)
        if candidate < bid and candidate > 0 and len(bid_prices) < BOOK_LEVELS:
            bid_prices.append(candidate)
        candidate = round(ask_grid + level * step, 9)
        if candidate > ask and len(ask_prices) < BOOK_LEVELS:
            ask_prices.append(candidate)

    bid_base = spec.top_size * regime.depth * regime.bid_skew
    ask_base = spec.top_size * regime.depth
    bid_decay = 0.30 if regime.name == "CRASH" else 0.12
    ask_decay = 0.10

    bids: Dict[float, float] = {}
    asks: Dict[float, float] = {}
    for level, price in enumerate(bid_prices):
        bids[price] = round(
            bid_base * math.exp(-bid_decay * level) * rng.between(0.55, 1.45),
            spec.size_dp,
        )
    for level, price in enumerate(ask_prices):
        asks[price] = round(
            ask_base * math.exp(-ask_decay * level) * rng.between(0.55, 1.45),
            spec.size_dp,
        )
    # A level rounded to zero is a level that is not there.
    bids = {p: s for p, s in bids.items() if s > 0}
    asks = {p: s for p, s in asks.items() if s > 0}
    return bids, asks


def latency_us(rng: Rng) -> int:
    """Collection latency: a few hundred microseconds to a few milliseconds.

    Squared uniform so most samples are fast and the tail is long, which is what
    a collector behind a network actually looks like.
    """
    return 250 + int(3_800.0 * (rng.uniform() ** 2))


class BookEmitter:
    """Emits an L2 event stream whose *reconstruction* is a valid book.

    The generator keeps two books: the ``target`` from the model, and
    ``emitted`` — what a consumer replaying the stream would hold. Each sample
    emits a bounded number of events to move ``emitted`` toward ``target``.
    Bounding it is deliberate: a real sampled feed is always slightly stale, and
    a corpus where every level is refreshed every tick would hide staleness bugs
    instead of exposing them.

    Two classes of event are **never** dropped by the bound:

    * deletes of any emitted level that the new touch has crossed, and
    * the new top of book.

    Without those, staleness could leave the reconstructed book crossed, and a
    crossed book is supposed to mean "something is wrong", not "the fixture is
    lazy". That invariant is asserted by the tests that replay this file.
    """

    def __init__(self, spec: SymbolSpec, rng: Rng) -> None:
        self.spec = spec
        self.rng = rng
        self.bids: Dict[float, float] = {}
        self.asks: Dict[float, float] = {}
        self.rows: List[Tuple[str, ...]] = []
        self.match_events = 0
        self.snapshot_events = 0
        self._last_coinapi = 0

    # -- serialisation ----------------------------------------------------

    def _row(self, when: int, is_buy: bool, px: float, sx: float,
             update_type: str) -> None:
        self.rows.append((
            self.spec.symbol_id,
            iso_utc(when),
            iso_utc(self._receive(when)),
            "true" if is_buy else "false",
            fmt_num(px, self.spec.price_dp),
            fmt_num(sx, self.spec.size_dp),
            update_type,
        ))

    def _receive(self, when: int) -> int:
        """``time_coinapi`` for an event: after ``when``, and monotonic.

        Independent latency draws alone are wrong. Forty snapshot rows share one
        ``time_exchange``, so independent draws hand them receive-times in
        random order — and a collector's own clock does not run backwards
        between two packets it has already written to the same file. Left
        unfixed, this made ``time_coinapi`` non-monotonic within every snapshot
        batch, which is not "realistic jitter": it is a receive clock that
        cannot exist, and it makes the no-lookahead cut ambiguous exactly where
        the book is being rebuilt from scratch.
        """
        received = when + latency_us(self.rng)
        if received <= self._last_coinapi:
            received = self._last_coinapi + 1
        self._last_coinapi = received
        return received

    # -- event kinds ------------------------------------------------------

    def snapshot(self, when: int, bids: Dict[float, float],
                 asks: Dict[float, float]) -> None:
        """Full replace. Every row in the batch shares one ``time_exchange``.

        That shared timestamp is the batch delimiter: it is how a consumer
        knows where one snapshot ends and the next begins, and
        ``market_data.BookState`` keys its "clear the book" decision on exactly
        that. Bids descending then asks ascending, which is the order the
        exchange sends and the order a reader expects.
        """
        self.bids = dict(bids)
        self.asks = dict(asks)
        for px in sorted(bids, reverse=True):
            self._row(when, True, px, bids[px], "SNAPSHOT")
            self.snapshot_events += 1
        for px in sorted(asks):
            self._row(when, False, px, asks[px], "SNAPSHOT")
            self.snapshot_events += 1

    def matches(self, when: int, count: int, sell_side: bool) -> None:
        """Trades consuming the touch. ``sell_side`` means hitting the bid."""
        for _ in range(count):
            book = self.bids if sell_side else self.asks
            if not book:
                return
            px = max(book) if sell_side else min(book)
            available = book[px]
            taken = round(min(available, self.spec.trade_size *
                              self.rng.between(0.4, 3.0)), self.spec.size_dp)
            if taken <= 0:
                return
            self._row(when, sell_side, px, taken, "MATCH")
            self.match_events += 1
            left = round(available - taken, self.spec.size_dp)
            if left > 0:
                book[px] = left
            else:
                del book[px]

    def converge(self, when: int, bids: Dict[float, float],
                 asks: Dict[float, float], budget: int) -> None:
        """Move the emitted book toward the target with at most ~``budget`` events."""
        best_bid = max(bids) if bids else None
        best_ask = min(asks) if asks else None

        mandatory: List[Tuple[bool, float, float, str]] = []
        optional: List[Tuple[float, Tuple[bool, float, float, str]]] = []

        # 1. Anything stale that the new touch has crossed must go, or the
        #    reconstruction is a crossed book that means nothing.
        if best_ask is not None:
            for px in sorted([p for p in self.bids if p >= best_ask], reverse=True):
                mandatory.append((True, px, 0.0, "DELETE"))
        if best_bid is not None:
            for px in sorted([p for p in self.asks if p <= best_bid]):
                mandatory.append((False, px, 0.0, "DELETE"))

        # 1b. Prune levels that price has walked away from. Without this the
        #     emitted book only ever grows: in a trend, every bid the market
        #     leaves behind stays in the reconstruction forever, so `depth to N
        #     levels` stays honest but the state itself becomes an unbounded
        #     memory leak that a test measuring depth would never notice.
        #
        #     The band is one book-width beyond the model's own range: the
        #     reconstruction then holds roughly twice the snapshot depth, which
        #     leaves real staleness to test while keeping the far side from
        #     silently accumulating hundreds of abandoned levels. At three
        #     widths the crash phase reached 97 stale ask levels against 10
        #     bids — the right *direction* for a crash, reached for an
        #     artefactual reason, which is the kind of fixture that makes a
        #     later measurement look meaningful when it is not.
        if bids and asks:
            low, high = min(bids), max(asks)
            width = max(high - low, self.spec.tick)
            for px in sorted([p for p in self.bids if p < low - width],
                             reverse=True):
                mandatory.append((True, px, 0.0, "DELETE"))
            for px in sorted([p for p in self.asks if p > high + width]):
                mandatory.append((False, px, 0.0, "DELETE"))

        # 2. The new touch itself is always published.
        for is_buy, side_target, side_emitted, touch in (
            (True, bids, self.bids, best_bid),
            (False, asks, self.asks, best_ask),
        ):
            if touch is None:
                continue
            want = side_target[touch]
            have = side_emitted.get(touch)
            if have is None or abs(have - want) > 1e-12:
                mandatory.append((is_buy, touch, want, "SET"))

        # 3. Everything else competes for the remaining budget, largest
        #    absolute change first — which is also the order a real feed's
        #    conflation would pick.
        for is_buy, side_target, side_emitted in (
            (True, bids, self.bids), (False, asks, self.asks),
        ):
            for px, want in side_target.items():
                if px == (best_bid if is_buy else best_ask):
                    continue
                have = side_emitted.get(px)
                if have is None:
                    optional.append((want, (is_buy, px, want, "ADD")))
                    continue
                delta = round(want - have, self.spec.size_dp)
                if abs(delta) <= 0:
                    continue
                kind = "ADD" if delta > 0 else "SUB"
                optional.append((abs(delta), (is_buy, px, abs(delta), kind)))
            for px in side_emitted:
                if px not in side_target:
                    optional.append((0.0, (is_buy, px, 0.0, "DELETE")))

        optional.sort(key=lambda item: -item[0])
        chosen = [event for event in mandatory]
        chosen.extend(event for _, event in optional[:max(0, budget)])

        for is_buy, px, sx, kind in chosen:
            self._apply(is_buy, px, sx, kind)
            self._row(when, is_buy, px, sx, kind)

    def _apply(self, is_buy: bool, px: float, sx: float, kind: str) -> None:
        """Apply to the emitted book with exactly the semantics the loader has."""
        book = self.bids if is_buy else self.asks
        if kind == "DELETE":
            book.pop(px, None)
        elif kind == "SET":
            if sx > 0:
                book[px] = sx
            else:
                book.pop(px, None)
        elif kind == "ADD":
            book[px] = round(book.get(px, 0.0) + sx, self.spec.size_dp)
        elif kind == "SUB":
            left = round(book.get(px, 0.0) - sx, self.spec.size_dp)
            if left > 0:
                book[px] = left
            else:
                book.pop(px, None)
        else:                                     # pragma: no cover - guarded above
            raise ValueError(f"unknown update type {kind!r}")


def build_book_rows(spec: SymbolSpec, rng: Rng,
                    bars: Sequence[GeneratedBar]) -> Tuple[List[Tuple[str, ...]], Dict[str, int]]:
    """The full L2 event stream for one symbol, with a SNAPSHOT at each regime edge."""
    emitter = BookEmitter(spec, rng)
    bounds = {first: regime for regime, first, _ in regime_bounds()}
    span = bars[0].end_us - bars[0].start_us

    for index, bar in enumerate(bars):
        regime, _ = regime_at(index)
        samples = regime.samples_per_bar

        if index in bounds:
            when = bar.start_us
            mid, recent, _ = price_at(bars, when)
            bid, ask = model_quote(spec, rng, mid, recent, regime)
            emitter.snapshot(when, *model_book(spec, rng, bid, ask, regime))

        for sample in range(samples):
            when = bar.start_us + (span * sample) // samples
            if index in bounds and sample == 0:
                continue                       # the snapshot already covered it
            mid, recent, _ = price_at(bars, when)
            bid, ask = model_quote(spec, rng, mid, recent, regime)
            target_bids, target_asks = model_book(spec, rng, bid, ask, regime)
            if rng.chance(regime.match_p):
                # Crash flow is overwhelmingly sells hitting the bid.
                sell_side = rng.chance(0.85 if regime.name == "CRASH" else 0.5)
                emitter.matches(when, 1 + int(rng.uniform() * 3), sell_side)
            # The budget caps only the *optional* (size-refresh) events;
            # deletes and the touch are always published. At a budget of 4 the
            # volatile phases starved — deletes outran refreshes and the
            # reconstructed book fell to nine levels a side, so a "depth to 10
            # levels" feature was quietly measuring depth to nine. 6 (10 in the
            # crash) keeps the reconstruction near the model's 20.
            emitter.converge(when, target_bids, target_asks,
                             budget=10 if regime.name == "CRASH" else 6)

    stats = {
        "match_events": emitter.match_events,
        "snapshot_events": emitter.snapshot_events,
    }
    return emitter.rows, stats


def build_quote_rows(spec: SymbolSpec, rng: Rng,
                     bars: Sequence[GeneratedBar]) -> List[Tuple[str, ...]]:
    """Top-of-book quotes, sampled four times an hour (more in the crash)."""
    rows: List[Tuple[str, ...]] = []
    span = bars[0].end_us - bars[0].start_us
    last_received = 0
    for index, bar in enumerate(bars):
        regime, _ = regime_at(index)
        samples = 4 if regime.name != "CRASH" else 12
        for sample in range(samples):
            when = bar.start_us + (span * sample) // samples
            mid, recent, _ = price_at(bars, when)
            bid, ask = model_quote(spec, rng, mid, recent, regime)
            bids, asks = model_book(spec, rng, bid, ask, regime)
            if not bids or not asks:
                continue
            # Monotonic receive clock, same reason as BookEmitter._receive.
            received = max(when + latency_us(rng), last_received + 1)
            last_received = received
            rows.append((
                spec.symbol_id,
                iso_utc(when),
                iso_utc(received),
                fmt_num(ask, spec.price_dp),
                fmt_num(asks[ask], spec.size_dp),
                fmt_num(bid, spec.price_dp),
                fmt_num(bids[bid], spec.size_dp),
            ))
    return rows


def ohlcv_rows(spec: SymbolSpec, bars: Sequence[GeneratedBar]) -> List[Tuple[str, ...]]:
    return [
        (
            iso_utc(bar.start_us), iso_utc(bar.end_us),
            iso_utc(bar.open_us), iso_utc(bar.close_us),
            fmt_num(bar.open, spec.price_dp), fmt_num(bar.high, spec.price_dp),
            fmt_num(bar.low, spec.price_dp), fmt_num(bar.close, spec.price_dp),
            fmt_num(bar.volume, spec.size_dp), str(bar.trades),
        )
        for bar in bars
    ]


# ---------------------------------------------------------------------------
# deliberate defects
# ---------------------------------------------------------------------------


#: (filename, kind, expected loader rejection reason, prose for data/README.md)
ANOMALY_DOCS: Tuple[Tuple[str, str, str, str], ...] = (
    ("ohlcv_duplicate_timestamp.csv", "ohlcv", "DUPLICATE_TIMESTAMP",
     "the third bar repeats the second bar's period start"),
    ("ohlcv_out_of_order.csv", "ohlcv", "NON_MONOTONIC_TIMESTAMP",
     "the third bar is stamped one hour *before* the second"),
    ("ohlcv_high_low_not_bracketing.csv", "ohlcv", "HIGH_LOW_NOT_BRACKETING",
     "a bar whose high is below its close, which no real bar can be"),
    ("ohlcv_negative_volume.csv", "ohlcv", "NEGATIVE_VALUE",
     "a bar with volume -12.5"),
    ("ohlcv_non_finite.csv", "ohlcv", "NON_FINITE_VALUE",
     "a bar whose close is the literal 'NaN', as some feeds emit on a gap"),
    ("ohlcv_malformed_row.csv", "ohlcv", "MALFORMED_ROW",
     "a truncated row with four columns instead of ten"),
    ("ohlcv_bad_timestamp.csv", "ohlcv", "BAD_TIMESTAMP",
     "a timestamp with no 'Z' and a two-digit year"),
    ("quotes_crossed_book.csv", "quotes", "CROSSED_BOOK",
     "a quote whose bid is above its ask"),
    ("quotes_absurd_spread.csv", "quotes", "SPREAD_ANOMALY",
     "a quote whose spread is 100% of mid, far beyond MAX_SPREAD_ANOMALY_PCT (0.67)"),
    ("quotes_zero_size.csv", "quotes", "ZERO_SIZE",
     "a quote with a zero bid size — a price nobody will actually trade"),
    ("l2_zero_size.csv", "book", "ZERO_SIZE",
     "a SNAPSHOT level with size 0 (legal for SET, meaningless for SNAPSHOT)"),
    ("l2_unknown_update_type.csv", "book", "UNKNOWN_UPDATE_TYPE",
     "an update_type of 'REPLACE', which is not in the CoinAPI vocabulary"),
    ("l2_negative_latency.csv", "book", "NEGATIVE_LATENCY",
     "time_coinapi *before* time_exchange — the collector cannot receive a "
     "message before it is sent, so this is a clock or a parse bug"),
)


def _ohlcv_fixture_row(index: int, open_: float, high: float, low: float,
                       close: float, volume: str = "12.5",
                       trades: str = "310",
                       start_override: Optional[str] = None) -> Tuple[str, ...]:
    start = START_US + index * BAR_SECONDS * 1_000_000
    end = start + BAR_SECONDS * 1_000_000
    return (
        start_override or iso_utc(start), iso_utc(end),
        iso_utc(start + 1_000), iso_utc(end - 1_000),
        fmt_num(open_, 2), fmt_num(high, 2), fmt_num(low, 2), fmt_num(close, 2),
        volume, trades,
    )


def _quote_fixture_row(index: int, ask: float, ask_sx: float,
                       bid: float, bid_sx: float) -> Tuple[str, ...]:
    when = START_US + index * 60_000_000
    return (
        "BYBIT_SPOT_BTC_USDT", iso_utc(when), iso_utc(when + 900),
        fmt_num(ask, 2), fmt_num(ask_sx, 4), fmt_num(bid, 2), fmt_num(bid_sx, 4),
    )


def _book_fixture_row(index: int, is_buy: bool, px: float, sx: float,
                      kind: str, latency: int = 800) -> Tuple[str, ...]:
    when = START_US + index * 1_000_000
    return (
        "BYBIT_SPOT_BTC_USDT", iso_utc(when), iso_utc(when + latency),
        "true" if is_buy else "false",
        fmt_num(px, 2), fmt_num(sx, 4), kind,
    )


def build_anomalies() -> Dict[str, Tuple[Sequence[str], List[Tuple[str, ...]]]]:
    """Every fixture: a few valid rows plus exactly one defective row.

    Exactly one, and the valid rows around it are genuinely valid, so a test can
    assert two things at once: the defect is rejected *and* the surrounding data
    survives. A loader that rejects the whole file on one bad row is a different
    bug, and this shape catches it.
    """
    good3 = [
        _ohlcv_fixture_row(0, 62000.0, 62150.0, 61950.0, 62100.0),
        _ohlcv_fixture_row(1, 62100.0, 62300.0, 62050.0, 62250.0),
    ]
    files: Dict[str, Tuple[Sequence[str], List[Tuple[str, ...]]]] = {}

    dup = list(good3)
    repeat = list(_ohlcv_fixture_row(2, 62250.0, 62400.0, 62200.0, 62380.0))
    repeat[0] = good3[1][0]                       # same period start as bar 1
    dup.append(tuple(repeat))
    dup.append(_ohlcv_fixture_row(3, 62380.0, 62500.0, 62300.0, 62420.0))
    files["ohlcv_duplicate_timestamp.csv"] = (OHLCV_COLUMNS, dup)

    # Bars 0, 1, 3, then 2 — an inversion that is NOT also a duplicate. The
    # first draft used bar 0 again, which tripped DUPLICATE_TIMESTAMP first and
    # left the ordering path with no fixture at all. Two defects in one row
    # test whichever check happens to run first, which is to say neither.
    unordered = list(good3)
    unordered.append(_ohlcv_fixture_row(3, 62250.0, 62400.0, 62200.0, 62380.0))
    unordered.append(_ohlcv_fixture_row(2, 62380.0, 62500.0, 62300.0, 62420.0))
    files["ohlcv_out_of_order.csv"] = (OHLCV_COLUMNS, unordered)

    bracket = list(good3)
    bracket.append(_ohlcv_fixture_row(2, 62250.0, 62300.0, 62200.0, 62380.0))
    files["ohlcv_high_low_not_bracketing.csv"] = (OHLCV_COLUMNS, bracket)

    negative = list(good3)
    negative.append(_ohlcv_fixture_row(2, 62250.0, 62400.0, 62200.0, 62380.0,
                                       volume="-12.5"))
    files["ohlcv_negative_volume.csv"] = (OHLCV_COLUMNS, negative)

    nonfinite = list(good3)
    nan_row = list(_ohlcv_fixture_row(2, 62250.0, 62400.0, 62200.0, 62380.0))
    nan_row[7] = "NaN"
    nonfinite.append(tuple(nan_row))
    files["ohlcv_non_finite.csv"] = (OHLCV_COLUMNS, nonfinite)

    truncated = list(good3)
    truncated.append(tuple(_ohlcv_fixture_row(2, 1.0, 2.0, 0.5, 1.5)[:4]))
    files["ohlcv_malformed_row.csv"] = (OHLCV_COLUMNS, truncated)

    bad_time = list(good3)
    bad_time.append(_ohlcv_fixture_row(2, 62250.0, 62400.0, 62200.0, 62380.0,
                                       start_override="24-03-01 02:00:00"))
    files["ohlcv_bad_timestamp.csv"] = (OHLCV_COLUMNS, bad_time)

    quotes_good = [
        _quote_fixture_row(0, 62010.0, 1.4, 62000.0, 1.2),
        _quote_fixture_row(1, 62020.0, 1.1, 62012.0, 0.9),
    ]
    files["quotes_crossed_book.csv"] = (
        QUOTE_COLUMNS, quotes_good + [_quote_fixture_row(2, 62000.0, 1.0, 62050.0, 1.0)],
    )
    files["quotes_absurd_spread.csv"] = (
        QUOTE_COLUMNS, quotes_good + [_quote_fixture_row(2, 150.0, 1.0, 50.0, 1.0)],
    )
    files["quotes_zero_size.csv"] = (
        QUOTE_COLUMNS, quotes_good + [_quote_fixture_row(2, 62030.0, 1.0, 62025.0, 0.0)],
    )

    book_good = [
        _book_fixture_row(0, True, 62000.0, 1.5, "SNAPSHOT"),
        _book_fixture_row(0, False, 62010.0, 1.3, "SNAPSHOT"),
    ]
    files["l2_zero_size.csv"] = (
        BOOK_COLUMNS, book_good + [_book_fixture_row(0, True, 61990.0, 0.0, "SNAPSHOT")],
    )
    files["l2_unknown_update_type.csv"] = (
        BOOK_COLUMNS, book_good + [_book_fixture_row(1, True, 62001.0, 1.0, "REPLACE")],
    )
    files["l2_negative_latency.csv"] = (
        BOOK_COLUMNS, book_good + [_book_fixture_row(1, True, 62001.0, 1.0, "SET",
                                                     latency=-5_000)],
    )
    return files


# ---------------------------------------------------------------------------
# README
# ---------------------------------------------------------------------------


def render_readme(manifest: Dict[str, object]) -> str:
    files = manifest["files"]                     # type: ignore[index]
    assert isinstance(files, dict)
    corpus_lines = []
    for relpath in sorted(files):
        entry = files[relpath]
        if entry["kind"] == "anomaly":            # type: ignore[index]
            continue
        corpus_lines.append(
            f"| `{relpath}` | {entry['kind']} | {entry['rows']:,} | "        # type: ignore[index]
            f"{entry['size_bytes'] / 1024:,.0f} KiB | "                      # type: ignore[index]
            f"{entry['first_timestamp'][:10]} → {entry['last_timestamp'][:10]} |"  # type: ignore[index]
        )

    anomaly_lines = [
        f"| `anomalies/{name}` | `{reason}` | {prose} |"
        for name, _kind, reason, prose in ANOMALY_DOCS
    ]

    regime_lines = []
    for regime in REGIMES:
        regime_lines.append(
            f"| `{regime.name}` | {regime.bars:,} | {regime.mu * 100:+.4f}% | "
            f"{regime.sigma * 100:.2f}% | {regime.spread_bps:.0f} bps | "
            f"×{regime.depth:.2f} | ×{regime.bid_skew:.2f} |"
        )

    return f"""# `data/` — SYNTHETIC data generated by `tools/make_dataset.py`. NOT A MARKET.

> **THIS IS SYNTHETIC DATA GENERATED BY `tools/make_dataset.py`. IT IS NOT A
> MARKET.** Nothing here was observed on an exchange. It is a stochastic model
> with microstructure features placed on purpose. Use it to test *code*. Any
> return, Sharpe or win rate measured on it is a property of the generator and
> says nothing whatsoever about edge. Do not put a number derived from this
> corpus in a document that a person might mistake for a backtest.

Regenerate with:

```bash
python3 tools/make_dataset.py            # seed {manifest['seed']}, byte-identical every run
python3 tools/make_dataset.py --check    # regenerate to a temp dir and diff the hashes
```

Verify what is on disk against the shipped hashes:

```python
from market_data import verify_manifest
report = verify_manifest()
assert report.ok, report.problems
```

---

## Layout

| File | Kind | Rows | Size | Span (UTC) |
|---|---|---:|---:|---|
{chr(10).join(corpus_lines)}

Schema is **CoinAPI flat-file shaped**, because that is the vendor the operator
intends to buy from. Timestamps are ISO 8601 UTC with seven fractional digits
and a trailing `Z` (CoinAPI's 100-nanosecond ticks); this corpus only populates
microsecond resolution, so the seventh digit is always `0`.

### OHLCV — `ohlcv/*.csv.gz`

```
{','.join(OHLCV_COLUMNS)}
```

### L2 book events — `orderbook/*.csv.gz`

```
{','.join(BOOK_COLUMNS)}
```

`update_type` ∈ `SNAPSHOT`, `ADD`, `SUB`, `MATCH`, `DELETE`, `SET`. A full
snapshot (both sides, {BOOK_LEVELS} levels each) is emitted at the start of the
series and again at every regime boundary; between them the stream is
incremental and **sampled**, so the reconstructed book is always slightly
stale — as a real sampled feed is. All rows of one snapshot share a single
`time_exchange`; that shared value is the batch delimiter.

### Quotes — `quotes/*.csv.gz`

```
{','.join(QUOTE_COLUMNS)}
```

`time_coinapi` is always **at or after** `time_exchange` by a few hundred
microseconds to a few milliseconds. That is collection latency, and it is
modelled because it is the difference between "when it happened" and "when you
could have known" — `market_data.replay()` keys its no-lookahead cut on
`time_coinapi` for exactly that reason.

`time_coinapi` is also **strictly monotonic** across each file. A collector's
receive clock does not run backwards between two packets it has already written
to the same file, and independent latency draws would otherwise hand the forty
rows of one snapshot batch (which share a single `time_exchange`) their receive
times in random order — making the no-lookahead cut ambiguous at exactly the
points where the book is rebuilt from scratch.

---

## Regimes

The series walks four labelled phases in order. A strategy measured on one
phase has been measured on one sample.

| Regime | Bars | Drift/bar | Vol/bar | Base spread | Depth | Bid skew |
|---|---:|---:|---:|---:|---:|---:|
{chr(10).join(regime_lines)}

`CRASH` is the phase that justifies shipping a book at all: the spread widens
by more than an order of magnitude, total depth drops to ~30% of calm, the
**bid** thins to about a third of the ask, and MATCH events (sells eating the
bid) dominate the flow. That combination is what makes a stop-loss expensive
precisely when it fires, and no constant-slippage backtest can see it.

---

## Deliberate defects — `anomalies/`

These files are **broken on purpose**. They exist so that every rejection path
in `market_data.py` has a real artefact to reject, rather than a mock that
agrees with the code by construction. Each file holds two valid rows and
exactly one defective row, so a test can assert both that the defect is caught
*and* that the good rows survive it.

| File | Expected rejection | Defect |
|---|---|---|
{chr(10).join(anomaly_lines)}

They are shipped **uncompressed** so they can be opened in an editor. A fixture
nobody can read is a fixture nobody checks.

---

## Swapping in real data

The point of matching CoinAPI's schema is that this is a file copy.

**CoinAPI flat files.** Order the `ohlcv_1hour`, `orderbook_l2` and `quotes`
datasets for `BYBIT_SPOT_BTC_USDT`, `BYBIT_SPOT_ETH_USDT`,
`BYBIT_SPOT_SOL_USDT`. Drop them in with the same filenames and the same
columns, gzipped. Nothing in `market_data.py` needs to change — the column
names, the timestamp grammar and the `update_type` vocabulary are already
theirs.

**Bybit klines** (`GET /v5/market/kline`) need a transform: Bybit returns
`[startTime, open, high, low, close, volume, turnover]` with `startTime` in
epoch **milliseconds** and the array in **descending** time order. To convert:

1. reverse the array, and drop the newest bar if the interval is still open —
   an unclosed candle repainted mid-flight is the most common source of
   accidental lookahead;
2. `time_period_start = iso(startTime)`, `time_period_end = iso(startTime + 3600000)`;
3. set `time_open`/`time_close` to the same bounds (Bybit does not report first
   and last trade times) — `market_data` only requires them to fall inside the
   period;
4. `trades_count` is not reported by Bybit; write `0` rather than inventing one.

**After swapping anything**, regenerate the hashes or `verify_manifest()` will
correctly report the corpus as modified:

```bash
python3 tools/make_dataset.py --rehash   # re-hash whatever is on disk, generate nothing
```

Real data will contain defects this generator does not model: exchange
downtime gaps, sub-second duplicate sequence numbers, and price scales that
change after a listing. `market_data.load_ohlcv` returns its rejections
alongside its bars precisely so those show up as a count you can look at
instead of a silent hole in the series.

---

## Provenance

| | |
|---|---|
| Generator | `tools/make_dataset.py` v{manifest['generator_version']} |
| Seed | `{manifest['seed']}` |
| Bars per symbol | {BARS_PER_SYMBOL:,} hourly |
| First bar | `{manifest['first_timestamp']}` |
| Reproducible | yes — same seed gives byte-identical files (`--check`) |
| Real market data | **none** |
"""


# ---------------------------------------------------------------------------
# orchestration
# ---------------------------------------------------------------------------


def _manifest_entry(written: WrittenFile, **extra: object) -> Dict[str, object]:
    entry: Dict[str, object] = {
        "rows": written.rows,
        "sha256": written.sha256,
        "sha256_uncompressed": written.sha256_uncompressed,
        "size_bytes": written.size_bytes,
    }
    entry.update(extra)
    return entry


def generate(out_root: str, seed: int = DEFAULT_SEED,
             bars: int = BARS_PER_SYMBOL) -> Dict[str, object]:
    """Write the whole corpus and return the manifest that describes it."""
    files: Dict[str, Dict[str, object]] = {}
    first_timestamp = ""
    last_timestamp = ""

    for spec in SYMBOLS:
        symbol_seed = derive_seed(seed, spec.symbol_id)
        series = build_bars(spec, Rng(symbol_seed), bars)
        assert_regimes_are_what_they_claim(spec.symbol_id, series)
        first_timestamp = iso_utc(series[0].start_us)
        last_timestamp = iso_utc(series[-1].end_us)

        regime_summary = []
        for regime, first, last in regime_bounds():
            if first >= len(series):
                break
            last = min(last, len(series) - 1)
            regime_summary.append({
                "name": regime.name,
                "rows": last - first + 1,
                "first_timestamp": iso_utc(series[first].start_us),
                "last_timestamp": iso_utc(series[last].end_us),
            })

        written = write_csv_gz(
            out_root, f"ohlcv/{spec.file_stem}_1H.csv.gz",
            OHLCV_COLUMNS, ohlcv_rows(spec, series),
        )
        files[written.relpath] = _manifest_entry(
            written, kind="ohlcv", symbol_id=spec.symbol_id, seed=symbol_seed,
            interval_seconds=BAR_SECONDS,
            first_timestamp=iso_utc(series[0].start_us),
            last_timestamp=iso_utc(series[-1].end_us),
            regimes=regime_summary,
        )

        if spec.with_book:
            book_seed = derive_seed(seed, spec.symbol_id + "/orderbook")
            rows, stats = build_book_rows(spec, Rng(book_seed), series)
            written = write_csv_gz(
                out_root, f"orderbook/{spec.file_stem}_L2.csv.gz",
                BOOK_COLUMNS, rows,
            )
            files[written.relpath] = _manifest_entry(
                written, kind="orderbook", symbol_id=spec.symbol_id,
                seed=book_seed,
                first_timestamp=rows[0][1], last_timestamp=rows[-1][1],
                snapshot_levels=BOOK_LEVELS,
                match_events=stats["match_events"],
                snapshot_events=stats["snapshot_events"],
                regimes=[r["name"] for r in regime_summary],
            )

        if spec.with_quotes:
            quote_seed = derive_seed(seed, spec.symbol_id + "/quotes")
            rows = build_quote_rows(spec, Rng(quote_seed), series)
            written = write_csv_gz(
                out_root, f"quotes/{spec.file_stem}_QUOTES.csv.gz",
                QUOTE_COLUMNS, rows,
            )
            files[written.relpath] = _manifest_entry(
                written, kind="quotes", symbol_id=spec.symbol_id,
                seed=quote_seed,
                first_timestamp=rows[0][1], last_timestamp=rows[-1][1],
                regimes=[r["name"] for r in regime_summary],
            )

    reasons = {name: reason for name, _kind, reason, _prose in ANOMALY_DOCS}
    kinds = {name: kind for name, kind, _reason, _prose in ANOMALY_DOCS}
    for name, (header, rows) in sorted(build_anomalies().items()):
        written = write_csv_plain(out_root, f"anomalies/{name}", header, rows)
        files[written.relpath] = _manifest_entry(
            written, kind="anomaly", data_kind=kinds[name],
            expected_rejection=reasons[name], defective_rows=1,
        )

    manifest: Dict[str, object] = {
        "schema": "coinapi-flat-files/v1",
        "synthetic": True,
        "warning": SYNTHETIC_WARNING,
        "generator": "tools/make_dataset.py",
        "generator_version": GENERATOR_VERSION,
        "seed": seed,
        "bar_seconds": BAR_SECONDS,
        "bars_per_symbol": bars,
        "first_timestamp": first_timestamp,
        "last_timestamp": last_timestamp,
        "regimes": [
            {"name": r.name, "bars": r.bars, "drift_per_bar": r.mu,
             "vol_per_bar": r.sigma, "base_spread_bps": r.spread_bps,
             "depth_multiplier": r.depth, "bid_depth_skew": r.bid_skew}
            for r in REGIMES
        ],
        "files": files,
    }
    write_manifest(out_root, manifest)

    readme_path = os.path.join(out_root, "README.md")
    with open(readme_path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(render_readme(manifest))
    return manifest


def write_manifest(out_root: str, manifest: Dict[str, object]) -> None:
    """Serialise the manifest with sorted keys and no wall-clock field.

    Sorted so a regeneration produces a diff only when the *data* changed, and
    with no `generated_at`, because a field that changes every run trains
    reviewers to skip the diff entirely.
    """
    os.makedirs(out_root, exist_ok=True)
    path = os.path.join(out_root, "MANIFEST.json")
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(manifest, handle, indent=2, sort_keys=True)
        handle.write("\n")


def rehash(root: str) -> Dict[str, object]:
    """Re-hash whatever is on disk without regenerating it.

    This is the escape hatch for someone who has just dropped **real** CoinAPI
    files into place: the data is now real, the shipped hashes are now wrong,
    and re-hashing is correct. It deliberately does not touch `synthetic`,
    because only a human knows whether what they dropped in is a market.
    """
    path = os.path.join(root, "MANIFEST.json")
    with open(path, encoding="utf-8") as handle:
        manifest = json.load(handle)
    files = manifest["files"]
    for relpath, entry in files.items():
        full = os.path.join(root, relpath)
        with open(full, "rb") as blob:
            data = blob.read()
        entry["sha256"] = hashlib.sha256(data).hexdigest()
        entry["size_bytes"] = len(data)
        if relpath.endswith(".gz"):
            entry["sha256_uncompressed"] = hashlib.sha256(
                gzip.decompress(data)
            ).hexdigest()
        else:
            entry["sha256_uncompressed"] = entry["sha256"]
    write_manifest(root, manifest)
    return manifest


def check_reproducible(root: str, seed: int) -> int:
    """Regenerate into a temp dir and compare hashes with what is on disk."""
    reference_path = os.path.join(root, "MANIFEST.json")
    if not os.path.exists(reference_path):
        print(f"no manifest at {reference_path}; run without --check first",
              file=sys.stderr)
        return 2
    with open(reference_path, encoding="utf-8") as handle:
        reference = json.load(handle)

    scratch = tempfile.mkdtemp(prefix="make_dataset_check_")
    try:
        fresh = generate(scratch, seed=seed,
                         bars=int(reference.get("bars_per_symbol", BARS_PER_SYMBOL)))
        problems = []
        for relpath, entry in sorted(reference["files"].items()):
            other = fresh["files"].get(relpath)          # type: ignore[union-attr]
            if other is None:
                problems.append(f"{relpath}: not regenerated")
            elif other["sha256"] != entry["sha256"]:
                problems.append(
                    f"{relpath}: sha256 {entry['sha256'][:12]}… -> "
                    f"{other['sha256'][:12]}…"
                )
        for relpath in sorted(fresh["files"]):           # type: ignore[union-attr]
            if relpath not in reference["files"]:
                problems.append(f"{relpath}: new file not in the shipped manifest")
    finally:
        shutil.rmtree(scratch, ignore_errors=True)

    if problems:
        print("NOT reproducible:", file=sys.stderr)
        for problem in problems:
            print(f"  {problem}", file=sys.stderr)
        return 1
    print(f"reproducible: {len(reference['files'])} files, identical hashes")
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Generate the synthetic market-data corpus under data/.",
    )
    default_root = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data"
    )
    parser.add_argument("--out", default=default_root,
                        help="output directory (default: <repo>/data)")
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED,
                        help=f"master seed (default: {DEFAULT_SEED})")
    parser.add_argument("--bars", type=int, default=BARS_PER_SYMBOL,
                        help=f"hourly bars per symbol (default: {BARS_PER_SYMBOL})")
    parser.add_argument("--check", action="store_true",
                        help="regenerate to a temp dir and diff hashes; write nothing")
    parser.add_argument("--rehash", action="store_true",
                        help="re-hash the files already on disk (use after dropping "
                             "real data in); generate nothing")
    args = parser.parse_args(list(argv) if argv is not None else None)

    if args.check and args.rehash:
        parser.error("--check and --rehash are mutually exclusive")
    if args.check:
        return check_reproducible(args.out, args.seed)
    if args.rehash:
        rehash(args.out)
        print(f"re-hashed {args.out}/MANIFEST.json against the files on disk")
        return 0

    manifest = generate(args.out, seed=args.seed, bars=args.bars)
    total = sum(int(entry["size_bytes"])                 # type: ignore[index]
                for entry in manifest["files"].values())  # type: ignore[union-attr]
    print(f"wrote {len(manifest['files'])} files "       # type: ignore[arg-type]
          f"({total / 1_048_576:.2f} MiB) to {args.out}")
    print(f"seed={args.seed}  bars/symbol={args.bars}  "
          f"span={manifest['first_timestamp']} .. {manifest['last_timestamp']}")
    print("SYNTHETIC. Not a market. See data/README.md.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
