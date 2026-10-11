"""Funding is the carry book's income, so it is booked from what the VENUE paid.

THE DEFECT
==========
`CarryEngine` computes `funding_collected += print_bps/1e4 * qty * mark` where
`mark` is the mark at the tick that noticed the print, not the settlement mark
(the engine's own comments say so). `main.tick` posted that number into the
journal as funding income, and the statement then called the net REALISED. An
estimate was standing in for the book's largest income line.

Bybit pays funding as `SETTLEMENT` rows in the account transaction log, with a
signed `funding` (positive = received), a stable `id` and the position `size`.
These tests pin: booked once from those rows, never from the estimate, only when
the row is the book's own, visible when it cannot be read, and persisted.
"""
from __future__ import annotations

import os
import sys
import threading
import time
from unittest import mock

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import ledger as L  # noqa: E402
from carry_broker import CarryBroker, PairIncident  # noqa: E402
from market_snapshot import MarketSnapshot  # noqa: E402
from persistence import StateStore  # noqa: E402
from test_carry_broker import FakeClient, envelope  # noqa: E402
from test_carry_fee_truth import MARK, H8, build, modern  # noqa: E402

T0 = 1_800_000_000_000


def hedged_journal(qty=0.5):
    """A journal with a confirmed-fee short of `qty` opened at T0."""
    j = L.Journal()
    L.perp_open(j, ms=T0, ref="o1", qty=qty, price=MARK, fee_usd=1.0,
                fee_status="confirmed")
    return j


def pay(i, usd, *, ms=T0 + 3_600_000, size=-0.5, currency="USDT"):
    return {"id": str(i), "ms": ms, "usd": usd, "currency": currency,
            "size": size, "symbol": "BTCUSDT"}


class TestBookingFromVenuePayments:
    def test_a_received_and_a_paid_settlement_are_booked_signed_from_the_venue(self):
        j = hedged_journal()
        out = L.book_funding_payments(
            j, [pay(1, 4.25), pay(2, -1.5, ms=T0 + 2 * 3_600_000)], ms=T0 + 10)
        st = j.statement()
        assert out == {"booked": 2, "duplicate": 0, "unattributed": 0}
        assert st["funding_usd"] == pytest.approx(2.75)
        assert st["funding_venue_usd"] == pytest.approx(2.75)
        assert st["funding_estimated_usd"] == 0.0
        assert st["funding_confirmed"] is True and st["net_basis"] == "confirmed"

    def test_the_same_row_is_never_booked_twice(self):
        j = hedged_journal()
        L.book_funding_payments(j, [pay(1, 4.25)], ms=T0 + 10)
        again = L.book_funding_payments(j, [pay(1, 4.25)], ms=T0 + 20)
        assert again["duplicate"] == 1 and again["booked"] == 0
        assert j.statement()["funding_usd"] == pytest.approx(4.25)

    @pytest.mark.parametrize("row,why", [
        (pay(1, 5.0, ms=T0 - 3_600_000), "no perp position"),
        (pay(2, 5.0, size=-2.0), "venue position"),
        (pay(3, 5.0, currency="USDC"), "USDC"),
    ])
    def test_a_payment_the_book_cannot_claim_is_flagged_and_not_income(
            self, row, why):
        j = hedged_journal()
        out = L.book_funding_payments(j, [row], ms=T0 + 10)
        st = j.statement()
        assert out["unattributed"] == 1 and out["booked"] == 0
        assert st["funding_usd"] == 0.0            # not this book's money
        assert st["funding_unattributed"] == 1
        assert st["funding_confirmed"] is False
        assert st["net_basis"] == "provisional_funding_unverified"
        entry = j.entries[-1]
        assert why in entry.memo and entry.fee_raw["venue_usd"] == 5.0

    def test_the_engine_estimate_path_is_labelled_an_estimate(self):
        j = hedged_journal()
        L.funding(j, ms=T0 + 5, ref="legacy", usd=9.0)      # historical caller
        st = j.statement()
        assert st["funding_estimated_usd"] == pytest.approx(9.0)
        assert st["funding_confirmed"] is False
        assert st["net_basis"] == "provisional_funding_unverified"

    def test_an_unreadable_venue_is_recorded_once_an_hour_and_persists(self):
        j = hedged_journal()
        L.mark_funding_unread(j, ms=T0 + 10, why="down")
        L.mark_funding_unread(j, ms=T0 + 20, why="still down")
        assert [e.kind for e in j.entries].count("funding_unread") == 1
        assert j.statement()["funding_confirmed"] is False

    def test_basis_survives_a_rebuild_and_old_rows_default_to_estimate(self):
        j = hedged_journal()
        L.book_funding_payments(j, [pay(1, 4.0)], ms=T0 + 10)
        L.funding(j, ms=T0 + 20, ref="old", usd=1.0)
        rows = j.to_rows()
        assert L.Journal.from_rows(rows).statement()["funding_venue_usd"] == \
            pytest.approx(4.0)
        for r in rows:
            r.pop("basis", None)
        legacy = L.Journal.from_rows(rows).statement()
        assert legacy["funding_venue_usd"] == 0.0           # never "venue" by default
        assert legacy["funding_estimated_usd"] == pytest.approx(5.0)


