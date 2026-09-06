"""Slice 65 — three bars, still no trade, and two defects that recurred.

WHAT THIS SLICE HAS TO SAY THAT SLICE 64 COULD NOT
==================================================
**Zero is now a sequence, and a sequence needs interpreting.** Three slices have
reported `forward_n_trades = 0` from windows of 1, 2 and 3 bars. That is *not*
three failed attempts. The ceiling — `max(0, N - HORIZON)` — has been zero in
every one, so the rule has never yet been given an opportunity to produce a
trade. Being able to say that in advance, and to distinguish it from "the rule
keeps failing", is the entire reason a ceiling is declared before each run.
`TestZeroIsNotThreeFailedAttempts` asserts that reading rather than leaving it
to prose.

**Both process defects recurred, and recurrence changes what they mean.** A
pack built from the wrong parent once is an accident; three times is a fixed
step in the pack-building process. A manifest with four wrong entries once is a
slip; the same four wrong again with NEW numbers, over files that did not
change, is a regeneration that broadcasts BTC's counts across all three symbols.
The guards were re-pinned, and — more usefully — taught to assert the SHAPE of
the defect so the next regeneration is caught before anyone compares numbers.

THE LESSON THIS SLICE HAD TO APPLY TO ITSELF
============================================
Four tests written in slice 64 pinned live absolutes and expired when a third
bar arrived — in the same file whose EDGE section had just named that exact
mistake. Recorded, not quietly fixed. The rule holds: **assert the dated claim
against the frozen artefact; assert only the direction of travel against live
disk.**
"""
from __future__ import annotations

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

FRESHNESS = "artifacts/slice65_data_freshness.json"
FORWARD = "artifacts/slice65_forward_shadow.json"
GATE = "artifacts/slice65_promotion_gate.json"
RESTORED = "artifacts/slice65_restored_from_slice64.json"

PRIOR = {
    62: ("artifacts/slice62_data_freshness.json",
         "artifacts/slice62_forward_shadow.json", 1),
    63: ("artifacts/slice63_data_freshness.json",
         "artifacts/slice63_forward_shadow.json", 1),
    64: ("artifacts/slice64_data_freshness.json",
         "artifacts/slice64_forward_shadow.json", 2),
}


def load(path: str) -> dict:
    with open(os.path.join(REPO, path), encoding="utf-8") as handle:
        return json.load(handle)


def sha256(path: str) -> str:
    with open(os.path.join(REPO, path), "rb") as handle:
        return hashlib.sha256(handle.read()).hexdigest()


def prose(text: str) -> str:
    return " ".join(str(text).split())


# ===========================================================================
# 1 — the window grew again, from files
# ===========================================================================


class TestTheWindowGrewAgain:

    def test_slice65_saw_three_bars_and_the_disk_has_not_gone_backwards(self):
        """Amended in slice 66 — the FIFTH consecutive slice to have to apply
        the dated-versus-live rule to the previous slice's tests.

        Slice 65 wrote the rule down and then pinned `appended_rows == 3`
        against live disk anyway. Recurring five times in a row means the rule
        is not enough on its own, so slice 66 adds a META-TEST that makes the
        mistake unshippable rather than correctable: see
        `tests/test_slice66_four_day_window.py::TestNoTestPinsALiveAbsolute`.
        """
        assert load(FRESHNESS)["after_t1_linear"] == 3            # dated
        assert load(FRESHNESS)["after_t1_dates"] == \
            ["2026-08-10", "2026-08-11", "2026-08-12"]            # dated
        assert load(FRESHNESS)["linear_rows"] == 1464             # dated
        result = cp.check(cp.LINEAR_BTC)
        assert result.appended_rows >= 3
        assert [s[:10] for s in result.appended_timestamps][:3] == \
            ["2026-08-10", "2026-08-11", "2026-08-12"]

    def test_the_growth_is_measured_against_the_previous_artefact(self):
        delta = load(FRESHNESS)["delta_since_slice64"]
        assert delta["previous_after_t1_linear"] == 2
        assert delta["new_linear_bars_since_slice64"] == 1
        assert delta["new_funding_prints_since_slice64"] == 2
        assert delta["the_window_grew"] is True

    def test_the_forward_artefact_agrees(self):
        grew = load(FORWARD)["window_grew_since_slice64"]
        assert grew["closed_forward_bars_previously"] == 2
        assert grew["closed_forward_bars_now"] == 3
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

    def test_only_btc_grew_and_eth_sol_are_untouched(self):
        for path, result in cp.check_all().items():
            expected = 0 if "BTC" not in path else result.appended_rows
            assert result.appended_rows == expected, path
        grew = {p for p, r in cp.check_all().items() if r.extended}
        assert grew == {cp.LINEAR_BTC, cp.FUNDING_BTC}

    def test_no_unclosed_bar_is_on_disk(self):
        """Computed from the clock, never a hard-coded date — the mistake this
        slice had to repair in four inherited tests."""
        today = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
        rows = cp.read_rows(cp.LINEAR_BTC)
        assert not any(r["time_period_start"][:10] >= today for r in rows)
        assert load(FRESHNESS)["open_bar_absent_as_expected"] is True

    def test_the_note_again_quotes_the_compressed_digest(self):
        digests = load(FRESHNESS)["digests"]
        assert digests["note_quotes_the_compressed_digest"] is True
        assert digests["note_digest_matches_the_compressed_file"] is True
        assert digests["linear_sha256_compressed"] != \
            digests["linear_sha256_uncompressed"]

    def test_the_growth_claim_matches_the_files(self):
        assert load(FRESHNESS)["claim_vs_files_discrepancy"] is False
        assert load(FRESHNESS)["human_note_is_evidence"] is False
        assert all(load(FRESHNESS)["human_note_claims_checked"].values())


