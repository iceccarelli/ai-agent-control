"""data_freshness_audit: offline, read-only. Roots are built with the real collector / history code and injected clocks; a socket guard fails any real connection."""
from __future__ import annotations

import ast
import hashlib
import json
import os
import socket
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
BOT = os.path.dirname(HERE)
sys.path.insert(0, BOT)
sys.path.insert(0, os.path.join(BOT, "tools"))
sys.path.insert(0, HERE)

import data_freshness_audit as DA  # noqa: E402
import market_collector as MC  # noqa: E402
import market_history as MH  # noqa: E402
import test_market_collector as TC  # noqa: E402
import test_market_history as TH  # noqa: E402

STEP = TC.STEP
TT = TH.Time()                                   # one fake clock for the token bucket: sleeping advances it


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def boom(*_a, **_k):
        raise AssertionError("network touched")
    monkeypatch.setattr(socket.socket, "connect", boom)
    monkeypatch.setattr(socket, "create_connection", boom)
    monkeypatch.setattr(socket, "getaddrinfo", boom)


def tree_hash(root):
    h = hashlib.sha256()
    for dp, dn, fn in os.walk(root):
        dn.sort()
        for f in sorted(fn):
            p = os.path.join(dp, f)
            h.update(p.encode() + open(p, "rb").read())
    return h.hexdigest()


def build(tmp_path, cycles=3, backup=True, restore=True, history=True):
    """A real root: ``cycles`` collector cycles with seal/backup/restore-check, plus (optionally) a COMPLETE funding dataset. Returns (root, ctx)."""
    root = tmp_path / "root"
    seal, rec, bk = tmp_path / "seal.jsonl", tmp_path / "receipts.jsonl", tmp_path / "backup"
    kw = {}
    if backup:
        kw.update(seal_manifest=str(seal), seal_every=cycles, backup_to=str(bk), receipts=str(rec), backup_every=cycles, restore_check_every=cycles if restore else 0)
    h, clk, v = TC.cont(root, max_cycles=cycles, **kw)
    ctx = {"clk": clk, "venue": v, "seal": str(seal), "rec": str(rec), "bk": str(bk), "now": clk.t + 1000, "root": root}
    if history:
        specs = TH.funding_specs(("BTCUSDT", "ETHUSDT"))
        clock = TH.Clock()
        TH.MH.run_dataset(str(root), "funding", None, specs, fetch=TH.funding_venue(("BTCUSDT", "ETHUSDT")), clock=clock, sleep=TT.sleep, mono=TT.mono,
                          free_bytes_fn=lambda _p: 1 << 40, reserve_bytes=1)
    return root, ctx


def verdicts(rep):
    return {k: v["verdict"] for k, v in rep["axes"].items()}


def codes(rep, axis="data_freshness"):
    return {i["code"] for i in rep["axes"][axis]["issues"]}


# ---------------------------------------------------------------- NO_DATA is never healthy

def test_an_empty_root_is_no_data_and_exits_non_zero(tmp_path, capsys):
    r = tmp_path / "r"
    os.mkdir(r, 0o700)
    rep = DA.audit(str(r), now_ms=TC.T0)
    assert verdicts(rep) == {"code_correctness": "NOT_ASSESSED", "data_freshness": "NO_DATA", "research_readiness": "NO_DATA"}
    assert DA.main(["--root", str(r), "--now-ms", str(TC.T0)]) == 4
    assert json.loads(capsys.readouterr().out)["axes"]["data_freshness"]["verdict"] == "NO_DATA"


def test_a_passing_backup_and_restore_do_not_make_no_data_healthy(tmp_path):
    r = tmp_path / "r"
    os.mkdir(r, 0o700)
    MC._write_health(str(r / MC.HEALTH_NAME), {"schema": MC.HEALTH_SCHEMA, "interval_ms": STEP, "status": "STOPPED", "consecutive_failures": 0,
                                               "last_backup": {"ok": True}, "last_restore_check": {"ok": True}, "last_seal": {"ok": True}})
    rep = DA.audit(str(r), now_ms=TC.T0)
    assert verdicts(rep)["data_freshness"] == "NO_DATA" and verdicts(rep)["research_readiness"] == "NO_DATA"


