"""The research harness must make bad strategies die: look-ahead, survivorship, uncounted variants, overfit-by-search, zero costs, leaky splits."""
from __future__ import annotations

import hashlib
import json
import os
import random
import sys
import uuid

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
BOT = os.path.dirname(HERE)
sys.path.insert(0, BOT)
sys.path.insert(0, os.path.join(BOT, "tools"))

import research_harness as RH  # noqa: E402
import statistical_power as SP  # noqa: E402
import strategy_verdict as SV  # noqa: E402

CAND = os.path.join(BOT, "research", "candidates")
DATA = os.path.join(BOT, "data")


def fam() -> str:
    return "t_" + uuid.uuid4().hex


def spec(name="cross_sectional_funding"):
    with open(os.path.join(CAND, name + ".json"), encoding="utf-8") as f:
        return json.load(f)


def pit_universe():
    return RH.PointInTimeUniverse({"AAA": {"listed_ms": 0, "delisted_ms": 5_000}, "BBB": {"listed_ms": 0}, "CCC": {"listed_ms": 1_000, "delisted_ms": 9_000}},
                                  membership_source="exchange_listing_records", includes_delisted=True)


def survivors():
    return RH.PointInTimeUniverse({"BTCUSDT": {"listed_ms": 0}, "ETHUSDT": {"listed_ms": 0}}, membership_source="hand_picked_survivors", includes_delisted=False)


CLEAN = RH.LeakAudit(True, 10, ())
SHA = "ab" * 32


def planted_edge(n=2000, mean=5.0, sd=10.0, seed=1):
    r = random.Random(seed)
    return [r.gauss(mean, sd) for _ in range(n)]


# ------------------------------------------------------------------ universe / survivorship
def test_symbol_list_without_dates_is_refused():
    for bad in (["BTCUSDT", "ETHUSDT"], ("A",), {"A", "B"}):
        with pytest.raises(RH.SurvivorshipError):
            RH.PointInTimeUniverse(bad, membership_source="x", includes_delisted=True)
    with pytest.raises(RH.SurvivorshipError):
        RH.PointInTimeUniverse.from_symbol_list(["BTCUSDT"])
    with pytest.raises(RH.SurvivorshipError):
        RH.PointInTimeUniverse({"A": {}}, membership_source="x", includes_delisted=True)          # no listed_ms
    with pytest.raises(RH.HarnessError):
        RH.PointInTimeUniverse({"A": {"listed_ms": "2020"}}, membership_source="x", includes_delisted=True)


def test_symbol_queried_outside_membership_is_an_error():
    u = pit_universe()
    u.require_active("AAA", 0)
    u.require_active("AAA", 4_999)
    with pytest.raises(RH.UniverseError):
        u.require_active("AAA", 5_000)                       # delisted: [listed, delisted)
    with pytest.raises(RH.UniverseError):
        u.require_active("CCC", 999)                         # before listing
    with pytest.raises(RH.UniverseError):
        u.require_active("ZZZ", 10)
    assert u.active_at(500) == ["AAA", "BBB"] and u.active_at(6_000) == ["BBB", "CCC"]


def test_survivor_universe_is_not_point_in_time_even_with_dates():
    s = survivors()
    assert not s.point_in_time
    assert any("SURVIVOR_BASED" in p for p in s.survivorship_problems())
    assert pit_universe().point_in_time
    no_delistings = RH.PointInTimeUniverse({"AAA": {"listed_ms": 0, "delisted_ms": 5_000}, "BBB": {"listed_ms": 0}}, membership_source="exchange_listing_records",
                                           includes_delisted=False)
    assert not no_delistings.point_in_time and "SOURCE_DOES_NOT_RECORD_DELISTED_SYMBOLS" in no_delistings.survivorship_problems()


def test_survivorship_is_caught_in_the_evidence():
    """A real, planted, significant edge on a survivor universe must NOT be counted."""
    L = RH.VariantLedger(fam())
    st = L.record("v", {"k": 1}, planted_edge())
    ev = RH.historical_evidence(stats=st, selection=L.selection(), universe=survivors(), leak=CLEAN, data_sha256=SHA)
    assert ev.metrics["universe_point_in_time"] is False
    assert ev.result == SV.INCONCLUSIVE, ev
    ok, why = SV.counted(ev, spec())
    assert not ok and why == "INCONCLUSIVE"
    forced = SV.EvidenceItem(ev.stage, SV.PASS, ev.provenance, ev.metrics, ev.data_sha256)
    assert SV.counted(forced, spec()) == (False, "UNIVERSE_NOT_POINT_IN_TIME")


