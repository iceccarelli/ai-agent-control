"""Tests for statistical_power: closed forms, simulation, null calibration, multiple testing, the trial
ledger, leakage-free splits and the pre-registered evaluation. Every random draw uses a fixed seed; every
simulation tolerance is several standard errors wide, so none of these is flaky."""
import ast
import dataclasses
import itertools
import math
import pathlib
import random
import statistics
from statistics import NormalDist

import pytest

import evidence_stats as ES
import statistical_power as sp

N = NormalDist()
SRC = pathlib.Path(sp.__file__)


# ---------------------------------------------------------------------------------------------
# helpers (independent of the module under test where it matters)
# ---------------------------------------------------------------------------------------------
def ar1(rng, n, rho, mean=0.0, sd=1.0):
    """Stationary Gaussian AR(1) with marginal sd ``sd``."""
    x = rng.gauss(0.0, 1.0)
    s = math.sqrt(1.0 - rho * rho)
    out = []
    for _ in range(n):
        x = rho * x + s * rng.gauss(0.0, 1.0)
        out.append(mean + sd * x)
    return out


def make_plan(n=200, **over):
    kw = dict(strategy_id="carry_v1", version="1.0.0", parameters={"entry_bps": 1.5, "lookback": [3, 6]},
              evaluation_window=(0, n), metric="mean", alpha=0.05, power_target=0.8, min_edge=0.25,
              n_hypotheses_declared=1, seed=20261105, adjust="holm")
    kw.update(over)
    return sp.EvaluationPlan(**kw)


def evaluate(plan, values, ledger=None, ts=None, B=40, digest=None, fn=sp.mean_statistic):
    led = ledger if ledger is not None else sp.TrialLedger()
    stamps = list(range(len(values))) if ts is None else list(ts)
    res = sp.evaluate_under_plan(plan, list(zip(stamps, values)), fn,
                                 expected_digest=digest or sp.plan_digest(plan), ledger=led, bootstrap_B=B)
    return res, led


def centred(rng, n, sd=1.0):
    xs = [rng.gauss(0.0, sd) for _ in range(n)]
    m = sum(xs) / n
    return [x - m for x in xs]


def z_upper(a):
    return -N.inv_cdf(a)


def closed_form_n(sd, edge, alpha, power, two_sided=False):
    return ((z_upper(alpha / 2 if two_sided else alpha) + N.inv_cdf(power)) * sd / edge) ** 2


META = sp.META_KEYS


def assert_labelled(result):
    cl = result["classes"]
    assert set(cl.values()) <= set(sp.CLASSES)
    assert set(cl) <= set(result), f"classes names keys the result does not have: {set(cl) - set(result)}"
    missing = [k for k in result if k not in META and k not in cl]
    assert not missing, f"unlabelled output keys: {missing}"


# ---------------------------------------------------------------------------------------------
# Student t and noncentral t
# ---------------------------------------------------------------------------------------------
@pytest.mark.parametrize("q,df,expected", [(0.025, 10, 2.228139), (0.05, 5, 2.015048), (0.05, 100, 1.660234),
                                           (0.025, 2, 4.302653), (0.005, 30, 2.750000), (0.0005, 20, 3.849520)])
def test_t_critical_values_match_published_tables(q, df, expected):
    assert abs(sp.student_t_isf(q, df) - expected) < 1.5e-5


def test_t_sf_and_isf_invert_each_other_and_are_symmetric():
    for df, t in itertools.product((2.0, 3.5, 10.0, 57.0, 1000.0), (-5.0, -1.0, 0.3, 2.0, 8.0)):
        q = sp.student_t_sf(t, df)
        assert 0.0 < q < 1.0
        assert abs(sp.student_t_isf(q, df) - t) < 1e-7 * max(1.0, abs(t))
        assert abs(sp.student_t_sf(-t, df) - (1.0 - q)) < 1e-12
    assert sp.student_t_sf(0.0, 7) == 0.5
    assert 0.0 < sp.student_t_sf(60.0, 5) < 1e-6          # a far tail is small, not zero


def test_t_approaches_the_normal_and_agrees_with_the_evidence_stats_expansion():
    assert abs(sp.student_t_sf(1.96, 1e7) - 0.025) < 1e-4
    assert abs(sp.student_t_isf(0.025, 30) - ES._t_crit(30, 0.05)) < 1e-3
    assert abs(sp.student_t_isf(0.025, 200) - ES._t_crit(200, 0.05)) < 1e-4


def test_noncentral_t_with_zero_noncentrality_is_the_central_t_and_grows_with_delta():
    for df in (2, 5, 30, 200):
        tc = sp.student_t_isf(0.05, df)
        assert abs(sp.noncentral_t_sf(tc, df, 0.0) - 0.05) < 1e-7
    vals = [sp.noncentral_t_sf(1.7, 25, d) for d in (0.0, 1.0, 2.0, 3.0, 4.0)]
    assert vals == sorted(vals) and vals[-1] > 0.95


def test_noncentral_t_matches_the_abramowitz_stegun_approximation():
    def as_cdf(t, df, d):
        return N.cdf((t * (1 - 1 / (4 * df)) - d) / math.sqrt(1 + t * t / (2 * df)))
    for df, d in ((20, 2.5), (60, 3.0), (100, 2.0)):
        tc = sp.student_t_isf(0.05, df)
        assert abs((1 - sp.noncentral_t_sf(tc, df, d)) - as_cdf(tc, df, d)) < 0.01


# ---------------------------------------------------------------------------------------------
# design effect
# ---------------------------------------------------------------------------------------------
def test_design_effect_closed_forms():
    assert sp.design_effect(rho=0.0)["design_effect"] == 1.0
    assert sp.design_effect(rho=0.5)["design_effect"] == pytest.approx(3.0)
    assert sp.design_effect(rho=0.9)["design_effect"] == pytest.approx(19.0)
    assert sp.design_effect(ess_ratio=0.25)["design_effect"] == pytest.approx(4.0)
    assert sp.design_effect(rho_sum=0.5)["design_effect"] == pytest.approx(2.0)
    assert sp.design_effect(rho_k=[0.5, 0.25, 0.125])["design_effect"] == pytest.approx(2.75)


def test_every_spelling_of_the_same_dependence_gives_the_same_design_effect():
    a = sp.design_effect(rho=0.5)["design_effect"]
    assert sp.design_effect(ess_ratio=1 / 3)["design_effect"] == pytest.approx(a)
    assert sp.design_effect(rho_sum=1.0)["design_effect"] == pytest.approx(a)
    assert sp.design_effect(rho_k=[0.5 ** k for k in range(1, 200)])["design_effect"] == pytest.approx(a, rel=1e-9)


def test_design_effect_requires_exactly_one_stated_dependence():
    with pytest.raises(sp.UnknownInputError):
        sp.design_effect()
    with pytest.raises(sp.StatisticalPowerError):
        sp.design_effect(rho=0.3, ess_ratio=0.5)


@pytest.mark.parametrize("kw", [dict(rho=1.0), dict(rho=-1.0), dict(rho=float("nan")), dict(rho=True),
                                dict(rho="0.5"), dict(ess_ratio=0.0), dict(ess_ratio=1.2), dict(ess_ratio=-0.1),
                                dict(rho_sum=float("inf")), dict(rho_k=[2.0]), dict(rho_k="abc")])
def test_design_effect_rejects_nonsense(kw):
    with pytest.raises(sp.StatisticalPowerError):
        sp.design_effect(**kw)


def test_empty_autocorrelation_list_is_unknown_not_independent():
    with pytest.raises(sp.UnknownInputError):
        sp.design_effect(rho_k=[])


def test_negative_dependence_is_floored_at_one_and_says_so():
    r = sp.design_effect(rho=-0.5)
    assert r["design_effect"] == 1.0 and r["floored_at_one"] is True
    assert "floored" in r["reading"]
    assert sp.design_effect(rho=0.5)["floored_at_one"] is False


# ---------------------------------------------------------------------------------------------
# required_observations: closed forms
# ---------------------------------------------------------------------------------------------
def req(**kw):
    base = dict(sd=1.0, min_edge=0.25, alpha=0.05, power=0.8, n_hypotheses=1, rho=0.0)
    base.update(kw)
    sd, edge = base.pop("sd"), base.pop("min_edge")
    return sp.required_observations(sd, edge, **base)


def test_one_sided_closed_form_gives_99_effective_observations():
    r = req()
    expected = closed_form_n(1.0, 0.25, 0.05, 0.8)
    assert expected == pytest.approx(98.92, abs=0.01)
    assert r["required_effective_n_exact"] == pytest.approx(expected, rel=1e-12)
    assert r["required_effective_n"] == 99 and r["required_raw_n"] == 99
    assert r["alpha_used"] == 0.05 and r["status"] == "OK"
    assert "99" in r["reading"] and "effective" in r["reading"]


def test_two_sided_needs_more_than_one_sided():
    r2 = req(two_sided=True)
    assert r2["required_effective_n"] == 126
    assert r2["required_effective_n_exact"] == pytest.approx(closed_form_n(1, 0.25, 0.05, 0.8, True), rel=1e-4)
    assert r2["required_effective_n"] > req()["required_effective_n"]


def test_ar1_dependence_inflates_raw_n_by_one_plus_rho_over_one_minus_rho():
    r = req(rho=0.5)
    assert r["required_effective_n"] == 99
    assert r["design_effect"] == pytest.approx(3.0)
    assert r["required_raw_n"] == math.ceil(closed_form_n(1, 0.25, 0.05, 0.8) * 3 - 1e-7) == 297
    assert req(rho=0.9)["required_raw_n"] == math.ceil(closed_form_n(1, 0.25, 0.05, 0.8) * 19 - 1e-7)


def test_a_supplied_ess_ratio_scales_raw_n_and_never_goes_below_effective_n():
    r = req(rho=None, ess_ratio=0.25)
    assert r["required_raw_n"] == 396 and r["required_effective_n"] == 99
    assert r["required_raw_n"] * 0.25 >= r["required_effective_n_exact"]
    assert req(rho=None, ess_ratio=1.0)["required_raw_n"] == req()["required_raw_n"]


def test_required_n_scales_with_sd_squared_and_inverse_edge_squared():
    base = req()["required_effective_n_exact"]
    assert req(sd=2.0)["required_effective_n_exact"] == pytest.approx(4 * base, rel=1e-12)
    assert req(min_edge=0.5)["required_effective_n_exact"] == pytest.approx(base / 4, rel=1e-12)


def test_required_n_is_monotone_in_power_hypotheses_and_alpha():
    ns = [req(power=p)["required_effective_n_exact"] for p in (0.5, 0.7, 0.8, 0.9, 0.99)]
    assert ns == sorted(ns) and len(set(ns)) == 5
    ms = [req(n_hypotheses=m)["required_effective_n_exact"] for m in (1, 2, 10, 100, 1000)]
    assert ms == sorted(ms) and len(set(ms)) == 5
    al = [req(alpha=a)["required_effective_n_exact"] for a in (0.2, 0.1, 0.05, 0.01, 0.001)]
    assert al == sorted(al) and len(set(al)) == 5


