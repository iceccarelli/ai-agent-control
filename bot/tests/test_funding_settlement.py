"""Funding settlement recognition. The sign of ``execFee`` on a Funding row is UNVERIFIED (no real row has ever been captured), so these tests pin the design that makes a wrong sign harmless:
a row is booked only when it agrees with the physical expectation (short + positive rate = received), a disagreeing row is quarantined and funding stays UNKNOWN (None), and nothing is ever flipped
or defaulted to zero. Fixtures are repo-authored (FIXTURE_ONLY); a reserved slot for one owner-captured real row is documented in RUNBOOKS."""
import json
import os
import sys
from decimal import Decimal as D

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
BOT = os.path.dirname(HERE)
sys.path[:0] = [HERE, BOT, os.path.join(BOT, "tools")]

import exec_evidence as XE  # noqa: E402
import exec_lifecycle as EL  # noqa: E402
import funding_settlement as FS  # noqa: E402
import runtime_capability as RC  # noqa: E402
from ms_testnet_world import World, begin  # noqa: E402

SYM = "BTCUSDT"
T_IN, PRINT, T_OUT = 1_000_000, 1_500_000, 2_000_000
NOW = 2_100_000


def fills(entry=T_IN, exit_=T_OUT, qty="0.5", px="60000"):
    f = [{"ts_ms": entry, "side": "Sell", "qty": qty, "price": px}]
    if exit_ is not None:
        f.append({"ts_ms": exit_, "side": "Buy", "qty": qty, "price": px})
    return f


def row(fee, ts=PRINT, eid="F1", ccy="USDT", symbol=SYM, mark="60000", **kw):
    r = {"exec_id": eid, "type": "Funding", "symbol": symbol, "fee": fee, "exec_time_ms": ts, "side": "Sell", "qty": "0.5", "price": mark, "mark": mark, "fee_ccy": ccy}
    r.update(kw)
    return r


def prints(*pp):
    return [{"ts_ms": t, "rate": r} for t, r in pp]


def rec(rows, pr=None, fl=None, wallet=(), other=(), now=NOW):
    return FS.recognise(symbol=SYM, perp_fills=fl if fl is not None else fills(), other_fill_ts=other, prints=prints((PRINT, "0.0001")) if pr is None else pr, rows=rows, wallet_samples=wallet, now_ms=now)


def only(r):
    assert len(r["prints"]) == 1, r["prints"]
    return r["prints"][0]


# ------------------------------------------------------------------ sign convention: booked only when physics agrees
def test_a_short_receiving_positive_funding_is_booked_under_the_h1_convention():
    r = rec([row("-3.0")])                                         # short 0.5 BTC x 60000 x 1 bp = +$3.00 received; H1: cash = -execFee
    e = only(r)
    assert e["status"] == "BOOKED" and D(e["amount_usd"]) == D("3") and D(e["expected_usd"]) == D("3")
    assert r["actions"] == [{"action": "record", "ts_ms": PRINT, "amount_usd": "3.00000000", "prov": "MEASURED"}] and D(r["booked_usd"]) == D("3") and r["all_prints_resolved"]


def test_a_row_with_the_opposite_convention_is_quarantined_never_flipped_and_funding_stays_unknown():
    r = rec([row("3.0")])                                          # H2 would read this as +3, H1 reads -3 = a PAYMENT: the physical expectation says RECEIVED -> disagree
    e = only(r)
    assert e["status"] == "QUARANTINED_SIGN" and "UNVERIFIED" in e["reason"]
    assert r["actions"] == [{"action": "record", "ts_ms": PRINT, "amount_usd": None, "prov": "UNKNOWN"}] and r["booked_usd"] is None and r["all_prints_resolved"] is False


def test_a_short_paying_negative_funding_is_booked_as_a_payment():
    r = rec([row("1.5")], pr=prints((PRINT, "-0.00005")))          # 0.5 x 60000 x -0.5 bp: the short PAYS 1.50; H1 fee = +1.5
    assert only(r)["status"] == "BOOKED" and D(only(r)["amount_usd"]) == D("-1.5") and D(r["booked_usd"]) == D("-1.5")


