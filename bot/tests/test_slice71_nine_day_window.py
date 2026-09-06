"""Slice 71 — nine bars, ceiling 4, zero setups, and two traps about form.

WHAT THIS SLICE MEASURES
========================
`ceiling 4, setups 0 of 9, fills 0`. That is the finding. No second sentence is
entitled to more.

WHAT THIS SLICE FOUND
=====================
Two things arrived looking like changes and were not, in opposite directions.

1.  The funding `.csv.gz` has a **different compressed digest** and a
    **byte-identical uncompressed stream**. It was recompressed; not one print
    changed. Every earlier instance of §46c's digest trap sat beside a corpus
    that had genuinely grown, so conflating the two still reached the right
    verdict by luck. Here it does not: the naive check reports a change that
    did not happen. `new_funding_prints_since_slice70` is **0**.

2.  Both BTC manifest entries changed their `end` field's **encoding** —
    `"2026-08-18"` became `"2026-08-18T00:00:00+00:00"`. A strict string
    comparison would have reported **six** disagreeing entries where there are
    **four**, and called a correct manifest wrong.

Same lesson, mirrored: identical content wearing different bytes, and identical
content wearing a different string. **Compare the thing you mean, not the thing
that is easy to compare.** The strict result is reported beside the content
result rather than discarded, because a provenance record that silently changes
encoding is worth seeing — it just does not get to be called a disagreement.

THE GUARD FAMILY IS CARRIED, AND ONE HOLE IS NAMED
==================================================
All four of slice 70's guards run here, plus the scoring-equality comparison
against slice 70. The single baseline failure was slice 70's own
`test_the_counts_came_from_files`: an equality against its OWN artefact, which
member three permits, and which became an equality against a DATED artefact one
slice later. No sweep can see that — the code was right when written. The
standing rule is what fixes it, and §54f says so rather than weakening a guard
that is working.
"""
from __future__ import annotations

import ast
import datetime as dt
import gzip
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

FRESHNESS = "artifacts/slice71_data_freshness.json"
FORWARD = "artifacts/slice71_forward_shadow.json"
GATE = "artifacts/slice71_promotion_gate.json"

WINDOW = ["2026-08-10", "2026-08-11", "2026-08-12", "2026-08-13",
          "2026-08-14", "2026-08-15", "2026-08-16", "2026-08-17",
          "2026-08-18"]

PRIOR = {n: (f"artifacts/slice{n}_forward_shadow.json", ceiling)
         for n, ceiling in ((62, 0), (63, 0), (64, 0), (65, 0), (66, 0),
                            (67, 0), (68, 1), (69, 2), (70, 3))}


def load(path: str) -> dict:
    with open(os.path.join(REPO, path), encoding="utf-8") as handle:
        return json.load(handle)


def sha256(path: str) -> str:
    with open(os.path.join(REPO, path), "rb") as handle:
        return hashlib.sha256(handle.read()).hexdigest()


def gunzip_sha256(path: str) -> str:
    with gzip.open(os.path.join(REPO, path), "rb") as handle:
        return hashlib.sha256(handle.read()).hexdigest()


def prose(text: str) -> str:
    return " ".join(str(text).split())


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
# 1 — the window reached nine
# ===========================================================================


