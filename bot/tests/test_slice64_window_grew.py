"""Slice 64 — the window grew, the ceiling did not, and a manifest went false.

THREE THINGS TO GET RIGHT
=========================
**The growth is real and must be reported as growth.** `after_t1_linear` moved
1 → 2 for the first time since `t1` was locked. Slice 63 had to report that
nothing moved; reporting the same thing here would be as dishonest as reporting
progress there.

**The growth changes nothing about the answer.** `max(0, 2 - 5) = 0`. Six closed
forward bars are needed before one trade can close. A bigger window producing no
evidence is pilot progress and no evidence, and those are different sentences —
so `the_window_grew` and `is_forward_observation` are asserted independently and
required to disagree.

**A manifest now asserts data that does not exist.** Four of six entries are
false and one of them understates a corpus. It is not edited; it is enumerated,
in `tests/test_slice55_data_eligibility.py`. This file asserts that the defect
did not touch the measured product and that the freshness artefact records it.

THE LESSON THAT KEEPS RECURRING
===============================
Four test files had to be amended this slice because they pinned a LIVE ABSOLUTE
— `appended_rows == 1`, `== 2`, `== 6`, a byte-identity — each true when written
and false the moment the window moved. That is the fourth appearance of the
dated-artefact-versus-living-document confusion (slices 54, 57, 62, 63). The
repair is always the same shape: assert the dated claim against the FROZEN
ARTEFACT, and assert only the DIRECTION OF TRAVEL against live disk.
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

FRESHNESS = "artifacts/slice64_data_freshness.json"
FORWARD = "artifacts/slice64_forward_shadow.json"
GATE = "artifacts/slice64_promotion_gate.json"
RESTORED = "artifacts/slice64_restored_from_slice63.json"
PRIOR_FORWARD = "artifacts/slice63_forward_shadow.json"
PRIOR_FRESHNESS = "artifacts/slice63_data_freshness.json"


def load(path: str) -> dict:
    with open(os.path.join(REPO, path), encoding="utf-8") as handle:
        return json.load(handle)


def sha256(path: str) -> str:
    with open(os.path.join(REPO, path), "rb") as handle:
        return hashlib.sha256(handle.read()).hexdigest()


def prose(text: str) -> str:
    return " ".join(str(text).split())


# ===========================================================================
# 1 — the window grew, from files
# ===========================================================================


class TestTheWindowGrew:

    def test_slice64_saw_two_bars_and_the_disk_has_not_gone_backwards(self):
        """Amended in slice 65 — by the very rule slice 64 wrote down.

        As written this asserted `appended_rows == 2` against LIVE DISK, which
        is the live-absolute mistake §47f named and then made anyway, in the
        same file, one paragraph after naming it. A third bar arrived and it
        went red.

        Slice 64's count lives in slice 64's frozen artefact. What is asserted
        against disk is the direction of travel and the prefix of dates, which
        cannot change without history being rewritten.
        """
        assert load(FRESHNESS)["after_t1_linear"] == 2          # dated
        assert load(FRESHNESS)["after_t1_dates"] == \
            ["2026-08-10", "2026-08-11"]                        # dated
        result = cp.check(cp.LINEAR_BTC)
        assert result.appended_rows >= 2
        assert [s[:10] for s in result.appended_timestamps][:2] == \
            ["2026-08-10", "2026-08-11"]

    def test_the_growth_is_measured_against_the_previous_artefact(self):
        delta = load(FRESHNESS)["delta_since_slice63"]
        assert delta["previous_after_t1_linear"] == 1
        assert delta["new_linear_bars_since_slice63"] == 1
        assert delta["the_window_grew"] is True
        assert delta["new_funding_prints_since_slice63"] == 3

    def test_the_forward_artefact_agrees(self):
        grew = load(FORWARD)["window_grew_since_slice63"]
        assert grew["comparable"] is True
        assert grew["closed_forward_bars_previously"] == 1
        assert grew["closed_forward_bars_now"] == 2
        assert grew["delta"] == 1
        assert grew["grew"] is True

    def test_it_is_still_an_append_and_history_is_untouched(self):
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
        assert grew == {cp.LINEAR_BTC, cp.FUNDING_BTC}, grew

    def test_no_bar_for_the_current_utc_day_is_present(self):
        """Amended in slice 65: the DATE was hard-coded, so the test expired.

        The claim is not "2026-08-12 is absent"; it is "a bar that has not
        closed is never on disk". Stated that way it survives every future
        slice, and it still fails loudly if an unclosed bar is ever shipped.
        """
        import datetime as dt  # noqa: PLC0415
        today = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
        rows = cp.read_rows(cp.LINEAR_BTC)
        assert not any(r["time_period_start"][:10] >= today for r in rows)
        assert all(entry["closed"]
                   for entry in load(FRESHNESS)["appended_linear_bars"])

    def test_the_note_again_quotes_the_compressed_digest(self):
        digests = load(FRESHNESS)["digests"]
        assert digests["note_quotes_the_compressed_digest"] is True
        assert digests["note_digest_matches_the_compressed_file"] is True
        assert digests["linear_identical_to_slice63_uncompressed"] is False


# ===========================================================================
# 2 — and the ceiling did not move
# ===========================================================================


class TestTheCeilingDidNotMove:

    def test_the_ceiling_is_still_zero_at_two_bars(self):
        ceiling = load(FORWARD)["ceiling"]
        assert ceiling["closed_forward_bars"] == 2
        assert ceiling["closed_forward_bars_needed_for_one_trade"] == \
            fb.HORIZON + 1 == 6
        assert ceiling["max_possible_forward_closed_trades"] == 0
        assert ceiling["observed_forward_closed_trades"] == 0
        assert ceiling["within_ceiling"] is True

    def test_the_ceiling_is_arithmetic_not_a_stored_zero(self):
        """It must fall out of the bar count and HORIZON, so that a sixth bar
        raises it on its own and a shortened horizon cannot raise it here."""
        ceiling = load(FORWARD)["ceiling"]
        assert ceiling["max_possible_forward_closed_trades"] == \
            max(0, ceiling["closed_forward_bars"] - fb.HORIZON)

    def test_growth_and_observation_are_independent_and_disagree(self):
        """The whole argument of the slice, asserted as a shape claim."""
        assert load(FRESHNESS)["delta_since_slice63"]["the_window_grew"] is True
        assert load(FORWARD)["is_forward_observation"] is False
        assert load(FORWARD)["forward_n_trades"] == 0
        assert load(FORWARD)["trades"] == []

    def test_the_labelling_rule_is_unchanged(self):
        rule = prose(load(FORWARD)["labelling_rule"])
        assert "forward_n_trades > 0" in rule
        assert "NOT extension_present" in rule

    def test_the_rule_stood_aside_on_both_bars(self):
        aside = load(FORWARD)["forward_decisions"]["why_the_rule_stood_aside"]
        assert len(aside) == 2
        assert all(entry["funding_setup_present"] is False for entry in aside)

    def test_the_first_bar_now_has_only_one_reason_to_stand_aside(self):
        """A sharper finding than slices 62-63 could make.

        There, 2026-08-10's zero was doubly determined: no funding setup AND it
        was the last bar of the corpus, which `directed_signal_bars` excludes
        on its own. A second bar has since arrived, so the structural exclusion
        no longer applies to it and the funding reason stands alone.
        """
        aside = {entry["bar_utc"][:10]: entry
                 for entry in load(FORWARD)["forward_decisions"][
                     "why_the_rule_stood_aside"]}
        assert aside["2026-08-10"]["is_last_bar_of_corpus"] is False
        assert aside["2026-08-10"]["funding_setup_present"] is False
        assert aside["2026-08-11"]["is_last_bar_of_corpus"] is True

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

    def test_the_scoring_was_not_re_tuned_since_slice63(self):
        """Same AST comparison slice 63 introduced, one slice on.

        Every function but `build` identical; `build` split at its `return`
        with the scoring required identical and every previously-emitted key
        required to map to the same expression.
        """
        import ast  # noqa: PLC0415

        substitutions = {
            "forward_shadow_observation/3": "forward_shadow_observation/4",
            "slice63_forward_shadow.json": "slice64_forward_shadow.json",
            "slice63_forward_shadow.log": "slice64_forward_shadow.log",
            "window_grew_since_slice62": "window_grew_since_slice63",
            "SLICE 63 — FORWARD SHADOW SEGMENT":
                "SLICE 64 — FORWARD SHADOW SEGMENT",
        }

        class Normalise(ast.NodeTransformer):
            def visit_Constant(self, node):          # noqa: N802
                if isinstance(node.value, str):
                    value = node.value
                    for before, after in substitutions.items():
                        value = value.replace(before, after)
                    return ast.copy_location(ast.Constant(value), node)
                if node.value == 63:
                    return ast.copy_location(ast.Constant(64), node)
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

        old_body, old_payload = split("slice63_forward_shadow.py")
        new_body, new_payload = split("slice64_forward_shadow.py")
        assert old_body == new_body, "the SCORING changed between slices"
        drifted = [key for key, value in old_payload.items()
                   if new_payload.get(key) != value]
        assert drifted == [], drifted


# ===========================================================================
# 3 — the manifest defect
# ===========================================================================


class TestTheManifestDefectIsRecorded:

    def test_the_freshness_artefact_audits_every_manifest_entry(self):
        audit = load(FRESHNESS)["manifests"]
        assert len(audit["entries"]) == 6
        assert audit["count_disagreeing"] == 4
        assert audit["not_edited_by_this_slice"] is True

    def test_the_measured_product_is_untainted(self):
        """Why this defect does not make the slice's claims unsafe."""
        audit = load(FRESHNESS)["manifests"]
        assert audit["measured_product_entries_are_accurate"] is True
        for path, entry in audit["entries"].items():
            if "BTC" in path:
                assert entry["manifest_describes_the_file"] is True

    def test_the_understating_entry_is_called_out(self):
        audit = load(FRESHNESS)["manifests"]
        understating = [p for p, e in audit["entries"].items()
                        if e["understates_the_corpus"]]
        assert understating == [
            "data/real_funding/funding/BINANCE_LINEAR_SOL_USDT_FUNDING.csv.gz"]
        assert "UNDERSTATES a corpus by 66 rows" in prose(audit["finding"])

    def test_the_manifest_was_not_edited_and_the_reason_is_recorded(self):
        payload = load(FRESHNESS)
        assert payload["manifests_edited_by_this_slice"] is False
        why = prose(payload["why_manifests_not_edited"])
        assert "cannot attest" in why
        assert "certifies nothing" in why

    def test_slice64s_counts_came_from_files_not_from_the_manifest(self):
        """The reason a false manifest cannot reach a slice's numbers: every
        count is read from the corpus.

        Amended in slice 65 to compare slice 64's artefact against slice 64's
        own recorded prefix figures rather than against today's disk, which has
        since moved. The equivalent live check for the CURRENT slice is in
        `tests/test_slice65_three_day_window.py`.
        """
        payload = load(FRESHNESS)
        linear = payload["prefix_invariant"]["checks"][cp.LINEAR_BTC]
        funding = payload["prefix_invariant"]["checks"][cp.FUNDING_BTC]
        assert payload["linear_rows"] == linear["rows_on_disk"] == 1463
        assert payload["funding_rows"] == funding["rows_on_disk"] == 4392
        # And the manifest it disagreed with is not the source of either.
        assert payload["manifests"]["count_disagreeing"] == 4


