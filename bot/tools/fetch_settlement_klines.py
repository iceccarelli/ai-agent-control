#!/usr/bin/env python3
"""8h klines on the funding clock, for both legs.

WHY
===
The book decides on DAILY CLOSES. Funding settles at 00:00, 08:00 and 16:00 UTC.
Those are not the same instant, and the basis is not the same number at each.

`carry_backtest` reports a mean entry basis of +6 bps and a mean exit of +29 bps
measured at daily closes. What the P&L actually paid is the basis AT
SETTLEMENT — the moment the funding transfer happens and the moment a real book
would have to be hedged. Nobody has looked at that number, and basis is the term
that turned -$4,772 against $42,843 of gross.

An 8h kline on Binance is aligned to 00/08/16 UTC, which is exactly the funding
clock. Its CLOSE is the price at settlement.

WHAT
====
Fetches 8h klines for the perp and the spot leg into a separate directory, with
the same discipline the daily corpora have: closed bars only, append-only,
prefix-verified, dry-run by default.

The IN-PROGRESS 8h bar is refused. A window that has not finished has no close,
and pricing a settlement that has not happened is the same error as writing
today's daily bar.

    python3 tools/fetch_settlement_klines.py               # dry run
    python3 tools/fetch_settlement_klines.py --write
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
import urllib.request
from typing import Any, Dict, List, Optional

PERP_ENDPOINT = "https://www.binance.com/fapi/v1/klines"
SPOT_ENDPOINT = "https://api.binance.com/api/v3/klines"
OUT_DIR = "data/real_settlement_8h"
HEADER = ["open_time_ms", "open_utc", "price_open", "price_high", "price_low",
          "price_close", "volume_traded"]

#: Funding settles every eight hours. A bar whose window includes now has not
#: closed and therefore has no settlement price.
WINDOW_S = 8 * 3600


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_rows(path: str) -> List[Dict[str, str]]:
    if not os.path.exists(path):
        return []
    with gzip.open(path, "rt") as handle:
        return list(csv.DictReader(handle))


def encode(rows: List[Dict[str, str]]) -> bytes:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=HEADER, lineterminator="\r\n")
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue().encode("utf-8")


def fetch(endpoint: str, symbol: str, start_ms: int) -> List[List[Any]]:
    out: List[List[Any]] = []
    cursor = start_ms
    while True:
        url = (f"{endpoint}?symbol={symbol}&interval=8h"
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
    open_ms = int(kline[0])
    return {"open_time_ms": str(open_ms),
            "open_utc": dt.datetime.fromtimestamp(
                open_ms / 1000.0, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "price_open": kline[1], "price_high": kline[2],
            "price_low": kline[3], "price_close": kline[4],
            "volume_traded": kline[5]}


def run(repo: str = ".", *, leg: str = "perp", symbol: str = "BTCUSDT",
        write: bool = False, now_s: Optional[float] = None,
        klines: Optional[List[List[Any]]] = None) -> Dict[str, Any]:
    now_s = now_s if now_s is not None else dt.datetime.now(
        dt.timezone.utc).timestamp()
    path = os.path.join(repo, OUT_DIR, f"BINANCE_{leg.upper()}_{symbol}_8H.csv.gz")
    rows = read_rows(path)
    have = {r["open_time_ms"] for r in rows}
    start_ms = (int(rows[-1]["open_time_ms"]) + 1) if rows else \
        int(dt.datetime(2022, 8, 10, tzinfo=dt.timezone.utc).timestamp() * 1000)

    endpoint = PERP_ENDPOINT if leg == "perp" else SPOT_ENDPOINT
    raw = fetch(endpoint, symbol, start_ms) if klines is None else klines
    candidates = [to_row(k) for k in raw]

    # A window that includes NOW has not closed. Pricing a settlement that has
    # not happened is the same error as writing today's daily bar as a close.
    def closed(row: Dict[str, str]) -> bool:
        return (int(row["open_time_ms"]) / 1000.0) + WINDOW_S <= now_s

    appended = [r for r in candidates
                if r["open_time_ms"] not in have and closed(r)]
    refused_open = [r["open_utc"] for r in candidates if not closed(r)]

    before_plain = encode(rows)
    after_plain = encode(rows + appended)
    prefix_holds = after_plain.startswith(before_plain)

    report = {"tool": "fetch_settlement_klines", "leg": leg, "symbol": symbol,
              "write": write, "rows_before": len(rows),
              "rows_after": len(rows) + len(appended),
              "new_windows": len(appended),
              "first_new": appended[0]["open_utc"] if appended else None,
              "last_new": appended[-1]["open_utc"] if appended else None,
              "refused_open_windows": refused_open[-2:],
              "uncompressed_sha256_before": sha256_bytes(before_plain),
              "uncompressed_sha256_after": sha256_bytes(after_plain),
              "historical_bytes_are_prefix": prefix_holds,
              "bars_fabricated": 0}

    if not prefix_holds:
        report["error"] = "APPEND WOULD REWRITE HISTORY — refusing"
        return report
    if write and appended:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with gzip.open(path, "wb") as handle:
            handle.write(after_plain)
        report["written"] = True
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repo", default=".")
    parser.add_argument("--symbol", default="BTCUSDT")
    parser.add_argument("--legs", nargs="+", default=["perp", "spot"])
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args(argv)
    failed = False
    for leg in args.legs:
        report = run(args.repo, leg=leg, symbol=args.symbol, write=args.write)
        print(json.dumps(report, indent=2))
        failed = failed or bool(report.get("error"))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
