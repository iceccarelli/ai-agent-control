"""The execution-cost estimator: only real, valid, earlier, comparable fills;
unavailable (never zero) when the evidence is short."""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from typing import Any, Dict

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import execution_cost_estimate as ece                        # noqa: E402

NOW = 1_800_000_000_000
DAY = ece.DAY_MS


@dataclass
class E:
    kind: str
    ms: int
    ref: str
    fee_raw: Dict[str, Any] = field(default_factory=dict)


def rec(side="Sell", product="linear", q=0.01, ref=60000.0, slip_usd=0.12,
        spr_usd=0.3, status="MEASURED", basis="book", order_type="market",
        symbol="BTCUSDT", req=None, **over):
    r = {"schema": 1, "status": status, "reference_basis": basis,
         "symbol": symbol, "product": product, "side": side,
         "order_type": order_type, "filled_qty": q, "requested_qty": req or q,
         "avg_price": ref, "reference_price": ref, "slippage_usd": slip_usd,
         "spread_cost_usd": spr_usd, "decision_to_submit_ms": 10,
         "submit_to_fill_ms": 50}
    r.update(over)
    return r


def fill(i, *, kind="perp_open", ago_days=1.0, **kw):
    return E(kind, int(NOW - ago_days * DAY - i), f"BB-{kind}-{i}",
             {"execution": rec(**kw)})


def many(n, **kw):
    return [fill(i, **kw) for i in range(n)]


LEG = dict(as_of_ms=NOW, symbol="BTCUSDT", product="linear", side="Sell",
           planned_notional_usd=600.0)


def test_enough_valid_samples_give_a_conservative_bound():
    out = ece.estimate_leg(many(25), **LEG)
    assert out["status"] == ece.MEASURED and out["n_in_size_band"] == 25
    s = out["slippage_bps"]
    assert s["mean"] == pytest.approx(0.12 / 600.0 * 1e4)
    assert s["bound"] >= s["mean"]                       # never below the mean
    assert out["cost_bound_bps"] == pytest.approx(
        out["slippage_bps"]["bound"] + out["half_spread_bps"]["bound"])
    assert out["survivorship"]


def test_too_few_samples_is_unavailable_not_zero():
    out = ece.estimate_leg(many(19), **LEG)
    assert out["status"] == ece.UNAVAILABLE and out["cost_bound_bps"] is None
    assert "INSUFFICIENT_SAMPLES" in out["reasons"][0]


def test_price_improvement_is_never_credited():
    out = ece.estimate_leg(many(25, slip_usd=-0.5, spr_usd=0.0), **LEG)
    assert out["slippage_bps"]["mean"] < 0 and out["slippage_bps"]["bound"] == 0.0


def test_variance_widens_the_bound():
    calm = ece.estimate_leg(many(25), **LEG)
    noisy = [fill(i, slip_usd=0.12 + (0.4 if i % 2 else -0.1)) for i in range(25)]
    wide = ece.estimate_leg(noisy, **LEG)
    assert wide["slippage_bps"]["bound"] > calm["slippage_bps"]["bound"]


def test_buy_and_sell_are_not_pooled():
    sells = many(25, side="Sell")
    out = ece.estimate_leg(sells, **{**LEG, "side": "Buy"})
    assert out["status"] == ece.UNAVAILABLE and out["n"] == 0


def test_other_symbol_product_and_order_type_are_not_pooled():
    assert ece.estimate_leg(many(25, symbol="ETHUSDT"), **LEG)["n"] == 0
    assert ece.estimate_leg(many(25, product="spot"), **LEG)["n"] == 0
    assert ece.estimate_leg(many(25, order_type="post_only"), **LEG)["n"] == 0


@pytest.mark.parametrize("over,why", [
    ({"status": "UNAVAILABLE"}, "NOT_BOOK_MEASURED"),
    ({"basis": "mark"}, "NOT_BOOK_MEASURED"),
    ({"slip_usd": None}, "MALFORMED"),
    ({"spr_usd": float("nan")}, "MALFORMED"),
    ({"q": 0.0}, "MALFORMED"),
    ({"q": 0.02, "req": 0.01}, "FILL_EXCEEDS_ORDER"),
    ({"decision_to_submit_ms": -5}, "CONTRADICTORY_TIMESTAMPS"),
])
def test_invalid_records_are_rejected_with_a_reason(over, why):
    out = ece.estimate_leg(many(25, **over), **LEG)
    assert out["status"] == ece.UNAVAILABLE and out["rejected"].get(why) == 25


def test_a_fill_without_a_record_is_rejected():
    out = ece.estimate_leg([E("perp_open", NOW - DAY, "BB-x", {})] * 3, **LEG)
    assert out["rejected"]["NO_EXECUTION_RECORD"] == 3


