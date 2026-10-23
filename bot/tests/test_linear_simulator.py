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

    def test_a_stop_reached_inside_the_bar_fills_at_the_trigger_less_slippage(self):
        ex = venue([100.0, 100.0], starting_cash=1_000.0,
                   high=[100.5, 100.5], low=[99.5, 94.0])
        position = open_long(ex, qty=1.0, price=100.0)
        position.stop_loss = 95.0
        ex.step()
        fills = [f for f in ex.fill_log if f["purpose"] == "stop"]
        assert fills and fills[0]["price"] == pytest.approx(95.0 * (1 - ex.slippage))
        assert SYMBOL not in ex.positions

    def test_a_bar_that_opens_through_the_stop_fills_at_the_open(self):
        """Open 90, range 88-100, stop 95: nothing traded at 95. The old fill at
        95 understated this loss by 5 per unit."""
        ex = venue([100.0, 90.0], starting_cash=1_000.0,
                   high=[100.5, 100.0], low=[99.5, 88.0])
        position = open_long(ex, qty=1.0, price=100.0)
        position.stop_loss = 95.0
        ex.step()
        fills = [f for f in ex.fill_log if f["purpose"] == "stop"]
        assert fills and fills[0]["price"] == pytest.approx(90.0 * (1 - ex.slippage))

    def test_a_short_stop_gapping_up_fills_at_the_open_plus_slippage(self):
        ex = venue([100.0, 110.0], starting_cash=1_000.0,
                   high=[100.5, 112.0], low=[99.5, 108.0])
        position = open_short(ex, qty=1.0, price=100.0)
        position.stop_loss = 105.0
        ex.step()
        fills = [f for f in ex.fill_log if f["purpose"] == "stop"]
        assert fills and fills[0]["price"] == pytest.approx(110.0 * (1 + ex.slippage))


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


