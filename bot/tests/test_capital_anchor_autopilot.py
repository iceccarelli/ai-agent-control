"""The capital lineage attacked THROUGH THE AUTOPILOT, end to end on the fake Testnet: the unattended session is the thing that must not trade when the capital record has been reset, restored, relocated or edited.

``test_capital_anchor.py`` proves the book. This file proves the integrated path (preflight states the account, the book is bound to it, the allocator reads the lineage's stop, the loop refuses before any order) and the
owner's one-time act (``capital --anchor-init``) that lets it start. Every refusal asserts that NO order was created.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
BOT = os.path.dirname(HERE)
for _p in (HERE, BOT, os.path.join(BOT, "tools")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import capital_testkit as CT  # noqa: E402
import ms_capital as CC  # noqa: E402
import ms_capital_anchor as AN  # noqa: E402
import ms_engine_state as ES  # noqa: E402
import ms_testnet_autopilot as AP  # noqa: E402
from ms_testnet_world import World  # noqa: E402
from test_ms_testnet_autopilot import _profit_world, write_grant  # noqa: E402,F401


def posts(w):
    return [r for r in w.fake.requests if r[0] == "POST"]


def session(w, eng, **kw):
    return AP.run_once(w.env, inner=w.fake, clock=w.clock.now, sleep=w.clock.sleep, out=[].append, state_root=str(w.tmp / "auto"), engine_root=str(eng), **kw)


@pytest.fixture
def w(tmp_path, monkeypatch):
    return World(tmp_path, monkeypatch, owner_initialised_account=False)


def lifetime_loss(book):
    """a lineage that carries a lifetime loss (-25), as five lost sessions would leave it"""
    book.ensure_baseline("10000")
    cash = 10000
    for i in range(5):
        assert book.park(f"lost_{i}", str(cash), "-5", True, True)
        cash -= 5
        book.settle(str(cash))
        if book.load()["halted"]:
            book.acknowledge_halt()
    assert D(book.load()["realised_usd"]) <= -CC.LIFETIME_LOSS_STOP_USD


from decimal import Decimal as D  # noqa: E402


def adopt_cash(w, cash):
    w.fake.wallet["USDT"] = D(str(cash))


# ---------------------------------------------------------------------------------------------- a new account needs the owner, once
def test_an_account_with_no_lineage_is_refused_without_an_order_or_an_alert_and_the_owner_can_initialise_it(w, capsys):
    _profit_world(w)
    eng = w.tmp / "eng"
    res = session(w, eng)
    assert res["result"] == "REFUSED" and res["why"] == ["CAPITAL_ANCHOR_REFUSED:ACCOUNT_NOT_INITIALISED"], res
    assert not posts(w) and not os.path.exists(eng / "capital.json"), "a refusal places nothing and writes no book"
    assert not ES.EngineState(str(eng), lambda: 0).has("ALERT"), "an owner-action refusal is a PAUSE, not an incident: nothing is wrong with the book"
    props = AN.proposals(AN.anchor_dir_for(os.environ))
    assert [p["account"] for p in props] == [CT.ACCOUNT], "the refusal left the owner the account to attest"
    # the owner, at a terminal, types the line the command prints
    def typed(_p):
        m = re.search(r"^(CONFIRM .+)$", capsys.readouterr().out, flags=re.M)
        return m.group(1).strip()
    rc = AP.main(["capital", "--root", str(eng), "--anchor-init"], env={}, isatty=lambda: True, input_fn=typed, uid=0)
    assert rc == 0
    res2 = session(w, eng)
    assert res2["result"] == "DONE", res2


def test_a_session_on_a_second_account_with_the_first_ones_root_is_refused(w):
    _profit_world(w)
    eng = w.tmp / "eng"
    CT.anchored(str(eng))                                      # the root belongs to account 100000001
    w.fake.user_id = 100000002                                  # the credentials now belong to another account
    res = session(w, eng)
    assert res["result"] == "REFUSED" and res["why"] == ["CAPITAL_ANCHOR_REFUSED:ACCOUNT_MISMATCH"], res
    assert not posts(w)
    assert ES.EngineState(str(eng), lambda: 0).has("ALERT"), "a book that does not belong to this account IS an incident"


def test_a_venue_that_states_no_account_pauses_the_loop_with_the_reason_and_never_guesses(w):
    _profit_world(w)
    eng = w.tmp / "eng"
    w.fake.user_id = None
    res = session(w, eng)
    assert res["result"] == "REFUSED" and res["why"] == ["CAPITAL_ANCHOR_REFUSED:ACCOUNT_IDENTITY_UNAVAILABLE"], res
    assert not posts(w) and not ES.EngineState(str(eng), lambda: 0).has("ALERT")


def test_the_owner_can_choose_the_key_id_as_the_identity_and_it_is_a_different_account_from_the_user_id(w):
    _profit_world(w)
    w.fake.user_id = None
    w.env["CAPITAL_ACCOUNT_IDENTITY_FALLBACK"] = "KEY_ID"
    eng = w.tmp / "eng"
    key_identity = AN.account_ref_of_key("testnet", "1")                     # the fake's query-api id
    CC.CapitalBook(str(eng), AN.Anchor(str(eng), key_identity, "testnet", AN.anchor_dir_for(os.environ))).initialise({"test": True})
    res = session(w, eng)
    assert res["result"] == "DONE", res
    assert key_identity != AN.account_ref_of("testnet", 1)


# ---------------------------------------------------------------------------------------------- the reset attacks, through the loop
@pytest.fixture
def lost(w):
    _profit_world(w)
    eng = w.tmp / "eng"
    book = CT.anchored(str(eng))
    lifetime_loss(book)
    adopt_cash(w, 9975)
    w.fake.requests.clear()
    return eng, book


@pytest.mark.parametrize("attack", ["delete_view", "delete_root", "other_root", "restore_older_view"])
def test_the_lifetime_stop_survives_a_reset_restore_or_relocation_and_no_order_is_created(w, lost, attack):
    eng, book = lost
    old_view = None
    if attack == "restore_older_view":
        # a consistent, valid, OLDER book: take it from a fresh account's genesis state
        old_view = json.dumps({**json.load(open(eng / "capital.json")), **{"realised_usd": "0", "losses_usd": "0", "loss_streak": 0}})
    target = eng
    if attack == "delete_view":
        os.unlink(eng / "capital.json")
    elif attack == "delete_root":
        shutil.rmtree(eng)
    elif attack == "other_root":
        target = w.tmp / "another_root"
    else:
        (eng / "capital.json").write_text(old_view)
    res = session(w, target)
    if attack == "restore_older_view":
        assert res["result"] == "REFUSED" and res["why"] == ["CAPITAL_ANCHOR_REFUSED:VIEW_TAMPERED"], res      # an edited view is an incident
    else:
        assert res["result"] == "SKIPPED" and "LIFETIME_LOSS_STOP" in "".join(res["why"]), res                    # rebuilt from the lineage: the stop is still there
    assert not posts(w), "no order may be created while the lifetime stop stands"


def test_an_account_whose_cash_fell_while_nothing_was_pending_is_refused_with_the_review_and_no_order(w, capsys):
    _profit_world(w)
    eng = w.tmp / "eng"
    book = CT.anchored(str(eng))
    book.ensure_baseline("10000")
    adopt_cash(w, 9990)                                         # 10 USD went missing between sessions: no pending session, no declaration
    res = session(w, eng)
    assert res["result"] == "REFUSED" and res["why"] == ["ACCOUNT_CASH_UNEXPLAINED"], res
    assert not posts(w)
    cb = CT.anchored(str(eng)).load()
    assert cb["history"][-1]["window"] == "IDLE_GAP" and D(cb["realised_usd"]) == D("-10")


def test_a_rolled_back_volume_is_caught_by_the_venues_cash_through_the_loop(w):
    """the whole state directory AND the anchor are put back to before the last session: both are valid and clean, and the venue's cash re-books the missing money"""
    _profit_world(w)
    eng = w.tmp / "eng"
    book = CT.anchored(str(eng))
    book.ensure_baseline("10000")
    adir = AN.anchor_dir_for(os.environ)
    snap = w.tmp / "snap"
    shutil.copytree(adir, snap / "anchor")
    shutil.copytree(eng, snap / "eng")
    book.park("lost_a", "10000", "-30", True, True)
    book.settle("9970")
    adopt_cash(w, 9970)
    shutil.rmtree(adir)
    shutil.rmtree(eng)
    shutil.copytree(snap / "anchor", adir)
    shutil.copytree(snap / "eng", eng)
    res = session(w, eng)
    assert res["result"] == "REFUSED" and res["why"] == ["ACCOUNT_CASH_UNEXPLAINED"], res
    assert not posts(w) and D(CT.anchored(str(eng)).load()["realised_usd"]) == D("-30")


