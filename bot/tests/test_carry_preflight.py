"""The testnet preflight reads what the carry book reads and can send nothing."""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from carry_broker import CarryBroker, CarryOrderRefused  # noqa: E402
from test_carry_broker import FakeClient, envelope  # noqa: E402
from tools import carry_preflight as pf  # noqa: E402


class Venue(FakeClient):
    def __init__(self, open_orders=(), unreadable=(), **kw):
        super().__init__(**kw)
        self._open = list(open_orders)
        self._unreadable = set(unreadable)

    def _request(self, method, endpoint, **kw):
        if endpoint in self._unreadable:
            raise RuntimeError(f"{endpoint} down")
        if endpoint == "/v5/order/realtime":
            self.calls.append((method, endpoint, kw.get("params"), None))
            return envelope({"list": self._open})
        if endpoint == "/v5/account/wallet-balance":
            self.calls.append((method, endpoint, kw.get("params"), None))
            return envelope({"list": [{"coin": [{
                "coin": "BTC", "walletBalance": str(self.spot_qty),
                "locked": "0"}]}]})
        return super()._request(method, endpoint, **kw)


def run(venue, *, paper=False, mode="overlay", known=0.0, equity=9776.0):
    broker = CarryBroker(client=venue, sequence_source=lambda *a: 0,
                         order_gate=lambda: (False, "preflight is read-only"))
    cfg = SimpleNamespace(PAPER_TRADING=paper, USE_TESTNET=True,
                          BYBIT_VENUE="testnet")
    client = SimpleNamespace(get_equity=lambda: equity)
    report = pf.run_preflight(
        broker=broker, client=client, cfg=cfg, spot_symbol="BTCUSDT",
        perp_symbol="BTCUSDT", execution_mode=mode, known_perp_qty=known)
    return report, venue


def posts(venue):
    return [c for c in venue.calls if c[0] == "POST"]


def test_a_clean_flat_funded_account_passes_and_sends_nothing():
    report, venue = run(Venue(perp_qty=0.0, spot_qty=1.0))
    assert report["verdict"] == "PREFLIGHT_OK", report["blockers"]
    assert report["order_gate_would_allow_orders"] is True
    assert posts(venue) == []


def test_paper_mode_is_reported_as_placing_nothing():
    report, _ = run(Venue(perp_qty=0.0, spot_qty=1.0), paper=True)
    assert report["verdict"] == "PREFLIGHT_OK"
    assert report["order_gate_would_allow_orders"] is False
    assert any("PAPER_TRADING=1" in n for n in report["notes"])


def test_a_venue_position_the_ledger_does_not_know_blocks():
    report, _ = run(Venue(perp_qty=0.004, spot_qty=1.0))
    assert report["verdict"] == "PREFLIGHT_BLOCKED"
    assert any("mismatch" in b for b in report["blockers"])


def test_the_same_position_is_fine_when_the_ledger_knows_it():
    report, _ = run(Venue(perp_qty=0.004, spot_qty=1.0), known=0.004)
    assert report["verdict"] == "PREFLIGHT_OK", report["blockers"]


def test_an_open_carry_order_blocks():
    open_order = {"orderLinkId": "x-carry-1", "orderStatus": "New"}
    report, _ = run(Venue(open_orders=[open_order], spot_qty=1.0))
    assert report["verdict"] == "PREFLIGHT_BLOCKED"
    assert any("open carry order" in b for b in report["blockers"])


def test_overlay_without_enough_btc_blocks_and_never_suggests_selling_it():
    report, _ = run(Venue(perp_qty=0.0, spot_qty=0.0000001))
    assert report["verdict"] == "PREFLIGHT_BLOCKED"
    assert any("will not buy or sell your spot" in b for b in report["blockers"])


@pytest.mark.parametrize("endpoint", [
    "/v5/position/list", "/v5/account/wallet-balance", "/v5/account/fee-rate",
    "/v5/market/instruments-info", "/v5/market/tickers"])
def test_an_unreadable_venue_is_a_blocker_never_an_assumption(endpoint):
    report, _ = run(Venue(unreadable=[endpoint], spot_qty=1.0))
    assert report["verdict"] == "PREFLIGHT_BLOCKED"
    assert any("unreadable" in b for b in report["blockers"])


def test_the_broker_it_uses_cannot_place_an_order_even_if_asked():
    broker = CarryBroker(client=Venue(), sequence_source=lambda *a: 0,
                         order_gate=lambda: (False, "preflight is read-only"))
    with pytest.raises(CarryOrderRefused):
        broker.place_market(symbol="BTCUSDT", side="Buy", qty=0.001,
                            product="spot")


@pytest.fixture()
def clean_env(monkeypatch):
    """Isolate from the ambient shell: a Codespace may already export
    BYBIT_VENUE, PAPER_TRADING, LIVE_TRADING_ACK and friends."""
    for k in ("BYBIT_VENUE", "USE_TESTNET", "PAPER_TRADING", "LIVE_TRADING_ACK",
              "BOOK_MODE", "CARRY_EXECUTION_MODE", "CARRY_EXECUTION_STYLE",
              "CARRY_BORROW_APR"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("BYBIT_API_KEY", "k")
    monkeypatch.setenv("BYBIT_API_SECRET", "s")
    return monkeypatch


def test_main_refuses_without_testnet(clean_env, capsys):
    clean_env.setenv("USE_TESTNET", "0")
    assert pf.main([]) == 3
    assert "PREFLIGHT_REFUSED" in capsys.readouterr().out


def test_main_refuses_a_contradictory_environment_with_a_reason(clean_env, capsys):
    """The exact state a Codespace produced: BYBIT_VENUE=testnet exported, then
    USE_TESTNET=0 set on top. Two sources of truth about the exchange."""
    clean_env.setenv("BYBIT_VENUE", "testnet")
    clean_env.setenv("USE_TESTNET", "0")
    assert pf.main([]) == 3
    out = capsys.readouterr().out
    assert "PREFLIGHT_REFUSED" in out and "disagree" in out


def test_main_refuses_when_a_live_authorisation_is_present(clean_env, capsys):
    clean_env.setenv("USE_TESTNET", "1")
    clean_env.setenv("LIVE_TRADING_ACK", "I_UNDERSTAND")
    assert pf.main([]) == 3
    assert "live authorisation" in capsys.readouterr().out


def test_the_real_command_line_refuses_a_contradictory_environment_cleanly():
    """A subprocess, because `config` validates the environment at IMPORT time
    and an in-process test has already imported it with a clean one."""
    import json
    import subprocess

    env = {"PATH": os.environ["PATH"], "HOME": "/tmp",
           "BYBIT_VENUE": "testnet", "USE_TESTNET": "0",
           "BYBIT_API_KEY": "k", "BYBIT_API_SECRET": "s"}
    proc = subprocess.run(
        [sys.executable, os.path.join(os.path.dirname(HERE), "tools",
                                      "carry_preflight.py")],
        env=env, capture_output=True, text=True, timeout=60)
    assert proc.returncode == 3, proc.stderr
    out = json.loads(proc.stdout)
    assert out["verdict"] == "PREFLIGHT_REFUSED" and "disagree" in out["reason"]
    assert "Traceback" not in proc.stderr
