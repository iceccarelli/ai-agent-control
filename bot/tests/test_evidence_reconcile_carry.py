"""The carry drill's legs and cycle, reconciled against captured venue rows.

Every row here is SYNTHETIC (see evidence_fixtures). What is under test is the
reconciler's logic: which disagreement it names, and that it never resolves a
leg or a cycle on the strength of the bot's own report. No test here says
anything about what Bybit returns; the real check is the operator pointing the
CLI at a real capture.

The shape follows what the drill's capture really contains: the order link id
is INSIDE `request.body` (a JSON string) and `response.result`, the top-level
`order_link_id` of every record is empty.
"""
from __future__ import annotations

import copy
import json
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "tools"))
sys.path.insert(0, os.path.dirname(HERE))

import evidence_fixtures as F  # noqa: E402
import evidence_reconcile as R  # noqa: E402
import venue_evidence as ve  # noqa: E402

H = 3_600_000
OPEN_MS = F.T0 + 1_000
SETTLE_MS = F.T0 + 4 * H
CLOSE_MS = F.T0 + 5 * H
NOW = F.T0 + 10 * H
OPEN_LINK, CLOSE_LINK = "BB-carr-1-aaaa", "BB-carr-2-bbbb"
OPEN_ORD, CLOSE_ORD = "ord-open", "ord-close"

# 0.001 BTC sold at 80620.4, bought back at 80650.0
QTY, P_IN, P_OUT = 0.001, 80620.4, 80650.0
FEE_IN, FEE_OUT = 0.04434122, 0.04435


def create_rec(link, order_id, side, *, reduce=False, qty="0.001", prev, at,
               category="linear", order_type="Market"):
    body = {"category": category, "symbol": "BTCUSDT", "side": side,
            "orderType": order_type, "qty": qty, "orderLinkId": link}
    if reduce:
        body["reduceOnly"] = True
    return F.record(
        "/v5/order/create", [], prev=prev, at=at, method="POST",
        request={"headers": {}, "params": {}, "body": json.dumps(body)},
        response={"retCode": 0, "retMsg": "OK", "_status_code": 200,
                  "result": {"orderId": order_id, "orderLinkId": link}})


def ex(i, order, link, side, qty, price, fee, t, cur="USDT"):
    r = F.exec_row(i, order=order, link=link, side=side, qty=qty, price=price,
                   fee=fee, t=t)
    r["feeCurrency"] = cur
    return r


def order(oid, link, side, qty, avg, fee, status="Filled", reduce=False):
    r = F.order_row(oid, cum=qty, side=side, status=status, link=link)
    r.update(avgPrice=avg, cumExecFee=fee, reduceOnly=reduce)
    return r


def pos(size, symbol="BTCUSDT"):
    return {"symbol": symbol, "size": size, "side": "Sell" if size != "0" else ""}


