"""The read-only portfolio state: sources, statuses, contradictions, blockers.

Every refusal path (stale state, unresolved order, unknown fee, kill switch,
contradiction, limit exceeded) must block NEW exposure and must never block
reduction_allowed(). Real StateStore on a temp file, a fake broker for the
CarryBroker read methods. Offline: nothing here can reach a venue.
"""
from __future__ import annotations

import os
import sqlite3
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import capital_reservations as cr                       # noqa: E402
import ledger as lg                                     # noqa: E402
import portfolio_state as ps                            # noqa: E402
from persistence import StateStore                      # noqa: E402

LIMITS = ps.PortfolioLimits(max_daily_loss_fraction=0.02,
                            max_drawdown_fraction=0.10,
                            max_state_age_s=60.0,
                            max_total_capital_usd=None)


class FakeBroker:
    """Only the read methods portfolio_state uses. No transport at all."""

    def __init__(self, **over):
        self.v = {"available": 1000.0, "quote": 500.0, "spot": 0.0,
                  "spot_mark": 60000.0, "perp": 0.0, "orders": [],
                  "liq": {"size": 0.0, "side": "", "mark": 60000.0,
                          "liq_price": None, "distance_pct": None,
                          "account_mm_rate": 0.01, "margin_mode": "REGULAR_MARGIN",
                          "reason": "NO_POSITION"}}
        self.v.update(over)

    def _get(self, k):
        v = self.v[k]
        if isinstance(v, Exception):
            raise v
        return v

    def get_available_margin_usd(self):
        return self._get("available")

    def get_quote_free_usd(self, coin="USDT"):
        return self._get("quote")

    def get_spot_inventory(self, symbol):
        return self._get("spot")

    def get_spot_mark(self, symbol):
        return self._get("spot_mark")

    def get_perp_position(self, symbol):
        return self._get("perp")

    def get_open_carry_orders(self, spot_symbol, perp_symbol):
        return self._get("orders")

    def get_liquidation_view(self, symbol):
        return self._get("liq")


@pytest.fixture()
def store(tmp_path):
    s = StateStore(str(tmp_path / "s.db"))
    yield s
    try:
        s.close()
    except sqlite3.ProgrammingError:
        pass


def seed_clean(store, journal=None):
    store.update_equity(1000.0)
    store.save_ledger((journal or lg.Journal()).to_rows())
    store.save_carry_position({"book_state": "FLAT", "position": None})


def build(store, broker=None, now=None, limits=LIMITS, **kw):
    venue = ps.read_venue(broker or FakeBroker(), spot_symbol="BTCUSDT",
                          perp_symbol="BTCUSDT")
    return ps.build_portfolio_state(store, limits=limits, venue=venue,
                                    now_epoch=now or time.time(), **kw)


def held_journal(qty=0.01, fee_status="confirmed"):
    j = lg.Journal()
    lg.spot_buy(j, ms=1_000, ref="s1", qty=qty, price=60000.0, fee_btc=0.0,
                fee_usdt=0.6, fee_status=fee_status)
    lg.perp_open(j, ms=1_001, ref="p1", qty=qty, price=60000.0, fee_usd=0.33,
                 fee_status=fee_status)
    return j


def assert_blocked_but_reducible(state, code_prefix):
    blockers = state.entry_blockers()
    assert any(b.split(":")[0] == code_prefix for b in blockers), blockers
    ok, why = state.entries_permitted()
    assert not ok and code_prefix in why
    assert state.reduction_allowed() is True
    for purpose in ps.REDUCTION_PURPOSES:
        assert state.reduction_allowed(purpose) is True


