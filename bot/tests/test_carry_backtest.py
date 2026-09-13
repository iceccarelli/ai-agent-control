"""The backtester must charge the book for existing.

Before this, the only carry number in the project was a naive sum of funding
prints — the gross income line of a business whose expenses had never been
written down. These tests pin every cost term to the output.
"""
from __future__ import annotations

import datetime as dt
import json
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
        0020 made quotability track provenance alone. 0031 makes it STRICTER:
        same venue and quote currency is still required, and no longer
        sufficient. Quotable implies every condition in the report; the old
        provenance condition is one of them."""
        _series, _label, same_venue = cb.resolve_spot(REPO)
        conditions = report["quotable_conditions"]
        assert conditions["same_venue_same_quote_spot"] is same_venue
        assert report["is_a_quotable_return"] is all(conditions.values())
        if report["is_a_quotable_return"]:
            assert same_venue
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
        """Only a non-quotable run owes an explanation, and it must name every
        unmet condition. A proxy spot leg still has to say venue AND currency."""
        if report["is_a_quotable_return"]:
            assert report["why_not_quotable"] == ""
        else:
            why = report["why_not_quotable"].lower()
            assert why
            if not report["quotable_conditions"]["same_venue_same_quote_spot"]:
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
        """Provenance is still tracked exactly; it is now a necessary
        condition for quotability rather than the whole of it."""
        _series, _label, same_venue = cb.resolve_spot(REPO)
        assert report["quotable_conditions"]["same_venue_same_quote_spot"] \
            is same_venue
        assert report["basis_is_a_proxy"] is (not same_venue)
        if not same_venue:
            assert report["is_a_quotable_return"] is False


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


# ---------------------------------------------------------------------------
# 0031 — the attribution identity and the stricter definition of quotable
# ---------------------------------------------------------------------------

def _synthetic(prices_perp, prices_spot, rate=0.0):
    """Daily series and one funding print per day at 00:00 UTC."""
    start = dt.date(2024, 1, 1)
    days = [start + dt.timedelta(days=i) for i in range(len(prices_perp))]
    perp = dict(zip(days, prices_perp))
    spot = dict(zip(days, prices_spot))
    ftimes = [int(dt.datetime.combine(d, dt.time(),
                                      dt.timezone.utc).timestamp() * 1000)
              for d in days]
    return perp, spot, ftimes, [rate] * len(days)


class TestTheAttributionSumsToNet:
    TERMS = ("funding_usd", "basis_usd", "fees_usd", "borrow_usd",
             "impact_usd", "other_usd")

    def test_the_report_attribution_sums_to_net(self, report):
        a = report["attribution"]
        assert sum(a[t] for t in self.TERMS) == pytest.approx(a["net_usd"],
                                                              abs=1e-6)
        assert a["residual_usd"] == pytest.approx(0.0, abs=1e-6)
        assert a["net_usd"] == pytest.approx(report["net_usd"], abs=1e-6)

    def test_costs_are_signed_as_costs(self, report):
        a = report["attribution"]
        assert a["fees_usd"] <= 0 and a["borrow_usd"] <= 0
        assert a["impact_usd"] <= 0

    def test_every_trade_sums_to_its_net(self, report):
        for t in report["trade_log"]:
            parts = (t["funding"] + t["basis"] - t["fees"] - t["borrow"]
                     - t["impact"] + t["other"])
            assert parts == pytest.approx(t["net"], abs=0.02)   # rounded to cents

    def test_the_basis_is_the_sum_of_the_two_price_legs(self, report):
        """Long spot + short perp. The price terms of the two legs are
        reported separately and must add up to the basis exactly — the price
        itself cancels, and this is where that is shown rather than asserted."""
        a = report["attribution"]
        assert (a["spot_leg_price_usd"] + a["perp_leg_price_usd"]) == \
            pytest.approx(a["basis_usd"], abs=1e-6)

    def test_the_cli_prints_the_identity(self, capsys):
        cb.main(["--repo", REPO])
        out = capsys.readouterr().out
        assert "ATTRIBUTION" in out
        assert "= NET" in out
        assert "residual" in out
        assert "impact" in out


class TestAPurePriceMoveIsFlat:
    def test_a_random_walk_with_zero_basis_books_no_price_pnl(self):
        import random
        rng = random.Random(7)
        px = [50_000.0]
        for _ in range(299):
            px.append(px[-1] * (1 + rng.gauss(0, 0.03)))
        perp, spot, ft, fr = _synthetic(px, px, rate=0.0)
        r = cb.simulate_series(perp=perp, spot=spot, ftimes=ft, frates=fr,
                               notional=100_000.0, borrow_apr=0.0,
                               entry_bps=-1.0)
        assert r["trades"] >= 1
        assert r["attribution"]["basis_usd"] == pytest.approx(0.0, abs=1e-6)
        assert r["net_usd"] == pytest.approx(-r["fees_usd"], abs=1e-6)

    def test_a_stable_basis_through_a_big_move_is_flat_within_basis_times_move(self):
        """Price doubles; basis holds at +10 bps. The book may move by the
        basis times the move (the dollar basis widens with price), never by
        the move itself."""
        b = 10.0
        spot_px = [50_000.0 * (1 + i / 99.0) for i in range(100)]   # 50k -> 100k
        perp_px = [p * (1 + b / 1e4) for p in spot_px]
        perp, spot, ft, fr = _synthetic(perp_px, spot_px, rate=0.0)
        r = cb.simulate_series(perp=perp, spot=spot, ftimes=ft, frates=fr,
                               notional=100_000.0, borrow_apr=0.0,
                               entry_bps=-1.0)
        move = abs(spot_px[-1] / spot_px[0] - 1.0)
        tolerance = 100_000.0 * (b / 1e4) * move * 1.01
        assert abs(r["attribution"]["basis_usd"]) <= tolerance
        assert abs(r["attribution"]["basis_usd"]) < 0.01 * 100_000.0 * move

    def test_constant_funding_is_income_and_nothing_else(self):
        px = [60_000.0] * 30
        perp, spot, ft, fr = _synthetic(px, px, rate=0.0001)   # 1 bp per print
        r = cb.simulate_series(perp=perp, spot=spot, ftimes=ft, frates=fr,
                               notional=100_000.0, borrow_apr=0.0,
                               entry_bps=-1.0)
        a = r["attribution"]
        assert a["basis_usd"] == pytest.approx(0.0, abs=1e-9)
        assert a["funding_usd"] > 0
        assert a["net_usd"] == pytest.approx(a["funding_usd"] + a["fees_usd"],
                                             abs=1e-6)


class TestDailyClosesAreNotQuotable:
    def test_the_daily_run_is_not_quotable(self, report):
        """Rule 19: no quotable return while settlement is a daily close."""
        assert report["is_a_quotable_return"] is False
        assert report["quotable_conditions"]["settlement_clock_basis"] is False
        assert report["quotable_conditions"]["impact_haircut_applied"] is False

    def test_the_explanation_names_the_clock_and_the_impact(self, report):
        why = report["why_not_quotable"].lower()
        assert "settlement" in why and "impact" in why

    def test_the_gated_run_is_named_in_sample(self):
        g = cb.simulate(REPO, gated=True)
        assert g["is_a_quotable_return"] is False
        assert g["quotable_conditions"]["rule_scored_on_an_untouched_window"] \
            is False
        assert "in-sample" in g["why_not_quotable"].lower()

    def test_the_ungated_run_says_the_engine_does_not_run_it(self, report):
        assert report["quotable_conditions"]["rule_is_what_the_engine_runs"] \
            is False

    def test_the_cli_prints_every_condition(self, capsys):
        cb.main(["--repo", REPO])
        out = capsys.readouterr().out
        for name in cb.QUOTABLE_CONDITIONS:
            assert name in out


class TestOnlyClosedTradesAreCounted:
    def test_the_open_trade_is_flagged(self, report):
        flagged = [t for t in report["trade_log"] if t["open_at_end"]]
        assert len(flagged) <= 1
        assert report["closed_trades"] == report["trades"] - len(flagged)

    def test_closed_and_marked_are_reported_separately(self, report):
        assert report["closed_net_usd"] + report["open_at_end_marked_usd"] == \
            pytest.approx(report["net_usd"], abs=1e-6)


class TestTheProxyIsKilled:
    def test_bitstamp_is_not_a_spot_source(self):
        """Kill Bitstamp-USD as the basis proxy for any allocator-facing number."""
        for path, _label, _q in cb.SPOT_SOURCES:
            assert "BITSTAMP" not in path.upper()

    def test_each_helper_is_defined_once(self):
        import ast
        with open(os.path.join(REPO, "tools", "carry_backtest.py"),
                  encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        names = [n.name for n in tree.body if isinstance(n, ast.FunctionDef)]
        dupes = sorted({n for n in names if names.count(n) > 1})
        assert dupes == [], f"defined twice: {dupes}"


# ---------------------------------------------------------------------------
# 0032 — the settlement clock, same venue, impact, stress, the read ledger
# ---------------------------------------------------------------------------

def _st(i, rate, perp=60_000.0, spot=60_000.0, high=None, low=None):
    ms = 1_700_006_400_000 + i * cb.SETTLEMENT_MS      # an 8h boundary
    return cb.Settlement(ms=ms, perp=perp, spot=spot, rate=rate,
                         perp_high=perp if high is None else high,
                         perp_low=perp if low is None else low)


@pytest.fixture(scope="module")
def bybit8h():
    return cb.simulate_settlement(REPO, venue="bybit", notional=100_000.0,
                                  borrow_apr=0.05)


class TestTheSettlementCorpusLoads:
    def test_every_settlement_is_on_the_8h_clock(self):
        rows, meta = cb.load_bybit_settlements(REPO)
        assert len(rows) > 4000
        assert all(r.ms % cb.SETTLEMENT_MS == 0 for r in rows)
        assert meta["same_venue"] is True

    def test_prices_are_the_close_of_the_bar_that_ends_at_the_stamp(self):
        rows, _ = cb.load_bybit_settlements(REPO)
        perp = cb._load_4h(os.path.join(REPO, cb.BYBIT_PERP_4H))
        spot = cb._load_4h(os.path.join(REPO, cb.BYBIT_SPOT_4H))
        for r in rows[::500]:
            assert r.perp == perp[r.ms - cb.BAR_4H_MS][3]
            assert r.spot == spot[r.ms - cb.BAR_4H_MS][3]


class TestTheSettlementRunIsHonest:
    def test_the_attribution_sums(self, bybit8h):
        a = bybit8h["attribution"]
        parts = (a["funding_usd"] + a["basis_usd"] + a["fees_usd"]
                 + a["borrow_usd"] + a["impact_usd"] + a["other_usd"])
        assert parts == pytest.approx(a["net_usd"], abs=1e-6)

    def test_impact_is_charged(self, bybit8h):
        assert bybit8h["attribution"]["impact_usd"] < 0
        assert bybit8h["impact_bps_per_leg"] >= cb.IMPACT_FLOOR_BPS_PER_LEG

    def test_three_conditions_hold_and_two_do_not(self, bybit8h):
        c = bybit8h["quotable_conditions"]
        assert c["same_venue_same_quote_spot"] is True
        assert c["settlement_clock_basis"] is True
        assert c["impact_haircut_applied"] is True
        assert c["rule_is_what_the_engine_runs"] is False
        assert c["rule_scored_on_an_untouched_window"] is False
        assert bybit8h["is_a_quotable_return"] is False

    def test_the_gated_rule_is_the_engine_rule_and_still_not_quotable(self):
        g = cb.simulate_settlement(REPO, venue="bybit", notional=100_000.0,
                                   borrow_apr=0.05, gated=True)
        assert g["quotable_conditions"]["rule_is_what_the_engine_runs"] is True
        assert g["is_a_quotable_return"] is False
        assert "in-sample" in g["why_not_quotable"].lower()

    def test_without_impact_the_condition_says_so(self):
        r = cb.simulate_settlement(REPO, venue="bybit", notional=100_000.0,
                                   borrow_apr=0.05, impact=False)
        assert r["attribution"]["impact_usd"] == 0.0
        assert r["quotable_conditions"]["impact_haircut_applied"] is False

    def test_the_wick_layer_is_labelled_as_not_a_tick_wick(self, bybit8h):
        assert "not" in bybit8h["wick_note"].lower()
        assert bybit8h["max_adverse_short_excursion_pct"] >= 0.0

    def test_the_venue_is_the_one_the_broker_trades_on(self, bybit8h):
        assert "BYBIT" in bybit8h["spot_source"]


class TestPrintsNotTicks:
    def test_three_negative_prints_exit_on_the_third(self):
        rows = [_st(0, 0.0001)] + [_st(i, -0.00001) for i in (1, 2, 3)]
        r = cb.simulate_settlements_series(rows, notional=100_000.0,
                                           borrow_apr=0.0, impact_bps=0.0)
        t = r["trade_log"][0]
        assert t["reason"] == "FUNDING_INVERTED"
        assert t["closed"] == str(cb._when(rows[3].ms))

    def test_two_negative_prints_then_a_positive_one_hold(self):
        rows = [_st(0, 0.0001), _st(1, -0.00001), _st(2, -0.00001),
                _st(3, 0.0001), _st(4, -0.00001)]
        r = cb.simulate_settlements_series(rows, notional=100_000.0,
                                           borrow_apr=0.0, impact_bps=0.0)
        assert r["trade_log"][0]["reason"] == "OPEN_AT_END"

    def test_each_print_is_booked_once(self):
        n = 10
        rows = [_st(i, 0.0001) for i in range(n)]
        r = cb.simulate_settlements_series(rows, notional=60_000.0,
                                           borrow_apr=0.0, impact_bps=0.0)
        qty = 60_000.0 / 60_000.0
        # opened at print 0, held through prints 1..n-1
        assert r["attribution"]["funding_usd"] == pytest.approx(
            (n - 1) * 0.0001 * qty * 60_000.0)

    def test_negative_prints_are_booked_as_paid(self):
        rows = [_st(0, 0.0001), _st(1, -0.0002), _st(2, 0.0001)]
        r = cb.simulate_settlements_series(rows, notional=60_000.0,
                                           borrow_apr=0.0, impact_bps=0.0)
        assert r["attribution"]["funding_usd"] == pytest.approx(
            (-0.0002 + 0.0001) * 60_000.0)


class TestTheImpactHaircut:
    def test_it_grows_with_size(self):
        small, _ = cb.impact_bps_per_leg(REPO, 100.0)
        big, _ = cb.impact_bps_per_leg(REPO, 10_000_000.0)
        assert cb.IMPACT_FLOOR_BPS_PER_LEG <= small < big

    def test_it_refuses_rather_than_guesses_without_depth(self, tmp_path):
        with pytest.raises(SystemExit):
            cb.impact_bps_per_leg(str(tmp_path), 100_000.0)

    def test_it_is_charged_on_four_legs(self):
        rows = [_st(i, 0.0) for i in range(3)]
        r = cb.simulate_settlements_series(rows, notional=60_000.0,
                                           borrow_apr=0.0, impact_bps=2.0,
                                           entry_bps=-1.0)
        assert r["attribution"]["impact_usd"] == pytest.approx(
            -4 * 60_000.0 * 2.0 / 1e4)


class TestTheBinanceClockNeedsItsData:
    def test_absent_8h_files_refuse_with_the_fetch_command(self, tmp_path):
        with pytest.raises(SystemExit) as exc:
            cb.load_binance_settlements(str(tmp_path))
        assert "fetch_settlement_klines" in str(exc.value)


class TestStressIsFirstClass:
    @pytest.fixture(scope="class")
    def stress(self):
        return cb.stress_scenarios(REPO, notional=100_000.0, borrow_apr=0.05)

    def test_the_four_named_scenarios_are_present(self, stress):
        names = " ".join(s["name"] for s in stress["scenarios"])
        for needle in ("2020-03-12", "2022-11", "2024-08-05", "USDT"):
            assert needle in names

    def test_absent_data_is_said_not_skipped(self, stress):
        by = {s["name"]: s for s in stress["scenarios"]}
        covid = next(v for k, v in by.items() if "2020-03-12" in k)
        usdt = next(v for k, v in by.items() if "USDT" in k)
        assert covid["status"] == "DATA_ABSENT"
        assert usdt["status"] == "NOT_MEASURABLE"
        assert covid["reason"] and usdt["reason"]

    def test_available_scenarios_sum(self, stress):
        done = [s for s in stress["scenarios"] if s["status"] == "MEASURED"]
        assert len(done) >= 2
        for s in done:
            a = s["attribution"]
            assert (a["funding_usd"] + a["basis_usd"] + a["borrow_usd"]) == \
                pytest.approx(a["net_usd"], abs=1e-6)

    def test_close_survival_is_not_called_wick_survival(self, stress):
        assert "not wick" in stress["label"].lower()

    def test_the_cli_prints_the_stress_block(self, capsys):
        assert cb.main(["--repo", REPO, "--clock", "8h", "--stress"]) == 0
        assert "STRESS" in capsys.readouterr().out


class TestReadsAreRecorded:
    def test_both_clocks_record_what_they_read(self, tmp_path, monkeypatch):
        ledger = tmp_path / "ledger.json"
        monkeypatch.setattr(cb, "LEDGER_PATH", str(ledger))
        monkeypatch.delenv("LEDGER_DISABLED", raising=False)
        cb.simulate(REPO, notional=100_000.0)
        cb.simulate_settlement(REPO, venue="bybit", notional=100_000.0,
                               borrow_apr=0.05)
        reads = json.loads(ledger.read_text())["reads"]
        datasets = {r["dataset"] for r in reads}
        for name in ("BINANCE_LINEAR_BTC_USDT_1D", "BINANCE_SPOT_BTC_USDT_1D",
                     "BINANCE_LINEAR_BTC_USDT_FUNDING",
                     "BYBIT_SPOT_BTC_USDT_4H", "BYBIT_LINEAR_BTC_USDT_4H",
                     "BYBIT_LINEAR_BTC_USDT_FUNDING"):
            assert name in datasets, name
        assert all(r["read_by"].endswith("carry_backtest.py") for r in reads)

    def test_a_test_run_records_nothing(self, tmp_path, monkeypatch):
        ledger = tmp_path / "ledger.json"
        monkeypatch.setattr(cb, "LEDGER_PATH", str(ledger))
        monkeypatch.setenv("LEDGER_DISABLED", "1")
        cb.simulate(REPO, notional=100_000.0)
        assert not ledger.exists()


class TestTheSettlementCli:
    def test_the_8h_run_prints_its_clock_and_conditions(self, capsys):
        assert cb.main(["--repo", REPO, "--clock", "8h"]) == 0
        out = capsys.readouterr().out
        assert "00/08/16" in out
        assert "= NET" in out
        for name in cb.QUOTABLE_CONDITIONS:
            assert name in out

    def test_the_8h_matrix_runs(self, capsys):
        assert cb.main(["--repo", REPO, "--clock", "8h", "--matrix"]) == 0
        assert "MATRIX" in capsys.readouterr().out


class TestTheLedgerIsReachableFromTheCli:
    def test_the_ledger_module_loads_when_run_as_a_script(self, tmp_path):
        """Run as `python3 tools/carry_backtest.py`, only tools/ is on the
        path. install() imports market_data, which lives one level up; it
        raised, the bare except set the ledger to None, and no CLI run ever
        recorded a read. The tests never saw it because pytest puts bot/ on
        the path."""
        import subprocess
        tools = os.path.abspath(os.path.join(REPO, "tools"))
        code = ("import sys; sys.path[:] = [%r] + [p for p in sys.path "
                "if p and not p.rstrip('/').endswith('bot')]; "
                "import carry_backtest as cb; "
                "print(cb._read_ledger is not None)") % tools
        out = subprocess.run([sys.executable, "-c", code], cwd=str(tmp_path),
                             capture_output=True, text=True, timeout=60)
        assert out.stdout.strip().endswith("True"), out.stderr


# ---------------------------------------------------------------------------
# 0036 — the overlay never trades the client's spot
# ---------------------------------------------------------------------------

class TestTheOverlayModeCostsLess:
    @pytest.fixture(scope="class")
    def pair(self):
        common = dict(venue="bybit", notional=100_000.0, borrow_apr=0.0)
        return (cb.simulate_settlement(REPO, mode=cb.ACQUIRE, **common),
                cb.simulate_settlement(REPO, mode=cb.OVERLAY, **common))

    def test_the_mode_and_its_round_trip_are_reported(self, pair):
        acquire, overlay = pair
        assert acquire["execution_mode"] == "acquire"
        assert overlay["execution_mode"] == "overlay"
        assert acquire["round_trip_bps"] == pytest.approx(31.0)
        assert overlay["round_trip_bps"] == pytest.approx(11.0)

    def test_the_overlay_pays_only_the_perp_legs(self, pair):
        acquire, overlay = pair
        ratio = overlay["attribution"]["fees_usd"] / \
            acquire["attribution"]["fees_usd"]
        # 11 bps of perp legs out of a 31 bps round trip; the spot and perp
        # prices differ by the basis, so this is not exact to the last digit.
        assert ratio == pytest.approx(11.0 / 31.0, rel=1e-3)

    def test_same_funding_same_basis_fewer_fees(self, pair):
        acquire, overlay = pair
        for term in ("funding_usd", "basis_usd"):
            assert overlay["attribution"][term] == pytest.approx(
                acquire["attribution"][term])
        assert overlay["net_usd"] > acquire["net_usd"]

    def test_the_attribution_still_sums(self, pair):
        for r in pair:
            a = r["attribution"]
            assert (a["funding_usd"] + a["basis_usd"] + a["fees_usd"]
                    + a["borrow_usd"] + a["impact_usd"] + a["other_usd"]) == \
                pytest.approx(a["net_usd"], abs=1e-6)

    def test_it_is_still_not_quotable(self, pair):
        for r in pair:
            assert r["is_a_quotable_return"] is False

    def test_the_gate_sees_the_mode_s_round_trip(self):
        """A gate charging 31 bps to a book that pays 11 refuses entries that
        pay for themselves. Same data, same constants, different execution."""
        common = dict(venue="bybit", notional=100_000.0, borrow_apr=0.05,
                      gated=True)
        assert cb.simulate_settlement(REPO, mode=cb.OVERLAY, **common)["trades"] \
            > cb.simulate_settlement(REPO, mode=cb.ACQUIRE, **common)["trades"]

    def test_the_cli_runs_both_modes(self, capsys):
        assert cb.main(["--repo", REPO, "--clock", "8h", "--mode",
                        "overlay"]) == 0
        out = capsys.readouterr().out
        assert "OVERLAY" in out and "round trip 11.0 bps" in out

    def test_the_daily_clock_takes_the_mode_too(self):
        r = cb.simulate(REPO, mode=cb.OVERLAY, borrow_apr=0.0)
        assert r["execution_mode"] == "overlay"
        assert r["round_trip_bps"] == pytest.approx(11.0)
