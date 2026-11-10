#!/usr/bin/env python3
"""Capital allocation and REALISED-ONLY compounding for the unattended Testnet autopilot. Pure arithmetic (Decimal) + one small JSON book. No network, no orders, no venue import.

WHAT THIS DECIDES: how many USD of notional the next session may use. It can only make the answer SMALLER than the owner's limits; it never raises them.
  size = min( grant max_notional_usd, AD.AGGREGATE_CEILING_USD - open gross, compounded size, margin cap )     (0 = stay idle)
  compounded size = ceiling * INITIAL_FRACTION * clamp(basis / start_cash, 0, MAX_SCALE)  <= ceiling * MAX_FRACTION
  basis = start_cash + VERIFIED realised result.  Unrealised gains, estimated funding and unexplained cash are NOT in it.

WHAT COUNTS AS REALISED (the evidence hierarchy, each step is a test):
  * The venue's own USDT wallet balance read at the start of two consecutive sessions, with the account verified FLAT in between, gives ``delta`` (cash that really moved: price result, fees, funding).
  * A LOSS (delta < 0) is debited in full, always. A gain is credited only up to what the ledger's KNOWN components explain (price result minus fees [plus funding when it was read]); the rest is
    recorded as ``unverified_excess`` and does not size anything. Cash that nothing explains (a faucet top-up, a transfer) can therefore never raise the allocation.
  * When funding WAS read and delta differs from the ledger net by more than the tolerance, the book flags ``UNEXPLAINED_CASH`` and the caller must stop new entries (ALERT).
  * A session that did not end verified-flat is never settled here: the engine's own NOT_FLAT/ALERT handling owns it.

WHAT IT REFUSES TO DO: martingale (a losing streak shrinks size, four in a row stop entries until the owner acknowledges), leverage escalation, size from an unknown or stale cash read (-> 0), size
below the minimum tradable notional (-> 0), more exposure because things went well (the ceiling is fixed; the top of the compounding range is MAX_FRACTION of it).
"""
from __future__ import annotations

import json
import os
from decimal import Decimal, ROUND_DOWN
from typing import Any, Dict, List, Mapping, Optional

import ms_engine_state as ES

D = Decimal
INITIAL_FRACTION = D("0.4")            # day-one deployment: 40% of the ceiling, not all of it
MAX_FRACTION = D("0.8")                # even after gains the compounded size never exceeds 80% of the ceiling (the rest is a cushion for margin and slippage)
MAX_SCALE = D("2")                     # basis/start_cash is clamped here (and then by MAX_FRACTION)
MIN_NOTIONAL_USD = D("20")
MECHANICS_CAP_USD = D("100")           # a session that is not a profit candidate proves plumbing only: it does not need real size
MARGIN_PER_USD = D("1.16")             # spot leg paid in full + ~15% perp margin + 1% buffer per USD of notional
LOSS_STREAK_HALF = 2                   # from this many consecutive realised losses the size is halved
LOSS_STREAK_STOP = 4                   # ... and at this many entries stop until the owner acknowledges
DRAWDOWN_STOP_USD = D("11.10")         # twice the Testnet loss ceiling: peak basis minus basis at or beyond this stops entries
TOLERANCE_USD = D("0.05")
TOLERANCE_FRAC = D("0.05")
BOOK_SCHEMA = "capital_book.v1"
HISTORY_MAX = 200


def _d(x: Any) -> Optional[Decimal]:
    if x is None or isinstance(x, bool):
        return None
    try:
        v = D(str(x))
    except Exception:
        return None
    return v if v.is_finite() else None


def _q(x: Decimal) -> Decimal:
    return x.quantize(D("0.01"), rounding=ROUND_DOWN)


class Allocation:
    def __init__(self, notional: Decimal, binding: str, reasons: List[str], detail: Mapping[str, Any]) -> None:
        self.notional, self.binding, self.reasons, self.detail = notional, binding, reasons, dict(detail)

    def as_dict(self) -> Dict[str, Any]:
        return {"notional_usd": str(self.notional), "binding_limit": self.binding, "reasons": list(self.reasons), **self.detail}


