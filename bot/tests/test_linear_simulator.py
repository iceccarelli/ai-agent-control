"""The perpetual simulator — funding, margin, liquidation, and the stop.

WHY THIS FILE EXISTS
====================
`ROADMAP_FROM_100_DOLLARS.md` Stage A item 1, and the promotion gate item
`linear_protective_stop_verified`.

`funding_carry_fade_btc_v1` trades a USDT-margined linear perpetual.
`Backtester.run()` refused `category="linear"` because the simulator kept spot
cash accounting, and running a perp through cash accounting would not fail — it
would SUCCEED, and report an equity curve with no funding drag and no
liquidation. Wrong in the flattering direction.

The consequence was the one `docs/promotion/LINEAR_STOP_VERIFICATION_CHECKLIST.md`
names: the guarantee this repository asserts everywhere — *a confirmed position
always has a verified protective stop* — **had never been exercised end to end
on the instrument the signal actually trades**. Every test of it ran on spot,
where a stop is a conditional ORDER. On linear a stop is a FIELD ON THE
POSITION, written by `/v5/position/trading-stop` and read back off
`/v5/position/list`. Those are different mechanisms, and only one of them was
ever simulated.

WHAT THE FLATTERING-DIRECTION TESTS ARE FOR
===========================================
`TestFundingIsActuallyCharged` and `TestLiquidationHappens` exist because a
simulator that models these terms *nominally* — a field that is set and never
read, a rate multiplied by zero — would pass every structural test and still
report the flattering number this whole refusal existed to prevent. So each
term is asserted to MOVE THE RESULT in the direction that costs money.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import backtest as bt                                    # noqa: E402
from bybit_connection import BybitClient                  # noqa: E402
from persistence import StateStore                        # noqa: E402

HOUR = 3_600_000
SYMBOL = "BTCUSDT"


def bars(closes, *, start=1_600_000_000_000, span=HOUR, high=None, low=None):
    """One bar per close. `high`/`low` override the default +-0.5% envelope."""
    out = []
    for i, close in enumerate(closes):
        out.append(bt.Bar(
            start_ms=start + i * span,
            open=close, close=close,
            high=close * 1.005 if high is None else high[i],
            low=close * 0.995 if low is None else low[i],
            volume=100.0,
        ))
    return out


def venue(closes, **kw):
    return bt.LinearSimulatedExchange({SYMBOL: bars(closes, **{
        k: v for k, v in kw.items() if k in ("start", "span", "high", "low")})},
        **{k: v for k, v in kw.items()
           if k not in ("start", "span", "high", "low")})


def open_long(ex, qty=1.0, price=None):
    ex.positions[SYMBOL] = bt._SimPosition(
        symbol=SYMBOL, side="Buy", size=qty,
        entry_price=price if price is not None else ex.current_bar().close)
    return ex.positions[SYMBOL]


def open_short(ex, qty=1.0, price=None):
    ex.positions[SYMBOL] = bt._SimPosition(
        symbol=SYMBOL, side="Sell", size=qty,
        entry_price=price if price is not None else ex.current_bar().close)
    return ex.positions[SYMBOL]


# ---------------------------------------------------------------------------


class TestItIsNotCashAccounting:
    """The defect the refusal existed to prevent: a perp priced as spot."""

    def test_opening_a_long_credits_no_base_coin(self):
        ex = venue([100.0] * 5, starting_cash=1_000.0)
        order = bt._SimOrder(
            order_link_id="e1", symbol=SYMBOL, side="Buy", order_type="Market",
            qty=2.0, price=0.0, order_filter="Order", trigger_price=0.0,
            trigger_direction=0)
        ex._execute(order, 100.0, taker=True)
        assert order.status == "Filled"
        assert "BTC" not in ex.balances
        assert ex.positions[SYMBOL].size == pytest.approx(2.0)

    def test_a_short_can_be_opened_which_spot_cannot_do(self):
        ex = venue([100.0] * 5, starting_cash=1_000.0)
        order = bt._SimOrder(
            order_link_id="e1", symbol=SYMBOL, side="Sell", order_type="Market",
            qty=1.0, price=0.0, order_filter="Order", trigger_price=0.0,
            trigger_direction=0)
        ex._execute(order, 100.0, taker=True)
        assert order.status == "Filled"
        assert ex.positions[SYMBOL].side == "Sell"

    def test_equity_marks_to_market_continuously(self):
        ex = venue([100.0, 110.0], starting_cash=1_000.0)
        open_long(ex, qty=2.0, price=100.0)
        assert ex.equity() == pytest.approx(1_000.0)
        ex.index = 1
        assert ex.equity() == pytest.approx(1_000.0 + 2.0 * 10.0)

    def test_a_short_loses_as_the_price_rises(self):
        ex = venue([100.0, 110.0], starting_cash=1_000.0)
        open_short(ex, qty=2.0, price=100.0)
        ex.index = 1
        assert ex.equity() == pytest.approx(1_000.0 - 2.0 * 10.0)


class TestFundingIsActuallyCharged:
    """Nominal modelling is the failure mode: a rate multiplied by nothing."""

    def test_a_long_pays_a_positive_rate(self):
        ex = venue([100.0] * 4, span=8 * HOUR, starting_cash=1_000.0,
                   funding_rate_per_8h=0.001)
        open_long(ex, qty=2.0, price=100.0)
        before = ex.balances[ex.quote_asset]
        ex._apply_funding(SYMBOL, ex.bars[SYMBOL][0])
        assert ex.balances[ex.quote_asset] < before
        assert ex.funding_paid == pytest.approx(0.001 * 2.0 * 100.0)

    def test_a_short_receives_a_positive_rate(self):
        ex = venue([100.0] * 4, span=8 * HOUR, starting_cash=1_000.0,
                   funding_rate_per_8h=0.001)
        open_short(ex, qty=2.0, price=100.0)
        before = ex.balances[ex.quote_asset]
        ex._apply_funding(SYMBOL, ex.bars[SYMBOL][0])
        assert ex.balances[ex.quote_asset] > before

    def test_the_same_settlement_is_never_charged_twice(self):
        ex = venue([100.0] * 4, span=8 * HOUR, starting_cash=1_000.0,
                   funding_rate_per_8h=0.001)
        open_long(ex, qty=1.0, price=100.0)
        ex._apply_funding(SYMBOL, ex.bars[SYMBOL][0])
        once = ex.funding_paid
        ex._apply_funding(SYMBOL, ex.bars[SYMBOL][0])
        assert ex.funding_paid == pytest.approx(once)

    def test_a_real_series_overrides_the_constant(self):
        settle = ((1_600_000_000_000 // (8 * HOUR)) + 1) * (8 * HOUR)
        ex = venue([100.0] * 4, span=8 * HOUR, starting_cash=1_000.0,
                   funding_rate_per_8h=0.0,
                   funding_by_symbol={SYMBOL: [(settle, 0.002)]})
        open_long(ex, qty=1.0, price=100.0)
        ex._apply_funding(SYMBOL, ex.bars[SYMBOL][0])
        assert ex.funding_paid == pytest.approx(0.002 * 100.0)

    def test_funding_makes_a_held_long_strictly_worse(self):
        """THE flattering-direction guard. Charged funding must cost money."""
        def equity_after(rate):
            ex = venue([100.0] * 6, span=8 * HOUR, starting_cash=1_000.0,
                       funding_rate_per_8h=rate)
            open_long(ex, qty=1.0, price=100.0)
            while ex.step():
                pass
            return ex.equity()
        assert equity_after(0.001) < equity_after(0.0)


class TestLiquidationHappens:
    def test_a_cash_backed_long_has_no_reachable_liquidation(self):
        ex = venue([100.0] * 3, starting_cash=100.0)
        open_long(ex, qty=1.0, price=100.0)
        assert ex.liquidation_price(SYMBOL) is None

    def test_a_cash_backed_short_liquidates_near_a_doubling(self):
        ex = venue([100.0] * 3, starting_cash=100.0)
        open_short(ex, qty=1.0, price=100.0)
        liq = ex.liquidation_price(SYMBOL)
        assert liq == pytest.approx(200.0 / 1.005, rel=1e-6)

    def test_an_under_margined_short_is_liquidated_by_the_bar(self):
        ex = venue([100.0, 140.0], starting_cash=20.0,
                   high=[100.5, 145.0], low=[99.5, 139.0])
        open_short(ex, qty=1.0, price=100.0)
        ex.step()
        assert ex.liquidations, "an under-margined short survived a +45% bar"
        assert SYMBOL not in ex.positions

    def test_a_liquidation_is_logged_as_an_exit_fill(self):
        ex = venue([100.0, 140.0], starting_cash=20.0,
                   high=[100.5, 145.0], low=[99.5, 139.0])
        open_short(ex, qty=1.0, price=100.0)
        ex.step()
        assert [f for f in ex.fill_log if f["purpose"] == "liquidation"]


class TestWhichLevelIsCrossedFirst:
    """A bar gives range, not path — but between a stop and a liquidation the
    path is NOT in doubt, and firing liquidation regardless would be wrong
    rather than conservative."""

    def test_a_stop_nearer_than_liquidation_fills_first(self):
        ex = venue([100.0, 60.0], starting_cash=60.0,
                   high=[100.5, 101.0], low=[99.5, 55.0])
        position = open_long(ex, qty=1.0, price=100.0)
        position.stop_loss = 95.0
        liq = ex.liquidation_price(SYMBOL)
        assert liq is not None and liq < 95.0, "test needs liq below the stop"
        ex.step()
        assert not ex.liquidations
        assert [f for f in ex.fill_log if f["purpose"] == "stop"]

    def test_a_liquidation_nearer_than_the_stop_fills_first(self):
        ex = venue([100.0, 60.0], starting_cash=10.0,
                   high=[100.5, 101.0], low=[99.5, 55.0])
        position = open_long(ex, qty=1.0, price=100.0)
        position.stop_loss = 70.0
        liq = ex.liquidation_price(SYMBOL)
        assert liq is not None and liq > 70.0, "test needs liq above the stop"
        ex.step()
        assert ex.liquidations, "liquidation was nearer and did not fire"

    def test_the_stop_closes_the_position_at_the_stop_price(self):
        ex = venue([100.0, 90.0], starting_cash=1_000.0,
                   high=[100.5, 100.0], low=[99.5, 88.0])
        position = open_long(ex, qty=1.0, price=100.0)
        position.stop_loss = 95.0
        ex.step()
        fills = [f for f in ex.fill_log if f["purpose"] == "stop"]
        assert fills and fills[0]["price"] == pytest.approx(95.0)
        assert SYMBOL not in ex.positions


class TestTheProtectiveStopEndToEnd:
    """The checklist item: PLACED on the venue, and confirmed by reading it
    back from the exchange — not from local state."""

    @pytest.fixture()
    def client(self):
        view = bt._backtest_config_view(bt.BacktestConfig(category="linear"))
        store = StateStore(":memory:")
        ex = venue([100.0] * 5, starting_cash=1_000.0)
        open_long(ex, qty=1.0, price=100.0)
        yield BybitClient(config=view, store=store, transport=ex), ex
        store.close()

    def test_the_client_is_linear(self, client):
        api, _ = client
        assert api.is_linear

    def test_a_position_with_no_stop_reads_back_as_unprotected(self, client):
        api, _ = client
        live, detail = api.verify_stop(symbol=SYMBOL, order_link_id="")
        assert live is False
        assert detail == "POSITION_HAS_NO_STOP_LOSS"

    def test_a_stop_written_by_trading_stop_reads_back_as_live(self, client):
        api, ex = client
        result = api.place_stop_order(
            symbol=SYMBOL, side="Sell", qty=1.0, trigger_price=95.0)
        assert result.ok, result.reason
        live, detail = api.verify_stop(
            symbol=SYMBOL, order_link_id=result.order_link_id)
        assert live is True, detail
        assert ex.positions[SYMBOL].stop_loss == pytest.approx(95.0)

    def test_the_stop_is_read_from_the_venue_not_from_local_state(self, client):
        """Clearing it at the venue must make the readback fail, even though
        nothing local changed. That is what makes this a verification."""
        api, ex = client
        api.place_stop_order(
            symbol=SYMBOL, side="Sell", qty=1.0, trigger_price=95.0)
        ex.positions[SYMBOL].stop_loss = 0.0
        live, _ = api.verify_stop(symbol=SYMBOL, order_link_id="")
        assert live is False

    def test_the_stop_survives_reading_the_position_across_bars(self, client):
        api, ex = client
        api.place_stop_order(
            symbol=SYMBOL, side="Sell", qty=1.0, trigger_price=95.0)
        ex.step()
        live, _ = api.verify_stop(symbol=SYMBOL, order_link_id="")
        assert live is True

    def test_no_position_reads_back_as_no_position(self, client):
        api, ex = client
        ex.positions.pop(SYMBOL)
        live, detail = api.verify_stop(symbol=SYMBOL, order_link_id="")
        assert live is False
        assert detail == "NO_POSITION"


class TestTheBacktesterNoLongerRefuses:
    def test_a_linear_run_completes(self):
        series = bars([100.0 + (i % 7) for i in range(400)])
        result = bt.Backtester(
            {SYMBOL: series},
            bt.BacktestConfig(category="linear", warmup_bars=120)).run()
        assert result.bars > 0

    def test_an_unsupported_category_is_still_refused(self):
        """Closing the linear gap must not open a door for inverse/option."""
        series = bars([100.0] * 300)
        with pytest.raises(NotImplementedError):
            bt.Backtester({SYMBOL: series},
                          bt.BacktestConfig(category="inverse")).run()

    def test_an_open_linear_position_is_not_reported_as_drift(self):
        """`_book_vs_exchange_drift` read coin balances, which are always zero
        on linear — every open perp would have looked like a broken book."""
        ex = venue([100.0] * 3, starting_cash=1_000.0)
        open_long(ex, qty=1.0, price=100.0)
        store = StateStore(":memory:")
        try:
            store.upsert_position(SYMBOL, "Buy", 1.0, 100.0, stop_price=95.0)
            drift = bt.Backtester._book_vs_exchange_drift(ex, store)
            assert drift == {}
        finally:
            store.close()
