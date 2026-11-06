"""tools/carry_prospective.py: the frozen prospective evaluation of BTC funding carry.

Everything here is synthetic. The expected required-funding values are written out from the closed form in the test, not read back from the module.
"""
from __future__ import annotations

import ast
import json
import math
import os
import random
import sys

import pytest

BOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BOT, "tools"))
sys.path.insert(0, BOT)

import account_review as AR  # noqa: E402
import carry_prospective as CP  # noqa: E402
import statistical_power as SP  # noqa: E402

H8 = CP.SETTLE_MS


def window_prints(mean_bps, *, n=None, seed=1, drop=(), sd=0.4, rho=0.8):
    """Settled prints for the registered window: an AR(1) series around ``mean_bps`` with the given dependence, on the 8h grid."""
    n = n if n is not None else CP.WINDOW_DAYS * CP.PER_DAY
    rng = random.Random(seed)
    x, out = 0.0, []
    for i in range(n):
        x = rho * x + rng.gauss(0.0, sd * math.sqrt(1 - rho * rho))
        if i not in set(drop):
            out.append((CP.WINDOW_START_MS + i * H8, mean_bps + x))
    return out


def test_the_required_funding_is_the_closed_form_for_each_provisional_hurdle():
    # (cost + hurdle x (1 + reserve) x hold / 365 x 1e4) / (3 x hold), written out here
    for hurdle, expected in ((10.0, 1.1312328767), (15.0, 1.6335159817), (20.0, 2.1357990868)):
        by_hand = (11.4 + hurdle / 100.0 * 1.10 * 30.0 / 365.0 * 1e4) / (3 * 30.0)
        assert CP.required_bps(hurdle) == pytest.approx(by_hand, abs=1e-12) == pytest.approx(expected, abs=1e-9)


def test_each_plan_is_pinned_and_a_changed_parameter_cost_or_window_is_a_different_plan(monkeypatch):
    for h in CP.HURDLES_PCT:
        assert SP.plan_digest(CP.registered_plan(h)) == CP.PINNED_DIGESTS[h]
    assert len(set(CP.PINNED_DIGESTS.values())) == 3
    with pytest.raises(SP.PlanError):
        CP.plan_for(7.0)                                           # a hurdle nobody declared
    monkeypatch.setattr(CP, "ROUND_TRIP_BPS", 8.0)                 # "the owner's fee is lower, so retune the cost": a different plan, refused
    with pytest.raises(SP.PlanDigestMismatch):
        CP.registered_plan(10.0)
    monkeypatch.setattr(CP, "ROUND_TRIP_BPS", 11.4)
    monkeypatch.setattr(CP, "WINDOW_START_MS", CP.WINDOW_START_MS + H8)       # "start after seeing the first prints": a different window, refused
    with pytest.raises(SP.PlanDigestMismatch):
        CP.registered_plan(10.0)


def test_the_window_opens_after_the_plan_was_written_and_after_all_the_historical_data():
    last_historical_settlement = 1_788_940_800_000        # 2026-09-09T08:00Z, the end of the committed public history
    assert CP.WINDOW_START_MS > last_historical_settlement and CP.WINDOW_START_MS % H8 == 0
    assert CP.WINDOW_END_MS - CP.WINDOW_START_MS == 365 * 86_400_000


def test_historical_data_cannot_be_fed_in_even_mixed_with_valid_future_data():
    prints = [(CP.WINDOW_START_MS - H8, 5.0)] + window_prints(2.0)
    with pytest.raises(SP.WindowViolation):
        CP.evaluate(prints, 10.0, SP.TrialLedger())
    ledger = SP.TrialLedger()
    with pytest.raises(SP.PlanError):
        CP.evaluate([(CP.WINDOW_START_MS - 10 * H8 + i * H8, 5.0) for i in range(100)], 10.0, ledger)      # entirely historical
    assert ledger.n_trials == 0                                     # a refusal records nothing


def test_a_rich_funding_regime_passes_a_poor_one_does_not_and_neither_authorises_anything():
    ledger = SP.TrialLedger()
    rich = CP.evaluate(window_prints(2.5, seed=3), 10.0, ledger)          # mean 2.5 bps: 1.37 above the 1.131 required
    assert rich["verdict"] == "EDGE_SUPPORTED_AFTER_ADJUSTMENT", rich
    poor = CP.evaluate(window_prints(0.26, seed=4), 10.0, ledger)          # the last year's level: far below it
    assert poor["verdict"] in ("NO_EDGE_DETECTED", "INSUFFICIENT_POWER") and poor["verdict"] != "EDGE_SUPPORTED_AFTER_ADJUSTMENT"
    assert ledger.n_trials == 2                                             # both looks are counted, winners and losers
    for r in (rich, poor):
        assert not any(k in r for k in ("authorizes_trading", "allocate", "capital")), r.keys()
    assert poor["z_observed"] < -5.0 and poor["p_value"] == pytest.approx(1.0)       # a mean far below the requirement reports a very negative z; it used to crash


