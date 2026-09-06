"""Slice 69 — seven bars, ceiling 2, zero setups, and a stale constant killed.

WHAT THIS SLICE FIXES
=====================
A human found a defect of mine: `human_note_path` read `…SLICE64.md` in the
slice-68 artefact. It was **four slices wide** (65, 66, 67, 68), it was caused by
an upper-case constant that the lower-case substitution pipeline never matched,
and it was worse than cosmetic — `human_note_present` was computed from that
path, every prior note still exists in the tree, so the check returned `True` by
verifying a file from four slices earlier. **A presence check that could not
fail.**

No scored claim was tainted: note claims are compared against values computed
from the corpus, never parsed out of the note. That is the whole blast radius
and `TestTheStaleNotePathIsDeadAndStaysDead` states it rather than minimising it.

Two fixes, because correcting the string would not stop the next one:

1. the path is **derived** from the slice number, removing the class;
2. `TestNoToolCarriesAnotherSlicesConstant` joins slice 66's AST family and
   fails any `sliceNN_*` tool carrying an unannotated string literal that names
   a different slice.

Slice 66's guard would not have caught this — a stale path is not an equality
against an integer literal. **A guard family grows one member per lesson.**

WHAT THIS SLICE MEASURES
========================
`ceiling 2, setups 0 of 7, fills 0`. That is the finding and no second sentence
is entitled to more.
"""
from __future__ import annotations

import ast
import datetime as dt
import hashlib
import json
import os
import re
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "tools"))

import backtest as bt  # noqa: E402
import corpus_prefix as cp  # noqa: E402
import market_data as md  # noqa: E402
import project_status as ps  # noqa: E402
import promotion_gate as pg  # noqa: E402
import shadow  # noqa: E402
from signals import funding_carry_fade_btc_v1 as fb  # noqa: E402

FRESHNESS = "artifacts/slice69_data_freshness.json"
FORWARD = "artifacts/slice69_forward_shadow.json"
GATE = "artifacts/slice69_promotion_gate.json"

PRIOR = {n: (f"artifacts/slice{n}_data_freshness.json",
             f"artifacts/slice{n}_forward_shadow.json", bars)
         for n, bars in ((62, 1), (63, 1), (64, 2), (65, 3), (66, 4),
                         (67, 5), (68, 6))}

# The slices whose freshness artefact carried a stale note path, and the value
# it was stuck on. EDGE.md §52a.
STALE_NOTE_PATH_SLICES = (65, 66, 67, 68)
STALE_NOTE_PATH = "docs/human/HUMAN_DATA_NOTE_SLICE64.md"


def load(path: str) -> dict:
    with open(os.path.join(REPO, path), encoding="utf-8") as handle:
        return json.load(handle)


def sha256(path: str) -> str:
    with open(os.path.join(REPO, path), "rb") as handle:
        return hashlib.sha256(handle.read()).hexdigest()


def prose(text: str) -> str:
    return " ".join(str(text).split())


# ===========================================================================
# 1 — the window reached seven
# ===========================================================================


