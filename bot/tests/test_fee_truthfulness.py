"""Fees on booked trades say what they are: confirmed, estimated, or unknown.

Why this matters economically: the only way to learn whether the frozen rule has
positive NET expectancy is the trades table, so every fee on it must carry its
provenance. The venue contract (Bybit v5 docs) is that, for linear and spot,
``cumFeeDetail`` -- a ``{coin: amount}`` map -- REPLACES the deprecated
``cumExecFee``/``feeCurrency``. The docs' own example is a linear order with
``cumExecFee: "0"`` beside a non-empty ``cumFeeDetail``: a parser that reads the
legacy field books a confident, wrong zero.

These tests run the REAL engine and store against the offline fake exchange,
with order rows shaped as the documentation describes them. They are fixtures of
the documented contract, not captures of live payloads.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import time

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import bybit_connection as bc  # noqa: E402
import trading_engine as te  # noqa: E402
import venue_fees as vf  # noqa: E402
from fake_bybit import FakeBybit  # noqa: E402
from performance_analytics import PerformanceAnalytics  # noqa: E402
from persistence import IncompatibleFeeCurrencies, StateStore, TradeRecord  # noqa: E402
from position_sizing import BillionairePositionSizing  # noqa: E402
from risk_management import BillionaireRiskManager  # noqa: E402
from test_lifecycle_integration import (  # noqa: E402
    EQUITY, LiveCfg, buy_intent,
)

FILL_PRICE = 50_000.0


class FeeExchange(FakeBybit):
    """FakeBybit whose market fills carry the venue fee fields.

    ``fee_for(side, qty)`` returns a dict of extra order-row fields (for example
    ``{"cumFeeDetail": {"BTC": "0.00002"}}``) or ``None`` to add nothing.
    """

    def __init__(self, fee_for, **kw):
        super().__init__(**kw)
        self._fee_for = fee_for

    def _create_order(self, body):
        status, text = super()._create_order(body)
        row = next((r for r in self.orders.values() if r["_body"] is body), None)
        if row is not None and row["orderStatus"] == "Filled":
            row["avgPrice"] = str(FILL_PRICE)
            extra = self._fee_for(body["side"], float(body["qty"]))
            if extra:
                row.update(extra)
        return status, text


def spot_detail(side, qty):
    """The real spot shape: the buy is charged in BTC, the sell in USDT."""
    if side == "Buy":
        return {"cumFeeDetail": {"BTC": f"{qty * 0.001:.8f}"}}
    return {"cumFeeDetail": {"USDT": f"{qty * FILL_PRICE * 0.001:.8f}"}}


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
    engine, store, exchange = _stack(tmp_path, spot_detail)
    yield engine, store, exchange
    store.close()


def _trade(store):
    rows = store._query("SELECT * FROM trades")
    assert len(rows) == 1
    return dict(rows[0])


def _fees(t):
    return json.loads(t["meta"])["fees"]


def _gross(held_qty):
    """The quantity BOUGHT. On spot the buy's 0.1% fee is taken in the base
    coin, so the account (and the position, stop and exit) hold 0.999 of it."""
    return held_qty / (1.0 - 0.001)


def _round_trip(tmp_path, fee_for):
    engine, store, _ = _stack(tmp_path, fee_for)
    report = engine.execute(buy_intent())
    assert report.ok, report
    assert engine.close_position(symbol="BTCUSDT", reason="t").ok
    t = _trade(store)
    return store, t, report.qty


class TestModernCumFeeDetail:
    def test_btc_entry_and_usdt_exit_are_converted_labelled_and_summed_in_one_unit(
            self, tmp_path):
        store, t, qty = _round_trip(tmp_path, spot_detail)
        f = _fees(t)
        assert t["entry_fee_realized"] == 1 and t["entry_fee_currency"] == "USDT"
        assert t["entry_fee"] == pytest.approx(_gross(qty) * 0.001 * FILL_PRICE)
        part = f["entry"]["parts"][0]
        assert (part["venue_amount"], part["venue_currency"]) == (
            pytest.approx(_gross(qty) * 0.001), "BTC")
        assert part["status"] == "converted" and part["rate"] == FILL_PRICE
        assert f["entry"]["source"] == "cumFeeDetail"
        assert f["entry"]["ref"]["order_link_id"]          # fill association
        assert t["exit_fee_realized"] == 1
        assert t["exit_fee"] == pytest.approx(qty * FILL_PRICE * 0.001)
        assert f["exit"]["parts"][0]["status"] == "identity"
        assert t["net_pnl"] == pytest.approx(
            t["gross_pnl"] - t["entry_fee"] - t["exit_fee"])
        ev = store.fee_evidence()
        assert (ev["fees_realized_trades"], ev["fees_unconfirmed_trades"]) == (1, 0)
        store.close()

    def test_detail_wins_over_a_deceptive_legacy_zero(self, tmp_path):
        """The docs' own linear example: cumExecFee "0" beside a real detail."""
        def fee_for(side, qty):
            d = spot_detail(side, qty)
            d["cumExecFee"] = "0"
            return d
        store, t, qty = _round_trip(tmp_path, fee_for)
        assert t["entry_fee"] == pytest.approx(_gross(qty) * 0.001 * FILL_PRICE)
        assert t["entry_fee"] > 0
        assert t["exit_fee"] > 0
        assert t["entry_fee_realized"] == 1 and t["exit_fee_realized"] == 1
        store.close()

    def test_legacy_fields_still_work_when_the_detail_map_is_absent(self, tmp_path):
        def fee_for(side, qty):
            if side == "Buy":
                return {"cumExecFee": f"{qty * 0.001:.8f}", "feeCurrency": "BTC"}
            return {"cumExecFee": f"{qty * FILL_PRICE * 0.001:.8f}",
                    "feeCurrency": "USDT"}
        store, t, qty = _round_trip(tmp_path, fee_for)
        assert t["entry_fee_realized"] == 1 and t["exit_fee_realized"] == 1
        assert _fees(t)["entry"]["source"] == "cumExecFee"
        assert t["entry_fee"] == pytest.approx(_gross(qty) * 0.001 * FILL_PRICE)
        store.close()

    def test_reported_zero_is_confirmed_and_distinct_from_unknown(self, tmp_path):
        store, t, _ = _round_trip(
            tmp_path, lambda s, q: {"cumFeeDetail": {"USDT": "0"}})
        assert t["entry_fee"] == 0.0 and t["exit_fee"] == 0.0
        assert t["entry_fee_realized"] == 1 and t["exit_fee_realized"] == 1
        store.close()

    @pytest.mark.parametrize("extra", [
        None,                                   # nothing reported
        {"cumFeeDetail": {}},                   # empty map: cannot tell zero
        {"cumFeeDetail": {"BTC": "oops"}},      # malformed amount
        {"cumFeeDetail": "not json"},           # malformed container
        {"cumFeeDetail": {"BTC": "nan"}},       # non-finite
    ])
    def test_unknown_is_an_estimate_not_zero_and_never_confirmed(
            self, tmp_path, extra):
        store, t, _ = _round_trip(tmp_path, lambda s, q: extra)
        for leg in ("entry", "exit"):
            assert t[f"{leg}_fee_realized"] == 0
            assert t[f"{leg}_fee_currency"] is None
            assert _fees(t)[leg]["status"] == "unknown"
            assert _fees(t)[leg]["substitute"] == "static_estimate"
            assert t[f"{leg}_fee"] > 0.0
        ev = store.fee_evidence()
        assert ev["fees_realized_trades"] == 0 and ev["realized_net_pnl"] == 0.0
        assert ev["unconfirmed_net_pnl"] == pytest.approx(t["net_pnl"])
        store.close()

    def test_two_currencies_both_convertible_are_summed_only_after_conversion(
            self, tmp_path):
        def fee_for(side, qty):
            if side == "Buy":
                return {"cumFeeDetail": {"BTC": "0.00001", "USDT": "0.25"}}
            return spot_detail(side, qty)
        store, t, _ = _round_trip(tmp_path, fee_for)
        assert t["entry_fee_realized"] == 1
        assert t["entry_fee"] == pytest.approx(0.00001 * FILL_PRICE + 0.25)
        assert len(_fees(t)["entry"]["parts"]) == 2
        store.close()

    def test_a_fee_in_an_unconvertible_coin_claims_nothing_and_keeps_the_raw_fact(
            self, tmp_path):
        def fee_for(side, qty):
            if side == "Buy":
                return {"cumFeeDetail": {"BTC": "0.00001", "MNT": "3.5"}}
            return spot_detail(side, qty)
        store, t, _ = _round_trip(tmp_path, fee_for)
        entry = _fees(t)["entry"]
        assert t["entry_fee_realized"] == 0 and entry["status"] == "unresolved"
        raw = {(p["venue_currency"], p["venue_amount"]) for p in entry["parts"]}
        assert ("MNT", 3.5) in raw and ("BTC", 0.00001) in raw
        assert t["entry_fee"] != 3.5            # raw MNT not booked as USDT
        assert t["exit_fee_realized"] == 1       # the other leg stays confirmed
        store.close()

    def test_caller_supplied_fee_is_kept_but_never_marked_confirmed(self, tmp_path):
        engine, store, _ = _stack(tmp_path, lambda s, q: None)
        engine.execute(buy_intent())
        engine.close_position(symbol="BTCUSDT", reason="t",
                              entry_fee=1.0, exit_fee=2.0)
        t = _trade(store)
        assert (t["entry_fee"], t["exit_fee"]) == (1.0, 2.0)
        assert t["entry_fee_realized"] == 0 and t["exit_fee_realized"] == 0
        store.close()