def test_a_long_position_pays_positive_funding():
    fl = [{"ts_ms": T_IN, "side": "Buy", "qty": "0.5", "price": "60000"}, {"ts_ms": T_OUT, "side": "Sell", "qty": "0.5", "price": "60000"}]
    r = rec([row("3.0", side="Buy")], fl=fl)
    assert only(r)["status"] == "BOOKED" and D(only(r)["amount_usd"]) == D("-3")
    assert only(rec([row("-3.0", side="Buy")], fl=fl))["status"] == "QUARANTINED_SIGN"


def test_the_right_sign_with_the_wrong_size_is_quarantined():
    r = rec([row("-30.0")])
    assert only(r)["status"] == "QUARANTINED_MAGNITUDE" and r["booked_usd"] is None
    assert only(rec([row("-3.1")]))["status"] == "BOOKED"          # within tolerance (rounding / mark drift) is fine


def test_a_zero_rate_cannot_be_sign_checked_so_only_a_zero_row_is_accepted():
    assert only(rec([row("0")], pr=prints((PRINT, "0"))))["status"] == "BOOKED"
    assert only(rec([row("-5.0")], pr=prints((PRINT, "0"))))["status"] == "QUARANTINED_SIGN"


# ------------------------------------------------------------------ unknown is not zero; zero only on proof
def test_a_held_settlement_with_no_row_is_unknown_pending_then_missing_never_zero():
    pending = rec([], now=PRINT + 60_000)
    assert only(pending)["status"] == "NO_ROW_PENDING" and pending["actions"] == [{"action": "record", "ts_ms": PRINT, "amount_usd": None, "prov": "UNKNOWN"}] and pending["booked_usd"] is None
    missing = rec([], now=PRINT + FS.GRACE_MS + 1)
    assert only(missing)["status"] == "NO_ROW_MISSING" and missing["booked_usd"] is None and missing["all_prints_resolved"] is False


def test_unavailable_history_books_and_declares_nothing():
    r = FS.recognise(symbol=SYM, perp_fills=fills(), prints=None, rows=[row("-3.0")], now_ms=NOW)
    assert r["actions"] == [] and "PRINTS_UNAVAILABLE" in r["flags"] and r["booked_usd"] is None and r["prints_available"] is False


def test_funding_is_declared_zero_only_when_a_complete_history_shows_no_held_settlement():
    r = rec([], pr=[])
    assert r["actions"] == [{"action": "declare_none", "reason": "the complete public funding history shows no settlement while the perp position was held", "prov": "DERIVED"}]
    assert r["booked_usd"] == "0" and r["all_prints_resolved"] is True
    before = rec([], pr=prints((T_IN - 5_000, "0.0001")))           # a print BEFORE the entry fill and one AFTER the exit are not held
    after = rec([], pr=prints((T_OUT + 5_000, "0.0001")))
    assert before["actions"][0]["action"] == "declare_none" and after["actions"][0]["action"] == "declare_none"


def test_a_print_while_flat_is_not_held():
    fl = fills(exit_=PRINT - 10_000)                                # flat before the print
    r = rec([row("-3.0")], fl=fl)
    assert r["prints"] == [] and r["actions"][0]["action"] == "declare_none"


def test_an_open_position_books_prints_up_to_now():
    r = rec([row("-3.0")], fl=fills(exit_=None))
    assert r["window"]["flat"] is False and only(r)["status"] == "BOOKED"


def test_a_fill_within_a_minute_of_the_print_makes_it_ambiguous():
    r = rec([row("-3.0")], fl=fills(exit_=PRINT + 30_000))
    assert only(r)["status"] == "AMBIGUOUS_INSTANT" and r["actions"] == [{"action": "record", "ts_ms": PRINT, "amount_usd": None, "prov": "UNKNOWN"}] and r["booked_usd"] is None


