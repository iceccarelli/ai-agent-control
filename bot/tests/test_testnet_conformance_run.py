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


class _StubWSConsumer:
    def __init__(self, *, evidence_path=None, evidence_capture_failed=False,
                auth_ok=True, subscribe_ok=True):
        self.evidence_path = evidence_path
        self.evidence_capture_failed = evidence_capture_failed
        self.auth_ok = auth_ok
        self.subscribe_ok = subscribe_ok


_UNSET = object()


class _StubBot:
    def __init__(self, *, startup_ok=True, tick_error=None,
                ws_consumer=_UNSET, observation_status="HEALTHY"):
        self.startup_ok = startup_ok
        self.tick_error = tick_error
        self.ticked = False
        self.ws_consumer = (_StubWSConsumer() if ws_consumer is _UNSET
                           else ws_consumer)
        self.observation_status = observation_status

    def startup(self):
        return self.startup_ok

    def tick(self):
        if self.tick_error:
            raise self.tick_error
        self.ticked = True

    def shutdown(self):
        pass


SYMBOL = "BTCUSDT"


class TestRunConformanceStageSequencing:
    """Tests entry/protection/flatten/reconciliation sequencing — the same
    thing they tested before WS observation existed. `await_ws_observation`
    is patched to always succeed HERE ONLY (a class-scoped autouse
    fixture, not module-scoped) so these tests keep testing exactly that,
    not WS observation's own behavior — covered separately by
    TestWsIntegrationStages/TestAwaitWsObservation below, against real
    evidence files.
    """

    @pytest.fixture(autouse=True)
    def _ws_observation_always_succeeds(self, monkeypatch):
        monkeypatch.setattr(tcr, "await_ws_observation", lambda **kw: True)

    def test_happy_path_runs_every_stage_and_is_ok(self):
        store = _StubStore(positions=[{"symbol": SYMBOL, "qty": 0.01}])
        engine = _StubEngine()
        client = _StubClient()
        bot = _StubBot()

        result = tcr.run_conformance(
            bot=bot, engine=engine, client=client, store=store, symbol=SYMBOL)

        assert result["ok"] is True
        names = [s["stage"] for s in result["stages"]]
        assert names == ["startup", "ws_startup", "entry", "ws_observation",
                        "protection_local",
                        "protection_readback", "flatten",
                        "final_reconciliation", "ws_evidence_completeness"]
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
        assert [s["stage"] for s in result["stages"]] == [
            "startup", "ws_startup", "entry"]

    def test_stops_at_protection_local_failure(self):
        store = _StubStore(positions=[{"symbol": SYMBOL}],
                           naked=[{"symbol": SYMBOL}])
        result = tcr.run_conformance(
            bot=_StubBot(), engine=_StubEngine(), client=_StubClient(),
            store=store, symbol=SYMBOL)
        assert result["ok"] is False
        assert [s["stage"] for s in result["stages"]] == [
            "startup", "ws_startup", "entry", "ws_observation",
            "protection_local"]

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
            "startup", "ws_startup", "entry", "ws_observation",
            "protection_local", "protection_readback"]

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
        assert names == ["startup", "ws_startup", "entry", "ws_observation",
                        "protection_local",
                        "protection_readback", "flatten",
                        "final_reconciliation", "ws_evidence_completeness"]
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
        recon_stage = next(
            s for s in result["stages"] if s["stage"] == "final_reconciliation")
        assert recon_stage["ok"] is False
        assert recon_stage["summary"]["unknown"] == 1


# ---------------------------------------------------------------------------
# WS integration: ws_startup / ws_observation / ws_evidence_completeness,
# and evidence-chain verification — the mission this file's later half exists
# to prove.
# ---------------------------------------------------------------------------

import json  # noqa: E402

import venue_evidence as ve  # noqa: E402


def _write_ws_evidence_record(path, *, order_link_id, topic="order",
                              prev_hash=None, duplicate=False,
                              captured_at_utc=None):
    prev_hash = prev_hash if prev_hash is not None else ve.last_record_hash(path)
    record = ve.build_ws_event_record(
        venue="bybit", environment="testnet",
        ws_url="wss://stream-testnet.bybit.com/v5/private",
        venue_event_id=f"{topic}:test", topic=topic,
        order_link_id=order_link_id, payload={"orderId": "V-1"},
        duplicate=duplicate, prev_hash=prev_hash,
        captured_at_utc=captured_at_utc)
    ve.append_evidence(path, record)


