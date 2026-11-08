"""The research harness must make bad strategies die: look-ahead, survivorship, uncounted variants, overfit-by-search, zero costs, leaky splits."""
from __future__ import annotations

import dataclasses
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


def spec(name="cross_sectional_funding", family=None):
    """A candidate spec. The shipped candidates truthfully declare biases they cannot yet rule out (the repo lacks the data), so for TESTING the evidence
    machinery the declarations are set True on a COPY; ``family`` binds the spec to a ledger's family (evidence refuses a ledger of another family)."""
    with open(os.path.join(CAND, name + ".json"), encoding="utf-8") as f:
        sp = json.load(f)
    sp["bias_declarations"] = {k: True for k in SV.BIAS_DECLARATIONS}
    for item in sp.get("required_data", []):
        if isinstance(item, dict):
            item["repo_has"] = True                                         # the COPY pretends the data exists, to exercise the evidence machinery only
    if family is not None:
        sp["multiple_testing_family"] = family
    return sp


def spec_for(ledger, name="cross_sectional_funding"):
    return spec(name, family=ledger.family)


def pit_universe():
    return RH.PointInTimeUniverse({"AAA": {"listed_ms": 0, "delisted_ms": 5_000}, "BBB": {"listed_ms": 0}, "CCC": {"listed_ms": 1_000, "delisted_ms": 9_000}},
                                  membership_source=PIT_SRC, includes_delisted=True, observed_from_ms=0, observed_to_ms=10_000)


def survivors():
    return RH.PointInTimeUniverse({"BTCUSDT": {"listed_ms": 0}, "ETHUSDT": {"listed_ms": 0}}, membership_source="hand_picked_survivors", includes_delisted=False)


CLEAN = RH.LeakAudit(True, 10, ())
SHA = hashlib.sha256(b"harness fixture input data").hexdigest()
PIT_SRC = "exchange_instruments_info_snapshots_with_status_history"


def planted_edge(n=2000, mean=5.0, sd=10.0, seed=1):
    r = random.Random(seed)
    return [r.gauss(mean, sd) for _ in range(n)]



# ------------------------------------------------------------------ helpers for the train-only selection log
N_ROWS = 1000


def variant_rets():
    return {"v": planted_edge(n=N_ROWS, mean=5.0, seed=1), "w": planted_edge(n=N_ROWS, mean=4.0, seed=2)}


def honest_log(folds, vr, off=0):
    log, oos = [], []
    for k, f in enumerate(folds):
        means = {n: sum(r[off + t] for t in f.train) / len(f.train) for n, r in vr.items()}
        pick = max(means, key=lambda n: (means[n], n))
        log.append({"fold": k, "chosen": pick, "train_window": [f.train[0], f.train[-1]]})
        oos.extend(vr[pick][off + t] for t in f.test)
    return log, oos


def oos_ev(L, folds, vr=None, log=None, oos=None, **kw):
    vr = vr or variant_rets()
    hl, ho = honest_log(folds, vr)
    return RH.oos_evidence(spec=spec_for(L), oos_returns=oos if oos is not None else ho, ledger=L, universe=pit_universe(), leak=CLEAN, folds=folds,
                           data_sha256=SHA, selection_log=log if log is not None else hl, variant_returns=vr, **kw)


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
    no_delistings = RH.PointInTimeUniverse({"AAA": {"listed_ms": 0, "delisted_ms": 5_000}, "BBB": {"listed_ms": 0}}, membership_source=PIT_SRC,
                                           includes_delisted=False, observed_from_ms=0, observed_to_ms=10_000)
    assert not no_delistings.point_in_time and "SOURCE_DOES_NOT_RECORD_DELISTED_SYMBOLS" in no_delistings.survivorship_problems()


def test_survivorship_is_caught_in_the_evidence():
    """A real, planted, significant edge on a survivor universe must NOT be counted."""
    L = RH.VariantLedger(fam())
    st = L.record("v", {"k": 1}, planted_edge())
    ev = RH.historical_evidence(spec=spec_for(L), stats=st, ledger=L, universe=survivors(), leak=CLEAN, data_sha256=SHA)
    assert ev.metrics["universe_point_in_time"] is False
    assert ev.result == SV.INCONCLUSIVE, ev
    ok, why = SV.counted(ev, spec_for(L))
    assert not ok and why == "INCONCLUSIVE"
    forced = dataclasses.replace(ev, result=SV.PASS)             # same stamps, forced PASS: only the metrics can stop it
    assert SV.counted(forced, spec_for(L)) == (False, "UNIVERSE_NOT_POINT_IN_TIME")


