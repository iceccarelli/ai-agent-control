"""The authoritative registry cannot silently disagree with the production rule.

`artifacts/hypothesis_registry.json` is append-only: a trial's note and numbers
are history. So when the rule `CarryEngine` runs changes, the way to keep the
registry truthful is a NEW trial that describes the rule now in force
(`production_rule: true`, with the constants it rests on written out as data) and
a pointer on the entry it replaces (`hypothesis_registry.supersede`), never an
edit of the old text.

These tests fail the day the code and the registry part company:
  * a constant of the production rule moves, or the engine/simulators stop asking
    the gate the registry names, without a new trial;
  * an entry still claims to be "the rule CarryEngine runs" and is not;
  * a historical entry is edited (every pre-existing trial is pinned by hash,
    ignoring only the supersession pointer).
"""
from __future__ import annotations

import hashlib
import inspect
import json
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

import carry_backtest as cb  # noqa: E402
import carry_costs as cc  # noqa: E402
import carry_engine  # noqa: E402
import hypothesis_registry as hr  # noqa: E402

CLAIM = "This is the rule CarryEngine runs"


@pytest.fixture(scope="module")
def registry():
    return hr.load()


def production(registry):
    return [t for t in registry["trials"] if t.get("production_rule") is True]


def test_exactly_one_trial_is_the_production_rule(registry):
    prod = production(registry)
    assert [t["trial_id"] for t in prod] == ["carry_entry_gate_v1@BTCUSDT"]
    assert "superseded_by" not in prod[0]


def test_its_constants_are_the_ones_the_code_uses(registry):
    rule = production(registry)[0]["rule"]
    assert rule["engine_entry_function"] == "carry_costs.entry_gate"
    assert rule["assumed_hold_days"] == cc.ASSUMED_HOLD_DAYS
    assert rule["demonstrated_hold_days"] == cc.DEMONSTRATED_HOLD_DAYS
    assert rule["ewma_alpha"] == cc.EWMA_ALPHA
    assert rule["ewma_min_prints"] == cc.EWMA_MIN_PRINTS
    assert rule["funding_periods_per_day"] == cc.FUNDING_PERIODS_PER_DAY


def test_the_engine_and_the_simulators_ask_the_gate_the_registry_names(registry):
    fn = production(registry)[0]["rule"]["engine_entry_function"].split(".")[-1]
    assert fn == "entry_gate"
    engine_src = inspect.getsource(carry_engine.CarryEngine.entry_economics)
    assert f".{fn}(" in engine_src and ".evaluate_entry(" not in engine_src
    sim_src = inspect.getsource(cb.simulate_settlements_series)
    assert f".{fn}(" in sim_src and ".evaluate_entry(" not in sim_src
    assert callable(getattr(cc, fn))


def test_a_note_claiming_to_be_the_production_rule_is_the_production_rule(registry):
    for t in registry["trials"]:
        if CLAIM in t.get("note", ""):
            assert t.get("production_rule") is True or "superseded_by" in t, (
                f"{t['trial_id']} says it is the rule CarryEngine runs, but it "
                "is neither the production rule nor marked superseded: append "
                "a new trial describing the current rule and supersede this")


def test_the_replaced_entries_point_at_the_current_rule_and_keep_their_text(registry):
    by_id = {t["trial_id"]: t for t in registry["trials"]}
    for old in ("carry_cost_gates_v1@BTCUSDT", "carry_overlay_v1@BTCUSDT"):
        assert by_id[old]["superseded_by"] == "carry_entry_gate_v1@BTCUSDT"
        assert by_id[old]["superseded_reason"].startswith("Superseded")
    assert CLAIM in by_id["carry_cost_gates_v1@BTCUSDT"]["note"]   # history kept


def test_the_new_entry_makes_no_claim_it_cannot_support(registry):
    note = production(registry)[0]["note"]
    assert "IN-SAMPLE ONLY" in note and "not validation" in note
    assert "FORWARD" in note and "HOLDOUT_SCORED is False" in note
    assert "INCONCLUSIVE" in note
    assert production(registry)[0]["status"] == "measured_in_sample"
    for banned in ("edge established", "profitable", "out-of-sample validated"):
        assert banned not in note


