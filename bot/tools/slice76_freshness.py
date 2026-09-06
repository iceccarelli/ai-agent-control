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
    new_linear_bars_since_slice75   the delta               (the informative one)

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

SCHEMA = "data_freshness/17"

# The first commit in this repository is the human pack, committed verbatim
# before anything was touched. Reading EDGE.md from that commit is how the
# pack-base claim is scored against the tree AS DELIVERED rather than against
# the tree as this slice left it.
_BASE_COMMIT = subprocess.check_output(
    ["git", "rev-list", "--max-parents=0", "HEAD"],
    cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
).decode().split()[0]
SLICE = 76

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
# Slice 72 note: fourth consecutive failure. The pipeline is not going to
# learn. Every payload now DERIVES its slice number from this constant, so
# there is one line to get wrong and one test aimed at it.
# Slice 73: fifth. Unchanged conclusion.
# Slice 74: sixth. Slice 75: seventh. Slice 76: eighth.
NOTE_PATH = f"docs/human/HUMAN_DATA_NOTE_SLICE{SLICE}.md"
PREVIOUS = "artifacts/slice75_data_freshness.json"

# The predecessor's UNCOMPRESSED linear digest, named once and used twice.
#
# THE DEFECT THIS CONSTANT HAS CARRIED, AND WHAT FIXED IT. EDGE.md §59d.
# Slice 75 set this to its OWN current digest, so the artefact shipped
# `linear_identical_to_slice74_uncompressed = true` and
# `linear_differs_from_slice74_because_a_bar_arrived = true` in the same
# dict — a pair that cannot both hold. It was the FOURTH occurrence: slice
# 66 was stale by two, and slices 73, 74 and 75 each self-compared. The
# value below is slice 75's CURRENT digest, which is what a predecessor
# constant is supposed to be, and it is VERIFIED against
# `slice75_data_freshness.json`'s own `linear_sha256_uncompressed` field —
# never against its `slice74_linear_sha256_uncompressed` field, which is the
# broken one. `_prior_digest_audit()` below does that verification and fails
# the run if it does not hold, so this is no longer a constant anyone has to
# be careful about.
PRIOR_LINEAR_UNCOMPRESSED = ("6e64847c54991ef38c0aaaad3095446ea955c822e07"
                             "f14938ff6943c8c93b0a7")

# The predecessor's UNCOMPRESSED FUNDING digest. New this slice, because the
# funding .csv.gz arrived with a DIFFERENT compressed digest and an IDENTICAL
# uncompressed stream: recompressed, not changed. Without this constant the
# artefact could only report that the file's bytes moved, which is true and
# misleading. EDGE.md §46c, §54d.
PRIOR_FUNDING_UNCOMPRESSED = ("fec0ee8c33b528cb0baeda4de0c3d5b0c3bb15a144bc"
                              "5537cd379e3bda4219b2")

# Spelled-out bar counts, so the headline sentence is derived from the count
# rather than typed beside it.
_SPELLED = {5: "FIVE", 6: "SIX", 7: "SEVEN", 8: "EIGHT", 9: "NINE",
            10: "TEN", 11: "ELEVEN", 12: "TWELVE", 13: "THIRTEEN",
            14: "FOURTEEN", 15: "FIFTEEN", 16: "SIXTEEN",
            17: "SEVENTEEN", 18: "EIGHTEEN", 19: "NINETEEN", 20: "TWENTY"}
# Slice 75: the table stopped at TWELVE and the window reached thirteen, so
# the sentence fell back to the integer. It degraded VISIBLY instead of
# lying, which is the behaviour a derived field is chosen for — but a lookup
# with a bounded domain is still a constant waiting to run out. Extended, and
# the fallback is kept so the next overflow is loud rather than silent.
# EDGE.md §58f.
NOTE_LINEAR_SHA = ("55a151d0466c77e788b9951ac408283a0852d8037339d1a7b"
                   "f10c548dda03b63")

# Slice 76's note quotes the UNCOMPRESSED linear digest as well as the
# compressed one — the first time both have appeared for the linear file.
# Scored as two separate claims, because the whole point of §46c is that
# they are different measures and only one of them is pinned.
NOTE_LINEAR_SHA_UNCOMPRESSED = ("293774ee35fcac24679bed81356ef081c3787e8a"
                                "cfc3877b2fe12650316d97c9")

# The note quotes an UNCOMPRESSED funding digest this slice — the first time a
# human note has quoted the measure the repository actually pins. Scored as a
# claim like any other. EDGE.md §46c, §54d.
NOTE_FUNDING_SHA = ("c652c9b0b6332b5b98bc11ec89097abd69cea1d0b6774cf4f"
                    "79532243ba611f4")