def test_positive_control_a_clean_planted_edge_is_counted():
    """The guards are not simply always-on: clean data + real edge + PIT universe -> PASS and counted."""
    L = RH.VariantLedger(fam())
    st = L.record("v", {"k": 1}, planted_edge())
    ev = RH.historical_evidence(spec=spec_for(L), stats=st, ledger=L, universe=pit_universe(), leak=CLEAN, data_sha256=SHA)
    assert ev.result == SV.PASS, ev.metrics
    assert SV.counted(ev, spec_for(L)) == (True, None)
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
    ev = RH.historical_evidence(spec=spec_for(L), stats=st, ledger=L, universe=pit_universe(), leak=leaky, data_sha256=SHA)
    assert ev.metrics["uses_future_information"] is True and ev.result == SV.INCONCLUSIVE
    forced = dataclasses.replace(ev, result=SV.PASS)             # same stamps, forced PASS: only the metrics can stop it
    assert SV.counted(forced, spec_for(L)) == (False, "LOOK_AHEAD_NOT_RULED_OUT")


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
    ev = RH.historical_evidence(spec=spec_for(L), stats=RH.significance(rets[int(sel["best_variant"][5:])]), ledger=L, universe=pit_universe(), leak=CLEAN, data_sha256=SHA)
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


def test_oos_evidence_refuses_a_leaky_split():
    L = RH.VariantLedger(fam())
    L.record("v", {"k": 1}, planted_edge(n=N_ROWS), horizon=5)
    L.record("w", {"k": 2}, planted_edge(n=N_ROWS, mean=4.0, seed=2), horizon=5)
    folds = RH.walk_forward_folds(1000, 300, 100, 5, 5)
    leaky = [dataclasses.replace(folds[0], train=tuple(range(0, 450)))] + folds[1:]        # train reaches into the test window (the stamp no longer matches)
    ok = oos_ev(L, folds)
    assert ok.result == SV.PASS and ok.metrics["uses_future_information"] is False, ok.metrics
    with pytest.raises(RH.HarnessError, match="walk_forward_folds"):
        oos_ev(L, leaky)
    handmade = [RH.Fold(tuple(range(0, 450)), folds[0].test)] + folds[1:]
    with pytest.raises(RH.HarnessError, match="walk_forward_folds"):
        oos_ev(L, handmade)


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
    f = fam()
    sp = spec(family=f)
    r = RH.cross_sectional_funding_demo(DATA, family=f, spec=sp)
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
    v = SV.evaluate(sp, [h, o])
    assert v.verdict in (SV.INSUFFICIENT_DATA, SV.REJECTED), f"{v.verdict} {v.reasons} | {numbers}"
    assert v.verdict != SV.PROMISING, numbers
    assert v.stages["cost_model"] == "MISSING" and v.stages["capacity_test"] == "MISSING", numbers
    # the best in-sample variant does not survive correction over the 15 that were tried
    assert sel["significant_after_adjustment"] is False, numbers
    assert sel["best_p_adjusted"] > 0.05 and sel["best_p_unadjusted"] < sel["best_p_adjusted"], numbers


def test_demo_cannot_be_promoted_even_if_the_numbers_were_great():
    """Replace the demo's evidence with a significant one: the non-PIT universe still keeps it from counting."""
    f = fam()
    sp = spec(family=f)
    r = RH.cross_sectional_funding_demo(DATA, family=f, spec=sp)
    forged = dataclasses.replace(r["historical"], result=SV.PASS, metrics=dict(r["historical"].metrics, p_adjusted=0.0001))
    v = SV.evaluate(sp, [forged])
    assert v.verdict == SV.INSUFFICIENT_DATA and v.stages["historical_test"] == "NOT_COUNTED:UNIVERSE_NOT_POINT_IN_TIME", (v.verdict, v.stages)


