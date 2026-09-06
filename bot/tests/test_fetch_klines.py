"""Slice 33 — the acquisition tool's refusals, tested without a network.

`tools/fetch_binance_klines.py` could not be run against a live endpoint from
this environment (`EDGE.md` §14c: the egress proxy answers CONNECT with 403 and
`WebFetch` is refused by robots.txt). What *can* be tested — and what actually
decides whether a written corpus is honest — is everything except the socket:
normalisation, validation, pagination, and the provenance record.

So the pager is injected. Every test below drives the real code paths with
scripted pages, including hostile ones.

The bias throughout is toward **refusing to write**. A missing corpus is a
visible problem. A corpus with forward-filled gaps, a silently truncated range,
or a `synthetic: false` manifest over generated bars is an invisible one, and it
would poison every measurement taken against it afterwards.
"""
from __future__ import annotations

import gzip
import json
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "tools"))

import data_contract as dc  # noqa: E402
import fetch_binance_klines as fk  # noqa: E402

DAY_MS = 86_400_000
START = 1_600_000_000_000 - (1_600_000_000_000 % DAY_MS)


def kline(open_ms, *, o=100.0, h=110.0, low=90.0, c=105.0, v=12.5, trades=42):
    """One Binance-shaped kline entry."""
    return [open_ms, str(o), str(h), str(low), str(c), str(v),
            open_ms + DAY_MS - 1, "0", trades, "0", "0", "0"]


def pages_of(count, *, start=START, step=DAY_MS):
    """A pager that serves ``count`` well-formed daily bars, then nothing."""
    everything = [kline(start + i * step) for i in range(count)]

    def pager(_url, params):
        cursor = int(params["startTime"])
        end = int(params["endTime"])
        limit = int(params["limit"])
        window = [k for k in everything if cursor <= k[0] < end]
        return window[:limit]

    return pager


class TestNormalisation:

    def test_a_page_becomes_repository_rows(self):
        rows = fk.normalise_page([kline(START)], interval_seconds=86400)
        assert len(rows) == 1
        row = rows[0]
        assert set(row) == set(fk.COLUMNS)
        assert row["time_period_start"].endswith("Z")
        assert row["price_open"] == 100.0
        assert row["price_high"] == 110.0
        assert row["trades_count"] == 42

    def test_timestamps_are_utc_in_the_repository_format(self):
        row = fk.normalise_page([kline(START)], interval_seconds=86400)[0]
        assert row["time_period_start"] == fk.iso_utc(START)
        assert ".0000000Z" in row["time_period_start"]

    def test_the_period_end_is_one_interval_after_the_start(self):
        rows = fk.normalise_page([kline(START)], interval_seconds=86400)
        assert rows[0]["time_period_end"] == fk.iso_utc(START + DAY_MS)

    @pytest.mark.parametrize("bad", ["nan", "inf", "-inf"])
    def test_a_non_finite_price_is_refused(self, bad):
        with pytest.raises(fk.KlineFetchError):
            fk.normalise_page([kline(START, o=float(bad))],
                              interval_seconds=86400)

    @pytest.mark.parametrize("price", [0.0, -1.0])
    def test_a_non_positive_price_is_refused(self, price):
        with pytest.raises(fk.KlineFetchError):
            fk.normalise_page([kline(START, c=price)], interval_seconds=86400)

    def test_high_below_low_is_refused(self):
        with pytest.raises(fk.KlineFetchError, match="below low"):
            fk.normalise_page([kline(START, h=50.0, low=90.0, o=60.0, c=60.0)],
                              interval_seconds=86400)

    def test_an_open_outside_the_bar_range_is_refused(self):
        with pytest.raises(fk.KlineFetchError, match="open outside"):
            fk.normalise_page([kline(START, o=999.0)], interval_seconds=86400)

    def test_a_close_outside_the_bar_range_is_refused(self):
        with pytest.raises(fk.KlineFetchError, match="close outside"):
            fk.normalise_page([kline(START, c=999.0)], interval_seconds=86400)

    def test_a_negative_volume_is_refused(self):
        with pytest.raises(fk.KlineFetchError, match="volume"):
            fk.normalise_page([kline(START, v=-1.0)], interval_seconds=86400)

    def test_a_truncated_kline_is_refused(self):
        with pytest.raises(fk.KlineFetchError, match="malformed"):
            fk.normalise_page([[START, "1", "2"]], interval_seconds=86400)


