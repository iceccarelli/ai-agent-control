"""Carry fees: from the venue's order row to the double-entry statement.

THE DEFECTS THIS PINS
=====================
1. ``CarryBroker._fee`` read only ``cumExecFee``. For linear and spot Bybit has
   replaced it with ``cumFeeDetail`` (a ``{coin: amount}`` map); the docs' own
   example pairs ``cumExecFee: "0"`` with a non-empty detail, so the old reader
   books a confident zero.
2. The currency was dropped at the broker, and the ledger then ASSUMED BTC for a
   spot BUY and USD for everything else.
3. A fee the venue never stated was booked as 0.0 and tallied on a counter that
   lived on the Journal OBJECT: ``to_rows``/``from_rows`` did not carry it, so a
   restarted book reported ``fees_unknown = 0`` -- a statement that looked fully
   confirmed because it had forgotten.

These tests drive the REAL ``CarryEngine`` through ``LedgerBroker`` and
``CarryBroker`` against a scripted venue whose order rows follow the documented
shape, then write the journal to the real ``StateStore`` and rebuild it.
"""
from __future__ import annotations

import os
import sys
import time

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import ledger as L  # noqa: E402
from carry_broker import CarryBroker  # noqa: E402
from carry_engine import CarryEngine  # noqa: E402
from carry_risk import CarryRisk  # noqa: E402
from market_snapshot import MarketSnapshot  # noqa: E402
from persistence import StateStore  # noqa: E402
from test_carry_broker import FakeClient, envelope  # noqa: E402

MARK = 100_000.0
H8 = 8 * 3600 * 1000


class FeeVenue(FakeClient):
    """FakeClient whose ORDER endpoints return documented order rows.

    ``fees(product, side, filled)`` returns the extra fields for a filled row.
    Orders are stored by orderLinkId so create -> realtime -> cancel behave as
    the real flow does (create returns only ids; the fill is read back).
    """

    def __init__(self, fees, post_only=None, **kw):
        super().__init__(**kw)
        self._fees = fees
        self._post_only = post_only      # (first_fill, final_fill) or None
        self.orders = {}
        #: Settlement rows the venue "has paid", as the documented transaction-
        #: log shape. `txlog_fail` makes the endpoint unreadable.
        self.settlements = []
        #: Public settled-funding print times (ms), served at
        #: /v5/market/funding/history for the completeness check.
        self.prints = []
        self.txlog_fail = False
        self.txlog_calls = []

    def _request(self, method, endpoint, *, params=None, body=None,
                 signed=False, retries=3):
        if endpoint == "/v5/order/create":
            self.calls.append((method, endpoint, params, body))
            self.created.append(body)
            link, qty = body["orderLinkId"], float(body["qty"])
            if body.get("timeInForce") == "PostOnly":
                first, _final = self._post_only
                row = {"orderLinkId": link, "orderStatus": "PartiallyFilled",
                       "cumExecQty": str(first), "avgPrice": str(MARK),
                       "_cat": body["category"], "_side": body["side"]}
                row.update(self._fees(body["category"], body["side"], first))
            else:
                row = {"orderLinkId": link, "orderStatus": "Filled",
                       "cumExecQty": body["qty"], "avgPrice": str(MARK),
                       "_cat": body["category"], "_side": body["side"]}
                row.update(self._fees(body["category"], body["side"], qty))
            self.orders[link] = row
            return envelope({"orderId": f"id-{link}", "orderLinkId": link})
        if endpoint == "/v5/order/cancel":
            self.calls.append((method, endpoint, params, body))
            row = self.orders[body["orderLinkId"]]
            _first, final = self._post_only
            row.update({"orderStatus": "Cancelled", "cumExecQty": str(final)})
            # the read-back REPLACES the fee fields (absent means absent)
            for key in ("cumExecFee", "cumFeeDetail", "feeCurrency"):
                row.pop(key, None)
            row.update(self._fees(row["_cat"], row["_side"], final))
            return envelope({"orderLinkId": body["orderLinkId"]})
        if endpoint == "/v5/order/realtime":
            self.calls.append((method, endpoint, params, body))
            row = self.orders.get(params.get("orderLinkId"))
            return envelope({"list": [] if row is None else [
                {k: v for k, v in row.items() if not k.startswith("_")}]})
        if endpoint == "/v5/market/funding/history":
            self.calls.append((method, endpoint, params, body))
            # Honour the documented window and limit, newest first, exactly as
            # the venue does; a fake that ignored them could not tell a
            # windowed reader from a broken one.
            lo = int((params or {}).get("startTime", 0))
            hi = int((params or {}).get("endTime", 1 << 62))
            lim = int((params or {}).get("limit", 200))
            stamps = sorted((t for t in self.prints if lo <= t <= hi),
                            reverse=True)[:lim]
            return envelope({"category": "linear", "list": [
                {"symbol": (params or {}).get("symbol", "BTCUSDT"),
                 "fundingRate": "0.0003", "fundingRateTimestamp": str(t)}
                for t in stamps]})
        if endpoint == "/v5/account/transaction-log":
            self.calls.append((method, endpoint, params, body))
            self.txlog_calls.append(dict(params))
            if self.txlog_fail:
                raise RuntimeError("transaction-log down")
            rows = [r for r in self.settlements
                    if params["startTime"] <= int(r["transactionTime"])
                    <= params["endTime"]]
            page = int(params.get("cursor") or 0)
            chunk = rows[page * 2:(page + 1) * 2]          # 2 per page
            more = (page + 1) * 2 < len(rows)
            return envelope({"list": chunk,
                             "nextPageCursor": str(page + 1) if more else ""})
        if endpoint == "/v5/market/tickers":
            self.calls.append((method, endpoint, params, body))
            return envelope({"list": [{
                "markPrice": str(MARK), "lastPrice": str(MARK),
                "fundingRate": "0.0003", "bid1Price": str(MARK - 5),
                "ask1Price": str(MARK + 5)}]})
        return super()._request(method, endpoint, params=params, body=body,
                                signed=signed, retries=retries)


