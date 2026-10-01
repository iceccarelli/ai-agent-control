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

import pytest

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
        # Both legs already in USDT: identity conversion, numbers unchanged.
        store = _FakeStore(_position(entry_fee_meta=0.05, entry_fee_currency="USDT"))
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
        assert trade.entry_fee_currency == "USDT"
        assert trade.exit_fee == 0.0202
        assert trade.exit_fee_known is True
        assert trade.exit_fee_currency == "USDT"
        assert report.detail["entry_fee"] == 0.05
        assert report.detail["entry_fee_currency"] == "USDT"
        assert report.detail["exit_fee"] == 0.0202
        assert report.detail["exit_fee_currency"] == "USDT"
        assert report.detail["entry_fee_known"] is True
        assert report.detail["exit_fee_known"] is True

    def test_real_btcusdt_spot_shape_is_normalized_before_booking_not_raw_summed(
            self):
        """The ACTUAL Bybit spot convention this repo's own INVENTORY.md
        documents (D11/D44): a spot BUY's fee is charged in the base coin,
        a spot SELL's fee in the quote coin. close_position() must convert
        the BTC leg into the accounting unit (this leg's own trade price)
        BEFORE booking — never let a raw BTC amount and a raw USDT amount
        sit in the same TradeRecord as if they were comparable."""
        store = _FakeStore(_position(entry_fee_meta=0.000002355,
                                     entry_fee_currency="BTC",
                                     entry_price=83364.6))
        client = _FakeClient(exit_order_row={
            "orderStatus": "Filled", "cumExecQty": "1.0", "avgPrice": "83364.5",
            "cumExecFee": "0.0834", "feeCurrency": "USDT",
        })
        engine = _engine(store, client)
        engine.close_position(symbol=SYMBOL, reason="test")
        trade = store.trades[0]
        # Converted into USDT at THIS trade's own entry price, not left raw.
        assert trade.entry_fee == pytest.approx(0.000002355 * 83364.6)
        assert trade.entry_fee_currency == "USDT"
        assert trade.exit_fee == 0.0834
        assert trade.exit_fee_currency == "USDT"
        # The defining property this whole pass exists to guarantee: once
        # booked, both legs are the SAME currency, so total_fees/net_pnl
        # can sum them without ever having mixed BTC and USDT.
        assert trade.entry_fee_currency == trade.exit_fee_currency
        assert trade.fees_compatible is True
        assert trade.total_fees == pytest.approx(
            0.000002355 * 83364.6 + 0.0834)

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
        the two must never silently disagree, once both are expressed in
        the same (converted) unit."""
        store = _FakeStore(_position(entry_fee_meta=0.0314159,
                                     entry_fee_currency="USDT",
                                     entry_price=100.0))
        client = _FakeClient(exit_order_row={
            "orderStatus": "Filled", "cumExecQty": "1.0", "avgPrice": "101.0",
            "cumExecFee": "0.01", "feeCurrency": "USDT",
        })
        engine = _engine(store, client)
        engine.close_position(symbol=SYMBOL, reason="test")
        assert store.trades[0].entry_fee == 0.0314159
        assert store.trades[0].entry_fee_currency == "USDT"

    def test_unresolved_currency_never_enters_the_booked_total(self):
        """A real, known amount in a currency that is neither the
        accounting unit nor the pair's base coin (e.g. a foreign ticker)
        must not be guessed into a number — it is re-marked unknown
        rather than let through as a false same-unit figure."""
        store = _FakeStore(_position(entry_fee_meta=0.05,
                                     entry_fee_currency="ETH"))
        client = _FakeClient(exit_order_row={
            "orderStatus": "Filled", "cumExecQty": "1.0", "avgPrice": "101.0",
            "cumExecFee": "0.02", "feeCurrency": "USDT",
        })
        engine = _engine(store, client)
        engine.close_position(symbol=SYMBOL, reason="test")
        trade = store.trades[0]
        assert trade.entry_fee == 0.0
        assert trade.entry_fee_known is False
        assert trade.entry_fee_currency is None
        # The other, resolvable leg is unaffected and still sums safely.
        assert trade.fees_compatible is True
        assert trade.total_fees == 0.02


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
        # The remaining position's meta must still carry the ORIGINAL raw
        # fact (BTC, unconverted) — `upsert_position` REPLACES meta
        # wholesale, so this is the one place it could silently vanish.
        # Conversion is `close_position`'s job, at FINAL close time, not
        # something this partial-exit rewrite must do.
        assert store.upserts[-1]["entry_fee"] == 0.00001
        assert store.upserts[-1]["entry_fee_currency"] == "BTC"
        # The partial booking itself is an ESTIMATE (no real fill to read
        # a fee from), correctly marked unknown — not what this test is
        # about, but worth pinning down so it is not mistaken for venue
        # truth later.
        assert store.trades[0].entry_fee_known is False

        # Now the remainder is closed for real, through the production
        # close_position() path, against the position row record_exit_fill
        # just rewrote. The raw BTC entry fee is converted at THIS point,
        # using the position's entry_price as the conversion basis.
        report = engine.close_position(symbol=SYMBOL, reason="final_close")
        assert report.ok
        final_trade = store.trades[-1]
        assert final_trade.entry_fee == pytest.approx(0.00001 * 100.0)
        assert final_trade.entry_fee_known is True
        assert final_trade.entry_fee_currency == "USDT"
        assert final_trade.exit_fee == 0.03
        assert final_trade.exit_fee_currency == "USDT"
        assert final_trade.fees_compatible is True


class TestTradeRecordFailsClosedOnIncompatibleCurrencies:
    """persistence.TradeRecord.total_fees/.net_pnl: the structural backstop
    against raw cross-currency summing, for any caller that bypasses
    close_position()'s normalization and constructs one directly."""

    def _record(self, **over):
        from persistence import TradeRecord
        base = dict(
            symbol="BTCUSDT", side="Buy", qty=1.0, entry_price=100.0,
            exit_price=101.0, gross_pnl=1.0, entry_fee=0.00001, exit_fee=0.03,
            opened_epoch=1.0, closed_epoch=2.0,
        )
        base.update(over)
        return TradeRecord(**base)

    def test_btc_entry_fee_and_usdt_exit_fee_cannot_be_raw_summed(self):
        from persistence import IncompatibleFeeCurrencies
        record = self._record(entry_fee_currency="BTC", exit_fee_currency="USDT")
        assert record.fees_compatible is False
        with pytest.raises(IncompatibleFeeCurrencies):
            record.total_fees
        with pytest.raises(IncompatibleFeeCurrencies):
            record.net_pnl

    def test_same_unit_fees_aggregate_correctly(self):
        record = self._record(entry_fee_currency="USDT", exit_fee_currency="USDT")
        assert record.fees_compatible is True
        assert record.total_fees == pytest.approx(0.00001 + 0.03)
        assert record.net_pnl == pytest.approx(1.0 - (0.00001 + 0.03))

    def test_legacy_untracked_currency_is_unchanged_behaviour(self):
        """Neither currency set at all (every pre-existing caller except
        close_position) — summed exactly as before this pass, the
        deliberately-preserved backward-compatible case."""
        record = self._record(entry_fee_currency=None, exit_fee_currency=None)
        assert record.fees_compatible is True
        assert record.total_fees == pytest.approx(0.00001 + 0.03)

    def test_one_currency_known_one_unset_is_still_compatible(self):
        """Only one leg ever got a currency recorded (e.g. the other was
        truly unknown) — not a contradiction, so still summable; this is
        NOT the same as two DIFFERENT known currencies."""
        record = self._record(entry_fee_currency="USDT", exit_fee_currency=None)
        assert record.fees_compatible is True
        assert record.total_fees == pytest.approx(0.00001 + 0.03)

    def test_unknown_fee_remains_unknown_through_total_fees(self):
        record = self._record(
            entry_fee=0.0, entry_fee_known=False, entry_fee_currency=None,
            exit_fee=0.03, exit_fee_known=True, exit_fee_currency="USDT")
        assert record.entry_fee_known is False
        # The booked number is still summable (compatible currencies —
        # None is never a contradiction), but a reader checking
        # entry_fee_known learns not to trust the entry leg's contribution.
        assert record.total_fees == pytest.approx(0.03)

    def test_known_zero_remains_zero_not_unknown(self):
        record = self._record(
            entry_fee=0.0, entry_fee_known=True, entry_fee_currency="USDT",
            exit_fee=0.0, exit_fee_known=True, exit_fee_currency="USDT")
        assert record.entry_fee_known is True
        assert record.exit_fee_known is True
        assert record.total_fees == 0.0
        assert record.net_pnl == record.gross_pnl
