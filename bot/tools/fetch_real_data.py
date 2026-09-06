#!/usr/bin/env python3
"""Build a corpus of REAL exchange data, in the schema this codebase already reads.

Why this tool exists
====================
Everything in `data/` is synthetic, and every number derived from it measures
the harness rather than any edge. That was acceptable while the machinery was
being built; it is not acceptable as the input to a trained policy. A model
fitted to a generated price path learns the generator.

This tool replaces that input with real market data from an openly licensed
source, converted into the exact CoinAPI-shaped layout `market_data.load_corpus`
expects, with a manifest that records where every byte came from.

The source
----------
    https://github.com/ff137/bitstamp-btcusd-minute-data      (MIT licensed)

Bitstamp BTC/USD, **1-minute OHLC, 2012-01-01 to 2025-01-07**, 6,846,600 rows,
published by the repository as having no missing minutes, no duplicates and no
nulls. It covers both stress periods this project cares about: the 2020-03 crash
and the whole of the 2022 bear.

Three honest caveats, stated here because they change what the results mean:

1. **It is Bitstamp BTC/USD, not Bybit BTCUSDT.** Different venue, different
   matching engine, different fee schedule, different liquidity. The price
   series is genuine and the shape of the market is genuine; the *execution*
   assumptions in the backtest are Bybit's. Filenames say `BITSTAMP_SPOT_BTC_USD`
   so this can never be mistaken for Bybit data.
2. **There is no order book.** This source is OHLC only. The liquidity gate
   therefore abstains on this corpus, and `load_corpus` says so in its notes.
   Real multi-year L2 archives are a paid product; per the migration plan they
   are not worth buying until a model on OHLCV alone shows positive expectancy
   under realistic costs.
3. **`trades_count` is not available** and is written as 0 rather than invented.
   Nothing in this codebase reads it.

Resampling
----------
1-minute bars are aggregated into the requested interval by the only correct
rule: open = first open, high = max high, low = min low, close = last close,
volume = sum. Buckets are aligned to the UTC epoch, and a bucket is emitted only
if at least one source minute fell in it — a gap stays a gap. Filling it with
the previous close would manufacture a bar that never traded, and the
backtester's intrabar stop logic reads exactly those extremes.

Usage
-----
    python3 tools/fetch_real_data.py                       # BTC/USD, 1h, from 2018
    python3 tools/fetch_real_data.py --from 2019-01-01 --interval 1h
    python3 tools/fetch_real_data.py --source /path/to/btcusd_1min.csv.gz
    python3 tools/fetch_real_data.py --verify               # check an existing corpus

Then:

    python3 tools/run_evaluation.py --data-dir data/real
"""
from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
from typing import Dict, Iterator, List, Optional, Sequence, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import market_data as md  # noqa: E402

# DATA-READ LEDGER (slice 78). Installed BEFORE the first load_corpus call so
# that every corpus this tool reads is recorded, and a future holdout can be
# certified untouched by tools/reserved_holdout.py. Reading is reading: a
# negative result steers the next hypothesis exactly as a positive one does,
# which is how slice 57 ended up "out of sample" on bytes slice 55 had read.
try:
    import reserved_holdout as _read_ledger
    _read_ledger.install()
except Exception as _ledger_exc:  # noqa: BLE001 - never blocks a measurement
    # NOT a silent pass: the reason is bound and the flag is legible. An
    # unrecorded read is a real loss (a future holdout cannot be certified),
    # but it must not take the measurement down with it.
    _read_ledger = None
    _READ_LEDGER_UNAVAILABLE = repr(_ledger_exc)



REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_OUT = os.path.join(REPO, "data", "real")

SOURCE_REPO = "https://github.com/ff137/bitstamp-btcusd-minute-data.git"
SOURCE_FILE = "data/historical/btcusd_bitstamp_1min_2012-2025.csv.gz"
SOURCE_LICENCE = "MIT"
SOURCE_EXCHANGE = "BITSTAMP"
SOURCE_BASE = "BTC"
SOURCE_QUOTE = "USD"

