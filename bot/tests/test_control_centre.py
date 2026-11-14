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
IDENT = {"run_id": "run_0123456789abcdef0123", "mode": "REPLAY", "candidate_id": "carry_eth", "spec_digest": "a" * 64, "data_digest": "b" * 64, "code_revision": "c0ffee1", "generated_ms": NOW - 60_000}


def art(schema, basis, **kw):
    """A schema-true artifact. It carries the run identity by default; ``identity=None`` makes it a legacy file, ``identity={...}`` overrides it."""
    ident = kw.pop("identity", IDENT)
    d = {"schema": schema, "generated_ms": NOW - 60_000, **kw}
    if basis is not None:
        d["basis"] = basis
    if ident is not None:
        d["identity"] = dict(ident)
    return d


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
        "ledger_pnl.json": art("ledger_pnl/v1", "SIMULATED", period_start_ms=None, period_end_ms=None, realised_gross_usd=15.25, fees_usd=7.0, funding_usd=2.0, realised_net_usd=10.25, unrealised_usd=1.25),
        "competition.json": art("competition/v1", "ESTIMATED", candidates=[
            {"candidate_id": "carry_eth", "kind": "SPOT_PERP_CARRY", "spot_hedge": "HEDGEABLE_ON_BYBIT", "status": "SUFFICIENT_EVIDENCE", "reason": None, "gross_bps_horizon": 30.0, "net_bps_horizon": 12.0, "n_observations": 40, "prov": "MODELLED"},
            {"candidate_id": "dir_pepe", "kind": "PERP_DIRECTIONAL_FUNDING", "spot_hedge": "PERP_ONLY", "status": "REJECTED", "reason": "net negative after costs in every fold", "gross_bps_horizon": 9.0, "net_bps_horizon": -2.0, "n_observations": 40, "prov": "MEASURED"}]),
        "opportunities.json": art("opportunities/v1", "ESTIMATED", decision_ms=NOW - 120_000, est_net_usd_per_day=None, items=[
            {"candidate_id": "carry_eth", "kind": "SPOT_PERP_CARRY", "symbols": ["ETHUSDT"], "spot_hedge": "HEDGEABLE_ON_BYBIT", "hold_days": 5.0, "gross_bps_horizon": 40.0, "net_bps_horizon": 15.0,
             "capacity_usd": 50_000.0, "prov": "MODELLED", "exclusions": []},
            {"candidate_id": "dir_pepe", "kind": "PERP_DIRECTIONAL_FUNDING", "symbols": ["PEPEUSDT"], "spot_hedge": "PERP_ONLY", "hold_days": 3.0, "gross_bps_horizon": 25.0, "net_bps_horizon": None,
             "capacity_usd": None, "prov": "ASSUMED", "exclusions": []}]),
        "pipeline_summary.json": art("pipeline_summary/v1", None, outcome="SIMULATED_EXECUTION", reason=None, required_span={"days": 457, "holding_rows": 60}, data_digest_parts={"funding": "f" * 16, "mark": "m" * 16},
                                     evidence_basis={"replay_report.json": "SIMULATED"}),
        "reconcile_report.json": art("reconcile_report/v1", None, findings=[], halt_new_exposure=False, views_compared=["ledger", "portfolio"]),
        "universe_scope.json": art("universe_scope/v1", None, scope_verdict="SURVIVORS_ONLY", eligible_perpetual_lifecycle_universe={"count": 120}, lifecycle_class_counts={"TRADING": 118, "DELISTED": 2}),
        "funnel.json": art("funnel/v1", None, steps=[{"name": "universe rows", "count": 600}, {"name": "eligible", "count": 120}], exclusion_reasons={"prelaunch_no_history": 12}),
        "testnet_session.json": art("testnet_session/v1", "NOT_RUN", identity={**IDENT, "mode": "TESTNET", "run_id": "run_testnet_0001"}, venue="Testnet", endpoint_host="api-testnet.bybit.com",
                                    credential_state={"dedicated_key_present": True, "dedicated_secret_present": True, "generic_credentials_ignored": True},
                                    preflight={"status": "NOT_RUN", "checked_ms": None, "checks": []}, plan=None, confirmation={"state": "NOT_REQUESTED", "plan_digest_confirmed": None},
                                    orders=[], executions=[], final_position=None, flat_verified=None, net={"fees_usd": None, "funding_usd": None, "price_pnl_usd": None, "note": None}),
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
    os.makedirs(tmp_path, exist_ok=True)
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
    assert pnl["simulated_net"]["v"] == 10.25 and pnl["simulated_net"]["basis"] == "SIMULATED"
    assert pnl["realised_net"]["v"] is None and pnl["realised_gross"]["v"] is None and pnl["realised_net"]["basis"] == "MISSING"
    files = good_set(**{"ledger_pnl.json": art("ledger_pnl/v1", "EXCHANGE_REPORTED", period_start_ms=None, period_end_ms=None, realised_gross_usd=20.0, fees_usd=5.0, funding_usd=1.0, realised_net_usd=16.0, unrealised_usd=None)})
    p2 = build(tmp_path, files)["portfolio"]["pnl"]
    assert p2["kind"] == "ACCOUNT_REPORTED_UNRECONCILED" and p2["realised_net"]["basis"] == "EXCHANGE_REPORTED" and p2["simulated_net"]["v"] is None and p2["unrealised"]["basis"] == "MISSING"
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
    "basis_not_allowed": lambda d, n: (d / n).write_text(json.dumps({**good_set()[n], "basis": "MEASURED" if n == "replay_report.json" else "NONSENSE"} if CC.ARTIFACTS[n][1] is not None
                                                                    else {**good_set()[n], "identity": {**IDENT, "mode": "MAINNET"}})),     # a file with no basis field: an identity mode outside the vocabulary
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
    flags = {f["name"]: f["presence"] for f in s["overview"]["environment"]["flags"]}
    assert flags["BYBIT_API_KEY"] == "set" and flags["BYBIT_API_SECRET"] == "set" and flags["LIVE_TRADING_ACK"] == "unset"


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
    page.on('dialog', d => { dialogs++; d.dismiss(); });
    const errors = [], external = [];
    let dialogs = 0;
    page.on('console', m => { if (m.type() === 'error') errors.push(m.text()); });
    page.on('pageerror', e => errors.push(String(e)));
    page.on('request', r => { if (!r.url().startsWith(base)) external.push(r.url()); });
    for (const view of ['overview', 'research', 'portfolio', 'execution', 'accounting', 'risk', 'testnet', 'evidence']) {
      await page.goto(base + '/#/' + view, { waitUntil: 'networkidle' });
      await page.waitForSelector('#view-h');
      const m = await page.evaluate(() => ({ sw: document.documentElement.scrollWidth, cw: document.documentElement.clientWidth, h: document.getElementById('view-h').textContent,
        legend: document.querySelectorAll('#legend .badge').length, text: document.getElementById('view').innerText.length, current: document.querySelector('.rail a[aria-current]').getAttribute('data-view'), xss: window.__xss === undefined ? null : window.__xss, imgs: document.querySelectorAll('#view img, #view script, #view iframe').length,
        hostile: document.getElementById('view').innerText.includes('onerror=window.__xss=1') }));
      await page.screenshot({ path: outDir + '/' + process.argv[5] + '-' + view + '-' + width + '.png', fullPage: true });
      results.push({ width, view, ...m, alerts: dialogs, errors: errors.slice(), external: external.slice() });
    }
    await ctx.close();
  }
  await browser.close();
  console.log(JSON.stringify(results));
})().catch(e => { console.error(e); process.exit(1); });
"""
CHROME = "/opt/pw-browsers/chromium-1243/chrome-linux64/chrome"
NODE_MODULES = "/opt/node22/lib/node_modules"


def _render(srv, tmp_path, tag, shots_dir=None):
    node = shutil.which("node")
    if not node or not os.path.exists(CHROME) or not os.path.isdir(os.path.join(NODE_MODULES, "playwright")):
        pytest.skip("render smoke needs node + node playwright + chromium at /opt/pw-browsers (python playwright.sync_api is not installed); not available here")
    script = tmp_path / "render_check.js"
    script.write_text(NODE_CHECK)
    shots = tmp_path / "shots" if shots_dir is None else shots_dir
    shots.mkdir(exist_ok=True)
    env = {"PATH": os.environ.get("PATH", ""), "NODE_PATH": NODE_MODULES, "HOME": str(tmp_path), "NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost"}
    p = subprocess.run([node, str(script), f"http://127.0.0.1:{srv.server_address[1]}", str(shots), CHROME, tag], capture_output=True, text=True, timeout=180, env=env)
    assert p.returncode == 0, p.stderr[-2000:]
    res = json.loads(p.stdout.strip().splitlines()[-1])
    assert len(res) == 16
    for r in res:
        assert r["errors"] == [] and r["external"] == [], r
        assert r["sw"] <= r["cw"], f"horizontal scroll at {r['width']}px on {r['view']}: {r['sw']} > {r['cw']}"
        assert r["legend"] == 8 and r["text"] > 80 and r["current"] == r["view"] and r["h"].lower() == r["view"]
        assert r["xss"] is None and r["imgs"] == 0 and r["alerts"] == 0, r
    assert len(list(shots.glob(f"{tag}-*.png"))) == 16
    return res


def test_render_smoke_with_data_eight_views_two_widths_no_console_error_no_horizontal_scroll(server, tmp_path):
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
    st = build(tmp_path, good_set())
    pnl = st["portfolio"]["pnl"]
    assert pnl["total_net"]["v"] == 11.5 and pnl["total_net"]["basis"] == "SIMULATED" and pnl["total_net"]["src"] == "replay_report.json:net_pnl_usd"
    assert round(pnl["simulated_net"]["v"] + pnl["unrealised"]["v"], 2) == pnl["total_net"]["v"]
    assert pnl["total_net"] == st["execution"]["net"] == st["figures"]["net"]                    # the same value in every view
    text = next(c for c in st["overview"]["cards"] if c["id"] == "pnl")["text"]
    assert "$11.50" in text and "realised $10.25" in text and "unrealised $1.25" in text and "not realised cash" in text
    files = good_set()
    del files["replay_report.json"]                                                              # no replay: the total is only what the ledger states (net_usd), never derived
    st = build(tmp_path / "b", files)
    assert st["portfolio"]["pnl"]["total_net"]["v"] is None and st["portfolio"]["pnl"]["total_net"]["basis"] == "MISSING"
    assert "not stated (MISSING)" in next(c for c in st["overview"]["cards"] if c["id"] == "pnl")["text"]
    files["ledger_pnl.json"]["net_usd"] = 11.5
    st = build(tmp_path / "c", files)
    assert st["portfolio"]["pnl"]["total_net"]["v"] == 11.5 and st["figures"]["net"]["src"] == "ledger_pnl.json:net_usd"


# ====================================================================== ROUND 9: one coherent state, run identity, the eight views, testnet, honesty
import contextlib      # noqa: E402
import copy           # noqa: E402

MONEY_KEYS = ("net", "gross", "fees", "funding", "realised_net", "realised_gross", "unrealised", "simulated_net", "simulated_gross", "simulated_fees", "simulated_funding",
              "realised_fees", "realised_funding", "total_net", "slippage", "hedge_cost", "adverse_selection")
PER_ITEM = ("symbols[", "positions[", "candidates[", "items[", ".testnet", "limits[", ".exposure", "competition", "opportunities")     # per-symbol/per-position/per-candidate or a different universe


def single_source_violations(state):
    """Walk the whole state. Every displayed net/gross/realised/unrealised/fees/funding/slippage figure must be one of the ``figures`` (it carries ``concept`` and ``src``) and every
    copy of a concept must be identical (same value, basis and source field)."""
    by, bad = {}, []
    for path, o in values(state):
        key = path.rsplit(".", 1)[-1].split("[")[0]
        if "concept" in o:
            by.setdefault(o["concept"], []).append((path, o))
        elif key in MONEY_KEYS and not any(c in path for c in PER_ITEM) and not (o["v"] is None and o["basis"] == "MISSING"):     # an empty slot cannot compete
            bad.append(("NO_CONCEPT", path))
    for concept, rows in by.items():
        if len({json.dumps(o, sort_keys=True) for _p, o in rows}) > 1:
            bad.append(("COMPETING", concept))
    return bad, by


def test_every_displayed_money_figure_is_read_from_one_source_field(full_state):
    bad, by = single_source_violations(full_state)
    assert bad == []                                                                                    # positive control
    assert set(by) >= {"net", "gross", "fees", "funding", "closed_net", "closed_gross", "unrealised", "slippage", "hedge_cost", "adverse_selection"}
    net_paths = {p for p, _ in by["net"]}
    assert {".figures.net", ".execution.net", ".portfolio.pnl.total_net"} <= net_paths and any(".accounting.components" in p for p in net_paths)      # the same figure in four places
    srcs = {c: {o["src"] for _p, o in rows} for c, rows in by.items()}
    assert srcs["net"] == {"replay_report.json:net_pnl_usd"} and srcs["gross"] == {"replay_report.json:gross_pnl_usd"} and srcs["fees"] == {"replay_report.json:fees_usd"}
    assert srcs["unrealised"] == {"ledger_pnl.json:unrealised_usd"} and srcs["closed_net"] == {"ledger_pnl.json:realised_net_usd"}
    for c in ("net", "gross"):                                                                          # gross and net are never conflated: the source field says which it is
        assert all(c in src.split(":")[1] for src in srcs[c])
    F = full_state["figures"]
    assert round(F["gross"]["v"] - F["fees"]["v"], 2) == F["net"]["v"]                                  # gross - fees = net
    assert round(F["closed_net"]["v"] + F["unrealised"]["v"], 2) == F["net"]["v"]                     # realised + unrealised = the one net
    assert round(F["closed_gross"]["v"] + F["funding"]["v"] - F["fees"]["v"], 2) == F["closed_net"]["v"]


def test_a_second_competing_figure_is_detected_mutation_check(full_state):
    st = copy.deepcopy(full_state)
    st["execution"]["net"]["v"] = 99.0                                                                  # a card that computed its own net
    assert ("COMPETING", "net") in single_source_violations(st)[0]
    st = copy.deepcopy(full_state)
    st["portfolio"]["pnl"]["unrealised"]["src"] = "portfolio_state.json:unrealised_pnl_usd"           # same number, another source field
    assert ("COMPETING", "unrealised") in single_source_violations(st)[0]
    st = copy.deepcopy(full_state)
    st["execution"]["fees"] = CC.val(7.0, "SIMULATED", "USD")                                           # a figure that does not say where it came from
    assert ("NO_CONCEPT", ".execution.fees") in single_source_violations(st)[0]


def test_the_accounting_identities_are_checked_shown_and_a_failure_is_loud(tmp_path):
    ok = build(tmp_path / "ok", good_set())["accounting"]
    assert ok["integrity"] == "PASS" and all(c["state"] == "PASS" for c in ok["checks"]) and {c["id"] for c in ok["checks"]} == {
        "gross_minus_fees", "symbols_sum", "ledger_components", "realised_plus_unrealised", "fees_match", "unrealised_matches_portfolio", "positions_sum"}
    for name, mutate, failing in (
            ("replay_report.json", lambda o: o.update(net_pnl_usd=12.5), "gross_minus_fees"),
            ("replay_report.json", lambda o: o["symbols"][0].update(net_pnl_usd=9.5), "symbols_sum"),
            ("ledger_pnl.json", lambda o: o.update(realised_net_usd=9.0), "ledger_components"),
            ("ledger_pnl.json", lambda o: o.update(unrealised_usd=2.0), "realised_plus_unrealised"),
            ("ledger_pnl.json", lambda o: o.update(fees_usd=8.0), "fees_match"),
            ("portfolio_state.json", lambda o: o.update(unrealised_pnl_usd=3.0), "unrealised_matches_portfolio")):
        files = good_set()
        mutate(files[name])
        st = build(tmp_path / (name + failing), files)
        assert st["accounting"]["integrity"] == "FAIL" and {c["id"]: c["state"] for c in st["accounting"]["checks"]}[failing] == "FAIL", failing
        assert any(a["source"] == "accounting" and a["severity"] == "FAIL" for a in st["overview"]["attention"]) and {c["id"]: c for c in st["overview"]["cards"]}["pnl"]["status"] == "FAIL"
        assert st["accounting"]["complete"] is False
    files = good_set()
    files["ledger_pnl.json"].update(unrealised_usd=None)                                                # an unknown input is NOT_CHECKABLE, never PASS
    st = build(tmp_path / "unk", files)["accounting"]
    assert {c["id"]: c["state"] for c in st["checks"]}["realised_plus_unrealised"] == "NOT_CHECKABLE"


def test_the_components_are_displayed_and_the_ones_no_backend_provides_are_missing(full_state):
    comp = {c["key"]: c for c in full_state["accounting"]["components"]}
    assert comp["hedge_cost"]["value"]["v"] is None and comp["hedge_cost"]["value"]["basis"] == "MISSING" and "no backend" in comp["hedge_cost"]["value"]["note"]
    for k in ("slippage", "adverse_selection"):                                                # no artifact states them in THIS set: missing, never zero
        assert comp[k]["value"]["v"] is None and comp[k]["value"]["basis"] == "MISSING"
    assert comp["funding"]["value"]["v"] == 2.0 and comp["fees"]["value"]["v"] == 7.0
    assert full_state["accounting"]["complete"] is False and full_state["accounting"]["why_incomplete"]


# ---------------------------------------------------------------- run identity
def ident_set(**per_file):
    files = good_set()
    for name, change in per_file.items():
        files[name.replace("__", ".")]["identity"] = {**IDENT, **change}
    return files


def test_matching_identities_are_consistent_and_carry_through(full_state):
    c = full_state["coherence"]
    assert c["verdict"] == "CONSISTENT" and c["mismatches"] == [] and c["primary"]["run_id"] == IDENT["run_id"] and c["code_revisions"] == ["c0ffee1", "c0ffee1"][:1] + [] or c["code_revisions"]
    assert c["combined_numbers_withheld"] is False and full_state["figures"]["net"]["v"] == 11.5
    assert {r["file"] for r in c["runs"]} == set(CC.ARTIFACTS)


@pytest.mark.parametrize("field,other", [("run_id", "run_other"), ("candidate_id", "dir_pepe"), ("data_digest", "d" * 64), ("mode", "PAPER")])
@pytest.mark.parametrize("victim", ["ledger_pnl.json", "portfolio_state.json", "risk_state.json"])
def test_a_run_candidate_digest_or_mode_mismatch_is_a_loud_inconsistent_run_and_no_figure_is_combined(tmp_path, field, other, victim):
    files = good_set()
    files[victim]["identity"] = {**IDENT, field: other}
    st = build(tmp_path, files)
    c = st["coherence"]
    assert c["verdict"] == "INCONSISTENT_RUN" and c["combined_numbers_withheld"] is True and any(m["field"] == field for m in c["mismatches"])
    assert st["overview"]["headline"]["status"] == "FAIL" and "INCONSISTENT_RUN" in st["overview"]["headline"]["text"]
    assert any(a["source"] == "coherence" and a["severity"] == "FAIL" for a in st["overview"]["attention"])
    for k, f in st["figures"].items():                                                                  # nothing that needs two files is combined
        assert f["v"] is None and f["basis"] == "MISSING" and "withheld" in f["note"], k
    assert st["accounting"]["withheld"] is True and st["accounting"]["integrity"] == "WITHHELD" and all(x["state"] == "NOT_CHECKABLE" for x in st["accounting"]["checks"])
    assert st["execution"]["net"]["v"] is None and st["portfolio"]["pnl"]["total_net"]["v"] is None and st["portfolio"]["unrealised"]["v"] is None
    cards = {x["id"]: x for x in st["overview"]["cards"]}
    assert "Withheld" in cards["pnl"]["text"] and "Withheld" in cards["exposure"]["text"] and "$" not in cards["pnl"]["text"]
    answers = " ".join(a["a"] for a in st["overview"]["answers"])
    assert "INCONSISTENT_RUN" in answers and "$11.50" not in answers and "$400" not in answers
    assert st["overview"]["next_steps"][0].startswith("Regenerate the artifacts from ONE run")
    assert single_source_violations(st)[0] == []


def test_identity_mismatch_positive_control_one_run_shows_the_figures(tmp_path):
    st = build(tmp_path, good_set())                                                                    # the same fixture without the mutation
    assert st["coherence"]["verdict"] == "CONSISTENT" and st["figures"]["net"]["v"] == 11.5 and st["accounting"]["withheld"] is False


def test_legacy_files_without_identity_are_unverified_not_inconsistent_and_not_hidden(tmp_path):
    files = good_set(**{n: art(CC.ARTIFACTS[n][0], good_set()[n].get("basis"), identity=None, **{k: v for k, v in good_set()[n].items() if k not in ("schema", "basis", "identity", "generated_ms")})
                        for n in ("replay_report.json", "ledger_pnl.json")})
    st = build(tmp_path, files)
    c = st["coherence"]
    assert c["verdict"] == "UNVERIFIED" and set(c["legacy_files"]) == {"replay_report.json", "ledger_pnl.json"} and c["mismatches"] == []
    assert st["figures"]["net"]["v"] == 11.5 and any(a["source"] == "coherence" and "UNVERIFIED" in a["text"] for a in st["overview"]["attention"])
    src = {x["file"]: x for x in st["evidence"]["sources"]}
    assert src["replay_report.json"]["identity_status"] == "LEGACY_NO_IDENTITY" and src["risk_state.json"]["identity_status"] == "IDENTIFIED"
    assert st["accounting"]["venue_reconciliation"]["status"] == "INTERNAL_CONSISTENCY_ONLY"


def test_unknown_identity_fields_are_unverified_and_never_matches(tmp_path):
    files = good_set()
    files["ledger_pnl.json"]["identity"] = {**IDENT, "data_digest": "UNKNOWN"}
    c = build(tmp_path, files)["coherence"]
    assert c["verdict"] == "UNVERIFIED" and "ledger_pnl.json:data_digest" in c["unknown_fields"]


def test_a_malformed_identity_refuses_the_file(tmp_path):
    for bad in ({"run_id": "x"}, "not an object", {**IDENT, "mode": "MAINNET"}, {**IDENT, "generated_ms": -1}, None):
        files = good_set()
        files["ledger_pnl.json"]["identity"] = bad
        assert CC.load_artifact(write_set(tmp_path / str(abs(hash(str(bad)))), files), "ledger_pnl.json", NOW)["state"] == "ERROR", bad


def test_the_testnet_family_is_separate_but_its_mode_must_be_testnet(tmp_path):
    st = build(tmp_path / "a", good_set())
    assert st["coherence"]["verdict"] == "CONSISTENT"                                                   # a different run_id and mode TESTNET do not conflict with the simulated family
    files = good_set()
    files["testnet_session.json"]["identity"] = {**IDENT, "mode": "REPLAY"}
    assert build(tmp_path / "b", files)["coherence"]["verdict"] == "INCONSISTENT_RUN"
    files = good_set()
    files["replay_report.json"]["identity"] = {**IDENT, "mode": "TESTNET"}
    assert build(tmp_path / "c", files)["coherence"]["verdict"] == "INCONSISTENT_RUN"


# ---------------------------------------------------------------- testnet
def tn(**over):
    return {**good_set()["testnet_session.json"], **over}


def tn_executed(**over):
    plan = {"digest": "p" * 16, "manifest_digest": "m" * 16, "notional_usd": 70.0,
            "legs": [{"symbol": "BTCUSDT", "side": "Sell", "order_type": "Market", "qty": 0.001, "price": None, "notional_usd": 70.0}]}
    base = dict(basis="EXCHANGE_REPORTED", preflight={"status": "PASS", "checked_ms": NOW - 5000, "checks": [{"name": "read_only_auth", "ok": True, "detail": "ok"}, {"name": "instrument", "ok": True, "detail": "Trading"}]},
                plan=plan, confirmation={"state": "CONFIRMED", "plan_digest_confirmed": "p" * 16},
                orders=[{"link_id": "g1-leg1", "order_id": "o1", "symbol": "BTCUSDT", "side": "Sell", "qty": 0.001, "status": "Filled", "filled_qty": 0.001, "avg_price": 70000.0},
                        {"link_id": "g1-leg2", "order_id": None, "symbol": "BTCUSDT", "side": "Buy", "qty": 0.001, "status": "Rejected", "filled_qty": 0.0, "avg_price": None},
                        {"link_id": "g1-leg3", "order_id": "o3", "symbol": "BTCUSDT", "side": "Buy", "qty": 0.001, "status": "PartiallyFilled", "filled_qty": 0.0004, "avg_price": 70010.0},
                        {"link_id": "g1-leg4", "order_id": "o4", "symbol": "BTCUSDT", "side": "Buy", "qty": 0.001, "status": "Cancelled", "filled_qty": 0.0, "avg_price": None}],
                executions=[{"exec_id": "e1", "link_id": "g1-leg1", "symbol": "BTCUSDT", "side": "Sell", "qty": 0.001, "price": 70000.0, "fee_usd": 0.04, "ts_ms": NOW - 4000}],
                final_position={"BTCUSDT": 0.0}, flat_verified=True, net={"fees_usd": 0.04, "funding_usd": None, "price_pnl_usd": -0.1, "note": "testnet books are thin"})
    base.update(over)
    return tn(**base) | {"basis": base["basis"]}


def tnv(tmp_path, session, name="t"):
    return build(tmp_path / name, good_set(**{"testnet_session.json": session}))["testnet"]


def test_testnet_not_run_is_an_explicit_empty_state_and_never_green(tmp_path, empty_state):
    for st in (empty_state, build(tmp_path, good_set())):                                               # no file at all, and a file that says NOT_RUN
        t = st["testnet"]
        assert t["session_state"] == "NOT_RUN" and t["pill"] == "NO_DATA" and "not owner profit" in t["banner"]
        card = next(c for c in st["overview"]["cards"] if c["id"] == "testnet")
        assert card["status"] == "NO_DATA" and card["status"] != "OK" and "NOT_RUN" in card["text"]
        assert any("testnet preflight" in step for step in st["overview"]["next_steps"])
    assert "NOT_RUN" in empty_state["testnet"]["empty"] and "not a pass" in empty_state["testnet"]["empty"]
    t = build(tmp_path / "f", good_set())["testnet"]
    assert t["state"] == "OK" and t["preflight"]["status"] == "NOT_RUN" and t["plan"] is None and t["orders"] == [] and t["final_position"] is None and t["flat"]["verified_flat"] is False
    assert t["fees"]["basis"] == "MISSING"


def test_testnet_credentials_are_booleans_only_and_the_endpoint_must_be_the_registered_testnet_host(tmp_path):
    t = tnv(tmp_path, tn(credential_state={"dedicated_key_present": True, "dedicated_secret_present": False, "generic_credentials_ignored": True}), "c")
    assert t["credentials"]["dedicated_key_present"]["v"] is True and t["credentials"]["dedicated_secret_present"]["v"] is False and "no value" in t["credentials"]["note"]
    assert t["endpoint_host"] == "api-testnet.bybit.com" and t["venue"]["v"] == "Testnet"
    for host in ("api.bybit.com", "api-demo.bybit.com", "api.bybit.eu", "evil.example.com", "API-TESTNET.bybit.com.evil.com"):
        bad = build(tmp_path / ("h" + host), good_set(**{"testnet_session.json": tn(endpoint_host=host)}))["testnet"]
        assert bad["state"] == "ERROR" and "not the registered testnet host" in bad["error"], host
    assert tnv(tmp_path, tn(endpoint_host="API-TESTNET.BYBIT.COM"), "up")["state"] == "OK"
    assert CC.load_artifact(write_set(tmp_path / "nv", good_set(**{"testnet_session.json": tn(venue="Mainnet")})), "testnet_session.json", NOW)["state"] == "ERROR"
    for non_bool in ("yes", 1, None):
        assert CC.load_artifact(write_set(tmp_path / f"nb{non_bool}", good_set(**{"testnet_session.json": tn(credential_state={"dedicated_key_present": non_bool, "dedicated_secret_present": True, "generic_credentials_ignored": True})})),
                                "testnet_session.json", NOW)["state"] == "ERROR"


def test_testnet_not_run_cannot_carry_orders_a_position_or_a_confirmation(tmp_path):
    """A preflight result is NOT in this list any more: the session tool records one (read-only, no session) under basis NOT_RUN, and that is a valid file."""
    for over in ({"orders": tn_executed()["orders"]}, {"final_position": {"BTCUSDT": 0.0}}, {"confirmation": {"state": "CONFIRMED", "plan_digest_confirmed": "x"}, "plan": tn_executed()["plan"]}):
        assert CC.load_artifact(write_set(tmp_path / str(abs(hash(str(over)))), good_set(**{"testnet_session.json": tn(**over)})), "testnet_session.json", NOW)["state"] == "ERROR", over


def test_an_executed_testnet_session_is_shown_in_full_but_never_as_green_or_owner_profit(tmp_path):
    st = build(tmp_path, good_set(**{"testnet_session.json": tn_executed()}))
    t = st["testnet"]
    assert t["session_state"] == "EXECUTED_FLAT_VERIFIED" and t["pill"] == "INFO" and t["flags"] == [] and t["run_basis"] == "EXCHANGE_REPORTED"
    assert t["counts"] == {"orders": 4, "acked": 3, "filled": 1, "partial": 1, "cancelled": 1, "rejected": 1, "open": 0, "executions": 1}
    assert t["plan"]["digest"]["v"] == "p" * 16 and t["confirmation"]["matches_plan"] is True and t["flat"]["verified_flat"] is True
    assert t["orders"][0]["filled_qty"]["basis"] == "EXCHANGE_REPORTED" and t["executions"][0]["fee"]["v"] == 0.04 and t["final_position"]["BTCUSDT"]["v"] == 0.0
    assert next(c for c in st["overview"]["cards"] if c["id"] == "testnet")["status"] == "INFO"
    assert st["accounting"]["testnet"]["fees"]["v"] == 0.04 and st["accounting"]["testnet"]["price_pnl"]["v"] == -0.1
    base = build(tmp_path / "base", good_set())
    assert st["figures"] == base["figures"] and st["accounting"]["simulated"] == base["accounting"]["simulated"]      # testnet never moves a simulated number
    assert st["accounting"]["mainnet_cash"]["status"] == "NOT_AVAILABLE" and st["accounting"]["mainnet_cash"]["realised"]["v"] is None
    assert "not owner profit" in t["banner"]
    assert single_source_violations(st)[0] == []


@pytest.mark.parametrize("mutate,flag,pill", [
    (lambda d: d.update(confirmation={"state": "NOT_REQUESTED", "plan_digest_confirmed": None}), "ORDERS_WITHOUT_CONFIRMATION", "FAIL"),
    (lambda d: d["confirmation"].update(plan_digest_confirmed="z" * 16), "CONFIRMATION_DIGEST_MISMATCH", "FAIL"),
    (lambda d: d["preflight"]["checks"].append({"name": "x", "ok": False, "detail": "bad"}), "PREFLIGHT_PASS_CONTRADICTS_CHECKS", "FAIL"),
    (lambda d: d.update(final_position=None), "FLAT_CLAIM_NOT_SUPPORTED", "FAIL"),
    (lambda d: d.update(final_position={"BTCUSDT": 0.001}), "FLAT_CLAIM_NOT_SUPPORTED", "FAIL"),
    (lambda d: d["net"].update(fees_usd=5.0), "FEES_DO_NOT_MATCH_EXECUTIONS", "FAIL"),
])
def test_testnet_contradictions_are_flags_and_never_shown_as_flat_or_clean(tmp_path, mutate, flag, pill):
    sess = tn_executed()
    mutate(sess)
    st = build(tmp_path, good_set(**{"testnet_session.json": sess}))
    t = st["testnet"]
    assert any(f.startswith(flag) for f in t["flags"]) and t["pill"] == pill
    assert any(a["source"] == "testnet" and a["text"].startswith(flag) for a in st["overview"]["attention"])
    if flag.startswith("FLAT"):
        assert t["flat"]["verified_flat"] is False and t["session_state"] != "EXECUTED_FLAT_VERIFIED"


def test_a_failed_testnet_preflight_is_a_failure_and_an_unverified_flat_is_not_flat(tmp_path):
    t = tnv(tmp_path, tn_executed(preflight={"status": "FAIL", "checked_ms": NOW, "checks": [{"name": "read_only_auth", "ok": False, "detail": "401"}]}), "pf")
    assert t["session_state"] == "PREFLIGHT_FAILED" and t["pill"] == "FAIL"
    t = tnv(tmp_path, tn_executed(flat_verified=None), "fv")
    assert t["session_state"] == "EXECUTED_FLAT_UNVERIFIED" and t["flat"]["verified_flat"] is False and t["pill"] == "ATTENTION"


# ---------------------------------------------------------------- pipeline, scope, funnel, reconcile, environment, answers
@pytest.mark.parametrize("code", CC.OUTCOME_CODES)
def test_every_pipeline_outcome_code_is_shown_with_its_meaning_and_a_next_step(tmp_path, code):
    files = good_set()
    files["pipeline_summary.json"]["outcome"] = code
    st = build(tmp_path, files)
    p = st["research"]["pipeline"]
    assert p["state"] == "OK" and p["outcome"] == code and p["meaning"] and p["next"] and p["required_span"] == {"days": 457, "holding_rows": 60}
    assert p["data_digest_parts"]["funding"] == "f" * 16 and p["evidence_basis"] == {"replay_report.json": "SIMULATED"}
    assert p["blocking"] is (code != "SIMULATED_EXECUTION")
    steps = st["overview"]["next_steps"]
    assert any(p["next"] in x for x in steps) is p["blocking"]                                           # the next step comes from the outcome
    assert (next(c for c in st["overview"]["cards"] if c["id"] == "pipeline")["status"] == "ATTENTION") is p["blocking"]


def test_an_unknown_pipeline_outcome_is_refused(tmp_path):
    for bad in ("PROFIT", "OK", None, 7):
        files = good_set()
        files["pipeline_summary.json"]["outcome"] = bad
        assert CC.load_artifact(write_set(tmp_path / str(bad), files), "pipeline_summary.json", NOW)["state"] == "ERROR", bad
    files = good_set()
    files["pipeline_summary.json"].pop("outcome")
    files["pipeline_summary.json"]["outcome_code"] = "NO_FILL"                                          # the alternative spelling
    assert CC.load_artifact(write_set(tmp_path / "alt", files), "pipeline_summary.json", NOW)["data"]["outcome"] == "NO_FILL"


def test_universe_scope_and_funnel_counts_are_shown_and_a_survivorship_free_claim_needs_corroboration(tmp_path, monkeypatch):
    st = build(tmp_path / "a", good_set())
    u, f = st["research"]["universe_scope"], st["research"]["funnel"]
    assert u["eligible"] == {"count": 120} and u["lifecycle_classes"] == {"TRADING": 118, "DELISTED": 2} and u["scope_verdict_shown"] == "SURVIVORS_ONLY"
    assert [x["name"] for x in f["steps"]] == ["universe rows", "eligible"] and f["exclusion_reasons"] == {"prelaunch_no_history": 12}
    files = good_set()
    files["universe_scope.json"]["scope_verdict"] = "SURVIVORSHIP_FREE"
    shown = build(tmp_path / "b", files)["research"]["universe_scope"]
    assert shown["corroborated_here"] is False and shown["scope_verdict_shown"].startswith("SURVIVORS_ONLY") and "SURVIVORSHIP_FREE" not in shown["scope_verdict_shown"]
    uni = tmp_path / "uni"
    uni.mkdir()
    monkeypatch.setattr(CC.UC, "verify_capture_dir", lambda p: {"ok": True, "problems": [], "coverage": {"dataset_status": "COMPLETE", "delisting_observability": "OBSERVED", "status_coverage": {"Closed": "OBSERVED_ROWS"}}})
    assert build(tmp_path / "c", files, universe=str(uni))["research"]["universe_scope"]["scope_verdict_shown"] == "SURVIVORSHIP_FREE"      # positive control: corroborated


def test_empty_research_extras_name_what_to_run(empty_state):
    for k, word in (("pipeline", "pipeline"), ("universe_scope", "universe scope"), ("funnel", "funnel")):
        sec = empty_state["research"][k]
        assert sec["state"] == "NO_DATA" and word in sec["empty"].lower() and "Run" in sec["empty"]


def test_reconcile_findings_become_incidents_attention_and_a_halt(tmp_path):
    files = good_set()
    files["reconcile_report.json"].update(findings=[{"code": "POSITION_MISMATCH", "detail": "venue 2 vs book 1", "severity": "CRITICAL"}, {"code": "NOTE", "detail": "fine", "severity": "INFO"}],
                                          halt_new_exposure=True, views_compared=["book", "venue_positions"])
    st = build(tmp_path, files)
    assert [i["code"] for i in st["overview"]["incidents"]] == ["POSITION_MISMATCH"]                    # INFO is not an incident
    assert any(a["source"] == "reconcile" and a["severity"] == "FAIL" and "POSITION_MISMATCH" in a["text"] for a in st["overview"]["attention"])
    assert st["overview"]["headline"]["status"] == "FAIL" and "reconcile" in st["overview"]["headline"]["text"]
    assert any(r["source"] == "reconcile_report.json" for r in st["overview"]["restrictions"]) and {x["source"]: x["halted"] for x in st["risk"]["halt_flags"]}["reconcile_report.json"] is True
    assert st["risk"]["recovery"]["state"] == "RECOVERY_REQUIRED" and "Resolve the reconciliation findings" in st["overview"]["next_steps"][0]
    assert st["execution"]["reconciliation"]["halt_new_exposure"] is True


def test_reconcile_is_green_only_with_a_compared_venue_view_and_no_findings(tmp_path):
    internal = build(tmp_path / "i", good_set())
    assert next(c for c in internal["overview"]["cards"] if c["id"] == "reconcile")["status"] == "INFO"       # clean but internal-only: not green
    files = good_set()
    files["reconcile_report.json"]["views_compared"] = ["ledger", "venue_positions"]
    assert next(c for c in build(tmp_path / "v", files)["overview"]["cards"] if c["id"] == "reconcile")["status"] == "OK"
    none = build(tmp_path, None)
    assert next(c for c in none["overview"]["cards"] if c["id"] == "reconcile")["status"] == "NO_DATA" and none["risk"]["recovery"]["state"] == "MISSING"


def test_venue_reconciled_needs_an_exchange_reported_ledger_a_venue_view_one_run_and_passing_identities(tmp_path):
    def venue_files(**kw):
        files = good_set()
        del files["replay_report.json"]
        files["ledger_pnl.json"].update(basis="EXCHANGE_REPORTED", net_usd=11.5)
        files["portfolio_state.json"]["basis"] = "EXCHANGE_REPORTED"
        files["reconcile_report.json"]["views_compared"] = ["ledger", "venue_positions"]
        for name, f in files.items():                                        # a venue-reconciled TESTNET run: every run-family artifact names the one TESTNET run
            if name != "testnet_session.json" and "identity" in f:
                f["identity"] = {**f["identity"], "mode": "TESTNET", "run_id": "run_testnet_0001"}
        for k, v in kw.items():
            files[k.replace("__", ".")].update(v)
        return files
    sim = build(tmp_path / "sim", good_set())["accounting"]["venue_reconciliation"]
    assert sim["status"] == "INTERNAL_CONSISTENCY_ONLY" and any("SIMULATED" in w for w in sim["why_not"])            # a simulation is never venue reconciled
    ok = build(tmp_path / "ok", venue_files())["accounting"]
    assert ok["venue_reconciliation"]["status"] == "VENUE_RECONCILED" and ok["venue_reconciliation"]["why_not"] == []        # positive control
    assert ok["mainnet_cash"]["status"] == "NOT_AVAILABLE" and ok["complete"] is False                                       # even then: not mainnet cash, gross is missing
    for name, kw in (("noview", {"reconcile_report__json": {"views_compared": ["ledger", "portfolio"]}}), ("halt", {"reconcile_report__json": {"halt_new_exposure": True}}),
                     ("finding", {"reconcile_report__json": {"findings": [{"code": "X", "detail": "d", "severity": "WARN"}]}}),
                     ("fees", {"ledger_pnl__json": {"fees_usd": 9.0}}), ("owner", {"ledger_pnl__json": {"basis": "OWNER_SUPPLIED_UNVERIFIED"}})):
        assert build(tmp_path / name, venue_files(**kw))["accounting"]["venue_reconciliation"]["status"] == "INTERNAL_CONSISTENCY_ONLY", name
    files = venue_files()
    del files["reconcile_report.json"]
    assert build(tmp_path / "norec", files)["accounting"]["venue_reconciliation"]["status"] == "INTERNAL_CONSISTENCY_ONLY"


def test_a_forged_venue_reconciliation_is_refused_when_the_artifacts_do_not_name_a_testnet_run(tmp_path):
    """Red team: a REPLAY / PAPER run (or a SimVenue 'venue' view) that merely DECLARES basis EXCHANGE_REPORTED and a venue view must never count as venue reconciled. Both the ledger and the
    reconcile report must carry identity.mode TESTNET."""
    def forged(ledger_mode, rec_mode, same_run=True):
        files = good_set()
        del files["replay_report.json"]
        files["ledger_pnl.json"].update(basis="EXCHANGE_REPORTED", net_usd=11.5)
        files["portfolio_state.json"]["basis"] = "EXCHANGE_REPORTED"
        files["reconcile_report.json"]["views_compared"] = ["ledger", "venue_positions"]
        for name, f in files.items():
            if name != "testnet_session.json" and "identity" in f:
                f["identity"] = {**f["identity"], "mode": "TESTNET", "run_id": "run_testnet_0001"}
        for name, mode in (("ledger_pnl.json", ledger_mode), ("reconcile_report.json", rec_mode)):
            if mode is None:
                files[name].pop("identity", None)
            else:
                files[name]["identity"] = {**files[name]["identity"], "mode": mode}
        return files
    ok = build(tmp_path / "ok", forged("TESTNET", "TESTNET"))["accounting"]["venue_reconciliation"]
    assert ok["status"] == "VENUE_RECONCILED", ok                                                                   # positive control
    for name, lm, rm in (("replay_ledger", "REPLAY", "TESTNET"), ("paper_rec", "TESTNET", "PAPER"), ("sim_both", "REPLAY", "REPLAY"), ("no_ident", None, None), ("no_rec_ident", "TESTNET", None)):
        v = build(tmp_path / name, forged(lm, rm))["accounting"]["venue_reconciliation"]
        assert v["status"] == "INTERNAL_CONSISTENCY_ONLY", (name, v)
        assert v["why_not"], name
    sim = good_set()                                                                                                 # a plain simulation that lies about its basis AND keeps its REPLAY identity
    del sim["replay_report.json"]
    sim["ledger_pnl.json"].update(basis="EXCHANGE_REPORTED")
    sim["reconcile_report.json"]["views_compared"] = ["ledger", "venue_positions"]
    v = build(tmp_path / "lie", sim)["accounting"]["venue_reconciliation"]
    assert v["status"] == "INTERNAL_CONSISTENCY_ONLY" and any("TESTNET run identity" in w for w in v["why_not"])


def test_environment_flags_are_presence_only_and_no_value_is_ever_in_the_state(tmp_path):
    names = ("LIVE_TRADING_ACK", "USE_TESTNET", "PAPER_TRADING", "BYBIT_API_KEY", "BYBIT_API_SECRET", "OWNER_ATTESTATION_KEY")
    vals = ("SUPER-" + "SECRET-ACK-VALUE", "0", "PAPER-VALUE-" + "zz", "KEY-VALUE-" + "1234", "SECRET-VALUE-" + "5678", "ATTEST-VALUE-" + "9")
    env = dict(zip(names, vals))
    st = build(tmp_path, None, env=env)
    text = json.dumps(st)
    for v in ("SUPER-SECRET-ACK-VALUE", "KEY-VALUE-1234", "SECRET-VALUE-5678", "ATTEST-VALUE-9", "PAPER-VALUE-zz"):
        assert v not in text, v
    flags = {f["name"]: f["presence"] for f in st["overview"]["environment"]["flags"]}
    assert flags == {n: "set" for n in ("LIVE_TRADING_ACK", "USE_TESTNET", "PAPER_TRADING", "BYBIT_API_KEY", "BYBIT_API_SECRET", "OWNER_ATTESTATION_KEY")}
    assert all(set(f) == {"name", "presence"} for f in st["overview"]["environment"]["flags"])
    assert "values" not in st["environment"] and "secrets" not in st["environment"]
    none = {f["name"]: f["presence"] for f in build(tmp_path / "n", None, env={})["overview"]["environment"]["flags"]}
    assert set(none.values()) == {"unset"}
    assert any("LIVE_TRADING_ACK" in a["text"] for a in st["overview"]["attention"])                      # the rule-breaking presence is a finding


def test_the_plain_language_answers_are_computed_from_the_state_not_constants(tmp_path, empty_state, full_state):
    qs = [a["q"] for a in full_state["overview"]["answers"]]
    assert qs == ["What is the engine doing?", "What capital is at risk?", "Are there positions?", "Is P&L complete?", "What has executed?", "What is blocked?", "What must happen next?"]
    ans = {a["q"]: a["a"] for a in full_state["overview"]["answers"]}
    assert "SIMULATED_EXECUTION" in ans["What is the engine doing?"] and "No real money" in ans["What capital is at risk?"] and "$400.00" in ans["What capital is at risk?"]
    assert "1 position(s)" in ans["Are there positions?"] and ans["Is P&L complete?"].startswith("No.") and "$11.50" in ans["Is P&L complete?"]
    assert "Replay: 5 order group(s)" in ans["What has executed?"] and "No mainnet order" in ans["What has executed?"]
    assert "Mainnet (real money)" in ans["What is blocked?"]
    e = {a["q"]: a["a"] for a in empty_state["overview"]["answers"]}
    assert "Unknown" in e["What capital is at risk?"] and "Unknown" in e["Are there positions?"] and "No pipeline_summary.json" in e["What is the engine doing?"]
    assert "Capture market data" in e["What must happen next?"]
    seen = set()
    for name, mutate in (("halt", lambda f: f["risk_state.json"].update(halted=True, halt_reasons=["kill switch"])),
                         ("hist", lambda f: f["pipeline_summary.json"].update(outcome="INADEQUATE_HISTORY")),
                         ("rec", lambda f: f["reconcile_report.json"].update(halt_new_exposure=True, findings=[{"code": "DRIFT", "detail": "d", "severity": "WARN"}])),
                         ("err", lambda f: f.update({"portfolio_state.json": "{broken"}))):
        files = good_set()
        mutate(files)
        nxt = build(tmp_path / name, files)["overview"]["next_steps"]
        assert nxt and nxt[-1].startswith("The promotion gate is REFUSE")                                  # the owner-only gate is always last, never 'all clear'
        seen.add(nxt[0])
    assert len(seen) == 4 and "halted" in sorted(seen)[0].lower() + " ".join(seen).lower()
    for st in (empty_state, full_state):
        assert all("all clear" not in x.lower() and x.strip() != "OK" for x in st["overview"]["next_steps"])


def test_market_data_health_overview_block_comes_from_the_freshness_audit(tmp_path, monkeypatch):
    report = {"axes": {"data_freshness": {"verdict": "STALE", "issues": [{"code": "OLD", "level": "STALE", "detail": "funding 9 days old"}]}, "research_readiness": {"verdict": "NOT_READY", "issues": []},
                       "code_correctness": {"verdict": "NOT_ASSESSED"}}, "cycles": {"latest_complete_cycle": {"cycle": "c1", "age_ms": 5000}}, "status_partitions": {"present": [], "missing": []},
              "backups": {}, "history": {}, "collector_health": {"status": "RUNNING", "consecutive_failures": 3, "interval_ms": 60000}}
    monkeypatch.setattr(CC.DFA, "audit", lambda *a, **k: report)
    st = build(tmp_path, good_set())
    md = st["overview"]["market_data"]
    assert md["verdict"] == "STALE" and md["collector"]["status"]["basis"] == "UNVERIFIED" and md["collector"]["consecutive_failures"]["v"] == 3
    assert any(a["source"] == "data" and a["severity"] == "FAIL" for a in st["overview"]["attention"]) and st["overview"]["headline"]["status"] == "FAIL"
    assert next(c for c in st["overview"]["cards"] if c["id"] == "data")["basis"] == "UNVERIFIED"


def test_portfolio_shows_only_existing_fields_and_marks_the_rest_missing(full_state):
    p = full_state["portfolio"]
    for k in ("available_balance", "reserved_capital", "margin", "drawdown"):
        assert p[k]["v"] is None and p[k]["basis"] == "MISSING" and "no such field" in p[k]["note"]
    assert p["leverage"]["v"] == 0.4 and p["leverage"]["basis"] == "CALCULATED" and p["concentration"]["v"] == 50.0 and "ETHUSDT" in p["concentration"]["note"]
    assert p["equity"]["v"] == 1000.0 and p["equity"]["basis"] == "SIMULATED"


def test_opportunities_are_ranked_by_known_net_and_an_unknown_net_is_unranked(full_state):
    items = full_state["research"]["opportunities"]["items"]
    assert [(i["candidate_id"], i["rank"]) for i in items] == [("carry_eth", 1), ("dir_pepe", None)]


def test_evidence_and_ops_carry_digests_revision_and_say_ci_is_not_available_offline(full_state, empty_state):
    ev = full_state["evidence"]
    assert ev["digests"]["run_data_digest"] == "b" * 64 and ev["digests"]["data_digest_parts"]["mark"] == "m" * 16 and ev["software"]["code_revisions"] == ["c0ffee1"]
    assert ev["ci"]["state"] == "NOT_AVAILABLE_OFFLINE" and "not available offline" in ev["ci"]["text"]
    assert empty_state["evidence"]["software"]["code_revisions"] == [] and empty_state["evidence"]["operations"] is None


def test_risk_extras_are_missing_when_no_backend_has_them(full_state):
    r = full_state["risk"]
    assert r["kill_switch"]["basis"] == "MISSING" and r["stress"]["basis"] == "MISSING" and r["exposure"]["gross"]["v"] == 400.0
    assert {h["source"] for h in r["halt_flags"]} == {"risk_state.json", "reconcile_report.json", "promotion gate"} and [h for h in r["halt_flags"] if h["source"] == "promotion gate"][0]["halted"] is True


# ---------------------------------------------------------------- honesty
def test_simulated_is_never_labelled_realised_and_testnet_is_never_owner_profit(full_state, tmp_path):
    for c in full_state["accounting"]["components"]:
        if c["value"]["basis"] == "SIMULATED":
            assert "realised" not in c["label"].lower().replace("unrealised", ""), c
    assert full_state["accounting"]["simulated"]["label"].endswith("never realised cash") and full_state["accounting"]["mainnet_cash"]["status"] == "NOT_AVAILABLE"
    for p, o in walk(full_state):                                                                       # a simulated value never sits in a realised_* slot
        if isinstance(o, dict) and o.get("basis") == "SIMULATED" and "concept" not in o:
            assert not re.search(r"(?:^|\.)realised_[a-z]+$", p), p
    st = build(tmp_path, good_set(**{"testnet_session.json": tn_executed()}))
    text = json.dumps(st)
    for m in re.finditer(r"(?i)owner profit", text):
        assert re.search(r"(?i)not\s+$", text[max(0, m.start() - 6):m.start()]) or re.search(r"(?i)never\s+$", text[max(0, m.start() - 8):m.start()]), text[max(0, m.start() - 40):m.end()]
    assert st["testnet"]["banner"] in json.dumps(st["testnet"]) and "TESTNET results are not owner profit" in st["testnet"]["banner"]
    assert "t.banner" in _bundle()["app.js"]                                                            # the page renders it on the Testnet view


def test_green_only_from_a_real_checker_every_ok_in_the_state(full_state, empty_state, tmp_path):
    def oks(st):
        return [(p, o) for p, o in walk(st) if isinstance(o, dict) and o.get("status") == "OK" and "id" in o and "title" in o]
    assert oks(empty_state) == []
    assert oks(full_state) == []                                                                        # the risk file carries a WARN alert, the data root is empty, the reconcile is internal only
    clean = good_set()
    clean["risk_state.json"]["alerts"] = []
    assert {o["id"] for _p, o in oks(build(tmp_path, clean))} == {"operations"}                         # positive control: the one checker that returned OK from real data
    for st in (empty_state, full_state):
        assert st["overview"]["headline"]["status"] != "OK" and st["testnet"]["pill"] != "OK"
        assert all(c["state"] != "OK" or c["id"] for c in st["accounting"]["checks"])


def test_gross_net_fields_are_never_the_same_object_and_net_is_never_inferred_from_gross(tmp_path):
    files = good_set()
    files["replay_report.json"]["net_pnl_usd"] = None
    st = build(tmp_path, files)
    assert st["figures"]["gross"]["v"] == 18.5 and st["figures"]["net"]["v"] is None and st["figures"]["net"]["basis"] == "MISSING"
    assert st["execution"]["net"]["v"] is None and "net_pnl_usd" not in json.dumps(st["figures"]["gross"])


def test_stale_artifacts_and_old_state_are_visible(tmp_path):
    files = good_set()
    files["testnet_session.json"]["generated_ms"] = NOW - 4 * 86_400_000
    st = build(tmp_path, files)
    assert {x["file"]: x["stale"] for x in st["evidence"]["sources"]}["testnet_session.json"] is True
    assert any(a["source"] == "artifacts" and "testnet_session.json" in a["text"] for a in st["overview"]["attention"])
    js = _bundle()["app.js"]
    assert "STALE_FETCH_MS" in js and "Stale source file" in js and 'id="updated"' in _bundle()["index.html"] and 'id="stale"' in _bundle()["index.html"]


def test_the_page_has_the_eight_views_loading_and_error_states_and_a_poll(tmp_path):
    b = _bundle()
    for v in ("overview", "research", "portfolio", "execution", "accounting", "risk", "testnet", "evidence"):
        assert f'data-view="{v}"' in b["index.html"] and f"{v}: {v}" in b["app.js"], v
    assert "Loading state from the local backend" in b["app.js"] and "Could not load /api/state" in b["app.js"] and "setInterval(load" in b["app.js"] and "INCONSISTENT_RUN" in b["app.js"]
    assert "createTextNode" in b["app.js"] and "@media (max-width: 700px)" in b["style.css"] and "overflow-x: hidden" in b["style.css"]


# ---------------------------------------------------------------- hostile strings (backend accepts them as data; the page must render them as text)
HOSTILE = '<img src=x onerror=window.__xss=1>"\''
ENUM_KEYS = {"schema", "basis", "kind", "spot_hedge", "status", "side", "severity", "prov", "universe_scope", "mode", "venue", "endpoint_host", "outcome", "state", "order_type"}
KEYED_MAPS = {"required_span", "data_digest_parts", "evidence_basis", "eligible_perpetual_lifecycle_universe", "lifecycle_class_counts", "exclusion_reasons", "final_position"}


def hostile(o, key=None):
    if isinstance(o, dict):
        return {(f"{HOSTILE}{i}" if key in KEYED_MAPS else k): hostile(v, k) for i, (k, v) in enumerate(o.items())}
    if isinstance(o, list):
        return [hostile(x, key) for x in o]
    if isinstance(o, str) and key not in ENUM_KEYS and key is not None:
        return HOSTILE + o
    return o


def hostile_set():
    files = {n: hostile(o) for n, o in good_set(**{"testnet_session.json": tn_executed()}).items()}
    files["reconcile_report.json"]["findings"] = [{"code": HOSTILE, "detail": HOSTILE, "severity": "WARN"}]
    files["risk_state.json"]["halted"] = True
    files["risk_state.json"]["halt_reasons"] = [HOSTILE]
    return files


def test_hostile_strings_in_every_artifact_field_are_accepted_as_data_and_reach_the_state_verbatim(tmp_path):
    files = hostile_set()
    st = build(tmp_path, files)
    assert [x["file"] for x in st["evidence"]["sources"] if x["state"] != "OK"] == []                    # the test is only meaningful if every file loaded
    text = json.dumps(st)
    assert text.count("onerror=window.__xss=1") > 60
    assert st["testnet"]["orders"][0]["link_id"].startswith(HOSTILE) and st["research"]["funnel"]["steps"][0]["name"].startswith(HOSTILE)
    assert st["coherence"]["verdict"] == "CONSISTENT"                                                   # identical mapping everywhere: still one run


def test_the_page_renders_hostile_strings_as_text_in_all_eight_views_at_both_widths(tmp_path):
    ad = write_set(tmp_path / "artifacts", hostile_set())
    cfg = CC._Config(None, None, ad, False, False, clock=lambda: NOW, ttl_ms=10 ** 12, env={})
    with _serving(cfg) as srv:
        res = _render(srv, tmp_path, "hostile")
    assert sum(1 for r in res if r["hostile"]) >= 10                                                    # the strings are on the page as visible text, not interpreted


@contextlib.contextmanager
def _serving(cfg):
    srv = CC.make_server(cfg, "127.0.0.1", 0)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        yield srv
    finally:
        srv.shutdown()
        srv.server_close()
        t.join(5)


def test_render_smoke_inconsistent_run_and_executed_testnet_and_legacy(tmp_path):
    inc = good_set(**{"testnet_session.json": tn_executed()})
    inc["ledger_pnl.json"]["identity"] = {**IDENT, "run_id": "run_other"}
    for tag, files in (("inconsistent", inc), ("testnet", good_set(**{"testnet_session.json": tn_executed()}))):
        ad = write_set(tmp_path / tag / "artifacts", files)
        with _serving(CC._Config(None, None, ad, False, False, clock=lambda: NOW, ttl_ms=10 ** 12, env={})) as srv:
            _render(srv, tmp_path / tag, tag)


def test_slippage_and_adverse_selection_are_shown_with_their_source_when_the_ledger_states_them(tmp_path):
    files = good_set()
    files["ledger_pnl.json"].update(taker_slippage_usd=1.5, adverse_selection_usd=0.75)
    comp = {c["key"]: c["value"] for c in build(tmp_path, files)["accounting"]["components"]}
    assert comp["slippage"]["v"] == 1.5 and comp["slippage"]["src"] == "ledger_pnl.json:taker_slippage_usd" and comp["slippage"]["basis"] == "SIMULATED"
    assert comp["adverse_selection"]["v"] == 0.75 and comp["adverse_selection"]["src"] == "ledger_pnl.json:adverse_selection_usd"
    files["ledger_pnl.json"].update(taker_slippage_usd=None, adverse_selection_usd=None)
    comp = {c["key"]: c["value"] for c in build(tmp_path / "unk", files)["accounting"]["components"]}
    assert comp["slippage"]["v"] is None and comp["slippage"]["basis"] == "MISSING"             # unknown stays unknown


def test_the_testnet_credential_names_are_scrubbed_like_the_others(tmp_path):
    k1, k2 = "TESTNET" + "KEYCANARY123", "TESTNET" + "SECRETCANARY456"
    env = dict(zip(("BYBIT_TESTNET_API_KEY", "BYBIT_TESTNET_API_SECRET"), (k1, k2)))
    files = good_set()
    files["risk_state.json"]["alerts"] = [{"severity": "WARN", "code": "X", "detail": "leaked TESTNETKEYCANARY123 and TESTNETSECRETCANARY456"}]
    text = json.dumps(build(tmp_path, files, env=env))
    assert "TESTNETKEYCANARY123" not in text and "TESTNETSECRETCANARY456" not in text and "[value hidden]" in text


def test_a_preflight_result_under_basis_not_run_is_shown_as_the_preflight_state_never_as_green(tmp_path):
    pf = lambda status: {"status": status, "checked_ms": NOW, "checks": [{"name": "env_presence", "ok": status == "PASS", "detail": "x"}]}
    for status, want, pill in (("PASS", "PREFLIGHT_PASSED_NO_PLAN", "ATTENTION"), ("FAIL", "PREFLIGHT_FAILED", "FAIL")):
        t = build(tmp_path / status, good_set(**{"testnet_session.json": tn(preflight=pf(status))}))["testnet"]
        assert t["state"] == "OK" and t["session_state"] == want and t["pill"] == pill, (status, t.get("error"), t.get("session_state"))


def test_every_stage_of_a_real_testnet_session_loads_without_error_and_shows_the_right_state(tmp_path, monkeypatch):
    """END TO END: the real session tool (preflight -> plan -> execute, against the in-process fake testnet) writes testnet_session.json; the control centre must accept the file as
    written at every stage (no coercion by the test), read from the session's own state dir through ``testnet_dir``, and name the stage."""
    sys.path.insert(0, HERE)
    from ms_testnet_world import World
    w = World(tmp_path / "w", monkeypatch)
    os.makedirs(tmp_path / "empty_art")

    def shown(label):
        st = CC.build_state(data_root=None, universe_dir=None, artifacts_dir=str(tmp_path / "empty_art"), testnet_dir=w.state_dir, env={}, now_ms=w.clock.now() + 1000)
        t = st["testnet"]
        assert t["state"] == "OK", (label, t.get("error"))
        assert not any(src["state"] == "ERROR" for src in st["evidence"]["sources"]), label
        return t

    w.run("status")
    assert shown("status")["session_state"] == "NOT_RUN"
    assert w.run("preflight", env={}) == 1                                    # no credentials: a FAIL that never touched the network
    assert shown("preflight fail")["session_state"] == "PREFLIGHT_FAILED"
    assert w.run("preflight") == 0
    assert shown("preflight pass")["session_state"] == "PREFLIGHT_PASSED_NO_PLAN"
    d = w.ready()
    t = shown("planned")
    assert t["session_state"] == "PLANNED_UNCONFIRMED" and t["plan"]["digest"]["v"] == d and len(t["plan"]["legs"]) == 2
    assert w.execute(d) == 0
    t = shown("executed")
    assert t["session_state"] == "EXECUTED_FLAT_VERIFIED" and t["flat"]["verified_flat"] is True and t["flags"] == [] and t["counts"]["orders"] == 4


