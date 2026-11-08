"""control_centre: a read-only, loopback-only operator UI on real backend data. Empty is never green; a basis is never upgraded; the HTTP surface is GET-only."""
from __future__ import annotations

import ast
import http.client
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
BOT = os.path.dirname(HERE)
sys.path.insert(0, BOT)
sys.path.insert(0, os.path.join(BOT, "tools"))

import control_centre as CC  # noqa: E402
import promotion_gate as PG  # noqa: E402

pytestmark = pytest.mark.real_entry_ledger      # owner_console runs the drill's real entry-limiter probe
NOW = 1_760_000_000_000                         # earlier than the real clock (owner_console only lets a caller move the clock earlier)
STATIC = os.path.join(BOT, "tools", "control_centre_static")


@pytest.fixture(autouse=True)
def loopback_only(monkeypatch):
    """The UI and its tests may only ever connect to 127.0.0.1."""
    real = socket.socket.connect

    def guard(self, addr, *a, **k):
        if not (isinstance(addr, tuple) and addr[0] in ("127.0.0.1", "::1", "localhost")):
            raise AssertionError(f"non-loopback connect attempted: {addr!r}")
        return real(self, addr, *a, **k)
    monkeypatch.setattr(socket.socket, "connect", guard)


# ------------------------------------------------------------------ synthetic but schema-true artifacts
def art(schema, basis, **kw):
    return {"schema": schema, "basis": basis, "generated_ms": NOW - 60_000, **kw}


def good_set(**over):
    sym = {"symbol": "ETHUSDT", "kind": "SPOT_PERP_CARRY", "spot_hedge": "HEDGEABLE_ON_BYBIT", "n_groups": 3, "gross_pnl_usd": 12.5, "fees_usd": 4.0, "net_pnl_usd": 8.5}
    sym2 = {"symbol": "PEPEUSDT", "kind": "PERP_DIRECTIONAL_FUNDING", "spot_hedge": "PERP_ONLY", "n_groups": 2, "gross_pnl_usd": 6.0, "fees_usd": 3.0, "net_pnl_usd": 3.0}
    s = {
        "replay_report.json": art("replay_report/v1", "SIMULATED", universe_scope="SURVIVORS_ONLY", period_start_ms=NOW - 86_400_000, period_end_ms=NOW - 1000, n_groups=5,
                                  groups_by_state={"CLOSED": 4, "UNHEDGED": 1}, gross_pnl_usd=18.5, fees_usd=7.0, net_pnl_usd=11.5, symbols=[sym, sym2]),
        "portfolio_state.json": art("portfolio_state/v1", "SIMULATED", equity_usd=1000.0, gross_exposure_usd=400.0, net_exposure_usd=10.0, unrealised_pnl_usd=1.25,
                                    positions=[{"symbol": "ETHUSDT", "kind": "SPOT_PERP_CARRY", "spot_hedge": "HEDGEABLE_ON_BYBIT", "side": "Sell", "notional_usd": 200.0, "entry_price": 3000.0,
                                                "mark_price": 3010.0, "unrealised_pnl_usd": 1.25, "group_id": "g1"}]),
        "risk_state.json": art("risk_state/v1", "CALCULATED", halted=False, halt_reasons=[], limits=[{"name": "gross exposure", "unit": "USD", "limit": 1000.0, "value": 400.0, "status": "OK"}],
                               alerts=[{"severity": "WARN", "code": "STALE_BOOK", "detail": "book 12 s old"}]),
        "ledger_pnl.json": art("ledger_pnl/v1", "SIMULATED", period_start_ms=None, period_end_ms=None, realised_gross_usd=18.5, fees_usd=7.0, funding_usd=2.0, realised_net_usd=11.5, unrealised_usd=1.25),
        "competition.json": art("competition/v1", "ESTIMATED", candidates=[
            {"candidate_id": "carry_eth", "kind": "SPOT_PERP_CARRY", "spot_hedge": "HEDGEABLE_ON_BYBIT", "status": "SUFFICIENT_EVIDENCE", "reason": None, "gross_bps_horizon": 30.0, "net_bps_horizon": 12.0, "n_observations": 40, "prov": "MODELLED"},
            {"candidate_id": "dir_pepe", "kind": "PERP_DIRECTIONAL_FUNDING", "spot_hedge": "PERP_ONLY", "status": "REJECTED", "reason": "net negative after costs in every fold", "gross_bps_horizon": 9.0, "net_bps_horizon": -2.0, "n_observations": 40, "prov": "MEASURED"}]),
        "opportunities.json": art("opportunities/v1", "ESTIMATED", decision_ms=NOW - 120_000, est_net_usd_per_day=None, items=[
            {"candidate_id": "carry_eth", "kind": "SPOT_PERP_CARRY", "symbols": ["ETHUSDT"], "spot_hedge": "HEDGEABLE_ON_BYBIT", "hold_days": 5.0, "gross_bps_horizon": 40.0, "net_bps_horizon": 15.0,
             "capacity_usd": 50_000.0, "prov": "MODELLED", "exclusions": []},
            {"candidate_id": "dir_pepe", "kind": "PERP_DIRECTIONAL_FUNDING", "symbols": ["PEPEUSDT"], "spot_hedge": "PERP_ONLY", "hold_days": 3.0, "gross_bps_horizon": 25.0, "net_bps_horizon": None,
             "capacity_usd": None, "prov": "ASSUMED", "exclusions": []}]),
    }
    s.update(over)
    return s


def write_set(d, files):
    os.makedirs(d, exist_ok=True)
    for name, obj in files.items():
        with open(os.path.join(d, name), "w", encoding="utf-8") as fh:
            fh.write(obj if isinstance(obj, str) else json.dumps(obj))
    return str(d)


