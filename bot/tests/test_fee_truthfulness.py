"""Fees on booked trades say what they are: realised, estimated, or unknown.

Why this matters economically: the only way to learn whether the frozen rule has
positive NET expectancy is the trades table. Before this change every fee leg
was a bare float -- ``close_position`` booked 0.0 unless the caller invented a
number, ``observe_exits`` booked an exit fee of 0.0, the entry fee on a
partial exit was a static estimate stored in the same column as a real one, and
the venue's ``cumExecFee`` (BTC on a spot buy, USDT on the sell) was read by
nobody. A net PnL built from that cannot distinguish "the venue charged
nothing" from "nobody looked", so it cannot support an expectancy claim.

These tests run the REAL engine and store against the offline fake exchange.
"""
from __future__ import annotations

import os
import sqlite3
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import bybit_connection as bc  # noqa: E402
import trading_engine as te  # noqa: E402
import venue_fees as vf  # noqa: E402
from fake_bybit import FakeBybit  # noqa: E402
from persistence import IncompatibleFeeCurrencies, StateStore, TradeRecord  # noqa: E402
from position_sizing import BillionairePositionSizing  # noqa: E402
from risk_management import BillionaireRiskManager  # noqa: E402
from test_lifecycle_integration import (  # noqa: E402
    ENTRY, EQUITY, LiveCfg, buy_intent,
)

FILL_PRICE = 50_000.0


class FeeExchange(FakeBybit):
    """FakeBybit whose market fills carry venue fee fields.

    ``fee_for(side, qty)`` returns the ``(cumExecFee, feeCurrency)`` the venue
    would report, or ``None`` to omit the fields entirely.
    """

    def __init__(self, fee_for, **kw):
        super().__init__(**kw)
        self._fee_for = fee_for

    def _create_order(self, body):
        status, text = super()._create_order(body)
        row = next((r for r in self.orders.values()
                    if r["_body"] is body), None)
        if row is not None and row["orderStatus"] == "Filled":
            row["avgPrice"] = str(FILL_PRICE)
            fee = self._fee_for(body["side"], float(body["qty"]))
            if fee is not None:
                row["cumExecFee"], row["feeCurrency"] = fee
        return status, text


def spot_fee(side, qty):
    """The real spot shape: the buy is charged in BTC, the sell in USDT."""
    if side == "Buy":
        return (f"{qty * 0.001:.8f}", "BTC")
    return (f"{qty * FILL_PRICE * 0.001:.8f}", "USDT")


def _stack(tmp_path, fee_for):
    cfg = LiveCfg()
    exchange = FeeExchange(fee_for, balances={"USDT": 100_000.0, "BTC": 5.0},
                           equity=EQUITY)
    store = StateStore(str(tmp_path / "state.db"))
    client = bc.BybitClient(config=cfg, store=store, transport=exchange)
    risk = BillionaireRiskManager(config=cfg, store=store)
    sizer = BillionairePositionSizing(config=cfg, risk_manager=risk)
    engine = te.TradingEngine(client=client, risk_manager=risk,
                              position_sizer=sizer, store=store, config=cfg)
    store.update_equity(EQUITY)
    return engine, store, exchange


@pytest.fixture()
def stack(tmp_path):
    engine, store, exchange = _stack(tmp_path, spot_fee)
    yield engine, store, exchange
    store.close()


def _trade(store):
    rows = store._query("SELECT * FROM trades")
    assert len(rows) == 1
    return dict(rows[0])


class TestRealisedFeesFromTheVenue:
    def test_btc_entry_fee_and_usdt_exit_fee_are_converted_and_labelled(self, stack):
        engine, store, _ = stack
        report = engine.execute(buy_intent())
        assert report.ok and report.stage == "complete"
        qty = report.qty
        raw_btc = qty * 0.001

        assert engine.close_position(symbol="BTCUSDT", reason="t").ok
        t = _trade(store)
        import json
        fees = json.loads(t["meta"])["fees"]

        # entry: BTC amount x the entry leg's own trade price, basis recorded
        assert t["entry_fee_realized"] == 1 and t["entry_fee_currency"] == "USDT"
        assert t["entry_fee"] == pytest.approx(raw_btc * FILL_PRICE)
        assert fees["entry"]["venue_amount"] == pytest.approx(raw_btc)
        assert fees["entry"]["venue_currency"] == "BTC"
        assert fees["entry"]["status"] == "converted"
        assert str(FILL_PRICE) in fees["entry"]["basis"]
        # exit: already USDT, identity
        assert t["exit_fee_realized"] == 1 and t["exit_fee_currency"] == "USDT"
        assert t["exit_fee"] == pytest.approx(qty * FILL_PRICE * 0.001)
        assert fees["exit"]["status"] == "identity"
        # net PnL is gross less both legs in ONE unit
        assert t["net_pnl"] == pytest.approx(
            t["gross_pnl"] - t["entry_fee"] - t["exit_fee"])
        ev = store.fee_evidence()
        assert ev["fees_realized_trades"] == 1 and ev["fees_unconfirmed_trades"] == 0
        assert ev["realized_net_pnl"] == pytest.approx(t["net_pnl"])

    def test_reported_zero_fee_is_realised_and_distinct_from_unknown(self, tmp_path):
        engine, store, _ = _stack(tmp_path, lambda s, q: ("0", "USDT"))
        assert engine.execute(buy_intent()).ok
        engine.close_position(symbol="BTCUSDT", reason="t")
        t = _trade(store)
        assert t["entry_fee"] == 0.0 and t["exit_fee"] == 0.0
        assert t["entry_fee_realized"] == 1 and t["exit_fee_realized"] == 1
        store.close()