def test_bonferroni_and_holm_size_at_alpha_over_m_and_none_ignores_the_count():
    r = req(n_hypotheses=10, adjust="bonferroni")
    assert r["alpha_used"] == pytest.approx(0.005)
    assert r["required_effective_n_exact"] == pytest.approx(closed_form_n(1, 0.25, 0.005, 0.8), rel=1e-12)
    assert r["required_effective_n"] == 187
    h = req(n_hypotheses=10, adjust="holm")
    assert h["alpha_used"] == pytest.approx(0.005) and h["required_effective_n"] == 187
    n = req(n_hypotheses=10, adjust="none")
    assert n["alpha_used"] == 0.05 and n["required_effective_n"] == 99


def test_bh_sizing_needs_the_expected_number_of_true_edges_and_never_defaults_it():
    with pytest.raises(sp.UnknownInputError):
        req(n_hypotheses=10, adjust="bh")
    one = req(n_hypotheses=10, adjust="bh", bh_n_true=1)
    assert one["alpha_used"] == pytest.approx(0.005)
    three = req(n_hypotheses=10, adjust="bh", bh_n_true=3)
    assert three["alpha_used"] == pytest.approx(0.015)
    assert three["required_effective_n_exact"] < one["required_effective_n_exact"]
    with pytest.raises(sp.StatisticalPowerError):
        req(n_hypotheses=10, adjust="bh", bh_n_true=11)


def test_t_method_matches_published_one_sample_t_test_sample_sizes():
    # Cohen's d = 0.5, alpha .05, power .8: n = 27 one-sided and 34 two-sided (G*Power / Cohen tables).
    assert req(min_edge=0.5, method="t")["required_effective_n"] == 27
    assert req(min_edge=0.5, method="t", two_sided=True)["required_effective_n"] == 34
    t = req(method="t")
    assert 100 <= t["required_effective_n"] <= 102
    assert t["required_effective_n_exact"] > req()["required_effective_n_exact"]      # the t test costs more than the z test


def test_required_n_is_the_smallest_n_that_reaches_the_target_power():
    for method, two_sided, m in itertools.product(("normal", "t"), (False, True), (1, 20)):
        r = req(method=method, two_sided=two_sided, n_hypotheses=m)
        n = r["required_effective_n"]
        kw = dict(n_kind="effective", alpha=0.05, n_hypotheses=m, two_sided=two_sided, method=method)
        assert sp.achieved_power(n, 1.0, 0.25, **kw)["power"] >= 0.8 - 1e-9
        assert sp.achieved_power(n - 1, 1.0, 0.25, **kw)["power"] < 0.8


def test_an_unreachable_requirement_is_insufficient_with_a_reason_not_a_huge_number():
    r = req(min_edge=1e-9)
    assert r["status"] == "INSUFFICIENT" and "not detectable" in r["reason"]
    assert "required_effective_n" not in r
    assert_labelled(r)


@pytest.mark.parametrize("kw", [dict(sd=None), dict(min_edge=None), dict(alpha=None), dict(power=None),
                                dict(n_hypotheses=None), dict(rho=None)])
def test_unknown_inputs_are_refused_never_defaulted(kw):
    with pytest.raises(sp.UnknownInputError):
        req(**kw)


def test_omitted_keyword_inputs_are_a_type_error():
    with pytest.raises(TypeError):
        sp.required_observations(1.0, 0.25, power=0.8, n_hypotheses=1, rho=0.0)            # no alpha
    with pytest.raises(TypeError):
        sp.required_observations(1.0, 0.25, alpha=0.05, power=0.8, rho=0.0)               # no n_hypotheses


@pytest.mark.parametrize("kw", [dict(sd=0.0), dict(sd=-1.0), dict(sd=float("nan")), dict(min_edge=0.0),
                                dict(min_edge=-0.1), dict(alpha=0.0), dict(alpha=1.0), dict(power=1.0),
                                dict(power=0.0), dict(n_hypotheses=0), dict(n_hypotheses=2.5), dict(n_hypotheses=True),
                                dict(adjust="sidak"), dict(method="exact"), dict(sd=True), dict(power=0.04)])
def test_invalid_values_are_refused(kw):
    with pytest.raises(sp.StatisticalPowerError):
        req(**kw)


# ---------------------------------------------------------------------------------------------
# achieved power and minimum detectable edge
# ---------------------------------------------------------------------------------------------
def test_achieved_power_at_the_closed_form_n_is_the_target():
    n = closed_form_n(1, 0.25, 0.05, 0.8)
    p = sp.achieved_power(n, 1.0, 0.25, n_kind="effective", alpha=0.05, n_hypotheses=1)["power"]
    assert p == pytest.approx(0.8, abs=1e-9)


def test_raw_and_effective_n_are_consistent_through_the_design_effect():
    eff = sp.achieved_power(99, 1.0, 0.25, n_kind="effective", alpha=0.05, n_hypotheses=1)
    raw = sp.achieved_power(297, 1.0, 0.25, n_kind="raw", rho=0.5, alpha=0.05, n_hypotheses=1)
    assert raw["power"] == pytest.approx(eff["power"], rel=1e-12)
    assert raw["n_effective"] == pytest.approx(99.0) and raw["design_effect"] == pytest.approx(3.0)
    iid = sp.achieved_power(297, 1.0, 0.25, n_kind="raw", rho=0.0, alpha=0.05, n_hypotheses=1)
    assert iid["power"] > raw["power"]


def test_effective_n_with_a_dependence_argument_is_contradictory():
    with pytest.raises(sp.StatisticalPowerError):
        sp.achieved_power(99, 1.0, 0.25, n_kind="effective", rho=0.5, alpha=0.05, n_hypotheses=1)
    with pytest.raises(sp.UnknownInputError):
        sp.achieved_power(99, 1.0, 0.25, n_kind="raw", alpha=0.05, n_hypotheses=1)           # raw needs the dependence
    with pytest.raises(sp.UnknownInputError):
        sp.achieved_power(99, 1.0, 0.25, n_kind=None, alpha=0.05, n_hypotheses=1)


def test_power_at_a_vanishing_edge_is_the_adjusted_size_and_at_a_huge_edge_is_one():
    for method in ("normal", "t"):
        for two_sided in (False, True):
            kw = dict(n_kind="effective", alpha=0.05, n_hypotheses=5, two_sided=two_sided, method=method)
            tiny = sp.achieved_power(80, 1.0, 1e-9, **kw)
            assert tiny["power"] == pytest.approx(0.01, abs=1e-6)
            assert sp.achieved_power(80, 1.0, 10.0, **kw)["power"] > 0.999999


def test_power_is_monotone_in_n_edge_and_inversely_in_hypotheses():
    kw = dict(n_kind="effective", alpha=0.05, n_hypotheses=1)
    by_n = [sp.achieved_power(n, 1, 0.25, **kw)["power"] for n in (20, 50, 100, 200)]
    by_e = [sp.achieved_power(100, 1, e, **kw)["power"] for e in (0.05, 0.1, 0.2, 0.4)]
    by_m = [sp.achieved_power(100, 1, 0.25, n_kind="effective", alpha=0.05, n_hypotheses=m)["power"] for m in (1, 5, 50)]
    assert by_n == sorted(by_n) and by_e == sorted(by_e) and by_m == sorted(by_m, reverse=True)


def test_t_power_is_below_normal_power_and_t_needs_three_effective_observations():
    kw = dict(n_kind="effective", alpha=0.05, n_hypotheses=1)
    assert sp.achieved_power(15, 1, 0.5, method="t", **kw)["power"] < sp.achieved_power(15, 1, 0.5, **kw)["power"]
    r = sp.achieved_power(2.0, 1, 0.5, method="t", **kw)
    assert r["status"] == "INSUFFICIENT" and r["power"] is None and "degrees of freedom" in r["reason"]
    assert_labelled(r)


def test_minimum_detectable_edge_is_the_closed_form_and_inverts_required_observations():
    m = sp.minimum_detectable_edge(99, 1.0, n_kind="effective", alpha=0.05, power=0.8, n_hypotheses=1)
    assert m["minimum_detectable_edge"] == pytest.approx((z_upper(0.05) + N.inv_cdf(0.8)) / math.sqrt(99), rel=1e-12)
    assert m["minimum_detectable_edge"] == pytest.approx(0.2499, abs=1e-3)
    for method, two_sided, adjust in itertools.product(("normal", "t"), (False, True), ("none", "holm")):
        n = 130
        e = sp.minimum_detectable_edge(n, 1.7, n_kind="effective", alpha=0.05, power=0.9, n_hypotheses=7,
                                       adjust=adjust, two_sided=two_sided, method=method)["minimum_detectable_edge"]
        back = sp.achieved_power(n, 1.7, e, n_kind="effective", alpha=0.05, n_hypotheses=7, adjust=adjust,
                                 two_sided=two_sided, method=method)["power"]
        assert back == pytest.approx(0.9, abs=1e-7)
        r = sp.required_observations(1.7, e, alpha=0.05, power=0.9, n_hypotheses=7, adjust=adjust, rho=0.0,
                                     two_sided=two_sided, method=method)
        assert r["required_effective_n_exact"] == pytest.approx(n, rel=1e-6)


def test_minimum_detectable_edge_grows_with_dependence_and_with_the_trial_count():
    base = sp.minimum_detectable_edge(300, 1.0, n_kind="raw", rho=0.0, alpha=0.05, power=0.8, n_hypotheses=1)
    dep = sp.minimum_detectable_edge(300, 1.0, n_kind="raw", rho=0.5, alpha=0.05, power=0.8, n_hypotheses=1)
    many = sp.minimum_detectable_edge(300, 1.0, n_kind="raw", rho=0.0, alpha=0.05, power=0.8, n_hypotheses=50)
    assert dep["minimum_detectable_edge"] == pytest.approx(base["minimum_detectable_edge"] * math.sqrt(3), rel=1e-9)
    assert many["minimum_detectable_edge"] > base["minimum_detectable_edge"]
    assert "at least" in many["reading"]


# ---------------------------------------------------------------------------------------------
# simulation against the formulas
# ---------------------------------------------------------------------------------------------
def test_simulated_power_matches_the_formula_for_iid_data():
    rng = random.Random(101)
    n, reps, edge = 99, 4000, 0.25
    crit = z_upper(0.05)
    hits = sum(1 for _ in range(reps) if sum(rng.gauss(edge, 1.0) for _ in range(n)) / math.sqrt(n) > crit)
    formula = sp.achieved_power(n, 1.0, edge, n_kind="effective", alpha=0.05, n_hypotheses=1)["power"]
    assert formula == pytest.approx(0.80, abs=0.005)
    assert abs(hits / reps - formula) < 0.025


def test_simulated_power_matches_the_formula_for_ar1_data():
    rng = random.Random(102)
    n, reps, rho, edge = 297, 2500, 0.5, 0.25
    deff = (1 + rho) / (1 - rho)
    crit = z_upper(0.05)
    hits = 0
    for _ in range(reps):
        xs = ar1(rng, n, rho, mean=edge)
        z = (sum(xs) / n) / math.sqrt(deff / n)
        hits += z > crit
    formula = sp.achieved_power(n, 1.0, edge, n_kind="raw", rho=rho, alpha=0.05, n_hypotheses=1)["power"]
    assert formula == pytest.approx(0.80, abs=0.005)
    assert abs(hits / reps - formula) < 0.035


