import ast
import json
import os
import random

import math

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
            "files_anchored_by_archive": 1, "files_stale_anchor": 0, "files_unanchored": 0, "files_anchor_conflict": 0, "files_missing_but_anchored": [],
            "anchor_location": "EXTERNAL_ARCHIVE_HEAD_MATCHES_OWNER_HELD_VALUE"}
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
    allowed = {"__future__", "math", "dataclasses", "enum", "typing", "evidence_stats", "trade_economics", "funding_eval", "strategy_measure", "execution_model"}
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
    """Measured at the family-wise alpha over 100 days of 5-minute bars: with a real, persistent drift the trend variants CAN be a candidate, and the mean-reversion hypothesis
    never is on the same prices.

    HOW OFTEN is no longer 'about 5 of 6 worlds'. That figure was measured with an interval that was too narrow: the 3-batch t interval took a Cornish-Fisher critical value
    that, at this family-wise alpha (0.05 / 16), was 10.6 where the exact Student-t value for 2 degrees of freedom is 17.8 (40.7% too small, ``evidence_stats._t_crit``). With
    the exact value the trend variants are a candidate in 2 of these 6 worlds (11 of 40 worlds, 27.5%; 24 of 40, 60%, with the old value), and a 3-fold stronger drift barely
    moves it (4 of 12): the widest-of interval at a family-wise alpha is conservative where the dependence is strong, because three batch means carry two degrees of freedom.
    That is the pre-registered rule's own conservatism, now stated honestly; it is not tuned away here. The upper bound below is what stops the narrow interval coming back
    (it gave 5)."""
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
    assert 1 <= t_hits <= 3 and m_hits == 0 and m_unattractive >= 4, (t_hits, m_hits, m_unattractive)


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
        "funding_settlements": {"source": "ACCOUNT_SETTLED", "reconciled": True, "unreconciled": 0, "settlements_reconciled": 90},
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
    c["execution"] = {"mode": "taker"} if execution is None else execution
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
    ("settlement count differs from the ledger", lambda a: (a["funding_settlements"].update(settlements_reconciled=120), a)[1], "account_funding_settlements"),
    ("unreconciled is False not 0", lambda a: (a["funding_settlements"].update(unreconciled=False), a)[1], "account_funding_settlements"),
    ("an absurd taker fee", lambda a: (a["fee_schedule"].update(taker_bps=1e12), a)[1], "account_fee_schedule"),
    ("maker above taker", lambda a: (a["fee_schedule"].update(maker_bps=9.0), a)[1], "account_fee_schedule"),
    ("fee differs from the ledger", lambda a: (a["fee_schedule"].update(taker_bps=4.0), a)[1], "account_fee_schedule"),
    ("fills counts as bools", lambda a: (a["fills"].update(fills=True), a)[1], "fills_and_fee_records"),
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


def test_integrity_grade_needs_the_archive_itself_to_anchor_every_chained_file():
    two = dict(ANCHORED, files_total=2, files_anchored_current=2)
    assert OE.integrity_grade({"integrity": dict(two, files_anchored_by_archive=2)}) == "ANCHORED"
    for by_archive in (1, 0, None, True, "2", 2.0, -1):
        assert OE.integrity_grade({"integrity": dict(two, files_anchored_by_archive=by_archive)}) == "ANCHORED_SAME_VOLUME_ONLY", by_archive
    gone = dict(two)
    gone.pop("files_anchored_by_archive")
    assert OE.integrity_grade({"integrity": gone}) == "ANCHORED_SAME_VOLUME_ONLY"           # an older object that cannot say is not anchored
    assert OE.integrity_grade({"integrity": dict(two, files_anchored_by_archive=2, anchor_location="SAME_VOLUME_COPY_NOT_VERIFIED_OFF_MACHINE")}) \
        == "ANCHORED_SAME_VOLUME_ONLY"


def test_no_default_input_to_the_report_can_reach_economic_reconciliation_or_readiness():
    """The report builds no account section: whatever the public evidence, the best it can say is a research candidate."""
    st, e = status(bundle(funding=4.0))
    assert st == OE.Status.RESEARCH_CANDIDATE and e.metrics["reconciliation_gaps"] and len(e.metrics["reconciliation_gaps"]) == len(OE.RECONCILIATION_REQUIREMENTS)


