#!/usr/bin/env python3
"""The Phase D drill, as a program. Staged, evidenced, and written down.

WHAT WAS MISSING
================
Phase D is "venue truth at $100, recorded drill", and there was no drill —
there was a list of things a human would remember to do in the right order, in
a markdown file, with nothing checking that they happened or that they happened
in that order.

That is the distance between a plan and an asset. A drill that lives in prose
gets half-run at 2am by somebody tired, and its evidence is a terminal
scrollback nobody kept.

WHAT THIS IS
============
Eight stages. Each produces EVIDENCE — a number the venue actually returned,
never a checkmark — and the drill refuses to advance when one fails. What it
writes is the Phase D deliverable: what was asked, what the venue answered,
what was sent, what came back, and what the ledger says afterwards.

    reachability   can this host reach the venue, and what is the mark
    flat           is the book already holding something (if so: stop)
    venue_rules    lot step and minimum, read not assumed (0036)
    fee_tier       what THIS account pays, read not assumed
    inventory      BTC the client owns; an overlay with none measures nothing
    warmup         the settled prints the EWMA needs before it will decide
    margin         headroom before the floor
    cap            the size, which is $100 and does not get an exemption
    open           one perp leg against the inventory
    pair           both sides agree, against the VENUE not the ledger
    unwind         close it
    final_reconcile   flat at the venue, not flat in our opinion
    statement      what it cost, out of the double-entry journal

SAFE BY DEFAULT
===============
Without `--arm` it reads, reports and sends nothing. With `--arm` it still goes
through the same order gate as every other path: PAPER refuses, mainnet needs
live authorisation AND a signed promotion gate. This is not a way around any of
that, and it has no order path of its own — every order goes through
`CarryEngine`, which goes through the broker. A test asserts the module never
touches `_request`.

WHY IT IS TESTED AGAINST A FAKE VENUE
=====================================
Because the sequence must be right BEFORE it meets a real one. Every failure
below — unreachable venue, empty wallet, a leg that will not land, a pair that
will not reconcile, an unwind that fails — is exercised at zero cost in
`tests/test_drill.py`. The first run against Bybit will be the hundredth run of
this sequence.

    python3 tools/drill.py                      # preflight only, sends nothing
    python3 tools/drill.py --arm --out artifacts/drill.json

HOLD ACROSS A FUNDING SETTLEMENT (the cycle that matters)
=========================================================
``--hold-for-settlement`` adds a ``hold`` stage between ``pair`` and ``unwind``
and a ``funding_final`` stage after ``final_reconcile``. The pair is held, and
watched every poll by the engine's own ``on_candle`` (margin, venue mismatch,
inventory cover, inversion exit all still apply), until the account's
transaction log has paid at least ``--min-settlements`` SETTLEMENT rows that
the journal booked as venue funding, or ``--max-hold-hours`` run out (then the
pair is unwound and the drill FAILS: no settlement is not a result). After the
unwind it re-reads the log until the evidence is complete and the lifecycle is
reconciled. ``--cash-ledger PATH`` writes ``cash_ledger/2`` (``allows_live``
always false) whenever the book ends flat after fills, with the funding status
stated, never assumed:

    CONFIRMED_FROM_SETTLEMENTS   rows the venue paid, sign cross-checked,
                                 evidence complete, lifecycle reconciled
                                 (the ledger judges a closed lifecycle only
                                 6 h after the close: `--settle-wait-hours`)
    BOOKED_PENDING_RECONCILIATION  the rows are booked, but that 6 h grace has
                                 not passed; exit 5, never 0
    NO_SETTLEMENT_OBSERVED       evidence complete and no row: not a result
    UNCONFIRMED                  anything else (incomplete, unattributed, ...)

The drill runs the OVERLAY mode (the production choice): the short perp is
written against BTC the account already owns, so the "long" leg is that
inventory. ACQUIRE (buy the spot leg too) is deliberately not offered here: the
engine refuses it whenever one perp lot is a large share of the position
(`ACQUIRE_LOT_TOO_COARSE_FOR_FEE`: the base-coin fee on the spot buy makes the
snapped short land unpaired), and under the fixed drill cap on BTCUSDT one lot
is the whole position. Raising the cap is a signed human decision, not a flag.

Exit 0 only when the drill PASSED, the book is flat and the funding status is
CONFIRMED_FROM_SETTLEMENTS. 3 = refused (not testnet / paper / live-armed),
2 = cannot start or zero fills, 4 = flat but funding not confirmed,
5 = flat, rows booked, reconciliation still inside its grace, 1 = other,
6 = would be 0 but `--evidence-path` was given and the final venue state (the
orders' own rows and their execution rows) could not be captured, 130 = stopped
by SIGINT/SIGTERM/SIGHUP, the pair closed and the report written (1 instead if
the book is not flat).

INTERRUPTS. The first SIGINT/SIGTERM/SIGHUP raises KeyboardInterrupt inside the
drill: the engine's own reduce-only unwind runs, the venue is read, the report
(and, with `--evidence-path`, the final-state capture) is written, and the
process exits 130 with a sentence, not a traceback. A SECOND signal while that
is happening is ignored, because interrupting the unwind would leave the short
open.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, ROOT)

SPOT_SYMBOL = "BTCUSDT"
PERP_SYMBOL = "BTCUSDT"

#: What a completed drill still does NOT establish. Printed on every run and
#: written into the transcript, because a document that lists only what passed
#: is a document that will be read as "everything passed".
NOT_PROVEN = [
    "liquidation behaviour: positionIM/positionMM is a risk-tier ratio, not "
    "headroom (INVENTORY F4). One $100 pair does not exercise it.",
    "rate limits and venue maintenance windows (F8): two orders in a minute "
    "meets neither.",
    "the create/query race (F2): a response that arrives after the fill is a "
    "timing accident this drill cannot schedule.",
    "duplicate-order codes 110072/170130 (F7): nothing here sends a duplicate.",
    "post-only fill probability (F10/F11): the drill is taker.",
    "anything at all about size. $100 is not evidence about $100,000.",
]


#: Flat to within this many base units counts as flat at the venue.
FLAT_DUST = 1e-8

#: Seconds between re-reads of an order's execution rows when they have not yet
#: added up to the order's own cumulative quantity.
FINAL_READ_RETRY_S = 1.0
FINAL_READ_TRIES = 4


def unwrap_broker(broker: Any) -> Any:
    """The venue adapter under any `LedgerBroker` layers.

    In a real stack `bot.carry.broker` is a `LedgerBroker` around the
    `CarryBroker`, and the ledger delegates EXPLICITLY: it carries none of the
    adapter's reads (`sent_order_links`, `read_order`, `get_order_executions`)
    nor its `client`. Anything that needs those must ask the adapter. A fake
    with no `inner` is its own adapter."""
    for _ in range(8):
        inner = getattr(broker, "inner", None)
        if inner is None or hasattr(broker, "sent_order_links"):
            return broker
        broker = inner
    if getattr(broker, "inner", None) is not None \
            and not hasattr(broker, "sent_order_links"):
        raise ValueError("broker wrapper chain is too deep to reach the venue "
                         "adapter; refusing to guess")
    return broker


def _vwap(rows: List[Dict[str, Any]]) -> Optional[float]:
    qty = sum(r["qty"] for r in rows)
    return sum(r["qty"] * r["price"] for r in rows) / qty if qty > 0 else None


def round_trip_summary(fills: List[Dict[str, Any]]) -> Dict[str, Any]:
    """What the PERP leg of this drill cost, from the fills the book booked.

    Entry and exit prices are volume-weighted over the perp fills; fees are the
    booked USDT fees. Nothing here is estimated: a figure that needs a close
    that did not happen is None. SCOPE: the perp leg only. In the overlay the
    long side is the client's own BTC, whose price change is NOT in these
    numbers, and funding is reported separately (the journal statement).
    """
    perp = [f for f in fills if f.get("product") == "linear"]
    opens = [f for f in perp if f["side"] == "Sell"]
    closes = [f for f in perp if f["side"] == "Buy"]
    open_qty = sum(f["qty"] for f in opens)
    close_qty = sum(f["qty"] for f in closes)
    entry, exit_ = _vwap(opens), _vwap(closes)
    entry_fee = sum(f["fee_usdt"] for f in opens)
    exit_fee = sum(f["fee_usdt"] for f in closes)
    confirmed = all(f.get("fee_status") == "confirmed" for f in perp) and bool(perp)
    out: Dict[str, Any] = {
        "scope": "perp leg only; the spot inventory's price change and "
                 "funding are not in these numbers",
        "entry_qty": open_qty, "entry_price": entry, "entry_fee_usdt": entry_fee,
        "exit_qty": close_qty, "exit_price": exit_, "exit_fee_usdt": exit_fee,
        "exit_order_link_ids": [f["ref"] for f in closes],
        "fees_confirmed": confirmed,
        "closed": bool(opens) and abs(open_qty - close_qty) <= FLAT_DUST,
        "perp_gross_usdt": None, "fees_usdt": entry_fee + exit_fee,
        "perp_net_before_funding_usdt": None,
        "fee_bps_of_entry_notional": None, "price_move_bps": None}
    if confirmed and entry and entry > 0 and open_qty > 0:
        out["fee_bps_of_entry_notional"] = (
            (entry_fee + exit_fee) / (entry * open_qty) * 1e4)
    if entry and exit_ and close_qty > 0:
        gross = (entry - exit_) * close_qty          # a short gains when price falls
        out["perp_gross_usdt"] = gross
        out["price_move_bps"] = (exit_ / entry - 1.0) * 1e4
        # a net needs every fee known and the whole entry closed: a partial
        # close would charge the full entry fee against part of the move
        if confirmed and out["closed"]:
            out["perp_net_before_funding_usdt"] = gross - entry_fee - exit_fee
    return out


def _exit_fields(fills: List[Dict[str, Any]]) -> Dict[str, Any]:
    """The exit's own price and fee, for the stage that closed the pair."""
    s = round_trip_summary(fills)
    return {"exit_qty": s["exit_qty"], "exit_price": s["exit_price"],
            "exit_fee_usdt": s["exit_fee_usdt"],
            "exit_order_link_ids": s["exit_order_link_ids"],
            "round_trip_fees_usdt": s["fees_usdt"]}


