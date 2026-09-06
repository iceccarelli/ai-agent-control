"""Slice 63 — the pack regressed, the window did not grow, and both are said so.

TWO FINDINGS, NEITHER OF THEM A NUMBER
======================================
**The pack is the slice-61 tree.** Everything slice 62 produced is missing from
it, and the baseline suite failed the same eight tests for the same reason. The
apparatus was restored byte-identically rather than re-implemented, and the
dated slice-62 records were restored rather than regenerated — a distinction
this file asserts mechanically, because "we copied it" and "we re-ran it and got
something similar" are indistinguishable by eye and very different in kind.

**The forward window did not grow.** `extension_present` is true — one closed
bar lies after `t1` — but it is the SAME bar slice 62 measured. `t1` does not
move, so once one bar lands that flag is permanently true and re-reporting it
reads like progress. The informative quantity is the delta, and it is zero.

THE TRAP THAT ALMOST FIRED
==========================
The human note quotes a linear sha256 that differs from slice 62's. Read alone
that says the corpus changed. Both are digests of the COMPRESSED file; the
uncompressed streams are byte-identical. `TestTheDigestTrap` pins the
distinction so the next slice cannot fall into it in the other direction.
"""
from __future__ import annotations

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

FRESHNESS = "artifacts/slice63_data_freshness.json"
FORWARD = "artifacts/slice63_forward_shadow.json"
GATE = "artifacts/slice63_promotion_gate.json"
RESTORED = "artifacts/slice63_restored_from_slice62.json"
PRIOR_FORWARD = "artifacts/slice62_forward_shadow.json"
PRIOR_FRESHNESS = "artifacts/slice62_data_freshness.json"

SLICE62_LINEAR_UNCOMPRESSED = (
    "32ca5971229f07ed866627d939ed07768f7e0e0d299a3d4c07d440c0b43bab22")


def load(path: str) -> dict:
    with open(os.path.join(REPO, path), encoding="utf-8") as handle:
        return json.load(handle)


def sha256(path: str) -> str:
    with open(os.path.join(REPO, path), "rb") as handle:
        return hashlib.sha256(handle.read()).hexdigest()


def prose(text: str) -> str:
    return " ".join(str(text).split())


# ===========================================================================
# 1 — the restoration is a restoration, not a re-run
# ===========================================================================


