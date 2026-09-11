#!/usr/bin/env python3
"""Promote the 0028 Bybit study set to a corpus: same venue, settlement clock.

WHY
===
Every corpus the backtester read before this was Binance, and the book trades
on Bybit. 0028 fetched Bybit BTCUSDT spot 4h, linear 4h and funding once, for
a venue study, and filed them under research/ as a STUDY SET that "feeds no
gate and no backtest". That was the right status for a study. It is the wrong
status for the only data in this tree where

    spot, perp and funding are the SAME venue the broker trades on,
    in the SAME quote currency,
    at a resolution that LANDS ON THE FUNDING CLOCK (4h bars open at
    00/04/08/12/16/20 UTC; settlements are 00/08/16).

This tool converts those bytes, deterministically and without editing a single
price, into data/real_bybit_btc_4h/ with a MANIFEST that pins every file by
sha256 and names the source file it came from by sha256. The study set is not
moved or modified.

WHAT IT DROPS, AND SAYS SO
==========================
  an OPEN bar      a 4h bar whose close is after the study set's own clock.
                   Every file in the set that carries a server time is stamped
                   2026-09-09 10:43:52-53 UTC; the last 4h bar in both kline
                   files opened 08:00 and closes 12:00. It had run for 2h44m
                   of 4h. It is DROPPED — never written as a close — and the
                   manifest records the drop. (The same rule carry_backtest
                   applies to the daily corpus: a window that has not finished
                   has no close.)

WHAT IT REFUSES
===============
  a GAP            consecutive bars not exactly 4h apart
  a DUPLICATE      two bars with one open time
  OFF-CLOCK        a funding print not on an 8h boundary

It writes nothing if any of these is found.

    python3 tools/promote_bybit_study_set.py            # dry run: report only
    python3 tools/promote_bybit_study_set.py --write
    python3 tools/promote_bybit_study_set.py --check    # regenerate, compare
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import gzip
import hashlib
import io
import json
import os
import sys
import tempfile
from typing import Any, Dict, List, Optional, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

STUDY = "research/exchange_study/bybit"
OUT = "data/real_bybit_btc_4h"
BAR_MS = 4 * 3600 * 1000
FUNDING_MS = 8 * 3600 * 1000

OHLCV_HEADER = ["time_period_start", "time_period_end", "time_open",
                "time_close", "price_open", "price_high", "price_low",
                "price_close", "volume_traded", "trades_count"]
FUNDING_HEADER = ["funding_time", "funding_time_ms", "symbol", "funding_rate",
                  "mark_price"]

#: (study file, corpus file, product) — the 4h klines.
KLINES = (
    ("spot_BTCUSDT_240.json.gz", "ohlcv/BYBIT_SPOT_BTC_USDT_4H.csv.gz", "spot"),
    ("linear_BTCUSDT_240.json.gz", "ohlcv/BYBIT_LINEAR_BTC_USDT_4H.csv.gz",
     "linear"),
)
FUNDING = ("funding_BTCUSDT.json.gz",
           "funding/BYBIT_LINEAR_BTC_USDT_FUNDING.csv.gz")
#: The study set's own clock: the linear ticker snapshot fetched with it.
CLOCK = "tickers_linear.json.gz"


class Refused(RuntimeError):
    """The study set cannot be promoted as it stands."""


def iso_utc(ms: int) -> str:
    """CoinAPI form, seven fractional digits, trailing Z."""
    t = dt.datetime.fromtimestamp(ms / 1000.0, dt.timezone.utc)
    return t.strftime("%Y-%m-%dT%H:%M:%S") + ".0000000Z"


def _load(path: str) -> Any:
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        return json.load(handle)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def study_clock_ms(study: str) -> int:
    """When the study set was observed, by its own ticker snapshot."""
    blob = _load(os.path.join(study, CLOCK))
    return int(blob["time"])


def kline_rows(raw: List[List[str]], *, clock_ms: int,
               dropped: Optional[List[str]] = None) -> List[List[str]]:
    """Bybit kline arrays -> CoinAPI rows, ascending, closed bars only.

    Open bars are removed and their open times appended to `dropped`."""
    bars = sorted(((int(r[0]), r) for r in raw), key=lambda x: x[0])
    opens = [b[0] for b in bars]
    if len(set(opens)) != len(opens):
        raise Refused("duplicate 4h bar open times")
    gaps = [(a, b) for a, b in zip(opens, opens[1:]) if b - a != BAR_MS]
    if gaps:
        raise Refused(f"{len(gaps)} gap(s) between 4h bars, first at "
                      f"{iso_utc(gaps[0][0])}")
    open_bars = [o for o in opens if o + BAR_MS > clock_ms]
    if dropped is not None:
        dropped.extend(iso_utc(o) for o in open_bars)
    rows = []
    for start, r in bars:
        if start + BAR_MS > clock_ms:
            continue
        rows.append([iso_utc(start), iso_utc(start + BAR_MS), iso_utc(start),
                     iso_utc(start + BAR_MS - 1000), r[1], r[2], r[3], r[4],
                     r[5], ""])
    return rows


def funding_rows(raw: List[Dict[str, str]]) -> List[List[str]]:
    prints = sorted(raw, key=lambda r: int(r["fundingRateTimestamp"]))
    stamps = [int(r["fundingRateTimestamp"]) for r in prints]
    if len(set(stamps)) != len(stamps):
        raise Refused("duplicate funding prints")
    off = [s for s in stamps if s % FUNDING_MS != 0]
    if off:
        raise Refused(f"{len(off)} funding print(s) off the 8h clock, first "
                      f"{off[0]}")
    gaps = [(a, b) for a, b in zip(stamps, stamps[1:]) if b - a != FUNDING_MS]
    if gaps:
        raise Refused(f"{len(gaps)} gap(s) in funding prints, first at "
                      f"{iso_utc(gaps[0][0])}")
    return [[dt.datetime.fromtimestamp(int(r["fundingRateTimestamp"]) / 1000,
                                       dt.timezone.utc).isoformat(),
             str(int(r["fundingRateTimestamp"])), r["symbol"],
             r["fundingRate"], ""] for r in prints]


def _csv_bytes(header: List[str], rows: List[List[str]]) -> bytes:
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(header)
    writer.writerows(rows)
    return buf.getvalue().encode("utf-8")


def _gzip(data: bytes) -> bytes:
    """Deterministic: no filename, mtime 0."""
    out = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=out, mtime=0,
                       compresslevel=9) as handle:
        handle.write(data)
    return out.getvalue()


def build(repo: str = ROOT) -> Tuple[Dict[str, bytes], Dict[str, Any]]:
    """Every output file (relpath -> gz bytes) and the manifest. Writes nothing."""
    study = os.path.join(repo, STUDY)
    clock = study_clock_ms(study)
    files: Dict[str, bytes] = {}
    entries: Dict[str, Any] = {}
    sources: Dict[str, Any] = {}

    def add(rel: str, header: List[str], rows: List[List[str]], kind: str,
            src: str, first: str, last: str, interval_s: int) -> None:
        raw = _csv_bytes(header, rows)
        gz = _gzip(raw)
        files[rel] = gz
        entries[rel] = {"kind": kind, "rows": len(rows),
                        "interval_seconds": interval_s,
                        "sha256": _sha(gz), "sha256_uncompressed": _sha(raw),
                        "size_bytes": len(gz), "first_timestamp": first,
                        "last_timestamp": last, "derived_from": src}

    for src, rel, _product in KLINES:
        path = os.path.join(study, src)
        with open(path, "rb") as handle:
            sources[src] = _sha(handle.read())
        dropped: List[str] = []
        rows = kline_rows(_load(path), clock_ms=clock, dropped=dropped)
        add(rel, OHLCV_HEADER, rows, "ohlcv", f"{STUDY}/{src}",
            rows[0][0], rows[-1][0], BAR_MS // 1000)
        entries[rel]["open_bars_dropped"] = dropped

    src, rel = FUNDING
    path = os.path.join(study, src)
    with open(path, "rb") as handle:
        sources[src] = _sha(handle.read())
    rows = funding_rows(_load(path))
    add(rel, FUNDING_HEADER, rows, "funding", f"{STUDY}/{src}",
        rows[0][0], rows[-1][0], FUNDING_MS // 1000)

    with open(os.path.join(study, CLOCK), "rb") as handle:
        sources[CLOCK] = _sha(handle.read())

    manifest = {
        "synthetic": False,
        "schema": "coinapi_flat/1 (ohlcv); binance_funding/1 (funding)",
        "venue": "bybit",
        "symbols": ["BTCUSDT"],
        "products": ["spot", "linear"],
        "bar_seconds": BAR_MS // 1000,
        "generator": "tools/promote_bybit_study_set.py",
        "source": {
            "study_set": STUDY,
            "fetched_by": "0028 venue study (tools/venue_study.py)",
            "endpoints": ["GET /v5/market/kline category=spot interval=240",
                          "GET /v5/market/kline category=linear interval=240",
                          "GET /v5/market/funding/history category=linear"],
            "observed_utc": iso_utc(clock),
            "observed_by": f"{STUDY}/{CLOCK} (result.time)",
            "note": ("the study set records no per-file fetch time; its "
                     "ticker snapshot is the latest instant the set can vouch "
                     "for, and every bar must have closed before it"),
            "sha256_of_source_files": sources,
        },
        "price_at_settlement": ("the CLOSE of the 4h bar that ENDS at the "
                                "settlement instant (opens 4h before it) — "
                                "the same convention as "
                                "tools/fetch_settlement_klines.py (0027)"),
        "trades_count": "not published by Bybit klines; left blank",
        "files": entries,
        "warning": ("REAL exchange data. A frozen record, like every corpus in "
                    "this tree (docs/human/CORPUS_POLICY.md). Reads are "
                    "recorded in artifacts/data_read_ledger.json."),
    }
    return files, manifest


def write(repo: str, files: Dict[str, bytes], manifest: Dict[str, Any]) -> None:
    out = os.path.join(repo, OUT)
    for rel, blob in files.items():
        path = os.path.join(out, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as handle:
            handle.write(blob)
    with open(os.path.join(out, "MANIFEST.json"), "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=1, sort_keys=True)
        fh.write("\n")


def check(repo: str = ROOT) -> List[str]:
    """Regenerate from the study set and compare CONTENT with what is on disk."""
    files, manifest = build(repo)
    problems = []
    on_disk_path = os.path.join(repo, OUT, "MANIFEST.json")
    if not os.path.exists(on_disk_path):
        return [f"{OUT}/MANIFEST.json missing"]
    with open(on_disk_path, encoding="utf-8") as fh:
        on_disk = json.load(fh)
    for rel, entry in manifest["files"].items():
        have = on_disk.get("files", {}).get(rel)
        if have is None:
            problems.append(f"{rel}: not in the committed manifest")
            continue
        if have["sha256_uncompressed"] != entry["sha256_uncompressed"]:
            problems.append(f"{rel}: regenerated content differs")
        path = os.path.join(repo, OUT, rel)
        with open(path, "rb") as handle:
            raw = gzip.decompress(handle.read())
        if _sha(raw) != entry["sha256_uncompressed"]:
            problems.append(f"{rel}: file on disk differs from regeneration")
    return problems


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", default=ROOT)
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args(argv)
    if args.check:
        problems = check(args.repo)
        for p in problems:
            print("DIFFERS:", p)
        print("CHECK OK" if not problems else "CHECK FAILED")
        return 0 if not problems else 1
    try:
        files, manifest = build(args.repo)
    except Refused as exc:
        print(f"REFUSED: {exc}")
        return 2
    for rel, entry in sorted(manifest["files"].items()):
        print(f"{rel:48s} rows={entry['rows']:5d}  "
              f"{entry['first_timestamp'][:16]} .. {entry['last_timestamp'][:16]}"
              f"  sha256(raw) {entry['sha256_uncompressed']}"
              + (f"  dropped open bar(s) {entry['open_bars_dropped']}"
                 if entry.get("open_bars_dropped") else ""))
    print(f"observed: {manifest['source']['observed_utc']}")
    if args.write:
        write(args.repo, files, manifest)
        print(f"wrote {OUT}/")
    else:
        print("dry run — nothing written (use --write)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
