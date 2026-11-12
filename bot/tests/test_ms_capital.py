"""ms_capital: smallest-of-limits sizing and realised-only compounding. Offline, pure; the book lives in tmp_path (never bot/state)."""
import os
import sys
from decimal import Decimal as D

import pytest

BOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [BOT, os.path.join(BOT, "tools")]
import ms_capital as C  # noqa: E402

CEIL = 500


def alloc(**kw):
    base = dict(grant_max_usd=375, ceiling_usd=CEIL, open_gross_usd=0, available_usdt=10_000, basis=D(10_000), start_cash=D(10_000), loss_streak=0, drawdown_usd=D(0), profit_seeking=True, realised_usd=D(0))
    base.update(kw)
    return C.allocate(**base)


def test_day_one_deploys_forty_percent_of_the_ceiling_not_all_of_it():
    a = alloc()
    assert a.notional == D("200.00") and a.binding == "COMPOUNDED_SIZE"


def test_the_grant_maximum_the_ceiling_left_and_margin_are_hard_caps():
    a = alloc(grant_max_usd=120)
    assert a.notional == D("120.00") and a.binding == "GRANT_MAX"
    a = alloc(open_gross_usd=450)
    assert a.notional == D("50.00") and a.binding == "AGGREGATE_CEILING_LEFT"
    a = alloc(available_usdt=204)                                           # 204 / 2.04 = 100: the plan commits spot + perp margin at leverage 1 + its 2% buffer per USD (was a 1.16 per-USD typed constant)
    assert a.notional == D("100.00") and a.binding == "MARGIN"
    a = alloc(available_usdt=136, leverage=3)                               # the same function at leverage 3: (1 + 1/3) x 1.02 = 1.36 per USD
    assert a.notional == D("100.00") and a.binding == "MARGIN"
    assert alloc(available_usdt=232).notional < D("200")                    # $232 no longer sizes $200: the plan would demand $408 and refuse (the old allocator and the plan disagreed 1.16x vs 2.04x)


def test_a_mechanics_session_is_capped_small():
    a = alloc(profit_seeking=False)
    assert a.notional == D("100.00") and a.binding == "MECHANICS_ONLY"


def test_gains_scale_the_size_up_but_never_beyond_eighty_percent_of_the_ceiling_or_the_grant():
    a = alloc(basis=D(10_500), start_cash=D(10_000))                       # +5% -> 200 * 1.05
    assert a.notional == D("210.00")
    a = alloc(basis=D(30_000), start_cash=D(10_000), grant_max_usd=500)     # scale clamps at 2 -> 400 = 80% of the ceiling
    assert a.notional == D("400.00") and a.notional <= D(CEIL) * C.MAX_FRACTION
    assert alloc(basis=D(30_000), start_cash=D(10_000), grant_max_usd=300).notional == D("300.00")   # the owner's grant still wins


def test_losses_shrink_the_size_and_a_losing_streak_halves_then_stops_never_martingale():
    assert alloc(basis=D(9_500), start_cash=D(10_000)).notional == D("190.00")
    assert alloc(loss_streak=2).notional == D("100.00")
    a = alloc(loss_streak=4)
    assert a.notional == D(0) and a.binding == "LOSS_STREAK_STOP"
    for streak in range(0, 8):
        assert alloc(loss_streak=streak).notional <= alloc(loss_streak=0).notional          # size is never larger after losses


def test_unknown_or_missing_inputs_refuse_instead_of_guessing():
    for kw, why in (({"available_usdt": None}, "AVAILABLE_CASH_UNKNOWN_OR_ZERO"), ({"available_usdt": 0}, "AVAILABLE_CASH_UNKNOWN_OR_ZERO"), ({"basis": None}, "CAPITAL_BASIS_UNKNOWN"),
                    ({"start_cash": None}, "CAPITAL_BASIS_UNKNOWN"), ({"grant_max_usd": None}, "LIMITS_UNKNOWN"), ({"ceiling_usd": 0}, "LIMITS_UNKNOWN"),
                    ({"halted": True}, "LOSS_STREAK_STOP_NOT_ACKNOWLEDGED"), ({"drawdown_usd": D("11.10")}, "DRAWDOWN_LIMIT")):
        a = alloc(**kw)
        assert a.notional == D(0) and why in a.reasons, (kw, a.as_dict())


def test_below_the_minimum_tradable_size_stays_idle():
    a = alloc(available_usdt=20)
    assert a.notional == D(0) and a.binding == "BELOW_MIN_NOTIONAL"


# ------------------------------------------------------------------------------------------------ the book: realised-only compounding
def book(tmp_path):
    return C.CapitalBook(str(tmp_path))


def test_the_baseline_is_the_first_flat_cash_read_and_never_moves(tmp_path):
    b = book(tmp_path)
    b.ensure_baseline("10000")
    b.ensure_baseline("12345")
    assert b.load()["start_cash_usd"] == "10000"


def test_a_realised_loss_is_booked_in_full_and_a_gain_only_up_to_what_the_ledger_explains(tmp_path):
    b = book(tmp_path)
    b.ensure_baseline("10000")
    b.park("s1", "10000", "-1.50", True, True)
    r = b.settle("9998.50")
    assert r["status"] == "SETTLED" and r["credited_usd"] == "-1.50" and b.basis() == D("9998.50") and b.load()["loss_streak"] == 1
    b.park("s2", "9998.50", "2.00", True, True)                              # ledger says +2.00 and the cash agrees
    r = b.settle("10000.50")
    assert r["status"] == "SETTLED" and r["credited_usd"] == "2.00" and b.load()["loss_streak"] == 0


def test_cash_nothing_explains_never_raises_the_allocation(tmp_path):
    b = book(tmp_path)
    b.ensure_baseline("10000")
    b.park("s1", "10000", None, False, True)                                 # the ledger could not know its net (funding unread)
    r = b.settle("10500")                                                    # a faucet top-up looks like +500
    assert r["status"] == "UNVERIFIED_GAIN" and r["credited_usd"] == "0" and b.basis() == D("10000")
    assert b.load()["unverified_excess_usd"] == "500"
    b.park("s2", "10500", "1.00", False, True)                               # funding unread: ledger lower bound +1.00, cash +50 -> only +1.00 is credited
    r = b.settle("10550")
    assert r["credited_usd"] == "1.00" and r["unverified_excess_usd"] == "49.00" and b.basis() == D("10001.00")