def test_positive_control_a_clean_planted_edge_is_counted():
    """The guards are not simply always-on: clean data + real edge + PIT universe -> PASS and counted."""
    L = RH.VariantLedger(fam())
    st = L.record("v", {"k": 1}, planted_edge())
    ev = RH.historical_evidence(stats=st, selection=L.selection(), universe=pit_universe(), leak=CLEAN, data_sha256=SHA)
    assert ev.result == SV.PASS, ev.metrics
    assert SV.counted(ev, spec()) == (True, None)
    m = ev.metrics
    assert m["uses_future_information"] is False and m["universe_point_in_time"] is True and m["multiple_testing_adjusted"] is True
    assert m["independent_observations"] >= 100 and m["n_variants_tested"] == 1


# ------------------------------------------------------------------ look-ahead
def test_asof_view_cannot_contain_or_read_the_future():
    ts = list(range(0, 1000, 10))
    vals = [float(i) for i in range(len(ts))]
    s = RH.PITSeries(ts, vals)
    v = s.asof(495)
    assert v.timestamps()[-1] == 490 and len(v) == 50 and max(v.values()) == 49.0
    assert v.at(490) == 49.0
    for t in (500, 505, 990):
        with pytest.raises(RH.LookAheadError):
            v.at(t)
    with pytest.raises(IndexError):
        v[len(v)]
    with pytest.raises(AttributeError):
        v.cutoff_ms = 10 ** 9                                  # cannot widen the window
    assert not hasattr(v, "asof") and not hasattr(s, "values") and not hasattr(s, "__getitem__")
    assert len(s.asof(10 ** 9)) == len(ts) and len(s.asof(-1)) == 0


def test_hostile_future_read_fails():
    """A feature function that tries every route to the future gets only the past."""
    s = RH.PITSeries(list(range(100)), [float(i) for i in range(100)])
    v = s.asof(40)
    seen = []
    for probe in (lambda: v.at(41), lambda: v.at(99), lambda: v[41], lambda: v.values()[41], lambda: v.timestamps()[41], lambda: v.last(1000)[41]):
        try:
            seen.append(probe())
        except (RH.LookAheadError, IndexError):
            pass
    assert seen == [], f"future values leaked: {seen}"


def test_detect_lookahead_catches_a_leaking_feature_and_passes_a_causal_one():
    ts = list(range(300))
    r = random.Random(7)
    vals = [r.gauss(0, 1) for _ in ts]

    def centered_mean(t, v):                                    # uses the next 3 rows: LEAK
        return [sum(v[max(0, i - 3):i + 4]) / len(v[max(0, i - 3):i + 4]) for i in range(len(v))]

    def shifted_forward(t, v):                                  # label leaked as a feature
        return [v[i + 1] if i + 1 < len(v) else float("nan") for i in range(len(v))]

    def trailing_mean(t, v):
        return [sum(v[max(0, i - 8):i + 1]) / len(v[max(0, i - 8):i + 1]) for i in range(len(v))]

    assert not RH.detect_lookahead(centered_mean, ts, vals).passed
    assert not RH.detect_lookahead(shifted_forward, ts, vals).passed
    ok = RH.detect_lookahead(trailing_mean, ts, vals)
    assert ok.passed and ok.n_probes > 10


def test_look_ahead_is_caught_in_the_evidence():
    L = RH.VariantLedger(fam())
    st = L.record("v", {"k": 1}, planted_edge())
    leaky = RH.LeakAudit(False, 50, (3, 9))
    ev = RH.historical_evidence(stats=st, selection=L.selection(), universe=pit_universe(), leak=leaky, data_sha256=SHA)
    assert ev.metrics["uses_future_information"] is True and ev.result == SV.INCONCLUSIVE
    forced = SV.EvidenceItem(ev.stage, SV.PASS, ev.provenance, ev.metrics, ev.data_sha256)
    assert SV.counted(forced, spec()) == (False, "LOOK_AHEAD_NOT_RULED_OUT")


