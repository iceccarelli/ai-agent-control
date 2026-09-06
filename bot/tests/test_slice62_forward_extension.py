"""Slice 62 — a real extension arrived, and it still is not an observation.

WHAT THIS SLICE HAD TO GET RIGHT
================================
Six slices waited for bars after `t1`. One arrived. The two failure modes on
either side of that event are symmetric and this file guards both:

* **understating it** — refusing to admit the extension, or quietly breaking
  when the corpus grew. Eight tests DID break, none of them on a defect, and
  §45c argues at length why the repair had to be a strengthening rather than a
  number moved from 1461 to 1462;
* **overstating it** — announcing `is_forward_observation: true` because data
  finally arrived. A bar is not a trade. The gate counts closed forward trades
  and it counts zero, and that zero is now BETTER EVIDENCED than the zeros of
  slices 59-61 rather than smaller.

THE LOAD-BEARING TESTS IN THIS FILE
===================================
Not the ones that read the artefacts — those only check that a number was
copied faithfully. The load-bearing ones are in
`TestTheNewInvariantActuallyCatchesThings`, which build corrupted corpora in a
temporary directory and prove the prefix check goes red for a rewrite, for a
truncation and for a back-fill. A guard that has never been observed to fail is
not known to be a guard.
"""
from __future__ import annotations

import ast
import csv
import gzip
import io
import json
import os
import shutil
import sys
import tempfile

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "tools"))

import corpus_prefix as cp  # noqa: E402
import project_status as ps  # noqa: E402
import promotion_gate as pg  # noqa: E402
import shadow  # noqa: E402
from signals import funding_carry_fade_btc_v1 as fb  # noqa: E402

FRESHNESS = "artifacts/slice62_data_freshness.json"
FORWARD = "artifacts/slice62_forward_shadow.json"
GATE = "artifacts/slice62_promotion_gate.json"
OOS = "artifacts/slice57_oos_edge_BTCUSDT_summary.json"


def load(path: str) -> dict:
    with open(os.path.join(REPO, path), encoding="utf-8") as handle:
        return json.load(handle)


def prose(text: str) -> str:
    """Collapse whitespace so a line-wrapped phrase still matches."""
    return " ".join(str(text).split())


# ===========================================================================
# 1 — the extension is real, and it is an append
# ===========================================================================


class TestTheExtensionIsRealAndIsAnAppend:

    def test_every_corpus_file_is_append_only(self):
        for path, result in cp.check_all().items():
            assert result.append_only, f"{path}: {result.why_not()}"

    def test_the_btc_corpora_are_the_only_ones_that_grew(self):
        grew = {p for p, r in cp.check_all().items() if r.extended}
        assert grew == {cp.LINEAR_BTC, cp.FUNDING_BTC}, grew

    def test_the_measured_prefix_still_hashes_to_the_slice57_pins(self):
        """The invariant. Everything else in this slice rests on it."""
        folds = fb.load_folds()
        assert cp.check(cp.LINEAR_BTC).prefix_sha256 == \
            folds["sources"]["linear_bars"]["sha256_uncompressed"]
        assert cp.check(cp.FUNDING_BTC).prefix_sha256 == \
            folds["sources"]["funding"]["sha256_uncompressed"]

    def test_the_whole_file_digests_now_differ_which_is_expected(self):
        """Stated explicitly so nobody 'fixes' it back.

        If these ever became equal again the corpus would have shrunk back to
        its measured length, which is a defect, not a restoration.
        """
        folds = fb.load_folds()
        assert cp.check(cp.LINEAR_BTC).whole_file_sha256 != \
            folds["sources"]["linear_bars"]["sha256_uncompressed"]

    def test_the_first_bar_after_t1_was_and_remains_2026_08_10(self):
        """Amended in slice 64, when a SECOND bar arrived.

        As written this asserted `appended_rows == 1` — a live absolute, true
        on the day it was written and false the moment the window grew, which
        is the event the programme exists to wait for. The fourth time this
        lesson has had to be applied (slices 54, 57, 62, 63).

        What is invariant is that the FIRST post-`t1` bar is 2026-08-10 and
        that the corpus only ever grows. What slice 62 saw is preserved in its
        own dated artefact and asserted below against that artefact, not
        against today's disk.
        """
        result = cp.check(cp.LINEAR_BTC)
        assert result.appended_rows >= 1
        assert result.appended_timestamps[0].startswith("2026-08-10")
        assert load(FRESHNESS)["after_t1_linear"] == 1        # dated: slice 62
        assert result.appended_rows >= load(FRESHNESS)["after_t1_linear"]

    def test_every_appended_row_is_strictly_after_t1(self):
        for path, result in cp.check_all().items():
            assert result.appended_all_strictly_after_t1, (
                path, result.appended_before_or_at_t1)

    def test_the_open_bar_for_the_current_day_is_absent(self):
        """It is still being printed. Requiring it would be requiring a bar to
        exist before it closes, which is how an unfinished bar becomes an
        observation."""
        bars = [entry for entry in load(FRESHNESS)["appended_linear_bars"]]
        assert all(entry["closed"] for entry in bars)
        assert load(FRESHNESS)["open_bar_absent_as_expected"] is True

    def test_two_independent_records_still_pin_the_same_digests(self):
        assert all(cp.pinned_digests_agree().values())


