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
    """A STATIC fallback, used only for a leg whose real fee amount is
    unknown. Always already in the accounting unit (it is modelled as a
    rate against notional, which is quoted in the accounting unit), so it
    never itself needs conversion — unlike a real venue fee, which can
    come back in the base coin."""
    return abs(price * qty * taker_fee)


def split_symbol_units(symbol: str) -> Tuple[str, str]:
    """`(base, quote)` for a spot pair symbol, by suffix — "BTCUSDT" ->
    ("BTC", "USDT"). This is ONLY used to know what a KNOWN currency
    converts against (e.g. "the venue said BTC; the quote leg of this
    symbol is USDT, so multiply by price"); it is never used to invent a
    currency extract_fee did not report (see `venue_fees.py`).
    """
    for quote in ("USDT", "USDC", "USD", "BTC", "ETH"):
        if symbol.endswith(quote) and len(symbol) > len(quote):
            return symbol[: -len(quote)], quote
    return symbol, ""


@dataclass(frozen=True)
class FeeConversion:
    """What happened when this leg's raw venue fee was turned into the
    accounting unit — enough to audit or reproduce the conversion, per the
    hardening pass's requirement that a conversion never be silent."""

    #: "identity" (already in the accounting unit), "converted" (base-coin
    #: amount multiplied by this leg's own trade price), "unresolved"
    #: (amount known but its currency could not be converted — a raw fact
    #: preserved, never guessed into a number), "unknown" (no amount at
    #: all; `account_unit_amount` is a labelled STATIC ESTIMATE, never
    #: realized), or "zero_qty" (nothing to convert).
    status: str
    account_unit_amount: Optional[float]
    rate: Optional[float] = None
    basis: str = ""


