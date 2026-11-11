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
from ms_execution import HELD_REASON as EX_HELD_REASON, OrderRequest  # noqa: E402
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


def _fee_charged_in(w, ccy, fee, *, category, side):
    """The venue charges the fee of every matching fill in ``ccy`` (an amount in THAT currency): the row says so, the wallet effects are left alone."""
    orig = w.fake._fill

    def fill(o, qty, px, *, taker, dup=False):
        orig(o, qty, px, taker=taker, dup=dup)
        if o["category"] == category and o["side"] == side:
            w.fake.execs[-1]["feeCurrency"], w.fake.execs[-1]["execFee"] = ccy, fee
    w.fake._fill = fill


@pytest.mark.parametrize("ccy", ["MNT", "USDC", "ETH"])
def test_a_spot_fee_in_a_currency_that_is_neither_the_quote_nor_the_base_coin_is_unknown_never_a_quantity_never_dollars(w, ccy):
    """REPRODUCED (red-team F, 'third-currency fee abandons hedge'): the executor took ANY non-USDT spot fee for the received coin. A BUY fee of 0.18 MNT was subtracted from the 0.001 BTC bought (the engine's
    spot leg went to -0.179, the group FAILED with the perp short and the spot long still on the venue) and valued at the BTC price (10,800 USD of fee, MEASURED). Only a fee in the base coin reduces the
    received quantity; a fee in any other currency is an UNKNOWN fee: the quantity is what the venue filled, the dollars are not invented."""
    _fee_charged_in(w, ccy, "0.18", category="spot", side="Buy")
    run, ctx, rec = begin(w)
    assert drive(w, run, 2) == "HEDGED", "the hedge must not be abandoned over a fee the engine cannot value"
    consistent(w, run)
    assert D(str(run.engine.positions()["spot:BTCUSDT"])) == D("0.001")
    x = [e for e in run.ex.venue_state()["executions"] if e["category"] == "spot" and e["side"] == "Buy"][0]
    assert x["qty"] == pytest.approx(0.001) and x["fee_known"] is False and x["prov"] == "UNKNOWN" and x["fee_usd"] == 0.0 and x["fee_signed_usd"] is None, x
    assert x["fee_ccy_venue"] == ccy and x["fee_ccy_unsupported"] is True


def test_a_session_whose_entry_fee_is_in_a_third_currency_ends_flat_and_publishes_no_fee_total(w):
    _fee_charged_in(w, "MNT", "0.18", category="spot", side="Buy")
    d = w.plan_doc()["plan_digest"]
    assert w.execute(d) == 0, w.text()
    rec = w.record()
    assert rec["flat_verified"] is True and all(p["size"] == 0 for p in w.fake.positions.values())
    assert rec["net"]["fees_usd"] is None, f"a fee nobody could value was published as {rec['net']['fees_usd']}"


def test_a_perp_fee_in_a_currency_other_than_the_quote_is_unknown_too_and_never_relabelled_usdt(w):
    _fee_charged_in(w, "MNT", "0.18", category="linear", side="Sell")
    run, ctx, rec = begin(w)
    assert drive(w, run, 2) == "HEDGED"
    x = [e for e in run.ex.venue_state()["executions"] if e["category"] == "linear"][0]
    assert x["fee_known"] is False and x["fee_usd"] == 0.0 and x["fee_ccy_venue"] == "MNT" and x["fee_ccy_unsupported"] is True and x["qty"] == pytest.approx(0.001)


def test_when_the_base_coin_cannot_be_read_no_fee_currency_but_the_quote_is_trusted(w):
    """Fail closed: with the instrument filters gone from the cache and unreadable, the executor cannot tell the base coin from a third currency, so a fee in the (really) base coin is UNKNOWN too, and
    the quantity is left as the venue filled it."""
    w.fake.spot_fee_in_base = True
    run, ctx, rec = begin(w)
    drive(w, run, 2)
    run.ex._filters.pop(("spot", "BTCUSDT"), None)
    w.fake.inject("/v5/market/instruments-info", "http500", times=50, match=lambda d: d.get("category") == "spot")
    row = {"execId": "E999999", "orderLinkId": f"{run.gid}-s", "orderId": "O1", "symbol": "BTCUSDT", "side": "Buy", "execQty": "0.001", "execPrice": "60000", "execFee": "0.000001",
           "feeCurrency": "BTC", "isMaker": False, "execTime": str(w.clock.now()), "execType": "Trade"}
    out = []
    run.ex._take_exec(row, "spot", out)
    assert out[0]["fee_known"] is False and out[0]["fee_ccy_unsupported"] is True and out[0]["qty"] == pytest.approx(0.001) and out[0]["fee_usd"] == 0.0


