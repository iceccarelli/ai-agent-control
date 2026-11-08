"""ms_testnet_adapter: the DISABLED owner-session adapter. Manifest closed schema, bounds lower-only, $100 AGGREGATE ceiling, dedicated credentials, kill-switch file semantics,
the capability flag, and an offline proof that nothing in the module can send anything. No network, no credential, no order."""
import ast
import copy
import json
import os
import socket
import subprocess
import sys

import pytest

BOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BOT)
sys.path.insert(0, os.path.join(BOT, "tools"))

import ms_risk  # noqa: E402
import ms_testnet_adapter as AD  # noqa: E402
import shadow  # noqa: E402

TOOL = os.path.join(BOT, "tools", "ms_testnet_adapter.py")
DIGEST = "a" * 64


def fresh(**kw):
    d = dict(candidate_id="cand_1", spec_digest=DIGEST, symbols=["BTCUSDT", "ETHUSDT"])
    d.update(kw)
    return AD.generate(**d)


def resealed(m):
    m = copy.deepcopy(m)
    m["plan_sha256"] = AD.plan_digest(m)
    return m


def granted(m):
    m = copy.deepcopy(m)
    m["authorisation"] = "GRANTED_BY_OWNER:ticket-42"
    return m


# ---------------------------------------------------------------- the manifest
def test_generated_manifest_validates_and_is_not_granted():
    m = fresh()
    r = AD.validate(m, env={})
    assert r["valid"] and r["refusals"] == [] and m["authorisation"] == "NOT_GRANTED" and r["authorised"] is False
    assert m["venue"] == "testnet" and m["credential_class"] == "testnet_dedicated"


def test_generation_is_deterministic_and_digest_ignores_only_authorisation():
    assert fresh() == fresh()
    m = fresh()
    assert AD.plan_digest(granted(m)) == m["plan_sha256"]
    assert AD.validate(granted(m), env={})["authorised"] is True and AD.validate(granted(m), env={})["valid"]


def test_aggregate_ceiling_is_100_across_all_symbols_and_never_per_symbol():
    assert AD.AGGREGATE_CEILING_USD == shadow.SHADOW_MAX_NOTIONAL_USD == 100.0
    m = fresh(symbols=["BTCUSDT", "ETHUSDT", "SOLUSDT"])
    assert m["bounds"]["aggregate_max_notional_usd"] == 100.0 and "max_notional_usd" not in m["bounds"]
    bad = copy.deepcopy(m)
    bad["bounds"]["aggregate_max_notional_usd"] = 100.01
    assert any("aggregate_max_notional_usd" in r for r in AD.refusals(resealed(bad)))
    # a larger per-symbol allowance than a hedged pair at the aggregate ceiling can never be stated
    bad = copy.deepcopy(m)
    bad["bounds"]["max_notional_per_symbol_usd"] = 200.5
    assert any("max_notional_per_symbol_usd" in r for r in AD.refusals(resealed(bad)))
    # the implied risk policy enforces ONE ceiling for all symbols: 3 symbols x 50 hedged = 150 > 100 is refused by ms_risk
    pol = AD.policy_from_manifest(m)
    assert pol["max_aggregate_notional_usd"] == 100.0 and not ms_risk.policy_refusals(pol)


def test_bounds_can_be_lowered_never_raised():
    low = fresh(lower={"aggregate_max_notional_usd": 20.0, "max_gross_exposure_usd": 40.0, "max_net_exposure_usd": 5.0, "max_unhedged_usd": 20.0})
    assert AD.validate(low, env={})["valid"] and AD.policy_from_manifest(low)["max_aggregate_notional_usd"] == 20.0
    for k, v in (("aggregate_max_notional_usd", 101.0), ("max_gross_exposure_usd", 201.0), ("max_net_exposure_usd", 101.0), ("max_unhedged_usd", 101.0), ("max_active_groups", 3),
                 ("max_entries_per_utc_day", 5), ("max_lifecycle_hours", 9.5), ("max_leverage", 3.5), ("max_market_age_ms", 5001), ("max_account_age_ms", 10001),
                 ("max_loss_usd", 1e9), ("max_notional_per_symbol_usd", 201.0)):
        m = fresh()
        m["bounds"][k] = v
        assert AD.refusals(resealed(m)), k
    with pytest.raises(AD.ManifestError):
        fresh(lower={"bogus": 1})
    too_low = fresh(lower={"aggregate_max_notional_usd": 1.0})
    assert AD.refusals(too_low)                                                     # below the minimum meaningful notional


