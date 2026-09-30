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
        self.order_status_updates = []

    def open_positions(self):
        return self._positions

    def positions_without_stops(self):
        return self._naked

    def update_order_status(self, order_link_id, status, exchange_id=""):
        self.order_status_updates.append((order_link_id, status, exchange_id))


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
                get_position_error=None, reconcile_error=None,
                is_linear=True, verify_stop_result=(True, "orderStatus=New"),
                verify_stop_error=None):
        self.remote_stop = remote_stop
        self._reconcile_summary = reconcile_summary or {
            "unknown": 0, "naked_positions": []}
        self._get_position_error = get_position_error
        self._reconcile_error = reconcile_error
        #: Defaults True so every EXISTING test (written before category
        #: dispatch existed, and never setting this) keeps exercising the
        #: LINEAR path unchanged -- see TestProtectionReadbackCategoryDispatch
        #: for the SPOT-specific tests, which set this False explicitly.
        self.is_linear = is_linear
        self._verify_stop_result = verify_stop_result
        self._verify_stop_error = verify_stop_error
        self.get_position_calls = 0
        self.verify_stop_calls = []

    def get_position(self, symbol):
        self.get_position_calls += 1
        if not self.is_linear:
            # Mirrors the real BybitClient.get_position: linear-only,
            # raises on spot. A caller that reaches here on a spot client
            # is exactly the mismatch this fix closes.
            raise RuntimeError("get_position is linear-only")
        if self._get_position_error:
            raise self._get_position_error
        return {"symbol": symbol, "stopLoss": str(self.remote_stop)}

    def verify_stop(self, *, symbol, order_link_id):
        self.verify_stop_calls.append((symbol, order_link_id))
        if self._verify_stop_error:
            raise self._verify_stop_error
        return self._verify_stop_result

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
        self.drain_calls = 0

    def drain_and_apply(self, store):
        self.drain_calls += 1
        return []


_UNSET = object()


class _FakeThread:
    """`threading.Thread`-shaped enough for `finalize_ws_lifecycle()`'s
    `is_alive()` check -- `alive` is fixed, never flips on its own, so a
    test controls exactly what "the shutdown join left it alive" looks
    like without a real thread/timing race."""

    def __init__(self, alive):
        self._alive = alive

    def is_alive(self):
        return self._alive


class _StubBot:
    def __init__(self, *, startup_ok=True, tick_error=None,
                ws_consumer=_UNSET, observation_status="HEALTHY",
                ws_thread=None):
        self.startup_ok = startup_ok
        self.tick_error = tick_error
        self.ticked = False
        self.ws_consumer = (_StubWSConsumer() if ws_consumer is _UNSET
                           else ws_consumer)
        self.observation_status = observation_status
        #: None (default) simulates "already stopped" -- the ordinary
        #: case. A `_FakeThread(alive=True)` simulates the shutdown
        #: failure this review round closes: the join budget elapsed
        #: with the WS thread still genuinely alive.
        self.ws_thread = ws_thread

    def startup(self):
        return self.startup_ok

    def tick(self):
        if self.tick_error:
            raise self.tick_error
        self.ticked = True

    def shutdown(self):
        pass

    def _stop_private_ws(self):
        self.ws_stop_calls = getattr(self, "ws_stop_calls", 0) + 1
        if isinstance(self.ws_thread, _FakeThread) and not self.ws_thread.is_alive():
            self.ws_thread = None


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


# ---------------------------------------------------------------------------
# FIX 1 (this review round) — bounded WS readiness wait: startup() starts the
# WS observer thread asynchronously; auth_ok/subscribe_ok must be POLLED, not
# read once immediately.
# ---------------------------------------------------------------------------


