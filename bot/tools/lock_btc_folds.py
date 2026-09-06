#!/usr/bin/env python3
"""Freeze the `funding_carry_fade_btc_v1` fold calendar, before anything is scored.

WHY THIS IS ITS OWN TOOL, RUN ITS OWN COMMIT
============================================
The intake's cut is `t_mid` = the timestamp at the 50% point of the BTC linear
daily bar count, **by index position**. That rule has no free parameters, which
is the entire reason it was chosen: there is nothing in it that could be nudged.

But a rule with no free parameters is still only as good as the order in which
it was applied. A cut computed after seeing that the OOS count came to 38 and
the floor is 40 is a fitted parameter no matter how principled the formula
looks in the write-up. So this runs first, alone, and its output is hashed:

* the calendar goes to `artifacts/funding_carry_fade_btc_v1_folds.json`;
* the sha256 of that file goes to `artifacts/slice57_folds_lock.log`;
* both are committed BEFORE the module that would consume them exists.

A later reader does not have to take the ordering on trust. They can check that
the commit adding the folds JSON precedes the commit adding any OOS artefact,
and that the hash in the lock log still matches the file.

WHAT IT PINS, AND WHY EACH FIELD IS THERE
=========================================
* `t0`, `t_mid`, `t1` — the cut itself, ISO UTC;
* `n_bars_early`, `n_bars_late` — so a reader can verify the 50% claim by
  arithmetic rather than by reading this docstring;
* `mid_index` — the index the rule produced, so the computation is reproducible
  without re-deriving what "50%" rounds to;
* `sha256` of **both** source files, decompressed. If either corpus is ever
  swapped, extended or re-fetched, the cut computed from it is no longer the cut
  that was locked, and that becomes visible instead of silent;
* `method` — the literal string `index_midpoint_50pct`, so the rule travels with
  the numbers.

WHAT IT DOES NOT DO
===================
It reads no funding rates, computes no setups, schedules no trades and produces
no score. It cannot: nothing in this file imports the signal module, which does
not exist yet.
"""
from __future__ import annotations

import argparse
import datetime as dt
import gzip
import hashlib
import io
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "tools"))

import backtest as bt  # noqa: E402
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



SYMBOL = "BTCUSDT"
LINEAR = "data/real_linear_1d"
LINEAR_FILE = "data/real_linear_1d/ohlcv/BINANCE_LINEAR_BTC_USDT_1D.csv.gz"
FUNDING_FILE = "data/real_funding/funding/BINANCE_LINEAR_BTC_USDT_FUNDING.csv.gz"
FOLDS = "artifacts/funding_carry_fade_btc_v1_folds.json"
LOCK = "artifacts/slice57_folds_lock.log"

SCHEMA = "fold_calendar/1"
METHOD = "index_midpoint_50pct"


def _open(path: str) -> io.TextIOBase:
    if path.endswith(".gz"):
        return gzip.open(path, "rt", encoding="utf-8", newline="")
    return open(path, "rt", encoding="utf-8", newline="")


def sha256_uncompressed(path: str) -> str:
    """Hash the DECOMPRESSED bytes — gzip containers are not deterministic."""
    digest = hashlib.sha256()
    with _open(path) as handle:
        for chunk in iter(lambda: handle.read(1 << 20), ""):
            digest.update(chunk.encode("utf-8"))
    return digest.hexdigest()