def _write_rest_evidence_record(path, *, prev_hash=None):
    prev_hash = prev_hash if prev_hash is not None else ve.last_record_hash(path)
    record = ve.build_record(
        venue="bybit", environment="testnet", method="GET",
        request_url="https://api-testnet.bybit.com/v5/market/time",
        request={}, response={}, prev_hash=prev_hash)
    ve.append_evidence(path, record)


class TestAwaitWsObservation:
    def test_returns_true_when_a_matching_record_already_exists(self, tmp_path):
        path = str(tmp_path / "ws_evidence.jsonl")
        _write_ws_evidence_record(path, order_link_id="BB-1")
        bot = _StubBot(ws_consumer=_StubWSConsumer(evidence_path=path))

        assert tcr.await_ws_observation(
            bot=bot, order_link_id="BB-1", timeout_seconds=0) is True

    def test_returns_false_when_no_consumer(self):
        bot = _StubBot(ws_consumer=None)
        assert tcr.await_ws_observation(
            bot=bot, order_link_id="BB-1", timeout_seconds=0) is False

    def test_returns_false_on_timeout_with_no_matching_record(self, tmp_path):
        path = str(tmp_path / "ws_evidence.jsonl")
        _write_ws_evidence_record(path, order_link_id="BB-OTHER")
        bot = _StubBot(ws_consumer=_StubWSConsumer(evidence_path=path))

        assert tcr.await_ws_observation(
            bot=bot, order_link_id="BB-1", timeout_seconds=0) is False

    def test_position_topic_does_not_satisfy_observation(self, tmp_path):
        """Only order/execution evidence counts — a position-topic record
        for the same symbol is not proof this ORDER was observed."""
        path = str(tmp_path / "ws_evidence.jsonl")
        _write_ws_evidence_record(path, order_link_id="BB-1", topic="position")

        bot = _StubBot(ws_consumer=_StubWSConsumer(evidence_path=path))
        assert tcr.await_ws_observation(
            bot=bot, order_link_id="BB-1", timeout_seconds=0) is False

    def test_execution_topic_satisfies_observation(self, tmp_path):
        path = str(tmp_path / "ws_evidence.jsonl")
        _write_ws_evidence_record(path, order_link_id="BB-1", topic="execution")
        bot = _StubBot(ws_consumer=_StubWSConsumer(evidence_path=path))

        assert tcr.await_ws_observation(
            bot=bot, order_link_id="BB-1", timeout_seconds=0) is True

    def test_appears_within_the_bound_is_observed(self, tmp_path):
        """A record that lands DURING the wait (not before it) is still
        found — proves this polls, not just checks once at t=0."""
        import threading
        import time

        path = str(tmp_path / "ws_evidence.jsonl")
        bot = _StubBot(ws_consumer=_StubWSConsumer(evidence_path=path))

        def _write_late():
            time.sleep(0.2)
            _write_ws_evidence_record(path, order_link_id="BB-1")

        threading.Thread(target=_write_late).start()
        assert tcr.await_ws_observation(
            bot=bot, order_link_id="BB-1", timeout_seconds=2.0,
            poll_interval=0.05) is True

    def test_wrong_transport_does_not_satisfy_observation(self, tmp_path):
        """FIX 3: a REST-transport record must never satisfy WS
        observation, even if it happens to carry a matching order_link_id
        and an order/execution-shaped `topic` field."""
        path = str(tmp_path / "ws_evidence.jsonl")
        record = ve.build_record(
            venue="bybit", environment="testnet", method="GET",
            request_url="https://api-testnet.bybit.com/v5/market/time",
            request={"topic": "order", "duplicate": False},
            response={}, order_link_id="BB-1")
        ve.append_evidence(path, record)
        bot = _StubBot(ws_consumer=_StubWSConsumer(evidence_path=path))

        assert tcr.await_ws_observation(
            bot=bot, order_link_id="BB-1", timeout_seconds=0) is False

    def test_duplicate_record_does_not_satisfy_observation(self, tmp_path):
        """FIX 3: a record flagged as a replayed/duplicate delivery is not
        proof of a fresh observation."""
        path = str(tmp_path / "ws_evidence.jsonl")
        _write_ws_evidence_record(path, order_link_id="BB-1", duplicate=True)
        bot = _StubBot(ws_consumer=_StubWSConsumer(evidence_path=path))

        assert tcr.await_ws_observation(
            bot=bot, order_link_id="BB-1", timeout_seconds=0) is False

    def test_stale_record_from_before_run_start_does_not_satisfy_observation(
            self, tmp_path):
        """FIX 3: `await_ws_observation` must not accept an arbitrary STALE
        matching record -- a real record for this exact order_link_id, but
        captured before THIS run started (e.g. a reused order_link_id from
        an earlier conformance run against the same testnet account), must
        not count."""
        path = str(tmp_path / "ws_evidence.jsonl")
        _write_ws_evidence_record(
            path, order_link_id="BB-1", captured_at_utc="2020-01-01T00:00:00Z")
        bot = _StubBot(ws_consumer=_StubWSConsumer(evidence_path=path))

        assert tcr.await_ws_observation(
            bot=bot, order_link_id="BB-1", timeout_seconds=0,
            run_start_utc="2026-01-01T00:00:00Z") is False

    def test_fresh_record_after_run_start_satisfies_observation(self, tmp_path):
        path = str(tmp_path / "ws_evidence.jsonl")
        _write_ws_evidence_record(
            path, order_link_id="BB-1", captured_at_utc="2026-06-01T00:00:00Z")
        bot = _StubBot(ws_consumer=_StubWSConsumer(evidence_path=path))

        assert tcr.await_ws_observation(
            bot=bot, order_link_id="BB-1", timeout_seconds=0,
            run_start_utc="2026-01-01T00:00:00Z") is True


