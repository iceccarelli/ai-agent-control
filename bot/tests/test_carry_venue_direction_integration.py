"""Integration of the direction-aware position read with the book's reconciliation.

`CarryBroker.get_perp_position` (real code, fed Bybit-shaped `/v5/position/list`
rows) is wired to the real `CarryEngine` tick check and the real
`TradingBot.carry_cold_start`. A perp LONG, a dual-row (hedge-mode) account,
a row with no `side`, a malformed size and an unreadable response must neither
pass as the expected short hedge nor read as flat. Offline and SYNTHETIC: the
rows follow Bybit's documented shape (unsigned `size`, `side` Buy/Sell); none is
a venue observation, and the client has no order path that is allowed to fire.
"""
from __future__ import annotations

import os
import sys
from dataclasses import asdict
from unittest import mock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import main as _main  # noqa: E402
from carry_broker import CarryBroker  # noqa: E402
from carry_engine import CarryEngine, Leg  # noqa: E402
from test_carry_broker import FakeClient, envelope  # noqa: E402

MARK = 100_000.0
QTY = 0.001


def broker_with_rows(*rows, raises=None):
    client = FakeClient(mark=MARK)
    real = client._request

    def request(method, endpoint, **kw):
        if endpoint == "/v5/position/list":
            if raises is not None:
                raise raises
            if rows == (None,):
                return {"retCode": 0, "retMsg": "OK", "result": {}}
            return envelope({"list": [dict(r, positionIM="20", positionMM="5")
                                      for r in rows]})
        return real(method, endpoint, **kw)

    client._request = request
    b = CarryBroker(client=client, order_gate=lambda: (True, "TEST"),
                    sequence_source=lambda *a: 1)
    b._client = client
    return b


def held_engine(broker, mode="acquire"):
    e = CarryEngine(broker=broker, max_notional_usd=100.0, borrow_apr=0.0,
                    execution_mode=mode, persist=lambda s: None)
    e.restore({"perp_symbol": "BTCUSDT", "last_print_ms": 0,
               "funding_history": [], "position": {
                   "spot": asdict(Leg(symbol="BTCUSDT", side="Buy",
                                      product="spot", requested_qty=QTY,
                                      filled_qty=QTY, avg_price=MARK)),
                   "perp": asdict(Leg(symbol="BTCUSDT", side="Sell",
                                      product="linear", requested_qty=QTY,
                                      filled_qty=QTY, avg_price=MARK)),
                   "opened_ms": 1, "funding_collected": 0.0,
                   "negative_funding_streak": 0}})
    return e


def tick(e):
    return e.on_candle(mark=MARK, funding_bps=1.0, spot=MARK,
                       funding_print_ms=1_000)


SHORT = {"side": "Sell", "size": str(QTY)}


def test_the_matching_short_is_the_only_row_that_passes():
    b = broker_with_rows(SHORT)
    d = tick(held_engine(b))
    assert d.action != "halted", d.reason
    assert not b._client.created


@pytest.mark.parametrize("rows", [
    ({"side": "Buy", "size": str(QTY)},),                    # LONG, same size
    ({"size": str(QTY)},),                                   # no side stated
    ({"side": "Sell", "size": "abc"},),                      # malformed size
    ({"side": "Sell", "size": "nan"},),                      # non-finite size
    ({"side": "Sell", "size": str(QTY)},
     {"side": "Buy", "size": str(QTY)}),                     # dual rows, a long leg
    ({"side": "Buy", "size": "0"}, {"side": "Buy", "size": str(QTY)}),
], ids=["long", "no-side", "malformed", "nan", "dual-with-long", "dual-long-2nd"])
def test_a_position_that_is_not_the_short_halts_and_trades_nothing(rows):
    b = broker_with_rows(*rows)
    d = tick(held_engine(b))
    assert d.action == "halted", d.reason
    assert d.reason == "VENUE_POSITION_UNREADABLE"
    assert not b._client.created


def test_dual_short_rows_are_summed_not_first_row_only():
    # Hedge-mode style: two short rows. 0.0005 + 0.0005 == the book's 0.001.
    half = str(QTY / 2)
    ok = broker_with_rows({"side": "Sell", "size": half},
                          {"side": "Sell", "size": half})
    assert tick(held_engine(ok)).action != "halted"
    # ... and a first row alone (the old behaviour) would have read as DRIFT:
    drift = broker_with_rows({"side": "Sell", "size": half})
    d = tick(held_engine(drift))
    assert d.action == "halted" and d.reason == "VENUE_POSITION_DRIFT"


def test_a_flat_venue_with_a_held_book_is_drift_not_a_match():
    # With no position row the margin read (which runs first) cannot be made
    # either; both are halts, neither is "flat so fine" and neither trades.
    b = broker_with_rows()
    d = tick(held_engine(b))
    assert d.action == "halted"
    assert d.reason in ("VENUE_POSITION_DRIFT", "MARGIN_UNREADABLE"), d.reason
    assert not b._client.created


def test_an_unreadable_response_halts_and_trades_nothing():
    for b in (broker_with_rows(None),
              broker_with_rows(raises=RuntimeError("timeout"))):
        d = tick(held_engine(b))
        assert d.action == "halted"
        assert d.reason in ("VENUE_POSITION_UNREADABLE", "MARGIN_UNREADABLE")
        assert not b._client.created


# -- cold start ---------------------------------------------------------------

class _Cold(CarryBroker):
    """Real position read; the other cold-start reads are flat/empty."""
    def get_spot_inventory(self, symbol): return 0.0
    def get_open_carry_orders(self, spot_symbol, perp_symbol): return []
    def get_funding_history(self, symbol, limit=8): return []


def cold_bot(rows):
    inner = broker_with_rows(*rows)
    b = _Cold(client=inner._client, order_gate=lambda: (True, "TEST"),
              sequence_source=lambda *a: 1)
    bot = _main.TradingBot.__new__(_main.TradingBot)
    bot.carry = CarryEngine(broker=b, max_notional_usd=100.0, borrow_apr=0.0,
                            execution_mode="acquire", persist=lambda s: None)
    bot.store = mock.Mock(load_carry_position=lambda: None)
    return bot, inner._client


def test_cold_start_with_a_flat_venue_starts():
    bot, _ = cold_bot([])
    assert bot.carry_cold_start() is True


@pytest.mark.parametrize("rows", [
    [{"side": "Buy", "size": str(QTY)}],
    [{"size": str(QTY)}],
    [{"side": "Sell", "size": "abc"}],
    [{"side": "Sell", "size": str(QTY)}, {"side": "Buy", "size": str(QTY)}],
])
def test_cold_start_never_reads_a_non_short_as_flat_or_as_a_hedge(rows):
    bot, client = cold_bot(rows)
    assert bot.carry_cold_start() is False
    assert not client.created


def test_cold_start_with_an_unrecorded_short_does_not_start():
    # The venue holds a short the ledger knows nothing about.
    bot, client = cold_bot([SHORT])
    assert bot.carry_cold_start() is False
    assert not client.created
