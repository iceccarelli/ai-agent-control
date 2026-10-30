#!/usr/bin/env python3
"""Net performance of a carry book, read only from what the ledger booked.

Reads a state database READ-ONLY, cuts the double-entry journal into cycles
(flat -> opened -> flat) and reports, per cycle and in total: confirmed versus
provisional net, funding, fees, financing, the price leg, return on the notional
committed, drawdown, concentration and holding time.

Nothing is estimated and nothing is assumed zero:

  * a cycle is CONFIRMED only if every fill's fee was venue-stated and every
    funding entry came from a venue settlement row; otherwise it is provisional
    and kept out of the confirmed total;
  * a quantity the ledger never recorded (the margin actually committed, the
    decision-time mark needed for slippage) is None with the reason, never a
    default;
  * a book with no closed cycle gets no ratios.

"Confirmed" here is per-entry evidence. Whether every settlement the venue
should have paid was booked is `carry_report.py` / `evidence_reconcile.py`'s
question. Places nothing; writes nothing.

THE MACHINE-READABLE REPORT (--json)
====================================
One document, every section of which carries an ``evidence_class`` from a
CLOSED set (``EVIDENCE_CLASSES``, plus ``UNDECLARED_ENVIRONMENT`` for an
execution ledger whose environment the operator did not declare):

  ACTUAL_RECONCILED      mainnet, environment corroborated by the database,
                         every fee venue-stated AND every funding lifecycle
                         positively reconciled, no unresolved carry order, every
                         ledger invariant holding. The ONLY class under which a
                         ``realized_cash``-type key may appear.
  OPEN_MARK_TO_MARKET    an open position valued at a mark (none is journalled
                         today, so this tool never emits it).
  PAPER_EXECUTION        a testnet or paper ledger. Never cash.
  HISTORICAL_SIMULATION  a replay/backtest ledger, or a counterfactual computed
                         from booked prices (the opportunity-cost comparison).
  FORWARD_SIMULATION     a forward simulation ledger.
  UNAVAILABLE            the figure does not exist, or exists only as a
                         provisional booking; ``why`` says which and why.

The ledger's class comes ONLY from ``--environment``; omitted, it is
UNDECLARED_ENVIRONMENT and nothing is presented as realized cash. A key that
names realized cash anywhere in a section of another class is refused by
``section()`` and again by ``validate_report()`` before anything is printed.

    python3 tools/carry_performance.py --db state/trading_state.db [--json]
        [--environment {mainnet,testnet,paper,historical_simulation,
                        forward_simulation}]
        [--cash-apr PCT [--cash-apr-source TEXT]] [--now-ms MS]
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import sqlite3
import sys
import time
from typing import Any, Dict, List, Mapping, Optional, Sequence

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import ledger as L  # noqa: E402

DAY_MS = 86_400_000
_SHORT_MEMO = re.compile(r"^short (\S+) @ (\S+)$")
_COVER_MEMO = re.compile(r"^cover (\S+) @ (\S+) from (\S+)$")
MARGIN_NOTE = ("margin committed comes from the venue's initial margin as "
               "observed on the liquidation reads; a cycle with no observed "
               "margin gets none, and no return on committed capital is "
               "computed from a guess; returns are on notional")


CAPITAL_BASIS = ("peak single-cycle committed capital (observed venue margin + "
                 "the spot leg); capital is reused between sequential cycles, so "
                 "the period figure is net over the period divided by the "
                 "largest amount tied up at once, not an account-level return")

# -- evidence classes ---------------------------------------------------------

ACTUAL_RECONCILED = "ACTUAL_RECONCILED"
OPEN_MARK_TO_MARKET = "OPEN_MARK_TO_MARKET"
PAPER_EXECUTION = "PAPER_EXECUTION"
HISTORICAL_SIMULATION = "HISTORICAL_SIMULATION"
FORWARD_SIMULATION = "FORWARD_SIMULATION"
UNAVAILABLE = "UNAVAILABLE"
EVIDENCE_CLASSES = (ACTUAL_RECONCILED, OPEN_MARK_TO_MARKET, PAPER_EXECUTION,
                    HISTORICAL_SIMULATION, FORWARD_SIMULATION, UNAVAILABLE)
#: The class of an execution ledger whose environment nobody declared.
UNDECLARED_ENVIRONMENT = "UNDECLARED_ENVIRONMENT"
SECTION_CLASSES = EVIDENCE_CLASSES + (UNDECLARED_ENVIRONMENT,)

#: What the operator may declare a ledger to be. Only "mainnet" can ever yield
#: ACTUAL_RECONCILED, and only when the database corroborates it.
ENVIRONMENTS = ("mainnet", "testnet", "paper", "historical_simulation",
                "forward_simulation")
_FIXED_CLASS = {"testnet": PAPER_EXECUTION, "paper": PAPER_EXECUTION,
                "historical_simulation": HISTORICAL_SIMULATION,
                "forward_simulation": FORWARD_SIMULATION}

#: A key naming realized cash. Allowed ONLY inside an ACTUAL_RECONCILED section.
CASH_KEY = re.compile(r"reali[sz]ed|cash", re.IGNORECASE)

REPORT_SCHEMA = "carry_performance/2"

#: `orders.status` values that are not terminal (persistence.unresolved_orders).
_UNRESOLVED_ORDER_STATUSES = ("pending", "submitted", "partial", "unknown",
                              "acknowledged", "cancel_requested", "timed_out",
                              "reconciliation_required")

RETURN_BASES = {
    "return_on_peak_notional_pct": {
        "numerator": "net of every closed cycle",
        "denominator": "largest closed hedged cycle's notional (perp qty x "
                       "perp entry price)",
        "time_basis": "the whole reporting period; not annualised"},
    "annualised_on_notional_in_market_pct": {
        "numerator": "net of every closed cycle",
        "denominator": "largest closed hedged cycle's notional",
        "time_basis": "x 365 / days in market (sum of closed cycles' holding "
                      "days); simple, not compounded; None under one day"},
    "period_return_on_peak_capital_pct": {
        "numerator": "net of every closed cycle",
        "denominator": CAPITAL_BASIS,
        "time_basis": "period_days calendar days, first cycle start to last "
                      "close; not annualised"},
    "return_on_committed_capital_pct": {
        "numerator": "that cycle's net",
        "denominator": "that cycle's committed capital (observed venue margin "
                       "+ spot leg)",
        "time_basis": "that cycle's holding period; not annualised"},
}


class ReportIntegrityError(RuntimeError):
    """A report that would present a non-cash figure as realized cash, or use
    a class outside the closed set. Never caught and printed anyway."""


def _cash_keys(obj: Any, path: str = "") -> List[str]:
    found: List[str] = []
    if isinstance(obj, Mapping):
        for k, v in obj.items():
            p = f"{path}.{k}" if path else str(k)
            if CASH_KEY.search(str(k)):
                found.append(p)
            found += _cash_keys(v, p)
    elif isinstance(obj, (list, tuple)):
        for i, v in enumerate(obj):
            found += _cash_keys(v, f"{path}[{i}]")
    return found


def section(evidence_class: str, *, why: Optional[Sequence[str]] = None,
            status: Optional[str] = None, **fields: Any) -> Dict[str, Any]:
    """The only constructor of a report section.

    Refuses a class outside the closed set, a realized-cash key under any class
    but ACTUAL_RECONCILED, and an UNAVAILABLE / UNDECLARED section that does
    not say why."""
    if evidence_class not in SECTION_CLASSES:
        raise ReportIntegrityError(
            f"evidence class {evidence_class!r} is not one of {SECTION_CLASSES}")
    why = list(why or [])
    if evidence_class != ACTUAL_RECONCILED:
        bad = _cash_keys(fields)
        if bad:
            raise ReportIntegrityError(
                f"a {evidence_class} section cannot carry realized-cash keys "
                f"{bad}")
    if evidence_class in (UNAVAILABLE, UNDECLARED_ENVIRONMENT) and not why:
        raise ReportIntegrityError(
            f"a {evidence_class} section must say why")
    return {"evidence_class": evidence_class,
            "status": status or ("FINAL" if evidence_class == ACTUAL_RECONCILED
                                 else "NOT_CASH"),
            "why": why, **fields}


def validate_report(report: Mapping[str, Any]) -> None:
    """Re-check the whole document. Raises ReportIntegrityError."""
    outside = {k: v for k, v in report.items() if k != "sections"}
    bad = _cash_keys(outside)
    if bad:
        raise ReportIntegrityError(f"realized-cash keys outside a section: {bad}")
    cash_ok = bool((report.get("environment") or {}).get("actual_reconciled_eligible"))
    for name, sec in (report.get("sections") or {}).items():
        cls = sec.get("evidence_class")
        if cls not in SECTION_CLASSES:
            raise ReportIntegrityError(f"section {name}: class {cls!r}")
        if cls == ACTUAL_RECONCILED and not cash_ok:
            raise ReportIntegrityError(
                f"section {name} is ACTUAL_RECONCILED but the environment is "
                "not cash-eligible")
        if cls != ACTUAL_RECONCILED:
            bad = _cash_keys({k: v for k, v in sec.items()
                              if k not in ("evidence_class", "status", "why")})
            if bad:
                raise ReportIntegrityError(
                    f"section {name} ({cls}) carries realized-cash keys {bad}")


# -- cycles from the journal ---------------------------------------------------

def _notional_of(entry: Any) -> Optional[float]:
    m = _SHORT_MEMO.match(entry.memo or "")
    if not m:
        return None
    try:
        return float(m.group(1)) * float(m.group(2))
    except ValueError:
        return None


def _memo_fill(entry: Any) -> Optional[tuple]:
    """(qty, price) of a perp fill from its memo, or None if unreadable."""
    m = (_SHORT_MEMO.match(entry.memo or "") if entry.kind == "perp_open"
         else _COVER_MEMO.match(entry.memo or ""))
    if not m:
        return None
    try:
        qty, price = float(m.group(1)), float(m.group(2))
    except ValueError:
        return None
    if not (math.isfinite(qty) and math.isfinite(price)):
        return None
    return (qty, price) if qty > 0 and price > 0 else None


_BAD_FEES = ("unknown", "unresolved", "unlabelled")
_BAD_FUNDING = ("engine_estimate", "unattributed", "unread")


def _basis(fee_statuses: List[str], bases: List[str]) -> str:
    if any(s in _BAD_FEES for s in fee_statuses):
        return "provisional_fees_incomplete"
    if any(b in _BAD_FUNDING for b in bases):
        return "provisional_funding_unverified"
    return "confirmed"


def _finish(cur: Dict[str, Any]) -> Dict[str, Any]:
    first, last = cur["first_open_ms"], cur["last_close_ms"]
    hedged = first is not None
    if hedged and cur["perp_closed"]:
        cur["outcome"] = "CLOSED"
    elif (not hedged and cur["spot_cost_usd"] > 0 and cur["spot_sold"]
          and abs(cur["spot_qty"]) <= L.DUST[L.BTC]):
        # Spot bought, the perp leg never landed, spot sold back flat: a round
        # trip of fees for nothing. It is a result, not an open position.
        cur["outcome"] = "ABORTED_BEFORE_HEDGE"
    else:
        cur["outcome"] = "OPEN"
    cur["closed"] = cur["outcome"] != "OPEN"
    if cur["outcome"] == "CLOSED":
        cur["end_ms"] = last
        cur["hold_days"] = (last - first) / DAY_MS
    elif cur["outcome"] == "ABORTED_BEFORE_HEDGE":
        cur["end_ms"] = cur["last_ms"]
        cur["hold_days"] = (cur["last_ms"] - cur["start_ms"]) / DAY_MS
    else:
        cur["end_ms"], cur["hold_days"] = None, None
    cur["net_usd"] = (cur["funding_usd"] - cur["fees_usd"] - cur["borrow_usd"]
                      + cur["realised_usd"])
    cur["notional_usd"] = (None if not hedged or cur["notional_unknown"]
                           or cur["notional_usd"] <= 0 else cur["notional_usd"])
    fee_statuses, bases = cur.pop("_fee_statuses"), cur.pop("_bases")
    cur["basis"] = _basis(fee_statuses, bases)
    cur["fees_unconfirmed_fills"] = sum(1 for s in fee_statuses
                                        if s in _BAD_FEES)
    cur["funding_unverified_entries"] = sum(1 for b in bases
                                            if b in _BAD_FUNDING)
    # A perp fill whose memo cannot be read leaves the quantity and prices
    # unknown (None), never a partial sum passed off as the whole.
    opens, closes = cur.pop("_opens"), cur.pop("_closes")
    unread = cur.pop("_fill_memo_unread")
    qty = sum(q for q, _ in opens) if opens and not unread else None
    cur["hedged_qty"] = qty
    cur["perp_fills"] = len(opens) + len(closes)
    cur["perp_entry_price"] = (sum(q * p for q, p in opens) / qty
                               if qty else None)
    cur["perp_exit_price"] = (
        sum(q * p for q, p in closes) / sum(q for q, _ in closes)
        if qty and closes and cur["outcome"] == "CLOSED" else None)
    cur["overlay"] = hedged and cur["spot_cost_usd"] <= 0
    cur["has_perp"] = hedged
    for key in ("perp_closed", "notional_unknown", "first_open_ms",
                "last_close_ms", "perp_qty", "spot_qty", "spot_sold", "last_ms"):
        cur.pop(key, None)
    return cur


def cycles_from_journal(journal: Any) -> List[Dict[str, Any]]:
    """One row per ledger cycle id, in time order. The journal assigns the id
    (and re-derives it for rows written before it existed), so this reader no
    longer has a boundary rule of its own."""
    by_id: Dict[str, Dict[str, Any]] = {}
    order: List[str] = []
    for e in journal.entries:
        if not e.cycle:
            continue                      # before any cycle (opening balance)
        cur = by_id.get(e.cycle)
        if cur is None:
            cur = by_id[e.cycle] = {
                "cycle_id": e.cycle, "start_ms": e.ms, "last_ms": e.ms,
                "funding_usd": 0.0, "fees_usd": 0.0, "borrow_usd": 0.0,
                "realised_usd": 0.0, "notional_usd": 0.0,
                "notional_unknown": False, "perp_closed": False,
                "perp_qty": 0.0, "spot_cost_usd": 0.0, "spot_qty": 0.0,
                "spot_sold": False, "first_open_ms": None,
                "last_close_ms": None, "_fee_statuses": [], "_bases": [],
                "revalue_usd": 0.0, "fills": 0, "_opens": [], "_closes": [],
                "_fill_memo_unread": False}
            order.append(e.cycle)
        cur["last_ms"] = e.ms
        for line in e.lines:
            if line.account == L.FUNDING:
                cur["funding_usd"] += -line.usd
            elif line.account == L.FEES:
                cur["fees_usd"] += line.usd
            elif line.account == L.BORROW:
                cur["borrow_usd"] += line.usd
            elif line.account == L.REALISED:
                cur["realised_usd"] += -line.usd
            elif line.account == L.PERP:
                cur["perp_qty"] += line.qty
            elif line.account == L.BTC and e.kind in ("spot_buy", "spot_sell"):
                cur["spot_qty"] += line.qty
        if e.kind in L.FILL_KINDS:
            cur["fills"] += 1
        if e.kind in ("perp_open", "perp_close"):
            fill = _memo_fill(e)
            if fill is None:
                cur["_fill_memo_unread"] = True
            else:
                cur["_opens" if e.kind == "perp_open" else "_closes"].append(fill)
        if e.kind == "revalue":
            cur["revalue_usd"] += -sum(l.usd for l in e.lines
                                       if l.account == L.REALISED)
        if e.kind == "spot_sell":
            cur["spot_sold"] = True
        if e.fee_status:
            cur["_fee_statuses"].append(e.fee_status)
        if e.basis:
            cur["_bases"].append(e.basis)
        if e.kind == "spot_buy":
            cur["spot_cost_usd"] += sum(
                l.usd for l in e.lines if l.account == L.BTC and l.usd > 0)
        if e.kind == "perp_open":
            if cur["first_open_ms"] is None:
                cur["first_open_ms"] = e.ms
            n = _notional_of(e)
            if n is None:
                cur["notional_unknown"] = True
            else:
                cur["notional_usd"] += n
        if e.kind == "perp_close" and abs(cur["perp_qty"]) <= L.DUST[L.PERP]:
            cur["perp_closed"] = True
            cur["last_close_ms"] = e.ms
    return [_finish(by_id[c]) for c in order]


def _cycle_ms(cycle_id: str) -> Optional[int]:
    """The open timestamp inside a `cyc-<ms>` or `cyc-<ms>-<n>` id."""
    parts = str(cycle_id).split("-")
    return int(parts[1]) if len(parts) >= 2 and parts[0] == "cyc" \
        and parts[1].isdigit() else None


def attach_capital(cycles: Sequence[Dict[str, Any]],
                   observations: Sequence[Dict[str, Any]]) -> None:
    """Add committed capital to each cycle from what was OBSERVED.

    Margin is the peak initial margin the venue stated for the cycle's position
    (liquidation observations keyed by the cycle). The spot leg is its purchase
    cost when the book bought it, else the hedged inventory valued at entry.
    With no observed margin the cycle gets None for margin and for committed
    capital: a return on capital is never computed from a guess."""
    peaks: Dict[int, float] = {}
    for o in observations:
        ms, im = o.get("cycle_opened_ms"), o.get("position_im_usd")
        try:
            ms, im = int(ms), float(im)
        except (TypeError, ValueError):
            continue
        if im == im and abs(im) != float("inf") and im >= 0:
            peaks[ms] = max(peaks.get(ms, 0.0), im)
    for c in cycles:
        c["margin_committed_usd"] = peaks.get(_cycle_ms(c["cycle_id"]))
        if c.get("spot_cost_usd", 0.0) > 0:
            c["spot_leg_usd"], c["spot_leg_basis"] = c["spot_cost_usd"], "bought_at_cost"
        elif c.get("notional_usd"):
            c["spot_leg_usd"] = c["notional_usd"]
            c["spot_leg_basis"] = "client_inventory_at_entry"
        else:
            c["spot_leg_usd"], c["spot_leg_basis"] = None, "unknown"
        c["capital_committed_usd"] = (
            None if c["margin_committed_usd"] is None or c["spot_leg_usd"] is None
            else c["margin_committed_usd"] + c["spot_leg_usd"])
        c["return_on_committed_capital_pct"] = (
            100.0 * c["net_usd"] / c["capital_committed_usd"]
            if c["closed"] and c["outcome"] == "CLOSED"
            and c["capital_committed_usd"] else None)


def summarise(cycles: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    closed = [c for c in cycles if c["closed"]]
    s: Dict[str, Any] = {
        "closed_cycles": len(closed),
        "aborted_cycles": sum(1 for c in closed
                              if c["outcome"] == "ABORTED_BEFORE_HEDGE"),
        "open_cycles": len(cycles) - len(closed),
        "margin_committed_usd": None, "margin_note": MARGIN_NOTE,
        "peak_capital_committed_usd": None,
        "period_return_on_peak_capital_pct": None, "period_days": None,
        "capital_basis": CAPITAL_BASIS,
        "slippage_note": ("decision-time marks are journalled only for fills "
                          "whose order path supplied one; slippage is the "
                          "report's slippage section and is UNAVAILABLE (not "
                          "zero) where a mark is missing; fees are the venue's"),
        "confirmed_basis": ("'confirmed' here is PER-ENTRY evidence (fees "
                            "venue-stated, funding from venue rows); it does "
                            "NOT mean lifecycle-reconciled and is not realized "
                            "cash: see the realized_cash section"),
    }
    if not closed:
        s.update(status="NO_CLOSED_CYCLES", net_usd_confirmed=0.0,
                 net_usd_provisional=0.0, provisional_cycles=0)
        for key in ("return_on_peak_notional_pct", "win_rate",
                    "max_drawdown_usd", "annualised_on_notional_in_market_pct",
                    "mean_hold_days", "best_cycle_share_of_winning_net_pct"):
            s[key] = None
        return s
    confirmed = [c for c in closed if c["basis"] == "confirmed"]
    provisional = [c for c in closed if c["basis"] != "confirmed"]
    s["status"] = "OK"
    s["net_usd_confirmed"] = sum(c["net_usd"] for c in confirmed)
    s["net_usd_provisional"] = sum(c["net_usd"] for c in provisional)
    s["provisional_cycles"] = len(provisional)
    s["funding_usd"] = sum(c["funding_usd"] for c in closed)
    s["fees_usd"] = sum(c["fees_usd"] for c in closed)
    s["borrow_usd"] = sum(c["borrow_usd"] for c in closed)
    s["price_leg_usd"] = sum(c["realised_usd"] for c in closed)
    s["gross_before_costs_usd"] = s["funding_usd"] + s["price_leg_usd"]
    s["fees_pct_of_funding"] = (100.0 * s["fees_usd"] / s["funding_usd"]
                                if s["funding_usd"] > 0 else None)
    hedged = [c for c in closed if c["outcome"] == "CLOSED"]
    notionals = [c["notional_usd"] for c in hedged]
    peak = (max(notionals) if notionals and all(n is not None for n in notionals)
            else None)
    s["peak_notional_usd"] = peak
    total_net = sum(c["net_usd"] for c in closed)
    s["return_on_peak_notional_pct"] = (100.0 * total_net / peak
                                        if peak else None)
    days = sum(c["hold_days"] or 0.0 for c in closed)
    s["days_in_market"] = days
    s["annualised_on_notional_in_market_pct"] = (
        100.0 * total_net / peak * 365.0 / days
        if peak and days >= 1.0 else None)
    cum = peak_cum = drawdown = 0.0
    for c in sorted(closed, key=lambda c: c["start_ms"]):
        cum += c["net_usd"]
        peak_cum = max(peak_cum, cum)
        drawdown = max(drawdown, peak_cum - cum)
    s["max_drawdown_usd"] = drawdown
    s["win_rate"] = sum(1 for c in closed if c["net_usd"] > 0) / len(closed)
    wins = sorted((c["net_usd"] for c in closed if c["net_usd"] > 0),
                  reverse=True)
    s["best_cycle_share_of_winning_net_pct"] = (
        100.0 * wins[0] / sum(wins) if wins else None)
    s["mean_hold_days"] = days / len(closed)
    margins = [c["margin_committed_usd"] for c in hedged
               if c.get("margin_committed_usd") is not None]
    s["margin_committed_usd"] = max(margins) if margins else None
    ends = [c["end_ms"] for c in closed if c.get("end_ms") is not None]
    starts = [c["start_ms"] for c in closed]
    s["period_days"] = ((max(ends) - min(starts)) / DAY_MS
                        if ends and starts else None)
    caps = [c.get("capital_committed_usd") for c in hedged]
    if caps and all(c is not None for c in caps):
        s["peak_capital_committed_usd"] = max(caps)
        s["period_return_on_peak_capital_pct"] = 100.0 * total_net / max(caps)
    return s


# -- reading the database (read-only) ------------------------------------------

def _ro(db_path: str) -> sqlite3.Connection:
    if not os.path.exists(db_path):
        raise FileNotFoundError(db_path)
    return sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)


def load_journal(db_path: str) -> Any:
    conn = _ro(db_path)
    try:
        row = conn.execute(
            "SELECT payload FROM carry_ledger WHERE id = 1").fetchone()
    finally:
        conn.close()
    if row is None:
        raise LookupError("no carry ledger in this database")
    return L.Journal.from_rows(json.loads(row[0] or "[]"))


def load_observations(db_path: str) -> List[Dict[str, Any]]:
    """The liquidation observations, read-only; [] when the table or columns
    are not there (an older database)."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT cycle_opened_ms, position_im_usd FROM "
            "carry_risk_observation").fetchall()
    except sqlite3.Error:
        return []
    finally:
        conn.close()
    return [dict(r) for r in rows]


