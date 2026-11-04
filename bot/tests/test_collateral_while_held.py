"""Collateral eligibility while a position is HELD.

PR #194 asks before an overlay opens. This is the other half: what the book does
when the venue stops saying the held BTC backs the short, or stops saying
anything. Fake venues only; no network, no account.

The response is deliberately modest. A STATED loss of cover exits through the
ordinary single reduce-only unwind (the same exit every other margin trigger
uses). An UNSTATED state is observed and, if it persists, halts with no order.
Nothing here retries, resizes or repeats a close."""
from __future__ import annotations

import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
BOT = os.path.dirname(HERE)
for p in (BOT, HERE):
    if p not in sys.path:
        sys.path.insert(0, p)

import margin_facts as MF                                       # noqa: E402
from carry_broker import CarryBroker                            # noqa: E402
from carry_engine import (COLLATERAL_LOST_CONFIRM_TICKS,  # noqa: E402
                          COLLATERAL_UNKNOWN_HALT_TICKS, BookState, CarryEngine)
from carry_risk import CarryRisk                                # noqa: E402
from market_snapshot import MarketSnapshot                      # noqa: E402
from test_carry_broker import FakeClient, envelope              # noqa: E402
from test_collateral_facts_refusal import REAL_ROW, wallet      # noqa: E402
from test_liquidation_distance import H8, MARK, Venue          # noqa: E402

import time                                                     # noqa: E402

OK = {"status": "OK", "reason": ""}
OFF = {"status": "NOT_COLLATERAL", "reason": "COLLATERAL_SWITCH_OFF"}
PLATFORM = {"status": "NOT_COLLATERAL", "reason": "PLATFORM_DISALLOWS_COLLATERAL"}
UNKNOWN = {"status": "INSUFFICIENT_RISK_DATA", "reason": "COLLATERAL_FIELD_ABSENT"}


class V(Venue):
    """The shared liquidation-test venue, plus a collateral state per tick."""

    def __init__(self, **kw):
        super().__init__(liq=None, **kw)
        self.collateral = OK
        self.close_fills = None            # None: full fill; else the filled share

    def get_collateral_facts(self, symbol):
        return self.collateral

    def get_liquidation_view(self, symbol):
        v = super().get_liquidation_view(symbol)
        v["collateral"] = self.collateral
        return v

    def get_perp_position(self, symbol):
        return sum(q if s == "Sell" else -q for s, p, q in self.orders if p == "linear")

    def place_market(self, *, symbol, side, qty, product):
        if side == "Buy" and product == "linear" and self.close_fills is not None:
            qty = qty * self.close_fills
        return super().place_market(symbol=symbol, side=side, qty=qty, product=product)


def held(venue, **kw):
    e = CarryEngine(broker=venue, max_notional_usd=100.0, borrow_apr=0.0,
                    execution_mode=kw.pop("mode", "overlay"),
                    persist=lambda s: None, require_liquidation_check=True,
                    require_collateral_facts=kw.pop("flag", True), **kw)
    e.pair_risk = CarryRisk(max_notional_usd=100.0)
    e.snapshot = MarketSnapshot(perp_mark=MARK, spot_mark=MARK, funding_bps=3.0,
                                margin_multiple=5.0, observed_at_s=time.time())
    for k in (1, 2):
        e.on_candle(mark=MARK, funding_bps=3.0, spot=MARK,
                    funding_print_ms=k * H8, timestamp_ms=k * H8)
    d = e.on_candle(mark=MARK, funding_bps=3.0, spot=MARK,
                    funding_print_ms=3 * H8, timestamp_ms=3 * H8)
    assert d.action == "opened", d.reason
    return e


def tick(e, k):
    return e.on_candle(mark=MARK, funding_bps=3.0, spot=MARK,
                       funding_print_ms=(3 + k) * H8, timestamp_ms=(3 + k) * H8)


def perp_orders(v):
    return [(s, q) for s, p, q in v.orders if p == "linear"]