class TestTheRestorationIsWhatItSaysItIs:

    def test_every_frozen_record_it_names_still_matches(self):
        """Amended in slice 64. A restoration manifest verifies what is FROZEN.

        As written this also re-verified the restored APPARATUS against live
        files — which a later slice may legitimately amend, and slice 64 did
        amend three test files. A dated manifest cannot be the authority on
        files that are still being worked on; the CURRENT slice's manifest is,
        because that is where an amendment can be declared alongside its
        reason.

        So this asserts the half that is genuinely frozen — the dated records —
        and `tests/test_slice64_window_grew.py` verifies the live apparatus
        against `artifacts/slice64_restored_from_slice63.json`.
        """
        record = load(RESTORED)
        files = record["dated_records"]["files"]
        assert files
        for relative, digest in files.items():
            assert os.path.exists(os.path.join(REPO, relative)), relative
            assert sha256(relative) == digest, relative

    def test_it_still_names_the_apparatus_it_restored(self):
        """The list itself is the dated claim and must not shrink."""
        record = load(RESTORED)
        assert record["apparatus"]["reimplemented"] is False
        assert "tools/corpus_prefix.py" in record["apparatus"]["files"]
        assert len(record["apparatus"]["files"]) == 9

    def test_no_dated_record_was_amended(self):
        """Tests are living documents. Dated artefacts are not.

        Whatever slice 63 amended, it must not be one of the restored records
        of a measurement that was already taken.
        """
        record = load(RESTORED)
        amended = set(record["apparatus"].get("amended_in_slice63", {}))
        assert amended, "the manifest claims no amendment; one was made"
        assert not (amended & set(record["dated_records"]["files"]))
        for relative, digest in record["dated_records"]["files"].items():
            assert sha256(relative) == digest, relative
        # And the same holds one slice later, for slice 64's own amendments.
        later = load("artifacts/slice64_restored_from_slice63.json")
        assert not (set(later["apparatus"].get("amended_in_slice64", {}))
                    & set(later["dated_records"]["files"]))

    def test_the_dated_records_were_not_regenerated_on_this_tree(self):
        """The decisive one, and it is checkable rather than promised.

        Each restored slice-62 artefact carries the `git_commit` of the tree it
        was written on. That commit does not exist in THIS repository's
        history. A file regenerated here would carry a commit that does.
        """
        record = load(RESTORED)
        assert record["dated_records"]["regenerated_against_todays_corpus"] \
            is False
        checked = 0
        for relative in record["dated_records"]["files"]:
            if not relative.endswith(".json"):
                continue
            payload = load(relative)
            commit = payload.get("git_commit")
            if not commit:
                continue
            checked += 1
            probe = subprocess.run(
                ["git", "cat-file", "-e", f"{commit}^{{commit}}"],
                cwd=REPO, capture_output=True)
            assert probe.returncode != 0, (
                f"{relative} carries commit {commit}, which EXISTS in this "
                f"tree — the artefact was regenerated here, not restored")
        assert checked >= 3, "too few dated records carried a commit to check"

    def test_the_prior_freshness_artefact_now_disagrees_with_disk(self):
        """Correct dated-artefact behaviour, asserted so it stays correct.

        `slice62_data_freshness.json` records 5 funding prints after `t1`; the
        disk holds 6. If this ever passes by both being equal, someone has
        edited a dated record to agree with a later observation.
        """
        assert load(PRIOR_FRESHNESS)["after_t1_funding"] == 5
        assert cp.check(cp.FUNDING_BTC).appended_rows > 5

    def test_the_restored_apparatus_was_not_refitted_to_this_corpus(self):
        assert "WITHOUT modification" in \
            load(RESTORED)["the_restored_invariant_was_not_refitted"]
        # And the invariant genuinely holds here, on a corpus slice 62 never
        # saw — which is what makes the sentence above a fact and not a boast.
        for path, result in cp.check_all().items():
            assert result.append_only, f"{path}: {result.why_not()}"

    def test_the_pack_regression_is_recorded_not_glossed(self):
        record = load(RESTORED)
        evidence = record["evidence_the_pack_is_the_slice61_tree"]
        assert evidence["baseline_suite"]["same_eight_tests"] is True
        assert evidence["baseline_suite"]["slice62_baseline"] == \
            evidence["baseline_suite"]["slice63_baseline"]
        assert load(FRESHNESS)["pack_regression"]["recorded_in"] == RESTORED


# ===========================================================================
# 2 — the digest trap
# ===========================================================================