class TestTheWindowReachedSeven:

    def test_seven_closed_bars_lie_after_t1(self):
        result = cp.check(cp.LINEAR_BTC)
        assert result.appended_rows >= 7
        assert [s[:10] for s in result.appended_timestamps][:7] == [
            "2026-08-10", "2026-08-11", "2026-08-12", "2026-08-13",
            "2026-08-14", "2026-08-15", "2026-08-16"]
        assert load(FRESHNESS)["after_t1_linear"] == 7
        assert load(FRESHNESS)["after_t1_dates"] == [
            "2026-08-10", "2026-08-11", "2026-08-12", "2026-08-13",
            "2026-08-14", "2026-08-15", "2026-08-16"]

    def test_the_growth_is_measured_against_the_previous_artefact(self):
        delta = load(FRESHNESS)["delta_since_slice68"]
        assert delta["previous_after_t1_linear"] == 6
        assert delta["new_linear_bars_since_slice68"] == 1
        assert delta["the_window_grew"] is True

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

    def test_the_open_bar_is_absent(self):
        """No bar dated today or later is on disk. Clock-derived.

        AMENDED BY SLICE 70. This test shipped with the clock-derived
        assertion AND a hard-coded restatement that 2026-08-17 was absent.
        The docstring said the assertion could not expire; the line below it
        expired eight days later, when 08-17 legitimately closed. The literal
        was added for certainty and was the only line in the test that could
        go stale — which is the whole lesson, so the derived line stays and
        the literal is gone rather than being bumped to 08-18. EDGE.md §53d.

        The frozen artefact's own claim is checked in the slice-70 tests
        against the tree as it was when that artefact was written.
        """
        today = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
        rows = cp.read_rows(cp.LINEAR_BTC)
        assert not any(r["time_period_start"][:10] >= today for r in rows)
        assert load(FRESHNESS)["open_bar_absent_as_expected"] is True

    def test_every_note_claim_is_true(self):
        payload = load(FRESHNESS)
        assert payload["note_claims_all_true"] is True
        assert payload["false_note_claims"] == []
        assert payload["human_note_is_evidence"] is False
        assert payload["claim_vs_files_discrepancy"] is False

    def test_this_slices_counts_came_from_files_not_the_manifest(self):
        """AMENDED BY SLICE 70: direction of travel, not equality.

        This asserted that the LIVE corpus row count equals the count in
        slice 69's DATED artefact. That held for exactly as long as no bar
        arrived, and broke the day one did — not because anything was wrong,
        but because a dated record was being asserted against a living file.
        A dated claim is checked against the frozen artefact; only the
        DIRECTION OF TRAVEL is checked against disk. EDGE.md §48c, §53d.

        Slice 66's guard could not catch this: the right-hand side is an
        artefact lookup, not an integer literal. Slice 70 adds the member of
        the guard family that does.
        """
        assert cp.check(cp.LINEAR_BTC).rows_on_disk >= \
            load(FRESHNESS)["linear_rows"]
        assert cp.check(cp.FUNDING_BTC).rows_on_disk >= \
            load(FRESHNESS)["funding_rows"]
        # The artefact's own internal consistency, which cannot expire.
        assert load(FRESHNESS)["linear_rows"] == 1468
        assert load(FRESHNESS)["after_t1_linear"] == 7


# ===========================================================================
# 2 — the stale note path, killed and kept dead
# ===========================================================================