class TestValidation:
    """The checks that stop a bad series reaching disk."""

    def _rows(self, count):
        return fk.normalise_page([kline(START + i * DAY_MS) for i in range(count)],
                                 interval_seconds=86400)

    def test_an_empty_series_is_refused(self):
        with pytest.raises(fk.KlineFetchError, match="empty"):
            fk.validate_series([])

    def test_a_short_series_is_refused_against_the_declared_floor(self):
        with pytest.raises(fk.KlineFetchError, match="at least 500"):
            fk.validate_series(self._rows(10), min_bars=500)

    def test_a_duplicate_timestamp_is_refused(self):
        rows = self._rows(5)
        rows[2] = dict(rows[1])
        with pytest.raises(fk.KlineFetchError, match="duplicate"):
            fk.validate_series(rows)

    def test_non_increasing_timestamps_are_refused(self):
        rows = self._rows(5)
        rows[1], rows[2] = rows[2], rows[1]
        with pytest.raises(fk.KlineFetchError, match="strictly increasing"):
            fk.validate_series(rows)

    def test_an_all_nan_series_is_refused(self):
        rows = self._rows(5)
        for row in rows:
            row["price_close"] = float("nan")
        with pytest.raises(fk.KlineFetchError, match="price_close"):
            fk.validate_series(rows)

    def test_a_single_nan_late_in_the_series_is_still_refused(self):
        """One bad bar in a thousand is still a bad corpus."""
        rows = self._rows(50)
        rows[37]["price_high"] = float("nan")
        with pytest.raises(fk.KlineFetchError):
            fk.validate_series(rows)

    def test_a_clean_series_passes(self):
        fk.validate_series(self._rows(600), min_bars=500)


class TestPagination:

    def test_it_collects_every_bar_across_pages(self):
        rows = fk.fetch_symbol(
            "ETHUSDT", "1d", start_ms=START, end_ms=START + 2500 * DAY_MS,
            pager=pages_of(2500), sleep_s=0)
        assert len(rows) == 2500

    def test_overlapping_pages_do_not_duplicate_bars(self):
        """Page boundaries repeat a bar on real exchanges; a repeat is not data."""
        served = [kline(START + i * DAY_MS) for i in range(10)]

        def overlapping(_url, params):
            cursor = int(params["startTime"])
            window = [k for k in served if k[0] >= cursor]
            return window[:4] if window else []

        rows = fk.fetch_symbol("ETHUSDT", "1d", start_ms=START,
                               end_ms=START + 10 * DAY_MS,
                               pager=overlapping, sleep_s=0)
        stamps = [r["time_period_start"] for r in rows]
        assert len(stamps) == len(set(stamps)) == 10

    def test_an_empty_first_page_is_refused_not_silently_accepted(self):
        with pytest.raises(fk.KlineFetchError, match="empty"):
            fk.fetch_symbol("NOPEUSDT", "1d", start_ms=START,
                            end_ms=START + 100 * DAY_MS,
                            pager=lambda *_a, **_k: [], sleep_s=0)

    def test_a_short_range_is_refused_against_the_floor(self):
        """Silent truncation is the failure this exists to prevent."""
        with pytest.raises(fk.KlineFetchError, match="at least 500"):
            fk.fetch_symbol("ETHUSDT", "1d", start_ms=START,
                            end_ms=START + 600 * DAY_MS,
                            pager=pages_of(40), min_bars=500, sleep_s=0)

    def test_an_unsupported_interval_is_refused(self):
        with pytest.raises(fk.KlineFetchError, match="unsupported interval"):
            fk.fetch_symbol("ETHUSDT", "3d", start_ms=START, end_ms=START + DAY_MS,
                            pager=pages_of(10), sleep_s=0)

    def test_a_pager_that_never_advances_terminates(self):
        """A stuck endpoint must not spin forever.

        Returning the same bar for ever is caught by the no-fresh-rows break
        rather than by the page cap: the second page contributes nothing new, so
        the loop stops. It then yields a one-bar series, which any real floor
        refuses (below).
        """
        rows = fk.fetch_symbol("ETHUSDT", "1d", start_ms=START,
                               end_ms=START + 10_000 * DAY_MS,
                               pager=lambda *_a, **_k: [kline(START)],
                               sleep_s=0, max_pages=5)
        assert len(rows) == 1

    def test_a_stuck_endpoint_cannot_satisfy_a_real_floor(self):
        with pytest.raises(fk.KlineFetchError, match="at least 500"):
            fk.fetch_symbol("ETHUSDT", "1d", start_ms=START,
                            end_ms=START + 10_000 * DAY_MS,
                            pager=lambda *_a, **_k: [kline(START)],
                            sleep_s=0, max_pages=5, min_bars=500)

    def test_the_page_cap_is_a_real_backstop(self):
        """A pager that always advances but never reaches the end is capped."""

        def always_new(_url, params):
            cursor = int(params["startTime"])
            return [kline(cursor)]

        with pytest.raises(fk.KlineFetchError, match="did not terminate"):
            fk.fetch_symbol("ETHUSDT", "1d", start_ms=START,
                            end_ms=START + 10_000 * DAY_MS,
                            pager=always_new, sleep_s=0, max_pages=5)

    def test_the_network_pager_is_isolated(self):
        """`fetch_symbol` must never import urllib itself."""
        import inspect
        source = inspect.getsource(fk.fetch_symbol)
        assert "urllib" not in source
        assert "urllib" in inspect.getsource(fk._http_pager)


