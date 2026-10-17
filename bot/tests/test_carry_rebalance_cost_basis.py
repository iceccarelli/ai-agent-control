"""A rebalance fill must keep the persisted perp leg equal to what the journal holds.

`CarryEngine._rebalance` changed `perp.filled_qty` and nothing else. Selling
more perp at a different price left the leg's `avg_price` (and `fee`) at the
original entry, while `LedgerBroker` had correctly re-weighted its own entry.
After a restart `_reseed_cost_basis` seeds the journal from the LEG, so the
realised P&L of the later close was computed against a stale entry price.
Buying perp back is a CLOSE of part of the short: the remaining short keeps its
entry price, and the closing fill is not blended into it.

Offline: the scripted `FeeVenue` of test_carry_fee_truth; no network.
"""
from __future__ import annotations

import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import ledger as L  # noqa: E402
import test_carry_fee_truth as T  # noqa: E402
from carry_engine import CarryEngine  # noqa: E402
from carry_risk import CarryRisk  # noqa: E402
from test_carry_fee_truth import H8, build, modern, open_the_hedge  # noqa: E402

ENTRY = 100_000.0


def cost_basis_fees(product, side, filled):
    if product == "spot" and side == "Buy":
        # This suite isolates perp entry/exit basis; use a reported quote-currency
        # spot fee so quantity remains unchanged and tests exercise one variable.
        return {"cumExecFee": "0", "cumFeeDetail": {"USDT": "0"}}
    return modern(product, side, filled)


def opened_with_perp_fill_ratio(ratio):
    """Open the hedge, but let the venue fill only `ratio` of the perp order."""
    venue, journal, engine = build(cost_basis_fees)
    real = venue._request
    state = {"done": False}

    def request(method, endpoint, **kw):
        body = kw.get("body")
        if (endpoint == "/v5/order/create" and body and not state["done"]
                and body["category"] == "linear" and body["side"] == "Sell"):
            state["done"] = True
            kw = dict(kw, body=dict(
                body, qty=f"{float(body['qty']) * ratio:.8f}"))
        return real(method, endpoint, **kw)

    venue._request = request
    assert open_the_hedge(engine).action == "opened"
    venue.perp_qty = engine.position.perp.filled_qty
    return venue, journal, engine


def oversized_perp(monkeypatch):
    """A short larger than its spot: the venue and the journal both hold 1.0
    perp against 0.97 spot (e.g. spot was reduced), so the correction is a BUY."""
    venue, journal, engine = build(cost_basis_fees)
    assert open_the_hedge(engine).action == "opened"
    engine.position.spot.filled_qty = 0.97
    engine.position.spot.requested_qty = 0.97
    return venue, journal, engine


def rebalance_at(engine, venue, price, monkeypatch):
    monkeypatch.setattr(T, "MARK", price)           # the fill prices at `price`
    decision = engine.on_candle(mark=price, funding_bps=3.0, spot=price,
                                funding_print_ms=3 * H8)
    assert decision.action == "rebalanced", (decision.action, decision.reason)
    return decision


def restored(state):
    _venue, _journal, fresh = build(cost_basis_fees)
    fresh.restore(state)
    return fresh


class TestSellingMorePerpReweightsTheShort:
    def test_the_leg_matches_the_journals_weighted_entry(self, monkeypatch):
        venue, _j, engine = opened_with_perp_fill_ratio(0.97)
        entry_fee = engine.position.perp.fee
        rebalance_at(engine, venue, 135_000.0, monkeypatch)
        perp = engine.position.perp
        assert perp.filled_qty == pytest.approx(1.0)
        want = (0.97 * ENTRY + 0.03 * 135_000.0) / 1.0
        assert perp.avg_price == pytest.approx(want)
        assert perp.avg_price == pytest.approx(engine.broker._perp_entry)
        assert perp.notional == pytest.approx(0.97 * ENTRY + 0.03 * 135_000.0)
        # the rebalance fill's own fee (linear taker, USDT) is part of the leg
        assert perp.fee == pytest.approx(
            entry_fee + 0.03 * 135_000.0 * 0.00055)

    def test_a_restart_seeds_the_journal_with_the_same_entry(self, monkeypatch):
        venue, _j, engine = opened_with_perp_fill_ratio(0.97)
        rebalance_at(engine, venue, 135_000.0, monkeypatch)
        again = restored(engine.to_state())
        assert again.broker._perp_entry == pytest.approx(
            engine.broker._perp_entry)
        assert again.broker._perp_qty == pytest.approx(1.0)

    def test_an_unknown_fee_stays_unknown(self, monkeypatch):
        venue, _j, engine = opened_with_perp_fill_ratio(0.97)
        engine.position.perp.fee = None
        rebalance_at(engine, venue, 135_000.0, monkeypatch)
        assert engine.position.perp.fee is None


class TestBuyingPerpBackClosesPartOfTheShort:
    def test_the_remaining_short_keeps_its_entry_price_and_fee(
            self, monkeypatch):
        venue, _j, engine = oversized_perp(monkeypatch)
        entry_fee = engine.position.perp.fee
        rebalance_at(engine, venue, 140_000.0, monkeypatch)
        perp = engine.position.perp
        assert perp.filled_qty == pytest.approx(0.97)
        assert perp.requested_qty == pytest.approx(0.97)
        assert perp.avg_price == pytest.approx(ENTRY)       # not blended
        assert perp.fee == pytest.approx(entry_fee)         # journal has the close
        assert perp.avg_price == pytest.approx(engine.broker._perp_entry)

    def test_a_restart_keeps_agreeing_with_the_journal(self, monkeypatch):
        venue, _j, engine = oversized_perp(monkeypatch)
        rebalance_at(engine, venue, 140_000.0, monkeypatch)
        again = restored(engine.to_state())
        assert again.broker._perp_entry == pytest.approx(
            engine.broker._perp_entry)