class TestAwaitWsReady:
    def test_no_consumer_is_never_ready(self):
        bot = _StubBot(ws_consumer=None)
        result = tcr.await_ws_ready(bot=bot, timeout_seconds=0)
        assert result["ready"] is False

    def test_already_ready_returns_immediately(self):
        bot = _StubBot(ws_consumer=_StubWSConsumer(auth_ok=True, subscribe_ok=True))
        result = tcr.await_ws_ready(bot=bot, timeout_seconds=0)
        assert result == {"ready": True, "auth_ok": True, "subscribe_ok": True}

    def test_times_out_when_never_ready(self):
        bot = _StubBot(
            ws_consumer=_StubWSConsumer(auth_ok=False, subscribe_ok=False))
        result = tcr.await_ws_ready(bot=bot, timeout_seconds=0)
        assert result["ready"] is False

    def test_waits_for_asynchronous_auth_and_subscribe_readiness(self):
        """The exact race FIX 1 closes: the observer thread's auth/
        subscribe sequence completes SHORTLY AFTER `startup()` already
        returned -- this must WAIT for it, not fail on the first read."""
        import threading
        import time as time_mod

        consumer = _StubWSConsumer(auth_ok=False, subscribe_ok=False)
        bot = _StubBot(ws_consumer=consumer)

        def _become_ready_late():
            time_mod.sleep(0.15)
            consumer.auth_ok = True
            consumer.subscribe_ok = True

        threading.Thread(target=_become_ready_late).start()

        result = tcr.await_ws_ready(
            bot=bot, timeout_seconds=2.0, poll_interval=0.05)
        assert result == {"ready": True, "auth_ok": True, "subscribe_ok": True}

    def test_only_auth_ready_is_not_ready(self):
        bot = _StubBot(
            ws_consumer=_StubWSConsumer(auth_ok=True, subscribe_ok=False))
        result = tcr.await_ws_ready(bot=bot, timeout_seconds=0)
        assert result["ready"] is False
        assert result["auth_ok"] is True
        assert result["subscribe_ok"] is False


class TestRunConformanceWsReadyWait:
    def test_ws_startup_waits_rather_than_failing_immediately(self, monkeypatch):
        """run_conformance's own ws_startup stage must use the bounded
        wait, not a single immediate read -- proven end to end, not just
        at the await_ws_ready unit level."""
        import threading
        import time as time_mod

        monkeypatch.setattr(tcr, "await_ws_observation", lambda **kw: True)
        consumer = _StubWSConsumer(auth_ok=False, subscribe_ok=False)
        bot = _StubBot(ws_consumer=consumer)

        def _become_ready_late():
            time_mod.sleep(0.15)
            consumer.auth_ok = True
            consumer.subscribe_ok = True

        threading.Thread(target=_become_ready_late).start()

        result = tcr.run_conformance(
            bot=bot, engine=_StubEngine(), client=_StubClient(),
            store=_StubStore(positions=[{"symbol": SYMBOL}]), symbol=SYMBOL,
            ws_ready_timeout=2.0)

        ws_stage = next(s for s in result["stages"] if s["stage"] == "ws_startup")
        assert ws_stage["ok"] is True

    def test_ws_startup_fails_closed_on_timeout(self):
        consumer = _StubWSConsumer(auth_ok=False, subscribe_ok=False)
        bot = _StubBot(ws_consumer=consumer)
        result = tcr.run_conformance(
            bot=bot, engine=_StubEngine(), client=_StubClient(),
            store=_StubStore(), symbol=SYMBOL, ws_ready_timeout=0)
        assert result["ok"] is False
        assert [s["stage"] for s in result["stages"]] == ["startup", "ws_startup"]


# ---------------------------------------------------------------------------
# FIX 2 — await_ws_observation must drain the WS consumer's pending queues
# onto the writer thread on every poll, not merely read the evidence file.
# ---------------------------------------------------------------------------


class TestAwaitWsObservationDrainsQueue:
    def test_drains_on_every_poll_when_store_is_given(self, tmp_path):
        path = str(tmp_path / "ws_evidence.jsonl")
        _write_ws_evidence_record(path, order_link_id="BB-1")
        consumer = _StubWSConsumer(evidence_path=path)
        bot = _StubBot(ws_consumer=consumer)
        store = _StubStore()

        assert tcr.await_ws_observation(
            bot=bot, order_link_id="BB-1", timeout_seconds=0,
            store=store) is True
        assert consumer.drain_calls >= 1

    def test_no_store_given_never_drains(self, tmp_path):
        """Existing callers (e.g. the pure evidence-polling unit tests
        above) that pass no `store` must keep working exactly as before —
        draining is opt-in via `store`, not forced."""
        path = str(tmp_path / "ws_evidence.jsonl")
        _write_ws_evidence_record(path, order_link_id="BB-1")
        consumer = _StubWSConsumer(evidence_path=path)
        bot = _StubBot(ws_consumer=consumer)

        assert tcr.await_ws_observation(
            bot=bot, order_link_id="BB-1", timeout_seconds=0) is True
        assert consumer.drain_calls == 0


