"""Account-level brakes stop NEW exposure; they never blind a held carry pair.

Real ``TradingBot.tick()`` -> real CarryEngine -> LedgerBroker -> CarryBroker ->
scripted venue. Only the collaborators that are not the subject are stubbed
(session, risk layer, directional engine, the market view).
"""
from __future__ import annotations

import os
import sys
from unittest import mock


HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from bybit_connection import BybitAPIError, TransientAPIError  # noqa: E402
import pytest  # noqa: E402
from carry_engine import BookState  # noqa: E402
from persistence import StateStore  # noqa: E402
from test_carry_fee_truth import build, modern  # noqa: E402
from test_carry_funding_truth import make_bot, tick  # noqa: E402


def _hedged(tmp_path):
    venue, journal, engine = build(modern)
    store = StateStore(str(tmp_path / "carry.db"))
    bot = make_bot(venue, journal, engine, store)
    for k in (1, 2, 3):
        tick(bot, k)
    assert engine.position is not None, "fixture failed to open the hedge"
    return venue, journal, engine, bot


def _flat(tmp_path):
    venue, journal, engine = build(modern)
    store = StateStore(str(tmp_path / "carry.db"))
    return venue, journal, engine, make_bot(venue, journal, engine, store)


class TestHeldPairIsMonitoredThroughBrakes:
    def test_a_risk_halt_does_not_skip_the_margin_exit(self, tmp_path):
        venue, journal, engine, bot = _hedged(tmp_path)
        bot.risk.should_halt_trading.return_value = True
        venue.im, venue.mm = 1.1, 1.0                    # margin collapses
        tick(bot, 4)
        assert engine.position is None and engine.state is BookState.FLAT
        assert any(b["category"] == "linear" and b["side"] == "Buy"
                   for b in venue.created), "the perp was not bought back"

    def test_an_unreadable_equity_does_not_skip_the_margin_exit(self, tmp_path):
        venue, journal, engine, bot = _hedged(tmp_path)
        bot.client.get_equity.side_effect = BybitAPIError("down")
        venue.im, venue.mm = 1.1, 1.0
        tick(bot, 4)
        assert engine.position is None

    def test_a_risk_halt_never_opens_a_new_pair(self, tmp_path):
        venue, journal, engine, bot = _flat(tmp_path)
        bot.risk.should_halt_trading.return_value = True
        for k in (1, 2, 3, 4):
            tick(bot, k)
        assert venue.created == []

    def test_an_unreadable_equity_never_opens_a_new_pair(self, tmp_path):
        venue, journal, engine, bot = _flat(tmp_path)
        bot.client.get_equity.side_effect = BybitAPIError("down")
        for k in (1, 2, 3, 4):
            tick(bot, k)
        assert venue.created == []


class TestAnEscapedExceptionMidTransactionHalts:
    def test_opening_state_with_an_exception_halts_instead_of_retrying(
            self, tmp_path):
        venue, journal, engine, bot = _flat(tmp_path)

        def boom(*a, **k):
            engine.state = BookState.OPENING
            raise KeyError("bug between the two legs")

        engine.on_candle = boom
        tick(bot, 1)
        assert engine.state is BookState.HALTED

    def test_a_steady_state_exception_does_not_halt_the_book(self, tmp_path):
        venue, journal, engine, bot = _flat(tmp_path)

        def boom(*a, **k):
            raise ValueError("transient read problem while flat")

        engine.on_candle = boom
        tick(bot, 1)
        assert engine.state is BookState.FLAT


