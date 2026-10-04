#!/usr/bin/env python3
"""Bounded Bybit TESTNET session: one entry, one flatten, one cash ledger.

WHAT THIS IS
============
Proves the production order path on Bybit testnet — same
`bybit_connection.BybitClient`, `trading_engine.TradingEngine`,
`venue_evidence.EvidenceCapturingTransport`, and (for flatness) the
read-only surfaces `recon_packet` already uses. Not a second HTTP client,
not a second signal, not a product dashboard.

WHAT THIS IS NOT
================
- Not mainnet. Refuses unless BYBIT_VENUE=testnet and the client's actual
  base URL resolves to the testnet host.
- Not live money. `allows_live` is always written false; this tool never
  sets LIVE_TRADING_ACK, allows_live, or live_authorized.
- Not cash. Testnet PnL is fake dollars. The forward shadow book sits at
  about -0.94 R on 4 trades; a larger notional can lose more fake dollars.
  This path caps notional at $1000 HERE ONLY and does not touch the live
  gate or shadow.SHADOW_MAX_NOTIONAL_USD.

EXIT CODES
==========
  0  flat is true and artifacts/cash_ledger.json exists
  2  zero fills (nothing submitted or nothing filled)
  1  any other refusal / partial failure (ledger or failure JSON written)
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
from typing import Any, Dict, List, Optional, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
BOT = os.path.dirname(HERE)
sys.path.insert(0, BOT)
sys.path.insert(0, HERE)

import venue_evidence as ve  # noqa: E402
import testnet_conformance_run as tcr  # noqa: E402

logger = logging.getLogger("testnet_session")

SYMBOL = "BTCUSDT"
#: Cap for THIS PATH ONLY. Does not change shadow.SHADOW_MAX_NOTIONAL_USD
#: or the live promotion gate.
SESSION_NOTIONAL_CAP_USD = 1000.0
DEFAULT_STATE_DB = os.path.join(BOT, "artifacts", "testnet_session_state.db")
DEFAULT_EVIDENCE_PATH = os.path.join(
    BOT, "artifacts", "testnet_session_evidence.jsonl")
DEFAULT_LEDGER_PATH = os.path.join(BOT, "artifacts", "cash_ledger.json")
DEFAULT_FAILURE_PATH = os.path.join(
    BOT, "artifacts", "testnet_session_failure.json")


class SessionRefused(RuntimeError):
    """A preflight or session invariant failed. Refuse, do not proceed."""


# ---------------------------------------------------------------------------
# notional cap — this path only; never mutates the live/shadow gate constants
# ---------------------------------------------------------------------------


@dataclass
class _CappedSizer:
    """Wraps a real sizer; clamps qty so notional <= SESSION_NOTIONAL_CAP_USD."""

    inner: Any
    max_notional_usd: float = SESSION_NOTIONAL_CAP_USD

    def calculate_position_size(self, request: Any) -> Any:
        from position_sizing import SizingResult

        sizing = self.inner.calculate_position_size(request)
        if not getattr(sizing, "should_trade", False):
            return sizing
        price = float(getattr(request, "current_price", 0.0) or 0.0)
        if price <= 0.0:
            return sizing
        qty = float(sizing.qty)
        notional = qty * price
        if notional <= self.max_notional_usd:
            return sizing
        capped_qty = self.max_notional_usd / price
        # Respect exchange filters via the inner sizer's filter object when
        # present on the request; otherwise leave a raw cap (engine will
        # still snap via client filters on place).
        filters = getattr(request, "filters", None)
        if filters is not None:
            try:
                from position_sizing import clamp_to_exchange
                capped_qty, _ = clamp_to_exchange(
                    getattr(request, "symbol", SYMBOL), price, capped_qty,
                    filters)
            except Exception:  # noqa: BLE001
                pass
        if capped_qty <= 0.0:
            return SizingResult(
                symbol=getattr(sizing, "symbol", SYMBOL), qty=0.0,
                reason="SESSION_NOTIONAL_CAP_BELOW_MIN",
                detail={"max_notional_usd": self.max_notional_usd})
        detail = dict(getattr(sizing, "detail", {}) or {})
        detail["session_notional_cap_usd"] = self.max_notional_usd
        detail["uncapped_qty"] = qty
        detail["uncapped_notional"] = notional
        return SizingResult(
            symbol=getattr(sizing, "symbol", SYMBOL),
            qty=float(capped_qty),
            notional=float(capped_qty) * price,
            risk_fraction=float(getattr(sizing, "risk_fraction", 0.0) or 0.0),
            reason="SESSION_NOTIONAL_CAPPED",
            method=str(getattr(sizing, "method", "") or "session_cap"),
            detail=detail,
        )


# ---------------------------------------------------------------------------
# order path — named for the one-page report
# ---------------------------------------------------------------------------


def submit_order(
    engine: Any,
    *,
    symbol: str,
    entry_price: float,
    stop_price: float,
    take_profit: float,
) -> Any:
    """Submit one entry through TradingEngine.execute (production path)."""
    import trading_engine as te

    intent = te.TradeIntent(
        symbol=symbol,
        signal_type="BUY",
        entry_price=float(entry_price),
        stop_price=float(stop_price),
        take_profits=((float(take_profit), 1.0),),
        confidence=0.8,
    )
    return engine.execute(intent)


def flatten_position(engine: Any, *, symbol: str,
                     reason: str = "testnet_session_flatten") -> Any:
    """Flatten through TradingEngine.close_position (production path)."""
    return engine.close_position(symbol=symbol, reason=reason)


# ---------------------------------------------------------------------------
# ledger
# ---------------------------------------------------------------------------


def _utc_date() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")


def _estimate_fees(*, entry_price: float, exit_price: float, qty: float,
                   taker_fee: float) -> float:
    return abs(entry_price * qty * taker_fee) + abs(exit_price * qty * taker_fee)


def build_cash_ledger(
    *,
    symbol: str,
    category: str,
    n_fills: int,
    entry_price: float,
    exit_price: float,
    qty: float,
    flat: bool,
    taker_fee: float = 0.001,
    gross_pnl: Optional[float] = None,
) -> Dict[str, Any]:
    """Machine-readable cash ledger. Testnet PnL is fake dollars — never cash."""
    if gross_pnl is None and qty > 0 and entry_price > 0 and exit_price > 0:
        # Long-only session path.
        gross_pnl = (exit_price - entry_price) * qty
    fees = _estimate_fees(
        entry_price=entry_price, exit_price=exit_price, qty=qty,
        taker_fee=taker_fee) if qty > 0 else 0.0
    pnl = float(gross_pnl or 0.0) - fees
    return {
        "schema": "cash_ledger/1",
        "date_utc": _utc_date(),
        "category": str(category or "spot"),
        "symbol": symbol,
        "n_fills": int(n_fills),
        "pnl_usd": round(float(pnl), 8),
        "fees_usd": round(float(fees), 8),
        "flat": bool(flat),
        "allows_live": False,
        "note": (
            "Testnet PnL is fake dollars, not cash. Forward shadow book is "
            "about -0.94 R on 4 trades; this path's $1000 notional cap is "
            "not profit and can lose more fake dollars."
        ),
        "session_notional_cap_usd": SESSION_NOTIONAL_CAP_USD,
        "entry_price": float(entry_price or 0.0),
        "exit_price": float(exit_price or 0.0),
        "qty": float(qty or 0.0),
    }


def write_json(path: str, payload: Dict[str, Any]) -> str:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
    return path


def write_failure(path: str, *, reason: str, **extra: Any) -> str:
    payload = {
        "schema": "testnet_session_failure/1",
        "date_utc": _utc_date(),
        "status": "FAILED",
        "failure": reason,
        "network_order_submitted": bool(extra.pop("network_order_submitted", False)),
        "entry_fills": int(extra.pop("entry_fills", 0)),
        "flatten_attempted": bool(extra.pop("flatten_attempted", False)),
        "ledger_written": bool(extra.pop("ledger_written", False)),
        "allows_live": False,
        "live_authorized": False,
        "note": "No testnet PnL was represented as cash.",
    }
    payload.update(extra)
    return write_json(path, payload)


# ---------------------------------------------------------------------------
# flatness — venue read via existing client surfaces (same as recon_packet)
# ---------------------------------------------------------------------------


def position_is_flat(client: Any, store: Any, *, symbol: str,
                     run_entry_qty: float, run_exit_qty: float) -> Tuple[bool, Dict[str, Any]]:
    """Fresh venue flatness. Reuses conformance's verify_remote_flat."""
    result = tcr.verify_remote_flat(
        client=client, symbol=symbol,
        run_entry_qty=run_entry_qty, run_exit_qty=run_exit_qty)
    ok = bool(result.get("ok"))
    # Also confirm the local ledger has no open row for this symbol.
    open_syms = {p.get("symbol") for p in store.open_positions()}
    local_flat = symbol not in open_syms
    detail = dict(result)
    detail["local_flat"] = local_flat
    return bool(ok and local_flat), detail


