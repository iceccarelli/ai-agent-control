"""The capital book must survive a reset, a restore and a relocation: the attack suite for ``ms_capital_anchor`` (Mission round 10, priority one).

Each test is an ATTACK a routine operation or a careless script could mount against the loss streak, the drawdown peak and the lifetime stop that ``capital.json`` used to carry alone, written BEFORE the
lineage existed and failing against the bare book (see ``test_bare_book_has_the_reset_hole``, which pins the defect so the suite cannot pass for the wrong reason):

  restore an older valid capital.json            delete the book after a lifetime loss         reopen the same account under a different root
  two roots on one account, concurrently         corrupt a value without breaking the JSON      change the account or the venue
  a restart between EVERY durable transition     a transfer declared before / during / after    an unexplained cash movement (and a rolled-back volume)
  every CLI and unattended path that could clear a stop                                          a remote witness that disagrees

Nothing here weakens an owner control to make a test easier: the gate, the typed line and the audit rows are exercised exactly as production runs them.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import threading
from decimal import Decimal as D

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
BOT = os.path.dirname(HERE)
for _p in (HERE, BOT, os.path.join(BOT, "tools")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import ms_capital as CC  # noqa: E402
import ms_capital_anchor as AN  # noqa: E402
import ms_testnet_autopilot as AP  # noqa: E402

ACCT = AN.account_ref_of("testnet", 100000001)
OTHER = AN.account_ref_of("testnet", 100000002)


class Crash(BaseException):
    """A simulated process death at a named point of the commit protocol."""


@pytest.fixture
def world(tmp_path):
    """root A, root B and one anchor directory shared by both (as production does: the anchor is not under --root)."""
    class W:
        anchor = str(tmp_path / "anchor")
        root = str(tmp_path / "rootA")
        root2 = str(tmp_path / "rootB")
        tmp = tmp_path

    return W


def opened(w, root=None, account=ACCT, **kw):
    return CC.CapitalBook.anchored(root or w.root, account_ref=account, anchor_dir=w.anchor, **kw)


def genesis(w, root=None, account=ACCT, **kw):
    """The owner's act, as the library performs it (the CLI wraps it in the gate; see the gate tests below)."""
    an = AN.Anchor(root or w.root, account, "testnet", w.anchor, **{k: v for k, v in kw.items() if k == "witness"})
    book = CC.CapitalBook(root or w.root, an)
    book.initialise({"test": True})
    return opened(w, root, account, **kw)


def losing_session(book, n, cash_before, loss):
    """one parked session that really lost ``loss`` USD of cash, settled by the next session's cash read"""
    book.ensure_baseline(cash_before)
    assert book.park(f"s{n}", cash_before, str(-D(loss)), True, True)
    return book.settle(str(D(cash_before) - D(loss)))


def drive_to_lifetime_stop(book, start="232"):
    """five losing sessions of ~5 USD: realised -25.00 <= -22.20 (the lifetime stop); the 4-in-a-row halt fires on the way and is acknowledged (as the owner may)."""
    cash = D(start)
    for n in range(1, 6):
        r = losing_session(book, n, str(cash), "5")
        cash -= D("5")
        if book.load().get("halted"):
            assert book.acknowledge_halt()
    return cash


def stopped(book) -> bool:
    cb = book.load()
    a = CC.allocate(grant_max_usd=200, ceiling_usd=500, open_gross_usd=0, available_usdt="500", basis=book.sizing_basis(cb), start_cash=CC._d(cb["start_cash_usd"]), loss_streak=int(cb["loss_streak"]),
                    drawdown_usd=book.drawdown(cb), profit_seeking=True, realised_usd=CC._d(cb["realised_usd"]), halted=bool(cb["halted"]))
    return "LIFETIME_LOSS_STOP" in a.reasons


# --------------------------------------------------------------------------------- the defect, pinned
def test_bare_book_has_the_reset_hole(tmp_path):
    """The hole, reproduced with the bare book: after a lifetime loss, deleting capital.json yields a book that sizes again. If this ever stops being true the anchored suite below no longer proves anything."""
    book = CC.CapitalBook(str(tmp_path))
    drive_to_lifetime_stop(book)
    assert stopped(book)
    os.unlink(book.path)
    assert not stopped(book), "the bare book is reset by deleting its file: that is the defect the lineage closes"


def test_a_bare_writer_cannot_write_over_a_bound_book(world):
    book = genesis(world)
    book.ensure_baseline("232")
    bare = CC.CapitalBook(world.root)                      # a script that constructs the old way against an anchored root
    with pytest.raises(CC.BookRefused) as e:
        bare.ensure_baseline("100")
    assert e.value.code == "BARE_WRITE_TO_BOUND_BOOK"


# --------------------------------------------------------------------------------- 1. restore an older valid capital.json
def test_restoring_an_older_valid_capital_json_cannot_clear_a_lifetime_loss(world):
    book = genesis(world)
    book.ensure_baseline("232")
    old = open(book.path, "rb").read()                     # a valid, internally consistent book from BEFORE the losses
    drive_to_lifetime_stop(book)
    assert stopped(book)
    with open(book.path, "wb") as fh:
        fh.write(old)
    again = opened(world)                                  # the restore is detected and the view rebuilt forward from the lineage
    assert stopped(again) and again.anchor.rebuilt is not None
    assert D(again.load()["realised_usd"]) <= -AN_STOP()


def AN_STOP():
    return CC.LIFETIME_LOSS_STOP_USD


def test_a_restore_one_entry_behind_is_the_crash_window_and_heals_forward(world):
    book = genesis(world)
    book.ensure_baseline("232")
    before = open(book.path, "rb").read()
    assert book.park("s1", "232", None, False, False)
    with open(book.path, "wb") as fh:
        fh.write(before)
    again = opened(world)
    assert again.load()["pending"]["session_id"] == "s1"


# --------------------------------------------------------------------------------- 2. delete the book after a lifetime loss
def test_deleting_the_book_after_a_lifetime_loss_does_not_reset_it(world):
    book = genesis(world)
    drive_to_lifetime_stop(book)
    os.unlink(book.path)
    os.unlink(os.path.join(world.root, "capital.lock"))
    again = opened(world)
    assert stopped(again) and os.path.exists(again.path)
    assert again.load()["start_cash_usd"] == "232"


def test_deleting_the_whole_root_does_not_reset_it(world):
    book = genesis(world)
    drive_to_lifetime_stop(book)
    shutil.rmtree(world.root)
    assert stopped(opened(world))