def load_environment_record(db_path: str) -> Dict[str, Any]:
    """What the database itself recorded about whose orders it booked
    (persistence.StateStore.record_execution_environment / set_equity_scope).
    Read-only; absent keys are None / []."""
    out: Dict[str, Any] = {"recorded": None, "seen": [], "equity_scope": None}
    conn = _ro(db_path)
    try:
        rows = dict(conn.execute(
            "SELECT key, value FROM meta WHERE key IN ('execution_environment',"
            " 'execution_environments_seen', 'equity_scope')").fetchall())
    except sqlite3.Error:
        rows = {}
    finally:
        conn.close()
    out["recorded"] = rows.get("execution_environment")
    out["seen"] = [v for v in str(rows.get("execution_environments_seen")
                                  or "").split(",") if v]
    out["equity_scope"] = rows.get("equity_scope")
    return out


def load_unresolved_carry_orders(db_path: str) -> Optional[int]:
    """Carry-leg orders not in a terminal state, or None when the database has
    no orders table (then nothing proves every fill reached the ledger)."""
    conn = _ro(db_path)
    try:
        marks = ",".join("?" for _ in _UNRESOLVED_ORDER_STATUSES)
        row = conn.execute(
            f"SELECT COUNT(*) FROM orders WHERE status IN ({marks}) AND "
            "purpose LIKE 'carry-%'", _UNRESOLVED_ORDER_STATUSES).fetchone()
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    return int(row[0])


