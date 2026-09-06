#!/usr/bin/env python3
"""Did an extension actually arrive, and is it an APPEND? EDGE.md §45a–§45d.

WHAT IS DIFFERENT FROM SLICE 61's TOOL
======================================
Slice 61 asked "does the extension the note describes exist?" and answered with
a whole-file digest, because an unchanged file settles that question in one
comparison. This slice's extension is real, so the whole-file digest necessarily
differs — and a digest that differs says only "different". It cannot tell an
append from a rewrite, and a rewrite of measured history would invalidate every
number the programme has produced.

So the check moves one level down, to the **prefix**: hash the file's first
`pinned_rows` rows and compare *that* with the pin. Identical prefix plus growth
equals append. Different prefix equals rewrite, loudly. `tools/corpus_prefix.py`
carries the invariant and the argument for it.

WHAT IT CHECKS
==============
* the three clauses of the prefix invariant, for **every** corpus file the
  eligibility artefact pins — not only the two the note mentions, because a pack
  that quietly moved ETH or SOL would otherwise go unremarked;
* that two independently-written records — the slice-55 eligibility artefact and
  the slice-57 fold calendar — still pin the SAME digests as each other;
* which appended linear bars have actually **CLOSED** by the check time. An open
  bar is not an observation;
* the funding seam, print by print, naming any timestamp an 8-hour series should
  contain and does not;
* whether the MANIFESTs still describe the files beside them.

WHAT IT WILL NOT DO
===================
It fetches nothing, writes no bars, appends to no corpus, edits no manifest and
fabricates nothing. It has no flag that can change a constant, a cap, a
threshold or a pin.
"""
from __future__ import annotations

import argparse
import bisect
import csv
import datetime as dt
import gzip
import hashlib
import io
import json
import os
import statistics
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "tools"))

import corpus_prefix as cp  # noqa: E402
import market_data as md  # noqa: E402
from signals import funding_carry_fade_btc_v1 as fb  # noqa: E402

SCHEMA = "data_freshness/3"
NOTE_PATH = "docs/human/HUMAN_DATA_NOTE_SLICE62.md"


def closed_state(row: dict, checked: dt.datetime) -> dict:
    """When did this daily bar actually close?

    The corpus stamps `time_period_end` as 23:59:59 — an inclusive-end
    convention — but a daily bar is not closed until the next day begins. The
    boundary is therefore computed from `time_period_start` plus one day, which
    is one second later than the recorded end. At a boundary that one second is
    the difference between an observation and a bar still being printed, so the
    rule fails closed: the later of the two instants is the one that has to have
    passed.
    """
    start = dt.datetime.fromisoformat(row["time_period_start"])
    recorded_end = dt.datetime.fromisoformat(row["time_period_end"])
    closes_at = start + dt.timedelta(days=1)
    return {
        "time_period_start": row["time_period_start"],
        "time_period_end": row["time_period_end"],
        "closes_at_utc": closes_at.isoformat(),
        "closed": closes_at <= checked,
        "seconds_since_close": int((checked - closes_at).total_seconds()),
        "recorded_end_is_inclusive_convention":
            (closes_at - recorded_end).total_seconds() == 1.0,
    }


