"""The financing series, and the two ways a fetcher lies about its own depth.

WHY
===
`borrow_apr` is a CONSTANT at every call site and the real USDT rate moved
1.00% to 5.00%/yr across half-years (INVENTORY D43). A per-period series is the
only way the 3/5/8% matrix columns stop being decoration.

TWO FAILURES THIS PINS, BOTH OF WHICH ALREADY HAPPENED TO ME
============================================================
1. A WALK THAT STOPS AT ITS OWN CAP IS NOT A WALK THAT REACHED THE END. A
   400-page probe returned exactly 40,000 rows -- 400 x 100 -- and reported a
   span starting 2022-02-22. That was the cap, not the data: targeted probes
   found rows back to 2021-12-14. `hit_page_cap` is therefore reported
   separately from `reached_the_venue_floor`, and a capped run says so.

2. A SAMPLE TOO THIN TO BE A MEDIAN, PRESENTED AS ONE. The first half-year
   medians recorded for D43 came from ONE 100-row page per half-year and were
   wrong by more than 3x in 2023H2 (10.00% against a true 3.00%). Not a thing a
   test can catch, but it is why this fetcher takes EVERY observation rather
   than sampling.

WHAT THE NUMBER IS
==================
OKX's public savings LENDING rate: what a lender earns. A borrower pays more,
so every value is a FLOOR on the cost of financing. A book that fails to clear
its costs against these rates fails harder against real ones.
"""
from __future__ import annotations

import datetime as dt
import gzip
import json
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "tools"))

import fetch_borrow_rates as fbr  # noqa: E402

HOUR_MS = 3_600_000


def page(start_ms: int, n: int, rate: str = "0.03", ccy: str = "USDT"):
    """`n` hourly rows ending at `start_ms`, newest first, as OKX serves them."""
    return [{"ts": str(start_ms - i * HOUR_MS), "ccy": ccy,
             "rate": rate, "lendingRate": rate, "amt": "1000"}
            for i in range(n)]


