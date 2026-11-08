"""The hypothesis registry is tamper-evident, cannot be deleted into silence, and records experiments before they run.
Reproduced before the fix: cutting 37 trials to 1 raised nothing; a missing file loaded as an empty registry (no multiple-testing penalty); a negative rule
count shrank the working search width."""
from __future__ import annotations

import copy
import json
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
BOT = os.path.dirname(HERE)
sys.path.insert(0, BOT)
sys.path.insert(0, os.path.join(BOT, "tools"))

import hypothesis_registry as hr  # noqa: E402

#: sha256 over (trial_id, entry_digest) of the 37 trials that existed when the seal was introduced. Editing any of them changes this and fails CI.
LEGACY_SEAL = "4f657f0aad5dca0aa130b8f514d6831ee7b44acaeae3c7f1d7c40811dcfb596f"
PROSPECTIVE = "btcusdt_funding_carry_overlay@prospective-1"


def committed():
    return hr.load(hr.DEFAULT_PATH)


def legacy_ids(reg):
    return [t["trial_id"] for t in reg["trials"] if t["trial_id"] != PROSPECTIVE]


REG = dict(hypothesis_id="h1", mechanism="m", family="f", strategy="s", universe=["BTCUSDT"], input_data_sha256={"a": "00"}, code_sha="abc",
           data_window={"a": 1}, parameter_grid={"x": [1]}, costs="c", statistical_design="d", train_test="t", multiple_testing_family="mt",
           prospective_period="none")


def test_the_committed_legacy_trials_are_exactly_the_sealed_ones():
    reg = committed()
    assert len(legacy_ids(reg)) == 37
    assert hr.legacy_seal(reg, legacy_ids(reg)) == LEGACY_SEAL, "a legacy trial was edited or removed"


def test_the_committed_registry_verifies_and_links_the_frozen_plan():
    reg = committed()
    assert hr.verify(reg, require_ids=legacy_ids(reg) + [PROSPECTIVE]) == []
    import carry_prospective as CP
    t = next(t for t in reg["trials"] if t["trial_id"] == PROSPECTIVE)
    assert t["input_data_sha256"]["registered_plan_digests_by_hurdle_pct"] == {f"{h:g}": CP.PINNED_DIGESTS[h] for h in CP.HURDLES_PCT}
    assert t["data_window"]["start_ms"] == CP.WINDOW_START_MS and t["data_window"]["end_ms"] == CP.WINDOW_END_MS


def test_removing_a_trial_is_detected_and_so_is_altering_one():
    reg = committed()
    cut = copy.deepcopy(reg); cut["trials"] = cut["trials"][:1]
    assert any("REMOVED" in p for p in hr.verify(cut))
    alt = copy.deepcopy(reg); alt["trials"][3]["status"] = "cleared_stage1"
    assert any("ALTERED" in p for p in hr.verify(alt))
    assert hr.legacy_seal(alt, legacy_ids(alt)) != LEGACY_SEAL


def test_an_unsealed_registry_and_an_unsealed_addition_are_reported():
    reg = copy.deepcopy(committed()); reg.pop("integrity")
    assert any("not sealed" in p for p in hr.verify(reg))
    reg = copy.deepcopy(committed()); hr.record(reg, "sneaky", status="tested")
    assert any("not sealed" in p for p in hr.verify(reg))
    hr.seal(reg)
    assert hr.verify(reg) == []


def test_supersession_and_evidence_do_not_break_the_seal():
    reg = copy.deepcopy(committed())
    hr.register_experiment(reg, "exp1", **REG); hr.seal(reg)
    hr.add_evidence(reg, "exp1", "BACKTEST", result="mean 0.3 bps", provenance="run abc")
    hr.amend(reg, "exp1", "added a robustness table", "reviewer asked")
    assert hr.verify(reg) == []


def test_a_missing_default_registry_raises_but_a_scratch_path_may_be_new(tmp_path, monkeypatch):
    monkeypatch.setattr(hr, "DEFAULT_PATH", str(tmp_path / "gone.json"))
    with pytest.raises(hr.RegistryError, match="missing"):
        hr.load()
    assert hr.load(str(tmp_path / "scratch.json"))["trials"] == []
    bad = tmp_path / "bad.json"; bad.write_text("{not json")
    with pytest.raises(hr.RegistryError):
        hr.load(str(bad))


@pytest.mark.parametrize("rules", [-25, True, 1.5, "7"])
def test_a_bad_rule_count_is_refused_and_a_small_one_cannot_shrink_the_width(rules):
    reg = copy.deepcopy(committed())
    reg["search_width"]["distinct_gated_rules_scored"] = rules
    with pytest.raises(hr.RegistryError):
        hr.summary(reg)


def test_the_working_width_is_never_below_the_recorded_count():
    reg = copy.deepcopy(committed())
    reg["search_width"]["distinct_gated_rules_scored"] = 0
    s = hr.summary(reg)
    assert s["search_width"]["n_working"] >= s["n_trials_recorded"] - 3 and s["search_width"]["n_working"] == max(s["n_trials_recorded"], s["n_trials_recorded"] - 3)


def test_registration_requires_every_field_and_rejects_unknown_ones():
    reg = {"trials": []}
    for k in hr.REQUIRED_REGISTRATION:
        bad = {kk: v for kk, v in REG.items() if kk != k}
        with pytest.raises(ValueError, match="missing"):
            hr.register_experiment(reg, "e", **bad)
    with pytest.raises(ValueError, match="unknown registration field"):
        hr.register_experiment(reg, "e", **REG, secret_sauce=1)
    assert reg["trials"] == []
    hr.register_experiment(reg, "e", **REG)                              # positive control
    with pytest.raises(ValueError, match="already recorded"):
        hr.register_experiment(reg, "e", **REG)