# -- environment, reconciliation, invariants -----------------------------------

def classify_environment(declared: Optional[str],
                         record: Optional[Mapping[str, Any]] = None
                         ) -> Dict[str, Any]:
    """The evidence class of an execution ledger, from the EXPLICIT declaration.

    ``booked_figures_class`` is what every figure that is not ACTUAL_RECONCILED gets.
    ``actual_reconciled_eligible`` is True only for a mainnet declaration the database
    corroborates (every environment it ever recorded is mainnet, and its
    equity scope does not say testnet). A declaration the database contradicts
    is UNAVAILABLE: a ledger whose provenance is in dispute is not classified.
    """
    record = dict(record or {})
    seen = list(record.get("seen") or [])
    scope = str(record.get("equity_scope") or "")
    env = {"declared": declared, "recorded": record.get("recorded"),
           "recorded_seen": seen, "equity_scope": record.get("equity_scope"),
           "actual_reconciled_eligible": False, "reasons": []}
    if declared is None:
        env.update(booked_figures_class=UNDECLARED_ENVIRONMENT, reasons=[
            "ENVIRONMENT_NOT_DECLARED: --environment was not given, so no "
            "figure in this report is realized cash"])
        return env
    if declared not in ENVIRONMENTS:
        raise ValueError(f"environment {declared!r} not in {ENVIRONMENTS}")
    contra: List[str] = []
    if declared in ("historical_simulation", "forward_simulation"):
        if seen:
            contra.append(f"the database records execution environments "
                          f"{seen}; a simulation ledger has none")
    elif any(s != declared for s in seen):
        contra.append(f"declared {declared} but the database recorded "
                      f"{seen}")
    if declared == "mainnet" and re.search(r"testnet=True", scope):
        contra.append(f"declared mainnet but the equity scope is {scope!r}")
    if declared == "testnet" and re.search(r"testnet=False", scope):
        contra.append(f"declared testnet but the equity scope is {scope!r}")
    if contra:
        env.update(booked_figures_class=UNAVAILABLE, reasons=[
            "ENVIRONMENT_CONTRADICTS_DATABASE: " + "; ".join(contra)])
        return env
    if declared in _FIXED_CLASS:
        env.update(booked_figures_class=_FIXED_CLASS[declared], reasons=[
            f"ENVIRONMENT_{declared.upper()}: a {declared} ledger is never "
            "cash"])
        return env
    # mainnet
    env["booked_figures_class"] = UNAVAILABLE
    if seen != ["mainnet"]:
        env["reasons"] = [
            "ENVIRONMENT_NOT_RECORDED: declared mainnet but the database has "
            "no execution-environment record to corroborate it "
            "(StateStore.record_execution_environment)"]
        return env
    env["actual_reconciled_eligible"] = True
    return env


