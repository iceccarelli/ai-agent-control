"""End-to-end acceptance: one pair through the SHIPPED stack, under failure.

CarryEngine -> LedgerBroker -> CarryBroker -> scripted venue, with the real
Journal and (where a restart or a kill switch is the point) the real StateStore.
Only the venue is scripted. Every case ends on the same three invariants:

  1. the venue's perp size equals what the book believes it holds;
  2. the ledger's perp size equals the venue's;
  3. nothing is left naked: spot held minus perp short stays inside the band,
     or the book is flat, or the book is HALTED and says so.

Every figure is a FIXTURE value, not an observation. Offline.
"""
from __future__ import annotations

import os
import sys
import time

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import ledger as L                                          # noqa: E402
from carry_broker import CarryBroker, CarryOrderRefused    # noqa: E402
from carry_engine import BookState, CarryEngine             # noqa: E402
from test_carry_broker import Boom                          # noqa: E402
from main import carry_entries_permitted_from               # noqa: E402
from market_snapshot import MarketSnapshot                  # noqa: E402
from persistence import StateStore                          # noqa: E402
from test_carry_fee_truth import (                          # noqa: E402
    H8, MARK, build, modern, open_the_hedge)


def perp_orders(venue):
    return [b for b in venue.created if b["category"] == "linear"]


def spot_net(venue):
    """BTC actually held: ordered quantity less the base-coin fee the venue
    took out of each spot BUY (the venue's own fee rows, not the engine's)."""
    net = 0.0
    for b in venue.created:
        if b["category"] != "spot":
            continue
        qty = float(b["qty"])
        if b["side"] == "Buy":
            row = venue.orders.get(b["orderLinkId"], {})
            qty -= float((row.get("cumFeeDetail") or {}).get("BTC", 0) or 0)
        net += qty * (1 if b["side"] == "Buy" else -1)
    return net


def fresh_snapshot(engine):
    engine.snapshot = MarketSnapshot(
        perp_mark=MARK, spot_mark=MARK, funding_bps=3.0, margin_multiple=5.0,
        observed_at_s=time.time())


def assert_consistent(venue, journal, engine, label=""):
    """The three invariants above."""
    held = 0.0 if engine.position is None else engine.position.perp.filled_qty
    if engine.state is not BookState.HALTED:
        assert venue.perp_qty == pytest.approx(held, abs=1e-9), \
            f"{label}: venue perp {venue.perp_qty} vs book {held}"
    st = journal.statement()
    # the ledger signs a short negative; the venue reports its size unsigned
    assert -st["open_perp_qty"] == pytest.approx(venue.perp_qty, abs=1e-9), \
        f"{label}: ledger perp {st['open_perp_qty']} vs venue {venue.perp_qty}"
    if engine.state is not BookState.HALTED:
        gap = abs(spot_net(venue) - venue.perp_qty)
        assert gap <= max(1e-9, 0.02 * max(venue.perp_qty, spot_net(venue))), \
            f"{label}: naked by {gap}"


def invert_until_flat(engine, venue, start=4):
    stamp = start * H8
    for _ in range(10):
        fresh_snapshot(engine)
        engine.on_candle(mark=MARK, funding_bps=-4.0, spot=MARK,
                         funding_print_ms=stamp)
        stamp += H8
        if engine.position is None:
            return


# 1 -- normal paired entry and exit ------------------------------------------

def test_normal_entry_and_exit_ends_flat_and_agreed():
    venue, journal, engine = build(modern)
    assert open_the_hedge(engine).action == "opened"
    assert_consistent(venue, journal, engine, "after open")
    assert venue.perp_qty > 0 and spot_net(venue) == pytest.approx(venue.perp_qty)
    invert_until_flat(engine, venue)
    assert engine.position is None
    assert_consistent(venue, journal, engine, "after exit")
    assert venue.perp_qty == 0.0 and spot_net(venue) == pytest.approx(0.0)


# 2 -- the second leg is refused ----------------------------------------------

