"""Paired-leg execution under failure, with a durable order record and a restart.

The real CarryEngine -> LedgerBroker -> CarryBroker, a real on-disk StateStore
as the order log, and a scripted venue. Each case ends by asking the same
questions: what does the store say each order's fate is, is anything left
unresolved, does the next cycle stay blocked while it is, and did the system
ever send an order twice. A simulated process death (SimulatedCrash, a
BaseException nothing catches) is followed by a brand-new store, broker and
engine on the same file, which is how a real restart looks. Offline; no venue.
"""
from __future__ import annotations

import os
import sys
import time

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import ledger as L                                           # noqa: E402
import order_lifecycle as ol                                 # noqa: E402
from carry_broker import (CarryBroker, OrderOutcomeUnknown,   # noqa: E402
                          PairIncident)
from carry_engine import BookState, CarryEngine              # noqa: E402
from carry_risk import CarryRisk                             # noqa: E402
from market_snapshot import MarketSnapshot                   # noqa: E402
from persistence import StateStore                           # noqa: E402
from test_carry_broker import Boom                           # noqa: E402
from test_carry_fee_truth import FeeVenue, H8, MARK, modern  # noqa: E402


class SimulatedCrash(BaseException):
    """The process dies. Not an Exception: nothing in the product may catch it."""


def stack(path, venue=None, *, style="taker"):
    """A fresh process on `path`: store, brokers, journal, engine."""
    store = StateStore(path)
    venue = venue or FeeVenue(modern)
    broker = CarryBroker(client=venue, order_gate=lambda: (True, "TEST"),
                         sequence_source=lambda p, s, w: store.next_order_seq(),
                         order_log=store)
    journal = L.Journal()
    ledger_broker = L.LedgerBroker(
        broker, journal, ms=lambda: int(time.time() * 1000) + 1,
        spot_symbol="BTCUSDT")
    engine = CarryEngine(broker=ledger_broker, max_notional_usd=100_000.0,
                         borrow_apr=0.05, execution_mode="acquire",
                         execution_style=style, maker_wait_s=0.001,
                         persist=lambda s: None)
    engine.pair_risk = CarryRisk(max_notional_usd=100_000.0, store=store,
                                 max_entries_per_day=10)
    return store, venue, broker, engine


def fresh_view(engine):
    engine.snapshot = MarketSnapshot(
        perp_mark=MARK, spot_mark=MARK, funding_bps=3.0, margin_multiple=5.0,
        observed_at_s=time.time())


def warm_and_open(engine, k=3):
    for i in (1, 2):
        fresh_view(engine)
        engine.on_candle(mark=MARK, funding_bps=3.0, spot=MARK,
                         funding_print_ms=i * H8)
    fresh_view(engine)
    return engine.on_candle(mark=MARK, funding_bps=3.0, spot=MARK,
                            funding_print_ms=k * H8, timestamp_ms=1_700_000_000_000)


def inject(venue, rule):
    real = venue._request

    def request(method, endpoint, **kw):
        body = kw.get("body") or {}
        hook = rule(method, endpoint, body, real, kw)
        return real(method, endpoint, **kw) if hook is None else hook()
    venue._request = request


def only(store, leg):
    rows = [o for o in store.orders_for_symbol("BTCUSDT")
            if o["purpose"] == f"carry-{leg}"]
    return rows


def statuses(store):
    return sorted((o["leg"], o["side"], o["status"])
                  for o in store.orders_for_symbol("BTCUSDT")
                  if str(o["purpose"]).startswith("carry-"))


@pytest.fixture()
def path(tmp_path):
    return str(tmp_path / "state.db")


# 1 -- both legs fill ---------------------------------------------------------------------

def test_both_legs_fill_and_both_orders_end_filled_in_one_cycle(path):
    store, venue, broker, engine = stack(path)
    assert warm_and_open(engine).action == "opened"
    assert statuses(store) == [("linear", "Sell", "filled"), ("spot", "Buy", "filled")]
    assert {o["cycle_id"] for o in store.orders_for_symbol("BTCUSDT")} == {
        "cyc-1700000000000"}
    assert store.unresolved_carry_orders() == []
    spot = only(store, "spot")[0]
    assert spot["filled_qty"] == pytest.approx(1.0) and spot["exchange_id"]
    kinds = [e["kind"] for e in store.order_events(spot["order_link_id"])]
    assert kinds[:2] == ["submit_attempt", "ack"] and kinds[-1] == "fill_report"
    store.close()


# 2 -- spot fills, the perp is refused -----------------------------------------------------------

