"""Offline proof of the testnet exchange adapter: lifecycle against the in-process fake Bybit testnet and every injected fault. After each: no untracked exposure, engine states
agree with the venue, the session record is consistent, nothing is sent after a halt."""
from __future__ import annotations

import json
import os
import sys
from dataclasses import replace
from decimal import Decimal as D

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "tools"))

import bybit_connection as bc  # noqa: E402
import entry_authority as EA  # noqa: E402
import ms_authority as MA  # noqa: E402
import ms_testnet_adapter as AD  # noqa: E402
import ms_testnet_executor as TE  # noqa: E402
import ms_testnet_session as S  # noqa: E402
import ms_testnet_transport as TT  # noqa: E402
import ms_types as T  # noqa: E402
from ms_execution import OrderRequest  # noqa: E402
from ms_fake_bybit_testnet import FakeBybitTestnet, SimulatedCrash  # noqa: E402
from ms_testnet_world import World, begin, restart, resting_plan  # noqa: E402

CREATE = "/v5/order/create"


@pytest.fixture
def w(tmp_path, monkeypatch):
    world = World(tmp_path, monkeypatch)
    world.ready()
    return world


def creates(w):
    return w.requests_to(CREATE)


def executed(w):
    return {k: v for k, v in w.fake.create_calls.items() if v}


def consistent(w, run):
    """No untracked exposure and the engine agrees with the venue's OWN reads."""
    view = run.ex.venue_state()
    assert view["complete"] and view["positions"] is not None
    exp = run.engine.positions()
    for k, v in view["positions"].items():
        mine = float(exp.get(k, 0))
        base = float(run.baseline.get(k, 0.0)) if k.startswith("spot:") else 0.0
        assert abs((v - base) - mine) < 1e-9, (k, v, base, mine)
    assert not view["foreign_open_orders"]
    return view


def drive(w, run, n=6):
    for _ in range(n):
        w.clock.sleep(3)
        now = w.clock.now()
        run.engine.step(now, market_ts_ms=now, account_ts_ms=now)
        run.bookkeep()
    return run.state()


# ---------------------------------------------------------------- lifecycle
def test_entry_partial_complete_hedge_exit_flat(w):
    run, ctx, rec = begin(w, submit=False, group_plan=resting_plan(w))
    eng = run.engine
    now = w.clock.now()
    eng.submit_group(run.gid, now, market_ts_ms=now, account_ts_ms=now)
    assert run.state() == "SUBMITTED" and len(executed(w)) == 2    # both legs rest
    w.fake.fill("gr1-p", "0.0004")                                  # partial fills over time
    w.fake.fill("gr1-s", "0.0006")
    drive(w, run, 1)
    assert run.state() in ("PARTIAL", "UNHEDGED", "SUBMITTED")
    consistent(w, run)
    w.fake.fill("gr1-p", "0.0006")
    w.fake.fill("gr1-s", "0.0004")
    assert drive(w, run, 1) == "HEDGED"
    view = consistent(w, run)
    assert view["positions"]["linear:BTCUSDT"] == pytest.approx(-0.001)
    eng.request_exit(run.gid, w.clock.now(), "TEST")
    assert drive(w, run, 3) == "CLOSED"
    view = consistent(w, run)
    assert view["positions"]["linear:BTCUSDT"] == 0 and abs(view["positions"]["spot:BTCUSDT"] - run.baseline["spot:BTCUSDT"]) < 1e-9
    assert eng.groups[run.gid].flat_verified is True
    assert S.record_refusals(S.load_record(ctx)) == []
    assert all(n == 1 for n in executed(w).values())               # every link id was created exactly once


def test_taker_pair_fills_immediately_and_exits_with_reduce_only(w):
    run, ctx, rec = begin(w)
    assert drive(w, run, 1) == "HEDGED"
    run.engine.request_exit(run.gid, w.clock.now(), "T")
    assert drive(w, run, 3) == "CLOSED"
    bodies = [r[3] for r in creates(w)]
    assert any('"reduceOnly":true' in b and '"category":"linear"' in b for b in bodies)
    consistent(w, run)


# ---------------------------------------------------------------- filters / precision BEFORE sending
FL = TE.Filters("linear", "BTCUSDT", "Trading", "BTC", D("0.1"), D("0.001"), D("0.001"), D("1000"), D("5"), 480)


