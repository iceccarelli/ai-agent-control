"""Taker vs maker vs hybrid execution (Phase 5): a touch is not a fill, an assumption is not a
measurement, and the break-even numbers are conditions, not forecasts.

Every number checked against the module is also computed here from the primitives
(`md_economics.walk_leg`, the Gaussian cdf, the fee schedule) so the tests do not restate the code."""
from __future__ import annotations

import ast
import json
import math
import os
import random
import subprocess
import sys
from statistics import NormalDist

import pytest

import execution_model as EM
import md_economics as ME
import trade_economics as TE

HERE = os.path.dirname(os.path.abspath(__file__))
BOT = os.path.dirname(HERE)

PHI0 = 1.0 / math.sqrt(2.0 * math.pi)

TIGHT_BIDS = ((89999.9, 2.0), (89999.8, 3.0), (89999.5, 8.0), (89999.0, 20.0))
TIGHT_ASKS = ((90000.0, 2.0), (90000.1, 3.0), (90000.4, 8.0), (90001.0, 20.0))
TIGHT = {"perp_bids": TIGHT_BIDS, "perp_asks": TIGHT_ASKS, "spot_bids": TIGHT_BIDS, "spot_asks": TIGHT_ASKS}
ASYM_BIDS = ((89999.9, 1.0), (89999.0, 2.0), (89998.0, 5.0))          # thin bids: selling 3 BTC walks two levels
ASYM_ASKS = ((90000.0, 3.0), (90000.1, 3.0), (90001.0, 9.0))          # deep asks: buying 3 BTC walks none
ASYM = {"perp_bids": ASYM_BIDS, "perp_asks": ASYM_ASKS, "spot_bids": ASYM_BIDS, "spot_asks": ASYM_ASKS}
REF, NOTIONAL = 90000.0, 270_000.0                    # 3.0 BTC per leg: walks past the first level

COARSE_B = ((100.0, 1.0), (99.0, 2.0), (98.0, 5.0))
COARSE_A = ((101.0, 1.0), (102.0, 2.0), (103.0, 5.0))
COARSE = {"perp_bids": COARSE_B, "perp_asks": COARSE_A, "spot_bids": COARSE_B, "spot_asks": COARSE_A}

FEES = ME.FeeSchedule.published_default()             # 5.5 bps perp, 10 bps spot, unverified

BASE = dict(fill_probability=0.6, adverse_selection_bps=1.0, latency_ms=50.0, vol_bps_per_sqrt_s=0.9,
            maker_fee_bps_perp=2.0, maker_fee_bps_spot=10.0, nonfill_penalty_bps=5.0,
            taker_leg_fail_probability=0.01)


def assume(**over):
    vals = {**BASE, **over}
    return EM.Assumptions.of(**{k: EM.Assumption(v, f"test fixture for {k}") for k, v in vals.items() if v is not None})


def scenario(mode=EM.OVERLAY, depth=TIGHT, notional=NOTIONAL, ref=REF, timeout=5000.0, fees=FEES,
             params=None, assumptions=None, **over):
    return EM.ExecutionScenario(depth, mode, notional, ref, timeout, fees, params or {},
                                assumptions if assumptions is not None else assume(**over))


def run(sc, n=200, **kw):
    return EM.compare_execution_modes(sc, n_paths=n, **kw)


def expected(sc):
    r = run(sc, 100)
    return {m: r[EM.SIMULATED_FILL]["modes"][m] for m in EM.MODES}


def walks(mode, depth, notional, ref):
    qty = notional / ref
    legs = {}
    for inst, phase, side, name in (("perp", "entry", "sell", "perp_sell_entry"), ("perp", "exit", "buy", "perp_buy_exit"),
                                    ("spot", "entry", "buy", "spot_buy_entry"), ("spot", "exit", "sell", "spot_sell_exit")):
        if inst == "spot" and mode != EM.ACQUIRE:
            continue
        legs[(inst, phase)] = ME.walk_leg(name, side, qty, depth[f"{inst}_bids"], depth[f"{inst}_asks"])
    return legs


def D(sigma, ms):
    return PHI0 * sigma * math.sqrt(ms / 1000.0)


def hand_taker(mode, depth, notional, ref, sigma, lat):
    """TAKER round trip from the primitives, no legging (OVERLAY, or ACQUIRE with zero failure)."""
    total = 0.0
    for (inst, _), w in walks(mode, depth, notional, ref).items():
        fee = FEES.perp_taker_bps if inst == "perp" else FEES.spot_taker_bps
        total += w.half_spread_bps + w.slippage_bps + fee + D(sigma, lat)
    return total


def hand_overlay_hybrid(p, a, lat, timeout, sigma, fee_m, depth=TIGHT, notional=NOTIONAL, ref=REF):
    total = 0.0
    for w in walks(EM.OVERLAY, depth, notional, ref).values():
        h = w.half_spread_bps
        c_tk = h + w.slippage_bps + FEES.perp_taker_bps
        c_mk = -h + fee_m + a
        sd = sigma * math.sqrt(lat / 1000.0)
        q = 0.0 if sd == 0 else NormalDist().cdf(-2 * h / sd)
        total += q * (c_tk + D(sigma, 3 * lat)) + (1 - q) * (p * c_mk + (1 - p) * (c_tk + D(sigma, 2 * lat + timeout)))
    return total


# -- records -----------------------------------------------------------------------------------------------

DAY = 86_400_000.0


def maker_record(i, n, status, *, markout=0.6, fee_bps=2.0, side="buy", market="linear_perp", span_days=3.0,
                 with_fee=True):
    qty = 0.1
    filled = {"FILLED": qty, "PARTIALLY_FILLED": qty * 0.4}.get(status, 0.0)
    sub = i * span_days * DAY / n
    kw = {}
    if filled:
        kw = dict(avg_fill_price=90000.0, arrival_mid=90000.05, mid_after_fill=90000.0 * (1 - markout / 1e4),
                  markout_horizon_ms=1000.0)
        if with_fee:
            kw.update(fee_quote=filled * 90000.0 * fee_bps / 1e4, fee_record_id=f"exec-{i}")
    return EM.AccountExecutionRecord(
        order_id=f"m-{i}", symbol="BTCUSDT", market=market, side=side, intent=EM.MAKER_POST_ONLY, status=status,
        qty_requested=qty, qty_filled=filled, submitted_ms=sub, terminal_ms=sub + 3000.0,
        source="excerpt of a venue /v5/execution/list reply", **kw)


def maker_records(n=80, full=60, partial=0, rejected=4, *, markout_fn=None, seed=11, **kw):
    statuses = (["FILLED"] * full + ["PARTIALLY_FILLED"] * partial + ["REJECTED_POST_ONLY"] * rejected)
    statuses += ["CANCELLED_UNFILLED"] * (n - len(statuses))
    random.Random(seed).shuffle(statuses)
    out = []
    for i, st in enumerate(statuses):
        mo = markout_fn(i) if markout_fn else 0.2 + 0.1 * ((i * 7) % 9)
        out.append(maker_record(i, n, st, markout=mo, **kw))
    return out


def taker_record(i, n=20):
    sub = i * 3 * DAY / n
    return EM.AccountExecutionRecord(
        order_id=f"t-{i}", symbol="BTCUSDT", market="linear_perp", side="sell", intent=EM.TAKER_ORDER, status="FILLED",
        qty_requested=0.1, qty_filled=0.1, submitted_ms=sub, terminal_ms=sub + 200.0, source="excerpt of a venue execution list",
        avg_fill_price=89999.9, fee_quote=0.1 * 89999.9 * 5.5 / 1e4, fee_record_id=f"tx-{i}", arrival_mid=89999.95)


# ====================================================================================================
# parameters: provenance, refusal, nothing defaulted
# ====================================================================================================

def test_assumed_cannot_be_built_as_a_param_and_unknown_carries_no_value():
    with pytest.raises(ValueError, match="Assumptions"):
        EM.Param(0.5, EM.ASSUMED, "wishful")
    with pytest.raises(ValueError):
        EM.Param(0.5, EM.UNKNOWN)
    assert EM.UNKNOWN_PARAM.value is None and EM.UNKNOWN_PARAM.provenance == EM.UNKNOWN
    with pytest.raises(ValueError, match="source"):
        EM.Param(0.5, EM.MEASURED, "")
    with pytest.raises(ValueError):
        EM.Param(float("nan"), EM.ESTIMATED, "x")
    with pytest.raises(ValueError):
        EM.Param(0.5, "GUESSED", "x")
    assert EM.measured(0.5, "venue").provenance == EM.MEASURED and EM.estimated(0.5, "paper").provenance == EM.ESTIMATED


def test_an_assumption_needs_a_rationale_and_a_known_parameter_name():
    with pytest.raises(ValueError):
        EM.Assumption(0.5, "  ")
    with pytest.raises(ValueError):
        EM.Assumption(float("inf"), "because")
    with pytest.raises(ValueError, match="unknown parameter"):
        EM.Assumptions.of(fill_probabilty=EM.Assumption(0.5, "typo"))
    with pytest.raises(ValueError, match="unknown parameter"):
        run(scenario(params={"latancy_ms": EM.estimated(5.0, "typo")}, assumptions=assume()))


def test_a_name_given_as_both_a_param_and_an_assumption_is_refused():
    with pytest.raises(ValueError, match="both"):
        run(scenario(params={"latency_ms": EM.estimated(30.0, "ping")}))


@pytest.mark.parametrize("name,bad", [("fill_probability", 1.5), ("fill_probability", -0.1), ("cancel_fraction_ahead", 1.01),
                                      ("adverse_selection_bps", -1.0), ("latency_ms", -5.0), ("mean_trade_size_btc", 0.0),
                                      ("vol_bps_per_sqrt_s", float("nan"))])
def test_out_of_range_inputs_are_errors_not_clamped(name, bad):
    over = {name: bad}
    if name in ("cancel_fraction_ahead", "mean_trade_size_btc"):               # only meaningful in the queue model
        over = {"fill_probability": None, "queue_ahead_btc": 1.0, "through_volume_rate_btc_per_s": 1.0,
                "cancel_fraction_ahead": 0.0, "mean_trade_size_btc": 1.0, name: bad}
    with pytest.raises(ValueError):
        run(scenario(assumptions=assume(**over)))