class TestTheWindowReachedNine:

    def test_nine_closed_bars_lie_after_t1(self):
        result = cp.check(cp.LINEAR_BTC)
        assert result.appended_rows >= 9
        assert [s[:10] for s in result.appended_timestamps][:9] == WINDOW
        assert load(FRESHNESS)["after_t1_linear"] == 9
        assert load(FRESHNESS)["after_t1_dates"] == WINDOW

    def test_the_growth_is_measured_against_the_previous_artefact(self):
        delta = load(FRESHNESS)["delta_since_slice70"]
        assert delta["previous_after_t1_linear"] == 8
        assert delta["new_linear_bars_since_slice70"] == 1
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

    def test_only_the_linear_corpus_grew(self):
        """Not "only BTC grew" — this slice is narrower than that.

        BTC's linear file gained one bar. BTC's FUNDING file gained nothing:
        same 4,410 rows, same last print. ETH and SOL gained nothing either.
        """
        grew = {p for p, r in cp.check_all().items() if r.extended}
        assert grew == {cp.LINEAR_BTC, cp.FUNDING_BTC}, (
            "the funding file is still EXTENDED relative to its pin — that is "
            "about the pin, not about this slice")
        assert load(FRESHNESS)["delta_since_slice70"][
            "new_funding_prints_since_slice70"] == 0
        for path, result in cp.check_all().items():
            if "BTC" not in path:
                assert result.appended_rows == 0, path

    def test_the_open_bar_is_absent(self):
        """Clock-derived and nothing else. No literal beside it. §53d."""
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
        assert len(payload["human_note_claims_checked"]) >= 10

    def test_the_note_path_names_this_slice_and_slice_is_derived(self):
        payload = load(FRESHNESS)
        assert payload["human_note_path"] == \
            "docs/human/HUMAN_DATA_NOTE_SLICE71.md"
        assert payload["human_note_present"] is True
        assert payload["slice"] == 71
        source = os.path.join(REPO, "tools", "slice71_freshness.py")
        with open(source, encoding="utf-8") as handle:
            tree = ast.parse(handle.read())
        note_path = next(
            node for node in tree.body
            if isinstance(node, ast.Assign)
            and any(getattr(t, "id", None) == "NOTE_PATH"
                    for t in node.targets))
        assert isinstance(note_path.value, ast.JoinedStr)
        slice_const = next(
            node for node in tree.body
            if isinstance(node, ast.Assign)
            and any(getattr(t, "id", None) == "SLICE" for t in node.targets))
        assert slice_const.value.value == 71, (
            "SLICE froze at its predecessor's value for the third slice "
            "running. The substitution pipeline cannot see it; this test is "
            "the only thing that does. EDGE.md §52a, §54f.")

    def test_the_slice_number_is_derived_in_every_tool(self):
        """The integer the pipeline has never once updated.

        Each slice-71 tool must take its `slice` field from the SLICE
        constant rather than repeating the number in the payload, so there is
        exactly one place to get it wrong and a test aimed at that place.
        """
        for name in ("slice71_freshness.py", "slice71_forward_shadow.py",
                     "slice71_promotion_gate.py"):
            with open(os.path.join(REPO, "tools", name),
                      encoding="utf-8") as handle:
                tree = ast.parse(handle.read())
            literals = [
                node for node in ast.walk(tree)
                if isinstance(node, ast.Dict)
                for key, value in zip(node.keys, node.values)
                if isinstance(key, ast.Constant) and key.value == "slice"
                and isinstance(value, ast.Constant)]
            assert literals == [], (
                f"{name}: the payload's slice number is a literal again")
        for artefact in (FRESHNESS, FORWARD, GATE):
            assert load(artefact)["slice"] == 71, artefact

    def test_the_counts_came_from_this_slices_files(self):
        """AMENDED BY SLICE 72, exactly as slice 71's verdict predicted.

        Written as an equality against THIS slice's own artefact, which is
        correct at authoring time and expires the moment a bar arrives. The
        expiry was named in `STAGE1_VERDICT_SLICE71.md` before it happened,
        which is the only part of this that was under anyone's control.

        Direction of travel against disk; equality against the record.
        EDGE.md §54f, §55f.
        """
        assert cp.check(cp.LINEAR_BTC).rows_on_disk >= \
            load(FRESHNESS)["linear_rows"]
        assert cp.check(cp.FUNDING_BTC).rows_on_disk >= \
            load(FRESHNESS)["funding_rows"]
        assert load(FRESHNESS)["linear_rows"] == 1470
        assert load(FRESHNESS)["after_t1_linear"] == 9


# ===========================================================================
# 2 — the funding corpus did not change, and its file digest did
# ===========================================================================


