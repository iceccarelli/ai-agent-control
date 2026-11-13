"""Offline proof of the owner testnet session tool (preflight -> plan -> execute -> reconcile -> status) against the in-process FAKE Bybit testnet. Nothing here touches a network
or a real credential; ``execute`` is only ever driven against the fake."""
from __future__ import annotations

import json
import os
import stat
import sys
from decimal import Decimal as D

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "tools"))

import ms_authority as MA  # noqa: E402
import ms_testnet_adapter as AD  # noqa: E402
import ms_testnet_session as S  # noqa: E402
import ms_testnet_executor as TE  # noqa: E402
import ms_testnet_transport as TT  # noqa: E402
import ms_types as T  # noqa: E402
from ms_fake_bybit_testnet import FAKE_KEY, FAKE_SECRET, SimulatedCrash  # noqa: E402
from ms_testnet_world import World, begin  # noqa: E402

CREATE = "/v5/order/create"
GENERIC_CANARY = "GENERIC_MAINNET_LOOKING_KEY_zzzz9999"


@pytest.fixture
def w(tmp_path, monkeypatch):
    return World(tmp_path, monkeypatch)


class HeaderSpy:
    """Wraps the fake: records which API key each request was signed with (the fake itself never keeps headers)."""

    def __init__(self, inner):
        self.inner, self.keys = inner, []

    def request(self, method, url, *, headers, params=None, body=None, timeout=10.0):
        self.keys.append({k.lower(): v for k, v in headers.items()}.get("x-bapi-api-key"))
        return self.inner.request(method, url, headers=headers, params=params, body=body, timeout=timeout)


def refused_without_network(w, argv, **kw):
    w.fake.requests.clear()
    code = w.run(*argv, **kw)
    assert code == 2, (code, w.text())
    assert w.fake.requests == [], "a refused command made a network call"
    return w.text()


# ---------------------------------------------------------------- status / the NOT_RUN record
def test_status_with_nothing_run_is_a_valid_not_run_record_and_sends_nothing(w):
    assert w.run("status") == 0
    rec = w.record()
    assert S.record_refusals(rec) == [] and w.fake.requests == []
    assert rec["schema"] == "testnet_session/v1" and rec["venue"] == "Testnet" and rec["endpoint_host"] == "api-testnet.bybit.com"
    assert rec["identity"]["mode"] == "TESTNET" and rec["preflight"] == {"status": "NOT_RUN", "checked_ms": None, "checks": []}
    assert rec["plan"] is None and rec["confirmation"] == {"state": "NOT_REQUESTED", "plan_digest_confirmed": None}
    assert rec["orders"] == [] and rec["executions"] == [] and rec["final_position"] is None and rec["flat_verified"] is None and rec["basis"] == "NOT_RUN"
    assert rec["net"] == {"fees_usd": None, "funding_usd": None, "price_pnl_usd": None, "note": "TESTNET, not owner profit"}
    assert rec["credential_state"] == {"dedicated_key_present": True, "dedicated_secret_present": True, "generic_credentials_ignored": True}
    assert stat.S_IMODE(os.stat(os.path.join(w.state_dir, "testnet_session.json")).st_mode) == 0o600


def test_record_validator_rejects_invented_evidence_and_wrong_shapes(w):
    w.run("status")
    rec = w.record()
    bad = dict(rec, orders=[{"link_id": "x"}])
    assert "EVIDENCE_WITHOUT_BASIS" in S.record_refusals(bad)
    assert S.record_refusals(dict(rec, venue="mainnet")) and S.record_refusals(dict(rec, extra=1)) and S.record_refusals({}) and S.record_refusals(dict(rec, basis="SIMULATED"))
    assert S.record_refusals(dict(rec, credential_state=dict(rec["credential_state"], generic_credentials_ignored=False)))
    assert S.record_refusals(dict(rec, identity=dict(rec["identity"], mode="PAPER")))


# ---------------------------------------------------------------- preflight (READ-ONLY)
def test_preflight_passes_against_a_healthy_fake_and_is_strictly_read_only(w):
    assert w.run("preflight") == 0, w.text()
    assert all(r[0] == "GET" for r in w.fake.requests), "preflight sent a non-GET"
    paths = {r[1] for r in w.fake.requests}
    assert {"/v5/user/query-api", "/v5/account/wallet-balance", "/v5/position/list", "/v5/market/instruments-info", "/v5/market/orderbook", "/v5/order/realtime", "/v5/market/time"} <= paths
    rec = w.record()
    assert rec["preflight"]["status"] == "PASS" and S.record_refusals(rec) == []
    names = {c["name"] for c in rec["preflight"]["checks"]}
    assert {"env_presence", "owner_capability_file", "manifest_credential_names", "kill_switch", "endpoint_identity", "server_time_skew", "key_permissions", "account_mode",
            "position_mode_one_way", "wallet_balance", "instrument_filters", "orderbook_ticker", "flat_and_no_open_orders"} <= names
    assert all(c["ok"] for c in rec["preflight"]["checks"])
    assert stat.S_IMODE(os.stat(os.path.join(w.state_dir, "preflight.json")).st_mode) == 0o600


def test_preflight_without_the_dedicated_credentials_fails_with_instructions_and_no_network(w):
    for env in ({}, {"BYBIT_API_KEY": GENERIC_CANARY, "BYBIT_API_SECRET": GENERIC_CANARY}, {TT.KEY_ENV: FAKE_KEY}):
        w.fake.requests.clear()
        assert w.run("preflight", env=env) == 1
        assert w.fake.requests == []
        assert TT.KEY_ENV in w.text() and TT.SECRET_ENV in w.text() and "Codespaces" in w.text()
        rec = w.record()
        assert rec["preflight"]["status"] == "FAIL" and rec["credential_state"]["generic_credentials_ignored"] is True
        assert GENERIC_CANARY not in w.text() and FAKE_KEY not in w.text()
    assert w.record()["credential_state"] == {"dedicated_key_present": True, "dedicated_secret_present": False, "generic_credentials_ignored": True}


def _fails(w, check):
    assert w.run("preflight") == 1
    rec = w.record()
    assert rec["preflight"]["status"] == "FAIL"
    bad = [c for c in rec["preflight"]["checks"] if not c["ok"]]
    assert any(c["name"] == check for c in bad), bad
    assert all(r[0] == "GET" for r in w.fake.requests)


def test_preflight_refuses_a_key_with_withdraw_or_transfer_permission(w):
    w.fake.api_perms["permissions"]["Wallet"] = ["AccountTransfer", "Withdraw"]
    _fails(w, "key_permissions")


def test_preflight_refuses_a_read_only_key(w):
    w.fake.api_perms["readOnly"] = 1
    _fails(w, "key_permissions")


def test_preflight_requires_flat_and_empty(w):
    w.fake.inject_position("BTCUSDT", -0.1)
    _fails(w, "flat_and_no_open_orders")


def test_preflight_refuses_an_open_order(w):
    w.fake.requests.clear()
    from ms_fake_bybit_testnet import FakeBybitTestnet
    c, _ = TT.build_client(TT.TestnetCredentials.from_mapping(w.env), w.fake)
    w.fake.wallet["BTC"] = w.fake.wallet["BTC"]
    w.fake._create({"category": "linear", "symbol": "BTCUSDT", "side": "Sell", "orderType": "Limit", "qty": "0.001", "price": "70000.0", "orderLinkId": "stray-1"})
    _fails(w, "flat_and_no_open_orders")


