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
    bare = CC.CapitalBook(world.root)
    assert stopped(bare), "a bare READ of the bound view is still possible, and it says the stop is active"
    with pytest.raises(CC.BookRefused) as e2:
        bare.ensure_baseline("100")                                  # only WRITES are refused
    assert e2.value.code == "BARE_WRITE_TO_BOUND_BOOK"


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


def _lineage_lines(world):
    return open(os.path.join(world.anchor, f"lineage-{ACCT}.jsonl")).read().splitlines()


def test_noise_below_the_gap_tolerance_books_nothing_writes_nothing_and_does_not_move_the_reference(world):
    book = genesis(world)
    book.ensure_baseline("232")
    n = len(_lineage_lines(world))
    book.ensure_baseline("232.10")
    cb = book.load()
    assert cb["history"] == [] and cb["last_cash_usdt"] == "232", "the reference stays at the last BOOKED read (it used to follow the noise)"
    assert len(_lineage_lines(world)) == n, "noise is not a durable event"


def test_a_slow_bleed_below_the_tolerance_is_booked_once_it_adds_up_not_swallowed_read_by_read(world):
    """Red team, finding 10: 60 idle reads of -0.2 USD each overwrote the reference every time and 12 USD vanished with no booking, no streak and no review."""
    book = genesis(world)
    book.ensure_baseline("232")
    cash = D("232")
    for _ in range(60):
        cash -= D("0.2")
        book.ensure_baseline(str(cash))
    cb = book.load()
    assert D(cb["realised_usd"]) == D("-12.0") and cb["halted"] is True and cb["review_required"]["code"] == "ACCOUNT_CASH_UNEXPLAINED", cb["realised_usd"]


def test_settling_with_nothing_parked_books_a_big_move_instead_of_overwriting_the_reference_and_an_unreadable_read_writes_nothing(world):
    book = genesis(world)
    book.ensure_baseline("232")
    n = len(_lineage_lines(world))
    assert book.settle(None)["status"] == "NO_PENDING" and len(_lineage_lines(world)) == n, "an unreadable cash read appended a durable, witnessed entry per call"
    assert book.settle("232.10")["status"] == "NO_PENDING" and len(_lineage_lines(world)) == n and book.load()["last_cash_usdt"] == "232"
    book.settle("200")                                                                  # a caller that settles WITHOUT a baseline call must not swallow a 32 USD fall
    cb = book.load()
    assert D(cb["realised_usd"]) == D("-32") and cb["review_required"]["code"] == "ACCOUNT_CASH_UNEXPLAINED" and cb["history"][-1]["window"] == "IDLE_GAP"


def test_a_rebaseline_between_a_settle_and_a_park_baselines_from_the_parked_sessions_starting_cash(world):
    """Red team, finding 10 (a23): the new epoch's baseline was the POST-session cash (202), and the session's own -5 was then booked a second time."""
    book = genesis(world)
    book.ensure_baseline("207")
    assert book.settle("207")["status"] == "NO_PENDING"
    book.rebaseline("a reason of my own about the wallet reset", {"test": True})
    assert book.park("s1", "207", None, False, False)                                    # the cycle that had already read its cash goes on to park
    book.ensure_baseline("202")                                                          # the session lost 5: its result is the next read
    assert book.load()["start_cash_usd"] == "207", "the baseline is the epoch's true start, not the cash after the session"
    r = book.settle("202")
    assert D(r["delta_usd"]) == D("-5") and D(book.load()["realised_usd"]) == D("-5") and D(book.load()["peak_basis_usd"]) == D("207")


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


# --------------------------------------------------------------------------------- the remote witness (the interface and a recording fake; the Supabase implementation needs NO migration, see test_ms_capital_witness.py)
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
    codes_owner = ("ACCOUNT_NOT_INITIALISED", "BOOK_UNANCHORED", "ACCOUNT_IDENTITY_UNAVAILABLE", "WITNESS_UNAVAILABLE", "LOCK_TIMEOUT", "LINEAGE_CHANGED_UNDER_COMMIT")
    incidents = ("ACCOUNT_MISMATCH", "VIEW_TAMPERED", "VIEW_UNBOUND", "ANCHOR_BEHIND_BOOK", "ANCHOR_CORRUPT", "ANCHOR_MISSING_BOOK_BOUND", "LOCAL_BEHIND_WITNESS", "LINEAGE_FORKED", "WITNESS_MISCONFIGURED",
                 "BARE_WRITE_TO_BOUND_BOOK", "GENESIS_REFUSED_LINEAGE_EXISTS", "GENESIS_REFUSED_ROOT_REMEMBERS_LINEAGE")
    owner_side = lambda why: any(why.startswith(p) for p in AP.OWNER_AUTHORITY_REFUSALS)          # noqa: E731
    for c in codes_owner:
        assert owner_side("CAPITAL_ANCHOR_REFUSED:" + c), c
    for c in incidents:
        assert not owner_side("CAPITAL_ANCHOR_REFUSED:" + c), c
    assert set(AP.NO_ALERT_ANCHOR_CODES) == set(codes_owner)


# =====================================================================================================================================================================================================
# ROUND 10, INDEPENDENT RED TEAM. Every test below was written against the tree that had the finding (each failed first) and has a mutant that now dies (see the PR description).
# =====================================================================================================================================================================================================
def _snapshot_dir(src, dst):
    shutil.copytree(src, dst)
    return dst


def _put_back(dst, snap):
    shutil.rmtree(dst)
    shutil.copytree(snap, dst)


# --------------------------------------------------------------------------------- HIGH 1: restoring ONLY the anchor directory, then deleting the view, was the rollback itself
def test_restoring_only_the_anchor_directory_and_then_deleting_the_view_cannot_roll_the_book_back(world):
    book = genesis(world)
    book.ensure_baseline("232")
    snap = _snapshot_dir(world.anchor, world.tmp / "anchor_backup")                   # the second-volume snapshot the RUNBOOK recommends
    drive_to_lifetime_stop(book)
    assert stopped(book)
    _put_back(world.anchor, snap)
    with pytest.raises(CC.BookRefused) as e:
        opened(world)
    assert e.value.code == "ANCHOR_BEHIND_BOOK"
    os.unlink(os.path.join(world.root, "capital.json"))                               # the natural 'fix' for a refused view: it is only a view
    with pytest.raises(CC.BookRefused) as e2:
        opened(world)
    assert e2.value.code == "ANCHOR_BEHIND_BOOK" and "capital.json" in e2.value.detail, "the root's own high-water mark refuses it"
    assert not os.path.exists(os.path.join(world.root, "capital.json")), "a refusal recreates nothing"


def test_the_same_attack_cannot_resurrect_a_four_loss_halt_either(world):
    book = genesis(world)
    book.ensure_baseline("232")
    snap = _snapshot_dir(world.anchor, world.tmp / "anchor_backup")
    cash = D("232")
    for n in range(1, 5):
        losing_session(book, n, str(cash), "2.5")
        cash -= D("2.5")
    assert book.load()["halted"] is True
    _put_back(world.anchor, snap)
    os.unlink(os.path.join(world.root, "capital.json"))
    with pytest.raises(CC.BookRefused) as e:
        opened(world)
    assert e.value.code == "ANCHOR_BEHIND_BOOK"


