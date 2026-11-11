"""Measured per-leg execution quality: the canonical calculation, its sign convention, idempotence, and the capture path through the real executor against the fake venue.

Every fixture states what the venue said; every assertion states what may and may not be inferred (a quote is not a fill, a resting order is not a maker execution, a timed-out cancel is not a
cancel, a missing field is UNKNOWN and never zero)."""
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
import exec_quality as EQ  # noqa: E402
from ms_testnet_world import World, begin, resting_plan, restart  # noqa: E402

CREATE_PATH = "/v5/order/create"
REF = {"bid": "100.00", "ask": "100.20", "ts_ms": 1_000, "source": "TICKER_TOUCH", "age_ms_at_send": 0}


def fill(i, qty, price, fee="0.01", liq="TAKER", ts=2_000, **kw):
    return {"fill_id": f"E{i}", "qty": qty, "price": price, "fee_usd": fee, "fee_ccy": "USDT", "liquidity": liq, "ts_ms": ts, "side": kw.get("side"), "symbol": "BTCUSDT"}


def leg(side="Buy", fills=(), ref=REF, timing=None, order=None, kind="ENTRY", leg_id="p", qty="2", reduce=False, order_type="Market", tif="IOC", category="linear"):
    plan = {"side": side, "category": category, "symbol": "BTCUSDT", "order_type": order_type, "time_in_force": tif, "qty": qty, "price": None, "reduce_only": reduce}
    return EQ.leg_quality(link_id=f"g-{leg_id}", group_id="g", leg_id=leg_id, kind=kind, candidate_id="c1", decision_id="dec_1", plan=plan, ref=ref,
                          timing=timing or {"sent_ms": 1_500, "acked_ms": 1_560}, order=order, fills=fills)


# ------------------------------------------------------------------ the calculation and its sign convention (pure)
def test_a_full_buy_fill_costs_the_half_spread_plus_the_walk_plus_the_fee_each_once():
    r = leg("Buy", [fill(1, "1", "100.20", "0.05"), fill(2, "1", "100.30", "0.05")], order={"status": "Filled", "created_ms": 1_510, "updated_ms": 2_001, "cum_qty": "2", "avg_price": "100.25"})
    assert r["fills"]["vwap"] == "100.25000000" and r["fills"]["fill_ratio"] == "1.000000" and r["liquidity"] == "TAKER"
    # mid = 100.10; Buy cost vs mid = (100.25 - 100.10) * 2 = 0.30 ; touch = ask 100.20 -> walk = (100.25 - 100.20) * 2 = 0.10 ; half spread = 0.10 * 2 = 0.20 (0.30 = 0.20 + 0.10)
    c = r["cost"]
    assert (D(c["price_vs_mid_usd"]), D(c["walk_vs_touch_usd"]), D(c["half_spread_usd"])) == (D("0.30"), D("0.10"), D("0.20"))
    assert D(c["price_vs_mid_usd"]) == D(c["walk_vs_touch_usd"]) + D(c["half_spread_usd"])                 # nothing counted twice or dropped
    assert D(c["fee_usd"]) == D("0.10") and D(c["total_usd"]) == D("0.40")                                    # the fee is the ONLY additive charge
    assert D(c["slippage_vs_mid_bps"]) == (D("0.15") / D("100.10") * 10000).quantize(D("0.0001"))
    assert D(c["total_bps"]) == (D("0.40") / D("200.50") * 10000).quantize(D("0.0001"))
    assert r["timing"]["ack_latency_ms"] == 60 and r["timing"]["order_to_first_fill_ms"] == 490 and r["unknown"] == []