def test_preflight_refuses_hedge_mode_and_unreadable_reads(w):
    w.fake.position_idx_hedge = True
    _fails(w, "position_mode_one_way")


def test_preflight_refuses_a_clock_skew_beyond_the_limit(w):
    w.fake.skew_ms = 20_000
    _fails(w, "server_time_skew")


def test_preflight_refuses_when_the_kill_switch_file_is_present(w):
    os.makedirs(w.state_dir, exist_ok=True)
    open(os.path.join(w.state_dir, "MS_KILL"), "w").write("x")
    assert w.run("preflight") == 1 and not [c for c in w.record()["preflight"]["checks"] if c["name"] == "kill_switch" and c["ok"]]
    assert w.fake.requests == []                                                    # the cheap local refusals come first: no network when they already fail


def test_preflight_without_the_owner_capability_file_does_not_touch_the_network(w):
    os.unlink(w.cap_path)
    assert w.run("preflight") == 1 and w.fake.requests == []
    assert any(c["name"] == "owner_capability_file" and not c["ok"] for c in w.record()["preflight"]["checks"])


def test_preflight_requires_the_manifest_to_name_the_dedicated_session_variables(w):
    default = AD.generate(candidate_id="cand_1", spec_digest="a" * 64, symbols=["BTCUSDT"])           # names BYBIT_TESTNET_DEDICATED_API_*: not what this tool reads
    w.write_capability(default)
    assert w.run("preflight") == 1 and w.fake.requests == []
    assert any(c["name"] == "manifest_credential_names" and not c["ok"] for c in w.record()["preflight"]["checks"])


@pytest.mark.parametrize("path", ["/v5/market/instruments-info", "/v5/position/list", "/v5/account/wallet-balance", "/v5/user/query-api"])
def test_preflight_survives_an_unreadable_endpoint_as_a_fail_not_a_crash(w, path):
    w.fake.inject(path, "http500", times=99)
    assert w.run("preflight") == 1
    assert w.record()["preflight"]["status"] == "FAIL"


# ---------------------------------------------------------------- plan
def test_plan_builds_a_complete_digested_plan_from_the_manifest_and_the_preflight(w):
    d = w.ready()
    plan = w.plan_doc()
    assert plan["plan_digest"] == d == S.digest_of(plan, ("plan_digest",)) and S.plan_refusals(plan) == []
    assert plan["manifest_plan_sha256"] == w.manifest["plan_sha256"] and plan["venue"] == "Testnet" and plan["endpoint_host"] == "api-testnet.bybit.com"
    assert [l["leg_id"] for l in plan["legs"]] == ["p", "s"] and [l["side"] for l in plan["legs"]] == ["Sell", "Buy"] and plan["style"] == "TAKER"
    assert plan["notional_usd"] <= 500 and plan["aggregate_ceiling_usd"] <= 500 and plan["leverage"] <= plan["bounds"]["max_leverage"]
    assert plan["bounds"]["one_active_group"] is True and plan["bounds"]["max_lifecycle_hours"] <= 9 and plan["exit_plan"] and plan["policy"]["venue"] == "testnet"
    assert plan["preflight_digest"] and "PLAN DIGEST: " + d in w.text()
    rec = w.record()
    assert rec["plan"]["digest"] == d and rec["plan"]["manifest_digest"] == w.manifest["plan_sha256"] and rec["plan"]["notional_usd"] == plan["notional_usd"]
    assert {k for k in rec["plan"]["legs"][0]} == {"symbol", "side", "order_type", "qty", "price", "notional_usd"} and S.record_refusals(rec) == []
    assert not any(r[0] == "POST" for r in w.fake.requests)


def test_plan_sends_nothing_at_all(w):
    assert w.run("preflight") == 0
    w.fake.requests.clear()
    assert w.run("plan", "--symbol", "BTCUSDT", "--notional-usd", "90") == 0
    assert w.fake.requests == []


@pytest.mark.parametrize("args,why", [
    (("--symbol", "BTCUSDT", "--notional-usd", "500.01"), "NOTIONAL_ABOVE_AGGREGATE_CEILING"),
    (("--symbol", "BTCUSDT", "--notional-usd", "0"), "NOTIONAL_ABOVE_AGGREGATE_CEILING"),
    (("--symbol", "SOLUSDT", "--notional-usd", "50"), "SYMBOL_NOT_IN_ALLOW_LIST"),
    (("--symbol", "BTCUSDT", "--notional-usd", "90", "--leverage", "4"), "LEVERAGE_ABOVE_MANIFEST"),
    (("--symbol", "BTCUSDT", "--notional-usd", "90", "--max-lifecycle-hours", "9.5"), "LIFECYCLE_ABOVE_BOUND"),
    (("--symbol", "BTCUSDT", "--notional-usd", "90", "--hold-seconds", "99999"), "TIMING_EXCEEDS_LIFECYCLE"),
    (("--symbol", "BTCUSDT", "--notional-usd", "40"), "QTY_BELOW_MIN_QTY"),
])
def test_plan_refuses_anything_beyond_the_bounds_or_the_instrument_rules(w, args, why):
    assert w.run("preflight") == 0
    assert w.run("plan", *args) == 2 and why in w.text()
    assert not os.path.exists(os.path.join(w.state_dir, "plan.json"))


def test_plan_bounds_can_be_lowered_by_the_manifest_never_raised(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch, lower={"aggregate_max_notional_usd": 70.0, "max_net_exposure_usd": 70.0, "max_unhedged_usd": 70.0,
                                                          "max_gross_exposure_usd": 140.0, "max_notional_per_symbol_usd": 140.0, "max_leverage": 2.0})
    assert w.run("preflight") == 0
    assert w.run("plan", "--symbol", "BTCUSDT", "--notional-usd", "90") == 2 and "ABOVE_AGGREGATE_CEILING" in w.text()
    assert w.run("plan", "--symbol", "BTCUSDT", "--notional-usd", "70", "--leverage", "3") == 2 and "LEVERAGE_ABOVE_MANIFEST" in w.text()
    assert AD.refusals(dict(w.manifest, bounds=dict(w.manifest["bounds"], aggregate_max_notional_usd=101.0)))          # the manifest itself cannot exceed $100


def test_plan_needs_a_passing_fresh_preflight_for_the_same_manifest(w):
    assert w.run("plan", "--symbol", "BTCUSDT", "--notional-usd", "90") == 2 and "NO_PASSING_PREFLIGHT" in w.text()
    assert w.run("preflight") == 0
    w.clock.t += 16 * 60_000
    assert w.run("plan", "--symbol", "BTCUSDT", "--notional-usd", "90") == 2 and "PREFLIGHT_STALE" in w.text()
    w.run("preflight")
    other = AD.generate(candidate_id="cand_2", spec_digest="b" * 64, symbols=["BTCUSDT"], credential_env={"api_key": TT.KEY_ENV, "api_secret": TT.SECRET_ENV})
    w.write_capability(other)
    assert w.run("plan", "--symbol", "BTCUSDT", "--notional-usd", "90") == 2 and "PREFLIGHT_FOR_ANOTHER_MANIFEST" in w.text()


def test_the_plan_digest_changes_with_any_edit(w):
    w.ready()
    plan = w.plan_doc()
    for k, v in (("notional_usd", 1.0), ("symbol", "ETHUSDT"), ("leverage", 2), ("manifest_plan_sha256", "0" * 64)):
        assert S.plan_refusals(dict(plan, **{k: v})) == ["PLAN_DIGEST_MISMATCH"]
    legs = [dict(l) for l in plan["legs"]]
    legs[0]["qty"] = "0.002"
    assert S.plan_refusals(dict(plan, legs=legs)) == ["PLAN_DIGEST_MISMATCH"]