@pytest.mark.parametrize("k,v", [("aggregate_max_notional_usd", True), ("aggregate_max_notional_usd", float("nan")), ("aggregate_max_notional_usd", "50"),
                                 ("max_active_groups", 1.5), ("max_active_groups", True), ("max_leverage", 0.5), ("max_unhedged_usd", 0), ("max_loss_usd", -1)])
def test_bound_types_are_strict(k, v):
    m = fresh()
    m["bounds"][k] = v
    try:
        assert AD.refusals(resealed(m))
    except ValueError:                                                              # NaN cannot even be digested: still a refusal path
        assert AD.validate(m, env={})["valid"] is False


@pytest.mark.parametrize("syms", [["*"], ["ALL"], [], ["btcusdt"], ["BTCUSDT", "BTCUSDT"], ["BTC*"], ["A1", "B2"], ["BTCUSDT", None],
                                  ["AAAUSDT", "BBBUSDT", "CCCUSDT", "DDDUSDT", "EEEUSDT", "FFFUSDT"]])
def test_symbol_allowlist_is_explicit_and_bounded(syms):
    m = fresh()
    m["symbol_allowlist"] = syms
    assert AD.refusals(resealed(m)) and AD.validate(resealed(m), env={})["valid"] is False


def test_strategy_allowlist_is_pinned_and_closed():
    for bad in ([], [{"strategy": "x", "candidate_id": "c", "spec_digest": DIGEST}], [{"strategy": "carry_one", "candidate_id": "cand_1", "spec_digest": "zz"}],
                [{"strategy": "carry_one", "candidate_id": "cand_1", "spec_digest": DIGEST, "extra": 1}], "ALL"):
        m = fresh()
        m["strategy_allowlist"] = bad
        assert AD.refusals(resealed(m))


def test_credential_names_must_be_dedicated_testnet_names():
    for bad in ("BYBIT_API_KEY", "BYBIT_API_SECRET", "BYBIT_MAINNET_KEY", "MY_KEY_VAR", "BYBIT_TESTNET_LIVE_KEY", "bybit_testnet_key", "BYBIT_TESTNET_EU_KEY"):
        m = fresh()
        m["credential_env"]["api_key"] = bad
        assert AD.refusals(resealed(m)), bad
    m = fresh()
    m["credential_env"]["api_secret"] = m["credential_env"]["api_key"]
    assert AD.refusals(resealed(m))
    m = fresh()
    m["credential_env"]["extra"] = "BYBIT_TESTNET_X_KEY"
    assert AD.refusals(resealed(m))


def test_credential_presence_is_reported_without_values():
    m = fresh()
    env = {m["credential_env"]["api_key"]: "SECRETVALUE-key", m["credential_env"]["api_secret"]: "SECRETVALUE-sec"}
    r = AD.validate(m, env=env)
    assert r["credential_presence"] == {"api_key": True, "api_secret": True} and "SECRETVALUE" not in json.dumps(r)
    assert AD.validate(m, env={})["credential_presence"] == {"api_key": False, "api_secret": False}
    assert AD.validate(m)["credential_presence"] == {"api_key": None, "api_secret": None}                   # no mapping given: not checked, and the process environment is never read


@pytest.mark.parametrize("field", ["venue", "mode", "credential_class", "schema"])
def test_pinned_strings(field):
    m = fresh()
    m[field] = "mainnet" if field == "venue" else "x"
    assert AD.refusals(resealed(m))


