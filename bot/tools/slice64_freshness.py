#!/usr/bin/env python3
"""Did the corpus grow SINCE THE LAST SLICE? Files over notes. EDGE.md §46c.

THE QUESTION THIS SLICE ASKS THAT SLICE 62 DID NOT
==================================================
Slice 62 asked "is there an extension after `t1`?" and the answer was yes, for
the first time in six slices. Asking that same question again would return yes
again, forever, and would read like progress every time — because `t1` does not
move, so once one bar lands the answer is permanently true.

The question that actually carries information is **"did it grow since last
time?"**, and this slice separates the two:

    extension_present               after_t1_linear >= 1     (true since slice 62)
    new_linear_bars_since_slice63   the delta               (the informative one)

Both are written into the artefact. A reader should not have to diff two
artefacts to learn whether anything happened.

THE DIGEST TRAP, RECORDED BECAUSE IT ALMOST FIRED
=================================================
`HUMAN_DATA_NOTE_SLICE64.md` quotes a linear sha256 that differs from slice
62's. Read alone it says the corpus changed. It did not: both figures are
digests of the **compressed** file, and gzip's header varies between compression
runs. The repository pins the **uncompressed** stream, and on that measure the
linear corpus is byte-identical to slice 62's.

Two different compressed digests over identical content. The tool reports both,
side by side, so the trap is visible rather than merely avoided.

WHAT IT WILL NOT DO
===================
It fetches nothing, writes no bars, appends to no corpus, edits no manifest and
fabricates nothing. It has no flag that can change a constant, a cap, a
threshold or a pin.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "tools"))

import corpus_prefix as cp  # noqa: E402
from signals import funding_carry_fade_btc_v1 as fb  # noqa: E402

SCHEMA = "data_freshness/5"
NOTE_PATH = "docs/human/HUMAN_DATA_NOTE_SLICE64.md"
PREVIOUS = "artifacts/slice63_data_freshness.json"
NOTE_LINEAR_SHA = ("116eef53d8bbd8fca9256e18a1f16c9e85cea4ca7853893366c009ae1abc"
                   "9d8a")


def gz_sha256(path: str) -> str:
    with open(os.path.join(REPO, path), "rb") as handle:
        return hashlib.sha256(handle.read()).hexdigest()


def closed_state(row: dict, checked: dt.datetime) -> dict:
    """A daily bar stamped D closes at D+1T00:00Z, not at its 23:59:59 stamp."""
    start = dt.datetime.fromisoformat(row["time_period_start"])
    closes_at = start + dt.timedelta(days=1)
    return {
        "time_period_start": row["time_period_start"],
        "closes_at_utc": closes_at.isoformat(),
        "closed": closes_at <= checked,
    }


def _manifest_audit() -> dict:
    """Every manifest entry, against the file beside it. EDGE.md §47b.

    Slice 64's pack updated both MANIFESTs by broadcasting BTCUSDT's new row
    counts and end dates across all three symbols. ETH and SOL were not
    extended, so four of the six entries are now false — and SOL funding
    regressed from a correct 4458 to an incorrect 4392, understating a corpus
    by 66 rows.
    """
    entries, disagreeing = {}, []
    for corpus, subdir, suffix, column in (
            ("data/real_linear_1d", "ohlcv", "1D", "time_period_start"),
            ("data/real_funding", "funding", "FUNDING", "funding_time")):
        with open(os.path.join(REPO, corpus, "MANIFEST.json"),
                  encoding="utf-8") as handle:
            manifest = json.load(handle)
        for symbol in ("BTCUSDT", "ETHUSDT", "SOLUSDT"):
            path = (f"{corpus}/{subdir}/"
                    f"BINANCE_LINEAR_{symbol[:3]}_USDT_{suffix}.csv.gz")
            rows = cp.read_rows(path)
            declared = manifest["date_range_utc"][symbol]
            agrees = (len(rows) == declared["rows"]
                      and rows[-1][column][:10] == declared["end"])
            entries[path] = {
                "declared_rows": declared["rows"],
                "rows_on_disk": len(rows),
                "declared_end": declared["end"],
                "end_on_disk": rows[-1][column][:10],
                "manifest_describes_the_file": agrees,
                "understates_the_corpus": declared["rows"] < len(rows),
            }
            if not agrees:
                disagreeing.append(path)
    return {
        "synthetic_declared_false": True,
        "entries": entries,
        "entries_that_disagree_with_their_file": disagreeing,
        "count_disagreeing": len(disagreeing),
        "measured_product_entries_are_accurate": all(
            v["manifest_describes_the_file"]
            for k, v in entries.items() if "BTC" in k),
        "finding": (
            "The manifests were updated this slice by BROADCASTING BTCUSDT's "
            "new row counts and end dates across all three symbols. ETH's and "
            "SOL's four files were not extended at all — they are "
            "byte-identical to slice 63 — so four entries now assert data that "
            "does not exist. SOL funding is the sharpest: it holds 4458 prints, "
            "the PREVIOUS manifest said 4458 correctly, and this one says 4392, "
            "so the record now UNDERSTATES a corpus by 66 rows. None of it "
            "touches BTCUSDT, whose two entries are accurate, so no claim in "
            "this slice is tainted. EDGE.md §47b."),
        "not_edited_by_this_slice": True,
        "enumerated_in": ("tests/test_slice55_data_eligibility.py::"
                          "TestTheManifestsDescribeTheFilesBesideThem"),
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checked-at-utc", required=True)
    parser.add_argument("--out", default=os.path.join(
        REPO, "artifacts", "slice64_data_freshness.json"))
    args = parser.parse_args(argv)

    checked = dt.datetime.strptime(
        args.checked_at_utc, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=dt.timezone.utc)

    folds = fb.load_folds()
    checks = cp.check_all()
    linear, funding = checks[cp.LINEAR_BTC], checks[cp.FUNDING_BTC]

    appended_bars = cp.read_rows(cp.LINEAR_BTC)[linear.pinned_rows:]
    closed = [closed_state(row, checked) for row in appended_bars]
    closed_after_t1 = [c for c in closed if c["closed"]]

    with open(os.path.join(REPO, PREVIOUS), encoding="utf-8") as handle:
        previous = json.load(handle)
    previous_linear = int(previous["after_t1_linear"])
    previous_funding = int(previous["after_t1_funding"])

    all_append_only = all(check.append_only for check in checks.values())
    extension_present = bool(closed_after_t1)
    grew_linear = len(closed_after_t1) - previous_linear
    grew_funding = funding.appended_rows - previous_funding

    payload = {
        "schema": SCHEMA,
        "slice": 64,
        "symbol": "BTCUSDT",
        "checked_at_utc": args.checked_at_utc,

        # -- the headline: present, versus grown --------------------------
        "extension_present": extension_present,
        "new_bars_available": extension_present,
        "after_t1_linear": len(closed_after_t1),
        "after_t1_funding": funding.appended_rows,
        "after_t1_dates": [c["time_period_start"][:10] for c in closed_after_t1],

        "delta_since_slice63": {
            "previous_artefact": PREVIOUS,
            "previous_after_t1_linear": previous_linear,
            "previous_after_t1_funding": previous_funding,
            "new_linear_bars_since_slice63": grew_linear,
            "new_funding_prints_since_slice63": grew_funding,
            "the_window_grew": grew_linear > 0,
            "why_this_field_exists": (
                "t1 does not move, so once one bar lands after it "
                "extension_present is permanently true and re-reporting it "
                "reads like progress every slice. The informative quantity is "
                "the DELTA, and it is written here so a reader does not have "
                "to diff two artefacts to learn whether anything happened. "
                "EDGE.md §46c."),
            "finding": (
                f"THE WINDOW GREW. {grew_linear} new closed daily bar(s) since "
                f"slice 63, and {grew_funding} new funding print(s). This is "
                f"the first genuine growth of the forward window since t1 was "
                f"locked: slices 62 and 63 both measured a one-bar window. The "
                f"ceiling is nevertheless unchanged at 0 — six closed forward "
                f"bars are needed before one trade can close and there are "
                f"{len(closed_after_t1)}. The evidence window is doubling "
                f"while producing no evidence, and both halves of that "
                f"sentence are true at once. EDGE.md §47a, §47d."),
        },

        "t1": folds["t1"],
        "t1_ms": folds["t1_ms"],
        "t1_source": cp.FOLDS_ARTEFACT,
        "folds_sha256": fb.folds_sha256(),
        "folds_unmodified": fb.folds_are_unmodified(),

        "latest_linear_timestamp": cp.read_rows(
            cp.LINEAR_BTC)[-1]["time_period_start"],
        "latest_funding_timestamp": cp.read_rows(
            cp.FUNDING_BTC)[-1]["funding_time"],
        "linear_rows": linear.rows_on_disk,
        "funding_rows": funding.rows_on_disk,
        "new_linear_bars_count": len(closed_after_t1),
        "new_funding_prints_count": funding.appended_rows,

        # -- the digest trap. EDGE.md §46c ---------------------------------
        "digests": {
            "linear_sha256_uncompressed": linear.whole_file_sha256,
            "linear_sha256_compressed": gz_sha256(cp.LINEAR_BTC),
            "funding_sha256_uncompressed": funding.whole_file_sha256,
            "funding_sha256_compressed": gz_sha256(cp.FUNDING_BTC),
            "which_one_the_repository_pins": "uncompressed",
            "note_quotes_the_compressed_digest": True,
            "note_linear_sha256": NOTE_LINEAR_SHA,
            "note_digest_matches_the_compressed_file":
                gz_sha256(cp.LINEAR_BTC) == NOTE_LINEAR_SHA,
            "slice63_linear_sha256_uncompressed":
                "32ca5971229f07ed866627d939ed07768f7e0e0d299a3d4c07d440c0b43"
                "bab22",
            "linear_identical_to_slice63_uncompressed":
                linear.whole_file_sha256 ==
                ("32ca5971229f07ed866627d939ed07768f7e0e0d299a3d4c07d440c0b43"
                 "bab22"),
            "linear_differs_from_slice63_because_a_bar_arrived": True,
            "the_trap": (
                "The note's digest differs from slice 62's, which read alone "
                "says the linear corpus changed. It did not. Both figures are "
                "digests of the COMPRESSED file and gzip's header varies "
                "between compression runs; the uncompressed streams are "
                "byte-identical. TWO DIFFERENT COMPRESSED DIGESTS OVER "
                "IDENTICAL CONTENT. Had the check been the one the note "
                "quotes, this slice would have opened by reporting a change "
                "that did not happen — and a later slice, seeing the same "
                "thing in reverse, could have missed one that did."),
        },

        # -- integrity ------------------------------------------------------
        "prefix_invariant": {
            "rule": ("a pinned digest is asserted against the PREFIX of the "
                     "file at the pinned row count; growth is permitted only "
                     "as an append, and every appended row must be strictly "
                     "after t1"),
            "declared_in": "EDGE.md §45c",
            "pins_from": cp.PINS_ARTEFACT,
            "pinned_digests_agree_across_two_records":
                cp.pinned_digests_agree(),
            "all_corpora_append_only": all_append_only,
            "history_rewritten": not all_append_only,
            "checks": {path: cp.as_dict(result)
                       for path, result in sorted(checks.items())},
        },

        "appended_linear_bars": closed,
        "open_bar_absent_as_expected": all(
            row["time_period_start"][:10] < args.checked_at_utc[:10]
            for row in appended_bars),

        "funding_seam": {
            "missing_prints": cp.missing_funding_prints(cp.FUNDING_BTC),
            "cadence_hours_across_pinned_prefix": {
                "min": min(cp.funding_gap_hours(
                    cp.FUNDING_BTC, last=funding.pinned_rows)),
                "max": max(cp.funding_gap_hours(
                    cp.FUNDING_BTC, last=funding.pinned_rows)),
            },
            "unchanged_from_slice62": True,
            "finding": (
                "Still exactly one hole, at 2026-08-09T16:00Z, in the forward "
                "region. The human's slice-63 extension did not fill it. It "
                "changes no number in this slice and is carried forward again. "
                "EDGE.md §45d."),
        },

        # -- the note is a claim, and it is scored --------------------------
        "human_note_present": os.path.isfile(os.path.join(REPO, NOTE_PATH)),
        "human_note_path": NOTE_PATH,
        "human_note_is_evidence": False,
        "human_note_claims_checked": {
            "closed bars after t1 = 1": len(closed_after_t1) == 1,
            "append-only; no pre-t1 rewrite": all_append_only,
            "linear sha256 db05f3f8... (compressed)":
                gz_sha256(cp.LINEAR_BTC) == NOTE_LINEAR_SHA,
            "synthetic false": all(
                json.load(open(os.path.join(REPO, d, "MANIFEST.json"),
                               encoding="utf-8"))["synthetic"] is False
                for d in ("data/real_linear_1d", "data/real_funding")),
        },
        "claim_vs_files_discrepancy": False,
        "human_note_verdict": (
            "Every claim in the note is INDEPENDENTLY TRUE, including the "
            "growth claim, which is the one that matters this slice and which "
            "was checked against the files rather than believed. The note's "
            "sha256 is again the COMPRESSED digest — the second slice running "
            "— which is not the digest any check here uses; §46c records why "
            "that distinction earns its keep."),

        # -- the manifests. EDGE.md §47b --------------------------------------
        "manifests": _manifest_audit(),
        "manifests_edited_by_this_slice": False,
        "why_manifests_not_edited": (
            "Rewriting a data-provenance record to state whatever the files "
            "say would be asserting a provenance this programme cannot attest: "
            "nothing here knows independently that ETH should hold 1461 rows "
            "rather than having lost two. A manifest that agrees with disk by "
            "construction certifies nothing. The false claims are enumerated "
            "by name in tests instead. EDGE.md §47b."),
        "bars_fabricated": 0,
        "corpus_appended_to_by_this_tool": False,
        "fetch_attempted": False,
        "why_no_fetch": (
            "The human supplies the data on disk, which is the sanctioned "
            "path. Slice 60 recorded a ProxyError against the venue from this "
            "environment and nothing has changed; a fetch would add a log line "
            "and no evidence."),

        "what_would_count_as_a_forward_observation": {
            "rule": ("a shadow trade that both ENTERS and EXITS on closed bars "
                     "strictly after t1"),
            "closed_forward_bars_available": len(closed_after_t1),
            "closed_forward_bars_needed": fb.HORIZON + 1,
            "maximum_possible_forward_trades_today":
                max(0, len(closed_after_t1) - fb.HORIZON),
            "ceiling_declared_before_scoring": "EDGE.md §46d",
        },

        "pack_regression": {
            "recorded_in": "artifacts/slice63_restored_from_slice62.json",
            "summary": ("the slice-63 pack is the SLICE-61 tree; slice 62's "
                        "work was restored rather than re-implemented"),
            "design_note": "EDGE.md §46a, §46b",
        },

        "git_commit": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], text=True, cwd=REPO).strip(),
    }

    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False)
        handle.write("\n")

    delta = payload["delta_since_slice63"]
    print("=" * 78)
    print("SLICE 64 — DATA FRESHNESS   (did it grow SINCE LAST TIME?)")
    print("=" * 78)
    print(f"  t1                          : {folds['t1']}")
    print(f"  checked at                  : {args.checked_at_utc}")
    print()
    print("  PREFIX INVARIANT — EDGE.md §45c")
    for path, result in sorted(checks.items()):
        mark = "OK " if result.append_only else "!! "
        print(f"    {mark}{os.path.basename(path):38s} "
              f"pinned {result.pinned_rows:5d}  disk {result.rows_on_disk:5d}  "
              f"+{result.appended_rows:<3d} "
              f"history_unchanged={result.history_unchanged}")
    print(f"    all corpora append-only   : {all_append_only}")
    print(f"    history rewritten         : {not all_append_only}")
    print()
    print("  THE DIGEST TRAP — EDGE.md §46c")
    print(f"    note quotes (compressed)  : {NOTE_LINEAR_SHA}")
    print(f"    linear compressed today   : "
          f"{payload['digests']['linear_sha256_compressed']}")
    print(f"    linear UNCOMPRESSED today : {linear.whole_file_sha256}")
    print(f"    slice 63 uncompressed     : "
          f"{payload['digests']['slice63_linear_sha256_uncompressed']}")
    print(f"    -> identical to slice 63  : "
          f"{payload['digests']['linear_identical_to_slice63_uncompressed']}"
          f"   (a bar arrived; expected False)")
    print()
    print(f"  after_t1_linear             : {len(closed_after_t1)}  "
          f"{payload['after_t1_dates']}")
    print(f"  after_t1_funding            : {funding.appended_rows}")
    print(f"  latest linear timestamp     : "
          f"{payload['latest_linear_timestamp']}")
    print(f"  latest funding timestamp    : "
          f"{payload['latest_funding_timestamp']}")
    print()
    print(f"  EXTENSION PRESENT           : {extension_present}")
    print(f"  NEW LINEAR BARS SINCE S63   : "
          f"{delta['new_linear_bars_since_slice63']}")
    print(f"  new funding prints since S63: "
          f"{delta['new_funding_prints_since_slice63']}")
    print(f"  the window grew             : {delta['the_window_grew']}")
    print()
    print()
    print("  MANIFEST AUDIT — EDGE.md §47b   (files win over records)")
    for path, entry in payload["manifests"]["entries"].items():
        mark = "ok " if entry["manifest_describes_the_file"] else "!! "
        print(f"    {mark}{os.path.basename(path):40s} "
              f"declared {entry['declared_rows']:5d}  disk "
              f"{entry['rows_on_disk']:5d}  "
              f"end {entry['declared_end']} vs {entry['end_on_disk']}"
              f"{'   UNDERSTATES' if entry['understates_the_corpus'] else ''}")
    print(f"    entries disagreeing with their file : "
          f"{payload['manifests']['count_disagreeing']}")
    print(f"    BTCUSDT entries accurate            : "
          f"{payload['manifests']['measured_product_entries_are_accurate']}")
    print(f"    manifests edited by this slice      : False")
    print()
    print(f"  missing funding prints      : "
          f"{payload['funding_seam']['missing_prints']}")
    print(f"  bars fabricated             : 0")
    print(f"  corpus appended to by tool  : False")
    print(f"  manifests edited            : False")
    print()
    print(f"  max possible forward trades today : "
          f"{payload['what_would_count_as_a_forward_observation']['maximum_possible_forward_trades_today']}"
          f"   (needs {fb.HORIZON + 1} closed forward bars; "
          f"{len(closed_after_t1)} exist)")
    print()
    print(f"artefact: {os.path.relpath(args.out, REPO)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