def drill_claims(fills: List[Dict[str, Any]], journal: Any, *, venue: str,
                 symbol: str = PERP_SYMBOL) -> Dict[str, Any]:
    """The drill's own statement of what it did, in the form
    `tools/evidence_reconcile.py --drill-report` checks against a venue capture.

    These are CLAIMS: every number comes from the book, none from the venue's
    execution list. A testnet run claims `testnet_claim`; anything else is a
    local record and can never reconcile.
    """
    testnet = str(venue).lower() == "testnet"
    source, env = ("testnet_claim", "testnet") if testnet else ("local", "unknown")
    legs = []
    for f in fills:
        if f.get("product") != "linear":
            continue
        cur = f.get("fee_currencies") or []
        legs.append({
            "claim_id": f"leg:{f['ref']}", "source": source, "environment": env,
            "kind": "perp_open" if f["side"] == "Sell" else "perp_close",
            "symbol": symbol, "side": f["side"], "qty": f["qty"],
            "price": f["price"], "fee": f["fee_usdt"],
            "fee_currency": cur[0] if len(cur) == 1 else ",".join(cur),
            "fee_status": f.get("fee_status", ""),
            "order_link_id": f["ref"], "reduce_only": f["side"] == "Buy",
            "booked_ms": f["ms"]})
    funding = []
    for e in (journal.entries if journal is not None else []):
        if e.kind == "funding" and e.basis == "venue" and "venue_id" in e.fee_raw:
            r = e.fee_raw
            funding.append({
                "claim_id": f"funding:{r['venue_id']}", "source": source,
                "environment": env, "symbol": r.get("symbol") or symbol,
                "time_ms": r["venue_ms"], "amount": r["venue_usd"],
                "venue_id": r["venue_id"], "size": r.get("size")})
    return {"schema": "drill_claims/1", "carry_leg_claims": legs,
            "funding_claims": funding}


def capture_final_venue_state(broker: Any, symbol: str = PERP_SYMBOL,
                              sleep: Callable[[float], None] = None
                              ) -> Dict[str, Any]:
    """Read, from the venue, the FINAL state and the fills of every perp order
    this process sent -- so a capture holds them and
    `tools/evidence_reconcile.py` can reconcile them.

    Mirrors `testnet_conformance_run.capture_final_venue_state`: only GETs, all
    through the broker's client (hence the evidence-capturing transport), and
    green only when every sent order was actually read. Per order: the order's
    own record must exist and be final, and when it executed its execution
    rows must add up to its cumulative quantity (re-read a few times: the list
    can lag the fill). Also one position read, as the flat proof. Sends,
    amends and cancels nothing.
    """
    sleep = sleep or time.sleep
    broker = unwrap_broker(broker)
    if not hasattr(broker, "sent_order_links"):
        return {"complete": False, "orders": 0, "reads": 0, "failures": [
            "the broker cannot say which orders it sent; a capture of nothing "
            "is not a capture"]}
    links = [(p, sym, link) for (p, sym, link)
             in broker.sent_order_links if p == "linear"]
    failures: List[str] = []
    reads = 0
    for _product, sym, link in links:
        try:
            state = broker.read_order(sym, link, "linear")
            reads += 1
            if state is None:
                failures.append(f"{link}: the venue returned no order")
                continue
            if state.get("resting"):
                failures.append(f"{link}: still {state.get('status')!r} at the "
                                "venue, not a final state")
                continue
            filled = float(state.get("filled_qty") or 0.0)
            if filled <= 0:
                continue
            total = 0.0
            for attempt in range(FINAL_READ_TRIES):
                rows = broker.get_order_executions(sym, order_link_id=link)
                reads += 1
                total = sum(float(r.get("execQty") or 0.0) for r in rows)
                if abs(total - filled) <= max(1e-12, 1e-9 * filled):
                    break
                if attempt + 1 < FINAL_READ_TRIES:
                    sleep(FINAL_READ_RETRY_S)
            else:
                failures.append(f"{link}: execution rows add up to {total} but "
                                f"the order filled {filled}")
        except Exception as exc:                               # noqa: BLE001
            failures.append(f"{link}: {type(exc).__name__}: {exc}")
    try:
        broker.get_perp_position(symbol)
        reads += 1
    except Exception as exc:                                   # noqa: BLE001
        failures.append(f"position: {type(exc).__name__}: {exc}")
    return {"complete": not failures, "orders": len(links), "reads": reads,
            "failures": failures[:20]}


def venue_flatness(broker: Any, *, inventory_before: Optional[float]
                   ) -> tuple[str, Dict[str, Any], str]:
    """A FRESH read of what the venue holds, never what the book believes.

    Returns ``(verdict, evidence, why)``; verdict is FLAT, NOT_FLAT or
    UNVERIFIED. Unreadable is UNVERIFIED, which every caller treats as "a
    human must look": a flat claim that could not be checked is not a flat
    claim. Three independent facts must agree:

      * the perp position at the venue is zero,
      * no carry order is still open (one could fill into a flat book),
      * the client's spot inventory is what it was before the drill (the
        overlay places no spot order, so any change is a trade nobody asked
        for).
    """
    try:
        perp = abs(float(broker.get_perp_position(PERP_SYMBOL)))
        orders = list(broker.get_open_carry_orders(SPOT_SYMBOL, PERP_SYMBOL))
        inventory = float(broker.get_spot_inventory(SPOT_SYMBOL))
    except Exception as exc:                                   # noqa: BLE001
        return "UNVERIFIED", {"error": f"{type(exc).__name__}: {exc}"}, (
            f"the venue could not be read ({type(exc).__name__}: {exc}); "
            "flatness is unverified")
    evidence: Dict[str, Any] = {
        "venue_perp_qty": perp, "open_orders": len(orders),
        "spot_inventory": inventory, "spot_inventory_before": inventory_before}
    problems = []
    if perp > FLAT_DUST:
        problems.append(f"the venue still holds {perp} perp")
    if orders:
        problems.append(f"{len(orders)} carry order(s) are still open")
    if inventory_before is not None and abs(inventory - inventory_before) > max(
            FLAT_DUST, 1e-6 * abs(inventory_before)):
        problems.append(
            f"spot inventory moved from {inventory_before} to {inventory}")
    if problems:
        return "NOT_FLAT", evidence, "; ".join(problems)
    return "FLAT", evidence, ""


