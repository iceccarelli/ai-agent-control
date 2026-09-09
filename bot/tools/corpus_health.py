#!/usr/bin/env python3
"""Do the three series the book depends on agree with each other?

WHY THIS EXISTS
===============
The carry book reads THREE corpora and they are only meaningful together:

    BINANCE_LINEAR_BTC_USDT_1D     the perp leg
    BINANCE_SPOT_BTC_USDT_1D       the spot leg
    BINANCE_LINEAR_BTC_USDT_FUNDING  the income

The basis is a DIFFERENCE between the first two and the carry decision needs
all three on the same UTC day. Measured on the tree as delivered:

    perp     2022-08-10 .. 2026-08-24   stale by 16 days
    spot     2022-09-09 .. 2026-09-08   stale by  1 day
    funding  2022-08-09 .. 2026-08-25   stale by 15 days

    30 perp days have no spot. 15 spot days have no perp.

`carry_backtest` intersects them silently. Nothing reported the gap and nothing
refused when one series fell 16 days behind the others. A book that decides on
today's funding against a spot mark from two weeks ago is not hedged, it is
guessing — and it would never say so.

This tool says so.

WHAT IT CHECKS
==============
  STALENESS   how far behind the last CLOSED UTC day each series is
  ALIGNMENT   days present in one series and missing from another
  OPEN BARS   a row dated today or later, written as a close
  MONOTONIC   strictly increasing timestamps, no duplicates
  CONTINUITY  calendar gaps inside each series
  UNITS       delegated to corpus_unit_audit's run-vs-point test

WHAT IT DOES NOT DO
===================
Fetch, repair, or write. It reads and reports and exits non-zero. Silently
rewriting a provenance record to whatever looks plausible is the failure this
programme exists to prevent, and a health check that fixes things is a health
check nobody can trust the output of.

    python3 tools/corpus_health.py --repo .
    python3 tools/corpus_health.py --repo . --max-stale-days 2
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import gzip
import hashlib
import json
import os
import sys
from typing import Any, Dict, List, Optional, Set

#: How far behind the last closed UTC day a series may be before the book
#: should refuse to act on it. Two days: one for the day that has not closed,
#: one for a venue or fetch that ran late. Beyond that, someone stopped
#: refreshing and the book must not pretend otherwise.
DEFAULT_MAX_STALE_DAYS = 2

#: name, path, timestamp column, rows-per-day.
#: Funding prints THREE times a day on Binance USDT-M, so repeated dates are the
#: schedule, not a duplicate. A daily bar series repeating a date IS a defect.
#: Applying one rule to both would either miss real duplicates in the bar series
#: or cry wolf 2,953 times on funding, and a check that cries wolf gets ignored.
SERIES = [
    ("perp_1d", "data/real_linear_1d/ohlcv/BINANCE_LINEAR_BTC_USDT_1D.csv.gz",
     "time_period_start", 1),
    ("spot_1d", "data/real_spot_btc/ohlcv/BINANCE_SPOT_BTC_USDT_1D.csv.gz",
     "time_period_start", 1),
    ("funding", "data/real_funding/funding/BINANCE_LINEAR_BTC_USDT_FUNDING.csv.gz",
     "funding_time_ms", 3),
]


def _open(path: str):
    return gzip.open(path, "rt") if path.endswith(".gz") else open(path, "rt")


def _to_date(raw: str, column: str) -> dt.date:
    if column == "funding_time_ms":
        return dt.datetime.fromtimestamp(
            int(raw) / 1000.0, dt.timezone.utc).date()
    return dt.datetime.fromisoformat(raw.replace("Z", "+00:00")).date()


def read_days(path: str, column: str) -> List[dt.date]:
    with _open(path) as handle:
        return [_to_date(row[column], column) for row in csv.DictReader(handle)]


def sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


def inspect(repo: str, name: str, rel: str, column: str, per_day: int,
            today: dt.date) -> Dict[str, Any]:
    path = os.path.join(repo, rel)
    if not os.path.exists(path):
        return {"name": name, "path": rel, "present": False,
                "problems": ["MISSING"]}
    days = read_days(path, column)
    if not days:
        return {"name": name, "path": rel, "present": True, "rows": 0,
                "problems": ["EMPTY"]}

    unique = sorted(set(days))
    last_closed = today - dt.timedelta(days=1)
    problems: List[str] = []

    # An OPEN bar is a day that has not finished, written as a close. Binance
    # returns the in-progress bar from klines and the fetcher writes it.
    open_bars = [d for d in unique if d >= today]
    if open_bars:
        problems.append(f"OPEN_BAR_IN_FILE:{open_bars[-1]}")

    stale_days = (last_closed - max(d for d in unique if d < today)).days \
        if any(d < today for d in unique) else 10_000
    if stale_days > 0:
        problems.append(f"STALE:{stale_days}d")

    if per_day == 1 and len(days) != len(unique):
        problems.append(f"DUPLICATE_DAYS:{len(days) - len(unique)}")
    if per_day > 1:
        thin = [d for d in unique[:-1] if days.count(d) < per_day]
        if thin:
            problems.append(f"INCOMPLETE_DAYS:{len(thin)}:{thin[-1]}")
    if days != sorted(days):
        problems.append("NOT_MONOTONIC")

    gaps = [(unique[i - 1], unique[i]) for i in range(1, len(unique))
            if (unique[i] - unique[i - 1]).days > 1]
    if gaps:
        problems.append(f"CALENDAR_GAPS:{len(gaps)}")

    return {"name": name, "path": rel, "present": True, "rows": len(days),
            "first": str(unique[0]), "last": str(unique[-1]),
            "stale_days": stale_days, "open_bars": [str(d) for d in open_bars],
            "gaps": [[str(a), str(b)] for a, b in gaps[:5]],
            "sha256": sha256(path), "problems": problems,
            "_days": set(unique)}


def check(repo: str, *, max_stale_days: int = DEFAULT_MAX_STALE_DAYS,
          today: Optional[dt.date] = None) -> Dict[str, Any]:
    today = today or dt.datetime.now(dt.timezone.utc).date()
    reports = [inspect(repo, n, r, c, k, today)
               for n, r, c, k in SERIES]
    present = [r for r in reports if r.get("_days")]

    alignment: List[Dict[str, Any]] = []
    overlap: Optional[Set[dt.date]] = None
    for report in present:
        overlap = set(report["_days"]) if overlap is None \
            else overlap & report["_days"]
    for a in present:
        for b in present:
            if a is b:
                continue
            missing = a["_days"] - b["_days"]
            if missing:
                alignment.append({"days_in": a["name"], "missing_from": b["name"],
                                  "count": len(missing),
                                  "example": str(max(missing))})

    problems = [f"{r['name']}: {p}" for r in reports for p in r["problems"]]
    too_stale = [r["name"] for r in present
                 if r.get("stale_days", 0) > max_stale_days]

    for report in reports:
        report.pop("_days", None)

    return {
        "checked_utc": str(today),
        "max_stale_days": max_stale_days,
        "series": reports,
        "three_way_overlap_days": len(overlap or ()),
        "overlap_first": str(min(overlap)) if overlap else None,
        "overlap_last": str(max(overlap)) if overlap else None,
        "alignment_gaps": alignment,
        "series_too_stale": too_stale,
        "problems": problems,
        "healthy": not problems and not alignment,
        "note": ("carry_backtest INTERSECTS these series silently. A day present "
                 "in one and missing from another is dropped without comment, "
                 "and a series that stops refreshing looks like a quiet market."),
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repo", default=".")
    parser.add_argument("--max-stale-days", type=int,
                        default=DEFAULT_MAX_STALE_DAYS)
    parser.add_argument("--out", default="")
    args = parser.parse_args(argv)

    report = check(args.repo, max_stale_days=args.max_stale_days)

    print("=" * 74)
    print("CORPUS HEALTH — do the three series agree?")
    print("=" * 74)
    print(f"  as of {report['checked_utc']} UTC\n")
    for series in report["series"]:
        if not series.get("present"):
            print(f"  {series['name']:9s} MISSING  {series['path']}")
            continue
        print(f"  {series['name']:9s} {series['first']} .. {series['last']}  "
              f"n={series['rows']:5d}  stale {series['stale_days']}d")
        print(f"            sha256 {series['sha256'][:16]}…")
        for problem in series["problems"]:
            print(f"            ! {problem}")
    print(f"\n  three-way overlap  {report['overlap_first']} .. "
          f"{report['overlap_last']}  n={report['three_way_overlap_days']}")
    for gap in report["alignment_gaps"]:
        print(f"  ! {gap['count']:4d} days in {gap['days_in']} are missing "
              f"from {gap['missing_from']} (e.g. {gap['example']})")
    print()
    if report["series_too_stale"]:
        print(f"  REFUSE: {', '.join(report['series_too_stale'])} exceed "
              f"{report['max_stale_days']} days stale.")
        print("  A book deciding on today's funding against a two-week-old spot")
        print("  mark is not hedged. It is guessing, and it would not say so.")
    print(f"\n  healthy: {report['healthy']}")
    print(f"  {report['note']}")

    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            json.dump(report, handle, indent=2)
    return 0 if report["healthy"] else 1


if __name__ == "__main__":
    sys.exit(main())