def test_unknown_inputs_refuse_the_modes_that_need_them_and_never_default_to_a_number():
    sc = scenario(assumptions=assume(vol_bps_per_sqrt_s=None, adverse_selection_bps=None))
    r = run(sc)
    modes = r[EM.SIMULATED_FILL]["modes"]
    for m in EM.MODES:
        assert modes[m]["status"] == EM.REFUSED and modes[m]["expected_cost_bps"] is None
        assert "vol_bps_per_sqrt_s" in modes[m]["unknown_parameters"]
    assert "adverse_selection_bps" in modes[EM.HYBRID]["unknown_parameters"]
    assert "adverse_selection_bps" not in modes[EM.TAKER]["unknown_parameters"]
    assert r[EM.SIMULATED_FILL]["unknown_parameters_by_mode"][EM.MAKER] == modes[EM.MAKER]["unknown_parameters"]
    assert "monte_carlo" not in modes[EM.TAKER]
    # the book-walk and fee floor is shown, but labelled as not a cost
    floor = modes[EM.TAKER]["lower_bound_bps_not_a_cost"]
    assert floor["value"] == pytest.approx(11.0 + sum(w.half_spread_bps + w.slippage_bps for w in walks(EM.OVERLAY, TIGHT, NOTIONAL, REF).values()))


def test_a_declared_unknown_param_is_the_same_as_a_missing_one():
    sc = scenario(params={"latency_ms": EM.UNKNOWN_PARAM}, assumptions=assume(latency_ms=None))
    r = run(sc)
    assert r[EM.SIMULATED_FILL]["modes"][EM.TAKER]["unknown_parameters"] == ["latency_ms"]
    assert r[EM.SIMULATED_FILL]["parameters"]["latency_ms"]["provenance"] == EM.UNKNOWN
    assert r[EM.SIMULATED_FILL]["parameters"]["latency_ms"]["value"] is None


def test_with_no_parameters_at_all_every_mode_is_refused_and_lists_what_is_missing():
    sc = scenario(assumptions=EM.Assumptions())
    modes = run(sc)[EM.SIMULATED_FILL]["modes"]
    assert modes[EM.TAKER]["unknown_parameters"] == ["latency_ms", "vol_bps_per_sqrt_s"]
    assert modes[EM.HYBRID]["unknown_parameters"] == ["latency_ms", "vol_bps_per_sqrt_s", "maker_fee_bps_perp",
                                                      "adverse_selection_bps", "fill_probability"]
    assert modes[EM.MAKER]["unknown_parameters"][-3:] == ["nonfill_penalty_bps", "adverse_selection_bps", "fill_probability"]
    assert all(m["expected_cost_bps"] is None for m in modes.values())


def test_maker_alone_needs_a_stated_cost_for_a_missed_fill():
    modes = run(scenario(assumptions=assume(nonfill_penalty_bps=None)))[EM.SIMULATED_FILL]["modes"]
    assert modes[EM.MAKER]["status"] == EM.REFUSED and modes[EM.MAKER]["unknown_parameters"] == ["nonfill_penalty_bps"]
    assert modes[EM.HYBRID]["status"] == EM.COMPUTED and modes[EM.TAKER]["status"] == EM.COMPUTED


def test_assumed_values_are_echoed_back_with_their_rationale_and_declared_measured_claims_are_flagged():
    sc = scenario(params={"adverse_selection_bps": EM.measured(0.4, "my own markout study")},
                  assumptions=assume(adverse_selection_bps=None))
    r = run(sc)
    sim = r[EM.SIMULATED_FILL]
    assert sim["assumptions_echo"]["fill_probability"] == {"value": 0.6, "provenance": EM.ASSUMED,
                                                           "rationale": "test fixture for fill_probability"}
    assert sim["parameters"]["fill_probability"]["provenance"] == EM.ASSUMED
    assert sim["parameters"]["adverse_selection_bps"]["declared_provenance"] == EM.MEASURED
    assert sim["parameters"]["adverse_selection_bps"]["verified_by_account_records"] is False
    assert sim["declared_measured_but_unverified_by_account_records"] == ["adverse_selection_bps"]


def test_two_fill_models_at_once_are_refused():
    with pytest.raises(ValueError, match="not both"):
        run(scenario(assumptions=assume(queue_ahead_btc=1.0, cancel_fraction_ahead=0.0,
                                        through_volume_rate_btc_per_s=1.0, mean_trade_size_btc=0.5)))


def test_a_half_specified_queue_model_names_the_missing_pieces():
    sc = scenario(assumptions=assume(fill_probability=None, queue_ahead_btc=5.0))
    miss = run(sc)[EM.SIMULATED_FILL]["modes"][EM.HYBRID]["unknown_parameters"]
    assert miss == ["cancel_fraction_ahead", "through_volume_rate_btc_per_s", "mean_trade_size_btc"]


# ====================================================================================================
# the maker fill primitive: a touch is not a fill
# ====================================================================================================

def test_a_touch_of_the_price_with_no_trade_volume_through_it_gives_zero_fill():
    touches = [EM.TradeEvent.touch(t, 100.0) for t in (10, 20, 30, 40)]
    r = EM.simulate_maker_fill(0.0, 1.0, touches, 0.0, side="buy", price=100.0)
    assert r.status == EM.NOT_FILLED and r.filled_qty == 0.0 and r.fills == ()
    assert r.reason == "TOUCH_WITHOUT_VOLUME" and r.touches_ignored == 4 and r.volume_through == 0.0
    assert r.evidence_class == EM.SIMULATED_FILL and r.as_dict()["is_measured"] is False


def test_a_touch_event_carrying_a_quantity_still_fills_nothing():
    ev = EM.TradeEvent(5.0, qty=999.0, price=100.0, aggressor="SELL", kind=EM.QUOTE_TOUCH)
    r = EM.simulate_maker_fill(0.0, 1.0, [ev], 0.0, side="buy", price=100.0)
    assert r.filled_qty == 0.0 and r.touches_ignored == 1


def test_no_events_at_all_is_a_non_fill_with_its_own_reason():
    r = EM.simulate_maker_fill(3.0, 1.0, [], 0.0)
    assert r.status == EM.NOT_FILLED and r.reason == "NO_TRADE_VOLUME"


def test_volume_exactly_equal_to_the_queue_ahead_fills_nothing_and_one_more_unit_fills_one():
    eq = EM.simulate_maker_fill(10.0, 4.0, [(1.0, 10.0)], 0.0)
    assert eq.filled_qty == 0.0 and eq.status == EM.NOT_FILLED and eq.reason == "QUEUE_NOT_CLEARED" and not eq.queue_cleared
    plus = EM.simulate_maker_fill(10.0, 4.0, [(1.0, 10.0), (2.0, 1.0)], 0.0)
    assert plus.filled_qty == 1.0 and plus.status == EM.PARTIAL and plus.queue_cleared
    full = EM.simulate_maker_fill(10.0, 4.0, [(1.0, 14.0)], 0.0)
    assert full.filled_qty == 4.0 and full.status == EM.FILLED and full.complete_ms == 1.0


def test_partial_fills_sum_correctly_across_events_and_never_exceed_the_order():
    r = EM.simulate_maker_fill(5.0, 4.0, [(1.0, 4.0), (2.0, 3.0), (3.0, 2.0), (4.0, 50.0)], 0.0)
    assert r.fills == ((2.0, 2.0), (3.0, 2.0)) and sum(q for _, q in r.fills) == r.filled_qty == 4.0
    assert r.status == EM.FILLED and r.first_fill_ms == 2.0 and r.complete_ms == 3.0
    assert r.volume_through == 9.0                                  # stopped counting when the order completed
    part = EM.simulate_maker_fill(5.0, 4.0, [(1.0, 4.0), (2.0, 3.0)], 0.0)
    assert part.status == EM.PARTIAL and part.filled_qty == 2.0 and part.fill_fraction == 0.5
    assert part.complete_ms is None


def test_fill_is_exactly_the_clamped_excess_of_cumulative_volume_over_the_queue():
    rng = random.Random(3)
    for _ in range(300):
        q, size, c = rng.uniform(0, 20), rng.uniform(0.1, 6), rng.choice([0.0, 0.25, 0.5, 1.0])
        vols = [rng.uniform(0, 4) for _ in range(rng.randint(0, 8))]
        r = EM.simulate_maker_fill(q, size, [(float(i), v) for i, v in enumerate(vols)], c)
        assert r.filled_qty == pytest.approx(min(size, max(0.0, sum(vols) - q * (1 - c))), abs=1e-9)
        assert 0.0 <= r.filled_qty <= size


def test_more_volume_never_means_less_fill():
    prev = -1.0
    for total in [0.0, 1.0, 3.0, 5.0, 5.0, 7.5, 12.0, 100.0]:
        f = EM.simulate_maker_fill(5.0, 4.0, [(1.0, total)], 0.0).filled_qty
        assert f >= prev
        prev = f


def test_cancellations_ahead_are_an_explicit_assumption_that_shortens_the_queue():
    half = EM.simulate_maker_fill(10.0, 4.0, [(1.0, 5.0)], 0.5)
    assert half.effective_queue_ahead == 5.0 and half.filled_qty == 0.0         # still exactly equal: no fill
    assert EM.simulate_maker_fill(10.0, 4.0, [(1.0, 6.0)], 0.5).filled_qty == 1.0
    assert EM.simulate_maker_fill(10.0, 4.0, [(1.0, 1.0)], 1.0).filled_qty == 1.0   # nothing left ahead
    assert half.assumed_inputs == ("cancel_fraction_ahead",) and half.provenance["cancel_fraction_ahead"] == "UNLABELLED_CALLER_VALUE"
    assumed = EM.simulate_maker_fill(10.0, 4.0, [(1.0, 6.0)], EM.Assumption(0.5, "no data; midpoint"))
    assert assumed.provenance["cancel_fraction_ahead"] == EM.ASSUMED


def test_an_unknown_cancel_rate_or_queue_refuses_instead_of_being_defaulted():
    with pytest.raises(EM.UnknownParameterError) as e:
        EM.simulate_maker_fill(10.0, 4.0, [(1.0, 20.0)], EM.UNKNOWN_PARAM)
    assert e.value.unknown == ("cancel_fraction_ahead",)
    with pytest.raises(EM.UnknownParameterError):
        EM.simulate_maker_fill(EM.UNKNOWN_PARAM, 4.0, [(1.0, 20.0)], 0.0)
    with pytest.raises(TypeError):
        EM.simulate_maker_fill(10.0, 4.0, [(1.0, 20.0)])                      # no default for the assumed quantity


def test_only_prints_at_or_through_the_price_on_the_right_side_count():
    ev = [EM.TradeEvent(1, 5.0, 100.1, "SELL"),        # above a bid: never reached it
          EM.TradeEvent(2, 5.0, 100.0, "BUY"),         # a buyer lifting an offer is not hitting our bid
          EM.TradeEvent(3, 5.0, None, "SELL"),         # no price: cannot prove it traded at/through
          EM.TradeEvent(4, 2.0, 100.0, "SELL")]        # at the price: ordinary volume
    r = EM.simulate_maker_fill(1.0, 5.0, ev, 0.0, side="buy", price=100.0)
    assert r.events_ignored == 3 and r.volume_through == 2.0 and r.filled_qty == 1.0
    sell = EM.simulate_maker_fill(0.0, 5.0, [EM.TradeEvent(1, 2.0, 99.9, "BUY"), EM.TradeEvent(2, 2.0, 100.0, "BUY")], 0.0,
                                  side="sell", price=100.0)
    assert sell.events_ignored == 1 and sell.filled_qty == 2.0