def test_a_refused_perp_leg_leaves_every_order_resolved(path):
    store, venue, broker, engine = stack(path)
    inject(venue, lambda m, e, b, real, kw: (
        (lambda: (_ for _ in ()).throw(Boom(110007)))
        if e == "/v5/order/create" and b.get("category") == "linear" else None))
    warm_and_open(engine)
    assert engine.position is None
    got = statuses(store)
    assert ("linear", "Sell", "rejected") in got
    assert ("spot", "Buy", "filled") in got and ("spot", "Sell", "filled") in got
    assert store.unresolved_carry_orders() == []
    assert len({o["cycle_id"] for o in store.orders_for_symbol("BTCUSDT")}) == 1
    store.close()


# 3 -- the perp request goes unanswered -------------------------------------------------------------

def test_an_unanswered_perp_is_unresolved_and_blocks_the_next_cycle(path):
    store, venue, broker, engine = stack(path)
    attempts = []

    def silence(m, e, b, real, kw):
        if e == "/v5/order/create" and b.get("category") == "linear":
            attempts.append(b["orderLinkId"])
            return lambda: (_ for _ in ()).throw(RuntimeError("connection reset"))
    inject(venue, silence)
    warm_and_open(engine)
    perp = only(store, "linear")[0]
    assert perp["status"] == "unknown"                    # not "rejected"
    assert len(attempts) == 1                              # never re-sent
    assert [o["order_link_id"] for o in store.unresolved_carry_orders()] == [
        perp["order_link_id"]]
    # the next cycle is refused while that order is unresolved
    gate = engine.pair_risk.gate_open(
        notional_usd=100_000.0, has_open_pair=False,
        snapshot=MarketSnapshot(perp_mark=MARK, spot_mark=MARK, funding_bps=3.0,
                                margin_multiple=5.0, observed_at_s=time.time()))
    assert not gate and gate.reason == "UNRESOLVED_ORDERS"
    store.close()


# 4 -- the answer is lost but the order landed ----------------------------------------------------------

def test_a_lost_answer_for_an_order_that_landed_is_resolved_by_asking(path):
    store, venue, broker, engine = stack(path)
    state = {"done": False}

    def lose_once(m, e, b, real, kw):
        if e == "/v5/order/create" and b.get("category") == "linear" and not state["done"]:
            state["done"] = True
            def go():
                real(m, e, **kw)                      # the order LANDED
                raise TimeoutError("read timed out")
            return go
    inject(venue, lose_once)
    assert warm_and_open(engine).action == "opened"
    perp_creates = [b for b in venue.created if b["category"] == "linear"]
    assert len(perp_creates) == 1                     # never sent twice
    assert only(store, "linear")[0]["status"] == "filled"
    assert store.unresolved_carry_orders() == []
    store.close()


# 5 -- a leg fills only in part ----------------------------------------------------------------------------

def test_a_partial_fill_is_recorded_as_exactly_what_filled(path):
    store, venue, broker, engine = stack(path)
    real = venue._request

    def half_spot(method, endpoint, **kw):
        body = dict(kw.get("body") or {})
        if endpoint == "/v5/order/create" and body.get("category") == "spot" \
                and body.get("side") == "Buy":
            body["qty"] = str(float(body["qty"]) / 2)
            kw = dict(kw, body=body)
        return real(method, endpoint, **kw)
    venue._request = half_spot
    warm_and_open(engine)
    spot_buy = [o for o in only(store, "spot") if o["side"] == "Buy"][0]
    assert spot_buy["filled_qty"] == pytest.approx(0.5)
    assert spot_buy["status"] in ("filled", "cancelled", "partial")
    store.close()


# 6 -- the process dies after one leg ------------------------------------------------------------------------

def test_a_crash_after_the_first_leg_leaves_a_durable_trail(path):
    store, venue, broker, engine = stack(path)
    inject(venue, lambda m, e, b, real, kw: (
        (lambda: (_ for _ in ()).throw(SimulatedCrash()))
        if e == "/v5/order/create" and b.get("category") == "linear" else None))
    with pytest.raises(SimulatedCrash):
        warm_and_open(engine)
    # a brand-new process on the same file
    store2, venue2, broker2, engine2 = stack(path, venue)
    got = {(o["leg"], o["status"]) for o in store2.orders_for_symbol("BTCUSDT")}
    assert ("spot", "filled") in got
    assert ("linear", "submitted") in got            # sent, never answered
    unresolved = store2.unresolved_carry_orders()
    assert [o["leg"] for o in unresolved] == ["linear"]
    store2.close()


