"""0044 — a book that can say what it owns, and prove it.

WHAT WAS MISSING
================
The engine knew `funding_collected` and nothing else. No fee was ever booked.
No cost was ever reconciled against the venue's wallet. There was no statement,
no journal, and no way to answer the only question a client actually asks:
*what did I earn, and where is it?*

INVENTORY D11 has said so since 0033 — "`cumExecFee` carried per leg; booking
it is Phase E" — and 0043 showed what that costs: funding was booked on the
entry price for four years, a quarter of it missing, and nothing in the system
could notice because nothing added up the books.

WHY THE LEDGER WRAPS THE BROKER
===============================
A ledger the engine has to remember to call is a ledger the engine can forget
to call, and the forgetting is silent. The broker is the engine's ONLY path to
the venue: every leg, in every mode, under every execution style, goes through
`place_market` or `place_post_only`. A ledger that decorates the broker cannot
be bypassed by any code path that exists or any code path anyone adds later.

DOUBLE ENTRY, AND WHY
=====================
Every entry balances to zero in USD or it is refused — nothing is written, and
the caller gets an exception rather than a warning. That single rule is what
makes a P&L checkable instead of asserted: a fee that vanishes, a funding
payment booked twice, a realised gain with no matching cash, each fails to
balance and each fails loudly.

The journal is APPEND-ONLY. A mistake is corrected by a reversing entry that
is itself in the journal, never by editing history. A ledger you can edit is a
ledger that cannot detect a bug, a partial fill, or a fat finger — the same
argument `reconcile_pair` has made since 0022.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import ledger as L  # noqa: E402

MS = 1_700_000_000_000


class TestEveryEntryBalancesOrIsRefused:
    def test_a_balanced_entry_is_accepted(self):
        j = L.Journal()
        j.post(L.Entry(ms=MS, kind="test", ref="a", lines=[
            L.Line(L.FEES, 5.0), L.Line(L.USDT, -5.0, qty=-5.0)]))
        assert len(j.entries) == 1

    def test_an_unbalanced_entry_is_refused_and_writes_nothing(self):
        j = L.Journal()
        with pytest.raises(L.LedgerError):
            j.post(L.Entry(ms=MS, kind="test", ref="a", lines=[
                L.Line(L.FEES, 5.0), L.Line(L.USDT, -4.0)]))
        assert j.entries == []

    def test_an_entry_with_no_lines_is_refused(self):
        j = L.Journal()
        with pytest.raises(L.LedgerError):
            j.post(L.Entry(ms=MS, kind="empty", ref="a", lines=[]))

    def test_an_unknown_account_is_refused_rather_than_created(self):
        """A typo must not silently open a new account that nothing reconciles."""
        j = L.Journal()
        with pytest.raises(L.LedgerError):
            j.post(L.Entry(ms=MS, kind="test", ref="a", lines=[
                L.Line("ASSET:BTCC", 1.0), L.Line(L.USDT, -1.0)]))

    def test_a_non_finite_amount_is_refused(self):
        j = L.Journal()
        with pytest.raises(L.LedgerError):
            j.post(L.Entry(ms=MS, kind="test", ref="a", lines=[
                L.Line(L.FEES, float("nan")), L.Line(L.USDT, 0.0)]))

    def test_the_whole_journal_sums_to_zero(self):
        j = _a_round_trip()
        assert sum(b.usd for b in j.balances().values()) == pytest.approx(
            0.0, abs=1e-6)


class TestTheJournalIsAppendOnly:
    def test_there_is_no_way_to_delete_an_entry(self):
        j = _a_round_trip()
        for forbidden in ("delete", "remove", "edit", "update_entry", "clear"):
            assert not hasattr(j, forbidden), forbidden

    def test_a_correction_is_a_reversing_entry(self):
        j = L.Journal()
        j.post(L.Entry(ms=MS, kind="fee", ref="wrong", lines=[
            L.Line(L.FEES, 5.0), L.Line(L.USDT, -5.0, qty=-5.0)]))
        j.reverse("wrong", ms=MS + 1, why="double booked")
        assert len(j.entries) == 2
        assert j.balances()[L.FEES].usd == pytest.approx(0.0)
        assert j.entries[-1].kind == "reversal"
        assert "double booked" in j.entries[-1].memo

    def test_reversing_something_that_does_not_exist_is_refused(self):
        j = L.Journal()
        with pytest.raises(L.LedgerError):
            j.reverse("nope", ms=MS, why="x")

    def test_entries_may_not_go_backwards_in_time(self):
        """A journal whose stamps are out of order cannot be cut into periods,
        which is what a statement is."""
        j = L.Journal()
        j.post(L.Entry(ms=MS, kind="fee", ref="a", lines=[
            L.Line(L.FEES, 1.0), L.Line(L.USDT, -1.0)]))
        with pytest.raises(L.LedgerError):
            j.post(L.Entry(ms=MS - 1, kind="fee", ref="b", lines=[
                L.Line(L.FEES, 1.0), L.Line(L.USDT, -1.0)]))


class TestTheSpotFeeIsChargedInBTC:
    """INVENTORY F3, made auditable instead of open.

    A Bybit spot market BUY charges its taker fee in the COIN. The wallet ends
    up holding `cumExecQty - fee`, while the perp leg hedges `cumExecQty` — and
    `reconcile_pair`'s dust tolerance is 1e-6, so every pair would have read as
    an INCIDENT. The ledger books the fee where the venue takes it, so the BTC
    balance is what the wallet will actually show."""

    def test_a_spot_buy_leaves_the_wallet_short_by_the_fee(self):
        j = L.Journal()
        L.spot_buy(j, ms=MS, ref="o1", qty=1.0, price=30_000.0,
                   fee_btc=0.00055)
        assert j.balances()[L.BTC].qty == pytest.approx(1.0 - 0.00055)
        assert j.balances()[L.USDT].qty == pytest.approx(-30_000.0)
        assert j.balances()[L.FEES].usd == pytest.approx(0.00055 * 30_000.0)

    def test_the_hedgeable_quantity_is_the_wallet_not_the_fill(self):
        j = L.Journal()
        L.spot_buy(j, ms=MS, ref="o1", qty=1.0, price=30_000.0,
                   fee_btc=0.00055)
        assert L.hedgeable_btc(j) == pytest.approx(0.99945)
        assert L.hedgeable_btc(j) != 1.0


class TestWhatTheBookEarned:
    def test_funding_is_booked_signed(self):
        j = L.Journal()
        L.funding(j, ms=MS, ref="f1", usd=12.5)
        L.funding(j, ms=MS + 1, ref="f2", usd=-4.0)
        assert j.statement()["funding_usd"] == pytest.approx(8.5)

    def test_a_short_that_falls_realises_a_gain(self):
        j = L.Journal()
        L.perp_open(j, ms=MS, ref="p1", qty=1.0, price=30_000.0, fee_usd=16.5)
        L.perp_close(j, ms=MS + 1, ref="p2", qty=1.0, price=29_000.0,
                     entry_price=30_000.0, fee_usd=15.95)
        assert j.statement()["realised_usd"] == pytest.approx(1_000.0)

    def test_a_short_that_rises_realises_a_loss(self):
        j = L.Journal()
        L.perp_open(j, ms=MS, ref="p1", qty=1.0, price=30_000.0, fee_usd=16.5)
        L.perp_close(j, ms=MS + 1, ref="p2", qty=1.0, price=31_000.0,
                     entry_price=30_000.0, fee_usd=17.05)
        assert j.statement()["realised_usd"] == pytest.approx(-1_000.0)

    def test_the_statement_identity_must_sum(self):
        """funding - fees - borrow + realised == net. Raises if it does not,
        exactly as carry_backtest's attribution does."""
        j = _a_round_trip()
        s = j.statement()
        assert s["net_usd"] == pytest.approx(
            s["funding_usd"] - s["fees_usd"] - s["borrow_usd"]
            + s["realised_usd"])

    def test_a_tampered_journal_fails_the_identity(self):
        j = _a_round_trip()
        j.entries[0].lines[0].usd += 1.0      # only a test can do this
        with pytest.raises(L.LedgerError):
            j.statement()

    def test_a_statement_can_be_cut_to_a_period(self):
        j = L.Journal()
        L.funding(j, ms=MS, ref="f1", usd=10.0)
        L.funding(j, ms=MS + 1000, ref="f2", usd=20.0)
        assert j.statement(from_ms=MS + 1)["funding_usd"] == pytest.approx(20.0)