class TestTheVenueIsTheSourceOfTruthForTheShort:
    def test_a_venue_position_that_differs_from_the_book_halts(self, tmp_path):
        venue, journal, engine, bot = _hedged(tmp_path)
        before = len(venue.created)
        venue.perp_qty = 0.6                    # someone/something cut the short
        tick(bot, 4)
        assert engine.state is BookState.HALTED
        assert len(venue.created) == before, "a correction was sent on a guess"
        assert engine.position is not None, "the ledger was rewritten to match"
        assert engine.position.perp.filled_qty == pytest.approx(1.0)

    def test_an_unreadable_venue_position_halts(self, tmp_path):
        venue, journal, engine, bot = _hedged(tmp_path)
        real = venue._request

        def unreadable(method, endpoint, **kw):
            if endpoint == "/v5/position/list":
                raise TransientAPIError("timed out")
            return real(method, endpoint, **kw)

        venue._request = unreadable
        engine.snapshot = None                  # not under test
        with mock.patch.object(engine, "_check_margin", return_value=None):
            d = engine.on_candle(mark=100_000.0, funding_bps=3.0,
                                 spot=100_000.0, funding_print_ms=1)
        assert d.reason == "VENUE_POSITION_UNREADABLE"
        assert engine.state is BookState.HALTED

    def test_a_matching_venue_position_changes_nothing(self, tmp_path):
        venue, journal, engine, bot = _hedged(tmp_path)
        before = len(venue.created)
        tick(bot, 4)
        assert engine.state is BookState.HEDGED and len(venue.created) == before


class TestAJournalThatCannotBeWrittenBlocksNewExposure:
    def test_flat_book_halts_instead_of_trading_unaccounted(self, tmp_path):
        venue, journal, engine, bot = _flat(tmp_path)
        bot.store.save_ledger = mock.Mock(side_effect=OSError("disk full"))
        tick(bot, 1)                      # flat: nothing yet to write wrongly
        assert bot._journal_unsaved is True
        before = len(venue.created)
        tick(bot, 2)
        assert engine.state is BookState.HALTED
        assert len(venue.created) == before

    def test_a_held_pair_keeps_being_monitored_when_the_journal_fails(
            self, tmp_path):
        venue, journal, engine, bot = _hedged(tmp_path)
        bot.store.save_ledger = mock.Mock(side_effect=OSError("disk full"))
        venue.im, venue.mm = 1.1, 1.0
        tick(bot, 4)
        assert engine.position is None, "stopped watching a live short"

    def test_a_healthy_journal_does_not_block(self, tmp_path):
        venue, journal, engine, bot = _flat(tmp_path)
        bot._journal_unsaved = True
        bot.store.save_ledger = mock.Mock()
        # a flat, unsaved book is halted on its next cycle; a successful write
        # in an earlier cycle is what would have cleared the flag
        bot._journal_unsaved = False
        tick(bot, 1)
        assert engine.state is not BookState.HALTED


class TestTheOperatorCanSeeTheBook:
    def test_status_reports_both_legs_delta_margin_funding_and_no_fault(
            self, tmp_path):
        venue, journal, engine, bot = _hedged(tmp_path)
        bot.cfg = mock.Mock(USE_TESTNET=True, PAPER_TRADING=False,
                            CARRY_EXECUTION_MODE="acquire")
        status = bot.carry_status()
        assert status["attached"] is True and status["book_state"] == "HEDGED"
        assert status["environment"]["testnet"] is True
        pos = status["position"]
        assert pos["spot_qty"] == pytest.approx(1.0)
        assert pos["perp_short_qty"] == pytest.approx(1.0)
        assert pos["net_delta_btc"] == pytest.approx(0.0)
        assert status["protective_stop"] == "NOT_APPLICABLE_DELTA_NEUTRAL"
        assert status["market"]["margin_multiple"] is not None
        acct = status["accounting"]
        # A hedge opened moments ago, reconciled over complete (empty) evidence:
        # no settlement was due, so funding is positively ZERO and the net is
        # fees only. That is "confirmed" under the contract below; it must not
        # depend on which millisecond the reconciliation happened to run in.
        _assert_contract(acct)
        assert acct["net_basis"] == "confirmed"
        assert acct["funding_usd"] == 0 and acct["fees_usd"] > 0
        assert status["blocking_fault"] == ""
        assert status["last_decision"]["reason"]

    def test_a_halted_book_names_its_fault_and_is_unhealthy(self, tmp_path):
        venue, journal, engine, bot = _hedged(tmp_path)
        bot.cfg = mock.Mock(USE_TESTNET=True, PAPER_TRADING=False)
        venue.perp_qty = 0.6
        tick(bot, 4)
        status = bot.carry_status()
        assert status["book_state"] == "HALTED"
        assert status["blocking_fault"] == "VENUE_POSITION_DRIFT"

    def test_health_is_false_when_the_book_has_a_blocking_fault(self, tmp_path):
        venue, journal, engine, bot = _hedged(tmp_path)
        bot.cfg = mock.Mock()
        bot._reconciled = True
        bot.started_epoch = 0.0
        bot.store = mock.Mock(
            is_kill_switch_engaged=lambda: (False, ""),
            positions_without_stops=lambda: [], open_position_count=lambda: 0)
        bot.memory = None
        bot.strategy = None
        bot._observation_state = "HEALTHY"
        assert bot.health()["healthy"] is True
        venue.perp_qty = 0.6
        real_store = bot.store
        bot.store = mock.Mock(
            is_kill_switch_engaged=real_store.is_kill_switch_engaged,
            positions_without_stops=real_store.positions_without_stops,
            open_position_count=real_store.open_position_count,
            save_ledger=lambda rows: None)
        tick(bot, 4)
        assert bot.health()["healthy"] is False