def build(*, drop=(), tweak=None, extra=(), settle=True):
    """A clean open -> hold -> close capture; `tweak(name, row_or_body)` may
    alter a named piece, `drop` omits named records, `extra` appends records
    (callables of prev, at)."""
    tweak = tweak or (lambda name, value: value)
    specs = []

    def add(name, make):
        if name not in drop:
            specs.append((name, make))

    add("create_open", lambda prev: create_rec(
        OPEN_LINK, OPEN_ORD, "Sell", prev=prev, at=OPEN_MS + 1_000))
    add("exec_open", lambda prev: F.record(
        "/v5/execution/list", [tweak("exec_open", ex(
            1, OPEN_ORD, OPEN_LINK, "Sell", "0.001", "80620.4", "0.04434122",
            OPEN_MS))], prev=prev, at=OPEN_MS + 5_000))
    add("order_open", lambda prev: F.record(
        "/v5/order/history", [tweak("order_open", order(
            OPEN_ORD, OPEN_LINK, "Sell", "0.001", "80620.4", "0.04434122"))],
        prev=prev, at=OPEN_MS + 6_000))
    if settle:
        add("settlement", lambda prev: F.record(
            "/v5/account/transaction-log",
            [tweak("settlement", dict(
                F.settlement_row(t=SETTLE_MS, funding="0.0123", change="0.0123"),
                id="S-1", size="-0.001"))], prev=prev, at=SETTLE_MS + 60_000))
    add("create_close", lambda prev: create_rec(
        CLOSE_LINK, CLOSE_ORD, "Buy", reduce=True, prev=prev,
        at=CLOSE_MS + 1_000))
    add("exec_close", lambda prev: F.record(
        "/v5/execution/list", [tweak("exec_close", ex(
            2, CLOSE_ORD, CLOSE_LINK, "Buy", "0.001", "80650", "0.04435",
            CLOSE_MS))], prev=prev, at=CLOSE_MS + 5_000))
    add("order_close", lambda prev: F.record(
        "/v5/order/history", [tweak("order_close", order(
            CLOSE_ORD, CLOSE_LINK, "Buy", "0.001", "80650", "0.04435",
            reduce=True))], prev=prev, at=CLOSE_MS + 6_000))
    add("position", lambda prev: F.record(
        "/v5/position/list", [tweak("position", pos("0"))], prev=prev,
        at=CLOSE_MS + 60_000))
    out, prev = [], ve.GENESIS_PREV_HASH
    for _name, make in specs:
        rec = make(prev)
        out.append(rec)
        prev = rec["record_hash"]
    for make in extra:
        rec = make(prev)
        out.append(rec)
        prev = rec["record_hash"]
    return out


def leg_claims(**over):
    legs = [
        {"claim_id": f"leg:{OPEN_LINK}", "source": "testnet_claim",
         "environment": "testnet", "kind": "perp_open", "symbol": "BTCUSDT",
         "side": "Sell", "qty": QTY, "price": P_IN, "fee": FEE_IN,
         "fee_currency": "USDT", "fee_status": "confirmed",
         "order_link_id": OPEN_LINK, "reduce_only": False},
        {"claim_id": f"leg:{CLOSE_LINK}", "source": "testnet_claim",
         "environment": "testnet", "kind": "perp_close", "symbol": "BTCUSDT",
         "side": "Buy", "qty": QTY, "price": P_OUT, "fee": FEE_OUT,
         "fee_currency": "USDT", "fee_status": "confirmed",
         "order_link_id": CLOSE_LINK, "reduce_only": True}]
    for leg in legs:
        leg.update(over)
    return legs


def funding_claims():
    return [{"claim_id": "funding:S-1", "source": "testnet_claim",
             "environment": "testnet", "symbol": "BTCUSDT", "time_ms": SETTLE_MS,
             "amount": 0.0123, "venue_id": "S-1", "size": -0.001}]


def run(records=None, legs=None, funding=None, window=None, **kw):
    records = build() if records is None else records
    return R.reconcile(
        carry_claims=leg_claims() if legs is None else legs,
        funding_claims=funding_claims() if funding is None else funding,
        funding_window=window,
        captures=[R.capture_from_records(records)], now_ms=NOW, **kw)


def issues_of(report, section="carry_legs"):
    return [v["issues"] for v in report[section]]


# ---------------------------------------------------------------------------

