import ast
import json
import os
import random

import pytest

import funding_eval as FE
import opportunity_engine as OE
import trade_economics as TE
from capacity_helpers import base_row, clone, noisy, series, START, H8

HERE = os.path.dirname(os.path.abspath(__file__))
BOT = os.path.dirname(HERE)
BASE = base_row()


def position(**kw):
    d = dict(notional_usd=100_000.0, hold_days=30.0, btc_inventory_usd=100_000.0, btc_was_owned=True,
             margin_posted_usd=0.0, collateral_haircut=0.1, reserve_usd=10_000.0,
             maintenance_margin_rate=0.005, initial_margin_rate=0.05)
    d.update(kw)
    return TE.Position(**d)


ANCHORED = {"files_total": 1, "files_legacy_pre_chain": 0, "files_anchored_current": 1, "files_anchored_lagging": 0,
            "files_stale_anchor": 0, "files_unanchored": 0, "files_anchor_conflict": 0}
NOT_ANCHORED = dict(ANCHORED, files_anchored_current=0, files_unanchored=1)
PRO_OK = {"registered_ms": START - 1, "first_evidence_ms": START, "n_after_registration": 270,
          "post_registration_ess": 60.0, "post_registration_annualised_pct_on_economic_capital": 14.0}


def bundle(funding=0.65, n=100, fillable=1.0, cost=11.4, reliability="HEALTHY", chain_ok=True, validation_ok=True,
           pos=None, prospective=None, realised=None, environment="mainnet", source="PUBLIC_REALISED", integrity=None):
    rows = series(BASE, n, lambda T, t: funding, realised or noisy(funding, sd=max(0.05, funding * 0.15)), cadence_ms=600_000)
    fe = FE.evaluate(rows)
    f_lo = fe["realised_bps"].get("conservative_lo_bps")
    if f_lo is None:
        f_lo = fe["realised_bps"]["mean"]
    costs = TE.Costs(perp_entry_fee_bps=5.5, perp_exit_fee_bps=5.5, spread_bps=cost - 11.0, slippage_bps=0.0, impact_bps=0.0,
                     latency_bps=0.0, conversion_bps=0.0, transfer_usd=0.0, custody_apr=0.0, operational_usd=0.0, borrow_apr=0.0)
    econ = TE.compute(pos or position(), costs, [f_lo] * 90, funding_source=source)
    return {"carry": {"validation_ok": validation_ok, "chain_ok": chain_ok, "reliability_verdict": reliability, "environment": environment,
                      "integrity": integrity if integrity is not None else NOT_ANCHORED,
                      "funding_eval": fe, "economics": econ, "fillable_share": fillable, "round_trip_cost_bps": cost,
                      "prospective": prospective or {}}, "rows": rows}


def status(b, key="carry_overlay_taker/v1"):
    e = OE.evaluate_carry(OE.REGISTRY[key], b["carry"])
    return e.status, e


def test_the_registry_is_complete_unique_and_never_connected_to_an_order_path():
    assert len(OE.REGISTRY) == len({s.key for s in OE.REGISTRY.values()}) >= 10
    for s in OE.REGISTRY.values():
        assert s.order_path_connected is False
        assert s.origin in (OE.EXECUTED_BY_ENGINE, OE.ORDER_LESS, OE.HYPOTHESIS, OE.FROZEN_ABSENT)
        if s.origin != OE.FROZEN_ABSENT:
            assert s.entry_conditions and s.exit_conditions is not None and s.required_inputs
            assert s.acceptance and s.rejection and s.research_protocol and s.risk_limits is not None
    assert OE.REGISTRY["funding_carry_fade/v1"].origin == OE.FROZEN_ABSENT


def test_the_module_imports_nothing_that_can_reach_an_order_a_store_or_the_network():
    tree = ast.parse(open(os.path.join(BOT, "opportunity_engine.py"), encoding="utf-8").read())
    imported = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            imported |= {a.name.split(".")[0] for a in n.names}
        elif isinstance(n, ast.ImportFrom):
            imported.add((n.module or "").split(".")[0])
    allowed = {"__future__", "math", "dataclasses", "enum", "typing", "evidence_stats", "trade_economics", "funding_eval", "strategy_measure"}
    assert imported <= allowed, imported - allowed


def test_too_few_independent_settlements_is_insufficient_not_attractive():
    st, e = status(bundle(funding=5.0, n=40))
    assert st == OE.Status.INSUFFICIENT_EVIDENCE and "90 needed" in " ".join(e.reasons)