def test_a_print_strictly_through_the_price_means_the_queue_and_the_order_were_consumed():
    r = EM.simulate_maker_fill(50.0, 4.0, [EM.TradeEvent(1, 0.01, 99.9, "SELL")], 0.0, side="buy", price=100.0)
    assert r.filled_qty == 4.0 and r.status == EM.FILLED
    assert EM.simulate_maker_fill(50.0, 4.0, [EM.TradeEvent(1, 0.01, 100.0, "SELL")], 0.0, side="buy", price=100.0).filled_qty == 0.0


def test_events_after_the_timeout_are_ignored_and_malformed_streams_are_refused():
    r = EM.simulate_maker_fill(0.0, 1.0, [(10.0, 0.4), (6000.0, 5.0)], 0.0, horizon_ms=5000.0)
    assert r.filled_qty == 0.4 and r.events_after_horizon == 1
    with pytest.raises(ValueError, match="ordered"):
        EM.simulate_maker_fill(0.0, 1.0, [(10.0, 1.0), (5.0, 1.0)], 0.0)
    with pytest.raises(ValueError):
        EM.simulate_maker_fill(0.0, 1.0, [(1.0, -1.0)], 0.0)
    with pytest.raises(ValueError):
        EM.simulate_maker_fill(0.0, 0.0, [], 0.0)
    with pytest.raises(ValueError, match="together"):
        EM.simulate_maker_fill(0.0, 1.0, [], 0.0, side="buy")


def test_a_post_only_order_that_would_cross_is_rejected_not_filled():
    assert EM.check_post_only("buy", 100.2, 100.0, 100.2).status == EM.REJECTED_POST_ONLY      # at the ask
    assert EM.check_post_only("buy", 100.5, 100.0, 100.2).reason == "WOULD_CROSS_THE_BOOK"
    assert not EM.check_post_only("sell", 100.0, 100.0, 100.2).accepted                      # at the bid
    assert EM.check_post_only("buy", 100.0, 100.0, 100.2).accepted
    assert EM.check_post_only("sell", 100.2, 100.0, 100.2).accepted
    # a tape that would have filled it is irrelevant once it is rejected
    tape = [EM.TradeEvent(1, 100.0, 99.0, "SELL")]
    r = EM.submit_post_only("buy", 100.2, 1.0, 100.0, 100.2, 0.0, tape, 0.0)
    assert r.status == EM.REJECTED_POST_ONLY and r.filled_qty == 0.0 and r.fills == () and r.volume_through == 0.0
    ok = EM.submit_post_only("buy", 100.0, 1.0, 100.0, 100.2, 0.0, tape, 0.0)
    assert ok.status == EM.FILLED
    with pytest.raises(ValueError, match="crossed"):
        EM.check_post_only("buy", 100.0, 100.2, 100.0)


def test_post_only_rejection_probability_is_a_bounded_monotone_function_of_spread_vol_and_latency():
    f = EM.post_only_reject_probability
    assert f(2.0, 1.0, 0.0) == 0.0 and f(2.0, 0.0, 100.0) == 0.0
    assert f(2.0, 1.0, 100.0) == pytest.approx(NormalDist().cdf(-2.0 / (1.0 * math.sqrt(0.1))))
    lats = [f(1.0, 1.0, L) for L in (1, 10, 100, 1000, 10000)]
    assert lats == sorted(lats) and all(0.0 <= x < 0.5 for x in lats)
    assert f(0.01, 5.0, 10000.0) < 0.5 and f(5.0, 1.0, 100.0) < f(0.5, 1.0, 100.0)
    with pytest.raises(ValueError):
        f(0.0, 1.0, 10.0)


# ====================================================================================================
# legging
# ====================================================================================================

def test_legging_probability_is_bounded_symmetric_and_has_the_binomial_form():
    for pa, pb in [(0, 0), (1, 1), (0.3, 0.3), (0.2, 0.9), (1, 0), (0.5, 0.5)]:
        p = EM.legging_probability(pa, pb)
        assert 0.0 <= p <= 1.0 and p == EM.legging_probability(pb, pa)
        assert p == pytest.approx(pa * (1 - pb) + pb * (1 - pa))
    assert EM.legging_probability(0, 0) == 0.0 == EM.legging_probability(1, 1)
    assert EM.legging_probability(1, 0) == 1.0
    assert EM.legging_probability(0.5, 0.5) == 0.5
    with pytest.raises(ValueError):
        EM.legging_probability(1.2, 0.5)


def test_expected_legging_move_follows_the_volatility_and_the_square_root_of_the_window():
    a = EM.legging_risk(0.7, 0.4, 1000.0, 2.0)
    assert a["expected_adverse_move_bps_if_legged"] == pytest.approx(PHI0 * 2.0)
    assert a["expected_cost_bps"] == pytest.approx(EM.legging_probability(0.7, 0.4) * PHI0 * 2.0)
    b = EM.legging_risk(0.7, 0.4, 4000.0, 2.0)
    assert b["expected_adverse_move_bps_if_legged"] == pytest.approx(2.0 * a["expected_adverse_move_bps_if_legged"])
    assert EM.legging_risk(0.7, 0.4, 1000.0, 0.0)["expected_cost_bps"] == 0.0
    assert a["evidence_class"] == EM.SIMULATED_FILL and a["is_measured"] is False


def test_an_overlay_has_one_hedge_leg_per_phase_so_nothing_can_leg():
    modes = expected(scenario(EM.OVERLAY))
    assert all(m["p_one_leg_failure"] == 0.0 and m["components_bps"]["legging"] == 0.0 for m in modes.values())


def test_taker_one_leg_failure_probability_is_the_binomial_of_the_stated_failure_rate():
    f = 0.03
    t = expected(scenario(EM.ACQUIRE, taker_leg_fail_probability=f))[EM.TAKER]
    per_phase = 2 * f * (1 - f)
    assert t["p_one_leg_failure"] == pytest.approx(1 - (1 - per_phase) ** 2)
    assert t["components_bps"]["legging"] == pytest.approx(2 * per_phase * D(0.9, 100.0))
    zero = expected(scenario(EM.ACQUIRE, taker_leg_fail_probability=0.0))[EM.TAKER]
    assert zero["p_one_leg_failure"] == 0.0 and zero["components_bps"]["legging"] == 0.0


def test_hybrid_legging_vanishes_at_the_degenerate_fill_probabilities_and_is_a_probability_between():
    probs = {}
    for p in (0.0, 0.25, 0.5, 0.75, 1.0):
        h = expected(scenario(EM.ACQUIRE, latency_ms=0.0, fill_probability=p))[EM.HYBRID]
        probs[p] = h["p_one_leg_failure"]
        assert 0.0 <= probs[p] <= 1.0
    assert probs[0.0] == 0.0 and probs[1.0] == 0.0                 # both fill together / both chase together
    per_phase = 2 * 0.5 * 0.5
    assert probs[0.5] == pytest.approx(1 - (1 - per_phase) ** 2)
    assert probs[0.25] == pytest.approx(probs[0.75]) and probs[0.5] > probs[0.25] > 0.0


def test_the_sampled_one_leg_failure_rate_agrees_with_the_enumerated_one():
    r = run(scenario(EM.ACQUIRE, latency_ms=0.0, fill_probability=0.5), n=4000)
    for pol in (EM.HYBRID, EM.MAKER):
        m = r[EM.SIMULATED_FILL]["modes"][pol]
        se = math.sqrt(m["p_one_leg_failure"] * (1 - m["p_one_leg_failure"]) / 4000)
        assert abs(m["p_one_leg_failure_monte_carlo"] - m["p_one_leg_failure"]) < 4 * se + 1e-9


# ====================================================================================================
# taker reconciliation
# ====================================================================================================

@pytest.mark.parametrize("mode,depth,notional,ref", [(EM.OVERLAY, COARSE, 201.0, 100.5), (EM.ACQUIRE, COARSE, 201.0, 100.5),
                                                      (EM.OVERLAY, TIGHT, NOTIONAL, REF), (EM.ACQUIRE, TIGHT, NOTIONAL, REF)])
def test_taker_cost_reconciles_with_md_economics_depth_costs_on_the_same_book(mode, depth, notional, ref):
    sigma, lat = 0.9, 80.0
    sc = scenario(mode, depth, notional, ref, taker_leg_fail_probability=0.0, latency_ms=lat, vol_bps_per_sqrt_s=sigma)
    r = run(sc)
    allowance = 2 * D(sigma, lat) * (2 if mode == EM.ACQUIRE else 1)
    dc = ME.depth_costs(depth, mode=mode, notional_usd=notional, ref_price=ref, latency_allowance_bps=allowance)
    assert dc.status == ME.MEASURED
    fees = (2 * FEES.perp_taker_bps) + (2 * FEES.spot_taker_bps if mode == EM.ACQUIRE else 0.0)
    t = r[EM.SIMULATED_FILL]["modes"][EM.TAKER]
    assert t["expected_cost_bps"] == pytest.approx(dc.spread_bps + dc.slippage_bps + dc.latency_allowance_bps + fees)
    c = t["components_bps"]
    assert c["spread"] == pytest.approx(dc.spread_bps) and c["slippage"] == pytest.approx(dc.slippage_bps)
    assert c["fees"] == pytest.approx(fees) and c["impact"] == 0.0
    pub = r[EM.PUBLIC_DISPLAYED_DEPTH]
    assert pub["spread_bps_round_trip"] == pytest.approx(dc.spread_bps) and pub["slippage_bps_round_trip"] == pytest.approx(dc.slippage_bps)
    assert t["expected_cost_bps"] == pytest.approx(hand_taker(mode, depth, notional, ref, sigma, lat))
    assert t["components_reconcile"] is True


def test_the_coarse_book_really_exercises_slippage_so_the_reconciliation_is_not_trivial():
    r = run(scenario(EM.OVERLAY, COARSE, 201.0, 100.5))
    assert r[EM.PUBLIC_DISPLAYED_DEPTH]["slippage_bps_round_trip"] > 10.0
    assert r[EM.PUBLIC_DISPLAYED_DEPTH]["legs"][0]["levels_used"] == 2