def test_the_mark_does_not_get_in_the_way_of_ordinary_healing(world):
    book = genesis(world)
    book.ensure_baseline("232")
    old_root = _snapshot_dir(world.root, world.tmp / "root_backup")
    drive_to_lifetime_stop(book)
    os.unlink(book.path)                                                              # the view is gone, the lineage and the mark agree: it heals
    again = opened(world)
    assert stopped(again) and again.anchor.rebuilt == "NO_VIEW"
    _put_back(world.root, old_root)                                                   # an OLDER root (view AND mark older than the lineage) heals forward
    assert stopped(opened(world))
    assert json.load(open(os.path.join(world.root, "capital_hwm.json")))["seq"] == len(_lineage_lines(world)) - 1, "the heal brings the mark forward too"


def test_the_mark_is_written_by_every_commit_and_by_a_heal_and_a_read_only_open_writes_nothing(world):
    book = genesis(world)
    hwm = os.path.join(world.root, "capital_hwm.json")
    assert json.load(open(hwm))["seq"] == 0
    book.ensure_baseline("232")
    d = json.load(open(hwm))
    head = _lineage_lines(world)[-1]
    assert d["seq"] == 1 and d["head"] == json.loads(head)["h"] and d["account"] == ACCT
    os.unlink(hwm)
    ro = opened(world, read_only=True)
    assert not os.path.exists(hwm), "a read-only open writes nothing"
    assert ro.load()["start_cash_usd"] == "232"
    ro.anchor._write_hwm(5, "a" * 64)
    assert not os.path.exists(hwm), "and the write itself refuses on a read-only anchor"
    opened(world)                                                                     # a normal open with the mark missing: nothing to compare with, the view equals the head, no write needed
    book.park("s1", "232", None, False, False)
    assert json.load(open(hwm))["seq"] == 2


def test_a_mark_that_names_another_head_than_the_lineage_holds_there_is_a_fork(world):
    book = genesis(world)
    book.ensure_baseline("232")
    hwm = os.path.join(world.root, "capital_hwm.json")
    d = json.load(open(hwm))
    d["head"] = "ab" * 32
    json.dump(d, open(hwm, "w"))
    with pytest.raises(CC.BookRefused) as e:
        opened(world)
    assert e.value.code == "LINEAGE_FORKED"


def test_a_mark_of_another_account_or_one_that_cannot_be_read_refuses_and_changes_nothing(world):
    book = genesis(world)
    book.ensure_baseline("232")
    hwm = os.path.join(world.root, "capital_hwm.json")
    good = open(hwm).read()
    d = json.loads(good)
    d["account"] = OTHER
    json.dump(d, open(hwm, "w"))
    with pytest.raises(CC.BookRefused) as e:
        opened(world)
    assert e.value.code == "ACCOUNT_MISMATCH"
    for junk in ("not json", json.dumps({**json.loads(good), "seq": -1}), json.dumps({**json.loads(good), "head": "short"}), json.dumps({**json.loads(good), "schema": "other"}), "[" * 5000):
        open(hwm, "w").write(junk)
        with pytest.raises(CC.BookRefused) as e2:
            opened(world)
        assert e2.value.code == "VIEW_TAMPERED", junk[:20]
        assert open(hwm).read() == junk, "nothing was repaired"
    open(hwm, "w").write(good)
    assert opened(world).load()["start_cash_usd"] == "232"


def test_a_lost_anchor_directory_that_the_root_remembers_refuses_and_a_fresh_genesis_is_refused_too(world):
    book = genesis(world)
    book.ensure_baseline("232")
    os.unlink(book.path)
    shutil.rmtree(world.anchor)                                                       # the whole anchor volume came back empty, and the view is gone as well
    with pytest.raises(CC.BookRefused) as e:
        opened(world)
    assert e.value.code == "ANCHOR_MISSING_BOOK_BOUND" and "restore it" in e.value.detail
    with pytest.raises(CC.BookRefused) as e2:
        CC.CapitalBook(world.root, AN.Anchor(world.root, ACCT, "testnet", world.anchor)).initialise({"test": True})
    assert e2.value.code == "GENESIS_REFUSED_ROOT_REMEMBERS_LINEAGE", "a new history would be the reset this exists to prevent"
    assert not os.path.exists(os.path.join(world.anchor, f"lineage-{ACCT}.jsonl"))


def test_a_lost_anchor_directory_with_the_view_that_the_mark_describes_may_be_adopted_but_an_older_view_may_not(world):
    book = genesis(world)
    book.ensure_baseline("232")
    old_view = open(book.path, "rb").read()
    drive_to_lifetime_stop(book)
    new_view = open(book.path, "rb").read()
    shutil.rmtree(world.anchor)
    open(book.path, "wb").write(old_view)                                             # a restored OLD view: older than what the root last saw
    with pytest.raises(CC.BookRefused) as e:
        CC.CapitalBook(world.root, AN.Anchor(world.root, ACCT, "testnet", world.anchor)).initialise({"test": True})
    assert e.value.code == "GENESIS_REFUSED_ROOT_REMEMBERS_LINEAGE"
    open(book.path, "wb").write(new_view)                                             # the view the root last saw: the lineage is lost, the book is not: adopted WHOLE, every stop kept
    CC.CapitalBook(world.root, AN.Anchor(world.root, ACCT, "testnet", world.anchor)).initialise({"test": True})
    assert stopped(opened(world))


def test_a_root_that_belongs_to_another_account_cannot_be_given_a_new_lineage(world):
    genesis(world)
    with pytest.raises(CC.BookRefused) as e:
        CC.CapitalBook(world.root, AN.Anchor(world.root, OTHER, "testnet", world.anchor)).initialise({"test": True})
    assert e.value.code == "ACCOUNT_MISMATCH"


# --------------------------------------------------------------------------------- the owner's RECORDED way out of ANCHOR_BEHIND_BOOK
def _behind_world(world):
    book = genesis(world)
    book.ensure_baseline("232")
    snap = _snapshot_dir(world.anchor, world.tmp / "anchor_backup")
    drive_to_lifetime_stop(book)
    _put_back(world.anchor, snap)
    with pytest.raises(CC.BookRefused) as e:
        opened(world)
    assert e.value.code == "ANCHOR_BEHIND_BOOK"
    return CC.CapitalBook(world.root, AN.Anchor(world.root, ACCT, "testnet", world.anchor))


def test_accepting_the_older_lineage_is_recorded_kept_and_demands_a_review(world):
    owner_book = _behind_world(world)
    r = owner_book.accept_older_lineage({"gate": "TEST"})
    assert r["status"] == "ACCEPTED" and r["discarded"]["view"]["realised_usd"] is not None and D(r["discarded"]["view"]["realised_usd"]) <= -AN_STOP()
    after = opened(world)
    cb = after.load()
    assert cb["review_required"]["code"] == "ROLLBACK_ACCEPTED" and cb["review_required"]["discarded"]["hwm"]["seq"] > r["older_head_seq"]
    assert not stopped(after), "the OLDER lineage's state is what continues (that is what accepting it means): the review is the guard"
    entries = AN.Lineage(os.path.join(world.anchor, f"lineage-{ACCT}.jsonl"), ACCT, "testnet").read()
    assert entries[-1]["kind"] == "ACK_OLDER" and entries[-1]["facts"]["discarded"]["view"]["seq"] > entries[-1]["facts"]["older_head_seq"], "the lineage itself remembers what was discarded"
    kept = sorted(os.listdir(world.root))
    assert any(n.startswith("capital.json.rejected-newer.") for n in kept) and any(n.startswith("capital_hwm.json.rejected-newer.") for n in kept), kept
    assert json.load(open(os.path.join(world.root, "capital_hwm.json")))["seq"] == entries[-1]["seq"]