def build(tmp_path, files=None, *, env=None, data_root="mk", universe=None, **kw):
    ad = write_set(tmp_path / "artifacts", files) if files is not None else str(tmp_path / "no-artifacts")
    dr = None
    if data_root == "mk":
        dr = tmp_path / "root"
        dr.mkdir(exist_ok=True)
        os.chmod(dr, 0o700)
        dr = str(dr)
    return CC.build_state(data_root=dr, universe_dir=universe, artifacts_dir=ad, env={} if env is None else env, now_ms=NOW, **kw)


def walk(o, path=""):
    yield path, o
    if isinstance(o, dict):
        for k, v in o.items():
            yield from walk(v, f"{path}.{k}")
    elif isinstance(o, list):
        for i, v in enumerate(o):
            yield from walk(v, f"{path}[{i}]")


def values(state):
    return [(p, o) for p, o in walk(state) if isinstance(o, dict) and "v" in o and "basis" in o]


@pytest.fixture(scope="module")
def empty_state(tmp_path_factory):
    return build(tmp_path_factory.mktemp("empty"), None)


@pytest.fixture(scope="module")
def full_state(tmp_path_factory):
    return build(tmp_path_factory.mktemp("full"), good_set())


# ------------------------------------------------------------------ empty => explicit empty states, nothing green
def test_empty_root_every_section_is_an_explicit_empty_state_and_nothing_is_green(empty_state):
    s = empty_state
    assert s["portfolio"]["state"] == "NO_DATA" and s["portfolio"]["pnl"]["state"] == "NO_DATA"
    assert s["execution"]["state"] == "NO_DATA" and s["risk"]["state"] == "NO_DATA"
    assert s["research"]["competition"]["state"] == "NO_DATA" and s["research"]["opportunities"]["state"] == "NO_DATA"
    assert s["evidence"]["freshness"]["state"] == "NO_DATA" and s["evidence"]["freshness"]["verdict"] == "NO_DATA"
    assert s["evidence"]["universe_capture"]["state"] == "NO_DATA"
    assert all(x["state"] == "NO_DATA" for x in s["evidence"]["sources"])
    for sec in (s["portfolio"], s["execution"], s["risk"]):
        assert "No " in sec.get("empty", "") and ("Run" in sec["empty"] or "run" in sec["empty"])
    ov = s["overview"]
    assert ov["headline"]["status"] == "NO_DATA"
    assert "OK" not in [c["status"] for c in ov["cards"]] and ov["headline"]["status"] != "OK"
    by = {c["id"]: c for c in ov["cards"]}
    for cid in ("data", "operations", "opportunity", "exposure", "pnl"):
        assert by[cid]["status"] == "NO_DATA" and by[cid]["basis"] == "MISSING"
    assert not [p for p, o in values(s) if o["v"] is not None and o["basis"] in ("MEASURED", "EXCHANGE_REPORTED", "SIMULATED", "OWNER_SUPPLIED_UNVERIFIED")]
    assert s["research"]["sufficient_evidence"] == [] and all(not r["authorised"] for r in s["research"]["candidates"])


def test_a_populated_state_can_be_green_only_where_a_checker_says_so_positive_control(full_state, tmp_path, monkeypatch):
    by = {c["id"]: c for c in full_state["overview"]["cards"]}
    assert by["operations"]["status"] == "ATTENTION"            # the risk file carries a WARN alert: not green
    clean = good_set()
    clean["risk_state.json"]["alerts"] = []
    s = build(tmp_path, clean)
    assert {c["id"]: c for c in s["overview"]["cards"]}["operations"]["status"] == "OK"      # real data, no alert, no breach, no halt
    assert s["overview"]["headline"]["status"] == "NO_DATA"     # but market data is still missing, so the system is not called operating correctly


def test_every_value_has_a_basis_from_the_closed_vocabulary_and_null_is_missing(empty_state, full_state):
    for st in (empty_state, full_state):
        vs = values(st)
        assert vs
        for p, o in vs:
            assert o["basis"] in CC.BASES, p
            assert (o["v"] is None) == (o["basis"] == "MISSING"), p
    assert [b["code"] for b in full_state["legend"]["basis"]] == list(CC.BASES)
    with pytest.raises(AssertionError):
        CC.val(1, "GREAT")


# ------------------------------------------------------------------ populated => right basis labels
def test_populated_values_carry_the_right_basis_labels(full_state):
    s = full_state
    assert s["portfolio"]["state"] == "OK"
    assert s["portfolio"]["gross_exposure"] == {"v": 400.0, "basis": "SIMULATED", "unit": "USD", "note": None}
    assert s["portfolio"]["positions"][0]["notional"]["basis"] == "SIMULATED"
    assert s["execution"]["gross"]["basis"] == "SIMULATED" and s["execution"]["net"]["basis"] == "SIMULATED"
    items = {i["candidate_id"]: i for i in s["research"]["opportunities"]["items"]}
    assert items["carry_eth"]["net"]["basis"] == "ESTIMATED" and items["carry_eth"]["net"]["v"] == 15.0      # MODELLED -> ESTIMATED
    assert items["dir_pepe"]["gross"]["basis"] == "ESTIMATED"                                                 # ASSUMED -> ESTIMATED
    comp = {c["candidate_id"]: c for c in s["research"]["competition"]["candidates"]}
    assert comp["dir_pepe"]["net"]["basis"] == "MEASURED" and comp["carry_eth"]["net"]["basis"] == "ESTIMATED"
    assert s["risk"]["limits"][0]["value"]["basis"] == "CALCULATED"
    assert s["research"]["opportunities"]["best_net"]["v"] == 15.0 and s["research"]["opportunities"]["n_positive_net"] == 1
    assert s["environment"]["capital_usd"]["v"] == 0.0 and s["environment"]["capital_usd"]["basis"] == "CALCULATED"
    assert "dir_pepe" in [r["candidate"] for r in s["research"]["rejected"]] and "carry_eth" in s["research"]["sufficient_evidence"]
    assert any("net negative" in r["why"] for r in s["research"]["rejected"])