def test_simulated_power_matches_the_exact_t_power_with_an_estimated_sd():
    rng = random.Random(103)
    n, reps, edge = 27, 4000, 0.5
    tc = sp.student_t_isf(0.05, n - 1)
    hits = 0
    for _ in range(reps):
        xs = [rng.gauss(edge, 1.0) for _ in range(n)]
        m = sum(xs) / n
        s = math.sqrt(sum((x - m) ** 2 for x in xs) / (n - 1))
        hits += m / (s / math.sqrt(n)) > tc
    formula = sp.achieved_power(n, 1.0, edge, n_kind="effective", alpha=0.05, n_hypotheses=1, method="t")["power"]
    assert abs(formula - 0.8) < 0.02
    assert abs(hits / reps - formula) < 0.025
    assert sp.achieved_power(n, 1.0, edge, n_kind="effective", alpha=0.05, n_hypotheses=1)["power"] > formula + 0.005


def test_simulated_two_sided_power_with_bonferroni_matches_the_formula():
    rng = random.Random(104)
    sd, n, reps, m = 2.0, 60, 4000, 5
    mde = sp.minimum_detectable_edge(n, sd, n_kind="effective", alpha=0.05, power=0.9, n_hypotheses=m,
                                     adjust="bonferroni", two_sided=True)["minimum_detectable_edge"]
    crit = z_upper(0.05 / m / 2)
    hits = 0
    for _ in range(reps):
        z = sum(rng.gauss(mde, sd) for _ in range(n)) / (sd * math.sqrt(n))
        hits += abs(z) > crit
    assert abs(hits / reps - 0.9) < 0.02


def test_simulated_false_positive_rate_equals_the_adjusted_alpha_under_the_null():
    rng = random.Random(105)
    n, reps = 50, 6000
    crit = z_upper(0.05)
    fp = sum(1 for _ in range(reps) if sum(rng.gauss(0, 1) for _ in range(n)) / math.sqrt(n) > crit)
    assert abs(fp / reps - 0.05) < 0.012


# ---------------------------------------------------------------------------------------------
# multiple testing
# ---------------------------------------------------------------------------------------------
BH_1995 = [0.0001, 0.0004, 0.0019, 0.0095, 0.0201, 0.0278, 0.0298, 0.0344, 0.0459, 0.3240, 0.4262, 0.5719,
           0.6528, 0.7590, 1.000]


def test_bonferroni_holm_and_bh_on_the_benjamini_hochberg_1995_example():
    bon = sp.adjust_pvalues(BH_1995, "bonferroni", alpha=0.05)
    holm = sp.adjust_pvalues(BH_1995, "holm", alpha=0.05)
    bh = sp.adjust_pvalues(BH_1995, "bh", alpha=0.05)
    assert bon["n_rejected"] == 3 and holm["n_rejected"] == 3 and bh["n_rejected"] == 4
    assert bon["reject"][:4] == [True, True, True, False] and bh["reject"][:5] == [True] * 4 + [False]
    # BH adjusted values worked out by hand: min over j >= i of 15 * p_j / j
    hand = [0.0015, 0.003, 0.0095, 0.035625, 0.0603, 0.063857, 0.063857, 0.064500, 0.0765, 0.486, 0.5812, 0.7149,
            0.7532, 0.8132, 1.0]
    assert bh["adjusted"] == pytest.approx(hand, rel=1e-3)
    assert bon["adjusted"][:4] == pytest.approx([0.0015, 0.006, 0.0285, 0.1425])


def test_holm_textbook_example_with_unsorted_input():
    p = [0.01, 0.04, 0.03, 0.005]
    r = sp.adjust_pvalues(p, "holm", alpha=0.05)
    assert r["adjusted"] == pytest.approx([0.03, 0.06, 0.06, 0.02])         # sorted .005,.01,.03,.04 -> x4,x3,x2,x1 then cummax
    assert r["reject"] == [True, False, False, True]
    b = sp.adjust_pvalues(p, "bh", alpha=0.05)
    assert b["adjusted"] == pytest.approx([0.02, 0.04, 0.04, 0.02])
    assert b["reject"] == [True, True, True, True]
    assert sp.adjust_pvalues(p, "bonferroni", alpha=0.05)["adjusted"] == pytest.approx([0.04, 0.16, 0.12, 0.02])
    assert sp.adjust_pvalues(p, "none", alpha=0.05)["adjusted"] == p


def test_adjusted_decisions_equal_the_original_step_procedures():
    rng = random.Random(7)
    for _ in range(300):
        m = rng.randint(1, 25)
        ps = [rng.random() ** rng.choice((1, 2, 4)) for _ in range(m)]
        alpha = rng.choice((0.01, 0.05, 0.1))
        # Holm step-down: compare p_(i) with alpha / (m - i + 1) until the first failure
        order = sorted(range(m), key=lambda i: ps[i])
        holm_rej = [False] * m
        for rank, i in enumerate(order):
            if ps[i] <= alpha / (m - rank):
                holm_rej[i] = True
            else:
                break
        # BH step-up: largest k with p_(k) <= k alpha / m; reject everything up to it
        k_max = max([k + 1 for k, i in enumerate(order) if ps[i] <= (k + 1) * alpha / m] or [0])
        bh_rej = [False] * m
        for i in order[:k_max]:
            bh_rej[i] = True
        assert sp.adjust_pvalues(ps, "holm", alpha=alpha)["reject"] == holm_rej
        assert sp.adjust_pvalues(ps, "bh", alpha=alpha)["reject"] == bh_rej
        assert sp.adjust_pvalues(ps, "bonferroni", alpha=alpha)["reject"] == [p <= alpha / m for p in ps]


def test_adjustment_ordering_bounds_and_monotonicity():
    rng = random.Random(8)
    for _ in range(100):
        ps = [rng.random() ** 3 for _ in range(rng.randint(2, 30))]
        bon = sp.adjust_pvalues(ps, "bonferroni", alpha=0.05)["adjusted"]
        holm = sp.adjust_pvalues(ps, "holm", alpha=0.05)["adjusted"]
        bh = sp.adjust_pvalues(ps, "bh", alpha=0.05)["adjusted"]
        for p, a, b, c in zip(ps, bon, holm, bh):
            assert p <= c + 1e-15 and c <= b + 1e-12 and b <= a + 1e-12 and a <= 1.0
        by_p = sorted(range(len(ps)), key=lambda i: ps[i])
        for adj in (holm, bh):                      # a smaller p never gets a larger adjusted p
            assert all(adj[i] <= adj[j] + 1e-15 for i, j in zip(by_p, by_p[1:]))


def test_n_tested_counts_the_unreported_trials_as_p_equal_one():
    winners = [0.001, 0.02]
    full = winners + [1.0] * 48
    for method in ("bonferroni", "holm", "bh"):
        padded = sp.adjust_pvalues(winners, method, alpha=0.05, n_tested=50)
        complete = sp.adjust_pvalues(full, method, alpha=0.05)
        assert padded["adjusted"] == pytest.approx(complete["adjusted"][:2])
        assert padded["n_tested"] == 50 and padded["n_padded_with_p1"] == 48
    only_winner = sp.adjust_pvalues([0.001, 0.02], "holm", alpha=0.05)
    assert only_winner["adjusted"][0] == pytest.approx(0.002) and only_winner["reject"][0]
    assert sp.adjust_pvalues([0.001, 0.02], "holm", alpha=0.05, n_tested=50)["adjusted"][0] == pytest.approx(0.05)
    with pytest.raises(sp.StatisticalPowerError):
        sp.adjust_pvalues([0.1, 0.2, 0.3], "holm", alpha=0.05, n_tested=2)


@pytest.mark.parametrize("bad", [[], None, [float("nan")], [-0.1], [1.1], ["0.1"], [True], "0.05", [0.1, None]])
def test_adjust_pvalues_refuses_malformed_p_values(bad):
    with pytest.raises(sp.StatisticalPowerError):
        sp.adjust_pvalues(bad, "holm", alpha=0.05)


def test_adjust_pvalues_refuses_unknown_method_and_missing_alpha():
    with pytest.raises(sp.StatisticalPowerError):
        sp.adjust_pvalues([0.1], "sidak", alpha=0.05)
    with pytest.raises(TypeError):
        sp.adjust_pvalues([0.1], "holm")
    with pytest.raises(sp.UnknownInputError):
        sp.adjust_pvalues([0.1], "holm", alpha=None)


def test_holm_controls_the_family_wise_error_and_still_finds_real_effects():
    rng = random.Random(9)
    reps, m = 3000, 20
    any_rej = sum(1 for _ in range(reps)
                  if any(sp.adjust_pvalues([rng.random() for _ in range(m)], "holm", alpha=0.05)["reject"]))
    assert any_rej / reps < 0.05 + 0.015
    found = 0
    for _ in range(300):                                  # 19 nulls plus one true effect with z ~ N(5, 1)
        ps = [rng.random() for _ in range(m - 1)] + [1 - N.cdf(rng.gauss(5.0, 1.0))]
        found += sp.adjust_pvalues(ps, "holm", alpha=0.05)["reject"][-1]
    assert found / 300 > 0.7


def test_bh_controls_the_false_discovery_rate_and_has_power():
    rng = random.Random(10)
    q, reps, m0, m1 = 0.1, 400, 80, 20
    fdp_sum = true_found = 0.0
    for _ in range(reps):
        ps = [rng.random() for _ in range(m0)] + [1 - N.cdf(rng.gauss(3.0, 1.0)) for _ in range(m1)]
        rej = sp.adjust_pvalues(ps, "bh", alpha=q)["reject"]
        r = sum(rej)
        v = sum(rej[:m0])
        fdp_sum += (v / r) if r else 0.0
        true_found += sum(rej[m0:])
    assert fdp_sum / reps < q * m0 / (m0 + m1) + 0.02
    assert true_found / reps / m1 > 0.4


# ---------------------------------------------------------------------------------------------
# the trial ledger
# ---------------------------------------------------------------------------------------------
def filled(n=5, p=0.5):
    led = sp.TrialLedger()
    for i in range(n):
        led.record("s", f"h{i}", "w1", float(i), pvalue=p, n_obs=100)
    return led


def test_ledger_counts_every_record_including_repeats_and_losers():
    led = sp.TrialLedger()
    assert led.n_trials == 0 and len(led) == 0
    led.record("s1", "a", "w1", 0.1, pvalue=0.4)
    led.record("s1", "a", "w1", 0.1, pvalue=0.4)            # the same look again is another trial
    led.record("s1", "b", "w1", -0.3)                       # no p-value: still examined
    led.record("s2", "a", "w2", 9.0, pvalue=0.001)
    assert led.n_trials == 4 and len(led) == 4 and led.n_distinct == 3
    assert [t.index for t in led.trials] == [0, 1, 2, 3]
    assert led.pvalues() == [0.4, 0.4, 1.0, 0.001]          # an uncomputed p-value counts as 1, never as significant


@pytest.mark.parametrize("op", [lambda l: l.clear(), lambda l: l.pop(), lambda l: l.remove(l[0]),
                                lambda l: l.__delitem__(0), lambda l: l.__setitem__(0, l[1]),
                                lambda l: l.append(l[0]), lambda l: l.extend([l[0]]), lambda l: l.insert(0, l[0]),
                                lambda l: l.sort(), lambda l: l.reverse()])
def test_ledger_has_no_way_to_delete_or_rewrite(op):
    led = filled()
    with pytest.raises(sp.LedgerError):
        op(led)
    assert led.n_trials == 5