def test_taker_components_reproduce_the_repo_trade_economics_ledger():
    r = run(scenario(EM.OVERLAY, COARSE, 201.0, 100.5))
    costs = EM.trade_economics_costs_for_taker(r)
    pos = TE.Position(notional_usd=201.0, hold_days=30.0, btc_inventory_usd=201.0, btc_was_owned=True,
                      margin_posted_usd=0.0, collateral_haircut=0.1, reserve_usd=0.0,
                      maintenance_margin_rate=0.005, initial_margin_rate=0.01)
    ledger = TE.compute(pos, costs, [0.0] * 90)
    assert ledger["cost_bps_of_notional_round_trip"] == pytest.approx(r[EM.SIMULATED_FILL]["modes"][EM.TAKER]["expected_cost_bps"])
    assert costs.maker_taker == "taker" and costs.fee_provenance == "UNVERIFIED"
    acq = run(scenario(EM.ACQUIRE, COARSE, 201.0, 100.5, taker_leg_fail_probability=0.0))
    led2 = TE.compute(pos, EM.trade_economics_costs_for_taker(acq), [0.0] * 90)
    assert led2["cost_bps_of_notional_round_trip"] == pytest.approx(acq[EM.SIMULATED_FILL]["modes"][EM.TAKER]["expected_cost_bps"])
    refused = run(scenario(assumptions=EM.Assumptions()))
    with pytest.raises(ValueError, match="refused"):
        EM.trade_economics_costs_for_taker(refused)


def test_quantity_beyond_displayed_depth_is_never_extrapolated_without_an_explicit_impact_parameter():
    big = scenario(EM.OVERLAY, COARSE, 1000.0, 100.5)                       # 9.95 BTC against 8 BTC displayed
    r = run(big)
    assert r[EM.PUBLIC_DISPLAYED_DEPTH]["status"] == "UNAVAILABLE"
    assert "perp_sell_entry:DEPTH_INSUFFICIENT" in r[EM.PUBLIC_DISPLAYED_DEPTH]["reasons"]
    assert r[EM.SIMULATED_FILL]["modes"][EM.TAKER]["unknown_parameters"] == ["beyond_depth_impact_bps"]
    costs = []
    for imp in (0.0, 5.0, 20.0):
        s = scenario(EM.OVERLAY, COARSE, 1000.0, 100.5, beyond_depth_impact_bps=imp)
        t = run(s)[EM.SIMULATED_FILL]["modes"][EM.TAKER]
        assert t["status"] == EM.COMPUTED and t["components_bps"]["impact"] > 0.0
        costs.append(t["expected_cost_bps"])
    assert costs == sorted(costs) and costs[0] < costs[1] < costs[2]
    # the displayed part is exactly the book walk; the rest is labelled impact
    t0 = run(scenario(EM.OVERLAY, COARSE, 1000.0, 100.5, beyond_depth_impact_bps=0.0))[EM.SIMULATED_FILL]["modes"][EM.TAKER]
    qty = 1000.0 / 100.5
    disp = 8.0
    sell_touch, sell_last = 100.0, 98.0
    avg_disp = (1 * 100 + 2 * 99 + 5 * 98) / 8.0
    expect_slip = (disp / qty) * abs(avg_disp - sell_touch) / sell_touch * 1e4
    expect_imp = (1 - disp / qty) * abs(sell_last - sell_touch) / sell_touch * 1e4
    buy_touch, buy_last = 101.0, 103.0
    avg_b = (1 * 101 + 2 * 102 + 5 * 103) / 8.0
    expect_slip += (disp / qty) * abs(avg_b - buy_touch) / buy_touch * 1e4
    expect_imp += (1 - disp / qty) * abs(buy_last - buy_touch) / buy_touch * 1e4
    assert t0["components_bps"]["slippage"] == pytest.approx(expect_slip)
    assert t0["components_bps"]["impact"] == pytest.approx(expect_imp)


def test_an_impact_parameter_is_ignored_when_the_book_is_deep_enough():
    a = expected(scenario())[EM.TAKER]["expected_cost_bps"]
    b = expected(scenario(beyond_depth_impact_bps=500.0))[EM.TAKER]["expected_cost_bps"]
    assert a == b


@pytest.mark.parametrize("bad", [{"perp_bids": (), "perp_asks": COARSE_A},
                                 {"perp_bids": ((101.0, 1.0),), "perp_asks": ((100.0, 1.0),)},
                                 {"perp_bids": ((100.0, float("nan")),), "perp_asks": COARSE_A}])
def test_an_unusable_book_refuses_every_mode_and_says_why(bad):
    r = run(scenario(EM.OVERLAY, bad, 201.0, 100.5))
    for m in EM.MODES:
        mm = r[EM.SIMULATED_FILL]["modes"][m]
        assert mm["status"] == EM.REFUSED and mm["reason"] == "BOOK_UNUSABLE" and mm["expected_cost_bps"] is None
    assert r[EM.PUBLIC_DISPLAYED_DEPTH]["status"] == "UNAVAILABLE"


# ====================================================================================================
# hybrid / maker analytics
# ====================================================================================================

def maker_fill_cost(mode, depth, notional, ref, a, fee_perp, fee_spot):
    total = 0.0
    for (inst, _), w in walks(mode, depth, notional, ref).items():
        total += -w.half_spread_bps + (fee_perp if inst == "perp" else fee_spot) + a
    return total


@pytest.mark.parametrize("mode", [EM.OVERLAY, EM.ACQUIRE])
def test_hybrid_with_certain_fills_and_no_latency_costs_exactly_the_maker_fill(mode):
    r = expected(scenario(mode, fill_probability=1.0, latency_ms=0.0, adverse_selection_bps=1.3))
    want = maker_fill_cost(mode, TIGHT, NOTIONAL, REF, 1.3, 2.0, 10.0)
    assert r[EM.HYBRID]["expected_cost_bps"] == pytest.approx(want)
    assert r[EM.HYBRID]["components_bps"]["delay"] == 0.0 and r[EM.HYBRID]["components_bps"]["legging"] == 0.0
    assert r[EM.HYBRID]["p_one_leg_failure"] == 0.0


@pytest.mark.parametrize("mode", [EM.OVERLAY, EM.ACQUIRE])
def test_hybrid_with_no_fills_and_no_latency_is_taker_cost_plus_the_delay_cost(mode):
    T = 5000.0
    r = expected(scenario(mode, fill_probability=0.0, latency_ms=0.0, taker_leg_fail_probability=0.0, timeout=T))
    legs = len(walks(mode, TIGHT, NOTIONAL, REF))
    taker = r[EM.TAKER]["expected_cost_bps"]
    assert taker == pytest.approx(hand_taker(mode, TIGHT, NOTIONAL, REF, 0.9, 0.0))
    delay = legs * D(0.9, T)
    assert r[EM.HYBRID]["expected_cost_bps"] == pytest.approx(taker + delay)
    assert r[EM.HYBRID]["expected_cost_bps"] > taker
    assert r[EM.HYBRID]["components_bps"]["adverse_selection"] == 0.0 and r[EM.HYBRID]["p_one_leg_failure"] == 0.0


def test_hybrid_cost_between_the_degenerate_bounds_for_every_fill_probability_in_an_overlay():
    kw = dict(latency_ms=0.0, adverse_selection_bps=1.0)
    lo = expected(scenario(fill_probability=1.0, **kw))[EM.HYBRID]["expected_cost_bps"]
    hi = expected(scenario(fill_probability=0.0, **kw))[EM.HYBRID]["expected_cost_bps"]
    assert lo < hi
    for p in (0.1, 0.33, 0.5, 0.9):
        c = expected(scenario(fill_probability=p, **kw))[EM.HYBRID]["expected_cost_bps"]
        assert lo < c < hi
        assert c == pytest.approx(p * lo + (1 - p) * hi)                 # linear in p for a single hedge leg


@pytest.mark.parametrize("p,lat", [(0.0, 50.0), (0.4, 50.0), (0.8, 300.0), (1.0, 300.0)])
def test_overlay_hybrid_matches_the_hand_computed_expectation_including_post_only_rejection(p, lat):
    r = expected(scenario(fill_probability=p, latency_ms=lat, adverse_selection_bps=0.7))
    want = hand_overlay_hybrid(p, 0.7, lat, 5000.0, 0.9, 2.0)
    assert r[EM.HYBRID]["expected_cost_bps"] == pytest.approx(want)


def test_more_adverse_selection_never_makes_maker_or_hybrid_cheaper():
    for pol in (EM.HYBRID, EM.MAKER):
        for mode in (EM.OVERLAY, EM.ACQUIRE):
            prev = None
            for a in (0.0, 0.1, 0.5, 1.0, 2.5, 6.0):
                c = expected(scenario(mode, adverse_selection_bps=a, fill_probability=0.6))[pol]["expected_cost_bps"]
                if prev is not None:
                    assert c > prev
                prev = c
            nofill = [expected(scenario(mode, adverse_selection_bps=a, fill_probability=0.0))[pol]["expected_cost_bps"]
                      for a in (0.0, 5.0)]
            assert nofill[0] == pytest.approx(nofill[1])                 # no fills, nothing to be adversely selected
    # and the taker, which is never filled passively, does not move at all
    t = [expected(scenario(adverse_selection_bps=a))[EM.TAKER]["expected_cost_bps"] for a in (0.0, 9.0)]
    assert t[0] == t[1]


def test_a_higher_fill_probability_is_cheaper_when_a_maker_fill_beats_the_chase():
    cs = [expected(scenario(fill_probability=p))[EM.HYBRID]["expected_cost_bps"] for p in (0.0, 0.2, 0.5, 0.8, 1.0)]
    assert cs == sorted(cs, reverse=True) and cs[0] > cs[-1]


def test_a_worse_maker_fee_never_helps_and_a_rebate_helps():
    base = expected(scenario())[EM.HYBRID]["expected_cost_bps"]
    worse = expected(scenario(maker_fee_bps_perp=4.0))[EM.HYBRID]["expected_cost_bps"]
    rebate = expected(scenario(maker_fee_bps_perp=-1.0))[EM.HYBRID]["expected_cost_bps"]
    assert rebate < base < worse


def test_pure_maker_charges_a_missed_fill_and_matches_the_hand_computation_in_an_overlay():
    p, pen, a = 0.35, 8.0, 1.0
    r = expected(scenario(fill_probability=p, latency_ms=0.0, nonfill_penalty_bps=pen, adverse_selection_bps=a))[EM.MAKER]
    want = 0.0
    for w in walks(EM.OVERLAY, TIGHT, NOTIONAL, REF).values():
        want += p * (-w.half_spread_bps + 2.0 + a) + (1 - p) * pen
    assert r["expected_cost_bps"] == pytest.approx(want)
    assert r["p_nonfill"] == pytest.approx(1 - p ** 2) and r["components_bps"]["penalty"] == pytest.approx(2 * (1 - p) * pen)
    lazy = expected(scenario(fill_probability=p, latency_ms=0.0, nonfill_penalty_bps=0.0, adverse_selection_bps=a))[EM.MAKER]
    assert lazy["expected_cost_bps"] < r["expected_cost_bps"]            # ignoring the miss flatters maker


