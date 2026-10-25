#!/usr/bin/env python3
"""Close a stray perp SHORT on the TESTNET account, safely, from the terminal.

For the case a drill was killed before its own unwind (a restarted Codespace, a
SIGKILL) and the account still holds the short it opened. The drill cannot
close it: its engine state lived in a scratch database that is gone.

WHAT IT DOES, AND ONLY THIS
  1. Reads the venue: the perp position, and any open carry orders.
  2. Prints what it found and what it WOULD do. Without `--arm` it stops there
     and sends nothing.
  3. With `--arm` and a typed confirmation, sends ONE reduce-only market Buy of
     exactly the quantity the venue reports short, through the same
     `CarryBroker.place_market` every drill order uses (its order gate, its
     reduce-only guard on every linear Buy, its deterministic order link id).
  4. Re-reads the venue until the position is flat, and says so or says it is
     not.

WHAT IT REFUSES
  * anything but the testnet venue (BYBIT_VENUE=testnet, USE_TESTNET, not paper,
    no live acknowledgement, the client's host positively the testnet host);
  * a position that is not a plain one-way short (a long, or any hedge-mode
    slot, positionIdx other than 0): refused;
  * a short that is not exactly the quantity the operator confirmed when the
    order is about to go (it changed while waiting);
  * a venue adapter whose client (hence host) cannot be found;
  * open carry orders (a resting order could fill into the flat book);
  * a quantity it cannot read or that is not positive and finite;
  * `--arm` without the confirmation typed exactly, or without a terminal;
  * running while a drill process holds the same evidence file (use a fresh
    `--evidence-path`).
  The order is reduce-only, so the venue itself refuses any quantity that would
  open or flip a position.

    # 1. look (sends nothing)
    python3 tools/close_testnet_short.py
    # 2. close (asks you to type the confirmation)
    python3 tools/close_testnet_short.py --arm \\
        --evidence-path /workspaces/ai-agent-control/runs/close_evidence.jsonl

UNATTENDED SAFETY NET (the testnet drill workflow): `--arm --no-prompt
--max-qty 0.0015` skips the typed confirmation and the terminal check, and is
refused if the short is larger than --max-qty. Everything else above applies.

Exit codes: 0 nothing to close, or closed and verified flat; 1 sent an order but
flat is NOT verified (a human must look); 2 cannot start; 3 refused.
"""
from __future__ import annotations

import argparse
import math
import os
import sys
import time
from typing import Any, List, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

FLAT_DUST = 1e-8
#: Reads after the close, spaced, before flat is called unverified.
VERIFY_TRIES = 6
VERIFY_SLEEP_S = 1.5


def confirmation_phrase(symbol: str, qty: float) -> str:
    return f"CLOSE {qty:.8f} {symbol} TESTNET"


def read_state(broker: Any, symbol: str) -> dict:
    """The venue's own answer. Raises (refusing) rather than assuming flat."""
    qty = float(broker.get_perp_position(symbol))
    orders = list(broker.get_open_carry_orders(symbol, symbol))
    slots = broker.get_position_slots(symbol)       # no slot read, no close
    return {"short_qty": qty, "open_orders": len(orders), "slots": slots}


def plan_problem(state: dict) -> Optional[str]:
    qty = state["short_qty"]
    if not (isinstance(qty, float) and math.isfinite(qty) and qty >= 0):
        return f"the venue's position size {qty!r} is not usable"
    for slot in state.get("slots", []):
        if str(slot.get("positionIdx")) not in ("0", "None", ""):
            return (f"the position sits in positionIdx {slot.get('positionIdx')!r}"
                    " (hedge mode); this tool closes a one-way position only "
                    "and sends no positionIdx")
    if state["open_orders"]:
        return (f"{state['open_orders']} carry order(s) are open at the venue; "
                "a resting order could fill into the position being closed. "
                "Cancel them on the testnet site first")
    return None