def _convert_leg_fee(
    *, amount: Optional[float], currency: Optional[str], known: bool,
    leg_price: float, qty: float, taker_fee: float, accounting_unit: str,
    base_coin: str,
) -> FeeConversion:
    """One leg's raw venue fee -> the accounting unit, or an explicit
    refusal to guess. NEVER sums a BTC amount and a USDT amount as though
    they were the same number — the real BTCUSDT defect this exists to
    foreclose: an entry fee charged in BTC and an exit fee charged in
    USDT are not numerically additive until this conversion happens, and
    it only happens when the currency is actually known and matches
    either the accounting unit (identity) or the pair's base coin (priced
    conversion, same basis `ledger.py`'s `spot_buy(fee_btc, price)`
    already uses for the carry book).
    """
    if qty <= 0:
        return FeeConversion(status="zero_qty", account_unit_amount=0.0)
    if not known or amount is None:
        estimate = _estimate_leg_fee(price=leg_price, qty=qty, taker_fee=taker_fee)
        return FeeConversion(
            status="unknown", account_unit_amount=estimate,
            basis=f"static taker_fee={taker_fee} estimate (no real fee known)")
    if currency is None:
        # A real, known amount — but with no currency to convert it by.
        # Guessing here is exactly the defect this module exists to
        # refuse: preserve the raw amount, claim nothing converted.
        return FeeConversion(
            status="unresolved", account_unit_amount=None,
            basis="amount known but currency unknown; conversion refused")
    if currency == accounting_unit:
        return FeeConversion(
            status="identity", account_unit_amount=amount, rate=1.0,
            basis=f"already {accounting_unit}")
    if currency == base_coin and leg_price > 0:
        return FeeConversion(
            status="converted", account_unit_amount=amount * leg_price,
            rate=leg_price,
            basis=f"{currency}->{accounting_unit} at this leg's own trade "
                 f"price {leg_price}")
    return FeeConversion(
        status="unresolved", account_unit_amount=None,
        basis=f"no conversion basis for currency={currency!r} "
             f"(expected {accounting_unit!r} or {base_coin!r})")


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
    entry_fee_currency: Optional[str] = None,
    exit_fee: Optional[float] = None,
    exit_fee_known: bool = False,
    exit_fee_currency: Optional[str] = None,
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

    Fee reconciliation: `entry_fee`/`exit_fee` (+ currency, + `_known`)
    are the venue's own `cumExecFee` as `trading_engine.close_position`
    already read and booked it (see `venue_fees.extract_fee`) — passed
    straight through, never re-derived. A raw venue fee is an amount in
    WHATEVER currency the venue charged it in — for this exact symbol,
    conventionally the base coin on entry (a spot BUY) and the quote coin
    on exit (a spot SELL), see `INVENTORY.md` D11/D44 — and is never
    itself "USD" or directly additive across legs charged in different
    currencies. `_convert_leg_fee` makes that conversion explicit, with a
    recorded basis, or refuses rather than guess; the static `taker_fee`
    estimate is used ONLY to fill a leg whose real AMOUNT is unknown, and
    is never substituted for a known amount whose currency merely could
    not be converted. `fees_source` says, per run, whether the aggregate
    is venue-confirmed, partly estimated, fully estimated, or contains an
    unresolved (known-but-unconvertible) leg — so a reader never mistakes
    one state for another.
    """
    if gross_pnl is None and qty > 0 and entry_price > 0 and exit_price > 0:
        # Long-only session path.
        gross_pnl = (exit_price - entry_price) * qty

    base_coin, accounting_unit = split_symbol_units(symbol)
    if not accounting_unit:
        accounting_unit = "UNKNOWN_QUOTE"

    entry_conv = _convert_leg_fee(
        amount=entry_fee, currency=entry_fee_currency, known=bool(entry_fee_known),
        leg_price=entry_price, qty=qty, taker_fee=taker_fee,
        accounting_unit=accounting_unit, base_coin=base_coin)
    exit_conv = _convert_leg_fee(
        amount=exit_fee, currency=exit_fee_currency, known=bool(exit_fee_known),
        leg_price=exit_price, qty=qty, taker_fee=taker_fee,
        accounting_unit=accounting_unit, base_coin=base_coin)

    statuses = {entry_conv.status, exit_conv.status}
    if statuses <= {"identity", "converted", "zero_qty"}:
        fees_source = "venue"
    elif "unresolved" in statuses:
        # A known amount exists that this ledger refuses to silently turn
        # into a number — the aggregate below is BEST-EFFORT, not
        # realized, no matter what the other leg resolved to.
        fees_source = "unresolved_mixed_unit"
    elif statuses <= {"unknown", "zero_qty"}:
        fees_source = "estimated"
    else:
        fees_source = "partial_venue_partial_estimated"

    # Best-effort total in the accounting unit: converted/identity/estimate
    # legs contribute their number; an "unresolved" leg contributes NOTHING
    # (never zero-as-silent-omission — `fees_source` flags exactly this).
    resolved_entry_fee = entry_conv.account_unit_amount
    resolved_exit_fee = exit_conv.account_unit_amount
    fees = (resolved_entry_fee or 0.0) + (resolved_exit_fee or 0.0)
    fees_fully_realized = fees_source == "venue"

    pnl = float(gross_pnl or 0.0) - fees
    return {
        "schema": "cash_ledger/2",
        "date_utc": _utc_date(),
        "run_id": str(run_id or ""),
        "code_commit": str(code_commit or prov.UNKNOWN),
        "source_tree_clean": source_tree_clean,
        "workspace_dirty": workspace_dirty,
        "category": str(category or "spot"),
        "symbol": symbol,
        "n_fills": int(n_fills),
        #: The accounting unit every converted/account-unit figure below
        #: is denominated in — this symbol's quote coin (e.g. "USDT" for
        #: BTCUSDT). NOT audited USD: no USDT->USD conversion is performed
        #: or claimed anywhere in this ledger.
        "accounting_unit": accounting_unit,
        #: Legacy key names, KEPT for the one existing reader
        #: (`main()`'s own `pnl_usd` stderr print) — but "usd" here has
        #: always actually meant `accounting_unit`; see
        #: `pnl_account_unit`/`fees_account_unit` for the same numbers
        #: under their accurate name, and `fees_source`/`fees_realized`
        #: for whether this number may be read as realized at all.
        "pnl_usd": round(float(pnl), 8),
        "fees_usd": round(float(fees), 8),
        "pnl_account_unit": round(float(pnl), 8),
        "fees_account_unit": round(float(fees), 8),
        "fees_source": fees_source,
        "fees_realized": fees_fully_realized,
        "entry_fee_amount": entry_fee if entry_fee_known else None,
        "entry_fee_currency": entry_fee_currency if entry_fee_known else None,
        "entry_fee_known": bool(entry_fee_known),
        "entry_fee_account_unit": (
            round(float(resolved_entry_fee), 8)
            if resolved_entry_fee is not None else None),
        "entry_fee_conversion": {
            "status": entry_conv.status, "rate": entry_conv.rate,
            "basis": entry_conv.basis,
        },
        # Legacy alias — None (not 0.0) when unresolved, never a silent
        # zero standing in for "could not convert".
        "entry_fee_usd": (
            round(float(resolved_entry_fee), 8)
            if resolved_entry_fee is not None else None),
        "exit_fee_amount": exit_fee if exit_fee_known else None,
        "exit_fee_currency": exit_fee_currency if exit_fee_known else None,
        "exit_fee_known": bool(exit_fee_known),
        "exit_fee_account_unit": (
            round(float(resolved_exit_fee), 8)
            if resolved_exit_fee is not None else None),
        "exit_fee_conversion": {
            "status": exit_conv.status, "rate": exit_conv.rate,
            "basis": exit_conv.basis,
        },
        "exit_fee_usd": (
            round(float(resolved_exit_fee), 8)
            if resolved_exit_fee is not None else None),
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
    """Overwrite-in-place write. For a MUTABLE "latest view" only — see
    `write_run_scoped_record` for the immutable per-run record."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
    return path


