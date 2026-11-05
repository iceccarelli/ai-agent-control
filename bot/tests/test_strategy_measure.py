"""The measurement kernels: hand-checked mechanics, no-lookahead structure, and NULL CALIBRATION (a kernel that finds an edge in
noise at more than about the stated rate is worse than no kernel). Every planted-effect case has a matching wrong-rule case that
must NOT detect it."""
import copy
import math
import random

import pytest

import strategy_measure as SM
from capacity_helpers import base_row, clone, series, H8, START

SEEDS = range(40)


def prices_from(rets, p0=60000.0):
    p = [p0]
    for r in rets:
        p.append(p[-1] * (1 + r))
    return p


def trending_returns(n, rng, persist=0.98, dsd=0.00012, sd=0.001):
    mu, out = 0.0, []
    for _ in range(n):
        mu = persist * mu + rng.gauss(0, dsd)
        out.append(mu + rng.gauss(0, sd))
    return out


def ou_prices(n, rng, phi=0.93, usd=0.0015, drift_sd=0.0005):
    f, u, out = math.log(60000.0), 0.0, []
    for _ in range(n):
        f += rng.gauss(0, drift_sd)
        u = phi * u + rng.gauss(0, usd)
        out.append(math.exp(f + u))
    return out


def lo_positive(res, key="net_per_bar"):
    ci = res[key]["ci"]
    return ci.get("status") == "OK" and ci["lo"] > 0


def directional(prices, rule):
    return SM.run_directional(prices, rule, one_way_cost_bps=1.0, bars_per_year=288 * 365, eval_start=500, embargo=24)


# ======================================= basis episodes =======================================

def brow(t_s, b):
    return {"at_ms": 1_800_000_000_000 + int(t_s * 1000), "basis_bps": b}


P = SM.BasisParams(theta_in_bps=10.0, theta_out_bps=4.0, max_hold_s=3600.0, round_trip_cost_bps=8.0)


def test_a_converging_episode_is_measured_by_hand_with_fills_at_the_next_snapshot():
    rows = [brow(i * 300, b) for i, b in enumerate([1, 1, 12, 14, 9, 3, 2, 1])]
    r = SM.basis_episodes(rows, P)
    assert r["episodes"] == 1 and r["converged"] == 1 and r["failed"] == 0
    e = r["episodes_list"][0]
    # signal seen at 12; the entry FILL is the next snapshot (14); the exit signal is seen at 3; the exit FILL is the next (2)
    assert e["entry_bps"] == 14 and e["exit_bps"] == 2 and e["gross_bps"] == 12 and e["net_bps"] == pytest.approx(4.0)
    assert e["adverse_widening_bps"] == 0 and e["hold_h"] == pytest.approx(0.25)
    assert "NEXT recorded snapshot" in r["fill_rule"]


def test_failure_to_converge_is_closed_after_the_max_hold_at_the_next_snapshots_basis():
    rows = [brow(0, 1), brow(300, 12)] + [brow(600 + 300 * k, 13) for k in range(16)]
    r = SM.basis_episodes(rows, P)
    assert r["episodes"] == 1 and r["failed"] == 1 and r["failure_share"] == 1.0
    e = r["episodes_list"][0]
    assert e["outcome"] == "FAILED_TO_CONVERGE" and e["net_bps"] == pytest.approx(13 - 13 - 8)


def test_a_data_gap_excludes_the_episode_rather_than_interpolating_across_it():
    rows = [brow(0, 1), brow(300, 12), brow(600, 13), brow(600 + 7200, 2), brow(600 + 7500, 1)]
    r = SM.basis_episodes(rows, P)
    assert r["episodes"] == 0 and r["excluded_by_data_gap"] == 1


def test_episodes_do_not_chain_the_basis_must_fall_below_entry_before_a_new_one_opens():
    two = [brow(i * 300, b) for i, b in enumerate([1, 12, 12, 3, 3, 1, 12, 12, 3, 3])]
    assert SM.basis_episodes(two, P)["episodes"] == 2
    chained = [brow(i * 300, b) for i, b in enumerate([1, 12, 12, 3, 12, 12, 3, 3])]       # exits at 12; 12 again must not re-open at once
    r = SM.basis_episodes(chained, P)
    assert r["episodes"] == 1 and r["episodes_list"][0]["exit_bps"] == 12


def test_a_signal_on_the_last_snapshot_has_no_fill_and_is_reported_open_not_booked():
    r = SM.basis_episodes([brow(0, 1), brow(300, 12)], P)
    assert r["episodes"] == 0 and r["still_open_at_end"] is True


