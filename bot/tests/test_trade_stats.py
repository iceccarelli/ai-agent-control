"""Tests for ``trade_stats`` — the payoff-geometry measurement.

The property these defend is not that the numbers are pretty. It is that the
module can reproduce, as arithmetic, the finding slice 8 measured on real data:
**a 54% win rate with 2:1-against payoffs loses money**, and says so by putting
the observed hit rate next to the break-even hit rate its own payoffs imply.

Everything here is offline and hand-computable. Where a number is asserted, it
was worked out on paper first and the formula is written into the test, so a
test that passes for the wrong reason has to be wrong in the same way twice.
"""
from __future__ import annotations

import ast
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import trade_stats as ts  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ---------------------------------------------------------------------------
# builders
# ---------------------------------------------------------------------------


def long_trade(r: float, *, entry: float = 100.0, stop: float = 90.0,
               qty: float = 1.0, fees: float = 0.0, **extra) -> ts.Trade:
    """A long whose fee-exclusive R is exactly ``r``.

    Risk per unit is ``entry - stop``, so the exit that produces ``r`` is
    ``entry + r * (entry - stop)``. Stated as a formula rather than as a magic
    number so the test cannot drift away from the definition it is checking.
    """
    risk = entry - stop
    return ts.Trade(
        entry_price=entry, exit_price=entry + r * risk, qty=qty, side="Buy",
        initial_stop_price=stop, entry_fee=fees / 2.0, exit_fee=fees / 2.0,
        **extra,
    )


def short_trade(r: float, *, entry: float = 100.0, stop: float = 110.0,
                qty: float = 1.0, fees: float = 0.0, **extra) -> ts.Trade:
    risk = stop - entry
    return ts.Trade(
        entry_price=entry, exit_price=entry - r * risk, qty=qty, side="Sell",
        initial_stop_price=stop, entry_fee=fees / 2.0, exit_fee=fees / 2.0,
        **extra,
    )


def sample(win_r: float, wins: int, loss_r: float, losses: int, **extra):
    """A sample with an exactly known hit rate and payoff ratio."""
    return (
        [long_trade(win_r, exit_reason="tp", **extra) for _ in range(wins)]
        + [long_trade(-abs(loss_r), exit_reason="stop", **extra)
           for _ in range(losses)]
    )


# ---------------------------------------------------------------------------
# direction
# ---------------------------------------------------------------------------


class TestDirection:
    @pytest.mark.parametrize("side", ["Buy", "buy", "BUY", "long", "Long", " b "])
    def test_long_words_are_plus_one(self, side):
        assert ts.direction_of(side) == 1.0

    @pytest.mark.parametrize("side", ["Sell", "sell", "SELL", "short", "Short"])
    def test_short_words_are_minus_one(self, side):
        assert ts.direction_of(side) == -1.0

    @pytest.mark.parametrize("side", ["Sideways", "", "hold", "none", "1", None])
    def test_an_unrecognised_side_raises_rather_than_defaulting_to_long(self, side):
        """Slice 5 matched sides by prefix and turned "Sideways" into "Sell".

        Guessing a direction silently flips the sign of every statistic derived
        from the trade, so there is no default here.
        """
        with pytest.raises(ValueError):
            ts.direction_of(side)


# ---------------------------------------------------------------------------
# the central formula
# ---------------------------------------------------------------------------


class TestRMultiple:
    def test_hand_computed_long(self):
        """Entry 100, stop 90, exit 120: risked 10, made 20, so +2.00R."""
        r = ts.r_multiple(entry_price=100.0, exit_price=120.0, qty=1.0,
                          side="Buy", initial_stop_price=90.0)
        assert r == pytest.approx(2.0)

    def test_hand_computed_short(self):
        """Short 100 with the stop at 110, covered at 85: risked 10, made 15."""
        r = ts.r_multiple(entry_price=100.0, exit_price=85.0, qty=1.0,
                          side="Sell", initial_stop_price=110.0)
        assert r == pytest.approx(1.5)

    def test_a_long_losing_trade_is_negative(self):
        r = ts.r_multiple(entry_price=100.0, exit_price=95.0, qty=1.0,
                          side="Buy", initial_stop_price=90.0)
        assert r == pytest.approx(-0.5)

    def test_a_short_that_moves_against_it_is_negative(self):
        r = ts.r_multiple(entry_price=100.0, exit_price=105.0, qty=1.0,
                          side="Sell", initial_stop_price=110.0)
        assert r == pytest.approx(-0.5)

    def test_a_long_stopped_out_exactly_at_its_stop_is_minus_one(self):
        """The definition of R, expressed as its own boundary case."""
        r = ts.r_multiple(entry_price=27_400.0, exit_price=26_900.0, qty=0.37,
                          side="Buy", initial_stop_price=26_900.0)
        assert r == pytest.approx(-1.0)

    def test_a_short_stopped_out_exactly_at_its_stop_is_minus_one(self):
        r = ts.r_multiple(entry_price=27_400.0, exit_price=27_900.0, qty=0.37,
                          side="Sell", initial_stop_price=27_900.0)
        assert r == pytest.approx(-1.0)

    @pytest.mark.parametrize("qty", [0.001, 1.0, 17.5, 1_000.0])
    def test_quantity_cancels_when_there_are_no_fees(self, qty):
        """R is size-independent, which is what makes trades comparable."""
        r = ts.r_multiple(entry_price=100.0, exit_price=115.0, qty=qty,
                          side="Buy", initial_stop_price=90.0)
        assert r == pytest.approx(1.5)

    def test_quantity_does_not_cancel_once_fees_are_involved(self):
        """Fees are absolute, so their cost in R depends on the size taken."""
        small = ts.r_multiple(entry_price=100.0, exit_price=110.0, qty=1.0,
                              side="Buy", initial_stop_price=90.0, fees=1.0)
        large = ts.r_multiple(entry_price=100.0, exit_price=110.0, qty=10.0,
                              side="Buy", initial_stop_price=90.0, fees=1.0)
        assert small == pytest.approx(0.9)
        assert large == pytest.approx(0.99)

    def test_fees_are_subtracted_in_r_units(self):
        r = ts.r_multiple(entry_price=100.0, exit_price=120.0, qty=2.0,
                          side="Buy", initial_stop_price=90.0, fees=4.0)
        # risk = 10 * 2 = 20; gross = 40; net = 36 -> 1.8R
        assert r == pytest.approx(1.8)

    def test_a_zero_width_stop_is_refused_not_reported_as_infinite(self):
        with pytest.raises(ValueError, match="zero-width"):
            ts.r_multiple(entry_price=100.0, exit_price=110.0, qty=1.0,
                          side="Buy", initial_stop_price=100.0)

    def test_a_zero_quantity_is_refused(self):
        with pytest.raises(ValueError):
            ts.r_multiple(entry_price=100.0, exit_price=110.0, qty=0.0,
                          side="Buy", initial_stop_price=90.0)

    def test_a_nan_stop_is_refused(self):
        with pytest.raises(ValueError):
            ts.r_multiple(entry_price=100.0, exit_price=110.0, qty=1.0,
                          side="Buy", initial_stop_price=float("nan"))