def test_when_funding_was_read_a_cash_difference_the_ledger_cannot_explain_is_flagged(tmp_path):
    b = book(tmp_path)
    b.ensure_baseline("10000")
    b.park("s1", "10000", "1.00", True, True)
    r = b.settle("10040")
    assert r["status"] == "UNEXPLAINED_CASH" and r["credited_usd"] == "0" and r["unverified_excess_usd"] == "40.00"     # an unreconciled gain credits NOTHING
    b.park("s2", "10041", "-1.00", True, True)
    assert b.settle("10000")["status"] == "UNEXPLAINED_CASH"                  # a loss bigger than the ledger knows is ALSO flagged (and still debited in full)
    assert b.basis() == D("10000") + D("-41")


def test_a_session_that_did_not_end_flat_counts_its_losses_but_never_its_gains_and_settle_is_idempotent(tmp_path):
    b = book(tmp_path)
    b.ensure_baseline("10000")
    b.park("s1", "10000", "1.00", True, False)                                # ended NOT flat: parked, so a loss cannot go missing
    r = b.settle("10001")
    assert r["status"] == "NOT_FLAT_SESSION" and r["credited_usd"] == "0" and b.basis() == D("10000")
    b.park("s2", "10001", "-2.00", True, False)
    b.park("s2", "10001", "-2.00", True, False)                               # duplicate park: ignored
    assert b.settle("9999")["credited_usd"] == "-2" and b.load()["loss_streak"] == 1
    assert b.settle("9999")["status"] == "NO_PENDING"
    b.park("s2", "9999", "-2.00", True, True)                                 # an already-settled session id is never booked twice
    assert b.load()["pending"] is None and b.basis() == D("9998")


def test_the_reserve_is_withheld_from_the_sizing_basis_but_never_softens_a_loss(tmp_path):
    b = book(tmp_path)
    b.ensure_baseline("10000")
    b.park("s1", "10000", "40.00", True, True)
    b.settle("10040")
    assert b.basis() == D("10040") and b.reserve_usd() == D("10.00") and b.sizing_basis() == D("10030.00")      # 25% default reserve of the 40 gain
    b.park("s2", "10040", "-10.00", True, True)
    b.settle("10030")
    assert b.basis() == D("10030") and b.sizing_basis() == D("10020.00")                                           # the loss is debited in full; the reserve stays
    assert b.set_reserve_fraction("0.5") and b.reserve_usd() == D("20.00")
    assert not b.set_reserve_fraction("0.95") and not b.set_reserve_fraction("-1") and not b.set_reserve_fraction("x")
    assert alloc(basis=b.sizing_basis(), start_cash=D(10000)).notional <= alloc(basis=b.basis(), start_cash=D(10000)).notional


def test_a_reserve_fraction_the_owner_set_to_zero_is_zero_and_only_an_unset_one_is_the_default(tmp_path):
    """`_d(x) or DEFAULT` treated the Decimal 0 as unset: the owner's explicit 0 silently became 25% (specialist E, F10)."""
    b = book(tmp_path)
    b.ensure_baseline("10000")
    b.park("s1", "10000", "40.00", True, True)
    b.settle("10040")
    assert b.reserve_usd() == D("10.00")                                                  # unset: the documented default
    assert b.set_reserve_fraction("0")
    assert b.reserve_usd() == D("0") and b.sizing_basis() == D("10040")                   # explicit zero: nothing withheld
    assert b.set_reserve_fraction("0.9") and b.reserve_usd() == D("36.00")
    assert b.set_reserve_fraction("0.0") and b.reserve_usd() == D("0")                     # every spelling of zero


def test_a_declared_deposit_or_withdrawal_is_never_a_trading_result(tmp_path):
    b = book(tmp_path)
    b.ensure_baseline("10000")
    b.park("s1", "10000", "-1.00", True, True)
    assert b.declare_transfer("dep-1", "500") and not b.declare_transfer("dep-1", "500") and not b.declare_transfer("", "5") and not b.declare_transfer("z", "0")      # made, THEN declared
    r = b.settle("10499")                                                    # +500 deposit, -1 trading loss
    assert r["status"] == "SETTLED" and r["transfers_removed_usd"] == "500" and r["credited_usd"] == "-1" and b.basis() == D("9999")
    assert not b.declare_transfer("dep-1", "500")                            # applied ids stay known: a retry cannot re-subtract it
    b.park("s2", "10499", "0.50", True, True)
    assert b.declare_transfer("wd-1", "-200")
    r = b.settle("10299.50")
    # the withdrawal is removed from the delta too, but a DECLARED withdrawal (the owner's unverified claim) can never lift a credit above the cash that actually rose (red-team F3): the cash FELL by
    # 199.50, so the +0.50 the ledger explains is recorded as excess and sizes nothing. This used to credit +0.50; the change only ever makes the book more conservative.
    assert r["transfers_removed_usd"] == "-200" and D(r["credited_usd"]) == 0 and D(r["unverified_excess_usd"]) == D("0.50"), r


def test_concurrent_settlement_credits_exactly_once(tmp_path):
    import threading
    b = book(tmp_path)
    b.ensure_baseline("10000")
    b.park("s1", "10000", "5.00", True, True)
    out = []
    ths = [threading.Thread(target=lambda: out.append(C.CapitalBook(str(tmp_path)).settle("10005")["status"])) for _ in range(8)]
    [t.start() for t in ths]
    [t.join() for t in ths]
    assert sorted(out).count("SETTLED") == 1 and out.count("NO_PENDING") == 7
    assert b.load()["realised_usd"] == "5" and len(b.load()["history"]) == 1


def test_a_restart_between_park_and_settle_loses_nothing_and_duplicates_nothing(tmp_path):
    C.CapitalBook(str(tmp_path)).ensure_baseline("10000")
    C.CapitalBook(str(tmp_path)).park("s1", "10000", "-3.00", True, True)       # process 1 dies here
    b2 = C.CapitalBook(str(tmp_path))                                           # process 2 starts from the file
    assert b2.load()["pending"]["session_id"] == "s1"
    assert b2.settle("9997")["credited_usd"] == "-3" and C.CapitalBook(str(tmp_path)).settle("9997")["status"] == "NO_PENDING"


def test_a_failed_cash_read_leaves_the_session_pending_and_credits_nothing(tmp_path):
    b = book(tmp_path)
    b.ensure_baseline("10000")
    b.park("s1", "10000", "3.00", True, True)
    assert b.settle(None)["status"] == "CASH_UNKNOWN" and b.load()["pending"] is not None and b.basis() == D("10000")


def test_four_losses_halt_entries_until_the_owner_acknowledges(tmp_path):
    b = book(tmp_path)
    b.ensure_baseline("10000")
    cash = D("10000")
    for i in range(4):
        b.park(f"s{i}", str(cash), "-1.00", True, True)
        cash -= 1
        b.settle(str(cash))
    s = b.load()
    assert s["halted"] is True and alloc(loss_streak=s["loss_streak"], halted=s["halted"]).notional == D(0)
    assert b.acknowledge_halt() is True and b.load()["loss_streak"] == 0 and b.acknowledge_halt() is False