# ---------------------------------------------------------------- execute: the confirmation gate (EVERY refusal is exit 2 with no network call)
def _ready(w):
    d = w.ready()
    w.fake.requests.clear()
    return d


def test_execute_without_the_confirmation_flag_is_refused_without_a_network_call(w):
    d = _ready(w)
    assert "CONFIRM_FLAG_DOES_NOT_EMBED_THE_PLAN_DIGEST" in refused_without_network(w, ["execute"], typed=d)


@pytest.mark.parametrize("flag", ["", "0" * 64, "abc", None])
def test_execute_with_a_wrong_confirmation_flag_is_refused(w, flag):
    d = _ready(w)
    argv = ["execute"] + ([] if flag is None else ["--confirm-plan-digest", flag])
    assert "CONFIRM_FLAG" in refused_without_network(w, argv, typed=d)


@pytest.mark.parametrize("typed", ["", "yes", "0" * 64, " "])
def test_execute_with_a_wrong_typed_confirmation_is_refused(w, typed):
    d = _ready(w)
    assert "TYPED_CONFIRMATION_DOES_NOT_MATCH" in refused_without_network(w, ["execute", "--confirm-plan-digest", d], typed=typed)


def test_the_flag_alone_or_the_typed_digest_alone_is_not_enough(w):
    d = _ready(w)
    refused_without_network(w, ["execute", "--confirm-plan-digest", d], typed="no")
    refused_without_network(w, ["execute"], typed=d)


def test_execute_refuses_without_the_owner_capability_file(w):
    d = _ready(w)
    os.unlink(w.cap_path)
    assert "OWNER_CAPABILITY_FILE" in refused_without_network(w, ["execute", "--confirm-plan-digest", d], typed=d)


def test_the_capability_file_must_be_owner_only_0600(w):
    d = _ready(w)
    os.chmod(w.cap_path, 0o644)
    assert "CAPABILITY_FILE_MODE_NOT_OWNER_ONLY_0600" in refused_without_network(w, ["execute", "--confirm-plan-digest", d], typed=d)


@pytest.mark.parametrize("env", [{}, {"BYBIT_API_KEY": GENERIC_CANARY, "BYBIT_API_SECRET": GENERIC_CANARY}, {TT.KEY_ENV: FAKE_KEY}])
def test_execute_refuses_without_the_dedicated_credentials_and_never_falls_back_to_generic_ones(w, env):
    d = _ready(w)
    assert "DEDICATED_TESTNET_CREDENTIALS_MISSING" in refused_without_network(w, ["execute", "--confirm-plan-digest", d], env=env, typed=d)


def test_execute_refuses_a_missing_stale_or_failed_preflight(w):
    d = _ready(w)
    w.clock.t += 16 * 60_000
    assert "PREFLIGHT_STALE" in refused_without_network(w, ["execute", "--confirm-plan-digest", d], typed=d)
    os.unlink(os.path.join(w.state_dir, "preflight.json"))
    assert "NO_PASSING_PREFLIGHT" in refused_without_network(w, ["execute", "--confirm-plan-digest", d], typed=d)


def test_execute_refuses_a_preflight_that_is_not_the_one_the_plan_was_built_from(w):
    d = _ready(w)
    w.clock.t += 1000
    assert w.run("preflight") == 0                                                  # a newer, different capture
    w.fake.requests.clear()
    assert "PREFLIGHT_IS_NOT_THE_ONE" in refused_without_network(w, ["execute", "--confirm-plan-digest", d], typed=d)


def test_execute_refuses_a_tampered_or_missing_or_old_plan(w):
    d = _ready(w)
    p = os.path.join(w.state_dir, "plan.json")
    plan = w.plan_doc()
    plan["notional_usd"] = 99.0
    json.dump(plan, open(p, "w"))
    assert "PLAN_DIGEST_MISMATCH" in refused_without_network(w, ["execute", "--confirm-plan-digest", d], typed=d)
    os.unlink(p)
    assert "PLAN_SCHEMA" in refused_without_network(w, ["execute", "--confirm-plan-digest", d], typed=d)
    d = w.ready()
    w.fake.requests.clear()
    w.clock.t += 121 * 60_000
    assert "PLAN_TOO_OLD" in refused_without_network(w, ["execute", "--confirm-plan-digest", d, "--max-preflight-age-min", "100000"], typed=d)


def test_execute_refuses_a_plan_for_another_manifest(w):
    d = _ready(w)
    other = AD.generate(candidate_id="cand_2", spec_digest="b" * 64, symbols=["BTCUSDT", "ETHUSDT"], credential_env={"api_key": TT.KEY_ENV, "api_secret": TT.SECRET_ENV})
    w.write_capability(other)
    assert "PLAN_IS_FOR_ANOTHER_MANIFEST" in refused_without_network(w, ["execute", "--confirm-plan-digest", d], typed=d)


def test_execute_refuses_while_the_kill_switch_file_exists(w):
    d = _ready(w)
    open(os.path.join(w.state_dir, "MS_KILL"), "w").write("x")
    assert "KILL_SWITCH_ENGAGED" in refused_without_network(w, ["execute", "--confirm-plan-digest", d], typed=d)
    os.unlink(os.path.join(w.state_dir, "MS_KILL"))
    os.mkdir(os.path.join(w.state_dir, "MS_KILL"))                                    # a directory is an engaged switch too
    assert "KILL_SWITCH_ENGAGED" in refused_without_network(w, ["execute", "--confirm-plan-digest", d], typed=d)


@pytest.mark.parametrize("flag", ["CI", "PYTEST_CURRENT_TEST"])
def test_execute_refuses_under_ci_or_pytest_unless_the_fake_transport_is_injected_explicitly(w, flag):
    d = _ready(w)
    env = dict(w.env, **{flag: "1"})
    w.fake.requests.clear()
    code = w.run("execute", "--confirm-plan-digest", d, env=env, inner=None, typed=d)
    assert code == 2 and "INJECTED_TRANSPORT" in w.text() and w.fake.requests == []
    assert w.run("reconcile", env=env, inner=None) in (0, 2)                          # nothing to reconcile / refused: never a real transport
    assert w.fake.requests == []


def test_execute_never_builds_the_real_transport_in_this_process(w, monkeypatch):
    """Control for the CI refusal: if the gate let it through the real inner transport would be built; make that an error."""
    monkeypatch.setattr(TT, "NoRedirectInner", lambda: (_ for _ in ()).throw(AssertionError("real transport built")))
    d = _ready(w)
    assert w.run("execute", "--confirm-plan-digest", d, env=dict(w.env, CI="1"), inner=None, typed=d) == 2


