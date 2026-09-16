#!/usr/bin/env python3
"""Grow the forward window without rewriting one byte of history.

WHY THIS EXISTS
===============
The forward programme is blocked on data, and not on patience.

`t1` is 2026-08-09. The daily corpus ends **2026-08-24** and the venue has bars
through today, so the forward window has been frozen at 15 bars while roughly
three weeks accumulated at Binance. Slice 76 states the consequence exactly:

    "Eligibility reaches back seven bars, so with 2026-08-24 last the highest
     scoreable bar is 2026-08-17 and all four flags sit in the tail."

Four setups have fired in the window and none is scoreable, because every one
of them is stranded against the end of the file. More bars move them out of the
tail. Nothing else does.

WHY THE EXISTING FETCHERS CANNOT DO THIS
========================================
`fetch_linear_klines_1d.py` and `fetch_funding_history.py` both end in
`write_gz(path, rows)` over a window computed as `--years` back from **now**.
They REPLACE the file. Run either against this corpus and one of two things
happens, both fatal to the invariant in `tools/corpus_prefix.py`:

* at `--years 4.0` the file would start 2022-09-16 rather than 2022-08-10, so
  it SHRINKS at the front and `never_shrank` goes false;
* at a larger `--years` it gains rows before the pinned prefix, the prefix
  shifts, and `history_unchanged` goes false.

Either way the corpus stops being the one the cut was locked against. That is
why the corpus has not been refreshed, and it is a tooling defect rather than a
decision anybody took.

WHAT THIS DOES INSTEAD
======================
Append, and prove it appended:

1. read the existing file, keeping every field as the STRING it is on disk.
   Values are never parsed and re-serialised, so an unchanged row cannot
   change its bytes through a float round trip;
2. fetch only what is strictly newer than the last row, and only CLOSED
   periods — today's daily bar is still forming and a bar counted before it
   closes is lookahead with a slow fuse;
3. write existing rows verbatim, then the new ones;
4. re-run `corpus_prefix.check` AFTER the write. If `history_unchanged`,
   `never_shrank` or `appended_all_strictly_after_t1` is false, **restore the
   original file from memory and fail.** A corpus that fails its own invariant
   is worse than a stale one.

WHAT IT DELIBERATELY WILL NOT TOUCH
===================================
**BTCUSDT only.** ETHUSDT and SOLUSDT carry a documented, tested falsehood —
their manifests declare 1465/4397 rows against 1461/4383/4458 on disk
(`test_each_known_false_claim_is_still_exactly_as_recorded`, EDGE.md §47b).
That enumeration is a finding under test. Refreshing those files would erase it
and the enumeration would go red for the wrong reason, so this tool refuses any
symbol but BTCUSDT unless a human passes `--symbol` explicitly.

**The volume unit swap stays.** 2026-08-18..08-22 carry quote-notional instead
of base volume (5.4e9..3.5e10 against a file median of 227,752). Appending does
not reach those rows and must not. `tests/test_corpus_unit_audit.py` says in its
own docstring that its first assertion is written to FAIL when a human repairs
the data, on purpose. Repairing it as a side effect of a refresh would spend
that signal without anybody reading it.

**The manifest, minimally.** Only BTCUSDT's `rows`/`end` and `fetched_utc`
move. Every other key, and both other symbols, are written back unchanged,
because `test_the_defect_set_is_exactly_four_files` counts the disagreements
and a fifth would hide behind the four.

    python3 tools/refresh_forward_corpus.py            # dry run, writes nothing
    python3 tools/refresh_forward_corpus.py --write
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import gzip
import io
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Sequence, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO)
sys.path.insert(0, HERE)

KLINES_URL = "https://fapi.binance.com/fapi/v1/klines"
FUNDING_URL = "https://fapi.binance.com/fapi/v1/fundingRate"
USER_AGENT = "Mozilla/5.0 (compatible; carry-research/1.0)"

DAY_MS = 86_400_000
PAGE = 1000

#: The only symbol this programme measures. ETH and SOL carry a finding.
DEFAULT_SYMBOL = "BTCUSDT"

LINEAR_DIR = "data/real_linear_1d"
FUNDING_DIR = "data/real_funding"

DAILY_FIELDS = ("time_period_start", "time_period_end", "time_open",
                "time_close", "price_open", "price_high", "price_low",
                "price_close", "volume_traded", "trades_count")
FUNDING_FIELDS = ("funding_time", "funding_time_ms", "symbol", "funding_rate",
                  "mark_price")

ISO = "%Y-%m-%dT%H:%M:%S+00:00"


class RefuseToRefresh(RuntimeError):
    """The append could not be made safely, so it was not made."""


# ---------------------------------------------------------------------------
# venue
# ---------------------------------------------------------------------------

def _get(url: str, params: Dict[str, Any]) -> Any:
    full = f"{url}?{urllib.parse.urlencode(params)}"
    request = urllib.request.Request(full, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def _iso(ms: int) -> str:
    return dt.datetime.fromtimestamp(ms / 1000.0, dt.timezone.utc).strftime(ISO)


def fetch_daily_after(symbol: str, after_ms: int, now_ms: int,
                      getter: Any = None) -> List[Dict[str, str]]:
    """Closed daily bars strictly after `after_ms`, oldest first.

    A bar is CLOSED only when its close time has passed. Binance returns the
    in-progress bar like every other; counting it would be lookahead.
    """
    call = getter or _get
    out: Dict[int, Dict[str, str]] = {}
    cursor = after_ms + DAY_MS
    while cursor < now_ms:
        batch = call(KLINES_URL, {"symbol": symbol, "interval": "1d",
                                  "startTime": cursor, "limit": PAGE})
        if not batch:
            break
        for k in batch:
            open_ms, close_ms = int(k[0]), int(k[6])
            if open_ms <= after_ms:
                continue
            if close_ms >= now_ms:
                continue            # still forming
            out[open_ms] = {
                "time_period_start": _iso(open_ms),
                "time_period_end": _iso(close_ms),
                "time_open": _iso(open_ms),
                "time_close": _iso(close_ms),
                "price_open": str(float(k[1])),
                "price_high": str(float(k[2])),
                "price_low": str(float(k[3])),
                "price_close": str(float(k[4])),
                "volume_traded": str(float(k[5])),
                "trades_count": str(int(k[8]) if k[8] is not None else 0),
            }
        last_open = int(batch[-1][0])
        if last_open + DAY_MS <= cursor:
            break
        cursor = last_open + DAY_MS
        if len(batch) < PAGE:
            break
        time.sleep(0.25)
    return [out[k] for k in sorted(out)]


def fetch_funding_after(symbol: str, after_ms: int, now_ms: int,
                        getter: Any = None) -> List[Dict[str, str]]:
    """Settled funding prints strictly after `after_ms`, oldest first."""
    call = getter or _get
    out: Dict[int, Dict[str, str]] = {}
    cursor = after_ms + 1
    while cursor < now_ms:
        batch = call(FUNDING_URL, {"symbol": symbol, "startTime": cursor,
                                   "limit": PAGE})
        if not batch:
            break
        for row in batch:
            stamp = int(row["fundingTime"])
            if stamp <= after_ms or stamp >= now_ms:
                continue
            mark = row.get("markPrice")
            out[stamp] = {
                "funding_time": _iso(stamp),
                "funding_time_ms": str(stamp),
                "symbol": symbol,
                "funding_rate": str(float(row["fundingRate"])),
                "mark_price": ("" if mark in (None, "") else str(float(mark))),
            }
        last = int(batch[-1]["fundingTime"])
        if last + 1 <= cursor:
            break
        cursor = last + 1
        if len(batch) < PAGE:
            break
        time.sleep(0.25)
    return [out[k] for k in sorted(out)]


# ---------------------------------------------------------------------------
# files
# ---------------------------------------------------------------------------

def read_rows(path: str) -> List[Dict[str, str]]:
    """Every row, with every value kept as the STRING on disk.

    This is the whole safety property of the append. `csv.DictReader` yields
    strings and `csv.DictWriter` writes them back unchanged, so a row that is
    not being modified cannot change its bytes. Parsing `227752.0` into a float
    and re-serialising it is how a "pure append" silently edits history.
    """
    with gzip.open(path, "rt", newline="") as handle:
        return list(csv.DictReader(handle))


def encode(rows: Sequence[Dict[str, str]],
           fields: Sequence[str]) -> bytes:
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=list(fields))
    writer.writeheader()
    for row in rows:
        writer.writerow({k: row.get(k, "") for k in fields})
    return buffer.getvalue().encode("utf-8")


def _last_stamp(rows: Sequence[Dict[str, str]], column: str) -> int:
    if not rows:
        raise RefuseToRefresh("the corpus is empty; this tool appends, it does "
                              "not create")
    value = rows[-1][column]
    if column == "funding_time_ms":
        return int(value)
    return int(dt.datetime.strptime(value, ISO)
               .replace(tzinfo=dt.timezone.utc).timestamp() * 1000)


def _verify(path_rel: str) -> Dict[str, Any]:
    """Re-run the append-only invariant against what is now on disk."""
    import corpus_prefix as cp          # noqa: PLC0415
    check = cp.check(path_rel)
    return {
        "history_unchanged": bool(check.history_unchanged),
        "never_shrank": bool(check.never_shrank),
        "appended_all_strictly_after_t1": bool(
            check.appended_all_strictly_after_t1),
        "pinned_rows": int(check.pinned_rows),
        "rows_on_disk": int(check.rows_on_disk),
        "appended_rows": int(check.appended_rows),
    }


def append_file(*, path_rel: str, fields: Sequence[str], time_column: str,
                new_rows: Sequence[Dict[str, str]], write: bool,
                verify: bool = True) -> Dict[str, Any]:
    """Append `new_rows`, then prove the invariant still holds or roll back."""
    path = os.path.join(REPO, path_rel)
    existing = read_rows(path)
    before = len(existing)

    seen = {row[time_column] for row in existing}
    fresh = [r for r in new_rows if r[time_column] not in seen]
    report: Dict[str, Any] = {
        "file": path_rel,
        "rows_before": before,
        "rows_appended": len(fresh),
        "rows_after": before + len(fresh),
        "first_appended": fresh[0][time_column] if fresh else None,
        "last_appended": fresh[-1][time_column] if fresh else None,
        "written": False,
    }
    if not fresh or not write:
        return report

    with open(path, "rb") as handle:
        original = handle.read()

    payload = encode(list(existing) + list(fresh), fields)
    with gzip.open(path, "wb") as handle:
        handle.write(payload)

    if verify:
        invariant = _verify(path_rel)
        report["invariant"] = invariant
        if not all((invariant["history_unchanged"], invariant["never_shrank"],
                    invariant["appended_all_strictly_after_t1"])):
            with open(path, "wb") as handle:
                handle.write(original)          # restore, byte for byte
            raise RefuseToRefresh(
                f"{path_rel}: the append broke the corpus invariant "
                f"({invariant}); the original file has been restored and "
                "nothing was kept")
    report["written"] = True
    return report


def update_manifest(*, directory: str, symbol: str, rows: int, end: str,
                    write: bool) -> Dict[str, Any]:
    """Move ONLY this symbol's row count and end date. Nothing else.

    ETHUSDT and SOLUSDT entries are written back exactly as read, because their
    disagreement with disk is a recorded finding under test rather than an
    error to tidy.
    """
    path = os.path.join(REPO, directory, "MANIFEST.json")
    with open(path, encoding="utf-8") as handle:
        manifest = json.load(handle)
    entry = manifest["date_range_utc"][symbol]
    was = {"rows": entry["rows"], "end": entry["end"]}
    if write:
        entry["rows"] = int(rows)
        entry["end"] = end
        manifest["fetched_utc"] = dt.datetime.now(dt.timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(manifest, handle, indent=2)
            handle.write("\n")
    return {"manifest": os.path.join(directory, "MANIFEST.json"),
            "symbol": symbol, "was": was,
            "now": {"rows": int(rows), "end": end}, "written": bool(write)}


# ---------------------------------------------------------------------------

def run(*, symbol: str = DEFAULT_SYMBOL, write: bool = False,
        now_ms: Optional[int] = None, daily_getter: Any = None,
        funding_getter: Any = None, verify: bool = True) -> Dict[str, Any]:
    if symbol != DEFAULT_SYMBOL:
        raise RefuseToRefresh(
            f"{symbol} is not {DEFAULT_SYMBOL}. ETHUSDT and SOLUSDT carry a "
            "documented manifest/disk disagreement that is held under test "
            "(EDGE.md §47b); refreshing them would erase the finding rather "
            "than repair it. Pass --symbol deliberately if a human has decided "
            "to retire that enumeration.")

    now = int(time.time() * 1000) if now_ms is None else int(now_ms)
    base = symbol.replace("USDT", "")
    daily_rel = f"{LINEAR_DIR}/ohlcv/BINANCE_LINEAR_{base}_USDT_1D.csv.gz"
    funding_rel = f"{FUNDING_DIR}/funding/BINANCE_LINEAR_{base}_USDT_FUNDING.csv.gz"

    daily_existing = read_rows(os.path.join(REPO, daily_rel))
    funding_existing = read_rows(os.path.join(REPO, funding_rel))

    daily_new = fetch_daily_after(
        symbol, _last_stamp(daily_existing, "time_period_start"), now,
        getter=daily_getter)
    funding_new = fetch_funding_after(
        symbol, _last_stamp(funding_existing, "funding_time_ms"), now,
        getter=funding_getter)

    out: Dict[str, Any] = {
        "tool": "refresh_forward_corpus",
        "symbol": symbol,
        "write": write,
        "observed_utc": _iso(now),
        "daily": append_file(
            path_rel=daily_rel, fields=DAILY_FIELDS,
            time_column="time_period_start", new_rows=daily_new, write=write,
            verify=verify),
        "funding": append_file(
            path_rel=funding_rel, fields=FUNDING_FIELDS,
            time_column="funding_time_ms", new_rows=funding_new, write=write,
            verify=verify),
    }

    if out["daily"]["written"]:
        out["daily_manifest"] = update_manifest(
            directory=LINEAR_DIR, symbol=symbol,
            rows=out["daily"]["rows_after"],
            end=out["daily"]["last_appended"][:10], write=True)
    if out["funding"]["written"]:
        out["funding_manifest"] = update_manifest(
            directory=FUNDING_DIR, symbol=symbol,
            rows=out["funding"]["rows_after"],
            end=_iso(int(out["funding"]["last_appended"]))[:10], write=True)

    out["note"] = (
        "A bigger window is not evidence. This moves flags out of the tail so "
        "the forward ladder can score them; whether any of them becomes a "
        "closed trade, and what it says, is a separate question this tool has "
        "no opinion about.")
    return out


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--symbol", default=DEFAULT_SYMBOL)
    parser.add_argument("--write", action="store_true",
                        help="persist (default: dry run, writes nothing)")
    args = parser.parse_args(argv)
    try:
        report = run(symbol=args.symbol, write=args.write)
    except RefuseToRefresh as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
