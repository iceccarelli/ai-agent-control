"""Appending to the forward corpus without editing history.

WHY THIS FILE EXISTS
====================
The forward programme is blocked on DATA, not on patience. `t1` is 2026-08-09,
the daily corpus ends 2026-08-24, and the venue has bars through today. Slice 76
records the consequence: four setups have fired in the window and none is
scoreable, because "eligibility reaches back seven bars" and every flag sits in
the tail against the end of the file.

The two shipped fetchers cannot fix that. Both end in `write_gz(path, rows)`
over `--years` back from now, so both REPLACE the file — shrinking it at the
front at 4.0 years, or shifting the pinned prefix at anything larger. Either
breaks `tools/corpus_prefix.py`, which is why the corpus went stale rather than
being refreshed.

THE PROPERTIES THAT MAKE AN APPEND SAFE, AND WHY EACH IS HERE
=============================================================
1. **History is preserved BYTE FOR BYTE.** Rows are read as strings and written
   back as strings. The failure this prevents is subtle and would look like a
   pure append: parse `227752.0` to a float, re-serialise, and rows that nobody
   meant to touch change their bytes — after which `history_unchanged` is false
   and the cut is no longer locked against the corpus it was locked against.
2. **Only CLOSED periods are appended.** Today's daily bar is still forming.
3. **The invariant is re-checked AFTER the write**, and a violation restores
   the original file. A corpus that fails its own invariant is worse than one
   that is merely stale.
4. **BTCUSDT only.** ETH and SOL carry a manifest/disk disagreement that is a
   recorded finding under test (EDGE.md §47b); refreshing them would erase the
   finding rather than repair it.
5. **The volume unit swap is not touched.** 2026-08-18..08-22 carry
   quote-notional, and `test_corpus_unit_audit` says its assertion is written
   to fail when a HUMAN repairs that. Repairing it as a side effect would spend
   the signal without anybody reading it.

Every test here runs against an injected fake venue. Nothing in this file
reaches the network.
"""
from __future__ import annotations

import csv
import gzip
import io
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "tools"))

import refresh_forward_corpus as rfc          # noqa: E402

DAY = 86_400_000
#: 2026-08-25T00:00:00Z — the first bar missing from the shipped corpus, and
#: `t1 + 16 days` where t1 (2026-08-09) is 1786233600000. The first version of
#: this constant was 1786665600000, which is t1 + 5 days = 2026-08-14, so a
#: test that meant "the day after the corpus ends" silently asserted against a
#: date ten days inside it. The arithmetic is spelled out here because a bare
#: epoch is exactly the kind of literal nobody re-checks.
NEXT_DAY_MS = 1787616000000


def kline(open_ms, close=100.0, volume=227_752.0, trades=1000):
    return [open_ms, "99.0", "101.0", "98.0", str(close), str(volume),
            open_ms + DAY - 1, "0", trades]


def fake_klines(bars):
    def getter(url, params):
        start = int(params["startTime"])
        return [k for k in bars if k[0] >= start][:int(params["limit"])]
    return getter


def fake_funding(prints):
    def getter(url, params):
        start = int(params["startTime"])
        return [p for p in prints if int(p["fundingTime"]) >= start][
            :int(params["limit"])]
    return getter


def rows_of(path):
    with gzip.open(path, "rt", newline="") as handle:
        return list(csv.DictReader(handle))


# ---------------------------------------------------------------------------


