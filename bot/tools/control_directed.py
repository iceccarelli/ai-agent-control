"""Is the DIRECTED instrument honest? The control slice 35 owes before it reports.

WHY THIS EXISTS
===============
`btc_alt_spillover_v1` is the first **two-sided** signal this instrument has
scored, and the first to fill at the *next* bar's open. That changes run
construction: a rotation replicate now re-places the entries while carrying the
observed direction sequence with it.

Slice 23 validated the instrument for the long-only, enter-at-close construction
at n = 200. It did **not** validate this one. The mission's STEP 4 is explicit —
*if you change run construction, re-validate the control at n ≥ 200 first* — and
EDGE.md §16b committed to doing so before interpreting any real-series
percentile.

THE TEST
========
Destroy the alt's temporal structure and keep everything else. `surrogate_series`
shuffles whole alt bars and re-bases them onto a running price, preserving the
return distribution, the total drift and **the dates**. The BTC driver is left
untouched, so the signal fires on exactly the same calendar dates it always
would.

On such a series there is nothing to time: BTC's shock dates cannot predict a
price path whose bar order was drawn from a hat. A correct instrument must
therefore report percentiles uniform on [0, 100].

THE CRITERION (human pre-declaration, slice 37)
==============================================
Adopted verbatim, before any surrogate in this slice was drawn:

    CONTROL VALID if and only if ALL THREE hold:
      (a) |z| < 1.96          two-sided, mean of surrogate percentiles vs 50,
                              SE = sd / sqrt(n_surrogates)   [SAMPLE sd]
      (b) KS vs Uniform(0,100) NOT rejected at p >= 0.05
      (c) incompletes <= 5% of surrogates

This replaces `median <= 50` **for the directed path only**. That clause is
symmetric about 50 for a *correct* instrument, so it fails roughly half the time
at any n — proved analytically in EDGE.md §11a and confirmed empirically at
n = 1,000 in §17c, where the distribution was uniform and the clause still
failed. A gate a correct instrument fails 50% of the time is not a gate.

**The median is retired as a gate and kept as an informational line.** Demoted,
not deleted: a future reader can still see it, and a test asserts that a median
above 50 alone cannot force INVALID when (a), (b) and (c) all hold.

Slice 36 made the case for this change and deliberately did **not** act on it —
the slice that benefits from a gate must not be the slice that writes it. The
human made the call independently and in advance.

Exit 0 when valid, 1 when not. **If this exits 1, no real-series percentile from
the directed path may be reported at all.**
"""
from __future__ import annotations

import argparse
import math
import os
import statistics
import sys
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "tools"))

import numpy as np  # noqa: E402

import backtest as bt  # noqa: E402
import market_data as md  # noqa: E402
import skill_test as sk  # noqa: E402
import edge_measurement as em  # noqa: E402
from signals import btc_alt_spillover_v1 as spillover  # noqa: E402

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




#: The human pre-declaration. Named constants so a change is a visible edit
#: rather than a number quietly moving inside an expression.
Z_ABS_MAX = 1.96
KS_MIN_P = 0.05
MAX_INCOMPLETE_SHARE = 0.05


@dataclass(frozen=True)
class ControlVerdict:
    """The three clauses, their inputs, and the conjunction."""

    n: int
    mean: float
    sd: float
    z: float
    ks_stat: float
    ks_p: float
    incomplete: int
    total: int
    incomplete_share: float
    median: float           # informational ONLY — never a clause
    z_ok: bool
    ks_ok: bool
    complete_ok: bool
    valid: bool
    failures: List[str]


def _ks_uniform_p(values: Sequence[float]) -> tuple:
    """KS statistic and p-value against Uniform(0, 100).

    `scipy.stats.kstest` with the fully-specified null `uniform(loc=0,
    scale=100)` — a one-sample KS test against a *known* distribution, which is
    the correct form here because the null is fixed a priori rather than
    estimated from the data.
    """
    from scipy import stats
    result = stats.kstest(list(values), "uniform", args=(0.0, 100.0))
    return float(result.statistic), float(result.pvalue)


