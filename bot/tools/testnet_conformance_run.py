#!/usr/bin/env python3
"""Bounded, deterministic Bybit TESTNET conformance run.

WHAT THIS IS AND IS NOT
=========================
This runs the EXACT production lifecycle — `main.TradingBot.startup()` /
`.tick()` -> `trading_engine.TradingEngine.execute()` -> the real
`bybit_connection.BybitClient` -> the real `Transport` -> the real Bybit
testnet host — never a standalone script that sends venue commands the
application itself does not use. If this tool's `run_conformance` diverges
from what `TradingBot.tick()` actually does, that is a bug in this tool,
not a second lifecycle to maintain.

The only thing this tool adds on top of the production path is:
(1) a fail-closed preflight (`run_preflight`) that a normal trading run
never needs, because production is never pointed at testnet by a human
mid-conformance-run; and (2) wiring the transport through
`venue_evidence.EvidenceCapturingTransport` so every real request/response
is captured automatically.

FAIL CLOSED — NOTHING IS SENT UNLESS ALL OF THESE PASS
=========================================================
  environment     BYBIT_VENUE must be exactly "testnet". Never mainnet,
                  never demo, never unset. PAPER_TRADING must be exactly
                  False — a paper "fill" proves nothing about the venue.
  credentials     BYBIT_API_KEY / BYBIT_API_SECRET must both be non-empty.
                  Never assumed present.
  venue_identity  The CLIENT'S ACTUAL base URL (bybit_connection.py's own
                  VENUE_REST table, read off the constructed client, not
                  the config value that was SUPPOSED to have produced it)
                  must resolve to the testnet host. This is the same law
                  venue_evidence.validate_environment_claim enforces on
                  evidence: a config claim is not a fact until the thing
                  it is supposed to control is checked directly.
  connectivity    An actual network call (BybitClient.sync_time(), the
                  cheapest real endpoint) must succeed.
  account_state   The wallet must be readable.

Even with `--arm --i-am-human`, a failed preflight check refuses. Preflight
runs before every conformance attempt; there is no flag that skips it.

CONFIRMED BLOCKED IN THIS CONTAINER (2026-09-29)
===================================================
No BYBIT_API_KEY/BYBIT_API_SECRET are set here, and this container's
network policy refuses api-testnet.bybit.com outright (a direct `curl`
check returned "CONNECT tunnel failed, response 403" — not inferred, not
assumed). Running `main()` here will refuse at `credentials` and
`connectivity` and go no further. That refusal is this tool doing its job,
not a bug to route around. `run_preflight`/`run_conformance` are still
fully unit-tested against `FakeBybit` (see
tests/test_testnet_conformance_run.py) so the logic is proven; only the
real network leg is blocked, and only here.

WHAT ONE RUN DOES (once preflight passes and `--arm --i-am-human` are
both given)
=========================================================================
  1. TradingBot.startup() — REST reconciliation, then (this tool forces
                             `ws_enabled_override=True`) the private-WS
                             observer STARTS asynchronously — the same
                             `_start_private_ws` production uses, not a
                             separate WS path.
  2. WS readiness           — bounded wait (`--ws-ready-timeout`,
                             `await_ws_ready`) for the observer thread to
                             have actually reached AUTH_OK and
                             SUBSCRIBE_OK. `startup()` returns before that
                             thread necessarily gets there, so "the
                             consumer object exists" is never treated as
                             "ready".
  3. TradingBot.tick()     — one cycle: a bounded one-shot strategy signals
                             BUY once, `engine.execute()` submits the order,
                             observes the fill, places and verifies the
                             protective stop — all synchronous, all real.
  4. WS observation        — bounded wait (`--ws-observation-timeout`,
                             `await_ws_observation`) for the WS observer
                             to actually capture an order or execution
                             evidence record for THIS order's
                             `order_link_id` — proof the private stream
                             observed the same real order, not merely that
                             REST did. Each poll also DRAINS the
                             consumer's pending state/evidence queues onto
                             this (the writer) thread via the existing
                             `drain_and_apply()` — a real WS event that
                             arrives after `tick()` already returned has
                             nowhere else to be applied/written from
                             before the next real tick, which this bounded
                             run never takes. ASSURANCE mode (this tool
                             always uses it): missing WS observation is a
                             failed run, not a soft warning.
  5. Protection read-back  — `verify_protection()`, category-dispatched:
                             LINEAR reads `client.get_position()` FRESH
                             (not the local ledger) for `stopLoss > 0`;
                             SPOT (get_position is linear-only) reads the
                             separate protective stop ORDER back via the
                             existing `client.verify_stop()`.
  6. Flatten               — `engine.close_position()`, the same method
                             production uses to exit.
  7. Remote flat verification — `verify_remote_flat()`: a FRESH venue
                             read proving the account is flat, never
                             inferred from `close_position()` returning
                             `ok` or from local `StateStore` having
                             removed the position. LINEAR re-reads
                             `get_position()` for `size <= 0`; SPOT checks
                             no open orders remain and the base-asset
                             balance is below the instrument's own
                             minimum tradeable quantity.
  8. Final reconciliation  — `client.reconcile_on_startup()`.
  9. Final WS flush        — `finalize_ws_lifecycle()`: stop the WS
                             thread, JOIN it, then one last
                             `drain_and_apply()` of anything it received
                             but that no poll of the bounded observation
                             wait happened to catch — before the store
                             closes or evidence is verified, so a real,
                             already-received observation can never
                             disappear at shutdown.
  10. Evidence verification — BOTH evidence chains (REST's, via
                             EvidenceCapturingTransport; WS's, via
                             WSPrivateConsumer's assurance-mode capture)
                             are hash-chain-verified, AND their actual
                             content is checked (`verify_evidence_
                             completeness`) — a clean chain around missing
                             evidence is not proof. A tamper, gap,
                             missing record, or capture failure fails the
                             run — see "ASSURANCE MODE" below.
  11. Shutdown + result    — `bot.shutdown()`; a machine-readable summary
                             (stage-by-stage outcome, latencies, both
                             evidence paths, both chain-verification
                             results) is written to `--out`.

ASSURANCE MODE — NO "BEST EFFORT THEREFORE GREEN"
====================================================
This tool always constructs its `WSPrivateConsumer` with
`assurance_mode=True` (see private_ws_consumer.py's "EVIDENCE FAILURE
SEMANTICS"). That changes nothing about HOW capture failures are
handled at capture time — they still never raise mid-receive-loop — but
it changes what THIS RUNNER does with `evidence_capture_failed`
afterward: normal trading (main.TradingBot, assurance_mode=False)
degrades and keeps trading; a conformance run fails closed. `result["ok"]`
is False if ANY of: a stage failed, the WS observer never captured
observable evidence for this order, `evidence_capture_failed` is set, or
either evidence chain fails `verify_chain`. Partial evidence is reported
as partial (`result["ws_evidence_complete"]`), never silently treated as
success.

REAL PROCESS RESTART: a separate, deliberate exercise
========================================================
This tool does not kill itself mid-run — a real crash against a real
funded testnet account is not something to automate blindly. The restart
property is instead exercised the same way
tests/test_orchestrator_process_restart.py proves it offline: run this
tool once with `--state-db PATH`, let it reach a chosen point, kill the
process by hand (or let a step raise), then run it again with the SAME
`--state-db`. `TradingBot.startup()`'s reconciliation and the
`POSITION_ALREADY_OPEN` gate are the same durable-state mechanism either
way — this tool does not reimplement them.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
BOT = os.path.dirname(HERE)
sys.path.insert(0, BOT)
sys.path.insert(0, HERE)

import venue_evidence as ve  # noqa: E402

logger = logging.getLogger("testnet_conformance_run")

DEFAULT_EVIDENCE_PATH = os.path.join(
    BOT, "artifacts", "testnet_conformance_evidence.jsonl")
DEFAULT_WS_EVIDENCE_PATH = os.path.join(
    BOT, "artifacts", "testnet_conformance_ws_evidence.jsonl")
DEFAULT_STATE_DB = os.path.join(
    BOT, "artifacts", "testnet_conformance_state.db")
DEFAULT_RESULT_PATH = os.path.join(
    BOT, "artifacts", "testnet_conformance_result.json")
SYMBOL = "BTCUSDT"
#: Bounded wait for the WS observer to capture an order/execution record
#: for the order this run just submitted. Bounded because a conformance
#: run must terminate, not hang on a stream that never confirms —
#: mission requirement "bounded WS observation timeout".
DEFAULT_WS_OBSERVATION_TIMEOUT_SECONDS = 30.0
#: Bounded wait for the WS observer to reach AUTH_OK/SUBSCRIBE_OK.
#: `TradingBot._start_private_ws()` starts the observer thread and
#: returns immediately — auth/subscribe happen asynchronously on that
#: thread, so reading `auth_ok`/`subscribe_ok` right after `startup()`
#: returns is a race, not a check (see FIX 1, this review round).
DEFAULT_WS_READY_TIMEOUT_SECONDS = 15.0


class ConformanceRefused(RuntimeError):
    """A preflight or execution invariant failed. Refuse, do not proceed."""


# ---------------------------------------------------------------------------
# preflight — every check independently re-derived, none trusted from config
# ---------------------------------------------------------------------------


def verify_environment(cfg: Any) -> None:
    venue = str(getattr(cfg, "BYBIT_VENUE", "") or "")
    if venue != "testnet":
        raise ConformanceRefused(
            f"BYBIT_VENUE={venue!r}; this runner arms on testnet only — "
            "never mainnet, never demo, never unset")
    if bool(getattr(cfg, "PAPER_TRADING", True)):
        raise ConformanceRefused(
            "PAPER_TRADING is true; a paper fill is not testnet "
            "conformance evidence")


def verify_credentials(cfg: Any) -> None:
    key = str(getattr(cfg, "BYBIT_API_KEY", "") or "")
    secret = str(getattr(cfg, "BYBIT_API_SECRET", "") or "")
    if not key or not secret:
        raise ConformanceRefused(
            "BYBIT_API_KEY/BYBIT_API_SECRET are not both set; testnet "
            "credentials are required and never assumed present")


def verify_venue_identity(client: Any) -> None:
    """The client's ACTUAL base URL, not the config value that was
    supposed to have produced it — the same law
    venue_evidence.infer_environment_from_url enforces on evidence."""
    base_url = str(getattr(client, "base_url", "") or "")
    actual = ve.infer_environment_from_url(base_url)
    if actual != "testnet":
        raise ConformanceRefused(
            f"the client's actual base URL ({base_url!r}) resolves to "
            f"{actual!r}, not testnet — refusing rather than trusting "
            "BYBIT_VENUE alone")


def verify_connectivity(client: Any) -> None:
    try:
        client.sync_time()
    except Exception as exc:  # noqa: BLE001
        raise ConformanceRefused(
            f"testnet unreachable: {type(exc).__name__}: {exc}") from exc


def verify_account_state(client: Any) -> None:
    try:
        wallet = client.get_wallet()
    except Exception as exc:  # noqa: BLE001
        raise ConformanceRefused(
            f"account/wallet unreadable: {type(exc).__name__}: {exc}") from exc
    if not wallet:
        raise ConformanceRefused("wallet response was empty")


PREFLIGHT_CHECKS: Tuple[Tuple[str, Callable[..., None], str], ...] = (
    ("environment", verify_environment, "cfg"),
    ("credentials", verify_credentials, "cfg"),
    ("venue_identity", verify_venue_identity, "client"),
    ("connectivity", verify_connectivity, "client"),
    ("account_state", verify_account_state, "client"),
)


@dataclass
class PreflightResult:
    ok: bool
    checks: "Dict[str, str]" = field(default_factory=dict)


def run_preflight(cfg: Any, client: Any) -> PreflightResult:
    """Every check runs and is recorded even after the first failure — an
    operator refused at 'credentials' should not have to fix that, re-run,
    and then discover 'connectivity' was also going to fail."""
    checks: Dict[str, str] = {}
    ok = True
    args_by_name = {"cfg": cfg, "client": client}
    for name, fn, arg_name in PREFLIGHT_CHECKS:
        try:
            fn(args_by_name[arg_name])
            checks[name] = "ok"
        except ConformanceRefused as exc:
            checks[name] = str(exc)
            ok = False
    return PreflightResult(ok=ok, checks=checks)


# ---------------------------------------------------------------------------
# the bounded run itself — the production lifecycle, called, not reimplemented
# ---------------------------------------------------------------------------


class _OneShotBuyStrategy:
    """Signals BUY exactly once, then HOLD forever. A conformance run
    proves one bounded lifecycle, not an open-ended trading session."""

    def __init__(self, *, entry_price: float, stop_price: float,
                take_profit: float, confidence: float = 0.8) -> None:
        self._entry_price = entry_price
        self._stop_price = stop_price
        self._take_profit = take_profit
        self._confidence = confidence
        self._fired = False

    def signal_for(self, symbol: str):
        import trading_engine as te

        if self._fired:
            return te.TradeIntent(symbol=symbol, signal_type="HOLD",
                                  entry_price=0.0, stop_price=0.0)
        self._fired = True
        return te.TradeIntent(
            symbol=symbol, signal_type="BUY",
            entry_price=self._entry_price, stop_price=self._stop_price,
            take_profits=((self._take_profit, 1.0),),
            confidence=self._confidence,
        )


def _read_evidence_records(path: Optional[str]) -> List[Dict[str, Any]]:
    if not path or not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as handle:
        return [json.loads(ln) for ln in handle if ln.strip()]


def _stop_order_link_id(row: Dict[str, Any]) -> str:
    """The protective stop's OWN `order_link_id`, as recorded by
    `TradingEngine.execute()` into the position row's `meta` JSON
    (`{"stop_order_link_id": ...}` — see `persistence.StateStore.
    upsert_position`). Needed for SPOT protection readback: on spot the
    stop is a separate resting order, identified by its own link id, not
    a field on a position (see `BybitClient.verify_stop`'s category
    dispatch). Never raises; a malformed/absent `meta` yields "" rather
    than fabricating an id.
    """
    try:
        meta = json.loads(row.get("meta") or "{}")
    except (TypeError, ValueError):
        return ""
    return str(meta.get("stop_order_link_id") or "")


def verify_protection(*, client: Any, store: Any, symbol: str,
                      entry_row: Dict[str, Any]) -> Dict[str, Any]:
    """Category-correct protection readback — the venue itself, not local
    state, reporting the protection mechanism as live for THIS run's
    order.

    Dispatches on `client.is_linear` (the same flag `BybitClient` and
    `TradingEngine` already use everywhere else to pick a mechanism —
    see `verify_stop`, `check_naked_positions`, `observe_exits`; this is
    not a new distinction, just this runner finally respecting the one
    that already exists):

    LINEAR — retains the original mechanism: `client.get_position(symbol)`
    read back fresh from `/v5/position/list`, `stopLoss > 0`.

    SPOT — `get_position()` is linear-only (raises `PermanentAPIError`
    if called) and a spot "position" has no `stopLoss` field to read in
    the first place: the protection is the separate conditional stop
    ORDER `TradingEngine._protect()` already placed. This calls the
    EXISTING `client.verify_stop(symbol=symbol, order_link_id=...)` —
    already written, already used by `check_naked_positions`/
    `reconcile_on_startup` — which reads that order back via
    `get_order()` and reports it live only if its `orderStatus` is
    `Untriggered`/`New`. No second spot-protection implementation is
    introduced here.

    Never raises; any failure (missing stop id, venue error, not live)
    comes back as `ok: False` with a `reason`/`detail`, matching this
    tool's "a failed stage is data, not an exception" convention.
    """
    category = "linear" if bool(getattr(client, "is_linear", False)) else "spot"
    if category == "linear":
        try:
            remote_position = client.get_position(symbol)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "category": category, "error": str(exc)}
        remote_stop = float((remote_position or {}).get("stopLoss", 0) or 0)
        return {"ok": remote_stop > 0, "category": category,
                "remote_stop": remote_stop}

    # SPOT: re-read the position fresh (never trust the row captured at
    # "entry" time — a partial exit could have re-sized/replaced the stop
    # since) for the CURRENT stop_order_link_id, then ask the venue
    # whether that exact order is still live.
    positions_now = {p["symbol"]: p for p in store.open_positions()}
    current_row = positions_now.get(symbol) or entry_row
    stop_link_id = _stop_order_link_id(current_row)
    if not stop_link_id:
        return {"ok": False, "category": category,
                "error": "no stop_order_link_id recorded for this position"}
    try:
        live, detail = client.verify_stop(
            symbol=symbol, order_link_id=stop_link_id)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "category": category,
                "stop_order_link_id": stop_link_id, "error": str(exc)}
    return {"ok": live, "category": category,
            "stop_order_link_id": stop_link_id, "detail": detail}


def verify_remote_flat(*, client: Any, symbol: str) -> Dict[str, Any]:
    """Fresh, venue-derived proof that the account is FLAT after flatten.

    "The conformance runner must not merely call final reconciliation and
    assume that means the account is flat" — `reconcile_on_startup()`,
    the way `run_conformance` calls it (no `symbols=` argument), never
    re-reads this symbol's remote state at all; it only chases locally
    *unresolved* orders and re-checks the LOCAL ledger's naked-position
    flag. Neither proves anything about the venue. This function is the
    explicit, separate remote read that closes that gap. Never relies on:
    local `StateStore` having removed the position, `close_position()`
    returning `ok`, a REST acknowledgement alone, or a locally generated
    trade record — every value here comes from a fresh venue query made
    right now.

    Dispatches on `client.is_linear`, the same flag used throughout this
    file and `BybitClient`/`TradingEngine` themselves:

    LINEAR — a position is a first-class venue object: re-reads
    `client.get_position(symbol)` fresh and requires `size <= 0` (or no
    row at all, which `get_position` already represents as `None`).

    SPOT — there is no position endpoint (`get_position` is linear-only
    and raises there — see `BybitClient.get_position`'s own docstring);
    "flat" on spot means no resting order remains for this symbol AND the
    base-asset coin balance is below the instrument's own minimum
    tradeable quantity (an unsellable/dust residual, not a real holding).
    Reuses the EXISTING `get_open_orders()`, `get_coin_balance()`,
    `_base_asset()` and `get_instrument_filters()` — every one of them
    already used elsewhere in `BybitClient` for exactly this purpose
    (e.g. spot balance sizing) — never a new spot-flatness mechanism.

    Never raises; any failure comes back as `ok: False` with `error`.
    """
    category = "linear" if bool(getattr(client, "is_linear", False)) else "spot"
    if category == "linear":
        try:
            remote_position = client.get_position(symbol)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "category": category, "error": str(exc)}
        remote_size = float((remote_position or {}).get("size", 0) or 0)
        return {"ok": remote_size <= 0, "category": category,
                "remote_size": remote_size}

    try:
        open_orders = client.get_open_orders(symbol)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "category": category, "error": str(exc)}
    if open_orders:
        return {"ok": False, "category": category,
                "open_order_count": len(open_orders),
                "error": "orders still resting for this symbol"}

    try:
        base_asset = client._base_asset(symbol)
        filters = client.get_instrument_filters(symbol)
        remote_balance = client.get_coin_balance(base_asset)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "category": category, "error": str(exc)}
    return {"ok": remote_balance < float(filters.min_qty), "category": category,
            "base_asset": base_asset, "remote_balance": remote_balance,
            "min_qty": float(filters.min_qty), "open_order_count": 0}


def _is_valid_ws_observation(record: Dict[str, Any], *, order_link_id: str,
                             run_start_utc: Optional[str]) -> bool:
    """FIX 3: a matching record must PROVE it is this run's own, not an
    arbitrary stale record that happens to share an `order_link_id` (e.g.
    reused across conformance runs against the same testnet account).
    Requires: `transport == "ws"` (never a REST record shaped to look like
    one); `duplicate == False` (a replayed delivery is not a fresh
    observation); a matching `order_link_id`; `topic` is `order` or
    `execution`; a REAL venue identity (see below); and, when
    `run_start_utc` is given, a `captured_at_utc` at or after it —
    comparable as plain strings because both are the same fixed
    `%Y-%m-%dT%H:%M:%SZ` UTC format, which sorts lexically the same as
    chronologically.

    REAL ORDER ID / REAL EXECUTION ID REQUIRED: `private_ws_consumer.py`'s
    FIX 4 normalizes an order with no usable `orderId` (or an execution
    with no usable `execId`) to `kind="unknown"` while LEAVING `topic`
    set to `"order"`/`"execution"` — so `topic in (order, execution)`
    alone is not proof of a real identity; an invalid-identity event
    looks identical on that field. What FIX 4 does NOT leave unchanged is
    `venue_event_id`: a legitimate event is always stamped
    `f"{topic}:{identity}"`; an invalid one is stamped `f"unknown:
    {identity}"` instead. Requiring that prefix match is what actually
    proves a real venue-derived orderId/execId was present at
    normalization time, not merely that a message arrived on the right
    topic.
    """
    if record.get("transport") != "ws":
        return False
    request = record.get("request") or {}
    if bool(request.get("duplicate", False)):
        return False
    if record.get("order_link_id") != order_link_id:
        return False
    topic = request.get("topic")
    if topic not in ("order", "execution"):
        return False
    venue_event_id = str(record.get("venue_event_id") or "")
    if not venue_event_id.startswith(f"{topic}:"):
        return False
    if run_start_utc is not None:
        captured_at_utc = str(record.get("captured_at_utc") or "")
        if captured_at_utc < run_start_utc:
            return False
    return True


def await_ws_ready(*, bot: Any, timeout_seconds: float,
                   poll_interval: float = 0.1) -> Dict[str, Any]:
    """FIX 1 — bounded WS readiness wait.

    `TradingBot._start_private_ws()` starts the observer thread and
    returns immediately (`observation_status` "STARTING"); AUTH_OK and
    SUBSCRIBE_OK are only ever set by that thread's own
    connect/authenticate/subscribe sequence, asynchronously, some time
    after `startup()` has already returned. Reading `ws_consumer` merely
    EXISTING, or reading `auth_ok`/`subscribe_ok` exactly once right after
    `startup()`, is a race — "thread exists" is never equated with
    "ready" here. This polls until `ws_consumer` exists AND `auth_ok` AND
    `subscribe_ok` are all true, or `timeout_seconds` elapses. Never
    raises; timeout or an explicit auth/subscribe failure is reported as
    `ready: False` for the caller to fail the run closed on — this
    function never decides pass/fail itself, matching every other bounded
    wait in this tool.
    """
    deadline = time.time() + timeout_seconds
    while True:
        consumer = getattr(bot, "ws_consumer", None)
        auth_ok = bool(getattr(consumer, "auth_ok", False)) if consumer else False
        subscribe_ok = (bool(getattr(consumer, "subscribe_ok", False))
                        if consumer else False)
        if consumer is not None and auth_ok and subscribe_ok:
            return {"ready": True, "auth_ok": auth_ok, "subscribe_ok": subscribe_ok}
        if time.time() >= deadline:
            return {"ready": False, "auth_ok": auth_ok, "subscribe_ok": subscribe_ok}
        time.sleep(poll_interval)


def await_ws_observation(*, bot: Any, order_link_id: str,
                         timeout_seconds: float,
                         poll_interval: float = 0.1,
                         run_start_utc: Optional[str] = None,
                         store: Any = None) -> bool:
    """Poll the WS evidence log (not an in-memory counter) for a record
    that proves THIS run's own private stream observed THIS order — see
    `_is_valid_ws_observation` (FIX 3) for exactly what "proves" requires;
    a reused `order_link_id` alone is never enough. Bounded by
    `timeout_seconds`; returns False (never raises) on timeout, matching
    `run_conformance`'s "a failed stage is data, not an exception"
    convention.

    FIX 2 — drains the WS consumer's pending state/evidence queues onto
    the writer thread (this thread, via the EXISTING
    `drain_and_apply()`) on every poll, BEFORE inspecting the evidence
    file. A real WS event that arrived after the one `bot.tick()` already
    ran is only sitting in `WSPrivateConsumer`'s in-memory queues (see
    private_ws_consumer.py's ownership model) until something on the
    writer thread drains it — there is no guaranteed second tick during
    this bounded wait, so this loop IS that writer-thread drain. Never a
    second writer: `store` is only ever passed to the consumer's own
    `drain_and_apply()`, exactly like `TradingBot.tick()` does it.
    `store=None` (the default) skips draining, e.g. for callers/tests that
    only want the pure evidence-file-polling behavior.
    """
    consumer = getattr(bot, "ws_consumer", None)
    if consumer is None:
        return False
    evidence_path = getattr(consumer, "evidence_path", None)
    deadline = time.time() + timeout_seconds
    while True:
        if store is not None:
            consumer.drain_and_apply(store)
        for record in _read_evidence_records(evidence_path):
            if _is_valid_ws_observation(
                    record, order_link_id=order_link_id,
                    run_start_utc=run_start_utc):
                return True
        if time.time() >= deadline:
            return False
        time.sleep(poll_interval)


def finalize_ws_lifecycle(*, bot: Any, store: Any) -> Dict[str, Any]:
    """FIX 3 — final WS flush, before the store closes and evidence is
    verified.

    Stops the WS observer thread (`bot._stop_private_ws()`), then, ONLY
    IF that thread is verifiably no longer alive, performs ONE LAST
    writer-thread drain (`drain_and_apply()`, the exact same mechanism
    `TradingBot.tick()` and `await_ws_observation()` above already use —
    never a second writer) of whatever it had already received and queued
    but that no poll of the bounded observation wait happened to catch
    before it returned. An event already on the wire, sitting in the
    queue, must not silently disappear at shutdown.

    "Stops the thread" is checked here, not assumed: `_stop_private_ws()`
    joins with a budget sized off `private_ws_consumer.RECV_TIMEOUT_
    SECONDS` (the receive loop's own maximum blocking `recv()` interval)
    plus a safety margin — see `main.TradingBot._stop_private_ws()` — but
    a join timing out with the thread still alive is a real possibility
    this function must not paper over. If the thread is STILL alive after
    `_stop_private_ws()` returns, the "final" drain is skipped entirely
    (draining now would not be final — the still-running thread could
    enqueue more right after) and `ws_thread_stopped: False` is returned
    for the caller to fail ASSURANCE/CONFORMANCE closed on — this
    function never pretends the flush completed when it did not.

    Ownership stays exactly as everywhere else in this lifecycle: the WS
    thread only ever observed/enqueued anything; this call is what
    performs the actual state mutation and durable evidence write (when
    it runs at all), on the calling (writer) thread. The `_stop_private_
    ws()`/`drain_and_apply()` calls themselves are wrapped so a bug in
    either cannot prevent evidence verification or shutdown from running,
    but `ws_thread_stopped` is computed directly from the thread's own
    `is_alive()`, never assumed true just because nothing raised.
    """
    stop = getattr(bot, "_stop_private_ws", None)
    if callable(stop):
        try:
            stop()
        except Exception:  # noqa: BLE001
            logger.exception("_stop_private_ws() raised during final WS flush")
    ws_thread = getattr(bot, "ws_thread", None)
    ws_thread_stopped = not (ws_thread is not None and ws_thread.is_alive())
    consumer = getattr(bot, "ws_consumer", None)
    if not ws_thread_stopped:
        logger.error(
            "private-WS thread is still alive after _stop_private_ws() "
            "returned; skipping the final drain (it would not be final) "
            "-- ASSURANCE/CONFORMANCE must fail closed on this")
        return {"ws_thread_stopped": False}
    if consumer is not None:
        try:
            consumer.drain_and_apply(store)
        except Exception:  # noqa: BLE001
            logger.exception("final WS drain_and_apply() raised")
    return {"ws_thread_stopped": True}


def _conformance_ok(*, stages_ok: bool, completeness_ok: bool,
                    ws_thread_stopped: bool) -> bool:
    """The final accept/reject decision, isolated as a pure function so
    the invariant "conformance cannot report success while the WS thread
    remains alive" is directly testable without standing up the whole CLI
    (`main()` only builds a real venue stack, which this container cannot
    reach). All three inputs must hold."""
    return bool(stages_ok and completeness_ok and ws_thread_stopped)


def run_conformance(*, bot: Any, engine: Any, client: Any, store: Any,
                    symbol: str = SYMBOL,
                    ws_observation_timeout: float =
                    DEFAULT_WS_OBSERVATION_TIMEOUT_SECONDS,
                    ws_ready_timeout: float =
                    DEFAULT_WS_READY_TIMEOUT_SECONDS) -> Dict[str, Any]:
    """The bounded lifecycle itself: startup (REST reconciliation + WS
    connect/auth/subscribe) -> one tick -> WS observation -> protection
    read-back -> flatten -> final reconciliation -> evidence-chain
    verification. Returns a machine-readable stage-by-stage result; never
    raises for an execution-stage failure (a failed stage is data, not an
    exception) — only a genuinely unexpected error propagates.
    """
    timestamps: Dict[str, float] = {"run_started": time.time()}
    stages: List[Dict[str, Any]] = []

    def _stage(name: str, ok: bool, **detail: Any) -> None:
        timestamps[f"{name}_at"] = time.time()
        stages.append({"stage": name, "ok": bool(ok), **detail})

    run_start_utc = dt.datetime.fromtimestamp(
        timestamps["run_started"], dt.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")

    started = bot.startup()
    _stage("startup", started)
    if not started:
        return _finalize(stages, timestamps, ok=False)

    # FIX 5 + FIX 1 (this review round): a connected WebSocket alone is
    # not sufficient — the integrated lifecycle must have explicitly
    # established AUTH_OK and SUBSCRIBE_OK (see
    # private_ws_consumer.WSPrivateConsumer.run_once). `startup()` starts
    # that thread ASYNCHRONOUSLY and returns before it necessarily gets
    # there, so this is a BOUNDED WAIT (`await_ws_ready`), not a single
    # immediate read — "thread exists" is never equated with "ready".
    ws_ready_result = await_ws_ready(bot=bot, timeout_seconds=ws_ready_timeout)
    _stage("ws_startup", ws_ready_result["ready"],
          observation_status=bot.observation_status,
          auth_ok=ws_ready_result["auth_ok"],
          subscribe_ok=ws_ready_result["subscribe_ok"])
    if not ws_ready_result["ready"]:
        return _finalize(stages, timestamps, ok=False)

    bot.tick()
    positions = {p["symbol"]: p for p in store.open_positions()}
    row = positions.get(symbol)
    _stage("entry", row is not None,
          position=dict(row) if row else None)
    if row is None:
        return _finalize(stages, timestamps, ok=False)

    order_link_id = str(row.get("order_link_id") or "")
    ws_observed = await_ws_observation(
        bot=bot, order_link_id=order_link_id,
        timeout_seconds=ws_observation_timeout,
        run_start_utc=run_start_utc, store=store)
    _stage("ws_observation", ws_observed, order_link_id=order_link_id,
          timeout_seconds=ws_observation_timeout)
    if not ws_observed:
        return _finalize(stages, timestamps, ok=False)

    naked = store.positions_without_stops()
    protected_locally = not any(p.get("symbol") == symbol for p in naked)
    _stage("protection_local", protected_locally)
    if not protected_locally:
        return _finalize(stages, timestamps, ok=False)

    # Category-correct: get_position()/stopLoss is LINEAR-only and raises
    # on spot, where protection is a separate order, not a position
    # field — see verify_protection()'s own docstring.
    protection = verify_protection(
        client=client, store=store, symbol=symbol, entry_row=row)
    _stage("protection_readback", protection.pop("ok"), **protection)
    if not stages[-1]["ok"]:
        return _finalize(stages, timestamps, ok=False)

    close_report = engine.close_position(symbol=symbol, reason="conformance_flatten")
    _stage("flatten", bool(close_report.ok), reason=close_report.reason)

    # Fresh, venue-derived proof of flat — never inferred from
    # close_position() returning ok or from local StateStore having
    # removed the position. Runs unconditionally (no early return),
    # exactly like "flatten" above: a failed flatten should still show
    # up here as "not flat", not be hidden by stopping early.
    remote_flat = verify_remote_flat(client=client, symbol=symbol)
    _stage("remote_flat_verification", remote_flat.pop("ok"), **remote_flat)

    try:
        summary = client.reconcile_on_startup()
    except Exception as exc:  # noqa: BLE001
        _stage("final_reconciliation", False, error=str(exc))
        return _finalize(stages, timestamps, ok=False)
    _stage("final_reconciliation",
          summary.get("unknown", 1) == 0 and not summary.get("naked_positions"),
          summary=summary)

    # ASSURANCE mode fail-closed: a WS evidence-capture failure anywhere
    # in this run, even one that did not block any stage above, still
    # fails the conformance result — "partial evidence is reported as
    # partial", never silently green. See WSPrivateConsumer's
    # `evidence_capture_failed` / assurance_mode.
    evidence_complete = not bool(bot.ws_consumer.evidence_capture_failed)
    _stage("ws_evidence_completeness", evidence_complete,
          evidence_capture_failed=bool(bot.ws_consumer.evidence_capture_failed))

    return _finalize(stages, timestamps, ok=all(s["ok"] for s in stages))


def _finalize(stages: List[Dict[str, Any]], timestamps: Dict[str, float],
             *, ok: bool) -> Dict[str, Any]:
    timestamps["run_finished"] = time.time()
    latencies_ms: Dict[str, float] = {}
    ordered = [k for k in timestamps if k not in ("run_started", "run_finished")]
    prev_key, prev_ts = "run_started", timestamps["run_started"]
    for key in ordered:
        latencies_ms[f"{prev_key}->{key}"] = (timestamps[key] - prev_ts) * 1000.0
        prev_key, prev_ts = key, timestamps[key]
    return {
        "ok": ok,
        "stages": stages,
        "timestamps_epoch": timestamps,
        "latencies_ms": latencies_ms,
    }


# ---------------------------------------------------------------------------
# CLI — the only place that touches real config / real network
# ---------------------------------------------------------------------------


def _build_real_stack(*, state_db: str, evidence_path: str):
    import config as _config
    import bybit_connection as bc
    import trading_engine as te
    import main as m
    from persistence import StateStore
    from position_sizing import BillionairePositionSizing
    from risk_management import BillionaireRiskManager

    cfg = _config.load()
    store = StateStore(state_db)
    inner_transport = bc.RequestsTransport()
    transport = ve.EvidenceCapturingTransport(
        inner_transport, evidence_path=evidence_path, venue="bybit",
        environment="testnet")
    client = bc.BybitClient(config=cfg, store=store, transport=transport)
    risk = BillionaireRiskManager(config=cfg, store=store)
    sizer = BillionairePositionSizing(config=cfg, risk_manager=risk)
    engine = te.TradingEngine(
        client=client, risk_manager=risk, position_sizer=sizer,
        store=store, config=cfg)
    return cfg, store, client, engine, m.TradingBot


def verify_evidence_chains(*, rest_evidence_path: str,
                           ws_evidence_path: str) -> Dict[str, Any]:
    """Hash-chain-verify BOTH evidence logs — the same
    `venue_evidence.verify_chain` mechanism for each, never a parallel
    checker. Returns a dict a caller can fold straight into the result;
    never raises.

    NOTE: a clean chain is NECESSARY but not SUFFICIENT for ASSURANCE
    acceptance — an absent or empty log has zero structural errors and
    zero proof (`verify_chain` treats "nothing to contradict" as clean;
    see its own tests). `verify_evidence_completeness` below is the
    explicit acceptance check (FIX 2); this function stays a pure
    chain-integrity check other callers may still want on its own.
    """
    rest_reasons = ve.verify_chain(rest_evidence_path)
    ws_reasons = ve.verify_chain(ws_evidence_path)
    return {
        "rest_evidence_path": rest_evidence_path,
        "rest_evidence_verified": rest_reasons == [],
        "rest_evidence_reasons": rest_reasons,
        "ws_evidence_path": ws_evidence_path,
        "ws_evidence_verified": ws_reasons == [],
        "ws_evidence_reasons": ws_reasons,
    }


def verify_evidence_completeness(*, rest_evidence_path: str,
                                 ws_evidence_path: str,
                                 order_link_id: str = "",
                                 evidence_capture_failed: bool = False
                                 ) -> Dict[str, Any]:
    """FIX 2 — ASSURANCE MUST REQUIRE ACTUAL EVIDENCE.

    `verify_chain()` returning no structural errors must NOT, by itself,
    count as a conformance run being verified: a missing or empty evidence
    file has no errors and no content. This explicitly requires: REST
    evidence exists; WS evidence exists; a real (non-duplicate,
    `transport="ws"`) `order`-topic record for this run's `order_link_id`
    exists; likewise for `execution`; both chains verify; and no evidence
    capture failure was recorded. Missing any of these fails this check —
    never raises.
    """
    chains = verify_evidence_chains(
        rest_evidence_path=rest_evidence_path,
        ws_evidence_path=ws_evidence_path)
    rest_records = _read_evidence_records(rest_evidence_path)
    ws_records = _read_evidence_records(ws_evidence_path)

    def _has_real_ws_evidence(topic: str) -> bool:
        return any(
            _is_valid_ws_observation(
                record, order_link_id=order_link_id, run_start_utc=None)
            and record.get("request", {}).get("topic") == topic
            for record in ws_records)

    checks = {
        "rest_evidence_exists": len(rest_records) > 0,
        "ws_evidence_exists": len(ws_records) > 0,
        "order_evidence_exists": _has_real_ws_evidence("order"),
        "execution_evidence_exists": _has_real_ws_evidence("execution"),
        "evidence_capture_failed": bool(evidence_capture_failed),
    }
    ok = bool(
        checks["rest_evidence_exists"] and checks["ws_evidence_exists"]
        and checks["order_evidence_exists"]
        and checks["execution_evidence_exists"]
        and chains["rest_evidence_verified"] and chains["ws_evidence_verified"]
        and not checks["evidence_capture_failed"])
    result = dict(chains)
    result.update(checks)
    result["ok"] = ok
    return result


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--arm", action="store_true",
                        help="proceed past preflight and send real orders. "
                             "Without it, this tool only runs preflight.")
    parser.add_argument("--i-am-human", action="store_true", dest="i_am_human",
                        help="confirms a human is running this by hand; "
                             "required together with --arm")
    parser.add_argument("--symbol", default=SYMBOL)
    parser.add_argument("--entry-price", type=float, default=None,
                        help="defaults to the live testnet mark")
    parser.add_argument("--state-db", default=DEFAULT_STATE_DB)
    parser.add_argument("--evidence-path", default=DEFAULT_EVIDENCE_PATH,
                        help="REST evidence log (EvidenceCapturingTransport)")
    parser.add_argument("--ws-evidence-path", default=DEFAULT_WS_EVIDENCE_PATH,
                        help="private-WS evidence log (WSPrivateConsumer, "
                             "assurance_mode=True)")
    parser.add_argument("--ws-observation-timeout", type=float,
                        default=DEFAULT_WS_OBSERVATION_TIMEOUT_SECONDS,
                        help="bounded wait for the WS observer to capture "
                             "this order's evidence before failing closed")
    parser.add_argument("--ws-ready-timeout", type=float,
                        default=DEFAULT_WS_READY_TIMEOUT_SECONDS,
                        help="bounded wait for the WS observer to reach "
                             "AUTH_OK/SUBSCRIBE_OK before failing closed")
    parser.add_argument("--out", default=DEFAULT_RESULT_PATH)
    args = parser.parse_args(argv)

    try:
        cfg, store, client, engine, TradingBot = _build_real_stack(
            state_db=args.state_db, evidence_path=args.evidence_path)
    except Exception as exc:  # noqa: BLE001
        print(f"could not build the real stack: {type(exc).__name__}: {exc}",
              file=sys.stderr)
        return 2

    preflight = run_preflight(cfg, client)
    print("PREFLIGHT:")
    for name, outcome in preflight.checks.items():
        print(f"  {name}: {outcome}")

    if not preflight.ok:
        print("\nREFUSED: preflight failed; nothing was sent.", file=sys.stderr)
        return 1

    if not (args.arm and args.i_am_human):
        print("\nPreflight passed. Re-run with --arm --i-am-human to "
             "execute the bounded conformance sequence.")
        return 0

    entry_price = args.entry_price
    if entry_price is None:
        entry_price = float(client.get_last_price(args.symbol))
    strategy = _OneShotBuyStrategy(
        entry_price=entry_price, stop_price=entry_price * 0.98,
        take_profit=entry_price * 1.05)
    # ws_enabled_override=True: this bounded run exercises the integrated
    # WS lifecycle regardless of the general PRIVATE_WS_ENABLED trading
    # config — see main.TradingBot's own docstring on why that override
    # exists. ws_assurance_mode=True: an evidence-capture failure here
    # fails this run closed (never "best effort therefore green") — see
    # private_ws_consumer.py's "EVIDENCE FAILURE SEMANTICS".
    bot = TradingBot(config=cfg, store=store, client=client, engine=engine,
                     risk_manager=engine.risk, strategy=strategy,
                     ws_enabled_override=True, ws_assurance_mode=True,
                     ws_evidence_path=args.ws_evidence_path)

    try:
        result = run_conformance(
            bot=bot, engine=engine, client=client, store=store,
            symbol=args.symbol,
            ws_observation_timeout=args.ws_observation_timeout,
            ws_ready_timeout=args.ws_ready_timeout)
    finally:
        # FIX 3 — final WS flush: stop the WS thread, join it (a REAL
        # synchronization boundary — see main.TradingBot._stop_private_ws()
        # sizing that join off private_ws_consumer.RECV_TIMEOUT_SECONDS,
        # not a fixed guess), then run ONE LAST writer-thread drain of
        # whatever it had queued but that no poll of the bounded
        # observation wait happened to catch — BEFORE the store closes
        # and evidence is verified, so a real observation already
        # received on the wire can never disappear at shutdown. Runs even
        # when run_conformance stopped early on a failed stage.
        # `evidence_capture_failed` is read AFTER this, on purpose: this
        # final drain can itself set it.
        finalize_result = {"ws_thread_stopped": False}
        try:
            finalize_result = finalize_ws_lifecycle(bot=bot, store=store)
        except Exception:  # noqa: BLE001
            print("finalize_ws_lifecycle() raised during cleanup",
                  file=sys.stderr)
        evidence_capture_failed = bool(
            bot.ws_consumer.evidence_capture_failed if bot.ws_consumer
            else True)
        # WS shutdown is part of the bounded lifecycle this tool exercises
        # — never leave the observer thread/socket running past the run
        # this process is about to report on. finalize_ws_lifecycle()
        # already stopped/joined the WS thread above; shutdown() closing
        # it again is a no-op (main.TradingBot._stop_private_ws() returns
        # immediately once self.ws_thread is None).
        try:
            bot.shutdown()
        except Exception:  # noqa: BLE001
            print("bot.shutdown() raised during cleanup", file=sys.stderr)

    order_link_id = ""
    for stage in result["stages"]:
        if stage["stage"] == "ws_observation":
            order_link_id = str(stage.get("order_link_id") or "")
            break

    # FIX 2: explicit ASSURANCE acceptance — a clean hash chain alone is
    # not proof; this requires the actual evidence content a real
    # conformance run must have produced. Missing evidence = failed
    # conformance, distinct from (and in addition to) trading correctness.
    completeness = verify_evidence_completeness(
        rest_evidence_path=args.evidence_path,
        ws_evidence_path=args.ws_evidence_path,
        order_link_id=order_link_id,
        evidence_capture_failed=evidence_capture_failed)
    result.update(completeness)
    ws_thread_stopped = bool(finalize_result.get("ws_thread_stopped", False))
    result["ws_thread_stopped"] = ws_thread_stopped
    if not ws_thread_stopped:
        print("\nWS thread did not stop within the shutdown boundary; "
             "the final flush could not run and this result cannot be "
             "green.", file=sys.stderr)
    # This run cannot be green unless the WS thread is VERIFIABLY
    # stopped — see finalize_ws_lifecycle()/_conformance_ok(): a clean
    # stage sequence and complete evidence are not enough if the final
    # flush this result depends on never actually happened.
    result["ok"] = _conformance_ok(
        stages_ok=result["ok"], completeness_ok=completeness["ok"],
        ws_thread_stopped=ws_thread_stopped)

    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(result, handle, indent=2, default=str)
        handle.write("\n")

    print(f"\nresult written to {args.out} (ok={result['ok']})")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