def test_drawdown_is_measured_from_the_verified_peak_and_the_report_is_labelled_simulation(tmp_path):
    b = book(tmp_path)
    b.ensure_baseline("10000")
    b.park("s1", "10000", "5.00", True, True)
    b.settle("10005")
    b.park("s2", "10005", "-3.00", True, True)
    b.settle("10002")
    assert b.drawdown() == D("3.00")
    r = b.report()
    assert r["wins"] == 1 and r["losses"] == 1 and r["profit_factor"] == "1.67" and "TESTNET SIMULATION" in r["label"]


def test_a_corrupt_book_file_is_refused_not_read_as_empty_money(tmp_path):
    """Was: a corrupt file read as an empty book (zero notional). A fresh-looking book also resets the baseline, the loss streak and the halt flag, so it now REFUSES (see the BookUnreadable test)."""
    (tmp_path / "capital.json").write_text("{not json")
    with pytest.raises(C.BookUnreadable):
        book(tmp_path).basis()


# ================================================================ round two: findings from the independent accounting review (verified in code first)
import json as _json  # noqa: E402
import time as _time  # noqa: E402


def test_a_session_parked_before_execution_still_gets_its_loss_debited_and_never_a_gain_credited_if_the_process_died(tmp_path):
    """REPRODUCED by review: park ran only AFTER execute returned, so a crash after orders left no pending record: the next settle said NO_PENDING and the loss was never booked."""
    b = book(tmp_path)
    b.ensure_baseline("10000")
    assert b.park("s1", "10000", None, False, False) is True                   # the conservative placeholder, written BEFORE any order
    # ... the process dies here: update_pending is never called ...
    r = b.settle("9996")
    assert r["status"] == "SETTLED" and r["credited_usd"] == "-4" and b.basis() == D("9996") and b.load()["loss_streak"] == 1
    b.park("s2", "9996", None, False, False)
    r = b.settle("10050")                                                       # a gain from a session nobody could verify
    assert r["credited_usd"] == "0" and r["status"] in ("UNVERIFIED_GAIN", "NOT_FLAT_SESSION") and b.basis() == D("9996")


def test_update_pending_replaces_the_placeholder_only_for_the_same_session(tmp_path):
    b = book(tmp_path)
    b.ensure_baseline("10000")
    b.park("s1", "10000", None, False, False)
    assert b.update_pending("other", "5", True, True) is False and b.load()["pending"]["flat_verified"] is False
    assert b.update_pending("s1", "2.00", True, True) is True
    p = b.load()["pending"]
    assert p["flat_verified"] is True and p["ledger_net_known_usd"] == "2.00" and p["parked_ms"] > 0
    assert D(b.settle("10002")["credited_usd"]) == D("2")


def test_a_different_unsettled_session_is_never_overwritten(tmp_path):
    b = book(tmp_path)
    b.ensure_baseline("10000")
    assert b.park("s1", "10000", None, False, False) is True
    assert b.park("s2", "9990", None, False, False) is False                   # refused: s1's cash result must be booked first
    assert b.load()["pending"]["session_id"] == "s1"
    assert b.park("s1", "10000", None, False, False) is True                   # the same session is idempotent
    assert b.park("s3", None, None, False, False) is False                     # no trustworthy starting cash


def test_a_transfer_declared_before_the_session_was_parked_is_not_part_of_its_delta(tmp_path, monkeypatch):
    """REPRODUCED by review: a declared deposit was subtracted from whichever session settled next, even when it was already inside that session's starting cash (a phantom loss)."""
    b = book(tmp_path)
    b.ensure_baseline("10000")
    clock = {"t": 1_000.0}
    monkeypatch.setattr(_time, "time", lambda: clock["t"])
    assert b.declare_transfer("dep-1", "500")                                  # declared at t=1000 and made before the session started
    clock["t"] = 2_000.0
    b.park("s1", "10500", None, False, False)                                  # starting cash ALREADY includes the deposit
    r = b.settle("10500")
    assert r["transfers_removed_usd"] == "0" and r["credited_usd"] == "0", r
    applied = {t["event_id"]: t for t in b.load()["transfers_applied"]}
    assert applied["dep-1"]["counted"] is False and applied["dep-1"]["applied_to"] == "s1"
    clock["t"] = 3_000.0
    b.park("s2", "10500", None, False, False)
    clock["t"] = 4_000.0
    assert b.declare_transfer("dep-2", "100")                                  # declared AFTER the session started: it is inside this session's delta
    r = b.settle("10600")
    assert r["transfers_removed_usd"] == "100" and r["credited_usd"] == "0"


def test_only_a_verified_credited_gain_breaks_a_losing_streak(tmp_path):
    """REPRODUCED by review: any zero-credit outcome reset the streak, so losses interleaved with unverified gains never reached the stop."""
    b = book(tmp_path)
    b.ensure_baseline("10000")
    cash = D(10000)
    for k, delta in enumerate(("-1", "+30", "-1", "+30", "-1")):               # +30 with no ledger net: UNVERIFIED, credited 0
        b.park(f"s{k}", str(cash), None, False, True)
        cash += D(delta)
        b.settle(str(cash))
    assert b.load()["loss_streak"] == 3 and b.load()["unverified_excess_usd"] == "60"
    b.park("s9", str(cash), "5.00", True, True)
    cash += D(5)
    assert D(b.settle(str(cash))["credited_usd"]) == D("5") and b.load()["loss_streak"] == 0


def test_the_drawdown_stop_has_an_owner_acknowledgement_that_changes_no_money(tmp_path):
    b = book(tmp_path)
    b.ensure_baseline("10000")
    b.park("s1", "10000", "20.00", True, True)
    b.settle("10020")                                                          # a verified +20 sets the peak
    b.park("s2", "10020", "-15.00", True, True)
    b.settle("10005")                                                          # -15 from the peak: beyond the 11.10 stop
    dd = b.drawdown()
    assert dd == D("15.00") and alloc(basis=b.sizing_basis(), start_cash=D(10000), drawdown_usd=dd).notional == D(0)
    realised, history = b.load()["realised_usd"], len(b.load()["history"])
    assert b.acknowledge_drawdown() is True and b.drawdown() == D(0)
    assert b.load()["realised_usd"] == realised and len(b.load()["history"]) == history and b.load()["drawdown_acknowledged"][0]["from_peak_usd"] == "10020"
    assert b.acknowledge_drawdown() is False                                   # nothing left to acknowledge
    assert alloc(basis=b.sizing_basis(), start_cash=D(10000), drawdown_usd=b.drawdown()).notional > 0


