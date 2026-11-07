"""The testnet action manifest: generation, strict closed schema, mutation table, hostile negatives, CLI. Offline."""
import copy
import json
import os
import subprocess
import sys

import pytest

BOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BOT)
sys.path.insert(0, os.path.join(BOT, "tools"))

import shadow  # noqa: E402
import testnet_action_manifest as M  # noqa: E402
import testnet_certificate as TC  # noqa: E402

TOOL = os.path.join(BOT, "tools", "testnet_action_manifest.py")


def fresh():
    return M.generate()


def resealed(m):
    m = copy.deepcopy(m)
    m["plan_sha256"] = M.plan_digest(m)
    return m


def test_generated_manifest_validates_and_is_not_granted():
    m = fresh()
    r = M.validate(m, env={})
    assert r["valid"] and r["refusals"] == [] and m["authorisation"] == "NOT_GRANTED" and r["authorised"] is False
    assert m["bounds"]["max_notional_usd"] == shadow.SHADOW_MAX_NOTIONAL_USD
    assert m["bounds"]["max_entries"] == 1 and m["bounds"]["max_lifecycle_hours"] == 9.0 and m["bounds"]["complete_lifecycles"] == 1
    assert m["venue"] == "testnet" and m["symbol"] == "BTCUSDT" and m["credential_class"] == "testnet_dedicated"


def test_generation_is_deterministic():
    assert fresh() == fresh()


def test_manifest_agrees_with_certificate_section_c():
    c = TC.section_c()
    m = fresh()
    assert list(M.STOP_CONDITIONS) == c["stop_conditions"]
    assert m["bounds"]["max_notional_usd"] == c["max_notional_usd"] and m["bounds"]["max_entries"] == c["max_entries"]
    assert m["bounds"]["max_lifecycle_hours"] == c["max_duration_hours"] and m["bounds"]["max_loss_usd"] == c["max_loss_test_currency_usd"]
    assert M.RECOVERY["close_command"] in c["recovery"] and M.RECOVERY["reconcile_command"] in c["recovery"]
    assert c["authorisation"].startswith("NOT GRANTED")


def test_preflight_list_covers_every_required_fact():
    facts = {r["fact"] for r in fresh()["preflight_reads"]}
    assert facts == {"account_identity", "position_mode", "margin_mode", "collateral", "positions", "orders", "instrument_rules", "funding_interval_and_cap",
                     "fee_tier", "clock_drift", "order_history_readable"}


def test_generator_cannot_emit_an_authorised_manifest(monkeypatch):
    monkeypatch.setattr(M, "NOT_GRANTED", "GRANTED_BY_OWNER:me-1")
    with pytest.raises(M.ManifestError):
        M.generate()
    monkeypatch.undo()
    assert not hasattr(M.generate, "__wrapped__")
    import inspect
    assert not inspect.signature(M.generate).parameters


def test_owner_marking_authorised_keeps_plan_digest_and_validates_without_enabling_anything():
    m = fresh()
    m["authorisation"] = "GRANTED_BY_OWNER:owner-2026-11-07"
    r = M.validate(m, env={})
    assert r["valid"] and r["authorised"] is True and r["plan_sha256"] == fresh()["plan_sha256"]


@pytest.mark.parametrize("bad", ["GRANTED", "granted_by_owner:abc", "GRANTED_BY_OWNER:", "GRANTED_BY_OWNER:a b c", "NOT_GRANTED ", "not_granted", "", None, True, 1,
                                 ["NOT_GRANTED"], {"a": 1}, "GRANTED_BY_OWNER:" + "x" * 65])
def test_authorisation_field_is_closed(bad):
    m = fresh()
    m["authorisation"] = bad
    assert not M.validate(m, env={})["valid"]


# ---- single-field mutations --------------------------------------------------------------------------------------------------------------------------------
def _mut(path, value, reseal=True):
    m = fresh()
    cur = m
    for p in path[:-1]:
        cur = cur[p]
    cur[path[-1]] = value
    return resealed(m) if reseal else m


