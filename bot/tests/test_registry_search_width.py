"""The registry says how wide the search was, without inflating it or hiding it.

Two opposite errors were in circulation. `n_trials_recorded` (36) counts the three
rule sweeps as THREE trials, which understates the search. The raw sum of their
counts (about 22,500) overstates it: the gated rule never reads `entry_bps`, so
the first sweep's 1,440 cells are 60 distinct rules printed 24 times, and the
walk-forward's 20,520 is the same 540-rule space SCORED in 38 refit windows, not
20,520 different rules. The number a multiple-testing correction should be given
is the count of distinct rules, and that is what the registry now states.

Also pinned: the real-market protocol is itself a recorded entry, tied by hash to
the code, so changing that protocol cannot happen without a new registry entry.
"""
from __future__ import annotations

import json
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
BOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(BOT, "tools"))
sys.path.insert(0, BOT)

import hypothesis_registry as hr  # noqa: E402
import mainnet_public as MP  # noqa: E402

SWEEPS = ["carry_rule_sweep@59feb30d2328", "carry_rule_sweep@d7d7d4141a05",
          "carry_walkforward@69b08ab2ab84"]


@pytest.fixture(scope="module")
def registry():
    return hr.load()


def by_id(registry):
    return {t["trial_id"]: t for t in registry["trials"]}


class TestTheStructuredWidth:
    def test_the_block_names_the_three_sweeps_that_exist(self, registry):
        sw = registry["search_width"]
        assert sw["sweep_trial_ids"] == SWEEPS
        assert set(SWEEPS) <= set(by_id(registry))

    def test_the_distinct_rule_count_is_the_conservative_sum_of_distinct_spaces(self, registry):
        sw = registry["search_width"]
        b = sw["breakdown"]
        assert b["first_sweep_distinct_rules"] == 60
        assert b["second_sweep_and_walkforward_rule_space"] == 540
        assert sw["distinct_gated_rules_scored"] == 600
        assert sw["distinct_gated_rules_scored"] == sum(
            v for v in b.values() if isinstance(v, int))

    def test_it_says_why_the_raw_sum_is_wrong_and_forbids_quoting_it(self, registry):
        sw = registry["search_width"]
        text = json.dumps(sw)
        assert "entry_bps" in text and "24 times" in text and "38 refit" in text
        assert "double" in sw["do_not_use"].lower() and "22,500" in sw["do_not_use"]

    def test_it_still_says_this_is_a_floor_and_the_effective_n_is_unestimated(self, registry):
        t = registry["search_width"]["still_a_floor"].lower()
        assert "abandoned" in t and "correlated" in t and "nothing here estimates" in t

    def test_the_older_prose_note_is_untouched(self, registry):
        assert registry["width_note_slice77"].startswith("Family-level counting")


class TestTheSummary:
    def test_the_working_width_replaces_three_sweeps_by_their_distinct_rules(self, registry):
        s = hr.summary(registry)
        n = len(registry["trials"])
        assert s["search_width"]["n_recorded"] == n
        assert s["search_width"]["n_working"] == (n - 3) + 600

    def test_the_working_width_grows_with_every_new_trial_and_only_that(self, registry):
        before = hr.summary(registry)["search_width"]["n_working"]
        r = json.loads(json.dumps(registry))
        hr.record(r, "a_new_trial_for_this_test", status="tested", note="x")
        assert hr.summary(r)["search_width"]["n_working"] == before + 1

    def test_the_correction_is_at_least_as_harsh_as_the_recorded_count_says(self, registry):
        sw = hr.summary(registry)["search_width"]
        s = hr.summary(registry)
        assert sw["family_wise_false_positive_rate_working"] >= s["family_wise_false_positive_rate"]
        assert sw["expected_max_sharpe_under_null_working"] >= s["expected_max_sharpe_under_null"]

    def test_the_reading_never_quotes_the_double_counted_sum(self, registry):
        s = json.dumps(hr.summary(registry))
        assert "22500" not in s and "22,500" not in s and "20520" not in s

    def test_the_reading_says_a_single_in_sample_clear_is_not_evidence(self, registry):
        r = hr.summary(registry)["search_width"]["reading"].lower()
        assert "not evidence of edge" in r and "distinct" in r
        assert "correlated" in r and "not estimated" in r and "harsh end" in r

    def test_a_registry_without_the_block_summarises_exactly_as_before(self):
        r = {"trials": []}
        for i in range(12):
            hr.record(r, f"t{i}", status="frozen_absent")
        s = hr.summary(r)
        assert "search_width" not in s and s["n_trials_recorded"] == 12

    def test_a_block_whose_sweeps_are_absent_does_not_invent_a_discount(self):
        r = {"trials": [], "search_width": {"sweep_trial_ids": ["gone"],
                                            "distinct_gated_rules_scored": 600}}
        for i in range(5):
            hr.record(r, f"t{i}", status="tested")
        s = hr.summary(r)["search_width"]
        assert s["n_working"] == 5 + 600           # nothing to replace: add, never subtract


class TestTheRealMarketProtocolIsOnTheRecord:
    def trial(self, registry):
        return by_id(registry)["mainnet_public_protocol_v1"]

    def test_it_is_recorded_as_declared_and_not_yet_evaluated(self, registry):
        t = self.trial(registry)
        assert t["status"] == "protocol_declared"
        assert "not yet evaluated" in t["note"].lower()

    def test_it_is_tied_by_hash_to_the_code(self, registry):
        assert self.trial(registry)["protocol_sha256"] == MP.protocol_hash()

    def test_changing_the_protocol_would_need_a_new_entry(self, registry, monkeypatch):
        monkeypatch.setitem(MP.MAINNET_PROTOCOL["thresholds"], "rarely_open_below", 0.01)
        assert self.trial(registry)["protocol_sha256"] != MP.protocol_hash()

    def test_it_makes_no_claim_it_cannot_support(self, registry):
        note = self.trial(registry)["note"].lower()
        for banned in ("edge established", "profitable", "validated"):
            assert banned not in note
        assert "not a profit claim" in note