def test_ledger_exposes_immutable_trials_and_a_defensive_copy():
    led = filled(3)
    trials = led.trials
    assert isinstance(trials, tuple)
    with pytest.raises(dataclasses.FrozenInstanceError):
        trials[0].statistic = 99.0
    assert led[0].statistic == 0.0 and led.n_trials == 3


@pytest.mark.parametrize("cut", [lambda e: e.pop(), lambda e: e.pop(0), lambda e: e.pop(2), lambda e: e.__delitem__(slice(1, 3)),
                                 lambda e: e.clear()])
def test_a_silently_shortened_ledger_is_detected(cut):
    led = filled()
    cut(led._entries)                                      # reaching under the API
    with pytest.raises(sp.LedgerTamperError):
        led.n_trials
    with pytest.raises(sp.LedgerTamperError):
        led.verify()
    with pytest.raises(sp.LedgerTamperError):
        led.record("s", "x", "w", 1.0)
    with pytest.raises(sp.LedgerTamperError):
        led.trials


def test_an_edited_entry_is_detected_by_the_hash_chain():
    led = filled()
    t = led._entries[2]
    led._entries[2] = dataclasses.replace(t, statistic=123.0)
    with pytest.raises(sp.LedgerTamperError):
        led.verify()
    led2 = filled()
    led2._entries[1], led2._entries[2] = led2._entries[2], led2._entries[1]
    with pytest.raises(sp.LedgerTamperError):
        led2.verify()


def test_an_anchor_catches_a_ledger_rebuilt_shorter_with_consistent_count_and_head():
    led = filled(5)
    anchor = led.anchor()
    assert led.verify(anchor) and anchor.n == 5
    short = filled(3)                                       # a forger rebuilds a consistent, shorter ledger
    assert short.verify()                                   # internally consistent ...
    with pytest.raises(sp.LedgerTamperError, match="shortened"):
        short.verify(anchor)                                # ... but not an extension of what was anchored
    longer = filled(5)
    longer.record("s", "late", "w1", 7.0)
    assert longer.verify(anchor)                            # appending is fine
    other = sp.TrialLedger()
    for i in range(5):
        other.record("s", f"different{i}", "w1", float(i))
    with pytest.raises(sp.LedgerTamperError):
        other.verify(anchor)


def test_the_chain_is_deterministic_and_order_sensitive():
    assert filled(4).head == filled(4).head
    a, b = sp.TrialLedger(), sp.TrialLedger()
    a.record("s", "x", "w", 1.0)
    a.record("s", "y", "w", 2.0)
    b.record("s", "y", "w", 2.0)
    b.record("s", "x", "w", 1.0)
    assert a.head != b.head


@pytest.mark.parametrize("kw", [dict(strategy_id=""), dict(params_hash=3), dict(window_id=None),
                                dict(statistic=float("nan")), dict(statistic=True), dict(pvalue=1.5),
                                dict(pvalue=-0.1), dict(pvalue=float("nan")), dict(n_obs=-1), dict(n_obs=2.5)])
def test_ledger_refuses_malformed_records_and_stays_unchanged(kw):
    led = filled(2)
    args = dict(strategy_id="s", params_hash="h", window_id="w", statistic=1.0)
    args.update(kw)
    with pytest.raises(sp.StatisticalPowerError):
        led.record(**args)
    assert led.n_trials == 2


# ---------------------------------------------------------------------------------------------
# selection among many trials
# ---------------------------------------------------------------------------------------------
def null_ledger(n, seed, extra=None):
    rng = random.Random(seed)
    led = sp.TrialLedger()
    for i in range(n):
        z = rng.gauss(0, 1)
        led.record("grid", f"p{i}", "w", z, pvalue=1 - N.cdf(z), n_obs=250)
    if extra is not None:
        led.record("planted", "pX", "w", extra[0], pvalue=extra[1], n_obs=250)
    return led


def test_the_best_of_fifty_noise_trials_does_not_survive_adjustment():
    led = null_ledger(50, seed=3)
    r = sp.selection_adjusted_best(led, alpha=0.05)
    assert r["status"] == "OK" and r["n_trials"] == 50
    raw = r["best_pvalue_unadjusted"]
    assert raw == min(t.pvalue for t in led.trials)
    assert r["best_pvalue_adjusted"] == pytest.approx(min(1.0, 50 * raw))          # Holm rank 1 over the whole family
    assert r["best_pvalue_adjusted"] > raw
    assert r["survives_adjustment"] is False
    assert r["expected_max_null_z"] == pytest.approx(sp.expected_max_null(50)) and r["expected_max_null_z"] > 2.0
    assert_labelled(r)


def test_adjustment_uses_all_trials_not_only_those_with_a_p_value():
    led = null_ledger(10, seed=4)
    for i in range(40):
        led.record("grid", f"unscored{i}", "w", 0.0)                   # examined, p-value never computed
    r = sp.selection_adjusted_best(led, alpha=0.05)
    assert r["n_trials"] == 50 and r["n_with_pvalue"] == 10
    assert r["best_pvalue_adjusted"] == pytest.approx(min(1.0, 50 * r["best_pvalue_unadjusted"]))


def test_a_planted_edge_survives_the_same_adjustment_and_beats_best_of_noise():
    led = null_ledger(49, seed=5, extra=(6.0, 1e-8))
    r = sp.selection_adjusted_best(led, alpha=0.05)
    assert r["best"]["strategy_id"] == "planted"
    assert r["best_pvalue_adjusted"] == pytest.approx(50e-8)
    assert r["survives_adjustment"] is True and r["best_beats_noise_max"] is True
    assert r["z_best"] > r["expected_max_null_z"]


def test_selection_adjusted_best_needs_a_real_ledger_and_reports_the_empty_and_unscored_cases():
    with pytest.raises(sp.StatisticalPowerError):
        sp.selection_adjusted_best([0.01, 0.02], alpha=0.05)
    assert sp.selection_adjusted_best(sp.TrialLedger(), alpha=0.05)["status"] == "INSUFFICIENT"
    led = sp.TrialLedger()
    led.record("s", "a", "w", 1.0)
    r = sp.selection_adjusted_best(led, alpha=0.05)
    assert r["status"] == "INSUFFICIENT" and "p-value" in r["reason"]
    assert_labelled(r)


def test_selection_detects_a_ledger_that_was_shortened():
    led = null_ledger(20, seed=6)
    del led._entries[:5]
    with pytest.raises(sp.LedgerTamperError):
        sp.selection_adjusted_best(led, alpha=0.05)


def test_expected_max_null_matches_exact_order_statistics_and_simulation():
    assert sp.expected_max_null(1) == 0.0
    exact = {2: 0.5641895835, 10: 1.5387527308, 100: 2.5075936364, 1000: 3.2414357691}
    for n, v in exact.items():
        assert sp.expected_max_null(n, method="exact") == pytest.approx(v, abs=1e-6)
        assert abs(sp.expected_max_null(n) - v) < 0.045
    assert sp.expected_max_null(10, null_sd=2.0) == pytest.approx(2 * sp.expected_max_null(10))
    rng = random.Random(11)
    sim = statistics.mean(max(rng.gauss(0, 1) for _ in range(20)) for _ in range(4000))
    assert abs(sim - sp.expected_max_null(20, method="exact")) < 0.03
    vals = [sp.expected_max_null(n) for n in (2, 5, 20, 100, 1000, 100000)]
    assert vals == sorted(vals)


def test_deflated_sharpe_matches_the_bailey_lopez_de_prado_formula():
    sr, t, n, var, g3, g4 = 0.12, 250, 40, 0.004, -0.3, 4.5
    r = sp.deflated_sharpe(sr, n_obs=t, n_trials=n, sharpe_variance=var, skew=g3, kurtosis=g4)
    gam = 0.5772156649015329
    sr0 = math.sqrt(var) * ((1 - gam) * N.inv_cdf(1 - 1 / n) + gam * N.inv_cdf(1 - 1 / (n * math.e)))
    expected = N.cdf((sr - sr0) * math.sqrt(t - 1) / math.sqrt(1 - g3 * sr + (g4 - 1) / 4 * sr * sr))
    assert r["sr0"] == pytest.approx(sr0, rel=1e-12) and r["dsr"] == pytest.approx(expected, rel=1e-12)
    at_luck = sp.deflated_sharpe(sr0, n_obs=t, n_trials=n, sharpe_variance=var, skew=0.0, kurtosis=3.0)
    assert at_luck["dsr"] == pytest.approx(0.5)
    assert r["dsr"] < r["psr_vs_zero"]                          # deflation always lowers the claim
    assert_labelled(r)


def test_deflated_sharpe_falls_with_more_trials_and_rises_with_sharpe_and_length():
    base = dict(n_obs=250, sharpe_variance=0.004, skew=0.0, kurtosis=3.0)
    by_n = [sp.deflated_sharpe(0.15, n_trials=n, **base)["dsr"] for n in (1, 5, 50, 500)]
    assert by_n == sorted(by_n, reverse=True) and by_n[0] > by_n[-1] + 0.2
    by_sr = [sp.deflated_sharpe(s, n_trials=50, **base)["dsr"] for s in (0.05, 0.1, 0.2, 0.3)]
    assert by_sr == sorted(by_sr)
    by_t = [sp.deflated_sharpe(0.2, n_trials=50, n_obs=t, sharpe_variance=0.004, skew=0.0, kurtosis=3.0)["dsr"]
            for t in (50, 250, 1000)]
    assert by_t == sorted(by_t)


def test_deflated_sharpe_does_not_default_the_moments_and_refuses_degenerate_ones():
    base = dict(n_obs=250, n_trials=10, sharpe_variance=0.004)
    with pytest.raises(sp.UnknownInputError):
        sp.deflated_sharpe(0.1, skew=None, kurtosis=3.0, **base)
    with pytest.raises(sp.UnknownInputError):
        sp.deflated_sharpe(0.1, skew=0.0, kurtosis=None, **base)
    with pytest.raises(sp.StatisticalPowerError):
        sp.deflated_sharpe(0.1, skew=0.0, kurtosis=3.0, n_obs=250, n_trials=0, sharpe_variance=0.004)
    r = sp.deflated_sharpe(10.0, skew=5.0, kurtosis=1.0, **base)       # variance term not positive
    assert r["status"] == "INSUFFICIENT" and r["dsr"] is None


def test_best_of_many_noise_sharpes_looks_like_luck_after_deflation_but_not_before():
    rng = random.Random(12)
    t, n_trials, reps = 250, 30, 150
    dsrs, psrs = [], []
    for _ in range(reps):
        srs = [rng.gauss(0.0, 1 / math.sqrt(t)) for _ in range(n_trials)]
        var = statistics.variance(srs)
        r = sp.deflated_sharpe(max(srs), n_obs=t, n_trials=n_trials, sharpe_variance=var, skew=0.0, kurtosis=3.0)
        dsrs.append(r["dsr"])
        psrs.append(r["psr_vs_zero"])
    assert statistics.mean(psrs) > 0.9               # ignoring the search, the winner looks significant
    assert abs(statistics.mean(dsrs) - 0.5) < 0.12    # accounting for it, the winner looks like luck
    real = sp.deflated_sharpe(0.35, n_obs=t, n_trials=n_trials, sharpe_variance=1 / t, skew=0.0, kurtosis=3.0)
    assert real["dsr"] > 0.99


