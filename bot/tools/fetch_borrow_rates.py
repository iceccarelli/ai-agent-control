#!/usr/bin/env python3
"""The financing leg, as a series instead of a guess.

WHY THIS EXISTS
===============
`borrow_apr` is a CONSTANT at every call site in this tree, and the matrices
sweep it at 0/3/5/8 %/yr. PHASE1_DECISION's entire threshold — "above roughly
4% financing the spread stops clearing its cost of capital" — is stated against
that constant.

The real rate is not flat. Measured over 40,000 hourly observations, the median
USDT lending rate by half-year runs:

    2022H1 1.00   2022H2 1.00   2023H1 2.00   2023H2 3.00   2024H1 5.00
    2024H2 5.00   2025H1 3.00   2025H2 4.00   2026H1 2.50   2026H2 2.80

A book charged a flat 3% across that is charged too much in 2022 and too little
in 2024. The 3/5/8% columns are decoration until a per-period series is charged
against the days a position was actually held.

WHAT IT IS, PRECISELY
=====================
OKX's PUBLIC savings lending-rate history: what a LENDER earns. A borrower pays
MORE — the spread between them is the venue's. So every figure this writes is a
FLOOR on the true cost of financing, and a book that fails to clear its costs
against these rates fails harder against real ones. That direction is stated
wherever the number is used, because a floor quoted as a cost is a cost
understated.

It is also the only deep public series of its kind. Binance's is signed-only
with undocumented depth; Bybit caps at six months; Deribit has no lending
product at all. That is why a venue the book does not trade on is the source
here, and it is a limitation rather than a preference.

WHAT IT CANNOT DO
=================
The floor is 2021-12-14 and it is the VENUE's, not a fetch limit. The carry
corpus reaches 2019-09. So the 2019-09 .. 2021-12 stretch has NO financing data
at any price — and that stretch contains 2021, which carries 58-68% of the
seven-year net (INVENTORY D42). The financed variant cannot be costed over the
period that produces most of its return. No amount of fetching changes that.

    python3 tools/fetch_borrow_rates.py                  # dry run
    python3 tools/fetch_borrow_rates.py --write
    python3 tools/fetch_borrow_rates.py --ccy USDT USDC --write
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
import time
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional

URL = "https://www.okx.com/api/v5/finance/savings/lending-rate-history"
OUT_DIR = "data/real_borrow"

#: The venue's own floor, established by probe rather than assumed: `after` at
#: 2021-12-16 returns 39 rows and every earlier stamp returns zero.
VENUE_FLOOR_UTC = "2021-12-14"

#: Rows per page the endpoint serves.
PAGE = 100

#: Enough pages to walk from today to the floor with room to spare
#: (~33,400 hourly rows / 100). A cap that is reached is reported, never
#: silently treated as the end of the data — that mistake cost a re-probe.
MAX_PAGES = 600

SLEEP_S = 0.1

COLUMNS = ("ts", "utc", "ccy", "rate_annual", "lending_rate", "amount")


class BorrowFetchError(RuntimeError):
    """The series could not be fetched or did not survive its own checks."""


#: OKX refuses the default `Python-urllib/3.12` agent with a 403. Measured:
#: bare urlopen -> 403, the same request with this header -> 200, `requests`
#: -> 200. It is a header, not a block, not a rate limit and not geo. Two of
#: the three urllib fetchers in this tree already send one
#: (`fetch_binance_klines`, `append_closed_corpus`); omitting it was the
#: deviation.
USER_AGENT = "Mozilla/5.0 (compatible; carry-research/1.0)"


def _get(params: Dict[str, Any]) -> List[Dict[str, Any]]:
    url = f"{URL}?{urllib.parse.urlencode(params)}"
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if str(payload.get("code")) != "0":
        raise BorrowFetchError(f"{url} -> code {payload.get('code')} "
                               f"{payload.get('msg')!r}")
    return payload.get("data") or []


def fetch(ccy: str, floor_ms: int, pager: Any = None,
          max_pages: int = MAX_PAGES) -> tuple[List[Dict[str, str]], bool]:
    """Every hourly observation from now back to `floor_ms`, oldest first.

    Returns `(rows, hit_cap)`. `hit_cap` is the honest half: a walk that stops
    because it ran out of pages has NOT reached the end of the data, and
    reporting the two as the same thing is how a 400-page cap got mistaken for
    a 2022-02 inception.
    """
    call = pager or _get
    seen: Dict[int, Dict[str, str]] = {}
    cursor: Optional[int] = None
    pages = 0
    while pages < max_pages:
        params: Dict[str, Any] = {"ccy": ccy, "limit": PAGE}
        if cursor is not None:
            params["after"] = cursor
        batch = call(params)
        if not batch:
            return _ordered(seen), False
        pages += 1
        oldest = None
        for row in batch:
            stamp = int(row["ts"])
            oldest = stamp if oldest is None else min(oldest, stamp)
            if stamp in seen:
                continue
            seen[stamp] = {
                "ts": str(stamp),
                "utc": dt.datetime.fromtimestamp(
                    stamp / 1000.0, dt.timezone.utc).strftime(
                        "%Y-%m-%dT%H:%M:%SZ"),
                "ccy": str(row.get("ccy", ccy)),
                "rate_annual": str(row["rate"]),
                "lending_rate": str(row.get("lendingRate", "")),
                "amount": str(row.get("amt", "")),
            }
        if oldest is None or oldest <= floor_ms:
            return _ordered(seen), False
        if cursor is not None and oldest >= cursor:
            return _ordered(seen), False        # not advancing; stop
        cursor = oldest
        if SLEEP_S:
            time.sleep(SLEEP_S)
    return _ordered(seen), True


def _ordered(seen: Dict[int, Dict[str, str]]) -> List[Dict[str, str]]:
    return [seen[k] for k in sorted(seen)]


def validate(rows: List[Dict[str, str]], ccy: str) -> None:
    """Refuse a series this tool would not want charged to a backtest."""
    if len(rows) < 1000:
        raise BorrowFetchError(f"{ccy}: only {len(rows)} rows")
    stamps = [int(r["ts"]) for r in rows]
    if stamps != sorted(stamps):
        raise BorrowFetchError(f"{ccy}: timestamps are not increasing")
    if len(set(stamps)) != len(stamps):
        raise BorrowFetchError(f"{ccy}: duplicate timestamps")
    for row in rows:
        rate = float(row["rate_annual"])
        if not (rate == rate) or rate < 0:      # NaN or negative
            raise BorrowFetchError(f"{ccy}: unusable rate {row['rate_annual']!r}"
                                   f" at {row['utc']}")


def encode(rows: List[Dict[str, str]]) -> bytes:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=list(COLUMNS))
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    return buffer.getvalue().encode("utf-8")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def run(repo: str = ".", *, ccy: str = "USDT", since: str = VENUE_FLOOR_UTC,
        write: bool = False, pager: Any = None,
        max_pages: int = MAX_PAGES) -> Dict[str, Any]:
    floor_ms = int(dt.datetime.fromisoformat(since).replace(
        tzinfo=dt.timezone.utc).timestamp() * 1000)
    rows, hit_cap = fetch(ccy, floor_ms, pager=pager, max_pages=max_pages)
    validate(rows, ccy)
    plain = encode(rows)
    name = f"OKX_LENDING_RATE_{ccy}_1H.csv.gz"
    path = os.path.join(repo, OUT_DIR, name)

    report = {
        "tool": "fetch_borrow_rates", "ccy": ccy, "rows": len(rows),
        "first_utc": rows[0]["utc"], "last_utc": rows[-1]["utc"],
        "requested_since": since,
        "reached_the_venue_floor": rows[0]["utc"][:10] <= VENUE_FLOOR_UTC,
        "hit_page_cap": hit_cap,
        "sha256_uncompressed": sha256_bytes(plain),
        "file": os.path.join(OUT_DIR, name),
        "write": write,
        "what_this_is": ("OKX PUBLIC savings LENDING rate — what a lender "
                         "earns. A borrower pays more, so every value is a "
                         "FLOOR on the true cost of financing."),
    }
    if hit_cap:
        report["warning"] = (
            "the walk stopped at its own page cap, NOT at the end of the data; "
            "the first_utc above is where paging stopped")
    if write:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with gzip.open(path, "wb") as handle:
            handle.write(plain)
        report["written"] = True
        report["manifest"] = write_manifest(os.path.join(repo, OUT_DIR))
    return report


def write_manifest(folder: str) -> str:
    """No corpus without provenance — the rule 0056 had to learn twice."""
    files: Dict[str, Any] = {}
    for name in sorted(os.listdir(folder)):
        if not name.endswith(".csv.gz"):
            continue
        full = os.path.join(folder, name)
        with open(full, "rb") as blob:
            raw = blob.read()
        plain = gzip.decompress(raw)
        rows = list(csv.DictReader(io.StringIO(plain.decode("utf-8"))))
        files[name] = {
            "kind": "borrow_rate",
            "interval_seconds": 3600,
            "rows": len(rows),
            "size_bytes": len(raw),
            "sha256": sha256_bytes(raw),
            "sha256_uncompressed": sha256_bytes(plain),
            "first_timestamp": rows[0]["utc"] if rows else None,
            "last_timestamp": rows[-1]["utc"] if rows else None,
        }
    manifest = {
        "synthetic": False,
        "asset_class": "borrow_rate",
        "venue": "okx",
        "interval": "1h",
        "source": "public_rest_savings_lending_rate_history",
        "endpoint": URL,
        "venue_floor_utc": VENUE_FLOOR_UTC,
        "fetched_utc": dt.datetime.now(dt.timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"),
        "files": files,
        "notes": (
            "LENDING rate: what a lender earns. A borrower pays more, so these "
            "are a FLOOR on the cost of financing. OKX is not the venue this "
            "book trades on; it is used because it is the only deep PUBLIC "
            "series of its kind (Binance signed-only and undocumented depth, "
            "Bybit six months, Deribit no lending product). The venue floor is "
            "2021-12-14, so the 2019-09..2021-12 stretch of the carry corpus "
            "has no financing data at any price."),
    }
    path = os.path.join(folder, "MANIFEST.json")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(manifest, handle, indent=2, sort_keys=True)
    return path


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repo", default=".")
    parser.add_argument("--ccy", nargs="+", default=["USDT"])
    parser.add_argument("--since", default=VENUE_FLOOR_UTC,
                        help=f"how far back to walk (default {VENUE_FLOOR_UTC}, "
                             "the venue's own floor)")
    parser.add_argument("--write", action="store_true",
                        help="persist (default: dry run, writes nothing)")
    args = parser.parse_args(argv)

    failed = False
    for ccy in args.ccy:
        try:
            report = run(args.repo, ccy=ccy, since=args.since,
                         write=args.write)
        except BorrowFetchError as exc:
            print(f"REFUSED {ccy}: {exc}", file=sys.stderr)
            failed = True
            continue
        print(json.dumps(report, indent=2))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