# ======================= Phase 5: maker and hybrid execution =======================

def test_maker_and_hybrid_are_unsupported_without_measured_fills_whatever_is_assumed():
    spec = OE.REGISTRY["maker_hybrid_execution/v1"]
    e = OE.evaluate_execution(spec, good_ev())
    assert e.status == OE.Status.INSUFFICIENT_EVIDENCE and e.metrics["verdict"] == "MAKER_NOT_SUPPORTED"
    assert "UNSUPPORTED" in e.reasons[0] and e.metrics["taker_remains_the_only_supported_mode"] is True
    assert e.authorizes_trading is False and e.order_path_connected is False
    out = {o["strategy"]: o for o in OE.evaluate_all(bundle(funding=4.0))}
    assert out["maker_hybrid_execution/v1"]["status"] == "INSUFFICIENT_EVIDENCE"


def test_simulated_or_derived_results_can_never_be_passed_off_as_account_fills():
    import execution_model as EM
    spec = OE.REGISTRY["maker_hybrid_execution/v1"]
    with pytest.raises(EM.EvidenceClassError):
        OE.evaluate_execution(spec, dict(good_ev(), account_execution_records=[{"qty_filled": 1.0, "source": "simulated"}]))


def test_an_unrecognised_execution_mode_is_treated_as_maker_never_as_taker():
    for mode in ("Maker", "MAKER", "hybrid ", "post_only", "", None, 3):
        acct = full_account()
        acct["execution_costs"]["provenance"].update(maker_fill_probability="ASSUMED", adverse_selection="ASSUMED")
        st, e = status(reconciled(account=acct, execution={"mode": mode}))
        assert st == OE.Status.PROSPECTIVE_MARKET_EDGE_SUPPORTED, mode
    assert status(reconciled(account=full_account(), execution={"mode": "taker"}))[0] == OE.Status.ECONOMICALLY_RECONCILED


def test_the_fee_the_account_claims_must_be_the_fee_its_own_ledger_charged():
    acct = full_account()
    acct["fee_schedule"].update(taker_bps=1.0, maker_bps=0.5)                       # a flattering claim; the ledger charged 5.5
    st, e = status(reconciled(account=acct))
    assert st == OE.Status.PROSPECTIVE_MARKET_EDGE_SUPPORTED and any("differ from the fee the account ledger charged" in g for g in e.metrics["reconciliation_gaps"])


def test_authorisation_needs_a_real_reference_not_a_placeholder():
    for ref in ("-", "x", "1234567", "        ", "!!!!!!!!!"):
        assert status(reconciled(authorisation={"owner_authorised": True, "reference": ref, "scope": "carry_overlay_taker/v1"}))[0] == OE.Status.ECONOMICALLY_RECONCILED, ref


@pytest.mark.parametrize("bad", ["a", True, float("inf"), float("nan"), -1])
def test_prospective_claims_must_be_typed_finite_and_ordered(bad):
    for field in ("registered_ms", "first_evidence_ms", "n_after_registration", "post_registration_ess", "post_registration_annualised_pct_on_economic_capital"):
        pro = dict(PRO_OK)
        pro[field] = bad
        assert status(bundle(funding=4.0, prospective=pro, integrity=ANCHORED))[0] == OE.Status.RESEARCH_CANDIDATE, (field, bad)
    pro = dict(PRO_OK, registered_ms="a", first_evidence_ms="b")
    assert status(bundle(funding=4.0, prospective=pro, integrity=ANCHORED))[0] == OE.Status.RESEARCH_CANDIDATE


def test_anchors_on_the_recorders_own_volume_do_not_grade_as_anchored():
    same = dict(ANCHORED, anchor_location="SAME_VOLUME_COPY_NOT_VERIFIED_OFF_MACHINE")
    assert OE.integrity_grade({"integrity": same}) == "ANCHORED_SAME_VOLUME_ONLY"
    assert status(bundle(funding=4.0, prospective=PRO_OK, integrity=same))[0] == OE.Status.RESEARCH_CANDIDATE
    assert OE.integrity_grade({"integrity": dict(ANCHORED, files_missing_but_anchored=["capacity-20261103.jsonl"])}) == "CONFLICT"