@pytest.mark.parametrize("ccy,side,known,qty_after,fee_usd", [
    ("BTC", "Buy", True, 0.003996, 0.24),                 # the received coin: net quantity, dollars at the fill price
    ("btc", "Buy", True, 0.003996, 0.24),                 # the venue's casing is not part of the meaning (review: the base was upper-cased but the fee currency was compared raw)
    (" Btc ", "Buy", True, 0.003996, 0.24),
    ("USDT", "Buy", True, 0.004, 0.004),                  # the quote: dollars as stated, the quantity untouched
    ("usdt", "Buy", True, 0.004, 0.004),
    ("BTC", "Sell", True, 0.004, 0.24),                   # a base-coin fee on a SELL is valued at the fill price, the sold quantity is what was sold
    ("MNT", "Sell", False, 0.004, 0.0),                   # a third currency on a SELL is unknown too
    ("MNT", "Buy", False, 0.004, 0.0),
])
def test_the_fee_currency_is_matched_case_insensitively_and_a_third_currency_is_unknown_on_either_side(w, ccy, side, known, qty_after, fee_usd):
    run, ctx, rec = begin(w, submit=False)
    fee = "0.000004" if ccy.strip().upper() == "BTC" else ("0.004" if ccy.strip().upper() == "USDT" else "0.18")
    row = {"execId": "E900001", "orderLinkId": f"{run.gid}-s", "orderId": "O1", "symbol": "BTCUSDT", "side": side, "execQty": "0.004", "execPrice": "60000", "execFee": fee,
           "feeCurrency": ccy, "isMaker": False, "execTime": str(w.clock.now()), "execType": "Trade"}
    out = []
    run.ex._take_exec(row, "spot", out)
    x = out[0]
    assert x["fee_known"] is known and x["qty"] == pytest.approx(qty_after) and x["fee_usd"] == pytest.approx(fee_usd), x
    assert x["fee_ccy_unsupported"] is (not known)


def test_the_base_coin_and_the_quote_fees_are_still_valued_exactly_as_before(w):
    run, ctx, rec = begin(w)
    drive(w, run, 2)
    xs = run.ex.venue_state()["executions"]
    assert all(e["fee_known"] is True and e["prov"] == "MEASURED" and not e.get("fee_ccy_unsupported") for e in xs) and all(e["fee_ccy_venue"] == "USDT" for e in xs)


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
    # the session now WAITS (bounded) for the first entry leg's async fill before it assesses and sends the second (a real exchange's order list trails its position read), so the
    # perp is already filled when begin() returns; the exit side below is what this test is about
    assert run.state() == "SUBMITTED"
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


@pytest.mark.parametrize("perp,group_spot,baseline,sale,goes,engine_spot", [
    ("-0.001", "0.004", "0",   "0.003", True,  None),                 # the excess of a partial hedge: the spot still covers the perp short afterwards
    ("-0.001", "0.004", "0.5", "0.003", True,  None),                 # the same with a pre-session spot holding the group never bought
    ("-0.001", "0.001", "0.5", "0.001", False, None),                 # the owner's OWN coin must not make the group's spot look like it covers the short
    ("-0.001", "0.004", "0",   "0.004", False, None),                 # selling it all leaves the perp naked
    ("-0.001", "0.004", "0",   "0.0031", False, None),                # one step too much
    ("-0.001", "0.003", "0",   "0.002", True,  None),                 # exactly covered afterwards (within the 1e-9 tolerance)
    ("0",      "0.004", "0",   "0.004", True,  None),                 # no perp left: nothing to keep hedged
    ("0.001",  "0.004", "0",   "0.003", False, None),                 # a perp LONG nobody planned is not a hedge: the sale still waits for a flat perp, as before
    # the wallet is not the only witness: the owner's own coin can GROW after the baseline was captured (a deposit, a faucet, a transfer), and then balance - baseline overstates the group's spot. The
    # engine's own book (what the group's fills made) caps it: BOTH must cover the short.
    ("-0.004", "0.024", "0",   "0.004", False, "0.004"),              # the wallet shows 0.02 BTC more than the baseline says: the full short is open, the sale waits (it used to leave at once)
    ("-0.001", "0.024", "0",   "0.003", True,  "0.004"),              # the same drift with a genuine excess: the engine's 0.004 still covers the 0.001 short after selling 0.003
    ("-0.001", "0.004", "0",   "0.003", False, "0.001"),              # the engine believes it holds only 0.001 (a fill it has not seen): the conservative reading holds the sale
    ("-0.001", "0.004", "0",   "0.003", False, "none"),               # the engine has no spot position at all: fail closed
    ("-0.001", "0.002", "0",   "0.002", False, "0.004"),              # the wallet shows LESS than the engine believes: the smaller of the two witnesses decides, in both directions
    ("-0.001", "0.004", "0",   "0.003", False, "abc"),                # an unreadable engine figure: fail closed
    ("-0.001", "0.004", "0",   "0.003", False, "no_engine"),          # no engine at all: fail closed
    ("-0.001", "0",     "0",   "0.001", False, "0.004"),              # the wallet shows no spot at all (the venue row is absent or zero): nothing covers the short, whatever the engine believes
])
def test_the_spot_exit_hold_lets_a_sale_through_only_while_the_group_spot_still_covers_the_perp_short(w, monkeypatch, perp, group_spot, baseline, sale, goes, engine_spot):
    run, ctx, rec = begin(w, submit=False)
    w.fake.inject_position("BTCUSDT", perp)
    w.fake.wallet["BTC"] = D(group_spot) + D(baseline)
    run.baseline = {"spot:BTCUSDT": float(baseline)}
    mine = {} if engine_spot in ("none", "no_engine") else {"spot:BTCUSDT": engine_spot or group_spot}
    if engine_spot == "no_engine":
        monkeypatch.setattr(run, "engine", None)
    else:
        monkeypatch.setattr(run.engine, "positions", lambda: dict(mine))
    leg = T.LegPlan("xspot", "BTCUSDT", "spot", "Sell", sale, "Market", None, False, False, "IOC")
    t0 = w.clock.now()
    ack = run._await_perp_closed(OrderRequest(run.gid, "c", "d", leg, f"{run.gid}-xspot"))
    if goes:
        assert ack is None and w.clock.now() - t0 < 5_000, f"the sale of {sale} was held although the spot still covers the short: {ack}"
    else:
        assert ack is not None and ack.status == "REJECTED" and ack.reason == EX_HELD_REASON and w.clock.now() - t0 >= 20_000, (ack, "the sale would have left the perp short uncovered")