# ===========================================================================
# 2 — THE LOAD-BEARING ONES: the new invariant is observed to fail
# ===========================================================================


class TestTheNewInvariantActuallyCatchesThings:
    """A guard never observed to fail is not known to be a guard.

    Each test below builds a corrupted copy of the real corpus in a temporary
    tree, points the checker at it, and requires the check to go red — and to
    go red for the RIGHT REASON, named in `why_not()`. Slice 57 established
    that lifting a guard has to change the answer; these are the same argument
    run in the opposite direction.
    """

    @staticmethod
    def _corrupt(mutate):
        """Write a mutated corpus into a temp repo and check it there."""
        original = cp.REPO
        temp = tempfile.mkdtemp(prefix="slice62_")
        try:
            for relative in (cp.LINEAR_BTC, cp.PINS_ARTEFACT,
                             cp.FOLDS_ARTEFACT):
                destination = os.path.join(temp, relative)
                os.makedirs(os.path.dirname(destination), exist_ok=True)
                shutil.copy(os.path.join(original, relative), destination)

            path = os.path.join(temp, cp.LINEAR_BTC)
            with gzip.open(path, "rt", encoding="utf-8", newline="") as handle:
                lines = handle.read().splitlines(keepends=True)
            with gzip.open(path, "wt", encoding="utf-8", newline="") as handle:
                handle.write("".join(mutate(lines)))

            cp.REPO = temp
            return cp.check(cp.LINEAR_BTC)
        finally:
            cp.REPO = original
            shutil.rmtree(temp, ignore_errors=True)

    def test_the_unmutated_control_passes(self):
        """Without this, every test below could be passing for the wrong
        reason — a broken harness rather than a working guard."""
        result = self._corrupt(lambda lines: lines)
        assert result.append_only, result.why_not()
        assert result.history_unchanged is True

    def test_a_rewritten_history_row_is_caught(self):
        def mutate(lines):
            out = list(lines)
            out[500] = out[500].replace(",", ",", 1)  # same bytes...
            fields = out[500].split(",")
            fields[4] = "1.0"                          # ...then a real edit
            out[500] = ",".join(fields)
            return out

        result = self._corrupt(mutate)
        assert result.history_unchanged is False
        assert result.append_only is False
        assert "REWRITTEN" in result.why_not()

    def test_a_truncated_corpus_is_caught(self):
        result = self._corrupt(lambda lines: lines[:-50])
        assert result.never_shrank is False
        assert result.append_only is False
        assert prose("may never shrink") in prose(result.why_not())

    def test_a_backfilled_row_stamped_before_t1_is_caught(self):
        """The case a whole-file digest could never see.

        A row appended at the END of the file but stamped INSIDE the measured
        window contaminates that window without altering one existing byte.
        """
        def mutate(lines):
            appended = lines[-1].split(",")
            appended[0] = "2025-01-01T00:00:00+00:00"
            appended[1] = "2025-01-01T23:59:59+00:00"
            return lines + [",".join(appended)]

        result = self._corrupt(mutate)
        assert result.history_unchanged is True     # history really is intact
        assert result.appended_all_strictly_after_t1 is False
        assert result.append_only is False
        assert "back-fill" in result.why_not()

    def test_a_row_stamped_exactly_at_t1_is_also_caught(self):
        """`t1` itself was measured. Strictly after means strictly."""
        def mutate(lines):
            appended = lines[-1].split(",")
            appended[0] = "2026-08-09T00:00:00+00:00"
            appended[1] = "2026-08-09T23:59:59+00:00"
            return lines + [",".join(appended)]

        assert self._corrupt(mutate).appended_all_strictly_after_t1 is False

    def test_a_legitimate_further_extension_still_passes(self):
        """The guard must not simply refuse everything.

        A second genuine post-`t1` bar is exactly what the programme is waiting
        for, and it has to pass.
        """
        def mutate(lines):
            appended = lines[-1].split(",")
            appended[0] = "2026-08-11T00:00:00+00:00"
            appended[1] = "2026-08-11T23:59:59+00:00"
            return lines + [",".join(appended)]

        before = cp.check(cp.LINEAR_BTC).appended_rows
        result = self._corrupt(mutate)
        assert result.append_only, result.why_not()
        # Relative, not absolute: the point is that ONE MORE legitimate bar is
        # accepted, whatever the window's size happens to be today.
        assert result.appended_rows == before + 1


