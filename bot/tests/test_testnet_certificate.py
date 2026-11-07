"""testnet_certificate: the offline probes catch a broken guard, and the certificate can never claim a testnet is certified."""
from __future__ import annotations

import os
import socket
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
BOT = os.path.dirname(HERE)
sys.path.insert(0, BOT)
sys.path.insert(0, os.path.join(BOT, "tools"))

import drill as D  # noqa: E402

pytestmark = pytest.mark.real_entry_ledger      # these tests exercise the drill's REAL entry limiter, not conftest's per-test substitute
import testnet_certificate as TC  # noqa: E402


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def boom(*_a, **_k):
        raise AssertionError("network touched")
    monkeypatch.setattr(socket.socket, "connect", boom)
    monkeypatch.setattr(socket, "getaddrinfo", boom)


def by_name(rep):
    return {p["probe"]: p["status"] for p in rep["section_a_offline_probes"]}


def test_a_clean_environment_passes_every_offline_probe_but_is_never_certified():
    rep = TC.build({})
    assert rep["offline_qualification"] == TC.PASS and all(v == TC.PASS for v in by_name(rep).values())
    assert rep["testnet_certified"] is False and "section B" in rep["why_not_certified"]
    assert all(f["status"] == TC.NOT_READ for f in rep["section_b_venue_facts"]) and len(rep["section_b_venue_facts"]) >= 10


def test_an_armed_environment_fails_the_live_gate_probe():
    env = {"USE_TESTNET": "0", "PAPER_TRADING": "0", "LIVE_TRADING_ACK": "I_UNDERSTAND", "BYBIT_API_KEY": "k" * 12, "BYBIT_API_SECRET": "s" * 12, "BYBIT_VENUE": "mainnet"}
    rep = TC.build(env)
    assert rep["offline_qualification"] == TC.FAIL
    assert by_name(rep)["live gate is closed in this environment"] == TC.FAIL and by_name(rep)["LIVE_TRADING_ACK is unset in this environment"] == TC.FAIL
    assert "k" * 12 not in str(rep) and "I_UNDERSTAND" not in str(rep)


@pytest.mark.parametrize("broken,probe", [
    ("venue_refusal", "drill refuses mainnet"),
    ("kill_file_engaged", "kill file present = engaged"),
    ("claim_entry_slot", "entry limiter grants one armed entry per UTC day and refuses the second"),
])
def test_a_weakened_guard_turns_the_matching_probe_red(monkeypatch, broken, probe):
    if broken == "venue_refusal":
        monkeypatch.setattr(D, "venue_refusal", lambda cfg, client=None: None)
    elif broken == "kill_file_engaged":
        monkeypatch.setattr(D, "kill_file_engaged", lambda path: (False, ""))
    else:
        monkeypatch.setattr(D, "claim_entry_slot", lambda path, now, max_per_day=None: (True, "always"))
    rep = TC.build({})
    assert rep["offline_qualification"] == TC.FAIL and by_name(rep)[probe] == TC.FAIL


def test_the_plan_is_bounded_by_the_existing_hundred_dollar_cap_and_authorisation_is_not_granted():
    import shadow
    plan = TC.build({})["section_c_plan"]
    assert plan["max_notional_usd"] == float(shadow.SHADOW_MAX_NOTIONAL_USD) == 100.0 and plan["max_entries"] == 1
    assert plan["expected_fees_usd"]["overlay_round_trip"] == pytest.approx(0.11) and plan["expected_fees_usd"]["acquire_round_trip"] == pytest.approx(0.31)
    assert "NOT GRANTED" in plan["authorisation"] and "ASSUMED" in plan["fee_inputs"]


def test_main_exit_code_follows_the_offline_verdict(capsys):
    assert TC.main([]) == 0
