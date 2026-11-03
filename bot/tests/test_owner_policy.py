"""The owner's inputs, reported exactly as they are; no value is ever suggested
or defaulted, and the status and the gate use the same validation."""
from __future__ import annotations

import os
import re
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config as _config                                      # noqa: E402
import owner_policy as op                                     # noqa: E402

NOW = 2_000_000_000.0
BOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO = os.path.dirname(BOT)


def cfg(**env):
    return _config.load(env)


def test_nothing_set_means_unset_and_evidence_cannot_qualify():
    s = op.owner_policy_status(cfg(), now_s=NOW)
    assert s["discovery_window"]["state"] == op.UNSET
    assert s["drawdown_limit_usd"]["state"] == op.UNSET
    assert s["policy_inputs_complete"] is False
    assert len(s["missing_or_unusable"]) == 2
    assert "INSUFFICIENT" in s["consequence"]
    assert "suggests no values" in s["note"]


def test_a_complete_valid_policy_can_qualify_evidence():
    s = op.owner_policy_status(cfg(
        CARRY_EVIDENCE_DISCOVERY_START_EPOCH=str(NOW - 400 * 86400),
        CARRY_EVIDENCE_DISCOVERY_END_EPOCH=str(NOW - 200 * 86400),
        CARRY_EVIDENCE_DRAWDOWN_LIMIT_USD="75"), now_s=NOW)
    assert s["policy_inputs_complete"] is True and s["missing_or_unusable"] == []
    assert s["drawdown_limit_usd"]["value"] == 75.0
    assert s["discovery_window"]["start_epoch"] < s["discovery_window"]["end_epoch"]


@pytest.mark.parametrize("env,why", [
    ({"CARRY_EVIDENCE_DISCOVERY_START_EPOCH": "1000"}, "only one end"),
    ({"CARRY_EVIDENCE_DISCOVERY_END_EPOCH": "1000"}, "only one end"),
    ({"CARRY_EVIDENCE_DISCOVERY_START_EPOCH": "2000",
      "CARRY_EVIDENCE_DISCOVERY_END_EPOCH": "1000"}, "not before"),
])
def test_unusable_windows_are_invalid_with_a_reason(env, why):
    window, state, reason = op.discovery_window(cfg(**env), now_s=NOW)
    assert window is None and state == op.INVALID and why in reason


def test_a_window_still_open_is_a_valid_input_for_the_evidence_judge():
    """Independence from an open window is strategy_evidence's call (it blocks
    overlapping trades); the policy layer does not duplicate that rule."""
    c = cfg(CARRY_EVIDENCE_DISCOVERY_START_EPOCH="1000",
            CARRY_EVIDENCE_DISCOVERY_END_EPOCH=str(NOW + 86400))
    window, state, _ = op.discovery_window(c, now_s=NOW)
    assert state == op.SET and window == (1000.0, NOW + 86400)


def test_the_provider_reads_the_policy_at_each_verdict_not_at_startup(monkeypatch):
    import carry_evidence
    import main as _main
    captured = {}
    monkeypatch.setattr(carry_evidence, "make_provider",
                        lambda **kw: captured.update(kw) or (lambda: None))

    class Bot:
        class store:
            _path = "x.db"
    c = cfg()
    _main._carry_evidence_provider(Bot(), c)
    assert callable(captured["discovery_window_s"]) and callable(
        captured["drawdown_limit_usd"])
    assert captured["discovery_window_s"]() is None
    object.__setattr__(c, "CARRY_EVIDENCE_DISCOVERY_START_EPOCH", 1000.0)
    object.__setattr__(c, "CARRY_EVIDENCE_DISCOVERY_END_EPOCH", 2000.0)
    assert captured["discovery_window_s"]() == (1000.0, 2000.0)   # same process, later


def test_with_no_configuration_the_status_keeps_its_shape():
    import main as _main
    full = set(op.owner_policy_status(cfg(), now_s=NOW))
    assert set(_main._owner_policy_status(None)) == full
    assert _main._owner_policy_status(None)["policy_inputs_complete"] is False