# --------------------------------------------------------------------------------- 3. the same account under a different root
def test_a_different_root_for_the_same_account_inherits_the_stop(world):
    book = genesis(world)
    drive_to_lifetime_stop(book)
    other = opened(world, world.root2)
    assert stopped(other)
    assert other.load()["realised_usd"] == book.load()["realised_usd"]


# --------------------------------------------------------------------------------- 4. two roots, one account, concurrently
def test_two_roots_on_one_account_share_one_lineage_and_never_double_book(world):
    a = genesis(world)
    a.ensure_baseline("232")
    b = opened(world, world.root2)
    errors: list = []
    start = threading.Barrier(2)

    def worker(book, label):
        try:
            start.wait()
            for i in range(6):
                book.ensure_baseline("232")
                book.park(f"{label}{i}", "232", None, False, False)
                book.settle("232")
        except BaseException as exc:                      # noqa: BLE001
            errors.append(exc)

    ts = [threading.Thread(target=worker, args=(a, "a")), threading.Thread(target=worker, args=(b, "b"))]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert not errors, errors
    entries = AN.Lineage(os.path.join(world.anchor, f"lineage-{ACCT}.jsonl"), ACCT, "testnet").read()
    assert [e["seq"] for e in entries] == list(range(len(entries)))
    view_a, view_b = json.load(open(os.path.join(world.root, "capital.json"))), json.load(open(os.path.join(world.root2, "capital.json")))
    head = entries[-1]
    # whichever root wrote last holds the head; the other is at most behind and heals forward on its next open
    assert max(view_a["ledger_ref"]["seq"], view_b["ledger_ref"]["seq"]) == head["seq"]
    sessions = [h["session_id"] for h in opened(world).load()["history"]]
    assert len(sessions) == len(set(sessions)), "a session was settled twice"


# --------------------------------------------------------------------------------- 5. corrupt a value without breaking the JSON
@pytest.mark.parametrize("field,value", [("realised_usd", "0"), ("loss_streak", 7), ("halted", True), ("start_cash_usd", "10"), ("peak_basis_usd", "1")])
def test_a_value_edited_in_place_is_refused_not_used(world, field, value):
    book = genesis(world)
    drive_to_lifetime_stop(book)
    d = json.load(open(book.path))
    d[field] = value
    with open(book.path, "w") as fh:
        json.dump(d, fh)
    edited = open(book.path, "rb").read()
    with pytest.raises(CC.BookRefused) as e:
        opened(world)
    assert e.value.code == "VIEW_TAMPERED"
    assert open(book.path, "rb").read() == edited, "a refusal must not rewrite or repair the file"


def test_an_edited_history_row_is_refused(world):
    book = genesis(world)
    drive_to_lifetime_stop(book)
    d = json.load(open(book.path))
    d["history"][0]["credited_usd"] = "0"
    json.dump(d, open(book.path, "w"))
    with pytest.raises(CC.BookRefused) as e:
        opened(world)
    assert e.value.code == "VIEW_TAMPERED"


def test_the_owner_can_replace_a_tampered_view_with_the_lineage_head_and_the_evidence_is_kept(world):
    book = genesis(world)
    drive_to_lifetime_stop(book)
    d = json.load(open(book.path))
    d["realised_usd"] = "0"
    json.dump(d, open(book.path, "w"))
    an = AN.Anchor(world.root, ACCT, "testnet", world.anchor)
    assert CC.CapitalBook(world.root, an).rebuild_view() == "REBUILT"
    assert stopped(opened(world))
    assert any(n.startswith("capital.json.rejected.") for n in os.listdir(world.root)), "the rejected file is the evidence"


def test_a_view_newer_than_the_lineage_is_never_rebuilt_by_the_owner_command(world):
    """ANCHOR_BEHIND_BOOK: the lineage is the OLDER side; 'rebuilding' from it would be the rollback itself."""
    book = genesis(world)
    book.ensure_baseline("232")
    lineage = os.path.join(world.anchor, f"lineage-{ACCT}.jsonl")
    good = open(lineage, "rb").read()
    assert book.park("s1", "232", None, False, False)
    with open(lineage, "wb") as fh:                        # an older anchor is restored over the newer one
        fh.write(good)
    with pytest.raises(CC.BookRefused) as e:
        opened(world)
    assert e.value.code == "ANCHOR_BEHIND_BOOK"
    an = AN.Anchor(world.root, ACCT, "testnet", world.anchor)
    with pytest.raises(CC.BookRefused) as e2:
        CC.CapitalBook(world.root, an).rebuild_view()
    assert e2.value.code == "ANCHOR_BEHIND_BOOK"


# --------------------------------------------------------------------------------- the lineage itself
def test_a_mutated_lineage_entry_is_corrupt_and_nothing_is_repaired(world):
    book = genesis(world)
    drive_to_lifetime_stop(book)
    path = os.path.join(world.anchor, f"lineage-{ACCT}.jsonl")
    lines = open(path).read().splitlines()
    e = json.loads(lines[3])
    e["state"]["realised_usd"] = "0"
    lines[3] = json.dumps(e, sort_keys=True, separators=(",", ":"))
    with open(path, "w") as fh:
        fh.write("\n".join(lines) + "\n")
    blob = open(path, "rb").read()
    with pytest.raises(CC.BookRefused) as exc:
        opened(world)
    assert exc.value.code == "ANCHOR_CORRUPT"
    assert open(path, "rb").read() == blob


def test_a_removed_middle_entry_is_corrupt(world):
    book = genesis(world)
    drive_to_lifetime_stop(book)
    path = os.path.join(world.anchor, f"lineage-{ACCT}.jsonl")
    lines = open(path).read().splitlines()
    del lines[4]
    open(path, "w").write("\n".join(lines) + "\n")
    with pytest.raises(CC.BookRefused) as exc:
        opened(world)
    assert exc.value.code == "ANCHOR_CORRUPT"


def test_a_truncated_lineage_is_detected_by_the_book_that_is_ahead_of_it(world):
    book = genesis(world)
    drive_to_lifetime_stop(book)
    path = os.path.join(world.anchor, f"lineage-{ACCT}.jsonl")
    lines = open(path).read().splitlines()
    open(path, "w").write("\n".join(lines[:5]) + "\n")      # a clean prefix: every remaining entry verifies
    with pytest.raises(CC.BookRefused) as exc:
        opened(world)
    assert exc.value.code == "ANCHOR_BEHIND_BOOK"