def allocate(*, grant_max_usd: Any, ceiling_usd: Any, open_gross_usd: Any, available_usdt: Any, basis: Optional[Decimal], start_cash: Optional[Decimal], loss_streak: int,
             drawdown_usd: Optional[Decimal], profit_seeking: bool, halted: bool = False) -> Allocation:
    """The smallest of every limit; a missing input is a refusal, never a guess."""
    reasons: List[str] = []
    gm, ce, og, av = _d(grant_max_usd), _d(ceiling_usd), _d(open_gross_usd), _d(available_usdt)
    if halted:
        reasons.append("LOSS_STREAK_STOP_NOT_ACKNOWLEDGED")
    if None in (gm, ce, og) or gm is None or ce is None or og is None or gm <= 0 or ce <= 0 or og < 0:
        reasons.append("LIMITS_UNKNOWN")
    if av is None or av <= 0:
        reasons.append("AVAILABLE_CASH_UNKNOWN_OR_ZERO")
    if basis is None or start_cash is None or start_cash <= 0 or basis < 0:
        reasons.append("CAPITAL_BASIS_UNKNOWN")
    if drawdown_usd is not None and drawdown_usd >= DRAWDOWN_STOP_USD:
        reasons.append("DRAWDOWN_LIMIT")
    if loss_streak >= LOSS_STREAK_STOP:
        reasons.append("LOSS_STREAK_STOP")
    if reasons:
        return Allocation(D(0), reasons[0], reasons, {})
    assert gm is not None and ce is not None and og is not None and av is not None and basis is not None and start_cash is not None
    scale = min(max(basis / start_cash, D(0)), MAX_SCALE)
    compounded = min(ce * INITIAL_FRACTION * scale, ce * MAX_FRACTION)
    if loss_streak >= LOSS_STREAK_HALF:
        compounded = compounded / 2
    caps = {"GRANT_MAX": gm, "AGGREGATE_CEILING_LEFT": ce - og, "COMPOUNDED_SIZE": compounded, "MARGIN": av / MARGIN_PER_USD}
    if not profit_seeking:
        caps["MECHANICS_ONLY"] = MECHANICS_CAP_USD
    binding = min(caps, key=lambda k: caps[k])
    size = _q(caps[binding])
    detail = {"basis_usd": str(basis), "start_cash_usd": str(start_cash), "scale": str(scale.quantize(D("0.0001"))), "loss_streak": loss_streak, "caps": {k: str(_q(v)) for k, v in caps.items()}}
    if size < MIN_NOTIONAL_USD:
        return Allocation(D(0), "BELOW_MIN_NOTIONAL", ["BELOW_MIN_NOTIONAL"], detail)
    return Allocation(size, binding, [], detail)