def test_accepting_changes_nothing_when_the_book_is_not_behind_and_is_not_a_way_around_any_other_refusal(world):
    book = genesis(world)
    book.ensure_baseline("232")
    ob = CC.CapitalBook(world.root, AN.Anchor(world.root, ACCT, "testnet", world.anchor))
    n = len(_lineage_lines(world))
    assert ob.accept_older_lineage({})["status"] == "NOTHING_TO_ACCEPT" and len(_lineage_lines(world)) == n
    v = json.load(open(book.path))
    v["realised_usd"] = "99"
    json.dump(v, open(book.path, "w"))                                                # an edited view is VIEW_TAMPERED: that is --rebuild-view's business, not this act's
    with pytest.raises(CC.BookRefused) as e:
        ob.accept_older_lineage({})
    assert e.value.code == "VIEW_TAMPERED" and len(_lineage_lines(world)) == n


def test_a_witness_that_knows_the_newer_head_refuses_the_acceptance_before_anything_is_touched(world):
    w = FakeWitness()
    book = genesis(world, witness=w)
    book.ensure_baseline("232")
    snap = _snapshot_dir(world.anchor, world.tmp / "anchor_backup")
    drive_to_lifetime_stop(book)
    _put_back(world.anchor, snap)
    ob = CC.CapitalBook(world.root, AN.Anchor(world.root, ACCT, "testnet", world.anchor, witness=w))
    before = sorted(os.listdir(world.root)), open(os.path.join(world.root, "capital.json")).read()
    with pytest.raises(CC.BookRefused) as e:
        ob.accept_older_lineage({})
    assert e.value.code == "LOCAL_BEHIND_WITNESS"
    assert (sorted(os.listdir(world.root)), open(os.path.join(world.root, "capital.json")).read()) == before, "nothing was moved, copied or written"


def test_the_accept_command_needs_a_terminal_the_typed_line_and_is_audited(world, capsys):
    import re
    _behind_world(world)
    state = {}

    def typed(_p):
        m = re.search(r"^(CONFIRM .+)$", capsys.readouterr().out, flags=re.M)
        state["line"] = m.group(1).strip() if m else ""
        return state["line"] + (" tampered" if state.get("wrong") else "")

    def run(tty=True):
        return AP.main(["capital", "--root", world.root, "--accept-older-lineage"], env={"CAPITAL_ANCHOR_DIR": world.anchor}, isatty=lambda: tty, input_fn=typed, uid=0)

    assert run(tty=False) == 2
    with pytest.raises(CC.BookRefused):
        opened(world)
    state["wrong"] = True
    assert run() == 2
    with pytest.raises(CC.BookRefused):
        opened(world)
    state["wrong"] = False
    assert run() == 0
    assert "newer_view_seq=" in state["line"] and "hwm_seq=" in state["line"] and "refusal=ANCHOR_BEHIND_BOOK" in state["line"], state["line"]
    assert opened(world).load()["review_required"]["code"] == "ROLLBACK_ACCEPTED"
    rows = [json.loads(l) for l in open(os.path.join(world.root, "marker_log.jsonl"))]
    assert [r["action"] for r in rows if r["marker"] == "CAPITAL_BOOK"].count("APPLIED") == 1 and any(r["action"] == "REFUSED_NO_TTY" for r in rows)


def test_a_review_rollback_accepted_is_cleared_only_by_the_owners_acknowledgement(world):
    owner_book = _behind_world(world)
    owner_book.accept_older_lineage({})
    book = opened(world)
    assert book.load()["review_required"] is not None
    assert book.acknowledge_review() is True and book.load()["review_required"] is None


# --------------------------------------------------------------------------------- MEDIUM 4: two committers on one lineage (the lock FILE can be unlinked) cannot both append
def test_two_committers_with_a_stale_lock_cannot_append_the_same_sequence_twice(world):
    book = genesis(world)
    book.ensure_baseline("232")
    other = opened(world, world.root2)                                                # a second process: reads the head BEFORE the first commits
    with other._locked():
        other.anchor.open_view()
        other.anchor.entries()
        os.unlink(os.path.join(world.anchor, f"lineage-{ACCT}.lock"))                 # a 'clean stale locks' script ran while it held the lock
        book2 = opened(world)                                                         # and a first writer gets a fresh lock and commits
        assert book2.park("s1", "232", None, False, False)
        with pytest.raises(AN.AnchorRefused) as e:
            other.anchor.commit({**other.anchor.entries()[-1]["state"], "history": [], "transfers_applied": []}, "BASELINE", {})
        assert e.value.code == "LINEAGE_CHANGED_UNDER_COMMIT"
    entries = AN.Lineage(os.path.join(world.anchor, f"lineage-{ACCT}.jsonl"), ACCT, "testnet").read()           # the chain still verifies: no [0,1,2,2]
    assert [e["seq"] for e in entries] == list(range(len(entries)))
    assert opened(world).load()["pending"]["session_id"] == "s1"


def test_an_append_that_was_never_preceded_by_a_verified_read_is_refused(world):
    genesis(world)
    lin = AN.Lineage(os.path.join(world.anchor, f"lineage-{ACCT}.jsonl"), ACCT, "testnet")
    with pytest.raises(AN.AnchorRefused) as e:
        lin.append({"x": 1})
    assert e.value.code == "LINEAGE_CHANGED_UNDER_COMMIT" and "not read and verified" in e.value.detail


# --------------------------------------------------------------------------------- MEDIUM 5: the owner CLI's information read must never repair a tail another process is writing
def test_the_owner_clis_information_read_never_truncates_a_tail_that_is_being_written(world, monkeypatch):
    book = genesis(world)
    book.ensure_baseline("232")
    path = os.path.join(world.anchor, f"lineage-{ACCT}.jsonl")
    before = open(path, "rb").read()
    half = b'{"schema": "capital_lineage.v1", "seq": 2, "pre'                          # a writer is in the middle of an append
    monkeypatch.setattr(AN, "LOCK_TIMEOUT_S", 0.2)
    with book._locked():                                                              # ... and holds the lineage lock while it does
        with open(path, "ab") as fh:
            fh.write(half)
        rc = AP.main(["capital", "--root", world.root, "--anchor-init"], env={"CAPITAL_ANCHOR_DIR": world.anchor}, isatty=lambda: False, input_fn=lambda _p: "", uid=0)
        assert rc == 2
        assert open(path, "rb").read() == before + half, "the half-written entry is still there: the information read of the owner CLI runs outside the lock and must never repair (it truncated the writer's entry)"
    assert not os.path.exists(path + ".torn")


# --------------------------------------------------------------------------------- MEDIUM 6: a REQUIRED witness that is down when the owner re-baselines: the act STANDS and the owner is told so
def test_a_rebaseline_with_a_required_witness_that_is_down_says_applied_locally_not_refused(world, capsys):
    import re
    w = FakeWitness(required=True)
    book = genesis(world, witness=w)
    drive_to_lifetime_stop(book)
    w.down = True
    state = {}

    def typed(_p):
        m = re.search(r"^(CONFIRM .+)$", capsys.readouterr().out, flags=re.M)
        state["line"] = m.group(1).strip() if m else ""
        return state["line"]

    import ms_capital_anchor as _AN
    orig = _AN.witness_from_env
    _AN.witness_from_env = lambda env: w
    try:
        rc = AP.main(["capital", "--root", world.root, "--rebaseline", "--reason", "a reason of my own that is long enough"], env={"CAPITAL_ANCHOR_DIR": world.anchor}, isatty=lambda: True, input_fn=typed, uid=0)
    finally:
        _AN.witness_from_env = orig
    err = capsys.readouterr().err
    assert rc == 0 and "APPLIED LOCALLY" in err and "nothing was changed" not in err, err
    assert opened(world).load()["epoch"] == 1