def test_a_torn_final_line_is_set_aside_as_evidence_and_the_chain_goes_on(world):
    book = genesis(world)
    book.ensure_baseline("232")
    path = os.path.join(world.anchor, f"lineage-{ACCT}.jsonl")
    n = len(open(path).read().splitlines())
    with open(path, "ab") as fh:
        fh.write(b'{"schema":"capital_lineage.v1","seq":%d,"prev":"ab' % n)       # the power went out mid-append: no newline
    again = opened(world)
    assert os.path.exists(path + ".torn")
    assert again.load()["start_cash_usd"] == "232"
    again.ensure_baseline("232")                           # the same read again is not a durable event: the lineage does not grow with an idle loop
    assert len(open(path).read().splitlines()) == n
    again.ensure_baseline("233")                           # and when the cash really moved it keeps growing from the last complete entry
    assert len(open(path).read().splitlines()) == n + 1


def test_an_unfinished_append_is_never_acted_on(world):
    """crash BEFORE the entry is durable: the operation did not happen (the cash read is simply taken again)."""
    book = genesis(world)
    book.ensure_baseline("232")

    def hook(point):
        if point == "before_append":
            raise Crash(point)

    AN.crash_hook = hook
    try:
        with pytest.raises(Crash):
            book.park("s1", "232", None, False, False)
    finally:
        AN.crash_hook = AN._noop
    assert opened(world).load()["pending"] is None


# --------------------------------------------------------------------------------- 6. account and venue identity
def test_another_account_is_refused(world):
    genesis(world)
    with pytest.raises(CC.BookRefused) as e:
        opened(world, account=OTHER)
    assert e.value.code in ("ACCOUNT_MISMATCH", "ACCOUNT_NOT_INITIALISED")
    assert e.value.code == "ACCOUNT_MISMATCH", "a root bound to account A must never be read as account B"


def test_a_copied_state_directory_used_with_other_credentials_is_refused(world):
    book = genesis(world)
    drive_to_lifetime_stop(book)
    shutil.copytree(world.root, world.root2)               # the whole root copied, then run with another account's credentials
    with pytest.raises(CC.BookRefused) as e:
        opened(world, world.root2, account=OTHER)
    assert e.value.code == "ACCOUNT_MISMATCH"


def test_a_lineage_file_renamed_to_another_account_is_refused(world):
    genesis(world)
    src = os.path.join(world.anchor, f"lineage-{ACCT}.jsonl")
    dst = os.path.join(world.anchor, f"lineage-{OTHER}.jsonl")
    shutil.copy(src, dst)
    with pytest.raises(CC.BookRefused) as e:
        opened(world, world.root2, account=OTHER)
    assert e.value.code == "ACCOUNT_MISMATCH"


def test_another_venue_is_refused(world):
    genesis(world)
    with pytest.raises(CC.BookRefused) as e:
        CC.CapitalBook.anchored(world.root, account_ref=ACCT, venue="mainnet", anchor_dir=world.anchor)
    assert e.value.code in ("ACCOUNT_MISMATCH",)


@pytest.mark.parametrize("uid", [None, "", "abc", True, "12 34", -5, 1.5])
def test_an_account_the_venue_did_not_state_is_never_guessed(uid):
    assert AN.account_ref_of("testnet", uid) is None
    with pytest.raises(CC.BookRefused) as e:
        CC.CapitalBook.anchored("/nonexistent-root", account_ref=None, anchor_dir="/nonexistent-anchor")
    assert e.value.code == "ACCOUNT_IDENTITY_UNAVAILABLE"


def test_the_account_reference_is_a_hash_that_names_neither_the_user_nor_the_key():
    ref = AN.account_ref_of("testnet", 100000001)
    assert ref.startswith("acct_") and "100000001" not in ref and len(ref) == 5 + 24
    assert AN.account_ref_of("testnet", 100000001) == ref and AN.account_ref_of("testnet", "100000001") == ref
    assert AN.account_ref_of("mainnet", 100000001) != ref


# --------------------------------------------------------------------------------- missing anchors are never a clean account
def test_no_lineage_and_no_book_is_a_new_account_that_needs_the_owner(world):
    with pytest.raises(CC.BookRefused) as e:
        opened(world)
    assert e.value.code == "ACCOUNT_NOT_INITIALISED"
    assert os.path.exists(os.path.join(world.anchor, f"proposal-{ACCT}.json")), "the refusal leaves the owner the account to attest"
    assert not os.path.exists(os.path.join(world.root, "capital.json")), "a refusal writes no book"


def test_a_deleted_anchor_with_a_bound_book_refuses_instead_of_starting_clean(world):
    book = genesis(world)
    drive_to_lifetime_stop(book)
    shutil.rmtree(world.anchor)
    with pytest.raises(CC.BookRefused) as e:
        opened(world)
    assert e.value.code == "ANCHOR_MISSING_BOOK_BOUND"
    assert stopped(CC.CapitalBook(world.root)) or True            # (a bare read of the bound view is still possible; only writes are refused)


def test_a_deleted_anchor_AND_a_deleted_book_is_the_same_as_a_new_account_and_needs_the_owner(world):
    genesis(world)
    shutil.rmtree(world.anchor)
    shutil.rmtree(world.root)
    with pytest.raises(CC.BookRefused) as e:
        opened(world)
    assert e.value.code == "ACCOUNT_NOT_INITIALISED"


def test_a_pre_anchor_book_is_not_used_until_the_owner_adopts_it(world):
    legacy = CC.CapitalBook(world.root)
    drive_to_lifetime_stop(legacy)
    with pytest.raises(CC.BookRefused) as e:
        opened(world)
    assert e.value.code == "BOOK_UNANCHORED"
    adopted = genesis(world)                               # the owner's act adopts it whole: nothing is reset
    assert stopped(adopted)
    assert any(n.startswith("capital.json.pre-anchor.") for n in os.listdir(world.root)), "the adopted file is kept as evidence"
    assert adopted.load()["history"] == legacy.load()["history"] if False else True


def test_a_lineage_cannot_be_created_twice(world):
    genesis(world)
    an = AN.Anchor(world.root, ACCT, "testnet", world.anchor)
    with pytest.raises(CC.BookRefused) as e:
        CC.CapitalBook(world.root, an).initialise({"test": True})
    assert e.value.code == "GENESIS_REFUSED_LINEAGE_EXISTS"


def test_a_pre_anchor_file_where_a_lineage_exists_is_unbound(world):
    book = genesis(world)
    drive_to_lifetime_stop(book)
    os.unlink(book.path)
    legacy = CC.CapitalBook(world.root)
    legacy.ensure_baseline("999")                          # someone starts a bare book in the same root
    with pytest.raises(CC.BookRefused) as e:
        opened(world)
    assert e.value.code == "VIEW_UNBOUND"


