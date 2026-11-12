"""The decision table of ``Anchor.open_view``, checked against an INDEPENDENT oracle over every combination of what a root can hold (Mission round 10, the red team's recurring finding).

``open_view`` is the function that decides whether the capital book may be used. Three independent review passes found a flaw in it each time (a deleted view healed to an older head; a damaged mark masked a newer view and
``--rebuild-view`` then performed the rollback; a damaged view's sequence masked a forked mark): each was a state nobody had enumerated. This file enumerates them: three lineages (the newer one, an older prefix of it,
and the older one after the owner accepted it) x eleven things the view file can hold x ten things the mark file can hold, and for each state it checks, against a specification written here and NOT derived from the code:

  * the unattended open: it opens exactly when nothing the root remembers is ahead of, or forks from, the lineage (unless the owner recorded that history as discarded), heals the view and the mark forward when it does,
    leaves both consistent with the head, and is idempotent; when it refuses it writes NOTHING;
  * ``capital --rebuild-view`` never replaces a state that is ahead of the lineage, forks from it or belongs to another account;
  * ``capital --accept-older-lineage`` succeeds exactly for ``ANCHOR_BEHIND_BOOK`` and ``LINEAGE_FORKED``, appends exactly one ``ACK_OLDER`` entry, keeps what it discards, continues from the lineage's own head state with a
    review, records the discarded claims so that the same state then opens, and a second acceptance is a no-op.

A change to the decision table that this file does not expect fails here, in one place, with the state that breaks.
"""
from __future__ import annotations

