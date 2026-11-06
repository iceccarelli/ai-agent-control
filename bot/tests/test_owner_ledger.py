"""tools/owner_ledger.py: the owner's complete P&L ledger, reconciled to account equity.

Every record here is SYNTHETIC. The perpetual legs come from the same generator the account-review tests use (the committed PUBLIC Bybit history
supplies the funding rates and bar opens, so the public-market checks have something to agree with). The account around them (balances, spot BTC,
deposits, interest) is built by hand in each test, with the expected numbers computed in the test from first principles (plain arithmetic written
out, not the ledger's own formulas), and each test then breaks exactly one thing and asserts that the ledger names it.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import random
import sys

import pytest

BOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BOT, "tools"))
sys.path.insert(0, BOT)

import account_review as AR  # noqa: E402
import owner_ledger as OL  # noqa: E402
from test_account_review import (K0, OPEN, PUB, TS, _fill, make_trip, _open_at, write_jsonl)  # noqa: E402,F401

HOUR = 3_600_000


# ---------------------------------------------------------------------------------------------------------------------------- the world

class World:
    """One round trip of 12 settlements (0.5 BTC short against 1.0 BTC owned) plus the account around it, with every expected number computed here."""

    def __init__(self, qty=0.5, btc0=1.0, usdt0=10_000.0, n=12):
        self.fills, self.funding, _ = make_trip(K0, n, qty=qty, b_in=2.0, b_out=0.5, fee_bps=5.5)
        self.qty = qty
        self.t_open = self.fills[0]["ts_ms"] - HOUR
        self.t_close = self.fills[1]["ts_ms"] + HOUR
        self.p0, self.p1 = _open_at(self.t_open), _open_at(self.t_close)
        self.usdt0, self.btc0 = usdt0, btc0
        self.spot, self.flows, self.lines, self.extra_snaps = [], [], [], []
        self.extra_files = []                                                  # (file name, rows, family): a second export that overlaps the first
        self.interest = 0.0
        # the expected perpetual numbers, by plain arithmetic
        self.sell, self.buy = self.fills[0]["price"], self.fills[1]["price"]
        self.realised = (self.sell - self.buy) * qty                          # a short gains when the buy-back is cheaper
        self.fees = self.fills[0]["fee_usd"] + self.fills[1]["fee_usd"]
        self.funding_sum = sum(r["payment_usd"] for r in self.funding)

    def snapshots(self, usdt1=None, btc1=None):
        s0 = {"snapshot_id": "open", "ts_ms": self.t_open, "usdt": self.usdt0, "btc": self.btc0, "btc_price": self.p0, "perp_upl_usd": 0.0,
              "perp_qty": 0.0, "margin_posted_usd": 0.0, "reserve_usd": 1000.0}
        if usdt1 is None:
            usdt1 = self.expected_usdt1()
        if btc1 is None:
            btc1 = self.expected_btc1()
        s1 = {"snapshot_id": "close", "ts_ms": self.t_close, "usdt": usdt1, "btc": btc1, "btc_price": self.p1, "perp_upl_usd": 0.0, "perp_qty": 0.0,
              "margin_posted_usd": 0.0, "reserve_usd": 1000.0}
        return [s0, s1] + self.extra_snaps

    def expected_usdt1(self):
        cash = self.usdt0 + self.realised - self.fees + self.funding_sum + self.interest
        cash += sum(f["amount"] for f in self.flows if f["coin"] == "USDT" and not f["kind"].startswith("INTERNAL"))
        for t in self.spot:
            cash += t["qty"] * t["price"] * (1 if t["side"] == "Sell" else -1)
            if t["fee_coin"] == "USDT":
                cash -= t["fee"]
        return cash

    def expected_btc1(self):
        q = self.btc0 + sum(f["amount"] for f in self.flows if f["coin"] == "BTC" and not f["kind"].startswith("INTERNAL"))
        for t in self.spot:
            q += t["qty"] * (1 if t["side"] == "Buy" else -1)
            if t["fee_coin"] == "BTC":
                q -= t["fee"]
        return q

    def write(self, tmp, **drop):
        files = []

        def put(name, rows, family):
            write_jsonl(tmp / name, rows)
            files.append({"family": family, "path": name})
        if self.fills:
            put("execs.jsonl", self.fills, "perp_executions")
        if self.funding:
            put("funding.jsonl", self.funding, "funding")
        if self.spot:
            put("spot.jsonl", self.spot, "spot_trades")
        if self.flows:
            put("flows.jsonl", self.flows, "external_flows")
        if self.lines:
            put("lines.jsonl", self.lines, "cash_lines")
        put("snaps.jsonl", drop.get("snapshots") or self.snapshots(), "snapshots")
        for name, rows, family in self.extra_files:
            put(name, rows, family)
        man = {"symbol": "BTCUSDT", "taker_bps": 5.5, "files": files}
        (tmp / "manifest.json").write_text(json.dumps(man))
        return str(tmp / "manifest.json")


def ledger(world, tmp, **kw):
    inp = OL.load_manifest(world.write(tmp, **{k: v for k, v in kw.items() if k == "snapshots"}))
    return OL.build(inp, **{k: v for k, v in kw.items() if k != "snapshots"})


def codes(rep, severity=None):
    return sorted(f["code"] for f in rep["flags"] if severity is None or f["severity"] == severity)


def hard(rep):
    return codes(rep, OL.SEVERITY_HARD)


# --------------------------------------------------------------------------------------------------------------------- the clean books

def test_a_clean_round_trip_reconciles_to_the_cent_and_the_bridge_matches_hand_arithmetic(tmp_path):
    w = World()
    rep = ledger(w, tmp_path)
    assert hard(rep) == [] and rep["residuals"]["equity_usd"] == pytest.approx(0.0, abs=1e-6)
    b = rep["equity_bridge"]
    btc_pnl = w.btc0 * (w.p1 - w.p0)                                            # 1.0 BTC held through the period
    assert b["perp_realised_usd"] == pytest.approx(w.realised)
    assert b["perp_fees_usd"] == pytest.approx(-w.fees)
    assert b["funding_net_usd"] == pytest.approx(w.funding_sum)
    assert b["btc_economic_pnl_usd"] == pytest.approx(btc_pnl)
    assert b["external_flows_usd"] == 0.0 and b["change_in_perp_unrealised_usd"] == 0.0
    e0 = w.usdt0 + w.btc0 * w.p0
    e1 = w.expected_usdt1() + w.expected_btc1() * w.p1
    assert b["opening_equity_usd"] == pytest.approx(e0) and b["closing_equity_usd"] == pytest.approx(e1)
    assert b["change_in_equity_usd"] == pytest.approx(w.realised - w.fees + w.funding_sum + btc_pnl)
    assert b["unexplained_usd"] == pytest.approx(0.0, abs=1e-6)


def test_the_verdict_without_a_statement_is_incomplete_and_never_account_verified(tmp_path):
    rep = ledger(World(), tmp_path)
    assert rep["verdict"] == "INCOMPLETE" and "NO_STATEMENT" in codes(rep, OL.SEVERITY_GAP)
    assert rep["authorizes_trading"] is False and rep["changes_any_status"] is False and rep["provenance"] == "OWNER_SUPPLIED_UNVERIFIED"
    assert "ACCOUNT_VERIFIED" not in json.dumps(rep) + OL.render(rep)
    assert "UNVERIFIED_BY_DESIGN" in rep["custody"]["authenticity"]


def _statement(w):
    """The account's own lines for a world, with a running cash balance: the independent record of every USDT movement."""
    rows, bal, n = [], w.usdt0, 0

    def add(ts, cat, change, ref=None):
        nonlocal bal, n
        n += 1
        bal += change
        rows.append({"line_id": f"L{n}", "ts_ms": ts, "coin": "USDT", "category": cat, "change": change, "cash_balance": bal, "ref_id": ref})
    add(w.fills[0]["ts_ms"], "FEE", -w.fills[0]["fee_usd"], w.fills[0]["exec_id"])
    for r in w.funding:
        add(r["ts_ms"], "FUNDING", r["payment_usd"], r["exec_id"])
    add(w.fills[1]["ts_ms"], "TRADE", w.realised, w.fills[1]["exec_id"])
    add(w.fills[1]["ts_ms"], "FEE", -w.fills[1]["fee_usd"], w.fills[1]["exec_id"])
    rows.sort(key=lambda r: (r["ts_ms"], r["line_id"]))
    return rows


def test_with_a_statement_that_agrees_the_ledger_reaches_the_top_rung_and_says_what_it_is(tmp_path):
    w = World()
    w.lines = _statement(w)
    rep = ledger(w, tmp_path)
    assert hard(rep) == [] and rep["residuals"]["usdt"] == pytest.approx(0.0, abs=1e-6)
    assert rep["statement"]["continuity_breaks"] == []
    assert rep["verdict"] in ("RECONCILED_BUT_UNCHECKED", "RECONCILED_NOT_AUTHENTICATED"), rep["flags"]
    assert rep["statement"]["fee_total"] == pytest.approx(w.fees) and rep["statement"]["funding_total"] == pytest.approx(w.funding_sum)


def test_the_strategy_contribution_is_exact_for_the_complete_round_trip_and_kept_apart_from_the_account(tmp_path):
    w = World()
    w.lines = _statement(w)
    rep = ledger(w, tmp_path)
    s = rep["strategy_contribution"]
    basis = w.qty * (w.sell - w.fills[0]["spot_ref"]) - w.qty * (w.buy - w.fills[1]["spot_ref"])          # hedge basis, by hand
    assert s["status"] == "COMPUTED" and s["complete_round_trips"] == 1
    assert s["funding_usd"] == pytest.approx(w.funding_sum) and s["fees_usd"] == pytest.approx(-w.fees) and s["basis_pnl_usd"] == pytest.approx(basis)
    assert s["net_usd"] == pytest.approx(w.funding_sum + basis - w.fees)
    assert s["funding_coverage"] == pytest.approx(1.0) and s["fee_coverage"] == pytest.approx(1.0)
    # the account moved by that PLUS the unhedged half of the BTC (0.5 BTC of the 1.0 owned is not hedged) and the perp/BTC legs net into the basis:
    assert rep["equity_bridge"]["change_in_equity_usd"] != pytest.approx(s["net_usd"])


def test_without_a_spot_reference_the_account_still_reconciles_but_the_strategy_basis_is_unknown_and_says_why(tmp_path):
    w = World()
    for f in w.fills:
        del f["spot_ref"]
    w.lines = _statement(w)
    rep = ledger(w, tmp_path)
    assert hard(rep) == [] and rep["residuals"]["usdt"] == pytest.approx(0.0, abs=1e-6)            # the bridge needs no spot reference
    assert rep["strategy_contribution"]["status"] == "UNKNOWN"
    assert "NO_SPOT_REFERENCE" in [l["code"] for l in rep["strategy_contribution"]["limitations"]]
    assert "NO_SPOT_REFERENCE" not in codes(rep)                                                    # a strategy limit, not an account-level gap


# --------------------------------------------------------------------------------------------------------------- the account around it

