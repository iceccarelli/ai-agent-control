"""Spot is half the basis and had no append-only guarantee.

fetch_binance_klines REWROTE the whole file every run. Three consecutive runs
against unchanged history produced three different digests, because the last row
is TODAY and its OHLC moves every time you ask.
"""
from __future__ import annotations

import datetime as dt
import gzip
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))

import append_spot_corpus as asc  # noqa: E402

TODAY = dt.date(2026, 9, 9)


def kline(day: dt.date, price: float = 100_000.0):
    ms = int(dt.datetime.combine(day, dt.time(),
                                 dt.timezone.utc).timestamp() * 1000)
    return [ms, price, price, price, price, "1.0", 0, 0, "100"]


@pytest.fixture
def repo(tmp_path):
    path = tmp_path / asc.SPOT_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [asc.to_row(kline(dt.date(2026, 9, 1) + dt.timedelta(days=i)))
            for i in range(5)]                     # 09-01 .. 09-05
    with gzip.open(path, "wb") as fh:
        fh.write(asc.encode(rows))
    return str(tmp_path)


class TestTodayIsNeverWritten:
    def test_the_open_day_is_refused(self, repo):
        r = asc.run(repo, today=TODAY,
                    klines=[kline(dt.date(2026, 9, 6)), kline(TODAY)])
        assert r["refused_open_day"] == str(TODAY)
        assert str(TODAY) not in r["new_closed_days"]

    def test_closed_days_are_appended(self, repo):
        r = asc.run(repo, today=TODAY, klines=[kline(dt.date(2026, 9, 6)),
                                               kline(dt.date(2026, 9, 7))])
        assert r["new_closed_days"] == ["2026-09-06", "2026-09-07"]


class TestHistoryIsNeverRewritten:
    def test_a_day_already_present_is_refused(self, repo):
        """Even if the venue now reports it differently. A corpus that updates
        its own past is a cache."""
        r = asc.run(repo, today=TODAY,
                    klines=[kline(dt.date(2026, 9, 3), price=999.0)])
        assert r["refused_already_present"] == 1
        assert not r["new_closed_days"]

    def test_the_prefix_is_checked(self, repo):
        r = asc.run(repo, today=TODAY, klines=[kline(dt.date(2026, 9, 6))])
        assert r["historical_bytes_are_prefix"] is True

    def test_appending_nothing_leaves_the_digest_unchanged(self, repo):
        r = asc.run(repo, today=TODAY, klines=[kline(TODAY)])
        assert (r["uncompressed_sha256_before"]
                == r["uncompressed_sha256_after"])

    def test_a_second_identical_run_is_a_no_op(self, repo):
        first = asc.run(repo, today=TODAY, write=True,
                        klines=[kline(dt.date(2026, 9, 6))])
        second = asc.run(repo, today=TODAY, write=True,
                         klines=[kline(dt.date(2026, 9, 6))])
        assert first.get("written") is True
        assert not second["new_closed_days"]
        assert (second["uncompressed_sha256_before"]
                == first["uncompressed_sha256_after"]), \
            "a repeat run changed the file; that is the bug this replaces"


class TestWriting:
    def test_dry_run_writes_nothing(self, repo):
        before = open(os.path.join(repo, asc.SPOT_PATH), "rb").read()
        asc.run(repo, today=TODAY, klines=[kline(dt.date(2026, 9, 6))])
        assert open(os.path.join(repo, asc.SPOT_PATH), "rb").read() == before

    def test_write_persists_and_verifies_what_landed(self, repo):
        r = asc.run(repo, today=TODAY, write=True,
                    klines=[kline(dt.date(2026, 9, 6))])
        assert r["written"] is True
        rows = asc.read_rows(os.path.join(repo, asc.SPOT_PATH))
        assert rows[-1]["time_period_start"][:10] == "2026-09-06"
        assert len(rows) == r["rows_after"]

    def test_no_backup_is_left_behind(self, repo):
        asc.run(repo, today=TODAY, write=True,
                klines=[kline(dt.date(2026, 9, 6))])
        assert not os.path.exists(os.path.join(repo, asc.SPOT_PATH + ".bak"))


class TestItFabricatesNothing:
    def test_bars_fabricated_is_always_zero(self, repo):
        assert asc.run(repo, today=TODAY,
                       klines=[kline(dt.date(2026, 9, 6))])["bars_fabricated"] == 0

    def test_an_empty_venue_response_appends_nothing(self, repo):
        r = asc.run(repo, today=TODAY, klines=[])
        assert not r["new_closed_days"]
        assert r["historical_bytes_are_prefix"] is True