# ===========================================================================
# 2 — zero is not three failed attempts
# ===========================================================================


class TestZeroIsNotThreeFailedAttempts:

    def test_the_ceiling_is_still_zero_at_three_bars(self):
        ceiling = load(FORWARD)["ceiling"]
        assert ceiling["closed_forward_bars"] == 3
        assert ceiling["closed_forward_bars_needed_for_one_trade"] == \
            fb.HORIZON + 1 == 6
        assert ceiling["max_possible_forward_closed_trades"] == 0
        assert ceiling["observed_forward_closed_trades"] == 0
        assert ceiling["within_ceiling"] is True

    def test_the_ceiling_is_arithmetic_not_a_stored_zero(self):
        ceiling = load(FORWARD)["ceiling"]
        assert ceiling["max_possible_forward_closed_trades"] == \
            max(0, ceiling["closed_forward_bars"] - fb.HORIZON)

    def test_every_prior_slice_also_ran_against_a_zero_ceiling(self):
        """The load-bearing one. Three zeros, and none of them was an attempt.

        If any prior slice had run with a POSITIVE ceiling and still returned
        no trade, the sequence would mean something quite different — the rule
        would have had an opportunity and declined it. None did.
        """
        for slice_number, (_fresh, forward, bars) in sorted(PRIOR.items()):
            payload = load(forward)
            assert payload["forward_n_trades"] == 0, slice_number
            assert payload["ceiling"]["closed_forward_bars"] == bars
            assert payload["ceiling"][
                "max_possible_forward_closed_trades"] == 0, slice_number
            assert max(0, bars - fb.HORIZON) == 0, slice_number

    def test_the_window_sizes_form_the_expected_sequence(self):
        sizes = [load(forward)["ceiling"]["closed_forward_bars"]
                 for _s, (_f, forward, _b) in sorted(PRIOR.items())]
        assert sizes == [1, 1, 2]
        assert load(FORWARD)["ceiling"]["closed_forward_bars"] == 3

    def test_the_reading_is_stated_in_the_artefact_not_left_to_prose(self):
        note = prose(load(FORWARD)["window_grew_since_slice64"]["note"])
        assert "NOT three failed attempts" in note
        assert "SIX closed forward bars" in note

    def test_three_more_closed_bars_are_needed(self):
        ceiling = load(FORWARD)["ceiling"]
        shortfall = (ceiling["closed_forward_bars_needed_for_one_trade"]
                     - ceiling["closed_forward_bars"])
        assert shortfall == 3

    def test_growth_and_observation_are_independent_and_disagree(self):
        assert load(FRESHNESS)["delta_since_slice64"]["the_window_grew"] is True
        assert load(FORWARD)["is_forward_observation"] is False
        assert load(FORWARD)["forward_n_trades"] == 0
        assert load(FORWARD)["forward_observations_to_date"] == 0
        assert load(FORWARD)["trades"] == []

    def test_the_rule_stood_aside_on_all_three_bars(self):
        aside = load(FORWARD)["forward_decisions"]["why_the_rule_stood_aside"]
        assert len(aside) == 3
        assert all(entry["funding_setup_present"] is False for entry in aside)
        last_bar = [e for e in aside if e["is_last_bar_of_corpus"]]
        assert len(last_bar) == 1
        assert last_bar[0]["bar_utc"].startswith("2026-08-12")

    def test_two_of_the_three_have_only_the_funding_reason(self):
        """The structural last-bar exclusion covers exactly one bar, so for the
        other two the funding threshold is the sole explanation."""
        aside = load(FORWARD)["forward_decisions"]["why_the_rule_stood_aside"]
        sole = [e for e in aside if not e["is_last_bar_of_corpus"]]
        assert len(sole) == 2
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

    def test_the_scoring_was_not_re_tuned_since_slice64(self):
        """Same AST comparison, one slice on. `build` must be identical apart
        from added keys; every other function identical outright."""
        import ast  # noqa: PLC0415

        substitutions = {
            "forward_shadow_observation/4": "forward_shadow_observation/5",
            "slice64_forward_shadow.json": "slice65_forward_shadow.json",
            "slice64_forward_shadow.log": "slice65_forward_shadow.log",
            "window_grew_since_slice63": "window_grew_since_slice64",
            "slice63_forward_shadow.json": "slice64_forward_shadow.json",
            "SLICE 64 — FORWARD SHADOW SEGMENT":
                "SLICE 65 — FORWARD SHADOW SEGMENT",
        }

        class Normalise(ast.NodeTransformer):
            def visit_Constant(self, node):          # noqa: N802
                if isinstance(node.value, str):
                    value = node.value
                    for before, after in substitutions.items():
                        value = value.replace(before, after)
                    return ast.copy_location(ast.Constant(value), node)
                if node.value == 64:
                    return ast.copy_location(ast.Constant(65), node)
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

        old_body, old_payload = split("slice64_forward_shadow.py")
        new_body, new_payload = split("slice65_forward_shadow.py")
        assert old_body == new_body, "the SCORING changed between slices"
        drifted = [key for key, value in old_payload.items()
                   if new_payload.get(key) != value
                   and "note" not in key.lower()]
        assert drifted == [], drifted