# ---------------------------------------------------------------------------------------------- the remote witness, through the loop
class Witness:
    configured = True

    def __init__(self, required=True, probe=None, publishes_before_failing=None):
        self.required, self.rows, self.down, self.probe, self.orders_at = required, {}, False, probe, {}
        self.left = publishes_before_failing                      # None = never; N = the (N+1)th publish and every later one is an outage (reads keep working)

    def head(self, account):
        if self.down:
            raise AN.WitnessError("down")
        r = self.rows.get(account) or {}
        return (max(r), r[max(r)]) if r else None

    def publish(self, account, entry):
        if self.down:
            raise AN.WitnessError("down")
        if self.left is not None:
            if self.left <= 0:
                raise AN.WitnessError("publish outage")
            self.left -= 1
        self.rows.setdefault(account, {})[entry["seq"]] = entry["h"]
        self.orders_at.setdefault(entry["seq"], self.probe() if self.probe else None)      # how many orders had left when this head reached the witness


def test_a_required_witness_that_is_down_refuses_before_any_order_without_an_alert_and_the_session_runs_when_it_is_back(w, monkeypatch):
    _profit_world(w)
    eng = w.tmp / "eng"
    wit = Witness()
    monkeypatch.setattr(AN, "witness_from_env", lambda env: wit)
    CT.anchored(str(eng))
    wit.down = True
    res = session(w, eng)
    assert res["result"] == "REFUSED" and res["why"] == ["CAPITAL_ANCHOR_REFUSED:WITNESS_UNAVAILABLE"], res
    assert not posts(w) and not ES.EngineState(str(eng), lambda: 0).has("ALERT"), "an outage is transient: it must not cost the owner a typed ALERT clearance"
    wit.down = False
    res2 = session(w, eng)
    assert res2["result"] == "DONE", res2
    head = AN.Lineage(os.path.join(AN.anchor_dir_for(os.environ), f"lineage-{CT.ACCOUNT}.jsonl"), CT.ACCOUNT, "testnet").read()[-1]
    assert max(wit.rows[CT.ACCOUNT]) == head["seq"], "the witness holds the head the session ended on"


