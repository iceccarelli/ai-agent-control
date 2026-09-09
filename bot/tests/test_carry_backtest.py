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
    def test_quotability_is_earned_not_assumed(self, report):
        """0018 hardcoded False because Bitstamp USD was the only spot series.
        0020 made quotability track provenance: it is True only when the spot
        leg is the SAME venue and SAME quote currency as the perp. Asserting
        False forever would have made the tool lie once real data arrived."""
        _series, _label, quotable = cb.resolve_spot(REPO)
        assert report["is_a_quotable_return"] is quotable
        if quotable:
            assert "BINANCE" in report["spot_source"]
            assert "USDT" in report["spot_source"]

    def test_the_spot_source_is_named(self, report):
        """`spot_proxy` became `spot_source` in 0020: once a same-venue series
        exists the word "proxy" is wrong, but the provenance must still be
        stated on every report, quotable or not."""
        assert report["spot_source"]
        assert ("BINANCE" in report["spot_source"]
                or "BITSTAMP" in report["spot_source"])

    def test_a_proxy_source_explains_itself(self, report):
        """Only a non-quotable run owes an explanation."""
        if report["is_a_quotable_return"]:
            assert report["why_not_quotable"] == ""
        else:
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


class TestSpotProvenanceDecidesQuotability:
    def test_the_source_is_named_in_the_report(self, report):
        assert "spot_source" in report
        assert report["spot_source"]

    def test_binance_usdt_is_preferred_over_bitstamp_usd(self):
        first = cb.SPOT_SOURCES[0]
        assert "BINANCE" in first[0] and "USDT" in first[0]
        assert first[2] is True, "the same-venue source must be the quotable one"

    def test_only_the_same_venue_same_currency_source_is_quotable(self):
        for path, _label, quotable in cb.SPOT_SOURCES:
            if quotable:
                assert "BINANCE" in path and "USDT" in path
            else:
                assert not ("BINANCE_SPOT_BTC_USDT" in path)

    def test_quotability_tracks_the_source_actually_used(self, report):
        _series, _label, quotable = cb.resolve_spot(REPO)
        assert report["is_a_quotable_return"] is quotable
        assert report["basis_is_a_proxy"] is (not quotable)


class TestTheOpenBarIsRefused:
    def test_today_is_dropped(self):
        """Binance returns the in-progress bar and the fetcher writes it. A day
        that has not closed is not a close."""
        import datetime as _dt
        today = _dt.datetime.now(_dt.timezone.utc).date()
        series = {today: 1.0, today - _dt.timedelta(days=1): 2.0}
        kept = cb.drop_open_bar(series)
        assert today not in kept
        assert (today - _dt.timedelta(days=1)) in kept

    def test_the_loaded_spot_series_ends_before_today(self):
        import datetime as _dt
        series, _l, _q = cb.resolve_spot(REPO)
        assert max(series) < _dt.datetime.now(_dt.timezone.utc).date()

    def test_the_report_declares_the_drop(self, report):
        assert report["open_bar_dropped"] is True


class TestTheGatesChangeTheBook:
    def test_gating_refuses_some_entries(self):
        ungated = cb.simulate(REPO)
        gated = cb.simulate(REPO, gated=True)
        assert gated["trades"] < ungated["trades"], \
            "a gate that changes no trade is decoration"

    def test_gating_does_not_refuse_everything(self):
        """Refusing every entry is not trading, dressed as risk management."""
        assert cb.simulate(REPO, gated=True)["trades"] > 0

    def test_gating_improves_the_hit_rate(self):
        ungated = cb.simulate(REPO)
        gated = cb.simulate(REPO, gated=True)
        u = ungated["losing_trades"] / max(1, ungated["trades"])
        g = gated["losing_trades"] / max(1, gated["trades"])
        assert g < u

    def test_the_refusal_reasons_are_counted(self):
        refusals = cb.simulate(REPO, gated=True)["refusals"]
        assert refusals
        assert "CARRY_BELOW_COST_OF_CAPITAL" in refusals

    def test_gating_rescues_the_book_at_expensive_financing(self):
        """At 8% the ungated book is negative. The gate is what stands between
        a cost of capital it cannot clear and simply not trading."""
        assert cb.simulate(REPO, borrow_apr=0.08)["net_annualised_pct"] < 0
        assert cb.simulate(REPO, borrow_apr=0.08,
                           gated=True)["net_annualised_pct"] >= 0


class TestTheCliActuallyRuns:
    """simulate() was tested; main() was not, so a field renamed in 0020 left
    `carry_backtest.py --repo .` crashing with KeyError: 'spot_proxy' on main.
    Only --matrix worked, and nothing noticed."""

    def test_the_default_invocation_does_not_crash(self, capsys):
        assert cb.main(["--repo", REPO]) == 0
        assert "CARRY BACKTEST" in capsys.readouterr().out

    def test_the_matrix_invocation_does_not_crash(self, capsys):
        assert cb.main(["--repo", REPO, "--matrix"]) == 0
        assert "BORROW x GATE MATRIX" in capsys.readouterr().out

    def test_the_gated_invocation_does_not_crash(self, capsys):
        assert cb.main(["--repo", REPO, "--gated"]) == 0

    def test_the_cli_publishes_the_corpus_digests(self, capsys):
        cb.main(["--repo", REPO])
        out = capsys.readouterr().out
        assert "CORPUS THIS WAS COMPUTED FROM" in out
        assert "perp_1d" in out and "sha" in out


class TestStructuralDefectsRefuseTheBacktest:
    def test_staleness_alone_does_not_refuse(self, report):
        """The committed corpus is FROZEN by design so the slice tests keep
        recording what past measurements saw. A frozen corpus is stale forever
        and a backtest on it is still valid."""
        assert report["corpus_health"]["structural_defects"] == []

    def test_the_report_carries_its_own_provenance(self, report):
        """A number without the digests it came from is a rumour."""
        for series in report["corpus_health"]["series"]:
            assert len(series["sha256"]) == 64
            assert series["rows"] > 0

    def test_structural_defects_are_named_not_swallowed(self):
        assert "OPEN_BAR_IN_FILE" in cb.STRUCTURAL_DEFECTS
        assert "NOT_MONOTONIC" in cb.STRUCTURAL_DEFECTS
        assert not any("STALE" in d for d in cb.STRUCTURAL_DEFECTS), \
            "staleness must not refuse a frozen research corpus"