@pytest.mark.parametrize("kw,code", [
    (dict(qty="0.0015"), "QTY_NOT_ON_STEP"), (dict(qty="0.0005"), "QTY_BELOW_MIN"), (dict(qty="0"), "QTY_NOT_POSITIVE"), (dict(qty="2000"), "QTY_ABOVE_MAX"),
    (dict(qty="abc"), "QTY_NOT_A_NUMBER"), (dict(order_type="Limit", price="60000.05"), "PRICE_NOT_ON_TICK"), (dict(order_type="Limit", price=None), "PRICE_INVALID"),
    (dict(order_type="Limit", price="1.0"), "NOTIONAL_BELOW_MIN"), (dict(ref_price=None), "NOTIONAL_UNVERIFIABLE"),
])
def test_filter_violations_name_every_broken_rule(kw, code):
    base = dict(side="Sell", order_type="Market", qty="0.001", price=None, ref_price=D("60000"))
    base.update(kw)
    assert code in TE.filter_violations(FL, **base)


def test_a_valid_order_has_no_violations_and_a_halted_symbol_is_one():
    assert TE.filter_violations(FL, side="Sell", order_type="Market", qty="0.001", price=None, ref_price=D("60000")) == []
    assert "INSTRUMENT_NOT_TRADING:PreLaunch" in TE.filter_violations(replace(FL, status="PreLaunch"), side="Sell", order_type="Market", qty="0.001", price=None, ref_price=D("1"))


def test_spot_market_buy_is_refused_locally_because_its_qty_unit_is_quote_value():
    spot = TE.Filters("spot", "BTCUSDT", "Trading", "BTC", D("0.01"), D("0.000001"), D(0), D("1000"), D("1"), None)
    assert "SPOT_MARKET_BUY_UNIT_AMBIGUOUS" in TE.filter_violations(spot, side="Buy", order_type="Market", qty="0.001", price=None, ref_price=D("60000"))
    assert "SPOT_MARKET_BUY_UNIT_AMBIGUOUS" not in TE.filter_violations(spot, side="Sell", order_type="Market", qty="0.001", price=None, ref_price=D("60000"))


def test_an_order_that_violates_the_instrument_rules_is_refused_before_any_request(w):
    run, ctx, rec = begin(w, submit=False)
    before = len(w.fake.requests)
    bad = T.LegPlan("q", "BTCUSDT", "linear", "Sell", "0.0015", "Market", None, False, False, "IOC")      # off the 0.001 step
    ack = run.ex.send_order(OrderRequest(run.gid, "c", "d", bad, f"{run.gid}-q"))
    assert ack.status == "REJECTED" and ack.reason.startswith("FILTER:") and "QTY_NOT_ON_STEP" in ack.reason
    assert [r for r in w.fake.requests[before:] if r[1] == CREATE] == [] and run.ex.sent_creates == []


def test_filters_parse_the_spot_rules_from_basePrecision_and_minOrderAmt(w):
    run, _, _ = begin(w, submit=False)
    f = run.ex.filters("spot", "BTCUSDT")
    assert f.qty_step == D("0.000001") and f.min_notional == D("1") and f.tick == D("0.01") and f.base_coin == "BTC"
    assert run.ex.filters("linear", "BTCUSDT").min_notional == D("5")
    with pytest.raises(TE.ExecutorError):
        TE.parse_filters("linear", {"symbol": "X", "lotSizeFilter": {}, "priceFilter": {}})


# ---------------------------------------------------------------- lost ack: query by orderLinkId BEFORE any resend
def test_lost_ack_after_acceptance_is_adopted_by_a_link_id_query_and_nothing_is_resent(w):
    w.fake.inject(CREATE, "lose_response", match=lambda d: d["orderLinkId"].endswith("-p"))
    run, ctx, rec = begin(w)
    link = f"{run.gid}-p"
    assert w.fake.create_calls[link] == 1 and run.ex.sent_creates.count(link) == 1       # the create left ONCE
    assert w.requests_to("/v5/order/realtime")                                           # and the venue was asked under the same link id
    assert drive(w, run, 2) == "HEDGED"
    consistent(w, run)
    assert all(n == 1 for n in executed(w).values())