def test_the_park_placeholder_is_witnessed_before_the_first_order_leaves(w, monkeypatch):
    _profit_world(w)
    eng = w.tmp / "eng"
    wit = Witness(probe=lambda: len([r for r in w.fake.requests if r[0] == "POST" and "/v5/order/create" in r[1]]))
    monkeypatch.setattr(AN, "witness_from_env", lambda env: wit)
    CT.anchored(str(eng))
    res = session(w, eng)
    assert res["result"] == "DONE", res
    kinds = [e["kind"] for e in AN.Lineage(os.path.join(AN.anchor_dir_for(os.environ), f"lineage-{CT.ACCOUNT}.jsonl"), CT.ACCOUNT, "testnet").read()]
    park_seq = kinds.index("PARK")
    assert park_seq in wit.orders_at and wit.orders_at[park_seq] == 0, (park_seq, wit.orders_at)


def test_an_outage_that_starts_after_the_session_opened_is_stopped_by_the_barrier_before_the_first_order(w, monkeypatch):
    """the witness answered when the book was opened (in sync), then could not take the PARK head: the open-time check cannot see that, the barrier before the first order must"""
    _profit_world(w)
    eng = w.tmp / "eng"
    wit = Witness(publishes_before_failing=2)                    # open publishes GENESIS (1), the baseline commit (2); the PARK commit's publish and the barrier's are an outage
    monkeypatch.setattr(AN, "witness_from_env", lambda env: wit)
    CT.anchored(str(eng))
    res = session(w, eng)
    assert res["result"] == "REFUSED" and res["why"] == ["CAPITAL_ANCHOR_REFUSED:WITNESS_UNAVAILABLE"], res
    assert not posts(w), "no order may leave on a head the required witness has not acknowledged"
    assert not ES.EngineState(str(eng), lambda: 0).has("ALERT")
    book = CT.anchored(str(eng))
    assert book.load()["pending"]["session_id"], "the placeholder was parked; the next cash read settles it (delta ~0)"