# ================================================================== red-team round
# ---- (6) family key normalisation and persistence
@pytest.mark.parametrize("a,b", [("fam", "fam "), ("fam", "FAM"), ("fam x", "  Fam   X\t"), ("Straße", "STRASSE")])
def test_family_key_is_normalised(a, b):
    a, b = a + uuid.uuid4().hex[:6], b + uuid.uuid4().hex[:6]
    sfx = uuid.uuid4().hex
    A, B = RH.VariantLedger(a.split()[0][:0] + "fx_" + sfx), RH.VariantLedger(" FX_" + sfx.upper() + " ")
    assert A.family == B.family == "fx_" + sfx
    A.record_rejected("n", {"i": 1}, "r")
    assert B.n_variants == 1 and RH.VariantLedger("Fx_" + sfx).n_variants == 1


def test_family_spellings_share_one_count_and_empty_is_refused():
    sfx = uuid.uuid4().hex
    one = RH.VariantLedger(f"My Fam {sfx}")
    for i in range(3):
        one.record_rejected(f"r{i}", {"i": i}, "x")
    for spelling in (f"my fam {sfx}", f"MY   FAM {sfx} ", f"\tmy\u00a0fam {sfx}"):
        assert RH.VariantLedger(spelling).n_variants == 3, spelling
    for bad in ("", "   ", "\t\n", None, 5):
        with pytest.raises(RH.HarnessError):
            RH.VariantLedger(bad)


def _restart():
    RH._FAMILY_HIGH_WATER.clear()
    RH._PATH_HIGH_WATER.clear()


def test_ledger_persists_across_a_restart_and_only_grows(tmp_path):
    p = str(tmp_path / "variants.jsonl")
    f = fam()
    r = random.Random(4)
    L = RH.VariantLedger(f, path=p)
    for i in range(3):
        L.record(f"v{i}", {"i": i}, [r.gauss(0, 1) for _ in range(100)])
    L.record_rejected("bad", {"i": 9}, "failed sanity")
    assert L.n_variants == 4
    sel_before = L.selection()
    _restart()                                                          # a new process: the in-memory counter is gone
    L2 = RH.VariantLedger(f.upper(), path=p)
    assert L2.n_variants == 4 and L2.selection() == sel_before
    L2.record("v_new", {"i": 5}, [r.gauss(0, 1) for _ in range(100)])
    assert L2.n_variants == 5
    _restart()
    assert RH.VariantLedger(f, path=p).n_variants == 5
    # a ledger for another family cannot adopt this file
    with pytest.raises(RH.HarnessError):
        RH.VariantLedger(fam(), path=p)


def test_persisted_ledger_detects_truncation_and_edits(tmp_path):
    import shutil
    p = str(tmp_path / "v.jsonl")
    f = fam()
    L = RH.VariantLedger(f, path=p)
    r = random.Random(5)
    for i in range(4):
        L.record(f"v{i}", {"i": i}, [r.gauss(0, 1) for _ in range(100)])
    good = {n: open(p + n, "rb").read() for n in ("", ".chain", ".head")}

    def reset():
        _restart()
        for n, b in good.items():
            open(p + n, "wb").write(b)

    reset()
    assert RH.VariantLedger(f, path=p).n_variants == 4                  # positive control
    # 1. last row dropped from the data file only
    reset()
    rows = good[""].split(b"\n")[:-1]
    open(p, "wb").write(b"\n".join(rows[:-1]) + b"\n")
    with pytest.raises(RH.HarnessError):
        RH.VariantLedger(f, path=p)
    # 2. torn (half-written) last line
    reset()
    open(p, "wb").write(good[""][:-20])
    with pytest.raises(RH.HarnessError):
        RH.VariantLedger(f, path=p)
    # 3. a row edited in place (the p-value made better)
    reset()
    open(p, "wb").write(good[""].replace(b'"n_obs":100', b'"n_obs":101', 1))
    with pytest.raises(RH.HarnessError):
        RH.VariantLedger(f, path=p)
    # 4. a middle row deleted
    reset()
    open(p, "wb").write(b"\n".join(rows[:1] + rows[2:]) + b"\n")
    with pytest.raises(RH.HarnessError):
        RH.VariantLedger(f, path=p)
    # 5. data file, sidecar and head all truncated together: internally consistent, caught by the caller's external anchor
    reset()
    chain = good[".chain"].split(b"\n")[:-1]
    open(p, "wb").write(b"\n".join(rows[:2]) + b"\n")
    open(p + ".chain", "wb").write(b"\n".join(chain[:2]) + b"\n")
    import json as _j
    open(p + ".head", "w").write(_j.dumps({"n": 2, "head": _j.loads(chain[1])["h"]}, sort_keys=True))
    anchor = {"n": 4, "head": _j.loads(chain[3])["h"]}
    with pytest.raises(RH.HarnessError):
        RH.VariantLedger(f, path=p, anchors=[anchor])
    # 6. in-process: a file shorter than the count this process already saw for it is refused even without an anchor
    reset()
    RH.VariantLedger(f, path=p)
    open(p, "wb").write(b"\n".join(rows[:2]) + b"\n")
    open(p + ".chain", "wb").write(b"\n".join(chain[:2]) + b"\n")
    open(p + ".head", "w").write(_j.dumps({"n": 2, "head": _j.loads(chain[1])["h"]}, sort_keys=True))
    with pytest.raises(RH.HarnessError):
        RH.VariantLedger(f, path=p)