class TestRestartWithAnOpenPositionThroughTheRealStartupPath:
    """Process 1 opens and persists through the real store; process 2 is a new
    bot, a new engine and a new venue connection over the SAME database."""

    def _first_process(self, tmp_path):
        venue, journal, engine = build(modern)
        store = StateStore(str(tmp_path / "carry.db"))
        engine.persist = store.save_carry_position
        bot = make_bot(venue, journal, engine, store)
        for k in (1, 2, 3):
            tick(bot, k)
        assert engine.position is not None
        store.save_ledger(journal.to_rows())
        store.close()
        return venue

    def _second_process(self, tmp_path, *, perp_qty=1.0, btc=1.0):
        venue2, journal2, engine2 = build(modern)
        venue2.perp_qty = perp_qty
        real = venue2._request

        def wallet(method, endpoint, **kw):
            if endpoint == "/v5/account/wallet-balance":
                from test_carry_broker import envelope
                return envelope({"list": [{"coin": [
                    {"coin": "BTC", "walletBalance": str(btc)}]}]})
            return real(method, endpoint, **kw)

        venue2._request = wallet
        store2 = StateStore(str(tmp_path / "carry.db"))
        engine2.persist = store2.save_carry_position
        bot2 = make_bot(venue2, journal2, engine2, store2)
        return venue2, engine2, bot2

    def test_a_consistent_venue_resumes_the_same_pair_and_opens_nothing(
            self, tmp_path):
        self._first_process(tmp_path)
        venue2, engine2, bot2 = self._second_process(tmp_path)
        assert bot2.carry_cold_start() is True
        assert engine2.position is not None
        assert engine2.position.perp.filled_qty == pytest.approx(1.0)
        tick(bot2, 4)
        assert venue2.created == [], "the restarted book opened a second pair"

    def test_a_venue_that_no_longer_matches_refuses_to_start(self, tmp_path):
        self._first_process(tmp_path)
        _venue2, engine2, bot2 = self._second_process(tmp_path, perp_qty=0.0)
        assert bot2.carry_cold_start() is False
        assert engine2.state is BookState.HALTED

    def test_a_halted_book_stays_halted_across_a_restart(self, tmp_path):
        venue, journal, engine = build(modern)
        store = StateStore(str(tmp_path / "carry.db"))
        engine.persist = store.save_carry_position
        bot = make_bot(venue, journal, engine, store)
        for k in (1, 2, 3):
            tick(bot, k)
        venue.perp_qty = 0.6
        tick(bot, 4)
        assert engine.state is BookState.HALTED
        store.close()
        _venue2, engine2, bot2 = self._second_process(tmp_path, perp_qty=0.6)
        assert bot2.carry_cold_start() is False


# ---------------------------------------------------------------------------
# THE ACCOUNTING CONTRACT
#
#   net_basis == "confirmed"  <=>  every fill's fee is venue-stated
#                                  AND no funding entry is an estimate,
#                                      unattributed or unread
#                                  AND every position lifecycle's LATEST
#                                      reconciliation record is FULLY_RECONCILED,
#                                      made against complete reads, not
#                                      contradicted since, and (when open)
#                                      younger than RECORD_MAX_AGE_MS.
#
# "The reconciliation ran" is not "funding is confirmed": a run over an
# unreadable, incomplete, contradicted or stale read is PROVISIONAL.
# `funding_confirmed` and `funding_reconciled` are the two halves of that rule
# and `carry_status()` must report them from the same statement, with a clock.
# ---------------------------------------------------------------------------