def cycle_funding_reconciliation(journal: Any, *, now_ms: Optional[int] = None
                                 ) -> Dict[str, Dict[str, Any]]:
    """Per cycle id: is the funding of every perp lifecycle in it positively
    reconciled (ledger.lifecycle_reconciliation)? A cycle with no perp
    exposure has no funding to reconcile."""
    ref_cycle = {e.ref: e.cycle for e in journal.entries if e.cycle}
    out: Dict[str, Dict[str, Any]] = {}
    for row in L.lifecycle_reconciliation(journal, now_ms=now_ms):
        first_ref = str(row["lifecycle_id"]).split(":", 2)[-1]
        cid = ref_cycle.get(first_ref, "")
        cur = out.setdefault(cid, {"reconciled": True, "lifecycles": [],
                                   "reasons": []})
        cur["lifecycles"].append(row["lifecycle_id"])
        if not row["reconciled"]:
            cur["reconciled"] = False
            cur["reasons"].append(
                f"{row['lifecycle_id']}: {row['state']} "
                f"({', '.join(row['reasons']) or 'no reason recorded'})")
    return out


def check_invariants(journal: Any, cycles: Sequence[Mapping[str, Any]],
                     statement: Mapping[str, Any]) -> List[Dict[str, Any]]:
    """Accounting invariants, each one a named pass/fail with the detail."""
    out: List[Dict[str, Any]] = []
    uncycled = [e for e in journal.entries if not e.cycle]
    pre_net = 0.0
    for e in uncycled:
        for line in e.lines:
            if line.account == L.FUNDING:
                pre_net += -line.usd
            elif line.account in (L.FEES, L.BORROW):
                pre_net -= line.usd
            elif line.account == L.REALISED:
                pre_net += -line.usd
    cyc_sum = sum(float(c["net_usd"]) for c in cycles)
    diff = cyc_sum + pre_net - float(statement["net_usd"])
    out.append({"name": "cycle_net_equals_statement_net",
                "ok": abs(diff) <= L.BALANCE_TOLERANCE,
                "detail": {"sum_of_cycle_nets_usd": cyc_sum,
                           "net_outside_any_cycle_usd": pre_net,
                           "statement_net_usd": statement["net_usd"],
                           "difference_usd": diff}})
    ids = [c["cycle_id"] for c in cycles]
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    out.append({"name": "cycle_ids_unique", "ok": not dupes,
                "detail": {"duplicated": dupes}})
    # Every entry carries exactly one cycle id, and a cycle's entries form one
    # unbroken run: an id that reappears after another cycle began would mean
    # an entry was booked to two positions' histories.
    finished: set = set()
    current = None
    split: List[str] = []
    for e in journal.entries:
        if not e.cycle:
            continue
        if e.cycle != current:
            if e.cycle in finished:
                split.append(f"{e.ref}->{e.cycle}")
            if current is not None:
                finished.add(current)
            current = e.cycle
    out.append({"name": "each_entry_in_exactly_one_contiguous_cycle",
                "ok": not split, "detail": {"reappearing": split}})
    entry_count = sum(1 for e in journal.entries if e.cycle)
    by_cycle = {c["cycle_id"] for c in cycles}
    orphans = sorted({e.cycle for e in journal.entries
                      if e.cycle and e.cycle not in by_cycle})
    out.append({"name": "every_cycled_entry_reported_in_one_cycle",
                "ok": not orphans,
                "detail": {"cycled_entries": entry_count,
                           "cycle_ids_missing_from_report": orphans}})
    return out