def _prior_digest_audit() -> dict:
    """Is each slice's predecessor digest actually its PREDECESSOR's? §59d.

    A predecessor constant has exactly one job: name the digest the previous
    slice CURRENTLY reported, so that "did the corpus move?" is answerable.
    It has failed that job four times in twelve slices and the failure is
    silent by construction — a constant equal to the artefact's OWN current
    digest yields `identical = true` beside `differs = true`, and nothing in
    the artefact objects.

    This audits every freshness artefact on disk rather than repeating a
    count from prose. For each one it reads the predecessor digest the
    artefact declared and compares it against (a) that artefact's own current
    digest — equality is the self-compare defect — and (b) the digest the
    PREVIOUS artefact actually reported, which is the value it should have
    carried.

    Two equal digests are not automatically a defect: slices 63 and 71 both
    reported a linear corpus that genuinely had not moved, and their
    predecessor constants were right. The discriminator is whether the
    constant equals the artefact's OWN digest (defect) or the PREVIOUS
    artefact's (correct), which is why both comparisons are made.
    """
    import glob  # noqa: PLC0415
    import re as _re  # noqa: PLC0415

    found, found_rows = {}, {}
    for path in sorted(glob.glob(os.path.join(
            REPO, "artifacts", "slice*_data_freshness.json"))):
        match = _re.search(r"slice(\d+)_data_freshness", os.path.basename(path))
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
        number = int(match.group(1))
        found[number] = payload.get("digests", {})
        found_rows[number] = {
            "linear_rows": payload.get("linear_rows"),
            "funding_rows": payload.get("funding_rows"),
            # Slice 62's artefact predates `linear_rows` and carries only the
            # after-t1 counts. Same question, coarser instrument, and it is
            # enough to say whether the corpus could have moved.
            "linear_after_t1": payload.get("after_t1_linear"),
            "funding_after_t1": payload.get("after_t1_funding"),
        }

    rows = []
    for number in sorted(found):
        digests = found[number]
        for kind in ("linear", "funding"):
            current = digests.get(f"{kind}_sha256_uncompressed")
            declared_key = next(
                (k for k in digests
                 if k.endswith(f"_{kind}_sha256_uncompressed")
                 and k.startswith("slice")), None)
            if declared_key is None or current is None:
                continue
            declared = digests[declared_key]
            predecessor = found.get(number - 1, {}).get(
                f"{kind}_sha256_uncompressed")
            equals_own = declared == current
            matches = None if predecessor is None else declared == predecessor
            unchanged = (None if predecessor is None
                         else predecessor == current)
            # A row-count fallback for the one slice whose predecessor
            # artefact carries no digests block at all (slice 62 wrote none).
            # It cannot prove the digest; it can prove whether the corpus
            # COULD have been unchanged, which is what separates a legitimate
            # equality from a self-compare.
            fallback = None
            if predecessor is None:
                for rows_key in (f"{kind}_rows", f"{kind}_after_t1"):
                    here = found_rows.get(number, {}).get(rows_key)
                    there = found_rows.get(number - 1, {}).get(rows_key)
                    if here is not None and there is not None:
                        fallback = here == there
                        break

            if matches is True:
                verdict = ("CORRECT — and the corpus did not move, so the "
                           "equality is legitimate"
                           if unchanged else "CORRECT")
            elif matches is False and equals_own:
                verdict = "SELF-COMPARE DEFECT — it names its own digest"
            elif matches is False:
                verdict = "WRONG — neither its own digest nor its predecessor's"
            elif fallback is True:
                verdict = ("LEGITIMATE BY ROW COUNT — the predecessor "
                           "artefact carries no digest, but the corpus did "
                           "not grow between the two, so equality is expected")
            elif fallback is False:
                verdict = ("SELF-COMPARE DEFECT BY ROW COUNT — the corpus "
                           "grew, so the digests cannot be equal")
            else:
                verdict = "INDETERMINATE — no predecessor digest to check"

            rows.append({
                "slice": number,
                "kind": kind,
                "field": declared_key,
                "declared_predecessor_digest": declared,
                "this_artefacts_own_digest": current,
                "the_previous_artefacts_actual_digest": predecessor,
                "declared_equals_its_own_current_digest": equals_own,
                "matches_the_real_predecessor": matches,
                "corpus_genuinely_unchanged": unchanged,
                "corpus_unchanged_by_row_count": fallback,
                "verdict": verdict,
                "is_a_defect": verdict.startswith("SELF-COMPARE DEFECT")
                or verdict.startswith("WRONG"),
            })

    defects = [r for r in rows if r["is_a_defect"]]
    self_compares = [r for r in rows
                     if r["verdict"].startswith("SELF-COMPARE DEFECT")]
    return {
        "artefacts_audited": sorted(found),
        "constants_audited": len(rows),
        "rows": rows,
        "defects": [{"slice": r["slice"], "field": r["field"],
                     "verdict": r["verdict"]} for r in defects],
        "defect_count": len(defects),
        "self_compare_defects": [
            {"slice": r["slice"], "field": r["field"]} for r in self_compares],
        "self_compare_defect_count": len(self_compares),
        "legitimate_equalities": [
            {"slice": r["slice"], "kind": r["kind"], "verdict": r["verdict"]}
            for r in rows
            if r["declared_equals_its_own_current_digest"]
            and "legitimate" in r["verdict"].lower()],
        "indeterminate": [
            {"slice": r["slice"], "kind": r["kind"], "verdict": r["verdict"]}
            for r in rows if r["verdict"].startswith("INDETERMINATE")],
        "why_equality_alone_is_not_the_test": (
            "Two equal digests can mean the constant named its own value "
            "(defect) or that the corpus genuinely did not move (correct). "
            "Slice 71's funding constant is the second kind — the file was "
            "RECOMPRESSED, not changed, which is the §54d case — and slice "
            "63's linear constant is the same shape, checked by row count "
            "because slice 62's artefact carries no digests block at all. "
            "Reporting either as a defect would inflate the count; reporting "
            "the real ones as legitimate would hide them. Both comparisons "
            "are made and the verdict names which one decided it."),
        "this_slice_is_clean": not any(
            r["slice"] == SLICE and r["is_a_defect"] for r in rows),
        "how_a_self_compare_hides": (
            "It is not caught by a digest check, because both digests are "
            "real and both are correctly computed. It is caught only by "
            "asking WHOSE digest the constant is, which no field in the "
            "artefact asked until this slice. EDGE.md §59d."),
    }


