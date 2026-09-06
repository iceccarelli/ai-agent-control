#!/usr/bin/env python3
"""Append CLOSED BTCUSDT daily bars and funding prints. Never the open bar.

Track B, in code:
  * closed bars only - a bar is appended iff its closeTime < observed_now
  * funding prints only at-or-before observed_now
  * append-only: the uncompressed bytes of the existing file are asserted to be
    an exact prefix of the new file, and both sha256s are published
  * BTCUSDT only, Binance USDT-M only (www.binance.com/fapi path); any other
    symbol or venue is a refuse, not an option
  * hash-check BEFORE writing: if the existing digest does not match
    --expect-linear-sha256 / --expect-funding-sha256 (when given), refuse
  * dry-run by default; --write to persist

Row formats reproduce the existing corpus columns (CRLF line endings):
  linear : time_period_start,time_period_end,time_open,time_close,
           price_open,price_high,price_low,price_close,volume_traded,trades_count
  funding: funding_time,funding_time_ms,symbol,funding_rate,mark_price

Exit codes: 0 appended or nothing to append; 4 refused (reason printed).
"""
from __future__ import annotations

import argparse
import datetime as dt
import gzip
import hashlib
import json
import os
import sys
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

SYMBOL = "BTCUSDT"
LINEAR_PATH = os.path.join(ROOT, "data", "real_linear_1d", "ohlcv",
                           "BINANCE_LINEAR_BTC_USDT_1D.csv.gz")
FUNDING_PATH = os.path.join(ROOT, "data", "real_funding", "funding",
                            "BINANCE_LINEAR_BTC_USDT_FUNDING.csv.gz")
BASE = "https://www.binance.com/fapi/v1"
DAY_MS = 86_400_000

Fetcher = Callable[[str, Dict[str, Any]], Any]


class Refuse(Exception):
    pass


def default_fetch(path: str, params: Dict[str, Any]) -> Any:
    import urllib.parse
    import urllib.request
    url = f"{BASE}/{path}?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "append-closed-corpus/1"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        if resp.status != 200:
            raise Refuse(f"HTTP {resp.status} from {url}")
        return json.loads(resp.read().decode("utf-8"))


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def read_gz(path: str) -> bytes:
    with gzip.open(path, "rb") as fh:
        return fh.read()