def test_a_deposit_of_the_owners_own_coin_after_the_baseline_never_lets_the_spot_sale_leave_before_the_perp_is_closed(w):
    """REPRODUCED by the independent review: the owner's own BTC balance grew by 0.02 after the session's baseline was captured, the perp exit filled 2.5 s after its ack, and the new relaxation sold the
    spot at t=0 with the whole perp short still open (the old hold waited for the flat perp, whatever the wallet said)."""
    w.fake.fill_delay_ms = 2500
    run, ctx, rec = begin(w)
    assert drive_full(w, run, 2) == "HEDGED"
    w.fake.wallet["BTC"] = w.fake.wallet.get("BTC", D(0)) + D("0.02")                       # the owner's deposit, AFTER the baseline was read
    sent, orig = [], w.fake._create

    def create(d):
        sent.append((d["category"], d["side"], w.fake.positions["BTCUSDT"]["size"]))
        return orig(d)
    w.fake._create = create
    run.engine.request_exit(run.gid, w.clock.now(), "T")
    drive_full(w, run, 6)
    sells = [x for x in sent if x[0] == "spot" and x[1] == "Sell"]
    assert sells, f"the spot was never sold: {sent}"
    assert all(size == 0 for _c, _s, size in sells), f"a spot sale left while the perp short was still open: {sent}"


# ---- the unattended close-out of an ENTRY leg that never got an answer (the owner's unwind does the same by hand)
def _a_run_whose_spot_entry_got_no_answer(w, fault="rate_limit"):
    w.fake.inject(CREATE, fault, times=2, match=lambda d: d.get("category") == "spot" and d.get("side") == "Buy")       # the create of the second leg fails twice (the send and its one idempotent resend)
    run, ctx, rec = begin(w)
    drive(w, run, 1)                                                                         # one engine step: the perp fill is read, the second leg's idempotent resend fails too
    leg = run.engine.groups[run.gid].legs["s"]
    assert run.unresolved_entries() == [f"{run.gid}-s"] and leg.status == "UNKNOWN" and run.engine.groups[run.gid].legs["p"].status == "FILLED"
    return run, ctx, leg.sent_ms


def test_an_unanswered_entry_leg_is_closed_out_only_after_the_age_bound_and_the_filled_leg_then_unwinds(w):
    run, ctx, sent = _a_run_whose_spot_entry_got_no_answer(w)
    link = f"{run.gid}-s"
    for age in (0, 1, S.CLOSE_OUT_MIN_AGE_MS - 1):                                           # a create that left a moment ago may still reach the venue: closing it out could double the order
        w.clock.t = sent + age
        S.close_out_unanswered_entries(ctx, run)
        assert run.unresolved_entries() == [link] and "closed out" not in w.text(), f"closed out after only {age} ms"
    w.clock.t = sent + S.CLOSE_OUT_MIN_AGE_MS
    S.close_out_unanswered_entries(ctx, run)
    assert run.engine.groups[run.gid].legs["s"].status == "CANCELLED" and f"ENTRY LEG {link}" in w.text() and "closed out" in w.text()
    assert drive(w, run, 6) == "CLOSED", w.text()                                            # the perp that DID fill is unwound reduce-only instead of waiting for a human
    assert all(p["size"] == 0 for p in w.fake.positions.values()) and not w.fake.wallet.get("BTC"), "the venue is flat"
    assert link not in executed(w), "the closed-out leg never executed at the venue and is never re-sent"


def test_an_entry_leg_the_venue_has_an_order_for_is_never_closed_out_however_old(w):
    run, ctx, sent = _a_run_whose_spot_entry_got_no_answer(w)
    link = f"{run.gid}-s"
    leg = next(l for l in run.group_plan.legs if l.leg_id == "s")
    w.fake._create({"category": "spot", "symbol": "BTCUSDT", "side": "Buy", "orderType": "Limit", "qty": leg.qty, "price": leg.price, "timeInForce": "IOC", "orderLinkId": link})      # the request the engine gave up on LANDS late
    assert link in run.ex.venue_state()["orders"], "the fixture must leave an order at the venue under the leg's link"
    w.clock.t = sent + 10 * S.CLOSE_OUT_MIN_AGE_MS
    S.close_out_unanswered_entries(ctx, run)
    assert run.engine.groups[run.gid].legs["s"].status == "UNKNOWN" and "closed out" not in w.text(), "closing it out and unwinding would trade against an order that did execute"


def test_an_entry_leg_whose_execution_is_listed_is_never_closed_out_even_when_its_order_is_not(w):
    """Review: the absence proof was only 'not in the open + recently closed order list'. A fill under the link is existence too (the order list may have rolled over while the executions still show it)."""
    run, ctx, sent = _a_run_whose_spot_entry_got_no_answer(w)
    link = f"{run.gid}-s"
    leg = next(l for l in run.group_plan.legs if l.leg_id == "s")
    w.fake._create({"category": "spot", "symbol": "BTCUSDT", "side": "Buy", "orderType": "Limit", "qty": leg.qty, "price": leg.price, "timeInForce": "IOC", "orderLinkId": link})
    real = run.ex.venue_state

    def venue_state(*a, **k):
        v = dict(real(*a, **k))
        assert any(e["link_id"] == link for e in v["executions"]), "the fixture must leave an execution under the leg's link"
        return {**v, "orders": {k2: o for k2, o in v["orders"].items() if k2 != link}}                      # the order has rolled out of the list; the fill has not
    run.ex.venue_state = venue_state
    run.ex.lookup = lambda category, symbol, link_id: None                                                  # each witness must be sufficient ON ITS OWN: the by-link lookup is made blind here
    w.clock.t = sent + 10 * S.CLOSE_OUT_MIN_AGE_MS
    S.close_out_unanswered_entries(ctx, run)
    assert run.engine.groups[run.gid].legs["s"].status == "UNKNOWN" and "closed out" not in w.text()


