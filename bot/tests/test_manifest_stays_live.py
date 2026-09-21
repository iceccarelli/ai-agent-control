"""Regression lock for the 2026-09-21 defect: a real catch-up grew
`data/real_linear_1d` and `data/real_funding` (BTCUSDT: 1498->1502,
4494->4506 rows) but nothing updated their MANIFEST.json, so 4 tests that
assert the living BTCUSDT manifest entry matches disk
(test_slice55_data_eligibility.py, test_slice67_five_day_window.py) went red
on `main` for a reason unrelated to what they were built to catch.

`append_closed_corpus.run()` now updates the sibling MANIFEST.json's
BTCUSDT `rows`/`end` on every successful write, so the next catch-up cannot
silently do this again. This is a LIVING descriptor, not a frozen pin: it
updates every time — the test below asserts the update happens, not that
today's specific numbers hold forever.

Nothing here reaches the network: every fetch is a fake.
"""
from __future__ import annotations

import datetime as dt
import gzip
import json
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

import append_closed_corpus as acc                  # noqa: E402

DAY = 86_400_000
NOW = "2026-09-18T18:19:11Z"
NOW_MS = int(dt.datetime(2026, 9, 18, 18, 19, 11, tzinfo=dt.timezone.utc).timestamp() * 1000)


def _corpus_with_manifest(tmp_path):
    """A linear+funding pair inside a `data/real_linear_1d`-shaped tree, with
    a MANIFEST.json beside it declaring stale BTCUSDT counts and untouched
    ETH/SOL entries — the exact shape the real repo was in."""
    tree = tmp_path / "data" / "real_linear_1d"
    ohlcv = tree / "ohlcv"
    ohlcv.mkdir(parents=True)
    lin = ohlcv / "BINANCE_LINEAR_BTC_USDT_1D.csv.gz"
    day0 = dt.datetime(2026, 9, 14, tzinfo=dt.timezone.utc)
    rows = ["time_period_start,time_period_end,time_open,time_close,price_open,"
            "price_high,price_low,price_close,volume_traded,trades_count"]
    for i in range(2):
        d = day0 + dt.timedelta(days=i)
        s = d.strftime("%Y-%m-%dT00:00:00+00:00")
        e = d.strftime("%Y-%m-%dT23:59:59+00:00")
        rows.append(f"{s},{e},{s},{e},77000,79000,76000,78000,1000,5")
    with gzip.open(lin, "wb") as fh:
        fh.write(("\r\n".join(rows) + "\r\n").encode())

    fun_dir = tmp_path / "data" / "real_funding" / "funding"
    fun_dir.mkdir(parents=True)
    fun = fun_dir / "BINANCE_LINEAR_BTC_USDT_FUNDING.csv.gz"
    t = int(dt.datetime(2026, 9, 15, 16, tzinfo=dt.timezone.utc).timestamp() * 1000)
    with gzip.open(fun, "wb") as fh:
        fh.write(("funding_time,funding_time_ms,symbol,funding_rate,mark_price\r\n"
                  f"2026-09-15T16:00:00+00:00,{t + 5},BTCUSDT,0.00010000,79464.0\r\n"
                  ).encode())

    manifest = {
        "symbols": ["BTCUSDT", "ETHUSDT", "SOLUSDT"],
        "date_range_utc": {
            "BTCUSDT": {"start": "2022-08-10", "end": "2026-09-15", "rows": 2},
            "ETHUSDT": {"start": "2022-08-10", "end": "2026-08-13", "rows": 1465},
            "SOLUSDT": {"start": "2022-08-10", "end": "2026-08-13", "rows": 1465},
        },
    }
    lin_manifest = tree / "MANIFEST.json"
    lin_manifest.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    fun_manifest_dict = json.loads(json.dumps(manifest))
    fun_manifest_dict["date_range_utc"]["BTCUSDT"]["rows"] = 1
    fun_manifest = tmp_path / "data" / "real_funding" / "MANIFEST.json"
    fun_manifest.write_text(json.dumps(fun_manifest_dict, indent=2), encoding="utf-8")

    return str(lin), str(fun), str(lin_manifest), str(fun_manifest)