def test_a_usdt_deposit_is_not_profit_and_moves_the_money_weighted_return_by_its_weight(tmp_path):
    w = World()
    t_dep = w.t_open + (w.t_close - w.t_open) // 2
    w.flows = [{"flow_id": "D1", "ts_ms": t_dep, "coin": "USDT", "amount": 5_000.0, "kind": "DEPOSIT"}]
    rep = ledger(w, tmp_path)
    b = rep["equity_bridge"]
    assert b["external_flows_usd"] == 5_000.0 and hard(rep) == []
    assert b["unexplained_usd"] == pytest.approx(0.0, abs=1e-6)
    profit = b["change_in_equity_usd"] - 5_000.0                                  # what the operation earned: the deposit removed
    e0 = w.usdt0 + w.btc0 * w.p0
    wgt = (w.t_close - t_dep) / (w.t_close - w.t_open)
    md = rep["returns"]["account_money_weighted"]
    assert md["period_return"] == pytest.approx(profit / (e0 + wgt * 5_000.0))
    assert md["capital_base_usd"] == pytest.approx(e0 + wgt * 5_000.0)


def test_a_btc_deposit_needs_its_usd_value_or_the_ledger_says_it_cannot_value_the_capital(tmp_path):
    w = World()
    w.flows = [{"flow_id": "D2", "ts_ms": w.t_open + HOUR, "coin": "BTC", "amount": 0.25, "kind": "DEPOSIT"}]
    rep = ledger(w, tmp_path)
    assert "UNPRICED_BTC_FLOW" in codes(rep, OL.SEVERITY_GAP) and rep["verdict"] == "INCOMPLETE"
    w2 = World()
    price = _open_at(w2.t_open + HOUR)
    w2.flows = [{"flow_id": "D2", "ts_ms": w2.t_open + HOUR, "coin": "BTC", "amount": 0.25, "kind": "DEPOSIT", "usd_value": 0.25 * price}]
    ok = ledger(w2, tmp_path / "ok" if (tmp_path / "ok").mkdir() is None else tmp_path)
    assert hard(ok) == [] and "UNPRICED_BTC_FLOW" not in codes(ok)
    assert ok["equity_bridge"]["external_flows_usd"] == pytest.approx(0.25 * price)
    # the 0.25 BTC arrived at `price` and is held to the close at p1: the price move on it IS economic P&L, the deposit itself is not
    expected_btc = w2.btc0 * (w2.p1 - w2.p0) + 0.25 * (w2.p1 - price)
    assert ok["equity_bridge"]["btc_economic_pnl_usd"] == pytest.approx(expected_btc)
    assert ok["equity_bridge"]["unexplained_usd"] == pytest.approx(0.0, abs=1e-6)


def test_spot_buys_and_sells_with_fees_in_either_currency_reconcile(tmp_path):
    w = World()
    t1, t2 = w.t_open + 2 * HOUR, w.t_open + 5 * HOUR
    p1, p2 = _open_at(t1), _open_at(t2)
    w.spot = [{"trade_id": "S1", "ts_ms": t1, "symbol": "BTCUSDT", "side": "Buy", "qty": 0.2, "price": p1, "fee": 0.0002, "fee_coin": "BTC"},
              {"trade_id": "S2", "ts_ms": t2, "symbol": "BTCUSDT", "side": "Sell", "qty": 0.1, "price": p2, "fee": 0.06 * p2 / 100, "fee_coin": "USDT"}]
    rep = ledger(w, tmp_path)
    assert hard(rep) == [] and rep["residuals"]["btc_qty"] == pytest.approx(0.0, abs=1e-12) and rep["residuals"]["usdt"] == pytest.approx(0.0, abs=1e-6)
    cash_out = 0.2 * p1 - 0.1 * p2 + 0.06 * p2 / 100                              # net USDT spent on BTC, by hand
    v0, v1 = w.btc0 * w.p0, (w.btc0 + 0.2 - 0.0002 - 0.1) * w.p1
    assert rep["equity_bridge"]["btc_economic_pnl_usd"] == pytest.approx(v1 - v0 - cash_out)
    assert rep["equity_bridge"]["unexplained_usd"] == pytest.approx(0.0, abs=1e-6)


def test_interest_and_other_statement_lines_are_costs_of_the_operation_not_noise(tmp_path):
    w = World()
    w.interest = -12.5
    w.lines = _statement(w)
    w.lines.append({"line_id": "I1", "ts_ms": w.t_open + 4 * HOUR, "coin": "USDT", "category": "INTEREST", "change": -12.5, "cash_balance": None, "ref_id": None})
    rep = ledger(w, tmp_path)
    assert hard(rep) == [] and rep["equity_bridge"]["interest_usd"] == -12.5
    assert rep["equity_bridge"]["unexplained_usd"] == pytest.approx(0.0, abs=1e-6)


def test_an_internal_transfer_is_neither_income_nor_outflow_and_must_be_paired(tmp_path):
    w = World()
    w.flows = [{"flow_id": "T1", "ts_ms": w.t_open + HOUR, "coin": "USDT", "amount": -300.0, "kind": "INTERNAL_OUT", "transfer_id": "X"},
               {"flow_id": "T2", "ts_ms": w.t_open + HOUR, "coin": "USDT", "amount": 300.0, "kind": "INTERNAL_IN", "transfer_id": "X"}]
    rep = ledger(w, tmp_path)
    assert rep["equity_bridge"]["external_flows_usd"] == 0.0 and "UNPAIRED_INTERNAL_TRANSFER" not in codes(rep) and hard(rep) == []
    w2 = World()
    w2.flows = [w.flows[0]]
    (tmp_path / "u").mkdir()
    bad = ledger(w2, tmp_path / "u")
    assert "UNPAIRED_INTERNAL_TRANSFER" in codes(bad, OL.SEVERITY_GAP) and bad["verdict"] == "INCOMPLETE"


# ---------------------------------------------------------------------------------------------------------------- incomplete histories

def _first(rep, code):
    return next(f for f in rep["flags"] if f["code"] == code)


def test_a_missing_funding_settlement_is_named_and_leaves_a_residual_of_exactly_its_amount(tmp_path):
    full = World()
    snaps = full.snapshots()                                                      # the account really received ALL twelve settlements
    w = World()
    gone = w.funding.pop(4)                                                       # ... but the export of them has only eleven
    rep = ledger(w, tmp_path, snapshots=snaps, residual_tolerance_usd=0.01, residual_tolerance_rel=0.0)      # a strict tolerance: one settlement is a few dollars
    assert rep["residuals"]["usdt"] == pytest.approx(gone["payment_usd"], abs=1e-6)
    assert "PERP_MISSING_SETTLEMENT" in codes(rep, OL.SEVERITY_GAP) and rep["verdict"] == "INCOMPLETE"
    # open coverage gaps exist, so the unexplained cash is an incomplete history, not a contradiction between two complete records
    assert "EQUITY_RESIDUAL" in codes(rep, OL.SEVERITY_GAP) and "EQUITY_RESIDUAL" not in hard(rep)


def test_a_missing_execution_leaves_the_balances_unexplained_and_is_not_a_clean_pass(tmp_path):
    full = World()
    snaps = full.snapshots()
    w = World()
    w.fills = w.fills[:1]                                                         # the buy-back is absent from the file; the account did make it
    rep = ledger(w, tmp_path, snapshots=snaps)
    assert abs(rep["residuals"]["usdt"]) > 1.0
    assert rep["verdict"] in ("INCOMPLETE", "REJECTED_RECORDS_CONTRADICT")
    assert "EQUITY_RESIDUAL" in codes(rep)


def test_a_balance_residual_with_open_coverage_gaps_is_an_incomplete_history_and_with_none_a_contradiction(tmp_path):
    # BTC that appears in the account with no trade and no transfer behind it, while every coverage check is closed (a statement agrees with the records):
    # the records contradict the balances
    w = World()
    w.lines = _statement(w)
    snaps = w.snapshots()
    snaps[1]["btc"] += 0.01
    rep = ledger(w, tmp_path, snapshots=snaps)
    assert "EQUITY_RESIDUAL" in hard(rep) and rep["verdict"] == "REJECTED_RECORDS_CONTRADICT"
    assert rep["residuals"]["btc_qty"] == pytest.approx(0.01, abs=1e-9)
    # the same discrepancy with NO statement: coverage is open, so it is an incomplete history, never a contradiction
    w2 = World()
    snaps2 = w2.snapshots()
    snaps2[1]["btc"] += 0.01
    (tmp_path / "b").mkdir()
    rep2 = ledger(w2, tmp_path / "b", snapshots=snaps2)
    assert "EQUITY_RESIDUAL" in codes(rep2, OL.SEVERITY_GAP) and "EQUITY_RESIDUAL" not in hard(rep2) and rep2["verdict"] == "INCOMPLETE"


def test_a_statement_that_ends_somewhere_other_than_the_closing_snapshot_is_not_anchored_and_vouches_for_nothing(tmp_path):
    w = World()
    w.lines = _statement(w)
    snaps = w.snapshots()
    snaps[1]["usdt"] += 250.0                                                    # the snapshot shows 250 USD the statement's own chain does not reach
    rep = ledger(w, tmp_path, snapshots=snaps)
    assert "STATEMENT_NOT_ANCHORED" in codes(rep, OL.SEVERITY_GAP) and rep["verdict"] == "INCOMPLETE" and rep["statement"]["anchored"] is False
    assert rep["residuals"]["usdt"] == pytest.approx(250.0, abs=1e-6)


def test_identical_records_repeated_by_an_overlapping_export_are_dropped_and_counted(tmp_path):
    w = World()
    w.extra_files = [("execs2.jsonl", [dict(w.fills[0])], "perp_executions"), ("funding2.jsonl", [dict(w.funding[0]), dict(w.funding[1])], "funding")]
    rep = ledger(w, tmp_path)
    assert "DUPLICATE_IDENTICAL_DROPPED" in codes(rep, OL.SEVERITY_GAP) and hard(rep) == []
    assert rep["records"]["perp_executions"] == 2 and rep["records"]["funding_settlements"] == 12
    assert rep["equity_bridge"]["unexplained_usd"] == pytest.approx(0.0, abs=1e-6)          # counted once, not twice


def test_a_repeated_id_with_different_content_is_an_error_whether_across_files_or_inside_one(tmp_path):
    w = World()
    w.extra_files = [("execs2.jsonl", [dict(w.fills[0], price=w.fills[0]["price"] + 1.0)], "perp_executions")]
    with pytest.raises(OL.RecordError):
        OL.build(OL.load_manifest(w.write(tmp_path)))
    w2 = World()
    w2.fills = w2.fills + [dict(w2.fills[0])]                                   # the same id twice INSIDE one file: refused at the reader
    (tmp_path / "b").mkdir()
    with pytest.raises(OL.RecordError, match="duplicate"):
        OL.load_manifest(w2.write(tmp_path / "b"))


def test_a_sign_inverted_funding_export_is_a_contradiction_not_a_loss(tmp_path):
    w = World()
    for r in w.funding:
        r["payment_usd"] = -r["payment_usd"]
    rep = ledger(w, tmp_path)
    assert "PERP_SIGN_CONVENTION_INVERTED" in hard(rep) and rep["verdict"] == "REJECTED_RECORDS_CONTRADICT"


@pytest.mark.parametrize("family,field,value", [("perp_executions", "fee_currency", "BTC"), ("perp_executions", "fee_currency", "EUR"), ("funding", "currency", "BTC")])
def test_a_perpetual_record_in_the_wrong_currency_is_refused(tmp_path, family, field, value):
    w = World()
    rows = w.fills if family == "perp_executions" else w.funding
    rows[0][field] = value
    with pytest.raises(OL.RecordError, match="USDT"):
        OL.load_manifest(w.write(tmp_path))