def test_the_capital_constants_cannot_drift_from_the_testnet_manifest_limits():
    import ms_testnet_adapter as AD
    assert C.DRAWDOWN_STOP_USD == D(str(AD.MAX_LOSS_CEILING_USD)) * 2, "the drawdown stop must stay twice the per-session loss ceiling"
    ceiling = D(str(AD.AGGREGATE_CEILING_USD))
    assert C.INITIAL_FRACTION * ceiling <= C.MAX_FRACTION * ceiling <= ceiling and C.MAX_SCALE >= 1
    assert C.MIN_NOTIONAL_USD < C.INITIAL_FRACTION * ceiling


def test_a_transfer_declared_between_the_cash_read_and_the_park_is_in_the_delta_not_the_starting_cash(tmp_path, monkeypatch):
    """Red-team MED: the deposit was declared AFTER the starting cash was read but BEFORE park(): comparing against the park time excluded it, so a real deposit became a phantom gain.
    The comparison is against the instant the cash was READ."""
    b = book(tmp_path)
    b.ensure_baseline("10000")
    clock = {"t": 1_000.0}
    monkeypatch.setattr(_time, "time", lambda: clock["t"])
    cash_read_ms = 1_000_000                                                   # starting cash (10000) read at t=1000
    clock["t"] = 1_500.0
    assert b.declare_transfer("dep-straddle", "500")                          # declared after the read ...
    clock["t"] = 2_000.0
    assert b.park("s1", "10000", None, False, False, cash_read_ms=cash_read_ms)    # ... but before the park
    r = b.settle("10500")                                                      # the deposit landed in cash after the read
    assert r["transfers_removed_usd"] == "500" and r["credited_usd"] == "0", r


def test_halt_and_drawdown_acknowledgements_are_reachable_only_from_the_owner_cli():
    """Red-team MED: the autopilot must never clear its own capital stop. Only the `capital --ack-*` CLI branch may call the acknowledgements; no other function in the tools tree does."""
    import ast
    # every method that clears a stop, re-bases the drawdown or changes what the book counts as cash/sizing: the autopilot's loop, run_once and the session tool never call them (red-team F2)
    OWNER_ONLY = ("acknowledge_halt", "acknowledge_drawdown", "acknowledge_review", "declare_transfer", "set_reserve_fraction", "initialise", "rebuild_view", "rebaseline")
    tools = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools")
    offenders = []
    for fn in sorted(os.listdir(tools)):
        if not fn.endswith(".py"):
            continue
        tree = ast.parse(open(os.path.join(tools, fn), encoding="utf-8").read())
        in_cli_main = set()
        if fn == "ms_testnet_autopilot.py":                                       # the CLI's ``main`` AND the closures it defines for its gated steps (init_account, rebuild, rebaseline)
            for top in tree.body:
                if isinstance(top, ast.FunctionDef) and top.name == "main":
                    in_cli_main = {id(n) for n in ast.walk(top)}
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for sub in ast.walk(node):
                    if isinstance(sub, ast.Attribute) and sub.attr in OWNER_ONLY and id(node) not in in_cli_main:
                        offenders.append((fn, node.name, sub.attr))
    offenders = [o for o in offenders if o[0] != "ms_capital.py"]
    assert offenders == [], offenders


def test_a_present_but_unreadable_book_is_not_a_fresh_book_and_is_left_untouched(tmp_path):
    """A corrupted or tampered capital.json used to load as a FRESH book: the baseline, the loss streak and the halt flag all reset. It must refuse, and keep the file as evidence."""
    b = book(tmp_path)
    b.ensure_baseline("10000")
    b.park("s1", "10000", None, False, True)
    b.settle("9990")
    assert b.load()["loss_streak"] == 1
    for junk in ("{broken", "[]", _json.dumps({"schema": "something/else"}), ""):
        with open(b.path, "w", encoding="utf-8") as fh:
            fh.write(junk)
        with pytest.raises(C.BookUnreadable):
            b.load()
        for call in (lambda: b.ensure_baseline("10000"), lambda: b.settle("10000"), lambda: b.park("s2", "10000", None, False, True), b.report):
            with pytest.raises(C.BookUnreadable):
                call()
        assert open(b.path, encoding="utf-8").read() == junk                  # never overwritten
    os.unlink(b.path)
    assert book(tmp_path).load()["loss_streak"] == 0                           # only a MISSING file is a fresh book


def test_a_schema_valid_book_stripped_of_its_stop_state_is_not_a_fresh_book(tmp_path):
    b = book(tmp_path)
    for doc in ({"schema": C.BOOK_SCHEMA}, {"schema": C.BOOK_SCHEMA, "start_cash_usd": "10000", "realised_usd": "0"}):          # no loss_streak / halted / history: a halt could be silently dropped
        with open(b.path, "w", encoding="utf-8") as fh:
            fh.write(_json.dumps(doc))
        with pytest.raises(C.BookUnreadable):
            b.load()


def test_the_allocators_margin_per_usd_is_the_plans_own_capital_for_every_leverage():
    """One definition: for any notional and leverage the allocator's margin cap and build_plan's cash need are the same economic_model function (no 1.16 constant to drift from it)."""
    import economic_model as EM
    assert C.MARGIN_PER_USD == EM.hedged_pair_capital_usd(1, 1).value == D("2.04")
    for lev in (1, 2, 3, 5):
        per_usd = EM.hedged_pair_capital_usd(1, lev).value
        a = alloc(available_usdt=per_usd * 100, leverage=lev)
        assert a.notional == D("100.00") and a.binding == "MARGIN", lev


# ---------------------------------------------------------------- an undeclared inflow must not erase a loss the ledger saw WHOLE
def test_a_loss_the_ledger_saw_whole_is_debited_even_when_an_undeclared_inflow_lifts_the_cash(tmp_path):
    """Funding was READ, so the ledger's known net is every component: a loss of 0.40. A +100 USDT top-up that nobody declared lifts the wallet, which used to book NOTHING (credited 0, streak 0): the
    loss-streak and drawdown stops were evadable by an unannounced faucet transfer. The cash is still UNEXPLAINED (an ALERT upstream); the known loss is booked."""
    b = book(tmp_path)
    b.ensure_baseline("10000")
    assert b.park("s1", "10000", "-0.40", True, True)
    r = b.settle("10100")
    assert r["status"] == "UNEXPLAINED_CASH" and D(r["credited_usd"]) == D("-0.40")
    assert D(r["unverified_excess_usd"]) == D("100.40")                                  # the whole unexplained inflow stays visible as excess, never sizes anything
    assert b.load()["loss_streak"] == 1 and b.basis() == D("9999.60") and b.report()["verified_losses_usd"] == "-0.40"