def test_the_cost_is_required_and_malformed_parameters_refuse():
    rows = [brow(0, 1)]
    with pytest.raises(ValueError):
        SM.basis_episodes(rows, SM.BasisParams(10, 4, 3600, None))
    with pytest.raises(ValueError):
        SM.basis_episodes(rows, SM.BasisParams(10, 4, 3600, float("nan")))
    with pytest.raises(ValueError):
        SM.basis_episodes(rows, SM.BasisParams(4, 10, 3600, 8))
    with pytest.raises(ValueError):
        SM.basis_episodes([brow(10, 1), brow(5, 1)], P)


def basis_path(rng, n, revert, theta_mean=3.0):
    b, out = theta_mean, []
    for i in range(n):
        b += revert * (theta_mean - b) + rng.gauss(0, 2.2)
        out.append(brow(i * 300, b))
    return out


def test_a_noisy_basis_whose_range_exceeds_cost_still_nets_negative_after_cost():
    """The 'percentile range exceeds fees' screen would call this attractive; the episodes say what actually happened."""
    neg = 0
    for s in range(20):
        r = SM.basis_episodes(basis_path(random.Random(s), 6000, 0.15), SM.BasisParams(8.0, 3.0, 4 * 3600, 8.0))
        if r["episodes"] >= 30:
            neg += (r["net"]["mean"] < 0)
    assert neg >= 15


def test_a_basis_that_really_snaps_back_far_beyond_cost_is_measured_positive():
    rows, t = [], 0
    rng = random.Random(3)
    for k in range(60):                                       # 60 spikes: up fast, back fast, quiet between
        for b in (1.0, 1.5, 28.0 + rng.gauss(0, 2), 26.0 + rng.gauss(0, 2), 3.0, 1.0, 1.2):
            rows.append(brow(t, b)); t += 300
    r = SM.basis_episodes(rows, SM.BasisParams(20.0, 5.0, 3600.0, 8.0))
    assert r["episodes"] == 60 and r["converged"] == 60 and r["net"]["mean"] > 10 and r["net"]["ci"]["status"] in ("OK", "NO_VARIANCE", "INSUFFICIENT")


# ======================================= directional rules =======================================

class Spy:
    def __init__(self):
        self.seen = []
        self.calls_at_bar = []

    def update(self, price):
        self.seen.append(price)
        return 1.0


def test_the_rule_is_fed_one_price_at_a_time_and_cannot_see_the_bar_it_trades():
    prices = [100.0, 101.0, 99.0, 103.0, 98.0, 104.0]
    spy = Spy()
    SM.run_directional(prices, spy, one_way_cost_bps=0.0, bars_per_year=1000, eval_start=0)
    assert spy.seen == prices                                      # exactly in order, each price delivered once
    # an oracle cannot be expressed through this interface; a perfect-foresight rule still only gets update(price_t) per bar
    class Oracle:
        def __init__(self): self.t = -1
        def update(self, price):
            self.t += 1
            return 1.0 if (self.t + 1 < len(prices) and prices[self.t + 1] > price) else -1.0      # peeks via the closure
    cheat = SM.run_directional(prices, Oracle(), one_way_cost_bps=0.0, bars_per_year=1000, eval_start=0)
    assert cheat["net_per_bar"]["mean"] > 0           # the CHEAT needs the test's own list; the library never hands the future to a rule


def test_costs_are_charged_on_the_bar_the_position_changes_exactly_once():
    prices = [100.0, 101.0, 102.0, 103.0]
    r = SM.run_directional(prices, Spy(), one_way_cost_bps=10.0, bars_per_year=1000, eval_start=0)
    nets = r["net_per_bar"]["mean"] * r["bars"]                     # always long: one entry cost, then pure hold
    hold = (103.0 / 100.0) - 1.0
    expected_sum = sum(prices[i] / prices[i - 1] - 1 for i in range(1, 4)) - 10.0 / 1e4
    assert nets == pytest.approx(expected_sum) and hold > 0


def test_the_warm_up_and_the_embargo_are_never_counted():
    p = prices_from([0.001] * 200)
    r = SM.run_directional(p, SM.EMACross(5, 20), one_way_cost_bps=1.0, bars_per_year=1000, eval_start=50, embargo=10)
    assert r["bars"] == len(p) - 60                   # bars 60..200: the first 50 (warm-up) and 10 (embargo) are run, never counted
    assert SM.run_directional(p, SM.EMACross(5, 20), one_way_cost_bps=1.0, bars_per_year=1000, eval_start=10_000)["status"] == "INSUFFICIENT"