#: Intervals this tool will produce, in seconds. Anything below the source
#: resolution is refused rather than interpolated.
INTERVALS: Dict[str, int] = {
    "1m": 60, "5m": 300, "15m": 900, "30m": 1800,
    "1h": 3600, "4h": 14400, "1d": 86400,
}

OHLCV_COLUMNS = (
    "time_period_start", "time_period_end", "time_open", "time_close",
    "price_open", "price_high", "price_low", "price_close",
    "volume_traded", "trades_count",
)


# ---------------------------------------------------------------------------
# acquisition
# ---------------------------------------------------------------------------


def clone_source(destination: str) -> str:
    """Shallow-clone the source repository and return the path to the archive.

    A shallow clone, because 13 years of history in this repository is ~240 MB
    and none of it beyond the tip is wanted. Failures are raised, not warned
    about: a partial download that silently produces a shorter corpus is worse
    than no corpus, because the shorter one still produces numbers.
    """
    print(f"cloning {SOURCE_REPO} (shallow) …", flush=True)
    subprocess.run(
        ["git", "clone", "--depth", "1", "--quiet", SOURCE_REPO, destination],
        check=True,
    )
    archive = os.path.join(destination, SOURCE_FILE)
    if not os.path.exists(archive):
        raise SystemExit(
            f"the source repository no longer contains {SOURCE_FILE}. "
            "Its layout has changed; check the repository before trusting any "
            "corpus this tool produces."
        )
    return archive