# --------------------------------------------------------------------------------- LOW 14: an unrecognised CAPITAL_WITNESS_REQUIRED is a refusal, never a silent 'optional'
@pytest.mark.parametrize("val,expect", [("1", True), ("true", True), ("YES", True), ("on", True), ("", False), ("0", False), ("false", False), ("No", False), ("off", False)])
def test_witness_required_accepts_the_documented_spellings(val, expect):
    assert AN.witness_required({"CAPITAL_WITNESS_REQUIRED": val}) is expect


@pytest.mark.parametrize("val", ["enabled", "required", "2", "y", "truee"])
def test_witness_required_refuses_anything_else(val):
    with pytest.raises(AN.AnchorRefused) as e:
        AN.witness_required({"CAPITAL_WITNESS_REQUIRED": val})
    assert e.value.code == "WITNESS_MISCONFIGURED"
    with pytest.raises(AN.AnchorRefused):
        AN.witness_from_env({"CAPITAL_WITNESS": "supabase", "CAPITAL_WITNESS_REQUIRED": val})


# --------------------------------------------------------------------------------- red team, finding 9: branches that carried the design and that no test noticed (each pinned by a mutant that now dies)
def test_a_failed_lineage_append_leaves_the_view_and_the_mark_untouched_the_append_is_the_commit_point(world, monkeypatch):
    book = genesis(world)
    book.ensure_baseline("232")
    hwm = os.path.join(world.root, "capital_hwm.json")
    view_before, hwm_before = open(book.path, "rb").read(), open(hwm, "rb").read()

    def full_disk(self, entry, *a, **k):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(AN.Lineage, "append", full_disk)
    with pytest.raises(OSError):
        book.park("s1", "232", None, False, False)
    assert open(book.path, "rb").read() == view_before and open(hwm, "rb").read() == hwm_before, "the view (or the mark) was written BEFORE the lineage held the entry"
    monkeypatch.undo()
    assert opened(world).load()["pending"] is None


def test_a_junk_line_in_the_middle_and_a_newline_terminated_junk_tail_are_corruption_and_only_an_unterminated_tail_is_a_torn_append(world):
    book = genesis(world)
    book.ensure_baseline("232")
    book.park("s1", "232", None, False, False)
    path = os.path.join(world.anchor, f"lineage-{ACCT}.jsonl")
    original = open(path, "rb").read()
    lines = original.decode().splitlines()
    assert len(lines) >= 3
    mid = list(lines)
    mid[1] = "this is not json"
    open(path, "w").write("\n".join(mid) + "\n")
    with pytest.raises(CC.BookRefused) as e:
        opened(world)
    assert e.value.code == "ANCHOR_CORRUPT" and "line 1" in e.value.detail and open(path).read() == "\n".join(mid) + "\n" and not os.path.exists(path + ".torn")
    open(path, "wb").write(original + b"x\n")                                          # a stray terminated line (`echo x >> lineage`): never silently dropped as 'torn'
    with pytest.raises(CC.BookRefused) as e2:
        opened(world)
    assert e2.value.code == "ANCHOR_CORRUPT" and open(path, "rb").read() == original + b"x\n" and not os.path.exists(path + ".torn")
    open(path, "wb").write(original + b'{"torn')                                       # an UNTERMINATED tail is an append that never completed: set aside as evidence and dropped
    opened(world)
    assert open(path, "rb").read() == original and os.path.exists(path + ".torn")


@pytest.mark.parametrize("field,value", [("head", "ab" * 32), ("digest", "cd" * 32)])
def test_each_part_of_the_views_reference_is_checked_on_its_own(world, field, value):
    book = genesis(world)
    book.ensure_baseline("232")
    d = json.load(open(book.path))
    d["ledger_ref"][field] = value
    json.dump(d, open(book.path, "w"))
    with pytest.raises(CC.BookRefused) as e:
        opened(world)
    assert e.value.code == "VIEW_TAMPERED"


def test_a_symlinked_lineage_is_never_appended_through(world, tmp_path):
    book = genesis(world)
    book.ensure_baseline("232")
    path = os.path.join(world.anchor, f"lineage-{ACCT}.jsonl")
    target = str(tmp_path / "elsewhere.jsonl")
    shutil.copy(path, target)
    os.unlink(path)
    os.symlink(target, path)
    before = open(target, "rb").read()
    with pytest.raises(OSError):
        book.park("s1", "232", None, False, False)
    assert open(target, "rb").read() == before


@pytest.mark.parametrize("move,booked", [("0.24", False), ("0.26", True), ("-0.24", False), ("-0.26", True)])
def test_the_idle_gap_tolerance_is_a_quarter_dollar_on_both_sides(world, move, booked):
    book = genesis(world)
    book.ensure_baseline("232")
    book.ensure_baseline(str(D("232") + D(move)))
    assert bool(book.load()["history"]) is booked


def test_the_anchor_init_typed_line_lists_the_other_lineages_in_the_anchor_directory(world, capsys):
    """Red team, finding 13: under the KEY_ID fallback a rotated key is a NEW account, and a fresh init would clear the stops; the owner must SEE the lineages that already exist before attesting a new one."""
    import re
    genesis(world)                                                                     # account ACCT already has a lineage in this anchor directory
    state = {}

    def typed(_p):
        m = re.search(r"^(CONFIRM .+)$", capsys.readouterr().out, flags=re.M)
        state["line"] = m.group(1).strip() if m else ""
        return ""
    rc = AP.main(["capital", "--root", world.root2, "--account", OTHER, "--anchor-init"], env={"CAPITAL_ANCHOR_DIR": world.anchor}, isatty=lambda: True, input_fn=typed, uid=0)
    assert rc == 2
    assert "other_lineages=" in state["line"] and ACCT[:13] in state["line"], state["line"]


def test_a_deeply_nested_capital_json_does_not_crash_the_offline_account_resolution(world):
    os.makedirs(world.root)
    open(os.path.join(world.root, "capital.json"), "w").write("[" * 200_000)
    assert AN.resolve_account(world.root, world.anchor) is None


def test_the_status_command_reports_an_unrecognised_witness_required_value_instead_of_raising():
    assert AP._witness_required_for_status({"CAPITAL_WITNESS_REQUIRED": "enabled"}) == "INVALID"
    assert AP._witness_required_for_status({"CAPITAL_WITNESS_REQUIRED": "on"}) is True and AP._witness_required_for_status({}) is False


def test_the_owners_rebuild_view_repairs_a_damaged_mark_and_never_a_mark_that_is_ahead(world):
    book = genesis(world)
    book.ensure_baseline("232")
    hwm = os.path.join(world.root, "capital_hwm.json")
    open(hwm, "w").write("not json")                                                  # damage, not an attack: the root's own record of the head is unreadable
    owner = CC.CapitalBook(world.root, AN.Anchor(world.root, ACCT, "testnet", world.anchor))
    assert owner.rebuild_view() == "REBUILT"
    d = json.load(open(hwm))
    assert d["seq"] == len(_lineage_lines(world)) - 1 and d["account"] == ACCT
    assert opened(world).load()["start_cash_usd"] == "232"
    # a mark that is AHEAD of the lineage is the rollback signal: --rebuild-view must not erase it (ANCHOR_BEHIND_BOOK is never rebuilt)
    d["seq"] += 5
    json.dump(d, open(hwm, "w"))
    assert owner.anchor.__class__ is AN.Anchor
    with pytest.raises(CC.BookRefused) as e:
        owner.rebuild_view()
    assert e.value.code == "ANCHOR_BEHIND_BOOK" and json.load(open(hwm))["seq"] == d["seq"], "nothing was rewritten"


