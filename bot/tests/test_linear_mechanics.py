"""The linear simulator's mechanics, each against a HAND-COMPUTED number.

`test_linear_simulator.py` proves each term moves the result in the direction
that costs money. This file pins the arithmetic: margin, funding sign at a
jittered venue stamp, the liquidation threshold and what a liquidation costs,
the stop fill, maker/taker fees, slippage and spread, partial fills and
cancellation, stop verification, reconciliation, the spot cash path never being
touched, and a JSON state that resumes bit-identically.
"""
from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import backtest as bt                                    # noqa: E402
import config                                            # noqa: E402

SYMBOL = "BTCUSDT"
H8 = 8 * 3_600_000
MIDNIGHT = 1_600_041_600_000          # 2020-09-14T00:00Z, on the 8h grid
assert MIDNIGHT % H8 == 0


def bar(start, o, h, l, c, v=1_000.0):
    return bt.Bar(start_ms=start, open=o, high=h, low=l, close=c, volume=v)


def flat(n, price=100.0, *, span=H8, start=MIDNIGHT, volume=1_000.0):
    return [bar(start + i * span, price, price * 1.001, price * 0.999, price, volume)
            for i in range(n)]


def venue(series, **kw):
    kw.setdefault("slippage", 0.0)
    kw.setdefault("funding_rate_per_8h", 0.0)
    return bt.LinearSimulatedExchange({SYMBOL: series}, **kw)


def order(ex, side, qty, *, oid, kind="Market", price=None, reduce_only=False):
    body = {"category": "linear", "symbol": SYMBOL, "side": side,
            "orderType": kind, "qty": str(qty), "orderLinkId": oid}
    if price is not None:
        body["price"] = str(price)
    if reduce_only:
        body["reduceOnly"] = True
    _status, text = ex.request("POST", "/v5/order/create", headers={},
                               body=json.dumps(body))
    return json.loads(text)


def call(ex, method, path, body=None, params=None):
    _s, text = ex.request(method, path, headers={}, params=params,
                          body=json.dumps(body) if body is not None else None)
    return json.loads(text)


# ---------------------------------------------------------------------------


class TestFeeConstantsAreTheRepositorys:
    def test_defaults_match_config_linear_defaults(self):
        cfg = config.load({"CATEGORY": "linear", "USE_TESTNET": "1",
                           "PAPER_TRADING": "1"})
        assert bt.PERP_TAKER_FEE_BPS == cfg.TAKER_FEE_BPS == 5.5
        assert bt.PERP_MAKER_FEE_BPS == cfg.MAKER_FEE_BPS == 2.0
        ex = venue(flat(3))
        assert ex.taker_fee == pytest.approx(0.00055)
        assert ex.maker_fee == pytest.approx(0.0002)


class TestMargin:
    def test_initial_and_maintenance_margin_and_available(self):
        ex = venue(flat(3), starting_cash=1_000.0, leverage=5.0)
        assert order(ex, "Buy", 1.0, oid="a")["retCode"] == 0
        # fee 1 * 100 * 0.00055 = 0.055 ; IM = 100 / 5 = 20 ; MM = 0.005 * 100
        assert ex.balances["USDT"] == pytest.approx(1_000.0 - 0.055)
        assert ex.initial_margin() == pytest.approx(20.0)
        assert ex.maintenance_margin() == pytest.approx(0.5)
        assert ex.available_balance() == pytest.approx(1_000.0 - 0.055 - 20.0)

    def test_an_unaffordable_open_is_rejected(self):
        ex = venue(flat(3), starting_cash=100.0, leverage=1.0)
        # needs IM 100 + fee 0.055 > 100
        assert order(ex, "Buy", 1.0, oid="a")["retCode"] != 0
        assert SYMBOL not in ex.positions
        assert ex.balances["USDT"] == 100.0

    def test_margin_already_committed_is_not_available_again(self):
        ex = venue(flat(3), starting_cash=150.0, leverage=1.0)
        assert order(ex, "Buy", 1.0, oid="a")["retCode"] == 0
        # available 150 - 0.055 - 100 = 49.945 < 100.055
        assert order(ex, "Buy", 1.0, oid="b")["retCode"] != 0
        assert ex.positions[SYMBOL].size == 1.0