def modern(product, side, filled):
    """Spot BUY: fee in BTC. Linear: fee in USDT. Legacy field is the docs'
    deceptive "0"."""
    if product == "spot" and side == "Buy":
        return {"cumExecFee": "0", "cumFeeDetail": {"BTC": f"{filled * 0.001:.8f}"}}
    return {"cumExecFee": "0",
            "cumFeeDetail": {"USDT": f"{filled * MARK * 0.00055:.8f}"}}


def absent(product, side, filled):
    return {}


def with_mnt(product, side, filled):
    d = modern(product, side, filled)
    if product == "spot" and side == "Buy":
        d["cumFeeDetail"]["MNT"] = "4.2"
    return d


def build(fees, *, style="taker", post_only=None, clock_offset_ms=0):
    venue = FeeVenue(fees, post_only=post_only)
    broker = CarryBroker(client=venue, sequence_source=lambda p, s, w: 7,
                         order_gate=lambda: (True, "TEST_VENUE"))
    journal, clock = L.Journal(), [0]

    def ms():
        # Real epoch time, strictly increasing: the funding window is derived
        # from journal stamps, so a fake clock that counts 1000, 2000... would
        # test a world that does not exist.
        clock[0] = max(clock[0] + 1, int(time.time() * 1000) + clock_offset_ms)
        return clock[0]

    ledger_broker = L.LedgerBroker(broker, journal, ms=ms, spot_symbol="BTCUSDT")
    engine = CarryEngine(broker=ledger_broker, max_notional_usd=100_000.0,
                         borrow_apr=0.05, execution_mode="acquire",
                         execution_style=style, maker_wait_s=0.001,
                         persist=lambda s: None)
    engine.pair_risk = CarryRisk(max_notional_usd=100_000.0)
    engine.snapshot = MarketSnapshot(
        perp_mark=MARK, spot_mark=MARK, funding_bps=3.0, margin_multiple=5.0,
        observed_at_s=time.time())
    return venue, journal, engine


def open_the_hedge(engine):
    """The real decision path: warm the funding EWMA, then the entry candle."""
    for k in (1, 2):
        engine.on_candle(mark=MARK, funding_bps=3.0, spot=MARK,
                         funding_print_ms=k * H8)
    return engine.on_candle(mark=MARK, funding_bps=3.0, spot=MARK,
                            funding_print_ms=3 * H8)


def kinds(journal):
    """Fill/booking kinds only: reconciliation records are $0 bookkeeping."""
    return [e.kind for e in journal.entries
            if e.kind != "funding_reconciliation"]