def _assert_contract(acct):
    confirmed = (acct["fees_confirmed"] and acct["funding_confirmed"]
                 and acct["funding_reconciled"])
    assert (acct["net_basis"] == "confirmed") == bool(confirmed), acct
    if acct["funding_confirmed"]:
        assert acct["funding_reconciled"]
        assert acct["funding_unread"] == 0 and acct["funding_unattributed"] == 0
    if acct["net_basis"] == "confirmed":
        assert acct["funding_estimated_entries"] == 0


class TestTheFundingAccountingContract:
    def _status(self, bot):
        bot.cfg = mock.Mock(USE_TESTNET=True, PAPER_TRADING=False)
        return bot.carry_status()["accounting"]

    def test_a_reconciliation_window_never_ends_before_the_lifecycle_began(
            self, tmp_path):
        """The exact condition that made the status flap: the wall clock one
        millisecond behind the journal stamp of the opening fill."""
        import time as _time
        venue, journal, engine, bot = _hedged(tmp_path)
        start = min(e.ms for e in journal.entries if e.kind == "perp_open")
        # drop the record the tick wrote, then reconcile with the clock behind
        journal.entries[:] = [e for e in journal.entries
                              if e.kind != "funding_reconciliation"]
        bot._last_reconcile_ms = bot._last_funding_poll_ms = 0
        behind = (start - 1) / 1000.0
        with mock.patch.object(_time, "time", lambda: behind):
            bot._book_carry_funding(journal, "BTCUSDT",
                                    mock.Mock(detail={}, action="hold"))
        rec = [e.fee_raw for e in journal.entries
               if e.kind == "funding_reconciliation"][-1]
        assert "FUNDING_HISTORY_UNREADABLE" not in rec["reasons"], rec
        assert rec["state"] == "FULLY_RECONCILED"
        assert rec["as_of_ms"] >= start
        _assert_contract(self._status(bot))

    def test_an_unreadable_account_log_is_provisional_not_confirmed(
            self, tmp_path):
        venue, journal, engine, bot = _hedged(tmp_path)
        journal.entries[:] = [e for e in journal.entries
                              if e.kind != "funding_reconciliation"]
        venue.txlog_fail = True
        bot._last_reconcile_ms = bot._last_funding_poll_ms = 0
        bot._book_carry_funding(journal, "BTCUSDT",
                                mock.Mock(detail={}, action="hold"))
        acct = self._status(bot)
        _assert_contract(acct)
        assert acct["net_basis"] == "provisional_funding_unverified"
        assert acct["funding_confirmed"] is False
        assert acct["funding_unread"] >= 1

    def test_no_reconciliation_record_at_all_is_provisional(self, tmp_path):
        venue, journal, engine, bot = _hedged(tmp_path)
        journal.entries[:] = [e for e in journal.entries
                              if e.kind != "funding_reconciliation"]
        acct = self._status(bot)
        _assert_contract(acct)
        assert acct["net_basis"] == "provisional_funding_unverified"
        assert acct["funding_reconciled"] is False

    def test_a_row_the_book_cannot_claim_contradicts_a_clean_reconciliation(
            self, tmp_path):
        import ledger as L
        venue, journal, engine, bot = _hedged(tmp_path)
        assert self._status(bot)["net_basis"] == "confirmed"
        opened = next(e for e in journal.entries if e.kind == "perp_open")
        L.book_funding_payments(journal, [{
            "id": "X1", "ms": opened.ms + 5, "usd": 3.0, "currency": "USDT",
            "size": -0.1, "symbol": "BTCUSDT"}], ms=opened.ms + 6)
        acct = self._status(bot)
        _assert_contract(acct)
        assert acct["net_basis"] == "provisional_funding_unverified"
        assert acct["funding_unattributed"] == 1
        assert acct["funding_confirmed"] is False

    def test_a_reconciliation_record_that_has_aged_out_is_not_confirmed(
            self, tmp_path):
        """carry_status used to ask for a statement WITHOUT a clock, so an open
        lifecycle's record never aged and /health said confirmed forever."""
        import time as _time
        import ledger as L
        venue, journal, engine, bot = _hedged(tmp_path)
        assert self._status(bot)["net_basis"] == "confirmed"
        later = _time.time() + (L.RECORD_MAX_AGE_MS / 1000.0) + 3600
        with mock.patch.object(_time, "time", lambda: later):
            acct = self._status(bot)
        _assert_contract(acct)
        assert acct["net_basis"] == "provisional_funding_unverified"
        assert acct["funding_reconciled"] is False

    def test_a_book_that_never_traded_is_no_fills_not_confirmed(self, tmp_path):
        venue, journal, engine, bot = _flat(tmp_path)
        bot.cfg = mock.Mock(USE_TESTNET=True, PAPER_TRADING=False)
        assert bot.carry_status()["accounting"]["net_basis"] == "no_fills"