def close_short(broker: Any, symbol: str, *, expected_qty: Optional[float] = None,
                sleep=time.sleep) -> dict:
    """Send the one reduce-only Buy for the venue's short, then verify flat.

    ``expected_qty`` is what the operator confirmed: if the venue's short is not
    exactly that when the order is about to go, nothing is sent."""
    before = read_state(broker, symbol)
    problem = plan_problem(before)
    if problem:
        return {"ok": False, "refused": problem, "before": before}
    qty = before["short_qty"]
    if expected_qty is not None and abs(qty - float(expected_qty)) > FLAT_DUST:
        return {"ok": False, "before": before, "refused": (
            f"the venue's short is {qty}, not the {expected_qty} that was "
            "confirmed; it changed while waiting. Nothing was sent")}
    if qty <= FLAT_DUST:
        return {"ok": True, "sent": False, "before": before, "after": before,
                "note": "the venue is already flat; nothing was sent"}
    result = broker.place_market(symbol=symbol, side="Buy", qty=qty,
                                 product="linear")
    if result is None:
        # a definite rejection: nothing was filled and no second order follows
        return {"ok": False, "sent": False, "before": before,
                "note": "the venue rejected the order; nothing filled. Look at "
                        "the venue and the reject reason"}
    after: dict = {}
    flat = False
    for attempt in range(VERIFY_TRIES):
        try:
            after = read_state(broker, symbol)
            flat = after["short_qty"] <= FLAT_DUST and not after["open_orders"]
        except Exception as exc:                               # noqa: BLE001
            after = {"error": f"{type(exc).__name__}: {exc}"}
            flat = False
        if flat:
            break
        if attempt + 1 < VERIFY_TRIES:
            sleep(VERIFY_SLEEP_S)
    return {"ok": flat, "sent": True, "before": before, "after": after,
            "fill": result, "flat_verified": flat}


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--symbol", default="BTCUSDT")
    ap.add_argument("--arm", action="store_true",
                    help="send the reduce-only close (asks for a typed "
                         "confirmation). Without it nothing is sent.")
    ap.add_argument("--no-prompt", action="store_true",
                    help="UNATTENDED safety net (needs --arm and --max-qty): skip "
                         "the typed confirmation and the terminal check. The "
                         "close is still one reduce-only Buy, still testnet "
                         "only, and refused if the short exceeds --max-qty.")
    ap.add_argument("--max-qty", type=float, default=0.0,
                    help="with --no-prompt: refuse to close a short larger than "
                         "this (a position this tool was not expecting)")
    ap.add_argument("--evidence-path", default="",
                    help="capture this run's REST traffic (redacted, "
                         "hash-chained) here; must not already exist")
    args = ap.parse_args(argv)

    if args.no_prompt and not (args.arm and math.isfinite(args.max_qty)
                               and args.max_qty > 0):
        print("refusing: --no-prompt needs --arm and a positive --max-qty",
              file=sys.stderr)
        return 3
    if args.evidence_path and os.path.exists(args.evidence_path):
        print("refusing: --evidence-path already exists; appending to a file a "
              "drill may still be writing would break its hash chain. Pass a "
              "new path.", file=sys.stderr)
        return 3

    import drill as D
    try:
        bot = D._load_bot(args.evidence_path)
    except Exception as exc:                                   # noqa: BLE001
        print(f"could not start the stack: {type(exc).__name__}: {exc}\n"
              "Export BOOK_MODE=carry CARRY_BORROW_APR=0.0 "
              "CARRY_EXECUTION_MODE=overlay BYBIT_VENUE=testnet and the "
              "testnet API key and secret, as for the drill.", file=sys.stderr)
        return 2
    if bot.carry is None:
        print("BOOK_MODE is not carry (export BOOK_MODE=carry "
              "CARRY_BORROW_APR=0.0 CARRY_EXECUTION_MODE=overlay)",
              file=sys.stderr)
        return 2
    # the venue adapter itself: the ledger wrapper around it books into an
    # in-memory journal nobody reads and hides the adapter's `client`
    try:
        broker = D.unwrap_broker(bot.carry.broker)
    except ValueError as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 3
    if getattr(broker, "client", None) is None:
        print("REFUSED: the venue adapter has no client, so the testnet host "
              "cannot be verified.", file=sys.stderr)
        return 3
    why_not = D.session_refusal(bot.cfg, broker.client)
    if why_not:
        print(f"REFUSED: {why_not}", file=sys.stderr)
        return 3

    try:
        state = read_state(broker, args.symbol)
    except Exception as exc:                                   # noqa: BLE001
        print(f"REFUSED: the venue could not be read as a plain one-way "
              f"short or flat ({type(exc).__name__}: {exc}). Nothing was sent.",
              file=sys.stderr)
        return 3
    print(f"venue: testnet   symbol: {args.symbol}")
    print(f"  short quantity at the venue: {state['short_qty']}")
    print(f"  open carry orders:           {state['open_orders']}")
    problem = plan_problem(state)
    if problem:
        print(f"REFUSED: {problem}", file=sys.stderr)
        return 3
    if state["short_qty"] <= FLAT_DUST:
        print("The venue is flat. Nothing to close.")
        return 0
    if args.no_prompt and state["short_qty"] > args.max_qty + FLAT_DUST:
        print(f"REFUSED: the short is {state['short_qty']}, above --max-qty "
              f"{args.max_qty}; this is not the position the safety net was "
              "written for. Look at the venue.", file=sys.stderr)
        return 3
    phrase = confirmation_phrase(args.symbol, state["short_qty"])
    print(f"\nWould send ONE reduce-only market Buy of {state['short_qty']} "
          f"{args.symbol} (testnet).")
    if not args.arm:
        print("Nothing was sent (no --arm). To close, re-run with --arm.")
        return 0
    if args.no_prompt:
        print(f"--no-prompt: closing the {state['short_qty']} short "
              f"(<= --max-qty {args.max_qty}) without a typed confirmation.")
    else:
        if not sys.stdin.isatty():
            print("REFUSED: --arm needs an interactive terminal for the typed "
                  "confirmation.", file=sys.stderr)
            return 3
        try:
            typed = input(f"Type exactly  {phrase}  and press Enter: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("REFUSED: no confirmation was given. Nothing was sent.",
                  file=sys.stderr)
            return 3
        if typed != phrase:
            print("REFUSED: confirmation did not match. Nothing was sent.",
                  file=sys.stderr)
            return 3

    D.InterruptGuard().install()
    try:
        outcome = close_short(broker, args.symbol,
                              expected_qty=state["short_qty"])
    except KeyboardInterrupt as exc:
        outcome = {"ok": False, "interrupted": str(exc)}
    except Exception as exc:                                   # noqa: BLE001
        outcome = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    # Everything below runs after an order may be on the wire: a signal or an
    # error here must end in the "look at the venue" message, never a traceback.
    try:
        try:
            outcome["final"] = read_state(broker, args.symbol)
            # the verdict is the FRESH read after everything, not what an
            # earlier step believed: an interrupt or an error that still left
            # it flat is flat
            outcome["ok"] = (outcome["final"]["short_qty"] <= FLAT_DUST
                             and not outcome["final"]["open_orders"])
        except BaseException as exc:                           # noqa: BLE001
            outcome["final"] = {"error": f"{type(exc).__name__}: {exc}"}
            outcome["ok"] = False
        if args.evidence_path and getattr(broker, "sent_order_links", None):
            outcome["evidence_capture"] = D.capture_final_venue_state(
                broker, args.symbol)
        for key, value in outcome.items():
            print(f"  {key}: {value}")
    except BaseException as exc:                               # noqa: BLE001
        print(f"  report interrupted ({type(exc).__name__}); state so far: "
              f"{outcome}")
        outcome["ok"] = False
    if outcome["ok"]:
        print("\nFLAT VERIFIED by a fresh venue read.")
        return 0
    print("\nNOT VERIFIED FLAT. Look at the venue on the testnet site and "
          "close by hand if anything is still open.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