# -- append-only: nothing recorded before may have changed -----------------------

PINNED = {
    "technical_analysis":
        "65b81bd02f48b435e98f844f0a334b3b6687b136db890baa5a471eedf767623d",
    "closed_analyser":
        "27492667c2e909e9ef60756f2fb25cf09ce8c93e5db71458635e832942fcebef",
    "donchian_breakout_v1":
        "d954cdb3cb672ca56b2bb5e82a1d1354464d3f0b098c2c6a8facccecc0c904c4",
    "btc_alt_spillover_v1":
        "6b3bdb94df3baed25c3f520c623258b29e64fdfb7315359a3f93f37fdc75c92c",
    "post_shock_fade_v1":
        "d211ea3263ee7ecc97dd0ba18dd785aa151bd83dbad8f294f5d2128030e1d7a2",
    "range_location_fade_v1":
        "9da02865be6d54f1c80615e8252a73ec31d640e87d873f4a1a91652914d3cfc9",
    "open_gap_fade_v1":
        "3518e728b82ec316bdf15eb709fdc80686cf72510ca8ac7527b8598b1b931174",
    "ts_momentum_v1":
        "086a49fdcf98c7ccac3dd6793a498c0e03906414546c3f66d313a5c74ea0a918",
    "sign_flip_momentum_v1":
        "b8bff98a67249459dfe8017bd4c598b689e0bb8ae69be19b8b8a2866d105a425",
    "compression_breakout_v1":
        "a2239656c597fbedb32c34567b76c71de177f9d143570d57ee05ff114a537b8f",
    "funding_carry_fade_v1":
        "a365ceb8bbee8b86e8038a1b5c86b84da2a2b5b6d6c0a634ffe8666664a7db8d",
    "funding_carry_fade_btc_v1":
        "c8014b6d782ce75faccdb856769ebf166767366f75499b667670b6d01cc42416",
    "funding_carry_fade_v1@BTCUSDT":
        "2634e241f862ce9a78b4ad1aeb8afdae68240da8c354c2c89559be369a05daae",
    "funding_carry_fade_v1@ETHUSDT":
        "ca0d51c4956de5d3b1e173cac6b52296fdf5d91f70f42e4098e038672ee5ea0f",
    "funding_carry_fade_v1@SOLUSDT":
        "b98baa5d491d8f0de5fa832e4c408d86a05649a254c7cb8285d2dd717678b24a",
    "btc_alt_spillover_v1@ETHUSDT":
        "0e2f5007b025f3fcbfb3c1b557cc829bbbadc74dd9776962514227c1d2fd8b01",
    "btc_alt_spillover_v1@SOLUSDT":
        "a50f5df71491729cc3d71abbd78a5c41cfedf12f339b65bab540020de3641ef2",
    "sign_flip_momentum_v1@BTCUSD":
        "358087463d4ec838ce3f309c887fc23d617ba6d14a1c5b9647cf7465a56f306b",
    "sign_flip_momentum_v1@ETHUSDT":
        "7abfd642857c34ac39e611529770b690f5308b6d459023ce94dfd277e6f5bb94",
    "sign_flip_momentum_v1@SOLUSDT":
        "901ead098e883135565ee8abd938316bc538d6edb3b9678b9ab150c771737e01",
    "compression_breakout_v1@BTCUSD":
        "e2ca52831bb17a9e8fbdd933d4afa35bdcb3710f1eba772c3228c44c9b72cfac",
    "post_shock_fade_v1@BTCUSD":
        "ec38407488cb6925b81f97b61ec26904ca6fbfa66462eda4bcfdc62d5ef47471",
    "donchian_breakout_v1@BTCUSD":
        "628fc80d079a577259c804a149fb4dfe254b14534f1de31ed8171f7078491a3e",
    "donchian_regression@BTCUSD":
        "b63523772f34700770c6ac2071c9001e9afd62ea36a67514308637d4a43cf096",
    "closed_regression@BTCUSD":
        "366f335b0048d27f0336b9c550150a5b5b86f91872ef07604682599c7bc81b96",
    "edge_recheck@BTCUSD":
        "4eab4189e0461b34dd2bcab901665a81eecbdf0f9c2620c96cd5eb6da7a5aad5",
    "edge_real_1h@BTCUSD":
        "fa52559e1a12c00fb941e5614b17bd5b36267b9f197bc208aba1610e2303c56b",
    "edge_real_4h@BTCUSD":
        "5273edfb6132a04e6eb0b4057e3107c55d0ca24f7241a78e39063fbbfe1d149b",
    "funding_carry_fade_btc_v1@BTCUSDT_OOS":
        "baa01c1d814b42bda7459d6c22a5c3bbe49d75c538e2edd7d9a54d82d9ebbeff",
    "carry_ungated_v1@BTCUSDT":
        "35730bd4bfd7473b91951fd00408bbe345a58f96bf5b52bf19478c674f86d271",
    "carry_cost_gates_v1@BTCUSDT":
        "6e1b73d003bed2eff9a57189301c060cdcb6914a500255b201f30701890103ec",
    "carry_overlay_v1@BTCUSDT":
        "8091be3e2dabddc1f62f3d633423dfceda5b5f164e01f32d974e2797d47abdf2",
    "carry_rule_sweep@59feb30d2328":
        "b3952c90851dcfdbef0dadad83b181c77b321e13f34151b61cb17688b14062a0",
    "carry_rule_sweep@d7d7d4141a05":
        "68a380c7f0bdf6a9a6efc4e43970a0780fc8098c7c6ef4a46e375d00ba3f3d2d",
    "carry_walkforward@69b08ab2ab84":
        "f46c393ac2c5c43a7af3a6ef80b925835d67b41cbcbdbd955c2f5d7ba75dd96e"
}


