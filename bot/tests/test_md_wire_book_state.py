"""Parser, local book and market state against REAL public Bybit frames, plus the
hostile variants a live feed can produce. The fixture is verbatim venue traffic
(see md_helpers); the hostile cases are derived from it by corrupting one field."""
from __future__ import annotations

import json
import math
from collections import Counter

import pytest

import md_book as B
import md_events as E
import md_state as S
import md_wire as W
from md_helpers import FakeClock, book_frame, load_frames, ticker_frame

CTX = W.FrameContext(raw_id=1, category=E.LINEAR, conn_id="c1", recv_wall_ms=1_793_819_950_000,
                     recv_mono_s=10.0)
TS = 1_793_819_950_000
BOOK = "orderbook.50.BTCUSDT"


def feed_fixture(state: S.MarketState, *, upto=None):
    frames = load_frames()
    for c in (E.LINEAR, E.SPOT):
        state.on_connected(c)
    t0 = frames[0]["recv_ms"]
    last = None
    incidents = []
    for i, fr in enumerate(frames[:upto]):
        ctx = W.FrameContext(i + 1, fr["category"], "c-" + fr["category"], fr["recv_ms"],
                             (fr["recv_ms"] - t0) / 1000.0)
        p = W.parse_frame(fr["raw"], ctx)
        incidents += state.ingest(p, category=fr["category"])
        last = fr, ctx
    return last, incidents


# -- parser on real frames ---------------------------------------------------------------

def test_every_real_frame_parses_without_a_rejection():
    kinds, rejected = Counter(), []
    for i, fr in enumerate(load_frames()):
        ctx = W.FrameContext(i + 1, fr["category"], "c", fr["recv_ms"], i / 100.0)
        p = W.parse_frame(fr["raw"], ctx)
        rejected += p.rejections
        for e in p.events:
            kinds[type(e).__name__] += 1
        if p.control is not None:
            kinds["control"] += 1
    assert rejected == []
    assert kinds["BookSnapshot"] == 2 and kinds["BookDelta"] > 300
    assert kinds["Ticker"] > 20 and kinds["Trade"] > 20 and kinds["control"] >= 2


def test_real_events_carry_provenance():
    fr = next(f for f in load_frames() if "orderbook" in f["raw"] and '"delta"' in f["raw"])
    ctx = W.FrameContext(77, fr["category"], "conn-9", fr["recv_ms"], 5.5)
    ev = W.parse_frame(fr["raw"], ctx).events[0]
    assert isinstance(ev, E.BookDelta)
    p = ev.prov
    assert (p.raw_id, p.conn_id, p.source, p.recv_mono_s) == (77, "conn-9", E.SOURCE_WS, 5.5)
    assert p.topic.startswith("orderbook.50.") and p.exch_ts_ms and p.update_id and p.seq
    assert p.venue_lag_ms is not None and -1000 < p.venue_lag_ms < 5000


# -- hostile frames never raise and never become data -------------------------------------------

def _good_delta(**over):
    j = json.loads(book_frame(BOOK, "delta", TS, [(100.0, 1.0)], [(101.0, 1.0)], 5, 50))
    for k, v in over.items():
        if k in ("b", "a", "u", "seq", "s"):
            j["data"][k] = v
        else:
            j[k] = v
    return json.dumps(j)


@pytest.mark.parametrize("raw,reason", [
    ("not json at all", E.NOT_JSON),
    ("[1,2,3]", E.NOT_AN_OBJECT),
    (json.dumps({"hello": 1}), E.MISSING_FIELD),
    (_good_delta(topic="orderbook.50"), E.UNKNOWN_TOPIC),
    (_good_delta(topic="mystery.BTCUSDT"), E.UNKNOWN_TOPIC),
    (_good_delta(b=[["NaN", "1"]]), E.BAD_NUMBER),
    (_good_delta(b=[["-5", "1"]]), E.BAD_PRICE),
    (_good_delta(b=[["0", "1"]]), E.BAD_PRICE),
    (_good_delta(b=[["100", "-1"]]), E.BAD_QTY),
    (_good_delta(b=[["100", "inf"]]), E.BAD_NUMBER),
    (_good_delta(b=[["100"]]), E.BAD_PRICE),
    (_good_delta(b=[["100", "1"], ["100", "2"]]), E.BAD_BOOK_ORDER),
    (_good_delta(u="x"), E.BAD_NUMBER),
    (_good_delta(u=0), E.BAD_NUMBER),
    (_good_delta(u=True), E.BAD_NUMBER),
    (_good_delta(ts=TS + 3 * 24 * 3600 * 1000), E.BAD_TIMESTAMP),
    (_good_delta(ts="soon"), E.BAD_NUMBER),
    (_good_delta(s="ETHUSDT"), E.BAD_SYMBOL),
    (_good_delta(type="weird"), E.MISSING_FIELD),
])
def test_hostile_book_frames_become_rejections(raw, reason):
    p = W.parse_frame(raw, CTX)
    assert p.events == () and len(p.rejections) == 1
    assert p.rejections[0].reason == reason, p.rejections[0]