def test_a_simulated_ledger_is_never_presented_as_realised(full_state, tmp_path):
    pnl = full_state["portfolio"]["pnl"]
    assert pnl["kind"] == "SIMULATED_NOT_REALISED"
    assert pnl["simulated_net"]["v"] == 11.5 and pnl["simulated_net"]["basis"] == "SIMULATED"
    assert pnl["realised_net"]["v"] is None and pnl["realised_gross"]["v"] is None and pnl["realised_net"]["basis"] == "MISSING"
    files = good_set(**{"ledger_pnl.json": art("ledger_pnl/v1", "EXCHANGE_REPORTED", period_start_ms=None, period_end_ms=None, realised_gross_usd=20.0, fees_usd=5.0, funding_usd=1.0, realised_net_usd=16.0, unrealised_usd=None)})
    p2 = build(tmp_path, files)["portfolio"]["pnl"]
    assert p2["kind"] == "ACCOUNT_REALISED" and p2["realised_net"]["basis"] == "EXCHANGE_REPORTED" and p2["simulated_net"]["v"] is None and p2["unrealised"]["basis"] == "MISSING"
    files = good_set(**{"ledger_pnl.json": art("ledger_pnl/v1", "OWNER_SUPPLIED_UNVERIFIED", period_start_ms=None, period_end_ms=None, realised_gross_usd=20.0, fees_usd=None, funding_usd=None, realised_net_usd=None, unrealised_usd=None)})
    p3 = build(tmp_path, files)["portfolio"]["pnl"]
    assert p3["kind"] == "OWNER_IMPORT_UNVERIFIED" and p3["realised_gross"]["basis"] == "OWNER_SUPPLIED_UNVERIFIED"


def test_gross_and_net_are_never_conflated_at_field_level(full_state, tmp_path):
    items = {i["candidate_id"]: i for i in full_state["research"]["opportunities"]["items"]}
    pepe = items["dir_pepe"]                                    # gross 25 bps stated, net unknown
    assert pepe["gross"]["v"] == 25.0 and pepe["net"]["v"] is None and pepe["net"]["basis"] == "MISSING" and pepe["eligible"] is False
    assert items["carry_eth"]["gross"]["v"] != items["carry_eth"]["net"]["v"]
    ex = full_state["execution"]
    assert ex["gross"]["v"] == 18.5 and ex["net"]["v"] == 11.5 and ex["fees"]["v"] == 7.0
    assert "GROSS" in ex["gross"]["note"] and "NET" in ex["net"]["note"] and "GROSS" not in ex["net"]["note"]
    for s in ex["symbols"]:
        assert set(s) >= {"gross", "fees", "net"} and s["gross"]["note"] != s["net"]["note"]
    led = good_set(**{"ledger_pnl.json": art("ledger_pnl/v1", "MEASURED", period_start_ms=None, period_end_ms=None, realised_gross_usd=30.0, fees_usd=None, funding_usd=None, realised_net_usd=None, unrealised_usd=None)})
    p = build(tmp_path, led)["portfolio"]["pnl"]
    assert p["realised_gross"]["v"] == 30.0 and p["realised_net"]["v"] is None            # net is never inferred from gross
    comp = {c["candidate_id"]: c for c in full_state["research"]["competition"]["candidates"]}
    assert comp["dir_pepe"]["gross"]["v"] == 9.0 and comp["dir_pepe"]["net"]["v"] == -2.0


def test_perp_only_symbols_are_never_rendered_as_carry(full_state, tmp_path):
    items = {i["candidate_id"]: i for i in full_state["research"]["opportunities"]["items"]}
    assert items["carry_eth"]["is_carry"] is True and items["carry_eth"]["label"] == "Spot-perp carry (hedged)"      # positive control
    assert items["dir_pepe"]["is_carry"] is False and "not carry" in items["dir_pepe"]["label"].lower()
    bad = good_set()
    bad["opportunities.json"]["items"][0]["spot_hedge"] = "PERP_ONLY"                                   # a file CLAIMS carry on a perp-only symbol
    bad["opportunities.json"]["items"][1]["kind"] = "SPOT_PERP_CARRY"
    s = build(tmp_path, bad)
    for it in s["research"]["opportunities"]["items"]:
        assert it["is_carry"] is False and "Spot-perp carry (hedged)" not in it["label"] and it["label"].startswith("Not shown as carry")
    for sym in full_state["execution"]["symbols"]:
        assert sym["is_carry"] == (sym["spot_hedge"] == "HEDGEABLE_ON_BYBIT" and sym["kind"] == "SPOT_PERP_CARRY")
    assert full_state["portfolio"]["positions"][0]["is_carry"] is True
    for p, o in walk(full_state):
        if isinstance(o, dict) and o.get("spot_hedge") == "PERP_ONLY" and "label" in o:
            assert not o["is_carry"], p