# ---------------------------------------------------------------- execute: the full lifecycle against the fake
def test_execute_runs_the_full_lifecycle_and_verifies_flat_from_the_venue(w):
    spy = HeaderSpy(w.fake)
    env = dict(w.env, BYBIT_API_KEY=GENERIC_CANARY, BYBIT_API_SECRET=GENERIC_CANARY)
    d = _ready(w)
    assert w.run("execute", "--confirm-plan-digest", d, env=env, inner=spy, typed=d) == 0, w.text()
    rec = w.record()
    assert S.record_refusals(rec) == []
    assert rec["confirmation"] == {"state": "CONFIRMED", "plan_digest_confirmed": d} and rec["plan"]["digest"] == d
    assert rec["flat_verified"] is True and rec["basis"] == "EXCHANGE_REPORTED" and rec["venue"] == "Testnet"
    assert rec["final_position"]["BTCUSDT"] == 0 and rec["final_position"]["SPOT:BTCUSDT"] == pytest.approx(0.0)
    assert len(rec["orders"]) == 4 and {o["status"] for o in rec["orders"]} == {"Filled"}
    ex = rec["executions"]
    assert len(ex) == 4 and len({e["exec_id"] for e in ex}) == 4 and all(e["link_id"].startswith(w.plan_doc()["group"]["group_id"]) for e in ex)
    assert rec["net"]["fees_usd"] == pytest.approx(sum(float(e["execFee"]) for e in w.fake.execs)) and rec["net"]["funding_usd"] is None
    assert rec["net"]["price_pnl_usd"] is not None and rec["net"]["note"] == "TESTNET, not owner profit"
    # the VENUE agrees: no position, no open order, every link id created exactly once
    assert all(p["size"] == 0 for p in w.fake.positions.values()) and not [o for o in w.fake.orders.values() if o["status"] in ("New", "PartiallyFilled")]
    assert set(w.fake.create_calls.values()) == {1} and len(w.fake.create_calls) == 4
    reduce_only = [json.loads(r[3]) for r in w.requests_to(CREATE) if '"reduceOnly":true' in r[3]]
    assert len(reduce_only) == 1 and reduce_only[0]["category"] == "linear"
    # only the dedicated key ever signed anything; the generic pair was ignored
    assert set(spy.keys) <= {FAKE_KEY, None} and GENERIC_CANARY not in spy.keys
    # nothing secret reached the printed output or any file in the state dir
    blob = w.text()
    for root, _d, files in os.walk(w.state_dir):
        for f in files:
            blob += open(os.path.join(root, f), errors="replace").read()
    for secret in (FAKE_KEY, FAKE_SECRET, GENERIC_CANARY):
        assert secret not in blob


def test_execute_is_one_shot_a_second_execute_is_refused_and_reconcile_is_idempotent(w):
    d = _ready(w)
    assert w.execute(d) == 0
    w.fake.requests.clear()
    assert "SESSION_ALREADY_EXECUTED_USE_RECONCILE" in refused_without_network(w, ["execute", "--confirm-plan-digest", d], typed=d)
    n = len(w.requests_to(CREATE))
    assert w.run("reconcile") == 0 and len(w.requests_to(CREATE)) == n
    assert w.record()["flat_verified"] is True


def test_execute_on_a_second_symbol_and_the_aggregate_ceiling_holds_across_symbols(w):
    d = w.ready("ETHUSDT", 61.0)
    assert w.plan_doc()["notional_usd"] == pytest.approx(60.001, abs=0.01)
    assert w.execute(d) == 0 and w.record()["flat_verified"] is True
    assert all(p["size"] == 0 for p in w.fake.positions.values())


def test_a_second_group_cannot_exceed_the_aggregate_ceiling_across_symbols(w):
    w.ready()
    run, ctx, rec = begin(w, submit=False)                                            # group 1 (BTC, ~$60) registered
    gp = run.group_plan
    eth = T.OrderGroupPlan("tnother1", gp.candidate_id, gp.spec_digest, gp.policy_version, w.clock.now(),
                           (T.LegPlan("p", "BTCUSDT", "linear", "Sell", "0.001", "Market", None, False, False, "IOC"),
                            T.LegPlan("s", "BTCUSDT", "spot", "Buy", "0.001", "Limit", "60001.01", False, False, "IOC")), "TAKER", 60000, 60.0)
    d = run.authority.register_group(eth)
    assert not d.approved and any("AGGREGATE_NOTIONAL_ABOVE_CAP" in r or "ACTIVE_GROUPS" in r or "GROSS" in r for r in d.reasons), d.reasons


def test_execute_aborts_before_any_order_if_the_venue_is_not_clean_at_execute_time(w):
    d = _ready(w)
    w.fake.inject_position("BTCUSDT", 0.5)                                            # appeared after the preflight
    assert w.run("execute", "--confirm-plan-digest", d, typed=d) == 1
    assert w.requests_to(CREATE) == [] and "not flat/clean" in w.text()


def test_execute_whose_risk_check_fails_sends_nothing(w):
    d = _ready(w)
    w.fake.wallet["USDT"] = w.fake.wallet["USDT"] * 0 + 1                              # equity collapsed: leverage bound breached
    assert w.run("execute", "--confirm-plan-digest", d, typed=d) == 1
    assert w.requests_to(CREATE) == [] and ("risk refused" in w.text() or "aborted" in w.text())


# ---------------------------------------------------------------- reconcile (restart recovery)
def test_reconcile_without_a_session_is_a_noop_that_leaves_a_valid_record(w):
    assert w.run("reconcile") == 0 and "nothing to reconcile" in w.text() and w.fake.requests == []
    assert S.record_refusals(w.record()) == []


def test_reconcile_recovers_a_session_that_crashed_after_the_exchange_accepted_an_order(w):
    d = _ready(w)
    w.fake.inject(CREATE, "crash_after", match=lambda b: b["orderLinkId"].endswith("-p"))
    with pytest.raises(SimulatedCrash):
        w.run("execute", "--confirm-plan-digest", d, typed=d)
    link = w.plan_doc()["group"]["group_id"] + "-p"
    assert w.fake.create_calls[link] == 1 and w.fake.positions["BTCUSDT"]["size"] != 0       # exposure exists at the venue
    w.fake.requests.clear()
    assert w.run("reconcile") == 1
    assert w.record()["flat_verified"] is False                                              # not flat: the tool says so, from the venue
    assert w.run("reconcile", "--unwind") == 2 and "UNWIND_NEEDS" in w.text()               # an unwind needs the plan digest too
    assert w.run("reconcile", "--unwind", "--confirm-plan-digest", d) == 0, w.text()
    rec = w.record()
    assert rec["flat_verified"] is True and S.record_refusals(rec) == []
    assert w.fake.create_calls[link] == 1                                                    # the accepted order was ADOPTED, never created again
    assert all(p["size"] == 0 for p in w.fake.positions.values())


def test_reconcile_refuses_without_credentials_or_capability_before_any_network_call(w):
    d = _ready(w)
    assert w.execute(d) == 0
    w.fake.requests.clear()
    assert w.run("reconcile", env={}) == 2 and w.fake.requests == []
    os.unlink(w.cap_path)
    assert w.run("reconcile") == 2 and w.fake.requests == []


def test_a_lost_ack_during_execute_is_adopted_and_the_session_still_ends_flat(w):
    d = _ready(w)
    w.fake.inject(CREATE, "lose_response", match=lambda b: b["orderLinkId"].endswith("-s"))
    assert w.run("execute", "--confirm-plan-digest", d, typed=d) == 0, w.text()
    assert set(w.fake.create_calls.values()) == {1} and w.record()["flat_verified"] is True