def test_selection_adjusted_best_reports_the_deflated_sharpe_only_when_it_can():
    rng = random.Random(13)
    led = sp.TrialLedger()
    t = 250
    for i in range(30):
        sr = rng.gauss(0.0, 1 / math.sqrt(t))
        led.record("grid", f"p{i}", "w", sr, pvalue=1 - N.cdf(sr * math.sqrt(t - 1)), n_obs=t)
    plain = sp.selection_adjusted_best(led, alpha=0.05)
    assert plain["deflated_sharpe"] is None
    unsure = sp.selection_adjusted_best(led, alpha=0.05, statistic_kind="sharpe")
    assert unsure["deflated_sharpe"]["status"] == "INSUFFICIENT" and "skew" in unsure["deflated_sharpe"]["reason"]
    assumed = sp.selection_adjusted_best(led, alpha=0.05, statistic_kind="sharpe", assume_normal_returns=True)
    d = assumed["deflated_sharpe"]
    assert d["status"] == "OK" and d["skew"] == 0.0 and d["kurtosis"] == 3.0
    assert any("ASSUMED" in w for w in assumed["warnings"])
    assert d["n_trials"] == 30 and d["sharpe_variance"] == pytest.approx(statistics.variance([x.statistic for x in led.trials]))
    stated = sp.selection_adjusted_best(led, alpha=0.05, statistic_kind="sharpe", skew=0.1, kurtosis=3.4)
    assert stated["deflated_sharpe"]["skew"] == 0.1 and stated["warnings"] == []
    assert_labelled(assumed)


# ---------------------------------------------------------------------------------------------
# cost of a false discovery
# ---------------------------------------------------------------------------------------------
def test_low_prior_odds_make_a_significant_result_probably_false():
    assert sp.posterior_prob_true(0.01, 0.05, 0.8) == pytest.approx(0.008 / (0.008 + 0.0495))
    r = sp.false_discovery_cost(prior=0.01, alpha=0.05, power=0.8, loss_if_false=100.0, gain_if_true=100.0)
    assert r["posterior_prob_true"] == pytest.approx(0.13913, abs=1e-5)
    assert r["prob_false_given_significant"] == pytest.approx(0.86087, abs=1e-5) and r["prob_false_source"] == "BAYES"
    assert r["expected_value"] == pytest.approx(0.13913 * 100 - 0.86087 * 100, abs=1e-3) and not r["positive_expected_value"]
    assert r["expected_false_discovery_cost"] == pytest.approx(86.087, abs=1e-2)
    assert r["likelihood_ratio_significant"] == pytest.approx(16.0)
    assert r["posterior_odds_true"] == pytest.approx(16.0 * 0.01 / 0.99)
    assert "false" in r["reading"]
    assert_labelled(r)


def test_even_prior_odds_make_the_same_result_probably_true_and_worth_acting_on():
    r = sp.false_discovery_cost(prior=0.5, alpha=0.05, power=0.8, loss_if_false=100.0, gain_if_true=100.0)
    assert r["posterior_prob_true"] == pytest.approx(0.8 / 0.85)
    assert r["positive_expected_value"] is True and r["expected_value"] > 0


def test_adjusted_alpha_and_power_move_the_posterior_in_the_right_direction():
    base = sp.posterior_prob_true(0.05, 0.05, 0.8)
    assert sp.posterior_prob_true(0.05, 0.005, 0.8) > base           # a stricter threshold raises the share that are real
    assert sp.posterior_prob_true(0.05, 0.05, 0.4) < base            # underpowered studies are mostly false when they hit
    priors = [sp.posterior_prob_true(p, 0.05, 0.8) for p in (0.001, 0.01, 0.1, 0.5, 0.9)]
    assert priors == sorted(priors)


def test_the_bayes_posterior_matches_a_simulation_of_many_candidates():
    rng = random.Random(14)
    prior, alpha, power = 0.1, 0.05, 0.8
    shift = z_upper(alpha) + N.inv_cdf(power)                       # the z-shift that gives 80% power
    sig_true = sig_false = 0
    for _ in range(60000):
        real = rng.random() < prior
        z = rng.gauss(shift if real else 0.0, 1.0)
        if z > z_upper(alpha):
            sig_true += real
            sig_false += not real
    sim = sig_true / (sig_true + sig_false)
    assert abs(sim - sp.posterior_prob_true(prior, alpha, power)) < 0.015


def test_a_supplied_false_discovery_probability_is_used_and_labelled():
    r = sp.false_discovery_cost(prob_false=0.25, loss_if_false=40.0, gain_if_true=10.0)
    assert r["prob_false_source"] == "SUPPLIED" and r["posterior_prob_true"] == pytest.approx(0.75)
    assert r["expected_value"] == pytest.approx(0.75 * 10 - 0.25 * 40)
    assert r["breakeven_posterior_prob_true"] == pytest.approx(0.8)
    assert r["classes"]["prob_false_given_significant"] == sp.ASSUMED
    assert_labelled(r)


def test_false_discovery_cost_refuses_unknown_and_invalid_inputs():
    with pytest.raises(sp.UnknownInputError):
        sp.false_discovery_cost(prior=0.1, alpha=0.05, loss_if_false=1.0, gain_if_true=1.0)          # no power
    with pytest.raises(sp.UnknownInputError):
        sp.false_discovery_cost(loss_if_false=1.0, gain_if_true=1.0)
    with pytest.raises(sp.StatisticalPowerError):
        sp.false_discovery_cost(prior=1.0, alpha=0.05, power=0.8, loss_if_false=1.0, gain_if_true=1.0)
    with pytest.raises(sp.StatisticalPowerError):
        sp.false_discovery_cost(prior=0.1, alpha=0.05, power=0.8, loss_if_false=-1.0, gain_if_true=1.0)
    with pytest.raises(sp.StatisticalPowerError):
        sp.false_discovery_cost(prob_false=1.5, loss_if_false=1.0, gain_if_true=1.0)
    with pytest.raises(TypeError):
        sp.false_discovery_cost(prior=0.1, alpha=0.05, power=0.8)                                    # money terms required


# ---------------------------------------------------------------------------------------------
# purged and embargoed splits, walk-forward, holdout
# ---------------------------------------------------------------------------------------------
def expected_train(n, a, b, h, e):
    banned = set(range(max(0, a - h), a)) | set(range(b, min(n, b + e))) | set(range(a, b))
    return [i for i in range(n) if i not in banned]


@pytest.mark.parametrize("n,k,h,e", [(100, 5, 0, 0), (100, 5, 3, 2), (101, 4, 10, 10), (50, 10, 4, 0), (37, 3, 7, 12),
                                     (200, 8, 25, 5), (30, 2, 1, 0), (64, 4, 40, 0)])
def test_purged_embargoed_splits_remove_exactly_the_label_and_embargo_zones(n, k, h, e):
    splits = sp.purged_embargoed_splits(n, k, h, e)
    assert len(splits) == k
    covered = []
    for train, test in splits:
        a, b = test[0], test[-1] + 1
        assert test == list(range(a, b))
        assert train == expected_train(n, a, b, h, e)
        covered.extend(test)
    assert covered == list(range(n))                        # test blocks partition the series, in time order
    sizes = [len(t) for _, t in splits]
    assert max(sizes) - min(sizes) <= 1


@pytest.mark.parametrize("n,k,h,e", [(120, 6, 5, 5), (90, 3, 12, 0), (75, 5, 0, 8), (60, 4, 9, 9)])
def test_no_training_label_window_overlaps_any_test_index_and_no_embargoed_index_trains(n, k, h, e):
    for train, test in sp.purged_embargoed_splits(n, k, h, e):
        test_set = set(test)
        assert train == sorted(set(train)) and test == sorted(test_set)          # chronological, no duplicates
        assert not set(train) & test_set
        for j in train:
            assert not (set(range(j, j + h + 1)) & test_set)                       # label window [j, j+h] misses the test block
            assert not any(0 < j - i <= e for i in test)                            # nothing within the embargo after the block
        rep = sp.audit_split(train, test, n, h, e)
        assert rep["clean"] is True and rep["label_overlap"] == 0 and rep["embargo_violations"] == 0


def test_the_audit_catches_a_naive_split_so_the_leak_checks_are_not_vacuous():
    n, k, h, e = 100, 5, 6, 4
    leaky = 0
    for f in range(k):
        test = list(range(f * 20, f * 20 + 20))
        naive_train = [i for i in range(n) if i not in set(test)]
        rep = sp.audit_split(naive_train, test, n, h, e)
        leaky += not rep["clean"]
        if f > 0:
            assert rep["label_overlap"] == h                    # the h indices just before the block
        if f < k - 1:
            assert rep["embargo_violations"] == e
    assert leaky == k
    assert "LEAKS" in sp.audit_split(naive_train, test, n, h, e)["reading"]


def test_without_an_embargo_training_values_sit_inside_test_labels_and_with_one_they_do_not():
    n, k, h = 100, 4, 6
    no_embargo = sp.purged_embargoed_splits(n, k, h, 0)
    exposed = [sp.audit_split(tr, te, n, h, 0)["train_values_inside_test_labels"] for tr, te in no_embargo]
    assert exposed[:-1] == [h] * (k - 1) and exposed[-1] == 0
    for tr, te in sp.purged_embargoed_splits(n, k, h, h):
        assert sp.audit_split(tr, te, n, h, h)["train_values_inside_test_labels"] == 0


def test_a_horizon_longer_than_a_fold_still_leaks_nothing():
    n, k, h, e = 60, 6, 25, 3                              # folds of 10 with a purge of 25
    for train, test in sp.purged_embargoed_splits(n, k, h, e):
        assert sp.audit_split(train, test, n, h, e)["clean"]
        assert all(j + h < test[0] or j >= test[-1] + 1 + e for j in train)


def test_purged_split_refuses_bad_arguments_and_empty_training_sets():
    for args in [(1, 2, 0, 0), (10, 1, 0, 0), (10, 11, 0, 0), (10, 2, -1, 0), (10, 2, 0, -1), (10.5, 2, 0, 0),
                 (10, 2, True, 0), (10, None, 0, 0)]:
        with pytest.raises(sp.StatisticalPowerError):
            sp.purged_embargoed_splits(*args)
    with pytest.raises(sp.InsufficientSplitError):
        sp.purged_embargoed_splits(10, 2, 0, 10)           # the embargo swallows everything after fold 0


@pytest.mark.parametrize("n,train_min,test_len,h,e", [(100, 30, 10, 3, 2), (60, 20, 20, 0, 0), (200, 50, 7, 12, 12),
                                                      (90, 10, 5, 5, 0)])
def test_walk_forward_trains_only_on_the_past_with_a_gap_and_tiles_the_future(n, train_min, test_len, h, e):
    folds = sp.walk_forward(n, train_min, test_len, h, e)
    assert folds
    prev_end = None
    sizes = []
    for train, test in folds:
        assert train == list(range(len(train))) and test == list(range(test[0], test[0] + test_len))
        assert len(train) >= train_min
        assert max(train) + h + e < test[0]                                         # the label window and the buffer clear the test block
        for j in train:
            assert not (set(range(j, j + h + 1)) & set(test))
        if prev_end is not None:
            assert test[0] == prev_end                                              # blocks tile without gap or overlap
        prev_end = test[-1] + 1
        sizes.append(len(train))
        rep = sp.audit_split(train, test, n, h, e)
        assert rep["clean"] and rep["chronological"]
    assert sizes == sorted(sizes) and len(set(sizes)) == len(sizes)                # expanding window
    assert prev_end <= n and prev_end + test_len > n                               # as many folds as fit