def test_the_period_is_open_at_the_opening_snapshot_and_closed_at_the_closing_one(tmp_path):
    w = World()
    # a deposit stamped exactly at the opening snapshot is already inside the opening balance; one exactly at the close is inside the period
    w.flows = [{"flow_id": "A", "ts_ms": w.t_open, "coin": "USDT", "amount": 100.0, "kind": "DEPOSIT"},
               {"flow_id": "B", "ts_ms": w.t_close, "coin": "USDT", "amount": 40.0, "kind": "DEPOSIT"}]
    snaps = w.snapshots(usdt1=w.expected_usdt1() - 100.0)                        # expected_usdt1 counts both flows; A is already in the opening balance, so remove it
    rep = ledger(w, tmp_path, snapshots=snaps)
    assert rep["equity_bridge"]["external_flows_usd"] == 40.0 and "RECORDS_OUTSIDE_WINDOW" in codes(rep, OL.SEVERITY_GAP)
    assert any("flow:A" in e for e in _first(rep, "RECORDS_OUTSIDE_WINDOW")["examples"])
    assert rep["equity_bridge"]["unexplained_usd"] == pytest.approx(0.0, abs=1e-6)


def test_records_far_outside_the_window_are_ignored_and_listed_and_those_at_its_edge_are_a_boundary_gap(tmp_path):
    w = World()
    w.lines = _statement(w)
    old = dict(w.funding[0], exec_id="old", ts_ms=w.funding[0]["ts_ms"] - 30 * 24 * HOUR)       # an export wider than the period: normal
    w.funding = [old] + w.funding
    rep = ledger(w, tmp_path)
    assert rep["records"]["funding_settlements"] == 12
    sev = {f["code"]: f["severity"] for f in rep["flags"]}
    assert sev.get("RECORDS_BEYOND_WINDOW_IGNORED") == "INFO" and "RECORDS_OUTSIDE_WINDOW" not in sev
    assert rep["verdict"] == "RECONCILED_NOT_AUTHENTICATED"                                      # nothing else is wrong with this world
    # a record a few minutes on the wrong side of a snapshot is a boundary error, and blocks
    w2 = World()
    w2.lines = _statement(w2)
    w2.flows = [{"flow_id": "edge", "ts_ms": w2.t_open - 5 * 60_000, "coin": "USDT", "amount": 10.0, "kind": "DEPOSIT"}]
    (tmp_path / "b").mkdir()
    rep2 = ledger(w2, tmp_path / "b")
    assert "RECORDS_OUTSIDE_WINDOW" in codes(rep2, OL.SEVERITY_GAP) and rep2["verdict"] == "INCOMPLETE"


def test_a_snapshot_whose_stated_equity_disagrees_with_its_own_parts_is_a_contradiction(tmp_path):
    w = World()
    snaps = w.snapshots()
    snaps[1]["equity_usd"] = 12345.0
    rep = ledger(w, tmp_path, snapshots=snaps)
    assert "SNAPSHOT_INCONSISTENT" in hard(rep)


def test_missing_snapshots_leave_no_period_to_measure(tmp_path):
    w = World()
    one = w.snapshots()[:1]
    rep = ledger(w, tmp_path, snapshots=one)
    assert "NO_CLOSING_SNAPSHOT" in codes(rep, OL.SEVERITY_GAP) and rep["equity_bridge"] is None and rep["verdict"] == "INCOMPLETE"


def test_a_statement_with_a_break_in_its_running_balance_is_a_gap_and_names_where(tmp_path):
    w = World()
    w.lines = _statement(w)
    del w.lines[3]                                                                # one line removed: the running balance no longer follows
    rep = ledger(w, tmp_path)
    assert "STATEMENT_GAP" in codes(rep, OL.SEVERITY_GAP) and rep["statement"]["continuity_breaks"]


def test_statement_fees_that_do_not_match_the_executions_say_an_execution_is_missing_somewhere(tmp_path):
    w = World()
    w.lines = _statement(w)
    w.lines.append({"line_id": "X1", "ts_ms": w.t_open + 3 * HOUR, "coin": "USDT", "category": "FEE", "change": -9.5, "cash_balance": None, "ref_id": "ghost"})
    rep = ledger(w, tmp_path)
    assert "EXECUTION_NOT_IN_STATEMENT" in codes(rep, OL.SEVERITY_GAP)


def test_a_statement_deposit_with_no_flow_record_is_an_unattributed_cash_movement(tmp_path):
    w = World()
    w.lines = _statement(w)
    w.lines.append({"line_id": "E1", "ts_ms": w.t_open + 2 * HOUR, "coin": "USDT", "category": "EXTERNAL", "change": 700.0, "cash_balance": None, "ref_id": None})
    rep = ledger(w, tmp_path)
    assert "FLOW_NOT_IN_STATEMENT" in codes(rep, OL.SEVERITY_GAP) and rep["verdict"] == "INCOMPLETE"
    w2 = World()
    w2.flows = [{"flow_id": "D1", "ts_ms": w2.t_open + 2 * HOUR, "coin": "USDT", "amount": 700.0, "kind": "DEPOSIT"}]
    w2.lines = _statement(w2)                                                    # the flow record exists but the statement never shows it
    (tmp_path / "b").mkdir()
    rep2 = ledger(w2, tmp_path / "b")
    assert "FLOW_NOT_IN_STATEMENT" in codes(rep2, OL.SEVERITY_GAP)


def test_the_statements_realised_cash_must_equal_the_realised_pnl_the_executions_imply(tmp_path):
    w = World()
    w.lines = _statement(w)
    for l in w.lines:
        if l["category"] == "TRADE":
            l["change"] += 25.0                                                  # the venue says it realised 25 USD more than the fills imply
    rep = ledger(w, tmp_path)
    assert "REALISED_NOT_IN_STATEMENT" in codes(rep, OL.SEVERITY_GAP)
    good = World()
    good.lines = _statement(good)
    (tmp_path / "g").mkdir()
    assert "REALISED_NOT_IN_STATEMENT" not in codes(ledger(good, tmp_path / "g"))


def test_spot_cash_in_the_statement_must_match_the_spot_trades(tmp_path):
    w = World()
    t1 = w.t_open + 2 * HOUR
    p1 = _open_at(t1)
    w.spot = [{"trade_id": "S1", "ts_ms": t1, "symbol": "BTCUSDT", "side": "Buy", "qty": 0.2, "price": p1, "fee": 0.0002, "fee_coin": "BTC"}]
    w.lines = _statement(w) + [{"line_id": "P1", "ts_ms": t1, "coin": "USDT", "category": "SPOT", "change": -0.2 * p1, "cash_balance": None, "ref_id": "S1"},
                               {"line_id": "P2", "ts_ms": t1, "coin": "BTC", "category": "SPOT", "change": 0.2 - 0.0002, "cash_balance": None, "ref_id": "S1"}]
    rep = ledger(w, tmp_path)
    assert "SPOT_NOT_IN_STATEMENT" not in codes(rep), rep["flags"]
    w.lines[-1]["change"] = 0.1                                                  # the venue credited less BTC than the trade record says
    (tmp_path / "b").mkdir()
    assert "SPOT_NOT_IN_STATEMENT" in codes(ledger(w, tmp_path / "b"), OL.SEVERITY_GAP)


def test_other_statement_cash_is_never_silent(tmp_path):
    w = World()
    w.lines = _statement(w) + [{"line_id": "O1", "ts_ms": w.t_open + 3 * HOUR, "coin": "USDT", "category": "OTHER", "change": 3.0, "cash_balance": None, "ref_id": None}]
    w.interest = 3.0
    rep = ledger(w, tmp_path)
    assert "OTHER_CASH_LINES_PRESENT" in codes(rep, OL.SEVERITY_GAP) and rep["verdict"] == "INCOMPLETE"


def test_rows_stamped_in_the_same_millisecond_chain_in_whichever_order_fits_and_one_row_split_into_lines_chains_as_one(tmp_path):
    t = 1_700_000_000_000
    def L(i, ts, ch, bal, row=None, cat="FEE"):
        return OL.CashLine(f"l{i}", ts, "USDT", cat, ch, bal, None, f"s#{i}", row)
    # three rows in ONE millisecond; the file lists them in the wrong order for their balances (500 -> 490 -> 489 -> 489.5)
    rows = [L(1, t - 1000, 0.5, 500.0), L(2, t, -1.0, 489.0), L(3, t, -10.0, 490.0), L(4, t, 0.5, 489.5)]
    assert OL._chain_breaks(rows, "USDT") == []
    # one venue row (a TRADE with a cash flow and a fee) is two lines but ONE running balance: 489.5 + 20 - 0.1 = 509.4
    split = rows + [L(5, t + 10, 20.0, None, "r5", "TRADE"), L(6, t + 10, -0.1, 509.4, "r5", "FEE")]
    assert OL._chain_breaks(split, "USDT") == []
    broken = rows + [L(5, t + 10, 20.0, None, "r5", "TRADE"), L(6, t + 10, -0.1, 509.0, "r5", "FEE")]
    assert OL._chain_breaks(broken, "USDT") == ["USDT:l4->r5"]
    # a single defect is a single break, not a break at every later row
    later = broken + [L(7, t + 20, 1.0, 510.0)]
    assert len(OL._chain_breaks(later, "USDT")) == 1


def test_an_unknown_statement_category_blocks_a_conclusive_result(tmp_path):
    w = World()
    w.lines = _statement(w) + [{"line_id": "Z", "ts_ms": w.t_open + 3 * HOUR, "coin": "USDT", "category": "MYSTERY", "change": 5.0, "cash_balance": None, "ref_id": None}]
    rep = ledger(w, tmp_path)
    assert "UNCLASSIFIED_CASH_LINE" in codes(rep, OL.SEVERITY_GAP) and rep["verdict"] == "INCOMPLETE"


# ---------------------------------------------------------------------------------------- an open position at either end of the period

def test_a_position_still_open_at_the_close_is_unrealised_not_cash_and_ties_to_the_stated_upl(tmp_path):
    w = World()
    mark1 = _open_at(w.t_close)
    sell = w.fills[0]
    w.fills = w.fills[:1]
    w.funding_sum = sum(r["payment_usd"] for r in w.funding)
    upl = -w.qty * (mark1 - sell["price"])                                        # a short 0.5 BTC entered at the sell price, marked at the close
    cash = w.usdt0 - sell["fee_usd"] + w.funding_sum
    snaps = w.snapshots(usdt1=cash)
    snaps[1].update(perp_upl_usd=upl, perp_qty=-w.qty, perp_avg_entry=sell["price"], perp_mark=mark1)
    # the settlements up to the close must exist; make_trip's last settlement is before the close, so the rows cover the held period
    rep = ledger(w, tmp_path, snapshots=snaps)
    assert hard(rep) == [] and rep["residuals"]["perp_unrealised_usd"] == pytest.approx(0.0, abs=1e-6)
    assert rep["equity_bridge"]["perp_realised_usd"] == 0.0 and rep["equity_bridge"]["change_in_perp_unrealised_usd"] == pytest.approx(upl)
    assert rep["equity_bridge"]["unexplained_usd"] == pytest.approx(0.0, abs=1e-6)


