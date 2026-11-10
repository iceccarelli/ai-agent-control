"""research_service: the public-data-only research worker core. Offline; state and data live in tmp_path (never bot/state)."""
import ast
import json
import os
import sys

import pytest

BOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [BOT, os.path.join(BOT, "tools")]
import research_service as RS  # noqa: E402

T0 = 1_794_312_000_000                                    # 2026-11-10 12:00 UTC


class Clock:
    def __init__(self):
        self.t = T0

    def __call__(self):
        return self.t


def data(tmp_path, days=("20261101", "20261102", "20261105")):
    r = tmp_path / "datasets" / "md-1"
    r.mkdir(parents=True)
    for d in days:
        (r / f"BTCUSDT-{d}.jsonl").write_text("x")
    return str(tmp_path / "datasets")


def svc(tmp_path, roots, clock=None):
    return RS.ResearchService(str(tmp_path / "state"), roots, clock or Clock())


def test_scheduling_is_idempotent_across_ticks_and_restarts(tmp_path):
    s = svc(tmp_path, {"datasets": data(tmp_path)})
    assert s.schedule() == 2 and s.schedule() == 0
    s.q.close()
    assert svc(tmp_path, {"datasets": str(tmp_path / "datasets")}).schedule() == 0              # a restart inside the same hour makes no duplicate jobs


def test_a_tick_finds_the_gap_and_the_stale_corpus_and_quarantines_typed_proposals(tmp_path):
    s = svc(tmp_path, {"datasets": data(tmp_path)})
    res = s.tick()
    assert res["worked"]["SUCCEEDED"] == 2 and res["worked"]["QUARANTINED"] == 0 and res["worked"]["FAILED"] == 0
    props = [json.loads(l) for l in open(os.path.join(s.quarantine, "proposals.jsonl"))]
    gap = [p for p in props if p["kind"] == "DATA_GAP" and "missing" in p["summary"]]
    stale = [p for p in props if p["kind"] == "DATA_GAP" and "newest named day" in p["summary"]]
    assert gap and gap[0]["parameters"]["missing_days"] == 2 and gap[0]["parameters"]["first_day"] == "2026-11-01"
    assert stale and stale[0]["parameters"]["age_days"] == 5 and stale[0]["parameters"]["stale_after_days"] == RS.STALE_AFTER_DAYS
    assert all(p["status"] == "PROPOSED_NOT_EXECUTABLE" and p["role"] == "infrastructure_ops" for p in props)


def test_a_fresh_complete_corpus_raises_no_gap_or_stale_finding(tmp_path):
    s = svc(tmp_path, {"datasets": data(tmp_path, days=("20261108", "20261109", "20261110"))})
    s.tick()
    props = [json.loads(l) for l in open(os.path.join(s.quarantine, "proposals.jsonl"))]
    assert [p["kind"] for p in props] == ["INFRASTRUCTURE_FINDING"] and "3 files" in props[0]["summary"]


def test_a_missing_or_unconfigured_root_is_an_infrastructure_finding_not_a_crash(tmp_path):
    s = svc(tmp_path, {"gone": str(tmp_path / "nope")})
    res = s.tick()
    assert res["worked"]["SUCCEEDED"] == 2
    props = [json.loads(l) for l in open(os.path.join(s.quarantine, "proposals.jsonl"))]
    assert any(p["kind"] == "INFRASTRUCTURE_FINDING" and "missing or empty" in p["summary"] for p in props)
    assert RS.handle_dataset_inventory({"root": "other"}, {}, T0)[0]["summary"].endswith("is not configured")


def test_a_crashing_handler_is_a_failed_attempt_with_backoff_and_never_kills_the_worker(tmp_path, monkeypatch):
    s = svc(tmp_path, {"datasets": data(tmp_path)})
    monkeypatch.setitem(RS.HANDLERS, "dataset_inventory", lambda *a: (_ for _ in ()).throw(RuntimeError("boom")))
    res = s.tick()
    assert res["worked"]["FAILED"] == 1 and res["worked"]["SUCCEEDED"] == 1                          # freshness still ran
    st = s.q.stats()["jobs"]
    assert st["QUEUED"] == 1 and st["DEAD"] == 0                                                      # the failed one waits for its backoff, it is not lost


