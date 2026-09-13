"""The same-venue corpus: Bybit spot, Bybit perp, Bybit funding, on the clock.

Promoted from the 0028 study set by tools/promote_bybit_study_set.py. These
tests pin what was promoted, prove no price was edited, and prove the open bar
the study set carried was dropped rather than written as a close.
"""
from __future__ import annotations

import csv
import gzip
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import promote_bybit_study_set as pb  # noqa: E402
import market_data as md  # noqa: E402

REPO = os.path.join(os.path.dirname(__file__), "..")
CORPUS = os.path.join(REPO, pb.OUT)

#: Content pins. If one of these moves, the corpus is not the data the
#: settlement-clock backtest was run on. Compressed bytes may differ across
#: zlib builds; CONTENT may not.
PINNED = {
    "ohlcv/BYBIT_SPOT_BTC_USDT_4H.csv.gz":
        "d8736429ccd140d31a65608d3f9ab04f429d5dc791f09c4b325bac9ecccbc3d5",
    "ohlcv/BYBIT_LINEAR_BTC_USDT_4H.csv.gz":
        "6d8ec4a20f453662455e1a1bb5b58dfb62fc3818a377bf4285b142e3975a6b19",
    "funding/BYBIT_LINEAR_BTC_USDT_FUNDING.csv.gz":
        "8fbe00f6702de1db03be5ea17375084c383bdc8ce8ddfb38f293f6932d4d9546",
}


def _rows(rel):
    with gzip.open(os.path.join(CORPUS, rel), "rt", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


class TestTheCommittedCorpus:
    def test_the_manifest_verifies(self):
        report = md.verify_manifest(CORPUS, deep=True)
        assert report.problems == [], report.problems
        assert report.synthetic is False

    def test_content_is_pinned(self):
        with open(os.path.join(CORPUS, "MANIFEST.json"), encoding="utf-8") as fh:
            files = json.load(fh)["files"]
        assert set(files) == set(PINNED)
        for rel, sha in PINNED.items():
            assert files[rel]["sha256_uncompressed"] == sha, rel

    def test_regenerating_from_the_study_set_reproduces_it(self):
        assert pb.check(REPO) == []

    def test_the_source_files_are_named_by_digest(self):
        with open(os.path.join(CORPUS, "MANIFEST.json"), encoding="utf-8") as fh:
            src = json.load(fh)["source"]["sha256_of_source_files"]
        for name in ("spot_BTCUSDT_240.json.gz", "linear_BTCUSDT_240.json.gz",
                     "funding_BTCUSDT.json.gz", "tickers_linear.json.gz"):
            assert len(src[name]) == 64


class TestTheOpenBarWasDropped:
    def test_the_last_bar_closed_before_the_study_set_was_observed(self):
        for rel in ("ohlcv/BYBIT_SPOT_BTC_USDT_4H.csv.gz",
                    "ohlcv/BYBIT_LINEAR_BTC_USDT_4H.csv.gz"):
            assert _rows(rel)[-1]["time_period_start"].startswith(
                "2026-09-09T04:00")

    def test_the_drop_is_recorded(self):
        with open(os.path.join(CORPUS, "MANIFEST.json"), encoding="utf-8") as fh:
            files = json.load(fh)["files"]
        for rel in ("ohlcv/BYBIT_SPOT_BTC_USDT_4H.csv.gz",
                    "ohlcv/BYBIT_LINEAR_BTC_USDT_4H.csv.gz"):
            assert files[rel]["open_bars_dropped"] == [
                "2026-09-09T08:00:00.0000000Z"]


class TestNoPriceWasEdited:
    def test_every_close_matches_the_study_set_byte_for_byte(self):
        for src, rel, _p in pb.KLINES:
            raw = pb._load(os.path.join(REPO, pb.STUDY, src))
            by_open = {pb.iso_utc(int(r[0])): r for r in raw}
            for row in _rows(rel):
                orig = by_open[row["time_period_start"]]
                assert (row["price_open"], row["price_high"], row["price_low"],
                        row["price_close"]) == (orig[1], orig[2], orig[3],
                                                orig[4])

    def test_every_funding_rate_matches_the_study_set(self):
        raw = pb._load(os.path.join(REPO, pb.STUDY, pb.FUNDING[0]))
        by_ms = {r["fundingRateTimestamp"]: r["fundingRate"] for r in raw}
        for row in _rows(pb.FUNDING[1]):
            assert row["funding_rate"] == by_ms[row["funding_time_ms"]]


class TestItLandsOnTheFundingClock:
    def test_both_legs_share_every_bar(self):
        spot = [r["time_period_start"] for r in _rows(pb.KLINES[0][1])]
        perp = [r["time_period_start"] for r in _rows(pb.KLINES[1][1])]
        assert spot == perp

    def test_every_print_is_on_an_8h_boundary(self):
        for row in _rows(pb.FUNDING[1]):
            assert int(row["funding_time_ms"]) % pb.FUNDING_MS == 0

    def test_every_settlement_inside_the_window_has_a_bar_ending_at_it(self):
        ends = {r["time_period_end"] for r in _rows(pb.KLINES[1][1])}
        first = _rows(pb.KLINES[1][1])[0]["time_period_end"]
        last = _rows(pb.KLINES[1][1])[-1]["time_period_end"]
        for row in _rows(pb.FUNDING[1]):
            stamp = pb.iso_utc(int(row["funding_time_ms"]))
            if first <= stamp <= last:
                assert stamp in ends, stamp


class TestWhatItRefuses:
    def _k(self, ms, px="100"):
        return [str(ms), px, px, px, px, "1", "1"]

    def test_a_gap_is_refused(self):
        with pytest.raises(pb.Refused):
            pb.kline_rows([self._k(0), self._k(2 * pb.BAR_MS)],
                          clock_ms=10 * pb.BAR_MS)

    def test_a_duplicate_is_refused(self):
        with pytest.raises(pb.Refused):
            pb.kline_rows([self._k(0), self._k(0)], clock_ms=10 * pb.BAR_MS)

    def test_an_open_bar_is_dropped_and_reported(self):
        dropped = []
        rows = pb.kline_rows([self._k(0), self._k(pb.BAR_MS)],
                             clock_ms=pb.BAR_MS + 1, dropped=dropped)
        assert len(rows) == 1
        assert dropped == [pb.iso_utc(pb.BAR_MS)]

    def test_an_off_clock_print_is_refused(self):
        with pytest.raises(pb.Refused):
            pb.funding_rows([{"symbol": "BTCUSDT", "fundingRate": "0.0001",
                              "fundingRateTimestamp": str(pb.FUNDING_MS + 16)}])

    def test_a_funding_gap_is_refused(self):
        with pytest.raises(pb.Refused):
            pb.funding_rows([
                {"symbol": "BTCUSDT", "fundingRate": "0.0001",
                 "fundingRateTimestamp": "0"},
                {"symbol": "BTCUSDT", "fundingRate": "0.0001",
                 "fundingRateTimestamp": str(2 * pb.FUNDING_MS)}])

    def test_the_build_is_deterministic(self):
        a, _ = pb.build(REPO)
        b, _ = pb.build(REPO)
        assert a == b