# -- the broker: the state comes from the response it already reads ---------------------

def broker_view(row, margin_mode="REGULAR_MARGIN"):
    client = FakeClient(mark=MARK, margin_mode=margin_mode)
    real = client._request
    calls = []

    def request(method, endpoint, **kw):
        if endpoint == "/v5/position/list":
            return envelope({"list": [{"side": "Sell", "size": "0.001",
                                       "positionIdx": 0, "markPrice": "100000",
                                       "positionIM": "20", "positionMM": "5",
                                       "liqPrice": "130000"}]})
        if endpoint == "/v5/account/wallet-balance":
            calls.append(kw.get("params"))
            return envelope({"list": [{"accountMMRate": "0.05",
                                       "coin": [dict(row)] if row else []}]})
        return real(method, endpoint, **kw)
    client._request = request
    b = CarryBroker(client=client, order_gate=lambda: (True, "T"),
                    sequence_source=lambda *a: 1)
    return b.get_liquidation_view("BTCUSDT"), calls


def test_the_view_carries_the_collateral_state_from_the_same_wallet_request():
    v, calls = broker_view(REAL_ROW)
    assert v["collateral"]["status"] == MF.OK
    assert len(calls) == 1                                      # no extra request
    assert calls == [{"accountType": "UNIFIED"}]


@pytest.mark.parametrize("row,reason", [
    (dict(REAL_ROW, collateralSwitch=False), MF.COLLATERAL_SWITCH_OFF),
    (dict(REAL_ROW, marginCollateral=False), MF.PLATFORM_DISALLOWS_COLLATERAL),
])
def test_a_switch_turned_off_or_a_platform_removal_shows_in_the_view(row, reason):
    v, _ = broker_view(row)
    assert (v["collateral"]["status"], v["collateral"]["reason"]) == (MF.NOT_COLLATERAL, reason)


def test_a_missing_or_malformed_row_is_insufficient_risk_data_in_the_view():
    assert broker_view(None)[0]["collateral"]["reason"] == MF.COIN_ROW_MISSING
    bad = dict(REAL_ROW, collateralSwitch="maybe")
    assert broker_view(bad)[0]["collateral"]["status"] == MF.INSUFFICIENT_RISK_DATA


def test_isolated_margin_does_not_read_the_wallet_and_states_no_collateral_block():
    v, calls = broker_view(REAL_ROW, margin_mode="ISOLATED_MARGIN")
    assert v["collateral"] is None and calls == []


# -- the engine: a STATED loss of cover --------------------------------------------------

@pytest.mark.parametrize("facts", [OFF, PLATFORM])
def test_a_stated_loss_of_cover_exits_once_through_the_ordinary_unwind(facts):
    v = V()
    e = held(v)
    assert tick(e, 1).action == "hold" and len(perp_orders(v)) == 1     # healthy
    v.collateral = facts
    assert tick(e, 2).action == "hold"                       # one read is not enough to close
    d = tick(e, 3)
    assert d.action == "unwound", (d.action, d.reason)
    assert d.reason.startswith("COLLATERAL_LOST_")
    assert [s for s, _ in perp_orders(v)] == ["Sell", "Buy"]            # one close
    qty_open, qty_close = perp_orders(v)[0][1], perp_orders(v)[1][1]
    assert qty_close == pytest.approx(qty_open)                          # exactly the position
    assert v.get_perp_position("BTCUSDT") == pytest.approx(0.0)
    for k in (4, 5, 6):                                      # and nothing more is ever sent
        tick(e, k)
    assert len(perp_orders(v)) == 2


def test_a_single_misread_flag_never_closes_the_position():
    v = V()
    e = held(v)
    tick(e, 1)
    for k in range(2, 12):
        v.collateral = OFF if k % 2 else OK                  # never two in a row
        assert tick(e, k).action == "hold"
    assert len(perp_orders(v)) == 1 and e.state != BookState.HALTED
    assert COLLATERAL_LOST_CONFIRM_TICKS == 2