def _drop(path):
    m = fresh()
    cur = m
    for p in path[:-1]:
        cur = cur[p]
    del cur[path[-1]]
    return m


CAP = shadow.SHADOW_MAX_NOTIONAL_USD
MUTATIONS = {
    "notional raised": _mut(["bounds", "max_notional_usd"], CAP + 0.01),
    "notional x10": _mut(["bounds", "max_notional_usd"], CAP * 10),
    "notional zero": _mut(["bounds", "max_notional_usd"], 0),
    "notional negative": _mut(["bounds", "max_notional_usd"], -1),
    "notional string": _mut(["bounds", "max_notional_usd"], "100"),
    "notional bool": _mut(["bounds", "max_notional_usd"], True),
    "notional inf": _mut(["bounds", "max_notional_usd"], float("inf"), reseal=False),
    "notional nan": _mut(["bounds", "max_notional_usd"], float("nan"), reseal=False),
    "notional null": _mut(["bounds", "max_notional_usd"], None),
    "entries 2": _mut(["bounds", "max_entries"], 2),
    "entries 0": _mut(["bounds", "max_entries"], 0),
    "entries float": _mut(["bounds", "max_entries"], 1.0),
    "entries bool": _mut(["bounds", "max_entries"], True),
    "hours 9.01": _mut(["bounds", "max_lifecycle_hours"], 9.01),
    "hours 24": _mut(["bounds", "max_lifecycle_hours"], 24),
    "hours 0": _mut(["bounds", "max_lifecycle_hours"], 0),
    "hours str": _mut(["bounds", "max_lifecycle_hours"], "9"),
    "lifecycles 2": _mut(["bounds", "complete_lifecycles"], 2),
    "lifecycles 0": _mut(["bounds", "complete_lifecycles"], 0),
    "loss raised": _mut(["bounds", "max_loss_usd"], 1000),
    "loss zero": _mut(["bounds", "max_loss_usd"], 0),
    "venue mainnet": _mut(["venue"], "mainnet"),
    "venue demo": _mut(["venue"], "demo"),
    "venue eu": _mut(["venue"], "eu"),
    "venue case": _mut(["venue"], "Testnet"),
    "venue padded": _mut(["venue"], "testnet "),
    "venue null": _mut(["venue"], None),
    "symbol ETH": _mut(["symbol"], "ETHUSDT"),
    "category spot": _mut(["category"], "spot"),
    "mode other": _mut(["mode"], "acquire"),
    "schema other": _mut(["schema"], "testnet_action_manifest/2"),
    "cred class generic": _mut(["credential_class"], "generic"),
    "cred class mainnet": _mut(["credential_class"], "mainnet"),
    "cred class case": _mut(["credential_class"], "Testnet_Dedicated"),
    "cred class null": _mut(["credential_class"], None),
    "key name generic": _mut(["credential_env", "api_key"], "BYBIT_API_KEY"),
    "secret name generic": _mut(["credential_env", "api_secret"], "BYBIT_API_SECRET"),
    "key name = generic secret": _mut(["credential_env", "api_key"], "BYBIT_API_SECRET"),
    "key=secret name": _mut(["credential_env", "api_secret"], M.DEFAULT_KEY_ENV),
    "key name lowercase generic": _mut(["credential_env", "api_key"], "bybit_api_key"),
    "key name no TESTNET": _mut(["credential_env", "api_key"], "DEDICATED_API_KEY"),
    "key name MAINNET": _mut(["credential_env", "api_key"], "BYBIT_TESTNET_MAINNET_API_KEY"),
    "key name LIVE": _mut(["credential_env", "api_key"], "BYBIT_TESTNET_LIVE_KEY"),
    "key name DEMO": _mut(["credential_env", "api_secret"], "BYBIT_TESTNET_DEMO_SECRET"),
    "key name space": _mut(["credential_env", "api_key"], "BYBIT TESTNET KEY"),
    "key name value-like": _mut(["credential_env", "api_key"], "abcDEF123456secretvalue"),
    "key name int": _mut(["credential_env", "api_key"], 5),
    "stop condition lost": _mut(["stop_conditions"], list(M.STOP_CONDITIONS)[1:]),
    "stop condition lost (kill)": _mut(["stop_conditions"], [s for s in M.STOP_CONDITIONS if "KILL" not in s]),
    "stop conditions empty": _mut(["stop_conditions"], []),
    "stop condition added": _mut(["stop_conditions"], list(M.STOP_CONDITIONS) + ["whatever"]),
    "stop condition reworded": _mut(["stop_conditions"], ["never stop"] + list(M.STOP_CONDITIONS)[1:]),
    "stop conditions str": _mut(["stop_conditions"], "any stage fails"),
    "recovery command changed": _mut(["recovery", "close_command"], "tools/close_testnet_short.py"),
    "recovery restart rule changed": _mut(["recovery", "restart_rule"], "trust the local journal"),
    "recovery key dropped": _drop(["recovery", "flat_rule"]),
    "recovery extra key": _mut(["recovery", "extra"], "x"),
    "evidence lost": _mut(["evidence_capture"], ["cash ledger"]),
    "preflight read removed": _mut(["preflight_reads"], fresh()["preflight_reads"][1:]),
    "preflight read repointed to a write": _mut(["preflight_reads", 0, "endpoint"], "/v5/order/create"),
    "preflight read repointed to withdraw": _mut(["preflight_reads", 3, "endpoint"], "/v5/asset/withdraw/query-record"),
    "preflight signed flipped": _mut(["preflight_reads", 0, "signed"], False),
    "preflight extra field": _mut(["preflight_reads", 0, "extra"], 1),
    "preflight reordered": _mut(["preflight_reads"], list(reversed(fresh()["preflight_reads"]))),
    "preflight empty": _mut(["preflight_reads"], []),
    "digest wrong": _mut(["plan_sha256"], "0" * 64, reseal=False),
    "digest null": _mut(["plan_sha256"], None, reseal=False),
    "edited without reseal (notional lowered)": _mut(["bounds", "max_notional_usd"], 50.0, reseal=False),
    "edited without reseal (credential name)": _mut(["credential_env", "api_key"], "BYBIT_TESTNET_OTHER_API_KEY", reseal=False),
    "extra top-level field": {**fresh(), "extra": 1},
    "extra top-level field (sneaky)": {**fresh(), "max_notional_override_usd": 1000},
    "extra bounds field": _mut(["bounds", "max_orders"], 5),
    "extra credential_env field": _mut(["credential_env", "value"], "sk-123"),
    "bounds not a dict": _mut(["bounds"], [1, 1, 9, 1, 1]),
    "credential_env not a dict": _mut(["credential_env"], "BYBIT_TESTNET_X"),
}
for _k in list(fresh()):
    MUTATIONS[f"missing top-level {_k}"] = _drop([_k])
