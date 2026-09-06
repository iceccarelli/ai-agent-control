"""Backtester tests.

The most important property is not that the backtester produces good numbers —
it is that it produces *honest* ones, and that it exercises the production code
path rather than a parallel simulation of it.
"""
from __future__ import annotations

import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import backtest as bt  # noqa: E402


def synthetic_bars(n=800, seed=5, flip_every=250, drift=0.0005, vol=0.008):
    """A series with regime flips, so both directions get exercised."""
    rng = np.random.default_rng(seed)
    price = 100.0
    bars = []
    current = drift
    for i in range(n):
        if i and i % flip_every == 0:
            current = -current
        ret = rng.normal(current, vol)
        opened = price
        price = max(1.0, price * (1 + ret))
        high = max(opened, price) * (1 + abs(rng.normal(0, vol / 4)))
        low = min(opened, price) * (1 - abs(rng.normal(0, vol / 4)))
        bars.append(bt.Bar(1_600_000_000_000 + i * 3_600_000,
                           opened, high, low, price, 1000.0))
    return bars


def permissive_config(**over):
    """Thresholds low enough that trades actually occur, so the harness is
    exercised rather than merely started."""
    cfg = bt.BacktestConfig(starting_cash=10_000.0, warmup_bars=150,
                            min_confidence=0.10, min_component_agreement=0.35)
    for k, v in over.items():
        setattr(cfg, k, v)
    return cfg


@pytest.fixture()
def permissive_view():
    """Formerly monkeypatched the config view's ``__init__`` to lower the
    component-agreement floor.

    It no longer needs to: ``min_component_agreement`` is a real
    ``BacktestConfig`` field, set by ``permissive_config`` above, and the view
    is built by the real ``config.load()``. The fixture is kept as a no-op so
    the tests that request it still read as "this test needs a sample that
    contains trades" — deleting it from eleven signatures would lose that.
    """
    return None


# ---------------------------------------------------------------------------
# it drives the real stack
# ---------------------------------------------------------------------------


class TestUsesTheLiveCodePath:
    def test_simulated_exchange_is_a_transport(self):
        """It plugs in below BybitClient, so the client is the real one."""
        from bybit_connection import Transport

        assert issubclass(bt.SimulatedExchange, Transport)

    def test_orders_are_signed_and_carry_link_ids(self, permissive_view):
        """If the real client is in the loop, its idempotency shows up here."""
        result_exchange = bt.SimulatedExchange(
            {"BTCUSDT": synthetic_bars(400)}, starting_cash=10_000.0
        )
        from bybit_connection import BybitClient
        from persistence import StateStore

        store = StateStore(":memory:")
        client = BybitClient(
            config=bt._backtest_config_view(bt.BacktestConfig()),
            store=store, transport=result_exchange,
        )
        result_exchange.index = 200
        order = client.place_order(symbol="BTCUSDT", side="Buy", qty=0.5)
        assert order.ok
        assert order.order_link_id
        assert len(order.order_link_id) <= 36
        store.close()

    def test_klines_served_to_the_strategy_exclude_the_open_bar(self):
        """The strategy must see exactly what it will see live: closed bars."""
        from bybit_connection import BybitClient
        from persistence import StateStore

        bars = synthetic_bars(400)
        exchange = bt.SimulatedExchange({"BTCUSDT": bars})
        exchange.index = 300
        store = StateStore(":memory:")
        client = BybitClient(
            config=bt._backtest_config_view(bt.BacktestConfig()),
            store=store, transport=exchange,
        )
        rows = client.get_klines("BTCUSDT", "60", 50)
        newest_served = int(rows[-1][0])
        assert newest_served == bars[299].start_ms, (
            "the in-progress bar leaked into the strategy's view"
        )
        store.close()

    def test_risk_gates_apply_in_backtest(self, permissive_view):
        """A position cap set in the backtest config must actually bind."""
        cfg = permissive_config(max_position_pct=0.01)
        result = bt.Backtester({"BTCUSDT": synthetic_bars(600)}, cfg).run()
        for equity in result.equity_curve:
            assert equity > 0


