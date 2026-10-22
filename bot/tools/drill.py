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
5 = flat, rows booked, reconciliation still inside its grace, 1 = other.
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
    url = str(getattr(client, "base_url", "") or "").rstrip("/")
    if url:
        from bybit_connection import MAINNET_REST
        if url == MAINNET_REST:
            return f"the client targets the mainnet host {MAINNET_REST}"
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


def run_drill(*, broker: Any, notional: Optional[float] = None,
              arm: bool = False, out: str = "", venue: str = "unknown",
              engine_factory: Any = None, hold: Optional[HoldPlan] = None,
              cash_ledger: str = "") -> Dict[str, Any]:
    """The whole sequence. Returns the transcript whatever happens."""
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
        persist=lambda s: None)
    engine.pair_risk = CarryRisk(store=_Store(), max_notional_usd=d.notional)
    engine.warm_funding_history(state["prints"])
    state["engine"] = engine
    state["journal"] = journal

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
        from market_snapshot import take_snapshot
        plan = hold
        state["opened_ms"] = engine.position.opened_ms
        deadline = time.time() + plan.max_hold_s
        polls = 0
        while True:
            polls += 1
            now_ms = int(time.time() * 1000)
            view = take_snapshot(broker, perp_symbol=PERP_SYMBOL,
                                 spot_symbol=SPOT_SYMBOL)
            view.assert_fresh()
            engine.snapshot = view
            decision = engine.on_candle(
                mark=view.perp_mark, funding_bps=view.funding_print_bps,
                spot=view.spot_mark, timestamp_ms=now_ms,
                funding_print_ms=view.funding_print_ms)
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
            except Exception as exc:  # noqa: BLE001
                state["evidence"] = None
                s.evidence["last_read_error"] = f"{type(exc).__name__}: {exc}"
            rows = venue_settlement_rows(journal)
            if rows >= plan.min_settlements:
                s.passed(polls=polls, settlements=rows,
                         held_s=(now_ms - state["opened_ms"]) / 1000.0)
                return
            if time.time() >= deadline:
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
                 detail=dict(decision.detail or {}))

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
    for name, fn in sequence:
        if not d.run_stage(name, fn):
            # Orders may have left this process whatever the failed stage was
            # (an ambiguous submission, a halted engine, a pair that would not
            # reconcile). Say what the venue holds NOW instead of letting the
            # report claim "nothing is open". A separate stage, so the stage
            # that actually failed stays the one reported.
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
                            if verdict2 == "FLAT":
                                d.still_open = False
                                abort.passed(action=decision.action,
                                             verdict=verdict2, **evidence2)
                            else:
                                abort.failed(why2, action=decision.action,
                                             verdict=verdict2, **evidence2)
                        except Exception as exc:  # noqa: BLE001
                            abort.failed(f"{type(exc).__name__}: {exc}")
            return _finish(d, state, out, cap, "FAILED", cash_ledger, venue)

    return _finish(d, state, out, cap, "PASSED", cash_ledger, venue)


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
        "orders_sent": d.orders_sent,
        "ledger_entries": len(journal.entries) if journal else 0,
        "stages": [s.as_dict() for s in d.stages],
        "evidence": evidence,
        "not_proven": list(NOT_PROVEN),
        "finished_ms": int(time.time() * 1000),
    }
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


def _load_bot():
    """Import and build the stack. Separated so a missing dependency is a
    sentence rather than a traceback four imports deep (0046) — the same
    lesson as INVENTORY D16, which is why `session_tail` reports UNREADABLE
    instead of NO."""
    import persistence

    import main as _main

    # The store is CONSTRUCTED and passed, not selected by mutating the
    # environment: config.load() is the only environment reader in this repo,
    # and a tool that writes to os.environ to steer another module is a second
    # one wearing a disguise.
    store = persistence.StateStore(_scratch_state_db())

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
    ap.add_argument("--cash-ledger", default="",
                    help="write cash_ledger/2 here when the book ends flat")
    args = ap.parse_args(argv)
    if args.hold_for_settlement and not args.arm:
        print("REFUSED: --hold-for-settlement sends orders and needs --arm.",
              file=sys.stderr)
        return 3
    if args.hold_for_settlement and args.min_settlements < 1:
        print("REFUSED: --min-settlements must be at least 1.", file=sys.stderr)
        return 3

    try:
        bot = _load_bot()
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

    if args.hold_for_settlement:
        why_not = session_refusal(bot.cfg, getattr(bot.carry.broker, "client", None))
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

    plan = (HoldPlan(min_settlements=args.min_settlements,
                     max_hold_s=args.max_hold_hours * 3600.0,
                     poll_s=args.poll_seconds,
                     settle_wait_s=args.settle_wait_hours * 3600.0)
            if args.hold_for_settlement else None)
    report = run_drill(broker=bot.carry.broker, notional=args.notional,
                       arm=args.arm, out=args.out, hold=plan,
                       cash_ledger=args.cash_ledger,
                       venue=str(getattr(bot.cfg, "BYBIT_VENUE", "unknown")
                                 if hasattr(bot, "cfg") else "unknown"))

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
    if args.out:
        print(f"\n  transcript: {args.out}")
    return drill_exit_code(report, hold=plan is not None)


def drill_exit_code(report: Dict[str, Any], *, hold: bool) -> int:
    """0 only for a drill that PASSED; with a hold: flat, a cash ledger on disk
    and funding CONFIRMED_FROM_SETTLEMENTS. Zero fills is 2, flat but funding
    unconfirmed is 4, anything else that failed is 1."""
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
