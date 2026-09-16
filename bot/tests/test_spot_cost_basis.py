"""A spot sale realised against ITSELF, so every spot gain booked as zero.

THE DEFECT
==========
`LedgerBroker._book` called `spot_sell(..., cost_basis=price)` — the SALE
price. `spot_sell` computes `cost = qty * cost_basis`, so `proceeds - cost` was
identically zero and **EQUITY:REALISED was booked as exactly $0.00 on every
spot sale the book ever made**, whatever the price moved.

Measured on a stub venue: buy 1 BTC at 100,000, sell at 110,000, and
`statement()["realised_usd"]` reads $0.00 instead of +$10,000.

WHY NOTHING CAUGHT IT
=====================
The money is not lost, which is exactly what made it invisible. The entry
still balances — `REALISED` gets `-(proceeds - cost)` and `BTC` gets `-cost`,
so setting `cost = proceeds` just moves the gain from EQUITY:REALISED into
ASSET:BTC. The BTC account then holds **value at zero quantity**:
-$10,010 against -0.0001 BTC in the reproduction.

And `reconcile()` compares QUANTITIES against the venue. The quantities were
right. It returned RECONCILED.

Three things had to line up: `spot_sell` itself is correct and is tested
directly with a distinct basis (`test_ledger.py`); `LedgerBroker` tracked a
weighted entry price for the PERP leg and nothing for the SPOT leg; and no
test drove a spot Sell through the wrapper. The primitive was right, the
wiring was wrong, and only the wiring was untested.

SCOPE
=====
OVERLAY never buys or sells the client's coin, so the live product is
unaffected. ACQUIRE buys the spot leg and sells it again, is a supported
`CARRY_EXECUTION_MODE`, and is what every `--mode acquire` backtest models.
"""
from __future__ import annotations

import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

import ledger as L  # noqa: E402


class Venue:
    """Fills everything at `px`, charging `fee` in the venue's own currency."""

    def __init__(self, px=100_000.0, fee=0.0):
        self.px = px
        self.fee = fee
        self.orders = []

    def place_market(self, *, symbol, side, qty, product):
        self.orders.append((side, product, qty, self.px))
        return {"filled_qty": qty, "avg_price": self.px,
                "order_link_id": f"{side}-{product}-{len(self.orders)}",
                "fee": self.fee}


def broker():
    venue = Venue()
    journal = L.Journal()
    clock = [0]

    def ms():
        clock[0] += 1000
        return clock[0]

    return venue, journal, L.LedgerBroker(venue, journal, ms=ms,
                                          spot_symbol="BTCUSDT")


def buy(b, v, qty, px, fee_btc=0.0):
    v.px, v.fee = px, fee_btc
    return b.place_market(symbol="BTCUSDT", side="Buy", qty=qty, product="spot")


def sell(b, v, qty, px, fee_usd=0.0):
    v.px, v.fee = px, fee_usd
    return b.place_market(symbol="BTCUSDT", side="Sell", qty=qty,
                          product="spot")