class TestWsIntegrationStages:
    def test_ws_startup_fails_when_bot_has_no_consumer(self):
        bot = _StubBot(ws_consumer=None)
        result = tcr.run_conformance(
            bot=bot, engine=_StubEngine(), client=_StubClient(),
            store=_StubStore(), symbol=SYMBOL)
        assert result["ok"] is False
        assert [s["stage"] for s in result["stages"]] == [
            "startup", "ws_startup"]

    def test_ws_startup_fails_when_auth_not_ok(self):
        """FIX 5: a connected consumer alone is not sufficient -- AUTH_OK
        must be explicitly established."""
        consumer = _StubWSConsumer(auth_ok=False, subscribe_ok=True)
        bot = _StubBot(ws_consumer=consumer)
        result = tcr.run_conformance(
            bot=bot, engine=_StubEngine(), client=_StubClient(),
            store=_StubStore(), symbol=SYMBOL)
        assert result["ok"] is False
        assert [s["stage"] for s in result["stages"]] == [
            "startup", "ws_startup"]
        ws_stage = result["stages"][-1]
        assert ws_stage["auth_ok"] is False

    def test_ws_startup_fails_when_subscribe_not_ok(self):
        """FIX 5: SUBSCRIBE_OK must also be explicitly established."""
        consumer = _StubWSConsumer(auth_ok=True, subscribe_ok=False)
        bot = _StubBot(ws_consumer=consumer)
        result = tcr.run_conformance(
            bot=bot, engine=_StubEngine(), client=_StubClient(),
            store=_StubStore(), symbol=SYMBOL)
        assert result["ok"] is False
        assert [s["stage"] for s in result["stages"]] == [
            "startup", "ws_startup"]
        ws_stage = result["stages"][-1]
        assert ws_stage["subscribe_ok"] is False

    def test_ws_startup_passes_when_both_auth_and_subscribe_ok(self):
        consumer = _StubWSConsumer(auth_ok=True, subscribe_ok=True)
        bot = _StubBot(ws_consumer=consumer)
        result = tcr.run_conformance(
            bot=bot, engine=_StubEngine(), client=_StubClient(),
            store=_StubStore(positions=[]), symbol=SYMBOL)
        ws_stage = next(s for s in result["stages"] if s["stage"] == "ws_startup")
        assert ws_stage["ok"] is True

    def test_ws_observation_failure_stops_before_protection(self, monkeypatch):
        monkeypatch.setattr(tcr, "await_ws_observation", lambda **kw: False)
        store = _StubStore(positions=[{"symbol": SYMBOL,
                                       "order_link_id": "BB-1"}])
        result = tcr.run_conformance(
            bot=_StubBot(), engine=_StubEngine(), client=_StubClient(),
            store=store, symbol=SYMBOL)
        assert result["ok"] is False
        assert [s["stage"] for s in result["stages"]] == [
            "startup", "ws_startup", "entry", "ws_observation"]

    def test_ws_evidence_completeness_fails_the_run_on_capture_failure(
            self, monkeypatch):
        monkeypatch.setattr(tcr, "await_ws_observation", lambda **kw: True)
        consumer = _StubWSConsumer(evidence_capture_failed=True)
        result = tcr.run_conformance(
            bot=_StubBot(ws_consumer=consumer), engine=_StubEngine(),
            client=_StubClient(), store=_StubStore(
                positions=[{"symbol": SYMBOL}]),
            symbol=SYMBOL)
        assert result["ok"] is False
        last_stage = result["stages"][-1]
        assert last_stage["stage"] == "ws_evidence_completeness"
        assert last_stage["ok"] is False

    def test_ws_evidence_completeness_passes_when_capture_succeeded(
            self, monkeypatch):
        monkeypatch.setattr(tcr, "await_ws_observation", lambda **kw: True)
        consumer = _StubWSConsumer(evidence_capture_failed=False)
        result = tcr.run_conformance(
            bot=_StubBot(ws_consumer=consumer), engine=_StubEngine(),
            client=_StubClient(), store=_StubStore(
                positions=[{"symbol": SYMBOL}]),
            symbol=SYMBOL)
        assert result["ok"] is True


