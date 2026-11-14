"""candidate_competition: ranked, machine-readable, never a forced winner; persistent multiple-testing ledgers by default; committed-corpus BTC rows labelled in-sample."""
from __future__ import annotations

import json
import os
import sys
import types
import uuid

import pytest

BOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (BOT, os.path.join(BOT, "tools"), os.path.join(BOT, "tests")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import candidate_competition as CC_  # noqa: E402
import candidate_readiness as CR  # noqa: E402
import research_harness as RH  # noqa: E402
from test_candidate_readiness import MARK_IV, _hist, offline  # noqa: E402,F401  (real-writer fixtures; their tests are not re-collected from here)


def _reset():
    RH._FAMILY_HIGH_WATER.clear()
    RH._PATH_HIGH_WATER.clear()


@pytest.fixture(scope="module")
def committed(tmp_path_factory):
    _reset()
    d = tmp_path_factory.mktemp("led")
    return CC_.run_competition(ledger_dir=str(d)), str(d)


def row(rep, name):
    return next(r for r in rep["results"] if r["name"] == name)


def test_committed_corpus_runs_the_btc_row_labelled_in_sample_and_every_candidate_is_present(committed):
    rep, _ = committed
    names = {r["name"] for r in rep["results"]}
    assert {"cross_sectional_funding", "spot_perp_basis", "cash_and_carry", "market_neutral_relative_value", "passive_execution", "liquidity_imbalance", "spread_microstructure",
            "cross_venue_funding"} <= names
    json.dumps(rep, default=str)
    b = row(rep, "cash_and_carry")
    assert b["label"] == "COMMITTED_CORPUS_IN_SAMPLE" and "COMMITTED_CORPUS_IN_SAMPLE" in b["net_edge"]["basis"]
    pol = {p["policy"]: p for p in b["policies"]}
    assert pol["unconditional_1_print_hold"]["verdict"] == "REJECTED" and pol["unconditional_1_print_hold"]["mean_net_bps"] == pytest.approx(-10.78, abs=0.05)
    p51 = pol["unconditional_51_print_hold"]
    assert p51["mean_net_bps"] == pytest.approx(20.2, abs=0.05)                                      # nominal: bps of NOTIONAL at ZERO capital cost (unchanged, still reported)
    assert p51["mean_excess_over_cash_bps_of_capital"] == pytest.approx(-5.75, abs=0.05)             # on the 1.16x capital it ties up, against the repo's own cash series
    assert p51["verdict"] == "REJECTED" and "does not beat the cash alternative" in p51["note"]      # a positive nominal net that loses to cash is not INSUFFICIENT_DATA
    assert p51["latest_year_mean_net_bps"] < 0                                                       # the regime that pays is not the current one
    assert pol["production_gated_sequence"]["trailing_365d_mean_net_bps"] == pytest.approx(-4.16, abs=0.05)
    assert pol["production_gated_sequence"]["mean_excess_over_cash_bps_of_capital"] < 0 and pol["production_gated_sequence"]["verdict"] == "REJECTED"
    assert b["verdict"] == "REJECTED" and b["readiness_level"] == "NONE"
    assert b["net_edge"]["value_bps"] == pytest.approx(-5.75, abs=0.05) and b["nominal_net_bps_zero_capital_cost"] == pytest.approx(20.2, abs=0.05)
    assert "EXCESS over cash" in b["net_edge"]["basis"] and "ZERO capital cost" in b["net_edge"]["basis"]
    assert any("REJECTED policy" in r for r in b["rejection_reasons"])


def test_every_row_without_a_wired_evaluator_says_so_and_nothing_else_changes(committed):
    """Round 18 (red team): ``run_competition`` hands the ladder a completed walk-forward for ONE family (cross_sectional_funding). The other rows can never reach PROMISING whatever the data, so
    their NO WINNER is structural, not a measured failure. The typed reason is reasons-only: the verdicts and levels the committed-corpus tests pin above are untouched."""
    rep, _ = committed
    assert CC_.WIRED_EVALUATORS == ("cross_sectional_funding",)
    for r in rep["results"]:
        wired = r["name"] in CC_.WIRED_EVALUATORS
        assert r["evaluator"] == {"wired": wired, "code": None if wired else f"NO_EVALUATOR_WIRED:{r['name']}"}
        assert wired or any(x.startswith(f"NO_EVALUATOR_WIRED:{r['name']}") for x in r["rejection_reasons"]), r["name"]
        assert wired is (not any(x.startswith("NO_EVALUATOR_WIRED") for x in r["rejection_reasons"]))
    b = rep["no_winner_breakdown"]
    assert b["evaluator_wired"] == ["cross_sectional_funding"] and "cash_and_carry" in b["structural_no_evaluator_wired"] and len(b["structural_no_evaluator_wired"]) == len(rep["results"]) - 1
    assert rep["winner"] is None and rep["winner_statement"].startswith("NO WINNER")


def test_missing_candidate_specs_are_a_typed_refusal_not_a_traceback(tmp_path, monkeypatch, capsys):
    """Round 18 (red team): the autopilot image ships these modules but not research/candidates; the CLI died with an uncaught FileNotFoundError at load_candidates."""
    import candidate_readiness as CR_
    monkeypatch.setattr(CR_, "CAND_DIR", str(tmp_path / "not_shipped"))
    with pytest.raises(RH.HarnessError, match="CANDIDATE_SPECS_NOT_SHIPPED"):
        CR_.load_candidates()
    assert CC_.main(["--ledger-dir", str(tmp_path / "led"), "--no-historical-cash"]) == 2
    assert "CANDIDATE_SPECS_NOT_SHIPPED" in capsys.readouterr().err
    with pytest.raises(SystemExit) as ex:
        CR_.main([])
    assert ex.value.code == 2


def test_the_wired_evaluator_set_is_the_set_run_competition_actually_builds():
    import ast
    import inspect
    src = ast.parse(inspect.getsource(CC_.run_competition))
    keys = []
    for node in ast.walk(src):
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "evals" for t in node.targets) and isinstance(node.value, ast.IfExp):
            for d in ast.walk(node.value.body):
                if isinstance(d, ast.Dict):
                    keys += [k.value for k in d.keys if isinstance(k, ast.Constant)]
    assert keys == ["cross_sectional_funding"] == list(CC_.WIRED_EVALUATORS), keys          # wiring a second evaluator must update WIRED_EVALUATORS (and is the owner's decision for the BTC families)


