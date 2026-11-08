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
    assert pol["unconditional_51_print_hold"]["mean_net_bps"] == pytest.approx(20.2, abs=0.05) and pol["unconditional_51_print_hold"]["verdict"] == "INSUFFICIENT_DATA"
    assert pol["unconditional_51_print_hold"]["latest_year_mean_net_bps"] < 0                       # the regime that pays is not the current one
    assert pol["production_gated_sequence"]["trailing_365d_mean_net_bps"] == pytest.approx(-4.16, abs=0.05)
    assert b["verdict"] == "INSUFFICIENT_DATA" and b["readiness_level"] == "NONE"
    assert any("REJECTED policy" in r for r in b["rejection_reasons"])


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
    rep = CC_.run_competition(data_root=root, universe_dir=uni, spot_dir=spot, mark_interval=MARK_IV, ledger_dir=ld)
    x = row(rep, "cross_sectional_funding")
    assert set(x["families"]) == set(CR.FAMILIES) and x["verdict"] == "INSUFFICIENT_DATA" and x["readiness_level"] in ("NONE", "DATA_READY")
    assert x["families"]["PERP_FUNDING_SPREAD"]["is_carry"] is False and x["families"]["SPOT_PERP_CARRY"]["is_carry"] is True
    assert any("TOO_FEW_HOLDING_ROWS" in r and "457 days" in r for r in x["rejection_reasons"])
    assert x["multiple_testing"]["scope"] == "PERSISTENT_LEDGER_FILE" and x["multiple_testing"]["n_variants_counted"] > 0
    for k in CR.FAMILIES:
        assert os.path.exists(os.path.join(ld, f"cross_sectional_funding.{k.lower()}.jsonl"))
    assert not os.path.exists(os.path.join(root, "candidate_ledgers"))                                # evidence directories stay byte-for-byte what the verifier hashed
    assert rep["winner"] is None


def test_the_ledger_is_persistent_by_default_a_fresh_process_keeps_the_count_and_an_identical_rerun_does_not_inflate_it(tmp_path):
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


def test_mutation_a_losing_policy_not_rejected_is_caught(tmp_path):
    m = _mutant('        rejected = mean is not None and mean < 0', '        rejected = False')
    econ = CR.btc_carry_economics()
    assert not any(p["verdict"] == "REJECTED" for p in m.btc_policies(econ)) and any(p["verdict"] == "REJECTED" for p in CC_.btc_policies(econ))


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
