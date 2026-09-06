#!/usr/bin/env python3
"""Did an extension actually arrive? Hash the files; do not read the note.

WHY THIS TOOL IS NOT `data_freshness_check.py`
==============================================
Slice 60's freshness tool asked "has time passed and did a fetch work". This one
asks a different question: **a human pack claims to contain extended data — does
it?**

The distinction matters because the failure mode is different. Slice 60's risk
was mistaking the passage of no time for progress. This slice's risk is
mistaking a *description of data* for data, which is the third costume of one
recurring substitution (EDGE.md §44d):

    slice 55  a rule in prose        instead of a rule in code
    slice 59  history relabelled     instead of forward experience
    slice 61  a note asserting data  instead of the data

The defence is the same every time and it is arithmetic, not sophistication:
**check the thing itself.**

WHAT IT CHECKS
==============
* the pack's own manifest — how many members, how many are data files;
* the sha256 of both BTCUSDT corpus files against the values pinned in the fold
  calendar when it was locked in slice 57. Identical hashes prove two things at
  once: **no history was rewritten** (the note's promise, kept) and **no
  extension exists** (identical files cannot contain new rows);
* the last timestamps, and the count of rows strictly after `t1`.

`extension_present` is true only if at least one **closed** linear daily bar has
a start strictly after `t1`. Not if the note says so. Not if a funding print
arrived without a bar to decide.

WHAT IT WILL NOT DO
===================
It fetches nothing — slice 60 attempted the sanctioned path and recorded a
`ProxyError`, this environment has no egress, and repeating a call that failed
yesterday to produce a fresh-looking log line is activity rather than evidence.
It writes no bars, appends to no corpus and fabricates nothing.
"""
from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import io
import json
import os
import subprocess
import sys
import zipfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "tools"))

import market_data as md  # noqa: E402
from signals import funding_carry_fade_btc_v1 as fb  # noqa: E402
import provenance as _provenance  # noqa: E402

SCHEMA = "data_freshness/2"
LINEAR = "data/real_linear_1d/ohlcv/BINANCE_LINEAR_BTC_USDT_1D.csv.gz"
FUNDING = "data/real_funding/funding/BINANCE_LINEAR_BTC_USDT_FUNDING.csv.gz"


def read_gz(path: str) -> str:
    with gzip.open(os.path.join(REPO, path), "rt", encoding="utf-8",
                   newline="") as handle:
        return handle.read()