class TestAWholeCycleThatTheVenueRowsReproduce:
    def test_both_legs_and_the_cycle_resolve(self):
        report = run()
        assert [v["provenance"] for v in report["carry_legs"]] == [R.VENUE_RECONCILED] * 2
        assert issues_of(report) == [[], []]
        assert report["carry_cycle"][0]["resolved"] is True
        assert report["funding"][0]["provenance"] == R.VENUE_RECONCILED
        assert report["pnl_claim_allowed"] is True

    def test_the_link_is_read_from_the_request_body_not_the_empty_top_level(self):
        records = build()
        assert all(r["order_link_id"] == "" for r in records)
        assert isinstance(next(r for r in records if r["method"] == "POST")
                          ["request"]["body"], str)
        assert run(records)["carry_legs"][0]["resolved"] is True

    def test_economics_are_summed_from_resolved_rows_and_state_their_scope(self):
        econ = run()["carry_cycle"][0]["economics"]
        assert econ["qty"] == pytest.approx(0.001)
        assert econ["perp_gross_usdt"] == pytest.approx((P_IN - P_OUT) * QTY)
        assert econ["fees_usdt"] == pytest.approx(FEE_IN + FEE_OUT)
        assert econ["funding_usdt"] == pytest.approx(0.0123)
        assert econ["net_usdt"] == pytest.approx(
            (P_IN - P_OUT) * QTY - FEE_IN - FEE_OUT + 0.0123)
        assert econ["settlements"] == 1
        assert econ["fee_bps_of_entry_notional"] == pytest.approx(
            (FEE_IN + FEE_OUT) / (P_IN * QTY) * 1e4)
        assert "NONE" in econ["inference"] and "spot" in econ["scope"]

    def test_a_cycle_with_no_settlement_is_a_cost_measurement_not_an_error(self):
        report = run(build(settle=False), funding=[])
        assert report["carry_cycle"][0]["resolved"] is True
        assert report["carry_cycle"][0]["economics"]["funding_usdt"] == 0.0
        assert report["carry_cycle"][0]["economics"]["settlements"] == 0


class TestWithoutTheVenuesExecutionRowsNothingIsReconciled:
    """The state of the first two real drill captures: orders and creates were
    captured, no execution/list row ever was."""

    def test_no_execution_rows_leaves_the_claim_a_claim(self):
        report = run(build(drop=("exec_open", "exec_close")))
        for v in report["carry_legs"]:
            assert v["provenance"] == R.CLAIMED_TESTNET_FILL
            assert "NOT_IN_CAPTURE" in v["issues"]
        assert report["carry_cycle"][0]["resolved"] is False
        assert report["pnl_claim_allowed"] is False

    def test_no_create_request_for_the_link(self):
        report = run(build(drop=("create_open",)))
        assert "NO_CREATE_REQUEST" in report["carry_legs"][0]["issues"]
        assert report["carry_legs"][0]["resolved"] is False

    def test_no_order_record_means_no_corroboration(self):
        report = run(build(drop=("order_open",)))
        assert "NO_CORROBORATION" in report["carry_legs"][0]["issues"]

    def test_an_order_only_ever_seen_live_has_no_final_state(self):
        report = run(build(tweak=lambda n, v: dict(v, orderStatus="New")
                           if n == "order_open" else v))
        assert "NO_FINAL_VENUE_STATE" in report["carry_legs"][0]["issues"]


def _alter(name, **fields):
    return lambda n, v: dict(v, **fields) if n == name else v