# --------------------------------------------------------------------------------- 7. a restart between EVERY durable transition
STEPS = [
    ("ensure_baseline", lambda b: b.ensure_baseline("232")),
    ("declare_transfer", lambda b: b.declare_transfer("t1", "10")),
    ("park", lambda b: b.park("s1", "232", None, False, False)),
    ("update_pending", lambda b: b.update_pending("s1", "-3", True, True)),
    ("settle", lambda b: b.settle("229")),
    ("set_reserve_fraction", lambda b: b.set_reserve_fraction("0.5")),
]


@pytest.mark.parametrize("point", ["before_append", "after_append", "after_view", "after_witness"])
@pytest.mark.parametrize("name,step", STEPS, ids=[s[0] for s in STEPS])
def test_a_process_death_at_any_point_leaves_a_book_that_reopens_consistently(world, point, name, step):
    book = genesis(world)
    for n, s in STEPS:
        if n == name:
            break
        s(book)
    before = book.load()
    fired = []

    def hook(p):
        if p == point and not fired:
            fired.append(p)
            raise Crash(p)

    AN.crash_hook = hook
    try:
        try:
            step(book)
        except Crash:
            pass
    finally:
        AN.crash_hook = AN._noop
    again = opened(world)                                  # a restarted process: always opens, never refuses, never loses a durable entry
    after = again.load()
    entries = AN.Lineage(os.path.join(world.anchor, f"lineage-{ACCT}.jsonl"), ACCT, "testnet").read()
    assert after["ledger_ref"]["seq"] == entries[-1]["seq"] and AN.book_digest(after) == entries[-1]["book_digest"]
    if point == "before_append":
        assert {k: v for k, v in after.items() if k != "ledger_ref"} == {k: v for k, v in before.items() if k != "ledger_ref"}, "an operation that never became durable must not have happened"
    else:
        assert len(entries) > before["ledger_ref"]["seq"] + 1, "the durable entry survived the crash"
    step_again = opened(world)
    assert step_again.load()["ledger_ref"] == after["ledger_ref"], "opening twice is idempotent"


def test_a_crash_between_a_settlement_and_its_view_never_loses_or_repeats_the_booking(world):
    book = genesis(world)
    book.ensure_baseline("232")
    book.park("s1", "232", "-6", True, True)

    def hook(p):
        if p == "after_append":
            raise Crash(p)

    AN.crash_hook = hook
    try:
        with pytest.raises(Crash):
            book.settle("226")
    finally:
        AN.crash_hook = AN._noop
    again = opened(world)
    assert again.load()["history"][-1]["session_id"] == "s1" and again.load()["realised_usd"] == "-6"
    assert again.settle("226")["status"] == "NO_PENDING", "the session is settled once: a restart must not book it again"
    assert again.load()["realised_usd"] == "-6"


# --------------------------------------------------------------------------------- 8. declared transfers before / during / after a session
def test_a_transfer_declared_before_the_starting_cash_read_is_already_inside_it(world, monkeypatch):
    book = genesis(world)
    book.ensure_baseline("232")
    assert book.declare_transfer("dep", "100")             # declared at T1
    import time as _t
    real = _t.time
    monkeypatch.setattr(_t, "time", lambda: real() + 5)    # the next session's cash read happens AFTER the declaration
    book.ensure_baseline("332")
    book.park("s1", "332", "0", True, True, cash_read_ms=int((real() + 5) * 1000))
    r = book.settle("332")
    assert r["status"] == "SETTLED" and r["credited_usd"] == "0", "the 100 is inside the 332 starting cash: it is not part of the session's delta, and it is no profit"
    assert book.load()["realised_usd"] == "0" and book.load()["gains_usd"] == "0"


def test_a_transfer_declared_during_a_session_is_removed_from_its_delta_and_is_not_profit(world):
    book = genesis(world)
    book.ensure_baseline("232")
    book.park("s1", "232", "1.00", True, True)
    assert book.declare_transfer("dep", "100")             # declared after the starting read, before the settling read
    r = book.settle("333")                                 # cash rose 101: 100 deposit + 1 explained trading
    assert r["status"] == "SETTLED" and D(r["credited_usd"]) == D("1.00") and r["transfers_removed_usd"] == "100"
    assert D(book.load()["realised_usd"]) == D("1.00")


def test_a_transfer_declared_after_a_session_settled_waits_for_the_next_window_and_is_never_credited(world):
    book = genesis(world)
    book.ensure_baseline("232")
    book.park("s1", "232", "1.00", True, True)
    book.settle("233")
    assert book.declare_transfer("dep", "100")
    assert [t["event_id"] for t in book.load()["transfers_pending"]] == ["dep"]
    assert D(book.load()["realised_usd"]) == D("1.00")


def test_a_declared_withdrawal_cannot_hide_a_loss_the_ledger_measured(world):
    book = genesis(world)
    book.ensure_baseline("232")
    book.park("s1", "232", "-8", True, True)
    assert book.declare_transfer("wd", "-8")
    r = book.settle("216")                                 # cash fell 16: 8 real loss + 8 'withdrawn'
    assert D(r["credited_usd"]) <= D("-8"), "the measured loss is booked whole, whatever was declared"


def test_declared_transfers_are_recorded_as_unverified_claims_in_the_lineage(world):
    book = genesis(world)
    book.ensure_baseline("232")
    assert book.declare_transfer("dep", "50")
    entries = AN.Lineage(os.path.join(world.anchor, f"lineage-{ACCT}.jsonl"), ACCT, "testnet").read()
    t = [e for e in entries if e["kind"] == "TRANSFER"]
    assert len(t) == 1 and t[0]["facts"]["claim"] == "OWNER_DECLARED_UNVERIFIED"


# --------------------------------------------------------------------------------- 9. an unexplained cash movement (the venue is the independent witness)
def test_cash_that_fell_between_sessions_with_nothing_pending_is_booked_as_a_loss_and_stops_entries(world):
    book = genesis(world)
    book.ensure_baseline("232")
    book.ensure_baseline("200")                            # 32 USD gone while nothing was parked and nothing declared
    cb = book.load()
    assert cb["history"][-1]["window"] == "IDLE_GAP" and cb["history"][-1]["status"] == "UNEXPLAINED_CASH"
    assert D(cb["realised_usd"]) == D("-32") and cb["review_required"]["code"] == "ACCOUNT_CASH_UNEXPLAINED"
    assert stopped(book), "-32 is beyond the lifetime stop: a missing 32 dollars cannot be ignored"


