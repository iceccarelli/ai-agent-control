"""A restart must not erase the entry price a close realises against.

`LedgerBroker` kept the weighted entry of the short (and, in acquire mode, of
the bought spot) in process memory only. After a restart the engine restored a
HEDGED pair from its ledger, but the new broker had no entry, so closing the
leg fell back to ``entry = exit price``: the realised P&L of everything that
happened between open and close was booked as exactly zero. Tests drive the
real engine + real LedgerBroker + a journal reloaded the way ``build_bot``
loads it (``Journal.from_rows``), with a venue that fills at a settable price.
"""
from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import ledger as L  # noqa: E402
import venue_fees  # noqa: E402
from carry_engine import (ACQUIRE, OVERLAY, BookState, ColdStart,
                          CarryPosition, Leg)  # noqa: E402
from test_carry_cold_start import H8, MARK, Venue, engine  # noqa: E402

QTY = 0.001


class PricedVenue(Venue):
    px = MARK

    def place_market(self, *, symbol, side, qty, product):
        out = super().place_market(symbol=symbol, side=side, qty=qty,
                                   product=product)
        out["avg_price"] = self.px
        return out


def wrapped(venue, journal, mode):
    broker = L.LedgerBroker(venue, journal, ms=lambda: 1_000,
                            spot_symbol="BTCUSDT")
    e = engine(broker, mode=mode)
    return e


def open_then_restart(mode):
    v1 = PricedVenue(inventory=5.0)
    j1 = L.Journal()
    e1 = wrapped(v1, j1, mode)
    d = e1.on_candle(mark=MARK, funding_bps=3.0, spot=MARK,
                     funding_print_ms=3 * H8, timestamp_ms=3 * H8)
    assert d.action == "opened", d.reason
    state = json.loads(json.dumps(e1.to_state()))
    rows = json.loads(json.dumps(j1.to_rows()))
    # --- the process dies here; a new one loads the journal and the ledger ---
    v2 = PricedVenue(inventory=5.0, perp=QTY)
    j2 = L.Journal.from_rows(rows)
    e2 = wrapped(v2, j2, mode)
    cold = e2.cold_start(ledger=state, venue_perp_qty=QTY,
                         venue_spot_qty=5.0 if mode == OVERLAY else QTY,
                         open_orders=())
    assert cold.action is ColdStart.RESUME, cold.reason
    return v2, j2, e2


def unwind(venue, e, px):
    venue.px = px
    for k in (4, 5, 6):
        e.on_candle(mark=px, funding_bps=-0.5, spot=px,
                    funding_print_ms=k * H8, timestamp_ms=k * H8)
    assert e.state is BookState.FLAT


def test_a_short_opened_before_a_restart_realises_its_move_after_it():
    v, j, e = open_then_restart(OVERLAY)
    unwind(v, e, 90_000.0)
    # short 0.001 BTC from 100,000 closed at 90,000 = +$10
    assert j.statement()["realised_usd"] == pytest.approx(10.0)


def test_an_acquired_spot_leg_keeps_its_cost_across_a_restart_too():
    v, j, e = open_then_restart(ACQUIRE)
    unwind(v, e, 90_000.0)
    # perp +$10, spot bought 100,000 sold 90,000 -$10
    assert j.statement()["realised_usd"] == pytest.approx(0.0)
    btc = j.balances()[L.BTC]
    assert btc.usd == pytest.approx(0.0, abs=1e-6), "value left at zero qty"


def test_without_a_restart_nothing_changed():
    v = PricedVenue(inventory=5.0)
    j = L.Journal()
    e = wrapped(v, j, OVERLAY)
    e.on_candle(mark=MARK, funding_bps=3.0, spot=MARK,
                funding_print_ms=3 * H8, timestamp_ms=3 * H8)
    unwind(v, e, 90_000.0)
    assert j.statement()["realised_usd"] == pytest.approx(10.0)



class BaseFeeVenue(PricedVenue):
    """A venue that deducts the spot BUY fee from BTC received."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.base_fee_rate = 0.001

    def place_market(self, *, symbol, side, qty, product):
        out = super().place_market(symbol=symbol, side=side, qty=qty,
                                   product=product)
        if product == "spot" and side == "Buy":
            fee = qty * self.base_fee_rate
            fees = venue_fees.extract_order_fees(
                {"cumFeeDetail": {"BTC": str(fee)}})
            out.update(fee=fees.single_amount, fee_detail=fees.to_dict())
        return out


def test_base_coin_fee_does_not_leave_ghost_spot_basis_between_round_trips():
    venue = BaseFeeVenue(inventory=5.0)
    journal = L.Journal()
    broker = L.LedgerBroker(venue, journal, ms=lambda: MS,
                            spot_symbol="BTCUSDT")

    first = broker.place_market(symbol="BTCUSDT", side="Buy", qty=1.0,
                                product="spot")
    first_held = first["filled_qty"] - 0.001
    assert broker._spot_qty == pytest.approx(first_held)
    broker.place_market(symbol="BTCUSDT", side="Sell", qty=first_held,
                        product="spot")
    assert broker._spot_qty == pytest.approx(0.0)
    assert broker._spot_entry is None
    assert journal.balances()[L.BTC].qty == pytest.approx(0.0, abs=1e-12)

    # A second round trip in the same process must not inherit the first
    # round's withheld BTC as synthetic inventory or dilute its entry price.
    venue.px = 120_000.0
    second = broker.place_market(symbol="BTCUSDT", side="Buy", qty=0.5,
                                 product="spot")
    second_held = second["filled_qty"] - 0.0005
    assert broker._spot_qty == pytest.approx(second_held)
    assert broker._spot_entry == pytest.approx(120_000.0)
    broker.place_market(symbol="BTCUSDT", side="Sell", qty=second_held,
                        product="spot")
    assert broker._spot_qty == pytest.approx(0.0)
    assert broker._spot_entry is None
    assert journal.balances()[L.BTC].qty == pytest.approx(0.0, abs=1e-12)


def test_restart_reseeds_acquired_spot_basis_with_net_wallet_quantity():
    venue = BaseFeeVenue(inventory=5.0)
    e = wrapped(venue, L.Journal(), ACQUIRE)
    e.position = CarryPosition(
        spot=Leg(symbol="BTCUSDT", side="Buy", product="spot",
                requested_qty=1.0, filled_qty=1.0, avg_price=MARK,
                fee_base_qty=0.001),
        perp=Leg(symbol="BTCUSDT", side="Sell", product="linear",
                 requested_qty=1.0, filled_qty=1.0, avg_price=MARK),
        opened_ms=MS)
    e._reseed_cost_basis()
    assert e.broker._spot_qty == pytest.approx(0.999)
    assert e.broker._spot_entry == pytest.approx(MARK)