def test_a_crossed_or_unsorted_snapshot_is_rejected():
    crossed = book_frame(BOOK, "snapshot", TS, [(101, 1)], [(100, 1)], 9, 90)
    assert W.parse_frame(crossed, CTX).rejections[0].reason == E.CROSSED_BOOK
    unsorted = book_frame(BOOK, "snapshot", TS, [(99, 1), (100, 1)], [(101, 1)], 9, 90)
    assert W.parse_frame(unsorted, CTX).rejections[0].reason == E.BAD_BOOK_ORDER
    zero = book_frame(BOOK, "snapshot", TS, [(100, 0)], [(101, 1)], 9, 90)
    assert W.parse_frame(zero, CTX).rejections[0].reason == E.BAD_QTY


def test_ticker_unset_fields_are_absent_not_zero_and_bad_ones_reject_the_frame():
    ok = W.parse_frame(ticker_frame("snapshot", TS, markPrice="80000.5", preOpenPrice="",
                                    fundingRate="0.0001", nextFundingTime=TS + 3600_000,
                                    fundingIntervalHour=8), CTX)
    f = ok.events[0].fields
    assert f["markPrice"] == 80000.5 and f["fundingRate"] == 0.0001 and "preOpenPrice" not in f
    for bad in (dict(markPrice="-1"), dict(markPrice="NaN"), dict(fundingRate="7"),
                dict(fundingRate="x"), dict(nextFundingTime="-5"), dict(bid1Size="-1")):
        p = W.parse_frame(ticker_frame("delta", TS, **bad), CTX)
        assert p.events == () and p.rejections, bad


def test_a_bad_trade_is_dropped_and_the_good_ones_are_kept():
    good = {"T": TS, "s": "BTCUSDT", "S": "Buy", "v": "0.5", "p": "80000.0", "i": "a", "seq": 1}
    frame = json.dumps({"topic": "publicTrade.BTCUSDT", "type": "snapshot", "ts": TS, "data": [
        good, dict(good, i="b", S="Maybe"), dict(good, i="c", p="0"), dict(good, i="d", v="-1"),
        dict(good, i="e", s="ETHUSDT"), dict(good, i="f", T=TS + 99 * 24 * 3600 * 1000)]})
    p = W.parse_frame(frame, CTX)
    assert [t.trade_id for t in p.events] == ["a"]
    assert Counter(r.reason for r in p.rejections) == Counter(
        [E.BAD_SIDE, E.BAD_PRICE, E.BAD_QTY, E.BAD_SYMBOL, E.BAD_TIMESTAMP])


def test_control_frames_are_control_not_events():
    p = W.parse_frame(json.dumps({"success": True, "ret_msg": "pong", "op": "ping"}), CTX)
    assert p.control["ret_msg"] == "pong" and p.events == () and p.rejections == ()


# -- the local book ------------------------------------------------------------------------------

def _snap(u=100, seq=1000, bids=((100.0, 1.0), (99.0, 2.0)), asks=((101.0, 1.0), (102.0, 2.0)), ts=TS):
    return W.parse_frame(book_frame(BOOK, "snapshot", ts, bids, asks, u, seq), CTX).events[0]


def _delta(u, seq, bids=(), asks=(), ts=TS):
    return W.parse_frame(book_frame(BOOK, "delta", ts, bids, asks, u, seq), CTX).events[0]


