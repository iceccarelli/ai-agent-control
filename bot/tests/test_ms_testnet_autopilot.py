"""Unattended Testnet autopilot: the owner's grant is the ONLY thing that replaces the typed digest, and every other gate still holds."""
from __future__ import annotations

import json
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (HERE, os.path.dirname(HERE), os.path.join(os.path.dirname(HERE), "tools")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import ms_edge_gate as EG  # noqa: E402
import ms_testnet_adapter as AD  # noqa: E402
import ms_testnet_autopilot as AP  # noqa: E402
import capital_testkit as CT  # noqa: E402
import ms_capital as CC  # noqa: E402
import ms_capital_anchor as AN  # noqa: E402
import ms_testnet_session as S  # noqa: E402
from ms_testnet_world import World  # noqa: E402

EG_MIN = float(EG.MIN_NET_EDGE_BPS)


@pytest.fixture
def w(tmp_path, monkeypatch):
    return World(tmp_path, monkeypatch)


def write_grant(w, **over):
    manifest = S._load_capability_manifest()[0]
    now = w.clock.now()
    g = {"schema": AP.GRANT_SCHEMA, "reference": "owner test 2026-11-10", "created_ms": now, "expires_ms": now + 3 * 86_400_000, "manifest_plan_sha256": manifest["plan_sha256"],
         "symbols": ["BTCUSDT"], "max_sessions_per_day": 2, "max_notional_usd": 400.0, "max_hold_s": 1500, "min_funding_bps": 0.5, "allow_mechanics_sessions": True, "grant_sha256": ""}
    g.update(over)
    g["grant_sha256"] = AP._sha(g)
    path = AP.grant_path()
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(g, fh)
    os.chmod(path, 0o600)
    return g


def refused_without_network(w, *argv):
    w.fake.requests.clear()
    code = w.run(*argv, typed="")
    assert code == 2, (code, w.text())
    assert not [r for r in w.fake.requests if r[0] == "POST"], "a refused command placed an order"
    return w.text()


# ---------------------------------------------------------------- the grant
def test_no_grant_means_no_unattended_execute_and_the_typed_path_is_untouched(w):
    d = w.ready("BTCUSDT", 400.0)
    assert "UNATTENDED:GRANT_MISSING" in refused_without_network(w, "execute", "--confirm-plan-digest", d, "--unattended")
    assert w.execute(d) == 0 and w.record()["flat_verified"] is True                      # positive control: the interactive path still works
    assert not os.path.exists(os.path.join(w.state_dir, "autopilot.json"))


def test_a_valid_grant_replaces_the_typed_digest_and_records_who_authorised_it(w):
    d = w.ready("BTCUSDT", 400.0)
    write_grant(w)
    assert w.run("execute", "--confirm-plan-digest", d, "--unattended", typed="", pre_order_check=lambda: None) == 0, w.text()      # as the loop invokes it (it alone supplies the hook)
    assert w.record()["flat_verified"] is True and w.record()["confirmation"]["state"] == "CONFIRMED"
    prov = json.load(open(os.path.join(w.state_dir, "autopilot.json")))
    assert prov["mode"] == "UNATTENDED_STANDING_AUTHORISATION" and prov["plan_digest"] == d and prov["grant_reference"] == "owner test 2026-11-10"


def test_unattended_execution_is_only_valid_under_the_autopilot_loop(w):
    """The loop is the only caller that enforces the halts (ALERT, STOP, PAUSED, the capital book) and the single-writer lease, through the pre-order hook it alone can supply. `execute --unattended`
    from a shell has no such hook, so a valid grant must not make it an order path that ignores every halt (specialist E, F1: reproduced with ALERT+STOP+PAUSED set and a 4-loss halt: 4 orders)."""
    d = w.ready("BTCUSDT", 400.0)
    write_grant(w)
    text = refused_without_network(w, "execute", "--confirm-plan-digest", d, "--unattended")
    assert "UNATTENDED_ONLY_VALID_UNDER_THE_AUTOPILOT_LOOP" in text
    assert w.record().get("confirmation", {}).get("state") != "CONFIRMED" and not os.path.exists(os.path.join(w.state_dir, "autopilot.json"))
    assert w.run("execute", "--confirm-plan-digest", d, "--unattended", typed="", pre_order_check=lambda: None) == 0, w.text()          # the loop's invocation (it passes its hook) is admitted


def test_a_hook_that_refuses_the_entry_places_no_order_even_with_a_valid_grant(w):
    d = w.ready("BTCUSDT", 400.0)
    write_grant(w)
    w.fake.requests.clear()
    w.run("execute", "--confirm-plan-digest", d, "--unattended", typed="", pre_order_check=lambda: "ENGINE_HALTED:TEST")
    assert not [r for r in w.fake.requests if r[0] == "POST" and r[1] == "/v5/order/create"], "an entry order was sent although the pre-order hook refused it"


def test_unattended_still_needs_the_plans_own_digest(w):
    d = w.ready("BTCUSDT", 400.0)
    write_grant(w)
    assert "CONFIRM_FLAG_DOES_NOT_EMBED_THE_PLAN_DIGEST" in refused_without_network(w, "execute", "--confirm-plan-digest", "0" * 64, "--unattended")


@pytest.mark.parametrize("over,reason", [
    ({"expires_ms": 1}, "GRANT_EXPIRED"),
    ({"manifest_plan_sha256": "f" * 64}, "GRANT_IS_FOR_ANOTHER_MANIFEST"),
    ({"max_notional_usd": 501.0}, "GRANT_MAX_NOTIONAL"),
    ({"max_sessions_per_day": 99}, "GRANT_MAX_SESSIONS"),
    ({"max_hold_s": 99999}, "GRANT_MAX_HOLD"),
    ({"symbols": ["DOGEUSDT"]}, "GRANT_SYMBOLS"),
    ({"min_funding_bps": -1.0}, "GRANT_MIN_FUNDING"),
    ({"reference": "  "}, "GRANT_REFERENCE"),
])
def test_a_grant_outside_the_manifest_or_expired_is_no_grant(w, over, reason):
    d = w.ready("BTCUSDT", 400.0)
    write_grant(w, **over)
    assert reason in refused_without_network(w, "execute", "--confirm-plan-digest", d, "--unattended")


def test_a_tampered_or_world_readable_grant_is_no_grant(w):
    d = w.ready("BTCUSDT", 400.0)
    g = write_grant(w)
    g["max_sessions_per_day"] = 3
    with open(AP.grant_path(), "w", encoding="utf-8") as fh:
        json.dump(g, fh)                                                                 # digest no longer matches
    assert "GRANT_DIGEST_MISMATCH" in refused_without_network(w, "execute", "--confirm-plan-digest", d, "--unattended")
    write_grant(w)
    os.chmod(AP.grant_path(), 0o644)
    assert "GRANT_FILE_NOT_PRIVATE" in refused_without_network(w, "execute", "--confirm-plan-digest", d, "--unattended")


def test_the_plan_must_fit_the_grants_bounds(w):
    d = w.ready("BTCUSDT", 400.0, hold_seconds=2000)
    write_grant(w, max_hold_s=1500)
    assert "PLAN_HOLD_ABOVE_GRANT" in refused_without_network(w, "execute", "--confirm-plan-digest", d, "--unattended")
    d = w.ready("BTCUSDT", 400.0)
    write_grant(w, max_notional_usd=200.0)
    assert "PLAN_NOTIONAL_ABOVE_GRANT" in refused_without_network(w, "execute", "--confirm-plan-digest", d, "--unattended")


def test_the_grants_sessions_per_day_and_the_manifest_cap_both_hold(w):
    write_grant(w, max_sessions_per_day=1)
    d = w.ready("BTCUSDT", 400.0)
    assert w.run("execute", "--confirm-plan-digest", d, "--unattended", typed="", pre_order_check=lambda: None) == 0
    w.state_dir = os.path.join(os.path.dirname(w.state_dir), "state2")
    d = w.ready("BTCUSDT", 400.0)
    assert "GRANT_SESSIONS_PER_DAY_REACHED" in refused_without_network(w, "execute", "--confirm-plan-digest", d, "--unattended")


def test_unattended_never_skips_the_kill_switch(w):
    d = w.ready("BTCUSDT", 400.0)
    write_grant(w)
    open(os.path.join(w.state_dir, "MS_KILL"), "w").close()
    assert "KILL_SWITCH_ENGAGED" in refused_without_network(w, "execute", "--confirm-plan-digest", d, "--unattended")


def test_ci_is_refused_without_the_unattended_flag_and_a_test_is_never_allowed_a_real_network(w):
    d = w.ready("BTCUSDT", 400.0)
    write_grant(w)
    env = dict(w.env, CI="true")
    w.fake.requests.clear()
    assert w.run("execute", "--confirm-plan-digest", d, typed=d, env=env, inner=None) == 2
    assert "EXECUTE_REFUSED_IN_CI_OR_TEST" in w.text() and w.fake.requests == []
    env2 = dict(w.env, PYTEST_CURRENT_TEST="x")                                          # even WITH --unattended and a valid grant, a test cannot reach the real network
    assert w.run("execute", "--confirm-plan-digest", d, "--unattended", typed="", env=env2, inner=None) == 2
    assert "EXECUTE_REFUSED_IN_CI_OR_TEST" in w.text()


# ---------------------------------------------------------------- the funding gate
G = {"min_funding_bps": 5.0, "max_hold_s": 1500}


def test_funding_decision_goes_only_when_funding_pays_and_the_settlement_is_in_reach():
    now = 1_000_000_000_000
    go, hold, why = AP.funding_decision(0.0008, now + 600_000, now, G)
    assert go and hold == 600 + AP.EXIT_MARGIN_S and why.startswith("FUNDING_OK")          # 8 bps, settlement in 10 min: hold across it
    assert AP.funding_decision(0.0003, now + 600_000, now, G)[2].startswith("FUNDING_BELOW_THRESHOLD")
    assert AP.funding_decision(-0.0010, now + 600_000, now, G)[0] is False                  # negative funding: a short PAYS, never go
    assert AP.funding_decision(0.0008, now + 30_000, now, G)[2].startswith("SETTLEMENT_TOO_CLOSE")
    assert AP.funding_decision(0.0008, now + 3_000_000, now, G)[2].startswith("SETTLEMENT_TOO_FAR")
    assert AP.funding_decision(0.0, now + 600_000, now, dict(G, min_funding_bps=0.0))[0] is False        # zero funding never pays the fees


# ---------------------------------------------------------------- one cycle, end to end on the fake
def cycle(w):
    out = []
    return AP.run_once(w.env, inner=w.fake, clock=w.clock.now, sleep=w.clock.sleep, out=out.append, state_root=str(w.tmp / "auto")), out


def test_a_cycle_without_a_grant_is_refused_and_places_nothing(w):
    res, _ = cycle(w)
    assert res["result"] == "REFUSED" and "GRANT_MISSING" in res["why"] and not [r for r in w.fake.requests if r[0] == "POST"]


def test_a_cycle_skips_when_funding_is_below_the_grants_minimum_and_places_nothing(w):
    write_grant(w, min_funding_bps=5.0)
    w.fake.funding_rate, w.fake.funding_in_ms = "0.0001", 600_000                          # 1 bp
    res, _ = cycle(w)
    assert res["result"] == "SKIPPED" and res["why"][0].startswith("FUNDING_BELOW_THRESHOLD") and not [r for r in w.fake.requests if r[0] == "POST"]


def test_a_cycle_skips_when_the_settlement_is_too_far_away(w):
    write_grant(w, min_funding_bps=0.5)
    w.fake.funding_rate, w.fake.funding_in_ms = "0.0008", 3_000_000
    res, _ = cycle(w)
    assert res["result"] == "SKIPPED" and res["why"][0].startswith("SETTLEMENT_TOO_FAR")


def test_a_cycle_holds_the_hedge_across_the_settlement_and_ends_flat(w):
    write_grant(w, min_funding_bps=0.5)
    w.fake.funding_rate, w.fake.funding_in_ms = "0.0008", 600_000
    res, _ = cycle(w)
    assert res["result"] == "DONE", res
    assert res["flat_verified"] is True and res["hold_s"] == 600 + AP.EXIT_MARGIN_S and res["orders"] >= 3
    assert os.path.exists(os.path.join(res["state_dir"], "autopilot.json"))
    assert S.entries_today(w.clock.now()) == 1


def test_a_cycle_stops_at_the_daily_cap(w):
    write_grant(w, min_funding_bps=0.5, max_sessions_per_day=1)
    w.fake.funding_rate, w.fake.funding_in_ms = "0.0008", 600_000
    assert cycle(w)[0]["result"] == "DONE"
    w.fake.funding_in_ms = 600_000
    res, _ = cycle(w)
    assert res["result"] == "SKIPPED" and res["why"] == ["DAILY_SESSIONS_REACHED"]


# ---------------------------------------------------------------- the loop


def test_the_owner_grant_command_needs_the_capability_file_a_typed_reference_and_refuses_to_overwrite(w):
    import argparse
    a = argparse.Namespace(symbols=["BTCUSDT"], max_sessions=2, max_notional_usd=400.0, max_hold_s=1500, min_funding_bps=5.0, days=7, allow_mechanics_sessions=False)
    msgs = []
    assert AP.cmd_grant(a, input_fn=lambda _p: "", now_ms=w.clock.now(), out=msgs.append) == 2                                   # no reference typed: nothing written
    assert not os.path.exists(AP.grant_path())
    assert AP.cmd_grant(a, input_fn=lambda _p: "Vincenzo 2026-11-10", now_ms=w.clock.now(), out=msgs.append) == 0
    assert os.stat(AP.grant_path()).st_mode & 0o777 == 0o600
    g, why = AP.load_grant(w.clock.now(), S._load_capability_manifest()[0])
    assert g and not why and g["reference"] == "Vincenzo 2026-11-10"
    assert AP.cmd_grant(a, input_fn=lambda _p: "again", now_ms=w.clock.now(), out=msgs.append) == 2                              # never overwrites
    bad = argparse.Namespace(**dict(vars(a), max_notional_usd=9999.0))
    os.remove(AP.grant_path())
    assert AP.cmd_grant(bad, input_fn=lambda _p: "x", now_ms=w.clock.now(), out=msgs.append) == 2 and not os.path.exists(AP.grant_path())


def test_mainnet_is_unreachable_from_the_autopilot():
    import venues
    src = open(AP.__file__, encoding="utf-8").read()
    for token in ("LIVE_TRADING_ACK", "USE_TESTNET=0", "PAPER_TRADING=0", "api.bybit.com", "mainnet\""):
        assert token not in src.replace("mainnet is not reachable", "").replace("touch mainnet", "").replace("Mainnet is not reachable", ""), token
    assert venues.get("testnet").host == "api-testnet.bybit.com"


def test_no_github_workflow_may_run_an_order_capable_command_fly_is_the_only_executor():
    """The canonical Testnet executor is the one Fly worker (holding the global lease). A self-hosted runner, a scheduled workflow or a hosted runner must never run the autopilot's
    order-capable commands or `execute`: that would be a SECOND executor (two hedges)."""
    import re as _re
    wf_dir = os.path.join(os.path.dirname(os.path.dirname(HERE)), ".github", "workflows")
    assert not os.path.exists(os.path.join(wf_dir, "testnet-autopilot.yml"))
    for name in sorted(os.listdir(wf_dir)):
        text = open(os.path.join(wf_dir, name), encoding="utf-8").read()
        body = "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("#"))
        assert not _re.search(r"ms_testnet_autopilot\.py\s+(once|loop|grant)", body), name
        assert "--unattended" not in body and not _re.search(r"ms_testnet_session\.py[^\n]*\bexecute\b", body), name
        assert "self-hosted" not in body or name in ("carry-drill-testnet.yml",), name


@pytest.fixture
def stop_state():
    AP._STOP.clear()
    AP._ACTIVE["dir"] = None
    yield
    AP._STOP.clear()
    AP._ACTIVE["dir"] = None


def test_a_signal_engages_the_active_sessions_kill_switch_and_stops_the_loop(tmp_path, stop_state):
    d = tmp_path / "ms_auto_x"
    d.mkdir()
    AP._ACTIVE["dir"] = str(d)
    AP._on_signal(15)
    assert (d / "MS_KILL").exists() and AP._STOP.is_set()                              # the engine's own kill path: cancel entries, reduce-only unwind, verify flat
    calls = []
    root = str(tmp_path / "ap")
    assert AP.loop({}, root=root, once=lambda *a, **k: calls.append(1) or {"result": "DONE"}, sleep=lambda _s: None, out=lambda _m: None) == 0 and calls == []


def test_a_signal_with_no_active_session_only_stops_and_never_raises(tmp_path, stop_state):
    AP._on_signal(2)
    assert AP._STOP.is_set()
    AP._ACTIVE["dir"] = str(tmp_path / "does-not-exist")                               # the state dir was not created yet: the handler must not raise
    AP._on_signal(15)


def test_a_stop_before_the_order_places_nothing_and_counts_no_entry(w, stop_state):
    write_grant(w, min_funding_bps=0.5)
    w.fake.funding_rate, w.fake.funding_in_ms = "0.0008", 600_000
    AP._STOP.set()
    res, _ = cycle(w)
    assert res["result"] == "SKIPPED" and res["why"] == ["STOP_REQUESTED"] and not [r for r in w.fake.requests if r[0] == "POST"] and S.entries_today(w.clock.now()) == 0




# ---------------------------------------------------------------- the ECONOMICS gate: a funding rate is not a profit
def test_a_funding_rate_above_the_owners_floor_is_still_refused_when_the_expected_net_edge_is_negative(w):
    write_grant(w, min_funding_bps=0.5, allow_mechanics_sessions=False)
    w.fake.fee_rates = {"spot": ("0.001", "0.001"), "linear": ("0.0002", "0.00055")}                  # the account's own measured tier
    w.fake.funding_rate, w.fake.funding_in_ms = "0.0001", 600_000                                      # 1 bp: clears the 0.5 bp floor, nowhere near the ~53 bps the round trip needs
    res, _ = cycle(w)
    assert res["result"] == "SKIPPED" and res["why"][0] == "EDGE_REJECTED_UNECONOMIC", res
    assert float(res["edge"]["net_edge_bps"]) < 0 and float(res["edge"]["required_funding_bps"]) > 40
    assert not [r for r in w.fake.requests if r[0] == "POST"] and S.entries_today(w.clock.now()) == 0


def test_lowering_the_owners_funding_threshold_cannot_make_an_uneconomic_trade_pass(w):
    write_grant(w, min_funding_bps=0.0, allow_mechanics_sessions=False)
    w.fake.fee_rates = {"spot": ("0.001", "0.001"), "linear": ("0.0002", "0.00055")}
    w.fake.funding_rate, w.fake.funding_in_ms = "0.0030", 600_000                                      # 30 bps: more funding, still below the derived requirement
    res, _ = cycle(w)
    assert res["result"] == "SKIPPED" and res["why"][0] == "EDGE_REJECTED_UNECONOMIC" and not [r for r in w.fake.requests if r[0] == "POST"]


def test_an_assumed_fee_tier_blocks_a_profit_session_even_with_huge_funding(w):
    write_grant(w, min_funding_bps=0.5, allow_mechanics_sessions=False)
    w.fake.fee_rates = None                                                                            # the key cannot read its own fee rate: the published tier is only ASSUMED
    w.fake.funding_rate, w.fake.funding_in_ms = "0.0100", 600_000
    res, _ = cycle(w)
    assert res["result"] == "SKIPPED" and res["why"][0] == "EDGE_INCONCLUSIVE" and any("ASSUMED_INPUTS_CANNOT_PASS" in x for x in res["why"])


def test_a_profit_candidate_is_NO_TRADE_until_the_evidence_the_calibration_and_the_measured_costs_exist(w):
    """Round 22 (Phase 6, the strategy must EARN the right to trade): an 80 bps print makes the ECONOMICS a PROFIT_CANDIDATE, but a profit session also needs QUALIFIED statistical evidence, a calibrated
    execution model, measured (not typed-constant) costs and a modelled worst case. None exists, so the deterministic gate says NO_TRADE BEFORE the plan, the capital park and any order. (Before Round 22
    this session ran: it was the executor being active, not the strategy being proven.)"""
    write_grant(w, min_funding_bps=0.5, allow_mechanics_sessions=False)
    w.fake.fee_rates = {"spot": ("0.001", "0.001"), "linear": ("0.0002", "0.00055")}
    w.fake.funding_rate, w.fake.funding_in_ms = "0.0080", 600_000                                      # 80 bps: above the derived requirement with a measured tier
    res, _ = cycle(w)
    assert res["result"] == "SKIPPED" and res["why"][0] == "NO_TRADE" and not [r for r in w.fake.requests if r[0] == "POST"], res["why"]
    gate = res["eligibility"]
    assert gate["profile"] == "PROFIT_SEEKING" and gate["verdict"] == "NO_TRADE"
    assert {"STATISTICAL_EVIDENCE_SUFFICIENT", "EXECUTION_MODEL_VALID", "COSTS_KNOWN"} <= set(gate["unknown"] + gate["failed"])
    assert not any(t["verdict"] == "FAIL" for t in gate["terms"] if t["term"] == "EDGE_POSITIVE_AFTER_COSTS"), "the economics DID clear: the missing evidence is what blocks"
    assert res["edge"]["session_class"] == "PROFIT_CANDIDATE" and float(res["edge"]["net_edge_bps"]) >= EG_MIN and res["decision_id"]
    # decided BEFORE a session directory, a preflight or any venue write exists (an idle loop would otherwise create one per cycle)
    assert not [d for d in (os.listdir(w.tmp / "auto") if (w.tmp / "auto").exists() else []) if d.startswith("ms_auto_")]
    assert not [r for r in w.fake.requests if r[1] in ("/v5/account/wallet-balance", "/v5/position/list")], "no preflight account read happened"


def test_a_mechanics_session_needs_the_owner_to_allow_it_and_is_never_called_an_opportunity(w):
    w.fake.fee_rates = {"spot": ("0.001", "0.001"), "linear": ("0.0002", "0.00055")}
    w.fake.funding_rate, w.fake.funding_in_ms = "0.0001", 600_000
    write_grant(w, min_funding_bps=0.5, allow_mechanics_sessions=True)
    res, _ = cycle(w)
    assert res["result"] == "DONE" and res["session_class"] == "TESTNET_MECHANICS" and float(res["edge"]["net_edge_bps"]) < 0
    ev = json.load(open(os.path.join(res["state_dir"], "edge_verdict.json")))
    assert ev["label"] == "TESTNET_MECHANICS_NOT_AN_OPPORTUNITY"


def test_a_mechanics_session_is_refused_when_the_market_is_unusable_even_if_allowed(w):
    write_grant(w, min_funding_bps=0.5, allow_mechanics_sessions=True)
    w.fake.fee_rates = {"spot": ("0.001", "0.001"), "linear": ("0.0002", "0.00055")}
    w.fake.funding_rate, w.fake.funding_in_ms = "0.0001", 600_000
    w.fake.liquidity["BTCUSDT"] = __import__("decimal").Decimal("0.0001")                              # the book cannot absorb the size
    res, _ = cycle(w)
    assert res["result"] == "SKIPPED" and not [r for r in w.fake.requests if r[0] == "POST"]


def test_an_old_style_grant_without_the_mechanics_field_is_no_grant(w):
    g = write_grant(w)
    del g["allow_mechanics_sessions"]
    g["grant_sha256"] = AP._sha(g)
    with open(AP.grant_path(), "w", encoding="utf-8") as fh:
        json.dump(g, fh)
    res, _ = cycle(w)
    assert res["result"] == "REFUSED" and res["why"] == ["GRANT_SHAPE"]


def test_the_grant_command_records_the_mechanics_choice_explicitly(w):
    import argparse
    a = argparse.Namespace(symbols=["BTCUSDT"], max_sessions=2, max_notional_usd=400.0, max_hold_s=1500, min_funding_bps=5.0, days=7, allow_mechanics_sessions=True)
    assert AP.cmd_grant(a, input_fn=lambda _p: "owner", now_ms=w.clock.now(), out=lambda _m: None) == 0
    assert AP.load_grant(w.clock.now(), S._load_capability_manifest()[0])[0]["allow_mechanics_sessions"] is True


# ================================================================ the lifecycle: persistent states, recovery, revocation, drain
import ms_engine_state as ES  # noqa: E402


def run_loop(w, root, once, *, n=1, recover_fn=None, telemetry=None, lease=None, **kw):
    out = []
    kwargs = dict(root=str(root), once=once, clock=w.clock.now, sleep=lambda _s: None, out=out.append, max_iterations=n, telemetry=telemetry, lease=lease, inner=w.fake,
                  state_root=str(w.tmp / "auto"))
    if recover_fn is not None:
        kwargs["recover_fn"] = recover_fn
    kwargs.update(kw)
    rc = AP.loop(w.env, **kwargs)
    return rc, out


OK_RECOVERY = lambda *a, **k: (True, [], {"venue_read_complete": True, "reconciled_ms": 1})          # noqa: E731
NEVER = lambda *a, **k: (_ for _ in ()).throw(AssertionError("once() must not be called"))          # noqa: E731


def state_of(root, clock):
    return ES.EngineState(str(root), clock).load()["state"]


def test_engine_state_records_a_source_and_timestamp_for_every_transition_and_refuses_unknown_states(tmp_path):
    t = {"now": 1000}
    st = ES.EngineState(str(tmp_path / "r"), lambda: t["now"])
    st.transition("RECOVERING", "startup")
    t["now"] = 2000
    cur = st.transition("WAITING_FOR_OPPORTUNITY", "recovery_ok")
    assert [h["state"] for h in cur["history"]] == ["RECOVERING", "WAITING_FOR_OPPORTUNITY"] and cur["history"][1] == {"state": "WAITING_FOR_OPPORTUNITY", "at_ms": 2000, "source": "recovery_ok"}
    with pytest.raises(ValueError):
        st.transition("HEALTHY", "x")
    assert os.stat(st.path).st_mode & 0o777 == 0o600


def test_a_stop_marker_keeps_the_process_alive_as_STOPPED_and_starts_nothing(w, tmp_path, stop_state):
    root = tmp_path / "ap"
    ES.EngineState(str(root), w.clock.now).set("STOP", {"by": "owner"})
    rc, _ = run_loop(w, root, NEVER, n=3, recover_fn=OK_RECOVERY)
    assert rc == 0 and state_of(root, w.clock.now) == "STOPPED"


def test_an_alert_survives_a_restart_and_blocks_trading_until_a_human_clears_it(w, tmp_path, stop_state):
    root = tmp_path / "ap"
    ES.EngineState(str(root), w.clock.now).set("ALERT", {"code": "NOT_FLAT"})
    for _ in range(2):                                                                  # two "restarts": a fresh loop each time
        rc, _ = run_loop(w, root, NEVER, n=2, recover_fn=OK_RECOVERY)
        assert rc == 0 and state_of(root, w.clock.now) == "ALERT" and os.path.exists(root / "ALERT")


def test_a_paused_marker_pauses_and_the_resume_command_clears_it_but_never_an_alert(w, tmp_path, stop_state):
    root = tmp_path / "ap"
    eng = ES.EngineState(str(root), w.clock.now)
    eng.set("PAUSED", {"why": "x"})
    run_loop(w, root, NEVER, n=1, recover_fn=OK_RECOVERY)
    assert state_of(root, w.clock.now) == "PAUSED"
    assert AP.cmd_resume(str(root), w.clock.now, "examined", out=lambda _m: None) == 0 and not eng.has("PAUSED")
    eng.set("ALERT", {"code": "x"})
    assert AP.cmd_resume(str(root), w.clock.now, "try", out=lambda _m: None) == 2 and eng.has("ALERT")


def test_a_restart_cannot_trade_before_the_venue_proves_flat(w, tmp_path, stop_state):
    root = tmp_path / "ap"
    w.fake.inject_position("BTCUSDT", -0.005)                                           # the previous process died with a short open
    calls = []
    rc, _ = run_loop(w, root, lambda *a, **k: calls.append(1) or {"result": "SKIPPED", "why": []}, n=3)
    st = ES.EngineState(str(root), w.clock.now)
    assert calls == [] and st.has("ALERT") and st.load()["state"] == "UNRECONCILED"
    assert any("PERP_POSITION_OPEN" in str(x) for x in st.read("ALERT")["why"])


def test_an_unreadable_venue_pauses_without_an_alert_and_is_retried(w, tmp_path, stop_state):
    root = tmp_path / "ap"
    unreadable = lambda *a, **k: (False, ["VENUE_UNREADABLE:ExecutorError"], {"venue_read_complete": False})          # noqa: E731
    run_loop(w, root, NEVER, n=2, recover_fn=unreadable)
    st = ES.EngineState(str(root), w.clock.now)
    assert st.load()["state"] == "PAUSED" and not st.has("ALERT")


def test_a_flat_venue_after_an_unresolved_prior_session_is_recorded_and_allowed(w, tmp_path, stop_state):
    write_grant(w, min_funding_bps=0.5)
    prior = w.tmp / "auto" / "ms_auto_20261110T000000"
    os.makedirs(prior)
    open(prior / S.FILES["journal"], "w").write("x\n")                                  # a session started (the REAL journal file) and never reached flat_verified
    json.dump({"baseline": {"spot:BTCUSDT": 0.0}}, open(prior / "run_limits.json", "w"))        # a started session always wrote its spot baseline first (Round 22: without it recovery refuses)
    json.dump({"group": {"legs": [{"symbol": "BTCUSDT", "qty": "0.005"}]}}, open(prior / "plan.json", "w"))      # ... and its plan: the symbols it traded are the ones that need a baseline
    ok, why, info = AP.recover(w.env, inner=w.fake, clock=w.clock.now, state_root=str(w.tmp / "auto"))
    assert ok and why == [] and info["unresolved"] is True, (ok, why, info)
    assert json.load(open(prior / "recovery.json"))["accounting_must_be_reconciled"] is True


def test_recovery_refuses_an_unresolved_session_whose_spot_baseline_is_unknown(w, tmp_path):
    """REPRODUCED (Round 22 execution review): a session that started and never reached flat_verified, with no run_limits baseline, was 'flat' because the spot check was silently skipped."""
    write_grant(w, min_funding_bps=0.5)
    prior = w.tmp / "auto" / "ms_auto_20261110T000000"
    os.makedirs(prior)
    open(prior / S.FILES["journal"], "w").write("x\n")
    json.dump({"group": {"legs": [{"symbol": "BTCUSDT", "qty": "0.005"}]}}, open(prior / "plan.json", "w"))
    ok, why, info = AP.recover(w.env, inner=w.fake, clock=w.clock.now, state_root=str(w.tmp / "auto"))
    assert not ok and why == ["BASELINE_UNKNOWN:BTCUSDT"] and info["venue_read_complete"] is True      # only the symbol the session TRADED needs a baseline
    (prior / "plan.json").unlink()                                                                       # an unreadable plan: which symbols were traded is unknown, so every allowlisted one needs it
    ok, why, info = AP.recover(w.env, inner=w.fake, clock=w.clock.now, state_root=str(w.tmp / "auto"))
    assert not ok and "BASELINE_UNKNOWN:BTCUSDT" in why and "BASELINE_UNKNOWN:ETHUSDT" in why
    # a prior session that NEVER started (no journal, no orders) has nothing unresolved: the account's own spot balance is not ours to baseline
    (prior / S.FILES["journal"]).unlink()
    ok, why, info = AP.recover(w.env, inner=w.fake, clock=w.clock.now, state_root=str(w.tmp / "auto"))
    assert ok and info["unresolved"] is False
    # an already-resolved session (flat_verified) is not unresolved either
    open(prior / S.FILES["journal"], "w").write("x\n")
    json.dump({"flat_verified": True}, open(prior / "testnet_session.json", "w"))
    ok, why, info = AP.recover(w.env, inner=w.fake, clock=w.clock.now, state_root=str(w.tmp / "auto"))
    assert ok and info["unresolved"] is False


def test_recovery_refuses_when_the_spot_balance_differs_from_the_sessions_baseline(w, tmp_path):
    prior = w.tmp / "auto" / "ms_auto_20261110T000000"
    os.makedirs(prior)
    json.dump({"baseline": {"spot:BTCUSDT": 0.0}}, open(prior / "run_limits.json", "w"))
    w.fake.wallet["BTC"] = __import__("decimal").Decimal("0.5")                          # a spot leg is still held
    ok, why, info = AP.recover(w.env, inner=w.fake, clock=w.clock.now, state_root=str(w.tmp / "auto"))
    assert not ok and info["venue_read_complete"] and any(x.startswith("SPOT_DELTA_OPEN") for x in why)


def test_clear_alert_is_refused_unless_the_venue_is_proven_flat_and_a_reason_is_given(w, tmp_path, stop_state):
    root = tmp_path / "ap"
    eng = ES.EngineState(str(root), w.clock.now)
    eng.set("ALERT", {"code": "NOT_FLAT"})
    kw = dict(inner=w.fake, state_root=str(w.tmp / "auto"))
    assert AP.cmd_clear_alert(w.env, str(root), w.clock.now, "  ", out=lambda _m: None, **kw) == 2 and eng.has("ALERT")
    w.fake.inject_position("BTCUSDT", -0.005)
    assert AP.cmd_clear_alert(w.env, str(root), w.clock.now, "looked", out=lambda _m: None, **kw) == 2 and eng.has("ALERT")
    w.fake.inject_position("BTCUSDT", 0)
    assert AP.cmd_clear_alert(w.env, str(root), w.clock.now, "closed the short by hand and verified flat", out=lambda _m: None, **kw) == 0 and not eng.has("ALERT")
    log = [json.loads(x) for x in open(root / "marker_log.jsonl")]
    assert log[-1]["action"] == "CLEARED" and "verified flat" in log[-1]["payload"]["reason"]


def test_results_map_to_states_and_a_session_that_is_not_flat_raises_an_alert(w, tmp_path, stop_state):
    root = tmp_path / "ap"
    run_loop(w, root, lambda *a, **k: {"result": "NOT_FLAT", "why": [], "state_dir": "x"}, n=1, recover_fn=OK_RECOVERY)
    st = ES.EngineState(str(root), w.clock.now)
    assert st.has("ALERT") and st.load()["state"] == "UNRECONCILED" and st.read("ALERT")["code"] == "NOT_FLAT"
    root2 = tmp_path / "ap2"
    run_loop(w, root2, lambda *a, **k: {"result": "SKIPPED", "why": ["FUNDING_BELOW_THRESHOLD"]}, n=1, recover_fn=OK_RECOVERY)
    assert state_of(root2, w.clock.now) == "WAITING_FOR_OPPORTUNITY"
    root3 = tmp_path / "ap3"
    run_loop(w, root3, lambda *a, **k: {"result": "DONE", "why": [], "flat_verified": True}, n=1, recover_fn=OK_RECOVERY)
    assert state_of(root3, w.clock.now) == "FLAT"


def test_missing_owner_authority_pauses_by_itself_and_repeated_unexpected_refusals_raise_an_alert(w, tmp_path, stop_state):
    root = tmp_path / "ap"
    run_loop(w, root, lambda *a, **k: {"result": "REFUSED", "why": ["GRANT_MISSING"]}, n=5, recover_fn=OK_RECOVERY)
    st = ES.EngineState(str(root), w.clock.now)
    assert st.load()["state"] == "PAUSED" and not st.has("ALERT")                       # owner-side: resumes by itself when the grant is installed
    root2 = tmp_path / "ap2"
    run_loop(w, root2, lambda *a, **k: {"result": "REFUSED", "why": ["EXECUTE_REFUSED"]}, n=4, recover_fn=OK_RECOVERY)
    assert ES.EngineState(str(root2), w.clock.now).has("ALERT")


def test_three_errors_in_a_row_raise_an_alert_and_the_loop_keeps_reporting_instead_of_exiting(w, tmp_path, stop_state):
    root = tmp_path / "ap"
    rc, _ = run_loop(w, root, lambda *a, **k: {"result": "ERROR", "why": ["FUNDING_READ_FAILED"]}, n=6, recover_fn=OK_RECOVERY)
    st = ES.EngineState(str(root), w.clock.now)
    assert rc == 0 and st.has("ALERT") and st.read("ALERT")["code"] == "REPEATED_ERRORS" and st.load()["state"] == "ALERT"


def test_drain_writes_a_fresh_proof_only_after_the_venue_is_flat_and_deploy_check_refuses_otherwise(w, tmp_path, stop_state):
    root = tmp_path / "ap"
    ok, msg = AP.deploy_ready(str(root), w.clock.now)
    assert not ok and "DRAINED" in msg
    AP.cmd_drain(str(root), w.clock.now, 0, out=lambda _m: None)
    run_loop(w, root, NEVER, n=2, recover_fn=OK_RECOVERY)
    assert state_of(root, w.clock.now) == "DRAINING" and AP.deploy_ready(str(root), w.clock.now) == (True, "OK_TO_DEPLOY")
    w.clock.t += AP.DRAINED_FRESH_MS + 1                                                # the proof goes stale
    assert AP.deploy_ready(str(root), w.clock.now)[0] is False
    ES.EngineState(str(root), w.clock.now).set("ALERT", {"code": "x"})
    ES.EngineState(str(root), w.clock.now).set("DRAINED", {"venue_flat_proof": True})
    assert "ALERT" in AP.deploy_ready(str(root), w.clock.now)[1]


def test_deploy_check_refuses_while_a_session_is_active(w, tmp_path):
    root = tmp_path / "ap"
    ES.EngineState(str(root), w.clock.now).transition("EXECUTING", "test")
    ES.EngineState(str(root), w.clock.now).set("DRAINED", {"venue_flat_proof": True})
    ok, msg = AP.deploy_ready(str(root), w.clock.now)
    assert not ok and "session is active" in msg


def test_a_failed_telemetry_sink_never_breaks_the_loop_and_every_heartbeat_has_the_contract_fields(w, tmp_path, stop_state):
    root = tmp_path / "ap"

    class Rec(AP.NullTelemetry):
        def __init__(self):
            self.hbs = []

        def heartbeat(self, hb):
            self.hbs.append(hb)

    class Broken(AP.NullTelemetry):
        def heartbeat(self, hb):
            raise RuntimeError("supabase down")

        def event(self, *a, **k):
            raise RuntimeError("supabase down")
    rec = Rec()
    run_loop(w, root, lambda *a, **k: {"result": "SKIPPED", "why": ["X"]}, n=2, recover_fn=OK_RECOVERY, telemetry=rec)
    need = {"worker_id", "app", "machine_id", "region", "code_revision", "runtime_version", "run_id", "mode", "state", "session_id", "last_bybit_read_ms", "last_market_data_ms",
            "last_reconcile_ms", "last_supabase_sync_ms", "outbox_pending", "disk_free_bytes", "lease_token", "seq", "at_ms"}
    assert rec.hbs and all(need <= set(h) for h in rec.hbs) and {h["state"] for h in rec.hbs} <= set(ES.STATES)
    rc, _ = run_loop(w, tmp_path / "ap2", lambda *a, **k: {"result": "SKIPPED", "why": ["X"]}, n=2, recover_fn=OK_RECOVERY, telemetry=Broken())
    assert rc == 0


def test_a_lease_that_is_not_held_pauses_the_loop_and_starts_no_session(w, tmp_path, stop_state):
    class NoLease:
        token = None

        def held(self):
            return False
    root = tmp_path / "ap"
    run_loop(w, root, NEVER, n=2, recover_fn=OK_RECOVERY, lease=NoLease())
    assert state_of(root, w.clock.now) == "PAUSED"


# ---------------------------------------------------------------- revocation during a session: never abandon a hedge, never a naked leg
def revoke_cycle(w, root, trigger, **grant_over):
    """A full hold session on the fake; ``trigger(n)`` runs inside the session's own sleep (n-th call) to revoke authority mid-session."""
    write_grant(w, **{"min_funding_bps": 0.5, "allow_mechanics_sessions": True, **grant_over})
    w.fake.fee_rates = {"spot": ("0.001", "0.001"), "linear": ("0.0002", "0.00055")}
    w.fake.funding_rate, w.fake.funding_in_ms = "0.0008", 600_000
    calls = {"n": 0}
    real = w.clock.sleep

    def sleep(s):
        calls["n"] += 1
        trigger(calls["n"])
        real(s)
    return AP.run_once(w.env, inner=w.fake, clock=w.clock.now, sleep=sleep, out=lambda _m: None, state_root=str(w.tmp / "auto"), engine_root=str(root), lease=getattr(w, "lease", None))


def test_deleting_the_grant_during_a_session_unwinds_and_verifies_flat(w, tmp_path):
    root = tmp_path / "ap"
    res = revoke_cycle(w, root, lambda n: os.remove(AP.grant_path()) if n == 3 and os.path.exists(AP.grant_path()) else None)
    assert res["revoked_reason"].startswith("GRANT:") and res["flat_verified"] is True and res["result"] == "DONE"
    assert os.path.exists(os.path.join(res["state_dir"], "MS_KILL")) and json.load(open(os.path.join(res["state_dir"], "revocation.json")))["reason"].startswith("GRANT:")
    assert all(p["size"] == 0 for p in w.fake.positions.values())                       # no naked leg


def test_a_stop_marker_set_during_a_session_unwinds_and_verifies_flat(w, tmp_path):
    root = tmp_path / "ap"
    res = revoke_cycle(w, root, lambda n: ES.EngineState(str(root), w.clock.now).set("STOP", {"by": "owner"}) if n == 3 else None)
    assert res["revoked_reason"] == "STOP" and res["flat_verified"] is True and all(p["size"] == 0 for p in w.fake.positions.values())


def test_losing_the_executor_lease_during_a_session_unwinds_and_verifies_flat(w, tmp_path):
    class Lease:
        token = 7

        def __init__(self):
            self.ok = True

        def held(self):
            return self.ok
    w.lease = Lease()
    root = tmp_path / "ap"
    res = revoke_cycle(w, root, lambda n: setattr(w.lease, "ok", False) if n == 3 else None)
    assert res["revoked_reason"] == "LEASE_LOST" and res["flat_verified"] is True and all(p["size"] == 0 for p in w.fake.positions.values())


def test_an_expiring_grant_during_a_session_is_a_revocation(w, tmp_path):
    root = tmp_path / "ap"
    res = revoke_cycle(w, root, lambda n: None, expires_ms=w.clock.now() + 400_000)       # valid at the plan and the execute gate, gone 6.7 minutes into a 12 minute hold
    assert res["revoked_reason"] == "GRANT:GRANT_EXPIRED" and res["flat_verified"] is True and all(p["size"] == 0 for p in w.fake.positions.values())


def test_a_session_nobody_revokes_runs_to_the_end_with_no_revocation(w, tmp_path):
    root = tmp_path / "ap"
    res = revoke_cycle(w, root, lambda n: None)
    assert res["result"] == "DONE" and res["revoked_reason"] is None and not os.path.exists(os.path.join(res["state_dir"], "revocation.json"))


# ================================================================ the real telemetry (durable outbox) and the global lease, adapted to the loop
import exec_evidence as XE  # noqa: E402


class StubLeaseClient:
    """Shaped like exec_evidence.LeaseClient: `held` is a PROPERTY."""

    def __init__(self, free=True):
        self.free, self.acquires, self.renews, self._held, self.token_n = free, 0, 0, False, 0
        self.renew_ok = True

    @property
    def held(self):
        return self._held

    @property
    def token(self):
        return self.token_n if self._held else None

    def acquire(self):
        self.acquires += 1
        if not self.free:
            return False
        self._held, self.token_n = True, self.token_n + 1
        return True

    def renew(self):
        self.renews += 1
        if not self.renew_ok:
            self._held = False
            return False
        return True


def test_the_lease_adapter_never_reacquires_inside_held_and_renews_only_when_due():
    t = {"m": 0.0}
    lc = StubLeaseClient()
    rl = AP.RenewingLease(lc, renew_every_s=20, mono=lambda: t["m"])
    assert rl.held() is False and lc.acquires == 0                                   # held() never acquires: a lease lost mid-session must unwind, not be silently re-won
    assert rl.ensure() is True and lc.acquires == 1 and rl.token == 1
    t["m"] = 10
    assert rl.held() is True and lc.renews == 0
    t["m"] = 25
    assert rl.held() is True and lc.renews == 1
    lc.renew_ok = False
    t["m"] = 50
    assert rl.held() is False and rl.reason == "renew_failed" and rl.token is None
    assert rl.held() is False and lc.acquires == 1                                   # still not re-won by held()
    lc.renew_ok = True
    assert rl.ensure() is True and lc.acquires == 2 and rl.token == 2                # between sessions it may re-acquire: a NEW fencing token


def test_a_lease_held_by_another_executor_keeps_this_one_paused(w, tmp_path, stop_state):
    rl = AP.RenewingLease(StubLeaseClient(free=False), renew_every_s=20)
    root = tmp_path / "ap"
    run_loop(w, root, NEVER, n=3, recover_fn=OK_RECOVERY, lease=rl)
    st = ES.EngineState(str(root), w.clock.now).load()
    assert st["state"] == "PAUSED" and st["source"].startswith("lease_not_held:acquire_refused")


def test_without_supabase_configuration_there_is_no_lease_and_the_engine_never_executes(w, tmp_path, stop_state):
    tel, lease = AP.build_runtime({}, str(tmp_path / "ap"), w.clock.now)
    assert isinstance(tel, AP.NullTelemetry) and isinstance(lease, AP.NoLease) and lease.reason == "supabase_not_configured"
    root = tmp_path / "ap"
    run_loop(w, root, NEVER, n=2, recover_fn=OK_RECOVERY, lease=lease)
    assert ES.EngineState(str(root), w.clock.now).load()["source"] == "lease_not_held:supabase_not_configured"
    tel2, lease2 = AP.build_runtime({"SUPABASE_URL": "http://evil.example.com", "SUPABASE_SERVICE_ROLE_KEY": "k" * 20, "SUPABASE_OWNER_ID": "0" * 8 + "-0000-0000-0000-" + "0" * 12}, str(tmp_path / "ap2"), w.clock.now)
    assert isinstance(lease2, AP.NoLease) and lease2.reason.startswith("supabase_config_refused")                # a non-*.supabase.co / non-https URL is refused


def test_a_configured_runtime_builds_the_real_client_lease_and_outbox_without_touching_the_network(w, tmp_path):
    env = {"SUPABASE_URL": "https://abcdefgh.supabase.co", "SUPABASE_SERVICE_ROLE_KEY": "k" * 30, "SUPABASE_OWNER_ID": "11111111-2222-3333-4444-555555555555", "FLY_MACHINE_ID": "m1"}
    tel, lease = AP.build_runtime(env, str(tmp_path / "ap"), w.clock.now)
    assert isinstance(tel, AP.EvidenceTelemetry) and isinstance(lease, AP.RenewingLease) and lease.held() is False and tel.pending() == 0
    assert "k" * 30 not in repr(tel.sync.client) and "k" * 30 not in repr(lease)


class CapturingSync:
    def __init__(self):
        self.hbs, self.drains = [], 0

    def drain(self):
        self.drains += 1
        return type("R", (), {"ok": True})()

    def send_heartbeat(self, hb):
        self.hbs.append(XE.validate_heartbeat(hb))                                   # the shared contract, validated by the real function
        return "INSERTED"


def test_every_heartbeat_and_event_the_loop_emits_satisfies_the_evidence_contract_through_the_real_outbox(w, tmp_path, stop_state):
    write_grant(w, min_funding_bps=0.5, allow_mechanics_sessions=True)
    w.fake.fee_rates = {"spot": ("0.001", "0.001"), "linear": ("0.0002", "0.00055")}
    w.fake.funding_rate, w.fake.funding_in_ms = "0.0008", 600_000
    box = XE.Outbox(str(tmp_path / "outbox"))
    sync = CapturingSync()
    errors = []

    class Tel(AP.EvidenceTelemetry):
        def event(self, kind, payload, session_id=None):
            try:
                super().event(kind, payload, session_id)
            except Exception as exc:                                                 # the loop swallows telemetry errors by design: this test must SEE them
                errors.append((kind, repr(exc)))
    tel = Tel(box, sync, w.clock.now)
    root = tmp_path / "ap"
    rc, _ = run_loop(w, root, AP.run_once, n=1, recover_fn=lambda *a, **k: (True, [], {"venue_read_complete": True, "reconciled_ms": 1}), telemetry=tel, sleep=w.clock.sleep)
    assert rc == 0 and errors == [], errors
    kinds = [ev["kind"] for _s, _o, ev in box.iter_pending()]
    assert "SESSION_STATE" in kinds and "OPPORTUNITY_EVAL" in kinds and "DECISION" in kinds
    assert sync.hbs and {h["state"] for h in sync.hbs} <= set(XE.ENGINE_STATES) and all(h["mode"] == "TESTNET" and h["disk_free_bytes"] >= 0 for h in sync.hbs)
    assert sync.drains >= 1 and tel.last_sync_ms is not None and sync.hbs[-1]["last_supabase_sync_ms"] is not None


def test_an_alert_emits_a_critical_incident_event_that_satisfies_the_evidence_contract(w, tmp_path, stop_state):
    box = XE.Outbox(str(tmp_path / "outbox"))
    tel = AP.EvidenceTelemetry(box, CapturingSync(), w.clock.now)
    w.fake.inject_position("BTCUSDT", -0.005)
    run_loop(w, tmp_path / "ap", NEVER, n=1, telemetry=tel)
    inc = [ev for _s, _o, ev in box.iter_pending() if ev["kind"] == "INCIDENT"]
    assert inc and inc[0]["payload"]["code"] == "UNRECONCILED_AT_RECOVERY" and inc[0]["payload"]["severity"] == "CRITICAL" and "PERP_POSITION_OPEN" in inc[0]["payload"]["detail"]


# ================================================================ red-team regressions (tree 4399e60): each asserts the SAFE behaviour
def creates(w):
    return [r for r in w.fake.requests if r[0] == "POST" and r[1] == "/v5/order/create"]


class CountingLease:
    """held() is True for the first `ok_calls` calls, then False (or raises)."""
    token = 3

    def __init__(self, ok_calls, raises=False):
        self.n, self.ok_calls, self.raises = 0, ok_calls, raises

    def held(self):
        self.n += 1
        if self.n > self.ok_calls:
            if self.raises:
                raise RuntimeError("lease backend exploded")
            return False
        return True


def prepared(w, **grant_over):
    write_grant(w, **{"min_funding_bps": 0.5, "allow_mechanics_sessions": True, **grant_over})
    w.fake.fee_rates = {"spot": ("0.001", "0.001"), "linear": ("0.0002", "0.00055")}
    # Round 22: a TESTNET_MECHANICS session (1 bp: the economics are negative by design). It used to be 8 bps, a PROFIT_CANDIDATE on this fake tier; a profit session now needs QUALIFIED evidence and a
    # calibrated model (eligibility gate) and is NO_TRADE here, so the lifecycle plumbing these tests exercise runs as the measurement session it really is.
    w.fake.funding_rate, w.fake.funding_in_ms = "0.0001", 600_000


def run(w, root, **kw):
    return AP.run_once(w.env, inner=w.fake, clock=w.clock.now, sleep=w.clock.sleep, out=lambda _m: None, state_root=str(w.tmp / "auto"), engine_root=str(root), **kw)


def test_RT1_a_lease_lost_between_the_gates_and_the_first_order_places_no_entry(w, tmp_path):
    prepared(w)
    res = run(w, tmp_path / "ap", lease=CountingLease(ok_calls=1))                       # held() True for the pre-execute check, False at the pre-ORDER check
    assert creates(w) == [], "an entry order was placed although the lease was lost before it"
    assert res["flat_verified"] is True or res["result"] in ("DONE", "SKIPPED")


def test_RT1b_a_stop_marker_set_at_the_execute_boundary_places_no_entry(w, tmp_path):
    prepared(w)
    root = tmp_path / "ap"
    res = run(w, root, on_state=lambda s, src: ES.EngineState(str(root), w.clock.now).set("STOP", {"by": "owner"}) if s == "EXECUTING" else None)
    assert creates(w) == [], "an entry order was placed after STOP was set"
    assert all(p["size"] == 0 for p in w.fake.positions.values())


def test_RT1c_a_stop_signal_before_the_order_places_no_entry(w, tmp_path, stop_state):
    prepared(w)
    res = run(w, tmp_path / "ap", on_state=lambda s, src: AP._STOP.set() if s == "EXECUTING" else None)
    assert creates(w) == [] and res["result"] in ("DONE", "SKIPPED", "NOT_FLAT") and all(p["size"] == 0 for p in w.fake.positions.values())


def test_RT2_an_exception_in_the_authority_check_mid_session_never_escapes_and_unwinds_flat(w, tmp_path):
    prepared(w)
    res = run(w, tmp_path / "ap", lease=CountingLease(ok_calls=6, raises=True))          # the lease backend raises a non-OSError in the middle of the hold
    assert res["revoked_reason"].startswith("WATCHER_ERROR:RuntimeError") and res["flat_verified"] is True and res["result"] == "DONE"
    assert all(p["size"] == 0 for p in w.fake.positions.values())


def test_RT2b_an_exception_in_the_pre_order_check_is_a_refusal_not_a_pass(w, tmp_path):
    prepared(w)
    run(w, tmp_path / "ap", lease=CountingLease(ok_calls=1, raises=True))
    assert creates(w) == []


def test_RT2c_an_exception_out_of_a_session_forces_a_fresh_venue_proof_and_never_kills_the_loop(w, tmp_path, stop_state):
    calls = {"recover": 0, "once": 0}

    def recover_fn(*a, **k):
        calls["recover"] += 1
        return True, [], {"venue_read_complete": True, "reconciled_ms": 1}

    def boom(*a, **k):
        calls["once"] += 1
        raise RuntimeError("session blew up with legs possibly open")
    rc, _ = run_loop(w, tmp_path / "ap", boom, n=3, recover_fn=recover_fn)
    assert rc == 0 and calls["once"] == 3 and calls["recover"] == 3                      # recovery is re-run after EVERY exception, before the next session may start
    assert ES.EngineState(str(tmp_path / "ap"), w.clock.now).has("ALERT")                # three in a row -> ALERT


def test_RT8_a_kill_switch_that_cannot_be_created_is_retried_every_poll_and_raises_an_alert(w, tmp_path, monkeypatch):
    prepared(w)
    root = tmp_path / "ap"
    real = AP.AD.engage_kill_switch
    n = {"calls": 0}

    def flaky(path, reason=""):
        n["calls"] += 1
        if n["calls"] <= 3:
            raise OSError("read-only file system")
        return real(path, reason)
    monkeypatch.setattr(AP.AD, "engage_kill_switch", flaky)
    res = run(w, root, lease=CountingLease(ok_calls=5))
    assert n["calls"] >= 4 and os.path.exists(os.path.join(res["state_dir"], "MS_KILL"))   # retried until it worked
    assert ES.EngineState(str(root), w.clock.now).has("ALERT") and ES.EngineState(str(root), w.clock.now).read("ALERT")["code"] == "KILL_SWITCH_UNENGAGEABLE"
    assert res["flat_verified"] is True and all(p["size"] == 0 for p in w.fake.positions.values())


def test_RT3_recovery_proves_flat_for_EVERY_allowlisted_symbol_not_just_the_first(w):
    w.fake.inject_position("ETHUSDT", -1)                                                 # the manifest allows BTCUSDT and ETHUSDT; only ETH is open
    ok, why, info = AP.recover(w.env, inner=w.fake, clock=w.clock.now, state_root=str(w.tmp / "auto"))
    assert not ok and any(x.startswith("PERP_POSITION_OPEN:ETHUSDT") for x in why) and info["venue_read_complete"] and info["symbols"] == ["BTCUSDT", "ETHUSDT"]


def test_RT3b_an_open_perp_on_any_symbol_blocks_even_when_asked_about_another(w):
    w.fake.inject_position("ETHUSDT", -1)
    ok, why, _ = AP.recover(w.env, inner=w.fake, clock=w.clock.now, state_root=str(w.tmp / "auto"), symbol="BTCUSDT")
    assert not ok and any("ETHUSDT" in x for x in why)


def test_RT9_there_is_no_once_command_and_evaluate_never_starts_a_session(w, tmp_path):
    with pytest.raises(SystemExit):
        AP.main(["once"], env={})
    prepared(w)
    w.fake.funding_rate = "0.0100"
    res = AP.run_once(w.env, inner=w.fake, clock=w.clock.now, sleep=w.clock.sleep, out=lambda _m: None, state_root=str(w.tmp / "auto"), dry_run=True)
    assert res["result"] == "EVALUATED" and res["session_class"] in ("PROFIT_SEEKING", "TESTNET_MECHANICS", None) and not [r for r in w.fake.requests if r[0] == "POST"]


def test_RT9b_run_once_is_reachable_from_the_cli_only_as_a_dry_run_and_otherwise_only_through_the_loop():
    import ast
    tree = ast.parse(open(AP.__file__, encoding="utf-8").read())
    main_fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "main")
    uses = [n for n in ast.walk(main_fn) if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "run_once"]
    assert uses and all(any(k.arg == "dry_run" and getattr(k.value, "value", None) is True for k in c.keywords) for c in uses)