# ---------------------------------------------------------------------------
# the record
# ---------------------------------------------------------------------------


class TestTradeRecord:
    def test_gross_and_net_pnl_for_a_long(self):
        trade = ts.Trade(entry_price=100.0, exit_price=110.0, qty=2.0,
                         side="Buy", initial_stop_price=95.0,
                         entry_fee=0.5, exit_fee=0.7)
        assert trade.gross_pnl == pytest.approx(20.0)
        assert trade.fees == pytest.approx(1.2)
        assert trade.net_pnl == pytest.approx(18.8)

    def test_gross_and_net_pnl_for_a_short(self):
        trade = ts.Trade(entry_price=100.0, exit_price=90.0, qty=2.0,
                         side="Sell", initial_stop_price=105.0,
                         entry_fee=0.5, exit_fee=0.5)
        assert trade.gross_pnl == pytest.approx(20.0)
        assert trade.net_pnl == pytest.approx(19.0)

    def test_risk_amount_is_stop_distance_times_size(self):
        trade = long_trade(1.0, entry=100.0, stop=94.0, qty=3.0)
        assert trade.risk_per_unit == pytest.approx(6.0)
        assert trade.risk_amount == pytest.approx(18.0)

    def test_stop_distance_in_bps(self):
        trade = long_trade(1.0, entry=100.0, stop=99.0)
        assert trade.stop_distance_bps == pytest.approx(100.0)

    def test_a_missing_stop_makes_r_none_rather_than_raising(self):
        """Missing and impossible are different. This one is merely unknown."""
        trade = ts.Trade(entry_price=100.0, exit_price=110.0, qty=1.0,
                         side="Buy", initial_stop_price=None)
        assert trade.r_net is None
        assert trade.r_gross is None
        assert trade.risk_amount is None
        assert trade.is_win is None

    def test_a_stop_equal_to_entry_is_refused_at_construction(self):
        with pytest.raises(ValueError, match="not on the losing side"):
            ts.Trade(entry_price=100.0, exit_price=110.0, qty=1.0,
                     side="Buy", initial_stop_price=100.0)

    def test_a_long_stop_above_entry_is_refused(self):
        """A stop on the profitable side is not a stop; the risk is negative."""
        with pytest.raises(ValueError, match="not on the losing side"):
            ts.Trade(entry_price=100.0, exit_price=110.0, qty=1.0,
                     side="Buy", initial_stop_price=105.0)

    def test_a_short_stop_below_entry_is_refused(self):
        with pytest.raises(ValueError, match="not on the losing side"):
            ts.Trade(entry_price=100.0, exit_price=90.0, qty=1.0,
                     side="Sell", initial_stop_price=95.0)

    @pytest.mark.parametrize("field,value", [
        ("entry_price", 0.0), ("entry_price", -1.0), ("exit_price", 0.0),
        ("qty", 0.0), ("qty", -2.0), ("entry_price", float("nan")),
        ("exit_price", float("inf")),
    ])
    def test_impossible_prices_and_sizes_are_refused(self, field, value):
        kwargs = dict(entry_price=100.0, exit_price=110.0, qty=1.0,
                      side="Buy", initial_stop_price=90.0)
        kwargs[field] = value
        with pytest.raises(ValueError):
            ts.Trade(**kwargs)

    def test_an_exit_before_its_entry_is_refused(self):
        with pytest.raises(ValueError, match="precedes"):
            ts.Trade(entry_price=100.0, exit_price=110.0, qty=1.0, side="Buy",
                     initial_stop_price=90.0, entry_index=10, exit_index=9)

    def test_bars_held_is_the_index_difference(self):
        trade = long_trade(1.0, entry_index=100, exit_index=137)
        assert trade.bars_held == 37

    def test_bars_held_is_none_without_indices(self):
        assert long_trade(1.0).bars_held is None

    def test_a_win_is_fee_aware(self):
        """Gross positive, net negative: that is a LOSS, as everywhere else."""
        trade = ts.Trade(entry_price=100.0, exit_price=100.5, qty=1.0,
                         side="Buy", initial_stop_price=90.0,
                         entry_fee=0.4, exit_fee=0.4)
        assert trade.gross_pnl > 0
        assert trade.net_pnl < 0
        assert trade.is_win is False


