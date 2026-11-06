"""A headline return says nothing about HOW it was earned. The replay now also
reports per-trade net, how much of the profit one trade supplied, and the worst
peak-to-trough fall, and checks that the per-trade numbers add back to the
headline. Hand-computed cases; offline."""
from __future__ import annotations

import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "tools"))
sys.path.insert(0, os.path.dirname(HERE))

import carry_replay as rp  # noqa: E402
from test_carry_replay import rows  # noqa: E402


def trade(opened, closed, funding, perp=(100.0, 100.0), spot=(100.0, 100.0), qty=1.0):
    return {"opened_ms": opened, "closed_ms": closed, "funding": funding,
            "qty": qty, "spot_qty": qty, "entry_perp": perp[0], "exit_perp": perp[1],
            "entry_spot": spot[0], "exit_spot": spot[1], "prints_held": 3,
            "entry_index": 0, "reason": "X"}


def test_per_trade_net_is_funding_plus_both_legs_minus_fees_and_borrow():
    # short perp loses 5 (100->105), long spot gains 5 (100->105): basis 0
    t = trade(1, 9, funding=10.0, perp=(100.0, 105.0), spot=(100.0, 105.0))
    out = rp.trade_concentration([t], notional=1000.0,
                                 fee_events=[(1, 0.3), (9, 0.2)],
                                 borrow_events=[(5, 1.0)])
    assert out["trade_nets_usd"] == [pytest.approx(10.0 - 0.5 - 1.0)]


def test_best_trade_and_top_five_share_of_net():
    nets = [100.0, 10.0, 10.0, 10.0, 10.0, 10.0, -10.0]       # total 140
    trades = [trade(i * 10, i * 10 + 5, n) for i, n in enumerate(nets)]
    out = rp.trade_concentration(trades, notional=1000.0)
    assert out["net_usd"] == pytest.approx(140.0)
    assert out["best_trade_usd"] == pytest.approx(100.0)
    assert out["best_trade_share_of_net"] == pytest.approx(100.0 / 140.0)
    assert out["top5_share_of_net"] == pytest.approx(140.0 / 140.0)   # 100+10*4
    assert out["losing_trade_pct"] == pytest.approx(100.0 / 7)


def test_max_drawdown_is_the_worst_peak_to_trough_of_cumulative_closed_net():
    # cumulative: 10, -20, -15, -25  -> peak 10, trough -25 -> drawdown 35
    trades = [trade(0, 1, 10.0), trade(2, 3, -30.0), trade(4, 5, 5.0),
              trade(6, 7, -10.0)]
    out = rp.trade_concentration(trades, notional=1000.0)
    assert out["max_drawdown_usd"] == pytest.approx(35.0)
    assert out["max_drawdown_pct_of_notional"] == pytest.approx(3.5)


def test_drawdown_is_ordered_by_close_time_not_list_order():
    a, b = trade(0, 9, -30.0), trade(0, 1, 10.0)       # b closes first
    out = rp.trade_concentration([a, b], notional=1000.0)
    assert out["max_drawdown_usd"] == pytest.approx(30.0)       # +10 then -30 = -20: dd 30


def test_a_net_that_is_not_positive_has_no_share_to_report():
    out = rp.trade_concentration([trade(0, 1, -5.0), trade(2, 3, 2.0)], notional=100.0)
    assert out["best_trade_share_of_net"] is None and out["top5_share_of_net"] is None


def test_no_trades_is_reported_as_none_not_zero_edge():
    out = rp.trade_concentration([], notional=100.0)
    assert out["trades"] == 0 and out["best_trade_share_of_net"] is None
    assert out["max_drawdown_usd"] == 0.0


def test_the_per_trade_sum_is_checked_against_the_headline():
    t = [trade(0, 1, 10.0)]
    assert rp.trade_concentration(t, notional=100.0, net_usd=10.0)["reconciles_to_net"] is True
    assert rp.trade_concentration(t, notional=100.0, net_usd=99.0)["reconciles_to_net"] is False


