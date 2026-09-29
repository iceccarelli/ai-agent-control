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
  1. TradingBot.startup()  — reconcile first, exactly as production does.
  2. TradingBot.tick()     — one cycle: a bounded one-shot strategy signals
                             BUY once, `engine.execute()` submits the order,
                             observes the fill, places and verifies the
                             protective stop — all synchronous, all real.
  3. Protection read-back  — `client.get_position()` queried FRESH (not the
                             local ledger) to confirm the stop the venue
                             itself reports, not merely what execute()
                             believed it set.
  4. Flatten               — `engine.close_position()`, the same method
                             production uses to exit.
  5. Final reconciliation  — `client.reconcile_on_startup()`.
  6. Evidence + result     — the evidence log (already being written by
                             EvidenceCapturingTransport since step 1) is
                             left in place; a machine-readable summary
                             (stage-by-stage outcome, latencies, evidence
                             path) is written to `--out`.

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
DEFAULT_STATE_DB = os.path.join(
    BOT, "artifacts", "testnet_conformance_state.db")
DEFAULT_RESULT_PATH = os.path.join(
    BOT, "artifacts", "testnet_conformance_result.json")
SYMBOL = "BTCUSDT"


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


def run_conformance(*, bot: Any, engine: Any, client: Any, store: Any,
                    symbol: str = SYMBOL) -> Dict[str, Any]:
    """The bounded lifecycle itself: startup -> one tick -> protection
    read-back -> flatten -> final reconciliation. Returns a machine-readable
    stage-by-stage result; never raises for an execution-stage failure (a
    failed stage is data, not an exception) — only a genuinely unexpected
    error propagates."""
    timestamps: Dict[str, float] = {"run_started": time.time()}
    stages: List[Dict[str, Any]] = []

    def _stage(name: str, ok: bool, **detail: Any) -> None:
        timestamps[f"{name}_at"] = time.time()
        stages.append({"stage": name, "ok": bool(ok), **detail})

    started = bot.startup()
    _stage("startup", started)
    if not started:
        return _finalize(stages, timestamps, ok=False)

    bot.tick()
    positions = {p["symbol"]: p for p in store.open_positions()}
    row = positions.get(symbol)
    _stage("entry", row is not None,
          position=dict(row) if row else None)
    if row is None:
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
    parser.add_argument("--evidence-path", default=DEFAULT_EVIDENCE_PATH)
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
    bot = TradingBot(config=cfg, store=store, client=client, engine=engine,
                     risk_manager=engine.risk, strategy=strategy)

    result = run_conformance(bot=bot, engine=engine, client=client,
                             store=store, symbol=args.symbol)
    result["evidence_path"] = args.evidence_path
    result["evidence_chain_verified"] = ve.verify_chain(args.evidence_path) == []

    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(result, handle, indent=2, default=str)
        handle.write("\n")

    print(f"\nresult written to {args.out} (ok={result['ok']})")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