def test_snapshot_then_in_order_deltas_apply_and_zero_size_deletes():
    b = B.LocalBook(E.LINEAR, "BTCUSDT", 50)
    assert b.apply(_snap()).status == B.RESET
    assert b.apply(_delta(101, 1001, bids=[(100.5, 3.0)], asks=[(101.0, 0.0)])).status == B.APPLIED
    assert b.top() == (100.5, 3.0, 102.0, 2.0)
    assert b.apply(_delta(102, 1002, bids=[(100.5, 0.0)])).status == B.APPLIED
    assert b.top()[0] == 100.0


def test_a_gap_unsyncs_the_book_and_it_stays_unsynced_until_a_snapshot():
    b = B.LocalBook(E.LINEAR, "BTCUSDT", 50)
    b.apply(_snap())
    assert b.apply(_delta(105, 1005, bids=[(100.0, 9.0)])).status == B.GAP
    assert not b.synced and b.top() is None and b.ladders() is None
    assert b.apply(_delta(106, 1006, bids=[(100.0, 9.0)])).status == B.NOT_SYNCED
    assert b.apply(_snap(u=200, seq=2000)).status == B.RESET
    assert b.synced and b.top() == (100.0, 1.0, 101.0, 1.0)


def test_duplicate_and_old_frames_are_dropped_without_touching_the_book():
    b = B.LocalBook(E.LINEAR, "BTCUSDT", 50)
    b.apply(_snap())
    b.apply(_delta(101, 1001, bids=[(100.0, 5.0)]))
    before = b.digest()
    assert b.apply(_delta(101, 1001, bids=[(100.0, 7.0)])).status == B.DUPLICATE
    assert b.apply(_delta(100, 1000, bids=[(100.0, 8.0)])).status == B.OUT_OF_ORDER
    assert b.digest() == before and b.synced


def test_a_sequence_that_does_not_rise_with_an_in_order_u_unsyncs():
    b = B.LocalBook(E.LINEAR, "BTCUSDT", 50)
    b.apply(_snap())
    assert b.apply(_delta(101, 999, bids=[(100.0, 5.0)])).status == B.OUT_OF_ORDER
    assert not b.synced


def test_a_delta_that_crosses_the_book_unsyncs_it_and_changes_nothing():
    b = B.LocalBook(E.LINEAR, "BTCUSDT", 50)
    b.apply(_snap())
    before = (dict(b.bids), dict(b.asks))
    assert b.apply(_delta(101, 1001, bids=[(101.5, 1.0)])).status == B.CROSSED
    assert (b.bids, b.asks) == before and not b.synced


def test_off_tick_prices_unsync_when_the_tick_is_known():
    b = B.LocalBook(E.LINEAR, "BTCUSDT", 50, tick_size=0.5)
    b.apply(_snap())
    assert b.apply(_delta(101, 1001, bids=[(100.25, 1.0)])).status == B.OFF_TICK
    assert not b.synced


def test_venue_restart_snapshot_u1_resets_even_if_seq_regresses():
    b = B.LocalBook(E.LINEAR, "BTCUSDT", 50)
    b.apply(_snap(u=500, seq=5000))
    assert b.apply(_snap(u=1, seq=10)).status == B.RESET
    assert b.update_id == 1


def test_an_older_snapshot_is_refused_after_a_newer_one():
    b = B.LocalBook(E.LINEAR, "BTCUSDT", 50)
    b.apply(_snap(u=500, seq=5000))
    assert b.apply(_snap(u=499, seq=4999)).status == B.OUT_OF_ORDER
    assert b.apply(_snap(u=500, seq=5000)).status == B.DUPLICATE


def test_an_oversized_book_is_corrupt_not_deep():
    b = B.LocalBook(E.LINEAR, "BTCUSDT", 2)
    b.apply(_snap(bids=[(100.0, 1.0)], asks=[(101.0, 1.0)]))
    many = [(90.0 - i, 1.0) for i in range(20)]
    assert b.apply(_delta(101, 1001, bids=many)).status == B.OVERSIZE
    assert not b.synced