def margin_stage_evidence(*, broker: Any, symbol: str, venue_perp_qty: float,
                          min_margin_multiple: float
                          ) -> tuple[bool, Dict[str, Any], str]:
    """The `margin` preflight stage's decision, pulled out to a pure
    function so both branches are directly testable — the full drill
    sequence can only ever reach this stage while `flat` (venue_perp_qty ==
    0, by the preflight's own design), so the size>0 branch below has no
    other way to be exercised.

    Returns `(ok, evidence, why)`. `why` is only meaningful when `ok` is
    False. Never catches an exception from `get_margin_multiple` itself:
    when a position genuinely exists, an unreadable margin must still
    propagate exactly as it always has (INVENTORY F4's fail-closed contract
    is unchanged; this function only decides WHETHER to ask).
    """
    if venue_perp_qty <= 0:
        return True, {
            "margin_multiple": None, "reason": "NO_POSITION",
            "note": "positionIM/positionMM is undefined while flat "
                    "(INVENTORY F4); margin/liquidation evidence is "
                    "gathered AFTER open at ~$100 notional (Part 1 item 4), "
                    "not required by preflight",
        }, ""
    multiple = float(broker.get_margin_multiple(symbol))
    if multiple < min_margin_multiple:
        return False, {"margin_multiple": multiple}, (
            f"margin multiple {multiple} is below the "
            f"{min_margin_multiple} floor")
    return True, {
        "margin_multiple": multiple, "floor": min_margin_multiple,
        "note": "positionIM/positionMM — a risk-tier ratio, NOT liquidation "
                "headroom (INVENTORY F4)",
    }, ""