def test_with_funding_unread_a_cash_rise_is_not_read_as_an_inflow_that_hid_a_loss(tmp_path):
    """Funding NOT read: the ledger's known net lacks the funding income, so a negative known with a rising wallet may simply be funding the book never saw. The book cannot tell the two apart, so it books
    nothing (the existing, conservative behaviour: no gain credited, no phantom loss) and says UNVERIFIED_EXCESS. Stated limit, not a claim of detection."""
    b = book(tmp_path)
    b.ensure_baseline("10000")
    assert b.park("s1", "10000", "-0.40", False, True)
    r = b.settle("10000.60")
    assert r["status"] == "UNVERIFIED_EXCESS" and D(r["credited_usd"]) == 0 and b.load()["loss_streak"] == 0


def test_a_known_gain_and_a_plain_loss_are_booked_exactly_as_before(tmp_path):
    b = book(tmp_path)
    b.ensure_baseline("10000")
    b.park("s1", "10000", "1.00", True, True)
    assert b.settle("10001")["status"] == "SETTLED" and b.basis() == D("10001")
    b.park("s2", "10001", "-2.00", True, True)
    r = b.settle("9999")
    assert D(r["credited_usd"]) == D("-2.00") and b.load()["loss_streak"] == 1


# ---- a loss the ledger saw WHOLE is booked whole on both sides of zero (second independent review: the delta < 0 branch used to book only the cash's smaller fall)
@pytest.mark.parametrize("inflow", ["0", "0.02", "1", "2.98", "3", "3.01", "10"])
def test_a_known_loss_is_debited_whole_whatever_undeclared_inflow_lifted_the_cash(tmp_path, inflow):
    """Ledger known -3.00 (funding read, flat). An undeclared inflow I makes the cash move by -3 + I: credited must be -3 for EVERY I (it used to be -3 + I while the cash still fell, and the
    result was not even monotone: an inflow of 2.98 erased 2.98 of the loss, an inflow of 3.00 erased none). The unexplained cash is flagged and stops entries; the loss stays booked."""
    book = C.CapitalBook(str(tmp_path))
    book.ensure_baseline("100")
    assert book.park("s1", "100", "-3.00", True, True)
    r = book.settle(str(D("100") - D("3") + D(inflow)))
    assert D(r["credited_usd"]) == D("-3.00"), r
    tol = max(C.TOLERANCE_USD, D("3") * C.TOLERANCE_FRAC)
    unexplained = abs(D(inflow)) > tol                                            # inside the tolerance the cash simply agrees with the ledger
    assert r["status"] == ("UNEXPLAINED_CASH" if unexplained else "SETTLED")
    assert (book.load()["review_required"] or {}).get("code") == ("ACCOUNT_CASH_UNEXPLAINED" if unexplained else None)
    assert book.load()["loss_streak"] == 1 and D(book.load()["realised_usd"]) == D("-3.00")


def test_a_cash_loss_bigger_than_the_ledger_knew_is_still_debited_in_full(tmp_path):
    book = C.CapitalBook(str(tmp_path))
    book.ensure_baseline("100")
    assert book.park("s1", "100", "-3.00", True, True)
    r = book.settle("95")                                                      # the cash fell by 5: the larger loss is the truth
    assert D(r["credited_usd"]) == D("-5") and r["status"] == "UNEXPLAINED_CASH"


def test_when_funding_was_not_read_the_cash_delta_alone_decides_a_loss(tmp_path):
    book = C.CapitalBook(str(tmp_path))
    book.ensure_baseline("100")
    assert book.park("s1", "100", "-3.00", False, True)                         # funding unread: the ledger's -3.00 is a partial figure, not the whole truth
    r = book.settle("99")
    assert D(r["credited_usd"]) == D("-1") and r["status"] == "SETTLED"


def test_a_dust_sized_booked_loss_still_counts_toward_the_loss_streak_a_conservative_rule_only_the_owner_may_change(tmp_path):
    """Review finding 9 (LOW, KNOWN): the ledger saw a 2-cent loss with funding read while the cash rose 2 cents (rounding). The loss is booked in full and extends the streak, so four such sessions
    halt entries. That is spurious but CONSERVATIVE, and the loss-streak stop is a risk limit: loosening it (a materiality floor on the streak) is the owner's policy decision (the same class as the
    deferred dust/mechanics streak question), not a rounding fix to slip into a review round. Pinned so any change to it is deliberate and visible."""
    book = C.CapitalBook(str(tmp_path))
    book.ensure_baseline("1000")
    for i in range(C.LOSS_STREAK_STOP):
        assert book.park(f"s{i}", "1000", "-0.02", True, True)
        r = book.settle("1000.02")
        assert r["status"] == "SETTLED" and D(r["credited_usd"]) == D("-0.02"), r
    rep = book.report()
    assert rep["loss_streak"] == C.LOSS_STREAK_STOP and rep["halted"] is True and D(rep["verified_losses_usd"]) == D("-0.08")


# ---- a DECLARED transfer is the owner's unverified claim (red-team F3): it may explain cash that moved, never hide a loss the ledger measured and never raise a credit above the cash that rose
@pytest.mark.parametrize("funding_read", [False, True])
def test_a_declared_withdrawal_cannot_hide_a_loss_the_ledger_measured(tmp_path, funding_read):
    """The session lost 1.0017 (the ledger's price result minus fees); nobody withdrew anything. A declared 'withdrawal' of exactly that amount made the settled delta 0: status SETTLED, credited 0, streak 0,
    NO alert, with funding unread, and (funding read) an alert but still no debit. The measured loss is booked whatever was declared, and the book asks for a review."""
    b = book(tmp_path)
    b.ensure_baseline("10000")
    assert b.park("s1", "10000", "-1.0017", funding_read, True)
    assert b.declare_transfer("fake-wd", "-1.0017")
    r = b.settle("9998.9983")
    assert D(r["credited_usd"]) == D("-1.0017"), r
    assert r["status"] == "UNEXPLAINED_CASH" and (b.load()["review_required"] or {}).get("code") == "ACCOUNT_CASH_UNEXPLAINED", r
    assert b.load()["loss_streak"] == 1 and D(b.load()["realised_usd"]) == D("-1.0017")
    assert D(r["unverified_excess_usd"]) == D("1.0017"), "the part of the cash change the book cannot place stays visible as excess and sizes nothing"