def test_lost_ack_with_an_unreadable_lookup_is_unknown_outcome_then_reconcile_adopts_without_duplicate(w):
    w.fake.inject(CREATE, "lose_response", match=lambda d: d["orderLinkId"].endswith("-p"))
    w.fake.inject("/v5/order/realtime", "timeout", times=99, match=lambda q: "orderLinkId" in q)     # the by-link lookup is unreadable (baseline reads are not)
    run, ctx, rec = begin(w)
    link = f"{run.gid}-p"
    assert run.state() == "UNKNOWN_OUTCOME"
    assert run.ex.sent_creates.count(link) == 1                                           # no blind resend
    w.fake.faults.clear()
    assert drive(w, run, 3) in ("HEDGED", "UNHEDGED", "EXITING", "CLOSED")
    assert w.fake.create_calls[link] == 1 and run.ex.sent_creates.count(link) == 1
    consistent(w, run)


def test_lost_request_is_resent_exactly_once_and_only_after_a_complete_read_shows_no_order(w):
    w.fake.inject(CREATE, "timeout", times=1, match=lambda d: d["orderLinkId"].endswith("-p"))
    run, ctx, rec = begin(w)
    link = f"{run.gid}-p"
    assert run.state() == "UNKNOWN_OUTCOME" and w.fake.create_calls.get(link, 0) == 0
    run.engine.step(w.clock.now())                                                        # too early: resend_after_ms not elapsed
    assert run.ex.sent_creates.count(link) == 1
    drive(w, run, 3)
    assert run.ex.sent_creates.count(link) == 2 and w.fake.create_calls[link] == 1        # ONE idempotent resend, executed once
    consistent(w, run)


def test_rate_limit_on_create_is_not_a_rejection_and_is_resolved_by_query_first(w):
    w.fake.inject(CREATE, "rate_limit", times=1)
    run, ctx, rec = begin(w)
    first = f"{run.gid}-p"
    assert run.state() == "UNKNOWN_OUTCOME" and w.requests_to("/v5/order/realtime")
    drive(w, run, 4)
    assert w.fake.create_calls[first] == 1
    consistent(w, run)


@pytest.mark.parametrize("kind", ["http500", "malformed", "no_retcode", "http403"])
def test_malformed_or_non_venue_answers_to_a_create_are_unknown_outcome_never_accepted_or_rejected(w, kind):
    w.fake.inject(CREATE, kind, times=1, match=lambda d: d["orderLinkId"].endswith("-p"))
    run, ctx, rec = begin(w)
    leg = run.engine.groups[run.gid].legs["p"]
    assert leg.status == "UNKNOWN" and run.state() == "UNKNOWN_OUTCOME"
    drive(w, run, 4)
    consistent(w, run)


def test_duplicate_orderLinkId_answer_adopts_the_existing_order(w):
    run, ctx, rec = begin(w, submit=False)
    leg = run.group_plan.legs[0]
    req = OrderRequest(run.gid, "c", "d", leg, f"{run.gid}-{leg.leg_id}")
    a1 = run.ex.send_order(req)
    a2 = run.ex.send_order(req)                                                           # an engine-level retry of the same link: venue says 110072
    assert a1.status == a2.status == "ACCEPTED" and a1.order_id == a2.order_id
    assert w.fake.create_calls[req.link_id] == 2 and len([o for o in w.fake.orders.values() if o["orderLinkId"] == req.link_id]) == 1


# ---------------------------------------------------------------- fills
def test_duplicate_execution_events_are_counted_once(w):
    w.fake.dup_executions = True
    run, ctx, rec = begin(w)
    assert drive(w, run, 2) == "HEDGED"
    consistent(w, run)
    view = run.ex.venue_state()
    ids = [x["fill_id"] for x in view["executions"]]
    assert len(ids) == len(set(ids)) == len(w.fake.execs)
    assert run.engine.groups[run.gid].legs["p"].filled == D("0.001")


def test_spot_fee_charged_in_the_received_coin_is_reported_net_so_the_engine_equals_the_venue(w):
    w.fake.spot_fee_in_base = True
    run, ctx, rec = begin(w)
    assert drive(w, run, 2) == "HEDGED"
    consistent(w, run)
    run.engine.request_exit(run.gid, w.clock.now(), "T")
    assert drive(w, run, 4) == "CLOSED"                                                    # the exit sells the NET quantity: flat on the venue's own read
    consistent(w, run)
    x = [e for e in run.ex.venue_state()["executions"] if e["category"] == "spot" and e["side"] == "Buy"][0]
    assert x["qty"] == pytest.approx(0.001 * 0.999) and x["fee_usd"] == pytest.approx(0.001 * 0.001 * 60001.01)