def hand_acquire(policy, p, a, lat, T, sigma, pen, depth=TIGHT):
    """Independent enumeration of every (rejected / filled / unfilled) outcome of the two hedge legs."""
    w = walks(EM.ACQUIRE, depth, NOTIONAL, REF)
    fee_t = {"perp": FEES.perp_taker_bps, "spot": FEES.spot_taker_bps}
    fee_m = {"perp": 2.0, "spot": 10.0}

    def tk(i, ph):
        x = w[(i, ph)]
        return x.half_spread_bps + x.slippage_bps + fee_t[i]

    def mk(i, ph):
        return -w[(i, ph)].half_spread_bps + fee_m[i] + a

    sd = sigma * math.sqrt(lat / 1000.0)
    q = 0.0 if sd == 0 else NormalDist().cdf(-2 * w[("perp", "entry")].half_spread_bps / sd)
    classes = [("R", q), ("F", (1 - q) * p), ("N", (1 - q) * (1 - p))]
    total = legging = 0.0
    for ph, other in (("entry", "exit"), ("exit", "entry")):
        for cp, wp in classes:
            for cs, ws in classes:
                prob = wp * ws
                if prob == 0:
                    continue
                cls = {"perp": cp, "spot": cs}
                tr = {i: (3 * lat if cls[i] == "R" else 2 * lat + T) for i in cls}
                done = {i: 1.0 if cls[i] == "F" else 0.0 for i in cls}
                cost = leg = 0.0
                if policy == EM.HYBRID:
                    for i in cls:
                        cost += mk(i, ph) if cls[i] == "F" else tk(i, ph) + D(sigma, tr[i])
                    t_done = {i: (lat if cls[i] == "F" else tr[i]) for i in cls}
                    leg = D(sigma, abs(t_done["perp"] - t_done["spot"]))
                else:
                    e = min(done.values())
                    for i in cls:
                        cost += mk(i, ph) if done[i] else 0.0
                        cost += tk(i, other) if done[i] - e > 0 else 0.0
                    cost += (1 - e) * pen
                    if done["perp"] != done["spot"]:
                        deficient = "perp" if done["perp"] < done["spot"] else "spot"
                        leg = D(sigma, tr[deficient] - lat)
                total += prob * (cost + leg)
                legging += prob * leg
    return total, legging


@pytest.mark.parametrize("policy", [EM.HYBRID, EM.MAKER])
@pytest.mark.parametrize("depth", [TIGHT, ASYM], ids=["symmetric-book", "asymmetric-book"])
@pytest.mark.parametrize("lat,p", [(0.0, 0.5), (50.0, 0.5), (50.0, 0.2), (300.0, 0.9)])
def test_acquire_policies_match_an_independent_enumeration_including_legging_windows(policy, depth, lat, p):
    pen, a, T = 4.0, 0.8, 4000.0
    r = expected(scenario(EM.ACQUIRE, depth, fill_probability=p, latency_ms=lat, nonfill_penalty_bps=pen,
                          adverse_selection_bps=a, timeout=T))[policy]
    total, legging = hand_acquire(policy, p, a, lat, T, 0.9, pen, depth)
    assert r["expected_cost_bps"] == pytest.approx(total, rel=1e-9)
    assert r["components_bps"]["legging"] == pytest.approx(legging, rel=1e-9) and legging > 0.0
    assert r["components_reconcile"] is True
    if policy == EM.MAKER:
        assert r["components_bps"]["unwind"] > 0.0 and r["p_nonfill"] > 0.0


def test_pure_maker_one_leg_failure_probability_is_the_binomial_when_nothing_is_rejected():
    p = 0.5
    r = expected(scenario(EM.ACQUIRE, fill_probability=p, latency_ms=0.0))[EM.MAKER]
    assert r["p_one_leg_failure"] == pytest.approx(1 - (1 - 2 * p * (1 - p)) ** 2)
    assert r["p_nonfill"] == pytest.approx(1 - p ** 4)                       # a phase is established only if both legs fill


# ====================================================================================================
# break-even and sensitivity
# ====================================================================================================

def test_min_fill_probability_matches_the_closed_form_for_an_overlay_and_ties_taker_there():
    for lat in (0.0, 80.0):
        sc = scenario(latency_ms=lat)
        a = 0.9
        f0 = hand_overlay_hybrid(0.0, a, lat, 5000.0, 0.9, 2.0)
        f1 = hand_overlay_hybrid(1.0, a, lat, 5000.0, 0.9, 2.0)
        taker = hand_taker(EM.OVERLAY, TIGHT, NOTIONAL, REF, 0.9, lat)
        want = (taker - f0) / (f1 - f0)
        got = EM.min_fill_probability_to_beat_taker(sc, EM.HYBRID, a)
        assert got["status"] == EM.COMPUTED and got["min_fill_probability"] == pytest.approx(want, abs=1e-9)
        assert got["taker_cost_bps"] == pytest.approx(taker)
        at = hand_overlay_hybrid(got["min_fill_probability"], a, lat, 5000.0, 0.9, 2.0)
        assert at == pytest.approx(taker, abs=1e-9)
        just_below = hand_overlay_hybrid(got["min_fill_probability"] - 0.01, a, lat, 5000.0, 0.9, 2.0)
        assert just_below > taker
        assert got["forecast"] is False and got["is_measured"] is False


def test_max_adverse_selection_matches_the_closed_form_and_hybrid_ties_taker_exactly_there():
    sc = scenario(latency_ms=50.0)
    p = 0.7
    c0 = hand_overlay_hybrid(p, 0.0, 50.0, 5000.0, 0.9, 2.0)
    c1 = hand_overlay_hybrid(p, 1.0, 50.0, 5000.0, 0.9, 2.0)
    taker = hand_taker(EM.OVERLAY, TIGHT, NOTIONAL, REF, 0.9, 50.0)
    want = (taker - c0) / (c1 - c0)
    got = EM.max_adverse_selection_to_beat_taker(sc, EM.HYBRID, p)
    assert got["max_adverse_selection_bps"] == pytest.approx(want)
    assert hand_overlay_hybrid(p, want, 50.0, 5000.0, 0.9, 2.0) == pytest.approx(taker)
    assert hand_overlay_hybrid(p, want + 0.05, 50.0, 5000.0, 0.9, 2.0) > taker
    # the two break-evens describe the same boundary
    back = EM.min_fill_probability_to_beat_taker(sc, EM.HYBRID, want)
    assert back["min_fill_probability"] == pytest.approx(p, abs=1e-6)


def test_break_even_does_not_need_the_unknowns_it_solves_for():
    sc = scenario(assumptions=assume(fill_probability=None, adverse_selection_bps=None))
    r = run(sc)
    assert r[EM.SIMULATED_FILL]["modes"][EM.HYBRID]["status"] == EM.REFUSED
    be = r[EM.SIMULATED_FILL]["breakeven"]
    assert be[EM.HYBRID]["status"] == EM.COMPUTED and be["forecast"] is False and be["kind"] == "WHAT_WOULD_HAVE_TO_BE_TRUE"
    assert be[EM.MAKER]["status"] == EM.COMPUTED
    gone = run(scenario(assumptions=assume(fill_probability=None, adverse_selection_bps=None, latency_ms=None)))
    assert gone[EM.SIMULATED_FILL]["breakeven"][EM.HYBRID]["status"] == EM.REFUSED
    assert gone[EM.SIMULATED_FILL]["breakeven"][EM.HYBRID]["unknown_parameters"] == ["latency_ms"]


def test_when_maker_fees_exceed_taker_fees_no_fill_probability_or_adverse_selection_makes_it_win():
    sc = scenario(maker_fee_bps_perp=30.0)
    a = EM.min_fill_probability_to_beat_taker(sc, EM.HYBRID, 0.0)
    assert a["min_fill_probability"] is None and a["reason"] == "NEVER_CHEAPER_THAN_TAKER_EVEN_AT_CERTAIN_FILL"
    b = EM.max_adverse_selection_to_beat_taker(sc, EM.HYBRID, 1.0)
    assert b["max_adverse_selection_bps"] is None and b["reason"].startswith("NEVER_CHEAPER")


def test_break_even_adverse_selection_is_reported_as_none_when_no_fill_can_be_adversely_selected():
    g = EM.max_adverse_selection_to_beat_taker(scenario(), EM.HYBRID, 0.0)
    assert g["max_adverse_selection_bps"] is None


def test_the_sensitivity_grid_is_monotone_labelled_and_consistent_with_the_cell_signs():
    be = run(scenario())[EM.SIMULATED_FILL]["breakeven"][EM.HYBRID]
    grid = be["sensitivity_grid"]
    cells = grid["cost_minus_taker_bps"]
    assert len(cells) == len(grid["fill_probability_axis"]) and len(cells[0]) == len(grid["adverse_selection_axis_bps"])
    for row in cells:
        assert row == sorted(row)                                                # worse with more adverse selection
    for j, a in enumerate(grid["adverse_selection_axis_bps"]):
        col = [row[j] for row in cells]
        if a <= 3.0:
            assert col == sorted(col, reverse=True)                              # better with more fills ...
        else:
            assert col == sorted(col)                                            # ... unless a fill itself costs more than the chase
    for row, flags in zip(cells, grid["beats_taker"]):
        assert flags == [c < 0.0 for c in row]
    assert cells[-1][0] < 0.0 < cells[0][-1]                                      # both outcomes occur: not a vacuous grid
    corner = hand_overlay_hybrid(1.0, 0.0, 50.0, 5000.0, 0.9, 2.0) - hand_taker(EM.OVERLAY, TIGHT, NOTIONAL, REF, 0.9, 50.0)
    assert cells[-1][0] == pytest.approx(corner)
    assert be["forecast"] is False
    maker = run(scenario())[EM.SIMULATED_FILL]["breakeven"][EM.MAKER]
    assert maker["status"] == EM.COMPUTED and len(maker["sensitivity_grid"]["cost_minus_taker_bps"]) == len(cells)


# ====================================================================================================
# Monte Carlo
# ====================================================================================================

def test_the_monte_carlo_is_seeded_and_reproducible_and_the_seed_matters():
    sc = scenario(EM.ACQUIRE)
    a, b = run(sc, 500, seed=7), run(sc, 500, seed=7)
    assert a == b
    c = run(sc, 500, seed=8)
    ha, hc = a[EM.SIMULATED_FILL]["modes"][EM.HYBRID], c[EM.SIMULATED_FILL]["modes"][EM.HYBRID]
    assert ha["monte_carlo"]["mean"] != hc["monte_carlo"]["mean"]
    assert ha["expected_cost_bps"] == hc["expected_cost_bps"]                    # the analytic expectation has no seed
    assert a[EM.SIMULATED_FILL]["seed"] == 7 and a[EM.SIMULATED_FILL]["n_paths"] == 500
    with pytest.raises(ValueError):
        run(sc, 500, seed=True)
    with pytest.raises(ValueError):
        run(sc, 10)
    with pytest.raises(ValueError):
        EM.compare_execution_modes(sc, seed=1.5)