def test_a_sell_is_signed_the_other_way_and_price_improvement_is_a_negative_cost():
    r = leg("Sell", [fill(1, "1", "100.10")], qty="1")                       # sells at 100.10 vs mid 100.10: zero cost vs mid, but BELOW the bid? touch = bid 100.00 -> improvement 0.10
    assert D(r["cost"]["price_vs_mid_usd"]) == D("0") and D(r["cost"]["walk_vs_touch_usd"]) == D("-0.10")
    worse = leg("Sell", [fill(1, "1", "99.90")], qty="1")
    assert D(worse["cost"]["price_vs_mid_usd"]) == D("0.20") and D(worse["cost"]["walk_vs_touch_usd"]) == D("0.10")


def test_a_rebate_is_a_negative_cost_and_is_not_clamped_away():
    r = leg("Buy", [{"fill_id": "E1", "qty": "1", "price": "100.00", "fee_signed_usd": "-0.02", "fee_ccy": "USDT", "liquidity": "MAKER", "ts_ms": 2_000}], qty="1")
    assert D(r["fee"]["usd_signed"]) == D("-0.02") and r["liquidity"] == "MAKER"
    assert D(r["cost"]["total_usd"]) == D(r["cost"]["price_vs_mid_usd"]) + D("-0.02")


def test_a_resting_order_without_an_execution_is_not_a_maker_fill_and_a_quote_is_not_a_fill():
    r = leg("Buy", [], order={"status": "New", "created_ms": 1_510}, order_type="Limit", tif="PostOnly")
    assert r["liquidity"] == "NONE" and r["fills"]["count"] == 0 and r["cost"]["total_usd"] is None and r["cost"]["price_vs_mid_usd"] is None
    assert r["fills"]["vwap"] is None and D(r["fills"]["qty"]) == 0


def test_liquidity_comes_only_from_the_venue_flag_and_unknown_stays_unknown():
    assert leg("Buy", [fill(1, "1", "100", liq="MAKER")], qty="1")["liquidity"] == "MAKER"
    assert leg("Buy", [fill(1, "1", "100", liq="MAKER"), fill(2, "1", "100", liq="TAKER")])["liquidity"] == "MIXED"
    r = leg("Buy", [{"fill_id": "E1", "qty": "1", "price": "100", "fee_usd": "0.01", "ts_ms": 2_000}], qty="1")        # the venue gave no isMaker
    assert r["liquidity"] == EQ.UNKNOWN and "liquidity" in r["unknown"]


def test_a_missing_reference_makes_the_cost_unknown_never_zero():
    r = leg("Buy", [fill(1, "1", "100.50")], ref=None, qty="1")
    assert r["reference"] is None and r["cost"]["price_vs_mid_usd"] is None and r["cost"]["total_usd"] is None and "reference_quote" in r["unknown"]
    assert D(r["cost"]["fee_usd"]) == D("0.01")                                                                    # the fee is a fact even without a reference
    crossed = leg("Buy", [fill(1, "1", "100.50")], ref={"bid": "101", "ask": "100", "ts_ms": 1}, qty="1")           # a crossed quote is not a reference
    assert crossed["reference"] is None and "reference_quote" in crossed["unknown"]


def test_a_missing_fee_is_unknown_not_zero():
    r = leg("Buy", [{"fill_id": "E1", "qty": "1", "price": "100.50", "liquidity": "TAKER", "ts_ms": 2_000}], qty="1")
    assert r["fee"]["usd_signed"] is None and r["cost"]["total_usd"] is None and "fee" in r["unknown"]


def test_a_duplicate_venue_execution_event_is_one_execution():
    once = leg("Buy", [fill(1, "1", "100.20")], qty="2")
    twice = leg("Buy", [fill(1, "1", "100.20"), fill(1, "1", "100.20")], qty="2")
    assert twice["fills"]["qty"] == once["fills"]["qty"] and twice["fills"]["count"] == 1 and twice["fills"]["duplicate_events_collapsed"] == 1
    assert twice["cost"] == once["cost"]


def test_unusable_rows_are_dropped_not_guessed():
    r = leg("Buy", [{"fill_id": "", "qty": "1", "price": "100"}, {"fill_id": "E2", "qty": "0", "price": "100"}, {"fill_id": "E3", "qty": "1", "price": "-1"}, {"fill_id": "E4"}])
    assert r["fills"]["count"] == 0 and r["liquidity"] == "NONE"