class Stage:
    """One step. Records what the venue said, not that it was asked."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.at_ms = int(time.time() * 1000)
        self.evidence: Dict[str, Any] = {}
        self.ok = False
        self.why = ""

    def passed(self, **evidence: Any) -> "Stage":
        self.evidence.update(evidence)
        self.ok = True
        return self

    def failed(self, why: str, **evidence: Any) -> "Stage":
        self.evidence.update(evidence)
        self.ok = False
        self.why = why
        return self

    def as_dict(self) -> Dict[str, Any]:
        return {"stage": self.name, "at_ms": self.at_ms, "ok": self.ok,
                "why": self.why, "evidence": dict(self.evidence)}


class Drill:
    def __init__(self, *, broker: Any, notional: float, arm: bool) -> None:
        self.broker = broker
        self.notional = notional
        self.arm = arm
        self.stages: List[Stage] = []
        self.orders_sent = 0
        self.failed_stage: Optional[str] = None
        self.still_open = False
        #: Stopped by SIGINT/SIGTERM/SIGHUP (KeyboardInterrupt): the pair was
        #: closed through the engine and this report written on the way out.
        self.interrupted = False
        #: WHICH EXCHANGE this ran against (0046). Defaults to "unknown"
        #: rather than to mainnet: a transcript that does not know where it
        #: ran must not be readable as the strongest possible claim.
        self.venue = "unknown"

    def stage(self, name: str) -> Stage:
        s = Stage(name)
        self.stages.append(s)
        return s

    def run_stage(self, name: str, fn: Callable[[Stage], None]) -> bool:
        """Run one stage. An exception is a failure with the exception in it —
        never a traceback that ends the drill without a transcript."""
        s = self.stage(name)
        try:
            fn(s)
        except Exception as exc:                               # noqa: BLE001
            s.failed(f"{type(exc).__name__}: {exc}")
        if not s.ok:
            self.failed_stage = name
        return s.ok


@dataclass
class HoldPlan:
    """How long to hold the pair, and what has to be paid before it is closed."""

    min_settlements: int = 1
    max_hold_s: float = 9 * 3600.0
    poll_s: float = 60.0
    #: after the unwind: how long to keep re-reading the log for the evidence to
    #: become complete and the lifecycle reconciled.
    settle_wait_s: float = 6.25 * 3600.0
    sleep: Callable[[float], None] = time.sleep


CONFIRMED = "CONFIRMED_FROM_SETTLEMENTS"
#: Rows the venue paid are booked (sign cross-checked, evidence complete, nothing
#: unattributed) but the ledger will not call the lifecycle reconciled until
#: `ledger.MISSING_GRACE_MS` after the close: a late settlement could still
#: arrive. Not a confirmed result; a later read promotes it.
PENDING = "BOOKED_PENDING_RECONCILIATION"
NO_SETTLEMENT = "NO_SETTLEMENT_OBSERVED"
UNCONFIRMED = "UNCONFIRMED"


def venue_settlement_rows(journal: Any) -> int:
    """Funding entries the journal booked from a row the VENUE paid."""
    return sum(1 for e in journal.entries
               if e.kind == "funding" and e.basis == "venue")


def funding_status(statement: Dict[str, Any], rows: int, evidence_complete: bool,
                   min_settlements: int) -> str:
    """The one place the funding answer is decided. Never assumed."""
    enough = evidence_complete and rows >= max(1, int(min_settlements))
    if enough and statement.get("funding_confirmed") is True:
        return CONFIRMED
    if (enough and not statement.get("funding_unattributed")
            and not statement.get("funding_unread")
            and not statement.get("funding_estimated_entries")
            and statement.get("funding_lifecycles")
            and all(l.get("state") == "PENDING_DELIVERY_GRACE"
                    for l in statement["funding_lifecycles"])):
        return PENDING
    if evidence_complete and rows == 0:
        return NO_SETTLEMENT
    return UNCONFIRMED


def session_refusal(cfg: Any, client: Any = None) -> Optional[str]:
    """Why a hold session must not start here, or None.

    The cash ledger is TESTNET evidence: mainnet, demo, paper and any live
    authorisation are all refused up front, before a single order could be
    considered (the order gate would also refuse per order).
    """
    if str(getattr(cfg, "BYBIT_VENUE", "")).lower() != "testnet":
        return (f"BYBIT_VENUE={getattr(cfg, 'BYBIT_VENUE', None)!r}: a hold "
                "session runs on the testnet venue only")
    if not bool(getattr(cfg, "USE_TESTNET", False)):
        return "USE_TESTNET is not set: refusing a non-testnet configuration"
    if bool(getattr(cfg, "PAPER_TRADING", True)):
        return ("PAPER_TRADING is on: a paper fill is not venue evidence and "
                "the carry book has no paper venue")
    if str(getattr(cfg, "LIVE_TRADING_ACK", "") or ""):
        return "a live-trading acknowledgement is present: refusing"
    if client is not None:
        # POSITIVE identification: the client must say it talks to the testnet
        # host. A missing, demo, custom or mainnet URL is not testnet evidence.
        from bybit_connection import TESTNET_REST
        url = str(getattr(client, "base_url", "") or "").rstrip("/")
        if url != TESTNET_REST:
            return (f"the client's host is {url or 'unstated'!r}, not the "
                    f"testnet host {TESTNET_REST}")
    return None


def write_carry_cash_ledger(path: str, *, journal: Any, state: Dict[str, Any],
                            status: str, flat: bool, venue: str) -> str:
    """``cash_ledger/2``: one carry cycle, from the journal, nothing invented."""
    st = journal.statement()
    closes = [e for e in journal.entries if e.kind == "perp_close"]
    qty = sum(next((l.qty for l in e.lines if l.account == "PERP"), 0.0)
              for e in closes)
    realised = -sum(next((l.usd for l in e.lines if l.account == "REALISED"), 0.0)
                    for e in closes)
    entry = float(state.get("entry_price") or 0.0)
    exit_price = (entry - realised / qty) if qty > 0 and entry > 0 else 0.0
    payload = {
        "schema": "cash_ledger/2",
        "allows_live": False,
        "venue": venue,
        "category": "carry",
        "symbol": PERP_SYMBOL,
        "flat": bool(flat),
        "qty": float(state.get("entry_qty") or 0.0),
        "entry_price": entry,
        "exit_price": exit_price,
        "fills": int(st["fills"]),
        "fees_usd": float(st["fees_usd"]),
        "fees_confirmed": bool(st["fees_confirmed"]),
        "funding_status": status,
        "funding_venue_usd": float(st["funding_venue_usd"]),
        "funding_settlements": venue_settlement_rows(journal),
        "funding_confirmed": bool(st["funding_confirmed"]),
        "realised_usd": float(st["realised_usd"]),
        "net_usd": float(st["net_usd"]),
        "net_basis": st["net_basis"],
        "note": ("Testnet dollars are not cash. Funding is the amount the "
                 "venue's own SETTLEMENT rows paid, sign cross-checked; "
                 "'UNCONFIRMED' and 'NO_SETTLEMENT_OBSERVED' are not results."),
        "written_ms": int(time.time() * 1000),
    }
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True, default=str)
    os.replace(tmp, path)
    return path


#: Consecutive polls without a usable market view the hold tolerates.
HOLD_MAX_BLIND_POLLS = 5
#: Past the ledger's ownership slack, so the close is clearly after it.
HOLD_AGING_MARGIN_MS = 15_000


def run_drill(*, broker: Any, notional: Optional[float] = None,
              arm: bool = False, out: str = "", venue: str = "unknown",
              engine_factory: Any = None, hold: Optional[HoldPlan] = None,
              cash_ledger: str = "", capture_final: bool = False
              ) -> Dict[str, Any]:
    """The whole sequence. Returns the transcript whatever happens.

    ``capture_final`` (set by ``--evidence-path``): after the sequence, however
    it ended, read every order this process sent and its execution rows from
    the venue, so the evidence file holds what the report claims."""
    import shadow

    cap = float(shadow.SHADOW_MAX_NOTIONAL_USD)
    asked = cap if notional is None else float(notional)
    d = Drill(broker=broker, notional=asked, arm=arm)
    d.venue = str(venue or "unknown")
    state: Dict[str, Any] = {}

    def reachability(s: Stage) -> None:
        perp = float(broker.get_mark(PERP_SYMBOL))
        spot = float(broker.get_spot_mark(SPOT_SYMBOL))
        if not (perp > 0 and spot > 0):
            s.failed(f"marks unusable: perp {perp!r} spot {spot!r}")
            return
        state["perp"], state["spot"] = perp, spot
        s.passed(perp_mark=perp, spot_mark=spot,
                 basis_bps=(perp / spot - 1.0) * 1e4)

    def flat(s: Stage) -> None:
        held = abs(float(broker.get_perp_position(PERP_SYMBOL)))
        # The one source of truth for "is anything open" - `margin` below
        # reads this instead of asking the venue a second, contradictory way.
        state["venue_perp_qty"] = held
        orders = list(broker.get_open_carry_orders(SPOT_SYMBOL, PERP_SYMBOL))
        if held > 0 or orders:
            d.still_open = True       # something IS open: say so, not "nothing is"
            s.failed(
                f"the venue already holds {held} and has {len(orders)} open "
                "order(s). A drill that opens on top of something already "
                "there is not a drill, it is an incident.",
                venue_perp_qty=held, open_orders=len(orders))
            return
        s.passed(venue_perp_qty=held, open_orders=len(orders))

    def venue_rules(s: Stage) -> None:
        rules = broker.get_lot_rules(PERP_SYMBOL, "linear")
        state["rules"] = rules
        s.passed(**{k: float(v) for k, v in rules.items()})

    def fee_tier(s: Stage) -> None:
        fees = broker.get_fee_rates(PERP_SYMBOL, "linear")
        state["fees"] = fees
        # The overlay's round trip is two perp legs, and this is where the
        # number that gates every entry actually comes from.
        s.passed(round_trip_bps=2.0 * float(fees["taker_bps"]), **fees)

    def inventory(s: Stage) -> None:
        btc = float(broker.get_spot_inventory(SPOT_SYMBOL))
        step = float(state["rules"]["qty_step"])
        min_qty = float(state["rules"]["min_qty"])
        wanted = min(d.notional / state["perp"], btc)
        from carry_engine import snap_to_lot
        sized = snap_to_lot(wanted, step)
        state["qty"] = sized
        state["inventory_before"] = btc
        if btc <= 0:
            s.failed(
                "the wallet holds no BTC. CARRY_EXECUTION_MODE=overlay hedges "
                "coin the client already owns; with none the book refuses "
                "with NO_SPOT_INVENTORY and the drill measures nothing.",
                btc=btc)
            return
        if sized < min_qty or sized <= 0:
            s.failed(
                f"{btc} BTC sizes to {sized}, below the venue minimum "
                f"{min_qty}. A size the venue will not accept is a rejected "
                "order, not a smaller book.",
                btc=btc, sized_qty=sized, min_qty=min_qty)
            return
        s.passed(btc=btc, sized_qty=sized, notional_usd=sized * state["perp"])

    def warmup(s: Stage) -> None:
        """The EWMA needs settled prints before the gate will look at
        anything. The venue has them; reading them is catching up, not
        looking ahead (0045)."""
        prints = list(broker.get_funding_history(PERP_SYMBOL, limit=8))
        state["prints"] = prints
        if len(prints) < 3:
            s.failed(
                f"the venue returned {len(prints)} settled funding prints; "
                "the EWMA needs 3 before the book will decide anything.",
                prints=len(prints))
            return
        s.passed(prints=len(prints), newest_ms=prints[-1][1],
                 newest_bps=prints[-1][0],
                 oldest_ms=prints[0][1])

    def margin(s: Stage) -> None:
        """positionIM/positionMM is a ratio on an OPEN position (INVENTORY
        F4) - it does not exist while flat, and Bybit says so by returning a
        position row with positionIM=0/positionMM=0 (proven live, Bybit
        testnet), not by omitting the row. That is not a wallet fault or a
        second envelope bug; it is the venue correctly reporting nothing to
        compute a ratio about. `flat` already established whether anything
        is open — reused here rather than asked a second, contradictory way
        — so this stage only calls the broker at all once a position exists.
        """
        from carry_engine import MIN_MARGIN_MULTIPLE
        ok, evidence, why = margin_stage_evidence(
            broker=broker, symbol=PERP_SYMBOL,
            venue_perp_qty=state["venue_perp_qty"],
            min_margin_multiple=MIN_MARGIN_MULTIPLE)
        if ok:
            s.passed(**evidence)
        else:
            s.failed(why, **evidence)

    def cap_check(s: Stage) -> None:
        if d.notional > cap:
            s.failed(
                f"${d.notional:,.2f} is above the ${cap:,.2f} cap. The cap is "
                "raised by a signed human memo after testnet evidence, never "
                "by a flag on a drill.",
                asked_usd=d.notional, cap_usd=cap)
            return
        s.passed(notional_usd=d.notional, cap_usd=cap)

    for name, fn in (("reachability", reachability), ("flat", flat),
                     ("venue_rules", venue_rules), ("fee_tier", fee_tier),
                     ("inventory", inventory), ("warmup", warmup),
                     ("margin", margin),
                     ("cap", cap_check)):
        if not d.run_stage(name, fn):
            return _report(d, state, out, cap, verdict="FAILED")

    if not arm:
        return _report(d, state, out, cap, verdict="PREFLIGHT_ONLY")

    # ---- from here orders are sent, through the engine and nothing else ----
    import ledger as L
    from carry_engine import BookState, CarryEngine
    from carry_risk import CarryRisk

    journal = L.Journal()
    booked = L.LedgerBroker(broker, journal, ms=lambda: int(time.time() * 1000))

    class _Store:
        def is_kill_switch_engaged(self):
            return (False, "")

        def trip_kill_switch(self, reason):
            state["killed"] = reason

    engine = (engine_factory or CarryEngine)(
        broker=booked, kill_switch=_Store().trip_kill_switch,
        max_notional_usd=d.notional, borrow_apr=0.0,
        execution_mode="overlay", execution_style="taker",
        persist=lambda s: None,
        # The one check that protects the held short from the margin call
        # (F4): ON here exactly as in the production book, and what it reads
        # each held tick is kept so the hold's evidence can show it.
        require_liquidation_check=True,
        risk_observer=lambda obs: state.setdefault("risk_obs", []).append(obs))
    engine.pair_risk = CarryRisk(store=_Store(), max_notional_usd=d.notional)
    engine.warm_funding_history(state["prints"])
    state["engine"] = engine
    state["journal"] = journal
    state["booked"] = booked
    state["broker"] = unwrap_broker(broker)
    state["capture_requested"] = bool(capture_final)

    def open_pair(s: Stage) -> None:
        from market_snapshot import take_snapshot
        view = take_snapshot(broker, perp_symbol=PERP_SYMBOL,
                             spot_symbol=SPOT_SYMBOL)
        view.assert_fresh()
        engine.snapshot = view
        decision = engine.on_candle(
            mark=view.perp_mark, funding_bps=view.funding_print_bps,
            spot=view.spot_mark, timestamp_ms=int(time.time() * 1000),
            funding_print_ms=view.funding_print_ms)
        d.orders_sent = len(journal.entries)
        if decision.action != "opened":
            s.failed(f"the book did not open: {decision.reason}",
                     action=decision.action, reason=decision.reason,
                     detail=dict(decision.detail or {}))
            return
        pos = engine.position
        state["entry_qty"] = pos.perp.filled_qty
        state["entry_price"] = pos.perp.avg_price
        s.passed(action=decision.action, reason=decision.reason,
                 filled_qty=pos.perp.filled_qty,
                 avg_price=pos.perp.avg_price, fee=pos.perp.fee,
                 order_link_id=pos.perp.order_link_id)

    def pair(s: Stage) -> None:
        """Against the VENUE, not against what we just wrote down."""
        venue_qty = abs(float(broker.get_perp_position(PERP_SYMBOL)))
        book_qty = engine.position.perp.filled_qty if engine.position else 0.0
        drift = abs(venue_qty - book_qty)
        if drift > 1e-8:
            s.failed(
                f"the venue holds {venue_qty} and the book believes "
                f"{book_qty}. One leg exists that the other does not hedge.",
                venue_perp_qty=venue_qty, book_perp_qty=book_qty, drift=drift)
            return
        s.passed(venue_perp_qty=venue_qty, book_perp_qty=book_qty, drift=drift)

    def book_settlements(now_ms: int) -> Any:
        """Read the account's settlement rows since the open and book them."""
        opened = engine.position.opened_ms if engine.position else int(
            state.get("opened_ms") or 0)
        ev = broker.get_funding_evidence(
            PERP_SYMBOL, max(0, opened - L.ATTRIBUTION_SLACK_MS), now_ms)
        state["evidence"] = ev
        L.book_funding_payments(journal, ev.payments, ms=now_ms)
        return ev

    def hold_stage(s: Stage) -> None:
        """Hold, watched, until the settlement is booked AND has aged past the
        ledger's ownership slack (a close inside it would make the settlement
        'ambiguous' and the lifecycle unconfirmable)."""
        from market_snapshot import take_snapshot
        plan = hold
        state["opened_ms"] = engine.position.opened_ms
        deadline = time.time() + plan.max_hold_s
        polls = 0
        blind = 0
        while True:
            polls += 1
            now_ms = int(time.time() * 1000)
            try:
                view = take_snapshot(broker, perp_symbol=PERP_SYMBOL,
                                     spot_symbol=SPOT_SYMBOL)
                view.assert_fresh()
                engine.snapshot = view
                blind = 0
                decision = engine.on_candle(
                    mark=view.perp_mark, funding_bps=view.funding_print_bps,
                    spot=view.spot_mark, timestamp_ms=now_ms,
                    funding_print_ms=view.funding_print_ms)
            except Exception as exc:  # noqa: BLE001
                # What production does: a cycle without a usable market view
                # still WATCHES the venue position; a few in a row are
                # tolerated, a run of them is not.
                blind += 1
                s.evidence["last_view_error"] = f"{type(exc).__name__}: {exc}"
                decision = engine.check_venue_position_without_a_view()
                if blind >= HOLD_MAX_BLIND_POLLS or (
                        decision is not None
                        and engine.state is BookState.HALTED):
                    s.failed(f"{blind} consecutive polls without a usable "
                             "market view", polls=polls)
                    return
                if time.time() >= deadline:
                    s.failed("the maximum hold ended without a usable view",
                             polls=polls)
                    return
                plan.sleep(plan.poll_s)
                continue
            d.orders_sent = len(journal.entries)
            if engine.position is None or engine.state is BookState.HALTED:
                state["engine_ended"] = decision.action
                s.failed(f"the book left the hedge on its own "
                         f"({decision.action}: {decision.reason}) before the "
                         "settlement was observed", polls=polls,
                         action=decision.action, reason=decision.reason)
                return
            try:
                book_settlements(now_ms)
                s.evidence.pop("last_read_error", None)
            except Exception as exc:  # noqa: BLE001
                state["evidence"] = None
                s.evidence["last_read_error"] = f"{type(exc).__name__}: {exc}"
            rows = venue_settlement_rows(journal)
            if rows >= plan.min_settlements:
                aged = max(int(e.fee_raw.get("venue_ms", 0))
                           for e in journal.entries
                           if e.kind == "funding" and e.basis == "venue"
                           ) + L.ATTRIBUTION_SLACK_MS + HOLD_AGING_MARGIN_MS
                if now_ms >= aged:
                    obs = state.get("risk_obs", [])
                    dists = [o["distance_pct"] for o in obs
                             if o.get("distance_pct") is not None]
                    s.passed(polls=polls, settlements=rows,
                             held_s=(now_ms - state["opened_ms"]) / 1000.0,
                             liquidation_reads=len(obs),
                             min_liquidation_distance_pct=(
                                 min(dists) if dists else None))
                    return
            elif time.time() >= deadline:
                s.failed("no settlement row was booked within the maximum "
                         "hold; the pair is unwound and this is not a result",
                         polls=polls, settlements=rows,
                         max_hold_s=plan.max_hold_s)
                return
            plan.sleep(plan.poll_s)

    def unwind(s: Stage) -> None:
        if engine.position is None and engine.state is BookState.FLAT:
            s.passed(action="already_flat",
                     reason=state.get("engine_ended", ""))
            return
        decision = engine._unwind(state["perp"], "DRILL_COMPLETE")
        d.orders_sent = len(journal.entries)
        if decision.action != "unwound" or engine.state is BookState.HALTED:
            d.still_open = True
            s.failed(
                f"the hedge did not close ({decision.reason}). THE BOOK IS "
                "NOT FLAT. A human must close it by hand and clear the kill "
                "switch; nothing automated will try again.",
                action=decision.action, reason=decision.reason)
            return
        s.passed(action=decision.action, reason=decision.reason,
                 detail=dict(decision.detail or {}),
                 **_exit_fields(booked.fills))

    def final_reconcile(s: Stage) -> None:
        verdict, evidence, why = venue_flatness(
            broker, inventory_before=state.get("inventory_before"))
        if verdict != "FLAT":
            # NOT_FLAT and UNVERIFIED both leave a human in charge.
            d.still_open = True
            s.failed(f"{why} after the unwind", verdict=verdict, **evidence)
            return
        s.passed(verdict="FLAT", **evidence)

    def funding_final(s: Stage) -> None:
        """After the close: re-read the log until the evidence is complete and
        the lifecycle reconciled (or the wait runs out). The status is decided
        by `funding_status` from what the journal holds, not from hope."""
        import funding_reconcile as R
        plan = hold
        start = max(0, int(state["opened_ms"]) - L.ATTRIBUTION_SLACK_MS)
        end = time.time() + plan.settle_wait_s
        ev = None
        while True:
            now_ms = int(time.time() * 1000)
            try:
                ev = book_settlements(now_ms)
                try:
                    history = broker.get_funding_history_range(
                        PERP_SYMBOL, start, now_ms)
                except Exception:  # noqa: BLE001
                    history = None
                R.reconcile_funding(journal, history=history, evidence=ev,
                                    now_ms=now_ms)
                st = journal.statement(now_ms=now_ms)
                if ev.complete and st["funding_reconciled"]:
                    break
            except Exception as exc:  # noqa: BLE001
                s.evidence["last_read_error"] = f"{type(exc).__name__}: {exc}"
            if time.time() >= end:
                break
            plan.sleep(plan.poll_s)
        st = journal.statement(now_ms=int(time.time() * 1000))
        rows = venue_settlement_rows(journal)
        status = funding_status(st, rows, bool(ev is not None and ev.complete),
                                plan.min_settlements)
        state["funding_status"] = status
        evidence = {"funding_status": status, "settlements": rows,
                    "funding_venue_usd": st["funding_venue_usd"],
                    "funding_confirmed": st["funding_confirmed"],
                    "evidence_complete": bool(ev is not None and ev.complete)}
        if status != CONFIRMED:
            s.failed(f"funding is {status}: the cycle did not produce "
                     "confirmed, settlement-booked funding", **evidence)
            return
        s.passed(**evidence)

    def statement(s: Stage) -> None:
        st = journal.statement()
        s.passed(**{k: v for k, v in st.items() if not k.startswith("_")})

    sequence = [("open", open_pair), ("pair", pair)]
    if hold is not None:
        sequence.append(("hold", hold_stage))
    sequence += [("unwind", unwind), ("final_reconcile", final_reconcile)]
    if hold is not None:
        sequence.append(("funding_final", funding_final))
    sequence.append(("statement", statement))
    def recover(name: str) -> None:
        if name != "final_reconcile":
            check = d.stage("venue_check_after_failure")
            verdict, evidence, why = venue_flatness(
                broker, inventory_before=state.get("inventory_before"))
            if verdict == "FLAT":
                check.passed(verdict=verdict, **evidence)
            else:
                d.still_open = True
                check.failed(why, verdict=verdict, **evidence)
                # A stage failed BEFORE the planned unwind (the pair would not
                # reconcile, the engine raised after sending): the hedge the
                # engine can close is still open. Try the engine's own
                # reduce-only unwind once, then say what the venue holds.
                if (name in ("open", "pair", "hold") and engine.position is not None
                        and state.get("perp") is not None):
                    abort = d.stage("abort_unwind")
                    try:
                        decision = engine._unwind(state["perp"], "DRILL_ABORT")
                        d.orders_sent = len(journal.entries)
                        verdict2, evidence2, why2 = venue_flatness(
                            broker,
                            inventory_before=state.get("inventory_before"))
                        # the cost of the abort, which the stage used to omit
                        exit_evidence = _exit_fields(booked.fills)
                        if verdict2 == "FLAT":
                            d.still_open = False
                            abort.passed(action=decision.action,
                                         verdict=verdict2, **evidence2,
                                         **exit_evidence)
                        else:
                            abort.failed(why2, action=decision.action,
                                         verdict=verdict2, **evidence2,
                                         **exit_evidence)
                    except Exception as exc:  # noqa: BLE001
                        abort.failed(f"{type(exc).__name__}: {exc}")

    def final_state_capture() -> None:
        """Read the final state of every order sent. Never changes the verdict:
        the book's verdict is about the book; whether the venue's own rows were
        captured is reported (and exit 6) separately."""
        if not capture_final or not getattr(unwrap_broker(broker),
                                            "sent_order_links", None):
            return
        s = d.stage("venue_final_state")
        try:
            result = capture_final_venue_state(broker)
        except Exception as exc:                               # noqa: BLE001
            result = {"complete": False, "orders": 0, "reads": 0,
                      "failures": [f"{type(exc).__name__}: {exc}"]}
        state["evidence_capture"] = result
        if result["complete"]:
            s.passed(**result)
        else:
            s.failed("the venue's final order/execution rows were not all "
                     "captured; the evidence file cannot reconcile this run",
                     **result)

    def finish(verdict: str) -> Dict[str, Any]:
        """Write the report BEFORE the capture's network reads (marked pending),
        so a hung read, a kill or a signal leaves what is known on disk; then
        capture, then write the final report."""
        if capture_final and getattr(unwrap_broker(broker),
                                     "sent_order_links", None):
            state["evidence_capture"] = {
                "complete": False, "orders": 0, "reads": 0,
                "failures": ["the final-state capture had not finished when "
                             "this report was written"]}
        _finish(d, state, out, cap, verdict, cash_ledger, venue)
        final_state_capture()
        return _finish(d, state, out, cap, verdict, cash_ledger, venue)

    # The tail (capture, final report) is inside the guarded region too: a
    # signal there still produces a report and the clean exit, never a false
    # "nothing was sent".
    try:
        for name, fn in sequence:
            if not d.run_stage(name, fn):
                recover(name)
                return finish("FAILED")
        return finish("PASSED")
    except BaseException as exc:
        # Ctrl-C, SIGTERM, anything: the pair may be open. Close it through
        # the engine, say what the venue holds, write the report, and only then
        # let the interruption continue. A KeyboardInterrupt carries the report
        # (`drill_report`) so the CLI can print it and exit 130 cleanly.
        d.failed_stage = d.failed_stage or "interrupted"
        d.interrupted = isinstance(exc, KeyboardInterrupt)
        d.stage("interrupted").failed(f"{type(exc).__name__}: {exc}")
        written = None
        try:
            recover("hold")
        finally:
            try:
                written = finish("FAILED")
            except BaseException:                              # noqa: BLE001
                # the capture itself was interrupted or failed: the report
                # without it (its capture still marked unfinished) stands
                written = _finish(d, state, out, cap, "FAILED", cash_ledger, venue)
            try:
                exc.drill_report = written
            except Exception:                                  # noqa: BLE001
                pass
        raise