def test_a_refused_perp_leg_unwinds_the_spot_and_ends_flat():
    venue, journal, engine = build(modern)
    real = venue._request

    def refuse_perp(method, endpoint, **kw):
        body = kw.get("body") or {}
        if endpoint == "/v5/order/create" and body.get("category") == "linear":
            raise Boom(110007)            # the venue answered "no": definitive
        return real(method, endpoint, **kw)
    venue._request = refuse_perp
    open_the_hedge(engine)
    assert engine.position is None and engine.state is not BookState.HALTED
    assert spot_net(venue) == pytest.approx(0.0, abs=1e-9)   # bought, sold back
    assert venue.perp_qty == 0.0
    assert_consistent(venue, journal, engine, "perp refused")


def test_an_unanswered_perp_leg_is_not_guessed_at():
    """No retCode means the order's fate is open: the book must not send the
    leg again, and must say what it holds."""
    venue, journal, engine = build(modern)
    real = venue._request

    attempts = []

    def silence(method, endpoint, **kw):
        body = kw.get("body") or {}
        if endpoint == "/v5/order/create" and body.get("category") == "linear":
            attempts.append(body["orderLinkId"])
            raise RuntimeError("connection reset")
        return real(method, endpoint, **kw)
    venue._request = silence
    open_the_hedge(engine)
    fresh_snapshot(engine)
    engine.on_candle(mark=MARK, funding_bps=3.0, spot=MARK,
                     funding_print_ms=4 * H8)
    assert len(attempts) == 1                           # never re-sent
    assert engine.state is BookState.HALTED
    assert engine.position is not None                  # the exposure is on record
    assert_consistent(venue, journal, engine, "perp unanswered")


# 3 -- partial fill on the perp leg --------------------------------------------

def test_a_partial_perp_fill_never_leaves_the_pair_unhedged():
    venue, journal, engine = build(modern)
    real = venue._request

    def half_perp(method, endpoint, **kw):
        body = dict(kw.get("body") or {})
        if endpoint == "/v5/order/create" and body.get("category") == "linear":
            body["qty"] = str(float(body["qty"]) / 2)
            kw = dict(kw, body=body)
        return real(method, endpoint, **kw)
    venue._request = half_perp
    open_the_hedge(engine)
    if engine.position is not None:
        assert engine.state in (BookState.HEDGED, BookState.HALTED)
    gap = abs(spot_net(venue) - venue.perp_qty)
    assert engine.state is BookState.HALTED or engine.position is None \
        or gap <= 0.02 * spot_net(venue), (engine.state, gap)
    assert_consistent(venue, journal, engine, "half perp fill")


# 4 -- the order was sent and the answer was lost ------------------------------

def test_a_timeout_after_submit_does_not_send_a_second_order_for_the_leg():
    venue, journal, engine = build(modern)
    real = venue._request
    state = {"raised": False}

    def lose_answer_once(method, endpoint, **kw):
        body = kw.get("body") or {}
        out = real(method, endpoint, **kw)         # the order LANDED
        if (endpoint == "/v5/order/create" and body.get("category") == "linear"
                and not state["raised"]):
            state["raised"] = True
            raise TimeoutError("read timed out after submit")
        return out
    venue._request = lose_answer_once
    open_the_hedge(engine)
    assert len(perp_orders(venue)) == 1, \
        "a lost answer must be resolved by query, not by sending the leg again"
    assert venue.perp_qty > 0
    assert engine.state in (BookState.HEDGED, BookState.HALTED)
    assert_consistent(venue, journal, engine, "answer lost")


# 5 -- the same client order id comes back twice --------------------------------

def test_a_repeated_candle_does_not_open_a_second_pair():
    venue, journal, engine = build(modern)
    open_the_hedge(engine)
    created = len(venue.created)
    for _ in range(3):
        fresh_snapshot(engine)
        engine.on_candle(mark=MARK, funding_bps=3.0, spot=MARK,
                         funding_print_ms=3 * H8)
    assert len(venue.created) == created
    ids = [b["orderLinkId"] for b in venue.created]
    assert len(ids) == len(set(ids))
    assert_consistent(venue, journal, engine, "repeated candle")


# 6 -- kill switch: no entry, but the exit still works --------------------------