class TestAnEmptyFundingAnswerSupportsZeroOnlyWhereItCanBeProven:
    """An empty public history is a COMPLETE answer only for a window too short
    to hold a settlement. Over a window the venue's own cadence says must
    contain one, empty means missing: it cannot support "funding is zero"."""

    H = 3_600_000

    def test_funding_gaps_flags_an_empty_answer_over_a_window_that_must_hold_a_print(
            self):
        from carry_broker import funding_gaps
        H = self.H
        assert funding_gaps([], start_ms=0, end_ms=6 * H) == ([], None)
        gaps, _ = funding_gaps([], start_ms=0, end_ms=20 * H)
        assert gaps == [(0, 20 * H)]
        # one print cannot vouch for a 20h window either
        gaps, _ = funding_gaps([10 * H], start_ms=0, end_ms=20 * H)
        assert gaps and gaps[0][0] == 0
        # a print at each end of an 8h cadence is whole
        assert funding_gaps([0, 8 * H], start_ms=0, end_ms=8 * H)[0] == []

    def _venue_hedged_hours_ago(self, tmp_path, hours):
        venue, journal, engine = build(modern, clock_offset_ms=-hours * self.H)
        store = StateStore(str(tmp_path / "carry.db"))
        bot = make_bot(venue, journal, engine, store)
        for k in (1, 2, 3):
            tick(bot, k)
        journal.entries[:] = [e for e in journal.entries
                              if e.kind != "funding_reconciliation"]
        bot.cfg = mock.Mock(USE_TESTNET=True, PAPER_TRADING=False)
        return venue, journal, bot

    def test_an_empty_history_over_twenty_hours_is_provisional_not_confirmed(
            self, tmp_path):
        venue, journal, bot = self._venue_hedged_hours_ago(tmp_path, 20)
        venue.prints = []                    # the venue "returns nothing"
        bot._last_reconcile_ms = bot._last_funding_poll_ms = 0
        bot._book_carry_funding(journal, "BTCUSDT",
                                mock.Mock(detail={}, action="hold"))
        rec = [e.fee_raw for e in journal.entries
               if e.kind == "funding_reconciliation"][-1]
        assert rec["state"] == "PROVISIONAL"
        assert "FUNDING_HISTORY_HAS_GAPS" in rec["reasons"]
        acct = bot.carry_status()["accounting"]
        _assert_contract(acct)
        assert acct["net_basis"] == "provisional_funding_unverified"
        assert acct["funding_confirmed"] is False

    def test_an_empty_history_over_a_short_window_still_supports_zero(
            self, tmp_path):
        venue, journal, bot = self._venue_hedged_hours_ago(tmp_path, 3)
        venue.prints = []
        bot._last_reconcile_ms = bot._last_funding_poll_ms = 0
        bot._book_carry_funding(journal, "BTCUSDT",
                                mock.Mock(detail={}, action="hold"))
        acct = bot.carry_status()["accounting"]
        _assert_contract(acct)
        assert acct["net_basis"] == "confirmed" and acct["funding_usd"] == 0