def _finish(d: Drill, state: Dict[str, Any], out: str, cap: float,
            verdict: str, cash_ledger: str, venue: str) -> Dict[str, Any]:
    """Report, and (hold sessions) write the cash ledger when the book ended
    flat after real fills -- whatever the funding status, which it states."""
    report = _report(d, state, out, cap, verdict=verdict)
    journal = state.get("journal")
    status = state.get("funding_status", UNCONFIRMED)
    report["funding_status"] = status
    report["cash_ledger"] = ""
    if (cash_ledger and journal is not None and not d.still_open
            and "funding_status" in state
            and any(e.kind in ("perp_open", "perp_close")
                    for e in journal.entries)):
        try:
            report["cash_ledger"] = write_carry_cash_ledger(
                cash_ledger, journal=journal, state=state, status=status,
                flat=True, venue=venue)
        except Exception as exc:  # noqa: BLE001
            report["cash_ledger_error"] = f"{type(exc).__name__}: {exc}"
    if out:
        with open(out, "w", encoding="utf-8") as handle:
            json.dump(report, handle, indent=2, default=str)
    return report


def _report(d: Drill, state: Dict[str, Any], out: str, cap: float,
            *, verdict: str) -> Dict[str, Any]:
    journal = state.get("journal")
    evidence = {s.name: s.evidence for s in d.stages}
    if d.still_open:
        action = ("A HUMAN MUST ACT NOW: the book is not flat or could not be "
                  "verified flat. Inspect the venue's perp position, open "
                  "orders and spot inventory, close what is open by hand, then "
                  "clear the kill switch. Nothing automated will retry.")
    elif verdict == "FAILED":
        action = (f"the drill stopped at {d.failed_stage!r}; nothing is open. "
                  "Fix what the stage reports and run it again.")
    elif verdict == "PREFLIGHT_ONLY":
        action = ("no order was sent. Re-run with --arm when the wallet and "
                  "the key are ready.")
    else:
        action = ("PASSED. The transcript is the Phase D evidence. Read "
                  "`not_proven` before treating it as more than it is.")
    venue_note = {
        "mainnet": "MAINNET — real money moved.",
        "testnet": "testnet — a separate exchange with its own order book; "
                   "its prices and funding are NOT the real ones.",
        "demo": "demo trading — REAL mainnet market data with SIMULATED "
                "matching. The marks, basis and funding prints are real; the "
                "fills are not, and no real money moved.",
        "unknown": "UNKNOWN venue: this transcript does not record which "
                   "exchange it ran against and must not be read as evidence "
                   "about any particular one.",
    }.get(d.venue, f"unrecognised venue {d.venue!r}")
    report = {
        "verdict": verdict,
        "venue": d.venue,
        "venue_note": venue_note,
        "armed": bool(d.arm),
        "failed_stage": d.failed_stage,
        "still_open": d.still_open,
        "next_action": action,
        "notional_usd": d.notional,
        "cap_usd": cap,
        # the larger of what was booked and what went on the wire: a signal
        # between the venue accepting an order and the book recording it must
        # not report zero orders
        "orders_sent": max(d.orders_sent, len(
            getattr(state.get("broker"), "sent_order_links", None) or [])),
        "ledger_entries": len(journal.entries) if journal else 0,
        "interrupted": d.interrupted,
        "stages": [s.as_dict() for s in d.stages],
        "evidence": evidence,
        "not_proven": list(NOT_PROVEN),
        "finished_ms": int(time.time() * 1000),
    }
    booked = state.get("booked")
    if booked is not None and booked.fills:
        report["round_trip"] = round_trip_summary(booked.fills)
        report["claims"] = drill_claims(booked.fills, journal, venue=d.venue)
    if state.get("capture_requested") and booked is not None and booked.fills:
        report["evidence_capture"] = {
            "requested": True,
            **(state.get("evidence_capture")
               or {"complete": False, "orders": 0, "reads": 0,
                   "failures": ["the final-state capture did not run"]})}
    if out:
        os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
        with open(out, "w", encoding="utf-8") as handle:
            json.dump(report, handle, indent=2, default=str)
    return report