# ===========================================================================
# 3 — a bar is not an observation
# ===========================================================================


class TestABarIsNotAnObservation:

    def test_extension_present_is_true(self):
        assert load(FRESHNESS)["extension_present"] is True
        assert load(FORWARD)["extension_present"] is True
        assert load(FRESHNESS)["after_t1_linear"] == 1

    def test_is_forward_observation_is_false(self):
        assert load(FORWARD)["is_forward_observation"] is False
        assert load(FORWARD)["forward_n_trades"] == 0
        assert load(FORWARD)["forward_observations_to_date"] == 0

    def test_the_two_are_reported_separately(self):
        """The whole argument of the slice, asserted as a shape claim."""
        payload = load(FORWARD)
        assert payload["extension_present"] != payload[
            "is_forward_observation"]

    def test_the_labelling_rule_is_stated_in_the_artefact(self):
        rule = prose(load(FORWARD)["labelling_rule"])
        assert "forward_n_trades > 0" in rule
        assert "NOT extension_present" in rule
        assert "A BAR ARRIVING IS NOT AN OBSERVATION" in rule

    def test_the_label_is_derived_from_the_trade_count_not_asserted(self):
        """Read the source, not the output: `is_forward_observation` must be
        computed from `forward_n_trades`, never written as a literal."""
        source = os.path.join(REPO, "tools", "slice62_forward_shadow.py")
        with open(source, encoding="utf-8") as handle:
            tree = ast.parse(handle.read())
        found = []
        for node in ast.walk(tree):
            if (isinstance(node, ast.Constant)
                    and node.value == "is_forward_observation"):
                found.append(node)
        assert found, "the key is not written at all"
        # The value beside it must be a comparison, not a constant.
        for node in ast.walk(tree):
            if isinstance(node, ast.Dict):
                for key, value in zip(node.keys, node.values):
                    if (isinstance(key, ast.Constant)
                            and key.value == "is_forward_observation"):
                        assert isinstance(value, ast.Compare), (
                            "is_forward_observation was written as a literal")

    def test_the_ceiling_was_declared_before_the_run_and_held(self):
        ceiling = load(FORWARD)["ceiling"]
        assert ceiling["max_possible_forward_closed_trades"] == 0
        assert ceiling["observed_forward_closed_trades"] == 0
        assert ceiling["within_ceiling"] is True
        assert "§45e" in ceiling["declared_in"]
        assert ceiling["closed_forward_bars_needed_for_one_trade"] == \
            fb.HORIZON + 1

    def test_the_ceiling_is_arithmetic_and_not_a_stored_number(self):
        """It must fall out of HORIZON and the bar count, so that a longer
        window raises it on its own and a shortened horizon cannot silently
        raise it here."""
        payload = load(FORWARD)
        bars = payload["ceiling"]["closed_forward_bars"]
        assert payload["ceiling"]["max_possible_forward_closed_trades"] == \
            max(0, bars - fb.HORIZON)

    def test_no_bars_were_invented(self):
        assert load(FRESHNESS)["bars_fabricated"] == 0
        assert load(FRESHNESS)["corpus_appended_to_by_this_tool"] is False
        assert load(FORWARD)["forward_window"]["invented_future_bars"] == 0

    def test_the_bar_array_was_never_sliced(self):
        window = load(FORWARD)["forward_window"]
        assert window["bar_array_sliced"] is False
        assert "invented out of nothing" in prose(window["why_not_sliced"])

    def test_no_exit_was_clamped_to_the_end_of_the_corpus(self):
        decisions = load(FORWARD)["forward_decisions"]
        assert decisions["exit_clamping_to_corpus_end"] is False
        assert "has not closed" in prose(decisions["why_no_clamping"])

    def test_the_rule_stood_aside_and_the_reason_is_recorded(self):
        aside = load(FORWARD)["forward_decisions"]["why_the_rule_stood_aside"]
        assert len(aside) == 1
        assert aside[0]["funding_setup_present"] is False
        assert aside[0]["is_last_bar_of_corpus"] is True