def test_costs_and_prices_are_validated_never_defaulted():
    with pytest.raises(ValueError):
        SM.run_directional([1.0, 2.0, 3.0], Spy(), one_way_cost_bps=None, bars_per_year=1000)
    with pytest.raises(ValueError):
        SM.run_directional([1.0, -2.0, 3.0], Spy(), one_way_cost_bps=1.0, bars_per_year=1000)
    with pytest.raises(ValueError):
        SM.EMACross(10, 5)
    with pytest.raises(ValueError):
        SM.ZScoreReversion(3, 2.0, 0.5)


def test_NULL_trend_and_reversion_rules_find_nothing_in_a_random_walk():
    t_hits = m_hits = 0
    for s in SEEDS:
        p = prices_from([random.Random(100 + s).gauss(0, 0.001) for _ in range(2500)])
        t_hits += lo_positive(directional(p, SM.EMACross(12, 48)))
        m_hits += lo_positive(directional(p, SM.ZScoreReversion(48, 2.0, 0.5)))
    assert t_hits <= 4 and m_hits <= 4, (t_hits, m_hits)             # nominal false-positive rate ~5 % of 40 = 2


def test_trend_is_detected_in_persistent_drift_and_reversion_is_not():
    t = m = 0
    for s in SEEDS:
        p = prices_from(trending_returns(2500, random.Random(200 + s)))
        t += lo_positive(directional(p, SM.EMACross(12, 48)))
        m += lo_positive(directional(p, SM.ZScoreReversion(48, 2.0, 0.5)))
    assert t >= 28 and m <= 3, (t, m)


def test_reversion_is_detected_in_a_mean_reverting_price_and_trend_is_not():
    t = m = 0
    for s in SEEDS:
        p = ou_prices(2500, random.Random(300 + s))
        m += lo_positive(directional(p, SM.ZScoreReversion(48, 2.0, 0.5)))
        t += lo_positive(directional(p, SM.EMACross(12, 48)))
    assert m >= 28 and t <= 3, (m, t)


def test_a_rule_that_only_matches_buy_and_hold_in_a_rising_market_is_not_credited_with_skill():
    p = prices_from([0.0004 + random.Random(7).gauss(0, 0.0005) for _ in range(2500)])
    r = directional(p, SM.EMACross(12, 48))
    assert r["hold_annualised_pct"] > 0 and r["excess_vs_hold_per_bar"]["mean"] is not None
    assert not lo_positive(r, "excess_vs_hold_per_bar")


# ======================================= order-book imbalance =======================================

def imbalance_world(rng, n, beta_bps):
    imb = [rng.uniform(-1, 1) for _ in range(n)]
    mid = [60000.0]
    for i in range(n - 1):
        mid.append(mid[-1] * (1 + beta_bps * imb[i] / 1e4 + rng.gauss(0, 0.001)))
    return imb, mid


def test_a_real_imbalance_signal_is_measured_positive_and_a_null_one_is_not():
    real = null = 0
    for s in range(20):
        i1, m1 = imbalance_world(random.Random(500 + s), 4000, 12.0)
        i0, m0 = imbalance_world(random.Random(600 + s), 4000, 0.0)
        a = SM.imbalance_trades(i1, m1, horizon=1, theta=0.6, one_way_cost_bps=1.0)
        b = SM.imbalance_trades(i0, m0, horizon=1, theta=0.6, one_way_cost_bps=1.0)
        real += a["net_bps_per_trade"]["ci"].get("status") == "OK" and a["net_bps_per_trade"]["ci"]["lo"] > 0
        null += b["net_bps_per_trade"]["ci"].get("status") == "OK" and b["net_bps_per_trade"]["ci"]["lo"] > 0
    assert real >= 16 and null <= 1


def test_imbalance_inputs_are_validated_and_the_fill_assumption_is_stated():
    with pytest.raises(ValueError):
        SM.imbalance_trades([0.1, 2.0], [100.0, 101.0], horizon=1, theta=0.5, one_way_cost_bps=1.0)
    with pytest.raises(ValueError):
        SM.imbalance_trades([0.1, 0.2], [100.0, 101.0], horizon=1, theta=0.5, one_way_cost_bps=None)
    r = SM.imbalance_trades([0.9, 0.0, 0.0, 0.0], [100.0, 100.0, 101.0, 101.0], horizon=2, theta=0.5, one_way_cost_bps=0.0)
    assert r["trades"] == 1 and "MID" in r["fill_assumption"]


# ======================================= regime =======================================