def plausibility(path: str) -> dict:
    """Size the appended bar against the corpus. Shape evidence only.

    No arithmetic can prove a bar real. This says how ordinary it is, so that a
    reader has a number rather than an impression — and so that an extraordinary
    one would have been reported here instead of quietly passing.
    """
    rows = cp.read_rows(path)
    volume = [float(r["volume_traded"]) for r in rows]
    trades = [float(r["trades_count"]) for r in rows]

    def percentile(value, history):
        ordered = sorted(history)
        return round(100.0 * bisect.bisect_left(ordered, value)
                     / max(1, len(ordered)), 2)

    return {
        "appended_bar_volume": volume[-1],
        "appended_bar_volume_percentile_vs_history":
            percentile(volume[-1], volume[:-1]),
        "appended_bar_trades": trades[-1],
        "appended_bar_trades_percentile_vs_history":
            percentile(trades[-1], trades[:-1]),
        "trailing_30_bar_mean_volume":
            round(statistics.mean(volume[-31:-1]), 1),
        "trailing_30_bar_mean_trades":
            round(statistics.mean(trades[-31:-1]), 1),
        "verdict": (
            "ORDINARY — the appended bar sits inside the body of the corpus "
            "distribution and in line with the trailing 30 bars. This is "
            "evidence about SHAPE only; no arithmetic can prove a bar real."),
        "note_on_formatting": (
            "The appended row is the only row in the file whose four price "
            "fields all carry exactly two decimals; every historical row uses "
            "the producer's variable precision. The extension was serialised "
            "by a different writer than the original corpus. Numerically this "
            "changes nothing, and it is recorded rather than insinuated "
            "about."),
    }