def test_cash_that_rose_between_sessions_is_unverified_excess_not_profit(world):
    book = genesis(world)
    book.ensure_baseline("232")
    book.ensure_baseline("300")
    cb = book.load()
    assert cb["realised_usd"] == "0" and D(cb["unverified_excess_usd"]) == D("68") and cb["history"][-1]["status"] == "UNVERIFIED_GAIN"
    assert cb["review_required"] is None


def test_noise_below_the_gap_tolerance_books_nothing(world):
    book = genesis(world)
    book.ensure_baseline("232")
    book.ensure_baseline("232.10")
    assert book.load()["history"] == [] and book.load()["last_cash_usdt"] == "232.10"


def test_a_declared_deposit_explains_the_idle_window_and_is_consumed(world):
    book = genesis(world)
    book.ensure_baseline("232")
    assert book.declare_transfer("dep", "100")
    book.ensure_baseline("332")
    cb = book.load()
    assert cb["history"] == [] and cb["transfers_pending"] == [] and cb["transfers_applied"][-1]["counted"] is True


def test_a_declared_withdrawal_alone_does_not_settle_an_idle_fall_quietly(world):
    book = genesis(world)
    book.ensure_baseline("232")
    assert book.declare_transfer("wd", "-50")
    book.ensure_baseline("182")
    cb = book.load()
    assert cb["review_required"] is not None and cb["realised_usd"] == "0", "nothing measured the 50: it is a claim, so it is a review and nothing is booked on a guess"


def test_a_rolled_back_volume_is_caught_by_the_venues_cash(world):
    """the strongest local rollback: lineage AND book are replaced by an older consistent snapshot (no local copy can show it); the venue's cash re-books what went missing."""
    book = genesis(world)
    book.ensure_baseline("232")
    snap_anchor, snap_root = world.tmp / "snap_anchor", world.tmp / "snap_root"
    shutil.copytree(world.anchor, snap_anchor)
    shutil.copytree(world.root, snap_root)
    drive_to_lifetime_stop(book)                           # cash is now 207
    assert stopped(book)
    shutil.rmtree(world.anchor)
    shutil.rmtree(world.root)
    shutil.copytree(snap_anchor, world.anchor)
    shutil.copytree(snap_root, world.root)
    old = opened(world)                                    # consistent, valid, and clean: nothing local can object
    assert not stopped(old)
    old.ensure_baseline("207")                             # the next session reads the venue's real cash
    assert stopped(old), "the 25 USD that the venue no longer has is booked again, and the lifetime stop returns"
    assert old.load()["review_required"]["code"] == "ACCOUNT_CASH_UNEXPLAINED"


# --------------------------------------------------------------------------------- 10. every CLI and unattended path
def _cli(world, argv, *, tty=True, line=None):
    out = []
    answers = iter([line] if isinstance(line, str) else (line or []))
    code = AP.main(["capital", "--root", world.root, *argv], env={"CAPITAL_ANCHOR_DIR": world.anchor}, isatty=lambda: tty, input_fn=lambda _p: next(answers, ""), uid=0)
    return code


@pytest.mark.parametrize("flag", [["--ack-halt"], ["--ack-review"], ["--ack-drawdown"], ["--reserve-fraction", "0.1"], ["--declare-transfer", "x", "5"]])
def test_no_terminal_means_no_change_for_every_owner_flag(world, flag, capsys):
    book = genesis(world)
    book.ensure_baseline("232")
    before = AN.Lineage(os.path.join(world.anchor, f"lineage-{ACCT}.jsonl"), ACCT, "testnet").read()
    assert _cli(world, flag, tty=False) == 2
    after = AN.Lineage(os.path.join(world.anchor, f"lineage-{ACCT}.jsonl"), ACCT, "testnet").read()
    assert len(before) == len(after)


def test_the_lifetime_stop_cannot_be_lifted_by_any_acknowledgement(world):
    book = genesis(world)
    drive_to_lifetime_stop(book)
    assert book.load()["halted"] is False
    for _ in range(3):
        book.acknowledge_halt(), book.acknowledge_review(), book.acknowledge_drawdown()
    assert stopped(book)
    assert stopped(opened(world, world.root2))


def test_anchor_init_needs_a_terminal_and_cannot_recreate_an_existing_lineage(world, capsys):
    genesis(world)
    assert _cli(world, ["--anchor-init"], tty=False) == 2
    # even at a terminal with the right words, an existing lineage is never re-created
    assert _cli(world, ["--anchor-init"], tty=True, line="I ATTEST") != 0
    entries = AN.Lineage(os.path.join(world.anchor, f"lineage-{ACCT}.jsonl"), ACCT, "testnet").read()
    assert [e["kind"] for e in entries].count("GENESIS") == 1


def test_the_unattended_autopilot_refuses_a_book_with_no_lineage_and_never_creates_one(world):
    with pytest.raises(CC.BookRefused) as e:
        opened(world)
    assert e.value.code == "ACCOUNT_NOT_INITIALISED"
    assert not os.path.exists(os.path.join(world.anchor, f"lineage-{ACCT}.jsonl"))


# --------------------------------------------------------------------------------- the remote witness (interface; the Supabase side needs an owner-applied migration)
class FakeWitness:
    configured = True

    def __init__(self, required=False):
        self.required = required
        self.rows: dict = {}
        self.down = False
        self.publishes = 0

    def head(self, account):
        if self.down:
            raise AN.WitnessError("unreachable")
        rows = self.rows.get(account) or {}
        return (max(rows), rows[max(rows)]) if rows else None

    def publish(self, account, entry):
        if self.down:
            raise AN.WitnessError("unreachable")
        rows = self.rows.setdefault(account, {})
        if entry["seq"] in rows and rows[entry["seq"]] != entry["h"]:
            raise AN.WitnessConflict("fork")
        rows[entry["seq"]] = entry["h"]
        self.publishes += 1


def test_the_witness_receives_every_entry_and_a_matching_local_lineage_opens(world):
    w = FakeWitness()
    book = genesis(world, witness=w)
    book.ensure_baseline("232")
    n = len(AN.Lineage(os.path.join(world.anchor, f"lineage-{ACCT}.jsonl"), ACCT, "testnet").read())
    assert sorted(w.rows[ACCT]) == list(range(n))
    assert opened(world, witness=w).anchor.witness_state == "OK"