class TestTheFundingCorpusDidNotChange:

    PRIOR_FUNDING_UNCOMPRESSED = (
        "fa8b207f79115694ec2908507e830c56a7a2a44c1d4f5bd0355223775450bf73")

    def test_the_uncompressed_stream_is_identical_to_slice70s(self):
        """AMENDED BY SLICE 72: the dated claim, checked against the record.

        This asserted a LIVE digest against a constant. It was true for as
        long as the funding corpus stood still, and slice 72's pack advanced
        it by six prints — which is the outcome slice 71 asked for, not a
        contradiction of what slice 71 found.

        What slice 71 found is preserved here in the form it was recorded:
        the FROZEN artefact says the streams were identical then. The live
        file is checked only for direction of travel. EDGE.md §48c, §55f.
        """
        digests = load(FRESHNESS)["digests"]
        assert digests["funding_identical_to_slice70_uncompressed"] is True
        assert digests["slice70_funding_sha256_uncompressed"] == \
            self.PRIOR_FUNDING_UNCOMPRESSED
        assert digests["funding_sha256_uncompressed"] == \
            self.PRIOR_FUNDING_UNCOMPRESSED
        # Direction of travel: history is still an append onto that stream.
        assert cp.check(cp.FUNDING_BTC).append_only is True

    def test_the_compressed_digest_moved_and_says_nothing(self):
        """The trap, asserted from both sides rather than described."""
        digests = load(FRESHNESS)["digests"]
        assert digests["funding_compressed_digest_changed_this_slice"] is True
        assert digests["funding_sha256_compressed"] != \
            digests["funding_sha256_uncompressed"]
        assert sha256(cp.FUNDING_BTC) != self.PRIOR_FUNDING_UNCOMPRESSED

    def test_the_content_is_identical_row_for_row(self):
        """AMENDED BY SLICE 72. The dated claim is kept; its live half is not.

        `len(rows) == 4410` was a live corpus count pinned to a positive
        integer literal — the defect slice 66's guard exists to make
        unshippable — in a spelling that guard does not recognise, because it
        matches `.rows_on_disk` and `.appended_rows` attributes rather than
        `len(cp.read_rows(...))`. It shipped, and then expired on schedule.
        Recorded rather than quietly deleted. EDGE.md §49b, §55f.
        """
        frozen = load(FRESHNESS)
        assert frozen["funding_rows"] == 4410
        assert frozen["latest_funding_timestamp"] == \
            "2026-08-18T16:00:00+00:00"
        assert frozen["after_t1_funding"] == 27
        assert frozen["delta_since_slice70"][
            "new_funding_prints_since_slice70"] == 0
        # Direction of travel only.
        assert cp.check(cp.FUNDING_BTC).rows_on_disk >= frozen["funding_rows"]

    def test_the_repository_pins_the_uncompressed_stream(self):
        """Which is why the prefix invariant is untouched by a recompression."""
        assert cp.check(cp.FUNDING_BTC).append_only is True
        assert cp.check(cp.FUNDING_BTC).history_unchanged is True
        folds = fb.load_folds()
        assert cp.check(cp.FUNDING_BTC).prefix_sha256 == \
            folds["sources"]["funding"]["sha256_uncompressed"]

    def test_the_finding_is_recorded_not_glossed(self):
        text = prose(load(FRESHNESS)["digests"]["the_trap_fired_this_slice"])
        assert "recompressed" in text.lower()
        assert "§54d" in text


# ===========================================================================
# 3 — a manifest that changed format, not content
# ===========================================================================


