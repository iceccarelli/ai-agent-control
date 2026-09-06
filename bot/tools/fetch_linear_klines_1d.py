#!/usr/bin/env python3
"""Fetch Binance USDT-M daily klines. CoinAPI-shaped columns for stack alignment."""
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

BASE = "https://fapi.binance.com/fapi/v1/klines"
LIMIT = 1500
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
            "interval": "1d",
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
        for k in batch:
            open_ms = int(k[0])
            close_ms = int(k[6])
            out.append(
                {
                    "time_period_start": datetime.fromtimestamp(
                        open_ms / 1000.0, tz=timezone.utc
                    ).strftime("%Y-%m-%dT%H:%M:%S+00:00"),
                    "time_period_end": datetime.fromtimestamp(
                        close_ms / 1000.0, tz=timezone.utc
                    ).strftime("%Y-%m-%dT%H:%M:%S+00:00"),
                    "time_open": datetime.fromtimestamp(
                        open_ms / 1000.0, tz=timezone.utc
                    ).strftime("%Y-%m-%dT%H:%M:%S+00:00"),
                    "time_close": datetime.fromtimestamp(
                        close_ms / 1000.0, tz=timezone.utc
                    ).strftime("%Y-%m-%dT%H:%M:%S+00:00"),
                    "price_open": float(k[1]),
                    "price_high": float(k[2]),
                    "price_low": float(k[3]),
                    "price_close": float(k[4]),
                    "volume_traded": float(k[5]),
                    "trades_count": int(k[8]) if k[8] is not None else 0,
                }
            )
        last_open = int(batch[-1][0])
        nxt = last_open + 86_400_000
        if nxt <= cursor:
            break
        cursor = nxt
        if len(batch) < LIMIT:
            break
        time.sleep(SLEEP)
    by_t = {r["time_period_start"]: r for r in out}
    return [by_t[k] for k in sorted(by_t)]


def write_gz(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "time_period_start",
        "time_period_end",
        "time_open",
        "time_close",
        "price_open",
        "price_high",
        "price_low",
        "price_close",
        "volume_traded",
        "trades_count",
    ]
    with gzip.open(path, "wt", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in rows:
            w.writerow(r)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--symbols", nargs="+", default=["BTCUSDT", "ETHUSDT", "SOLUSDT"])
    p.add_argument("--years", type=float, default=4.0)
    p.add_argument("--out", default="data/real_linear_1d")
    args = p.parse_args()

    end = datetime.now(timezone.utc)
    start = datetime.fromtimestamp(end.timestamp() - args.years * 365.25 * 24 * 3600, tz=timezone.utc)
    start_ms, end_ms = utc_ms(start), utc_ms(end)
    out_root = Path(args.out)
    ohlcv = out_root / "ohlcv"
    ranges = {}

    for sym in args.symbols:
        print(f"fetching linear 1d {sym} ...")
        rows = fetch_symbol(sym, start_ms, end_ms)
        if len(rows) < 500:
            print(f"ERROR: {sym} only {len(rows)} daily bars", file=sys.stderr)
            return 1
        for r in rows:
            if not (r["price_high"] >= r["price_low"] and r["price_open"] > 0 and r["price_close"] > 0):
                print(f"ERROR: bad OHLC {sym} {r['time_period_start']}", file=sys.stderr)
                return 1
        base = sym.replace("USDT", "")
        fname = f"BINANCE_LINEAR_{base}_USDT_1D.csv.gz"
        path = ohlcv / fname
        write_gz(path, rows)
        ranges[sym] = {
            "start": rows[0]["time_period_start"][:10],
            "end": rows[-1]["time_period_start"][:10],
            "rows": len(rows),
        }
        print(f"  {len(rows)} bars  {ranges[sym]['start']} -> {ranges[sym]['end']}  -> {path}")

    manifest = {
        "synthetic": False,
        "asset_class": "ohlcv",
        "venue": "binance_usdtm",
        "market": "linear",
        "symbols": list(args.symbols),
        "interval": "1d",
        "source": "public_rest_klines",
        "endpoint": "GET /fapi/v1/klines",
        "fetched_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "date_range_utc": ranges,
        "notes": "No synthetic bars. Pagination complete. Stage-1 barrier series for funding_carry_fade_v1.",
    }
    man_path = out_root / "MANIFEST.json"
    man_path.write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"wrote {man_path}")
    print("Eligibility is NOT edge. Timing-skill research remains CLOSED until Stage-1 POSITIVE.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