class TestEveryFieldTheVenueContradictsIsNamed:
    @pytest.mark.parametrize("name,fields,issue", [
        ("exec_open", {"execQty": "0.002"}, "VENUE_DISAGREES:qty"),
        ("exec_open", {"execPrice": "80621.4"}, "VENUE_DISAGREES:price"),
        ("exec_open", {"execFee": "0.05"}, "VENUE_DISAGREES:fee"),
        ("exec_open", {"feeCurrency": "BTC"}, "VENUE_DISAGREES:fee"),
        ("exec_open", {"side": "Buy"}, "VENUE_DISAGREES:leg_identity"),
        ("exec_open", {"execType": "Funding"}, "VENUE_DISAGREES:leg_identity"),
        ("exec_open", {"symbol": "ETHUSDT"}, "VENUE_DISAGREES:leg_identity"),
        ("exec_open", {"orderId": "someone-elses"}, "VENUE_DISAGREES:leg_identity"),
        ("order_open", {"cumExecQty": "0.002"}, "VENUE_DISAGREES:order_qty"),
        ("order_open", {"avgPrice": "80000"}, "VENUE_DISAGREES:order_price"),
        ("order_open", {"cumExecFee": "0.5"}, "VENUE_DISAGREES:order_fee"),
        ("order_open", {"orderStatus": "Cancelled"}, "VENUE_DISAGREES:order_status"),
        ("order_open", {"orderLinkId": "BB-other"}, "VENUE_DISAGREES:order_identity"),
        ("order_open", {"side": "Buy"}, "VENUE_DISAGREES:order_identity"),
        ("order_close", {"reduceOnly": False}, "VENUE_DISAGREES:order_reduce_only"),
    ])
    def test_the_disagreement_is_named_and_blocks(self, name, fields, issue):
        report = run(build(tweak=_alter(name, **fields)))
        leg = 1 if "close" in name else 0
        assert issue in report["carry_legs"][leg]["issues"], \
            report["carry_legs"][leg]["issues"]
        assert report["carry_legs"][leg]["resolved"] is False
        assert report["pnl_claim_allowed"] is False

    def test_a_claim_that_understates_the_fee_is_caught(self):
        legs = leg_claims()
        legs[0]["fee"] = FEE_IN / 2
        report = run(legs=legs)
        assert "VENUE_DISAGREES:fee" in report["carry_legs"][0]["issues"]

    def test_a_close_sent_without_reduce_only_is_a_create_disagreement(self):
        records = build()
        for r in records:
            if r["method"] == "POST" and CLOSE_LINK in r["request"]["body"]:
                body = json.loads(r["request"]["body"])
                body.pop("reduceOnly")
                r["request"]["body"] = json.dumps(body)
        # the chain is now broken too; rebuild it so only the content differs
        records = _rechain(records)
        issues = run(records)["carry_legs"][1]["issues"]
        assert "VENUE_DISAGREES:create_reduce_only" in issues

    @pytest.mark.parametrize("field,value,issue", [
        ("category", "spot", "VENUE_DISAGREES:create_category"),
        ("orderType", "Limit", "VENUE_DISAGREES:create_order_type"),
        ("qty", "0.0001", "VENUE_DISAGREES:create_qty"),
    ])
    def test_a_create_that_does_not_match_the_claim(self, field, value, issue):
        records = build()
        for r in records:
            if r["method"] == "POST" and OPEN_LINK in r["request"]["body"]:
                body = json.loads(r["request"]["body"])
                body[field] = value
                r["request"]["body"] = json.dumps(body)
        assert issue in run(_rechain(records))["carry_legs"][0]["issues"]


def _rechain(records):
    out, prev = [], ve.GENESIS_PREV_HASH
    for rec in records:
        body = {k: v for k, v in rec.items() if k not in ("record_hash",)}
        body["prev_hash"] = prev
        body["record_hash"] = ve.content_hash(
            {k: v for k, v in body.items() if k != "record_hash"})
        out.append(body)
        prev = body["record_hash"]
    return out


class TestTheClaimItselfMustBeSound:
    def test_a_simulation_claim_is_never_venue_evidence(self):
        legs = leg_claims(source="simulation", environment="simulation")
        report = run(legs=legs)
        assert {v["provenance"] for v in report["carry_legs"]} == {R.SIMULATED}
        assert report["pnl_claim_allowed"] is False

    def test_a_local_record_never_reconciles(self):
        report = run(legs=leg_claims(source="local", environment="unknown"))
        assert {v["provenance"] for v in report["carry_legs"]} == {R.LOCAL_RECORD}

    def test_mainnet_is_not_accepted(self):
        report = run(legs=leg_claims(environment="mainnet"))
        assert "MAINNET_EVIDENCE_NOT_ACCEPTED" in report["carry_legs"][0]["issues"]

    def test_a_fee_the_book_did_not_confirm_blocks(self):
        report = run(legs=leg_claims(fee_status="unknown"))
        assert "FEE_NOT_CONFIRMED" in report["carry_legs"][0]["issues"]
        assert report["carry_legs"][0]["resolved"] is False

    def test_a_fee_in_another_coin_blocks(self):
        report = run(legs=leg_claims(fee_currency="BTC"))
        assert "VENUE_DISAGREES:fee_currency" in report["carry_legs"][0]["issues"]

    def test_the_kind_must_match_side_and_reduce_only(self):
        legs = leg_claims()
        legs[1]["reduce_only"] = False
        assert "BAD_KIND" in run(legs=legs)["carry_legs"][1]["issues"]
        legs = leg_claims()
        legs[0]["side"] = "Buy"
        assert "BAD_SIDE" in run(legs=legs)["carry_legs"][0]["issues"]

    def test_a_link_claimed_twice_resolves_neither(self):
        legs = leg_claims()
        legs[1]["order_link_id"] = OPEN_LINK
        report = run(legs=legs)
        assert all("DUPLICATE_ORDER_LINK_ID" in v["issues"]
                   for v in report["carry_legs"])

    @pytest.mark.parametrize("name", ["qty", "price", "fee"])
    def test_non_numbers_are_refused_not_crashed_on(self, name):
        for bad in ("nan", "x", None, float("inf"), True, -1):
            legs = leg_claims()
            legs[0][name] = bad
            report = run(legs=legs)
            assert report["carry_legs"][0]["resolved"] is False

    def test_a_malformed_claim_is_reported_not_raised(self):
        report = R.reconcile(carry_claims=["junk", None, 5],
                             captures=[R.capture_from_records(build())], now_ms=NOW)
        assert all(v["resolved"] is False for v in report["carry_legs"])
        assert report["pnl_claim_allowed"] is False