# 7 -- restart with an unresolved order: the resolver asks the venue ---------------------------------------------

def test_the_resolver_settles_an_order_that_the_venue_filled(path):
    import order_recovery as rec
    store, venue, broker, engine = stack(path)
    state = {"n": 0}

    def land_then_die(m, e, b, real, kw):
        if e == "/v5/order/create" and b.get("category") == "linear":
            def go():
                real(m, e, **kw)                    # it filled on the venue ...
                raise SimulatedCrash()              # ... and the process died
            return go
    inject(venue, land_then_die)
    with pytest.raises(SimulatedCrash):
        warm_and_open(engine)
    store.close()
    store2, venue2, broker2, engine2 = stack(path, venue)
    created = len(venue.created)
    report = rec.resolve_unresolved_orders(broker2, store2)
    assert len(venue.created) == created              # recovery sends nothing
    assert report.unresolved == []
    perp = only(store2, "linear")[0]
    # the perp hedges what the spot leg actually delivered: 1.0 less its 0.1% fee
    assert perp["status"] == "filled" and perp["filled_qty"] == pytest.approx(0.999)
    assert any(r.order_link_id == perp["order_link_id"] and r.outcome == "filled"
               for r in report.resolved)
    store2.close()


def test_the_resolver_marks_an_intent_that_never_left_as_never_sent(path):
    import order_recovery as rec
    store, venue, broker, engine = stack(path)
    store.record_order_intent("BB-carr-9-x", "BTCUSDT", "Sell", 1.0,
                              leg="linear", cycle_id="cyc-9", order_type="Market")
    report = rec.resolve_unresolved_orders(broker, store)
    assert store.get_order("BB-carr-9-x")["status"] == "never_sent"
    assert report.unresolved == [] and venue.created == []
    store.close()


def test_a_sent_order_the_venue_does_not_show_is_not_called_failed_too_soon(path):
    import order_recovery as rec
    store, venue, broker, engine = stack(path)
    store.record_order_intent("BB-carr-8-x", "BTCUSDT", "Sell", 1.0,
                              leg="linear", cycle_id="cyc-8")
    store.apply_order_event("BB-carr-8-x", ol.SUBMIT_ATTEMPT)
    young = rec.resolve_unresolved_orders(broker, store)       # just sent
    assert store.get_order("BB-carr-8-x")["status"] == "reconciliation_required"
    assert [u.order_link_id for u in young.unresolved] == ["BB-carr-8-x"]
    # past the point where a signed request can still be accepted: provably absent
    old = rec.resolve_unresolved_orders(
        broker, store, now_epoch=time.time() + rec.ABSENCE_PROOF_S + 1)
    assert store.get_order("BB-carr-8-x")["status"] == "never_sent"
    assert old.unresolved == []
    store.close()


def test_a_venue_that_cannot_be_read_leaves_everything_unresolved(path):
    import order_recovery as rec
    store, venue, broker, engine = stack(path)
    store.record_order_intent("BB-carr-7-x", "BTCUSDT", "Sell", 1.0,
                              leg="linear", cycle_id="cyc-7")
    store.apply_order_event("BB-carr-7-x", ol.SUBMIT_ATTEMPT)
    inject(venue, lambda m, e, b, real, kw: (
        (lambda: (_ for _ in ()).throw(ConnectionError("down")))
        if e.startswith("/v5/order/") and m == "GET" else None))
    report = rec.resolve_unresolved_orders(
        broker, store, now_epoch=time.time() + 10 * rec.ABSENCE_PROOF_S)
    assert [u.order_link_id for u in report.unresolved] == ["BB-carr-7-x"]
    assert store.get_order("BB-carr-7-x")["status"] == "reconciliation_required"
    store.close()


def test_the_resolver_never_abandons_exposure(path):
    import order_recovery as rec
    store, venue, broker, engine = stack(path)
    store.record_order_intent("BB-carr-6-x", "BTCUSDT", "Buy", 1.0,
                              leg="spot", cycle_id="cyc-6")
    store.apply_order_event("BB-carr-6-x", ol.SUBMIT_ATTEMPT)
    store.apply_order_event("BB-carr-6-x", ol.FILL_REPORT, filled_qty=0.4,
                            avg_price=MARK)
    inject(venue, lambda m, e, b, real, kw: (
        (lambda: (_ for _ in ()).throw(ConnectionError("down")))
        if e.startswith("/v5/order/") and m == "GET" else None))
    rec.resolve_unresolved_orders(broker, store, now_epoch=time.time() + 1e6)
    assert store.get_order("BB-carr-6-x")["status"] in (
        "partial", "reconciliation_required")
    store.close()