# ===========================================================================
# 3 — the defects recurred
# ===========================================================================


class TestBothDefectsRecurred:

    def test_the_pack_regression_is_the_third(self):
        record = load(RESTORED)
        evidence = record["evidence_the_pack_is_the_slice61_tree"]
        assert evidence["occurrences"] == 3
        assert record["restores_slices"] == [62, 63, 64]
        assert evidence["baseline_suite"]["identical_to_slice64"] is True
        assert evidence["baseline_suite"]["slice65"] == \
            "12 failed, 4032 passed, 2 skipped"

    def test_the_manifest_defect_recurred_rather_than_being_repaired(self):
        audit = load(FRESHNESS)["manifests"]
        assert audit["count_disagreeing"] == 4
        assert audit["measured_product_entries_are_accurate"] is True
        assert audit["not_edited_by_this_slice"] is True

    def test_the_understating_entry_persists(self):
        audit = load(FRESHNESS)["manifests"]
        understating = [p for p, e in audit["entries"].items()
                        if e["understates_the_corpus"]]
        assert understating == [
            "data/real_funding/funding/BINANCE_LINEAR_SOL_USDT_FUNDING.csv.gz"]

    def test_slice65s_counts_came_from_files_not_the_manifest(self):
        """Amended in slice 66: compared against slice 65's OWN frozen prefix
        figures rather than today's disk, which has since moved on.

        The live equivalent belongs to whichever slice is current.
        """
        payload = load(FRESHNESS)
        linear = payload["prefix_invariant"]["checks"][cp.LINEAR_BTC]
        funding = payload["prefix_invariant"]["checks"][cp.FUNDING_BTC]
        assert payload["linear_rows"] == linear["rows_on_disk"] == 1464
        assert payload["funding_rows"] == funding["rows_on_disk"] == 4394
        assert payload["manifests"]["count_disagreeing"] == 4

    def test_the_manifests_were_not_edited(self):
        assert load(FRESHNESS)["manifests_edited_by_this_slice"] is False
        why = prose(load(FRESHNESS)["why_manifests_not_edited"])
        assert "cannot attest" in why


# ===========================================================================
# 4 — the restoration and its declared amendments
# ===========================================================================


class TestTheRestoration:

    def test_every_frozen_record_it_names_still_matches(self):
        """Amended in slice 66, under the rule slice 65 itself wrote down:

        > each slice's restoration manifest owns the live apparatus; every
        > earlier one owns only its frozen records.

        Slice 65 stated that rule and then left its own restoration test
        checking live apparatus, which slice 66 legitimately amended. The rule
        is now applied here, and slice 66's manifest owns the live check.
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
        assert len(record["apparatus"]["files"]) == 17

    def test_no_dated_record_was_amended(self):
        record = load(RESTORED)
        amended = set(record["apparatus"].get("amended_in_slice65", {}))
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
        assert checked >= 9, checked

    def test_the_prior_artefacts_still_record_their_own_window_sizes(self):
        for slice_number, (fresh, _forward, bars) in sorted(PRIOR.items()):
            assert load(fresh)["after_t1_linear"] == bars, slice_number
        assert cp.check(cp.LINEAR_BTC).appended_rows >= \
            load(FRESHNESS)["after_t1_linear"]


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

    def test_the_gate_evidence_names_the_sequence(self):
        evidence = prose(load(GATE)["checklist"]["forward_shadow_clean"][
            "evidence"])
        assert "THE WINDOW GREW AGAIN" in evidence
        assert "THIS IS NOT THREE FAILED ATTEMPTS" in evidence

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
