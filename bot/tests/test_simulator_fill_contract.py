"""The fill contract of the replay exchanges.

A bar gives RANGE, not path or queue. So: a stop triggers on a touch (it is a
market order once reached); a RESTING limit or take-profit fills only when the
bar trades THROUGH its price; a gap does not improve a fill; when a bar could
have done either, the unfavourable event is assumed."""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import backtest as bt  # noqa: E402

HOUR = 3_600_000
SYM = "BTCUSDT"
T0 = 1_600_000_000_000


def two_bars(open1=100.0, high1=100.0, low1=100.0, close1=100.0):
    return [bt.Bar(T0, 100.0, 100.0, 100.0, 100.0, 1.0),
            bt.Bar(T0 + HOUR, open1, high1, low1, close1, 1.0)]


def spot(bars, **kw):
    ex = bt.SimulatedExchange({SYM: bars}, starting_cash=0.0, taker_fee=0.0,
                              maker_fee=0.001, slippage=0.0, **kw)
    ex.balances["BTC"] = 1.0
    return ex


def limit(ex, side, price, qty=1.0, oid="tp", purpose="tp"):
    order = bt._SimOrder(order_link_id=oid, symbol=SYM, side=side,
                         order_type="Limit", qty=qty, price=price,
                         order_filter="Order", trigger_price=0.0,
                         trigger_direction=0, purpose=purpose)
    ex.orders[oid] = order
    return order


def stop(ex, price, qty=1.0, oid="sl"):
    order = bt._SimOrder(order_link_id=oid, symbol=SYM, side="Sell",
                         order_type="Market", qty=qty, price=0.0,
                         order_filter="StopOrder", trigger_price=price,
                         trigger_direction=2, status="Untriggered",
                         purpose="stop")
    ex.orders[oid] = order
    return order


class TestARestingSellLimitNeedsATradeThrough:
    def test_a_bar_that_only_approaches_does_not_fill(self):
        ex = spot(two_bars(high1=104.99, low1=99.0))
        order = limit(ex, "Sell", 105.0)
        ex.step()
        assert order.status == "New" and ex.fill_log == []

    def test_a_one_tick_touch_does_not_fill(self):
        ex = spot(two_bars(high1=105.0, low1=99.0))
        order = limit(ex, "Sell", 105.0)
        ex.step()
        assert order.status == "New" and ex.fill_log == []

    def test_trading_through_fills_at_the_order_price_at_the_maker_fee(self):
        ex = spot(two_bars(high1=105.01, low1=99.0))
        order = limit(ex, "Sell", 105.0)
        ex.step()
        assert order.status == "Filled" and order.avg_price == 105.0
        [fill] = ex.fill_log
        assert fill["fee"] == pytest.approx(105.0 * 0.001)

    def test_the_touch_rule_is_available_only_by_asking_for_it(self):
        ex = spot(two_bars(high1=105.0, low1=99.0), touch_fills=True)
        order = limit(ex, "Sell", 105.0)
        ex.step()
        assert order.status == "Filled"

    def test_a_gap_through_fills_at_the_order_price_never_better(self):
        ex = spot(two_bars(open1=108.0, high1=109.0, low1=107.0, close1=108.0))
        order = limit(ex, "Sell", 105.0)
        ex.step()
        assert order.status == "Filled"
        assert order.avg_price == 105.0               # no price improvement credited

    def test_a_buy_limit_mirrors_it(self):
        ex = spot(two_bars(high1=101.0, low1=95.0))
        ex.balances["USDT"] = 1_000.0
        touch = limit(ex, "Buy", 95.0, oid="b1", purpose="entry")
        ex.step()
        assert touch.status == "New"
        ex2 = spot(two_bars(high1=101.0, low1=94.99))
        ex2.balances["USDT"] = 1_000.0
        through = limit(ex2, "Buy", 95.0, oid="b1", purpose="entry")
        ex2.step()
        assert through.status == "Filled"


class TestStopsAndTargetsInOneBar:
    def test_a_stop_triggers_on_a_touch(self):
        ex = spot(two_bars(high1=100.0, low1=95.0))
        order = stop(ex, 95.0)
        ex.step()
        assert order.status == "Filled"

    def test_when_a_bar_could_have_hit_both_the_stop_wins(self):
        ex = spot(two_bars(high1=106.0, low1=94.0))
        tp = limit(ex, "Sell", 105.0)
        sl = stop(ex, 95.0)
        ex.step()
        assert sl.status == "Filled" and tp.status == "Cancelled"
        assert len(ex.fill_log) == 1

    def test_a_stop_gaps_at_the_open_not_the_trigger(self):
        ex = spot(two_bars(open1=90.0, high1=91.0, low1=89.0, close1=90.0))
        sl = stop(ex, 95.0)
        ex.step()
        assert sl.avg_price == 90.0

    def test_a_touch_of_the_target_does_not_beat_a_stop_in_the_same_bar(self):
        ex = spot(two_bars(high1=105.0, low1=94.0))
        tp = limit(ex, "Sell", 105.0)
        sl = stop(ex, 95.0)
        ex.step()
        assert sl.status == "Filled" and tp.status == "Cancelled"