class TestReconciliationObservesAndNeverAdjusts:
    def test_agreement_is_reported(self):
        j = _a_round_trip()
        b = j.balances()
        report = L.reconcile(j, venue_btc=b[L.BTC].qty,
                             venue_usdt=b[L.USDT].qty,
                             venue_perp_qty=abs(b[L.PERP].qty))
        assert report["verdict"] == "RECONCILED"

    def test_a_missing_coin_is_an_incident(self):
        j = _a_round_trip()
        b = j.balances()
        report = L.reconcile(j, venue_btc=b[L.BTC].qty - 0.5,
                             venue_usdt=b[L.USDT].qty,
                             venue_perp_qty=abs(b[L.PERP].qty))
        assert report["verdict"] == "INCIDENT"
        assert "btc" in report["disagrees_on"]

    def test_reconciling_does_not_write_to_the_journal(self):
        j = _a_round_trip()
        before = len(j.entries)
        L.reconcile(j, venue_btc=0.0, venue_usdt=0.0, venue_perp_qty=99.0)
        assert len(j.entries) == before

    def test_dust_is_not_an_incident(self):
        j = _a_round_trip()
        b = j.balances()
        report = L.reconcile(j, venue_btc=b[L.BTC].qty + 1e-9,
                             venue_usdt=b[L.USDT].qty + 1e-6,
                             venue_perp_qty=abs(b[L.PERP].qty))
        assert report["verdict"] == "RECONCILED"