class TestEntryFeeSurvivesPartialExits:
    def test_two_part_exit_books_exactly_one_entry_fee_in_every_currency(
            self, tmp_path):
        def fee_for(side, qty):
            if side == "Buy":
                return {"cumFeeDetail": {"BTC": "0.00010", "USDT": "1.00"}}
            return spot_detail(side, qty)
        engine, store, _ = _stack(tmp_path, fee_for)
        qty = engine.execute(buy_intent()).qty

        first = qty * 0.4
        assert engine.record_exit_fill(
            symbol="BTCUSDT", qty=first, price=FILL_PRICE, fee=0.0,
            reason="tp1", dedupe_key="k1",
            fees=vf.FeeSet((vf.VenueFee(1.0, "USDT"),), True, "cumFeeDetail"),
            fee_ref={"order_ids": ["A"]}) is False
        row = [p for p in store.open_positions() if p["symbol"] == "BTCUSDT"][0]
        carried = vf.FeeSet.from_dict(json.loads(row["meta"])["entry_fees"])
        amounts = {c.currency: c.amount for c in carried.components}
        assert amounts["BTC"] == pytest.approx(0.00010 * 0.6)
        assert amounts["USDT"] == pytest.approx(0.6)

        assert engine.record_exit_fill(
            symbol="BTCUSDT", qty=qty - first, price=FILL_PRICE, fee=0.0,
            reason="tp2", dedupe_key="k2",
            fees=vf.FeeSet((vf.VenueFee(2.0, "USDT"),), True, "cumFeeDetail")
        ) is True
        rows = store._query("SELECT * FROM trades ORDER BY id")
        assert len(rows) == 2
        assert all(r["entry_fee_realized"] == 1 and r["exit_fee_realized"] == 1
                   for r in rows)
        assert sum(r["entry_fee"] for r in rows) == pytest.approx(
            0.00010 * FILL_PRICE + 1.0)
        assert [r["exit_fee"] for r in rows] == [1.0, 2.0]
        store.close()

    def test_replaying_a_booked_exit_does_not_double_book_fees_or_anchor(
            self, tmp_path):
        engine, store, _ = _stack(tmp_path, spot_detail)
        qty = engine.execute(buy_intent()).qty
        kw = dict(symbol="BTCUSDT", qty=qty * 0.4, price=FILL_PRICE, fee=0.0,
                  reason="tp1", dedupe_key="same",
                  fees=vf.FeeSet((vf.VenueFee(1.0, "USDT"),), True, "cumFeeDetail"))
        engine.record_exit_fill(**kw)
        before = store.fee_evidence()
        # A crash after record_trade but before the position mutation leaves the
        # position row at its pre-exit quantity; model it by restoring it.
        store.upsert_position("BTCUSDT", "Buy", qty, FILL_PRICE, stop_price=1.0,
                              meta={"entry_fees": spot_detail("Buy", qty)
                                    and vf.extract_order_fees(
                                        spot_detail("Buy", qty)).to_dict()})
        engine.record_exit_fill(**kw)
        after = store.fee_evidence()
        assert after["trades"] == before["trades"] == 1
        store.close()