@pytest.mark.parametrize("answer", ["an order", "unreadable"])
def test_an_entry_leg_the_venues_own_by_link_lookup_knows_or_cannot_answer_for_is_never_closed_out(w, answer):
    run, ctx, sent = _a_run_whose_spot_entry_got_no_answer(w)

    def lookup(category, symbol, link):
        if answer == "unreadable":
            raise TE.ExecutorError("the lookup could not be read")
        return {"orderLinkId": link, "orderStatus": "Filled", "orderId": "O777"}                              # a CLOSED order neither the open list nor the executions page still shows
    run.ex.lookup = lookup
    w.clock.t = sent + 10 * S.CLOSE_OUT_MIN_AGE_MS
    S.close_out_unanswered_entries(ctx, run)
    assert run.engine.groups[run.gid].legs["s"].status == "UNKNOWN" and "closed out" not in w.text()


def test_each_unresolved_entry_leg_is_judged_on_a_view_read_for_it_not_on_one_read_before_the_loop():
    """Review: ``view`` was read once before the loop, so a second leg was judged on a view that could be tens of seconds stale after the first close-out sent exits."""
    import types
    UNK = "UNKNOWN"
    legs = {k: types.SimpleNamespace(sent_ms=0, status=UNK, plan=types.SimpleNamespace(category="spot", symbol="BTCUSDT")) for k in ("a", "b")}
    closed, reads = [], []
    engine = types.SimpleNamespace(groups={"g": types.SimpleNamespace(legs=legs)}, on_order_done=lambda gid, leg, ms: closed.append(leg))

    def venue_state():
        reads.append(1)
        orders = {"g-b": {}} if len(reads) >= 2 else {}                                                     # leg b's order only becomes visible on the SECOND read
        return {"complete": True, "positions": {}, "orders": orders, "executions": []}
    run = types.SimpleNamespace(gid="g", engine=engine, ex=types.SimpleNamespace(venue_state=venue_state, lookup=lambda c, s, l: None), unresolved_entries=lambda: ["g-a", "g-b"])
    ctx = types.SimpleNamespace(clock=lambda: 10 ** 12, out=lambda m: None)
    S.close_out_unanswered_entries(ctx, run)
    assert len(reads) == 2 and closed == ["a"], (len(reads), closed)


def test_an_incomplete_read_for_any_leg_stops_the_whole_close_out():
    import types
    legs = {k: types.SimpleNamespace(sent_ms=0, status="UNKNOWN", plan=types.SimpleNamespace(category="spot", symbol="BTCUSDT")) for k in ("a", "b")}
    closed, reads = [], []
    engine = types.SimpleNamespace(groups={"g": types.SimpleNamespace(legs=legs)}, on_order_done=lambda gid, leg, ms: closed.append(leg))

    def venue_state():
        reads.append(1)
        return {"complete": len(reads) >= 2, "positions": {}, "orders": {}, "executions": []}                 # the first read is incomplete, the second is fine
    run = types.SimpleNamespace(gid="g", engine=engine, ex=types.SimpleNamespace(venue_state=venue_state, lookup=lambda c, s, l: None), unresolved_entries=lambda: ["g-a", "g-b"])
    S.close_out_unanswered_entries(types.SimpleNamespace(clock=lambda: 10 ** 12, out=lambda m: None), run)
    assert closed == [] and len(reads) == 1, "one unreliable read means nothing is closed out this time (the owner's unwind or the next call decides)"


def test_a_leg_that_resolved_while_the_loop_ran_is_not_closed_out():
    import types
    legs = {"a": types.SimpleNamespace(sent_ms=0, status="UNKNOWN", plan=types.SimpleNamespace(category="spot", symbol="BTCUSDT")),
            "b": types.SimpleNamespace(sent_ms=0, status="UNKNOWN", plan=types.SimpleNamespace(category="spot", symbol="BTCUSDT"))}
    closed = []

    def on_order_done(gid, leg, ms):
        closed.append(leg)
        legs["b"].status = "FILLED"                                                                            # closing a out lets the engine adopt b's fill
    engine = types.SimpleNamespace(groups={"g": types.SimpleNamespace(legs=legs)}, on_order_done=on_order_done)
    run = types.SimpleNamespace(gid="g", engine=engine, ex=types.SimpleNamespace(venue_state=lambda: {"complete": True, "positions": {}, "orders": {}, "executions": []}, lookup=lambda c, s, l: None),
                                unresolved_entries=lambda: ["g-a", "g-b"])
    S.close_out_unanswered_entries(types.SimpleNamespace(clock=lambda: 10 ** 12, out=lambda m: None), run)
    assert closed == ["a"]