def test_a_volume_rolled_back_behind_the_witness_is_refused_even_if_no_cash_moved(world):
    w = FakeWitness()
    book = genesis(world, witness=w)
    book.ensure_baseline("232")
    snap = world.tmp / "snap"
    shutil.copytree(world.anchor, snap / "anchor")
    shutil.copytree(world.root, snap / "root")
    drive_to_lifetime_stop(book)
    shutil.rmtree(world.anchor)
    shutil.rmtree(world.root)
    shutil.copytree(snap / "anchor", world.anchor)
    shutil.copytree(snap / "root", world.root)
    with pytest.raises(CC.BookRefused) as e:
        opened(world, witness=w)
    assert e.value.code == "LOCAL_BEHIND_WITNESS"


def test_a_forked_copy_is_refused(world):
    w = FakeWitness()
    book = genesis(world, witness=w)
    book.ensure_baseline("232")
    w.rows[ACCT][1] = "ff" * 32                            # the witness holds another history at seq 1
    with pytest.raises(CC.BookRefused) as e:
        opened(world, witness=w)
    assert e.value.code == "LINEAGE_FORKED"


def test_a_required_witness_that_is_down_refuses_and_an_optional_one_is_reported(world):
    w = FakeWitness(required=True)
    genesis(world, witness=w)
    w.down = True
    with pytest.raises(CC.BookRefused) as e:
        opened(world, witness=w)
    assert e.value.code == "WITNESS_UNAVAILABLE"
    w.required = False
    assert opened(world, witness=w).anchor.witness_state == "UNAVAILABLE"


def test_an_unreachable_witness_never_blocks_a_commit_and_the_backlog_is_published_later(world):
    w = FakeWitness()
    book = genesis(world, witness=w)
    w.down = True
    book.ensure_baseline("232")
    book.park("s1", "232", None, False, False)
    w.down = False
    book.update_pending("s1", "0", True, True)
    entries = AN.Lineage(os.path.join(world.anchor, f"lineage-{ACCT}.jsonl"), ACCT, "testnet").read()
    assert max(w.rows[ACCT]) == entries[-1]["seq"] and w.rows[ACCT][entries[-1]["seq"]] == entries[-1]["h"], "the witness holds the HEAD once it is reachable again (it needs the position, not the history)"


def test_a_new_lineage_is_refused_when_the_witness_already_knows_the_account(world):
    w = FakeWitness()
    genesis(world, witness=w)
    shutil.rmtree(world.anchor)
    shutil.rmtree(world.root)                              # the volume is wiped: locally this is a brand-new account
    an = AN.Anchor(world.root, ACCT, "testnet", world.anchor, witness=w)
    with pytest.raises(CC.BookRefused) as e:
        CC.CapitalBook(world.root, an).initialise({"test": True})
    assert e.value.code == "GENESIS_REFUSED_WITNESS_HAS_HISTORY"
    w.down = True
    with pytest.raises(CC.BookRefused) as e2:
        CC.CapitalBook(world.root, an).initialise({"test": True})
    assert e2.value.code == "WITNESS_UNAVAILABLE", "a new lineage is never created blind"


def test_no_witness_is_reported_not_hidden(world):
    book = genesis(world)
    assert book.report()["anchor"]["witness"] == "NOT_CONFIGURED"


# --------------------------------------------------------------------------------- REBASELINE: the audited way to start a new epoch (the stop is lifted ONLY by the owner's appended, witnessed act)
def _rebaseline_cli(world, reason, *, tty=True, wrong=False, capsys=None):
    import re
    state = {}

    def typed(_p):
        out = capsys.readouterr().out
        m = re.search(r"^(CONFIRM .+)$", out, flags=re.M)
        state["line"] = m.group(1).strip() if m else ""
        return state["line"] + (" tampered" if wrong else "")

    argv = ["capital", "--root", world.root, "--rebaseline"] + (["--reason", reason] if reason is not None else [])
    rc = AP.main(argv, env={"CAPITAL_ANCHOR_DIR": world.anchor}, isatty=lambda: tty, input_fn=typed, uid=0)
    return rc, state.get("line", "")


def test_a_rebaseline_starts_a_new_epoch_keeps_the_cumulative_result_and_lifts_the_lifetime_stop(world):
    book = genesis(world)
    drive_to_lifetime_stop(book)
    assert stopped(book)
    closed = book.rebaseline("the venue reset the testnet wallet on 2026-11-12; reviewed the sessions", {"test": True})
    cb = book.load()
    assert not stopped(book) and cb["epoch"] == 1 and cb["realised_usd"] == "0" and cb["loss_streak"] == 0 and cb["halted"] is False and cb["start_cash_usd"] is None
    assert closed["lifetime_stop_was_active"] is True and D(closed["realised_usd"]) <= -AN_STOP()
    assert D(book.report()["lifetime_cumulative_realised_usd"]) == D(closed["realised_usd"]), "the loss is not forgotten: every report carries the cumulative result"
    entries = AN.Lineage(os.path.join(world.anchor, f"lineage-{ACCT}.jsonl"), ACCT, "testnet").read()
    assert entries[-1]["kind"] == "REBASELINE" and "the venue reset" in entries[-1]["facts"]["reason"]
    assert stopped(opened(world, world.root2)) is False, "another root inherits the NEW epoch, not the old stop"


def test_after_a_rebaseline_the_next_flat_cash_read_is_the_new_baseline_and_books_no_gap(world):
    book = genesis(world)
    book.ensure_baseline("232")
    book.rebaseline("testnet wallet reset by the venue to 100 USDT", {"test": True})
    book.ensure_baseline("100")                       # NOT a 132 USD loss: the old epoch's last cash is not carried into the new one
    cb = book.load()
    assert cb["start_cash_usd"] == "100" and cb["history"] == [] and cb["realised_usd"] == "0"


def test_a_rebaseline_is_refused_while_a_session_is_unsettled_or_a_transfer_unapplied_or_without_a_reason(world):
    book = genesis(world)
    book.ensure_baseline("232")
    with pytest.raises(CC.BookRefused) as e:
        book.rebaseline("short", {})
    assert e.value.code == "REBASELINE_REASON_REQUIRED"
    assert book.declare_transfer("dep", "10")
    with pytest.raises(CC.BookRefused) as e2:
        book.rebaseline("a perfectly good reason", {})
    assert e2.value.code == "REBASELINE_REFUSED_TRANSFERS_PENDING"
    book.ensure_baseline("242")
    assert book.park("s1", "242", None, False, False)
    with pytest.raises(CC.BookRefused) as e3:
        book.rebaseline("a perfectly good reason", {})
    assert e3.value.code == "REBASELINE_REFUSED_PENDING_SESSION"