class TestFundingIsOnlyForWhatWasHeldThroughTheSettlement:
    EIGHT_H = 8 * HOUR

    def _venue(self):
        boundary = (1_600_000_000_000 // self.EIGHT_H + 1) * self.EIGHT_H
        ex = venue([100.0, 100.0], starting_cash=1_000.0,
                   start=boundary - HOUR, funding_rate_per_8h=0.0001)
        return ex, boundary

    def test_a_position_opened_at_the_settlement_instant_does_not_receive_it(self):
        ex, boundary = self._venue()
        open_short(ex, qty=1.0, price=100.0).opened_index = 0
        ex.step()                       # bar 1 starts exactly at the settlement
        assert ex.funding_paid == 0.0

    def test_a_position_held_through_the_settlement_does_pay_it(self):
        ex, boundary = self._venue()
        pos = open_short(ex, qty=1.0, price=100.0)
        pos.opened_index = -5           # opened well before
        ex.step()
        assert ex.funding_paid == pytest.approx(-0.01)


class TestReduceOnlyNeverOpensOrFlips:
    def _order(self, side, qty):
        return bt._SimOrder(
            order_link_id="r1", symbol=SYMBOL, side=side, order_type="Market",
            qty=qty, price=0.0, order_filter="Order", trigger_price=0.0,
            trigger_direction=0, reduce_only=True)

    def test_an_oversized_reduce_is_cut_to_the_position_not_flipped(self):
        ex = venue([100.0] * 3, starting_cash=1_000.0)
        open_long(ex, qty=1.0, price=100.0)
        order = self._order("Sell", 5.0)
        ex._execute(order, 100.0, taker=True)
        assert order.status == "Filled" and order.qty == 1.0
        assert SYMBOL not in ex.positions

    def test_a_reduce_with_nothing_to_reduce_is_refused(self):
        ex = venue([100.0] * 3, starting_cash=1_000.0)
        order = self._order("Sell", 1.0)
        ex._execute(order, 100.0, taker=True)
        assert order.status == "Rejected" and SYMBOL not in ex.positions

    def test_a_reduce_on_the_same_side_does_not_add(self):
        ex = venue([100.0] * 3, starting_cash=1_000.0)
        open_long(ex, qty=1.0, price=100.0)
        order = self._order("Buy", 1.0)
        ex._execute(order, 100.0, taker=True)
        assert order.status == "Rejected" and ex.positions[SYMBOL].size == 1.0


class TestAdverseEquity:
    def test_a_long_is_marked_at_the_low_and_a_short_at_the_high(self):
        ex = venue([100.0] * 3, starting_cash=1_000.0,
                   high=[110.0] * 3, low=[90.0] * 3)
        open_long(ex, qty=1.0, price=100.0)
        assert ex.equity() == pytest.approx(1_000.0)
        assert ex.adverse_equity() == pytest.approx(990.0)
        ex.positions.clear()
        open_short(ex, qty=1.0, price=100.0)
        assert ex.adverse_equity() == pytest.approx(990.0)


DAY = 24 * HOUR
# a UTC-midnight start, so a daily bar contains settlements at +0h, +8h, +16h
MIDNIGHT = ((1_600_000_000_000 // DAY) + 1) * DAY


class TestFundingStopsWhenThePositionStops:
    """The event-order contract of one bar that contains several settlements.

    A settlement stamped at the bar's START precedes everything in the bar. A
    settlement INSIDE the bar cannot be ordered against a stop, take-profit or
    partial exit by OHLC, so the unfavourable order is assumed per sign: a
    payment is charged as if the position were still held, a receipt is not
    credited to a position that was closed in the bar."""

    RATE = 0.001

    def _daily(self, side, *, low=None, high=None, cash=100_000.0, **kw):
        # bar 0 is the position's opening bar; bar 1 is the bar under test
        ex = venue([100.0, 100.0], start=MIDNIGHT, span=DAY,
                   starting_cash=cash, funding_rate_per_8h=self.RATE,
                   high=high or [100.5, 100.5], low=low or [99.5, 99.5], **kw)
        pos = (open_long if side == "Buy" else open_short)(ex, qty=1.0, price=100.0)
        pos.opened_index = -5                    # held through the open of bar 1
        ex.index = 0
        return ex, pos

    def test_a_long_still_held_pays_all_three_settlements(self):
        ex, _ = self._daily("Buy")
        ex.step()
        assert ex.funding_paid == pytest.approx(3 * self.RATE * 100.0)

    def test_a_short_still_held_receives_all_three_settlements(self):
        ex, _ = self._daily("Sell")
        ex.step()
        assert ex.funding_paid == pytest.approx(-3 * self.RATE * 100.0)

    def test_a_short_stopped_in_the_bar_receives_only_the_settlement_at_the_open(self):
        ex, pos = self._daily("Sell", high=[100.5, 120.0])
        pos.stop_loss = 110.0
        ex.step()
        assert SYMBOL not in ex.positions                    # it was stopped
        # the settlement stamped at the bar's start precedes the stop; the two
        # inside the bar are not credited to a position that was closed in it
        assert ex.funding_paid == pytest.approx(-1 * self.RATE * 100.0)

    def test_a_long_stopped_in_the_bar_still_pays_every_settlement(self):
        ex, pos = self._daily("Buy", low=[99.5, 80.0])
        pos.stop_loss = 90.0
        ex.step()
        assert SYMBOL not in ex.positions
        # the favourable order (stop before the later settlements) is NOT assumed
        assert ex.funding_paid == pytest.approx(3 * self.RATE * 100.0, rel=0.06)
        assert ex.funding_paid >= 3 * self.RATE * 90.0

    def test_funding_after_the_position_closed_is_never_charged_again(self):
        ex, pos = self._daily("Buy", low=[99.5, 80.0, 80.0])
        ex.bars[SYMBOL].append(bt.Bar(MIDNIGHT + 2 * DAY, 100.0, 100.5, 99.5, 100.0, 1.0))
        pos.stop_loss = 90.0
        ex.step()
        paid = ex.funding_paid
        ex.step()                                            # next bar: flat
        assert ex.funding_paid == paid

    def test_a_partial_reduction_charges_a_payer_on_the_larger_size_and_a_receiver_on_the_smaller(self):
        def run(side):
            ex, pos = self._daily(side)
            pos.size = 2.0
            ex.orders["tp1"] = bt._SimOrder(
                order_link_id="tp1", symbol=SYMBOL,
                side="Sell" if side == "Buy" else "Buy", order_type="Limit",
                qty=1.0, price=100.4, order_filter="Order", trigger_price=0.0,
                trigger_direction=0, purpose="tp", reduce_only=True)
            ex.step()
            assert ex.positions[SYMBOL].size == pytest.approx(1.0)   # reduced to 1
            return ex.funding_paid
        long_paid = run("Buy")
        short_paid = run("Sell")
        assert long_paid == pytest.approx(3 * self.RATE * 2.0 * 100.0, rel=0.01)
        # at-open settlement on the full 2.0, the two in-bar ones on the 1.0 left
        assert short_paid == pytest.approx(
            -(self.RATE * 2.0 * 100.0 + 2 * self.RATE * 1.0 * 100.0), rel=0.01)

    def test_a_re_resolved_bar_cannot_double_bill(self):
        ex, _ = self._daily("Buy")
        ex.step()
        once = ex.funding_paid
        ex._resolve_bar(SYMBOL, ex.bars[SYMBOL][ex.index])
        ex._resolve_bar(SYMBOL, ex.bars[SYMBOL][ex.index])
        assert ex.funding_paid == pytest.approx(once)

    def test_a_position_opened_at_the_open_does_not_hold_through_that_settlement(self):
        ex, pos = self._daily("Buy")
        pos.opened_index = 0                                 # opened at bar 0's close
        ex.step()
        # the settlement at bar 1's start is before the fill; only the two
        # inside the bar are held through
        assert ex.funding_paid == pytest.approx(2 * self.RATE * 100.0)

    def test_the_log_names_the_position_that_was_charged(self):
        ex = venue([100.0] * 3, start=MIDNIGHT, span=DAY, starting_cash=100_000.0,
                   funding_rate_per_8h=self.RATE)
        pos = open_long(ex, qty=1.0, price=100.0)
        pos.opened_index = 0                        # opened in bar 0
        ex.index = 1
        ex.step()                                   # bar 2: held through all three
        assert len(ex.funding_log) == 3
        assert {opened for *_rest, opened in ex.funding_log} == {MIDNIGHT}

    def test_cash_reconciles_with_pnl_fees_and_funding(self):
        ex, pos = self._daily("Buy", low=[99.5, 80.0])
        pos.stop_loss = 90.0
        start = ex.balances[ex.quote_asset]
        ex.step()
        pnl = sum(f["qty"] * (f["price"] - 100.0) for f in ex.fill_log)
        fees = sum(f["fee"] for f in ex.fill_log)
        assert ex.balances[ex.quote_asset] == pytest.approx(
            start + pnl - fees - ex.funding_paid)


class TestFundingIsGivenToThePositionThatPaidIt:
    def test_two_sequential_positions_of_one_symbol_keep_their_own_funding(self):
        class Fake:
            funding_log = [(SYMBOL, 1, 5.0, 1000), (SYMBOL, 2, 3.0, 1000),
                           (SYMBOL, 3, -2.0, 9000)]
        rows = [{"symbol": SYMBOL, "opened_epoch": 1.0, "closed_epoch": 99.0,
                 "net_pnl": 10.0},
                {"symbol": SYMBOL, "opened_epoch": 9.0, "closed_epoch": 99.0,
                 "net_pnl": 4.0}]
        a, b = bt.Backtester._net_of_funding(Fake(), rows)
        assert (a["net_pnl"], a["funding_usd"]) == (10.0 - 8.0, -8.0)
        assert (b["net_pnl"], b["funding_usd"]) == (4.0 + 2.0, 2.0)


class TestTheSettlementInstantHasOneConvention:
    """The engine acts after observing a CLOSED bar, so a fill at a bar's close
    happens just AFTER a settlement stamped at the next bar's start: what was
    held before the fill pays or receives it; what the fill opens or adds does
    not. (The strict rule the opening of a position already followed.)"""

    RATE = 1e-4

    def _venue(self, *, start=MIDNIGHT + 7 * HOUR):
        # bar 0 closes at 08:00, bar 1 STARTS on the 08:00 settlement
        ex = venue([100.0] * 3, start=start, span=HOUR, starting_cash=100_000.0,
                   funding_rate_per_8h=self.RATE, slippage=0.0,
                   taker_fee=0.0, maker_fee=0.0)
        ex.index = 0
        return ex

    def _market(self, ex, side, qty, oid="m1", reduce_only=False):
        order = bt._SimOrder(order_link_id=oid, symbol=SYMBOL, side=side,
                             order_type="Market", qty=qty, price=0.0,
                             order_filter="Order", trigger_price=0.0,
                             trigger_direction=0, reduce_only=reduce_only)
        ex._execute(order, 100.0, taker=True)
        assert order.status == "Filled"
        return order

    def test_a_position_opened_at_the_close_does_not_hold_through_that_settlement(self):
        ex = self._venue()
        self._market(ex, "Buy", 1.0)
        ex.step()
        assert ex.funding_log == []

    def test_a_quantity_added_at_the_close_does_not_pay_that_settlement(self):
        ex = self._venue()
        pos = open_long(ex, qty=1.0, price=100.0)
        pos.opened_index = -5
        self._market(ex, "Buy", 1.0)                      # now 2, one of it late
        ex.step()
        assert ex.funding_paid == pytest.approx(self.RATE * 1.0 * 100.0)

    def test_a_quantity_reduced_at_the_close_still_paid_that_settlement(self):
        ex = self._venue()
        pos = open_long(ex, qty=2.0, price=100.0)
        pos.opened_index = -5
        self._market(ex, "Sell", 1.0, reduce_only=True)
        ex.step()
        # 1 left through the exit slice (once) and 1 held at the open: both of
        # the original 2 paid it, neither paid it twice
        assert ex.funding_paid == pytest.approx(self.RATE * 2.0 * 100.0)

    def test_a_close_at_the_boundary_is_charged_once(self):
        ex = self._venue()
        pos = open_long(ex, qty=1.0, price=100.0)
        pos.opened_index = -5
        self._market(ex, "Sell", 1.0, reduce_only=True)
        assert SYMBOL not in ex.positions
        assert ex.funding_paid == pytest.approx(self.RATE * 1.0 * 100.0)
        ex.step()
        assert ex.funding_paid == pytest.approx(self.RATE * 1.0 * 100.0)  # not again

    def test_a_short_receives_it_on_the_same_convention(self):
        ex = self._venue()
        pos = open_short(ex, qty=1.0, price=100.0)
        pos.opened_index = -5
        self._market(ex, "Buy", 1.0, reduce_only=True)
        assert ex.funding_paid == pytest.approx(-self.RATE * 1.0 * 100.0)

    def test_a_settlement_not_on_a_bar_boundary_is_untouched_by_the_rule(self):
        ex = self._venue(start=MIDNIGHT + 8 * HOUR + 600_000)   # 08:10 start
        pos = open_long(ex, qty=1.0, price=100.0)
        pos.opened_index = -5
        self._market(ex, "Sell", 1.0, reduce_only=True)
        assert ex.funding_paid == 0.0

    @pytest.mark.parametrize("side,rate,delta", [
        ("Buy", 1e-4, -0.01), ("Buy", -1e-4, 0.01),
        ("Sell", 1e-4, 0.01), ("Sell", -1e-4, -0.01)])
    def test_the_sign_of_funding_against_cash(self, side, rate, delta):
        ex = venue([100.0] * 3, start=MIDNIGHT + 7 * HOUR, span=HOUR,
                   starting_cash=1_000.0, funding_rate_per_8h=rate)
        pos = (open_long if side == "Buy" else open_short)(ex, qty=1.0, price=100.0)
        pos.opened_index = -5
        before = ex.balances[ex.quote_asset]
        ex.step()
        assert ex.balances[ex.quote_asset] - before == pytest.approx(delta)
        assert ex.funding_paid == pytest.approx(-delta)

    def test_a_hole_in_the_data_does_not_make_the_settlements_in_it_vanish(self):
        series = [bt.Bar(MIDNIGHT + HOUR, 100.0, 100.5, 99.5, 100.0, 1.0),
                  bt.Bar(MIDNIGHT + 26 * HOUR, 100.0, 100.5, 99.5, 100.0, 1.0)]
        ex = bt.LinearSimulatedExchange({SYMBOL: series}, starting_cash=1_000.0,
                                        funding_rate_per_8h=1e-4, slippage=0.0)
        pos = open_long(ex, qty=1.0, price=100.0)
        pos.opened_index = -5
        ex.index = 0
        ex.step()                               # 08:00, 16:00 and 24:00 are in the hole
        assert len(ex.funding_log) == 3
        assert ex.funding_paid == pytest.approx(3 * 1e-4 * 100.0)

    def test_a_second_position_holding_the_same_settlement_is_charged_separately(self):
        ex = self._venue()
        pos = open_long(ex, qty=1.0, price=100.0)
        pos.opened_index = -5
        ex.step()
        first = ex.funding_paid
        bar = ex.bars[SYMBOL][ex.index]
        second = bt._SimPosition(symbol=SYMBOL, side="Buy", size=1.0,
                                 entry_price=100.0, opened_index=-4)
        ex.positions[SYMBOL] = second
        ex._fund_at_open(SYMBOL, bar, ex._snapshot(SYMBOL, second, bar), 1.0)
        assert ex.funding_paid == pytest.approx(2 * first)