def test_a_btc_rejection_says_how_strong_it_is_by_the_repos_own_early_rejection_standard(committed):
    """REJECTED here is the competition's documented in-sample verdict (it loses after costs or to cash by point estimate). The repo has a STRONGER standard for rejecting (candidate_readiness.early_rejection:
    >= 12 independent observations, lower-tail p <= 0.01, the widest 90% interval wholly below zero). Every policy now carries whether it meets it, with the numbers, so a point-estimate loss is never read as a
    statistical rejection (independent specialist review: the 51-print overlay does NOT meet it; ESS-adjusted p_lower 0.034 and a widest interval that includes zero)."""
    rep, _ = committed
    b = row(rep, "cash_and_carry")
    pol = {p["policy"]: p for p in b["policies"]}
    for name in ("unconditional_1_print_hold", "unconditional_3_print_hold", "unconditional_9_print_hold", "unconditional_17_print_hold"):
        bar = pol[name]["statistical_rejection"]
        assert bar["meets_early_rejection_standard"] is True and bar["p_lower"] <= CR.REJECT_EARLY_ALPHA and bar["independent_observations"] >= CR.REJECT_EARLY_MIN_INDEPENDENT and bar["widest_ci90"][1] < 0, name
    p51 = pol["unconditional_51_print_hold"]
    bar = p51["statistical_rejection"]
    assert p51["verdict"] == "REJECTED"                                                   # the documented point-estimate verdict is unchanged ...
    assert bar["meets_early_rejection_standard"] is False and bar["scope"] == "IN_SAMPLE_OVERLAPPING_ENTRIES"          # ... and it is NOT a statistical rejection at the repo's standard
    assert bar["p_lower"] == pytest.approx(0.034, abs=0.01) and bar["p_lower"] > CR.REJECT_EARLY_ALPHA
    assert bar["widest_ci90"][0] < 0 < bar["widest_ci90"][1]                              # the widest method's 90% interval includes zero: break-even is not excluded
    assert bar["basis"] == "mean_excess_over_cash_bps_of_capital" and bar["mean"] == pytest.approx(-5.75, abs=0.05)
    assert "NOT statistically rejected" in p51["note"] and "includes zero" in p51["note"]
    g = pol["production_gated_sequence"]["statistical_rejection"]
    assert g["scope"] == "IN_SAMPLE_OVERLAPPING_ENTRIES" and set(g) >= {"meets_early_rejection_standard", "p_lower", "widest_ci90", "independent_observations", "basis"}
    summary = b["statistical_rejection_summary"]
    assert "unconditional_51_print_hold" in summary["not_meeting_the_standard"] and "unconditional_1_print_hold" in summary["meeting_the_standard"]
    assert b["verdict"] == "REJECTED" and any("point estimate" in r and "statistical" in r.lower() for r in b["rejection_reasons"])
    assert rep["winner"] is None and "NO WINNER" in rep["winner_statement"]              # the economic conclusion is unchanged: nothing is a winner


def _bar(**kw):
    return {"meets_early_rejection_standard": False, "p_lower": 0.005, "widest_ci90": [-9.0, 2.0], "independent_observations": 30, "basis": "mean_excess_over_cash_bps_of_capital", **kw}