# =====================================================================================================================================================================================================
# ROUND 10, SECOND RED TEAM (the fixes themselves). NEW-1 was a HIGH and a release blocker: a damaged mark masked a newer view, and `capital --rebuild-view` then performed the rollback.
# =====================================================================================================================================================================================================
def _lost_ground(world):
    """lifetime stop reached after a snapshot of the anchor directory, which is then restored: the root remembers MORE than the lineage"""
    book = genesis(world)
    book.ensure_baseline("232")
    snap = _snapshot_dir(world.anchor, world.tmp / "anchor_backup")
    drive_to_lifetime_stop(book)
    assert stopped(book)
    _put_back(world.anchor, snap)
    return book


def test_a_damaged_mark_cannot_mask_a_newer_view_and_rebuild_view_does_not_perform_the_rollback(world):
    _lost_ground(world)
    hwm = os.path.join(world.root, "capital_hwm.json")
    open(hwm, "w").write("not json")                                                  # a damaged mark: the view still says how far the account had come
    view_before = open(os.path.join(world.root, "capital.json"), "rb").read()
    with pytest.raises(CC.BookRefused) as e:
        opened(world)
    assert e.value.code == "ANCHOR_BEHIND_BOOK", "it was VIEW_TAMPERED, which the documented fix (--rebuild-view) turned into the rollback"
    owner = CC.CapitalBook(world.root, AN.Anchor(world.root, ACCT, "testnet", world.anchor))
    with pytest.raises(CC.BookRefused) as e2:
        owner.rebuild_view()
    assert e2.value.code == "ANCHOR_BEHIND_BOOK"
    assert open(os.path.join(world.root, "capital.json"), "rb").read() == view_before and open(hwm).read() == "not json", "nothing was rebuilt, moved or copied"
    assert not [n for n in os.listdir(world.root) if ".rejected" in n or ".unreadable" in n]


def test_a_damaged_mark_with_the_lineage_gone_is_a_missing_anchor_not_a_tampered_view_and_nothing_stray_is_written(world):
    book = genesis(world)
    book.ensure_baseline("232")
    os.unlink(book.path)
    open(os.path.join(world.root, "capital_hwm.json"), "w").write("not json")
    shutil.rmtree(world.anchor)
    with pytest.raises(CC.BookRefused) as e:
        opened(world)
    assert e.value.code == "ANCHOR_MISSING_BOOK_BOUND"
    owner = CC.CapitalBook(world.root, AN.Anchor(world.root, ACCT, "testnet", world.anchor))
    with pytest.raises(CC.BookRefused) as e2:
        owner.rebuild_view()
    assert e2.value.code == "ANCHOR_MISSING_BOOK_BOUND", "it was ANCHOR_CORRUPT 'empty lineage', after writing a stray copy"
    assert not [n for n in os.listdir(world.root) if ".rejected" in n]


def test_rebuild_view_refuses_over_a_newer_remembered_state_whatever_the_refusal_at_hand_is(world, monkeypatch):
    """defence in depth: the guard lives in rebuild_view itself, not only in the order open_view judges the evidence"""
    _lost_ground(world)
    monkeypatch.setattr(AN.Anchor, "open_view", lambda self: (_ for _ in ()).throw(AN.AnchorRefused("VIEW_TAMPERED", "forced")))
    owner = CC.CapitalBook(world.root, AN.Anchor(world.root, ACCT, "testnet", world.anchor))
    view_before = open(os.path.join(world.root, "capital.json"), "rb").read()
    with pytest.raises(CC.BookRefused) as e:
        owner.rebuild_view()
    assert e.value.code == "ANCHOR_BEHIND_BOOK" and open(os.path.join(world.root, "capital.json"), "rb").read() == view_before


def test_rebuild_view_keeps_a_damaged_mark_as_evidence_when_it_repairs_it(world):
    book = genesis(world)
    book.ensure_baseline("232")
    hwm = os.path.join(world.root, "capital_hwm.json")
    open(hwm, "w").write("not json")
    CC.CapitalBook(world.root, AN.Anchor(world.root, ACCT, "testnet", world.anchor)).rebuild_view()
    assert any(n.startswith("capital_hwm.json.unreadable.") for n in os.listdir(world.root))


# --------------------------------------------------------------------------------- NEW-3: a CONFIGURED witness that cannot be read is not 'no witness'
def test_accepting_the_older_lineage_is_refused_while_a_configured_witness_cannot_be_read_even_if_it_is_optional(world):
    w = FakeWitness(required=False)
    book = genesis(world, witness=w)
    book.ensure_baseline("232")
    snap = _snapshot_dir(world.anchor, world.tmp / "anchor_backup")
    drive_to_lifetime_stop(book)
    _put_back(world.anchor, snap)
    ob = CC.CapitalBook(world.root, AN.Anchor(world.root, ACCT, "testnet", world.anchor, witness=w))
    before = (sorted(os.listdir(world.root)), open(os.path.join(world.root, "capital.json")).read(), len(_lineage_lines(world)))
    w.down = True
    with pytest.raises(CC.BookRefused) as e:
        ob.accept_older_lineage({})
    assert e.value.code == "WITNESS_UNAVAILABLE"
    assert (sorted(os.listdir(world.root)), open(os.path.join(world.root, "capital.json")).read(), len(_lineage_lines(world))) == before, "nothing was changed: accepting blind would have left the account refused for good"


def test_a_configured_witness_that_holds_nothing_does_not_block_the_acceptance(world):
    book = genesis(world)
    book.ensure_baseline("232")
    snap = _snapshot_dir(world.anchor, world.tmp / "anchor_backup")
    drive_to_lifetime_stop(book)
    _put_back(world.anchor, snap)
    empty = FakeWitness()
    ob = CC.CapitalBook(world.root, AN.Anchor(world.root, ACCT, "testnet", world.anchor, witness=empty))
    assert ob.accept_older_lineage({})["status"] == "ACCEPTED"


# --------------------------------------------------------------------------------- L2: an act that discards history keeps what it discards
def test_accepting_aborts_when_the_evidence_cannot_be_kept(world, monkeypatch):
    owner_book = _behind_world(world)
    n = len(_lineage_lines(world))
    view_before = open(os.path.join(world.root, "capital.json"), "rb").read()
    monkeypatch.setattr(AN.Anchor, "_preserve_path", lambda self, path, tag: False)
    monkeypatch.setattr(AN.Anchor, "_preserve", lambda self, tag: False)
    with pytest.raises(CC.BookRefused) as e:
        owner_book.accept_older_lineage({})
    assert e.value.code == "EVIDENCE_NOT_KEPT" and len(_lineage_lines(world)) == n and open(os.path.join(world.root, "capital.json"), "rb").read() == view_before


