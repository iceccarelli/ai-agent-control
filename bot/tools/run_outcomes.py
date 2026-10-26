#!/usr/bin/env python3
"""What an unattended run LEARNED, in words that cannot be misread.

Shared by `runner_monitor.py` (read-only, the deployed default) and
`unattended_drill.py --conformance` (order-capable, explicitly invoked). Pure:
it parses text and classifies it, and imports nothing that can reach a venue.

OUTCOMES
  ENTRY_NOT_ELIGIBLE  readiness is good; the production entry gate
                      (carry_costs.entry_gate) refuses. No order was sent. This
                      is a correct strategy decision, not an operational error.
  MONITOR_READY       the checks completed and the entry gate PASSES. No order
                      was sent. Economic eligibility only: not permission to
                      execute, not a profitable trade.
  MONITOR_FAILED      required state or a safety check was unreadable,
                      contradictory or failed. Nothing is claimed about the
                      opportunity.
  CONFORMANCE_PASSED  reserved for an explicitly armed run in which an order was
                      actually placed, exited, captured and reconciled and the
                      venue ended flat. A no-trade run is NEVER this.
  CONFORMANCE_FAILED  an armed run that did not meet all of the above.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

ENTRY_NOT_ELIGIBLE = "ENTRY_NOT_ELIGIBLE"
MONITOR_READY = "MONITOR_READY"
MONITOR_FAILED = "MONITOR_FAILED"
CONFORMANCE_PASSED = "CONFORMANCE_PASSED"
CONFORMANCE_FAILED = "CONFORMANCE_FAILED"
OUTCOMES = (ENTRY_NOT_ELIGIBLE, MONITOR_READY, MONITOR_FAILED,
            CONFORMANCE_PASSED, CONFORMANCE_FAILED)

NO_ORDER_NOTE = "no order was sent by this run"
NOT_PROFIT_NOTE = ("economic eligibility is a model output; it is not "
                   "permission to execute and not evidence of profit")


def parse_report(text: str) -> Optional[Dict[str, Any]]:
    """The ONE top-level JSON object with a `verdict` printed at the start of a
    line, or None.

    The child's stderr (configuration warnings) shares the log with its JSON, so
    the object is found rather than assumed to be the whole file. Only objects
    that begin a line count (the report is printed, never embedded in prose), a
    second one makes the output AMBIGUOUS (None: a forged-looking object after
    the real report must not outvote it), and anything that cannot be decoded
    safely - including pathological nesting - is None.
    """
    decoder = json.JSONDecoder()
    found: List[Dict[str, Any]] = []
    i = 0
    n = len(text)
    while i < n:
        i = text.find("{", i)
        if i < 0:
            break
        if i != 0 and text[i - 1] != "\n":
            i += 1
            continue
        try:
            obj, end = decoder.raw_decode(text, i)
        except (ValueError, RecursionError):
            i += 1
            continue
        if isinstance(obj, dict) and "verdict" in obj:
            found.append(obj)
        i = end
    return found[0] if len(found) == 1 else None


#: The entry gate refusing on the ECONOMICS (carry_costs.GATE_REASONS that are a
#: judgment about funding, cost or basis). Anything else that stops an entry -
#: unreadable inputs, an unknown reason, an empty one - is not a strategy
#: decision and fails closed. tests/test_runner_monitor.py pins this against
#: carry_costs.GATE_REASONS so a new gate reason cannot be misread.
ECONOMIC_REFUSALS = frozenset({
    "FUNDING_NOT_POSITIVE", "CARRY_BELOW_COST_OF_CAPITAL",
    "ENTRY_BASIS_EXCEEDS_CARRY_BUDGET",
    "ENTRY_BASIS_DISCOUNT_EXCEEDS_CARRY_BUDGET",
    "BREAKEVEN_HOLD_EXCEEDS_DEMONSTRATED_HOLD"})

#: Refusal reasons that say an INPUT could not be used, not that the economics
#: are unfavourable. "Too few prints" or "no readable basis" is missing data, not
#: a strategy decision, so it is a monitor failure and not ENTRY_NOT_ELIGIBLE.
INPUT_REASONS = frozenset({
    "COST_INPUT_INVALID", "INSUFFICIENT_FUNDING_HISTORY", "BASIS_UNREADABLE",
    "FEE_RATES_UNREADABLE", "SPOT_UNAVAILABLE_BASIS_UNKNOWN",
    "PAIR_SYMBOLS_MISMATCH", "FUNDING_CADENCE_UNPROVEN"})


def classify_preflight(report: Optional[Dict[str, Any]], rc: Optional[int], *,
                       expect_order_gate_closed: bool = True) -> Dict[str, Any]:
    """Outcome of one `carry_preflight.py` run. Never raises.

    `expect_order_gate_closed`: the monitor runs with PAPER_TRADING=1 so the
    carry order gate refuses every order. A report that says the gate would
    ALLOW orders therefore contradicts the environment the monitor set, and is a
    failure rather than a reading.
    """
    reasons: List[str] = []

    def failed(*why: str) -> Dict[str, Any]:
        return {"outcome": MONITOR_FAILED, "reasons": list(reasons) + list(why),
                "order_sent": False, "assessment": _assessment(report)}

    try:
        return _classify(report, rc, expect_order_gate_closed, failed, reasons)
    except Exception as exc:                                   # noqa: BLE001
        return failed(f"the report could not be classified "
                      f"({type(exc).__name__}): nothing is claimed")


def _classify(report, rc, expect_order_gate_closed, failed, reasons):
    if not isinstance(report, dict):
        return failed("the preflight produced no readable report"
                      + (f" (exit {rc})" if rc is not None else ""))
    verdict = report.get("verdict")
    if verdict == "PREFLIGHT_REFUSED" or rc == 3:
        return failed("the environment was refused before anything was read: "
                      + str(report.get("reason", "")))
    if verdict not in ("PREFLIGHT_OK", "PREFLIGHT_BLOCKED"):
        return failed(f"unrecognised preflight verdict {verdict!r}")
    if verdict == "PREFLIGHT_BLOCKED":
        blockers = [str(b) for b in (report.get("blockers") or [])]
        return failed(*(blockers or ["the preflight reported BLOCKED"]))
    # PREFLIGHT_OK from here: look for contradictions before believing it
    if report.get("blockers"):
        return failed("verdict PREFLIGHT_OK contradicts the blockers it lists")
    if rc not in (None, 0, 5):
        return failed(f"verdict PREFLIGHT_OK contradicts the exit code {rc}")
    if str(report.get("venue", "")).lower() != "testnet" \
            or report.get("use_testnet") is not True:
        return failed("the report does not say venue=testnet use_testnet=true "
                      f"(venue={report.get('venue')!r} "
                      f"use_testnet={report.get('use_testnet')!r})")
    if expect_order_gate_closed:
        gate = report.get("order_gate_would_allow_orders")
        if gate is True or (gate is not False and gate):
            return failed("the carry order gate would ALLOW orders although "
                          "this run set PAPER_TRADING=1")
        if gate is not False:
            # Absent, null or not a boolean: the report does not SAY the gate
            # is closed, and a safety claim nobody made is not a closed gate.
            return failed("the report does not state "
                          "order_gate_would_allow_orders=false (found "
                          f"{gate!r}): an unstated gate is not a closed one")
    econ = report.get("entry_economics")
    if not isinstance(econ, dict) or "state" not in econ:
        return failed("the report carries no entry-economics assessment")
    state = econ["state"]
    if state == "ENTRY_ECONOMICS_UNAVAILABLE":
        return failed("the entry economics could not be assessed (an input "
                      "was unreadable or stale): " + str(econ.get("reason", "")))
    if state == "ENTRY_ECONOMICS_REFUSED":
        reason = str(econ.get("reason") or "")
        if reason in INPUT_REASONS:
            return failed("the entry gate refused because an input was "
                          f"unusable ({reason}): missing data, not an "
                          "economic judgment")
        if reason not in ECONOMIC_REFUSALS:
            return failed(f"the entry gate refused with {reason!r}, which is "
                          "not a recognised economic refusal: nothing is claimed")
        return {"outcome": ENTRY_NOT_ELIGIBLE, "reasons": [reason],
                "order_sent": False, "assessment": _assessment(report)}
    if state == "ENTRY_ECONOMICS_PASS":
        return {"outcome": MONITOR_READY, "reasons": [str(econ.get("reason", ""))],
                "order_sent": False, "assessment": _assessment(report)}
    return failed(f"unrecognised entry-economics state {state!r}")


def _assessment(report: Any) -> Dict[str, Any]:
    """The decision inputs, copied as read, plus what they do not mean."""
    econ = report.get("entry_economics") if isinstance(report, dict) else None
    decision = econ.get("decision") if isinstance(econ, dict) else None
    return {
        "entry_decision": decision,
        "economics_state": econ.get("state") if isinstance(econ, dict) else None,
        "notes": [NO_ORDER_NOTE, NOT_PROFIT_NOTE],
    }


def stood_aside_without_orders(drill_report: Any) -> Optional[str]:
    """The reason code when an armed drill's engine stood aside on the ECONOMICS
    and nothing was sent (a strategy refusal: ENTRY_NOT_ELIGIBLE), else None.
    A stand-aside for an unreadable input is not a strategy decision."""
    if not isinstance(drill_report, dict) or drill_report.get("orders_sent") != 0:
        return None
    for stage in drill_report.get("stages", []):
        if not isinstance(stage, dict):
            continue
        ev = stage.get("evidence") or {}
        if stage.get("ok") is False and ev.get("action") == "stand_aside" \
                and ev.get("reason") in ECONOMIC_REFUSALS:
            return str(ev["reason"])
    return None


def orders_exercised(drill_report: Any) -> bool:
    """True only when the drill's own report says an entry and an exit were both
    sent and nothing is left open. A run that sent nothing is never a pass."""
    return (isinstance(drill_report, dict)
            and isinstance(drill_report.get("orders_sent"), int)
            and drill_report["orders_sent"] >= 2
            and drill_report.get("still_open") is False)


def cycle_reconciled(recon_report: Any) -> bool:
    """True only when the reconciliation says the whole captured cycle is
    VENUE_RECONCILED and resolved. Anything unreadable is False."""
    if not isinstance(recon_report, dict) \
            or recon_report.get("schema") != "evidence_reconciliation/1":
        return False
    cycles = recon_report.get("carry_cycle") or []
    if not isinstance(cycles, list):
        return False
    # A row that is not an object is unreadable, hence not reconciled: it must
    # not raise out of the caller, which has already flattened the venue and
    # still owes the run a verdict.
    return bool(cycles) and all(
        isinstance(c, dict) and c.get("provenance") == "VENUE_RECONCILED"
        and c.get("resolved") is True for c in cycles)
