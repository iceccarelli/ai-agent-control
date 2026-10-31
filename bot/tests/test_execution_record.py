"""Pure execution-record maths: sign, spread vs slippage, partials, stale/invalid
inputs, contradictions, delays, expected-vs-actual, reconciliation."""
from __future__ import annotations

import math
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import execution_record as xr  # noqa: E402

NOW = 1_700_000_100_000


def ctx(**kw):
    base = dict(venue="bybit", symbol="BTCUSDT", product="linear", side="Sell",
                decision_ms=NOW - 1000, qty=1.0, mark=100_000.0,
                bid=99_990.0, ask=100_010.0, observed_ms=NOW - 1000,
                expected_fee_usd=55.0, expected_cost_usd=80.0)
    base.update(kw)
    return xr.DecisionContext(**base)


def assess(c=None, **kw):
    base = dict(side="Sell", symbol="BTCUSDT", product="linear",
                venue="bybit", order_type="market", requested_qty=1.0,
                filled_qty=1.0, avg_price=99_985.0, now_ms=NOW,
                fee_usd=55.0, fee_confirmed=True, order_link_id="L1",
                submit_ms=NOW - 900, fill_ms=NOW - 800)
    base.update(kw)
    return xr.assess_fill(ctx() if c is None else c, **base)


def test_sell_slippage_is_against_bid_and_spread_is_separate():
    r = assess()
    assert r["status"] == xr.MEASURED
    assert r["reference_basis"] == "executable_side"
    assert r["reference_price"] == 99_990.0          # the bid a Sell hits
    assert r["slippage_usd"] == pytest.approx(5.0)   # sold 5 below the bid
    assert r["spread_cost_usd"] == pytest.approx(10.0)   # half of 20
    # no double count: shortfall vs mid = (mid - fill) * q exactly
    assert r["shortfall_vs_mid_usd"] == pytest.approx(100_000.0 - 99_985.0)
    assert r["actual_cost_usd"] == pytest.approx(15.0 + 55.0)
    assert r["cost_variance_usd"] == pytest.approx(70.0 - 80.0)


def test_buy_sign_and_price_improvement_is_negative():
    r = assess(ctx(side="Buy"), side="Buy", avg_price=100_005.0)
    assert r["reference_price"] == 100_010.0
    assert r["slippage_usd"] == pytest.approx(-5.0)    # filled below the ask
    assert r["spread_cost_usd"] == pytest.approx(10.0)


def test_mark_only_basis_keeps_spread_unavailable():
    r = assess(ctx(bid=None, ask=None), avg_price=99_980.0)
    assert r["reference_basis"] == "mark" and r["reference_price"] == 100_000.0
    assert r["slippage_usd"] == pytest.approx(20.0)
    assert r["spread_cost_usd"] is None
    assert any(x.startswith("SPREAD_UNAVAILABLE") for x in r["reasons"])


def test_partial_fill_counts_only_filled_and_prorates_expected():
    r = assess(requested_qty=1.0, filled_qty=0.4, avg_price=99_980.0)
    assert r["filled_qty"] == 0.4 and r["unfilled_qty"] == pytest.approx(0.6)
    assert r["fill_ratio"] == pytest.approx(0.4)
    assert r["slippage_usd"] == pytest.approx(10.0 * 0.4)
    assert r["expected_fee_usd"] == pytest.approx(55.0 * 0.4)
    assert r["expected_cost_usd"] == pytest.approx(80.0 * 0.4)


def test_zero_fill_is_no_fill_and_measures_nothing():
    r = assess(filled_qty=0.0, avg_price=0.0)
    assert r["status"] == xr.NO_FILL
    assert r["slippage_usd"] is None and r["unfilled_qty"] == 1.0
    assert r["reference_price"] is None or r["filled_qty"] == 0.0


def test_fill_exceeding_order_is_contradictory_and_unmeasured():
    r = assess(requested_qty=1.0, filled_qty=1.5)
    assert r["status"] == xr.UNAVAILABLE
    assert "FILL_EXCEEDS_ORDER" in r["reasons"]
    assert r["slippage_usd"] is None and r["reference_price"] is None
    assert r["unfilled_qty"] is None


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), 0.0, -5.0,
                                 "abc", True])
def test_invalid_mark_or_book_is_unavailable_never_fabricated(bad):
    for c in (ctx(mark=bad), ctx(bid=bad), ctx(ask=bad)):
        r = assess(c)
        if r["status"] == xr.MEASURED:
            # `None`-like coercions never happen; only valid inputs measure
            assert r["reference_price"] and math.isfinite(r["reference_price"])
        else:
            assert r["status"] == xr.UNAVAILABLE
            assert r["reference_price"] is None and r["slippage_usd"] is None


