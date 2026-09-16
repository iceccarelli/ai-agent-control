"""A cancel can catch a fill on the way out. The journal never saw it.

THE DEFECT
==========
`LedgerBroker.cancel_order` called `_book(result, side="",
allow_missing_side=True)`, and `_book` reaches `if not side: return` — so it
returned before booking anything. Its own comment said the opposite: "A cancel
can report a fill it caught on the way out (0040). It is booked like any other
fill."

`CarryEngine._rest` DOES consume that number — `leg.filled_qty = total` when
the cancel reports more than the placement did — so the POSITION recorded the
fill and the JOURNAL did not. Measured: a cancel reporting `filled_qty=0.4,
fee=$8.00` produced 0 journal entries, 0 perp quantity and $0.00 of fees, and
`reconcile()` against the venue's 0.4 returned INCIDENT on a book whose engine
was correct.

WHY THE OBVIOUS FIX IS THE WRONG ONE
====================================
The venue reports a cancel's fill as the order's CUMULATIVE total —
`carry_broker._order_state` reads `cumExecQty` — and `place_post_only` has
usually booked part of it already. Booking the cancel's number outright turns
a missing-fill defect into an over-booking one: post-only books 0.3, the cancel
says 0.4, and 0.3 + 0.4 = 0.7 against a venue holding 0.4.

So the increment is booked, which is exactly what `_rest` does with the same
two numbers. The side comes from what was remembered at placement, because a
cancel response does not carry one — which is why the original passed
`side=""` and why it booked nothing.

SCOPE
=====
Only `CARRY_EXECUTION_STYLE=maker_first` rests an order, and the default is
`taker`, so this is a supported-but-not-default path — the same shape as every
other defect this ledger has had: an unexercised mode.
"""
from __future__ import annotations

import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

import ledger as L  # noqa: E402

PX = 100_000.0


class Venue:
    """A resting order that fills `placed`, then reports `final` on cancel."""

    def __init__(self, placed=0.3, placed_fee=6.0, final=0.4, final_fee=8.0,
                 link="m1"):
        self.placed, self.placed_fee = placed, placed_fee
        self.final, self.final_fee = final, final_fee
        self.link = link

    def place_post_only(self, *, symbol, side, qty, price, product):
        return {"filled_qty": self.placed, "avg_price": PX,
                "order_link_id": self.link, "fee": self.placed_fee,
                "maker": True, "resting": True}

    def cancel_order(self, *, symbol, order_link_id, product):
        return {"filled_qty": self.final, "avg_price": PX,
                "order_link_id": order_link_id, "fee": self.final_fee}

    def place_market(self, *, symbol, side, qty, product):
        return {"filled_qty": qty, "avg_price": PX,
                "order_link_id": "t1", "fee": 5.0}


def broker(venue):
    journal = L.Journal()
    clock = [0]

    def ms():
        clock[0] += 1000
        return clock[0]

    return journal, L.LedgerBroker(venue, journal, ms=ms,
                                   spot_symbol="BTCUSDT")


def rest_then_cancel(b, side="Sell", product="linear", qty=1.0):
    b.place_post_only(symbol="BTCUSDT", side=side, qty=qty, price=PX,
                      product=product)
    return b.cancel_order(symbol="BTCUSDT", order_link_id="m1",
                          product=product)


class TestTheIncrementReachesTheJournal:
    def test_the_fill_the_cancel_caught_is_booked(self):
        j, b = broker(Venue())
        rest_then_cancel(b)
        assert abs(j.statement()["open_perp_qty"]) == pytest.approx(0.4)

    def test_its_fee_is_booked_too(self):
        j, b = broker(Venue())
        rest_then_cancel(b)
        assert j.statement()["fees_usd"] == pytest.approx(8.0)

    def test_the_book_then_reconciles_against_the_venue(self):
        j, b = broker(Venue())
        rest_then_cancel(b)
        st = j.statement()
        r = L.reconcile(j, venue_btc=0.0, venue_usdt=st["usdt_qty"],
                        venue_perp_qty=0.4)
        assert r["verdict"] == "RECONCILED", r["detail"]

    def test_a_cancel_after_a_fully_resting_order_books_the_whole_fill(self):
        """Nothing filled at placement; the cancel caught all of it."""
        j, b = broker(Venue(placed=0.0, placed_fee=0.0, final=0.4,
                            final_fee=8.0))
        rest_then_cancel(b)
        assert abs(j.statement()["open_perp_qty"]) == pytest.approx(0.4)


