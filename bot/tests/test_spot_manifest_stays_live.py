"""append_spot_corpus.py had no MANIFEST.json handling at all — unlike
append_closed_corpus.py, which now keeps the living BTCUSDT entry in
data/real_linear_1d/MANIFEST.json and data/real_funding/MANIFEST.json
truthful after every write (see test_manifest_stays_live.py). This is the
same regression-proofing for spot, ahead of the day a real catch-up runs
(this host is CORPUS_BLOCKED, so no live write is attempted here).

Different schema, same intent: spot's manifest is `coinapi_flat/1`
(bars_per_symbol + files{rows, sha256_uncompressed, last_timestamp}), not
linear/funding's `date_range_utc`. There is no ETH/SOL entry to avoid
touching here — this manifest describes exactly one symbol, one file.

Nothing here reaches the network: every fetch is a fake.
"""
from __future__ import annotations

import datetime as dt
import gzip
import hashlib
import json
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

import append_spot_corpus as asc                    # noqa: E402

TODAY = dt.date(2026, 9, 20)
REL_PATH = "data/real_spot_btc/ohlcv/BINANCE_SPOT_BTC_USDT_1D.csv.gz"


def kline(day: dt.date, price: float = 100_000.0):
    ms = int(dt.datetime.combine(day, dt.time(),
                                 dt.timezone.utc).timestamp() * 1000)
    return [ms, price, price, price, price, "1.0", 0, 0, "100"]


def _repo_with_manifest(tmp_path):
    path = tmp_path / REL_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [asc.to_row(kline(dt.date(2026, 9, 1) + dt.timedelta(days=i)))
            for i in range(3)]                      # 09-01 .. 09-03
    with gzip.open(path, "wb") as fh:
        fh.write(asc.encode(rows))

    manifest = {
        "synthetic": False, "bar_seconds": 86400, "schema": "coinapi_flat/1",
        "generator": "tools/fetch_binance_klines.py",
        "source": {"exchange": "BINANCE", "market": "SPOT"},
        "bars_per_symbol": {"BINANCE_SPOT_BTC_USDT_1D": 3},
        "files": {
            "ohlcv/BINANCE_SPOT_BTC_USDT_1D.csv.gz": {
                "rows": 3,
                "sha256_uncompressed": "stale-before-append",
                "kind": "ohlcv", "interval_seconds": 86400,
                "first_timestamp": "2026-09-01T00:00:00.0000000Z",
                "last_timestamp": "2026-09-03T00:00:00.0000000Z",
            }
        },
    }
    manifest_path = tmp_path / "data" / "real_spot_btc" / "MANIFEST.json"
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return str(tmp_path), str(manifest_path)


def test_a_successful_write_updates_rows_sha_and_last_timestamp(tmp_path):
    repo, manifest_path = _repo_with_manifest(tmp_path)
    new_days = [kline(dt.date(2026, 9, d)) for d in (4, 5)]

    rep = asc.run(repo, today=TODAY, klines=new_days, write=True,
                  rel_path=REL_PATH)
    assert rep["new_closed_days"] == ["2026-09-04", "2026-09-05"]

    with open(manifest_path, encoding="utf-8") as fh:
        manifest = json.load(fh)
    entry = manifest["files"]["ohlcv/BINANCE_SPOT_BTC_USDT_1D.csv.gz"]
    assert entry["rows"] == 5
    assert entry["sha256_uncompressed"] == rep["uncompressed_sha256_after"]
    assert entry["last_timestamp"] == "2026-09-05T00:00:00.0000000Z"
    assert manifest["bars_per_symbol"]["BINANCE_SPOT_BTC_USDT_1D"] == 5


def test_a_dry_run_leaves_the_manifest_untouched(tmp_path):
    repo, manifest_path = _repo_with_manifest(tmp_path)
    before = open(manifest_path, encoding="utf-8").read()
    new_days = [kline(dt.date(2026, 9, d)) for d in (4, 5)]

    asc.run(repo, today=TODAY, klines=new_days, write=False, rel_path=REL_PATH)

    assert open(manifest_path, encoding="utf-8").read() == before


def test_no_manifest_beside_the_file_is_not_an_error(tmp_path):
    path = tmp_path / "lin.csv.gz"
    rows = [asc.to_row(kline(dt.date(2026, 9, 1) + dt.timedelta(days=i)))
            for i in range(3)]
    with gzip.open(path, "wb") as fh:
        fh.write(asc.encode(rows))

    rep = asc.run(str(tmp_path), today=TODAY,
                  klines=[kline(dt.date(2026, 9, 4))], write=True,
                  rel_path="lin.csv.gz")
    assert rep["new_closed_days"] == ["2026-09-04"]  # no exception raised


def test_nothing_new_leaves_the_manifest_untouched(tmp_path):
    """`appended` empty means the write block never runs at all — the
    manifest must not be touched just because run() was called."""
    repo, manifest_path = _repo_with_manifest(tmp_path)
    before = open(manifest_path, encoding="utf-8").read()

    asc.run(repo, today=TODAY, klines=[kline(TODAY)], write=True,
           rel_path=REL_PATH)  # only the open day; nothing closes

    assert open(manifest_path, encoding="utf-8").read() == before