@pytest.mark.parametrize("field", ["stop_conditions", "recovery_plan", "evidence_capture", "reconciliation", "kill_switch", "preflight_reads", "allowed_sides", "allowed_order_types",
                                   "execution_styles"])
def test_safety_sections_cannot_be_edited(field):
    m = fresh()
    v = m[field]
    if isinstance(v, list):
        m[field] = v[:-1]
    else:
        m[field] = {**v, "x": "y"}
    assert AD.refusals(resealed(m))


def test_unknown_and_missing_fields_and_unsealed_edit_refused():
    m = fresh()
    assert AD.refusals({**m, "extra": 1}) and AD.refusals({k: v for k, v in m.items() if k != "bounds"})
    e = copy.deepcopy(m)
    e["symbol_allowlist"] = ["ETHUSDT"]
    assert any("plan_sha256" in r for r in AD.refusals(e))
    assert AD.refusals([]) == ["manifest is not a JSON object"]


def test_authorisation_syntax():
    for ok in ("NOT_GRANTED", "GRANTED_BY_OWNER:ref-1"):
        m = fresh()
        m["authorisation"] = ok
        assert not AD.refusals(m)
    for bad in ("GRANTED", "granted_by_owner:x", "GRANTED_BY_OWNER:", "GRANTED_BY_OWNER:a b", 1, None):
        m = fresh()
        m["authorisation"] = bad
        assert AD.refusals(m)


def test_implied_policy_is_a_valid_closed_ms_risk_policy():
    m = fresh(symbols=["BTCUSDT"])
    pol = AD.policy_from_manifest(m)
    assert set(pol) == set(ms_risk.POLICY_KEYS) | {"max_daily_loss_usd"} and not ms_risk.policy_refusals(pol) and pol["venue"] == "testnet" and pol["allowed_symbols"] == ["BTCUSDT"]
    with pytest.raises(AD.ManifestError):
        AD.policy_from_manifest({**m, "venue": "mainnet"})


# ---------------------------------------------------------------- kill switch file
def test_kill_switch_file_semantics(tmp_path):
    p = str(tmp_path / "MS_KILL")
    assert AD.kill_switch_engaged(p) == (False, "absent")
    assert AD.engage_kill_switch(p, "operator") is True and AD.engage_kill_switch(p) is False
    assert AD.kill_switch_engaged(p) == (True, "present") and open(p).read() == "operator"
    d = tmp_path / "dir_kill"
    d.mkdir()
    assert AD.kill_switch_engaged(str(d))[0] is True                                 # any kind of file engages
    os.symlink(str(tmp_path / "nowhere"), str(tmp_path / "dangling"))
    assert AD.kill_switch_engaged(str(tmp_path / "dangling"))[0] is True
    assert AD.kill_switch_engaged(str(tmp_path / "a\x00b"))[0] is True               # unreadable path = engaged
    assert AD.kill_switch_engaged(str(tmp_path) + "/" + "x" * 5000)[0] is True              # ENAMETOOLONG is not 'absent'


# ---------------------------------------------------------------- the capability flag
def write_cap(path, m, digest=None, schema=AD.CAPABILITY_SCHEMA, mode=0o600):
    with open(path, "w") as fh:
        json.dump({"schema": schema, "manifest": m, "manifest_plan_sha256": digest or m.get("plan_sha256")}, fh)
    os.chmod(path, mode)


def test_disabled_by_default_and_the_default_path_is_not_created_by_anything_here():
    assert AD.enabled() is False and AD.capability_status()["reasons"] == ["CAPABILITY_FILE_ABSENT"]
    assert not os.path.exists(AD.DEFAULT_CAPABILITY_PATH)
    assert AD.enabled(None) is False and AD.enabled("/nonexistent/x.json") is False


