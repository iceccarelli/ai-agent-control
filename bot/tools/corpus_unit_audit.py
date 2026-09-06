#!/usr/bin/env python3
"""Read-only meaning-layer audit of ``volume_traded``. Flags unit swaps.

The prefix-hash / append-only tests in this tree prove BYTES are honest.
They say nothing about MEANING: a column that silently switches from
base-asset volume to quote-notional (or back) passes every existing
invariant untouched, because the bytes are still a valid, append-only,
monotonic CSV. Only the number itself is wrong by four to five orders of
magnitude for the days it happened.

Verified against the shipped corpus (this tool, this file, 2026-09-05):
2026-08-18 through 2026-08-22 carry values 5.4e9 .. 3.5e10 against a
1,476-row median of 227,714 - a run of 5 contiguous days at
23,738x-151,759x the file median, then a clean return to the prior order
of magnitude on 2026-08-23. That is not a fat-tailed volume day; BTC's
entire spot+derivatives volume never reaches five billion COINS traded in
a day. It is quote-notional (USD) leaking into a base-volume column for
exactly the venue-typical shape of a unit mismatch.

Exit codes: 0 clean; 1 SUSPECT_UNITS (a contiguous run flagged);
2 insufficient rows to judge.

This tool does not rewrite the corpus, does not touch FUND_ABS, does not
score anything a gate reads, and is not wired into the promotion path.
It is a second pair of eyes on a column the cleared signal
(funding_carry_fade_btc_v1) does not use - see
tests/test_corpus_unit_audit.py for which signals DO use volume and are
therefore exposed until this is fixed at the source.
"""
from __future__ import annotations

import argparse
import csv
import gzip
import os
import statistics
import sys
from typing import List, Optional, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DEFAULT_CSV = os.path.join(ROOT, "data", "real_linear_1d", "ohlcv",
                           "BINANCE_LINEAR_BTC_USDT_1D.csv.gz")


def _open(path: str):
    return gzip.open(path, "rt") if path.endswith(".gz") else open(path, "r", newline="")


def load_volume(path: str) -> List[Tuple[str, float]]:
    with _open(path) as fh:
        rows = list(csv.DictReader(fh))
    out: List[Tuple[str, float]] = []
    for row in rows:
        date = (row.get("time_period_start") or row.get("date") or "")[:10]
        raw = row.get("volume_traded") or row.get("volume") or ""
        try:
            out.append((date, float(raw)))
        except (TypeError, ValueError):
            continue
    return out


def audit(rows: List[Tuple[str, float]], run_threshold: float = 1_000.0,
          min_run: int = 3) -> Tuple[int, List[dict]]:
    """Returns (exit_code, runs). A run is >= min_run contiguous rows each
    more than run_threshold times (or less than 1/run_threshold times) the
    whole-file median.
    """
    xs = [v for _, v in rows if v > 0]
    if len(xs) < 20:
        return 2, []
    med = statistics.median(xs)
    print(f"rows={len(rows)} median_volume={med:.4f}")

    runs: List[dict] = []
    cur: List[Tuple[str, float]] = []

    def ratio(v: float) -> float:
        return (v / med) if med else float("inf")

    def flush() -> None:
        if len(cur) >= min_run:
            runs.append({
                "start": cur[0][0], "end": cur[-1][0], "n": len(cur),
                "xmed_min": min(ratio(v) for _, v in cur),
                "xmed_max": max(ratio(v) for _, v in cur),
            })
        cur.clear()

    for date, v in rows:
        r = ratio(v)
        if r > run_threshold or (0 < r < 1.0 / run_threshold):
            cur.append((date, v))
        else:
            flush()
    flush()

    for r in runs:
        print(f"RUN {r['start']}..{r['end']} n={r['n']} "
              f"xmed={r['xmed_min']:.1f}..{r['xmed_max']:.1f}")
        print("  consistent with UNIT SWAP (quote volume in base column)")
    return (1 if runs else 0), runs


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", default=ROOT)
    ap.add_argument("--csv", default="", help="override the corpus path")
    args = ap.parse_args(argv)
    path = args.csv or DEFAULT_CSV
    rows = load_volume(path)
    code, _ = audit(rows)
    print("CLEAN" if code == 0 else ("INSUFFICIENT_ROWS" if code == 2 else "SUSPECT_UNITS"))
    return code


if __name__ == "__main__":
    sys.exit(main())