def _scratch_state_db() -> str:
    """A throwaway `StateStore` path. ALWAYS throwaway — never the book's.

    Two reasons, and the second is the important one.

    In a checkout the config default is `state/trading_state.db`, which is the
    COMMITTED FIXTURE: the preflight modified a tracked file and
    `scripts/verify.sh` then refused a receipt ("FIXTURE DB MUTATED"). A tool
    that reads must not write.

    On Fly that path names the RUNNING BOOK's database, and the drill is a
    separate process. Opening it would be two writers on one SQLite file —
    against the repo's one-writer rule, and against a ledger that is supposed
    to explain the entire equity change. The drill needs none of it: it builds
    its own journal and reads its position from the VENUE.

    Nothing here reads the environment. `config.load()` is the only
    environment reader, and the path is handed to `StateStore` explicitly.
    """
    import tempfile
    folder = tempfile.mkdtemp(prefix="carry-drill-")
    return os.path.join(folder, "drill_state.db")


def _load_bot(evidence_path: str = ""):
    """Import and build the stack. Separated so a missing dependency is a
    sentence rather than a traceback four imports deep (0046) — the same
    lesson as INVENTORY D16, which is why `session_tail` reports UNREADABLE
    instead of NO.

    `evidence_path` (testnet only): every REST request the book sends is also
    captured, redacted and hash-chained there by the same
    `EvidenceCapturingTransport` the conformance run uses, so the drill's
    orders, fills and funding settlements can be validated with
    `tools/evidence_reconcile.py` instead of resting on the drill's own report.
    The book still receives an ordinary `BybitClient`; only its transport is
    wrapped."""
    import persistence

    import main as _main

    # The store is CONSTRUCTED and passed, not selected by mutating the
    # environment: config.load() is the only environment reader in this repo,
    # and a tool that writes to os.environ to steer another module is a second
    # one wearing a disguise.
    store = persistence.StateStore(_scratch_state_db())
    if evidence_path:
        import bybit_connection as _bc
        import config as _config
        import venue_evidence as _ve
        cfg = _config.get_config_object()
        venue = str(getattr(cfg, "BYBIT_VENUE", "") or "").lower()
        if venue != "testnet":
            raise RuntimeError(
                f"evidence capture is for the testnet venue only (BYBIT_VENUE="
                f"{venue!r}); refusing to label a transcript with another venue")
        transport = _ve.EvidenceCapturingTransport(
            _bc.RequestsTransport(), evidence_path=evidence_path,
            venue="bybit", environment="testnet")
        client = _bc.BybitClient(config=cfg, store=store, transport=transport)
        return _main.build_bot(store=store, config=cfg, client=client)

    # NOT attach_strategy=False. That flag reads as "attach no signal source",
    # and it does — but the CARRY book is gated behind the same flag, so
    # passing False returned a bot with `carry is None` and the drill reported
    # "BOOK_MODE is not carry" against a config that plainly said carry. It
    # would have failed the same way on Fly.
    #
    # None is what `main()` passes: the config decides, and for BOOK_MODE=carry
    # build_bot attaches the book and returns before the directional voter is
    # ever constructed.
    return _main.build_bot(store=store)