# ---------------------------------------------------------------------------
# stack + session
# ---------------------------------------------------------------------------


def _build_session_stack(*, state_db: str, evidence_path: str):
    """Same production stack as conformance, with this-path notional cap."""
    cfg, store, client, engine, TradingBot = tcr._build_real_stack(
        state_db=state_db, evidence_path=evidence_path)
    # Cap THIS path only — wrap the engine's sizer, leave shadow/live gates alone.
    engine.sizer = _CappedSizer(engine.sizer, SESSION_NOTIONAL_CAP_USD)
    return cfg, store, client, engine, TradingBot


def run_session(
    *,
    cfg: Any,
    store: Any,
    client: Any,
    engine: Any,
    symbol: str = SYMBOL,
) -> Dict[str, Any]:
    """One entry, one flatten. Returns a result dict; does not write the ledger."""
    out: Dict[str, Any] = {
        "ok": False,
        "entry_fills": 0,
        "flatten_attempted": False,
        "flat": False,
        "n_fills": 0,
        "symbol": symbol,
        "category": str(getattr(cfg, "CATEGORY", "spot") or "spot"),
        "entry_price": 0.0,
        "exit_price": 0.0,
        "qty": 0.0,
        "gross_pnl": 0.0,
        "network_order_submitted": False,
        "stages": [],
    }

    preflight = tcr.run_preflight(cfg, client)
    out["preflight"] = preflight.checks
    if not preflight.ok:
        out["failure"] = "preflight failed: " + "; ".join(
            f"{k}={v}" for k, v in preflight.checks.items() if v != "ok")
        return out

    # Refuse paper: a paper fill is not venue evidence.
    if bool(getattr(cfg, "PAPER_TRADING", True)):
        out["failure"] = "PAPER_TRADING is true; refusing (paper is not a fill)"
        return out
    if bool(getattr(engine, "paper", False)):
        out["failure"] = "engine.paper is true; refusing"
        return out

    # The entry geometry comes from the production strategy, not from a
    # fabricated BUY — build_bot attaches the same signal source main.py's
    # runtime would (MarketStrategy/ShadowStrategy per BOOK_MODE), so this
    # tool consults it exactly like the runtime does rather than forcing a
    # trade just to exercise the fill path.
    import main as _main

    bot = _main.build_bot(
        config=cfg, store=store, client=client, engine=engine,
        risk_manager=engine.risk)
    if bot.strategy is None:
        out["failure"] = "no signal"
        return out
    try:
        intent = bot.strategy.signal_for(symbol)
    except Exception as exc:  # noqa: BLE001
        out["failure"] = f"strategy failed: {type(exc).__name__}: {exc}"
        return out
    if intent is None:
        out["failure"] = "no signal"
        return out

    report = engine.execute(intent)
    out["stages"].append({
        "stage": "entry",
        "ok": bool(report.ok and report.stage not in ("paper",)),
        "reason": report.reason,
        "stage_name": report.stage,
        "qty": float(report.qty or 0.0),
        "detail": dict(report.detail or {}),
    })
    out["network_order_submitted"] = report.stage not in ("paper", "signal", "pre_gate",
                                                          "sizing", "equity", "filters",
                                                          "quote", "exit_plan", "final_gate")

    positions = {p["symbol"]: p for p in store.open_positions()}
    row = positions.get(symbol)
    if row is None:
        # Zero fills: either rejected, paper, or never opened.
        out["failure"] = f"no position after entry ({report.stage}:{report.reason})"
        out["entry_fills"] = 0
        return out

    entry_price = float(row.get("entry_price") or last)
    qty = float(row.get("qty") or 0.0)
    out["entry_fills"] = 1 if qty > 0 else 0
    out["entry_price"] = entry_price
    out["qty"] = qty
    out["n_fills"] = 1 if qty > 0 else 0
    if qty <= 0:
        out["failure"] = "entry reported a zero qty"
        return out

    # Flatten before exit — always attempt once we have a position.
    out["flatten_attempted"] = True
    close = flatten_position(engine, symbol=symbol)
    exit_price = float((close.detail or {}).get("exit_avg_price") or 0.0)
    executed_exit = float((close.detail or {}).get("executed_exit_qty") or 0.0)
    gross = float((close.detail or {}).get("gross_pnl") or 0.0)
    out["stages"].append({
        "stage": "flatten",
        "ok": bool(close.ok),
        "reason": close.reason,
        "exit_avg_price": exit_price,
        "executed_exit_qty": executed_exit,
        "gross_pnl": gross,
    })
    out["exit_price"] = exit_price
    out["gross_pnl"] = gross
    if executed_exit > 0:
        out["n_fills"] = 2

    flat, flat_detail = position_is_flat(
        client, store, symbol=symbol,
        run_entry_qty=qty, run_exit_qty=executed_exit)
    out["flat"] = flat
    out["flat_detail"] = flat_detail
    out["stages"].append({"stage": "flat_verification", "ok": flat, **flat_detail})

    # Cancel any residual protective orders for this symbol (spot stop).
    try:
        client.cancel_all(symbol)
    except Exception:  # noqa: BLE001
        logger.exception("cancel_all after flatten raised")

    out["ok"] = bool(flat and out["n_fills"] >= 2)
    if not out["ok"] and "failure" not in out:
        out["failure"] = "session incomplete (not flat or missing exit fill)"
    return out


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--symbol", default=SYMBOL)
    parser.add_argument("--state-db", default=DEFAULT_STATE_DB)
    parser.add_argument("--evidence-path", default=DEFAULT_EVIDENCE_PATH)
    parser.add_argument("--ledger", default=DEFAULT_LEDGER_PATH)
    parser.add_argument("--failure-out", default=DEFAULT_FAILURE_PATH)
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    # Hard refuse before building anything if venue env is wrong.
    venue = (os.environ.get("BYBIT_VENUE") or "").strip().lower()
    # Allow unset here only if config will default sandbox→testnet; still
    # re-check on the loaded cfg + client URL below.
    if venue and venue != "testnet":
        write_failure(
            args.failure_out,
            reason=f"BYBIT_VENUE={venue!r}; this tool arms on testnet only",
            network_order_submitted=False, entry_fills=0)
        print(f"REFUSED: BYBIT_VENUE={venue!r}", file=sys.stderr)
        return 1

    try:
        cfg, store, client, engine, _TradingBot = _build_session_stack(
            state_db=args.state_db, evidence_path=args.evidence_path)
    except Exception as exc:  # noqa: BLE001
        write_failure(
            args.failure_out,
            reason=f"stack build failed: {type(exc).__name__}: {exc}",
            network_order_submitted=False, entry_fills=0)
        print(f"stack build failed: {exc}", file=sys.stderr)
        return 1

    if str(getattr(cfg, "BYBIT_VENUE", "") or "") != "testnet":
        write_failure(
            args.failure_out,
            reason=f"cfg.BYBIT_VENUE={getattr(cfg, 'BYBIT_VENUE', None)!r}",
            network_order_submitted=False, entry_fills=0)
        return 1

    try:
        result = run_session(
            cfg=cfg, store=store, client=client, engine=engine,
            symbol=args.symbol)
    except Exception as exc:  # noqa: BLE001
        logger.exception("run_session raised")
        # Best-effort flatten if a position somehow opened.
        try:
            flatten_position(engine, symbol=args.symbol, reason="session_error_flatten")
        except Exception:  # noqa: BLE001
            pass
        write_failure(
            args.failure_out,
            reason=f"run_session raised: {type(exc).__name__}: {exc}",
            network_order_submitted=False, entry_fills=0,
            flatten_attempted=True)
        return 1
    finally:
        try:
            store.close()
        except Exception:  # noqa: BLE001
            pass

    entry_fills = int(result.get("entry_fills") or 0)
    n_fills = int(result.get("n_fills") or 0)

    if entry_fills == 0 or n_fills == 0:
        write_failure(
            args.failure_out,
            reason=result.get("failure") or "zero fills",
            network_order_submitted=bool(result.get("network_order_submitted")),
            entry_fills=entry_fills,
            flatten_attempted=bool(result.get("flatten_attempted")),
            stages=result.get("stages"),
            preflight=result.get("preflight"),
        )
        print("ZERO FILLS — exit 2", file=sys.stderr)
        return 2

    ledger = build_cash_ledger(
        symbol=str(result.get("symbol") or args.symbol),
        category=str(result.get("category") or "spot"),
        n_fills=n_fills,
        entry_price=float(result.get("entry_price") or 0.0),
        exit_price=float(result.get("exit_price") or 0.0),
        qty=float(result.get("qty") or 0.0),
        flat=bool(result.get("flat")),
        taker_fee=float(getattr(cfg, "TAKER_FEE", 0.001) or 0.001),
        gross_pnl=float(result.get("gross_pnl") or 0.0),
    )
    write_json(args.ledger, ledger)
    print(json.dumps(ledger, indent=2, sort_keys=True))

    if not ledger["flat"]:
        write_failure(
            args.failure_out,
            reason="position not flat after session",
            network_order_submitted=True,
            entry_fills=entry_fills,
            flatten_attempted=True,
            ledger_written=True,
            ledger=ledger,
            flat_detail=result.get("flat_detail"),
        )
        return 1

    if not os.path.exists(args.ledger):
        return 1

    pnl = float(ledger["pnl_usd"])
    if pnl < 0:
        print(f"pnl_usd={pnl} (fake testnet dollars; negative)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