@pytest.mark.parametrize("read", ["incomplete", "no_positions", "raises"])
def test_an_entry_leg_is_never_closed_out_on_a_venue_read_that_is_not_complete(w, read):
    run, ctx, sent = _a_run_whose_spot_entry_got_no_answer(w)
    real = run.ex.venue_state

    def venue_state(*a, **k):
        if read == "raises":
            raise TE.ExecutorError("the read failed")
        v = dict(real(*a, **k))
        return {**v, "complete": False} if read == "incomplete" else {**v, "positions": None}
    run.ex.venue_state = venue_state
    w.clock.t = sent + 10 * S.CLOSE_OUT_MIN_AGE_MS
    S.close_out_unanswered_entries(ctx, run)
    assert run.engine.groups[run.gid].legs["s"].status == "UNKNOWN" and "closed out" not in w.text(), "absence from an INCOMPLETE read proves nothing"


def test_every_held_spot_exit_is_printed_with_a_running_count_so_a_hold_that_never_ends_is_not_silent(w):
    """Review: a HELD answer no longer counts toward the exit halt (R3), so a hold that never resolved left only ACK rows in the journal. Each one is printed now, with the count and what it waits for;
    the session's exit window still bounds the total (the end state of a hold that never resolves is NOT_FLAT and the owner's unwind, as before)."""
    run, ctx, rec = begin(w)
    assert drive_full(w, run, 2) == "HEDGED"
    leg = T.LegPlan("xspot", "BTCUSDT", "spot", "Sell", "0.001", "Market", None, False, False, "IOC")
    for n in (1, 2, 3):
        ack = run._await_perp_closed(OrderRequest(run.gid, "c", "d", leg, f"{run.gid}-xspot{n}"))
        assert ack.reason == EX_HELD_REASON
        held = [l for l in w.out if l.startswith("SPOT EXIT HELD")]
        assert len(held) == n and f"#{n}" in held[-1] and f"{run.gid}-xspot{n}" in held[-1] and "perp" in held[-1], held
    assert run.spot_exits_held == 3


def test_the_delayed_exit_sequence_end_to_end_through_the_session_tool(w):
    w.fake.fill_delay_ms = 2500
    d = w.ready()
    assert w.execute(d) == 0, w.text()
    rec = w.record()
    assert rec["flat_verified"] is True and all(p["size"] == 0 for p in w.fake.positions.values())
    assert rec["net"]["funding_usd"] is None and rec["net"]["fees_usd"] == pytest.approx(sum(float(e["execFee"]) for e in w.fake.execs))
    assert S.record_refusals(rec) == [] and set(w.fake.create_calls.values()) == {1}


# ================================================================================================ red-team regressions (cp-E2)
import ms_reconcile as MR  # noqa: E402
from decimal import Decimal  # noqa: E402


def _steps(ctx, run, rec, n, dt=1):
    for _ in range(n):
        S._step_once(ctx, run, rec)
        run.ctx.sleep(dt)


# ---- 1 (HIGH) a group is resolved only after a COMPLETE venue read shows it flat; a late fill re-opens it so the unwind is authorised
def test_late_fill_after_a_failed_group_is_unwound_not_refused_as_resolved(w):
    run, ctx, rec = begin(w, submit=True, group_plan=resting_plan(w))
    w.clock.sleep(3_700)                                                                  # past max_wait: the engine cancels the resting entries and the group FAILS
    _steps(ctx, run, rec, 4)
    assert run.state() in ("CLOSED", "FAILED") and run.authority.is_resolved("gr1")       # flat on the venue: resolved
    w.fake.fill("gr1-p", "0.001")                                                         # the cancel RACED a fill: exposure after resolve_group
    assert w.fake.positions["BTCUSDT"]["size"] != 0
    _steps(ctx, run, rec, 40)
    codes = [r for _l, rs in run.authority.refusals for r in rs]
    assert "EXIT_GROUP_RESOLVED" not in codes, codes
    assert w.fake.positions["BTCUSDT"]["size"] == 0                                       # the reduce-only unwind was authorised and the VENUE ends flat
    assert run.state() in ("CLOSED", "FAILED")


def test_a_group_is_not_resolved_while_the_venue_still_holds_a_position(w):
    run, ctx, rec = begin(w, submit=True, group_plan=resting_plan(w))
    g = run.engine.groups["gr1"]
    run.engine._set_state(g, T.GroupState.FAILED, "FORCED_FOR_TEST", w.clock.now())          # terminal in the engine's arithmetic ...
    w.fake.inject_position("BTCUSDT", "-0.001")                                              # ... while the VENUE still holds the perp
    run.last_view = run.ex.venue_state()
    run.bookkeep()
    assert not run.authority.is_resolved("gr1")
    w.fake.positions["BTCUSDT"]["size"] = Decimal(0)
    for o in w.fake.orders.values():
        o["status"] = "Cancelled"                                                            # the resting entries are gone too
    run.last_view = run.ex.venue_state()
    run.bookkeep()
    assert run.authority.is_resolved("gr1")                                                  # a complete flat read releases it


def test_resolving_needs_a_complete_read(w):
    run, ctx, rec = begin(w, submit=True, group_plan=resting_plan(w))
    w.clock.sleep(3_700)
    _steps(ctx, run, rec, 1)
    w.fake.inject("/v5/position/list", "timeout", times=999)
    g = run.engine.groups["gr1"]
    if g.state not in (T.GroupState.CLOSED, T.GroupState.FAILED):
        run.engine._set_state(g, T.GroupState.FAILED, "FORCED_FOR_TEST", w.clock.now())
    run.authority._groups.append({"event": "reopen", "group_id": "gr1", "at_ms": 1}) if run.authority.is_resolved("gr1") else None
    run.last_view = run.ex.venue_state()                                                     # incomplete read
    run.bookkeep()
    assert not run.authority.is_resolved("gr1")


