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


# ====================================================================== collector-backed path: recorded book + recorded trade tape (fixtures from the REAL collector writer)

sys.path.insert(0, HERE)
import carry_costs as CC  # noqa: E402
import ms_microstructure as MS  # noqa: E402
import test_ms_microstructure as TMS  # noqa: E402

SYM, T0 = TMS.SYM, TMS.T0


def row(off, price, size, side="Sell"):
    return (T0 + off, str(price), str(size), side)


BASE = row(100, "100.0", "0.1")                      # the oldest recorded trade: proves the tape reaches back to the order's go-live time; a non-through touch


def world(tmp_path, rows1, *, books=None, tapes=None, verify=True, name="c"):
    root = str(tmp_path / name)
    tp = {1: TMS.tape(1, [BASE] + list(rows1))}
    tp.update(tapes or {})
    TMS.build(root, books=books, tapes=tp)
    return MS.load_cycles(root, verify=verify)


def d0_of(ds):
    return TMS.snaps(ds)[0].local_receive_ms + 10


def place(ds, side="Buy", qty=2.0, price=100.0, now=None, **kw):
    return FM.place_maker_from_micro(ds, order_id="o1", group_id="g", leg_id="l", symbol=SYM, category=kw.pop("category", "linear"), side=side, qty=qty, price=price,
                                     now_ms=d0_of(ds) if now is None else now, params=P, **kw)


def run(ds, rows_ok=True, side="Buy", qty=2.0, price=100.0, upto=None, cancel_at=None):
    pl = place(ds, side=side, qty=qty, price=price)
    assert pl.order is not None, pl
    o = pl.order if cancel_at is None else FM.request_cancel(pl.order, cancel_at)
    return pl, FM.advance_with_trades(o, ds, TMS.snaps(ds)[1].tape_recv_ms if upto is None else upto, P)


def test_micro_placement_takes_queue_from_the_visible_size_at_the_price(tmp_path):
    ds = world(tmp_path, [])
    pl = place(ds)
    assert pl.reasons == () and pl.provenance == FM.PROV_MEASURED and pl.order is not None
    assert pl.visible_at_price == 2.0 and pl.queue_ahead == pytest.approx(2.0 * P.queue_pessimism) and pl.order.queue_basis == "VISIBLE_AT_PRICE"
    assert pl.order.micro_ref == f"{SYM}@cycle-{T0}:{T0 - 100}" and pl.snapshot_ts == T0 - 100 and pl.book_age_ms == 10
    assert place(ds, price=99.9).queue_ahead == pytest.approx(3.0 * P.queue_pessimism)             # a deeper level: its own visible size
    assert place(ds, price=99.5).order.queue_basis == "BEYOND_VISIBLE_DEPTH_PESSIMISTIC"             # beyond the recorded ladder: the whole side is ahead
    assert FM.PROV_MEASURED == MS.PROV_MEASURED and FM.PROV_MODELLED == MS.PROV_MODELLED and FM.PROV_REJECTED == MS.PROV_REJECTED


def test_micro_placement_is_refused_without_a_contemporaneous_valid_snapshot(tmp_path):
    ds = world(tmp_path, [])
    r0 = TMS.snaps(ds)[0].local_receive_ms
    stale = place(ds, now=r0 + 5_001)
    assert stale.order is None and stale.provenance == FM.PROV_REJECTED and "STALE_SNAPSHOT" in stale.reasons
    fut = place(ds, now=r0 - 1)
    assert fut.order is None and "FUTURE_SNAPSHOT_NOT_USED" in fut.reasons                      # the book of the future is not attached
    assert place(ds, price=100.1).reasons == ("POST_ONLY_WOULD_CROSS",)
    assert place(ds, category="spot").reasons == ("NO_RECORDED_BOOK_FOR_CATEGORY",)             # the collector records linear only
    assert place(ds, qty=0).reasons == ("BAD_QTY_OR_PRICE",)
    bad = world(tmp_path, [], books={0: TMS.book(0, bids=[["100.2", "1"]], asks=[["100.1", "1"]])}, name="crossed")
    assert "CROSSED_BOOK" in place(bad, now=TMS.snaps(bad)[0].local_receive_ms + 10).reasons
    disc = world(tmp_path, [], books={1: TMS.book(1, u=2)}, name="disc")
    assert "DISCONTINUOUS" in place(disc, now=TMS.snaps(disc)[1].local_receive_ms + 10).reasons