@pytest.mark.parametrize("mode", [EM.OVERLAY, EM.ACQUIRE])
def test_sampled_means_agree_with_the_exact_expectation_within_sampling_error(mode):
    r = run(scenario(mode), 6000, seed=20261105)
    for pol in EM.MODES:
        m = r[EM.SIMULATED_FILL]["modes"][pol]
        mc = m["monte_carlo"]
        assert abs(mc["mean"] - m["expected_cost_bps"]) < 4.5 * mc["se_mean"] + 1e-6, pol
        assert mc["p05"] <= mc["mean"] <= mc["p95"] and mc["min"] <= mc["p05"] and mc["p95"] <= mc["max"]
        assert mc["std"] >= 0.0 and mc["n_paths"] == 6000
        assert abs(m["p_nonfill_monte_carlo"] - m["p_nonfill"]) < 0.04
    # hybrid always completes by chasing: it has no non-fill, maker does
    assert r[EM.SIMULATED_FILL]["modes"][EM.HYBRID]["p_nonfill"] == 0.0
    assert r[EM.SIMULATED_FILL]["modes"][EM.TAKER]["p_nonfill"] == 0.0
    assert r[EM.SIMULATED_FILL]["modes"][EM.MAKER]["p_nonfill"] > 0.0


def test_the_reported_quantiles_are_the_sample_quantiles_of_a_discrete_distribution_with_known_atoms():
    # No latency, no timeout: every leg either fills (maker cost) or is chased (taker cost) and nothing else
    # is random, so the round trip takes a few known values and the quantiles can be read off exactly.
    sc = scenario(fill_probability=0.96, latency_ms=0.0, timeout=0.0, adverse_selection_bps=1.0)
    r = run(sc, 6000, seed=99)[EM.SIMULATED_FILL]["modes"][EM.HYBRID]
    legs = list(walks(EM.OVERLAY, TIGHT, NOTIONAL, REF).values())
    c_mk = [-w.half_spread_bps + 2.0 + 1.0 for w in legs]
    c_tk = [w.half_spread_bps + w.slippage_bps + FEES.perp_taker_bps for w in legs]
    ff = sum(c_mk)
    mc = r["monte_carlo"]
    assert mc["min"] == pytest.approx(ff) and mc["p05"] == pytest.approx(ff)
    # P(at least one chase) = 1 - 0.96^2 = 7.8%: above the 5% tail, below the 10% one
    assert 0.05 < 1 - 0.96 ** 2 < 0.10
    one_chase = min(c_mk[0] + c_tk[1], c_tk[0] + c_mk[1])
    assert mc["p95"] >= one_chase - 1e-9 > ff + 2.0
    assert one_chase - 1e-9 <= mc["max"] <= sum(c_tk) + 1e-9


def test_the_distribution_reports_the_tails_not_just_the_mean():
    m = run(scenario(), 3000)[EM.SIMULATED_FILL]["modes"][EM.HYBRID]["monte_carlo"]
    assert m["p95"] - m["p05"] > 3.0                                              # fill vs chase outcomes differ by bps
    t = run(scenario(), 3000)[EM.SIMULATED_FILL]["modes"][EM.TAKER]["monte_carlo"]
    assert 0.0 < t["p95"] - t["p05"] < 1.0


def test_structural_queue_model_fills_everything_when_there_is_no_queue_and_plenty_of_volume():
    sc = scenario(assumptions=assume(fill_probability=None, queue_ahead_btc=0.0, cancel_fraction_ahead=0.0,
                                     through_volume_rate_btc_per_s=200.0, mean_trade_size_btc=1.0,
                                     post_only_reject_probability=0.0))
    h = run(sc, 400)[EM.SIMULATED_FILL]["modes"][EM.HYBRID]
    assert h["expected_cost_method"] == "MONTE_CARLO_MEAN" and h["fill_model"]["kind"] == "STRUCTURAL"
    assert h["p_full_fill_per_leg"] > 0.99 and h["mean_fill_fraction_per_leg"] > 0.99
    assert h["monte_carlo"]["mean"] == pytest.approx(maker_fill_cost(EM.OVERLAY, TIGHT, NOTIONAL, REF, 1.0, 2.0, 10.0), abs=0.05)


def test_structural_queue_model_never_fills_when_no_volume_prints_through_the_price():
    sc = scenario(assumptions=assume(fill_probability=None, queue_ahead_btc=0.0, cancel_fraction_ahead=0.0,
                                     through_volume_rate_btc_per_s=0.0, mean_trade_size_btc=1.0,
                                     post_only_reject_probability=0.0, latency_ms=0.0))
    r = run(sc, 300)[EM.SIMULATED_FILL]["modes"]
    assert r[EM.HYBRID]["mean_fill_fraction_per_leg"] == 0.0 and r[EM.HYBRID]["p_full_fill_per_leg"] == 0.0
    chase = expected(scenario(fill_probability=0.0, latency_ms=0.0))[EM.HYBRID]["expected_cost_bps"]
    assert r[EM.HYBRID]["monte_carlo"]["mean"] == pytest.approx(chase, abs=0.6)
    assert r[EM.MAKER]["p_nonfill"] == 1.0


def test_structural_queue_model_produces_partial_fills_and_a_longer_queue_fills_less():
    def frac(queue):
        sc = scenario(assumptions=assume(fill_probability=None, queue_ahead_btc=queue, cancel_fraction_ahead=0.0,
                                         through_volume_rate_btc_per_s=0.8, mean_trade_size_btc=0.4,
                                         post_only_reject_probability=0.0), timeout=4000.0)
        m = run(sc, 600, seed=5)[EM.SIMULATED_FILL]["modes"][EM.HYBRID]
        return m["mean_fill_fraction_per_leg"], m["p_full_fill_per_leg"]
    short, longer = frac(0.5), frac(6.0)
    assert 0.0 < short[0] < 1.0 and short[1] < short[0]                          # partial fills exist
    assert longer[0] < short[0]
    # cancellations ahead (an assumption) help
    sc = scenario(assumptions=assume(fill_probability=None, queue_ahead_btc=6.0, cancel_fraction_ahead=0.9,
                                     through_volume_rate_btc_per_s=0.8, mean_trade_size_btc=0.4,
                                     post_only_reject_probability=0.0), timeout=4000.0)
    helped = run(sc, 600, seed=5)[EM.SIMULATED_FILL]["modes"][EM.HYBRID]["mean_fill_fraction_per_leg"]
    assert helped > longer[0]


def test_a_supplied_post_only_rejection_probability_replaces_the_diffusion_estimate():
    model = expected(scenario(latency_ms=50.0))[EM.HYBRID]
    forced = expected(scenario(latency_ms=50.0, post_only_reject_probability=0.0))[EM.HYBRID]
    assert forced["expected_cost_bps"] < model["expected_cost_bps"]
    assert forced["post_only_reject_probability_by_leg"] == {"perp_sell_entry": 0.0, "perp_buy_exit": 0.0}
    assert all(0.3 < q < 0.5 for q in model["post_only_reject_probability_by_leg"].values())


# ====================================================================================================
# evidence classes
# ====================================================================================================

def walk_strings(obj, path=()):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from walk_strings(v, path + (str(k),))
    elif isinstance(obj, (list, tuple)):
        for i, v in enumerate(obj):
            yield from walk_strings(v, path + (str(i),))
    else:
        yield path, obj


def test_the_three_evidence_classes_are_separate_top_level_sections():
    r = run(scenario())
    for c in EM.EVIDENCE_CLASSES:
        assert r[c]["evidence_class"] == c
    assert set(EM.EVIDENCE_CLASSES) <= set(r)
    assert r[EM.PUBLIC_DISPLAYED_DEPTH].get("is_fill") is False
    assert r[EM.SIMULATED_FILL]["is_measured"] is False
    assert r[EM.ACCOUNT_EXECUTION].get("is_simulated") is False
    for pol in EM.MODES:
        assert r[EM.SIMULATED_FILL]["modes"][pol]["evidence_class"] == EM.SIMULATED_FILL
        assert r[EM.SIMULATED_FILL]["modes"][pol]["is_measured"] is False
    assert "modes" not in r[EM.ACCOUNT_EXECUTION] and "counts" not in r[EM.SIMULATED_FILL]


@pytest.mark.parametrize("declare", [False, True])
def test_nothing_simulated_or_displayed_is_ever_labelled_measured(declare):
    sc = scenario(EM.ACQUIRE)
    if declare:                         # a caller's own MEASURED claim is echoed as a claim, under `parameters` only
        sc = scenario(EM.ACQUIRE, params={"latency_ms": EM.measured(40.0, "my ping log")}, assumptions=assume(latency_ms=None))
    r = run(sc, account_records=None)
    if declare:
        assert r[EM.SIMULATED_FILL]["parameters"]["latency_ms"]["declared_provenance"] == EM.MEASURED
    for section in (EM.SIMULATED_FILL, EM.PUBLIC_DISPLAYED_DEPTH):
        for path, v in walk_strings(r[section]):
            if v == EM.MEASURED:
                assert "parameters" in path, f"{section}:{path}"             # only a caller's declared provenance echo
    assert r[EM.ACCOUNT_EXECUTION]["status"] != EM.MEASURED
    stat = {r[EM.PUBLIC_DISPLAYED_DEPTH]["status"], *[l["status"] for l in r[EM.PUBLIC_DISPLAYED_DEPTH]["legs"]]}
    assert stat == {EM.COMPUTED}                                              # md_economics' "MEASURED" is renamed here


def test_without_account_records_the_measured_section_is_not_available_and_holds_no_numbers():
    for none in (None, [], ()):
        r = run(scenario(), account_records=none)[EM.ACCOUNT_EXECUTION]
        assert r["status"] == EM.NOT_AVAILABLE and r["reasons"] == ["NO_ACCOUNT_EXECUTION_RECORDS_SUPPLIED"]
        assert "maker" not in r and "counts" not in r and "taker" not in r
    assert EM.measure_account_execution(None)["minimums"]["maker_attempts"] == EM.MIN_MAKER_ATTEMPTS == 30


def test_a_declared_measured_parameter_does_not_create_measured_account_evidence():
    sc = scenario(params={k: EM.measured(BASE[k], "my own spreadsheet") for k in ("fill_probability", "adverse_selection_bps")},
                  assumptions=assume(fill_probability=None, adverse_selection_bps=None))
    r = run(sc)
    assert r[EM.ACCOUNT_EXECUTION]["status"] == EM.NOT_AVAILABLE
    assert r["verdict"]["verdict"] == EM.MAKER_NOT_SUPPORTED
    assert r[EM.SIMULATED_FILL]["declared_measured_but_unverified_by_account_records"] == ["adverse_selection_bps", "fill_probability"]