class TestTheCaptureItselfMustBeTrustworthy:
    def test_a_broken_chain_blocks_everything(self):
        records = build()
        records[1]["response"]["result"]["list"][0]["execQty"] = "0.0010"
        report = run(records)
        assert any("CAPTURE_CHAIN_BROKEN" in v["issues"]
                   for v in report["carry_legs"])
        assert report["pnl_claim_allowed"] is False

    def test_rows_from_a_lookalike_host_prove_nothing(self):
        records = build()
        for r in records:
            r["request_url"] = r["request_url"].replace(
                "api-testnet.bybit.com", "api-testnet.bybit.com.evil.example")
        report = run(_rechain(records))
        assert report["carry_legs"][0]["resolved"] is False

    def test_a_mainnet_host_cannot_prove_a_testnet_claim(self):
        records = build()
        for r in records:
            r["request_url"] = r["request_url"].replace("api-testnet", "api")
            r["environment"] = "mainnet"
        report = run(_rechain(records))
        assert report["carry_legs"][0]["resolved"] is False
        assert report["pnl_claim_allowed"] is False

    def test_a_failed_create_is_not_a_create(self):
        records = build()
        for r in records:
            if r["method"] == "POST":
                r["response"]["retCode"] = 110007
        assert "NO_CREATE_REQUEST" in run(_rechain(records))["carry_legs"][0]["issues"]

    def test_execution_rows_captured_before_they_happened_are_refused(self):
        records = build()
        records[1]["captured_at_utc"] = F.iso(OPEN_MS - 60_000)
        assert "CAPTURED_BEFORE_EXECUTION" in run(_rechain(records))["carry_legs"][0]["issues"]