def test_the_strength_note_prints_the_criterion_that_actually_failed_and_never_a_false_inequality():
    """Second independent review: the note printed ``p=0.005 > 0.01`` when the interval was what failed, and 'does not exclude break-even' when the interval was wholly negative."""
    note = CC_._strength_note
    assert note(False, _bar()) == "" and note(True, None) == ""
    ok = note(True, _bar(meets_early_rejection_standard=True, p_lower=0.004, widest_ci90=[-9.0, -1.0]))
    assert ok.startswith("; meets the repo's early-rejection standard") and "NOT" not in ok
    # p passes, the interval is what fails: no statement about p being too high
    n1 = note(True, _bar(p_lower=0.005, widest_ci90=[-9.0, 2.0]))
    assert "includes zero" in n1 and "above" not in n1 and "p=0.005" not in n1 and "NOT statistically rejected" in n1
    # the 51-print numbers of the committed corpus: p fails AND the interval includes zero
    n2 = note(True, _bar(p_lower=0.034, widest_ci90=[-15.7, 4.2]))
    assert "p=0.034 is above 0.01" in n2 and "includes zero" in n2
    # the interval is wholly negative, p is what fails: it must NOT say the interval includes zero or that break-even is not excluded
    n3 = note(True, _bar(p_lower=0.02, widest_ci90=[-9.0, -1.0]))
    assert "p=0.020 is above 0.01" in n3 and "includes zero" not in n3 and "not wholly below zero" not in n3 and "exclude break-even" not in n3
    n4 = note(True, _bar(independent_observations=5))
    assert "only 5 independent observations (the standard needs 12)" in n4
    n5 = note(True, _bar(p_lower=None, widest_ci90=None, independent_observations=None))
    assert "no usable lower-tail p" in n5 and "no usable 90% interval" in n5 and "only 0 independent observations" in n5
    assert "is not wholly below zero" in note(True, _bar(p_lower=0.004, widest_ci90=[-3.0, 0.0]))                  # an upper bound of exactly 0 is not below zero
    assert "a criterion of the standard could not be evaluated" in note(True, _bar(p_lower=0.004, widest_ci90=[-3.0, -1.0]))     # nothing failed yet the bar says it does not meet: said, never invented
    import re
    for bar in (_bar(p_lower=0.034), _bar(p_lower=0.011), _bar(p_lower=0.5, widest_ci90=[-1.0, 1.0])):
        for m in re.finditer(r"p=([0-9.]+) is above ([0-9.]+)", note(True, bar)):
            assert float(m.group(1)) > float(m.group(2)), "a printed inequality is false"


def test_the_table_caveat_is_only_written_when_some_policy_is_rejected():
    """Re-review finding 13: with no REJECTED policy the first reason read 'IN-SAMPLE; all 0 REJECTED policies meet the stat. rejection bar'."""
    f = CC_._table_caveat
    assert f([], []) is None, "no REJECTED policy: no caveat"
    assert f(["unconditional_1_print_hold", "production_gated_sequence"], []) == "IN-SAMPLE; all 2 REJECTED policies meet the stat. rejection bar"
    assert f(["unconditional_1_print_hold"], ["unconditional_51_print_hold"]) == "IN-SAMPLE; point-estimate only (NOT stat. rejected): 51p"
    assert f([], ["unconditional_51_print_hold", "production_gated_sequence"]) == "IN-SAMPLE; point-estimate only (NOT stat. rejected): 51p, gated_seq"
    assert all(len(x) <= 90 for x in (f(["a"] * 3, []), f([], ["unconditional_51_print_hold", "production_gated_sequence"]))), "the table prints 90 characters of it"


def test_the_ranked_table_shows_the_point_estimate_only_caveat_first_for_the_btc_row(committed):
    rep, _ = committed
    b = row(rep, "cash_and_carry")
    assert b["rejection_reasons"][0].startswith("IN-SAMPLE; point-estimate only (NOT stat. rejected):") and "51p" in b["rejection_reasons"][0], b["rejection_reasons"][0]
    line = next(x for x in CC_.render_table(rep).splitlines() if x.split("\t")[1] == "cash_and_carry")
    assert "point-estimate only" in line, line
    assert any(r.startswith("REJECTED policy unconditional_1_print_hold") for r in b["rejection_reasons"]), "the per-policy reasons are still all there"


def test_the_candidate_readiness_verdict_and_the_competition_still_agree_on_the_same_evidence(committed):
    rep, _ = committed
    econ = CR.build_report(with_economics=True)["economics"]["btc_carry_corpus"]
    verdict, reasons, policies = CR._carry_verdict(econ)
    assert verdict == row(rep, "cash_and_carry")["verdict"] == "REJECTED"
    assert any("statistical" in r.lower() for r in reasons)


def test_never_a_forced_winner_and_the_next_hypotheses_are_the_registered_untested_drafts(committed):
    rep, _ = committed
    assert rep["winner"] is None and rep["all_failed"] is True and "NO WINNER" in rep["winner_statement"]
    assert {h["name"] for h in rep["next_hypotheses"]} == {"funding_regime_conditioned_entry", "cross_interval_funding_dispersion", "hedgeable_majors_basis_convergence"}
    for h in rep["next_hypotheses"]:
        assert h["status"].startswith("UNTESTED") and h["rationale"]
        assert row(rep, h["name"])["verdict"] == "UNTESTED" and row(rep, h["name"])["origin"] == "DRAFT_PROPOSED"
    for r in rep["results"]:
        assert r["verdict"] in ("REJECTED", "INSUFFICIENT_DATA", "UNTESTED") and r["highest_supportable_verdict"] == r["verdict"]
        assert r["readiness_level"] not in ("PROMISING", "PROSPECTIVE", "EXECUTION_READY", "OWNER_PROFIT_EVIDENCED")


