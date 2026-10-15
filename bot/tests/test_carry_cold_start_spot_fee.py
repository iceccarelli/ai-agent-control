"""Acquire mode: a spot BUY's fee is taken from the coin received.

Bybit charges a spot Buy in the BASE coin, so the wallet holds
``cumExecQty - fee`` while the ledger's spot leg recorded ``cumExecQty``. Cold
start compared the two exactly and HALTED (and tripped the kill switch) on a
book that was correctly hedged. The shortfall is explained only by a fee the
venue stated in the base coin on that very leg; any other gap still halts.
"""
from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import venue_fees  # noqa: E402
from carry_engine import ACQUIRE, OVERLAY, ColdStart  # noqa: E402
from test_carry_cold_start import MARK, Venue, engine, opened  # noqa: E402

FEE_BTC = 0.000001          # 10 bps of the 0.001 BTC clip


class FeeVenue(Venue):
    """place_market answers like CarryBroker: fee_detail from a Bybit row."""

    def __init__(self, *, spot_row, **kw):
        super().__init__(**kw)
        self.spot_row = spot_row

    def place_market(self, *, symbol, side, qty, product):
        out = super().place_market(symbol=symbol, side=side, qty=qty,
                                   product=product)
        if product == "spot":
            fees = venue_fees.extract_order_fees(self.spot_row)
            out.update(fee=fees.single_amount, fee_detail=fees.to_dict())
        return out


def acquired(row):
    return opened(FeeVenue(spot_row=row), mode=ACQUIRE)


def cold(state, venue_spot, mode=ACQUIRE):
    fresh = engine(Venue(perp=0.001), mode=mode)
    return fresh.cold_start(ledger=json.loads(json.dumps(state)),
                            venue_perp_qty=0.001, venue_spot_qty=venue_spot,
                            open_orders=())


def test_a_base_coin_fee_does_not_halt_a_hedged_acquire_book():
    state = acquired({"cumFeeDetail": {"BTC": str(FEE_BTC)}}).to_state()
    assert state["position"]["spot"]["filled_qty"] == pytest.approx(0.001)
    d = cold(state, 0.001 - FEE_BTC)
    assert d.action is ColdStart.RESUME, d.reason


def test_a_gap_larger_than_the_stated_fee_still_halts():
    state = acquired({"cumFeeDetail": {"BTC": str(FEE_BTC)}}).to_state()
    assert cold(state, 0.001 - 5 * FEE_BTC).action is ColdStart.HALT


def test_a_surplus_still_halts():
    state = acquired({"cumFeeDetail": {"BTC": str(FEE_BTC)}}).to_state()
    assert cold(state, 0.001 + FEE_BTC).action is ColdStart.HALT


def test_a_usdt_fee_explains_no_shortfall():
    state = acquired({"cumFeeDetail": {"USDT": "0.1"}}).to_state()
    assert cold(state, 0.001 - FEE_BTC).action is ColdStart.HALT
    assert cold(state, 0.001).action is ColdStart.RESUME


def test_an_unstated_fee_explains_no_shortfall():
    state = acquired({}).to_state()
    assert cold(state, 0.001 - FEE_BTC).action is ColdStart.HALT


def test_a_ledger_written_before_the_field_existed_still_loads():
    state = acquired({"cumFeeDetail": {"BTC": str(FEE_BTC)}}).to_state()
    del state["position"]["spot"]["fee_base_qty"]
    assert cold(state, 0.001).action is ColdStart.RESUME
    assert cold(state, 0.001 - FEE_BTC).action is ColdStart.HALT


def test_overlay_behaviour_is_unchanged():
    e = opened(Venue(), mode=OVERLAY)
    assert cold(e.to_state(), 5.0, mode=OVERLAY).action is ColdStart.RESUME
    assert cold(e.to_state(), 0.0005, mode=OVERLAY).action is ColdStart.HALT
