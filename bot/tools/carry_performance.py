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
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import ledger as L  # noqa: E402
import order_lifecycle as _order_lifecycle  # noqa: E402

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
#: The hedged pair valued with the client's BTC MARKED (never sold) between the
#: entry spot and the exit spot. It is economic performance, not cash: it can
#: never be a `realized_cash` figure and never satisfies a cash-only policy.
MARKED_PAIR_ECONOMICS = "MARKED_PAIR_ECONOMICS"
EVIDENCE_CLASSES = (ACTUAL_RECONCILED, OPEN_MARK_TO_MARKET, PAPER_EXECUTION,
                    HISTORICAL_SIMULATION, FORWARD_SIMULATION,
                    MARKED_PAIR_ECONOMICS, UNAVAILABLE)

#: How old (before the fill it values) an inventory mark may be. A mark is the
#: spot the engine saw at the decision tick, or one public read taken right
#: after the fill; anything older than two ticks is not "the price at the time".
MAX_MARK_AGE_MS = 120_000
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

#: `orders.status` values that are not terminal. The state machine owns the
#: set (order_lifecycle.UNRESOLVED); a hand-written copy here missed the
#: 'contradicted' state and let a contradicted fill sit under "realised cash".
_UNRESOLVED_ORDER_STATUSES = tuple(sorted(_order_lifecycle.UNRESOLVED))

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
    cur["perp_closed_qty"] = (sum(q for q, _ in closes)
                              if closes and not unread else None)
    cur["mark_status"], cur["mark_reasons"] = verify_marks(
        cur.pop("_marks"), cur.pop("_open_raw"), cur["perp_closed_qty"])
    cur["perp_fills"] = len(opens) + len(closes)
    cur["perp_entry_price"] = (sum(q * p for q, p in opens) / qty
                               if qty else None)
    cur["perp_exit_price"] = (
        sum(q * p for q, p in closes) / sum(q for q, _ in closes)
        if qty and closes and cur["outcome"] == "CLOSED" else None)
    cur["perp_entry_notional_usd"] = (
        sum(q * p for q, p in opens) if opens and not unread else None)
    cur["perp_exit_notional_usd"] = (
        sum(q * p for q, p in closes) if closes and not unread else None)
    cur["overlay"] = hedged and cur["spot_cost_usd"] <= 0
    cur["has_perp"] = hedged
    # The spot leg's state when the perp went flat. An ACQUIRE cycle is a hedged
    # PAIR only if its spot leg is flat and was the size of the hedge
    # (`_acquire_spot_leg_problem`): a perp closed while the spot sale was
    # refused, partial or never made leaves the BTC held, with its price move
    # unbooked.
    cur["spot_residual_qty"] = cur.get("spot_qty")
    for key in ("perp_closed", "notional_unknown", "first_open_ms",
                "last_close_ms", "perp_qty", "spot_qty", "spot_sold", "last_ms"):
        cur.pop(key, None)
    return cur


def _finite_pos(x: Any) -> bool:
    return (isinstance(x, (int, float)) and not isinstance(x, bool)
            and math.isfinite(x) and x > 0)


def verify_marks(marks: Sequence[Mapping[str, Any]],
                 opens: Sequence[Any],
                 closed_qty: Optional[float]) -> Tuple[str, List[str]]:
    """Is every inventory mark in this cycle a usable one? Returns
    ("NONE" | "VERIFIED" | "UNVERIFIED", reasons). Fails closed: a mark with no
    stated provenance, a label other than MARK_TO_MARKET, a non-finite or
    non-positive price, an amount that disagrees with its own prices, an age
    beyond `MAX_MARK_AGE_MS` (or observed after its fill), a repeated or
    conflicting entry, or marks that do not cover the quantity the perp closed
    is UNVERIFIED, and the cycle then has no pair economics at all."""
    if not marks:
        return "NONE", []
    why: List[str] = []
    seen: Dict[str, Mapping[str, Any]] = {}
    for m in marks:
        ref, raw = m["ref"], m["raw"]
        if ref in seen:
            same = all(raw.get(k) == seen[ref]["raw"].get(k)
                       for k in ("qty", "entry_spot", "exit_spot")) \
                and abs(m["usd"] - seen[ref]["usd"]) <= L.BALANCE_TOLERANCE
            why.append(f"{'DUPLICATE_MARK' if same else 'CONFLICTING_MARK'}: "
                       f"{ref} appears more than once")
            continue
        seen[ref] = m
    for ref, m in seen.items():
        raw = m["raw"]
        if raw.get("valuation") != L.MARK_VALUATION:
            why.append(f"UNVERIFIED_MARK: {ref} states no MARK_TO_MARKET "
                       "provenance (source, prices, observation time)")
            continue
        qty, p0, p1 = raw.get("qty"), raw.get("entry_spot"), raw.get("exit_spot")
        if not (_finite_pos(qty) and _finite_pos(p0) and _finite_pos(p1)):
            why.append(f"UNVERIFIED_MARK: {ref} has a non-finite or "
                       "non-positive quantity or price")
            continue
        if abs(qty * (p1 - p0) - m["usd"]) > max(L.BALANCE_TOLERANCE,
                                                 1e-9 * abs(m["usd"])):
            why.append(f"MARK_AMOUNT_INCONSISTENT: {ref} books {m['usd']} but "
                       f"its own prices give {qty * (p1 - p0)}")
        obs, fill = raw.get("exit_mark_observed_ms"), raw.get("fill_ms")
        if not (isinstance(obs, (int, float)) and isinstance(fill, (int, float))
                and not isinstance(obs, bool) and not isinstance(fill, bool)
                and math.isfinite(obs) and math.isfinite(fill)):
            why.append(f"UNVERIFIED_MARK: {ref} has no observation time")
        elif obs > fill:
            why.append(f"MARK_OBSERVED_AFTER_FILL: {ref} was observed "
                       f"{obs - fill:.0f} ms after the fill it values")
        elif fill - obs > MAX_MARK_AGE_MS:
            why.append(f"STALE_MARK: {ref} was observed {fill - obs:.0f} ms "
                       f"before the fill it values (limit {MAX_MARK_AGE_MS})")
    for ms, raw in opens:
        if "hedge_spot_ref" in raw and not _finite_pos(raw["hedge_spot_ref"]):
            why.append("UNVERIFIED_MARK: an open fill recorded no entry spot")
            break
        obs = raw.get("hedge_spot_ref_observed_ms")
        if obs is not None and (not isinstance(obs, (int, float))
                                or obs > ms or ms - obs > MAX_MARK_AGE_MS):
            why.append("STALE_MARK: an open fill's entry spot was not "
                       "observed within the allowed window before it")
            break
    covered = sum(float(m["raw"].get("qty") or 0.0) for m in seen.values()
                  if _finite_pos(m["raw"].get("qty")))
    if closed_qty is None or not _finite_pos(closed_qty):
        why.append("MARK_QTY_MISMATCH: the closed perp quantity is not "
                   "readable, so what the marks cover cannot be checked")
    elif abs(covered - closed_qty) > 1e-9 + 1e-6 * closed_qty:
        why.append(f"MARK_QTY_MISMATCH: marks cover {covered} BTC but the "
                   f"perp closed {closed_qty}")
    return ("UNVERIFIED", why) if why else ("VERIFIED", [])


def cycles_from_journal(journal: Any) -> List[Dict[str, Any]]:
    """One row per ledger cycle id, in time order. The journal assigns the id
    (and re-derives it for rows written before it existed), so this reader no
    longer has a boundary rule of its own."""
    by_id: Dict[str, Dict[str, Any]] = {}
    order: List[str] = []
    _pairs = L.supersession_pairs(journal.entries)
    _cancelled = set(_pairs) | set(_pairs.values())      # estimate and its reversal
    for _idx, e in enumerate(journal.entries):
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
                "revalue_usd": 0.0, "revalue_entries": 0, "fills": 0,
                "_marks": [], "_open_raw": [],
                "_opens": [], "_closes": [],
                "_fill_memo_unread": False,
                "funding_received_usd": 0.0, "funding_paid_usd": 0.0,
                "funding_entries": 0, "borrow_entries": 0,
                "spot_buy_gross_usd": 0.0, "spot_buy_net_qty": 0.0,
                "spot_sell_gross_usd": 0.0, "spot_sold_qty": 0.0,
                "fill_refs": []}
            order.append(e.cycle)
        cur["last_ms"] = e.ms
        for line in e.lines:
            if line.account == L.FUNDING and _idx in _cancelled:
                continue      # an estimate and its reversal: neither is funding that happened
            if line.account == L.FUNDING:
                cur["funding_usd"] += -line.usd
                cur["funding_entries"] += 1
                if -line.usd >= 0:
                    cur["funding_received_usd"] += -line.usd
                else:
                    cur["funding_paid_usd"] += line.usd
            elif line.account == L.FEES:
                cur["fees_usd"] += line.usd
            elif line.account == L.BORROW:
                cur["borrow_usd"] += line.usd
                cur["borrow_entries"] += 1
            elif line.account == L.REALISED:
                # A revaluation posts here too, but it is a MARK of coin that
                # was never sold: it is accumulated apart (`revalue_usd`) and
                # is not part of any cash figure.
                if e.kind != "revalue":
                    cur["realised_usd"] += -line.usd
            elif line.account == L.PERP:
                cur["perp_qty"] += line.qty
            elif line.account == L.BTC and e.kind in ("spot_buy", "spot_sell"):
                cur["spot_qty"] += line.qty
        if e.kind in L.FILL_KINDS:
            cur["fills"] += 1
            cur["fill_refs"].append(e.ref)
        if e.kind == "spot_buy" and e.lines:
            cur["spot_buy_gross_usd"] += e.lines[0].usd
            cur["spot_buy_net_qty"] += sum(l.qty for l in e.lines
                                           if l.account == L.BTC)
        if e.kind == "spot_sell" and len(e.lines) > 1:
            cur["spot_sell_gross_usd"] += e.lines[0].usd
            cur["spot_sold_qty"] += -e.lines[1].qty
        if e.kind == "perp_open":
            cur["_open_raw"].append((e.ms, e.fee_raw or {}))
        if e.kind in ("perp_open", "perp_close"):
            fill = _memo_fill(e)
            if fill is None:
                cur["_fill_memo_unread"] = True
            else:
                cur["_opens" if e.kind == "perp_open" else "_closes"].append(fill)
        if e.kind == "revalue":
            usd = -sum(l.usd for l in e.lines if l.account == L.REALISED)
            cur["revalue_usd"] += usd
            cur["revalue_entries"] += 1
            cur["_marks"].append({"ref": e.ref, "ms": e.ms, "usd": usd,
                                  "raw": e.fee_raw or {}})
        if e.kind == "spot_sell":
            cur["spot_sold"] = True
        if e.fee_status:
            cur["_fee_statuses"].append(e.fee_status)
        if L.funding_basis(e) and _idx not in _pairs:
            cur["_bases"].append(L.funding_basis(e))
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
    out = [_finish(by_id[c]) for c in order]
    for c in out:
        c["economic_net_usd"], c["economic_net_basis"] = _economic_net_of(c)
    return out