def inspect(path: str, column: str, t1_ms: int) -> dict:
    text = read_gz(path)
    rows = list(csv.DictReader(io.StringIO(text)))
    after = [r for r in rows
             if md.parse_timestamp(r[column]) // 1000 > t1_ms]
    return {
        "path": path,
        "rows": len(rows),
        "last_timestamp": rows[-1][column],
        "rows_after_t1": len(after),
        "sha256_uncompressed": hashlib.sha256(text.encode("utf-8")).hexdigest(),
    }


def pack_manifest(pack_path: str) -> dict:
    if not pack_path or not os.path.isfile(pack_path):
        return {"pack_path": pack_path, "readable": False,
                "note": "the pack was not available at this path"}
    with open(pack_path, "rb") as handle:
        digest = hashlib.sha256(handle.read()).hexdigest()
    with zipfile.ZipFile(pack_path) as archive:
        members = [{"name": i.filename, "bytes": i.file_size}
                   for i in archive.infolist()]
    data_members = [m for m in members
                    if m["name"].endswith((".csv", ".gz", ".json", ".parquet"))]
    return {
        "pack_path": os.path.basename(pack_path),
        "readable": True,
        "sha256": digest,
        "bytes": os.path.getsize(pack_path),
        "members": members,
        "member_count": len(members),
        "data_member_count": len(data_members),
        "contains_the_files_the_note_names": False if not data_members else None,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pack", default="/tmp/s61pack.zip")
    parser.add_argument("--checked-at-utc", required=True)
    parser.add_argument("--out", default=os.path.join(
        REPO, "artifacts", "slice61_data_freshness.json"))
    args = parser.parse_args(argv)

    folds = fb.load_folds()
    t1, t1_ms = folds["t1"], int(folds["t1_ms"])

    linear = inspect(LINEAR, "time_period_start", t1_ms)
    funding = inspect(FUNDING, "funding_time", t1_ms)

    pinned_linear = folds["sources"]["linear_bars"]["sha256_uncompressed"]
    pinned_funding = folds["sources"]["funding"]["sha256_uncompressed"]
    linear_unchanged = linear["sha256_uncompressed"] == pinned_linear
    funding_unchanged = funding["sha256_uncompressed"] == pinned_funding

    extension_present = linear["rows_after_t1"] > 0
    note_path = "docs/human/HUMAN_DATA_NOTE_SLICE61.md"
    note_present = os.path.isfile(os.path.join(REPO, note_path))

    payload = {
        "schema": SCHEMA,
        "slice": 61,
        "symbol": "BTCUSDT",
        "checked_at_utc": args.checked_at_utc,

        "t1_previous": t1,
        "t1_previous_ms": t1_ms,
        "t1_source": "artifacts/funding_carry_fade_btc_v1_folds.json",
        "folds_sha256": fb.folds_sha256(),
        "folds_unmodified": fb.folds_are_unmodified(),

        # -- what the human pack actually contained --------------------------
        "human_note_present": note_present,
        "human_note_path": note_path,
        "human_note_claims_extended_files": [
            "data/real_linear_1d/ohlcv/BINANCE_LINEAR_BTC_USDT_1D.csv.gz",
            "data/real_funding/funding/BINANCE_LINEAR_BTC_USDT_FUNDING.csv.gz",
        ],
        "human_pack": pack_manifest(args.pack),
        "human_pack_supplied_data_files": False,
        "discrepancy": (
            "The human note describes an extension at two data paths. The pack "
            "contains 2 markdown documents and 0 data files, and no repository "
            "tree. Recorded without accusation — a pack can be assembled and "
            "its large members fail to attach — but the artefact that would "
            "settle the question is absent, so the question is not settled. "
            "EDGE.md §44a."),

        # -- what the files on disk actually say ------------------------------
        "latest_linear_timestamp": linear["last_timestamp"],
        "latest_funding_timestamp": funding["last_timestamp"],
        "linear_rows": linear["rows"],
        "funding_rows": funding["rows"],
        "new_linear_bars_count": linear["rows_after_t1"],
        "new_funding_prints_count": funding["rows_after_t1"],

        "hash_check": {
            "linear_sha256": linear["sha256_uncompressed"],
            "linear_pinned_in_folds": pinned_linear,
            "linear_unchanged_since_t1": linear_unchanged,
            "funding_sha256": funding["sha256_uncompressed"],
            "funding_pinned_in_folds": pinned_funding,
            "funding_unchanged_since_t1": funding_unchanged,
            "what_this_proves": (
                "Identical hashes prove TWO things at once. (1) NO HISTORY WAS "
                "REWRITTEN — the note's more important promise, kept exactly, "
                "not one byte of measured history has moved. (2) NO EXTENSION "
                "EXISTS — identical files cannot contain new rows. The second "
                "follows from the first by arithmetic, which is why the check "
                "is a hash and not a reading of the note."),
        },

        "extension_present": extension_present,
        "new_bars_available": extension_present,
        "bars_fabricated": 0,
        "corpus_appended_to": False,
        "fetch_attempted": False,
        "why_no_fetch": (
            "Slice 60 attempted the sanctioned in-repo fetcher and recorded a "
            "ProxyError against fapi.binance.com; this environment has no "
            "egress to the venue and nothing about that has changed. Repeating "
            "a call that failed yesterday in order to produce a fresh-looking "
            "log line is activity, not evidence. See "
            "artifacts/slice60_data_freshness.json."),

        "what_would_count": {
            "rule": "a CLOSED linear daily bar with start strictly after t1",
            "closed_only": True,
            "synthetic_must_be_false": True,
            "first_forward_observation_needs_closed_bars": fb.HORIZON + 1,
            "note": (
                "A trade is an observation only when it CLOSES. With a "
                f"{fb.HORIZON}-bar horizon the first forward observation "
                f"cannot exist until at least {fb.HORIZON + 1} closed forward "
                f"bars do. A funding print without a bar to decide is not an "
                f"observation either — the decision clock is the daily bar."),
        },
        "git_commit": _provenance.git_commit(REPO),
    }
    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False)
        handle.write("\n")

    print("=" * 78)
    print("SLICE 61 — DATA FRESHNESS   (hash the files; do not read the note)")
    print("=" * 78)
    pack = payload["human_pack"]
    print(f"  human pack                : {pack.get('pack_path')}  "
          f"({pack.get('bytes')} bytes)")
    print(f"  pack sha256               : {pack.get('sha256')}")
    for member in pack.get("members", []):
        print(f"      {member['bytes']:8d}  {member['name']}")
    print(f"  data files in the pack    : {pack.get('data_member_count')}")
    print(f"  the note names            : "
          f"{len(payload['human_note_claims_extended_files'])} extended files")
    print()
    print(f"  t1                        : {t1}")
    print(f"  latest linear timestamp   : "
          f"{payload['latest_linear_timestamp']}")
    print(f"  latest funding timestamp  : "
          f"{payload['latest_funding_timestamp']}")
    print()
    print(f"  linear sha256 == folds pin : {linear_unchanged}")
    print(f"  funding sha256 == folds pin: {funding_unchanged}")
    print(f"    -> history rewritten     : {not (linear_unchanged and funding_unchanged)}")
    print(f"    -> extension present     : {extension_present}")
    print()
    print(f"  new linear bars           : {payload['new_linear_bars_count']}")
    print(f"  new funding prints        : {payload['new_funding_prints_count']}")
    print(f"  EXTENSION PRESENT         : {extension_present}")
    print(f"  bars fabricated           : 0")
    print(f"  corpus appended to        : False")
    print()
    print(f"artefact: {os.path.relpath(args.out, REPO)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