def test_enabled_only_with_owner_file_granted_manifest_and_signed_digest(tmp_path):
    p = str(tmp_path / "cap.json")
    m = granted(fresh())
    write_cap(p, m)
    assert AD.enabled(p) is True                                                     # positive control: the only way to True
    write_cap(p, fresh())
    assert AD.capability_status(p)["reasons"] == ["MANIFEST_NOT_GRANTED"]
    write_cap(p, m, digest="b" * 64)
    assert AD.capability_status(p)["reasons"] == ["DIGEST_NOT_SIGNED_OFF"]
    edited = copy.deepcopy(m)
    edited["bounds"]["max_leverage"] = 2.0                                           # edited after sign-off, digest stale
    write_cap(p, edited, digest=m["plan_sha256"])
    assert AD.enabled(p) is False
    raised = copy.deepcopy(m)
    raised["bounds"]["aggregate_max_notional_usd"] = 500.0
    write_cap(p, resealed(granted(raised)))
    assert AD.enabled(p) is False                                                    # sealed but above the ceiling: refused
    write_cap(p, m, schema="x")
    assert AD.enabled(p) is False
    open(p, "w").write("{not json")
    assert AD.enabled(p) is False
    open(p, "w").write('{"schema": "%s", "schema": "%s"}' % (AD.CAPABILITY_SCHEMA, AD.CAPABILITY_SCHEMA))
    assert AD.enabled(p) is False                                                    # duplicate keys
    open(p, "w").write(json.dumps({"schema": AD.CAPABILITY_SCHEMA, "manifest": m, "manifest_plan_sha256": m["plan_sha256"], "extra": 1}))
    assert AD.enabled(p) is False


def test_symlinked_capability_file_is_not_an_owner_file(tmp_path):
    real = str(tmp_path / "real.json")
    write_cap(real, granted(fresh()))
    link = str(tmp_path / "link.json")
    os.symlink(real, link)
    assert AD.enabled(real) is True and AD.enabled(link) is False


@pytest.mark.parametrize("mode", [0o644, 0o660, 0o666, 0o640, 0o602, 0o620, 0o700, 0o4600, 0o1600])
def test_capability_file_must_be_owner_only(tmp_path, mode):
    p = str(tmp_path / "cap.json")
    write_cap(p, granted(fresh()), mode=0o600)
    assert AD.enabled(p) is True                                                     # positive control: 0600, ours
    os.chmod(p, mode)
    st = AD.capability_status(p)
    assert st["enabled"] is False and st["reasons"] == ["CAPABILITY_FILE_MODE_NOT_OWNER_ONLY_0600"]


def test_capability_file_read_only_for_the_owner_is_fine_and_a_foreign_owner_is_refused(tmp_path, monkeypatch):
    p = str(tmp_path / "cap.json")
    write_cap(p, granted(fresh()), mode=0o400)
    assert AD.enabled(p) is True
    me = os.getuid()
    monkeypatch.setattr(AD.os, "getuid", lambda: me + 1)                    # a file that is not ours
    st = AD.capability_status(p)
    assert st["enabled"] is False and st["reasons"] == ["CAPABILITY_FILE_NOT_OWNED_BY_CURRENT_USER"]


def test_the_capability_file_is_documented_as_self_attested():
    doc = AD.capability_status.__doc__
    assert "SELF-ATTESTED" in doc and "not a signature" in doc and "0600" in doc


# ---------------------------------------------------------------- nothing can be sent
FORBIDDEN_IMPORT_ROOTS = {"requests", "urllib", "urllib3", "http", "socket", "ssl", "websocket", "websockets", "aiohttp", "httpx", "bybit_connection", "carry_broker",
                          "carry_engine", "entry_authority", "main", "subprocess", "asyncio", "ftplib", "smtplib", "telnetlib", "xmlrpc", "backtest", "simulator"}
SEND_WORDS = ("place_order", "create_order", "submit", "send_order", "post(", "_request", "BybitClient", "RequestsTransport", "set_leverage", "cancel_order")


