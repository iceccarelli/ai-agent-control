"""The journal is written whenever the engine writes the position.

`tick()` wrote the double-entry journal AFTER the engine returned, while the
engine wrote its position (`persist`) the moment each leg landed. A hard kill
(Fly restart, OOM) between the fill and the end of the tick left a position
the next start resumed against a journal that had never seen the fill: no
entry fees, no perp/inventory lines, and a later close with nothing to close.
The engine's own persist now writes the journal first, so the two cannot
disagree, and a journal that cannot be written halts the book like any other
ledger write failure.
"""
from __future__ import annotations

import inspect
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import ledger as L  # noqa: E402
import main as _main  # noqa: E402
from carry_engine import OVERLAY, BookState  # noqa: E402
from persistence import StateStore  # noqa: E402
from test_carry_cold_start import H8, MARK, Venue, engine  # noqa: E402


def wired(tmp_path, store=None):
    store = store or StateStore(str(tmp_path / "s.db"))
    bot = _main.TradingBot.__new__(_main.TradingBot)
    bot.store = store
    bot.carry_journal = L.Journal()
    broker = L.LedgerBroker(Venue(inventory=5.0), bot.carry_journal,
                            ms=lambda: 1_000, spot_symbol="BTCUSDT")
    e = engine(broker, mode=OVERLAY, persist=bot._persist_carry)
    return bot, store, e


def test_a_kill_right_after_the_fill_loses_no_accounting(tmp_path):
    bot, store, e = wired(tmp_path)
    d = e.on_candle(mark=MARK, funding_bps=3.0, spot=MARK,
                    funding_print_ms=3 * H8, timestamp_ms=3 * H8)
    assert d.action == "opened", d.reason
    # NO tick() epilogue ran: this is the process dying here.
    rows = store.load_ledger()
    assert rows, "the position was saved but its journal never was"
    reloaded = L.Journal.from_rows(rows)
    # the journal carries a short as negative quantity
    assert reloaded.statement()["open_perp_qty"] == pytest.approx(
        -e.position.perp.filled_qty)
    assert store.load_carry_position()["book_state"] == "HEDGED"


def test_a_journal_that_cannot_be_written_halts_the_book(tmp_path):
    bot, store, e = wired(tmp_path)

    def broken(_rows):
        raise RuntimeError("disk full")
    store.save_ledger = broken
    d = e.on_candle(mark=MARK, funding_bps=3.0, spot=MARK,
                    funding_print_ms=3 * H8, timestamp_ms=3 * H8)
    assert d.state is BookState.HALTED
    assert d.reason == "LEDGER_WRITE_FAILED"


def test_the_journal_is_written_before_the_position(tmp_path):
    order = []
    bot, store, e = wired(tmp_path)
    real_l, real_p = store.save_ledger, store.save_carry_position
    store.save_ledger = lambda r: (order.append("journal"), real_l(r))[1]
    store.save_carry_position = lambda s: (order.append("position"),
                                           real_p(s))[1]
    e.on_candle(mark=MARK, funding_bps=3.0, spot=MARK,
                funding_print_ms=3 * H8, timestamp_ms=3 * H8)
    assert order and order[-1] == "position"
    assert order[order.index("position") - 1] == "journal"


def test_a_book_without_a_journal_still_persists_its_position(tmp_path):
    bot, store, e = wired(tmp_path)
    bot.carry_journal = None
    bot._persist_carry({"book_state": "FLAT", "position": None})
    assert store.load_carry_position()["book_state"] == "FLAT"


def test_build_bot_wires_the_engine_to_it():
    src = inspect.getsource(_main.build_bot)
    assert "persist=bot._persist_carry" in src