class TestTheManifestChangedFormatNotContent:

    BTC = ("data/real_linear_1d/ohlcv/BINANCE_LINEAR_BTC_USDT_1D.csv.gz",
           "data/real_funding/funding/BINANCE_LINEAR_BTC_USDT_FUNDING.csv.gz")

    def test_the_declared_end_is_now_a_timestamp(self):
        entries = load(FRESHNESS)["manifests"]["entries"]
        for path in self.BTC:
            entry = entries[path]
            assert entry["declared_end_is_a_timestamp_not_a_date"] is True
            assert entry["declared_end_format_changed_this_slice"] is True
            assert entry["end_agrees_strictly"] is False
            assert entry["end_agrees_on_content"] is True

    def test_a_strict_comparison_would_have_reported_six(self):
        audit = load(FRESHNESS)["manifests"]
        assert audit["count_disagreeing"] == 4
        assert audit["count_a_strict_comparison_would_have_reported"] == 6
        assert sorted(audit["entries_a_strict_end_comparison_would_have_"
                            "failed"]) == sorted(self.BTC)

    def test_the_measured_products_entries_are_still_accurate(self):
        audit = load(FRESHNESS)["manifests"]
        assert audit["measured_product_entries_are_accurate"] is True
        for path in self.BTC:
            entry = audit["entries"][path]
            assert entry["rows_agree"] is True
            assert entry["rows_on_disk"] == entry["declared_rows"]

    def test_the_format_change_is_surfaced_not_smoothed(self):
        audit = load(FRESHNESS)["manifests"]
        assert sorted(audit["declared_end_format_changed_this_slice"]) == \
            sorted(self.BTC)
        why = prose(audit["why_content_decides"])
        assert "§54e" in why
        assert "representation" in why

    def test_eth_and_sol_are_untouched_in_both_senses(self):
        entries = load(FRESHNESS)["manifests"]["entries"]
        for path, entry in entries.items():
            if "BTC" in path:
                continue
            assert entry["declared_end_is_a_timestamp_not_a_date"] is False
            assert entry["declared_rows_moved_this_slice"] is False
            assert entry["manifest_describes_the_file"] is False
        assert load(FRESHNESS)["manifests"][
            "broadcast_occurred_this_slice"] is False
        assert load(FRESHNESS)["manifests"]["not_edited_by_this_slice"] is True


# ===========================================================================
# 4 — ceiling 4, setups 0 of 9, fills 0
# ===========================================================================


class TestCeilingFourZeroSetupsZeroFills:

    def test_the_ceiling_is_four(self):
        ceiling = load(FORWARD)["ceiling"]
        assert ceiling["closed_forward_bars"] == 9
        assert ceiling["max_possible_forward_closed_trades"] == 4
        assert ceiling["observed_forward_closed_trades"] == 0
        assert ceiling["within_ceiling"] is True
        assert "§54a" in ceiling["declared_in"]

    def test_the_ceiling_is_arithmetic(self):
        payload = load(FRESHNESS)
        assert max(0, payload["after_t1_linear"] - fb.HORIZON) == 4
        assert fb.HORIZON == 5

    def test_it_is_not_three_and_not_five(self):
        ceiling = load(FORWARD)["ceiling"]["max_possible_forward_closed_trades"]
        assert ceiling != 3, "3 was slice 70's ceiling, at N=8"
        assert ceiling < 5, "5 would need N=10"

    def test_every_prior_slice_ceiling_is_recorded_and_lower(self):
        observed = [load(f)["ceiling"]["max_possible_forward_closed_trades"]
                    for _n, (f, _c) in sorted(PRIOR.items())]
        assert observed == [expected for _n, (_f, expected)
                            in sorted(PRIOR.items())]
        assert observed == [0, 0, 0, 0, 0, 0, 1, 2, 3]
        assert load(FORWARD)["ceiling"][
            "max_possible_forward_closed_trades"] == 4

    def test_every_prior_slice_observed_zero(self):
        """The numerator, across every slice that has ever reported one."""
        for _n, (path, _c) in sorted(PRIOR.items()):
            assert load(path)["forward_n_trades"] == 0, path
        assert load(FORWARD)["forward_n_trades"] == 0

    def test_zero_setups_on_all_nine_bars(self):
        regime = load(FRESHNESS)["forward_funding_regime"]
        assert regime["forward_bars"] == 9
        assert regime["funding_setups_in_window"] == 0
        rates = regime["rate_at_each_forward_decision"]
        assert sorted(rates) == WINDOW
        assert all(abs(v) < fb.FUND_ABS for v in rates.values())

    def test_the_per_bar_table_covers_every_date(self):
        table = load(FORWARD)["funding_setup_table"]
        assert [row["date"] for row in table] == WINDOW
        assert all(row["funding_setup_present"] is False for row in table)
        assert all(row["shortfall_vs_fund_abs"] > 0 for row in table)

    def test_the_rule_stood_aside_on_all_nine(self):
        aside = load(FORWARD)["forward_decisions"]["why_the_rule_stood_aside"]
        assert len(aside) == 9
        assert all(row["funding_setup_present"] is False for row in aside)
        assert load(FORWARD)["forward_decisions"][
            "flagged_bars_in_window"] == []
        assert load(FORWARD)["forward_decisions"][
            "candidates_after_schedule"] == []

    def test_no_trade_and_no_observation(self):
        forward = load(FORWARD)
        assert forward["forward_n_trades"] == 0
        assert forward["is_forward_observation"] is False
        assert forward["trades"] == []
        assert forward["forward_mean_net_r"] is None
        assert forward["is_stage1_evidence"] is False
        assert forward["registration_eligible"] is False

    def test_growth_and_observation_remain_independent(self):
        forward = load(FORWARD)
        assert forward["extension_present"] is True
        assert forward["is_forward_observation"] is False
        assert "A BAR ARRIVING IS NOT AN OBSERVATION" in \
            prose(forward["labelling_rule"])

    def test_the_finding_says_nine_and_not_eight(self):
        for text in (prose(load(FRESHNESS)["delta_since_slice70"]["finding"]),
                     prose(load(FORWARD)["window_grew_since_slice70"]["note"]),
                     prose(load(FRESHNESS)["forward_funding_regime"][
                         "finding"])):
            assert "EIGHT BARS" not in text.upper()
            assert "SEVEN BARS" not in text.upper()
        finding = prose(load(FRESHNESS)["delta_since_slice70"]["finding"])
        assert "NINE BARS" in finding.upper()
        assert "ceiling 4, setups 0 of 9, fills 0" in finding

    def test_no_clamping_no_slicing_no_invention(self):
        forward = load(FORWARD)
        assert forward["forward_decisions"]["exit_clamping_to_corpus_end"] \
            is False
        assert forward["forward_window"]["bar_array_sliced"] is False
        assert forward["forward_window"]["invented_future_bars"] == 0
        assert forward["bars_fabricated"] == 0
        assert load(FRESHNESS)["bars_fabricated"] == 0
        assert load(FRESHNESS)["corpus_appended_to_by_this_tool"] is False

    def test_the_last_bar_cannot_fill_and_says_so(self):
        aside = load(FORWARD)["forward_decisions"]["why_the_rule_stood_aside"]
        last = aside[-1]
        assert last["bar_utc"][:10] == "2026-08-18"
        assert last["is_last_bar_of_corpus"] is True
        assert "no next bar to fill on" in prose(last["excluded_as_last_bar"])

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