class TestTheDigestTrap:

    def test_the_note_quotes_the_compressed_digest(self):
        digests = load(FRESHNESS)["digests"]
        assert digests["note_quotes_the_compressed_digest"] is True
        assert digests["note_digest_matches_the_compressed_file"] is True
        assert digests["which_one_the_repository_pins"] == "uncompressed"

    def test_the_two_digests_differ_for_the_same_file(self):
        digests = load(FRESHNESS)["digests"]
        assert digests["linear_sha256_compressed"] != \
            digests["linear_sha256_uncompressed"]

    def test_the_artefact_records_that_it_was_identical_to_slice62(self):
        """Amended in slice 64. The claim was DATED, and it has expired.

        In slice 63 the linear corpus was byte-identical to slice 62's, and the
        digest trap would have hidden that. In slice 64 a bar arrived, so the
        file legitimately differs — asserting the old equality against today's
        disk would force the programme to fail the moment it succeeded.

        The finding is preserved where it belongs, in slice 63's own frozen
        artefact, and the live claim becomes the direction of travel.
        """
        assert load(FRESHNESS)["digests"][
            "linear_identical_to_slice62_uncompressed"] is True
        assert load(FRESHNESS)["digests"][
            "slice62_linear_sha256_uncompressed"] == SLICE62_LINEAR_UNCOMPRESSED

    def test_the_disk_has_since_grown_past_that_record(self):
        """And slice 63's artefact was not edited to keep up.

        If this fails while the test above passes, a dated record has been
        rewritten to agree with a later observation.
        """
        assert cp.check(cp.LINEAR_BTC).whole_file_sha256 != \
            SLICE62_LINEAR_UNCOMPRESSED
        assert cp.check(cp.LINEAR_BTC).appended_rows > \
            load(FRESHNESS)["after_t1_linear"]

    def test_the_pinned_digests_are_of_the_uncompressed_stream(self):
        """Which is why the pins survived a re-compression untouched."""
        folds = fb.load_folds()
        assert cp.check(cp.LINEAR_BTC).prefix_sha256 == \
            folds["sources"]["linear_bars"]["sha256_uncompressed"]
        assert cp.check(cp.FUNDING_BTC).prefix_sha256 == \
            folds["sources"]["funding"]["sha256_uncompressed"]


# ===========================================================================
# 3 — present is not grown
# ===========================================================================


class TestPresentIsNotGrown:

    def test_extension_is_present(self):
        assert load(FRESHNESS)["extension_present"] is True
        assert load(FRESHNESS)["after_t1_linear"] == 1
        assert load(FRESHNESS)["after_t1_dates"] == ["2026-08-10"]

    def test_the_window_did_not_grow(self):
        delta = load(FRESHNESS)["delta_since_slice62"]
        assert delta["new_linear_bars_since_slice62"] == 0
        assert delta["the_window_grew"] is False
        assert delta["new_funding_prints_since_slice62"] == 1

    def test_both_facts_are_reported_and_they_differ(self):
        """`extension_present` is permanently true once one bar lands. The
        delta is the quantity that carries information, and an artefact that
        reported only the first would read like progress every slice."""
        payload = load(FRESHNESS)
        assert payload["extension_present"] is True
        assert payload["delta_since_slice62"]["the_window_grew"] is False

    def test_the_forward_artefact_agrees_the_window_did_not_grow(self):
        grew = load(FORWARD)["window_grew_since_slice62"]
        assert grew["comparable"] is True
        assert grew["closed_forward_bars_previously"] == 1
        assert grew["closed_forward_bars_now"] == 1
        assert grew["delta"] == 0
        assert grew["grew"] is False

    def test_a_funding_print_is_not_a_decision(self):
        finding = prose(load(FRESHNESS)["delta_since_slice62"]["finding"])
        assert "THE DECISION CLOCK IS THE DAILY BAR" in finding

    def test_the_funding_seam_was_not_filled(self):
        seam = load(FRESHNESS)["funding_seam"]
        assert seam["missing_prints"] == ["2026-08-09T16:00:00+00:00"]
        assert seam["unchanged_from_slice62"] is True


# ===========================================================================
# 4 — the forward segment, and that it was not re-tuned
# ===========================================================================