def test_RT4_every_heartbeat_has_a_unique_increasing_seq_even_when_the_state_does_not_change(w, tmp_path, stop_state):
    class Rec(AP.NullTelemetry):
        def __init__(self):
            self.hbs = []

        def heartbeat(self, hb, drain=True):
            self.hbs.append(hb)
    rec = Rec()
    ES.EngineState(str(tmp_path / "ap"), w.clock.now).set("STOP", {"by": "owner"})
    run_loop(w, tmp_path / "ap", NEVER, n=5, recover_fn=OK_RECOVERY, telemetry=rec)       # five idle iterations in the SAME state
    seqs = [h["seq"] for h in rec.hbs]
    assert len(seqs) >= 5 and seqs == sorted(set(seqs)), seqs


def test_RT5_the_loop_keeps_heartbeating_through_the_cooldown_and_the_hold(w, tmp_path, stop_state):
    class Rec(AP.NullTelemetry):
        def __init__(self):
            self.at = []

        def heartbeat(self, hb, drain=True):
            self.at.append(hb["at_ms"])
    rec = Rec()
    prepared(w)
    rc, _ = run_loop(w, tmp_path / "ap", AP.run_once, n=1, recover_fn=OK_RECOVERY, telemetry=rec, sleep=w.clock.sleep)      # one full session, then the 30-minute cooldown
    gaps = [b - a for a, b in zip(rec.at, rec.at[1:])]
    assert rc == 0 and rec.at and max(gaps) <= 40_000, max(gaps)                         # never silent for more than ~40 s (the monitor's stale threshold is 300 s)