class TestUnknownFeesAreNeverPassedOffAsRealised:
    def test_absent_venue_fee_is_an_estimate_not_zero_and_not_realised(self, tmp_path):
        engine, store, _ = _stack(tmp_path, lambda s, q: None)
        report = engine.execute(buy_intent())
        engine.close_position(symbol="BTCUSDT", reason="t")
        t = _trade(store)
        import json
        fees = json.loads(t["meta"])["fees"]
        for leg in ("entry", "exit"):
            assert t[f"{leg}_fee_realized"] == 0
            assert t[f"{leg}_fee_currency"] is None
            assert fees[leg]["status"] == "unknown"
            assert fees[leg]["substitute"] == "static_estimate"
            assert fees[leg]["venue_amount"] is None
            assert t[f"{leg}_fee"] > 0.0   # an estimate, not a silent 0.0
        ev = store.fee_evidence()
        assert ev["fees_realized_trades"] == 0 and ev["fees_unconfirmed_trades"] == 1
        assert ev["realized_net_pnl"] == 0.0
        assert ev["unconfirmed_net_pnl"] == pytest.approx(t["net_pnl"])
        assert report.ok
        store.close()

    def test_unconvertible_currency_keeps_the_raw_fact_and_claims_nothing(self, tmp_path):
        engine, store, _ = _stack(tmp_path, lambda s, q: ("3.5", "XYZ"))
        engine.execute(buy_intent())
        engine.close_position(symbol="BTCUSDT", reason="t")
        t = _trade(store)
        import json
        entry = json.loads(t["meta"])["fees"]["entry"]
        assert t["entry_fee_realized"] == 0
        assert entry["status"] == "unresolved"
        assert entry["venue_amount"] == 3.5 and entry["venue_currency"] == "XYZ"
        assert t["entry_fee"] != 3.5      # the raw XYZ number is not booked as USDT
        store.close()

    def test_caller_supplied_fee_is_kept_but_never_marked_realised(self, tmp_path):
        engine, store, _ = _stack(tmp_path, lambda s, q: None)
        engine.execute(buy_intent())
        engine.close_position(symbol="BTCUSDT", reason="t",
                              entry_fee=1.0, exit_fee=2.0)
        t = _trade(store)
        assert (t["entry_fee"], t["exit_fee"]) == (1.0, 2.0)
        assert t["entry_fee_realized"] == 0 and t["exit_fee_realized"] == 0
        store.close()


class TestEntryFeeSurvivesPartialExits:
    def test_two_part_exit_books_exactly_one_entry_fee(self, stack):
        engine, store, _ = stack
        report = engine.execute(buy_intent())
        qty = report.qty
        raw_total = qty * 0.001            # BTC, as the venue reported it

        first = qty * 0.4
        assert engine.record_exit_fill(
            symbol="BTCUSDT", qty=first, price=FILL_PRICE, fee=0.0,
            reason="tp1", dedupe_key="k1",
            fee_fact=vf.VenueFee(amount=1.0, currency="USDT")) is False
        row = [p for p in store.open_positions() if p["symbol"] == "BTCUSDT"][0]
        import json
        meta = json.loads(row["meta"])
        assert meta["entry_fee"] == pytest.approx(raw_total * 0.6)
        assert meta["entry_fee_currency"] == "BTC"

        assert engine.record_exit_fill(
            symbol="BTCUSDT", qty=qty - first, price=FILL_PRICE, fee=0.0,
            reason="tp2", dedupe_key="k2",
            fee_fact=vf.VenueFee(amount=2.0, currency="USDT")) is True
        rows = store._query("SELECT * FROM trades ORDER BY id")
        assert len(rows) == 2
        assert all(r["entry_fee_realized"] == 1 and r["exit_fee_realized"] == 1
                   for r in rows)
        assert sum(r["entry_fee"] for r in rows) == pytest.approx(
            raw_total * FILL_PRICE)
        assert [r["exit_fee"] for r in rows] == [1.0, 2.0]