# ------------------------------------------------------------------ rows that must not be booked
def test_rows_outside_the_window_for_another_symbol_or_unusable_are_listed_and_never_booked():
    rows = [row("-3.0", ts=T_IN - 600_000, eid="OLD"), row("-3.0", ts=T_OUT + 900_000, eid="LATE"), row("-9.0", eid="OTHER", symbol="ETHUSDT"),
            row(None, eid="BLANK"), row("-3.0", eid="EUR", ccy="EUR"), row("-3.0", eid="NOTIME", ts=None), row("-3.0", eid="GOOD")]
    r = rec(rows)
    assert only(r)["status"] == "BOOKED" and only(r)["row_id"] == "GOOD" and D(r["booked_usd"]) == D("3")
    reasons = {u["exec_id"]: u["reason"] for u in r["unmatched_rows"]}
    assert reasons["OLD"] == "BEFORE_FIRST_FILL" and reasons["LATE"] == "AFTER_WINDOW" and reasons["OTHER"].startswith("NOT_THIS_SYMBOL")
    assert reasons["BLANK"] == "UNUSABLE:NO_FEE" and reasons["EUR"] == "UNUSABLE:FEE_CURRENCY_EUR" and reasons["NOTIME"] == "UNUSABLE:NO_EXEC_TIME"


def test_a_blank_fee_is_unknown_never_zero():
    r = rec([row(None)], now=PRINT + FS.GRACE_MS + 1)
    assert only(r)["status"] == "NO_ROW_MISSING" and r["booked_usd"] is None


# ------------------------------------------------------------------ duplicates and conflicts
def test_the_same_execution_twice_is_one_and_a_second_id_with_the_same_amount_is_flagged_not_added():
    assert D(rec([row("-3.0"), row("-3.0")])["booked_usd"]) == D("3")
    r = rec([row("-3.0", eid="F1"), row("-3.0", eid="F2", ts=PRINT + 5)])
    assert D(r["booked_usd"]) == D("3") and any(f.startswith("DUPLICATE_PRINT_ROW") for f in r["flags"])


def test_two_rows_at_one_settlement_with_different_amounts_are_a_conflict_and_unknown():
    r = rec([row("-3.0", eid="F1"), row("-2.0", eid="F2", ts=PRINT + 5)])
    assert only(r)["status"] == "CONFLICT" and r["booked_usd"] is None and any(f.startswith("FUNDING_CONFLICT") for f in r["flags"])


# ------------------------------------------------------------------ the wallet is the independent measurement
def test_a_wallet_delta_that_equals_the_row_confirms_it_and_one_that_does_not_blocks_it():
    ok = rec([row("-3.0")], wallet=[(PRINT - 10_000, "1000.00"), (PRINT + 10_000, "1003.00")])
    assert only(ok)["status"] == "BOOKED_WALLET_CONFIRMED" and only(ok)["wallet"]["status"] == "CONFIRMED" and D(ok["booked_usd"]) == D("3")
    bad = rec([row("-3.0")], wallet=[(PRINT - 10_000, "1000.00"), (PRINT + 10_000, "1000.00")])       # the row says +3 but the cash did not move
    assert only(bad)["status"] == "WALLET_MISMATCH" and bad["booked_usd"] is None
    assert bad["actions"] == [{"action": "record", "ts_ms": PRINT, "amount_usd": None, "prov": "UNKNOWN"}]


def test_a_wallet_change_across_a_fill_is_not_funding_so_the_check_is_skipped_and_flagged():
    r = rec([row("-3.0")], wallet=[(PRINT - 10_000, "1000.00"), (PRINT + 10_000, "1500.00")], other=[PRINT + 1_000])
    assert only(r)["status"] == "BOOKED" and only(r)["wallet"] == {"status": "NOT_SAMPLED"} and any(f.startswith("WALLET_UNCONFIRMED") for f in r["flags"])