@pytest.mark.parametrize("field,value", [("fillable_share", float("nan")), ("round_trip_cost_bps", float("nan")), ("fillable_share", None)])
def test_non_finite_execution_inputs_fail_closed(field, value):
    b = bundle(funding=4.0)
    b["carry"][field] = value
    assert status(b)[0] == OE.Status.EXECUTION_UNATTRACTIVE


def test_non_finite_risk_and_return_inputs_fail_closed():
    for path, expect in ((("liquidation", "conservative_adverse_move_pct"), OE.Status.RISK_UNACCEPTABLE),
                         (("returns", "annualised_pct_on_economic_capital"), OE.Status.ECONOMICALLY_UNATTRACTIVE)):
        b = bundle(funding=4.0)
        b["carry"]["economics"][path[0]][path[1]] = float("nan")
        assert status(b)[0] == expect, path
    b = bundle(funding=4.0)
    b["carry"]["funding_eval"]["realised_bps"]["min"] = float("nan")
    assert status(b)[0] == OE.Status.RISK_UNACCEPTABLE


# ---- the ledger is recomputed, not trusted (delta review M3, L3-L5) ----

def _ae(**kw):
    """The account ledger, with its components or claims altered after computing it (the way a hand-assembled object would be)."""
    import copy
    ae = copy.deepcopy(account_economics())
    for key, val in kw.items():
        if key == "components":
            ae["components_usd"].update(val)
        elif key == "capital":
            ae["capital"].update(val)
        elif key == "liquidation":
            ae["liquidation"] = val if not isinstance(val, dict) else dict(ae["liquidation"], **val)
        elif key == "returns":
            ae["returns"].update(val)
        else:
            ae[key] = val
    return ae


def _gaps(**kw):
    st, e = status(reconciled(account_economics_=_ae(**kw)))
    return st, e.metrics["reconciliation_gaps"]


def test_the_untouched_ledger_is_self_consistent_and_clears():
    st, gaps = _gaps()
    assert st == OE.Status.ECONOMICALLY_RECONCILED and gaps == []
    assert OE._ledger_is_self_consistent(account_economics(), 3.8, 0.5) is True
    assert OE._ledger_return(account_economics()) == pytest.approx(account_economics()["returns"]["annualised_pct_on_economic_capital"])


def test_a_zero_exit_fee_in_the_ledger_cannot_lift_the_return_over_the_hurdle():
    # the entry leg is charged at the claimed taker rate; the exit leg is claimed free. Only checking the entry leg let this through.
    ae = account_economics()
    n = ae["capital"]["hedged_notional_usd"]
    assert ae["components_usd"]["perp_exit_fee_usd"] == pytest.approx(-n * 5.5 / 1e4)
    st, gaps = _gaps(components={"perp_exit_fee_usd": 0.0})
    assert any("account_fee_schedule" in g for g in gaps)         # (the net no longer sums either: both defects are named)
    assert st != OE.Status.ECONOMICALLY_RECONCILED


def test_spot_leg_fees_in_an_overlay_ledger_are_a_gap_not_silently_accepted():
    st, gaps = _gaps(components={"spot_entry_fee_usd": -5.0})
    assert any("account_fee_schedule" in g for g in gaps) and st != OE.Status.ECONOMICALLY_RECONCILED


@pytest.mark.parametrize("kw", [
    {"net_pnl_usd": None}, {"net_pnl_usd": -5000.0}, {"reconciles": False}, {"capital": {"initial_margin_sufficient": False}},
    {"liquidation": None}, {"liquidation": {"conservative_adverse_move_pct": 1.0}}, {"liquidation": {"conservative_adverse_move_pct": float("nan")}},
    {"returns": {"annualised_pct_on_economic_capital": 1e30}}, {"returns": {"annualised_pct_on_economic_capital": float("nan")}},
    {"components": {"funding_income_usd": 10 ** 400}}, {"net_pnl_usd": 10 ** 400}, {"components": {"basis_pnl_usd": "1"}},
    {"capital": {"economic_capital_usd": 0.0}}, {"position": {"hold_days": 0.0}}])