def test_testnet_dir_is_read_for_the_session_file_only_and_defaults_to_the_artifacts_dir(tmp_path):
    a, n = tmp_path / "art", tmp_path / "ns"
    write_set(a, good_set())
    write_set(n, {"testnet_session.json": tn_executed()})
    base = dict(data_root=None, universe_dir=None, env={}, now_ms=NOW)
    assert CC.build_state(artifacts_dir=str(a), **base)["testnet"]["session_state"] == "NOT_RUN"
    assert CC.build_state(artifacts_dir=str(a), testnet_dir=str(n), **base)["testnet"]["session_state"] == "EXECUTED_FLAT_VERIFIED"
    out = subprocess.run([sys.executable, os.path.join(BOT, "tools", "control_centre.py"), "--artifacts-dir", str(a), "--testnet-dir", str(n), "--print-state"],
                         capture_output=True, text=True, timeout=120)
    assert out.returncode == 0 and json.loads(out.stdout)["testnet"]["session_state"] == "EXECUTED_FLAT_VERIFIED"


# ------------------------------------------------------------------------------------------------ Round 20: the competition net is BEFORE the owner's cost of capital, and the view says so

def _comp_candidate(**extra):
    base = {"candidate_id": "carry_eth", "kind": "SPOT_PERP_CARRY", "spot_hedge": "HEDGEABLE_ON_BYBIT", "status": "INSUFFICIENT_EVIDENCE", "reason": "replay fills are MODELLED",
            "gross_bps_horizon": 30.0, "net_bps_horizon": 5.0, "n_observations": 40, "prov": "MODELLED"}
    base.update(extra)
    return base