# ===========================================================================
# 4 — the restoration, and the amendments it declares
# ===========================================================================


class TestTheRestoration:

    def test_every_frozen_record_it_names_still_matches(self):
        """Amended in slice 65 — the same amendment slice 64 made to slice 63's
        file, one level up, which is the sign it should have been a RULE.

        A restoration manifest verifies what is FROZEN. As written this also
        re-verified the restored APPARATUS against live files, and slice 65
        amended two of them. A dated manifest cannot be the authority on files
        that are still being worked on; the CURRENT slice's manifest is,
        because that is where an amendment can be declared with its reason.

        Stated once, generally, so slice 66 does not have to rediscover it:
        **each slice's restoration manifest owns the live apparatus; every
        earlier one owns only its frozen records.**
        """
        record = load(RESTORED)
        files = record["dated_records"]["files"]
        assert files
        for relative, digest in files.items():
            assert os.path.exists(os.path.join(REPO, relative)), relative
            assert sha256(relative) == digest, relative

    def test_it_still_names_the_apparatus_it_restored(self):
        """The list itself is a dated claim and must not shrink."""
        record = load(RESTORED)
        assert record["apparatus"]["reimplemented"] is False
        assert "tools/corpus_prefix.py" in record["apparatus"]["files"]
        assert len(record["apparatus"]["files"]) == 13

    def test_no_dated_record_was_amended(self):
        record = load(RESTORED)
        amended = set(record["apparatus"].get("amended_in_slice64", {}))
        assert amended, "the manifest claims no amendment; three were made"
        assert not (amended & set(record["dated_records"]["files"]))

    def test_the_dated_records_were_not_regenerated_on_this_tree(self):
        """Checkable, not promised: each carries the commit of the tree it was
        written on, and that commit does not exist here."""
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
        assert checked >= 6, checked

    def test_the_prior_artefacts_now_disagree_with_disk(self):
        """Correct dated behaviour. If these ever agree, a record was edited."""
        assert load(PRIOR_FRESHNESS)["after_t1_linear"] == 1
        assert load(PRIOR_FORWARD)["forward_window"]["of_which_closed"] == 1
        assert cp.check(cp.LINEAR_BTC).appended_rows > \
            load(PRIOR_FRESHNESS)["after_t1_linear"]

    def test_the_pack_regression_is_recorded(self):
        record = load(RESTORED)
        assert record["restores_slices"] == [62, 63]
        evidence = record["evidence_the_pack_is_the_slice61_tree"]
        assert evidence["baseline_suite"]["slice64"] == \
            "12 failed, 4032 passed, 2 skipped"
        assert "manifest broadcast defect" in \
            prose(evidence["baseline_suite"]["note"])


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

    def test_eth_and_sol_were_not_measured_under_the_btc_name(self):
        """Especially not now, with a manifest claiming post-t1 data for them
        that does not exist."""
        assert fb.UNIVERSE == ("BTCUSDT",)
        assert load(FORWARD)["symbol"] == "BTCUSDT"
        for path, result in cp.check_all().items():
            if "BTC" not in path:
                assert result.appended_rows == 0

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

    def test_the_gate_refuses(self):
        assert pg.promotion_gate_allows_live() is False
        assert load(GATE)["promotion_gate_allows_live"] is False
        assert load(GATE)["items_complete"] == 2
        assert load(GATE)["items_total"] == 8
        assert load(GATE)["items_complete_unchanged_from_slice_63"] is True
        assert load(FORWARD)["promotion_gate_allows_live"] == \
            pg.promotion_gate_allows_live()

    def test_the_minimums_did_not_move(self):
        assert pg.MIN_FORWARD_TRADES == 20
        assert pg.MIN_FORWARD_DAYS == 180
        assert load(GATE)["minimums_moved_this_slice"] is False

    def test_the_gate_evidence_says_the_window_grew_and_the_count_did_not(self):
        evidence = prose(load(GATE)["checklist"]["forward_shadow_clean"][
            "evidence"])
        assert "THE WINDOW GREW THIS SLICE" in evidence
        assert "THE COUNT DID NOT MOVE" in evidence

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
        seam = load(FRESHNESS)["funding_seam"]
        assert seam["missing_prints"] == ["2026-08-09T16:00:00+00:00"]

    def test_no_fetch_was_attempted(self):
        assert load(FRESHNESS)["fetch_attempted"] is False