def test_a_ledger_that_does_not_recompute_from_its_own_components_is_not_reconciled(kw):
    st, gaps = _gaps(**kw)
    assert st != OE.Status.ECONOMICALLY_RECONCILED, kw
    assert any("account_economics" in g for g in gaps), (kw, gaps)


def test_round_trips_cannot_exceed_what_the_fills_can_form():
    acct = full_account()
    acct["fills"].update(fills=12, fee_records=12, round_trips=10 ** 9)
    st, e = status(reconciled(account=acct))
    assert st == OE.Status.PROSPECTIVE_MARKET_EDGE_SUPPORTED and any("fills_and_fee_records" in g for g in e.metrics["reconciliation_gaps"])


def test_a_missing_execution_mode_is_treated_as_maker_not_taker():
    st, e = status(reconciled(execution={}))
    assert st == OE.Status.PROSPECTIVE_MARKET_EDGE_SUPPORTED
    assert any("maker_fill_probability" in g for g in e.metrics["reconciliation_gaps"])
    acct = full_account()
    acct["execution_costs"]["provenance"].update(maker_fill_probability="MEASURED", adverse_selection="MEASURED")
    acct["fee_schedule"]["maker_bps"] = 5.5          # (so that both legs sit at a real rate for either reading of the mode)
    assert status(reconciled(account=acct, execution={}))[0] == OE.Status.ECONOMICALLY_RECONCILED


@pytest.mark.parametrize("field", ["fee_schedule", "product_qualification", "funding_settlements", "fills", "execution_costs", "capital", "venue_rules"])
@pytest.mark.parametrize("junk", [True, 7, "x", [1], None])
def test_a_non_object_container_is_a_gap_never_an_exception(field, junk):
    acct = full_account()
    acct[field] = junk
    st, e = status(reconciled(account=acct))
    assert st == OE.Status.PROSPECTIVE_MARKET_EDGE_SUPPORTED and e.metrics["reconciliation_gaps"]


@pytest.mark.parametrize("junk", [True, 7, "x", [1], 0])      # (None means "use the default" in the reconciled() helper)
def test_non_object_ledger_and_account_are_gaps_never_exceptions(junk):
    for kw in ({"account": junk}, {"account_economics_": junk}):
        st, e = status(reconciled(**kw))
        assert st == OE.Status.PROSPECTIVE_MARKET_EDGE_SUPPORTED and e.metrics["reconciliation_gaps"]


def test_an_integer_beyond_float_range_is_not_a_number_and_never_raises():
    huge = 10 ** 400
    assert OE._num(huge) is False and OE._num(-huge) is False and OE._num(True) is False and OE._num(1) and OE._num(1.5)
    acct = full_account()
    acct["fee_schedule"]["taker_bps"] = huge
    assert status(reconciled(account=acct))[0] == OE.Status.PROSPECTIVE_MARKET_EDGE_SUPPORTED


@pytest.mark.parametrize("ref", ["xxxxxxxx", "11111111", "placeholder", "TODO-TODO", "none none", "aaaaaaaaaaaa", "test-test-test", "pending-1", "n/a n/a n/a",
                                 "short", "", None, 12345678, "        "])
def test_a_placeholder_authorisation_reference_does_not_make_anything_execution_ready(ref):
    auth = {"owner_authorised": True, "reference": ref, "scope": "carry_overlay_taker/v1"}
    assert status(reconciled(authorisation=auth))[0] == OE.Status.ECONOMICALLY_RECONCILED


@pytest.mark.parametrize("ref", ["OWNER-AUTH-2026-11-05", "decision 2026-11-05 / ticket 4471", "authorisation-9b3f7c"])
def test_a_reference_shaped_like_a_record_is_still_only_a_pointer(ref):
    auth = {"owner_authorised": True, "reference": ref, "scope": "carry_overlay_taker/v1"}
    st, e = status(reconciled(authorisation=auth))
    assert st == OE.Status.EXECUTION_READY and e.authorizes_trading is False