# ------------------------------------------------------------------ variant counter
def test_variant_counter_is_monotone_counts_rejected_and_cannot_reset():
    f = fam()
    L = RH.VariantLedger(f)
    r = random.Random(3)
    seen = []
    for i in range(5):
        L.record(f"v{i}", {"i": i}, [r.gauss(0, 1) for _ in range(100)])
        seen.append(L.n_variants)
    L.record_rejected("bad_data", {"i": 99}, "failed sanity check")
    L.record_rejected("overfit", {"i": 100}, "dropped after peeking")
    seen.append(L.n_variants)
    assert seen == [1, 2, 3, 4, 5, 7], seen
    for name in ("reset", "clear", "remove", "pop", "decrement"):
        with pytest.raises(RH.HarnessError):
            getattr(L, name)()
    # a fresh object for the same family does not erase the search
    L2 = RH.VariantLedger(f)
    assert L2.carried_prior_variants == 7 and L2.n_variants == 7
    L2.record("v_new", {"i": 5}, [r.gauss(0, 1) for _ in range(100)])
    assert L2.n_variants == 8 and RH.VariantLedger(f).n_variants == 8
    assert L.n_variants == 8                                    # the old object sees the family high-water mark too
    # tampering is detected, not silently accepted
    L2._tl._entries.pop()
    with pytest.raises(SP.LedgerTamperError):
        L2.n_variants
    # an empty/anonymous family is refused
    with pytest.raises(RH.HarnessError):
        RH.VariantLedger("")


def test_rejected_variants_raise_the_bar():
    f1, f2 = fam(), fam()
    x = planted_edge(n=400, mean=1.2, sd=10.0, seed=5)
    a = RH.VariantLedger(f1)
    a.record("best", {"k": 0}, x)
    b = RH.VariantLedger(f2)
    b.record("best", {"k": 0}, x)
    for i in range(30):
        b.record_rejected(f"dropped{i}", {"i": i}, "silently discarded")
    pa, pb = a.selection()["best_p_adjusted"], b.selection()["best_p_adjusted"]
    assert b.n_variants == 31 and pb > pa, (pa, pb)
    assert pb == min(1.0, 31 * b.selection()["best_p_unadjusted"])


def test_a_fresh_ledger_for_the_same_family_adjusts_over_the_inherited_search():
    f = fam()
    old = RH.VariantLedger(f)
    r = random.Random(9)
    for i in range(10):
        old.record(f"v{i}", {"i": i}, [r.gauss(0, 1) for _ in range(200)])
    new = RH.VariantLedger(f)
    new.record("again", {"i": 10}, planted_edge(n=300, mean=1.0, sd=10.0, seed=2))
    sel = new.selection()
    assert sel["n_variants_tested"] == 11
    assert sel["best_p_adjusted"] == pytest.approx(min(1.0, 11 * sel["best_p_unadjusted"]), rel=1e-9) and sel["best_p_adjusted"] > sel["best_p_unadjusted"]


# ------------------------------------------------------------------ overfit by search
def test_overfit_by_search_pure_noise_is_not_significant_after_correction():
    """200 pure-noise variants. Hand-checked bound: P(min p <= 0.05) = 1 - 0.95**200 > 0.9999, so some variant always looks 'significant' unadjusted;
    Holm over 200 controls the family-wise error at 5%, so the best one must NOT survive (and across 20 seeds at most 3 may, binomial P(>=4 | 20, .05) = 0.016)."""
    unadj_hits = holm_hits = 0
    first = None
    for seed in range(20):
        r = random.Random(1000 + seed)
        L = RH.VariantLedger(fam())
        for j in range(200):
            L.record(f"noise{j}", {"j": j}, [r.gauss(0, 1) for _ in range(500)])
        sel = L.selection(alpha=0.05, method="holm")
        unadj_hits += sel["best_p_unadjusted"] < 0.05
        holm_hits += sel["significant_after_adjustment"]
        first = first or sel
        assert sel["n_variants_tested"] == 200
        assert sel["best_p_adjusted"] == pytest.approx(min(1.0, 200 * sel["best_p_unadjusted"]), rel=1e-9)
    assert unadj_hits >= 19, f"unadjusted best looked significant in only {unadj_hits}/20 searches"
    assert holm_hits <= 3, f"Holm let {holm_hits}/20 pure-noise searches through (first: {first})"
    assert not first["significant_after_adjustment"], first
    # and the evidence built from the noise winner is never a PASS
    r = random.Random(1000)
    L = RH.VariantLedger(fam())
    rets = {}
    for j in range(200):
        rets[j] = [r.gauss(0, 1) for _ in range(500)]
        L.record(f"noise{j}", {"j": j}, rets[j])
    sel = L.selection()
    ev = RH.historical_evidence(stats=RH.significance(rets[int(sel["best_variant"][5:])]), selection=sel, universe=pit_universe(), leak=CLEAN, data_sha256=SHA)
    assert ev.result != SV.PASS, f"noise winner passed: {sel}"