def test_an_empty_dataset_manifest_alone_is_not_data(tmp_path):
    r = tmp_path / "r"
    os.mkdir(r, 0o700)
    specs = TH.funding_specs()
    MH.run_dataset(str(r), "funding", None, specs, fetch=TH.HV({}), clock=TH.Clock(), sleep=TT.sleep, mono=TT.mono, free_bytes_fn=lambda _p: 1 << 40, reserve_bytes=1)
    rep = DA.audit(str(r), now_ms=TH.SERVER0)
    assert verdicts(rep)["data_freshness"] == "NO_DATA"


# ---------------------------------------------------------------- HEALTHY needs everything

def test_real_complete_fresh_data_with_passing_backup_and_restore_is_healthy(tmp_path, capsys):
    root, ctx = build(tmp_path)
    before = tree_hash(root)
    rep = DA.audit(str(root), now_ms=ctx["now"], backup_dir=ctx["bk"], receipts=ctx["rec"], seal_manifest=ctx["seal"])
    assert verdicts(rep)["data_freshness"] == "HEALTHY", rep["axes"]["data_freshness"]["issues"]
    assert rep["cycles"]["latest_complete_cycle"]["status"] == "OK" and rep["cycles"]["cycles"] == 3 and rep["backups"]["source"] == "live_check"
    assert rep["history"]["funding"]["verify"] == "OK" and rep["history"]["funding"]["symbols"]["BTCUSDT"]["latest_observed_ts"] == TH.BASE
    assert rep["history"]["funding"]["symbols"]["BTCUSDT"]["expected_cadence_ms"] == TH.H8
    assert "tickers:*" in rep["cycles"]["fields"] and any(k.startswith("book:") for k in rep["cycles"]["fields"])
    assert tree_hash(root) == before                                                   # the audit writes nothing
    assert DA.main(["--root", str(root), "--now-ms", str(ctx["now"]), "--backup-dir", ctx["bk"], "--receipts", ctx["rec"], "--seal-manifest", ctx["seal"]]) == 0
    capsys.readouterr()


def test_healthy_from_the_collectors_own_health_report_without_live_checks(tmp_path):
    root, ctx = build(tmp_path)
    rep = DA.audit(str(root), now_ms=ctx["now"])
    assert verdicts(rep)["data_freshness"] == "HEALTHY" and rep["backups"]["source"] == "health_report"


def test_healthy_is_independent_of_research_readiness(tmp_path):
    root, ctx = build(tmp_path)
    rep = DA.audit(str(root), now_ms=ctx["now"])
    assert verdicts(rep)["data_freshness"] == "HEALTHY" and verdicts(rep)["research_readiness"] == "NOT_READY"      # no research universe declared, v1 capture lacks partitions
    assert {"MISSING_STATUS_PARTITIONS"} <= codes(rep, "research_readiness")


@pytest.mark.parametrize("kw,code", [({"backup": False}, "BACKUP_NEVER_RAN"), ({"restore": False}, "RESTORE_CHECK_NEVER_RAN")])
def test_no_backup_or_no_restore_check_is_partial_never_healthy(tmp_path, kw, code):
    root, ctx = build(tmp_path, **kw)
    rep = DA.audit(str(root), now_ms=ctx["now"])
    assert verdicts(rep)["data_freshness"] == "PARTIAL" and code in codes(rep)
    assert DA.main(["--root", str(root), "--now-ms", str(ctx["now"])]) == 1


def test_a_failed_backup_in_the_health_report_is_partial(tmp_path):
    root, ctx = build(tmp_path)
    h = json.load(open(root / MC.HEALTH_NAME))
    h["last_backup"] = {"at_ms": 1, "ok": False, "detail": {"failed": ["cycle-1: copy differs"]}}
    MC._write_health(str(root / MC.HEALTH_NAME), h)
    rep = DA.audit(str(root), now_ms=ctx["now"])
    assert verdicts(rep)["data_freshness"] == "PARTIAL" and "BACKUP_FAILED" in codes(rep)


def test_a_failed_live_restore_check_is_partial(tmp_path):
    root, ctx = build(tmp_path)
    victim = os.path.join(ctx["bk"], MC.cycle_dirs(ctx["bk"])[0], "manifest.json")
    open(victim, "ab").write(b" ")
    rep = DA.audit(str(root), now_ms=ctx["now"], backup_dir=ctx["bk"], receipts=ctx["rec"], seal_manifest=ctx["seal"])
    assert verdicts(rep)["data_freshness"] == "PARTIAL" and "RESTORE_CHECK_FAILED" in codes(rep)
    assert rep["backups"]["restore_check"]["live"] is True


