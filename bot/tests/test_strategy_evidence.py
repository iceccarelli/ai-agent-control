"""strategy_evidence: verdicts, allocator integration, no self-promotion, entry rule.
Every number is a TEST FIXTURE, not a claim about any strategy."""
from __future__ import annotations

import ast
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

import capital_allocator as ca                              # noqa: E402
import strategy_evidence as se                              # noqa: E402
from test_capital_allocator import S, limits, opp           # noqa: E402

NOW = 2_000_000_000.0
DAY = 86400.0
DISC = (NOW - 400 * DAY, NOW - 200 * DAY)


def trades(nets, start=NOW - 100 * DAY):
    return tuple(se.EvidenceTrade(start + i * DAY, start + i * DAY + 3600, x)
                 for i, x in enumerate(nets))


def ev(nets=None, **over):
    nets = nets if nets is not None else [1.0, -0.5] * 15
    base = dict(strategy_id="carry", trades=trades(nets), observed_at_s=NOW - 3600,
                net_basis="fixture", discovery_window_s=DISC, drawdown_limit=50.0)
    base.update(over)
    return se.StrategyEvidence(**base)


GOOD = [1.0, 0.8, 1.2, -0.3] * 8


def verdict(e, **kw):
    return se.evaluate_strategy_evidence(e, now_s=NOW, **kw)


def test_good_evidence_allocates_but_grants_no_approval():
    v = verdict(ev(GOOD))
    assert v.verdict == se.ALLOCATE and v.allows_capital
    assert v.grants_owner_approval is False
    assert v.as_dict()["grants_owner_approval"] is False


def test_unknown_expectancy_gets_nothing():
    assert verdict(None).verdict == se.INSUFFICIENT
    v = verdict(ev([]))
    assert v.verdict == se.INSUFFICIENT and se.UNKNOWN_EXPECTANCY in v.reasons


def test_negative_expectancy_blocks_even_on_small_sample():
    v = verdict(ev([-1.06, -0.61, -1.03, -1.05]))
    assert v.verdict == se.BLOCK
    assert any(r.startswith(se.NEGATIVE_NET_EXPECTANCY) for r in v.reasons)


def test_stale_evidence_rejected():
    v = verdict(ev(GOOD, observed_at_s=NOW - 3 * DAY))
    assert v.verdict == se.INSUFFICIENT
    assert any(r.startswith(se.STALE_EVIDENCE) for r in v.reasons)


def test_future_dated_or_missing_observation_rejected():
    assert verdict(ev(GOOD, observed_at_s=NOW + DAY)).verdict == se.INSUFFICIENT
    assert verdict(ev(GOOD, observed_at_s=None)).verdict == se.INSUFFICIENT


def test_too_few_trades_rejected():
    v = verdict(ev([1.0, 0.9, 1.1, 0.8, 1.0]))
    assert v.verdict == se.INSUFFICIENT
    assert any(r.startswith(se.INSUFFICIENT_TRADES) for r in v.reasons)


def test_drawdown_breach_blocks():
    v = verdict(ev(GOOD, drawdown_limit=0.1))
    assert v.verdict == se.BLOCK
    assert any(r.startswith(se.DRAWDOWN_BREACH) for r in v.reasons)


def test_unknown_drawdown_limit_is_insufficient():
    assert verdict(ev(GOOD, drawdown_limit=None)).verdict == se.INSUFFICIENT


def test_one_big_win_carrying_the_book_blocks():
    nets = [30.0] + [-0.5] * 10 + [0.1] * 14        # mean > 0 only thanks to one win
    v = verdict(ev(nets, drawdown_limit=1000.0))
    assert v.verdict == se.BLOCK
    assert any(r.startswith(se.EXPECTANCY_EXCL_LARGEST_WIN_NOT_POSITIVE)
               for r in v.reasons)
    assert v.metrics["mean_net"] > 0 and v.metrics["mean_net_excl_largest_win"] < 0


def test_profit_concentration_blocks_even_if_excl_largest_positive():
    nets = [10.0] + [0.2] * 29
    v = verdict(ev(nets))
    assert v.verdict == se.BLOCK
    assert any(r.startswith(se.PROFIT_CONCENTRATION) for r in v.reasons)


def test_overlap_with_discovery_window_blocks():
    t = trades(GOOD, start=DISC[1] - 5 * DAY)       # first trades inside discovery
    v = verdict(ev(GOOD, trades=t))
    assert v.verdict == se.BLOCK
    assert any(r.startswith(se.OVERLAPS_DISCOVERY_WINDOW) for r in v.reasons)


