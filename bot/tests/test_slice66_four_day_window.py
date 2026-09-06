"""Slice 66 — four bars, the last structural zero, and a mistake made unshippable.

WHAT IS NEW HERE
================
**A prediction, recorded before the fact.** The ceiling is `max(0, |W| - HORIZON)`.
At `|W| = 4` it is 0; at 5 it is still 0; at **6 it becomes 1**. So two more
closed days take the ceiling off zero for the first time since `t1` was locked.
`TestTheCeilingIsAboutToStopBeingStructural` asserts that arithmetic against the
frozen constants, so slices 67 and 68 can be checked against it — and if `|W| = 6`
arrives and the ceiling is still reported as zero, this file goes red and the
defect is in the reasoning, not in the result.

**Five zeros, and none of them is evidence.** Slices 62–66 have all reported
`forward_n_trades = 0`, and every one ran at ceiling 0. The rule has never been
given an opportunity. From `|W| = 6` a zero would carry information; recording
the transition in advance means nobody decides afterwards which kind of zero
they were looking at.

**The recurring test defect is made unshippable rather than repaired again.**
Five consecutive slices have had to amend the previous slice's tests for pinning
a LIVE ABSOLUTE — `appended_rows == 1`, `== 2`, `== 3`, hard-coded dates — each
true when written and false the moment the window grew. Slice 65 wrote the rule
down and then broke it three times in its own file. A rule that needs restating
every slice is not working, so `TestNoTestPinsALiveAbsolute` walks every test
file's syntax tree and fails any equality between a live corpus count and an
integer literal. AST, not text search — EDGE.md §12c, seven times over.
"""
from __future__ import annotations

import ast
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
import project_status as ps  # noqa: E402
import promotion_gate as pg  # noqa: E402
import shadow  # noqa: E402
from signals import funding_carry_fade_btc_v1 as fb  # noqa: E402

FRESHNESS = "artifacts/slice66_data_freshness.json"
FORWARD = "artifacts/slice66_forward_shadow.json"
GATE = "artifacts/slice66_promotion_gate.json"
RESTORED = "artifacts/slice66_restored_from_slice65.json"

# slice -> (freshness artefact, forward artefact, window size it saw)
PRIOR = {
    62: ("artifacts/slice62_data_freshness.json",
         "artifacts/slice62_forward_shadow.json", 1),
    63: ("artifacts/slice63_data_freshness.json",
         "artifacts/slice63_forward_shadow.json", 1),
    64: ("artifacts/slice64_data_freshness.json",
         "artifacts/slice64_forward_shadow.json", 2),
    65: ("artifacts/slice65_data_freshness.json",
         "artifacts/slice65_forward_shadow.json", 3),
}

LIVE_COUNT_ATTRIBUTES = ("appended_rows", "rows_on_disk")


def load(path: str) -> dict:
    with open(os.path.join(REPO, path), encoding="utf-8") as handle:
        return json.load(handle)


def sha256(path: str) -> str:
    with open(os.path.join(REPO, path), "rb") as handle:
        return hashlib.sha256(handle.read()).hexdigest()


def prose(text: str) -> str:
    return " ".join(str(text).split())


# ===========================================================================
# 1 — the window grew to four, from files
# ===========================================================================