# ------------------------------------------------------------------ the row parser never raises and never defaults a fee
@pytest.mark.parametrize("bad", [{}, {"exec_id": "x"}, {"exec_id": "x", "exec_time_ms": 5}, {"exec_id": "x", "exec_time_ms": 5, "fee": "abc", "fee_ccy": "USDT"}, {"exec_id": "x", "exec_time_ms": "nan", "fee": "1", "fee_ccy": "USDT"}])
def test_parse_row_refuses_what_it_cannot_use(bad):
    got, why = FS.parse_row(bad)
    assert got is None and why


# ------------------------------------------------------------------ the executor and the session against the fake venue
@pytest.fixture
def w(tmp_path, monkeypatch):
    return World(tmp_path, monkeypatch)


def _held_session(w, *, rate="0.0001", convention="H1", publish_print=True, settle=True, sample_wallet=True, credit_wallet=True):
    """Hedge, hold, let the venue settle funding mid-hold, exit, verify flat. Returns (run, rec)."""
    assert w.run("preflight") == 0 and w.run("plan", "--symbol", "BTCUSDT", "--notional-usd", "90") == 0
    run, ctx, rec = begin(w)
    from test_ms_testnet_executor import drive_full
    assert drive_full(w, run, 2) == "HEDGED"
    w.fake.funding_prints = [] if publish_print is not None else None
    w.clock.sleep(300)                                              # hold well clear of the entry fills (a fill within 60 s of a settlement makes it ambiguous)
    if sample_wallet:
        run.ex.account_state(run.baseline, 0)
    w.clock.sleep(10)
    if settle:
        w.fake.settle_funding("BTCUSDT", rate, convention=convention, publish_print=publish_print, credit_wallet=credit_wallet)
    w.clock.sleep(10)
    if sample_wallet:
        run.ex.account_state(run.baseline, 0)
    w.clock.sleep(120)
    run.engine.request_exit(run.gid, w.clock.now(), "TEST_EXIT")
    st = drive_full(w, run, 6)
    assert st in ("CLOSED", "FLAT"), st
    run.snapshot_record(rec, final=True)
    return run, rec


def test_a_held_settlement_is_booked_into_the_session_net_and_confirmed_by_the_wallet(w):
    run, rec = _held_session(w)
    assert rec["flat_verified"] is True
    f = json.load(open(os.path.join(w.state_dir, "funding_settlement.json")))["recognitions"][0]
    assert [p["status"] for p in f["prints"]] == ["BOOKED_WALLET_CONFIRMED"] and f["booked_usd"] is not None
    qty = D(str(w.fake.execs[0]["execQty"]))                                           # the perp quantity the venue held
    expected = -(-qty) * D("60000.5") * D("0.0001")                                    # short: + qty * mark * rate received
    assert rec["net"]["funding_usd"] == pytest.approx(float(expected), abs=1e-6) and rec["net"]["funding_usd"] > 0
    assert rec["net"]["price_pnl_usd"] is not None


def test_the_opposite_row_convention_leaves_funding_unknown_in_the_net(w):
    run, rec = _held_session(w, convention="H2", sample_wallet=False)
    f = json.load(open(os.path.join(w.state_dir, "funding_settlement.json")))["recognitions"][0]
    assert f["prints"][0]["status"] == "QUARANTINED_SIGN" and rec["net"]["funding_usd"] is None


def test_a_settlement_whose_cash_never_arrived_is_blocked_by_the_wallet(w):
    run, rec = _held_session(w, rate="0.01", credit_wallet=False)      # 1% so the missing cash is well above the wallet tolerance
    f = json.load(open(os.path.join(w.state_dir, "funding_settlement.json")))["recognitions"][0]
    assert f["prints"][0]["status"] == "WALLET_MISMATCH" and rec["net"]["funding_usd"] is None


def test_no_settlement_in_the_window_is_declared_zero_from_a_complete_history(w):
    run, rec = _held_session(w, settle=False)
    assert rec["net"]["funding_usd"] == 0.0


