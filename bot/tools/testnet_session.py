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
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
BOT = os.path.dirname(HERE)
sys.path.insert(0, BOT)
sys.path.insert(0, HERE)

import venue_evidence as ve  # noqa: E402
import testnet_conformance_run as tcr  # noqa: E402
import provenance as prov  # noqa: E402

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
#: Run-scoped, immutable records: one directory per run_id, never
#: overwritten. `DEFAULT_LEDGER_PATH`/`DEFAULT_FAILURE_PATH` above remain a
#: DERIVED "latest" view for convenience — these are the actual record.
DEFAULT_RUNS_DIR = os.path.join(BOT, "artifacts", "testnet_runs")
#: Append-only index of every run this tool has ever written a record for.
#: Never rewritten in place — a new run never erases the line that named
#: the run before it, which is exactly what reusing the singleton ledger/
#: failure paths alone always did.
DEFAULT_RUNS_INDEX = os.path.join(DEFAULT_RUNS_DIR, "index.jsonl")


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


def new_run_id() -> str:
    """One identifier per `main()` invocation, shared by the ledger and any
    failure JSON it writes.

    Without this, two sessions run on the same date_utc produce two
    `cash_ledger.json` payloads a reader cannot tell apart — the second
    silently overwrites the first's evidence. A run_id does not stop the
    overwrite (the ledger is still a singleton file), but it ties whatever
    IS on disk to one unambiguous run, and to that run's order ids and
    `testnet_session_evidence.jsonl` hash-chain records.
    """
    return f"{dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:12]}"


def _estimate_leg_fee(*, price: float, qty: float, taker_fee: float) -> float:
    """A STATIC fallback, used only for a leg whose real fee is unknown."""
    return abs(price * qty * taker_fee)


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
    run_id: str = "",
    code_commit: str = "",
    source_tree_clean: Optional[bool] = None,
    workspace_dirty: Optional[bool] = None,
    entry_order_link_id: str = "",
    stop_order_link_id: str = "",
    take_profit_ids: Tuple[str, ...] = (),
    exit_order_link_id: str = "",
    exit_order_id: str = "",
    entry_fee: Optional[float] = None,
    entry_fee_known: bool = False,
    exit_fee: Optional[float] = None,
    exit_fee_known: bool = False,
) -> Dict[str, Any]:
    """Machine-readable cash ledger. Testnet PnL is fake dollars — never cash.

    `run_id`/`code_commit` and the order identity fields are what let this
    singleton file be attributed to one specific session rather than read
    as "the" result: without them, a stale or unrelated run looks exactly
    like the one a reader is trying to verify (no order id to check it
    against, no commit to say what code produced it).

    `code_commit` is always a bare `HEAD` sha — never the `-dirty`-suffixed
    form `tools/provenance.git_commit` returns elsewhere in this repo.
    `source_tree_clean` (no tracked `*.py` differs from that commit) and
    `workspace_dirty` (ANYTHING uncommitted, tracked or not) are reported
    separately, precisely so a reader cannot misread "some untracked
    artefact is lying around" as "the source code that ran does not match
    `code_commit`" — the one thing a `-dirty` suffix cannot distinguish.

    Fee reconciliation: `entry_fee`/`exit_fee` + their `_known` flags are
    the venue's own `cumExecFee` as `trading_engine.close_position` already
    read it and booked it on the local trade row (see
    `trading_engine.venue_fee`) — passed straight through, never
    re-derived. The static `taker_fee` estimate is used ONLY to fill a leg
    whose real fee is unknown (e.g. no fill ever resolved), and
    `fees_source` says, per run, whether this ledger's `fees_usd` is
    venue-confirmed, partly estimated, or fully estimated — so a reader
    never mistakes one for the other.
    """
    if gross_pnl is None and qty > 0 and entry_price > 0 and exit_price > 0:
        # Long-only session path.
        gross_pnl = (exit_price - entry_price) * qty

    if qty > 0:
        resolved_entry_fee = (
            float(entry_fee) if entry_fee_known and entry_fee is not None
            else _estimate_leg_fee(price=entry_price, qty=qty, taker_fee=taker_fee))
        resolved_exit_fee = (
            float(exit_fee) if exit_fee_known and exit_fee is not None
            else _estimate_leg_fee(price=exit_price, qty=qty, taker_fee=taker_fee))
    else:
        resolved_entry_fee = 0.0
        resolved_exit_fee = 0.0
    fees = resolved_entry_fee + resolved_exit_fee

    known_legs = int(bool(entry_fee_known)) + int(bool(exit_fee_known))
    if known_legs == 2:
        fees_source = "venue"
    elif known_legs == 1:
        fees_source = "partial_venue_partial_estimated"
    else:
        fees_source = "estimated"

    pnl = float(gross_pnl or 0.0) - fees
    return {
        "schema": "cash_ledger/1",
        "date_utc": _utc_date(),
        "run_id": str(run_id or ""),
        "code_commit": str(code_commit or prov.UNKNOWN),
        "source_tree_clean": source_tree_clean,
        "workspace_dirty": workspace_dirty,
        "category": str(category or "spot"),
        "symbol": symbol,
        "n_fills": int(n_fills),
        "pnl_usd": round(float(pnl), 8),
        "fees_usd": round(float(fees), 8),
        "fees_source": fees_source,
        "entry_fee_usd": round(float(resolved_entry_fee), 8),
        "entry_fee_known": bool(entry_fee_known),
        "exit_fee_usd": round(float(resolved_exit_fee), 8),
        "exit_fee_known": bool(exit_fee_known),
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
        "entry_order_link_id": str(entry_order_link_id or ""),
        "stop_order_link_id": str(stop_order_link_id or ""),
        "take_profit_ids": list(take_profit_ids or ()),
        "exit_order_link_id": str(exit_order_link_id or ""),
        "exit_order_id": str(exit_order_id or ""),
    }