class TestTheWindowGrewToFour:

    def test_four_closed_bars_lie_after_t1(self):
        result = cp.check(cp.LINEAR_BTC)
        assert result.appended_rows >= 4
        assert [s[:10] for s in result.appended_timestamps][:4] == \
            ["2026-08-10", "2026-08-11", "2026-08-12", "2026-08-13"]
        assert load(FRESHNESS)["after_t1_linear"] == 4
        assert load(FRESHNESS)["after_t1_dates"] == \
            ["2026-08-10", "2026-08-11", "2026-08-12", "2026-08-13"]

    def test_the_growth_is_measured_against_the_previous_artefact(self):
        delta = load(FRESHNESS)["delta_since_slice65"]
        assert delta["previous_after_t1_linear"] == 3
        assert delta["new_linear_bars_since_slice65"] == 1
        assert delta["new_funding_prints_since_slice65"] == 3
        assert delta["the_window_grew"] is True

    def test_the_forward_artefact_agrees(self):
        grew = load(FORWARD)["window_grew_since_slice65"]
        assert grew["closed_forward_bars_previously"] == 3
        assert grew["closed_forward_bars_now"] == 4
        assert grew["delta"] == 1
        assert grew["grew"] is True

    def test_history_is_untouched_and_growth_is_an_append(self):
        folds = fb.load_folds()
        for path, result in cp.check_all().items():
            assert result.append_only, f"{path}: {result.why_not()}"
            assert result.appended_all_strictly_after_t1
        assert cp.check(cp.LINEAR_BTC).prefix_sha256 == \
            folds["sources"]["linear_bars"]["sha256_uncompressed"]
        assert cp.check(cp.FUNDING_BTC).prefix_sha256 == \
            folds["sources"]["funding"]["sha256_uncompressed"]
        assert load(FRESHNESS)["prefix_invariant"]["history_rewritten"] is False

    def test_only_btc_grew(self):
        grew = {p for p, r in cp.check_all().items() if r.extended}
        assert grew == {cp.LINEAR_BTC, cp.FUNDING_BTC}
        for path, result in cp.check_all().items():
            if "BTC" not in path:
                assert result.appended_rows == 0, path

    def test_no_unclosed_bar_is_on_disk(self):
        """Clock-derived, never a hard-coded date."""
        today = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
        rows = cp.read_rows(cp.LINEAR_BTC)
        assert not any(r["time_period_start"][:10] >= today for r in rows)
        assert load(FRESHNESS)["open_bar_absent_as_expected"] is True

    def test_every_note_claim_was_checked_against_the_files(self):
        payload = load(FRESHNESS)
        assert payload["human_note_is_evidence"] is False
        assert payload["claim_vs_files_discrepancy"] is False
        assert len(payload["human_note_claims_checked"]) >= 6
        assert all(payload["human_note_claims_checked"].values())

    def test_the_note_again_quotes_the_compressed_digest(self):
        digests = load(FRESHNESS)["digests"]
        assert digests["note_quotes_the_compressed_digest"] is True
        assert digests["note_digest_matches_the_compressed_file"] is True
        assert digests["linear_sha256_compressed"] != \
            digests["linear_sha256_uncompressed"]


# ===========================================================================
# 2 — the ceiling, and the prediction
# ===========================================================================


class TestTheCeilingIsAboutToStopBeingStructural:

    def test_the_ceiling_is_still_zero_at_four_bars(self):
        ceiling = load(FORWARD)["ceiling"]
        assert ceiling["closed_forward_bars"] == 4
        assert ceiling["closed_forward_bars_needed_for_one_trade"] == \
            fb.HORIZON + 1 == 6
        assert ceiling["max_possible_forward_closed_trades"] == 0
        assert ceiling["observed_forward_closed_trades"] == 0
        assert ceiling["within_ceiling"] is True
        assert "§49b" in ceiling["declared_in"]

    def test_the_ceiling_is_arithmetic_not_a_stored_zero(self):
        ceiling = load(FORWARD)["ceiling"]
        assert ceiling["max_possible_forward_closed_trades"] == \
            max(0, ceiling["closed_forward_bars"] - fb.HORIZON)

    def test_the_prediction_five_still_zero_six_becomes_one(self):
        """Recorded BEFORE the data arrives, so slices 67-68 can check it.

        Derived from the frozen HORIZON rather than written as literals, so a
        changed horizon would move the prediction rather than silently
        invalidate it.
        """
        def ceiling_at(bars):
            return max(0, bars - fb.HORIZON)

        assert ceiling_at(4) == 0
        assert ceiling_at(5) == 0
        assert ceiling_at(6) == 1
        assert fb.HORIZON == 5

    def test_two_more_closed_days_are_needed(self):
        ceiling = load(FORWARD)["ceiling"]
        needed = (ceiling["closed_forward_bars_needed_for_one_trade"]
                  - ceiling["closed_forward_bars"])
        assert needed == 2

    def test_the_prediction_is_stated_in_the_artefact(self):
        note = prose(load(FORWARD)["window_grew_since_slice65"]["note"])
        assert "TWO MORE CLOSED DAYS" in note
        assert "|W| = 6 yields 1" in note
        assert "checkable in slices 67-68" in note


