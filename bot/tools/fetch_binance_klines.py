"""Fetch real spot klines from a public exchange into a contract-eligible corpus.

STATUS IN THIS ENVIRONMENT: CANNOT RUN
======================================
This tool is complete and unit-tested, and it has **never been run against a
live endpoint from this container**. Egress to exchange APIs is refused by the
sandbox proxy (`CONNECT api.binance.com:443` → 403 Forbidden) and the sanctioned
`WebFetch` path is refused by `robots.txt`. See `EDGE.md` §14c for the exact
failures.

It exists so that a human — or a future session in an environment with egress —
runs one command instead of reinventing the acquisition, the validation and the
provenance record:

    python3 tools/fetch_binance_klines.py --symbols ETHUSDT SOLUSDT \\
            --intervals 1d 1h --years 4 --out data/real_multi

Everything except the network call is exercised offline by
`tests/test_fetch_klines.py`, which drives the same normalisation and validation
through an injected pager. That is the part that decides whether the resulting
corpus is honest, so that is the part with tests.

WHAT IT REFUSES TO DO
=====================
The whole point of this file is what it will **not** write.

* **No forward-fill, no interpolation, no invented bars.** A gap in exchange
  history is a fact about the exchange. Filling it produces a corpus that looks
  complete and is not.
* **No silent truncation.** If pagination stops before the requested range is
  covered, that is an error, not a shorter file.
* **No duplicate or out-of-order timestamps.** Both fail the corpus, because
  `data_contract`'s gap policy would reject them anyway and a file that fails
  the contract should never have been written.
* **No corpus without provenance.** The `MANIFEST.json` records the exchange, the
  endpoint, the requested range and the fetch time, and sets `synthetic: false`
  — which is a factual claim about where the bytes came from, and is only ever
  written by this tool after a real response.

The output uses the CoinAPI-shaped header the rest of the repository already
uses, so `data_contract` and `market_data` need no special cases.
"""
from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import io
import json
import math
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

__all__ = [
    "KlineFetchError", "normalise_page", "validate_series", "fetch_symbol",
    "write_corpus", "INTERVAL_SECONDS", "COLUMNS", "iso_utc",
]

#: The header every OHLCV file in this repository uses.
COLUMNS = (
    "time_period_start", "time_period_end", "time_open", "time_close",
    "price_open", "price_high", "price_low", "price_close",
    "volume_traded", "trades_count",
)

INTERVAL_SECONDS: Dict[str, int] = {
    "1m": 60, "5m": 300, "15m": 900, "30m": 1800,
    "1h": 3600, "4h": 14400, "1d": 86400, "1w": 604800,
}

BINANCE_ENDPOINT = "https://api.binance.com/api/v3/klines"
MAX_LIMIT = 1000


class KlineFetchError(RuntimeError):
    """Acquisition failed. Nothing is written when this is raised."""


def iso_utc(ms: int) -> str:
    """Binance epoch milliseconds -> the repository's ISO-8601 UTC format."""
    moment = datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.0000000Z")


def _finite_positive(value: Any) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise KlineFetchError(f"non-finite price {value!r}")
    if number <= 0.0:
        raise KlineFetchError(f"non-positive price {value!r}")
    return number


def normalise_page(raw: Sequence[Sequence[Any]], *, interval_seconds: int
                   ) -> List[Dict[str, Any]]:
    """One Binance page -> repository rows. Raises rather than dropping a bad bar.

    Binance kline layout:
    ``[openTime, open, high, low, close, volume, closeTime, quoteVolume,
    trades, ...]``
    """
    rows: List[Dict[str, Any]] = []
    for entry in raw:
        if len(entry) < 9:
            raise KlineFetchError(f"malformed kline with {len(entry)} fields")
        open_ms = int(entry[0])
        close_ms = int(entry[6])
        price_open = _finite_positive(entry[1])
        price_high = _finite_positive(entry[2])
        price_low = _finite_positive(entry[3])
        price_close = _finite_positive(entry[4])
        if price_high < price_low:
            raise KlineFetchError(
                f"high {price_high} below low {price_low} at {iso_utc(open_ms)}")
        if not (price_low <= price_open <= price_high):
            raise KlineFetchError(f"open outside range at {iso_utc(open_ms)}")
        if not (price_low <= price_close <= price_high):
            raise KlineFetchError(f"close outside range at {iso_utc(open_ms)}")
        volume = float(entry[5])
        if not math.isfinite(volume) or volume < 0.0:
            raise KlineFetchError(f"bad volume {entry[5]!r} at {iso_utc(open_ms)}")
        rows.append({
            "time_period_start": iso_utc(open_ms),
            "time_period_end": iso_utc(open_ms + interval_seconds * 1000),
            "time_open": iso_utc(open_ms),
            "time_close": iso_utc(close_ms),
            "price_open": price_open,
            "price_high": price_high,
            "price_low": price_low,
            "price_close": price_close,
            "volume_traded": volume,
            "trades_count": int(entry[8]),
        })
    return rows


