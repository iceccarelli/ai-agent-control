"""Restart/recovery across a REAL OS process boundary, not object
reconstruction inside the same interpreter.

WHY THIS FILE EXISTS
=====================
`tests/test_lifecycle_integration.py::TestChaos` proves the *recovery
mechanism itself* (`BybitClient.reconcile_on_startup`, `TradingEngine.
reconcile`) is correct, but every one of its "restart" scenarios rebuilds a
second `StateStore`/`BybitClient` inside the SAME Python process, against
the SAME `FakeBybit` object still sitting in memory. `tests/
test_orchestrator.py` proves `TradingBot.tick()`/`.run()` wire signal ->
execute correctly, but never combines that with a restart. No test in the
suite ever crosses an actual process boundary: nothing here has proven that
a fresh `python3` interpreter, sharing nothing with the process that died
except the on-disk SQLite file, actually recovers correctly.

That is a real gap, not a cosmetic one: an in-process "restart" can pass
for reasons that have nothing to do with durable recovery — a cached
attribute, a class-level default, module state left over from import time,
GC not having run yet. None of those survive `os.fork()` into a distinct
address space and a hard, cleanup-free `os._exit()`.

WHAT THIS FILE ACTUALLY PROVES
================================
Two "crash points" from `TestChaos`, replayed across a real subprocess
boundary:

1. A submitted entry order, killed before its acknowledgement landed. The
   post-crash process must resolve it from the exchange, not resubmit it.
2. A naked position left in the ledger. The post-crash process must find
   and protect it before anything else runs.

`multiprocessing.get_context("fork")` is used rather than `"spawn"`: fork
gives a genuinely separate OS process (separate PID, separate address
space, copy-on-write — killing it cannot touch the parent's memory or the
next process's), which is what "restart" needs to mean here, while staying
reliable under pytest's own import machinery (spawn re-imports test modules
by qualified name, which is fragile inside a test runner). The crash itself
uses `os._exit()` — no atexit, no `__del__`, no context-manager unwind —
because `StateStore._exec` commits synchronously per write (see
persistence.py), so anything the crash point is meant to have persisted
already has, and a "clean" exit would test something weaker than a real
kill.

The FakeBybit "venue" cannot itself survive a `fork`+`os._exit` the way a
real venue survives a bot process dying — a real exchange is a separate
system that does not go away, but the offline fake is: only an object. The
post-crash process is given a fresh `FakeBybit` reconstructed from a plain
dict snapshot of what process 1's venue held at the moment of the crash —
exactly what a real venue's honest answer would be, transferred over a
`multiprocessing.Queue` rather than shared Python memory, so the recovering
process derives everything from data, not from a lingering object.

STILL NOT COVERED (see NEXT MISSION in this session's stop packet): a
literal `python3 main.py` invocation killed by an external SIGKILL. That is
the maximally strict version of this test and remains a genuine gap; this
file closes the specific, narrower gap the audit named ("no subprocess, no
OS-level process kill/restart") without overclaiming it also covers
container-level orchestration.
"""
from __future__ import annotations

import multiprocessing
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TESTS = os.path.dirname(os.path.abspath(__file__))

EQUITY = 100_000.0


class LiveCfg:
    """Config double with paper mode OFF, so the order path actually runs —
    same shape as test_lifecycle_integration.py's LiveCfg, duplicated
    (not imported) so this file has no cross-test-module import coupling."""

    USE_TESTNET = True
    PAPER_TRADING = False
    BYBIT_API_KEY = "test-key"
    BYBIT_API_SECRET = "test-secret"
    BYBIT_RECV_WINDOW_MS = 5000
    REQUEST_TIMEOUT_SECONDS = 5.0
    USE_LEVERAGE = False
    SYMBOL_FILTERS_TTL = 3600
    ORDERLINK_PREFIX = "BB"
    MAX_POSITION_SIZE_PCT = 0.02
    MAX_TOTAL_EXPOSURE_PCT = 0.10
    MAX_DAILY_LOSS_PCT = 0.02
    MAX_DRAWDOWN_PCT = 0.10
    STOP_LOSS_PCT = 0.02
    RISK_PER_TRADE_PCT = 0.005
    MAX_OPEN_POSITIONS = 4
    MAX_CONSECUTIVE_LOSSES = 4
    MIN_RISK_REWARD_RATIO = 2.0
    MIN_CONFIDENCE = 0.60
    MAX_CORRELATION_EXPOSURE_PCT = 0.30
    MAX_LEVERAGE_EU = 1
    DEFAULT_LEVERAGE = 1
    TRADE_COOLDOWN_SECONDS = 60
    CIRCUIT_BREAKER_HOURS = 2.0
    BREAKEVEN_AFTER_FIRST_TP = True