def test_RT7_the_cooldown_notices_a_drain_or_stop_marker_at_once(w, tmp_path, stop_state):
    root = tmp_path / "ap"
    seen = {"slept": 0}

    def sleep(s):
        seen["slept"] += s
        if seen["slept"] >= 90:
            ES.EngineState(str(root), w.clock.now).set("DRAIN", {"requested": True})

    def once(*a, **k):
        return {"result": "DONE", "why": [], "flat_verified": True}
    run_loop(w, root, once, n=2, recover_fn=OK_RECOVERY, sleep=sleep)
    assert seen["slept"] < 300, seen["slept"]                                            # it did NOT sleep the whole 1800 s cooldown


def test_RT11_a_session_keeps_real_sleep_pacing_after_a_stop_signal(w, tmp_path, stop_state, monkeypatch):
    got = {}

    def once(env, **k):
        got["sleep"] = k["sleep"]
        return {"result": "SKIPPED", "why": []}
    slept = []
    monkeypatch.setattr(AP.time, "sleep", lambda s: slept.append(s))
    out = []
    AP.loop(w.env, root=str(tmp_path / "ap"), once=once, clock=w.clock.now, out=out.append, max_iterations=1, recover_fn=OK_RECOVERY, inner=w.fake, state_root=str(w.tmp / "auto"))
    AP._STOP.set()
    got["sleep"](0.25)                                                                    # the session's sleep is a REAL sleep, not an event wait that returns at once after SIGTERM
    assert slept == [0.25]


