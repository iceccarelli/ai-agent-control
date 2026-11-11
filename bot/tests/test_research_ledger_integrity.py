"""The multiple-testing family count is a number the verdict is adjusted by, so it must mean what it says.

Three silent wrong answers on the cross-sectional path, each reproduced on the repo's own fixtures before it was fixed:

* an IDENTICAL re-run of ``evaluate_ranked_funding`` on identical data added its variants to the ledger again (+4 per run, in the short-history path and in the tested
  path), although the documented contract is "an identical re-run is the same trial; new data or a new variant enlarges the family". ``evaluate_family`` and
  ``regime_entry_eval`` already honoured it; the top-level ranked-funding path did not. The inflation only ever made a later test harder to pass, but a count that grows with
  how often someone re-ran a command is not a count of hypotheses tried;
* a run with no usable spot snapshot recorded six rejected SPOT_PERP_CARRY trials for a family that could not have been evaluated at all: the family has no universe without a
  hedgeability fact for any symbol, so nothing was tried and nothing should be counted.

Nothing here claims an edge: every series is planted by the test.
"""
from __future__ import annotations

import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
BOT = os.path.dirname(HERE)
for _p in (HERE, BOT, os.path.join(BOT, "tools")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import candidate_readiness as CR  # noqa: E402
import research_harness as RH  # noqa: E402
from test_candidate_readiness import MARK_IV, _hist, offline, sx  # noqa: E402,F401  (fixtures and builders the readiness tests already own)


def _lines(path):
    return sum(1 for _ in open(path, encoding="utf-8")) if os.path.exists(path) else 0


def _long_world(n_prints, *, seed=0):
    """Four 8-hour symbols with enough prints for the 60 x 7-day grid: 457 days = 1371 prints plus slack. Rates and paths are deterministic functions of (symbol, i)."""
    out = []
    for k, sym in enumerate(("AAAUSDT", "BBBUSDT", "CCCUSDT", "DDDUSDT")):
        rates = [((i * 7 + k * 13 + seed) % 11 - 5) / 3.0 for i in range(n_prints)]
        out.append(sx(sym, rates, lambda i, k=k: 100.0 + ((i * 3 + k) % 17) * 0.05))
    return out


# ------------------------------------------------------------------ F7: an identical re-run is the same trial
def test_an_identical_rerun_on_a_short_history_does_not_enlarge_the_family(tmp_path, offline):
    root, uni, spot = _hist(tmp_path)
    led = str(tmp_path / "x.jsonl")
    counts = []
    for _ in range(3):
        out = CR.evaluate_cross_sectional(root, universe_dir=uni, ledger_path=led, spot_dir=spot, mark_interval=MARK_IV)
        counts.append((out["n_variants_counted"], _lines(led)))
    assert out["status"] == "TOO_FEW_HOLDING_ROWS"
    assert counts[0] == counts[1] == counts[2], f"an identical re-run changed the family count: {counts}"


def test_an_identical_rerun_on_a_testable_history_does_not_enlarge_the_family(tmp_path):
    series = _long_world(1500)
    led = str(tmp_path / "t.jsonl")
    counts = []
    for _ in range(3):
        out = CR.evaluate_ranked_funding(series, ledger_path=led)
        counts.append((out["n_variants_counted"], _lines(led)))
    assert out["status"] == "WALK_FORWARD_DONE", out.get("status")
    assert counts[0] == counts[1] == counts[2], f"an identical re-run changed the family count: {counts}"
    assert counts[0][0] > 0


def test_new_data_or_a_new_variant_still_enlarges_the_family(tmp_path):
    led = str(tmp_path / "t.jsonl")
    first = CR.evaluate_ranked_funding(_long_world(1500), ledger_path=led)["n_variants_counted"]
    grown = CR.evaluate_ranked_funding(_long_world(1560), ledger_path=led)["n_variants_counted"]
    assert grown > first, "a longer capture is new evidence about the same variants and must be a new trial"
    wider = CR.evaluate_ranked_funding(_long_world(1560), ledger_path=led, formation_days_grid=(3, 7, 14, 30, 45))["n_variants_counted"]
    assert wider > grown, "a new formation window is a new variant and must enlarge the family"
    again = CR.evaluate_ranked_funding(_long_world(1560), ledger_path=led, formation_days_grid=(3, 7, 14, 30, 45))["n_variants_counted"]
    assert again == wider


def test_the_count_a_run_reports_is_what_the_verdict_was_adjusted_by(tmp_path):
    led = str(tmp_path / "t.jsonl")
    series = _long_world(1500)
    a = CR.evaluate_ranked_funding(series, ledger_path=led)
    b = CR.evaluate_ranked_funding(series, ledger_path=led)
    assert (a["n_variants_counted"], a["p_adjusted"], a["verdict"]) == (b["n_variants_counted"], b["p_adjusted"], b["verdict"])
    assert RH.VariantLedger("cross_sectional_funding", path=led).n_variants == a["n_variants_counted"]


# ------------------------------------------------------------------ F8: a family with no hedgeability fact for any symbol was not tried
def test_the_carry_family_without_any_hedgeability_fact_is_not_evaluated_and_records_nothing(tmp_path, offline):
    root, uni, _spot = _hist(tmp_path, spot=False)
    series, info = CR.load_history_root(root, universe_dir=uni, mark_interval=MARK_IV)
    assert all(s.spot_listed is None for s in series)
    led = str(tmp_path / "carry.jsonl")
    out = CR.evaluate_family(series, "SPOT_PERP_CARRY", ledger_path=led, universe_info=info)
    assert out["status"] == "HEDGEABILITY_UNKNOWN_FOR_EVERY_SYMBOL" and out["verdict"] == "UNTESTED"
    assert "--spot-dir" in out["reason"] and "nothing was tried" in out["reason"]
    assert _lines(led) == 0, "a family that could not run must not enlarge the multiple-testing count"
    assert "n_variants_counted" not in out


def test_the_other_two_families_do_not_need_a_spot_snapshot_and_are_still_counted(tmp_path, offline):
    root, uni, _ = _hist(tmp_path, spot=False)
    led = str(tmp_path / "x.jsonl")
    out = CR.evaluate_cross_sectional(root, universe_dir=uni, ledger_path=led, mark_interval=MARK_IV)
    fams = out["families"]
    assert fams["SPOT_PERP_CARRY"]["status"] == "HEDGEABILITY_UNKNOWN_FOR_EVERY_SYMBOL"
    assert fams["PERP_FUNDING_SPREAD"]["status"] == "TOO_FEW_HOLDING_ROWS" and fams["PERP_FUNDING_SPREAD"]["n_variants_counted"] > 0
    assert fams["PERP_DIRECTIONAL_FUNDING"]["status"] == "TOO_FEW_HOLDING_ROWS"
    assert not os.path.exists(str(tmp_path / "x.spot_perp_carry.jsonl")) or _lines(str(tmp_path / "x.spot_perp_carry.jsonl")) == 0


def test_a_snapshot_that_resolves_some_symbols_is_not_the_no_information_case(tmp_path, offline):
    root, uni, spot = _hist(tmp_path)
    series, info = CR.load_history_root(root, universe_dir=uni, spot_dir=spot, mark_interval=MARK_IV)
    assert any(s.spot_listed is not None for s in series)
    out = CR.evaluate_family(series, "SPOT_PERP_CARRY", ledger_path=str(tmp_path / "carry.jsonl"), universe_info=info)
    assert out["status"] != "HEDGEABILITY_UNKNOWN_FOR_EVERY_SYMBOL"


def test_the_perp_only_families_never_need_hedgeability(tmp_path):
    series = _long_world(1500)
    assert all(s.spot_listed is None for s in series)
    out = CR.evaluate_family(series, "PERP_FUNDING_SPREAD", ledger_path=str(tmp_path / "s.jsonl"))
    assert out["status"] != "HEDGEABILITY_UNKNOWN_FOR_EVERY_SYMBOL"


@pytest.mark.parametrize("kind", ["SPOT_PERP_CARRY"])
def test_a_later_run_with_the_snapshot_is_not_poisoned_by_the_forgotten_one(tmp_path, offline, kind):
    root, uni, spot = _hist(tmp_path)
    led = str(tmp_path / "carry.jsonl")
    s0, i0 = CR.load_history_root(root, universe_dir=uni, mark_interval=MARK_IV)               # forgot --spot-dir
    CR.evaluate_family(s0, kind, ledger_path=led, universe_info=i0)
    assert _lines(led) == 0
    s1, i1 = CR.load_history_root(root, universe_dir=uni, spot_dir=spot, mark_interval=MARK_IV)
    real = CR.evaluate_family(s1, kind, ledger_path=led, universe_info=i1)
    fresh = CR.evaluate_family(s1, kind, ledger_path=str(tmp_path / "fresh.jsonl"), universe_info=i1)
    assert real["n_variants_counted"] == fresh["n_variants_counted"], "the forgotten run left junk in the family count"