class TestTheForwardSegment:

    def test_is_forward_observation_is_false(self):
        assert load(FORWARD)["is_forward_observation"] is False
        assert load(FORWARD)["forward_n_trades"] == 0
        assert load(FORWARD)["forward_observations_to_date"] == 0
        assert load(FORWARD)["trades"] == []

    def test_the_labelling_rule_is_unchanged(self):
        rule = prose(load(FORWARD)["labelling_rule"])
        assert "forward_n_trades > 0" in rule
        assert "NOT extension_present" in rule

    def test_the_ceiling_was_declared_and_held(self):
        ceiling = load(FORWARD)["ceiling"]
        assert ceiling["closed_forward_bars"] == 1
        assert ceiling["closed_forward_bars_needed_for_one_trade"] == \
            fb.HORIZON + 1
        assert ceiling["max_possible_forward_closed_trades"] == 0
        assert ceiling["observed_forward_closed_trades"] == 0
        assert ceiling["within_ceiling"] is True

    def test_the_ceiling_is_arithmetic(self):
        ceiling = load(FORWARD)["ceiling"]
        assert ceiling["max_possible_forward_closed_trades"] == \
            max(0, ceiling["closed_forward_bars"] - fb.HORIZON)

    def test_the_scoring_logic_was_not_re_tuned_between_slices(self):
        """The strongest available claim that this is a measurement and not a
        series of one-off estimates.

        Compared as SYNTAX TREES, not as text, and per function — so prose,
        comments, blank lines and the module docstring cannot make it pass or
        fail. For every function slice 62's tool defines, the same-named
        function in slice 63's tool must be IDENTICAL once the five identifier
        substitutions are applied.

        Slice 63 may ADD (it adds two helpers and four artefact fields). It may
        not change or remove, and `build` — the function that actually scores
        the segment — is covered by this.
        """
        import ast  # noqa: PLC0415

        substitutions = {
            "forward_shadow_observation/2": "forward_shadow_observation/3",
            "slice62_forward_shadow.json": "slice63_forward_shadow.json",
            "slice62_forward_shadow.log": "slice63_forward_shadow.log",
            "SLICE 62 — FORWARD SHADOW SEGMENT":
                "SLICE 63 — FORWARD SHADOW SEGMENT",
        }

        class Normalise(ast.NodeTransformer):
            def visit_Constant(self, node):          # noqa: N802
                if isinstance(node.value, str):
                    value = node.value
                    for before, after in substitutions.items():
                        value = value.replace(before, after)
                    return ast.copy_location(ast.Constant(value), node)
                if node.value == 62:
                    return ast.copy_location(ast.Constant(63), node)
                return node

        def functions(name):
            with open(os.path.join(REPO, "tools", name),
                      encoding="utf-8") as handle:
                tree = Normalise().visit(ast.parse(handle.read()))
            out = {}
            for node in tree.body:
                if isinstance(node, ast.FunctionDef):
                    node.body = [s for s in node.body
                                 if not (isinstance(s, ast.Expr)
                                         and isinstance(s.value, ast.Constant)
                                         and isinstance(s.value.value, str))]
                    out[node.name] = ast.dump(node)
            return out

        old = functions("slice62_forward_shadow.py")
        new = functions("slice63_forward_shadow.py")
        assert "build" in old and "build" in new, "the scoring function moved"

        # Every function except `build` must be identical outright.
        changed = [name for name, dump in old.items()
                   if name != "build" and new.get(name) != dump]
        assert changed == [], changed
        assert set(new) >= set(old)

        # `build` legitimately gained four keys in the artefact it returns, so
        # it is compared in two halves: everything BEFORE the return — which is
        # the whole of the scoring — must be identical, and every key slice 62
        # emitted must still map to the same expression.
        def split(name):
            with open(os.path.join(REPO, "tools", name),
                      encoding="utf-8") as handle:
                tree = Normalise().visit(ast.parse(handle.read()))
            build = next(n for n in tree.body
                         if isinstance(n, ast.FunctionDef) and n.name == "build")
            *before, final = build.body
            assert isinstance(final, ast.Return)
            before = [s for s in before
                      if not (isinstance(s, ast.Expr)
                              and isinstance(s.value, ast.Constant)
                              and isinstance(s.value.value, str))]
            payload = {ast.dump(k): ast.dump(v)
                       for k, v in zip(final.value.keys, final.value.values)}
            return [ast.dump(s) for s in before], payload

        old_body, old_payload = split("slice62_forward_shadow.py")
        new_body, new_payload = split("slice63_forward_shadow.py")

        assert old_body == new_body, "the SCORING changed between slices"
        drifted = [key for key, value in old_payload.items()
                   if new_payload.get(key) != value]
        assert drifted == [], drifted
        assert len(new_payload) > len(old_payload), "no fields were added"

    def test_no_exit_was_clamped_and_no_bar_invented(self):
        assert load(FORWARD)["forward_decisions"][
            "exit_clamping_to_corpus_end"] is False
        assert load(FORWARD)["forward_window"]["invented_future_bars"] == 0
        assert load(FORWARD)["forward_window"]["bar_array_sliced"] is False
        assert load(FORWARD)["bars_fabricated"] == 0
        assert load(FRESHNESS)["bars_fabricated"] == 0

    def test_the_forward_monitors_report_insufficient_data(self):
        states = {r["name"]: r["state"]
                  for r in load(FORWARD)["forward_monitor_readings"]}
        assert set(states.values()) == {"INSUFFICIENT_DATA"}
        assert load(FORWARD)["forward_monitor_status"] == "INSUFFICIENT_DATA"

    def test_the_historical_m4_warn_was_not_rebranded_as_forward(self):
        historical = load("artifacts/slice59_forward_shadow.json")
        m4 = next(r for r in historical["monitor_readings"]
                  if r["name"] == "M4_halves")
        assert m4["state"] == "WARN"
        forward = {r["name"]: r["state"]
                   for r in load(FORWARD)["forward_monitor_readings"]}
        assert forward["M4_halves"] == "INSUFFICIENT_DATA"
        assert "deliberately NOT recomputed" in \
            prose(load(FORWARD)["monitor_scope_note"])

    def test_the_gate_answer_is_read_not_asserted(self):
        assert load(FORWARD)["promotion_gate_allows_live"] == \
            pg.promotion_gate_allows_live()