class TestTheCycleMustAddUp:
    def test_an_order_the_books_do_not_claim_blocks_the_cycle(self):
        extra = [lambda prev: create_rec("BB-carr-9-stray", "ord-stray", "Sell",
                                         prev=prev, at=CLOSE_MS + 90_000)]
        report = run(build(extra=extra))
        cycle = report["carry_cycle"][0]
        assert "UNCLAIMED_ORDER:BB-carr-9-stray" in cycle["issues"]
        assert cycle["resolved"] is False
        assert report["pnl_claim_allowed"] is False

    def test_entry_and_exit_quantities_must_match(self):
        legs = leg_claims()
        legs[1]["qty"] = 0.0005
        records = build(tweak=lambda n, v: (
            dict(v, execQty="0.0005") if n == "exec_close" else
            dict(v, cumExecQty="0.0005", qty="0.0005") if n == "order_close" else v))
        report = run(records, legs=legs)
        assert "CYCLE_QTY_MISMATCH" in report["carry_cycle"][0]["issues"]

    def test_there_must_be_both_an_entry_and_an_exit(self):
        report = run(legs=leg_claims()[:1])
        assert "CYCLE_NO_EXIT" in report["carry_cycle"][0]["issues"]
        assert report["carry_cycle"][0]["resolved"] is False
        # and the unclaimed close order is named
        assert f"UNCLAIMED_ORDER:{CLOSE_LINK}" in report["carry_cycle"][0]["issues"]

    def test_an_exit_before_the_entry_is_refused(self):
        records = build(tweak=lambda n, v: (
            dict(v, execTime=str(OPEN_MS - 500)) if n == "exec_close" else v))
        report = run(records)
        assert "CYCLE_EXIT_BEFORE_ENTRY" in report["carry_cycle"][0]["issues"]

    def test_no_position_read_after_the_close_means_no_proof_of_flat(self):
        report = run(build(drop=("position",)))
        assert "NO_FINAL_POSITION_READ" in report["carry_cycle"][0]["issues"]
        assert report["pnl_claim_allowed"] is False

    def test_a_position_read_from_before_the_close_is_not_proof(self):
        records = build()
        pos_rec = next(r for r in records if r["request_url"].endswith("position/list"))
        pos_rec["captured_at_utc"] = F.iso(CLOSE_MS - 3_600_000)
        assert "NO_FINAL_POSITION_READ" in run(_rechain(records))["carry_cycle"][0]["issues"]

    def test_a_position_still_open_is_not_flat(self):
        report = run(build(tweak=_alter_pos("0.001")))
        assert "CYCLE_NOT_FLAT" in report["carry_cycle"][0]["issues"]

    def test_an_unreadable_position_size_is_not_zero(self):
        report = run(build(tweak=_alter_pos("")))
        assert report["carry_cycle"][0]["resolved"] is False

    def test_the_numbers_are_withheld_when_the_cycle_is_not_resolved(self):
        econ = run(build(drop=("exec_open",)))["carry_cycle"][0]["economics"]
        assert "net_usdt" not in econ


def _alter_pos(size):
    return lambda n, v: dict(v, size=size) if n == "position" else v


class TestFundingMustMatchTheHold:
    def test_a_settlement_paid_in_the_hold_that_the_books_did_not_claim(self):
        # claims list a different settlement than the one the venue paid
        wrong = funding_claims()
        wrong[0]["time_ms"] = SETTLE_MS + 80_000
        report = run(funding=wrong)
        assert report["funding"][0]["issues"] == ["NOT_IN_CAPTURE"]
        issues = report["carry_cycle"][0]["issues"]
        assert f"UNCLAIMED_SETTLEMENT:{SETTLE_MS}" in issues
        assert "LEG_UNRESOLVED" in issues
        assert report["carry_cycle"][0]["resolved"] is False
        assert report["pnl_claim_allowed"] is False

    def test_a_claimed_settlement_outside_the_hold_is_refused(self):
        late = funding_claims()
        late[0]["time_ms"] = CLOSE_MS + H
        records = build(tweak=lambda n, v: (
            dict(v, transactionTime=str(CLOSE_MS + H)) if n == "settlement" else v))
        report = run(records, funding=late)
        assert "FUNDING_OUTSIDE_HOLD" in report["carry_cycle"][0]["issues"]

    def test_a_settlement_id_that_differs_from_the_venues_is_refused(self):
        wrong = funding_claims()
        wrong[0]["venue_id"] = "S-999"
        report = run(funding=wrong)
        assert "VENUE_DISAGREES:venue_id" in report["funding"][0]["issues"]

    def test_a_settlement_on_a_different_position_size_is_refused(self):
        records = build(tweak=lambda n, v: dict(v, size="-0.5") if n == "settlement" else v)
        report = run(records)
        assert "CYCLE_FUNDING_SIZE_MISMATCH" in report["carry_cycle"][0]["issues"]

    def test_an_amount_the_venue_did_not_pay_is_refused(self):
        wrong = funding_claims()
        wrong[0]["amount"] = 5.0
        assert "VENUE_DISAGREES:amount" in run(funding=wrong)["funding"][0]["issues"]

    def test_with_no_funding_claims_a_paid_settlement_is_still_unclaimed(self):
        report = run(funding=[])
        assert "UNCLAIMED_SETTLEMENT:all" in report["carry_cycle"][0]["issues"]