@pytest.mark.parametrize("make", [lambda: maker_records(n=5, full=3, rejected=0),
                                  lambda: maker_records(n=29, full=20, rejected=1),
                                  lambda: maker_records(n=60, full=5, rejected=2),
                                  lambda: maker_records(n=60, full=40, span_days=0.2)])
def test_a_record_set_below_the_stated_minimum_is_insufficient_and_shows_no_estimate(make):
    recs = make()
    r = EM.measure_account_execution(recs)
    assert r["status"] == EM.INSUFFICIENT and r["reasons"] and r["estimates_withheld"] is True
    assert "maker" not in r and r["counts"]["records_supplied"] == len(recs)
    full = run(scenario(), account_records=recs)
    assert full["verdict"]["verdict"] == EM.MAKER_NOT_SUPPORTED and full[EM.ACCOUNT_EXECUTION]["status"] == EM.INSUFFICIENT


def test_fills_without_a_fee_record_and_duplicate_orders_are_excluded_not_silently_counted():
    recs = maker_records(n=80, full=60)
    broken = [maker_record(1000 + i, 80, "FILLED", with_fee=False) for i in range(5)]
    dup = [recs[0]]
    r = EM.measure_account_execution(recs + broken + dup)
    assert r["counts"]["excluded"] == {"DUPLICATE_ORDER_ID": 1, "FILL_WITHOUT_FEE_RECORD": 5}
    assert r["counts"]["records_supplied"] == 86 and r["counts"]["records_usable"] == 80
    only_broken = EM.measure_account_execution([maker_record(i, 40, "FILLED", with_fee=False) for i in range(40)])
    assert only_broken["status"] == EM.INSUFFICIENT and only_broken["counts"]["records_usable"] == 0


def test_a_sufficient_record_set_is_measured_with_conservative_bounds():
    recs = maker_records(n=80, full=60, rejected=4, fee_bps=2.0)
    recs += [taker_record(i) for i in range(20)]
    r = EM.measure_account_execution(recs)
    assert r["status"] == EM.MEASURED and r["evidence_class"] == EM.ACCOUNT_EXECUTION and r["is_simulated"] is False
    mk = r["maker"]
    assert mk["attempts"] == 80 and mk["full_fills"] == 60 and mk["post_only_rejections"] == 4
    assert mk["full_fill_rate"] == 0.75
    assert 0.0 < mk["full_fill_rate_lower_95"] < 0.75 < mk["full_fill_rate_upper_95"] < 1.0
    assert mk["full_fill_rate_given_accepted_lower_95"] > mk["full_fill_rate_lower_95"]
    assert mk["post_only_rejection_rate_upper_95"] > 4 / 80
    assert mk["adverse_selection"]["status"] == EM.MEASURED and mk["adverse_selection"]["upper_bound_bps"] > mk["adverse_selection"]["mean_bps"]
    assert mk["realized_fee_bps_by_market"]["linear_perp"]["mean_bps"] == pytest.approx(2.0)
    assert r["taker"]["fills"] == 20 and r["taker"]["realized_cost_n"] == 20


def test_taker_realized_cost_uses_the_signed_slippage_against_the_arrival_mid_plus_the_fee():
    recs = maker_records(n=80, full=60) + [taker_record(i) for i in range(20)]
    cost = EM.measure_account_execution(recs)["taker"]["realized_cost_bps_vs_arrival_mid"]
    slip = -1.0 * (89999.9 - 89999.95) / 89999.95 * 1e4                      # a sell below the mid costs, > 0
    assert cost == pytest.approx(slip + 5.5, abs=1e-9) and slip > 0.0


@pytest.mark.parametrize("src", ["mocked fills", "faked execution list", "backtested /v5/execution/list", "replayed tape", "shadow fills",
                                 "dry-run executions", "stub client", "fixture export", "hypothetical fills", "projected executions",
                                 "assumed fills", "modelled output", "generated by script", "paper trading", "estimated from the book", "MC sim",
                                 "Monte-Carlo", "forecast fills", "dummy data", "placeholder", "back-test", "back test", "re-played", "sandbox account",
                                 "demo account", "synthesised fills", "fabricated list"])
def test_the_provenance_tripwire_matches_word_stems_not_only_whole_words(src):
    with pytest.raises(EM.EvidenceClassError):
        EM.AccountExecutionRecord("x", "BTCUSDT", "spot", "buy", EM.TAKER_ORDER, "FILLED", 1.0, 1.0, 1.0, 2.0,
                                  src, avg_fill_price=1.0, fee_quote=0.0, fee_record_id="e")


@pytest.mark.parametrize("src", ["bybit v5 /v5/execution/list simple export", "excerpt of a venue /v5/execution/list reply",
                                 "BYBIT_V5_EXECUTION_LIST", "owner export of the unified account trade history 2026-11",
                                 "bybit v5 derivatives execution list", "Bybit account inventory export", "(project: carry-book)", "model account UTA2",
                                 "scenario 3 account", "sample-trader account", "exported 2026-11 EST", "testnet account execution list"])
def test_a_source_that_names_the_venue_record_is_not_blocked_by_the_tripwire(src):
    EM.AccountExecutionRecord("x", "BTCUSDT", "spot", "buy", EM.TAKER_ORDER, "FILLED", 1.0, 1.0, 1.0, 2.0,
                              src, avg_fill_price=1.0, fee_quote=0.0, fee_record_id="e")


def test_a_simulated_result_or_any_non_record_is_refused_as_account_evidence():
    sim = EM.simulate_maker_fill(0.0, 1.0, [(1.0, 1.0)], 0.0)
    for bad in (sim, sim.as_dict(), {"status": "FILLED"}, "FILLED", 3.0):
        with pytest.raises((EM.EvidenceClassError, ValueError)):
            EM.measure_account_execution([bad] if not isinstance(bad, str) else bad)
    mode_dict = run(scenario())[EM.SIMULATED_FILL]["modes"][EM.HYBRID]
    with pytest.raises(EM.EvidenceClassError):
        EM.measure_account_execution(maker_records(n=40, full=30) + [mode_dict])
    for src in ("SIMULATED fills", "paper-simulator", "sim", "synthetic fills", "backtest engine"):
        with pytest.raises(EM.EvidenceClassError, match="simulated"):
            EM.AccountExecutionRecord("x", "BTCUSDT", "spot", "buy", EM.TAKER_ORDER, "FILLED", 1.0, 1.0, 1.0, 2.0,
                                      src, avg_fill_price=1.0, fee_quote=0.0, fee_record_id="e")
    ok = EM.AccountExecutionRecord("x", "BTCUSDT", "spot", "buy", EM.TAKER_ORDER, "FILLED", 1.0, 1.0, 1.0, 2.0,
                                   "bybit v5 /v5/execution/list simple export", avg_fill_price=1.0, fee_quote=0.0,
                                   fee_record_id="e")
    assert ok.evidence_class == EM.ACCOUNT_EXECUTION


@pytest.mark.parametrize("kw", [dict(status="FILLED", qty_filled=0.5), dict(status="PARTIALLY_FILLED", qty_filled=1.0),
                                dict(status="CANCELLED_UNFILLED", qty_filled=0.2), dict(status="OPEN", qty_filled=0.0),
                                dict(status="FILLED", qty_filled=1.0, avg_fill_price=None),
                                dict(status="FILLED", qty_filled=1.5), dict(market="futures"), dict(side="long"),
                                dict(terminal_ms=0.5)])
def test_inconsistent_account_records_cannot_be_constructed(kw):
    base = dict(order_id="o", symbol="BTCUSDT", market="spot", side="buy", intent=EM.MAKER_POST_ONLY, status="FILLED",
                qty_requested=1.0, qty_filled=1.0, submitted_ms=1.0, terminal_ms=2.0, source="venue", avg_fill_price=1.0)
    EM.AccountExecutionRecord(**base)
    with pytest.raises(ValueError):
        EM.AccountExecutionRecord(**{**base, **kw})


def test_measured_data_never_leaks_into_the_simulation_and_the_simulation_never_into_measured_data():
    recs = maker_records(n=80, full=60) + [taker_record(i) for i in range(20)]
    plain = run(scenario(), account_records=None)
    with_records = run(scenario(), account_records=recs)
    assert plain[EM.SIMULATED_FILL] == with_records[EM.SIMULATED_FILL]
    assert plain[EM.PUBLIC_DISPLAYED_DEPTH] == with_records[EM.PUBLIC_DISPLAYED_DEPTH]
    other = run(scenario(fill_probability=0.05, adverse_selection_bps=9.0, latency_ms=5.0), account_records=recs)
    assert other[EM.ACCOUNT_EXECUTION] == with_records[EM.ACCOUNT_EXECUTION]
    assert other[EM.SIMULATED_FILL] != with_records[EM.SIMULATED_FILL]


# ====================================================================================================
# the verdict
# ====================================================================================================

RICH_MAKER = dict(fill_probability=1.0, adverse_selection_bps=0.0, maker_fee_bps_perp=-2.0, latency_ms=0.0)


def test_assumed_parameters_that_make_maker_look_cheap_still_give_maker_not_supported():
    sc = scenario(**RICH_MAKER)
    modes = expected(sc)
    assert modes[EM.HYBRID]["expected_cost_bps"] < 0.5 * modes[EM.TAKER]["expected_cost_bps"]   # looks wonderful ...
    v = EM.recommend_mode(sc)
    assert v["verdict"] == EM.MAKER_NOT_SUPPORTED and v["maker_economics_supported"] is False        # ... and is unsupported
    assert v["account_execution_status"] == EM.NOT_AVAILABLE
    assert any("UNSUPPORTED" in x for x in v["reasons"]) and v["model_at_measured_bounds"] is None
    w = v["what_would_have_to_be_true"]
    assert w["forecast"] is False and w["policy"] == EM.HYBRID and "no fill has been observed" in w["reading"]
    assert run(sc)["verdict"]["verdict"] == EM.MAKER_NOT_SUPPORTED


def test_the_default_verdict_is_maker_not_supported_for_every_input_that_lacks_measured_fills():
    for sc in (scenario(), scenario(**RICH_MAKER), scenario(EM.ACQUIRE, **RICH_MAKER),
               scenario(assumptions=EM.Assumptions()), scenario(maker_fee_bps_perp=40.0)):
        v = EM.recommend_mode(sc, None)
        assert v["verdict"] == EM.MAKER_NOT_SUPPORTED and v["verdict"] in EM.VERDICTS
        assert v["reasons"]


def test_insufficient_records_cannot_support_maker_whatever_the_assumptions_say():
    v = EM.recommend_mode(scenario(**RICH_MAKER), maker_records(n=29, full=29, rejected=0))
    assert v["verdict"] == EM.MAKER_NOT_SUPPORTED and v["account_execution_status"] == EM.INSUFFICIENT
    assert any("BELOW_MINIMUM" in x for x in v["reasons"])


def good_scenario(**over):
    return scenario(post_only_reject_probability=0.05, latency_ms=5.0, **over)