class TestOnARealReplay:
    def test_the_replay_reports_it_and_it_adds_back_to_the_headline(self):
        data = rows(150, cycle=12)
        report = rp.replay(data, notional=100_000.0, borrow_apr=0.05)
        c = report["concentration"]
        assert c["trades"] == report["trades"] > 1
        assert c["reconciles_to_net"] is True
        assert sum(c["trade_nets_usd"]) == pytest.approx(report["net_usd"], abs=0.01)
        assert 0.0 <= c["losing_trade_pct"] <= 100.0
        assert c["max_drawdown_usd"] >= 0.0


class TestThePrintedReportShowsIt:
    def test_the_lines_state_the_concentration_and_the_without_best_figure(self):
        trades = [trade(i * 10, i * 10 + 5, n) for i, n in enumerate([100.0, 10.0, -10.0])]
        c = rp.trade_concentration(trades, notional=1000.0)
        text = "\n".join(rp.concentration_lines(c, years=1.0, notional=1000.0))
        assert "best trade" in text and "100.0% of net" in text
        assert "without the best trade" in text

    def test_nothing_is_printed_for_a_replay_with_no_trades(self):
        c = rp.trade_concentration([], notional=1000.0)
        assert rp.concentration_lines(c, years=1.0, notional=1000.0) == []


class TestTheCaptionSaysWhatWasCharged:
    """The 'without the best trade' figure comes from per-trade nets that already
    include whatever borrow the run charged. The caption used to say 'before
    financing' on every run, which is false the moment a borrow rate is set."""

    def _lines(self, borrow_apr):
        trades = [trade(0, 5, 100.0), trade(10, 15, 10.0)]
        c = rp.trade_concentration(trades, notional=1000.0)
        return "\n".join(rp.concentration_lines(
            c, years=1.0, notional=1000.0, borrow_apr=borrow_apr))

    def test_with_no_borrow_it_says_none_was_charged(self):
        assert "0% borrow" in self._lines(0.0)
        assert "before financing" not in self._lines(0.0)

    def test_with_borrow_it_says_the_rate_that_was_charged(self):
        assert "after 5% borrow" in self._lines(0.05)
        assert "before financing" not in self._lines(0.05)


class TestReturnOnTotalCapital:
    """The headline is a return on NOTIONAL. A hedged book also posts margin for
    the short perp, so the money tied up is the spot leg plus that margin. The
    margin fraction is a stated assumption, never a venue figure."""

    def test_total_capital_is_spot_notional_plus_assumed_margin(self):
        out = rp.return_on_total_capital(net_annualised_pct=8.0, margin_fractions=(0.10, 0.25))
        assert out[0.10] == pytest.approx(8.0 / 1.10)
        assert out[0.25] == pytest.approx(8.0 / 1.25)

    def test_a_negative_return_stays_negative(self):
        assert rp.return_on_total_capital(-2.0, (0.2,))[0.2] == pytest.approx(-2.0 / 1.2)

    def test_the_lines_name_the_assumption_and_say_it_is_not_the_venues(self):
        text = "\n".join(rp.capital_lines(8.05))
        assert "10%" in text and "20%" in text and "33%" in text
        assert "assumed" in text and "not the venue" in text.lower()

    def test_an_unusable_fraction_is_refused(self):
        with pytest.raises(ValueError):
            rp.return_on_total_capital(8.0, (-0.1,))


# ---------------------------------------------------------------------------------------------------------------- return denominators
from types import SimpleNamespace as _NS  # noqa: E402


def _row(perp, rate):
    return _NS(perp=perp, rate=rate)