class TestFiveZerosAndWhatTheyMean:

    def test_every_prior_slice_also_ran_against_a_zero_ceiling(self):
        """The load-bearing one. Read from each prior slice's frozen artefact.

        If any had run with a POSITIVE ceiling and still returned nothing, the
        sequence would mean the opposite — the rule would have had an
        opportunity and declined it.
        """
        for slice_number, (_fresh, forward, bars) in sorted(PRIOR.items()):
            payload = load(forward)
            assert payload["forward_n_trades"] == 0, slice_number
            assert payload["ceiling"]["closed_forward_bars"] == bars
            assert payload["ceiling"][
                "max_possible_forward_closed_trades"] == 0, slice_number
            assert max(0, bars - fb.HORIZON) == 0, slice_number

    def test_the_window_sizes_form_the_expected_sequence(self):
        sizes = [load(f)["ceiling"]["closed_forward_bars"]
                 for _s, (_x, f, _b) in sorted(PRIOR.items())]
        assert sizes == [1, 1, 2, 3]
        assert load(FORWARD)["ceiling"]["closed_forward_bars"] == 4

    def test_none_of_the_five_zeros_is_evidence_about_the_edge(self):
        note = prose(load(FORWARD)["window_grew_since_slice65"]["note"])
        assert "none of them is evidence about the edge" in note
        assert "a zero WOULD carry information" in note

    def test_growth_and_observation_are_independent_and_disagree(self):
        assert load(FRESHNESS)["delta_since_slice65"]["the_window_grew"] is True
        assert load(FORWARD)["is_forward_observation"] is False
        assert load(FORWARD)["forward_n_trades"] == 0
        assert load(FORWARD)["forward_observations_to_date"] == 0
        assert load(FORWARD)["trades"] == []

    def test_the_rule_stood_aside_on_all_four_bars(self):
        aside = load(FORWARD)["forward_decisions"]["why_the_rule_stood_aside"]
        assert len(aside) == 4
        assert all(entry["funding_setup_present"] is False for entry in aside)
        last = [e for e in aside if e["is_last_bar_of_corpus"]]
        assert len(last) == 1 and last[0]["bar_utc"].startswith("2026-08-13")

    def test_three_of_four_have_only_the_funding_reason(self):
        aside = load(FORWARD)["forward_decisions"]["why_the_rule_stood_aside"]
        sole = [e for e in aside if not e["is_last_bar_of_corpus"]]
        assert len(sole) == 3
        assert all(e["fund_abs_threshold"] == fb.FUND_ABS for e in sole)

    def test_no_clamping_no_slicing_no_invention(self):
        assert load(FORWARD)["forward_decisions"][
            "exit_clamping_to_corpus_end"] is False
        assert load(FORWARD)["forward_window"]["bar_array_sliced"] is False
        assert load(FORWARD)["forward_window"]["invented_future_bars"] == 0
        assert load(FORWARD)["bars_fabricated"] == 0
        assert load(FRESHNESS)["bars_fabricated"] == 0
        assert load(FRESHNESS)["corpus_appended_to_by_this_tool"] is False

    def test_the_forward_monitors_report_insufficient_data(self):
        states = {r["name"]: r["state"]
                  for r in load(FORWARD)["forward_monitor_readings"]}
        assert set(states.values()) == {"INSUFFICIENT_DATA"}
        assert load(FORWARD)["forward_monitor_status"] == "INSUFFICIENT_DATA"

    def test_the_historical_m4_warn_was_not_rebranded(self):
        historical = load("artifacts/slice59_forward_shadow.json")
        m4 = next(r for r in historical["monitor_readings"]
                  if r["name"] == "M4_halves")
        assert m4["state"] == "WARN"
        assert "deliberately NOT recomputed" in \
            prose(load(FORWARD)["monitor_scope_note"])

    def test_the_scoring_was_not_re_tuned_since_slice65(self):
        substitutions = {
            "forward_shadow_observation/5": "forward_shadow_observation/6",
            "slice65_forward_shadow.json": "slice66_forward_shadow.json",
            "slice65_forward_shadow.log": "slice66_forward_shadow.log",
            "window_grew_since_slice64": "window_grew_since_slice65",
            "slice64_forward_shadow.json": "slice65_forward_shadow.json",
            "SLICE 65 — FORWARD SHADOW SEGMENT":
                "SLICE 66 — FORWARD SHADOW SEGMENT",
            "EDGE.md §45e, before this tool ran":
                "EDGE.md §49b, before this tool ran",
            "THE CEILING, DECLARED BEFORE THIS RUN (EDGE.md §45e)":
                "THE CEILING, DECLARED BEFORE THIS RUN (EDGE.md §49b)",
        }

        class Normalise(ast.NodeTransformer):
            def visit_Constant(self, node):          # noqa: N802
                if isinstance(node.value, str):
                    value = node.value
                    for before, after in substitutions.items():
                        value = value.replace(before, after)
                    return ast.copy_location(ast.Constant(value), node)
                if node.value == 65:
                    return ast.copy_location(ast.Constant(66), node)
                return node

        def split(name):
            with open(os.path.join(REPO, "tools", name),
                      encoding="utf-8") as handle:
                tree = Normalise().visit(ast.parse(handle.read()))
            build = next(n for n in tree.body
                         if isinstance(n, ast.FunctionDef)
                         and n.name == "build")
            *before, final = build.body
            assert isinstance(final, ast.Return)
            before = [s for s in before
                      if not (isinstance(s, ast.Expr)
                              and isinstance(s.value, ast.Constant)
                              and isinstance(s.value.value, str))]
            payload = {ast.dump(k): ast.dump(v)
                       for k, v in zip(final.value.keys, final.value.values)}
            return [ast.dump(s) for s in before], payload

        old_body, old_payload = split("slice65_forward_shadow.py")
        new_body, new_payload = split("slice66_forward_shadow.py")
        assert old_body == new_body, "the SCORING changed between slices"
        drifted = [key for key, value in old_payload.items()
                   if new_payload.get(key) != value
                   and "note" not in key.lower()]
        assert drifted == [], drifted