# ---- third review: the ledger is RE-DERIVED from its own inputs; a maker rate never lifts a status; hostile numbers never raise ----

def _resynced(mutate):
    """A ledger altered by `mutate`, then its net and its claimed return re-synced to the altered components, so that ONLY a check that
    re-derives the pieces from the ledger's own inputs can still see the contradiction."""
    import copy
    ae = copy.deepcopy(account_economics())
    mutate(ae)
    ae["net_pnl_usd"] = math.fsum(ae["components_usd"].values())
    ae["returns"]["annualised_pct_on_economic_capital"] = OE._ledger_return(ae)
    return ae


def _bump(key, factor=None, add=None):
    def m(ae):
        ae["components_usd"][key] = ae["components_usd"][key] * factor if factor is not None else ae["components_usd"][key] + add
    return m


def _rescale_notional(ae):
    ae["capital"]["hedged_notional_usd"] = 1000.0
    for k in ("perp_entry_fee_usd", "perp_exit_fee_usd"):
        ae["components_usd"][k] = -1000.0 * 5.5 / 1e4                 # the fees now look like 5.5 bps of 1,000; the position and funding stay at 100,000


def _set(path, value):
    def m(ae):
        d = ae
        for k in path[:-1]:
            d = d[k]
        d[path[-1]] = value
    return m


@pytest.mark.parametrize("name,mutate", [
    ("funding line x10 against its own funding path", _bump("funding_income_usd", factor=10.0)),
    ("notional shrunk, fee legs rescaled to match", _rescale_notional),
    ("hold of 3 days against 90 settlements", _set(("position", "hold_days"), 3.0)),
    ("economic capital a tenth of inventory + margin + reserve", _set(("capital", "economic_capital_usd"), 11_000.0)),
    ("margin 'sufficient' with 1 USD of collateral against 5,000 required", lambda ae: ae["capital"].update(available_collateral_usd=1.0, required_initial_margin_usd=5000.0)),
    ("liquidation headroom 89.5 % with 1 USD of collateral", lambda ae: ae["capital"].update(available_collateral_usd=1.0, required_initial_margin_usd=0.5)),
    ("a +50,000 rebate in a taker ledger", _bump("rebates_usd", add=50_000.0)),
    ("an unnamed extra income line", lambda ae: ae["components_usd"].update(mystery_income_usd=50_000.0)),
    ("a +50,000 basis P&L nobody can verify", _bump("basis_pnl_usd", add=50_000.0)),
    ("a missing component", lambda ae: ae["components_usd"].pop("borrow_usd")),
    ("position notional differs from the capital block's", _set(("position", "notional_usd"), 50_000.0)),
])
def test_ROUND3_a_ledger_whose_pieces_contradict_each_other_is_not_reconciled_even_when_net_and_return_are_re_synced(name, mutate):
    ae = _resynced(mutate)
    st, e = status(reconciled(account_economics_=ae))
    assert st == OE.Status.PROSPECTIVE_MARKET_EDGE_SUPPORTED, name
    assert any("account_economics" in g for g in e.metrics["reconciliation_gaps"]), (name, e.metrics["reconciliation_gaps"])
    # and an authorisation record cannot lift it
    auth = {"owner_authorised": True, "reference": "OWNER-AUTH-2026-11-05", "scope": "carry_overlay_taker/v1"}
    assert status(reconciled(account_economics_=ae, authorisation=auth))[0] == OE.Status.PROSPECTIVE_MARKET_EDGE_SUPPORTED, name


def test_ROUND3_the_untouched_ledger_still_passes_every_re_derivation():
    assert OE._ledger_is_self_consistent(account_economics(), 3.8, 0.5) is True
    for hold, n in ((30.0, 90), (7.0, 21), (5.8, 17), (90.0, 270)):
        pos = position(hold_days=hold)
        costs = TE.Costs(perp_entry_fee_bps=5.5, perp_exit_fee_bps=5.5, spread_bps=0.3, slippage_bps=0.1, impact_bps=0.2, latency_bps=0.1,
                         conversion_bps=0.0, transfer_usd=0.0, custody_apr=0.0, operational_usd=0.0, borrow_apr=0.0, fee_provenance="ACCOUNT_VERIFIED")
        assert OE._ledger_is_self_consistent(TE.compute(pos, costs, [3.8] * n, funding_source="ACCOUNT_SETTLED"), 3.8, 0.5), (hold, n)