def test_executions_outside_our_links_are_unattributed_not_fills(w):
    run, ctx, rec = begin(w)
    assert drive(w, run, 1) == "HEDGED"
    w.fake.force_close("BTCUSDT", "0.0005")                                                # liquidation / ADL: an execution under NO client link id
    view = run.ex.venue_state()
    assert all(x["link_id"].startswith(run.gid) for x in view["executions"]) and run.ex.unattributed
    run.engine.reconcile(w.clock.now())
    assert any(h.startswith("POSITION_MISMATCH") for h in run.engine.halts)                 # the position moved without us: new exposure halts


# ---------------------------------------------------------------- restart / crash
def test_restart_with_open_orders_recovers_from_the_journal_and_the_venue(w):
    run, ctx, rec = begin(w, submit=False, group_plan=resting_plan(w))
    now = w.clock.now()
    run.engine.submit_group(run.gid, now, market_ts_ms=now, account_ts_ms=now)
    w.fake.fill("gr1-p", "0.0004")
    drive(w, run, 1)
    before = (run.state(), dict(run.engine.positions()))
    sent_before = len(creates(w))
    run2 = restart(w, ctx, "gr1")                                                         # the process died; a new one starts
    assert run2.state() == before[0] and run2.engine.positions() == before[1]
    assert len(creates(w)) == sent_before                                                 # recovery sends no order
    assert run2.engine.groups["gr1"].legs["p"].status == "LIVE"
    w.fake.fill("gr1-p", "0.0006")
    w.fake.fill("gr1-s", "0.001")
    drive(w, run2, 2)
    assert run2.state() == "HEDGED"
    consistent(w, run2)


def test_crash_between_acceptance_and_persistence_is_recovered_without_a_duplicate(w):
    run, ctx, rec = begin(w, submit=False)
    w.fake.inject(CREATE, "crash_after", match=lambda d: d["orderLinkId"].endswith("-p"))
    now = w.clock.now()
    with pytest.raises(SimulatedCrash):
        run.engine.submit_group(run.gid, now, market_ts_ms=now, account_ts_ms=now)
    link = f"{run.gid}-p"
    assert w.fake.create_calls[link] == 1                                                 # accepted at the venue, never journalled as acked
    run2 = restart(w, ctx)
    drive(w, run2, 5)
    assert w.fake.create_calls[link] == 1 and run2.ex.sent_creates.count(link) == 0       # the NEW process never resent it
    assert run2.state() in ("CLOSED", "FAILED", "HEDGED", "UNHEDGED", "EXITING")
    consistent(w, run2)


# ---------------------------------------------------------------- stale / unreadable / conflicting
def test_stale_account_data_refuses_new_exposure_and_nothing_reaches_the_venue(w):
    run, ctx, rec = begin(w, submit=False)
    w.clock.sleep(30)                                                                     # > max_account_age_ms (10 s)
    w.fake.inject("/v5/account/wallet-balance", "timeout", times=99)
    now = w.clock.now()
    run.engine.submit_group(run.gid, now, market_ts_ms=now, account_ts_ms=now)
    assert creates(w) == [] and w.fake.create_calls == {}
    refs = [r for _l, rs in run.authority.refusals for r in rs]
    assert any("ACCOUNT_DATA_STALE" in r or "STATE_UNAVAILABLE" in r for r in refs), refs
    w.fake.faults.clear()
    consistent(w, run)


def test_stale_book_refuses_new_exposure(w):
    run, ctx, rec = begin(w, submit=False)
    w.clock.sleep(30)
    w.fake.inject("/v5/market/orderbook", "timeout", times=99)                              # only the BOOK is unreadable: the data cached from before is now old
    now = w.clock.now()
    run.engine.submit_group(run.gid, now, market_ts_ms=now, account_ts_ms=now)
    assert creates(w) == []
    refs = [r for _l, rs in run.authority.refusals for r in rs]
    assert any("BOOK_STALE" in r for r in refs), refs


def test_conflicting_position_halts_new_exposure_and_nothing_is_sent_after_the_halt(w):
    run, ctx, rec = begin(w, submit=False)
    w.fake.inject_position("ETHUSDT", -2)                                                  # exposure the engine did not open
    run.engine.reconcile(w.clock.now())
    assert any(h.startswith("POSITION_MISMATCH") for h in run.engine.halts)
    n = len(creates(w))
    now = w.clock.now()
    assert run.engine.submit_group(run.gid, now, market_ts_ms=now, account_ts_ms=now).value == "FAILED"
    assert len(creates(w)) == n == 0