class CapitalBook:
    """Persistent record of VERIFIED cash outcomes (``capital.json`` in the engine root). Idempotent by session id; every write is atomic."""

    def __init__(self, root: str) -> None:
        self.path = os.path.join(root, "capital.json")

    def load(self) -> Dict[str, Any]:
        try:
            with open(self.path, encoding="utf-8") as fh:
                d = json.load(fh)
            if isinstance(d, dict) and d.get("schema") == BOOK_SCHEMA:
                return d
        except (OSError, ValueError):
            pass
        return {"schema": BOOK_SCHEMA, "start_cash_usd": None, "realised_usd": "0", "peak_basis_usd": None, "loss_streak": 0, "halted": False, "pending": None, "history": [],
                "unverified_excess_usd": "0"}

    def _save(self, b: Mapping[str, Any]) -> None:
        ES.write_json_atomic(self.path, b)

    # ---- the baseline: the FIRST flat cash read ever seen (kept for the life of the book)
    def ensure_baseline(self, usdt_now: Any) -> Dict[str, Any]:
        b = self.load()
        v = _d(usdt_now)
        if b["start_cash_usd"] is None and v is not None and v > 0:
            b["start_cash_usd"], b["peak_basis_usd"] = str(v), str(v)
            self._save(b)
        return b

    def basis(self, b: Optional[Mapping[str, Any]] = None) -> Optional[Decimal]:
        b = b or self.load()
        s = _d(b.get("start_cash_usd"))
        return None if s is None else s + (_d(b.get("realised_usd")) or D(0))

    def drawdown(self, b: Optional[Mapping[str, Any]] = None) -> Optional[Decimal]:
        b = b or self.load()
        bs, pk = self.basis(b), _d(b.get("peak_basis_usd"))
        return None if bs is None or pk is None else max(D(0), pk - bs)

    # ---- a session that ended verified-flat is parked until the NEXT flat cash read says what the cash really did
    def park(self, session_id: str, usdt_before: Any, ledger_net_known: Any, funding_was_read: bool, flat_verified: bool) -> None:
        b = self.load()
        if not flat_verified or _d(usdt_before) is None:
            return                                                    # not settled here: NOT_FLAT / unknown cash is the engine's ALERT, not a compounding input
        if any(h.get("session_id") == session_id for h in b["history"]) or (b["pending"] or {}).get("session_id") == session_id:
            return
        kn = _d(ledger_net_known)
        b["pending"] = {"session_id": session_id, "usdt_before": str(_d(usdt_before)), "ledger_net_known_usd": None if kn is None else str(kn), "funding_was_read": bool(funding_was_read)}
        self._save(b)

    def settle(self, usdt_now: Any) -> Dict[str, Any]:
        """Called with the first flat cash read after a parked session. Returns {status, delta, credited, ...}; NOTHING pending -> NO_PENDING."""
        b = self.load()
        p = b.get("pending")
        now = _d(usdt_now)
        if not p:
            return {"status": "NO_PENDING"}
        if now is None:
            return {"status": "CASH_UNKNOWN"}                           # stays pending: nothing is credited on a cash read that failed
        before = D(p["usdt_before"])
        delta = now - before
        known = _d(p.get("ledger_net_known_usd"))
        tol = max(TOLERANCE_USD, abs(known if known is not None else delta) * TOLERANCE_FRAC)
        status, credited, excess = "SETTLED", D(0), D(0)
        if delta < 0:
            credited = delta                                            # a realised loss is always booked in full
            if p.get("funding_was_read") and known is not None and abs(delta - known) > tol:
                status = "UNEXPLAINED_CASH"
        elif known is None:
            excess = delta                                              # nothing verifiable explains a gain: it does not size anything
            status = "UNVERIFIED_GAIN" if delta > tol else "SETTLED"
        else:
            credited = min(delta, max(known, D(0)))
            excess = delta - credited
            if p.get("funding_was_read") and abs(delta - known) > tol:
                status = "UNEXPLAINED_CASH"
            elif excess > tol:
                status = "UNVERIFIED_EXCESS"
        if status == "UNEXPLAINED_CASH" and credited > 0:                # unreconciled profit never sizes anything; an unexplained LOSS is still debited in full
            excess, credited = excess + credited, D(0)
        b["realised_usd"] = str((_d(b["realised_usd"]) or D(0)) + credited)
        b["unverified_excess_usd"] = str((_d(b["unverified_excess_usd"]) or D(0)) + excess)
        bs = self.basis(b)
        if bs is not None and (_d(b.get("peak_basis_usd")) is None or bs > D(b["peak_basis_usd"])):
            b["peak_basis_usd"] = str(bs)
        b["loss_streak"] = int(b.get("loss_streak", 0)) + 1 if credited < 0 else 0
        if b["loss_streak"] >= LOSS_STREAK_STOP:
            b["halted"] = True
        entry = {"session_id": p["session_id"], "delta_usd": str(delta), "credited_usd": str(credited), "unverified_excess_usd": str(excess), "status": status,
                 "usdt_before": p["usdt_before"], "usdt_after": str(now)}
        b["history"] = (list(b["history"]) + [entry])[-HISTORY_MAX:]
        b["pending"] = None
        self._save(b)
        return {"status": status, **entry}

    def acknowledge_halt(self) -> bool:
        b = self.load()
        if not b.get("halted"):
            return False
        b["halted"], b["loss_streak"] = False, 0
        self._save(b)
        return True

    def report(self) -> Dict[str, Any]:
        b = self.load()
        wins = [h for h in b["history"] if D(h["credited_usd"]) > 0]
        losses = [h for h in b["history"] if D(h["credited_usd"]) < 0]
        gp, gl = sum((D(h["credited_usd"]) for h in wins), D(0)), -sum((D(h["credited_usd"]) for h in losses), D(0))
        bs = self.basis(b)
        return {"schema": BOOK_SCHEMA, "start_cash_usd": b["start_cash_usd"], "verified_realised_usd": b["realised_usd"], "basis_usd": None if bs is None else str(bs),
                "unverified_excess_usd": b["unverified_excess_usd"], "lifecycles_settled": len(b["history"]), "wins": len(wins), "losses": len(losses),
                "profit_factor": None if gl == 0 else str((gp / gl).quantize(D("0.01"))), "max_drawdown_now_usd": None if self.drawdown(b) is None else str(self.drawdown(b)),
                "loss_streak": b["loss_streak"], "halted": b["halted"], "pending": b["pending"],
                "label": "TESTNET SIMULATION: fake money; verified realised cash only; not real-world performance"}