def test_overfill_is_a_data_fault_flagged_never_a_ratio():
    r = leg("Buy", [fill(1, "3", "100.20")], qty="2")
    assert r["fills"]["overfilled"] is True and r["fills"]["fill_ratio"] is None and "fill_ratio" in r["unknown"]


def test_partial_fill_keeps_the_ratio_and_the_residual():
    r = leg("Buy", [fill(1, "0.5", "100.20")], qty="2", order={"status": "PartiallyFilledCanceled", "cum_qty": "0.5"})
    assert r["fills"]["fill_ratio"] == "0.250000" and r["order"]["status"] == "PartiallyFilledCanceled"


def test_a_rejected_order_has_an_ack_time_and_no_cost():
    r = leg("Buy", [], timing={"sent_ms": 1_500, "acked_ms": 1_540}, order={"status": "Rejected"})
    assert r["timing"]["ack_latency_ms"] == 40 and r["cost"]["total_usd"] is None and r["liquidity"] == "NONE" and r["order"]["status"] == "Rejected"


# ------------------------------------------------------------------ cancel semantics
def test_a_cancel_is_recorded_only_as_the_venue_answered_it():
    ok = leg("Buy", [], timing={"sent_ms": 1_500, "acked_ms": 1_520, "cancel_sent_ms": 9_000, "cancel_acked_ms": 9_050, "cancel_result": "CANCELLED"}, order={"status": "Cancelled"})
    assert ok["cancel"]["result"] == "CANCELLED" and ok["cancel"]["latency_ms"] == 50 and ok["liquidity"] == "NONE"
    timed_out = leg("Buy", [], timing={"sent_ms": 1_500, "acked_ms": 1_520, "cancel_sent_ms": 9_000})                   # no answer: NOT cancelled
    assert timed_out["cancel"]["result"] == EQ.UNKNOWN and timed_out["cancel"]["latency_ms"] is None and "cancel_result" in timed_out["unknown"]


def test_a_fill_after_the_cancel_request_is_counted_beyond_the_clock_tolerance():
    r = leg("Buy", [fill(1, "1", "100.20", ts=5_000), fill(2, "1", "100.20", ts=9_500), fill(3, "1", "100.20", ts=20_000)], qty="5",
            timing={"sent_ms": 1_500, "acked_ms": 1_520, "cancel_sent_ms": 9_000, "cancel_result": "TOO_LATE", "cancel_acked_ms": 9_040})
    c = r["cancel"]
    assert c["result"] == "TOO_LATE" and c["fills_after_request"] == 1 and D(c["qty_after_request"]) == D("1") and c["fills_within_clock_tolerance"] == 1 and c["clock_tolerance_ms"] == 2000


# ------------------------------------------------------------------ the group: unhedged window, residual, unwind, completion
def _g(legs, flat, style="TAKER"):
    return EQ.group_quality(legs, style=style, notional_usd="200", flat_verified=flat, decision_id="dec_1")


def test_a_clean_hedged_round_trip_sums_every_leg_and_is_flat():
    p = leg("Sell", [fill(1, "1", "100.00", ts=2_000)], qty="1", leg_id="p", ref={"bid": "100", "ask": "100.1", "ts_ms": 1})
    s = leg("Buy", [fill(2, "1", "100.10", ts=2_300)], qty="1", leg_id="s", category="spot", ref={"bid": "100", "ask": "100.1", "ts_ms": 1})
    xp = leg("Buy", [fill(3, "1", "100.10", ts=9_000)], qty="1", leg_id="x1", kind="EXIT", reduce=True, ref={"bid": "100", "ask": "100.1", "ts_ms": 1})
    xs = leg("Sell", [fill(4, "1", "100.00", ts=9_100)], qty="1", leg_id="x2", kind="EXIT", category="spot", ref={"bid": "100", "ask": "100.1", "ts_ms": 1})
    g = _g([p, s, xp, xs], True)
    assert g["unhedged_window_ms"] == 300 and g["all_entry_legs_filled"] and g["attempt_completed"] and g["cost_complete"]
    assert g["residual_qty_by_instrument"] == {"linear:BTCUSDT": "0.00000000", "spot:BTCUSDT": "0.00000000"}
    # each leg costs 0.05 (half spread on 1 unit) + 0.01 fee
    assert D(g["total_execution_cost_usd"]) == D("0.24") and g["unwind_first_fill_ms"] == 9_000 and g["unwind_last_fill_ms"] == 9_100
    assert D(g["position_notional_usd"]) == D("100.05") and D(g["total_execution_cost_bps_of_position_notional"]) == (D("0.24") / D("100.05") * 10000).quantize(D("0.0001"))


