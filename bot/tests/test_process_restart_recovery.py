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

EXIT-SIDE CRASH BOUNDARIES (added: prior coverage above proves only the
ENTRY side -- intent/order/ack/fill/position/protection -- survives a real
process crash; the EXIT side -- exit order submitted, exit fill observed,
trade record persisted, position/evidence state finalised -- had none).
`TestExitCrashRecovery` below closes that gap for the highest-risk boundary:
a crash between `TradingEngine.record_exit_fill`'s `store.record_trade`
call and its `store.remove_position`/`upsert_position` call. Both are
separate synchronous commits (see `StateStore._exec`), so a real kill
between them is exactly reproducible by letting the first process run the
real, unmocked call through to the first commit and then writing back the
position row it was about to remove -- the same technique
`_proc1_leave_naked_position` above already uses to represent "the crash
already happened at this exact point" using real store state, not a mock.
The invariant proven: replaying `observe_exits()`/`record_exit_fill()` from
a genuinely fresh OS process, given the same still-open position row and
the same venue closed-pnl history, books the trade at most ONCE (no
duplicate row, no double-counted `daily_anchor.realised_pnl` -- see
`StateStore.record_trade`'s docstring) and still finishes the position's
removal that the crash interrupted, rather than leaving it orphaned open
forever.
"""
from __future__ import annotations

import multiprocessing
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TESTS = os.path.dirname(os.path.abspath(__file__))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from persistence import StateStore  # noqa: E402

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
        # Linear-only /v5/position/closed-pnl history -- empty for every
        # existing (spot) test above; included so the exit-crash tests below
        # can hand the recovering process the SAME closed-pnl rows the first
        # process saw, exactly as a real venue's history would still answer
        # after the bot process (not the exchange) restarts.
        "closed_pnl": [dict(r) for r in getattr(exchange, "closed_pnl", [])],
    }


def _venue_from_snapshot(snapshot: dict):
    from fake_bybit import FakeBybit

    exchange = FakeBybit(balances=dict(snapshot["balances"]), equity=EQUITY)
    exchange.orders = {k: dict(v) for k, v in snapshot["orders"].items()}
    exchange.positions = {k: dict(v) for k, v in snapshot["positions"].items()}
    exchange.closed_pnl = [dict(r) for r in snapshot.get("closed_pnl") or []]
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


class LinearLiveCfg(LiveCfg):
    """`observe_exits`/`record_exit_fill` are linear-only -- see
    `trading_engine.py`'s docstring for why (spot holdings are balances, not
    positions)."""

    CATEGORY = "linear"
    ALLOW_SHORTS = True


def _build_linear_engine(db_path: str, exchange):
    import bybit_connection as bc
    import trading_engine as te
    from persistence import StateStore
    from risk_management import BillionaireRiskManager

    store = StateStore(db_path)
    cfg = LinearLiveCfg()
    client = bc.BybitClient(config=cfg, store=store, transport=exchange)
    risk = BillionaireRiskManager(config=cfg, store=store)
    engine = te.TradingEngine(
        client=client, risk_manager=risk, store=store, config=cfg)
    return engine, store


def _proc1_exit_fill_crash_before_position_removed(db_path: str, out_queue) -> None:
    """Runs the REAL `observe_exits()` -> `record_exit_fill()` ->
    `store.record_trade()` + `store.remove_position()` sequence to
    completion (nothing mocked), then reproduces the exact disk state a hard
    kill BETWEEN those two commits would have left: the trade is durably
    recorded, but the position row `remove_position` just deleted is written
    straight back — precisely what a `kill -9` landing after the first
    commit and before the second would leave on disk, using the same
    technique `_proc1_leave_naked_position` above uses to represent a crash
    point with real store state. Then it crashes hard."""
    _import_path()
    import time as _time
    from fake_bybit import FakeBybit

    exchange = FakeBybit(balances={"USDT": 100_000.0}, equity=EQUITY)
    # Venue is flat (fully exited already) and the closed-pnl history is the
    # honest record of that exit — exactly `tests/test_observe_exits.py::
    # test_stop_fill_at_venue_is_booked_at_closed_pnl_price`'s setup.
    exchange.closed_pnl.append({
        "symbol": "BTCUSDT", "side": "Buy", "qty": "0.01",
        "avgExitPrice": "61000", "closedPnl": "-10.6", "orderId": "ORD-1",
        "updatedTime": str(int(_time.time() * 1000) + 1_000),
    })
    engine, store = _build_linear_engine(db_path, exchange)
    store.update_equity(EQUITY)
    store.upsert_position("BTCUSDT", "Sell", 0.01, 60_000.0, stop_price=61_000.0)

    summary = engine.observe_exits()
    trades_before_crash = len(store._query("SELECT * FROM trades"))
    anchor_before_crash = store.ensure_daily_anchor(EQUITY).realised_pnl

    # Reproduce the crash point: `remove_position` above already committed
    # the DELETE for real; write the row back to represent a kill landing
    # before that commit reached disk (record_trade's commit, one line
    # earlier in record_exit_fill, already has).
    store.upsert_position("BTCUSDT", "Sell", 0.01, 60_000.0, stop_price=61_000.0)

    _put_and_crash(out_queue, {
        "observe_exits_summary": summary,
        "trades_before_crash": trades_before_crash,
        "anchor_before_crash": anchor_before_crash,
        "venue": _venue_snapshot(exchange),
    })


def _proc2_exit_fill_recover(db_path: str, venue_snapshot: dict, out_queue) -> None:
    """A genuinely fresh process, fresh `TradingEngine`, fresh `FakeBybit`
    built only from process 1's venue snapshot (same closed-pnl history,
    same flat position). Replays exactly the next scheduled cycle would:
    `observe_exits()` again, against the position row the crash left open."""
    _import_path()
    exchange = _venue_from_snapshot(venue_snapshot)
    engine, store = _build_linear_engine(db_path, exchange)

    summary = engine.observe_exits()

    _put_and_crash(out_queue, {
        "observe_exits_summary": summary,
        "trades_after_replay": len(store._query("SELECT * FROM trades")),
        "anchor_after_replay": store.ensure_daily_anchor(EQUITY).realised_pnl,
        "open_position_count_after_replay": store.open_position_count(),
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


@pytest.mark.skipif(
    multiprocessing.get_start_method(allow_none=True) not in (None, "fork")
    and "fork" not in multiprocessing.get_all_start_methods(),
    reason="fork start method unavailable on this platform")
class TestExitCrashRecovery:
    """The EXIT side of the lifecycle, across a real OS process crash.

    Prior coverage in this file (`TestRealProcessRestart`) proves entry-side
    crash boundaries -- order submitted, ack pending, naked position -- are
    handled correctly by a genuinely fresh process. Nothing proved the same
    for the exit side: exit fill observed -> trade persisted -> position
    state finalised. The crash point exercised here is exactly that last
    gap: a kill between `record_exit_fill`'s `store.record_trade()` commit
    and its `store.remove_position()` commit.
    """

    def test_exit_fill_is_not_double_booked_after_a_real_process_crash(
            self, tmp_path):
        db_path = str(tmp_path / "state.db")

        r1 = _run(_proc1_exit_fill_crash_before_position_removed, (db_path,))
        assert r1["trades_before_crash"] == 1
        # -10 gross, 0.60 apportioned entry-fee estimate and 0.61 exit-fee
        # estimate (61_000 x 0.01 x 0.001). observe_exits has no venue fee to read,
        # so the exit leg is a labelled estimate, no longer a silent 0.0.
        assert r1["anchor_before_crash"] == pytest.approx(-11.21)

        r2 = _run(_proc2_exit_fill_recover, (db_path, r1["venue"]))

        assert r2["trades_after_replay"] == 1, (
            "a real process crash between booking the trade and removing "
            "the position it closed caused the replayed exit to be booked "
            "TWICE after restart")
        assert r2["anchor_after_replay"] == pytest.approx(r1["anchor_before_crash"]), (
            "daily_anchor.realised_pnl was double-counted by the replayed "
            "exit -- this corrupts the daily-loss/drawdown gates even if "
            "the trades table itself looked deduplicated")
        assert r2["open_position_count_after_replay"] == 0, (
            "the position the crash interrupted removing was left "
            "orphaned open forever instead of being finished off by the "
            "replay")

    def test_a_genuinely_different_exit_is_still_booked_after_a_crash(
            self, tmp_path):
        """Negative control: the dedupe guard must not swallow a REAL second
        exit that merely happens to follow a crash-recovered one -- only an
        exact replay of the same venue exit (same closed-pnl orderId) is a
        no-op, never a new, distinct one."""
        db_path = str(tmp_path / "state.db")

        r1 = _run(_proc1_exit_fill_crash_before_position_removed, (db_path,))

        def _proc2_new_position_then_new_exit(db_path, venue_snapshot, out_queue):
            _import_path()
            import time as _time
            exchange = _venue_from_snapshot(venue_snapshot)
            engine, store = _build_linear_engine(db_path, exchange)

            # Recover the crash-interrupted exit first, exactly as the real
            # next cycle would.
            engine.observe_exits()

            # A brand-new, unrelated position and a brand-new venue exit --
            # different symbol, different closed-pnl orderId.
            store.upsert_position("ETHUSDT", "Buy", 1.0, 2_000.0, stop_price=1_900.0)
            exchange.closed_pnl.append({
                "symbol": "ETHUSDT", "side": "Sell", "qty": "1.0",
                "avgExitPrice": "2100", "closedPnl": "100", "orderId": "ORD-2",
                "updatedTime": str(int(_time.time() * 1000) + 2_000),
            })
            engine.observe_exits()

            _put_and_crash(out_queue, {
                "trades": len(store._query("SELECT * FROM trades")),
                "open_position_count": store.open_position_count(),
            })

        r2 = _run(_proc2_new_position_then_new_exit, (db_path, r1["venue"]))
        assert r2["trades"] == 2, (
            "a genuinely different exit was wrongly deduplicated against "
            "the crash-recovered one")
        assert r2["open_position_count"] == 0


class TestWSStatusMonotonicGuard:
    """`StateStore.update_order_status(..., source="ws")`'s guard, exercised
    against the real on-disk store (not a stub) -- this is the property gap
    a prior audit cycle flagged: a stale/out-of-order WS event, possible on
    reconnect replay since `private_ws_consumer._Deduplicator` is an
    in-memory LRU that a restart resets, must not regress a terminal order's
    status. `source="rest"` (the default, what `BybitClient` uses) must stay
    completely unaffected, since REST is the venue's own authoritative
    answer, not a replayed observation.
    """

    def _store(self, tmp_path) -> StateStore:
        store = StateStore(str(tmp_path / "state.db"))
        store.claim_writer()
        store.record_order("BB-1", "BTCUSDT", "Buy", "Market", 0.01)
        return store

    def test_stale_ws_new_after_filled_does_not_regress(self, tmp_path):
        store = self._store(tmp_path)
        store.update_order_status("BB-1", "filled", exchange_id="V-1", source="ws")

        # A replayed/out-of-order "New" delivered after the terminal
        # "Filled" -- e.g. resent by the venue on reconnect, or delivered
        # out of order across independent delivery paths.
        store.update_order_status("BB-1", "submitted", exchange_id="V-1", source="ws")

        assert store.get_order("BB-1")["status"] == "filled", (
            "a stale WS 'New' regressed a terminal order's status")

    def test_duplicate_terminal_ws_event_is_a_harmless_noop(self, tmp_path):
        """Re-delivery of the SAME terminal status (a normal duplicate, not
        an attack) must apply cleanly with no error and no anomaly journal
        entry."""
        store = self._store(tmp_path)
        store.update_order_status("BB-1", "filled", exchange_id="V-1", source="ws")
        store.update_order_status("BB-1", "filled", exchange_id="V-1", source="ws")

        assert store.get_order("BB-1")["status"] == "filled"
        anomalies = [
            d for d in store.recent_decisions()
            if d["decision"] == "WS_STATUS_REGRESSION_BLOCKED"
        ]
        assert anomalies == [], "a genuine duplicate must not be journalled"

    def test_legitimate_advancement_still_applies_new_partial_filled(self, tmp_path):
        """New -> PartiallyFilled -> Filled, delivered in the correct order,
        must not be blocked by the guard -- it only ever blocks a move OUT
        OF a terminal status."""
        store = self._store(tmp_path)
        store.update_order_status("BB-1", "submitted", exchange_id="V-1", source="ws")
        assert store.get_order("BB-1")["status"] == "submitted"

        store.update_order_status("BB-1", "partial", exchange_id="V-1", source="ws")
        assert store.get_order("BB-1")["status"] == "partial"

        store.update_order_status("BB-1", "filled", exchange_id="V-1", source="ws")
        assert store.get_order("BB-1")["status"] == "filled"

        assert [
            d for d in store.recent_decisions()
            if d["decision"] == "WS_STATUS_REGRESSION_BLOCKED"
        ] == []

    def test_stale_partial_after_cancelled_does_not_regress(self, tmp_path):
        store = self._store(tmp_path)
        store.update_order_status("BB-1", "cancelled", exchange_id="V-1", source="ws")
        store.update_order_status("BB-1", "partial", exchange_id="V-1", source="ws")

        assert store.get_order("BB-1")["status"] == "cancelled", (
            "a stale WS 'PartiallyFilled' regressed a cancelled order")

    def test_blocked_regression_is_journalled_and_inspectable(self, tmp_path):
        store = self._store(tmp_path)
        store.update_order_status("BB-1", "rejected", exchange_id="V-1", source="ws")
        store.update_order_status("BB-1", "submitted", exchange_id="V-1", source="ws")

        anomalies = [
            d for d in store.recent_decisions()
            if d["decision"] == "WS_STATUS_REGRESSION_BLOCKED"
        ]
        assert len(anomalies) == 1
        import json as _json
        detail = _json.loads(anomalies[0]["detail"])
        assert detail["order_link_id"] == "BB-1"
        assert detail["from_status"] == "rejected"
        assert detail["to_status"] == "submitted"
        assert anomalies[0]["symbol"] == "BTCUSDT"

    def test_rest_reconciliation_is_unaffected_by_the_ws_guard(self, tmp_path):
        """REST (`source="rest"`, the default) must remain free to correct
        any status, including what would be a "regression" for a WS-sourced
        write -- it is the venue's own authoritative answer, e.g.
        `reconcile_on_startup` discovering the exchange actually cancelled
        an order the local ledger still shows as filled from a bad fill
        poll, or any other terminal-to-terminal correction."""
        store = self._store(tmp_path)
        store.update_order_status("BB-1", "filled", exchange_id="V-1", source="ws")

        store.update_order_status("BB-1", "cancelled", exchange_id="V-1")  # source="rest" default
        assert store.get_order("BB-1")["status"] == "cancelled"

        store.update_order_status("BB-1", "submitted", exchange_id="V-1", source="rest")
        assert store.get_order("BB-1")["status"] == "submitted"

        assert [
            d for d in store.recent_decisions()
            if d["decision"] == "WS_STATUS_REGRESSION_BLOCKED"
        ] == []
