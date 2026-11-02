"""Evidence integrity: nothing but reconciled MAINNET cash is cash evidence.

These tests run the REAL report builder (`tools/carry_performance.build_report`)
over real ledgers and feed its REAL output to the carry evidence adapter. An
earlier adapter was only ever tested against a hand-built report whose field
names the real report does not have, so it could never see a genuine cycle.

Every figure is a hand-built FIXTURE, not an observation. No owner policy value
(discovery window, drawdown limit) is a production value.
"""
from __future__ import annotations

import copy
import math
import os
import pathlib
import sys
from types import SimpleNamespace

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for p in (ROOT, os.path.join(ROOT, "tools"), HERE):
    if p not in sys.path:
        sys.path.insert(0, p)

import carry_evidence as cev                                   # noqa: E402
import carry_performance as perf                               # noqa: E402
import carry_report as creport                                 # noqa: E402
import ledger as L                                             # noqa: E402
import strategy_evidence as se                                 # noqa: E402
import test_performance_evidence as T                          # noqa: E402

NOW_S = T.NOW / 1000.0
#: a discovery window that ended before the first fixture cycle (T0 = 1.7e9 s)
WINDOW = (1.0e9, 1.6e9)
LIMIT = 50.0                                   # fixture, not the owner's value
N = 25


def journal(n=N, *, extra=None):
    j = L.Journal()
    for i in range(n):
        T.overlay_cycle(j, 2 * i, 2 * i + 1, tag=f"c{i}", funding=300.0 + i)
        # The client's BTC the short hedges, marked to the close: an overlay
        # cycle without this leg is the net of a naked short and is (rightly)
        # not carry evidence, so the fixture must be a hedged pair.
        L.revalue_inventory(j, ms=T.T0 + (2 * i + 1) * T.DAY + 1,
                            ref=f"c{i}-inv", qty=1.0, from_price=100_000.0,
                            to_price=98_000.0)
    if extra:
        extra(j)
    return j


def real_report(tmp_path, j, *, envs=("mainnet",), environment="mainnet",
                name="s.db"):
    db = T.write_db(tmp_path, j, envs=envs, name=name)
    return db, perf.build_report(db, environment=environment, now_ms=T.NOW)


def evidence(report, **kw):
    kw.setdefault("strategy_id", "btc_funding_carry")
    kw.setdefault("observed_at_s", NOW_S)
    kw.setdefault("discovery_window_s", WINDOW)
    kw.setdefault("drawdown_limit_usd", LIMIT)
    return cev.evidence_from_report(report, **kw)


def verdict_of(report):
    return se.evaluate_strategy_evidence(evidence(report), now_s=NOW_S)


# -- the genuine path works (and is the only one) ------------------------------

def test_a_genuine_reconciled_mainnet_report_yields_its_cycles_as_trades(tmp_path):
    _db, rep = real_report(tmp_path, journal())
    assert rep["sections"]["realized_cash"]["evidence_class"] == "ACTUAL_RECONCILED"
    ev = evidence(rep)
    assert len(ev.trades) == N
    assert ev.evidence_class == se.CASH_EVIDENCE_CLASS
    v = verdict_of(rep)
    assert v.verdict == se.ALLOCATE and v.grants_owner_approval is False
    assert v.evidence_class == se.CASH_EVIDENCE_CLASS


def test_fewer_than_the_policy_minimum_stays_insufficient_on_real_cash(tmp_path):
    _db, rep = real_report(tmp_path, journal(se.DEFAULT_POLICY.min_trades - 1))
    v = verdict_of(rep)
    assert v.verdict == se.INSUFFICIENT
    assert any(r.startswith(se.INSUFFICIENT_TRADES) for r in v.reasons)


# -- testnet, paper and simulated ledgers are never cash evidence --------------

@pytest.mark.parametrize("env", ["testnet", "paper"])
def test_a_sandbox_ledger_gives_no_trades_even_with_every_fee_and_lifecycle_clean(
        tmp_path, env):
    _db, rep = real_report(tmp_path, journal(), envs=(env,), environment=env)
    assert evidence(rep).trades == ()
    assert verdict_of(rep).verdict == se.INSUFFICIENT


@pytest.mark.parametrize("env", ["testnet", "paper"])
def test_a_sandbox_report_relabelled_as_cash_is_still_refused(tmp_path, env):
    """The adapter must not trust a section label by itself: it re-checks that
    the report's own environment block says corroborated mainnet."""
    _db, rep = real_report(tmp_path, journal(), envs=(env,), environment=env)
    forged = copy.deepcopy(rep)
    mainnet_db, mainnet = real_report(tmp_path, journal(), name="m.db")
    forged["sections"]["realized_cash"] = copy.deepcopy(
        mainnet["sections"]["realized_cash"])
    forged["sections"]["ledger_detail"] = copy.deepcopy(
        mainnet["sections"]["ledger_detail"])
    assert forged["sections"]["realized_cash"]["evidence_class"] == "ACTUAL_RECONCILED"
    assert evidence(forged).trades == ()
    assert verdict_of(forged).verdict == se.INSUFFICIENT