class TestTheStaleNotePathIsDeadAndStaysDead:

    def test_this_slices_path_names_this_slice(self):
        payload = load(FRESHNESS)
        assert payload["human_note_path"] == \
            "docs/human/HUMAN_DATA_NOTE_SLICE69.md"
        assert payload["human_note_present"] is True

    def test_the_path_is_derived_not_a_literal(self):
        """The fix that removes the class rather than the instance.

        The module must build the path from its slice number. A literal would
        freeze again the moment the tool is derived for slice 70.
        """
        source = os.path.join(REPO, "tools", "slice69_freshness.py")
        with open(source, encoding="utf-8") as handle:
            tree = ast.parse(handle.read())
        assignment = next(
            node for node in tree.body
            if isinstance(node, ast.Assign)
            and any(getattr(t, "id", None) == "NOTE_PATH" for t in node.targets))
        assert isinstance(assignment.value, ast.JoinedStr), (
            "NOTE_PATH is a plain literal again; it must be derived from the "
            "slice number. EDGE.md §52a.")

    def test_the_presence_check_actually_reads_this_slices_note(self):
        """The defect's real shape: a check that verified the wrong file and
        therefore could not fail. Assert it now fails when it should."""
        payload = load(FRESHNESS)
        note = os.path.join(REPO, payload["human_note_path"])
        assert os.path.isfile(note)
        with open(note, encoding="utf-8") as handle:
            text = handle.read()
        assert "Slice 69" in text
        assert "2026-08-16" in text

    def test_the_defect_spanned_four_slices_and_is_recorded_as_such(self):
        """Not minimised to the one slice the human happened to spot."""
        for number in STALE_NOTE_PATH_SLICES:
            artefact = load(f"artifacts/slice{number}_data_freshness.json")
            assert artefact["human_note_path"] == STALE_NOTE_PATH, number

    def test_the_slices_before_it_were_correct(self):
        for number in (62, 63, 64):
            artefact = load(f"artifacts/slice{number}_data_freshness.json")
            assert artefact["human_note_path"] == \
                f"docs/human/HUMAN_DATA_NOTE_SLICE{number}.md", number

    def test_no_scored_claim_was_tainted(self):
        """The blast radius, asserted rather than asserted-about.

        Every claim in each affected slice's `human_note_claims_checked` is a
        comparison against a value computed from the corpus — so the artefacts
        said true things about the right data while pointing at a wrong path.
        """
        for number in STALE_NOTE_PATH_SLICES:
            artefact = load(f"artifacts/slice{number}_data_freshness.json")
            claims = artefact["human_note_claims_checked"]
            assert claims, number
            # Slice 67's pack-base claim was legitimately false; every other
            # claim in every affected slice held.
            false_claims = [k for k, v in claims.items() if not v]
            assert false_claims in ([], ["pack base: post-restore slice66 tree "
                                         "(not slice-61)"]), (number,
                                                              false_claims)


class TestNoToolCarriesAnotherSlicesConstant:
    """A new member of slice 66's guard family. EDGE.md §52a.

    Slice 66's AST sweep catches a live corpus count pinned to a literal. It
    would NOT have caught a stale path, because that is not a comparison at
    all — so the family grows a member: a `sliceNN_*` tool may not carry a
    string literal naming a DIFFERENT slice unless the line is annotated as a
    deliberate back-reference.

    Legitimate back-references are common and necessary — every slice reads its
    predecessor's artefact — so they are permitted and must be *marked*, which
    is the point: an intentional cross-slice reference is a decision, and an
    accidental one is a stale constant.
    """

    ANNOTATION = "prior-slice"
    PATTERN = re.compile(r"[Ss][Ll][Ii][Cc][Ee][_ -]?(\d{2})")

    @classmethod
    def _offences(cls, path):
        """A literal naming a slice that is neither THIS one nor the one just
        before it must be annotated.

        That rule is chosen because it matches the defect exactly. Each tool is
        derived from its predecessor by substitution, so a correct reference
        either names the current slice (own outputs) or the previous one
        (reading its artefacts) — and ADVANCES every slice. A literal naming
        anything older either advanced and stopped, which is the defect, or was
        always meant to be fixed, which is a decision and must be marked.

        Long prose is exempt: a docstring narrating history is not a constant
        in use, and requiring annotations inside narrative would train people
        to sprinkle the marker rather than think about it.
        """
        own = re.search(r"slice(\d+)_", os.path.basename(path))
        if not own:
            return []
        current = int(own.group(1))
        permitted = {current, current - 1}
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
        lines = text.splitlines()

        found = []
        for node in ast.walk(ast.parse(text, filename=path)):
            if not (isinstance(node, ast.Constant)
                    and isinstance(node.value, str)):
                continue
            if len(node.value) > 200:
                continue
            others = {int(m) for m in cls.PATTERN.findall(node.value)}
            stale = sorted(others - permitted)
            if not stale:
                continue
            line = lines[node.lineno - 1] if node.lineno <= len(lines) else ""
            # Five lines: an annotation sits above the KEY, and a
            # multi-line literal pushes its own lineno further down.
            context = "\n".join(lines[max(0, node.lineno - 6):node.lineno])
            if cls.ANNOTATION in line or cls.ANNOTATION in context:
                continue
            found.append((node.lineno, stale, node.value[:70]))
        return found

    def _tools(self):
        base = os.path.join(REPO, "tools")
        return [os.path.join(base, name) for name in sorted(os.listdir(base))
                if re.match(r"slice\d+_.*\.py$", name)]

    def test_this_slices_tools_carry_no_unannotated_foreign_slice(self):
        offences = {os.path.basename(p): self._offences(p)
                    for p in self._tools()
                    if os.path.basename(p).startswith("slice69_")}
        assert {k: v for k, v in offences.items() if v} == {}, json.dumps(
            offences, indent=2)

    def test_the_guard_catches_the_defect_it_was_written_for(self):
        """A guard never observed to fail is not known to be a guard.

        The exact shape that shipped four times: a slice-69 tool carrying
        `HUMAN_DATA_NOTE_SLICE64.md`.
        """
        import tempfile

        cases = {
            'NOTE_PATH = "docs/human/HUMAN_DATA_NOTE_SLICE64.md"\n': 1,
            'PREVIOUS = "artifacts/slice68_data.json"  # prior-slice\n': 0,
            'SLICE = 69\nNOTE = f"NOTE_SLICE{SLICE}.md"\n': 0,
        }
        for source, expected in cases.items():
            directory = tempfile.mkdtemp()
            name = os.path.join(directory, "slice69_probe.py")
            with open(name, "w", encoding="utf-8") as handle:
                handle.write(source)
            try:
                assert len(self._offences(name)) == expected, source
            finally:
                os.unlink(name)
                os.rmdir(directory)

    def test_the_sweep_reached_the_slice_tools(self):
        names = {os.path.basename(p) for p in self._tools()}
        assert any(n.startswith("slice69_") for n in names)
        assert len(names) >= 12


