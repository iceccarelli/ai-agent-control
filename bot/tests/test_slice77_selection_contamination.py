"""Slice 77 — the invariant that would have caught the slice-57 selection path.

Every existing guard asks whether a cut was DECLARED before scoring. None asks
whether the bytes had already been READ. These tests pin the new one.
"""
from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import purged_cv  # noqa: E402
import reserved_holdout as rh  # noqa: E402

REPO = os.path.join(os.path.dirname(__file__), "..")


def _seeded():
    ledger = {"schema": rh.SCHEMA, "seeded": False, "reads": []}
    rh.seed(ledger)
    return ledger


class TestTheSlice57WindowWasAlreadyRead:
    def test_the_oos_window_is_refused(self):
        """[t_mid, t1] is a subset of the range slice 55 read end to end."""
        report = rh.certify_untouched(
            _seeded(), dataset="BINANCE_LINEAR_BTC_USDT_1D",
            from_utc="2024-08-09T00:00:00Z", to_utc="2026-08-09T00:00:00Z")
        assert report["certified_untouched"] is False
        assert report["verdict"] == "ALREADY_READ"

    def test_it_names_the_slice55_read_that_selected_btc(self):
        report = rh.certify_untouched(
            _seeded(), dataset="BINANCE_LINEAR_BTC_USDT_1D",
            from_utc="2024-08-09T00:00:00Z", to_utc="2026-08-09T00:00:00Z")
        who = " ".join(r["read_by"] for r in report["overlapping_reads"])
        assert "slice55" in who, "the selecting read must be named, not merely counted"

    def test_a_genuinely_future_window_is_clean(self):
        """After the corpus ends, nothing has read anything. That is a holdout."""
        report = rh.certify_untouched(
            _seeded(), dataset="BINANCE_LINEAR_BTC_USDT_1D",
            from_utc="2026-09-01T00:00:00Z", to_utc="2027-03-01T00:00:00Z")
        assert report["certified_untouched"] is True
        assert report["verdict"] == "RESERVED_CLEAN"

    def test_a_negative_prior_read_contaminates_too(self):
        """SOL read -0.1658 and that still counts: it steered the next hypothesis."""
        report = rh.certify_untouched(
            _seeded(), dataset="BINANCE_LINEAR_SOL_USDT_1D",
            from_utc="2023-01-01T00:00:00Z", to_utc="2024-01-01T00:00:00Z")
        assert report["certified_untouched"] is False


class TestTheLedgerIsAppendOnly:
    def test_seeding_twice_adds_nothing(self):
        ledger = _seeded()
        before = len(ledger["reads"])
        assert rh.seed(ledger) == 0
        assert len(ledger["reads"]) == before

    def test_recording_never_removes(self):
        ledger = _seeded()
        before = len(ledger["reads"])
        rh.record_read(ledger, dataset="X", from_utc="2026-01-01T00:00:00Z",
                       to_utc="2026-02-01T00:00:00Z", read_by="test")
        assert len(ledger["reads"]) == before + 1

    def test_a_bad_timestamp_is_refused(self):
        with pytest.raises(ValueError):
            rh.record_read(_seeded(), dataset="X", from_utc="not-a-time",
                           to_utc="2026-02-01T00:00:00Z", read_by="test")


class TestPurgedCombinatorialCV:
    def test_six_choose_two_gives_fifteen_paths(self):
        paths = list(purged_cv.cpcv_paths(1476, n_groups=6, n_test_groups=2))
        assert len(paths) == 15, "one OOS number becomes fifteen"

    def test_train_and_test_never_intersect(self):
        for path in purged_cv.cpcv_paths(400, n_groups=5, n_test_groups=2):
            assert not (set(path["train"]) & set(path["test"]))

    def test_every_path_drops_rows_to_purge_and_embargo(self):
        report = purged_cv.summarise(1476)
        assert report["leakage_guard_is_active"] is True
        assert report["mean_dropped_to_leakage"] > 0

    def test_purge_removes_labels_that_reach_into_the_test_block(self):
        """A label at t resolves using bars to t+HORIZON, so t may not train."""
        train = purged_cv.purge_and_embargo(100, list(range(50, 60)), horizon=5)
        assert 46 not in train, "46+5 = 51 lands inside the test block"
        assert 44 in train, "44+5 = 49 stops before it"

    def test_embargo_removes_rows_just_after_the_test_block(self):
        train = purged_cv.purge_and_embargo(100, list(range(50, 60)),
                                            horizon=5, embargo=6)
        assert all(i not in train for i in range(60, 66))
        assert 66 in train

    def test_purge_width_matches_the_frozen_horizon(self):
        assert purged_cv.HORIZON == 5
        assert purged_cv.LOCKUP == 1


class TestTheseToolsScoreNothing:
    def test_cpcv_declares_it_is_not_stage1_evidence(self):
        report = purged_cv.summarise(500)
        assert report["is_stage1_evidence"] is False
        assert report["re_scored_anything"] is False

    def test_neither_tool_can_reach_live_authorized(self):
        for name in ("purged_cv.py", "reserved_holdout.py"):
            with open(os.path.join(REPO, "tools", name), encoding="utf-8") as fh:
                body = fh.read()
            assert "live_authorized = True" not in body
            assert "LIVE_TRADING_ACK" not in body


class TestTheFreezeNoteExistsAndIsUnsigned:
    NOTE = os.path.join(
        REPO, "docs", "human",
        "FREEZE_NOTE_SLICE77_funding_carry_fade_btc_v1.md")

    def test_the_note_exists(self):
        assert os.path.isfile(self.NOTE)

    def test_the_note_is_not_yet_signed(self):
        """Demotion on STATISTICAL grounds is a judgement and takes a signature.

        When a human fills in the name line, this test is expected to be updated
        in the SAME commit that flips project_status. Until then the freeze is a
        proposal, and project_status must still report the old cleared signal.
        """
        with open(self.NOTE, encoding="utf-8") as handle:
            body = handle.read()
        assert "  name:\n" in body, "signature block missing or already filled"

    def test_the_note_refuses_to_freeze_on_n_equals_2(self):
        with open(self.NOTE, encoding="utf-8") as handle:
            body = handle.read()
        assert "I am not freezing on n=2" in body
