"""Round 22: the Testnet MECHANICS verifier turns a session directory into a closed verdict, and no claim is stronger than its evidence.

A real session is run on the in-process fake venue (no network, no secret), then the verifier reads its directory. Every tamper below changes ONE fact a real defect or a dishonest record would change,
and the verdict must move: an unproven end state is INCONCLUSIVE, a contradiction is MECHANICS_FAILED, a verified session still says it is fake money."""
from __future__ import annotations

import copy
import json
import os
import shutil
import sys
from decimal import Decimal as D

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (HERE, os.path.dirname(HERE), os.path.join(os.path.dirname(HERE), "tools")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import mechanics_verify as MV  # noqa: E402
from ms_testnet_world import World  # noqa: E402


@pytest.fixture(scope="module")
def session(tmp_path_factory):
    """ONE real session (perp short + spot buy, a 150 s hold across a simulated funding settlement, both legs closed) on the fake venue, and the independent end-of-session venue snapshot."""
    mp = pytest.MonkeyPatch()
    try:
        tmp = tmp_path_factory.mktemp("mech")
        w = World(tmp, mp)
        state = {"done": False, "t0": None}
        orig = w.clock.sleep

        def sleep(s):
            orig(s)
            size = w.fake.positions.get("BTCUSDT", {"size": D(0)})["size"]
            if size != 0:
                if state["t0"] is None:
                    state["t0"] = w.clock.now()
                if not state["done"] and w.clock.now() - state["t0"] >= 75_000:
                    state["done"] = True
                    w.fake.settle_funding("BTCUSDT", "0.0001", ts_ms=w.clock.now())
        w.clock.sleep = sleep
        digest = w.ready("BTCUSDT", 100.0, hold_seconds=150)
        assert w.execute(digest) == 0, w.text()
        rec = w.record()
        assert rec["flat_verified"] is True and rec["orders"]
        snap = {"schema": MV.SNAPSHOT_SCHEMA, "read_ms": w.clock.now() + 1000, "complete": True,
                "linear_positions": {s: float(p["size"]) for s, p in w.fake.positions.items()}, "spot_balances": {k: str(v) for k, v in w.fake.wallet.items()},
                "open_orders_active": [], "open_orders_conditional": [], "usdt_balance": str(w.fake.wallet["USDT"])}
        yield {"dir": w.state_dir, "snap": snap, "tmp": tmp}
    finally:
        mp.undo()


def _copy(session, tmp_path, name="s"):
    dst = tmp_path / name
    shutil.copytree(session["dir"], dst)
    return str(dst)


def _edit(path, fn):
    with open(path, encoding="utf-8") as fh:
        d = json.load(fh)
    fn(d)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(d, fh, sort_keys=True)


def _non_pass(res):
    return {c["id"]: c["verdict"] for c in res["checks"] if c["verdict"] != "PASS"}


def test_a_real_session_with_an_independent_snapshot_is_mechanics_verified_and_says_it_is_fake_money(session):
    res = MV.verify(session["dir"], session["snap"], require_funding=True)
    assert res["overall"] == MV.VERIFIED, _non_pass(res)
    assert "fake money" in res["claim"] and "not owner profit" in res["claim"]
    ids = {c["id"] for c in res["checks"]}
    assert {"C00_MECHANICS_EXERCISED", "C07_NO_DUPLICATE_ORDERS", "C09_FILL_QTY", "C10_HEDGE", "C11_FEES", "C12_NET_IDENTITY", "C14_CLOSE", "C15_FINAL_VENUE_STATE", "C16_LOCAL_STATE"} <= ids
    assert res["group_id"] and res["plan_digest"]


def test_without_an_independent_snapshot_the_end_state_is_unproven_so_the_verdict_is_inconclusive_not_verified(session):
    res = MV.verify(session["dir"], None, require_funding=True)
    assert res["overall"] == MV.INCONCLUSIVE and _non_pass(res).get("C15_FINAL_VENUE_STATE") == "UNKNOWN"


@pytest.mark.parametrize("name,change,expect_check", [
    ("open_order", lambda s: s.update(open_orders_active=[{"orderId": "X1"}]), "C15_FINAL_VENUE_STATE"),
    ("conditional_order", lambda s: s.update(open_orders_conditional=[{"orderId": "SL1"}]), "C15_FINAL_VENUE_STATE"),
    ("perp_open_other_symbol", lambda s: s["linear_positions"].update({"ETHUSDT": -0.2}), "C15_FINAL_VENUE_STATE"),
    ("perp_open", lambda s: s["linear_positions"].update({"BTCUSDT": -0.001}), "C15_FINAL_VENUE_STATE"),
    ("snapshot_before_last_fill", lambda s: s.update(read_ms=1), "C15_FINAL_VENUE_STATE"),
    ("cash_delta_wrong", lambda s: s.update(usdt_balance=str(D(s["usdt_balance"]) + D("0.9"))), "C12b_CASH_IDENTITY"),
])
def test_a_venue_snapshot_that_contradicts_the_session_fails_it(session, name, change, expect_check):
    snap = copy.deepcopy(session["snap"])
    change(snap)
    res = MV.verify(session["dir"], snap, require_funding=True)
    assert res["overall"] == MV.FAILED and _non_pass(res).get(expect_check) == "FAIL", (name, _non_pass(res))


@pytest.mark.parametrize("name,change", [
    ("incomplete", lambda s: s.update(complete=False)),
    ("no_conditional_orders_read", lambda s: s.pop("open_orders_conditional")),
    ("wrong_schema", lambda s: s.update(schema="other")),
    ("read_ms_not_int", lambda s: s.update(read_ms="123")),
    ("orders_not_a_list", lambda s: s.update(open_orders_active={})),
])
def test_an_unusable_snapshot_is_unknown_never_a_pass(session, name, change):
    snap = copy.deepcopy(session["snap"])
    change(snap)
    res = MV.verify(session["dir"], snap, require_funding=True)
    assert res["overall"] == MV.INCONCLUSIVE and _non_pass(res).get("C15_FINAL_VENUE_STATE") == "UNKNOWN", (name, _non_pass(res))


def test_a_mutated_journal_line_breaks_the_chain_and_fails_integrity(session, tmp_path):
    d = _copy(session, tmp_path)
    p = os.path.join(d, "engine_journal.jsonl")
    lines = open(p, encoding="utf-8").read().splitlines()
    i = next(k for k, ln in enumerate(lines) if '"FILL"' in ln)
    lines[i] = lines[i].replace("0.001", "0.002", 1)
    assert lines[i] != open(p, encoding="utf-8").read().splitlines()[i]
    open(p, "w", encoding="utf-8").write("\n".join(lines) + "\n")
    res = MV.verify(d, session["snap"], require_funding=True)
    assert res["overall"] == MV.FAILED and _non_pass(res).get("C01_INTEGRITY") == "FAIL"


@pytest.mark.parametrize("name,fn,check", [
    ("phantom_duplicate_order", lambda r: r["orders"].append(dict(r["orders"][0], order_id="O9999")), "C07_NO_DUPLICATE_ORDERS"),
    ("fee_total_altered", lambda r: r["net"].__setitem__("fees_usd", r["net"]["fees_usd"] + 0.01), "C11_FEES"),
    ("final_position_not_flat", lambda r: r["final_position"].__setitem__("BTCUSDT", -0.001), "C15_FINAL_VENUE_STATE"),
    ("wrong_side_fill", lambda r: r["executions"][0].__setitem__("side", "Buy" if r["executions"][0]["side"] == "Sell" else "Sell"), "C08_EXEC_SIDE_SYMBOL"),
    ("wrong_symbol_fill", lambda r: r["executions"][1].__setitem__("symbol", "ETHUSDT"), "C08_EXEC_SIDE_SYMBOL"),
])
def test_each_tampered_session_record_is_caught_by_the_check_that_owns_the_fact(session, tmp_path, name, fn, check):
    d = _copy(session, tmp_path)
    _edit(os.path.join(d, "testnet_session.json"), fn)
    res = MV.verify(d, session["snap"], require_funding=True)
    assert res["overall"] in (MV.FAILED, MV.INCONCLUSIVE) and res["overall"] != MV.VERIFIED, (name, _non_pass(res))
    assert check in _non_pass(res), (name, _non_pass(res))


def test_a_quarantined_funding_sign_is_unknown_when_funding_was_required_and_optional_when_it_was_not(session, tmp_path):
    d = _copy(session, tmp_path)
    _edit(os.path.join(d, "funding_settlement.json"), lambda f: f["recognitions"][0]["prints"][0].__setitem__("status", "QUARANTINED_SIGN"))
    res = MV.verify(d, session["snap"], require_funding=True)
    assert res["overall"] == MV.INCONCLUSIVE and _non_pass(res).get("C13_FUNDING") == "UNKNOWN"
    res2 = MV.verify(d, session["snap"], require_funding=False)
    assert _non_pass(res2).get("C13_FUNDING") == "UNKNOWN" and res2["overall"] == MV.VERIFIED, "a session not meant to span a settlement does not need the observation, but the row stays UNKNOWN"


def test_a_missing_or_malformed_file_is_inconclusive_not_a_crash_and_never_verified(session, tmp_path):
    d = _copy(session, tmp_path)
    os.remove(os.path.join(d, "execution_quality.json"))
    res = MV.verify(d, session["snap"])
    assert res["overall"] == MV.INCONCLUSIVE and _non_pass(res) == {"C01_FILES": "UNKNOWN"}
    d2 = _copy(session, tmp_path, "s2")
    _edit(os.path.join(d2, "testnet_session.json"), lambda r: r.pop("executions"))
    res2 = MV.verify(d2, session["snap"])
    assert res2["overall"] != MV.VERIFIED
    d3 = _copy(session, tmp_path, "s3")
    open(os.path.join(d3, "plan.json"), "w").write("not json")
    assert MV.verify(d3, session["snap"])["overall"] == MV.INCONCLUSIVE


def test_a_session_that_sent_orders_but_filled_nothing_verifies_nothing(session, tmp_path):
    d = _copy(session, tmp_path)
    _edit(os.path.join(d, "testnet_session.json"), lambda r: r.update(executions=[]))
    res = MV.verify(d, session["snap"], require_funding=False)
    assert res["overall"] != MV.VERIFIED and _non_pass(res).get("C00_MECHANICS_EXERCISED") == "UNKNOWN"


def test_repeated_needs_two_distinct_verified_sessions_not_a_copied_directory(session):
    one = MV.verify(session["dir"], session["snap"], require_funding=True)
    assert MV.verify_repeated([one])["level"] == "MECHANICS_VERIFIED"
    assert MV.verify_repeated([one, one])["level"] == "MECHANICS_VERIFIED", "the same session twice is one session"
    other = dict(one, group_id="another-group", plan_digest="f" * 64, decision_id="d2")
    assert MV.verify_repeated([one, other])["level"] == "REPEATED_MECHANICS_VERIFIED"
    assert MV.verify_repeated([one, dict(other, overall=MV.FAILED)])["level"] == "MECHANICS_VERIFIED"
    assert MV.verify_repeated([])["level"] == "NOT_VERIFIED"


def test_the_command_line_exit_codes_and_that_it_reads_but_never_writes(session, tmp_path, capsys):
    snap_ok = tmp_path / "snap.json"
    snap_ok.write_text(json.dumps(session["snap"]))
    before = sorted((os.path.relpath(os.path.join(r, f), session["dir"]), os.path.getmtime(os.path.join(r, f))) for r, _d, fs in os.walk(session["dir"]) for f in fs)
    assert MV.main([session["dir"], "--venue-snapshot", str(snap_ok), "--require-funding"]) == 0
    assert "OVERALL: MECHANICS_VERIFIED" in capsys.readouterr().out
    assert MV.main([session["dir"], "--require-funding"]) == 3
    bad = dict(session["snap"], open_orders_active=[{"orderId": "X"}])
    snap_bad = tmp_path / "bad.json"
    snap_bad.write_text(json.dumps(bad))
    capsys.readouterr()
    assert MV.main([session["dir"], "--venue-snapshot", str(snap_bad), "--require-funding", "--json"]) == 1
    out = json.loads(capsys.readouterr().out)
    assert out["schema"] == MV.SCHEMA and out["overall"] == MV.FAILED
    assert MV.main([str(tmp_path / "nope")]) == 2
    after = sorted((os.path.relpath(os.path.join(r, f), session["dir"]), os.path.getmtime(os.path.join(r, f))) for r, _d, fs in os.walk(session["dir"]) for f in fs)
    assert before == after, "the verifier must not modify the session directory"


def test_the_verifier_has_no_network_clock_or_order_path():
    src = open(os.path.join(os.path.dirname(HERE), "tools", "mechanics_verify.py"), encoding="utf-8").read()
    for banned in ("requests", "urllib", "socket", "http.client", "time.time", "datetime", "place_order", "create_order", "os.environ", "getenv", "open(path, \"w\"", "write_json", "BYBIT_TESTNET_API", "SECRET"):
        assert banned not in src, banned
