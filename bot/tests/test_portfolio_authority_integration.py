"""Strategy -> portfolio -> capital reservation -> risk -> lifecycle -> venue.

The portfolio authority is a gate every ENTRY passes before the pair gate and a
capital reservation every entry holds while its dollars are in use; exits never
ask it. Real CarryEngine, LedgerBroker, CarryBroker, StateStore and
portfolio_state / capital_reservations; only the venue is scripted. Offline.
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
import portfolio_state as ps                                 # noqa: E402
import capital_reservations as cr                            # noqa: E402
from carry_capital import CarryCapitalAuthority              # noqa: E402
from test_paired_leg_recovery import stack, fresh_view      # noqa: E402
from test_pretrade_capital_check import with_available       # noqa: E402
from test_carry_fee_truth import FeeVenue, H8, MARK, modern  # noqa: E402

LIMITS = ps.PortfolioLimits(max_daily_loss_fraction=0.02,
                            max_drawdown_fraction=0.10,
                            max_state_age_s=90.0, max_total_capital_usd=None)
SCOPE = "venue=testnet;UNIFIED"
TOTAL = "500000"


def wired(path, *, total=TOTAL, clock=time.time, seed_clean=True):
    store, venue, broker, engine = stack(path, FeeVenue(modern, spot_qty=0.0))
    with_available(venue, total)
    if seed_clean:
        store.update_equity(float(total))
        store.save_ledger(L.Journal().to_rows())
        store.save_carry_position({"book_state": "FLAT", "position": None})
    reservations = cr.CapitalReservations(store, buffer_usd=0.0,
                                          max_available_age_s=90.0)
    authority = CarryCapitalAuthority(
        store=store, broker=broker, spot_symbol="BTCUSDT", perp_symbol="BTCUSDT",
        reservations=reservations, limits=LIMITS, account_scope=SCOPE,
        ttl_s=600.0, clock=clock)
    engine.capital_authority = authority
    return store, venue, broker, engine, authority, reservations


@pytest.fixture()
def path(tmp_path):
    return str(tmp_path / "s.db")


def opened(d):
    return d.action == "opened"


def warm_and_open(engine, k=3):
    """The paired-leg helper with every candle on the same clock (timestamp 0),
    as the acceptance tests drive the engine: mixing a real-time entry candle
    with zero-stamped exit candles makes the engine see the clock go backwards
    and discard the prints, which is a harness artefact, not the behaviour
    under test."""
    for i in (1, 2):
        fresh_view(engine)
        engine.on_candle(mark=MARK, funding_bps=3.0, spot=MARK,
                         funding_print_ms=i * H8)
    fresh_view(engine)
    return engine.on_candle(mark=MARK, funding_bps=3.0, spot=MARK,
                            funding_print_ms=k * H8)


# -- the clean path ----------------------------------------------------------------

def test_a_clean_account_opens_and_holds_a_reservation_for_the_cycle(path):
    store, venue, broker, engine, auth, res = wired(path)
    d = warm_and_open(engine)
    assert opened(d), (d.reason, d.detail)
    live = res.live(SCOPE)
    assert len(live) == 1 and live[0]["strategy_id"] == "carry"
    cycle = f"cyc-{engine.position.opened_ms}"
    assert live[0]["intent_id"] == cycle
    need = engine.required_capital_usd(100_000.0) + engine.fee_reserve_usd(100_000.0)
    assert live[0]["amount_usd"] == pytest.approx(need)
    store.close()


# -- each refusal blocks new exposure and sends nothing ------------------------------------

def blocked(engine, venue, d, code):
    assert d.action == "stand_aside" and d.reason == "PORTFOLIO_BLOCKED", (d.reason, d.detail)
    assert any(b.startswith(code) for b in d.detail["blockers"]), d.detail
    assert venue.created == []


def test_an_unresolved_order_blocks_the_entry(path):
    store, venue, broker, engine, auth, res = wired(path)
    store.record_order_intent("BB-carr-1-x", "BTCUSDT", "Sell", 1.0,
                              leg="linear", cycle_id="cyc-1")
    store.apply_order_event("BB-carr-1-x", ol.SUBMIT_ATTEMPT)
    store.apply_order_event("BB-carr-1-x", ol.TIMEOUT)
    blocked(engine, venue, warm_and_open(engine), "UNRESOLVED_ORDERS")
    store.close()


def test_the_kill_switch_blocks_the_entry(path):
    store, venue, broker, engine, auth, res = wired(path)
    store.trip_kill_switch("test")
    blocked(engine, venue, warm_and_open(engine), "KILL_SWITCH_ENGAGED")
    store.close()


def test_a_stale_balance_blocks_the_entry(path):
    now = [time.time()]
    store, venue, broker, engine, auth, res = wired(path, clock=lambda: now[0])
    real = auth._state

    def late():
        now[0] += 3600.0                      # the clock moves on between read and use
        return real()
    auth._state = late
    d = warm_and_open(engine)
    assert d.reason in ("PORTFOLIO_BLOCKED", "CAPITAL_RESERVATION_REFUSED")
    assert venue.created == []
    store.close()


def test_a_venue_position_the_ledger_does_not_know_blocks_the_entry(path):
    store, venue, broker, engine, auth, res = wired(path)
    venue.perp_qty = 0.5                      # the venue holds a short the books do not
    blocked(engine, venue, warm_and_open(engine), "CONTRADICTION")
    store.close()


def test_an_unknown_fee_blocks_the_entry(path):
    store, venue, broker, engine, auth, res = wired(path, seed_clean=False)
    j = L.Journal()
    L.perp_open(j, ms=1_700_000_000_000, ref="o", qty=1.0, price=MARK,
                fee_usd=0.0, fee_status="unknown")
    L.perp_close(j, ms=1_700_000_100_000, ref="c", qty=1.0, price=MARK,
                 entry_price=MARK, fee_usd=0.0, fee_status="confirmed")
    store.update_equity(500000.0)
    store.save_ledger(j.to_rows())
    store.save_carry_position({"book_state": "FLAT", "position": None})
    blocked(engine, venue, warm_and_open(engine), "FEE_UNKNOWN")
    store.close()


def test_an_account_whose_equity_was_never_recorded_cannot_enter(path):
    store, venue, broker, engine, auth, res = wired(path, seed_clean=False)
    store.save_ledger(L.Journal().to_rows())
    store.save_carry_position({"book_state": "FLAT", "position": None})
    blocked(engine, venue, warm_and_open(engine), "REQUIRED_UNAVAILABLE")
    store.close()


def test_a_portfolio_read_that_raises_refuses_the_entry(path):
    store, venue, broker, engine, auth, res = wired(path)
    auth._state = lambda: (_ for _ in ()).throw(RuntimeError("boom"))
    d = warm_and_open(engine)
    assert d.action == "stand_aside" and d.reason == "PORTFOLIO_STATE_UNREADABLE"
    assert venue.created == []
    store.close()


# -- capital cannot be double-spent -----------------------------------------------------------

def test_capital_another_strategy_reserved_is_not_available_to_carry(path):
    store, venue, broker, engine, auth, res = wired(path)
    state = auth._state()
    other = res.reserve("other", "other-1", 450_000.0, SCOPE, "entry",
                        available=state.available_capital, ttl_s=600.0)
    assert other.ok, other.code
    d = warm_and_open(engine)
    assert d.action == "stand_aside" and d.reason in (
        "CAPITAL_RESERVATION_REFUSED", "PORTFOLIO_BLOCKED")
    assert venue.created == []
    store.close()


def test_a_second_strategy_cannot_take_the_dollars_carry_is_holding(path):
    store, venue, broker, engine, auth, res = wired(path)
    assert opened(warm_and_open(engine))
    state = auth._state()
    other = res.reserve("other", "other-2", 400_000.0, SCOPE, "entry",
                        available=state.available_capital, ttl_s=600.0)
    assert not other.ok and other.code == "INSUFFICIENT_FREE_CAPITAL"
    store.close()


def test_a_flat_book_with_nothing_in_flight_gives_its_reservation_back(path):
    store, venue, broker, engine, auth, res = wired(path)
    state = auth._state()
    res.reserve("carry", "cyc-orphan", 1000.0, SCOPE, "entry",
                available=state.available_capital, ttl_s=600.0)
    assert res.live(SCOPE)
    fresh_view(engine)
    engine.on_candle(mark=MARK, funding_bps=3.0, spot=MARK, funding_print_ms=H8)
    assert res.live(SCOPE) == []
    store.close()


def test_a_held_pair_keeps_its_reservation(path):
    store, venue, broker, engine, auth, res = wired(path)
    assert opened(warm_and_open(engine))
    fresh_view(engine)
    engine.on_candle(mark=MARK, funding_bps=3.0, spot=MARK, funding_print_ms=4 * H8)
    assert len(res.live(SCOPE)) == 1
    store.close()


# -- reducing risk is never blocked by the portfolio ------------------------------------------------

def test_a_held_pair_still_exits_while_the_portfolio_blocks_every_entry(path):
    store, venue, broker, engine, auth, res = wired(path)
    assert opened(warm_and_open(engine))
    store.save_ledger(engine.broker.journal.to_rows())
    store.trip_kill_switch("test: entries blocked")          # entries now refused
    assert auth.entries_permitted()[0] is False
    stamp = 4 * H8
    for _ in range(10):
        fresh_view(engine)
        engine.on_candle(mark=MARK, funding_bps=-4.0, spot=MARK,
                         funding_print_ms=stamp)
        stamp += H8
        if engine.position is None:
            break
    assert engine.position is None and venue.perp_qty == 0.0
    store.close()


def test_the_authority_always_allows_reduction(path):
    store, venue, broker, engine, auth, res = wired(path)
    store.trip_kill_switch("test")
    assert auth.reduction_allowed() is True
    store.close()


# -- holds are bound to their orders before they can expire; ids never repeat ------------------

def test_a_hold_for_an_unresolved_first_leg_survives_its_ttl(path):
    """Code-review finding: with the entry halted and flat, nothing committed the
    hold, so it expired after the TTL and another strategy could spend dollars an
    unanswered order may be using."""
    from test_paired_leg_recovery import inject
    now = [time.time()]
    store, venue, broker, engine, auth, res = wired(path, clock=lambda: now[0])
    inject(venue, lambda m, e, b, real, kw: (
        (lambda: (_ for _ in ()).throw(RuntimeError("connection reset")))
        if e == "/v5/order/create" and b.get("category") == "linear" else None))
    warm_and_open(engine)
    assert store.unresolved_carry_orders()                    # the perp leg is unanswered
    assert len(res.live(SCOPE)) == 1
    now[0] += 3600.0                                          # far beyond the 600s TTL
    auth.sync(position_open=False)
    live = res.live(SCOPE)
    assert len(live) == 1 and live[0]["state"] == "COMMITTED"
    other = res.reserve("other", "other-9", 400_000.0, SCOPE, "entry",
                        available=auth._state().available_capital, ttl_s=600.0,
                        now=now[0])
    assert not other.ok and other.code == "INSUFFICIENT_FREE_CAPITAL"
    store.close()


def test_a_tick_interval_longer_than_the_ttl_does_not_lose_a_held_pairs_hold(path):
    now = [time.time()]
    store, venue, broker, engine, auth, res = wired(path, clock=lambda: now[0])
    assert opened(warm_and_open(engine))
    now[0] += 7200.0                                          # one very slow tick
    auth.sync(position_open=True)
    live = res.live(SCOPE)
    assert len(live) == 1 and live[0]["state"] == "COMMITTED"
    store.close()


def test_two_cycles_stamped_alike_get_distinct_ids_and_both_can_reserve(path):
    store, venue, broker, engine, auth, res = wired(path)
    assert opened(warm_and_open(engine))
    first = engine.position.cycle_id
    stamp = 4 * H8
    for _ in range(12):                                       # invert, unwind
        fresh_view(engine)
        engine.on_candle(mark=MARK, funding_bps=-4.0, spot=MARK, funding_print_ms=stamp)
        stamp += H8
        if engine.position is None:
            break
    assert engine.position is None
    store.save_ledger(engine.broker.journal.to_rows())
    for _ in range(6):                                        # funding recovers, reopen
        fresh_view(engine)
        d = engine.on_candle(mark=MARK, funding_bps=3.0, spot=MARK, funding_print_ms=stamp)
        stamp += H8
        if opened(d):
            break
    else:
        pytest.fail(f"the second cycle never opened: {d.reason} {d.detail}")
    second = engine.position.cycle_id
    assert second != first                                    # same default clock, new id
    assert {e.cycle for e in engine.broker.journal.entries if e.cycle} == {first, second}
    store.close()


def test_a_bumped_cycle_stamp_never_becomes_the_daily_limit_clock(path):
    """Cycle identity is made unique; the per-day entry limit keeps the caller's
    clock, so a second default-clock entry is not dated 1970."""
    store, venue, broker, engine, auth, res = wired(path)
    assert opened(warm_and_open(engine))
    assert engine._entry_clock_ms == 0
    assert engine._gate_now(engine._entry_clock_ms) is None   # wall clock
    assert engine.position.opened_ms >= 0
    store.close()