def regime_world(rng, n, block=250):
    rets, vols = [], []
    level = 1
    for i in range(n):
        if i % block == 0:
            level = rng.choice((0, 1, 2))
        v = (0.0004, 0.001, 0.0025)[level]
        rets.append(rng.gauss(0, v))
        vols.append(level)
    return prices_from(rets), vols


def test_regime_labels_use_the_past_only():
    p, _ = regime_world(random.Random(1), 3000)
    a = SM.regime_labels(p[:2000], window=50, min_history=100)
    b = SM.regime_labels(p, window=50, min_history=100)
    assert a == b[:2000]                                         # extending the series cannot change an earlier label
    assert a[:50] == [None] * 50 and any(x is not None for x in a)
    p2 = list(p)
    p2[2500:] = [x * 3 for x in p2[2500:]]
    assert SM.regime_labels(p2, window=50, min_history=100)[:2400] == b[:2400]


def test_a_regime_that_really_changes_the_value_is_found_out_of_sample_and_a_null_one_is_not():
    real = null = 0
    for s in range(30):
        rng = random.Random(800 + s)
        p, vols = regime_world(rng, 6000)
        labels = SM.regime_labels(p, window=50, min_history=200)
        v_real = [0.2 + (0.5 if lab == "HIGH" else 0.0) + rng.gauss(0, 0.3) for lab in labels]
        v_null = [0.2 + rng.gauss(0, 0.3) for _ in labels]
        a = SM.regime_conditioning(labels, v_real, alpha=0.05 / 3)
        b = SM.regime_conditioning(labels, v_null, alpha=0.05 / 3)
        real += a.get("improves_out_of_sample", False)
        null += b.get("improves_out_of_sample", False)
    assert real >= 22 and null <= 3, (real, null)


def test_regime_conditioning_refuses_thin_data_and_states_the_chronology():
    assert SM.regime_conditioning(["LOW"] * 10, [0.1] * 10)["status"] == "INSUFFICIENT"
    r = SM.regime_conditioning(["LOW", "MID", "HIGH"] * 60, [0.1 * (i % 7) for i in range(180)])
    assert r["status"] in ("MEASURED", "INSUFFICIENT") and (r["status"] == "INSUFFICIENT" or "second" in r["reading"])


# ======================================= cross-sectional funding =======================================

SYMS = ("BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT")