class TestASpotSaleRealisesAgainstWhatItCost:
    def test_a_gain_is_booked_as_a_gain(self):
        v, j, b = broker()
        buy(b, v, 1.0, 100_000.0)
        sell(b, v, 1.0, 110_000.0)
        assert j.statement()["realised_usd"] == pytest.approx(10_000.0)

    def test_a_loss_is_booked_as_a_loss(self):
        v, j, b = broker()
        buy(b, v, 1.0, 100_000.0)
        sell(b, v, 1.0, 90_000.0)
        assert j.statement()["realised_usd"] == pytest.approx(-10_000.0)

    def test_a_flat_round_trip_realises_nothing(self):
        v, j, b = broker()
        buy(b, v, 1.0, 100_000.0)
        sell(b, v, 1.0, 100_000.0)
        assert j.statement()["realised_usd"] == pytest.approx(0.0)

    def test_the_gain_no_longer_hides_in_the_btc_account(self):
        """The tell: an account holding VALUE at ZERO QUANTITY."""
        v, j, b = broker()
        buy(b, v, 1.0, 100_000.0)
        sell(b, v, 1.0, 110_000.0)
        btc = j.balances()[L.BTC]
        assert btc.qty == pytest.approx(0.0)
        assert btc.usd == pytest.approx(0.0), (
            "BTC holds value at zero quantity; the realised gain is misfiled")

    def test_the_basis_is_the_weighted_average_of_several_buys(self):
        v, j, b = broker()
        buy(b, v, 1.0, 100_000.0)
        buy(b, v, 1.0, 120_000.0)          # average 110,000
        sell(b, v, 2.0, 130_000.0)
        assert j.statement()["realised_usd"] == pytest.approx(40_000.0)

    def test_a_partial_sale_realises_only_what_it_sold(self):
        v, j, b = broker()
        buy(b, v, 2.0, 100_000.0)
        sell(b, v, 0.5, 120_000.0)
        assert j.statement()["realised_usd"] == pytest.approx(10_000.0)

    def test_the_remaining_lot_keeps_its_basis(self):
        v, j, b = broker()
        buy(b, v, 2.0, 100_000.0)
        sell(b, v, 1.0, 120_000.0)
        sell(b, v, 1.0, 90_000.0)
        assert j.statement()["realised_usd"] == pytest.approx(10_000.0)


class TestTheHonestFallback:
    def test_a_sale_with_no_recorded_purchase_realises_nothing(self):
        """An OVERLAY never buys the client's coin. A sale with no basis has
        nothing to realise against, and zero is the honest answer."""
        v, j, b = broker()
        sell(b, v, 1.0, 110_000.0)
        assert j.statement()["realised_usd"] == pytest.approx(0.0)

    def test_selling_more_than_was_bought_does_not_go_negative(self):
        v, j, b = broker()
        buy(b, v, 1.0, 100_000.0)
        sell(b, v, 1.0, 110_000.0)
        sell(b, v, 1.0, 110_000.0)         # nothing left; basis is gone
        assert b._spot_qty == pytest.approx(0.0)
        assert b._spot_entry is None


class TestTheRestOfTheLedgerIsUnchanged:
    def test_the_perp_leg_still_realises_correctly(self):
        v, j, b = broker()
        v.px, v.fee = 100_000.0, 0.0
        b.place_market(symbol="BTCUSDT", side="Sell", qty=1.0,
                       product="linear")
        v.px = 90_000.0
        b.place_market(symbol="BTCUSDT", side="Buy", qty=1.0, product="linear")
        assert j.statement()["realised_usd"] == pytest.approx(10_000.0)

    def test_fees_are_still_booked_where_the_venue_takes_them(self):
        v, j, b = broker()
        buy(b, v, 1.0, 100_000.0, fee_btc=0.0001)    # fee in BTC (F3)
        sell(b, v, 0.9999, 110_000.0, fee_usd=11.0)  # fee in USDT
        st = j.statement()
        assert st["fees_usd"] == pytest.approx(0.0001 * 100_000.0 + 11.0)

    def test_the_statement_identity_still_holds(self):
        v, j, b = broker()
        buy(b, v, 1.0, 100_000.0)
        sell(b, v, 1.0, 110_000.0)
        st = j.statement()
        assert st["net_usd"] == pytest.approx(
            st["funding_usd"] - st["fees_usd"] - st["borrow_usd"]
            + st["realised_usd"])

    def test_every_entry_still_balances_to_zero(self):
        v, j, b = broker()
        buy(b, v, 1.0, 100_000.0, fee_btc=0.0001)
        sell(b, v, 0.5, 120_000.0, fee_usd=6.0)
        for entry in j.entries:
            assert sum(line.usd for line in entry.lines) == \
                pytest.approx(0.0, abs=L.BALANCE_TOLERANCE)
