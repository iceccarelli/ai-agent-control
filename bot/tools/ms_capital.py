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

import contextlib
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
import ms_capital_anchor as AN  # noqa: E402
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
#: The VERIFIED realised result (gains net of losses, since the book began) at or beyond which entries stop for good: twice the drawdown stop, four per-session loss ceilings. The loss-streak halt and
#: the drawdown stop are the owner's to acknowledge after a review; this one is not, so an owner (or a script, or a habit) that acknowledges every stop cannot walk the account down without limit
#: (red-team F2: five rounds of "four losses, ack-halt, ack-drawdown" reached -60 and the next size was still 198.80). It derives from the realised figure alone, which no acknowledgement edits; lifting
#: it means a NEW EPOCH on purpose (the owner's interactive, typed, audited ``capital --rebaseline``, which the lineage and the witness keep for good; RUNBOOKS), never an acknowledgement.
LIFETIME_LOSS_STOP_USD = DRAWDOWN_STOP_USD * 2
TOLERANCE_USD = D("0.05")
TOLERANCE_FRAC = D("0.05")
#: A declared deposit or withdrawal is the OWNER'S UNVERIFIED CLAIM about cash that moved (the book has no network and cannot see the venue's transfer history). Nothing on this Testnet stack moves a
#: million USD: a larger figure is a typo (or an attempt to make the delta arithmetic overflow), refused outright. A withdrawal is also refused when it exceeds the cash the book knows of.
MAX_DECLARED_TRANSFER_USD = D("1000000")
DEFAULT_RESERVE_FRACTION = D("0.25")  # of every VERIFIED realised gain, this share is withheld: it never enters the sizing basis (owner-configurable, 0..0.9)
BOOK_SCHEMA = "capital_book.v1"
HISTORY_MAX = AN.HISTORY_MAX
#: A cash movement between two sessions (nothing pending, so nothing explains it) smaller than this is noise (a late rebate, a rounding cent); at or above it the book books it (a loss in full, a gain as unverified
#: excess) and asks for a review when cash FELL. It is what makes a rolled-back local state visible against the venue's own cash: the cash that went missing is booked again.
GAP_TOLERANCE_USD = D("0.25")


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
             drawdown_usd: Optional[Decimal], profit_seeking: bool, realised_usd: Optional[Decimal], halted: bool = False, leverage: int = 1) -> Allocation:
    """The smallest of every limit; a missing input is a refusal, never a guess. ``realised_usd`` (the book's verified net result since it began) arms the lifetime stop no acknowledgement lifts. It is a
    REQUIRED keyword and an unknown figure is a refusal: the stop used to be skipped when the argument was left out or unreadable."""
    reasons: List[str] = []
    gm, ce, og, av = _d(grant_max_usd), _d(ceiling_usd), _d(open_gross_usd), _d(available_usdt)
    if realised_usd is None:
        reasons.append("REALISED_UNKNOWN")
    elif realised_usd <= -LIFETIME_LOSS_STOP_USD:
        reasons.append("LIFETIME_LOSS_STOP")
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