def test_missing_data_is_untested_not_rejected_and_cross_venue_is_research_only_and_unranked(committed):
    rep, _ = committed
    for n in ("spot_perp_basis", "liquidity_imbalance", "passive_execution", "spread_microstructure", "market_neutral_relative_value"):
        r = row(rep, n)
        assert r["verdict"] == "UNTESTED" and r["net_edge"]["value_bps"] is None and r["data_readiness"]["missing"], n
    cv = row(rep, "cross_venue_funding")
    assert cv["origin"] == "RESEARCH_ONLY" and cv["rank"] is None and cv["executable_on_qualified_venue"]["executable"] is False
    assert [r["rank"] for r in rep["results"] if r["rank"] is not None] == list(range(1, len([1 for r in rep["results"] if r["rank"] is not None]) + 1))
    assert [r["name"] for r in rep["results"] if r["executable_on_qualified_venue"]["executable"]] == ["cash_and_carry"]


def test_without_a_universe_dir_the_cross_sectional_row_says_so_loudly(tmp_path, offline):
    _reset()
    root, uni, _ = _hist(tmp_path)
    rep = CC_.run_competition(data_root=root, ledger_dir=str(tmp_path / "led"))
    x = row(rep, "cross_sectional_funding")
    assert x["verdict"] == "UNTESTED" and any("UNIVERSE_DIR_REQUIRED" in r for r in x["rejection_reasons"]) and x["universe_dir"] == "UNIVERSE_DIR_REQUIRED"
    assert any("UNIVERSE_DIR_REQUIRED" in f for f in rep["findings"])


def test_with_verified_data_and_a_universe_the_three_families_are_evaluated_and_never_exceed_the_ceiling(tmp_path, offline):
    _reset()
    root, uni, spot = _hist(tmp_path)
    ld = str(tmp_path / "led")
    rep = CC_.run_competition(data_root=root, universe_dir=uni, spot_dir=spot, mark_interval=MARK_IV, ledger_dir=ld, unreserved_exploratory=True)         # this test is about the family plumbing: the holdout guard has its own file
    x = row(rep, "cross_sectional_funding")
    assert set(x["families"]) == set(CR.FAMILIES) and x["verdict"] == "INSUFFICIENT_DATA" and x["readiness_level"] in ("NONE", "DATA_READY")
    assert x["families"]["PERP_FUNDING_SPREAD"]["is_carry"] is False and x["families"]["SPOT_PERP_CARRY"]["is_carry"] is True
    assert any("TOO_FEW_HOLDING_ROWS" in r and "457 days" in r for r in x["rejection_reasons"])
    assert x["multiple_testing"]["scope"] == "PERSISTENT_LEDGER_FILE" and x["multiple_testing"]["n_variants_counted"] > 0
    for k in CR.FAMILIES:
        assert os.path.exists(os.path.join(ld, f"cross_sectional_funding.{k.lower()}.jsonl"))
    assert not os.path.exists(os.path.join(root, "candidate_ledgers"))                                # evidence directories stay byte-for-byte what the verifier hashed
    assert rep["winner"] is None


def test_the_cross_sectional_row_carries_the_data_state_with_the_evaluators_verdict_folded_in(tmp_path, offline, monkeypatch):
    """Round 18 review: the two DATA_SUFFICIENT_* verdict states were never produced by any code. The competition row is where an evaluation's verdict meets the state of the data it read."""
    _reset()
    root, uni, spot = _hist(tmp_path)                                                      # the default fixture: three symbols are PARTIAL over the window
    x = row(CC_.run_competition(data_root=root, universe_dir=uni, spot_dir=spot, mark_interval=MARK_IV, ledger_dir=str(tmp_path / "led"), unreserved_exploratory=True), "cross_sectional_funding")
    assert x["dataset_state"]["state"] == "DATA_INCOMPLETE" and x["readiness_levels"]["DATA_READY"] is False
    _reset()
    other = tmp_path / "ok"
    other.mkdir()
    monkeypatch.setattr(CR, "STATE_MIN_ROWS", 10)                                          # complete data that the registered 60 rows would call insufficient is sufficient for 10 rows ...
    root2, uni2, spot2 = _hist(other, complete_window=True)
    y = row(CC_.run_competition(data_root=root2, universe_dir=uni2, spot_dir=spot2, mark_interval=MARK_IV, ledger_dir=str(other / "led"), unreserved_exploratory=True), "cross_sectional_funding")
    assert y["dataset_state"]["state"] == "DATA_VALID_BUT_INSUFFICIENT" and any("evaluator's own verdict is INSUFFICIENT_DATA" in r for r in y["dataset_state"]["reasons"])      # ... until the evaluator says not enough
    for r in (x, y):
        assert r["verdict"] == "INSUFFICIENT_DATA"                                         # the state never changes a verdict, level or rank
    rest = [r for r in CC_.run_competition(ledger_dir=str(tmp_path / "led2"))["results"] if r["name"] != "cross_sectional_funding"]
    assert all("dataset_state" not in r for r in rest)