def test_a_declared_withdrawal_cannot_raise_a_credit_above_the_cash_that_actually_rose(tmp_path):
    """Funding read, known +2.00, the wallet rose by 1.00; a declared 'withdrawal' of 1.00 turned that into a settled delta of +2.00 and the whole +2.00 was credited against a true +1.00."""
    b = book(tmp_path)
    b.ensure_baseline("10000")
    assert b.park("s1", "10000", "2.00", True, True)
    assert b.declare_transfer("fake-wd", "-1.00")
    r = b.settle("10001")
    assert D(r["credited_usd"]) == D("1.00") and b.basis() == D("10001.00"), r
    assert D(r["unverified_excess_usd"]) == D("1.00")


def test_a_withdrawal_the_owner_really_made_during_a_session_never_costs_more_than_the_known_loss_nor_credits_a_gain(tmp_path):
    """The same cap and floor are conservative for a REAL withdrawal. That is the NORMAL case, not a rare one: a withdrawal you declare between two sessions is counted against the session that is still pending
    (it was parked before you declared), so after every real withdrawal the previous session's gain shows as unverified excess instead of being credited (review finding 4)."""
    b = book(tmp_path)
    b.ensure_baseline("10000")
    assert b.park("s1", "10000", "-1.00", True, True)
    assert b.declare_transfer("wd", "-500")
    r = b.settle("9499")                                                          # -1.00 trading + the real -500 withdrawal: exactly what the ledger measured
    assert D(r["credited_usd"]) == D("-1.00") and r["status"] == "SETTLED", r
    assert b.park("s2", "9499", "0.50", True, True)
    assert b.declare_transfer("wd2", "-300")
    r = b.settle("9199.50")                                                       # +0.50 trading and a real -300 withdrawal: the cash fell, so the gain is not credited (visible as excess)
    assert D(r["credited_usd"]) == 0 and D(r["unverified_excess_usd"]) == D("0.50"), r


def test_a_deposit_declared_in_the_window_still_removes_exactly_the_deposit(tmp_path):
    b = book(tmp_path)
    b.ensure_baseline("10000")
    assert b.park("s1", "10000", "1.00", True, True)
    assert b.declare_transfer("dep", "500")
    r = b.settle("10501")
    assert D(r["credited_usd"]) == D("1.00") and r["status"] == "SETTLED" and r["transfers_removed_usd"] == "500", r


@pytest.mark.parametrize("amount", ["1e9", "-1e9", "1000001", "-1000001", "1e999999", "-1e999999", "NaN", "Infinity", "0", "", "abc"])
def test_an_absurd_or_unreadable_transfer_is_refused_and_leaves_the_book_untouched(tmp_path, amount):
    b = book(tmp_path)
    b.ensure_baseline("10000")
    before = b.load()
    assert b.declare_transfer("x", amount) is False
    assert b.load() == before


def test_a_withdrawal_larger_than_the_cash_the_book_knows_of_is_refused_but_a_deposit_is_not(tmp_path):
    b = book(tmp_path)
    b.ensure_baseline("10000")
    assert b.declare_transfer("wd-too-big", "-10000.01") is False and b.declare_transfer("wd", "-10000") is True
    assert b.declare_transfer("dep", "250000") is True                                    # a deposit has no cash to be larger than; only the absolute sanity bound applies
    assert b.declare_transfer("wd2", "-60000") is True, "the declared deposit is available to withdraw"
    assert b.declare_transfer("wd3", "-200000") is False


def test_the_declaration_bound_is_inclusive_at_the_limit(tmp_path):
    b = book(tmp_path)
    b.ensure_baseline("10000")
    assert b.declare_transfer("max-dep", str(C.MAX_DECLARED_TRANSFER_USD)) is True
    assert b.declare_transfer("over", str(C.MAX_DECLARED_TRANSFER_USD + D("0.01"))) is False


def test_a_declared_deposit_never_changes_how_a_loss_is_booked_with_funding_unread(tmp_path):
    """The masked-loss floor binds only a declared WITHDRAWAL (the claim that moves the delta up). A deposit leaves the existing rule alone: funding unread, the cash delta alone decides a loss."""
    b = book(tmp_path)
    b.ensure_baseline("10000")
    assert b.park("s1", "10000", "-1.00", False, True)                          # known -1.00 is partial (funding unread); the cash says the session lost only 0.50
    assert b.declare_transfer("dep", "100")
    r = b.settle("10099.50")
    assert D(r["credited_usd"]) == D("-0.50") and r["status"] == "SETTLED", r


# ---- a LIFETIME loss stop that no acknowledgement can lift (red-team F2: five rounds of "four losses, ack-halt, ack-drawdown" reached -60 realised and the next size was still 198.80)
def test_the_lifetime_loss_stop_is_twice_the_drawdown_stop_and_four_session_ceilings():
    import ms_testnet_adapter as AD
    assert C.LIFETIME_LOSS_STOP_USD == C.DRAWDOWN_STOP_USD * 2 == D(str(AD.MAX_LOSS_CEILING_USD)) * 4, "one definition: it cannot drift from the manifest's per-session ceiling"


@pytest.mark.parametrize("realised,stops", [("5", False), ("0", False), ("-11.10", False), ("-22.19", False), ("-22.20", True), ("-22.21", True), ("-60", True)])
def test_allocation_stops_at_the_lifetime_realised_loss(realised, stops):
    a = alloc(realised_usd=D(realised))
    assert (a.notional == 0 and a.binding == "LIFETIME_LOSS_STOP" and a.reasons[0] == "LIFETIME_LOSS_STOP") == stops, (a.binding, a.reasons)
    if not stops:
        assert a.notional > 0


def test_the_lifetime_stop_is_named_first_even_when_other_stops_are_also_open():
    a = alloc(realised_usd=D("-30"), halted=True, loss_streak=4, drawdown_usd=D("30"))
    assert a.binding == "LIFETIME_LOSS_STOP" and {"LIFETIME_LOSS_STOP", "LOSS_STREAK_STOP_NOT_ACKNOWLEDGED", "DRAWDOWN_LIMIT", "LOSS_STREAK_STOP"} <= set(a.reasons)