class TestBaseline:
    def test_clean_flat_book_has_no_blockers(self, store):
        seed_clean(store)
        st = build(store)
        assert st.entry_blockers() == []
        assert st.entries_permitted() == (True, "portfolio clear")
        assert st.reduction_allowed()

    def test_every_field_carries_source_status_and_time(self, store):
        seed_clean(store)
        st = build(store)
        for name, f in st.fields.items():
            assert f.status in ps.STATUSES, name
            assert f.source, name
            if f.status == ps.UNAVAILABLE:
                assert f.value is None and f.reason, name
        assert st.available_capital.value == 1000.0
        assert st.available_capital.source == ps.AUTHORITY["available_capital"]
        assert st.account_equity.value == 1000.0
        assert st.kill_switch.value is False
        assert st.drawdown.value == 0.0 and st.daily_loss.value == 0.0
        assert st.realised_pnl.value == 0.0  # no fills: a confirmed zero

    def test_unsourced_fields_are_unavailable_never_zero(self, store):
        seed_clean(store)
        st = build(store)
        for name in ("fees_due", "funding_accrued", "unrealised_pnl"):
            f = st.fields[name]
            assert f.status == ps.UNAVAILABLE and f.value is None and f.reason

    def test_reserved_capital_requires_reservations(self, store):
        seed_clean(store)
        st = build(store)
        assert st.reserved_capital.status == ps.UNAVAILABLE
        r = cr.CapitalReservations(store, buffer_usd=0.0, max_available_age_s=60)
        now = time.time()
        assert r.reserve("carry", "i1", 100.0, "acct", "entry",
                         available=st.available_capital, ttl_s=60, now=now)
        st2 = build(store, reservations=r, account_scope="acct", now=now)
        assert st2.reserved_capital.value == 100.0
        assert st2.per_strategy_risk.value == {
            "carry": {"reserved_usd": 100.0, "committed_usd": 0.0}}

    def test_building_state_writes_nothing(self, store):
        seed_clean(store)
        before = store._query("SELECT COUNT(*) AS n FROM daily_anchor")[0]["n"]
        # a later UTC day with no anchor: must report UNAVAILABLE, not create one
        st = build(store, now=time.time() + 3 * 86400)
        after = store._query("SELECT COUNT(*) AS n FROM daily_anchor")[0]["n"]
        assert before == after
        assert st.daily_loss.status == ps.UNAVAILABLE

    def test_no_venue_read_blocks(self, store):
        seed_clean(store)
        st = ps.build_portfolio_state(store, limits=LIMITS, venue=None,
                                      now_epoch=time.time())
        assert st.available_capital.status == ps.UNAVAILABLE
        assert f"{ps.REQUIRED_UNAVAILABLE}:available_capital" in st.entry_blockers()
        assert st.reduction_allowed()

    def test_unreadable_venue_read_is_unavailable_with_reason(self, store):
        seed_clean(store)
        st = build(store, FakeBroker(available=RuntimeError("wallet down")))
        f = st.available_capital
        assert f.status == ps.UNAVAILABLE and f.value is None
        assert "wallet down" in f.reason
        assert_blocked_but_reducible(st, ps.REQUIRED_UNAVAILABLE)


