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
import contextlib
import json
import math
import os
import signal
import sys
import threading
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


#: Wall-clock ceiling on the OPTIONAL decision-time book read in front of the
#: emergency close, enforced by a one-shot OS timer (no thread, no leftover
#: work). The transport's own `timeout_s` is a per-socket-operation figure that
#: does not cover name resolution, so it is not treated as the bound.
BOOK_DEADLINE_S = 2.0
#: What the read is told to use for its own request timeout (inside the ceiling).
BOOK_TIMEOUT_S = 1.0


class BookReadDeadline(BaseException):
    """Raised by the timer. A BaseException so that no `except Exception` on the
    way (the transport's, `_read_touch`'s) can swallow it and carry on to the
    next, unbounded, step."""


@contextlib.contextmanager
def hard_deadline(seconds: float):
    """Arm a one-shot SIGALRM for `seconds`; yield True if armed, False if it
    cannot be (no `setitimer`, not the main thread, or a timer already running
    that must not be clobbered). Always disarms and restores the previous
    handler on exit."""
    armed = False
    previous = None
    try:
        if (hasattr(signal, "setitimer") and hasattr(signal, "SIGALRM")
                and threading.current_thread() is threading.main_thread()
                and signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0)):
            def _expired(signum, frame):
                raise BookReadDeadline(
                    f"the optional market-data read exceeded {seconds}s")
            previous = signal.signal(signal.SIGALRM, _expired)
            signal.setitimer(signal.ITIMER_REAL, float(seconds))
            armed = True
        yield armed
    finally:
        if armed:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, previous if previous is not None
                          else signal.SIG_DFL)


#: What this order IS: the safety net closing a short the drill left behind.
#: An emergency close is its own population, never pooled with planned exits.
_ROUTE = {"route": "protective_close", "purpose": "safety_net_close",
          "execution_mode": "overlay"}


def _recording_broker(broker: Any, symbol: str, qty: float, path: str,
                      venue_label: str):
    """(order_broker, writer, recorder, error, note).

    The order goes through a `LedgerBroker` whose sink appends the close's
    execution record the moment it fills, so the exits the drill itself failed
    to complete (the worst ones) are in the sample set, not missing from it.
    Every read still uses the raw adapter.

    NOTHING here may delay or prevent the close:
      * no mark is read (the book's own touch is the reference, or there is
        none); the only optional read is the decision-time book, and it runs
        under `hard_deadline` and only through a getter that can take a
        timeout - otherwise it is skipped and the record is UNMEASURED;
      * ANY failure returns (broker, writer, None, reason, note): the close
        then goes out exactly as it did before this existed.
    `writer` is returned so the caller can read its final status."""
    writer = None
    try:
        import drill as _drill
        import ledger as _ledger
        writer = _drill.RecordWriter(path, venue_label, set())
        recorder = _ledger.LedgerBroker(
            broker, _ledger.Journal(), ms=lambda: int(time.time() * 1000),
            record_sink=writer)
    except Exception as exc:                                   # noqa: BLE001
        return broker, writer, None, f"{type(exc).__name__}: {exc}", ""
    note = ""
    declared = False
    try:
        with hard_deadline(BOOK_DEADLINE_S) as armed:
            if armed:
                recorder.begin_decision(
                    symbol=symbol, product="linear", side="Buy", qty=qty,
                    book_timeout_s=BOOK_TIMEOUT_S, **_ROUTE)
                declared = True
            else:
                note = ("no hard deadline can be armed here, so the decision-"
                        "time book was NOT read; the close is unmeasured")
    except BookReadDeadline as exc:
        note = f"{exc}; the close is unmeasured"
    except Exception as exc:                                   # noqa: BLE001
        note = f"decision-time book not read ({type(exc).__name__}: {exc})"
    if not declared:
        try:
            # a decision context with NO book and NO mark: the record then says
            # it could not be measured, rather than not existing
            recorder.begin_decision(symbol=symbol, product="linear",
                                    side="Buy", qty=qty, capture_book=False,
                                    **_ROUTE)
        except Exception as exc:                               # noqa: BLE001
            note = (note + "; " if note else "") + \
                f"no decision context ({type(exc).__name__}: {exc})"
    return recorder, writer, recorder, None, note


def _recording_status(path: str, *, attempted: bool, writer: Any,
                      recorder: Any, setup_error: Optional[str],
                      booking_error: Optional[str], note: str) -> dict:
    """The final, observable state of the close's evidence. One of:
    NOT_CONFIGURED (no path), NOT_ATTEMPTED (no order was sent, nothing to
    record), SETUP_FAILED, OK (a record is in the file and synced), RECOVERED
    (a write failed, the post-close flush wrote it), UNSYNCED (in the file,
    durability unconfirmed), FAILED (a record could not be written), or
    NOT_WRITTEN (an order went out and no record reached the file, with no
    write error: e.g. a booking failure). Recording failure is NEVER a failure
    of the close: this only describes the evidence."""
    if not path:
        return {"state": "NOT_CONFIGURED"}
    if not attempted:
        return {"state": "NOT_ATTEMPTED", "path": path,
                "why": "no order was sent, so there is nothing to record"}
    status: dict = {"path": path, "market_data_note": note,
                    "booking_error": booking_error}
    if setup_error is not None or writer is None:
        status.update(state="SETUP_FAILED", setup_error=setup_error or
                      "no writer", records_written=0, writer_errors=0)
        return status
    recovered = 0
    recovery_error = ""
    if writer.errors and recorder is not None:
        # a record the writer could not write: try ONCE more from the journal.
        # This never sends an order; it only re-attempts the file append.
        try:
            import drill as _drill
            recovered = _drill.append_execution_records(
                path, recorder.journal, writer.venue, writer.written,
                outcomes=recorder.executions)
        except Exception as exc:                               # noqa: BLE001
            recovery_error = f"{type(exc).__name__}: {exc}"
    written = writer.count + recovered
    status.update(records_written=written, writer_errors=writer.errors,
                  writer_sync_errors=writer.sync_errors,
                  writer_last_error=writer.last_error or recovery_error)
    if writer.errors:
        status["state"] = "RECOVERED" if recovered and not recovery_error \
            else "FAILED"
    elif written == 0:
        status["state"] = "NOT_WRITTEN"
    else:
        status["state"] = "OK"
    if writer.sync_errors or (recovery_error and recovered):
        if status["state"] in ("OK", "RECOVERED"):
            status["state"] = "UNSYNCED"
    return status