# ===========================================================================
# 5 — nothing frozen moved, and the gate still refuses
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

    def test_the_oos_clear_was_not_re_scored(self):
        assert sha256("artifacts/slice57_oos_edge_BTCUSDT_summary.json") == \
            "28b7dfe0f22c4867661bc73f6434c4acce54a0483c4469a6540155b1b3c18c51"
        assert load(FORWARD)["cleared_edge_re_scored_this_slice"] is False
        assert load(FORWARD)["is_stage1_evidence"] is False
        assert load(FORWARD)["registration_eligible"] is False
        assert fb.folds_sha256() == \
            "ff5cc8a2bb92362058b376659ff12f314c10f71a101d1f581f69da31f025013c"
        assert fb.folds_are_unmodified() is True

    def test_the_gate_refuses(self):
        assert pg.promotion_gate_allows_live() is False
        assert load(GATE)["promotion_gate_allows_live"] is False
        assert load(GATE)["items_complete"] == 2
        assert load(GATE)["items_total"] == 8
        assert load(GATE)["items_complete_unchanged_from_slice_62"] is True

    def test_the_minimums_did_not_move(self):
        assert pg.MIN_FORWARD_TRADES == 20
        assert pg.MIN_FORWARD_DAYS == 180
        assert load(GATE)["minimums_moved_this_slice"] is False

    def test_the_gate_evidence_names_the_unchanged_reason(self):
        evidence = prose(load(GATE)["checklist"]["forward_shadow_clean"][
            "evidence"])
        assert "UNCHANGED FROM SLICE 62" in evidence
        assert "the forward window did not grow" in evidence

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
        assert not os.path.exists(os.path.join(REPO, "models", "current"))
        assert status.policy_mode == "off"

    def test_not_closer_to_autonomy(self):
        for path in (FORWARD, GATE):
            assert load(path)["closer_to_autonomous_profit_agent"] is False

    def test_the_cleared_edge_signal_is_unchanged(self):
        assert ps.current().cleared_edge_signal == "funding_carry_fade_btc_v1"

    def test_no_fetch_was_attempted(self):
        assert load(FRESHNESS)["fetch_attempted"] is False