def test_a_second_leg_failure_is_an_unwound_attempt_with_its_real_cost_and_no_hedge():
    p = leg("Sell", [fill(1, "1", "100.00", ts=2_000)], qty="1", leg_id="p", ref={"bid": "100", "ask": "100.1", "ts_ms": 1})
    s = leg("Buy", [], qty="1", leg_id="s", category="spot", order={"status": "Rejected"}, ref={"bid": "100", "ask": "100.1", "ts_ms": 1})
    xp = leg("Buy", [fill(3, "1", "100.10", ts=3_000)], qty="1", leg_id="x1", kind="EXIT", reduce=True, ref={"bid": "100", "ask": "100.1", "ts_ms": 1})
    g = _g([p, s, xp], True)
    assert g["all_entry_legs_filled"] is False and g["entry_legs_filled"] == 1 and g["entry_legs_planned"] == 2 and g["attempt_completed"] is True
    assert g["residual_qty_by_instrument"]["linear:BTCUSDT"] == "0.00000000" and D(g["total_execution_cost_usd"]) == D("0.12")          # the failed hedge still cost 0.12


def test_an_unflat_group_is_not_a_completed_attempt_and_shows_its_residual():
    p = leg("Sell", [fill(1, "1", "100.00")], qty="1", leg_id="p", ref={"bid": "100", "ask": "100.1", "ts_ms": 1})
    g = _g([p], False)
    assert g["attempt_completed"] is False and g["residual_qty_by_instrument"]["linear:BTCUSDT"] == "-1.00000000"


# ------------------------------------------------------------------ the comparison never invents a winner
def _attempt(style, bps, done=True, n=1):
    return {"style": style, "attempt_completed": done, "cost_complete": True, "total_execution_cost_bps_of_position_notional": str(bps), "total_execution_cost_usd": "0.2",
            "position_notional_usd": "100"}


def test_the_policy_comparison_is_insufficient_below_the_minimum_sample_and_counts_unfilled_attempts():
    rows = [_attempt("TAKER", 22), _attempt("TAKER", 24), _attempt("MAKER_FIRST", 9), _attempt("MAKER_FIRST", 0, done=False), _attempt("MAKER_FIRST", 0, done=False)]
    s = EQ.policy_summary(rows)
    assert s["verdict"] == "INSUFFICIENT_SAMPLE"
    assert s["policies"]["TAKER"]["mean_cost_bps"] == "23.0000" and s["policies"]["MAKER_FIRST"]["completion_rate"] == "0.3333"     # cheap when filled, rarely filled: visible
    full = EQ.policy_summary([_attempt("TAKER", 22 + (i % 3)) for i in range(5)] + [_attempt("MAKER_FIRST", 10 + (i % 3)) for i in range(5)], min_orders=5)
    assert full["verdict"] == "COMPARABLE_SAMPLE" and D(full["policies"]["MAKER_FIRST"]["mean_cost_bps"]) < D(full["policies"]["TAKER"]["mean_cost_bps"])


# ------------------------------------------------------------------ the capture path: the real executor and session against the fake venue
@pytest.fixture
def w(tmp_path, monkeypatch):
    return World(tmp_path, monkeypatch)