def test_ROUND3_numbers_beyond_float_range_and_odd_unknowns_never_raise():
    import copy
    ae = copy.deepcopy(account_economics())
    ae["components_usd"]["funding_income_usd"] = ae["components_usd"]["basis_pnl_usd"] = 10 ** 308
    ae["net_pnl_usd"] = 1.0
    assert OE._ledger_is_self_consistent(ae, 3.8, 0.5) is False
    st, e = status(reconciled(account_economics_=ae))
    assert st == OE.Status.PROSPECTIVE_MARKET_EDGE_SUPPORTED and e.metrics["reconciliation_gaps"]
    for junk in (5, [1, 2], {"a": 1}, "borrow", [None], (1,), True):
        bad = copy.deepcopy(account_economics())
        bad["unknowns"] = junk
        st, e = status(reconciled(account_economics_=bad))
        assert st == OE.Status.PROSPECTIVE_MARKET_EDGE_SUPPORTED, junk          # claiming unknowns of any shape blocks, and never raises
    assert OE.unknown_cost_headroom_bps({"net_pnl_usd": 10 ** 400, "capital": {"economic_capital_usd": 1, "hedged_notional_usd": 1}, "position": {"hold_days": 1}}, 10) is None
    for junk in (True, 7, "x", [1], 10 ** 400):
        assert OE.integrity_grade({"integrity": junk}) == "NOT_FULLY_ANCHORED"
        # a hostile object claiming a count never raises and never reaches ANCHORED (no files_anchored_by_archive, no external location)
        assert OE.integrity_grade({"integrity": {"files_total": junk, "files_anchored_current": junk}}) in ("NOT_FULLY_ANCHORED", "ANCHORED_SAME_VOLUME_ONLY")


@pytest.mark.parametrize("mode", ["maker", "hybrid", None, "unknown-mode"])
def test_ROUND3_a_ledger_charged_at_the_maker_rate_never_lifts_the_status(mode):
    maker_costs = TE.Costs(perp_entry_fee_bps=2.0, perp_exit_fee_bps=2.0, spread_bps=0.3, slippage_bps=0.1, impact_bps=0.2, latency_bps=0.1,
                           conversion_bps=0.0, transfer_usd=0.0, custody_apr=0.0, operational_usd=0.0, borrow_apr=0.0, fee_provenance="ACCOUNT_VERIFIED",
                           maker_taker="maker")
    ledger = TE.compute(position(), maker_costs, [3.8] * 90, funding_source="ACCOUNT_SETTLED")
    acct = full_account()
    acct["fee_schedule"].update(taker_bps=5.5, maker_bps=2.0)
    acct["execution_costs"]["provenance"].update(maker_fill_probability="MEASURED", adverse_selection="MEASURED")
    execution = {} if mode is None else {"mode": mode}
    st, e = status(reconciled(account=acct, account_economics_=ledger, execution=execution))
    assert st == OE.Status.PROSPECTIVE_MARKET_EDGE_SUPPORTED
    assert any("account_fee_schedule" in g for g in e.metrics["reconciliation_gaps"])


def test_ROUND3_hybrid_with_one_maker_leg_is_still_a_gap():
    costs = TE.Costs(perp_entry_fee_bps=2.0, perp_exit_fee_bps=5.5, spread_bps=0.3, slippage_bps=0.1, impact_bps=0.2, latency_bps=0.1,
                     conversion_bps=0.0, transfer_usd=0.0, custody_apr=0.0, operational_usd=0.0, borrow_apr=0.0, fee_provenance="ACCOUNT_VERIFIED")
    acct = full_account()
    acct["execution_costs"]["provenance"].update(maker_fill_probability="MEASURED", adverse_selection="MEASURED")
    st, e = status(reconciled(account=acct, account_economics_=TE.compute(position(), costs, [3.8] * 90, funding_source="ACCOUNT_SETTLED"),
                              execution={"mode": "hybrid"}))
    assert st == OE.Status.PROSPECTIVE_MARKET_EDGE_SUPPORTED and any("account_fee_schedule" in g for g in e.metrics["reconciliation_gaps"])