def test_a_seal_check_failure_is_partial(tmp_path):
    root, ctx = build(tmp_path)
    c = MC.cycle_dirs(str(root))[0]
    p = os.path.join(str(root), c, "tickers.jsonl")
    open(p, "ab").write(b"x")
    rep = DA.audit(str(root), now_ms=ctx["now"], backup_dir=ctx["bk"], receipts=ctx["rec"], seal_manifest=ctx["seal"])
    assert verdicts(rep)["data_freshness"] == "PARTIAL" and "SEAL_FAILED" in codes(rep)


# ---------------------------------------------------------------- stale, gaps, failing collector, corrupt datasets

def test_old_cycles_are_stale(tmp_path, capsys):
    root, ctx = build(tmp_path)
    rep = DA.audit(str(root), now_ms=ctx["now"] + 10 * STEP)
    assert verdicts(rep)["data_freshness"] == "STALE" and rep["cycles"]["stale"] is True and "STALE_DATA" in codes(rep)
    assert DA.main(["--root", str(root), "--now-ms", str(ctx["now"] + 10 * STEP)]) == 3
    capsys.readouterr()


def test_a_symbol_or_field_that_stopped_updating_is_reported_stale(tmp_path):
    root, ctx = build(tmp_path, cycles=3, history=False)
    # the newest cycle stops carrying one symbol's book: that field's newest observation is older than the cadence allows
    names = MC.cycle_dirs(str(root))
    newest = os.path.join(str(root), names[-1], "manifest.json")
    man = json.load(open(newest))
    man["artifacts"] = [a for a in man["artifacts"] if not (a["kind"] == "book" and a["symbol"] == "BTCUSDT")]
    os.chmod(newest, 0o600)
    json.dump(man, open(newest, "w"))
    # only the newest manifest was edited; older cycles still carry BTCUSDT, so its newest book is two cycles old
    rep = DA.audit(str(root), now_ms=ctx["now"] + 2 * STEP)
    assert any(k.startswith("book:") for k in rep["cycles"]["fields"]) and rep["cycles"]["fields"]["book:BTCUSDT"]["age_ms"] > 2 * STEP
    assert rep["cycles"].get("stale_fields") and verdicts(rep)["data_freshness"] == "STALE"


def test_recorded_gaps_between_cycles_are_partial(tmp_path):
    root, ctx = build(tmp_path, cycles=2, history=False)
    ctx["clk"].t += 20 * STEP
    TC.cont(root, clk=ctx["clk"], venue=ctx["venue"], max_cycles=1)
    rep = DA.audit(str(root), now_ms=ctx["clk"].t + 1000)
    assert rep["cycles"]["gaps"] and "CYCLE_GAPS" in codes(rep) and verdicts(rep)["data_freshness"] == "PARTIAL"


def test_a_collector_that_is_failing_or_refused_is_partial(tmp_path):
    root, ctx = build(tmp_path, cycles=2, history=False)
    h = json.load(open(root / MC.HEALTH_NAME))
    h.update(status="REFUSED", refusal={"code": "DISK_GUARD"}, consecutive_failures=4)
    MC._write_health(str(root / MC.HEALTH_NAME), h)
    rep = DA.audit(str(root), now_ms=ctx["now"])
    assert {"COLLECTOR_REFUSED", "COLLECTOR_FAILING"} <= codes(rep) and verdicts(rep)["data_freshness"] == "PARTIAL"


def test_a_corrupt_dataset_page_is_partial_and_names_the_problem(tmp_path):
    root, ctx = build(tmp_path)
    p = root / "funding" / "BTCUSDT" / "page-000002.json"
    data = bytearray(open(p, "rb").read())
    data[40] ^= 1
    open(p, "wb").write(bytes(data))
    rep = DA.audit(str(root), now_ms=ctx["now"])
    assert rep["history"]["funding"]["verify"] == "FAILED" and "DATASET_DOES_NOT_VERIFY" in codes(rep) and verdicts(rep)["data_freshness"] == "PARTIAL"
    assert verdicts(rep)["research_readiness"] == "NOT_READY"