for _k in fresh()["bounds"]:
    MUTATIONS[f"missing bounds.{_k}"] = _drop(["bounds", _k])
for _k in fresh()["recovery"]:
    MUTATIONS[f"missing recovery.{_k}"] = _drop(["recovery", _k])


@pytest.mark.parametrize("name", sorted(MUTATIONS))
def test_every_single_field_mutation_is_refused(name):
    r = M.validate(MUTATIONS[name], env={})
    assert not r["valid"] and r["refusals"], name
    assert r["credential_presence"] == {"api_key": None, "api_secret": None}


@pytest.mark.parametrize("m", [None, [], "x", 1, True, {}, [fresh()], json.dumps(fresh())])
def test_non_object_manifests_refused(m):
    assert not M.validate(m, env={})["valid"]


def test_lowering_a_bound_is_allowed_only_with_a_matching_digest():
    m = _mut(["bounds", "max_notional_usd"], 50.0)
    assert M.validate(m, env={})["valid"]
    m2 = _mut(["bounds", "max_lifecycle_hours"], 3)
    assert M.validate(m2, env={})["valid"]
    assert not M.validate(_mut(["bounds", "max_notional_usd"], 4.99), env={})["valid"]


# ---- credentials: presence only ---------------------------------------------------------------------------------------------------------------------------
def test_generic_bybit_key_never_counts_as_the_testnet_credential():
    m = fresh()
    r = M.validate(m, env={"BYBIT_API_KEY": "GENERICKEYVALUE", "BYBIT_API_SECRET": "GENERICSECRETVALUE"})
    assert r["valid"] and r["credential_presence"] == {"api_key": False, "api_secret": False}