def test_RT12_drained_requires_a_fresh_venue_read_not_the_startup_flag(w, tmp_path, stop_state):
    root = tmp_path / "ap"
    AP.cmd_drain(str(root), w.clock.now, 0, out=lambda _m: None)
    state = {"n": 0}

    def recover_fn(*a, **k):
        state["n"] += 1
        return (state["n"] == 1, [] if state["n"] == 1 else ["PERP_POSITION_OPEN:BTCUSDT:-0.005"], {"venue_read_complete": True, "reconciled_ms": 1})       # flat at startup, then NOT flat
    run_loop(w, root, NEVER, n=2, recover_fn=recover_fn)
    assert state["n"] >= 2 and not ES.EngineState(str(root), w.clock.now).has("DRAINED")      # no DRAINED proof while the venue shows a position


def test_the_lease_holder_name_is_unique_per_process():
    env = {"SUPABASE_URL": "https://abcdefgh.supabase.co", "SUPABASE_SERVICE_ROLE_KEY": "k" * 30, "SUPABASE_OWNER_ID": "11111111-2222-3333-4444-555555555555", "FLY_MACHINE_ID": "m1"}
    h1 = AP.build_runtime(env, "/tmp/x1", lambda: 1)[1]._l.holder
    h2 = AP.build_runtime(env, "/tmp/x2", lambda: 1)[1]._l.holder
    assert h1 != h2 and h1.startswith("m1:") and h2.startswith("m1:")


# ---- the mutations the red team found SURVIVING
def test_an_ALERT_marker_set_during_a_session_revokes_it(w, tmp_path):
    prepared(w)
    root = tmp_path / "ap"
    res = run(w, root, lease=None, on_state=lambda s, src: None)
    root2 = tmp_path / "ap2"
    n = {"i": 0}
    real = w.clock.sleep

    def sleep(s):
        n["i"] += 1
        if n["i"] == 3:
            ES.EngineState(str(root2), w.clock.now).set("ALERT", {"code": "X"})
        real(s)
    prepared(w)
    res2 = AP.run_once(w.env, inner=w.fake, clock=w.clock.now, sleep=sleep, out=lambda _m: None, state_root=str(w.tmp / "auto2"), engine_root=str(root2))
    assert res2["revoked_reason"] == "ALERT" and res2["flat_verified"] is True


def test_recovery_reports_an_open_order_even_when_every_position_is_flat():
    class Ex:
        def venue_state(self):
            return {"complete": True, "positions": {"linear:BTCUSDT": 0.0, "spot:BTCUSDT": 0.0}, "orders": {"x-1": {"status": "New"}}, "foreign_open_orders": []}

        def filters(self, *_a):
            return type("F", (), {"qty_step": 0.001})()
    ok, why, complete = AP.venue_flat_proof(Ex(), "BTCUSDT", 0.0, 0.005)
    assert not ok and complete and any(x.startswith("OPEN_ORDERS") for x in why)
    class Ex2(Ex):
        def venue_state(self):
            return {"complete": True, "positions": {"linear:BTCUSDT": 0.0}, "orders": {}, "foreign_open_orders": ["999"]}
    assert not AP.venue_flat_proof(Ex2(), "BTCUSDT", None, None)[0]


def test_the_same_state_transition_does_not_bump_the_state_seq_but_a_change_does(tmp_path):
    st = ES.EngineState(str(tmp_path / "r"), lambda: 5)
    a = st.transition("PAUSED", "x")
    b = st.transition("PAUSED", "again")
    c = st.transition("ALERT", "now")
    assert a["seq"] == b["seq"] and c["seq"] == a["seq"] + 1


# ---------------------------------------------------------------- capital allocation and realised-only compounding wired into the session path
def _session_world(w):
    """A world in which the order-capable cycle RUNS a session. Round 22: it used to be an 80 bps 'profit' world; a profit session now needs QUALIFIED evidence and a calibrated model (the eligibility gate) and is
    NO_TRADE, so the lifecycle, capital-settlement and evidence plumbing these tests exercise runs the TESTNET_MECHANICS session (1 bp, the owner allowed it) that is what the executor really runs."""
    write_grant(w, min_funding_bps=0.5, allow_mechanics_sessions=True)
    w.fake.fee_rates = {"spot": ("0.001", "0.001"), "linear": ("0.0002", "0.00055")}
    w.fake.funding_rate, w.fake.funding_in_ms = "0.0001", 600_000


def _cycle_eng(w):
    eng = str(w.tmp / "eng")
    return AP.run_once(w.env, inner=w.fake, clock=w.clock.now, sleep=w.clock.sleep, out=[].append, state_root=str(w.tmp / "auto"), engine_root=eng), eng


def test_the_session_is_sized_by_the_allocator_not_the_grants_full_maximum_and_is_parked_for_settlement(w):
    _session_world(w)
    res, eng = _cycle_eng(w)
    assert res["result"] == "DONE" and res["flat_verified"] is True, res
    plan = json.load(open(os.path.join(res["state_dir"], "plan.json")))
    assert float(plan["notional_usd"]) <= 100.0 + 1e-6 < 400.0                                        # grant max is 400: the allocator, not the grant, sizes it (Round 22: a mechanics session is bound by the $100 mechanics cap; the compounded day-one 40% of the ceiling applies to profit sessions, which the eligibility gate stops today)
    assert res["allocation"]["binding_limit"] == "MECHANICS_ONLY"
    ev = json.load(open(os.path.join(res["state_dir"], "edge_verdict.json")))
    assert ev["allocation"]["notional_usd"] == "100.00" and ev["capital_settlement"]["status"] == "NO_PENDING"
    book = CT.anchored(eng).load()
    assert book["start_cash_usd"] is not None and book["pending"]["session_id"] == os.path.basename(res["state_dir"]) and book["realised_usd"] == "0"   # parked, NOT credited yet


def test_cash_the_ledger_cannot_explain_raises_an_alert_and_places_nothing(w):
    _session_world(w)
    eng = str(w.tmp / "eng")
    b = CT.anchored(eng)
    b.ensure_baseline("1")
    b.park("prev_session", "1", "0.50", True, True)                                                   # the venue's cash is thousands of dollars away from this
    w.fake.requests.clear()
    res, _ = _cycle_eng(w)
    assert res["result"] == "REFUSED" and res["why"] == ["ACCOUNT_CASH_UNEXPLAINED"] and not [r for r in w.fake.requests if r[0] == "POST"]
    assert AP.ES.EngineState(eng, w.clock.now).has("ALERT")
    assert CT.anchored(eng).load()["realised_usd"] == "0"                                          # and nothing unexplained was credited


def test_a_halted_book_keeps_the_engine_idle_until_the_owner_acknowledges(w):
    _session_world(w)
    eng = str(w.tmp / "eng")
    b = CT.anchored(eng)
    b.ensure_baseline("10000")
    bk = b.load()
    bk["halted"], bk["loss_streak"] = True, 4
    b._save(bk)
    w.fake.requests.clear()
    res, _ = _cycle_eng(w)
    assert res["result"] == "SKIPPED" and res["why"][0].startswith("ALLOCATION_ZERO") and not [r for r in w.fake.requests if r[0] == "POST"]
    assert b.acknowledge_halt() is True
    res, _ = _cycle_eng(w)
    assert res["result"] in ("DONE", "SKIPPED") and "ALLOCATION_ZERO" not in "".join(res["why"])


def test_the_lifetime_loss_stop_idles_the_engine_and_no_acknowledgement_resumes_it(w):
    """Red-team F2: the owner's acknowledgements resume the loss-streak halt and the drawdown stop; the LIFETIME stop (verified net realised <= -2 x the drawdown stop) is not theirs to lift."""
    _session_world(w)
    eng = str(w.tmp / "eng")
    b = CT.anchored(eng)
    b.ensure_baseline("10000")
    bk = b.load()
    bk["realised_usd"], bk["losses_usd"], bk["halted"], bk["loss_streak"] = "-23.00", "-23.00", True, 4
    b._save(bk)
    w.fake.requests.clear()
    res, _ = _cycle_eng(w)
    assert res["result"] == "SKIPPED" and "LIFETIME_LOSS_STOP" in "".join(res["why"]) and not [r for r in w.fake.requests if r[0] == "POST"], res["why"]
    assert b.acknowledge_halt() is True and b.acknowledge_drawdown() in (True, False)
    res, _ = _cycle_eng(w)
    assert res["result"] == "SKIPPED" and "LIFETIME_LOSS_STOP" in "".join(res["why"]) and not [r for r in w.fake.requests if r[0] == "POST"], "acknowledged, and still stopped"


# ---------------------------------------------------------------- the STALE EXECUTOR: a frozen process must be refused by the server before any entry order
class SimServer:
    """The server's lease record: one holder at a time, a fencing token that only grows, expiry on the SERVER clock."""

    def __init__(self):
        self.holder, self.token, self.now, self.expires = None, 0, 0.0, 0.0

    def client(self, name):
        return SimClient(self, name)


class SimClient:
    """Shaped like exec_evidence.LeaseClient; `held` answers from LOCAL memory (a deadline this process measured earlier), renew/acquire go to the server."""

    def __init__(self, srv, name):
        self.srv, self.name, self._tok, self.local_deadline = srv, name, None, None

    @property
    def held(self):
        return self._tok is not None and self.local_deadline is not None and self.local_deadline > 0          # local belief only: frozen clocks never expire it

    @property
    def token(self):
        return self._tok

    def acquire(self):
        s = self.srv
        if s.holder not in (None, self.name) and s.expires > s.now:
            return False
        s.token += 1
        s.holder, s.expires, self._tok, self.local_deadline = self.name, s.now + 30, s.token, 1.0
        return True

    def renew(self):
        s = self.srv
        if self._tok is None or s.holder != self.name or s.token != self._tok or s.expires <= s.now:
            self._tok = None
            return False
        s.expires = s.now + 30
        return True


def _stale_world(w):
    prepared(w)
    srv = SimServer()
    a, b = AP.RenewingLease(srv.client("A"), renew_every_s=1e9), AP.RenewingLease(srv.client("B"), renew_every_s=1e9)    # renew_every huge: the cheap check NEVER renews
    assert a.ensure() is True and a.token == 1
    srv.now = 100.0                                                       # process A is frozen; its lease expires on the server
    assert b.ensure() is True and b.token == 2                            # process B legitimately takes over with a NEWER fencing token
    return srv, a, b


def test_a_frozen_process_still_believes_in_its_lease_but_the_strong_check_asks_the_server(w, tmp_path):
    srv, a, b = _stale_world(w)
    manifest = S._load_capability_manifest()[0]
    assert a.held() is True                                               # the cheap local check is fooled: this is the bug the strong check exists for
    assert AP.revocation_reason(w.clock.now(), manifest, str(tmp_path), a) is None
    assert AP.revocation_reason(w.clock.now(), manifest, str(tmp_path), a, strong=True) == "LEASE_LOST"
    assert a.reason == "renew_failed_pre_order" and a.token is None and a.held() is False      # and it stays lost: held() never re-wins
    assert AP.revocation_reason(w.clock.now(), manifest, str(tmp_path), b, strong=True) is None  # the legitimate holder passes, and renewing keeps its token


def test_a_woken_stale_executor_is_refused_before_a_single_order_is_sent(w, tmp_path):
    srv, a, b = _stale_world(w)
    w.fake.requests.clear()
    res = run(w, tmp_path / "eng", lease=a)
    assert res["result"] == "SKIPPED" and res["why"] == ["REVOKED_BEFORE_ORDER:LEASE_LOST"], res
    assert not [r for r in w.fake.requests if r[0] == "POST"], "a stale executor reached the order endpoint"


def test_an_unanswerable_lease_check_is_not_authority(w, tmp_path):
    class Exploding:
        token = 1

        def held(self):
            return True

        def held_fresh(self):
            raise RuntimeError("supabase unreachable")
    manifest = S._load_capability_manifest()[0]
    prepared(w)
    assert AP.revocation_reason(w.clock.now(), manifest, str(tmp_path), Exploding(), strong=True) == "LEASE_CHECK_ERROR"
    w.fake.requests.clear()
    res = run(w, tmp_path / "eng2", lease=Exploding())
    assert res["result"] == "SKIPPED" and res["why"] == ["REVOKED_BEFORE_ORDER:LEASE_CHECK_ERROR"] and not [r for r in w.fake.requests if r[0] == "POST"]


def test_a_lease_lost_between_the_start_of_execute_and_the_first_entry_order_places_nothing(w, tmp_path):
    class FreshOnce:
        """The server says yes for the pre-execute check and no from then on (another executor took over in between)."""
        token = 1

        def __init__(self):
            self.fresh_calls = 0

        def held(self):
            return True                                                   # the cheap local belief never changes

        def held_fresh(self):
            self.fresh_calls += 1
            return self.fresh_calls == 1
    prepared(w)
    lease = FreshOnce()
    w.fake.requests.clear()
    res = run(w, tmp_path / "eng3", lease=lease)
    assert lease.fresh_calls >= 2, "the per-order check never asked the server"
    entry_posts = [r for r in w.fake.requests if r[0] == "POST" and "/order/create" in str(r[1])]
    assert not entry_posts, f"an entry order was sent without fresh authority: {entry_posts}"
    assert res["orders"] == 0 if "orders" in res else True


# ---------------------------------------------------------------- the execution lifecycle as evidence (kinds/stages in order, guard-clean, no raw venue ids)
import exec_lifecycle as EL  # noqa: E402


def _collect(w, eng_name="eng_ev", **kw):
    got = []
    res = AP.run_once(w.env, inner=w.fake, clock=w.clock.now, sleep=w.clock.sleep, out=[].append, state_root=str(w.tmp / "auto"), engine_root=str(w.tmp / eng_name),
                      on_event=lambda kind, stage, payload, n=0: got.append((kind, stage, dict(payload), n)), **kw)
    return res, got


def test_a_session_emits_the_whole_lifecycle_in_order_and_every_event_passes_the_evidence_guard(w):
    _session_world(w)
    res, got = _collect(w)
    assert res["result"] == "DONE", res
    stages = [g[1] for g in got]
    order = ["CAPITAL_ALLOCATED", "PLAN_CREATED", "RISK_APPROVED", "LEASE_VERIFIED", "ORDER_VENUE_RECORD", "FILL_VENUE_RECORD", "VENUE_FLAT_VERIFIED", "COSTS_BOOKED", "SESSION_RECONCILED"]
    idx = [stages.index(x) for x in order]
    assert idx == sorted(idx), stages
    assert stages.count("ORDER_VENUE_RECORD") >= 2 and stages.count("FILL_VENUE_RECORD") >= 2                              # both legs entered and both were exited
    sid = os.path.basename(res["state_dir"])
    seen_ids = set()
    for kind, stage, payload, n in got:
        ev = XE.make_event(kind, {"stage": stage, **payload}, session_id=sid, occurred_at_ms=w.clock.now(), event_id=EL.event_id(sid, kind, stage, n))     # the REAL secret guard
        assert ev["kind"] in XE.KINDS and ev["event_id"] not in seen_ids
        seen_ids.add(ev["event_id"])


def test_the_executed_sessions_full_decision_record_and_reconciliation_outcome_are_durable_events(w):
    """Round 18 (execution): the full decision record lived only in ``<session dir>/decision.json`` on the volume, so losing it lost what was evaluated, under which limits, on which code and
    candidate; and the ledger-vs-venue reconciliation outcome was never recorded anywhere."""
    _session_world(w)
    res, got = _collect(w)
    assert res["result"] == "DONE", res
    stages = [g[1] for g in got]
    dec = [g for g in got if g[1] == "DECISION_RECORDED"]
    assert len(dec) == 1 and dec[0][0] == "DECISION" and stages.index("DECISION_RECORDED") < stages.index("CAPITAL_ALLOCATED")
    assert dec[0][2]["decision_id"] == res["decision_id"] and {"inputs_ref", "risk_limits", "candidate", "code_sha"} <= set(dec[0][2]), sorted(dec[0][2])
    on_disk = json.load(open(os.path.join(res["state_dir"], "decision.json")))
    assert on_disk["decision_id"] == dec[0][2]["decision_id"]                                                            # the durable event IS the file's record
    rc = next(g for g in got if g[1] == "SESSION_RECONCILED")[2]
    assert rc["venue_reconciled"] is True and rc["view_complete"] is True and rc["finding_codes"] == [] and rc["venue_flat_verified"] is True
    assert rc["cash_compared"] is False                                    # `venue_reconciled` compares executions, ledger fills, positions and fees; the cash balance is not part of it, and the event says so (the guard refuses "wallet" as a key)