def book_kill_switch(db_path: str) -> "tuple[bool, str]":
    """Is the RUNNING book's persisted kill switch engaged? Read-only.

    The drill's own engine uses a scratch database, so without this an engaged
    switch on the real book would not stop an armed drill. A missing database
    is "no book state"; one that exists and cannot be read is treated as
    engaged: arming on an unreadable safety control is the wrong default.
    """
    import sqlite3
    if not db_path or not os.path.isfile(db_path):
        return False, "no state db"
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            row = con.execute(
                "SELECT engaged, reason FROM kill_switch LIMIT 1").fetchone()
        finally:
            con.close()
    except Exception as exc:  # noqa: BLE001
        return True, f"unreadable ({exc})"
    if not row:
        return False, "no row"
    return bool(row[0]), str(row[1] or "")


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--arm", action="store_true",
                    help="send orders. Without it nothing is sent.")
    ap.add_argument("--notional", type=float, default=None)
    ap.add_argument("--out", default="")
    ap.add_argument("--hold-for-settlement", action="store_true",
                    help="hold the pair until the venue has paid a funding "
                         "settlement, then close and reconcile (needs --arm)")
    ap.add_argument("--min-settlements", type=int, default=1)
    ap.add_argument("--max-hold-hours", type=float, default=9.0)
    ap.add_argument("--poll-seconds", type=float, default=60.0)
    ap.add_argument("--settle-wait-hours", type=float, default=6.25,
                    help="after the close, keep re-reading the log this long "
                         "(the ledger judges a lifecycle 6 h after its end)")
    ap.add_argument("--evidence-path", default="",
                    help="testnet only: capture every REST request/response "
                         "(redacted, hash-chained) here, for "
                         "tools/evidence_reconcile.py")
    ap.add_argument("--cash-ledger", default="",
                    help="write cash_ledger/2 here when the book ends flat")
    args = ap.parse_args(argv)
    if args.hold_for_settlement and not args.arm:
        print("REFUSED: --hold-for-settlement sends orders and needs --arm.",
              file=sys.stderr)
        return 3
    if args.hold_for_settlement:
        import math
        problems = []
        if not args.out or not args.cash_ledger:
            problems.append("--out and --cash-ledger are both required: a run "
                            "that cannot save its evidence cannot succeed")
        if args.min_settlements < 1:
            problems.append("--min-settlements must be at least 1")
        if not (math.isfinite(args.poll_seconds)
                and 1.0 <= args.poll_seconds <= 3600.0):
            problems.append("--poll-seconds must be between 1 and 3600")
        if not (math.isfinite(args.max_hold_hours)
                and 0.0 < args.max_hold_hours <= 48.0):
            problems.append("--max-hold-hours must be in (0, 48]")
        if not (math.isfinite(args.settle_wait_hours)
                and 0.0 <= args.settle_wait_hours <= 48.0):
            problems.append("--settle-wait-hours must be in [0, 48]")
        if problems:
            print("REFUSED: " + "; ".join(problems) + ".", file=sys.stderr)
            return 3

    try:
        bot = _load_bot(args.evidence_path) if args.evidence_path else _load_bot()
    except Exception as exc:                                   # noqa: BLE001
        print(f"the trading stack could not be imported: "
              f"{type(exc).__name__}: {exc}\n\n"
              "This is almost always the wrong interpreter. The drill imports "
              "the whole book,\nwhich needs numpy and requests. Run it with "
              "the virtualenv the suite uses:\n\n"
              "    . .venv/bin/activate && python3 tools/drill.py\n"
              "    (or: ../.venv/bin/python tools/drill.py)\n",
              file=sys.stderr)
        return 2
    if bot.carry is None:
        print(
            "BOOK_MODE is not carry, so there is no book to drill.\n\n"
            "The drill needs the same three keys the running book needs, and\n"
            "none of them has a default — the two modes place different\n"
            "orders and the financing decides whether the carry clears its\n"
            "cost of capital. On Fly these come from fly.toml; here, export\n"
            "them:\n\n"
            "    export BOOK_MODE=carry\n"
            "    export CARRY_BORROW_APR=0.0        # 0.0 = an overlay on BTC\n"
            "                                       # already owned\n"
            "    export CARRY_EXECUTION_MODE=overlay\n\n"
            "    . ../.venv/bin/activate && python3 tools/drill.py\n",
            file=sys.stderr)
        return 2

    if args.hold_for_settlement or args.arm:
        why_not = session_refusal(bot.cfg, getattr(
            unwrap_broker(bot.carry.broker), "client", None))
        if why_not:
            print(f"REFUSED: {why_not}", file=sys.stderr)
            return 3

    if args.arm:
        engaged, why = book_kill_switch(
            str(getattr(getattr(bot, "cfg", None), "STATE_DB_PATH", "") or ""))
        if engaged:
            print(f"REFUSED: the running book's kill switch is engaged "
                  f"({why}). The drill runs on a scratch database and would "
                  "not see it; a human clears the switch before anything is "
                  "armed.", file=sys.stderr)
            return 2

    if args.arm:
        # an interrupt closes the pair and writes the report; a second one
        # while that happens is ignored (it would leave the short open)
        InterruptGuard().install()
    plan = (HoldPlan(min_settlements=args.min_settlements,
                     max_hold_s=args.max_hold_hours * 3600.0,
                     poll_s=args.poll_seconds,
                     settle_wait_s=args.settle_wait_hours * 3600.0)
            if args.hold_for_settlement else None)
    try:
        report = run_drill(broker=bot.carry.broker, notional=args.notional,
                           arm=args.arm, out=args.out, hold=plan,
                           cash_ledger=args.cash_ledger,
                           capture_final=bool(args.evidence_path),
                           venue=str(getattr(bot.cfg, "BYBIT_VENUE", "unknown")
                                     if hasattr(bot, "cfg") else "unknown"))
    except KeyboardInterrupt as exc:
        report = getattr(exc, "drill_report", None)
        if report is None:
            print(f"interrupted ({exc}) before any order could have been "
                  "sent (during setup); no report was produced.",
                  file=sys.stderr)
            return 130
        print(f"INTERRUPTED ({exc}): the pair was closed through the engine "
              "and the report written; the venue state is below.",
              file=sys.stderr)
    _print_report(report, args)
    return drill_exit_code(report, hold=plan is not None)


