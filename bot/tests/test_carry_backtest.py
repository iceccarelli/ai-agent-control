"""The backtester must charge the book for existing.

Before this, the only carry number in the project was a naive sum of funding
prints — the gross income line of a business whose expenses had never been
written down. These tests pin every cost term to the output.
"""
from __future__ import annotations

import datetime as dt
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import carry_backtest as cb  # noqa: E402

REPO = os.path.join(os.path.dirname(__file__), "..")


@pytest.fixture(scope="module")
def report():
    return cb.simulate(REPO, notional=100_000.0)


class TestEveryCostIsCharged:
    @pytest.mark.parametrize("term", ["fees_usd", "borrow_usd"])
    def test_the_cost_is_non_zero(self, report, term):
        """A cost that never appears is a cost nobody modelled."""
        assert report[term] > 0, f"{term} is zero; the book traded for free"

    def test_the_basis_term_exists(self, report):
        assert "basis_pnl_usd" in report

    def test_net_equals_the_sum_of_its_parts(self, report):
        expected = (report["gross_funding_usd"] + report["basis_pnl_usd"]
                    - report["fees_usd"] - report["borrow_usd"])
        assert report["net_usd"] == pytest.approx(expected, rel=1e-9)

    def test_net_is_below_the_naive_figure(self, report):
        """If costs did not reduce the number, they were not charged."""
        assert report["net_annualised_pct"] < \
            report["naive_funding_sum_pct_per_yr"]


class TestBorrowDecidesWhoCanRunThisBook:
    """At 5% financing the trade nets ~1.8%. At 0% it nets ~8%. Whether this is
    a business depends entirely on whether you already own the BTC."""

    def test_a_higher_borrow_rate_lowers_the_return(self):
        cheap = cb.simulate(REPO, borrow_apr=0.0)["net_annualised_pct"]
        dear = cb.simulate(REPO, borrow_apr=0.08)["net_annualised_pct"]
        assert cheap > dear

    def test_expensive_financing_turns_the_book_negative(self):
        assert cb.simulate(REPO, borrow_apr=0.08)["net_annualised_pct"] < 0

    def test_owning_the_spot_leg_outright_is_the_viable_case(self):
        assert cb.simulate(REPO, borrow_apr=0.0)["net_annualised_pct"] > 5.0


class TestTheBasisIsRealPnL:
    def test_entry_and_exit_basis_are_reported(self, report):
        for key in ("mean_entry_basis_bps", "mean_exit_basis_bps"):
            assert isinstance(report[key], float)

    def test_basis_pnl_follows_the_short_perp_long_spot_convention(self):
        """Enter wide, exit narrow -> the convergence PAYS a short-perp book."""
        wide = cb.Trade(opened=dt.date(2024, 1, 1), qty=1.0,
                        entry_spot=100.0, entry_perp=101.0)
        wide.closed, wide.exit_spot, wide.exit_perp = dt.date(2024, 1, 5), 100.0, 100.0
        assert wide.basis_pnl > 0

        narrow = cb.Trade(opened=dt.date(2024, 1, 1), qty=1.0,
                          entry_spot=100.0, entry_perp=100.0)
        narrow.closed, narrow.exit_spot, narrow.exit_perp = \
            dt.date(2024, 1, 5), 100.0, 101.0
        assert narrow.basis_pnl < 0

    def test_a_pure_price_move_with_a_stable_basis_is_flat(self):
        """The whole point of the hedge: direction must not matter."""
        t = cb.Trade(opened=dt.date(2024, 1, 1), qty=1.0,
                     entry_spot=100.0, entry_perp=100.0)
        t.closed, t.exit_spot, t.exit_perp = dt.date(2024, 1, 5), 150.0, 150.0
        assert t.basis_pnl == pytest.approx(0.0)


class TestItRefusesToBeQuoted:
    def test_the_report_declares_itself_unquotable(self, report):
        assert report["is_a_quotable_return"] is False

    def test_the_spot_proxy_is_named(self, report):
        assert "BITSTAMP" in report["spot_proxy"]
        assert report["basis_is_a_proxy"] is True

    def test_the_reason_names_venue_and_currency(self, report):
        why = report["why_not_quotable"].lower()
        assert "venue" in why and "currency" in why

    def test_what_is_not_modelled_is_listed(self, report):
        joined = " ".join(report["not_modelled"]).lower()
        for gap in ("slippage", "depth", "liquidation"):
            assert gap in joined


class TestItScoresNothingAndArmsNothing:
    def test_no_module_assigns_live_authorized(self):
        with open(os.path.join(REPO, "tools", "carry_backtest.py"),
                  encoding="utf-8") as fh:
            body = fh.read()
        assert "live_authorized" not in body
        assert "place_order" not in body

    def test_costs_are_declared_as_module_constants(self):
        """Declared before the run, not tuned after seeing the answer."""
        for name in ("TAKER_BPS_SPOT", "TAKER_BPS_PERP", "BORROW_APR",
                     "ENTRY_FUNDING_BPS"):
            assert hasattr(cb, name)


class TestTheSimulationIsSane:
    def test_it_produces_trades(self, report):
        assert report["trades"] > 0

    def test_it_is_deterministic(self):
        a = cb.simulate(REPO)["net_usd"]
        b = cb.simulate(REPO)["net_usd"]
        assert a == pytest.approx(b)

    def test_every_trade_closes(self, report):
        assert all(t["closed"] != "None" for t in report["trade_log"])

    def test_the_trade_log_carries_each_cost(self, report):
        for key in ("funding", "basis", "fees", "borrow", "net"):
            assert key in report["trade_log"][0]
