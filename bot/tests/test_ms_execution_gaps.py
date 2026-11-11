"""Recovery and hedge-completion gaps in the Testnet execution path, each reproduced on the fake venue before it was fixed (an independent read-only review found them on PR head 45d04b0):

* a torn LAST journal line (killed or out of disk mid-append) must not disable the owner's emergency unwind;
* a spot BUY whose fee is taken in the base coin, with the order row ahead of the execution list for one read, must not leave phantom inventory that the exit then cannot sell;
* one transient failure of the FIRST entry create must not leave a naked perp for the whole hold while the engine reports the pair hedged.

In-process fake Bybit testnet only: no network, no credential."""
import json
import os
import sys
from decimal import Decimal as D

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
BOT = os.path.dirname(HERE)
sys.path[:0] = [HERE, BOT, os.path.join(BOT, "tools")]

from ms_fake_bybit_testnet import SimulatedCrash  # noqa: E402
from ms_testnet_world import World  # noqa: E402

CREATE = "/v5/order/create"


@pytest.fixture
def w(tmp_path, monkeypatch):
    return World(tmp_path, monkeypatch)


def _ready(w, hold="600"):
    assert w.run("preflight") == 0
    assert w.run("plan", "--symbol", "BTCUSDT", "--notional-usd", "90", "--hold-seconds", hold) == 0
    return w.plan_doc()["plan_digest"]


def _journal(w):
    return [json.loads(l) for l in open(os.path.join(w.state_dir, "engine_journal.jsonl"))]


