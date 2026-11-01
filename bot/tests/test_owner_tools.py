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
    assert code == 3 and body["policy_inputs_complete"] is False
    assert len(body["missing_or_unusable"]) == 2


def test_a_complete_policy_exits_0():
    code, out = run(opc.main, env={
        "CARRY_EVIDENCE_DISCOVERY_START_EPOCH": "1000",
        "CARRY_EVIDENCE_DISCOVERY_END_EPOCH": "2000",
        "CARRY_EVIDENCE_DRAWDOWN_LIMIT_USD": "50"})
    assert code == 0 and json.loads(out)["policy_inputs_complete"] is True


def test_a_malformed_owner_setting_is_named_not_echoed():
    code, out = run(opc.main, env={"CARRY_EVIDENCE_DRAWDOWN_LIMIT_USD": "sekrit-x",
                                   "BYBIT_API_SECRET": "s3cr3t-value-123"})
    body = json.loads(out)
    assert code == 3 and "sekrit-x" not in out and "s3cr3t" not in out
    assert body["unparseable_settings"] == ["CARRY_EVIDENCE_DRAWDOWN_LIMIT_USD"]
    assert body["policy_inputs_complete"] is False


def test_a_bad_unrelated_setting_cannot_hide_the_owner_policy():
    code, out = run(opc.main, env={
        "ENABLE_HEALTH_SERVER": "banana-secret", "LOOP_INTERVAL_SECONDS": "zzz",
        "CARRY_EVIDENCE_DISCOVERY_START_EPOCH": "1000",
        "CARRY_EVIDENCE_DISCOVERY_END_EPOCH": "2000",
        "CARRY_EVIDENCE_DRAWDOWN_LIMIT_USD": "50"})
    assert code == 0 and "banana-secret" not in out
    assert json.loads(out)["policy_inputs_complete"] is True


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


def test_the_report_defaults_mode_and_symbols_from_the_environment(tmp_path):
    path = _db(tmp_path, 22)
    code, out = run(esr.main, ["--db", path, "--notional-usd", "600"],
                    env={"CARRY_EXECUTION_MODE": "overlay"})
    body = json.loads(out)
    assert code == 0 and [l["symbol"] for l in body["legs"]] == ["BTCUSDT", "BTCUSDT"]
    other = run(esr.main, ["--db", path, "--notional-usd", "600"],
                env={"CARRY_EXECUTION_MODE": "overlay", "CARRY_PERP_SYMBOL": "ETHUSDT"})
    assert other[0] == 3                           # that symbol has no samples here


def test_the_report_demands_a_mode_and_a_sane_notional(tmp_path):
    path = _db(tmp_path, 3)
    for argv, env in ((["--db", path, "--notional-usd", "600"], {}),
                      (["--db", path, "--mode", "overlay", "--notional-usd", "0"], {}),
                      (["--db", path, "--mode", "overlay", "--notional-usd", "-5"], {}),
                      (["--db", path, "--mode", "overlay", "--notional-usd", "nan"], {})):
        with pytest.raises(SystemExit) as e:
            esr.main(argv, env=env)
        assert e.value.code == 2


def test_any_database_failure_is_exit_2_not_a_traceback(tmp_path):
    bad = tmp_path / "junk.db"
    bad.write_bytes(b"this is not sqlite" * 50)
    code, out = run(esr.main, ["--db", str(bad), "--mode", "overlay",
                               "--notional-usd", "600"])
    assert code == 2 and "error" in json.loads(out)


def test_a_literal_word_in_a_policy_setting_is_named_not_a_traceback():
    code, out = run(opc.main, env={"CARRY_EVIDENCE_DISCOVERY_START_EPOCH": "START",
                                   "CARRY_EVIDENCE_DISCOVERY_END_EPOCH": "END",
                                   "CARRY_EVIDENCE_DRAWDOWN_LIMIT_USD": "LIMIT"})
    body = json.loads(out)
    assert code == 3 and "START'" not in out and "LIMIT'" not in out
    assert sorted(body["unparseable_settings"]) == [
        "CARRY_EVIDENCE_DISCOVERY_END_EPOCH",
        "CARRY_EVIDENCE_DISCOVERY_START_EPOCH",
        "CARRY_EVIDENCE_DRAWDOWN_LIMIT_USD"]