def _economic_net_of(c: Mapping[str, Any]) -> Tuple[Optional[float], str]:
    """THE economic net of ONE closed cycle (and how it was reached), or None.

    The booked `net_usd` is CASH. It is the economic result only when both
    price legs are booked: an aborted cycle (its whole round trip is the loss),
    an ACQUIRE cycle whose spot leg is flat and the size of the hedge, or an
    OVERLAY whose never-sold BTC is booked with VERIFIED marks. An overlay whose
    inventory leg is only DERIVED from perp prices has its basis counted as zero
    (not an economic result), and any cycle whose pair cannot be computed has
    none: every return, win rate, drawdown and verdict is taken on this figure,
    so a figure that is not measured can never be the base of a return."""
    if not c.get("closed"):
        return None, "OPEN"
    if c.get("outcome") != "CLOSED":
        return c.get("net_usd"), "ABORTED_CASH_IS_THE_LOSS"
    pair = hedged_pair(c)
    if not pair["available"]:
        return None, "PAIR_NOT_COMPUTABLE"
    if pair.get("derived"):
        return None, "DERIVED_NOT_BOOKED"
    return pair["net_usd"], pair["inventory_leg"]


#: How far an acquire cycle's spot leg may differ from its hedge and still be the
#: hedge: the engine's own DELTA_BAND (carry_engine.DELTA_BAND, pinned equal by a
#: test), measured the engine's way, `CarryPosition.delta_fraction`: |spot qty -
#: perp qty| as a fraction of BOTH legs (about 3.9% of the larger one at the band),
#: because the hedge is the perp short snapped to the lot, which can sit up to about
#: a lot under the BTC actually held. A pair the engine itself accepts is therefore
#: never withheld here.
ACQUIRE_HEDGE_TOLERANCE = 0.02

#: What an unsold spot remainder may be worth and still leave a cycle a flat pair:
#: the sell is floored to the venue's lot (`_spot_sellable`), so a clean close
#: leaves sub-lot dust (a 1e-6 BTC lot is about $0.10 at $100k; the red team's
#: two fixtures left 5.4e-7 and 8e-7 BTC, about $0.05) whose price move is worth
#: cents against tens of dollars of fees. The dust is STATED in the pair
#: (`spot_residual_btc`), never hidden, and a remainder above this value is a leg
#: whose price move is not booked.
SPOT_DUST_USD = 1.0


def _spot_dust_qty(c: Mapping[str, Any]) -> float:
    """The largest unsold/oversold BTC quantity treated as dust for this cycle:
    SPOT_DUST_USD at the cycle's entry price, never below the ledger's own dust.
    No readable price: the ledger's strict dust only."""
    floor = max(L.DUST[L.BTC], 1e-9)
    for price in (c.get("perp_entry_price"),
                  (c.get("spot_buy_gross_usd") or 0.0)
                  / c["spot_buy_net_qty"] if c.get("spot_buy_net_qty") else None):
        if (isinstance(price, (int, float)) and not isinstance(price, bool)
                and math.isfinite(price) and price > 0):
            return max(floor, SPOT_DUST_USD / price)
    return floor


def _acquire_spot_leg_problem(c: Mapping[str, Any]) -> Optional[str]:
    """Why an ACQUIRE cycle is not (provably) a flat hedged pair, or None.

    Its booked net holds both price legs only if the spot leg was sold back
    (nothing left beyond lot dust worth `SPOT_DUST_USD`) and was the size of the
    perp short. Unknown stays unknown: a quantity the journal cannot state is a
    problem, never a pass."""
    residual = c.get("spot_residual_qty")
    bought = c.get("spot_buy_net_qty")
    hedged = c.get("hedged_qty")
    if not all(isinstance(x, (int, float)) and not isinstance(x, bool)
               and math.isfinite(x) for x in (residual, bought, hedged)):
        return ("the spot or perp quantity of this cycle is not readable from "
                "the journal, so a flat hedged pair cannot be shown")
    if abs(residual) > _spot_dust_qty(c):
        return (f"{residual:+.8f} BTC of the spot leg was never sold back: its "
                "price move is not booked, so the booked net is not the "
                "pair's")
    if hedged <= 0 or bought <= 0:
        return "no hedged quantity or no spot purchase to compare"
    if abs(bought - hedged) > ACQUIRE_HEDGE_TOLERANCE * (bought + hedged) * (1 + 1e-9):
        return (f"the spot leg ({bought:.8f} BTC bought) was not the size of "
                f"the perp short ({hedged:.8f} BTC) within the engine's delta "
                "band: a directional position wearing a hedge is not a "
                "delta-neutral pair")
    return None


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
        # on the ECONOMIC net (None unless the pair is measured): the cash net
        # of an unhedged short follows BTC and is not a return on anything
        c["return_on_committed_capital_pct"] = (
            100.0 * c["economic_net_usd"] / c["capital_committed_usd"]
            if c["closed"] and c["outcome"] == "CLOSED"
            and c.get("economic_net_usd") is not None
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
                    "mean_hold_days", "best_cycle_share_of_winning_net_pct",
                    "economic_net_usd", "returns_withheld_why"):
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
    # EVERY RETURN-TYPE FIGURE BELOW IS TAKEN ON THE ECONOMIC NET, never on the
    # cash `net_usd`. For an overlay the booked cash is the net of an UNHEDGED
    # short (its sign follows BTC); for an acquire cycle that closed its perp
    # with the spot leg unsold or the wrong size it omits the BTC's move. Each
    # cycle's `economic_net_usd` is None unless its pair is measured
    # (`_economic_net_of`); if ANY closed cycle has none, every return, win rate,
    # drawdown and concentration is withheld (None) and the reason is stated,
    # never replaced by the cash figure.
    econ = [c.get("economic_net_usd") for c in closed]
    measured = all(x is not None for x in econ)
    econ_total = sum(econ) if measured else None
    s["economic_net_usd"] = econ_total
    s["returns_withheld_why"] = None if measured else (
        "NO_RETURN_OR_STATISTIC: " + "; ".join(sorted({
            f"{c['cycle_id']}: {c.get('economic_net_basis')}"
            for c in closed if c.get("economic_net_usd") is None}))
        + " (the booked cash net of such a cycle is not an economic result)")
    s["return_on_peak_notional_pct"] = (
        100.0 * econ_total / peak if measured and peak else None)
    days = sum(c["hold_days"] or 0.0 for c in closed)
    s["days_in_market"] = days
    import performance_analytics as PA
    ann, ann_why = PA.annualise_simple(
        100.0 * econ_total / peak if measured and peak else None, days,
        len(closed))
    s["annualised_on_notional_in_market_pct"] = ann
    s["annualised_refusal_reason"] = ann_why or (
        None if measured else s["returns_withheld_why"])
    s["annualisation_rule"] = (
        f">= {PA.MIN_ANNUALISE_CYCLES} closed cycles and >= "
        f"{PA.MIN_ANNUALISE_DAYS:g} days in market, else None")
    # A cost the venue did not state is booked as 0 and flagged; the totals
    # above are then a LOWER BOUND on cost / UPPER BOUND on net, never final.
    s["cycles_with_unstated_fees"] = sum(
        1 for c in closed if c.get("fees_unconfirmed_fills"))
    s["cycles_with_unverified_funding"] = sum(
        1 for c in closed if c.get("funding_unverified_entries"))
    s["totals_are_bounds"] = bool(s["cycles_with_unstated_fees"]
                                  or s["cycles_with_unverified_funding"])
    cum = peak_cum = drawdown = 0.0
    if measured:
        for c in sorted(closed, key=lambda c: c["start_ms"]):
            cum += c["economic_net_usd"]
            peak_cum = max(peak_cum, cum)
            drawdown = max(drawdown, peak_cum - cum)
    s["max_drawdown_usd"] = drawdown if measured else None
    s["open_cycle_booked_costs_usd"] = sum(
        c["fees_usd"] + c["borrow_usd"] for c in cycles if not c["closed"])
    s["win_rate"] = (sum(1 for x in econ if x > 0) / len(closed)
                     if measured else None)
    wins = (sorted((x for x in econ if x > 0), reverse=True)
            if measured else [])
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
        s["period_return_on_peak_capital_pct"] = (
            100.0 * econ_total / max(caps) if measured else None)
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


def load_carry_order_fills(db_path: str) -> Optional[List[Dict[str, Any]]]:
    """(link, status, filled_qty) of every carry-leg order, or None when the
    database has no orders table."""
    conn = _ro(db_path)
    try:
        rows = conn.execute(
            "SELECT order_link_id, status, filled_qty FROM orders "
            "WHERE purpose LIKE 'carry-%'").fetchall()
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    return [{"link": str(r[0]), "status": str(r[1]),
             "filled_qty": float(r[2] or 0.0)} for r in rows]


def order_fills_match_ledger(journal: Any,
                             orders: Optional[Sequence[Mapping[str, Any]]]
                             ) -> Dict[str, Any]:
    """Invariant: for every finished carry order, the quantity the order record
    holds equals the quantity the ledger booked under its link id. A fill the
    order record has and the ledger lacks (or the reverse) means the cash in the
    report rests on a quantity nobody reconciled, however the record got there
    (a resolved contradiction included)."""
    name = "carry_order_fills_equal_ledger_fills"
    if orders is None:
        return {"name": name, "ok": True,
                "detail": {"skipped": "no orders table to compare with"}}
    booked: Dict[str, float] = {}
    for e in journal.entries:
        if e.kind not in L.FILL_KINDS:
            continue
        spq = _fill_price_qty(e)
        if spq is not None:
            booked[e.ref] = booked.get(e.ref, 0.0) + float(spq[1])
    mismatched = []
    for o in orders:
        if o["status"] not in _order_lifecycle.TERMINAL:
            continue            # in flight: the unresolved-orders blocker speaks
        want, have = float(o["filled_qty"]), booked.get(o["link"], 0.0)
        if abs(want - have) > max(1e-9, 1e-6 * max(want, have)):
            mismatched.append({"order_link_id": o["link"],
                               "order_filled_qty": want,
                               "ledger_filled_qty": have})
    return {"name": name, "ok": not mismatched,
            "detail": {"mismatched": mismatched}}


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
            elif line.account == L.REALISED and e.kind != "revalue":
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
    if (c["closed"] and c.get("outcome") == "CLOSED" and not c.get("overlay")
            and c.get("economic_net_usd") is None):
        # An ACQUIRE cycle whose pair cannot be computed (its spot leg unsold,
        # partly sold or not hedge-sized, or a perp price unreadable) is not a
        # completed round trip: its booked cash is real, but it is the cash of a
        # position that is still partly open, not a RESULT. Without this the
        # report's cash section and `carry_evidence` booked such a cycle as a
        # reconciled trade (+$4,785 where the pair lost $160).
        reasons.append("PAIR_NOT_COMPUTABLE: " + str(
            hedged_pair(c).get("why") or "no hedged-pair net for this cycle")
            + "; its booked cash is not a result")
    reasons += list(blockers)
    c["provisional_reasons"] = reasons
    c["evidence_class"] = (ACTUAL_RECONCILED
                           if env["actual_reconciled_eligible"] and not reasons
                           else env["booked_figures_class"])


def _pair_fields(c: Mapping[str, Any], complete: bool) -> Dict[str, Any]:
    """The marked-pair view of a cycle, kept apart from its cash fields. The
    pair is only offered for a complete (fees and funding confirmed) closed
    cycle with verified marks."""
    pr = hedged_pair(c) if c.get("closed") else {"available": False,
                                                 "why": "cycle still open"}
    ok = bool(pr["available"] and complete and pr.get("marked"))
    return {
        "inventory_mark_usd": c.get("revalue_usd") if c.get(
            "revalue_entries") else None,
        "pair_net_usd": pr["net_usd"] if ok else None,
        "pair_economics_class": MARKED_PAIR_ECONOMICS if ok else None,
        "pair_unavailable_reasons": (
            [] if ok else
            ([pr["why"]] if not pr["available"] else
             ([] if not c.get("revalue_entries") else
              ["cash figures incomplete: fees or funding not confirmed"]))),
    }


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
        "economic_net_usd": c.get("economic_net_usd"),
        "economic_net_basis": c.get("economic_net_basis"),
        **_pair_fields(c, fees_ok and fund_ok),
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