class TestHistoryIsPreservedByteForByte:
    """The property everything else rests on."""

    def test_encode_round_trips_the_shipped_corpus_exactly(self):
        """Read the real file and re-encode it. If one byte moves, an 'append'
        would silently edit history and the cut would stop being locked."""
        path = os.path.join(
            REPO, "data/real_linear_1d/ohlcv/BINANCE_LINEAR_BTC_USDT_1D.csv.gz")
        if not os.path.exists(path):
            pytest.skip("shipped corpus absent")
        with gzip.open(path, "rb") as handle:
            original = handle.read()
        again = rfc.encode(rfc.read_rows(path), rfc.DAILY_FIELDS)
        assert again == original

    def test_the_funding_corpus_round_trips_too(self):
        path = os.path.join(
            REPO,
            "data/real_funding/funding/BINANCE_LINEAR_BTC_USDT_FUNDING.csv.gz")
        if not os.path.exists(path):
            pytest.skip("shipped corpus absent")
        with gzip.open(path, "rb") as handle:
            original = handle.read()
        again = rfc.encode(rfc.read_rows(path), rfc.FUNDING_FIELDS)
        assert again == original

    def test_values_are_kept_as_strings_never_floats(self):
        """A float round trip is how a pure append edits bytes."""
        path = os.path.join(
            REPO, "data/real_linear_1d/ohlcv/BINANCE_LINEAR_BTC_USDT_1D.csv.gz")
        if not os.path.exists(path):
            pytest.skip("shipped corpus absent")
        row = rfc.read_rows(path)[0]
        assert all(isinstance(v, str) for v in row.values())


class TestOnlyClosedPeriodsAreAppended:
    def test_a_forming_bar_is_excluded(self):
        """The bar whose close is in the future is still being written."""
        now = NEXT_DAY_MS + DAY + 1000          # mid-way through the NEXT day
        got = rfc.fetch_daily_after(
            "BTCUSDT", NEXT_DAY_MS - DAY, now,
            getter=fake_klines([kline(NEXT_DAY_MS),
                                kline(NEXT_DAY_MS + DAY)]))
        stamps = [r["time_period_start"] for r in got]
        assert len(got) == 1, stamps

    def test_bars_at_or_before_the_cursor_are_excluded(self):
        now = NEXT_DAY_MS + 5 * DAY
        got = rfc.fetch_daily_after(
            "BTCUSDT", NEXT_DAY_MS, now,
            getter=fake_klines([kline(NEXT_DAY_MS - DAY), kline(NEXT_DAY_MS),
                                kline(NEXT_DAY_MS + DAY)]))
        assert len(got) == 1
        assert got[0]["time_period_start"].startswith("2026-08-26")

    def test_an_unsettled_funding_print_is_excluded(self):
        now = NEXT_DAY_MS + 1000
        prints = [{"fundingTime": NEXT_DAY_MS - 1, "fundingRate": "0.0001",
                   "markPrice": "100"},
                  {"fundingTime": NEXT_DAY_MS + 5000, "fundingRate": "0.0002",
                   "markPrice": "100"}]
        got = rfc.fetch_funding_after("BTCUSDT", NEXT_DAY_MS - DAY, now,
                                      getter=fake_funding(prints))
        assert len(got) == 1