class TestVerifyEvidenceChains:
    def test_both_chains_clean_is_verified(self, tmp_path):
        rest_path = str(tmp_path / "rest.jsonl")
        ws_path = str(tmp_path / "ws.jsonl")
        record = ve.build_record(
            venue="bybit", environment="testnet", method="GET",
            request_url="https://api-testnet.bybit.com/v5/market/time",
            request={}, response={})
        ve.append_evidence(rest_path, record)
        _write_ws_evidence_record(ws_path, order_link_id="BB-1")

        result = tcr.verify_evidence_chains(
            rest_evidence_path=rest_path, ws_evidence_path=ws_path)

        assert result["rest_evidence_verified"] is True
        assert result["ws_evidence_verified"] is True

    def test_tampered_ws_chain_is_not_verified(self, tmp_path):
        rest_path = str(tmp_path / "rest.jsonl")
        ws_path = str(tmp_path / "ws.jsonl")
        _write_ws_evidence_record(ws_path, order_link_id="BB-1")
        with open(ws_path, encoding="utf-8") as h:
            record = json.loads(h.readline())
        record["response"] = {"forged": True}
        with open(ws_path, "w", encoding="utf-8") as h:
            h.write(json.dumps(record) + "\n")

        result = tcr.verify_evidence_chains(
            rest_evidence_path=rest_path, ws_evidence_path=ws_path)

        assert result["ws_evidence_verified"] is False
        assert result["ws_evidence_reasons"] != []

    def test_missing_files_are_vacuously_verified(self, tmp_path):
        """No evidence file yet (e.g. a run that never got far enough to
        capture anything) is not the same as a TAMPERED chain — verify_chain
        already treats an absent file as clean (nothing to contradict); the
        conformance runner's `ws_observation` stage is what actually
        requires real evidence to exist, not this check."""
        result = tcr.verify_evidence_chains(
            rest_evidence_path=str(tmp_path / "nope-rest.jsonl"),
            ws_evidence_path=str(tmp_path / "nope-ws.jsonl"))
        assert result["rest_evidence_verified"] is True
        assert result["ws_evidence_verified"] is True