def test_evidence_takes_n_variants_from_the_ledger_not_a_free_integer():
    L = RH.VariantLedger(fam())
    st = L.record("v", {"k": 1}, planted_edge())
    for i in range(4):
        L.record_rejected(f"r{i}", {"i": i}, "dropped")
    with pytest.raises(TypeError):
        RH.historical_evidence(spec=spec_for(L), stats=st, selection={"best_p_adjusted": 0.0, "alpha": 0.05, "n_variants_tested": 1, "best_variant": "v"},
                               universe=pit_universe(), leak=CLEAN, data_sha256=SHA)
    ev = RH.historical_evidence(spec=spec_for(L), stats=st, ledger=L, universe=pit_universe(), leak=CLEAN, data_sha256=SHA)
    assert ev.metrics["n_variants_tested"] == 5 == L.n_variants
    # stats that are not the ledger's best variant's are refused
    with pytest.raises(RH.HarnessError):
        RH.historical_evidence(spec=spec_for(L), stats=RH.significance(planted_edge(seed=77)), ledger=L, universe=pit_universe(), leak=CLEAN, data_sha256=SHA)
    with pytest.raises(TypeError):
        RH._build("historical_test", stats=st, p_adjusted=0.0, alpha=0.05, n_variants=1, universe=pit_universe(), leak=CLEAN, data_sha256=SHA,
                  provenance="x", split_clean=True, extra={})


# ---- (7) survivorship
FAKE_DELIST = {"AAA": {"listed_ms": 0, "delisted_ms": 5_000}, "BBB": {"listed_ms": 0}}


def test_arbitrary_membership_source_is_not_point_in_time():
    for src in ("trust_me", "exchange_listing_records", "survivors_but_with_delistings", PIT_SRC.upper(), PIT_SRC + "x"):
        u = RH.PointInTimeUniverse(FAKE_DELIST, membership_source=src, includes_delisted=True, observed_from_ms=0, observed_to_ms=10_000)
        assert not u.point_in_time and any("NOT_ALLOWLISTED" in p for p in u.survivorship_problems()), src
    for src in RH.PIT_SOURCE_KINDS:
        assert RH.PointInTimeUniverse(FAKE_DELIST, membership_source=src, includes_delisted=True, observed_from_ms=0, observed_to_ms=10_000).point_in_time
    assert set(RH.PIT_SOURCE_KINDS) >= {"exchange_instruments_info_snapshots_with_status_history", "recorded_universe_snapshots"}


def test_pit_needs_an_observed_window_and_a_delisting_inside_it_or_an_attestation():
    def mk(members, **kw):
        return RH.PointInTimeUniverse(members, membership_source=PIT_SRC, includes_delisted=True, **kw)
    assert "OBSERVED_WINDOW_MISSING" in mk(FAKE_DELIST).survivorship_problems()
    assert mk(FAKE_DELIST, observed_from_ms=0, observed_to_ms=10_000).point_in_time                       # control
    # the one delisting is dated OUTSIDE the observed window: not evidence the source records delistings in it
    out = mk(FAKE_DELIST, observed_from_ms=6_000, observed_to_ms=10_000)
    assert not out.point_in_time and any("NO_DELISTING_EVENT_IN_OBSERVED_WINDOW" in p for p in out.survivorship_problems())
    nobody = {"AAA": {"listed_ms": 0}, "BBB": {"listed_ms": 0}}
    assert not mk(nobody, observed_from_ms=0, observed_to_ms=10_000).point_in_time
    h1, h2 = hashlib.sha256(b"s1").hexdigest(), hashlib.sha256(b"s2").hexdigest()
    att = {"no_delistings_observed_in_window": True, "window_from_ms": 0, "window_to_ms": 10_000, "snapshot_sha256": [h1, h2]}
    assert mk(nobody, observed_from_ms=0, observed_to_ms=10_000, no_delistings_attestation=att).point_in_time
    for bad in (dict(att, snapshot_sha256=[]), {"no_delistings_observed_in_window": True}, dict(att, snapshot_sha256=["nothex", h2]),
                dict(att, no_delistings_observed_in_window=False), dict(att, no_delistings_observed_in_window="yes")):
        assert not mk(nobody, observed_from_ms=0, observed_to_ms=10_000, no_delistings_attestation=bad).point_in_time, bad
    # an attestation that contradicts a delisting present in the window is not accepted
    assert not mk(FAKE_DELIST, observed_from_ms=0, observed_to_ms=10_000, no_delistings_attestation=att).point_in_time
    with pytest.raises(RH.HarnessError):
        mk(FAKE_DELIST, observed_from_ms=10, observed_to_ms=10)
    with pytest.raises(RH.HarnessError):
        mk(FAKE_DELIST, observed_from_ms=0)