class TestTradeFromMapping:
    def test_builds_from_a_ledger_shaped_row(self):
        trade = ts.Trade.from_mapping({
            "symbol": "BTCUSDT", "side": "Buy", "qty": 2.0,
            "entry_price": 100.0, "exit_price": 120.0,
            "entry_fee": 0.2, "exit_fee": 0.24,
            "initial_stop_price": 90.0, "entry_index": 5, "exit_index": 9,
            "exit_reason": "tp",
        })
        assert trade.r_gross == pytest.approx(2.0)
        assert trade.bars_held == 4
        assert trade.canonical_reason == "take_profit"

    @pytest.mark.parametrize("missing", ["entry_price", "exit_price", "qty", "side"])
    def test_a_missing_required_field_raises(self, missing):
        row = {"entry_price": 100.0, "exit_price": 110.0, "qty": 1.0,
               "side": "Buy"}
        row.pop(missing)
        with pytest.raises(ValueError, match="missing required field"):
            ts.Trade.from_mapping(row)

    def test_a_stop_price_of_zero_is_read_as_not_recorded(self):
        """``positions.stop_price`` defaults to 0.0 before the bracket exists."""
        trade = ts.Trade.from_mapping({
            "entry_price": 100.0, "exit_price": 110.0, "qty": 1.0,
            "side": "Buy", "stop_price": 0.0,
        })
        assert trade.initial_stop_price is None
        assert trade.r_net is None

    def test_a_recorded_gross_pnl_that_disagrees_with_the_prices_is_refused(self):
        with pytest.raises(ValueError, match="internally inconsistent"):
            ts.Trade.from_mapping({
                "entry_price": 100.0, "exit_price": 110.0, "qty": 1.0,
                "side": "Buy", "initial_stop_price": 90.0, "gross_pnl": -10.0,
            })

    def test_a_side_written_the_wrong_way_round_is_caught_by_that_check(self):
        """The failure this cross-check exists for: PnL right, every R inverted.

        A long that exited below its entry lost money. A ledger row claiming it
        made money is a row whose side was written the wrong way round, and
        without this check the PnL would still reconcile while every R-multiple
        derived from it carried the wrong sign.
        """
        with pytest.raises(ValueError, match="internally inconsistent"):
            ts.Trade.from_mapping({
                "entry_price": 100.0, "exit_price": 90.0, "qty": 1.0,
                "side": "Buy", "initial_stop_price": 85.0, "gross_pnl": 10.0,
            })

    def test_a_consistent_gross_pnl_is_accepted(self):
        trade = ts.Trade.from_mapping({
            "entry_price": 100.0, "exit_price": 90.0, "qty": 1.5,
            "side": "Sell", "initial_stop_price": 110.0, "gross_pnl": 15.0,
        })
        # risk per unit 10, qty 1.5 -> risk amount 15; gross 15 -> exactly 1R.
        assert trade.r_gross == pytest.approx(1.0)

    def test_epoch_aliases_are_accepted_for_the_time_axis(self):
        trade = ts.Trade.from_mapping({
            "entry_price": 100.0, "exit_price": 110.0, "qty": 1.0, "side": "Buy",
            "initial_stop_price": 90.0,
            "opened_epoch": 1_600_000_000.0, "closed_epoch": 1_600_003_600.0,
        })
        assert trade.bars_held == pytest.approx(3600.0)

    def test_explicit_indices_win_over_the_epoch_aliases(self):
        trade = ts.Trade.from_mapping({
            "entry_price": 100.0, "exit_price": 110.0, "qty": 1.0, "side": "Buy",
            "initial_stop_price": 90.0, "entry_index": 3, "exit_index": 8,
            "opened_epoch": 1_600_000_000.0, "closed_epoch": 1_600_003_600.0,
        })
        assert trade.bars_held == 5


# ---------------------------------------------------------------------------
# fee-inclusive versus fee-exclusive
# ---------------------------------------------------------------------------


class TestFeeVariants:
    def test_the_two_r_variants_differ_by_exactly_the_fees(self):
        trade = ts.Trade(entry_price=100.0, exit_price=120.0, qty=2.0,
                         side="Buy", initial_stop_price=90.0,
                         entry_fee=0.6, exit_fee=0.9)
        assert trade.risk_amount == pytest.approx(20.0)
        assert trade.r_gross - trade.r_net == pytest.approx(1.5 / 20.0)
        assert trade.r_fees == pytest.approx(0.075)

    def test_zero_fees_make_the_variants_identical(self):
        trade = long_trade(1.7)
        assert trade.r_gross == trade.r_net
        assert trade.r_fees == 0.0

    def test_aggregate_totals_reconcile(self):
        trades = sample(1.0, 20, 1.0, 20, fees=2.0)
        stats = ts.compute_trade_stats(trades)
        assert (stats.total_r_gross - stats.total_r_net
                == pytest.approx(stats.total_r_fees))

    def test_expectancies_differ_by_the_mean_drag(self):
        trades = sample(2.0, 20, 1.0, 20, fees=1.0)
        stats = ts.compute_trade_stats(trades)
        assert (stats.expectancy_r_gross - stats.expectancy_r
                == pytest.approx(stats.realised_cost_drag_r))

    def test_fees_can_turn_a_positive_geometry_negative(self):
        """The whole reason both variants are reported."""
        trades = sample(1.05, 20, 1.0, 20, fees=1.2)   # risk amount is 10.0
        stats = ts.compute_trade_stats(trades)
        assert stats.expectancy_r_gross > 0
        assert stats.expectancy_r < 0


# ---------------------------------------------------------------------------
# break-even hit rate — the headline comparison
# ---------------------------------------------------------------------------


class TestBreakEvenHitRate:
    def test_two_to_one_needs_one_third(self):
        """33.3% is the number slice 8's labelling study ran into."""
        assert ts.break_even_hit_rate(2.0) == pytest.approx(1.0 / 3.0)

    def test_one_to_one_needs_a_half(self):
        assert ts.break_even_hit_rate(1.0) == pytest.approx(0.5)

    def test_three_to_one_needs_a_quarter(self):
        assert ts.break_even_hit_rate(3.0) == pytest.approx(0.25)

    def test_a_two_to_one_adverse_payoff_needs_two_thirds(self):
        assert ts.break_even_hit_rate(0.5) == pytest.approx(2.0 / 3.0)

    @pytest.mark.parametrize("bad", [0.0, -1.0, float("nan"), float("inf"), None])
    def test_a_payoff_that_cannot_break_even_returns_none_not_one(self, bad):
        assert ts.break_even_hit_rate(bad) is None

    def test_the_computed_break_even_matches_the_observed_payoff(self):
        trades = sample(2.0, 15, 1.0, 25)
        stats = ts.compute_trade_stats(trades)
        assert stats.payoff_ratio == pytest.approx(2.0)
        assert stats.break_even_hit_rate == pytest.approx(1.0 / 3.0)
        assert stats.hit_rate == pytest.approx(15 / 40)
        assert stats.hit_rate_edge == pytest.approx(15 / 40 - 1 / 3)


# ---------------------------------------------------------------------------
# THE slice-8 finding, reproduced as arithmetic
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def stats():
    """54 wins of +0.5R against 46 losses of -1.0R — slice 8's fold 2."""
    return ts.compute_trade_stats(sample(0.5, 54, 1.0, 46))