class TestTheBrokerCannotBeBypassed:
    class Venue:
        def __init__(self):
            self.sent = []

        def place_market(self, *, symbol, side, qty, product):
            self.sent.append((side, product, qty))
            price = 30_000.0
            return {"filled_qty": qty, "avg_price": price,
                    "order_link_id": f"x{len(self.sent)}",
                    "fee": qty * price * 5.5 / 1e4}

        def place_post_only(self, *, symbol, side, qty, price, product):
            self.sent.append((side, product, qty))
            return {"filled_qty": qty, "avg_price": price,
                    "order_link_id": f"m{len(self.sent)}",
                    "fee": qty * price * 2.0 / 1e4, "maker": True,
                    "resting": False}

        def get_mark(self, symbol):
            return 30_000.0

    def test_every_market_fill_reaches_the_journal(self):
        j = L.Journal()
        b = L.LedgerBroker(self.Venue(), j, ms=lambda: MS)
        b.place_market(symbol="BTCUSDT", side="Sell", qty=1.0,
                       product="linear")
        assert len(j.entries) == 1
        assert j.balances()[L.PERP].qty == pytest.approx(-1.0)

    def test_every_maker_fill_reaches_the_journal(self):
        j = L.Journal()
        b = L.LedgerBroker(self.Venue(), j, ms=lambda: MS)
        b.place_post_only(symbol="BTCUSDT", side="Sell", qty=1.0,
                          price=30_000.0, product="linear")
        assert len(j.entries) == 1
        assert j.balances()[L.FEES].usd == pytest.approx(
            1.0 * 30_000.0 * 2.0 / 1e4)

    def test_a_fee_the_venue_would_not_state_is_not_booked_as_zero(self):
        """`None` means unknown. Booking it as 0.0 makes a real cost vanish,
        which is the whole reason LegFill.fee is Optional."""
        class Quiet(self.Venue):
            def place_market(self, *, symbol, side, qty, product):
                out = super().place_market(symbol=symbol, side=side, qty=qty,
                                           product=product)
                out["fee"] = None
                return out
        j = L.Journal()
        b = L.LedgerBroker(Quiet(), j, ms=lambda: MS)
        b.place_market(symbol="BTCUSDT", side="Sell", qty=1.0,
                       product="linear")
        assert j.unknown_fees == 1
        assert j.statement()["fees_unknown"] == 1

    def test_reads_pass_straight_through(self):
        j = L.Journal()
        b = L.LedgerBroker(self.Venue(), j, ms=lambda: MS)
        assert b.get_mark("BTCUSDT") == 30_000.0

    def test_a_method_the_wrapper_does_not_know_fails_loudly(self):
        """No __getattr__ that silently forwards anything: a broker method
        added later must be routed deliberately, because the question is
        always 'does this one move money?'"""
        j = L.Journal()
        b = L.LedgerBroker(self.Venue(), j, ms=lambda: MS)
        with pytest.raises(AttributeError):
            b.some_method_invented_later()