def test_walk_forward_refuses_what_does_not_fit():
    with pytest.raises(sp.InsufficientSplitError):
        sp.walk_forward(20, 30, 5, 0, 0)
    with pytest.raises(sp.StatisticalPowerError):
        sp.walk_forward(100, 0, 5, 0, 0)
    with pytest.raises(sp.StatisticalPowerError):
        sp.walk_forward(100, 10, 0, 0, 0)


@pytest.mark.parametrize("n,frac,h,e", [(100, 0.2, 5, 3), (101, 0.25, 0, 0), (50, 0.1, 10, 10), (1000, 0.33, 20, 0)])
def test_holdout_is_the_last_block_and_training_stops_before_its_label_window(n, frac, h, e):
    train, hold = sp.holdout_split(n, frac, h, e)
    assert hold == list(range(n - len(hold), n)) and len(hold) == math.ceil(frac * n - 1e-9)
    assert train == list(range(len(train))) and max(train) + h + e < hold[0]
    assert sp.audit_split(train, hold, n, h, e)["clean"]
    assert not set(train) & set(hold)


def test_holdout_refuses_bad_fractions_and_an_empty_training_set():
    for frac in (0.0, 1.0, -0.1, 1.5, None, True):
        with pytest.raises(sp.StatisticalPowerError):
            sp.holdout_split(100, frac, 0, 0)
    with pytest.raises(sp.InsufficientSplitError):
        sp.holdout_split(20, 0.5, 8, 8)


def test_audit_flags_malformed_index_lists():
    assert sp.audit_split([3, 2, 1], [5], 10, 0, 0)["clean"] is False
    assert sp.audit_split([1, 2], [2, 3], 10, 0, 0)["clean"] is False                 # overlapping train and test
    assert sp.audit_split([1, 99], [5], 10, 0, 0)["clean"] is False
    rep = sp.audit_split([5, 6], [1, 2], 10, 0, 0)                                      # train AFTER test is flagged non-chronological
    assert rep["clean"] and rep["chronological"] is False
    assert_labelled(rep)


# ---------------------------------------------------------------------------------------------
# EvaluationPlan and its digest
# ---------------------------------------------------------------------------------------------
def test_plan_is_frozen_and_hashable_and_its_parameters_cannot_be_edited():
    plan = make_plan()
    with pytest.raises(dataclasses.FrozenInstanceError):
        plan.alpha = 0.5
    with pytest.raises(dataclasses.FrozenInstanceError):
        plan.evaluation_window = (0, 10 ** 6)
    with pytest.raises(TypeError):
        plan.parameters["entry_bps"] = 9.9
    with pytest.raises(AttributeError):
        plan.parameters._d = {}
    assert plan.parameters["lookback"] == (3, 6) and hash(plan) == hash(make_plan()) and plan == make_plan()


def test_editing_the_callers_dict_after_the_fact_does_not_change_the_plan():
    params = {"entry_bps": 1.5, "lookback": [3, 6], "nested": {"a": [1, 2]}}
    plan = make_plan(parameters=params)
    digest = sp.plan_digest(plan)
    params["entry_bps"] = 99.0
    params["lookback"].append(12)
    params["nested"]["a"].append(3)
    assert sp.plan_digest(plan) == digest and plan.parameters["lookback"] == (3, 6)


def test_digest_is_a_deterministic_sha256_hex_independent_of_key_order_and_integral_float_spelling():
    plan = make_plan()
    d = sp.plan_digest(plan)
    assert len(d) == 64 and int(d, 16) >= 0 and d == sp.plan_digest(make_plan())
    reordered = make_plan(parameters={"lookback": [3, 6], "entry_bps": 1.5})
    assert sp.plan_digest(reordered) == d
    assert sp.plan_digest(make_plan(parameters={"entry_bps": 1.5, "lookback": [3.0, 6.0]})) == d
    assert sp.plan_digest(make_plan(evaluation_window=(0.0, 200.0))) == d


@pytest.mark.parametrize("field,value", [
    ("strategy_id", "carry_v2"), ("version", "1.0.1"), ("parameters", {"entry_bps": 1.6, "lookback": [3, 6]}),
    ("parameters", {"entry_bps": 1.5, "lookback": [6, 3]}), ("parameters", {"entry_bps": 1.5}),
    ("evaluation_window", (0, 201)), ("evaluation_window", (1, 200)), ("metric", "median"), ("alpha", 0.049),
    ("power_target", 0.81), ("min_edge", 0.26), ("n_hypotheses_declared", 2), ("seed", 1), ("adjust", "bh")])
def test_changing_any_declared_field_changes_the_digest(field, value):
    assert sp.plan_digest(make_plan(**{field: value})) != sp.plan_digest(make_plan())


def test_plan_digest_has_no_collisions_across_a_sweep_of_windows():
    digests = {sp.plan_digest(make_plan(evaluation_window=(0, 200 + k))) for k in range(300)}
    assert len(digests) == 300


@pytest.mark.parametrize("field,value", [
    ("strategy_id", ""), ("strategy_id", None), ("version", " "), ("metric", 5), ("parameters", None),
    ("parameters", [1, 2]), ("parameters", {"x": object()}), ("parameters", {"x": float("nan")}),
    ("parameters", {1: "x"}), ("evaluation_window", None), ("evaluation_window", (5, 5)), ("evaluation_window", (9, 1)),
    ("evaluation_window", (0,)), ("evaluation_window", (0, float("inf"))), ("alpha", 0.0), ("alpha", 1.0),
    ("power_target", 1.0), ("min_edge", 0.0), ("min_edge", None), ("n_hypotheses_declared", 0),
    ("n_hypotheses_declared", 1.5), ("seed", -1), ("seed", True), ("seed", None), ("adjust", "sidak")])
def test_plan_refuses_invalid_or_unknown_declarations(field, value):
    with pytest.raises(sp.StatisticalPowerError):
        make_plan(**{field: value})


# ---------------------------------------------------------------------------------------------
# evaluate_under_plan: refusals
# ---------------------------------------------------------------------------------------------
def test_a_changed_window_changes_the_digest_and_is_refused_both_ways():
    plan = make_plan(n=200)
    pinned = sp.plan_digest(plan)
    rng = random.Random(21)
    data = [rng.gauss(0.3, 1) for _ in range(200)]
    shifted = dataclasses.replace(plan, evaluation_window=(0, 100))
    assert sp.plan_digest(shifted) != pinned
    with pytest.raises(sp.PlanDigestMismatch):                       # the analyst edits the plan after seeing the data
        evaluate(shifted, data[:100], digest=pinned)
    with pytest.raises(sp.WindowViolation):                          # the analyst keeps the plan but feeds a different window
        evaluate(plan, data, ts=range(100, 300))
    with pytest.raises(sp.WindowViolation):                          # an honest re-registration still cannot take the old data
        evaluate(shifted, data, digest=sp.plan_digest(shifted))
    res, led = evaluate(plan, data)                                  # the registered plan on the registered window works
    assert res["plan_digest"] == pinned and led.n_trials == 1


def test_data_outside_the_window_is_refused_on_either_side_and_at_the_exclusive_end():
    plan = make_plan(n=100)
    base = [0.1] * 100
    with pytest.raises(sp.WindowViolation, match="outside"):
        evaluate(plan, base, ts=range(-1, 99))                       # one observation before the start
    with pytest.raises(sp.WindowViolation, match="outside"):
        evaluate(plan, base, ts=range(1, 101))                       # the window is half-open: ts == end is outside
    with pytest.raises(sp.WindowViolation):
        evaluate(plan, base + [0.2], ts=list(range(100)) + [100])
    with pytest.raises(sp.WindowViolation):
        evaluate(plan, base + [0.2], ts=list(range(100)) + [10 ** 9])


def test_data_that_covers_only_part_of_the_window_is_refused():
    plan = make_plan(n=200)
    rng = random.Random(22)
    data = [rng.gauss(0, 1) for _ in range(200)]
    with pytest.raises(sp.WindowCoverageError):
        evaluate(plan, data[:100], ts=range(100))                     # the unfavourable second half was dropped
    with pytest.raises(sp.WindowCoverageError):
        evaluate(plan, data[100:], ts=range(100, 200))                # the first half was dropped
    with pytest.raises(sp.WindowCoverageError):
        evaluate(plan, [], ts=[])
    assert issubclass(sp.WindowCoverageError, sp.WindowViolation)
    evaluate(plan, data[1:], ts=range(1, 200))                       # a spacing's worth of slack at the ends is fine


def test_a_digest_that_is_not_the_pinned_one_is_refused_and_nothing_is_recorded():
    plan = make_plan()
    other = sp.plan_digest(make_plan(min_edge=0.1))
    led = sp.TrialLedger()
    data = [0.1] * 200
    for bad in (other, "0" * 64, "", None, 12345):
        with pytest.raises(sp.PlanDigestMismatch):
            sp.evaluate_under_plan(plan, list(zip(range(200), data)), sp.mean_statistic, expected_digest=bad, ledger=led)
    assert led.n_trials == 0
    with pytest.raises(TypeError):
        sp.evaluate_under_plan(plan, list(zip(range(200), data)), sp.mean_statistic, ledger=led)    # no pinned digest at all
    res = sp.evaluate_under_plan(plan, list(zip(range(200), data)), sp.mean_statistic,
                                 expected_digest=sp.plan_digest(plan).upper(), ledger=led)
    assert res["plan_digest"] == sp.plan_digest(plan) and led.n_trials == 1


def test_every_refusal_leaves_the_ledger_untouched():
    plan = make_plan(n=50)
    led = sp.TrialLedger()
    good = list(zip(range(50), [0.1 * (i % 5) for i in range(50)]))
    bad_inputs = [
        good + [(60, 1.0)],                                           # outside
        good[:20],                                                    # truncated
        [(t, v) for t, v in good[:10]] + [(5, 1.0)] + good[10:],      # time goes backwards
        good[:10] + [good[9]] + good[10:],                            # duplicated timestamp
        good[:10] + [(10, float("nan"))] + good[11:],                 # NaN value
        good[:10] + [(10, True)] + good[11:],                         # bool is not a number
        good[:10] + [(10, "x")] + good[11:],
        good[:10] + [(10,)] + good[11:],
        "not observations",
    ]
    for obs in bad_inputs:
        with pytest.raises(sp.PlanError):
            sp.evaluate_under_plan(plan, obs, sp.mean_statistic, expected_digest=sp.plan_digest(plan), ledger=led)
    assert led.n_trials == 0


def test_malformed_arguments_to_evaluate_are_refused():
    plan = make_plan(n=50)
    obs = [(i, float(i % 3)) for i in range(50)]
    d = sp.plan_digest(plan)
    with pytest.raises(sp.PlanError):
        sp.evaluate_under_plan("plan", obs, sp.mean_statistic, expected_digest=d, ledger=sp.TrialLedger())
    with pytest.raises(sp.PlanError):
        sp.evaluate_under_plan(plan, obs, sp.mean_statistic, expected_digest=d, ledger=[])
    with pytest.raises(sp.PlanError):
        sp.evaluate_under_plan(plan, obs, "mean", expected_digest=d, ledger=sp.TrialLedger())
    with pytest.raises(sp.UnknownInputError):
        sp.evaluate_under_plan(plan, None, sp.mean_statistic, expected_digest=d, ledger=sp.TrialLedger())
    with pytest.raises(sp.StatisticalPowerError):
        sp.evaluate_under_plan(plan, obs, sp.mean_statistic, expected_digest=d, ledger=sp.TrialLedger(), bootstrap_B=5)
    with pytest.raises(sp.DataIntegrityError):
        sp.evaluate_under_plan(plan, obs, lambda v: float("nan"), expected_digest=d, ledger=sp.TrialLedger())