def test_walk_never_extrapolates():
    b = B.LocalBook(E.LINEAR, "BTCUSDT", 50)
    b.apply(_snap())
    full = b.walk("buy", 2.0)                       # 1@101 + 1@102
    assert full.complete and full.avg_price == pytest.approx(101.5) and full.worst_price == 102.0
    part = b.walk("buy", 5.0)                       # only 3 displayed
    assert not part.complete and part.avg_price is None and part.qty_filled == 3.0
    sell = b.walk("sell", 1.5)
    assert sell.complete and sell.avg_price == pytest.approx((100 + 0.5 * 99) / 1.5)
    for bad in (0, -1, float("nan"), float("inf"), True, "1"):
        with pytest.raises(ValueError):
            b.walk("buy", bad)
    with pytest.raises(ValueError):
        b.walk("hold", 1.0)
    b.invalidate("X")
    assert b.walk("buy", 1.0) is None


# -- the state and its view ---------------------------------------------------------------------------

def test_real_traffic_yields_a_view_whose_only_entry_refusals_are_the_rest_items():
    st = S.MarketState("BTCUSDT")
    (fr, ctx), incidents = feed_fixture(st)
    assert incidents == [] and st.rejections == {}
    v = st.view(ctx.recv_mono_s, fr["recv_ms"])
    assert {r.split(":")[0] for r in v.refusals(S.ENTRY)} == {"instruments", "funding_print",
                                                              "rest_agreement"}
    assert v.refusals(S.MONITOR) == []
    assert v.perp_bid < v.perp_ask and v.spot_bid < v.spot_ask
    assert v.perp_mark and v.perp_index and v.next_funding_ms and v.funding_interval_h == 8
    assert v.leg_span_s is not None and v.leg_span_s < 3.0
    assert math.isfinite(v.basis_bps) and math.isfinite(v.mark_basis_bps)
    assert v.perp_bids[0][0] == v.perp_bid and v.perp_asks[0][0] == v.perp_ask
    assert len(v.perp_bids) >= 20 and len(v.spot_bids) >= 20


def test_a_quiet_feed_goes_stale_instead_of_staying_fresh():
    st = S.MarketState("BTCUSDT")
    (fr, ctx), _ = feed_fixture(st)
    now = ctx.recv_mono_s
    assert st.view(now, fr["recv_ms"]).refusals(S.MONITOR) == []
    v = st.view(now + 4.0, fr["recv_ms"] + 4000)
    assert v.perp_bid is None and v.spot_bid is None            # absent, not carried forward
    assert any(r.startswith("perp_book:STALE") for r in v.refusals(S.MONITOR))
    v = st.view(now + 11.0, fr["recv_ms"] + 11_000)
    assert any(r.startswith("perp_ticker:STALE") for r in v.refusals(S.MONITOR))
    assert v.perp_mark is None and v.funding_rate is None


def test_disconnect_and_reconnect_invalidate_until_fresh_snapshots_arrive():
    st = S.MarketState("BTCUSDT")
    (fr, ctx), _ = feed_fixture(st)
    st.on_disconnected(E.LINEAR)
    v = st.view(ctx.recv_mono_s, fr["recv_ms"])
    assert "connection:DISCONNECTED:linear" in v.refusals(S.MONITOR)
    assert v.perp_bid is None and v.spot_bid is not None       # only the lost leg is lost
    st.on_connected(E.LINEAR)
    v = st.view(ctx.recv_mono_s, fr["recv_ms"])
    assert any(r.startswith("perp_book:UNSYNCED") for r in v.refusals(S.MONITOR))
    assert any(r.startswith("perp_ticker:NO_SNAPSHOT") for r in v.refusals(S.MONITOR))


def test_a_ticker_delta_before_any_snapshot_is_not_a_ticker():
    st = S.MarketState("BTCUSDT")
    st.on_connected(E.LINEAR)
    p = W.parse_frame(ticker_frame("delta", TS, markPrice="80000"), CTX)
    st.ingest(p, category=E.LINEAR)
    assert st.tickers[E.LINEAR].fields == {} and not st.tickers[E.LINEAR].synced


def test_funding_time_in_the_past_means_the_ticker_has_not_rolled_over():
    st = S.MarketState("BTCUSDT")
    (fr, ctx), _ = feed_fixture(st)
    nxt = st.tickers[E.LINEAR].fields["nextFundingTime"]
    v = st.view(ctx.recv_mono_s, nxt + 31_000)
    assert "perp_ticker:FUNDING_TIME_IN_THE_PAST" in v.refusals(S.MONITOR)
    assert v.funding_rate is None and v.next_funding_ms is None