def test_sufficient_good_measured_fills_support_maker_and_the_evaluation_is_labelled_a_model_output():
    recs = maker_records(n=80, full=60, rejected=4, markout_fn=lambda i: 0.3 + 0.1 * (i % 5))
    v = EM.recommend_mode(good_scenario(), recs)
    assert v["verdict"] == EM.MAKER_SUPPORTED_BY_MEASURED_FILLS and v["maker_economics_supported"] is True
    m = v["model_at_measured_bounds"]
    assert m["evidence_class"] == EM.SIMULATED_FILL and m["is_measured"] is False
    assert m["hybrid_cost_bps"] < m["taker_cost_bps"] and m["supporting_policy"] == EM.HYBRID
    inp = m["inputs_from_account_records"]
    mk = EM.measure_account_execution(recs)["maker"]
    assert inp["fill_probability_given_accepted_lower_95"] == mk["full_fill_rate_given_accepted_lower_95"]
    assert inp["adverse_selection_upper_bps"] == pytest.approx(mk["adverse_selection"]["upper_bound_bps"])
    assert inp["post_only_rejection_floor_upper_95"] == mk["post_only_rejection_rate_upper_95"]
    # the assumed fill probability and adverse selection in the scenario play no part in this verdict
    flipped = EM.recommend_mode(good_scenario(fill_probability=0.01, adverse_selection_bps=50.0), recs)
    assert flipped["model_at_measured_bounds"]["hybrid_cost_bps"] == m["hybrid_cost_bps"]


def test_the_verdict_arithmetic_uses_exactly_the_measured_bounds_including_the_rejection_floor():
    recs = maker_records(n=80, full=50, rejected=12, markout_fn=lambda i: 0.3 + 0.1 * (i % 5))
    sc = scenario(post_only_reject_probability=0.0, latency_ms=20.0)
    m = EM.recommend_mode(sc, recs)["model_at_measured_bounds"]
    inp = m["inputs_from_account_records"]
    assert inp["post_only_rejection_floor_upper_95"] > 12 / 80                 # the model's 0 is floored by the data
    p, a, q = (inp["fill_probability_given_accepted_lower_95"], inp["adverse_selection_upper_bps"],
               inp["post_only_rejection_floor_upper_95"])
    fee_m = inp["maker_fee_bps_by_instrument_used_if_higher_than_modelled"].get("perp", 2.0)
    want = 0.0
    for w in walks(EM.OVERLAY, TIGHT, NOTIONAL, REF).values():
        h = w.half_spread_bps
        c_tk, c_mk = h + w.slippage_bps + FEES.perp_taker_bps, -h + max(fee_m, 2.0) + a
        want += q * (c_tk + D(0.9, 60.0)) + (1 - q) * (p * c_mk + (1 - p) * (c_tk + D(0.9, 2 * 20.0 + 5000.0)))
    assert m["hybrid_cost_bps"] == pytest.approx(want)
    assert m["taker_cost_bps"] == pytest.approx(hand_taker(EM.OVERLAY, TIGHT, NOTIONAL, REF, 0.9, 20.0))


def test_a_required_edge_can_turn_a_measured_win_into_taker_only():
    recs = maker_records(n=80, full=60, rejected=4, markout_fn=lambda i: 0.3 + 0.1 * (i % 5))
    m = EM.recommend_mode(good_scenario(), recs)["model_at_measured_bounds"]
    gap = m["taker_cost_bps"] - m["hybrid_cost_bps"]
    assert EM.recommend_mode(good_scenario(), recs, min_edge_bps=gap - 0.01)["verdict"] == EM.MAKER_SUPPORTED_BY_MEASURED_FILLS
    assert EM.recommend_mode(good_scenario(), recs, min_edge_bps=gap + 0.01)["verdict"] == EM.TAKER_ONLY
    with pytest.raises(ValueError):
        EM.recommend_mode(good_scenario(), recs, min_edge_bps=-1.0)


def test_poor_measured_fills_or_severe_measured_adverse_selection_give_taker_only():
    poor_fills = maker_records(n=80, full=12, rejected=0)                 # 12 of 80 filled
    v1 = EM.recommend_mode(good_scenario(), poor_fills)
    assert v1["verdict"] == EM.TAKER_ONLY and v1["account_execution_status"] == EM.MEASURED
    # pure MAKER looks cheaper here only because the unmeasured missed-fill penalty is small: it must not decide
    b = v1["model_at_measured_bounds"]
    assert b["maker_only_cost_bps_not_used"] < b["taker_cost_bps"] < b["hybrid_cost_bps"]
    toxic = maker_records(n=80, full=64, rejected=0, markout_fn=lambda i: 9.0 + 0.3 * (i % 7))
    v2 = EM.recommend_mode(good_scenario(), toxic)
    assert v2["verdict"] == EM.TAKER_ONLY
    assert v2["model_at_measured_bounds"]["inputs_from_account_records"]["adverse_selection_upper_bps"] > 9.0
    # a measured fee above the modelled maker fee is used (the worse of the two)
    pricey = maker_records(n=80, full=60, rejected=4, fee_bps=9.0, markout_fn=lambda i: 0.3 + 0.1 * (i % 5))
    v3 = EM.recommend_mode(good_scenario(), pricey)
    assert v3["model_at_measured_bounds"]["inputs_from_account_records"]["maker_fee_bps_by_instrument_used_if_higher_than_modelled"] == {
        "perp": pytest.approx(9.0)}
    assert v3["verdict"] == EM.TAKER_ONLY


def test_measured_fills_cannot_support_maker_when_the_taker_baseline_or_other_inputs_are_unknown():
    recs = maker_records(n=80, full=60, rejected=4, markout_fn=lambda i: 0.3 + 0.1 * (i % 5))
    v = EM.recommend_mode(scenario(assumptions=assume(latency_ms=None, vol_bps_per_sqrt_s=None,
                                                      post_only_reject_probability=0.05)), recs)
    assert v["verdict"] == EM.MAKER_NOT_SUPPORTED
    assert any("CANNOT_EVALUATE_UNKNOWN_PARAMETERS" in x and "latency_ms" in x for x in v["reasons"])
    constant = maker_records(n=80, full=60, rejected=4, markout_fn=lambda i: 0.5)         # no spread to bound
    v2 = EM.recommend_mode(good_scenario(), constant)
    assert v2["verdict"] == EM.MAKER_NOT_SUPPORTED and any("ADVERSE_SELECTION" in x for x in v2["reasons"])


# ====================================================================================================
# purity
# ====================================================================================================

ALLOWED_IMPORTS = {"__future__", "itertools", "math", "random", "dataclasses", "statistics", "typing",
                   "evidence_stats", "md_economics", "trade_economics"}


def module_tree():
    return ast.parse(open(os.path.join(BOT, "execution_model.py"), encoding="utf-8").read())


def test_the_module_imports_only_the_standard_library_and_the_three_allowed_repo_modules():
    seen = set()
    for node in ast.walk(module_tree()):
        if isinstance(node, ast.Import):
            seen |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            seen.add((node.module or "").split(".")[0])
    assert seen <= ALLOWED_IMPORTS, sorted(seen - ALLOWED_IMPORTS)
    assert {"md_economics", "evidence_stats", "trade_economics"} <= seen


def test_importing_the_module_reaches_no_order_broker_client_store_or_config():
    code = ("import sys; sys.path.insert(0, %r); import execution_model; "
            "print('\\n'.join(sorted(m for m in sys.modules if '.' not in m)))" % BOT)
    out = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True, timeout=60, cwd=BOT)
    assert out.returncode == 0, out.stderr
    loaded = set(out.stdout.split())
    local = {f[:-3] for f in os.listdir(BOT) if f.endswith(".py")}
    mine = loaded & local
    assert mine == {"execution_model", "md_economics", "trade_economics", "evidence_stats", "carry_costs", "strategy_evidence"}
    forbidden = ("broker", "client", "store", "config", "persistence", "order", "bybit", "ledger", "engine")
    assert not [m for m in mine if any(f in m for f in forbidden)]


def test_no_io_no_wall_clock_no_unseeded_randomness_and_no_import_time_side_effects():
    tree = module_tree()
    banned_calls = {"open", "print", "input", "exec", "eval", "__import__"}
    random_attrs, time_attrs = set(), set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            assert node.func.id not in banned_calls, node.func.id
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            if node.value.id == "random":
                random_attrs.add(node.attr)
            if node.value.id in ("time", "datetime", "os", "sys", "socket", "subprocess"):
                time_attrs.add(node.value.id)
    assert random_attrs == {"Random"}                       # only seeded generators, never the global one
    assert not time_attrs
    allowed_top = (ast.Import, ast.ImportFrom, ast.Assign, ast.AnnAssign, ast.FunctionDef, ast.ClassDef, ast.Expr)
    for node in tree.body:
        assert isinstance(node, allowed_top), ast.dump(node)[:80]
        if isinstance(node, ast.Expr):
            assert isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)     # the docstring only


def test_every_output_is_json_serialisable_and_finite():
    for mode in (EM.OVERLAY, EM.ACQUIRE):
        recs = maker_records(n=80, full=60) + [taker_record(i) for i in range(12)]
        r = run(scenario(mode), account_records=recs)
        text = json.dumps(r, allow_nan=False)
        assert json.loads(text)["schema"] == EM.SCHEMA
    json.dumps(run(scenario(assumptions=EM.Assumptions())), allow_nan=False)
    json.dumps(EM.simulate_maker_fill(1.0, 1.0, [(1.0, 5.0)], 0.0).as_dict(), allow_nan=False)


def test_the_scenario_itself_is_validated():
    with pytest.raises(ValueError):
        run(scenario(mode="margin"))
    with pytest.raises(ValueError):
        run(scenario(notional=0.0))
    with pytest.raises(ValueError):
        run(scenario(timeout=-1.0))
    with pytest.raises(TypeError):
        run(scenario(fees=(5.5, 10.0)))
    with pytest.raises(TypeError):
        EM.compare_execution_modes({"mode": "overlay"})


def test_the_provenance_tripwire_refuses_more_than_the_word_simulated():
    """A tripwire, not a proof (an account record cannot prove where it came from), but it should not be defeated by the obvious synonyms."""
    base = dict(order_id="o", symbol="BTCUSDT", market="spot", side="buy", intent=EM.MAKER_POST_ONLY, status="FILLED",
                qty_requested=1.0, qty_filled=1.0, submitted_ms=1.0, terminal_ms=2.0, avg_fill_price=1.0)
    EM.AccountExecutionRecord(**{**base, "source": "venue"})
    for src in ("paper", "mock", "replay", "fake", "monte_carlo", "generated", "SimFill_v2", "estimated fills", "derived from model", "Paper trading"):
        with pytest.raises(EM.EvidenceClassError):
            EM.AccountExecutionRecord(**{**base, "source": src})