# ---------------------------------------------------------------------------
# the older report shape (what the first two real drills wrote)
# ---------------------------------------------------------------------------

def legacy_report(**over):
    stages = [
        {"stage": "preflight", "ok": True, "evidence": {}},
        {"stage": "open", "ok": True, "evidence": {
            "action": "opened", "filled_qty": QTY, "avg_price": P_IN,
            "fee": FEE_IN, "order_link_id": OPEN_LINK}},
        {"stage": "pair", "ok": True, "evidence": {}},
    ]
    rep = {"verdict": "PASSED", "venue": "testnet", "stages": stages}
    rep.update(over)
    return rep


def legacy_ledger(**over):
    led = {"schema": "cash_ledger/2", "category": "carry", "allows_live": False,
           "symbol": "BTCUSDT", "flat": True, "qty": QTY, "entry_price": P_IN,
           "exit_price": P_OUT, "fills": 2, "fees_usd": FEE_IN + FEE_OUT,
           "fees_confirmed": True, "funding_status": "CONFIRMED_FROM_SETTLEMENTS",
           "funding_settlements": 1, "funding_venue_usd": 0.0123}
    led.update(over)
    return led


class TestAnOlderDrillReport:
    def test_the_entry_leg_is_read_from_the_open_stage(self):
        got = R.claims_from_drill_report(legacy_report())
        assert got["derivation"] == "legacy_report"
        (leg,) = got["carry_leg_claims"]
        assert leg["order_link_id"] == OPEN_LINK and leg["kind"] == "perp_open"
        assert leg["qty"] == QTY and leg["price"] == P_IN and leg["fee"] == FEE_IN

    def test_the_exit_is_only_claimed_when_a_confirmed_ledger_states_it(self):
        assert len(R.claims_from_drill_report(legacy_report())["carry_leg_claims"]) == 1
        for bad in ({"fees_confirmed": False}, {"fills": 3}, {"flat": False},
                    {"exit_price": 0.0}, {"schema": "cash_ledger/1"}):
            got = R.claims_from_drill_report(legacy_report(), legacy_ledger(**bad))
            assert len(got["carry_leg_claims"]) == 1, bad
        got = R.claims_from_drill_report(legacy_report(), legacy_ledger())
        exit_leg = got["carry_leg_claims"][1]
        assert exit_leg["fee"] == pytest.approx(FEE_OUT)
        assert exit_leg["link_inferred"] is True and exit_leg["order_link_id"] == ""
        assert got["funding_window"] == {"count": 1, "total_usd": 0.0123}

    def test_a_non_testnet_report_is_a_local_record(self):
        got = R.claims_from_drill_report(legacy_report(venue="unknown"))
        assert got["carry_leg_claims"][0]["source"] == "local"

    def test_unreadable_input_yields_no_claims(self):
        for junk in (None, [], "x", 5, {}):
            got = R.claims_from_drill_report(junk)
            assert got["carry_leg_claims"] == []

    def _legacy_run(self, records=None, ledger=None):
        got = R.claims_from_drill_report(
            legacy_report(), legacy_ledger() if ledger is None else ledger)
        return R.reconcile(carry_claims=got["carry_leg_claims"],
                           funding_claims=got["funding_claims"],
                           funding_window=got.get("funding_window"),
                           captures=[R.capture_from_records(records or build())],
                           now_ms=NOW)

    def test_the_exit_link_is_found_by_its_numbers_and_that_is_stated(self):
        report = self._legacy_run()
        assert [v["resolved"] for v in report["carry_legs"]] == [True, True]
        assert "LEG_LINK_INFERRED" in report["carry_legs"][1]["issues"]
        assert report["carry_cycle"][0]["resolved"] is True
        # the inferred link keeps the headline permission shut
        assert report["pnl_claim_allowed"] is False

    def test_the_window_total_must_equal_what_the_venue_paid(self):
        report = self._legacy_run(ledger=legacy_ledger(funding_venue_usd=0.5))
        assert any(i.startswith("FUNDING_OUTSIDE_HOLD")
                   for i in report["carry_cycle"][0]["issues"])

    def test_two_candidate_closes_are_ambiguous_not_guessed(self):
        extra = [lambda prev: create_rec("BB-carr-3-other", "ord-3", "Buy",
                                         reduce=True, prev=prev, at=CLOSE_MS + 70_000)]
        report = self._legacy_run(build(extra=extra))
        assert "NO_VENUE_IDENTIFIERS" in report["carry_legs"][1]["issues"]
        assert report["carry_legs"][1]["resolved"] is False

    def test_the_first_real_capture_shape_stays_unresolved(self):
        """Attempt 1: entry + abort close, creates and order reads only (no
        execution rows), no ledger: nothing reconciles, and the close is an
        order the report never claimed."""
        records = build(drop=("exec_open", "exec_close", "settlement"))
        got = R.claims_from_drill_report(legacy_report())
        report = R.reconcile(carry_claims=got["carry_leg_claims"],
                             captures=[R.capture_from_records(records)], now_ms=NOW)
        assert report["carry_legs"][0]["provenance"] == R.CLAIMED_TESTNET_FILL
        assert f"UNCLAIMED_ORDER:{CLOSE_LINK}" in report["carry_cycle"][0]["issues"]
        assert report["pnl_claim_allowed"] is False