class TestEmergencyCloseReachesTheLedger:
    def test_a_bracket_failure_books_the_trade_with_both_venue_fees(self, tmp_path):
        engine, store, exchange = _stack(tmp_path, spot_detail)
        real_create = exchange._create_order

        def reject_stops(body):
            if body.get("orderFilter") == "StopOrder":
                return exchange._err(10001, "stop rejected by exchange")
            return real_create(body)

        exchange._create_order = reject_stops
        report = engine.execute(buy_intent())
        assert report.stage == "emergency" and report.reason == "BRACKET_FAILED"
        assert store.open_positions() == []
        # Before this change the position row was deleted and NOTHING was booked.
        t = _trade(store)
        assert t["entry_fee_realized"] == 1 and t["exit_fee_realized"] == 1
        assert t["entry_fee"] > 0 and t["exit_fee"] > 0
        meta = json.loads(t["meta"])
        assert meta["reason"].startswith("EMERGENCY:BRACKET_FAILED")
        assert meta["exit_fill_confirmed"] is True
        assert t["order_link_id"]                      # stable exit identity
        store.close()


class TestAnEmergencyFlattenIsNeverDerailedByAccounting:
    def _fail_bracket(self, exchange):
        real_create = exchange._create_order

        def reject_stops(body):
            if body.get("orderFilter") == "StopOrder":
                return exchange._err(10001, "stop rejected by exchange")
            return real_create(body)

        exchange._create_order = reject_stops

    def test_an_unreadable_exit_fill_is_booked_as_unconfirmed(self, tmp_path):
        engine, store, exchange = _stack(tmp_path, spot_detail)
        self._fail_bracket(exchange)

        real_await, calls = engine._await_fill, []

        def flaky(symbol, link, timeout_s=10.0):
            calls.append(link)
            if len(calls) == 1:                 # the entry fill resolves
                return real_await(symbol, link, timeout_s)
            raise RuntimeError("fill poll exploded")   # the emergency exit's
        engine._await_fill = flaky
        report = engine.execute(buy_intent())
        assert report.stage == "emergency"
        assert store.open_positions() == []
        assert store.is_kill_switch_engaged()[0] is True
        t = _trade(store)
        assert json.loads(t["meta"])["exit_fill_confirmed"] is False
        assert t["exit_fee_realized"] == 0           # no fill, no confirmed fee
        store.close()

    def test_a_booking_failure_still_removes_the_position_and_is_journaled(
            self, tmp_path):
        engine, store, exchange = _stack(tmp_path, spot_detail)
        self._fail_bracket(exchange)

        def broken(**kw):
            raise RuntimeError("ledger write failed")
        engine._book_full_exit = broken
        report = engine.execute(buy_intent())
        assert report.stage == "emergency" and report.detail["closed"] is True
        assert store.open_positions() == []
        assert store.is_kill_switch_engaged()[0] is True
        assert store._query("SELECT * FROM trades") == []
        assert any(r["decision"] == "EMERGENCY_UNBOOKED"
                   for r in store._query("SELECT * FROM decisions"))
        store.close()