def evaluate_control(percentiles: Sequence[float], *, incomplete: int,
                     total: int) -> ControlVerdict:
    """Apply the human three-clause rule. Pure — no I/O, no globals.

    Separated from the runner so the rule itself can be tested against
    hand-built percentile vectors rather than only against 20-minute surrogate
    runs.
    """
    import statistics as _stats

    n = len(percentiles)
    if n == 0:
        return ControlVerdict(
            n=0, mean=float("nan"), sd=float("nan"), z=float("nan"),
            ks_stat=float("nan"), ks_p=0.0, incomplete=incomplete, total=total,
            incomplete_share=1.0, median=float("nan"),
            z_ok=False, ks_ok=False, complete_ok=False, valid=False,
            failures=["no usable surrogates"])

    mean = _stats.fmean(percentiles)
    sd = _stats.stdev(percentiles) if n > 1 else 0.0
    # SE from the SAMPLE sd, exactly as the pre-declaration specifies.
    se = (sd / math.sqrt(n)) if sd > 0 else float("inf")
    z = (mean - 50.0) / se if se not in (0.0, float("inf")) else 0.0
    ks_stat, ks_p = _ks_uniform_p(percentiles)
    share = (incomplete / total) if total else 1.0

    z_ok = abs(z) < Z_ABS_MAX
    ks_ok = ks_p >= KS_MIN_P
    complete_ok = share <= MAX_INCOMPLETE_SHARE

    failures: List[str] = []
    if not z_ok:
        failures.append(f"(a) |z| = {abs(z):.2f} >= {Z_ABS_MAX}")
    if not ks_ok:
        failures.append(f"(b) KS p = {ks_p:.4f} < {KS_MIN_P}")
    if not complete_ok:
        failures.append(
            f"(c) incompletes {share:.1%} > {MAX_INCOMPLETE_SHARE:.0%}")

    return ControlVerdict(
        n=n, mean=mean, sd=sd, z=z, ks_stat=ks_stat, ks_p=ks_p,
        incomplete=incomplete, total=total, incomplete_share=share,
        median=_stats.median(percentiles),
        z_ok=z_ok, ks_ok=ks_ok, complete_ok=complete_ok,
        valid=bool(z_ok and ks_ok and complete_ok), failures=failures)