# -- net economics: every component a number with its evidence, or UNAVAILABLE --

NET_ECONOMICS_SCHEMA = "net_economics/1"

#: Components that ADD UP to a cycle's net. Everything else in a cycle's
#: ``components`` is a memo line (``additive: False``): shown for diagnosis,
#: never summed into net, because its cost is already inside an additive line
#: (slippage is inside the price leg; failed-leg cost is inside the aborted
#: cycle's net; a counterfactual is not a cost at all).
ADDITIVE_COMPONENTS = ("funding_net", "fees", "financing", "price_leg")


def _comp(value: Optional[float], evidence_class: str, *, unit: str = "usd",
          status: str = "BOOKED", why: Optional[str] = None,
          additive: bool = False, sign: str = "", **extra: Any) -> Dict[str, Any]:
    """One economics line. A missing number is None AND says why: it is never
    0 and never omitted."""
    if value is None:
        evidence_class = UNAVAILABLE
        status = status if status != "BOOKED" else "UNAVAILABLE"
        if not why:
            raise ReportIntegrityError("an UNAVAILABLE component must say why")
    out = {"value": value, "unit": unit, "evidence_class": evidence_class,
           "status": status, "additive": additive, "sign": sign}
    if why:
        out["why"] = why
    out.update(extra)
    return out


def slippage_by_cycle(journal: Any) -> Dict[str, Dict[str, Any]]:
    """Per cycle: fills, fills with a decision mark, and the measured slippage
    (positive = adverse). Same arithmetic as `slippage_section`."""
    out: Dict[str, Dict[str, Any]] = {}
    for e in journal.entries:
        if e.kind not in L.FILL_KINDS or not e.cycle:
            continue
        cur = out.setdefault(e.cycle, {"fills": 0, "measured": 0, "usd": 0.0})
        cur["fills"] += 1
        spq = _fill_price_qty(e)
        if e.decision_mark is None or spq is None:
            continue
        side, qty, price = spq
        cur["measured"] += 1
        cur["usd"] += ((price - e.decision_mark) if side == "Buy"
                       else (e.decision_mark - price)) * qty
    return out


def hedged_pair(c: Mapping[str, Any]) -> Dict[str, Any]:
    """The net of the hedged PAIR for one closed cycle, and how it was reached.

    The booked cycle net is funding - fees - financing + the price legs the
    journal holds. For an ACQUIRE cycle both price legs are booked (spot cost to
    proceeds, perp entry to exit), so the booked net is the pair's. For an
    OVERLAY the live journal books the short's price leg and never revalues the
    client's BTC that it hedges, so the booked net is the net of an UNHEDGED
    SHORT: a rally reads as a large loss and a fall as a large win, neither of
    which is carry.

    * ``SPOT_LEG_BOOKED``  acquire: the booked net already holds both legs.
    * ``BOOKED_MARK``      overlay with VERIFIED revaluation entries: the
                           pair is the cash net PLUS the marked (never sold)
                           inventory move. It is MARKED_PAIR_ECONOMICS, not
                           cash; unverified marks give no pair at all.
    * ``DERIVED_FROM_PERP_FILL_PRICES``  overlay, inventory not booked: the
                           leg is hedged_qty x (perp exit - perp entry), the
                           perp fill prices standing in for the BTC price. That
                           makes the price legs cancel BY CONSTRUCTION, so the
                           basis P&L (entry basis - exit basis) is NOT measured
                           and is reported UNAVAILABLE, never as zero.

    Nothing is invented: an overlay cycle without a readable hedged quantity
    and both perp prices has no pair net (available False).
    """
    if not c.get("closed"):
        return {"available": False, "why": "cycle still open"}
    net = c.get("net_usd")
    if c.get("outcome") == "ABORTED_BEFORE_HEDGE":
        return {"available": True, "net_usd": net,
                "inventory_leg": "NOT_APPLICABLE", "inventory_leg_usd": None,
                "derived": False, "basis_pnl_usd": None,
                "basis_why": "the hedge never landed; the whole round trip "
                             "is the cycle's loss"}
    if not c.get("overlay"):
        problem = _acquire_spot_leg_problem(c)
        if problem:
            return {"available": False, "why": "SPOT_LEG_NOT_FLAT: " + problem}
        return {"available": True, "net_usd": net,
                "inventory_leg": "SPOT_LEG_BOOKED", "inventory_leg_usd": None,
                "derived": False, "basis_pnl_usd": c["realised_usd"],
                "basis_why": None,
                # unsold lot dust (worth at most SPOT_DUST_USD): stated, not hidden
                "spot_residual_btc": c.get("spot_residual_qty")}
    if c.get("revalue_entries"):
        if c.get("mark_status") != "VERIFIED":
            return {"available": False, "why": (
                "UNVERIFIED_INVENTORY_MARK: " + "; ".join(
                    c.get("mark_reasons") or ["no usable mark"]))}
        # `net` is CASH (funding - fees - financing + the perp's realised
        # result); the pair adds the MARKED, never-sold inventory move.
        return {"available": True, "net_usd": net + c["revalue_usd"],
                "inventory_leg": "BOOKED_MARK",
                "inventory_leg_usd": c["revalue_usd"], "derived": False,
                "marked": True, "basis_pnl_usd": c["realised_usd"]
                + c["revalue_usd"], "basis_why": None}
    qty, p0, p1 = (c.get("hedged_qty"), c.get("perp_entry_price"),
                   c.get("perp_exit_price"))
    if not (qty and p0 and p1 and qty > 0 and p0 > 0 and p1 > 0
            and all(math.isfinite(x) for x in (qty, p0, p1))):
        return {"available": False, "why": (
            "overlay inventory leg not booked and the hedged quantity or a "
            "perp fill price is not in the journal for this cycle")}
    leg = qty * (p1 - p0)
    return {"available": True, "net_usd": net + leg,
            "inventory_leg": "DERIVED_FROM_PERP_FILL_PRICES",
            "inventory_leg_usd": leg, "derived": True, "basis_pnl_usd": None,
            "basis_why": (
                "BASIS_NOT_MEASURABLE: the inventory leg is derived from the "
                "perp fill prices, so basis (perp vs spot) at entry and exit "
                "never enters; the journal holds no spot price for this cycle")}


def cycle_net_economics(c: Mapping[str, Any],
                        slip: Optional[Mapping[str, Any]] = None,
                        opp: Optional[Mapping[str, Any]] = None
                        ) -> Dict[str, Any]:
    """The complete per-cycle economics of a classified cycle."""
    cls = c.get("evidence_class", UNAVAILABLE)
    closed = bool(c["closed"])
    fees_ok = c.get("fees_complete", c.get("fees_unconfirmed_fills", 0) == 0)
    fund_ok = c.get("funding_complete", c.get("funding_unverified_entries", 0) == 0)
    bad_cls = cls if cls != UNAVAILABLE else UNAVAILABLE
    comp: Dict[str, Any] = {}
    overlay = bool(c.get("overlay"))

    def principal(name, value, why_none):
        comp[name] = _comp(value, cls, status="BOOKED", additive=False,
                           why=why_none, note="gross principal flow; "
                           "its gain/loss is the price leg, not added again")

    principal("spot_buy", c["spot_buy_gross_usd"] if c["spot_buy_gross_usd"]
              else None, "no spot purchase in this cycle (overlay hedges the "
              "client's own BTC, or the cycle never bought)")
    principal("spot_sell", c["spot_sell_gross_usd"] if c["spot_sold_qty"]
              else None, "no spot sale in this cycle")
    principal("perp_entry", c.get("perp_entry_notional_usd"),
              "no perp entry, or a perp fill memo could not be read")
    principal("perp_exit", c.get("perp_exit_notional_usd"),
              "the perp leg has not been closed, or a fill memo was unreadable")

    comp["funding_received"] = _comp(
        c["funding_received_usd"], cls, additive=False, sign="income",
        status="BOOKED" if fund_ok else "UNVERIFIED",
        note="sum of positive funding prints (venue paid the book)")
    comp["funding_paid"] = _comp(
        c["funding_paid_usd"], cls, additive=False, sign="cost",
        status="BOOKED" if fund_ok else "UNVERIFIED",
        note="sum of negative funding prints (book paid the venue)")
    comp["funding_net"] = _comp(
        c["funding_usd"], cls, additive=True, sign="income",
        status="BOOKED" if fund_ok else "UNVERIFIED",
        note=None if fund_ok else "includes entries that are estimated, "
        "unattributed (booked $0) or unread")
    comp["maker_fee"] = _comp(None, UNAVAILABLE, why=(
        "MAKER_TAKER_NOT_JOURNALLED: a ledger entry records the fee, not the "
        "execution type, so the fee cannot be split into maker and taker"))
    comp["taker_fee"] = comp["maker_fee"]
    comp["fees"] = _comp(
        c["fees_usd"], cls, additive=True, sign="cost",
        status="BOOKED" if fees_ok else "INCOMPLETE_LOWER_BOUND",
        note=None if fees_ok else (
            f"{c.get('fees_unconfirmed_fills')} fill(s) with a fee the venue "
            "did not state were booked at $0: the true fee is at least this"))
    comp["financing"] = _comp(
        c["borrow_usd"], cls, additive=True, sign="cost",
        status="BOOKED" if c["borrow_entries"] else "NONE_BOOKED",
        note=None if c["borrow_entries"] else (
            "no borrow/financing entry was booked; that is not proof that "
            "none accrued"))
    comp["price_leg"] = _comp(c["realised_usd"], cls, additive=True,
                              sign="income_or_cost")

    pr = hedged_pair(c)
    if pr["available"]:
        comp["hedged_pair_net"] = _comp(
            pr["net_usd"], HISTORICAL_SIMULATION if pr["derived"] else
            (MARKED_PAIR_ECONOMICS if pr.get("marked") else cls),
            status="DERIVED_NOT_BOOKED" if pr["derived"] else
            ("MARKED" if pr.get("marked") else "BOOKED"),
            additive=False, sign="income_or_cost",
            inventory_leg=pr["inventory_leg"],
            inventory_leg_usd=pr["inventory_leg_usd"],
            note="cash net plus the inventory leg: MARKED (client BTC not "
                 "sold) when verified marks exist, else derived from perp "
                 "fill prices; never cash and NOT added to the additive "
                 "lines (net_after_booked_costs_usd is cash and unchanged)")
        if pr["basis_pnl_usd"] is not None:
            comp["basis_pnl"] = _comp(
                pr["basis_pnl_usd"],
                MARKED_PAIR_ECONOMICS if pr.get("marked") else cls,
                additive=False,
                sign="income_or_cost", note="both price legs booked: what is "
                "left after the hedge cancels, slippage included")
        else:
            comp["basis_pnl"] = _comp(None, UNAVAILABLE, why=(
                pr["basis_why"] or "no basis figure for this cycle"))
    else:
        comp["hedged_pair_net"] = _comp(None, UNAVAILABLE, why=pr["why"])
        comp["basis_pnl"] = _comp(None, UNAVAILABLE, why=pr["why"])

    s = slip or {}
    if not s.get("fills"):
        comp["slippage"] = _comp(None, UNAVAILABLE, why="no fills in this cycle")
    elif s["measured"] < s["fills"]:
        comp["slippage"] = _comp(
            None, UNAVAILABLE, why=(
                f"DECISION_MARK_NOT_JOURNALLED: {s['fills'] - s['measured']} of "
                f"{s['fills']} fills carry no decision mark; unknown, not zero"),
            measured_subset_usd=s["usd"])
    else:
        comp["slippage"] = _comp(
            s["usd"], cls, sign="cost", additive=False,
            embedded_in="price_leg",
            note="fill price vs decision-time mark; ALREADY inside price_leg "
                 "(fills are booked at the actual price) and is not part of "
                 "fees; never subtract it from net again")
    comp["spread"] = _comp(None, UNAVAILABLE, why=(
        "BOOK_TOP_NOT_JOURNALLED: bid/ask at decision time is not recorded; "
        "spread paid is unknown, not zero"))

    if c["outcome"] == "ABORTED_BEFORE_HEDGE":
        comp["failed_leg_cost"] = _comp(
            -c["net_usd"], cls, sign="cost", additive=False,
            embedded_in="cycle net", note="spot bought, perp never landed, "
            "spot sold flat: the whole round trip is this cycle's loss")
    elif closed:
        comp["failed_leg_cost"] = _comp(
            0.0, cls, status="NOT_APPLICABLE", additive=False,
            note="the cycle completed its hedge; an order that never filled "
                 "is counted at book level (execution_quality)")
    else:
        comp["failed_leg_cost"] = _comp(None, UNAVAILABLE,
                                        why="cycle still open")
    comp["partial_execution"] = _comp(None, UNAVAILABLE, why=(
        "the ledger books fills only; requested-vs-filled quantity lives in "
        "the order record and is reported at book level (execution_quality)"))

    # hedge imbalance: spot actually held vs the perp it was hedged with
    hq = c.get("hedged_qty")
    if overlay:
        comp["hedge_imbalance"] = _comp(None, UNAVAILABLE, status="NOT_APPLICABLE",
                                        why="overlay: the client's BTC is the "
                                        "spot leg, no purchase to compare")
    elif hq is None or not c["spot_buy_net_qty"]:
        comp["hedge_imbalance"] = _comp(None, UNAVAILABLE, why=(
            "no spot purchase or no readable perp quantity in this cycle"))
    else:
        diff = c["spot_buy_net_qty"] - hq
        px = c.get("perp_entry_price") or 0.0
        comp["hedge_imbalance"] = _comp(
            diff, cls, unit="btc", sign="exposure", additive=False,
            usd_at_entry=diff * px,
            note="spot BTC held (net of BTC-denominated fee) minus perp qty; "
                 "positive = long residual, negative = short residual")

    cap, days = c.get("capital_committed_usd"), c.get("hold_days")
    comp["capital_reservation"] = _comp(None, UNAVAILABLE, why=(
        "reservations are not keyed to a cycle; see execution_quality / "
        "capital_reservations at book level"))
    if cap and days:
        comp["capital_occupation"] = _comp(cap * days, cls, unit="usd_days",
                                           capital_usd=cap, hold_days=days)
    else:
        comp["capital_occupation"] = _comp(None, UNAVAILABLE, unit="usd_days",
                                           why="no committed capital or no "
                                           "holding period for this cycle")
    o = (opp or {}).get("vs_unhedged_btc") or {}
    if o.get("available"):
        comp["btc_opportunity_cost"] = _comp(
            o["unhedged_btc_pnl_usd"], HISTORICAL_SIMULATION,
            status="COUNTERFACTUAL", additive=False,
            note="what the same BTC would have made unhedged over the "
                 "holding period; a counterfactual, not a booked cost")
    else:
        comp["btc_opportunity_cost"] = _comp(None, UNAVAILABLE, why=(
            o.get("why") or "no opportunity-cost computation for this cycle"))
    r = (opp or {}).get("vs_benchmark_rate") or {}
    if r.get("available"):
        comp["benchmark_rate_alternative"] = _comp(
            r["benchmark_alternative_usd"], HISTORICAL_SIMULATION,
            status="COUNTERFACTUAL", additive=False,
            benchmark_apr_pct=r.get("benchmark_apr_pct"))
    else:
        comp["benchmark_rate_alternative"] = _comp(None, UNAVAILABLE, why=(
            r.get("why") or "no benchmark computation for this cycle"))
    comp["conversion_cost"] = _comp(None, UNAVAILABLE, why=(
        "USDT<->BTC conversions are not journalled as their own entries; a "
        "BTC-denominated spot fee is booked at the fill price"))

    additive_sum = (comp["funding_net"]["value"] - comp["fees"]["value"]
                    - comp["financing"]["value"] + comp["price_leg"]["value"])
    complete = bool(fees_ok and fund_ok)
    return {"cycle_id": c["cycle_id"], "outcome": c["outcome"],
            "evidence_class": cls, "closed": closed,
            "complete": complete,
            "net_after_booked_costs_usd": c["net_usd"],
            "additive_components_sum_usd": additive_sum,
            "components": comp}