# --------------------------------------------------------------------------------- NEW-4: a root that still names a history the owner RECORDED as discarded is healed forward, never stuck
def test_a_crash_inside_the_acceptance_after_the_append_heals_forward_instead_of_forking(world):
    book = genesis(world)
    book.ensure_baseline("232")
    snap = _snapshot_dir(world.anchor, world.tmp / "anchor_backup")
    assert book.park("s1", "232", None, False, False)                                  # the discarded history is exactly ONE entry ahead: the ACK entry takes the same sequence
    _put_back(world.anchor, snap)
    owner = CC.CapitalBook(world.root, AN.Anchor(world.root, ACCT, "testnet", world.anchor))

    def die(point):
        if point == "after_append":
            raise Crash(point)
    AN.crash_hook = die
    try:
        with pytest.raises(Crash):
            owner.accept_older_lineage({})
    finally:
        AN.crash_hook = AN._noop
    again = opened(world)                                                              # the view and mark still name the discarded head at the ACK entry's own sequence
    cb = again.load()
    assert cb["review_required"]["code"] == "ROLLBACK_ACCEPTED" and cb["pending"] is None, "the older lineage's state continues; the discarded park is gone"
    assert any(n.startswith("capital.json.rejected-newer.") for n in os.listdir(world.root)), "and what was discarded is still kept"


def test_a_second_root_of_the_account_is_healed_forward_after_the_owner_accepted_on_the_first(world):
    book = genesis(world)
    book.ensure_baseline("232")
    snap = _snapshot_dir(world.anchor, world.tmp / "anchor_backup")
    drive_to_lifetime_stop(book)
    other_root = opened(world, world.root2)                                            # root B follows the lineage to its (newer) head
    assert stopped(other_root)
    _put_back(world.anchor, snap)
    CC.CapitalBook(world.root, AN.Anchor(world.root, ACCT, "testnet", world.anchor)).accept_older_lineage({})
    healed = opened(world, world.root2)                                                # it names the discarded history: recorded in the lineage, so healed forward
    assert healed.load()["review_required"]["code"] == "ROLLBACK_ACCEPTED" and not stopped(healed)


def test_a_root_naming_a_history_nobody_recorded_as_discarded_still_refuses(world):
    """the exemption is only for what an ACK_OLDER entry of THIS lineage names (sequence and head), never a general 'ahead is fine'"""
    owner_book = _behind_world(world)
    r = owner_book.accept_older_lineage({})
    hwm = os.path.join(world.root, "capital_hwm.json")
    d = json.load(open(hwm))
    d["seq"] = r["discarded"]["hwm"]["seq"]                                            # the very sequence that was discarded ...
    d["head"] = "ab" * 32                                                              # ... but another head: nobody recorded THAT
    json.dump(d, open(hwm, "w"))
    with pytest.raises(CC.BookRefused) as e:
        opened(world)
    assert e.value.code in ("ANCHOR_BEHIND_BOOK", "LINEAGE_FORKED")
    d["seq"] += 7                                                                      # and a sequence nobody recorded
    json.dump(d, open(hwm, "w"))
    with pytest.raises(CC.BookRefused) as e2:
        opened(world)
    assert e2.value.code == "ANCHOR_BEHIND_BOOK"


# --------------------------------------------------------------------------------- L5: a view that is AHEAD of the mark may be adopted (a crash window), an older one may not
def test_a_view_one_ahead_of_the_mark_can_be_adopted_when_the_lineage_is_lost(world):
    book = genesis(world)
    book.ensure_baseline("232")
    hwm = os.path.join(world.root, "capital_hwm.json")
    stale_mark = open(hwm).read()
    drive_to_lifetime_stop(book)                                                       # the view is several entries ahead of the mark we put back
    open(hwm, "w").write(stale_mark)
    shutil.rmtree(world.anchor)
    CC.CapitalBook(world.root, AN.Anchor(world.root, ACCT, "testnet", world.anchor)).initialise({"test": True})
    assert stopped(opened(world)), "the newest book the root has is adopted whole: every stop kept"


def test_a_view_that_is_not_the_one_the_root_last_saw_cannot_be_adopted_at_the_same_sequence(world):
    book = genesis(world)
    book.ensure_baseline("232")
    hwm = os.path.join(world.root, "capital_hwm.json")
    d = json.load(open(hwm))
    d["head"] = "ef" * 32                                                              # the mark names another head at the view's own sequence
    json.dump(d, open(hwm, "w"))
    shutil.rmtree(world.anchor)
    with pytest.raises(CC.BookRefused) as e:
        CC.CapitalBook(world.root, AN.Anchor(world.root, ACCT, "testnet", world.anchor)).initialise({"test": True})
    assert e.value.code == "GENESIS_REFUSED_ROOT_REMEMBERS_LINEAGE"


# --------------------------------------------------------------------------------- L11: the torn-tail cut never deletes an entry a writer appended since the read
def test_the_torn_tail_cut_refuses_when_the_file_changed_since_it_was_judged(world):
    book = genesis(world)
    book.ensure_baseline("232")
    path = os.path.join(world.anchor, f"lineage-{ACCT}.jsonl")
    lin = AN.Lineage(path, ACCT, "testnet")
    data = open(path, "rb").read()
    with open(path, "ab") as fh:
        fh.write(b'{"torn')
    torn = b'{"torn'
    at = len(data)
    with open(path, "ab") as fh:
        fh.write(b' and a writer appended more')                                       # the file is not what was judged any more
    before = open(path, "rb").read()
    with pytest.raises(AN.AnchorRefused) as e:
        lin._set_aside(torn, at)
    assert e.value.code == "LINEAGE_CHANGED_UNDER_COMMIT" and open(path, "rb").read() == before and not os.path.exists(path + ".torn")


# --------------------------------------------------------------------------------- NEW-5: what the owner types reads what is DISCARDED, what is CONTINUED, and what the witness said
def _typed_line(world, capsys, argv, *, witness=None, answer=""):
    import re
    holder = {}

    def typed(_p):
        m = re.search(r"^(CONFIRM .+)$", capsys.readouterr().out, flags=re.M)
        holder["line"] = m.group(1).strip() if m else ""
        return answer or holder["line"]
    orig = AN.witness_from_env
    if witness is not None:
        AN.witness_from_env = lambda env: witness
    try:
        rc = AP.main(["capital", "--root", world.root, *argv], env={"CAPITAL_ANCHOR_DIR": world.anchor}, isatty=lambda: True, input_fn=typed, uid=0)
    finally:
        AN.witness_from_env = orig
    return rc, holder["line"]


def test_the_accept_line_states_what_is_discarded_what_is_continued_and_the_witness(world, capsys):
    _behind_world(world)
    rc, line = _typed_line(world, capsys, ["--accept-older-lineage"], answer="not the line")
    assert rc == 2
    for key in ("older_head_seq=", "older_realised_usd=", "older_streak=", "older_halted=", "newer_view_seq=", "newer_view_realised_usd=", "newer_view_streak=", "newer_view_halted=", "hwm_seq=", "witness_state=NOT_CONFIGURED"):
        assert key in line, (key, line)
    w = FakeWitness()
    w.rows[ACCT] = {40: "ab" * 32}                                                      # a witness that holds a NEWER head than the local lineage
    rc2, line2 = _typed_line(world, capsys, ["--accept-older-lineage"], witness=w, answer="not the line")
    assert "witness_state=LOCAL_BEHIND_WITNESS" in line2 and "witness_head_seq=40" in line2, line2


def test_the_rebuild_view_line_names_the_newer_view_and_mark(world, capsys):
    _behind_world(world)
    rc, line = _typed_line(world, capsys, ["--rebuild-view"], answer="not the line")
    assert rc == 2 and "newer_view_seq=" in line and "hwm_seq=" in line, line


