"""Capital reservations: atomic, persisted, idempotent, and never double-spent.

Drives the real StateStore on a temp file (reopened to prove restarts). Proves:
two strategies racing for the same dollars cannot both win (threads on one
store AND threads on two connections to one file); state survives restart;
replays are idempotent; refusals say why; expiry frees only claims no
unresolved order may be using. Offline.
"""
from __future__ import annotations

import os
import sqlite3
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import capital_reservations as cr                       # noqa: E402
from persistence import StateStore                      # noqa: E402
from portfolio_state import confirmed, unavailable      # noqa: E402

SCOPE = "bybit:UNIFIED"
SRC = "venue:CarryBroker.get_available_margin_usd"


def avail(value: float, at: float) -> object:
    return confirmed("available_capital", value, at, SRC)


@pytest.fixture()
def path(tmp_path):
    return str(tmp_path / "s.db")


@pytest.fixture()
def store(path):
    s = StateStore(path)
    yield s
    try:
        s.close()
    except sqlite3.ProgrammingError:
        pass


def make(store, buffer_usd=10.0):
    return cr.CapitalReservations(store, buffer_usd=buffer_usd,
                                  max_available_age_s=60.0)


class TestConstruction:
    def test_buffer_and_age_are_required_and_validated(self, store):
        with pytest.raises(TypeError):
            cr.CapitalReservations(store)  # type: ignore[call-arg]
        with pytest.raises(ValueError):
            cr.CapitalReservations(store, buffer_usd=-1, max_available_age_s=60)
        with pytest.raises(ValueError):
            cr.CapitalReservations(store, buffer_usd=0, max_available_age_s=0)
        with pytest.raises(ValueError):
            cr.CapitalReservations(store, buffer_usd=float("nan"),
                                   max_available_age_s=60)

    def test_schema_is_idempotent(self, store):
        make(store)
        make(store)
        names = {r["name"] for r in store._query(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"capital_reservation", "capital_reservation_event",
                "capital_reservation_lock"} <= names


class TestReserve:
    def test_reserve_within_free_capital(self, store):
        r = make(store)
        now = time.time()
        res = r.reserve("carry", "i-1", 50.0, SCOPE, "entry",
                        available=avail(100.0, now), ttl_s=30, now=now)
        assert res.ok and res.code == cr.OK_RESERVED
        assert res.detail["free_usd"] == pytest.approx(90.0)
        assert r.live_total_usd(SCOPE) == 50.0

    def test_buffer_is_held_back(self, store):
        r = make(store, buffer_usd=10.0)
        now = time.time()
        res = r.reserve("carry", "i-1", 95.0, SCOPE, "entry",
                        available=avail(100.0, now), ttl_s=30, now=now)
        assert not res.ok and res.code == cr.INSUFFICIENT_FREE_CAPITAL
        assert res.detail["buffer_usd"] == 10.0

    def test_second_strategy_cannot_take_the_same_dollars(self, store):
        r = make(store, buffer_usd=0.0)
        now = time.time()
        assert r.reserve("a", "i-a", 60.0, SCOPE, "entry",
                         available=avail(100.0, now), ttl_s=30, now=now)
        res = r.reserve("b", "i-b", 60.0, SCOPE, "entry",
                        available=avail(100.0, now), ttl_s=30, now=now)
        assert not res.ok and res.code == cr.INSUFFICIENT_FREE_CAPITAL
        assert res.detail["live_reserved_usd"] == 60.0

    def test_other_scope_is_independent(self, store):
        r = make(store, buffer_usd=0.0)
        now = time.time()
        assert r.reserve("a", "i-a", 60.0, SCOPE, "entry",
                         available=avail(100.0, now), ttl_s=30, now=now)
        assert r.reserve("b", "i-b", 60.0, "other", "entry",
                         available=avail(100.0, now), ttl_s=30, now=now)

    @pytest.mark.parametrize("kw,code", [
        ({"amount_usd": 0.0}, cr.AMOUNT_INVALID),
        ({"amount_usd": float("nan")}, cr.AMOUNT_INVALID),
        ({"amount_usd": -5.0}, cr.AMOUNT_INVALID),
        ({"ttl_s": 0}, cr.TTL_INVALID),
        ({"ttl_s": float("inf")}, cr.TTL_INVALID),
        ({"strategy_id": ""}, cr.FIELD_INVALID),
        ({"account_scope": " "}, cr.FIELD_INVALID),
    ])
    def test_invalid_arguments_refuse(self, store, kw, code):
        r = make(store)
        now = time.time()
        args = dict(strategy_id="a", intent_id="i", amount_usd=5.0,
                    account_scope=SCOPE, purpose="entry", ttl_s=30)
        args.update(kw)
        res = r.reserve(args.pop("strategy_id"), args.pop("intent_id"),
                        args.pop("amount_usd"), args.pop("account_scope"),
                        args.pop("purpose"), available=avail(100.0, now),
                        now=now, **args)
        assert not res.ok and res.code == code
        assert r.live_total_usd() == 0.0

    def test_unconfirmed_available_refuses(self, store):
        r = make(store)
        res = r.reserve("a", "i", 5.0, SCOPE, "entry",
                        available=unavailable("available_capital", "raised"),
                        ttl_s=30)
        assert not res.ok and res.code == cr.AVAILABLE_UNCONFIRMED

    def test_stale_available_refuses(self, store):
        r = make(store)
        now = time.time()
        res = r.reserve("a", "i", 5.0, SCOPE, "entry",
                        available=avail(100.0, now - 61), ttl_s=30, now=now)
        assert not res.ok and res.code == cr.AVAILABLE_STALE

    def test_unsourced_available_refuses(self, store):
        r = make(store)
        now = time.time()
        res = r.reserve("a", "i", 5.0, SCOPE, "entry",
                        available=confirmed("available_capital", 100.0, now, " "),
                        ttl_s=30, now=now)
        assert not res.ok and res.code == cr.AVAILABLE_INVALID

    def test_refusals_are_journalled(self, store):
        r = make(store)
        now = time.time()
        r.reserve("a", "i", 500.0, SCOPE, "entry", available=avail(100.0, now),
                  ttl_s=30, now=now)
        ev = r.events("i")
        assert ev and ev[-1]["kind"] == "REFUSE"
        assert cr.INSUFFICIENT_FREE_CAPITAL in ev[-1]["detail"]


class TestIdempotency:
    def test_replay_claims_nothing_new(self, store):
        r = make(store, buffer_usd=0.0)
        now = time.time()
        a = r.reserve("a", "i", 60.0, SCOPE, "entry", available=avail(100.0, now),
                      ttl_s=30, now=now)
        b = r.reserve("a", "i", 60.0, SCOPE, "entry", available=avail(100.0, now),
                      ttl_s=30, now=now)
        assert a.code == cr.OK_RESERVED and b.ok and b.code == cr.OK_IDEMPOTENT
        assert r.live_total_usd() == 60.0

    def test_same_id_different_terms_is_a_conflict(self, store):
        r = make(store, buffer_usd=0.0)
        now = time.time()
        r.reserve("a", "i", 10.0, SCOPE, "entry", available=avail(100.0, now),
                  ttl_s=30, now=now)
        res = r.reserve("b", "i", 10.0, SCOPE, "entry",
                        available=avail(100.0, now), ttl_s=30, now=now)
        assert not res.ok and res.code == cr.INTENT_ID_CONFLICT

    def test_released_id_is_never_reused(self, store):
        r = make(store, buffer_usd=0.0)
        now = time.time()
        r.reserve("a", "i", 10.0, SCOPE, "entry", available=avail(100.0, now),
                  ttl_s=30, now=now)
        assert r.release("i", "done", now=now).ok
        assert r.release("i", "again", now=now).code == cr.OK_IDEMPOTENT
        res = r.reserve("a", "i", 10.0, SCOPE, "entry",
                        available=avail(100.0, now), ttl_s=30, now=now)
        assert not res.ok and res.code == cr.INTENT_ID_SPENT


class TestCommitRelease:
    def test_commit_then_release_after_order_terminal(self, store):
        r = make(store, buffer_usd=0.0)
        now = time.time()
        r.reserve("a", "i", 40.0, SCOPE, "entry", available=avail(100.0, now),
                  ttl_s=30, now=now)
        store.record_order_intent("L-1", "BTCUSDT", "Sell", 0.01, leg="linear",
                                  cycle_id="cyc-1")
        assert r.commit("i", "L-1", now=now).code == cr.OK_COMMITTED
        assert r.commit("i", "L-1", now=now).code == cr.OK_IDEMPOTENT
        assert r.commit("i", "L-2", now=now).code == cr.ORDER_LINK_CONFLICT
        # order is pending (unresolved): capital may be in use
        res = r.release("i", "try", now=now)
        assert not res.ok and res.code == cr.ORDER_UNRESOLVED
        assert r.live_total_usd() == 40.0
        store.apply_order_event("L-1", "never_sent")
        assert r.release("i", "order never sent", now=now).code == cr.OK_RELEASED
        assert r.live_total_usd() == 0.0

    def test_unknown_reservation(self, store):
        r = make(store)
        assert r.commit("nope", "L").code == cr.NO_SUCH_RESERVATION
        assert r.release("nope", "x").code == cr.NO_SUCH_RESERVATION


class TestExpiry:
    def test_stale_reservation_expires_and_frees(self, store):
        r = make(store, buffer_usd=0.0)
        now = time.time()
        r.reserve("a", "i-old", 80.0, SCOPE, "entry", available=avail(100.0, now),
                  ttl_s=5, now=now)
        later = now + 10
        res = r.reserve("b", "i-new", 80.0, SCOPE, "entry",
                        available=avail(100.0, later), ttl_s=5, now=later)
        assert res.ok
        assert r.get("i-old")["state"] == cr.EXPIRED

    def test_expiry_never_frees_capital_an_unresolved_order_may_use(self, store):
        r = make(store, buffer_usd=0.0)
        now = time.time()
        r.reserve("a", "i-old", 80.0, SCOPE, "entry", available=avail(100.0, now),
                  ttl_s=5, now=now)
        # The caller sent an order using the intent id as its link, without
        # committing; it is still unresolved at the venue.
        store.record_order_intent("i-old", "BTCUSDT", "Sell", 0.01,
                                  leg="linear", cycle_id="cyc-1")
        store.apply_order_event("i-old", "submit_attempt")
        later = now + 10
        assert r.expire_stale(now=later) == []
        assert r.get("i-old")["state"] == cr.RESERVED
        res = r.reserve("b", "i-new", 80.0, SCOPE, "entry",
                        available=avail(100.0, later), ttl_s=5, now=later)
        assert not res.ok and res.code == cr.INSUFFICIENT_FREE_CAPITAL
        assert any(e["kind"] == "EXPIRY_DEFERRED" for e in r.events("i-old"))

    def test_committed_reservations_never_expire_on_a_timer(self, store):
        r = make(store, buffer_usd=0.0)
        now = time.time()
        r.reserve("a", "i", 80.0, SCOPE, "entry", available=avail(100.0, now),
                  ttl_s=5, now=now)
        r.commit("i", "L-x", now=now)
        assert r.expire_stale(now=now + 1e6) == []
        assert r.get("i")["state"] == cr.COMMITTED


class TestRestart:
    def test_reservations_survive_restart(self, path):
        s1 = StateStore(path)
        r1 = make(s1, buffer_usd=0.0)
        now = time.time()
        assert r1.reserve("a", "i-a", 70.0, SCOPE, "entry",
                          available=avail(100.0, now), ttl_s=300, now=now)
        s1.close()
        s2 = StateStore(path)
        r2 = make(s2, buffer_usd=0.0)
        assert r2.get("i-a")["state"] == cr.RESERVED
        res = r2.reserve("b", "i-b", 70.0, SCOPE, "entry",
                         available=avail(100.0, now), ttl_s=300, now=now)
        assert not res.ok and res.code == cr.INSUFFICIENT_FREE_CAPITAL
        # replay of the original after restart is still idempotent
        assert r2.reserve("a", "i-a", 70.0, SCOPE, "entry",
                          available=avail(100.0, now), ttl_s=300,
                          now=now).code == cr.OK_IDEMPOTENT
        s2.close()


def _race(reservers, amount=60.0, available=100.0):
    now = time.time()
    barrier = threading.Barrier(len(reservers))
    out = [None] * len(reservers)

    def run(i, r):
        barrier.wait()
        out[i] = r.reserve(f"strat-{i}", f"intent-{i}", amount, SCOPE, "entry",
                           available=avail(available, now), ttl_s=300, now=now)
    threads = [threading.Thread(target=run, args=(i, r))
               for i, r in enumerate(reservers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    return out


class TestRace:
    @pytest.mark.parametrize("trial", range(5))
    def test_threads_on_one_store_cannot_double_reserve(self, store, trial):
        r = make(store, buffer_usd=0.0)
        out = _race([r] * 8)
        assert sum(1 for x in out if x.ok) == 1
        assert all(x.code == cr.INSUFFICIENT_FREE_CAPITAL for x in out if not x.ok)
        assert r.live_total_usd(SCOPE) == 60.0

    @pytest.mark.parametrize("trial", range(5))
    def test_two_connections_to_one_file_cannot_double_reserve(self, path, trial):
        stores = [StateStore(path) for _ in range(4)]
        try:
            rs = [make(s, buffer_usd=0.0) for s in stores]
            out = _race(rs)
            assert all(x is not None for x in out)
            assert sum(1 for x in out if x.ok) == 1, [x.code for x in out]
            assert rs[0].live_total_usd(SCOPE) == 60.0
        finally:
            for s in stores:
                s.close()


def test_reading_from_far_future_is_refused(store):
    r = make(store)
    now = time.time()
    res = r.reserve("a", "i", 5.0, SCOPE, "entry",
                    available=avail(100.0, now + 3600), ttl_s=30, now=now)
    assert not res.ok and res.code == cr.AVAILABLE_STALE