def test_the_competition_view_carries_the_after_cost_of_capital_net_and_its_state_and_an_old_artifact_reads_unknown():
    """REPRODUCED (independent review, Round 20): opportunities.json's net is after the stated financing and competition.json's net is before it, both named net_bps_horizon, and the view showed the
    producer's number as 'net' with no word about the cost of capital. The after-figure and the input's state now travel with it; an artifact that does not carry them reads UNKNOWN, never zero."""
    stated = CC._v_competition({"candidates": [_comp_candidate(net_bps_after_cost_of_capital=-1.5, cost_of_capital={"state": "STATED", "apr": 0.05})]})["candidates"][0]
    assert stated["net_bps_after_cost_of_capital"] == -1.5 and stated["cost_of_capital_state"] == "STATED" and stated["net_bps_horizon"] == 5.0
    unstated = CC._v_competition({"candidates": [_comp_candidate(net_bps_after_cost_of_capital=None, cost_of_capital={"state": "UNSTATED", "apr": None})]})["candidates"][0]
    assert unstated["net_bps_after_cost_of_capital"] is None and unstated["cost_of_capital_state"] == "UNSTATED"
    old = CC._v_competition({"candidates": [_comp_candidate()]})["candidates"][0]
    assert old["net_bps_after_cost_of_capital"] is None and old["cost_of_capital_state"] is None
    forged = CC._v_competition({"candidates": [_comp_candidate(cost_of_capital={"state": "FREE"})]})["candidates"][0]
    assert forged["cost_of_capital_state"] is None, "only the two real states are carried"