def _quality(w):
    with open(os.path.join(w.state_dir, "execution_quality.json"), encoding="utf-8") as fh:
        return json.load(fh)


def _ready(w, notional="90"):
    assert w.run("preflight") == 0, w.text()
    assert w.run("plan", "--symbol", "BTCUSDT", "--notional-usd", notional) == 0, w.text()


def test_a_full_session_records_reference_timestamps_fills_and_the_exact_costs(w):
    _ready(w)
    assert w.execute() == 0, w.text()
    q = _quality(w)
    legs = {l["kind"] + ":" + l["instrument"]["category"]: l for l in q["legs"]}
    assert set(legs) == {"ENTRY:linear", "ENTRY:spot", "EXIT:linear", "EXIT:spot"}
    perp = legs["ENTRY:linear"]
    assert perp["reference"]["source"] == "TICKER_TOUCH" and perp["reference"]["age_ms_at_send"] == 0 and perp["liquidity"] == "TAKER"
    assert perp["timing"]["sent_ms"] is not None and perp["timing"]["acked_ms"] is not None and perp["order"]["status"] == "Filled"
    # the fake book is 60000.0 / 60001.0: a Sell at the bid costs half a dollar per BTC against the mid, plus the 5.5 bps taker fee
    qty = D(perp["fills"]["qty"])
    assert D(perp["cost"]["price_vs_mid_usd"]) == D("0.5") * qty and D(perp["cost"]["fee_usd"]) == (qty * D("60000.0") * D("0.00055")).quantize(D("0.00000001"))
    assert D(perp["fee"]["bps_of_notional"]) == D("5.5000") and perp["fee"]["rate_provenance"] == EQ.UNKNOWN                  # the fake gives no feeRate: stated, not invented
    assert legs["EXIT:linear"]["reference"]["source"] == "LATEST_HELD_QUOTE"                                                 # an exit is never slowed by a read: the latest held quote, labelled
    g = q["group"]
    assert g["attempt_completed"] and g["cost_complete"] and g["all_entry_legs_filled"] and g["style"] == "TAKER" and g["unknown"] == []
    assert g["residual_qty_by_instrument"] == {"linear:BTCUSDT": "0.00000000", "spot:BTCUSDT": "0.00000000"}
    # measured fees reconcile with the venue's own execution list to the last digit
    assert sum(D(l["cost"]["fee_usd"]) for l in q["legs"]) == sum(D(e["execFee"]) for e in w.fake.execs).quantize(D("0.00000001"))
    # the raw measurements were persisted beside it (a restart keeps them)
    assert os.path.exists(os.path.join(w.state_dir, "exec_measurements.json"))


def test_a_rejected_second_leg_is_an_unwound_attempt_in_the_record(w):
    _ready(w)
    gid = w.plan_doc()["group"]["group_id"]
    w.fake.reject["-s"] = (170131, "Insufficient balance")                                   # the spot leg is refused by the venue
    w.execute()
    q = _quality(w)
    spot = next(l for l in q["legs"] if l["leg_id"] == "s")
    assert spot["fills"]["count"] == 0 and spot["cost"]["total_usd"] is None and spot["timing"]["acked_ms"] is not None            # a venue refusal is an answer with a time
    g = q["group"]
    assert g["all_entry_legs_filled"] is False and g["exit_legs"] >= 1 and g["entry_legs_filled"] == 1
    assert w.record()["flat_verified"] is True and g["attempt_completed"] is True
    assert all(p["size"] == 0 for p in w.fake.positions.values())


def test_a_partial_fill_is_measured_as_a_partial_fill(w):
    _ready(w)
    w.fake.liquidity["BTCUSDT"] = D("0.0004")                                                 # only part of the perp quantity can be filled
    w.execute()
    q = _quality(w)
    perp = next(l for l in q["legs"] if l["leg_id"] == "p")
    assert perp["fills"]["fill_ratio"] is not None and D(perp["fills"]["fill_ratio"]) < 1 and perp["order"]["status"] in ("PartiallyFilledCanceled", "Cancelled", "Filled")