def test_a_conflicting_position_mid_session_ends_not_verified_flat_and_asks_for_a_human(w):
    real_sleep = w.clock.sleep
    w.ready()

    def sabotage(s):
        w.fake.inject_position("ETHUSDT", -3)                                       # exposure nobody opened appears
        real_sleep(s)
    w.clock.sleep = sabotage
    w.run("plan", "--symbol", "BTCUSDT", "--notional-usd", "90", "--hold-seconds", "30")
    d = w.plan_doc()["plan_digest"]
    code = w.execute(d)
    assert code in (0, 1)
    assert w.requests_to(CREATE) and S.record_refusals(w.record()) == []
    # whatever happened, no ENTRY was sent after the mismatch halted new exposure: every create after the first mismatch is reduce-only
    bodies = [json.loads(r[3]) for r in w.requests_to(CREATE)]
    assert len([b for b in bodies if not b.get("reduceOnly") and not (b["category"] == "spot" and b["side"] == "Sell")]) == 2


# ================================================================================================ red-team regressions (cp-E2): manifest bounds are ENFORCED, not just validated
def test_the_plan_states_one_active_group_and_the_policy_enforces_it(w):
    w.ready()
    plan = w.plan_doc()
    assert plan["bounds"]["one_active_group"] is True and plan["policy"]["max_active_groups"] == 1


def test_max_entries_per_utc_day_is_enforced_across_state_dirs(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch, lower={"max_entries_per_utc_day": 1})
    assert w.execute(w.ready()) == 0
    w.state_dir = str(tmp_path / "state2")                                              # a different session, the same UTC day
    d = w.ready()
    w.fake.requests.clear()
    assert "MAX_ENTRIES_PER_UTC_DAY_REACHED" in refused_without_network(w, ["execute", "--confirm-plan-digest", d], typed=d)
    w.clock.t += 86_400_000                                                             # the next UTC day
    d = w.ready()
    assert w.execute(d) == 0


def test_the_manifest_loss_limit_stops_the_run_and_unwinds_reduce_only(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch, lower={"max_loss_usd": 0.01})                      # the round-trip taker fees alone exceed one cent
    d = w.ready(hold_seconds=30)
    assert w.execute(d) == 0, w.text()
    assert "STOP: LOSS_LIMIT" in w.text() and w.record()["flat_verified"] is True and all(p["size"] == 0 for p in w.fake.positions.values())
    # every order created once, nothing re-entered. The second entry leg is now assessed on a FRESH account read (the first leg's fee is already a loss), so with a one-cent limit the
    # run may stop before it (2 orders: the first entry and its exit) or after it (4); either way it ends flat via reduce-only exits.
    assert set(w.fake.create_calls.values()) == {1} and len(w.fake.create_calls) in (2, 4)


def test_the_loss_limit_does_not_fire_inside_the_bound(w):
    d = w.ready(hold_seconds=30)
    assert w.execute(d) == 0 and "STOP:" not in w.text()


def test_the_lifecycle_bound_is_enforced_during_the_run(w):
    w.ready()
    run, ctx, rec = begin(w, submit=True)
    for _ in range(2):
        w.clock.sleep(1)
        S._step_once(ctx, run, rec)
    assert run.state() == "HEDGED" and run.stopped is None
    run.start_ms = w.clock.now() - run.lifecycle_ms - 1                                 # the session has outlived its bound
    for _ in range(6):
        w.clock.sleep(1)
        S._step_once(ctx, run, rec)
    assert run.stopped == "LIFECYCLE_EXCEEDED" and "STOP: LIFECYCLE_EXCEEDED" in w.text()
    assert run.state() == "CLOSED" and all(p["size"] == 0 for p in w.fake.positions.values())
    assert "LIFECYCLE_EXCEEDED" in run.engine.halts or any("LIFECYCLE" in h for h in run.engine.halts)


def test_the_lifecycle_is_the_lower_of_the_plan_and_the_manifest(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch, lower={"max_lifecycle_hours": 2.0})
    w.run("preflight")
    assert w.run("plan", "--symbol", "BTCUSDT", "--notional-usd", "90", "--max-lifecycle-hours", "3") == 2 and "LIFECYCLE_ABOVE_BOUND" in w.text()
    assert w.run("plan", "--symbol", "BTCUSDT", "--notional-usd", "90", "--max-lifecycle-hours", "1.5") == 0
    run, ctx, rec = begin(w, submit=False)
    assert run.lifecycle_ms == 1.5 * 3_600_000


def test_reconcile_past_the_lifecycle_says_unwind_required(w):
    d = w.ready()
    w.fake.inject(CREATE, "crash_after", match=lambda b: b["orderLinkId"].endswith("-p"))
    with pytest.raises(SimulatedCrash):
        w.run("execute", "--confirm-plan-digest", d, typed=d)
    w.clock.t += 2 * 3_600_000
    w.run("reconcile")
    assert "LIFECYCLE_EXCEEDED" in w.text()


# ---------------------------------------------------------------- the state directory argument (the runbook's S= line passes a literal '~')
def test_a_literal_tilde_state_dir_is_the_home_directory_not_a_directory_named_tilde(tmp_path, monkeypatch):
    home, cwd = tmp_path / "home", tmp_path / "cwd"
    home.mkdir()
    cwd.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(cwd)
    out = []
    assert S.main(["--state-dir", "~/.ai-agent-control/ms_testnet", "status"], env={}, out=out.append) == 0
    assert (home / ".ai-agent-control" / "ms_testnet" / "testnet_session.json").is_file()
    assert os.listdir(cwd) == [], "a literal '~' directory was created in the working directory"


def test_a_state_dir_inside_a_git_work_tree_is_refused_before_anything_is_written(tmp_path):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    out = []
    target = repo / "bot" / "~" / "state"
    assert S.main(["--state-dir", str(target), "status"], env={}, out=out.append) == 2
    assert "STATE_DIR_INSIDE_GIT_WORK_TREE" in "\n".join(out)
    assert not (repo / "bot").exists(), "the refusal must come before the directory is created"


def test_the_session_record_carries_the_git_revision_not_UNKNOWN(w, monkeypatch):
    monkeypatch.setattr(S.MP, "git_revision", lambda *a, **k: "b" * 40)
    assert w.run("status") == 0
    assert w.record()["identity"]["code_revision"] == "b" * 40
    w.ready()
    assert w.record()["identity"]["code_revision"] == "b" * 40

# ---------------------------------------------------------------- the manifest's words describe what the session tool really does
def test_the_manifest_text_names_the_real_kill_file_the_real_entry_bound_and_only_evidence_the_tool_writes(w):
    m = w.manifest
    sc = " | ".join(m["stop_conditions"])
    assert "DRILL_KILL" not in sc and "MS_KILL" in sc, sc
    assert "more than one entry" not in sc and "max_entries_per_utc_day" in sc, sc          # the bound is 4 (and lowerable), not 'one'
    ev = " | ".join(m["evidence_capture"])
    assert "--execution-records" not in ev and "--evidence-path" not in ev and "drill" not in ev.lower(), ev


def test_every_read_the_manifest_declares_is_really_made_by_preflight_and_every_evidence_file_it_names_exists_after_a_session(w):
    assert w.run("preflight") == 0
    made = {r[1] for r in w.fake.requests}
    for r in w.manifest["preflight_reads"]:
        assert r["endpoint"] in made, f"declared but never read: {r['fact']} {r['endpoint']}"
    d = w.ready()
    assert w.execute(d) == 0
    for name in ("testnet_session.json", "plan.json", "preflight.json", "engine_journal.jsonl", "ms_groups.jsonl"):
        assert os.path.isfile(os.path.join(w.state_dir, name)), name
    ev = " | ".join(w.manifest["evidence_capture"])
    for name in ("testnet_session.json", "plan.json", "preflight.json", "engine_journal.jsonl", "ms_groups.jsonl"):
        assert name in ev, f"{name} is written but not named as evidence"