def test_reconcile_unwind_authorises_the_exit_of_a_group_resolved_in_an_earlier_process(w):
    d = w.ready()
    run, ctx, rec = begin(w, submit=True)
    assert drive(w, run, 2) == "HEDGED"
    run.authority.resolve_group(run.gid)                                                  # what the old code did on a CLOSED/FAILED engine state
    assert w.run("reconcile", "--unwind", "--confirm-plan-digest", d) == 0, w.text()
    assert w.record()["flat_verified"] is True and all(p["size"] == 0 for p in w.fake.positions.values())


# ---- 2 (HIGH) reconcile never sends an entry; an unwind needs the digest
def test_reconcile_without_flags_never_sends_an_entry_after_a_crash_before_the_send(w):
    d = w.ready()
    orig = w.fake.request
    state = {"n": 0}

    def req(method, url, **kw):
        if url.endswith(CREATE) and state["n"] == 0:
            state["n"] = 1
            raise SimulatedCrash("before send")
        return orig(method, url, **kw)
    w.fake.request = req
    with pytest.raises(SimulatedCrash):
        w.run("execute", "--confirm-plan-digest", d, typed=d)
    w.fake.request = orig
    assert w.fake.create_calls == {}
    w.clock.sleep(120)                                                                    # resend_after_ms has long elapsed
    w.run("reconcile")
    assert w.fake.create_calls == {} and w.requests_to(CREATE) == []                      # reconcile sent NOTHING (no entry, no exit)
    assert "UNRESOLVED ENTRY" in w.text()
    assert w.run("reconcile", "--unwind", "--confirm-plan-digest", d) == 0, w.text()      # with the digest the never-sent leg is closed out; the venue is flat
    assert w.fake.create_calls == {} and w.record()["flat_verified"] is True


def test_reconcile_without_the_digest_sends_no_reduce_only_exit_either(w):
    d = w.ready()
    w.fake.inject(CREATE, "crash_after", match=lambda b: b["orderLinkId"].endswith("-p"))
    with pytest.raises(SimulatedCrash):
        w.run("execute", "--confirm-plan-digest", d, typed=d)
    n = len(w.requests_to(CREATE))
    assert w.run("reconcile") == 1
    assert len(w.requests_to(CREATE)) == n and w.record()["flat_verified"] is False
    assert w.run("reconcile", "--unwind", "--confirm-plan-digest", d) == 0
    assert all(p["size"] == 0 for p in w.fake.positions.values()) and set(w.fake.create_calls.values()) == {1} | {1}


# ---- 3 (MED) an empty / malformed wallet answer is incomplete, never flat
def _wallet_answer(w, mutate):
    orig = w.fake.request

    def req(method, url, **kw):
        st, txt = orig(method, url, **kw)
        if "wallet-balance" in url:
            d = json.loads(txt)
            mutate(d)
            txt = json.dumps(d)
        return st, txt
    w.fake.request = req


@pytest.mark.parametrize("mutate", [lambda d: d["result"].update(list=[]), lambda d: d["result"].update(list="x"), lambda d: d["result"].update(list=[{}]),
                                    lambda d: d["result"].update(list=[{"coin": []}]), lambda d: d["result"].update(list=[{"coin": [{"coin": "ETH", "walletBalance": "1"}]}]),
                                    lambda d: d["result"].pop("list"), lambda d: d["result"].update(list=[None])])
def test_an_empty_or_malformed_wallet_answer_makes_the_view_incomplete_never_flat(w, mutate):
    run, ctx, rec = begin(w, submit=False)
    w.fake.wallet["BTC"] = Decimal("0.5")
    assert run.ex.venue_state()["positions"]["spot:BTCUSDT"] == 0.5                       # positive control
    _wallet_answer(w, mutate)
    v = run.ex.venue_state()
    assert v["complete"] is False and v["positions"] is None


@pytest.mark.parametrize("mutate", [lambda d: d["result"].update(list=[]), lambda d: d["result"]["list"][0].pop("totalEquity"), lambda d: d["result"]["list"][0].pop("coin"),
                                    lambda d: d["result"]["list"][0].update(totalEquity="abc"), lambda d: d["result"].update(list=[None])])
def test_account_state_raises_executor_error_on_a_malformed_wallet_answer(w, mutate):
    run, ctx, rec = begin(w, submit=False)
    run.ex.account_state({}, 0)                                                           # positive control
    _wallet_answer(w, mutate)
    with pytest.raises(TE.ExecutorError):
        run.ex.account_state({}, 0)


# ---- 4 (MED) release_leaked decides flat from the VENUE read
def test_release_leaked_uses_the_venue_read_not_the_engine(w):
    run, ctx, rec = begin(w, submit=True)

    class Eng:
        spot_base = {}

        def states(self):
            return {run.gid: "CLOSED"}

        def positions(self):
            return {}                                                                     # engine arithmetic says flat

    w.fake.inject_position("BTCUSDT", "-0.001")
    view = run.ex.venue_state()
    assert view["positions"]["linear:BTCUSDT"] != 0
    out = MR.release_leaked(run.authority, Eng(), [run.gid], view)
    assert out[run.gid].startswith("REFUSED") and not run.authority.is_resolved(run.gid)
    assert MR.release_leaked(run.authority, Eng(), [run.gid])[run.gid] == "REFUSED:NO_COMPLETE_VENUE_READ"             # no venue read: no release
    assert MR.release_leaked(run.authority, Eng(), [run.gid], dict(view, complete=False))[run.gid] == "REFUSED:NO_COMPLETE_VENUE_READ"
    w.fake.positions["BTCUSDT"]["size"] = Decimal(0)
    w.fake.wallet["BTC"] = Decimal(0)
    flat = run.ex.venue_state()
    assert MR.release_leaked(run.authority, Eng(), [run.gid], flat)[run.gid] == "RELEASED"                             # positive control