def test_a_small_gap_is_reported_not_filled_and_a_large_one_refuses_the_evaluation():
    ok = window_prints(2.5, seed=5, drop=range(10, 60))                     # 50 of 1095 missing: 95.4 % coverage
    cov = CP.coverage(ok, CP.WINDOW_END_MS)
    assert cov["expected"] == 1095 and cov["missing"] == 50 and cov["observed"] == 1045 and cov["coverage"] == pytest.approx(1045 / 1095)
    assert CP.evaluate(ok, 10.0, SP.TrialLedger())["available_raw_n"] == 1045         # evaluated on what exists; nothing interpolated
    bad = window_prints(2.5, seed=5, drop=range(10, 200))                   # 190 missing: 82.6 %
    ledger = SP.TrialLedger()
    with pytest.raises(SP.DataIntegrityError, match="coverage"):
        CP.evaluate(bad, 10.0, ledger)
    assert ledger.n_trials == 0


@pytest.mark.parametrize("bad", [[(CP.WINDOW_START_MS + 5, 1.0)], [(CP.WINDOW_START_MS, float("nan"))], [(True, 1.0)], [(CP.WINDOW_START_MS, True)], [(1.5e12 + 0.5, 1.0)]])
def test_prints_off_the_grid_or_malformed_are_refused_never_snapped(bad):
    with pytest.raises(SP.DataIntegrityError):
        CP.observations(bad, 10.0)


def test_before_the_window_opens_nothing_of_the_evaluation_exists_and_the_requirement_is_stated():
    rep = CP.progress([], CP.WINDOW_START_MS - 1, 10.0)
    assert rep["state"] == "WINDOW_NOT_OPEN" and rep["coverage"]["expected"] == 0 and rep["coverage"]["observed"] == 0
    assert isinstance(rep["needed_settlements_for_min_edge"], int) and rep["needed_settlements_for_min_edge"] > 0
    assert rep["still_required"] == rep["needed_settlements_for_min_edge"]
    assert rep["dependence_inputs"].startswith("ASSUMED")
    assert rep["plan_digest"] == CP.PINNED_DIGESTS[10.0] and rep["required_funding_bps"] == pytest.approx(1.1312328767, abs=1e-9)


def test_progress_switches_to_the_windows_own_dependence_once_enough_settlements_exist_and_never_evaluates():
    prints = window_prints(2.0, n=200, seed=7)
    now = CP.WINDOW_START_MS + 200 * H8
    rep = CP.progress(prints, now, 10.0)
    assert rep["state"] == "WINDOW_OPEN" and rep["coverage"]["observed"] == 200 and rep["coverage"]["missing"] == 0
    assert rep["dependence_inputs"].startswith("OBSERVED")
    assert rep["still_required"] == max(0, rep["needed_settlements_for_min_edge"] - 200)
    assert "p_value" not in rep and "verdict" not in rep                      # progress is not a look at the result
    ledger = SP.TrialLedger()
    CP.progress(prints, now, 10.0)
    assert ledger.n_trials == 0


def test_a_one_year_window_may_be_too_short_for_the_declared_min_edge_and_says_so():
    rep = CP.progress([], CP.WINDOW_START_MS - 1, 10.0)
    assert rep["needed_exceeds_window"] is (rep["needed_settlements_for_min_edge"] > CP.WINDOW_DAYS * CP.PER_DAY)
    assert "INSUFFICIENT_POWER" in rep["reading"]


def test_the_command_line_reads_one_local_file_prints_text_and_json_and_refuses_protected_names(tmp_path, capsys):
    f = tmp_path / "prints.jsonl"
    f.write_text("\n".join(json.dumps({"ts_ms": ts, "funding_bps": v}) for ts, v in window_prints(1.0, n=30)) + "\n")
    assert CP.main(["--funding", str(f), "--now-ms", str(CP.WINDOW_START_MS + 30 * H8)]) == 0
    assert "PROSPECTIVE EVALUATION" in capsys.readouterr().out
    assert CP.main(["--funding", str(f), "--json", "--now-ms", str(CP.WINDOW_START_MS + 30 * H8), "--hurdle-pct", "15"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["hurdle_pct"] == 15.0 and doc["coverage"]["observed"] == 30
    assert CP.main(["--funding", str(tmp_path / "trading_state.db.jsonl")]) == 2
    assert "carry_prospective:" in capsys.readouterr().err
    f2 = tmp_path / "bad.jsonl"
    f2.write_text(json.dumps({"ts_ms": 1}) + "\n")
    assert CP.main(["--funding", str(f2)]) == 2


def test_the_module_reads_one_file_and_imports_nothing_that_reaches_the_network_a_database_or_a_process():
    tree = ast.parse(open(CP.__file__, encoding="utf-8").read())
    imported = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            imported |= {a.name.split(".")[0] for a in n.names}
        elif isinstance(n, ast.ImportFrom) and n.module:
            imported.add(n.module.split(".")[0])
    assert imported <= {"__future__", "argparse", "datetime", "json", "math", "os", "sys", "time", "typing", "account_review", "carry_teardown",
                        "statistical_power", "evidence_stats"}, imported
    calls = {n.func.id for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert not calls & {"open", "exec", "eval", "compile", "__import__"}