# ---------------------------------------------------------------------------------------------- what the owner can read BEFORE deploying
def test_preflight_states_the_account_identity_the_book_will_bind_to(tmp_path, monkeypatch):
    w0 = World(tmp_path, monkeypatch)
    assert w0.run("preflight") == 0
    assert f"capital account identity: {CT.ACCOUNT} (basis USER_ID)" in w0.text()
    doc = json.load(open(os.path.join(w0.state_dir, "preflight.json")))
    assert doc["account_ref"] == CT.ACCOUNT and "100000001" not in json.dumps(doc), "the raw userID is never written down"


def test_preflight_says_so_when_the_venue_states_no_account(tmp_path, monkeypatch):
    w0 = World(tmp_path, monkeypatch)
    w0.fake.user_id = None
    assert w0.run("preflight") == 0, "a typed session does not need the capital book: preflight still passes"
    assert "capital account identity: UNAVAILABLE" in w0.text() and "CAPITAL_ACCOUNT_IDENTITY_FALLBACK=KEY_ID" in w0.text()
    doc = json.load(open(os.path.join(w0.state_dir, "preflight.json")))
    assert doc["account_ref"] is None and doc["account_identity_basis"] == "UNAVAILABLE"


def test_the_witness_check_command_is_read_only_and_reports_a_rollback(tmp_path, monkeypatch, capsys):
    import ms_capital_anchor as ANC
    w0 = World(tmp_path, monkeypatch)
    root = str(tmp_path / "rootc")
    CT.anchored(root)
    adir = AN.anchor_dir_for(os.environ)
    lineage = os.path.join(adir, f"lineage-{CT.ACCOUNT}.jsonl")
    before = open(lineage, "rb").read()
    wit = Witness()
    wit.rows[CT.ACCOUNT] = {0: AN.Lineage(lineage, CT.ACCOUNT, "testnet").read()[0]["h"], 5: "ab" * 32}      # the witness holds a NEWER head than the local lineage
    monkeypatch.setattr(ANC, "witness_from_env", lambda env: wit)
    rc = AP.main(["capital", "--root", root, "--check-witness"], env={})
    out = json.loads(capsys.readouterr().out)
    assert rc == 2 and out["witness"] == "LOCAL_BEHIND_WITNESS", out
    assert open(lineage, "rb").read() == before and 1 not in wit.rows[CT.ACCOUNT], "checking publishes and writes nothing"