# ---------------------------------------------------------------------------
# FIX 3 — final WS flush: stop + join the WS thread, then one last
# writer-thread drain, BEFORE evidence is verified and the store closes.
# ---------------------------------------------------------------------------


class TestFinalizeWsLifecycle:
    def test_stops_the_ws_thread_and_drains_once_more(self):
        consumer = _StubWSConsumer()
        bot = _StubBot(ws_consumer=consumer)
        store = _StubStore()

        tcr.finalize_ws_lifecycle(bot=bot, store=store)

        assert bot.ws_stop_calls == 1
        assert consumer.drain_calls == 1

    def test_no_consumer_is_a_noop(self):
        bot = _StubBot(ws_consumer=None)
        store = _StubStore()
        tcr.finalize_ws_lifecycle(bot=bot, store=store)  # must not raise
        assert bot.ws_stop_calls == 1

    def test_missing_stop_method_does_not_raise(self):
        class _NoStopBot:
            ws_consumer = None

        tcr.finalize_ws_lifecycle(bot=_NoStopBot(), store=_StubStore())

    def test_drain_raising_never_propagates(self):
        class _ExplodingConsumer(_StubWSConsumer):
            def drain_and_apply(self, store):
                raise RuntimeError("disk full")

        bot = _StubBot(ws_consumer=_ExplodingConsumer())
        tcr.finalize_ws_lifecycle(bot=bot, store=_StubStore())  # must not raise
        assert bot.ws_stop_calls == 1

    def test_stop_raising_still_attempts_the_drain(self):
        class _ExplodingStopBot(_StubBot):
            def _stop_private_ws(self):
                raise RuntimeError("thread join timed out")

        consumer = _StubWSConsumer()
        bot = _ExplodingStopBot(ws_consumer=consumer)
        tcr.finalize_ws_lifecycle(bot=bot, store=_StubStore())  # must not raise
        assert consumer.drain_calls == 1

    def test_thread_still_alive_after_stop_skips_the_drain_and_reports_it(self):
        """The exact race this review round closes: `_stop_private_ws()`
        can return with the WS thread still genuinely alive (a real
        shutdown failure). Draining now would not be FINAL -- the still-
        running thread could enqueue more immediately after -- so the
        drain must be skipped, not attempted anyway."""
        consumer = _StubWSConsumer()
        bot = _StubBot(ws_consumer=consumer, ws_thread=_FakeThread(alive=True))

        result = tcr.finalize_ws_lifecycle(bot=bot, store=_StubStore())

        assert result == {"ws_thread_stopped": False}
        assert bot.ws_stop_calls == 1
        assert consumer.drain_calls == 0, (
            "a drain while the thread is still alive is not the final "
            "drain the invariant requires")

    def test_thread_genuinely_stopped_reports_it_and_drains(self):
        consumer = _StubWSConsumer()
        bot = _StubBot(ws_consumer=consumer, ws_thread=_FakeThread(alive=False))

        result = tcr.finalize_ws_lifecycle(bot=bot, store=_StubStore())

        assert result == {"ws_thread_stopped": True}
        assert consumer.drain_calls == 1


class TestConformanceOk:
    """FIX (this round): conformance must not report success while the WS
    thread remains alive, even when every stage and all evidence content
    checks otherwise pass -- isolated as a pure function so this is
    directly testable without a real venue stack."""

    def test_all_conditions_true_is_ok(self):
        assert tcr._conformance_ok(
            stages_ok=True, completeness_ok=True, ws_thread_stopped=True
        ) is True

    def test_ws_thread_still_alive_fails_even_with_everything_else_green(self):
        assert tcr._conformance_ok(
            stages_ok=True, completeness_ok=True, ws_thread_stopped=False
        ) is False

    def test_bad_stages_still_fails_regardless_of_ws_thread(self):
        assert tcr._conformance_ok(
            stages_ok=False, completeness_ok=True, ws_thread_stopped=True
        ) is False

    def test_incomplete_evidence_still_fails_regardless_of_ws_thread(self):
        assert tcr._conformance_ok(
            stages_ok=True, completeness_ok=False, ws_thread_stopped=True
        ) is False


