#!/usr/bin/env python3
"""recon_packet.py — READ-ONLY venue reconciliation / assurance packet.

WHAT THIS IS
============
A small CLI that takes one symbol, reads LOCAL state (via
`persistence.StateStore`'s existing accessors) and VENUE state (via the
real `bybit_connection.BybitClient`, GET-only), reconciles the two, and
writes one deterministic, machine-readable JSON packet: what the ledger
believes, what the exchange actually holds, where they agree, and where
they do not.

This is an execution-assurance tool for a paper/testnet research bot, not
a strategy tool and not a second copy of anything that already exists:

  * ONE client         — `bybit_connection.BybitClient`. This module never
                          builds its own HTTP request, never re-signs
                          anything, and never talks to a venue except
                          through that client's already-existing GET
                          methods (`get_wallet`, `get_coin_balance`,
                          `get_open_orders`, `get_position`).
  * ONE state model     — `persistence.StateStore`. Every LOCAL fact below
                          comes from an accessor that module already
                          exposes (`open_positions`, `orders_for_symbol`,
                          `unresolved_orders`, `positions_without_stops`,
                          `is_kill_switch_engaged`, `get_breaker`).
  * ONE evidence format — `tools/venue_evidence.py`'s hash-chained JSONL.
                          `--compare-evidence` calls that module's own
                          `verify_chain`; it does not implement a second
                          chain checker.
  * ONE env reader       — `config.load()`. This module never reads
                          `os.environ` directly.

READ-ONLY CONTRACT (HARD REQUIREMENT)
======================================
This tool never calls, imports the use of, or references any of:
`BybitClient.place_order`, `.place_stop_order`, `.place_take_profit`,
`.cancel_order`, `.cancel_all`, `._place_position_stop`,
`.clear_position_stop`, `._submit`, `.reconcile_on_startup` (which itself
only issues GETs but is skipped here anyway — this tool does not want to
resolve anything, only observe it), or any `--arm`-style flag. It only
calls GET-shaped venue reads: `get_wallet`, `get_coin_balance`,
`get_open_orders` (per spot order-filter bucket), `get_position`. See
`tests/test_recon_packet.py::TestReadOnlyContract` for both a static
source-text check and a runtime spy that fails the moment any mutating
method is invoked.

ENVIRONMENT
===========
`config.load()` is read once for `BYBIT_VENUE`/`CATEGORY`/credentials.
`BYBIT_VENUE` must be exactly "testnet" — mainnet (or demo, or unset) is
refused before a single request is built, exactly like
`testnet_conformance_run.verify_environment`. This tool would be
read-only even against mainnet, but the live-authorization gate this
repo enforces everywhere else is not something an assurance tool gets to
opt out of by claiming "I only read".

FAIL CLOSED
===========
`packet["ok"]` is only ever `True` when: local state was fully readable,
venue state was fully readable, every reconciled item classified
`MATCHED`, and (if `--compare-evidence`) the referenced evidence files
were readable with a valid hash chain and their identities agreed with
this snapshot. Anything else — a venue read that raised, a local read
that raised, an unresolved identity, a tampered evidence chain — is
`ok=False` with an explicit reason code. `UNKNOWN` is never coerced to
`MATCHED`.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import sys
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
BOT = os.path.dirname(HERE)
sys.path.insert(0, BOT)
sys.path.insert(0, HERE)

import config as _config              # noqa: E402
import provenance as _provenance      # noqa: E402
import venue_evidence as ve           # noqa: E402
from persistence import StateStore    # noqa: E402

logger = logging.getLogger("recon_packet")

SCHEMA = "recon_packet/1"

DEFAULT_STATE_DB = os.path.join(BOT, "artifacts", "testnet_conformance_state.db")
DEFAULT_REST_EVIDENCE_PATH = os.path.join(
    BOT, "artifacts", "testnet_conformance_evidence.jsonl")
DEFAULT_WS_EVIDENCE_PATH = os.path.join(
    BOT, "artifacts", "testnet_conformance_ws_evidence.jsonl")
DEFAULT_CONFORMANCE_RESULT_PATH = os.path.join(
    BOT, "artifacts", "testnet_conformance_result.json")
DEFAULT_OUT_PATH = os.path.join(BOT, "artifacts", "recon_packet_result.json")
SYMBOL = "BTCUSDT"

#: See `bybit_connection.BybitClient._SPOT_CANCEL_ALL_ORDER_FILTERS` — the
#: exact same three spot order kinds, for the exact same reason: a single
#: unfiltered GET does not enumerate all of them together.
SPOT_ORDER_FILTERS: Tuple[str, ...] = ("Order", "StopOrder", "tpslOrder")

#: Local order statuses that mean "should still be resting at the venue".
#: A locally 'filled'/'cancelled'/'rejected'/'never_sent' order is expected
#: to be ABSENT from a venue open-orders read; comparing it there would
#: manufacture a false LOCAL_ONLY finding for every trade that ever closed
#: normally.
OPEN_LOCAL_STATUSES = frozenset({"pending", "submitted", "partial", "unknown"})

CLASSIFICATIONS = frozenset({
    "MATCHED", "LOCAL_ONLY", "VENUE_ONLY", "QUANTITY_MISMATCH",
    "SIDE_MISMATCH", "PRICE_MISMATCH", "PROTECTION_MISMATCH", "UNKNOWN",
})

#: Absolute floors under which a float comparison tolerance never goes,
#: plus a relative fraction of the larger operand — catches both "compare
#: two numbers near zero" and "compare two large numbers that are 1ulp
#: apart from a decimal round-trip", without either being so loose that a
#: real drift is swallowed.
QTY_TOLERANCE_ABS = 1e-9
PRICE_TOLERANCE_ABS = 1e-8
RELATIVE_TOLERANCE = 1e-6


class ReconciliationRefused(RuntimeError):
    """The packet must not even attempt a venue read. Refuse outright."""


def _isclose(a: float, b: float, *, abs_tol: float) -> bool:
    return abs(a - b) <= max(abs_tol, RELATIVE_TOLERANCE * max(abs(a), abs(b)))


def _f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _new_run_id() -> str:
    return f"recon-{uuid.uuid4().hex[:16]}"


def _read_jsonl(path: str) -> List[Dict[str, Any]]:
    with open(path, encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def _order_identity_known(order_link_id: str, prefix: str) -> bool:
    """Whether `order_link_id` matches THIS bot's own id scheme
    (`build_order_link_id`'s `f"{prefix}-{purpose}-{seq}-{digest}"` shape,
    checked loosely as "starts with our prefix") — never a guess about
    what a foreign id *might* mean. A venue order with an id this loose
    outside our own convention did not come from this bot's order path
    and its identity cannot be resolved from local state; see
    `reconcile_orders`'s UNKNOWN branch.
    """
    return bool(order_link_id) and order_link_id.startswith(f"{prefix}-")


def _stop_order_link_id(position_row: Mapping[str, Any]) -> str:
    """The protective stop's own `order_link_id`, exactly as
    `tools/testnet_conformance_run._stop_order_link_id` reads it from a
    position row's `meta` JSON — copied rather than imported because that
    function is private to its module (leading underscore) and this tool
    must not reach into another tool's internals; the *logic* (read
    `meta.stop_order_link_id`, never fabricate one) is the one already
    proven in that module's own tests.
    """
    try:
        meta = json.loads(position_row.get("meta") or "{}")
    except (TypeError, ValueError):
        return ""
    return str(meta.get("stop_order_link_id") or "")


# ---------------------------------------------------------------------------
# reconciliation items
# ---------------------------------------------------------------------------


@dataclass
class ReconciliationItem:
    kind: str              # "order" | "position" | "protection"
    identity: str          # order_link_id or symbol
    classification: str
    detail: str = ""
    local: Optional[Dict[str, Any]] = None
    venue: Optional[Dict[str, Any]] = None

    def __post_init__(self) -> None:
        if self.classification not in CLASSIFICATIONS:
            raise ValueError(f"unknown classification {self.classification!r}")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind,
            "identity": self.identity,
            "classification": self.classification,
            "detail": self.detail,
            "local": self.local,
            "venue": self.venue,
        }


def _reason_for(item: ReconciliationItem) -> Optional[str]:
    """Map one item's fine-grained classification onto a coarse top-level
    reason code. `reconciliation` (the item list) always carries the exact
    classification; `reasons` is the fail-closed summary a caller can gate
    on without inspecting every item."""
    if item.classification == "MATCHED":
        return None
    if item.kind == "order":
        return {
            "VENUE_ONLY": "VENUE_ONLY_ORDER",
            "LOCAL_ONLY": "LOCAL_ONLY_ORDER",
            "UNKNOWN": "UNKNOWN_ORDER",
        }.get(item.classification, "ORDER_MISMATCH")
    if item.kind == "protection":
        return "PROTECTION_DRIFT"
    # kind == "position"
    return {
        "UNKNOWN": "UNKNOWN_POSITION",
    }.get(item.classification, "POSITION_DRIFT")


# ---------------------------------------------------------------------------
# LOCAL snapshot — persistence.StateStore accessors only
# ---------------------------------------------------------------------------


def build_local_snapshot(store: StateStore, symbol: str) -> Dict[str, Any]:
    """Every LOCAL fact this packet reports, from `StateStore` accessors
    that already exist. No new table, no new query shape beyond
    `orders_for_symbol` (a plain additive `SELECT * FROM orders WHERE
    symbol = ?`, the same read-only shape `pending_orders`/
    `unresolved_orders` already use)."""
    positions = [p for p in store.open_positions() if p.get("symbol") == symbol]
    orders = store.orders_for_symbol(symbol)
    unresolved = [o for o in store.unresolved_orders() if o.get("symbol") == symbol]
    naked = [p for p in store.positions_without_stops() if p.get("symbol") == symbol]
    kill_engaged, kill_reason = store.is_kill_switch_engaged()
    breaker = store.get_breaker()
    return {
        "positions": positions,
        "orders": orders,
        "unresolved_orders": unresolved,
        "naked_positions": naked,
        "kill_switch": {"engaged": kill_engaged, "reason": kill_reason},
        "breaker": {
            "active": breaker.active,
            "reason": breaker.reason,
            "tripped_at_epoch": breaker.tripped_at_epoch,
            "cooldown_until_epoch": breaker.cooldown_until_epoch,
            "consecutive_losses": breaker.consecutive_losses,
        },
    }


# ---------------------------------------------------------------------------
# VENUE snapshot — bybit_connection.BybitClient GET methods only
# ---------------------------------------------------------------------------


def build_spot_venue_snapshot(client: Any, symbol: str) -> Dict[str, Any]:
    """SPOT: balances (base + quote, available/locked where the wallet
    exposes it) and open orders enumerated SEPARATELY per
    `SPOT_ORDER_FILTERS` — never one unfiltered read assumed to cover all
    three. GET-only: `get_wallet`, `get_coin_balance`, `get_open_orders`.
    """
    base_asset = client._base_asset(symbol)
    quote_asset = client._quote_asset(symbol)
    wallet = client.get_wallet()
    coins_by_symbol = {
        str(c.get("coin", "")).upper(): c for c in (wallet.get("coin") or [])
    }

    def _balance(asset: str) -> Dict[str, Any]:
        row = coins_by_symbol.get(asset.upper(), {})
        available = row.get("availableToWithdraw")
        if available in (None, ""):
            available = row.get("free")
        return {
            "asset": asset,
            "available": _f(available if available not in (None, "") else
                            client.get_coin_balance(asset)),
            "locked": _f(row.get("locked")) if row.get("locked") not in (None, "") else None,
            "wallet_balance": _f(row.get("walletBalance")) if row.get(
                "walletBalance") not in (None, "") else None,
        }

    orders_by_filter: Dict[str, Dict[str, Dict[str, Any]]] = {}
    for order_filter in SPOT_ORDER_FILTERS:
        rows = client.get_open_orders(symbol, order_filter=order_filter)
        orders_by_filter[order_filter] = {
            str(r.get("orderLinkId", "")): r for r in rows if r.get("orderLinkId")
        }

    return {
        "category": "spot",
        "base_asset": base_asset,
        "quote_asset": quote_asset,
        "base_balance": _balance(base_asset),
        "quote_balance": _balance(quote_asset),
        "orders_by_filter": orders_by_filter,
    }


def build_linear_venue_snapshot(client: Any, symbol: str) -> Dict[str, Any]:
    """LINEAR: the position (size/side/entry/stop) read fresh from
    `/v5/position/list` via `get_position`, and open orders via
    `get_open_orders`. GET-only."""
    position = client.get_position(symbol)
    orders = client.get_open_orders(symbol)
    return {
        "category": "linear",
        "position": position,
        "orders_by_link_id": {
            str(r.get("orderLinkId", "")): r for r in orders if r.get("orderLinkId")
        },
    }


# ---------------------------------------------------------------------------
# reconciliation
# ---------------------------------------------------------------------------


def reconcile_orders(
    *, local_orders: Sequence[Mapping[str, Any]],
    venue_orders_by_filter: Mapping[str, Mapping[str, Mapping[str, Any]]],
    order_link_prefix: str,
) -> List[ReconciliationItem]:
    """Compare every LOCALLY-open order against the matching venue
    order-filter bucket, then report every unmatched venue order.

    A local order is matched against the venue bucket its own `purpose`
    implies (`"stop"` -> `StopOrder`, everything else -> `Order`) —
    `tpslOrder` has no local equivalent in this codebase's order path
    (protective stops are placed as `StopOrder` conditional orders, never
    `tpslOrder` — see `bybit_connection.place_stop_order`), so any
    `tpslOrder` found at the venue is inherently unmatched.
    """
    remaining: Dict[str, Dict[str, Dict[str, Any]]] = {
        filt: dict(rows) for filt, rows in venue_orders_by_filter.items()
    }
    items: List[ReconciliationItem] = []

    for row in local_orders:
        status = str(row.get("status", ""))
        if status not in OPEN_LOCAL_STATUSES:
            continue
        oid = str(row.get("order_link_id", ""))
        purpose = str(row.get("purpose", ""))
        order_filter = "StopOrder" if purpose == "stop" else "Order"
        venue_row = remaining.get(order_filter, {}).pop(oid, None)
        if venue_row is None:
            items.append(ReconciliationItem(
                kind="order", identity=oid, classification="LOCAL_ONLY",
                local=dict(row), venue=None,
                detail=f"local {purpose!r} order not found among venue "
                       f"{order_filter} orders"))
            continue

        local_side = str(row.get("side", "")).strip().lower()
        venue_side = str(venue_row.get("side", "")).strip().lower()
        if local_side and venue_side and local_side != venue_side:
            items.append(ReconciliationItem(
                kind="order", identity=oid, classification="SIDE_MISMATCH",
                local=dict(row), venue=dict(venue_row),
                detail=f"local side {local_side!r} != venue side {venue_side!r}"))
            continue

        local_qty = _f(row.get("qty"))
        venue_qty = _f(venue_row.get("qty"))
        if not _isclose(local_qty, venue_qty, abs_tol=QTY_TOLERANCE_ABS):
            items.append(ReconciliationItem(
                kind="order", identity=oid, classification="QUANTITY_MISMATCH",
                local=dict(row), venue=dict(venue_row),
                detail=f"local qty {local_qty} != venue qty {venue_qty}"))
            continue

        local_price = _f(row.get("price"))
        venue_price = _f(venue_row.get("price") or venue_row.get("triggerPrice"))
        if local_price > 0 and venue_price > 0 and not _isclose(
                local_price, venue_price, abs_tol=PRICE_TOLERANCE_ABS):
            items.append(ReconciliationItem(
                kind="order", identity=oid, classification="PRICE_MISMATCH",
                local=dict(row), venue=dict(venue_row),
                detail=f"local price {local_price} != venue price {venue_price}"))
            continue

        items.append(ReconciliationItem(
            kind="order", identity=oid, classification="MATCHED",
            local=dict(row), venue=dict(venue_row), detail="order matches"))

    for order_filter, rows_by_id in remaining.items():
        for oid, venue_row in rows_by_id.items():
            if _order_identity_known(oid, order_link_prefix):
                items.append(ReconciliationItem(
                    kind="order", identity=oid, classification="VENUE_ONLY",
                    local=None, venue=dict(venue_row),
                    detail=f"venue {order_filter} order has no matching "
                           "open local record"))
            else:
                items.append(ReconciliationItem(
                    kind="order", identity=oid, classification="UNKNOWN",
                    local=None, venue=dict(venue_row),
                    detail=f"venue {order_filter} order id does not match "
                           "this bot's orderLinkId scheme; identity cannot "
                           "be attributed and is never coerced to MATCHED"))
    return items


def reconcile_spot_protection(
    *, local_positions: Sequence[Mapping[str, Any]],
    stop_orders_by_link_id: Mapping[str, Mapping[str, Any]],
) -> List[ReconciliationItem]:
    """SPOT protection: the conditional StopOrder the position's own `meta`
    names must be visible at the venue AND still live (`Untriggered`/
    `New`) — the same acceptance `bybit_connection.verify_stop` uses,
    checked here from a snapshot instead of a live call so this stays a
    pure reconciliation over already-fetched state."""
    items: List[ReconciliationItem] = []
    for pos in local_positions:
        symbol = str(pos.get("symbol", ""))
        stop_link_id = _stop_order_link_id(pos)
        if not stop_link_id:
            items.append(ReconciliationItem(
                kind="protection", identity=symbol,
                classification="PROTECTION_MISMATCH", local=dict(pos), venue=None,
                detail="no stop_order_link_id recorded in local position meta "
                       "-- a confirmed position must always have a verified stop"))
            continue
        venue_row = stop_orders_by_link_id.get(stop_link_id)
        if venue_row is None:
            items.append(ReconciliationItem(
                kind="protection", identity=symbol,
                classification="PROTECTION_MISMATCH", local=dict(pos), venue=None,
                detail=f"protective stop {stop_link_id!r} not visible among "
                       "venue StopOrder orders"))
            continue
        order_status = str(venue_row.get("orderStatus", ""))
        if order_status not in ("Untriggered", "New"):
            items.append(ReconciliationItem(
                kind="protection", identity=symbol,
                classification="PROTECTION_MISMATCH", local=dict(pos),
                venue=dict(venue_row),
                detail=f"protective stop {stop_link_id!r} orderStatus="
                       f"{order_status!r}, not live"))
            continue
        items.append(ReconciliationItem(
            kind="protection", identity=symbol, classification="MATCHED",
            local=dict(pos), venue=dict(venue_row),
            detail=f"protective stop live orderStatus={order_status!r}"))
    return items


def reconcile_linear_protection(
    *, local_positions: Sequence[Mapping[str, Any]],
    venue_position: Optional[Mapping[str, Any]],
) -> List[ReconciliationItem]:
    """LINEAR protection: the position-attached `stopLoss` field, never
    confused with a spot conditional order (a different mechanism
    entirely — see `bybit_connection.place_stop_order`'s category
    dispatch)."""
    items: List[ReconciliationItem] = []
    for pos in local_positions:
        symbol = str(pos.get("symbol", ""))
        local_stop = _f(pos.get("stop_price"))
        venue_stop = _f((venue_position or {}).get("stopLoss"))
        if local_stop <= 0:
            items.append(ReconciliationItem(
                kind="protection", identity=symbol,
                classification="PROTECTION_MISMATCH", local=dict(pos),
                venue=dict(venue_position) if venue_position else None,
                detail="local position has no recorded protective stop "
                       "(naked) -- a confirmed position must always have "
                       "a verified stop"))
            continue
        if venue_position is None or venue_stop <= 0:
            items.append(ReconciliationItem(
                kind="protection", identity=symbol,
                classification="PROTECTION_MISMATCH", local=dict(pos),
                venue=dict(venue_position) if venue_position else None,
                detail="venue position reports no live stopLoss"))
            continue
        if not _isclose(local_stop, venue_stop, abs_tol=PRICE_TOLERANCE_ABS):
            items.append(ReconciliationItem(
                kind="protection", identity=symbol,
                classification="PROTECTION_MISMATCH", local=dict(pos),
                venue=dict(venue_position),
                detail=f"local stop {local_stop} != venue stopLoss {venue_stop}"))
            continue
        items.append(ReconciliationItem(
            kind="protection", identity=symbol, classification="MATCHED",
            local=dict(pos), venue=dict(venue_position),
            detail=f"venue stopLoss={venue_stop} matches local stop_price"))
    return items


def reconcile_linear_position(
    *, local_positions: Sequence[Mapping[str, Any]], symbol: str,
    venue_position: Optional[Mapping[str, Any]],
) -> ReconciliationItem:
    local = next((p for p in local_positions if p.get("symbol") == symbol), None)
    if local is None and venue_position is None:
        return ReconciliationItem(
            kind="position", identity=symbol, classification="MATCHED",
            local=None, venue=None, detail="flat on both sides")
    if local is not None and venue_position is None:
        return ReconciliationItem(
            kind="position", identity=symbol, classification="LOCAL_ONLY",
            local=dict(local), venue=None,
            detail="local position has no venue counterpart")
    if local is None and venue_position is not None:
        return ReconciliationItem(
            kind="position", identity=symbol, classification="VENUE_ONLY",
            local=None, venue=dict(venue_position),
            detail="venue holds a position the local ledger does not know "
                   "(orphan position -- a human must resolve it)")
    assert local is not None and venue_position is not None
    local_side = str(local.get("side", "")).strip().lower()
    venue_side = str(venue_position.get("side", "")).strip().lower()
    if local_side and venue_side and local_side != venue_side:
        return ReconciliationItem(
            kind="position", identity=symbol, classification="SIDE_MISMATCH",
            local=dict(local), venue=dict(venue_position),
            detail=f"local side {local_side!r} != venue side {venue_side!r}")
    local_qty = _f(local.get("qty"))
    venue_qty = _f(venue_position.get("size"))
    if not _isclose(local_qty, venue_qty, abs_tol=QTY_TOLERANCE_ABS):
        return ReconciliationItem(
            kind="position", identity=symbol, classification="QUANTITY_MISMATCH",
            local=dict(local), venue=dict(venue_position),
            detail=f"local qty {local_qty} != venue size {venue_qty}")
    return ReconciliationItem(
        kind="position", identity=symbol, classification="MATCHED",
        local=dict(local), venue=dict(venue_position), detail="position matches")


def reconcile_spot_inventory(
    *, local_positions: Sequence[Mapping[str, Any]], symbol: str,
    base_asset: str, venue_base_available: float,
    baseline: Optional[Mapping[str, Any]],
) -> ReconciliationItem:
    """SPOT has no venue "position" object -- only a coin balance. This is
    exactly the distinction mission Finding #1 (see
    `tools/testnet_conformance_run.capture_spot_baseline`) exists for: a
    non-zero balance with no local position is PRE-EXISTING INVENTORY,
    never this run's position, and is reported as such rather than as a
    drift. A local position WITH no baseline evidence to attribute the
    balance to this run is UNKNOWN -- never silently MATCHED."""
    local = next((p for p in local_positions if p.get("symbol") == symbol), None)
    if local is None:
        return ReconciliationItem(
            kind="position", identity=symbol, classification="MATCHED",
            local=None, venue={"base_asset": base_asset,
                                "available": venue_base_available},
            detail=f"no local position for {symbol}; venue {base_asset} "
                   f"balance {venue_base_available} is pre-existing "
                   "inventory (or untracked by this run) and is not "
                   "attributed to a position this run created")
    if baseline is None:
        return ReconciliationItem(
            kind="position", identity=symbol, classification="UNKNOWN",
            local=dict(local), venue={"base_asset": base_asset,
                                       "available": venue_base_available},
            detail="a local spot position is recorded but no run-scoped "
                   "baseline evidence (--compare-evidence) was supplied to "
                   "attribute the venue balance to this run's own entry; "
                   "refusing to call this MATCHED")
    starting = _f(baseline.get("starting_balance"))
    net_run_qty = _f(baseline.get("net_run_qty"))
    expected = starting + net_run_qty
    if not _isclose(expected, venue_base_available, abs_tol=QTY_TOLERANCE_ABS):
        return ReconciliationItem(
            kind="position", identity=symbol, classification="QUANTITY_MISMATCH",
            local=dict(local), venue={"base_asset": base_asset,
                                       "available": venue_base_available},
            detail=f"expected balance (baseline {starting} + run-owned delta "
                   f"{net_run_qty} = {expected}) != venue balance "
                   f"{venue_base_available}")
    return ReconciliationItem(
        kind="position", identity=symbol, classification="MATCHED",
        local=dict(local), venue={"base_asset": base_asset,
                                   "available": venue_base_available},
        detail="venue balance matches baseline + this run's own delta")


# ---------------------------------------------------------------------------
# evidence linkage (--compare-evidence)
# ---------------------------------------------------------------------------


def _extract_rest_order_identities(
    rest_records: Sequence[Mapping[str, Any]], *, symbol: str,
) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for record in rest_records:
        if record.get("method") != "POST":
            continue
        if not str(record.get("request_url", "")).endswith("/v5/order/create"):
            continue
        try:
            body = json.loads((record.get("request") or {}).get("body") or "{}")
        except (TypeError, ValueError):
            body = {}
        if str(body.get("symbol", "")) != symbol:
            continue
        response = record.get("response") or {}
        result = response.get("result") or {}
        out.append({
            "order_link_id": str(body.get("orderLinkId")
                                 or result.get("orderLinkId", "")),
            "order_id": str(result.get("orderId", "")),
            "side": str(body.get("side", "")),
            "order_filter": str(body.get("orderFilter", "Order")),
            "captured_at_utc": record.get("captured_at_utc", ""),
        })
    return out


def _extract_ws_events(
    ws_records: Sequence[Mapping[str, Any]], *, symbol: str,
) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for record in ws_records:
        if record.get("transport") != "ws":
            continue
        topic = str((record.get("request") or {}).get("topic", ""))
        if topic not in ("order", "execution"):
            continue
        response = record.get("response") or {}
        if response.get("symbol") and str(response.get("symbol")) != symbol:
            continue
        out.append({
            "topic": topic,
            "order_link_id": str(record.get("order_link_id")
                                 or response.get("orderLinkId", "")),
            "order_id": str(response.get("orderId", "")),
            "exec_id": str(response.get("execId", "")),
            "side": str(response.get("side", "")),
            "order_status": str(response.get("orderStatus", "")),
        })
    return out


def _extract_spot_baseline(
    conformance_result: Optional[Mapping[str, Any]],
) -> Optional[Dict[str, Any]]:
    """Read Finding #1's run-scoped baseline back out of a committed
    `testnet_conformance_result.json`-shaped dict (read-only -- this
    module never writes to that file). Returns `None` when the result has
    no successful baseline stage, so a caller cannot manufacture a
    baseline from a run that never captured one."""
    if not conformance_result:
        return None
    stages = {
        str(s.get("stage")): s for s in conformance_result.get("stages", [])
        if s.get("ok")
    }
    baseline_stage = stages.get("spot_baseline_capture")
    if baseline_stage is None:
        return None
    entry_stage = stages.get("entry") or {}
    flatten_stage = stages.get("flatten") or {}
    entry_qty = _f((entry_stage.get("position") or {}).get("qty"))
    exit_qty = _f(flatten_stage.get("executed_exit_qty"))
    return {
        "base_asset": baseline_stage.get("base_asset", ""),
        "starting_balance": _f(baseline_stage.get("starting_balance")),
        "entry_qty": entry_qty,
        "exit_qty": exit_qty,
        "net_run_qty": entry_qty - exit_qty,
    }


def compare_against_evidence(
    *, rest_evidence_path: str, ws_evidence_path: str, symbol: str,
    conformance_result_path: Optional[str] = None,
) -> Dict[str, Any]:
    """Load the committed evidence artifacts, hash-chain-verify them with
    `venue_evidence.verify_chain` (never a second checker), and correlate
    REST order identities (`orderLinkId`/`orderId`) against WS
    order/execution events (`orderLinkId`/`orderId`/`execId`) for
    `symbol`. Never raises -- every failure mode is reported in the
    returned dict."""
    result: Dict[str, Any] = {
        "rest_evidence_path": rest_evidence_path,
        "ws_evidence_path": ws_evidence_path,
    }
    if not (os.path.exists(rest_evidence_path) and os.path.exists(ws_evidence_path)):
        result.update(readable=False, chain_valid=False, identities_agree=False,
                      ok=False, notes=["one or both evidence files are missing"])
        return result
    try:
        rest_records = _read_jsonl(rest_evidence_path)
        ws_records = _read_jsonl(ws_evidence_path)
    except (OSError, ValueError) as exc:
        result.update(readable=False, chain_valid=False, identities_agree=False,
                      ok=False, notes=[f"evidence unreadable: {exc}"])
        return result

    rest_chain_reasons = ve.verify_chain(rest_evidence_path)
    ws_chain_reasons = ve.verify_chain(ws_evidence_path)
    chain_valid = not rest_chain_reasons and not ws_chain_reasons

    rest_orders = _extract_rest_order_identities(rest_records, symbol=symbol)
    ws_events = _extract_ws_events(ws_records, symbol=symbol)

    identities_agree = True
    notes: List[str] = []
    for order in rest_orders:
        oid = order["order_link_id"]
        matching = [e for e in ws_events if e["order_link_id"] == oid]
        if not matching:
            identities_agree = False
            notes.append(f"{oid}: no WS order/execution evidence observed")
            continue
        if order["order_id"] and not any(
                e["order_id"] == order["order_id"] for e in matching):
            identities_agree = False
            notes.append(
                f"{oid}: REST orderId {order['order_id']!r} not confirmed "
                "by any WS event for the same orderLinkId")

    conformance_result = None
    if conformance_result_path and os.path.exists(conformance_result_path):
        try:
            with open(conformance_result_path, encoding="utf-8") as handle:
                conformance_result = json.load(handle)
        except (OSError, ValueError) as exc:
            notes.append(f"conformance result unreadable: {exc}")

    result.update(
        readable=True,
        chain_valid=chain_valid,
        rest_chain_reasons=rest_chain_reasons,
        ws_chain_reasons=ws_chain_reasons,
        rest_orders=rest_orders,
        ws_events=ws_events,
        identities_agree=identities_agree,
        notes=notes,
        spot_baseline=_extract_spot_baseline(conformance_result),
        ok=bool(chain_valid and identities_agree),
    )
    return result


# ---------------------------------------------------------------------------
# packet assembly
# ---------------------------------------------------------------------------


def _refused_packet(header: Dict[str, Any], *, reason: str, detail: str) -> Dict[str, Any]:
    packet = dict(header)
    packet["ok"] = False
    packet["reasons"] = [reason]
    packet["refused"] = True
    packet["detail"] = detail
    packet["local"] = None
    packet["venue"] = None
    packet["reconciliation"] = []
    return packet


def build_packet(
    *, cfg: Any, store: StateStore, client: Any, symbol: str,
    compare_evidence: bool = False,
    rest_evidence_path: str = DEFAULT_REST_EVIDENCE_PATH,
    ws_evidence_path: str = DEFAULT_WS_EVIDENCE_PATH,
    conformance_result_path: str = DEFAULT_CONFORMANCE_RESULT_PATH,
    repo: Optional[str] = None,
) -> Dict[str, Any]:
    """Build one reconciliation packet. Never raises: every failure mode
    (unreadable local state, unreadable venue, bad evidence) is captured
    into the packet's own `ok`/`reasons`, because a packet that crashed
    instead of reporting `ok=False` is a worse failure mode for an
    assurance tool than the condition it was checking for."""
    category = "linear" if bool(getattr(client, "is_linear", False)) else "spot"
    environment = str(getattr(cfg, "BYBIT_VENUE", "") or "")
    header = {
        "schema": SCHEMA,
        "run_id": _new_run_id(),
        "observed_at_utc": _utc_now(),
        "repository_commit": _provenance.git_commit(repo),
        "venue": "bybit",
        "environment": environment,
        "category": category,
        "symbol": symbol,
    }

    if environment != "testnet":
        raise ReconciliationRefused(
            f"BYBIT_VENUE={environment!r}; this tool only ever reads "
            "testnet -- refusing before a single request is built, exactly "
            "like tools/testnet_conformance_run.verify_environment")

    reasons: List[str] = []
    items: List[ReconciliationItem] = []

    try:
        local = build_local_snapshot(store, symbol)
        local_ok = True
    except Exception as exc:  # noqa: BLE001
        local = {"error": f"{type(exc).__name__}: {exc}"}
        local_ok = False
        reasons.append("LOCAL_STATE_UNREADABLE")

    try:
        if category == "linear":
            venue = build_linear_venue_snapshot(client, symbol)
        else:
            venue = build_spot_venue_snapshot(client, symbol)
        venue_ok = True
    except Exception as exc:  # noqa: BLE001
        venue = {"error": f"{type(exc).__name__}: {exc}"}
        venue_ok = False
        reasons.append("VENUE_UNREADABLE")

    evidence_block: Optional[Dict[str, Any]] = None
    if compare_evidence:
        evidence_block = compare_against_evidence(
            rest_evidence_path=rest_evidence_path,
            ws_evidence_path=ws_evidence_path, symbol=symbol,
            conformance_result_path=conformance_result_path)
        if not evidence_block.get("readable"):
            reasons.append("EVIDENCE_UNREADABLE")
        elif not evidence_block.get("chain_valid"):
            reasons.append("EVIDENCE_CHAIN_INVALID")

    if local_ok and venue_ok:
        order_link_prefix = str(getattr(cfg, "ORDERLINK_PREFIX", "BB"))
        if category == "linear":
            venue_position = venue.get("position")
            # A LINEAR protective stop (`_place_position_stop`) is a FIELD
            # on the position, never a separate resting order -- unlike
            # spot's conditional StopOrder. The local `orders` table still
            # gets a bookkeeping row for it (purpose="stop"), but it has no
            # venue *order* counterpart to match against; comparing it here
            # would manufacture a false LOCAL_ONLY finding for every linear
            # stop. `reconcile_linear_protection` below is what actually
            # checks it, against the position's own `stopLoss` field.
            order_rows = [o for o in local["orders"]
                          if str(o.get("purpose", "")) != "stop"]
            items.extend(reconcile_orders(
                local_orders=order_rows,
                venue_orders_by_filter={"Order": venue["orders_by_link_id"]},
                order_link_prefix=order_link_prefix))
            items.append(reconcile_linear_position(
                local_positions=local["positions"], symbol=symbol,
                venue_position=venue_position))
            items.extend(reconcile_linear_protection(
                local_positions=local["positions"], venue_position=venue_position))
        else:
            items.extend(reconcile_orders(
                local_orders=local["orders"],
                venue_orders_by_filter=venue["orders_by_filter"],
                order_link_prefix=order_link_prefix))
            baseline = (evidence_block or {}).get("spot_baseline")
            items.append(reconcile_spot_inventory(
                local_positions=local["positions"], symbol=symbol,
                base_asset=venue["base_asset"],
                venue_base_available=venue["base_balance"]["available"],
                baseline=baseline))
            items.extend(reconcile_spot_protection(
                local_positions=local["positions"],
                stop_orders_by_link_id=venue["orders_by_filter"].get(
                    "StopOrder", {})))

        for item in items:
            reason = _reason_for(item)
            if reason:
                reasons.append(reason)

    reasons = sorted(set(reasons)) if reasons else ["RECONCILED"]
    ok = bool(
        local_ok and venue_ok and reasons == ["RECONCILED"]
        and (evidence_block is None or evidence_block.get("ok") is True))

    packet = dict(header)
    packet["ok"] = ok
    packet["reasons"] = reasons
    packet["local"] = local
    packet["venue"] = venue
    packet["reconciliation"] = [item.to_dict() for item in items]
    if evidence_block is not None:
        packet["evidence_comparison"] = evidence_block
    return packet


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_real_stack(*, state_db: str) -> Tuple[Any, StateStore, Any]:
    """The real, read-only stack: `config.load()`, a real `StateStore`, and
    a real `BybitClient` over the real `RequestsTransport` -- the SAME
    client every other tool in this tree uses, never a second one. This
    tool issues no writes, so it deliberately does NOT wrap the transport
    in `venue_evidence.EvidenceCapturingTransport`: there is nothing here
    for that capture layer to redact-and-chain that this packet does not
    already report in its own `venue` block, and skipping it keeps this
    tool from creating evidence-log entries for reads that were never
    part of a conformance run."""
    import bybit_connection as bc

    cfg = _config.load()
    store = StateStore(state_db)
    client = bc.BybitClient(config=cfg, store=store, transport=bc.RequestsTransport())
    return cfg, store, client


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--symbol", default=SYMBOL)
    parser.add_argument("--state-db", default=DEFAULT_STATE_DB)
    parser.add_argument("--compare-evidence", action="store_true",
                        help="also correlate identities against the "
                             "committed conformance evidence artifacts")
    parser.add_argument("--evidence-path", default=DEFAULT_REST_EVIDENCE_PATH,
                        help="REST evidence log (venue_evidence.py)")
    parser.add_argument("--ws-evidence-path", default=DEFAULT_WS_EVIDENCE_PATH,
                        help="private-WS evidence log (venue_evidence.py)")
    parser.add_argument("--conformance-result", default=DEFAULT_CONFORMANCE_RESULT_PATH,
                        help="testnet_conformance_run.py result.json, read "
                             "only for its run-scoped spot baseline")
    parser.add_argument("--out", default=DEFAULT_OUT_PATH)
    args = parser.parse_args(argv)

    print("=" * 72)
    print("recon_packet: READ-ONLY reconciliation packet -- NO MUTATION.")
    print("This tool never places, cancels, or amends an order or position.")
    print("=" * 72)

    try:
        cfg, store, client = _build_real_stack(state_db=args.state_db)
    except Exception as exc:  # noqa: BLE001
        print(f"could not build the read-only stack: {type(exc).__name__}: {exc}",
              file=sys.stderr)
        return 2

    try:
        packet = build_packet(
            cfg=cfg, store=store, client=client, symbol=args.symbol,
            compare_evidence=args.compare_evidence,
            rest_evidence_path=args.evidence_path,
            ws_evidence_path=args.ws_evidence_path,
            conformance_result_path=args.conformance_result)
    except ReconciliationRefused as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 1

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(packet, handle, indent=2, sort_keys=True, default=str)
        handle.write("\n")

    print(f"ok={packet['ok']} reasons={packet['reasons']}")
    print(f"packet written to {args.out}")
    return 0 if packet["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
