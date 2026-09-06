#!/usr/bin/env python3
"""Fetch Binance USDT-M funding history. No forward-fill. synthetic: false only."""
from __future__ import annotations

import argparse
import csv
import gzip
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

BASE = "https://fapi.binance.com/fapi/v1/fundingRate"
LIMIT = 1000
SLEEP = 0.25


def utc_ms(dt: datetime) -> int:
    return int(dt.replace(tzinfo=timezone.utc).timestamp() * 1000)


def fetch_symbol(symbol: str, start_ms: int, end_ms: int) -> list[dict]:
    out: list[dict] = []
    cursor = start_ms
    session = requests.Session()
    while cursor < end_ms:
        params = {
            "symbol": symbol,
            "startTime": cursor,
            "endTime": end_ms,
            "limit": LIMIT,
        }
        r = session.get(BASE, params=params, timeout=30)
        if r.status_code == 429:
            time.sleep(2.0)
            continue
        r.raise_for_status()
        batch = r.json()
        if not batch:
            break
        for row in batch:
            out.append(
                {
                    "funding_time": datetime.fromtimestamp(
                        row["fundingTime"] / 1000.0, tz=timezone.utc
                    ).strftime("%Y-%m-%dT%H:%M:%S+00:00"),
                    "funding_time_ms": int(row["fundingTime"]),
                    "symbol": symbol,
                    "funding_rate": float(row["fundingRate"]),
                    "mark_price": float(row["markPrice"]) if row.get("markPrice") not in (None, "") else "",
                }
            )
        last = int(batch[-1]["fundingTime"])
        nxt = last + 1
        if nxt <= cursor:
            break
        cursor = nxt
        if len(batch) < LIMIT:
            break
        time.sleep(SLEEP)
    # de-dupe + sort
    by_ts = {r["funding_time_ms"]: r for r in out}
    rows = [by_ts[k] for k in sorted(by_ts)]
    return rows


def write_gz(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = ["funding_time", "funding_time_ms", "symbol", "funding_rate", "mark_price"]
    with gzip.open(path, "wt", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in rows:
            w.writerow(r)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--symbols", nargs="+", default=["BTCUSDT", "ETHUSDT", "SOLUSDT"])
    p.add_argument("--years", type=float, default=4.0)
    p.add_argument("--out", default="data/real_funding")
    args = p.parse_args()

    end = datetime.now(timezone.utc)
    start = datetime.fromtimestamp(end.timestamp() - args.years * 365.25 * 24 * 3600, tz=timezone.utc)
    start_ms, end_ms = utc_ms(start), utc_ms(end)
    out_root = Path(args.out)
    fund_dir = out_root / "funding"
    ranges = {}

    for sym in args.symbols:
        print(f"fetching funding {sym} ...")
        rows = fetch_symbol(sym, start_ms, end_ms)
        if len(rows) < 100:
            print(f"ERROR: {sym} only {len(rows)} rows", file=sys.stderr)
            return 1
        # integrity
        ts = [r["funding_time_ms"] for r in rows]
        if ts != sorted(ts) or len(ts) != len(set(ts)):
            print(f"ERROR: {sym} time integrity failed", file=sys.stderr)
            return 1
        for r in rows:
            if not (r["funding_rate"] == r["funding_rate"]):  # NaN
                print(f"ERROR: NaN rate {sym}", file=sys.stderr)
                return 1
        fname = f"BINANCE_LINEAR_{sym.replace('USDT', '_USDT')}_FUNDING.csv.gz"
        # BTCUSDT -> BINANCE_LINEAR_BTC_USDT_FUNDING.csv.gz
        base = sym.replace("USDT", "")
        fname = f"BINANCE_LINEAR_{base}_USDT_FUNDING.csv.gz"
        path = fund_dir / fname
        write_gz(path, rows)
        ranges[sym] = {
            "start": rows[0]["funding_time"][:10],
            "end": rows[-1]["funding_time"][:10],
            "rows": len(rows),
        }
        print(f"  {len(rows)} rows  {ranges[sym]['start']} -> {ranges[sym]['end']}  -> {path}")

    manifest = {
        "synthetic": False,
        "asset_class": "funding",
        "venue": "binance_usdtm",
        "symbols": list(args.symbols),
        "interval": "8h",
        "source": "public_rest_funding_history",
        "endpoint": "GET /fapi/v1/fundingRate",
        "fetched_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "date_range_utc": ranges,
        "notes": "No forward-fill. No synthetic rates. Pagination complete. Stage-1 only.",
    }
    man_path = out_root / "MANIFEST.json"
    man_path.write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"wrote {man_path}")
    print("Eligibility is NOT edge. Timing-skill research remains CLOSED until Stage-1 POSITIVE.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