def test_module_imports_no_transport_client_or_network_library():
    tree = ast.parse(open(TOOL).read())
    roots = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            roots |= {a.name.split(".")[0] for a in n.names}
        elif isinstance(n, ast.ImportFrom) and n.module:
            roots.add(n.module.split(".")[0])
    assert not (roots & FORBIDDEN_IMPORT_ROOTS), roots & FORBIDDEN_IMPORT_ROOTS
    assert roots <= {"argparse", "hashlib", "json", "math", "os", "re", "sys", "typing", "__future__", "ms_risk", "shadow", "testnet_action_manifest", "runtime_capability"}


def test_module_has_no_send_like_names_or_calls():
    src = open(TOOL).read()
    tree = ast.parse(src)
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} | {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)} | \
            {n.name for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.ClassDef))}
    for w in ("place_order", "create_order", "send_order", "submit_order", "BybitClient", "RequestsTransport", "request", "urlopen", "connect", "sendall", "send"):
        assert w not in names, w
    public = [n for n in dir(AD) if not n.startswith("_") and callable(getattr(AD, n))]
    assert not [n for n in public if any(w in n.lower() for w in ("send", "place", "submit", "order", "trade", "execute"))]


def test_every_public_function_runs_with_the_network_and_subprocesses_forbidden(monkeypatch, tmp_path):
    def boom(*a, **k):
        raise AssertionError("network / process attempted")
    monkeypatch.setattr(socket, "socket", boom)
    monkeypatch.setattr(socket, "create_connection", boom)
    monkeypatch.setattr(socket, "getaddrinfo", boom)
    monkeypatch.setattr(subprocess, "Popen", boom)
    m = fresh()
    AD.refusals(m)
    AD.validate(m, env={})
    AD.policy_from_manifest(m)
    AD.capability_status(str(tmp_path / "none.json"))
    AD.enabled()
    AD.kill_switch_engaged(str(tmp_path / "k"))
    write_cap(str(tmp_path / "c.json"), granted(m))
    assert AD.enabled(str(tmp_path / "c.json")) is True                              # even ENABLED, nothing is sent: the module has no sender
    assert AD.main(["status"]) == 0


def test_cli_generate_validate_status(tmp_path):
    out = subprocess.run([sys.executable, TOOL, "generate", "--candidate-id", "cand_1", "--spec-digest", DIGEST, "--symbols", "BTCUSDT,ETHUSDT"], capture_output=True, text=True,
                         cwd=BOT, timeout=60)
    assert out.returncode == 0, out.stderr
    m = json.loads(out.stdout)
    assert m["authorisation"] == "NOT_GRANTED"
    f = tmp_path / "m.json"
    f.write_text(out.stdout)
    v = subprocess.run([sys.executable, TOOL, "validate", str(f)], capture_output=True, text=True, cwd=BOT, timeout=60)
    assert v.returncode == 0 and json.loads(v.stdout)["valid"] is True
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({**m, "venue": "mainnet"}))
    assert subprocess.run([sys.executable, TOOL, "validate", str(bad)], capture_output=True, text=True, cwd=BOT, timeout=60).returncode == 1
    st = subprocess.run([sys.executable, TOOL, "status"], capture_output=True, text=True, cwd=BOT, timeout=60)
    assert st.returncode == 0 and json.loads(st.stdout)["enabled"] is False


def test_module_reads_no_environment_flag_and_names_no_drill_switch():
    body = open(TOOL).read().split('"""', 2)[2]
    for w in ("CARRY_DRILL_ENABLED", "LIVE_TRADING_ACK", "PAPER_TRADING", "USE_TESTNET", "getenv", "os.environ"):
        assert w not in body, w


