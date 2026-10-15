"""Acquire unwind sells what the wallet holds, not what the order filled.

A spot BUY's fee is taken from the coin received, so after buying Q the wallet
holds ``Q - fee``. The unwind sold the leg's gross ``filled_qty``; the venue
rejects a spot sell above the free balance, so the perp short was already
bought back and the spot long was left unsold: an unhedged long, reported as
a partial close. The leg now sells ``filled_qty - fee_base_qty``.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import venue_fees  # noqa: E402
from carry_engine import ACQUIRE, BookState  # noqa: E402
from test_carry_cold_start import H8, Venue, engine  # noqa: E402

FEE_BTC = 0.000001


class WalletVenue(Venue):
    """A spot wallet that takes the Buy fee in BTC and refuses an oversell."""

    def __init__(self, row, **kw):
        super().__init__(**kw)
        self.row, self.wallet = row, 0.0

    def place_market(self, *, symbol, side, qty, product):
        if product == "spot" and side == "Sell" and qty > self.wallet + 1e-12:
            raise RuntimeError("170131 Insufficient balance")
        out = super().place_market(symbol=symbol, side=side, qty=qty,
                                   product=product)
        if product == "spot":
            if side == "Buy":
                fees = venue_fees.extract_order_fees(self.row)
                out.update(fee=fees.single_amount, fee_detail=fees.to_dict())
                self.wallet += qty - sum(c.amount for c in fees.components
                                         if c.currency == "BTC")
            else:
                self.wallet -= qty
        return out


def run(row):
    v = WalletVenue(row)
    e = engine(v, mode=ACQUIRE)
    d = e.on_candle(mark=100_000.0, funding_bps=3.0, spot=100_000.0,
                    funding_print_ms=3 * H8, timestamp_ms=3 * H8)
    assert d.action == "opened", d.reason
    for k in (4, 5, 6):
        e.on_candle(mark=100_000.0, funding_bps=-0.5, spot=100_000.0,
                    funding_print_ms=k * H8, timestamp_ms=k * H8)
    return v, e


def test_the_unwind_sells_what_the_wallet_holds_and_ends_flat():
    v, e = run({"cumFeeDetail": {"BTC": str(FEE_BTC)}})
    assert e.state is BookState.FLAT
    assert v.wallet == pytest.approx(0.0, abs=1e-12)


def test_a_fee_that_is_not_base_coin_changes_nothing():
    v, e = run({"cumFeeDetail": {"USDT": "0.1"}})
    assert e.state is BookState.FLAT
    assert v.wallet == pytest.approx(0.0, abs=1e-12)


def test_a_failed_perp_leg_sells_the_naked_spot_net_of_the_fee_too():
    class PerpRejected(WalletVenue):
        def place_market(self, *, symbol, side, qty, product):
            if product == "linear":
                return None
            return super().place_market(symbol=symbol, side=side, qty=qty,
                                        product=product)

    v = PerpRejected({"cumFeeDetail": {"BTC": str(FEE_BTC)}})
    e = engine(v, mode=ACQUIRE)
    e.on_candle(mark=100_000.0, funding_bps=3.0, spot=100_000.0,
                funding_print_ms=3 * H8, timestamp_ms=3 * H8)
    assert v.wallet == pytest.approx(0.0, abs=1e-12), (
        "a spot long was left unhedged and unsold")