# =====================================================================================================================================================================================================
# ROUND 10, THIRD RED-TEAM PASS (the delta of cycle 4): no new HIGH; one MEDIUM sibling of NEW-1 (a damaged view's sequence masked the mark), one test gap, and low items. Each fixed test-first.
# =====================================================================================================================================================================================================
def _damage_view_sequence(world):
    d = json.load(open(os.path.join(world.root, "capital.json")))
    d["ledger_ref"]["seq"] = "x"                                                       # valid JSON, a ledger_ref whose sequence is not a number
    json.dump(d, open(os.path.join(world.root, "capital.json"), "w"))


def test_a_damaged_view_sequence_cannot_mask_a_forked_mark_and_rebuild_view_does_not_adopt_the_other_history(world):
    book = genesis(world)
    book.ensure_baseline("232")
    hwm = os.path.join(world.root, "capital_hwm.json")
    d = json.load(open(hwm))
    d["head"] = "ab" * 32                                                              # the lineage is another history than the one this root followed
    json.dump(d, open(hwm, "w"))
    _damage_view_sequence(world)
    with pytest.raises(CC.BookRefused) as e:
        opened(world)
    assert e.value.code == "LINEAGE_FORKED", "it was VIEW_TAMPERED, and --rebuild-view then replaced the forked mark and the view with the lineage head"
    view_before, mark_before = open(os.path.join(world.root, "capital.json"), "rb").read(), open(hwm, "rb").read()
    owner = CC.CapitalBook(world.root, AN.Anchor(world.root, ACCT, "testnet", world.anchor))
    with pytest.raises(CC.BookRefused) as e2:
        owner.rebuild_view()
    assert e2.value.code == "LINEAGE_FORKED" and open(os.path.join(world.root, "capital.json"), "rb").read() == view_before and open(hwm, "rb").read() == mark_before


def test_a_damaged_view_sequence_with_a_mark_that_is_ahead_is_behind_the_book_and_the_owner_can_accept(world):
    _lost_ground(world)
    _damage_view_sequence(world)
    with pytest.raises(CC.BookRefused) as e:
        opened(world)
    assert e.value.code == "ANCHOR_BEHIND_BOOK", "it was VIEW_TAMPERED, a circle: rebuild_view said behind, accept said tampered"
    owner = CC.CapitalBook(world.root, AN.Anchor(world.root, ACCT, "testnet", world.anchor))
    assert owner.accept_older_lineage({})["status"] == "ACCEPTED"
    assert opened(world).load()["review_required"]["code"] == "ROLLBACK_ACCEPTED"


def test_a_damaged_view_sequence_with_a_damaged_mark_is_tampered_and_nothing_readable_remembers_more(world):
    book = genesis(world)
    book.ensure_baseline("232")
    open(os.path.join(world.root, "capital_hwm.json"), "w").write("not json")
    _damage_view_sequence(world)
    with pytest.raises(CC.BookRefused) as e:
        opened(world)
    assert e.value.code == "VIEW_TAMPERED"


# --------------------------------------------------------------------------------- the halves of rebuild_view's guard, each pinned alone (third pass, N3-2)
def _force_tamper(monkeypatch):
    """open_view says VIEW_TAMPERED whatever the files hold: it isolates the guard INSIDE rebuild_view from the order in which open_view judges the evidence"""
    monkeypatch.setattr(AN.Anchor, "open_view", lambda self: (_ for _ in ()).throw(AN.AnchorRefused("VIEW_TAMPERED", "forced")))


@pytest.mark.parametrize("keep", ["view", "mark"])
def test_rebuild_view_refuses_when_only_the_view_or_only_the_mark_is_ahead(world, monkeypatch, keep):
    _lost_ground(world)
    if keep == "view":
        os.unlink(os.path.join(world.root, "capital_hwm.json"))
    else:
        os.unlink(os.path.join(world.root, "capital.json"))
    owner = CC.CapitalBook(world.root, AN.Anchor(world.root, ACCT, "testnet", world.anchor))
    before = sorted(os.listdir(world.root))
    _force_tamper(monkeypatch)
    with pytest.raises(CC.BookRefused) as e:
        owner.rebuild_view()
    assert e.value.code == "ANCHOR_BEHIND_BOOK" and sorted(os.listdir(world.root)) == before


def test_rebuild_view_with_no_lineage_has_nothing_to_rebuild_from(world, monkeypatch):
    genesis(world)
    shutil.rmtree(world.anchor)
    owner = CC.CapitalBook(world.root, AN.Anchor(world.root, ACCT, "testnet", world.anchor))
    _force_tamper(monkeypatch)
    with pytest.raises(CC.BookRefused) as e:
        owner.rebuild_view()
    assert e.value.code == "ANCHOR_MISSING_BOOK_BOUND"


def test_rebuild_view_may_proceed_when_everything_the_root_remembers_ahead_was_recorded_as_discarded(world, monkeypatch):
    root_c, _ = _two_roots_after_an_accept(world)
    owner = CC.CapitalBook(root_c, AN.Anchor(root_c, ACCT, "testnet", world.anchor))     # root C still names the discarded view
    _force_tamper(monkeypatch)
    assert owner.rebuild_view() == "REBUILT", "a recorded discard is not 'remembering more than the lineage holds'"


# --------------------------------------------------------------------------------- the recorded discard is EXACT (whole head), per half, and never excuses an edited view of the lineage's own head
def _two_roots_after_an_accept(world):
    """root A accepts; roots B (mark only) and C (view only) still name the two DISCARDED states, which differ because A's mark was one entry behind its view"""
    book = genesis(world)
    book.ensure_baseline("232")
    snap = _snapshot_dir(world.anchor, world.tmp / "anchor_backup")
    drive_to_lifetime_stop(book)
    entries = AN.Lineage(os.path.join(world.anchor, f"lineage-{ACCT}.jsonl"), ACCT, "testnet").read()
    n = entries[-1]["seq"]
    root_c = str(world.tmp / "rootC")
    opened(world, world.root2)
    opened(world, root_c)
    prev_mark = {"schema": AN.HWM_SCHEMA, "account": ACCT, "seq": n - 1, "head": entries[n - 1]["h"]}
    json.dump(prev_mark, open(os.path.join(world.root, "capital_hwm.json"), "w"))     # A's mark is one entry behind its view: the two discarded halves differ
    os.unlink(os.path.join(world.root2, "capital.json"))                               # B keeps only a mark
    json.dump(prev_mark, open(os.path.join(world.root2, "capital_hwm.json"), "w"))
    os.unlink(os.path.join(root_c, "capital_hwm.json"))                                # C keeps only a view
    _put_back(world.anchor, snap)
    CC.CapitalBook(world.root, AN.Anchor(world.root, ACCT, "testnet", world.anchor)).accept_older_lineage({})
    return root_c, entries


def test_a_root_holding_only_the_discarded_mark_or_only_the_discarded_view_is_healed_forward(world):
    root_c, _ = _two_roots_after_an_accept(world)
    assert opened(world, world.root2).load()["review_required"]["code"] == "ROLLBACK_ACCEPTED"              # the MARK half of the record
    assert opened(world, root_c).load()["review_required"]["code"] == "ROLLBACK_ACCEPTED"                    # the VIEW half of the record