def iso(ms: int) -> str:
    return dt.datetime.fromtimestamp(
        ms / 1000.0, tz=dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build(created_utc: str) -> dict:
    # verify=False: this corpus's MANIFEST carries no per-file checksum block
    # and market_data.verify_manifest was not weakened to admit it (slice 55).
    # The files are pinned by the sha256 values computed below instead.
    loaded, _books, _notes = md.load_corpus(
        os.path.join(REPO, LINEAR), verify=False)
    bars = [bt.Bar(b.start_ms, b.open, b.high, b.low, b.close, b.volume)
            for b in loaded[SYMBOL]]
    bars.sort(key=lambda b: b.start_ms)

    stamps = [b.start_ms for b in bars]
    assert all(b > a for a, b in zip(stamps, stamps[1:])), \
        "bar times are not strictly increasing; refusing to cut a series " \
        "whose order is not the order it claims"

    n = len(bars)
    mid_index = n // 2                      # 50% by INDEX POSITION, floor
    t0, t_mid, t1 = stamps[0], stamps[mid_index], stamps[-1]

    return {
        "schema": SCHEMA,
        "slice": 57,
        "signal": "funding_carry_fade_btc_v1",
        "symbol": SYMBOL,
        "created_utc": created_utc,
        "method": METHOD,
        "method_note": (
            "t_mid is the start time of the bar at index floor(n/2) in the "
            "time-sorted BTC linear daily series. The cut is derived from "
            "TIMESTAMPS AND INDEX POSITION ONLY — not from returns, not from "
            "the funding series, not from any trade list, and not from any "
            "score. Nothing in this file was computed after a percentile "
            "existed, because at the time it was written the signal module "
            "did not exist."),
        "t0": iso(t0),
        "t_mid": iso(t_mid),
        "t1": iso(t1),
        "t0_ms": int(t0),
        "t_mid_ms": int(t_mid),
        "t1_ms": int(t1),
        "n_bars_total": n,
        "mid_index": mid_index,
        "n_bars_early": mid_index,
        "n_bars_late": n - mid_index,
        "early_window": "[t0, t_mid)  — burn-in, MAY NOT REGISTER",
        "late_window": "[t_mid, t1]  — out of sample, the ONLY window that may register",
        "sources": {
            "linear_bars": {
                "path": LINEAR_FILE,
                "sha256_uncompressed": sha256_uncompressed(
                    os.path.join(REPO, LINEAR_FILE)),
            },
            "funding": {
                "path": FUNDING_FILE,
                "sha256_uncompressed": sha256_uncompressed(
                    os.path.join(REPO, FUNDING_FILE)),
            },
        },
        "forbidden": (
            "t_mid may not be moved after n_OOS, M1, M2 or the mean net R "
            "exists. A cut adjusted to clear a count is a parameter fitted to "
            "a result. If the OOS count falls below the intake's floor of 40, "
            "the verdict is INCONCLUSIVE and this file is unchanged."),
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--created-utc", required=True,
                        help="ISO UTC stamp, passed in so the tool is pure")
    args = parser.parse_args(argv)

    payload = build(args.created_utc)

    out = os.path.join(REPO, FOLDS)
    with open(out, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False)
        handle.write("\n")

    folds_sha = hashlib.sha256(
        open(out, "rb").read()).hexdigest()

    lines = [
        "=" * 78,
        "SLICE 57 — FOLD CALENDAR LOCKED",
        "=" * 78,
        "",
        f"  signal          : {payload['signal']}  ({payload['symbol']} only)",
        f"  method          : {payload['method']}",
        f"  created_utc     : {payload['created_utc']}",
        "",
        f"  t0              : {payload['t0']}",
        f"  t_mid           : {payload['t_mid']}        <- the cut",
        f"  t1              : {payload['t1']}",
        "",
        f"  bars total      : {payload['n_bars_total']:,}",
        f"  mid_index       : {payload['mid_index']:,}",
        f"  early [t0,t_mid): {payload['n_bars_early']:,} bars   burn-in, may NOT register",
        f"  late  [t_mid,t1]: {payload['n_bars_late']:,} bars   OOS, the ONLY registering window",
        "",
        f"  linear  sha256  : {payload['sources']['linear_bars']['sha256_uncompressed']}",
        f"  funding sha256  : {payload['sources']['funding']['sha256_uncompressed']}",
        "",
        "-" * 78,
        f"  FOLDS FILE      : {FOLDS}",
        f"  FOLDS SHA256    : {folds_sha}",
        "-" * 78,
        "",
        "This file was written BEFORE signals/funding_carry_fade_btc_v1.py",
        "existed, before any count, before any control and before any",
        "percentile. From this point t_mid may not be moved. If the OOS trade",
        "count falls below the intake's floor of 40 the verdict is",
        "INCONCLUSIVE and this calendar is unchanged — a cut adjusted to clear",
        "a count is a parameter fitted to a result, and it would be nearly",
        "invisible in a summary.",
        "",
    ]
    text = "\n".join(lines)
    with open(os.path.join(REPO, LOCK), "w", encoding="utf-8") as handle:
        handle.write(text)
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