# -- the report ----------------------------------------------------------------

def _classify_cycle(c: Dict[str, Any], env: Mapping[str, Any],
                    recon: Mapping[str, Mapping[str, Any]],
                    blockers: Sequence[str]) -> None:
    """Attach evidence_class, fees_complete, funding_complete and the reasons
    a cycle is not ACTUAL_RECONCILED."""
    reasons: List[str] = []
    c["fees_complete"] = c["fees_unconfirmed_fills"] == 0
    rec = recon.get(c["cycle_id"])
    lifecycles_ok = rec["reconciled"] if rec else not c["has_perp"]
    c["funding_complete"] = (c["funding_unverified_entries"] == 0
                             and lifecycles_ok)
    if not c["closed"]:
        reasons.append("OPEN: the position has not gone flat; nothing in it is "
                       "a result")
    if not c["fees_complete"]:
        reasons.append(f"FEES_INCOMPLETE: {c['fees_unconfirmed_fills']} fill(s) "
                       "with a fee the venue did not state or that has no "
                       "booking treatment")
    if c["funding_unverified_entries"]:
        reasons.append(f"FUNDING_NOT_VENUE_STATED: "
                       f"{c['funding_unverified_entries']} funding entr(y/ies) "
                       "estimated, unattributed or unread")
    if not lifecycles_ok:
        reasons.append("FUNDING_NOT_RECONCILED: " + (
            "; ".join(rec["reasons"]) if rec else
            "no perp lifecycle reconciliation found for this cycle"))
    reasons += list(blockers)
    c["provisional_reasons"] = reasons
    c["evidence_class"] = (ACTUAL_RECONCILED
                           if env["actual_reconciled_eligible"] and not reasons
                           else env["booked_figures_class"])