# ===========================================================================
# 4 — the forward window is strict at t1
# ===========================================================================


class TestTheForwardWindowIsStrictAtT1:

    class _Bar:
        def __init__(self, start_ms):
            self.start_ms = start_ms

    def test_a_bar_stamped_exactly_at_t1_is_not_forward(self):
        folds = fb.load_folds()
        assert fb.in_forward_window(
            self._Bar(int(folds["t1_ms"])), folds) is False

    def test_a_bar_one_millisecond_later_is_forward(self):
        folds = fb.load_folds()
        assert fb.in_forward_window(
            self._Bar(int(folds["t1_ms"]) + 1), folds) is True

    def test_the_late_window_is_inclusive_at_its_own_open(self):
        """The asymmetry is deliberate and is worth pinning: `t_mid` opens the
        registering window and belongs to it; `t1` closes it and was measured.
        """
        folds = fb.load_folds()
        assert fb.in_late_window(self._Bar(int(folds["t_mid_ms"])), folds) \
            is True

    def test_restrict_flags_to_forward_zeroes_and_never_slices(self):
        import numpy as np
        folds = fb.load_folds()
        bars = [self._Bar(int(folds["t1_ms"]) - 86_400_000),
                self._Bar(int(folds["t1_ms"])),
                self._Bar(int(folds["t1_ms"]) + 86_400_000)]
        flags = np.array([True, True, True])
        out = fb.restrict_flags_to_forward(flags, bars, folds)
        assert len(out) == len(flags), "the array was resized"
        assert list(out) == [False, False, True]

    def test_it_does_not_mutate_its_input(self):
        import numpy as np
        folds = fb.load_folds()
        bars = [self._Bar(int(folds["t1_ms"]) - 1)]
        flags = np.array([True])
        fb.restrict_flags_to_forward(flags, bars, folds)
        assert bool(flags[0]) is True


# ===========================================================================
# 5 — nothing that was frozen moved
# ===========================================================================