class TestFundingSignAtAJitteredStamp:
    """Real venue stamps land a few ms after the nominal instant."""

    def _held_long(self, rows):
        series = flat(3, span=H8)
        ex = venue(series, starting_cash=10_000.0,
                   funding_by_symbol={SYMBOL: rows})
        # opened BEFORE bar 0, so it holds the settlement at bar 1's start
        ex.positions[SYMBOL] = bt._SimPosition(SYMBOL, "Buy", 2.0, 100.0,
                                               opened_index=-1)
        return ex

    def test_the_print_stamped_3ms_late_is_the_one_charged(self):
        settle = MIDNIGHT + H8                     # bar 1 starts here
        ex = self._held_long([(MIDNIGHT + 2, -0.0001), (settle + 3, 0.0003)])
        start = ex.balances["USDT"]
        ex.step()
        # long pays +rate: 2 * 100 * 0.0003 = 0.06 (NOT the stale -0.0001)
        assert ex.funding_paid == pytest.approx(0.06)
        assert ex.balances["USDT"] == pytest.approx(start - 0.06)
        assert ex.funding_settlements_without_print == 0

    def test_a_short_receives_a_positive_rate(self):
        settle = MIDNIGHT + H8
        series = flat(3, span=H8)
        ex = venue(series, starting_cash=10_000.0,
                   funding_by_symbol={SYMBOL: [(settle + 1, 0.0003)]})
        ex.positions[SYMBOL] = bt._SimPosition(SYMBOL, "Sell", 2.0, 100.0,
                                               opened_index=-1)
        ex.step()
        assert ex.funding_paid == pytest.approx(-0.06)

    def test_a_settlement_with_no_print_is_counted_not_hidden(self):
        ex = self._held_long([(MIDNIGHT - 5 * H8, 0.0001)])
        ex.step()
        assert ex.funding_paid == pytest.approx(2 * 100 * 0.0001)
        assert ex.funding_settlements_without_print == 1


class TestLiquidation:
    def _short(self, high):
        series = [bar(MIDNIGHT, 100, 100.1, 99.9, 100),
                  bar(MIDNIGHT + 3_600_000, 100, high, 99.0, 100)]
        ex = venue(series, starting_cash=20.0)
        ex.positions[SYMBOL] = bt._SimPosition(SYMBOL, "Sell", 1.0, 100.0)
        return ex

    def test_the_threshold_is_where_equity_meets_maintenance(self):
        ex = self._short(110.0)
        liq = (20.0 + 100.0) / (1.0 * 1.005)       # 119.4029850746...
        assert ex.liquidation_price(SYMBOL) == pytest.approx(liq)
        # at that mark equity == maintenance margin
        assert 20.0 - (liq - 100.0) == pytest.approx(0.005 * liq)

    def test_a_bar_short_of_it_does_not_liquidate(self):
        ex = self._short(119.3)
        ex.step()
        assert not ex.liquidations and SYMBOL in ex.positions

    def test_crossing_it_liquidates_and_costs_fee_plus_forfeit(self):
        ex = self._short(120.0)
        liq = 120.0 / 1.005
        ex.step()
        assert len(ex.liquidations) == 1
        fee = liq * 0.00055
        forfeit = 0.005 * liq
        assert ex.balances["USDT"] == pytest.approx(
            20.0 - (liq - 100.0) - fee - forfeit)
        assert ex.liquidation_forfeited == pytest.approx(forfeit)
        assert ex.exit_events[-1]["purpose"] == "liquidation"

    def test_the_position_row_reports_liq_price_the_venue_way(self):
        ex = self._short(110.0)
        row = call(ex, "GET", "/v5/position/list",
                   params={"symbol": SYMBOL})["result"]["list"][0]
        assert float(row["liqPrice"]) == pytest.approx(120.0 / 1.005)
        cash_backed = venue(flat(2), starting_cash=1_000.0)
        cash_backed.positions[SYMBOL] = bt._SimPosition(SYMBOL, "Buy", 1.0, 100.0)
        row = call(cash_backed, "GET", "/v5/position/list",
                   params={"symbol": SYMBOL})["result"]["list"][0]
        assert row["liqPrice"] == ""            # unreachable == venue's ""


