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
  * A session that did not end verified-flat is parked too, but only its LOSSES count (debited in full); a gain from it is never credited.
  * The owner declares deposits/withdrawals (``declare_transfer``, idempotent by event id): they are removed from the next settlement's delta so a transfer is never a profit and never a mystery.
  * A RESERVE share of every credited gain stays out of the sizing basis (never auto-reinvested). Every read-modify-write holds an exclusive file lock, so concurrent callers cannot double-credit.

WHAT IT REFUSES TO DO: martingale (a losing streak shrinks size, four in a row stop entries until the owner acknowledges), leverage escalation, size from an unknown or stale cash read (-> 0), size
below the minimum tradable notional (-> 0), more exposure because things went well (the ceiling is fixed; the top of the compounding range is MAX_FRACTION of it).
"""
from __future__ import annotations

import fcntl
import json
import os
import sys
import time
from contextlib import contextmanager
from decimal import Decimal, ROUND_DOWN
from typing import Any, Dict, List, Mapping, Optional

BOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BOT not in sys.path:
    sys.path.insert(0, BOT)

import economic_model as EM  # noqa: E402
import ms_engine_state as ES  # noqa: E402

D = Decimal
INITIAL_FRACTION = D("0.4")            # day-one deployment: 40% of the ceiling, not all of it
MAX_FRACTION = D("0.8")                # even after gains the compounded size never exceeds 80% of the ceiling (the rest is a cushion for margin and slippage)
MAX_SCALE = D("2")                     # basis/start_cash is clamped here (and then by MAX_FRACTION)
MIN_NOTIONAL_USD = D("20")
MECHANICS_CAP_USD = D("100")           # a session that is not a profit candidate proves plumbing only: it does not need real size
#: Cash a session COMMITS per USD of notional at the leverage the autopilot plans with (1): the spot leg paid in full + the perp initial margin (1/leverage) + the plan's 2% buffer = 2.04. It is the plan's
#: OWN definition (``economic_model.hedged_pair_capital_usd``, which ``ms_testnet_session.build_plan`` enforces), not a constant someone typed: the old 1.16 (a research overlay assumption: BTC already
#: held + ~15% perp margin) sized $200 on $232 of cash while the plan then demanded $408 and refused.
MARGIN_PER_USD = EM.hedged_pair_capital_usd(1, 1).value
LOSS_STREAK_HALF = 2                   # from this many consecutive realised losses the size is halved
LOSS_STREAK_STOP = 4                   # ... and at this many entries stop until the owner acknowledges
DRAWDOWN_STOP_USD = D("11.10")         # twice the Testnet loss ceiling: peak basis minus basis at or beyond this stops entries
TOLERANCE_USD = D("0.05")
TOLERANCE_FRAC = D("0.05")
DEFAULT_RESERVE_FRACTION = D("0.25")  # of every VERIFIED realised gain, this share is withheld: it never enters the sizing basis (owner-configurable, 0..0.9)
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
             drawdown_usd: Optional[Decimal], profit_seeking: bool, halted: bool = False, leverage: int = 1) -> Allocation:
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
    caps = {"GRANT_MAX": gm, "AGGREGATE_CEILING_LEFT": ce - og, "COMPOUNDED_SIZE": compounded, "MARGIN": av / EM.hedged_pair_capital_usd(1, max(1, int(leverage))).value}
    if not profit_seeking:
        caps["MECHANICS_ONLY"] = MECHANICS_CAP_USD
    binding = min(caps, key=lambda k: caps[k])
    size = _q(caps[binding])
    detail = {"basis_usd": str(basis), "start_cash_usd": str(start_cash), "scale": str(scale.quantize(D("0.0001"))), "loss_streak": loss_streak, "caps": {k: str(_q(v)) for k, v in caps.items()}}
    if size < MIN_NOTIONAL_USD:
        return Allocation(D(0), "BELOW_MIN_NOTIONAL", ["BELOW_MIN_NOTIONAL"], detail)
    return Allocation(size, binding, [], detail)


class BookUnreadable(Exception):
    """``capital.json`` exists but is not a valid book. Entries must stop until the owner has looked (the file is left as it is)."""


class CapitalBook:
    """Persistent record of VERIFIED cash outcomes (``capital.json`` in the engine root). Idempotent by session id; every write is atomic; every read-modify-write is under an exclusive lock."""

    def __init__(self, root: str) -> None:
        self.root = root
        self.path = os.path.join(root, "capital.json")

    @contextmanager
    def _locked(self):
        os.makedirs(self.root, mode=0o700, exist_ok=True)
        fd = os.open(os.path.join(self.root, "capital.lock"), os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    def _fresh(self) -> Dict[str, Any]:
        return {"schema": BOOK_SCHEMA, "start_cash_usd": None, "realised_usd": "0", "gains_usd": "0", "losses_usd": "0", "peak_basis_usd": None, "loss_streak": 0, "halted": False,
                "pending": None, "history": [], "unverified_excess_usd": "0", "reserve_fraction": str(DEFAULT_RESERVE_FRACTION), "transfers_pending": [], "transfers_applied": [],
                "last_cash_usdt": None, "last_cash_ms": None}

    def load(self) -> Dict[str, Any]:
        """The book, a fresh one ONLY when no file exists. A file that is present but unreadable, malformed or of another schema is NOT a fresh book: silently starting over would reset the baseline,
        the loss streak and the halt flag (a corrupted or tampered file would clear a stop). It raises ``BookUnreadable`` and leaves the file untouched as evidence."""
        try:
            with open(self.path, encoding="utf-8") as fh:
                d = json.load(fh)
        except FileNotFoundError:
            return self._fresh()
        except (OSError, ValueError) as exc:
            raise BookUnreadable(f"capital book {self.path!r} exists but cannot be read ({type(exc).__name__}); it is NOT treated as a fresh book") from None
        if not (isinstance(d, dict) and d.get("schema") == BOOK_SCHEMA):
            raise BookUnreadable(f"capital book {self.path!r} has an unknown schema; it is NOT treated as a fresh book")
        missing = [k for k in ("start_cash_usd", "realised_usd", "loss_streak", "halted", "history", "pending") if k not in d]
        if missing:                                                    # schema-valid but stripped of the state that stops entries: also NOT a fresh book
            raise BookUnreadable(f"capital book {self.path!r} lacks {missing}; it is NOT treated as a fresh book")
        return {**self._fresh(), **d}

    def _save(self, b: Mapping[str, Any]) -> None:
        ES.write_json_atomic(self.path, b)

    # ---- the baseline: the FIRST flat cash read ever seen (kept for the life of the book)
    def ensure_baseline(self, usdt_now: Any) -> Dict[str, Any]:
        with self._locked():
            b = self.load()
            v = _d(usdt_now)
            if v is not None and v > 0:
                b["last_cash_usdt"], b["last_cash_ms"] = str(v), int(time.time() * 1000)
                if b["start_cash_usd"] is None:
                    b["start_cash_usd"], b["peak_basis_usd"] = str(v), str(v)
                self._save(b)
            return b

    def basis(self, b: Optional[Mapping[str, Any]] = None) -> Optional[Decimal]:
        """Verified equity basis = baseline cash + verified realised result (drawdown is measured on this)."""
        b = b or self.load()
        s = _d(b.get("start_cash_usd"))
        return None if s is None else s + (_d(b.get("realised_usd")) or D(0))

    def reserve_usd(self, b: Optional[Mapping[str, Any]] = None) -> D:
        b = b or self.load()
        frac = _d(b.get("reserve_fraction"))
        frac = DEFAULT_RESERVE_FRACTION if frac is None else frac            # `or` would read the owner's explicit 0 as UNSET (Decimal 0 is falsy)
        return (_d(b.get("gains_usd")) or D(0)) * frac

    def sizing_basis(self, b: Optional[Mapping[str, Any]] = None) -> Optional[Decimal]:
        """What the allocator may compound: the verified basis MINUS the reserve withheld from gains (losses are never softened)."""
        b = b or self.load()
        full = self.basis(b)
        return None if full is None else full - self.reserve_usd(b)

    def drawdown(self, b: Optional[Mapping[str, Any]] = None) -> Optional[Decimal]:
        b = b or self.load()
        bs, pk = self.basis(b), _d(b.get("peak_basis_usd"))
        return None if bs is None or pk is None else max(D(0), pk - bs)

    def set_reserve_fraction(self, frac: Any) -> bool:
        f = _d(frac)
        if f is None or not (D(0) <= f <= D("0.9")):
            return False
        with self._locked():
            b = self.load()
            b["reserve_fraction"] = str(f)
            self._save(b)
        return True

    # ---- owner-declared deposits / withdrawals: removed from the next delta (never a profit, never a mystery); idempotent by event id
    def declare_transfer(self, event_id: str, amount_usd: Any) -> bool:
        a = _d(amount_usd)
        if not event_id or a is None or a == 0:
            return False
        with self._locked():
            b = self.load()
            if any(t["event_id"] == event_id for t in b["transfers_pending"] + b["transfers_applied"]):
                return False
            b["transfers_pending"] = list(b["transfers_pending"]) + [{"event_id": event_id, "amount_usd": str(a), "declared_ms": int(time.time() * 1000)}]
            self._save(b)
        return True

    # ---- a session is parked until the NEXT cash read says what the cash really did (a session that ended NOT flat is parked as well: its losses count, its gains never do)
    def park(self, session_id: str, usdt_before: Any, ledger_net_known: Any, funding_was_read: bool, flat_verified: bool, cash_read_ms: Optional[int] = None) -> bool:
        """Park a session for settlement. True = parked (or already parked / already settled: idempotent); False = REFUSED: a different session is still unsettled (its cash result must be booked
        first, never overwritten) or there is no trustworthy starting cash."""
        if _d(usdt_before) is None:
            return False                                              # no trustworthy starting cash: nothing can be settled later (the engine's NOT_FLAT/ALERT handling still applies)
        with self._locked():
            b = self.load()
            if any(h.get("session_id") == session_id for h in b["history"]) or (b["pending"] or {}).get("session_id") == session_id:
                return True
            if b["pending"]:
                return False
            kn = _d(ledger_net_known)
            b["pending"] = {"session_id": session_id, "usdt_before": str(_d(usdt_before)), "ledger_net_known_usd": None if kn is None else str(kn), "funding_was_read": bool(funding_was_read),
                            "flat_verified": bool(flat_verified), "parked_ms": int(time.time() * 1000),
                            "cash_read_ms": int(cash_read_ms) if cash_read_ms is not None else int(time.time() * 1000)}
            self._save(b)
            return True

    def update_pending(self, session_id: str, ledger_net_known: Any, funding_was_read: bool, flat_verified: bool) -> bool:
        """After the session ends: replace the conservative placeholder parked BEFORE execution (not flat, nothing known) with what the session actually established. A crash before this call
        leaves the placeholder, so the next settlement still debits the loss and never credits a gain."""
        with self._locked():
            b = self.load()
            p = b.get("pending")
            if not p or p.get("session_id") != session_id:
                return False
            kn = _d(ledger_net_known)
            p.update({"ledger_net_known_usd": None if kn is None else str(kn), "funding_was_read": bool(funding_was_read), "flat_verified": bool(flat_verified)})
            b["pending"] = p
            self._save(b)
            return True

    def settle(self, usdt_now: Any) -> Dict[str, Any]:
        """Called with the first cash read after a parked session. Returns {status, delta, credited, ...}; NOTHING pending -> NO_PENDING."""
        with self._locked():
            b = self.load()
            now = _d(usdt_now)
            if now is not None:
                b["last_cash_usdt"], b["last_cash_ms"] = str(now), int(time.time() * 1000)
            p = b.get("pending")
            if not p:
                self._save(b)
                return {"status": "NO_PENDING"}
            if now is None:
                return {"status": "CASH_UNKNOWN"}                       # stays pending: nothing is credited on a cash read that failed
            parked_ms = int(p.get("cash_read_ms") or p.get("parked_ms") or 0)       # the instant the starting cash was READ (a deposit declared between that read and the park is not in it)
            # a transfer declared BEFORE the starting cash was read was already inside it: it is not part of this session's delta (recorded as applied, with its reason)
            counted = [t for t in b["transfers_pending"] if int(t.get("declared_ms") or 0) >= parked_ms]
            transfers = sum((D(t["amount_usd"]) for t in counted), D(0))
            before = D(p["usdt_before"])
            delta = now - before - transfers                            # cash that moved BECAUSE OF TRADING (declared deposits/withdrawals removed)
            known = _d(p.get("ledger_net_known_usd"))
            flat = p.get("flat_verified", True)
            tol = max(TOLERANCE_USD, abs(known if known is not None else delta) * TOLERANCE_FRAC)
            status, credited, excess = "SETTLED", D(0), D(0)
            if delta < 0:
                credited = delta                                        # a realised loss is always booked in full
                if p.get("funding_was_read") and known is not None and abs(delta - known) > tol:
                    status = "UNEXPLAINED_CASH"
            elif known is None:
                excess = delta                                          # nothing verifiable explains a gain: it does not size anything
                status = "UNVERIFIED_GAIN" if delta > tol else "SETTLED"
            else:
                credited = min(delta, max(known, D(0)))
                excess = delta - credited
                if p.get("funding_was_read") and abs(delta - known) > tol:
                    status = "UNEXPLAINED_CASH"
                elif excess > tol:
                    status = "UNVERIFIED_EXCESS"
            if (status == "UNEXPLAINED_CASH" or not flat) and credited > 0:   # unreconciled / not-flat profit never sizes anything; an unexplained or not-flat LOSS is still debited in full
                excess, credited = excess + credited, D(0)
                status = status if status == "UNEXPLAINED_CASH" else "NOT_FLAT_SESSION"
            b["realised_usd"] = str((_d(b["realised_usd"]) or D(0)) + credited)
            if credited > 0:
                b["gains_usd"] = str((_d(b["gains_usd"]) or D(0)) + credited)
            elif credited < 0:
                b["losses_usd"] = str((_d(b["losses_usd"]) or D(0)) + credited)
            b["unverified_excess_usd"] = str((_d(b["unverified_excess_usd"]) or D(0)) + excess)
            bs = self.basis(b)
            if bs is not None and (_d(b.get("peak_basis_usd")) is None or bs > D(b["peak_basis_usd"])):
                b["peak_basis_usd"] = str(bs)
            if credited < 0:
                b["loss_streak"] = int(b.get("loss_streak", 0)) + 1
            elif credited > 0:
                b["loss_streak"] = 0                                    # only a VERIFIED credited gain breaks a losing streak: an unverified gain or a quiet session does not
            if b["loss_streak"] >= LOSS_STREAK_STOP:
                b["halted"] = True
            entry = {"session_id": p["session_id"], "delta_usd": str(delta), "transfers_removed_usd": str(transfers), "credited_usd": str(credited), "unverified_excess_usd": str(excess),
                     "status": status, "usdt_before": p["usdt_before"], "usdt_after": str(now), "flat_verified": bool(flat)}
            b["history"] = (list(b["history"]) + [entry])[-HISTORY_MAX:]
            b["transfers_applied"] = (list(b["transfers_applied"]) + [{**t, "applied_to": p["session_id"], "counted": t in counted} for t in b["transfers_pending"]])[-HISTORY_MAX:]
            b["transfers_pending"] = []
            b["pending"] = None
            self._save(b)
            return {"status": status, **entry}

    def acknowledge_halt(self) -> bool:
        with self._locked():
            b = self.load()
            if not b.get("halted"):
                return False
            b["halted"], b["loss_streak"] = False, 0
            self._save(b)
            return True

    def acknowledge_drawdown(self) -> bool:
        """The owner's explicit decision, after reviewing the losses, to resume from the CURRENT verified basis: the drawdown is measured from a new peak. Nothing is credited; the
        verified result and the history are untouched. False when there is no drawdown to acknowledge."""
        with self._locked():
            b = self.load()
            bs, pk = self.basis(b), _d(b.get("peak_basis_usd"))
            if bs is None or pk is None or pk <= bs:
                return False
            b["peak_basis_usd"] = str(bs)
            b.setdefault("drawdown_acknowledged", []).append({"from_peak_usd": str(pk), "to_basis_usd": str(bs), "at_ms": int(time.time() * 1000)})
            self._save(b)
            return True

    def report(self) -> Dict[str, Any]:
        b = self.load()
        wins = [h for h in b["history"] if D(h["credited_usd"]) > 0]
        losses = [h for h in b["history"] if D(h["credited_usd"]) < 0]
        gp, gl = sum((D(h["credited_usd"]) for h in wins), D(0)), -sum((D(h["credited_usd"]) for h in losses), D(0))
        bs, sb = self.basis(b), self.sizing_basis(b)
        return {"schema": BOOK_SCHEMA, "start_cash_usd": b["start_cash_usd"], "last_cash_usdt": b["last_cash_usdt"], "verified_realised_usd": b["realised_usd"], "verified_gains_usd": b["gains_usd"],
                "verified_losses_usd": b["losses_usd"], "basis_usd": None if bs is None else str(bs), "reserve_fraction": b["reserve_fraction"], "reserve_usd": str(self.reserve_usd(b)),
                "sizing_basis_usd": None if sb is None else str(sb), "unverified_excess_usd": b["unverified_excess_usd"], "transfers_pending": b["transfers_pending"],
                "lifecycles_settled": len(b["history"]), "wins": len(wins), "losses": len(losses), "profit_factor": None if gl == 0 else str((gp / gl).quantize(D("0.01"))),
                "max_drawdown_now_usd": None if self.drawdown(b) is None else str(self.drawdown(b)), "loss_streak": b["loss_streak"], "halted": b["halted"], "pending": b["pending"],
                "label": "TESTNET SIMULATION: fake money; verified realised cash only; not real-world performance"}