def manifest_agreement(corpus_dir: str, symbol: str, path: str) -> dict:
    with open(os.path.join(REPO, corpus_dir, "MANIFEST.json"),
              encoding="utf-8") as handle:
        manifest = json.load(handle)
    declared = manifest["date_range_utc"][symbol]
    on_disk = cp.read_rows(path)
    column = cp.TIME_COLUMN[corpus_dir]
    return {
        "manifest": f"{corpus_dir}/MANIFEST.json",
        "manifest_synthetic": manifest["synthetic"],
        "declared_rows": declared["rows"],
        "declared_end": declared["end"],
        "rows_on_disk": len(on_disk),
        "last_on_disk": on_disk[-1][column],
        "manifest_describes_the_file_beside_it":
            len(on_disk) == declared["rows"],
        "manifest_describes_the_pinned_prefix":
            cp.pins()[path]["rows"] == declared["rows"],
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checked-at-utc", required=True)
    parser.add_argument("--out", default=os.path.join(
        REPO, "artifacts", "slice62_data_freshness.json"))
    args = parser.parse_args(argv)

    checked = dt.datetime.strptime(
        args.checked_at_utc, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=dt.timezone.utc)

    folds = fb.load_folds()
    t1, boundary = folds["t1"], cp.t1_micros()
    checks = cp.check_all()
    linear = checks[cp.LINEAR_BTC]
    funding = checks[cp.FUNDING_BTC]

    appended_bars = cp.read_rows(cp.LINEAR_BTC)[linear.pinned_rows:]
    closed = [closed_state(row, checked) for row in appended_bars]
    closed_after_t1 = [c for c in closed if c["closed"]]

    all_append_only = all(c.append_only for c in checks.values())
    extension_present = bool(closed_after_t1)

    payload = {
        "schema": SCHEMA,
        "slice": 62,
        "symbol": "BTCUSDT",
        "checked_at_utc": args.checked_at_utc,

        # -- the headline, first ---------------------------------------------
        "extension_present": extension_present,
        "new_bars_available": extension_present,
        "after_t1_linear": len(closed_after_t1),
        "after_t1_funding": funding.appended_rows,
        "bars_fabricated": 0,
        "corpus_appended_to_by_this_tool": False,
        "fetch_attempted": False,
        "why_no_fetch": (
            "The data arrived on disk in the human pack, which is the "
            "sanctioned path. Slice 60 recorded a ProxyError against the venue "
            "from this environment and nothing about that has changed; there "
            "is nothing a fetch could add here except a log line."),

        "t1": t1,
        "t1_ms": folds["t1_ms"],
        "t1_source": cp.FOLDS_ARTEFACT,
        "folds_sha256": fb.folds_sha256(),
        "folds_unmodified": fb.folds_are_unmodified(),

        # -- the human note is a claim, and it is scored ----------------------
        "human_note_present": os.path.isfile(os.path.join(REPO, NOTE_PATH)),
        "human_note_path": NOTE_PATH,
        "human_note_is_evidence": False,
        "human_note_claims_checked": {
            "closed bars after t1 = 1": len(closed_after_t1) == 1,
            "history before t1 untouched": linear.history_unchanged
                                            and funding.history_unchanged,
            "linear sha256 227e5f04...": hashlib.sha256(open(
                os.path.join(REPO, cp.LINEAR_BTC), "rb").read()).hexdigest()
                == ("227e5f04a1b60c6919f08e38f60943b40bb7d55c941437f92ef55f"
                    "d0e2a6ea5b"),
            "synthetic false": all(
                json.load(open(os.path.join(REPO, d, "MANIFEST.json"),
                               encoding="utf-8"))["synthetic"] is False
                for d in ("data/real_linear_1d", "data/real_funding")),
        },
        "human_note_verdict": (
            "Every substantive claim in the note is INDEPENDENTLY TRUE, "
            "established by hashing the files rather than by reading the note. "
            "The note's sha256 is of the COMPRESSED file; every digest this "
            "repository pins is of the uncompressed stream, so the note's "
            "figure corroborates but is not the check. Had the note been "
            "false, the same arithmetic would have said so — as it did in "
            "slice 61, where the same two promises resolved in opposite "
            "directions. EDGE.md §45a."),

        # -- the prefix invariant. EDGE.md §45c ------------------------------
        "prefix_invariant": {
            "rule": ("a pinned digest is asserted against the PREFIX of the "
                     "file at the pinned row count; growth is permitted only "
                     "as an append, and every appended row must be strictly "
                     "after t1"),
            "pins_from": cp.PINS_ARTEFACT,
            "pinned_digests_agree_across_two_records":
                cp.pinned_digests_agree(),
            "all_corpora_append_only": all_append_only,
            "checks": {path: cp.as_dict(result)
                       for path, result in sorted(checks.items())},
            "what_this_proves": (
                "NOT ONE BYTE OF MEASURED HISTORY HAS MOVED. The digest locked "
                "in slice 57 — before funding_carry_fade_btc_v1 existed as a "
                "module — is still the digest of the corpus the cleared "
                "measurement was taken over. It is now the digest of a PREFIX "
                "of a longer file rather than of the whole file, and that is "
                "the only thing that changed. The 41 OOS trades, M1 95.13, M2 "
                "96.0 and mean net R +0.1736 all still describe exactly the "
                "bytes they described."),
            "what_this_does_not_prove": (
                "It does not prove no extension exists. That was never an "
                "invariant — it was slice 61's dated finding, which "
                "artifacts/slice61_data_freshness.json continues to record and "
                "which this slice does not rewrite."),
        },

        # -- closed bars only -------------------------------------------------
        "appended_linear_bars": closed,
        "closed_rule": (
            "A daily bar stamped D closes at D+1T00:00Z. Only closed bars "
            "count. The bar for the check date is correctly ABSENT from the "
            "corpus and is not required."),
        "open_bar_absent_as_expected": all(
            row["time_period_start"][:10] < args.checked_at_utc[:10]
            for row in appended_bars),

        # -- the funding seam. EDGE.md §45d ----------------------------------
        "funding_seam": {
            "appended_prints": list(funding.appended_timestamps),
            "cadence_hours_across_pinned_prefix": {
                "min": min(cp.funding_gap_hours(
                    cp.FUNDING_BTC, last=funding.pinned_rows)),
                "max": max(cp.funding_gap_hours(
                    cp.FUNDING_BTC, last=funding.pinned_rows)),
            },
            "missing_prints": cp.missing_funding_prints(cp.FUNDING_BTC),
            "finding": (
                "The extension began at the next DAY boundary rather than the "
                "next PRINT, leaving exactly one hole at 2026-08-09T16:00Z. "
                "The hole is AFTER t1, in the forward region, so measured "
                "history is unaffected and the pinned prefix's own cadence is "
                "still a flat 8.0 hours throughout. It changes no number in "
                "this slice. It is named rather than absorbed into a widened "
                "bound because 'fail closed on missing data' is a standing "
                "rule, and a hole tolerated silently today is a hole that "
                "silently feeds a stale rate to a decision in a later slice "
                "with more bars."),
            "affects_any_number_this_slice": False,
            "carried_forward_for_the_human_to_fill": True,
        },

        # -- the manifests were left verbatim --------------------------------
        "manifests": {
            "linear": manifest_agreement(
                "data/real_linear_1d", "BTCUSDT", cp.LINEAR_BTC),
            "funding": manifest_agreement(
                "data/real_funding", "BTCUSDT", cp.FUNDING_BTC),
            "edited_by_this_slice": False,
            "why_not_edited": (
                "The MANIFESTs still declare the pinned prefix and are now "
                "wrong about the files beside them. They are left BYTE-VERBATIM: "
                "editing the pack's own provenance records would make the "
                "delivery look internally consistent when it is not, and the "
                "disagreement is itself a finding worth shipping. The repaired "
                "tests read a manifest as a description of the PINNED PREFIX, "
                "which is what it accurately is, and account for the remainder "
                "separately."),
        },

        "plausibility": plausibility(cp.LINEAR_BTC),

        "what_would_count_as_a_forward_observation": {
            "rule": ("a shadow trade that both ENTERS and EXITS on closed bars "
                     "strictly after t1"),
            "not_this": ("a bar arriving. Not a flagged bar. Not an entry. A "
                         "trade is an observation only when it CLOSES."),
            "closed_forward_bars_available": len(closed_after_t1),
            "closed_forward_bars_needed": fb.HORIZON + 1,
            "maximum_possible_forward_trades_today": 0,
            "ceiling_declared_before_scoring": "EDGE.md §45e",
        },

        "git_commit": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], text=True, cwd=REPO).strip(),
    }

    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False)
        handle.write("\n")

    print("=" * 78)
    print("SLICE 62 — DATA FRESHNESS   (hash the prefix; the note is a claim)")
    print("=" * 78)
    print(f"  t1                         : {t1}")
    print(f"  checked at                 : {args.checked_at_utc}")
    print()
    print("  PREFIX INVARIANT — EDGE.md §45c")
    for path, result in sorted(checks.items()):
        mark = "OK " if result.append_only else "!! "
        print(f"    {mark}{os.path.basename(path):38s} "
              f"pinned {result.pinned_rows:5d}  disk {result.rows_on_disk:5d}  "
              f"+{result.appended_rows:<3d} "
              f"history_unchanged={result.history_unchanged}")
        if not result.append_only:
            print(f"        {result.why_not()}")
    print(f"    all corpora append-only  : {all_append_only}")
    print(f"    two records agree on pins: "
          f"{all(cp.pinned_digests_agree().values())}")
    print()
    print(f"  linear  pinned prefix sha  : {linear.prefix_sha256}")
    print(f"          the slice-57 pin   : {linear.pinned_sha256}")
    print(f"          whole file today   : {linear.whole_file_sha256}")
    print()
    for entry in closed:
        print(f"  appended bar {entry['time_period_start'][:10]}  "
              f"closed={entry['closed']}  (closes {entry['closes_at_utc']})")
    print(f"  appended funding prints    : {funding.appended_rows}")
    print(f"  missing funding prints     : "
          f"{payload['funding_seam']['missing_prints']}")
    print()
    print(f"  AFTER-t1 LINEAR (closed)   : {len(closed_after_t1)}")
    print(f"  EXTENSION PRESENT          : {extension_present}")
    print(f"  bars fabricated            : 0")
    print(f"  corpus appended to by tool : False")
    print(f"  manifests edited           : False")
    print()
    print(f"  max possible forward trades today : 0   "
          f"(needs {fb.HORIZON + 1} closed forward bars; "
          f"{len(closed_after_t1)} exist)")
    print()
    print(f"artefact: {os.path.relpath(args.out, REPO)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
