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
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
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


def run_drill(*, broker: Any, notional: Optional[float] = None,
              arm: bool = False, out: str = "",
              engine_factory: Any = None) -> Dict[str, Any]:
    """The whole sequence. Returns the transcript whatever happens."""
    import shadow

    cap = float(shadow.SHADOW_MAX_NOTIONAL_USD)
    asked = cap if notional is None else float(notional)
    d = Drill(broker=broker, notional=asked, arm=arm)
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
        from carry_engine import MIN_MARGIN_MULTIPLE
        multiple = float(broker.get_margin_multiple(PERP_SYMBOL))
        if multiple < MIN_MARGIN_MULTIPLE:
            s.failed(f"margin multiple {multiple} is below the "
                     f"{MIN_MARGIN_MULTIPLE} floor", margin_multiple=multiple)
            return
        s.passed(margin_multiple=multiple, floor=MIN_MARGIN_MULTIPLE,
                 note="positionIM/positionMM — a risk-tier ratio, NOT "
                      "liquidation headroom (INVENTORY F4)")

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

    def unwind(s: Stage) -> None:
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
        venue_qty = abs(float(broker.get_perp_position(PERP_SYMBOL)))
        if venue_qty > 1e-8:
            d.still_open = True
            s.failed(f"the venue still holds {venue_qty} after the unwind",
                     venue_perp_qty=venue_qty, verdict="NOT_FLAT")
            return
        s.passed(venue_perp_qty=venue_qty, verdict="FLAT")

    def statement(s: Stage) -> None:
        st = journal.statement()
        s.passed(**{k: v for k, v in st.items() if not k.startswith("_")})

    for name, fn in (("open", open_pair), ("pair", pair), ("unwind", unwind),
                     ("final_reconcile", final_reconcile),
                     ("statement", statement)):
        if not d.run_stage(name, fn):
            return _report(d, state, out, cap, verdict="FAILED")

    return _report(d, state, out, cap, verdict="PASSED")


def _report(d: Drill, state: Dict[str, Any], out: str, cap: float,
            *, verdict: str) -> Dict[str, Any]:
    journal = state.get("journal")
    evidence = {s.name: s.evidence for s in d.stages}
    if d.still_open:
        action = ("A HUMAN MUST ACT NOW: the book is not flat. Close the perp "
                  "position by hand at the venue, then clear the kill switch. "
                  "Nothing automated will retry.")
    elif verdict == "FAILED":
        action = (f"the drill stopped at {d.failed_stage!r}; nothing is open. "
                  "Fix what the stage reports and run it again.")
    elif verdict == "PREFLIGHT_ONLY":
        action = ("no order was sent. Re-run with --arm when the wallet and "
                  "the key are ready.")
    else:
        action = ("PASSED. The transcript is the Phase D evidence. Read "
                  "`not_proven` before treating it as more than it is.")
    report = {
        "verdict": verdict,
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


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--arm", action="store_true",
                    help="send orders. Without it nothing is sent.")
    ap.add_argument("--notional", type=float, default=None)
    ap.add_argument("--out", default="")
    args = ap.parse_args(argv)

    import main as _main
    bot = _main.build_bot(attach_strategy=False)
    if bot.carry is None:
        print("BOOK_MODE is not carry; there is no book to drill.",
              file=sys.stderr)
        return 2

    report = run_drill(broker=bot.carry.broker, notional=args.notional,
                       arm=args.arm, out=args.out)

    print("=" * 74)
    print(f"PHASE D DRILL — {report['verdict']}"
          f"   ({'ARMED' if report['armed'] else 'preflight only'})")
    print("=" * 74)
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
    return 0 if report["verdict"] in ("PASSED", "PREFLIGHT_ONLY") else 1


if __name__ == "__main__":
    raise SystemExit(main())