# =====================================================================================================================================================================================================
# ROUND 10, INDEPENDENT RED TEAM (loop and session classification)
# =====================================================================================================================================================================================================
def test_a_lock_timeout_inside_the_park_is_a_transient_anchor_refusal_not_an_unreadable_book(w, monkeypatch):
    """park() used to DROP the anchor's code: a lock that did not free up in time became CAPITAL_BOOK_UNREADABLE, which is neither owner-side nor transient (three in a row would raise the repeated-refusal ALERT)."""
    _profit_world(w)
    eng = w.tmp / "eng"
    CT.anchored(str(eng))

    def stuck(self, *a, **k):
        raise CC.BookRefused("LOCK_TIMEOUT", "the capital lineage lock is held by another process")
    monkeypatch.setattr(CC.CapitalBook, "park", stuck)
    res = session(w, eng)
    assert res["result"] == "REFUSED" and res["why"] == ["CAPITAL_ANCHOR_REFUSED:LOCK_TIMEOUT"], res
    assert not posts(w) and not ES.EngineState(str(eng), lambda: 0).has("ALERT")
    assert any(res["why"][0].startswith(p) for p in AP.OWNER_AUTHORITY_REFUSALS), "the loop classifies it as owner-side/transient: it neither alerts nor counts as an unexpected refusal"


def _loop_with(tmp_path, reasons, n=6):
    seq = list(reasons)
    last = reasons[-1]

    def once(env, **kw):
        return {"result": "REFUSED", "why": [seq.pop(0) if seq else last]}

    t = [1_700_000_000_000]

    def clock():
        t[0] += 1000
        return t[0]
    root = str(tmp_path / ("loop_" + reasons[0].replace(":", "_")))
    AP.loop({}, root=root, once=once, clock=clock, sleep=lambda s: None, out=[].append, max_iterations=n, recover_fn=lambda env, **k: (True, [], {"venue_read_complete": True, "reconciled_ms": 1}))
    eng = ES.EngineState(root, clock)
    return eng.has("ALERT"), eng.read("ALERT") or {}


@pytest.mark.parametrize("code", sorted(AP.NO_ALERT_ANCHOR_CODES))
def test_the_loop_given_an_owner_action_or_transient_anchor_refusal_six_times_raises_no_alert(tmp_path, code):
    alerted, alert = _loop_with(tmp_path, ["CAPITAL_ANCHOR_REFUSED:" + code])
    assert alerted is False, (code, alert)


@pytest.mark.parametrize("code", ["ACCOUNT_MISMATCH", "VIEW_TAMPERED", "ANCHOR_BEHIND_BOOK", "ANCHOR_CORRUPT", "ANCHOR_MISSING_BOOK_BOUND", "LOCAL_BEHIND_WITNESS", "LINEAGE_FORKED", "WITNESS_MISCONFIGURED"])
def test_the_loop_given_an_incident_anchor_refusal_repeatedly_raises_an_alert(tmp_path, code):
    alerted, alert = _loop_with(tmp_path, ["CAPITAL_ANCHOR_REFUSED:" + code])
    assert alerted is True, code


def test_the_decision_record_and_the_evidence_event_state_the_epoch_and_the_result_over_all_epochs(w):
    import exec_lifecycle as EL
    book = CT.anchored(str(w.tmp / "eng_epochs"))
    book.ensure_baseline("232")
    for n in range(1, 3):
        book.park(f"s{n}", str(232 - 5 * (n - 1)), "-5", True, True)
        book.settle(str(232 - 5 * n))
    book.rebaseline("a reason of my own about the wallet reset", {"test": True})
    rep = book.report()
    ev = EL.capital_book(rep)
    assert ev[2]["epoch"] == 1 and D(ev[2]["all_epochs_verified_result_usd"]) == D("-10"), "a new epoch must never read as a fresh account"
    ctx = AP._decision_context(str(w.tmp / "eng_epochs"), "0")
    assert ctx["portfolio"]["epoch"] == 1 and D(ctx["portfolio"]["all_epochs_verified_result_usd"]) == D("-10"), ctx["portfolio"]