class TestObservedExchangeSideExits:
    def test_stop_fill_without_a_venue_fee_is_not_booked_as_a_zero_fee(self, tmp_path):
        import time
        from test_observe_exits import LinearCfg
        cfg = LinearCfg()
        fake = FakeBybit()
        store = StateStore(str(tmp_path / "obs.db"))
        store.claim_writer()
        store.update_equity(10_000.0)
        client = bc.BybitClient(config=cfg, store=store, transport=fake)
        risk = BillionaireRiskManager(config=cfg, store=store)
        engine = te.TradingEngine(
            client=client, risk_manager=risk,
            position_sizer=BillionairePositionSizing(config=cfg, risk_manager=risk),
            store=store, config=cfg)
        store.upsert_position("BTCUSDT", "Sell", 0.01, 60_000.0, stop_price=61_000.0)
        fake.closed_pnl.append({
            "symbol": "BTCUSDT", "side": "Buy", "qty": "0.01",
            "avgExitPrice": "61000", "closedPnl": "-10.6",
            "updatedTime": str(int(time.time() * 1000) + 1000)})
        assert engine.observe_exits()["closed"] == 1
        t = _trade(store)
        assert t["exit_fee_realized"] == 0 and t["exit_fee"] == pytest.approx(0.61)
        assert t["entry_fee_realized"] == 0     # no venue entry fee on record
        assert store.fee_evidence()["fees_unconfirmed_trades"] == 1
        store.close()


class TestStorageContract:
    def test_legacy_database_migrates_additively_and_old_rows_are_not_realised(
            self, tmp_path):
        path = str(tmp_path / "legacy.db")
        conn = sqlite3.connect(path)
        conn.execute(
            "CREATE TABLE trades (id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT"
            " NOT NULL, side TEXT NOT NULL, qty REAL NOT NULL, entry_price REAL NOT"
            " NULL, exit_price REAL NOT NULL, gross_pnl REAL NOT NULL, entry_fee REAL"
            " NOT NULL, exit_fee REAL NOT NULL, net_pnl REAL NOT NULL, opened_epoch"
            " REAL NOT NULL, closed_epoch REAL NOT NULL, day TEXT NOT NULL,"
            " order_link_id TEXT NOT NULL DEFAULT '', meta TEXT NOT NULL DEFAULT '{}')")
        conn.execute(
            "INSERT INTO trades(symbol,side,qty,entry_price,exit_price,gross_pnl,"
            "entry_fee,exit_fee,net_pnl,opened_epoch,closed_epoch,day) VALUES"
            "('BTCUSDT','Buy',1,100,110,10,0.1,0.1,9.8,1,2,'1970-01-01')")
        conn.commit()
        conn.close()

        store = StateStore(path)
        try:
            ev = store.fee_evidence()
            assert ev["trades"] == 1
            assert ev["fees_realized_trades"] == 0          # provenance never recorded
            assert ev["unconfirmed_net_pnl"] == pytest.approx(9.8)
            row = store.recent_trades()[0]
            assert row["net_pnl"] == pytest.approx(9.8)     # nothing rewritten
        finally:
            store.close()

    def test_record_that_would_add_btc_to_usdt_refuses(self):
        t = TradeRecord(symbol="BTCUSDT", side="Buy", qty=1, entry_price=1,
                        exit_price=1, gross_pnl=0, entry_fee=0.001, exit_fee=5.0,
                        opened_epoch=1, closed_epoch=2,
                        entry_fee_currency="BTC", exit_fee_currency="USDT")
        with pytest.raises(IncompatibleFeeCurrencies):
            _ = t.net_pnl

    def test_trade_record_defaults_to_not_realised(self):
        t = TradeRecord(symbol="BTCUSDT", side="Buy", qty=1, entry_price=1,
                        exit_price=1, gross_pnl=0, entry_fee=0.0, exit_fee=0.0,
                        opened_epoch=1, closed_epoch=2)
        assert not t.fees_realized


class TestVenueFeeRules:
    @pytest.mark.parametrize("row", [None, {}, {"cumExecFee": None},
                                     {"cumExecFee": ""}, {"cumExecFee": "x"},
                                     {"cumExecFee": "nan"}, {"cumExecFee": "inf"}])
    def test_unusable_fee_is_unknown_never_zero(self, row):
        assert vf.extract_fee(row).known is False

    def test_zero_is_a_known_fee_and_currency_is_never_guessed(self):
        fee = vf.extract_fee({"cumExecFee": "0"})
        assert fee.known and fee.amount == 0.0 and fee.currency is None
        assert vf.extract_fee({"cumExecFee": "1", "feeCurrency": "bad cur!"}
                              ).currency is None

    def test_conversion_refuses_to_guess(self):
        kw = dict(leg_price=100.0, qty=1.0, taker_fee=0.001,
                  accounting_unit="USDT", base_coin="BTC")
        assert vf.convert_to_account_unit(
            amount=0.5, currency=None, known=True, **kw).status == "unresolved"
        assert vf.convert_to_account_unit(
            amount=0.5, currency="ETH", known=True, **kw).known is False
        est = vf.convert_to_account_unit(
            amount=None, currency=None, known=False, **kw)
        assert est.status == "unknown" and not est.known
        assert est.account_unit_amount == pytest.approx(0.1)