def test_unverified_data_never_claims_measured_provenance(tmp_path):
    ds = world(tmp_path, [row(5_000, "99.9", 10)], verify=False)
    pl, adv = run(ds)
    assert pl.provenance == FM.PROV_MODELLED and adv.provenance == FM.PROV_MODELLED and adv.fills and all(f.provenance == FM.PROV_MODELLED for f in adv.fills)


def test_touch_without_through_fills_nothing_however_large(tmp_path):
    ds = world(tmp_path, [row(5_000, "100.0", 500)])                                           # a huge trade AT the price
    _pl, adv = run(ds)
    assert adv.fills == () and adv.through_volume == 0.0 and adv.order.filled == 0.0 and not adv.order.closed
    assert adv.provenance == FM.PROV_MEASURED and adv.tape_complete and adv.reasons == ()


def test_same_side_trades_below_the_bid_are_not_through_evidence(tmp_path):
    ds = world(tmp_path, [row(5_000, "99.9", 500, "Buy")])                                      # priced through but labelled a taker BUY: not the opposing side
    _pl, adv = run(ds)
    assert adv.fills == () and adv.through_volume == 0.0


def test_through_volume_below_the_queue_is_no_fill_and_the_excess_is_partial(tmp_path):
    ds = world(tmp_path, [row(5_000, "99.9", 2.0)])                                             # queue ahead = 2 x 1.5 = 3.0
    _pl, adv = run(ds)
    assert adv.fills == () and adv.through_volume == 2.0 and adv.queue_ahead == pytest.approx(3.0)
    ds = world(tmp_path, [row(5_000, "99.9", 2.0), row(6_000, "99.8", 1.5)], name="c2")           # 3.5 through: 0.5 excess
    _pl, adv = run(ds)
    assert [round(f.sim.fill.qty, 9) for f in adv.fills] == [0.5] and adv.order.filled == pytest.approx(0.5) and not adv.order.closed
    assert adv.fills[0].sim.fill.price == 100.0 and adv.fills[0].sim.fill.liquidity == "MAKER" and adv.fills[0].sim.fill.prov == "MODELLED"
    assert adv.fills[0].queue_basis == "VISIBLE_AT_PRICE" and adv.fills[0].sim.fill.fee_usd >= 0


def test_through_with_enough_volume_fills_in_full_and_closes(tmp_path):
    ds = world(tmp_path, [row(5_000, "99.9", 6.0)])
    _pl, adv = run(ds)
    assert sum(f.sim.fill.qty for f in adv.fills) == pytest.approx(2.0) and adv.order.closed and adv.order.remaining == pytest.approx(0.0, abs=1e-9)
    big = world(tmp_path, [row(5_000, "99.9", 600.0)], name="c3")
    assert sum(f.sim.fill.qty for f in run(big)[1].fills) == pytest.approx(2.0)                   # never more than the order size


def test_sell_side_is_symmetric_needs_buys_strictly_above(tmp_path):
    ds = world(tmp_path, [row(5_000, "100.1", 50, "Buy")])                                      # a buy AT the ask: a touch
    assert run(ds, side="Sell", qty=1.0, price=100.1)[1].fills == ()
    ds = world(tmp_path, [row(5_000, "100.2", 1.9, "Buy")], name="c2")                           # queue ahead = 1 x 1.5; 1.9 through -> 0.4 fills
    adv = run(ds, side="Sell", qty=1.0, price=100.1)[1]
    assert [round(f.sim.fill.qty, 9) for f in adv.fills] == [0.4]