class TestTheInvariantIsCheckedAfterWriting:
    def test_a_dry_run_writes_nothing(self, tmp_path):
        path = tmp_path / "c.csv.gz"
        with gzip.open(path, "wb") as handle:
            handle.write(rfc.encode(
                [{"funding_time_ms": "1", "funding_time": "a", "symbol": "B",
                  "funding_rate": "0.1", "mark_price": ""}],
                rfc.FUNDING_FIELDS))
        before = path.read_bytes()
        report = rfc.append_file(
            path_rel=str(path), fields=rfc.FUNDING_FIELDS,
            time_column="funding_time_ms",
            new_rows=[{"funding_time_ms": "2", "funding_time": "b",
                       "symbol": "B", "funding_rate": "0.2",
                       "mark_price": ""}],
            write=False, verify=False)
        assert report["rows_appended"] == 1
        assert report["written"] is False
        assert path.read_bytes() == before

    def test_a_write_appends_and_keeps_the_original_rows(self, tmp_path):
        path = tmp_path / "c.csv.gz"
        first = {"funding_time_ms": "1", "funding_time": "a", "symbol": "B",
                 "funding_rate": "0.1", "mark_price": ""}
        with gzip.open(path, "wb") as handle:
            handle.write(rfc.encode([first], rfc.FUNDING_FIELDS))
        rfc.append_file(
            path_rel=str(path), fields=rfc.FUNDING_FIELDS,
            time_column="funding_time_ms",
            new_rows=[{"funding_time_ms": "2", "funding_time": "b",
                       "symbol": "B", "funding_rate": "0.2",
                       "mark_price": ""}],
            write=True, verify=False)
        rows = rows_of(path)
        assert len(rows) == 2
        assert rows[0] == first

    def test_a_duplicate_timestamp_is_not_appended_twice(self, tmp_path):
        path = tmp_path / "c.csv.gz"
        first = {"funding_time_ms": "1", "funding_time": "a", "symbol": "B",
                 "funding_rate": "0.1", "mark_price": ""}
        with gzip.open(path, "wb") as handle:
            handle.write(rfc.encode([first], rfc.FUNDING_FIELDS))
        report = rfc.append_file(
            path_rel=str(path), fields=rfc.FUNDING_FIELDS,
            time_column="funding_time_ms", new_rows=[dict(first)],
            write=True, verify=False)
        assert report["rows_appended"] == 0
        assert len(rows_of(path)) == 1

    def test_a_broken_invariant_restores_the_original_file(self, tmp_path,
                                                           monkeypatch):
        """The property that makes this safe to run unattended."""
        path = tmp_path / "c.csv.gz"
        first = {"funding_time_ms": "1", "funding_time": "a", "symbol": "B",
                 "funding_rate": "0.1", "mark_price": ""}
        with gzip.open(path, "wb") as handle:
            handle.write(rfc.encode([first], rfc.FUNDING_FIELDS))
        before = path.read_bytes()

        monkeypatch.setattr(rfc, "_verify", lambda _p: {
            "history_unchanged": False, "never_shrank": True,
            "appended_all_strictly_after_t1": True,
            "pinned_rows": 1, "rows_on_disk": 2, "appended_rows": 1})

        with pytest.raises(rfc.RefuseToRefresh) as exc:
            rfc.append_file(
                path_rel=str(path), fields=rfc.FUNDING_FIELDS,
                time_column="funding_time_ms",
                new_rows=[{"funding_time_ms": "2", "funding_time": "b",
                           "symbol": "B", "funding_rate": "0.2",
                           "mark_price": ""}],
                write=True, verify=True)
        assert "restored" in str(exc.value)
        assert path.read_bytes() == before, "the rollback did not restore bytes"


class TestItRefusesTheSymbolsThatCarryAFinding:
    @pytest.mark.parametrize("symbol", ("ETHUSDT", "SOLUSDT"))
    def test_eth_and_sol_are_refused(self, symbol):
        with pytest.raises(rfc.RefuseToRefresh) as exc:
            rfc.run(symbol=symbol)
        assert "finding" in str(exc.value)

    def test_the_refusal_names_the_reason_not_just_the_symbol(self):
        with pytest.raises(rfc.RefuseToRefresh) as exc:
            rfc.run(symbol="ETHUSDT")
        assert "47b" in str(exc.value)


class TestTheUnitSwapIsLeftForAHuman:
    def test_the_defective_rows_are_before_the_append_point(self):
        """2026-08-18..08-22 are already on disk, so an append never reaches
        them. This asserts the geometry rather than trusting it."""
        path = os.path.join(
            REPO, "data/real_linear_1d/ohlcv/BINANCE_LINEAR_BTC_USDT_1D.csv.gz")
        if not os.path.exists(path):
            pytest.skip("shipped corpus absent")
        rows = rfc.read_rows(path)
        last = rows[-1]["time_period_start"][:10]
        assert last >= "2026-08-22", last

    def test_the_tool_says_it_will_not_repair_the_units(self):
        assert "unit swap" in rfc.__doc__.lower()