def test_later_fills_are_invisible_to_the_decision():
    """No look-ahead: a fill after as_of_ms must not enter the estimate."""
    early = many(25)
    late = [fill(100 + i, ago_days=-1.0) for i in range(10)]     # in the future
    base = ece.estimate_leg(early, **LEG)
    both = ece.estimate_leg(early + late, **LEG)
    assert both["n"] == base["n"] == 25
    assert both["rejected"]["AFTER_DECISION"] == 10
    assert both["slippage_bps"] == base["slippage_bps"]


def test_the_same_row_twice_counts_once():
    rows = many(25)
    out = ece.estimate_leg(rows + [rows[0]], **LEG)
    assert out["n"] == 25 and out["rejected"]["DUPLICATE_ROW"] == 1


def test_partial_fill_and_cancel_increment_are_one_order():
    parts = []
    for i in range(25):
        parts.append(E("perp_open", int(NOW - DAY - i), f"BB-o-{i}",
                       {"execution": rec(q=0.005, slip_usd=0.06, spr_usd=0.15)}))
        parts.append(E("perp_open", int(NOW - DAY - i + 1), f"BB-o-{i}+cancel",
                       {"execution": rec(q=0.005, slip_usd=0.06, spr_usd=0.15)}))
    out = ece.estimate_leg(parts, **LEG)
    assert out["n"] == 25                                # 25 orders, not 50 fills
    assert out["slippage_bps"]["mean"] == pytest.approx(0.12 / 600.0 * 1e4)


def test_old_samples_are_dropped_and_a_stale_newest_is_refused():
    old = many(25, ago_days=40.0)
    out = ece.estimate_leg(old, **LEG)
    assert out["status"] == ece.UNAVAILABLE and out["rejected"]["TOO_OLD"] == 25
    stale = many(25, ago_days=10.0)                       # inside 30d, newest > 7d
    out = ece.estimate_leg(stale, **LEG)
    assert out["status"] == ece.UNAVAILABLE and "STALE_SAMPLES" in out["reasons"][0]


def test_unrepresentative_sizes_are_excluded():
    big = many(25, q=1.0)                                  # $60,000 vs a $600 plan
    out = ece.estimate_leg(big, **LEG)
    assert out["status"] == ece.UNAVAILABLE
    assert out["rejected"]["OUTSIDE_SIZE_BAND"] == 25


def test_bad_planned_notional_is_unavailable():
    for bad in (0, -1, float("nan"), None):
        out = ece.estimate_leg(many(25), **{**LEG, "planned_notional_usd": bad})
        assert out["status"] == ece.UNAVAILABLE


def test_the_policy_can_only_get_stricter():
    ece.CostSamplePolicy(min_samples=40, z=2.0).validate()
    for loose in (dict(min_samples=5), dict(max_sample_age_days=90),
                  dict(max_newest_age_days=30), dict(size_band=10), dict(z=1.0)):
        with pytest.raises(ValueError):
            ece.estimate_leg(many(25), **LEG, policy=ece.CostSamplePolicy(**loose))


def test_round_trip_needs_every_leg_and_names_the_missing_ones():
    entries = many(25, side="Sell")                        # only the perp entry leg
    out = ece.estimate_round_trip(entries, as_of_ms=NOW, execution_mode="overlay",
                                  perp_symbol="BTCUSDT", spot_symbol="BTCUSDT",
                                  planned_notional_usd=600.0)
    assert out["status"] == ece.UNAVAILABLE and out["spread_bps"] is None
    assert any("linear/Buy" in r for r in out["reasons"])
    assert not any("linear/Sell" in r for r in out["reasons"])


def test_round_trip_sums_the_legs_once():
    entries = many(25, side="Sell") + many(25, kind="perp_close", side="Buy")
    out = ece.estimate_round_trip(entries, as_of_ms=NOW, execution_mode="overlay",
                                  perp_symbol="BTCUSDT", spot_symbol="BTCUSDT",
                                  planned_notional_usd=600.0)
    assert out["status"] == ece.MEASURED
    assert out["spread_bps"] == pytest.approx(
        sum(l["half_spread_bps"]["bound"] for l in out["legs"]))
    assert len(out["legs"]) == 2
    acquire = ece.required_legs("acquire", "BTCUSDT", "BTCUSDT")
    assert len(acquire) == 4 and {p for _, p, _ in acquire} == {"linear", "spot"}


def test_estimation_changes_nothing_it_is_given():
    rows = many(25)
    snapshot = [dict(r.fee_raw["execution"]) for r in rows]
    ece.estimate_leg(rows, **LEG)
    assert [r.fee_raw["execution"] for r in rows] == snapshot
