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
def test_the_loop_honours_stop_alerts_on_not_flat_and_allows_one_loop_per_root(tmp_path):
    root = str(tmp_path / "ap")
    os.makedirs(root)
    open(os.path.join(root, "STOP"), "w").close()
    calls = []
    assert AP.loop({}, root=root, once=lambda *a, **k: calls.append(1) or {"result": "DONE"}, out=lambda _m: None) == 0 and calls == []
    os.remove(os.path.join(root, "STOP"))
    assert AP.loop({}, root=root, once=lambda *a, **k: {"result": "NOT_FLAT", "why": []}, out=lambda _m: None) == 3
    assert os.path.exists(os.path.join(root, "ALERT"))
    os.remove(os.path.join(root, "ALERT"))
    seq = iter([{"result": "ERROR", "why": ["x"]}] * 3)
    assert AP.loop({}, root=root, once=lambda *a, **k: next(seq), sleep=lambda _s: None, out=lambda _m: None) == 3                # three errors in a row stop it
    done = iter([{"result": "SKIPPED", "why": []}] * 2)
    assert AP.loop({}, root=root, once=lambda *a, **k: next(done), sleep=lambda _s: None, out=lambda _m: None, max_iterations=2) == 0


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


def test_the_autopilot_workflow_is_off_by_default_self_hosted_main_only_and_holds_no_exchange_secret():
    import re
    text = open(os.path.join(os.path.dirname(os.path.dirname(HERE)), ".github", "workflows", "testnet-autopilot.yml"), encoding="utf-8").read()
    assert "vars.TESTNET_AUTOPILOT_ENABLED == 'true'" in text and "github.ref == 'refs/heads/main'" in text and "github.repository == 'iceccarelli/ai-agent-control'" in text
    assert re.search(r"runs-on:\s*\[self-hosted, linux, bybit-testnet\]", text) and "ubuntu-latest" not in text
    assert "secrets." not in text.lower() and "pull_request" not in text and "LIVE_TRADING_ACK" not in text and "api.bybit.com" not in text
    assert re.search(r"^permissions:\n  contents: read\n", text, re.M) and "cancel-in-progress: false" in text
    steps = text[text.index("\njobs:"):]
    assert "ms_testnet_autopilot.py once" in steps and "--unattended" not in steps and "ms_testnet_session.py" not in steps     # the workflow only calls the autopilot; the grant gate lives in the engine


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


def test_the_loop_does_not_start_while_an_alert_exists_so_a_fly_restart_cannot_skip_the_human(tmp_path, stop_state):
    root = tmp_path / "ap"
    root.mkdir()
    (root / "ALERT").write_text("{}")
    calls = []
    msgs = []
    assert AP.loop({}, root=str(root), once=lambda *a, **k: calls.append(1) or {"result": "DONE"}, out=msgs.append) == 0 and calls == [] and "ALERT present" in msgs[0]


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