# ===========================================================================
# 5 — three near misses, none rescued
# ===========================================================================


class TestNoNearMissWasRescued:

    def test_fund_abs_is_unchanged(self):
        assert fb.FUND_ABS == 0.0001
        assert load(FORWARD)["fund_abs"] == 0.0001
        assert load(FORWARD)["fund_abs_moved_this_slice"] is False
        assert load(FRESHNESS)["forward_funding_regime"][
            "parameters_moved_because_of_this"] is False

    def test_the_join_is_unchanged_and_reproduces_all_three(self):
        assert load(FORWARD)["join_changed_this_slice"] is False
        assert "CLOSE" in load(FORWARD)["join_rule"]
        bars, funding, folds = _corpus()
        window = list(fb.forward_window_indices(bars, folds))
        rates = fb.funding_at_decision(bars, funding)

        def on(date):
            return next(i for i in window
                        if dt.datetime.fromtimestamp(
                            bars[i].start_ms / 1000.0,
                            tz=dt.timezone.utc).strftime("%Y-%m-%d") == date)

        assert rates[on("2026-08-12")] == 0.00006601
        assert rates[on("2026-08-17")] == 0.00009202
        assert rates[on("2026-08-18")] == 0.00003650

    def test_the_newest_miss_is_the_widest_of_the_three(self):
        """Not a narrowing sequence — which is the reassuring part."""
        table = {r["date"]: r["shortfall_vs_fund_abs"]
                 for r in load(FORWARD)["funding_setup_table"]}
        assert table["2026-08-17"] < 1e-5
        assert table["2026-08-18"] > table["2026-08-17"]
        assert round(table["2026-08-18"], 8) == 0.00006350

    def test_the_08_12_print_still_equals_fund_abs_and_still_missed(self):
        prints = load(FRESHNESS)["forward_funding_regime"][
            "prints_at_or_above_fund_abs"]
        assert [p["funding_time"] for p in prints] == \
            ["2026-08-12T00:00:00+00:00"]
        assert prints[0]["equals_fund_abs_exactly"] is True

    def test_the_reason_none_moved_cites_all_three(self):
        why = prose(load(FORWARD)["why_neither_moved"])
        for stamp in ("2026-08-12T00:00Z", "2026-08-17T16:00Z",
                      "2026-08-18T16:00Z"):
            assert stamp in why, stamp
        assert "0.00003650" in why
        assert "fitting the rule to the data" in why
        assert "void the clear" in why

    def test_the_newest_decision_cannot_be_revised_later(self):
        """A midnight print could not reach back into the bar that just closed.

        `close_time_ms` falls back to `start_ms + 86_400_000 - 1` and these
        bars carry no `end_us`, so the decision instant is 23:59:59.999Z —
        strictly before the next day's 00:00 print. Asserted from the code's
        behaviour, not from the docstring. EDGE.md §54b.
        """
        bars, _funding, _folds = _corpus()
        close = fb.close_time_ms(bars[-1])
        midnight = int(bars[-1].start_ms) + 86_400_000
        assert close < midnight
        assert close == midnight - 1
        instant = load(FRESHNESS)["forward_funding_regime"][
            "newest_decision_instant_utc"]
        assert instant.startswith("2026-08-18T23:59:59")

    def test_the_horizon_and_fingerprint_are_unchanged(self):
        assert fb.HORIZON == 5
        assert shadow.constants_fingerprint()[:32] == \
            "662de0115880871352d5d623b1020eaa"
        assert load(FORWARD)["thresholds_moved_this_slice"] is False
        assert load(FORWARD)["caps_changed_this_slice"] is False