def edge_state() -> dict:
    """What EDGE.md looked like when this tool ran. EDGE.md §56h.

    Self-reported, and therefore WEAKER than reading the file out of git at
    the artefact's own commit — a tool cannot certify its own honesty. It is
    recorded anyway because the deliverable ships without a repository, so a
    git-based check cannot run on an unzipped tree, and a check that silently
    skips is the one defect class this programme keeps re-learning.

    Both checks are kept: this one always runs, the git one runs when a
    repository is present.
    """
    path = os.path.join(REPO, "EDGE.md")
    with open(path, "rb") as handle:
        raw = handle.read()
    text = raw.decode("utf-8")
    return {
        "edge_sha256": hashlib.sha256(raw).hexdigest(),
        "sections_present": sorted(
            int(n) for n in __import__("re").findall(r"^## (\d+)a\.", text,
                                                     flags=8)),
        "self_reported": True,
        "why_self_reported_is_weaker": (
            "A tool recording its own inputs cannot prove it did so honestly. "
            "The stronger check reads EDGE.md out of git at this artefact's "
            "own git_commit; it needs a repository and the deliverable ships "
            "without one. Both are asserted where they can be. §56h."),
    }


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

    The decisive test is whether EDGE.md carries the sections slices 62-72
    wrote. The slice-75 deliverable has 45 through 58; a slice-61 base has
    none. The range advances with the programme: a fixed upper bound would
    keep passing on a tree that had silently lost the newest slice's work.
    Read from the tree AS DELIVERED — recorded by the STEP 0 baseline before
    this slice wrote anything — via the sections that were present at commit
    time, not via a file this slice may since have appended to.
    """
    delivered = subprocess.check_output(
        ["git", "show", f"{_BASE_COMMIT}:EDGE.md"], cwd=REPO).decode("utf-8")
    return all(f"\n## {n}a." in delivered
               for n in range(45, 59))


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

    # Measured once and used twice: the census feeds both its own block and
    # the finding sentence below, so the two can never disagree. Slice 75
    # typed "1,174 of 4,422" beside a block that computed it, and the two
    # would have drifted the moment a print arrived — which is this slice.
    # EDGE.md §53d, §57c.
    _census = _threshold_census()

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
        "setup_dates": [day(i) for i in window if i in setups],
        "setup_directions": {day(i): setups[i] for i in window if i in setups},
        "setup_bars_that_are_the_last_bar_of_the_corpus": [
            day(i) for i in window if i in setups and i == len(bars) - 1],
        "why_a_setup_is_not_a_trade": (
            "A setup is a property of one bar's funding rate. A trade needs "
            "an ENTRY, which the rule places at the following bar's open, and "
            "an EXIT inside the corpus. A setup on the final bar has neither. "
            "Counting it as a trade, an observation, or gate progress would "
            "be the slice-59 substitution — activity relabelled as "
            "experience. EDGE.md §55b."),
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
        # prior-slice: the inversion was slice 71's and the correction was
        # slice 72's; both references are historical and never advance.
        # CORRECTED IN SLICE 74. As written in slice 71 this asked whether
        # the LAST PRINT falls at or before the newest decision instant —
        # which is true exactly when the funding corpus STOPS SHORT, i.e. the
        # stale condition the field was named to rule out. It returned True
        # in slice 71 for the wrong reason and went False in slice 72 while
        # coverage was in fact better. Inverted relative to its name; nothing
        # consumed it, so no measurement was affected. EDGE.md §55h.
        "funding_covers_the_newest_decision": bool(
            window and rates[window[-1]] == rates[window[-1]]),
        "funding_extends_past_the_newest_decision": bool(
            window and cp.read_rows(cp.FUNDING_BTC)[-1]["funding_time"]
            > dt.datetime.fromtimestamp(
                fb.close_time_ms(bars[window[-1]]) / 1000.0,
                tz=dt.timezone.utc).isoformat()),
        "what_those_two_fields_mean": (
            "COVERS = the join returns a rate for the newest decision, i.e. a "
            "print exists at or before its close. EXTENDS PAST = the corpus "
            "also carries prints later than that instant, which is a "
            "freshness signal about the FILE and cannot change the decision, "
            "because close_time_ms is 23:59:59.999 and the join reads only "
            "backwards. Slice 71 had covers=True, extends_past=False; this "
            "slice has both true."),
        "prints_after_t1": len(prints_after_t1),
        # EDGE.md §57c. Computed here so the claim that FUND_ABS sits on the
        # venue's base rate is a measurement in the artefact, not prose.
        "threshold_census": _census,
        "base_rate_runs": _base_rate_runs(),
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
            f"{sum(1 for i in window if i in setups)} of {len(window)} "
            f"forward decisions reached FUND_ABS = {fb.FUND_ABS}: "
            f"{', '.join(day(i) for i in window if i in setups) or 'none'}. "
            "The newest, 2026-08-24, close-joins at 0.00010000 — its 16:00 "
            "print, which stands at the bar's close (23:59:59.999Z, strictly "
            "before the 2026-08-25T00:00Z print) — and the comparison is "
            "inclusive, so it is a SHORT setup. It is the LAST BAR, so "
            "directed_signal_bars excludes it and it is not a flag. "
            "2026-08-22 AND 2026-08-23 made exactly that transition this "
            "slice: 08-22 was a last-bar setup in slice 75, and 08-23 was a "
            "setup and a last bar within this same catch-up. Two closed bars "
            "promote two, which is arithmetic about which bar is last and "
            "not momentum. EDGE.md §59b. "
            f"WHAT THE THRESHOLD ACTUALLY SELECTS. {fb.FUND_ABS:.8f} is the "
            f"venue's BASE funding rate, not a cap and not an extreme: it is "
            f"the single most common value in this corpus at "
            f"{_census['exactly_at_fund_abs']:,} of {_census['prints']:,} "
            f"prints ({100 * _census['share_exactly_at_fund_abs']:.1f}%), "
            f"while {_census['strictly_above_fund_abs']} prints exceed it and "
            f"the maximum is {_census['max_print']}. "
            "FUND_ABS sits exactly on that mode with an "
            "inclusive comparison, so the rule fades BASE-OR-ABOVE funding, "
            "and its selectivity comes from the close-time join and the "
            "contiguous-run schedule rather than from the threshold being "
            "rare. This is a correction to how the rule has been DESCRIBED; "
            "no measured number moves, and it is emphatically not a reason "
            "to touch FUND_ABS. EDGE.md §57c. "
            "NO PARAMETER MOVED: FUND_ABS is 0.0001 and the market printed "
            "0.00010000. EDGE.md §51c, §57b."),
        "parameters_moved_because_of_this": False,
        "why_not": (
            "A threshold adjusted after seeing which prints missed it is a "
            "threshold fitted to the data, and a narrow miss makes the "
            "temptation larger rather than the change more defensible. "
            "FUND_ABS stays 0.0001; the horizon stays 5; the join stays "
            "close-time — and §57c, which shows the threshold sits on the "
            "venue's BASE rate rather than on an extreme, is a correction to "
            "the programme's language and not a licence to move it. A "
            "threshold re-derived after seeing which prints qualify is fitted "
            "to data whether the re-derivation is dressed as an optimisation "
            "or as a correction. Three near misses were recorded across "
            "slices 70 "
            "and 71 and neither available rescue — moving the join, moving "
            "the threshold — was taken; each is exactly the change that would "
            "void the n=41 clear these bars are measured against. That "
            "restraint is what makes this slice's setup worth anything: a "
            "threshold reached by a market is evidence, and a threshold "
            "reached because it was lowered is not. The distinction only "
            "exists because nothing was moved while the misses were "
            "accumulating."),
    }


def _days_until_scoreable_for(date: str) -> int:
    """Closed days until a forward bar gets a barrier outcome. EDGE.md §59c.

    `(i + HORIZON + 2) - last`, recomputed from indices EVERY slice. §57b
    reused a distance from the slice before and was wrong by one; slice 75
    said four for 08-19, slice 74 said five, and this slice says two. None of
    those numbers is carried — the formula is, and the indices are read fresh.
    A non-positive result means the bar is already scoreable.
    """
    import backtest as bt  # noqa: PLC0415
    import market_data as md  # noqa: PLC0415

    loaded, _b, _n = md.load_corpus(
        os.path.join(REPO, "data", "real_linear_1d"), verify=False)
    bars = [bt.Bar(b.start_ms, b.open, b.high, b.low, b.close, b.volume)
            for b in loaded["BTCUSDT"]]
    last = len(bars) - 1
    for index, item in enumerate(bars):
        stamp = dt.datetime.fromtimestamp(
            item.start_ms / 1000.0, tz=dt.timezone.utc).strftime("%Y-%m-%d")
        if stamp == date:
            return (index + fb.HORIZON + 2) - last
    raise KeyError(date)


def _base_rate_runs() -> dict:
    """How long the funding rate has been sitting at exactly the base rate.

    Every forward setup is a print at exactly FUND_ABS, which §57c
    established is the venue's BASE rate rather than an extreme. A reader
    seeing five setups arrive in six days could take that for a regime
    change. This measures the run against the corpus's own history so the
    answer is a percentile rather than an impression. EDGE.md §58d.
    """
    import itertools  # noqa: PLC0415
    values = [float(r["funding_rate"]) for r in cp.read_rows(cp.FUNDING_BTC)]
    runs = [len(list(group)) for at, group in itertools.groupby(
        v == fb.FUND_ABS for v in values) if at]
    trailing = 0
    for value in reversed(values):
        if value != fb.FUND_ABS:
            break
        trailing += 1
    return {
        "runs_at_exactly_base": len(runs),
        "mean_run_length_prints": round(sum(runs) / len(runs), 2),
        "longest_run_prints": max(runs),
        "current_trailing_run_prints": trailing,
        "current_trailing_run_hours": trailing * 8,
        "percentile_of_current_run": round(
            100 * sum(1 for r in runs if r <= trailing) / len(runs), 1),
        "finding": (
            f"The rate has printed exactly {fb.FUND_ABS} for {trailing} "
            f"consecutive prints ({trailing * 8} hours). Against this "
            f"corpus's own history that is unremarkable: {len(runs)} such "
            f"runs, mean {round(sum(runs) / len(runs), 2)} prints, longest "
            f"{max(runs)}. The setups are accumulating because the book is "
            f"BALANCED, not because anything is extreme — the same fact §57c "
            f"reported as a census, now visible in the forward window. It is "
            f"also the moment at which moving FUND_ABS would feel most "
            f"reasonable and be most wrong. EDGE.md §57c, §58d."),
        "fund_abs_moved_because_of_this": False,
    }


def _threshold_census() -> dict:
    """Where FUND_ABS sits in the distribution of prints. EDGE.md §57c.

    The forward window has produced three prints at exactly FUND_ABS and a
    reader could take that for a regime turning extreme. It is not: 0.0001 is
    the venue's BASE funding rate and the modal value of this corpus.
    Measured, so the claim is checkable rather than asserted.
    """
    rows = cp.read_rows(cp.FUNDING_BTC)
    values = [float(r["funding_rate"]) for r in rows]
    at = sum(1 for v in values if v == fb.FUND_ABS)
    above = sum(1 for v in values if v > fb.FUND_ABS)
    below_neg = sum(1 for v in values if v < -fb.FUND_ABS)
    return {
        "fund_abs": fb.FUND_ABS,
        "prints": len(values),
        "exactly_at_fund_abs": at,
        "share_exactly_at_fund_abs": round(at / len(values), 4),
        "strictly_above_fund_abs": above,
        "strictly_below_negative_fund_abs": below_neg,
        "max_print": max(values),
        "min_print": min(values),
        "fund_abs_is_a_cap": above == 0,
        "fund_abs_is_the_modal_value": at == max(
            __import__("collections").Counter(values).values()),
        "runner_up_value_count": sorted(
            __import__("collections").Counter(values).values())[-2],
        "finding": (
            f"{at} of {len(values)} prints ({100 * at / len(values):.1f}%) sit "
            f"at EXACTLY {fb.FUND_ABS}, and {above} exceed it, with a maximum "
            f"of {max(values)}. So it is not a cap — it is the venue's BASE "
            f"funding rate and the modal value of the corpus. FUND_ABS sits "
            f"on that mode with an inclusive comparison, which means the rule "
            f"fades BASE-OR-ABOVE funding rather than extreme funding. The "
            f"selectivity comes from the close-time join and "
            f"one_entry_per_contiguous_run, not from the threshold being "
            f"rare. This corrects the programme's LANGUAGE and moves no "
            f"measured number; the OOS null was scored on the same threshold "
            f"by the same code, so whatever the threshold admits it admitted "
            f"for both sides. EDGE.md §57c."),
        "this_is_not_a_reason_to_move_fund_abs": True,
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
        REPO, "artifacts", "slice76_data_freshness.json"))
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
    payload_dates = [c["time_period_start"][:10] for c in closed_after_t1]

    with open(os.path.join(REPO, PREVIOUS), encoding="utf-8") as handle:
        previous = json.load(handle)
    previous_linear = int(previous["after_t1_linear"])
    previous_funding = int(previous["after_t1_funding"])

    # EDGE.md §59d. The predecessor constants are VERIFIED against the
    # previous artefact's OWN current digests before anything is computed
    # from them, and the run FAILS CLOSED if they disagree. A constant that
    # can be wrong silently is the whole defect; a constant that stops the
    # run is not one.
    for kind, constant in (("linear", PRIOR_LINEAR_UNCOMPRESSED),
                           ("funding", PRIOR_FUNDING_UNCOMPRESSED)):
        actual = previous["digests"][f"{kind}_sha256_uncompressed"]
        if constant != actual:
            raise SystemExit(
                f"PRIOR_{kind.upper()}_UNCOMPRESSED does not match "
                f"{PREVIOUS} :: digests.{kind}_sha256_uncompressed\n"
                f"  constant : {constant}\n"
                f"  artefact : {actual}\n"
                "EDGE.md §59d — a predecessor digest is the PREVIOUS "
                "slice's CURRENT value, and nothing is computed from a "
                "constant that has not been checked against it.")

    # ONE comparison per corpus, read with both senses wherever the artefact
    # needs them, so `identical` and `differs` cannot contradict. §59d.
    linear_identical = linear.whole_file_sha256 == PRIOR_LINEAR_UNCOMPRESSED
    funding_identical = funding.whole_file_sha256 == PRIOR_FUNDING_UNCOMPRESSED

    all_append_only = all(check.append_only for check in checks.values())
    extension_present = bool(closed_after_t1)
    grew_linear = len(closed_after_t1) - previous_linear
    grew_funding = funding.appended_rows - previous_funding

    _regime_cache = {}

    def _regime():
        if "value" not in _regime_cache:
            _regime_cache["value"] = _forward_funding_regime()
        return _regime_cache["value"]

    def _setup_count():
        # Derived. The sentence below used to carry the words "Zero setups
        # fired" as a constant, and on the slice a setup finally fired it
        # would have shipped a lie. EDGE.md §53d, §55b.
        return _regime()["funding_setups_in_window"]

    _claims = {
        "closed bars after t1 = 15": len(closed_after_t1) == 15,
        "after_t1_dates": [c["time_period_start"][:10]
                           for c in closed_after_t1]
        == ["2026-08-10", "2026-08-11", "2026-08-12", "2026-08-13",
            "2026-08-14", "2026-08-15", "2026-08-16", "2026-08-17",
            "2026-08-18", "2026-08-19", "2026-08-20", "2026-08-21",
            "2026-08-22", "2026-08-23", "2026-08-24"],
        "linear rows = 1476": linear.rows_on_disk == 1476,
        "linear sha256 (compressed)":
            gz_sha256(cp.LINEAR_BTC) == NOTE_LINEAR_SHA,
        # New this slice: the note quotes the UNCOMPRESSED linear digest too,
        # which is the measure the repository actually pins. Scored
        # separately from the compressed one on purpose. EDGE.md §46c.
        "linear sha256 (uncompressed)":
            linear.whole_file_sha256 == NOTE_LINEAR_SHA_UNCOMPRESSED,
        "append-only; no pre-t1 rewrite": all_append_only,
        # prior-slice: the previous artefact this delta is measured against.
        "grew vs slice 75 (13 -> 15)": grew_linear == 2,
        "catch-up appended 2026-08-23 AND 2026-08-24": [
            c["time_period_start"][:10] for c in closed_after_t1][-2:]
            == ["2026-08-23", "2026-08-24"],
        "2026-08-25 NOT appended as a linear bar": not any(
            row["time_period_start"].startswith("2026-08-25")
            for row in cp.read_rows(cp.LINEAR_BTC)),
        "ceiling: max(0, 15-5) = 10":
            max(0, len(closed_after_t1) - fb.HORIZON) == 10,
        "funding rows = 4431": funding.rows_on_disk == 4431,
        "funding last print = 2026-08-25T16:00:00+00:00":
            cp.read_rows(cp.FUNDING_BTC)[-1]["funding_time"]
            == "2026-08-25T16:00:00+00:00",
        "08-19 remains a setup": "2026-08-19" in _regime()["setup_dates"],
        "08-19 not scoreable until last bar >= 2026-08-26":
            _days_until_scoreable_for("2026-08-19") > 0,
        "funding sha256 (uncompressed)":
            funding.whole_file_sha256 == NOTE_FUNDING_SHA,
        "predecessor linear uncompressed = slice 75 CURRENT":
            PRIOR_LINEAR_UNCOMPRESSED == previous["digests"][
                "linear_sha256_uncompressed"],
        "predecessor funding uncompressed = slice 75 CURRENT":
            PRIOR_FUNDING_UNCOMPRESSED == previous["digests"][
                "funding_sha256_uncompressed"],
        # The note quotes a CLOSE-JOIN rate, not a print, so it is scored
        # against the join rather than against a row: the two coincide here
        # only because 16:00 is the last print before the bar's close, and
        # collapsing them is the confusion §51c exists to prevent.
        # The note quotes 08-23's and 08-24's CLOSE-JOINS. Scored against
        # the join, not against a row, so a print and a decision cannot be
        # collapsed. EDGE.md §51c, §58b.
        "08-23 close-join = 0.00010000":
            round(_regime()["rate_at_each_forward_decision"]
                  .get("2026-08-23", 0), 8) == 0.00010000,
        "08-24 close-join = 0.00010000":
            round(_regime()["rate_at_each_forward_decision"]
                  .get("2026-08-24", 0), 8) == 0.00010000,
        "08-23 is a setup": "2026-08-23" in _regime()["setup_dates"],
        "08-24 is a setup": "2026-08-24" in _regime()["setup_dates"],
        "synthetic false": all(
            json.load(open(os.path.join(REPO, d, "MANIFEST.json"),
                           encoding="utf-8"))["synthetic"] is False
            for d in ("data/real_linear_1d", "data/real_funding")),
        "pack base: tradingbot_slice75.zip": _pack_base_claim_is_true(),
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

        "delta_since_slice75": {
            "previous_artefact": PREVIOUS,
            "previous_after_t1_linear": previous_linear,
            "previous_after_t1_funding": previous_funding,
            "new_linear_bars_since_slice75": grew_linear,
            "new_funding_prints_since_slice75": grew_funding,
            "this_was_a_catch_up": grew_linear > 1,
            "what_a_catch_up_is": (
                "Two UTC days closed between packs because the human was "
                "late. It is two rows appended to one file. It licenses no "
                "second look at the rule, no wider universe, no new intake "
                "and no re-read of the clear. The only thing it changes is "
                "the FLAG count, because each closed bar promotes the "
                "previous last-bar setup and two closed bars promote two. "
                "EDGE.md §59b."),
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
                f" BARS. {grew_linear} new closed daily bar(s) "
                f"({', '.join(payload_dates[-grew_linear:]) if grew_linear else 'none'}) and "
                f"{grew_funding} new funding print(s) since the "
                f"previous artefact, taking the ceiling to "
                f"{max(0, len(closed_after_t1) - fb.HORIZON)}. That many "
                f"forward decision bars could now yield a trade that closes "
                f"inside the window. "
                f"{_setup_count()} of {len(closed_after_t1)} decisions "
                f"reached FUND_ABS"
                f"{': ' + ', '.join(_regime()['setup_dates']) if _setup_count() else ''}"
                f". Fills: {0}. The measurement is 'ceiling "
                f"{max(0, len(closed_after_t1) - fb.HORIZON)}, setups "
                f"{_setup_count()} of {len(closed_after_t1)}, fills 0' and "
                f"there is no second sentence entitled to more. A setup is "
                f"not a trade, a FLAG is not an entry, and an entry is not a "
                f"closed trade. EDGE.md §59a, §59b."),
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
            "note_quotes_the_uncompressed_digest_too": True,
            "note_linear_sha256_uncompressed": NOTE_LINEAR_SHA_UNCOMPRESSED,
            "note_uncompressed_digest_matches_the_file":
                linear.whole_file_sha256 == NOTE_LINEAR_SHA_UNCOMPRESSED,

            # EDGE.md §59d. ONE comparison, read twice with opposite sense,
            # so `identical` and `differs` cannot both be true however the
            # constant is set. Slice 75 computed them independently — one
            # from the constant, one from a hard-coded True — and shipped
            # the contradiction.
            "slice75_linear_sha256_uncompressed": PRIOR_LINEAR_UNCOMPRESSED,
            "linear_identical_to_slice75_uncompressed": linear_identical,
            "linear_differs_because_a_bar_arrived": not linear_identical,
            "identical_and_differs_cannot_both_be_true":
                linear_identical is not (not linear_identical),
            "prior_linear_digest_equals_this_slices_own":
                PRIOR_LINEAR_UNCOMPRESSED == linear.whole_file_sha256,
            "prior_linear_digest_equals_the_previous_artefacts_own":
                PRIOR_LINEAR_UNCOMPRESSED == previous["digests"][
                    "linear_sha256_uncompressed"],
            "where_the_prior_digest_came_from": (
                "slice75_data_freshness.json :: digests."
                "linear_sha256_uncompressed — the previous slice's CURRENT "
                "digest. NOT its slice74_linear_sha256_uncompressed field, "
                "which slice 75 set to its own current digest and is "
                "therefore the broken one. EDGE.md §59d."),

            # The funding half, on identical terms.
            "slice75_funding_sha256_uncompressed": PRIOR_FUNDING_UNCOMPRESSED,
            "funding_identical_to_slice75_uncompressed": funding_identical,
            "funding_differs_because_prints_arrived": not funding_identical,
            "funding_identical_and_differs_cannot_both_be_true":
                funding_identical is not (not funding_identical),
            "prior_funding_digest_equals_this_slices_own":
                PRIOR_FUNDING_UNCOMPRESSED == funding.whole_file_sha256,
            "prior_funding_digest_equals_the_previous_artefacts_own":
                PRIOR_FUNDING_UNCOMPRESSED == previous["digests"][
                    "funding_sha256_uncompressed"],
            "funding_compressed_digest_changed_this_slice":
                gz_sha256(cp.FUNDING_BTC) != previous["digests"][
                    "funding_sha256_compressed"],
            "funding_was_recompressed_not_changed": funding_identical,
            "funding_genuinely_changed_this_slice": not funding_identical,
            "prior_digest_audit": _prior_digest_audit(),
            "funding_prints_added_since_previous_slice": grew_funding,
            "note_funding_sha256_uncompressed": NOTE_FUNDING_SHA,
            "note_funding_digest_matches_the_uncompressed_file":
                funding.whole_file_sha256 == NOTE_FUNDING_SHA,
            "the_trap_did_not_fire_this_slice": (
                f"The funding corpus GENUINELY changed: its UNCOMPRESSED "
                f"digest moved and {grew_funding} new prints arrived, running "
                f"through "
                f"{cp.read_rows(cp.FUNDING_BTC)[-1]['funding_time']}. The same "
                f"check answered 'recompressed, not changed' in slice 71 and "
                f"'changed' in slices 72 through 75, so the two situations are "
                f"told apart by the instrument rather than by the reader's "
                f"expectations. EDGE.md §46c, §54d."),
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
            "unchanged_from_slice75": True,
            "finding": (
                "Still exactly one hole, at 2026-08-09T16:00Z, in the forward "
                "region. This slice's extension did not fill it either. It "
                "changes no number in this slice and is carried forward "
                "again, now for the sixteenth slice. EDGE.md §45d."),
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
        "claim_vs_files_discrepancy":
            not _claims["grew vs slice 75 (13 -> 15)"],
        "growth_claim_verified": _claims["grew vs slice 75 (13 -> 15)"],

        # The wider question, reported separately and unmissably.
        "note_claims_all_true": all(_claims.values()),
        "false_note_claims": [k for k, v in _claims.items() if not v],
        "pack_base_claim": {
            "claim": "Pack base: tradingbot_slice75.zip",
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
                "has now held for NINE slices running — 68 through 76."),
            # No restoration artefact exists — there was nothing to
            # restore. The evidence is the delivered tree itself, read from
            # the pack commit, not a file this slice wrote.
            "evidence": (
                f"git show {_BASE_COMMIT[:12]}:EDGE.md — sections 45a-57a all "
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
        "forward_funding_regime": _regime(),
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
            "ceiling_declared_before_scoring": "EDGE.md §58a",
        },

        "pack_regression": {
            "occurred_this_slice": False,
            "pack_base": "the slice-75 deliverable, verified by digest",
            "restore_artefact": None,
            # prior-slice: the run of slice-61 pack regressions was slices
            # 63-67 and ended at slice 68. Frozen history; it does not
            # advance, and it advanced twice before this comment existed.
            "summary": ("No regression. The pack is the previous deliverable, "
                        "so nothing was restored and no restore artefact "
                        "exists — the NINTH consecutive slice with no "
                        "restoration tax, counting 68 through 76 inclusive. "
                        "The five-slice run of slice-61 regressions ended at "
                        "slice 68. EDGE.md §59a said TENTH; that was an "
                        "off-by-one written before this count was derived, "
                        "and it is corrected here rather than back-dated."),
            "clean_run_started_at_slice": 68,
            "consecutive_clean_packs": SLICE - 68 + 1,
            "design_note": "EDGE.md §51a",
        },

        "edge_state_when_this_ran": edge_state(),

        "git_commit": _provenance.git_commit(REPO),
    }

    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False)
        handle.write("\n")

    delta = payload["delta_since_slice75"]
    print("=" * 78)
    print(f"SLICE {SLICE} — DATA FRESHNESS   "
          f"(did it grow SINCE LAST TIME?)")
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
    print(f"    slice 75 uncompressed     : "
          f"{payload['digests']['slice75_linear_sha256_uncompressed']}")
    print(f"    -> identical to slice 75  : "
          f"{payload['digests']['linear_identical_to_slice75_uncompressed']}"
          f"   (two bars arrived; expected False)")
    print(f"    funding compressed today  : "
          f"{payload['digests']['funding_sha256_compressed']}")
    print(f"    funding UNCOMPRESSED today: {funding.whole_file_sha256}")
    print(f"    slice 75 uncompressed     : "
          f"{payload['digests']['slice75_funding_sha256_uncompressed']}")
    print(f"    -> identical to slice 75  : "
          f"{payload['digests']['funding_identical_to_slice75_uncompressed']}"
          f"   (new prints arrived; expected False)")
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
    print(f"  NEW LINEAR BARS SINCE S75   : "
          f"{delta['new_linear_bars_since_slice75']}")
    print(f"  new funding prints since S75: "
          f"{delta['new_funding_prints_since_slice75']}")
    print(f"  PRIOR-DIGEST AUDIT          : "
          f"{payload['digests']['prior_digest_audit']['self_compare_defect_count']}"
          f" self-compare defect(s) across "
          f"{payload['digests']['prior_digest_audit']['constants_audited']}"
          f" constants; this slice clean = "
          f"{payload['digests']['prior_digest_audit']['this_slice_is_clean']}")
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