class TestRefusalPaths:
    def test_kill_switch(self, store):
        seed_clean(store)
        store.trip_kill_switch("drill")
        assert_blocked_but_reducible(build(store), ps.KILL_SWITCH_ENGAGED)

    def test_unreadable_kill_switch(self, store):
        seed_clean(store)
        store._exec("DELETE FROM kill_switch")
        st = build(store)
        assert_blocked_but_reducible(st, ps.KILL_SWITCH_UNREADABLE)

    def test_unresolved_order(self, store):
        seed_clean(store)
        store.record_order_intent("L-1", "BTCUSDT", "Sell", 0.01, leg="linear",
                                  cycle_id="cyc-1")
        st = build(store, FakeBroker(orders=[{"orderLinkId": "L-1"}]))
        assert_blocked_but_reducible(st, ps.UNRESOLVED_ORDERS)
        assert st.unresolved_orders.value[0]["order_link_id"] == "L-1"

    def test_stale_state(self, store):
        seed_clean(store)
        st = build(store, now=time.time() + 120)
        assert_blocked_but_reducible(st, ps.STALE_STATE)
        assert f"{ps.STALE_STATE}:account_equity" in st.entry_blockers()

    def test_unknown_fee(self, store):
        j = held_journal(fee_status="unknown")
        seed_clean(store, j)
        store.save_carry_position({"book_state": "HEDGED", "position": {
            "perp": {"filled_qty": 0.01}, "spot": {"filled_qty": 0.01}}})
        st = build(store, FakeBroker(perp=0.01, spot=0.01))
        assert_blocked_but_reducible(st, ps.FEE_UNKNOWN)
        assert st.fees_paid.status == ps.UNAVAILABLE

    def test_unknown_fee_outside_the_window_does_not_block(self, store):
        j = held_journal(fee_status="unknown")
        seed_clean(store, j)
        store.save_carry_position({"book_state": "HEDGED", "position": {
            "perp": {"filled_qty": 0.01}, "spot": {"filled_qty": 0.01}}})
        st = build(store, FakeBroker(perp=0.01, spot=0.01),
                   fee_window_from_ms=10_000)
        assert ps.FEE_UNKNOWN not in st.entry_blockers()

    def test_contradiction_engine_ledger_venue(self, store):
        seed_clean(store, held_journal(0.01))
        store.save_carry_position({"book_state": "HEDGED", "position": {
            "perp": {"filled_qty": 0.01}, "spot": {"filled_qty": 0.01}}})
        st = build(store, FakeBroker(perp=0.02, spot=0.01))
        assert_blocked_but_reducible(st, ps.CONTRADICTION)
        codes = {c.code for c in st.contradictions}
        assert {"PERP_ENGINE_VS_VENUE", "PERP_LEDGER_VS_VENUE"} <= codes
        assert "PERP_ENGINE_VS_LEDGER" not in codes
        assert st.linear_positions.status == ps.CONTRADICTED
        assert st.linear_positions.value == 0.02  # the authority's number

    def test_agreeing_held_book_is_not_contradicted(self, store):
        seed_clean(store, held_journal(0.01))
        store.save_carry_position({"book_state": "HEDGED", "position": {
            "perp": {"filled_qty": 0.01}, "spot": {"filled_qty": 0.01}}})
        st = build(store, FakeBroker(perp=0.01 * (1 + 0.005), spot=0.01))
        assert st.contradictions == ()
        assert st.linear_positions.status == ps.CONFIRMED

    def test_contradiction_order_open_at_venue_unknown_to_log(self, store):
        seed_clean(store)
        st = build(store, FakeBroker(orders=[{"orderLinkId": "ghost"}]))
        assert_blocked_but_reducible(st, ps.CONTRADICTION)
        assert st.open_orders.status == ps.CONTRADICTED

    def test_contradiction_quote_exceeds_available(self, store):
        seed_clean(store)
        st = build(store, FakeBroker(available=100.0, quote=200.0))
        assert f"{ps.CONTRADICTION}:QUOTE_EXCEEDS_AVAILABLE" in st.entry_blockers()
        assert st.reduction_allowed()

    def test_limit_exceeded_daily_loss(self, store):
        seed_clean(store)
        store.update_equity(970.0)  # -3% on the day against a 2% limit
        st = build(store)
        assert_blocked_but_reducible(st, ps.LIMIT_EXCEEDED)
        assert f"{ps.LIMIT_EXCEEDED}:DAILY_LOSS" in st.entry_blockers()

    def test_limit_exceeded_total_capital(self, store):
        seed_clean(store)
        r = cr.CapitalReservations(store, buffer_usd=0.0, max_available_age_s=60)
        now = time.time()
        st0 = build(store, now=now)
        assert r.reserve("carry", "i1", 300.0, "acct", "entry",
                         available=st0.available_capital, ttl_s=60, now=now)
        lim = ps.PortfolioLimits(0.02, 0.10, 60.0, max_total_capital_usd=250.0)
        st = build(store, now=now, limits=lim, reservations=r, account_scope="acct")
        assert f"{ps.LIMIT_EXCEEDED}:TOTAL_CAPITAL" in st.entry_blockers()
        assert st.reduction_allowed()

    def test_halted_engine_blocks(self, store):
        seed_clean(store)
        store.save_carry_position({"book_state": "HALTED", "position": None})
        assert_blocked_but_reducible(build(store), ps.ENGINE_HALTED)

    def test_missing_ledger_blocks(self, store):
        store.update_equity(1000.0)
        st = build(store)
        assert f"{ps.REQUIRED_UNAVAILABLE}:ledger" in st.entry_blockers()
        assert st.realised_pnl.status == ps.UNAVAILABLE
        assert st.reduction_allowed()

    def test_as_dict_is_plain_data(self, store):
        import json
        seed_clean(store)
        json.dumps(build(store).as_dict())