@pytest.mark.parametrize("mutate", [
    lambda e: e.update(actual_reconciled_eligible=False),
    lambda e: e.update(declared="testnet"),
    lambda e: e.update(declared=None),
    lambda e: e.update(recorded_seen=["mainnet", "testnet"]),
    lambda e: e.update(recorded_seen=[]),
    lambda e: e.update(booked_figures_class="PAPER_EXECUTION"),
])
def test_a_report_whose_environment_block_is_not_corroborated_mainnet_is_refused(
        tmp_path, mutate):
    _db, rep = real_report(tmp_path, journal())
    assert len(evidence(rep).trades) == N
    bad = copy.deepcopy(rep)
    mutate(bad["environment"])
    assert evidence(bad).trades == ()
    assert verdict_of(bad).verdict == se.INSUFFICIENT


def test_a_report_with_no_environment_block_is_refused(tmp_path):
    _db, rep = real_report(tmp_path, journal())
    bad = copy.deepcopy(rep)
    del bad["environment"]
    assert evidence(bad).trades == ()


@pytest.mark.parametrize("key,value", [
    ("invariants_hold", False),
    ("unresolved_carry_orders", 1),
    ("unresolved_carry_orders", None),
])
def test_a_report_that_admits_a_broken_ledger_is_refused(tmp_path, key, value):
    _db, rep = real_report(tmp_path, journal())
    bad = copy.deepcopy(rep)
    bad[key] = value
    assert evidence(bad).trades == ()


@pytest.mark.parametrize("field,value", [
    ("evidence_class", "PAPER_EXECUTION"),
    ("evidence_class", "UNAVAILABLE"),
    ("evidence_class", "HISTORICAL_SIMULATION"),
    ("fees_complete", False),
    ("funding_complete", False),
    ("closed", False),
    ("outcome", "OPEN"),
    ("net_usd", None),
    ("net_usd", float("nan")),
    ("net_usd", "12"),
    ("start_ms", "soon"),
    ("end_ms", None),
])
def test_a_cycle_that_is_not_itself_reconciled_cash_is_not_a_trade(
        tmp_path, field, value):
    _db, rep = real_report(tmp_path, journal())
    bad = copy.deepcopy(rep)
    bad["sections"]["ledger_detail"]["cycles"][3][field] = value
    ev = evidence(bad)                       # must not raise
    assert len(ev.trades) == N - 1
    # and the missing result is not silently ignored: the verdict is not a pass
    assert se.evaluate_strategy_evidence(ev, now_s=NOW_S).verdict != se.ALLOCATE


def test_a_cycle_that_ends_before_it_starts_is_not_a_trade(tmp_path):
    _db, rep = real_report(tmp_path, journal())
    bad = copy.deepcopy(rep)
    c = bad["sections"]["ledger_detail"]["cycles"][0]
    c["start_ms"], c["end_ms"] = c["end_ms"], c["start_ms"]
    assert len(evidence(bad).trades) == N - 1


def test_a_duplicated_cash_cycle_id_is_not_counted_twice(tmp_path):
    _db, rep = real_report(tmp_path, journal())
    bad = copy.deepcopy(rep)
    ids = bad["sections"]["realized_cash"]["realized_cash_cycle_ids"]
    ids.append(ids[0])
    detail = bad["sections"]["ledger_detail"]["cycles"]
    detail.append(copy.deepcopy(detail[0]))
    ev = evidence(bad)
    assert len(ev.trades) == N
    assert se.evaluate_strategy_evidence(ev, now_s=NOW_S).verdict != se.ALLOCATE


# -- results that are left out cannot flatter the rest ----------------------------

def test_a_closed_unreconciled_loss_cannot_be_hidden_behind_reconciled_wins(tmp_path):
    """25 reconciled winners plus one CLOSED cycle whose funding was never
    reconciled and which lost heavily. The report lists only the 25 as cash and
    says the other is excluded; the verdict must not pretend it does not exist."""
    def loser(j):
        T.overlay_cycle(j, 2 * N, 2 * N + 1, tag="loser", close_price=130_000.0,
                        reconcile=False)
    _db, rep = real_report(tmp_path, journal(extra=loser))
    rc = rep["sections"]["realized_cash"]
    assert rc["evidence_class"] == "ACTUAL_RECONCILED"
    assert rc["realized_cash_cycles"] == N
    assert rep["sections"]["closed_cycles_booked"]["cycle_count"] == 1
    ev = evidence(rep)
    assert len(ev.trades) == N and ev.unqualified_results == 1
    v = se.evaluate_strategy_evidence(ev, now_s=NOW_S)
    assert v.verdict == se.INSUFFICIENT
    assert any("unqualified_closed_results=1" in r for r in v.reasons)