def test_unknown_discovery_window_is_insufficient_not_clear():
    v = verdict(ev(GOOD, discovery_window_s=None))
    assert v.verdict == se.INSUFFICIENT and se.DISCOVERY_WINDOW_UNKNOWN in v.reasons


def test_positive_mean_not_distinguishable_from_zero_is_insufficient():
    nets = [5.0, -4.0] * 10 + [0.5] * 2
    v = verdict(ev(nets, drawdown_limit=1000.0))
    assert v.verdict in (se.INSUFFICIENT, se.BLOCK)
    assert v.verdict != se.ALLOCATE


def test_malformed_trades_and_blank_basis_are_not_allocated():
    bad = ev(GOOD) 
    bad = se.StrategyEvidence(**{**bad.__dict__, "net_basis": "  "})
    assert verdict(bad).verdict == se.INSUFFICIENT
    nan = ev(GOOD, trades=trades(GOOD) + (se.EvidenceTrade(1.0, 2.0, float("nan")),))
    assert verdict(nan).verdict == se.INSUFFICIENT


def test_policy_cannot_be_loosened():
    for p in (se.EvidencePolicy(min_trades=5), se.EvidencePolicy(max_age_s=1e9),
              se.EvidencePolicy(max_top1_profit_share=0.99),
              se.EvidencePolicy(lcb_z=0.0)):
        with pytest.raises(ValueError):
            verdict(ev(GOOD), policy=p)
    verdict(ev(GOOD), policy=se.EvidencePolicy(min_trades=40))   # stricter is fine


def test_block_dominates_insufficient():
    v = verdict(ev([-1.0] * 3))
    assert v.verdict == se.BLOCK and any(r.startswith(se.INSUFFICIENT_TRADES)
                                         for r in v.reasons)


# -- the real negative forward-shadow artefact stays visible ------------------

def test_real_forward_shadow_is_blocked_and_visible_in_status():
    import forward_status as fs_mod
    art = os.path.join(ROOT, "artifacts", "forward_shadow_current.json")
    f = fs_mod.evaluate_forward_evidence(art, now=1_791_000_000.0,
                                         max_age_s=1e12)
    e = se.evidence_from_forward_status(
        f, strategy_id="funding_carry_fade_btc_v1",
        discovery_window_s=None, drawdown_limit=None)
    v = se.evaluate_strategy_evidence(e, now_s=1_791_000_000.0)
    assert v.verdict == se.BLOCK
    rep = se.status_report({v.strategy_id: v}, forward=f)
    assert rep["capital_allowed_for"] == []
    assert rep["forward_shadow"]["mean_net_r"] < 0
    assert all(x < 0 for x in rep["forward_shadow"]["net_r_per_trade"])


# -- allocator integration ---------------------------------------------------

def test_allocator_gives_zero_unless_evidence_allocates():
    ok = verdict(ev(GOOD))
    blocked = verdict(ev([-1.0] * 30))
    insuf = verdict(ev([1.0] * 3))
    for evid, expect in ((ok, True), (blocked, False), (insuf, False),
                         (None, False), ({}, False)):
        m = {"carry": evid} if isinstance(evid, se.EvidenceVerdict) else (
            {} if evid == {} else {"carry": evid})
        a = ca.allocate([opp("a")], limits(), risk_aversion=1.0, evidence=m)
        assert (a.total_allocated_usd > 0) is expect


def test_allocator_refuses_forged_or_mismatched_evidence():
    ok = verdict(ev(GOOD))
    for forged in ({"verdict": "ALLOCATE"}, "ALLOCATE", True):
        a = ca.allocate([opp("a")], limits(), risk_aversion=1.0,
                        evidence={"carry": forged})
        assert a.total_allocated_usd == 0.0
    other = se.EvidenceVerdict("other", se.ALLOCATE, (), {})
    a = ca.allocate([opp("a")], limits(), risk_aversion=1.0,
                    evidence={"carry": other})
    assert a.total_allocated_usd == 0.0
    assert ca.allocate([opp("a")], limits(), risk_aversion=1.0,
                       evidence={"carry": ok}).total_allocated_usd > 0


def test_blocked_reasons_are_reported_by_allocator():
    b = verdict(ev([-1.0] * 30))
    a = ca.allocate([opp("a")], limits(), risk_aversion=1.0, evidence={"carry": b})
    r = a.results[0]
    assert not r.eligible and r.reasons[0].startswith(ca.EVIDENCE_NOT_ALLOCATE)


# -- self-promotion impossible -----------------------------------------------

