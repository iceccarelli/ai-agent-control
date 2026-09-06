#!/usr/bin/env python3
"""The first honest reading of real-series timing skill.

WHAT THIS IS
============
Fourteen slices built a ruler. Slice 23 proved it does not lie: on 200
structure-free surrogates the control reported median 48.0, uniformity
z = -1.18, KS p = 0.435 against U(0,100), 0 incomplete. That is a statement
about the instrument and nothing else — no real-series percentile has ever been
interpreted in this project.

This tool makes the measurement the ruler was built for, under the design
pre-declared in EDGE.md §5b:

    M1  the strategy's mean net R on the REAL series, ranked against 1500
        rotation replicates under one-trade-per-run at lock-up 1
    M2  a drift-controlled contrast on the same real bars: the analyser's flags
        against >= 200 shape-matched information-free schedules
    M3  the same four out-of-sample folds, reported per fold

WHY M2 IS MANDATORY AND M1 IS NOT SUFFICIENT
--------------------------------------------
The strategy is long-only on an asset that rose roughly 8x over the sample. A
rotation null moves *where* the flags sit while leaving that exposure intact, so
a high M1 percentile is consistent with two very different worlds: the analyser
times entries well, or the analyser is simply long at moments rotation does not
fully neutralise. M2 separates them. Side N has the **same multiset of run
lengths and gaps** as the real flag sequence — same number of trades, same
burst structure, same total exposure — and carries no information about the
bars at all. If the analyser beats that, the difference is timing.

THIS FILE COMPUTES NO ARITHMETIC OF ITS OWN
--------------------------------------------
Every quantity comes from ``skill_test``: ``barrier_r_for_all_bars`` for the R
of a trade opened at each bar, ``signal_flags`` for the analyser's decisions,
``simulate_schedule`` for the flags -> entries map, ``shape_matched_flags`` for
Side N, ``extract_blocks`` for the run/gap structure. A forked copy of the R
maths that drifted from the instrument by one line would invalidate the
comparison silently, so there is no copy. ``tests/test_skill_test.py`` asserts
by AST that this module defines no scoring function of its own.

Usage
-----
    python3 tools/edge_measurement.py --shape-schedules 200 --runs 1500
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from dataclasses import dataclass, asdict
from typing import Dict, List, Optional, Sequence, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np

import backtest as bt
import market_data as md
import skill_test as sk
from signals import btc_alt_spillover_v1 as spillover
from signals import donchian_breakout_v1 as donchian
from signals import post_shock_fade_v1 as fade
from signals import range_location_fade_v1 as rangefade
from signals import open_gap_fade_v1 as opengap
from signals import ts_momentum_v1 as tsmom
from signals import sign_flip_momentum_v1 as flipmom
from signals import compression_breakout_v1 as compbrk
from signals import funding_carry_fade_v1 as fundfade
from signals import funding_carry_fade_btc_v1 as fundbtc

# DATA-READ LEDGER (slice 78). Installed BEFORE the first load_corpus call so
# that every corpus this tool reads is recorded, and a future holdout can be
# certified untouched by tools/reserved_holdout.py. Reading is reading: a
# negative result steers the next hypothesis exactly as a positive one does,
# which is how slice 57 ended up "out of sample" on bytes slice 55 had read.
try:
    import reserved_holdout as _read_ledger
    _read_ledger.install()
except Exception as _ledger_exc:  # noqa: BLE001 - never blocks a measurement
    # NOT a silent pass: the reason is bound and the flag is legible. An
    # unrecorded read is a real loss (a future holdout cannot be certified),
    # but it must not take the measurement down with it.
    _read_ledger = None
    _READ_LEDGER_UNAVAILABLE = repr(_ledger_exc)



REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: Pre-declared in EDGE.md 5b, before any number existed. Not tunable.
M1_PERCENTILE_BAR = 95.0
M2_PERCENTILE_BAR = 95.0
M3_SOFT_GATE_FOLDS = 3


@dataclass(frozen=True)
class Scored:
    """One entry schedule, scored. ``mean_r`` is the quantity every bar uses."""

    n_trades: int
    mean_r: float


@dataclass(frozen=True)
class DirectedBook:
    """A two-sided signal's scoring material.

    ``direction_seq`` is the OBSERVED sequence of directions, in order. A null
    replicate re-places the entries but keeps this sequence, so trade count,
    run/gap shape and the long/short mix all survive and only the *timing*
    changes — which is the single thing the instrument is built to test.

    ``lookup`` and ``net_r`` are keyed by side. Both sides come from
    ``skill_test.barrier_r_for_all_bars``; nothing is recomputed here.
    """

    directions: Dict[int, str]
    direction_seq: List[str]
    lookup: Dict[str, Dict[int, int]]
    net_r: Dict[str, np.ndarray]


def tradable_flags(flags: np.ndarray, eligible: np.ndarray) -> np.ndarray:
    """S1 — the one flag array BOTH sides build their schedules from.

    ``eligible`` is false wherever ``barrier_r_for_all_bars`` could not resolve
    a full horizon, which on a next-open fill with horizon 5 is the last seven
    bars of the series. A flag there describes a trade that would still be open
    when the data ends.

    Before slice 40 the real path built its schedule from raw ``flags`` and the
    nulls from ``flags & eligible``: the same builder, one trade apart, before a
    single R was read. On the full window that was 125 real entries against 124
    null slots, the extra one being 2026-07-31 — flagged, unscorable, and
    silently dropped later by a lookup miss.

    Dropping it *here* rather than at scoring time is the difference between an
    entry that is "not scored as 0" and an entry that is not ranked at all. Both
    sides now start from the same array, so the schedules cannot differ in
    length for a reason that has nothing to do with timing skill.
    """
    return flags & eligible


def score_schedule(
    entries: Sequence[int], lookup: Dict[int, int], net_r: np.ndarray,
    *, book: Optional[DirectedBook] = None, directed_mode: str = "own",
    direction_offset: int = 0,
) -> Optional[Scored]:
    """Mean net R over the eligible entries of one schedule.

    Identical in form to what ``run_permutation`` does for both the observed
    side and every null replicate: filter to bars the barrier could score, take
    the mean of their net R. ``net_r`` is produced by
    ``skill_test.barrier_r_for_all_bars`` and is never recomputed here.

    With ``book`` supplied the schedule is two-sided, and ``directed_mode``
    decides where each entry's direction comes from:

    * ``"own"`` — the direction the signal actually assigned to that bar. This
      is the ONLY correct mode for the observed schedule and for any subset of
      it (the walk-forward folds).
    * ``"sequence"`` — recycle the observed entries' direction sequence by
      position. This is for null replicates, whose bars carry no direction of
      their own: it preserves the strategy's long/short mix and its order while
      changing only *when* the trades happen.

    ``direction_offset`` rotates that sequence (S3, slice 40). Until slice 40
    every replicate read from position 0, so all 1,500 rotations of a surrogate
    shared one long/short phase — a fixed alignment the observed side does not
    have and cannot be compared against. The offset is drawn per replicate from
    the RNG the null generator already holds; it is ignored in ``"own"`` mode,
    where each bar carries its own direction.

    Getting this wrong is not cosmetic. The first version of this function built
    the sequence from every *flagged* bar rather than from the bars actually
    entered, and since the one-trade-per-run schedule drops overlapping setups
    (93 flagged -> 72 entered on ETH), **37 of 72 observed trades were scored in
    the wrong direction**. The observed reading was meaningless and the control
    was measuring that meaninglessness. Hence the explicit mode.

    One function, all modes, on purpose — the comparison is only meaningful if
    the observed side and the null side are scored by the same code.
    """
    if book is None:
        picked = [lookup[i] for i in entries if i in lookup]
        if not picked:
            return None
        return Scored(n_trades=len(picked), mean_r=float(np.mean(net_r[picked])))

    values: List[float] = []
    for position, bar in enumerate(entries):
        if directed_mode == "own":
            side = book.directions.get(bar)
            if side is None:
                continue
        else:
            if not book.direction_seq:
                return None
            size = len(book.direction_seq)
            side = book.direction_seq[(direction_offset + position) % size]
        side_lookup = book.lookup[side]
        if bar in side_lookup:
            values.append(float(book.net_r[side][side_lookup[bar]]))
    if not values:
        return None
    return Scored(n_trades=len(values), mean_r=float(np.mean(values)))


def percentile_of(value: float, distribution: Sequence[float]) -> float:
    """Rank of ``value`` within ``distribution``, in the instrument's convention.

    ``(d < value).mean() * 100`` — strictly less-than, so ties count as *not*
    below. This is byte-for-byte the convention ``run_permutation`` uses, which
    is what makes an M1 or M2 percentile comparable with every control number
    already in EDGE.md.
    """
    array = np.asarray(distribution, dtype=float)
    if array.size == 0:
        return float("nan")
    return float((array < value).mean() * 100.0)


def bootstrap_ci(
    observed: float, distribution: Sequence[float], *,
    resamples: int, rng: np.random.Generator, alpha: float = 0.05,
) -> Tuple[float, float, float]:
    """Two-sided CI for ``observed - mean(distribution)``.

    The uncertainty being quantified is in the **null side**: Side S is a single
    realised schedule on a single price path, while Side N is a sample of
    information-free schedules whose mean is estimated. Resampling that sample
    with replacement gives the sampling distribution of its mean, and therefore
    of the contrast.

    Returns ``(delta, low, high)``.
    """
    array = np.asarray(distribution, dtype=float)
    delta = observed - float(array.mean())
    if array.size < 2:
        return delta, float("nan"), float("nan")
    draws = np.empty(resamples, dtype=float)
    for i in range(resamples):
        draws[i] = observed - float(rng.choice(array, size=array.size,
                                               replace=True).mean())
    low = float(np.percentile(draws, 100.0 * alpha / 2.0))
    high = float(np.percentile(draws, 100.0 * (1.0 - alpha / 2.0)))
    return delta, low, high


def rotation_replicates(
    flags: np.ndarray, eligible: np.ndarray, lookup: Dict[int, int],
    net_r: np.ndarray, *, warmup: int, lockup: int, runs: int,
    rng: np.random.Generator, minimum_trades: int = 20,
    book: Optional["DirectedBook"] = None,
) -> List[Scored]:
    """The validated null: circularly rotate the flag sequence, rescore.

    This mirrors ``run_permutation``'s rotation branch exactly — the same
    ``np.roll`` over the same post-warm-up region of ``flags & eligible``, the
    same ``simulate_schedule``, the same minimum-trade filter. Rotation
    preserves every run length and every gap, so clustering and trade counts
    survive; only the phase changes.
    """
    tradable = tradable_flags(flags, eligible)
    # S1/S3 (slice 40): rotate WITHIN the bars that could host a scorable
    # entry, not across the whole post-warm-up span.
    #
    # `np.roll` wraps. Rolling the raw post-warm-up region moves flags into the
    # embargoed tail — bars whose horizon runs off the end — and every one that
    # lands there was silently dropped later by a lookup miss. That cost the
    # replicate trades AND shifted the direction sequence's phase by however
    # many it lost, which is exactly the S1/S2 defect again, on the null side
    # only. The observed schedule is never rotated, so it never paid it.
    #
    # Rotating in eligible-index space keeps every flag on a scorable bar and
    # preserves the flag count exactly. Where the eligible span is contiguous —
    # as it is here, bars 200..1453 — this is identical to rotating the block,
    # so run and gap lengths survive unchanged, which is the property the
    # rotation null exists for.
    positions = np.nonzero(eligible[warmup:])[0]
    values = tradable[warmup:][positions]
    out: List[Scored] = []
    seq_size = len(book.direction_seq) if book is not None else 0
    for _ in range(runs):
        candidate = np.zeros_like(flags)
        if positions.size:
            rolled = np.roll(values, int(rng.integers(1, max(2, values.size))))
            tail = np.zeros(flags.size - warmup, dtype=bool)
            tail[positions] = rolled
            candidate[warmup:] = tail
        entries = sk.simulate_schedule(candidate, warmup=warmup, lockup=lockup)
        offset = int(rng.integers(0, seq_size)) if seq_size else 0
        scored = score_schedule(entries, lookup, net_r, book=book,
                                directed_mode="sequence",
                                direction_offset=offset)
        if scored is not None and scored.n_trades >= minimum_trades:
            out.append(scored)
    return out


def shape_matched_replicates(
    flags: np.ndarray, eligible: np.ndarray, lookup: Dict[int, int],
    net_r: np.ndarray, *, warmup: int, lockup: int, schedules: int,
    rng: np.random.Generator, minimum_trades: int = 20,
    book: Optional["DirectedBook"] = None,
) -> List[Scored]:
    """Side N: same run/gap multisets, no knowledge of the bars.

    Built by ``skill_test.shape_matched_flags``, whose whole signature is
    ``(run_lengths, gaps, n_bars, warmup, rng)``. It cannot see a price, so its
    independence from the path is structural rather than argued.
    """
    # S1/S3 (slice 40): the shape is extracted and re-tiled in ELIGIBLE-index
    # space, for the same reason the rotation null is rotated there. Tiling
    # across the whole post-warm-up span lets a synthetic run land in the
    # embargoed tail, where its entries have no R and are dropped — costing the
    # replicate trades and shifting the direction phase, on the null side only.
    positions = np.nonzero(eligible[warmup:])[0]
    compact = tradable_flags(flags, eligible)[warmup:][positions]
    runs_observed, gaps_observed = sk.extract_blocks(compact, warmup=0)
    lengths = np.array([length for _, length in runs_observed], dtype=np.int64)
    gaps = np.array(gaps_observed, dtype=np.int64)
    out: List[Scored] = []
    seq_size = len(book.direction_seq) if book is not None else 0
    for _ in range(schedules):
        drawn = sk.shape_matched_flags(
            lengths, gaps, compact.size, warmup=0, rng=rng)
        synthetic = np.zeros_like(flags)
        tail = np.zeros(flags.size - warmup, dtype=bool)
        tail[positions] = drawn
        synthetic[warmup:] = tail
        entries = sk.simulate_schedule(synthetic, warmup=warmup, lockup=lockup)
        offset = int(rng.integers(0, seq_size)) if seq_size else 0
        scored = score_schedule(entries, lookup, net_r, book=book,
                                directed_mode="sequence",
                                direction_offset=offset)
        if scored is not None and scored.n_trades >= minimum_trades:
            out.append(scored)
    return out


def fold_bounds(n_bars: int, folds: int, warmup: int) -> List[Tuple[int, int]]:
    """The project's existing walk-forward split: equal contiguous OOS blocks."""
    start = warmup
    span = (n_bars - start) // folds
    return [(start + i * span,
             (start + (i + 1) * span) if i < folds - 1 else n_bars)
            for i in range(folds)]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", default=os.path.join(REPO, "data", "real_1d"))
    parser.add_argument("--interval", default="D")
    parser.add_argument("--symbol", default=None)
    parser.add_argument("--runs", type=int, default=1500)
    parser.add_argument("--shape-schedules", type=int, default=200)
    parser.add_argument("--fold-runs", type=int, default=500)
    parser.add_argument("--folds", type=int, default=4)
    parser.add_argument("--seed", type=int, default=20250730)
    parser.add_argument("--lockup", type=int, default=1)
    parser.add_argument("--horizon", type=int, default=24)
    parser.add_argument("--atr-period", type=int, default=14)
    parser.add_argument("--round-trip-bps", type=float, default=25.0)
    parser.add_argument("--take-profit-atr", type=float, default=4.0)
    parser.add_argument("--stop-atr", type=float, default=2.0)
    parser.add_argument("--min-confidence", type=float, default=0.12)
    parser.add_argument("--min-agreement", type=float, default=0.40)
    # WHICH SIGNAL supplies the observed flags. The default is the CLOSED
    # analyser so that every command already recorded in EDGE.md reproduces
    # exactly what it reproduced before -- slice 28 added a selector, not a
    # new default. Whatever is selected supplies FLAGS ONLY; the barrier, the
    # schedule and every R still come from skill_test.
    parser.add_argument("--btc-data", default=os.path.join(REPO, "data", "real_1d"),
                        help="BTC driver corpus for btc_alt_spillover_v1")
    parser.add_argument(
        "--control-attestation", default="",
        help=("path to a control log that ends CONTROL: **VALID** for the "
              "construction this run uses. Without it the summary records "
              "control_validated=false and no cleared-edge claim can be "
              "registered from it, however large the percentiles are."))
    parser.add_argument("--signal", default="closed_analyser",
                        choices=("closed_analyser", "donchian_breakout_v1",
                                 "btc_alt_spillover_v1",
                                 "post_shock_fade_v1",
                                 "range_location_fade_v1",
                                 "open_gap_fade_v1",
                                 "ts_momentum_v1",
                                 "sign_flip_momentum_v1",
                                 "compression_breakout_v1",
                                 "funding_carry_fade_v1",
                                 "funding_carry_fade_btc_v1"))
    parser.add_argument("--funding-data", default=None,
                        help=("funding CSV for funding_carry_fade_v1. Required "
                              "for that signal and ignored by every other: the "
                              "trigger lives in this file, not in the bars."))
    parser.add_argument("--oos-folds", default=None,
                        help=("path to a LOCKED fold calendar. When given, "
                              "every decision bar before its t_mid is "
                              "withheld, so only the late window can produce "
                              "a trade. Applies to funding_carry_fade_btc_v1 "
                              "only. The calendar is READ, never computed: "
                              "the tool refuses one whose sha256 is not in "
                              "the lock log, so a cut edited after a count "
                              "existed cannot be used to score."))
    parser.add_argument("--unverified-manifest", action="store_true",
                        help=("load the bar corpus with verify=False. Only for "
                              "corpora whose MANIFEST carries no per-file "
                              "checksum block; slice 55's linear corpus is the "
                              "only one, and its integrity is pinned by "
                              "artifacts/slice55_data_eligibility.json instead. "
                              "verify_manifest itself is never weakened."))
    parser.add_argument("--out-prefix", default=os.path.join(REPO, "artifacts",
                                                             "slice24_edge"))
    args = parser.parse_args(argv)

    loaded, _books, notes = md.load_corpus(
        args.data_dir, verify=not args.unverified_manifest)
    symbol = args.symbol or sorted(loaded)[0]
    bars = [bt.Bar(b.start_ms, b.open, b.high, b.low, b.close, b.volume)
            for b in loaded[symbol]]

    print("=" * 78)
    print("REAL-SERIES EDGE MEASUREMENT (slice 24)")
    print("=" * 78)
    for note in notes:
        print(note)
    print(f"symbol            : {symbol}")
    print(f"bars              : {len(bars):,}")
    print()
    print("Instrument path = the configuration whose control PASSED at n=200")
    print("(EDGE.md 4u: median 48.0, z = -1.18, KS p = 0.435, 0 incomplete):")
    print(f"  null rotation | lock-up {args.lockup} | one trade per run | "
          f"{args.runs:,} replicates")
    print(f"  barrier +{args.take_profit_atr:g}/-{args.stop_atr:g} ATR, "
          f"{args.horizon}-bar horizon, {args.round_trip_bps:g} bps round trip")
    print()
    print("Bars pre-declared in EDGE.md 5b, before any number existed:")
    print(f"  M1  real-series percentile          >= {M1_PERCENTILE_BAR}")
    print(f"  M2  Delta > 0, 95% CI excludes 0, percentile >= {M2_PERCENTILE_BAR}")
    print(f"  M3  >= {M3_SOFT_GATE_FOLDS} of {args.folds} folds mean net R > 0 "
          "(soft, supporting only)")
    print()

    cfg = bt.BacktestConfig(
        starting_cash=10_000.0, warmup_bars=200, interval=args.interval,
        min_confidence=args.min_confidence,
        min_component_agreement=args.min_agreement,
    )
    warmup = cfg.warmup_bars

    indices, net_r, _used = sk.barrier_r_for_all_bars(
        bars, take_profit_atr=args.take_profit_atr, stop_atr=args.stop_atr,
        horizon=args.horizon, atr_period=args.atr_period,
        round_trip_bps=args.round_trip_bps,
    )
    lookup = {int(idx): pos for pos, idx in enumerate(indices)}
    eligible = np.zeros(len(bars), dtype=bool)
    eligible[indices] = True
    eligible[:warmup] = False

    book = None
    pending_book = None
    if args.signal == "btc_alt_spillover_v1":
        # Two-sided and cross-asset: the trigger is BTC's, the geometry is the
        # alt's, and the fill is at the alt's NEXT open. Both sides come from
        # the one shared barrier implementation.
        btc_loaded, _bb, _bn = md.load_corpus(args.btc_data)
        btc_symbol = sorted(btc_loaded)[0]
        btc_bars = [bt.Bar(b.start_ms, b.open, b.high, b.low, b.close, b.volume)
                    for b in btc_loaded[btc_symbol]]
        shared, first_date, last_date = spillover.aligned_date_span(bars, btc_bars)
        print(f"signal            : {spillover.NAME}  "
              f"(BTC shock >= {spillover.SHOCK_MULTIPLIER:g} x ATR%, "
              f"two-sided, fill at alt next open)")
        _used = spillover.usable_btc_bars(btc_bars)
        _dropped = len(btc_bars) - len(_used)
        print(f"BTC driver        : {btc_symbol} from {args.btc_data} "
              f"({len(_used):,} bars used"
              + (f", {_dropped} excluded: "
                 f"{', '.join(spillover.EXCLUDED_BTC_UTC_DATES)}"
                 if _dropped else "") + ")")
        print(f"aligned UTC dates : {shared:,}  {first_date} -> {last_date}")
        print("computing BTC shock setups and aligning to the alt calendar ...")
        flags, directions = spillover.flags_and_directions(
            bars, btc_bars, warmup=warmup)

        side_lookup: Dict[str, Dict[int, int]] = {}
        side_net: Dict[str, np.ndarray] = {}
        for tag, side in ((spillover.LONG_SETUP, "long"),
                          (spillover.SHORT_SETUP, "short")):
            idx, net, _u = sk.barrier_r_for_all_bars(
                bars, take_profit_atr=args.take_profit_atr,
                stop_atr=args.stop_atr, horizon=args.horizon,
                atr_period=args.atr_period,
                round_trip_bps=args.round_trip_bps,
                side=side, entry_on=spillover.ENTRY_ON,
            )
            side_lookup[tag] = {int(i): p for p, i in enumerate(idx)}
            side_net[tag] = net
        # Eligibility is side-independent (it depends only on a finite ATR and
        # on there being enough bars left), but intersect rather than assume.
        eligible = np.zeros(len(bars), dtype=bool)
        common = set(side_lookup[spillover.LONG_SETUP]) & \
            set(side_lookup[spillover.SHORT_SETUP])
        eligible[sorted(common)] = True
        eligible[:warmup] = False

        pending_book = (directions, side_lookup, side_net)
        scoreable = [i for i in sorted(directions) if eligible[i] and flags[i]]
        longs = sum(1 for i in scoreable
                    if directions[i] == spillover.LONG_SETUP)
        print(f"setups scoreable  : {len(scoreable)}  "
              f"({longs} long / {len(scoreable) - longs} short)")
    elif args.signal == "post_shock_fade_v1":
        # Two-sided and same-asset: the trigger is this symbol's own return
        # scaled by its own ATR, the direction is the OPPOSITE of the move, and
        # the fill is at this symbol's next open. Both sides come from the one
        # shared barrier implementation -- nothing is recomputed here.
        counts = fade.summary(bars, warmup=warmup)
        print(f"signal            : {fade.NAME}  "
              f"(own shock >= {fade.SHOCK_K:g} x ATR%, FADED, "
              f"two-sided, fill at next open)")
        print(f"setups            : {counts['setups']}  "
              f"({counts['long_setups']} long / {counts['short_setups']} short)")
        flags, directions = fade.flags_and_directions(bars, warmup=warmup)

        side_lookup = {}
        side_net = {}
        for tag, side in ((fade.LONG_SETUP, "long"),
                          (fade.SHORT_SETUP, "short")):
            idx, net, _u = sk.barrier_r_for_all_bars(
                bars, take_profit_atr=args.take_profit_atr,
                stop_atr=args.stop_atr, horizon=args.horizon,
                atr_period=args.atr_period,
                round_trip_bps=args.round_trip_bps,
                side=side, entry_on=fade.ENTRY_ON,
            )
            side_lookup[tag] = {int(i): p for p, i in enumerate(idx)}
            side_net[tag] = net
        eligible = np.zeros(len(bars), dtype=bool)
        common = set(side_lookup[fade.LONG_SETUP]) & \
            set(side_lookup[fade.SHORT_SETUP])
        eligible[sorted(common)] = True
        eligible[:warmup] = False

        pending_book = (directions, side_lookup, side_net)
        scoreable = [i for i in sorted(directions) if eligible[i] and flags[i]]
        longs = sum(1 for i in scoreable if directions[i] == fade.LONG_SETUP)
        print(f"setups scoreable  : {len(scoreable)}  "
              f"({longs} long / {len(scoreable) - longs} short)")
    elif args.signal == "range_location_fade_v1":
        # Two-sided and same-asset: the trigger is where this symbol's close
        # sits inside its own trailing 20-bar range, the direction is the
        # OPPOSITE of that location, and the fill is at this symbol's next
        # open. Both sides come from the one shared barrier implementation.
        counts = rangefade.summary(bars, warmup=warmup)
        print(f"signal            : {rangefade.NAME}  "
              f"(loc >= {rangefade.UPPER:g} -> SHORT, "
              f"loc <= {rangefade.LOWER:g} -> LONG; "
              f"range {rangefade.RANGE_BARS} bars, t-{rangefade.RANGE_BARS}..t-1, "
              f"fill at next open)")
        print(f"setups            : {counts['setups']}  "
              f"({counts['long_setups']} long / {counts['short_setups']} short)")
        flags, directions = rangefade.flags_and_directions(bars, warmup=warmup)

        side_lookup = {}
        side_net = {}
        for tag, side in ((rangefade.LONG_SETUP, "long"),
                          (rangefade.SHORT_SETUP, "short")):
            idx, net, _u = sk.barrier_r_for_all_bars(
                bars, take_profit_atr=args.take_profit_atr,
                stop_atr=args.stop_atr, horizon=args.horizon,
                atr_period=args.atr_period,
                round_trip_bps=args.round_trip_bps,
                side=side, entry_on=rangefade.ENTRY_ON,
            )
            side_lookup[tag] = {int(i): p for p, i in enumerate(idx)}
            side_net[tag] = net
        eligible = np.zeros(len(bars), dtype=bool)
        common = set(side_lookup[rangefade.LONG_SETUP]) & \
            set(side_lookup[rangefade.SHORT_SETUP])
        eligible[sorted(common)] = True
        eligible[:warmup] = False

        pending_book = (directions, side_lookup, side_net)
        scoreable = [i for i in sorted(directions) if eligible[i] and flags[i]]
        longs = sum(1 for i in scoreable
                    if directions[i] == rangefade.LONG_SETUP)
        print(f"setups scoreable  : {len(scoreable)}  "
              f"({longs} long / {len(scoreable) - longs} short)")
    elif args.signal == "open_gap_fade_v1":
        # Two-sided and same-asset: the trigger is the OVERNIGHT move, this
        # symbol's open against its own prior close, scaled by the ATR of the
        # bars before it. The direction is the OPPOSITE of the gap.
        #
        # The index convention (EDGE.md 30c) matters here and nowhere else:
        # the signal is emitted at `t-1` so that the shared barrier's
        # "next open" IS the open of the gap bar, which is where the intake
        # fills. Nothing about the scoring changes; only which index carries
        # the flag.
        counts = opengap.summary(bars, warmup=warmup)
        print(f"signal            : {opengap.NAME}  "
              f"(|open[t] - close[t-1]| / ATR_prev >= {opengap.GAP_K:g}, "
              f"FADED, two-sided, fill at the gap bar's own open)")
        print(f"setups            : {counts['setups']}  "
              f"({counts['long_setups']} long / {counts['short_setups']} short)")
        flags, directions = opengap.flags_and_directions(bars, warmup=warmup)

        side_lookup = {}
        side_net = {}
        for tag, side in ((opengap.LONG_SETUP, "long"),
                          (opengap.SHORT_SETUP, "short")):
            idx, net, _u = sk.barrier_r_for_all_bars(
                bars, take_profit_atr=args.take_profit_atr,
                stop_atr=args.stop_atr, horizon=args.horizon,
                atr_period=args.atr_period,
                round_trip_bps=args.round_trip_bps,
                side=side, entry_on=opengap.ENTRY_ON,
            )
            side_lookup[tag] = {int(i): p for p, i in enumerate(idx)}
            side_net[tag] = net
        eligible = np.zeros(len(bars), dtype=bool)
        common = set(side_lookup[opengap.LONG_SETUP]) & \
            set(side_lookup[opengap.SHORT_SETUP])
        eligible[sorted(common)] = True
        eligible[:warmup] = False

        pending_book = (directions, side_lookup, side_net)
        scoreable = [i for i in sorted(directions) if eligible[i] and flags[i]]
        longs = sum(1 for i in scoreable if directions[i] == opengap.LONG_SETUP)
        print(f"setups scoreable  : {len(scoreable)}  "
              f"({longs} long / {len(scoreable) - longs} short)")
    elif args.signal == "ts_momentum_v1":
        # Two-sided and same-asset, and the first CONTINUATION family measured
        # here since donchian_breakout_v1: the trigger is the SIGN of this
        # symbol's own 10-bar close-to-close return skipping the most recent
        # bar, the direction FOLLOWS that sign, and the fill is the next open.
        #
        # Unlike every prior family this is a STATE rather than an EVENT: it is
        # true on nearly every bar past warmup. `simulate_schedule` yields at
        # most one entry per contiguous run of flags, so the schedule this
        # produces is governed by where the sign FLIPS, not by how many bars
        # are flagged. See EDGE.md §31d.
        counts = tsmom.summary(bars, warmup=warmup)
        print(f"signal            : {tsmom.NAME}  "
              f"(sign of close[t-{tsmom.SKIP}] / "
              f"close[t-{tsmom.SKIP + tsmom.LOOKBACK}] - 1, CONTINUATION, "
              f"two-sided, fill at next open)")
        print(f"setups            : {counts['setups']}  "
              f"({counts['long_setups']} long / {counts['short_setups']} short)")
        flags, directions = tsmom.flags_and_directions(bars, warmup=warmup)

        side_lookup = {}
        side_net = {}
        for tag, side in ((tsmom.LONG_SETUP, "long"),
                          (tsmom.SHORT_SETUP, "short")):
            idx, net, _u = sk.barrier_r_for_all_bars(
                bars, take_profit_atr=args.take_profit_atr,
                stop_atr=args.stop_atr, horizon=args.horizon,
                atr_period=args.atr_period,
                round_trip_bps=args.round_trip_bps,
                side=side, entry_on=tsmom.ENTRY_ON,
            )
            side_lookup[tag] = {int(i): p for p, i in enumerate(idx)}
            side_net[tag] = net
        eligible = np.zeros(len(bars), dtype=bool)
        common = set(side_lookup[tsmom.LONG_SETUP]) & \
            set(side_lookup[tsmom.SHORT_SETUP])
        eligible[sorted(common)] = True
        eligible[:warmup] = False

        pending_book = (directions, side_lookup, side_net)
        scoreable = [i for i in sorted(directions) if eligible[i] and flags[i]]
        longs = sum(1 for i in scoreable if directions[i] == tsmom.LONG_SETUP)
        print(f"setups scoreable  : {len(scoreable)}  "
              f"({longs} long / {len(scoreable) - longs} short)")
    elif args.signal == "sign_flip_momentum_v1":
        # Two-sided, same-asset, and EVENT-shaped: the trigger is a CHANGE in
        # the sign of this symbol's own 10-bar close-to-close return, the
        # direction follows the new sign, and the fill is the next open.
        #
        # Same state variable and window as the frozen ts_momentum_v1; the
        # difference is that only the bars where the sign CHANGES are flagged,
        # so the flags are sparse and form many short runs instead of one long
        # one. See EDGE.md 33a.
        counts = flipmom.summary(bars, warmup=warmup)
        print(f"signal            : {flipmom.NAME}  "
              f"(sign CHANGE of close[t-{flipmom.SKIP}] / "
              f"close[t-{flipmom.SKIP + flipmom.LOOKBACK}] - 1, CONTINUATION "
              f"of the new sign, two-sided, fill at next open)")
        print(f"setups            : {counts['setups']}  "
              f"({counts['long_setups']} long / {counts['short_setups']} short)")
        flags, directions = flipmom.flags_and_directions(bars, warmup=warmup)

        side_lookup = {}
        side_net = {}
        for tag, side in ((flipmom.LONG_SETUP, "long"),
                          (flipmom.SHORT_SETUP, "short")):
            idx, net, _u = sk.barrier_r_for_all_bars(
                bars, take_profit_atr=args.take_profit_atr,
                stop_atr=args.stop_atr, horizon=args.horizon,
                atr_period=args.atr_period,
                round_trip_bps=args.round_trip_bps,
                side=side, entry_on=flipmom.ENTRY_ON,
            )
            side_lookup[tag] = {int(i): p for p, i in enumerate(idx)}
            side_net[tag] = net
        eligible = np.zeros(len(bars), dtype=bool)
        common = set(side_lookup[flipmom.LONG_SETUP]) & \
            set(side_lookup[flipmom.SHORT_SETUP])
        eligible[sorted(common)] = True
        eligible[:warmup] = False

        pending_book = (directions, side_lookup, side_net)
        scoreable = [i for i in sorted(directions) if eligible[i] and flags[i]]
        longs = sum(1 for i in scoreable
                    if directions[i] == flipmom.LONG_SETUP)
        print(f"setups scoreable  : {len(scoreable)}  "
              f"({longs} long / {len(scoreable) - longs} short)")
    elif args.signal == "compression_breakout_v1":
        # Two-sided, same-asset, EVENT-shaped, and the first CONJUNCTION
        # trigger measured here: a 20-bar channel break that happens while the
        # bar's own ATR sits at or below the 25th percentile of its trailing
        # 50-bar ATR window. Direction follows the break; fill at the next open.
        #
        # The break half alone is a short-window Donchian. The compression
        # precondition is the entire novelty, which is why the count log
        # reports how many otherwise-qualifying breaks it removes.
        counts = compbrk.summary(bars, warmup=warmup)
        print(f"signal            : {compbrk.NAME}  "
              f"(ATR pctile <= {compbrk.COMP_MAX:g} over {compbrk.PCTILE_WINDOW} "
              f"bars AND close beyond the {compbrk.BREAK_N}-bar channel, "
              f"CONTINUATION, two-sided, fill at next open)")
        print(f"setups            : {counts['setups']}  "
              f"({counts['long_setups']} long / {counts['short_setups']} short)"
              f"   of {counts['breaks_any_volatility']} breaks at any volatility")
        flags, directions = compbrk.flags_and_directions(bars, warmup=warmup)

        side_lookup = {}
        side_net = {}
        for tag, side in ((compbrk.LONG_SETUP, "long"),
                          (compbrk.SHORT_SETUP, "short")):
            idx, net, _u = sk.barrier_r_for_all_bars(
                bars, take_profit_atr=args.take_profit_atr,
                stop_atr=args.stop_atr, horizon=args.horizon,
                atr_period=args.atr_period,
                round_trip_bps=args.round_trip_bps,
                side=side, entry_on=compbrk.ENTRY_ON,
            )
            side_lookup[tag] = {int(i): p for p, i in enumerate(idx)}
            side_net[tag] = net
        eligible = np.zeros(len(bars), dtype=bool)
        common = set(side_lookup[compbrk.LONG_SETUP]) & \
            set(side_lookup[compbrk.SHORT_SETUP])
        eligible[sorted(common)] = True
        eligible[:warmup] = False

        pending_book = (directions, side_lookup, side_net)
        scoreable = [i for i in sorted(directions) if eligible[i] and flags[i]]
        longs = sum(1 for i in scoreable
                    if directions[i] == compbrk.LONG_SETUP)
        print(f"setups scoreable  : {len(scoreable)}  "
              f"({longs} long / {len(scoreable) - longs} short)")
    elif args.signal == "funding_carry_fade_v1":
        # The first family whose TRIGGER LIVES IN A DIFFERENT SERIES from the
        # bars. Funding is read from its own corpus and joined to the daily
        # decision clock by "last print at or before this bar's close"; the
        # bars supply only the barrier.
        #
        # Funding is also a COST. This rule shorts exactly when funding is rich
        # and positive, and a short RECEIVES positive funding, so a measurement
        # that ignored it would let the fade collect the carry for free. The
        # adjustment is folded into the per-side net_r arrays BEFORE anything
        # is scored, so the observed schedule and every null replicate are
        # charged identically. See EDGE.md 36d.
        if not args.funding_data:
            print("funding_carry_fade_v1 needs --funding-data; refusing to "
                  "measure a funding signal without funding.")
            return 2
        funding = fundfade.load_funding(args.funding_data)
        counts = fundfade.summary(bars, funding, warmup=warmup)
        print(f"signal            : {fundfade.NAME}  "
              f"(|f| >= {fundfade.FUND_ABS:g} at the last print on or before "
              f"the bar close; rich long carry -> SHORT, rich short carry -> "
              f"LONG; fill at next open)")
        print(f"funding prints    : {counts['funding_prints']:,}  "
              f"from {os.path.basename(args.funding_data)}")
        print(f"bars with funding : {counts['bars_with_funding']:,} of "
              f"{counts['bars']:,}  (no forward-fill)")
        print(f"setups            : {counts['setups']}  "
              f"({counts['long_setups']} long / {counts['short_setups']} short)")
        flags, directions = fundfade.flags_and_directions(bars, funding,
                                                          warmup=warmup)

        side_lookup = {}
        side_net = {}
        funding_shift = {}
        for tag, side in ((fundfade.LONG_SETUP, "long"),
                          (fundfade.SHORT_SETUP, "short")):
            idx, net, used = sk.barrier_r_for_all_bars(
                bars, take_profit_atr=args.take_profit_atr,
                stop_atr=args.stop_atr, horizon=args.horizon,
                atr_period=args.atr_period,
                round_trip_bps=args.round_trip_bps,
                side=side, entry_on=fundfade.ENTRY_ON,
            )
            adjusted = fundfade.funding_adjusted_net_r(
                bars, funding, idx, net, used, tag,
                stop_atr=args.stop_atr, atr_period=args.atr_period)
            funding_shift[tag] = float(np.mean(adjusted - net)) if net.size \
                else 0.0
            side_lookup[tag] = {int(i): p for p, i in enumerate(idx)}
            side_net[tag] = adjusted
        print(f"funding adjustment: mean net R shifts "
              f"{funding_shift[fundfade.LONG_SETUP]:+.4f} on longs, "
              f"{funding_shift[fundfade.SHORT_SETUP]:+.4f} on shorts "
              f"(a short RECEIVES positive funding; applied to the observed "
              f"schedule AND every replicate)")
        eligible = np.zeros(len(bars), dtype=bool)
        common = set(side_lookup[fundfade.LONG_SETUP]) & \
            set(side_lookup[fundfade.SHORT_SETUP])
        eligible[sorted(common)] = True
        eligible[:warmup] = False

        pending_book = (directions, side_lookup, side_net)
        scoreable = [i for i in sorted(directions) if eligible[i] and flags[i]]
        longs = sum(1 for i in scoreable
                    if directions[i] == fundfade.LONG_SETUP)
        print(f"setups scoreable  : {len(scoreable)}  "
              f"({longs} long / {len(scoreable) - longs} short)")
    elif args.signal == "funding_carry_fade_btc_v1":
        # The BTC-only product (slice 57). Same rule, same constants, same
        # join, same costs as the frozen multi-symbol family — the ONLY
        # differences are the one-symbol universe and, when --oos-folds is
        # given, that decision bars before the locked cut are withheld.
        fundbtc.require_supported_symbol(symbol)
        if not args.funding_data:
            print("funding_carry_fade_btc_v1 needs --funding-data; refusing "
                  "to measure a funding signal without funding.")
            return 2
        folds = None
        if args.oos_folds:
            folds = fundbtc.load_folds(args.oos_folds)
            if not fundbtc.folds_are_unmodified(args.oos_folds):
                print("REFUSING: the fold calendar's sha256 is not the one in "
                      "the lock log. A cut that changed after it was locked "
                      "cannot produce a registering number.")
                return 2
        funding = fundbtc.load_funding(args.funding_data)
        counts = fundbtc.summary(bars, funding, warmup=warmup)
        print(f"signal            : {fundbtc.NAME}  "
              f"(BTCUSDT only; |f| >= {fundbtc.FUND_ABS:g} at the last print "
              f"on or before the bar close; rich long carry -> SHORT; fill at "
              f"next open)")
        print(f"derived from      : {fundbtc.DERIVED_FROM}  "
              f"(FROZEN ABSENT — identical constants, different universe and "
              f"a different clear gate; see EDGE.md 39)")
        print(f"funding prints    : {counts['funding_prints']:,}  "
              f"from {os.path.basename(args.funding_data)}")
        print(f"bars with funding : {counts['bars_with_funding']:,} of "
              f"{counts['bars']:,}  (no forward-fill)")
        if folds is None:
            print("window            : FULL SAMPLE — DIAGNOSTIC ONLY, "
                  "registration_eligible = false")
        else:
            print(f"window            : OOS LATE ONLY  "
                  f"[{folds['t_mid']} .. {folds['t1']}]  "
                  f"{folds['n_bars_late']:,} of {folds['n_bars_total']:,} bars")
            print(f"folds sha256      : "
                  f"{fundbtc.folds_sha256(args.oos_folds)}  (matches the lock "
                  f"log)")
        flags, directions = fundbtc.flags_and_directions(bars, funding,
                                                         warmup=warmup)
        print(f"setups (all bars) : {counts['setups']}  "
              f"({counts['long_setups']} long / {counts['short_setups']} short)")
        if folds is not None:
            before = int(flags.sum())
            flags = fundbtc.restrict_flags_to_late(flags, bars, folds)
            print(f"flags after cut   : {int(flags.sum())} of {before}  "
                  f"(decision withheld before t_mid; the ATR, the barrier and "
                  f"the funding join still read the full history, so a trade "
                  f"on the first late bar has the geometry it always had)")

        side_lookup = {}
        side_net = {}
        funding_shift = {}
        for tag, side in ((fundbtc.LONG_SETUP, "long"),
                          (fundbtc.SHORT_SETUP, "short")):
            idx, net, used = sk.barrier_r_for_all_bars(
                bars, take_profit_atr=args.take_profit_atr,
                stop_atr=args.stop_atr, horizon=args.horizon,
                atr_period=args.atr_period,
                round_trip_bps=args.round_trip_bps,
                side=side, entry_on=fundbtc.ENTRY_ON,
            )
            adjusted = fundbtc.funding_adjusted_net_r(
                bars, funding, idx, net, used, tag,
                stop_atr=args.stop_atr, atr_period=args.atr_period)
            funding_shift[tag] = float(np.mean(adjusted - net)) if net.size \
                else 0.0
            side_lookup[tag] = {int(i): p for p, i in enumerate(idx)}
            side_net[tag] = adjusted
        print(f"funding adjustment: mean net R shifts "
              f"{funding_shift[fundbtc.LONG_SETUP]:+.4f} on longs, "
              f"{funding_shift[fundbtc.SHORT_SETUP]:+.4f} on shorts "
              f"(a short RECEIVES positive funding; applied to the observed "
              f"schedule AND every replicate)")
        eligible = np.zeros(len(bars), dtype=bool)
        common = set(side_lookup[fundbtc.LONG_SETUP]) & \
            set(side_lookup[fundbtc.SHORT_SETUP])
        eligible[sorted(common)] = True
        eligible[:warmup] = False
        if folds is not None:
            # The null must rotate WITHIN the same window the observed
            # schedule was drawn from. Leaving `eligible` spanning the full
            # history would let replicates land on early bars the real rule
            # was forbidden to trade, which is not a null for this measurement
            # — it is a comparison between two different strategies.
            late = np.zeros(len(bars), dtype=bool)
            late[list(fundbtc.late_window_indices(bars, folds))] = True
            eligible = eligible & late
            print(f"eligible bars     : {int(eligible.sum())}  "
                  f"(rotation is confined to the OOS window, so replicates "
                  f"cannot borrow dates the real rule was not allowed)")

        pending_book = (directions, side_lookup, side_net)
        scoreable = [i for i in sorted(directions) if eligible[i] and flags[i]]
        longs = sum(1 for i in scoreable
                    if directions[i] == fundbtc.LONG_SETUP)
        print(f"setups scoreable  : {len(scoreable)}  "
              f"({longs} long / {len(scoreable) - longs} short)")
    elif args.signal == "donchian_breakout_v1":
        print(f"signal            : {donchian.NAME}  "
              f"(Donchian N={donchian.CHANNEL_N}, long-only, breakout event)")
        print("computing channel breakout flags on the REAL series ...")
        flags = donchian.flags_from_bars(bars, warmup=warmup)
    else:
        print("signal            : closed_analyser  "
              "(CLOSED -- kept reproducible, see STAGE1_VERDICT.md)")
        print("computing the analyser's flags on the REAL series, bar by bar ...")
        flags = sk.signal_flags(bars, cfg, warmup=warmup)
    # S1 (slice 40): for the DIRECTED path the real schedule is built from the
    # same array the nulls use, so an entry whose horizon cannot complete is
    # never ranked on either side.
    #
    # The two CLOSED long-only families keep raw `flags` deliberately. The same
    # one-bar tail asymmetry exists there in principle, but changing their
    # schedule would make the ABSENT numbers recorded in EDGE.md 4-13
    # non-reproducible from this code — a real cost for families nobody is
    # going to re-measure. It is written down here rather than fixed silently,
    # and a future slice that re-opens them must fix it first.
    if pending_book is not None:
        schedule_flags = tradable_flags(flags, eligible)
    else:
        schedule_flags = flags
    entries = sk.simulate_schedule(schedule_flags, warmup=warmup,
                                   lockup=args.lockup)
    if pending_book is not None:
        # S2 (slice 40): the direction sequence is the ordered directions of
        # the entries that filled AND passed the embargo. Before slice 35 it
        # came from every flagged bar (37 of 72 ETH trades scored in the wrong
        # direction); before slice 40 it still carried the one unscorable tail
        # entry, so the nulls recycled 125 directions over 124 slots.
        directions, side_lookup, side_net = pending_book
        book = DirectedBook(
            directions=directions,
            direction_seq=[directions[e] for e in entries if e in directions],
            lookup=side_lookup, net_r=side_net)
    observed = score_schedule(entries, lookup, net_r, book=book)
    if observed is None or observed.n_trades < 30:
        print("INCONCLUSIVE: fewer than 30 scoreable real-series entries.")
        return 2

    runs_observed, gaps_observed = sk.extract_blocks(flags & eligible, warmup=warmup)
    print()
    print(f"real-series schedule: {observed.n_trades} trades from "
          f"{len(runs_observed)} flag runs")
    print(f"strategy mean net R : {observed.mean_r:+.4f}")
    print()

    rng = np.random.default_rng(args.seed)

    # ---------------------------------------------------------------- M1 ---
    print("M1: ranking against the validated rotation null ...")
    rotations = rotation_replicates(
        flags, eligible, lookup, net_r, warmup=warmup, lockup=args.lockup,
        runs=args.runs, rng=rng, book=book)
    rotation_r = [s.mean_r for s in rotations]
    m1 = percentile_of(observed.mean_r, rotation_r)
    m1_pass = bool(m1 >= M1_PERCENTILE_BAR)
    print(f"  replicates        : {len(rotations):,} of {args.runs:,}")
    print(f"  null mean net R   : {np.mean(rotation_r):+.4f}  "
          f"(sd {np.std(rotation_r, ddof=1):.4f})")
    print(f"  null trade counts : median {np.median([s.n_trades for s in rotations]):.0f}")
    print(f"  M1 PERCENTILE     : {m1:.1f}   bar {M1_PERCENTILE_BAR}   "
          f"-> {'PASS' if m1_pass else 'FAIL'}")
    print()

    # ---------------------------------------------------------------- M2 ---
    print("M2: drift-controlled contrast against shape-matched schedules ...")
    shaped = shape_matched_replicates(
        flags, eligible, lookup, net_r, warmup=warmup, lockup=args.lockup,
        schedules=args.shape_schedules, rng=rng, book=book)
    shaped_r = [s.mean_r for s in shaped]
    m2_percentile = percentile_of(observed.mean_r, shaped_r)
    delta, low, high = bootstrap_ci(
        observed.mean_r, shaped_r, resamples=10_000,
        rng=np.random.default_rng(args.seed ^ 0xC0FFEE))
    m2_pass = bool(delta > 0 and low > 0 and m2_percentile >= M2_PERCENTILE_BAR)
    print(f"  schedules         : {len(shaped):,} of {args.shape_schedules:,}")
    print(f"  Side N mean net R : {np.mean(shaped_r):+.4f}  "
          f"(sd {np.std(shaped_r, ddof=1):.4f})")
    print(f"  Side N trades     : median {np.median([s.n_trades for s in shaped]):.0f}"
          f"   (Side S: {observed.n_trades})")
    print(f"  Delta             : {delta:+.4f}   95% CI [{low:+.4f}, {high:+.4f}]")
    print(f"  M2 PERCENTILE     : {m2_percentile:.1f}  bar {M2_PERCENTILE_BAR}")
    print(f"  M2                -> {'PASS' if m2_pass else 'FAIL'}"
          f"   (needs Delta>0 AND CI excludes 0 AND percentile>={M2_PERCENTILE_BAR})")
    print()

    # ---------------------------------------------------------------- M3 ---
    print("M3: walk-forward, per fold ...")
    fold_rows = []
    for number, (lo, hi) in enumerate(fold_bounds(len(bars), args.folds, warmup), 1):
        in_fold = [i for i in entries if lo <= i < hi]
        scored = score_schedule(in_fold, lookup, net_r, book=book)
        if scored is None:
            fold_rows.append(dict(fold=number, start=lo, end=hi, trades=0,
                                  mean_r=None, percentile=None,
                                  note="no scoreable entries"))
            print(f"  fold {number}: bars {lo}-{hi}  0 trades  n/a")
            continue
        fold_flags = np.zeros_like(flags)
        fold_flags[lo:hi] = schedule_flags[lo:hi]
        fold_eligible = np.zeros_like(eligible)
        fold_eligible[lo:hi] = eligible[lo:hi]
        pct = None
        note = ""
        if scored.n_trades >= 10:
            replicates = rotation_replicates(
                fold_flags, fold_eligible, lookup, net_r, warmup=warmup,
                lockup=args.lockup, runs=args.fold_runs, rng=rng,
                minimum_trades=max(5, scored.n_trades // 2), book=book)
            if len(replicates) >= args.fold_runs // 10:
                pct = percentile_of(scored.mean_r, [s.mean_r for s in replicates])
            else:
                note = f"only {len(replicates)} usable replicates"
        else:
            note = f"{scored.n_trades} trades: too few for a stable percentile"
        fold_rows.append(dict(fold=number, start=lo, end=hi,
                              trades=scored.n_trades, mean_r=scored.mean_r,
                              percentile=pct, note=note))
        print(f"  fold {number}: bars {lo}-{hi}  {scored.n_trades:3d} trades  "
              f"mean net R {scored.mean_r:+.4f}  "
              f"percentile {'n/a' if pct is None else f'{pct:.1f}'}"
              f"{('  [' + note + ']') if note else ''}")

    positive_folds = sum(1 for r in fold_rows
                         if r["mean_r"] is not None and r["mean_r"] > 0)
    m3_soft = positive_folds >= M3_SOFT_GATE_FOLDS
    print(f"  folds with mean net R > 0 : {positive_folds} of {len(fold_rows)}"
          f"   soft gate {'met' if m3_soft else 'NOT met'} (supporting only)")
    print()

    # ------------------------------------------------------------ verdict ---
    if m1_pass and m2_pass:
        verdict = "EDGE_EVIDENCE_POSITIVE"
    else:
        verdict = "EDGE_EVIDENCE_ABSENT"

    print("=" * 78)
    print("DECISION")
    print("=" * 78)
    print(f"  M1  percentile {m1:.1f} vs bar {M1_PERCENTILE_BAR}  -> "
          f"{'PASS' if m1_pass else 'FAIL'}")
    print(f"  M2  delta {delta:+.4f}, CI [{low:+.4f}, {high:+.4f}], "
          f"percentile {m2_percentile:.1f}  -> {'PASS' if m2_pass else 'FAIL'}")
    print(f"  M3  {positive_folds}/{len(fold_rows)} folds positive  "
          f"(soft, supporting only)")
    print()
    print(f"  {verdict}")
    print()
    if verdict == "EDGE_EVIDENCE_ABSENT":
        print("  The pre-declared bars are not met. No threshold, stop, size or")
        print("  confidence parameter may be retuned to change this reading.")
    else:
        print("  Edge evidence positive. Model / Stage 2 / shadow / live still")
        print("  BLOCKED. No real-series percentile may resize risk or override")
        print("  a gate.")

    # A percentile is only a claim if the ruler that produced it was checked.
    # Slice 35 learned this the hard way: a genuine POSITIVE summary from a run
    # whose directed control had FAILED was accepted by the registration hook,
    # and ProjectStatus announced a cleared edge. The attestation must be
    # supplied explicitly and must actually say the control passed.
    control_validated = False
    if args.control_attestation:
        try:
            with open(args.control_attestation, encoding="utf-8") as handle:
                attestation = handle.read()
            control_validated = "CONTROL: **VALID**" in attestation
        except Exception as exc:  # noqa: BLE001
            print(f"  control attestation unreadable ({exc}); "
                  "recording control_validated=false")
    print(f"  control_validated : {control_validated}"
          + ("" if control_validated
             else "  <- no validated-control attestation supplied"))

    payload = dict(
        symbol=symbol, bars=len(bars),
        signal=args.signal,
        instrument=dict(null="rotation", lockup=args.lockup, runs=args.runs,
                        horizon=args.horizon, atr_period=args.atr_period,
                        round_trip_bps=args.round_trip_bps,
                        take_profit_atr=args.take_profit_atr,
                        stop_atr=args.stop_atr, seed=args.seed),
        observed=asdict(observed), flag_runs=len(runs_observed),
        m1=dict(percentile=m1, bar=M1_PERCENTILE_BAR, passed=m1_pass,
                replicates=len(rotations), null_mean=float(np.mean(rotation_r)),
                null_sd=float(np.std(rotation_r, ddof=1))),
        m2=dict(percentile=m2_percentile, bar=M2_PERCENTILE_BAR, passed=m2_pass,
                delta=delta, ci_low=low, ci_high=high, schedules=len(shaped),
                null_mean=float(np.mean(shaped_r)),
                null_sd=float(np.std(shaped_r, ddof=1))),
        m3=dict(folds=fold_rows, positive=positive_folds,
                soft_gate=M3_SOFT_GATE_FOLDS, soft_gate_met=m3_soft),
        verdict=verdict,
        control_validated=control_validated,
        control_attestation=(args.control_attestation or None),
    )
    with open(f"{args.out_prefix}_summary.json", "w") as handle:
        json.dump(payload, handle, indent=2)
    with open(f"{args.out_prefix}_null_distributions.csv", "w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["family", "replicate", "n_trades", "mean_r"])
        for i, s in enumerate(rotations):
            writer.writerow(["rotation", i, s.n_trades, f"{s.mean_r:.6f}"])
        for i, s in enumerate(shaped):
            writer.writerow(["shape_matched", i, s.n_trades, f"{s.mean_r:.6f}"])
    with open(f"{args.out_prefix}_folds.csv", "w", newline="") as handle:
        writer = csv.DictWriter(
            handle, fieldnames=["fold", "start", "end", "trades", "mean_r",
                                "percentile", "note"])
        writer.writeheader()
        writer.writerows(fold_rows)
    print()
    print(f"artifacts: {args.out_prefix}_summary.json / _null_distributions.csv"
          f" / _folds.csv")
    return 0 if verdict == "EDGE_EVIDENCE_POSITIVE" else 1


if __name__ == "__main__":
    sys.exit(main())