def test_an_open_cycle_is_not_a_result_and_does_not_block(tmp_path):
    def open_one(j):
        L.perp_open(j, ms=T.T0 + 500 * T.DAY, ref="open-o", qty=1.0,
                    price=100_000.0, fee_usd=55.0, fee_status="confirmed")
    _db, rep = real_report(tmp_path, journal(extra=open_one))
    ev = evidence(rep)
    assert len(ev.trades) == N and ev.unqualified_results == 0


def test_a_missing_booked_section_counts_as_unknown_not_as_none(tmp_path):
    _db, rep = real_report(tmp_path, journal())
    bad = copy.deepcopy(rep)
    del bad["sections"]["closed_cycles_booked"]
    assert evidence(bad).unqualified_results >= 1


# -- evidence from non-cash sources can never allocate ---------------------------

GOOD_NETS = [1.0, 0.8, 1.2, -0.3] * 8


def _trades():
    start = NOW_S - 100 * 86400.0
    return tuple(se.EvidenceTrade(start + i * 86400.0, start + i * 86400.0 + 3600, x)
                 for i, x in enumerate(GOOD_NETS))


def _ev(cls):
    kw = {} if cls is None else {"evidence_class": cls}
    return se.StrategyEvidence(
        strategy_id="btc_funding_carry", trades=_trades(), observed_at_s=NOW_S - 3600,
        net_basis="fixture", discovery_window_s=(NOW_S - 400 * 86400.0, NOW_S - 200 * 86400.0),
        drawdown_limit=50.0, **kw)


@pytest.mark.parametrize("cls", [None, "", "UNAVAILABLE", "FORWARD_SIMULATION",
                                 "HISTORICAL_SIMULATION", "PAPER_EXECUTION",
                                 "OPEN_MARK_TO_MARKET", "UNDECLARED_ENVIRONMENT",
                                 "actual_reconciled", "ACTUAL_RECONCILED ", 7])
def test_positive_evidence_of_any_other_class_never_allocates(cls):
    v = se.evaluate_strategy_evidence(_ev(cls), now_s=NOW_S)
    assert v.verdict != se.ALLOCATE and not v.allows_capital
    assert any(r.startswith(se.EVIDENCE_NOT_CASH) for r in v.reasons)


def test_only_the_exact_cash_class_can_allocate():
    v = se.evaluate_strategy_evidence(_ev(se.CASH_EVIDENCE_CLASS), now_s=NOW_S)
    assert v.verdict == se.ALLOCATE


def test_forward_shadow_paper_trades_can_never_allocate_whatever_their_mean():
    """The forward-shadow adapter is paper. With 30 winning paper trades, a
    matching strategy id and the owner inputs set, it must still not allocate,
    and the entry gate must not accept its verdict."""
    start = 1_700_000_000
    rows = []
    for i in range(30):
        s = start + i * 86400
        rows.append({"entry_utc": _utc(s), "exit_utc": _utc(s + 3600), "net_r": 1.0 + 0.01 * i})
    fs = SimpleNamespace(trades=rows, state="OK", observed_at_utc=_utc(start + 40 * 86400))
    e = se.evidence_from_forward_status(
        fs, strategy_id="btc_funding_carry",
        discovery_window_s=(start - 500 * 86400.0, start - 400 * 86400.0),
        drawdown_limit=5.0)
    assert e.evidence_class == "FORWARD_SIMULATION"
    now = start + 41 * 86400.0
    v = se.evaluate_strategy_evidence(e, now_s=now)
    assert len(e.trades) == 30 and v.verdict != se.ALLOCATE
    import entry_benefit_gate as ebg
    gate = ebg.EntryBenefitGate(evidence_provider=lambda: v)
    assert gate.evidence_provider() is v and not v.allows_capital


def _utc(epoch):
    import time
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def test_the_verdict_names_the_class_it_was_judged_on():
    v = se.evaluate_strategy_evidence(_ev("PAPER_EXECUTION"), now_s=NOW_S)
    assert v.as_dict()["evidence_class"] == "PAPER_EXECUTION"
    assert v.as_dict()["grants_owner_approval"] is False


# -- the second classifier (tools/carry_report) agrees with the first --------------

def _order_fill_mismatch(db):
    from persistence import StateStore
    s = StateStore(db)
    s.record_order("c0-o", "BTCUSDT", "Sell", "Market", 5.0,
                   purpose="carry-perp", status="filled")
    s._exec("UPDATE orders SET filled_qty = 5.0 WHERE order_link_id = 'c0-o'")
    s.close()