# ---------------------------------------------------------------- the runbook's numbers and commands are the code's
def _runbook():
    with open(os.path.join(os.path.dirname(HERE), "RUNBOOKS.md"), encoding="utf-8") as fh:
        return fh.read()


def test_the_runbook_states_the_real_windows_and_loss_limit(w):
    rb = _runbook()
    assert f"{S.DEFAULT_MAX_PREFLIGHT_AGE_MIN} minutes" in rb and f"{S.MAX_PLAN_AGE_MIN} minutes" in rb
    assert f"${w.manifest['bounds']['max_loss_usd']}" in rb and "totalEquity" in rb
    assert f"({w.manifest['bounds']['max_entries_per_utc_day']})" in rb and "ONE-SHOT" in rb
    assert "SESSION_ALREADY_EXECUTED_USE_RECONCILE" in rb and "PREFLIGHT_IS_NOT_THE_ONE_THE_PLAN_WAS_BUILT_FROM" in rb


def test_the_runbooks_generate_command_and_digest_one_liner_run_and_agree(tmp_path):
    import re
    import shlex
    import subprocess
    rb = _runbook()
    gen = re.search(r"`(python3 tools/ms_testnet_adapter\.py generate --candidate research/candidates/\S+ --symbols \S+ --session-credentials) > manifest\.json`", rb)
    assert gen, "the runbook's generate command moved"
    r = subprocess.run(shlex.split(gen.group(1)), cwd=os.path.dirname(HERE), capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    m = json.loads(r.stdout)
    assert m["strategy_allowlist"][0]["candidate_id"] == "cash_and_carry@spec-1" and m["credential_env"] == {"api_key": TT.KEY_ENV, "api_secret": TT.SECRET_ENV}
    one = re.search(r'`(python3 -c "import json,sys.*?")`', rb)
    assert one, "the runbook's digest one-liner moved"
    cmd = one.group(1)
    r2 = subprocess.run(["bash", "-c", cmd], cwd=os.path.dirname(HERE), capture_output=True, text=True, timeout=60)
    assert r2.returncode == 0, r2.stderr
    assert r2.stdout.strip() == m["strategy_allowlist"][0]["spec_digest"]


def test_a_plan_at_the_new_ceiling_is_not_silently_clamped_to_the_old_100(w):
    """The session tool had its own hard-coded 100 ceiling: a 400 plan must come out as ~400, not refused or clamped."""
    assert S.HARD_CEILING_USD == AD.AGGREGATE_CEILING_USD == 500.0
    assert w.run("preflight") == 0
    assert w.run("plan", "--symbol", "BTCUSDT", "--notional-usd", "400") == 0
    plan = w.plan_doc()
    assert 300.0 < plan["notional_usd"] <= 400.0 and plan["aggregate_ceiling_usd"] == 500.0        # rounded DOWN to the 0.001 BTC lot (about $60 at the fake price)


def test_a_plan_the_wallet_cannot_fund_is_refused_offline_before_any_order(w):
    """Spot buy (notional) + perp initial margin (notional / leverage) + a 2% buffer must fit in the USDT the preflight read."""
    w.fake.wallet["USDT"] = D("500")
    assert w.run("preflight") == 0
    assert w.run("plan", "--symbol", "BTCUSDT", "--notional-usd", "400") != 0
    assert "INSUFFICIENT_USDT_FOR_PLAN" in w.text() and not any(r[0] == "POST" for r in w.fake.requests)
    w.fake.wallet["USDT"] = D("1000")                                              # positive control: 400 + 400/1 + 2% = 816 fits
    assert w.run("preflight") == 0
    assert w.run("plan", "--symbol", "BTCUSDT", "--notional-usd", "400") == 0


def test_an_account_that_already_holds_the_base_coin_can_run_a_plan_at_the_new_ceiling(w):
    """The owner's real account holds ~0.1047 BTC of spot before the session. That holding is not the session's exposure: a 400 plan must pass every notional cap and finish flat."""
    w.fake.wallet["BTC"] = D("0.1")
    d = w.ready("BTCUSDT", 400.0)
    assert 300.0 < w.plan_doc()["notional_usd"] <= 400.0
    rc = w.execute(d)
    assert rc == 0, w.text()[-800:]
    assert w.record()["flat_verified"] is True and w.record()["orders"]


def test_assess_is_read_only_and_reports_the_baselines_and_the_state_the_authority_sees(w):
    """Diagnostic for a risk refusal: GET-only, writes no journal, prints plan vs venue baseline and the account state under each."""
    w.fake.wallet["BTC"] = D("0.1")
    w.ready("BTCUSDT", 400.0)
    w.fake.requests.clear()
    assert w.run("assess") == 0, w.text()
    assert w.fake.requests and all(r[0] == "GET" for r in w.fake.requests)
    assert not os.path.exists(os.path.join(w.state_dir, "journal")) or os.path.getsize(os.path.join(w.state_dir, "journal")) == 0
    out = json.loads(w.text()[w.text().index("{"):])
    assert out["plan_baseline"]["spot:BTCUSDT"] == out["venue_baseline"]["spot:BTCUSDT"] == 0.1
    assert "SPOT:BTCUSDT" not in out["state_with_venue_baseline"]["positions"]                 # the owner's holding is not the session's exposure
    assert out["state_with_no_baseline"]["positions"]["SPOT:BTCUSDT"]["notional_usd"] > 1000    # positive control: without the baseline it would be
    legs = out["risk_per_leg"]
    assert legs and all(v["approved"] for v in legs.values()), legs                          # a 400 plan passes every cap leg by leg (the caps are printed with it)
    assert out["policy_caps"]["max_aggregate_notional_usd"] == 500.0
    assert out["risk_whole_group"]["approved"] is True and out["risk_whole_group"]["limits"]["per_symbol"]["BTCUSDT"] > 0       # the registration-time check, both legs together


def test_a_filled_spot_leg_is_not_counted_twice_when_the_perp_leg_is_assessed_after_the_state_cache_expires(w, monkeypatch):
    """Found on the real Testnet: the engine sends both entry legs in one loop. The spot buy fills at once (the wallet shows it) but is only SETTLED by bookkeeping after the loop, so
    the second leg saw the first leg's venue position AND the still-reserved spot leg (2x) and tripped PER_SYMBOL/AGGREGATE/GROSS/UNHEDGED caps on a plan that fits them. The fake hides it
    because its clock does not advance between the sends (the 500 ms state cache never expires); a real round trip does."""
    monkeypatch.setattr(S._Cached.__init__, "__defaults__", (0,))                  # the state/snapshot cache never serves a value (a real order's latency)
    d = w.ready("BTCUSDT", 400.0)
    rc = w.execute(d)
    assert rc == 0, w.text()[-900:]
    entries = [o for o in w.record()["orders"] if str(o.get("link_id", "")).rsplit("-", 1)[-1] in ("p", "s")]
    assert {o["link_id"].rsplit("-", 1)[-1] for o in entries} == {"p", "s"}, entries          # BOTH entry legs reached the venue (an unwound group also ends flat)
    assert w.record()["flat_verified"] is True


def test_a_lagging_order_read_does_not_leave_the_filled_first_leg_double_counted(w, monkeypatch):
    """Second real refusal of the same plan: the perp filled (the position read shows it) but the ORDER read had not caught up when the spot leg was assessed, so the perp was settled
    by nobody and counted twice (position + reservation). The session now waits, bounded, for the venue to confirm the earlier entry leg (order Filled OR its executions) and only
    then settles it; a leg the venue never confirms stays reserved (conservative: the group is refused and unwound, never under-counted)."""
    monkeypatch.setattr(S._Cached.__init__, "__defaults__", (0,))
    seen: dict = {}
    real = TE.TestnetExecutor._take_order

    def lagging(self, r, cat, out):
        real(self, r, cat, out)
        link = r.get("orderLinkId")
        if link in out:
            seen[link] = seen.get(link, 0) + 1
            if seen[link] <= 2:                                                  # the order list shows the new order as still working for its first two reads
                out[link] = dict(out[link], status="New", filled_qty=0.0)
    monkeypatch.setattr(TE.TestnetExecutor, "_take_order", lagging)
    monkeypatch.setattr(TE.TestnetExecutor, "_take_exec", lambda self, r, cat, out: None)   # and its execution list is empty until the order list has caught up
    d = w.ready("BTCUSDT", 400.0)
    rc = w.execute(d)
    assert rc == 0, w.text()[-900:]
    entries = [o for o in w.record()["orders"] if str(o.get("link_id", "")).rsplit("-", 1)[-1] in ("p", "s")]
    assert {o["link_id"].rsplit("-", 1)[-1] for o in entries} == {"p", "s"}, entries


def test_a_risk_refusal_of_an_entry_prints_what_the_authority_was_shown(w, monkeypatch):
    """Positive control for the double count (settling disabled) AND the diagnostic: the refused leg, the account positions, the engine's leg statuses (the perp still LIVE: the window the double count lives in)."""
    monkeypatch.setattr(S._Cached.__init__, "__defaults__", (0,))
    monkeypatch.setattr(S.Run, "settle_filled_entries", lambda self, *a, **k: None)
    d = w.ready("BTCUSDT", 400.0)
    w.execute(d)
    out = w.text()
    assert "REFUSAL-DIAGNOSTIC" in out and "ABOVE_CAP" in out and '"engine_legs": {"p": "LIVE"' in out and '"state_positions": {"BTCUSDT": -360' in out


def test_the_spot_buy_limit_has_a_cushion_over_the_ask_so_a_drifting_market_still_fills_it(w):
    """Real Testnet run 4: the spot limit IOC was priced AT the plan-time ask; the market drifted ~15 bps up before execute, the limit never filled and the perp unwound. The limit now
    sits SPOT_BUY_CUSHION_BPS above the ask (an IOC buy still fills at the best ask: the cushion only bounds the worst price), and the cushioned leg still fits the notional ceiling."""
    assert w.run("preflight") == 0
    assert w.run("plan", "--symbol", "BTCUSDT", "--notional-usd", "400") == 0
    plan = w.plan_doc()
    spot = next(l for l in plan["legs"] if l["category"] == "spot")
    assert S.SPOT_BUY_CUSHION_BPS == 30
    pre = json.load(open(os.path.join(w.state_dir, "preflight.json")))
    spot_ask = float(pre["market"]["BTCUSDT"]["spot_ask"])
    assert float(spot["price"]) >= spot_ask * (1 + 29.0 / 1e4) and float(spot["price"]) <= spot_ask * (1 + 31.0 / 1e4)
    assert float(spot["qty"]) * float(spot["price"]) <= plan["aggregate_ceiling_usd"]
    d = plan["plan_digest"]
    assert w.execute(d) == 0 and w.record()["flat_verified"] is True
    assert {o["link_id"].rsplit("-", 1)[-1] for o in w.record()["orders"] if o["link_id"].rsplit("-", 1)[-1] in ("p", "s")} == {"p", "s"}


def test_an_invalidated_cache_never_serves_the_value_it_held_before_the_fill():
    """After a leg settles, the next assessment must re-read the account. If that re-read FAILS, the pre-fill value (no position, reservation already gone) must NOT be served:
    the read raises and the authority refuses with STATE_UNAVAILABLE (never under-counts)."""
    import ms_testnet_executor as _TE
    t = {"now": 0, "fail": False}

    def read():
        if t["fail"]:
            raise _TE.ExecutorError("account read failed")
        return {"positions": {}}
    c = S._Cached(read, lambda: t["now"])
    assert c.get() == {"positions": {}}
    t["fail"] = True
    t["now"] += 1000
    assert c.get() == {"positions": {}}                                   # an ordinary failed refresh still serves the last good value (the authority's age check decides)
    c.invalidate()
    with pytest.raises(_TE.ExecutorError):
        c.get()


def test_the_baseline_the_session_traded_with_is_persisted_and_reloaded_not_the_older_preflight_copy(w):
    """execute re-reads the spot baseline from the venue just before its first order; a later reconcile/restart must use THAT, not plan['spot_baseline'] (read up to 15 minutes earlier)."""
    w.fake.wallet["BTC"] = D("0.1")
    d = w.ready("BTCUSDT", 400.0)
    w.fake.wallet["BTC"] = D("0.2")                                      # the holding changed between the preflight/plan and the order
    assert w.execute(d) == 0, w.text()[-600:]
    saved = json.load(open(os.path.join(w.state_dir, "run_limits.json")))
    assert saved["baseline"] == {"spot:BTCUSDT": 0.2}
    a = S._parser().parse_args(["--state-dir", w.state_dir, "status"])
    ctx = S.Ctx(a, w.env, w.fake, w.clock.now, w.clock.sleep, lambda _p: "", w.out.append)
    run = S.Run(ctx, w.plan_doc(), S._load_capability_manifest()[0])
    assert run.baseline == {"spot:BTCUSDT": 0.2} and w.plan_doc()["spot_baseline"]["spot:BTCUSDT"] == 0.1


# ---------------------------------------------------------------- an EXIT whose create call fails twice must not wedge the group beyond the owner's confirmed unwind
_EXIT_TARGETS = {"exit-perp": lambda b: b.get("reduceOnly") is True,
                 "exit-spot": lambda b: b.get("category") == "spot" and b.get("side") == "Sell"}


@pytest.mark.parametrize("fault", ["timeout", "http500", "malformed"])
@pytest.mark.parametrize("target", ["exit-perp", "exit-spot"])
def test_a_wedged_exit_leg_is_closed_out_by_the_confirmed_unwind_and_the_account_ends_flat(w, target, fault):
    """Two consecutive create failures on the EXIT (the send and its one idempotent resend) leave the leg SENT/UNKNOWN; the engine sends no further exit while any leg is unresolved, and the
    unwind used to close out only stuck ENTRY legs, so the documented emergency tool could not flatten a naked spot long or an open hedge. Reproduced before the fix for every case."""
    d = _ready(w)
    w.fake.inject(CREATE, fault, times=2, match=_EXIT_TARGETS[target])
    assert w.execute(d) != 0                                                                   # the session cannot flatten: the exit never got an answer
    assert w.record()["flat_verified"] is not True
    exposure = {k: v["size"] for k, v in w.fake.positions.items() if v["size"] != 0}
    assert exposure or w.fake.wallet.get("BTC", D(0)) != D(0)                                 # the account really holds something that must be closed
    w.fake.faults.clear()                                                                       # the venue is healthy again
    assert w.run("reconcile", "--unwind", "--confirm-plan-digest", d) == 0, w.text()
    assert "UNRESOLVED EXIT LEG" in w.text()                                                    # the tool says what it found and what it did
    rec = w.record()
    assert rec["flat_verified"] is True and S.record_refusals(rec) == []
    assert all(p["size"] == 0 for p in w.fake.positions.values())
    assert w.fake.wallet.get("BTC", D(0)) == D(0)                                               # no orphan spot long either


def test_an_exit_the_venue_accepted_but_whose_answer_was_lost_is_adopted_not_recreated(w):
    """The request EXECUTES and the answer is lost: the engine adopts the venue's order, so no leg is ever stuck and nothing is closed out or sent twice."""
    d = _ready(w)
    w.fake.inject(CREATE, "lose_response", times=1, match=_EXIT_TARGETS["exit-perp"])
    w.execute(d)
    w.fake.faults.clear()
    assert w.run("reconcile", "--unwind", "--confirm-plan-digest", d) == 0, w.text()
    assert "UNRESOLVED EXIT LEG" not in w.text()
    assert all(p["size"] == 0 for p in w.fake.positions.values())
    assert all(c == 1 for c in w.fake.create_calls.values() if c)                                 # every link was created exactly once


def test_the_unwind_never_closes_out_an_exit_leg_on_an_incomplete_venue_read(w):
    """The close-out needs the venue's COMPLETE read to have no order under the link. With the order read failing, nothing is closed out, nothing is sent, and the exit code says not flat."""
    d = _ready(w)
    w.fake.inject(CREATE, "timeout", times=2, match=_EXIT_TARGETS["exit-perp"])
    assert w.execute(d) != 0
    w.fake.faults.clear()
    w.fake.inject("/v5/order/realtime", "http500", times=999)
    before = len(w.requests_to(CREATE))
    assert w.run("reconcile", "--unwind", "--confirm-plan-digest", d) != 0
    assert len(w.requests_to(CREATE)) == before                                                   # no fresh exit was sent on an unverified read
    assert w.record()["flat_verified"] is not True
    w.fake.faults.clear()                                                                         # the venue answers again: the same command now flattens
    assert w.run("reconcile", "--unwind", "--confirm-plan-digest", d) == 0, w.text()
    assert w.record()["flat_verified"] is True and all(p["size"] == 0 for p in w.fake.positions.values())


def test_the_unwind_does_not_close_out_a_leg_sent_moments_ago(w, monkeypatch):
    """A request that left a moment ago may still land after a fresh exit (a second exit would oversell pre-owned spot): it is left alone and the owner is told to wait."""
    d = _ready(w)
    w.fake.inject(CREATE, "timeout", times=2, match=_EXIT_TARGETS["exit-spot"])
    assert w.execute(d) != 0
    w.fake.faults.clear()
    monkeypatch.setattr(S, "CLOSE_OUT_MIN_AGE_MS", 10 ** 12)                                      # every leg looks "sent moments ago"
    before = len(w.requests_to(CREATE))
    assert w.run("reconcile", "--unwind", "--confirm-plan-digest", d) != 0
    assert "NOT closed out" in w.text() and len(w.requests_to(CREATE)) == before
    monkeypatch.setattr(S, "CLOSE_OUT_MIN_AGE_MS", 15_000)
    assert w.run("reconcile", "--unwind", "--confirm-plan-digest", d) == 0, w.text()
    assert w.record()["flat_verified"] is True


def test_an_incomplete_final_read_publishes_no_fee_funding_or_price_number(w):
    """Round 18 (execution): when the FINAL venue read is incomplete the ledger holds only the executions THAT read returned (a symbol's order and execution reads share one try-block), so its sums
    are a lower bound. The record used to publish them as numbers (fees 0.0 with both entry legs filled and fees paid; the durable COSTS_BOOKED said funding 0 was READ). Unknown is not zero."""
    d = _ready(w)
    w.fake.inject("/v5/order/realtime", "malformed", 10 ** 6, lambda q: len(w.fake.execs) >= 2)           # the order read fails once both entry legs are filled on the venue
    w.execute(d)
    rec = w.record()
    assert rec["flat_verified"] is not True
    assert len(w.fake.execs) >= 2 and sum(1 for e in w.fake.execs if e["execFee"] != "0") >= 2             # ground truth: the venue really charged fees
    assert rec["net"]["fees_usd"] is None and rec["net"]["funding_usd"] is None and rec["net"]["price_pnl_usd"] is None, rec["net"]


def test_an_incomplete_read_never_erases_rows_an_earlier_complete_read_recorded(w):
    """The same fault one execution later: the rows the complete reads recorded stay (they are venue facts that cannot un-happen), while no number is published from the incomplete final read."""
    d = _ready(w)
    w.fake.inject("/v5/order/realtime", "malformed", 10 ** 6, lambda q: len(w.fake.execs) >= 3)
    w.execute(d)
    rec = w.record()
    assert len(w.fake.execs) >= 3 and rec["flat_verified"] is not True
    assert rec["executions"] and rec["basis"] == "EXCHANGE_REPORTED", rec["basis"]
    assert rec["net"]["fees_usd"] is None and rec["net"]["funding_usd"] is None and rec["net"]["price_pnl_usd"] is None, rec["net"]


def test_a_complete_final_read_still_publishes_the_measured_fees(w):
    d = _ready(w)
    assert w.execute(d) == 0
    rec = w.record()
    assert rec["flat_verified"] is True and rec["net"]["fees_usd"] is not None and rec["net"]["fees_usd"] > 0, rec["net"]


def test_the_final_ledger_vs_venue_result_is_persisted_and_a_clean_session_says_so(w):
    """Round 18 (execution): ``Run.venue_recon`` was assigned at every reconcile and never read or written, so whether a finished session's executions, ledger fills, positions and fees agreed
    with the venue could not be audited afterwards."""
    d = _ready(w)
    assert w.execute(d) == 0
    doc = json.load(open(os.path.join(w.state_dir, "reconciliation.json")))
    assert doc["schema"] == "ms_session_reconciliation/1" and doc["view_complete"] is True
    assert doc["venue_reconciled"] is True and doc["finding_codes"] == [] and doc["checked"]["executions"] >= 4 and doc["halt_new_exposure"] is False
    assert "details" not in json.dumps(doc) and "evidence only" in doc["note"]


def test_a_hidden_execution_is_a_named_finding_in_the_persisted_reconciliation(w):
    orig = w.fake._fill

    def hide(o, qty, px, *, taker, dup=False):
        orig(o, qty, px, taker=taker, dup=dup)
        if o["orderLinkId"].endswith("-p"):
            w.fake.hidden_execs.add(w.fake.execs[-1]["execId"])        # the perp ENTRY fill: the order row says Filled, /v5/execution/list does not list it
    w.fake._fill = hide
    w.execute(_ready(w))
    doc = json.load(open(os.path.join(w.state_dir, "reconciliation.json")))
    assert doc["venue_reconciled"] is False and doc["finding_codes"], doc


def test_a_blank_execfee_leaves_the_session_net_unknown_not_a_measured_zero(w, monkeypatch):
    """The unknown fee must reach the P&L, not only the quality record: the net's fees are null, so the capital book is never debited a zero fee."""
    orig = TE.TestnetExecutor._take_exec

    def blank(self, r, cat, out):
        return orig(self, {**r, "execFee": ""} if r.get("execType", "Trade") == "Trade" else r, cat, out)
    monkeypatch.setattr(TE.TestnetExecutor, "_take_exec", blank)
    assert w.execute(_ready(w)) == 0
    net = w.record()["net"]
    assert net["fees_usd"] is None, net                                                           # unknown, never 0.0