def test_a_torn_last_journal_line_does_not_disable_the_owners_unwind(w):
    """A process killed mid-append (ENOSPC / power loss) leaves a partial last line while the hedge is open: `reconcile --unwind` must still flatten (the torn line was never acted on: write-ahead)."""
    d = _ready(w)
    real_sleep, n = w.clock.sleep, {"n": 0}

    def sleep(s):
        real_sleep(s)
        if any(p["size"] != 0 for p in w.fake.positions.values()):
            n["n"] += 1
            if n["n"] >= 3:
                raise SimulatedCrash("killed during the hold")
    w.clock.sleep = sleep
    with pytest.raises(SimulatedCrash):
        w.run("execute", "--confirm-plan-digest", d, typed=d)
    w.clock.sleep = real_sleep
    assert any(p["size"] != 0 for p in w.fake.positions.values())
    jp = os.path.join(w.state_dir, "engine_journal.jsonl")
    lines = open(jp).read().splitlines()
    with open(jp, "w") as fh:
        fh.write("\n".join(lines[:-1]) + "\n" + lines[-1][: len(lines[-1]) // 2])             # the torn tail
    assert w.run("reconcile", "--unwind", "--confirm-plan-digest", d) == 0, w.text()
    assert all(p["size"] == 0 for p in w.fake.positions.values()) and w.fake.wallet["BTC"] == 0 and w.record()["flat_verified"] is True


@pytest.mark.parametrize("baseline_btc", [0.0, 0.05])
def test_a_spot_buy_whose_fee_is_taken_in_base_coin_and_whose_executions_lag_the_order_row_still_closes_flat(w, baseline_btc):
    """The order row (gross cumExecQty) is ahead of the execution list for ONE read: the adopted (recon) quantity must not survive as phantom inventory that the exit then tries to sell (and the authority refuses)."""
    w.fake.spot_fee_in_base = True
    w.fake.wallet["BTC"] = D(str(baseline_btc))
    d = _ready(w)
    w.fake.hidden_execs.add("E000002")                                                         # the spot BUY's execution row is not listed yet
    real_sleep, n = w.clock.sleep, {"n": 0}

    def sleep(s):
        real_sleep(s)
        n["n"] += 1
        if n["n"] == 1:
            w.fake.hidden_execs.discard("E000002")
    w.clock.sleep = sleep
    rc = w.run("execute", "--confirm-plan-digest", d, typed=d)
    assert rc == 0 and w.record()["flat_verified"] is True, w.text()[-400:]
    assert w.fake.wallet["BTC"] == D(str(baseline_btc)) and not [h for h in _journal(w) if h["type"] == "HALT_GROUP"]


@pytest.mark.parametrize("kind", ["http500", "rate_limit", "timeout"])
def test_one_transient_failure_of_the_first_create_never_leaves_a_naked_perp_for_the_hold(w, kind):
    """The perp (first leg) create fails once (it never reaches the matching engine) and is resent by reconcile; the spot leg was never sent. The group must be UNHEDGED -> unwound, not 'HEDGED' for the whole hold."""
    d = _ready(w, hold="600")
    link = w.plan_doc()["group"]["group_id"] + "-p"
    w.fake.inject(CREATE, kind, times=1, match=lambda b: b["orderLinkId"] == link)
    naked_samples, real_sleep = [], w.clock.sleep

    def sleep(s):
        real_sleep(s)
        if w.fake.positions.get("BTCUSDT", {"size": D(0)})["size"] != 0 and w.fake.wallet["BTC"] == 0:
            naked_samples.append(w.clock.now())
    w.clock.sleep = sleep
    assert w.run("execute", "--confirm-plan-digest", d, typed=d) == 0, w.text()[-400:]
    assert w.record()["flat_verified"] is True
    assert len(naked_samples) <= 10, f"a perp short was held with NO spot hedge for {len(naked_samples)} polls (the hold is 600 s)"
    states = [e["body"]["to"] for e in _journal(w) if e["type"] == "STATE"]
    assert "UNHEDGED" in states, states


# ---------------------------------------------------------------- the journal store itself: only an unparseable LAST line is repaired, nothing else is
def _journal_file(tmp_path, n=3):
    import ms_execution as X
    st = X.FileStore(str(tmp_path / "engine_journal.jsonl"))
    j = X.Journal(st)
    for i in range(n):
        j.append("STATE", "g1", 1000 + i, {"to": f"S{i}"})
    return X, st


def test_a_torn_last_line_is_set_aside_kept_for_the_owner_and_the_journal_continues_cleanly(tmp_path, capsys):
    X, st = _journal_file(tmp_path)
    lines = open(st.path).read().splitlines()
    with open(st.path, "w") as fh:
        fh.write("\n".join(lines[:2]) + "\n" + lines[2][:25])
    j = X.Journal(X.FileStore(st.path))
    assert [e["seq"] for e in j.entries[:2]] == [0, 1]
    assert open(st.path + ".torn").read() == lines[2][:25] + "\n"                  # evidence kept, never silently discarded
    # the repair is not silent either (second review: the only trace used to be the .torn file): it is journalled as an ANOMALY and printed
    assert [e["type"] for e in j.entries] == ["STATE", "STATE", "ANOMALY"] and j.entries[2]["seq"] == 2
    a = j.entries[2]["body"]
    assert a["code"] == "TORN_TAIL_DROPPED" and a["dropped_chars"] == 25 and a["kept_in"] == "engine_journal.jsonl.torn"
    import hashlib
    assert a["dropped_sha16"] == hashlib.sha256(lines[2][:25].encode()).hexdigest()[:16] and j.entries[2]["ts_ms"] == j.entries[1]["ts_ms"]
    assert j.repaired_torn_tail == a
    err = capsys.readouterr().err
    assert "JOURNAL_TORN_TAIL_DROPPED" in err and "25 characters" in err and "engine_journal.jsonl.torn" in err
    j.append("STATE", "g1", 2000, {"to": "AFTER_REPAIR"})
    again = X.Journal(X.FileStore(st.path))
    assert [e["seq"] for e in again.entries] == [0, 1, 2, 3] and X.verify_chain(open(st.path).read().splitlines())[0] and again.repaired_torn_tail is None
    assert "JOURNAL_TORN_TAIL_DROPPED" not in capsys.readouterr().err, "a clean journal prints nothing"


def test_the_repair_refuses_to_drop_a_line_it_did_not_judge_and_changes_nothing(tmp_path):
    """A lock-broken emergency unwind has no single-writer lock: between judging the tail torn and dropping it another writer may have appended a good line. Re-reading and dropping 'whatever is
    last' would discard that line; the repair now drops only the line it judged, otherwise it refuses."""
    X, st = _journal_file(tmp_path)
    lines = open(st.path).read().splitlines()
    with open(st.path, "w") as fh:
        fh.write("\n".join(lines[:2]) + "\n" + lines[2][:25])
    judged = X.FileStore(st.path).read()[-1]

    class Racy(X.FileStore):
        def drop_torn_tail(self, expected=None):
            with open(self.path, "a") as fh:                                          # another writer completes / extends the file between judging and dropping
                fh.write("\n" + lines[2] + "\n")
            return super().drop_torn_tail(expected)
    racy = Racy(st.path)
    with pytest.raises(ValueError, match="journal changed while its torn tail was being set aside"):
        X.Journal(racy)
    after = open(st.path).read()
    assert lines[2] in after and not os.path.exists(st.path + ".torn"), "the good line another writer appended must survive and nothing may be set aside"
    assert judged == lines[2][:25]
    # called directly: a stale expectation or an empty journal is refused, an exact one is honoured
    solo = X.FileStore(str(tmp_path / "solo.jsonl"))
    with pytest.raises(ValueError):
        solo.drop_torn_tail(expected="x")
    with open(solo.path, "w") as fh:
        fh.write("a\nb\n")
    with pytest.raises(ValueError):
        solo.drop_torn_tail(expected="not-the-last")
    assert open(solo.path).read() == "a\nb\n" and not os.path.exists(solo.path + ".torn")
    assert solo.drop_torn_tail(expected="b") == "b" and open(solo.path).read() == "a\n" and open(solo.path + ".torn").read() == "b\n"


def test_the_torn_text_is_kept_before_the_journal_is_cut_so_a_crash_between_loses_nothing(tmp_path, monkeypatch):
    X, st = _journal_file(tmp_path)
    lines = open(st.path).read().splitlines()
    with open(st.path, "w") as fh:
        fh.write("\n".join(lines[:2]) + "\n" + lines[2][:25])
    torn_state = open(st.path).read()
    seen = {}

    def crash_at_the_cut(path, size):
        seen["torn_file_at_the_cut"] = open(st.path + ".torn").read() if os.path.exists(st.path + ".torn") else None
        raise SimulatedCrash("killed between keeping the torn text and cutting the journal")
    monkeypatch.setattr(X, "_truncate_file", crash_at_the_cut)
    with pytest.raises(SimulatedCrash):
        X.Journal(X.FileStore(st.path))
    monkeypatch.undo()
    assert seen["torn_file_at_the_cut"] == lines[2][:25] + "\n", "the evidence must be durable BEFORE the journal is cut"
    assert open(st.path).read() == torn_state, "a crash at the cut leaves the ORIGINAL journal, never a mixture"
    assert not os.path.exists(st.path + ".repair"), "no second copy of the journal is ever written"
    again = X.Journal(X.FileStore(st.path))                                              # and the next open repairs it
    assert [e["type"] for e in again.entries] == ["STATE", "STATE", "ANOMALY"] and X.verify_chain(open(st.path).read().splitlines())[0]


def test_a_repair_needs_no_free_space_beyond_the_kept_text_and_that_is_best_effort(tmp_path, capsys):
    """Independent re-review, finding 6: the repair wrote the kept text AND a full copy of the journal, so on the full disk that tore the append the journal could not even be opened and the
    owner's unwind was blocked. It now cuts in place; keeping the text is best effort and falls back to stderr."""
    X, st = _journal_file(tmp_path)
    lines = open(st.path).read().splitlines()
    with open(st.path, "w") as fh:
        fh.write("\n".join(lines[:2]) + "\n" + lines[2][:25])
    os.makedirs(st.path + ".torn")                                                           # the evidence file cannot be opened for append (stands in for ENOSPC on it)
    j = X.Journal(X.FileStore(st.path))
    err = capsys.readouterr().err
    assert [e["seq"] for e in j.entries[:2]] == [0, 1] and "JOURNAL_TORN_TEXT_NOT_KEPT" in err and lines[2][:25] in err, "the only copy of the text must be on stderr"
    assert not os.path.exists(st.path + ".repair") and X.verify_chain(open(st.path).read().splitlines())[0]
    assert open(st.path).read().startswith("\n".join(lines[:2]) + "\n"), "the two good lines are untouched"


def test_a_journal_that_is_only_a_torn_line_opens_empty_and_journals_no_zero_time_anomaly(tmp_path, capsys):
    """Re-review T1: a torn FIRST line made the repair's ANOMALY entry seq 0 with ts_ms 0, which the lifecycle read as a run that started in 1970 (a spurious LIFECYCLE_EXCEEDED)."""
    X, st = _journal_file(tmp_path, n=1)
    first = open(st.path).read().splitlines()[0]
    with open(st.path, "w") as fh:
        fh.write(first[:20])
    j = X.Journal(X.FileStore(st.path))
    err = capsys.readouterr().err
    assert j.entries == [] and j.repaired_torn_tail["code"] == "TORN_TAIL_DROPPED" and "JOURNAL_TORN_TAIL_DROPPED" in err and "held nothing before the torn line" in err
    assert open(st.path).read() == "" and open(st.path + ".torn").read() == first[:20] + "\n"
    j.append("STATE", "g1", 5000, {"to": "FIRST_REAL_ENTRY"})
    assert [e["seq"] for e in X.Journal(X.FileStore(st.path)).entries] == [0] and X.verify_chain(open(st.path).read().splitlines())[0]


def test_a_full_disk_that_cannot_journal_the_anomaly_still_lets_the_owner_open_the_journal(tmp_path, monkeypatch, capsys):
    """The torn append is often a full disk: journalling the repair must be best effort, or the emergency unwind could not open the journal at all."""
    X, st = _journal_file(tmp_path)
    lines = open(st.path).read().splitlines()
    with open(st.path, "w") as fh:
        fh.write("\n".join(lines[:2]) + "\n" + lines[2][:25])
    real_append = X.FileStore.append

    def full(self, line):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(X.FileStore, "append", full)
    j = X.Journal(X.FileStore(st.path))
    monkeypatch.setattr(X.FileStore, "append", real_append)
    assert [e["seq"] for e in j.entries] == [0, 1] and j.repaired_torn_tail["code"] == "TORN_TAIL_DROPPED"
    err = capsys.readouterr().err
    assert "JOURNAL_TORN_TAIL_DROPPED" in err and "JOURNAL_TORN_TAIL_ANOMALY_NOT_JOURNALLED" in err and open(st.path + ".torn").read() == lines[2][:25] + "\n"
    assert X.verify_chain(open(st.path).read().splitlines())[0]


@pytest.mark.parametrize("damage,why", [
    ("tamper_last", "MUTATED_AT_2"),                 # a COMPLETE last line with a changed body: tampering, not a torn append
    ("corrupt_middle", "UNPARSEABLE_AT_1"),          # a damaged EARLIER line
    ("torn_over_broken_chain", "UNPARSEABLE_AT_2"),  # a torn last line is only repaired when everything before it verifies
    ("sequence_gap", "SEQ_GAP_AT_"),
])
def test_anything_other_than_a_torn_last_line_stays_refused(tmp_path, damage, why):
    X, st = _journal_file(tmp_path)
    lines = open(st.path).read().splitlines()
    if damage == "tamper_last":
        lines[2] = lines[2].replace("S2", "S9")
    elif damage == "corrupt_middle":
        lines[1] = lines[1][:20]
    elif damage == "torn_over_broken_chain":
        lines[0] = lines[0].replace("S0", "S7")
        lines[2] = lines[2][:25]
    elif damage == "sequence_gap":
        del lines[1]
    with open(st.path, "w") as fh:
        fh.write("\n".join(lines) + "\n")
    before = open(st.path).read()
    with pytest.raises(ValueError, match="journal refused"):
        X.Journal(X.FileStore(st.path))
    assert open(st.path).read() == before and not os.path.exists(st.path + ".torn"), "a refused journal must be left exactly as found"
    if damage != "torn_over_broken_chain":
        with pytest.raises(ValueError, match=why):
            X.Journal(X.FileStore(st.path))


def test_a_failed_append_leaves_no_partial_line_behind(tmp_path, monkeypatch):
    X, st = _journal_file(tmp_path, n=2)
    before = open(st.path).read()
    j = X.Journal(X.FileStore(st.path))
    real_fsync = os.fsync

    def boom(fd):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(X.os, "fsync", boom)
    with pytest.raises(OSError):
        j.append("STATE", "g1", 5000, {"to": "NEVER"})
    monkeypatch.setattr(X.os, "fsync", real_fsync)
    assert open(st.path).read() == before                                      # truncated back: a later append cannot glue onto a half line


def test_a_complete_last_line_that_lost_only_its_newline_is_not_glued_onto(tmp_path):
    X, st = _journal_file(tmp_path, n=2)
    text = open(st.path).read()
    with open(st.path, "w") as fh:
        fh.write(text.rstrip("\n"))                                            # valid, verified, but unterminated
    j = X.Journal(X.FileStore(st.path))
    assert len(j.entries) == 2
    j.append("STATE", "g1", 3000, {"to": "NEXT"})
    lines = open(st.path).read().splitlines()
    assert len(lines) == 3 and X.verify_chain(lines)[0] and not os.path.exists(st.path + ".torn")


def test_the_in_memory_store_is_unaffected_and_has_no_repair(tmp_path):
    import ms_execution as X
    m = X.MemoryStore(["not json"])
    with pytest.raises(ValueError, match="journal refused"):
        X.Journal(m)


# ---------------------------------------------------------------- the fee-in-base remainder rule is bounded
def test_the_fee_in_base_remainder_bound_is_twice_the_highest_published_spot_fee():
    import ms_execution as X
    assert X.FEE_IN_BASE_MAX_FRACTION == D("0.002")
    # the highest spot taker fee on the published schedule is 0.1% (VIP 0): the bound leaves a 2x margin and nothing close to a real partial fill
    assert X.FEE_IN_BASE_MAX_FRACTION < D("0.01")


# ---------------------------------------------------------------- an execution list that never returns is an UNKNOWN session net, not a measured zero
def _quality(w):
    return json.load(open(os.path.join(w.state_dir, "execution_quality.json")))


def test_executions_the_venue_never_listed_make_the_session_net_and_cost_unknown_not_zero(w):
    """All four orders are Filled at the venue and the account is flat, but /v5/execution/list returned nothing: the record said fees 0, price leg 0, total cost 0 with nothing unknown (specialist E,
    G4). The capital book's `known` is None whenever fees or the price leg are None, so such a session can still debit a loss but never credit a gain."""
    d = _ready(w, hold="60")
    for i in range(1, 9):
        w.fake.hidden_execs.add("E%06d" % i)
    assert w.run("execute", "--confirm-plan-digest", d, typed=d) == 0, w.text()[-300:]
    rec = w.record()
    assert rec["flat_verified"] is True                                                         # flat is the VENUE's own read
    assert rec["net"]["fees_usd"] is None and rec["net"]["price_pnl_usd"] is None, rec["net"]   # unknown, never a measured zero
    import ms_testnet_session as S
    assert S.record_refusals(rec) == []                                                         # the closed record schema still holds (None is a valid component)
    g = _quality(w)["group"]
    assert g["cost_complete"] is False and g["total_execution_cost_usd"] is None and "fills_incomplete" in g["unknown"]
    assert g["attempt_completed"] is True                                                       # the pair did finish flat; its COST is what is unknown


def test_a_session_whose_executions_all_arrive_keeps_a_measured_net(w):
    d = _ready(w, hold="60")
    assert w.run("execute", "--confirm-plan-digest", d, typed=d) == 0, w.text()[-300:]
    rec = w.record()
    assert rec["flat_verified"] is True and rec["net"]["fees_usd"] is not None and rec["net"]["price_pnl_usd"] is not None
    g = _quality(w)["group"]
    assert g["cost_complete"] is True and "fills_incomplete" not in g["unknown"] and g["total_execution_cost_usd"] is not None


def test_the_fee_in_base_remainder_rule_belongs_to_a_spot_buy_alone():
    """Second independent review: the rule that drops a fee-sized remainder when a real execution replaces an adopted (recon) fill was not limited to spot BUY legs. A PERP whose execution list lags
    its order row by 0.1% would have lost the remainder from the engine's exposure and then halted on a position mismatch with the venue (which holds the full quantity)."""
    import ms_execution as EX
    import ms_types as T
    from test_ms_execution import GID, PERP, SPOT, H, PL, SL
    h = H()
    h.start()
    h.e.on_fill(h.v.execute(PL, 10, 100.0, 3000, fill_id="recon:perp"), 3000)             # adopted from the order rows' cumulative quantity (the executions have not been listed yet)
    h.e.on_fill(h.v.execute(SL, 10, 100.0, 3000, fill_id="recon:spot"), 3000)
    h.e.on_fill(T.Fill("E-perp", GID, PERP, "XUSDT", "Sell", 9.98, 100.0, 0.0, "USDT", "TAKER", 3100, "MEASURED"), 3100)     # the list arrives, 0.2% short
    h.e.on_fill(T.Fill("E-spot", GID, SPOT, "XUSDT", "Buy", 9.98, 100.0, 0.0, "USDT", "TAKER", 3100, "MEASURED"), 3100)
    assert h.e.positions_exp["linear:XUSDT"] == D("-10"), "a perp keeps the venue's quantity: the missing 0.02 is a lagging execution, not a fee"
    assert h.e.positions_exp["spot:XUSDT"] == D("9.98"), "a spot BUY's fee is taken from the coin received: the 0.02 was never held"
    assert EX.FEE_IN_BASE_MAX_FRACTION * 10 == D("0.020")


# ---------------------------------------------------------------- a spot exit HELD behind its perp exit is not a venue rejection (R3)
def _engine_with_a_spot_exit_that_answers(reason):
    """A HEDGED group whose perp exit stays live and whose spot exit is answered REJECTED with ``reason`` (``mode['reason']`` can be switched while the test runs)."""
    import ms_execution as X
    from test_ms_execution import H, hedged
    h = hedged(H())
    real = h.v.send_order
    mode = {"reason": reason}

    def send(req):
        if req.leg.category == "spot" and "-x" in req.link_id:
            return X.Ack(req.link_id, "REJECTED", None, mode["reason"])
        return real(req)
    h.e._send = send
    return X, h, mode


def test_a_held_spot_exit_never_counts_toward_the_exit_halt_but_a_venue_rejection_does():
    """Review R3: the session holds a spot sale while the perp is still short (selling the spot first would leave a naked short), answering the engine REJECTED. The engine counted every such answer
    as a venue rejection, so a perp exit that needed several steps (a partial fill) halted the group (EXIT_ORDERS_REJECTED) with the hedge still open. A HELD answer is not a rejection."""
    from test_ms_execution import GID
    X, h, mode = _engine_with_a_spot_exit_that_answers(None)
    mode["reason"] = X.HELD_REASON
    h.e.request_exit(GID, 5_000)
    for i in range(1, 16):
        h.e.step(5_000 + i * 1_000)
    g = h.e.groups[GID]
    held = [l for l in g.exit_legs() if l.status == X.REJECTED and l.reject_reason == X.HELD_REASON]
    assert len(held) > h.policy.max_exit_attempts, f"the scenario must hold MORE answers than the halt allows, got {len(held)}"
    assert not g.halted_reason, f"held answers halted the group: {g.halted_reason}"
    # control: the SAME number of answers that are real venue rejections DO halt it
    X2, h2, mode2 = _engine_with_a_spot_exit_that_answers("REDUCE_ONLY_NO_POSITION_TO_REDUCE")
    h2.e.request_exit(GID, 5_000)
    for i in range(1, 16):
        h2.e.step(5_000 + i * 1_000)
    assert h2.e.groups[GID].halted_reason == "EXIT_ORDERS_REJECTED"


def test_the_exit_halt_floor_the_owner_lifts_to_counts_only_real_rejections_and_a_held_answer_is_not_resent_in_its_own_step():
    from test_ms_execution import GID
    X, h, mode = _engine_with_a_spot_exit_that_answers(None)
    mode["reason"] = X.HELD_REASON
    h.e.request_exit(GID, 5_000)
    spot_exits = lambda: [l for l in h.e.groups[GID].exit_legs() if l.plan.category == "spot"]   # noqa: E731
    assert len(spot_exits()) == 1 and spot_exits()[0].reject_reason == X.HELD_REASON, "the exit request sends the spot exit ONCE: a held answer is not re-sent inside the step that was held"
    for i in range(1, 5):
        h.e.step(5_000 + i * 1_000)                                                   # four more steps: one held answer per step
    assert len(spot_exits()) == 5 and all(l.reject_reason == X.HELD_REASON for l in spot_exits())
    assert len({l.sent_ms for l in spot_exits()}) == 5, "two held exits were sent in the same step"
    mode["reason"] = "REDUCE_ONLY_NO_POSITION_TO_REDUCE"
    for i in range(5, 14):
        h.e.step(5_000 + i * 1_000)
    g = h.e.groups[GID]
    real = [l for l in g.exit_legs() if l.status == X.REJECTED and l.reject_reason != X.HELD_REASON]
    assert g.halted_reason == "EXIT_ORDERS_REJECTED" and len(real) == h.policy.max_exit_attempts, (g.halted_reason, len(real))
    every_rejected_exit = sum(1 for l in g.exit_legs() if l.status == X.REJECTED)
    assert every_rejected_exit == len(real) + 5, "five held answers and the real rejections are the only rejected exit legs"
    assert h.e.clear_exit_halt(GID, 30_000) == "EXIT_ORDERS_REJECTED"
    lifts = [r["body"] for r in h.types("HALT_GROUP") if r["body"].get("reason") == ""]
    assert len(lifts) == 1, "the lift is one journalled HALT_GROUP with an empty reason (the engine may re-halt after it; that is a later, separate record)"
    floor = lifts[0]["exit_floor"]
    assert floor == len(real) < every_rejected_exit, f"the floor must exclude the held answers: floor={floor}, rejected exit legs={every_rejected_exit}"
    # the reason is a pure fold of the journal: a restart rebuilds it
    again = h.make()
    assert [l.reject_reason for l in again.groups[GID].exit_legs()] == [l.reject_reason for l in g.exit_legs()]