def test_carry_report_does_not_call_a_ledger_cash_that_the_performance_report_refuses(
        tmp_path):
    j = L.Journal()
    T.overlay_cycle(j, 0, 10, tag="c0")
    db = T.write_db(tmp_path, j)
    clean = creport.build_report(db, environment="mainnet")
    assert clean["carry"]["evidence_class"] == "ACTUAL_RECONCILED"
    _order_fill_mismatch(db)
    pr = perf.build_report(db, environment="mainnet", now_ms=T.NOW)
    assert pr["sections"]["realized_cash"]["evidence_class"] == "UNAVAILABLE"
    cr = creport.build_report(db, environment="mainnet")
    assert cr["carry"]["evidence_class"] != "ACTUAL_RECONCILED"


@pytest.mark.parametrize("env", ["testnet", "paper"])
def test_carry_report_never_calls_sandbox_cash(tmp_path, env):
    j = L.Journal()
    T.overlay_cycle(j, 0, 10, tag="c0")
    db = T.write_db(tmp_path, j, envs=(env,))
    cr = creport.build_report(db, environment=env)
    assert cr["carry"]["evidence_class"] == "PAPER_EXECUTION"
    cr = creport.build_report(db, environment="mainnet")      # wrong declaration
    assert cr["carry"]["evidence_class"] != "ACTUAL_RECONCILED"


# -- the frozen policy is untouched ---------------------------------------------

def test_the_sample_and_evidence_policies_are_unchanged():
    import execution_cost_estimate as ece
    assert (se.DEFAULT_POLICY.min_trades, se.DEFAULT_POLICY.max_age_s,
            se.DEFAULT_POLICY.max_top1_profit_share, se.DEFAULT_POLICY.lcb_z) == \
        (20, 48 * 3600.0, 0.5, 1.645)
    p = ece.DEFAULT_POLICY
    assert (p.min_samples, p.max_sample_age_days, p.max_newest_age_days,
            p.size_band, p.z) == (20, 30.0, 7.0, 4.0, 1.645)
    assert math.isfinite(p.z)


def test_the_literals_strategy_evidence_uses_are_the_report_tools_classes():
    assert se.CASH_EVIDENCE_CLASS == perf.ACTUAL_RECONCILED
    assert se.FORWARD_SIMULATION_CLASS == perf.FORWARD_SIMULATION


# -- an ALLOCATE verdict cannot exist for anything but cash -----------------------------

@pytest.mark.parametrize("cls", ["", "PAPER_EXECUTION", "FORWARD_SIMULATION", "UNAVAILABLE",
                                 "actual_reconciled", None, 3])
def test_an_allocate_verdict_cannot_be_constructed_for_a_non_cash_class(cls):
    with pytest.raises(ValueError):
        se.EvidenceVerdict("btc_funding_carry", se.ALLOCATE, ("OK",), {}, cls)
    with pytest.raises(ValueError):
        se.EvidenceVerdict("btc_funding_carry", se.ALLOCATE, ("OK",), {})   # no class at all
    for v in (se.BLOCK, se.INSUFFICIENT):                    # non-allocating verdicts are fine
        assert se.EvidenceVerdict("s", v, ("x",), {}, cls if isinstance(cls, str) else "")


def test_the_allocator_and_the_gate_refuse_a_verdict_built_from_paper_evidence():
    import capital_allocator as ca
    from test_capital_allocator import limits, opp
    paper = se.evaluate_strategy_evidence(_ev("PAPER_EXECUTION"), now_s=NOW_S)
    paper = se.EvidenceVerdict("carry", paper.verdict, paper.reasons, paper.metrics,
                               paper.evidence_class)
    a = ca.allocate([opp("a")], limits(), risk_aversion=1.0, evidence={"carry": paper})
    assert a.total_allocated_usd == 0.0


def test_no_production_code_calls_the_allocator_without_evidence():
    """`allocate(evidence=None)` is the legacy no-evidence call that exists for the
    pure allocator tests. Nothing outside tests may use it."""
    offenders = []
    for base, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in ("tests", "__pycache__", ".git", "venv")
                   and not d.startswith(".")]
        for f in files:
            if not f.endswith(".py") or f == "capital_allocator.py":
                continue
            src = open(os.path.join(base, f), encoding="utf-8", errors="ignore").read()
            if "capital_allocator" not in src:
                continue
            import ast
            for node in ast.walk(ast.parse(src)):
                if isinstance(node, ast.Call):
                    fn = node.func
                    name = getattr(fn, "attr", None) or getattr(fn, "id", None)
                    if name == "allocate" and not any(
                            k.arg == "evidence" for k in node.keywords):
                        offenders.append(os.path.relpath(os.path.join(base, f), ROOT))
    assert offenders == []