# ---------------------------------------------------------------------------
# The critical proof, against a REAL WSPrivateConsumer (never a
# pre-populated evidence file): tick -> order submitted -> WS event arrives
# -> queue -> observation wait drains it onto the writer thread -> durable
# evidence -> success.
# ---------------------------------------------------------------------------


import private_ws_consumer as pwc  # noqa: E402


class _WSEvidenceClient:
    def __init__(self, venue="testnet",
                ws_private_url="wss://stream-testnet.bybit.com/v5/private"):
        self.venue = venue
        self.ws_private_url = ws_private_url


class TestRealQueueDrainClosesTheRace:
    def test_ws_event_after_tick_is_queued_then_drained_and_observed(
            self, tmp_path):
        """No pre-populated evidence file. A REAL WSPrivateConsumer only
        ENQUEUES `handle_raw_message` (the WS thread's job); nothing
        durable exists until `drain_and_apply` (the writer thread's job)
        runs. The WS event is delivered from a background thread AFTER
        this test's own `run_conformance` call has already started its
        bounded observation wait -- proving the drain happens DURING that
        wait, not merely once at the start.
        """
        import threading
        import time as time_mod

        ws_evidence_path = str(tmp_path / "ws_evidence.jsonl")
        order_link_id = "BB-real-race-1"
        consumer = pwc.WSPrivateConsumer(
            client=_WSEvidenceClient(), transport_factory=lambda: None,
            evidence_path=ws_evidence_path, assurance_mode=True)
        consumer.auth_ok = True
        consumer.subscribe_ok = True

        order_msg = json.dumps({
            "topic": "order",
            "data": [{
                "symbol": SYMBOL, "orderId": "V-REAL-RACE-1",
                "orderLinkId": order_link_id, "orderStatus": "Filled",
                "updatedTime": "1",
            }],
        })

        def _deliver_ws_event_late():
            time_mod.sleep(0.3)
            # The WS (background) thread: OBSERVE and ENQUEUE only -- see
            # private_ws_consumer.py's ownership model. Must never itself
            # write durable evidence or touch the store.
            consumer.handle_raw_message(order_msg)

        threading.Thread(target=_deliver_ws_event_late).start()

        # Nothing durable yet -- proves this test is not reading a
        # pre-populated file.
        assert not os.path.exists(ws_evidence_path) or tcr._read_evidence_records(
            ws_evidence_path) == []

        bot = _StubBot(ws_consumer=consumer)
        store = _StubStore(
            positions=[{"symbol": SYMBOL, "order_link_id": order_link_id}])

        result = tcr.run_conformance(
            bot=bot, engine=_StubEngine(), client=_StubClient(),
            store=store, symbol=SYMBOL, ws_observation_timeout=3.0)

        ws_observation_stage = next(
            s for s in result["stages"] if s["stage"] == "ws_observation")
        assert ws_observation_stage["ok"] is True, (
            "the bounded observation wait must drain the queued WS event "
            "onto the writer thread and then see the durable record it "
            "produces")

        records = tcr._read_evidence_records(ws_evidence_path)
        assert any(
            r.get("order_link_id") == order_link_id
            and r.get("transport") == "ws"
            and r.get("request", {}).get("topic") == "order"
            for r in records), "the drained event must be durably on disk"
        # And it got there via the real StateStore-update path too --
        # drain_and_apply() applies order/execution events to the store,
        # exactly like TradingBot.tick() does.
        assert store.order_status_updates == [
            (order_link_id, "filled", "V-REAL-RACE-1")]

    def test_final_flush_persists_an_event_the_observation_wait_missed(
            self, tmp_path):
        """FIX 3: an event that arrives so late it is NOT caught by the
        bounded observation wait (ws_observation legitimately times out
        and the run fails that stage) must still not be lost -- the final
        flush (finalize_ws_lifecycle) must drain it before evidence
        verification, proving the record is not silently dropped at
        shutdown."""
        import threading
        import time as time_mod

        ws_evidence_path = str(tmp_path / "ws_evidence.jsonl")
        order_link_id = "BB-real-late-1"
        consumer = pwc.WSPrivateConsumer(
            client=_WSEvidenceClient(), transport_factory=lambda: None,
            evidence_path=ws_evidence_path, assurance_mode=True)
        consumer.auth_ok = True
        consumer.subscribe_ok = True

        order_msg = json.dumps({
            "topic": "order",
            "data": [{
                "symbol": SYMBOL, "orderId": "V-REAL-LATE-1",
                "orderLinkId": order_link_id, "orderStatus": "Filled",
                "updatedTime": "1",
            }],
        })

        def _deliver_after_observation_gives_up():
            # Later than the (very short) observation timeout below, so
            # ws_observation legitimately fails this run -- the point is
            # what happens to the event that arrives AFTER that.
            time_mod.sleep(0.3)
            consumer.handle_raw_message(order_msg)

        threading.Thread(target=_deliver_after_observation_gives_up).start()

        bot = _StubBot(ws_consumer=consumer)
        store = _StubStore(
            positions=[{"symbol": SYMBOL, "order_link_id": order_link_id}])

        result = tcr.run_conformance(
            bot=bot, engine=_StubEngine(), client=_StubClient(),
            store=store, symbol=SYMBOL, ws_observation_timeout=0.05)
        ws_observation_stage = next(
            s for s in result["stages"] if s["stage"] == "ws_observation")
        assert ws_observation_stage["ok"] is False, (
            "setup check: the event must genuinely arrive after this "
            "run's own bounded wait gave up")

        time_mod.sleep(0.3)  # let the late delivery actually land
        tcr.finalize_ws_lifecycle(bot=bot, store=store)

        records = tcr._read_evidence_records(ws_evidence_path)
        assert any(
            r.get("order_link_id") == order_link_id
            and r.get("request", {}).get("topic") == "order"
            for r in records), (
            "an event already received on the wire, sitting in the "
            "queue, must not disappear at shutdown")