# ===========================================================================
# 3 — THE STRUCTURAL FIX: the live-absolute mistake is made unshippable
# ===========================================================================


class TestNoTestPinsALiveAbsolute:
    """Five slices in a row amended the previous slice's tests for the same
    mistake. A rule that needs restating every slice is not working.

    The mistake has a precise syntactic form: an EQUALITY between a value read
    live from the corpus — `cp.check(...).appended_rows`, `.rows_on_disk` — and
    a POSITIVE integer literal. Such an assertion is a snapshot of a quantity
    that is expected to grow: true on the day it is written, false the moment
    the window grows, which is the event the programme exists to wait for.

    **`== 0` is deliberately permitted, and the distinction is not a
    convenience.** Zero is the one value that is not a snapshot of growth — it
    asserts that growth has NOT occurred, which is exactly the claim
    "ETH and SOL were never extended" that carries the manifest finding. When a
    `== 0` fails, the failure IS the finding; when a `== 4` fails, the test was
    merely stale. Those are opposite meanings and the guard distinguishes them.

    `>=`, `>`, and comparisons against a value read from a FROZEN ARTEFACT are
    all fine and none is flagged. This is an AST check, not a text search:
    §12c has recorded seven times that a guard written as a text ban stops the
    code explaining what it forbids.
    """

    @staticmethod
    def _offences(path):
        with open(path, encoding="utf-8") as handle:
            tree = ast.parse(handle.read(), filename=path)

        def is_live_count(node):
            return (isinstance(node, ast.Attribute)
                    and node.attr in LIVE_COUNT_ATTRIBUTES
                    and isinstance(node.value, ast.Call))

        found = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Compare):
                continue
            operands = [node.left] + list(node.comparators)
            for op, right in zip(node.ops, node.comparators):
                if not isinstance(op, ast.Eq):
                    continue
                for a, b in ((node.left, right), (right, node.left)):
                    if (is_live_count(a)
                            and isinstance(b, ast.Constant)
                            and isinstance(b.value, int)
                            and not isinstance(b.value, bool)
                            and b.value > 0):
                        found.append((node.lineno, ast.unparse(node)))
            del operands
        return found

    def _test_files(self):
        base = os.path.join(REPO, "tests")
        return [os.path.join(base, name) for name in sorted(os.listdir(base))
                if name.startswith("test_") and name.endswith(".py")]

    def test_no_test_file_compares_a_live_corpus_count_to_a_literal(self):
        offences = {}
        for path in self._test_files():
            found = self._offences(path)
            if found:
                offences[os.path.basename(path)] = found
        assert offences == {}, (
            "A live corpus count is pinned to an integer literal. It is true "
            "today and false the moment the window grows. Assert the dated "
            "claim against the frozen artefact and only the direction of "
            "travel against disk. EDGE.md §49f.\n" + json.dumps(offences,
                                                                indent=2))

    def test_the_checker_catches_the_exact_form_it_is_meant_to(self):
        """A guard never observed to fail is not known to be a guard.

        Five snippets: two offences and three innocents that must NOT be
        flagged, so the check is shown to DISCRIMINATE rather than to accept
        or reject everything. The `== 0` case is the one that matters most —
        it is an absence claim, not a snapshot, and flagging it would have
        deleted the assertion that carries the ETH/SOL manifest finding.
        """
        import tempfile

        cases = {
            # offences: a growing count pinned to today's value
            "assert cp.check(cp.LINEAR_BTC).appended_rows == 4": 1,
            "assert cp.check(p).rows_on_disk == 1465": 1,
            # innocents, each for a different reason
            "assert cp.check(cp.LINEAR_BTC).appended_rows >= 4": 0,
            "assert cp.check(p).rows_on_disk == payload['linear_rows']": 0,
            "assert cp.check(path).appended_rows == 0": 0,
        }
        for source, expected in cases.items():
            with tempfile.NamedTemporaryFile("w", suffix=".py",
                                             delete=False) as handle:
                handle.write(source + "\n")
                name = handle.name
            try:
                assert len(self._offences(name)) == expected, source
            finally:
                os.unlink(name)

    def test_the_sweep_actually_reached_the_slice_test_files(self):
        """Without this, an empty offence set could mean an empty sweep."""
        names = {os.path.basename(p) for p in self._test_files()}
        for slice_number in (62, 63, 64, 65, 66):
            assert any(f"slice{slice_number}" in name for name in names), \
                slice_number
        assert len(names) > 30