def test_survivors_only_results_are_never_rendered_as_survivorship_free(full_state, tmp_path, monkeypatch):
    sv = full_state["execution"]["survivorship"]
    assert sv["claimed_by_file"] == "SURVIVORS_ONLY" and sv["survivorship_free"] is False and "SURVIVORS ONLY" in sv["text"] and "not be read as survivorship-free" in sv["text"]
    claim = good_set()
    claim["replay_report.json"]["universe_scope"] = "DELISTINGS_OBSERVED"
    uncorroborated = build(tmp_path, claim)["execution"]["survivorship"]
    assert uncorroborated["survivorship_free"] is False and uncorroborated["corroborated_here"] is False       # the file's claim alone is not enough
    uni = tmp_path / "uni"
    uni.mkdir()
    monkeypatch.setattr(CC.UC, "verify_capture_dir", lambda p: {"ok": True, "problems": [], "coverage": {"dataset_status": "COMPLETE", "delisting_observability": "OBSERVED", "status_coverage": {"Closed": "OBSERVED_ROWS"}}})
    ok = build(tmp_path, claim, universe=str(uni))
    assert ok["execution"]["survivorship"]["survivorship_free"] is True                                         # positive control: claim AND corroboration
    monkeypatch.setattr(CC.UC, "verify_capture_dir", lambda p: {"ok": True, "problems": [], "coverage": {"dataset_status": "COMPLETE", "delisting_observability": "NOT_OBSERVED", "status_coverage": {"Closed": "REFUSED"}}})
    assert build(tmp_path, claim, universe=str(uni))["execution"]["survivorship"]["survivorship_free"] is False
    bad = good_set()
    bad["replay_report.json"]["universe_scope"] = "SURVIVORSHIP_FREE"
    assert build(tmp_path, bad)["execution"]["state"] == "ERROR"


def test_an_unverified_capture_is_labelled_unverified_not_measured(tmp_path, monkeypatch):
    uni = tmp_path / "uni"
    uni.mkdir()
    cov = {"dataset_status": "COMPLETE", "delisting_observability": "OBSERVED", "status_coverage": {"Closed": "OBSERVED_ROWS"}}
    monkeypatch.setattr(CC.UC, "verify_capture_dir", lambda p: {"ok": True, "problems": [], "coverage": cov})
    cap = CC._capture(str(uni))
    assert cap["integrity"]["basis"] == "UNVERIFIED" and cap["integrity"]["v"] == "RAW_RESPONSES_VERIFIED" and "NOT authenticated" in cap["integrity"]["note"]
    monkeypatch.setattr(CC.UC, "verify_capture_dir", lambda p: {"ok": False, "problems": ["hash mismatch"], "coverage": {"dataset_status": "UNVERIFIED", "delisting_observability": "NOT_OBSERVED"}})
    bad = CC._capture(str(uni))
    assert bad["state"] == "ERROR" and bad["integrity"]["basis"] == "MISSING" and bad["survivorship"] == "SURVIVORS_ONLY"
    # captured timestamps are MEASURED only when this run re-hashed the data
    report = {"axes": {"data_freshness": {"verdict": "HEALTHY", "issues": []}, "research_readiness": {"verdict": "READY", "issues": []}, "code_correctness": {"verdict": "NOT_ASSESSED"}},
              "cycles": {"latest_complete_cycle": {"cycle": "c1", "age_ms": 1000}}, "status_partitions": {"present": [], "missing": []}, "backups": {},
              "history": {"d": {"dataset_status": "COMPLETE", "verify": "SKIPPED", "symbols": {"X": {"latest_observed_ts": NOW - 1000}}, "stale_symbols": [], "incomplete_symbols": []}}}
    monkeypatch.setattr(CC.DFA, "audit", lambda *a, **k: report)
    root = tmp_path / "r"
    root.mkdir()
    f0 = CC._freshness(str(root), None, NOW, False)
    f1 = CC._freshness(str(root), None, NOW, True)
    assert f0["datasets"][0]["age_ms"]["basis"] == "UNVERIFIED" and f0["latest_cycle"]["age_ms"]["basis"] == "UNVERIFIED"
    assert f1["datasets"][0]["age_ms"]["basis"] == "MEASURED"


# ------------------------------------------------------------------ defensive artifact loading
def _symlink(d, name):
    outside = d.parent / "outside.json"
    outside.write_text(json.dumps(good_set()[name]))
    os.symlink(outside, d / name)
    return None


BAD = {
    "garbage": lambda d, n: (d / n).write_text("{not json"),
    "nan": lambda d, n: (d / n).write_text(json.dumps(good_set()[n]).replace("400.0", "NaN") if n == "portfolio_state.json" else '{"schema": "x", "v": NaN}'),
    "infinity_by_overflow": lambda d, n: (d / n).write_text(json.dumps(good_set()[n]).replace("400.0", "1e999") if n == "portfolio_state.json" else "[1e999]"),
    "oversize": lambda d, n: (d / n).write_text(" " * (CC.MAX_ARTIFACT_BYTES + 10) + json.dumps(good_set()[n])),
    "symlink": lambda d, n: _symlink(d, n),
    "directory": lambda d, n: (d / n).mkdir(),
    "not_an_object": lambda d, n: (d / n).write_text("[1, 2, 3]"),
    "wrong_schema": lambda d, n: (d / n).write_text(json.dumps({**good_set()[n], "schema": "other/v9"})),
    "basis_not_allowed": lambda d, n: (d / n).write_text(json.dumps({**good_set()[n], "basis": "MEASURED" if n == "replay_report.json" else "NONSENSE"})),
    "future": lambda d, n: (d / n).write_text(json.dumps({**good_set()[n], "generated_ms": NOW + 10 * 86_400_000})),
    "invalid_utf8": lambda d, n: (d / n).write_bytes(b"\xff\xfe\x00{"),
    "deep_nesting": lambda d, n: (d / n).write_text("[" * 100_000 + "]" * 100_000),
}