# ---------------------------------------------------------------------------
# FIX — category mismatch: run_conformance() unconditionally called the
# LINEAR-only client.get_position() for protection readback, which raises
# on spot and reads a field (stopLoss) that does not exist there. Spot's
# protection is a separate order; the EXISTING client.verify_stop() already
# dispatches correctly and reads it back via get_order() -- this fix just
# makes the conformance runner use it instead of reinventing a second
# mechanism.
# ---------------------------------------------------------------------------


class TestProtectionReadbackCategoryDispatch:
    def test_linear_category_retains_get_position_stoploss_check(self):
        client = _StubClient(is_linear=True, remote_stop=100.0)
        row = {"symbol": SYMBOL, "meta": "{}"}
        store = _StubStore(positions=[row])

        result = tcr.verify_protection(
            client=client, store=store, symbol=SYMBOL, entry_row=row)

        assert result == {"ok": True, "category": "linear", "remote_stop": 100.0}
        assert client.verify_stop_calls == [], (
            "linear must not go anywhere near the spot order-readback path")

    def test_linear_category_fails_when_stoploss_is_zero(self):
        client = _StubClient(is_linear=True, remote_stop=0.0)
        row = {"symbol": SYMBOL, "meta": "{}"}
        result = tcr.verify_protection(
            client=client, store=_StubStore(positions=[row]), symbol=SYMBOL,
            entry_row=row)
        assert result["ok"] is False
        assert result["category"] == "linear"

    def test_linear_category_readback_error_fails_closed(self):
        client = _StubClient(is_linear=True,
                             get_position_error=RuntimeError("venue down"))
        row = {"symbol": SYMBOL, "meta": "{}"}
        result = tcr.verify_protection(
            client=client, store=_StubStore(positions=[row]), symbol=SYMBOL,
            entry_row=row)
        assert result["ok"] is False
        assert result["category"] == "linear"
        assert "error" in result

    def test_spot_category_never_calls_linear_only_get_position(self):
        client = _StubClient(is_linear=False,
                             verify_stop_result=(True, "orderStatus=New"))
        row = {"symbol": SYMBOL,
              "meta": json.dumps({"stop_order_link_id": "BB-stop-1"})}
        store = _StubStore(positions=[row])

        result = tcr.verify_protection(
            client=client, store=store, symbol=SYMBOL, entry_row=row)

        assert client.get_position_calls == 0, (
            "spot must never call the linear-only get_position() -- this "
            "is the exact mismatch being fixed")
        assert result == {
            "ok": True, "category": "spot",
            "stop_order_link_id": "BB-stop-1", "detail": "orderStatus=New"}
        assert client.verify_stop_calls == [(SYMBOL, "BB-stop-1")]

    def test_spot_category_fails_when_stop_order_not_live(self):
        """The venue itself must report the order live -- a stop order
        that has already triggered/filled/cancelled is not protection,
        regardless of what local state believes."""
        client = _StubClient(
            is_linear=False,
            verify_stop_result=(False, "STOP_ORDER_NOT_LIVE:Filled"))
        row = {"symbol": SYMBOL,
              "meta": json.dumps({"stop_order_link_id": "BB-stop-1"})}
        store = _StubStore(positions=[row])

        result = tcr.verify_protection(
            client=client, store=store, symbol=SYMBOL, entry_row=row)

        assert result["ok"] is False
        assert result["category"] == "spot"
        assert client.get_position_calls == 0

    def test_spot_category_fails_closed_when_no_stop_order_link_id_recorded(self):
        client = _StubClient(is_linear=False)
        row = {"symbol": SYMBOL, "meta": "{}"}
        store = _StubStore(positions=[row])

        result = tcr.verify_protection(
            client=client, store=store, symbol=SYMBOL, entry_row=row)

        assert result["ok"] is False
        assert result["category"] == "spot"
        assert client.verify_stop_calls == [], (
            "must never call verify_stop with a fabricated/empty id")
        assert client.get_position_calls == 0

    def test_spot_category_uses_the_freshest_position_row_not_the_stale_entry_row(self):
        """A partial exit between 'entry' and readback could have re-sized
        or replaced the stop -- the CURRENT store row's
        stop_order_link_id must be used, never whatever was captured at
        entry time."""
        client = _StubClient(is_linear=False,
                             verify_stop_result=(True, "orderStatus=New"))
        stale_row = {"symbol": SYMBOL,
                    "meta": json.dumps({"stop_order_link_id": "BB-stop-OLD"})}
        fresh_row = {"symbol": SYMBOL,
                    "meta": json.dumps({"stop_order_link_id": "BB-stop-NEW"})}
        store = _StubStore(positions=[fresh_row])

        result = tcr.verify_protection(
            client=client, store=store, symbol=SYMBOL, entry_row=stale_row)

        assert result["stop_order_link_id"] == "BB-stop-NEW"
        assert client.verify_stop_calls == [(SYMBOL, "BB-stop-NEW")]

    def test_spot_category_verify_stop_raising_fails_closed(self):
        client = _StubClient(is_linear=False,
                             verify_stop_error=RuntimeError("venue down"))
        row = {"symbol": SYMBOL,
              "meta": json.dumps({"stop_order_link_id": "BB-stop-1"})}
        result = tcr.verify_protection(
            client=client, store=_StubStore(positions=[row]), symbol=SYMBOL,
            entry_row=row)
        assert result["ok"] is False
        assert result["category"] == "spot"
        assert "error" in result