_FORBIDDEN_NAMES = {"promotion_gate", "promotion_approve", "approval_digest",
                    "live_authorized", "LIVE_TRADING_ACK", "write_approvals"}
_OWNED = ("strategy_evidence.py", "capital_allocator.py", "forward_status.py")


@pytest.mark.parametrize("fname", _OWNED)
def test_evidence_modules_cannot_reach_approval_machinery(fname):
    tree = ast.parse(open(os.path.join(ROOT, fname), encoding="utf-8").read())
    for node in ast.walk(tree):
        names = set()
        if isinstance(node, ast.Import):
            names = {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            names = {(node.module or "").split(".")[0]} | {a.name for a in node.names}
        elif isinstance(node, (ast.Name,)):
            names = {node.id}
        elif isinstance(node, ast.Attribute):
            names = {node.attr}
        elif isinstance(node, ast.Call) and getattr(node.func, "id", "") == "open":
            if len(node.args) > 1 and isinstance(node.args[1], ast.Constant) \
                    and any(c in str(node.args[1].value) for c in "wax"):
                pytest.fail(f"{fname} opens a file for writing")
        assert not (names & _FORBIDDEN_NAMES), (fname, names & _FORBIDDEN_NAMES)


def test_allocate_verdict_does_not_complete_any_human_gate_item(tmp_path):
    import promotion_gate as pg
    ok = verdict(ev(GOOD))
    assert ok.verdict == se.ALLOCATE
    gv = pg.evaluate_promotion_gate(approvals_path=str(tmp_path / "none.json"),
                                    now=NOW)
    human = {k for k, _d, o in pg.CHECKLIST_ITEMS if o in ("human",)}
    assert not gv.allows_live
    for it in gv.items:
        if it.key in human:
            assert not it.complete


def test_gate_signature_has_no_evidence_input():
    import inspect
    import promotion_gate as pg
    for fn in (pg.evaluate_promotion_gate, pg.promotion_gate_allows_live):
        assert not any("evidence" in p or "verdict" in p
                       for p in inspect.signature(fn).parameters)


# -- entry decision rule -----------------------------------------------------

def entry(**over):
    base = dict(expected_funding_bps_per_settlement=2.0, hold_days=10.0,
                notional_usd=1000.0, round_trip_fee_bps=11.0, spread_bps=1.0,
                expected_slippage_bps=2.0, borrow_apr=0.0,
                opportunity_cost_apr=0.05, income_haircut_fraction=0.25,
                income_uncertainty_bps=5.0, evidence=verdict(ev(GOOD)))
    base.update(over)
    return se.evaluate_entry_benefit(**base)


def test_entry_allowed_returns_every_component():
    d = entry()
    # income 60; fees 11, spread 1, slip 2, opp 0.05/365*1e4*10=13.70, haircut 15
    assert d.allowed and d.net_benefit_bps == pytest.approx(60 - 11 - 1 - 2 - 13.6986 - 15, abs=1e-3)
    assert set(d.components_bps) == {"income", "fees", "spread", "slippage",
                                     "financing", "opportunity_cost",
                                     "model_haircut"}
    assert d.net_benefit_usd == pytest.approx(d.net_benefit_bps / 1e4 * 1000.0)


@pytest.mark.parametrize("name", ["round_trip_fee_bps", "spread_bps",
                                  "expected_slippage_bps", "borrow_apr",
                                  "opportunity_cost_apr", "income_haircut_fraction",
                                  "income_uncertainty_bps", "hold_days",
                                  "notional_usd",
                                  "expected_funding_bps_per_settlement"])
@pytest.mark.parametrize("bad", [None, float("nan"), float("inf"), -1.0])
def test_unavailable_component_refuses_never_zero(name, bad):
    if name == "expected_funding_bps_per_settlement" and bad == -1.0:
        pytest.skip("negative funding is a valid input; it refuses as a loss")
    d = entry(**{name: bad})
    assert not d.allowed and d.reasons[0].startswith(se.REFUSE_UNAVAILABLE)
    assert d.net_benefit_bps is None and se.UNAVAILABLE in d.components_bps.values()


def test_entry_refuses_negative_and_too_uncertain_and_unsupported():
    assert se.REFUSE_NEGATIVE in entry(hold_days=1.0).reasons[0]
    assert any(r.startswith(se.REFUSE_TOO_UNCERTAIN)
               for r in entry(income_uncertainty_bps=500.0).reasons)
    for evid in (None, verdict(ev([-1.0] * 30)), verdict(None)):
        d = entry(evidence=evid)
        assert not d.allowed and d.reasons[0].startswith(se.REFUSE_NO_EVIDENCE)