class TestItSurvivesARestart:
    def test_a_journal_round_trips_through_rows(self):
        j = _a_round_trip()
        back = L.Journal.from_rows(j.to_rows())
        assert back.statement() == j.statement()
        assert len(back.entries) == len(j.entries)

    def test_a_row_that_does_not_balance_is_refused_on_load(self):
        j = _a_round_trip()
        rows = j.to_rows()
        rows[0]["lines"][0]["usd"] += 1.0
        with pytest.raises(L.LedgerError):
            L.Journal.from_rows(rows)


def _a_round_trip() -> "L.Journal":
    """One complete acquire cycle, with every cost the venue charges."""
    j = L.Journal()
    L.spot_buy(j, ms=MS, ref="s1", qty=1.0, price=30_000.0, fee_btc=0.00055)
    L.perp_open(j, ms=MS + 1, ref="p1", qty=0.99945, price=30_000.0,
                fee_usd=16.49)
    L.funding(j, ms=MS + 2, ref="f1", usd=9.0)
    L.borrow(j, ms=MS + 3, ref="b1", usd=4.1)
    L.perp_close(j, ms=MS + 4, ref="p2", qty=0.99945, price=29_500.0,
                 entry_price=30_000.0, fee_usd=16.21)
    L.spot_sell(j, ms=MS + 5, ref="s2", qty=0.99945, price=29_500.0,
                cost_basis=30_000.0, fee_usd=29.48)
    return j


class TestItIsWiredIntoTheLiveBook:
    """A ledger that only exists in a backtest is a ledger that books nothing."""

    def _bot(self):
        import config as _config
        import main as _main
        from unittest import mock
        cfg = _config.load({"BOOK_MODE": "carry", "CARRY_BORROW_APR": "0.0",
                            "CARRY_EXECUTION_MODE": "overlay"})
        return _main.build_bot(config=cfg, store=_Store(), client=mock.Mock(),
                               risk_manager=mock.Mock(), engine=mock.Mock())

    def test_the_engines_broker_books_every_fill(self):
        bot = self._bot()
        assert isinstance(bot.carry.broker, L.LedgerBroker)
        assert bot.carry_journal is not None

    def test_the_venue_adapter_is_still_underneath(self):
        from carry_broker import CarryBroker
        bot = self._bot()
        assert isinstance(bot.carry.broker.inner, CarryBroker)

    def test_a_previous_journal_is_resumed_not_restarted(self):
        import config as _config
        import main as _main
        from unittest import mock
        store = _Store()
        j = L.Journal()
        L.funding(j, ms=MS, ref="f", usd=42.0)
        store.save_ledger(j.to_rows())
        cfg = _config.load({"BOOK_MODE": "carry", "CARRY_BORROW_APR": "0.0",
                            "CARRY_EXECUTION_MODE": "overlay"})
        bot = _main.build_bot(config=cfg, store=store, client=mock.Mock(),
                              risk_manager=mock.Mock(), engine=mock.Mock())
        assert bot.carry_journal.statement()["funding_usd"] == pytest.approx(
            42.0)

    def test_a_tampered_journal_fails_at_startup_not_in_a_statement(self):
        import config as _config
        import main as _main
        from unittest import mock
        store = _Store()
        j = L.Journal()
        L.funding(j, ms=MS, ref="f", usd=42.0)
        rows = j.to_rows()
        rows[0]["lines"][0]["usd"] += 7.0        # somebody edited the database
        store.save_ledger(rows)
        cfg = _config.load({"BOOK_MODE": "carry", "CARRY_BORROW_APR": "0.0",
                            "CARRY_EXECUTION_MODE": "overlay"})
        with pytest.raises(L.LedgerError):
            _main.build_bot(config=cfg, store=store, client=mock.Mock(),
                            risk_manager=mock.Mock(), engine=mock.Mock())