@pytest.mark.parametrize("kind", sorted(BAD))
@pytest.mark.parametrize("name", sorted(CC.ARTIFACTS))
def test_a_malformed_oversize_symlink_or_nan_artifact_is_an_error_state_never_a_crash_or_a_guess(tmp_path, kind, name):
    d = tmp_path / "artifacts"
    write_set(d, {k: v for k, v in good_set().items() if k != name})
    BAD[kind](d, name)
    a = CC.load_artifact(str(d), name, NOW)
    assert a["state"] == "ERROR" and a["data"] is None and a["error"], (kind, name)
    others = [CC.load_artifact(str(d), n, NOW) for n in CC.ARTIFACTS if n != name]
    assert all(o["state"] == "OK" for o in others)              # one bad file does not poison the rest


def test_an_error_artifact_surfaces_in_the_state_with_the_file_named_and_no_value(tmp_path):
    files = good_set()
    files["portfolio_state.json"] = "{broken"
    s = build(tmp_path, files)
    assert s["portfolio"]["state"] == "ERROR" and "portfolio_state.json" in s["portfolio"]["error"]
    assert "gross_exposure" not in s["portfolio"] and "positions" not in s["portfolio"]
    src = {x["file"]: x for x in s["evidence"]["sources"]}
    assert src["portfolio_state.json"]["state"] == "ERROR" and src["risk_state.json"]["state"] == "OK"
    assert any(a["source"] == "artifacts" and "portfolio_state.json" in a["text"] and a["severity"] == "FAIL" for a in s["overview"]["attention"])
    assert {c["id"]: c for c in s["overview"]["cards"]}["exposure"]["status"] == "FAIL"
    json.dumps(s, allow_nan=False)


def test_a_missing_required_key_a_bool_for_a_number_and_a_rejection_without_reason_are_errors(tmp_path):
    cases = {
        "portfolio_state.json": lambda o: o.pop("gross_exposure_usd"),
        "ledger_pnl.json": lambda o: o.update(realised_net_usd=True),
        "competition.json": lambda o: o["candidates"][1].update(reason=""),
        "opportunities.json": lambda o: o["items"][0].update(kind="DIRECTIONAL_CARRY"),
        "risk_state.json": lambda o: o.update(halted="no"),
        "replay_report.json": lambda o: o.update(groups_by_state={"MADE_UP": 1}),
    }
    for name, mutate in cases.items():
        files = good_set()
        mutate(files[name])
        assert CC.load_artifact(write_set(tmp_path / name, files), name, NOW)["state"] == "ERROR", name


def test_nothing_in_the_state_is_nan_or_infinite(full_state, empty_state):
    for st in (full_state, empty_state):
        json.dumps(st, allow_nan=False)
        assert all(not (isinstance(o, float) and (o != o or o in (float("inf"), float("-inf")))) for _p, o in walk(st))
    assert CC._clean({"a": float("nan"), "b": [float("inf")]}) == {"a": None, "b": [None]}


# ------------------------------------------------------------------ actions and safety, from real checkers
def test_actions_lists_what_is_permitted_and_a_reason_chain_for_each_blocked_action(full_state):
    acts = {a["id"]: a for a in full_state["actions"]["items"]}
    assert acts["research"]["permitted"] and acts["replay"]["permitted"]
    assert acts["testnet"]["permitted"] is False and any("owner-run" in r or "none is recorded" in r for r in acts["testnet"]["reasons"])
    m = acts["mainnet"]
    assert m["permitted"] is False and any("not armed" in r for r in m["reasons"]) and any("promotion gate REFUSE" in r for r in m["reasons"])
    assert full_state["environment"]["gate"]["decision"] == "REFUSE" and full_state["environment"]["live_authorized"] is False
    assert [b["label"] for b in full_state["overview"]["blocked_now"]] == ["Testnet session", "Mainnet (real money)"]


def test_the_safety_card_follows_the_real_gate_not_a_constant(tmp_path, monkeypatch):
    class V:
        allows_live, items, reasons = True, [], []
    monkeypatch.setattr(PG, "evaluate_promotion_gate", lambda *a, **k: V())
    s = build(tmp_path, None)
    by = {c["id"]: c for c in s["overview"]["cards"]}
    assert by["safety"]["status"] == "FAIL" and s["overview"]["headline"]["status"] == "FAIL"
    assert any(a["source"] == "safety" for a in s["overview"]["attention"])
    monkeypatch.setattr(PG, "evaluate_promotion_gate", lambda *a, **k: (_ for _ in ()).throw(OSError("unreadable")))
    s2 = build(tmp_path, None)
    assert s2["environment"]["gate"]["decision"] == "REFUSE"          # an unreadable gate is a closed gate


def test_environment_findings_reach_the_attention_list_and_secrets_are_never_shown(tmp_path):
    s = build(tmp_path, None, env={"BYBIT_VENUE": "nowhere", "BYBIT_API_KEY": "KEYVALUE12345", "BYBIT_API_SECRET": "SECRETVALUE6789"})
    assert any(a["source"] == "environment" and "BYBIT_VENUE" in a["text"] for a in s["overview"]["attention"])
    text = json.dumps(s)
    assert "KEYVALUE12345" not in text and "SECRETVALUE6789" not in text
    assert s["environment"]["secrets"]["BYBIT_API_KEY"] == "set"


def test_a_halted_risk_state_and_critical_alerts_are_failures(tmp_path):
    files = good_set()
    files["risk_state.json"].update(halted=True, halt_reasons=["kill switch engaged"], alerts=[{"severity": "CRITICAL", "code": "HEDGE_BROKEN", "detail": "leg lost"}],
                                    limits=[{"name": "gross", "unit": "USD", "limit": 1.0, "value": 2.0, "status": "BREACH"}])
    s = build(tmp_path, files)
    assert {c["id"]: c for c in s["overview"]["cards"]}["operations"]["status"] == "FAIL" and s["overview"]["headline"]["status"] == "FAIL"
    assert len([a for a in s["overview"]["attention"] if a["source"] == "risk"]) == 3
    assert any("UNHEDGED" in a["text"] for a in s["overview"]["attention"])        # from the replay's group states