class Log(FakeClient):
    def __init__(self, rows, **kw):
        super().__init__(**kw)
        self.rows, self.params = rows, []

    def _request(self, method, endpoint, **kw):
        if endpoint != "/v5/account/transaction-log":
            return super()._request(method, endpoint, **kw)
        p = kw["params"]
        self.params.append(dict(p))
        rows = [r for r in self.rows
                if p["startTime"] <= int(r["transactionTime"]) <= p["endTime"]]
        i = int(p.get("cursor") or 0)
        return envelope({"list": rows[i * 2:(i + 1) * 2],
                         "nextPageCursor": str(i + 1) if (i + 1) * 2 < len(rows)
                         else ""})


def row(i, funding, t, **kw):
    base = {"id": f"S{i}", "symbol": "BTCUSDT", "type": "SETTLEMENT",
            "funding": funding, "transactionTime": str(t), "currency": "USDT",
            "size": "-0.5"}
    base.update(kw)
    return base


def broker(client):
    return CarryBroker(client=client, sequence_source=lambda *a: 0,
                       order_gate=lambda: (False, "read-only"))


class TestTheVenueReader:
    def test_it_asks_for_settlement_rows_of_the_linear_unified_account(self):
        c = Log([row(1, "0.5", 1_000)])
        broker(c).get_funding_payments("BTCUSDT", 0, 5_000)
        p = c.params[0]
        assert (p["accountType"], p["category"], p["type"], p["limit"]) == (
            "UNIFIED", "linear", "SETTLEMENT", 50)

    def test_signs_ids_sizes_and_currency_are_carried_and_pages_are_followed(self):
        c = Log([row(1, "0.5", 1_000), row(2, "-0.25", 2_000),
                 row(3, "1.0", 3_000)])
        got = broker(c).get_funding_payments("BTCUSDT", 0, 5_000)
        assert [(g["id"], g["usd"]) for g in got] == [
            ("S1", 0.5), ("S2", -0.25), ("S3", 1.0)]
        assert got[0]["size"] == -0.5 and got[0]["currency"] == "USDT"
        assert len(c.params) == 2                          # two pages

    def test_other_symbols_and_trade_rows_are_ignored(self):
        c = Log([row(1, "0.5", 1_000), row(2, "9", 2_000, symbol="ETHUSDT"),
                 row(3, "", 3_000)])
        got = broker(c).get_funding_payments("BTCUSDT", 0, 5_000)
        assert [g["id"] for g in got] == ["S1"]

    def test_a_long_gap_is_walked_in_windows_of_at_most_seven_days(self):
        day = 86_400_000
        c = Log([])
        broker(c).get_funding_payments("BTCUSDT", 0, 15 * day)
        assert len(c.params) == 3
        assert all(p["endTime"] - p["startTime"] <= 7 * day for p in c.params)

    @pytest.mark.parametrize("bad", [
        row(1, "abc", 1_000), row(1, "nan", 1_000),
        {k: v for k, v in row(1, "0.5", 1_000).items() if k != "id"}])
    def test_a_malformed_funding_row_is_refused_not_skipped(self, bad):
        with pytest.raises(PairIncident):
            broker(Log([bad])).get_funding_payments("BTCUSDT", 0, 5_000)

    def test_a_response_with_no_list_is_unreadable_not_empty(self):
        class NoList(Log):
            def _request(self, method, endpoint, **kw):
                if endpoint == "/v5/account/transaction-log":
                    return envelope({})
                return super()._request(method, endpoint, **kw)
        with pytest.raises(PairIncident):
            broker(NoList([])).get_funding_payments("BTCUSDT", 0, 5_000)


# ---------------------------------------------------------------------------
# the deployed route: TradingBot.tick() -> venue -> journal -> store -> restart
# ---------------------------------------------------------------------------