class TestWrittenCorpusIsContractEligible:
    """The end-to-end claim: what this tool writes, the contract accepts."""

    def _write(self, tmp_path, name="BINANCE_SPOT_ETH_USDT_1D", bars=600):
        rows = fk.fetch_symbol("ETHUSDT", "1d", start_ms=START,
                               end_ms=START + bars * DAY_MS,
                               pager=pages_of(bars), sleep_s=0)
        # Under `<root>/data/`, because that is where `scan_all` looks.
        out = os.path.join(str(tmp_path), "data", "real_multi")
        fk.write_corpus(out, {name: rows}, exchange="BINANCE", market="SPOT",
                        endpoint=fk.BINANCE_ENDPOINT, bar_seconds=86400,
                        requested_range_utc=["2020-09-13T00:00:00Z",
                                             "2022-05-06T00:00:00Z"],
                        fetched_utc="2026-07-31T00:00:00Z")
        return out

    def test_the_manifest_records_real_provenance(self, tmp_path):
        out = self._write(tmp_path)
        with open(os.path.join(out, "MANIFEST.json"), encoding="utf-8") as handle:
            manifest = json.load(handle)
        assert manifest["synthetic"] is False
        assert manifest["source"]["exchange"] == "BINANCE"
        assert manifest["source"]["endpoint"] == fk.BINANCE_ENDPOINT
        assert manifest["bar_seconds"] == 86400
        assert "Eligibility" in manifest["warning"]

    def test_the_csv_has_the_repository_header(self, tmp_path):
        out = self._write(tmp_path)
        path = os.path.join(out, "ohlcv", "BINANCE_SPOT_ETH_USDT_1D.csv.gz")
        with gzip.open(path, "rt") as handle:
            header = handle.readline().strip().split(",")
        assert header == list(fk.COLUMNS)
        for column in dc.REQUIRED_COLUMNS:
            assert column in header

    def test_the_contract_marks_it_eligible(self, tmp_path):
        self._write(tmp_path)
        record = dc.scan_corpus("data/real_multi", repo_root=str(tmp_path))
        assert record.is_synthetic is False
        assert record.is_real_exchange_ohlcv is True
        assert record.interval_label == "D"
        assert record.gap_policy_result == "pass"
        assert record.bar_count == 600
        assert record.eligible_for_stage1 is True
        assert record.ineligible_reasons == []
        assert record.symbols == ["ETH_USDT"]

    def test_multi_asset_flips_true_only_with_a_real_non_btc_corpus(self, tmp_path):
        """The contract's multi-asset flag is wired correctly and is currently
        false only because no such corpus exists — not because it cannot be set.

        Without this test, `multi_asset_corpus_present: false` in the shipped
        report would be indistinguishable from a flag that never flips.
        """
        before = dc.build_report(repo_root=str(tmp_path))
        assert before["multi_asset_corpus_present"] is False

        self._write(tmp_path)
        after = dc.build_report(repo_root=str(tmp_path))
        assert after["multi_asset_corpus_present"] is True
        assert after["non_btc_real_symbols"] == ["ETH_USDT"]
        assert "data/real_multi" in after["stage1_eligible_corpus_paths"]

    def test_a_btc_only_corpus_does_not_flip_it(self, tmp_path):
        self._write(tmp_path, name="BINANCE_SPOT_BTC_USDT_1D")
        report = dc.build_report(repo_root=str(tmp_path))
        assert report["multi_asset_corpus_present"] is False
        assert report["non_btc_real_symbols"] == []

    def test_a_short_corpus_is_never_written(self, tmp_path):
        """`write_corpus` re-validates; it does not trust its caller."""
        rows = fk.normalise_page([kline(START)], interval_seconds=86400)
        out = os.path.join(str(tmp_path), "data", "short")
        fk.write_corpus(out, {"BINANCE_SPOT_ETH_USDT_1D": rows},
                        exchange="BINANCE", market="SPOT",
                        endpoint=fk.BINANCE_ENDPOINT, bar_seconds=86400,
                        requested_range_utc=["a", "b"], fetched_utc="c")
        # It writes (one bar is a valid series), but the contract refuses it.
        record = dc.scan_corpus("data/short", repo_root=str(tmp_path))
        assert record.eligible_for_stage1 is False
        assert any("500" in r for r in record.ineligible_reasons)

    def test_writing_nothing_is_an_error(self, tmp_path):
        with pytest.raises(fk.KlineFetchError, match="nothing to write"):
            fk.write_corpus(str(tmp_path), {}, exchange="BINANCE", market="SPOT",
                            endpoint="x", bar_seconds=86400,
                            requested_range_utc=["a", "b"], fetched_utc="c")

    def test_a_corrupt_series_is_refused_at_write_time(self, tmp_path):
        rows = fk.normalise_page(
            [kline(START + i * DAY_MS) for i in range(5)], interval_seconds=86400)
        rows[3]["price_low"] = float("nan")
        with pytest.raises(fk.KlineFetchError):
            fk.write_corpus(str(tmp_path), {"BINANCE_SPOT_ETH_USDT_1D": rows},
                            exchange="BINANCE", market="SPOT", endpoint="x",
                            bar_seconds=86400, requested_range_utc=["a", "b"],
                            fetched_utc="c")


class TestItNeverFabricates:
    """Structural guards on the one thing that would be catastrophic."""

    def test_it_does_not_forward_fill_or_interpolate(self):
        import inspect
        source = inspect.getsource(fk)
        for banned in ("ffill", "fillna", "interpolate", "resample", "reindex"):
            assert banned not in source, banned

    def test_it_does_not_import_the_synthetic_generator(self):
        import ast
        import inspect
        tree = ast.parse(inspect.getsource(fk))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        for banned in ("make_dataset", "numpy", "random"):
            assert banned not in imported, banned

    def test_synthetic_false_is_written_in_exactly_one_place(self):
        """So the factual claim cannot be made casually from elsewhere."""
        import inspect
        source = inspect.getsource(fk)
        assert source.count('"synthetic": False') == 1

    def test_it_does_not_write_into_the_existing_real_corpora(self):
        import inspect
        source = inspect.getsource(fk)
        for banned in ("data/real_1d", "data/real_4h", "data/ohlcv"):
            assert banned not in source, banned