def test_repeated_acknowledgements_cannot_walk_past_the_lifetime_stop(tmp_path):
    b = book(tmp_path)
    b.ensure_baseline("10000")
    cash, n, rounds = D(10000), 0, 0
    while True:
        rounds += 1
        for _ in range(4):
            n += 1
            before, cash = cash, cash - D("1.50")
            assert b.park(f"s{n}", str(before), "-1.50", True, True)
            assert b.settle(str(cash))["status"] == "SETTLED"
        assert b.load()["halted"] is True
        cb = b.load()
        stopped = C.allocate(grant_max_usd=375, ceiling_usd=CEIL, open_gross_usd=0, available_usdt=cash, basis=b.sizing_basis(cb), start_cash=D(cb["start_cash_usd"]), loss_streak=cb["loss_streak"],
                             drawdown_usd=b.drawdown(cb), profit_seeking=True, halted=cb["halted"], realised_usd=D(cb["realised_usd"]))
        assert stopped.notional == 0, "four losses halt entries until acknowledged"
        assert b.acknowledge_halt() is True
        b.acknowledge_drawdown()
        cb = b.load()
        a = C.allocate(grant_max_usd=375, ceiling_usd=CEIL, open_gross_usd=0, available_usdt=cash, basis=b.sizing_basis(cb), start_cash=D(cb["start_cash_usd"]), loss_streak=cb["loss_streak"],
                       drawdown_usd=b.drawdown(cb), profit_seeking=True, halted=cb["halted"], realised_usd=D(cb["realised_usd"]))
        if a.notional == 0:
            break
        assert rounds < 10
    assert a.binding == "LIFETIME_LOSS_STOP" and rounds == 4 and D(b.load()["realised_usd"]) == D("-24.00")        # 4 rounds of -6.00: acknowledged three times, stopped on the fourth
    assert b.report()["lifetime_loss_stop_active"] is True and b.report()["lifetime_loss_stop_usd"] == str(C.LIFETIME_LOSS_STOP_USD)
    assert b.acknowledge_halt() is False and b.acknowledge_drawdown() is False and b.acknowledge_review() is False, "nothing left for an acknowledgement to clear: the stop is not one it can lift"


def test_a_gain_after_losses_lifts_the_lifetime_figure_it_is_a_net_realised_bound(tmp_path):
    b = book(tmp_path)
    b.ensure_baseline("10000")
    assert b.park("l1", "10000", "-20.00", True, True) and b.settle("9980")
    assert b.report()["lifetime_loss_stop_active"] is False
    assert b.park("l2", "9980", "-3.00", True, True) and b.settle("9977")
    assert b.report()["lifetime_loss_stop_active"] is True and D(b.load()["realised_usd"]) == D("-23.00")


# ---- a book that PARSES but holds nonsense is not a book (specialist E N1, REPRODUCED): ``_d`` reads "abc" or "NaN" as None/0, so a corrupted or tampered ``realised_usd`` made the basis, the drawdown check and the
# lifetime stop go silent (the allocator sized 200.00 where the honest book said LIFETIME_LOSS_STOP) and a string streak raised ValueError mid-session. Every figure the stops read is validated at load.
BAD_BOOK_FIELDS = [
    ("realised_usd", "abc"), ("realised_usd", "NaN"), ("realised_usd", "Infinity"), ("realised_usd", None), ("realised_usd", []), ("realised_usd", True),
    ("gains_usd", "x"), ("losses_usd", "x"), ("unverified_excess_usd", "x"),
    ("start_cash_usd", "abc"), ("start_cash_usd", "-5"), ("start_cash_usd", "0"),
    ("peak_basis_usd", "abc"), ("peak_basis_usd", "NaN"), ("last_cash_usdt", "abc"),
    ("reserve_fraction", "abc"), ("reserve_fraction", "0.95"), ("reserve_fraction", "-0.1"), ("reserve_fraction", None),
    ("loss_streak", "x"), ("loss_streak", -1), ("loss_streak", 1.5), ("loss_streak", True), ("loss_streak", None),
    ("halted", "false"), ("halted", 0), ("halted", None),
    ("history", "oops"), ("history", [{}]), ("history", [1]), ("history", [{"session_id": "s", "credited_usd": "abc"}]), ("history", [{"session_id": 5, "credited_usd": "1"}]),
    ("pending", "oops"), ("pending", {"session_id": "s"}), ("pending", {"session_id": 5, "usdt_before": "1"}), ("pending", {"session_id": "s", "usdt_before": "abc"}),
    ("transfers_pending", {}), ("transfers_pending", [{"event_id": "e", "amount_usd": "abc"}]), ("transfers_applied", [{"amount_usd": "1"}]),
    ("review_required", "yes"),
]


@pytest.mark.parametrize("field,bad", BAD_BOOK_FIELDS)
def test_a_book_holding_a_nonsense_figure_is_unreadable_and_left_as_evidence_not_a_book_with_no_stops(tmp_path, field, bad):
    import json
    b = book(tmp_path)
    b.ensure_baseline("10000")
    assert b.park("s1", "10000", "-30", True, True)
    b.settle("9970")                                                            # realised -30: the lifetime stop is active, the drawdown is 30
    doc = json.load(open(b.path))
    doc[field] = bad
    text = json.dumps(doc)
    with open(b.path, "w") as fh:
        fh.write(text)
    with pytest.raises(C.BookUnreadable) as exc:
        b.load()
    assert field in str(exc.value) and open(b.path).read() == text, "the message names the field, the file is untouched evidence"
    for call in (lambda: b.report(), lambda: b.park("s2", "9970", None, False, True), lambda: b.settle("9970"), lambda: b.acknowledge_halt(), lambda: b.declare_transfer("x", "1")):
        with pytest.raises(C.BookUnreadable):
            call()


def test_every_book_the_software_itself_writes_validates(tmp_path):
    """The validator must never refuse the book's own output: the whole lifecycle (baseline, park, settle with declared transfers, a review, acknowledgements, a reserve, a pending session)."""
    b = book(tmp_path)
    b.ensure_baseline("10000")
    assert b.park("s1", "10000", "0.50", True, True) and b.declare_transfer("dep", "100") and b.settle("10100.50")["status"] == "SETTLED"
    assert b.park("s2", "10100.50", "-1.00", True, False) and b.settle("10099.50") and b.set_reserve_fraction("0.5") and b.declare_transfer("wd", "-10")
    assert b.park("s3", "10099.50", "0.50", True, True) and b.settle("20000")["status"] == "UNEXPLAINED_CASH"
    assert b.acknowledge_review()
    assert b.park("l0", "20000", "-1.00", True, True) and b.settle("19999")
    assert b.park("pending-one", "19999", None, False, False)
    b.load()
    assert b.report()["pending"]["session_id"] == "pending-one"


def test_the_allocator_cannot_be_walked_past_a_stop_by_corrupting_the_realised_figure(tmp_path):
    """The reproduction: with realised_usd='abc' the honest LIFETIME_LOSS_STOP became a 200.00 allocation. Now the load refuses, so there is no allocation at all."""
    import json
    b = book(tmp_path)
    b.ensure_baseline("10000")
    assert b.park("s1", "10000", "-30", True, True)
    b.settle("9970")
    doc = json.load(open(b.path))
    doc["realised_usd"] = "abc"
    with open(b.path, "w") as fh:
        json.dump(doc, fh)
    with pytest.raises(C.BookUnreadable):
        b.sizing_basis()