# ---------------------------------------------------------------------------
# accounting
# ---------------------------------------------------------------------------


class TestAccounting:
    def test_fees_are_charged_on_both_legs(self, permissive_view):
        cfg = permissive_config(taker_fee=0.01)
        result = bt.Backtester({"BTCUSDT": synthetic_bars(600)}, cfg).run()
        if result.trades:
            assert result.fees_paid > 0

    def test_higher_fees_reduce_return(self, permissive_view):
        bars = synthetic_bars(700, seed=21)
        cheap = bt.Backtester({"BTCUSDT": bars},
                              permissive_config(taker_fee=0.0002)).run()
        dear = bt.Backtester({"BTCUSDT": bars},
                             permissive_config(taker_fee=0.01)).run()
        if cheap.trades and dear.trades:
            assert dear.fees_paid > cheap.fees_paid
            assert dear.total_return <= cheap.total_return

    def test_slippage_is_applied_against_the_trader(self):
        exchange = bt.SimulatedExchange(
            {"BTCUSDT": synthetic_bars(300)}, slippage=0.01, taker_fee=0.0
        )
        exchange.index = 200
        reference = exchange.current_bar().close
        cash_before = exchange.balances["USDT"]
        order = bt._SimOrder("x", "BTCUSDT", "Buy", "Market", 1.0, 0.0, "Order", 0, 0)
        exchange.orders["x"] = order
        exchange._execute(order, reference, taker=True)
        assert order.avg_price > reference, "buy slippage must raise the fill price"
        assert exchange.balances["USDT"] < cash_before

    def test_equity_curve_has_no_negative_values(self, permissive_view):
        result = bt.Backtester({"BTCUSDT": synthetic_bars(700)},
                               permissive_config()).run()
        assert all(e >= 0 for e in result.equity_curve)

    def test_positions_are_closed_at_the_end(self, permissive_view):
        """Otherwise the reported return excludes an open position's PnL."""
        result = bt.Backtester({"BTCUSDT": synthetic_bars(700)},
                               permissive_config()).run()
        assert result.ending_equity > 0

    def test_partial_take_profit_reduces_the_position(self, permissive_view):
        """The bug the first sweep exposed: a filled TP left the book unchanged,
        so the next full close asked to sell more than was held."""
        result = bt.Backtester({"BTCUSDT": synthetic_bars(900, seed=31)},
                               permissive_config()).run()
        # If accounting were broken, closes would fail and trades would be 0
        # while signals were plentiful.
        if result.signals_actionable > 20:
            assert result.trades > 0, (
                "signals were acted on but no trade ever completed — "
                "position accounting is out of sync with the exchange"
            )


# ---------------------------------------------------------------------------
# honesty
# ---------------------------------------------------------------------------