def test_a_cancel_that_never_answered_and_a_later_fill_are_both_recorded_honestly(w):
    _ready(w)
    run, ctx, rec = begin(w, submit=False, group_plan=resting_plan(w))
    run.engine.submit_group(run.gid, w.clock.now(), market_ts_ms=w.clock.now(), account_ts_ms=w.clock.now())
    run.bookkeep()
    link = f"{run.gid}-p"
    assert run.ex.meas[link]["sent_ms"] is not None and run.ex.meas[link]["reference"]["source"] == "TICKER_TOUCH"             # a resting limit order still gets its entry reference
    w.fake.inject("/v5/order/cancel", "timeout", times=1)
    w.clock.sleep(5)
    req = type("R", (), {})()
    from ms_execution import OrderRequest
    leg = next(l.plan for l in run.engine.groups[run.gid].legs.values() if l.link == link)
    with pytest.raises(Exception):
        run.ex.cancel_order(OrderRequest(run.gid, "c", "d", leg, link))
    m = run.ex.meas[link]
    assert m["cancel_sent_ms"] is not None and "cancel_acked_ms" not in m and "cancel_result" not in m                             # the timeout is recorded as NO ANSWER
    w.clock.sleep(10)
    w.fake.fill(link, "0.001")                                                                 # the venue fills it AFTER the cancel request
    view = run.ex.venue_state()
    q = run.execution_quality(view, True, None)
    pl = next(l for l in q["legs"] if l["link_id"] == link)
    assert pl["cancel"]["result"] == EQ.UNKNOWN and pl["cancel"]["fills_after_request"] == 1 and pl["liquidity"] == "MAKER" and "cancel_result" in pl["unknown"]


def test_a_restart_keeps_the_timestamps_the_first_process_measured(w):
    _ready(w)
    run, ctx, rec = begin(w)
    view = run.ex.venue_state()
    first = run.execution_quality(view, False, None)
    sent = {l["link_id"]: l["timing"]["sent_ms"] for l in first["legs"]}
    assert all(v is not None for v in sent.values())
    w.clock.sleep(30)
    run2 = restart(w, ctx, run.gid)                                                           # a NEW process: its executor has measured nothing
    assert run2.ex.meas == {}
    again = run2.execution_quality(run2.ex.venue_state(), False, None)
    assert {l["link_id"]: l["timing"]["sent_ms"] for l in again["legs"]} == sent               # merged from disk: nothing invented, nothing lost


def test_a_measurement_fault_never_reaches_the_trading_path(w, monkeypatch):
    _ready(w)
    monkeypatch.setattr(EQ, "leg_quality", lambda **k: (_ for _ in ()).throw(RuntimeError("boom")))
    assert w.execute() == 0, w.text()
    assert w.record()["flat_verified"] is True and "execution quality not written" in w.text()
    assert all(p["size"] == 0 for p in w.fake.positions.values())


def test_the_evidence_events_are_idempotent_pass_the_guard_and_name_unknowns(w):
    _ready(w)
    assert w.execute() == 0
    q = _quality(w)
    evs = EL.execution_quality(q)
    assert len(evs) == len(q["legs"]) + 1 and {e[1] for e in evs} == {"EXECUTION_QUALITY_LEG", "EXECUTION_QUALITY_GROUP"}
    for i, (kind, stage, payload) in enumerate(evs):
        XE.make_event(kind, {"stage": stage, **payload}, session_id=None, occurred_at_ms=1, event_id=EL.event_id("s", kind, stage, i))           # the secret guard accepts it
    assert evs == EL.execution_quality(q)                                                      # deterministic: re-emitting the same session is the same bytes
    blob = json.dumps(evs)
    assert not any(str(x["fill_id"]) in blob for l in q["legs"] for x in [{"fill_id": i} for i in l["fills"]["ids"]])                   # venue ids are shortened references, never the ids


