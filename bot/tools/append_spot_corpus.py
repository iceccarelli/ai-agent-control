#!/usr/bin/env python3
"""Append closed spot days. Never rewrite. Never write the open one.

WHY THIS EXISTS
===============
`fetch_binance_klines.py` REWRITES the whole spot file on every run. Three
consecutive runs against unchanged history produced three different digests:

    0c013cfeb290b5bf  ->  315ef254024a1818  ->  8ef481aa3298771c

Same days, different bytes, because the last row is TODAY — an in-progress bar
whose OHLC moves every time you ask. The spot corpus is therefore not
reproducible, has no append-only guarantee, and carries no prefix hash.

The linear and funding corpora have had all three since slice 60. Spot never
did, because until 0020 it was Bitstamp and nothing important read it. It is
now HALF THE BASIS, and the basis is the term that decides whether a carry
trade closes positive.

WHAT THIS DOES
==============
Reads what is already on disk, fetches, and writes ONLY days after the current
last row — never today, never a day already present. Then verifies the old
bytes are a byte-prefix of the new file and refuses to keep the result if they
are not.

    prefix holds   -> commit, publish both digests
    prefix broken  -> restore, report, exit non-zero

A rewrite that happens to produce the same numbers is still a rewrite: it means
the file is whatever the last fetch believed, not an accumulating record. The
prefix check is the difference between a corpus and a cache.

    python3 tools/append_spot_corpus.py              # dry run
    python3 tools/append_spot_corpus.py --write
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
import shutil
import sys
import urllib.request
from typing import Any, Dict, List, Optional

SPOT_PATH = "data/real_spot_btc/ohlcv/BINANCE_SPOT_BTC_USDT_1D.csv.gz"
ENDPOINT = "https://api.binance.com/api/v3/klines"
HEADER = ["time_period_start", "time_period_end", "time_open", "time_close",
          "price_open", "price_high", "price_low", "price_close",
          "volume_traded", "trades_count"]


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_rows(path: str) -> List[Dict[str, str]]:
    if not os.path.exists(path):
        return []
    with gzip.open(path, "rt") as handle:
        return list(csv.DictReader(handle))


def last_day(rows: List[Dict[str, str]]) -> Optional[dt.date]:
    if not rows:
        return None
    return dt.datetime.fromisoformat(
        rows[-1]["time_period_start"].replace("Z", "+00:00")).date()


def fetch(symbol: str, start_ms: int) -> List[List[Any]]:
    out: List[List[Any]] = []
    cursor = start_ms
    while True:
        url = (f"{ENDPOINT}?symbol={symbol}&interval=1d"
               f"&startTime={cursor}&limit=1000")
        with urllib.request.urlopen(url, timeout=30) as response:
            batch = json.loads(response.read())
        if not batch:
            break
        out.extend(batch)
        if len(batch) < 1000:
            break
        cursor = int(batch[-1][0]) + 1
    return out


def to_row(kline: List[Any]) -> Dict[str, str]:
    start = dt.datetime.fromtimestamp(int(kline[0]) / 1000.0, dt.timezone.utc)
    end = start + dt.timedelta(days=1)
    fmt = "%Y-%m-%dT%H:%M:%S.0000000Z"
    return {"time_period_start": start.strftime(fmt),
            "time_period_end": end.strftime(fmt),
            "time_open": start.strftime(fmt),
            "time_close": (end - dt.timedelta(seconds=1)).strftime(fmt),
            "price_open": kline[1], "price_high": kline[2],
            "price_low": kline[3], "price_close": kline[4],
            "volume_traded": kline[5], "trades_count": kline[8]}


def encode(rows: List[Dict[str, str]]) -> bytes:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=HEADER, lineterminator="\r\n")
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue().encode("utf-8")


def run(repo: str = ".", *, symbol: str = "BTCUSDT",
        write: bool = False, today: Optional[dt.date] = None,
        klines: Optional[List[List[Any]]] = None) -> Dict[str, Any]:
    today = today or dt.datetime.now(dt.timezone.utc).date()
    path = os.path.join(repo, SPOT_PATH)
    rows = read_rows(path)
    have = {r["time_period_start"][:10] for r in rows}
    last = last_day(rows)

    start_ms = int(dt.datetime.combine(
        (last + dt.timedelta(days=1)) if last else dt.date(2022, 8, 10),
        dt.time(), dt.timezone.utc).timestamp() * 1000)

    raw = fetch(symbol, start_ms) if klines is None else klines
    candidates = [to_row(k) for k in raw]

    # Two refusals, and they are different refusals. TODAY has not closed, so
    # its bar is an in-progress quote wearing a close's clothes. A day already
    # present must not be rewritten even if the venue now reports it slightly
    # differently, because a corpus that updates its own history is a cache.
    appended = [r for r in candidates
                if r["time_period_start"][:10] not in have
                and dt.date.fromisoformat(r["time_period_start"][:10]) < today]
    refused_open = [r["time_period_start"][:10] for r in candidates
                    if dt.date.fromisoformat(r["time_period_start"][:10]) >= today]
    refused_dupe = [r["time_period_start"][:10] for r in candidates
                    if r["time_period_start"][:10] in have]

    before = open(path, "rb").read() if os.path.exists(path) else b""
    before_plain = encode(rows)
    after_plain = encode(rows + appended)
    prefix_holds = after_plain.startswith(before_plain)

    report = {
        "tool": "append_spot_corpus", "symbol": symbol, "write": write,
        "last_before": str(last), "new_closed_days": [
            r["time_period_start"][:10] for r in appended],
        "refused_open_day": refused_open[-1] if refused_open else None,
        "refused_already_present": len(refused_dupe),
        "rows_before": len(rows), "rows_after": len(rows) + len(appended),
        "uncompressed_sha256_before": sha256_bytes(before_plain),
        "uncompressed_sha256_after": sha256_bytes(after_plain),
        "historical_bytes_are_prefix": prefix_holds,
        "bars_fabricated": 0,
    }

    if not prefix_holds:
        report["error"] = ("APPEND WOULD REWRITE HISTORY — refusing. A corpus "
                           "that updates its own past is a cache, not a record.")
        return report

    if write and appended:
        backup = path + ".bak"
        if os.path.exists(path):
            shutil.copy2(path, backup)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with gzip.open(path, "wb") as handle:
            handle.write(after_plain)
        # Verify what actually landed, not what we meant to write.
        if encode(read_rows(path)) != after_plain:
            if os.path.exists(backup):
                shutil.move(backup, path)
            report["error"] = "WRITE VERIFY FAILED — restored"
            return report
        if os.path.exists(backup):
            os.remove(backup)
        report["written"] = True
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repo", default=".")
    parser.add_argument("--symbol", default="BTCUSDT")
    parser.add_argument("--write", action="store_true",
                        help="persist (default: dry run)")
    args = parser.parse_args(argv)
    report = run(args.repo, symbol=args.symbol, write=args.write)
    print(json.dumps(report, indent=2))
    return 1 if report.get("error") else 0


if __name__ == "__main__":
    sys.exit(main())