def test_fake_delisted_symbol_with_arbitrary_source_cannot_make_a_pit_pass():
    L = RH.VariantLedger(fam())
    st = L.record("v", {"k": 1}, planted_edge())
    u = RH.PointInTimeUniverse(FAKE_DELIST, membership_source="whatever_i_say", includes_delisted=True, observed_from_ms=0, observed_to_ms=10_000)
    ev = RH.historical_evidence(spec=spec_for(L), stats=st, ledger=L, universe=u, leak=CLEAN, data_sha256=SHA)
    assert ev.metrics["universe_point_in_time"] is False and ev.result == SV.INCONCLUSIVE


# ---- (8) selection log: train-only selection is recomputed
def _two_variant_ledger():
    L = RH.VariantLedger(fam())
    for n, r in variant_rets().items():
        L.record(n, {"n": n}, r, horizon=5)
    return L


def test_oos_with_an_honest_selection_log_passes():
    folds = RH.walk_forward_folds(N_ROWS, 300, 100, 5, 5)
    ev = oos_ev(_two_variant_ledger(), folds)
    assert ev.result == SV.PASS and ev.metrics["selection_log_verified"] is True and ev.metrics["n_variants_tested"] == 2


def test_oos_refuses_a_selection_not_a_function_of_the_train_window():
    folds = RH.walk_forward_folds(N_ROWS, 300, 100, 5, 5)
    L = _two_variant_ledger()
    vr = variant_rets()
    log, oos = honest_log(folds, vr)
    # control: the log picks both variants somewhere or at least is consistent
    other = "w" if log[1]["chosen"] == "v" else "v"
    # (a) a variant picked with hindsight (the one that wins on the TEST window), logged as if chosen on train
    peek = [dict(e) for e in log]
    peek[1]["chosen"] = other
    oos_peek = []
    for k, f in enumerate(folds):
        oos_peek.extend(vr[peek[k]["chosen"]][t] for t in f.test)
    with pytest.raises(RH.HarnessError):
        oos_ev(L, folds, vr, log=peek, oos=oos_peek)
    # (b) honest log but returns that are not the chosen variants' test returns
    with pytest.raises(RH.HarnessError):
        oos_ev(L, folds, vr, log=log, oos=[x + 1.0 for x in oos])
    with pytest.raises(RH.HarnessError):
        oos_ev(L, folds, vr, log=log, oos=oos[:-1])
    # (c) a log that names a train window other than the fold's
    wrong = [dict(e) for e in log]
    wrong[0]["train_window"] = [0, wrong[0]["train_window"][1] + 50]
    with pytest.raises(RH.HarnessError):
        oos_ev(L, folds, vr, log=wrong)
    # (d) missing / extra / unknown-variant / malformed entries, or no log at all
    for bad in (log[:-1], log + [log[-1]], [dict(e, chosen="ghost") for e in log], [dict(e, fold=7) for e in log], [], "x", [1]):
        with pytest.raises(RH.HarnessError):
            oos_ev(L, folds, vr, log=bad)
    with pytest.raises(RH.HarnessError):
        RH.oos_evidence(spec=spec_for(L), oos_returns=oos, ledger=L, universe=pit_universe(), leak=CLEAN, folds=folds, data_sha256=SHA,
                        selection_log=None, variant_returns=vr)
    with pytest.raises(TypeError):
        RH.oos_evidence(spec=spec_for(L), oos_returns=oos, ledger=L, universe=pit_universe(), leak=CLEAN, folds=folds, data_sha256=SHA)