class TestAnOrderIsNotTestedOnItsOwnPlacementBar:
    def test_it_can_only_fill_on_a_later_bar(self):
        bars = [bt.Bar(T0, 100.0, 110.0, 99.0, 100.0, 1.0),     # bar 0 reaches 110
                bt.Bar(T0 + HOUR, 100.0, 100.0, 99.0, 100.0, 1.0)]
        ex = spot(bars)
        order = limit(ex, "Sell", 105.0)                         # placed at bar 0's close
        ex.step()
        assert order.status == "New"                             # bar 1 never reached it


class TestTheBooksStillAddUp:
    def test_cash_and_holdings_reconcile_after_a_fill(self):
        ex = spot(two_bars(high1=106.0, low1=99.0))
        limit(ex, "Sell", 105.0)
        ex.step()
        fee = sum(f["fee"] for f in ex.fill_log)
        assert ex.balances.get("USDT", 0.0) == pytest.approx(105.0 - fee)
        assert ex.balances.get("BTC", 0.0) == pytest.approx(0.0)
        assert ex.fees_paid == pytest.approx(fee)


SYMBOL_BARS = lambda highs, lows: [      # noqa: E731
    bt.Bar(T0 + i * HOUR, 100.0, h, l, 100.0, 1.0)
    for i, (h, l) in enumerate(zip(highs, lows))]


def perp(bars, **kw):
    ex = bt.LinearSimulatedExchange(
        {SYM: bars}, starting_cash=10_000.0, taker_fee=0.00055,
        maker_fee=0.0002, slippage=0.0005, funding_rate_per_8h=0.0, **kw)
    return ex


def long_position(ex, tp=0.0, sl=0.0):
    pos = bt._SimPosition(symbol=SYM, side="Buy", size=1.0, entry_price=100.0,
                          stop_loss=sl, take_profit=tp, opened_index=-5)
    ex.positions[SYM] = pos
    return pos


class TestThePositionAttachedTargetOnAPerp:
    def test_a_touch_does_not_close_it(self):
        ex = perp(SYMBOL_BARS([100.0, 105.0], [100.0, 99.0]))
        long_position(ex, tp=105.0)
        ex.step()
        assert SYM in ex.positions and ex.fill_log == []

    def test_trading_through_closes_it_at_the_target_at_the_taker_fee_no_slippage(self):
        ex = perp(SYMBOL_BARS([100.0, 105.01], [100.0, 99.0]))
        long_position(ex, tp=105.0)
        ex.step()
        assert SYM not in ex.positions
        [fill] = ex.fill_log
        assert fill["price"] == 105.0 and fill["purpose"] == "tp"
        assert fill["fee"] == pytest.approx(105.0 * 0.00055)

    def test_the_stop_still_wins_the_same_bar(self):
        ex = perp(SYMBOL_BARS([100.0, 106.0], [100.0, 94.0]))
        long_position(ex, tp=105.0, sl=95.0)
        ex.step()
        assert [f["purpose"] for f in ex.fill_log] == ["stop"]

    def test_a_resting_reduce_leg_obeys_the_same_contract(self):
        ex = perp(SYMBOL_BARS([100.0, 105.0], [100.0, 99.0]))
        long_position(ex)
        leg = limit(ex, "Sell", 105.0, oid="leg")
        leg.reduce_only = True
        ex.step()
        assert leg.status == "New" and SYM in ex.positions


class TestAnOrphanedLegDoesNotReduceTheNextPosition:
    def test_a_stop_cancels_the_reduce_only_legs_of_the_position_it_closed(self):
        ex = perp(SYMBOL_BARS([100.0, 100.0, 106.0], [100.0, 90.0, 99.0]))
        long_position(ex, sl=95.0)
        leg = limit(ex, "Sell", 105.0, oid="leg")
        leg.reduce_only = True
        ex.step()                                   # bar 1: the stop closes it
        assert SYM not in ex.positions and leg.status == "Cancelled"
        new = bt._SimPosition(symbol=SYM, side="Buy", size=1.0, entry_price=100.0,
                              opened_index=1)
        ex.positions[SYM] = new
        ex.step()                                   # bar 2 trades through 105
        assert ex.positions[SYM].size == 1.0        # the stale leg did not fire