@pytest.mark.parametrize("kw", [dict(reliability="STALE"), dict(reliability="NO_DATA"), dict(chain_ok=False), dict(validation_ok=False)])
def test_untrustworthy_evidence_is_insufficient_whatever_the_numbers_say(kw):
    st, _ = status(bundle(funding=5.0, **kw))
    assert st == OE.Status.INSUFFICIENT_EVIDENCE


def test_a_7_percent_gross_carry_is_economically_unattractive_against_the_lowest_screen():
    st, e = status(bundle(funding=0.65))
    assert st == OE.Status.ECONOMICALLY_UNATTRACTIVE and "PROVISIONAL_SCREENING_HURDLE_NOT_OWNER_APPROVED" in e.reasons[0]


def test_a_rich_funding_regime_is_only_a_research_candidate_never_validated_without_a_prior_registration():
    st, e = status(bundle(funding=4.0))
    assert st == OE.Status.RESEARCH_CANDIDATE and e.authorizes_trading is False
    assert "not prospectively confirmed" in e.reasons[0]


def test_prospective_validation_needs_registration_before_the_evidence_enough_later_settlements_and_the_screens_rerun_on_them():
    ok = PRO_OK
    assert status(bundle(funding=4.0, prospective=ok, integrity=ANCHORED))[0] == OE.Status.PROSPECTIVE_MARKET_EDGE_SUPPORTED
    late = dict(ok, registered_ms=START + 1)
    assert status(bundle(funding=4.0, prospective=late, integrity=ANCHORED))[0] == OE.Status.RESEARCH_CANDIDATE
    few = dict(ok, n_after_registration=100)
    assert status(bundle(funding=4.0, prospective=few, integrity=ANCHORED))[0] == OE.Status.RESEARCH_CANDIDATE
    # the in-sample pass does not carry over: the screens must pass on the post-registration data ALONE
    failing_later = dict(ok, post_registration_annualised_pct_on_economic_capital=3.0)
    assert status(bundle(funding=4.0, prospective=failing_later, integrity=ANCHORED))[0] == OE.Status.RESEARCH_CANDIDATE
    no_rerun = {k: v for k, v in ok.items() if not k.startswith("post_registration_annualised")}
    assert status(bundle(funding=4.0, prospective=no_rerun, integrity=ANCHORED))[0] == OE.Status.RESEARCH_CANDIDATE
    thin_ess = dict(ok, post_registration_ess=5.0)
    assert status(bundle(funding=4.0, prospective=thin_ess, integrity=ANCHORED))[0] == OE.Status.RESEARCH_CANDIDATE


