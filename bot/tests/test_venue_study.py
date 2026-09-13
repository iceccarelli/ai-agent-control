"""Three questions the repo could not answer without exchange data."""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))

import venue_study as vs  # noqa: E402

REPO = os.path.join(os.path.dirname(__file__), "..")


@pytest.fixture(scope="module")
def venue():
    return vs.venue_funding(REPO)


class TestTheVenueMismatch:
    def test_bybit_and_binance_funding_are_matched_print_by_print(self, venue):
        assert venue["available"] is True
        assert venue["matched_prints"] > 4_000

    def test_the_difference_is_small_against_the_edge(self, venue):
        """Orders go to Bybit; every corpus is Binance. If funding differed by
        more than the edge, the backtest would describe a trade nobody makes."""
        assert abs(venue["difference"]["annualised_pct"]) < 1.0

    def test_bybit_funding_is_noisier(self, venue):
        """It is. That matters for the EWMA gate: the same smoother sees a
        wider distribution on the venue the book will trade."""
        assert venue["bybit"]["stdev_bps"] > venue["binance"]["stdev_bps"]

    def test_both_venues_are_positive_most_of_the_time(self, venue):
        for name in ("bybit", "binance"):
            assert venue[name]["pct_positive"] > 75.0


class TestCapacity:
    def test_a_number_exists_at_last(self):
        c = vs.capacity(REPO)
        assert c["available"] is True
        assert c["thinnest_1pct_usd"] > 0

    def test_capacity_varies_by_regime(self):
        """A size safe on a calm day is several times too large on a violent
        one. Capacity is a number PER REGIME."""
        assert vs.capacity(REPO)["regime_spread_x"] > 2.0

    def test_the_resolution_limit_is_stated_not_hidden(self):
        """1% bands cannot resolve impact below 1%. Reporting a number the data
        cannot support is how a capacity estimate becomes a fiction."""
        assert "unresolvable" in vs.capacity(REPO)["cannot_resolve"]

    def test_a_round_trip_is_four_legs(self):
        assert vs.LEGS_PER_ROUND_TRIP == 4


class TestStress:
    def test_violent_days_are_replayed(self):
        s = vs.stress(REPO)
        assert s["available"] is True
        assert len(s["days"]) >= 3

    def test_price_moves_far_more_than_the_basis(self):
        """The whole thesis in one assertion: the hedge absorbs direction."""
        for day in vs.stress(REPO)["days"]:
            price_bps = day["price_swing_pct"] * 100
            assert day["basis_range_bps"] < price_bps / 10, (
                f"{day['day']}: basis moved {day['basis_range_bps']:.1f} bps "
                f"against a {price_bps:.0f} bps price swing — if these were "
                "comparable the hedge would not be a hedge")

    def test_the_basis_is_stable_even_when_price_is_not(self):
        for day in vs.stress(REPO)["days"]:
            assert day["basis_stdev_bps"] < 5.0


class TestItIsAStudyNotACorpus:
    def test_nothing_here_feeds_a_gate(self):
        with open(os.path.join(REPO, "tools", "venue_study.py"),
                  encoding="utf-8") as fh:
            body = fh.read()
        for banned in ("place_market", "live_authorized", "FUND_ABS",
                       "evaluate_entry"):
            assert banned not in body

    def test_the_cli_runs(self, capsys):
        assert vs.main(["--repo", REPO]) == 0
        assert "VENUE STUDY" in capsys.readouterr().out


class TestItsReadsReachTheLedger:
    """0028 read the Bybit set and the Binance funding corpus and recorded
    neither (INVENTORY D6). A future holdout cannot be certified untouched if
    the reads that informed a venue decision are missing."""

    def test_venue_and_stress_reads_are_recorded(self, tmp_path, monkeypatch):
        import json
        ledger = tmp_path / "ledger.json"
        monkeypatch.setattr(vs, "LEDGER_PATH", str(ledger))
        monkeypatch.delenv("LEDGER_DISABLED", raising=False)
        vs.venue_funding(REPO)
        vs.stress(REPO)
        reads = json.loads(ledger.read_text())["reads"]
        datasets = {r["dataset"] for r in reads}
        assert "BYBIT_LINEAR_BTC_USDT_FUNDING" in datasets
        assert "BINANCE_LINEAR_BTC_USDT_FUNDING" in datasets
        assert any(d.startswith("BINANCE_LINEAR_BTC_USDT_1M") for d in datasets)
        assert all(r["read_by"].endswith("venue_study.py") for r in reads)

    def test_a_test_run_records_nothing(self, tmp_path, monkeypatch):
        ledger = tmp_path / "ledger.json"
        monkeypatch.setattr(vs, "LEDGER_PATH", str(ledger))
        monkeypatch.setenv("LEDGER_DISABLED", "1")
        vs.venue_funding(REPO)
        assert not ledger.exists()