def test_lifecycle_evidence_names_who_said_it_and_never_leaks_raw_venue_ids(w):
    _session_world(w)
    res, got = _collect(w)
    rec = json.load(open(os.path.join(res["state_dir"], "testnet_session.json")))
    blob = json.dumps(got, default=str)
    raw = [str(o["order_id"]) for o in rec["orders"] if o.get("order_id")] + [str(x["exec_id"]) for x in rec["executions"]] + [o["link_id"] for o in rec["orders"]]
    assert raw and not [r for r in raw if r in blob], "a raw venue identifier reached the evidence payload"
    src = {g[1]: g[2]["source"] for g in got}
    assert src["ORDER_VENUE_RECORD"] == "VENUE" and src["FILL_VENUE_RECORD"] == "VENUE" and src["VENUE_FLAT_VERIFIED"] == "VENUE"
    assert src["COSTS_BOOKED"] == "LEDGER" and src["PLAN_CREATED"] == "LOCAL" and src["RISK_APPROVED"] == "LOCAL"
    flat = next(g for g in got if g[1] == "VENUE_FLAT_VERIFIED")[2]
    assert flat["venue_flat_verified"] is True
    cost = next(g for g in got if g[1] == "COSTS_BOOKED")[2]
    assert "components only" in cost["note"]                                                                              # costs are not cash and not profit


def test_the_next_session_settles_the_previous_one_and_emits_the_settlement_and_the_capital_book(w):
    _session_world(w)
    res1, got1 = _collect(w)
    assert res1["result"] == "DONE" and not [g for g in got1 if g[1] == "CAPITAL_SETTLED"]
    w.clock.sleep(1800)                                                                                                   # a later cycle (the fake venue is flat again)
    w.fake.funding_rate, w.fake.funding_in_ms = "0.0001", 600_000
    res2, got2 = _collect(w)
    st = [g[1] for g in got2]
    if res2["result"] in ("DONE", "SKIPPED") and "CAPITAL_SETTLED" in st:
        ev = next(g for g in got2 if g[1] == "CAPITAL_SETTLED")
        assert ev[2]["source"] == "CASH" and ev[2]["for_session"] == os.path.basename(res1["state_dir"])
        book = next(g for g in got2 if g[1] == "CAPITAL_BOOK")
        assert book[0] == "DAILY_PNL" and "TESTNET SIMULATION" in book[2]["label"] and "verified_result_usd" in book[2]
        XE.make_event("DAILY_PNL", {"stage": "CAPITAL_BOOK", **book[2]}, session_id="s_settle_1", occurred_at_ms=1)
    else:
        pytest.fail(f"the second cycle did not settle the first: {res2['result']} {res2.get('why')} {st}")


def test_an_unexplained_cash_refusal_still_leaves_evidence(w):
    _session_world(w)
    eng = str(w.tmp / "eng_ev2")
    b = CT.anchored(eng)
    b.ensure_baseline("1")
    b.park("prev_session", "1", "0.50", True, True)
    res, got = _collect(w, "eng_ev2")
    # Round 18: the durable stream also carries the decision this refused session was about to execute (DECISION_RECORDED, emitted when the session starts), then the refusal's own evidence
    assert res["result"] == "REFUSED" and [g[1] for g in got] == ["DECISION_RECORDED", "CAPITAL_SETTLED"] and got[1][2]["status"] == "UNEXPLAINED_CASH"


def test_the_loop_routes_lifecycle_events_into_the_real_outbox_with_deterministic_ids(w, tmp_path):
    outbox = XE.Outbox(str(tmp_path / "ob"), clock=lambda: 1_794_312_000_000)                                              # a fixed clock: the same fact is the same content
    tel = AP.EvidenceTelemetry(outbox, CapturingSync(), w.clock.now)
    sid = "ms_auto_20261110T000000"
    eid = EL.event_id(sid, "PLAN", "PLAN_CREATED", 0)
    assert eid == f"{sid}:PLAN:PLAN_CREATED:0"
    tel.event("PLAN", {"stage": "PLAN_CREATED", "source": "LOCAL"}, sid, event_id=eid)
    tel.event("PLAN", {"stage": "PLAN_CREATED", "source": "LOCAL"}, sid, event_id=eid)                                      # a replay of the same fact
    assert tel.pending() == 1                                                                                              # suppressed, not stored twice
    with pytest.raises(XE.EventConflict):                                                                                  # the same id for a DIFFERENT fact is refused (and quarantined), never overwritten
        tel.event("PLAN", {"stage": "PLAN_CREATED", "source": "LOCAL", "notional_usd": "999.0000"}, sid, event_id=eid)
    assert tel.pending() == 1


# ---------------------------------------------------------------- Phase B: the read-only decision needs NO execution authority
def _tree(root):
    out = {}
    for dp, _d, fs in os.walk(root):
        for f in fs:
            p = os.path.join(dp, f)
            out[p] = (os.stat(p).st_size, os.stat(p).st_mtime_ns)
    return out


def _evaluate(w, **kw):
    return AP.run_once(w.env, inner=w.fake, clock=w.clock.now, sleep=w.clock.sleep, out=lambda _m: None, state_root=str(w.tmp / "auto"), dry_run=True, **kw)


def _measured_world(w, funding="0.0080"):
    w.fake.fee_rates = {"spot": ("0.001", "0.001"), "linear": ("0.0002", "0.00055")}
    w.fake.funding_rate, w.fake.funding_in_ms = funding, 600_000


def test_evaluate_needs_no_grant_no_capability_and_no_lease_and_writes_nothing(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch, grant=False)                                    # NO capability file, NO grant
    _measured_world(w)
    assert not os.path.exists(w.cap_path) and not os.path.exists(AP.grant_path())
    before = _tree(str(tmp_path))
    w.fake.requests.clear()
    res = _evaluate(w, notional_usd="200")
    assert res["result"] == "EVALUATED" and res["decision"] == "ELIGIBLE_FOR_OWNER_TESTNET_DECISION", res
    assert res["authority"]["grant"] == "ABSENT" and res["authority"]["capability"] == "ABSENT" and res["read_only"] is True
    assert {"CAPABILITY_FILE", "UNATTENDED_GRANT", "EXECUTOR_LEASE", "RISK_APPROVAL", "VENUE_FLAT_PROOF"} <= set(res["authority_gaps"])        # eligibility is NOT authority
    assert "eligibility is not authority" in " ".join(res["why"]) and res["edge"]["session_class"] == "PROFIT_CANDIDATE"
    assert not [r for r in w.fake.requests if r[0] == "POST"], "a read-only evaluation placed an order"
    assert _tree(str(tmp_path)) == before, "a read-only evaluation wrote something"


def test_the_decision_classes_are_unambiguous_and_economics_decide_them(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch, grant=False)
    _measured_world(w, funding="0.0001")                                             # 1 bp: far below the cost chain
    assert _evaluate(w, notional_usd="200")["decision"] == "REJECT_UNECONOMIC"
    _measured_world(w)
    w.fake.fee_rates = None                                                          # the key cannot read its own fee tier: ASSUMED, so never a PASS even at 80 bps
    res = _evaluate(w, notional_usd="200")
    assert res["decision"] == "INCONCLUSIVE" and any("ASSUMED_INPUTS_CANNOT_PASS" in x for x in res["why"])
    _measured_world(w)
    res = _evaluate(w, notional_usd="200", min_funding_bps="100")                    # the owner's own floor is above this funding: economics clear, the floor does not
    assert res["decision"] == "SKIP_NO_OPPORTUNITY" and res["edge"]["session_class"] == "PROFIT_CANDIDATE"
    w.fake.funding_in_ms = 30_000                                                    # a settlement 30 s away cannot be held across
    res = _evaluate(w, notional_usd="200")
    assert res["decision"] == "SKIP_NO_OPPORTUNITY" and "edge" not in res and res["funding_check"].startswith("SETTLEMENT_TOO_CLOSE")


def test_missing_credentials_or_bad_parameters_are_wait_for_data_and_send_nothing(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch, grant=False)
    _measured_world(w)
    w.fake.requests.clear()
    res = AP.run_once({}, inner=w.fake, clock=w.clock.now, dry_run=True)
    assert res["decision"] == "WAIT_FOR_DATA" and "NO_DEDICATED_CREDENTIALS" in res["why"][0] and not w.fake.requests
    for kw, why in (({"notional_usd": "501"}, "EVALUATION_PARAMETERS_OUT_OF_RANGE"), ({"notional_usd": "0"}, "EVALUATION_PARAMETERS_OUT_OF_RANGE"), ({"notional_usd": "x"}, "EVALUATION_PARAMETERS_INVALID"),
                    ({"min_funding_bps": "-1"}, "EVALUATION_PARAMETERS_OUT_OF_RANGE"), ({"symbol": "btc usdt"}, "SYMBOL_INVALID")):
        r = _evaluate(w, **kw)
        assert r["decision"] == "WAIT_FOR_DATA" and r["why"] == [why], (kw, r)
    assert not [r for r in w.fake.requests if r[0] == "POST"]


def test_a_grant_changes_the_defaults_never_the_economics(tmp_path, monkeypatch):
    a = World(tmp_path / "a", monkeypatch, grant=False)
    _measured_world(a)
    ra = _evaluate(a, notional_usd="200")                                            # evaluated BEFORE any capability or grant exists (the patched paths are process-wide)
    b = World(tmp_path / "b", monkeypatch)
    _measured_world(b)
    write_grant(b, max_notional_usd=200.0, min_funding_bps=0.5)
    rb = _evaluate(b)                                                                # defaults come from the grant: BTCUSDT, $200, floor 0.5 bp
    assert rb["authority"]["grant"] == "VALID" and rb["notional_usd"] == "200.00" and rb["owner_funding_floor_bps"] == 0.5
    assert ra["decision"] == rb["decision"] and ra["edge"]["digest"] == rb["edge"]["digest"], "the same inputs must give the same economic answer with or without authority"
    assert "UNATTENDED_GRANT" in ra["authority_gaps"] and "UNATTENDED_GRANT" not in rb["authority_gaps"]


def test_the_order_capable_cycle_still_requires_every_authority_gate(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch, grant=False)
    _measured_world(w)
    w.fake.requests.clear()
    res = AP.run_once(w.env, inner=w.fake, clock=w.clock.now, sleep=w.clock.sleep, out=lambda _m: None, state_root=str(w.tmp / "auto"))
    assert res["result"] == "REFUSED" and any("OWNER_CAPABILITY_FILE" in x for x in res["why"]) and not [r for r in w.fake.requests if r[0] == "POST"]
    w.write_capability()
    res = AP.run_once(w.env, inner=w.fake, clock=w.clock.now, sleep=w.clock.sleep, out=lambda _m: None, state_root=str(w.tmp / "auto"))
    assert res["result"] == "REFUSED" and "GRANT_MISSING" in res["why"] and not [r for r in w.fake.requests if r[0] == "POST"]


def test_the_evaluate_command_exposes_symbol_notional_and_floor_and_nothing_that_grants_authority():
    a = AP._parser().parse_args(["evaluate", "--symbol", "ETHUSDT", "--notional-usd", "150", "--min-funding-bps", "3"])
    assert (a.cmd, a.symbol, a.notional_usd, a.min_funding_bps) == ("evaluate", "ETHUSDT", "150", "3")
    evaluate = next(sp for sp in AP._parser()._subparsers._group_actions[0].choices.values() if sp.prog.endswith("evaluate"))
    assert not [o for act in evaluate._actions for o in act.option_strings if any(w in o for w in ("grant", "capability", "lease", "confirm", "execute", "order"))]


def test_a_crash_between_execute_and_the_end_of_the_session_leaves_the_cash_pending_for_settlement(w, tmp_path, monkeypatch):
    """REPRODUCED by review: the capital book was only parked after execute returned; a crash after orders left nothing to settle and the loss was invisible."""
    _session_world(w)
    real_main = S.main

    def dying(argv, **kw):
        if "execute" in argv:
            raise RuntimeError("process killed after orders were placed")
        return real_main(argv, **kw)
    monkeypatch.setattr(AP.S, "main", dying)
    eng = str(w.tmp / "eng_crash")
    with pytest.raises(RuntimeError):
        AP.run_once(w.env, inner=w.fake, clock=w.clock.now, sleep=w.clock.sleep, out=[].append, state_root=str(w.tmp / "auto"), engine_root=eng)
    p = CT.anchored(eng).load()["pending"]
    assert p is not None and p["flat_verified"] is False and p["ledger_net_known_usd"] is None, "no conservative placeholder was parked before execution"
    r = CT.anchored(eng).settle("1")                                        # the venue's cash is thousands lower than this: a LOSS, booked in full
    assert r["status"] == "SETTLED" and CT.anchored(eng).load()["loss_streak"] == 1


# ---------------------------------------------------------------- the durable, reproducible decision record
def test_every_evaluation_carries_a_decision_record_whose_id_is_reproducible_from_its_inputs(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch, grant=False)
    _measured_world(w)
    r1 = _evaluate(w, notional_usd="200")
    r2 = _evaluate(w, notional_usd="200")
    rec1, rec2 = r1["decision_record"], r2["decision_record"]
    assert rec1["decision_id"].startswith("dec_") and rec1 == rec2, "the same market inputs, size and code must give the same record"
    assert rec1["decision"] == "ELIGIBLE_FOR_OWNER_TESTNET_DECISION" and rec1["fee_tier"] == "VENUE_STATED" and rec1["inputs_ref"] and rec1["edge"]["edge_ref"] and rec1["data_scope"].startswith("TESTNET_DATA")
    assert rec1["capital_required_usd"] == "408.00" and rec1["worst_credible_loss_usd"]["basis"] == "POLICY_PER_SESSION_LOSS_CEILING_NOT_A_MODEL" and "UNATTENDED_GRANT" in rec1["authority_gaps"]
    assert "inputs_digest" in r1 and len(r1["inputs_digest"]) == 64                                  # the full digest stays in the local result; the record carries a 16-hex reference


def test_a_different_market_state_size_fee_tier_or_code_can_never_share_a_decision_id(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch, grant=False)
    _measured_world(w)
    base = _evaluate(w, notional_usd="200")["decision_record"]["decision_id"]
    assert _evaluate(w, notional_usd="150")["decision_record"]["decision_id"] != base                  # size
    w.fake.fee_rates = {"spot": ("0.001", "0.001"), "linear": ("0.0003", "0.0006")}
    assert _evaluate(w, notional_usd="200")["decision_record"]["decision_id"] != base                  # fee tier
    _measured_world(w, funding="0.0090")
    assert _evaluate(w, notional_usd="200")["decision_record"]["decision_id"] != base                  # funding observation
    _measured_world(w)
    w.clock.sleep(5)
    assert _evaluate(w, notional_usd="200")["decision_record"]["decision_id"] != base                  # a later observation
    _measured_world(w)
    w.env = {**w.env, "GIT_SHA": "a" * 40}
    assert _evaluate(w, notional_usd="200")["decision_record"]["decision_id"] != base                  # code revision


def test_the_decision_record_passes_the_evidence_guard_and_is_stored_once_on_explicit_opt_in(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch, grant=False)
    _measured_world(w)
    rec = _evaluate(w, notional_usd="200")["decision_record"]
    XE.make_event("DECISION", {"stage": "DECISION_RECORDED", **rec}, session_id=None, occurred_at_ms=rec["observed_ms"], event_id=EL.event_id("decision", "DECISION", rec["decision_id"], 0))   # the REAL secret guard
    d = str(tmp_path / "ob")
    assert not os.path.exists(d), "evaluate wrote an outbox without the opt-in flag"
    assert AP.record_decision(d, rec, w.clock.now()) == "STORED"
    assert AP.record_decision(d, rec, w.clock.now() + 99_000) == "DUPLICATE"                          # the same decision, even much later: byte-identical, never a second record
    assert XE.Outbox(d).pending == 1
    assert AP.record_decision(d, {**rec, "decision": "REJECT_UNECONOMIC"}, w.clock.now()).startswith("REFUSED")      # the same id with a different fact is a conflict, never an overwrite


def test_every_decision_class_yields_a_valid_record(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch, grant=False)
    for setup, expect in ((lambda: _measured_world(w, funding="0.0001"), "REJECT_UNECONOMIC"), (lambda: (_measured_world(w), setattr(w.fake, "fee_rates", None)), "INCONCLUSIVE"),
                          (lambda: (_measured_world(w), setattr(w.fake, "funding_in_ms", 30_000)), "SKIP_NO_OPPORTUNITY")):
        setup()
        res = _evaluate(w, notional_usd="200")
        rec = res["decision_record"]
        assert rec["decision"] == expect and rec["decision_id"].startswith("dec_")
        XE.make_event("DECISION", {"stage": "DECISION_RECORDED", **rec}, session_id=None, occurred_at_ms=rec["observed_ms"] or w.clock.now(), event_id=EL.event_id("decision", "DECISION", rec["decision_id"], 0))
    r = AP.run_once({}, inner=w.fake, clock=w.clock.now, dry_run=True)                               # WAIT_FOR_DATA (no credentials): a record without inputs, still deterministic and valid
    assert r["decision"] == "WAIT_FOR_DATA" and r["decision_record"]["inputs_ref"] is None and r["decision_record"]["decision_id"].startswith("dec_")


def test_the_order_capable_cycle_carries_the_same_decision_id_shape_into_its_result_and_event(w, tmp_path):
    _session_world(w)
    res, _eng = _cycle_eng(w)
    assert res["result"] == "DONE" and str(res["decision_id"]).startswith("dec_"), res
    w.clock.sleep(1800)
    res2, _ = _cycle_eng(w)
    assert str(res2.get("decision_id", "dec_")).startswith("dec_") and res2["decision_id"] != res["decision_id"]          # a later observation is a different decision


def test_a_decision_record_is_refused_inside_the_repository_the_state_dir_or_the_engine_root(tmp_path, monkeypatch):
    """Red-team MED: --record-to could point at the checkout, bot/state or the owner's authority directory; evidence must never be written beside a grant or capability."""
    w = World(tmp_path, monkeypatch, grant=False)
    _measured_world(w)
    rec = _evaluate(w, notional_usd="200")["decision_record"]
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    for bad in (os.path.join(AP.BOT, "evidence_out"), os.path.join(AP.BOT, "state", "x"), os.path.dirname(AP.BOT), str(tmp_path / "home" / ".ai-agent-control" / "ev")):
        existed = os.path.exists(bad)
        r = AP.record_decision(bad, rec, w.clock.now())
        assert r.startswith("REFUSED:DIRECTORY_INSIDE_"), (bad, r)
        assert os.path.exists(bad) == existed, bad                                   # a refusal creates nothing
    link = tmp_path / "innocent"
    os.symlink(AP.BOT, str(link))
    assert AP.record_decision(str(link / "ev2"), rec, w.clock.now()).startswith("REFUSED:DIRECTORY_INSIDE_")        # a symlink does not smuggle it in
    assert AP.record_decision(str(tmp_path / "fine"), rec, w.clock.now()) == "STORED"