class TestModernFeesReachTheStatement:
    def test_the_hedge_opens_and_every_fee_is_booked_by_its_own_currency(self):
        venue, journal, engine = build(modern)
        decision = open_the_hedge(engine)
        assert decision.action == "opened", decision
        assert kinds(journal) == ["spot_buy", "perp_open"]
        spot_qty = float(next(b for b in venue.created
                              if b["category"] == "spot")["qty"])
        perp_qty = float(next(b for b in venue.created
                              if b["category"] == "linear")["qty"])
        btc_fee, usdt_fee = spot_qty * 0.001, perp_qty * MARK * 0.00055
        st = journal.statement()
        # the deprecated "0" was ignored; the BTC fee is priced at the fill and
        # the USDT fee is taken as it is
        assert st["fees_usd"] == pytest.approx(btc_fee * MARK + usdt_fee)
        assert st["fees_usd"] > 0
        # a spot BUY fee comes out of the coin received: BTC balance is net of it
        assert st["btc_qty"] == pytest.approx(spot_qty - btc_fee)
        assert st["fees_confirmed"] is True
        # every FEE is confirmed, but the engine-level open never reconciled
        # funding, so the NET is honestly not yet confirmed
        assert st["net_basis"] == "provisional_funding_unverified"
        assert (st["fees_unknown"], st["fees_unresolved"],
                st["fees_unlabelled"]) == (0, 0, 0)
        raw = journal.entries[0].fee_raw
        assert raw["source"] == "cumFeeDetail"
        assert raw["components"] == [{"amount": pytest.approx(btc_fee),
                                      "currency": "BTC"}]

    def test_the_book_reconciles_against_the_wallet_it_describes(self):
        venue, journal, engine = build(modern)
        open_the_hedge(engine)
        st = journal.statement()
        rec = L.reconcile(journal, venue_btc=st["btc_qty"],
                          venue_usdt=st["usdt_qty"],
                          venue_perp_qty=st["open_perp_qty"])
        assert rec["verdict"] == "RECONCILED"


class TestUnknownFeesNeverLookConfirmed:
    def test_a_fee_the_venue_never_stated_is_flagged_and_the_net_is_provisional(
            self):
        venue, journal, engine = build(absent)
        assert open_the_hedge(engine).action == "opened"
        st = journal.statement()
        assert st["fees_usd"] == 0.0                     # nothing trusted, nothing invented
        assert st["fees_unknown"] == 2
        assert st["fees_confirmed"] is False
        assert st["net_basis"] == "provisional_fees_incomplete"

    def test_the_flag_survives_a_restart_through_the_real_store(self, tmp_path):
        """The counter used to live on the Journal object and die with it."""
        venue, journal, engine = build(absent)
        open_the_hedge(engine)
        store = StateStore(str(tmp_path / "carry.db"))
        store.save_ledger(journal.to_rows())
        reborn = L.Journal.from_rows(store.load_ledger())
        store.close()
        st = reborn.statement()
        assert st["fees_unknown"] == 2 and st["fees_confirmed"] is False
        assert reborn.unknown_fees == 2

    def test_rows_written_before_fee_provenance_cannot_claim_confirmation(self):
        _v, journal, engine = build(modern)
        open_the_hedge(engine)
        rows = journal.to_rows()
        for r in rows:
            r.pop("fee_status", None)
            r.pop("fee_raw", None)
        st = L.Journal.from_rows(rows).statement()
        assert st["fees_unlabelled"] == 2 and st["fees_confirmed"] is False

    def test_a_coin_with_no_booking_treatment_is_unresolved_not_converted(self):
        venue, journal, engine = build(with_mnt)
        open_the_hedge(engine)
        st = journal.statement()
        assert st["fees_unresolved"] == 1 and st["fees_confirmed"] is False
        spot = journal.entries[0]
        assert spot.fee_status == "unresolved"
        assert {c["currency"] for c in spot.fee_raw["components"]} == {"BTC", "MNT"}
        # the resolvable BTC part is still booked; the MNT is not guessed at
        spot_qty = float(next(b for b in venue.created
                              if b["category"] == "spot")["qty"])
        assert st["btc_qty"] == pytest.approx(spot_qty * (1 - 0.001))