# ===========================================================================
# 4 — the defects, at four and three occurrences
# ===========================================================================


class TestBothDefectsRecurred:

    def test_the_pack_regression_is_the_fourth(self):
        record = load(RESTORED)
        evidence = record["evidence_the_pack_is_the_slice61_tree"]
        assert evidence["occurrences"] == 4
        assert record["restores_slices"] == [62, 63, 64, 65]
        assert evidence["baseline_suite"]["identical_to_slices_64_and_65"] \
            is True

    def test_the_manifest_broadcast_is_the_third(self):
        audit = load(FRESHNESS)["manifests"]
        assert audit["count_disagreeing"] == 4
        assert audit["measured_product_entries_are_accurate"] is True
        assert audit["not_edited_by_this_slice"] is True

    def test_sol_funding_has_been_declared_wrong_three_different_ways(self):
        from test_slice55_data_eligibility import (  # noqa: PLC0415
            TestTheManifestsDescribeTheFilesBesideThem as Manifests)
        history = Manifests.SOL_FUNDING_DECLARED_HISTORY
        assert history == (4392, 4394, 4397)
        assert len(set(history)) == 3
        _path, disk = Manifests._on_disk("data/real_funding", "SOLUSDT")
        assert disk == 4458
        assert all(value != disk for value in history)

    def test_slice66s_counts_came_from_files_not_the_manifest(self):
        """Amended in slice 67: compared against slice 66's OWN frozen prefix
        figures rather than today's disk, which has since moved on. The live
        equivalent belongs to whichever slice is current."""
        payload = load(FRESHNESS)
        linear = payload["prefix_invariant"]["checks"][cp.LINEAR_BTC]
        funding = payload["prefix_invariant"]["checks"][cp.FUNDING_BTC]
        assert payload["linear_rows"] == linear["rows_on_disk"] == 1465
        assert payload["funding_rows"] == funding["rows_on_disk"] == 4397
        assert payload["manifests"]["count_disagreeing"] == 4

    def test_the_manifests_were_not_edited(self):
        assert load(FRESHNESS)["manifests_edited_by_this_slice"] is False
        assert "cannot attest" in prose(
            load(FRESHNESS)["why_manifests_not_edited"])