def test_the_decision_record_carries_every_field_the_owner_needs_and_the_scenarios_are_exact_arithmetic(tmp_path, monkeypatch):
    from decimal import Decimal as D
    w = World(tmp_path, monkeypatch, grant=False)
    _measured_world(w)
    res = _evaluate(w, notional_usd="200")
    r = res["decision_record"]
    for k in ("decision_id", "observed_utc", "code_sha", "inputs_ref", "candidate", "venue", "settlement", "market_inputs", "fee_tier", "edge", "scenarios", "capital_required_usd",
              "risk_limits", "worst_credible_loss_usd", "portfolio_context", "authority_gaps", "data_freshness_ms", "decision", "reasons", "symbol", "notional_usd", "observed_ms"):
        assert k in r, k
    assert r["observed_utc"].endswith("Z") and r["venue"]["id"] == "testnet" and r["portfolio_context"]["status"] == "NO_CAPITAL_BOOK"
    mi = r["market_inputs"]
    assert mi["perp_bid"] and mi["perp_ask"] and mi["spot_bid"] and mi["spot_ask"] and mi["perp_depth_usd_within_10bps"] is not None and mi["perp_book_age_ms"] is not None
    assert r["data_freshness_ms"]["oldest_book_age"] is not None and r["settlement"]["next_ms"] and r["settlement"]["rate_bps"]
    rl = r["risk_limits"]
    assert rl["per_session_loss_ceiling_usd"] and D(rl["aggregate_notional_ceiling_usd"]) == 500 and rl["min_net_edge_bps"] and D(rl["funding_haircut"]) == D("0.30") and rl["entries_stop_at_consecutive_losses"] == 4
    sc = r["scenarios"]
    assert sc["label"] == "ARITHMETIC_ON_DECLARED_INPUTS"
    n, rate = res["edge"]["settlements_in_hold"], D(str(res["funding_bps"])).quantize(D("0.0001"))
    cost = sum(D(str(v)) for v in res["edge"]["costs_bps"].values())
    assert D(sc["gross_expected_funding_bps"]) == (rate * n).quantize(D("0.0001")) and D(sc["total_cost_bps"]) == cost.quantize(D("0.0001"))
    assert abs(D(sc["stress_half_funding_net_bps"]) - (rate * n / 2 - cost)) < D("0.001") and abs(D(sc["gross_before_haircut_net_bps"]) - (rate * n - cost)) < D("0.001")
    assert D(sc["conservative_expected_net_bps"]) == D(str(res["edge"]["net_edge_bps"])).quantize(D("0.0001")) and D(sc["conservative_expected_net_bps"]) <= D(sc["gross_before_haircut_net_bps"])
    # reproducible: the same inputs and code give the same identity, and the decision needs no grant, capability or lease
    _measured_world(w)
    assert _evaluate(w, notional_usd="200")["decision_record"]["decision_id"] == r["decision_id"] and "UNATTENDED_GRANT" in r["authority_gaps"]


# ---------------------------------------------------------------- the owner's COST OF CAPITAL: stated in the environment, never defaulted (the old gate priced an unstated one as free)
CASH_ENV = (EG.ENV_CASH_APR, EG.ENV_CASH_SOURCE, EG.ENV_CASH_STATED_UTC)


def _no_cash(w, **replace):
    for k in CASH_ENV:
        w.env.pop(k, None)
    w.env.update(replace)


def test_an_unstated_cost_of_capital_makes_even_an_80_bps_print_inconclusive_in_the_read_only_decision(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch, grant=False)
    _measured_world(w)                                                                # 80 bps of funding, a MEASURED fee tier: only the cost of capital is missing
    _no_cash(w)
    w.fake.requests.clear()
    res = _evaluate(w, notional_usd="200")
    assert res["decision"] == "INCONCLUSIVE" and res["edge"]["session_class"] == "INCONCLUSIVE", res["why"]
    assert any(x.startswith("COST_OF_CAPITAL_UNKNOWN:") for x in res["why"]) and res["edge"]["net_edge_bps"] is None
    assert res["cost_of_capital"]["state"] == "UNSTATED" and res["cost_of_capital"]["usable"] is False and res["cost_of_capital"]["apr"] is None
    cb = res["decision_record"]["edge"]["capital_basis"]
    assert cb["state"] == "UNSTATED" and cb["cash_alternative_apr"] is None and cb["opportunity_cost_bps"] is None and cb["committed_usd"] == "408.00"
    assert res["decision_record"]["market_inputs"]["cost_of_capital_state"] == "UNSTATED"
    assert not [r for r in w.fake.requests if r[0] == "POST"]


def test_a_hopeless_edge_stays_rejected_when_the_cost_of_capital_is_unstated(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch, grant=False)
    _measured_world(w, funding="0.0001")                                              # 1 bp: no cost of capital could rescue it
    _no_cash(w)
    res = _evaluate(w, notional_usd="200")
    assert res["decision"] == "REJECT_UNECONOMIC" and any("BEFORE_ANY_COST_OF_CAPITAL" in x for x in res["why"])


def test_a_stated_cost_of_capital_is_charged_on_the_committed_capital_and_shown_in_the_decision_record(tmp_path, monkeypatch):
    from decimal import Decimal as D
    w = World(tmp_path, monkeypatch, grant=False)
    _measured_world(w)
    res = _evaluate(w, notional_usd="200")
    assert res["decision"] == "ELIGIBLE_FOR_OWNER_TESTNET_DECISION" and res["cost_of_capital"] == {
        **res["cost_of_capital"], "state": "STATED", "usable": True, "apr": "0.04", "source": "OWNER_STATED:test-fixture", "provenance": "modelled"}
    cb = res["decision_record"]["edge"]["capital_basis"]
    assert (cb["state"], cb["cash_alternative_apr"], cb["cash_alternative_source"], cb["cash_alternative_provenance"], cb["multiple_of_notional"], cb["committed_usd"]) == (
        "STATED", "0.040000", "OWNER_STATED:test-fixture", "modelled", "2.0400", "408.00")
    hold_s = res["hold_s"]
    expected_bps = D("0.04") * D("2.04") * D(hold_s) / D(86_400) / D(365) * D(10_000)
    assert abs(D(res["edge"]["costs_bps"]["opportunity_cost"]) - expected_bps) < D("1e-9") and expected_bps > 0           # a real, positive, hold-proportional charge


@pytest.mark.parametrize("change,state,problem", [
    ({EG.ENV_CASH_STATED_UTC: "2026-09-01"}, "STALE", "CASH_ALTERNATIVE_STALE"),                       # 70 days before the fixed test clock
    ({EG.ENV_CASH_STATED_UTC: "2026-12-31"}, "FUTURE_DATED", "CASH_ALTERNATIVE_DATED_IN_THE_FUTURE"),
    ({EG.ENV_CASH_APR: "4%"}, "INVALID", "CASH_ALTERNATIVE_RATE_INVALID"),
    ({EG.ENV_CASH_APR: "4"}, "INVALID", "CASH_ALTERNATIVE_INVALID"),                                      # a unit slip (400%/yr) is refused, not obeyed
])
def test_a_stale_future_dated_or_malformed_statement_is_unknown_and_says_why(tmp_path, monkeypatch, change, state, problem):
    w = World(tmp_path, monkeypatch, grant=False)
    _measured_world(w)
    w.env.update(change)
    res = _evaluate(w, notional_usd="200")
    assert res["decision"] == "INCONCLUSIVE" and res["cost_of_capital"]["state"] == state and res["cost_of_capital"]["usable"] is False
    assert any(problem in x for x in res["cost_of_capital"]["problems"]) and res["edge"]["net_edge_bps"] is None


def test_a_partial_statement_is_invalid_not_defaulted(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch, grant=False)
    _measured_world(w)
    del w.env[EG.ENV_CASH_STATED_UTC]
    res = _evaluate(w, notional_usd="200")
    assert res["decision"] == "INCONCLUSIVE" and res["cost_of_capital"]["state"] == "INVALID" and any("INCOMPLETE" in x for x in res["why"])


def test_the_order_capable_loop_places_no_profit_seeking_session_while_the_cost_of_capital_is_unstated(w):
    write_grant(w, min_funding_bps=0.5, allow_mechanics_sessions=False)
    w.fake.fee_rates = {"spot": ("0.001", "0.001"), "linear": ("0.0002", "0.00055")}
    w.fake.funding_rate, w.fake.funding_in_ms = "0.0080", 600_000                                      # the same 80 bps that DID open a PROFIT_SEEKING session when the cost of capital was stated
    _no_cash(w)
    res, _ = cycle(w)
    assert res["result"] == "SKIPPED" and res["why"][0] == "EDGE_INCONCLUSIVE" and any(x.startswith("COST_OF_CAPITAL_UNKNOWN:") for x in res["why"])
    assert not [r for r in w.fake.requests if r[0] == "POST"], "an order was placed on capital priced as free"


def test_stating_the_cost_of_capital_makes_the_edge_a_profit_candidate_but_the_eligibility_gate_still_says_no_trade(w):
    write_grant(w, min_funding_bps=0.5, allow_mechanics_sessions=False)
    w.fake.fee_rates = {"spot": ("0.001", "0.001"), "linear": ("0.0002", "0.00055")}
    w.fake.funding_rate, w.fake.funding_in_ms = "0.0080", 600_000
    res, _ = cycle(w)                                                                                  # the fixture's world states 4%/yr
    assert res["result"] == "SKIPPED" and res["why"][0] == "NO_TRADE" and res["eligibility"]["profile"] == "PROFIT_SEEKING"
    ev = {"edge": res["edge"]}
    assert ev["edge"]["capital"]["state"] == "STATED" and ev["edge"]["capital"]["cash_alternative_apr"] == "0.04" and ev["edge"]["capital"]["opportunity_cost_bps"] is not None      # the local verdict keeps full precision
    shipped = AP.evidence_edge(ev["edge"])["capital_basis"]                                           # the evidence payload carries the rounded basis (the guard refuses long digit runs)
    assert shipped["state"] == "STATED" and shipped["cash_alternative_apr"] == "0.040000" and shipped["cash_alternative_provenance"] == "modelled" and shipped["committed_usd"] is not None


def test_the_status_command_tells_the_owner_the_state_of_the_cost_of_capital(w, tmp_path, capsys):
    assert AP.main(["status", "--root", str(tmp_path / "ap")], env=w.env) == 0
    stated = json.loads(capsys.readouterr().out)["cost_of_capital"]
    assert stated["state"] == "STATED" and stated["usable"] is True and stated["apr"] == "0.04" and stated["names"] == list(CASH_ENV)
    _no_cash(w)
    assert AP.main(["status", "--root", str(tmp_path / "ap")], env=w.env) == 0
    unstated = json.loads(capsys.readouterr().out)["cost_of_capital"]
    assert unstated["state"] == "UNSTATED" and unstated["usable"] is False and unstated["apr"] is None and unstated["problems"] == ["NO_CASH_ALTERNATIVE_STATED"]


def test_the_environment_the_image_and_the_fly_config_ship_never_states_a_cost_of_capital_for_the_owner():
    """The owner states it; nothing in the repository's deployment files may default it (a default would be the old free-capital loophole again, one layer out)."""
    bot = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for name in ("Dockerfile.autopilot", "Dockerfile", "Dockerfile.drill", "Dockerfile.research", "fly.autopilot.toml", "fly.toml", "fly.drill.toml", "fly.research.toml"):
        text = open(os.path.join(bot, name), encoding="utf-8").read()
        assert not any(k in text for k in CASH_ENV), f"{name} ships a default cost of capital"


def test_a_different_capital_state_is_a_different_decision_and_an_unreadable_book_is_stated(tmp_path, monkeypatch):
    import ms_capital as CC
    ctx = AP._decision_context(str(tmp_path / "none"), 1.0)
    assert ctx["portfolio"] == {"status": "NO_CAPITAL_BOOK"} and ctx["venue"]["scope"] == "TESTNET_FAKE_MONEY"
    root = tmp_path / "book"
    b = CT.anchored(str(root))
    b.ensure_baseline("10000")
    ctx2 = AP._decision_context(str(root), 1.0)
    assert ctx2["portfolio"]["status"] == "BOOK" and ctx2["portfolio"]["halted"] in (False, None)
    base = {"decision": "REJECT_UNECONOMIC", "symbol": "BTCUSDT", "notional_usd": "200", "inputs_digest": "d" * 64, "observed_ms": 1_794_312_000_000, "edge": None}
    a = EL.decision_record(base, code_sha="c" * 40, **ctx)["decision_id"]
    assert a == EL.decision_record(base, code_sha="c" * 40, **ctx)["decision_id"] and a != EL.decision_record(base, code_sha="c" * 40, **ctx2)["decision_id"]
    (root / "capital.json").write_text("{broken")                                        # the view is damaged but the lineage is intact: the book is the lineage's, and the record says it had to be rebuilt
    rebuilt = AP._decision_context(str(root), 1.0)["portfolio"]
    assert rebuilt["status"] == "BOOK" and rebuilt["anchor"]["view_rebuilt"] == "VIEW_UNREADABLE", rebuilt
    assert (root / "capital.json").read_text() == "{broken", "a read-only decision never rewrites the book"
    lone = tmp_path / "lone"
    os.makedirs(lone)
    (lone / "capital.json").write_text("{broken")
    monkeypatch.setenv("CAPITAL_ANCHOR_DIR", str(tmp_path / "an_empty_anchor_dir"))       # no lineage at all: the loop refuses this book, so the record names the refusal
    assert AP._decision_context(str(lone), 1.0)["portfolio"] == {"status": "BOOK_REFUSED", "refusal": "ACCOUNT_NOT_RESOLVED"}


def test_the_read_only_decision_names_an_open_review_a_halt_an_unreadable_book_and_that_cash_was_not_checked(tmp_path, monkeypatch):
    """Review R2-6: with an unexplained-cash review open the loop refuses (ACCOUNT_CASH_UNEXPLAINED), yet the read-only ``evaluate`` said ``halted: false`` and ELIGIBLE with no word of it, and its
    authority gaps never said it had not looked at the cash ($200 of notional commits $408). The stops the book already holds are named in the record; eligibility stays a statement about economics."""
    import json as _json
    import ms_capital as CC
    w = World(tmp_path, monkeypatch, grant=False)
    _measured_world(w)
    clean = _evaluate(w, notional_usd="200")
    assert clean["decision"] == "ELIGIBLE_FOR_OWNER_TESTNET_DECISION" and clean["decision_record"]["capital_required_usd"] == "408.00"
    assert "ACCOUNT_CASH_NOT_CHECKED" in clean["authority_gaps"] and "ACCOUNT_CASH_NOT_CHECKED" in clean["decision_record"]["authority_gaps"]      # evaluate never reads the account's cash
    assert not any(g.startswith("CAPITAL_") for g in clean["authority_gaps"]), "no book, no stop"

    root = tmp_path / "book_review"
    book = CT.anchored(str(root))
    book.ensure_baseline("1")
    assert book.park("prev_session", "1", "0.50", True, True)
    assert book.settle("10000")["status"] == "UNEXPLAINED_CASH"                    # the review is persisted IN the book
    res = _evaluate(w, notional_usd="200", engine_root=str(root))
    assert res["decision"] == "ELIGIBLE_FOR_OWNER_TESTNET_DECISION", "eligibility is an economics statement: the stop is NAMED, not silently converted"
    assert "CAPITAL_REVIEW_OPEN" in res["authority_gaps"] and "CAPITAL_REVIEW_OPEN" in res["decision_record"]["authority_gaps"], res["authority_gaps"]
    assert res["decision_record"]["portfolio_context"]["review_required"] == "ACCOUNT_CASH_UNEXPLAINED"
    assert res["decision_record"]["decision_id"] != clean["decision_record"]["decision_id"]            # a different capital state is a different decision

    doc = book.load()                                                                  # (test-only state injection, THROUGH the lineage: an edit of the view file would be VIEW_TAMPERED)
    doc["review_required"], doc["halted"] = None, True
    book._save(doc, "BASELINE")
    halted = _evaluate(w, notional_usd="200", engine_root=str(root))
    assert "CAPITAL_BOOK_HALTED" in halted["authority_gaps"] and "CAPITAL_REVIEW_OPEN" not in halted["authority_gaps"]

    doc = book.load()
    doc["halted"] = False
    doc["realised_usd"] = "-22.20"
    book._save(doc, "BASELINE")
    stopped = _evaluate(w, notional_usd="200", engine_root=str(root))
    assert "CAPITAL_LIFETIME_LOSS_STOP" in stopped["authority_gaps"] and stopped["decision_record"]["portfolio_context"]["lifetime_loss_stop_active"] is True, "the stop no acknowledgement lifts is named"
    from decimal import Decimal as D
    assert D(stopped["decision_record"]["risk_limits"]["lifetime_loss_stop_usd"]) == D("22.20") == 2 * D(stopped["decision_record"]["risk_limits"]["drawdown_stop_usd"])

    tampered = _json.load(open(book.path))
    tampered["realised_usd"] = "0"                                                     # someone edits the view to lift the lifetime stop
    with open(book.path, "w") as fh:
        _json.dump(tampered, fh)
    before = open(book.path).read()
    broken = _evaluate(w, notional_usd="200", engine_root=str(root))
    assert "CAPITAL_ANCHOR_REFUSED:VIEW_TAMPERED" in broken["authority_gaps"], broken["authority_gaps"]
    assert open(book.path).read() == before, "a read-only evaluation never rewrites the book"


def test_a_corrupt_capital_book_stops_the_session_before_any_order_and_is_not_reset(w, monkeypatch):
    """THE REFUSAL A SESSION REALLY GETS. The test world auto-initialises an account (the owner's one-time act), which turned this into ``BOOK_UNREADABLE``, a code only the OWNER's ``--anchor-init`` raises: production
    answers BOOK_UNANCHORED (a pre-anchor file that cannot be adopted), a PAUSE with no ALERT, and the owner's own command is what refuses to adopt it (next test)."""
    monkeypatch.setattr(AP, "_open_book", w.production_open_book)
    _session_world(w)
    root = w.tmp / "eng_corrupt"
    os.makedirs(root)
    (root / "capital.json").write_text("{corrupt")
    res, _got = _collect(w, eng_name="eng_corrupt")
    assert res["result"] == "REFUSED" and res["why"] == ["CAPITAL_ANCHOR_REFUSED:BOOK_UNANCHORED"], res
    assert (root / "capital.json").read_text() == "{corrupt" and not w.requests_to("/v5/order/create"), "not reset, not guessed, nothing placed"
    assert not ES.EngineState(str(root), lambda: 0).has("ALERT"), "an owner-action refusal is a PAUSE; the owner then decides"


def test_the_owners_anchor_init_refuses_to_adopt_a_book_it_cannot_read_and_leaves_it_alone(w, tmp_path):
    root = tmp_path / "eng_unreadable"
    os.makedirs(root)
    (root / "capital.json").write_text("{corrupt")
    book = CC.CapitalBook(str(root), AN.Anchor(str(root), CT.ACCOUNT, "testnet", AN.anchor_dir_for(os.environ)))
    with pytest.raises(CC.BookRefused) as e:
        book.initialise({"test": True})
    assert e.value.code == "BOOK_UNREADABLE" and (root / "capital.json").read_text() == "{corrupt"
    assert not os.path.exists(os.path.join(AN.anchor_dir_for(os.environ), f"lineage-{CT.ACCOUNT}.jsonl")), "no lineage was created over a record nobody could read"


