"""tools/testnet_conformance_run.py: fail-closed preflight, and the bounded
run's own stage sequencing.

Scope, deliberately: `TradingEngine.execute()`'s own correctness for a real
linear order/fill/protective-stop sequence is already proven elsewhere
(tests/test_naked_position_handling.py, tests/test_linear_stop_venue_drill.py,
tests/test_lifecycle_integration.py) — this file does not re-derive linear
sizing/margin/filters to duplicate that. What is new here, and what this file
actually tests, is `run_preflight`'s five independent checks and
`run_conformance`'s own stage sequencing (does it stop at the first failed
stage, does it read back protection from the venue rather than trusting the
local ledger, does it compute latencies) — using small stand-ins that expose
exactly the surface those functions call, not a full linear FakeBybit stack.
"""
from __future__ import annotations

import os
import sys
import types

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "tools"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import testnet_conformance_run as tcr  # noqa: E402


def _cfg(**over):
    base = {"BYBIT_VENUE": "testnet", "PAPER_TRADING": False,
           "BYBIT_API_KEY": "key123", "BYBIT_API_SECRET": "secret123"}
    base.update(over)
    return types.SimpleNamespace(**base)


class _Client:
    def __init__(self, *, base_url="https://api-testnet.bybit.com",
                sync_error=None, wallet=None):
        self.base_url = base_url
        self._sync_error = sync_error
        self._wallet = {"accountType": "UNIFIED"} if wallet is None else wallet

    def sync_time(self):
        if self._sync_error:
            raise self._sync_error
        return 0

    def get_wallet(self):
        return self._wallet


class TestPreflightIndividualChecks:
    def test_verify_environment_accepts_testnet_paper_off(self):
        tcr.verify_environment(_cfg())

    def test_verify_environment_refuses_mainnet(self):
        with pytest.raises(tcr.ConformanceRefused, match="mainnet"):
            tcr.verify_environment(_cfg(BYBIT_VENUE="mainnet"))

    def test_verify_environment_refuses_demo(self):
        with pytest.raises(tcr.ConformanceRefused):
            tcr.verify_environment(_cfg(BYBIT_VENUE="demo"))

    def test_verify_environment_refuses_unset_venue(self):
        with pytest.raises(tcr.ConformanceRefused):
            tcr.verify_environment(_cfg(BYBIT_VENUE=""))

    def test_verify_environment_refuses_paper_mode(self):
        with pytest.raises(tcr.ConformanceRefused, match="PAPER_TRADING"):
            tcr.verify_environment(_cfg(PAPER_TRADING=True))

    def test_verify_credentials_accepts_both_present(self):
        tcr.verify_credentials(_cfg())

    def test_verify_credentials_refuses_missing_key(self):
        with pytest.raises(tcr.ConformanceRefused):
            tcr.verify_credentials(_cfg(BYBIT_API_KEY=""))

    def test_verify_credentials_refuses_missing_secret(self):
        with pytest.raises(tcr.ConformanceRefused):
            tcr.verify_credentials(_cfg(BYBIT_API_SECRET=""))

    def test_verify_venue_identity_accepts_testnet_host(self):
        tcr.verify_venue_identity(_Client())

    def test_verify_venue_identity_refuses_mainnet_host(self):
        """The exact law: a config claim of testnet is not trusted over the
        client's actual base URL."""
        with pytest.raises(tcr.ConformanceRefused, match="mainnet"):
            tcr.verify_venue_identity(
                _Client(base_url="https://api.bybit.com"))

    def test_verify_venue_identity_refuses_unrecognized_host(self):
        with pytest.raises(tcr.ConformanceRefused):
            tcr.verify_venue_identity(_Client(base_url="https://example.com"))

    def test_verify_connectivity_accepts_reachable(self):
        tcr.verify_connectivity(_Client())

    def test_verify_connectivity_refuses_on_exception(self):
        with pytest.raises(tcr.ConformanceRefused, match="unreachable"):
            tcr.verify_connectivity(
                _Client(sync_error=ConnectionError("no route to host")))

    def test_verify_account_state_accepts_nonempty_wallet(self):
        tcr.verify_account_state(_Client())

    def test_verify_account_state_refuses_empty_wallet(self):
        with pytest.raises(tcr.ConformanceRefused):
            tcr.verify_account_state(_Client(wallet={}))