def test_tape_gap_is_unknown_so_no_fill_even_if_later_trades_were_through(tmp_path):
    ds = world(tmp_path, [], tapes={1: TMS.tape(1, [row(30_000, "99.0", 500)])})                # the file starts at +30 s: the first 30 s after the order is NOT recorded
    _pl, adv = run(ds)
    assert adv.fills == () and adv.provenance == FM.PROV_REJECTED and "TAPE_GAP" in adv.reasons and adv.order.closed and not adv.tape_complete
    nxt = FM.advance_with_trades(adv.order, ds, TMS.snaps(ds)[1].tape_recv_ms + 100, P)
    assert nxt.fills == () and nxt.reasons == ("ORDER_CLOSED",)                                    # the unknown order is not resurrected


def test_interval_beyond_the_last_recorded_tape_is_a_gap_not_no_trades(tmp_path):
    ds = world(tmp_path, [])
    _pl, adv = run(ds, upto=TMS.snaps(ds)[2].tape_recv_ms + 5_000)
    assert adv.provenance == FM.PROV_REJECTED and "TAPE_GAP" in adv.reasons and adv.fills == ()


def test_book_traded_through_without_recorded_trades_is_a_tape_gap(tmp_path):
    thru_book = TMS.book(1, bids=[["99.6", "1"]], asks=[["99.8", "1"]])                           # the next book's ask is BELOW our bid, yet the tape shows no trade through
    ds = world(tmp_path, [row(5_000, "100.0", 9)], books={1: thru_book})
    _pl, adv = run(ds)
    assert adv.fills == () and adv.provenance == FM.PROV_REJECTED and "BOOK_TRADED_THROUGH_WITHOUT_RECORDED_TRADES" in adv.reasons
    ok = world(tmp_path, [row(5_000, "99.7", 9)], books={1: thru_book}, name="c2")                # with recorded through-trades there is evidence: no gap, a fill
    adv2 = run(ok)[1]
    assert adv2.provenance != FM.PROV_REJECTED and adv2.fills


def test_cancel_latency_race_a_trade_inside_the_window_fills_after_it_does_not(tmp_path):
    d0 = T0 + 52
    cancel = d0 + 2_000                                                                           # effective at cancel + 300
    inside = world(tmp_path, [row(2_100 + 52, "99.9", 10)])
    adv = run(inside, cancel_at=cancel)[1]
    assert sum(f.sim.fill.qty for f in adv.fills) == pytest.approx(2.0)                            # the cancel had not taken effect yet
    after = world(tmp_path, [row(2_400 + 52, "99.9", 10)], name="c2")
    adv = run(after, cancel_at=cancel)[1]
    assert adv.fills == () and adv.order.closed


def test_markout_is_measured_from_the_next_snapshot_mid_and_adverse_is_positive(tmp_path):
    down = TMS.book(1, bids=[["99.5", "1"]], asks=[["99.7", "1"]])                                # mid 99.6 after we BOUGHT at 100.0: adverse 40 bps
    ds = world(tmp_path, [row(5_000, "99.9", 10)], books={1: down})
    f = run(ds)[1].fills[0]
    assert f.markout_basis == FM.MARKOUT_MEASURED and f.provenance == FM.PROV_MEASURED and f.mid_after == pytest.approx(99.6)
    assert f.markout_signed_bps == pytest.approx((100.0 - 99.6) / 100.0 * 1e4) and f.sim.markout_bps == pytest.approx(f.markout_signed_bps) and f.sim.markout_bps > 0
    assert f.sim.markout_usd == pytest.approx(f.sim.fill.qty * 100.0 * f.sim.markout_bps / 1e4) and f.markout_horizon_ms > 0
    up = TMS.book(1, bids=[["100.4", "1"]], asks=[["100.6", "1"]])                                # price rose after the buy: favourable, never booked as a gain
    ds = world(tmp_path, [row(5_000, "99.9", 10)], books={1: up}, name="c2")
    f = run(ds)[1].fills[0]
    assert f.markout_signed_bps < 0 and f.sim.markout_bps == 0.0 and f.sim.markout_usd == 0.0
    ds = world(tmp_path, [row(5_000, "100.2", 3.0, "Buy")], books={1: TMS.book(1, bids=[["100.3", "1"]], asks=[["100.4", "1"]])}, name="c3")   # a SELL at 100.1, mid rose to 100.35
    f = run(ds, side="Sell", qty=1.0, price=100.1)[1].fills[0]
    assert f.markout_signed_bps == pytest.approx((100.35 - 100.1) / 100.1 * 1e4) and f.markout_signed_bps > 0