# ===========================================================================
# 3 — ceiling 2, setups 0 of 7, fills 0
# ===========================================================================


class TestCeilingTwoZeroSetupsZeroFills:

    def test_the_ceiling_is_two(self):
        ceiling = load(FORWARD)["ceiling"]
        assert ceiling["closed_forward_bars"] == 7
        assert ceiling["max_possible_forward_closed_trades"] == 2
        assert ceiling["observed_forward_closed_trades"] == 0
        assert ceiling["within_ceiling"] is True
        assert "§52b" in ceiling["declared_in"]

    def test_the_ceiling_is_arithmetic(self):
        ceiling = load(FORWARD)["ceiling"]
        assert ceiling["max_possible_forward_closed_trades"] == \
            max(0, ceiling["closed_forward_bars"] - fb.HORIZON)
        assert fb.HORIZON == 5

    def test_it_is_not_one_and_not_three(self):
        """Both adjacent errors are explicit mission failures."""
        ceiling = load(FORWARD)["ceiling"][
            "max_possible_forward_closed_trades"]
        assert ceiling != 1, "1 was slice 68's ceiling"
        assert ceiling < 3

    def test_slice68_ran_at_ceiling_one(self):
        prior = load("artifacts/slice68_forward_shadow.json")["ceiling"]
        assert prior["closed_forward_bars"] == 6
        assert prior["max_possible_forward_closed_trades"] == 1

    def test_zero_setups_on_all_seven_bars(self):
        regime = load(FRESHNESS)["forward_funding_regime"]
        assert regime["funding_setups_in_window"] == 0
        assert regime["forward_bars"] == 7
        rates = regime["rate_at_each_forward_decision"]
        assert len(rates) == 7
        assert all(value < fb.FUND_ABS for value in rates.values())

    def test_the_newest_bar_is_half_the_threshold(self):
        rates = load(FRESHNESS)["forward_funding_regime"][
            "rate_at_each_forward_decision"]
        assert rates["2026-08-16"] == 0.00005
        assert rates["2026-08-16"] < fb.FUND_ABS

    def test_the_only_threshold_touching_print_is_still_the_08_12_one(self):
        regime = load(FRESHNESS)["forward_funding_regime"]
        assert len(regime["prints_at_or_above_fund_abs"]) == 1
        entry = regime["prints_at_or_above_fund_abs"][0]
        assert entry["funding_time"].startswith("2026-08-12T00:00")
        assert entry["equals_fund_abs_exactly"] is True

    def test_the_rule_stood_aside_on_all_seven(self):
        aside = load(FORWARD)["forward_decisions"]["why_the_rule_stood_aside"]
        assert len(aside) == 7
        assert all(entry["funding_setup_present"] is False for entry in aside)
        last = [e for e in aside if e["is_last_bar_of_corpus"]]
        assert len(last) == 1 and last[0]["bar_utc"].startswith("2026-08-16")

    def test_the_finding_is_stated_and_bounded(self):
        note = prose(load(FORWARD)["window_grew_since_slice68"]["note"])
        assert "ceiling 2, setups 0 of 7, fills 0" in note
        assert "no second sentence is entitled to more" in note
        assert "says NOTHING about whether the rule makes money" in note
        assert "one per eighteen days" in note

    def test_growth_and_observation_remain_independent(self):
        assert load(FRESHNESS)["delta_since_slice68"]["the_window_grew"] is True
        assert load(FORWARD)["is_forward_observation"] is False
        assert load(FORWARD)["forward_n_trades"] == 0
        assert load(FORWARD)["trades"] == []

    def test_no_clamping_no_slicing_no_invention(self):
        assert load(FORWARD)["forward_decisions"][
            "exit_clamping_to_corpus_end"] is False
        assert load(FORWARD)["forward_window"]["bar_array_sliced"] is False
        assert load(FORWARD)["forward_window"]["invented_future_bars"] == 0
        assert load(FORWARD)["bars_fabricated"] == 0
        assert load(FRESHNESS)["bars_fabricated"] == 0

    def test_the_forward_monitors_report_insufficient_data(self):
        states = {r["name"]: r["state"]
                  for r in load(FORWARD)["forward_monitor_readings"]}
        assert set(states.values()) == {"INSUFFICIENT_DATA"}

    def test_the_historical_m4_warn_was_not_rebranded(self):
        historical = load("artifacts/slice59_forward_shadow.json")
        m4 = next(r for r in historical["monitor_readings"]
                  if r["name"] == "M4_halves")
        assert m4["state"] == "WARN"
        assert "deliberately NOT recomputed" in \
            prose(load(FORWARD)["monitor_scope_note"])

    def test_every_prior_slice_ceiling_is_recorded_and_lower(self):
        ceilings = {n: load(f)["ceiling"]["max_possible_forward_closed_trades"]
                    for n, (_x, f, _b) in sorted(PRIOR.items())}
        assert list(ceilings.values()) == [0, 0, 0, 0, 0, 0, 1]
        assert load(FORWARD)["ceiling"][
            "max_possible_forward_closed_trades"] == 2