def test_the_session_record_schema_is_untouched(w):
    _ready(w)
    assert w.execute() == 0
    import ms_testnet_session as S
    assert S.record_refusals(w.record()) == [] and "execution_quality" not in w.record()


# ------------------------------------------------------------------ research feedback: measured vs modelled
def _row(style="TAKER", measured="22.0", modelled="20.0", expected="9.0", realised="8.0", done=True):
    return {"style": style, "attempt_completed": done, "cost_complete": True, "total_execution_cost_bps_of_position_notional": measured, "total_execution_cost_usd": "0.2",
            "position_notional_usd": "100", "modelled_cost_bps": modelled, "expected_funding_bps": expected, "realised_funding_bps": realised}


def test_the_feedback_report_says_how_far_the_model_is_from_measurement_and_never_averages_a_handful():
    rep = EQ.feedback_report([_row(measured="22"), _row(measured="26"), _row(measured="19")])
    mv = rep["model_vs_measured"]
    assert mv["sessions"] == 3 and mv["mean_measured_cost_bps"] == "22.3333" and mv["mean_modelled_cost_bps"] == "20.0000" and mv["mean_cost_surprise_bps"] == "2.3333"
    assert mv["sessions_costlier_than_modelled"] == 2 and mv["status"] == "INSUFFICIENT_SAMPLE" and rep["policy"]["verdict"] == "INSUFFICIENT_SAMPLE"
    f = rep["funding_vs_expected"]
    assert f["mean_shortfall_bps"] == "-1.0000" and f["status"] == "INSUFFICIENT_SAMPLE"
    big = EQ.feedback_report([_row(measured=str(20 + i % 5)) for i in range(30)], min_orders=30)
    assert big["model_vs_measured"]["status"] == "ENOUGH"
    empty = EQ.feedback_report([])
    assert empty["model_vs_measured"]["mean_cost_surprise_bps"] is None and empty["sessions_logged"] == 0          # nothing is invented from no data


def test_an_unknown_model_cost_or_unresolved_funding_is_unknown_in_the_row_never_zero():
    q = {"group": {"style": "TAKER", "attempt_completed": True, "cost_complete": True, "position_notional_usd": "100", "total_execution_cost_usd": "0.2",
                   "total_execution_cost_bps_of_position_notional": "20.0"}}
    r = EQ.feedback_row(session="s", at_ms=1, decision_id="dec_1", quality=q, funding=None, edge={"costs_bps": {"fees": "5", "spread": None, "impact": "1"}, "expected_funding_bps": "9"}, flat_verified=True)
    assert r["modelled_cost_bps"] is None and r["realised_funding_bps"] is None and r["funding_booked_usd"] is None and r["expected_funding_bps"] == "9.0000"
    unresolved = {"recognitions": [{"all_prints_resolved": False, "booked_usd": None}]}
    assert EQ.feedback_row(session="s", at_ms=1, decision_id=None, quality=q, funding=unresolved, edge=None, flat_verified=True)["realised_funding_bps"] is None
    booked = {"recognitions": [{"all_prints_resolved": True, "booked_usd": "0.9"}]}
    assert EQ.feedback_row(session="s", at_ms=1, decision_id=None, quality=q, funding=booked, edge=None, flat_verified=True)["realised_funding_bps"] == "90.0000"