def test_evidence_is_per_level_never_one_flag_and_needs_provenance():
    reg = {"trials": []}
    hr.register_experiment(reg, "e", **REG)
    hr.add_evidence(reg, "e", "BACKTEST", result="positive", provenance="run 1")
    st = hr.evidence_status(reg["trials"][0])
    assert st["BACKTEST"] == "positive" and all(v is None for k, v in st.items() if k != "BACKTEST")      # a backtest says nothing about the others
    for kw in (dict(level="NOPE", result="x", provenance="p"), dict(level="OUT_OF_SAMPLE", result="x", provenance=""), dict(level="OUT_OF_SAMPLE", result=" ", provenance="p")):
        with pytest.raises(ValueError):
            hr.add_evidence(reg, "e", **kw)
    assert "passed" not in json.dumps(hr.evidence_status(reg["trials"][0])).lower()


def test_an_abandoned_trial_stays_counted_and_takes_no_more_evidence():
    reg = {"trials": []}
    hr.register_experiment(reg, "e", **REG)
    hr.abandon(reg, "e", "no data for the universe")
    assert len(reg["trials"]) == 1 and hr.summary(reg)["n_trials_recorded"] == 1
    with pytest.raises(ValueError, match="abandoned"):
        hr.add_evidence(reg, "e", "BACKTEST", result="x", provenance="p")
    with pytest.raises(ValueError, match="already abandoned"):
        hr.abandon(reg, "e", "again")
    with pytest.raises(ValueError):
        hr.abandon(reg, "e2", "unknown")


def test_registration_fields_cannot_be_changed_by_an_amendment():
    reg = {"trials": []}
    hr.register_experiment(reg, "e", **REG); hr.seal(reg)
    hr.amend(reg, "e", "changed costs", "new fee tier")
    assert hr.verify(reg) == []
    reg["trials"][0]["costs"] = "cheaper"                              # editing the registered field itself is the tamper
    assert any("ALTERED" in p for p in hr.verify(reg))


# ---------------------------------------------------------------- red-team reproductions (8c7a716)

def _sealed_pair(tmp_path):
    reg = {"trials": []}
    hr.register_experiment(reg, "t1", **REG); hr.register_experiment(reg, "t2", **REG)
    hr.add_evidence(reg, "t1", "BACKTEST", result="FAIL", provenance="run 1")
    path = str(tmp_path / "r.json"); hr.save(reg, path)
    return hr.load(path), path


def test_a_save_cannot_launder_a_removed_or_altered_trial(tmp_path):
    reg, path = _sealed_pair(tmp_path)
    reg["trials"][0]["parameter_grid"] = {"x": [999]}; del reg["trials"][1]
    assert hr.verify(reg)
    with pytest.raises(hr.RegistryError, match="refusing to save over tampering"):
        hr.save(reg, path)
    hr.save(reg, path, accept_tampering=True)                           # the owner's explicit, named re-baseline


def test_evidence_cannot_be_edited_wiped_or_have_abandonment_removed(tmp_path):
    reg, path = _sealed_pair(tmp_path)
    edited = copy.deepcopy(reg); edited["trials"][0]["evidence"][0]["result"] = "PASS"
    assert any("evidence was ALTERED" in p for p in hr.verify(edited))
    wiped = copy.deepcopy(reg); wiped["trials"][0]["evidence"] = []
    assert any("evidence was ALTERED" in p for p in hr.verify(wiped))
    ok = copy.deepcopy(reg); hr.add_evidence(ok, "t1", "OUT_OF_SAMPLE", result="x", provenance="p")
    assert hr.verify(ok) == []                                          # appending is allowed (positive control)
    hr.abandon(reg, "t2", "no data"); hr.save(reg, path); reg = hr.load(path)
    gone = copy.deepcopy(reg); del gone["trials"][1]["abandoned"]
    assert any("abandonment was removed" in p for p in hr.verify(gone))
    with pytest.raises(hr.RegistryError):
        hr.save(gone, path)


def test_evidence_cannot_be_added_to_a_superseded_trial():
    reg = {"trials": []}
    hr.register_experiment(reg, "old", **REG); hr.register_experiment(reg, "new", **REG)
    hr.supersede(reg, "old", "new", "replaced")
    with pytest.raises(ValueError, match="superseded"):
        hr.add_evidence(reg, "old", "BACKTEST", result="x", provenance="p")
    hr.add_evidence(reg, "new", "BACKTEST", result="x", provenance="p")


def test_a_nan_in_an_entry_is_reported_not_crashed_into_a_false_pass(tmp_path):
    reg, _ = _sealed_pair(tmp_path)
    reg["trials"][0]["evidence"][0]["details"] = {"x": float("nan")}
    assert any("cannot be read" in p or "ALTERED" in p for p in hr.verify(reg))


def test_the_keyless_seal_is_fooled_by_delete_and_reseal_but_an_owner_held_anchor_is_not():
    reg = committed()
    anchor = hr.anchor_digest(reg)
    assert hr.verify(reg, anchor=anchor) == [] and hr.anchor_digest(copy.deepcopy(reg)) == anchor
    gone = copy.deepcopy(reg)
    gone["trials"] = gone["trials"][1:]
    hr.seal(gone)
    assert hr.verify(gone) == []                                    # a resealed registry verifies against its own (keyless) seal: untrusted
    assert any("anchor" in p for p in hr.verify(gone, anchor=anchor))
    assert hr.verify(reg, anchor="not-a-digest")
    assert hr.main(["--path", hr.DEFAULT_PATH, "--print-anchor"]) == 0