# 8 -- a retry of a recorded order never resends -------------------------------------------------------------------

def test_a_retry_of_an_order_with_an_unknown_outcome_asks_instead_of_sending(path):
    store, venue, broker, engine = stack(path)
    link = "BB-carr-5-fixed"
    store.record_order_intent(link, "BTCUSDT", "Sell", 1.0, leg="linear",
                              cycle_id="cyc-5")
    store.apply_order_event(link, ol.SUBMIT_ATTEMPT)
    store.apply_order_event(link, ol.TIMEOUT)
    broker._build_link_id = lambda **kw: link          # the same intent, again
    created = len(venue.created)
    with pytest.raises(OrderOutcomeUnknown):
        broker.place_market(symbol="BTCUSDT", side="Sell", qty=1.0, product="linear")
    assert len(venue.created) == created              # not sent again
    store.close()


# 9 -- cancel races -----------------------------------------------------------------------------------------------------

def test_a_cancel_that_loses_to_a_fill_ends_filled(path):
    store, venue, broker, engine = stack(path, FeeVenue(modern, post_only=(0.4, 1.0)))
    state = broker.place_post_only(symbol="BTCUSDT", side="Buy", qty=1.0,
                                   price=MARK, product="spot")
    link = only(store, "spot")[0]["order_link_id"]
    assert store.get_order(link)["status"] in ("partial", "acknowledged")
    final = broker.cancel_order(symbol="BTCUSDT", order_link_id=link, product="spot")
    row = store.get_order(link)
    assert row["filled_qty"] == pytest.approx(final["filled_qty"])
    assert row["status"] in ("filled", "cancelled")
    kinds = [e["kind"] for e in store.order_events(link)]
    assert "cancel_request" in kinds and kinds.index("cancel_request") < len(kinds) - 1
    store.close()


# 10 -- the log must not become a way to send an order -------------------------------------------------------------------

def test_an_unwritable_order_log_stops_the_order_before_it_leaves(path):
    store = StateStore(path)
    venue = FeeVenue(modern)
    broker = CarryBroker(client=venue, order_gate=lambda: (True, "TEST"),
                         sequence_source=lambda p, s, w: 7, order_log=store)
    store.close()                                       # the log is gone
    from carry_broker import CarryOrderRefused
    with pytest.raises(CarryOrderRefused, match="order log"):
        broker.place_market(symbol="BTCUSDT", side="Buy", qty=0.001, product="spot")
    assert venue.created == []


# 11 -- the process start ----------------------------------------------------------------------------------------------

def _bot_on(store, engine):
    import main as _main
    bot = _main.TradingBot.__new__(_main.TradingBot)
    bot.store = store
    bot.symbols = ["BTCUSDT"]
    bot.carry = engine
    engine.persist = store.save_carry_position
    engine.kill_switch = store.trip_kill_switch
    return bot


def test_a_start_with_an_unresolved_order_is_refused_and_trips_the_switch(path):
    store, venue, broker, engine = stack(path, FeeVenue(modern, spot_qty=0.0))
    store.record_order_intent("BB-carr-4-x", "BTCUSDT", "Sell", 1.0,
                              leg="linear", cycle_id="cyc-4")
    store.apply_order_event("BB-carr-4-x", ol.SUBMIT_ATTEMPT)
    store.apply_order_event("BB-carr-4-x", ol.TIMEOUT)
    created = len(venue.created)
    bot = _bot_on(store, engine)
    assert bot.carry_cold_start() is False
    assert store.is_kill_switch_engaged()[0] is True
    assert len(venue.created) == created                  # recovery sent nothing
    assert store.get_order("BB-carr-4-x")["status"] == "reconciliation_required"
    store.close()


def test_a_start_with_only_a_never_sent_intent_settles_it_and_goes_on(path):
    store, venue, broker, engine = stack(path, FeeVenue(modern, spot_qty=0.0))
    store.record_order_intent("BB-carr-3-x", "BTCUSDT", "Sell", 1.0,
                              leg="linear", cycle_id="cyc-3")
    bot = _bot_on(store, engine)
    assert bot.carry_cold_start() is True
    assert store.get_order("BB-carr-3-x")["status"] == "never_sent"
    assert store.is_kill_switch_engaged()[0] is False
    store.close()