class TestTheJournalReproducesFourYearsOfPandL:
    """The cross-check that makes the P&L evidence rather than an assertion.

    The replay runs its own running totals. The journal builds the same number
    out of 300-odd double-entry postings, every one of which had to balance to
    zero before it was written. They agree to the cent, and the only thing that
    separates them is the mark on the position still open at the window end —
    which the journal deliberately does not hold, because an open position's
    value is an estimate.
    """

    corpus = pytest.mark.skipif(
        not os.path.exists(os.path.join(
            os.path.dirname(__file__), "..", "data", "real_bybit_btc_4h")),
        reason="the same-venue corpus is not built here")

    @corpus
    @pytest.mark.parametrize("borrow,impact", [(0.0, 0.0), (0.0, 1.093),
                                               (0.05, 0.0)])
    def test_realised_plus_unrealised_is_the_replays_net(self, borrow, impact):
        import carry_backtest as cb
        import carry_replay as rp
        repo = os.path.join(os.path.dirname(__file__), "..")
        rows, _meta = cb.load_bybit_settlements(repo)
        r = rp.replay(rows, notional=100_000.0, borrow_apr=borrow,
                      impact_bps=impact)
        assert r["ledger_agrees"], (r["ledger"]["net_usd"],
                                    r["unrealised_usd"], r["net_usd"])
        assert r["ledger"]["net_usd"] + r["unrealised_usd"] == pytest.approx(
            r["net_usd"], abs=0.01)

    @corpus
    def test_the_fees_agree_exactly(self):
        """Not approximately: every fee in the journal came from a fill the
        engine actually sent, through the broker it cannot go around."""
        import carry_backtest as cb
        import carry_replay as rp
        repo = os.path.join(os.path.dirname(__file__), "..")
        rows, _meta = cb.load_bybit_settlements(repo)
        r = rp.replay(rows, notional=100_000.0, borrow_apr=0.0, impact_bps=0.0)
        assert r["ledger"]["fees_usd"] == pytest.approx(-r["fees_usd"],
                                                        abs=0.01)
        assert r["ledger"]["fees_unknown"] == 0

    @corpus
    def test_the_hedge_is_shown_to_have_hedged(self):
        """Both legs, and the residual between them, which is the basis — the
        only price exposure a delta-neutral overlay actually runs."""
        import carry_backtest as cb
        import carry_replay as rp
        repo = os.path.join(os.path.dirname(__file__), "..")
        rows, _meta = cb.load_bybit_settlements(repo)
        r = rp.replay(rows, notional=100_000.0, borrow_apr=0.0, impact_bps=0.0)
        effect = L.hedge_effectiveness(
            perp_realised_usd=r["hedge"]["perp_realised_usd"],
            inventory_change_usd=r["hedge"]["inventory_change_usd"])
        # the legs are large and opposite; what is left is small
        assert abs(effect["perp_realised_usd"]) > 10_000
        assert abs(effect["residual_fraction_of_leg"]) < 0.02


class _Store:
    """The store a carry book needs: a sequence, a kill switch, a position
    and — since 0044 — a journal."""

    def __init__(self):
        self.seq = 0

    def next_order_seq(self):
        self.seq += 1
        return self.seq

    def trip_kill_switch(self, reason):
        self.tripped = reason

    def is_kill_switch_engaged(self):
        return (False, "")

    def save_carry_position(self, state):
        self.carry_state = state

    def load_carry_position(self):
        return getattr(self, "carry_state", None)

    def save_ledger(self, rows):
        self.ledger_rows = list(rows)

    def load_ledger(self):
        return getattr(self, "ledger_rows", None)

    def open_positions(self):
        return []