def test_without_a_later_valid_snapshot_the_markout_is_modelled_and_the_fill_says_so(tmp_path):
    crossed2 = TMS.book(2, bids=[["100.2", "1"]], asks=[["100.1", "1"]])
    ds = world(tmp_path, [row(5_000, "99.9", 10)], books={1: TMS.book(1, bids=[["100.0", "1"]], asks=[["100.5", "1"]]), 2: crossed2})
    f = run(ds)[1].fills[0]
    assert ds.next_valid_after(SYM, f.sim.fill.ts_ms).cycle == f"cycle-{T0 + TMS.STEP}"
    # remove the only later valid snapshot by asking for a fill after it
    ds2 = world(tmp_path, [], books={2: crossed2}, tapes={2: TMS.tape(2, [row(TMS.STEP + 100, "100.0", "0.1"), row(TMS.STEP + 5_000, "99.9", 10)])}, name="c2")
    snap1 = TMS.snaps(ds2)[1]
    pl = FM.place_maker_from_micro(ds2, order_id="o2", group_id="g", leg_id="l", symbol=SYM, category="linear", side="Buy", qty=2.0, price=100.0, now_ms=snap1.local_receive_ms + 10, params=P)
    adv = FM.advance_with_trades(pl.order, ds2, TMS.snaps(ds2)[2].tape_recv_ms, P)
    assert adv.fills and all(x.markout_basis == FM.MARKOUT_MODELLED and x.provenance == FM.PROV_MODELLED and x.markout_signed_bps is None for x in adv.fills)
    assert adv.provenance == FM.PROV_MODELLED


def test_taker_walk_is_priced_from_the_recorded_depth_with_separate_components(tmp_path):
    ds = world(tmp_path, [])
    t = FM.taker_from_micro(ds, fill_id="f", group_id="g", leg_id="perp", symbol=SYM, category="linear", side="Buy", qty=3.0, decision_ms=d0_of(ds), params=P)
    mid, raw = 100.05, (100.1 + 2 * 100.2) / 3
    base = 3.0 * mid
    assert t.provenance == FM.PROV_MEASURED and t.reasons == () and t.mid == pytest.approx(mid)
    assert t.half_spread_bps == pytest.approx(0.05 * 3 / base * 1e4) and t.depth_walk_bps == pytest.approx((raw - 100.1) * 3 / base * 1e4)
    assert t.latency_slip_bps == pytest.approx(raw * P.taker_extra_slip_bps / 1e4 * 3 / base * 1e4)
    vwap = raw * (1 + P.taker_extra_slip_bps / 1e4)
    assert t.result.vwap == pytest.approx(vwap) and t.fee_bps == pytest.approx(CC.TAKER_BPS_PERP * 3 * vwap / base)
    assert t.total_cost_bps == pytest.approx(t.half_spread_bps + t.depth_walk_bps + t.latency_slip_bps + t.fee_bps)
    assert dict(t.component_prov) == {"half_spread": FM.PROV_MEASURED, "depth_walk": FM.PROV_MEASURED, "latency_slip": FM.PROV_MODELLED, "fee": "VENUE_STATED"}
    small = FM.taker_from_micro(ds, fill_id="f", group_id="g", leg_id="perp", symbol=SYM, category="linear", side="Buy", qty=0.5, decision_ms=d0_of(ds), params=P)
    assert small.depth_walk_bps == pytest.approx(0.0, abs=1e-9) and t.depth_walk_bps > small.depth_walk_bps and t.total_cost_bps > small.total_cost_bps        # slippage grows with size
    short = FM.taker_from_micro(ds, fill_id="f", group_id="g", leg_id="perp", symbol=SYM, category="linear", side="Buy", qty=50.0, decision_ms=d0_of(ds), params=P)
    assert short.reasons == ("DEPTH_EXHAUSTED",) and short.result.unfilled_qty == pytest.approx(45.0)