def test_bh_is_less_strict_than_holm_but_still_controls_noise():
    r = random.Random(11)
    L = RH.VariantLedger(fam())
    for j in range(100):
        L.record(f"n{j}", {"j": j}, [r.gauss(0, 1) for _ in range(300)])
    assert not L.selection(method="bh")["significant_after_adjustment"]
    with pytest.raises(RH.HarnessError):
        L.selection(method="none")


# ------------------------------------------------------------------ walk-forward
def test_walk_forward_windows_disjoint_with_embargo_and_no_overlap():
    n, h, e = 1000, 5, 7
    folds = RH.walk_forward_folds(n, train_min=300, test_len=100, label_horizon=h, embargo=e)
    assert len(folds) >= 5
    tests = [set(f.test) for f in folds]
    for i in range(len(tests)):
        for j in range(i + 1, len(tests)):
            assert not (tests[i] & tests[j]), "test windows overlap"
    for f in folds:
        assert max(f.train) < min(f.test)
        assert min(f.test) - max(f.train) - 1 >= h + e, "purge+embargo gap not respected"
        assert not (set(f.train) & set(f.test))
        assert SP.audit_split(list(f.train), list(f.test), n, h, e)["clean"]
    assert folds[0].test[-1] + 1 == folds[1].test[0]            # contiguous, no holes either


def test_walk_forward_refuses_a_short_embargo():
    with pytest.raises(RH.HarnessError):
        RH.walk_forward_folds(1000, 300, 100, label_horizon=5, embargo=2)
    with pytest.raises(RH.HarnessError):
        RH.walk_forward_folds(1000, 300, 100, label_horizon=0, embargo=0)
    with pytest.raises(SP.InsufficientSplitError):
        RH.walk_forward_folds(50, 300, 100, 5, 5)


def test_oos_evidence_flags_a_leaky_split():
    L = RH.VariantLedger(fam())
    L.record("v", {"k": 1}, planted_edge())
    folds = RH.walk_forward_folds(1000, 300, 100, 5, 5)
    leaky = [RH.Fold(tuple(range(0, 450)), folds[0].test)] + folds[1:]        # train reaches into the test window
    ok = RH.oos_evidence(oos_returns=planted_edge(), ledger=L, universe=pit_universe(), leak=CLEAN, folds=folds, n=1000, label_horizon=5, embargo=5, data_sha256=SHA)
    bad = RH.oos_evidence(oos_returns=planted_edge(), ledger=L, universe=pit_universe(), leak=CLEAN, folds=leaky, n=1000, label_horizon=5, embargo=5, data_sha256=SHA)
    assert ok.result == SV.PASS and ok.metrics["uses_future_information"] is False, ok.metrics
    assert bad.metrics["uses_future_information"] is True and bad.result != SV.PASS


# ------------------------------------------------------------------ costs
GOOD = {"fee_bps": {"value": 5.5, "provenance": "assumed"}, "slippage_bps": {"value": 2.0, "provenance": "assumed"}}


def test_net_edge_arithmetic_and_labels():
    r = RH.net_edge_bps(20.0, GOOD)
    assert r["net_edge_bps_after_costs"] == pytest.approx(12.5) and r["unknown_cost_inputs"] == 0
    assert RH.net_edge_bps(3.0, GOOD)["net_edge_bps_after_costs"] < 0
    z = dict(GOOD, fee_bps={"value": 0.0, "provenance": "venue_stated"})
    assert RH.net_edge_bps(10.0, z)["net_edge_bps_after_costs"] == pytest.approx(8.0)            # a stated zero is allowed
    z = dict(GOOD, slippage_bps={"value": 0, "provenance": "measured"})
    assert RH.net_edge_bps(10.0, z)["net_edge_bps_after_costs"] == pytest.approx(4.5)


@pytest.mark.parametrize("mut", [
    {"fee_bps": {"value": 0.0, "provenance": "assumed"}}, {"fee_bps": {"value": 0.0, "provenance": "modelled"}}, {"fee_bps": {"value": 0, "provenance": "derived"}},
    {"slippage_bps": {"value": 0.0, "provenance": "assumed"}}, {"fee_bps": {"value": 5.5}}, {"fee_bps": {"value": 5.5, "provenance": "guess"}},
    {"fee_bps": {"value": 5.5, "provenance": "unknown"}}, {"fee_bps": {"value": None, "provenance": "assumed"}}, {"fee_bps": {"value": float("nan"), "provenance": "measured"}},
    {"fee_bps": {"value": -1.0, "provenance": "measured"}}, {"fee_bps": {"value": True, "provenance": "measured"}}, {"fee_bps": 5.5}])