def _digest(entry):
    kept = {k: v for k, v in entry.items() if not k.startswith("superseded_")}
    return hashlib.sha256(json.dumps(kept, sort_keys=True).encode()).hexdigest()


def test_every_historical_trial_is_exactly_as_recorded(registry):
    by_id = {t["trial_id"]: t for t in registry["trials"]}
    for trial_id, digest in PINNED.items():
        assert trial_id in by_id, f"{trial_id} was removed from the registry"
        assert _digest(by_id[trial_id]) == digest, (
            f"{trial_id} was edited. The registry is append-only: record a new "
            "trial, and use hypothesis_registry.supersede for a pointer")


def test_the_frozen_strategy_decision_is_untouched(registry):
    by_id = {t["trial_id"]: t for t in registry["trials"]}
    assert by_id["funding_carry_fade_v1"]["status"] == "frozen_absent"
    assert "superseded_by" not in by_id["funding_carry_fade_v1"]


# -- the supersede mechanism itself ------------------------------------------------

class TestSupersede:
    def reg(self):
        r = {"trials": []}
        hr.record(r, "a", status="measured", note="old")
        hr.record(r, "b", status="measured_in_sample", note="new")
        return r

    def test_it_adds_a_pointer_and_changes_nothing_else(self):
        r = self.reg()
        before = dict(r["trials"][0])
        hr.supersede(r, "a", "b", "the rule changed")
        after = r["trials"][0]
        assert after["superseded_by"] == "b"
        assert {k: v for k, v in after.items()
                if not k.startswith("superseded_")} == before

    @pytest.mark.parametrize("args", [("zz", "b", "r"), ("a", "zz", "r"),
                                      ("a", "a", "r"), ("a", "b", "  ")])
    def test_it_refuses_unknown_self_or_unexplained(self, args):
        with pytest.raises(ValueError):
            hr.supersede(self.reg(), *args)

    def test_it_will_not_supersede_twice(self):
        r = self.reg()
        hr.record(r, "c", status="x")
        hr.supersede(r, "a", "b", "why")
        with pytest.raises(ValueError):
            hr.supersede(r, "a", "c", "why again")

    def test_record_still_refuses_to_overwrite(self):
        with pytest.raises(ValueError):
            hr.record(self.reg(), "a", status="x")
