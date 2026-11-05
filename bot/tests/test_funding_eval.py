import copy
import math
import random

import pytest

import funding_eval as FE
from capacity_helpers import base_row, series, H8, START

BASE = base_row()
_RNG = random.Random(11)
_NOISE = {k: 0.6 + _RNG.gauss(0, 0.25) for k in range(2000)}


def noisy(T):
    """A realised print with real variation and no persistence: what a claim-worthy series looks like."""
    return _NOISE[(T // H8) % 2000]


def run(n=60, forecast=lambda T, t: 0.6, realised=lambda T: 0.6, **kw):
    rows = series(BASE, n, forecast, realised, **kw)
    return rows, FE.evaluate(rows)


def test_a_perfect_forecast_has_zero_bias_and_full_sign_accuracy():
    rows, ev = run(n=150, forecast=lambda T, t: noisy(T), realised=noisy)
    assert ev["status"] == "OK" and ev["joined"] >= 145 and ev["realised_bps"]["ess"] >= FE.MIN_ESS
    o = ev["by_offset"][30]
    assert o["bias_bps"] == pytest.approx(0.0) and o["mae_bps"] == pytest.approx(0.0) and o["sign_accuracy"] == 1.0


def test_a_known_bias_is_measured_exactly_and_the_ci_brackets_it():
    rows, ev = run(n=150, forecast=lambda T, t: noisy(T) + 0.3, realised=noisy)
    o = ev["by_offset"][60]
    assert o["bias_bps"] == pytest.approx(0.3) and o["rmse_bps"] == pytest.approx(0.3)
    assert o["bias_ci"]["status"] == "NO_VARIANCE"       # the error is a constant 0.3: nothing to resample, no false bound


def test_the_realised_print_is_the_public_print_of_that_settlement_not_a_neighbours():
    rows, ev = run(realised=lambda T: 0.1 * ((T // H8) % 7))
    for rec in FE.join(rows)["joined"]:
        assert rec["realised_bps"] == pytest.approx(0.1 * ((rec["settlement_ms"] // H8) % 7))


def test_a_settlement_with_no_print_is_counted_missing_never_forward_filled():
    gone = START + 20 * H8
    rows = [r for r in series(BASE, 40, lambda T, t: 0.6, lambda T: 0.6)
            if r["funding"]["last_print_settled_ms"] != gone]
    j = FE.join(rows)
    assert gone not in [x["settlement_ms"] for x in j["joined"]]
    assert j["missing_realised"] >= 1


def test_a_print_seen_before_its_own_settlement_is_an_anomaly_and_excluded():
    rows = series(BASE, 30, lambda T, t: 0.6, lambda T: 0.6)
    T = START + 10 * H8
    early = copy.deepcopy([r for r in rows if r["funding"]["next_settlement_ms"] == T][5])
    early["funding"]["last_print_bps"], early["funding"]["last_print_settled_ms"] = 9.9, T
    early["row_id"] = "mainnet:BTCUSDT:99999"
    j = FE.join(rows + [early])
    assert j["anomalies"] and T not in [x["settlement_ms"] for x in j["joined"]]


def test_forecasts_use_only_rows_before_the_settlement_and_the_predeclared_offsets():
    rows = series(BASE, 30, lambda T, t: 0.6 if t < T - 25 * 60_000 else 5.0, lambda T: 0.6)
    j = FE.join(rows)
    rec = j["joined"][5]
    assert rec["forecasts"][60] == 0.6 and rec["forecasts"][10] == 5.0      # each offset reads its own instant
    assert set(rec["forecasts"]) <= {60, 30, 10}


def test_extreme_negative_outcomes_and_calibration_buckets_are_reported():
    rows, ev = run(n=80, forecast=lambda T, t: 0.6, realised=lambda T: -3.0 if (T // H8) % 10 == 0 else 0.6)
    r = ev["realised_bps"]
    assert r["min"] == -3.0 and r["count_below_minus_1bps"] >= 7 and r["share_negative"] > 0.05
    b = ev["by_offset"][30]["calibration_by_forecast_bucket"]
    assert b["0.5_to_1"]["n"] > 50 and b["0.5_to_1"]["mean_realised"] < 0.6        # forecasts overpromise


def test_persistence_does_not_bridge_a_missing_settlement():
    rows = series(BASE, 40, lambda T, t: 0.6, lambda T: 0.2 * ((T // H8) % 5))
    ev_all = FE.evaluate(rows)
    gone = START + 15 * H8
    thin = [r for r in rows if r["funding"]["last_print_settled_ms"] != gone]
    ev_thin = FE.evaluate(thin)
    assert ev_thin["persistence"]["adjacent_pairs"] < ev_all["persistence"]["adjacent_pairs"] - 1


def test_too_few_settlements_is_insufficient_not_a_number_to_trust():
    rows, ev = run(n=8)
    assert ev["status"] == "INSUFFICIENT" and ev["min_independent"] == 30


def test_a_naive_last_print_baseline_is_reported_beside_the_forecast():
    rows, ev = run(forecast=lambda T, t: 0.6, realised=lambda T: 0.6)
    assert ev["by_offset"][30]["mae_naive_last_print_bps"] is not None


def test_a_constant_realised_series_is_never_called_sufficient_however_long():
    """Bybit's funding often sits at its baseline for weeks. A month of identical prints says 'no variation was
    seen', not 'the distribution is known': ESS is 1 and no zero-width interval is offered."""
    rows, ev = run(n=150)
    assert ev["joined"] >= 145 and ev["status"] == "INSUFFICIENT"
    assert ev["realised_bps"]["ess"] == 1.0 and ev["realised_bps"]["ci"]["status"] == "NO_VARIANCE"
    assert any("effective sample size" in w for w in ev["status_reasons"])


def test_a_persistent_series_with_ninety_settlements_does_not_pass_as_ninety_independent_draws():
    rng = random.Random(5)
    x, ar = 0.0, {}
    for k in range(400):
        x = 0.9 * x + rng.gauss(0, 0.2)
        ar[k] = 0.6 + x
    rows, ev = run(n=90, forecast=lambda T, t: ar[(T // H8) % 400], realised=lambda T: ar[(T // H8) % 400])
    assert ev["joined"] >= 85
    assert ev["realised_bps"]["ess"] < FE.MIN_ESS and ev["status"] == "INSUFFICIENT"


def test_conflicting_public_prints_for_one_settlement_block_the_status_and_keep_the_adverse_value():
    rows = series(BASE, 150, lambda T, t: noisy(T), noisy)
    T = START + 40 * H8
    late = [r for r in rows if r["funding"]["last_print_settled_ms"] == T]
    late[len(late) // 2]["funding"]["last_print_bps"] = noisy(T) - 5.0
    ev = FE.evaluate(rows)
    assert ev["conflicting_prints"] == 1 and ev["status"] == "INSUFFICIENT"
    rec = [r for r in FE.join(rows)["joined"] if r["settlement_ms"] == T][0]
    assert rec["realised_bps"] == pytest.approx(noisy(T) - 5.0)