def write_json(path: str, payload: Dict[str, Any]) -> str:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
    return path


def write_run_scoped_record(
    payload: Dict[str, Any], *, run_id: str, kind: str,
    runs_dir: str = DEFAULT_RUNS_DIR, index_path: str = DEFAULT_RUNS_INDEX,
) -> str:
    """Persist `payload` under `<runs_dir>/<run_id>/<kind>.json` — the
    IMMUTABLE record this run_id's `kind` of artefact exists at, once and
    permanently. Distinct run_ids never collide (see `new_run_id`); the
    same run_id can legitimately gain more than one `kind` (a ledger,
    later found not-flat, is followed by a failure for the SAME run), so
    the directory itself is not exist_ok=False, but neither file is ever
    reopened for writing once this call returns.

    Also appends one line to `index_path` — append-only, never rewritten —
    so a reader can enumerate every run this tool has ever produced a
    record for without having to list the runs directory, and so the run
    before this one is never erased the way overwriting a singleton
    ledger/failure file always erased it.

    A missing `run_id` is a caller bug, not a reason to guess one; this
    silently no-ops rather than inventing a directory with no identity.
    """
    if not run_id:
        return ""
    manifest_path = os.path.join(runs_dir, run_id, f"{kind}.json")
    write_json(manifest_path, payload)
    os.makedirs(os.path.dirname(index_path) or ".", exist_ok=True)
    with open(index_path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps({
            "run_id": run_id,
            "kind": kind,
            "manifest_path": manifest_path,
            "written_at_utc": dt.datetime.now(dt.timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"),
            "code_commit": payload.get("code_commit"),
        }, sort_keys=True) + "\n")
    return manifest_path


def write_failure(
    path: str, *, reason: str, run_id: str = "",
    runs_dir: str = DEFAULT_RUNS_DIR, runs_index: str = DEFAULT_RUNS_INDEX,
    **extra: Any,
) -> str:
    payload = {
        "schema": "testnet_session_failure/1",
        "date_utc": _utc_date(),
        "run_id": str(run_id or ""),
        "code_commit": str(extra.pop("code_commit", "") or prov.UNKNOWN),
        "source_tree_clean": extra.pop("source_tree_clean", None),
        "workspace_dirty": extra.pop("workspace_dirty", None),
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
    run_manifest_path = write_run_scoped_record(
        payload, run_id=run_id, kind="failure",
        runs_dir=runs_dir, index_path=runs_index)
    if run_manifest_path:
        payload["run_manifest_path"] = run_manifest_path
    # `path` (cash_ledger.json's sibling, testnet_session_failure.json by
    # default) is the DERIVED "latest" view — the run-scoped file above,
    # one per run_id, is the record of this specific run.
    payload["artifact_role"] = "latest_view"
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
        "entry_order_link_id": "",
        "stop_order_link_id": "",
        "take_profit_ids": (),
        "exit_order_link_id": "",
        "exit_order_id": "",
        "entry_fee": None,
        "entry_fee_known": False,
        "exit_fee": None,
        "exit_fee_known": False,
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

    try:
        out = _run_session_body(
            out, cfg=cfg, store=store, client=client, engine=engine,
            symbol=symbol)
    except Exception as exc:  # noqa: BLE001
        # Survivable failure evidence: whatever order identity `out` already
        # holds (set incrementally, below, the moment each report/close
        # comes back) is NOT lost just because something raised before this
        # function could return normally — a crash after a real network
        # submission must not erase the ids that submission produced.
        logger.exception("run_session body raised")
        out["exception"] = f"{type(exc).__name__}: {exc}"
        if "failure" not in out:
            out["failure"] = f"run_session raised: {out['exception']}"
        if not out.get("flatten_attempted") and out.get("entry_order_link_id"):
            # An entry was actually submitted (it has an orderLinkId) and
            # nothing has attempted to close it yet — try once, best-effort,
            # rather than leave it silently naked. Gated on the orderLinkId
            # rather than `entry_fills`: a crash between the entry report
            # coming back and this function noticing the fill still leaves
            # a real submitted order, with real identity, worth trying to
            # flatten.
            out["flatten_attempted"] = True
            try:
                flatten_position(engine, symbol=symbol,
                                 reason="session_error_flatten")
            except Exception:  # noqa: BLE001
                logger.exception("best-effort flatten after exception raised")
    return out


def _run_session_body(
    out: Dict[str, Any], *, cfg: Any, store: Any, client: Any, engine: Any,
    symbol: str,
) -> Dict[str, Any]:
    """The risky part of `run_session`, split out so its caller can wrap it
    in one try/except and still return whatever `out` holds at the moment
    of failure — see `run_session`'s docstring."""
    last = float(client.get_last_price(symbol))
    stop = last * 0.98
    take = last * 1.05

    report = submit_order(
        engine, symbol=symbol, entry_price=last, stop_price=stop,
        take_profit=take)
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
    # Order identity — carried on `report` itself (ExecutionReport fields),
    # not inside `report.detail`. Captured here regardless of outcome: a
    # rejected/unconfirmed entry still has an orderLinkId worth keeping.
    out["entry_order_link_id"] = str(getattr(report, "entry_order_link_id", "") or "")
    out["stop_order_link_id"] = str(getattr(report, "stop_order_link_id", "") or "")
    out["take_profit_ids"] = tuple(getattr(report, "take_profit_ids", ()) or ())
    # Authoritative entry fee (trading_engine.venue_fee's cumExecFee read),
    # carried through report.detail exactly as the engine booked it locally
    # — never re-derived here, so there is one authority for this number.
    entry_detail = dict(report.detail or {})
    out["entry_fee"] = entry_detail.get("entry_fee")
    out["entry_fee_known"] = bool(entry_detail.get("entry_fee") is not None)

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
    exit_order_link_id = str((close.detail or {}).get("exit_order_link_id") or "")
    exit_order_id = str((close.detail or {}).get("exit_order_id") or "")
    out["stages"].append({
        "stage": "flatten",
        "ok": bool(close.ok),
        "reason": close.reason,
        "exit_avg_price": exit_price,
        "executed_exit_qty": executed_exit,
        "gross_pnl": gross,
        "exit_order_link_id": exit_order_link_id,
        "exit_order_id": exit_order_id,
    })
    out["exit_price"] = exit_price
    out["gross_pnl"] = gross
    out["exit_order_link_id"] = exit_order_link_id
    out["exit_order_id"] = exit_order_id
    # `close.detail` is the authoritative source for BOTH fee legs: it is
    # produced by the same `close_position()` call that just booked the
    # local trade row, re-reading the entry fee from position meta and
    # reading the exit fee from the exit fill itself — overrides the
    # entry-stage heuristic above rather than duplicating its logic.
    close_detail = dict(close.detail or {})
    if "entry_fee" in close_detail:
        out["entry_fee"] = close_detail.get("entry_fee")
        out["entry_fee_known"] = bool(close_detail.get("entry_fee_known"))
    out["exit_fee"] = close_detail.get("exit_fee")
    out["exit_fee_known"] = bool(close_detail.get("exit_fee_known"))
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
    parser.add_argument("--runs-dir", default=DEFAULT_RUNS_DIR)
    parser.add_argument("--runs-index", default=DEFAULT_RUNS_INDEX)
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    # One identifier and one provenance stamp for everything this
    # invocation writes (ledger and/or failure), so either artefact can be
    # tied back to the exact run and the exact code that produced it.
    # `code_commit` is bare (no `-dirty` suffix) — `source_tree_clean` and
    # `workspace_dirty` carry that distinction explicitly instead.
    run_id = new_run_id()
    code_commit = prov.git_commit(BOT, mark_dirty=False)
    source_tree_clean = prov.source_tree_clean(BOT)
    workspace_dirty = prov.workspace_dirty(BOT)

    # Hard refuse before building anything if venue env is wrong.
    venue = (os.environ.get("BYBIT_VENUE") or "").strip().lower()
    # Allow unset here only if config will default sandbox→testnet; still
    # re-check on the loaded cfg + client URL below.
    if venue and venue != "testnet":
        write_failure(
            args.failure_out,
            reason=f"BYBIT_VENUE={venue!r}; this tool arms on testnet only",
            network_order_submitted=False, entry_fills=0,
            run_id=run_id, code_commit=code_commit,
            source_tree_clean=source_tree_clean, workspace_dirty=workspace_dirty,
            runs_dir=args.runs_dir, runs_index=args.runs_index)
        print(f"REFUSED: BYBIT_VENUE={venue!r}", file=sys.stderr)
        return 1

    try:
        cfg, store, client, engine, _TradingBot = _build_session_stack(
            state_db=args.state_db, evidence_path=args.evidence_path)
    except Exception as exc:  # noqa: BLE001
        write_failure(
            args.failure_out,
            reason=f"stack build failed: {type(exc).__name__}: {exc}",
            network_order_submitted=False, entry_fills=0,
            run_id=run_id, code_commit=code_commit,
            source_tree_clean=source_tree_clean, workspace_dirty=workspace_dirty,
            runs_dir=args.runs_dir, runs_index=args.runs_index)
        print(f"stack build failed: {exc}", file=sys.stderr)
        return 1

    if str(getattr(cfg, "BYBIT_VENUE", "") or "") != "testnet":
        write_failure(
            args.failure_out,
            reason=f"cfg.BYBIT_VENUE={getattr(cfg, 'BYBIT_VENUE', None)!r}",
            network_order_submitted=False, entry_fills=0,
            run_id=run_id, code_commit=code_commit,
            source_tree_clean=source_tree_clean, workspace_dirty=workspace_dirty,
            runs_dir=args.runs_dir, runs_index=args.runs_index)
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
            flatten_attempted=True,
            run_id=run_id, code_commit=code_commit,
            source_tree_clean=source_tree_clean, workspace_dirty=workspace_dirty,
            runs_dir=args.runs_dir, runs_index=args.runs_index)
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
            run_id=run_id, code_commit=code_commit,
            source_tree_clean=source_tree_clean, workspace_dirty=workspace_dirty,
            exception=result.get("exception") or "",
            # Whatever order identity exists survives even a zero-fill
            # failure — an entry that was submitted and then the process
            # raised still has an orderLinkId worth keeping (requirement:
            # failure evidence must not lose known order identity).
            entry_order_link_id=result.get("entry_order_link_id") or "",
            stop_order_link_id=result.get("stop_order_link_id") or "",
            take_profit_ids=list(result.get("take_profit_ids") or ()),
            exit_order_link_id=result.get("exit_order_link_id") or "",
            exit_order_id=result.get("exit_order_id") or "",
            runs_dir=args.runs_dir, runs_index=args.runs_index,
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
        run_id=run_id,
        code_commit=code_commit,
        source_tree_clean=source_tree_clean,
        workspace_dirty=workspace_dirty,
        entry_order_link_id=str(result.get("entry_order_link_id") or ""),
        stop_order_link_id=str(result.get("stop_order_link_id") or ""),
        take_profit_ids=tuple(result.get("take_profit_ids") or ()),
        exit_order_link_id=str(result.get("exit_order_link_id") or ""),
        exit_order_id=str(result.get("exit_order_id") or ""),
        entry_fee=result.get("entry_fee"),
        entry_fee_known=bool(result.get("entry_fee_known")),
        exit_fee=result.get("exit_fee"),
        exit_fee_known=bool(result.get("exit_fee_known")),
    )
    run_manifest_path = write_run_scoped_record(
        ledger, run_id=run_id, kind="ledger",
        runs_dir=args.runs_dir, index_path=args.runs_index)
    # `args.ledger` (cash_ledger.json by default) stays a DERIVED "latest"
    # view for convenience — the run-scoped file just written above, under
    # this run_id, is the immutable record.
    if run_manifest_path:
        ledger["run_manifest_path"] = run_manifest_path
    ledger["artifact_role"] = "latest_view"
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
            run_id=run_id, code_commit=code_commit,
            source_tree_clean=source_tree_clean, workspace_dirty=workspace_dirty,
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