class InterruptGuard:
    """The first SIGINT/SIGTERM/SIGHUP becomes a KeyboardInterrupt (so the
    drill's own recovery runs); every later one is ignored, because the
    recovery is the engine's reduce-only unwind and interrupting it would leave
    the short open."""

    def __init__(self) -> None:
        self.count = 0

    def handler(self, signum: int, frame: Any) -> None:       # noqa: ARG002
        self.count += 1
        if self.count == 1:
            raise KeyboardInterrupt(f"signal {signum}")
        # This runs in the middle of the engine's reduce-only close: nothing
        # here may raise (a dead stderr after SIGHUP would abort the unwind).
        try:
            sys.stderr.write(f"signal {signum} ignored: the drill is closing "
                             "the pair and writing its report\n")
        except BaseException:                                  # noqa: BLE001
            pass

    def install(self) -> None:
        import signal
        for name in ("SIGINT", "SIGTERM", "SIGHUP"):
            sig = getattr(signal, name, None)
            if sig is not None:
                signal.signal(sig, self.handler)


def _print_report(report: Dict[str, Any], args: Any) -> None:
    print("=" * 74)
    print(f"PHASE D DRILL — {report['verdict']}"
          f"   ({'ARMED' if report['armed'] else 'preflight only'})")
    print("=" * 74)
    print(f"  venue: {report['venue']} — {report['venue_note']}")
    print()
    for stage in report["stages"]:
        mark = "ok  " if stage["ok"] else "STOP"
        print(f"  [{mark}] {stage['stage']}")
        for key, value in stage["evidence"].items():
            print(f"           {key}: {value}")
        if stage["why"]:
            print(f"           -> {stage['why']}")
    print(f"\n  orders sent: {report['orders_sent']}   "
          f"ledger entries: {report['ledger_entries']}")
    print(f"\n  {report['next_action']}")
    print("\n  WHAT A PASS STILL DOES NOT ESTABLISH")
    for line in report["not_proven"]:
        print(f"    - {line}")
    trip = report.get("round_trip")
    if trip:
        print("\n  ROUND TRIP (perp leg, from the booked fills)")
        for key in ("entry_qty", "entry_price", "exit_qty", "exit_price",
                    "closed", "fees_confirmed", "fees_usdt", "perp_gross_usdt",
                    "perp_net_before_funding_usdt", "fee_bps_of_entry_notional"):
            print(f"    {key}: {trip.get(key)}")
    capture = report.get("evidence_capture")
    if capture:
        print(f"\n  venue final-state capture: complete={capture['complete']} "
              f"orders={capture['orders']} reads={capture['reads']}")
        for line in capture.get("failures", []):
            print(f"    - {line}")
    if args.out:
        print(f"\n  transcript: {args.out}")


def drill_exit_code(report: Dict[str, Any], *, hold: bool) -> int:
    """0 only for a drill that PASSED; with a hold: flat, a cash ledger on disk
    and funding CONFIRMED_FROM_SETTLEMENTS. Zero fills is 2, flat but funding
    unconfirmed is 4, anything else that failed is 1. An interrupt that left
    the book flat is 130; one that did not is 1. A result that would be 0 but
    whose requested venue capture is incomplete is 6."""
    if report.get("interrupted"):
        return 1 if report.get("still_open") else 130
    code = _base_exit_code(report, hold=hold)
    capture = report.get("evidence_capture")
    if code == 0 and capture and capture.get("requested") \
            and not capture.get("complete"):
        return 6
    return code


def _base_exit_code(report: Dict[str, Any], *, hold: bool) -> int:
    if report["verdict"] == "PREFLIGHT_ONLY":
        return 0
    if not hold:
        return 0 if report["verdict"] == "PASSED" else 1
    if report.get("still_open"):
        return 1
    if report["verdict"] == "FAILED" and report.get("orders_sent", 0) == 0:
        return 2
    if report["verdict"] != "PASSED":
        if (report.get("funding_status") == PENDING and report.get("cash_ledger")
                and os.path.isfile(report["cash_ledger"])):
            return 5
        return 4 if report.get("funding_status") in (
            NO_SETTLEMENT, UNCONFIRMED) and report.get("orders_sent", 0) else 1
    if (report.get("funding_status") == CONFIRMED
            and report.get("cash_ledger")
            and os.path.isfile(report["cash_ledger"])):
        return 0
    return 4


if __name__ == "__main__":
    raise SystemExit(main())