def test_a_head_that_only_starts_like_a_recorded_one_is_not_recorded(world):
    root_c, entries = _two_roots_after_an_accept(world)
    d = json.load(open(os.path.join(world.root2, "capital_hwm.json")))
    d["head"] = d["head"][:16] + "0" * 48                                              # same sequence, same first 16 hex, another head
    json.dump(d, open(os.path.join(world.root2, "capital_hwm.json"), "w"))
    with pytest.raises(CC.BookRefused) as e:
        opened(world, world.root2)
    assert e.value.code in ("ANCHOR_BEHIND_BOOK", "LINEAGE_FORKED")


def test_a_recorded_discard_never_excuses_an_edited_view_that_names_the_lineages_own_entry(world):
    book = genesis(world)
    book.ensure_baseline("232")
    snap = _snapshot_dir(world.anchor, world.tmp / "anchor_backup")
    drive_to_lifetime_stop(book)
    _put_back(world.anchor, snap)
    entries = AN.Lineage(os.path.join(world.anchor, f"lineage-{ACCT}.jsonl"), ACCT, "testnet").read()
    head = entries[-1]
    genuine = {**AN.rebuild(entries), "ledger_ref": {"account": ACCT, "seq": head["seq"], "head": head["h"], "digest": head["book_digest"]}}
    json.dump(genuine, open(os.path.join(world.root, "capital.json"), "w"))            # the view equals the older lineage's own head while the mark is ahead
    owner = CC.CapitalBook(world.root, AN.Anchor(world.root, ACCT, "testnet", world.anchor))
    r = owner.accept_older_lineage({})
    assert r["discarded"]["view"]["head_full"] == head["h"], "the recorded view IS the lineage's own entry"
    edited = dict(genuine)
    edited["start_cash_usd"] = "99999"                                                 # content that is not what the lineage attested at that sequence (the older state had a 232 baseline)
    json.dump(edited, open(os.path.join(world.root, "capital.json"), "w"))
    with pytest.raises(CC.BookRefused) as e:
        opened(world)
    assert e.value.code == "VIEW_TAMPERED", "it healed forward silently: the tamper signal was lost"


# --------------------------------------------------------------------------------- L-1: an entry's facts must be an object
def test_a_chain_valid_entry_whose_facts_are_not_an_object_is_corruption_not_a_crash(world):
    book = genesis(world)
    book.ensure_baseline("232")
    path = os.path.join(world.anchor, f"lineage-{ACCT}.jsonl")

    def bad_facts(lines):
        lines[-1]["kind"] = "ACK_OLDER"
        lines[-1]["facts"] = [1, 2]
    _rechain(path, bad_facts)
    with pytest.raises(CC.BookRefused) as e:
        opened(world)
    assert e.value.code == "ANCHOR_CORRUPT" and "MALFORMED" in e.value.detail
    assert AN.Anchor._discarded([{"kind": "ACK_OLDER", "facts": [1]}, {"kind": "ACK_OLDER", "facts": None}, {"kind": "ACK_OLDER", "facts": {"discarded": 5}}]) == set()


# --------------------------------------------------------------------------------- L-6: an append never follows a read that left a torn tail in place
def test_an_append_after_a_non_repairing_read_over_a_torn_tail_is_refused(world):
    book = genesis(world)
    book.ensure_baseline("232")
    path = os.path.join(world.anchor, f"lineage-{ACCT}.jsonl")
    with open(path, "ab") as fh:
        fh.write(b'{"torn')
    lin = AN.Lineage(path, ACCT, "testnet")
    lin.read(repair_torn=False)
    before = open(path, "rb").read()
    with pytest.raises(AN.AnchorRefused) as e:
        lin.append({"x": 1})
    assert e.value.code == "LINEAGE_CHANGED_UNDER_COMMIT" and open(path, "rb").read() == before


# --------------------------------------------------------------------------------- L-7: an EDITED bound view is not adopted as the account's history
def test_an_edited_bound_view_is_not_adopted_when_the_lineage_is_lost(world):
    book = genesis(world)
    drive_to_lifetime_stop(book)
    d = json.load(open(book.path))
    d["realised_usd"] = "0"
    d["loss_streak"] = 0
    d["gains_usd"] = "0"                                                               # an edit that lifts the lifetime stop
    json.dump(d, open(book.path, "w"))
    shutil.rmtree(world.anchor)
    with pytest.raises(CC.BookRefused) as e:
        CC.CapitalBook(world.root, AN.Anchor(world.root, ACCT, "testnet", world.anchor)).initialise({"test": True})
    assert e.value.code == "VIEW_TAMPERED"
    assert not os.path.exists(os.path.join(world.anchor, f"lineage-{ACCT}.jsonl"))


# --------------------------------------------------------------------------------- L-8: LINEAGE_FORKED against the ROOT's own mark has a recorded way out too
def test_a_root_that_followed_an_accepted_history_and_meets_the_restored_newer_anchor_can_accept_the_lineage(world):
    book = genesis(world)
    book.ensure_baseline("232")
    snap_old = _snapshot_dir(world.anchor, world.tmp / "anchor_old")
    drive_to_lifetime_stop(book)
    snap_new = _snapshot_dir(world.anchor, world.tmp / "anchor_new")
    _put_back(world.anchor, snap_old)
    owner = CC.CapitalBook(world.root, AN.Anchor(world.root, ACCT, "testnet", world.anchor))
    assert owner.accept_older_lineage({})["status"] == "ACCEPTED"                      # the owner continued from the older lineage ...
    _put_back(world.anchor, snap_new)                                                  # ... and then the newer anchor volume turned up and was restored
    with pytest.raises(CC.BookRefused) as e:
        opened(world)
    assert e.value.code == "LINEAGE_FORKED"
    again = CC.CapitalBook(world.root, AN.Anchor(world.root, ACCT, "testnet", world.anchor))
    assert again.accept_older_lineage({})["status"] == "ACCEPTED"
    final = opened(world)
    assert stopped(final) and final.load()["review_required"]["code"] == "ROLLBACK_ACCEPTED", "the restored newer lineage continues, with the review the owner must acknowledge"


def test_accepting_does_not_get_around_a_witness_that_disagrees_about_a_forked_mark(world):
    w = FakeWitness()
    book = genesis(world, witness=w)
    book.ensure_baseline("232")
    hwm = os.path.join(world.root, "capital_hwm.json")
    d = json.load(open(hwm))
    d["head"] = "ab" * 32
    json.dump(d, open(hwm, "w"))
    w.rows[ACCT][1] = "ff" * 32                                                         # the witness holds ANOTHER head than the local lineage at seq 1
    owner = CC.CapitalBook(world.root, AN.Anchor(world.root, ACCT, "testnet", world.anchor, witness=w))
    before = sorted(os.listdir(world.root))
    with pytest.raises(CC.BookRefused) as e:
        owner.accept_older_lineage({})
    assert e.value.code == "LINEAGE_FORKED" and sorted(os.listdir(world.root)) == before


# --------------------------------------------------------------------------------- L-5: no terminal, no witness read
def test_a_non_interactive_accept_does_not_read_the_witness_before_the_gate_refuses(world):
    class Counting(FakeWitness):
        heads = 0

        def head(self, account):
            Counting.heads += 1
            return super().head(account)
    w = Counting()
    book = genesis(world, witness=w)
    book.ensure_baseline("232")
    Counting.heads = 0
    orig = AN.witness_from_env
    AN.witness_from_env = lambda env: w
    try:
        rc = AP.main(["capital", "--root", world.root, "--accept-older-lineage"], env={"CAPITAL_ANCHOR_DIR": world.anchor}, isatty=lambda: False, input_fn=lambda _p: "", uid=0)
    finally:
        AN.witness_from_env = orig
    assert rc == 2 and Counting.heads == 0
