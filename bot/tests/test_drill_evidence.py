"""The carry drill can capture its venue traffic as hash-chained evidence.

Without this the drill's fills and funding settlements are only what the
drill reports about itself; with it they can be reconciled by
`tools/evidence_reconcile.py` like the directional round trip.
"""
from __future__ import annotations

import config as _config
import pytest



def _tools():
    import drill
    import venue_evidence
    return drill, venue_evidence


def _carry_env(monkeypatch, tmp_path, venue):
    env = {"BOOK_MODE": "carry", "CARRY_BORROW_APR": "0.0",
           "CARRY_EXECUTION_MODE": "overlay", "BYBIT_VENUE": venue,
           "STATE_DB_PATH": str(tmp_path / "s.db")}
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(_config, "_CFG", _config.load(env))


def test_testnet_bot_client_sends_through_the_capturing_transport(
        monkeypatch, tmp_path):
    D, ve = _tools()
    _carry_env(monkeypatch, tmp_path, "testnet")
    bot = D._load_bot(str(tmp_path / "ev.jsonl"))
    assert isinstance(bot.client.transport, ve.EvidenceCapturingTransport)
    assert bot.carry is not None
    # the carry broker must share that very client, or its orders escape capture
    assert bot.engine.client is bot.client


def test_without_the_flag_nothing_is_wrapped(monkeypatch, tmp_path):
    D, ve = _tools()
    _carry_env(monkeypatch, tmp_path, "testnet")
    bot = D._load_bot()
    assert not isinstance(bot.client.transport, ve.EvidenceCapturingTransport)


def test_capture_refuses_a_non_testnet_venue(monkeypatch, tmp_path):
    D, ve = _tools()
    _carry_env(monkeypatch, tmp_path, "mainnet")
    with pytest.raises(RuntimeError, match="testnet venue only"):
        D._load_bot(str(tmp_path / "ev.jsonl"))
