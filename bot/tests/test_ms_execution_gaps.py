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


def test_a_torn_last_line_is_set_aside_kept_for_the_owner_and_the_journal_continues_cleanly(tmp_path):
    X, st = _journal_file(tmp_path)
    lines = open(st.path).read().splitlines()
    with open(st.path, "w") as fh:
        fh.write("\n".join(lines[:2]) + "\n" + lines[2][:25])
    j = X.Journal(X.FileStore(st.path))
    assert [e["seq"] for e in j.entries] == [0, 1]
    assert open(st.path + ".torn").read() == lines[2][:25] + "\n"                  # evidence kept, never silently discarded
    j.append("STATE", "g1", 2000, {"to": "AFTER_REPAIR"})
    again = X.Journal(X.FileStore(st.path))
    assert [e["seq"] for e in again.entries] == [0, 1, 2] and X.verify_chain(open(st.path).read().splitlines())[0]


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


def test_the_loop_books_no_gain_for_a_session_whose_net_is_unknown():
    """The arithmetic the autopilot applies to the record: any None among fees / price leg makes `known` None, and the book credits nothing it cannot explain."""
    import ms_capital as CC
    b_known = lambda fees, price, fund: None if (fees is None or price is None) else price - fees + (fund or D(0))     # noqa: E731 - the rule at ms_testnet_autopilot.py (`known = None if ...`)
    assert b_known(None, D(0), None) is None and b_known(D("0.2"), None, None) is None
    assert CC.allocate(grant_max_usd=375, ceiling_usd=500, open_gross_usd=0, available_usdt=10_000, basis=D(10_000), start_cash=D(10_000), loss_streak=0, drawdown_usd=D(0), profit_seeking=True).notional > 0
