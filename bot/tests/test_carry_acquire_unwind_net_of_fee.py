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


def open_once(row, venue_cls=WalletVenue):
    v = venue_cls(row)
    e = engine(v, mode=ACQUIRE)
    d = e.on_candle(mark=100_000.0, funding_bps=3.0, spot=100_000.0,
                    funding_print_ms=3 * H8, timestamp_ms=3 * H8)
    return v, e, d


def run(row):
    v, e, d = open_once(row)
    assert d.action == "opened", d.reason
    for k in (4, 5, 6):
        e.on_candle(mark=100_000.0, funding_bps=-0.5, spot=100_000.0,
                    funding_print_ms=k * H8, timestamp_ms=k * H8)
    return v, e


def test_entry_hedges_net_btc_received_after_base_coin_fee():
    v, e, d = open_once({"cumFeeDetail": {"BTC": str(FEE_BTC)}})
    assert d.action == "opened", d.reason
    assert e.position is not None

    spot = e.position.spot
    perp = e.position.perp
    net_spot = spot.filled_qty - spot.fee_base_qty
    assert spot.fee_base_qty == pytest.approx(FEE_BTC)
    assert v.wallet == pytest.approx(net_spot)
    assert perp.requested_qty == pytest.approx(net_spot)
    assert perp.filled_qty == pytest.approx(net_spot)
    assert e.position.delta_qty == pytest.approx(0.0, abs=1e-12)
    assert e.position.delta_fraction(100_000.0) == pytest.approx(0.0, abs=1e-12)
    assert v.orders[-1][0:2] == ("Sell", "linear")
    assert v.orders[-1][2] == pytest.approx(net_spot)


def test_quote_coin_fee_does_not_reduce_the_spot_quantity_to_hedge():
    v, e, d = open_once({"cumFeeDetail": {"USDT": "0.1"}})
    assert d.action == "opened", d.reason
    assert e.position is not None
    assert e.position.spot.fee_base_qty == pytest.approx(0.0)
    assert e.position.perp.filled_qty == pytest.approx(
        e.position.spot.filled_qty)
    assert e.position.delta_qty == pytest.approx(0.0, abs=1e-12)


def test_net_spot_below_linear_minimum_is_unwound_without_opening_short():
    class CoarseLinearLotVenue(WalletVenue):
        def get_lot_rules(self, symbol, product):
            if product == "linear":
                return {"qty_step": 0.001, "min_qty": 0.001,
                        "min_notional": 0.0}
            return super().get_lot_rules(symbol, product)

        def get_fee_rates(self, symbol, product):
            # A fee the tier did NOT predict (the order row carries a base-coin
            # fee anyway): the pre-trade refusal cannot see it, so this keeps
            # the in-flight defence under test.
            rates = dict(super().get_fee_rates(symbol, product))
            if product == "spot":
                rates["taker_bps"] = 0.0
            return rates

    v, e, d = open_once(
        {"cumFeeDetail": {"BTC": str(FEE_BTC)}},
        venue_cls=CoarseLinearLotVenue)
    assert d.action == "unwound", d.reason
    assert "SPOT_NET_BELOW_PERP_MINIMUM" in d.reason
    assert e.state is BookState.FLAT
    assert e.position is None
    assert v.wallet == pytest.approx(0.0, abs=1e-12)
    assert all(product != "linear" for _side, product, _qty in v.orders)


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


class OnStepSpotVenue(WalletVenue):
    """The venue refuses a spot quantity that is not a multiple of the spot
    basePrecision (1e-6): `170137 too many decimals`."""

    def place_market(self, *, symbol, side, qty, product):
        from decimal import Decimal
        if product == "spot" and Decimal(repr(qty)) % Decimal("0.000001") != 0:
            raise RuntimeError("170137 Order quantity has too many decimals")
        return super().place_market(symbol=symbol, side=side, qty=qty,
                                    product=product)


def test_a_fee_net_spot_sale_is_floored_to_the_spot_lot():
    """A 1.2e-6 BTC fee leaves a 7-decimal balance (0.0009988). Selling it
    as-is is a rejection AFTER the perp was bought back: a naked spot long."""
    v, e = run_with(OnStepSpotVenue, {"cumFeeDetail": {"BTC": "0.0000012"}})
    assert e.state is BookState.FLAT
    assert v.wallet < 1e-6                     # only sub-lot dust is left
    sells = [q for side, product, q in v.orders
             if product == "spot" and side == "Sell"]
    assert sells and sells[-1] == pytest.approx(0.000998)


def test_an_emergency_spot_sale_is_floored_to_the_spot_lot_too():
    class PerpRejected(OnStepSpotVenue):
        def place_market(self, *, symbol, side, qty, product):
            if product == "linear":
                return None
            return super().place_market(symbol=symbol, side=side, qty=qty,
                                        product=product)

    v = PerpRejected({"cumFeeDetail": {"BTC": "0.0000012"}})
    e = engine(v, mode=ACQUIRE)
    e.on_candle(mark=100_000.0, funding_bps=3.0, spot=100_000.0,
                funding_print_ms=3 * H8, timestamp_ms=3 * H8)
    assert e.state is BookState.FLAT
    assert v.wallet < 1e-6


def run_with(venue_cls, row):
    v, e, d = open_once(row, venue_cls=venue_cls)
    assert d.action == "opened", d.reason
    for k in (4, 5, 6):
        e.on_candle(mark=100_000.0, funding_bps=-0.5, spot=100_000.0,
                    funding_print_ms=k * H8, timestamp_ms=k * H8)
    return v, e


class _CoarseLot(WalletVenue):
    def __init__(self, row, step, **kw):
        super().__init__(row, **kw)
        self.step = step

    def get_lot_rules(self, symbol, product):
        if product == "linear":
            return {"qty_step": self.step, "min_qty": self.step,
                    "min_notional": 0.0}
        return super().get_lot_rules(symbol, product)


def test_acquire_refuses_an_entry_whose_fee_would_leave_the_pair_unpaired():
    """Spot BUY 0.01 with a 10 bps base-coin fee leaves 0.00999; the short snaps
    to 0.009 and the pair lands 5% unpaired (band 2%), then unwinds: a round
    trip of fees, again every UTC day. The fee tier already predicts it."""
    v = _CoarseLot({"cumFeeDetail": {"BTC": "0.00001"}}, step=0.001)
    e = engine(v, mode=ACQUIRE)
    d = e.on_candle(mark=100_000.0, funding_bps=3.0, spot=100_000.0,
                    funding_print_ms=3 * H8, timestamp_ms=3 * H8)
    assert d.action == "stand_aside" and d.reason == "ACQUIRE_LOT_TOO_COARSE_FOR_FEE"
    assert v.orders == [], "a leg was sent for a pair that cannot be paired"


def test_acquire_still_opens_when_the_lot_is_fine_enough_for_the_fee():
    v = _CoarseLot({"cumFeeDetail": {"BTC": "0.00001"}}, step=0.000001)
    e = engine(v, mode=ACQUIRE)
    d = e.on_candle(mark=100_000.0, funding_bps=3.0, spot=100_000.0,
                    funding_print_ms=3 * H8, timestamp_ms=3 * H8)
    assert d.action == "opened", d.reason