# ===========================================================================
# 6 — the guard family, carried forward and re-run
# ===========================================================================


class TestTheGuardFamilyIsCarried:
    """The mission requires all four, by name, plus scoring equality.

    They are imported from the slices that wrote them rather than copied, so
    there is one implementation of each rule and this module cannot drift
    from it — the failure mode that left slices 67-69 without the scoring
    guard was a fresh test file that simply did not carry it.
    """

    def test_member_one_live_absolutes(self):
        from test_slice66_four_day_window import (  # noqa: PLC0415
            TestNoTestPinsALiveAbsolute as Guard)
        guard = Guard()
        offences = {os.path.basename(p): guard._offences(p)
                    for p in guard._test_files()}
        assert {k: v for k, v in offences.items() if v} == {}, json.dumps(
            offences, indent=2)

    def test_member_two_foreign_slice_constants(self):
        from test_slice69_seven_day_window import (  # noqa: PLC0415
            TestNoToolCarriesAnotherSlicesConstant as Guard)
        guard = Guard()
        offences = {os.path.basename(p): guard._offences(p)
                    for p in guard._tools()
                    if os.path.basename(p).startswith("slice71_")}
        assert {k: v for k, v in offences.items() if v} == {}, json.dumps(
            offences, indent=2)

    def test_member_three_live_counts_vs_dated_artefacts(self):
        from test_slice70_eight_day_window import (  # noqa: PLC0415
            TestNoTestEqualsALiveCountAgainstADatedArtefact as Guard)
        guard = Guard()
        offences = {os.path.basename(p): guard._offences(p)
                    for p in guard._test_files()}
        assert {k: v for k, v in offences.items() if v} == {}, json.dumps(
            offences, indent=2)

    def test_member_four_verdicts_cite_real_tests(self):
        from test_slice70_eight_day_window import (  # noqa: PLC0415
            TestAVerdictMayNotCiteATestThatDoesNotExist as Guard)
        guard = Guard()
        path = os.path.join(REPO, "STAGE1_VERDICT_SLICE71.md")
        if not os.path.isfile(path):
            return                    # written in STEP 6; checked on re-run
        with open(path, encoding="utf-8") as handle:
            lines = handle.read().splitlines()
        cited, exhibited = set(), set()
        for line in lines:
            names = set(Guard.NAME.findall(line))
            (exhibited if Guard.ANNOTATION in line else cited).update(names)
        present = guard._test_names_in_the_tree()
        assert sorted(cited - exhibited - present) == [], sorted(
            cited - exhibited - present)
        assert sorted(exhibited & present) == [], sorted(exhibited & present)

    def test_all_four_guards_still_exist_where_this_module_expects_them(self):
        """The mission's carry-forward clause, asserted rather than trusted."""
        expected = {
            "test_slice66_four_day_window": ("TestNoTestPinsALiveAbsolute",),
            "test_slice69_seven_day_window": (
                "TestNoToolCarriesAnotherSlicesConstant",),
            "test_slice70_eight_day_window": (
                "TestNoTestEqualsALiveCountAgainstADatedArtefact",
                "TestAVerdictMayNotCiteATestThatDoesNotExist"),
        }
        for module, classes in expected.items():
            with open(os.path.join(REPO, "tests", f"{module}.py"),
                      encoding="utf-8") as handle:
                tree = ast.parse(handle.read())
            defined = {n.name for n in ast.walk(tree)
                       if isinstance(n, ast.ClassDef)}
            for name in classes:
                assert name in defined, (module, name)