def test_execution_and_risk_failures_are_named_separately():
    assert status(bundle(funding=4.0, fillable=0.5))[0] == OE.Status.EXECUTION_UNATTRACTIVE
    assert status(bundle(funding=4.0, cost=20.0))[0] == OE.Status.EXECUTION_UNATTRACTIVE
    thin = position(btc_inventory_usd=10_000.0, margin_posted_usd=1_000.0)      # 9.5% conservative headroom
    assert status(bundle(funding=4.0, pos=thin))[0] == OE.Status.RISK_UNACCEPTABLE
    st, e = status(bundle(funding=4.0, realised=lambda T: -30.0 if (T // H8) % 25 == 0 else 4.0))
    assert st == OE.Status.RISK_UNACCEPTABLE and "paid" in e.reasons[0]


def test_every_evaluation_says_it_authorises_nothing_and_the_frozen_strategy_is_never_scored():
    out = OE.evaluate_all({"carry": bundle(funding=0.65)["carry"], "rows": [], "slice57": None})
    assert all(o["authorizes_trading"] is False and o["order_path_connected"] is False for o in out)
    frozen = [o for o in out if o["strategy"] == "funding_carry_fade/v1"][0]
    assert frozen["status"] is None and frozen["origin"] == "FROZEN_ABSENT"
    scored = {o["strategy"]: o["status"] for o in out if o["status"]}
    assert set(scored.values()) <= {s.value for s in OE.Status}


def test_the_slice_57_hypothesis_is_never_proven_by_its_own_thin_historical_pass():
    d = json.load(open(os.path.join(BOT, "artifacts", "slice57_stage1_funding_carry_fade_btc_v1.json")))
    e = OE.evaluate_btc_fade(OE.REGISTRY["funding_carry_fade_btc/v1"], d)
    assert e.status == OE.Status.INSUFFICIENT_EVIDENCE
    text = " ".join(e.reasons)
    assert "already been examined" in text and "does not execute" in text
    assert e.metrics["n_oos"] == 41 and e.metrics["n_floor"] == 40 and e.metrics["m1_margin_replicates"] == 3
    assert e.metrics["q2_mean_net_r"] < 0.1 * e.metrics["q1_mean_net_r"]


GOOD_EV = None


def good_ev():
    global GOOD_EV
    if GOOD_EV is None:
        GOOD_EV = bundle(funding=4.0)["carry"]
    return GOOD_EV


def brows(spec_rows, step_s=300):
    return [{"at_ms": START + int(i * step_s * 1000), "basis_bps": b} for i, b in enumerate(spec_rows)]


def snapback_basis(n_spikes=60, quiet=63, seed=3):
    rng = random.Random(seed)
    out = []
    for _ in range(n_spikes):
        out += [1.0, 1.5, 28.0 + rng.gauss(0, 2), 26.0 + rng.gauss(0, 2), 3.0, 1.0, 1.2] + [1.0 + rng.gauss(0, 0.3) for _ in range(quiet)]
    return out


def test_basis_needs_two_weeks_a_cost_and_trustworthy_evidence_before_it_is_graded():
    spec = OE.REGISTRY["basis_convergence/v1"]
    ev = good_ev()
    assert "days of basis" in OE.evaluate_basis(spec, brows(snapback_basis(10, 20)), round_trip_cost_bps=11.4, ev=ev).reasons[0]
    full = brows(snapback_basis())
    assert (full[-1]["at_ms"] - full[0]["at_ms"]) / 86_400_000 >= 14
    assert OE.evaluate_basis(spec, full, round_trip_cost_bps=None, ev=ev).status == OE.Status.INSUFFICIENT_EVIDENCE
    assert OE.evaluate_basis(spec, full, round_trip_cost_bps=11.4, ev=dict(ev, environment="testnet")).status == OE.Status.INSUFFICIENT_EVIDENCE
    assert OE.evaluate_basis(spec, full, round_trip_cost_bps=11.4, ev=dict(ev, chain_ok=False)).status == OE.Status.INSUFFICIENT_EVIDENCE


def test_a_basis_that_really_snaps_back_far_beyond_cost_is_a_candidate_with_its_episodes_reported():
    e = OE.evaluate_basis(OE.REGISTRY["basis_convergence/v1"], brows(snapback_basis()), round_trip_cost_bps=11.4, ev=good_ev())
    assert e.status == OE.Status.RESEARCH_CANDIDATE and "family-wise" in e.reasons[0] and e.authorizes_trading is False
    v = [x for x in e.metrics["variants"] if x["ok"]]
    assert v and all(x["episodes"] >= OE.MIN_EPISODES for x in v) and e.metrics["family_trials"] == OE.FAMILY_TRIALS
    assert e.metrics["exploratory_screen"]["p95_bps"] - e.metrics["exploratory_screen"]["p05_bps"] > 0        # the range screen is shown, not graded


def test_NULL_a_basis_that_does_not_revert_never_becomes_a_candidate_even_when_its_range_exceeds_cost():
    hits = 0
    for sd in range(12):
        rng = random.Random(900 + sd)
        b, path = 0.0, []
        for _ in range(20 * 288):
            b = max(-60.0, min(60.0, b + rng.gauss(0, 4.0)))          # a reflected random walk: wide range, no reversion
            path.append(b)
        e = OE.evaluate_basis(OE.REGISTRY["basis_convergence/v1"], brows(path), round_trip_cost_bps=11.4, ev=good_ev())
        hits += e.status == OE.Status.RESEARCH_CANDIDATE
        assert e.metrics["exploratory_screen"]["p95_bps"] - e.metrics["exploratory_screen"]["p05_bps"] > 11.4        # the old screen would have said yes
    assert hits <= 1


def test_the_range_screen_alone_is_never_graded_a_square_wave_has_no_variance_to_judge():
    rows = brows([20.0 if (k // 72) % 2 else -20.0 for k in range(20 * 288)])
    e = OE.evaluate_basis(OE.REGISTRY["basis_convergence/v1"], rows, round_trip_cost_bps=11.4, ev=good_ev())
    assert e.status in (OE.Status.INSUFFICIENT_EVIDENCE, OE.Status.RESEARCH_CANDIDATE)
    assert e.status != OE.Status.RESEARCH_CANDIDATE or any(v["lo"] and v["lo"] > 0 for v in e.metrics["variants"])


def price_rows(prices, step_ms=300_000):
    return [{"at_ms": START + i * step_ms, "perp_mid": p} for i, p in enumerate(prices)]


def walk(rets, p0=60000.0):
    p = [p0]
    for r in rets:
        p.append(p[-1] * (1 + r))
    return p


def trending(n, rng, persist=0.98, dsd=0.00012, sd=0.001):
    mu, out = 0.0, []
    for _ in range(n):
        mu = persist * mu + rng.gauss(0, dsd)
        out.append(mu + rng.gauss(0, sd))
    return out


def test_the_directional_evaluators_refuse_short_history_and_a_missing_cost():
    ev = good_ev()
    short = price_rows(walk(trending(20 * 288, random.Random(1))))
    for fn, key in ((OE.evaluate_trend, "btc_trend_following/v1"), (OE.evaluate_mean_reversion, "btc_mean_reversion/v1")):
        e = fn(OE.REGISTRY[key], ev, short, 5.7)
        assert e.status == OE.Status.INSUFFICIENT_EVIDENCE and "fresh holdout" in " ".join(e.reasons)
        long_ = price_rows(walk(trending(100 * 288, random.Random(1))))
        assert fn(OE.REGISTRY[key], ev, long_, None).status == OE.Status.INSUFFICIENT_EVIDENCE
        assert fn(OE.REGISTRY[key], dict(ev, reliability_verdict="STALE"), long_, 5.7).status == OE.Status.INSUFFICIENT_EVIDENCE


def test_trend_is_a_candidate_where_persistent_drift_really_exists_and_reversion_never_is_there():
    """Measured at the family-wise alpha over 100 days of 5-minute bars: the trend variants are a candidate in about 5 of 6 worlds
    with a real, persistent drift (power is NOT 100 %: that is the price of the multiple-testing correction), and the
    mean-reversion hypothesis is never a candidate on the same prices."""
    ev = good_ev()
    t_hits = m_hits = m_unattractive = 0
    for sd in range(6):
        rows = price_rows(walk(trending(100 * 288, random.Random(40 + sd), dsd=0.0002)))
        t = OE.evaluate_trend(OE.REGISTRY["btc_trend_following/v1"], ev, rows, 0.5)
        m = OE.evaluate_mean_reversion(OE.REGISTRY["btc_mean_reversion/v1"], ev, rows, 0.5)
        t_hits += t.status == OE.Status.RESEARCH_CANDIDATE
        m_hits += m.status == OE.Status.RESEARCH_CANDIDATE
        m_unattractive += m.status == OE.Status.ECONOMICALLY_UNATTRACTIVE
        assert "beat zero AND buy-and-hold" in t.metrics["baseline"] and t.authorizes_trading is False
    assert t_hits >= 3 and m_hits == 0 and m_unattractive >= 4, (t_hits, m_hits, m_unattractive)


def test_NULL_neither_directional_hypothesis_becomes_a_candidate_on_a_random_walk():
    ev = good_ev()
    hits = 0
    for sd in range(8):
        rows = price_rows(walk([random.Random(700 + sd).gauss(0, 0.001) for _ in range(100 * 288)]))
        for fn, key in ((OE.evaluate_trend, "btc_trend_following/v1"), (OE.evaluate_mean_reversion, "btc_mean_reversion/v1")):
            hits += fn(OE.REGISTRY[key], ev, rows, 0.5).status == OE.Status.RESEARCH_CANDIDATE
    assert hits <= 1


def test_microstructure_cannot_be_measured_on_rows_that_do_not_carry_imbalance_and_says_why():
    e = OE.evaluate_microstructure(OE.REGISTRY["microstructure_imbalance/v1"], good_ev(), price_rows(walk([0.0] * 400)), 0.5)
    assert e.status == OE.Status.INSUFFICIENT_EVIDENCE and "NOT_RECORDED" in " ".join(e.reasons)


def test_microstructure_is_measured_when_the_imbalance_is_recorded():
    rng = random.Random(5)
    imb = [rng.uniform(-1, 1) for _ in range(6000)]
    mid = [60000.0]
    for i in range(5999):
        mid.append(mid[-1] * (1 + 12.0 * imb[i] / 1e4 + rng.gauss(0, 0.001)))
    rows = [dict(r, book={"imbalance": x}) for r, x in zip(price_rows(mid), imb)]
    e = OE.evaluate_microstructure(OE.REGISTRY["microstructure_imbalance/v1"], good_ev(), rows, 1.0)
    assert e.status == OE.Status.RESEARCH_CANDIDATE and e.metrics["variants"][0]["fill_assumption"].startswith("AT THE MID")


def test_the_regime_modifier_needs_a_trustworthy_funding_series_first():
    e = OE.evaluate_regime(OE.REGISTRY["regime_classifier/v1"], good_ev(), price_rows(walk([0.001] * 100)), {"status": "INSUFFICIENT"})
    assert e.status == OE.Status.INSUFFICIENT_EVIDENCE and "funding evaluation is not OK" in " ".join(e.reasons)


def test_a_cross_section_needs_more_than_one_recorded_symbol():
    spec = OE.REGISTRY["cross_sectional_funding/v1"]
    e = OE.evaluate_cross_sectional(spec, good_ev(), {"BTCUSDT": [1]})
    assert e.status == OE.Status.INSUFFICIENT_EVIDENCE and "recorder is configured for one symbol" in " ".join(e.reasons)


def test_a_multi_instrument_recording_is_measured_with_each_instruments_own_cost():
    from test_strategy_measure import multi_rows
    rows = multi_rows(random.Random(9), 120, informative=True)
    e = OE.evaluate_cross_sectional(OE.REGISTRY["cross_sectional_funding/v1"], good_ev(), rows)
    assert e.status == OE.Status.RESEARCH_CANDIDATE and "rank information coefficient" in " ".join(e.reasons)
    assert set(e.metrics["measured"]["round_trip_cost_bps"]) == {"BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT"}


def test_every_remaining_hypothesis_has_a_declared_variant_grid_and_the_family_is_counted_once():
    assert OE.FAMILY_TRIALS == 3 + 3 + 3 + 3 + 3 + 1 and OE.FAMILY_ALPHA == pytest.approx(0.05 / OE.FAMILY_TRIALS)
    for k in ("basis_convergence/v1", "btc_trend_following/v1", "btc_mean_reversion/v1", "microstructure_imbalance/v1",
              "regime_classifier/v1", "cross_sectional_funding/v1"):
        assert k in OE.PREDECLARED


def test_evaluate_all_never_labels_an_unimplemented_hypothesis_as_working():
    out = {o["strategy"]: o for o in OE.evaluate_all(bundle(funding=4.0))}
    assert out["funding_carry_fade/v1"]["status"] is None
    for k, o in out.items():
        assert o["authorizes_trading"] is False and o["order_path_connected"] is False
        assert o["status"] not in ("ECONOMICALLY_RECONCILED", "EXECUTION_READY", "PROSPECTIVE_MARKET_EDGE_SUPPORTED"), k
    assert out["microstructure_imbalance/v1"]["status"] == "INSUFFICIENT_EVIDENCE"
    assert out["cross_sectional_funding/v1"]["status"] == "INSUFFICIENT_EVIDENCE"


@pytest.mark.parametrize("kw,needle", [
    (dict(environment="testnet"), "only mainnet evidence"),
    (dict(environment=None), "only mainnet evidence"),
    (dict(reliability="DEGRADED"), "only HEALTHY"),
    (dict(source="SCENARIO"), "realised public funding print"),
    (dict(source="PREDICTED"), "realised public funding print"),
])
def test_testnet_degraded_or_forecast_based_evidence_cannot_reach_a_candidate(kw, needle):
    st, e = status(bundle(funding=4.0, **kw))
    assert st == OE.Status.INSUFFICIENT_EVIDENCE and needle in " ".join(e.reasons)


def test_a_constant_funding_history_never_becomes_a_candidate_however_rich_or_long():
    st, e = status(bundle(funding=4.0, n=150, realised=lambda T: 4.0))
    assert st == OE.Status.INSUFFICIENT_EVIDENCE and "effective sample size" in " ".join(e.reasons)


def test_a_candidate_names_its_unpriced_inputs_instead_of_implying_they_are_zero():
    costs_unknown = TE.Costs(perp_entry_fee_bps=5.5, perp_exit_fee_bps=5.5, spread_bps=0.4, slippage_bps=0.0)   # nothing else priced
    b = bundle(funding=4.0)
    f_lo = b["carry"]["funding_eval"]["realised_bps"]["conservative_lo_bps"]
    b["carry"]["economics"] = TE.compute(position(), costs_unknown, [f_lo] * 90, funding_source="PUBLIC_REALISED")
    st, e = status(b)
    assert st == OE.Status.RESEARCH_CANDIDATE
    assert "latency" in e.metrics["unpriced_inputs"] and "unpriced inputs: " in e.reasons[0] and "MODELLED" in e.reasons[0]


def test_the_depth_candidate_is_never_given_a_screen_on_the_overlays_evidence():
    out = {o["strategy"]: o for o in OE.evaluate_all(bundle(funding=4.0)["carry"] and {"carry": bundle(funding=4.0)["carry"], "rows": []})}
    d = out["candidate_carry_depth/v1"]
    assert d["status"] == "INSUFFICIENT_EVIDENCE" and d["metrics"]["carry_overlay_status"] == "RESEARCH_CANDIDATE"
    assert "depth-benefit entry rule is not evaluated" in d["reasons"][0]
    assert out["carry_overlay_taker/v1"]["status"] == "RESEARCH_CANDIDATE"


# ======================= Phase 2: public market edge is not account profit =======================

def full_account():
    """A structurally COMPLETE account-evidence object. Nothing in the repository produces one; it exists here so that
    each requirement can be removed in turn and shown to block the claim."""
    return {
        "fee_schedule": {"provenance": "ACCOUNT_VERIFIED", "taker_bps": 5.5, "maker_bps": 2.0},
        "product_qualification": {"verdict": "QUALIFIED", "environment": "mainnet", "product": "BTCUSDT linear", "evidence_ref": "owner-ref-1"},
        "funding_settlements": {"source": "ACCOUNT_SETTLED", "reconciled": True, "unreconciled": 0, "settlements_reconciled": 120},
        "fills": {"evidence_class": "ACCOUNT_EXECUTION", "reconciled": True, "fills": 12, "fee_records": 12, "round_trips": 6},
        "execution_costs": {"provenance": {"latency": "MEASURED", "market_impact": "MEASURED", "slippage": "MEASURED"}},
        "capital": {"reconciled": True, "source": "ACCOUNT_READ"},
        "venue_rules": {"verified": True, "source": "owner read of the margin rules"},
    }


def account_economics(**over):
    costs = TE.Costs(perp_entry_fee_bps=5.5, perp_exit_fee_bps=5.5, spread_bps=0.3, slippage_bps=0.1, impact_bps=0.2, latency_bps=0.1,
                     conversion_bps=0.0, transfer_usd=0.0, custody_apr=0.0, operational_usd=0.0, borrow_apr=0.0,
                     fee_provenance=over.pop("fee_provenance", "ACCOUNT_VERIFIED"))
    return TE.compute(position(), costs, [3.8] * 90, funding_source=over.pop("funding_source", "ACCOUNT_SETTLED"))


def reconciled(account=None, account_economics_=None, authorisation=None, execution=None, economics=None):
    b = bundle(funding=4.0, prospective=PRO_OK, integrity=ANCHORED)
    c = b["carry"]
    c["account"] = full_account() if account is None else account
    c["account_economics"] = account_economics() if account_economics_ is None else account_economics_
    if authorisation is not None:
        c["execution_authorisation"] = authorisation
    if execution is not None:
        c["execution"] = execution
    if economics is not None:
        c["economics"] = economics
    return b


def test_complete_account_evidence_reaches_economically_reconciled_but_never_execution_ready_without_a_separate_record():
    st, e = status(reconciled())
    assert st == OE.Status.ECONOMICALLY_RECONCILED and e.metrics["reconciliation_gaps"] == []
    assert e.authorizes_trading is False and "NOT execution-ready" in e.reasons[0]


def test_execution_ready_needs_a_named_owner_authorisation_and_still_authorises_nothing():
    good = {"owner_authorised": True, "reference": "OWNER-AUTH-2026-11-05", "scope": "carry_overlay_taker/v1"}
    st, e = status(reconciled(authorisation=good))
    assert st == OE.Status.EXECUTION_READY and e.authorizes_trading is False and e.order_path_connected is False
    assert e.as_dict()["authorizes_trading"] is False
    for bad in ({"owner_authorised": True, "reference": "x", "scope": "candidate_carry_depth/v1"},      # another strategy's
                {"owner_authorised": True, "reference": "", "scope": "carry_overlay_taker/v1"},         # no reference
                {"owner_authorised": "yes", "reference": "x", "scope": "carry_overlay_taker/v1"},       # not a real True
                {"owner_authorised": False, "reference": "x", "scope": "carry_overlay_taker/v1"}, {}):
        assert status(reconciled(authorisation=bad))[0] == OE.Status.ECONOMICALLY_RECONCILED, bad


def _drop(path):
    def mutate(acct):
        d = acct
        for k in path[:-1]:
            d = d[k]
        d.pop(path[-1]) if path[-1] in d else None
        return acct
    return mutate


@pytest.mark.parametrize("name,mutate,needle", [
    ("fees unverified (published defaults)", lambda a: (a["fee_schedule"].update(provenance="PUBLISHED_DEFAULT"), a)[1], "account_fee_schedule"),
    ("no fee schedule at all", lambda a: (a.pop("fee_schedule"), a)[1], "account_fee_schedule"),
    ("product not qualified (EU)", lambda a: (a["product_qualification"].update(verdict="UNVERIFIED"), a)[1], "product_account_qualification"),
    ("qualified but testnet", lambda a: (a["product_qualification"].update(environment="testnet"), a)[1], "product_account_qualification"),
    ("public funding present, account settlement missing", lambda a: (a.pop("funding_settlements"), a)[1], "account_funding_settlements"),
    ("account settlements are the public print", lambda a: (a["funding_settlements"].update(source="PUBLIC_REALISED"), a)[1], "account_funding_settlements"),
    ("an unreconciled settlement", lambda a: (a["funding_settlements"].update(unreconciled=1), a)[1], "account_funding_settlements"),
    ("too few settlements", lambda a: (a["funding_settlements"].update(settlements_reconciled=30), a)[1], "account_funding_settlements"),
    ("fills are modelled, not executions", lambda a: (a["fills"].update(evidence_class="SIMULATED_FILL"), a)[1], "fills_and_fee_records"),
    ("fills without fee records", lambda a: (a["fills"].update(fee_records=3), a)[1], "fills_and_fee_records"),
    ("latency UNKNOWN", lambda a: (a["execution_costs"]["provenance"].update(latency="UNKNOWN"), a)[1], "execution_and_operational_costs"),
    ("market impact UNKNOWN", lambda a: (a["execution_costs"]["provenance"].update(market_impact="UNKNOWN"), a)[1], "execution_and_operational_costs"),
    ("slippage only ESTIMATED", lambda a: (a["execution_costs"]["provenance"].update(slippage="ESTIMATED"), a)[1], "execution_and_operational_costs"),
    ("capital not reconciled", lambda a: (a["capital"].update(reconciled=False), a)[1], "capital_and_collateral"),
    ("venue margin rules ASSUMED", lambda a: (a["venue_rules"].update(verified=False), a)[1], "venue_margin_and_liquidation_rules"),
    ("no venue rules object", lambda a: (a.pop("venue_rules"), a)[1], "venue_margin_and_liquidation_rules"),
])
def test_every_missing_account_requirement_blocks_economic_reconciliation(name, mutate, needle):
    st, e = status(reconciled(account=mutate(full_account())))
    assert st == OE.Status.PROSPECTIVE_MARKET_EDGE_SUPPORTED, name
    assert any(needle in g for g in e.metrics["reconciliation_gaps"]), (name, e.metrics["reconciliation_gaps"])
    assert "NOT about any account's profit" in " ".join(e.reasons)


def test_unknown_latency_or_impact_in_the_ledger_blocks_reconciliation_even_if_the_account_object_claims_measurement():
    for field in ("latency_bps", "impact_bps"):
        kw = dict(perp_entry_fee_bps=5.5, perp_exit_fee_bps=5.5, spread_bps=0.3, slippage_bps=0.1, impact_bps=0.2, latency_bps=0.1,
                  conversion_bps=0.0, transfer_usd=0.0, custody_apr=0.0, operational_usd=0.0, borrow_apr=0.0)
        kw[field] = None
        econ = TE.compute(position(), TE.Costs(**kw), [3.9] * 90, funding_source="PUBLIC_REALISED")
        assert econ["unknowns"]
        st, e = status(reconciled(economics=econ))
        assert st != OE.Status.ECONOMICALLY_RECONCILED and any("unpriced inputs remain" in g for g in e.metrics["reconciliation_gaps"]), field


def test_an_assumed_maker_fill_probability_blocks_a_maker_or_hybrid_claim_but_not_a_taker_one():
    for mode in ("maker", "hybrid"):
        acct = full_account()
        acct["execution_costs"]["provenance"].update(maker_fill_probability="ASSUMED", adverse_selection="ASSUMED")
        st, e = status(reconciled(account=acct, execution={"mode": mode}))
        assert st == OE.Status.PROSPECTIVE_MARKET_EDGE_SUPPORTED and any("maker_fill_probability" in g for g in e.metrics["reconciliation_gaps"]), mode
        acct["execution_costs"]["provenance"].update(maker_fill_probability="MEASURED", adverse_selection="MEASURED")
        assert status(reconciled(account=acct, execution={"mode": mode}))[0] == OE.Status.ECONOMICALLY_RECONCILED
    acct = full_account()
    acct["execution_costs"]["provenance"].update(maker_fill_probability="ASSUMED")
    assert status(reconciled(account=acct, execution={"mode": "taker"}))[0] == OE.Status.ECONOMICALLY_RECONCILED


def test_account_economics_computed_from_public_funding_or_unverified_fees_do_not_count():
    assert status(reconciled(account_economics_=account_economics(funding_source="PUBLIC_REALISED")))[0] == OE.Status.PROSPECTIVE_MARKET_EDGE_SUPPORTED
    assert status(reconciled(account_economics_=account_economics(fee_provenance="UNVERIFIED")))[0] == OE.Status.PROSPECTIVE_MARKET_EDGE_SUPPORTED
    assert status(reconciled(account_economics_={}))[0] == OE.Status.PROSPECTIVE_MARKET_EDGE_SUPPORTED


def test_account_evidence_cannot_rescue_a_market_edge_that_is_not_supported():
    """A perfect account object on top of in-sample-only public evidence is still only a candidate."""
    b = reconciled()
    b["carry"]["prospective"] = {}
    assert status(b)[0] == OE.Status.RESEARCH_CANDIDATE
    b2 = reconciled()
    b2["carry"]["integrity"] = NOT_ANCHORED
    assert status(b2)[0] == OE.Status.RESEARCH_CANDIDATE
    b3 = reconciled()
    b3["carry"]["reliability_verdict"] = "DEGRADED"
    assert status(b3)[0] == OE.Status.INSUFFICIENT_EVIDENCE


def test_a_prospective_claim_needs_headroom_for_the_unpriced_costs_and_shows_it(monkeypatch):
    st, e = status(bundle(funding=4.0, prospective=PRO_OK, integrity=ANCHORED))
    assert st == OE.Status.PROSPECTIVE_MARKET_EDGE_SUPPORTED and e.metrics["unknown_cost_headroom_bps_round_trip"] >= OE.MIN_UNKNOWN_COST_HEADROOM_BPS
    monkeypatch.setattr(OE, "MIN_UNKNOWN_COST_HEADROOM_BPS", 1e9)
    assert status(bundle(funding=4.0, prospective=PRO_OK, integrity=ANCHORED))[0] == OE.Status.RESEARCH_CANDIDATE


def test_integrity_grade_is_anchored_only_when_every_chained_file_has_a_matching_anchor():
    assert OE.integrity_grade({"integrity": ANCHORED}) == "ANCHORED"
    assert OE.integrity_grade({"integrity": dict(ANCHORED, files_anchored_current=0, files_anchored_lagging=1)}) == "ANCHORED"
    assert OE.integrity_grade({"integrity": dict(ANCHORED, files_total=2, files_legacy_pre_chain=1)}) == "ANCHORED"     # a legacy file is not chained
    assert OE.integrity_grade({"integrity": NOT_ANCHORED}) == "NOT_FULLY_ANCHORED"
    assert OE.integrity_grade({"integrity": dict(ANCHORED, files_stale_anchor=1, files_anchored_current=0)}) == "NOT_FULLY_ANCHORED"
    assert OE.integrity_grade({"integrity": dict(ANCHORED, files_anchor_conflict=1)}) == "CONFLICT"
    assert OE.integrity_grade({}) == "NOT_FULLY_ANCHORED"


def test_no_default_input_to_the_report_can_reach_economic_reconciliation_or_readiness():
    """The report builds no account section: whatever the public evidence, the best it can say is a research candidate."""
    st, e = status(bundle(funding=4.0))
    assert st == OE.Status.RESEARCH_CANDIDATE and e.metrics["reconciliation_gaps"] and len(e.metrics["reconciliation_gaps"]) == len(OE.RECONCILIATION_REQUIREMENTS)