class TestRunConformanceProtectionReadbackEndToEnd:
    """The whole pipeline, not just verify_protection() in isolation --
    proves run_conformance() actually wires category dispatch in, not
    merely that the helper function does the right thing."""

    @pytest.fixture(autouse=True)
    def _ws_observation_always_succeeds(self, monkeypatch):
        monkeypatch.setattr(tcr, "await_ws_observation", lambda **kw: True)

    def test_default_linear_client_reaches_flatten(self):
        """No regression: the pre-fix default (is_linear=True) must still
        run every stage through to a clean flatten, exactly as before."""
        store = _StubStore(positions=[{"symbol": SYMBOL, "meta": "{}"}])
        client = _StubClient(is_linear=True, remote_stop=100.0)
        engine = _StubEngine()

        result = tcr.run_conformance(
            bot=_StubBot(), engine=engine, client=client, store=store,
            symbol=SYMBOL)

        readback = next(
            s for s in result["stages"] if s["stage"] == "protection_readback")
        assert readback["ok"] is True
        assert readback["category"] == "linear"
        assert client.get_position_calls >= 1
        assert engine.closed == [SYMBOL]
        assert result["ok"] is True

    def test_spot_client_reaches_flatten_via_verify_stop_not_get_position(self):
        row = {"symbol": SYMBOL,
              "meta": json.dumps({"stop_order_link_id": "BB-stop-1"})}
        store = _StubStore(positions=[row])
        client = _StubClient(is_linear=False,
                             verify_stop_result=(True, "orderStatus=New"))
        engine = _StubEngine()

        result = tcr.run_conformance(
            bot=_StubBot(), engine=engine, client=client, store=store,
            symbol=SYMBOL)

        readback = next(
            s for s in result["stages"] if s["stage"] == "protection_readback")
        assert readback["ok"] is True
        assert readback["category"] == "spot"
        assert client.get_position_calls == 0, (
            "the exact mismatch this fix closes: spot must never reach "
            "the linear-only get_position()")
        assert client.verify_stop_calls == [(SYMBOL, "BB-stop-1")]
        # Flatten follows the EXISTING engine.close_position() path
        # unconditionally -- category-agnostic already, no second close
        # implementation introduced by this fix.
        assert engine.closed == [SYMBOL]
        assert result["ok"] is True

    def test_spot_client_with_dead_stop_stops_before_flatten(self):
        """A stop the venue no longer reports as live must stop the run
        BEFORE flatten -- exactly like the linear stopLoss==0 case did
        before this fix, now on the spot path too."""
        row = {"symbol": SYMBOL,
              "meta": json.dumps({"stop_order_link_id": "BB-stop-1"})}
        store = _StubStore(positions=[row])
        client = _StubClient(
            is_linear=False,
            verify_stop_result=(False, "STOP_ORDER_NOT_LIVE:Filled"))
        engine = _StubEngine()

        result = tcr.run_conformance(
            bot=_StubBot(), engine=engine, client=client, store=store,
            symbol=SYMBOL)

        assert result["ok"] is False
        assert [s["stage"] for s in result["stages"]] == [
            "startup", "ws_startup", "entry", "ws_observation",
            "protection_local", "protection_readback"]
        assert engine.closed == [], "must not flatten past a failed readback"