def make_bot(venue, journal, engine, store):
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


def tick(bot, k, after_open=False):
    """One real `TradingBot.tick()`. `after_open` delivers a funding print that
    settled AFTER the hedge opened, which is what makes the ENGINE accrue its own
    (estimated) `funding_collected`."""
    print_ms = (bot.carry.position.opened_ms + 1 if after_open
                else int(time.time() * 1000) - 3_600_000 + k)
    view = MarketSnapshot(
        perp_mark=MARK, spot_mark=MARK, funding_bps=3.0, margin_multiple=5.0,
        observed_at_s=time.time(), funding_print_bps=3.0,
        funding_print_ms=print_ms)
    bot._last_funding_poll_ms = 0           # defeat the throttle for the test
    with mock.patch("market_snapshot.take_snapshot", return_value=view):
        bot.tick()


class TestTickBooksWhatTheVenuePaid:
    def _hedged(self, tmp_path):
        venue, journal, engine = build(modern)
        store = StateStore(str(tmp_path / "carry.db"))
        bot = make_bot(venue, journal, engine, store)
        for k in (1, 2, 3):
            tick(bot, k)
        assert [e.kind for e in journal.entries] == ["spot_buy", "perp_open"]
        qty = abs(journal.balances()[L.PERP].qty)
        return venue, journal, bot, store, qty

    def test_funding_comes_from_the_settlement_row_not_the_engine_estimate(
            self, tmp_path):
        venue, journal, bot, store, qty = self._hedged(tmp_path)
        t = journal.entries[-1].ms - 5
        venue.settlements.append(row(1, "0.1234", t, size=str(-qty)))
        tick(bot, 4, after_open=True)
        st = journal.statement()
        assert st["funding_usd"] == pytest.approx(0.1234)    # the venue's figure
        assert st["funding_venue_usd"] == pytest.approx(0.1234)
        assert st["funding_confirmed"] is True
        # the engine DID accrue its own estimate (qty * mark * 3 bps), and none
        # of it reached the journal
        assert bot._carry_estimated_funding == pytest.approx(qty * MARK * 3e-4)
        assert bot._carry_estimated_funding > 10 * st["funding_usd"]
        kinds = [e.kind for e in journal.entries]
        assert kinds.count("funding") == 1

    def test_polling_again_does_not_double_book(self, tmp_path):
        venue, journal, bot, store, qty = self._hedged(tmp_path)
        venue.settlements.append(
            row(1, "0.1234", journal.entries[-1].ms - 5, size=str(-qty)))
        tick(bot, 4, after_open=True)
        tick(bot, 5)
        tick(bot, 6)
        assert [e.kind for e in journal.entries].count("funding") == 1
        assert journal.statement()["funding_usd"] == pytest.approx(0.1234)

    def test_someone_elses_perp_on_the_same_account_is_not_booked(self, tmp_path):
        venue, journal, bot, store, qty = self._hedged(tmp_path)
        venue.settlements.append(row(
            1, "7.0", journal.entries[-1].ms - 5, size=str(-qty * 10)))
        tick(bot, 4)
        st = journal.statement()
        assert st["funding_usd"] == 0.0 and st["funding_unattributed"] == 1
        assert st["net_basis"] == "provisional_funding_unverified"

    def test_an_unreadable_log_with_a_position_open_is_visible_not_zero(
            self, tmp_path):
        venue, journal, bot, store, qty = self._hedged(tmp_path)
        venue.txlog_fail = True
        tick(bot, 4)
        st = journal.statement()
        assert st["funding_unread"] == 1 and st["funding_confirmed"] is False

    def test_the_truth_survives_a_restart(self, tmp_path):
        venue, journal, bot, store, qty = self._hedged(tmp_path)
        venue.settlements.append(
            row(1, "0.1234", journal.entries[-1].ms - 5, size=str(-qty)))
        tick(bot, 4)
        live = journal.statement()
        store.close()
        store = StateStore(str(tmp_path / "carry.db"))
        reborn = L.Journal.from_rows(store.load_ledger()).statement()
        store.close()
        for key in ("funding_usd", "funding_venue_usd", "funding_confirmed",
                    "net_basis", "fees_usd"):
            assert reborn[key] == live[key], key

    def test_nothing_is_polled_before_the_book_has_ever_traded(self, tmp_path):
        venue, journal, engine = build(modern)
        bot = make_bot(venue, journal, engine,
                       StateStore(str(tmp_path / "idle.db")))
        tick(bot, 1)
        assert venue.txlog_calls == []