def test_oos_selection_variant_must_be_counted_in_the_ledger():
    folds = RH.walk_forward_folds(N_ROWS, 300, 100, 5, 5)
    L = RH.VariantLedger(fam())
    L.record("v", {"n": "v"}, variant_rets()["v"], horizon=5)           # "w" was tried but never counted
    with pytest.raises(RH.HarnessError):
        oos_ev(L, folds)


# ================================================================== red-team round 2 (research harness)
def _rec(L, name="v", seed=1, n=600, mean=5.0, horizon=5):
    r = planted_edge(n=n, mean=mean, seed=seed)
    return L.record(name, {"n": name}, r, horizon=horizon), r


# ---- H) the ledger's family is the spec's multiple_testing_family
def test_ledger_family_must_equal_the_specs_multiple_testing_family():
    L = RH.VariantLedger(fam())
    st, _ = _rec(L)
    assert RH.historical_evidence(spec=spec_for(L), stats=st, ledger=L, universe=pit_universe(), leak=CLEAN, data_sha256=SHA).result == SV.PASS      # control
    other = spec(family="cherry_pick_family_b_" + uuid.uuid4().hex)
    with pytest.raises(RH.HarnessError, match="family"):
        RH.historical_evidence(spec=other, stats=st, ledger=L, universe=pit_universe(), leak=CLEAN, data_sha256=SHA)
    shouting = spec(family="  " + L.family.upper() + "  ")                                  # normalisation is the same one the ledger uses
    assert RH.historical_evidence(spec=shouting, stats=st, ledger=L, universe=pit_universe(), leak=CLEAN, data_sha256=SHA).result == SV.PASS
    nofam = spec()
    del nofam["multiple_testing_family"]                                                   # absent: falls back to candidate_id, as register_candidate does
    with pytest.raises(RH.HarnessError, match="family"):
        RH.historical_evidence(spec=nofam, stats=st, ledger=L, universe=pit_universe(), leak=CLEAN, data_sha256=SHA)
    L2 = RH.VariantLedger(nofam["candidate_id"])
    st2, _ = _rec(L2)
    assert RH.historical_evidence(spec=nofam, stats=st2, ledger=L2, universe=pit_universe(), leak=CLEAN, data_sha256=SHA).result == SV.PASS


def test_oos_ledger_family_must_equal_the_specs_family():
    L = RH.VariantLedger(fam())
    vr = _two_variant_ledger_fill(L)
    folds = RH.walk_forward_folds(N_ROWS, 300, 100, 5, 5)
    hl, ho = honest_log(folds, vr)
    kw = dict(oos_returns=ho, ledger=L, universe=pit_universe(), leak=CLEAN, folds=folds, data_sha256=SHA, selection_log=hl, variant_returns=vr)
    assert RH.oos_evidence(spec=spec_for(L), **kw).result == SV.PASS                    # control
    with pytest.raises(RH.HarnessError, match="family"):
        RH.oos_evidence(spec=spec(family="elsewhere_" + uuid.uuid4().hex), **kw)


def _two_variant_ledger_fill(L, horizon=5):
    vr = variant_rets()
    for n, r in vr.items():
        L.record(n, {"n": n}, r, horizon=horizon)
    return vr


# ---- I) independent_observations / ess come from the ledger, not the caller
def test_claimed_independent_observations_must_match_the_ledger_trial():
    L = RH.VariantLedger(fam())
    r = [random.Random(i).gauss(0.9, 1) for i in range(40)]
    st = L.record("v", {"a": 1}, r)
    assert st["independent_observations"] < 100
    honest = RH.historical_evidence(spec=spec_for(L), stats=st, ledger=L, universe=pit_universe(), leak=CLEAN, data_sha256=SHA)
    assert honest.metrics["independent_observations"] == st["independent_observations"] and honest.result != SV.PASS      # control: 40 obs cannot reach 100
    for key, val in (("independent_observations", 5000), ("ess", 5000.0)):
        forged = dict(st, **{key: val})
        with pytest.raises(RH.HarnessError, match="ledger"):
            RH.historical_evidence(spec=spec_for(L), stats=forged, ledger=L, universe=pit_universe(), leak=CLEAN, data_sha256=SHA)


