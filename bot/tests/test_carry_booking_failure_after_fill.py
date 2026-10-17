"""An order the venue FILLED must never be reported to the engine as "did not
fill" because booking it in the journal raised.

`LedgerBroker.place_market` sends the order and then books the fill. When the
booking raised (an inconsistent fee, a journal error), the exception reached
`CarryEngine._take` as an ordinary failure: "the leg failed, nothing happened".
The spot leg was then "refused" (FLAT, allowance untouched, the next candle
buys again), or a perp short that had filled was treated as absent and the spot
was sold: a naked short.

Offline: the scripted `FeeVenue`; no network, no credential.
"""
from __future__ import annotations

import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import ledger as L  # noqa: E402
from carry_engine import BookState  # noqa: E402
from test_carry_fee_truth import (  # noqa: E402
    H8, MARK, build, modern, open_the_hedge)


def spot_fee_above_the_fill(product, side, filled):
    if product == "spot" and side == "Buy":
        return {"cumExecFee": "0", "cumFeeDetail": {"BTC": f"{filled * 3:.8f}"}}
    return modern(product, side, filled)


def sent(venue):
    return [(b["category"], b["side"]) for b in venue.created]


class TestASpotFillThatCannotBeBookedIsNotAFailedLeg:
    def test_the_book_halts_and_buys_nothing_more(self):
        venue, _j, engine = build(spot_fee_above_the_fill)
        d = open_the_hedge(engine)
        assert sent(venue) == [("spot", "Buy")]       # the venue DID fill it
        assert d.state is BookState.HALTED, (d.action, d.reason)
        assert d.reason == "LEG_OUTCOME_UNKNOWN"
        assert engine.state is BookState.HALTED

    def test_later_ticks_cannot_buy_again(self):
        venue, _j, engine = build(spot_fee_above_the_fill)
        open_the_hedge(engine)
        for k in (4, 5, 6):
            engine.on_candle(mark=MARK, funding_bps=3.0, spot=MARK,
                             funding_print_ms=k * H8)
        assert sent(venue) == [("spot", "Buy")], "bought again after a halt"


class TestAPerpFillThatCannotBeBookedDoesNotStripTheHedge:
    def test_the_spot_is_not_sold_against_a_live_short(self, monkeypatch):
        venue, _j, engine = build(modern)

        def broken(*a, **kw):
            raise L.LedgerError("journal rejected the entry")

        monkeypatch.setattr(L, "perp_open", broken)
        d = open_the_hedge(engine)
        assert sent(venue) == [("spot", "Buy"), ("linear", "Sell")], \
            "an order was sent on top of a perp short the venue filled"
        assert d.state is BookState.HALTED
        assert engine.state is BookState.HALTED
        # the spot that IS held is written down for the next cold start
        assert engine.position is not None
        assert engine.position.spot.filled_qty > 0