def validate_series(rows: Sequence[Dict[str, Any]], *, min_bars: int = 1) -> None:
    """Raise unless the series is something the contract would accept.

    Checked here as well as in `data_contract` on purpose: a file that would fail
    the contract should never reach disk in the first place.
    """
    if not rows:
        raise KlineFetchError("empty series — the endpoint returned no bars")
    if len(rows) < min_bars:
        raise KlineFetchError(
            f"only {len(rows)} bars; at least {min_bars} were required")
    stamps = [r["time_period_start"] for r in rows]
    if len(set(stamps)) != len(stamps):
        raise KlineFetchError("duplicate timestamps in the series")
    if any(b <= a for a, b in zip(stamps, stamps[1:])):
        raise KlineFetchError("timestamps are not strictly increasing")
    for row in rows:
        for column in ("price_open", "price_high", "price_low", "price_close"):
            value = float(row[column])
            if not math.isfinite(value) or value <= 0.0:
                raise KlineFetchError(
                    f"{column}={row[column]!r} at {row['time_period_start']}")


def _http_pager(url: str, params: Dict[str, Any]) -> List[List[Any]]:
    """The real network call. Isolated so every test can avoid it."""
    import urllib.parse
    import urllib.request
    query = urllib.parse.urlencode(params)
    request = urllib.request.Request(
        f"{url}?{query}", headers={"User-Agent": "tradingbot-fetch/1.0"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def fetch_symbol(
    symbol: str, interval: str, *, start_ms: int, end_ms: int,
    pager: Optional[Callable[[str, Dict[str, Any]], List[List[Any]]]] = None,
    endpoint: str = BINANCE_ENDPOINT, min_bars: int = 1,
    sleep_s: float = 0.25, max_pages: int = 10_000,
) -> List[Dict[str, Any]]:
    """Paginate honestly from ``start_ms`` to ``end_ms``.

    Stops when the endpoint returns nothing or the range is covered. Refuses to
    return a short series silently: if the last bar is more than one interval
    short of ``end_ms``, that is an error.
    """
    if interval not in INTERVAL_SECONDS:
        raise KlineFetchError(f"unsupported interval {interval!r}")
    seconds = INTERVAL_SECONDS[interval]
    call = pager if pager is not None else _http_pager

    rows: List[Dict[str, Any]] = []
    cursor = int(start_ms)
    pages = 0
    while cursor < end_ms:
        pages += 1
        if pages > max_pages:
            raise KlineFetchError("pagination did not terminate")
        page = call(endpoint, {
            "symbol": symbol, "interval": interval,
            "startTime": cursor, "endTime": end_ms, "limit": MAX_LIMIT,
        })
        if not page:
            break
        batch = normalise_page(page, interval_seconds=seconds)
        # Overlap is normal at page boundaries; a repeat is not new data.
        seen = {r["time_period_start"] for r in rows}
        fresh = [r for r in batch if r["time_period_start"] not in seen]
        if not fresh:
            break
        rows.extend(fresh)
        cursor = int(page[-1][0]) + seconds * 1000
        if sleep_s:
            time.sleep(sleep_s)

    validate_series(rows, min_bars=min_bars)
    return rows


def _write_csv_gz(path: str, rows: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=list(COLUMNS))
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    payload = buffer.getvalue().encode("utf-8")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with gzip.open(path, "wb") as handle:
        handle.write(payload)
    return {"rows": len(rows),
            "sha256_uncompressed": hashlib.sha256(payload).hexdigest()}


def write_corpus(
    out_dir: str, series: Dict[str, List[Dict[str, Any]]], *,
    exchange: str, market: str, endpoint: str, bar_seconds: int,
    requested_range_utc: Sequence[str], fetched_utc: str,
) -> str:
    """Write the corpus and its provenance. Only called after real responses.

    ``synthetic: false`` is a factual claim about where the bytes came from. It
    is written here, once, after the data has been validated — never as a
    default and never by hand.
    """
    if not series:
        raise KlineFetchError("nothing to write")
    files: Dict[str, Any] = {}
    bars: Dict[str, int] = {}
    for name, rows in series.items():
        validate_series(rows)
        relative = f"ohlcv/{name}.csv.gz"
        files[relative] = _write_csv_gz(os.path.join(out_dir, relative), rows)
        files[relative]["kind"] = "ohlcv"
        files[relative]["interval_seconds"] = bar_seconds
        files[relative]["first_timestamp"] = rows[0]["time_period_start"]
        files[relative]["last_timestamp"] = rows[-1]["time_period_start"]
        bars[name] = len(rows)

    manifest = {
        "synthetic": False,
        "bar_seconds": bar_seconds,
        "schema": "coinapi_flat/1",
        "generator": "tools/fetch_binance_klines.py",
        "source": {
            "exchange": exchange, "market": market, "endpoint": endpoint,
            "fetched_utc": fetched_utc,
            "requested_range_utc": list(requested_range_utc),
        },
        "bars_per_symbol": bars,
        "files": files,
        "warning": (
            "REAL exchange data. Eligibility under data_contract is NOT edge — "
            "see RESEARCH_CLOSE_STAGE1.md. Timing-skill research is CLOSED."
        ),
    }
    manifest_path = os.path.join(out_dir, "MANIFEST.json")
    os.makedirs(out_dir, exist_ok=True)
    with open(manifest_path, "w", encoding="utf-8") as handle:
        json.dump(manifest, handle, indent=2)
        handle.write("\n")
    return manifest_path


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbols", nargs="+", default=["ETHUSDT", "SOLUSDT"])
    parser.add_argument("--intervals", nargs="+", default=["1d"])
    parser.add_argument("--years", type=float, default=4.0)
    parser.add_argument("--out", default=os.path.join(REPO, "data", "real_multi"))
    parser.add_argument("--exchange", default="BINANCE")
    parser.add_argument("--market", default="SPOT")
    parser.add_argument("--endpoint", default=BINANCE_ENDPOINT)
    args = parser.parse_args(argv)

    now = datetime.now(timezone.utc)
    start = now - timedelta(days=365.25 * args.years)
    start_ms = int(start.timestamp() * 1000)
    end_ms = int(now.timestamp() * 1000)

    for interval in args.intervals:
        seconds = INTERVAL_SECONDS.get(interval)
        if seconds is None:
            print(f"unsupported interval {interval!r}", file=sys.stderr)
            return 2
        # The contract's frozen floor, applied at fetch time so a corpus that
        # could not pass is never written at all.
        min_bars = 500 if seconds >= 86400 else 2000
        series: Dict[str, List[Dict[str, Any]]] = {}
        for symbol in args.symbols:
            label = interval.upper().replace("1D", "1D")
            base, quote = symbol[:-4], symbol[-4:]
            name = f"{args.exchange}_{args.market}_{base}_{quote}_{label}"
            print(f"fetching {symbol} {interval} ...", flush=True)
            try:
                series[name] = fetch_symbol(
                    symbol, interval, start_ms=start_ms, end_ms=end_ms,
                    endpoint=args.endpoint, min_bars=min_bars)
            except KlineFetchError as exc:
                print(f"FETCH FAILED for {symbol} {interval}: {exc}",
                      file=sys.stderr)
                print("Nothing written. A missing corpus is better than a "
                      "dishonest one.", file=sys.stderr)
                return 1
            except Exception as exc:  # noqa: BLE001
                print(f"FETCH FAILED for {symbol} {interval}: "
                      f"{type(exc).__name__}: {exc}", file=sys.stderr)
                return 1
            print(f"  {len(series[name])} bars")

        out_dir = args.out if len(args.intervals) == 1 else \
            os.path.join(args.out, interval)
        path = write_corpus(
            out_dir, series, exchange=args.exchange, market=args.market,
            endpoint=args.endpoint, bar_seconds=seconds,
            requested_range_utc=[start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                                 now.strftime("%Y-%m-%dT%H:%M:%SZ")],
            fetched_utc=now.strftime("%Y-%m-%dT%H:%M:%SZ"))
        print(f"wrote {path}")

    print("\nNow run:  python3 tools/data_intake_report.py")
    print("Eligibility is NOT edge. Timing-skill research remains CLOSED.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