class TestFiftyFourPercentWinRateLosesMoney:
    """54 wins of +0.5R against 46 losses of −1.0R.

        expectancy   = 0.54 * 0.5 - 0.46 * 1.0 = -0.19R
        payoff ratio = 0.5
        break-even   = 1 / (1 + 0.5) = 66.7%

    Fold 2 of slice 8's walk-forward: 192 trades, 54.2% win rate, negative
    return. This is that result as a unit test.
    """

    def test_the_hit_rate_really_is_fifty_four_percent(self, stats):
        assert stats.hit_rate == pytest.approx(0.54)

    def test_the_payoff_ratio_is_two_to_one_against(self, stats):
        assert stats.payoff_ratio == pytest.approx(0.5)

    def test_the_break_even_hit_rate_is_two_thirds(self, stats):
        assert stats.break_even_hit_rate == pytest.approx(2.0 / 3.0)

    def test_the_observed_hit_rate_is_below_break_even(self, stats):
        assert stats.hit_rate < stats.break_even_hit_rate
        assert stats.hit_rate_edge == pytest.approx(0.54 - 2.0 / 3.0)
        assert stats.hit_rate_edge < 0

    def test_expectancy_is_negative_despite_the_winning_majority(self, stats):
        assert stats.expectancy_r == pytest.approx(-0.19)

    def test_the_profit_factor_is_below_one(self, stats):
        assert stats.profit_factor == pytest.approx(27.0 / 46.0)
        assert stats.profit_factor < 1.0

    def test_more_winners_than_losers_and_still_losing(self, stats):
        assert stats.wins > stats.losses
        assert stats.total_r_net < 0

    def test_the_summary_says_the_hit_rate_is_below_break_even(self, stats):
        text = stats.summary()
        assert "BELOW break-even" in text
        assert "66.7%" in text        # the break-even the payoffs imply
        assert "54.0%" in text        # the hit rate actually achieved

    def test_the_same_hit_rate_with_a_better_payoff_wins(self, stats):
        """The diagnosis is the geometry, not the entry: hold the hit rate at
        54% and widen the payoff to 1:1, and the same signal makes money."""
        better = ts.compute_trade_stats(sample(1.0, 54, 1.0, 46))
        assert better.hit_rate == pytest.approx(stats.hit_rate)
        assert better.expectancy_r > 0
        assert better.hit_rate_edge > 0


class TestShortsAggregate:
    """Shorts must survive the aggregate path, not only the formula.

    Spot cannot short, so the backtester never produces one — which is exactly
    why the sign convention has to be pinned by a test rather than by a run.
    """

    def test_a_winning_short_sample_has_positive_expectancy(self):
        trades = ([short_trade(2.0) for _ in range(15)]
                  + [short_trade(-1.0) for _ in range(25)])
        stats = ts.compute_trade_stats(trades)
        assert stats.hit_rate == pytest.approx(0.375)
        assert stats.payoff_ratio == pytest.approx(2.0)
        assert stats.expectancy_r == pytest.approx(0.125)
        assert stats.hit_rate_edge > 0

    def test_longs_and_shorts_with_mirrored_geometry_agree(self):
        longs = [long_trade(1.5) for _ in range(20)] + [
            long_trade(-1.0) for _ in range(20)]
        shorts = [short_trade(1.5) for _ in range(20)] + [
            short_trade(-1.0) for _ in range(20)]
        assert (ts.compute_trade_stats(longs).expectancy_r
                == pytest.approx(ts.compute_trade_stats(shorts).expectancy_r))

    def test_a_short_stopped_out_is_minus_one_r_through_the_aggregate(self):
        stats = ts.compute_trade_stats([short_trade(-1.0) for _ in range(30)])
        assert stats.expectancy_r == pytest.approx(-1.0)
        assert stats.hit_rate == 0.0


# ---------------------------------------------------------------------------
# small samples
# ---------------------------------------------------------------------------


class TestSmallSamplesReturnNone:
    def test_an_empty_list_is_not_a_zero_edge(self):
        stats = ts.compute_trade_stats([])
        assert stats.trades == 0
        assert stats.hit_rate is None
        assert stats.expectancy_r is None
        assert stats.insufficient_data is True
        assert stats.notes

    def test_twenty_nine_trades_are_not_enough_for_a_rate(self):
        stats = ts.compute_trade_stats(sample(2.0, 15, 1.0, 14))
        assert stats.trades_with_r == 29
        assert stats.hit_rate is None
        assert stats.expectancy_r is None
        assert stats.profit_factor is None
        assert stats.expectancy_r_ci is None

    def test_thirty_trades_are(self):
        stats = ts.compute_trade_stats(sample(2.0, 15, 1.0, 15))
        assert stats.hit_rate is not None
        assert stats.expectancy_r is not None
        assert stats.expectancy_r_ci is not None

    def test_the_counts_are_still_reported_below_the_threshold(self):
        """Counts and sums are facts about the sample; only rates are estimates."""
        stats = ts.compute_trade_stats(sample(2.0, 3, 1.0, 2))
        assert stats.trades == 5
        assert stats.wins == 3
        assert stats.losses == 2
        assert stats.total_r_net == pytest.approx(4.0)

    def test_four_wins_are_not_an_average_win(self):
        stats = ts.compute_trade_stats(sample(2.0, 4, 1.0, 40))
        assert stats.avg_win_r is None
        assert stats.avg_loss_r is not None
        assert stats.payoff_ratio is None
        assert stats.break_even_hit_rate is None
        assert stats.hit_rate_edge is None

    def test_five_wins_are(self):
        stats = ts.compute_trade_stats(sample(2.0, 5, 1.0, 40))
        assert stats.avg_win_r == pytest.approx(2.0)
        assert stats.payoff_ratio == pytest.approx(2.0)

    def test_a_ratio_can_exist_before_a_hit_rate_does(self):
        """Different questions, different sample requirements — so the report
        answers the one it can and returns None for the other."""
        stats = ts.compute_trade_stats(sample(2.0, 6, 1.0, 6))
        assert stats.payoff_ratio == pytest.approx(2.0)
        assert stats.hit_rate is None
        assert stats.hit_rate_edge is None

    def test_a_distribution_needs_ten_observations(self):
        assert ts.Distribution.of([1.0] * 9) is None
        assert ts.Distribution.of([1.0] * 10) is not None

    def test_the_note_explains_the_refusal_rather_than_leaving_a_blank(self):
        stats = ts.compute_trade_stats(sample(2.0, 5, 1.0, 5))
        assert any("30" in note for note in stats.notes)