def _fake_fetch(now_ms):
    def fetch(path, params):
        if path == "klines":
            start = int(params["startTime"])
            out, o = [], start
            while o <= now_ms:  # includes the OPEN bar on purpose
                out.append([o, "77719.00000000", "79974.80", "76649", "78953.0",
                            "232502.35000000", o + DAY - 1, "0", 5914733])
                o += DAY
            return out
        if path == "fundingRate":
            start = int(params["startTime"])
            out, t = [], (start // 28_800_000 + 1) * 28_800_000
            while t <= now_ms + 28_800_000:  # includes one FUTURE print on purpose
                out.append({"symbol": "BTCUSDT", "fundingTime": t + 3,
                            "fundingRate": "0.00010000", "markPrice": "80000.1"})
                t += 28_800_000
            return out
        raise AssertionError(path)
    return fetch


def test_a_successful_write_updates_the_living_btc_entry(tmp_path):
    lin, fun, lin_manifest, fun_manifest = _corpus_with_manifest(tmp_path)

    rep = acc.run(observed_at_utc=NOW, fetch=_fake_fetch(NOW_MS),
                  linear_path=lin, funding_path=fun, write=True)

    assert rep["new_closed_bars"], "fixture must actually append something"
    with open(lin_manifest, encoding="utf-8") as fh:
        lin_m = json.load(fh)["date_range_utc"]["BTCUSDT"]
    assert lin_m["rows"] == 2 + len(rep["new_closed_bars"])
    assert lin_m["end"] == rep["linear_last_after"]

    with open(fun_manifest, encoding="utf-8") as fh:
        fun_m = json.load(fh)["date_range_utc"]["BTCUSDT"]
    assert fun_m["rows"] == 1 + rep["new_funding_prints"]
    assert fun_m["end"] == rep["funding_last_after"]


def test_eth_and_sol_entries_are_never_touched(tmp_path):
    lin, fun, lin_manifest, _fun_manifest = _corpus_with_manifest(tmp_path)
    with open(lin_manifest, encoding="utf-8") as fh:
        before = json.load(fh)["date_range_utc"]["ETHUSDT"]

    acc.run(observed_at_utc=NOW, fetch=_fake_fetch(NOW_MS),
           linear_path=lin, funding_path=fun, write=True)

    with open(lin_manifest, encoding="utf-8") as fh:
        after = json.load(fh)["date_range_utc"]["ETHUSDT"]
    assert after == before


def test_a_dry_run_leaves_the_manifest_untouched(tmp_path):
    lin, fun, lin_manifest, fun_manifest = _corpus_with_manifest(tmp_path)
    before_lin = open(lin_manifest, encoding="utf-8").read()
    before_fun = open(fun_manifest, encoding="utf-8").read()

    acc.run(observed_at_utc=NOW, fetch=_fake_fetch(NOW_MS),
           linear_path=lin, funding_path=fun, write=False)

    assert open(lin_manifest, encoding="utf-8").read() == before_lin
    assert open(fun_manifest, encoding="utf-8").read() == before_fun


def test_no_manifest_beside_the_file_is_not_an_error(tmp_path):
    """The dual-tree tests' fixtures have no MANIFEST.json at all — this
    must not raise, since not every corpus tree carries one."""
    lin = tmp_path / "lin.csv.gz"
    fun = tmp_path / "fun.csv.gz"
    day0 = dt.datetime(2026, 9, 14, tzinfo=dt.timezone.utc)
    rows = ["time_period_start,time_period_end,time_open,time_close,price_open,"
            "price_high,price_low,price_close,volume_traded,trades_count"]
    for i in range(2):
        d = day0 + dt.timedelta(days=i)
        s = d.strftime("%Y-%m-%dT00:00:00+00:00")
        e = d.strftime("%Y-%m-%dT23:59:59+00:00")
        rows.append(f"{s},{e},{s},{e},77000,79000,76000,78000,1000,5")
    with gzip.open(lin, "wb") as fh:
        fh.write(("\r\n".join(rows) + "\r\n").encode())
    with gzip.open(fun, "wb") as fh:
        fh.write(("funding_time,funding_time_ms,symbol,funding_rate,mark_price\r\n"
                  "2026-09-15T16:00:00+00:00,1789574400005,BTCUSDT,0.0001,79464.0\r\n"
                  ).encode())

    rep = acc.run(observed_at_utc=NOW, fetch=_fake_fetch(NOW_MS),
                  linear_path=str(lin), funding_path=str(fun), write=True)
    assert rep["new_closed_bars"]  # actually wrote something; no exception raised


def test_the_real_manifests_agree_with_disk_right_now():
    """The exact ground truth Q0 found broken: after this fix, the living
    BTCUSDT entries in the real repo's MANIFEST.json files must already
    match what is on disk (fixed by hand this shift, matching what the
    write path now does automatically going forward)."""
    import tools.corpus_prefix as cp
    for directory in ("data/real_linear_1d", "data/real_funding"):
        manifest_path = os.path.join(ROOT, directory, "MANIFEST.json")
        with open(manifest_path, encoding="utf-8") as fh:
            declared = json.load(fh)["date_range_utc"]["BTCUSDT"]
        subdir, suffix = ("ohlcv", "1D") if "linear" in directory else ("funding", "FUNDING")
        path = f"{directory}/{subdir}/BINANCE_LINEAR_BTC_USDT_{suffix}.csv.gz"
        assert declared["rows"] == len(cp.read_rows(path)), (directory, declared)