def iso(ms: int) -> str:
    return dt.datetime.fromtimestamp(ms / 1000, dt.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S+00:00")


def last_field(raw: bytes, col: int) -> Optional[str]:
    lines = [ln for ln in raw.decode("utf-8").split("\n") if ln.strip()]
    if len(lines) < 2:
        return None
    return lines[-1].rstrip("\r").split(",")[col]


def _num(s: Any) -> str:
    """Binance sends '77719.00000000'; the corpus stores the shortest exact form."""
    t = str(s)
    if "." in t:
        t = t.rstrip("0").rstrip(".")
    return t or "0"


def closed_linear_rows(fetch: Fetcher, *, after_open_ms: int, now_ms: int
                       ) -> List[Tuple[int, str]]:
    """(openTime, csv_row) for every bar strictly after after_open_ms whose
    closeTime is before now_ms. The open bar is dropped here, by arithmetic,
    not by trust in the endpoint."""
    start = after_open_ms + DAY_MS
    if start >= now_ms:
        return []
    raw = fetch("klines", {"symbol": SYMBOL, "interval": "1d",
                           "startTime": start, "limit": 1000})
    out: List[Tuple[int, str]] = []
    for k in raw:
        open_ms, close_ms = int(k[0]), int(k[6])
        if open_ms <= after_open_ms:
            continue
        if close_ms >= now_ms:
            continue  # the open bar. refused.
        if open_ms % DAY_MS != 0:
            raise Refuse(f"kline openTime {open_ms} is not UTC-midnight aligned")
        row = ",".join([
            iso(open_ms), iso(open_ms + DAY_MS - 1000), iso(open_ms),
            iso(open_ms + DAY_MS - 1000),
            _num(k[1]), _num(k[2]), _num(k[3]), _num(k[4]), _num(k[5]), str(int(k[8])),
        ])
        out.append((open_ms, row))
    for a, b in zip(out, out[1:]):
        if b[0] - a[0] != DAY_MS:
            raise Refuse(f"gap or overlap between {iso(a[0])} and {iso(b[0])}")
    return out


def funding_rows(fetch: Fetcher, *, after_ms: int, now_ms: int) -> List[Tuple[int, str]]:
    raw = fetch("fundingRate", {"symbol": SYMBOL, "startTime": after_ms + 1,
                                "limit": 1000})
    out: List[Tuple[int, str]] = []
    for r in raw:
        t = int(r["fundingTime"])
        if t <= after_ms or t > now_ms or str(r.get("symbol")) != SYMBOL:
            continue
        hour_ms = (t // 3_600_000) * 3_600_000
        out.append((t, ",".join([iso(hour_ms), str(t), SYMBOL,
                                 str(r["fundingRate"]), str(r.get("markPrice", ""))])))
    for a, b in zip(out, out[1:]):
        if b[0] <= a[0]:
            raise Refuse("funding prints not strictly increasing")
    return out


def append(existing: bytes, rows: Sequence[Tuple[int, str]]) -> bytes:
    if not existing.endswith(b"\n"):
        existing += b"\r\n"
    return existing + "".join(r + "\r\n" for _, r in rows).encode("utf-8")


def run(*, observed_at_utc: str, fetch: Fetcher = default_fetch,
        linear_path: str = LINEAR_PATH, funding_path: str = FUNDING_PATH,
        expect_linear_sha256: str = "", expect_funding_sha256: str = "",
        write: bool = False) -> Dict[str, Any]:
    now = dt.datetime.strptime(observed_at_utc, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=dt.timezone.utc)
    now_ms = int(now.timestamp() * 1000)

    lin_before = read_gz(linear_path)
    fun_before = read_gz(funding_path)
    lin_sha_before, fun_sha_before = sha256_bytes(lin_before), sha256_bytes(fun_before)
    if expect_linear_sha256 and expect_linear_sha256 != lin_sha_before:
        raise Refuse(f"linear digest {lin_sha_before} != expected {expect_linear_sha256}")
    if expect_funding_sha256 and expect_funding_sha256 != fun_sha_before:
        raise Refuse(f"funding digest {fun_sha_before} != expected {expect_funding_sha256}")

    last_open_iso = last_field(lin_before, 2)
    last_fund_ms = int(last_field(fun_before, 1) or 0)
    if not last_open_iso:
        raise Refuse("linear corpus has no data rows")
    last_open_ms = int(dt.datetime.strptime(last_open_iso, "%Y-%m-%dT%H:%M:%S+00:00")
                       .replace(tzinfo=dt.timezone.utc).timestamp() * 1000)

    lin_new = closed_linear_rows(fetch, after_open_ms=last_open_ms, now_ms=now_ms)
    fun_new = funding_rows(fetch, after_ms=last_fund_ms, now_ms=now_ms)
    lin_after = append(lin_before, lin_new) if lin_new else lin_before
    fun_after = append(fun_before, fun_new) if fun_new else fun_before
    assert lin_after.startswith(lin_before) and fun_after.startswith(fun_before)

    report: Dict[str, Any] = {
        "tool": "append_closed_corpus", "symbol": SYMBOL, "venue": "binance_usdtm",
        "observed_at_utc": observed_at_utc, "write": write,
        "linear_last_before": last_open_iso[:10],
        "linear_last_after": (iso(lin_new[-1][0])[:10] if lin_new else last_open_iso[:10]),
        "new_closed_bars": [iso(o)[:10] for o, _ in lin_new],
        "refused_open_bar": iso((now_ms // DAY_MS) * DAY_MS)[:10],
        "new_funding_prints": len(fun_new),
        "bars_fabricated": 0,
        "linear_sha256_before": lin_sha_before,
        "linear_sha256_after": sha256_bytes(lin_after),
        "funding_sha256_before": fun_sha_before,
        "funding_sha256_after": sha256_bytes(fun_after),
        "historical_bytes_are_prefix": True,
        "eth_sol_touched": False,
    }
    if write and (lin_new or fun_new):
        for path, data in ((linear_path, lin_after), (funding_path, fun_after)):
            tmp = path + ".tmp"
            with gzip.open(tmp, "wb") as fh:
                fh.write(data)
            os.replace(tmp, path)
    return report


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--observed-at-utc", default=dt.datetime.now(dt.timezone.utc)
                    .strftime("%Y-%m-%dT%H:%M:%SZ"))
    ap.add_argument("--expect-linear-sha256", default="")
    ap.add_argument("--expect-funding-sha256", default="")
    ap.add_argument("--write", action="store_true", help="persist (default dry-run)")
    ap.add_argument("--out", default="")
    args = ap.parse_args(argv)
    try:
        report = run(observed_at_utc=args.observed_at_utc,
                     expect_linear_sha256=args.expect_linear_sha256,
                     expect_funding_sha256=args.expect_funding_sha256,
                     write=args.write)
    except Refuse as exc:
        print("REFUSED:", exc)
        return 4
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2)
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