def _linear_engine(tmp_path):
    from test_observe_exits import LinearCfg
    cfg, fake = LinearCfg(), FakeBybit()
    store = StateStore(str(tmp_path / "obs.db"))
    store.claim_writer()
    store.update_equity(10_000.0)
    client = bc.BybitClient(config=cfg, store=store, transport=fake)
    risk = BillionaireRiskManager(config=cfg, store=store)
    engine = te.TradingEngine(
        client=client, risk_manager=risk,
        position_sizer=BillionairePositionSizing(config=cfg, risk_manager=risk),
        store=store, config=cfg)
    return engine, store, fake


def _venue_close_order(fake, oid="V-CLOSE-1", qty="0.01", detail=None):
    fake.orders[oid] = {
        "orderId": oid, "orderLinkId": f"link-{oid}", "symbol": "BTCUSDT",
        "side": "Buy", "orderType": "Market", "qty": qty, "price": "",
        "orderStatus": "Filled", "orderFilter": "StopOrder",
        "triggerPrice": "61000", "cumExecQty": qty, "avgPrice": "61000",
        "cumFeeDetail": {"USDT": "0.366"} if detail is None else detail,
        "_purpose": "stop", "_body": {}}


def _open_short(store, with_entry_fee=True):
    meta = {}
    if with_entry_fee:
        meta = {"entry_fees": vf.FeeSet(
            (vf.VenueFee(0.33, "USDT"),), True, "cumFeeDetail").to_dict(),
            "entry_fee_ref": {"order_link_id": "entry-1"}}
    store.upsert_position("BTCUSDT", "Sell", 0.01, 60_000.0,
                          stop_price=61_000.0, meta=meta)