def test_a_position_carried_in_is_priced_from_its_stated_entry_and_its_settlements_are_owed(tmp_path):
    w = World()
    sell = w.fills[0]
    w.fills = w.fills[1:]                                                         # only the buy-back is in the period
    mark0 = _open_at(w.t_open)
    upl0 = -w.qty * (mark0 - sell["price"])
    realised = (sell["price"] - w.fills[0]["price"]) * w.qty                       # entered before the period at sell["price"], closed inside it
    fees = w.fills[0]["fee_usd"]
    cash1 = w.usdt0 + realised - fees + w.funding_sum
    snaps = w.snapshots(usdt1=cash1)
    snaps[0].update(perp_upl_usd=upl0, perp_qty=-w.qty, perp_avg_entry=sell["price"], perp_mark=mark0)
    w.fills_write = w.fills
    rep = ledger(w, tmp_path, snapshots=snaps)
    assert hard(rep) == [], rep["flags"]
    assert rep["equity_bridge"]["perp_realised_usd"] == pytest.approx(realised)
    assert rep["equity_bridge"]["change_in_perp_unrealised_usd"] == pytest.approx(-upl0)
    assert rep["equity_bridge"]["unexplained_usd"] == pytest.approx(0.0, abs=1e-6)
    assert "CARRIED_IN_POSITION" in codes(rep) and rep["perp_review"]["settlements"]["missing"] == 0


def test_an_opening_snapshot_that_does_not_state_the_position_cannot_price_a_carried_one(tmp_path):
    w = World()
    snaps = w.snapshots()
    del snaps[0]["perp_qty"]
    rep = ledger(w, tmp_path, snapshots=snaps)
    assert "OPENING_POSITION_UNKNOWN" in codes(rep, OL.SEVERITY_GAP) and rep["verdict"] == "INCOMPLETE"


# ---------------------------------------------------------------------------------------------------------- the venue's averaging convention

def _fill_obj(i, side, qty, price, ts=None):
    return AR.Fill(f"e{i}", ts or (1_700_000_000_000 + i * 1000), side, qty, price, 0.0, "taker", None, None)


def test_average_entry_scaling_in_and_out_by_hand():
    # short 1 @ 100, short 1 more @ 110 -> short 2 @ avg 105; buy 0.5 @ 100 -> realised (105-100) x 0.5 = 2.5, still short 1.5 @ 105
    r = OL.perp_average_entry([_fill_obj(1, "Sell", 1.0, 100.0), _fill_obj(2, "Sell", 1.0, 110.0), _fill_obj(3, "Buy", 0.5, 100.0)])
    assert r["qty"] == pytest.approx(-1.5) and r["avg_entry"] == pytest.approx(105.0) and r["realised_usd"] == pytest.approx(2.5)
    assert r["cash_usd"] == pytest.approx(100.0 + 110.0 - 50.0)
    # then buy the remaining 1.5 @ 120: realised += (105-120) x 1.5 = -22.5 -> total -20.0, flat
    r2 = OL.perp_average_entry([_fill_obj(1, "Sell", 1.0, 100.0), _fill_obj(2, "Sell", 1.0, 110.0), _fill_obj(3, "Buy", 0.5, 100.0), _fill_obj(4, "Buy", 1.5, 120.0)])
    assert r2["qty"] == 0.0 and r2["avg_entry"] == 0.0 and r2["realised_usd"] == pytest.approx(-20.0)


def test_average_entry_crossing_zero_realises_the_old_side_and_opens_the_rest_at_the_fill_price():
    # short 1 @ 100, buy 1.5 @ 90: realised (100-90) x 1 = 10, then long 0.5 @ 90
    r = OL.perp_average_entry([_fill_obj(1, "Sell", 1.0, 100.0), _fill_obj(2, "Buy", 1.5, 90.0)])
    assert r["realised_usd"] == pytest.approx(10.0) and r["qty"] == pytest.approx(0.5) and r["avg_entry"] == pytest.approx(90.0)
    # a long: buy 2 @ 50, sell 1 @ 60 -> realised +10, long 1 @ 50
    r2 = OL.perp_average_entry([_fill_obj(1, "Buy", 2.0, 50.0), _fill_obj(2, "Sell", 1.0, 60.0)])
    assert r2["realised_usd"] == pytest.approx(10.0) and r2["qty"] == pytest.approx(1.0) and r2["avg_entry"] == pytest.approx(50.0)


def test_total_perp_pnl_is_independent_of_the_realised_unrealised_split():
    """realised + unrealised == cash from fills + final qty x mark - opening qty x opening mark, whatever the averaging does (the identity the
    bridge relies on)."""
    rng = random.Random(4)
    for k in range(200):
        q0 = rng.choice([0.0, -1.0, -2.5])
        a0 = rng.uniform(50, 150) if q0 else None
        m0 = rng.uniform(50, 150)
        fills = [_fill_obj(i, rng.choice(["Buy", "Sell"]), round(rng.uniform(0.1, 2.0), 3), rng.uniform(50, 150)) for i in range(rng.randint(0, 6))]
        m1 = rng.uniform(50, 150)
        r = OL.perp_average_entry(fills, q0, a0)
        upl0 = q0 * (m0 - a0) if q0 else 0.0
        upl1 = r["qty"] * (m1 - r["avg_entry"]) if r["qty"] else 0.0
        total_a = r["realised_usd"] + upl1 - upl0
        total_b = r["cash_usd"] + r["qty"] * m1 - q0 * m0
        assert total_a == pytest.approx(total_b, abs=1e-9), (k, q0, fills)


def test_average_entry_agrees_with_the_account_review_round_trip_pnl():
    f, g, _ = make_trip(K0, 12, qty=0.7)
    fills, _ = AR.parse_fills(f, "BTCUSDT")
    funding, _ = AR.parse_funding(g, "BTCUSDT")
    rep = AR.review(fills, funding, taker_bps=5.5, public=PUB, bar_open=OPEN)
    assert OL.perp_average_entry(fills)["realised_usd"] == pytest.approx(rep["round_trips"][0]["perp_pnl_usd"])


def test_an_opening_position_without_its_average_entry_is_an_error_not_a_guess():
    with pytest.raises(OL.RecordError, match="average entry"):
        OL.perp_average_entry([_fill_obj(1, "Buy", 1.0, 100.0)], -1.0, None)


# ----------------------------------------------------------------------------------------------------------------------------- returns

def test_modified_dietz_by_hand_and_the_irr_agrees_when_there_are_no_flows():
    t0, t1 = 1_700_000_000_000, 1_700_000_000_000 + 100 * 86_400_000
    # E0 = 1000, E1 = 1150, a deposit of 100 exactly half way: return = (1150 - 1000 - 100) / (1000 + 0.5 x 100) = 50 / 1050
    md = OL.modified_dietz(1000.0, 1150.0, [(t0 + 50 * 86_400_000, 100.0)], t0, t1)
    assert md["period_return"] == pytest.approx(50.0 / 1050.0) and md["capital_base_usd"] == pytest.approx(1050.0)
    r = OL.irr(1000.0, 1100.0, [], t0, t1)                                      # +10 % over 100 days -> (1.1)^(365/100) - 1
    assert r["annual_rate"] == pytest.approx(1.1 ** (365.0 / 100.0) - 1.0, rel=1e-6)
    assert OL.modified_dietz(0.0, 10.0, [], t0, t1) is None
    assert OL.modified_dietz(100.0, 110.0, [], t1, t0) is None


def test_linked_returns_need_three_snapshots_and_chain_the_sub_periods():
    def snap(i, ts, e):
        return OL.Snapshot(f"s{i}", ts, e, 0.0, 60000.0, 0.0, None, None, None, None, 0.0, None, None, "t")
    base = 1_700_000_000_000
    s = [snap(0, base, 1000.0), snap(1, base + 10 * 86_400_000, 1100.0), snap(2, base + 20 * 86_400_000, 1210.0)]
    out = OL.linked_returns(s, [])
    assert out["period_return"] == pytest.approx(0.21) and out["sub_periods"] == 2
    assert OL.linked_returns(s[:2], []) is None


# -------------------------------------------------------------------------------------------------------------- no live record, no claim

def test_with_no_perpetual_activity_the_answer_is_that_realised_pnl_is_not_established(tmp_path):
    w = World()
    w.fills, w.funding = [], []                                                   # the owner never traded the perpetual: only balances exist
    rep = ledger(w, tmp_path, snapshots=w.snapshots(usdt1=w.usdt0, btc1=w.btc0))
    assert rep["verdict"] == "NO_TRADING_RECORDS" and "NOT been established" in rep["conclusion"]
    assert rep["records"]["perp_executions"] == 0 and rep["strategy_contribution"]["status"] == "NOT_COMPUTED"
    assert rep["authorizes_trading"] is False
    # the BTC the owner holds still moves the account: that is price exposure, reported, but it is not the strategy's P&L
    assert rep["equity_bridge"]["btc_economic_pnl_usd"] == pytest.approx(w.btc0 * (w.p1 - w.p0))
    assert rep["equity_bridge"]["perp_realised_usd"] == 0.0 and rep["equity_bridge"]["funding_net_usd"] == 0.0


# ------------------------------------------------------------------------------------------------------------ the manifest and the files

def test_a_manifest_cannot_point_the_reader_outside_its_own_directory(tmp_path):
    w = World()
    man = json.loads(open(w.write(tmp_path)).read())
    man["files"][0]["path"] = "../elsewhere.jsonl"
    (tmp_path / "manifest.json").write_text(json.dumps(man))
    with pytest.raises(OL.RecordError, match="outside the manifest's directory"):
        OL.load_manifest(str(tmp_path / "manifest.json"))
    man["files"][0]["path"] = "execs.jsonl"
    man["files"].append(dict(man["files"][0]))
    (tmp_path / "manifest.json").write_text(json.dumps(man))
    with pytest.raises(OL.RecordError, match="listed twice"):
        OL.load_manifest(str(tmp_path / "manifest.json"))


@pytest.mark.parametrize("bad", [{"files": []}, {"files": [1]}, {"files": [{"family": "nope", "path": "x"}]}, [], {"symbol": "ETHUSDT", "files": [{"family": "snapshots", "path": "s"}]}])
def test_a_malformed_manifest_is_refused(tmp_path, bad):
    (tmp_path / "manifest.json").write_text(json.dumps(bad))
    with pytest.raises(OL.RecordError):
        OL.load_manifest(str(tmp_path / "manifest.json"))


def test_protected_names_are_refused_as_manifest_inputs(tmp_path):
    w = World()
    man_path = w.write(tmp_path)
    os.rename(tmp_path / "snaps.jsonl", tmp_path / "trading_state.db.jsonl")
    man = json.loads(open(man_path).read())
    for e in man["files"]:
        if e["family"] == "snapshots":
            e["path"] = "trading_state.db.jsonl"
    (tmp_path / "manifest.json").write_text(json.dumps(man))
    with pytest.raises(OL.RecordError, match="refusing to read"):
        OL.load_manifest(str(tmp_path / "manifest.json"))


