"""The two read-only owner tools: they report, they suggest nothing, they send
nothing and they leak no setting."""
from __future__ import annotations

import io
import json
import os
import sys
from contextlib import redirect_stdout

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
BOT = os.path.dirname(HERE)
sys.path.insert(0, BOT)
sys.path.insert(0, os.path.join(BOT, "tools"))

import execution_samples_report as esr                       # noqa: E402
import owner_policy_check as opc                              # noqa: E402

NOW = 2_000_000_000.0


def run(main, *args, **kw):
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = main(*args, **kw)
    return code, buf.getvalue()


# -- owner_policy_check --------------------------------------------------------------

def test_nothing_set_exits_3_and_lists_what_is_missing():
    code, out = run(opc.main, env={})
    body = json.loads(out)
    assert code == 3 and body["evidence_can_qualify"] is False
    assert len(body["missing_or_unusable"]) == 2


def test_a_complete_policy_exits_0():
    code, out = run(opc.main, env={
        "CARRY_EVIDENCE_DISCOVERY_START_EPOCH": "1000",
        "CARRY_EVIDENCE_DISCOVERY_END_EPOCH": "2000",
        "CARRY_EVIDENCE_DRAWDOWN_LIMIT_USD": "50"})
    assert code == 0 and json.loads(out)["evidence_can_qualify"] is True


def test_a_bad_setting_exits_2_without_echoing_other_settings():
    code, out = run(opc.main, env={"CARRY_EVIDENCE_DRAWDOWN_LIMIT_USD": "x",
                                   "BYBIT_API_SECRET": "s3cr3t-value-123"})
    assert code == 2 and "s3cr3t" not in out


def test_the_output_never_contains_credentials():
    code, out = run(opc.main, env={"BYBIT_API_KEY": "k" * 18,
                                   "BYBIT_API_SECRET": "TOPSECRET" * 4})
    assert "TOPSECRET" not in out and "k" * 18 not in out


def test_the_tool_opens_no_network_and_no_database():
    src = open(os.path.join(BOT, "tools", "owner_policy_check.py")).read()
    for word in ("socket", "requests", "sqlite3", "StateStore", "BybitClient",
                 "urllib"):
        assert word not in src


# -- execution_samples_report --------------------------------------------------------

def _db(tmp_path, n):
    import ledger as L
    from persistence import StateStore
    path = str(tmp_path / "s.db")
    store = StateStore(path)
    j = L.Journal()
    import time
    base = int(time.time() * 1000) - 86_400_000

    def rec(side, i):
        return {"schema": 1, "status": "MEASURED",
                "reference_basis": "executable_side", "symbol": "BTCUSDT",
                "product": "linear", "side": side, "order_type": "market",
                "filled_qty": 0.01, "requested_qty": 0.01, "avg_price": 60000.0,
                "reference_price": 60000.0, "slippage_usd": 0.1,
                "spread_cost_usd": 0.3, "decision_to_submit_ms": 5,
                "submit_to_fill_ms": 40}
    for i in range(n):
        L.perp_open(j, ms=base + 2000 * i, ref=f"BB-o-{i}", qty=0.01, price=60000.0,
                    fee_usd=0.2, fee_status="confirmed",
                    fee_raw={"execution": rec("Sell", i)})
        L.perp_close(j, ms=base + 2000 * i + 1000, ref=f"BB-c-{i}", qty=0.01,
                     price=60000.0, entry_price=60000.0, fee_usd=0.2,
                     fee_status="confirmed", fee_raw={"execution": rec("Buy", i)})
    store.save_ledger(j.to_rows())
    store.close()
    return path


def test_the_report_names_the_shortfall_per_leg(tmp_path):
    path = _db(tmp_path, 5)
    code, out = run(esr.main, ["--db", path, "--mode", "overlay",
                               "--notional-usd", "600"])
    body = json.loads(out)
    assert code == 3 and body["status"] == "UNAVAILABLE"
    assert body["shortfall"]["linear/Sell/market"] == 15


def test_enough_samples_exit_0(tmp_path):
    path = _db(tmp_path, 22)
    code, out = run(esr.main, ["--db", path, "--mode", "overlay",
                               "--notional-usd", "600"])
    body = json.loads(out)
    assert code == 0 and body["status"] == "MEASURED"
    assert set(body["shortfall"].values()) == {0}


def test_it_is_read_only_and_never_creates_a_database(tmp_path):
    missing = str(tmp_path / "nope.db")
    code, out = run(esr.main, ["--db", missing, "--mode", "overlay",
                               "--notional-usd", "600"])
    assert code == 2 and not os.path.exists(missing)
    path = _db(tmp_path, 3)
    before = open(path, "rb").read()
    run(esr.main, ["--db", path, "--mode", "overlay", "--notional-usd", "600"])
    assert open(path, "rb").read() == before


def test_a_database_with_no_ledger_is_an_error_not_a_pass(tmp_path):
    from persistence import StateStore
    path = str(tmp_path / "e.db")
    StateStore(path).close()
    code, out = run(esr.main, ["--db", path, "--mode", "overlay",
                               "--notional-usd", "600"])
    assert code == 2 and "error" in json.loads(out)