def test_hedge_leg_costs_are_separate_and_an_unrecorded_book_is_rejected_not_borrowed(tmp_path):
    ds = world(tmp_path, [])
    d = d0_of(ds)
    legs = [dict(fill_id="a", group_id="g", leg_id="perp", symbol=SYM, category="linear", side="Sell", qty=1.0),
            dict(fill_id="b", group_id="g", leg_id="spot", symbol=SYM, category="spot", side="Buy", qty=1.0)]
    perp, spot = FM.taker_legs_from_micro(ds, legs, decision_ms=d, params=P)
    assert perp.provenance == FM.PROV_MEASURED and perp.leg_id == "perp" and perp.total_cost_bps > 0
    assert spot.provenance == FM.PROV_REJECTED and spot.reasons == ("NO_RECORDED_BOOK_FOR_CATEGORY",) and spot.total_cost_bps is None and spot.result is None
    stale = FM.taker_from_micro(ds, fill_id="f", group_id="g", leg_id="perp", symbol=SYM, category="linear", side="Buy", qty=1.0, decision_ms=d + 30_000, params=P)
    assert stale.provenance == FM.PROV_REJECTED and "STALE_SNAPSHOT" in stale.reasons


def test_capacity_is_bounded_by_the_edge_or_the_depth_fraction(tmp_path):
    ds = world(tmp_path, [])
    d = d0_of(ds)

    def cap(edge, frac=0.10):
        return FM.capacity_from_micro(ds, symbol=SYM, decision_ms=d, side="Buy", edge_bps=edge, params=P, depth_fraction=frac)
    wide = cap(50.0)
    assert wide.binding == "DEPTH_FRACTION" and wide.qty == pytest.approx(0.5) and wide.usd == pytest.approx(0.5 * 100.05) and wide.provenance == FM.PROV_MEASURED
    assert cap(4.0).qty == 0.0 and cap(4.0).binding == "SLIPPAGE_EDGE" and cap(4.0).reasons == ("EDGE_BELOW_TOUCH_COST",)          # the touch already costs more than the edge
    c = cap(8.0, 1.0)
    assert c.binding == "SLIPPAGE_EDGE" and c.qty == pytest.approx(1.334, rel=2e-3) and c.slippage_bps_at_capacity == pytest.approx(8.0, abs=1e-3)
    bigger = FM.capacity_from_micro(ds, symbol=SYM, decision_ms=d, side="Buy", edge_bps=8.0, params=P, depth_fraction=1.0)
    assert bigger.qty == c.qty and cap(12.0, 1.0).qty > c.qty > cap(6.0, 1.0).qty                                                   # monotone in the stated edge
    rej = FM.capacity_from_micro(ds, symbol=SYM, decision_ms=d + 30_000, side="Buy", edge_bps=8.0, params=P)
    assert rej.binding == FM.PROV_REJECTED and rej.qty == 0.0 and "STALE_SNAPSHOT" in rej.reasons


def test_the_default_bar_policy_still_fills_nothing_and_micro_orders_replay_identically(tmp_path):
    ds = world(tmp_path, [row(5_000, "99.9", 6.0)])
    pl, a1 = run(ds)
    _pl, a2 = run(ds)
    assert a1 == a2                                                                               # pure: same data, same answer
    assert FM.bar_ticks(FM.Bar(10_000, 50.0, 100.0, 1e9), pl.order, P) == [] and P.bar_policy == "NO_FILL"