def test_complete_inputs_do_not_claim_entries_will_pass():
    s = op.owner_policy_status(cfg(
        CARRY_EVIDENCE_DISCOVERY_START_EPOCH="1000",
        CARRY_EVIDENCE_DISCOVERY_END_EPOCH="2000",
        CARRY_EVIDENCE_DRAWDOWN_LIMIT_USD="5"), now_s=NOW)
    assert "execution samples" in s["consequence"] and "reconciled" in s["consequence"]


def test_the_status_and_the_evidence_provider_share_one_validation(monkeypatch):
    """What the status calls the window is exactly what the gate is given."""
    import carry_evidence
    import main as _main
    seen = []
    monkeypatch.setattr(carry_evidence, "make_provider",
                        lambda **kw: seen.append(kw) or (lambda: None))

    class Bot:
        class store:
            _path = "x.db"
    good = cfg(CARRY_EVIDENCE_DISCOVERY_START_EPOCH="1000",
               CARRY_EVIDENCE_DISCOVERY_END_EPOCH="2000",
               CARRY_EVIDENCE_DRAWDOWN_LIMIT_USD="10")
    _main._carry_evidence_provider(Bot(), good)
    assert seen[-1]["discovery_window_s"]() == (1000.0, 2000.0)
    assert seen[-1]["drawdown_limit_usd"]() == 10.0
    inverted = cfg(CARRY_EVIDENCE_DISCOVERY_START_EPOCH="2000",
                   CARRY_EVIDENCE_DISCOVERY_END_EPOCH="1000")
    _main._carry_evidence_provider(Bot(), inverted)
    assert seen[-1]["discovery_window_s"]() is None


def test_carry_status_carries_the_owner_policy(tmp_path):
    import main as _main
    from persistence import StateStore
    from test_paired_leg_recovery import FeeVenue, modern, stack, _bot_on
    store, venue, broker, engine = stack(str(tmp_path / "s.db"),
                                         FeeVenue(modern, spot_qty=0.0))
    bot = _bot_on(store, engine)
    bot.cfg = cfg()
    bot._last_carry_decision = {}
    bot._journal_unsaved = False
    status = bot.carry_status()
    assert status["owner_policy"]["policy_inputs_complete"] is False
    assert status["owner_policy"]["missing_or_unusable"]
    store.close()


# -- every deployment states its entry-gate mode; none relies on the default ----------

def _toml_env(path):
    text = open(path, encoding="utf-8").read()
    m = re.search(r"^\[env\]\n(.*?)(?=^\[|\Z)", text, re.S | re.M)
    return dict(re.findall(r'^([A-Z_]+)\s*=\s*"([^"]*)"', m.group(1), re.M))


@pytest.mark.parametrize("name,expected", [("fly.toml", "enforce"),
                                           ("fly.drill.toml", "observe")])
def test_each_deployment_declares_its_entry_benefit_mode(name, expected):
    env = _toml_env(os.path.join(BOT, name))
    assert env.get("CARRY_ENTRY_BENEFIT_MODE") == expected, (
        f"{name} must state CARRY_ENTRY_BENEFIT_MODE explicitly")


def test_the_drill_workflow_declares_observe():
    text = open(os.path.join(REPO, ".github", "workflows",
                             "carry-drill-testnet.yml"), encoding="utf-8").read()
    assert re.search(r"CARRY_ENTRY_BENEFIT_MODE:\s*observe", text)


def test_no_deployment_config_sets_the_owner_evidence_inputs():
    """The owner's inputs are theirs. A committed value would be an invented one."""
    for name in ("fly.toml", "fly.drill.toml"):
        env = _toml_env(os.path.join(BOT, name))
        assert not [k for k in env if k.startswith("CARRY_EVIDENCE_")], name
    text = open(os.path.join(REPO, ".github", "workflows",
                             "carry-drill-testnet.yml"), encoding="utf-8").read()
    assert "CARRY_EVIDENCE_" not in text


# -- the opportunity-cost assumption is stated, never invented -------------------