class TestVerifyEvidenceCompleteness:
    """FIX 2 — ASSURANCE MUST REQUIRE ACTUAL EVIDENCE: a clean (vacuous)
    chain around missing evidence must not read as verified."""

    def test_missing_both_files_fails_closed(self, tmp_path):
        result = tcr.verify_evidence_completeness(
            rest_evidence_path=str(tmp_path / "nope-rest.jsonl"),
            ws_evidence_path=str(tmp_path / "nope-ws.jsonl"),
            order_link_id="BB-1")
        assert result["ok"] is False
        assert result["rest_evidence_exists"] is False
        assert result["ws_evidence_exists"] is False

    def test_missing_rest_evidence_alone_fails(self, tmp_path):
        ws_path = str(tmp_path / "ws.jsonl")
        _write_ws_evidence_record(ws_path, order_link_id="BB-1", topic="order")
        _write_ws_evidence_record(ws_path, order_link_id="BB-1", topic="execution")
        result = tcr.verify_evidence_completeness(
            rest_evidence_path=str(tmp_path / "nope-rest.jsonl"),
            ws_evidence_path=ws_path, order_link_id="BB-1")
        assert result["ok"] is False
        assert result["rest_evidence_exists"] is False
        assert result["ws_evidence_exists"] is True

    def test_missing_ws_evidence_alone_fails(self, tmp_path):
        rest_path = str(tmp_path / "rest.jsonl")
        _write_rest_evidence_record(rest_path)
        result = tcr.verify_evidence_completeness(
            rest_evidence_path=rest_path,
            ws_evidence_path=str(tmp_path / "nope-ws.jsonl"),
            order_link_id="BB-1")
        assert result["ok"] is False
        assert result["ws_evidence_exists"] is False

    def test_ws_evidence_present_but_missing_order_topic_fails(self, tmp_path):
        rest_path = str(tmp_path / "rest.jsonl")
        ws_path = str(tmp_path / "ws.jsonl")
        _write_rest_evidence_record(rest_path)
        _write_ws_evidence_record(ws_path, order_link_id="BB-1", topic="execution")
        result = tcr.verify_evidence_completeness(
            rest_evidence_path=rest_path, ws_evidence_path=ws_path,
            order_link_id="BB-1")
        assert result["ok"] is False
        assert result["order_evidence_exists"] is False
        assert result["execution_evidence_exists"] is True

    def test_ws_evidence_present_but_missing_execution_topic_fails(self, tmp_path):
        rest_path = str(tmp_path / "rest.jsonl")
        ws_path = str(tmp_path / "ws.jsonl")
        _write_rest_evidence_record(rest_path)
        _write_ws_evidence_record(ws_path, order_link_id="BB-1", topic="order")
        result = tcr.verify_evidence_completeness(
            rest_evidence_path=rest_path, ws_evidence_path=ws_path,
            order_link_id="BB-1")
        assert result["ok"] is False
        assert result["execution_evidence_exists"] is False

    def test_only_duplicate_ws_records_fails(self, tmp_path):
        """A duplicate-flagged record must not count toward evidence
        completeness, same as it does not satisfy `await_ws_observation`."""
        rest_path = str(tmp_path / "rest.jsonl")
        ws_path = str(tmp_path / "ws.jsonl")
        _write_rest_evidence_record(rest_path)
        _write_ws_evidence_record(
            ws_path, order_link_id="BB-1", topic="order", duplicate=True)
        _write_ws_evidence_record(
            ws_path, order_link_id="BB-1", topic="execution", duplicate=True)
        result = tcr.verify_evidence_completeness(
            rest_evidence_path=rest_path, ws_evidence_path=ws_path,
            order_link_id="BB-1")
        assert result["ok"] is False
        assert result["order_evidence_exists"] is False
        assert result["execution_evidence_exists"] is False

    def test_evidence_capture_failed_flag_fails_even_with_full_evidence(
            self, tmp_path):
        rest_path = str(tmp_path / "rest.jsonl")
        ws_path = str(tmp_path / "ws.jsonl")
        _write_rest_evidence_record(rest_path)
        _write_ws_evidence_record(ws_path, order_link_id="BB-1", topic="order")
        _write_ws_evidence_record(ws_path, order_link_id="BB-1", topic="execution")
        result = tcr.verify_evidence_completeness(
            rest_evidence_path=rest_path, ws_evidence_path=ws_path,
            order_link_id="BB-1", evidence_capture_failed=True)
        assert result["ok"] is False

    def test_all_present_and_clean_passes(self, tmp_path):
        rest_path = str(tmp_path / "rest.jsonl")
        ws_path = str(tmp_path / "ws.jsonl")
        _write_rest_evidence_record(rest_path)
        _write_ws_evidence_record(ws_path, order_link_id="BB-1", topic="order")
        _write_ws_evidence_record(ws_path, order_link_id="BB-1", topic="execution")
        result = tcr.verify_evidence_completeness(
            rest_evidence_path=rest_path, ws_evidence_path=ws_path,
            order_link_id="BB-1", evidence_capture_failed=False)
        assert result["ok"] is True
