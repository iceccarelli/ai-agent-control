"""trading_engine.close_position() — fee reconciliation against venue truth.

The accounting bug this locks in: `close_position` used to default
`entry_fee`/`exit_fee` to 0.0 unconditionally, so the local `trades` row
recorded zero cost even when the venue's own fill carried a real
`cumExecFee` — and even when it carried none at all, the ledger built from
that row had no way to tell "charged nothing" apart from "the real cost
was never recorded". `venue_fee()` (same rule as
`carry_broker.CarryBroker._fee`) and the `entry_fee_known`/`exit_fee_known`
flags on `TradeRecord` are the fix; these tests hold the boundary.

Builds a `TradingEngine` with `object.__new__` and only the two
collaborators `close_position` actually reads (`store`, `client`) as
hand-rolled fakes — the same style `test_testnet_session.py` already uses
for its own `run_session` tests, not the full FakeBybit fixture, because
nothing here needs a real order book or signing.
"""
from __future__ import annotations

import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import trading_engine as te  # noqa: E402

SYMBOL = "BTCUSDT"


class _FakeStore:
    def __init__(self, position):
        self._position = position
        self.trades = []
        self.removed = []
        self.journal_entries = []

    def open_positions(self):
        return [self._position] if self._position else []

    def record_trade(self, trade):
        self.trades.append(trade)
        return len(self.trades)

    def remove_position(self, symbol):
        self.removed.append(symbol)

    def journal(self, symbol, kind, reason, detail):
        self.journal_entries.append((symbol, kind, reason, detail))

    def update_order_status(self, order_link_id, status):
        pass


class _FakeOrderResult:
    def __init__(self, *, ok=True, order_link_id="BB-clos-1", order_id="ex-1",
                 reason="OK"):
        self.ok = ok
        self.order_link_id = order_link_id
        self.order_id = order_id
        self.reason = reason


class _FakeClient:
    def __init__(self, *, exit_order_row):
        self._exit_order_row = exit_order_row
        self.cancelled = []

    def place_order(self, **kw):
        return _FakeOrderResult()

    def get_order(self, *, symbol, order_link_id):
        # Terminal on the first poll — _await_fill returns immediately.
        return self._exit_order_row

    def cancel_all(self, symbol):
        self.cancelled.append(symbol)


def _engine(store, client):
    engine = object.__new__(te.TradingEngine)
    engine.store = store
    engine.client = client
    return engine


def _position(*, entry_fee_meta=None, entry_price=100.0, qty=1.0, side="Buy"):
    import json as _json
    return {
        "symbol": SYMBOL, "side": side, "qty": qty, "entry_price": entry_price,
        "opened_epoch": 1000.0,
        "meta": _json.dumps({} if entry_fee_meta is None
                            else {"entry_fee": entry_fee_meta}),
    }


class TestVenueFeeExtraction:
    def test_known_fee_is_a_float(self):
        assert te.venue_fee({"cumExecFee": "0.0123"}) == 0.0123

    def test_missing_fee_is_none_not_zero(self):
        assert te.venue_fee({}) is None
        assert te.venue_fee({"cumExecFee": None}) is None
        assert te.venue_fee({"cumExecFee": ""}) is None
        assert te.venue_fee(None) is None

    def test_garbage_fee_is_none(self):
        assert te.venue_fee({"cumExecFee": "not-a-number"}) is None
        assert te.venue_fee({"cumExecFee": "nan"}) is None
        assert te.venue_fee({"cumExecFee": "inf"}) is None


class TestCloseBooksTheVenuesOwnFee:
    def test_exit_fee_known_uses_venues_cumExecFee_not_a_static_estimate(self):
        store = _FakeStore(_position(entry_fee_meta=0.05))
        client = _FakeClient(exit_order_row={
            "orderStatus": "Filled", "cumExecQty": "1.0", "avgPrice": "101.0",
            "cumExecFee": "0.0202",
        })
        engine = _engine(store, client)
        report = engine.close_position(symbol=SYMBOL, reason="test")
        assert report.ok
        trade = store.trades[0]
        assert trade.entry_fee == 0.05
        assert trade.entry_fee_known is True
        assert trade.exit_fee == 0.0202
        assert trade.exit_fee_known is True
        assert report.detail["entry_fee"] == 0.05
        assert report.detail["exit_fee"] == 0.0202
        assert report.detail["entry_fee_known"] is True
        assert report.detail["exit_fee_known"] is True

    def test_unknown_fee_is_recorded_as_unknown_not_silently_zero(self):
        store = _FakeStore(_position(entry_fee_meta=None))
        client = _FakeClient(exit_order_row={
            "orderStatus": "Filled", "cumExecQty": "1.0", "avgPrice": "101.0",
            # No cumExecFee at all — the venue did not say.
        })
        engine = _engine(store, client)
        report = engine.close_position(symbol=SYMBOL, reason="test")
        trade = store.trades[0]
        # The booked number may still be 0.0 (the caller-supplied default),
        # but it must be marked UNKNOWN, never indistinguishable from a
        # venue-confirmed zero-fee fill.
        assert trade.entry_fee == 0.0
        assert trade.entry_fee_known is False
        assert trade.exit_fee == 0.0
        assert trade.exit_fee_known is False
        assert report.detail["entry_fee_known"] is False
        assert report.detail["exit_fee_known"] is False

    def test_reconciliation_entry_fee_survives_from_meta_to_trade_row(self):
        """entry fee was captured at OPEN time (position meta) and must
        reconcile through to the row `close_position` books at EXIT time —
        the two must never silently disagree."""
        store = _FakeStore(_position(entry_fee_meta=0.0314159))
        client = _FakeClient(exit_order_row={
            "orderStatus": "Filled", "cumExecQty": "1.0", "avgPrice": "101.0",
            "cumExecFee": "0.01",
        })
        engine = _engine(store, client)
        engine.close_position(symbol=SYMBOL, reason="test")
        assert store.trades[0].entry_fee == 0.0314159