def test_the_column_map_converts_a_foreign_export_deterministically_and_keeps_every_input_digest(tmp_path):
    w = World()
    man_path = w.write(tmp_path)
    man = json.loads(open(man_path).read())
    # re-express the executions in a foreign layout (seconds, upper-case sides, 'Fee' paid as a positive number, a text column the ledger never reads)
    foreign = [{"ExecId": r["exec_id"], "Time": r["ts_ms"] / 1000.0, "Side": r["side"].upper(), "Qty": r["qty"], "Px": r["price"], "Fee": r["fee_usd"],
                "Liq": r["liquidity"].upper(), "Ref": r["spot_ref"], "Note": "x"} for r in w.fills]
    write_jsonl(tmp_path / "foreign.jsonl", foreign)
    for e in man["files"]:
        if e["family"] == "perp_executions":
            e.update(path="foreign.jsonl", constants={"symbol": "BTCUSDT"}, column_map={
                "exec_id": "ExecId", "ts_ms": {"column": "Time", "transform": "epoch_s_to_ms"},
                "side": {"column": "Side", "transform": 'map:{"BUY":"Buy","SELL":"Sell"}'}, "qty": "Qty", "price": "Px", "fee_usd": "Fee",
                "liquidity": {"column": "Liq", "transform": 'map:{"TAKER":"taker","MAKER":"maker"}'}, "spot_ref": "Ref"})
    (tmp_path / "manifest.json").write_text(json.dumps(man))
    a = OL.load_manifest(str(tmp_path / "manifest.json"))
    assert [(f.exec_id, f.ts_ms, f.side, f.qty, f.price, f.fee_usd, f.liquidity) for f in a.fills] == \
        [(r["exec_id"], r["ts_ms"], r["side"], r["qty"], r["price"], r["fee_usd"], r["liquidity"]) for r in w.fills]
    b = OL.load_manifest(str(tmp_path / "manifest.json"))
    assert a.digests == b.digests and any(k.startswith("perp_executions:foreign.jsonl") for k in a.digests)
    with open(tmp_path / "foreign.jsonl", "rb") as fh:
        assert a.digests["perp_executions:foreign.jsonl"] == hashlib.sha256(fh.read()).hexdigest()


def test_a_mapping_never_guesses_a_missing_column_a_bad_transform_or_a_local_time(tmp_path):
    rows = [{"A": "x", "T": "2026-11-05T10:00:00", "S": "weird"}]
    with pytest.raises(OL.RecordError, match="not in the file"):
        OL.apply_column_map(rows, {"exec_id": "Missing"}, None, "f")
    with pytest.raises(OL.RecordError, match="explicit UTC marker"):
        OL.apply_column_map(rows, {"ts_ms": {"column": "T", "transform": "iso_utc_to_ms"}}, None, "f")
    with pytest.raises(OL.RecordError, match="not in the declared map"):
        OL.apply_column_map(rows, {"side": {"column": "S", "transform": 'map:{"BUY":"Buy"}'}}, None, "f")
    with pytest.raises(OL.RecordError, match="unknown transform"):
        OL.apply_column_map(rows, {"side": {"column": "S", "transform": "shout"}}, None, "f")
    ok = OL.apply_column_map([{"T": "2026-11-05T10:00:00Z"}], {"ts_ms": {"column": "T", "transform": "iso_utc_to_ms"}}, None, "f")
    assert ok == [{"ts_ms": 1_793_872_800_000}]
    assert OL.apply_column_map([{"V": "5"}], {"v": {"column": "V", "transform": ["negate", "scale:2"]}}, None, "f") == [{"v": -10.0}]


# -------------------------------------------------------------------------------------------------------------- hostile input and purity

@pytest.mark.parametrize("row", [
    {"snapshot_id": "x", "ts_ms": 1_700_000_000_000, "usdt": float("nan"), "btc": 1, "btc_price": 1, "perp_upl_usd": 0},
    {"snapshot_id": "x", "ts_ms": 1_700_000_000_000, "usdt": 1, "btc": 10 ** 400, "btc_price": 1, "perp_upl_usd": 0},
    {"snapshot_id": "x", "ts_ms": 1_700_000_000_000, "usdt": 1, "btc": 1, "btc_price": 0, "perp_upl_usd": 0},
    {"snapshot_id": "x", "ts_ms": 1_700_000_000_000, "usdt": True, "btc": 1, "btc_price": 1, "perp_upl_usd": 0},
    {"snapshot_id": "", "ts_ms": 1_700_000_000_000, "usdt": 1, "btc": 1, "btc_price": 1, "perp_upl_usd": 0}])
def test_malformed_snapshots_are_errors(row):
    with pytest.raises(OL.RecordError):
        OL.parse_snapshots([row])


@pytest.mark.parametrize("row", [
    {"trade_id": "t", "ts_ms": 1_700_000_000_000, "symbol": "BTCUSDT", "side": "Buy", "qty": 1, "price": 1, "fee": 0, "fee_coin": "EUR"},
    {"trade_id": "t", "ts_ms": 1_700_000_000_000, "symbol": "ETHUSDT", "side": "Buy", "qty": 1, "price": 1, "fee": 0, "fee_coin": "BTC"},
    {"trade_id": "t", "ts_ms": 1_700_000_000_000, "symbol": "BTCUSDT", "side": "buy", "qty": 1, "price": 1, "fee": 0, "fee_coin": "BTC"},
    {"trade_id": "t", "ts_ms": 1_700_000_000_000, "symbol": "BTCUSDT", "side": "Buy", "qty": 1, "price": 100, "fee": 50, "fee_coin": "USDT"},
    {"trade_id": "t", "ts_ms": 1_700_000_000_000, "symbol": "BTCUSDT", "side": "Buy", "qty": 0, "price": 1, "fee": 0, "fee_coin": "BTC"}])
def test_malformed_spot_trades_are_errors(row):
    with pytest.raises(OL.RecordError):
        OL.parse_spot_trades([row])


@pytest.mark.parametrize("row", [
    {"flow_id": "f", "ts_ms": 1_700_000_000_000, "coin": "EUR", "amount": 1, "kind": "DEPOSIT"},
    {"flow_id": "f", "ts_ms": 1_700_000_000_000, "coin": "USDT", "amount": -5, "kind": "DEPOSIT"},
    {"flow_id": "f", "ts_ms": 1_700_000_000_000, "coin": "USDT", "amount": 5, "kind": "WITHDRAWAL"},
    {"flow_id": "f", "ts_ms": 1_700_000_000_000, "coin": "USDT", "amount": 0, "kind": "DEPOSIT"},
    {"flow_id": "f", "ts_ms": 1_700_000_000_000, "coin": "USDT", "amount": 5, "kind": "DEPOSIT", "usd_value": 7},
    {"flow_id": "f", "ts_ms": 1_700_000_000_000, "coin": "BTC", "amount": 1, "kind": "DEPOSIT", "usd_value": -5}])
def test_malformed_external_flows_are_errors(row):
    with pytest.raises(OL.RecordError):
        OL.parse_external_flows([row])


def test_a_fuzz_of_mutated_worlds_never_reaches_a_reconciled_verdict_and_the_unmutated_world_does(tmp_path):
    rng = random.Random(9)
    base = World()
    base.lines = _statement(base)
    (tmp_path / "ctl").mkdir()
    control = ledger(base, tmp_path / "ctl")
    assert control["verdict"] == "RECONCILED_NOT_AUTHENTICATED", control["flags"]              # POSITIVE CONTROL: the clean world does reach the top rung
    snaps_ok = base.snapshots()
    n_run = 0
    for k in range(80):
        w = World()
        d = tmp_path / f"f{k}"
        d.mkdir()
        w.lines = _statement(w) if rng.random() < 0.6 else []
        mut = rng.choice(["drop_fill", "drop_funding", "dup_fill", "flip_sign", "shift_far", "zero_fee", "inflate_fee"])
        if mut == "drop_fill":
            w.fills.pop(rng.randrange(len(w.fills)))
        elif mut == "drop_funding":
            w.funding.pop(rng.randrange(len(w.funding)))
        elif mut == "dup_fill":
            w.extra_files = [("execs2.jsonl", [dict(w.fills[0], price=w.fills[0]["price"] * 1.001)], "perp_executions")]       # same id, different content
        elif mut == "flip_sign":
            w.funding[rng.randrange(len(w.funding))]["payment_usd"] *= -1
        elif mut == "shift_far":
            w.fills[1]["ts_ms"] += 3 * HOUR
        elif mut == "zero_fee":
            w.fills[0]["fee_usd"] = 0.0
        elif mut == "inflate_fee":
            w.fills[1]["fee_usd"] += 3.0
        try:
            rep = ledger(w, d, snapshots=copy.deepcopy(snaps_ok))
        except OL.RecordError:
            continue
        n_run += 1
        assert not rep["verdict"].startswith("RECONCILED"), (k, mut, [(f["code"], f["severity"]) for f in rep["flags"]])
    assert n_run >= 40                                                                          # most mutants load and are judged; they are not all refused at the door


def test_a_fill_time_wrong_by_seconds_that_moves_no_money_is_not_detectable_and_the_ledger_says_so_in_its_limits(tmp_path):
    """A STATED LIMIT, pinned so it cannot be forgotten: the checks are on totals and on settlement ownership, not on each record's time. Moving the closing
    fill by 1 ms or a minute changes no cash and no settlement the book was in, so nothing contradicts it."""
    w = World()
    w.lines = _statement(w)
    w.fills[1]["ts_ms"] += 61_000
    for l in w.lines:
        if l["ts_ms"] == w.fills[1]["ts_ms"] - 61_000:
            l["ts_ms"] += 61_000
    rep = ledger(w, tmp_path)
    assert rep["verdict"] == "RECONCILED_NOT_AUTHENTICATED"
    assert any("TOTALS" in lim and "not a match of each execution" in lim for lim in rep["limits"])
    assert "WHAT THIS DOES NOT ESTABLISH" in OL.render(rep)


def test_the_module_reads_files_only_and_imports_nothing_that_can_reach_a_network_a_database_or_a_process():
    import ast
    tree = ast.parse(open(OL.__file__, encoding="utf-8").read())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    allowed = {"__future__", "argparse", "bisect", "datetime", "json", "math", "os", "sys", "dataclasses", "typing", "itertools", "account_review", "bybit_v5_records"}
    assert imported <= allowed, sorted(imported - allowed)
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)]
    direct = {n.func.attr for n in calls if isinstance(n.func, ast.Attribute) and isinstance(n.func.value, ast.Name) and n.func.value.id == "os"}
    assert direct <= {"path"} or direct == set(), direct
    for n in calls:
        if isinstance(n.func, ast.Name) and n.func.id == "open":
            raise AssertionError("owner_ledger must read through account_review's guarded readers, never open() a path itself")


def test_a_build_writes_nothing_beside_its_inputs(tmp_path):
    w = World()
    man = w.write(tmp_path)

    def snap():
        out = {}
        for d, _dirs, files in os.walk(tmp_path):
            for f in sorted(files):
                p = os.path.join(d, f)
                with open(p, "rb") as fh:
                    out[os.path.relpath(p, tmp_path)] = hashlib.sha256(fh.read()).hexdigest()
        return out
    before = snap()
    OL.build(OL.load_manifest(man))
    assert OL.main(["--manifest", man, "--json"]) in (0, 2)
    assert snap() == before


def test_the_command_line_prints_text_and_strict_json_and_a_message_on_error(tmp_path, capsys):
    w = World()
    w.lines = _statement(w)
    man = w.write(tmp_path)
    assert OL.main(["--manifest", man]) == 0
    text = capsys.readouterr().out
    assert "OWNER P&L LEDGER" in text and "EQUITY BRIDGE" in text and "UNVERIFIED_BY_DESIGN" in text
    assert OL.main(["--manifest", man, "--json", "--alternative-return-pct", "4.5"]) == 0

    def refuse(c):
        raise AssertionError(f"non-JSON constant {c}")
    doc = json.loads(capsys.readouterr().out, parse_constant=refuse)
    assert doc["authorizes_trading"] is False and doc["returns"]["alternative_return_pct"] == 4.5
    assert OL.main(["--manifest", str(tmp_path / "missing.json")]) == 2
    assert "owner_ledger:" in capsys.readouterr().err


