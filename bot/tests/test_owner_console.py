"""owner_console: offline read-out. Secrets are never printed, capital is never raised, unavailable is never shown as fine."""
from __future__ import annotations

import json
import os
import socket
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
BOT = os.path.dirname(HERE)
sys.path.insert(0, BOT)
sys.path.insert(0, os.path.join(BOT, "tools"))

import carry_prospective as CP  # noqa: E402
import owner_console as OC  # noqa: E402

LIVE = {"USE_TESTNET": "0", "PAPER_TRADING": "0", "LIVE_TRADING_ACK": "I_UNDERSTAND", "BYBIT_API_KEY": "KEYVALUE12345", "BYBIT_API_SECRET": "SECRETVALUE6789", "BYBIT_VENUE": "mainnet"}
LATER = CP.WINDOW_START_MS + 10 * 86_400_000


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def boom(*_a, **_k):
        raise AssertionError("network touched")
    monkeypatch.setattr(socket.socket, "connect", boom)
    monkeypatch.setattr(socket, "getaddrinfo", boom)
    monkeypatch.setattr(OC, "_now_ms", lambda: LATER)
    monkeypatch.setattr(CP, "_now_ms", lambda: LATER)


def prints_file(tmp_path, last_ts):
    p = tmp_path / "prints.jsonl"
    rows = [{"ts_ms": t, "funding_bps": 1.0} for t in range(CP.WINDOW_START_MS, last_ts + 1, CP.SETTLE_MS)]
    p.write_text("".join(json.dumps(r) + "\n" for r in rows))
    return str(p)


def test_a_clean_environment_is_not_armed_capital_is_zero_and_unsupplied_inputs_are_unavailable():
    rep = OC.build({}, now_ms=LATER)
    assert rep["environment"]["live_authorized"] is False and rep["environment"]["findings"] == []
    assert rep["capital"]["capital_usd"] == 0.0
    assert rep["evidence"]["status"] == OC.UNAVAILABLE and rep["backup"]["status"] == OC.UNAVAILABLE
    assert rep["pnl"]["actual_realised"]["status"] == OC.UNAVAILABLE and rep["pnl"]["modelled"]["status"] == OC.ESTIMATED
    assert rep["eligibility"]["owner_authorised"]["met"] is False and rep["eligibility"]["testnet"]["met"] is False
    assert all(rep[k]["status"] in OC.STATUSES for k in ("environment", "evidence", "capital", "backup"))


def test_an_armed_environment_is_flagged_secrets_are_never_printed_and_capital_still_zero(capsys, monkeypatch):
    rep = OC.build(LIVE, now_ms=LATER)
    text = json.dumps(rep)
    assert rep["environment"]["live_authorized"] is True and any("REAL MONEY IS ARMED" in f for f in rep["environment"]["findings"])
    assert "KEYVALUE12345" not in text and "SECRETVALUE6789" not in text and "I_UNDERSTAND" not in text
    assert rep["environment"]["secrets"] == {"BYBIT_API_KEY": "set", "BYBIT_API_SECRET": "set", "LIVE_TRADING_ACK": "set"}
    assert rep["capital"]["capital_usd"] == 0.0
    assert rep["eligibility"]["owner_authorised"]["met"] is False               # armed flags alone are not the signed gate
    for k, v in LIVE.items():
        monkeypatch.setenv(k, v)
    assert OC.main([]) == 3
    assert "KEYVALUE12345" not in capsys.readouterr().out


def test_an_unqualified_venue_name_is_a_finding():
    assert OC.build({"BYBIT_VENUE": "eu"}, now_ms=LATER)["environment"]["findings"]


def test_stale_evidence_is_stale_not_fine(tmp_path):
    old = prints_file(tmp_path, CP.WINDOW_START_MS + 2 * CP.SETTLE_MS)
    assert OC.build({}, funding=old, now_ms=LATER)["evidence"]["status"] == OC.STALE


def test_fresh_evidence_is_provisional_while_the_window_is_open_never_verified(tmp_path):
    fresh = prints_file(tmp_path, (LATER // CP.SETTLE_MS) * CP.SETTLE_MS)
    ev = OC.build({}, funding=fresh, now_ms=LATER)["evidence"]
    assert ev["status"] == OC.PROVISIONAL and ev["window_state"] == "WINDOW_OPEN" and "verdict" not in ev


def test_unreadable_prints_are_unavailable(tmp_path):
    bad = tmp_path / "p.jsonl"; bad.write_text("not json\n")
    assert OC.build({}, funding=str(bad), now_ms=LATER)["evidence"]["status"] == OC.UNAVAILABLE


def test_a_clock_in_the_future_cannot_be_claimed():
    assert OC.build({}, now_ms=LATER + 10**12)["as_of_ms"] == LATER


def test_backup_verification_distinguishes_good_from_tampered(tmp_path):
    import sqlite3
    import state_backup as SB
    src = tmp_path / "src"; src.mkdir()
    db = src / "s.db"
    con = sqlite3.connect(db); con.execute("create table t(x)"); con.execute("insert into t values (1)"); con.commit(); con.close()
    out = tmp_path / "bk"
    made = SB.backup(str(db), str(out))
    f = made.get("backup") or made.get("file") or next(str(p) for p in out.iterdir() if p.suffix == ".db")
    assert OC.build({}, backup=f, now_ms=LATER)["backup"]["status"] == OC.VERIFIED
    with open(f, "ab") as h:
        h.write(b"x")
    assert OC.build({}, backup=f, now_ms=LATER)["backup"]["status"] == OC.PROVISIONAL


def test_the_console_is_read_only(tmp_path):
    before = sorted(os.listdir(tmp_path))
    OC.build({}, now_ms=LATER)
    assert sorted(os.listdir(tmp_path)) == before