def test_presence_is_recorded_and_values_never_appear_in_the_report():
    env = {M.DEFAULT_KEY_ENV: "TOPSECRETKEY123", M.DEFAULT_SECRET_ENV: "TOPSECRETSECRET456"}
    r = M.validate(fresh(), env=env)
    assert r["credential_presence"] == {"api_key": True, "api_secret": True}
    blob = json.dumps(r)
    assert "TOPSECRETKEY123" not in blob and "TOPSECRETSECRET456" not in blob
    assert M.validate(fresh(), env={M.DEFAULT_KEY_ENV: "  "})["credential_presence"] == {"api_key": False, "api_secret": False}


# ---- CLI --------------------------------------------------------------------------------------------------------------------------------------------------
def _run(*args, env_extra=None):
    env = {k: v for k, v in os.environ.items() if not k.startswith("BYBIT_")}
    env.update(env_extra or {})
    return subprocess.run([sys.executable, TOOL, *args], capture_output=True, text=True, env=env, timeout=120)


def test_cli_generate_then_validate_roundtrip(tmp_path):
    g = _run("generate")
    assert g.returncode == 0
    m = json.loads(g.stdout)
    assert m == fresh() and m["authorisation"] == "NOT_GRANTED"
    f = tmp_path / "m.json"
    f.write_text(g.stdout)
    v = _run("validate", str(f))
    assert v.returncode == 0 and json.loads(v.stdout)["valid"] is True


def test_cli_validate_exits_nonzero_on_refusal_and_on_garbage(tmp_path):
    f = tmp_path / "m.json"
    bad = fresh()
    bad["bounds"]["max_notional_usd"] = 1000
    f.write_text(json.dumps(bad))
    assert _run("validate", str(f)).returncode == 1
    f.write_text("{not json")
    assert _run("validate", str(f)).returncode == 1
    assert _run("validate", str(tmp_path / "missing.json")).returncode == 1
    f.write_text('{"venue":"testnet","venue":"mainnet"}')
    assert _run("validate", str(f)).returncode == 1
    f.write_text('{"a": NaN}')
    assert _run("validate", str(f)).returncode == 1


def test_cli_validate_never_prints_credential_values(tmp_path):
    f = tmp_path / "m.json"
    f.write_text(json.dumps(fresh()))
    v = _run("validate", str(f), env_extra={M.DEFAULT_KEY_ENV: "LEAKME-KEY", M.DEFAULT_SECRET_ENV: "LEAKME-SECRET"})
    assert v.returncode == 0 and "LEAKME" not in v.stdout + v.stderr and json.loads(v.stdout)["credential_presence"]["api_key"] is True


def test_every_preflight_read_is_allowed_for_the_monitor_role_and_none_for_the_collector_if_signed():
    import runtime_capability as RC
    mon = RC.Capability.for_role(RC.READ_ONLY_MONITOR, venue="testnet")
    col = RC.Capability.for_role(RC.PUBLIC_COLLECTOR, venue="testnet")
    for f, e, s in M.PREFLIGHT_READS:
        assert mon.check("GET", e, s, mon.host) is None
        assert (col.check("GET", e, s, col.host) is None) == (not s)


@pytest.mark.parametrize("name", ["BYBIT_API_KEY", "BYBIT_API_SECRET"])
def test_generic_credential_names_are_refused_by_the_generic_name_guard_itself(name):
    for field in ("api_key", "api_secret"):
        r = M.refusals(_mut(["credential_env", field], name))
        assert any("generic credential" in x for x in r), r