class TestTheScoringWasNotReTuned:
    """Carried forward from slice 70, now comparing 70 -> 71.

    Slices 67-69 shipped without this and slice 67's verdict cited it anyway
    (EDGE.md §53f). It is re-run here, and the DECLARED_CHANGES set below is
    what makes it useful: an undeclared change fails, and so does a
    declaration for something that did not change.
    """

    SUBSTITUTIONS = {
        "forward_shadow_observation/9": "forward_shadow_observation/10",
        "slice70_forward_shadow.json": "slice71_forward_shadow.json",
        "slice70_forward_shadow.log": "slice71_forward_shadow.log",
        "window_grew_since_slice69": "window_grew_since_slice70",
        "slice69_forward_shadow.json": "slice70_forward_shadow.json",
        "SLICE 70 — FORWARD SHADOW SEGMENT":
            "SLICE 71 — FORWARD SHADOW SEGMENT",
        "EDGE.md §53b, before this tool ran":
            "EDGE.md §54a, before this tool ran",
    }

    DECLARED_CHANGES = {
        "slice": "was a literal 70, now derived from SLICE. The one number "
                 "the substitution pipeline has never updated, so it is no "
                 "longer written down twice.",
        "ceiling": "the pre-declared ceiling advances 3 -> 4 and says so",
        "why_neither_moved": "now cites all THREE near misses, per the mission",
    }
    DECLARED_ADDITIONS = {}

    @classmethod
    def _split(cls, name, substitutions):
        class Normalise(ast.NodeTransformer):
            def visit_Constant(self, node):          # noqa: N802
                if isinstance(node.value, str):
                    value = node.value
                    for before, after in substitutions.items():
                        value = value.replace(before, after)
                    return ast.copy_location(ast.Constant(value), node)
                return node

        with open(os.path.join(REPO, "tools", name), encoding="utf-8") as h:
            tree = Normalise().visit(ast.parse(h.read()))
        build = next(n for n in tree.body
                     if isinstance(n, ast.FunctionDef) and n.name == "build")
        *before, final = build.body
        assert isinstance(final, ast.Return)
        before = [s for s in before
                  if not (isinstance(s, ast.Expr)
                          and isinstance(s.value, ast.Constant)
                          and isinstance(s.value.value, str))]
        payload = {k.value: ast.dump(v)
                   for k, v in zip(final.value.keys, final.value.values)}
        return [ast.dump(s) for s in before], payload

    def test_the_scoring_was_not_re_tuned_since_slice70(self):
        old_body, old_payload = self._split(
            "slice70_forward_shadow.py", self.SUBSTITUTIONS)
        new_body, new_payload = self._split(
            "slice71_forward_shadow.py", self.SUBSTITUTIONS)
        assert old_body == new_body, "the SCORING changed between slices"
        assert set(old_payload) - set(new_payload) == set(), "a key was DROPPED"
        drifted = {k for k, v in old_payload.items()
                   if new_payload.get(k) != v}
        assert drifted == set(self.DECLARED_CHANGES), sorted(drifted)
        added = set(new_payload) - set(old_payload)
        assert added == set(self.DECLARED_ADDITIONS), sorted(added)

    def test_the_guard_is_where_the_next_slice_will_look_for_it(self):
        """Slices 67-69 lost this by writing a fresh module without it."""
        with open(os.path.join(REPO, "tests",
                               "test_slice71_nine_day_window.py"),
                  encoding="utf-8") as handle:
            tree = ast.parse(handle.read())
        names = {n.name for n in ast.walk(tree)
                 if isinstance(n, ast.FunctionDef)}
        assert "test_the_scoring_was_not_re_tuned_since_slice70" in names