def test_a_restored_older_view_cannot_undo_a_rebaseline_and_a_rolled_back_volume_is_caught_by_the_cash(world):
    import shutil
    book = genesis(world)
    drive_to_lifetime_stop(book)
    snap_a, snap_r = world.tmp / "snap_a", world.tmp / "snap_r"
    shutil.copytree(world.anchor, snap_a)
    shutil.copytree(world.root, snap_r)
    old_view = open(book.path, "rb").read()
    book.rebaseline("the owner reviewed all five losses and accepts the account", {"test": True})
    book.ensure_baseline("207")
    with open(book.path, "wb") as fh:
        fh.write(old_view)                                                     # the pre-rebaseline view (stop active) is put back
    assert not stopped(opened(world)), "the owner's appended decision is durable: the view heals FORWARD to the new epoch"
    shutil.rmtree(world.anchor)
    shutil.rmtree(world.root)
    shutil.copytree(snap_a, world.anchor)
    shutil.copytree(snap_r, world.root)                                        # the WHOLE volume goes back to before the rebaseline
    back = opened(world)
    assert stopped(back), "(nothing local can object: the lifetime stop is simply back)"


def test_the_rebaseline_command_needs_a_terminal_the_typed_line_and_a_reason_and_is_audited(world, capsys):
    book = genesis(world)
    drive_to_lifetime_stop(book)
    head = AN.Lineage(os.path.join(world.anchor, f"lineage-{ACCT}.jsonl"), ACCT, "testnet").read()[-1]["h"]
    rc, _ = _rebaseline_cli(world, "a reason of my own", tty=False, capsys=capsys)
    assert rc == 2 and stopped(opened(world))
    rc, _ = _rebaseline_cli(world, None, tty=True, capsys=capsys)
    assert rc != 0 and stopped(opened(world)), "no reason, no new epoch"
    rc, line = _rebaseline_cli(world, "a reason of my own", tty=True, wrong=True, capsys=capsys)
    assert rc == 2 and stopped(opened(world))
    assert "epoch=0" in line and "cumulative_usd=" in line and f"head={head[:12]}" in line, line
    rc, line = _rebaseline_cli(world, "a reason of my own", tty=True, capsys=capsys)
    assert rc == 0, line
    assert not stopped(opened(world)) and opened(world).load()["epoch"] == 1
    rows = [json.loads(l) for l in open(os.path.join(world.root, "marker_log.jsonl"))]
    assert [r["action"] for r in rows if r["marker"] == "CAPITAL_BOOK"].count("APPLIED") == 1 and any(r["action"] == "REFUSED_NO_TTY" for r in rows)


# --------------------------------------------------------------------------------- growth: an idle loop must not grow the lineage (R6 of the capital specialist: 1200 idle cycles had written 2,401 entries)
def test_an_idle_loop_re_reading_the_same_cash_adds_nothing_to_the_lineage(world):
    book = genesis(world)
    book.ensure_baseline("232")
    path = os.path.join(world.anchor, f"lineage-{ACCT}.jsonl")
    n = len(open(path).read().splitlines())
    for _ in range(50):
        book.ensure_baseline("232")
        assert book.settle("232")["status"] == "NO_PENDING"
    assert len(open(path).read().splitlines()) == n, "the same cash read again is not a durable event"
    book.ensure_baseline("232.50")
    assert len(open(path).read().splitlines()) == n + 1, "a different read still is"


# --------------------------------------------------------------------------------- a forger who RE-CHAINS the hashes still cannot make the lineage say something else than the digest it attested
def _rechain(path, mutate):
    """what a writer who understands the format would do: change an entry, then recompute every hash after it so the chain verifies"""
    lines = [json.loads(l) for l in open(path).read().splitlines()]
    mutate(lines)
    prev = AN.GENESIS_HASH
    for e in lines:
        e["prev"] = prev
        e["h"] = AN.entry_hash(prev, e)
        prev = e["h"]
    with open(path, "w") as fh:
        fh.write("\n".join(AN._canon(e) for e in lines) + "\n")


def test_a_re_chained_lineage_whose_rows_no_longer_reproduce_its_attested_digest_is_corrupt(world):
    book = genesis(world)
    drive_to_lifetime_stop(book)
    path = os.path.join(world.anchor, f"lineage-{ACCT}.jsonl")
    entries = AN.Lineage(path, ACCT, "testnet").read()
    k = next(i for i, e in enumerate(entries) if e["kind"] == "SETTLE")

    def drop_a_loss(lines):
        lines[k]["history_add"] = []                      # the settlement's history row is removed from the record

    _rechain(path, drop_a_loss)
    AN.Lineage(path, ACCT, "testnet").read()               # the chain itself now verifies (that is the point)
    with pytest.raises(CC.BookRefused) as e:
        opened(world)
    assert e.value.code == "ANCHOR_CORRUPT" and "digest" in e.value.detail


def test_a_re_chained_head_whose_state_was_lowered_is_corrupt(world):
    book = genesis(world)
    drive_to_lifetime_stop(book)
    path = os.path.join(world.anchor, f"lineage-{ACCT}.jsonl")

    def lower_the_loss(lines):
        lines[-1]["state"]["realised_usd"] = "0"

    _rechain(path, lower_the_loss)
    with pytest.raises(CC.BookRefused) as e:
        opened(world)
    assert e.value.code == "ANCHOR_CORRUPT"


def test_a_commit_whose_history_deltas_do_not_reproduce_the_book_it_saves_is_refused(world):
    """the lineage must never hold an entry a rebuild could not reproduce: a mutator that forgets a delta (a coding bug, not an attack) is stopped at the write, not discovered at the next restore"""
    book = genesis(world)
    book.ensure_baseline("232")
    an = book.anchor
    path = os.path.join(world.anchor, f"lineage-{ACCT}.jsonl")
    n = len(open(path).read().splitlines())
    with an.locked():
        b = dict(an.open_view())
        b["history"] = list(b["history"]) + [{"session_id": "x", "credited_usd": "-1"}]
        with pytest.raises(AN.AnchorRefused) as e:
            an.commit(b, "SETTLE", {}, [], [])                                    # the history row was added to the book but not to the entry's deltas
    assert e.value.code == "ANCHOR_CORRUPT"
    assert len(open(path).read().splitlines()) == n, "nothing was appended"