def test_a_stale_artifact_is_flagged(tmp_path):
    files = good_set()
    files["risk_state.json"]["generated_ms"] = NOW - 10 * 86_400_000
    s = build(tmp_path, files)
    assert s["risk"]["stale"] is True and any("risk_state.json is older" in a["text"] for a in s["overview"]["attention"])


def test_research_rows_never_authorise_anything(full_state):
    rows = full_state["research"]["candidates"]
    assert rows and all(r["authorised"] is False for r in rows)
    assert all(any("Promotion gate: REFUSE" in w for w in r["why_not_authorised"]) for r in rows)
    assert {r["stage"] for r in rows} <= {"NOT_READY", "DATA_READY", "RESEARCH_READY", "PROMISING", "PROSPECTIVE", "EXECUTION_READY", "OWNER_PROFIT_EVIDENCED"}
    assert not any(r["stage"] in ("EXECUTION_READY", "OWNER_PROFIT_EVIDENCED") for r in rows)


# ------------------------------------------------------------------ the module itself
def test_the_module_imports_no_venue_client_transport_or_signing_and_no_network_library():
    src = open(os.path.join(BOT, "tools", "control_centre.py"), encoding="utf-8").read()
    tree = ast.parse(src)
    mods = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            mods |= {a.name for a in n.names}
        elif isinstance(n, ast.ImportFrom):
            mods.add(n.module or "")
    banned = ("bybit", "requests", "urllib.request", "http.client", "socket", "websocket", "httpx", "aiohttp", "subprocess", "hmac", "hashlib", "transport", "signing", "carry_broker", "broker")
    assert not [m for m in mods if any(b in m.lower() for b in banned)], mods
    assert "BybitClient" not in src and "place_order" not in src and "/v5/" not in src
    for name in ("do_POST", "do_PUT", "do_DELETE", "do_PATCH"):
        assert name in src                                          # present only to answer 405
    assert "def do_GET" in src
    for n in ast.walk(tree):                                        # no file is ever opened for writing and nothing is deleted or renamed
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "open":
            modes = [a.value for a in n.args[1:2] if isinstance(a, ast.Constant)] + [k.value.value for k in n.keywords if k.arg == "mode" and isinstance(k.value, ast.Constant)]
            assert all(m in ("rb", "r") for m in modes), modes
        if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id == "os":
            assert n.attr not in ("remove", "unlink", "rename", "replace", "rmdir", "mkdir", "makedirs", "write", "system", "popen", "chmod", "symlink")


# ------------------------------------------------------------------ HTTP
@pytest.fixture()
def server(tmp_path):
    ad = write_set(tmp_path / "artifacts", good_set())
    root = tmp_path / "root"
    root.mkdir()
    os.chmod(root, 0o700)
    cfg = CC._Config(str(root), None, ad, False, False, clock=lambda: NOW, ttl_ms=10 ** 12, env={})
    srv = CC.make_server(cfg, "127.0.0.1", 0)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield srv
    srv.shutdown()
    srv.server_close()
    t.join(5)


def req(srv, method, path, host=None, body=None):
    port = srv.server_address[1]
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
    try:
        c.putrequest(method, path, skip_host=True)
        c.putheader("Host", host if host is not None else f"127.0.0.1:{port}")
        c.endheaders(body)
        r = c.getresponse()
        return r.status, {k.lower(): v for k, v in r.getheaders()}, r.read()
    finally:
        c.close()


def test_get_serves_the_page_the_three_static_files_state_and_health_with_security_headers(server):
    for path, ctype in (("/", "text/html"), ("/static/index.html", "text/html"), ("/static/app.js", "text/javascript"), ("/static/style.css", "text/css"), ("/api/state", "application/json"), ("/api/health", "application/json")):
        st, h, body = req(server, "GET", path)
        assert st == 200 and h["content-type"].startswith(ctype), path
        assert "default-src 'self'" in h["content-security-policy"] and h["x-content-type-options"] == "nosniff" and h["cache-control"] == "no-store", path
    st, _h, body = req(server, "GET", "/api/state")
    state = json.loads(body, parse_constant=lambda c: (_ for _ in ()).throw(ValueError(c)))
    assert state["schema"] == "control_centre/v1" and state["portfolio"]["state"] == "OK"
    assert json.loads(req(server, "GET", "/api/health")[2]) == {"ok": True}
    assert req(server, "GET", f"/api/state?file=/etc/passwd&path=../../x")[0] == 200            # a query string is ignored, never a file path
    assert b"root:" not in req(server, "GET", "/api/state?file=/etc/passwd")[2]


@pytest.mark.parametrize("host", ["evil.example.com", "evil.example.com:80", "127.0.0.1.evil.com", "localhost.evil.com", "0.0.0.0", "192.168.1.5", "127.0.0.1:1", "[::1]", "", "a" * 200])
def test_a_non_localhost_host_header_is_rejected_dns_rebinding(server, host):
    for path in ("/", "/api/state", "/api/health", "/static/app.js"):
        st, _h, body = req(server, "GET", path, host=host)
        assert st == 403 and b"portfolio" not in body, (host, path)


def test_the_host_header_is_accepted_for_localhost_and_loopback_with_the_right_port(server):
    port = server.server_address[1]
    for host in (f"localhost:{port}", f"127.0.0.1:{port}", "127.0.0.1", "LOCALHOST"):
        assert req(server, "GET", "/api/health", host=host)[0] == 200, host


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD", "TRACE", "FOO"])
def test_every_method_but_get_is_405_with_no_side_effect(server, method):
    for path in ("/", "/api/state", "/api/health", "/static/app.js", "/anything"):
        st, h, body = req(server, method, path, body=b"{}" if method in ("POST", "PUT", "PATCH") else None)
        assert st == 405 and h.get("allow") == "GET", (method, path)
        assert b"portfolio" not in body


