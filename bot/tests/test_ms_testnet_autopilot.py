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
import ms_capital as CC  # noqa: E402
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
    assert w.run("execute", "--confirm-plan-digest", d, "--unattended", typed="") == 0, w.text()
    assert w.record()["flat_verified"] is True and w.record()["confirmation"]["state"] == "CONFIRMED"
    prov = json.load(open(os.path.join(w.state_dir, "autopilot.json")))
    assert prov["mode"] == "UNATTENDED_STANDING_AUTHORISATION" and prov["plan_digest"] == d and prov["grant_reference"] == "owner test 2026-11-10"


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
    assert w.run("execute", "--confirm-plan-digest", d, "--unattended", typed="") == 0
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


def test_a_profit_candidate_session_runs_labelled_profit_seeking_with_its_edge_evidence(w):
    write_grant(w, min_funding_bps=0.5, allow_mechanics_sessions=False)
    w.fake.fee_rates = {"spot": ("0.001", "0.001"), "linear": ("0.0002", "0.00055")}
    w.fake.funding_rate, w.fake.funding_in_ms = "0.0080", 600_000                                      # 80 bps: above the derived requirement with a measured tier
    res, _ = cycle(w)
    assert res["result"] == "DONE" and res["session_class"] == "PROFIT_SEEKING" and res["flat_verified"] is True
    ev = json.load(open(os.path.join(res["state_dir"], "edge_verdict.json")))
    assert ev["session_class"] == "PROFIT_SEEKING" and ev["edge"]["session_class"] == "PROFIT_CANDIDATE" and float(ev["edge"]["net_edge_bps"]) >= EG_MIN


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
    ok, why, info = AP.recover(w.env, inner=w.fake, clock=w.clock.now, state_root=str(w.tmp / "auto"))
    assert ok and why == [] and info["unresolved"] is True
    assert json.load(open(prior / "recovery.json"))["accounting_must_be_reconciled"] is True


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
    w.fake.funding_rate, w.fake.funding_in_ms = "0.0008", 600_000


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
def _profit_world(w):
    write_grant(w, min_funding_bps=0.5, allow_mechanics_sessions=False)
    w.fake.fee_rates = {"spot": ("0.001", "0.001"), "linear": ("0.0002", "0.00055")}
    w.fake.funding_rate, w.fake.funding_in_ms = "0.0080", 600_000


def _cycle_eng(w):
    eng = str(w.tmp / "eng")
    return AP.run_once(w.env, inner=w.fake, clock=w.clock.now, sleep=w.clock.sleep, out=[].append, state_root=str(w.tmp / "auto"), engine_root=eng), eng


def test_the_session_is_sized_by_the_allocator_not_the_grants_full_maximum_and_is_parked_for_settlement(w):
    _profit_world(w)
    res, eng = _cycle_eng(w)
    assert res["result"] == "DONE" and res["flat_verified"] is True, res
    plan = json.load(open(os.path.join(res["state_dir"], "plan.json")))
    assert float(plan["notional_usd"]) <= 200.0 + 1e-6 < 400.0                                        # grant max is 400: day one deploys 40% of the $500 ceiling
    assert res["allocation"]["binding_limit"] == "COMPOUNDED_SIZE"
    ev = json.load(open(os.path.join(res["state_dir"], "edge_verdict.json")))
    assert ev["allocation"]["notional_usd"] == "200.00" and ev["capital_settlement"]["status"] == "NO_PENDING"
    book = CC.CapitalBook(eng).load()
    assert book["start_cash_usd"] is not None and book["pending"]["session_id"] == os.path.basename(res["state_dir"]) and book["realised_usd"] == "0"   # parked, NOT credited yet


def test_cash_the_ledger_cannot_explain_raises_an_alert_and_places_nothing(w):
    _profit_world(w)
    eng = str(w.tmp / "eng")
    b = CC.CapitalBook(eng)
    b.ensure_baseline("1")
    b.park("prev_session", "1", "0.50", True, True)                                                   # the venue's cash is thousands of dollars away from this
    w.fake.requests.clear()
    res, _ = _cycle_eng(w)
    assert res["result"] == "REFUSED" and res["why"] == ["ACCOUNT_CASH_UNEXPLAINED"] and not [r for r in w.fake.requests if r[0] == "POST"]
    assert AP.ES.EngineState(eng, w.clock.now).has("ALERT")
    assert CC.CapitalBook(eng).load()["realised_usd"] == "0"                                          # and nothing unexplained was credited


def test_a_halted_book_keeps_the_engine_idle_until_the_owner_acknowledges(w):
    _profit_world(w)
    eng = str(w.tmp / "eng")
    b = CC.CapitalBook(eng)
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