class TestMakerPartialTakerRemainderAndCancelCaughtFills:
    def test_only_newly_observed_amounts_are_booked_per_currency(self):
        # post-only fills 0.3 (fee 0.0003 BTC); the cancel catches a cumulative
        # 0.4 (fee 0.0004 BTC); the taker remainder 0.6 pays 0.0006 BTC.
        venue, journal, engine = build(
            modern, style="maker_first", post_only=(0.3, 0.4))
        leg = engine._fire("BTCUSDT", "Buy", 1.0, "spot", patient=True)
        assert leg is not None and leg.filled_qty == pytest.approx(1.0)
        assert kinds(journal) == ["spot_buy"] * 3
        assert [e.ref.endswith("+cancel") for e in journal.entries] == [
            False, True, False]
        st = journal.statement()
        # cumulative figures were differenced, never re-added: 0.0003 + 0.0001
        # + 0.0006 = 0.001 BTC, against 1.0 BTC bought
        assert st["btc_qty"] == pytest.approx(1.0 - 0.001)
        assert st["fees_usd"] == pytest.approx(0.001 * MARK)
        assert st["fees_confirmed"] is True

    def test_an_unknown_fee_on_the_cancel_increment_stays_unknown(self):
        def fees(product, side, filled):
            # the placement states its fee; the cancel read-back does not
            return modern(product, side, filled) if filled == 0.3 else (
                {} if filled == 0.4 else modern(product, side, filled))
        venue, journal, engine = build(
            fees, style="maker_first", post_only=(0.3, 0.4))
        engine._fire("BTCUSDT", "Buy", 1.0, "spot", patient=True)
        statuses = [e.fee_status for e in journal.entries]
        assert statuses == ["confirmed", "unknown", "confirmed"]
        assert journal.statement()["fees_confirmed"] is False


class TestTickPersistsTheTruthAcrossARestart:
    """The deployed route: ``TradingBot.tick()`` -> ``CarryEngine.on_candle()`` ->
    ``LedgerBroker`` -> ``CarryBroker`` -> ``StateStore.save_ledger`` -> restart.

    Only collaborators that are not the subject (session log, equity read, the
    ordinary engine's observers, the market snapshot read) are stubbed; the
    carry engine, both brokers, the journal and the store are real.
    """

    def _bot(self, venue, journal, engine, store):
        import threading
        from unittest import mock

        import main as _main

        bot = _main.TradingBot.__new__(_main.TradingBot)
        bot.session = mock.Mock()
        bot.client = mock.Mock()
        bot.client.get_equity.return_value = 10_000.0
        bot.risk = mock.Mock()
        bot.risk.should_halt_trading.return_value = False
        bot.ws_consumer = None
        bot.engine = mock.Mock()
        bot.engine.observe_exits.return_value = {}
        bot.engine.check_naked_positions.return_value = {}
        bot._absorb_ws_observations = lambda rest_ok: None
        bot.carry, bot.carry_journal, bot.store = engine, journal, store
        bot.symbols = ["BTCUSDT"]
        bot._stop = threading.Event()
        return bot

    def _view(self, k):
        return MarketSnapshot(
            perp_mark=MARK, spot_mark=MARK, funding_bps=3.0, margin_multiple=5.0,
            observed_at_s=time.time(), funding_print_bps=3.0,
            # a recent settlement (the freshness guard is real); each tick
            # carries a distinct stamp so each counts as one new print
            funding_print_ms=int(time.time() * 1000) - 3_600_000 + k)

    @pytest.mark.parametrize("fees,confirmed", [(modern, True), (absent, False)])
    def test_the_statement_a_restart_rebuilds_is_the_one_that_was_earned(
            self, tmp_path, fees, confirmed):
        from unittest import mock

        venue, journal, engine = build(fees)
        store = StateStore(str(tmp_path / "carry.db"))
        bot = self._bot(venue, journal, engine, store)
        for k in (1, 2, 3):
            view = self._view(k)
            with mock.patch("market_snapshot.take_snapshot", return_value=view):
                bot.tick()
        assert kinds(journal) == ["spot_buy", "perp_open"]
        live = journal.statement()
        assert live["fees_confirmed"] is confirmed

        # ---- process restart: nothing survives but the database ----
        store.close()
        store = StateStore(str(tmp_path / "carry.db"))
        reborn = L.Journal.from_rows(store.load_ledger())
        again = reborn.statement()
        store.close()
        assert again["fees_confirmed"] is confirmed
        assert again["fees_usd"] == pytest.approx(live["fees_usd"])
        assert again["fees_unknown"] == live["fees_unknown"]
        assert again["net_basis"] == live["net_basis"]