def test_the_ledger_is_persistent_by_default_a_fresh_process_keeps_the_count_and_an_identical_rerun_does_not_inflate_it(tmp_path, monkeypatch):
    monkeypatch.delenv("CANDIDATE_LEDGER_DIR", raising=False)         # the suite isolates the variable for the whole session; this test is about the DEFAULT resolution without it
    _reset()
    a = CC_.run_competition(ledger_dir=str(tmp_path))
    n1 = row(a, "cash_and_carry")["multiple_testing"]["n_variants_counted"]
    assert n1 == len(CR.HOLDS_PRINTS) and os.path.exists(os.path.join(str(tmp_path), "cash_and_carry.jsonl"))
    _reset()                                                                                         # "fresh process"
    b = CC_.run_competition(ledger_dir=str(tmp_path))
    assert row(b, "cash_and_carry")["multiple_testing"]["n_variants_counted"] == n1
    assert CC_.default_ledger_dir("/data/x/md", None) == "/data/x/candidate_ledgers" and CC_.default_ledger_dir(None, None).endswith(os.path.join("ai-agent-control", "candidate_ledgers"))
    assert CC_.default_ledger_dir("/data/x/md", "/l") == "/l"


def test_ranking_tiers_a_promising_row_first_rejected_last_and_ties_by_name():
    mk = lambda v, lvl="NONE", ne=None: {"verdict": v, "readiness_level": lvl, "net_edge": {"value_bps": ne}}           # noqa: E731
    assert CC_._tier(mk("INSUFFICIENT_DATA", "PROMISING", 5.0)) < CC_._tier(mk("INSUFFICIENT_DATA", ne=3.0)) < CC_._tier(mk("INSUFFICIENT_DATA", ne=None)) \
        < CC_._tier(mk("UNTESTED")) < CC_._tier(mk("REJECTED"))
    assert CC_._tier(mk("INSUFFICIENT_DATA", ne=-1.0)) == CC_._tier(mk("INSUFFICIENT_DATA"))                          # a negative measured edge is not rewarded


def test_cli_prints_a_table_and_writes_json(tmp_path, capsys):
    out = str(tmp_path / "o.json")
    assert CC_.main(["--ledger-dir", str(tmp_path / "led"), "--json", out]) == 0
    t = capsys.readouterr().out
    assert t.splitlines()[0].startswith("rank\tcandidate") and "NO WINNER" in t
    assert json.load(open(out))["schema"] == "candidate_competition/v1"


def _mutant(old, new):
    path = os.path.join(BOT, "tools", "candidate_competition.py")
    src = open(path, encoding="utf-8").read()
    assert src.count(old) == 1, old
    mod = types.ModuleType("cc_mut_" + uuid.uuid4().hex)
    mod.__file__ = path
    sys.modules[mod.__name__] = mod
    exec(compile(src.replace(old, new), path, "exec"), mod.__dict__)
    return mod


def test_mutation_a_losing_policy_not_rejected_is_caught(tmp_path, monkeypatch):
    """With the cash series available the cash rule also rejects every nominally losing policy (a nominal loss is a loss to cash), so the NOMINAL rule is exercised where it still stands alone:
    when the cash series is unknown."""
    monkeypatch.setattr(CR.OM, "load_cash_curve", lambda repo: CR.OM.CashCurve([]))
    m = _mutant('        lost_nominal = mean is not None and mean < 0', '        lost_nominal = False')
    econ = CR.btc_carry_economics()
    assert econ["on_capital_after_cash"]["status"] == CR.UNKNOWN
    assert not any(p["verdict"] == "REJECTED" and p["mean_net_bps"] < 0 for p in m.btc_policies(econ)) and any(p["verdict"] == "REJECTED" and p["mean_net_bps"] < 0 for p in CC_.btc_policies(econ))


def test_mutation_dropping_the_cash_rejection_is_caught(tmp_path):
    """A policy positive on notional that loses to cash on the capital it ties up must be REJECTED; the 51-print hold is exactly that case."""
    econ = CR.btc_carry_economics()
    real = {p["policy"]: p for p in CC_.btc_policies(econ)}["unconditional_51_print_hold"]
    assert real["mean_net_bps"] > 0 and real["verdict"] == "REJECTED"
    for old, new in (('        lost_to_cash = (not lost_nominal) and excess is not None and excess < 0', '        lost_to_cash = False'),
                     ('        lost_to_cash = (not lost_nominal) and excess is not None and excess < 0', '        lost_to_cash = (not lost_nominal) and excess is not None and excess < -1e9'),
                     ('    g_cash_lost = (not g_nominal_lost) and g_excess is not None and g_excess < 0', '    g_cash_lost = False')):
        m = _mutant(old, new)
        got = {p["policy"]: p for p in m.btc_policies(econ)}
        assert got["unconditional_51_print_hold"]["verdict"] == "INSUFFICIENT_DATA" or got["production_gated_sequence"]["verdict"] == "INSUFFICIENT_DATA", old + " -> " + new


