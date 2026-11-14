"""Round 20: the registered forward kill test (E1) can neither be run early nor drift under its own engine, and its data freshness is a checkable fact, not prose. Offline; no holdout, ledger or anchor is touched."""
from __future__ import annotations

import ast
import csv
import datetime as dt
import gzip
import json
import os
import shutil
import socket
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
BOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(BOT, "tools"))
sys.path.insert(0, BOT)

import e1_status as E1  # noqa: E402

NOW = int(dt.datetime(2026, 11, 14, 0, 0, 0, tzinfo=dt.timezone.utc).timestamp() * 1000)


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def boom(*_a, **_k):
        raise AssertionError("network touched")
    monkeypatch.setattr(socket.socket, "connect", boom)
    monkeypatch.setattr(socket, "create_connection", boom)
    monkeypatch.setattr(socket, "getaddrinfo", boom)


def test_the_registered_window_start_is_the_protocols_instant():
    assert E1.WINDOW_START_UTC == "2026-09-09T16:00:00Z" and E1.WINDOW_START_MS == 1788969600000
    assert E1.REGISTERED_ENGINE_REVISION.startswith("56f9da6")


def test_the_engine_is_the_registered_one_today():
    eng = E1.engine_integrity()
    assert eng["engine_is_the_registered_one"], [r["file"] for r in eng["files"] if not r["match"]]
    assert len(eng["files"]) == len(E1.ENGINE_FILE_PINS) + 1


def _tree(tmp_path):
    root = tmp_path / "bot"
    for rel in list(E1.ENGINE_FILE_PINS) + [E1.REPLAY_FILE]:
        dst = root / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(os.path.join(BOT, rel), dst)
    return str(root)


@pytest.mark.parametrize("rel", sorted(E1.ENGINE_FILE_PINS))
def test_a_changed_engine_file_is_detected(tmp_path, rel):
    root = _tree(tmp_path)
    with open(os.path.join(root, rel), "a", encoding="utf-8") as fh:
        fh.write("\n# changed\n")
    eng = E1.engine_integrity(root)
    assert eng["engine_is_the_registered_one"] is False and [r["file"] for r in eng["files"] if not r["match"]] == [rel]


def test_a_changed_replay_computation_is_detected_but_a_console_change_is_not(tmp_path):
    root = _tree(tmp_path)
    path = os.path.join(root, E1.REPLAY_FILE)
    src = open(path, encoding="utf-8").read()
    console = src.replace('print(f"  driver     {r[\'driver\']}")', 'print(f"  driver     {r[\'driver\']} (console edit)")', 1)
    assert console != src
    open(path, "w", encoding="utf-8").write(console)
    assert E1.engine_integrity(root)["engine_is_the_registered_one"] is True
    computation = src.replace("< max(0.01, 1e-6 * abs(net))", "< max(0.02, 1e-6 * abs(net))", 1)
    assert computation != src
    open(path, "w", encoding="utf-8").write(computation)
    assert E1.engine_integrity(root)["engine_is_the_registered_one"] is False


def test_the_two_pins_of_the_replay_computation_cannot_drift_apart():
    import test_carry_replay_concentration as T
    assert E1.REPLAY_COMPUTATION_DIGEST == T._COMPUTATION_DIGEST and E1.REPLAY_PRESENTATION == T._PRESENTATION
    src = open(os.path.join(BOT, E1.REPLAY_FILE), encoding="utf-8").read()
    assert E1.replay_computation_digest(src) == T._computation_digest(src)[0]


def test_no_forward_data_is_committed_and_the_gap_is_stated_exactly():
    """The committed Bybit BTC corpus ends at the 2026-09-09T08:00Z print, eight hours BEFORE the registered window: nothing of the window exists, so nothing can be tested."""
    d = E1.forward_data(now_ms=NOW)
    assert d["state"] == "NO_FORWARD_DATA" and d["last_committed_funding_print"] == "2026-09-09T08:00:00Z"
    # 2026-09-09T16:00Z .. 2026-11-14T00:00Z is 196 eight-hour intervals: 197 prints settled at or before the clock (196 strictly before it)
    assert d["funding_prints_committed_in_window"] == 0 and d["funding_prints_expected_since_window_start"] == 197 and d["funding_prints_missing"] == 197
    assert d["elapsed_days"] == pytest.approx(65.333, abs=0.001)