def test_the_modelled_cost_is_like_for_like_only_fees_spread_and_slippage_lines_count():
    """Review finding: impact, basis stress, hedge error, adverse selection and borrow are not what a leg's measured fee + spread + walk cover; summing them in biased the 'surprise' negative."""
    q = {"group": {"style": "TAKER", "attempt_completed": True, "cost_complete": True, "position_notional_usd": "100", "total_execution_cost_usd": "0.3",
                   "total_execution_cost_bps_of_position_notional": "30.0"}}
    edge = {"costs_bps": {"fees": "31", "spread": "0.3", "slippage": "0", "impact": "1", "basis": "3", "hedge_error": "0.05", "adverse_selection": "0", "borrow": "0", "maker_fill": "0"},
            "expected_funding_bps": "80"}
    r = EQ.feedback_row(session="s", at_ms=1, decision_id=None, quality=q, funding=None, edge=edge, flat_verified=True)
    assert r["modelled_cost_bps"] == "31.3000"                                        # 31 + 0.3 + 0, NOT 35.35
    assert EQ.feedback_row(session="s", at_ms=1, decision_id=None, quality=q, funding=None, edge={"costs_bps": {"impact": "1"}}, flat_verified=True)["modelled_cost_bps"] is None


def test_a_planned_entry_leg_the_engine_never_sent_is_in_the_record_and_the_group_is_not_hedged(w):
    from ms_fake_bybit_testnet import SimulatedCrash
    _ready(w)
    d = w.plan_doc()["plan_digest"]
    w.fake.inject(CREATE_PATH, "crash_after", match=lambda b: b["orderLinkId"].endswith("-p"))       # the process dies right after the perp leg: the spot leg was never sent
    with pytest.raises(SimulatedCrash):
        w.run("execute", "--confirm-plan-digest", d, typed=d)
    assert w.run("reconcile", "--unwind", "--confirm-plan-digest", d) == 0, w.text()
    g = _quality(w)["group"]
    assert g["entry_legs_planned"] == 2 and g["entry_legs_filled"] == 1 and g["all_entry_legs_filled"] is False and g["attempt_completed"] is True


# ------------------------------------------------------------------ a stale reference is not the market the order met
def test_a_stale_reference_quote_makes_the_cost_unknown_never_a_number():
    old = {**REF, "age_ms_at_send": 3_600_000}                                   # the quote is an hour old
    r = leg("Buy", [fill(1, "1", "100.20", "0.05")], ref=old, qty="1")
    assert "reference_stale" in r["unknown"] and r["cost"]["price_vs_mid_usd"] is None and r["cost"]["total_usd"] is None
    assert r["fills"]["vwap"] == "100.20000000"                                  # what DID happen is still recorded
    fresh = leg("Buy", [fill(1, "1", "100.20", "0.05")], ref={**REF, "age_ms_at_send": 4_999}, qty="1")
    assert "reference_stale" not in fresh["unknown"] and D(fresh["cost"]["total_usd"]) > 0
    over = leg("Buy", [fill(1, "1", "100.20", "0.05")], ref={**REF, "age_ms_at_send": 5_001}, qty="1")
    assert "reference_stale" in over["unknown"]                                  # the entry bound is 5 s
    ex = leg("Sell", [fill(1, "1", "100.10")], ref={**REF, "age_ms_at_send": 30_000}, qty="1", kind="EXIT", reduce=True)
    assert "reference_stale" not in ex["unknown"]                                # an exit may follow a hold: 60 s
    ex2 = leg("Sell", [fill(1, "1", "100.10")], ref={**REF, "age_ms_at_send": 61_000}, qty="1", kind="EXIT", reduce=True)
    assert "reference_stale" in ex2["unknown"]


def test_a_blank_venue_fee_is_unknown_not_a_measured_zero():
    row = {**fill(1, "1", "100.20"), "fee_usd": 0.0, "fee_signed_usd": None, "fee_known": False}      # what the executor now emits for a Trade row with no execFee
    r = leg("Buy", [row], qty="1")
    assert "fee" in r["unknown"] and r["cost"]["total_usd"] is None and r["fee"]["usd_signed"] is None
    zero = leg("Buy", [{**fill(2, "1", "100.20"), "fee_usd": 0.0, "fee_signed_usd": 0.0, "fee_known": True}], qty="1")
    assert "fee" not in zero["unknown"] and D(zero["cost"]["fee_usd"]) == 0            # a fee the venue STATED as zero is still a measured zero