def test_missing_context_and_no_prices_are_unavailable():
    r = xr.assess_fill(None, side="Sell", symbol="S", product="linear",
                       venue="bybit", order_type="market", requested_qty=1,
                       filled_qty=1, avg_price=1.0, now_ms=NOW)
    assert r["status"] == xr.UNAVAILABLE
    assert "NO_DECISION_CONTEXT" in r["reasons"]
    r2 = assess(ctx(mark=None, bid=None, ask=None))
    assert "NO_DECISION_PRICE" in r2["reasons"] and r2["status"] == xr.UNAVAILABLE


def test_stale_decision_and_stale_observation():
    old = assess(ctx(decision_ms=NOW - 10 * 60_000, observed_ms=None),
                 submit_ms=NOW - 900)
    assert old["status"] == xr.UNAVAILABLE and "DECISION_STALE" in old["reasons"]
    stale_obs = assess(ctx(observed_ms=NOW - 1000 - 60_000))
    assert "OBSERVATION_STALE" in stale_obs["reasons"]
    assert stale_obs["reference_price"] is None


def test_future_and_later_observation_rejected():
    fut = assess(ctx(decision_ms=NOW + 60_000, observed_ms=None))
    assert "DECISION_TIME_IN_FUTURE" in fut["reasons"]
    later = assess(ctx(decision_ms=NOW - 10_000, observed_ms=NOW),
                   submit_ms=NOW - 900)        # observed after the decision
    assert "OBSERVATION_AFTER_DECISION" in later["reasons"]
    assert later["reference_price"] is None


def test_crossed_book_and_mark_contradiction():
    assert "BOOK_CROSSED" in assess(ctx(bid=100_100.0, ask=99_900.0))["reasons"]
    assert "MARK_CONTRADICTS_BOOK" in assess(ctx(mark=150_000.0))["reasons"]


def test_delays_and_contradictory_timestamps():
    r = assess()
    assert r["decision_to_submit_ms"] == 100 and r["submit_to_fill_ms"] == 100
    bad = assess(submit_ms=NOW - 5000)          # submitted before the decision
    assert bad["decision_to_submit_ms"] is None
    assert "SUBMIT_BEFORE_DECISION" in bad["reasons"]
    back = assess(fill_ms=NOW - 2000)
    assert back["submit_to_fill_ms"] is None and "FILL_BEFORE_SUBMIT" in back["reasons"]


def test_delayed_execution_measures_slippage_against_decision_not_submit():
    # decision 20s ago, submitted 19s ago, price ran away 30 after the decision
    r = assess(ctx(decision_ms=NOW - 20_000, observed_ms=NOW - 20_000),
               submit_ms=NOW - 19_000, fill_ms=NOW - 18_900,
               avg_price=99_960.0)
    assert r["status"] == xr.MEASURED
    assert r["decision_to_submit_ms"] == 1000
    assert r["slippage_usd"] == pytest.approx(30.0)


def test_unconfirmed_fee_withholds_actual_cost_only():
    r = assess(fee_confirmed=False)
    assert r["status"] == xr.MEASURED and r["fee_usd"] is None
    assert r["actual_cost_usd"] is None and r["cost_variance_usd"] is None


def test_failed_submission_record_executes_nothing():
    r = xr.failed_submission(ctx(), side="Sell", symbol="BTCUSDT",
                             product="linear", venue="bybit",
                             order_type="market", requested_qty=1.0,
                             why="SUBMISSION_FAILED: Boom")
    assert r["status"] == xr.NO_FILL and r["filled_qty"] == 0.0
    assert r["unfilled_qty"] == 1.0 and r["venue"] == "bybit"


def test_reconcile_flags_qty_fee_and_orphans():
    recs = [assess(), assess(order_link_id="L2", filled_qty=0.5,
                             requested_qty=1.0, fee_usd=10.0)]
    booked = [{"ref": "L1", "qty": 1.0, "price": 99_985.0, "fee_usdt": 55.0,
               "fee_btc": 0.0},
              {"ref": "L2", "qty": 0.4, "price": 99_985.0, "fee_usdt": 9.0,
               "fee_btc": 0.0},
              {"ref": "L3", "qty": 1.0, "price": 1.0, "fee_usdt": 0.0,
               "fee_btc": 0.0}]
    problems = xr.reconcile(recs, booked)
    assert len(problems) == 3
    assert xr.reconcile(recs[:1], booked[:1]) == []