def test_a_tampered_cycle_log_is_partial(tmp_path):
    root, ctx = build(tmp_path, history=False)
    log = root / MC.LOG_NAME
    lines = open(log).read().splitlines()
    os.chmod(log, 0o600)
    open(log, "w").write("\n".join(lines[:-1] + [lines[-1].replace('"OK"', '"PARTIAL"')]) + "\n")
    rep = DA.audit(str(root), now_ms=ctx["now"])
    assert "CYCLE_LOG_DOES_NOT_VERIFY" in codes(rep) and verdicts(rep)["data_freshness"] != "HEALTHY"


def test_a_non_complete_dataset_is_partial_and_skip_verify_can_never_be_healthy(tmp_path):
    root, ctx = build(tmp_path)
    rep = DA.audit(str(root), now_ms=ctx["now"], verify=False)
    assert verdicts(rep)["data_freshness"] == "PARTIAL" and "VERIFICATION_SKIPPED" in codes(rep)
    r2 = tmp_path / "r2"
    os.mkdir(r2, 0o700)
    ts = TH.grid(TH.FIRST, TH.N, TH.H8)
    del ts[100]
    MH.run_dataset(str(r2), "funding", None, TH.funding_specs(), fetch=TH.HV({("funding", "BTCUSDT"): ts}), clock=TH.Clock(), sleep=TT.sleep, mono=TT.mono,
                   free_bytes_fn=lambda _p: 1 << 40, reserve_bytes=1)
    rep2 = DA.audit(str(r2), now_ms=TH.SERVER0)
    assert "DATASET_PARTIAL" in codes(rep2) and verdicts(rep2)["data_freshness"] != "HEALTHY"


def test_a_history_window_that_ended_too_long_ago_is_stale_on_request(tmp_path):
    root, ctx = build(tmp_path)
    rep = DA.audit(str(root), now_ms=ctx["now"] + 10 * TH.H8, history_max_age_ms=TH.H8)
    assert rep["history"]["funding"]["stale_symbols"] == ["BTCUSDT", "ETHUSDT"] or verdicts(rep)["data_freshness"] == "STALE"
    assert verdicts(rep)["data_freshness"] == "STALE"


# ---------------------------------------------------------------- research readiness and coverage versus the declared universe

def _universe_file(tmp_path, syms):
    p = tmp_path / "research.json"
    p.write_text(json.dumps({"symbols": list(syms)}))
    return str(p)


def test_coverage_versus_a_declared_research_universe_and_missing_status_partitions(tmp_path):
    root, ctx = build(tmp_path)
    uf = _universe_file(tmp_path, ["BTCUSDT", "ETHUSDT", "SOLUSDT"])
    rep = DA.audit(str(root), now_ms=ctx["now"], universe_file=uf)
    ru = rep["history"]["funding"]["research_universe"]
    assert ru["declared"] == 3 and ru["complete"] == 2 and ru["missing"] == ["SOLUSDT"]
    assert verdicts(rep)["research_readiness"] == "NOT_READY" and {"UNIVERSE_NOT_COVERED", "MISSING_STATUS_PARTITIONS"} <= codes(rep, "research_readiness")
    assert rep["status_partitions"]["missing"] == ["Trading", "Closed", "PreLaunch"]                 # the collector's v1 universe took the venue's default (nothing recorded)


def test_ready_needs_a_covered_universe_and_every_status_partition_observed(tmp_path):
    root, ctx = build(tmp_path)
    d = TH._universe_dir(tmp_path, syms=("BTCUSDT", "ETHUSDT", "OLDUSDT"), statuses={"OLDUSDT": "Closed"}, deliveries={"OLDUSDT": TH.BASE}, name="uni_ok")
    uf = _universe_file(tmp_path, ["BTCUSDT", "ETHUSDT"])
    rep = DA.audit(str(root), now_ms=ctx["now"], universe_file=uf, universe_capture=str(d))
    assert rep["status_partitions"]["missing"] == [] and rep["status_partitions"]["present"] == ["Closed", "PreLaunch", "Trading"] or rep["status_partitions"]["missing"] == []
    assert verdicts(rep)["research_readiness"] == "READY", rep["axes"]["research_readiness"]["issues"]
    refused = TH._universe_dir(tmp_path, syms=("BTCUSDT", "OLDUSDT"), statuses={"OLDUSDT": "Closed"}, refuse_closed=True, name="uni_refused")
    rep2 = DA.audit(str(root), now_ms=ctx["now"], universe_file=uf, universe_capture=str(refused))
    assert rep2["status_partitions"]["missing"] == ["Closed"] and verdicts(rep2)["research_readiness"] == "NOT_READY"
    assert "REFUSED_BY_VENUE" in rep2["status_partitions"]["outcomes"]["Closed"]
    assert verdicts(rep2)["data_freshness"] == "HEALTHY"                                              # the axes are separate