def test_hedge_mode_or_unreadable_positions_are_unknown_not_flat(w):
    w.fake.position_idx_hedge = True
    run, ctx, rec = begin(w, submit=False)
    view = run.ex.venue_state()
    assert view["positions"] is None and view["complete"] is False
    run.engine.reconcile(w.clock.now())
    assert "UNKNOWN_VENUE_POSITION" in run.engine.halts


def test_a_failed_or_paginated_incomplete_read_makes_the_view_incomplete(w, monkeypatch):
    run, ctx, rec = begin(w, submit=False)
    w.fake.inject("/v5/execution/list", "http500", times=99)
    v = run.ex.venue_state()
    assert v["complete"] is False
    w.fake.faults.clear()
    w.fake.page_size = 1
    w.fake.inject_position("BTCUSDT", -1)
    w.fake.inject_position("ETHUSDT", -1)
    monkeypatch.setattr(TE, "MAX_PAGES", 1)                                                # still paging after the cap => incomplete, never "flat"
    v = run.ex.venue_state()
    assert v["complete"] is False and v["positions"] is None


def test_an_incomplete_read_never_verifies_flat(w):
    run, ctx, rec = begin(w)
    drive(w, run, 1)
    run.engine.request_exit(run.gid, w.clock.now(), "T")
    w.fake.inject("/v5/position/list", "timeout", times=999)
    drive(w, run, 4)
    assert run.state() != "CLOSED" and run.engine.groups[run.gid].flat_verified is False
    assert "UNKNOWN_VENUE_POSITION" in run.engine.halts


# ---------------------------------------------------------------- rejects, cancel race, kill switch
def test_a_rejected_leg_is_unhedged_then_unwound_reduce_only_to_flat(w):
    w.fake.reject["-s"] = (170131, "Insufficient balance")
    run, ctx, rec = begin(w)
    assert run.engine.groups[run.gid].legs["s"].status == "REJECTED"
    drive(w, run, 4)
    view = consistent(w, run)
    assert run.state() in ("CLOSED", "FAILED") and abs(view["positions"]["linear:BTCUSDT"]) < 1e-12
    assert S.record_refusals(S.load_record(ctx)) == []


def test_permanent_venue_rejects_are_rejections_with_their_retcode(w):
    run, ctx, rec = begin(w, submit=False)
    w.fake.reject["-p"] = (110007, "Available balance is too low")
    ack = run.ex.send_order(OrderRequest(run.gid, "c", "d", run.group_plan.legs[0], f"{run.gid}-p"))
    assert ack.status == "REJECTED" and ack.reason.startswith("RET:110007")


def test_reduce_only_without_a_position_is_rejected_by_the_venue(w):
    run, ctx, rec = begin(w, submit=False)
    leg = T.LegPlan("x1", "BTCUSDT", "linear", "Buy", "0.001", "Market", None, False, True, "IOC")
    ack = run.ex.send_order(OrderRequest(run.gid, "c", "d", leg, f"{run.gid}-x1"))
    assert ack.status == "REJECTED"                                                       # (the authority refuses it first: group exits are bound to an opened symbol)


def test_cancel_of_a_resting_order_and_the_cancel_race_with_a_fill(w):
    run, ctx, rec = begin(w, submit=False, group_plan=resting_plan(w))
    now = w.clock.now()
    run.engine.submit_group(run.gid, now, market_ts_ms=now, account_ts_ms=now)
    leg_p, leg_s = (run.engine.groups[run.gid].legs[k] for k in ("p", "s"))
    req = lambda l: OrderRequest(run.gid, "c", "d", l.plan, l.link)                       # noqa: E731
    assert run.ex.cancel_order(req(leg_p)) == "CANCELLED"
    assert run.ex.cancel_order(req(leg_p)) == "CANCELLED"                                  # already cancelled: idempotent answer
    w.fake.fill(leg_s.link, "0.001")                                                       # the fill wins the race
    assert run.ex.cancel_order(req(leg_s)) == "TOO_LATE"
    ghost = replace(leg_s.plan, leg_id="zz")
    assert run.ex.cancel_order(OrderRequest(run.gid, "c", "d", ghost, f"{run.gid}-zz")) == "NOT_FOUND"
    w.fake.inject("/v5/order/cancel", "timeout")
    other = run.engine.groups[run.gid].legs["p"]
    with pytest.raises(Exception):
        run.ex.cancel_order(req(other))                                                   # a lost cancel raises: the engine records LOST and reconciles