class TestStopFeesSlippageSpread:
    def test_a_stop_fills_at_trigger_less_slippage_and_half_spread(self):
        series = [bar(MIDNIGHT, 100, 100.1, 99.9, 100),
                  bar(MIDNIGHT + 3_600_000, 99, 99.5, 94.0, 96)]
        ex = venue(series, starting_cash=1_000.0, slippage=0.001, spread_bps=10.0)
        ex.positions[SYMBOL] = bt._SimPosition(SYMBOL, "Buy", 1.0, 100.0,
                                               stop_loss=95.0, stop_verified=True)
        ex.step()
        fill = ex.exit_events[-1]
        assert fill["purpose"] == "stop"
        assert fill["price"] == pytest.approx(95.0 * (1 - 0.001 - 0.0005))
        assert fill["fee"] == pytest.approx(fill["price"] * 0.00055)

    def test_a_market_buy_pays_slippage_and_half_spread(self):
        ex = venue(flat(2), starting_cash=1_000.0, slippage=0.0005, spread_bps=2.0)
        order(ex, "Buy", 1.0, oid="a")
        assert ex.orders["a"].avg_price == pytest.approx(100.0 * 1.0006)
        assert ex.orders["a"].fee_amount == pytest.approx(100.06 * 0.00055)

    def test_the_ticker_shows_the_declared_spread(self):
        ex = venue(flat(2), spread_bps=2.0)
        row = call(ex, "GET", "/v5/market/tickers",
                   params={"symbol": SYMBOL})["result"]["list"][0]
        assert float(row["bid1Price"]) == pytest.approx(99.99)
        assert float(row["ask1Price"]) == pytest.approx(100.01)

    def test_an_undeclared_spread_is_zero_and_says_so(self):
        assert venue(flat(2)).costs_declared is False
        assert venue(flat(2), spread_bps=0.0).costs_declared is True

    def test_a_resting_target_fills_at_its_price_at_the_maker_fee(self):
        series = flat(1) + [bar(MIDNIGHT + H8, 100, 106, 99.5, 104)]
        ex = venue(series, starting_cash=1_000.0, slippage=0.01, spread_bps=50)
        ex.positions[SYMBOL] = bt._SimPosition(SYMBOL, "Buy", 1.0, 100.0)
        order(ex, "Sell", 1.0, oid="tp", kind="Limit", price=105.0,
              reduce_only=True)
        ex.step()
        tp = ex.orders["tp"]
        assert tp.status == "Filled" and tp.avg_price == 105.0
        assert tp.fee_amount == pytest.approx(105.0 * 0.0002)
        assert SYMBOL not in ex.positions