def check_net_components(cycles: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    """Invariant: per cycle, funding - fees - financing + price leg equals the
    net the cycle reports, and funding received - paid equals funding net.
    A cost counted twice (or dropped) shows up as a difference here."""
    bad = []
    for c in cycles:
        e = (c["funding_usd"] - c["fees_usd"] - c["borrow_usd"]
             + c["realised_usd"])
        f = c["funding_received_usd"] - c["funding_paid_usd"]
        if (abs(e - c["net_usd"]) > L.BALANCE_TOLERANCE
                or abs(f - c["funding_usd"]) > L.BALANCE_TOLERANCE):
            bad.append({"cycle_id": c["cycle_id"], "components_sum_usd": e,
                        "net_usd": c["net_usd"], "funding_split_usd": f,
                        "funding_usd": c["funding_usd"]})
    return {"name": "net_components_sum_to_cycle_net", "ok": not bad,
            "detail": {"mismatched": bad}}


def load_carry_order_execution(db_path: str) -> Optional[List[Dict[str, Any]]]:
    conn = _ro(db_path)
    try:
        rows = conn.execute(
            "SELECT order_link_id, status, qty, filled_qty FROM orders "
            "WHERE purpose LIKE 'carry-%'").fetchall()
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    return [{"link": str(r[0]), "status": str(r[1]), "qty": float(r[2] or 0.0),
             "filled_qty": float(r[3] or 0.0)} for r in rows]


def load_capital_reservations(db_path: str) -> Optional[List[Dict[str, Any]]]:
    conn = _ro(db_path)
    try:
        rows = conn.execute(
            "SELECT intent_id, state, amount_usd, created_epoch, updated_epoch "
            "FROM capital_reservation").fetchall()
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    return [{"intent_id": str(r[0]), "state": str(r[1]),
             "amount_usd": float(r[2] or 0.0), "created": float(r[3] or 0.0),
             "updated": float(r[4] or 0.0)} for r in rows]


def execution_quality(orders: Optional[Sequence[Mapping[str, Any]]],
                      slip_total: Mapping[str, Any]) -> Dict[str, Any]:
    """Book-level partial / abandoned order counts and expected-vs-actual."""
    if orders is None:
        return {"orders": _comp(None, UNAVAILABLE,
                                why="no orders table in this database")}
    term = [o for o in orders if o["status"] in _order_lifecycle.TERMINAL]
    partial = [o for o in term if 0 < o["filled_qty"] < o["qty"]
               and abs(o["qty"] - o["filled_qty"]) > max(1e-9, 1e-6 * o["qty"])]
    unfilled = [o for o in term if o["filled_qty"] <= 0]
    return {
        "carry_orders": len(orders), "terminal_orders": len(term),
        "partially_filled_orders": len(partial),
        "unfilled_terminal_orders": len(unfilled),
        "unfilled_qty_total": sum(o["qty"] - o["filled_qty"] for o in term
                                  if o["qty"] > o["filled_qty"]),
        "cost_of_partial_or_abandoned_orders": _comp(None, UNAVAILABLE, why=(
            "a leg that filled partially or not at all leaves unhedged "
            "exposure whose price move is not journalled; count only")),
        "slippage_expected_vs_actual": slip_total}


def reservation_stats(rows: Optional[Sequence[Mapping[str, Any]]],
                      now_ms: int, cls: str) -> Dict[str, Any]:
    if rows is None:
        return _comp(None, UNAVAILABLE, why="no capital_reservation table")
    if not rows:
        return _comp(None, UNAVAILABLE, why="no capital reservations recorded")
    now_s = now_ms / 1000.0
    durs: List[float] = []
    usd_days, still = 0.0, 0
    for r in rows:
        end = r["updated"]
        if r["state"] == "RESERVED":
            end, still = now_s, still + 1
        d = max(0.0, end - r["created"])
        durs.append(d)
        usd_days += r["amount_usd"] * d / 86400.0
    return _comp(sum(durs) / len(durs) / 3600.0, cls, unit="hours_mean",
                 reservations=len(rows), still_reserved=still,
                 max_hours=max(durs) / 3600.0,
                 reserved_capital_usd_days=usd_days,
                 note="duration from created to last update (still-RESERVED "
                      "rows to now); a reservation is an intent, not a fill")


def _metric(value: Optional[float], evidence_class: str, definition: str, *,
            unit: str = "usd", why: Optional[str] = None,
            status: str = "COMPUTED", **extra: Any) -> Dict[str, Any]:
    if value is None:
        evidence_class, status = UNAVAILABLE, "UNAVAILABLE"
        if not why:
            raise ReportIntegrityError("an UNAVAILABLE metric must say why")
    out = {"value": value, "unit": unit, "evidence_class": evidence_class,
           "status": status, "definition": definition}
    if why:
        out["why"] = why
    out.update(extra)
    return out


def _agg_class(cycles: Sequence[Mapping[str, Any]], default: str) -> str:
    if cycles and all(c["evidence_class"] == ACTUAL_RECONCILED for c in cycles):
        return ACTUAL_RECONCILED
    return default


#: Shocks the sensitivity applies. Stated here so a reader can see exactly what
#: was assumed; they are not forecasts and not a measurement.
SENSITIVITY_EXTRA_BPS = (0.0, 2.0, 5.0, 10.0, 20.0)
SENSITIVITY_FEE_MULTIPLES = (1.0, 1.5, 2.0, 3.0)
SETTLEMENTS_PER_DAY = 3

PAIR_METRIC_KEYS = (
    "hedged_pair_net_usd", "observed_funding_bps_per_settlement",
    "break_even_funding_bps_per_settlement",
    "funding_margin_over_break_even_bps", "min_holding_days_to_cover_costs",
    "min_required_funding_edge_bps_per_settlement", "net_bps_per_notional_day",
    "pair_return_on_posted_margin_pct", "execution_cost_sensitivity")


def pair_economics(closed: Sequence[Mapping[str, Any]], cls_closed: str,
                   inc_status: str) -> Dict[str, Any]:
    """The hedged-pair net, break-evens, required edge, efficiency and the
    sensitivity of the result to execution cost. Additive: nothing here changes
    a booked figure, and every metric is None with a reason where an input is
    missing (never 0).

    DEFINITIONS (notional = perp entry notional; a settlement is one 8h print,
    three a day; ND = sum over hedged cycles of notional x holding days):
      observed funding      funding net / (3 x ND) x 1e4, bps per settlement
      break-even funding    (funding - pair net) / (3 x ND) x 1e4: the flat
                            funding per settlement at which the hedged pair
                            would have netted exactly zero over the holds
                            actually observed (fees, financing, and the price
                            leg when it is booked)
      min holding days      fixed cost (fees less any booked price leg, bps of
                            notional) / observed funding net of financing
                            (bps of notional per day); NEVER (None) when that
                            rate is not positive
      required edge         the break-even at the MEDIAN observed hold:
                            fixed bps / (3 x median days) + financing/3
      sensitivity           pair net minus a stated extra round-trip cost, or
                            minus extra multiples of the journalled fees; the
                            slippage already inside the price leg is not
                            scaled, and spread is not journalled
    """
    out: Dict[str, Any] = {}

    def unavailable(why: str, defn: str, unit: str = "usd") -> Dict[str, Any]:
        return _metric(None, UNAVAILABLE, defn, unit=unit, why=why)

    def all_unavailable(why: str) -> Dict[str, Any]:
        return {k: unavailable(why, k) for k in PAIR_METRIC_KEYS}

    if not closed:
        return all_unavailable("NO_CLOSED_CYCLES: no cycle has gone flat")
    hedged = [c for c in closed if c["outcome"] == "CLOSED"]
    aborted = [c for c in closed if c["outcome"] != "CLOSED"]
    aborted_net = sum(c["net_usd"] for c in aborted)
    if not hedged:
        return all_unavailable(
            "NO_HEDGED_CYCLE: only aborted cycles are closed; their whole "
            f"round trip (${aborted_net:,.2f}) is booked in the net")
    pairs = [(c, hedged_pair(c)) for c in hedged]
    missing = [c["cycle_id"] for c, p in pairs if not p["available"]]
    if missing:
        return all_unavailable(
            f"PAIR_NOT_COMPUTABLE: no hedged-pair net for cycle(s) {missing}: "
            + "; ".join(sorted({p["why"] for _, p in pairs
                                if not p["available"]})))
    derived = [c for c, p in pairs if p["derived"]]
    marked = [c for c, p in pairs if p.get("marked")]
    cls = (HISTORICAL_SIMULATION if derived else
           MARKED_PAIR_ECONOMICS if marked else cls_closed)
    status = ("INCOMPLETE_BOUND" if inc_status != "COMPLETE" else
              "DERIVED_NOT_BOOKED" if derived else "COMPUTED")
    pair_h = sum(p["net_usd"] for _, p in pairs)
    out["hedged_pair_net_usd"] = _metric(
        pair_h + aborted_net, cls,
        "booked net plus, for an overlay cycle whose hedged BTC was not "
        "revalued in the journal, hedged_qty x (perp exit - perp entry); "
        "aborted cycles at their booked loss", status=status,
        derived_cycles=len(derived), booked_net_usd=sum(
            c["net_usd"] for c in closed),
        aborted_cycles_net_usd=aborted_net,
        basis_pnl_included=not derived,
        basis_note=("basis (perp vs spot) P&L is NOT in this figure for the "
                    f"{len(derived)} derived cycle(s): it is not measurable "
                    "from perp prices" if derived else
                    "every price leg is booked; basis is inside the price leg"))

    notional = [c.get("notional_usd") for c in hedged]
    days = [c.get("hold_days") for c in hedged]
    if not all(n and n > 0 for n in notional) or not all(
            d and d > 0 for d in days):
        why = ("NOTIONAL_OR_HOLD_UNKNOWN: a closed hedged cycle has no "
               "readable perp notional or a zero holding period; a rate is "
               "never computed from a guess")
        for k in PAIR_METRIC_KEYS[1:]:
            out[k] = unavailable(why, k)
        return out
    N = sum(notional)
    ND = sum(n * d for n, d in zip(notional, days))
    funding = sum(c["funding_usd"] for c in hedged)
    fees = sum(c["fees_usd"] for c in hedged)
    fin = sum(c["borrow_usd"] for c in hedged)
    S = SETTLEMENTS_PER_DAY

    realised_rate = funding / (S * ND) * 1e4
    out["observed_funding_bps_per_settlement"] = _metric(
        realised_rate, cls_closed, "funding net / (3 x sum notional x days) "
        "x 1e4 over closed hedged cycles", unit="bps_per_settlement",
        status=inc_status)
    be = (funding - pair_h) / (S * ND) * 1e4
    out["break_even_funding_bps_per_settlement"] = _metric(
        be, cls, "flat funding per 8h settlement at which the hedged pair "
        "nets zero over the holds observed: (funding - pair net)/(3 x ND)",
        unit="bps_per_settlement", status=status,
        basis_pnl_included=not derived,
        covers="fees + financing" + (
            " - booked price leg" if not derived else
            " (basis P&L not measurable for derived cycles)"))
    out["funding_margin_over_break_even_bps"] = _metric(
        realised_rate - be, cls, "observed funding - break-even funding, bps "
        "per settlement; negative = funding did not pay for the cycles",
        unit="bps_per_settlement", status=status)

    fixed_bps = (funding - fin - pair_h) / N * 1e4     # fees less price leg
    rate_day = (funding - fin) / ND * 1e4              # funding net of financing
    if rate_day <= 0:
        out["min_holding_days_to_cover_costs"] = unavailable(
            "NEVER: observed funding net of financing is "
            f"{rate_day:.4f} bps/day, which cannot pay back a fixed cost of "
            f"{fixed_bps:.2f} bps at any holding period", "min holding days",
            unit="days")
    else:
        out["min_holding_days_to_cover_costs"] = _metric(
            max(0.0, fixed_bps / rate_day), cls, "fixed cost (fees less any "
            "booked price leg, bps of notional) / observed funding net of "
            "financing (bps of notional per day)", unit="days", status=status,
            fixed_cost_bps_of_notional=fixed_bps,
            funding_net_of_financing_bps_per_day=rate_day,
            observed_mean_hold_days=sum(days) / len(days))
    ordered = sorted(days)
    mid = len(ordered) // 2
    median_hold = (ordered[mid] if len(ordered) % 2 else
                   (ordered[mid - 1] + ordered[mid]) / 2.0)
    out["min_required_funding_edge_bps_per_settlement"] = _metric(
        max(0.0, fixed_bps) / (S * median_hold) + fin / ND * 1e4 / S, cls,
        "break-even funding at the median observed hold: fixed bps / (3 x "
        "median days) + financing bps per day / 3", unit="bps_per_settlement",
        status=status, median_hold_days=median_hold)
    out["net_bps_per_notional_day"] = _metric(
        pair_h / ND * 1e4, cls, "hedged-pair net / sum(notional x days) x 1e4 "
        "over closed hedged cycles (aborted cycles excluded: no holding "
        "period)", unit="bps_per_notional_day", status=status)
    margins = [c.get("margin_committed_usd") for c in hedged]
    if all(m and m > 0 for m in margins):
        peak_margin = max(margins)
        out["pair_return_on_posted_margin_pct"] = _metric(
            100.0 * (pair_h + aborted_net) / peak_margin, cls,
            "hedged-pair net / PEAK observed venue initial margin; for an "
            "overlay the client's BTC is already owned, so margin is the only "
            "capital the hedge adds. Whole period, not annualised",
            unit="pct", status=status, denominator_usd=peak_margin)
    else:
        out["pair_return_on_posted_margin_pct"] = unavailable(
            "no observed venue margin for every closed hedged cycle",
            "pair return on posted margin", unit="pct")

    rows_bps = [{"extra_round_trip_bps": b, "net_usd": pair_h - b / 1e4 * N}
                for b in SENSITIVITY_EXTRA_BPS]
    rows_mult = [{"fee_multiple": m, "net_usd": pair_h - (m - 1.0) * fees}
                 for m in SENSITIVITY_FEE_MULTIPLES]
    out["execution_cost_sensitivity"] = _metric(
        pair_h, HISTORICAL_SIMULATION, "hedged-pair net under stated extra "
        "round-trip cost (bps of notional, per cycle) and stated multiples of "
        "the journalled fees",
        status="SENSITIVITY_NOT_MEASUREMENT", base_net_usd=pair_h,
        extra_bps=rows_bps, fee_multiple=rows_mult,
        extra_round_trip_bps_to_zero_net=(pair_h / N * 1e4
                                          if pair_h > 0 else None),
        fee_multiple_to_zero_net=(1.0 + pair_h / fees
                                  if pair_h > 0 and fees > 0 else None),
        notional_usd=N, fees_usd=fees,
        not_scaled="slippage already inside the price leg; spread (not "
                   "journalled); financing")
    if derived:
        # A pair whose inventory leg is only DERIVED from perp prices is funding
        # minus fees by construction: its basis (perp vs spot) is NOT measured
        # and counts as zero. An EDGE, a CUSHION or a RETURN on it would claim
        # money the basis may have taken back (the reviewer's repro: +1.77% on
        # posted margin and +2.95 bps per notional-day while the unmeasured
        # basis was worth about -$300 per cycle). The cost-recovery LEVELS above
        # (break-even funding, minimum hold, required edge) stay, with their
        # existing "basis not measurable" label: they say what must be earned,
        # not that it was.
        why_d = (
            f"OVERLAY_PAIR_DERIVED_NOT_BOOKED: {len(derived)} overlay cycle(s) "
            "have no booked inventory leg, so their pair is funding minus fees "
            "by construction and its basis (perp vs spot) is NOT measured; an "
            "edge, cushion or return on it would count the basis as zero")
        for key, unit in (("funding_margin_over_break_even_bps",
                           "bps_per_settlement"),
                          ("net_bps_per_notional_day", "bps_per_notional_day"),
                          ("pair_return_on_posted_margin_pct", "pct")):
            out[key] = unavailable(why_d, key, unit=unit)
        out["execution_cost_sensitivity"] = dict(
            out["execution_cost_sensitivity"],
            extra_round_trip_bps_to_zero_net=None,
            fee_multiple_to_zero_net=None, basis_pnl_included=False,
            cushion_withheld=why_d)
    return out


def net_economics(cycles: Sequence[Dict[str, Any]], env: Mapping[str, Any], *,
                  journal: Any, opp_by_cycle: Mapping[str, Mapping[str, Any]],
                  period_days: Optional[float],
                  total_capital_usd: Optional[float] = None,
                  cash_apr_pct: Optional[float] = None,
                  orders: Optional[Sequence[Mapping[str, Any]]] = None,
                  reservations: Optional[Sequence[Mapping[str, Any]]] = None,
                  now_ms: int = 0) -> Dict[str, Any]:
    """The complete net-economics section body and the verdict.

    DEFINITIONS (one each):
      net            = funding received - funding paid - fees - financing
                       + price legs (perp entry->exit, spot cost->proceeds),
                       booked in the ledger; execution slippage is INSIDE the
                       price legs and is not subtracted again.
      committed capital = observed venue initial margin + spot leg (per cycle);
                       the period base is the PEAK single-cycle figure.
      capital-day    = committed capital x holding days.
      headline return = net / peak committed capital. Never gross funding.
    """
    import performance_analytics as PA

    booked_cls = env["booked_figures_class"]
    closed = [c for c in cycles if c["closed"]]
    open_ = [c for c in cycles if not c["closed"]]
    slip_all = slippage_by_cycle(journal)
    per_cycle = [cycle_net_economics(c, slip_all.get(c["cycle_id"]),
                                     opp_by_cycle.get(c["cycle_id"]))
                 for c in cycles]
    cls_closed = _agg_class(closed, booked_cls)
    ordered = sorted(closed, key=lambda c: c["start_ms"])
    cash_nets = [c["net_usd"] for c in ordered]
    complete = all(c.get("fees_complete") and c.get("funding_complete")
                   for c in closed)
    inc_status = "COMPLETE" if complete else "INCOMPLETE_BOUND"
    M: Dict[str, Any] = {}

    # THE ECONOMIC NET (one definition). The booked cycle net is CASH: funding
    # - fees - financing + the perp's realised result. For an ACQUIRE cycle
    # both price legs are booked, so cash IS the pair. For an OVERLAY the
    # client's BTC is never sold, so its move is a MARK that cash does not
    # hold: the booked net is the net of an UNHEDGED short, whose sign follows
    # BTC (a rally reads as a loss, a fall as a win) and which is not a return
    # on anything. Every return, benchmark comparison, risk statistic and the
    # verdict below is therefore taken on the pair (cash + the booked mark),
    # never on cash, whenever an overlay cycle is in the book. A pair that
    # cannot be computed gives NO figure (UNAVAILABLE), not the cash net.
    M.update(pair_economics(closed, cls_closed, inc_status))
    pair_metric = M["hedged_pair_net_usd"]
    n_overlay = sum(1 for c in closed
                    if c["outcome"] == "CLOSED" and c.get("overlay"))
    n_hedged = sum(1 for c in closed if c["outcome"] == "CLOSED")
    n_derived = (pair_metric.get("derived_cycles", 0)
                 if pair_metric["value"] is not None else 0)
    # An ACQUIRE cycle whose perp went flat while the spot leg was unsold or the
    # wrong size has NO pair (`_acquire_spot_leg_problem`): its booked cash net
    # omits the BTC's move (phantom +$5,000 per 5% fall per BTC in the red
    # team's reproduction, with the strongest verdict code).
    n_unflat = sum(1 for c in closed if c["outcome"] == "CLOSED"
                   and not c.get("overlay")
                   and c.get("economic_net_usd") is None)

    # THE ECONOMIC NET (one definition: `_economic_net_of` per cycle). Cash is
    # the pair only where both price legs are booked; an overlay's booked cash is
    # the net of an UNHEDGED short whose sign follows BTC. A cycle whose pair is
    # not MEASURED (derived basis, unavailable, spot leg not flat) has no
    # economic net, and then EVERY return, benchmark comparison, risk statistic
    # and the verdict is UNAVAILABLE rather than taken on the cash figure.
    per_cycle_econ = [c.get("economic_net_usd") for c in ordered]
    measured = all(x is not None for x in per_cycle_econ)
    nets = per_cycle_econ if measured else None
    if n_hedged:
        econ_net = pair_metric["value"]
        econ_class = pair_metric["evidence_class"]
        econ_status = pair_metric["status"]
        econ_why = pair_metric.get("why")
    else:
        econ_net, econ_class, econ_status, econ_why = (
            sum(cash_nets), cls_closed, "CASH_IS_PAIR", None)
    # THE RETURN BASE: the sum of the per-cycle economic nets, or None. A pair
    # whose inventory leg is only DERIVED from perp fill prices is funding - fees
    # BY CONSTRUCTION (basis unmeasured, counted as zero): shown, labelled, as
    # `hedged_pair_net_usd`, never a return, benchmark or statistics base.
    ret_net = sum(per_cycle_econ) if measured else None
    if n_unflat:
        no_pair = ("ACQUIRE_SPOT_LEG_NOT_FLAT: " + str(
            econ_why or "an acquire cycle's spot leg is not flat or not the "
            "size of its hedge") + "; its booked cash net omits the BTC's move "
            "and is not a return")
    elif n_overlay and n_derived and econ_net is not None:
        no_pair = (
            "OVERLAY_PAIR_DERIVED_NOT_BOOKED: the inventory leg is derived from "
            "the perp fill prices, so basis (perp vs spot) is NOT measured and a "
            "return on it would count it as zero; the booked cash net of an "
            "unhedged short is not a return either")
    else:
        no_pair = ("OVERLAY_PAIR_NOT_COMPUTABLE: " + str(
            econ_why or "no hedged-pair net for an overlay cycle")
            + "; the booked cash net of an unhedged short is not a return")

    funding_net = sum(c["funding_usd"] for c in closed)
    funding_in = sum(c["funding_received_usd"] for c in closed)
    funding_out = sum(c["funding_paid_usd"] for c in closed)
    fees = sum(c["fees_usd"] for c in closed)
    fin = sum(c["borrow_usd"] for c in closed)
    price = sum(c["realised_usd"] for c in closed)
    net = sum(cash_nets)          # CASH: the booked net, never a return base
    if not closed:
        no = "NO_CLOSED_CYCLES: no cycle has gone flat"
        for k in ("net_closed_cycles_usd", "funding_received_usd",
                  "funding_paid_usd"):
            M[k] = _metric(None, UNAVAILABLE, k, why=no)
    else:
        M["net_closed_cycles_usd"] = _metric(
            net, cls_closed, "sum over closed cycles of funding received - "
            "funding paid - fees - financing + price legs", status=inc_status)
        M["funding_received_usd"] = _metric(
            funding_in, cls_closed, "sum of positive funding prints",
            status=inc_status)
        M["funding_paid_usd"] = _metric(
            funding_out, cls_closed, "sum of negative funding prints "
            "(a cost)", status=inc_status)
    # open positions: costs and income booked so far; NOT valued at a mark
    open_booked = sum(c["funding_usd"] - c["fees_usd"] - c["borrow_usd"]
                      + c["realised_usd"] for c in open_)
    if open_:
        M["net_open_positions_booked_usd"] = _metric(
            open_booked, OPEN_MARK_TO_MARKET, "funding - fees - financing + "
            "price legs booked so far on cycles that have not gone flat; NO "
            "mark is journalled, so the unrealised price move is excluded",
            status="PARTIAL_NO_MARK",
            mark_to_market_usd=_metric(None, UNAVAILABLE, "mark to market",
                                       why="NO_MARK_JOURNALLED"))
    else:
        M["net_open_positions_booked_usd"] = _metric(
            None, UNAVAILABLE, "open positions", why="no open cycle")
    M["net_total_including_open_booked_usd"] = _metric(
        net + open_booked, booked_cls, "closed net + open positions' booked "
        "costs and income (no mark)", status="PARTIAL_NO_MARK") \
        if closed or open_ else _metric(None, UNAVAILABLE, "total",
                                        why="no cycles")

    # capital
    hedged = [c for c in closed if c["outcome"] == "CLOSED"]
    caps = [c.get("capital_committed_usd") for c in hedged]
    cap_ok = bool(hedged) and all(x for x in caps)
    peak_cap = max(caps) if cap_ok else None
    cap_days = (sum(c["capital_committed_usd"] * c["hold_days"] for c in hedged)
                if cap_ok else None)
    no_cap = ("no observed venue margin (or no spot leg value) for every "
              "closed hedged cycle; committed capital is never guessed")
    ret_cls = econ_class if closed else cls_closed
    # An overlay whose pair is not computable has no return: its cash net
    # follows BTC, so it is withheld from every return rather than divided by
    # capital. The capital OCCUPATION below does not depend on the net.
    ret_why = no_pair if (closed and ret_net is None) else no_cap
    M["return_on_committed_capital_pct"] = (
        _metric(100.0 * ret_net / peak_cap, ret_cls, "economic net (the "
                "hedged pair for an overlay) / PEAK single-cycle committed "
                "capital; whole period, not annualised",
                unit="pct", status=inc_status, denominator_usd=peak_cap,
                numerator_basis=econ_status)
        if peak_cap and closed and ret_net is not None else
        _metric(None, UNAVAILABLE, "net / peak committed capital",
                why=ret_why))
    if total_capital_usd is not None and total_capital_usd > 0 \
            and math.isfinite(total_capital_usd) and closed \
            and ret_net is not None:
        M["return_on_total_capital_pct"] = _metric(
            100.0 * ret_net / total_capital_usd, ret_cls,
            "economic net (the hedged pair for an overlay) / operator-supplied "
            "total account capital; not annualised",
            unit="pct", status=inc_status, denominator_usd=total_capital_usd,
            denominator_source="operator-supplied (--total-capital-usd)",
            numerator_basis=econ_status)
    elif closed and ret_net is None and total_capital_usd:
        M["return_on_total_capital_pct"] = _metric(
            None, UNAVAILABLE, "net / total capital", unit="pct", why=no_pair)
    else:
        M["return_on_total_capital_pct"] = _metric(
            None, UNAVAILABLE, "net / total capital", unit="pct",
            why="TOTAL_CAPITAL_NOT_SUPPLIED: pass --total-capital-usd; the "
                "ledger does not know the account's capital")
    days_in_market = sum(c.get("hold_days") or 0.0 for c in closed)
    M["capital_occupation_usd_days"] = (
        _metric(cap_days, cls_closed, "sum over closed hedged cycles of "
                "committed capital x holding days", unit="usd_days")
        if cap_days else _metric(None, UNAVAILABLE, "capital-days", why=no_cap,
                                 unit="usd_days"))
    per_cd = ((100.0 * ret_net / cap_days)
              if cap_days and ret_net is not None else None)
    M["net_return_per_capital_day_pct"] = (
        _metric(per_cd, ret_cls, "economic net (the hedged pair for an "
                "overlay) / capital-days x 100 (per $ of committed capital "
                "per day)", unit="pct_per_usd_day", status=inc_status,
                numerator_basis=econ_status)
        if per_cd is not None else
        _metric(None, UNAVAILABLE, "net per capital-day", why=ret_why,
                unit="pct_per_usd_day"))
    ann = ann_why = None
    if per_cd is not None:
        ann, ann_why = PA.annualise_simple(per_cd * days_in_market,
                                           days_in_market, len(closed))
    M["annualised_return_on_committed_capital_pct"] = (
        _metric(ann, ret_cls, "economic net per capital-day x 365 (simple, "
                "not compounded); refused for small samples", unit="pct",
                status=inc_status, numerator_basis=econ_status)
        if ann is not None else _metric(
            None, UNAVAILABLE, "annualised return", unit="pct",
            why=ann_why or ret_why))
    M["annualisation_rule"] = {
        "min_closed_cycles": PA.MIN_ANNUALISE_CYCLES,
        "min_days_in_market": PA.MIN_ANNUALISE_DAYS,
        "closed_cycles": len(closed), "days_in_market": days_in_market}
    M["time_utilization_pct"] = (
        _metric(100.0 * min(1.0, days_in_market / period_days), cls_closed,
                "days with a position open / calendar days first start to last "
                "close", unit="pct")
        if closed and period_days and period_days > 0 else
        _metric(None, UNAVAILABLE, "time utilization", unit="pct",
                why="no closed cycle or zero-length period"))
    M["capital_utilization_pct"] = (
        _metric(100.0 * cap_days / (total_capital_usd * period_days),
                cls_closed, "capital-days / (total capital x period days)",
                unit="pct")
        if cap_days and total_capital_usd and total_capital_usd > 0
        and period_days and period_days > 0 else
        _metric(None, UNAVAILABLE, "capital utilization", unit="pct",
                why="needs committed capital for every closed hedged cycle AND "
                    "an operator-supplied total capital"))
    M["reservation_duration"] = reservation_stats(reservations, now_ms,
                                                  booked_cls)

    # risk and distribution, on the ECONOMIC net of each cycle (the pair for an
    # overlay; see the definition above). An overlay cycle whose pair cannot be
    # computed leaves every statistic UNAVAILABLE: the cash net of an unhedged
    # short is not a distribution of returns.
    stats_ok = bool(closed) and nets is not None
    stats_why = ("NO_CLOSED_CYCLES" if not closed else no_pair)
    st_cls = econ_class if closed else cls_closed
    dd = PA.max_drawdown_usd(nets) if stats_ok else None
    M["max_drawdown_usd"] = (
        _metric(dd, st_cls, "largest peak-to-trough fall of cumulative "
                "economic net (the hedged pair for an overlay) across closed "
                "cycles in start order; counts closed cycles "
                "only (no intra-cycle mark)", status=inc_status)
        if stats_ok else _metric(None, UNAVAILABLE, "drawdown",
                                 why=stats_why))
    dsd = PA.downside_deviation(nets) if stats_ok else None
    M["downside_deviation_usd"] = (
        _metric(dsd, st_cls, "sqrt(mean(min(cycle net, 0)^2)) over all "
                "closed cycles", status=inc_status)
        if dsd is not None else _metric(
            None, UNAVAILABLE, "downside deviation",
            why=(stats_why if closed and nets is None else
                 f"SAMPLE_TOO_SMALL: needs >= {PA.MIN_CYCLES_FOR_DISPERSION} "
                 "closed cycles")))
    conc = PA.profit_concentration(nets) if stats_ok else {}
    M["net_excluding_largest_winner_usd"] = (
        _metric(conc["net_excluding_largest_winner_usd"], st_cls,
                "economic net of closed cycles with the single best cycle "
                "removed", status=inc_status)
        if stats_ok else _metric(None, UNAVAILABLE, "ex-winner",
                                 why=stats_why))
    M["profit_concentration"] = (
        _metric(conc["largest_winner_share_of_winning_net_pct"], st_cls,
                "best cycle / sum of winning cycles x 100", unit="pct",
                status=inc_status,
                top_quartile_share_of_winning_net_pct=conc[
                    "top_quartile_share_of_winning_net_pct"])
        if stats_ok and conc["largest_winner_usd"] is not None else
        _metric(None, UNAVAILABLE, "concentration", unit="pct",
                why=(stats_why if closed and nets is None
                     else "no winning closed cycle")))
    ex = PA.expectancy(nets) if stats_ok else None
    M["expectancy_after_costs_usd"] = (
        _metric(ex["expectancy_per_cycle_usd"], st_cls, "mean economic net "
                "per closed cycle, after every booked cost", status=inc_status,
                win_rate=ex["win_rate"], avg_win_usd=ex["avg_win_usd"],
                avg_loss_usd=ex["avg_loss_usd"], cycles=len(closed))
        if stats_ok else _metric(None, UNAVAILABLE, "expectancy",
                                 why=stats_why))
    for dim in ("regime", "symbol", "venue"):
        M[f"performance_by_{dim}"] = _metric(
            None, UNAVAILABLE, f"net by {dim}", why=(
                f"{dim.upper()}_NOT_JOURNALLED: the carry ledger's cycles carry "
                "no " + dim + " attribute, so no split is computed"))

    # execution
    slip_tot = {"fills": sum(v["fills"] for v in slip_all.values()),
                "measured_fills": sum(v["measured"] for v in slip_all.values()),
                "slippage_cost_usd_measured_subset": sum(
                    v["usd"] for v in slip_all.values()),
                "complete": all(v["measured"] == v["fills"]
                                for v in slip_all.values()) and bool(slip_all),
                "expected_funding": _comp(None, UNAVAILABLE, why=(
                    "the funding expected at entry is not journalled, so "
                    "expected-vs-actual funding is unknown"))}
    M["execution_quality"] = execution_quality(orders, slip_tot)

    # benchmarks: only when every closed hedged cycle has the figure
    def _all(key: str, field: str) -> Optional[List[float]]:
        vals = []
        for c in hedged:
            blk = (opp_by_cycle.get(c["cycle_id"]) or {}).get(key) or {}
            if not blk.get("available"):
                return None
            vals.append(blk[field])
        return vals or None
    u = _all("vs_unhedged_btc", "unhedged_btc_pnl_usd")
    M["vs_unhedged_btc_hold"] = (
        _metric(sum(u), HISTORICAL_SIMULATION, "sum of what the hedged BTC "
                "would have made unhedged over the same holding periods",
                status="COUNTERFACTUAL",
                hedged_pair_minus_unhedged_usd=(
                    None if ret_net is None else ret_net - sum(u)))
        if u else _metric(None, UNAVAILABLE, "unhedged BTC", why=(
            "an unhedged-hold comparison needs a closed overlay cycle with "
            "readable perp prices for EVERY closed hedged cycle")))
    b = _all("vs_benchmark_rate", "benchmark_alternative_usd")
    M["vs_benchmark_rate"] = (
        _metric(sum(b), HISTORICAL_SIMULATION, "committed capital held at the "
                "operator-supplied APR over the same holding periods",
                status="COUNTERFACTUAL", benchmark_apr_pct=cash_apr_pct,
                net_minus_benchmark_usd=(
                    None if ret_net is None else ret_net - sum(b)))
        if b else _metric(None, UNAVAILABLE, "benchmark rate", why=(
            "no operator-supplied --cash-apr, or committed capital missing "
            "for a closed hedged cycle")))

    verdict = net_verdict(
        hedged_pair_net=pair_metric["value"], inventory_unbooked_cycles=n_derived,
        closed=closed, net=net, funding_net=funding_net, fees=fees,
        financing=fin, price=price, complete=complete,
        net_ex_winner=(conc.get("net_excluding_largest_winner_usd")
                       if stats_ok else None),
        open_booked=open_booked, n_open=len(open_),
        vs_benchmark=((ret_net - sum(b))
                      if b and ret_net is not None else None),
        sample_ok=len(closed) >= PA.MIN_ANNUALISE_CYCLES,
        overlay_cycles=n_overlay, unflat_cycles=n_unflat,
        economic_net=econ_net)
    headline = {
        # CASH, as booked. For an overlay this is the net of an unhedged
        # short and follows BTC; it is kept for the ledger and is NOT the
        # figure a return, a benchmark or the verdict is taken on.
        "basis": "NET_AFTER_BOOKED_COSTS",
        "net_usd": net if closed else None,
        "net_usd_meaning": (
            "the BOOKED ledger net: funding - fees - financing + the perp's "
            "realised result. For an overlay it is the net of an unhedged "
            "short and follows BTC; see economic_net_usd"),
        # THE economic result: the hedged pair (cash + the booked, never-sold
        # inventory mark for an overlay; cash itself for acquire).
        "economic_net_usd": ret_net if closed else None,
        "economic_net_basis": econ_status if closed else None,
        "return_on_committed_capital_pct": M["return_on_committed_capital_pct"][
            "value"],
        "annualised_return_on_committed_capital_pct": M[
            "annualised_return_on_committed_capital_pct"]["value"],
        "complete": complete,
        "evidence_class": cls_closed if closed else UNAVAILABLE,
        "hedged_pair_net_usd": pair_metric["value"],
        "gross_funding_is_not_the_headline": funding_net if closed else None}
    return {"schema": NET_ECONOMICS_SCHEMA,
            "headline": headline, "metrics": M, "cycles": per_cycle,
            "verdict": verdict}


def net_verdict(*, hedged_pair_net: Optional[float] = None,
                inventory_unbooked_cycles: int = 0, overlay_cycles: int = 0,
                unflat_cycles: int = 0,
                economic_net: Optional[float] = None,
                **kw: Any) -> Dict[str, Any]:
    """The verdict, judged on the ECONOMIC net.

    An ACQUIRE book (``overlay_cycles`` 0) is judged on its booked net exactly
    as before: both price legs are in it. An OVERLAY book is judged on the
    hedged PAIR (cash + the booked, never-sold inventory mark), never on the
    cash net of an unhedged short, whose sign follows BTC: a rally is not a
    carry loss and a fall is not a carry win.

    * pair measured (booked, verified marks): the ordinary verdict codes, on
      the pair net, with the cash net kept as its own labelled figure;
    * pair only DERIVED from perp fill prices (basis not measured): the
      verdict is ``OVERLAY_PAIR_DERIVED_NOT_BOOKED`` and claims no profit and
      no loss, only the two figures and what they are;
    * no pair computable: ``OVERLAY_PAIR_NOT_COMPUTABLE``.
    """
    cash_net = kw["net"]
    money = lambda x: f"{'-' if x < 0 else ''}${abs(x):,.2f}"  # noqa: E731
    if not ((overlay_cycles or unflat_cycles) and kw["closed"]):
        v = _net_verdict(**kw)
    elif unflat_cycles:
        base = _net_verdict(**kw)
        v = {"code": "ACQUIRE_SPOT_LEG_NOT_FLAT",
             **{k: base[k] for k in ("funding_net_usd", "explicit_costs_usd",
                                     "price_leg_usd", "net_usd",
                                     "funding_exceeds_explicit_costs",
                                     "net_complete")},
             "statement": (
                 f"The booked cash net is {money(cash_net)}, but {unflat_cycles} "
                 "acquire cycle(s) closed the perp while the spot leg was "
                 "unsold or not the size of the hedge, so the BTC's price move "
                 "is not booked and the cash net is not the pair's: no net "
                 "result is claimed.")}
    elif economic_net is None:
        base = _net_verdict(**kw)
        v = {"code": "OVERLAY_PAIR_NOT_COMPUTABLE",
             **{k: base[k] for k in ("funding_net_usd", "explicit_costs_usd",
                                     "price_leg_usd", "net_usd",
                                     "funding_exceeds_explicit_costs",
                                     "net_complete")},
             "statement": (
                 f"The booked cash net is {money(cash_net)}, but {overlay_cycles} "
                 "overlay cycle(s) hedge client BTC and no hedged-pair net can "
                 "be computed for them, so the booked figure is the net of an "
                 "unhedged short (its sign follows BTC) and no net result is "
                 "claimed.")}
    elif inventory_unbooked_cycles:
        base = _net_verdict(**kw)
        v = {"code": "OVERLAY_PAIR_DERIVED_NOT_BOOKED",
             **{k: base[k] for k in ("funding_net_usd", "explicit_costs_usd",
                                     "price_leg_usd", "net_usd",
                                     "funding_exceeds_explicit_costs",
                                     "net_complete")},
             "statement": (
                 f"{inventory_unbooked_cycles} overlay cycle(s) hedge client "
                 "BTC inventory that the journal never revalued, so the booked "
                 f"net ({money(cash_net)}) is the net of an unhedged short, not "
                 "of the pair. With the inventory leg derived from the perp "
                 f"fill prices the hedged-pair net is {money(economic_net)} "
                 "(basis P&L is not measurable from perp prices). Neither "
                 "figure is a measured economic result: no profit and no loss "
                 "is claimed.")}
    else:
        # A measured pair: the ordinary verdict, on the pair. The price legs
        # of the PAIR are what is left after funding and the explicit costs.
        pair_kw = dict(kw)
        pair_kw["net"] = economic_net
        pair_kw["price"] = economic_net - (kw["funding_net"] - kw["fees"]
                                           - kw["financing"])
        v = _net_verdict(**pair_kw)
        v["booked_net_usd"] = cash_net
        v["statement"] += (
            f" (Judged on the hedged pair: cash net {money(cash_net)} plus the "
            "booked inventory mark; the cash net alone is the net of an "
            "unhedged short.)")
    v["hedged_pair_net_usd"] = hedged_pair_net
    v["economic_net_usd"] = (None if not kw["closed"]
                             or inventory_unbooked_cycles or unflat_cycles
                             else economic_net)
    v["inventory_unbooked_cycles"] = inventory_unbooked_cycles
    if kw["closed"] and inventory_unbooked_cycles and hedged_pair_net is not None:
        v["booked_net_minus_pair_net_usd"] = cash_net - hedged_pair_net
    return v


def _net_verdict(*, closed: Sequence[Any], net: float, funding_net: float,
                 fees: float, financing: float, price: float, complete: bool,
                 net_ex_winner: Optional[float], open_booked: float,
                 n_open: int, vs_benchmark: Optional[float],
                 sample_ok: bool) -> Dict[str, Any]:
    """One plain statement of what the book's economics are. A carry whose
    funding beats its costs but whose NET is negative says so here."""
    costs = fees + financing
    base = {"funding_net_usd": funding_net, "explicit_costs_usd": costs,
            "price_leg_usd": price, "net_usd": net,
            "funding_exceeds_explicit_costs": funding_net > costs,
            "net_complete": complete}
    if not closed:
        return {"code": "NO_CLOSED_CYCLES", **base, "statement": (
            "No cycle has gone flat: there is no net result to report."
            + (f" {n_open} open position(s) have booked costs of "
               f"${-open_booked:,.2f} net so far and are not valued."
               if n_open and open_booked < 0 else ""))}
    money = lambda v: f"{'-' if v < 0 else ''}${abs(v):,.2f}"  # noqa: E731
    tail = "" if complete else (
        " Some fees or funding are unconfirmed, so this net is a bound, not "
        "a final figure.")
    if net <= 0:
        if funding_net > costs:
            code = "FUNDING_POSITIVE_BUT_NET_NEGATIVE"
            msg = (f"Funding ({money(funding_net)}) exceeds fees and financing "
                   f"({money(costs)}), but NET after the price legs "
                   f"({money(price)}) is {money(net)}: this carry LOSES money "
                   "net. Gross funding is not profit.")
        elif funding_net > 0:
            code = "FUNDING_DOES_NOT_COVER_COSTS"
            msg = (f"Funding ({money(funding_net)}) is below fees and "
                   f"financing ({money(costs)}); NET is {money(net)}.")
        else:
            code = "NET_NEGATIVE"
            msg = f"Net is {money(net)}; funding was {money(funding_net)}."
        return {"code": code, **base, "statement": msg + tail}
    if not complete:
        return {"code": "NET_POSITIVE_UNVERIFIED", **base, "statement": (
            f"Booked net is {money(net)} but fees or funding are unconfirmed; "
            "do not quote it as profit.")}
    if net_ex_winner is not None and net_ex_winner <= 0 and len(closed) > 1:
        return {"code": "NET_POSITIVE_DEPENDS_ON_LARGEST_WINNER", **base,
                "statement": (f"Net is {money(net)}, but without the single "
                              f"best cycle it is {money(net_ex_winner)}.")}
    if vs_benchmark is not None and vs_benchmark < 0:
        return {"code": "NET_POSITIVE_BELOW_BENCHMARK_RATE", **base,
                "statement": (f"Net is {money(net)} but committed capital at "
                              f"the supplied benchmark rate would have earned "
                              f"{money(-vs_benchmark)} more.")}
    if not sample_ok:
        return {"code": "NET_POSITIVE_SMALL_SAMPLE", **base, "statement": (
            f"Net is {money(net)} over {len(closed)} closed cycle(s): too few "
            "to annualise or to call an edge.")}
    drag = (" Open positions carry booked costs of "
            f"{money(-open_booked)} not in this net." if n_open
            and open_booked < 0 else "")
    if vs_benchmark is None:
        # A positive net with no alternative stated is not a profit claim: the
        # question is what the same capital earns elsewhere, and nothing here
        # was told (no --cash-apr, or no committed capital for a cycle). It is
        # never defaulted to a rate, and never read as "beats nothing".
        return {"code": "NET_POSITIVE_BENCHMARK_UNSTATED", **base,
                "statement": (
                    f"Net after booked costs is {money(net)}, but no benchmark "
                    "rate (--cash-apr) was supplied or committed capital is "
                    "missing, so this is NOT a statement that the book beats "
                    "the owner's alternative use of the capital." + drag)}
    return {"code": "NET_POSITIVE", **base,
            "statement": (f"Net after booked costs is {money(net)}, ahead of "
                          "the supplied benchmark rate." + drag)}


def build_report(db_path: str, *, environment: Optional[str] = None,
                 cash_apr_pct: Optional[float] = None,
                 cash_apr_source: str = "",
                 now_ms: Optional[int] = None,
                 total_capital_usd: Optional[float] = None) -> Dict[str, Any]:
    """The one machine-readable performance report. Reads only.

    ``total_capital_usd`` is operator-supplied account capital; without it
    return on total capital and capital utilization are UNAVAILABLE."""
    import performance_analytics as PA

    journal = load_journal(db_path)
    now_ms = int(time.time() * 1000) if now_ms is None else int(now_ms)
    env = classify_environment(environment, load_environment_record(db_path))
    statement = journal.statement(now_ms=now_ms)
    cycles = cycles_from_journal(journal)
    attach_capital(cycles, load_observations(db_path))
    summary = summarise(cycles)
    invariants = check_invariants(journal, cycles, statement)
    invariants.append(order_fills_match_ledger(
        journal, load_carry_order_fills(db_path)))
    invariants.append(check_net_components(cycles))
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
        pr = hedged_pair(c)
        d["hedged_pair_net_usd"] = pr["net_usd"] if pr["available"] else None
        d["hedged_pair_inventory_leg"] = (pr["inventory_leg"]
                                          if pr["available"] else None)
        d.update(_pair_fields(c, bool(c["fees_complete"]
                                      and c["funding_complete"])))
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

    marked = [d for d in detail_cycles if d.get("pair_net_usd") is not None]
    if marked:
        sections["marked_pair_economics"] = section(
            MARKED_PAIR_ECONOMICS,
            why=["the client's BTC is valued at a mark and was not sold: this "
                 "is economic performance of the hedged pair, not settled "
                 "money, and it can never satisfy a cash-only policy"],
            status="NOT_CASH", settled_on_venue=False,
            valuation=("inventory MARKED from the quantity-weighted spot seen "
                       "at each open fill to the spot seen at the decision "
                       "tick (or one public read right after the fill) of "
                       "each close; marks older than "
                       f"{MAX_MARK_AGE_MS} ms before their fill, observed "
                       "after it, duplicated, conflicting or not covering the "
                       "closed quantity make a cycle UNAVAILABLE"),
            marked_pair_net_usd=sum(d["pair_net_usd"] for d in marked),
            inventory_mark_usd=sum(d["inventory_mark_usd"] for d in marked),
            net_after_booked_costs_usd=sum(d["net_usd"] for d in marked),
            marked_pair_cycles=len(marked),
            marked_pair_cycle_ids=[d["cycle_id"] for d in marked],
            max_mark_age_ms=MAX_MARK_AGE_MS)
    else:
        sections["marked_pair_economics"] = section(
            UNAVAILABLE, why=sorted({r for d in detail_cycles
                                     for r in d.get("pair_unavailable_reasons",
                                                    []) if r}) or [
                "NO_VERIFIED_MARKED_PAIR_CYCLE: no closed, complete overlay "
                "cycle has verified inventory marks"])

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

    opp_by_cycle = {o["cycle_id"]: o for o in opp}
    ne = net_economics(
        cycles, env, journal=journal, opp_by_cycle=opp_by_cycle,
        period_days=summary.get("period_days"),
        total_capital_usd=total_capital_usd, cash_apr_pct=cash_apr_pct,
        orders=load_carry_order_execution(db_path),
        reservations=load_capital_reservations(db_path), now_ms=now_ms)
    ne_complete = ne["headline"]["complete"] and not blockers
    sections["net_economics"] = section(
        ne["headline"]["evidence_class"] if closed else cls,
        why=([] if ne_complete and cls == ACTUAL_RECONCILED else
             (blockers or ["a closed cycle has unconfirmed fees or funding"]
              if closed else ["NO_CLOSED_CYCLES"])),
        status=None if ne["headline"]["evidence_class"] == ACTUAL_RECONCILED
        else ("PROVISIONAL" if cls in (UNAVAILABLE, UNDECLARED_ENVIRONMENT)
              else "NOT_CASH"),
        headline=ne["headline"], metrics=ne["metrics"], cycles=ne["cycles"],
        verdict=ne["verdict"], schema=ne["schema"])

    report = {"report_schema": REPORT_SCHEMA, "db": db_path,
              "net_economics_verdict": ne["verdict"],
              "headline": ne["headline"],
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
    # an unmeasured figure prints as a bare "n/a", never "$n/a" or "n/a%"
    usd = lambda v: "n/a" if v is None else f"${v:,.2f}"  # noqa: E731
    pct = lambda v, p="{:,.2f}": "n/a" if v is None else p.format(v) + "%"  # noqa: E731
    lines = [f"cycles: {s['closed_cycles']} closed ({s['aborted_cycles']} "
             f"aborted before the hedge), {s['open_cycles']} open "
             f"({s['provisional_cycles']} closed but provisional)"]
    if s["status"] == "NO_CLOSED_CYCLES":
        lines.append("no closed cycle: no performance figure exists yet")
    else:
        lines += [
            f"confirmed net (booked CASH; for an overlay the net of an unhedged "
            f"short): {usd(s['net_usd_confirmed'])}   provisional net (not "
            f"confirmed): {usd(s['net_usd_provisional'])}   economic net (the "
            f"hedged pair): {usd(s.get('economic_net_usd'))}",
            f"funding {usd(s['funding_usd'])}  price leg "
            f"{usd(s['price_leg_usd'])}  = gross before costs "
            f"{usd(s['gross_before_costs_usd'])}",
            f"fees {usd(s['fees_usd'])}  financing {usd(s['borrow_usd'])}  "
            f"(fees are {pct(s['fees_pct_of_funding'], '{:.1f}')} of funding)",
            f"peak notional {usd(s['peak_notional_usd'])}  return on it "
            f"{pct(s['return_on_peak_notional_pct'])}  annualised over "
            f"{f(s['days_in_market'], '{:.1f}')} days in market: "
            f"{pct(s['annualised_on_notional_in_market_pct'])}",
            f"max drawdown (closed cycles) {usd(s['max_drawdown_usd'])}  win "
            f"rate {pct(None if s['win_rate'] is None else 100 * s['win_rate'], '{:.0f}')}  best winner = "
            f"{pct(s['best_cycle_share_of_winning_net_pct'], '{:.1f}')} of "
            f"winning net  mean hold {f(s['mean_hold_days'], '{:.1f}')} days"]
    if s.get("returns_withheld_why"):
        lines.append("returns, win rate and drawdown: n/a - " + str(
            s["returns_withheld_why"]))
    if s.get("period_return_on_peak_capital_pct") is not None:
        lines.append(
            f"committed capital (venue margin + spot leg), peak "
            f"{usd(s['peak_capital_committed_usd'])}; period economic net / "
            f"that peak {pct(s['period_return_on_peak_capital_pct'])} over "
            f"{f(s['period_days'], '{:.1f}')} calendar days (peak margin "
            f"{usd(s['margin_committed_usd'])}); {s['capital_basis']}")
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
    v = report.get("net_economics_verdict")
    lines = ([f"NET ECONOMICS [{v['code']}]: {v['statement']}"] if v else [])
    h = report.get("headline") or {}
    pair, booked = h.get("hedged_pair_net_usd"), h.get("net_usd")
    if pair is not None and booked is not None and abs(pair - booked) > 0.005:
        lines.append(
            f"hedged-pair net ${pair:,.2f} vs booked net ${booked:,.2f}: an "
            "overlay's hedged BTC is not revalued in the journal, so the "
            "booked net includes the short's price leg without its offset "
            "(basis P&L not measurable from perp prices)")
    lines += [f"environment: declared={env['declared'] or 'NONE'}  "
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
    ap.add_argument("--total-capital-usd", type=float, default=None,
                    help="operator-supplied total account capital in USD; "
                         "omitted, return on total capital and capital "
                         "utilization are UNAVAILABLE")
    ap.add_argument("--now-ms", type=int, default=None)
    args = ap.parse_args(argv)
    try:
        report = build_report(args.db, environment=args.environment,
                              cash_apr_pct=args.cash_apr,
                              cash_apr_source=args.cash_apr_source,
                              now_ms=args.now_ms,
                              total_capital_usd=args.total_capital_usd)
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