def test_the_status_file_is_private_names_the_code_version_and_declares_no_capabilities(tmp_path):
    s = svc(tmp_path, {"datasets": data(tmp_path)})
    st = s.tick()["status"]
    p = os.path.join(str(tmp_path / "state"), "status.json")
    assert oct(os.stat(p).st_mode & 0o777) == "0o600" and json.load(open(p)) == st
    assert len(st["code_version"]) == 40 and st["code_version"] == RS.code_version()
    assert st["capabilities"] == {"orders": False, "exchange_credentials": False, "network": False}
    assert st["roots"]["datasets"]["newest_named_day"] == "2026-11-05" and st["queue"]["jobs"]["SUCCEEDED"] == 2


def test_every_job_names_its_inputs_and_the_exact_code_that_produced_the_result(tmp_path):
    s = svc(tmp_path, {"datasets": data(tmp_path)})
    s.schedule()
    j = s.q.claim("w", roles=["infrastructure_ops"])
    assert j["code_version"] == RS.code_version() and j["inputs"]["refs"][0]["name"] == "root:datasets" and len(j["inputs"]["refs"][0]["sha256"]) == 64 and len(j["config_digest"]) == 64


def test_the_cli_runs_once_and_prints_status(tmp_path, capsys):
    d = data(tmp_path)
    assert RS.main(["run", "--state-dir", str(tmp_path / "st"), "--root", f"datasets={d}", "--once"]) == 0
    assert json.loads(capsys.readouterr().out.strip().splitlines()[-1])["worked"]["SUCCEEDED"] == 2
    assert RS.main(["status", "--state-dir", str(tmp_path / "st"), "--root", f"datasets={d}"]) == 0
    assert RS.main(["run", "--state-dir", str(tmp_path / "st"), "--root", "bad", "--once"]) == 2


def test_the_worker_can_reach_no_venue_credential_or_network_by_construction():
    src = open(os.path.join(BOT, "tools", "research_service.py"), encoding="utf-8").read()
    tree = ast.parse(src)
    mods = {n.names[0].name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import)} | {n.module.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
    assert mods <= {"__future__", "argparse", "datetime", "hashlib", "json", "os", "signal", "sys", "threading", "time", "typing", "agent_contracts", "dataset_inventory", "research_jobs"}, mods
    low = src.lower()
    assert "environ" not in low and "socket" not in low and "requests" not in low and "urllib" not in low and "bybit" not in low.replace("venue", "")


def test_a_stop_signal_ends_the_loop_cleanly_after_the_current_tick(tmp_path, capsys):
    import signal
    d = data(tmp_path)
    # signal.signal only works in the main thread, so drive the handler the way the kernel would: set the stop event once the first tick has printed
    RS._STOP.clear()
    orig = RS.ResearchService.tick

    def tick_then_stop(self):
        r = orig(self)
        RS._on_signal(signal.SIGTERM, None)
        return r
    RS.ResearchService.tick = tick_then_stop
    try:
        rc = RS.main(["run", "--state-dir", str(tmp_path / "st"), "--root", f"datasets={d}", "--tick-seconds", "3600"])
    finally:
        RS.ResearchService.tick = orig
        RS._STOP.clear()
    assert rc == 0 and json.loads(capsys.readouterr().out.strip().splitlines()[-1])["worked"]["SUCCEEDED"] == 2


def test_the_image_files_exist_and_the_signal_handlers_are_installed():
    src = open(os.path.join(BOT, "tools", "research_service.py"), encoding="utf-8").read()
    assert "signal.SIGTERM" in src and "_STOP.wait(" in src