class TestPartialFillsAndCancellation:
    def test_a_market_order_beyond_the_cap_fills_part_and_cancels_the_rest(self):
        ex = venue(flat(3, volume=10.0), starting_cash=10_000.0,
                   max_participation=0.1)
        assert order(ex, "Buy", 3.0, oid="a")["retCode"] == 0
        o = ex.orders["a"]
        assert o.cum_qty == pytest.approx(1.0)        # 0.1 * 10
        assert o.status == "Cancelled"
        assert ex.positions[SYMBOL].size == pytest.approx(1.0)

    def test_a_resting_limit_fills_across_bars(self):
        series = flat(1, volume=10.0) + [
            bar(MIDNIGHT + H8 * k, 100, 106, 99.5, 104, 10.0) for k in (1, 2)]
        ex = venue(series, starting_cash=10_000.0, max_participation=0.1)
        ex.positions[SYMBOL] = bt._SimPosition(SYMBOL, "Buy", 2.0, 100.0)
        order(ex, "Sell", 2.0, oid="tp", kind="Limit", price=105.0,
              reduce_only=True)
        ex.step()
        assert ex.orders["tp"].status == "PartiallyFilled"
        assert ex.orders["tp"].cum_qty == pytest.approx(1.0)
        rows = call(ex, "GET", "/v5/order/realtime",
                    params={"orderLinkId": "tp"})["result"]["list"]
        assert rows and rows[0]["orderStatus"] == "PartiallyFilled"
        ex.step()
        assert ex.orders["tp"].status == "Filled"
        assert SYMBOL not in ex.positions

    def test_a_partially_filled_order_can_be_cancelled_keeping_its_fill(self):
        series = flat(1, volume=10.0) + [
            bar(MIDNIGHT + H8 * k, 100, 106, 99.5, 104, 10.0) for k in (1, 2)]
        ex = venue(series, starting_cash=10_000.0, max_participation=0.1)
        ex.positions[SYMBOL] = bt._SimPosition(SYMBOL, "Buy", 2.0, 100.0)
        order(ex, "Sell", 2.0, oid="tp", kind="Limit", price=105.0,
              reduce_only=True)
        ex.step()
        call(ex, "POST", "/v5/order/cancel", {"symbol": SYMBOL, "orderLinkId": "tp"})
        ex.step()
        assert ex.orders["tp"].status == "Cancelled"
        assert ex.orders["tp"].cum_qty == pytest.approx(1.0)
        assert ex.positions[SYMBOL].size == pytest.approx(1.0)

    def test_an_unfilled_limit_stays_new_and_a_cancelled_one_never_fills(self):
        series = flat(1) + [bar(MIDNIGHT + H8, 100, 104, 99.5, 101),
                            bar(MIDNIGHT + 2 * H8, 100, 110, 99.5, 108)]
        ex = venue(series, starting_cash=10_000.0)
        ex.positions[SYMBOL] = bt._SimPosition(SYMBOL, "Buy", 1.0, 100.0)
        order(ex, "Sell", 1.0, oid="tp", kind="Limit", price=105.0,
              reduce_only=True)
        ex.step()
        assert ex.orders["tp"].status == "New"        # 104 never reached 105
        call(ex, "POST", "/v5/order/cancel", {"symbol": SYMBOL, "orderLinkId": "tp"})
        ex.step()                                      # 110 trades through
        assert ex.orders["tp"].status == "Cancelled"
        assert ex.orders["tp"].cum_qty == 0.0
        assert ex.positions[SYMBOL].size == 1.0


class TestStopVerification:
    def test_unverified_and_absent_stops_are_logged_as_unprotected(self):
        ex = venue(flat(4), starting_cash=1_000.0)
        order(ex, "Buy", 1.0, oid="a")
        ex.step()
        assert ex.unprotected_log[-1]["reason"] == "no_stop"
        call(ex, "POST", "/v5/position/trading-stop",
             {"symbol": SYMBOL, "stopLoss": "90"})
        assert ex.positions[SYMBOL].stop_verified is False
        ex.step()
        assert ex.unprotected_log[-1]["reason"] == "stop_not_verified"
        row = call(ex, "GET", "/v5/position/list",
                   params={"symbol": SYMBOL})["result"]["list"][0]
        assert row["stopLoss"] == "90.0"
        assert ex.positions[SYMBOL].stop_verified is True
        n = len(ex.unprotected_log)
        ex.step()
        assert len(ex.unprotected_log) == n          # protected bar: no entry

    def test_a_refused_trading_stop_leaves_the_position_unprotected(self):
        ex = venue(flat(3), starting_cash=1_000.0)
        order(ex, "Buy", 1.0, oid="a")
        ex.fail_next_trading_stops = 1
        reply = call(ex, "POST", "/v5/position/trading-stop",
                     {"symbol": SYMBOL, "stopLoss": "90"})
        assert reply["retCode"] != 0
        assert ex.positions[SYMBOL].stop_loss == 0.0