def test_kill_switch_engaged_mid_session_exits_flat_and_sends_no_new_entry(w):
    w.run("plan", "--symbol", "BTCUSDT", "--notional-usd", "90", "--hold-seconds", "60")
    open(os.path.join(w.state_dir, "MS_KILL"), "w").write("stop")
    d = w.plan_doc()["plan_digest"]
    assert w.run("execute", "--confirm-plan-digest", d, typed=d) == 2                       # engaged BEFORE: refused, nothing sent
    assert w.fake.requests == [] or all(r[0] == "GET" for r in w.fake.requests)
    os.unlink(os.path.join(w.state_dir, "MS_KILL"))
    real_sleep = w.clock.sleep
    n = {"i": 0}

    def sleep_then_kill(s):
        n["i"] += 1
        if n["i"] == 2:
            open(os.path.join(w.state_dir, "MS_KILL"), "w").write("stop")
        real_sleep(s)
    w.clock.sleep = sleep_then_kill
    assert w.execute(d) == 0, w.text()
    rec = w.record()
    assert rec["flat_verified"] is True and w.fake.positions["BTCUSDT"]["size"] == 0
    reduce_only = [r for r in creates(w) if '"reduceOnly":true' in r[3]]
    assert reduce_only and len(creates(w)) == 4                                              # 2 entries + 2 exits; the kill added no entry


def test_executor_halt_blocks_entries_but_not_reduce_only_exits(w):
    run, ctx, rec = begin(w, submit=False)
    run.ex.halt("TEST")
    n = len(creates(w))
    ack = run.ex.send_order(OrderRequest(run.gid, "c", "d", run.group_plan.legs[0], f"{run.gid}-p"))
    assert ack.status == "REJECTED" and "EXECUTOR_HALTED" in ack.reason and len(creates(w)) == n


# ---------------------------------------------------------------- the final boundary: nothing bypasses the registered, risk-approved group
def test_no_manual_call_can_bypass_the_registered_order_group(w):
    run, ctx, rec = begin(w, submit=False)
    c = run.client
    bodies = [
        {"category": "linear", "symbol": "BTCUSDT", "side": "Sell", "orderType": "Market", "qty": "0.001", "orderLinkId": "unregistered-p"},         # no such group
        {"category": "linear", "symbol": "BTCUSDT", "side": "Sell", "orderType": "Market", "qty": "0.002", "orderLinkId": f"{run.gid}-p"},          # qty is not the plan's
        {"category": "linear", "symbol": "ETHUSDT", "side": "Sell", "orderType": "Market", "qty": "0.001", "orderLinkId": f"{run.gid}-p"},          # symbol is not the plan's
        {"category": "linear", "symbol": "BTCUSDT", "side": "Sell", "orderType": "Market", "qty": "0.001", "orderLinkId": f"{run.gid}-p", "positionIdx": 1},
        {"category": "linear", "symbol": "BTCUSDT", "side": "Sell", "orderType": "Market", "qty": "0.001"},                                        # no link id
    ]
    for b in bodies:
        with pytest.raises(bc.OrderNotSent):
            c._request("POST", CREATE, body=b, signed=True, retries=0)
    assert creates(w) == []
    # the matching body IS accepted (positive control: the refusals above are about the body, not a dead path)
    ok = run.ex.body_for(run.group_plan.legs[0], f"{run.gid}-p")
    assert c._request("POST", CREATE, body=ok, signed=True, retries=0)["retCode"] == 0


def test_a_client_without_the_authority_refuses_every_entry(w, monkeypatch):
    monkeypatch.setattr(bc, "DEFAULT_ENTRY_AUTHORITY", None)                               # conftest installs a permissive stand-in for the suite; production has none
    fake = w.fake
    client, _ = TT.build_client(TT.TestnetCredentials.from_mapping(w.env), fake)
    with pytest.raises(bc.OrderNotSent, match="NO_ENTRY_AUTHORITY"):
        client._request("POST", CREATE, body={"category": "linear", "symbol": "BTCUSDT", "side": "Sell", "orderType": "Market", "qty": "0.001", "orderLinkId": "g-p"}, signed=True)
    assert creates(w) == []