def test_a_finished_session_appends_one_chained_feedback_row_and_the_quality_command_reports_it(w, capsys):
    import evidence_chain as EC
    _session_world(w)
    res, _got = _collect(w, eng_name="eng_fb")
    assert res["result"] == "DONE", res
    log = str(w.tmp / "eng_fb" / "exec_quality_log.jsonl")
    rows = EC.rows_of(EC.read_lines(log)[0])
    assert len(rows) == 1 and EC.verify(log)["ok"] and rows[0]["flat_verified"] is True and rows[0]["attempt_completed"] is True
    assert rows[0]["total_execution_cost_bps_of_position_notional"] is not None and rows[0]["modelled_cost_bps"] is not None and rows[0]["decision_id"].startswith("dec_")
    assert AP.main(["quality", "--root", str(w.tmp / "eng_fb")], env={}) == 0
    rep = json.loads(capsys.readouterr().out)
    assert rep["sessions_logged"] == 1 and rep["log_chain_ok"] is True and rep["policy"]["verdict"] == "INSUFFICIENT_SAMPLE" and rep["model_vs_measured"]["sessions"] == 1
    assert AP.main(["quality", "--root", str(w.tmp / "no_such_root")], env={}) == 0
    assert json.loads(capsys.readouterr().out)["sessions_logged"] == 0                      # nothing logged: stated, not invented
    with open(log, "a", encoding="utf-8") as fh:
        fh.write('{"tampered": true}\n')                                                      # a broken chain is reported, not trusted
    assert AP.main(["quality", "--root", str(w.tmp / "eng_fb")], env={}) == 1


# ================================================================ review round: capital / decision findings (each reproduced first)
def _collect_with_sleep(w, sleep, eng_name="eng_held"):
    got = []
    res = AP.run_once(w.env, inner=w.fake, clock=w.clock.now, sleep=sleep, out=[].append, state_root=str(w.tmp / "auto"), engine_root=str(w.tmp / eng_name),
                      on_event=lambda kind, stage, payload, n=0: got.append((kind, stage, dict(payload), n)))
    return res, got


def test_a_held_session_books_the_real_settlement_and_the_next_cycle_credits_exactly_what_cash_shows(w):
    """The autopilot-level proof the review found missing: funding_was_read=True end to end. Mutating `known = price - fees + fund` (the sign of funding) must fail HERE."""
    from decimal import Decimal as D
    _session_world(w)
    start, state, real_sleep = w.clock.now(), {"done": False}, w.clock.sleep

    def sleep(s):
        real_sleep(s)
        if not state["done"] and w.clock.now() >= start + 600_000 and any(p["size"] != 0 for p in w.fake.positions.values()):
            w.fake.settle_funding("BTCUSDT", "0.0080", ts_ms=w.clock.now())                      # the venue pays the held short into the wallet, mid-hold (an 80 bps print: a clear credit the books must reproduce exactly)
            state["done"] = True
    cash_before = w.fake.wallet["USDT"]
    res1, got1 = _collect_with_sleep(w, sleep)
    assert res1["result"] == "DONE" and state["done"], res1
    rec = json.load(open(os.path.join(res1["state_dir"], "testnet_session.json")))
    net = rec["net"]
    assert net["funding_usd"] is not None and net["funding_usd"] > 0.3 and rec["flat_verified"] is True        # a clear credit (an 80 bps print on one mechanics lot is about $0.5; it was > $1 when this session was sized at $200)
    f = [g for g in got1 if g[1] == "FUNDING_SETTLEMENT"]
    assert len(f) == 1 and f[0][2]["status"] == "BOOKED_WALLET_CONFIRMED"
    known = D(str(net["price_pnl_usd"])) - D(str(net["fees_usd"])) + D(str(net["funding_usd"]))
    assert abs((w.fake.wallet["USDT"] - cash_before) - known) < D("1e-6")                         # the wallet agrees with the ledger to the 6th decimal
    w.clock.sleep(1800)
    w.fake.funding_rate, w.fake.funding_in_ms = "0.0001", 600_000
    res2, got2 = _collect(w, "eng_held")
    ev = next(g for g in got2 if g[1] == "CAPITAL_SETTLED")
    assert ev[2]["status"] == "SETTLED" and abs(D(ev[2]["credited_usd"]) - known) < D("0.0002") and abs(D(ev[2]["cash_change_usd"]) - known) < D("0.0002")


def test_a_decision_record_with_a_real_git_revision_is_stored_and_identifies_the_code(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch, grant=False)
    _measured_world(w)
    sha = "04bd8ec6e07740a2e9ef73f5ffe34b92fc99ed3b"
    w.env = {**w.env, "GIT_SHA": sha}
    rec = _evaluate(w, notional_usd="200")["decision_record"]
    assert rec["code_sha"] == sha[:12]                                                       # the guard refuses a 40-hex token; the full revision is inside the id
    assert AP.record_decision(str(tmp_path / "ob"), rec, w.clock.now()) == "STORED"
    _measured_world(w)
    other = _evaluate(w, notional_usd="200")["decision_record"]
    w.env = {**w.env, "GIT_SHA": "b" * 40}
    _measured_world(w)
    assert _evaluate(w, notional_usd="200")["decision_record"]["decision_id"] != other["decision_id"]      # the code revision still separates decisions


def test_two_no_data_decisions_at_different_moments_are_different_decisions_and_both_store(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch, grant=False)
    w.fake.funding_rate, w.fake.funding_in_ms = "0.0080", 99_999_999                            # settlement far away: SKIP_NO_OPPORTUNITY (no market inputs digest at all)
    first = _evaluate(w, notional_usd="200")["decision_record"]
    assert first["decision"] == "SKIP_NO_OPPORTUNITY" and first["evaluated_ms"] and AP.record_decision(str(tmp_path / "ob"), first, w.clock.now()) == "STORED"
    assert AP.record_decision(str(tmp_path / "ob"), first, w.clock.now() + 60_000) == "DUPLICATE"           # the same decision again is a duplicate, never a conflict
    w.clock.sleep(60)
    second = _evaluate(w, notional_usd="200")["decision_record"]
    assert second["decision_id"] != first["decision_id"] and AP.record_decision(str(tmp_path / "ob"), second, w.clock.now()) == "STORED"


def test_long_digit_runs_in_the_book_or_the_limits_never_make_a_record_unstorable(tmp_path):
    import ms_capital as CC
    root = tmp_path / "book"
    b = CT.anchored(str(root))
    b.ensure_baseline("10000.12345678")
    b.park("s1", "10000.12345678", "0.30000000000000004", True, True)
    b.settle("10000.43")
    ctx = AP._decision_context(str(root), 0.30000000000000004, "400.12345678901234")
    base = {"decision": "REJECT_UNECONOMIC", "symbol": "BTCUSDT", "notional_usd": "200", "inputs_digest": "d" * 64, "observed_ms": 1_794_312_000_000, "edge": None}
    rec = EL.decision_record(base, code_sha="c" * 40, **ctx)
    XE.make_event("DECISION", {"stage": "DECISION_RECORDED", **rec}, session_id=None, occurred_at_ms=1, event_id=EL.event_id("dec", "DECISION", "x", 0))      # the real secret guard accepts it
    assert EL.neutral("0.30000000000000004") == "0.3000" and EL.neutral("abc") == "abc" and EL.neutral(None) is None and EL.neutral(7) == 7


def test_the_book_age_in_the_record_is_signed_never_clamped_to_look_fresh():
    import types
    import ms_edge_gate as EGG
    b = types.SimpleNamespace(ts_ms=1_000_080, bids=[(100.0, 5.0)], asks=[(100.1, 5.0)])
    assert EGG._touch_summary("perp", b, 1_000_000)["perp_book_age_ms"] == -80                 # stamped 80 ms AFTER our reading: the record says so
    assert EGG._touch_summary("perp", b, 1_000_200)["perp_book_age_ms"] == 120


def test_the_executed_session_records_what_the_allocator_actually_sized_next_to_the_decision(w):
    _session_world(w)
    res, _got = _collect(w, "eng_exec")
    dec = json.load(open(os.path.join(res["state_dir"], "decision.json")))
    assert dec["decision_id"].startswith("dec_") and dec["executed"]["allocator_notional_usd"] and dec["executed"]["portfolio_after_settle"]["status"] in ("BOOK", "NO_CAPITAL_BOOK")
    assert "evaluated at the grant's maximum size" in dec["executed"]["note"]


def test_a_book_that_goes_bad_after_the_orders_still_emits_every_event_and_stops_all_future_entries(w, monkeypatch):
    import ms_capital as CC
    import ms_engine_state as ESS
    _session_world(w)

    def bad(self, *a, **k):
        raise CC.BookUnreadable("capital book went bad mid-session")
    monkeypatch.setattr(CC.CapitalBook, "update_pending", bad)
    res, got = _collect(w, "eng_bad_after")
    stages = [g[1] for g in got]
    assert res["result"] == "DONE" and "ORDER_VENUE_RECORD" in stages and "VENUE_FLAT_VERIFIED" in stages and "COSTS_BOOKED" in stages and "SESSION_RECONCILED" in stages
    assert ESS.EngineState(str(w.tmp / "eng_bad_after"), w.clock.now).has("ALERT")                 # a persistent ALERT: the loop stops until the owner has looked


def test_a_book_that_goes_bad_at_the_park_refuses_before_any_order_and_alerts(w, monkeypatch):
    import ms_capital as CC
    import ms_engine_state as ESS
    _session_world(w)

    def bad(self, *a, **k):
        raise CC.BookUnreadable("capital book went bad at the park")
    monkeypatch.setattr(CC.CapitalBook, "park", bad)
    res, _got = _collect(w, "eng_bad_park")
    assert res["result"] == "REFUSED" and res["why"] == ["CAPITAL_BOOK_UNREADABLE"] and not w.requests_to("/v5/order/create")
    assert ESS.EngineState(str(w.tmp / "eng_bad_park"), w.clock.now).has("ALERT")


def test_the_capital_command_on_a_corrupt_book_explains_instead_of_a_traceback(tmp_path, capsys):
    (tmp_path / "capital.json").write_text("{corrupt")
    assert AP.main(["capital", "--root", str(tmp_path)], env={}) == 2
    out = capsys.readouterr().out
    assert "NO_ACCOUNT_RESOLVED" in out and "--anchor-init" in out and (tmp_path / "capital.json").read_text() == "{corrupt", "it explains what to do and rewrites nothing"


# ---------------------------------------------------------------- the owner's emergency unwind must STOP the loop, not just the session
def test_the_owners_unwind_handover_stops_the_loop_with_an_alert_instead_of_rearming(w, tmp_path):
    root = tmp_path / "ap"

    def handover(n):                                                   # what `reconcile --unwind` leaves in the holder's session dir while the session runs
        dirs = AP.session_dirs(str(w.tmp / "auto"))
        if n == 3 and dirs:
            AD.engage_kill_switch(os.path.join(dirs[-1], "MS_KILL"), S.OWNER_UNWIND_KILL_REASON)
    res = revoke_cycle(w, root, handover)
    assert res["flat_verified"] is True and res["owner_emergency_unwind"] is True and all(p["size"] == 0 for p in w.fake.positions.values())
    eng = ES.EngineState(str(root), w.clock.now)
    assert eng.has("ALERT") and eng.read("ALERT")["code"] == "OWNER_EMERGENCY_UNWIND"


def test_the_autopilots_own_revocation_is_not_an_owner_emergency(w, tmp_path):
    root = tmp_path / "ap"
    res = revoke_cycle(w, root, lambda n: ES.EngineState(str(root), w.clock.now).set("STOP", {"by": "owner"}) if n == 3 else None)
    assert res["revoked_reason"] == "STOP" and "owner_emergency_unwind" not in res and not ES.EngineState(str(root), w.clock.now).has("ALERT")


def test_owner_unwind_alert_recognises_only_the_handover_reason(tmp_path):
    clock = lambda: 1_800_000_000_000                                   # noqa: E731
    eng = str(tmp_path / "eng")
    d = tmp_path / "session"
    d.mkdir()
    assert AP.owner_unwind_alert(str(d), eng, clock) is False                                          # no kill file
    (d / "MS_KILL").write_text("KILL_SWITCH")                                                          # the autopilot's own kill
    assert AP.owner_unwind_alert(str(d), eng, clock) is False and not ES.EngineState(eng, clock).has("ALERT")
    (d / "MS_KILL").write_text(S.OWNER_UNWIND_KILL_REASON)
    assert AP.owner_unwind_alert(str(d), eng, clock) is True and ES.EngineState(eng, clock).has("ALERT")


def test_the_owner_marker_is_recognised_even_when_the_kill_file_already_holds_another_reason(tmp_path):
    """engage_kill_switch is exclusive-create: when the session's kill file already says something else (the autopilot's own revocation), the handover's separate marker still tells the loop who asked."""
    clock = lambda: 1_800_000_000_000                                   # noqa: E731
    eng = str(tmp_path / "eng")
    d = tmp_path / "session"
    d.mkdir()
    (d / "MS_KILL").write_text("revoked:GRANT:expired")
    assert AP.owner_unwind_alert(str(d), eng, clock) is False
    (d / S.FILES["owner_unwind"]).write_text(S.OWNER_UNWIND_KILL_REASON)
    assert AP.owner_unwind_alert(str(d), eng, clock) is True and ES.EngineState(eng, clock).has("ALERT")


# ---------------------------------------------------------------- the settlement is re-checked when the first entry leaves (a slow session must not enter after the print)
def _slow_world_before_settlement(w, monkeypatch, settle_in_s, round_trip_s):
    t0 = w.clock.now()
    t_settle = t0 + settle_in_s * 1000
    w.fake.funding_rate = "0.0020"                                                         # 20 bps: clears the owner's floor
    monkeypatch.setattr(type(w.fake), "funding_in_ms", property(lambda s: t_settle - s.now_ms(), lambda s, v: None), raising=False)     # an ABSOLUTE settlement time, not a sliding one
    orig = w.fake.request
    slow_paths = {"/v5/account/wallet-balance", "/v5/position/list", "/v5/order/realtime", "/v5/execution/list", "/v5/order/create", "/v5/position/set-leverage", "/v5/market/time"}

    def slow(method, url, **k):
        if url.split("?")[0].split("bybit.com")[-1] in slow_paths:
            w.clock.t += round_trip_s * 1000                                                 # a slow venue round trip on the session's own reads
        return orig(method, url, **k)
    w.fake.request = slow
    w.fake.fee_rates = {"spot": ("0.001", "0.001"), "linear": ("0.0002", "0.00055")}
    write_grant(w, min_funding_bps=0.5, allow_mechanics_sessions=True)
    return t_settle


def _cycle_with_create_times(w, tmp_path, t_settle):
    creates = []
    orig = w.fake.request

    def spy(method, url, **k):
        if "/v5/order/create" in url:
            creates.append((t_settle - w.clock.now()) / 1000.0)                                   # seconds to the settlement when the order request leaves
        return orig(method, url, **k)
    w.fake.request = spy
    res = AP.run_once(w.env, inner=w.fake, clock=w.clock.now, sleep=w.clock.sleep, out=lambda _m: None, state_root=str(w.tmp / "auto"), engine_root=str(tmp_path / "ap"))
    return res, creates


@pytest.mark.parametrize("settle_in_s", [126, 130])
def test_a_session_slowed_past_the_margin_sends_nothing_and_is_a_skip_with_its_reason(w, tmp_path, monkeypatch, settle_in_s):
    t_settle = _slow_world_before_settlement(w, monkeypatch, settle_in_s=settle_in_s, round_trip_s=3)
    res, creates = _cycle_with_create_times(w, tmp_path, t_settle)
    assert not creates and not [e for e in w.fake.execs if e.get("execType", "Trade") == "Trade"], "an entry left too close to (or after) the settlement: all fees, no funding"
    assert res["result"] == "SKIPPED" and res["why"] and res["why"][0].startswith("SETTLEMENT_TOO_CLOSE_AT_ORDER") and res["orders"] == 0 and res["flat_verified"] is True
    log = os.path.join(str(tmp_path / "ap"), "exec_quality_log.jsonl")
    assert not os.path.exists(log) or not open(log).read().strip()                                  # a session that sent nothing is not a measured-cost sample


@pytest.mark.parametrize("settle_in_s", [136, 140])
def test_the_first_entry_is_gated_at_the_margin_but_the_hedge_leg_is_not(w, tmp_path, monkeypatch, settle_in_s):
    """The perp (first leg) leaves with at least 20 s to spare; the spot hedge follows seconds later, at 140 s it is AFTER the print, and it must still be sent: refusing it would open a perp
    and burn a round trip unwinding it."""
    t_settle = _slow_world_before_settlement(w, monkeypatch, settle_in_s=settle_in_s, round_trip_s=3)
    res, creates = _cycle_with_create_times(w, tmp_path, t_settle)
    assert res["result"] == "DONE" and res["flat_verified"] is True and res["orders"] == 4        # perp + spot entry, two exits
    assert creates[0] >= 15.0                                                                      # the first leg went with time to spare (the check ran ~5 s earlier in this slowed world)
    if settle_in_s == 140:
        assert creates[1] < 0                                                                      # the hedge leg left AFTER the settlement and was not time-gated


def test_a_session_with_time_to_spare_still_enters_across_the_settlement(w, tmp_path, monkeypatch):
    _slow_world_before_settlement(w, monkeypatch, settle_in_s=300, round_trip_s=1)
    res = AP.run_once(w.env, inner=w.fake, clock=w.clock.now, sleep=w.clock.sleep, out=lambda _m: None, state_root=str(w.tmp / "auto"), engine_root=str(tmp_path / "ap"))
    assert res["result"] == "DONE" and res["flat_verified"] is True and res["orders"] > 0               # positive control: the guard does not stop a session with time left
    log = os.path.join(str(tmp_path / "ap"), "exec_quality_log.jsonl")
    assert os.path.exists(log) and open(log).read().strip()                                          # a session that sent orders is a feedback sample


@pytest.mark.parametrize("field,bad", [("realised_usd", "abc"), ("realised_usd", "NaN"), ("peak_basis_usd", "abc"), ("loss_streak", "x"), ("halted", "false")])
def test_a_capital_book_with_a_nonsense_figure_stops_the_session_before_any_order_and_is_not_rewritten(w, field, bad):
    """REPRODUCED (specialist E N1): a parsable book with realised_usd='abc' made the allocator size 200.00 where the honest book said LIFETIME_LOSS_STOP."""
    _session_world(w)
    root = w.tmp / "eng_semantic"
    b = CT.anchored(str(root))
    b.ensure_baseline("10000")
    assert b.park("s1", "10000", "-30", True, True)
    b.settle("9970")
    doc = json.load(open(root / "capital.json"))
    doc[field] = bad
    text = json.dumps(doc)
    (root / "capital.json").write_text(text)
    res, _got = _collect(w, eng_name="eng_semantic")
    # the view no longer matches what the lineage attested: refused before any order (a figure edited in place can never size anything), and the file is left exactly as it is
    assert res["result"] == "REFUSED" and res["why"] == ["CAPITAL_ANCHOR_REFUSED:VIEW_TAMPERED"], res
    assert (root / "capital.json").read_text() == text and not w.requests_to("/v5/order/create")


def _hb_loop(results, tmp_path):
    t = {"v": 1_000_000}

    def clock():
        t["v"] += 1000
        return t["v"]

    class Tel:
        last_sync_ms = None

        def __init__(self):
            self.hbs = []

        def heartbeat(self, hb, drain=True):
            self.hbs.append(dict(hb))

        def event(self, *a, **k):
            pass

        def pending(self):
            return 0
    tel, it = Tel(), iter(results)
    AP.loop({}, root=str(tmp_path), once=lambda *a, **k: next(it), clock=clock, sleep=lambda s: None, out=lambda s: None, max_iterations=len(results), telemetry=tel,
            recover_fn=lambda env, **k: (True, [], {"venue_read_complete": True, "reconciled_ms": clock()}))
    return tel.hbs