def test_evaluation_refuses_a_tampered_ledger():
    plan = make_plan()
    led = filled(4)
    del led._entries[0]
    with pytest.raises(sp.LedgerTamperError):
        evaluate(plan, [0.1] * 200, ledger=led)


# ---------------------------------------------------------------------------------------------
# evaluate_under_plan: contents, recording and verdicts
# ---------------------------------------------------------------------------------------------
def test_evaluation_records_itself_and_reports_every_required_piece():
    plan = make_plan(n=300)
    rng = random.Random(23)
    data = [rng.gauss(0.5, 1) for _ in range(300)]
    res, led = evaluate(plan, data)
    assert led.n_trials == 1
    t = led.trials[0]
    assert (t.strategy_id, t.window_id, t.n_obs, t.plan_digest) == ("carry_v1", "[0,300]", 300, res["plan_digest"])
    assert t.params_hash == plan.params_hash and t.pvalue == res["p_value"] and t.statistic == res["statistic"]
    assert res["statistic"] == pytest.approx(sum(data) / 300)
    assert res["ledger"]["n_trials"] == 1 and res["ledger"]["head"] == led.head
    for key in ("ci", "required", "achieved_power", "adjusted_p_value", "verdict", "effective_n_available",
                "required_raw_n", "available_raw_n", "required_effective_n"):
        assert res[key] is not None
    assert res["verdict"] in sp.VERDICTS and res["status"] == "OK"
    assert res["required"]["required_raw_n"] == res["required_raw_n"] and res["available_raw_n"] == 300
    assert res["required"]["classes"]["sd"] == sp.ESTIMATED          # measured here, not assumed
    assert_labelled(res)
    assert_labelled(res["required"])


def test_the_interval_is_the_evidence_stats_block_bootstrap_with_the_plan_seed():
    plan = make_plan(n=150, seed=4242)
    rng = random.Random(24)
    data = [rng.gauss(0.2, 1) for _ in range(150)]
    res, _ = evaluate(plan, data, B=60)
    direct = ES.block_bootstrap_ci(data, None, B=60, seed=4242)
    assert res["ci"] == direct and res["ci"]["block"] >= round(150 ** (1 / 3))
    assert res["effective_n_available"] == ES.conservative_ess(data)
    other, _ = evaluate(make_plan(n=150, seed=1), data, B=60)
    assert other["ci"]["bootstrap_lo"] != res["ci"]["bootstrap_lo"] or other["ci"]["bootstrap_hi"] != res["ci"]["bootstrap_hi"]


def test_the_p_value_uses_student_t_on_the_effective_n_not_the_normal():
    rng = random.Random(37)
    data = [rng.gauss(0.9, 1) for _ in range(12)]
    ess = ES.conservative_ess(data)
    assert 3.0 <= ess <= 12.0
    m = sum(data) / 12
    sd = statistics.stdev(data)
    t_stat = m / (sd / math.sqrt(ess))
    res, _ = evaluate(make_plan(n=12, n_hypotheses_declared=1), data)
    assert res["p_value"] == pytest.approx(sp.student_t_sf(t_stat, ess - 1.0), rel=1e-9)
    assert res["p_value"] > 1.05 * (1.0 - N.cdf(t_stat))          # heavier tail than the normal at this few draws
    assert res["t_or_z_statistic"] == pytest.approx(t_stat)


def test_the_evaluation_is_deterministic():
    plan = make_plan(n=200)
    rng = random.Random(25)
    data = [rng.gauss(0.1, 1) for _ in range(200)]
    a, _ = evaluate(plan, data)
    b, _ = evaluate(plan, data)
    assert a == b


def test_a_strong_edge_in_adequate_data_is_supported():
    plan = make_plan(n=400)
    rng = random.Random(26)
    res, _ = evaluate(plan, [rng.gauss(0.6, 1) for _ in range(400)])
    assert res["verdict"] == sp.EDGE_SUPPORTED_AFTER_ADJUSTMENT
    assert res["adjusted_p_value"] < 1e-6 and res["adequately_powered"] and res["achieved_power"] >= 0.8
    assert res["effective_n_available"] >= res["required_effective_n"]
    assert res["z_exceeds_expected_noise_max"] is True


def test_pure_noise_in_adequate_data_finds_no_edge():
    plan = make_plan(n=600)
    rng = random.Random(27)
    res, _ = evaluate(plan, centred(rng, 600))                       # sample mean exactly zero
    assert res["verdict"] == sp.NO_EDGE_DETECTED
    assert res["p_value"] == pytest.approx(0.5, abs=1e-9) and res["adequately_powered"]
    assert "adequately powered" in res["reason"]


def test_noise_in_data_too_short_to_tell_is_insufficient_power_not_no_edge():
    plan = make_plan(n=40)
    rng = random.Random(28)
    res, _ = evaluate(plan, centred(rng, 40))
    assert res["verdict"] == sp.INSUFFICIENT_POWER and not res["adequately_powered"]
    assert res["required_effective_n"] > res["effective_n_available"]
    assert res["achieved_power"] < 0.8


def test_a_significant_result_from_an_underpowered_sample_is_not_promoted():
    plan = make_plan(n=60, min_edge=0.05)                            # detecting 0.05 sd needs thousands of observations
    rng = random.Random(29)
    res, _ = evaluate(plan, [rng.gauss(1.0, 1) for _ in range(60)])
    assert res["significant_after_adjustment"] is True and res["adjusted_p_value"] < 1e-6
    assert res["verdict"] == sp.INSUFFICIENT_POWER
    assert "winner's curse" in res["reason"]


def test_the_same_data_stop_being_an_edge_when_more_hypotheses_were_declared():
    rng = random.Random(30)
    data = [rng.gauss(0.12, 1) for _ in range(500)]
    one, _ = evaluate(make_plan(n=500, n_hypotheses_declared=1), data)
    many, _ = evaluate(make_plan(n=500, n_hypotheses_declared=2000), data)
    p = one["p_value"]
    assert 1e-4 < p < 0.04
    assert one["adjusted_p_value"] == pytest.approx(p) and one["verdict"] == sp.EDGE_SUPPORTED_AFTER_ADJUSTMENT
    assert many["adjusted_p_value"] == pytest.approx(min(1.0, 2000 * p)) and many["n_hypotheses_used"] == 2000
    assert many["verdict"] != sp.EDGE_SUPPORTED_AFTER_ADJUSTMENT
    assert many["required_effective_n"] > one["required_effective_n"]


def test_the_ledger_count_overrides_a_smaller_declared_count_and_every_look_is_counted():
    plan = make_plan(n=300, n_hypotheses_declared=1)
    rng = random.Random(31)
    data = [rng.gauss(0.2, 1) for _ in range(300)]
    led = null_ledger(49, seed=32)
    res, led = evaluate(plan, data, ledger=led)
    assert res["n_hypotheses_declared"] == 1 and res["n_hypotheses_used"] == 50 and led.n_trials == 50
    assert any("more than the 1 declared" in w for w in res["warnings"])
    again, led = evaluate(plan, data, ledger=led)                     # looking again is another trial, and it says so
    assert again["n_hypotheses_used"] == 51 and again["ledger"]["evaluations_of_this_plan"] == 2
    assert any("evaluated 2 times" in w for w in again["warnings"])
    assert led.n_trials == 51 and led.n_distinct == 50          # the repeat is a trial but not a new hypothesis


def test_a_constant_series_has_no_interval_and_no_verdict_beyond_insufficient():
    plan = make_plan(n=100)
    res, led = evaluate(plan, [0.5] * 100)
    assert res["verdict"] == sp.INSUFFICIENT_POWER and res["status"] == "INSUFFICIENT"
    assert res["ci"]["status"] == "NO_VARIANCE" and "lo" not in res["ci"]
    assert res["p_value"] is None and res["adjusted_p_value"] is None and "constant" in res["reason"]
    assert led.n_trials == 1 and led.trials[0].pvalue is None          # the look was still counted
    assert_labelled(res)


def test_too_few_observations_is_insufficient_with_the_reason():
    plan = make_plan(n=6)
    res, led = evaluate(plan, [0.3, 0.1, 0.5, 0.2, 0.4, 0.6])
    assert res["verdict"] == sp.INSUFFICIENT_POWER and "fewer than" in res["reason"] and res["achieved_power"] is None
    assert led.n_trials == 1


def test_a_persistent_series_with_too_few_independent_draws_cannot_be_supported():
    rng = random.Random(33)
    n = 150
    data = ar1(rng, n, 0.93, mean=3.0)
    res, _ = evaluate(make_plan(n=n, min_edge=0.5), data)
    assert res["effective_n_available"] < sp.MIN_ESS_FOR_SUPPORT
    assert res["verdict"] == sp.INSUFFICIENT_POWER and res["significant_after_adjustment"] is True
    assert any("effective observations" in w for w in res["warnings"])


def test_bh_and_none_adjustments_flow_through_the_plan():
    rng = random.Random(34)
    data = [rng.gauss(0.15, 1) for _ in range(500)]
    led_a, led_b = null_ledger(30, seed=35), null_ledger(30, seed=35)
    holm, _ = evaluate(make_plan(n=500, adjust="holm", n_hypotheses_declared=31), data, ledger=led_a)
    bh, _ = evaluate(make_plan(n=500, adjust="bh", n_hypotheses_declared=31), data, ledger=led_b)
    none, _ = evaluate(make_plan(n=500, adjust="none", n_hypotheses_declared=31), data, ledger=sp.TrialLedger())
    assert holm["adjusted_p_value"] >= bh["adjusted_p_value"] >= none["adjusted_p_value"]
    assert none["adjusted_p_value"] == pytest.approx(none["p_value"])
    assert holm["adjust"] == "holm" and bh["adjust"] == "bh"


def test_a_non_mean_statistic_uses_the_bootstrap_standard_error_and_says_so():
    def median_edge(values):
        return statistics.median(values)
    plan = make_plan(n=300, metric="median_edge")
    rng = random.Random(36)
    data = [rng.gauss(0.5, 1) for _ in range(300)]
    res, led = evaluate(plan, data, fn=median_edge)
    assert res["statistic"] == pytest.approx(statistics.median(data)) and "bootstrap" in res["inference"]
    assert res["ci"]["method"].startswith("moving-block bootstrap only") and 0.0 < res["p_value"] < 1e-3
    assert res["verdict"] in sp.VERDICTS and not res["warnings"]
    assert_labelled(res)
    mean_res, _ = evaluate(make_plan(n=300), data)
    assert "t test of the mean" in mean_res["inference"]
    null_res, _ = evaluate(plan, centred(rng, 300), fn=median_edge)
    assert null_res["p_value"] > 0.05


def test_a_statistic_function_that_does_not_match_the_declared_metric_is_flagged():
    def something_else(values):
        return sum(values) / len(values)
    res, _ = evaluate(make_plan(n=200, metric="mean"), [0.1 * (i % 7) for i in range(200)], fn=something_else)
    assert any("something_else" in w and "declared" in w for w in res["warnings"])