class RunRecordAlreadyExists(RuntimeError):
    """A `(run_id, kind)` record already exists. Refused — never silently
    replaced, truncated, or merged. The original bytes on disk are
    untouched by this call."""


def _write_json_exclusive(path: str, payload: Dict[str, Any]) -> None:
    """Create `path` and write `payload` to it, or raise if it already
    exists — `os.O_CREAT | os.O_EXCL` is atomic at the OS/filesystem
    level (a single syscall decides create-vs-exists), unlike the
    race-prone `if not os.path.exists(path): write(path)`, where a second
    process can pass the `exists()` check before the first one's `write`
    lands. Two processes racing to create the SAME path: exactly one
    `open` succeeds, the other raises `FileExistsError` immediately,
    before either has written a single byte — there is no window where a
    second writer's content could land in the same file as the first's.
    """
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
    except BaseException:
        # The create succeeded but the write didn't finish — remove the
        # partial file rather than leave a corrupt "record" behind that a
        # later read would have to distinguish from a complete one.
        try:
            os.remove(path)
        except OSError:
            pass
        raise


def write_run_scoped_record(
    payload: Dict[str, Any], *, run_id: str, kind: str,
    runs_dir: str = DEFAULT_RUNS_DIR, index_path: str = DEFAULT_RUNS_INDEX,
) -> str:
    """Persist `payload` under `<runs_dir>/<run_id>/<kind>.json` — the
    IMMUTABLE record this run_id's `kind` of artefact exists at, once and
    permanently.

    FIRST write for a given `(run_id, kind)`: succeeds, and appends one
    line to `index_path`. SECOND write for the SAME `(run_id, kind)`:
    raises `RunRecordAlreadyExists` — the original file's bytes are
    untouched, and no duplicate index line is appended (the index write
    happens only after the exclusive file create has already succeeded,
    so a refused second write never reaches it). Distinct run_ids never
    collide (see `new_run_id`); the same run_id legitimately gains more
    than one `kind` (a ledger, later found not-flat, is followed by a
    failure for the SAME run) — those are different files, not a
    collision.

    `index_path` is append-only; a concurrent duplicate attempt against
    the SAME `(run_id, kind)` still cannot produce two canonical records,
    because the exclusive file create — not the index — is what decides
    "first writer wins"; the index can never show two different writers
    claiming to have been first for the same identity, because only the
    one whose `_write_json_exclusive` actually succeeded ever reaches the
    `open(..., "a")` below.

    A missing `run_id` is a caller bug, not a reason to guess one; this
    silently no-ops rather than inventing a directory with no identity.
    """
    if not run_id:
        return ""
    manifest_path = os.path.join(runs_dir, run_id, f"{kind}.json")
    os.makedirs(os.path.dirname(manifest_path), exist_ok=True)
    try:
        _write_json_exclusive(manifest_path, payload)
    except FileExistsError as exc:
        raise RunRecordAlreadyExists(
            f"a {kind!r} record for run_id={run_id!r} already exists at "
            f"{manifest_path!r} — refusing to overwrite it") from exc
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
        "entry_fee_currency": None,
        "exit_fee": None,
        "exit_fee_known": False,
        "exit_fee_currency": None,
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
    out["entry_fee_currency"] = entry_detail.get("entry_fee_currency")

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
        out["entry_fee_currency"] = close_detail.get("entry_fee_currency")
    out["exit_fee"] = close_detail.get("exit_fee")
    out["exit_fee_known"] = bool(close_detail.get("exit_fee_known"))
    out["exit_fee_currency"] = close_detail.get("exit_fee_currency")
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

    # Everything from here down is BOOKKEEPING on data `run_session()`
    # already produced. A bug or disk fault in bookkeeping itself (a bad
    # ledger field, a full disk, a genuine (run_id, kind) collision) must
    # not erase the order identity `result` already holds — so this whole
    # block is one try/except whose failure path still calls
    # `write_failure` with that identity, rather than letting the
    # exception propagate and leave NOTHING written at all.
    try:
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
            entry_fee_currency=result.get("entry_fee_currency"),
            exit_fee=result.get("exit_fee"),
            exit_fee_known=bool(result.get("exit_fee_known")),
            exit_fee_currency=result.get("exit_fee_currency"),
        )
        run_manifest_path = write_run_scoped_record(
            ledger, run_id=run_id, kind="ledger",
            runs_dir=args.runs_dir, index_path=args.runs_index)
        # `args.ledger` (cash_ledger.json by default) stays a DERIVED
        # "latest" view for convenience — the run-scoped file just written
        # above, under this run_id, is the immutable record.
        if run_manifest_path:
            ledger["run_manifest_path"] = run_manifest_path
        ledger["artifact_role"] = "latest_view"
        write_json(args.ledger, ledger)
    except Exception as exc:  # noqa: BLE001
        logger.exception("ledger/latest-view/index bookkeeping raised")
        write_failure(
            args.failure_out,
            reason=f"bookkeeping raised after a real session: "
                   f"{type(exc).__name__}: {exc}",
            network_order_submitted=True, entry_fills=entry_fills,
            flatten_attempted=bool(result.get("flatten_attempted")),
            run_id=run_id, code_commit=code_commit,
            source_tree_clean=source_tree_clean, workspace_dirty=workspace_dirty,
            entry_order_link_id=result.get("entry_order_link_id") or "",
            stop_order_link_id=result.get("stop_order_link_id") or "",
            take_profit_ids=list(result.get("take_profit_ids") or ()),
            exit_order_link_id=result.get("exit_order_link_id") or "",
            exit_order_id=result.get("exit_order_id") or "",
            entry_fee=result.get("entry_fee"),
            entry_fee_currency=result.get("entry_fee_currency"),
            exit_fee=result.get("exit_fee"),
            exit_fee_currency=result.get("exit_fee_currency"),
            runs_dir=args.runs_dir, runs_index=args.runs_index,
        )
        return 1
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
