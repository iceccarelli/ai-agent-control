"""An order whose outcome is unknown is never treated as "did not fill".

Driven through the REAL CarryEngine -> LedgerBroker -> CarryBroker against the
scripted venue of test_carry_fee_truth. The failure injected is the one that
loses money: the request times out AFTER the venue executed it.

* the venue holds the order -> the leg is the venue's fill, no second order;
* the venue cannot be asked  -> the book HALTS and sends nothing else;
* the perp leg's fate is unknown while spot is held -> spot is NOT sold;
* a perp close that fails or fills short -> spot is sold only in proportion.
"""
from __future__ import annotations

import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from bybit_connection import PermanentAPIError, TransientAPIError  # noqa: E402
from carry_engine import BookState  # noqa: E402
from test_carry_fee_truth import (  # noqa: E402
    H8, MARK, build, kinds, modern, open_the_hedge)


def _lose_response(venue, *, category, side=None, readable=True,
                   execute=True):
    """Make the matching /order/create execute (or not) and then time out."""
    real = venue._request

    def request(method, endpoint, **kw):
        body = kw.get("body") or {}
        hit = (endpoint == "/v5/order/create"
               and body.get("category") == category
               and (side is None or body.get("side") == side))
        if hit and not request.fired:
            request.fired = True
            if execute:
                real(method, endpoint, **kw)         # the venue DID execute it
            raise TransientAPIError("transport error: timed out")
        if (not readable and request.fired
                and endpoint == "/v5/order/realtime"):
            raise TransientAPIError("transport error: timed out")
        return real(method, endpoint, **kw)

    request.fired = False
    venue._request = request


def _orders(venue, category=None, side=None):
    return [b for b in venue.created
            if (category is None or b["category"] == category)
            and (side is None or b["side"] == side)]


class TestATimeoutAfterSubmission:
    def test_an_order_the_venue_holds_is_adopted_not_resent(self):
        venue, journal, engine = build(modern)
        _lose_response(venue, category="linear", side="Sell")
        decision = open_the_hedge(engine)
        assert decision.action == "opened", decision
        assert len(_orders(venue, "linear", "Sell")) == 1, "perp sent twice"
        assert len(_orders(venue, "spot", "Buy")) == 1
        assert kinds(journal) == ["spot_buy", "perp_open"]
        assert engine.position.perp.filled_qty == pytest.approx(
            engine.position.spot_held_qty)

    def test_an_unreadable_venue_halts_instead_of_calling_it_a_failed_leg(self):
        venue, journal, engine = build(modern)
        _lose_response(venue, category="spot", side="Buy", readable=False)
        decision = open_the_hedge(engine)
        assert decision.state is BookState.HALTED
        assert decision.reason == "LEG_OUTCOME_UNKNOWN"
        assert len(venue.created) == 1, "something was sent on top of it"

    def test_perp_fate_unknown_with_spot_held_does_not_sell_the_spot(self):
        venue, journal, engine = build(modern)
        _lose_response(venue, category="linear", side="Sell", readable=False)
        decision = open_the_hedge(engine)
        assert decision.state is BookState.HALTED
        assert decision.reason == "PERP_LEG_OUTCOME_UNKNOWN"
        assert not _orders(venue, "spot", "Sell"), "stripped a possible hedge"
        # the exposure is written down for the next cold start
        assert engine.position is not None
        assert engine.position.spot.filled_qty > 0
        assert engine.position.perp.filled_qty == 0

    def test_a_venue_rejection_is_still_just_a_refused_leg(self):
        venue, journal, engine = build(modern)
        real = venue._request

        def reject(method, endpoint, **kw):
            if endpoint == "/v5/order/create" and \
                    (kw.get("body") or {}).get("category") == "spot":
                raise PermanentAPIError("insufficient balance", 170131)
            return real(method, endpoint, **kw)

        venue._request = reject
        decision = open_the_hedge(engine)
        assert decision.action == "refused"
        assert engine.state is BookState.FLAT


class TestUnwindNeverStripsTheHedgeFromAnOpenShort:
    def _held(self):
        venue, journal, engine = build(modern)
        assert open_the_hedge(engine).action == "opened"
        return venue, journal, engine

    def test_a_perp_close_that_fails_leaves_the_spot_in_place(self):
        venue, journal, engine = self._held()
        _lose_response(venue, category="linear", side="Buy", execute=False,
                       readable=True)
        d = engine.on_candle(mark=MARK, funding_bps=-9.0, spot=MARK,
                             funding_print_ms=4 * H8)
        # drive the exit directly: the unwind is what is under test
        d = engine._unwind(MARK, "TEST_EXIT")
        assert d.state is BookState.HALTED
        assert not _orders(venue, "spot", "Sell"), "sold the hedge of a live short"
        assert engine.position is not None

    def test_a_half_closed_perp_sells_only_half_the_spot(self):
        venue, journal, engine = self._held()
        # the wallet holds what was bought less the BTC fee taken from it
        gross = engine.position.spot.filled_qty
        spot_qty = gross - engine.position.spot.fee_base_qty
        real = venue._request

        def half(method, endpoint, **kw):
            body = kw.get("body")
            if endpoint == "/v5/order/create" and body and \
                    body["category"] == "linear" and body["side"] == "Buy":
                body = dict(body, qty=f"{float(body['qty']) / 2:.8f}")
                kw = dict(kw, body=body)
            return real(method, endpoint, **kw)

        venue._request = half
        d = engine._unwind(MARK, "TEST_EXIT")
        assert d.reason == "UNWIND_PARTIAL" and d.state is BookState.HALTED
        sold = sum(float(b["qty"]) for b in _orders(venue, "spot", "Sell"))
        assert sold == pytest.approx(spot_qty / 2, rel=1e-6)
        assert engine.position.perp.filled_qty == pytest.approx(spot_qty / 2)
        assert engine.position.spot.filled_qty == pytest.approx(spot_qty / 2)