# ===========================================================================
# 4 — FUND_ABS and the join did not move
# ===========================================================================


class TestTheRuleWasNotRescued:

    def test_fund_abs_is_unchanged(self):
        assert fb.FUND_ABS == 0.0001
        assert load(FORWARD)["fund_abs"] == 0.0001
        assert load(FORWARD)["fund_abs_moved_this_slice"] is False

    def test_the_join_is_unchanged(self):
        assert load(FORWARD)["join_changed_this_slice"] is False
        assert "CLOSE" in load(FORWARD)["join_rule"]
        bars, funding, folds = _corpus()
        window = list(fb.forward_window_indices(bars, folds))
        rates = fb.funding_at_decision(bars, funding)
        target = next(i for i in window
                      if dt.datetime.fromtimestamp(
                          bars[i].start_ms / 1000.0,
                          tz=dt.timezone.utc).strftime("%Y-%m-%d")
                      == "2026-08-12")
        assert rates[target] == 0.00006601
        assert rates[target] < fb.FUND_ABS

    def test_the_reason_neither_moved_is_recorded(self):
        why = prose(load(FORWARD)["why_neither_moved"])
        assert "fitting the rule to the data" in why
        assert "would void the clear" in why

    def test_the_horizon_and_fingerprint_are_unchanged(self):
        assert fb.HORIZON == 5
        assert shadow.constants_fingerprint()[:32] == \
            "662de0115880871352d5d623b1020eaa"