def test_the_close_is_a_protective_close_in_the_cost_evidence():
    import execution_record as xr
    assert xr.route_for_exit("COLLATERAL_LOST_COLLATERAL_SWITCH_OFF") == xr.ROUTE_PROTECTIVE_CLOSE


def test_a_partial_fill_on_a_collateral_close_halts_and_never_sends_a_second_order():
    v = V()
    v.close_fills = 0.5
    e = held(v)
    tick(e, 1)
    v.collateral = OFF
    tick(e, 2)
    d = tick(e, 3)
    assert e.state == BookState.HALTED, (d.action, d.reason)
    sent = len(perp_orders(v))
    for k in (4, 5, 6):
        tick(e, k)
    assert len(perp_orders(v)) == sent == 2                  # open + ONE close attempt
    assert 0 < v.get_perp_position("BTCUSDT") < 0.001 + 1e-12   # the remainder is known, never reversed


# -- the engine: an UNSTATED state --------------------------------------------------------

def test_one_or_two_unstated_ticks_are_observed_and_do_not_act():
    v = V()
    seen = []
    e = held(v, risk_observer=seen.append)
    tick(e, 1)
    v.collateral = UNKNOWN
    for k in range(2, 2 + COLLATERAL_UNKNOWN_HALT_TICKS - 1):
        assert tick(e, k).action == "hold"
    assert len(perp_orders(v)) == 1 and e.state != BookState.HALTED
    assert any(str(o.get("reason", "")).startswith("COLLATERAL_UNKNOWN:") for o in seen)


def test_a_persistent_unstated_state_halts_with_no_order():
    v = V()
    e = held(v)
    tick(e, 1)
    v.collateral = UNKNOWN
    for k in range(2, 2 + COLLATERAL_UNKNOWN_HALT_TICKS + 2):
        tick(e, k)
    assert e.state == BookState.HALTED
    assert len(perp_orders(v)) == 1                          # halting sends nothing
    assert COLLATERAL_UNKNOWN_HALT_TICKS == 3


def test_a_good_read_between_unknowns_resets_the_count():
    v = V()
    e = held(v)
    tick(e, 1)
    for k in range(2, 12):
        v.collateral = UNKNOWN if k % 3 else OK              # never 3 in a row
        tick(e, k)
    assert e.state != BookState.HALTED and len(perp_orders(v)) == 1


def test_a_view_with_no_collateral_block_counts_as_unstated_not_as_ok():
    v = V()
    e = held(v)
    tick(e, 1)
    real = v.get_liquidation_view
    v.get_liquidation_view = lambda s: {k: x for k, x in real(s).items() if k != "collateral"}
    for k in range(2, 6):
        tick(e, k)
    assert e.state == BookState.HALTED and len(perp_orders(v)) == 1


# -- nothing else changes -----------------------------------------------------------------

def test_with_the_flag_off_a_lost_collateral_state_is_ignored_as_before():
    v = V()
    e = held(v, flag=False)
    v.collateral = OFF
    for k in range(1, 6):
        assert tick(e, k).action == "hold"
    assert len(perp_orders(v)) == 1


def test_the_existing_margin_gates_still_run_first_and_unwind_on_their_own():
    v = V(mm_rate=0.60)
    e = held(v)
    d = tick(e, 1)
    assert d.action == "unwound" and d.reason.startswith("ACCOUNT_MM_RATE_")


def test_an_unreadable_view_still_halts_exactly_as_before():
    v = V()
    e = held(v)
    v.raises = True
    assert tick(e, 1).reason == "LIQUIDATION_PRICE_UNREADABLE"
    assert e.state == BookState.HALTED and len(perp_orders(v)) == 1


# -- review round: ordering, per-position counters, one observation row, isolated ----------------------