def _public_cycle(c: Mapping[str, Any]) -> Dict[str, Any]:
    """A cycle as a section shows it. An incomplete fee or funding figure is
    None with the booked amount beside it, never a number read as final."""
    fees_ok, fund_ok = c["fees_complete"], c["funding_complete"]
    return {
        "cycle_id": c["cycle_id"], "outcome": c["outcome"],
        "start_ms": c["start_ms"], "end_ms": c.get("end_ms"),
        "hold_days": c.get("hold_days"), "fills": c["fills"],
        "fees_usd": c["fees_usd"] if fees_ok else None,
        "fees_booked_usd": c["fees_usd"], "fees_complete": fees_ok,
        "fees_unconfirmed_fills": c["fees_unconfirmed_fills"],
        "funding_usd": c["funding_usd"] if fund_ok else None,
        "funding_booked_usd": c["funding_usd"], "funding_complete": fund_ok,
        "financing_usd": c["borrow_usd"],
        "price_leg_usd": c["realised_usd"],
        "net_usd": c["net_usd"] if fees_ok and fund_ok and c["closed"] else None,
        "booked_net_usd": c["net_usd"],
        "notional_usd": c.get("notional_usd"),
        "capital_committed_usd": c.get("capital_committed_usd"),
        "return_on_committed_capital_pct": c.get(
            "return_on_committed_capital_pct"),
        "provisional_reasons": list(c["provisional_reasons"]),
    }


def _fill_price_qty(e: Any) -> Optional[tuple]:
    """(side, qty, price) of a fill entry, from its own lines and memo."""
    if e.kind in ("perp_open", "perp_close"):
        f = _memo_fill(e)
        return None if f is None else (
            "Sell" if e.kind == "perp_open" else "Buy", f[0], f[1])
    if e.kind == "spot_buy" and e.lines and e.lines[0].qty > 0:
        return "Buy", e.lines[0].qty, e.lines[0].usd / e.lines[0].qty
    if e.kind == "spot_sell" and len(e.lines) > 1 and e.lines[1].qty < 0:
        q = -e.lines[1].qty
        return "Sell", q, e.lines[0].usd / q
    return None


def slippage_section(journal: Any, env: Mapping[str, Any]) -> Dict[str, Any]:
    fills = [e for e in journal.entries if e.kind in L.FILL_KINDS]
    if not fills:
        return section(UNAVAILABLE, why=["NO_FILLS: nothing to measure"],
                       fills=0)
    measured = []
    for e in fills:
        if e.decision_mark is None:
            continue
        spq = _fill_price_qty(e)
        if spq is None:
            continue
        side, qty, price = spq
        cost = ((price - e.decision_mark) if side == "Buy"
                else (e.decision_mark - price)) * qty
        measured.append({"ref": e.ref, "side": side, "qty": qty,
                         "fill_price": price, "decision_mark": e.decision_mark,
                         "slippage_cost_usd": cost,
                         "fee_status": e.fee_status})
    fields = dict(fills=len(fills), measured_fills=len(measured),
                  sign="positive = adverse to the book",
                  measured=measured)
    if len(measured) < len(fills):
        return section(UNAVAILABLE, why=[
            f"DECISION_MARK_NOT_JOURNALLED: {len(fills) - len(measured)} of "
            f"{len(fills)} fills carry no decision-time mark; slippage over "
            "the book is unknown, not zero"],
            slippage_cost_usd_measured_subset=sum(
                m["slippage_cost_usd"] for m in measured), **fields)
    total = sum(m["slippage_cost_usd"] for m in measured)
    if env["actual_reconciled_eligible"] and all(m["fee_status"] == "confirmed"
                                    for m in measured):
        return section(ACTUAL_RECONCILED, slippage_cost_usd=total, **fields)
    cls = env["booked_figures_class"]
    return section(cls, why=list(env["reasons"]) or [
        "fills on a mainnet ledger whose fees are not all confirmed"],
        status="PROVISIONAL" if cls == UNAVAILABLE else "NOT_CASH",
        slippage_cost_usd=total, **fields)