# ---------------------------------------------------------------------------
# trades without a recorded stop
# ---------------------------------------------------------------------------


class TestMissingStops:
    def test_they_are_excluded_and_counted_not_dropped_silently(self):
        trades = sample(2.0, 20, 1.0, 20) + [
            ts.Trade(entry_price=100.0, exit_price=150.0, qty=1.0, side="Buy")
            for _ in range(3)
        ]
        stats = ts.compute_trade_stats(trades)
        assert stats.trades == 43
        assert stats.trades_with_r == 40
        assert stats.trades_without_stop == 3
        assert any("no initial stop" in note for note in stats.notes)

    def test_a_sample_with_no_stops_at_all_reports_nothing_rather_than_zero(self):
        trades = [
            ts.Trade(entry_price=100.0, exit_price=110.0, qty=1.0, side="Buy")
            for _ in range(50)
        ]
        stats = ts.compute_trade_stats(trades)
        assert stats.trades == 50
        assert stats.trades_with_r == 0
        assert stats.expectancy_r is None
        assert stats.hit_rate is None
        assert any("no R-multiple" in note for note in stats.notes)

    def test_an_excluded_trade_cannot_move_the_expectancy(self):
        with_stops = sample(2.0, 20, 1.0, 20)
        polluted = with_stops + [
            ts.Trade(entry_price=100.0, exit_price=1_000.0, qty=5.0, side="Buy")
        ]
        assert (ts.compute_trade_stats(polluted).expectancy_r
                == pytest.approx(ts.compute_trade_stats(with_stops).expectancy_r))


# ---------------------------------------------------------------------------
# cost drag
# ---------------------------------------------------------------------------


class TestCostDrag:
    def test_the_formula(self):
        """25 bps of round trip against a 100 bps stop is a quarter of an R."""
        assert ts.cost_drag_r(25.0, 100.0) == pytest.approx(0.25)

    @pytest.mark.parametrize("cost,stop,expected", [
        (10.0, 100.0, 0.10), (25.0, 50.0, 0.50), (5.0, 500.0, 0.01),
        (0.0, 100.0, 0.0),
    ])
    def test_more_cases(self, cost, stop, expected):
        assert ts.cost_drag_r(cost, stop) == pytest.approx(expected)

    def test_a_tighter_stop_costs_more_r(self):
        assert ts.cost_drag_r(25.0, 50.0) > ts.cost_drag_r(25.0, 200.0)

    @pytest.mark.parametrize("stop", [0.0, -100.0, float("nan"), float("inf")])
    def test_a_zero_or_impossible_stop_width_is_refused(self, stop):
        with pytest.raises(ValueError):
            ts.cost_drag_r(25.0, stop)

    def test_a_non_finite_cost_is_refused(self):
        with pytest.raises(ValueError):
            ts.cost_drag_r(float("nan"), 100.0)

    def test_realised_drag_is_the_mean_of_fees_over_risk(self):
        trades = sample(2.0, 20, 1.0, 20, fees=1.0)   # risk amount 10.0 each
        stats = ts.compute_trade_stats(trades)
        assert stats.realised_cost_drag_r == pytest.approx(0.1)

    def test_the_modelled_drag_uses_the_mean_stop_width(self):
        trades = sample(2.0, 20, 1.0, 20)             # 100 -> 90 is 1000 bps
        stats = ts.compute_trade_stats(trades, round_trip_cost_bps=25.0)
        assert stats.mean_stop_distance_bps == pytest.approx(1000.0)
        assert stats.modelled_cost_drag_r == pytest.approx(0.025)

    def test_the_modelled_drag_is_absent_unless_a_cost_is_supplied(self):
        stats = ts.compute_trade_stats(sample(2.0, 20, 1.0, 20))
        assert stats.modelled_cost_drag_r is None
        assert stats.round_trip_cost_bps is None

    def test_mean_fee_bps_is_measured_against_entry_notional(self):
        trades = sample(1.0, 20, 1.0, 20, fees=1.0)   # 1.0 on 100.0 notional
        stats = ts.compute_trade_stats(trades)
        assert stats.mean_fee_bps == pytest.approx(100.0)


# ---------------------------------------------------------------------------
# expectancy and its interval
# ---------------------------------------------------------------------------


class TestExpectancy:
    def test_matches_a_hand_computed_example(self):
        """40 trades: 10 at +3R, 30 at −1R.

            (10 * 3 + 30 * -1) / 40 = 0 / 40 = 0.00R — exactly break-even.
        """
        stats = ts.compute_trade_stats(sample(3.0, 10, 1.0, 30))
        assert stats.expectancy_r == pytest.approx(0.0)
        assert stats.hit_rate == pytest.approx(0.25)
        assert stats.break_even_hit_rate == pytest.approx(0.25)
        assert stats.hit_rate_edge == pytest.approx(0.0)

    def test_expectancy_equals_the_mean_of_the_r_multiples(self):
        trades = sample(1.7, 21, 0.9, 19)
        stats = ts.compute_trade_stats(trades)
        expected = sum(t.r_net for t in trades) / len(trades)
        assert stats.expectancy_r == pytest.approx(expected)

    def test_the_interval_brackets_the_point_estimate(self):
        stats = ts.compute_trade_stats(sample(2.0, 20, 1.0, 20))
        low, high = stats.expectancy_r_ci
        assert low <= stats.expectancy_r <= high

    def test_the_interval_is_reproducible(self):
        """A confidence interval that moves between runs is not evidence."""
        trades = sample(2.0, 20, 1.0, 20)
        assert (ts.compute_trade_stats(trades).expectancy_r_ci
                == ts.compute_trade_stats(trades).expectancy_r_ci)

    def test_a_clearly_losing_sample_has_an_interval_that_excludes_zero(self):
        stats = ts.compute_trade_stats(sample(0.5, 54, 1.0, 46))
        assert stats.expectancy_excludes_zero is True
        assert stats.expectancy_r_ci[1] < 0

    def test_a_marginal_sample_says_it_cannot_tell(self):
        """31 trades either side of break-even is not a finding."""
        stats = ts.compute_trade_stats(sample(1.0, 16, 1.0, 15))
        assert stats.expectancy_excludes_zero is False
        assert any("straddles zero" in note for note in stats.notes)
        assert "straddles zero" in stats.summary()

    def test_the_gross_interval_is_computed_too(self):
        stats = ts.compute_trade_stats(sample(2.0, 20, 1.0, 20, fees=1.0))
        assert stats.expectancy_r_gross_ci is not None
        assert stats.expectancy_r_gross_ci[0] > stats.expectancy_r_ci[0]

    def test_a_sample_with_no_variance_gives_a_degenerate_interval(self):
        stats = ts.compute_trade_stats(sample(1.0, 40, 1.0, 0))
        assert stats.expectancy_r_ci == pytest.approx((1.0, 1.0))
        assert stats.expectancy_excludes_zero is True

    def test_profit_factor_is_wins_over_losses(self):
        stats = ts.compute_trade_stats(sample(2.0, 20, 1.0, 20))
        assert stats.profit_factor == pytest.approx(40.0 / 20.0)

    def test_profit_factor_is_none_not_infinite_without_losses(self):
        stats = ts.compute_trade_stats(sample(1.0, 40, 1.0, 0))
        assert stats.profit_factor is None
        assert any("not infinite" in note for note in stats.notes)