def test_clock_skew_in_either_direction_refuses_entries():
    for sign, tag in ((-1, "CLOCK_BEHIND"), (+1, "CLOCK_AHEAD")):
        st = S.MarketState("BTCUSDT")
        for c in (E.LINEAR, E.SPOT):
            st.on_connected(c)
        shift = 3000 if sign < 0 else 9000                    # recv - ts: -3000ms or +9000ms
        for i in range(40):
            ts = 1_793_819_950_000 + i * 100
            recv = ts + (-shift if sign < 0 else shift)
            ctx = W.FrameContext(i + 1, E.LINEAR, "c", recv, 10.0 + i * 0.1)
            st.ingest(W.parse_frame(book_frame(BOOK, "snapshot" if i == 0 else "delta", ts,
                                               [(100.0, 1.0)] if i == 0 else [],
                                               [(101.0, 1.0)] if i == 0 else [], 100 + i, 1000 + i),
                                    ctx), category=E.LINEAR)
        skew, why = st.clock_status()
        assert why.startswith(tag), (skew, why)


def test_clock_unmeasured_is_a_refusal_not_a_pass():
    st = S.MarketState("BTCUSDT")
    v = st.view(1.0, 1)
    assert "clock:UNMEASURED" in v.refusals(S.ENTRY)


def test_instrument_spec_status_and_tick_reach_the_book():
    st = S.MarketState("BTCUSDT")
    spec = E.InstrumentSpec(E.LINEAR, "BTCUSDT", "Trading", 0.1, 0.001, 0.001, 1500.0, 5.0, "BTC", "USDT")
    st.set_specs([spec])
    assert st.books[E.LINEAR].tick_size == 0.1
    halted = E.InstrumentSpec(E.SPOT, "BTCUSDT", "Closed", 0.1, 0.000001, 0.000001, 230.0, 5.0, "BTC", "USDT")
    st.set_specs([halted])
    v = st.view(1.0, 1)
    assert "instruments:NOT_TRADING:spot" in v.refusals(S.ENTRY)
    assert "instruments:NOT_TRADING:spot" not in v.refusals(S.MONITOR)


def test_rest_touch_that_cannot_coexist_with_the_socket_is_a_disagreement():
    st = S.MarketState("BTCUSDT")
    (fr, ctx), _ = feed_fixture(st)
    now = ctx.recv_mono_s
    v0 = st.view(now, fr["recv_ms"])
    pb, pa, sb, sa = v0.perp_bid, v0.perp_ask, v0.spot_bid, v0.spot_ask
    st.set_rest_touch(E.LINEAR, pb, pa, now)
    st.set_rest_touch(E.SPOT, sb, sa, now)
    assert st.view(now, fr["recv_ms"]).status("rest_agreement").ok
    st.set_rest_touch(E.LINEAR, pb * 1.01, pa * 1.01, now)              # 100 bps above the socket
    assert st.view(now, fr["recv_ms"]).status("rest_agreement").reason == "PERP_REST_WS_DISAGREE"
    st.set_rest_touch(E.LINEAR, pb, pa, now - 500)                       # agrees but is old
    assert st.view(now, fr["recv_ms"]).status("rest_agreement").reason.startswith("REST_CHECK_OLD")


def test_a_view_is_deterministic_for_the_same_state_and_clocks():
    st = S.MarketState("BTCUSDT")
    (fr, ctx), _ = feed_fixture(st)
    a = json.dumps(st.view(ctx.recv_mono_s, fr["recv_ms"]).to_dict(), sort_keys=True)
    b = json.dumps(st.view(ctx.recv_mono_s, fr["recv_ms"]).to_dict(), sort_keys=True)
    assert a == b


def test_trades_are_deduplicated_by_id():
    st = S.MarketState("BTCUSDT")
    st.on_connected(E.SPOT)
    t = {"T": TS, "s": "BTCUSDT", "S": "Buy", "v": "0.5", "p": "80000.0", "i": "x", "seq": 1}
    frame = json.dumps({"topic": "publicTrade.BTCUSDT", "type": "snapshot", "ts": TS, "data": [t, t]})
    spot = W.FrameContext(2, E.SPOT, "c2", CTX.recv_wall_ms, 11.0)
    st.ingest(W.parse_frame(frame, spot), category=E.SPOT)
    st.ingest(W.parse_frame(frame, spot), category=E.SPOT)
    assert len(st.trades[E.SPOT]) == 1