def test_ledger_statistics_survive_a_restart(tmp_path):
    p = str(tmp_path / "led.jsonl")
    f = fam()
    L = RH.VariantLedger(f, path=p)
    st, _ = _rec(L, horizon=3)
    L2 = RH.VariantLedger(f, path=p)
    ev = RH.historical_evidence(spec=spec_for(L2), stats=st, ledger=L2, universe=pit_universe(), leak=CLEAN, data_sha256=SHA)
    assert ev.metrics["independent_observations"] == st["independent_observations"] and L2.variant_horizon("v") == 3
    with pytest.raises(RH.HarnessError, match="ledger"):
        RH.historical_evidence(spec=spec_for(L2), stats=dict(st, independent_observations=5000), ledger=L2, universe=pit_universe(), leak=CLEAN, data_sha256=SHA)


def test_variant_returns_must_be_the_series_the_ledger_recorded():
    L = RH.VariantLedger(fam())
    vr = _two_variant_ledger_fill(L)
    folds = RH.walk_forward_folds(N_ROWS, 300, 100, 5, 5)
    fake = {"v": [x + 100.0 for x in vr["v"]], "w": vr["w"]}                                # a different series under the counted name
    hl, ho = honest_log(folds, fake)
    with pytest.raises(RH.HarnessError, match="recorded"):
        RH.oos_evidence(spec=spec_for(L), oos_returns=ho, ledger=L, universe=pit_universe(), leak=CLEAN, folds=folds, data_sha256=SHA, selection_log=hl, variant_returns=fake)


# ---- J) folds must come from walk_forward_folds; horizon/embargo come from the declared variants
def test_hand_built_folds_with_zero_embargo_are_refused():
    L = RH.VariantLedger(fam())
    vr = _two_variant_ledger_fill(L)
    hand = [RH.Fold(tuple(range(0, 350)), tuple(range(350, 700)))]
    log = [{"fold": 0, "chosen": "v", "train_window": [0, 349]}]
    oos = vr["v"][350:700]
    with pytest.raises(RH.HarnessError, match="walk_forward_folds"):
        RH.oos_evidence(spec=spec_for(L), oos_returns=oos, ledger=L, universe=pit_universe(), leak=CLEAN, folds=hand, data_sha256=SHA, selection_log=log, variant_returns=vr)
    good = RH.walk_forward_folds(N_ROWS, 300, 100, 5, 5)                                    # control: the produced folds are accepted
    hl, ho = honest_log(good, vr)
    assert RH.oos_evidence(spec=spec_for(L), oos_returns=ho, ledger=L, universe=pit_universe(), leak=CLEAN, folds=good, data_sha256=SHA, selection_log=hl,
                           variant_returns=vr).result == SV.PASS
    # a produced fold whose indices were edited afterwards loses its stamp
    edited = [dataclasses.replace(good[0], train=tuple(range(0, 450)))] + good[1:]
    hl2, ho2 = honest_log(edited, vr)
    with pytest.raises(RH.HarnessError, match="walk_forward_folds"):
        RH.oos_evidence(spec=spec_for(L), oos_returns=ho2, ledger=L, universe=pit_universe(), leak=CLEAN, folds=edited, data_sha256=SHA, selection_log=hl2, variant_returns=vr)


def test_oos_horizon_and_embargo_come_from_the_declared_variants():
    # variants hold 9 settlements; folds built with a 1-settlement horizon/embargo would let training labels see test returns
    L = RH.VariantLedger(fam())
    vr = _two_variant_ledger_fill(L, horizon=9)
    short = RH.walk_forward_folds(N_ROWS, 300, 100, 1, 1)
    hl, ho = honest_log(short, vr)
    with pytest.raises(RH.HarnessError, match="horizon"):
        RH.oos_evidence(spec=spec_for(L), oos_returns=ho, ledger=L, universe=pit_universe(), leak=CLEAN, folds=short, data_sha256=SHA, selection_log=hl, variant_returns=vr)
    ok = RH.walk_forward_folds(N_ROWS, 300, 100, 9, 9)                                     # control: folds at the declared horizon
    hl, ho = honest_log(ok, vr)
    ev = RH.oos_evidence(spec=spec_for(L), oos_returns=ho, ledger=L, universe=pit_universe(), leak=CLEAN, folds=ok, data_sha256=SHA, selection_log=hl, variant_returns=vr)
    assert ev.result == SV.PASS and ev.metrics["label_horizon"] == 9 and ev.metrics["embargo"] == 9
    # label_horizon / embargo are no longer caller-supplied numbers
    with pytest.raises(TypeError):
        RH.oos_evidence(spec=spec_for(L), oos_returns=ho, ledger=L, universe=pit_universe(), leak=CLEAN, folds=ok, data_sha256=SHA, selection_log=hl, variant_returns=vr,
                        label_horizon=0, embargo=0)