def test_kill_switch_blocks_entries_and_still_lets_a_held_pair_exit(tmp_path):
    store = StateStore(str(tmp_path / "s.db"))
    try:
        venue, journal, engine = build(modern)
        broker = engine.broker.inner
        broker.entry_gate = carry_entries_permitted_from(store)
        assert open_the_hedge(engine).action == "opened"      # switch clear
        store.trip_kill_switch("acceptance test")
        # an entry-side order is now refused at the broker ...
        with pytest.raises(CarryOrderRefused):
            broker.place_market(symbol="BTCUSDT", side="Sell", qty=0.001,
                                product="linear")
        # ... while the held pair still flattens when funding inverts
        invert_until_flat(engine, venue)
        assert engine.position is None and venue.perp_qty == 0.0
        assert_consistent(venue, journal, engine, "exit under kill switch")
    finally:
        store.close()


# 7 -- restart with an open position ---------------------------------------------

def test_a_restart_with_an_open_pair_reopens_nothing_and_exits_cleanly():
    venue, journal, engine = build(modern)
    open_the_hedge(engine)
    saved = engine.to_state()
    created = len(venue.created)
    reborn = CarryEngine(broker=engine.broker, max_notional_usd=100_000.0,
                         borrow_apr=0.05, execution_mode="acquire",
                         persist=lambda s: None)
    reborn.pair_risk = engine.pair_risk
    reborn.restore(saved)
    fresh_snapshot(reborn)
    assert reborn.position is not None
    reborn.on_candle(mark=MARK, funding_bps=3.0, spot=MARK,
                     funding_print_ms=4 * H8)
    assert len(venue.created) == created, "restart must not open a second pair"
    assert_consistent(venue, journal, reborn, "after restart")
    invert_until_flat(reborn, venue, start=5)
    assert reborn.position is None and venue.perp_qty == 0.0
    assert_consistent(venue, journal, reborn, "restart then exit")


# 8 -- venue and book disagree -----------------------------------------------------

def test_a_venue_position_the_book_does_not_expect_stops_all_trading():
    venue, journal, engine = build(modern)
    open_the_hedge(engine)
    venue.perp_qty *= 3                      # something else added to the short
    created = len(venue.created)
    fresh_snapshot(engine)
    d = engine.on_candle(mark=MARK, funding_bps=3.0, spot=MARK,
                         funding_print_ms=4 * H8)
    assert d.action == "halted" and d.reason == "VENUE_POSITION_DRIFT", d
    assert engine.state is BookState.HALTED
    assert len(perp_orders(venue)) == 1, "a drifted book must not trade"
    assert len(venue.created) == created


# 9 -- stale market data ------------------------------------------------------------

def test_stale_market_data_prevents_an_entry():
    venue, journal, engine = build(modern)
    for k in (1, 2):
        engine.on_candle(mark=MARK, funding_bps=3.0, spot=MARK,
                         funding_print_ms=k * H8)
    engine.snapshot = MarketSnapshot(
        perp_mark=MARK, spot_mark=MARK, funding_bps=3.0, margin_multiple=5.0,
        observed_at_s=time.time() - 3600)
    d = engine.on_candle(mark=MARK, funding_bps=3.0, spot=MARK,
                         funding_print_ms=3 * H8)
    assert d.action != "opened" and venue.created == []


# 10 -- the exchange goes away while a pair is held ---------------------------------

def test_a_disconnect_while_held_halts_instead_of_guessing():
    venue, journal, engine = build(modern)
    open_the_hedge(engine)
    real = venue._request

    def down(method, endpoint, **kw):
        if endpoint in ("/v5/position/list", "/v5/account/wallet-balance"):
            raise ConnectionError("network unreachable")
        return real(method, endpoint, **kw)
    venue._request = down
    created = len(venue.created)
    fresh_snapshot(engine)
    d = engine.on_candle(mark=MARK, funding_bps=3.0, spot=MARK,
                         funding_print_ms=4 * H8)
    assert len(venue.created) == created, "no order may be guessed while blind"
    assert d.action == "halted" and d.reason == "MARGIN_UNREADABLE", d
    assert engine.state is BookState.HALTED and engine.position is not None