# ===========================================================================
# 7 — the pack, and everything frozen
# ===========================================================================


class TestThePackAndTheFreezes:

    def test_no_restoration_was_needed_again(self):
        assert not os.path.exists(os.path.join(
            REPO, "artifacts", "slice71_restored_from_slice70.json"))
        assert load(FRESHNESS)["pack_regression"][
            "occurred_this_slice"] is False

    def test_the_pack_base_claim_is_true_this_time(self):
        claim = load(FRESHNESS)["pack_base_claim"]
        assert claim["true"] is True
        assert claim["was_false_when_first_made_in_slice67"] is True

    def test_edge_carries_every_section_from_45_to_54(self):
        with open(os.path.join(REPO, "EDGE.md"), encoding="utf-8") as handle:
            edge = handle.read()
        for section in range(45, 55):
            assert f"\n## {section}a." in edge, section

    def test_the_frozen_pack_is_intact(self):
        assert shadow.SHADOW_MAX_CONCURRENT_POSITIONS == 1
        assert shadow.SHADOW_MAX_ENTRIES_PER_DAY == 1
        assert shadow.SHADOW_MAX_NOTIONAL_USD == 100.00
        assert load(FORWARD)["constants_fingerprint_matches_frozen"] is True
        assert load(FORWARD)["schedule_mode"] == "one_entry_per_contiguous_run"
        assert load(FORWARD)["caps"]["symbol"] == "BTCUSDT"

    def test_the_monitor_thresholds_are_unchanged(self):
        thresholds = shadow.MONITOR_THRESHOLDS
        assert thresholds["M1_rolling_trades"]["warn_below"] == 0.0
        assert thresholds["M1_rolling_trades"]["alert_below"] == -0.25
        assert thresholds["M2_rolling_days"]["window_days"] == 90
        assert thresholds["M3_concentration"]["warn_above"] == 0.60
        assert thresholds["M4_halves"]["min_trades"] == 20

    def test_eleven_families_stay_frozen(self):
        assert len(ps.ABSENT_SIGNALS) == 11
        assert load(FORWARD)["frozen_absent_count"] == 11
        assert fb.UNIVERSE == ("BTCUSDT",)

    def test_the_oos_clear_was_not_re_scored(self):
        assert sha256("artifacts/slice57_oos_edge_BTCUSDT_summary.json") == \
            "28b7dfe0f22c4867661bc73f6434c4acce54a0483c4469a6540155b1b3c18c51"
        assert load(FORWARD)["cleared_edge_re_scored_this_slice"] is False
        assert fb.folds_sha256() == \
            "ff5cc8a2bb92362058b376659ff12f314c10f71a101d1f581f69da31f025013c"
        assert fb.folds_are_unmodified() is True
        assert load(FRESHNESS)["folds_unmodified"] is True

    def test_no_edge_measurement_ran_this_slice(self):
        for name in sorted(os.listdir(os.path.join(REPO, "artifacts"))):
            if not name.startswith("slice71_") or not name.endswith(".json"):
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
        assert load(GATE)["items_complete_unchanged_from_slice_70"] is True

    def test_the_gate_refuses_to_credit_four_possible_trades(self):
        evidence = prose(load(GATE)["checklist"]["forward_shadow_clean"][
            "evidence"])
        assert "4 POSSIBLE TRADES DO NOT ADVANCE THIS GATE" in evidence
        assert "never the numerator of what has happened" in evidence
        assert load(GATE)["checklist"]["forward_shadow_clean"][
            "forward_trades_to_date"] == 0

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