def test_bad_costs_are_refused(mut):
    with pytest.raises(RH.CostError):
        RH.net_edge_bps(20.0, dict(GOOD, **mut))


def test_missing_cost_components_are_refused():
    for k in ("fee_bps", "slippage_bps"):
        c = dict(GOOD)
        del c[k]
        with pytest.raises(RH.CostError):
            RH.net_edge_bps(20.0, c)
    with pytest.raises(RH.CostError):
        RH.net_edge_bps(float("inf"), GOOD)


# ------------------------------------------------------------------ data hash
def test_data_hash_is_the_files_sha256(tmp_path):
    p = tmp_path / "x.csv"
    p.write_bytes(b"a,b\n1,2\n")
    assert RH.sha256_files([str(p)]) == hashlib.sha256(b"a,b\n1,2\n").hexdigest()
    q = tmp_path / "y.csv"
    q.write_bytes(b"z")
    assert RH.sha256_files([str(p), str(q)]) == RH.sha256_files([str(q), str(p)]) != RH.sha256_files([str(p)])
    with pytest.raises(RH.HarnessError):
        RH.sha256_files([])


# ------------------------------------------------------------------ the demonstration: cross-sectional funding on 3 survivors
def test_cross_sectional_funding_demo_is_killed_honestly():
    r = RH.cross_sectional_funding_demo(DATA, family=fam())
    h, o, sel = r["historical"], r["oos"], r["selection"]
    numbers = (f"settlements={r['grid_settlements']} variants={sel['n_variants_tested']} best={sel['best_variant']} p_unadj={sel['best_p_unadjusted']:.4f} "
               f"p_holm={sel['best_p_adjusted']:.4f} hist={h.result} net_mean_bps={h.metrics['mean_net_per_settlement_bps']:.3f} ess={h.metrics['ess']:.0f} "
               f"oos={o.result} oos_net_mean_bps={o.metrics['mean_net_per_settlement_bps']:.3f} oos_p_adj={o.metrics['p_adjusted']:.4f} oos_ess={o.metrics['ess']:.0f} "
               f"folds={r['n_folds']} pit={r['universe_point_in_time']} problems={r['survivorship_problems']}")
    print("\nDEMO", numbers)
    assert r["universe_point_in_time"] is False, numbers
    assert h.metrics["universe_point_in_time"] is False and o.metrics["universe_point_in_time"] is False, numbers
    assert h.metrics["uses_future_information"] is False and o.metrics["uses_future_information"] is False, numbers     # causal: the signal reads asof(prev)
    assert sel["n_variants_tested"] == 15 and h.metrics["n_variants_tested"] == 15, numbers
    assert len(h.data_sha256) == 64
    v = SV.evaluate(spec("cross_sectional_funding"), [h, o])
    assert v.verdict in (SV.INSUFFICIENT_DATA, SV.REJECTED), f"{v.verdict} {v.reasons} | {numbers}"
    assert v.verdict != SV.PROMISING, numbers
    assert v.stages["cost_model"] == "MISSING" and v.stages["capacity_test"] == "MISSING", numbers
    # the best in-sample variant does not survive correction over the 15 that were tried
    assert sel["significant_after_adjustment"] is False, numbers
    assert sel["best_p_adjusted"] > 0.05 and sel["best_p_unadjusted"] < sel["best_p_adjusted"], numbers


def test_demo_cannot_be_promoted_even_if_the_numbers_were_great():
    """Replace the demo's evidence with a significant one: the non-PIT universe still keeps it from counting."""
    r = RH.cross_sectional_funding_demo(DATA, family=fam())
    forged = SV.EvidenceItem("historical_test", SV.PASS, "offline_backtest", dict(r["historical"].metrics, p_adjusted=0.0001), r["historical"].data_sha256)
    v = SV.evaluate(spec("cross_sectional_funding"), [forged])
    assert v.verdict == SV.INSUFFICIENT_DATA and v.stages["historical_test"] == "NOT_COUNTED:UNIVERSE_NOT_POINT_IN_TIME", (v.verdict, v.stages)