# ---- surviving mutants
def test_flat_verified_is_false_when_spot_residue_exceeds_dust_and_true_within_it(w):
    run, ctx, rec = begin(w)
    assert drive(w, run, 2) == "HEDGED"
    run.engine.request_exit(run.gid, w.clock.now(), "T")
    assert drive(w, run, 4) == "CLOSED"
    w.fake.wallet["BTC"] += Decimal("0.0000010")                                          # within dust (<= max(step, 0.2% of qty))
    run.snapshot_record(rec, final=True)
    assert rec["flat_verified"] is True
    w.fake.wallet["BTC"] += Decimal("0.01")                                               # 10x the position: far beyond any dust
    run.snapshot_record(rec, final=True)
    assert rec["flat_verified"] is False


def test_flat_verified_is_false_with_a_foreign_open_order_on_the_symbol(w):
    run, ctx, rec = begin(w)
    assert drive(w, run, 2) == "HEDGED"
    run.engine.request_exit(run.gid, w.clock.now(), "T")
    assert drive(w, run, 4) == "CLOSED"
    run.snapshot_record(rec, final=True)
    assert rec["flat_verified"] is True
    w.fake._create({"category": "linear", "symbol": "BTCUSDT", "side": "Sell", "orderType": "Limit", "qty": "0.001", "price": "70000.0", "orderLinkId": ""})     # an order that is not ours
    run.snapshot_record(rec, final=True)
    assert rec["flat_verified"] is False


# ---- unconfirmed suspicions
def test_non_trade_execution_types_are_surfaced_not_dropped(w):
    run, ctx, rec = begin(w)
    drive(w, run, 1)
    w.fake.execs.append({"execId": "F000001", "orderId": "", "orderLinkId": "", "symbol": "BTCUSDT", "category": "linear", "side": "Sell", "execQty": "0", "execPrice": "60000",
                         "execFee": "-0.05", "feeCurrency": "USDT", "isMaker": False, "execTime": str(w.clock.now()), "execType": "Funding"})
    v = run.ex.venue_state()
    assert any(u["type"] == "Funding" and u["exec_id"] == "F000001" for u in v["unattributed"])
    assert all(x["fill_id"] != "F000001" for x in v["executions"])                        # still not booked as a fill


def test_a_reconcile_disagreement_halt_clears_only_when_a_later_complete_venue_read_agrees(w):
    from ms_types import Fill
    run, ctx, rec = begin(w)
    assert drive_full(w, run, 2) == "HEDGED"
    run.ledger.record_fill(Fill("bogus-1", run.gid, "p", "BTCUSDT", "Sell", 0.5, 60000.0, 0.0, "USDT", "TAKER", w.clock.now(), "MEASURED"), category="linear")
    settle_view(w, run)
    assert any(h.startswith("RECONCILE_DISAGREEMENT") for h in run.engine.halts) and run.ex.halted == "RECONCILE_DISAGREEMENT"
    w.fake.inject("/v5/position/list", "timeout", times=99)                               # the next venue read is incomplete: it can NOT clear the halt
    settle_view(w, run)
    assert any(h.startswith("RECONCILE_DISAGREEMENT") for h in run.engine.halts) and run.ex.halted
    w.fake.faults.clear()
    run.ledger.record_fill(Fill("bogus-2", run.gid, "p", "BTCUSDT", "Buy", 0.5, 60000.0, 0.0, "USDT", "TAKER", w.clock.now() + 1, "MEASURED"), category="linear")   # the books agree again
    settle_view(w, run)
    assert run.recon_report.ok and not [h for h in run.engine.halts if h.startswith("RECONCILE_DISAGREEMENT")] and run.ex.halted is None


def test_recovery_after_a_crash_between_legs_names_the_unsent_leg_and_raises_no_orphan_fill_halt(w):
    d = w.ready()
    w.fake.inject(CREATE, "crash_after", match=lambda b: b["orderLinkId"].endswith("-p"))
    with pytest.raises(SimulatedCrash):
        w.run("execute", "--confirm-plan-digest", d, typed=d)
    n = len(w.requests_to(CREATE))
    assert w.run("reconcile") == 1
    assert "UNSENT PLAN LEG" in w.text() and "'s'" in w.text()                             # the engine calls a one-legged group HEDGED; the tool says it is not
    assert "ORPHAN_FILL" not in w.text()                                                   # the venue's own order record acknowledges the adopted perp order in the ledger
    assert len(w.requests_to(CREATE)) == n and w.record()["flat_verified"] is False