def test_an_unset_opportunity_cost_is_reported_as_free_capital_and_is_not_required():
    import types
    import owner_policy as op
    cfg = types.SimpleNamespace(
        CARRY_EVIDENCE_DISCOVERY_START_EPOCH=1.0,
        CARRY_EVIDENCE_DISCOVERY_END_EPOCH=2.0,
        CARRY_EVIDENCE_DRAWDOWN_LIMIT_USD=20.0,
        CARRY_ENTRY_BENEFIT_MODE="enforce", CARRY_OPPORTUNITY_COST_APR=None)
    st = op.owner_policy_status(cfg)
    (a,) = st["assumptions"]
    assert a["state"] == "UNSET_TREATED_AS_ZERO" and a["value"] is None
    assert "FREE" in a["meaning"] and a["affects_policy_inputs_complete"] is False
    assert st["policy_inputs_complete"] is True        # not a required input


def test_a_supplied_opportunity_cost_is_labelled_user_supplied_and_unverified():
    import types
    import owner_policy as op
    a = op.opportunity_cost_assumption(
        types.SimpleNamespace(CARRY_OPPORTUNITY_COST_APR=0.05))
    assert a["state"] == "USER_SUPPLIED" and a["value"] == 0.05
    assert "not verified" in a["meaning"]


def test_zero_cannot_be_told_from_unset_once_parsed_and_says_so():
    import types
    import owner_policy as op
    a = op.opportunity_cost_assumption(
        types.SimpleNamespace(CARRY_OPPORTUNITY_COST_APR=0.0))
    assert a["state"] == "ZERO_UNSET_OR_EXPLICIT" and "FREE" in a["meaning"]


def test_a_negative_or_bad_opportunity_cost_is_invalid():
    import types
    import owner_policy as op
    for bad in (-0.01, "x", float("nan")):
        a = op.opportunity_cost_assumption(
            types.SimpleNamespace(CARRY_OPPORTUNITY_COST_APR=bad))
        assert a["state"] == "INVALID"


def test_the_check_tool_reports_the_assumption_without_changing_its_exit_code():
    import io
    import json
    from contextlib import redirect_stdout
    import owner_policy_check as chk
    base = {"CARRY_EVIDENCE_DISCOVERY_START_EPOCH": "1",
            "CARRY_EVIDENCE_DISCOVERY_END_EPOCH": "2",
            "CARRY_EVIDENCE_DRAWDOWN_LIMIT_USD": "20"}
    def run(extra):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = chk.main(env={**base, **extra})
        return code, json.loads(buf.getvalue())
    code, out = run({})
    assert code == 0 and out["assumptions"][0]["state"] == "UNSET_TREATED_AS_ZERO"
    code, out = run({"CARRY_OPPORTUNITY_COST_APR": "0.04"})
    assert code == 0 and out["assumptions"][0]["state"] == "USER_SUPPLIED"
    code, out = run({"CARRY_OPPORTUNITY_COST_APR": "banana"})
    assert code == 3 and "CARRY_OPPORTUNITY_COST_APR" in out["unparseable_settings"]
    assert "banana" not in json.dumps(out)


def test_unset_and_explicit_zero_are_told_apart_when_the_config_says_so():
    import types
    import owner_policy as op
    unset = op.opportunity_cost_assumption(types.SimpleNamespace(
        CARRY_OPPORTUNITY_COST_APR=0.0, CARRY_OPPORTUNITY_COST_APR_SUPPLIED=False))
    zero = op.opportunity_cost_assumption(types.SimpleNamespace(
        CARRY_OPPORTUNITY_COST_APR=0.0, CARRY_OPPORTUNITY_COST_APR_SUPPLIED=True))
    assert unset["state"] == "UNSET_TREATED_AS_ZERO" and unset["value"] == 0.0
    assert zero["state"] == "EXPLICIT_ZERO" and zero["value"] == 0.0
    assert unset["state"] != zero["state"]


def test_config_records_whether_the_opportunity_cost_was_supplied():
    import config
    base = config.load({})
    assert base is not None
    assert base.CARRY_OPPORTUNITY_COST_APR_SUPPLIED is False
    assert base.CARRY_OPPORTUNITY_COST_APR == 0.0
    z = config.load({"CARRY_OPPORTUNITY_COST_APR": "0"})
    assert z.CARRY_OPPORTUNITY_COST_APR_SUPPLIED is True
    assert z.CARRY_OPPORTUNITY_COST_APR == 0.0
    blank = config.load({"CARRY_OPPORTUNITY_COST_APR": "  "})
    assert blank.CARRY_OPPORTUNITY_COST_APR_SUPPLIED is False