# ---------------------------------------------------------------------------
# time to resolution
# ---------------------------------------------------------------------------


class TestTimeToResolution:
    def test_the_overall_distribution_is_measured(self):
        trades = [long_trade(1.0, entry_index=0, exit_index=i)
                  for i in range(1, 21)]
        stats = ts.compute_trade_stats(trades)
        assert stats.bars_held.n == 20
        assert stats.bars_held.minimum == 1
        assert stats.bars_held.maximum == 20
        assert stats.bars_held.median == pytest.approx(10.5)

    def test_winners_resolving_slower_than_losers_is_detected_and_named(self):
        """A specific, fixable defect: capital tied up longest in the trades
        that pay least."""
        trades = (
            [long_trade(1.0, entry_index=0, exit_index=40) for _ in range(20)]
            + [long_trade(-1.0, entry_index=0, exit_index=3) for _ in range(20)]
        )
        stats = ts.compute_trade_stats(trades)
        assert stats.bars_held_wins.median == pytest.approx(40)
        assert stats.bars_held_losses.median == pytest.approx(3)
        assert stats.winners_resolve_slower is True
        assert any("longer to resolve" in note for note in stats.notes)
        assert "SLOWER" in stats.summary()

    def test_winners_resolving_faster_is_not_flagged(self):
        trades = (
            [long_trade(1.0, entry_index=0, exit_index=2) for _ in range(20)]
            + [long_trade(-1.0, entry_index=0, exit_index=30) for _ in range(20)]
        )
        stats = ts.compute_trade_stats(trades)
        assert stats.winners_resolve_slower is False
        assert "SLOWER" not in stats.summary()

    def test_no_indices_means_no_distribution_rather_than_zero(self):
        stats = ts.compute_trade_stats(sample(2.0, 20, 1.0, 20))
        assert stats.bars_held is None
        assert stats.winners_resolve_slower is None
        assert any("time-to-resolution is unmeasured" in n for n in stats.notes)

    def test_the_split_needs_five_of_each(self):
        trades = (
            [long_trade(1.0, entry_index=0, exit_index=5) for _ in range(4)]
            + [long_trade(-1.0, entry_index=0, exit_index=5) for _ in range(30)]
        )
        stats = ts.compute_trade_stats(trades)
        assert stats.bars_held_wins is None
        assert stats.bars_held_losses is not None
        assert stats.winners_resolve_slower is None


# ---------------------------------------------------------------------------
# exit reasons
# ---------------------------------------------------------------------------


class TestExitReasons:
    @pytest.mark.parametrize("raw,canonical", [
        ("stop", "stop"), ("STOP", "stop"), ("stop_loss", "stop"),
        ("sl", "stop"), ("tp", "take_profit"), ("take_profit", "take_profit"),
        ("Target", "take_profit"), ("timeout", "timeout"),
        ("max_hold", "timeout"), ("manual", "manual"), ("human", "manual"),
        ("backtest_end", "end_of_data"), ("", "unknown"), (None, "unknown"),
    ])
    def test_canonicalisation(self, raw, canonical):
        assert ts.canonical_exit_reason(raw) == canonical

    def test_an_unknown_reason_keeps_its_own_bucket(self):
        """Filing an unfamiliar exit under "manual" would report a clean
        four-way split that is not true."""
        assert ts.canonical_exit_reason("BRACKET_FAILED") == "bracket_failed"
        assert ts.canonical_exit_reason("liquidation") == "liquidation"

    def test_the_breakdown_splits_by_reason(self):
        trades = sample(2.0, 20, 1.0, 20)
        stats = ts.compute_trade_stats(trades)
        assert set(stats.by_exit_reason) == {"take_profit", "stop"}
        assert stats.by_exit_reason["stop"].trades == 20
        assert stats.by_exit_reason["take_profit"].trades == 20

    def test_the_shares_sum_to_one(self):
        stats = ts.compute_trade_stats(sample(2.0, 13, 1.0, 27))
        assert sum(b.share for b in stats.by_exit_reason.values()) == pytest.approx(1.0)

    def test_bucket_totals_are_facts_reported_at_any_size(self):
        trades = sample(2.0, 20, 1.0, 20) + [
            long_trade(-0.4, exit_reason="timeout")
        ]
        stats = ts.compute_trade_stats(trades)
        timeout = stats.by_exit_reason["timeout"]
        assert timeout.trades == 1
        assert timeout.total_r == pytest.approx(-0.4)
        assert timeout.hit_rate is None      # an estimate, so gated
        assert timeout.avg_r is None

    def test_a_bucket_of_five_gets_its_rates(self):
        trades = sample(2.0, 20, 1.0, 20) + [
            long_trade(-0.4, exit_reason="timeout") for _ in range(5)
        ]
        stats = ts.compute_trade_stats(trades)
        timeout = stats.by_exit_reason["timeout"]
        assert timeout.trades == 5
        assert timeout.avg_r == pytest.approx(-0.4)
        assert timeout.hit_rate == pytest.approx(0.0)

    def test_the_stop_bucket_carries_the_losses(self):
        stats = ts.compute_trade_stats(sample(2.0, 20, 1.0, 20))
        assert stats.by_exit_reason["stop"].total_r == pytest.approx(-20.0)
        assert stats.by_exit_reason["take_profit"].total_r == pytest.approx(40.0)