def test_mutation_a_forced_winner_is_caught(tmp_path):
    m = _mutant('"winner": None if all_failed else promising[0]["name"]', '"winner": ranked[0]["name"]')
    _reset()
    assert m.run_competition(ledger_dir=str(tmp_path))["winner"] is not None
    _reset()
    assert CC_.run_competition(ledger_dir=str(tmp_path / "b"))["winner"] is None


def test_mutation_research_only_candidate_ranked_is_caught(tmp_path):
    m = _mutant('ranked = sorted((x for x in rows if x["origin"] != "RESEARCH_ONLY")', 'ranked = sorted((x for x in rows)')
    _reset()
    assert [r for r in m.run_competition(ledger_dir=str(tmp_path))["results"] if r["name"] == "cross_venue_funding"][0]["rank"] is not None


def test_mutation_inflating_rerun_is_caught(tmp_path):
    m = _mutant('        if rets and RH.series_digest(rets) not in led.returns_digests(name):', '        if rets:')
    _reset()
    m.run_competition(ledger_dir=str(tmp_path))
    _reset()
    assert m.run_competition(ledger_dir=str(tmp_path))["results"] and \
        [r for r in m.run_competition(ledger_dir=str(tmp_path))["results"] if r["name"] == "cash_and_carry"][0]["multiple_testing"]["n_variants_counted"] > len(CR.HOLDS_PRINTS)


def test_without_a_cash_series_nothing_is_rejected_for_cash_and_the_gap_is_stated(tmp_path, monkeypatch):
    """An unknown cost of capital is not zero and is not a rejection: the old nominal reading stays, labelled CASH_UNKNOWN."""
    _reset()
    monkeypatch.setattr(CR.OM, "load_cash_curve", lambda repo: CR.OM.CashCurve([]))
    rep = CC_.run_competition(ledger_dir=str(tmp_path))
    b = row(rep, "cash_and_carry")
    p51 = {p["policy"]: p for p in b["policies"]}["unconditional_51_print_hold"]
    assert p51["verdict"] == "INSUFFICIENT_DATA" and p51["mean_excess_over_cash_bps_of_capital"] is None and "CASH_UNKNOWN" in p51["note"]
    assert b["verdict"] == "INSUFFICIENT_DATA" and "CASH_UNKNOWN" in b["net_edge"]["basis"] and "nominal_net_bps_zero_capital_cost" not in b
    econ = CR.btc_carry_economics()
    assert econ["on_capital_after_cash"]["status"] == CR.UNKNOWN and "CASH_SERIES_UNAVAILABLE" in econ["on_capital_after_cash"]["reason"]
    assert any("CASH_UNKNOWN" in r for r in CR._carry_verdict(econ)[1])


def test_the_readiness_and_competition_verdicts_agree_on_the_btc_carry_candidates(committed):
    """One rule, two tools: a candidate every policy of which loses after costs or to cash is REJECTED in both; they may never disagree on the same evidence."""
    rep, _ = committed
    base = CR.build_report()
    comp = row(rep, "cash_and_carry")["verdict"]
    assert comp == "REJECTED"
    for n in ("cash_and_carry", "btcusdt_funding_carry_overlay"):
        r = next(x for x in base["candidates"] if x["name"] == n)
        assert r["readiness_verdict"] == comp, (n, r["readiness_verdict"], r["readiness_reasons"][-3:])


def test_a_short_cash_series_does_not_crash_the_verdict_and_is_stated_unknown(monkeypatch):
    monkeypatch.setattr(CR.OM, "load_cash_curve", lambda repo: CR.OM.CashCurve([(1_659_369_600_000 + k * 3_600_000, 0.04) for k in range(24 * 5)]))      # five days of cash rate
    econ = CR.btc_carry_economics()
    assert econ["on_capital_after_cash"]["status"] == "COMPUTED"
    assert econ["on_capital_after_cash"]["classes"][CR.CAP_OVERLAY]["holds"]["51"]["mean_excess_over_cash_bps_of_capital"] is None
    verdict, reasons, policies = CR._carry_verdict(econ)                                         # used to raise TypeError formatting None
    assert verdict == "INSUFFICIENT_DATA" and any("CASH_UNKNOWN" in x for x in reasons) and policies["long_hold_vs_cash"] == CR.UNKNOWN
    p51 = {p["policy"]: p for p in CC_.btc_policies(econ)}["unconditional_51_print_hold"]
    assert p51["verdict"] == "INSUFFICIENT_DATA" and "CASH_UNKNOWN" in p51["note"]


# ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------
# ROUND 10, INDEPENDENT RED TEAM, finding 3: the ROW's basis label must be the one the summary computed
# ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------
def _stub_xs(monkeypatch, nominal, blocks):
    fam = {"verdict": "INSUFFICIENT_DATA", "status": "WALK_FORWARD_DONE", "is_carry": False, "independent_holding_rows": 80, "oos_mean_total_bps_per_row": nominal, "vs_cash": blocks,
           "survivorship": "SURVIVORS_ONLY", "n_variants_counted": 5, "n_variants_used_for_nominal_p": 3, "reason": "x"}
    res = {"status": "EVALUATED", "evaluation": {"families": {"PERP_FUNDING_SPREAD": fam}, "survivorship": "SURVIVORS_ONLY", "input": {"holdout": {"status": "RESERVED"}}}}
    monkeypatch.setattr(CC_, "run_cross_sectional", lambda *a, **k: res)


def _xs_row(tmp_path):
    _reset()
    rep = CC_.run_competition(ledger_dir=str(tmp_path))
    return row(rep, "cross_sectional_funding")


def test_the_cross_sectional_row_says_excess_over_cash_in_bps_of_capital_when_a_cash_source_was_applied(tmp_path, monkeypatch):
    blk = lambda x: {"verdict": "INSUFFICIENT_DATA", "status": "WALK_FORWARD_DONE", "oos_mean_excess_bps_of_capital_per_row": x, "capital_per_gross_notional": 1.02}      # noqa: E731
    _stub_xs(monkeypatch, 30.0, {"HISTORICAL": blk(-2.0)})
    r = _xs_row(tmp_path)
    assert r["net_edge"]["value_bps"] == -2.0
    assert "EXCESS_OVER_CASH_BPS_OF_CAPITAL" in r["net_edge"]["basis"] and "NOMINAL" not in r["net_edge"]["basis"], r["net_edge"]["basis"]
    assert "HISTORICAL" in r["net_edge"]["basis"], "which cash source the figure is against"


def test_the_cross_sectional_row_says_nominal_and_not_a_comparison_with_cash_when_none_was_applied(tmp_path, monkeypatch):
    _stub_xs(monkeypatch, 30.0, {})
    r = _xs_row(tmp_path)
    assert r["net_edge"]["value_bps"] == 30.0
    assert "NOMINAL_BPS_OF_GROSS_NOTIONAL" in r["net_edge"]["basis"] and "NOT a comparison with cash" in r["net_edge"]["basis"] and "EXCESS_OVER_CASH" not in r["net_edge"]["basis"], r["net_edge"]["basis"]


def test_the_family_row_carries_both_variant_counts_so_a_p_value_is_never_read_against_the_wrong_one(tmp_path, monkeypatch):
    blk = lambda x: {"verdict": "INSUFFICIENT_DATA", "status": "WALK_FORWARD_DONE", "oos_mean_excess_bps_of_capital_per_row": x, "capital_per_gross_notional": 1.02}      # noqa: E731
    _stub_xs(monkeypatch, 30.0, {"HISTORICAL": blk(-2.0)})
    f = _xs_row(tmp_path)["families"]["PERP_FUNDING_SPREAD"]
    assert f["n_variants_counted"] == 5 and f["n_variants_used_for_nominal_p"] == 3


# ------------------------------------------------------------------ a family whose trials are already counted has been TRIED (Round 17)
def test_a_family_whose_trials_are_already_counted_is_not_untested_and_is_not_a_next_hypothesis(tmp_path):
    """The persistent ledger counted reg-1 and reg-2 for the regime family, yet the table said 'no test has been run' (UNTESTED) and listed it among the next
    hypotheses: a third trial on data that was already read widens the multiple-testing family."""
    _reset()
    fam = "funding_regime_conditioned_entry"
    led = RH.VariantLedger(fam, path=os.path.join(str(tmp_path), RH.normalise_family(fam) + ".jsonl"))
    led.record("reg-1", {"k": 1}, [0.1, -0.2, 0.3, 0.05], horizon=1)
    led.record("reg-2", {"k": 2}, [0.2, 0.1, -0.1, 0.0], horizon=1)
    _reset()                                                                                             # a fresh process reloads the count from the file
    rep = CC_.run_competition(ledger_dir=str(tmp_path))
    r = row(rep, fam)
    assert r["multiple_testing"]["n_variants_counted"] == 2
    assert r["verdict"] == "INSUFFICIENT_DATA" and r["highest_supportable_verdict"] == "INSUFFICIENT_DATA"
    assert any("TRIALS_ALREADY_COUNTED" in x and "2 variant" in x for x in r["rejection_reasons"])
    assert not any("no test has been run" in x for x in r["rejection_reasons"])
    assert fam not in {h["name"] for h in rep["next_hypotheses"]}
    # an untouched sibling is still genuinely untested, and still a next hypothesis
    assert row(rep, "cross_interval_funding_dispersion")["verdict"] == "UNTESTED"
    assert "cross_interval_funding_dispersion" in {h["name"] for h in rep["next_hypotheses"]}
    assert rep["winner"] is None and rep["all_failed"] is True


def test_a_family_with_no_trials_is_unchanged(tmp_path):
    _reset()
    rep = CC_.run_competition(ledger_dir=str(tmp_path))
    r = row(rep, "funding_regime_conditioned_entry")
    assert r["multiple_testing"]["n_variants_counted"] == 0 and r["verdict"] == "UNTESTED"
    assert any("no test has been run" in x for x in r["rejection_reasons"])


