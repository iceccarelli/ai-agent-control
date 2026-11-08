"""ms_fillmodel: a quote touch is NOT a fill, a fill needs a STRICT trade-through past the queue, fees are never rebates, every fill is MODELLED. Synthetic books/ticks."""
from __future__ import annotations

import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
BOT = os.path.dirname(HERE)
for _p in (BOT, os.path.join(BOT, "tools")):
    sys.path.insert(0, _p)

import ms_fillmodel as FM  # noqa: E402
import ms_types as T  # noqa: E402

P = FM.FillParams()
BIDS = (T.BookLevel(100.0, 10.0), T.BookLevel(99.9, 20.0), T.BookLevel(99.8, 30.0))
ASKS = (T.BookLevel(100.1, 10.0), T.BookLevel(100.2, 20.0), T.BookLevel(100.3, 30.0))


def mk(side="Buy", price=100.0, qty=5.0, age=100, params=P, now=1_000, bids=BIDS, asks=ASKS):
    return FM.place_maker(order_id="o1", group_id="g", leg_id="l", symbol="XUSDT", category="linear", side=side, qty=qty, price=price,
                          bids=bids, asks=asks, book_age_ms=age, now_ms=now, params=params)


def test_positive_control_strict_trade_through_beyond_queue_fills_at_the_limit_price():
    o, why = mk()
    assert why == "" and o.queue_ahead == pytest.approx(15.0)             # 10 visible x 1.5 pessimism
    o2, fills = FM.advance(o, [FM.Tick(5_000, 99.99, 25.0)], 6_000, P)     # through volume 25 > 15 ahead => 10 excess, capped at our 5
    assert len(fills) == 1 and fills[0].fill.qty == pytest.approx(5.0) and fills[0].fill.price == 100.0
    assert fills[0].fill.prov == "MODELLED" and fills[0].fill.liquidity == "MAKER" and o2.closed


def test_a_touch_or_a_trade_at_the_price_is_not_a_fill_however_large():
    o, _ = mk()
    o2, fills = FM.advance(o, [FM.Tick(5_000, 100.0, 1e9), FM.Tick(5_100, 100.0, 1e9)], 6_000, P)
    assert fills == [] and o2.filled == 0.0 and o2.through_cum == 0.0


def test_through_volume_below_the_queue_does_not_fill_and_the_excess_fills_proportionally():
    o, _ = mk(qty=10.0)
    o, f1 = FM.advance(o, [FM.Tick(5_000, 99.0, 10.0)], 5_000, P)          # 10 < 15 ahead
    assert f1 == []
    o, f2 = FM.advance(o, [FM.Tick(6_000, 99.0, 8.0)], 6_000, P)           # cum 18 => 3 filled (partial)
    assert [round(x.fill.qty, 9) for x in f2] == [3.0] and not o.closed
    o, f3 = FM.advance(o, [FM.Tick(7_000, 99.0, 100.0)], 7_000, P)
    assert round(sum(x.fill.qty for x in f3), 9) == 7.0 and o.closed and o.filled == pytest.approx(10.0)


def test_sell_side_is_symmetric_and_needs_a_trade_strictly_above():
    o, why = mk(side="Sell", price=100.1)
    assert why == ""
    _, none = FM.advance(o, [FM.Tick(5_000, 100.1, 1e6)], 6_000, P)
    assert none == []
    _, some = FM.advance(o, [FM.Tick(5_000, 100.11, 1e6)], 6_000, P)
    assert some and some[0].fill.side == "Sell" and some[0].fill.price == 100.1


def test_trades_before_the_order_is_live_never_fill_it():
    o, _ = mk()
    _, fills = FM.advance(o, [FM.Tick(1_100, 90.0, 1e6)], 6_000, P)         # placed at 1000, live from 1150
    assert fills == []


def test_cancel_race_a_trade_inside_the_cancel_latency_still_fills_and_after_it_does_not():
    o, _ = mk(qty=1.0)
    o = FM.request_cancel(o, 5_000)
    inside, f_in = FM.advance(o, [FM.Tick(5_200, 90.0, 1e6)], 6_000, P)     # cancel effective at 5300
    assert f_in and inside.closed
    after, f_after = FM.advance(o, [FM.Tick(5_300, 90.0, 1e6)], 6_000, P)
    assert f_after == [] and after.closed


def test_post_only_that_would_cross_and_stale_or_unknown_age_books_are_refused():
    assert mk(price=100.1)[1] == "POST_ONLY_WOULD_CROSS" and mk(side="Sell", price=100.0)[1] == "POST_ONLY_WOULD_CROSS"
    assert mk(age=P.stale_quote_ms + 1)[1] == "STALE_QUOTE" and mk(age=None)[1] == "STALE_QUOTE"
    assert mk(bids=())[1] == "NO_BOOK" and mk(qty=0.0)[1] == "BAD_QTY_OR_PRICE"