class TestRunPreflightAggregation:
    def test_all_checks_pass(self):
        result = tcr.run_preflight(_cfg(), _Client())
        assert result.ok is True
        assert all(v == "ok" for v in result.checks.values())
        assert set(result.checks) == {c[0] for c in tcr.PREFLIGHT_CHECKS}

    def test_every_check_runs_even_after_an_earlier_failure(self):
        """An operator refused at 'credentials' should see every other
        problem too, not fix one and discover the next on a second run."""
        result = tcr.run_preflight(
            _cfg(BYBIT_API_KEY="", BYBIT_VENUE="mainnet"),
            _Client(sync_error=ConnectionError("down")))
        assert result.ok is False
        assert result.checks["environment"] != "ok"
        assert result.checks["credentials"] != "ok"
        assert result.checks["connectivity"] != "ok"
        # venue_identity and account_state still ran and recorded something:
        assert "venue_identity" in result.checks
        assert "account_state" in result.checks

    def test_single_failure_is_isolated(self):
        result = tcr.run_preflight(_cfg(), _Client(wallet={}))
        assert result.ok is False
        assert result.checks["environment"] == "ok"
        assert result.checks["credentials"] == "ok"
        assert result.checks["venue_identity"] == "ok"
        assert result.checks["connectivity"] == "ok"
        assert result.checks["account_state"] != "ok"


class _StubStore:
    def __init__(self, positions=None, naked=None):
        self._positions = positions or []
        self._naked = naked if naked is not None else []

    def open_positions(self):
        return self._positions

    def positions_without_stops(self):
        return self._naked


class _StubReport:
    def __init__(self, ok=True, reason="OK"):
        self.ok = ok
        self.reason = reason


class _StubEngine:
    def __init__(self, close_ok=True):
        self.close_ok = close_ok
        self.closed = []

    def close_position(self, *, symbol, reason):
        self.closed.append(symbol)
        return _StubReport(ok=self.close_ok, reason="CLOSED")


class _StubClient:
    def __init__(self, *, remote_stop=100.0, reconcile_summary=None,
                get_position_error=None, reconcile_error=None):
        self.remote_stop = remote_stop
        self._reconcile_summary = reconcile_summary or {
            "unknown": 0, "naked_positions": []}
        self._get_position_error = get_position_error
        self._reconcile_error = reconcile_error

    def get_position(self, symbol):
        if self._get_position_error:
            raise self._get_position_error
        return {"symbol": symbol, "stopLoss": str(self.remote_stop)}

    def reconcile_on_startup(self):
        if self._reconcile_error:
            raise self._reconcile_error
        return self._reconcile_summary


class _StubBot:
    def __init__(self, *, startup_ok=True, tick_error=None):
        self.startup_ok = startup_ok
        self.tick_error = tick_error
        self.ticked = False

    def startup(self):
        return self.startup_ok

    def tick(self):
        if self.tick_error:
            raise self.tick_error
        self.ticked = True


SYMBOL = "BTCUSDT"