# ---------------------------------------------------------------------------------------------
# calibration and power of the whole pipeline (hundreds of seeded replications)
# ---------------------------------------------------------------------------------------------
def test_null_calibration_with_fifty_trials_in_the_ledger_never_declares_an_edge():
    rng = random.Random(40)
    reps, n = 400, 250
    supported = unadj = 0
    for r in range(reps):
        led = null_ledger(50, seed=1000 + r)                          # 50 other noise trials already examined
        plan = make_plan(n=n, n_hypotheses_declared=51)
        res, _ = evaluate(plan, [rng.gauss(0, 1) for _ in range(n)], ledger=led, B=40)
        supported += res["verdict"] == sp.EDGE_SUPPORTED_AFTER_ADJUSTMENT
        unadj += res["p_value"] <= 0.05
        assert res["n_hypotheses_used"] == 51
    assert supported / reps <= 0.05
    assert 0.02 <= unadj / reps <= 0.08         # the test has teeth: without the adjustment it rejects at about alpha


def test_null_calibration_for_iid_noise_matches_alpha_without_and_far_below_alpha_with_adjustment():
    rng = random.Random(41)
    reps, n = 500, 250
    raw_hits = adj_hits = 0
    for _ in range(reps):
        res, _ = evaluate(make_plan(n=n, n_hypotheses_declared=1), [rng.gauss(0, 1) for _ in range(n)])
        raw_hits += res["adjusted_p_value"] <= 0.05
    for _ in range(reps):
        res, _ = evaluate(make_plan(n=n, n_hypotheses_declared=50), [rng.gauss(0, 1) for _ in range(n)])
        adj_hits += res["adjusted_p_value"] <= 0.05
    assert 0.015 <= raw_hits / reps <= 0.075 and adj_hits / reps <= 0.02


def test_null_calibration_for_serially_dependent_noise():
    rng = random.Random(42)
    reps, n = 400, 1000
    hits = supported = 0
    for _ in range(reps):
        res, _ = evaluate(make_plan(n=n), ar1(rng, n, 0.5))
        hits += res["adjusted_p_value"] <= 0.05
        supported += res["verdict"] == sp.EDGE_SUPPORTED_AFTER_ADJUSTMENT
        assert res["ess_ratio"] < 0.5                                  # the dependence was seen
    assert hits / reps <= 0.08 and supported / reps <= 0.08


def test_family_wise_error_over_fifty_independent_noise_strategies():
    rng = random.Random(43)
    reps, m, n = 50, 50, 120
    runs_with_a_claim = 0
    for _ in range(reps):
        led = sp.TrialLedger()
        claimed = False
        for k in range(m):
            plan = make_plan(n=n, parameters={"k": k}, n_hypotheses_declared=m)
            res, _ = evaluate(plan, [rng.gauss(0, 1) for _ in range(n)], ledger=led, B=40)
            claimed |= res["verdict"] == sp.EDGE_SUPPORTED_AFTER_ADJUSTMENT
        assert led.n_trials == m and led.n_distinct == m
        runs_with_a_claim += claimed
    assert runs_with_a_claim / reps <= 0.10


def test_the_same_fifty_strategy_search_does_find_a_planted_edge_among_the_noise():
    rng = random.Random(47)
    m, reps, found, false_claims = 50, 12, 0, 0
    for _ in range(reps):
        led = sp.TrialLedger()
        winner = rng.randrange(m)
        for k in range(m):
            real = k == winner
            n = 600 if real else 120                              # the real edge was given a sample that can show it
            plan = make_plan(n=n, parameters={"k": k}, n_hypotheses_declared=m)
            res, _ = evaluate(plan, [rng.gauss(0.6 if real else 0.0, 1) for _ in range(n)], ledger=led, B=40)
            supported = res["verdict"] == sp.EDGE_SUPPORTED_AFTER_ADJUSTMENT
            found += supported and real
            false_claims += supported and not real
    assert found / reps >= 0.9 and false_claims == 0


def test_a_planted_edge_sized_by_the_framework_is_found_at_about_the_stated_power():
    rng = random.Random(44)
    pilot = [ES.conservative_ess(ar_noise) / 150 for ar_noise in ([rng.gauss(0, 1) for _ in range(150)] for _ in range(60))]
    ratio = statistics.median(pilot)                                  # the conservative ESS ratio the pipeline will measure
    assert 0.6 < ratio < 1.0
    sized = sp.required_observations(1.0, 0.25, alpha=0.05, power=0.8, n_hypotheses=1, ess_ratio=ratio, method="t")
    n = sized["required_raw_n"]
    reps, hits = 500, 0
    predicted = []
    for _ in range(reps):
        res, _ = evaluate(make_plan(n=n), [rng.gauss(0.25, 1) for _ in range(n)])
        hits += res["adjusted_p_value"] <= 0.05
        predicted.append(res["achieved_power"])
    assert abs(hits / reps - 0.8) < 0.07                              # roughly the stated power
    assert abs(hits / reps - statistics.mean(predicted)) < 0.07       # and the reported power is honest about it


def test_a_planted_edge_with_a_margin_is_called_supported_and_the_same_data_without_it_is_not():
    rng = random.Random(45)
    n, reps = 250, 300
    supported = noise_supported = 0
    for _ in range(reps):
        res, _ = evaluate(make_plan(n=n), [rng.gauss(0.25, 1) for _ in range(n)])
        supported += res["verdict"] == sp.EDGE_SUPPORTED_AFTER_ADJUSTMENT
        res0, _ = evaluate(make_plan(n=n), [rng.gauss(0.0, 1) for _ in range(n)])
        noise_supported += res0["verdict"] == sp.EDGE_SUPPORTED_AFTER_ADJUSTMENT
    assert supported / reps > 0.8 and noise_supported / reps < 0.05


def test_a_planted_edge_in_dependent_data_is_found_when_sized_for_the_dependence():
    rng = random.Random(46)
    rho, n, reps = 0.5, 700, 250
    hits = sum(1 for _ in range(reps)
               if evaluate(make_plan(n=n), ar1(rng, n, rho, mean=0.25))[0]["adjusted_p_value"] <= 0.05)
    expected = sp.achieved_power(n, 1.0, 0.25, n_kind="raw", rho=rho, alpha=0.05, n_hypotheses=1, method="t")["power"]
    assert expected > 0.9 and hits / reps > 0.8


# ---------------------------------------------------------------------------------------------
# honest labelling, unknowns, and module hygiene
# ---------------------------------------------------------------------------------------------
def test_every_result_dict_labels_every_field_as_calculated_estimated_or_assumed():
    plan = make_plan(n=200)
    rng = random.Random(50)
    data = [rng.gauss(0.3, 1) for _ in range(200)]
    results = [
        sp.design_effect(rho=0.4),
        req(), req(rho=0.5, method="t", two_sided=True, n_hypotheses=12), req(min_edge=1e-9),
        sp.achieved_power(100, 1, 0.25, n_kind="raw", rho=0.3, alpha=0.05, n_hypotheses=3),
        sp.achieved_power(2.0, 1, 0.25, n_kind="effective", alpha=0.05, n_hypotheses=3, method="t"),
        sp.minimum_detectable_edge(100, 1, n_kind="raw", rho=0.3, alpha=0.05, power=0.8, n_hypotheses=3),
        sp.minimum_detectable_edge(2.0, 1, n_kind="effective", alpha=0.05, power=0.8, n_hypotheses=3, method="t"),
        sp.adjust_pvalues([0.01, 0.2, 0.03], "bh", alpha=0.05),
        sp.selection_adjusted_best(null_ledger(10, seed=1), alpha=0.05),
        sp.deflated_sharpe(0.1, n_obs=200, n_trials=5, sharpe_variance=0.003, skew=0.0, kurtosis=3.0),
        sp.false_discovery_cost(prior=0.1, alpha=0.05, power=0.8, loss_if_false=3.0, gain_if_true=5.0),
        sp.audit_split(*sp.holdout_split(100, 0.2, 3, 3), 100, 3, 3),
        evaluate(plan, data)[0], evaluate(plan, [1.0] * 200)[0],
    ]
    for r in results:
        assert_labelled(r)
    est = evaluate(plan, data)[0]["classes"]
    assert est["p_value"] == sp.ESTIMATED and est["alpha"] == sp.ASSUMED and est["plan_digest"] == sp.CALCULATED
    assert results[1]["classes"]["required_raw_n"] == sp.CALCULATED and results[1]["classes"]["sd"] == sp.ASSUMED


def test_no_unknown_input_is_replaced_by_a_number_anywhere():
    plan = make_plan(n=100)
    calls = [
        lambda: sp.achieved_power(None, 1, 0.25, n_kind="effective", alpha=0.05, n_hypotheses=1),
        lambda: sp.achieved_power(99, None, 0.25, n_kind="effective", alpha=0.05, n_hypotheses=1),
        lambda: sp.achieved_power(99, 1, None, n_kind="effective", alpha=0.05, n_hypotheses=1),
        lambda: sp.minimum_detectable_edge(99, 1, n_kind="effective", alpha=0.05, power=None, n_hypotheses=1),
        lambda: sp.adjust_pvalues(None, "holm", alpha=0.05),
        lambda: sp.expected_max_null(None),
        lambda: sp.posterior_prob_true(None, 0.05, 0.8),
        lambda: sp.purged_embargoed_splits(100, 5, None, 0),
        lambda: sp.holdout_split(100, None, 0, 0),
        lambda: sp.evaluate_under_plan(plan, [(0, 1.0)], sp.mean_statistic, expected_digest=None, ledger=sp.TrialLedger()),
        lambda: sp.TrialLedger().record("s", "h", "w", None),
        lambda: sp.TrialLedger().record(None, "h", "w", 1.0),
    ]
    for call in calls:
        with pytest.raises(sp.StatisticalPowerError):
            call()


def test_the_module_imports_only_the_standard_library_and_evidence_stats_and_does_no_io():
    tree = ast.parse(SRC.read_text(encoding="utf-8"))
    allowed = {"__future__", "dataclasses", "hashlib", "json", "math", "numbers", "statistics", "collections",
               "typing", "evidence_stats"}
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add((node.module or "").split(".")[0])
    assert imported <= allowed, imported - allowed
    banned_calls = {"open", "print", "input", "exec", "eval", "compile", "__import__"}
    banned_names = {"random", "time", "datetime", "os", "sys", "socket", "subprocess", "pathlib", "requests"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            assert node.func.id not in banned_calls, node.func.id
        if isinstance(node, ast.Name):
            assert node.id not in banned_names, node.id


def test_public_functions_are_pure_repeated_calls_agree_and_inputs_are_not_mutated():
    ps = [0.01, 0.04, 0.03, 0.005]
    before = list(ps)
    a = sp.adjust_pvalues(ps, "holm", alpha=0.05)
    b = sp.adjust_pvalues(ps, "holm", alpha=0.05)
    assert a == b and ps == before
    assert sp.purged_embargoed_splits(50, 5, 2, 2) == sp.purged_embargoed_splits(50, 5, 2, 2)
    assert req(method="t") == req(method="t")
    obs = [(i, 0.1 * (i % 4)) for i in range(100)]
    snapshot = list(obs)
    plan = make_plan(n=100)
    sp.evaluate_under_plan(plan, obs, sp.mean_statistic, expected_digest=sp.plan_digest(plan), ledger=sp.TrialLedger())
    assert obs == snapshot