# ===========================================================================
# 5 — the restoration
# ===========================================================================


class TestTheRestoration:

    def test_every_frozen_record_it_names_still_matches(self):
        """Amended in slice 67, under the rule slice 65 wrote down:

        > each slice's restoration manifest owns the live apparatus; every
        > earlier one owns only its frozen records.

        This is the third consecutive slice to apply that rule to its
        predecessor's restoration test, which is itself worth noticing: the
        rule works, and the amendment it produces is mechanical.
        """
        record = load(RESTORED)
        files = record["dated_records"]["files"]
        assert files
        for relative, digest in files.items():
            assert os.path.exists(os.path.join(REPO, relative)), relative
            assert sha256(relative) == digest, relative

    def test_it_still_names_the_apparatus_it_restored(self):
        record = load(RESTORED)
        assert record["apparatus"]["reimplemented"] is False
        assert "tools/corpus_prefix.py" in record["apparatus"]["files"]
        assert len(record["apparatus"]["files"]) == 21

    def test_no_dated_record_was_amended(self):
        record = load(RESTORED)
        amended = set(record["apparatus"].get("amended_in_slice66", {}))
        assert amended
        assert not (amended & set(record["dated_records"]["files"]))

    def test_the_dated_records_were_not_regenerated_on_this_tree(self):
        record = load(RESTORED)
        assert record["dated_records"]["regenerated_against_todays_corpus"] \
            is False
        checked = 0
        for relative in record["dated_records"]["files"]:
            if not relative.endswith(".json"):
                continue
            commit = load(relative).get("git_commit")
            if not commit:
                continue
            checked += 1
            probe = subprocess.run(
                ["git", "cat-file", "-e", f"{commit}^{{commit}}"],
                cwd=REPO, capture_output=True)
            assert probe.returncode != 0, (
                f"{relative} carries commit {commit}, which EXISTS here — it "
                f"was regenerated, not restored")
        assert checked >= 12, checked

    def test_the_prior_artefacts_still_record_their_own_window_sizes(self):
        for slice_number, (fresh, _forward, bars) in sorted(PRIOR.items()):
            assert load(fresh)["after_t1_linear"] == bars, slice_number


# ===========================================================================
# 6 — nothing frozen moved, and the gate still refuses
# ===========================================================================