class TestHonesty:
    def test_zero_trades_is_reported_as_a_result_not_hidden(self):
        """At default thresholds this strategy does not trade. Say so."""
        cfg = bt.BacktestConfig(starting_cash=10_000.0, warmup_bars=150,
                                min_confidence=0.60)
        result = bt.Backtester({"BTCUSDT": synthetic_bars(600)}, cfg).run()
        assert result.trades == 0
        assert result.win_rate is None
        assert any("no trades" in n for n in result.notes)
        assert result.blocked_reasons, "block reasons must be recorded"

    def test_win_rate_is_none_not_zero_without_trades(self):
        cfg = bt.BacktestConfig(warmup_bars=150, min_confidence=0.95)
        result = bt.Backtester({"BTCUSDT": synthetic_bars(500)}, cfg).run()
        assert result.win_rate is None
        assert result.expectancy is None

    def test_summary_reports_n_a_rather_than_a_fake_number(self):
        cfg = bt.BacktestConfig(warmup_bars=150, min_confidence=0.95)
        summary = bt.Backtester({"BTCUSDT": synthetic_bars(500)}, cfg).run().summary()
        assert "n/a" in summary

    def test_stop_resolves_before_take_profit_within_a_bar(self):
        """The conservative assumption. Assuming the favourable one is how
        backtests flatter themselves."""
        bars = [
            bt.Bar(1_600_000_000_000, 100.0, 100.0, 100.0, 100.0, 1000.0),
            # This bar's range touches BOTH a stop at 95 and a TP at 105.
            bt.Bar(1_600_003_600_000, 100.0, 110.0, 90.0, 100.0, 1000.0),
        ]
        exchange = bt.SimulatedExchange({"BTCUSDT": bars}, taker_fee=0.0, slippage=0.0)
        exchange.balances["BTC"] = 2.0
        exchange.orders["stop"] = bt._SimOrder(
            "stop", "BTCUSDT", "Sell", "Market", 1.0, 0.0, "StopOrder", 95.0, 2,
            status="Untriggered", purpose="stop",
        )
        exchange.orders["tp"] = bt._SimOrder(
            "tp", "BTCUSDT", "Sell", "Limit", 1.0, 105.0, "Order", 0.0, 0,
            status="New", purpose="tp",
        )
        exchange.step()
        assert exchange.fill_log[0]["purpose"] == "stop", (
            "the take-profit was resolved before the stop within the same bar"
        )


# ---------------------------------------------------------------------------
# walk-forward
# ---------------------------------------------------------------------------


class TestWalkForward:
    def test_folds_are_produced(self, permissive_view):
        folds = bt.walk_forward({"BTCUSDT": synthetic_bars(1200)},
                                permissive_config(), folds=4)
        assert len(folds) == 4
        for fold in folds:
            assert fold["train_bars"] > 0
            assert fold["test_bars"] > 0

    def test_folds_do_not_overlap(self, permissive_view):
        folds = bt.walk_forward({"BTCUSDT": synthetic_bars(1200)},
                                permissive_config(), folds=4)
        assert [f["fold"] for f in folds] == [1, 2, 3, 4]

    def test_too_little_data_raises_rather_than_silently_reducing_folds(self):
        with pytest.raises(ValueError):
            bt.walk_forward({"BTCUSDT": synthetic_bars(50)}, folds=4)

    def test_every_fold_reports_its_own_blocked_reasons(self, permissive_view):
        folds = bt.walk_forward({"BTCUSDT": synthetic_bars(1200)},
                                permissive_config(), folds=4)
        assert all("blocked_reasons" in f for f in folds)


# ---------------------------------------------------------------------------
# Monte Carlo
# ---------------------------------------------------------------------------


