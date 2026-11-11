"""Cash the ledger cannot explain stops entries, and the stop must not depend on a second write surviving.

Before: ``CapitalBook.settle`` booked the session, cleared ``pending`` and saved. Only THEN did the autopilot write the ALERT marker (a separate file). A process killed between the two
(a Fly restart, an OOM kill, a power cut) left a book that had already moved on and no marker: the next session's ``settle`` said NO_PENDING, nothing asked for a human, and the autopilot
traded on an account whose cash it had flagged as unexplained. Specialist E reproduced the window; this file reproduces it with the real book and the real autopilot and pins the fix.

After: the settlement that is UNEXPLAINED_CASH writes ``review_required`` into the book IN THE SAME atomic save. Every later session reads it from the book (the source of truth), re-asserts the
ALERT, and refuses; clearing the ALERT alone does not resume trading; only the owner's explicit ``capital --ack-review`` clears the book's flag. Nothing here credits, debits or hides money.
"""
from __future__ import annotations

import json
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
BOT = os.path.dirname(HERE)
for _p in (HERE, BOT, os.path.join(BOT, "tools")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import ms_capital as CC  # noqa: E402
import ms_testnet_autopilot as AP  # noqa: E402
from test_ms_testnet_autopilot import _cycle_eng, _profit_world, w  # noqa: E402,F401  (the fake venue world the autopilot tests already own)


def _unexplained(book):
    """A session whose ledger knew +0.50 but whose cash moved by thousands of dollars: UNEXPLAINED_CASH, funding read."""
    book.ensure_baseline("1")
    assert book.park("prev_session", "1", "0.50", True, True)


# ------------------------------------------------------------------ the book
def test_an_unexplained_settlement_puts_the_review_in_the_book_in_the_same_save(tmp_path):
    book = CC.CapitalBook(str(tmp_path))
    _unexplained(book)
    r = book.settle("10000")
    assert r["status"] == "UNEXPLAINED_CASH"
    saved = json.load(open(book.path))
    assert saved["pending"] is None and saved["history"][-1]["status"] == "UNEXPLAINED_CASH"
    rr = saved["review_required"]
    assert rr["code"] == "ACCOUNT_CASH_UNEXPLAINED" and rr["session_id"] == "prev_session" and rr["delta_usd"] == r["delta_usd"] and rr["known_usd"] == "0.50"
    assert book.report()["review_required"]["code"] == "ACCOUNT_CASH_UNEXPLAINED"


def test_a_settled_session_leaves_no_review(tmp_path):
    book = CC.CapitalBook(str(tmp_path))
    book.ensure_baseline("500")
    assert book.park("s1", "500", "0.50", True, True)
    assert book.settle("500.50")["status"] == "SETTLED"
    assert book.load()["review_required"] is None


def test_a_book_written_before_the_field_existed_loads_with_no_review(tmp_path):
    book = CC.CapitalBook(str(tmp_path))
    book.ensure_baseline("500")
    d = json.load(open(book.path))
    d.pop("review_required", None)
    json.dump(d, open(book.path, "w"))
    assert CC.CapitalBook(str(tmp_path)).load()["review_required"] is None


def test_the_first_review_is_kept_when_a_second_unexplained_session_follows(tmp_path):
    book = CC.CapitalBook(str(tmp_path))
    _unexplained(book)
    book.settle("10000")
    first = book.load()["review_required"]
    assert book.park("second_session", "10000", "0.50", True, True)
    assert book.settle("20000")["status"] == "UNEXPLAINED_CASH"
    assert book.load()["review_required"] == first, "the evidence that raised the stop is the first session's, not overwritten by a later one"


def test_acknowledging_the_review_clears_only_the_review_and_changes_no_money(tmp_path):
    book = CC.CapitalBook(str(tmp_path))
    _unexplained(book)
    book.settle("10000")
    audit = ("review_required", "review_acknowledged")
    before = {k: v for k, v in book.load().items() if k not in audit}
    assert book.acknowledge_review() is True
    after = book.load()
    assert after["review_required"] is None and {k: v for k, v in after.items() if k not in audit} == before, "no money, no history, no baseline moved"
    assert len(after["review_acknowledged"]) == 1, "the owner's decision is itself on the record"
    assert book.acknowledge_review() is False, "nothing left to acknowledge"
    assert len(book.load()["review_acknowledged"]) == 1


# ------------------------------------------------------------------ the autopilot
def test_a_crash_between_the_settlement_save_and_the_alert_cannot_lose_the_stop(w):
    """The window: settle() has saved, the ALERT write never happened. The next session must still refuse, and must re-raise the ALERT itself."""
    _profit_world(w)
    eng = str(w.tmp / "eng")
    book = CC.CapitalBook(eng)
    _unexplained(book)
    assert book.settle("10000")["status"] == "UNEXPLAINED_CASH"            # ... and the process died here: no ALERT was written
    assert not AP.ES.EngineState(eng, w.clock.now).has("ALERT")
    w.fake.requests.clear()
    res, _ = _cycle_eng(w)
    assert res["result"] == "REFUSED" and res["why"] == ["ACCOUNT_CASH_UNEXPLAINED"], res
    assert not [r for r in w.fake.requests if r[0] == "POST"], "no order may be sent while a review is open"
    assert AP.ES.EngineState(eng, w.clock.now).has("ALERT")


def test_clearing_the_alert_alone_does_not_resume_trading(w):
    _profit_world(w)
    eng = str(w.tmp / "eng")
    book = CC.CapitalBook(eng)
    _unexplained(book)
    book.settle("10000")
    es = AP.ES.EngineState(eng, w.clock.now)
    es.set("ALERT", {"code": "ACCOUNT_CASH_UNEXPLAINED"})
    assert es.clear("ALERT", "owner cleared it without looking")
    w.fake.requests.clear()
    res, _ = _cycle_eng(w)
    assert res["result"] == "REFUSED" and res["why"] == ["ACCOUNT_CASH_UNEXPLAINED"]
    assert AP.ES.EngineState(eng, w.clock.now).has("ALERT"), "the stop re-asserts itself from the book"
    assert not [r for r in w.fake.requests if r[0] == "POST"]


def test_after_the_owners_acknowledgement_the_normal_gates_decide_again(w):
    _profit_world(w)
    eng = str(w.tmp / "eng")
    book = CC.CapitalBook(eng)
    _unexplained(book)
    book.settle("10000")
    AP.ES.EngineState(eng, w.clock.now).set("ALERT", {"code": "ACCOUNT_CASH_UNEXPLAINED"})
    assert book.acknowledge_review() and AP.ES.EngineState(eng, w.clock.now).clear("ALERT", "owner reviewed the history")
    res, _ = _cycle_eng(w)
    assert "ACCOUNT_CASH_UNEXPLAINED" not in (res.get("why") or []), res


def test_the_capital_command_shows_the_review_and_acknowledges_it_only_when_asked(tmp_path, capsys):
    eng = str(tmp_path / "eng_cli")
    book = CC.CapitalBook(eng)
    _unexplained(book)
    book.settle("10000")
    assert AP.main(["capital", "--root", eng], env={}) == 0
    out = capsys.readouterr().out
    assert '"review_required"' in out and "ACCOUNT_CASH_UNEXPLAINED" in out
    assert CC.CapitalBook(eng).load()["review_required"] is not None, "printing the book must not clear anything"
    assert AP.main(["capital", "--root", eng, "--ack-review"], env={}) == 0
    out = capsys.readouterr().out
    assert "review acknowledged" in out and CC.CapitalBook(eng).load()["review_required"] is None
    assert AP.main(["capital", "--root", eng, "--ack-review"], env={}) == 0
    assert "no review to acknowledge" in capsys.readouterr().out