def test_authority_admission_matrix_every_other_combination_is_refused(w, monkeypatch):
    policy = AD.policy_from_manifest(S._load_capability_manifest()[0])

    def build(transport, *, enabled=True, venue="testnet", binding=TT.KEY_ENV):
        monkeypatch.setattr(AD, "enabled", lambda path=None: enabled)
        p = dict(policy, venue=venue)
        return MA.MultiSymbolAuthority(policy=p, transport=transport, state_dir=str(w.tmp / "adm"), strategy="s", state_provider=lambda: {}, snapshot_provider=lambda: {},
                                       kill_switch_reader=lambda: (False, ""), testnet=None if binding is None else MA.TestnetBinding(binding))
    good = TT.TestnetTransport(w.fake)
    assert build(good).mode == "TESTNET"                                                       # positive control: all four conditions
    with pytest.raises(MA.AuthorityConfigError, match="TESTNET_CAPABILITY_DISABLED"):
        build(good, enabled=False)
    for venue in ("mainnet", "demo", "eu", "Testnet"):
        with pytest.raises(MA.AuthorityConfigError):
            build(good, venue=venue)
    for name in ("BYBIT_API_KEY", "BYBIT_API_SECRET", TT.SECRET_ENV, "BYBIT_TESTNET_DEDICATED_API_KEY", None):
        with pytest.raises(MA.AuthorityConfigError):
            build(good, binding=name)
    with pytest.raises(MA.AuthorityConfigError, match="TRANSPORT_NOT_TESTNET_EXECUTOR_CAPABILITY"):
        build(w.fake)                                                                          # the raw fake / any non-TestnetTransport object
    with pytest.raises(MA.AuthorityConfigError, match="TRANSPORT_NOT_TESTNET_EXECUTOR_CAPABILITY"):
        build(bc.RequestsTransport())


def test_install_binds_the_authority_to_this_transport_and_the_testnet_venue(w):
    run, ctx, rec = begin(w, submit=False)
    other, _ = TT.build_client(TT.TestnetCredentials.from_mapping(w.env), w.fake)
    with pytest.raises(MA.AuthorityConfigError, match="CLIENT_TRANSPORT_IS_NOT_THE_BOUND_TRANSPORT"):
        MA.install(other, run.authority)
    assert run.client.entry_authority is run.authority and run.authority.bound_transport is run.client.transport


# ---------------------------------------------------------------- canonical ledger + reconciliation (worker F) and the asynchronous exit sequence
def settle_view(w, run):
    now = w.clock.now()
    run.feed_ledger()
    run.reconcile_step(now)


def drive_full(w, run, n=6):
    for _ in range(n):
        w.clock.sleep(3)
        now = w.clock.now()
        run.engine.step(now, market_ts_ms=now, account_ts_ms=now)
        run.bookkeep()
        settle_view(w, run)
    return run.state()


def test_ledger_engine_venue_and_reservations_agree_through_the_whole_lifecycle(w):
    run, ctx, rec = begin(w)
    assert drive_full(w, run, 2) == "HEDGED"
    assert run.recon_report.ok, run.recon_report.codes
    assert run.venue_recon["venue_reconciled"] is True, run.venue_recon["findings"]
    assert {k: float(v) for k, v in run.ledger.positions().items()} == {"BTCUSDT": pytest.approx(-0.001), "SPOT:BTCUSDT": pytest.approx(0.001)}
    run.engine.request_exit(run.gid, w.clock.now(), "T")
    assert drive_full(w, run, 4) == "CLOSED"
    assert run.recon_report.ok, run.recon_report.codes
    assert run.venue_recon["venue_reconciled"] is True, run.venue_recon["findings"]
    assert all(abs(float(v)) < 1e-12 for v in run.ledger.positions().values())              # exits are booked into the entry leg's position: the pair reads flat
    cn = run.ledger.canonical_net()
    assert cn["fees"] == pytest.approx(sum(float(e["execFee"]) for e in w.fake.execs))
    assert cn["funding"] is None and cn["net"] is None and cn["net_complete"] is False       # funding was not read: unknown, never zero
    assert cn["price_leg"] is not None and run.ledger.integrity_flags() == []
    assert {a for a in run.ledger._acks} == {(run.gid, l) for l in run.engine.groups[run.gid].legs}   # every journalled ack reached the ledger