class TestReconciliationAndTheSpotPath:
    def test_a_matching_book_passes_and_a_mismatch_raises(self):
        ex = venue(flat(3), starting_cash=1_000.0)
        order(ex, "Sell", 1.0, oid="a")
        ex.reconcile({SYMBOL: -1.0})
        with pytest.raises(bt.PositionReconciliationError):
            ex.reconcile({SYMBOL: 1.0})
        with pytest.raises(bt.PositionReconciliationError):
            ex.reconcile({})

    def test_a_linear_run_never_touches_spot_accounting(self, monkeypatch):
        def boom(*_a, **_k):
            raise AssertionError("spot cash accounting was called")
        monkeypatch.setattr(bt.SimulatedExchange, "_execute", boom)
        monkeypatch.setattr(bt.SimulatedExchange, "_resolve_bar", boom)
        monkeypatch.setattr(bt.SimulatedExchange, "adverse_equity", boom)
        series = flat(1) + [bar(MIDNIGHT + H8 * k, 100, 106, 94, 100)
                            for k in (1, 2)]
        ex = venue(series, starting_cash=1_000.0)
        order(ex, "Buy", 1.0, oid="a")
        call(ex, "POST", "/v5/position/trading-stop",
             {"symbol": SYMBOL, "stopLoss": "95"})
        order(ex, "Sell", 1.0, oid="tp", kind="Limit", price=105.0,
              reduce_only=True)
        ex.adverse_equity()
        while ex.step():
            pass
        assert set(ex.balances) == {"USDT"}            # no BTC ever credited
        assert ex.exit_events and ex.exit_events[0]["purpose"] == "stop"

    def test_a_base_coin_balance_is_refused(self):
        ex = venue(flat(3))
        ex.balances["BTC"] = 0.5
        with pytest.raises(RuntimeError, match="spot cash accounting"):
            ex.step()


class TestRestartAndRecovery:
    def _series(self):
        out = []
        price = 100.0
        for i in range(12):
            o = price
            price = price * (1.03 if i % 3 else 0.98)
            out.append(bar(MIDNIGHT + i * H8, o, max(o, price) * 1.01,
                           min(o, price) * 0.99, price, 50.0))
        return out

    def _drive(self, ex, upto):
        while ex.index < upto and ex.step():
            if ex.index == 2:
                order(ex, "Buy", 3.0, oid="e")
                call(ex, "POST", "/v5/position/trading-stop",
                     {"symbol": SYMBOL, "stopLoss": "80"})
                call(ex, "GET", "/v5/position/list", params={"symbol": SYMBOL})
                order(ex, "Sell", 3.0, oid="tp", kind="Limit", price=125.0,
                      reduce_only=True)

    def _fresh(self, series):
        rows = [(MIDNIGHT + i * H8 + 2, 0.0001 * (1 if i % 2 else -2))
                for i in range(12)]
        return venue(series, starting_cash=1_000.0, slippage=0.0005,
                     spread_bps=1.0, max_participation=0.05,
                     funding_by_symbol={SYMBOL: rows}), rows

    def test_a_resumed_run_is_identical_to_an_uninterrupted_one(self):
        series = self._series()
        straight, rows = self._fresh(series)
        self._drive(straight, 99)

        first, _ = self._fresh(series)
        self._drive(first, 4)
        saved = json.loads(json.dumps(first.to_state()))
        resumed = bt.LinearSimulatedExchange.from_state(
            {SYMBOL: series}, saved, funding_by_symbol={SYMBOL: rows})
        self._drive(resumed, 99)
        assert straight.fill_log, "the scenario must actually trade"
        assert resumed.to_state() == straight.to_state()

    def test_a_state_is_refused_on_different_data(self):
        series = self._series()
        ex, rows = self._fresh(series)
        state = ex.to_state()
        other = list(series)
        other[5] = bar(other[5].start_ms, 1, 2, 0.5, 1.5)
        with pytest.raises(ValueError, match="different bars"):
            bt.LinearSimulatedExchange.from_state(
                {SYMBOL: other}, state, funding_by_symbol={SYMBOL: rows})

    def test_the_run_is_deterministic(self):
        series = self._series()
        a, _ = self._fresh(series)
        b, _ = self._fresh(series)
        self._drive(a, 99)
        self._drive(b, 99)
        assert a.to_state() == b.to_state()


def test_an_order_refused_after_a_partial_fill_is_cancelled_not_rejected():
    from backtest import LinearSimulatedExchange, _SimOrder
    order = _SimOrder.__new__(_SimOrder)
    order.cum_qty, order.status = 0.4, "PartiallyFilled"
    LinearSimulatedExchange._refuse(order)
    assert order.status == "Cancelled"
    order.cum_qty, order.status = 0.0, "New"
    LinearSimulatedExchange._refuse(order)
    assert order.status == "Rejected"