def _import_path() -> None:
    """Every child process needs this — `fork` copies the parent's already-
    imported modules, but each test still calls this defensively so the
    file works under either start method."""
    if REPO not in sys.path:
        sys.path.insert(0, REPO)
    if TESTS not in sys.path:
        sys.path.insert(0, TESTS)


def _put_and_crash(out_queue, payload: dict) -> None:
    """Hand `payload` to the parent, then crash hard.

    `multiprocessing.Queue.put` is asynchronous — it hands the object to a
    background feeder thread that serialises it onto the pipe. `os._exit`
    called immediately after `put` can win that race and kill the process
    before the feeder thread ever runs, silently losing the payload. The
    queue is pure test instrumentation (a real crash has no such side
    channel), so it is flushed here before the actual, unrecoverable exit —
    everything from this point on is what the simulated crash prevented.
    """
    out_queue.put(payload)
    out_queue.close()
    out_queue.join_thread()
    os._exit(0)


def _venue_snapshot(exchange) -> dict:
    """A plain, picklable dict of everything the venue currently holds —
    the only thing that crosses the process boundary to the recovering
    process, standing in for "what a real exchange would honestly answer"."""
    return {
        "balances": dict(exchange.balances),
        "orders": {k: dict(v) for k, v in exchange.orders.items()},
        "positions": {k: dict(v) for k, v in exchange.positions.items()},
    }


def _venue_from_snapshot(snapshot: dict):
    from fake_bybit import FakeBybit

    exchange = FakeBybit(balances=dict(snapshot["balances"]), equity=EQUITY)
    exchange.orders = {k: dict(v) for k, v in snapshot["orders"].items()}
    exchange.positions = {k: dict(v) for k, v in snapshot["positions"].items()}
    return exchange


def _proc1_submit_unacked_entry(db_path: str, out_queue) -> None:
    """Runs in its own OS process: submits one entry order, marks it
    'pending' (as if the acknowledgement never arrived), and crashes hard —
    `os._exit` — with no further cleanup. `out_queue.put` happens BEFORE the
    crash point deliberately: everything after it is what did not happen."""
    _import_path()
    import bybit_connection as bc
    from persistence import StateStore

    from fake_bybit import FakeBybit
    exchange = FakeBybit(balances={"USDT": 100_000.0, "BTC": 5.0}, equity=EQUITY)
    store = StateStore(db_path)
    client = bc.BybitClient(config=LiveCfg(), store=store, transport=exchange)

    result = client.place_order(symbol="BTCUSDT", side="Buy", qty=0.01)
    store.update_order_status(result.order_link_id, "pending")

    _put_and_crash(out_queue, {
        "ok": bool(result.ok),
        "order_link_id": result.order_link_id,
        "venue": _venue_snapshot(exchange),
    })  # simulated kill -9: no atexit, no __del__, no store.close()


def _proc2_recover_from_unacked_entry(db_path: str, venue_snapshot: dict,
                                      out_queue) -> None:
    """A genuinely fresh process: reopens the on-disk store and reconciles
    against a venue rebuilt only from the snapshot — no object from
    process 1 is reachable here."""
    _import_path()
    import bybit_connection as bc
    from persistence import StateStore

    exchange = _venue_from_snapshot(venue_snapshot)
    store = StateStore(db_path)
    client = bc.BybitClient(config=LiveCfg(), store=store, transport=exchange)
    summary = client.reconcile_on_startup()
    store.close()
    _put_and_crash(out_queue, {
        "summary": summary,
        "entry_orders_on_venue": len(exchange.submitted_orders("entry")),
    })


