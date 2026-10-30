"""Operations: forward-evidence alerts, deduplicated history, an honest digest,
and the paper operator's forward facts. Offline: every transport is injected.
"""
from __future__ import annotations

import calendar
import json
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
BOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(BOT, "tools"))
sys.path.insert(0, BOT)

import operator_paper_daily as opd  # noqa: E402
import runner_alerts as RA  # noqa: E402
import runner_monitor as RM  # noqa: E402
from test_runner_alerts import ENV, NOW, Log, Wire, row, text  # noqa: E402

REAL = os.path.join(BOT, "artifacts", "forward_shadow_current.json")
TODAY = float(calendar.timegm((2026, 10, 30, 12, 0, 0)))
ITS_DAY = float(calendar.timegm((2026, 9, 30, 12, 0, 0)))


@pytest.fixture(autouse=True)
def no_real_sleep(monkeypatch):
    monkeypatch.setattr(RA, "RETRY_BACKOFF_S", (0.0, 0.0))


# -- forward alerts ---------------------------------------------------------

def test_the_committed_artefact_today_raises_one_forward_stale_warning():
    status = RA.forward_status(REAL, TODAY)
    assert status["state"] == "STALE"
    alerts = RA.forward_alerts(status)
    assert [(a.key, a.severity) for a in alerts] == [("forward_stale", RA.WARNING)]
    assert "not a gate decision" in alerts[0].text


def test_a_fresh_but_small_sample_is_a_reading_not_a_failure():
    status = RA.forward_status(REAL, ITS_DAY)
    assert status["state"] == "INSUFFICIENT_SAMPLE"
    assert RA.forward_alerts(status) == []


def test_missing_and_garbage_artefacts_page(tmp_path):
    missing = RA.forward_status(str(tmp_path / "none.json"), TODAY)
    assert [a.key for a in RA.forward_alerts(missing)] == ["forward_no_data"]
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    assert [a.key for a in RA.forward_alerts(RA.forward_status(str(bad), TODAY))] \
        == ["forward_unreadable"]


def test_no_artefact_configured_means_no_check():
    assert RA.forward_status(None, TODAY) is None
    assert RA.forward_alerts(None) == []


def test_a_reader_that_raises_is_unreadable_not_silent(monkeypatch):
    import forward_status as FS

    def boom(*a, **k):
        raise RuntimeError("x")
    monkeypatch.setattr(FS, "evaluate_forward_evidence", boom)
    assert RA.forward_status(REAL, TODAY)["state"] == "UNREADABLE"


def test_evaluate_adds_the_forward_alert_beside_the_history_alerts():
    h = text(row(5))
    assert RA.evaluate(h, NOW) == []
    keys = [a.key for a in RA.evaluate(h, NOW, forward=RA.forward_status(REAL, TODAY))]
    assert keys == ["forward_stale"]


def test_the_forward_alert_is_deduplicated_and_its_recovery_said_once():
    t = RA.Throttle(resend_s=3600)
    stale = RA.forward_alerts(RA.forward_status(REAL, TODAY))
    assert [a.key for a in t.plan(stale, 0.0)] == ["forward_stale"]
    assert t.plan(stale, 60.0) == []
    assert t.plan(stale, 120.0) == []
    assert [a.key for a in t.plan([], 180.0)] == ["forward_stale:recovered"]
    assert t.plan([], 240.0) == []


# -- a repeated history line is one cycle -----------------------------------

def test_one_failure_delivered_twice_is_not_a_streak():
    failed = row(5, "MONITOR_FAILED", reasons=["x"])
    assert [a.key for a in RA.evaluate(text(row(65), failed, failed), NOW)] == []


def test_two_distinct_failures_still_are():
    h = text(row(65, "MONITOR_FAILED", reasons=["x"]),
             row(5, "MONITOR_FAILED", reasons=["x"]))
    assert "failed_streak" in [a.key for a in RA.evaluate(h, NOW)]


def test_a_repeated_line_cannot_complete_a_mainnet_stall():
    r = row(5, mainnet_state="FAULT")
    rows = [row(5 + 60 * i, mainnet_state="FAULT") for i in range(4, 0, -1)]
    h = text(*rows, r, r)                       # 5 distinct, 6 lines
    assert "mainnet_stalled" not in [a.key for a in RA.evaluate(h, NOW)]


def test_distinct_rows_keeps_order_and_first_occurrence():
    a, b = {"at": "1", "outcome": "x"}, {"at": "2", "outcome": "x"}
    assert RA.distinct_rows([a, b, dict(a), b]) == [a, b]


# -- the digest never claims health it does not have ------------------------

def test_a_healthy_history_digest_is_info():
    d = RA.build_digest(text(row(5)), NOW)
    assert d.severity == RA.INFO and d.text.startswith("daily digest\n")