class TestItDoesNotOverBook:
    def test_a_cumulative_total_is_not_added_to_what_was_booked(self):
        """0.3 booked + a cancel reporting 0.4 is 0.4, never 0.7."""
        j, b = broker(Venue())
        rest_then_cancel(b)
        assert abs(j.statement()["open_perp_qty"]) == pytest.approx(0.4)
        assert len(j.entries) == 2

    def test_a_cancel_that_caught_nothing_new_books_nothing(self):
        j, b = broker(Venue(placed=0.4, placed_fee=8.0, final=0.4,
                            final_fee=8.0))
        rest_then_cancel(b)
        assert len(j.entries) == 1
        assert abs(j.statement()["open_perp_qty"]) == pytest.approx(0.4)

    def test_a_cancel_reporting_less_than_was_booked_books_nothing(self):
        """A venue that under-reports must not produce a negative fill."""
        j, b = broker(Venue(placed=0.4, placed_fee=8.0, final=0.1,
                            final_fee=2.0))
        rest_then_cancel(b)
        assert len(j.entries) == 1
        assert abs(j.statement()["open_perp_qty"]) == pytest.approx(0.4)

    def test_a_cancel_for_an_order_it_never_placed_books_nothing(self):
        """No baseline means no honest increment. Refusing beats guessing."""
        j, b = broker(Venue())
        b.cancel_order(symbol="BTCUSDT", order_link_id="never-seen",
                       product="linear")
        assert len(j.entries) == 0

    def test_the_same_order_cannot_be_cancelled_twice_into_the_book(self):
        j, b = broker(Venue())
        rest_then_cancel(b)
        before = len(j.entries)
        b.cancel_order(symbol="BTCUSDT", order_link_id="m1", product="linear")
        assert len(j.entries) == before


class TestTheSideComesFromThePlacement:
    def test_a_rested_sell_increments_the_short(self):
        j, b = broker(Venue())
        rest_then_cancel(b, side="Sell")
        assert j.statement()["open_perp_qty"] < 0

    def test_a_rested_buy_reduces_it(self):
        j, b = broker(Venue(placed=0.0, placed_fee=0.0))
        L.perp_open(j, ms=1, ref="seed", qty=1.0, price=PX, fee_usd=0.0)
        before = j.statement()["open_perp_qty"]
        rest_then_cancel(b, side="Buy")
        assert j.statement()["open_perp_qty"] > before

    def test_a_rested_spot_buy_books_against_the_coin(self):
        j, b = broker(Venue(placed=0.0, placed_fee=0.0, final=0.4,
                            final_fee=0.00004))
        rest_then_cancel(b, side="Buy", product="spot")
        assert j.statement()["btc_qty"] > 0


class TestUnknownStaysUnknown:
    def test_an_unstated_cumulative_fee_is_counted_not_computed(self):
        j, b = broker(Venue(final_fee=None))
        rest_then_cancel(b)
        assert j.unknown_fees == 1

    def test_an_unstated_placement_fee_leaves_no_honest_difference(self):
        j, b = broker(Venue(placed_fee=None))
        rest_then_cancel(b)
        assert j.unknown_fees >= 1


class TestTheTakerPathIsUnchanged:
    def test_a_market_order_still_books_exactly_once(self):
        j, b = broker(Venue())
        b.place_market(symbol="BTCUSDT", side="Sell", qty=1.0,
                       product="linear")
        assert len(j.entries) == 1
        assert abs(j.statement()["open_perp_qty"]) == pytest.approx(1.0)

    def test_every_entry_still_balances(self):
        j, b = broker(Venue())
        rest_then_cancel(b)
        for entry in j.entries:
            assert sum(line.usd for line in entry.lines) == \
                pytest.approx(0.0, abs=L.BALANCE_TOLERANCE)