def build_report(db_path: str, *, environment: Optional[str] = None,
                 cash_apr_pct: Optional[float] = None,
                 cash_apr_source: str = "",
                 now_ms: Optional[int] = None) -> Dict[str, Any]:
    """The one machine-readable performance report. Reads only."""
    import performance_analytics as PA

    journal = load_journal(db_path)
    now_ms = int(time.time() * 1000) if now_ms is None else int(now_ms)
    env = classify_environment(environment, load_environment_record(db_path))
    statement = journal.statement(now_ms=now_ms)
    cycles = cycles_from_journal(journal)
    attach_capital(cycles, load_observations(db_path))
    summary = summarise(cycles)
    invariants = check_invariants(journal, cycles, statement)
    unresolved = load_unresolved_carry_orders(db_path)

    blockers: List[str] = list(env["reasons"])
    bad_inv = [i["name"] for i in invariants if not i["ok"]]
    if bad_inv:
        blockers.append(f"INVARIANT_VIOLATED: {bad_inv}")
    if unresolved is None:
        blockers.append("ORDER_RECORD_ABSENT: no orders table, so nothing "
                        "proves every fill reached the ledger")
    elif unresolved:
        blockers.append(f"UNRESOLVED_CARRY_ORDERS: {unresolved} carry order(s) "
                        "not terminal; a fill may exist the ledger has not "
                        "booked")
    recon = cycle_funding_reconciliation(journal, now_ms=now_ms)
    for c in cycles:
        _classify_cycle(c, env, recon, blockers)

    closed = [c for c in cycles if c["closed"]]
    actual = [c for c in closed if c["evidence_class"] == ACTUAL_RECONCILED]
    other = [c for c in closed if c["evidence_class"] != ACTUAL_RECONCILED]
    open_ = [c for c in cycles if not c["closed"]]
    sections: Dict[str, Any] = {}

    if actual:
        sections["realized_cash"] = section(
            ACTUAL_RECONCILED,
            why=([f"{len(other)} closed cycle(s) excluded: not reconciled"]
                 if other else []),
            realized_cash_net_usd=sum(c["net_usd"] for c in actual),
            realized_cash_funding_usd=sum(c["funding_usd"] for c in actual),
            realized_cash_fees_usd=sum(c["fees_usd"] for c in actual),
            realized_cash_financing_usd=sum(c["borrow_usd"] for c in actual),
            realized_cash_price_leg_usd=sum(c["realised_usd"] for c in actual),
            realized_cash_cycles=len(actual),
            realized_cash_cycle_ids=[c["cycle_id"] for c in actual])
    elif closed:
        sections["realized_cash"] = section(UNAVAILABLE, why=sorted(
            {r for c in closed for r in c["provisional_reasons"]}))
    else:
        sections["realized_cash"] = section(UNAVAILABLE, why=list(blockers) + [
            "NO_CLOSED_CYCLES: no cycle has gone flat"])

    cls = env["booked_figures_class"]
    sections["closed_cycles_booked"] = section(
        cls,
        why=(sorted({r for c in other for r in c["provisional_reasons"]})
             or list(env["reasons"])
             or ["every closed cycle is ACTUAL_RECONCILED"]),
        status="PROVISIONAL" if cls in (UNAVAILABLE, UNDECLARED_ENVIRONMENT)
        else "NOT_CASH",
        cycles=[_public_cycle(c) for c in other],
        booked_net_usd_total=sum(c["net_usd"] for c in other),
        cycle_count=len(other))

    sections["open_positions"] = section(
        UNAVAILABLE,
        why=["NO_MARK_JOURNALLED: an open position is not valued (no mark is "
             "journalled); only the costs and income booked to date are "
             "listed, and none of it is a result"],
        status="OPEN", execution_evidence_class=cls,
        open_perp_qty=statement["open_perp_qty"],
        cycles=[_public_cycle(c) for c in open_])

    all_actual = (env["actual_reconciled_eligible"] and closed and not other and not open_)
    detail_cycles = []
    for c in cycles:
        d = {k: v for k, v in c.items() if k != "realised_usd"}
        d["price_leg_usd"] = c["realised_usd"]
        detail_cycles.append(d)
    sections["ledger_detail"] = section(
        ACTUAL_RECONCILED if all_actual else cls,
        why=[] if all_actual else (sorted({r for c in cycles
                                           for r in c["provisional_reasons"]})
                                   or list(env["reasons"])
                                   or ["NO_CYCLES"]),
        status=None if all_actual else (
            "PROVISIONAL" if cls in (UNAVAILABLE, UNDECLARED_ENVIRONMENT)
            else "NOT_CASH"),
        summary=summary, cycles=detail_cycles, return_bases=RETURN_BASES,
        statement={k: v for k, v in statement.items()
                   if k not in ("funding_lifecycles", "_cash_check",
                                "realised_usd")})

    sections["slippage"] = slippage_section(journal, env)
    sections["spread_cost"] = section(
        UNAVAILABLE, why=[
            "BOOK_TOP_NOT_JOURNALLED: the bid/ask at decision time is not "
            "recorded, so the spread paid is not computed; it is unknown, not "
            "zero"])

    opp = [PA.overlay_opportunity_cost(c, benchmark_apr_pct=cash_apr_pct,
                                       benchmark_source=cash_apr_source)
           for c in closed if c["outcome"] == "CLOSED"]
    any_avail = any(o["evidence_class"] == HISTORICAL_SIMULATION for o in opp)
    sections["opportunity_cost"] = section(
        HISTORICAL_SIMULATION if any_avail else UNAVAILABLE,
        why=([] if any_avail else (
            ["NO_CLOSED_HEDGED_CYCLE"] if not opp else
            sorted({o.get("why", "") for o in opp} - {""})
            or ["no alternative computable"])),
        status="COUNTERFACTUAL",
        benchmark_apr_pct=cash_apr_pct,
        benchmark_source=(cash_apr_source or "operator-supplied")
        if cash_apr_pct is not None else None,
        note=("counterfactuals from booked prices; the overlay leg keeps its "
              "own evidence class (overlay_leg_evidence_class) and is never "
              "upgraded by this comparison"),
        cycles=opp)

    report = {"report_schema": REPORT_SCHEMA, "db": db_path,
              "as_of_ms": now_ms,
              "evidence_classes": list(SECTION_CLASSES),
              "environment": env,
              "unresolved_carry_orders": unresolved,
              "invariants": invariants,
              "invariants_hold": not bad_inv,
              "sections": sections}
    validate_report(report)
    return report


