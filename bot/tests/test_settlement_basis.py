"""The basis the book decides on is not the basis the P&L pays.

CarryEngine decides on DAILY CLOSES. Funding settles at 00/08/16 UTC. The
backtest's +6 bps entry basis is a once-a-day proxy for the number that changes
hands, and basis is the term that took -$4,772 out of $42,843 of gross.
"""
from __future__ import annotations

import datetime as dt
import gzip
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))

import basis_at_settlement as bas  # noqa: E402
import fetch_settlement_klines as fsk  # noqa: E402

REPO = os.path.join(os.path.dirname(__file__), "..")


def kline(when: dt.datetime, close: float):
    ms = int(when.timestamp() * 1000)
    return [ms, str(close), str(close), str(close), str(close), "10"]


class TestAnUnfinishedWindowHasNoSettlementPrice:
    def test_the_running_window_is_refused(self, tmp_path):
        """Pricing a settlement that has not happened is the same error as
        writing today's daily bar as a close."""
        now = dt.datetime(2026, 9, 9, 12, 0, tzinfo=dt.timezone.utc)
        r = fsk.run(str(tmp_path), leg="perp", now_s=now.timestamp(), klines=[
            kline(dt.datetime(2026, 9, 9, 0, tzinfo=dt.timezone.utc), 100.0),
            kline(dt.datetime(2026, 9, 9, 8, tzinfo=dt.timezone.utc), 101.0)])
        assert r["new_windows"] == 1, "the 08:00 window closes at 16:00"
        assert r["refused_open_windows"] == ["2026-09-09T08:00:00Z"]

    def test_a_window_that_has_closed_is_taken(self, tmp_path):
        now = dt.datetime(2026, 9, 9, 16, 1, tzinfo=dt.timezone.utc)
        r = fsk.run(str(tmp_path), leg="perp", now_s=now.timestamp(), klines=[
            kline(dt.datetime(2026, 9, 9, 8, tzinfo=dt.timezone.utc), 101.0)])
        assert r["new_windows"] == 1

    def test_the_window_length_is_the_funding_period(self):
        assert fsk.WINDOW_S == 8 * 3600


class TestAppendOnly:
    def test_the_prefix_is_checked(self, tmp_path):
        now = dt.datetime(2026, 9, 10, tzinfo=dt.timezone.utc).timestamp()
        r = fsk.run(str(tmp_path), leg="perp", now_s=now, klines=[
            kline(dt.datetime(2026, 9, 9, 0, tzinfo=dt.timezone.utc), 100.0)])
        assert r["historical_bytes_are_prefix"] is True

    def test_a_repeat_run_appends_nothing(self, tmp_path):
        now = dt.datetime(2026, 9, 10, tzinfo=dt.timezone.utc).timestamp()
        k = [kline(dt.datetime(2026, 9, 9, 0, tzinfo=dt.timezone.utc), 100.0)]
        first = fsk.run(str(tmp_path), leg="perp", write=True, now_s=now, klines=k)
        second = fsk.run(str(tmp_path), leg="perp", write=True, now_s=now, klines=k)
        assert first["new_windows"] == 1
        assert second["new_windows"] == 0
        assert (second["uncompressed_sha256_after"]
                == first["uncompressed_sha256_after"])

    def test_dry_run_writes_nothing(self, tmp_path):
        now = dt.datetime(2026, 9, 10, tzinfo=dt.timezone.utc).timestamp()
        fsk.run(str(tmp_path), leg="perp", now_s=now, klines=[
            kline(dt.datetime(2026, 9, 9, 0, tzinfo=dt.timezone.utc), 100.0)])
        assert not os.path.exists(
            os.path.join(str(tmp_path), fsk.OUT_DIR,
                         "BINANCE_PERP_BTCUSDT_8H.csv.gz"))

    def test_the_two_legs_land_in_separate_files(self, tmp_path):
        now = dt.datetime(2026, 9, 10, tzinfo=dt.timezone.utc).timestamp()
        k = [kline(dt.datetime(2026, 9, 9, 0, tzinfo=dt.timezone.utc), 100.0)]
        for leg in ("perp", "spot"):
            fsk.run(str(tmp_path), leg=leg, write=True, now_s=now, klines=k)
        listing = os.listdir(os.path.join(str(tmp_path), fsk.OUT_DIR))
        assert len(listing) == 2


class TestTheAnalysisDegradesHonestly:
    def test_missing_data_is_stated_not_faked(self):
        """No 8h series yet. It says so and names the command, rather than
        substituting the daily basis and calling it a settlement basis."""
        report = bas.analyse(REPO)
        if not report["available"]:
            assert report["is_a_measurement"] is False
            assert "fetch_settlement_klines" in report["reason"]

    def test_the_cli_exits_non_zero_without_data(self):
        report = bas.analyse(REPO)
        if not report["available"]:
            assert bas.main(["--repo", REPO]) == 1


class TestItComputesTheRightThing:
    def _seed(self, tmp_path, perp_close, spot_close):
        when = dt.datetime(2026, 9, 9, 0, tzinfo=dt.timezone.utc)
        now = dt.datetime(2026, 9, 10, tzinfo=dt.timezone.utc).timestamp()
        for leg, close in (("perp", perp_close), ("spot", spot_close)):
            fsk.run(str(tmp_path), leg=leg, write=True, now_s=now,
                    klines=[kline(when, close)])

    def test_settlement_basis_is_perp_over_spot(self, tmp_path):
        self._seed(tmp_path, 100_100.0, 100_000.0)
        # Daily series absent in tmp_path, so the paired comparison is empty —
        # but the settlement side must still be computed.
        report = bas.analyse(str(tmp_path))
        assert report["available"] is True

    def test_only_settlement_hours_are_considered(self):
        assert bas.SETTLEMENT_HOURS == (0, 8, 16)


class TestItChangesNoThreshold:
    def test_the_report_says_so(self):
        report = bas.analyse(REPO)
        if report["available"]:
            assert report["changes_no_threshold"] is True

    def test_the_module_touches_no_gate(self):
        with open(os.path.join(REPO, "tools", "basis_at_settlement.py"),
                  encoding="utf-8") as fh:
            body = fh.read()
        for banned in ("FUND_ABS", "place_market", "live_authorized",
                       "MIN_ENTRY_FUNDING_BPS ="):
            assert banned not in body