def test_lowering_the_aggregate_alone_leaves_net_unhedged_and_gross_above_it_which_is_refused():
    only_agg = fresh(lower={"aggregate_max_notional_usd": 20.0})
    r = AD.refusals(only_agg)
    assert any("max_net_exposure_usd" in x for x in r) and any("max_unhedged_usd" in x for x in r)
    r2 = AD.refusals(fresh(lower={"aggregate_max_notional_usd": 20.0, "max_net_exposure_usd": 20.0, "max_unhedged_usd": 20.0}))
    assert any("max_gross_exposure_usd" in x for x in r2)
    assert not AD.refusals(fresh(lower={"aggregate_max_notional_usd": 20.0, "max_net_exposure_usd": 20.0, "max_unhedged_usd": 20.0, "max_gross_exposure_usd": 40.0}))


# ---- session credential names (testnet execution path) --------------------------------------------------------------------------------------------------------
def test_generate_can_name_the_session_tools_dedicated_variables_and_the_names_are_in_the_digest():
    ce = AD.SESSION_CREDENTIAL_ENV
    assert list(ce.values()) == ["BYBIT_TESTNET_API_KEY", "BYBIT_TESTNET_API_" + "SECRET"]
    default = AD.generate(candidate_id="cand_1", spec_digest="a" * 64, symbols=["BTCUSDT"])
    named = AD.generate(candidate_id="cand_1", spec_digest="a" * 64, symbols=["BTCUSDT"], credential_env=ce)
    assert AD.refusals(named) == [] and named["credential_env"] == ce and named["authorisation"] == "NOT_GRANTED"
    assert named["plan_sha256"] != default["plan_sha256"] and default["credential_env"]["api_key"] == "BYBIT_TESTNET_DEDICATED_API_KEY"      # default unchanged


def _pair(k, v):
    return dict(zip(("api_key", "api_secret"), (k, v)))


@pytest.mark.parametrize("ce", [_pair("BYBIT_API_KEY", "BYBIT_API_" + "SECRET"), _pair("BYBIT_MAINNET_API_KEY", "BYBIT_TESTNET_API_" + "SECRET"),
                                _pair("BYBIT_TESTNET_API_KEY", "BYBIT_TESTNET_API_KEY")])
def test_a_generic_or_mainnet_named_or_duplicate_credential_variable_is_still_refused(ce):
    m = AD.generate(candidate_id="cand_1", spec_digest="a" * 64, symbols=["BTCUSDT"], credential_env=ce)
    assert AD.refusals(m)


def test_the_cli_flag_names_the_session_variables_and_the_module_still_sends_nothing(capsys):
    assert AD.main(["generate", "--candidate-id", "cand_1", "--spec-digest", "a" * 64, "--symbols", "BTCUSDT", "--session-credentials"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["credential_env"] == AD.SESSION_CREDENTIAL_ENV and out["authorisation"] == "NOT_GRANTED"


# ---- one active group (red team): the default manifest and the ceiling are 1
def test_the_default_manifest_allows_one_active_group_and_more_is_refused():
    m = AD.generate(candidate_id="cand_1", spec_digest="a" * 64, symbols=["BTCUSDT"])
    assert m["bounds"]["max_active_groups"] == 1 and AD.refusals(m) == []
    assert AD.policy_from_manifest(m)["max_active_groups"] == 1
    for n in (2, 3):
        bad = dict(m, bounds=dict(m["bounds"], max_active_groups=n))
        bad["plan_sha256"] = AD.plan_digest(bad)
        assert any("max_active_groups" in r for r in AD.refusals(bad)), n
    assert AD.refusals(AD.generate(candidate_id="cand_1", spec_digest="a" * 64, symbols=["BTCUSDT"], lower={"max_active_groups": 2}))


def test_the_policy_carries_the_manifest_loss_limit_as_a_hard_risk_limit():
    m = AD.generate(candidate_id="cand_1", spec_digest="a" * 64, symbols=["BTCUSDT"])
    pol = AD.policy_from_manifest(m)
    assert pol["max_daily_loss_usd"] == m["bounds"]["max_loss_usd"] and ms_risk.policy_refusals(pol) == []
