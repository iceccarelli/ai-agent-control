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
                             observer connects, authenticates, and
                             subscribes — the same `_start_private_ws`
                             production uses, not a separate WS path.
  2. TradingBot.tick()     — one cycle: a bounded one-shot strategy signals
                             BUY once, `engine.execute()` submits the order,
                             observes the fill, places and verifies the
                             protective stop — all synchronous, all real.
  3. WS observation        — bounded wait (`--ws-observation-timeout`) for
                             the WS observer to actually capture an order
                             or execution evidence record for THIS order's
                             `order_link_id` — proof the private stream
                             observed the same real order, not merely that
                             REST did. ASSURANCE mode (this tool always
                             uses it): missing WS observation is a failed
                             run, not a soft warning.
  4. Protection read-back  — `client.get_position()` queried FRESH (not the
                             local ledger) to confirm the stop the venue
                             itself reports, not merely what execute()
                             believed it set.
  5. Flatten               — `engine.close_position()`, the same method
                             production uses to exit.
  6. Final reconciliation  — `client.reconcile_on_startup()`.
  7. Evidence verification — BOTH evidence chains (REST's, via
                             EvidenceCapturingTransport; WS's, via
                             WSPrivateConsumer's assurance-mode capture)
                             are hash-chain-verified with
                             `venue_evidence.verify_chain`. A tamper,
                             gap, or capture failure in EITHER chain fails
                             the run — see "ASSURANCE MODE" below.
  8. WS shutdown + result  — `bot.shutdown()` stops the WS thread; a
                             machine-readable summary (stage-by-stage
                             outcome, latencies, both evidence paths, both
                             chain-verification results) is written to
                             `--out`.

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
import json
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


def await_ws_observation(*, bot: Any, order_link_id: str,
                         timeout_seconds: float,
                         poll_interval: float = 0.1) -> bool:
    """Poll the WS evidence log (not an in-memory counter) for a record
    whose `order_link_id` matches this order and whose topic is `order`
    or `execution` — proof the private stream itself observed THIS order,
    not merely that a WS connection exists. Bounded by `timeout_seconds`;
    returns False (never raises) on timeout, matching `run_conformance`'s
    "a failed stage is data, not an exception" convention.
    """
    consumer = getattr(bot, "ws_consumer", None)
    if consumer is None:
        return False
    evidence_path = getattr(consumer, "evidence_path", None)
    deadline = time.time() + timeout_seconds
    while True:
        for record in _read_evidence_records(evidence_path):
            if (record.get("order_link_id") == order_link_id
                    and record.get("request", {}).get("topic") in
                    ("order", "execution")):
                return True
        if time.time() >= deadline:
            return False
        time.sleep(poll_interval)


def run_conformance(*, bot: Any, engine: Any, client: Any, store: Any,
                    symbol: str = SYMBOL,
                    ws_observation_timeout: float =
                    DEFAULT_WS_OBSERVATION_TIMEOUT_SECONDS) -> Dict[str, Any]:
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

    started = bot.startup()
    _stage("startup", started)
    if not started:
        return _finalize(stages, timestamps, ok=False)

    _stage("ws_startup", bot.ws_consumer is not None,
          observation_status=bot.observation_status)
    if bot.ws_consumer is None:
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
        timeout_seconds=ws_observation_timeout)
    _stage("ws_observation", ws_observed, order_link_id=order_link_id,
          timeout_seconds=ws_observation_timeout)
    if not ws_observed:
        return _finalize(stages, timestamps, ok=False)

    naked = store.positions_without_stops()
    protected_locally = not any(p.get("symbol") == symbol for p in naked)
    _stage("protection_local", protected_locally)
    if not protected_locally:
        return _finalize(stages, timestamps, ok=False)

    try:
        remote_position = client.get_position(symbol)
    except Exception as exc:  # noqa: BLE001
        _stage("protection_readback", False, error=str(exc))
        return _finalize(stages, timestamps, ok=False)
    remote_stop = float((remote_position or {}).get("stopLoss", 0) or 0)
    _stage("protection_readback", remote_stop > 0, remote_stop=remote_stop)
    if not (remote_stop > 0):
        return _finalize(stages, timestamps, ok=False)

    close_report = engine.close_position(symbol=symbol, reason="conformance_flatten")
    _stage("flatten", bool(close_report.ok), reason=close_report.reason)

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
    never raises."""
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
            ws_observation_timeout=args.ws_observation_timeout)
    finally:
        # WS shutdown is part of the bounded lifecycle this tool exercises
        # — never leave the observer thread/socket running past the run
        # this process is about to report on.
        try:
            bot.shutdown()
        except Exception:  # noqa: BLE001
            print("bot.shutdown() raised during cleanup", file=sys.stderr)

    chains = verify_evidence_chains(
        rest_evidence_path=args.evidence_path,
        ws_evidence_path=args.ws_evidence_path)
    result.update(chains)
    # Fail closed: a stage can all read "ok" while either evidence chain
    # is broken (e.g. a hand-edited or truncated file) — this must still
    # fail the CONFORMANCE result, distinct from trading correctness.
    result["ok"] = bool(
        result["ok"] and chains["rest_evidence_verified"]
        and chains["ws_evidence_verified"])

    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(result, handle, indent=2, default=str)
        handle.write("\n")

    print(f"\nresult written to {args.out} (ok={result['ok']})")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