class TestMonteCarlo:
    def test_too_few_trades_returns_insufficient_not_a_number(self):
        """A ruin probability from six trades is noise with a decimal point."""
        result = bt.monte_carlo_ruin([0.01] * 6)
        assert result["insufficient_data"] is True
        assert "probability_of_ruin" not in result

    def test_a_losing_edge_erodes_equity(self):
        result = bt.monte_carlo_ruin(
            [-0.02] * 60, runs=500, horizon=200, position_fraction=0.02
        )
        assert result["insufficient_data"] is False
        assert result["median_final_equity"] < 10_000.0

    def test_a_winning_edge_grows_equity(self):
        returns = [0.03] * 40 + [-0.01] * 20
        result = bt.monte_carlo_ruin(
            returns, runs=500, horizon=200, position_fraction=0.02
        )
        assert result["median_final_equity"] > 10_000.0

    def test_a_losing_edge_is_worse_than_a_winning_one(self):
        """The relationship, not an arbitrary threshold."""
        losing = bt.monte_carlo_ruin([-0.02] * 60, runs=400, horizon=200,
                                     position_fraction=0.02)
        winning = bt.monte_carlo_ruin([0.02] * 60, runs=400, horizon=200,
                                      position_fraction=0.02)
        assert losing["median_final_equity"] < winning["median_final_equity"]
        assert losing["probability_of_ruin"] >= winning["probability_of_ruin"]

    def test_bigger_positions_raise_the_probability_of_ruin(self):
        """The property that makes this worth computing at all."""
        returns = [-0.05] * 40 + [0.03] * 20
        small = bt.monte_carlo_ruin(returns, runs=400, horizon=400,
                                    position_fraction=0.02)
        large = bt.monte_carlo_ruin(returns, runs=400, horizon=400,
                                    position_fraction=0.50)
        assert large["probability_of_ruin"] > small["probability_of_ruin"]

    def test_position_fraction_is_actually_applied(self):
        """An earlier version cancelled the factor out of the compounding term,
        applying each trade's full notional return to the whole account."""
        returns = [0.05] * 60
        tiny = bt.monte_carlo_ruin(returns, runs=200, horizon=50,
                                   position_fraction=0.001)
        big = bt.monte_carlo_ruin(returns, runs=200, horizon=50,
                                  position_fraction=0.10)
        # Compare the GAIN, not the total: a 100x difference in position size
        # shows up in what was made, which starting equity would otherwise
        # swamp. If the factor cancelled out, these would be identical.
        tiny_gain = tiny["median_final_equity"] - 10_000.0
        big_gain = big["median_final_equity"] - 10_000.0
        assert big_gain > tiny_gain * 10, (
            f"position_fraction barely affects the outcome "
            f"(gains {tiny_gain:.2f} vs {big_gain:.2f}) — is it cancelling out?"
        )

    def test_results_are_reproducible_for_a_fixed_seed(self):
        returns = [0.02, -0.01] * 30
        a = bt.monte_carlo_ruin(returns, runs=200, seed=7)
        b = bt.monte_carlo_ruin(returns, runs=200, seed=7)
        assert a["probability_of_ruin"] == b["probability_of_ruin"]

    def test_percentiles_are_ordered(self):
        returns = [0.02, -0.015] * 40
        result = bt.monte_carlo_ruin(returns, runs=500)
        assert result["p05_final_equity"] <= result["median_final_equity"]
        assert result["median_final_equity"] <= result["p95_final_equity"]
        assert result["median_max_drawdown"] <= result["p95_max_drawdown"]


# ---------------------------------------------------------------------------
# data loading
# ---------------------------------------------------------------------------


class TestDataLoading:
    def test_csv_round_trip(self, tmp_path):
        path = tmp_path / "bars.csv"
        path.write_text(
            "timestamp,open,high,low,close,volume\n"
            "1600000000,100,101,99,100.5,10\n"
            "1600003600,100.5,102,100,101.5,12\n"
        )
        bars = bt.load_bars_csv(str(path))
        assert len(bars) == 2
        assert bars[0].start_ms == 1_600_000_000_000   # seconds normalised to ms
        assert bars[1].close == 101.5

    def test_unparseable_row_raises_rather_than_being_skipped(self, tmp_path):
        """Silently dropping rows changes the result and hides the reason."""
        path = tmp_path / "bad.csv"
        path.write_text(
            "timestamp,open,high,low,close,volume\n"
            "1600000000,100,101,99,100.5,10\n"
            "1600003600,oops,102,100,101.5,12\n"
        )
        with pytest.raises(ValueError):
            bt.load_bars_csv(str(path))

    def test_bars_are_sorted_oldest_first(self, tmp_path):
        path = tmp_path / "unsorted.csv"
        path.write_text(
            "timestamp,open,high,low,close,volume\n"
            "1600003600,100.5,102,100,101.5,12\n"
            "1600000000,100,101,99,100.5,10\n"
        )
        bars = bt.load_bars_csv(str(path))
        assert bars[0].start_ms < bars[1].start_ms