def test_the_tool_survives_a_process_whose_environment_config_would_reject():
    """Run as a real process: importing config validates every setting at import,
    which this tool must not do. A bad owner value exits 3 without a traceback."""
    import subprocess
    env = dict(os.environ, CARRY_EVIDENCE_DISCOVERY_START_EPOCH="START",
               LOOP_INTERVAL_SECONDS="zzz")
    r = subprocess.run([sys.executable, os.path.join(BOT, "tools",
                                                     "owner_policy_check.py")],
                       capture_output=True, text=True, env=env, timeout=60)
    assert r.returncode == 3, (r.returncode, r.stderr[-300:])
    assert "Traceback" not in r.stderr and "START'" not in r.stdout + r.stderr


@pytest.mark.parametrize("raw", ["", "  ", "5", " 5.5 ", "-3", "0", "nan", "inf",
                                 "x", "1e3", "True"])
@pytest.mark.parametrize("positive", [False, True])
def test_the_local_parser_agrees_with_the_configs(raw, positive):
    import config as _config

    def local():
        try:
            return ("ok", opc._parse_optional({"K": raw}, "K", positive))
        except ValueError:
            return ("bad", None)

    def official():
        try:
            return ("ok", _config.parse_optional_float({"K": raw}, "K",
                                                       positive=positive))
        except _config.ConfigError:
            return ("bad", None)
    assert local() == official()


# -- drill execution records -> samples report ---------------------------------------

def _journal_with_fills(n, tag="x"):
    import ledger as L
    import time
    j = L.Journal()
    base = int(time.time() * 1000) - 86_400_000

    def rec(side):
        return {"schema": 1, "status": "MEASURED",
                "reference_basis": "executable_side", "symbol": "BTCUSDT",
                "product": "linear", "side": side, "order_type": "market",
                "filled_qty": 0.01, "requested_qty": 0.01, "avg_price": 60000.0,
                "reference_price": 60000.0, "slippage_usd": 0.1,
                "spread_cost_usd": 0.3, "decision_to_submit_ms": 5,
                "submit_to_fill_ms": 40}
    for i in range(n):
        L.perp_open(j, ms=base + 2000 * i, ref=f"BB-{tag}o-{i}", qty=0.01,
                    price=60000.0, fee_usd=0.2, fee_status="confirmed",
                    fee_raw={"execution": rec("Sell")})
        L.perp_close(j, ms=base + 2000 * i + 1000, ref=f"BB-{tag}c-{i}",
                     qty=0.01, price=60000.0, entry_price=60000.0, fee_usd=0.2,
                     fee_status="confirmed", fee_raw={"execution": rec("Buy")})
    return j


def test_drill_records_accumulate_and_are_counted_per_environment(tmp_path):
    import drill
    path = str(tmp_path / "execution_records.jsonl")
    written = set()
    j = _journal_with_fills(12)
    assert drill.append_execution_records(path, j, "testnet", written) == 24
    # the same journal written again appends nothing
    assert drill.append_execution_records(path, j, "testnet", written) == 0
    # a second drill run (new process, fresh `written`) adds its own fills
    j2 = _journal_with_fills(12, tag="y")
    assert drill.append_execution_records(path, j2, "testnet", set()) == 24
    code, out = run(esr.main, ["--records", path, "--environment", "testnet",
                               "--mode", "overlay", "--notional-usd", "600"])
    body = json.loads(out)
    assert code == 0 and body["status"] == "MEASURED", body
    # none of those testnet fills count as mainnet samples
    code, out = run(esr.main, ["--records", path, "--environment", "mainnet",
                               "--mode", "overlay", "--notional-usd", "600"])
    body = json.loads(out)
    assert code == 3 and body["records_skipped"] == 48


def test_records_need_an_environment_and_bad_lines_are_not_samples(tmp_path):
    import pytest
    path = tmp_path / "r.jsonl"
    path.write_text("not json\n{\"schema\": \"other\"}\n\n")
    with pytest.raises(SystemExit):
        run(esr.main, ["--records", str(path), "--mode", "overlay",
                       "--notional-usd", "600"])
    code, out = run(esr.main, ["--records", str(path), "--environment",
                               "testnet", "--mode", "overlay",
                               "--notional-usd", "600"])
    body = json.loads(out)
    assert code == 3 and body["records_skipped"] == 2


def test_db_and_records_merge(tmp_path):
    import drill
    db = _db(tmp_path, 12)
    path = str(tmp_path / "r.jsonl")
    drill.append_execution_records(path, _journal_with_fills(12, "z"),
                                   "testnet", set())
    code, out = run(esr.main, ["--db", db, "--records", path, "--environment",
                               "testnet", "--mode", "overlay",
                               "--notional-usd", "600"])
    assert code == 0 and json.loads(out)["status"] == "MEASURED"