def test_a_history_the_venue_cannot_answer_keeps_funding_unknown(w):
    assert w.run("preflight") == 0 and w.run("plan", "--symbol", "BTCUSDT", "--notional-usd", "90") == 0
    assert w.execute() == 0 and w.fake.funding_prints is None                          # the fake does not implement the endpoint: nothing is declared
    assert w.record()["net"]["funding_usd"] is None and w.record()["flat_verified"] is True


def test_a_malformed_funding_row_no_longer_fails_the_venue_read(w):
    assert w.run("preflight") == 0 and w.run("plan", "--symbol", "BTCUSDT", "--notional-usd", "90") == 0
    run, ctx, rec = begin(w)
    for bad in ({"execId": "F1", "execType": "Funding", "symbol": "BTCUSDT", "category": "linear"},                      # nothing else at all
                {"execId": "F2", "execType": "Funding", "symbol": "BTCUSDT", "category": "linear", "execQty": "", "execPrice": "", "execFee": "", "execTime": "x", "side": "Sell"}):
        w.fake.execs.append({"orderId": "", "orderLinkId": "", **bad})
    v = run.ex.venue_state()
    assert v["complete"] is True and {u["exec_id"] for u in v["unattributed"]} == {"F1", "F2"} and all(u["fee"] is None for u in v["unattributed"])
    assert all(x["fill_id"] not in ("F1", "F2") for x in v["executions"])


def test_the_executor_can_read_the_public_history_and_the_transaction_log_stays_refused(w):
    assert w.run("preflight") == 0 and w.run("plan", "--symbol", "BTCUSDT", "--notional-usd", "90") == 0
    run, ctx, rec = begin(w)
    w.fake.funding_prints = [(1_794_000_000_000, "0.0001")]
    assert run.ex.funding_history("BTCUSDT", 1_793_000_000_000, 1_795_000_000_000) == [{"ts_ms": 1_794_000_000_000, "rate": "0.0001"}]
    assert run.ex.funding_history("BTCUSDT", 1_795_000_000_000, 1_796_000_000_000) == []                 # a complete empty answer is a statement: no settlement
    w.fake.funding_prints = None
    assert run.ex.funding_history("BTCUSDT", 1, 2) is None                                                # an error is UNKNOWN, not empty
    # the capability pin (unconditional): the transaction log is NOT readable by the executor role (the Wallet-class permission question is the owner's); the public history and the execution list are
    cap = RC.Capability.for_role(RC.TESTNET_EXECUTOR, venue="testnet", credential_class=RC.TESTNET_CREDENTIAL_CLASS, api_key="k" * 20, api_secret="s" * 20)
    assert cap.check("GET", "/v5/account/transaction-log", True, cap.host) == "ENDPOINT_NOT_ALLOWED"
    assert cap.check("GET", "/v5/execution/list", True, cap.host) is None
    assert cap.check("GET", "/v5/market/funding/history", False, cap.host) is None


def test_the_funding_evidence_events_pass_the_guard_state_the_unverified_convention_and_never_carry_a_venue_id(w):
    run, rec = _held_session(w)
    doc = json.load(open(os.path.join(w.state_dir, "funding_settlement.json")))
    evs = EL.funding_events(doc)
    assert [e[1] for e in evs] == ["FUNDING_SETTLEMENT", "FUNDING_RECOGNITION"]
    for i, (kind, stage, payload) in enumerate(evs):
        XE.make_event(kind, {"stage": stage, **payload}, session_id=None, occurred_at_ms=1, event_id=EL.event_id("s", kind, stage, i))
    assert evs[1][2]["sign_hypothesis"].endswith("UNVERIFIED") and evs[0][2]["status"] == "BOOKED_WALLET_CONFIRMED" and evs[0][2]["cash_check"] == "CONFIRMED"
    assert all(str(r.get("row_id")) not in json.dumps(evs) for r in doc["recognitions"][0]["prints"]) and evs == EL.funding_events(doc)
    assert EL.funding_events(None) == []