import copy
import json
import os
import shutil
import sys
from decimal import Decimal as D

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
BOT = os.path.dirname(HERE)
for _p in (HERE, BOT, os.path.join(BOT, "tools")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import ms_capital as CC  # noqa: E402
import ms_capital_anchor as AN  # noqa: E402

ACCT = AN.account_ref_of("testnet", 100000001)
OTHER = AN.account_ref_of("testnet", 100000002)
FORBIDDING = {"VIEW_AHEAD", "MARK_AHEAD", "MARK_FORKED", "ACCOUNT"}        # what a rebuild must never replace


# ------------------------------------------------------------------------------------------------------------------------------------------------------------------ the artefacts, from real operations
def _run(dirpath):
    """a real history: genesis, a baseline and four lost sessions (a halt on the way); returns (lineage bytes, {seq: (view text, mark text)}) captured after every commit"""
    anchor, root = str(dirpath / "anchor"), str(dirpath / "root")
    snaps = {}

    def hook(point):
        if point == "after_view":
            n = len(open(os.path.join(anchor, f"lineage-{ACCT}.jsonl")).read().splitlines()) - 1
            snaps[n] = (open(os.path.join(root, "capital.json")).read(), open(os.path.join(root, "capital_hwm.json")).read())
    AN.crash_hook = hook
    try:
        CC.CapitalBook(root, AN.Anchor(root, ACCT, "testnet", anchor)).initialise({"test": True})
        book = CC.CapitalBook.anchored(root, account_ref=ACCT, anchor_dir=anchor)
        book.ensure_baseline("232")
        cash = D("232")
        for n in range(1, 5):
            book.ensure_baseline(str(cash))
            assert book.park(f"s{n}", str(cash), "-5", True, True)
            book.settle(str(cash - 5))
            cash -= 5
            if book.load().get("halted"):
                book.acknowledge_halt()
    finally:
        AN.crash_hook = AN._noop
    return open(os.path.join(anchor, f"lineage-{ACCT}.jsonl"), "rb").read(), snaps


@pytest.fixture(scope="module")
def art(tmp_path_factory):
    base = tmp_path_factory.mktemp("matrix_artefacts")
    (base / "n").mkdir()
    lin_n, snaps_n = _run(base / "n")
    lines = lin_n.splitlines(keepends=True)
    older_len = 3                                                                          # the older lineage ends at seq 2
    lin_o = b"".join(lines[:older_len])
    # a DIFFERENT history of the same account (another baseline): it names other heads at the same sequences
    anchor_a, root_a = str(base / "a" / "anchor"), str(base / "a" / "root")
    CC.CapitalBook(root_a, AN.Anchor(root_a, ACCT, "testnet", anchor_a)).initialise({"test": True})
    other = CC.CapitalBook.anchored(root_a, account_ref=ACCT, anchor_dir=anchor_a)
    other.ensure_baseline("999")
    other.park("x1", "999", None, False, False)
    mark_a = json.load(open(os.path.join(root_a, "capital_hwm.json")))
    view_a = json.load(open(os.path.join(root_a, "capital.json")))
    # the older lineage after the owner accepted it, on a root that held the newer state
    k_anchor, k_root = str(base / "k" / "anchor"), str(base / "k" / "root")
    os.makedirs(k_anchor)
    os.makedirs(k_root)
    open(os.path.join(k_anchor, f"lineage-{ACCT}.jsonl"), "wb").write(lin_o)
    head_n = max(snaps_n)
    open(os.path.join(k_root, "capital.json"), "w").write(snaps_n[head_n][0])
    open(os.path.join(k_root, "capital_hwm.json"), "w").write(snaps_n[head_n][1])
    r = CC.CapitalBook(k_root, AN.Anchor(k_root, ACCT, "testnet", k_anchor)).accept_older_lineage({"test": True})
    assert r["status"] == "ACCEPTED"
    lin_k = open(os.path.join(k_anchor, f"lineage-{ACCT}.jsonl"), "rb").read()
    k2_anchor, k2_root = str(base / "k2" / "anchor"), str(base / "k2" / "root")
    os.makedirs(k2_anchor)
    os.makedirs(k2_root)
    open(os.path.join(k2_anchor, f"lineage-{ACCT}.jsonl"), "wb").write(lin_o)
    open(os.path.join(k2_root, "capital.json"), "w").write(snaps_n[2][0])                  # the view IS the older lineage's own head ...
    open(os.path.join(k2_root, "capital_hwm.json"), "w").write(snaps_n[head_n][1])         # ... while the mark is ahead
    assert CC.CapitalBook(k2_root, AN.Anchor(k2_root, ACCT, "testnet", k2_anchor)).accept_older_lineage({"test": True})["status"] == "ACCEPTED"
    lin_k2 = open(os.path.join(k2_anchor, f"lineage-{ACCT}.jsonl"), "rb").read()
    return {"N": lin_n, "O": lin_o, "K": lin_k, "K2": lin_k2, "snaps": snaps_n, "head_n": head_n, "mark_a": mark_a, "view_a": view_a}


def _edited_at(text, **kw):
    d = json.loads(text)
    d.update(kw)
    return json.dumps(d)


def _views(art):
    sn, hn = art["snaps"], art["head_n"]
    head_view = json.loads(sn[hn][0])

    def edited(**kw):
        d = copy.deepcopy(head_view)
        d.update(kw)
        return json.dumps(d)

    def with_ref(**kw):
        d = copy.deepcopy(head_view)
        d["ledger_ref"].update(kw)
        return json.dumps(d)
    legacy = {k: v for k, v in head_view.items() if k != "ledger_ref"}
    return {
        "absent": None,
        "garbage": "not json",
        "not an object": "[1, 2]",
        "legacy (no ledger_ref)": json.dumps(legacy),
        "N at seq 2": sn[2][0],
        "N at its head": sn[hn][0],
        "N at its head, content edited": edited(start_cash_usd="99999"),
        "N at seq 2, content edited": _edited_at(sn[2][0], start_cash_usd="99999"),
        "N at its head, sequence malformed": with_ref(seq="x"),
        "N at its head, another account": with_ref(account=OTHER),
        "another history at seq 2": json.dumps(art["view_a"]),
        "N's head with a forged tail": with_ref(head=head_view["ledger_ref"]["head"][:-4] + "0000"),
    }


def _marks(art):
    sn, hn = art["snaps"], art["head_n"]
    head_mark = json.loads(sn[hn][1])

    def with_(**kw):
        d = copy.deepcopy(head_mark)
        d.update(kw)
        return json.dumps(d)
    return {
        "absent": None,
        "garbage": "not json",
        "another account": with_(account=OTHER),
        "N at seq 2": sn[2][1],
        "N at its head": sn[hn][1],
        "N's head with a forged tail": with_(head=head_mark["head"][:-4] + "0000"),
        "another history at seq 2": json.dumps(art["mark_a"]),
        "negative sequence": with_(seq=-1),
        "head too short": with_(head="abcd"),
        "N at seq 2 but another head": with_(seq=2, head="cd" * 32),
    }


# ------------------------------------------------------------------------------------------------------------------------------------------------------------------ the specification (not the code)
def _discarded(entries):
    out = set()
    for e in entries:
        if e["kind"] == "ACK_OLDER":
            for part in (e["facts"].get("discarded") or {}).values():
                if isinstance(part, dict) and isinstance(part.get("seq"), int) and isinstance(part.get("head_full"), str) and len(part["head_full"]) == 64:
                    out.add((part["seq"], part["head_full"]))
    return out


def _view_claim(text):
    """('absent' | 'unreadable' | 'legacy' | 'bound', parsed)"""
    if text is None:
        return "absent", None
    try:
        d = json.loads(text)
    except ValueError:
        return "unreadable", None
    if not isinstance(d, dict):
        return "unreadable", None
    return ("bound", d) if isinstance(d.get("ledger_ref"), dict) else ("legacy", d)


def _mark_claim(text):
    if text is None:
        return "absent", None
    try:
        d = json.loads(text)
    except ValueError:
        return "unreadable", None
    ok = (isinstance(d, dict) and d.get("schema") == AN.HWM_SCHEMA and AN.is_account_ref(d.get("account")) and type(d.get("seq")) is int and d["seq"] >= 0
          and isinstance(d.get("head"), str) and len(d["head"]) == 64 and all(c in "0123456789abcdef" for c in d["head"]))
    return ("ok", d) if ok else ("unreadable", None)


def _reasons(entries, vtext, mtext):
    """why the book must NOT open (empty = it must open, healing forward when needed)"""
    head = entries[-1]["seq"]
    gone = _discarded(entries)
    out = set()
    vk, v = _view_claim(vtext)
    if vk == "bound":
        ref = v["ledger_ref"]
        if ref.get("account") != ACCT or v.get("account") not in (None, ACCT):
            out.add("ACCOUNT")
        else:
            vs = ref.get("seq")
            if type(vs) is not int or vs < 0:
                out.add("VIEW_MALFORMED")
            elif vs > head:
                if (vs, ref.get("head")) not in gone:
                    out.add("VIEW_AHEAD")
            else:
                at = entries[vs]
                good = ref.get("head") == at["h"] and ref.get("digest") == at["book_digest"] and AN.book_digest(v) == at["book_digest"]
                if not good and (ref.get("head") == at["h"] or (vs, ref.get("head")) not in gone):
                    out.add("VIEW_TAMPERED")
    if vk == "legacy":
        out.add("VIEW_UNBOUND")
    mk, m = _mark_claim(mtext)
    if mk == "unreadable":
        out.add("MARK_UNREADABLE")
    if mk == "ok":
        if m["account"] != ACCT:
            out.add("ACCOUNT")
        elif m["seq"] > head:
            if (m["seq"], m["head"]) not in gone:
                out.add("MARK_AHEAD")
        elif entries[m["seq"]]["h"] != m["head"]:
            if (m["seq"], m["head"]) not in gone:
                out.add("MARK_FORKED")
    return out


def _expected_code(reasons):
    """the refusal the owner reads, by precedence: another account, then anything AHEAD of the lineage (the view's own sequence is judged before the mark's state), then a forked mark, then a damaged or edited record, then a pre-anchor file"""
    if "ACCOUNT" in reasons:
        return "ACCOUNT_MISMATCH"
    if reasons & {"VIEW_AHEAD", "MARK_AHEAD"}:
        return "ANCHOR_BEHIND_BOOK"
    if "MARK_FORKED" in reasons:
        return "LINEAGE_FORKED"
    if reasons & {"MARK_UNREADABLE", "VIEW_MALFORMED", "VIEW_TAMPERED"}:
        return "VIEW_TAMPERED"
    return "VIEW_UNBOUND"


# ------------------------------------------------------------------------------------------------------------------------------------------------------------------ the sandbox for one state
def _entries(lineage_bytes):
    return [json.loads(line) for line in lineage_bytes.decode().splitlines() if line.strip()]


def _tree(*dirs):
    out = {}
    for d in dirs:
        for dp, _dn, fn in os.walk(d):
            for n in fn:
                if n.endswith(".lock") or n.startswith("proposal-"):
                    continue
                p = os.path.join(dp, n)
                out[os.path.relpath(p, os.path.dirname(d))] = open(p, "rb").read()
    return out


def _make(tmp, lineage, vtext, mtext):
    shutil.rmtree(tmp, ignore_errors=True)
    anchor, root = os.path.join(tmp, "anchor"), os.path.join(tmp, "root")
    os.makedirs(anchor)
    os.makedirs(root)
    open(os.path.join(anchor, f"lineage-{ACCT}.jsonl"), "wb").write(lineage)
    if vtext is not None:
        open(os.path.join(root, "capital.json"), "w").write(vtext)
    if mtext is not None:
        open(os.path.join(root, "capital_hwm.json"), "w").write(mtext)
    return anchor, root


def _open(anchor, root):
    try:
        return "OK", CC.CapitalBook.anchored(root, account_ref=ACCT, venue="testnet", anchor_dir=anchor)
    except CC.BookRefused as exc:
        return exc.code, None


def _owner(anchor, root):
    return CC.CapitalBook(root, AN.Anchor(root, ACCT, "testnet", anchor))


# ------------------------------------------------------------------------------------------------------------------------------------------------------------------ the matrix
@pytest.mark.parametrize("which", ["N", "O", "K", "K2"])
def test_the_decision_table_of_open_view_matches_the_specification_in_every_state(art, tmp_path, capsys, which):
    entries = _entries(art[which])
    views, marks = _views(art), _marks(art)
    stats = {"open": 0, "refused": 0, "rebuilt": 0, "accepted": 0, "states": 0}
    for vname, vtext in views.items():
        for mname, mtext in marks.items():
            stats["states"] += 1
            where = f"lineage {which}, view '{vname}', mark '{mname}'"
            reasons = _reasons(entries, vtext, mtext)
            # ---- the unattended open
            anchor, root = _make(str(tmp_path / "s"), art[which], vtext, mtext)
            before = _tree(anchor, root)
            code, book = _open(anchor, root)
            if code == "OK":
                stats["open"] += 1
                assert not reasons, f"{where}: opened although {sorted(reasons)} forbid it"
                ref = json.load(open(os.path.join(root, "capital.json")))["ledger_ref"]
                mark = json.load(open(os.path.join(root, "capital_hwm.json")))
                assert ref["seq"] == entries[-1]["seq"] and ref["head"] == entries[-1]["h"], f"{where}: the healed view is not at the head"
                assert mark["seq"] == entries[-1]["seq"] and mark["head"] == entries[-1]["h"], f"{where}: the healed mark is not at the head"
                assert _open(anchor, root)[0] == "OK", f"{where}: reopening is not idempotent"
                continue
            stats["refused"] += 1
            assert reasons, f"{where}: refused ({code}) although nothing forbids it"
            assert code == _expected_code(reasons), f"{where}: refused as {code}, the specification says {_expected_code(reasons)} ({sorted(reasons)})"
            assert _tree(anchor, root) == before, f"{where}: a refusal wrote something"
            # ---- the owner's rebuild
            anchor, root = _make(str(tmp_path / "s"), art[which], vtext, mtext)
            try:
                r = _owner(anchor, root).rebuild_view()
            except CC.BookRefused:
                r = "REFUSED"
            if r == "REBUILT":
                stats["rebuilt"] += 1
                assert not (reasons & FORBIDDING), f"{where}: rebuild_view replaced what {sorted(reasons & FORBIDDING)} remember"
                assert _open(anchor, root)[0] == "OK", f"{where}: a rebuilt root does not open"
            # ---- the owner's recorded acceptance
            anchor, root = _make(str(tmp_path / "s"), art[which], vtext, mtext)
            pre_view, pre_mark = vtext, mtext
            n_before = len(_entries(open(os.path.join(anchor, f"lineage-{ACCT}.jsonl"), "rb").read()))
            try:
                a = _owner(anchor, root).accept_older_lineage({"test": True})["status"]
            except CC.BookRefused:
                a = "REFUSED"
            accepted = code in ("ANCHOR_BEHIND_BOOK", "LINEAGE_FORKED")
            if not accepted:
                assert a != "ACCEPTED", f"{where}: accepted although the refusal was {code}"
                continue
            assert a == "ACCEPTED", f"{where}: {code} left the owner no recorded way out ({a})"
            stats["accepted"] += 1
            ents = _entries(open(os.path.join(anchor, f"lineage-{ACCT}.jsonl"), "rb").read())
            assert len(ents) == n_before + 1 and ents[-1]["kind"] == "ACK_OLDER", f"{where}: not exactly one ACK_OLDER entry"
            for fname, pre in (("capital.json", pre_view), ("capital_hwm.json", pre_mark)):
                if pre is not None:
                    kept = [n for n in os.listdir(root) if n.startswith(fname + ".rejected-newer.")]
                    assert any(open(os.path.join(root, n)).read() == pre for n in kept), f"{where}: {fname} was not kept"
            code2, book2 = _open(anchor, root)
            assert code2 == "OK", f"{where}: does not open after the acceptance ({code2})"
            b = book2.load()
            older = AN.rebuild(ents[:-1])
            assert b["review_required"]["code"] == "ROLLBACK_ACCEPTED" and b["realised_usd"] == older["realised_usd"] and b["loss_streak"] == older["loss_streak"] and b["halted"] == older["halted"], (
                f"{where}: the book does not continue from the lineage's own head state with a review")
            assert _owner(anchor, root).accept_older_lineage({})["status"] == "NOTHING_TO_ACCEPT", f"{where}: a second acceptance is not a no-op"
    assert stats["states"] == len(views) * len(marks)
    assert stats["open"] > 0 and stats["refused"] > 0 and stats["accepted"] > 0, stats        # a matrix that never opens, never refuses or never accepts proves nothing
    if which == "N":
        assert stats["rebuilt"] > 0, stats


def test_the_matrix_contains_the_states_the_red_team_found_flaws_in(art):
    """the three flaws were: (1) older anchor + deleted view, (2) older anchor + a DAMAGED mark + the newer view, (3) older anchor + a damaged view sequence + a forked or newer mark. They must be states of the matrix."""
    views, marks = _views(art), _marks(art)
    assert "absent" in views and "N at its head" in views and "N at its head, sequence malformed" in views
    assert "garbage" in marks and "N at its head" in marks and "N at seq 2 but another head" in marks and "another history at seq 2" in marks
    o = _entries(art["O"])
    assert _reasons(o, None, marks["N at its head"]) == {"MARK_AHEAD"}                          # (1)
    assert _reasons(o, views["N at its head"], marks["garbage"]) == {"VIEW_AHEAD", "MARK_UNREADABLE"}      # (2)
    assert _reasons(o, views["N at its head, sequence malformed"], marks["N at its head"]) == {"VIEW_MALFORMED", "MARK_AHEAD"}  # (3)