class BookRefused(BookUnreadable):
    """The anchored book may not be used (see ``ms_capital_anchor``): ``code`` is the stable refusal (ACCOUNT_NOT_INITIALISED, VIEW_TAMPERED, ANCHOR_BEHIND_BOOK, ...). Subclasses ``BookUnreadable`` so every
    existing handler that stops entries on an unusable book stops on this too; nothing was rewritten."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code, self.detail = code, detail


class CapitalBook:
    """Persistent record of VERIFIED cash outcomes (``capital.json`` in the engine root). Idempotent by session id; every write is atomic; every read-modify-write is under an exclusive lock.

    TWO MODES. ``CapitalBook(root)`` is the bare arithmetic book (unit tests only: its integrity is bound to its own file, which is exactly the weakness ``ms_capital_anchor`` documents). Production code opens it with
    ``CapitalBook.anchored(...)``: the book is then a VIEW of an account-bound, append-only lineage kept outside ``--root``; every mutation appends a lineage entry first (the commit point) and writes the view after it,
    a deleted, restored or relocated view is rebuilt from the lineage, and a book that does not match its lineage refuses (``BookRefused``). A bare book refuses to write over a book that is bound to a lineage."""

    def __init__(self, root: str, anchor: Optional["AN.Anchor"] = None) -> None:
        self.root = root
        self.path = os.path.join(root, "capital.json")
        self.anchor = anchor
        self._depth = 0

    @classmethod
    def anchored(cls, root: str, *, account_ref: Optional[str], venue: str = "testnet", anchor_dir: Optional[str] = None, env: Optional[Mapping[str, str]] = None, witness: Any = None,
                 clock: Optional[Any] = None, read_only: bool = False, check_witness: bool = True) -> "CapitalBook":
        """The production book. Refuses (``BookRefused``) unless the account is stated by the venue, a lineage exists for it, and the view matches the lineage. ``read_only`` opens it for reporting only: nothing is
        written, healed, proposed or repaired. The remote witness (when configured) is consulted once, here."""
        if account_ref is None:
            raise BookRefused("ACCOUNT_IDENTITY_UNAVAILABLE", "the venue did not state which account these credentials belong to (no userID), so the capital book cannot be bound to it")
        try:
            an = AN.Anchor(root, account_ref, venue, anchor_dir or AN.anchor_dir_for(env), witness=witness, clock=clock, verify_only=read_only)
        except AN.AnchorRefused as exc:
            raise BookRefused(exc.code, exc.detail) from None
        book = cls(root, an)
        book._open_check(check_witness)
        return book

    def barrier(self) -> str:
        """The last gate before an order: the lineage head (including the PARK placeholder just written) must be known to the remote witness. With a REQUIRED witness an unreachable witness or an unacknowledged head
        refuses (``BookRefused``: WITNESS_UNAVAILABLE, transient); with an optional one the state is returned ("UNAVAILABLE") and reported. NOT_CONFIGURED when there is no witness."""
        if self.anchor is None or not getattr(self.anchor.witness, "configured", False):
            return "NOT_CONFIGURED"
        try:
            with self._locked():
                self.anchor.open_view()
                return self.anchor.check_witness()
        except AN.AnchorRefused as exc:
            raise BookRefused(exc.code, exc.detail) from None

    def _open_check(self, check_witness: bool) -> None:
        try:
            with self._locked():
                self.anchor.open_view()                                                    # type: ignore[union-attr]
                if check_witness:
                    self.anchor.check_witness()                                            # type: ignore[union-attr]
        except AN.AnchorRefused as exc:
            raise BookRefused(exc.code, exc.detail) from None

    @contextmanager
    def _root_lock(self):
        os.makedirs(self.root, mode=0o700, exist_ok=True)
        fd = os.open(os.path.join(self.root, "capital.lock"), os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
        try:
            try:
                AN.flock_with_timeout(fd, AN.LOCK_TIMEOUT_S, "the capital book lock")
            except AN.AnchorRefused as exc:
                raise BookRefused(exc.code, exc.detail) from None
            yield
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    @contextmanager
    def _locked(self):
        """The anchor lock first (every root that shares an account serialises there), then the root lock. Re-entrant for one instance: an outer lock covers a ``load()`` made inside it. A lock that cannot be
        taken within ``AN.LOCK_TIMEOUT_S`` is a transient refusal (LOCK_TIMEOUT), never an indefinite wait."""
        if self._depth > 0:
            self._depth += 1
            try:
                yield
            finally:
                self._depth -= 1
            return
        with contextlib.ExitStack() as stack:
            try:
                if self.anchor is not None:
                    stack.enter_context(self.anchor.locked())
                if self.anchor is None or not self.anchor.verify_only:       # a read-only open takes no root lock and creates nothing
                    stack.enter_context(self._root_lock())
            except AN.AnchorRefused as exc:
                raise BookRefused(exc.code, exc.detail) from None
            self._depth = 1
            try:
                yield
            finally:
                self._depth = 0

    def _fresh(self) -> Dict[str, Any]:
        return {"schema": BOOK_SCHEMA, "start_cash_usd": None, "realised_usd": "0", "gains_usd": "0", "losses_usd": "0", "peak_basis_usd": None, "loss_streak": 0, "halted": False,
                "pending": None, "history": [], "unverified_excess_usd": "0", "reserve_fraction": str(DEFAULT_RESERVE_FRACTION), "transfers_pending": [], "transfers_applied": [],
                "last_cash_usdt": None, "last_cash_ms": None, "review_required": None, "epoch": 0, "epochs": []}

    def load(self) -> Dict[str, Any]:
        """The book, a fresh one ONLY when no file exists (bare mode). A file that is present but unreadable, malformed or of another schema is NOT a fresh book: silently starting over would reset the baseline,
        the loss streak and the halt flag (a corrupted or tampered file would clear a stop). It raises ``BookUnreadable`` and leaves the file untouched as evidence. An ANCHORED book is the lineage's view: it is
        verified against the lineage, rebuilt forward when it is missing or older, and refused (``BookRefused``) when it does not match."""
        if self.anchor is not None:
            return self._load_anchored()
        try:
            with open(self.path, encoding="utf-8") as fh:
                d = json.load(fh)
        except FileNotFoundError:
            return self._fresh()
        except (OSError, ValueError) as exc:
            raise BookUnreadable(f"capital book {self.path!r} exists but cannot be read ({type(exc).__name__}); it is NOT treated as a fresh book") from None
        return self._from_dict(d)

    def _from_dict(self, d: Any) -> Dict[str, Any]:
        if not (isinstance(d, dict) and d.get("schema") == BOOK_SCHEMA):
            raise BookUnreadable(f"capital book {self.path!r} has an unknown schema; it is NOT treated as a fresh book")
        missing = [k for k in ("start_cash_usd", "realised_usd", "loss_streak", "halted", "history", "pending") if k not in d]
        if missing:                                                    # schema-valid but stripped of the state that stops entries: also NOT a fresh book
            raise BookUnreadable(f"capital book {self.path!r} lacks {missing}; it is NOT treated as a fresh book")
        book = {**self._fresh(), **d}
        self._validate(book)
        return book

    def _load_anchored(self) -> Dict[str, Any]:
        try:
            with self._locked():
                view = self.anchor.open_view()                                             # type: ignore[union-attr]
        except AN.AnchorRefused as exc:
            raise BookRefused(exc.code, exc.detail) from None
        return self._from_dict(view)

    def _validate(self, b: Mapping[str, Any]) -> None:
        """Every figure the stops read must be what it claims to be. A book that PARSES but holds nonsense used to LOAD: ``_d`` reads "abc" or "NaN" as None (or 0), so a corrupted or tampered ``realised_usd``
        made the basis, the drawdown check and the lifetime stop go silent (the allocator sized 200.00 where the honest book said LIFETIME_LOSS_STOP), and a string streak raised mid-session. A violation is
        ``BookUnreadable`` like any other unreadable book: entries stop, the file is left untouched as evidence, and nothing is rewritten."""
        def bad(name: str) -> None:
            raise BookUnreadable(f"capital book {self.path!r} has an invalid {name}; it is NOT treated as a fresh book")

        def number(v: Any, name: str, *, none_ok: bool = False, lo: Optional[Decimal] = None, hi: Optional[Decimal] = None, lo_open: bool = False) -> None:
            if v is None and none_ok:
                return
            x = _d(v)
            if x is None or (lo is not None and (x <= lo if lo_open else x < lo)) or (hi is not None and x > hi):
                bad(name)

        def rows(v: Any, name: str, need: Mapping[str, str]) -> None:
            if not isinstance(v, list):
                bad(name)
            for e in v:
                if not isinstance(e, dict):
                    bad(name)
                for k, kind in need.items():
                    if (kind == "str" and not (isinstance(e.get(k), str) and e[k])) or (kind == "num" and _d(e.get(k)) is None):
                        bad(f"{name}[].{k}")

        for k in ("realised_usd", "gains_usd", "losses_usd", "unverified_excess_usd"):
            number(b.get(k), k)
        number(b.get("start_cash_usd"), "start_cash_usd", none_ok=True, lo=D(0), lo_open=True)
        number(b.get("peak_basis_usd"), "peak_basis_usd", none_ok=True)
        number(b.get("last_cash_usdt"), "last_cash_usdt", none_ok=True)
        number(b.get("reserve_fraction"), "reserve_fraction", lo=D(0), hi=D("0.9"))
        ls = b.get("loss_streak")
        if isinstance(ls, bool) or not isinstance(ls, int) or ls < 0:
            bad("loss_streak")
        if not isinstance(b.get("halted"), bool):
            bad("halted")
        rows(b.get("history"), "history", {"session_id": "str", "credited_usd": "num"})
        rows(b.get("transfers_pending"), "transfers_pending", {"event_id": "str", "amount_usd": "num"})
        rows(b.get("transfers_applied"), "transfers_applied", {"event_id": "str", "amount_usd": "num"})
        p = b.get("pending")
        if p is not None:
            rows([p], "pending", {"session_id": "str", "usdt_before": "num"})
        if b.get("review_required") is not None and not isinstance(b["review_required"], dict):
            bad("review_required")
        ep = b.get("epoch")
        if isinstance(ep, bool) or not isinstance(ep, int) or ep < 0:
            bad("epoch")
        rows(b.get("epochs"), "epochs", {"realised_usd": "num"})

    def _save(self, b: Mapping[str, Any], kind: str = "", facts: Optional[Mapping[str, Any]] = None, history_add: Any = (), transfers_add: Any = ()) -> None:
        """Bare mode: an atomic file write, REFUSED over a book that is bound to a lineage (a bare writer must never edit an anchored view). Anchored mode: the lineage entry is the commit point, the view follows."""
        if self.anchor is None:
            if isinstance(b.get("ledger_ref"), dict) or self._on_disk_is_bound():
                raise BookRefused("BARE_WRITE_TO_BOUND_BOOK", "this book is bound to a capital lineage; open it with CapitalBook.anchored")
            ES.write_json_atomic(self.path, b)
            return
        self._validate({**self._fresh(), **b})                       # a book that would not LOAD is never appended: the lineage must only ever hold states every reader accepts
        try:
            self.anchor.commit(b, kind or "BASELINE", facts, history_add, transfers_add)
        except AN.AnchorRefused as exc:
            raise BookRefused(exc.code, exc.detail) from None

    def _on_disk_is_bound(self) -> bool:
        d = ES.read_json(self.path)
        return isinstance(d, dict) and isinstance(d.get("ledger_ref"), dict)

    # ---- the baseline: the FIRST flat cash read ever seen (kept for the life of the book)
    def ensure_baseline(self, usdt_now: Any) -> Dict[str, Any]:
        with self._locked():
            b = self.load()
            v = _d(usdt_now)
            if v is None or v <= 0:
                return b
            now_ms = int(time.time() * 1000)
            if b["start_cash_usd"] is None:
                # The FIRST flat cash read of an epoch. Normally nothing is parked yet and the read is the baseline. After an owner REBASELINE that fell between a cycle's settle and its park, a session IS parked and
                # its starting cash is the epoch's true start: the post-session cash would put the session's own result into the baseline and book it a second time (reproduced: 202 instead of 207).
                p = b.get("pending")
                pre = _d(p.get("usdt_before")) if isinstance(p, dict) else None
                base = pre if pre is not None and pre > 0 else v
                b["last_cash_usdt"], b["last_cash_ms"] = str(v), now_ms
                b["start_cash_usd"], b["peak_basis_usd"] = str(base), str(base)
                self._save(b, "BASELINE", {"usdt": str(v), "first": True, **({"baseline_from_pending": True} if pre is not None and pre > 0 else {})})
            elif b["pending"] is None:
                # Nothing is parked, so nothing explains a change in the venue's cash: book it (``_book_gap``). A change below the tolerance is NOT a durable event and does NOT move the reference: the cash is compared
                # with the last BOOKED read, so a slow bleed (60 idle reads of -0.2 USD, reproduced, was 12 USD that no review or streak ever saw) accumulates until it crosses the tolerance and is booked then.
                gap = self._book_gap(b, v, now_ms)
                if gap is not None:
                    b["last_cash_usdt"], b["last_cash_ms"] = str(v), now_ms
                    rows = [gap["entry"]] if gap["entry"] is not None else []
                    self._save(b, "GAP" if rows else "BASELINE", {"usdt": str(v), "first": False, "gap": gap["entry"]["status"] if gap["entry"] is not None else "TRANSFERS_CONSUMED"}, rows, gap["applied"])
            # a parked session: the same cash read is its settlement (``settle``); nothing to do here
            return b

    def _book_gap(self, b: Dict[str, Any], now: Decimal, now_ms: int) -> Optional[Dict[str, Any]]:
        """CASH CONTINUITY while nothing is pending. Between the last cash read and this one no session was parked, so nothing explains a change in the venue's cash except a declared transfer. A change at or
        above ``GAP_TOLERANCE_USD`` is booked here instead of being overwritten by the new read: a FALL in full as a realised loss (and a review is required: the stop is in this save), a rise as unverified excess
        (it sizes nothing). This is the independent witness that does not live on this machine: a restored or relocated book, a copied state directory or a rolled-back volume carries an older ``last_cash_usdt``,
        and the venue's cash re-books what went missing. Declared transfers inside the window are consumed (counted or already inside the last read), exactly as ``settle`` does. Mutates ``b``; returns None when
        nothing was booked (noise), else ``{"entry", "applied"}`` for the lineage (``entry`` is None when only declared transfers were consumed)."""
        last = _d(b.get("last_cash_usdt"))
        if last is None:
            return None
        last_ms = int(b.get("last_cash_ms") or 0)
        counted = [t for t in b["transfers_pending"] if int(t.get("declared_ms") or 0) >= last_ms]
        transfers = sum((D(t["amount_usd"]) for t in counted), D(0))
        delta = now - last - transfers
        applied = [{**t, "applied_to": f"gap@{now_ms}", "counted": t in counted} for t in b["transfers_pending"]]
        withdrawal_only = transfers < 0 and now < last and abs(delta) <= GAP_TOLERANCE_USD      # a declared withdrawal is the only thing explaining a fall: nothing measured it, so it is a review, not a quiet SETTLED
        if abs(delta) <= GAP_TOLERANCE_USD and not withdrawal_only:
            if b["transfers_pending"]:                                  # a declared transfer that explains the whole movement is consumed (no money booked, nothing left to count against a session)
                b["transfers_applied"] = (list(b["transfers_applied"]) + applied)[-HISTORY_MAX:]
                b["transfers_pending"] = []
                return {"entry": None, "applied": applied}
            return None
        credited, excess, status = D(0), D(0), "SETTLED"
        if withdrawal_only:
            status = "UNEXPLAINED_CASH"
        elif delta < 0:
            credited, status = delta, "UNEXPLAINED_CASH"
        else:
            excess, status = delta, "UNVERIFIED_GAIN"
        entry = self._post_settlement(b, session_id=f"gap@{now_ms}", delta=delta, transfers=transfers, credited=credited, excess=excess, status=status, before=last, after=now, flat=True, known=None,
                                      funding_read=False, window="IDLE_GAP", applied=applied)
        return {"entry": entry, "applied": applied}

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
            was = b["reserve_fraction"]
            b["reserve_fraction"] = str(f)
            self._save(b, "RESERVE", {"from": was, "to": str(f)})
        return True

    # ---- owner-declared deposits / withdrawals: removed from the next delta (never a profit, never a mystery); idempotent by event id
    def declare_transfer(self, event_id: str, amount_usd: Any) -> bool:
        """A declared transfer is an unverified claim: refused when it is zero, unreadable, beyond ``MAX_DECLARED_TRANSFER_USD``, or a withdrawal larger than the cash the book knows of (the last cash
        read plus every transfer still pending). ``settle`` then keeps it from hiding a measured loss or raising a credit above the cash that rose."""
        a = _d(amount_usd)
        if not event_id or a is None or a == 0 or abs(a) > MAX_DECLARED_TRANSFER_USD:
            return False
        with self._locked():
            b = self.load()
            if any(t["event_id"] == event_id for t in b["transfers_pending"] + b["transfers_applied"]):
                return False
            last = _d(b.get("last_cash_usdt"))
            if a < 0 and last is not None and -a > last + sum((D(t["amount_usd"]) for t in b["transfers_pending"]), D(0)):
                return False
            b["transfers_pending"] = list(b["transfers_pending"]) + [{"event_id": event_id, "amount_usd": str(a), "declared_ms": int(time.time() * 1000)}]
            self._save(b, "TRANSFER", {"event_id": event_id, "amount_usd": str(a), "claim": "OWNER_DECLARED_UNVERIFIED"})
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
            self._save(b, "PARK", {"session_id": session_id, "usdt_before": str(_d(usdt_before))})
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
            self._save(b, "UPDATE_PENDING", {"session_id": session_id, "ledger_net_known_usd": None if kn is None else str(kn), "funding_was_read": bool(funding_was_read), "flat_verified": bool(flat_verified)})
            return True

    def settle(self, usdt_now: Any) -> Dict[str, Any]:
        """Called with the first cash read after a parked session. Returns {status, delta, credited, ...}; NOTHING pending -> NO_PENDING."""
        with self._locked():
            b = self.load()
            now = _d(usdt_now)
            now_ms = int(time.time() * 1000)
            p = b.get("pending")
            if not p:
                # nothing parked: the cash read is judged exactly as ``ensure_baseline`` judges it (a caller that settles without a baseline call must not silently OVERWRITE the reference, which would swallow the
                # movement), and an unreadable read writes nothing at all (it used to append a durable, witnessed entry per call)
                if now is not None and now > 0 and b["start_cash_usd"] is not None:
                    gap = self._book_gap(b, now, now_ms)
                    if gap is not None:
                        b["last_cash_usdt"], b["last_cash_ms"] = str(now), now_ms
                        rows = [gap["entry"]] if gap["entry"] is not None else []
                        self._save(b, "GAP" if rows else "BASELINE", {"usdt": str(now), "first": False, "via": "settle_no_pending", "gap": gap["entry"]["status"] if gap["entry"] is not None else "TRANSFERS_CONSUMED"}, rows, gap["applied"])
                return {"status": "NO_PENDING"}
            if now is not None:
                b["last_cash_usdt"], b["last_cash_ms"] = str(now), now_ms
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
                if p.get("funding_was_read") and known is not None:
                    credited = min(delta, known)                        # the ledger saw EVERY component (funding included): the loss it saw is booked whole even when an undeclared inflow made the cash fall by less
                    if abs(delta - known) > tol:
                        status = "UNEXPLAINED_CASH"
            elif known is None:
                excess = delta                                          # nothing verifiable explains a gain: it does not size anything
                status = "UNVERIFIED_GAIN" if delta > tol else "SETTLED"
            else:
                if p.get("funding_was_read") and known < 0:
                    credited = known                                    # the ledger saw EVERY component (funding included) and the session LOST: an undeclared inflow that lifted the cash does not erase it
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
            if transfers < 0:
                # A declared WITHDRAWAL is the owner's unverified claim and it moves the settled delta UP by its size. It may explain cash that fell; it may not (1) hide a loss the ledger measured, nor
                # (2) lift a credit above the cash that actually rose, and (3) where the ledger measured nothing at all it cannot be told from a loss it could not see, so the book asks for the owner's
                # review instead of settling quietly. The ledger-vs-cash comparison is the book's only independent check of the ledger and a declaration blinds it for its window: nothing is CREDITED on the
                # strength of one. All three only ever make the book more conservative, and they bind only a declared withdrawal counted for this session.
                if known is not None and known < 0 and delta > known and credited > known:
                    credited = known                                    # the measured loss is booked whole; the part of the cash change the book cannot place is the excess (visible, sizes nothing)
                    excess = delta - credited
                    if abs(delta - known) > tol:
                        status = "UNEXPLAINED_CASH"                     # a review, never a silent SETTLED
                raw_rise = max(now - before, D(0))
                if credited > raw_rise:
                    credited, excess = raw_rise, delta - raw_rise
                    if excess > tol and status == "SETTLED":
                        status = "UNVERIFIED_EXCESS"
                if known is None and now < before and status == "SETTLED":
                    status = "UNEXPLAINED_CASH"                         # the cash fell, the ledger has no net to compare it with, and a withdrawal is claimed to explain it: a review, nothing booked on a guess
            applied = [{**t, "applied_to": p["session_id"], "counted": t in counted} for t in b["transfers_pending"]]
            entry = self._post_settlement(b, session_id=p["session_id"], delta=delta, transfers=transfers, credited=credited, excess=excess, status=status, before=D(p["usdt_before"]), after=now,
                                          flat=bool(flat), known=known, funding_read=bool(p.get("funding_was_read")), window="SESSION", applied=applied)
            b["pending"] = None
            self._save(b, "SETTLE", {"session_id": p["session_id"], "status": status, "delta_usd": str(delta), "credited_usd": str(credited), "unverified_excess_usd": str(excess),
                                     "transfers_removed_usd": str(transfers)}, [entry], applied)
            return {"status": status, **entry}

    def _post_settlement(self, b: Dict[str, Any], *, session_id: str, delta: Decimal, transfers: Decimal, credited: Decimal, excess: Decimal, status: str, before: Decimal, after: Decimal,
                         flat: bool, known: Optional[Decimal], funding_read: bool, window: str, applied: List[Dict[str, Any]]) -> Dict[str, Any]:
        """The book's bookkeeping for ONE settled cash window (a parked session, or an idle gap): realised, gains, losses, unverified excess, peak, loss streak, the halt, the history row, the review, the
        applied transfers. Mutates ``b`` (the caller saves); returns the history row. One implementation, so a session and an idle gap can never be booked by different rules."""
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
        entry = {"session_id": session_id, "delta_usd": str(delta), "transfers_removed_usd": str(transfers), "credited_usd": str(credited), "unverified_excess_usd": str(excess),
                 "status": status, "usdt_before": str(before), "usdt_after": str(after), "flat_verified": bool(flat), "ledger_net_known_usd": None if known is None else str(known),
                 "funding_was_read": bool(funding_read), "window": window}
        b["history"] = (list(b["history"]) + [entry])[-HISTORY_MAX:]
        if status == "UNEXPLAINED_CASH" and not b.get("review_required"):
            # The stop is part of THIS save. The autopilot used to write its ALERT marker in a separate file afterwards; a process killed between the two left a book that had moved on and no
            # marker, and the next session traded on cash it had already called unexplained. The book is the source of truth: every session reads this and re-asserts the ALERT.
            b["review_required"] = {"code": "ACCOUNT_CASH_UNEXPLAINED", "session_id": session_id, "delta_usd": str(delta), "known_usd": None if known is None else str(known), "set_ms": int(time.time() * 1000)}
        b["transfers_applied"] = (list(b["transfers_applied"]) + applied)[-HISTORY_MAX:]
        b["transfers_pending"] = []
        return entry

    # ---- the OWNER's acts on the lineage itself (the CLI gates each with a terminal and a typed line; nothing unattended calls these)
    def initialise(self, owner: Mapping[str, Any]) -> Dict[str, Any]:
        """GENESIS: create this account's lineage. The ONLY way a lineage begins, and it is the owner's act. A pre-anchor ``capital.json`` (or a book bound to a lineage that is gone) is ADOPTED whole and kept
        as evidence; with none, the account starts with a fresh book whose baseline is the first flat cash read. Refused when a lineage exists (it can only be extended) or when a configured witness knows the account."""
        if self.anchor is None:
            raise BookRefused("NOT_ANCHORED", "a bare book has no lineage to create")
        try:
            with self._locked():
                status, view = self.anchor._read_view()
                adopt: Optional[Dict[str, Any]] = None
                if status == "unreadable":
                    raise BookRefused("BOOK_UNREADABLE", f"capital.json cannot be read; move it aside deliberately before a new lineage is created")
                if status in ("legacy", "bound"):
                    ref = (view or {}).get("ledger_ref")
                    if isinstance(ref, dict) and ref.get("account") != self.anchor.account:
                        raise BookRefused("ACCOUNT_MISMATCH", f"the book is bound to account {ref.get('account')}, not {self.anchor.account}")
                    adopt = self._from_dict({k: v for k, v in (view or {}).items() if k != "ledger_ref"})
                    self.anchor._preserve("pre-anchor")
                return self.anchor.initialise(adopt=adopt, fresh_book=self._fresh(), owner=owner, adopt_ref=(view or {}).get("ledger_ref") if status in ("legacy", "bound") else None)
        except AN.AnchorRefused as exc:
            raise BookRefused(exc.code, exc.detail) from None

    def rebuild_view(self) -> str:
        """The owner's decision to replace a book that does not match its lineage (VIEW_TAMPERED, VIEW_UNBOUND) with the lineage's own head; the rejected file is kept. ANCHOR_BEHIND_BOOK is never rebuilt: the
        book there is NEWER than the lineage, and replacing it would be the rollback this exists to prevent."""
        if self.anchor is None:
            raise BookRefused("NOT_ANCHORED", "a bare book has no lineage")
        try:
            with self._locked():
                try:
                    self.anchor.open_view()
                    return "NOTHING_TO_REBUILD"
                except AN.AnchorRefused as exc:
                    if exc.code not in ("VIEW_TAMPERED", "VIEW_UNBOUND"):
                        raise
                entries = self.anchor.entries()
                self.anchor._preserve("rejected")
                self.anchor._heal(AN.rebuild(entries), entries[-1], "OWNER_REBUILD", None)
                return "REBUILT"
        except AN.AnchorRefused as exc:
            raise BookRefused(exc.code, exc.detail) from None

    def accept_older_lineage(self, owner: Mapping[str, Any]) -> Dict[str, Any]:
        """The owner's RECORDED decision to continue from a lineage that is OLDER than what this root remembers (``ANCHOR_BEHIND_BOOK``: an older anchor directory was restored over a newer view or high-water mark).
        Refused for every other state, and refused while a configured witness holds a newer head (``LOCAL_BEHIND_WITNESS``: nothing local may overrule it) or cannot be read when it is REQUIRED. The newer view and mark
        are kept as ``*.rejected-newer.<ms>``; a lineage entry (ACK_OLDER) carries the facts of what was discarded and the owner's gate facts, and the book then requires a review (``ROLLBACK_ACCEPTED``,
        acknowledged with ``--ack-review``) before any entry. The streak, halt, realised result and lifetime stop are those of the OLDER lineage: this is the rollback, made deliberate, visible and permanent in
        the lineage instead of silent (the cash the venue no longer matches is booked by the next session's idle-window GAP, but a faucet top-up hides that, which is why a review is required)."""
        if self.anchor is None:
            raise BookRefused("NOT_ANCHORED", "a bare book has no lineage")
        try:
            with self._locked():
                an = self.anchor
                try:
                    an.open_view()
                    return {"status": "NOTHING_TO_ACCEPT"}
                except AN.AnchorRefused as exc:
                    if exc.code != "ANCHOR_BEHIND_BOOK":
                        raise
                an.check_witness(publish=False)                                              # a witness that knows a newer head refuses HERE, before anything is written
                entries = an.entries()
                discarded = an.newer_facts()
                an._preserve("rejected-newer")
                an._preserve_path(an.hwm_path, "rejected-newer")
                b = AN.rebuild(entries)
                prev = b.get("review_required")
                b["review_required"] = {"code": "ROLLBACK_ACCEPTED", "session_id": None, "delta_usd": None, "known_usd": None, "set_ms": int(time.time() * 1000), "older_head_seq": entries[-1]["seq"],
                                        "discarded": discarded, **({"previous_review": prev} if isinstance(prev, dict) else {})}
                self._save(b, "ACK_OLDER", {"discarded": discarded, "owner": dict(owner), "older_head_seq": entries[-1]["seq"]})
                return {"status": "ACCEPTED", "older_head_seq": entries[-1]["seq"], "discarded": discarded}
        except AN.AnchorRefused as exc:
            raise BookRefused(exc.code, exc.detail) from None

    def lifetime_cumulative_usd(self, b: Optional[Mapping[str, Any]] = None) -> Decimal:
        """Verified net realised over EVERY epoch of this account (closed epochs plus the current one). The lifetime stop is evaluated per epoch; this is what an owner reads before starting another one."""
        b = b or self.load()
        return sum((D(e["realised_usd"]) for e in (b.get("epochs") or [])), D(0)) + (_d(b.get("realised_usd")) or D(0))

    def rebaseline(self, reason: str, owner: Mapping[str, Any]) -> Dict[str, Any]:
        """The OWNER's decision to start a new EPOCH of this account: the closing figures of the current epoch go into the lineage (a durable, witnessed, permanent row, with the owner's reason), and the loss
        streak, halt, review, drawdown peak, realised result and baseline start again from the next flat cash read. This is the one supported way to lift the lifetime stop on the SAME account (the other is a new
        account); it is never a rollback because it is an APPENDED entry the witness holds, and the cumulative result across epochs stays in every report, decision record and typed line. Refused while a session
        is pending (its cash result must be booked first) or a declared transfer is unapplied."""
        if self.anchor is None:
            raise BookRefused("NOT_ANCHORED", "a bare book has no lineage")
        reason = (reason or "").strip()
        if not (8 <= len(reason) <= 200) or any(ord(c) < 32 for c in reason):
            raise BookRefused("REBASELINE_REASON_REQUIRED", "state in 8 to 200 characters of your own what happened and why a new epoch is right")
        with self._locked():
            b = self.load()
            if b["pending"]:
                raise BookRefused("REBASELINE_REFUSED_PENDING_SESSION", "a session is still unsettled: let the next cash read book it first")
            if b["transfers_pending"]:
                raise BookRefused("REBASELINE_REFUSED_TRANSFERS_PENDING", "a declared transfer is still unapplied: let the next cash read consume it first")
            closed = {"epoch": int(b.get("epoch") or 0), "closed_ms": int(time.time() * 1000), "reason": reason, "start_cash_usd": b.get("start_cash_usd"), "realised_usd": b["realised_usd"], "gains_usd": b["gains_usd"],
                      "losses_usd": b["losses_usd"], "unverified_excess_usd": b["unverified_excess_usd"], "loss_streak": int(b["loss_streak"]), "halted": bool(b["halted"]), "peak_basis_usd": b.get("peak_basis_usd"),
                      "last_cash_usdt": b.get("last_cash_usdt"), "review_code": (b.get("review_required") or {}).get("code") if isinstance(b.get("review_required"), dict) else None,
                      "lifetime_stop_was_active": (_d(b["realised_usd"]) or D(0)) <= -LIFETIME_LOSS_STOP_USD}
            b["epochs"] = (list(b.get("epochs") or []) + [closed])[-HISTORY_MAX:]
            b["epoch"] = int(b.get("epoch") or 0) + 1
            b.update({"start_cash_usd": None, "peak_basis_usd": None, "realised_usd": "0", "gains_usd": "0", "losses_usd": "0", "unverified_excess_usd": "0", "loss_streak": 0, "halted": False,
                      "review_required": None, "last_cash_usdt": None, "last_cash_ms": None})
            self._save(b, "REBASELINE", {"reason": reason, "closed": closed, "owner": dict(owner)})
            return closed

    def acknowledge_halt(self) -> bool:
        """The owner's explicit decision, after reviewing the losses, to let entries resume. The book keeps a row of what was acknowledged (it used to leave no trace anywhere). Nothing is credited."""
        with self._locked():
            b = self.load()
            if not b.get("halted"):
                return False
            last = (b.get("history") or [{}])[-1]
            b.setdefault("halt_acknowledged", []).append({"at_ms": int(time.time() * 1000), "loss_streak": int(b.get("loss_streak", 0)), "realised_usd": b.get("realised_usd"),
                                                          "last_session": last.get("session_id")})
            b["halted"], b["loss_streak"] = False, 0
            self._save(b, "ACK_HALT", {"ack": b["halt_acknowledged"][-1]})
            return True

    def acknowledge_review(self) -> bool:
        """The owner's explicit decision, after reading the unexplained session's evidence in the book, to let entries resume. It changes no money and no history; False when there is no
        review open. Clearing the ALERT marker alone never does this: the autopilot re-asserts the marker from the book."""
        with self._locked():
            b = self.load()
            if not b.get("review_required"):
                return False
            b["review_required"] = None
            b.setdefault("review_acknowledged", []).append({"at_ms": int(time.time() * 1000)})
            self._save(b, "ACK_REVIEW", {"ack": b["review_acknowledged"][-1]})
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
            self._save(b, "ACK_DRAWDOWN", {"ack": b["drawdown_acknowledged"][-1]})
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
                "max_drawdown_now_usd": None if self.drawdown(b) is None else str(self.drawdown(b)), "loss_streak": b["loss_streak"], "halted": b["halted"], "review_required": b.get("review_required"), "pending": b["pending"],
                "lifetime_loss_stop_usd": str(LIFETIME_LOSS_STOP_USD), "lifetime_loss_stop_active": (_d(b["realised_usd"]) or D(0)) <= -LIFETIME_LOSS_STOP_USD,
                "epoch": int(b.get("epoch") or 0), "epochs_closed": len(b.get("epochs") or []), "lifetime_cumulative_realised_usd": str(self.lifetime_cumulative_usd(b)),
                "anchor": self.anchor.report() if self.anchor is not None else {"anchored": False, "warning": "BARE BOOK: its integrity is bound to its own file; production opens CapitalBook.anchored"},
                "label": "TESTNET SIMULATION: fake money; verified realised cash only; not real-world performance"}