def test_the_heartbeat_stamps_market_data_only_for_a_cycle_that_proved_a_market_read(tmp_path):
    """Round 19 (adversarial review, Round 17 known-open): run_once returns before its first read on REFUSED (capability, grant, credentials) and on a daily-cap SKIP; the loop stamped
    'market data fresh' for them, while a real read failure (ERROR) showed None."""
    def fresh(i):                                                    # one engine root per scenario: a PAUSED/ALERT marker from one cycle must not leak into the next
        d = tmp_path / f"root{i}"
        d.mkdir()
        return d
    n = 0
    for res in ({"result": "REFUSED", "why": ["OWNER_CAPABILITY_FILE:absent"]}, {"result": "SKIPPED", "why": ["DAILY_SESSIONS_REACHED"]}, {"result": "ERROR", "why": ["FUNDING_READ_FAILED:X"]}):
        n += 1
        assert _hb_loop([dict(res) for _ in range(3)], fresh(n))[-1]["last_market_data_ms"] is None, res["result"]
    for res in ({"result": "SKIPPED", "why": ["FUNDING_BELOW_FLOOR"], "funding_bps": 0.4}, {"result": "DONE", "flat_verified": True}, {"result": "NOT_FLAT"}):
        n += 1
        beats = _hb_loop([dict(res) for _ in range(3)], fresh(n))
        assert any(isinstance(b["last_market_data_ms"], int) for b in beats), res["result"]


def test_a_grant_created_in_the_future_is_refused(tmp_path):
    """Round 19 (adversarial review): the lifetime check compared expires - created only, so a hand-built grant dated 400 days ahead was valid for 430 days (the capability loader refuses this)."""
    now, day = 1_800_000_000_000, 86_400_000
    manifest = {"plan_sha256": "a" * 64, "symbol_allowlist": ["BTCUSDT"], "bounds": {"max_entries_per_utc_day": 3, "aggregate_max_notional_usd": 100}}

    def write(created, expires):
        g = {"schema": AP.GRANT_SCHEMA, "reference": "r", "created_ms": created, "expires_ms": expires, "manifest_plan_sha256": "a" * 64, "symbols": ["BTCUSDT"], "max_sessions_per_day": 1,
             "max_notional_usd": 50, "max_hold_s": 3300, "min_funding_bps": 1.0, "allow_mechanics_sessions": False}
        for k in AP.GRANT_KEYS - set(g) - {"grant_sha256"}:
            g[k] = None
        g["grant_sha256"] = AP._sha(g)
        p = str(tmp_path / f"g{created}.json")
        fd = os.open(p, os.O_WRONLY | os.O_CREAT, 0o600)
        os.write(fd, json.dumps(g).encode())
        os.close(fd)
        return AP.load_grant(now, manifest, p)
    out, why = write(now + 400 * day, now + 430 * day)
    assert out is None and "GRANT_FROM_THE_FUTURE" in why
    out2, why2 = write(now - day, now + 29 * day)
    assert out2 is not None and not why2                                   # control: an ordinary grant is unchanged


# ------------------------------------------------------------------------------------------------ Round 21: the read-only entry points reach no executor write method
def _calls_in(path, func_names):
    import ast
    tree = ast.parse(open(path, encoding="utf-8").read())
    found, seen = set(), set()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name in func_names:
            seen.add(node.name)
            for c in ast.walk(node):
                if isinstance(c, ast.Call):
                    f = c.func
                    found.add(f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", ""))
    return found, seen


EXECUTOR_WRITE_METHODS = {"send_order", "cancel_order", "set_leverage", "halt", "unhalt", "execute", "cmd_execute", "run_session", "create_order", "place_order", "amend_order", "cancel_all", "reconcile", "unwind",
                          "exec_lease_acquire", "exec_lease_renew", "acquire", "renew", "write_grant", "cmd_grant", "save_record"}


def test_the_read_only_decision_and_its_funding_read_reach_no_executor_write_method():
    """REVIEWED (Round 21): `evaluate` is documented as read-only (public GETs and two signed fee-rate GETs, no lease, no authority, no state directory). This pins it structurally: the functions that make up
    that path may not call an order, leverage, halt, lease or grant-writing method. (`status` separately creates the empty private state directory; that is documented and sends nothing.)"""
    path = os.path.join(os.path.dirname(HERE), "tools", "ms_testnet_autopilot.py")
    found, seen = _calls_in(path, {"evaluate_readonly", "fetch_funding", "_executor"})
    assert seen == {"evaluate_readonly", "fetch_funding", "_executor"}, seen
    assert not (found & EXECUTOR_WRITE_METHODS), sorted(found & EXECUTOR_WRITE_METHODS)


def test_the_venue_snapshot_command_and_the_executors_end_state_reads_reach_no_write_method():
    """Review P3-5 (Round 22): the account-wide end-state read is structurally read-only, not only observed to send no POST."""
    path = os.path.join(os.path.dirname(HERE), "tools", "ms_testnet_autopilot.py")
    found, seen = _calls_in(path, {"cmd_venue_snapshot"})
    assert seen == {"cmd_venue_snapshot"}, seen
    assert not (found & EXECUTOR_WRITE_METHODS), sorted(found & EXECUTOR_WRITE_METHODS)
    epath = os.path.join(os.path.dirname(HERE), "tools", "ms_testnet_executor.py")
    found2, seen2 = _calls_in(epath, {"venue_end_state", "account_open_orders"})
    assert seen2 == {"venue_end_state", "account_open_orders"}, seen2
    assert not (found2 & EXECUTOR_WRITE_METHODS), sorted(found2 & EXECUTOR_WRITE_METHODS)


def test_the_preflight_venue_reads_reach_no_executor_write_method():
    path = os.path.join(os.path.dirname(HERE), "tools", "ms_testnet_session.py")
    found, seen = _calls_in(path, {"_preflight_reads"})
    assert seen == {"_preflight_reads"}, seen
    assert not (found & EXECUTOR_WRITE_METHODS), sorted(found & EXECUTOR_WRITE_METHODS)


def test_the_write_method_guard_can_fail():
    """A mutant: a function that calls send_order must be caught by the same walker."""
    import ast
    import tempfile
    src = "def evaluate_readonly(x):\n    return x.executor.send_order(1)\n"
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "m.py")
        open(p, "w").write(src)
        found, seen = _calls_in(p, {"evaluate_readonly"})
    assert seen == {"evaluate_readonly"} and "send_order" in found and found & EXECUTOR_WRITE_METHODS


# ---------------------------------------------------------------- Round 22: a mis-armed or full machine refuses visibly and never crashes the loop

def test_forbidden_environment_names_are_found_never_their_values():
    assert AP.forbidden_environment({}) == [] and AP.forbidden_environment({"USE_TESTNET": "1", "PAPER_TRADING": "1"}) == []
    sentinel = "sentinel-not-a-credential-1"
    assert AP.forbidden_environment({"LIVE_TRADING_ACK": sentinel}) == ["LIVE_TRADING_ACK"]
    assert AP.forbidden_environment({"BYBIT_API_KEY": sentinel, "BYBIT_API_SECRET": sentinel}) == ["BYBIT_API_KEY", "BYBIT_API_SECRET"]
    for v in ("0", "false", "No", "off", ""):
        assert AP.forbidden_environment({"USE_TESTNET": v}) == ["USE_TESTNET"], v
    for v in ("0", "false", "No", "off"):
        assert AP.forbidden_environment({"PAPER_TRADING": v}) == ["PAPER_TRADING"], v
    assert AP.forbidden_environment({"LIVE_TRADING_ACK": "  "}) == []                          # blank is not set
    assert AP.forbidden_environment({"LIVE_ANYTHING": "1", "BYBIT_API_ANY": "x", "BYBIT_TESTNET_API_KEY": "dedicated", "BYBIT_TESTNET_API_SECRET": "dedicated"}) == ["BYBIT_API_ANY", "LIVE_ANYTHING"]   # prefixes; the dedicated Testnet pair is NOT forbidden


@pytest.mark.parametrize("name,value", [("LIVE_TRADING_ACK", "I-ACCEPT"), ("USE_TESTNET", "0"), ("PAPER_TRADING", "0"), ("BYBIT_API_KEY", "generic-key"), ("BYBIT_API_SECRET", "generic-secret")])
def test_run_once_refuses_a_deployment_that_carries_an_arming_switch_before_any_read(w, name, value):
    write_grant(w, min_funding_bps=0.5)
    w.fake.requests.clear()
    res = AP.run_once({**w.env, name: value}, inner=w.fake, clock=w.clock.now, sleep=w.clock.sleep, out=lambda _m: None, state_root=str(w.tmp / "auto"), engine_root=str(w.tmp / "ap"))
    assert res["result"] == "REFUSED" and res["why"] == [f"BAD_ENVIRONMENT:{name}"], res
    assert w.fake.requests == [], "a refused mis-armed deployment must not touch the network"
    assert value not in json.dumps(res)


def test_the_loop_pauses_on_a_bad_environment_without_an_alert_marker_and_without_a_session(w, tmp_path):
    root = tmp_path / "ap"
    write_grant(w, min_funding_bps=0.5)
    out = []
    kw = dict(root=str(root), clock=w.clock.now, sleep=lambda _s: None, out=out.append, max_iterations=2, inner=w.fake, state_root=str(w.tmp / "auto"), recover_fn=OK_RECOVERY)
    AP.loop({**w.env, "LIVE_TRADING_ACK": "x"}, **kw)
    st = ES.EngineState(str(root), w.clock.now)
    assert st.load()["state"] == "PAUSED" and not st.has("ALERT")
    assert "BAD_ENVIRONMENT:LIVE_TRADING_ACK" in st.load()["source"]


def test_a_low_state_volume_refuses_the_session_and_the_floor_can_only_be_raised(w, monkeypatch):
    write_grant(w, min_funding_bps=0.5)
    import collections
    usage = collections.namedtuple("usage", "total used free")
    monkeypatch.setattr(AP.shutil, "disk_usage", lambda p: usage(5 * 10 ** 9, 5 * 10 ** 9 - 100 * 1024 ** 2, 100 * 1024 ** 2))
    res = AP.run_once(w.env, inner=w.fake, clock=w.clock.now, sleep=w.clock.sleep, out=lambda _m: None, state_root=str(w.tmp / "auto"), engine_root=str(w.tmp / "ap"))
    assert res["result"] == "REFUSED" and res["why"][0].startswith("DISK_LOW:") and "268435456B" in res["why"][0]
    assert AP.disk_gate({"EXECUTOR_MIN_FREE_BYTES": "1"}, str(w.tmp)) is not None            # a LOWER floor than 256 MiB is ignored
    monkeypatch.setattr(AP.shutil, "disk_usage", lambda p: usage(5 * 10 ** 9, 0, 300 * 1024 ** 2))
    assert AP.disk_gate({}, str(w.tmp)) is None and AP.disk_gate({"EXECUTOR_MIN_FREE_BYTES": str(400 * 1024 ** 2)}, str(w.tmp)).startswith("DISK_LOW:")
    monkeypatch.setattr(AP.shutil, "disk_usage", lambda p: (_ for _ in ()).throw(OSError("nope")))
    assert AP.disk_gate({}, str(w.tmp)) == "DISK_UNREADABLE"                                  # unknown is not "enough"


def test_a_full_state_volume_does_not_crash_the_loop_and_no_session_starts_while_it_stays_full(w, tmp_path):
    """REPRODUCED (Round 22 execution review): once the volume filled, an OSError out of go()/last_result.json escaped the loop and killed the process (an always-restart policy then looped on it)."""
    import errno
    root = tmp_path / "ap"
    real = ES.write_json_atomic
    state = {"full": False}

    def writer(path, obj):
        if state["full"]:
            raise OSError(errno.ENOSPC, "No space left on device")
        return real(path, obj)
    sessions = []

    def once(env, **k):
        sessions.append(1)
        k["on_state"]("EXECUTING", "execute_started")
        state["full"] = True                                                                       # the volume fills DURING the session
        return {"result": "DONE", "flat_verified": True, "why": []}
    ES.write_json_atomic = writer
    try:
        rc, out = run_loop(w, root, once, n=8, recover_fn=OK_RECOVERY)
        assert rc == 0, "the loop must return normally, not raise"
        assert len(sessions) == 1, "no second session may start while the state cannot be persisted"
        assert any("STATE_WRITE_FAILED" in line or "LAST_RESULT_WRITE_FAILED" in line for line in out)
        state["full"] = False                                                                      # space is freed: the loop probes, recovers and trades again
        rc2, _ = run_loop(w, root, once, n=3, recover_fn=OK_RECOVERY)
        assert rc2 == 0 and len(sessions) >= 2
    finally:
        ES.write_json_atomic = real


def test_a_session_whose_first_entry_is_aborted_before_any_order_is_a_skip_with_the_reason_not_done(w, tmp_path, stop_state):
    """REPRODUCED (Round 22 execution review): authority lost / a stop at the pre-order check sent nothing, the venue was flat, and the result was DONE: a 30-minute cooldown and a counted session for a hedge that never ran."""
    prepared(w)
    res = run(w, tmp_path / "ap", lease=CountingLease(ok_calls=1, raises=True))
    assert creates(w) == [] and res["orders"] == 0 and res["flat_verified"] is True
    assert res["result"] == "SKIPPED" and res["why"] and res["why"][0].startswith("EXECUTE_ABORTED_BEFORE_ANY_ORDER:"), res
    log = os.path.join(str(tmp_path / "ap"), "exec_quality_log.jsonl")
    assert not os.path.exists(log) or not open(log).read().strip()


def test_a_mechanics_session_records_its_measurement_only_gate_next_to_the_decision_and_still_runs(w, tmp_path):
    write_grant(w, min_funding_bps=0.5, allow_mechanics_sessions=True)
    w.fake.fee_rates = {"spot": ("0.001", "0.001"), "linear": ("0.0002", "0.00055")}
    w.fake.funding_rate, w.fake.funding_in_ms = "0.0001", 600_000
    res, _ = cycle(w)
    assert res["result"] == "DONE" and res["session_class"] == "TESTNET_MECHANICS"
    dec = json.load(open(os.path.join(res["state_dir"], "decision.json")))
    gate = dec["executed"]["eligibility_gate"]
    assert gate["verdict"] == "ELIGIBLE_MEASUREMENT_ONLY" and gate["profile"] == "MEASUREMENT"
    assert gate["waived"] == ["EDGE_POSITIVE_AFTER_COSTS", "STATISTICAL_EVIDENCE_SUFFICIENT"] and "NOT eligibility to trade for profit" in gate["claim"]
    assert next(t for t in gate["terms"] if t["term"] == "AUTHORITY_VALID")["facts"]["lease"] == "NOT_REQUIRED"


def test_the_gate_cannot_be_bypassed_from_the_command_line_or_the_loop():
    src = open(AP.__file__, encoding="utf-8").read()
    for loose in ("skip_gate", "no_gate", "bypass", "qualification="):
        assert loose not in src, loose
    assert src.count("session_gate(") >= 2


def test_the_documented_mechanics_grant_floor_lets_an_ordinary_print_start_a_session(w):
    """REPRODUCED (Round 22): the qualification proposal's grant said --min-funding-bps 5, and the loop starts NO session (mechanics included) unless the print pays that much; the usual BTCUSDT print is about 1 bp.
    The proposal's own grant text is run through the loop's own decision."""
    import re
    doc = open(os.path.join(os.path.dirname(os.path.dirname(HERE)), "docs", "TESTNET_QUALIFICATION_PROPOSAL.md"), encoding="utf-8").read()
    cmds = re.findall(r"autopilot-ctl grant([^`]*)`", doc)
    mech = [c for c in cmds if "--allow-mechanics-sessions" in c]
    assert mech, "the proposal must carry the mechanics grant command"
    floor = float(re.search(r"--min-funding-bps ([0-9.]+)", mech[0]).group(1))
    hold = int(re.search(r"--max-hold-s ([0-9]+)", mech[0]).group(1))
    grant = {"min_funding_bps": floor, "max_hold_s": hold, "allow_mechanics_sessions": True}
    now = w.clock.now()
    go, _hold, why = AP.funding_decision(0.0001, now + 600_000, now, grant)             # 1 bp, settlement in 10 minutes
    assert go, (floor, why)
    assert not AP.funding_decision(0.00004, now + 600_000, now, grant)[0]                # 0.4 bp is below the 0.5 floor
    assert not AP.funding_decision(0.0, now + 600_000, now, grant)[0] and not AP.funding_decision(-0.0001, now + 600_000, now, grant)[0]   # funding must be strictly positive
    assert floor <= 1.0, "a mechanics-only grant floor above one ordinary print would wait for a rare one"


@pytest.mark.parametrize("state", ["EXECUTING", "RISK_APPROVED", "PLAN_PROPOSED"])
def test_a_state_volume_that_fills_before_the_first_order_refuses_the_session_and_places_no_order(w, tmp_path, monkeypatch, state):
    """REPRODUCED (Round 22 review): the new tolerant state write let a session reach its first order after `EXECUTING` could not be recorded (4 orders sent; on main the OSError ended the session before any order).
    The transitions BEFORE the first order are strict; only the ones after it are tolerant."""
    import errno
    prepared(w)
    real = ES.write_json_atomic

    def writer(path, obj):
        if isinstance(obj, dict) and obj.get("state") == state:
            raise OSError(errno.ENOSPC, "No space left on device")
        return real(path, obj)
    monkeypatch.setattr(ES, "write_json_atomic", writer)
    rc, out = run_loop(w, tmp_path / "ap", AP.run_once, n=1, recover_fn=OK_RECOVERY, sleep=w.clock.sleep)
    assert rc == 0 and creates(w) == [], "no order may leave while a pre-order state cannot be recorded"
    res = json.load(open(tmp_path / "ap" / "last_result.json")) if (tmp_path / "ap" / "last_result.json").exists() else {"result": "ERROR"}
    assert res["result"] != "DONE"
    assert any("STATE_WRITE_FAILED" in line for line in out)


def test_the_gate_verdict_is_an_allowlist_so_junk_never_proceeds(w, tmp_path, monkeypatch):
    """P3-1 (review): the check used to be `== NO_TRADE`, a denylist. Any verdict that is not the one outcome matching the session class stops before the park."""
    prepared(w)
    for junk in ({"verdict": None, "profile": "MEASUREMENT"}, {"verdict": "MAYBE", "profile": "MEASUREMENT"}, {"verdict": "ELIGIBLE", "profile": "MEASUREMENT"},
                 {"verdict": "ELIGIBLE_MEASUREMENT_ONLY", "profile": "PROFIT_SEEKING"}, {}, None):
        monkeypatch.setattr(AP, "session_gate", lambda *a, _j=junk, **k: _j)
        w.fake.requests.clear()
        res = AP.run_once(w.env, inner=w.fake, clock=w.clock.now, sleep=w.clock.sleep, out=lambda _m: None, state_root=str(w.tmp / "auto"), engine_root=str(tmp_path / ("ap%d" % id(junk))))
        assert res["result"] == "SKIPPED" and res["why"][0] == "NO_TRADE", (junk, res["why"])
        assert creates(w) == [] and not [r for r in w.fake.requests if r[0] == "POST"], junk