def build_book_and_flags(alt_bars, btc_bars, *, warmup, args):
    """Exactly what `edge_measurement` builds, via the same calls."""
    flags, directions = spillover.flags_and_directions(
        alt_bars, btc_bars, warmup=warmup)

    side_lookup: Dict[str, Dict[int, int]] = {}
    side_net: Dict[str, np.ndarray] = {}
    for tag, side in ((spillover.LONG_SETUP, "long"),
                      (spillover.SHORT_SETUP, "short")):
        idx, net, _u = sk.barrier_r_for_all_bars(
            alt_bars, take_profit_atr=args.take_profit_atr,
            stop_atr=args.stop_atr, horizon=args.horizon,
            atr_period=args.atr_period, round_trip_bps=args.round_trip_bps,
            side=side, entry_on=spillover.ENTRY_ON,
        )
        side_lookup[tag] = {int(i): p for p, i in enumerate(idx)}
        side_net[tag] = net

    eligible = np.zeros(len(alt_bars), dtype=bool)
    common = set(side_lookup[spillover.LONG_SETUP]) & \
        set(side_lookup[spillover.SHORT_SETUP])
    eligible[sorted(common)] = True
    eligible[:warmup] = False

    # The book is completed by the caller once the ACTUAL entries are known:
    # the direction sequence the nulls recycle must come from the bars entered,
    # not from every flagged bar. See `edge_measurement.score_schedule`.
    return (flags, eligible, directions, side_lookup, side_net,
            side_lookup[spillover.LONG_SETUP], side_net[spillover.LONG_SETUP])


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", default=os.path.join(REPO, "data", "real_multi_1d"))
    parser.add_argument("--btc-data", default=os.path.join(REPO, "data", "real_1d"))
    parser.add_argument("--symbol", default="ETHUSDT")
    parser.add_argument("--interval", default="D")
    parser.add_argument("--control-runs", type=int, default=200)
    parser.add_argument("--runs", type=int, default=1500)
    parser.add_argument("--lockup", type=int, default=1)
    parser.add_argument("--horizon", type=int, default=spillover.HORIZON)
    parser.add_argument("--atr-period", type=int, default=spillover.ATR_PERIOD)
    parser.add_argument("--stop-atr", type=float, default=spillover.STOP_ATR)
    parser.add_argument("--take-profit-atr", type=float,
                        default=spillover.TAKE_PROFIT_ATR)
    parser.add_argument("--round-trip-bps", type=float,
                        default=spillover.ROUND_TRIP_BPS)
    parser.add_argument("--warmup", type=int, default=200)
    parser.add_argument("--seed", type=int, default=20250730)
    args = parser.parse_args(argv)

    loaded, _b, _n = md.load_corpus(args.data_dir)
    alt_bars = [bt.Bar(b.start_ms, b.open, b.high, b.low, b.close, b.volume)
                for b in loaded[args.symbol]]
    btc_loaded, _bb, _bn = md.load_corpus(args.btc_data)
    btc_symbol = sorted(btc_loaded)[0]
    btc_bars = [bt.Bar(b.start_ms, b.open, b.high, b.low, b.close, b.volume)
                for b in btc_loaded[btc_symbol]]

    print("=" * 78)
    print("DIRECTED-CONSTRUCTION CONTROL — btc_alt_spillover_v1")
    print("=" * 78)
    print(f"  alt              : {args.symbol} ({len(alt_bars):,} bars) "
          f"from {args.data_dir}")
    # Both counts, always. Printing only the loaded figure would credit the
    # run with bars the trigger never sees, and printing only the used figure
    # would hide that anything was dropped.
    _used = spillover.usable_btc_bars(btc_bars)
    _dropped = len(btc_bars) - len(_used)
    print(f"  BTC driver       : {btc_symbol} ({len(_used):,} bars used"
          + (f", {_dropped} excluded: "
             f"{', '.join(spillover.EXCLUDED_BTC_UTC_DATES)}"
             if _dropped else "") + ")")
    print(f"  surrogates       : {args.control_runs}")
    print(f"  rotations each   : {args.runs}")
    print(f"  barrier          : +{args.take_profit_atr:g}/-{args.stop_atr:g} ATR, "
          f"horizon {args.horizon}, {args.round_trip_bps:g} bps, "
          f"entry {spillover.ENTRY_ON}")
    print()
    print("Whole alt bars are shuffled and re-based; the DATES and the BTC")
    print("driver are untouched, so the signal fires on the same calendar days.")
    print("On a series with no temporal structure there is nothing to time, so")
    print("a correct instrument must report percentiles uniform on [0, 100].")
    print()

    percentiles: List[float] = []
    incomplete = 0
    for run in range(1, args.control_runs + 1):
        shuffled = sk.surrogate_series(alt_bars, seed=args.seed + 1000 + run)
        (flags, eligible, directions, side_lookup, side_net,
         lookup, net_r) = build_book_and_flags(
            shuffled, btc_bars, warmup=args.warmup, args=args)
        # S1/S3 (slice 40): the surrogate's own "observed" schedule is built
        # from the SAME array its nulls rotate, so neither side can be a trade
        # longer than the other for a reason unrelated to timing.
        entries = sk.simulate_schedule(
            em.tradable_flags(flags, eligible),
            warmup=args.warmup, lockup=args.lockup)
        book = em.DirectedBook(
            directions=directions,
            direction_seq=[directions[e] for e in entries if e in directions],
            lookup=side_lookup, net_r=side_net)
        observed = em.score_schedule(entries, lookup, net_r, book=book)
        if observed is None or observed.n_trades < 20:
            incomplete += 1
            print(f"  surrogate {run}: INCOMPLETE (too few scoreable entries)")
            continue
        rng = np.random.default_rng(args.seed + 7_000_000 + run)
        replicates = em.rotation_replicates(
            flags, eligible, lookup, net_r, warmup=args.warmup,
            lockup=args.lockup, runs=args.runs, rng=rng, book=book)
        if len(replicates) < args.runs // 10:
            incomplete += 1
            print(f"  surrogate {run}: INCOMPLETE ({len(replicates)} replicates)")
            continue
        pct = em.percentile_of(observed.mean_r, [s.mean_r for s in replicates])
        percentiles.append(pct)
        print(f"  surrogate {run}: percentile {pct:5.1f}  "
              f"({observed.n_trades} trades)", flush=True)

    print()
    print("=" * 78)
    print("VERDICT")
    print("=" * 78)

    verdict = evaluate_control(percentiles, incomplete=incomplete,
                               total=args.control_runs)
    if verdict.n == 0:
        print("no usable surrogates")
        print("CONTROL: **INVALID** — no usable surrogates")
        return 1

    print(f"surrogates used      : {verdict.n} of {args.control_runs}")
    print(f"  (a) mean vs 50     : mean {verdict.mean:.2f}, sd {verdict.sd:.2f}, "
          f"z = {verdict.z:+.3f}   |z| < {Z_ABS_MAX} -> "
          f"{'PASS' if verdict.z_ok else 'FAIL'}")
    print(f"  (b) KS vs U(0,100) : D = {verdict.ks_stat:.4f}, "
          f"p = {verdict.ks_p:.4f}   p >= {KS_MIN_P} -> "
          f"{'PASS' if verdict.ks_ok else 'FAIL'}")
    print(f"  (c) incompletes    : {verdict.incomplete} of {verdict.total} "
          f"({verdict.incomplete_share:.1%})   <= "
          f"{MAX_INCOMPLETE_SHARE:.0%} -> "
          f"{'PASS' if verdict.complete_ok else 'FAIL'}")
    print()
    print(f"  median {verdict.median:.2f}   INFORMATIONAL ONLY — retired as a "
          "gate in slice 37;")
    print("  it is a coin flip on a correct instrument (EDGE.md 11a, 17c) and "
          "decides nothing.")
    print()

    if verdict.valid:
        print("CONTROL: **VALID** — all three pre-declared clauses hold.")
        print()
        print("The DIRECTED construction (two-sided, next-open fill, rotation")
        print("carrying the observed direction sequence) is unbiased on series")
        print("where nothing can be timed. THIS SAYS NOTHING ABOUT EDGE.")
        return 0

    print("CONTROL: **INVALID — the directed instrument cannot be trusted.**")
    print(f"  failed: {'; '.join(verdict.failures)}")
    print()
    print("No real-series percentile from the directed path may be reported.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
