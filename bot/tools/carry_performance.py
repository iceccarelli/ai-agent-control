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

    python3 tools/carry_performance.py --db state/trading_state.db [--json]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from typing import Any, Dict, List, Optional, Sequence

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import ledger as L  # noqa: E402

DAY_MS = 86_400_000
_SHORT_MEMO = re.compile(r"^short (\S+) @ (\S+)$")
MARGIN_NOTE = ("margin committed comes from the venue's initial margin as "
               "observed on the liquidation reads; a cycle with no observed "
               "margin gets none, and no return on committed capital is "
               "computed from a guess; returns are on notional")


def _notional_of(entry: Any) -> Optional[float]:
    m = _SHORT_MEMO.match(entry.memo or "")
    if not m:
        return None
    try:
        return float(m.group(1)) * float(m.group(2))
    except ValueError:
        return None


def _basis(fee_statuses: List[str], bases: List[str]) -> str:
    if any(s in ("unknown", "unresolved", "unlabelled") for s in fee_statuses):
        return "provisional_fees_incomplete"
    if any(b in ("engine_estimate", "unattributed", "unread") for b in bases):
        return "provisional_funding_unverified"
    return "confirmed"


def _finish(cur: Dict[str, Any]) -> Dict[str, Any]:
    first, last = cur["first_open_ms"], cur["last_close_ms"]
    cur["closed"] = bool(cur["perp_closed"])
    cur["hold_days"] = (None if first is None or last is None or not cur["closed"]
                        else (last - first) / DAY_MS)
    cur["net_usd"] = (cur["funding_usd"] - cur["fees_usd"] - cur["borrow_usd"]
                      + cur["realised_usd"])
    cur["notional_usd"] = (None if cur["notional_unknown"]
                           or cur["notional_usd"] <= 0 else cur["notional_usd"])
    cur["basis"] = _basis(cur.pop("_fee_statuses"), cur.pop("_bases"))
    for key in ("perp_closed", "notional_unknown", "first_open_ms",
                "last_close_ms", "perp_qty"):
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
                "cycle_id": e.cycle, "start_ms": e.ms, "funding_usd": 0.0,
                "fees_usd": 0.0, "borrow_usd": 0.0, "realised_usd": 0.0,
                "notional_usd": 0.0, "notional_unknown": False,
                "perp_closed": False, "perp_qty": 0.0, "spot_cost_usd": 0.0,
                "first_open_ms": None,
                "last_close_ms": None, "_fee_statuses": [], "_bases": []}
            order.append(e.cycle)
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
    prefix, _, tail = str(cycle_id).partition("-")
    return int(tail) if prefix == "cyc" and tail.isdigit() else None


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


def summarise(cycles: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    closed = [c for c in cycles if c["closed"]]
    s: Dict[str, Any] = {
        "closed_cycles": len(closed),
        "open_cycles": len(cycles) - len(closed),
        "margin_committed_usd": None, "margin_note": MARGIN_NOTE,
        "peak_capital_committed_usd": None,
        "return_on_committed_capital_pct": None,
        "slippage_note": ("decision-time marks are not journalled, so fill "
                          "slippage is not computed here; fees are the venue's"),
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
    notionals = [c["notional_usd"] for c in closed]
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
    margins = [c["margin_committed_usd"] for c in closed
               if c.get("margin_committed_usd") is not None]
    s["margin_committed_usd"] = max(margins) if margins else None
    caps = [c.get("capital_committed_usd") for c in closed]
    if caps and all(c is not None for c in caps):
        s["peak_capital_committed_usd"] = max(caps)
        s["return_on_committed_capital_pct"] = 100.0 * total_net / max(caps)
    return s


def load_journal(db_path: str) -> Any:
    if not os.path.exists(db_path):
        raise FileNotFoundError(db_path)
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
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


def report_lines(cycles: Sequence[Dict[str, Any]], s: Dict[str, Any]) -> List[str]:
    f = lambda v, p="{:,.2f}": "n/a" if v is None else p.format(v)  # noqa: E731
    lines = [f"cycles: {s['closed_cycles']} closed, {s['open_cycles']} open "
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
    if s.get("return_on_committed_capital_pct") is not None:
        lines.append(
            f"committed capital (venue margin + spot leg), peak "
            f"${f(s['peak_capital_committed_usd'])}; return on it "
            f"{f(s['return_on_committed_capital_pct'])}% (peak margin "
            f"${f(s['margin_committed_usd'])})")
    else:
        lines.append("committed capital: not available (no observed venue margin "
                     "for every closed cycle); no return on capital is shown")
    lines.append(s["margin_note"])
    lines.append(s["slippage_note"])
    lines.append("confirmed = every fee venue-stated and every funding entry "
                 "from a venue row; completeness against the public funding "
                 "prints is carry_report.py / evidence_reconcile.py")
    return lines


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--db", required=True)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    try:
        journal = load_journal(args.db)
    except (FileNotFoundError, LookupError, sqlite3.Error, ValueError) as exc:
        print(f"refusing: {exc}", file=sys.stderr)
        return 2
    cycles = cycles_from_journal(journal)
    attach_capital(cycles, load_observations(args.db))
    summary = summarise(cycles)
    if args.json:
        print(json.dumps({"cycles": cycles, "summary": summary}, indent=2,
                         default=str))
    else:
        print("\n".join(report_lines(cycles, summary)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