# ---------------------------------------------------------------------------
# MAE / MFE
# ---------------------------------------------------------------------------


class TestExcursions:
    def test_mae_and_mfe_in_r_for_a_long(self):
        trade = ts.Trade(entry_price=100.0, exit_price=110.0, qty=1.0,
                         side="Buy", initial_stop_price=90.0,
                         mae_price=95.0, mfe_price=125.0)
        assert trade.mae_r == pytest.approx(0.5)
        assert trade.mfe_r == pytest.approx(2.5)

    def test_mae_and_mfe_in_r_for_a_short(self):
        trade = ts.Trade(entry_price=100.0, exit_price=90.0, qty=1.0,
                         side="Sell", initial_stop_price=110.0,
                         mae_price=105.0, mfe_price=75.0)
        assert trade.mae_r == pytest.approx(0.5)
        assert trade.mfe_r == pytest.approx(2.5)

    def test_a_trade_that_never_went_against_us_has_zero_mae(self):
        trade = ts.Trade(entry_price=100.0, exit_price=110.0, qty=1.0,
                         side="Buy", initial_stop_price=90.0,
                         mae_price=101.0, mfe_price=112.0)
        assert trade.mae_r == 0.0

    def test_swapped_excursions_are_refused(self):
        with pytest.raises(ValueError, match="swapped"):
            ts.Trade(entry_price=100.0, exit_price=110.0, qty=1.0, side="Buy",
                     initial_stop_price=90.0, mae_price=120.0, mfe_price=95.0)

    def test_excursions_are_none_without_a_stop_to_express_them_in(self):
        trade = ts.Trade(entry_price=100.0, exit_price=110.0, qty=1.0,
                         side="Buy", mae_price=95.0, mfe_price=115.0)
        assert trade.mae_r is None
        assert trade.mfe_r is None

    def test_missing_excursions_are_not_approximated_from_the_exit(self):
        """An exit price is not an MFE, and pretending otherwise would make
        every winner look like it was sold at the high."""
        stats = ts.compute_trade_stats(sample(2.0, 20, 1.0, 20))
        assert stats.mae_r is None
        assert stats.mfe_r is None
        assert stats.excursion_coverage == 0
        assert any("rather than approximated" in note for note in stats.notes)

    def test_coverage_is_reported_when_only_some_trades_carry_them(self):
        trades = (
            [long_trade(1.0, mae_price=95.0, mfe_price=115.0) for _ in range(20)]
            + [long_trade(-1.0) for _ in range(20)]
        )
        stats = ts.compute_trade_stats(trades)
        assert stats.excursion_coverage == 20
        assert stats.mae_r.n == 20
        assert any("20 of 40" in note for note in stats.notes)

    def test_the_distribution_is_measured_when_they_are_present(self):
        trades = [long_trade(1.0, mae_price=100.0 - i, mfe_price=100.0 + i)
                  for i in range(1, 21)]
        stats = ts.compute_trade_stats(trades)
        assert stats.mae_r.n == 20
        assert stats.mae_r.minimum == pytest.approx(0.1)
        assert stats.mae_r.maximum == pytest.approx(2.0)


# ---------------------------------------------------------------------------
# the report block
# ---------------------------------------------------------------------------


class TestSummary:
    def test_an_empty_sample_says_so_and_does_not_crash(self):
        text = ts.compute_trade_stats([]).summary()
        assert "no closed trades" in text

    def test_nothing_is_reported_as_a_number_when_it_is_none(self):
        """Six trades: the rates are unknown, and unknown prints as n/a."""
        text = ts.compute_trade_stats(sample(2.0, 3, 1.0, 3)).summary()
        fields = [line for line in text.splitlines()
                  if not line.startswith("note")]
        assert any("hit rate (R)       : n/a" in line for line in fields)
        assert any("expectancy net     : n/a" in line for line in fields)
        assert any("payoff ratio       : n/a" in line for line in fields)
        assert any("break-even hit     : n/a" in line for line in fields)
        assert any("hit-rate edge      : n/a" in line for line in fields)

    def test_the_headline_comparison_is_present(self):
        text = ts.compute_trade_stats(sample(2.0, 20, 1.0, 20)).summary()
        assert "break-even hit" in text
        assert "hit-rate edge" in text

    def test_the_block_is_labelled_and_every_line_is_prefixed(self):
        text = ts.compute_trade_stats(sample(2.0, 20, 1.0, 20)).summary()
        assert text.splitlines()[0].startswith("-- payoff geometry")
        assert "expectancy net" in text

    def test_it_is_a_string_with_no_placeholders_left_in_it(self):
        text = ts.compute_trade_stats(sample(2.0, 20, 1.0, 20, fees=0.5)).summary()
        fields = [line for line in text.splitlines()
                  if not line.startswith("note")]
        assert isinstance(text, str)
        # "None" is prose in a note; it must never be a field's value.
        assert not any("None" in line for line in fields)
        assert "{" not in text
        assert "%s" not in text


# ---------------------------------------------------------------------------
# input handling
# ---------------------------------------------------------------------------


class TestInputHandling:
    def test_mappings_and_trade_objects_agree(self):
        objects = sample(2.0, 20, 1.0, 20)
        mappings = [
            {"entry_price": t.entry_price, "exit_price": t.exit_price,
             "qty": t.qty, "side": t.side,
             "initial_stop_price": t.initial_stop_price,
             "exit_reason": t.exit_reason}
            for t in objects
        ]
        assert (ts.compute_trade_stats(mappings).expectancy_r
                == pytest.approx(ts.compute_trade_stats(objects).expectancy_r))

    def test_a_corrupt_record_raises_rather_than_being_skipped(self):
        """Skipping it would change the result and hide the reason — the same
        rule ``load_bars_csv`` follows for unparseable rows."""
        with pytest.raises(ValueError):
            ts.compute_trade_stats(sample(2.0, 20, 1.0, 20) + [
                {"entry_price": 100.0, "exit_price": 110.0, "qty": 1.0,
                 "side": "Sideways"}
            ])

    def test_an_iterator_is_accepted(self):
        stats = ts.compute_trade_stats(iter(sample(2.0, 20, 1.0, 20)))
        assert stats.trades == 40

    def test_the_input_is_not_mutated(self):
        trades = sample(2.0, 20, 1.0, 20)
        before = list(trades)
        ts.compute_trade_stats(trades)
        assert trades == before