# ============================================================================================ regressions from the independent review
# Each of these FAILED against the first version of the ledger (a reconciled verdict, or a headline return, that the records did not support).

def _clean_with_statement(tmp_path, **mod):
    w = World()
    w.lines = _statement(w)
    return w, ledger(w, tmp_path, **mod)


def test_value_held_outside_the_modelled_assets_is_a_bridge_line_and_a_gap_never_a_balancing_item(tmp_path):
    w = World()
    w.lines = _statement(w)
    snaps = w.snapshots()
    snaps[1]["other_usd"] = 3000.0                                             # 3,000 USD of something the ledger does not model appeared by the close
    rep = ledger(w, tmp_path, snapshots=snaps)
    assert "OTHER_ASSETS_PRESENT" in codes(rep, OL.SEVERITY_GAP) and not rep["verdict"].startswith("RECONCILED")
    assert rep["equity_bridge"]["change_in_other_assets_usd"] == 3000.0 and rep["equity_bridge"]["unexplained_usd"] == pytest.approx(0.0, abs=1e-6)


def test_an_unexplained_bridge_residual_blocks_the_verdict_even_when_the_components_balance(tmp_path):
    w = World()
    w.lines = _statement(w)
    snaps = w.snapshots()
    snaps[1]["equity_usd"] = None
    rep = ledger(w, tmp_path, snapshots=snaps)
    assert "BRIDGE_UNEXPLAINED" not in codes(rep) and rep["verdict"] == "RECONCILED_NOT_AUTHENTICATED"                 # the clean case has none
    # an opening unrealised P&L that its own position does not produce moves equity without a trade: it is a snapshot that contradicts itself
    snaps2 = w.snapshots()
    snaps2[0]["perp_upl_usd"] = -3000.0
    (tmp_path / "b").mkdir()
    rep2 = ledger(w, tmp_path / "b", snapshots=snaps2)
    assert "SNAPSHOT_INCONSISTENT" in hard(rep2) and rep2["verdict"] == "REJECTED_RECORDS_CONTRADICT"


def test_a_stated_unrealised_pnl_must_equal_the_position_it_belongs_to_at_either_end(tmp_path):
    w = World()
    sell = w.fills[0]
    w.fills = w.fills[:1]
    w.funding_sum = sum(r["payment_usd"] for r in w.funding)
    mark1 = _open_at(w.t_close)
    good_upl = -w.qty * (mark1 - sell["price"])
    snaps = w.snapshots(usdt1=w.usdt0 - sell["fee_usd"] + w.funding_sum)
    snaps[1].update(perp_upl_usd=good_upl + 400.0, perp_qty=-w.qty, perp_avg_entry=sell["price"], perp_mark=mark1)       # 400 USD more than -0.5 x (mark - entry)
    rep = ledger(w, tmp_path, snapshots=snaps)
    assert "SNAPSHOT_INCONSISTENT" in hard(rep)


def test_a_missing_perp_mark_on_an_open_position_is_a_gap_not_a_false_contradiction(tmp_path):
    w = World()
    sell = w.fills[0]
    w.fills = w.fills[:1]
    w.funding_sum = sum(r["payment_usd"] for r in w.funding)
    mark1 = _open_at(w.t_close)
    snaps = w.snapshots(usdt1=w.usdt0 - sell["fee_usd"] + w.funding_sum)
    snaps[1].update(perp_upl_usd=-w.qty * (mark1 - sell["price"]), perp_qty=-w.qty, perp_avg_entry=sell["price"])          # perp_mark omitted
    rep = ledger(w, tmp_path, snapshots=snaps)
    assert "PERP_MARK_MISSING" in codes(rep, OL.SEVERITY_GAP) and "EQUITY_RESIDUAL" not in hard(rep) and rep["verdict"] == "INCOMPLETE"


@pytest.mark.parametrize("factor", [1.10, 0.90])
def test_a_snapshot_btc_price_outside_the_public_bar_is_refused_not_trusted(tmp_path, factor):
    w = World()
    w.lines = _statement(w)
    snaps = w.snapshots()
    snaps[1]["btc_price"] *= factor                                            # the supplier marks the owned BTC 10 % away from the market
    rep = ledger(w, tmp_path, snapshots=snaps)
    assert "SNAPSHOT_PRICE_OFF_PUBLIC_BAR" in hard(rep) and rep["verdict"] == "REJECTED_RECORDS_CONTRADICT"


def test_a_btc_flow_valued_at_zero_or_far_from_the_market_cannot_move_profit_across_the_contribution_line(tmp_path):
    w = World()
    ts = w.t_open + HOUR
    w.flows = [{"flow_id": "D", "ts_ms": ts, "coin": "BTC", "amount": 1.0, "kind": "DEPOSIT", "usd_value": 0.0}]
    with pytest.raises(OL.RecordError, match="cannot be worth 0 USD"):
        OL.load_manifest(w.write(tmp_path))
    w2 = World()
    w2.flows = [{"flow_id": "D", "ts_ms": ts, "coin": "BTC", "amount": 1.0, "kind": "DEPOSIT", "usd_value": 1.0}]
    (tmp_path / "b").mkdir()
    rep = ledger(w2, tmp_path / "b")
    assert "BTC_FLOW_VALUE_OFF_PUBLIC_BAR" in hard(rep) and rep["verdict"] == "REJECTED_RECORDS_CONTRADICT"


def test_while_a_btc_flow_is_unpriced_no_return_and_no_btc_pnl_is_stated(tmp_path):
    w = World()
    w.flows = [{"flow_id": "D", "ts_ms": w.t_open + HOUR, "coin": "BTC", "amount": 1.0, "kind": "DEPOSIT"}]
    rep = ledger(w, tmp_path)
    assert rep["returns"]["status"] == "WITHHELD_UNPRICED_BTC_FLOW" and rep["equity_bridge"]["btc_economic_pnl_usd"] is None
    assert rep["equity_bridge"]["unexplained_usd"] is None and "UNPRICED_BTC_FLOW" in codes(rep, OL.SEVERITY_GAP)
    text = OL.render(rep)
    assert "RETURNS: withheld" in text and "modified Dietz" not in text


def test_the_same_btc_transfer_supplied_unpriced_and_then_priced_is_one_priced_flow(tmp_path):
    w = World()
    ts = w.t_open + HOUR
    price = _open_at(ts)
    flow = {"flow_id": "D", "ts_ms": ts, "coin": "BTC", "amount": 0.25, "kind": "DEPOSIT"}
    merged = OL._merge_flow_prices([OL.parse_external_flows([flow])[0][0], OL.parse_external_flows([dict(flow, usd_value=0.25 * price)])[0][0]])
    assert len(merged) == 1 and merged[0].usd_value == pytest.approx(0.25 * price)
    different = OL._merge_flow_prices([OL.parse_external_flows([flow])[0][0], OL.parse_external_flows([dict(flow, amount=0.3, usd_value=0.3 * price)])[0][0]])
    assert len(different) == 2                                               # a flow that differs in more than the price is left for the duplicate rule


def test_a_statement_with_no_running_balance_proves_nothing_and_says_so(tmp_path):
    w = World()
    w.lines = _statement(w)
    for l in w.lines:
        l["cash_balance"] = None
    rep = ledger(w, tmp_path)
    assert "STATEMENT_UNCHAINED" in codes(rep, OL.SEVERITY_GAP) and rep["verdict"] == "INCOMPLETE" and rep["statement"]["anchored"] is False


def test_a_self_consistent_statement_that_starts_and_ends_elsewhere_is_not_anchored(tmp_path):
    w = World()
    w.lines = _statement(w)
    for l in w.lines:
        l["cash_balance"] += 12_345.0                                          # the whole chain shifted: every step still follows from the one before
    rep = ledger(w, tmp_path)
    assert rep["statement"]["continuity_breaks"] == [] and "STATEMENT_NOT_ANCHORED" in codes(rep, OL.SEVERITY_GAP) and rep["verdict"] == "INCOMPLETE"


def test_an_unrecorded_cost_larger_than_the_periods_income_is_not_hidden_by_a_large_equity(tmp_path):
    """At about 1.06M of equity a tolerance scaled to the equity is over 100 USD; the statement is cent-exact, and 80 USD of unrecorded cost exceeds the
    funding the window earned (about 18 USD)."""
    w = World(usdt0=1_000_000.0)
    w.lines = _statement(w)
    snaps = w.snapshots()
    snaps[1]["usdt"] -= 80.0
    rep = ledger(w, tmp_path, snapshots=snaps)
    assert not rep["verdict"].startswith("RECONCILED") and rep["residuals"]["usdt"] == pytest.approx(-80.0, abs=1e-6)
    assert rep["residuals"]["tolerance_usd"] > 80.0                          # the equity-scaled tolerance WOULD have hidden it
    good = World(usdt0=1_000_000.0)
    good.lines = _statement(good)
    (tmp_path / "g").mkdir()
    assert ledger(good, tmp_path / "g")["verdict"] == "RECONCILED_NOT_AUTHENTICATED"


def test_a_positive_interest_line_is_unexplained_income(tmp_path):
    w = World()
    w.lines = _statement(w) + [{"line_id": "I", "ts_ms": w.t_open + 4 * HOUR, "coin": "USDT", "category": "INTEREST", "change": 5000.0, "cash_balance": None, "ref_id": None}]
    w.interest = 5000.0
    rep = ledger(w, tmp_path)
    assert "INTEREST_UNEXPECTED" in codes(rep, OL.SEVERITY_GAP) and not rep["verdict"].startswith("RECONCILED")


def test_btc_statement_lines_other_than_spot_and_transfers_are_flagged_and_internal_lines_must_net_to_zero(tmp_path):
    w = World()
    w.lines = _statement(w) + [{"line_id": "B", "ts_ms": w.t_open + 4 * HOUR, "coin": "BTC", "category": "INTEREST", "change": -0.001, "cash_balance": None, "ref_id": None},
                               {"line_id": "N", "ts_ms": w.t_open + 5 * HOUR, "coin": "USDT", "category": "INTERNAL", "change": 70.0, "cash_balance": None, "ref_id": None}]
    rep = ledger(w, tmp_path)
    assert "BTC_LINES_UNMODELLED" in codes(rep, OL.SEVERITY_GAP) and "INTERNAL_LINES_UNBALANCED" in codes(rep, OL.SEVERITY_GAP)


def _same_ms_rows(order, t=1_700_000_000_000):
    bal, rows = 500.0, []
    for i in range(8):
        bal -= 1.0 + i
        rows.append((i, bal, -(1.0 + i)))
    out = [OL.CashLine(f"l{i}", t, "USDT", "FEE", ch, b, None, f"s#{i + 1}", None) for i, b, ch in rows]
    return [out[i] for i in order]


@pytest.mark.parametrize("order", [list(range(8)), list(reversed(range(8))), [3, 0, 7, 1, 6, 2, 5, 4]])
def test_eight_rows_in_one_millisecond_chain_in_whichever_order_the_file_lists_them(order):
    ch = OL._chain(_same_ms_rows(order), "USDT")
    assert ch["breaks"] == [] and ch["unchained"] == [] and ch["steps"] == 8
    assert ch["start"] == pytest.approx(500.0) and ch["end"] == pytest.approx(500.0 - sum(1.0 + i for i in range(8)))


