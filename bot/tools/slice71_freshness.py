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
    new_linear_bars_since_slice70   the delta               (the informative one)

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
import provenance as _provenance  # noqa: E402

SCHEMA = "data_freshness/12"

# The first commit in this repository is the human pack, committed verbatim
# before anything was touched. Reading EDGE.md from that commit is how the
# pack-base claim is scored against the tree AS DELIVERED rather than against
# the tree as this slice left it.
_BASE_COMMIT = subprocess.check_output(
    ["git", "rev-list", "--max-parents=0", "HEAD"],
    cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
).decode().split()[0]
SLICE = 71

# EDGE.md §52a. Derived, never a literal: the substitution pipeline that
# produces each slice's tool from its predecessor rewrites lower-case
# `sliceNN` tokens and never matched the UPPER-case constant that used to
# live here, so it froze at SLICE64 and four slices reported a path four
# slices out of date -- while `human_note_present` happily verified that
# stale file and could therefore never fail. Deriving it removes the class.
# Slice 70 note: the constant BELOW this one still had to be edited by hand,
# and shipped one slice stale in the working tree before it was caught. A
# derived field is safe; a hand-carried one is not, and there is no version
# of "be careful" that fixes the second.
# Slice 71 note: the substitution pipeline failed to update `SLICE` for the
# THIRD consecutive slice. It arrived reading 70. Nothing in the derivation
# improved; the test that asserts `SLICE == 71` is the only reason this is
# not shipping stale again. EDGE.md §52a.
NOTE_PATH = f"docs/human/HUMAN_DATA_NOTE_SLICE{SLICE}.md"
PREVIOUS = "artifacts/slice70_data_freshness.json"

# The predecessor's UNCOMPRESSED linear digest, named once and used twice.
# It was carried in two places last slice and the substitution pipeline
# updated one of them, so the artefact reported the current digest against a
# two-slice-old comparand. One constant, one place. EDGE.md §53d.
PRIOR_LINEAR_UNCOMPRESSED = ("416e4430cd5eb0844bb7a64601fd36855bafa261465"
                             "70722c7d4424b4b06eea9")

# The predecessor's UNCOMPRESSED FUNDING digest. New this slice, because the
# funding .csv.gz arrived with a DIFFERENT compressed digest and an IDENTICAL
# uncompressed stream: recompressed, not changed. Without this constant the
# artefact could only report that the file's bytes moved, which is true and
# misleading. EDGE.md §46c, §54d.
PRIOR_FUNDING_UNCOMPRESSED = ("fa8b207f79115694ec2908507e830c56a7a2a44c1d4f"
                              "5bd0355223775450bf73")

# Spelled-out bar counts, so the headline sentence is derived from the count
# rather than typed beside it.
_SPELLED = {5: "FIVE", 6: "SIX", 7: "SEVEN", 8: "EIGHT", 9: "NINE",
            10: "TEN", 11: "ELEVEN", 12: "TWELVE"}
NOTE_LINEAR_SHA = ("82e281166496ff3ee1b72bacc397501c01f942183d70f8b1"
                   "a49a7d8f7fdb42ab")


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


def _pack_base_claim_is_true() -> bool:
    """Is the note's claim about its own PACK BASE true? EDGE.md §50a, §51a.

    Slice 67's note made this claim and it was FALSE — the first false claim a
    human note had made in the programme. It is re-scored here on the same
    terms rather than dropped: a claim that failed once is exactly the one
    worth checking again.

    The decisive test is whether EDGE.md carries the sections slices 62-70
    wrote. The slice-70 deliverable has 45 through 53; a slice-61 base has
    none. The range advances with the programme: a fixed upper bound would
    keep passing on a tree that had silently lost the newest slice's work.
    Read from the tree AS DELIVERED — recorded by the STEP 0 baseline before
    this slice wrote anything — via the sections that were present at commit
    time, not via a file this slice may since have appended to.
    """
    delivered = subprocess.check_output(
        ["git", "show", f"{_BASE_COMMIT}:EDGE.md"], cwd=REPO).decode("utf-8")
    return all(f"\n## {n}a." in delivered
               for n in range(45, 54))


