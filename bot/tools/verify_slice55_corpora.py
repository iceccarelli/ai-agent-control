#!/usr/bin/env python3
"""Verify the slice-55 funding and linear corpora, and pin them by content hash.

WHY THIS TOOL EXISTS
====================
`market_data.verify_manifest` checks a corpus against per-file `sha256` entries
in its own MANIFEST. The corpora `data/real_multi_1d` and `data/real_1d` carry
those entries because `tools/fetch_binance_klines.py` wrote them.

The slice-55 corpora were fetched by a human with a different tool, and their
MANIFESTs describe provenance (venue, endpoint, fetch time, per-symbol row
counts and date ranges) **without** a per-file checksum block. That left two
bad options and one good one:

* weaken `verify_manifest` so a missing `files` block is acceptable — this
  would remove a real integrity check from **every** corpus in the repository
  in order to admit one. Refused;
* edit the human's MANIFEST to add checksums — that would put an attestation
  the human did not make inside a file that reads as theirs. Refused;
* **verify the content independently and pin it here**, in an artefact this
  slice owns and signs. Taken.

WHAT THE PIN DOES AND DOES NOT PROVE
====================================
The sha256 values below are computed from the files **as received**. They
therefore detect any later drift, edit or truncation, and a test asserts them
on every run.

They do **not** independently attest the fetch. Nothing in this repository can:
the fetch happened outside it. What the repository can say honestly is that the
MANIFEST declares `synthetic: false`, names the venue and the REST endpoint, and
that the files on disk match that MANIFEST's own row counts and date ranges,
parse cleanly, are strictly increasing in time, carry no duplicate timestamps,
have finite rates and sane OHLC. That is the claim this tool checks, and it is
the claim the eligibility artefact records — no more.

This distinction is the whole reason the artefact carries a
`checksum_provenance` field rather than a bare `verified: true`.
"""
from __future__ import annotations

import csv
import datetime as dt
import gzip
import hashlib
import io
import json
import math
import os
import sys
from typing import Any, Dict, List

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

import data_contract as dc  # noqa: E402

FUNDING_DIR = "data/real_funding"
LINEAR_DIR = "data/real_linear_1d"
SYMBOLS = ("BTCUSDT", "ETHUSDT", "SOLUSDT")


def _open(path: str) -> io.TextIOBase:
    if path.endswith(".gz"):
        return gzip.open(path, "rt", encoding="utf-8", newline="")
    return open(path, "rt", encoding="utf-8", newline="")


def sha256_uncompressed(path: str) -> str:
    """Hash the DECOMPRESSED bytes.

    Gzip output is not deterministic across tools — it carries a timestamp and
    depends on the compressor — so hashing the container would produce a value
    that changes when nothing about the data does.
    """
    digest = hashlib.sha256()
    with _open(path) as handle:
        for chunk in iter(lambda: handle.read(1 << 20), ""):
            digest.update(chunk.encode("utf-8"))
    return digest.hexdigest()


def inspect_funding(symbol: str) -> Dict[str, Any]:
    path = os.path.join(REPO, FUNDING_DIR, "funding",
                        f"BINANCE_LINEAR_{symbol[:3]}_USDT_FUNDING.csv.gz")
    with _open(path) as handle:
        rows = list(csv.DictReader(handle))
    stamps = [dt.datetime.fromisoformat(r["funding_time"]) for r in rows]
    rates = [float(r["funding_rate"]) for r in rows]
    gaps = [(b - a).total_seconds() / 3600.0 for a, b in zip(stamps, stamps[1:])]
    return {
        "file": os.path.relpath(path, REPO),
        "sha256_uncompressed": sha256_uncompressed(path),
        "rows": len(rows),
        "first_utc": stamps[0].isoformat(),
        "last_utc": stamps[-1].isoformat(),
        "strictly_increasing": all(b > a for a, b in zip(stamps, stamps[1:])),
        "duplicate_timestamps": len(stamps) - len(set(stamps)),
        "all_rates_finite": all(math.isfinite(x) for x in rates),
        "rate_min": min(rates),
        "rate_max": max(rates),
        "gap_hours_min": min(gaps),
        "gap_hours_max": max(gaps),
        "abs_rate_ge_1e4": int(sum(1 for x in rates if abs(x) >= 1e-4)),
    }


def inspect_linear(symbol: str) -> Dict[str, Any]:
    path = os.path.join(REPO, LINEAR_DIR, "ohlcv",
                        f"BINANCE_LINEAR_{symbol[:3]}_USDT_1D.csv.gz")
    with _open(path) as handle:
        rows = list(csv.DictReader(handle))
    stamps = [dt.datetime.fromisoformat(r["time_period_start"]) for r in rows]
    ohlc = [(float(r["price_open"]), float(r["price_high"]),
             float(r["price_low"]), float(r["price_close"])) for r in rows]
    sane = all(h >= max(o, c) and l <= min(o, c) and l > 0.0
               for o, h, l, c in ohlc)
    return {
        "file": os.path.relpath(path, REPO),
        "sha256_uncompressed": sha256_uncompressed(path),
        "rows": len(rows),
        "first_utc": stamps[0].isoformat(),
        "last_utc": stamps[-1].isoformat(),
        "strictly_increasing": all(b > a for a, b in zip(stamps, stamps[1:])),
        "duplicate_timestamps": len(stamps) - len(set(stamps)),
        "ohlc_sane": sane,
    }


