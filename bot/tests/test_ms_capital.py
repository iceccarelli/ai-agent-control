"""ms_capital: smallest-of-limits sizing and realised-only compounding. Offline, pure; the book lives in tmp_path (never bot/state)."""
import os
import sys
from decimal import Decimal as D

BOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [BOT, os.path.join(BOT, "tools")]
import ms_capital as C  # noqa: E402

CEIL = 500


def alloc(**kw):
    base = dict(grant_max_usd=375, ceiling_usd=CEIL, open_gross_usd=0, available_usdt=10_000, basis=D(10_000), start_cash=D(10_000), loss_streak=0, drawdown_usd=D(0), profit_seeking=True)
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
    a = alloc(available_usdt=116)                                           # 116 / 1.16 = 100
    assert a.notional == D("100.00") and a.binding == "MARGIN"


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
    assert D(b.settle("10299.50")["credited_usd"]) == D("0.50")              # withdrawal removed too


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


def test_a_corrupt_book_file_reads_as_empty_not_as_money(tmp_path):
    (tmp_path / "capital.json").write_text("{not json")
    b = book(tmp_path)
    assert b.basis() is None and alloc(basis=b.basis(), start_cash=None).notional == D(0)


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
    tools = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools")
    offenders = []
    for fn in sorted(os.listdir(tools)):
        if not fn.endswith(".py"):
            continue
        tree = ast.parse(open(os.path.join(tools, fn), encoding="utf-8").read())
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for sub in ast.walk(node):
                    if isinstance(sub, ast.Attribute) and sub.attr in ("acknowledge_halt", "acknowledge_drawdown") and not (fn == "ms_testnet_autopilot.py" and node.name == "main"):
                        offenders.append((fn, node.name, sub.attr))
    offenders = [o for o in offenders if o[0] != "ms_capital.py"]
    assert offenders == [], offenders