def multi_rows(rng, n_settle, informative, env="mainnet", cost_extra=None, drop=None, equal_means=False):
    base = base_row()
    mu = {s: (0.6 if equal_means else m) for s, m in zip(SYMS, (0.6, 0.9, 1.4, 0.3))}
    true = {s: {k: mu[s] + rng.gauss(0, 0.5) for k in range(n_settle + 5)} for s in SYMS}
    fc_noise = {s: {k: (rng.gauss(0, 0.1) if informative else rng.gauss(0, 1.0) + mu[s]) for k in range(n_settle + 5)} for s in SYMS}
    out = {}
    for s in SYMS:
        rows = series(base, n_settle, lambda T, t, s=s: (true[s][(T // H8) % (n_settle + 5)] + fc_noise[s][(T // H8) % (n_settle + 5)]) if informative
                      else fc_noise[s][(T // H8) % (n_settle + 5)], lambda T, s=s: true[s][(T // H8) % (n_settle + 5)], cadence_ms=1_800_000)
        for r in rows:
            r["symbol"], r["environment"] = s, env
            r["row_id"] = f"{env}:{s}:{r['source']['store_record_id']}"
            if cost_extra and s in cost_extra:
                for p in r["curve"]:
                    if p.get("status") == "MEASURED":
                        p["total_round_trip_bps"] += cost_extra[s]
        out[s] = rows if not (drop and s in drop) else []
    return out


def run_xs(rows, **kw):
    kw.setdefault("baseline", "BTCUSDT")
    return SM.cross_sectional_funding(rows, **kw)


def test_informative_forecasts_rank_realised_funding_and_tilting_beats_the_baseline():
    r = run_xs(multi_rows(random.Random(1), 80, informative=True), k=1, hold_settlements=21)
    assert r["status"] == "MEASURED" and r["aligned_settlements"] >= 60
    assert r["rank_information_coefficient"]["mean"] > 0.3
    ci = r["net_edge_bps_per_settlement_vs_baseline"]["ci"]
    assert ci["status"] == "OK" and ci["lo"] > 0


def test_NULL_forecasts_that_carry_no_information_have_no_rank_information_and_no_edge():
    """Equal mean funding across instruments and forecasts independent of the prints: the only thing left is noise."""
    hits = 0
    for s in range(12):
        r = run_xs(multi_rows(random.Random(40 + s), 80, informative=False, equal_means=True), k=1)
        ci = r["net_edge_bps_per_settlement_vs_baseline"]["ci"]
        hits += ci.get("status") == "OK" and ci["lo"] > 0
        assert abs(r["rank_information_coefficient"]["mean"]) < 0.35
    assert hits <= 2


def test_each_instruments_own_cost_is_charged_and_can_destroy_the_tilt():
    cheap = run_xs(multi_rows(random.Random(2), 80, informative=True), k=1, hold_settlements=21)
    dear = run_xs(multi_rows(random.Random(2), 80, informative=True, cost_extra={"ETHUSDT": 400.0, "SOLUSDT": 400.0, "XRPUSDT": 400.0}),
                  k=1, hold_settlements=3)
    assert dear["net_edge_bps_per_settlement_vs_baseline"]["mean"] < cheap["net_edge_bps_per_settlement_vs_baseline"]["mean"] - 10


def test_non_comparable_or_thin_cross_sections_are_insufficient_not_extrapolated():
    rows = multi_rows(random.Random(3), 40, informative=True)
    assert run_xs({"BTCUSDT": rows["BTCUSDT"], "ETHUSDT": rows["ETHUSDT"]})["status"] == "INSUFFICIENT"          # two instruments
    assert run_xs(rows, baseline="DOGEUSDT")["status"] == "INSUFFICIENT"                                          # no baseline rows
    mixed = copy.deepcopy(rows)
    for r in mixed["SOLUSDT"]:
        r["environment"] = "testnet"
    assert "not comparable" in run_xs(mixed)["reason"]
    nocost = copy.deepcopy(rows)
    for r in nocost["XRPUSDT"]:
        for p in r["curve"]:
            p["status"] = "UNAVAILABLE"
    assert "no measured cost" in run_xs(nocost)["reason"]
    assert run_xs(multi_rows(random.Random(3), 40, informative=True, drop=("XRPUSDT", "SOLUSDT")))["status"] == "INSUFFICIENT"


def test_the_realised_value_is_the_public_print_and_the_result_says_so():
    assert "not account cash" in run_xs(multi_rows(random.Random(4), 60, informative=True))["reading"]


def test_instruments_that_simply_pay_more_on_average_show_an_edge_even_from_uninformative_forecasts():
    """An honest caution, pinned: a persistently higher-funding instrument beats the baseline whatever the forecast skill. That is
    a different (and riskier: basis, liquidity, borrow) claim from forecast skill, so the result reports the rank information
    coefficient beside the edge and a reader can tell them apart."""
    r = run_xs(multi_rows(random.Random(41), 80, informative=False, equal_means=False), k=1)
    assert r["net_edge_bps_per_settlement_vs_baseline"]["mean"] > 0
    assert abs(r["rank_information_coefficient"]["mean"]) < 0.4


def test_the_kernels_import_only_the_standard_library_and_the_pure_evidence_modules():
    import ast, os
    tree = ast.parse(open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "strategy_measure.py"), encoding="utf-8").read())
    got = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            got |= {a.name.split(".")[0] for a in n.names}
        elif isinstance(n, ast.ImportFrom):
            got.add((n.module or "").split(".")[0])
    assert got <= {"__future__", "math", "dataclasses", "typing", "evidence_stats", "funding_eval"}, got


def test_NULL_i_i_d_noise_basis_never_becomes_a_candidate_whatever_its_range():
    """The failure the third review found: filling at the signal snapshot booked the extreme itself, so pure noise showed +15 bps per
    episode with a lower bound of +11 bps in 12 of 12 seeds. With next-snapshot fills the episodes net about minus the cost."""
    import opportunity_engine as OE
    from test_opportunity_engine import good_ev, brows
    cands = 0
    nets = []
    for s in range(60):
        rng = random.Random(4000 + s)
        path = [rng.gauss(0, 10.0) for _ in range(20 * 288)]
        e = OE.evaluate_basis(OE.REGISTRY["basis_convergence/v1"], brows(path), round_trip_cost_bps=11.0, ev=good_ev())
        cands += e.status == OE.Status.RESEARCH_CANDIDATE
        r = SM.basis_episodes(brows(path), SM.BasisParams(16.5, 4.1, 86400.0, 11.0))
        if r["episodes"] >= 30:
            nets.append(r["net"]["mean"])
    assert cands == 0
    assert nets and sum(nets) / len(nets) < -8.0, sum(nets) / len(nets)        # about minus the 11 bps cost, not plus 15