def test_queue_basis_is_pessimistic_when_the_price_is_beyond_the_visible_depth():
    o, _ = mk(price=99.0)
    assert o.queue_basis == "BEYOND_VISIBLE_DEPTH_PESSIMISTIC" and o.queue_ahead == pytest.approx(60 * 1.5)
    inside, _ = mk(price=99.95)
    assert inside.queue_basis == "EMPTY_LEVEL_INSIDE_VISIBLE" and inside.queue_ahead == 0.0
    improve, _ = mk(price=100.05)
    assert improve.queue_basis == "IMPROVES_TOUCH" and improve.queue_ahead == 0.0
    _, f = FM.advance(improve, [FM.Tick(5_000, 100.05, 100.0)], 6_000, P)     # at zero queue a trade AT our price is still not through
    assert f == []
    _, f2 = FM.advance(improve, [FM.Tick(5_000, 100.0, 100.0)], 6_000, P)     # one tick below our improved bid is through
    assert f2 and f2[0].fill.price == 100.05


def test_fees_are_never_negative_rebates_are_rejected_and_the_rates_are_the_stated_ones():
    with pytest.raises(ValueError):
        FM.FillParams(maker_bps_perp=-1.0)
    assert FM.fee_usd(10_000, "TAKER", "linear", P) == pytest.approx(5.5)
    assert FM.fee_usd(10_000, "TAKER", "spot", P) == pytest.approx(10.0)
    assert FM.fee_usd(10_000, "MAKER", "linear", P) == pytest.approx(2.0)
    assert FM.fee_usd(-10_000, "MAKER", "linear", P) >= 0
    o, _ = mk(qty=1.0)
    _, f = FM.advance(o, [FM.Tick(5_000, 90.0, 1e6)], 6_000, P)
    assert f[0].fill.fee_usd == pytest.approx(100.0 * 2.0 / 1e4) and f[0].fill.fee_usd > 0


def test_markout_is_adverse_deterministic_capped_and_applied_to_each_fill():
    a = [FM.markout_bps("o1", n, P) for n in range(50)]
    assert a == [FM.markout_bps("o1", n, P) for n in range(50)] and min(a) >= 0 and max(a) <= P.markout_cap_bps and max(a) > 0
    assert FM.markout_bps("o2", 0, P) != FM.markout_bps("o1", 0, P) or FM.markout_bps("o2", 1, P) != FM.markout_bps("o1", 1, P)
    o, _ = mk(qty=1.0)
    _, f = FM.advance(o, [FM.Tick(5_000, 90.0, 1e6)], 6_000, P)
    assert f[0].markout_bps == a[0] and f[0].markout_usd == pytest.approx(100.0 * a[0] / 1e4)
    zero = FM.FillParams(markout_mean_bps=0.0)
    assert FM.markout_bps("o1", 0, zero) == 0.0


def test_taker_walks_the_book_pays_spread_depth_and_the_taker_fee():
    r = FM.taker_fill(fill_id="t1", group_id="g", leg_id="l", symbol="XUSDT", category="linear", side="Buy", qty=15.0, bids=BIDS, asks=ASKS, ts_ms=9, params=P)
    raw = (10 * 100.1 + 5 * 100.2) / 15
    f = r.fills[0].fill
    assert f.price == pytest.approx(raw * (1 + 0.5 / 1e4)) and f.qty == 15.0 and f.liquidity == "TAKER" and f.prov == "MODELLED"
    assert f.fee_usd == pytest.approx(f.qty * f.price * 5.5 / 1e4)
    assert r.spread_usd == pytest.approx(0.05 * 15) and r.depth_walk_usd > 0 and r.latency_slip_usd > 0 and not r.depth_exhausted
    assert r.fills[0].slippage_usd == pytest.approx(abs(f.price - 100.05) * 15)
    s = FM.taker_fill(fill_id="t2", group_id="g", leg_id="l", symbol="XUSDT", category="spot", side="Sell", qty=5.0, bids=BIDS, asks=ASKS, ts_ms=9, params=P)
    assert s.fills[0].fill.price < 100.0 and s.fills[0].fill.fee_usd == pytest.approx(5.0 * s.fills[0].fill.price * 10.0 / 1e4)


def test_taker_short_fills_when_depth_is_exhausted_and_empty_book_fills_nothing():
    r = FM.taker_fill(fill_id="t", group_id="g", leg_id="l", symbol="X", category="linear", side="Buy", qty=100.0, bids=BIDS, asks=ASKS, ts_ms=1, params=P)
    assert r.depth_exhausted and r.filled_qty == pytest.approx(60.0) and r.unfilled_qty == pytest.approx(40.0)
    e = FM.taker_fill(fill_id="t", group_id="g", leg_id="l", symbol="X", category="linear", side="Buy", qty=1.0, bids=(), asks=(), ts_ms=1, params=P)
    assert e.fills == () and e.depth_exhausted