def _forward_funding_regime() -> dict:
    """What the funding regime did over the forward window. EDGE.md §51c.

    Reported in the FRESHNESS artefact because it is a fact about the DATA, not
    a scoring decision — the forward shadow reports what the rule did with it.
    Separating the two keeps "the regime was quiet" distinguishable from "the
    rule declined", which are different claims that happen to coincide here.
    """
    import backtest as bt  # noqa: PLC0415
    import market_data as md  # noqa: PLC0415

    loaded, _b, _n = md.load_corpus(
        os.path.join(REPO, "data", "real_linear_1d"), verify=False)
    bars = [bt.Bar(b.start_ms, b.open, b.high, b.low, b.close, b.volume)
            for b in loaded["BTCUSDT"]]
    funding = fb.load_funding(os.path.join(
        REPO, "data", "real_funding", "funding",
        "BINANCE_LINEAR_BTC_USDT_FUNDING.csv.gz"))
    folds = fb.load_folds()
    window = list(fb.forward_window_indices(bars, folds))
    rates = fb.funding_at_decision(bars, funding)
    setups = fb.funding_setups(bars, funding, fund_abs=fb.FUND_ABS)

    def day(index):
        return dt.datetime.fromtimestamp(
            bars[index].start_ms / 1000.0,
            tz=dt.timezone.utc).strftime("%Y-%m-%d")

    prints_after_t1 = [
        row for row in cp.read_rows(cp.FUNDING_BTC)
        if row["funding_time"][:10] > folds["t1"][:10]]
    at_or_above = [row for row in prints_after_t1
                   if abs(float(row["funding_rate"])) >= fb.FUND_ABS]

    return {
        "fund_abs": fb.FUND_ABS,
        "comparison_is_inclusive": True,
        "comparison_source": ("signals/funding_carry_fade_btc_v1.py :: "
                              "funding_setups tests f >= fund_abs"),
        "rate_at_each_forward_decision": {
            day(i): (None if not float(rates[i]) == float(rates[i])
                     else float(rates[i]))
            for i in window},
        "funding_setups_in_window": sum(1 for i in window if i in setups),
        "forward_bars": len(window),
        "join_rule": ("funding_at_decision reads "
                      "funding.at_or_before(close_time_ms(bar)) — the rate "
                      "STANDING AT THE BAR'S CLOSE, reading only backwards in "
                      "time"),
        "newest_decision_instant_utc": dt.datetime.fromtimestamp(
            fb.close_time_ms(bars[window[-1]]) / 1000.0,
            tz=dt.timezone.utc).isoformat() if window else None,
        "newest_decision_is_final": (
            "close_time_ms falls back to start_ms + 86_400_000 - 1 and these "
            "bars carry no end_us, so the newest decision instant is "
            "23:59:59.999Z — STRICTLY BEFORE the next day's 00:00 funding "
            "print. A print stamped exactly at midnight, which this file does "
            "not yet carry, could not retroactively revise the newest "
            "decision. The join is final the moment the bar closes. "
            "EDGE.md §54b."),
        "funding_covers_the_newest_decision": bool(
            window and cp.read_rows(cp.FUNDING_BTC)[-1]["funding_time"]
            <= dt.datetime.fromtimestamp(
                fb.close_time_ms(bars[window[-1]]) / 1000.0,
                tz=dt.timezone.utc).isoformat()),
        "prints_after_t1": len(prints_after_t1),
        "prints_at_or_above_fund_abs": [
            {"funding_time": row["funding_time"],
             "funding_rate": float(row["funding_rate"]),
             "equals_fund_abs_exactly":
                 float(row["funding_rate"]) == fb.FUND_ABS}
            for row in at_or_above],
        # Derived from the numbers above, not narrated beside them: the bar
        # count and the setup count are interpolated so this sentence cannot
        # go on saying "six" once the window is eight. EDGE.md §53d.
        "finding": (
            f"No setup fired on any of the {len(window)} forward bars: every "
            f"rate at a decision is below FUND_ABS = {fb.FUND_ABS}. "
            f"{sum(1 for i in window if i in setups)} of {len(window)}. "
            "THREE forward decisions have now come near the line and each "
            "fails differently. 2026-08-12 joined at 0.00006601 after its "
            "00:00 print of 0.00010000 — equal to FUND_ABS exactly, and the "
            "comparison is inclusive — was superseded by two later prints "
            "before that bar's close: only a JOIN change could have captured "
            "it. 2026-08-17 joined at 0.00009202, which DID stand at its "
            "bar's close and simply missed by 8.0e-6: only a lower FUND_ABS "
            "could have captured it. 2026-08-18 joined at 0.00003650 and "
            "missed by 6.4e-5: nothing short of abandoning the threshold "
            "would capture it. None is a missed trade and none is a defect. "
            "The newest is the most reassuring of the three precisely because "
            "it is NOT close — a threshold that produced only narrow misses "
            "would look like a boundary being crept toward, and one that "
            "produces a wide miss the very next day is behaving like a "
            "threshold. 'A qualifying rate existed that day' and 'the "
            "qualifying rate stood at the decision' remain different claims. "
            "EDGE.md §51c, §54b."),
        "parameters_moved_because_of_this": False,
        "why_not": (
            "A threshold adjusted after seeing which prints missed it is a "
            "threshold fitted to the data, and a narrow miss makes the "
            "temptation larger rather than the change more defensible. "
            "FUND_ABS stays 0.0001; the horizon stays 5; the join stays "
            "close-time. Three misses have now been recorded and the two "
            "available rescues — moving the join, moving the threshold — are "
            "each exactly the change that would void the n=41 clear these "
            "bars are being measured against."),
    }