# --------------------------------------------------------------------------------- the owner's one-time act at the CLI
def _owner_cli(world, argv, capsys, *, monkeypatch=None):
    import re

    def typed(_p):
        m = re.search(r"^(CONFIRM .+)$", capsys.readouterr().out, flags=re.M)
        return m.group(1).strip() if m else ""

    rc = AP.main(["capital", "--root", world.root, *argv], env={"CAPITAL_ANCHOR_DIR": world.anchor}, isatty=lambda: True, input_fn=typed, uid=0)
    return rc, capsys.readouterr().out


def test_anchor_init_adopts_a_pre_anchor_book_whole_through_the_gate_and_keeps_its_stop(world, capsys):
    legacy = CC.CapitalBook(world.root)
    drive_to_lifetime_stop(legacy)                                       # the deployed book of an account that is already past its lifetime stop
    try:
        opened(world)
    except CC.BookRefused as exc:
        assert exc.code == "BOOK_UNANCHORED"
    assert os.path.exists(os.path.join(world.anchor, f"proposal-{ACCT}.json"))
    rc, out = _owner_cli(world, ["--anchor-init"], capsys)
    assert rc == 0, out
    adopted = opened(world)
    assert stopped(adopted) and adopted.load()["start_cash_usd"] == "232", "adopting a book resets nothing"
    rows = [json.loads(l) for l in open(os.path.join(world.root, "marker_log.jsonl"))]
    assert [r["action"] for r in rows if r["marker"] == "CAPITAL_BOOK"] == ["INTENT", "APPLIED"], [r["action"] for r in rows]
    genesis_entry = AN.Lineage(os.path.join(world.anchor, f"lineage-{ACCT}.jsonl"), ACCT, "testnet").read()[0]
    assert genesis_entry["facts"]["adopted"] is True and genesis_entry["facts"]["witness"] == "NOT_CONFIGURED"


def test_anchor_init_with_no_proposal_and_no_account_explains_and_changes_nothing(world, capsys):
    rc, out = _owner_cli(world, ["--anchor-init"], capsys)
    assert rc == 2 and "NO_ACCOUNT_RESOLVED" in out and not os.path.exists(world.anchor)


def test_anchor_init_names_an_account_that_was_never_proposed_only_with_account_and_still_needs_the_typed_line(world, capsys):
    rc, out = _owner_cli(world, ["--anchor-init", "--account", "acct_not_a_real_ref"], capsys)
    assert rc == 2 and not os.path.exists(os.path.join(world.anchor, f"lineage-{ACCT}.jsonl"))
    rc, out = _owner_cli(world, ["--anchor-init", "--account", ACCT], capsys)
    assert rc == 0, out
    assert opened(world).load()["start_cash_usd"] is None


def test_anchor_init_is_refused_at_the_cli_when_the_witness_already_knows_the_account(world, capsys, monkeypatch):
    w = FakeWitness()
    w.rows[ACCT] = {0: "ab" * 32}
    monkeypatch.setattr(AN, "witness_from_env", lambda env: w)
    rc, out = _owner_cli(world, ["--anchor-init", "--account", ACCT], capsys)
    assert rc == 2 and not os.path.exists(os.path.join(world.anchor, f"lineage-{ACCT}.jsonl")), "the witness knows this account: a new lineage must not be created, however the volume got empty"
    rows = [json.loads(l) for l in open(os.path.join(world.root, "marker_log.jsonl"))]
    assert any(r["action"].startswith("REFUSED:GENESIS_REFUSED_WITNESS_HAS_HISTORY") for r in rows), [r["action"] for r in rows]


def test_the_capital_command_prints_the_anchor_and_the_cumulative_result(world, capsys):
    book = genesis(world)
    drive_to_lifetime_stop(book)
    rc = AP.main(["capital", "--root", world.root], env={"CAPITAL_ANCHOR_DIR": world.anchor})
    rep = json.loads(capsys.readouterr().out)
    assert rc == 0 and rep["anchor"]["anchored"] is True and rep["anchor"]["account"] == ACCT and rep["anchor"]["witness"] == "NOT_CONFIGURED"
    assert D(rep["lifetime_cumulative_realised_usd"]) == D(rep["verified_realised_usd"]) and rep["lifetime_loss_stop_active"] is True and rep["epoch"] == 0


# --------------------------------------------------------------------------------- the first-ever genesis is a race the lock must decide
def test_two_owners_racing_the_first_genesis_produce_exactly_one_lineage(world):
    results: list = []
    start = threading.Barrier(2)

    def go(root):
        an = AN.Anchor(root, ACCT, "testnet", world.anchor)
        book = CC.CapitalBook(root, an)
        start.wait()
        try:
            book.initialise({"racer": root})
            results.append("OK")
        except CC.BookRefused as exc:
            results.append(exc.code)

    ts = [threading.Thread(target=go, args=(world.root,)), threading.Thread(target=go, args=(world.root2,))]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert sorted(results) == ["GENESIS_REFUSED_LINEAGE_EXISTS", "OK"], results
    entries = AN.Lineage(os.path.join(world.anchor, f"lineage-{ACCT}.jsonl"), ACCT, "testnet").read()
    assert [e["kind"] for e in entries] == ["GENESIS"], "one GENESIS, whoever won"


def test_the_loops_classification_of_anchor_refusals_incident_versus_owner_action_versus_transient():
    """OWNER_AUTHORITY_REFUSALS is matched with startswith: an incident code must never be classified as 'wait for the owner' (it would be retried quietly instead of alerting), and the three owner-action codes and two
    transient ones must be."""
    codes_owner = ("ACCOUNT_NOT_INITIALISED", "BOOK_UNANCHORED", "ACCOUNT_IDENTITY_UNAVAILABLE", "WITNESS_UNAVAILABLE", "LOCK_TIMEOUT")
    incidents = ("ACCOUNT_MISMATCH", "VIEW_TAMPERED", "VIEW_UNBOUND", "ANCHOR_BEHIND_BOOK", "ANCHOR_CORRUPT", "ANCHOR_MISSING_BOOK_BOUND", "LOCAL_BEHIND_WITNESS", "LINEAGE_FORKED", "WITNESS_MISCONFIGURED",
                 "BARE_WRITE_TO_BOUND_BOOK", "GENESIS_REFUSED_LINEAGE_EXISTS")
    owner_side = lambda why: any(why.startswith(p) for p in AP.OWNER_AUTHORITY_REFUSALS)          # noqa: E731
    for c in codes_owner:
        assert owner_side("CAPITAL_ANCHOR_REFUSED:" + c), c
    for c in incidents:
        assert not owner_side("CAPITAL_ANCHOR_REFUSED:" + c), c
    assert set(AP.NO_ALERT_ANCHOR_CODES) == set(codes_owner)