class TestEvidenceRequirementsUnchangedByCategoryFix:
    """This fix must not touch FIX 2's evidence-completeness acceptance
    criteria -- missing evidence still fails, a clean chain around empty
    evidence is still not proof, regardless of category."""

    def test_verify_evidence_completeness_still_rejects_a_fabricated_empty_pass(
            self, tmp_path):
        """No fake/pre-populated evidence is accepted: an empty (never
        written) WS evidence file must still fail completeness, exactly
        as FIX 2 established -- this category fix changes nothing here."""
        rest_path = str(tmp_path / "rest.jsonl")
        ws_path = str(tmp_path / "ws.jsonl")  # never written to
        _write_rest_evidence_record(rest_path)

        result = tcr.verify_evidence_completeness(
            rest_evidence_path=rest_path, ws_evidence_path=ws_path,
            order_link_id="BB-1", evidence_capture_failed=False)

        assert result["ok"] is False
        assert result["ws_evidence_exists"] is False

    def test_verify_evidence_completeness_signature_unchanged(self):
        """Sanity check this fix did not alter FIX 2's function contract
        while touching the same file."""
        import inspect
        params = list(inspect.signature(tcr.verify_evidence_completeness).parameters)
        assert params == [
            "rest_evidence_path", "ws_evidence_path", "order_link_id",
            "evidence_capture_failed"]