class TestTheLedgerSummaryCountsCarryLegs:
    def test_a_cash_ledger_agrees_with_the_legs_it_summarises(self):
        report = run(ledgers=[legacy_ledger()])
        summary = report["ledger_summaries"][0]
        assert "LEDGER_FILL_COUNT_DISAGREES" not in summary["issues"]
        assert "LEDGER_FEES_DISAGREE_WITH_FILLS" not in summary["issues"]
        assert report["pnl_claim_allowed"] is True

    def test_a_ledger_with_a_different_fee_total_blocks(self):
        report = run(ledgers=[legacy_ledger(fees_usd=0.5)])
        assert "LEDGER_FEES_DISAGREE_WITH_FILLS" in report["ledger_summaries"][0]["issues"]
        assert report["pnl_claim_allowed"] is False


class TestTheCommandLine:
    def _files(self, tmp_path, records, report, ledger):
        ev = tmp_path / "ev" / "cap.jsonl"
        ev.parent.mkdir()
        ev.write_text("".join(json.dumps(r) + "\n" for r in records))
        rp, lp = tmp_path / "drill.json", tmp_path / "ledger.json"
        rp.write_text(json.dumps(report))
        lp.write_text(json.dumps(ledger))
        return ["--evidence", str(ev), "--drill-report", str(rp),
                "--ledger", str(lp), "--now-ms", str(NOW),
                "--json", str(tmp_path / "out" / "r.json")]

    def test_a_new_format_report_reconciles_end_to_end(self, tmp_path):
        claims = {"schema": "drill_claims/1", "carry_leg_claims": leg_claims(),
                  "funding_claims": funding_claims()}
        args = self._files(tmp_path, build(), {"claims": claims}, legacy_ledger())
        assert R.main(args) == 0
        out = json.load(open(tmp_path / "out" / "r.json"))
        assert out["pnl_claim_allowed"] is True
        assert out["carry_cycle"][0]["economics"]["settlements"] == 1

    def test_an_old_report_with_its_ledger_is_reconciled_but_not_allowed(self, tmp_path):
        args = self._files(tmp_path, build(), legacy_report(), legacy_ledger())
        assert R.main(args) == 2
        out = json.load(open(tmp_path / "out" / "r.json"))
        assert out["carry_cycle"][0]["resolved"] is True
        assert out["pnl_claim_allowed"] is False

    def test_an_unreadable_report_is_a_refusal_not_a_traceback(self, tmp_path):
        bad = tmp_path / "bad.json"
        bad.write_text("{")
        assert R.main(["--drill-report", str(bad), "--now-ms", str(NOW)]) == 3