@pytest.mark.parametrize("history", [
    text(row(400)),                                        # stale
    text(row(5, "MONITOR_FAILED", reasons=["x"])),         # last cycle failed
    text(row(-600)),                                       # future-stamped
    "",                                                    # no data
])
def test_an_unhealthy_history_digest_is_a_warning_headed_not_healthy(history):
    d = RA.build_digest(history, NOW)
    assert d.severity == RA.WARNING
    assert d.text.startswith("daily digest: NOT HEALTHY")


def test_the_digest_carries_the_forward_facts():
    d = RA.build_digest(text(row(5)), NOW, forward=RA.forward_status(REAL, TODAY))
    assert "forward    STALE (evidence INSUFFICIENT_SAMPLE, trades 4 of 20, " \
           "days 51 of 180" in d.text
    assert "not a gate decision" in d.text


# -- wired into the monitor loop --------------------------------------------

def _history(root, *rows):
    with open(os.path.join(root, "monitor.jsonl"), "w", encoding="utf-8") as fh:
        fh.write(text(*rows))


def test_the_monitor_pages_stale_forward_evidence_once(tmp_path):
    root = str(tmp_path)
    _history(root, row(5))
    wire = Wire()
    a = RM._Alerts(root, RA.Notifier(ENV, transport=wire, say=Log()),
                   lambda: NOW, forward_artifact=REAL)
    a.after_cycle()
    a.after_cycle()
    texts = [m["text"] for _, m, _ in wire.sent]
    assert len([t for t in texts if "forward-shadow evidence is STALE" in t]) == 1


def test_without_an_artefact_the_monitor_is_unchanged(tmp_path):
    root = str(tmp_path)
    _history(root, row(5))
    wire = Wire()
    RM._Alerts(root, RA.Notifier(ENV, transport=wire, say=Log()),
               lambda: NOW).after_cycle()
    assert wire.sent == []


def test_loop_passes_the_artefact_through(tmp_path):
    wire = Wire()
    n = RA.Notifier(ENV, transport=wire, say=Log(), sleep=lambda s: None)

    def once(run_dir, *, env, history_root, **kw):
        _history(history_root, row(5))
        return 0
    code = RM.loop(str(tmp_path), env={}, interval_s=0.0, max_runs=1,
                   clock=lambda: NOW, sleep=lambda s: None, once=once,
                   notifier=n, forward_artifact=REAL)
    assert code == 0
    texts = [m["text"] for _, m, _ in wire.sent]
    assert any("forward-shadow evidence is STALE" in t for t in texts)
    assert any("forward-shadow evidence" in t and "alerts are ON" in t
               for t in texts)


def test_the_cli_checks_the_artefact_on_a_dry_run(tmp_path, capsys):
    h = tmp_path / "monitor.jsonl"
    h.write_text(text(row(5)))
    code = RA.main([str(h), "--now", str(NOW), "--dry-run",
                    "--forward-artifact", REAL], env={})
    out = capsys.readouterr().out
    assert code == 1 and "forward_stale" in out


# -- the paper operator reports the forward facts ---------------------------

def test_operator_reports_the_artefact_as_stale_today(tmp_path):
    code, report = opd.run(BOT, {"USE_TESTNET": "1", "PAPER_TRADING": "1",
                                 "STATE_DB_PATH": str(tmp_path / "none.db")},
                           now=TODAY)
    assert code == 0, report["preflight_reasons"]
    fs = report["forward_status"]
    assert fs["state"] == "STALE" and fs["evidence_state"] == "INSUFFICIENT_SAMPLE"
    assert fs["is_gate_decision"] is False
    assert report["promotion_gate"]["allows_live"] is False
    last = opd.three_lines(report).splitlines()[-1]
    assert last.startswith("forward_state=STALE evidence=INSUFFICIENT_SAMPLE")
    assert "trades=4 of 20 days=51 of 180" in last


def test_operator_with_no_artefact_says_no_data(tmp_path):
    os.makedirs(tmp_path / "artifacts")
    code, report = opd.run(str(tmp_path), {"STATE_DB_PATH": str(tmp_path / "x.db")},
                           now=TODAY)
    assert report["forward_status"]["state"] == "NO_DATA"


def test_an_unreadable_state_db_does_not_read_as_a_clear_kill_switch(tmp_path):
    db = tmp_path / "trading_state.db"
    db.write_bytes(b"this is not a sqlite database at all" * 100)
    engaged, why = opd.kill_switch_state(str(db))
    assert engaged is True and "unknown" in why
    code, report = opd.run(BOT, {"USE_TESTNET": "1", "PAPER_TRADING": "1",
                                 "STATE_DB_PATH": str(db)}, now=TODAY)
    assert code == 3
    assert any("kill switch" in r for r in report["preflight_reasons"])


def test_the_operator_never_writes_the_artefact(tmp_path):
    before = open(REAL, "rb").read()
    opd.run(BOT, {"STATE_DB_PATH": str(tmp_path / "n.db")}, now=TODAY)
    assert open(REAL, "rb").read() == before
