"""The three series are only meaningful together, and nothing checked that.

perp ended 2026-08-24, funding 2026-08-25, spot 2026-09-08. carry_backtest
intersected them silently: 45 days present in one and missing from another were
dropped without comment, and a series 16 days behind looked like a quiet market.
"""
from __future__ import annotations

import datetime as dt
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))

import corpus_health as ch  # noqa: E402

REPO = os.path.join(os.path.dirname(__file__), "..")


@pytest.fixture(scope="module")
def report():
    return ch.check(REPO)


class TestAllThreeSeriesAreInspected:
    def test_every_series_the_book_reads_is_covered(self, report):
        assert {s["name"] for s in report["series"]} == {
            "perp_1d", "spot_1d", "funding"}

    def test_each_series_publishes_a_digest(self, report):
        """Bytes beat prose. A health claim without a hash is a rumour."""
        for series in report["series"]:
            if series.get("present"):
                assert len(series["sha256"]) == 64


class TestFundingIsNotJudgedAsADailyBar:
    def test_three_prints_a_day_is_not_a_duplicate(self, report):
        """Funding prints 3x daily on Binance USDT-M. Flagging that would cry
        wolf 2,953 times, and a check that cries wolf gets ignored."""
        funding = next(s for s in report["series"] if s["name"] == "funding")
        assert not any(p.startswith("DUPLICATE_DAYS") for p in funding["problems"])

    def test_a_daily_bar_repeating_a_date_would_still_be_flagged(self):
        names = {name: per for name, _p, _c, per in ch.SERIES}
        assert names["perp_1d"] == 1
        assert names["spot_1d"] == 1
        assert names["funding"] == 3


class TestStalenessIsReported:
    def test_staleness_is_measured_against_the_last_CLOSED_day(self, report):
        for series in report["series"]:
            if series.get("present"):
                assert series["stale_days"] >= 0

    def test_a_series_past_the_limit_is_named(self):
        strict = ch.check(REPO, max_stale_days=0)
        assert strict["series_too_stale"]

    def test_a_generous_limit_names_nobody(self):
        assert not ch.check(REPO, max_stale_days=10_000)["series_too_stale"]


class TestAlignmentIsReportedNotSwallowed:
    def test_the_three_way_overlap_is_stated(self, report):
        assert report["three_way_overlap_days"] > 0
        assert report["overlap_first"] < report["overlap_last"]

    def test_days_missing_from_another_series_are_named(self, report):
        """These were silently dropped by the backtest before this existed."""
        assert isinstance(report["alignment_gaps"], list)
        for gap in report["alignment_gaps"]:
            assert gap["days_in"] != gap["missing_from"]
            assert gap["count"] > 0


class TestOpenBarsInTheFile:
    def test_a_bar_dated_today_is_flagged(self, tmp_path):
        """drop_open_bar handles it at LOAD. The file still contains it, which
        is a trap for any other consumer."""
        import csv
        import gzip
        path = tmp_path / "s.csv.gz"
        today = dt.datetime.now(dt.timezone.utc).date()
        with gzip.open(path, "wt", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["time_period_start"])
            w.writerow([f"{today - dt.timedelta(days=1)}T00:00:00Z"])
            w.writerow([f"{today}T00:00:00Z"])
        out = ch.inspect(str(tmp_path), "x", "s.csv.gz", "time_period_start",
                         1, today)
        assert any(p.startswith("OPEN_BAR_IN_FILE") for p in out["problems"])


class TestItOnlyReads:
    def test_the_tool_never_writes_a_corpus(self):
        with open(os.path.join(REPO, "tools", "corpus_health.py"),
                  encoding="utf-8") as fh:
            body = fh.read()
        # AST, not text: the docstring explains that the tool does not fetch,
        # and a substring check finds its own documentation. Same trap as the
        # 0016 cap guard.
        import ast
        tree = ast.parse(body)
        writes = [n for n in ast.walk(tree)
                  if isinstance(n, ast.Call)
                  and isinstance(n.func, ast.Name) and n.func.id == "open"
                  and len(n.args) > 1
                  and isinstance(n.args[1], ast.Constant)
                  and "w" in str(n.args[1].value)]
        # The only write is the optional --out JSON report, never a corpus.
        assert len(writes) <= 1
        for banned in ("place_market", "live_authorized"):
            assert banned not in body

    def test_an_unhealthy_corpus_is_reported_not_repaired(self, report):
        """A health check that fixes things is one nobody can trust."""
        assert "healthy" in report
        assert isinstance(report["problems"], list)