def test_the_research_universe_file_formats_and_bad_input(tmp_path):
    p = tmp_path / "u.txt"
    p.write_text("BTCUSDT # core\n\nETHUSDT\n")
    assert DA.load_research_universe(str(p)) == ["BTCUSDT", "ETHUSDT"]
    q = tmp_path / "u.json"
    q.write_text(json.dumps(["B1USDT", "A1USDT"]))
    assert DA.load_research_universe(str(q)) == ["A1USDT", "B1USDT"]
    for bad in ("[]", '["bad sym"]', ""):
        (tmp_path / "bad").write_text(bad)
        with pytest.raises(ValueError):
            DA.load_research_universe(str(tmp_path / "bad"))


def test_require_ready_makes_a_not_ready_corpus_exit_non_zero(tmp_path, capsys):
    root, ctx = build(tmp_path)
    argv = ["--root", str(root), "--now-ms", str(ctx["now"])]
    assert DA.main(argv) == 0
    assert DA.main(argv + ["--require-ready"]) == 1
    capsys.readouterr()


# ---------------------------------------------------------------- refusals, axes, scope

def test_a_root_inside_git_or_not_a_directory_is_refused(tmp_path, capsys):
    repo = tmp_path / "repo"
    os.makedirs(repo / ".git")
    r = repo / "data"
    os.mkdir(r, 0o700)
    assert DA.main(["--root", str(r)]) == 2
    assert DA.main(["--root", str(tmp_path / "missing")]) == 2
    capsys.readouterr()


def test_a_group_readable_root_is_reported(tmp_path):
    root, ctx = build(tmp_path)
    os.chmod(root, 0o755)
    rep = DA.audit(str(root), now_ms=ctx["now"])
    assert "ROOT_PERMISSIONS" in codes(rep) and verdicts(rep)["data_freshness"] == "PARTIAL"
    os.chmod(root, 0o700)


def test_the_three_axes_are_separate_and_code_correctness_is_not_assessed(tmp_path):
    root, ctx = build(tmp_path)
    rep = DA.audit(str(root), now_ms=ctx["now"])
    assert set(rep["axes"]) == {"code_correctness", "data_freshness", "research_readiness"}
    assert rep["axes"]["code_correctness"]["verdict"] == "NOT_ASSESSED" and "CI" in rep["axes"]["code_correctness"]["note"]
    assert set(DA.EXIT) == {"HEALTHY", "PARTIAL", "STALE", "NO_DATA"} and DA.EXIT["HEALTHY"] == 0 and all(v != 0 for k, v in DA.EXIT.items() if k != "HEALTHY")


def test_the_docstring_says_it_is_not_the_carry_watch_and_the_marker_is_untouched():
    src = open(os.path.join(BOT, "tools", "data_freshness_audit.py")).read()
    assert "carry-watch" in src and "Fly monitor" in src and "NO_DATA" in src
    tree = ast.parse(src)
    imps = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names} | {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    assert not {"urllib.request", "socket", "requests", "hmac", "subprocess", "bybit_connection"} & imps
    ini = open(os.path.join(BOT, "pytest.ini")).read()
    assert "data_freshness" in ini                                                                  # the existing marker is still declared; this module neither adds nor removes it


def test_the_audit_never_writes(tmp_path):
    root, ctx = build(tmp_path)
    before = tree_hash(root)
    DA.audit(str(root), now_ms=ctx["now"], backup_dir=ctx["bk"], receipts=ctx["rec"], seal_manifest=ctx["seal"], universe_file=_universe_file(tmp_path, ["BTCUSDT"]))
    assert tree_hash(root) == before