def pager_over(total_rows: int, newest_ms: int):
    """A venue with exactly `total_rows` hourly observations."""
    oldest = newest_ms - (total_rows - 1) * HOUR_MS

    def call(params):
        after = params.get("after")
        top = newest_ms if after is None else int(after) - HOUR_MS
        if top < oldest:
            return []
        n = min(fbr.PAGE, int((top - oldest) // HOUR_MS) + 1)
        return page(top, n, ccy=params["ccy"])
    return call


NOW = int(dt.datetime(2026, 9, 16, tzinfo=dt.timezone.utc).timestamp() * 1000)


class TestItReportsItsOwnDepthHonestly:
    def test_a_completed_walk_does_not_claim_a_cap(self):
        rows, hit_cap = fbr.fetch("USDT", 0, pager=pager_over(2_500, NOW))
        assert len(rows) == 2_500
        assert hit_cap is False

    def test_a_capped_walk_says_so(self):
        """The 40,000-row mistake, as a test: a series longer than the walk
        can carry must never look like a series that ended.

        The cap is INJECTED rather than exercised at its real value of 600.
        Driving 600 synthetic pages and validating 60,000 rows took these two
        tests 151 seconds, on a file that never touches the network — a 2.5
        minute tax on every full-suite gate, for a property that holds
        identically at cap=3. The behaviour under test is "stopped early, and
        said so", not the size of the number.
        """
        rows, hit_cap = fbr.fetch("USDT", 0, pager=pager_over(10_000, NOW),
                                  max_pages=3)
        assert hit_cap is True
        assert len(rows) == fbr.PAGE * 3

    def test_the_report_carries_a_warning_when_capped(self):
        report = fbr.run(".", ccy="USDT", since="2000-01-01",
                         pager=pager_over(10_000, NOW), max_pages=12)
        assert report["hit_page_cap"] is True
        assert "cap" in report["warning"]

    def test_the_real_cap_can_reach_the_venue_floor(self):
        """cap x page must span 2021-12-14 to now with room to spare, or the
        default would silently truncate the corpus — which is the failure the
        injected cap above must not hide."""
        hours = (dt.datetime(2026, 9, 16, tzinfo=dt.timezone.utc)
                 - dt.datetime(2021, 12, 14, tzinfo=dt.timezone.utc)
                 ).total_seconds() / 3600.0
        assert fbr.MAX_PAGES * fbr.PAGE > hours

    def test_it_stops_at_the_floor_it_was_given(self):
        floor = NOW - 500 * HOUR_MS
        rows, hit_cap = fbr.fetch("USDT", floor, pager=pager_over(50_000, NOW))
        assert hit_cap is False
        assert int(rows[0]["ts"]) <= floor + fbr.PAGE * HOUR_MS

    def test_the_venue_floor_is_a_measured_constant(self):
        """2021-12-14, established by probe: `after` at 2021-12-16 returns 39
        rows and every earlier stamp returns zero."""
        assert fbr.VENUE_FLOOR_UTC == "2021-12-14"


class TestItRefusesASeriesItWouldNotCharge:
    def test_a_short_series_is_refused(self):
        with pytest.raises(fbr.BorrowFetchError):
            fbr.run(".", ccy="USDT", pager=pager_over(50, NOW))

    def test_duplicate_timestamps_are_refused(self):
        rows = [{"ts": "1000", "utc": "x", "ccy": "USDT",
                 "rate_annual": "0.03", "lending_rate": "", "amount": ""}] * 2000
        with pytest.raises(fbr.BorrowFetchError):
            fbr.validate(rows, "USDT")

    def test_non_monotonic_timestamps_are_refused(self):
        rows = [{"ts": str(2000 - i), "utc": "x", "ccy": "USDT",
                 "rate_annual": "0.03", "lending_rate": "", "amount": ""}
                for i in range(2000)]
        with pytest.raises(fbr.BorrowFetchError):
            fbr.validate(rows, "USDT")

    def test_a_negative_rate_is_refused(self):
        rows = [{"ts": str(i), "utc": "x", "ccy": "USDT",
                 "rate_annual": "0.03", "lending_rate": "", "amount": ""}
                for i in range(2000)]
        rows[7]["rate_annual"] = "-0.01"
        with pytest.raises(fbr.BorrowFetchError):
            fbr.validate(rows, "USDT")


class TestADryRunWritesNothing:
    def test_no_file_and_no_manifest(self, tmp_path):
        report = fbr.run(str(tmp_path), ccy="USDT",
                         pager=pager_over(2_500, NOW))
        assert report["write"] is False
        assert "written" not in report
        assert not os.path.exists(os.path.join(str(tmp_path), fbr.OUT_DIR))


class TestNoCorpusWithoutProvenance:
    def test_a_write_produces_a_manifest(self, tmp_path):
        report = fbr.run(str(tmp_path), ccy="USDT", write=True,
                         pager=pager_over(2_500, NOW))
        assert report["written"] is True
        assert os.path.exists(report["manifest"])

    def test_the_manifest_declares_itself_real(self, tmp_path):
        fbr.run(str(tmp_path), ccy="USDT", write=True,
                pager=pager_over(2_500, NOW))
        with open(os.path.join(str(tmp_path), fbr.OUT_DIR, "MANIFEST.json"),
                  encoding="utf-8") as handle:
            manifest = json.load(handle)
        assert manifest["synthetic"] is False
        assert manifest["venue"] == "okx"
        assert manifest["endpoint"] == fbr.URL

    def test_it_verifies_against_market_datas_checker(self, tmp_path):
        import market_data as md
        fbr.run(str(tmp_path), ccy="USDT", write=True,
                pager=pager_over(2_500, NOW))
        report = md.verify_manifest(
            os.path.join(str(tmp_path), fbr.OUT_DIR))
        assert report.ok, report.problems
        assert report.synthetic is False

    def test_the_manifest_records_the_venue_floor(self, tmp_path):
        """So nobody later reads a 2021-12 start as a fetch that gave up."""
        fbr.run(str(tmp_path), ccy="USDT", write=True,
                pager=pager_over(2_500, NOW))
        with open(os.path.join(str(tmp_path), fbr.OUT_DIR, "MANIFEST.json"),
                  encoding="utf-8") as handle:
            manifest = json.load(handle)
        assert manifest["venue_floor_utc"] == fbr.VENUE_FLOOR_UTC


class TestItSaysWhatTheNumberIs:
    def test_the_report_names_it_a_floor_not_a_cost(self, tmp_path):
        """A LENDING rate quoted as a borrowing cost is a cost understated."""
        report = fbr.run(str(tmp_path), ccy="USDT",
                         pager=pager_over(2_500, NOW))
        assert "FLOOR" in report["what_this_is"]
        assert "LENDING" in report["what_this_is"]

    def test_the_manifest_says_it_too(self, tmp_path):
        fbr.run(str(tmp_path), ccy="USDT", write=True,
                pager=pager_over(2_500, NOW))
        with open(os.path.join(str(tmp_path), fbr.OUT_DIR, "MANIFEST.json"),
                  encoding="utf-8") as handle:
            notes = json.load(handle)["notes"]
        assert "FLOOR" in notes
        assert "2021-12-14" in notes

    def test_the_written_rows_round_trip(self, tmp_path):
        import csv as _csv
        import io as _io
        fbr.run(str(tmp_path), ccy="USDT", write=True,
                pager=pager_over(2_500, NOW))
        path = os.path.join(str(tmp_path), fbr.OUT_DIR,
                            "OKX_LENDING_RATE_USDT_1H.csv.gz")
        with gzip.open(path, "rt") as handle:
            rows = list(_csv.DictReader(handle))
        assert len(rows) == 2_500
        assert list(rows[0]) == list(fbr.COLUMNS)
        stamps = [int(r["ts"]) for r in rows]
        assert stamps == sorted(stamps)


class TestARefreshMustExtendNotReplace:
    """This fetcher re-walks the WHOLE series from `since` every run rather
    than resuming from the last row (OKX serves complete history cheaply,
    unlike Binance). That means a bad page, a venue hiccup, or a botched
    `--since` produces a walk that is SHORTER than what is already on disk
    — and with no prefix check, `run()` would silently overwrite good
    history with less of it. This is the same append-only guarantee
    append_closed_corpus and fetch_settlement_klines already have."""

    def test_a_normal_extension_reports_the_prefix_holds(self, tmp_path):
        fbr.run(str(tmp_path), ccy="USDT", write=True,
               pager=pager_over(2_500, NOW))
        later = NOW + 500 * HOUR_MS
        report = fbr.run(str(tmp_path), ccy="USDT", write=True,
                         pager=pager_over(3_000, later))
        assert report["historical_bytes_are_prefix"] is True
        assert report["written"] is True

    def test_a_shorter_walk_is_refused_not_written(self, tmp_path):
        fbr.run(str(tmp_path), ccy="USDT", write=True,
               pager=pager_over(2_500, NOW))
        path = os.path.join(str(tmp_path), fbr.OUT_DIR,
                            "OKX_LENDING_RATE_USDT_1H.csv.gz")
        before = open(path, "rb").read()

        report = fbr.run(str(tmp_path), ccy="USDT", write=True,
                         pager=pager_over(1_000, NOW))
        assert report["historical_bytes_are_prefix"] is False
        assert "error" in report
        assert "written" not in report
        assert open(path, "rb").read() == before, (
            "a shorter walk must never touch the file already on disk")

    def test_no_existing_file_is_trivially_a_prefix(self, tmp_path):
        report = fbr.run(str(tmp_path), ccy="USDT",
                         pager=pager_over(2_500, NOW))
        assert report["historical_bytes_are_prefix"] is True