class TestNothingMovedAndTheGateRefuses:

    def test_the_frozen_pack_is_intact(self):
        assert shadow.constants_fingerprint()[:32] == \
            "662de0115880871352d5d623b1020eaa"
        assert shadow.SHADOW_MAX_CONCURRENT_POSITIONS == 1
        assert shadow.SHADOW_MAX_ENTRIES_PER_DAY == 1
        assert shadow.SHADOW_MAX_NOTIONAL_USD == 100.00
        assert load(FORWARD)["constants_fingerprint_matches_frozen"] is True
        assert load(FORWARD)["schedule_mode"] == "one_entry_per_contiguous_run"
        assert load(FORWARD)["caps_changed_this_slice"] is False
        assert load(FORWARD)["thresholds_moved_this_slice"] is False
        assert load(FORWARD)["caps"]["max_notional_usd"] == 100.00

    def test_the_monitor_thresholds_are_unchanged(self):
        thresholds = shadow.MONITOR_THRESHOLDS
        assert thresholds["M1_rolling_trades"]["warn_below"] == 0.0
        assert thresholds["M1_rolling_trades"]["alert_below"] == -0.25
        assert thresholds["M2_rolling_days"]["window_days"] == 90
        assert thresholds["M3_concentration"]["warn_above"] == 0.60
        assert thresholds["M4_halves"]["min_trades"] == 20

    def test_eleven_families_stay_frozen(self):
        assert len(ps.ABSENT_SIGNALS) == 11
        assert "funding_carry_fade_v1" in ps.ABSENT_SIGNALS
        assert load(FORWARD)["frozen_absent_count"] == 11

    def test_eth_and_sol_were_not_measured_under_the_btc_name(self):
        assert fb.UNIVERSE == ("BTCUSDT",)
        assert load(FORWARD)["symbol"] == "BTCUSDT"

    def test_the_oos_clear_was_not_re_scored(self):
        assert sha256("artifacts/slice57_oos_edge_BTCUSDT_summary.json") == \
            "28b7dfe0f22c4867661bc73f6434c4acce54a0483c4469a6540155b1b3c18c51"
        assert load(FORWARD)["cleared_edge_re_scored_this_slice"] is False
        assert load(FORWARD)["is_stage1_evidence"] is False
        assert load(FORWARD)["registration_eligible"] is False
        assert load(FORWARD)["forward_mean_net_r"] is None
        assert fb.folds_sha256() == \
            "ff5cc8a2bb92362058b376659ff12f314c10f71a101d1f581f69da31f025013c"
        assert fb.folds_are_unmodified() is True

    def test_no_edge_measurement_ran_this_slice(self):
        """The mission forbids it explicitly. No slice-66 artefact carries a
        percentile, a control attestation or a replicate count."""
        for name in sorted(os.listdir(os.path.join(REPO, "artifacts"))):
            if not name.startswith("slice66_") or not name.endswith(".json"):
                continue
            text = json.dumps(load(f"artifacts/{name}"))
            for banned in ("m1_percentile", "replicates",
                           "control_attestation", "EDGE_EVIDENCE_POSITIVE"):
                assert banned not in text, (name, banned)

    def test_the_gate_refuses(self):
        assert pg.promotion_gate_allows_live() is False
        assert load(GATE)["promotion_gate_allows_live"] is False
        assert load(GATE)["items_complete"] == 2
        assert load(GATE)["items_total"] == 8
        assert load(FORWARD)["promotion_gate_allows_live"] == \
            pg.promotion_gate_allows_live()

    def test_the_minimums_did_not_move(self):
        assert pg.MIN_FORWARD_TRADES == 20
        assert pg.MIN_FORWARD_DAYS == 180
        assert load(GATE)["minimums_moved_this_slice"] is False

    def test_the_gate_evidence_names_the_sequence_and_the_transition(self):
        evidence = prose(load(GATE)["checklist"]["forward_shadow_clean"][
            "evidence"])
        assert "THE WINDOW GREW AGAIN" in evidence
        assert "NOT FIVE FAILED ATTEMPTS" in evidence
        assert "Two more closed days" in evidence

    def test_no_human_item_was_completed_in_code(self):
        assert load(GATE)["human_items_completed_in_code"] == 0
        assert load(GATE)["signatures_forged"] is False
        human = [item for item in load(GATE)["evaluated"]["items"]
                 if item["owner"].startswith("human")]
        assert human and not any(item["complete"] for item in human)

    def test_live_is_dark(self):
        status = ps.current()
        assert bool(status.live_authorized) is False
        assert bool(status.models_current_present) is False
        assert status.policy_mode == "off"
        assert not os.path.exists(os.path.join(REPO, "models", "current"))

    def test_not_closer_to_autonomy(self):
        for path in (FORWARD, GATE):
            assert load(path)["closer_to_autonomous_profit_agent"] is False

    def test_the_funding_seam_is_still_unfilled(self):
        assert load(FRESHNESS)["funding_seam"]["missing_prints"] == \
            ["2026-08-09T16:00:00+00:00"]

    def test_no_fetch_was_attempted(self):
        assert load(FRESHNESS)["fetch_attempted"] is False