def _proc1_leave_naked_position(db_path: str, out_queue) -> None:
    """A different crash point: the ledger already believes there is an
    open position with no stop (e.g. the process died between fill and
    protective-stop placement), then crashes before doing anything else."""
    _import_path()
    from persistence import StateStore
    from fake_bybit import FakeBybit

    exchange = FakeBybit(balances={"USDT": 100_000.0, "BTC": 5.0}, equity=EQUITY)
    exchange.positions["BTCUSDT"] = {
        "symbol": "BTCUSDT", "side": "Buy", "size": "0.01",
        "avgPrice": "50000", "stopLoss": "0",
    }
    store = StateStore(db_path)
    store.update_equity(EQUITY)
    store.upsert_position("BTCUSDT", "Buy", 0.01, 50_000.0, stop_price=0.0)

    _put_and_crash(out_queue, {"venue": _venue_snapshot(exchange)})


def _proc2_recover_naked_position(db_path: str, venue_snapshot: dict,
                                  out_queue) -> None:
    _import_path()
    import bybit_connection as bc
    import trading_engine as te
    from persistence import StateStore
    from risk_management import BillionaireRiskManager

    exchange = _venue_from_snapshot(venue_snapshot)
    store = StateStore(db_path)
    client = bc.BybitClient(config=LiveCfg(), store=store, transport=exchange)
    risk = BillionaireRiskManager(config=LiveCfg(), store=store)
    engine = te.TradingEngine(
        client=client, risk_manager=risk, store=store, config=LiveCfg())
    engine.reconcile()
    remaining = store.positions_without_stops()
    store.close()
    _put_and_crash(out_queue, {
        "naked_positions_remaining": remaining,
        "stop_orders_on_venue": len(exchange.submitted_orders("stop")),
    })


def _run(target, args) -> dict:
    ctx = multiprocessing.get_context("fork")
    queue = ctx.Queue()
    proc = ctx.Process(target=target, args=(*args, queue))
    proc.start()
    result = queue.get(timeout=30)
    proc.join(timeout=10)
    assert proc.exitcode == 0, f"child process exited abnormally: {proc.exitcode}"
    return result


@pytest.mark.skipif(
    multiprocessing.get_start_method(allow_none=True) not in (None, "fork")
    and "fork" not in multiprocessing.get_all_start_methods(),
    reason="fork start method unavailable on this platform")
class TestRealProcessRestart:
    def test_unacked_entry_is_not_duplicated_after_a_real_process_crash(
            self, tmp_path):
        """The literal TestChaos scenario, but process 1 and process 2 are
        actually different OS processes sharing nothing but the sqlite
        file and a snapshot of the venue's own answer."""
        db_path = str(tmp_path / "state.db")

        r1 = _run(_proc1_submit_unacked_entry, (db_path,))
        assert r1["ok"] is True

        r2 = _run(_proc2_recover_from_unacked_entry, (db_path, r1["venue"]))

        assert r2["summary"]["resolved"] == 1
        assert r2["entry_orders_on_venue"] == 1, (
            "the recovering process resubmitted the order instead of "
            "resolving the existing one from the venue")

    def test_naked_position_is_protected_after_a_real_process_crash(
            self, tmp_path):
        db_path = str(tmp_path / "state.db")

        r1 = _run(_proc1_leave_naked_position, (db_path,))

        r2 = _run(_proc2_recover_naked_position, (db_path, r1["venue"]))

        assert r2["naked_positions_remaining"] == [], (
            "a position survived the crash unprotected")
        assert r2["stop_orders_on_venue"] == 1

    def test_third_process_sees_the_second_processs_writes(self, tmp_path):
        """Durability, chained: what process 2 persists must be visible to
        a process 3 that never ran alongside either — proves this is real
        disk state, not a fluke of one particular process pairing."""
        db_path = str(tmp_path / "state.db")

        r1 = _run(_proc1_submit_unacked_entry, (db_path,))
        _run(_proc2_recover_from_unacked_entry, (db_path, r1["venue"]))

        def _proc3_reread(db_path, out_queue):
            _import_path()
            from persistence import StateStore
            store = StateStore(db_path)
            _put_and_crash(out_queue, {"pending": store.pending_orders(),
                                       "unresolved": store.unresolved_orders()})

        r3 = _run(_proc3_reread, (db_path,))
        assert r3["unresolved"] == [], (
            "process 2's resolution was not durably visible to process 3")