class TestNothingThatWasFrozenMoved:

    def test_the_constants_fingerprint_is_unchanged(self):
        assert shadow.constants_fingerprint()[:32] == \
            "662de0115880871352d5d623b1020eaa"
        assert shadow.CONSTANTS_FINGERPRINT == \
            "662de0115880871352d5d623b1020eaa"

    def test_the_new_forward_helpers_are_purely_additive(self):
        """They exist, and nothing that produced a measured number calls them.

        If a scoring path ever did, a percentile could move because a helper
        was added — which is exactly the kind of change this programme forbids
        making silently.

        Amended in slice 63. As written it excluded files named `slice62_*`,
        which is a SLICE NUMBER standing in for a ROLE — so slice 63's own
        forward-shadow tool, whose entire job is to call this helper, failed
        the test the moment it existed. Excluding `slice63_*` too would have
        been the same mistake one slice later.

        The rule now names the role: the helper may be called by the module
        that defines it, and by a slice's forward-shadow tool. Anything else —
        a backtester, a measurement tool, a strategy — is a scoring path and
        still fails, which is what this test was ever for.
        """
        import re  # noqa: PLC0415

        assert hasattr(fb, "restrict_flags_to_forward")
        definer = os.path.join("signals", "funding_carry_fade_btc_v1.py")
        permitted = re.compile(r"^slice\d+_forward_shadow\.py$")

        callers = []
        for directory in ("", "signals", "tools"):
            base = os.path.join(REPO, directory) if directory else REPO
            for name in sorted(os.listdir(base)):
                if not name.endswith(".py"):
                    continue
                with open(os.path.join(base, name), encoding="utf-8") as fh:
                    if "restrict_flags_to_forward" in fh.read():
                        callers.append(os.path.join(directory, name))

        assert definer in callers, "the helper vanished from its own module"
        unexpected = [c for c in callers
                      if c != definer
                      and not (c.startswith("tools" + os.sep)
                               and permitted.match(os.path.basename(c)))]
        assert unexpected == [], unexpected

        # And the named scoring paths must never appear, whatever they are
        # called. Stated positively so the check cannot pass by an empty sweep.
        scoring = ("backtest.py", "edge_measurement.py", "skill_test.py",
                   "shadow_strategy.py", "ml_strategy.py", "trading_engine.py")
        assert any(os.path.basename(c) in ("funding_carry_fade_btc_v1.py",)
                   for c in callers)
        assert not [c for c in callers if os.path.basename(c) in scoring]

    def test_the_caps_are_unchanged(self):
        assert shadow.SHADOW_MAX_CONCURRENT_POSITIONS == 1
        assert shadow.SHADOW_MAX_ENTRIES_PER_DAY == 1
        assert shadow.SHADOW_MAX_NOTIONAL_USD == 100.00

    def test_the_monitor_thresholds_are_unchanged(self):
        thresholds = shadow.MONITOR_THRESHOLDS
        assert thresholds["M1_rolling_trades"]["warn_below"] == 0.0
        assert thresholds["M1_rolling_trades"]["alert_below"] == -0.25
        assert thresholds["M2_rolling_days"]["window_days"] == 90
        assert thresholds["M3_concentration"]["warn_above"] == 0.60
        assert thresholds["M4_halves"]["min_trades"] == 20
        assert load(FORWARD)["thresholds_moved_this_slice"] is False
        assert load(FORWARD)["caps_changed_this_slice"] is False

    def test_the_eleven_families_are_still_frozen(self):
        assert len(ps.ABSENT_SIGNALS) == 11
        assert "funding_carry_fade_v1" in ps.ABSENT_SIGNALS

    def test_the_registering_oos_artefact_is_byte_identical(self):
        import hashlib
        with open(os.path.join(REPO, OOS), "rb") as handle:
            digest = hashlib.sha256(handle.read()).hexdigest()
        assert digest == \
            "28b7dfe0f22c4867661bc73f6434c4acce54a0483c4469a6540155b1b3c18c51"

    def test_the_cleared_edge_was_not_re_scored(self):
        assert load(FORWARD)["cleared_edge_re_scored_this_slice"] is False
        assert load(FORWARD)["is_stage1_evidence"] is False
        assert load(FORWARD)["registration_eligible"] is False

    def test_the_fold_calendar_is_unmodified(self):
        assert fb.folds_are_unmodified() is True

    def test_the_m4_warning_was_not_erased(self):
        """It is a fact about the HISTORICAL segment, and it stands."""
        historical = load("artifacts/slice59_forward_shadow.json")
        m4 = next(r for r in historical["monitor_readings"]
                  if r["name"] == "M4_halves")
        assert m4["state"] == "WARN"
        scope = prose(load(FORWARD)["monitor_scope_note"])
        assert "deliberately NOT recomputed" in scope
        assert "under the threshold AS IT STANDS" in scope
        # And the forward monitors report insufficient data rather than
        # inheriting the historical state, which would make a WARN look like a
        # forward reading.
        states = {r["name"]: r["state"]
                  for r in load(FORWARD)["forward_monitor_readings"]}
        assert states["M4_halves"] == "INSUFFICIENT_DATA"
        assert load(FORWARD)["forward_monitor_status"] == "INSUFFICIENT_DATA"


# ===========================================================================
# 6 — the gate still refuses
# ===========================================================================


