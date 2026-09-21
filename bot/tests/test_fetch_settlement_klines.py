"""`fetch_settlement_klines.py` — 8h klines on the funding clock, offline.

No prior test file exercised this tool: it was hardened for the same
append-only discipline as the daily corpora (closed windows only,
append-only, prefix-verified, dry-run by default) but had no offline
coverage of its own. This file pins that discipline directly, plus a gap
check the tool did not have (see `_find_gap` in the tool itself): a venue
that drops one 8h window used to be accepted silently, because the only
protection against a bad fetch was the byte-prefix check on EXISTING
history — which says nothing about a hole inside the NEW rows being
appended.

Nothing here reaches the network: every fetch is injected via `klines=`.
"""
from __future__ import annotations

import datetime as dt
import gzip
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

import fetch_settlement_klines as fsk               # noqa: E402

WINDOW_MS = fsk.WINDOW_S * 1000


def kline(open_ms: int, price: str = "80000.0"):
    return [open_ms, price, price, price, price, "1.0"]


def ms(iso: str) -> int:
    return int(dt.datetime.fromisoformat(iso).replace(
        tzinfo=dt.timezone.utc).timestamp() * 1000)


@pytest.fixture
def repo(tmp_path):
    """A perp file with three closed 8h windows, last closing 2026-09-15T16:00Z."""
    path = tmp_path / fsk.OUT_DIR / "BINANCE_PERP_BTCUSDT_8H.csv.gz"
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [fsk.to_row(kline(ms("2026-09-15T00:00:00")) if i == 0 else
                       kline(ms("2026-09-15T00:00:00") + i * WINDOW_MS))
            for i in range(3)]                       # 00:00, 08:00, 16:00
    with gzip.open(path, "wb") as fh:
        fh.write(fsk.encode(rows))
    return str(tmp_path)


NOW_S = ms("2026-09-16T18:19:11") / 1000.0            # well past the next window


class TestClosedWindowsOnly:
    def test_closed_windows_are_appended(self, repo):
        new = [kline(ms("2026-09-16T00:00:00"))]      # closed by NOW_S
        r = fsk.run(repo, leg="perp", write=False, now_s=NOW_S, klines=new)
        assert r["new_windows"] == 1
        assert r["last_new"] == "2026-09-16T00:00:00Z"

    def test_the_open_window_is_refused(self, repo):
        # a window whose close is still in the future relative to NOW_S
        open_start = int(NOW_S * 1000) - 1000         # opened 1s before "now"
        r = fsk.run(repo, leg="perp", write=False, now_s=NOW_S,
                   klines=[kline(open_start)])
        assert r["new_windows"] == 0
        assert r["refused_open_windows"]


class TestHistoryIsNeverRewritten:
    def test_the_prefix_is_checked(self, repo):
        new = [kline(ms("2026-09-16T00:00:00"))]
        r = fsk.run(repo, leg="perp", write=False, now_s=NOW_S, klines=new)
        assert r["historical_bytes_are_prefix"] is True

    def test_dry_run_writes_nothing(self, repo):
        path = os.path.join(repo, fsk.OUT_DIR, "BINANCE_PERP_BTCUSDT_8H.csv.gz")
        before = open(path, "rb").read()
        fsk.run(repo, leg="perp", write=False, now_s=NOW_S,
               klines=[kline(ms("2026-09-16T00:00:00"))])
        assert open(path, "rb").read() == before

    def test_write_extends_and_verifies_the_prefix(self, repo):
        path = os.path.join(repo, fsk.OUT_DIR, "BINANCE_PERP_BTCUSDT_8H.csv.gz")
        before = open(path, "rb").read()
        r = fsk.run(repo, leg="perp", write=True, now_s=NOW_S,
                   klines=[kline(ms("2026-09-16T00:00:00"))])
        assert r["written"] is True
        after = open(path, "rb").read()
        assert gzip.decompress(after).startswith(gzip.decompress(before))


class TestAGapIsRefused:
    """A window the venue skipped must not be silently appended: the
    prefix check alone cannot catch a hole inside the NEW rows."""

    def test_a_skipped_window_is_refused(self, repo):
        # last on disk closes 2026-09-15T16:00; 00:00 next day is skipped,
        # only the 08:00 window is offered.
        new = [kline(ms("2026-09-16T08:00:00"))]
        r = fsk.run(repo, leg="perp", write=False, now_s=NOW_S, klines=new)
        assert "error" in r
        assert "GAP" in r["error"]

    def test_a_gap_write_touches_nothing_on_disk(self, repo):
        path = os.path.join(repo, fsk.OUT_DIR, "BINANCE_PERP_BTCUSDT_8H.csv.gz")
        before = open(path, "rb").read()
        fsk.run(repo, leg="perp", write=True, now_s=NOW_S,
               klines=[kline(ms("2026-09-16T08:00:00"))])
        assert open(path, "rb").read() == before

    def test_a_gap_between_two_new_windows_is_also_refused(self, repo):
        # 2026-09-16T00:00 is contiguous, but 08:00 is skipped and 16:00 is
        # offered — a later `now_s` so 16:00 has actually closed.
        later_now_s = ms("2026-09-17T20:00:00") / 1000.0
        new = [kline(ms("2026-09-16T00:00:00")), kline(ms("2026-09-16T16:00:00"))]
        r = fsk.run(repo, leg="perp", write=False, now_s=later_now_s, klines=new)
        assert "error" in r
        assert "GAP" in r["error"]

    def test_no_gap_is_not_refused(self, repo):
        new = [kline(ms("2026-09-16T00:00:00")), kline(ms("2026-09-16T08:00:00"))]
        r = fsk.run(repo, leg="perp", write=False, now_s=NOW_S, klines=new)
        assert "error" not in r


class TestBothLegsAreIndependentFiles:
    def test_perp_and_spot_write_different_files(self, tmp_path):
        r_perp = fsk.run(str(tmp_path), leg="perp", write=True, now_s=NOW_S,
                         klines=[kline(ms("2022-08-10T00:00:00"))])
        r_spot = fsk.run(str(tmp_path), leg="spot", write=True, now_s=NOW_S,
                         klines=[kline(ms("2022-08-10T00:00:00"))])
        assert r_perp["written"] is True
        assert r_spot["written"] is True
        assert os.path.exists(os.path.join(
            str(tmp_path), fsk.OUT_DIR, "BINANCE_PERP_BTCUSDT_8H.csv.gz"))
        assert os.path.exists(os.path.join(
            str(tmp_path), fsk.OUT_DIR, "BINANCE_SPOT_BTCUSDT_8H.csv.gz"))


class TestItFabricatesNothing:
    def test_bars_fabricated_is_always_zero(self, repo):
        r = fsk.run(repo, leg="perp", write=False, now_s=NOW_S,
                   klines=[kline(ms("2026-09-16T00:00:00"))])
        assert r["bars_fabricated"] == 0

    def test_an_empty_venue_response_appends_nothing(self, repo):
        r = fsk.run(repo, leg="perp", write=False, now_s=NOW_S, klines=[])
        assert r["new_windows"] == 0
        assert r["historical_bytes_are_prefix"] is True