def test_a_ledger_that_disagrees_with_the_venue_halts_new_exposure(w):
    run, ctx, rec = begin(w)
    assert drive_full(w, run, 2) == "HEDGED"
    from ms_types import Fill
    run.ledger.record_fill(Fill("bogus-1", run.gid, "p", "BTCUSDT", "Sell", 0.5, 60000.0, 0.0, "USDT", "TAKER", w.clock.now(), "MEASURED"), category="linear")
    settle_view(w, run)
    assert not run.recon_report.ok and any(h.startswith("RECONCILE_DISAGREEMENT") for h in run.engine.halts) and run.ex.halted == "RECONCILE_DISAGREEMENT"
    assert run.venue_recon["venue_reconciled"] is False
    n = len(creates(w))
    ack = run.ex.send_order(OrderRequest("gx", "c", "d", T.LegPlan("p", "BTCUSDT", "linear", "Sell", "0.001", "Market", None, False, False, "IOC"), "gx-p"))
    assert ack.status == "REJECTED" and len(creates(w)) == n                                  # nothing new leaves after the halt


def test_spot_exit_waits_for_the_perp_exit_fill_to_show_on_the_venue_read(w):
    w.fake.fill_delay_ms = 2500                                                              # fills arrive ASYNCHRONOUSLY, after the ack
    run, ctx, rec = begin(w)
    assert run.state() == "SUBMITTED" and run.ex.venue_state()["positions"]["linear:BTCUSDT"] == 0
    assert drive_full(w, run, 2) == "HEDGED"
    run.engine.request_exit(run.gid, w.clock.now(), "T")
    assert drive_full(w, run, 4) == "CLOSED"
    assert run.authority.refusals == [], run.authority.refusals                              # the authority was never asked for a spot sell it had to refuse
    legs = run.engine.groups[run.gid].legs
    assert not [l for l in legs.values() if l.status == "REJECTED"]
    exits = [e for e in w.fake.execs if e["orderLinkId"].split("-", 1)[1].startswith("x")]
    assert [e["category"] for e in exits] == ["linear", "spot"]                              # perp exit filled BEFORE the spot exit traded
    bodies = [json.loads(r[3]) for r in creates(w)]
    assert [b["category"] for b in bodies[-2:]] == ["linear", "spot"]
    consistent(w, run)
    assert run.recon_report.ok and run.venue_recon["venue_reconciled"] is True


def test_control_without_the_hold_the_same_sequence_is_refused_by_the_authority(w):
    """Control: the fake really reproduces the hazard (otherwise the test above proves nothing). With the hold off, the spot sell is authorised while the perp still shows short."""
    w.fake.fill_delay_ms = 2500
    run, ctx, rec = begin(w)
    run.hold_spot_exit = False
    drive_full(w, run, 2)
    run.engine.request_exit(run.gid, w.clock.now(), "T")
    drive_full(w, run, 1)
    codes = [r for _l, rs in run.authority.refusals for r in rs]
    assert "SPOT_SELL_NOT_PROVEN_HEDGE_KEEPING" in codes, codes


def test_a_spot_exit_whose_perp_never_closes_gives_up_after_the_hold_with_one_rejection(w):
    run, ctx, rec = begin(w)
    assert drive_full(w, run, 2) == "HEDGED"                                                 # the perp is short on the venue
    leg = T.LegPlan("xspot", "BTCUSDT", "spot", "Sell", "0.001", "Market", None, False, False, "IOC")
    t0 = w.clock.now()
    ack = run._await_perp_closed(OrderRequest(run.gid, "c", "d", leg, f"{run.gid}-xspot"))
    assert ack.status == "REJECTED" and ack.reason == "SPOT_EXIT_HELD_PERP_NOT_CLOSED" and w.clock.now() - t0 >= 20_000
    assert [b for b in (json.loads(r[3]) for r in creates(w)) if b["category"] == "spot" and b["side"] == "Sell"] == []


def test_the_delayed_exit_sequence_end_to_end_through_the_session_tool(w):
    w.fake.fill_delay_ms = 2500
    d = w.ready()
    assert w.execute(d) == 0, w.text()
    rec = w.record()
    assert rec["flat_verified"] is True and all(p["size"] == 0 for p in w.fake.positions.values())
    assert rec["net"]["funding_usd"] is None and rec["net"]["fees_usd"] == pytest.approx(sum(float(e["execFee"]) for e in w.fake.execs))
    assert S.record_refusals(rec) == [] and set(w.fake.create_calls.values()) == {1}