class TestTheGateStillRefuses:

    def test_promotion_gate_allows_live_is_false(self):
        assert pg.promotion_gate_allows_live() is False
        assert load(GATE)["promotion_gate_allows_live"] is False
        assert load(GATE)["evaluated"]["allows_live"] is False

    def test_two_of_eight_items_complete_unchanged(self):
        assert load(GATE)["items_complete"] == 2
        assert load(GATE)["items_total"] == 8
        assert load(GATE)["items_complete_unchanged_from_slice_61"] is True

    def test_the_minimums_did_not_move(self):
        assert pg.MIN_FORWARD_TRADES == 20
        assert pg.MIN_FORWARD_DAYS == 180
        assert load(GATE)["minimums_moved_this_slice"] is False

    def test_the_forward_item_still_reads_zero_of_twenty(self):
        item = load(GATE)["checklist"]["forward_shadow_clean"]
        assert item["complete"] is False
        assert item["forward_trades_to_date"] == 0
        assert item["min_forward_trades"] == 20
        assert item["extension_present_this_slice"] is True
        assert item["closed_forward_bars"] == 1

    def test_the_forward_item_explains_why_a_bar_is_not_a_trade(self):
        why = prose(load(GATE)["checklist"]["forward_shadow_clean"][
            "why_bars_are_not_trades"])
        assert "only when it CLOSES" in why

    def test_no_human_item_was_completed_in_code(self):
        assert load(GATE)["human_items_completed_in_code"] == 0
        assert load(GATE)["signatures_present"] is False
        assert load(GATE)["signatures_forged"] is False
        human = [item for item in load(GATE)["evaluated"]["items"]
                 if item["owner"].startswith("human")]
        assert human and not any(item["complete"] for item in human)

    def test_the_gate_path_was_not_repointed(self):
        assert load(GATE)["evaluated"]["gate_path_repointed_this_slice"] is False
        assert pg.GATE_PATH.endswith("slice59_promotion_gate.json")

    def test_the_gate_tool_refuses_to_record_a_promotion(self):
        """The tool raises rather than writing if the verdict is not REFUSE."""
        source = os.path.join(REPO, "tools", "slice62_promotion_gate.py")
        with open(source, encoding="utf-8") as handle:
            tree = ast.parse(handle.read())
        raises = [n for n in ast.walk(tree)
                  if isinstance(n, ast.Raise)]
        assert raises, "the tool has no refusal path"

    def test_the_shadow_mean_is_not_treated_as_stage1_evidence(self):
        assert load(GATE)["forward_evidence_this_slice"][
            "shadow_mean_treated_as_stage1_evidence"] is False
        assert load(GATE)["forward_evidence_this_slice"][
            "registration_eligible"] is False


# ===========================================================================
# 7 — the standing posture
# ===========================================================================


class TestTheStandingPosture:

    def test_live_is_dark(self):
        status = ps.current()
        assert bool(status.live_authorized) is False
        assert bool(status.models_current_present) is False

    def test_not_closer_to_autonomy(self):
        for path in (FORWARD, GATE):
            assert load(path)["closer_to_autonomous_profit_agent"] is False
        assert "pilot progress, not autonomy" in prose(
            load(FORWARD)["why_not_closer"])

    def test_the_cleared_edge_signal_is_unchanged(self):
        assert ps.current().cleared_edge_signal == "funding_carry_fade_btc_v1"

    def test_the_manifests_were_left_verbatim(self):
        manifests = load(FRESHNESS)["manifests"]
        assert manifests["edited_by_this_slice"] is False
        assert manifests["linear"]["manifest_describes_the_pinned_prefix"] \
            is True
        assert manifests["linear"]["manifest_describes_the_file_beside_it"] \
            is False

    def test_the_human_note_was_verified_rather_than_believed(self):
        payload = load(FRESHNESS)
        assert payload["human_note_is_evidence"] is False
        assert all(payload["human_note_claims_checked"].values())
        assert "INDEPENDENTLY TRUE" in payload["human_note_verdict"]

    def test_the_funding_hole_is_named_not_absorbed(self):
        seam = load(FRESHNESS)["funding_seam"]
        assert seam["missing_prints"] == ["2026-08-09T16:00:00+00:00"]
        assert seam["affects_any_number_this_slice"] is False
        assert seam["carried_forward_for_the_human_to_fill"] is True
        assert seam["cadence_hours_across_pinned_prefix"] == \
            {"min": 8.0, "max": 8.0}

    def test_no_fetch_was_attempted_and_the_reason_is_recorded(self):
        assert load(FRESHNESS)["fetch_attempted"] is False
        assert "arrived on disk" in prose(load(FRESHNESS)["why_no_fetch"])