class TestRunConformanceStageSequencing:
    def test_happy_path_runs_every_stage_and_is_ok(self):
        store = _StubStore(positions=[{"symbol": SYMBOL, "qty": 0.01}])
        engine = _StubEngine()
        client = _StubClient()
        bot = _StubBot()

        result = tcr.run_conformance(
            bot=bot, engine=engine, client=client, store=store, symbol=SYMBOL)

        assert result["ok"] is True
        names = [s["stage"] for s in result["stages"]]
        assert names == ["startup", "entry", "protection_local",
                        "protection_readback", "flatten",
                        "final_reconciliation"]
        assert all(s["ok"] for s in result["stages"])
        assert engine.closed == [SYMBOL]
        assert result["latencies_ms"], "no latencies computed"

    def test_stops_at_startup_failure(self):
        bot = _StubBot(startup_ok=False)
        result = tcr.run_conformance(
            bot=bot, engine=_StubEngine(), client=_StubClient(),
            store=_StubStore(), symbol=SYMBOL)
        assert result["ok"] is False
        assert [s["stage"] for s in result["stages"]] == ["startup"]

    def test_stops_at_entry_failure_when_no_position_opened(self):
        """tick() ran but no position exists -- the order never landed (or
        was refused by a gate). Nothing past 'entry' should run."""
        result = tcr.run_conformance(
            bot=_StubBot(), engine=_StubEngine(), client=_StubClient(),
            store=_StubStore(positions=[]), symbol=SYMBOL)
        assert result["ok"] is False
        assert [s["stage"] for s in result["stages"]] == ["startup", "entry"]

    def test_stops_at_protection_local_failure(self):
        store = _StubStore(positions=[{"symbol": SYMBOL}],
                           naked=[{"symbol": SYMBOL}])
        result = tcr.run_conformance(
            bot=_StubBot(), engine=_StubEngine(), client=_StubClient(),
            store=store, symbol=SYMBOL)
        assert result["ok"] is False
        assert [s["stage"] for s in result["stages"]] == [
            "startup", "entry", "protection_local"]

    def test_stops_when_venue_readback_disagrees_with_local_belief(self):
        """The whole point of protection_readback: the LOCAL ledger says
        protected, but the VENUE's own answer (queried fresh) says
        otherwise -- this must be treated as unprotected, not glossed
        over because the local state looked fine."""
        store = _StubStore(positions=[{"symbol": SYMBOL}], naked=[])
        client = _StubClient(remote_stop=0.0)
        result = tcr.run_conformance(
            bot=_StubBot(), engine=_StubEngine(), client=client,
            store=store, symbol=SYMBOL)
        assert result["ok"] is False
        assert [s["stage"] for s in result["stages"]] == [
            "startup", "entry", "protection_local", "protection_readback"]

    def test_protection_readback_error_is_a_failed_stage_not_an_exception(self):
        store = _StubStore(positions=[{"symbol": SYMBOL}], naked=[])
        client = _StubClient(get_position_error=RuntimeError("venue down"))
        result = tcr.run_conformance(
            bot=_StubBot(), engine=_StubEngine(), client=client,
            store=store, symbol=SYMBOL)
        assert result["ok"] is False
        assert result["stages"][-1]["stage"] == "protection_readback"
        assert "error" in result["stages"][-1]

    def test_flatten_failure_still_records_final_reconciliation(self):
        """close_position() failing does not stop the run early -- the
        engine's own close_position already fails closed internally; this
        tool's job is to record what happened, including a failed flatten,
        not to hide it by stopping before reconciliation runs."""
        store = _StubStore(positions=[{"symbol": SYMBOL}], naked=[])
        engine = _StubEngine(close_ok=False)
        result = tcr.run_conformance(
            bot=_StubBot(), engine=engine, client=_StubClient(),
            store=store, symbol=SYMBOL)
        assert result["ok"] is False
        names = [s["stage"] for s in result["stages"]]
        assert names == ["startup", "entry", "protection_local",
                        "protection_readback", "flatten",
                        "final_reconciliation"]
        flatten_stage = next(s for s in result["stages"] if s["stage"] == "flatten")
        assert flatten_stage["ok"] is False

    def test_reconciliation_mismatch_is_reported_not_hidden(self):
        store = _StubStore(positions=[{"symbol": SYMBOL}], naked=[])
        client = _StubClient(
            reconcile_summary={"unknown": 1, "naked_positions": ["ETHUSDT"]})
        result = tcr.run_conformance(
            bot=_StubBot(), engine=_StubEngine(), client=client,
            store=store, symbol=SYMBOL)
        assert result["ok"] is False
        recon_stage = result["stages"][-1]
        assert recon_stage["stage"] == "final_reconciliation"
        assert recon_stage["ok"] is False
        assert recon_stage["summary"]["unknown"] == 1