def test_an_exit_in_flight_at_the_crash_is_not_sent_by_reconcile_without_the_digest(w):
    d = w.ready()
    run, ctx, rec = begin(w, submit=True)
    assert drive(w, run, 2) == "HEDGED"
    run.engine._emit("EXIT_MODE", run.gid, w.clock.now(), {"mode": "EXIT", "reason": "T"})   # the exit decision was journalled; the process died before sending it
    n = len(creates(w))
    assert w.run("reconcile") == 1 and len(creates(w)) == n                                 # recovery did NOT continue the exit by itself
    assert w.run("reconcile", "--unwind", "--confirm-plan-digest", d) == 0 and len(creates(w)) > n
    assert all(p["size"] == 0 for p in w.fake.positions.values())


# ---------------------------------------------------------------- the loss limit measures the SESSION, not the owner's pre-existing holdings (found on the first real execute)
def _world_with_existing_btc(tmp_path, monkeypatch, qty="0.1"):
    world = World(tmp_path, monkeypatch)
    world.fake.wallet["BTC"] = D(qty)                    # the owner already holds BTC before the session: about $6,000 here, about $7,800 on the real account
    world.ready()
    return world


def test_the_account_equity_ignores_price_moves_of_the_pre_session_spot_holding(tmp_path, monkeypatch):
    world = _world_with_existing_btc(tmp_path, monkeypatch)
    run, ctx, rec = begin(world, submit=False)
    assert run.baseline["spot:BTCUSDT"] == pytest.approx(0.1)
    e0 = run.ex.account_state(run.baseline)["equity_usd"]
    raw0 = run.ex.account_state({})["equity_usd"]
    world.fake.book["BTCUSDT"]["mark"] = "59900.5"      # -$100 per BTC = -$10 on 0.1 BTC: nine times the 1.11 limit, with no trade at all
    world.fake.book["BTCUSDT"]["spot_mark"] = "59900.5"
    e1 = run.ex.account_state(run.baseline)["equity_usd"]
    raw1 = run.ex.account_state({})["equity_usd"]
    assert raw1 == pytest.approx(raw0 - 10.0)            # positive control: the whole-account figure DOES move with the unrelated holding
    assert e1 == pytest.approx(e0)                       # the session's measure does not
    world.fake.wallet["USDT"] -= D("2")                  # a real loss (fees / slippage) still shows in full
    assert run.ex.account_state(run.baseline)["equity_usd"] == pytest.approx(e0 - 2.0)


def test_a_price_swing_of_the_existing_holding_does_not_trip_the_loss_limit_but_a_real_loss_does(tmp_path, monkeypatch):
    world = _world_with_existing_btc(tmp_path, monkeypatch)
    run, ctx, rec = begin(world, submit=False)
    run._read_state()                                    # the session's first account read: its start equity
    assert run.max_loss_usd == pytest.approx(AD.MAX_LOSS_CEILING_USD)
    world.clock.sleep(60)
    world.fake.book["BTCUSDT"]["mark"] = "59500.5"       # -$500 per BTC on the 0.1 BTC held: -$50
    world.fake.book["BTCUSDT"]["spot_mark"] = "59500.5"
    assert run.enforce_limits(world.clock.now()) is None
    world.clock.sleep(60)
    world.fake.wallet["USDT"] -= D("6.0")                # a real session loss above the limit (5.55)
    assert run.enforce_limits(world.clock.now()) == "LOSS_LIMIT"


def test_without_a_usd_value_in_the_wallet_read_the_baseline_falls_back_to_the_spot_price(tmp_path, monkeypatch):
    world = _world_with_existing_btc(tmp_path, monkeypatch)
    monkeypatch.setattr(TE, "_wallet_usd_values", lambda res: {})          # a wallet row with no usable usdValue
    run, ctx, rec = begin(world, submit=False)
    e0 = run.ex.account_state(run.baseline)["equity_usd"]
    world.fake.book["BTCUSDT"]["mark"] = "59900.5"
    world.fake.book["BTCUSDT"]["spot_mark"] = "59900.5"
    assert run.ex.account_state(run.baseline)["equity_usd"] == pytest.approx(e0)


# ---------------------------------------------------------------- a Trade row with no fee stated is an UNKNOWN fee, never a measured zero
@pytest.mark.parametrize("blank", ["", None])
def test_a_blank_execfee_on_a_trade_row_is_surfaced_as_an_unknown_fee_not_a_measured_zero(w, blank):
    client, _t = TT.build_client(TT.TestnetCredentials.from_mapping(w.env), w.fake)
    ex = TE.TestnetExecutor(client, symbols=["BTCUSDT"], spot_symbols=["BTCUSDT"], clock_ms=w.clock.now, kill_reader=lambda: (False, "t"))
    row = {"execId": "X1", "orderId": "O1", "orderLinkId": "g1-p", "symbol": "BTCUSDT", "category": "linear", "side": "Sell", "execQty": "0.01", "execPrice": "60000", "execFee": blank,
           "feeCurrency": "USDT", "isMaker": False, "execTime": str(w.clock.now()), "execType": "Trade"}
    if blank is None:
        row.pop("execFee")
    w.fake.execs = [row]
    fills = [x for x in ex.venue_state()["executions"] if x["link_id"] == "g1-p"]
    assert len(fills) == 1 and fills[0]["fee_known"] is False and fills[0]["fee_signed_usd"] is None and fills[0]["prov"] == "UNKNOWN"
    stated = dict(row, execId="X2", orderLinkId="g1-q", execFee="0")
    w.fake.execs = [stated]
    z = [x for x in ex.venue_state()["executions"] if x["link_id"] == "g1-q"][0]
    assert z["fee_known"] is True and z["fee_signed_usd"] == 0.0 and z["prov"] == "MEASURED"      # a fee the venue STATED as zero is still measured