def test_mixed_maker_entry_taker_exit_pays_mixed_costs():
    o, _ = mk(qty=2.0)
    _, f = FM.advance(o, [FM.Tick(5_000, 90.0, 1e6)], 6_000, P)
    ex = FM.taker_fill(fill_id="t", group_id="g", leg_id="x", symbol="XUSDT", category="linear", side="Sell", qty=2.0, bids=BIDS, asks=ASKS, ts_ms=9, params=P).fills[0]
    total = f[0].fill.fee_usd + ex.fill.fee_usd
    assert total == pytest.approx(200.0 * 2.0 / 1e4 + 2.0 * ex.fill.price * 5.5 / 1e4)
    assert f[0].fill.fee_usd < ex.fill.fee_usd


def test_bars_without_volume_fill_nothing_unless_the_explicit_assumed_knob_is_on():
    o, _ = mk(qty=2.0)
    assert FM.bar_ticks(FM.Bar(5_000, 98.0, 101.0), o, P) == []
    assume = FM.FillParams(bar_policy="ASSUME_THROUGH_MULT", bar_through_mult=1.0)
    ticks = FM.bar_ticks(FM.Bar(5_000, 98.0, 101.0), o, assume)
    _, f = FM.advance(o, ticks, 6_000, assume)
    assert f and f[0].fill.qty == pytest.approx(2.0)
    t_only = FM.bar_ticks(FM.Bar(5_000, 100.0, 101.0), o, assume)          # low == price: touched, not through
    _, g = FM.advance(o, t_only, 6_000, assume)
    assert g == []
    assert FM.bar_ticks(FM.Bar(5_000, 98.0, 101.0, volume_base=1000.0), o, P) == []         # NO_FILL fills nothing from a bar, volume or not
    vol = FM.bar_ticks(FM.Bar(5_000, 98.0, 101.0, volume_base=1000.0), o, assume)
    assert vol and vol[0].qty == pytest.approx(1000.0 * 0.1 * (100.0 - 98.0) / (101.0 - 98.0))     # positive control: only the part of the range BELOW the buy price counts


def check_bar_volume_is_gated_and_scaled():
    o, _ = mk(qty=2.0)                                                       # a Buy at 100.0
    no_fill = FM.FillParams(bar_policy="NO_FILL")
    assume = FM.FillParams(bar_policy="ASSUME_THROUGH_MULT", bar_volume_frac=0.1)
    bar = FM.Bar(5_000, 98.0, 101.0, volume_base=1000.0)
    assert FM.bar_ticks(bar, o, no_fill) == []
    _, f = FM.advance(o, FM.bar_ticks(bar, o, no_fill), 6_000, no_fill)
    assert f == []
    t = FM.bar_ticks(bar, o, assume)
    assert len(t) == 1 and t[0].qty == pytest.approx(1000.0 * 0.1 * 2.0 / 3.0) and t[0].price == 98.0
    assert FM.bar_ticks(FM.Bar(5_000, 100.5, 101.0, volume_base=1000.0), o, assume) == []      # the bar never traded below the price: no through-volume at all
    s_o, _ = mk(side="Sell", price=100.1, qty=2.0)
    ts = FM.bar_ticks(FM.Bar(5_000, 99.0, 101.1, volume_base=1000.0), s_o, assume)
    assert ts and ts[0].qty == pytest.approx(1000.0 * 0.1 * (101.1 - 100.1) / (101.1 - 99.0)) and ts[0].price == 101.1       # a sell: the part ABOVE its price


def test_bar_volume_is_gated_on_the_policy_and_scaled_by_the_portion_traded_through():
    check_bar_volume_is_gated_and_scaled()


def test_mutation_ignoring_the_policy_or_the_portion_for_bar_volume_is_caught(monkeypatch):
    real = FM.bar_ticks

    def ungated(bar, order, p):                                              # the old behaviour: volume branch regardless of policy, full fraction of the volume
        q = bar.volume_base * p.bar_volume_frac
        px = bar.low if order.side == "Buy" else bar.high
        return [FM.Tick(bar.ts_ms, px, q)]
    monkeypatch.setattr(FM, "bar_ticks", ungated)
    with pytest.raises(AssertionError):
        check_bar_volume_is_gated_and_scaled()
    monkeypatch.setattr(FM, "bar_ticks", real)
    check_bar_volume_is_gated_and_scaled()


def test_no_state_leaks_between_calls_and_replay_of_the_same_ticks_is_identical():
    o, _ = mk(qty=7.0)
    ticks = [FM.Tick(5_000, 99.0, 20.0), FM.Tick(5_500, 99.0, 3.0)]
    a = FM.advance(o, ticks, 6_000, P)
    b = FM.advance(o, ticks, 6_000, P)
    assert a == b