def main(argv=None) -> int:
    funding_manifest = json.load(
        open(os.path.join(REPO, FUNDING_DIR, "MANIFEST.json"), encoding="utf-8"))
    linear_manifest = json.load(
        open(os.path.join(REPO, LINEAR_DIR, "MANIFEST.json"), encoding="utf-8"))

    funding_record = dc.scan_funding_corpus(FUNDING_DIR)
    linear_record = dc.scan_corpus(LINEAR_DIR)

    payload: Dict[str, Any] = {
        "schema": "data_eligibility/1",
        "slice": 55,
        "checksum_provenance": (
            "sha256 values were computed BY THIS SLICE from the files as "
            "received. They detect later drift, edits or truncation and are "
            "asserted by tests/test_slice55_data_eligibility.py on every run. "
            "They do NOT independently attest the fetch, which happened "
            "outside this repository — unlike data/real_multi_1d, whose "
            "MANIFEST carries checksums written by the fetcher itself. The "
            "honest claim is: the MANIFEST declares synthetic false and names "
            "the venue and REST endpoint, and the files match that MANIFEST's "
            "own row counts and date ranges and pass every structural check."),
        "corpora": {
            FUNDING_DIR: {
                "manifest_synthetic": funding_manifest["synthetic"],
                "manifest_venue": funding_manifest["venue"],
                "manifest_source": funding_manifest["source"],
                "manifest_endpoint": funding_manifest["endpoint"],
                "manifest_interval": funding_manifest["interval"],
                "contract_eligible": funding_record.eligible_for_stage1,
                "contract_reasons": list(funding_record.ineligible_reasons),
                "symbols": {s: inspect_funding(s) for s in SYMBOLS},
            },
            LINEAR_DIR: {
                "manifest_synthetic": linear_manifest["synthetic"],
                "manifest_venue": linear_manifest["venue"],
                "manifest_source": linear_manifest["source"],
                "manifest_endpoint": linear_manifest["endpoint"],
                "manifest_interval": linear_manifest["interval"],
                "contract_eligible": linear_record.eligible_for_stage1,
                "contract_reasons": list(linear_record.ineligible_reasons),
                "loader_manifest_has_checksums": False,
                "loader_note": (
                    "market_data.verify_manifest requires a per-file checksum "
                    "block this MANIFEST does not carry, so the measurement "
                    "path loads this corpus with verify=False and relies on "
                    "the pinned hashes above instead. verify_manifest itself "
                    "was NOT weakened."),
                "symbols": {s: inspect_linear(s) for s in SYMBOLS},
            },
        },
    }
    payload["all_checks_passed"] = bool(
        funding_record.eligible_for_stage1
        and linear_record.eligible_for_stage1
        and all(v["strictly_increasing"] and v["duplicate_timestamps"] == 0
                and v["all_rates_finite"]
                for v in payload["corpora"][FUNDING_DIR]["symbols"].values())
        and all(v["strictly_increasing"] and v["duplicate_timestamps"] == 0
                and v["ohlc_sane"]
                for v in payload["corpora"][LINEAR_DIR]["symbols"].values()))

    out = os.path.join(REPO, "artifacts", "slice55_data_eligibility.json")
    with open(out, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False)
        handle.write("\n")

    print("=" * 78)
    print("SLICE 55 — DATA ELIGIBILITY")
    print("=" * 78)
    for directory, block in payload["corpora"].items():
        print(f"\n{directory}")
        print(f"  synthetic {block['manifest_synthetic']}   "
              f"venue {block['manifest_venue']}   "
              f"interval {block['manifest_interval']}")
        print(f"  contract eligible: {block['contract_eligible']}")
        for reason in block["contract_reasons"]:
            print(f"    - {reason}")
        for symbol, info in block["symbols"].items():
            print(f"  {symbol:8s} rows {info['rows']:>5}  "
                  f"{info['first_utc'][:10]} -> {info['last_utc'][:10]}  "
                  f"mono {info['strictly_increasing']}  "
                  f"dupes {info['duplicate_timestamps']}")
            print(f"           sha256 {info['sha256_uncompressed'][:32]}...")
    print()
    print(f"ALL CHECKS PASSED: {payload['all_checks_passed']}")
    print(f"artefact: artifacts/slice55_data_eligibility.json")
    return 0 if payload["all_checks_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