@pytest.mark.parametrize("ref", ["decision/approval-2026-11-05", "authorisation/audit-4471", "owner-attestation-4471", "latest-approval-4471",
                                 "unknown-risk-signoff-12", "exampleco-auth-7788", "sample-size-approval-77", "ticket 4471 / owner decision"])
def test_ROUND3_a_natural_reference_that_merely_contains_a_placeholder_word_is_accepted(ref):
    assert OE._real_reference(ref) is True
    auth = {"owner_authorised": True, "reference": ref, "scope": "carry_overlay_taker/v1"}
    assert status(reconciled(authorisation=auth))[0] == OE.Status.EXECUTION_READY


@pytest.mark.parametrize("ref", ["TODO-TODO", "pending-1", "n/a n/a n/a", "test-test-test", "xxxxxxxx", "placeholder", "none none", "11111111", "short",
                                 "TBD / TBD", "unknown unknown", "ref-ref-ref-1", "sample sample 1"])
def test_ROUND3_a_reference_made_only_of_placeholder_words_is_refused(ref):
    assert OE._real_reference(ref) is False


# ---- fourth review: cost lines are costs, the capital block is a real position, funding is the public market's ----

def _cost_line(key, value):
    return lambda ae: ae["components_usd"].__setitem__(key, value)


@pytest.mark.parametrize("key", ["spread_usd", "slippage_usd", "market_impact_usd", "latency_usd", "conversion_usd", "transfer_usd", "custody_usd",
                                 "operational_usd", "borrow_usd"])
def test_ROUND4_a_cost_line_with_the_sign_of_income_inflates_nothing(key):
    ae = _resynced(_cost_line(key, 50_000.0))
    assert OE._ledger_is_self_consistent(ae, 3.8, 0.5) is False
    st, e = status(reconciled(account_economics_=ae))
    assert st == OE.Status.PROSPECTIVE_MARKET_EDGE_SUPPORTED and any("account_economics" in g for g in e.metrics["reconciliation_gaps"])


def test_ROUND4_a_taker_ledger_that_paid_no_spread_at_all_is_not_a_measurement():
    assert OE._ledger_is_self_consistent(_resynced(_cost_line("spread_usd", 0.0)), 3.8, 0.5) is False


def test_ROUND4_the_unknowns_key_must_be_present_and_empty():
    for value in (None, ["latency"], 0, "", [[]]):
        ae = _resynced(lambda a, v=value: a.__setitem__("unknowns", v))
        assert OE._ledger_is_self_consistent(ae, 3.8, 0.5) is False, value
    ae = _resynced(lambda a: a.pop("unknowns"))
    assert OE._ledger_is_self_consistent(ae, 3.8, 0.5) is False


@pytest.mark.parametrize("name,mutate", [
    ("a negative reserve that shrinks economic capital to a tenth", lambda ae: (ae["capital"].update(reserve_usd=-99_000.0, economic_capital_usd=1_000.0))),
    ("a negative posted margin offset by reserve", lambda ae: ae["capital"].update(margin_posted_extra_usd=-5_000.0, reserve_usd=15_000.0)),
    ("a negative inventory", lambda ae: ae["capital"].update(btc_inventory_usd=-100.0, economic_capital_usd=9_900.0)),
    ("inventory 1,000 behind a 100,000 notional with the collateral unchanged", lambda ae: ae["capital"].update(btc_inventory_usd=1_000.0, reserve_usd=0.0, economic_capital_usd=1_000.0)),
    ("inventory 20,000 behind a 100,000 notional (a naked 80,000 short)", lambda ae: ae["capital"].update(btc_inventory_usd=20_000.0, reserve_usd=0.0, economic_capital_usd=20_000.0, available_collateral_usd=18_000.0)),
    ("available collateral above inventory + margin", lambda ae: ae["capital"].update(available_collateral_usd=500_000.0)),
    ("available collateral below the posted margin", lambda ae: ae["capital"].update(margin_posted_extra_usd=50_000.0, economic_capital_usd=160_000.0, available_collateral_usd=10_000.0)),
    ("an initial margin of a hundredth of a cent", lambda ae: ae["capital"].update(required_initial_margin_usd=0.0001)),
    ("economic capital below the available collateral", lambda ae: ae["capital"].update(economic_capital_usd=50_000.0, reserve_usd=-60_000.0)),
])
def test_ROUND4_a_capital_block_no_real_position_can_have_is_not_reconciled(name, mutate):
    ae = _resynced(mutate)
    assert OE._ledger_is_self_consistent(ae, 3.8, 0.5) is False, name
    st, e = status(reconciled(account_economics_=ae))
    assert st == OE.Status.PROSPECTIVE_MARKET_EDGE_SUPPORTED, name