def test_file_position_is_numeric_so_row_ten_follows_row_nine():
    assert OL._seq("label#10") > OL._seq("label#9") and OL._seq("label") == 0


def test_a_third_snapshot_that_contradicts_the_records_is_refused_and_does_not_earn_the_top_rung(tmp_path):
    w = World()
    w.lines = _statement(w)
    mid = {"snapshot_id": "mid", "ts_ms": (w.t_open + w.t_close) // 2, "usdt": 1.0, "btc": 0.001, "btc_price": _open_at((w.t_open + w.t_close) // 2),
           "perp_upl_usd": 0.0, "perp_qty": 0.0}
    rep = ledger(w, tmp_path, snapshots=w.snapshots() + [mid])
    assert "MIDDLE_SNAPSHOT_UNRECONCILED" in codes(rep) and not rep["verdict"].startswith("RECONCILED")


def test_two_snapshots_at_one_instant_that_disagree_are_a_contradiction_and_identical_ones_are_one(tmp_path):
    w = World()
    snaps = w.snapshots()
    twin = dict(snaps[1], snapshot_id="close2", usdt=snaps[1]["usdt"] + 4.0)
    rep = ledger(w, tmp_path, snapshots=snaps + [twin])
    assert "SNAPSHOT_DUPLICATE_TIMESTAMP" in hard(rep) and rep["verdict"] == "REJECTED_RECORDS_CONTRADICT"
    (tmp_path / "b").mkdir()
    same = ledger(w, tmp_path / "b", snapshots=snaps + [dict(snaps[1], snapshot_id="close2")])
    assert "SNAPSHOT_DUPLICATE_TIMESTAMP" not in codes(same) and same["records"]["snapshots"] == 2