@pytest.mark.parametrize("path", ["/static/../control_centre.py", "/static/..%2fcontrol_centre.py", "/static/%2e%2e/control_centre.py", "/static/../../config.py", "/static//etc/passwd",
                                  "/static/app.js/../../control_centre.py", "/static/", "/static", "/static/app.js.bak", "/static/APP.JS", "/static/index.html%00.js", "/static/..\\app.js",
                                  "/api", "/api/", "/api/state/", "/api/order", "/admin", "/etc/passwd", "/..", "//etc/passwd", "/api/action", "/static/__init__.py", "/static/control_centre.py"])
def test_path_traversal_and_every_other_path_is_404(server, path):
    st, _h, body = req(server, "GET", path)
    assert st == 404, (path, st)
    assert b"import " not in body and b"root:" not in body


def test_the_server_refuses_any_bind_host_but_loopback():
    cfg = CC._Config(None, None, None, False, False)
    for host in ("0.0.0.0", "", "::", "192.168.0.2", "localhost", "example.com"):
        with pytest.raises(ValueError):
            CC.make_server(cfg, host, 0)
    srv = CC.make_server(cfg, "127.0.0.1", 0)
    try:
        assert srv.server_address[0] == "127.0.0.1"
    finally:
        srv.server_close()


def test_the_handler_does_not_log_requests_or_env_values(server, capfd):
    req(server, "GET", "/api/health")
    out, err = capfd.readouterr()
    assert "GET" not in out + err and "/api/health" not in out + err


def test_an_internal_failure_is_a_500_without_a_traceback(tmp_path, monkeypatch):
    cfg = CC._Config(None, None, None, False, False, clock=lambda: NOW, ttl_ms=0, env={})
    monkeypatch.setattr(CC, "build_state", lambda **k: (_ for _ in ()).throw(RuntimeError("secret detail /home/x")))
    srv = CC.make_server(cfg, "127.0.0.1", 0)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        st, _h, body = req(srv, "GET", "/api/state")
        assert st == 500 and b"secret detail" not in body and b"Traceback" not in body
    finally:
        srv.shutdown()
        srv.server_close()


# ------------------------------------------------------------------ the static bundle
def _bundle():
    return {n: open(os.path.join(STATIC, n), encoding="utf-8").read() for n in ("index.html", "app.js", "style.css")}


def test_the_static_directory_holds_exactly_the_three_allow_listed_files():
    assert sorted(os.listdir(STATIC)) == sorted(CC.STATIC_FILES) == ["app.js", "index.html", "style.css"]


def test_the_bundle_contains_no_external_url_font_or_cdn():
    for name, text in _bundle().items():
        assert not re.search(r"(?i)\b(?:https?|wss?|ftp)://", text), name
        assert not re.search(r"""(?i)(?:src|href|action|content)\s*=\s*["']\s*//""", text), name
        assert not re.search(r"(?i)@import|url\(\s*[\"']?(?!#)", text), name
        assert not re.search(r"(?i)fonts\.(?:googleapis|gstatic)|cdn\.|unpkg|jsdelivr|cdnjs|@font-face", text), name
    html = _bundle()["index.html"]
    assert set(re.findall(r"""(?:src|href)=["']([^"'#]+)""", html)) == {"data:,", "/static/style.css", "/static/app.js"}


def test_the_bundle_is_csp_compatible_no_eval_no_inline_script_style_or_event_handler():
    b = _bundle()
    js, html = b["app.js"], b["index.html"]
    assert not re.search(r"\beval\s*\(|\bnew\s+Function\b|setTimeout\s*\(\s*[\"']|setInterval\s*\(\s*[\"']|document\.write|\.innerHTML|\.outerHTML|insertAdjacentHTML|javascript:", js)
    assert not re.search(r"(?i)<script(?![^>]*\ssrc=)[^>]*>", html), "inline script"
    assert not re.search(r"(?i)<style", html) and not re.search(r"(?i)\sstyle\s*=", html) and not re.search(r"""(?i)\.setAttribute\(\s*["']style""", js)
    assert not re.search(r"(?i)\son[a-z]+\s*=", html) and not re.search(r"""(?i)setAttribute\(\s*["']on""", js) and not re.search(r"\.on[a-z]+\s*=", js)
    assert not re.search(r"XMLHttpRequest|WebSocket|EventSource|sendBeacon|importScripts|\bimport\s*\(|\.style\.", js)


def test_the_only_request_the_page_makes_is_a_same_origin_get_of_api_state():
    js = _bundle()["app.js"]
    calls = re.findall(r"fetch\s*\(([^)]*)\)?", js)
    assert calls and all(re.match(r"""\s*["']/api/[a-z/]+["']""", c) for c in calls), calls
    assert not re.search(r"""(?i)method\s*:\s*["'](?:post|put|delete|patch)""", js) and "<form" not in _bundle()["index.html"].lower()
    assert not re.search(r"(?i)<(?:button|input)[^>]*>", _bundle()["index.html"].replace('<button id="refresh" type="button" class="btn">Refresh</button>', ""))


def test_the_ui_has_no_control_that_implies_it_can_trade():
    b = " ".join(_bundle().values()).lower()
    for word in ("place order", "buy now", "sell now", "execute trade", "submit order", "go live", "arm ", "cancel order", "confirm trade", "start trading"):
        assert word not in b, word
    assert "cannot trade" in b and "read-only" in b


def test_the_page_labels_every_basis_and_keeps_the_legend_on_every_view():
    b = _bundle()
    assert "legend" in b["index.html"] and b["index.html"].index('id="legend"') < b["index.html"].index('id="view"')       # outside the per-view container
    assert 'name="viewport"' in b["index.html"] and "prefers-color-scheme: dark" in b["style.css"] and ":focus-visible" in b["style.css"] and "lang=\"en\"" in b["index.html"]


