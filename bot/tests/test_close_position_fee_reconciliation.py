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
        self.upserts = []

    def open_positions(self):
        return [self._position] if self._position else []

    def record_trade(self, trade):
        self.trades.append(trade)
        return len(self.trades)

    def remove_position(self, symbol):
        self.removed.append(symbol)
        self._position = None

    def journal(self, symbol, kind, reason, detail):
        self.journal_entries.append((symbol, kind, reason, detail))

    def update_order_status(self, order_link_id, status):
        pass

    def upsert_position(self, symbol, side, qty, entry_price, *,
                        stop_price=0.0, order_link_id="", meta=None):
        import json as _json
        self.upserts.append(dict(meta or {}))
        self._position = {
            "symbol": symbol, "side": side, "qty": qty,
            "entry_price": entry_price, "stop_price": stop_price,
            "opened_epoch": 1000.0, "order_link_id": order_link_id,
            "meta": _json.dumps(dict(meta or {})),
        }


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


def _position(*, entry_fee_meta=None, entry_fee_currency=None,
              entry_price=100.0, qty=1.0, side="Buy"):
    import json as _json
    meta = {}
    if entry_fee_meta is not None:
        meta["entry_fee"] = entry_fee_meta
        meta["entry_fee_currency"] = entry_fee_currency
    return {
        "symbol": SYMBOL, "side": side, "qty": qty, "entry_price": entry_price,
        "opened_epoch": 1000.0,
        "meta": _json.dumps(meta),
    }


class TestCloseBooksTheVenuesOwnFee:
    def test_exit_fee_known_uses_venues_cumExecFee_not_a_static_estimate(self):
        store = _FakeStore(_position(entry_fee_meta=0.05, entry_fee_currency="BTC"))
        client = _FakeClient(exit_order_row={
            "orderStatus": "Filled", "cumExecQty": "1.0", "avgPrice": "101.0",
            "cumExecFee": "0.0202", "feeCurrency": "USDT",
        })
        engine = _engine(store, client)
        report = engine.close_position(symbol=SYMBOL, reason="test")
        assert report.ok
        trade = store.trades[0]
        assert trade.entry_fee == 0.05
        assert trade.entry_fee_known is True
        assert trade.entry_fee_currency == "BTC"
        assert trade.exit_fee == 0.0202
        assert trade.exit_fee_known is True
        assert trade.exit_fee_currency == "USDT"
        assert report.detail["entry_fee"] == 0.05
        assert report.detail["entry_fee_currency"] == "BTC"
        assert report.detail["exit_fee"] == 0.0202
        assert report.detail["exit_fee_currency"] == "USDT"
        assert report.detail["entry_fee_known"] is True
        assert report.detail["exit_fee_known"] is True

    def test_real_btcusdt_spot_shape_entry_fee_in_btc_exit_fee_in_usdt(self):
        """The ACTUAL Bybit spot convention this repo's own INVENTORY.md
        documents (D11/D44): a spot BUY's fee is charged in the base coin,
        a spot SELL's fee in the quote coin. These are genuinely different
        units and must never be summed as if they were the same currency
        — this test exists so that non-mixing claim is checked against the
        real shape, not a contrived one."""
        store = _FakeStore(_position(entry_fee_meta=0.000002355,
                                     entry_fee_currency="BTC"))
        client = _FakeClient(exit_order_row={
            "orderStatus": "Filled", "cumExecQty": "1.0", "avgPrice": "83364.5",
            "cumExecFee": "0.0834", "feeCurrency": "USDT",
        })
        engine = _engine(store, client)
        engine.close_position(symbol=SYMBOL, reason="test")
        trade = store.trades[0]
        assert trade.entry_fee_currency == "BTC"
        assert trade.exit_fee_currency == "USDT"
        assert trade.entry_fee_currency != trade.exit_fee_currency

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
        assert trade.entry_fee_currency is None
        assert trade.exit_fee == 0.0
        assert trade.exit_fee_known is False
        assert trade.exit_fee_currency is None
        assert report.detail["entry_fee_known"] is False
        assert report.detail["exit_fee_known"] is False

    def test_known_zero_fee_is_distinct_from_unknown(self):
        """`known_zero != unknown`: the venue can genuinely charge 0."""
        store = _FakeStore(_position(entry_fee_meta=0.0, entry_fee_currency="BTC"))
        client = _FakeClient(exit_order_row={
            "orderStatus": "Filled", "cumExecQty": "1.0", "avgPrice": "101.0",
            "cumExecFee": "0", "feeCurrency": "USDT",
        })
        engine = _engine(store, client)
        engine.close_position(symbol=SYMBOL, reason="test")
        trade = store.trades[0]
        assert trade.entry_fee == 0.0
        assert trade.entry_fee_known is True
        assert trade.exit_fee == 0.0
        assert trade.exit_fee_known is True

    def test_reconciliation_entry_fee_survives_from_meta_to_trade_row(self):
        """entry fee was captured at OPEN time (position meta) and must
        reconcile through to the row `close_position` books at EXIT time —
        the two must never silently disagree."""
        store = _FakeStore(_position(entry_fee_meta=0.0314159,
                                     entry_fee_currency="BTC"))
        client = _FakeClient(exit_order_row={
            "orderStatus": "Filled", "cumExecQty": "1.0", "avgPrice": "101.0",
            "cumExecFee": "0.01", "feeCurrency": "USDT",
        })
        engine = _engine(store, client)
        engine.close_position(symbol=SYMBOL, reason="test")
        assert store.trades[0].entry_fee == 0.0314159
        assert store.trades[0].entry_fee_currency == "BTC"


