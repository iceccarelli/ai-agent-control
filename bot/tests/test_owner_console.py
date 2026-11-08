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

pytestmark = pytest.mark.real_entry_ledger      # the console runs the drill's real entry-limiter probe

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
    assert rep["environment"]["secrets"] == {"BYBIT_API_KEY": "set", "BYBIT_API_SECRET": "set", "LIVE_TRADING_ACK": "set", "OWNER_ATTESTATION_KEY": "unset"}
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


def test_the_console_reports_the_offline_testnet_qualification_but_never_certifies():
    rep = OC.build({}, now_ms=LATER)
    t = rep["testnet"]
    assert t["status"] in OC.STATUSES and t["testnet_certified"] is False and t["venue_facts_not_read"] >= 10
    assert t["offline_qualification"] in ("PASS", "FAIL") and "testnet_offline=" in rep["headline"]
    armed = OC.build(LIVE, now_ms=LATER)["testnet"]
    assert armed["offline_qualification"] == "FAIL" and armed["failing_probes"]


def test_the_console_shows_the_risk_policy_as_proposed_and_not_active():
    r = OC.build({}, now_ms=LATER)["risk"]
    assert r["status"] == OC.ESTIMATED and r["policy_active"] is False and r["evaluation_without_an_approved_policy"] == ("NO_APPROVED_POLICY",)
    assert "OWNER DECISION REQUIRED" in r["decision"] and "per_trade_loss_usd" in r["unmeasured_limits"]
    assert 0.0 <= r["share_of_history_below_hurdle_requirement"] <= 1.0


def test_the_console_lists_every_candidate_with_a_verdict_and_none_is_promoted():
    s = OC.build({}, now_ms=LATER)["strategies"]
    assert s["status"] in OC.STATUSES and len(s["candidates"]) >= 9                       # the frozen BTC description + the 8 mechanism templates
    assert "btcusdt_funding_carry_overlay@prospective-1" in s["candidates"]
    assert set(s["verdict_counts"]) == {"UNTESTED"} and s["candidates"]["btcusdt_funding_carry_overlay@prospective-1"]["spec_problems"] == 2 and all(c["spec_problems"] > 0 for k, c in s["candidates"].items() if not k.startswith("btcusdt_"))   # the BTC description truthfully declares depth and basis/margin missing (2 problems); templates lack the data they name; none is registrable


def test_the_console_never_reports_a_verdict_above_untested_without_evidence():
    """No registry, key or anchor is read by the console: it cannot display PROMISING or above for anything."""
    s = OC.section_strategies()
    assert set(s["verdict_counts"]) == {"UNTESTED"}


def test_rt4_the_owner_attestation_key_is_reported_by_presence_and_flagged_on_an_app_host():
    key = "ownerkeyVALUE-0123456789-abcdefghijklmnop"
    on_own_machine = OC.build({"OWNER_ATTESTATION_KEY": key}, now_ms=LATER)["environment"]
    assert on_own_machine["secrets"]["OWNER_ATTESTATION_KEY"] == "set" and on_own_machine["findings"] == []        # positive control: no BYBIT_* here
    assert OC.build({}, now_ms=LATER)["environment"]["secrets"]["OWNER_ATTESTATION_KEY"] == "unset"
    for host in ({"BYBIT_API_KEY": "KEYVALUE12345"}, {"BYBIT_VENUE": "testnet"}, {"BYBIT_API_SECRET": "SECRETVALUE6789"}):
        rep = OC.build(dict(host, OWNER_ATTESTATION_KEY=key), now_ms=LATER)
        env = rep["environment"]
        assert any("OWNER_ATTESTATION_KEY" in f and "app host" in f for f in env["findings"]), host
        assert key not in json.dumps(rep) and env["status"] == OC.PROVISIONAL                                         # presence only, never the value
    assert not any("OWNER_ATTESTATION_KEY" in f for f in OC.build({"BYBIT_API_KEY": "KEYVALUE12345"}, now_ms=LATER)["environment"]["findings"])