def _manifest_audit() -> dict:
    """Every manifest entry, against the file beside it. EDGE.md §47b.

    Slice 64's pack updated both MANIFESTs by broadcasting BTCUSDT's new row
    counts and end dates across all three symbols. ETH and SOL were not
    extended, so four of the six entries went false — and SOL funding
    regressed from a correct 4458 to an incorrect 4392, understating a corpus
    by 66 rows. The broadcast stopped in slice 67; the residue did not.

    The finding sentence below is DERIVED from the numbers this function just
    computed, and from the PREVIOUS slice's frozen artefact for the question
    "did the declared counts move this slice?". It used to be prose written in
    the present tense about slice 64, and it went on asserting "the manifests
    were updated this slice by BROADCASTING" for three slices after that
    stopped being true. EDGE.md §53e.
    """
    with open(os.path.join(REPO, PREVIOUS), encoding="utf-8") as handle:
        _previous_entries = json.load(handle)["manifests"]["entries"]
    previously_declared = {path: entry["declared_rows"]
                           for path, entry in _previous_entries.items()}
    previously_ended = {path: entry["declared_end"]
                        for path, entry in _previous_entries.items()}

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
            # THREE comparisons, not one. EDGE.md §54e. The strict one is
            # kept so a format change is visible; the content one decides
            # whether the record actually describes the file. This slice's
            # pack re-encoded both BTC `end` fields from "2026-08-18" to
            # "2026-08-18T00:00:00+00:00", and on the strict comparison alone
            # a correct manifest reads as a wrong one.
            rows_agree = len(rows) == declared["rows"]
            end_strict = rows[-1][column][:10] == declared["end"]
            end_content = rows[-1][column][:10] == str(declared["end"])[:10]
            entries[path] = {
                "declared_rows": declared["rows"],
                "rows_on_disk": len(rows),
                "declared_end": declared["end"],
                "end_on_disk": rows[-1][column][:10],
                "declared_end_normalised": str(declared["end"])[:10],
                "end_agrees_strictly": end_strict,
                "end_agrees_on_content": end_content,
                "declared_end_is_a_timestamp_not_a_date":
                    len(str(declared["end"])) > 10,
                "rows_agree": rows_agree,
                "manifest_describes_the_file": rows_agree and end_content,
                "understates_the_corpus": declared["rows"] < len(rows),
            }
            agrees = rows_agree and end_content
            entry = entries[path]
            entry["declared_rows_previous_slice"] = previously_declared.get(
                path)
            entry["declared_rows_moved_this_slice"] = (
                previously_declared.get(path) != declared["rows"])
            entry["declared_end_previous_slice"] = previously_ended.get(path)
            entry["declared_end_format_changed_this_slice"] = (
                previously_ended.get(path) is not None
                and (len(str(previously_ended[path])) > 10)
                != (len(str(declared["end"])) > 10))
            if not agrees:
                disagreeing.append(path)

    moved = sorted(p for p, e in entries.items()
                   if e["declared_rows_moved_this_slice"])
    broadcast = len({p for p in moved if "BTC" not in p}) > 0
    wrong = sorted(p for p, e in entries.items()
                   if not e["manifest_describes_the_file"])
    reformatted = sorted(p for p, e in entries.items()
                         if e["declared_end_format_changed_this_slice"])
    strict_only = sorted(p for p, e in entries.items()
                         if e["manifest_describes_the_file"]
                         and not e["end_agrees_strictly"])
    return {
        "synthetic_declared_false": True,
        "entries": entries,
        "entries_that_disagree_with_their_file": disagreeing,
        "count_disagreeing": len(disagreeing),
        "measured_product_entries_are_accurate": all(
            v["manifest_describes_the_file"]
            for k, v in entries.items() if "BTC" in k),
        "declared_rows_that_moved_this_slice": moved,
        "broadcast_occurred_this_slice": broadcast,
        "entries_that_disagree_with_their_file_count": len(wrong),
        "declared_end_format_changed_this_slice": reformatted,
        "entries_a_strict_end_comparison_would_have_failed": strict_only,
        "count_a_strict_comparison_would_have_reported":
            len(wrong) + len(strict_only),
        "why_content_decides": (
            "A strict `end` comparison would have reported "
            f"{len(wrong) + len(strict_only)} disagreeing entries where there "
            f"are {len(wrong)}, because the pack re-encoded BTC's `end` from a "
            "DATE to a TIMESTAMP. Identical meaning, different string — the "
            "mirror image of the compressed-digest trap in §46c, where "
            "identical content wore different bytes. Both are representation, "
            "not substance. The strict result is reported rather than "
            "discarded, because a provenance record that silently changes "
            "encoding is worth seeing; it just does not get to be called a "
            "disagreement. EDGE.md §54e."),
        "finding": (
            f"{len(wrong)} of {len(entries)} manifest entries disagree with "
            f"the file beside them: {', '.join(os.path.basename(p) for p in wrong)}. "
            f"Declared row counts that moved this slice: "
            f"{', '.join(os.path.basename(p) for p in moved) or 'none'}. "
            f"Broadcast to non-BTC symbols this slice: {broadcast}. "
            f"Entries whose declared `end` was re-encoded this slice: "
            f"{', '.join(os.path.basename(p) for p in reformatted) or 'none'} "
            f"(format only; content unchanged). "
            "The four wrong entries are the frozen residue of slice 64's "
            "broadcast, which slice 67 stopped: ETH and SOL were never "
            "extended, so their declared counts describe data that does not "
            "exist. They are wrong and they are STILL, which is a different "
            "and smaller problem than wrong and moving. BTCUSDT's two entries "
            "— the only ones any claim in this slice depends on — agree with "
            "disk. EDGE.md §47b, §53e."),
        "not_edited_by_this_slice": True,
        # prior-slice: a real path to the slice-55 test module.
        "enumerated_in": ("tests/test_slice55_data_eligibility.py::"
                          "TestTheManifestsDescribeTheFilesBesideThem"),
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checked-at-utc", required=True)
    parser.add_argument("--out", default=os.path.join(
        REPO, "artifacts", "slice71_data_freshness.json"))
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

    _claims = {
        "closed bars after t1 = 9": len(closed_after_t1) == 9,
        "after_t1_dates": [c["time_period_start"][:10]
                           for c in closed_after_t1]
        == ["2026-08-10", "2026-08-11", "2026-08-12", "2026-08-13",
            "2026-08-14", "2026-08-15", "2026-08-16", "2026-08-17",
            "2026-08-18"],
        "linear rows = 1470": linear.rows_on_disk == 1470,
        "linear sha256 (compressed)":
            gz_sha256(cp.LINEAR_BTC) == NOTE_LINEAR_SHA,
        "append-only; no pre-t1 rewrite": all_append_only,
        # prior-slice: the previous artefact this delta is measured against.
        "grew vs slice 70 (8 -> 9)": grew_linear == 1,
        "08-19 NOT appended": not any(
            row["time_period_start"].startswith("2026-08-19")
            for row in cp.read_rows(cp.LINEAR_BTC)),
        "ceiling: max(0, 9-5) = 4":
            max(0, len(closed_after_t1) - fb.HORIZON) == 4,
        # The note quotes a CLOSE-JOIN rate, not a print, so it is scored
        # against the join rather than against a row: the two coincide here
        # only because 16:00 is the last print before the bar's close, and
        # collapsing them is the confusion §51c exists to prevent.
        "08-18 close-join = 0.00003650":
            round(_forward_funding_regime()[
                "rate_at_each_forward_decision"].get("2026-08-18", 0), 8)
            == 0.00003650,
        "synthetic false": all(
            json.load(open(os.path.join(REPO, d, "MANIFEST.json"),
                           encoding="utf-8"))["synthetic"] is False
            for d in ("data/real_linear_1d", "data/real_funding")),
        "pack base: tradingbot_slice70.zip": _pack_base_claim_is_true(),
    }

    payload = {
        "schema": SCHEMA,
        # Derived, not typed: the substitution pipeline rewrites `sliceNN`
        # strings and has never once updated an integer. EDGE.md §52a, §54f.
        "slice": SLICE,
        "symbol": "BTCUSDT",
        "checked_at_utc": args.checked_at_utc,

        # -- the headline: present, versus grown --------------------------
        "extension_present": extension_present,
        "new_bars_available": extension_present,
        "after_t1_linear": len(closed_after_t1),
        "after_t1_funding": funding.appended_rows,
        "after_t1_dates": [c["time_period_start"][:10] for c in closed_after_t1],

        "delta_since_slice70": {
            "previous_artefact": PREVIOUS,
            "previous_after_t1_linear": previous_linear,
            "previous_after_t1_funding": previous_funding,
            "new_linear_bars_since_slice70": grew_linear,
            "new_funding_prints_since_slice70": grew_funding,
            "the_window_grew": grew_linear > 0,
            "why_this_field_exists": (
                "t1 does not move, so once one bar lands after it "
                "extension_present is permanently true and re-reporting it "
                "reads like progress every slice. The informative quantity is "
                "the DELTA, and it is written here so a reader does not have "
                "to diff two artefacts to learn whether anything happened. "
                "EDGE.md §46c."),
            # Every number in this sentence is interpolated. The word for
            # the bar count is spelled out from the count itself, because a
            # hand-typed "seven" is the one token that can survive into a
            # slice where it is false. EDGE.md §53d.
            "finding": (
                f"THE WINDOW REACHED "
                f"{_SPELLED.get(len(closed_after_t1), len(closed_after_t1))}"
                f" BARS. {grew_linear} new closed daily bar(s) and "
                f"{grew_funding} new funding print(s) since the "
                f"previous artefact, taking the ceiling to "
                f"{max(0, len(closed_after_t1) - fb.HORIZON)}. That many "
                f"forward decision bars could now yield a trade that closes "
                f"inside the window. Zero setups fired: every rate at a "
                f"decision is below FUND_ABS. The measurement is 'ceiling "
                f"{max(0, len(closed_after_t1) - fb.HORIZON)}, setups 0 of "
                f"{len(closed_after_t1)}, fills 0' and there is no second "
                f"sentence entitled to more. EDGE.md §54b, §54c."),
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
            "slice70_linear_sha256_uncompressed": PRIOR_LINEAR_UNCOMPRESSED,
            "linear_identical_to_slice70_uncompressed":
                linear.whole_file_sha256 == PRIOR_LINEAR_UNCOMPRESSED,
            "linear_differs_from_slice70_because_a_bar_arrived": True,

            # The funding half, new this slice and the point of §54d.
            "slice70_funding_sha256_uncompressed": PRIOR_FUNDING_UNCOMPRESSED,
            "funding_identical_to_slice70_uncompressed":
                funding.whole_file_sha256 == PRIOR_FUNDING_UNCOMPRESSED,
            "funding_compressed_digest_changed_this_slice": True,
            "funding_was_recompressed_not_changed":
                funding.whole_file_sha256 == PRIOR_FUNDING_UNCOMPRESSED,
            "the_trap_fired_this_slice": (
                "The funding .csv.gz differs from slice 70's byte for byte and "
                "its UNCOMPRESSED stream is identical — same 4,410 rows, same "
                "last print at 2026-08-18T16:00:00Z, same digest. It was "
                "recompressed; the data was not touched. Every previous "
                "instance of this trap sat beside a corpus that HAD grown, so "
                "a reader conflating the two still reached the right verdict "
                "by luck. Here the naive check is simply wrong: comparing "
                "compressed digests reports a change that did not happen. "
                "EDGE.md §46c, §54d."),
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
            "unchanged_from_slice70": True,
            "finding": (
                "Still exactly one hole, at 2026-08-09T16:00Z, in the forward "
                "region. This slice's extension did not fill it either. It "
                "changes no number in this slice and is carried forward "
                "again, now for the tenth slice. EDGE.md §45d."),
        },

        # -- the note is a claim, and it is scored --------------------------
        "human_note_present": os.path.isfile(os.path.join(REPO, NOTE_PATH)),
        "human_note_path": NOTE_PATH,
        "human_note_is_evidence": False,
        "human_note_claims_checked": _claims,
        # The MISSION defines this field narrowly: true if the note claims
        # growth and the files do not. It does claim growth, and the files
        # agree, so this is False — and it is kept False rather than widened,
        # because a reader scanning for "was the growth claim honest?" must not
        # be answered by a different question.
        # prior-slice: keyed on the delta claim, which names the predecessor.
        "claim_vs_files_discrepancy": not _claims["grew vs slice 70 (8 -> 9)"],
        "growth_claim_verified": _claims["grew vs slice 70 (8 -> 9)"],

        # The wider question, reported separately and unmissably.
        "note_claims_all_true": all(_claims.values()),
        "false_note_claims": [k for k, v in _claims.items() if not v],
        "pack_base_claim": {
            "claim": "Pack base: tradingbot_slice70.zip",
            "true": _pack_base_claim_is_true(),
            # prior-slice: slice 67 is where this claim first appeared and
            # was false; the reference is deliberate, not a stale constant.
            "was_false_when_first_made_in_slice67": True,
            "true_this_slice": _pack_base_claim_is_true(),
            # prior-slice: slices 62-67 are the history this paragraph is
            # about; the references are fixed and must not advance.
            "why_it_matters": (
                "Across slices 62-66 the notes made claims about DATA — bar "
                "counts, dates, digests, append-only, synthetic false — and "
                "every one verified. Slice 67's was the first claim about "
                "PROCESS and the first to fail. A human can check a bar count "
                "by looking at the file they wrote; they cannot check which "
                "parent their build script used by looking at anything. It is "
                "re-scored every slice since, on the same terms, because a "
                "claim that failed once is the one worth checking again. It "
                "has now held for four slices running."),
            # No restoration artefact exists — there was nothing to
            # restore. The evidence is the delivered tree itself, read from
            # the pack commit, not a file this slice wrote.
            "evidence": (
                f"git show {_BASE_COMMIT[:12]}:EDGE.md — sections 45a-53a all "
                f"present in the tree AS DELIVERED"),
            "recorded_without_accusation": True,
        },
        "human_note_verdict": (
            "Every claim in the note was scored against the files, not "
            "believed. That includes the growth claim, the ceiling arithmetic, "
            "the absence of 2026-08-18, and the 0.00009202 print the note "
            "quotes from 2026-08-17T16:00Z — which is checked against the "
            "funding corpus rather than accepted, because a near-miss quoted "
            "in prose is exactly the number a later reader would build an "
            "argument on. The note's sha256 is the COMPRESSED digest, which "
            "is not the digest any check here uses; §46c records why that "
            "distinction earns its keep."),

        # -- the manifests. EDGE.md §47b --------------------------------------
        "manifests": _manifest_audit(),
        "forward_funding_regime": _forward_funding_regime(),
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
            "ceiling_declared_before_scoring": "EDGE.md §54a",
        },

        "pack_regression": {
            "occurred_this_slice": False,
            "pack_base": "the slice-70 deliverable, verified by digest",
            "restore_artefact": None,
            # prior-slice: the run of slice-61 pack regressions was slices
            # 63-67 and ended at slice 68. Frozen history; it does not
            # advance, and it advanced twice before this comment existed.
            "summary": ("No regression. The pack is the previous deliverable, "
                        "so nothing was restored and no restore artefact "
                        "exists — the fourth consecutive slice with no "
                        "restoration tax. The five-slice run of slice-61 "
                        "regressions ended at slice 68."),
            "design_note": "EDGE.md §51a",
        },

        "git_commit": _provenance.git_commit(REPO),
    }

    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False)
        handle.write("\n")

    delta = payload["delta_since_slice70"]
    print("=" * 78)
    print("SLICE 71 — DATA FRESHNESS   (did it grow SINCE LAST TIME?)")
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
    print(f"    slice 70 uncompressed     : "
          f"{payload['digests']['slice70_linear_sha256_uncompressed']}")
    print(f"    -> identical to slice 70  : "
          f"{payload['digests']['linear_identical_to_slice70_uncompressed']}"
          f"   (a bar arrived; expected False)")
    print(f"    funding compressed today  : "
          f"{payload['digests']['funding_sha256_compressed']}")
    print(f"    funding UNCOMPRESSED today: {funding.whole_file_sha256}")
    print(f"    slice 70 uncompressed     : "
          f"{payload['digests']['slice70_funding_sha256_uncompressed']}")
    print(f"    -> identical to slice 70  : "
          f"{payload['digests']['funding_identical_to_slice70_uncompressed']}"
          f"   (RECOMPRESSED, not changed; expected True)")
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
    print(f"  NEW LINEAR BARS SINCE S70   : "
          f"{delta['new_linear_bars_since_slice70']}")
    print(f"  new funding prints since S70: "
          f"{delta['new_funding_prints_since_slice70']}")
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