def write_recording_status(path: str, outcome: dict) -> None:
    """Best effort: the operator-visible record of what happened to the close's
    evidence, for the unattended runner's verdict. Never raises."""
    try:
        data = {"recording": outcome.get("recording"),
                "flat_verified": outcome.get("flat_verified"),
                "ok": outcome.get("ok"), "sent": outcome.get("sent")}
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(data, handle, sort_keys=True, default=str)
    except Exception:                                          # noqa: BLE001
        pass


def close_short(broker: Any, symbol: str, *, expected_qty: Optional[float] = None,
                sleep=time.sleep, records_path: str = "",
                venue_label: str = "testnet") -> dict:
    """Send the one reduce-only Buy for the venue's short, then verify flat.

    ``expected_qty`` is what the operator confirmed: if the venue's short is not
    exactly that when the order is about to go, nothing is sent.

    ``records_path`` appends this close's execution record (book-measured only
    where the public book could be read inside a hard deadline) to the drill's
    records file, labelled ``venue_label``. It is purely observational: every
    return carries a ``recording`` status saying what became of the evidence,
    and a recording failure is never a failure of the close, never delays it
    and never causes a second order."""
    def status(**kw) -> dict:
        return _recording_status(records_path, **kw)

    before = read_state(broker, symbol)
    problem = plan_problem(before)
    if problem:
        return {"ok": False, "refused": problem, "before": before,
                "recording": status(attempted=False, writer=None,
                                    recorder=None, setup_error=None,
                                    booking_error=None, note="")}
    qty = before["short_qty"]
    if expected_qty is not None and abs(qty - float(expected_qty)) > FLAT_DUST:
        return {"ok": False, "before": before, "refused": (
            f"the venue's short is {qty}, not the {expected_qty} that was "
            "confirmed; it changed while waiting. Nothing was sent"),
            "recording": status(attempted=False, writer=None, recorder=None,
                                setup_error=None, booking_error=None, note="")}
    if qty <= FLAT_DUST:
        return {"ok": True, "sent": False, "before": before, "after": before,
                "note": "the venue is already flat; nothing was sent",
                "recording": status(attempted=False, writer=None,
                                    recorder=None, setup_error=None,
                                    booking_error=None, note="")}
    order_broker, writer, recorder, setup_error, note = broker, None, None, \
        None, ""
    if records_path:
        order_broker, writer, recorder, setup_error, note = _recording_broker(
            broker, symbol, qty, records_path, venue_label)
    booking_error: Optional[str] = None

    def current_status() -> dict:
        return status(attempted=True, writer=writer, recorder=recorder,
                      setup_error=setup_error, booking_error=booking_error,
                      note=note)
    try:
        result = order_broker.place_market(symbol=symbol, side="Buy", qty=qty,
                                           product="linear")
    except Exception as exc:                                   # noqa: BLE001
        import ledger as _ledger
        if not isinstance(exc, _ledger.BookingFailedAfterFill):
            # the order itself failed or its outcome is unknown: this is the
            # caller's to handle exactly as before; the evidence status rides
            # on the exception so it is not lost
            exc.recording = current_status()
            raise
        # The venue ACCEPTED the order; only the bookkeeping failed. That is
        # not "the close failed": go on to verify flat, and say what was lost.
        result = {"booking_failed": str(exc)}
        booking_error = f"booking failed after the order was sent: {exc}"
    if result is None:
        # a definite rejection: nothing was filled and no second order follows
        return {"ok": False, "sent": False, "before": before,
                "note": "the venue rejected the order; nothing filled. Look at "
                        "the venue and the reject reason",
                "recording": current_status()}
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
    out = {"ok": flat, "sent": True, "before": before, "after": after,
           "fill": result, "flat_verified": flat,
           "recording": current_status()}
    if booking_error:
        out["recording_error"] = booking_error
    elif out["recording"]["state"] not in ("OK", "NOT_CONFIGURED"):
        out["recording_error"] = (out["recording"].get("writer_last_error")
                                  or out["recording"].get("setup_error")
                                  or out["recording"]["state"])
    return out


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
    ap.add_argument("--execution-records", default="",
                    help="append this close's execution record to the drill's "
                         "records file (testnet only; optional, and a failure "
                         "to record never blocks the close)")
    ap.add_argument("--recording-status-out", default="",
                    help="write what became of this close's execution record "
                         "(OK/RECOVERED/UNSYNCED/FAILED/...) here, for the "
                         "unattended runner's verdict")
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
                              expected_qty=state["short_qty"],
                              records_path=args.execution_records,
                              venue_label="testnet")
    except KeyboardInterrupt as exc:
        outcome = {"ok": False, "interrupted": str(exc)}
    except Exception as exc:                                   # noqa: BLE001
        outcome = {"ok": False, "error": f"{type(exc).__name__}: {exc}",
                   "recording": getattr(exc, "recording", None)}
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
        if args.recording_status_out:
            write_recording_status(args.recording_status_out, outcome)
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