# ------------------------------------------------------------------ render smoke (node playwright; python playwright is not installed here)
NODE_CHECK = r"""
const { chromium } = require('playwright');
const base = process.argv[2], outDir = process.argv[3], exe = process.argv[4];
(async () => {
  const browser = await chromium.launch({ executablePath: exe, headless: true, args: ['--no-sandbox'] });
  const results = [];
  for (const width of [1280, 390]) {
    const ctx = await browser.newContext({ viewport: { width, height: 900 } });
    const page = await ctx.newPage();
    const errors = [], external = [];
    page.on('console', m => { if (m.type() === 'error') errors.push(m.text()); });
    page.on('pageerror', e => errors.push(String(e)));
    page.on('request', r => { if (!r.url().startsWith(base)) external.push(r.url()); });
    for (const view of ['overview', 'research', 'portfolio', 'execution', 'risk', 'evidence']) {
      await page.goto(base + '/#/' + view, { waitUntil: 'networkidle' });
      await page.waitForSelector('#view-h');
      const m = await page.evaluate(() => ({ sw: document.documentElement.scrollWidth, cw: document.documentElement.clientWidth, h: document.getElementById('view-h').textContent,
        legend: document.querySelectorAll('#legend .badge').length, text: document.getElementById('view').innerText.length, current: document.querySelector('.rail a[aria-current]').textContent }));
      await page.screenshot({ path: outDir + '/' + process.argv[5] + '-' + view + '-' + width + '.png', fullPage: true });
      results.push({ width, view, ...m, errors: errors.slice(), external: external.slice() });
    }
    await ctx.close();
  }
  await browser.close();
  console.log(JSON.stringify(results));
})().catch(e => { console.error(e); process.exit(1); });
"""
CHROME = "/opt/pw-browsers/chromium-1243/chrome-linux64/chrome"
NODE_MODULES = "/opt/node22/lib/node_modules"


def _render(srv, tmp_path, tag):
    node = shutil.which("node")
    if not node or not os.path.exists(CHROME) or not os.path.isdir(os.path.join(NODE_MODULES, "playwright")):
        pytest.skip("render smoke needs node + node playwright + chromium at /opt/pw-browsers (python playwright.sync_api is not installed); not available here")
    script = tmp_path / "render_check.js"
    script.write_text(NODE_CHECK)
    shots = tmp_path / "shots"
    shots.mkdir(exist_ok=True)
    env = {"PATH": os.environ.get("PATH", ""), "NODE_PATH": NODE_MODULES, "HOME": str(tmp_path), "NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost"}
    p = subprocess.run([node, str(script), f"http://127.0.0.1:{srv.server_address[1]}", str(shots), CHROME, tag], capture_output=True, text=True, timeout=180, env=env)
    assert p.returncode == 0, p.stderr[-2000:]
    res = json.loads(p.stdout.strip().splitlines()[-1])
    assert len(res) == 12
    for r in res:
        assert r["errors"] == [] and r["external"] == [], r
        assert r["sw"] <= r["cw"], f"horizontal scroll at {r['width']}px on {r['view']}: {r['sw']} > {r['cw']}"
        assert r["legend"] == 8 and r["text"] > 80 and r["current"].lower() == r["view"] and r["h"].lower() == r["view"]
    assert len(list(shots.glob(f"{tag}-*.png"))) == 12
    return res


def test_render_smoke_with_data_six_views_two_widths_no_console_error_no_horizontal_scroll(server, tmp_path):
    _render(server, tmp_path, "data")


def test_render_smoke_with_no_data_shows_empty_states(tmp_path):
    cfg = CC._Config(None, None, str(tmp_path / "none"), False, False, clock=lambda: NOW, ttl_ms=10 ** 12, env={})
    srv = CC.make_server(cfg, "127.0.0.1", 0)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        _render(srv, tmp_path, "empty")
    finally:
        srv.shutdown()
        srv.server_close()


def test_the_pnl_card_states_one_net_the_total_with_realised_and_unrealised_beside_it(tmp_path):
    files = good_set(**{"ledger_pnl.json": art("ledger_pnl/v1", "SIMULATED", period_start_ms=None, period_end_ms=None, realised_gross_usd=18.5, fees_usd=7.0, funding_usd=2.0,
                                                realised_net_usd=11.5, unrealised_usd=1.25, net_usd=12.75)})
    st = build(tmp_path, files)
    pnl = st["portfolio"]["pnl"]
    assert pnl["total_net"]["v"] == 12.75 and pnl["total_net"]["basis"] == "SIMULATED"
    assert pnl["simulated_net"]["v"] + pnl["unrealised"]["v"] == pnl["total_net"]["v"]
    text = next(c for c in st["overview"]["cards"] if c["id"] == "pnl")["text"]
    assert "$12.75" in text and "realised $11.50" in text and "unrealised $1.25" in text and "not realised cash" in text
    files = good_set(**{"ledger_pnl.json": art("ledger_pnl/v1", "SIMULATED", period_start_ms=None, period_end_ms=None, realised_gross_usd=18.5, fees_usd=7.0, funding_usd=2.0,
                                                realised_net_usd=11.5, unrealised_usd=1.25)})                      # no net_usd: the total is NOT derived here
    st = build(tmp_path, files)
    assert st["portfolio"]["pnl"]["total_net"]["v"] is None and st["portfolio"]["pnl"]["total_net"]["basis"] == "MISSING"
    assert "not stated (MISSING)" in next(c for c in st["overview"]["cards"] if c["id"] == "pnl")["text"]