def test_the_liquidation_floor_wins_over_an_unstated_collateral_halt():
    """P1-1: two unstated ticks, then price moves to 2% from liquidation on the
    third. The distance exit must act, not the collateral halt."""
    v = V()
    e = held(v, )
    tick(e, 1)
    v.collateral = UNKNOWN
    tick(e, 2)
    tick(e, 3)
    v.liq = MARK * 1.02
    d = tick(e, 4)                                           # the third unstated tick
    assert d.action == "unwound" and d.reason.startswith("LIQUIDATION_"), (d.action, d.reason)
    assert e.state != BookState.HALTED


def test_the_account_margin_rate_exit_wins_over_the_collateral_halt():
    v = V()
    e = held(v)
    tick(e, 1)
    v.collateral = UNKNOWN
    tick(e, 2)
    tick(e, 3)
    v.mm_rate = 0.60
    d = tick(e, 4)
    assert d.action == "unwound" and d.reason.startswith("ACCOUNT_MM_RATE_")


def test_a_stated_loss_does_not_skip_the_liquidation_floor_either():
    v = V()
    e = held(v)
    tick(e, 1)
    v.collateral = OFF
    tick(e, 2)
    v.liq = MARK * 1.02
    d = tick(e, 3)                                           # confirm tick AND a close liquidation
    assert d.action == "unwound" and d.reason.startswith("LIQUIDATION_")


def test_the_counters_belong_to_one_position():
    """P1-2: two unstated ticks on one position must not carry into the next."""
    v = V()
    e = held(v)
    tick(e, 1)
    v.collateral = UNKNOWN
    tick(e, 2)
    tick(e, 3)
    assert e._collateral_unknown_ticks == COLLATERAL_UNKNOWN_HALT_TICKS - 1
    e.position.opened_ms += 1                                # a different position
    tick(e, 4)
    assert e._collateral_unknown_ticks == 1
    assert e.state != BookState.HALTED


def test_a_flat_book_clears_the_counters():
    v = V()
    e = held(v)
    v.collateral = UNKNOWN
    tick(e, 1)
    tick(e, 2)
    assert e._collateral_unknown_ticks == 2
    e.position = None
    assert e._collateral_action({"margin_mode": "REGULAR_MARGIN", "collateral": UNKNOWN}) is None
    assert e._collateral_unknown_ticks == e._collateral_lost_ticks == 0


def test_one_observation_row_per_tick_carries_the_collateral_note():
    """P1-3: the observation table keeps ONE row per millisecond, so the note
    must ride the single row the tick already writes."""
    seen = []
    v = V()
    e = held(v, risk_observer=seen.append)
    seen.clear()
    tick(e, 1)
    assert len(seen) == 1 and not str(seen[0]["reason"]).startswith("COLLATERAL")
    seen.clear()
    v.collateral = UNKNOWN
    tick(e, 2)
    assert len(seen) == 1 and seen[0]["reason"].startswith("COLLATERAL_UNKNOWN:")
    seen.clear()
    v.collateral = OFF
    tick(e, 3)
    assert len(seen) == 1 and seen[0]["reason"].startswith("COLLATERAL_LOST:")


def test_isolated_margin_has_no_cross_collateral_to_lose_and_is_left_alone():
    v = V(margin_mode="ISOLATED_MARGIN")
    v.liq = MARK * 1.6
    e = held(v)
    v.collateral = UNKNOWN
    for k in range(1, 8):
        assert tick(e, k).action == "hold"
    assert e.state != BookState.HALTED and len(perp_orders(v)) == 1


def test_the_flag_without_the_liquidation_check_warns_instead_of_failing_silently(caplog):
    import logging
    with caplog.at_level(logging.WARNING):
        CarryEngine(broker=V(), max_notional_usd=100.0, borrow_apr=0.0,
                    execution_mode="overlay", persist=lambda s: None,
                    require_collateral_facts=True, require_liquidation_check=False)
    assert any("HOLD-time collateral check" in r.message for r in caplog.records)