def test_the_lifetime_stop_cannot_be_skipped_by_leaving_the_realised_figure_out_or_unreadable():
    """Review finding: ``realised_usd`` used to default to None and None skipped the stop, so a caller that forgot the argument (or passed an unreadable figure) silently had no lifetime stop. It is a REQUIRED
    argument now and an unknown figure is a refusal, like every other unknown input."""
    import inspect
    sig = inspect.signature(C.allocate)
    assert sig.parameters["realised_usd"].default is inspect.Parameter.empty, "a required keyword: no default to fall back on"
    base = dict(grant_max_usd=375, ceiling_usd=CEIL, open_gross_usd=0, available_usdt=10_000, basis=D(10_000), start_cash=D(10_000), loss_streak=0, drawdown_usd=D(0), profit_seeking=True)
    with pytest.raises(TypeError):
        C.allocate(**base)
    a = C.allocate(**base, realised_usd=None)
    assert a.notional == 0 and a.binding == "REALISED_UNKNOWN" and a.reasons[0] == "REALISED_UNKNOWN"
    assert C.allocate(**base, realised_usd=D(0)).notional > 0


def test_the_product_passes_the_realised_figure_to_the_allocator():
    """Structural: the one product call site of ``ms_capital.allocate`` names ``realised_usd`` (a required keyword, so it cannot be dropped silently, and this pins what it is fed from)."""
    import ast
    src = open(os.path.join(BOT, "tools", "ms_testnet_autopilot.py"), encoding="utf-8").read()
    calls = [n for n in ast.walk(ast.parse(src)) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "allocate" and getattr(n.func.value, "id", "") == "CC"]
    assert len(calls) == 1, "one product call site of the capital allocator"
    kw = {k.arg: ast.get_source_segment(src, k.value) for k in calls[0].keywords}
    assert kw.get("realised_usd") == 'CC._d(cb["realised_usd"])', kw


# ---- review round: edge cases of the declared-withdrawal rules
def test_a_not_flat_session_keeps_its_not_flat_label_when_a_withdrawal_is_declared_in_its_window(tmp_path):
    b = book(tmp_path)
    b.ensure_baseline("10000")
    assert b.park("s1", "10000", "0.50", True, False)                            # the ledger saw +0.50, but the session did NOT end verified flat
    assert b.declare_transfer("wd", "-300")
    r = b.settle("9700.50")
    assert r["status"] == "NOT_FLAT_SESSION" and D(r["credited_usd"]) == 0 and D(r["unverified_excess_usd"]) == D("0.50"), r


def test_a_declared_withdrawal_that_explains_a_cash_fall_the_ledger_could_not_measure_is_a_review_never_a_silent_settled(tmp_path):
    """Review (MEDIUM-LOW): with the ledger's net UNKNOWN (a fee in a currency nobody could value, say) a declared withdrawal equal to the cash fall settled as SETTLED, credited 0, streak 0, no review. The
    book cannot tell the withdrawal from a lost session it could not measure, so it asks for the owner's review (nothing is booked on a guess: a real withdrawal must not look like a trading loss)."""
    b = book(tmp_path)
    b.ensure_baseline("100")
    assert b.park("s1", "100", None, False, True)
    assert b.declare_transfer("wd", "-5")
    r = b.settle("95")                                                           # the cash fell by exactly the declared 5
    assert r["status"] == "UNEXPLAINED_CASH" and D(r["credited_usd"]) == 0 and (b.load()["review_required"] or {}).get("code") == "ACCOUNT_CASH_UNEXPLAINED", r
    assert b.load()["loss_streak"] == 0 and D(b.load()["realised_usd"]) == 0, "nothing is booked on a guess"


def test_with_the_ledger_net_unknown_a_withdrawal_declared_while_the_cash_rose_or_stayed_is_not_a_review(tmp_path):
    b = book(tmp_path)
    b.ensure_baseline("100")
    assert b.park("s1", "100", None, False, True)
    assert b.declare_transfer("wd", "-5")
    r = b.settle("100")                                                          # the cash did not fall: nothing to explain away
    assert r["status"] != "UNEXPLAINED_CASH" and not b.load()["review_required"], r


def test_two_declared_withdrawals_act_as_their_sum_and_a_deposit_offsets_them(tmp_path):
    b = book(tmp_path)
    b.ensure_baseline("10000")
    assert b.park("s1", "10000", "-1.00", True, True)
    assert b.declare_transfer("w1", "-3") and b.declare_transfer("w2", "-2")
    r = b.settle("9994.00")                                                      # -1 trading - 5 withdrawn
    assert D(r["credited_usd"]) == D("-1.00") and r["status"] == "SETTLED" and r["transfers_removed_usd"] == "-5", r
    assert b.park("s2", "9994", "-1.00", True, True)
    assert b.declare_transfer("w3", "-100") and b.declare_transfer("d1", "10")   # net -90: a NET withdrawal, so the loss floor binds
    r = b.settle("9904.00")                                                      # the cash fell by 90: the ledger's -1.00 is NOT what the cash says (-90 + 90 = 0)
    assert D(r["credited_usd"]) == D("-1.00") and r["status"] == "UNEXPLAINED_CASH", r
    assert b.park("s3", "9904", "-1.00", True, True)
    assert b.declare_transfer("w4", "-10") and b.declare_transfer("d2", "100")   # net +90: a deposit; the withdrawal rules do not apply
    r = b.settle("9993.00")
    assert D(r["credited_usd"]) == D("-1.00") and r["status"] == "SETTLED", r


def test_the_unknown_net_review_is_not_raised_for_dust_nor_when_a_gain_is_already_unverified(tmp_path):
    b = book(tmp_path)
    b.ensure_baseline("100")
    assert b.park("s1", "100", None, False, True)
    assert b.declare_transfer("dust", "-0.01")
    r = b.settle("100")                                                          # a one-cent withdrawal and an unchanged cash: dust, not a review
    assert r["status"] != "UNEXPLAINED_CASH" and not b.load()["review_required"], r
    assert b.park("s2", "100", None, False, True)
    assert b.declare_transfer("big", "-20")
    r = b.settle("95")                                                           # the cash fell 5 but 20 left: the book sees an unverified +15, which already sizes nothing
    assert r["status"] == "UNVERIFIED_GAIN" and D(r["credited_usd"]) == 0 and not b.load()["review_required"], r