def test_the_return_bases_by_hand_for_one_trade_whose_price_rises():
    # entered at index 0 at 100, three prints held (indices 1..3) while the perp marks 110, 120, 130; rate 1 bp per print; qty 1
    rows_ = [_row(100.0, 0.0), _row(110.0, 1e-4), _row(120.0, 1e-4), _row(130.0, 1e-4)]
    t = trade(0, 3, funding=0.0, perp=(100.0, 130.0), qty=1.0)
    funding_at_mark = 1e-4 * (110.0 + 120.0 + 130.0)                     # 0.036: what the engine books (rate x qty x MARK)
    funding_at_entry = 1e-4 * 100.0 * 3                                   # 0.030: the same prints priced at the entry
    net = funding_at_mark - 0.01                                          # 0.01 of fees
    out = rp.denominator_report([dict(t, funding=funding_at_mark)], rows_, net_usd=net, funding_usd=funding_at_mark, years=1.0, notional=100.0)
    assert out["reconciles"] is True and out["held_prints"] == 3
    assert out["funding_recomputed_at_mark_usd"] == pytest.approx(0.036) and out["funding_at_entry_notional_usd"] == pytest.approx(0.030)
    assert out["avg_marked_hedged_notional_usd"] == pytest.approx(120.0) and out["marked_over_entry_ratio"] == pytest.approx(1.2)
    assert out["on_entry_notional_pct"] == pytest.approx(100.0 * 0.026 / 100.0)                       # the headline, unchanged
    assert out["drift_neutral_pct"] == pytest.approx(100.0 * (0.026 - 0.036 + 0.030) / 100.0)        # 0.020
    assert out["on_avg_marked_notional_pct"] == pytest.approx(100.0 * 0.026 / 120.0)
    assert out["by_margin"][1.0] == pytest.approx(100.0 * 0.026 / 240.0) and out["by_margin"][0.0] == pytest.approx(out["on_avg_marked_notional_pct"])


def test_at_a_constant_price_every_basis_agrees():
    rows_ = [_row(100.0, 0.0)] + [_row(100.0, 2e-4)] * 5
    t = trade(0, 5, funding=0.0, perp=(100.0, 100.0), qty=2.0)
    f = 2e-4 * 2.0 * 100.0 * 5
    out = rp.denominator_report([dict(t, funding=f, prints_held=5)], rows_, net_usd=f, funding_usd=f, years=1.0, notional=200.0)
    assert out["marked_over_entry_ratio"] == pytest.approx(1.0)
    assert out["drift_neutral_pct"] == pytest.approx(out["on_entry_notional_pct"]) == pytest.approx(out["on_avg_marked_notional_pct"])


def test_a_falling_price_makes_the_headline_understate_and_the_drift_neutral_figure_exceed_it():
    rows_ = [_row(100.0, 0.0), _row(90.0, 1e-4), _row(80.0, 1e-4)]
    t = trade(0, 2, funding=0.0, perp=(100.0, 80.0), qty=1.0)
    f = 1e-4 * (90.0 + 80.0)
    out = rp.denominator_report([dict(t, funding=f, prints_held=2)], rows_, net_usd=f, funding_usd=f, years=1.0, notional=100.0)
    assert out["marked_over_entry_ratio"] < 1.0 and out["drift_neutral_pct"] > out["on_entry_notional_pct"]


def test_when_the_funding_cannot_be_reproduced_from_the_held_prints_nothing_is_claimed():
    rows_ = [_row(100.0, 0.0), _row(110.0, 1e-4), _row(120.0, 1e-4)]
    t = trade(0, 2, funding=0.0, perp=(100.0, 120.0), qty=1.0)
    out = rp.denominator_report([dict(t, prints_held=2)], rows_, net_usd=1.0, funding_usd=0.5, years=1.0, notional=100.0)        # the engine's funding is not rate x qty x mark
    assert out["reconciles"] is False and out["drift_neutral_pct"] is None and out["on_avg_marked_notional_pct"] is None
    assert "not claimed" in rp.denominator_lines(out)[0]
    empty = rp.denominator_report([], rows_, net_usd=0.0, funding_usd=0.0, years=1.0, notional=100.0)
    assert empty["held_prints"] == 0 and empty["drift_neutral_pct"] is None


def test_the_real_replay_reports_the_bases_and_the_headline_is_untouched():
    data = rows(120, rate=0.0006)
    report = rp.replay(data, notional=100_000.0, borrow_apr=0.0)
    d = report["denominators"]
    assert report["net_annualised_pct"] == pytest.approx(d["on_entry_notional_pct"])
    assert d["reconciles"] is True, d                        # the engine's own funding IS rate x qty x mark over the held prints
    assert d["funding_recomputed_at_mark_usd"] == pytest.approx(report["funding_usd"], abs=1e-6)
    assert d["marked_over_entry_ratio"] > 1.0                # the synthetic corpus rises 0.02 % a print
    assert d["drift_neutral_pct"] < d["on_entry_notional_pct"]