# ---------------------------------------------------------------------------
# structural guards
# ---------------------------------------------------------------------------


class TestStaysStandalone:
    """The module must remain usable from a tool, a test and the backtester.

    That is only true while it depends on nothing in the stack. An import of
    ``config`` or ``persistence`` here would make the analytics answer a
    different question depending on which process asked it.
    """

    ALLOWED = {"__future__", "math", "dataclasses", "typing", "numpy"}

    def test_it_imports_only_the_standard_library_and_numpy(self):
        tree = ast.parse(open(os.path.join(REPO, "trade_stats.py"),
                              encoding="utf-8").read())
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        assert imported <= self.ALLOWED, f"unexpected imports: {imported - self.ALLOWED}"

    def test_it_holds_no_module_level_state(self):
        """Stateless by design: two callers must get the same answer."""
        trades = sample(2.0, 20, 1.0, 20)
        first = ts.compute_trade_stats(trades)
        second = ts.compute_trade_stats(trades)
        assert first == second


# ---------------------------------------------------------------------------
# wired into the backtester
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def backtest_result():
    """One real backtest, reused: it drives the live stack and is not fast."""
    import backtest as bt
    from tests.test_backtest import permissive_config, synthetic_bars

    return bt.Backtester({"BTCUSDT": synthetic_bars(1200, seed=31)},
                         permissive_config()).run()


class TestWiredIntoTheBacktester:
    def test_trades_actually_closed_in_the_fixture(self, backtest_result):
        assert backtest_result.trades > 0, "the fixture must exercise the path"

    def test_every_closed_trade_has_a_detail_record(self, backtest_result):
        assert len(backtest_result.trade_details) == backtest_result.trades

    def test_the_initial_stop_is_recorded(self, backtest_result):
        """The one field R needs that the trade ledger does not carry."""
        stops = [d["initial_stop_price"] for d in backtest_result.trade_details]
        assert all(s is not None and s > 0 for s in stops)

    def test_bar_indices_are_recorded_and_ordered(self, backtest_result):
        for detail in backtest_result.trade_details:
            assert detail["entry_index"] is not None
            assert detail["exit_index"] >= detail["entry_index"]

    def test_r_stats_are_computed(self, backtest_result):
        assert backtest_result.r_stats is not None
        assert backtest_result.r_stats.trades == backtest_result.trades

    def test_the_details_can_be_re_analysed_independently(self, backtest_result):
        """The point of keeping plain dicts: a tool can sweep the geometry
        without re-running the backtest."""
        recomputed = ts.compute_trade_stats(backtest_result.trade_details)
        assert recomputed.total_r_net == pytest.approx(
            backtest_result.r_stats.total_r_net
        )

    def test_the_r_block_is_printed_by_summary(self, backtest_result):
        text = backtest_result.summary()
        assert "-- payoff geometry" in text
        assert "break-even hit" in text

    def test_the_existing_block_is_unchanged(self, backtest_result):
        """Additive: every line the previous report had is still there."""
        text = backtest_result.summary()
        for label in ("bars replayed", "signals evaluated", "trades closed",
                      "starting equity", "ending equity", "total return",
                      "fees paid", "max drawdown", "win rate",
                      "expectancy / trade", "book/exchange breaks"):
            assert label in text

    def test_win_rate_still_means_what_it_meant(self, backtest_result):
        """Fee-aware share of ledger trades with positive net PnL, ungated."""
        nets = [d["net_pnl"] for d in backtest_result.trade_details]
        assert backtest_result.win_rate == pytest.approx(
            sum(1 for n in nets if n > 0) / len(nets)
        )

    def test_exit_reasons_come_from_the_engine(self, backtest_result):
        reasons = set(backtest_result.r_stats.by_exit_reason)
        assert reasons <= {"stop", "take_profit", "end_of_data", "timeout",
                           "manual", "unknown"}
        assert "stop" in reasons or "take_profit" in reasons

    def test_excursions_are_measured_from_the_bars(self, backtest_result):
        covered = [d for d in backtest_result.trade_details
                   if d["mae_price"] is not None]
        assert covered, "post-entry bar ranges were available and unused"
        for detail in covered:
            assert detail["mae_price"] <= detail["mfe_price"]  # all longs on spot

    def test_a_wrong_side_stop_is_counted_not_absorbed(self, backtest_result):
        """A stop on the profitable side of the fill was never protection.

        It happens for real: the stop is planned against the signal bar's close
        and the entry fills at the next bar's close, so a gap wider than the
        stop distance opens a position already past its own stop. Such a trade
        has no measurable R, so it is excluded — and counted, so the exclusion
        is visible rather than silent.
        """
        assert backtest_result.stops_on_the_wrong_side >= 0
        for detail in backtest_result.trade_details:
            stop = detail["initial_stop_price"]
            if stop is None:
                continue
            assert stop < detail["entry_price"], (
                "a Buy kept a stop at or above its entry price: the R computed "
                "from it would be measured against risk that was never taken"
            )

    def test_the_excluded_count_and_the_r_block_agree(self, backtest_result):
        missing = sum(1 for d in backtest_result.trade_details
                      if d["initial_stop_price"] is None)
        assert backtest_result.r_stats.trades_without_stop == missing
        assert (backtest_result.r_stats.trades_with_r
                == backtest_result.trades - missing)

    def test_a_run_with_no_trades_reports_no_r_block_rather_than_zeros(self):
        import backtest as bt
        from tests.test_backtest import synthetic_bars

        cfg = bt.BacktestConfig(warmup_bars=150, min_confidence=0.95)
        result = bt.Backtester({"BTCUSDT": synthetic_bars(400)}, cfg).run()
        assert result.trades == 0
        assert result.r_stats is None
        assert result.trade_details == []
        assert "payoff geometry" not in result.summary()