def _corpus(tmp_path, first_ms, n):
    root = tmp_path / "corpus"
    (root / "funding").mkdir(parents=True)
    (root / "ohlcv").mkdir()
    with gzip.open(root / "funding" / "BYBIT_LINEAR_BTC_USDT_FUNDING.csv.gz", "wt", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["funding_time", "funding_time_ms", "symbol", "funding_rate", "mark_price"])
        for i in range(n):
            ms = first_ms + i * E1.FUNDING_INTERVAL_MS
            w.writerow([dt.datetime.fromtimestamp(ms / 1000, dt.timezone.utc).isoformat(), ms, "BTCUSDT", "0.0001", ""])
    return str(root)


def test_only_prints_inside_the_window_count_and_a_complete_capture_reads_current(tmp_path):
    before = E1.WINDOW_START_MS - 5 * E1.FUNDING_INTERVAL_MS
    corpus = _corpus(tmp_path, before, 5 + 10)                          # five prints BEFORE the window, ten inside it
    d = E1.forward_data(corpus, now_ms=E1.WINDOW_START_MS + 9 * E1.FUNDING_INTERVAL_MS)
    assert d["funding_prints_committed_in_window"] == 10 and d["funding_prints_missing"] == 0 and d["state"] == "FORWARD_DATA_CURRENT"
    stale = E1.forward_data(corpus, now_ms=E1.WINDOW_START_MS + 40 * E1.FUNDING_INTERVAL_MS)
    assert stale["state"] == "FORWARD_DATA_PRESENT_BUT_INCOMPLETE" and stale["funding_prints_missing"] == 31


def test_the_interim_and_final_dates_come_from_the_window_start_and_gate_on_the_clock():
    s = E1.schedule(now_ms=NOW)
    assert s["interim_six_months"]["not_before"] == "2027-03-09T16:00:00Z" and s["interim_six_months"]["status"] == "NOT_AVAILABLE_YET"
    assert s["final_twelve_months"]["not_before"] == "2027-09-09T16:00:00Z" and s["final_twelve_months"]["status"] == "NOT_AVAILABLE_YET"
    assert s["interim_six_months"]["days_remaining"] == 115
    later = E1.schedule(now_ms=int(dt.datetime(2027, 3, 10, tzinfo=dt.timezone.utc).timestamp() * 1000))
    assert later["interim_six_months"]["status"] == "AVAILABLE_AFTER_DATA_CHECK" and later["final_twelve_months"]["status"] == "NOT_AVAILABLE_YET"


def test_the_status_is_incomplete_computes_no_result_and_names_the_owner_commands(capsys, tmp_path, monkeypatch):
    ledger = tmp_path / "must_not_be_created.jsonl"
    monkeypatch.setenv("HISTORY_HOLDOUT_LEDGER", str(ledger))
    monkeypatch.setenv("CANDIDATE_LEDGER_DIR", str(tmp_path / "cand"))
    monkeypatch.setenv("CAPITAL_ANCHOR_DIR", str(tmp_path / "anchor"))
    doc = E1.status(now_ms=NOW)
    assert doc["verdict"] == "INCOMPLETE" and doc["result"] is None
    text = json.dumps(doc).lower()
    for forbidden in ("p_value", "upper_bound", "excess_usd", "mean_monthly", "winner\":", "rejected\":"):
        assert forbidden not in text
    assert any("INTERIM_NOT_AVAILABLE_YET" in p for p in doc["problems"]) and any("NO_FORWARD_DATA" in p for p in doc["problems"])
    assert any("funding_capture.py" in c and "1788969600000" in c for c in doc["owner_commands"]) and any("NO REPOSITORY TOOL" in c for c in doc["owner_commands"])
    assert E1.main(["--now-ms", str(NOW)]) == 1 and json.loads(capsys.readouterr().out)["verdict"] == "INCOMPLETE"
    assert not ledger.exists() and not (tmp_path / "cand").exists() and not (tmp_path / "anchor").exists(), "this tool never touches the holdout, a candidate ledger or an anchor"


def test_the_tool_imports_nothing_that_can_reach_the_holdout_a_ledger_an_anchor_or_a_venue():
    tree = ast.parse(open(os.path.join(BOT, "tools", "e1_status.py"), encoding="utf-8").read())
    imported = {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names} | {n.module.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
    assert not imported & {"history_holdout", "reserved_holdout", "candidate_competition", "ms_capital_anchor", "capital_anchor", "requests", "urllib", "http", "socket", "bybit_connection", "market_data"}, imported
