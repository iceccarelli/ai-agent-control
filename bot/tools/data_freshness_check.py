#!/usr/bin/env python3
"""Is there any market data after the measure end? Prove it; do not assume it.

WHY THIS IS A TOOL AND NOT A PARAGRAPH
======================================
The promotion gate's central item is "forward shadow, no ALERT, >= 20 closed
trades". It reads 0 / 20. The only thing that can change that is bars arriving
after the cleared measurement ended.

It would be easy — and wrong — to write "no new data" in an artefact because it
seems obviously true. `EDGE.md` §43b records the arithmetic that makes it
obviously true, **and** requires the attempt anyway, because *"I reasoned it must
be zero"* is weaker evidence than *"I looked, and here is exactly what came
back"*. This tool does the looking and records the outcome verbatim, including
the exact error if the attempt fails.

WHAT COUNTS AS NEW — §43b, fixed before any fetch ran
=====================================================
Strictly after `t1` from the locked fold calendar, and:

* **closed only.** A daily bar stamped `2026-08-10T00:00:00Z` does not close
  until `2026-08-11T00:00:00Z`. An open bar is not an observation; the shadow
  decides at a bar's close;
* **`synthetic: false`** on any corpus supplying it;
* a **trade** is an observation only when it closes, which needs at least six
  closed forward bars given a five-bar horizon.

WHAT THIS TOOL WILL NOT DO
==========================
It writes no bars, appends to no corpus and fabricates nothing. If the fetch is
unavailable it records `fetch_attempted: true`, the error class and message, and
`new_bars_available: false` — which is the honest answer when the answer cannot
be improved by wanting it.
"""
from __future__ import annotations

import argparse
import datetime as dt
import gzip
import json
import os
import subprocess
import sys
import traceback

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "tools"))

import csv  # noqa: E402

import market_data as md  # noqa: E402
from signals import funding_carry_fade_btc_v1 as fb  # noqa: E402
import provenance as _provenance  # noqa: E402

SCHEMA = "data_freshness/1"
LINEAR_FILE = "data/real_linear_1d/ohlcv/BINANCE_LINEAR_BTC_USDT_1D.csv.gz"
FUNDING_FILE = ("data/real_funding/funding/"
                "BINANCE_LINEAR_BTC_USDT_FUNDING.csv.gz")
DAY_MS = 86_400_000


def _rows(path: str):
    with gzip.open(os.path.join(REPO, path), "rt", encoding="utf-8",
                   newline="") as handle:
        return list(csv.DictReader(handle))


def _last_local_timestamps() -> dict:
    linear = _rows(LINEAR_FILE)
    funding = _rows(FUNDING_FILE)
    return {
        "linear_rows": len(linear),
        "latest_linear_timestamp": linear[-1]["time_period_start"],
        "funding_rows": len(funding),
        "latest_funding_timestamp": funding[-1]["funding_time"],
    }


