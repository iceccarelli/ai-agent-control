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
        assert acct["net_basis"] == "provisional_funding_unverified"
        assert acct["funding_confirmed"] is False
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