class TestEntryFeeSurvivesAPartialExit:
    """requirement: entry fee survives -> partial exit -> final close ->
    record_trade. Proven through the actual `record_exit_fill` ->
    `close_position` call path, not by inspecting the source."""

    def test_entry_fee_and_currency_reach_the_final_close_after_a_partial_exit(
            self):
        store = _FakeStore(_position(
            entry_fee_meta=0.00001, entry_fee_currency="BTC",
            entry_price=100.0, qty=2.0))
        client = _FakeClient(exit_order_row={
            "orderStatus": "Filled", "cumExecQty": "1.0", "avgPrice": "101.0",
            "cumExecFee": "0.03", "feeCurrency": "USDT",
        })
        engine = _engine(store, client)

        # A take-profit leg fills for HALF the position — exchange-side,
        # discovered the way `observe_exits` discovers it.
        fully_closed = engine.record_exit_fill(
            symbol=SYMBOL, qty=1.0, price=105.0, fee=0.0,
            reason="tp_leg_fill", dedupe_key="tp-leg-1")
        assert fully_closed is False
        # The remaining position's meta must still carry the ORIGINAL
        # entry fee fact — `upsert_position` REPLACES meta wholesale, so
        # this is the one place that fact could silently vanish.
        assert store.upserts[-1]["entry_fee"] == 0.00001
        assert store.upserts[-1]["entry_fee_currency"] == "BTC"
        # The partial booking itself is an ESTIMATE (no real fill to read
        # a fee from), correctly marked unknown — not what this test is
        # about, but worth pinning down so it is not mistaken for venue
        # truth later.
        assert store.trades[0].entry_fee_known is False

        # Now the remainder is closed for real, through the production
        # close_position() path, against the position row record_exit_fill
        # just rewrote.
        report = engine.close_position(symbol=SYMBOL, reason="final_close")
        assert report.ok
        final_trade = store.trades[-1]
        assert final_trade.entry_fee == 0.00001
        assert final_trade.entry_fee_known is True
        assert final_trade.entry_fee_currency == "BTC"
        assert final_trade.exit_fee == 0.03
        assert final_trade.exit_fee_currency == "USDT"