# ------------------------------------------------------------------------------------------------ Round 21: the capital the cost of capital was charged on travels with the after-cost figure

def test_the_competition_view_states_the_capital_basis_beside_the_after_cost_net_and_never_stays_silent():
    """REPRODUCED (Round 21): the view kept only the cost-of-capital STATE; the producer's capital factor and its basis text were dropped, so 'net after cost of capital' and the $/day read as comparable
    with the executor's although they were charged on 0.6x of the capital the executor commits. The factor travels with the figure; an artifact that does not carry it says so."""
    coc = {"state": "STATED", "apr": 0.05, "capital_factor_used": {"min": 2.04, "max": 2.04}, "capital_factor_basis": "economic_model.structure_capital_usd at leverage 1"}
    row = CC._v_competition({"candidates": [_comp_candidate(net_bps_after_cost_of_capital=-1.5, cost_of_capital=coc)]})["candidates"][0]
    assert row["capital_factor_used"] == {"min": 2.04, "max": 2.04} and "structure_capital_usd" in row["capital_factor_basis"]
    note = CC._coc_basis_note(row)
    assert "2.04x" in note and "structure_capital_usd" in note
    old_row = CC._v_competition({"candidates": [_comp_candidate(net_bps_after_cost_of_capital=-1.5, cost_of_capital={"state": "STATED", "apr": 0.05})]})["candidates"][0]
    assert old_row["capital_factor_used"] is None and "NOT carried" in CC._coc_basis_note(old_row) and "1.2x research basis" in CC._coc_basis_note(old_row)
    legacy = CC._v_competition({"candidates": [_comp_candidate(net_bps_after_cost_of_capital=-1.5, cost_of_capital={**coc, "capital_factor_used": {"min": 1.2, "max": 1.2}})]})["candidates"][0]
    assert "1.20x" in CC._coc_basis_note(legacy), "an artifact that states 1.2x is shown as 1.20x: the reader sees the basis that was used, not the one that is now correct"
    assert "BELOW the 2.04x the executor commits for a carry" in CC._coc_basis_note(legacy), "a carry charged on less than the executor's capital is flagged as not comparable"
    assert "BELOW" not in CC._coc_basis_note(row), "the registry basis (2.04x) carries no warning"
    span = CC._v_competition({"candidates": [_comp_candidate(cost_of_capital={**coc, "capital_factor_used": {"min": 1.02, "max": 2.04}})]})["candidates"][0]
    assert "1.02x to 2.04x" in CC._coc_basis_note(span)


@pytest.mark.parametrize("bad", [{"min": True, "max": 2.0}, {"min": 0, "max": 2.0}, {"min": -1.0, "max": 2.0}, {"min": float("nan"), "max": 2.0}, {"min": 3.0, "max": 2.0}, {"min": "2", "max": 2.0}, [2.04], "2.04", None])
def test_a_forged_capital_factor_is_not_carried(bad):
    row = CC._v_competition({"candidates": [_comp_candidate(cost_of_capital={"state": "STATED", "apr": 0.05, "capital_factor_used": bad})]})["candidates"][0]
    assert row["capital_factor_used"] is None