def test_the_process_wide_high_water_mark_does_not_make_a_family_tried(tmp_path):
    """PR #263's CI: `n_variants` is never below the family's PROCESS-WIDE high-water mark, so after an earlier test in the same process recorded variants for a family, a fresh ledger
    directory still 'counted' them and a refused row flipped to INSUFFICIENT_DATA. 'Already tried' is a statement about the ledger FILE."""
    _reset()
    fam = "funding_regime_conditioned_entry"
    mem = RH.VariantLedger(fam)                                          # in memory: raises the family's high-water mark, writes no file
    mem.record("reg-1", {"k": 1}, [0.1, -0.2, 0.3, 0.05], horizon=1)
    mem.record("reg-2", {"k": 2}, [0.2, 0.1, -0.1, 0.0], horizon=1)
    assert RH.VariantLedger(fam).n_variants >= 2 and RH.VariantLedger(fam).persisted_variants == 0
    rep = CC_.run_competition(ledger_dir=str(tmp_path))                  # NO _reset(): the high-water mark is still raised
    r = row(rep, fam)
    assert r["verdict"] == "UNTESTED" and any("no test has been run" in x for x in r["rejection_reasons"])
    assert fam in {h["name"] for h in rep["next_hypotheses"]}
    _reset()


# ------------------------------------------------------------------------------------------------ Round 20: a noise row never heads the ranked table, and NaN never reaches it

def _fam(*, status="WALK_FORWARD_DONE", p=0.95, nobs=3, oos=12.0, vs=None, verdict="INSUFFICIENT_DATA"):
    return {"verdict": verdict, "status": status, "is_carry": False, "independent_holding_rows": 5, "oos_mean_total_bps_per_row": oos, "p_adjusted": p, "oos_independent_observations": nobs,
            "n_variants_counted": 4, "n_variants_used_for_nominal_p": 4, "survivorship": "SURVIVORS_ONLY", "vs_cash": vs or {}}


def _xs(families):
    return CC_._xs_summary({"status": "EVALUATED", "evaluation": {"families": families, "survivorship": "SURVIVORS_ONLY", "history_coverage": {}, "input": {}}})


def _block(x, status="WALK_FORWARD_DONE"):
    return {"verdict": "INSUFFICIENT_DATA", "status": status, "oos_mean_excess_bps_of_capital_per_row": x}


def test_a_positive_but_insignificant_best_of_k_mean_is_descriptive_not_the_positive_tier():
    """REPRODUCED (Round 20, specialist E): +40 bps with Holm p 0.95 on 3 independent observations became the row's net edge and ranked in the EVALUATED_POSITIVE tier, above every INSUFFICIENT_DATA row."""
    weak = _xs({"A": _fam(vs={"okx": _block(40.0)})})
    assert weak["net_edge"] == 40.0 and weak["net_edge_significant"] is False
    row = {"verdict": "INSUFFICIENT_DATA", "readiness_level": "RESEARCH_READY", "net_edge": {"value_bps": 40.0, "significant": False}}
    assert CC_._tier(row) == CC_.VERDICT_TIER["INSUFFICIENT_DATA"]
    strong = _xs({"A": _fam(p=0.01, nobs=CR.MIN_PROMISING_OOS_OBS, vs={"okx": _block(40.0)})})
    assert strong["net_edge_significant"] is True
    assert CC_._tier(dict(row, net_edge={"value_bps": 40.0, "significant": True})) == CC_.VERDICT_TIER["EVALUATED_POSITIVE"]
    assert CC_._tier(dict(row, net_edge={"value_bps": 40.0, "significant": None})) == CC_.VERDICT_TIER["EVALUATED_POSITIVE"], "rows with no assessment keep their prior behaviour"


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), True, "7", None])
def test_a_non_real_number_never_enters_the_headline_or_the_best_of(bad):
    s = _xs({"A": _fam(oos=bad, vs={"okx": _block(bad)}), "B": _fam(oos=3.0, vs={"okx": _block(5.0)})})
    assert s["net_edge"] == 5.0, "the real figure is used whatever order the families come in"
    s2 = _xs({"B": _fam(oos=3.0, vs={"okx": _block(5.0)}), "A": _fam(oos=bad, vs={"okx": _block(bad)})})
    assert s2["net_edge"] == 5.0
    only = _xs({"A": _fam(oos=bad, vs={"okx": _block(bad)})})
    assert only["net_edge"] is None, "nothing real to report is None, never NaN or a bool"


def test_a_block_that_did_not_finish_its_walk_forward_cannot_set_the_headline():
    s = _xs({"A": _fam(vs={"okx": _block(99.0, status="TOO_FEW_HOLDING_ROWS")})})
    assert s["net_edge"] != 99.0


def test_real_number_is_finite_numeric_and_not_a_bool():
    assert CC_._real_number(1) and CC_._real_number(-2.5) and not any(CC_._real_number(v) for v in (True, False, float("nan"), float("inf"), "1", None, [], {}))