def test_a_start_with_an_unreadable_order_log_is_refused(path):
    store, venue, broker, engine = stack(path, FeeVenue(modern, spot_qty=0.0))
    bot = _bot_on(store, engine)
    store.unresolved_carry_orders = lambda: (_ for _ in ()).throw(
        RuntimeError("log corrupt"))
    assert bot.carry_cold_start() is False
    store.close()


def test_the_next_cycle_stays_blocked_after_the_crash_until_it_is_resolved(path):
    store, venue, broker, engine = stack(path)
    inject(venue, lambda m, e, b, real, kw: (
        (lambda: (_ for _ in ()).throw(SimulatedCrash()))
        if e == "/v5/order/create" and b.get("category") == "linear" else None))
    with pytest.raises(SimulatedCrash):
        warm_and_open(engine)
    store.close()
    store2, venue2, broker2, engine2 = stack(path, venue)
    created = len(venue.created)
    # the new process tries to trade at once: nothing is sent
    d = warm_and_open(engine2, k=3)
    assert d.action != "opened" and d.reason == "UNRESOLVED_ORDERS"
    assert len(venue.created) == created
    store2.close()


# 12 -- the absence window is derived and measured from the request, not the intent ----------------

def test_the_absence_window_covers_the_transports_worst_case():
    import order_recovery as rec
    w = rec.absence_window_s(request_timeout_s=10.0, recv_window_ms=5000,
                             attempts=4, max_wait_s=2.0)
    assert w >= 4 * 10.0 + 3 * 2.0 + 5.0                  # every attempt, every wait, the window
    assert w >= rec.ABSENCE_PROOF_S                       # never below the floor


def test_a_slower_transport_widens_the_window():
    import order_recovery as rec
    base = rec.absence_window_s(request_timeout_s=20.0, recv_window_ms=5000,
                                attempts=4, max_wait_s=2.0)
    assert base > rec.ABSENCE_PROOF_S                      # above the floor
    assert rec.absence_window_s(request_timeout_s=40.0, recv_window_ms=5000,
                                attempts=4, max_wait_s=2.0) > base
    assert rec.absence_window_s(request_timeout_s=20.0, recv_window_ms=10000,
                                attempts=4, max_wait_s=2.0) > base


def test_the_default_transport_never_gets_less_than_the_floor():
    import order_recovery as rec
    assert rec.absence_window_s(request_timeout_s=10.0, recv_window_ms=5000,
                                attempts=4, max_wait_s=2.0) == rec.ABSENCE_PROOF_S


@pytest.mark.parametrize("bad", [dict(request_timeout_s=0), dict(request_timeout_s=float("nan")),
                                 dict(recv_window_ms=-1), dict(attempts=0)])
def test_nonsense_transport_numbers_are_refused(bad):
    import order_recovery as rec
    kw = dict(request_timeout_s=10.0, recv_window_ms=5000, attempts=4, max_wait_s=2.0)
    kw.update(bad)
    with pytest.raises(ValueError):
        rec.absence_window_s(**kw)


def test_absence_is_measured_from_the_request_not_from_the_intent(path):
    import order_recovery as rec
    store, venue, broker, engine = stack(path)
    store.record_order_intent("BB-carr-2-late", "BTCUSDT", "Sell", 1.0,
                              leg="linear", cycle_id="cyc-2")
    # the intent is old, but the request left only now
    store._exec("UPDATE orders SET created_epoch = ? WHERE order_link_id = ?",
                (time.time() - 3600.0, "BB-carr-2-late"))
    store.apply_order_event("BB-carr-2-late", ol.SUBMIT_ATTEMPT)
    report = rec.resolve_unresolved_orders(broker, store)
    assert store.get_order("BB-carr-2-late")["status"] == "reconciliation_required"
    assert [u.order_link_id for u in report.unresolved] == ["BB-carr-2-late"]
    store.close()


def test_a_caller_supplied_window_is_honoured(path):
    import order_recovery as rec
    store, venue, broker, engine = stack(path)
    store.record_order_intent("BB-carr-2-w", "BTCUSDT", "Sell", 1.0,
                              leg="linear", cycle_id="cyc-2")
    store.apply_order_event("BB-carr-2-w", ol.SUBMIT_ATTEMPT)
    rec.resolve_unresolved_orders(broker, store, now_epoch=time.time() + 100.0,
                                  absence_proof_s=500.0)
    assert store.get_order("BB-carr-2-w")["status"] == "reconciliation_required"
    rec.resolve_unresolved_orders(broker, store, now_epoch=time.time() + 600.0,
                                  absence_proof_s=500.0)
    assert store.get_order("BB-carr-2-w")["status"] == "never_sent"
    store.close()