def report_lines(cycles: Sequence[Dict[str, Any]], s: Dict[str, Any]) -> List[str]:
    f = lambda v, p="{:,.2f}": "n/a" if v is None else p.format(v)  # noqa: E731
    lines = [f"cycles: {s['closed_cycles']} closed ({s['aborted_cycles']} "
             f"aborted before the hedge), {s['open_cycles']} open "
             f"({s['provisional_cycles']} closed but provisional)"]
    if s["status"] == "NO_CLOSED_CYCLES":
        lines.append("no closed cycle: no performance figure exists yet")
    else:
        lines += [
            f"confirmed net: ${f(s['net_usd_confirmed'])}   provisional net "
            f"(not confirmed): ${f(s['net_usd_provisional'])}",
            f"funding ${f(s['funding_usd'])}  price leg ${f(s['price_leg_usd'])}  "
            f"= gross before costs ${f(s['gross_before_costs_usd'])}",
            f"fees ${f(s['fees_usd'])}  financing ${f(s['borrow_usd'])}  "
            f"(fees are {f(s['fees_pct_of_funding'], '{:.1f}')}% of funding)",
            f"peak notional ${f(s['peak_notional_usd'])}  return on it "
            f"{f(s['return_on_peak_notional_pct'])}%  annualised over "
            f"{f(s['days_in_market'], '{:.1f}')} days in market: "
            f"{f(s['annualised_on_notional_in_market_pct'])}%",
            f"max drawdown (closed cycles) ${f(s['max_drawdown_usd'])}  win "
            f"rate {f(100 * s['win_rate'], '{:.0f}')}%  best winner = "
            f"{f(s['best_cycle_share_of_winning_net_pct'], '{:.1f}')}% of "
            f"winning net  mean hold {f(s['mean_hold_days'], '{:.1f}')} days"]
    if s.get("period_return_on_peak_capital_pct") is not None:
        lines.append(
            f"committed capital (venue margin + spot leg), peak "
            f"${f(s['peak_capital_committed_usd'])}; period net / that peak "
            f"{f(s['period_return_on_peak_capital_pct'])}% over "
            f"{f(s['period_days'], '{:.1f}')} calendar days (peak margin "
            f"${f(s['margin_committed_usd'])}); {s['capital_basis']}")
    else:
        lines.append("committed capital: not available (no observed venue margin "
                     "for every closed cycle); no return on capital is shown")
    lines.append(s["margin_note"])
    lines.append(s["slippage_note"])
    lines.append("confirmed = every fee venue-stated and every funding entry "
                 "from a venue row; completeness against the public funding "
                 "prints is carry_report.py / evidence_reconcile.py")
    return lines


def evidence_lines(report: Mapping[str, Any]) -> List[str]:
    """The evidence header the text report leads with."""
    env = report["environment"]
    rc = report["sections"]["realized_cash"]
    lines = [f"environment: declared={env['declared'] or 'NONE'}  "
             f"recorded={env['recorded_seen'] or 'none'}  booked figures are "
             f"{env['booked_figures_class']}"]
    if rc["evidence_class"] == ACTUAL_RECONCILED:
        lines.append(f"realized cash (ACTUAL_RECONCILED, "
                     f"{rc['realized_cash_cycles']} cycle(s)): "
                     f"${rc['realized_cash_net_usd']:,.2f}")
    else:
        lines.append("realized cash: UNAVAILABLE -- " + "; ".join(rc["why"]))
        lines.append("NOTHING BELOW IS REALIZED CASH")
    if not report["invariants_hold"]:
        lines.append("LEDGER INVARIANT VIOLATED: " + ", ".join(
            i["name"] for i in report["invariants"] if not i["ok"]))
    return lines


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--db", required=True)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--environment", choices=ENVIRONMENTS, default=None,
                    help="whose orders this ledger booked; REQUIRED for any "
                         "figure to be classed as cash. Omitted: "
                         "UNDECLARED_ENVIRONMENT")
    ap.add_argument("--cash-apr", type=float, default=None,
                    help="operator-supplied benchmark rate, ANNUAL PERCENT "
                         "(4.5 = 4.5%%/yr). No default: omitted, the cash "
                         "comparison is UNAVAILABLE")
    ap.add_argument("--cash-apr-source", default="",
                    help="where the benchmark rate came from (recorded)")
    ap.add_argument("--now-ms", type=int, default=None)
    args = ap.parse_args(argv)
    try:
        report = build_report(args.db, environment=args.environment,
                              cash_apr_pct=args.cash_apr,
                              cash_apr_source=args.cash_apr_source,
                              now_ms=args.now_ms)
    except (FileNotFoundError, LookupError, sqlite3.Error, ValueError,
            L.LedgerError) as exc:
        print(f"refusing: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(report, indent=2, default=str, allow_nan=False))
    else:
        detail = report["sections"]["ledger_detail"]
        cycles = [dict(c, realised_usd=c["price_leg_usd"])
                  for c in detail["cycles"]]
        print("\n".join(evidence_lines(report)
                        + report_lines(cycles, detail["summary"])))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