def _closed_pnl(fake, oid="V-CLOSE-1", qty="0.01"):
    fake.closed_pnl.append({
        "symbol": "BTCUSDT", "side": "Buy", "qty": qty, "orderId": oid,
        "avgExitPrice": "61000", "closedPnl": "-10.6", "openFee": "0.33",
        "closeFee": "0.366", "updatedTime": str(int(time.time() * 1000) + 1000)})


class TestExchangeObservedExits:
    def test_stop_fill_books_the_closing_orders_own_cumFeeDetail(self, tmp_path):
        engine, store, fake = _linear_engine(tmp_path)
        _open_short(store)
        _venue_close_order(fake)
        _closed_pnl(fake)
        assert engine.observe_exits()["closed"] == 1
        t = _trade(store)
        assert t["exit_fee_realized"] == 1 and t["exit_fee"] == pytest.approx(0.366)
        assert t["entry_fee_realized"] == 1 and t["entry_fee"] == pytest.approx(0.33)
        meta = json.loads(t["meta"])
        assert meta["fees"]["exit"]["ref"]["order_ids"] == ["V-CLOSE-1"]
        # closed-pnl openFee/closeFee are kept as evidence, never as the fee
        ev = meta["fee_evidence"]["closed_pnl"][0]
        assert (ev["openFee"], ev["closeFee"]) == ("0.33", "0.366")
        store.close()

    @pytest.mark.parametrize("break_it", [
        "no_order", "qty_mismatch", "empty_detail", "page_full", "no_order_id"])
    def test_fee_is_unattributed_unless_the_evidence_closes_exactly(
            self, tmp_path, break_it):
        engine, store, fake = _linear_engine(tmp_path)
        _open_short(store)
        if break_it != "no_order":
            _venue_close_order(
                fake, detail={} if break_it == "empty_detail" else None,
                qty="0.02" if break_it == "qty_mismatch" else "0.01")
        _closed_pnl(fake)
        if break_it == "page_full":
            for i in range(49):
                _closed_pnl(fake, oid=f"X{i}", qty="0")
        if break_it == "no_order_id":
            fake.closed_pnl[0]["orderId"] = ""
        assert engine.observe_exits()["closed"] == 1
        t = _trade(store)
        assert t["exit_fee_realized"] == 0           # estimate, flagged
        assert t["exit_fee"] > 0.0
        store.close()

    def test_position_without_a_recorded_entry_fee_is_not_confirmed(self, tmp_path):
        engine, store, fake = _linear_engine(tmp_path)
        _open_short(store, with_entry_fee=False)
        _venue_close_order(fake)
        _closed_pnl(fake)
        engine.observe_exits()
        t = _trade(store)
        assert t["exit_fee_realized"] == 1 and t["entry_fee_realized"] == 0
        assert store.fee_evidence()["fees_unconfirmed_trades"] == 1
        store.close()