def test_oos_requires_a_declared_horizon_for_every_variant():
    L = RH.VariantLedger(fam())
    vr = variant_rets()
    L.record("v", {"n": "v"}, vr["v"], horizon=5)
    L.record("w", {"n": "w"}, vr["w"])                                                      # horizon not declared
    folds = RH.walk_forward_folds(N_ROWS, 300, 100, 5, 5)
    hl, ho = honest_log(folds, vr)
    with pytest.raises(RH.HarnessError, match="horizon"):
        RH.oos_evidence(spec=spec_for(L), oos_returns=ho, ledger=L, universe=pit_universe(), leak=CLEAN, folds=folds, data_sha256=SHA, selection_log=hl, variant_returns=vr)
    for bad in (0, -1, True, 2.5):
        with pytest.raises(RH.HarnessError):
            L.record("x", {"n": "x"}, vr["w"], horizon=bad)


# ---- K) PIT attestation: what can be checked offline is checked, and the limit is stated
_ATT_H1, _ATT_H2 = hashlib.sha256(b"snapshot-1").hexdigest(), hashlib.sha256(b"snapshot-2").hexdigest()


def _attested(**over):
    att = {"no_delistings_observed_in_window": True, "window_from_ms": 0, "window_to_ms": 10_000, "snapshot_sha256": [_ATT_H1, _ATT_H2]}
    att.update(over)
    return RH.PointInTimeUniverse({"AAA": {"listed_ms": 0}, "BBB": {"listed_ms": 0}}, membership_source=PIT_SRC, includes_delisted=True,
                                  observed_from_ms=0, observed_to_ms=10_000, no_delistings_attestation=att)


def test_attestation_is_bound_to_the_observed_window_and_distinct_real_looking_hashes():
    u = _attested()
    assert u.point_in_time and u.pit_basis == "attested_no_delistings_unverified"          # control
    for bad in ({"window_from_ms": 1}, {"window_to_ms": 9_999}, {"window_from_ms": None}, {"window_to_ms": "10000"},
                {"snapshot_sha256": [_ATT_H1]},                                             # one snapshot cannot span a window
                {"snapshot_sha256": [_ATT_H1, _ATT_H1]},                                    # not distinct
                {"snapshot_sha256": [_ATT_H1, "0" * 64]},                                   # placeholder-shaped
                {"snapshot_sha256": [_ATT_H1, "cd" * 32]},
                {"snapshot_sha256": [_ATT_H1, _ATT_H2.upper()]}):
        assert not _attested(**bad).point_in_time, bad
    assert not _attested(snapshot_sha256=[_ATT_H1, _ATT_H1]).point_in_time and any("ATTESTATION" in p or "NO_DELISTING" in p for p in _attested(window_from_ms=1).survivorship_problems())


def test_pit_limits_are_stated_in_gaps_and_in_the_evidence():
    assert any("attest" in g and "not verified" in g for g in RH.GAPS), RH.GAPS
    L = RH.VariantLedger(fam())
    st, _ = _rec(L)
    ev = RH.historical_evidence(spec=spec_for(L), stats=st, ledger=L, universe=_attested(), leak=CLEAN, data_sha256=SHA)
    assert ev.metrics["universe_pit_basis"] == "attested_no_delistings_unverified" and ev.metrics["universe_pit_verified_against_exchange"] is False
    ev2 = RH.historical_evidence(spec=spec_for(L), stats=st, ledger=L, universe=pit_universe(), leak=CLEAN, data_sha256=SHA)
    assert ev2.metrics["universe_pit_basis"] == "self_declared_delisting_event_in_window" and ev2.metrics["universe_pit_verified_against_exchange"] is False
    ev3 = RH.historical_evidence(spec=spec_for(L), stats=st, ledger=L, universe=survivors(), leak=CLEAN, data_sha256=SHA)
    assert ev3.metrics["universe_pit_basis"] == "not_point_in_time"