def _corpus():
    loaded, _b, _n = md.load_corpus(
        os.path.join(REPO, "data", "real_linear_1d"), verify=False)
    bars = [bt.Bar(b.start_ms, b.open, b.high, b.low, b.close, b.volume)
            for b in loaded["BTCUSDT"]]
    funding = fb.load_funding(os.path.join(
        REPO, "data", "real_funding", "funding",
        "BINANCE_LINEAR_BTC_USDT_FUNDING.csv.gz"))
    return bars, funding, fb.load_folds()


# ===========================================================================
# 5 — the pack, and everything frozen
# ===========================================================================


class TestThePackAndTheFreezes:

    def test_no_restoration_was_needed_again(self):
        assert not os.path.exists(os.path.join(
            REPO, "artifacts", "slice69_restored_from_slice68.json"))

    def test_edge_carries_every_section_from_45_to_52(self):
        with open(os.path.join(REPO, "EDGE.md"), encoding="utf-8") as handle:
            edge = handle.read()
        for section in range(45, 53):
            assert f"\n## {section}a." in edge, section

    def test_the_meta_guards_are_all_enforced(self):
        from test_slice66_four_day_window import (  # noqa: PLC0415
            TestNoTestPinsALiveAbsolute as Guard)
        guard = Guard()
        offences = {os.path.basename(p): guard._offences(p)
                    for p in guard._test_files()}
        assert {k: v for k, v in offences.items() if v} == {}

    def test_the_frozen_pack_is_intact(self):
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
        assert fb.UNIVERSE == ("BTCUSDT",)

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
        for name in sorted(os.listdir(os.path.join(REPO, "artifacts"))):
            if not name.startswith("slice69_") or not name.endswith(".json"):
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
        assert pg.MIN_FORWARD_TRADES == 20
        assert pg.MIN_FORWARD_DAYS == 180
        assert load(GATE)["minimums_moved_this_slice"] is False

    def test_the_gate_refuses_to_credit_two_possible_trades(self):
        evidence = prose(load(GATE)["checklist"]["forward_shadow_clean"][
            "evidence"])
        assert "TWO POSSIBLE TRADES DO NOT ADVANCE THIS GATE" in evidence
        assert "never the numerator of what has happened" in evidence

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

    def test_the_manifest_residue_is_unchanged_and_the_broadcast_stopped(self):
        for directory in ("data/real_linear_1d", "data/real_funding"):
            with open(os.path.join(REPO, directory, "MANIFEST.json"),
                      encoding="utf-8") as handle:
                declared = json.load(handle)["date_range_utc"]
            assert declared["BTCUSDT"]["rows"] != declared["ETHUSDT"]["rows"]
            assert declared["ETHUSDT"]["rows"] == declared["SOLUSDT"]["rows"]
        audit = load(FRESHNESS)["manifests"]
        assert audit["measured_product_entries_are_accurate"] is True
        assert audit["count_disagreeing"] == 4
        assert audit["not_edited_by_this_slice"] is True

    def test_the_funding_seam_is_still_unfilled(self):
        assert load(FRESHNESS)["funding_seam"]["missing_prints"] == \
            ["2026-08-09T16:00:00+00:00"]

    def test_no_fetch_was_attempted(self):
        assert load(FRESHNESS)["fetch_attempted"] is False