def test_the_json_output_works_when_the_closing_snapshot_omits_the_position(tmp_path, capsys):
    w = World()
    snaps = w.snapshots()
    del snaps[1]["perp_qty"]
    man = w.write(tmp_path, snapshots=snaps)
    assert OL.main(["--manifest", man, "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["residuals"]["perp_qty"] is None


def test_one_settlement_under_two_ids_is_a_contradiction_not_double_funding(tmp_path):
    w = World()
    w.extra_files = [("funding2.jsonl", [dict(w.funding[0], exec_id="alias-of-first")], "funding")]
    rep = ledger(w, tmp_path)
    assert "SETTLEMENT_DUPLICATED_UNDER_DIFFERENT_IDS" in hard(rep) and rep["verdict"] == "REJECTED_RECORDS_CONTRADICT"


def test_the_dietz_base_guard_and_the_minimum_annualising_window():
    day = 86_400_000
    t0 = 1_700_000_000_000
    # 1,000 -> 1,100 with a 999.99999 withdrawal almost at the start: the capital at work is nothing, the ratio is not a rate
    d = OL.modified_dietz(1000.0, 1100.0, [(t0 + 1000, -999.99999)], t0, t0 + 100 * day)
    assert d["base_small"] is True and d["annualised_simple_pct"] is None and "tenth" in d["annualised_why_not"]
    # four days of -1.25 % must not become a -68 %/yr figure
    short = OL.modified_dietz(1000.0, 987.5, [], t0, t0 + 4 * day)
    assert short["annualised_simple_pct"] is None and "30 days" in short["annualised_why_not"]
    r = OL.irr(1000.0, 987.5, [], t0, t0 + 4 * day)
    assert r["annual_rate"] is None and "30 days" in r["why_not"]
    ok = OL.modified_dietz(1000.0, 1100.0, [], t0, t0 + 100 * day)
    assert ok["base_small"] is False and ok["annualised_simple_pct"] == pytest.approx(10.0 / (100 / 365.0))


def test_capital_notes_describe_the_return_figure_and_do_not_lower_the_reconciliation_verdict(tmp_path):
    w, rep = _clean_with_statement(tmp_path)
    assert rep["verdict"] == "RECONCILED_NOT_AUTHENTICATED"
    sev = {f["code"]: f["severity"] for f in rep["flags"]}
    assert sev.get("TWO_POINT_CAPITAL_AVERAGE") == "INFO"


# ============================================================================================ regressions from the second independent review

def test_a_usd_labelled_statement_cannot_escape_the_usdt_anchors_or_the_cent_tolerance(tmp_path):
    for coin in ("USDT", "USD"):
        w = World(usdt0=1_000_000.0)
        w.lines = [dict(l, coin=coin) for l in _statement(w)]
        snaps = w.snapshots()
        snaps[1]["usdt"] -= 80.0                                                    # an unrecorded 80 USD cost at ~1.06M of equity
        (tmp_path / coin).mkdir()
        rep = ledger(w, tmp_path / coin, snapshots=snaps)
        assert "STATEMENT_NOT_ANCHORED" in codes(rep, OL.SEVERITY_GAP) and not rep["verdict"].startswith("RECONCILED"), (coin, rep["verdict"])
    assert OL.parse_cash_lines([{"line_id": "x", "ts_ms": 1_700_000_000_000, "coin": "usd", "category": "FEE", "change": -1.0}])[0][0].coin == "USDT"
    assert OL.parse_external_flows([{"flow_id": "f", "ts_ms": 1_700_000_000_000, "coin": "USD", "amount": 5.0, "kind": "DEPOSIT"}])[0][0].coin == "USDT"


def test_the_cash_tolerance_is_to_the_cent_only_when_the_statement_is_anchored(tmp_path):
    w = World(usdt0=1_000_000.0)
    w.lines = _statement(w)
    rep = ledger(w, tmp_path)
    assert rep["statement"]["anchored"] is True and rep["residuals"]["cash_tolerance_usd"] < 1.0 < rep["residuals"]["tolerance_usd"]
    (tmp_path / "b").mkdir()
    bare = ledger(World(usdt0=1_000_000.0), tmp_path / "b")                            # no statement: nothing independent, the loose tolerance stands
    assert bare["statement"]["anchored"] is False and bare["residuals"]["cash_tolerance_usd"] == bare["residuals"]["tolerance_usd"]


def test_no_price_dependent_figure_survives_an_unpriced_btc_flow(tmp_path):
    w = World()
    w.flows = [{"flow_id": "D", "ts_ms": w.t_open + HOUR, "coin": "BTC", "amount": 1.0, "kind": "DEPOSIT"}]
    rep = ledger(w, tmp_path)
    assert rep["economic_capital"]["account_return_on_capital_period"] is None and "account_return_withheld" in rep["economic_capital"]
    assert rep["equity_bridge"]["external_flows_complete"] is False and "PARTIAL" in OL.render(rep)
    assert rep["returns"]["status"] == "WITHHELD_UNPRICED_BTC_FLOW"


def test_no_figure_is_annualised_over_a_window_shorter_than_thirty_days(tmp_path):
    w = World()                                                                      # four days
    w.lines = _statement(w)
    mid_ts = (w.t_open + w.t_close) // 2
    mid = {"snapshot_id": "mid", "ts_ms": mid_ts, "usdt": w.usdt0 + 0.0, "btc": w.btc0, "btc_price": _open_at(mid_ts), "perp_upl_usd": 0.0, "perp_qty": 0.0}
    # a middle snapshot that is coherent at that instant: USDT after the opening fill's fee is paid and before anything else (fills are far later)
    rep = ledger(w, tmp_path, snapshots=w.snapshots() + [mid])
    r = rep["returns"]
    assert r["account_linked"] is None or r["account_linked"]["annualised_simple_pct"] is None
    c = rep["economic_capital"]
    assert c.get("strategy_return_on_capital_annualised_simple_pct") is None and "30 days" in c.get("annualised_why_not", "")
    assert rep["economic_capital"]["return_on_hedged_notional"].get("strategy_return_annualised_simple_pct") is None
    assert OL.annualise(0.01, 29.9)[0] is None and OL.annualise(0.01, 365.0)[0] == pytest.approx(1.0)


def test_without_public_history_the_snapshot_prices_are_the_suppliers_word_and_the_report_says_how_much_depends_on_them(tmp_path):
    w = World()
    w.lines = _statement(w)
    man = json.loads(open(w.write(tmp_path)).read())
    man["no_public_history"] = True
    (tmp_path / "manifest.json").write_text(json.dumps(man))
    rep = OL.build(OL.load_manifest(str(tmp_path / "manifest.json")))
    sev = {f["code"]: f["severity"] for f in rep["flags"]}
    assert sev.get("SNAPSHOT_PRICE_CHECK_NOT_RUN") == "GAP" and rep["verdict"] == "RECONCILED_BUT_UNCHECKED"      # a check that could not run, never a clean top rung
    assert rep["equity_bridge"]["btc_leg_price_sensitivity_usd_per_1pct"] == pytest.approx(0.01 * (abs(w.btc0 * w.p0) + abs(w.expected_btc1() * w.p1)))
    assert any("SNAPSHOT_PRICE_CHECK_NOT_RUN" in lim for lim in rep["limits"])


def test_a_closing_perp_mark_outside_the_public_bar_is_refused(tmp_path):
    w = World()
    sell = w.fills[0]
    w.fills = w.fills[:1]
    w.funding_sum = sum(r["payment_usd"] for r in w.funding)
    mark1 = _open_at(w.t_close) * 1.10
    snaps = w.snapshots(usdt1=w.usdt0 - sell["fee_usd"] + w.funding_sum)
    snaps[1].update(perp_upl_usd=-w.qty * (mark1 - sell["price"]), perp_qty=-w.qty, perp_avg_entry=sell["price"], perp_mark=mark1)
    rep = ledger(w, tmp_path, snapshots=snaps)
    assert any("perp_mark" in str(ex) for f in rep["flags"] if f["code"] == "SNAPSHOT_PRICE_OFF_PUBLIC_BAR" for ex in f["examples"]) and "SNAPSHOT_PRICE_OFF_PUBLIC_BAR" in hard(rep)


def test_the_opening_anchor_is_checked_on_its_own(tmp_path):
    w = World()
    w.lines = _statement(w)
    snaps = w.snapshots()
    snaps[0]["usdt"] += 50.0                                                         # only the OPENING balance disagrees with the statement's first balance
    snaps[1]["usdt"] += 50.0
    rep = ledger(w, tmp_path, snapshots=snaps)
    msgs = [e for f in rep["flags"] if f["code"] == "STATEMENT_NOT_ANCHORED" for e in f["examples"]]
    assert any(m.startswith("USDT opening") for m in msgs) and any(m.startswith("USDT closing") for m in msgs)
    snaps2 = w.snapshots()
    snaps2[0]["usdt"] += 50.0                                                        # opening only (the closing still ties)
    (tmp_path / "b").mkdir()
    rep2 = ledger(w, tmp_path / "b", snapshots=snaps2)
    msgs2 = [e for f in rep2["flags"] if f["code"] == "STATEMENT_NOT_ANCHORED" for e in f["examples"]]
    assert msgs2 and all(m.startswith("USDT opening") for m in msgs2)


def test_the_btc_chain_is_anchored_to_the_btc_snapshots(tmp_path):
    w = World()
    t1 = w.t_open + 2 * HOUR
    p1 = _open_at(t1)
    w.spot = [{"trade_id": "S1", "ts_ms": t1, "symbol": "BTCUSDT", "side": "Buy", "qty": 0.2, "price": p1, "fee": 0.0002, "fee_coin": "BTC"}]
    w.lines = _statement(w) + [{"line_id": "P1", "ts_ms": t1, "coin": "BTC", "category": "SPOT", "change": 0.2 - 0.0002, "cash_balance": w.btc0 + 0.2 - 0.0002, "ref_id": "S1"}]
    snaps = w.snapshots()
    snaps[1]["btc"] += 0.5                                                           # the closing snapshot holds BTC the statement never received
    rep = ledger(w, tmp_path, snapshots=snaps)
    msgs = [e for f in rep["flags"] if f["code"] == "STATEMENT_NOT_ANCHORED" for e in f["examples"]]
    assert any(m.startswith("BTC closing") for m in msgs)


def test_a_middle_snapshot_whose_cash_alone_is_wrong_is_caught_and_does_not_feed_the_average(tmp_path):
    w = World()
    w.lines = _statement(w)
    mid_ts = (w.t_open + w.t_close) // 2
    good_mid = {"snapshot_id": "mid", "ts_ms": mid_ts, "usdt": w.usdt0 - w.fills[0]["fee_usd"] + sum(r["payment_usd"] for r in w.funding if r["ts_ms"] <= mid_ts),
                "btc": w.btc0, "btc_price": _open_at(mid_ts), "perp_upl_usd": 0.0, "perp_qty": 0.0, "perp_avg_entry": None}
    # the position is open at mid (opened before it), so the snapshot must state it; build it coherently
    mark = _open_at(mid_ts)
    good_mid.update(perp_qty=-w.qty, perp_avg_entry=w.fills[0]["price"], perp_mark=mark, perp_upl_usd=-w.qty * (mark - w.fills[0]["price"]))
    ok = ledger(w, tmp_path, snapshots=[w.snapshots()[0], good_mid, w.snapshots()[1]])
    assert "MIDDLE_SNAPSHOT_UNRECONCILED" not in codes(ok), ok["flags"]
    bad = dict(good_mid, usdt=good_mid["usdt"] + 500.0)                              # only the cash is wrong
    (tmp_path / "b").mkdir()
    rep = ledger(w, tmp_path / "b", snapshots=[w.snapshots()[0], bad, w.snapshots()[1]])
    assert "MIDDLE_SNAPSHOT_UNRECONCILED" in codes(rep) and rep["economic_capital"]["snapshots_used"] == 2
    assert rep["economic_capital"]["average_economic_capital_usd"] == pytest.approx((w.btc0 * w.p0 + 1000.0 + w.expected_btc1() * w.p1 + 1000.0) / 2.0)
    assert ok["economic_capital"]["snapshots_used"] == 3


def test_an_unpriced_flow_listed_twice_and_then_priced_merges_whatever_the_order(tmp_path):
    w = World()
    ts = w.t_open + HOUR
    price = _open_at(ts)
    flow = {"flow_id": "D", "ts_ms": ts, "coin": "BTC", "amount": 0.25, "kind": "DEPOSIT"}
    mk = lambda **kw: OL.parse_external_flows([dict(flow, **kw)])[0][0]                # noqa: E731
    for order in ([mk(), mk(), mk(usd_value=0.25 * price)], [mk(usd_value=0.25 * price), mk(), mk()], [mk(), mk(usd_value=0.25 * price), mk()]):
        merged = OL._merge_flow_prices(order)
        assert len(OL._dedupe(merged, lambda f: f.flow_id, "flows", [])) == 1
        assert all(f.usd_value is not None for f in OL._dedupe(merged, lambda f: f.flow_id, "flows", []))


def test_either_layout_of_the_spot_usdt_fee_reconciles_but_one_layout_must_explain_both_totals(tmp_path):
    w = World()
    t1 = w.t_open + 2 * HOUR
    p1 = _open_at(t1)
    fee = 0.2 * p1 * 0.001
    w.spot = [{"trade_id": "S1", "ts_ms": t1, "symbol": "BTCUSDT", "side": "Buy", "qty": 0.2, "price": p1, "fee": fee, "fee_coin": "USDT"}]
    base = _statement(w)
    # layout B: the SPOT line excludes the fee, a FEE line carries it
    b = base + [{"line_id": "P1", "ts_ms": t1, "coin": "USDT", "category": "SPOT", "change": -0.2 * p1, "cash_balance": None, "ref_id": "S1"},
                {"line_id": "P2", "ts_ms": t1, "coin": "BTC", "category": "SPOT", "change": 0.2, "cash_balance": None, "ref_id": "S1"},
                {"line_id": "P3", "ts_ms": t1, "coin": "USDT", "category": "FEE", "change": -fee, "cash_balance": None, "ref_id": "S1"}]
    w.lines = b
    rep = ledger(w, tmp_path)
    assert "EXECUTION_NOT_IN_STATEMENT" not in codes(rep) and "SPOT_NOT_IN_STATEMENT" not in codes(rep), rep["flags"]
    # layout A: the fee is inside the SPOT line, with no separate FEE line
    a_lines = base + [{"line_id": "P1", "ts_ms": t1, "coin": "USDT", "category": "SPOT", "change": -0.2 * p1 - fee, "cash_balance": None, "ref_id": "S1"},
                      {"line_id": "P2", "ts_ms": t1, "coin": "BTC", "category": "SPOT", "change": 0.2, "cash_balance": None, "ref_id": "S1"}]
    w.lines = a_lines
    (tmp_path / "a").mkdir()
    rep2 = ledger(w, tmp_path / "a")
    assert "EXECUTION_NOT_IN_STATEMENT" not in codes(rep2) and "SPOT_NOT_IN_STATEMENT" not in codes(rep2), rep2["flags"]
    # the fee counted in BOTH places is wrong under either layout
    w.lines = b[:-1] + [dict(b[-1], line_id="P4"), {"line_id": "P5", "ts_ms": t1, "coin": "USDT", "category": "SPOT", "change": -fee, "cash_balance": None, "ref_id": "S1"}]
    (tmp_path / "c").mkdir()
    rep3 = ledger(w, tmp_path / "c")
    assert {"EXECUTION_NOT_IN_STATEMENT", "SPOT_NOT_IN_STATEMENT"} & set(codes(rep3, OL.SEVERITY_GAP))


def test_duplicate_snapshots_that_differ_only_by_rounding_or_an_omitted_field_are_one(tmp_path):
    w = World()
    snaps = w.snapshots()
    twin = dict(snaps[1], snapshot_id="ui", usdt=round(snaps[1]["usdt"], 2) + 0.004)
    twin.pop("perp_qty", None)
    rep = ledger(w, tmp_path, snapshots=snaps + [twin])
    assert "SNAPSHOT_DUPLICATE_TIMESTAMP" not in codes(rep) and rep["records"]["snapshots"] == 2


def test_the_first_group_of_same_millisecond_rows_is_ordered_from_the_snapshot_balance():
    t = 1_700_000_000_000
    rows = [OL.CashLine("b", t, "USDT", "FEE", -5.0, 100.0, None, "s#1", None), OL.CashLine("a", t, "USDT", "FEE", 5.0, 105.0, None, "s#2", None)]
    ch = OL._chain(rows, "USDT", seed=100.0)
    assert ch["breaks"] == [] and ch["start"] == pytest.approx(100.0) and ch["end"] == pytest.approx(100.0)
    off = OL._chain(rows, "USDT", seed=7.0)                                          # a seed the rows cannot start from: still ordered, but the start shows it
    assert off["start"] != pytest.approx(7.0) and off["breaks"] == []                # the rows chain from either end; the caller compares start with its snapshot and flags the anchor


def test_other_assets_that_are_the_same_at_every_snapshot_are_a_note_and_one_that_changes_is_a_gap(tmp_path):
    w = World()
    w.lines = _statement(w)
    snaps = w.snapshots()
    for sn in snaps:
        sn["other_usd"] = 25.0
    rep = ledger(w, tmp_path, snapshots=snaps)
    sev = {f["code"]: f["severity"] for f in rep["flags"]}
    assert sev["OTHER_ASSETS_PRESENT"] == "INFO" and rep["verdict"] == "RECONCILED_NOT_AUTHENTICATED"
    snaps[1]["other_usd"] = 31.0
    (tmp_path / "b").mkdir()
    rep2 = ledger(w, tmp_path / "b", snapshots=snaps)
    assert {f["code"]: f["severity"] for f in rep2["flags"]}["OTHER_ASSETS_PRESENT"] == "GAP" and not rep2["verdict"].startswith("RECONCILED")


def test_the_bridges_unexplained_line_is_the_usdt_residual_by_identity_and_a_btc_quantity_error_is_the_components_residuals_to_catch(tmp_path):
    w = World()
    w.lines = _statement(w)
    snaps = w.snapshots()
    snaps[1]["usdt"] += 7.0
    rep = ledger(w, tmp_path, snapshots=snaps)
    assert rep["equity_bridge"]["unexplained_usd"] == pytest.approx(rep["residuals"]["usdt"], abs=1e-9)
    w2 = World()
    w2.lines = _statement(w2)
    snaps2 = w2.snapshots()
    snaps2[1]["btc"] += 0.02                                                          # BTC that appeared: absorbed into the BTC leg of the bridge, caught by the components
    (tmp_path / "b").mkdir()
    rep2 = ledger(w2, tmp_path / "b", snapshots=snaps2)
    assert rep2["equity_bridge"]["unexplained_usd"] == pytest.approx(0.0, abs=1e-6) and "EQUITY_RESIDUAL" in hard(rep2)


def test_a_grossly_mispriced_btc_flow_is_rejected_even_when_no_public_bar_covers_the_period(tmp_path):
    # The flow's USD value cancels out of the bridge, so 'unexplained' cannot catch it: with no public history a 0.5 BTC deposit valued at 1 USD
    # used to read as +40 % profit. The snapshot price envelope now bounds it.
    w = World()
    w.flows = [{"flow_id": "D", "ts_ms": w.t_open + HOUR, "coin": "BTC", "amount": 0.5, "kind": "DEPOSIT", "usd_value": 1.0}]
    inp = OL.load_manifest(w.write(tmp_path))
    inp.perp_kwargs["spot_range"] = None
    rep = OL.build(inp)
    assert "BTC_FLOW_VALUE_OFF_PUBLIC_BAR" in hard(rep)
    assert rep["verdict"] == "REJECTED_RECORDS_CONTRADICT"


def test_a_sane_btc_flow_without_a_public_bar_is_not_rejected_by_the_envelope(tmp_path):
    w = World()
    w.flows = [{"flow_id": "D", "ts_ms": w.t_open + HOUR, "coin": "BTC", "amount": 0.5, "kind": "DEPOSIT", "usd_value": 0.5 * w.p0}]
    inp = OL.load_manifest(w.write(tmp_path))
    inp.perp_kwargs["spot_range"] = None
    assert "BTC_FLOW_VALUE_OFF_PUBLIC_BAR" not in hard(OL.build(inp))


@pytest.mark.parametrize("spec", [5, None, ["a"], {"column": "A", "transform": 7}])
def test_a_malformed_column_map_entry_is_a_clean_refusal_not_a_traceback(spec):
    with pytest.raises(OL.RecordError):
        OL.apply_column_map([{"A": "1"}], {"exec_id": spec}, None, "x.csv")