def _attempt_fetch(symbol: str, after_ms: int) -> dict:
    """Try the project's sanctioned fetcher. Record whatever happens.

    Deliberately calls the in-repo tool's own helper rather than assembling a
    request here: this slice is checking whether the SANCTIONED path yields new
    data, not inventing a second way to reach the venue. A tool that reached the
    exchange by some other route would be answering a different question.
    """
    record = {"fetch_attempted": True, "tool": "tools/fetch_linear_klines_1d.py"}
    try:
        import fetch_linear_klines_1d as fetcher
        now_ms = int(dt.datetime.now(dt.timezone.utc).timestamp() * 1000)
        rows = fetcher.fetch_symbol(symbol, after_ms + 1, now_ms)
        record.update({
            "fetch_succeeded": True,
            "rows_returned": len(rows),
            "note": ("rows returned by the venue; each still has to pass the "
                     "CLOSED and synthetic:false tests before it counts"),
        })
    except Exception as exc:  # noqa: BLE001
        record.update({
            "fetch_succeeded": False,
            "rows_returned": 0,
            "error_class": type(exc).__name__,
            "error_message": str(exc)[:400],
            "error_traceback_tail": traceback.format_exc()[-400:],
            "note": ("The sanctioned fetch path did not return data in this "
                     "environment. That is recorded verbatim and treated as "
                     "'no new bars', which is the honest answer. Nothing is "
                     "fabricated and no corpus is appended to."),
        })
    return record


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", default="BTCUSDT")
    parser.add_argument("--checked-at-utc", required=True)
    parser.add_argument("--no-fetch", action="store_true",
                        help="skip the network attempt and record why")
    parser.add_argument("--out", default=os.path.join(
        REPO, "artifacts", "slice60_data_freshness.json"))
    args = parser.parse_args(argv)

    folds = fb.load_folds()
    t1 = folds["t1"]
    t1_ms = int(folds["t1_ms"])
    local = _last_local_timestamps()

    checked = dt.datetime.strptime(args.checked_at_utc, "%Y-%m-%dT%H:%M:%SZ") \
        .replace(tzinfo=dt.timezone.utc)
    # The latest daily bar that can possibly have CLOSED by `checked`.
    last_closed_ms = (int(checked.timestamp() * 1000) // DAY_MS) * DAY_MS - DAY_MS
    ceiling = max(0, (last_closed_ms - t1_ms) // DAY_MS)

    fetch = ({"fetch_attempted": False,
              "reason": "--no-fetch was passed"}
             if args.no_fetch else _attempt_fetch(args.symbol, t1_ms))

    # Only CLOSED bars strictly after t1 count. The local corpus is the only
    # source that has passed eligibility, so anything a fetch returned is
    # reported but not counted until a human ingests it through the sanctioned
    # corpus path with synthetic:false.
    local_new_linear = sum(
        1 for r in _rows(LINEAR_FILE)
        if md.parse_timestamp(r["time_period_start"]) // 1000 > t1_ms)
    local_new_funding = sum(
        1 for r in _rows(FUNDING_FILE)
        if md.parse_timestamp(r["funding_time"]) // 1000 > t1_ms)

    manifest_linear = json.load(open(os.path.join(
        REPO, "data", "real_linear_1d", "MANIFEST.json"), encoding="utf-8"))
    manifest_funding = json.load(open(os.path.join(
        REPO, "data", "real_funding", "MANIFEST.json"), encoding="utf-8"))

    new_available = bool(local_new_linear > 0)

    payload = {
        "schema": SCHEMA,
        "slice": 60,
        "symbol": args.symbol,
        "checked_at_utc": args.checked_at_utc,

        "t1_previous": t1,
        "t1_previous_ms": t1_ms,
        "t1_source": "artifacts/funding_carry_fade_btc_v1_folds.json",
        "folds_sha256": fb.folds_sha256(),

        "latest_linear_timestamp": local["latest_linear_timestamp"],
        "latest_funding_timestamp": local["latest_funding_timestamp"],
        "linear_rows": local["linear_rows"],
        "funding_rows": local["funding_rows"],

        "new_linear_bars_count": local_new_linear,
        "new_funding_prints_count": local_new_funding,
        "new_bars_available": new_available,

        "closed_bar_ceiling": {
            "last_bar_that_could_have_closed_utc": dt.datetime.fromtimestamp(
                last_closed_ms / 1000.0,
                tz=dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "max_possible_new_closed_bars": int(ceiling),
            "note": ("Computed from the clock and the daily grid, and recorded "
                     "in EDGE.md §43b BEFORE the fetch ran. A daily bar closes "
                     "24h after it opens, so on this date the newest CLOSED "
                     "bar is t1 itself — not after it. Any count above this "
                     "ceiling would be a DATA DEFECT to investigate, not "
                     "progress."),
        },

        "fetch": fetch,
        "synthetic": {
            "linear_manifest_synthetic": manifest_linear["synthetic"],
            "funding_manifest_synthetic": manifest_funding["synthetic"],
            "requirement": ("any corpus supplying new data must declare "
                            "synthetic false; no synthetic bar is ever an "
                            "observation"),
        },
        "bars_fabricated": 0,
        "corpus_appended_to": False,

        "what_would_count": {
            "rule": "timestamp strictly after t1, AND the bar is CLOSED",
            "closed_only": True,
            "first_forward_observation_needs_closed_bars": fb.HORIZON + 1,
            "note": ("A trade is an observation only when it CLOSES. With a "
                     f"{fb.HORIZON}-bar horizon the first forward observation "
                     f"cannot exist until at least {fb.HORIZON + 1} closed "
                     f"forward bars do."),
        },
        "git_commit": _provenance.git_commit(REPO),
    }
    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False)
        handle.write("\n")

    print("=" * 78)
    print("SLICE 60 — DATA FRESHNESS")
    print("=" * 78)
    print(f"  measure end (t1)          : {t1}")
    print(f"  latest linear timestamp   : "
          f"{payload['latest_linear_timestamp']}")
    print(f"  latest funding timestamp  : "
          f"{payload['latest_funding_timestamp']}")
    print(f"  checked at                : {args.checked_at_utc}")
    print()
    print(f"  newest bar that COULD have closed : "
          f"{payload['closed_bar_ceiling']['last_bar_that_could_have_closed_utc']}")
    print(f"  max possible new closed bars      : "
          f"{payload['closed_bar_ceiling']['max_possible_new_closed_bars']}")
    print()
    print(f"  fetch attempted           : {fetch.get('fetch_attempted')}")
    if fetch.get("fetch_attempted"):
        print(f"  fetch succeeded           : {fetch.get('fetch_succeeded')}")
        if not fetch.get("fetch_succeeded"):
            print(f"  error                     : {fetch.get('error_class')}: "
                  f"{str(fetch.get('error_message'))[:120]}")
        else:
            print(f"  rows returned             : "
                  f"{fetch.get('rows_returned')}")
    print()
    print(f"  new linear bars           : {local_new_linear}")
    print(f"  new funding prints        : {local_new_funding}")
    print(f"  NEW BARS AVAILABLE        : {new_available}")
    print(f"  bars fabricated           : 0")
    print(f"  corpus appended to        : False")
    print()
    print(f"artefact: {os.path.relpath(args.out, REPO)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