class TestStorageAndReports:
    def test_legacy_database_migrates_additively_and_old_rows_are_not_confirmed(
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
            assert (ev["trades"], ev["fees_realized_trades"]) == (1, 0)
            assert ev["unconfirmed_net_pnl"] == pytest.approx(9.8)
            assert store.recent_trades()[0]["net_pnl"] == pytest.approx(9.8)
            rep = PerformanceAnalytics(store).report()
            assert rep.fees_confirmed is False
            assert "PROVISIONAL" in rep.summary()
        finally:
            store.close()

    def test_a_report_over_confirmed_trades_is_not_marked_provisional(self, tmp_path):
        store, t, _ = _round_trip(tmp_path, spot_detail)
        rep = PerformanceAnalytics(store).report()
        assert rep.fees_confirmed and rep.fee_confirmed_trades == 1
        assert "PROVISIONAL" not in rep.summary()
        store.close()

    def test_record_that_would_add_btc_to_usdt_refuses(self):
        t = TradeRecord(symbol="BTCUSDT", side="Buy", qty=1, entry_price=1,
                        exit_price=1, gross_pnl=0, entry_fee=0.001, exit_fee=5.0,
                        opened_epoch=1, closed_epoch=2,
                        entry_fee_currency="BTC", exit_fee_currency="USDT")
        with pytest.raises(IncompatibleFeeCurrencies):
            _ = t.net_pnl


class TestFeeSetRules:
    def test_extraction_matrix(self):
        ex = vf.extract_order_fees
        assert not ex(None).known and not ex({}).known
        assert not ex({"cumExecFee": ""}).known
        z = ex({"cumExecFee": "0"})
        assert z.known and z.single_amount == 0.0           # zero needs no coin
        d = ex({"cumFeeDetail": {"btc": "0.1", "MNT": "2"}})
        assert d.known and d.source == "cumFeeDetail" and d.single_amount is None
        assert ex({"cumFeeDetail": '{"USDT": "1.5"}'}).single_amount == 1.5
        assert not ex({"cumFeeDetail": {"BTC bad": "1"}}).known
        assert ex({"cumExecFee": "1", "feeCurrency": "bad cur!"}
                  ).components[0].currency is None

    def test_arithmetic_never_invents_a_number(self):
        a = vf.extract_order_fees({"cumFeeDetail": {"BTC": "0.4", "USDT": "2"}})
        b = vf.extract_order_fees({"cumFeeDetail": {"BTC": "0.1"}})
        diff = {c.currency: c.amount for c in a.minus(b).components}
        assert diff == {"BTC": pytest.approx(0.3), "USDT": 2.0}
        assert not a.minus(vf.UNKNOWN_FEES).known
        assert not a.plus(vf.UNKNOWN_FEES).known           # half a total is unknown

    def test_conversion_refuses_to_guess(self):
        kw = dict(leg_price=100.0, qty=1.0, accounting_unit="USDT",
                  base_coin="BTC")
        nameless = vf.FeeSet((vf.VenueFee(0.5, None),), True, "cumExecFee")
        assert vf.convert_fee_set(nameless, **kw).status == "unresolved"
        assert vf.convert_fee_set(vf.UNKNOWN_FEES, **kw).status == "unknown"
        assert vf.convert_fee_set(vf.UNKNOWN_FEES, **kw).account_unit_amount is None
        ok = vf.convert_fee_set(vf.FeeSet(
            (vf.VenueFee(0.5, "BTC"),), True, "cumFeeDetail"), **kw)
        assert ok.confirmed and ok.account_unit_amount == 50.0


class TestASpotBuyIsProtectedForWhatIsActuallyHeld:
    def test_the_stop_and_legs_are_sized_net_of_the_base_coin_fee(self, tmp_path):
        engine, store, _ = _stack(tmp_path, spot_detail)
        report = engine.execute(buy_intent())
        assert report.ok, report
        row = store.open_positions()[0]
        held = float(row["qty"])
        buys = [o for o in store._query("SELECT * FROM orders WHERE purpose='entry'")]
        bought = float(buys[0]["qty"])
        assert held < bought, "the position still claims the gross fill"
        assert held == pytest.approx(bought * (1 - 0.001), abs=1e-5)
        protective = store._query(
            "SELECT * FROM orders WHERE purpose IN ('stop','tp')")
        assert protective
        assert max(float(o["qty"]) for o in protective) <= held + 1e-9
        stop = [o for o in protective if o["purpose"] == "stop"][0]
        assert float(stop["qty"]) == pytest.approx(held, abs=1e-9)
        assert sum(float(o["qty"]) for o in protective if o["purpose"] == "tp") \
            == pytest.approx(held, abs=1e-5)
        store.close()

    def test_a_fill_that_leaves_nothing_sellable_is_booked_as_dust(self, tmp_path):
        def eaten(side, qty):
            return ({"cumFeeDetail": {"BTC": f"{qty:.8f}"}} if side == "Buy"
                    else {"cumFeeDetail": {"USDT": "0"}})
        engine, store, _ = _stack(tmp_path, eaten)
        report = engine.execute(buy_intent())
        assert report.ok is False and report.reason == "ZERO_SELLABLE"
        assert store.open_positions() == []
        assert store._query("SELECT * FROM decisions WHERE decision = ?",
                            ("ENTRY_DUST",))
        store.close()


def test_a_stated_fee_without_a_currency_is_the_base_coin_on_a_spot_buy():
    import venue_fees
    fees = venue_fees.extract_order_fees({"cumExecFee": "0.002"})
    assert fees.known
    assert venue_fees.spot_buy_net_qty("BTCUSDT", 2.0, fees) == pytest.approx(1.998)
    assert venue_fees.spot_buy_net_qty(
        "BTCUSDT", 2.0, venue_fees.UNKNOWN_FEES) == pytest.approx(2.0 * 0.998)


def test_a_rebate_never_makes_a_spot_buy_hold_more_than_it_filled():
    import venue_fees
    fees = venue_fees.extract_order_fees({"cumFeeDetail": {"BTC": "-0.001"}})
    assert venue_fees.spot_buy_net_qty("BTCUSDT", 2.0, fees) == pytest.approx(2.0)


class TestOrderLifecycleStaysConsistent:
    def test_cancelling_an_entry_does_not_write_a_terminal_status(self, tmp_path):
        """"Order not found" also means "already filled": only the venue's own
        record may settle an entry, after what it executed is in the book."""
        engine, store, _ = _stack(tmp_path, spot_detail)
        store.record_order("EC1", "BTCUSDT", "Buy", "Market", 0.001, purpose="entry")
        store.update_order_status("EC1", "submitted")
        assert engine.client.cancel_order(symbol="BTCUSDT", order_link_id="EC1")
        assert store.get_order("EC1")["status"] == "submitted"
        store.record_order("TP1", "BTCUSDT", "Sell", "Limit", 0.001, purpose="tp")
        store.update_order_status("TP1", "submitted")
        assert engine.client.cancel_order(symbol="BTCUSDT", order_link_id="TP1")
        assert store.get_order("TP1")["status"] == "cancelled"
        store.close()

    def test_a_failed_venue_cancel_leaves_the_protective_legs_open_locally(self, tmp_path):
        engine, store, _ = _stack(tmp_path, spot_detail)
        report = engine.execute(buy_intent())
        assert report.ok
        engine.client.cancel_all = lambda symbol=None: False
        engine._cancel_protective_orders("BTCUSDT")
        legs = store._query("SELECT * FROM orders WHERE purpose IN ('stop','tp')")
        assert legs and all(o["status"] != "cancelled" for o in legs)
        assert store._query("SELECT * FROM decisions WHERE decision = ?",
                            ("PROTECTIVE_CANCEL_FAILED",))
        store.close()

    def test_a_full_close_marks_its_protective_legs_cancelled(self, tmp_path):
        engine, store, _ = _stack(tmp_path, spot_detail)
        assert engine.execute(buy_intent()).ok
        assert engine.close_position(symbol="BTCUSDT", reason="t").ok
        legs = store._query("SELECT * FROM orders WHERE purpose IN ('stop','tp')")
        assert legs and all(o["status"] == "cancelled" for o in legs)
        store.close()

    def test_an_entry_is_marked_filled_only_once_its_position_exists(self, tmp_path):
        engine, store, _ = _stack(tmp_path, spot_detail)

        def boom(*a, **k):
            raise RuntimeError("db hiccup")
        store.upsert_position = boom
        with pytest.raises(RuntimeError):
            engine.execute(buy_intent())
        entry = store._query("SELECT * FROM orders WHERE purpose='entry'")[0]
        assert entry["status"] != "filled"
        assert entry["order_link_id"] in [o["order_link_id"]
                                         for o in store.unresolved_orders()]
        store.close()

    def test_a_spot_close_that_only_partly_sold_books_only_what_sold(self, tmp_path):
        engine, store, _ = _stack(tmp_path, spot_detail)
        assert engine.execute(buy_intent()).ok
        held = float(store.open_positions()[0]["qty"])
        engine._await_fill = lambda symbol, link, timeout_s=10.0: {
            "orderStatus": "PartiallyFilledCanceled",
            "cumExecQty": f"{held * 0.4:.6f}", "avgPrice": str(FILL_PRICE)}
        report = engine.close_position(symbol="BTCUSDT", reason="t")
        assert report.reason == "CLOSE_PARTIAL" and report.ok is False
        rows = store.open_positions()
        assert len(rows) == 1 and float(rows[0]["qty"]) == pytest.approx(held * 0.6, rel=1e-3)
        assert len(store._query("SELECT * FROM trades")) == 1
        store.close()


def test_a_close_does_not_floor_float_noise_to_one_lot_step_short(tmp_path):
    """A row quantity of 0.9470719999999999 (float noise on 0.947072) used to
    snap DOWN to 0.947071: a one-step dust remainder on every such close."""
    engine, store, _ = _stack(tmp_path, spot_detail)
    assert engine.execute(buy_intent()).ok
    row = store.open_positions()[0]
    store.upsert_position("BTCUSDT", "Buy", 0.9470719999999999, float(row["entry_price"]),
                          stop_price=float(row["stop_price"]),
                          order_link_id=str(row["order_link_id"]), meta=row["meta"] and
                          __import__("json").loads(row["meta"]))
    sent = []
    real = engine.client.place_order

    def spy(**kw):
        if kw.get("purpose") == "close":
            sent.append(kw["qty"])
        return real(**kw)
    engine.client.place_order = spy
    engine.close_position(symbol="BTCUSDT", reason="t")
    assert sent and sent[0] == 0.947072
    store.close()


class TestQuantitiesSurviveFloatNoiseAndLotSteps:
    def test_snap_ignores_float_noise_below_any_step(self):
        from decimal import Decimal
        import position_sizing as ps
        assert ps.snap_qty_down(0.3 - 0.0003, Decimal("0.0001")) == Decimal("0.2997")
        assert ps.snap_qty_down(0.7 - 0.42, Decimal("0.01")) == Decimal("0.28")
        assert ps.snap_qty_down(0.0099, Decimal("0.01")) == Decimal("0")

    def test_the_net_of_fee_quantity_is_noise_free(self):
        import venue_fees
        fees = venue_fees.extract_order_fees({"cumFeeDetail": {"BTC": "0.0003"}})
        assert venue_fees.spot_buy_net_qty("BTCUSDT", 0.3, fees) == 0.2997

    def test_take_profit_legs_are_snapped_before_the_residual_is_taken(self):
        """step 0.01, filled 0.19, fractions 0.4/0.3/0.3: snapping each leg only
        at the client strands 0.02; snapping first gives the last leg the floor
        losses. And 0.7 split 0.6/0.4 must not lose a step to float noise."""
        from decimal import Decimal
        import trading_engine as te
        step = Decimal("0.01")
        assert te.TradingEngine._allocate_legs(0.19, [0.4, 0.3, 0.3], step) \
            == [0.07, 0.05, 0.07]
        legs = te.TradingEngine._allocate_legs(0.7, [0.6, 0.4], step)
        assert legs == [0.42, 0.28]
        # legs that do not cover the position keep their own fractions
        assert te.TradingEngine._allocate_legs(1.0, [0.5, 0.2], step) == [0.5, 0.2]