def test_ROUND4_initial_margin_sufficiency_is_checked_on_its_own_not_only_through_the_liquidation_bound():
    def m(ae):
        ae["capital"].update(available_collateral_usd=25_000.0, required_initial_margin_usd=30_000.0, initial_margin_sufficient=True)
        ae["liquidation"]["conservative_adverse_move_pct"] = 24.5
    assert OE._ledger_is_self_consistent(_resynced(m), 3.8, 0.5) is False


@pytest.mark.parametrize("name,mutate", [
    ("ten times the funding path with the line matched", lambda ae: (ae["funding"].update(sum_bps=ae["funding"]["sum_bps"] * 10),
                                                                     ae["components_usd"].update(funding_income_usd=ae["components_usd"]["funding_income_usd"] * 10))),
    ("six times the funding path with the line matched", lambda ae: (ae["funding"].update(sum_bps=ae["funding"]["sum_bps"] * 6),
                                                                     ae["components_usd"].update(funding_income_usd=ae["components_usd"]["funding_income_usd"] * 6))),
    ("a per-settlement mean beyond any venue print (40 bps)", lambda ae: (ae["funding"].update(sum_bps=40.0 * 90),
                                                                          ae["components_usd"].update(funding_income_usd=100_000.0 * 40.0 * 90 / 1e4))),
])
def test_ROUND4_funding_the_public_market_never_paid_is_not_reconciled(name, mutate):
    ae = _resynced(mutate)
    assert OE._ledger_is_self_consistent(ae, 3.8, 0.5) is False, name
    st, _e = status(reconciled(account_economics_=ae))
    assert st == OE.Status.PROSPECTIVE_MARKET_EDGE_SUPPORTED, name


def test_ROUND4_without_the_public_realised_mean_the_funding_cannot_be_cross_checked_and_is_a_gap():
    ae = account_economics()
    for pm, psd in ((None, None), (float("nan"), 0.5), (3.8, None), (3.8, float("inf")), ("3.8", 0.5), (True, 0.5)):
        assert OE._ledger_is_self_consistent(ae, pm, psd) is False, (pm, psd)
    assert OE._ledger_is_self_consistent(ae, 3.8, 0.5) is True
    assert OE._ledger_is_self_consistent(ae, 3.0, 0.1) is True              # 0.8 bps from the public mean: inside the 2 bps floor
    assert OE._ledger_is_self_consistent(ae, 0.6, 0.1) is False             # 3.2 bps away with a tight public distribution


@pytest.mark.parametrize("ref", ["TODO1234", "TBD12345678", "test12345", "pending123", "placeholder1", "none12345", "todo_1234", "TODO-1234"])
def test_ROUND4_a_typed_placeholder_glued_to_digits_is_still_a_placeholder(ref):
    assert OE._real_reference(ref) is False


@pytest.mark.parametrize("ref", ["decision/approval-2026-11-05", "OWNER-AUTH-2026-11-05", "sample-size-approval-77", "ticket 4471 / owner decision",
                                 "authorisation9b3f7c"])
def test_ROUND4_natural_references_survive_the_letters_only_tokenisation(ref):
    assert OE._real_reference(ref) is True