def source_digest(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


# ---------------------------------------------------------------------------
# conversion
# ---------------------------------------------------------------------------


def read_minutes(path: str) -> Iterator[Tuple[int, float, float, float, float, float]]:
    """Yield ``(epoch_seconds, open, high, low, close, volume)`` from the source.

    Rows that cannot be parsed are **raised on**, not skipped. This is a
    one-time conversion of a file that its publisher documents as clean; if that
    stops being true, the right response is to find out why, not to drop rows
    and carry on with a corpus of unknown completeness.
    """
    opener = gzip.open if path.endswith(".gz") else open
    with opener(path, "rt", newline="") as handle:  # type: ignore[operator]
        reader = csv.reader(handle)
        header = next(reader, None)
        if header is None:
            raise SystemExit(f"{path} is empty")
        expected = ["timestamp", "open", "high", "low", "close", "volume"]
        if [h.strip().lower() for h in header] != expected:
            raise SystemExit(
                f"{path}: unexpected header {header}; expected {expected}. "
                "Refusing to guess a column mapping."
            )
        for lineno, row in enumerate(reader, start=2):
            if not row:
                continue
            try:
                yield (
                    int(float(row[0])), float(row[1]), float(row[2]),
                    float(row[3]), float(row[4]), float(row[5]),
                )
            except (ValueError, IndexError) as exc:
                raise SystemExit(f"{path}:{lineno}: unparseable row {row!r}: {exc}")


def resample(
    minutes: Iterator[Tuple[int, float, float, float, float, float]],
    *,
    seconds: int,
    start_epoch: Optional[int],
    end_epoch: Optional[int],
) -> Tuple[List[Sequence[object]], Dict[str, int]]:
    """Aggregate 1-minute bars into ``seconds`` buckets aligned to the UTC epoch.

    Returns ``(rows, stats)``. ``rows`` are in the CoinAPI OHLCV column order.

    A bucket is emitted only if at least one source minute landed in it. Empty
    buckets are counted and reported, never filled. The count matters: a corpus
    with 4% missing hours is usable and a corpus with 40% missing hours is a
    different market, and the only way to tell them apart is to be told.
    """
    rows: List[Sequence[object]] = []
    stats = {"minutes_read": 0, "minutes_used": 0, "buckets": 0,
             "gaps": 0, "zero_volume_buckets": 0}

    bucket_key: Optional[int] = None
    o = h = l = c = 0.0
    volume = 0.0
    first_ts = last_ts = 0
    previous_key: Optional[int] = None

    def flush() -> None:
        nonlocal previous_key
        if bucket_key is None:
            return
        if previous_key is not None:
            missing = (bucket_key - previous_key) // seconds - 1
            if missing > 0:
                stats["gaps"] += missing
        previous_key = bucket_key
        stats["buckets"] += 1
        if volume <= 0.0:
            stats["zero_volume_buckets"] += 1
        rows.append((
            md.format_timestamp(bucket_key * 1_000_000),
            md.format_timestamp((bucket_key + seconds) * 1_000_000),
            md.format_timestamp(first_ts * 1_000_000),
            md.format_timestamp(last_ts * 1_000_000),
            _fmt(o), _fmt(h), _fmt(l), _fmt(c), _fmt(volume),
            # Not available from this source. Written as 0 rather than
            # invented; nothing in this codebase reads it.
            0,
        ))

    for ts, m_open, m_high, m_low, m_close, m_volume in minutes:
        stats["minutes_read"] += 1
        if start_epoch is not None and ts < start_epoch:
            continue
        if end_epoch is not None and ts >= end_epoch:
            continue
        stats["minutes_used"] += 1
        key = (ts // seconds) * seconds
        if key != bucket_key:
            flush()
            bucket_key = key
            o, h, l, c = m_open, m_high, m_low, m_close
            volume = m_volume
            first_ts = last_ts = ts
        else:
            h = max(h, m_high)
            l = min(l, m_low)
            c = m_close
            volume += m_volume
            last_ts = ts
    flush()
    return rows, stats


def _fmt(value: float) -> str:
    """Nine decimal places, matching the corpus convention, without an exponent.

    ``repr(1e-08)`` is ``'1e-08'``, which parses back fine in Python and not at
    all in half the tools a CSV ends up in. Fixed notation, trailing zeros
    stripped.
    """
    text = f"{md.normalize_price(value):.9f}".rstrip("0").rstrip(".")
    return text or "0"


def write_csv_gz(path: str, header: Sequence[str],
                 rows: Sequence[Sequence[object]]) -> Dict[str, object]:
    """Write a deterministic gzip: no mtime, no filename in the header."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(header)
    writer.writerows(rows)
    raw = buffer.getvalue().encode("utf-8")

    with open(path, "wb") as handle:
        # mtime=0 so the same input produces the same bytes; otherwise the
        # manifest hash changes on every run and verification means nothing.
        with gzip.GzipFile(fileobj=handle, mode="wb", mtime=0) as gz:
            gz.write(raw)
    with open(path, "rb") as handle:
        blob = handle.read()
    return {
        "rows": len(rows),
        "size_bytes": len(blob),
        "sha256": hashlib.sha256(blob).hexdigest(),
        "sha256_uncompressed": hashlib.sha256(raw).hexdigest(),
        "kind": "ohlcv",
        "data_kind": "ohlcv",
    }


# ---------------------------------------------------------------------------
# the corpus
# ---------------------------------------------------------------------------


def build(args) -> int:
    seconds = INTERVALS[args.interval]
    start_epoch = _parse_day(args.date_from)
    end_epoch = _parse_day(args.date_to)

    workdir: Optional[str] = None
    try:
        if args.source:
            archive = args.source
            provenance = f"local file {os.path.abspath(archive)}"
        else:
            workdir = tempfile.mkdtemp(prefix="realdata-")
            archive = clone_source(workdir)
            provenance = SOURCE_REPO

        digest = source_digest(archive)
        print(f"source sha256 {digest[:16]}…", flush=True)

        rows, stats = resample(
            read_minutes(archive), seconds=seconds,
            start_epoch=start_epoch, end_epoch=end_epoch,
        )
    finally:
        if workdir:
            shutil.rmtree(workdir, ignore_errors=True)

    if not rows:
        raise SystemExit("no rows in the requested range; nothing written")

    symbol_stub = f"{SOURCE_EXCHANGE}_SPOT_{SOURCE_BASE}_{SOURCE_QUOTE}"
    suffix = {"1h": "_1H", "4h": "_4H", "1d": "_1D", "1m": "_1M",
              "5m": "_5M", "15m": "_15M", "30m": "_30M"}[args.interval]
    relpath = f"ohlcv/{symbol_stub}{suffix}.csv.gz"
    out_path = os.path.join(args.out, relpath)

    entry = write_csv_gz(out_path, OHLCV_COLUMNS, rows)
    entry["interval_seconds"] = seconds

    coverage = _coverage(rows, seconds, stats)
    manifest = {
        "schema": "coinapi-flat-files/1",
        # The single most important field in this file. `market_data` reads it
        # and every report prints it.
        "synthetic": False,
        "warning": (
            "REAL market data from a third-party archive. It is Bitstamp "
            "BTC/USD, NOT Bybit BTCUSDT: the price series is genuine, the "
            "execution assumptions in the backtest are Bybit's. There is no "
            "order book in this corpus, so the liquidity gate abstains."
        ),
        "generator": "tools/fetch_real_data.py",
        "generator_version": 1,
        "source": {
            "provenance": provenance,
            "file": SOURCE_FILE if not args.source else os.path.basename(archive),
            "licence": SOURCE_LICENCE,
            "sha256": digest,
            "exchange": SOURCE_EXCHANGE,
            "instrument": f"{SOURCE_BASE}/{SOURCE_QUOTE}",
            "native_resolution": "1m",
        },
        "bar_seconds": seconds,
        "bars_per_symbol": len(rows),
        "first_timestamp": rows[0][0],
        "last_timestamp": rows[-1][1],
        "coverage": coverage,
        "files": {relpath: entry},
    }
    _write_manifest(args.out, manifest)
    _write_readme(args.out, manifest)

    print()
    print(f"wrote {out_path}")
    print(f"  bars              : {len(rows):,}")
    print(f"  range             : {rows[0][0]} .. {rows[-1][1]}")
    print(f"  missing buckets   : {stats['gaps']:,} ({coverage['completeness']:.4%} complete)")
    print(f"  zero-volume bars  : {stats['zero_volume_buckets']:,}")
    for period, present in coverage["stress_periods"].items():
        print(f"  {period:<18}: {'PRESENT' if present else 'ABSENT'}")
    print()
    print("verifying with the loader the backtester uses …")
    load = md.load_ohlcv(out_path, expected_interval_seconds=seconds)
    print(f"  accepted {len(load.bars):,} bars, "
          f"rejected {len(load.rejections)}: {load.rejection_reasons or 'none'}")
    report = md.verify_manifest(args.out)
    print(f"  manifest: {'OK' if not report.problems else report.problems}")
    print()
    print(f"next: python3 tools/run_evaluation.py --data-dir {args.out}")
    return 0 if not load.rejections and not report.problems else 1


def _coverage(rows, seconds: int, stats: Dict[str, int]) -> Dict[str, object]:
    """Say what the corpus covers, including whether the stress periods are in it.

    A dataset drawn only from calm conditions produces a model that has never
    seen the day it will be judged on, so "does it contain 2020-03" is not a
    nice-to-have field — it is the difference between an evaluation and a
    reassurance.
    """
    first_us = md.parse_timestamp(str(rows[0][0]))
    last_us = md.parse_timestamp(str(rows[-1][1]))
    span_buckets = max(1, (last_us - first_us) // (seconds * 1_000_000))
    stress = {
        "2020-03 covid crash": ("2020-03-01", "2020-04-01"),
        "2022 bear (full year)": ("2022-01-01", "2023-01-01"),
        "2021 bull": ("2021-01-01", "2022-01-01"),
    }
    present = {}
    for label, (begin, end) in stress.items():
        begin_us = md.parse_timestamp(f"{begin}T00:00:00.0000000Z")
        end_us = md.parse_timestamp(f"{end}T00:00:00.0000000Z")
        present[label] = bool(first_us <= begin_us and last_us >= end_us)
    return {
        "bars": len(rows),
        "expected_bars": int(span_buckets),
        "missing_buckets": stats["gaps"],
        "completeness": len(rows) / float(span_buckets),
        "zero_volume_bars": stats["zero_volume_buckets"],
        "stress_periods": present,
    }


def _write_manifest(root: str, manifest: Dict[str, object]) -> None:
    os.makedirs(root, exist_ok=True)
    path = os.path.join(root, "MANIFEST.json")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(manifest, handle, indent=1, sort_keys=True)
        handle.write("\n")


def _write_readme(root: str, manifest: Dict[str, object]) -> None:
    source = manifest["source"]           # type: ignore[index]
    coverage = manifest["coverage"]       # type: ignore[index]
    stress = "\n".join(
        f"- {label}: **{'present' if ok else 'ABSENT'}**"
        for label, ok in coverage["stress_periods"].items()   # type: ignore[index]
    )
    text = f"""# REAL market data — {source['exchange']} {source['instrument']}

**This is real exchange data, not synthetic.** It is the corpus to evaluate and
train against. `../README.md` describes the synthetic corpus, which exists only
to exercise the machinery offline.

## Provenance

| | |
|---|---|
| Source | {source['provenance']} |
| File | `{source['file']}` |
| Licence | {source['licence']} |
| Source sha256 | `{source['sha256']}` |
| Exchange | {source['exchange']} |
| Instrument | {source['instrument']} |
| Native resolution | {source['native_resolution']} |
| Produced by | `tools/fetch_real_data.py` |

## What it covers

- **{coverage['bars']:,} bars** at {manifest['bar_seconds']}s
- {manifest['first_timestamp']} to {manifest['last_timestamp']}
- completeness **{coverage['completeness']:.4%}** ({coverage['missing_buckets']:,} missing buckets, left as gaps and never filled)

{stress}

## Three things this corpus is not

1. **It is not Bybit.** Bitstamp BTC/USD is a different venue with different
   liquidity and a different fee schedule. The price series is genuine; the
   execution assumptions applied to it in the backtest are Bybit's.
2. **It has no order book.** OHLC only. `_gate_book_liquidity` abstains here,
   and `load_corpus` says so in its notes. Real multi-year L2 archives are a
   paid product and are not worth buying before a model on OHLCV alone shows
   positive expectancy under realistic costs.
3. **`trades_count` is 0**, because the source does not carry it. It is not
   invented, and nothing in this codebase reads it.

## Rebuild

```bash
python3 tools/fetch_real_data.py --from 2018-01-01 --interval 1h
python3 -c "import market_data; print(market_data.verify_manifest('data/real'))"
python3 tools/run_evaluation.py --data-dir data/real
```
"""
    with open(os.path.join(root, "README.md"), "w", encoding="utf-8") as handle:
        handle.write(text)


def _parse_day(text: Optional[str]) -> Optional[int]:
    if not text:
        return None
    micros = md.parse_timestamp(f"{text}T00:00:00.0000000Z")
    return micros // 1_000_000


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default=DEFAULT_OUT)
    parser.add_argument("--interval", default="1h", choices=sorted(INTERVALS))
    parser.add_argument("--from", dest="date_from", default="2018-01-01",
                        help="inclusive UTC start day (default 2018-01-01)")
    parser.add_argument("--to", dest="date_to", default=None,
                        help="exclusive UTC end day")
    parser.add_argument("--source", default=None,
                        help="use a local 1-minute CSV instead of cloning")
    parser.add_argument("--verify", action="store_true",
                        help="verify an existing corpus and exit")
    args = parser.parse_args(argv)

    if args.verify:
        report = md.verify_manifest(args.out)
        print(report)
        for problem in report.problems:
            print(f"  {problem}")
        return 0 if not report.problems else 1
    return build(args)


if __name__ == "__main__":
    sys.exit(main())
