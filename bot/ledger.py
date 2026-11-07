"""Double-entry journal for the carry book. What it owns, and the proof.

WHY THIS EXISTS
===============
Until now the engine knew `funding_collected` and nothing else. No fee was
booked. No balance was reconciled against the venue's wallet. There was no
statement and no way to answer the question a client actually asks: what did I
earn, and where is it?

INVENTORY D11 has said so since 0033 — "`cumExecFee` carried per leg; booking
it is Phase E" — and 0043 showed the cost of the gap: funding was booked on the
entry price for four years, a quarter of it missing, and nothing in the system
could notice, because nothing added the books up.

THE ONE RULE
============
Every entry balances to zero in USD or it is REFUSED. Nothing is written and
the caller gets an exception. That single rule is what makes a P&L checkable
instead of asserted: a fee that vanishes, a funding payment booked twice, a
realised gain with no matching cash — each fails to balance, and each fails
loudly at the moment it happens rather than in a quarterly statement.

APPEND ONLY
===========
There is no delete and no edit. A mistake is corrected by a REVERSING entry
that is itself in the journal. A ledger you can edit is a ledger that cannot
detect a bug, a partial fill or a fat finger — the same argument
`reconcile_pair` has made since 0022, applied to money instead of quantity.

FUNCTIONAL CURRENCY
===================
USD. Every line carries a USD amount and, where the account holds a thing, a
QUANTITY of that thing. Balance is asserted on the USD column; the quantity
column is what reconciles against the venue's wallet. This is deliberate: a
book with two currencies and no functional currency is a book whose "total" is
a matter of opinion.

WHAT IT DOES NOT DO
===================
It does not value an open position. `statement()` reports REALISED money and
the open quantity separately, and never blends them into one number — an
unrealised mark is an estimate, and this file's whole purpose is to hold the
things that are not estimates.
"""
from __future__ import annotations

import inspect
import logging
import math
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

import venue_fees

#: The chart of accounts, spelled once. An account not in here is refused
#: rather than created: a typo must not silently open a ledger nothing
#: reconciles.
BTC = "ASSET:BTC"
USDT = "ASSET:USDT"
PERP = "POSITION:PERP"
FUNDING = "INCOME:FUNDING"
FEES = "EXPENSE:FEES"
BORROW = "EXPENSE:BORROW"
REALISED = "EQUITY:REALISED"
OPENING = "EQUITY:OPENING"

ACCOUNTS = (BTC, USDT, PERP, FUNDING, FEES, BORROW, REALISED, OPENING)

#: Accounts that hold a THING, whose quantity reconciles against the venue.
QUANTITY_ACCOUNTS = (BTC, USDT, PERP)

#: An entry must balance to better than this in USD. A tenth of a cent: tighter
#: than any fee the venue charges, looser than float noise on a $1e9 book.
BALANCE_TOLERANCE = 1e-4

#: Reconciliation dust, per asset. Below this the venue and the book agree.
DUST = {BTC: 1e-8, USDT: 1e-4, PERP: 1e-8}


class LedgerError(RuntimeError):
    """An entry that does not balance, an account that does not exist, or a
    statement that does not sum. Never caught and ignored."""


@dataclass
class Line:
    account: str
    usd: float
    qty: float = 0.0


#: Entry kinds that move money because an order filled, and so carry a fee.
FILL_KINDS = ("spot_buy", "spot_sell", "perp_open", "perp_close")

#: How well the fee on a fill entry is known. "" means the entry is not a fill.
#:   confirmed  - the venue stated it and every currency was booked by name
#:   unknown    - the venue did not state it; 0 is booked and FLAGGED, not trusted
#:   unresolved - stated, but a currency has no booking treatment (e.g. MNT);
#:                the resolvable part is booked, the rest is flagged
#:   unlabelled - a bare number with no currency evidence (legacy callers)
FEE_STATUSES = ("", "confirmed", "unknown", "unresolved", "unlabelled")

FUNDING_BASES = ("", "venue", "engine_estimate", "unattributed", "unread")


@dataclass
class Entry:
    ms: int
    kind: str
    ref: str
    lines: List[Line]
    memo: str = ""
    #: Persisted with the entry. A counter on the Journal object is lost on
    #: restart; this is not, so a rebuilt journal cannot claim its fees were
    #: confirmed when they were not.
    fee_status: str = ""
    #: The raw venue evidence the booking was made from: the fee components and
    #: the field that stated them, or for funding the venue row (id, time,
    #: amount, size).
    fee_raw: Dict[str, Any] = field(default_factory=dict)
    #: Where a FUNDING entry's number came from. "" for everything else.
    #:   venue           - a settlement row the venue paid (transaction log)
    #:   engine_estimate - the engine's own rate x qty x mark; NOT observed
    #:   unattributed    - the venue paid something this book cannot claim (no
    #:                     position of ours, a different size, another coin);
    #:                     booked at $0 and flagged for a human
    #:   unread          - the venue could not be read while a position was open
    basis: str = ""
    #: The position cycle (flat -> opened -> flat) this entry belongs to. Set by
    #: `Journal.post`, persisted, and re-derived identically for older rows.
    #: "" for an entry before any cycle (the opening balance).
    cycle: str = ""
    #: The mark the engine DECIDED on, for a fill entry, when the order path
    #: supplied one. None = not journalled (every row written before this field
    #: existed, and every fill whose caller did not pass it). It is serialised
    #: only when set, so an older row round-trips byte-for-byte; slippage is
    #: computed only from fills that carry it and is UNAVAILABLE otherwise.
    decision_mark: Optional[float] = None


@dataclass
class Balance:
    usd: float = 0.0
    qty: float = 0.0


def funding_basis(e: "Entry") -> str:
    """The basis a READER should use. A funding entry persisted with an empty
    basis is the engine's own number (nothing proves otherwise); the stored row
    is never rewritten."""
    return "engine_estimate" if (e.kind == "funding" and e.basis == "") else e.basis


def _negates(original: "Entry", reversal: "Entry") -> bool:
    """Do the reversal's lines exactly cancel the original's?"""
    if len(original.lines) != len(reversal.lines):
        return False
    return all(a.account == b.account and abs(a.usd + b.usd) <= 1e-9
               and abs(a.qty + b.qty) <= 1e-12
               for a, b in zip(original.lines, reversal.lines))


def supersession_pairs(entries) -> Dict[int, int]:
    """{index of an engine-estimate funding entry: index of the reversal that
    replaced it}, in JOURNAL ORDER and validated.

    A pairing exists only if (a) a venue-basis funding entry for the named venue
    id came EARLIER in the journal (a settlement that arrived), (b) the reversal's
    lines exactly negate the estimate's, and (c) the estimate is the latest
    still-unpaired estimate of that ref before the reversal. Each reversal pairs
    with ONE estimate; a second estimate that reuses a ref is not covered by the
    first one's reversal; a reversal that merely carries the right keys, with no
    such settlement or other lines, pairs with nothing and is just cash. The
    estimate stays in the journal either way."""
    venue_ids: set = set()
    open_est: Dict[str, List[int]] = {}
    pairs: Dict[int, int] = {}
    for i, e in enumerate(entries):
        if e.kind == "funding":
            if funding_basis(e) == "engine_estimate":
                open_est.setdefault(e.ref, []).append(i)
            elif e.basis == "venue" and "venue_id" in e.fee_raw:
                venue_ids.add(str(e.fee_raw["venue_id"]))
        elif (e.kind == "reversal" and "supersedes" in e.fee_raw
              and "superseded_by_venue_id" in e.fee_raw):
            if str(e.fee_raw["superseded_by_venue_id"]) not in venue_ids:
                continue
            for j in reversed(open_est.get(str(e.fee_raw["supersedes"]), [])):
                if _negates(entries[j], e):
                    pairs[j] = i
                    open_est[str(e.fee_raw["supersedes"])].remove(j)
                    break
    return pairs


def _net_basis(fee_counts: Dict[str, int], fund_counts: Dict[str, int],
               reconciled: bool = True) -> str:
    """How far the net figure can be trusted, in one word."""
    if sum(fee_counts.values()) + sum(fund_counts.values()) == 0:
        return "no_fills"
    if any(fee_counts[k] for k in ("unknown", "unresolved", "unlabelled")):
        return "provisional_fees_incomplete"
    if any(fund_counts[k] for k in ("engine_estimate", "unattributed", "unread")):
        return "provisional_funding_unverified"
    if not reconciled:
        return "provisional_funding_unverified"
    return "confirmed"


class Journal:
    """Append-only. Balanced. The only thing that knows what the book owns."""

    def __init__(self) -> None:
        self.entries: List[Entry] = []
        self._cycle = ""            # the cycle new entries belong to
        self._pending_cycle = ""    # a name given for the NEXT cycle
        self._cycle_perp = 0.0      # running perp quantity
        self._cycle_spot = 0.0      # running spot quantity bought/sold by the book
        self._cycle_closed = False  # the cycle has gone flat (perp closed, or an
                                    # attempt that never hedged was sold back)
        self._seen_cycles: set = set()

    def begin_cycle(self, cycle_id: str) -> None:
        """Name the cycle the next opening fill will start. A name that is
        never used (the order never filled) is replaced by the next call."""
        if not isinstance(cycle_id, str) or not cycle_id.strip():
            raise LedgerError("a cycle id must be a non-empty string")
        self._pending_cycle = cycle_id

    def _unique_cycle_id(self, candidate: str) -> str:
        """A cycle id nobody has used. Two cycles opened in the same millisecond
        (or named alike) must stay two cycles, so a repeat gets -2, -3, ...
        The same rule runs on a replay, so ids are reproducible."""
        cid, n = candidate, 1
        while cid in self._seen_cycles:
            n += 1
            cid = f"{candidate}-{n}"
        return cid

    @staticmethod
    def _settlement_ms(entry: Entry) -> Optional[int]:
        """When the VENUE settled this funding row, or None if it is not one."""
        if entry.basis in ("venue", "unattributed") and "venue_ms" in entry.fee_raw:
            try:
                return int(entry.fee_raw["venue_ms"])
            except (TypeError, ValueError):
                return None
        return None

    def _assign_cycle(self, entry: Entry) -> None:
        # A venue funding row belongs to the cycle that held the position AT
        # THE SETTLEMENT, not to whichever cycle is current when the row is
        # delivered (a row for a settlement inside cycle A can arrive after
        # cycle B opened). It carries no position line, so it never moves the
        # running cycle state either: restoring it from a stored row must not
        # make an older cycle current.
        if entry.kind == "reversal" and "supersedes" in entry.fee_raw:
            # belongs to the cycle of the estimate it cancels; never moves the
            # running cycle (also true when restored from a stored row)
            if not entry.cycle:
                orig = next((e for e in self.entries
                             if e.ref == entry.fee_raw["supersedes"]), None)
                entry.cycle = orig.cycle if orig else self._cycle
            return
        settled = self._settlement_ms(entry)
        if settled is not None:
            if not entry.cycle:
                for prior in reversed(self.entries):
                    if prior.cycle and prior.ms <= settled:
                        entry.cycle = prior.cycle
                        return
                entry.cycle = self._cycle         # nothing earlier: as before
            return
        flat = abs(self._cycle_perp) <= DUST[PERP]
        if entry.cycle:                       # restored from a stored row
            if entry.cycle != self._cycle:
                self._cycle_perp = self._cycle_spot = 0.0
            self._cycle = entry.cycle
            self._seen_cycles.add(entry.cycle)
            self._pending_cycle = ""
            self._cycle_closed = False
        elif entry.kind in ("spot_buy", "perp_open") and flat and (
                not self._cycle or self._cycle_closed
                or (self._pending_cycle and self._pending_cycle != self._cycle)):
            self._cycle = self._unique_cycle_id(
                self._pending_cycle or f"cyc-{entry.ms}")
            self._seen_cycles.add(self._cycle)
            self._pending_cycle = ""
            self._cycle_closed = False
            self._cycle_spot = 0.0
            entry.cycle = self._cycle
        elif self._cycle:
            entry.cycle = self._cycle
        for line in entry.lines:
            if line.account == PERP:
                self._cycle_perp += line.qty
            elif line.account == BTC and entry.kind in ("spot_buy", "spot_sell"):
                self._cycle_spot += line.qty
        if entry.kind == "perp_close" and abs(self._cycle_perp) <= DUST[PERP]:
            self._cycle_closed = True
        elif (entry.kind == "spot_sell" and abs(self._cycle_perp) <= DUST[PERP]
              and abs(self._cycle_spot) <= DUST[BTC]):
            # An attempt whose perp leg never landed, sold back to flat.
            self._cycle_closed = True

    @property
    def unknown_fees(self) -> int:
        """Fills whose fee the venue did not state, derived from the entries
        (so it survives a reload). Each is booked at 0 and FLAGGED: an unknown
        cost that reads as $0.00 is a cost that has left the ledger."""
        return sum(1 for e in self.entries if e.fee_status == "unknown")

    # -- writing ----------------------------------------------------------

    def post(self, entry: Entry, *, _loading: bool = False) -> Entry:
        """Validate, then append. A refusal writes nothing at all.

        ``_loading`` is for `from_rows` only: rows already persisted are accepted
        as written (tolerant loading); NEW entries must carry their provenance."""
        if not entry.lines:
            raise LedgerError(f"entry {entry.ref!r} has no lines")
        if entry.basis not in FUNDING_BASES:
            raise LedgerError(
                f"entry {entry.ref!r} has basis {entry.basis!r}; expected one "
                f"of {FUNDING_BASES}")
        if not _loading:
            if entry.kind == "funding" and entry.basis == "":
                raise LedgerError(
                    f"funding entry {entry.ref!r} has no basis; say "
                    "engine_estimate or book it through book_funding_payments")
            if entry.basis == "venue" and not {
                    "venue_id", "venue_ms", "venue_usd"} <= set(entry.fee_raw):
                raise LedgerError(
                    f"entry {entry.ref!r} claims basis 'venue' without the venue "
                    "row (venue_id, venue_ms, venue_usd) it was booked from")
        if entry.decision_mark is not None and not (
                isinstance(entry.decision_mark, (int, float))
                and math.isfinite(entry.decision_mark)
                and entry.decision_mark > 0):
            raise LedgerError(
                f"entry {entry.ref!r} has decision_mark "
                f"{entry.decision_mark!r}; a mark is a finite positive price "
                "or None (not journalled)")
        if entry.fee_status not in FEE_STATUSES:
            raise LedgerError(
                f"entry {entry.ref!r} has fee_status {entry.fee_status!r}; "
                f"expected one of {FEE_STATUSES}")
        total = 0.0
        for line in entry.lines:
            if line.account not in ACCOUNTS:
                raise LedgerError(
                    f"{line.account!r} is not an account. Refusing rather "
                    f"than creating one: {list(ACCOUNTS)}")
            if not (math.isfinite(line.usd) and math.isfinite(line.qty)):
                raise LedgerError(
                    f"entry {entry.ref!r} carries a non-finite amount "
                    f"({line.usd!r}, {line.qty!r})")
            total += line.usd
        if abs(total) > BALANCE_TOLERANCE:
            raise LedgerError(
                f"entry {entry.ref!r} ({entry.kind}) is out by ${total:.6f}. "
                "Nothing was written. A book that accepts an unbalanced entry "
                "cannot tell a missing fee from a rounding error.")
        if self.entries and entry.ms < self.entries[-1].ms:
            raise LedgerError(
                f"entry {entry.ref!r} is stamped {entry.ms}, before the last "
                f"entry at {self.entries[-1].ms}. A journal whose stamps go "
                "backwards cannot be cut into periods, and a period is what a "
                "statement is.")
        self._assign_cycle(entry)
        self.entries.append(entry)
        return entry

    def reverse(self, ref: str, *, ms: int, why: str,
                raw: Optional[Dict[str, Any]] = None,
                original: Optional["Entry"] = None) -> Entry:
        """Undo an entry the only way a ledger may: by writing another one.
        `original` names the exact entry when a ref has been reused."""
        if original is None:
            original = next((e for e in self.entries if e.ref == ref), None)
        if original is None:
            raise LedgerError(f"no entry {ref!r} to reverse")
        used = sum(1 for e in self.entries if e.ref.startswith(f"{ref}~rev"))
        return self.post(Entry(
            ms=ms, kind="reversal", ref=f"{ref}~rev" + (f"{used}" if used else ""),
            lines=[Line(l.account, -l.usd, -l.qty) for l in original.lines],
            memo=f"reverses {ref}: {why}", fee_raw=dict(raw or {})))

    # -- reading ----------------------------------------------------------

    def balances(self, *, to_ms: Optional[int] = None) -> Dict[str, Balance]:
        out = {name: Balance() for name in ACCOUNTS}
        for entry in self.entries:
            if to_ms is not None and entry.ms > to_ms:
                continue
            for line in entry.lines:
                out[line.account].usd += line.usd
                out[line.account].qty += line.qty
        return out

    def statement(self, *, from_ms: Optional[int] = None,
                  to_ms: Optional[int] = None,
                  now_ms: Optional[int] = None) -> Dict[str, Any]:
        """What the book earned over a period, and the proof that it sums.

        Raises when the identity fails, exactly as `carry_backtest.attribution`
        does. A statement that does not add up is not a statement.
        """
        acc = {name: Balance() for name in ACCOUNTS}
        total_usd = 0.0
        fee_counts = {k: 0 for k in FEE_STATUSES if k}
        fund_counts = {k: 0 for k in FUNDING_BASES if k}
        fund_usd = {k: 0.0 for k in FUNDING_BASES if k}
        pairs = supersession_pairs(self.entries)
        rev_of = {j: i for j, i in pairs.items()}
        prior_reversals = 0.0
        for idx, entry in enumerate(self.entries):
            if from_ms is not None and entry.ms < from_ms:
                continue
            if to_ms is not None and entry.ms > to_ms:
                continue
            if entry.fee_status:
                fee_counts[entry.fee_status] += 1
            fb = funding_basis(entry)
            cancelled = False
            if idx in rev_of:
                r = self.entries[rev_of[idx]]
                # cancelled for the counters only if its replacement is in this
                # period too; otherwise it is an estimate in THIS period
                cancelled = ((from_ms is None or r.ms >= from_ms)
                             and (to_ms is None or r.ms <= to_ms))
            if idx in rev_of.values():
                # a reversal in THIS period of an estimate that lies in an earlier
                # one: its cash is real here, so say what it is
                est_idx = next(j for j, i in rev_of.items() if i == idx)
                e_ms = self.entries[est_idx].ms
                if (from_ms is not None and e_ms < from_ms) or (to_ms is not None and e_ms > to_ms):
                    prior_reversals += -sum(l.usd for l in entry.lines if l.account == FUNDING)
            if fb and not cancelled:
                fund_counts[fb] += 1
                fund_usd[fb] += -sum(
                    l.usd for l in entry.lines if l.account == FUNDING)
            for line in entry.lines:
                acc[line.account].usd += line.usd
                acc[line.account].qty += line.qty
                total_usd += line.usd
        if abs(total_usd) > BALANCE_TOLERANCE:
            raise LedgerError(
                f"the journal does not balance: ${total_usd:.6f} unaccounted. "
                "Some entry was written by something other than post().")

        lifecycle_status = lifecycle_reconciliation(
            self, from_ms=from_ms, to_ms=to_ms, now_ms=now_ms)
        funding_reconciled = all(l["reconciled"] for l in lifecycle_status)

        funding_usd = -acc[FUNDING].usd          # income is credit-normal
        fees_usd = acc[FEES].usd
        borrow_usd = acc[BORROW].usd
        # A revaluation posts to REALISED too, but it MARKS coin that was never
        # sold: it is kept out of the realised/net (cash) figures and reported
        # on its own, with the pair (cash net + mark) beside it.
        inventory_mark_usd = -sum(
            line.usd for entry in self.entries
            if entry.kind == "revalue"
            and (from_ms is None or entry.ms >= from_ms)
            and (to_ms is None or entry.ms <= to_ms)
            for line in entry.lines if line.account == REALISED)
        inventory_unbooked = sum(
            1 for entry in self.entries
            if entry.kind == INVENTORY_UNBOOKED_KIND
            and (from_ms is None or entry.ms >= from_ms)
            and (to_ms is None or entry.ms <= to_ms))
        realised_usd = -acc[REALISED].usd - inventory_mark_usd
        net = funding_usd - fees_usd - borrow_usd + realised_usd

        # The identity, enforced. Every term is a balance read off the same
        # journal, so this catches an account that was posted to but is not
        # in the sum — the failure mode a hand-written P&L has forever.
        cash_and_holdings = (acc[USDT].usd + acc[BTC].usd + acc[PERP].usd
                             - acc[OPENING].usd * -1.0)
        if not math.isfinite(net):
            raise LedgerError("net is not finite")
        return {
            "funding_usd": funding_usd,
            "fees_usd": fees_usd,
            "borrow_usd": borrow_usd,
            "realised_usd": realised_usd,
            "net_usd": net,
            # Marked-to-market, NOT cash: the client's BTC valued between two
            # spot prices and never sold. `pair_net_usd` is the hedged pair's
            # economic result (cash net + the mark), labelled as such.
            "inventory_mark_usd": inventory_mark_usd,
            # None when an overlay cycle's inventory leg was not valued in this period: net + 0 would then be the perp's gain or loss with nothing
            # against it (reproduced: +89.06 reported for a pair that lost its 10.95 of fees). The CASH net above is unaffected.
            "inventory_leg_booked": inventory_unbooked == 0,
            "inventory_legs_unbooked": inventory_unbooked,
            "pair_net_usd": None if inventory_unbooked else net + inventory_mark_usd,
            "fees_unknown": fee_counts["unknown"],
            "fees_unresolved": fee_counts["unresolved"],
            "fees_unlabelled": fee_counts["unlabelled"],
            # Every fill's fee venue-stated and booked by currency. Only then is
            # `net_usd` a confirmed figure; otherwise it is PROVISIONAL and the
            # fees it nets are a lower bound (unknown/unresolved fees booked as
            # 0), never to be quoted as realised net economics.
            "fees_confirmed": all(
                fee_counts[k] == 0 for k in ("unknown", "unresolved",
                                             "unlabelled")),
            # Fills in the period. "All fees confirmed" is vacuously true of a
            # book that never traded; it must not read as a result.
            "fills": sum(fee_counts.values()),
            # Funding is the book's income, so WHERE it came from is part of
            # the answer. Only a settlement row the venue actually paid counts
            # as observed; the engine's own rate x qty x mark does not.
            "funding_venue_usd": fund_usd["venue"],
            "funding_estimated_usd": fund_usd["engine_estimate"],
            # reversals in this period of estimates that sit in an earlier one:
            # they explain why funding_usd can be below funding_venue_usd here
            "funding_prior_period_estimate_reversals_usd": prior_reversals,
            "funding_estimated_entries": fund_counts["engine_estimate"],
            "funding_unattributed": fund_counts["unattributed"],
            "funding_unread": fund_counts["unread"],
            # Confirmed needs BOTH that nothing bad was flagged AND that every
            # position lifecycle was positively reconciled (see
            # `lifecycle_reconciliation`): a book that was never checked has
            # flagged nothing, and absence of a flag is not a check.
            "funding_confirmed": funding_reconciled and all(
                fund_counts[k] == 0 for k in ("engine_estimate",
                                              "unattributed", "unread")),
            "funding_reconciled": funding_reconciled,
            "funding_lifecycles": lifecycle_status,
            "net_basis": _net_basis(fee_counts, fund_counts, funding_reconciled),
            # Quantities, for reconciliation. NOT valued: an open position's
            # mark is an estimate and this file holds what is not estimated.
            "open_perp_qty": acc[PERP].qty,
            "btc_qty": acc[BTC].qty,
            "usdt_qty": acc[USDT].qty,
            "entries": sum(
                1 for e in self.entries
                if (from_ms is None or e.ms >= from_ms)
                and (to_ms is None or e.ms <= to_ms)),
            "_cash_check": cash_and_holdings,
        }

    # -- persistence ------------------------------------------------------

    def to_rows(self) -> List[Dict[str, Any]]:
        rows = []
        for e in self.entries:
            row = {"ms": e.ms, "kind": e.kind, "ref": e.ref, "memo": e.memo,
                   "fee_status": e.fee_status, "fee_raw": e.fee_raw,
                   "basis": e.basis, "cycle": e.cycle,
                   "lines": [{"account": l.account, "usd": l.usd, "qty": l.qty}
                             for l in e.lines]}
            if e.decision_mark is not None:
                # additive: absent (not null) when never journalled, so a row
                # written before the field existed serialises identically
                row["decision_mark"] = float(e.decision_mark)
            rows.append(row)
        return rows

    @classmethod
    def from_rows(cls, rows: List[Dict[str, Any]]) -> "Journal":
        """Rebuild, re-validating every entry. A journal is not trusted because
        it came out of a database: the balance rule is applied again on load,
        so a row edited in the file fails here instead of in a statement."""
        journal = cls()
        for row in rows:
            kind = str(row["kind"])
            # A row written before fee provenance existed says nothing about its
            # fee: on a fill it is "unlabelled", never "confirmed".
            status = str(row.get("fee_status") if "fee_status" in row
                         else ("unlabelled" if kind in FILL_KINDS else ""))
            journal.post(Entry(
                ms=int(row["ms"]), kind=kind,
                ref=str(row["ref"]), memo=str(row.get("memo", "")),
                fee_status=status, fee_raw=dict(row.get("fee_raw") or {}),
                # A funding row written before provenance existed is the
                # engine's own number: never "venue" by default.
                basis=str(row["basis"] if "basis" in row else
                          ("engine_estimate" if kind == "funding" else "")),
                cycle=str(row.get("cycle") or ""),
                decision_mark=(None if row.get("decision_mark") is None
                               else float(row["decision_mark"])),
                lines=[Line(str(l["account"]), float(l["usd"]),
                            float(l.get("qty", 0.0)))
                       for l in row["lines"]]), _loading=True)
        return journal


# -- position lifecycles ---------------------------------------------------

#: Kind of the $0 entry that records one lifecycle's reconciliation outcome.
RECONCILIATION_KIND = "funding_reconciliation"

#: Exchange-precision policy for "the venue's size is the book's size": equal
#: within one part in a hundred or 1e-9 coin, whichever is larger. A venue size
#: of the opposite sign never matches, however close in magnitude.
SIZE_REL_TOL = 0.01
SIZE_ABS_TOL = 1e-9

#: A lifecycle that stays provisional this long after it ended is FINAL:
#: further polling cannot be expected to find what the venue never returned.
RECONCILIATION_FINAL_AFTER_MS = 7 * 86_400_000


@dataclass(frozen=True)
class Lifecycle:
    """One continuous non-flat interval of the book's perp exposure.

    Derived ONLY from the journal's perp quantity (never from spot inventory or
    from a venue account balance). ``end_ms`` is None while open. ``moves`` and
    ``last_ref`` fingerprint the exposure history inside it, so a reconciliation
    record made before a later fill is recognisably stale.
    """

    id: str
    start_ms: int
    end_ms: Optional[int]
    moves: int
    last_ref: str

    @property
    def fingerprint(self) -> Tuple[int, str, Optional[int]]:
        return (self.moves, self.last_ref, self.end_ms)


def lifecycles(journal: "Journal") -> List[Lifecycle]:
    """Every non-flat perp interval, in order, from the persisted entries.

    Ordering is the journal's append order, which `post` forces to be
    non-decreasing in time; entries sharing a millisecond keep that order. An
    entry moving the exposure through zero in one step (long to short) ends one
    lifecycle and starts the next at the same instant. Dust below
    ``DUST[PERP]`` is flat.
    """
    out: List[Lifecycle] = []
    qty = 0.0
    start: Optional[int] = None
    first_ref = ""
    moves = 0
    last_ref = ""

    def close(at: int) -> None:
        nonlocal start, moves, last_ref, first_ref
        out.append(Lifecycle(f"perp:{start}:{first_ref}", int(start), int(at),
                             moves, last_ref))
        start, moves, last_ref, first_ref = None, 0, "", ""

    for e in journal.entries:
        d = sum(l.qty for l in e.lines if l.account == PERP)
        if d == 0.0:
            continue
        new = qty + d
        flat_before = abs(qty) <= DUST[PERP]
        flat_after = abs(new) <= DUST[PERP]
        flipped = (not flat_before and not flat_after and (qty > 0) != (new > 0))
        if start is not None and (flat_after or flipped):
            moves += 1
            last_ref = e.ref
            close(e.ms)
            if flipped:
                start, first_ref, moves, last_ref = e.ms, e.ref, 1, e.ref
        elif start is None and not flat_after:
            start, first_ref, moves, last_ref = e.ms, e.ref, 1, e.ref
        elif start is not None:
            moves += 1
            last_ref = e.ref
        qty = 0.0 if flat_after else new
    if start is not None:
        out.append(Lifecycle(f"perp:{start}:{first_ref}", int(start), None,
                             moves, last_ref))
    return out


#: An OPEN lifecycle's FULLY_RECONCILED record speaks for the moment it was
#: made. Read later, with a clock, it is only trusted this long: the bot polls
#: hourly, so a record older than this was not refreshed (process down, venue
#: unreadable) and says nothing about the settlements since.
RECORD_MAX_AGE_MS = 6 * 3_600_000


_RECORD_PROBLEM_COUNTERS = (
    "missing_expected", "flagged_missing", "ambiguous_ownership",
    "unread_markers", "rows_contradicting_public_rate_sign",
    "account_rows_without_public_history", "account_rows_unattributed",
    "account_rows_identity_conflict")

_CONTRADICTIONS = (
    ("unattributed", "UNATTRIBUTED_SETTLEMENT_ROWS"),
    ("conflicts", "IDENTITY_CONFLICT"),
    ("missing", "EXPECTED_SETTLEMENT_MISSING"),
    ("unread", "FUNDING_UNREADABLE_WHILE_OPEN"),
    ("estimated", "ESTIMATED_FUNDING_IN_LIFECYCLE"))


def lifecycle_flags(journal: "Journal", lc: Lifecycle) -> Dict[str, List[Entry]]:
    """The journal entries that contradict "this lifecycle's funding is whole".

    One definition, used both when a reconciliation record is made and when it
    is read back, so the two cannot disagree. A closed lifecycle owns the
    entries up to its end plus the delivery tail it was polled through; an open
    one owns everything since it began.
    """
    lo = lc.start_ms - ATTRIBUTION_SLACK_MS
    hi = (lc.end_ms + ATTRIBUTION_SLACK_MS) if lc.end_ms is not None \
        else float("inf")
    tail = (lc.end_ms + MISSING_GRACE_MS) if lc.end_ms is not None \
        else float("inf")
    booked = [e for e in journal.entries
              if e.basis in ("venue", "unattributed") and "venue_ms" in e.fee_raw
              and lo <= int(e.fee_raw["venue_ms"]) <= hi]
    return {
        "booked": booked,
        "unattributed": [e for e in booked if e.basis == "unattributed"],
        "conflicts": [e for e in booked if e.fee_raw.get("identity_conflict")],
        "missing": [e for e in journal.entries if e.kind == "funding_missing"
                    and lo <= int(e.fee_raw.get("print_ms", -1)) <= hi],
        "unread": [e for e in journal.entries if e.kind == "funding_unread"
                   and lc.start_ms <= e.ms <= tail],
        # The engine's own funding number inside the lifecycle (not cancelled by
        # a venue settlement): cash that is not the venue's, so the lifecycle's
        # funding cannot be called reconciled.
        "estimated": [e for i, e in enumerate(journal.entries)
                      if e.kind == "funding" and funding_basis(e) == "engine_estimate"
                      and i not in supersession_pairs(journal.entries)
                      and lc.start_ms <= e.ms <= tail],
    }


def lifecycle_reconciliation(journal: "Journal", *, from_ms: Optional[int] = None,
                             to_ms: Optional[int] = None,
                             now_ms: Optional[int] = None) -> List[Dict[str, Any]]:
    """One status row per lifecycle: is its funding positively reconciled?

    A lifecycle is ``reconciled`` only when the LATEST reconciliation record
    written for it (a) says FULLY_RECONCILED with no reasons, (b) was made
    against the same exposure history the journal now has, (c) for a closed
    lifecycle, had judged every settlement up to its end, (d) is not
    contradicted by anything the journal holds NOW (an unattributed row, an
    identity conflict, a missing-settlement flag, an unread marker), and (e)
    when a clock is supplied, for an open lifecycle, is not older than
    ``RECORD_MAX_AGE_MS``. Otherwise it is not reconciled and the reasons are
    carried verbatim. Earlier records stay in the journal: the audit trail of a
    provisional or contradicted state is never overwritten.
    """
    latest: Dict[str, Dict[str, Any]] = {}
    for e in journal.entries:
        if e.kind == RECONCILIATION_KIND and e.fee_raw.get("lifecycle_id"):
            latest[str(e.fee_raw["lifecycle_id"])] = e.fee_raw
    out: List[Dict[str, Any]] = []
    for lc in lifecycles(journal):
        end = lc.end_ms if lc.end_ms is not None else float("inf")
        if from_ms is not None and end < from_ms:
            continue
        if to_ms is not None and lc.start_ms > to_ms:
            continue
        rec = latest.get(lc.id)
        reasons: List[str] = []
        state = "NOT_RECONCILED"
        if rec is None:
            reasons.append("NO_RECONCILIATION_RECORD")
        else:
            state = str(rec.get("state", "PROVISIONAL"))
            reasons = list(rec.get("reasons", []))
            if state == "FULLY_RECONCILED" and reasons:
                # a record cannot be both whole and carry reasons
                state = "INCONSISTENT_RECORD"
            if list(rec.get("fingerprint", [])) != list(lc.fingerprint):
                state = "STALE"
                reasons = ["EXPOSURE_CHANGED_SINCE_RECONCILIATION"] + reasons
            elif lc.end_ms is not None and int(
                    rec.get("judged_to_ms", 0)) < lc.end_ms:
                state = "PENDING_DELIVERY_GRACE"
                reasons = ["SETTLEMENTS_NOT_YET_JUDGEABLE"] + [
                    r for r in reasons if r != "SETTLEMENTS_NOT_YET_JUDGEABLE"]
            elif state == "FULLY_RECONCILED":
                f = lifecycle_flags(journal, lc)
                contra = [code for name, code in _CONTRADICTIONS if f[name]]
                # The record says how many venue rows it attributed. A row the
                # journal holds NOW and the record never counted (a late
                # duplicate under a new id, a late delivery) changes the cash
                # the record vouches for, so it cannot stay vouched for.
                #
                # CLOSED lifecycles only: an open one is re-assessed on every
                # poll and a legitimate new settlement is expected to arrive, so
                # a changed count there is not a contradiction (the print
                # cross-check judges it at the next assessment).
                counted = (rec.get("evidence") or {}).get(
                    "account_rows_attributed")
                if counted is not None and lc.end_ms is not None:
                    held = sum(1 for e in f["booked"] if e.basis == "venue")
                    try:
                        same = int(counted) == held
                    except (TypeError, ValueError):
                        same = False          # an unreadable count vouches for nothing
                    if not same:
                        contra.append(
                            "SETTLEMENT_ROWS_CHANGED_SINCE_RECONCILIATION")
                # The record also states the evidence it was made from. A record
                # that cites an incomplete read or a non-zero problem counter is
                # not whole, whatever its own `state` says.
                ev = rec.get("evidence") or {}
                for key in ("history", "account_log"):
                    if key in ev and not (isinstance(ev[key], dict)
                                          and ev[key].get("complete") is True):
                        contra.append(f"RECORD_CITES_INCOMPLETE_{key.upper()}")
                for key in _RECORD_PROBLEM_COUNTERS:
                    try:
                        if int(ev.get(key) or 0) > 0:
                            contra.append(f"RECORD_CITES_{key.upper()}")
                    except (TypeError, ValueError):
                        contra.append(f"RECORD_CITES_{key.upper()}")
                if contra:
                    state = "CONTRADICTED_AFTER_RECONCILIATION"
                    reasons = contra
                elif (now_ms is not None and lc.end_ms is None and int(
                        now_ms) - int(rec.get("as_of_ms", 0))
                        > RECORD_MAX_AGE_MS):
                    state = "STALE_AS_OF"
                    reasons = ["RECORD_OLDER_THAN_MAX_AGE"]
        out.append({
            "lifecycle_id": lc.id, "start_ms": lc.start_ms, "end_ms": lc.end_ms,
            "open": lc.end_ms is None, "state": state,
            "reconciled": state == "FULLY_RECONCILED", "reasons": reasons,
            "as_of_ms": (rec or {}).get("as_of_ms"),
            "evidence": (rec or {}).get("evidence", {}),
        })
    return out


# -- the postings ---------------------------------------------------------

def opening_balance(journal: Journal, *, ms: int, btc: float,
                    price: float) -> Entry:
    """The client's BTC, which the overlay hedges and never trades.

    Booked so the ledger can reconcile against their wallet. Without it the
    book's BTC balance is zero and every reconciliation reads as an incident.
    """
    return journal.post(Entry(ms=ms, kind="opening", ref=f"open:{ms}", lines=[
        Line(BTC, btc * price, qty=btc),
        Line(OPENING, -btc * price)],
        memo="client inventory; the book never buys or sells it"))


def spot_buy(journal: Journal, *, ms: int, ref: str, qty: float, price: float,
             fee_btc: float, fee_usdt: float = 0.0, fee_status: str = "",
             fee_raw: Optional[Dict[str, Any]] = None,
             decision_mark: Optional[float] = None) -> Entry:
    """A spot BUY, whose fee the venue takes in BTC (INVENTORY F3).

    The wallet ends up holding `qty - fee_btc` while the perp leg hedges
    `qty`, and `reconcile_pair`'s dust tolerance is 1e-6. Booking the fee
    where the venue actually takes it is what makes the BTC balance equal
    what the wallet will show.
    """
    fee_usd = fee_btc * price
    return journal.post(Entry(ms=ms, kind="spot_buy", ref=ref, lines=[
        Line(BTC, qty * price, qty=qty),
        Line(USDT, -qty * price, qty=-qty * price),
        Line(FEES, fee_usd + fee_usdt),
        Line(BTC, -fee_usd, qty=-fee_btc),
        Line(USDT, -fee_usdt, qty=-fee_usdt)],
        fee_status=fee_status, fee_raw=dict(fee_raw or {}),
        decision_mark=decision_mark))


def spot_sell(journal: Journal, *, ms: int, ref: str, qty: float, price: float,
              cost_basis: float, fee_usd: float, fee_status: str = "",
              fee_raw: Optional[Dict[str, Any]] = None,
              decision_mark: Optional[float] = None) -> Entry:
    proceeds = qty * price
    cost = qty * cost_basis
    return journal.post(Entry(ms=ms, kind="spot_sell", ref=ref, lines=[
        Line(USDT, proceeds, qty=proceeds),
        Line(BTC, -cost, qty=-qty),
        Line(REALISED, -(proceeds - cost)),
        Line(FEES, fee_usd),
        Line(USDT, -fee_usd, qty=-fee_usd)],
        fee_status=fee_status, fee_raw=dict(fee_raw or {}),
        decision_mark=decision_mark))


def perp_open(journal: Journal, *, ms: int, ref: str, qty: float, price: float,
              fee_usd: float, fee_status: str = "",
              fee_raw: Optional[Dict[str, Any]] = None,
              decision_mark: Optional[float] = None) -> Entry:
    """Open (or add to) the short. A derivative costs no principal, so the
    only money that moves is the fee; the position is carried as a quantity."""
    return journal.post(Entry(ms=ms, kind="perp_open", ref=ref, lines=[
        Line(PERP, 0.0, qty=-qty),
        Line(FEES, fee_usd),
        Line(USDT, -fee_usd, qty=-fee_usd)],
        memo=f"short {qty} @ {price}",
        fee_status=fee_status, fee_raw=dict(fee_raw or {}),
        decision_mark=decision_mark))


def perp_close(journal: Journal, *, ms: int, ref: str, qty: float,
               price: float, entry_price: float, fee_usd: float,
               fee_status: str = "",
               fee_raw: Optional[Dict[str, Any]] = None,
              decision_mark: Optional[float] = None) -> Entry:
    """Buy the short back. A short gains when the price falls."""
    realised = qty * (entry_price - price)
    return journal.post(Entry(ms=ms, kind="perp_close", ref=ref, lines=[
        Line(PERP, 0.0, qty=qty),
        Line(USDT, realised, qty=realised),
        Line(REALISED, -realised),
        Line(FEES, fee_usd),
        Line(USDT, -fee_usd, qty=-fee_usd)],
        memo=f"cover {qty} @ {price} from {entry_price}",
        fee_status=fee_status, fee_raw=dict(fee_raw or {}),
        decision_mark=decision_mark))


def funding(journal: Journal, *, ms: int, ref: str, usd: float,
            basis: str = "engine_estimate",
            raw: Optional[Dict[str, Any]] = None) -> Entry:
    """One settled print. Signed: a print that was PAID is booked (D10).

    ``basis`` defaults to "engine_estimate" because every historical caller
    passed the engine's own number; a payment the venue stated is booked through
    `book_funding_payments`, which says "venue".
    """
    return journal.post(Entry(ms=ms, kind="funding", ref=ref, lines=[
        Line(USDT, usd, qty=usd),
        Line(FUNDING, -usd)], basis=basis, fee_raw=dict(raw or {})))


#: The settle coin of the book's perp, which the journal's USD column treats as
#: one-for-one (the same convention every fee already follows).
SETTLE_CURRENCY = "USDT"

#: Journal stamps are the bot's clock at booking, a little after the real fill.
#: Ownership of a settlement is only claimed when the book's position was the
#: SAME through this whole span on both sides of it; a fill booked inside it
#: makes ownership ambiguous, and an ambiguous payment is not claimed.
ATTRIBUTION_SLACK_MS = 60_000

#: A settlement that should have a venue row is "missing" once this long has
#: passed, so an ordinary delivery delay does not raise a false alarm.
MISSING_GRACE_MS = 6 * 3_600_000

#: How closely a settlement row's time must match a public funding print.
PRINT_MATCH_TOLERANCE_MS = 5 * 60_000


def _perp_qty_at(journal: Journal, ms: int) -> float:
    """Signed perp quantity (short is negative) per entries stamped by `ms`."""
    return journal.balances(to_ms=int(ms))[PERP].qty


def _perp_moved_within(journal: Journal, lo_ms: int, hi_ms: int) -> bool:
    """Did any entry change the perp quantity in (lo_ms, hi_ms]? Equal
    quantity at both ends does not prove the position was held in between: a
    close then a same-size reopen is flat for a moment."""
    return any(e.ms > lo_ms and e.ms <= hi_ms
               and any(l.account == PERP and abs(l.qty or 0.0) > DUST[PERP]
                       for l in e.lines)
               for e in journal.entries)


def _venue_fill_ms(e: "Entry") -> Optional[int]:
    """The venue's own time of the fill behind a booking, when it was kept."""
    ex = e.fee_raw.get("execution") if isinstance(e.fee_raw, dict) else None
    if not isinstance(ex, dict) or not str(ex.get("fill_ms_basis") or "").startswith("venue"):
        return None                       # a time on the bot's own clock (no stated venue basis) is not the venue's
    v = ex.get("fill_ms")
    return (int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and v > 0 else None)


def _payment_identity(p: Dict[str, Any]) -> tuple:
    """What makes two settlement rows the SAME payment.

    The venue's `id` alone is not trusted to be unambiguous: two distinct
    payments that reused an id must both be booked, and a re-poll of one must
    still match itself. Identity is therefore the whole observed record.
    """
    return (str(p["id"]), int(p["ms"]), float(p["usd"]),
            str(p.get("symbol") or ""))


def _entry_identity(e: Entry) -> Optional[tuple]:
    r = e.fee_raw
    if e.basis not in ("venue", "unattributed") or "venue_id" not in r:
        return None
    return (str(r["venue_id"]), int(r["venue_ms"]), float(r["venue_usd"]),
            str(r.get("symbol") or ""))


def _attribution_problem(journal: Journal, p: Dict[str, Any]) -> Optional[str]:
    """Why this settlement is NOT provably the book's, or None if it is.

    Conservative by construction: every check below can only REFUSE to claim.
    """
    if p.get("currency") != SETTLE_CURRENCY:
        return (f"paid in {p.get('currency')!r}, not {SETTLE_CURRENCY}; "
                "no conversion basis")
    if p.get("sign") != "confirmed":
        # The direction of the money is not established: the row's funding
        # sign was not reproduced by its own wallet change. Booking it would
        # assume who paid whom.
        return (f"funding sign {p.get('sign') or 'unchecked'}: "
                f"{p.get('sign_why') or 'no cross-check against the wallet change'}")
    t = int(p["ms"])
    before = _perp_qty_at(journal, t - ATTRIBUTION_SLACK_MS)
    after = _perp_qty_at(journal, t + ATTRIBUTION_SLACK_MS)
    size = p.get("size")
    try:
        size = None if size is None else float(size)
        if size is not None and not math.isfinite(size):
            size = None
    except (TypeError, ValueError):
        size = None                      # a size that is not a number is no size: the row cannot be matched to the book
    held = after
    if (abs(after - before) > DUST[PERP]
            or _perp_moved_within(journal, t - ATTRIBUTION_SLACK_MS,
                                  t + ATTRIBUTION_SLACK_MS)):
        # The journal's stamps are the bot's clock, a little AFTER the real
        # fill, and a FUNDING_INVERTED exit is decided at the first tick after
        # the settlement it reacts to (up to a minute later): so a book that
        # exits right after a settlement was still in the position through it,
        # and the venue charged it. The venue's signed `size` is the position
        # it charged. If that is exactly the position the book ENTERED the
        # window with (a short), the book held it at the settlement and the
        # payment is its own. A row sized like the position the book is in
        # AFTER the window is not accepted: the engine enters after the print,
        # so a row matching the new entry is the case that must stay refused.
        if (size is not None and before < -DUST[PERP]
                and abs(float(size) - before)
                <= max(SIZE_ABS_TOL, SIZE_REL_TOL * abs(before))):
            # A fill is stamped when it was BOOKED, never before it happened,
            # so a movement stamped at or before the settlement happened before
            # it: the position at the settlement is then not `before`, and a
            # row that says it is contradicts the journal. Only movements
            # stamped AFTER the settlement are left for the row to place.
            # (Likewise a fill the VENUE timed at or before it, when the
            # booking kept the venue's time.)
            moves = [e for e in journal.entries
                     if t - ATTRIBUTION_SLACK_MS < e.ms <= t + ATTRIBUTION_SLACK_MS
                     and any(l.account == PERP and abs(l.qty or 0.0) > DUST[PERP]
                             for l in e.lines)]
            if any(e.ms <= t or (_venue_fill_ms(e) is not None
                                 and _venue_fill_ms(e) <= t) for e in moves):
                return ("a fill booked at or before this settlement changed "
                        "the position, which the venue row's size does not "
                        "reflect; ownership cannot be established")
            held = before
        else:
            return ("the book's position changed within a minute of this "
                    "settlement; ownership cannot be established")
    if abs(held) <= DUST[PERP]:
        return "the book held no perp position at this settlement"
    if size is None:
        return ("the venue row carries no position size, so the payment "
                "cannot be matched to the book's position")
    # SIGNED: a short is negative. A long of the same size is not ours.
    if abs(float(size) - held) > max(SIZE_ABS_TOL, SIZE_REL_TOL * abs(held)):
        return (f"venue position {float(size)} != the book's {held}; part of "
                "it is not this book's")
    return None


def _legacy_funding_by_id(journal: "Journal") -> Dict[str, List[List[float]]]:
    """{venue id: [[amount, booked-at ms], ...]} for venue-basis funding entries
    that carry no `venue_id` (written before identity was stored), read from the
    ref the booking always used (`fund:<id>`, the whole remainder is the id: ids
    may contain '#'). Empty for every entry written today."""
    out: Dict[str, List[List[float]]] = {}
    for e in journal.entries:
        if (e.kind == "funding" and e.basis == "venue"
                and "venue_id" not in e.fee_raw and e.ref.startswith("fund:")):
            usd = sum(l.usd for l in e.lines if l.account == USDT)
            out.setdefault(e.ref[len("fund:"):], []).append([usd, float(e.ms)])
    return out


def _consume_legacy(legacy: Dict[str, List[List[float]]], ident: tuple) -> bool:
    """Is this payment the one a legacy entry already booked? Same id, same
    amount, and booked no earlier than the settlement it pays (a payment cannot
    have been booked before it existed). Each legacy entry answers ONE replay:
    a second row with the same id is a different payment and is not swallowed."""
    for slot in legacy.get(ident[0], []):
        if abs(slot[0] - ident[2]) <= 1e-9 and slot[1] >= ident[1]:
            legacy[ident[0]].remove(slot)
            return True
    return False


def book_funding_payments(journal: Journal, payments: List[Dict[str, Any]], *,
                          ms: int) -> Dict[str, int]:
    """Book what the VENUE paid, once each, and only what is this book's.

    ``payments`` are settlement rows from the account transaction log:
    ``{id, ms, usd, currency, size, symbol}`` with ``usd`` signed (positive =
    received). Rules:

    * a payment already in the journal (same id, time, amount and symbol) is
      skipped, so a re-poll, a replay and a restart cannot book it twice; a
      DIFFERENT payment that reuses an id is an identity conflict: it is kept
      as evidence at $0 (``identity_conflict``) and the statement is provisional;
    * a payment the book cannot prove is its own (other coin, no position,
      position changing around the settlement, no venue size, or a signed size
      that disagrees with the book's quantity) is posted at $0 as
      ``unattributed`` with the evidence, so a human sees it and the statement
      stops claiming confirmation;
    * everything else is booked at the venue's amount.
    """
    known = {i for i in (_entry_identity(e) for e in journal.entries) if i}
    ids_seen = {i[0] for i in known}
    # A venue-basis funding entry written before identity was stored has no
    # `venue_id`; its ref still names the payment ("fund:<id>"). Without this a
    # replay of that payment would be booked a second time. Matched by id and
    # amount it is a duplicate; the same id with another amount is a conflict.
    legacy = _legacy_funding_by_id(journal)
    ids_seen |= set(legacy)
    # A closed lifecycle that is already reconciled has had every settlement it
    # will ever have judged. A row that is not an exact replay and arrives for
    # one now cannot be credited: it is kept as $0 evidence, the lifecycle is
    # contradicted, and a human decides. (Never silently re-credited.)
    sealed = [r for r in lifecycle_reconciliation(journal)
              if r["reconciled"] and not r["open"]]
    used_refs = {e.ref for e in journal.entries}
    out = {"booked": 0, "duplicate": 0, "unattributed": 0, "id_collisions": 0}
    stamp = max(int(ms), journal.entries[-1].ms if journal.entries else 0)
    for p in sorted(payments, key=lambda r: (int(r["ms"]), str(r["id"]))):
        ident = _payment_identity(p)
        if ident in known:
            out["duplicate"] += 1
            continue
        if ident[0] in legacy and _consume_legacy(legacy, ident):
            out["duplicate"] += 1
            continue
        collision = ident[0] in ids_seen
        known.add(ident)
        ids_seen.add(ident[0])
        ref = f"fund:{p['id']}"
        if ref in used_refs:
            # A distinct payment under an id already used: keep the evidence,
            # with a reference that is unique and reproducible.
            ref = f"fund:{p['id']}#{int(p['ms'])}:{float(p['usd'])!r}"
        used_refs.add(ref)
        raw = {"venue_id": str(p["id"]), "venue_ms": int(p["ms"]),
               "venue_usd": float(p["usd"]), "currency": p.get("currency"),
               "size": p.get("size"), "symbol": p.get("symbol")}
        if collision:
            # Two different payments under one id: the venue's identity is not
            # unique, so neither can be taken for the other. The first stays as
            # booked; this one is kept as evidence at $0, visible and
            # provisional, and never silently dropped or silently counted.
            raw["identity_conflict"] = True
            journal.post(Entry(
                ms=stamp, kind="funding_unattributed", ref=ref,
                lines=[Line(USDT, 0.0), Line(FUNDING, 0.0)],
                memo=(f"venue paid {p['usd']:+.6f} under id {p['id']!r} which "
                      "already identifies a different payment: identity "
                      "conflict, not booked as income"),
                basis="unattributed", fee_raw=raw))
            out["id_collisions"] += 1
            out["unattributed"] += 1
            continue
        reason = _attribution_problem(journal, p)
        if reason is None and any(
                r["start_ms"] - ATTRIBUTION_SLACK_MS <= ident[1]
                <= r["end_ms"] + ATTRIBUTION_SLACK_MS for r in sealed):
            reason = ("arrived after the lifecycle it belongs to was already "
                      "reconciled; it is kept as evidence, not credited")
        if reason is not None:
            journal.post(Entry(
                ms=stamp, kind="funding_unattributed", ref=ref,
                lines=[Line(USDT, 0.0), Line(FUNDING, 0.0)],
                memo=f"venue paid {p['usd']:+.6f}: {reason}",
                basis="unattributed", fee_raw=raw))
            out["unattributed"] += 1
        else:
            # The venue's row first (it validates the amount and the evidence; if it
            # raises, nothing has been cancelled). Then every still-unpaired
            # engine estimate booked for this same settlement (ref fund:<id>) is
            # replaced by a reversal naming the venue id; each stays in the journal.
            funding(journal, ms=stamp, ref=ref, usd=float(p["usd"]),
                    basis="venue", raw=raw)
            paired = supersession_pairs(journal.entries)
            for idx in [i for i, e in enumerate(journal.entries)
                        if e.kind == "funding" and funding_basis(e) == "engine_estimate"
                        and e.ref == f"fund:{p['id']}" and i not in paired]:
                old = journal.entries[idx]
                journal.reverse(old.ref, ms=stamp, original=old, why=(
                    f"estimate superseded by venue settlement {p['id']!r}"),
                    raw={"supersedes": old.ref,
                         "superseded_by_venue_id": str(p["id"])})
                out["superseded"] = out.get("superseded", 0) + 1
            out["booked"] += 1
    return out


def check_funding_completeness(journal: Journal, print_ms: List[int], *,
                               now_ms: int) -> int:
    """Flag settlements the book's position was held through with no venue row.

    ``print_ms`` are the public funding settlement times. Each one the book held
    a stable position through, and old enough to have been delivered, must have
    a matching venue payment (booked or unattributed) or it is recorded, once, as
    ``unread``: a missing PAID-funding row would otherwise overstate income while
    the statement said confirmed. Returns how many new flags were written.
    """
    have = [int(e.fee_raw["venue_ms"]) for e in journal.entries
            if e.basis in ("venue", "unattributed") and "venue_ms" in e.fee_raw]
    flagged = {e.ref for e in journal.entries if e.kind == "funding_missing"}
    stamp = max(int(now_ms), journal.entries[-1].ms if journal.entries else 0)
    written = 0
    for t in sorted(int(x) for x in print_ms):
        if t > now_ms - MISSING_GRACE_MS or f"fundmissing:{t}" in flagged:
            continue
        before = _perp_qty_at(journal, t - ATTRIBUTION_SLACK_MS)
        after = _perp_qty_at(journal, t + ATTRIBUTION_SLACK_MS)
        if (abs(before) <= DUST[PERP] or abs(after) <= DUST[PERP]
                or _perp_moved_within(journal, t - ATTRIBUTION_SLACK_MS,
                                      t + ATTRIBUTION_SLACK_MS)):
            # Flat on EITHER side of the print, or the quantity moved inside the
            # window (a close and a same-size reopen is flat for a moment): the book opened or closed within
            # the attribution slack of it, so whether it held through the
            # settlement is not established by the bot's clock. That is the
            # OWNERSHIP_AMBIGUOUS case (funding_reconcile), not a missing venue
            # row; a permanent "held a position through it" flag would be false.
            continue
        if any(abs(h - t) <= PRINT_MATCH_TOLERANCE_MS for h in have):
            continue
        journal.post(Entry(
            ms=stamp, kind="funding_missing", ref=f"fundmissing:{t}",
            lines=[Line(USDT, 0.0), Line(FUNDING, 0.0)],
            memo=f"the book held a position through the {t} settlement and the "
                 "venue returned no payment row for it"[:300],
            basis="unread", fee_raw={"print_ms": t}))
        written += 1
    return written


def mark_funding_unread(journal: Journal, *, ms: int, why: str) -> None:
    """Record that funding could not be read while a position was open.

    A $0 entry, so it persists and the statement cannot claim confirmed funding.
    At most one per hour: a venue that stays down must not flood the journal.
    """
    ref = f"fundunread:{int(ms) // 3_600_000}"
    if any(e.ref == ref for e in journal.entries):
        return
    stamp = max(int(ms), journal.entries[-1].ms if journal.entries else 0)
    journal.post(Entry(ms=stamp, kind="funding_unread", ref=ref,
                       lines=[Line(USDT, 0.0), Line(FUNDING, 0.0)],
                       memo=f"funding unreadable with a position open: {why}"[:300],
                       basis="unread"))


INVENTORY_UNBOOKED_KIND = "inventory_unbooked"


def mark_inventory_unbooked(journal: Journal, *, ms: int, ref: str, why: str) -> None:
    """Record that an overlay cycle's inventory leg was NOT valued.

    The hedged pair's economic result is the perp's P&L PLUS the mark on the coin it hedges. When that mark cannot be taken (no spot reference, a
    partial open, a failed read) the cash net still stands, but a "pair net" computed from the books would silently equal the cash net: the perp's gain
    or loss with nothing against it. A $0 entry, so it persists; ``statement`` then withholds ``pair_net_usd`` instead of reporting that figure.
    """
    eref = f"invunbooked:{ref}"
    if any(e.ref == eref for e in journal.entries):
        return
    stamp = max(int(ms), journal.entries[-1].ms if journal.entries else 0)
    journal.post(Entry(ms=stamp, kind=INVENTORY_UNBOOKED_KIND, ref=eref,
                       lines=[Line(USDT, 0.0), Line(REALISED, 0.0)],
                       memo=f"overlay inventory leg not valued for {ref}: {why}"[:300]))


def borrow(journal: Journal, *, ms: int, ref: str, usd: float) -> Entry:
    return journal.post(Entry(ms=ms, kind="borrow", ref=ref, lines=[
        Line(BORROW, usd),
        Line(USDT, -usd, qty=-usd)]))


#: The only valuation basis a revaluation may claim. A revaluation is a MARK of
#: coin the book still holds (or never traded): it is not cash, and the report
#: keeps it out of every realised-cash figure on the strength of this label.
MARK_VALUATION = "MARK_TO_MARKET"


def revalue_inventory(journal: Journal, *, ms: int, ref: str, qty: float,
                      from_price: float, to_price: float,
                      raw: Optional[Dict[str, Any]] = None) -> Entry:
    """Mark the hedged BTC to a new price.

    In the OVERLAY the book never trades the client's coin, so without this
    the journal shows the short's loss and not the inventory gain that offsets
    it — and a delta-neutral book reads as a catastrophic naked short. That is
    not a presentational nicety: the whole product claim is that those two
    move together, and a ledger that shows one of them cannot evidence it.

    `hedge_effectiveness` is what reads the pair back out.
    """
    change = qty * (to_price - from_price)
    return journal.post(Entry(ms=ms, kind="revalue", ref=ref, lines=[
        Line(BTC, change),
        Line(REALISED, -change)],
        memo=f"{qty} BTC from {from_price} to {to_price}",
        fee_raw=dict(raw or {})))


def hedge_effectiveness(*, perp_realised_usd: float,
                        inventory_change_usd: float) -> Dict[str, Any]:
    """Did the hedge hedge? The two numbers, and their sum.

    A delta-neutral overlay claims these cancel. The residual is the BASIS —
    the only price exposure the book actually runs — and naming it is the
    difference between a claim and evidence.
    """
    residual = perp_realised_usd + inventory_change_usd
    scale = max(abs(perp_realised_usd), abs(inventory_change_usd), 1.0)
    return {"perp_realised_usd": perp_realised_usd,
            "inventory_change_usd": inventory_change_usd,
            "residual_usd": residual,
            "residual_fraction_of_leg": residual / scale,
            "note": "residual is the BASIS: the only price exposure the "
                    "overlay runs. It is not expected to be zero; it is "
                    "expected to be small, and measured rather than assumed."}


def hedgeable_btc(journal: Journal) -> float:
    """What the WALLET holds, which is what may be hedged — not what filled."""
    return journal.balances()[BTC].qty


# -- reconciliation -------------------------------------------------------

def reconcile(journal: Journal, *, venue_btc: float, venue_usdt: float,
              venue_perp_qty: float) -> Dict[str, Any]:
    """What the book believes against what the venue holds.

    OBSERVES. It never writes, and it never adjusts the journal to agree with
    the venue: a ledger that edits itself to match whatever it finds cannot
    detect theft, a bug, or a fat finger. A disagreement is an INCIDENT and a
    human's problem.
    """
    book = journal.balances()
    checks = {
        "btc": (book[BTC].qty, float(venue_btc), DUST[BTC]),
        "usdt": (book[USDT].qty, float(venue_usdt), DUST[USDT]),
        "perp": (abs(book[PERP].qty), abs(float(venue_perp_qty)), DUST[PERP]),
    }
    disagrees = [name for name, (mine, theirs, dust) in checks.items()
                 if not math.isfinite(theirs) or abs(mine - theirs) > dust]
    return {
        "verdict": "INCIDENT" if disagrees else "RECONCILED",
        "disagrees_on": disagrees,
        "detail": {name: {"book": mine, "venue": theirs,
                          "drift": mine - theirs}
                   for name, (mine, theirs, _d) in checks.items()},
        "note": ("the journal is NOT adjusted; a book that edits itself to "
                 "agree with the venue cannot detect a bug"),
    }


# -- the broker wrapper ---------------------------------------------------

#: Status of the execution record of an order the venue accepted whose local
#: booking failed. Never a fill, never a miss, never a cost sample.
UNBOOKED_STATUS = "UNBOOKED"


class BookingFailedAfterFill(RuntimeError):
    """An order was sent and answered, and the journal could not book it.

    ``outcome_unknown`` is what the engine tests (as for
    ``carry_broker.OrderOutcomeUnknown``), so it needs no import of this
    module: the book halts instead of treating the leg as not filled.
    """

    outcome_unknown = True


class LedgerBroker:
    """Every order the engine sends, booked — because it cannot go elsewhere.

    A ledger the engine has to remember to call is a ledger the engine can
    forget to call, and the forgetting is silent. The broker is the engine's
    ONLY path to the venue: every leg, in every execution mode, under every
    execution style, goes through `place_market` or `place_post_only`.

    Delegation is EXPLICIT — there is no `__getattr__` that forwards anything
    it is handed. A broker method added later must be routed deliberately,
    because the question for each one is always the same: does this move
    money? A wrapper that guesses would answer it silently and wrongly.
    """

    def begin_cycle(self, cycle_id: str) -> None:
        """Name the cycle the engine is about to open (explicit delegation to
        the journal and, when it keeps an order log, the venue adapter; it
        moves no money)."""
        self.journal.begin_cycle(cycle_id)
        inner_naming = getattr(self.inner, "begin_cycle", None)
        if callable(inner_naming):
            inner_naming(cycle_id)

    def __init__(self, inner: Any, journal: Journal,
                 ms: Optional[Callable[[], int]] = None,
                 spot_symbol: str = "BTCUSDT",
                 book_overlay_inventory: bool = False,
                 record_sink: Optional[Callable[[Dict[str, Any]], None]] = None
                 ) -> None:
        self.inner = inner
        #: Called synchronously right after a fill or an order outcome is
        #: booked, so evidence reaches disk at the moment it exists (a hard
        #: kill loses nothing that already happened). It is only an observer:
        #: an exception from it is counted and logged, and never touches the
        #: order, the booking or the engine.
        self.record_sink = record_sink
        self.sink_errors = 0
        self.sink_last_error = ""
        self.journal = journal
        self.spot_symbol = spot_symbol
        self._ms = ms or (lambda: 0)
        #: OVERLAY ONLY. The book never trades the client's BTC, so without a
        #: revaluation the journal holds the short's price move and not the
        #: coin it hedges. When on, each close marks the hedged quantity from
        #: the spot seen at entry to the spot seen at exit (`revalue_inventory`,
        #: the entry the replay uses). Off in acquire, where `spot_sell`
        #: already realises the move: both would count the same dollars twice.
        self.book_overlay_inventory = bool(book_overlay_inventory)
        #: The spot the engine decided on this tick (None outside a tick).
        self._hedge_ref: Optional[float] = None
        self._hedge_ref_ms: Optional[int] = None
        #: Quantity of the open short whose entry spot is known, and the sum of
        #: qty x spot over it. A short with an unknown part is never averaged
        #: over the gap: the close then books no revaluation at all.
        self._hedge_ref_qty: float = 0.0
        self._hedge_ref_cost: float = 0.0
        #: Entry price per open short, so a close can realise against it.
        self._perp_entry: Optional[float] = None
        self._perp_qty: float = 0.0
        #: The same thing for the SPOT leg, which did not have it. Without a
        #: remembered purchase price a sale has nothing to realise against,
        #: and the code below realised it against ITSELF.
        self._spot_entry: Optional[float] = None
        self._spot_qty: float = 0.0
        #: What has already been booked for one resting order, by link id:
        #: `(side, qty_booked, fee_booked)`. A venue reports a cancel's fill
        #: as the order's CUMULATIVE total (`cumExecQty`), and the post-only
        #: call has usually booked part of it already — so the cancel must
        #: book the INCREMENT or it records more than the venue holds. The
        #: side lives here because a cancel response does not carry one.
        self._resting: Dict[str, tuple] = {}
        #: Every fill this broker booked, in order: what the journal alone does
        #: not keep (the fill PRICE, the order link id as a field, the fee
        #: currency). A drill reads it to state its own round-trip cost and to
        #: name the orders a venue capture must be able to reproduce. In
        #: memory only: the journal stays the book, this is its index.
        self.fills: List[Dict[str, Any]] = []
        #: The decision each (symbol, product, side) is currently trading on,
        #: stated by the engine BEFORE it sends (`begin_decision`). Never
        #: filled in from an observation taken after the order.
        self._decisions: Dict[Tuple[str, str, str], Any] = {}
        #: Execution records (execution_record.py) of every order this broker
        #: sent, including the ones that filled nothing. In memory; the same
        #: record of every FILL is persisted on its journal entry
        #: (`fee_raw["execution"]`), so there is no second ledger.
        self.executions: List[Dict[str, Any]] = []
        #: What a resting order was decided on and asked for, by link id, so
        #: a cancel-time fill is measured against the same decision.
        self._resting_exec: Dict[str, tuple] = {}

    # -- decision-time context (execution_record) -------------------------

    def begin_decision(self, *, symbol: str, product: str, side: str,
                       qty: float, mark: Optional[float] = None,
                       bid: Optional[float] = None, ask: Optional[float] = None,
                       decision_ms: Optional[int] = None,
                       observed_ms: Optional[int] = None,
                       expected_fee_usd: Optional[float] = None,
                       expected_cost_usd: Optional[float] = None,
                       capture_book: bool = True,
                       book_source: str = "",
                       book_timeout_s: Optional[float] = None,
                       route: str = "", purpose: str = "",
                       execution_mode: str = "") -> None:
        """State what the strategy sees at the moment it decides to trade.

        Call it immediately before the order(s) for this leg. With no bid/ask
        and ``capture_book`` it reads the touch NOW (public, read-only), which
        is a decision-time observation; if that read fails the reference is
        the mark alone, or nothing when there is no mark either. Nothing here
        moves money. A context is replaced by the next call for the same
        (symbol, product, side) and ignored once stale.

        The read prefers the public order book (``get_book_depth``: touch plus
        the ladders, so the record can state a depth-walked VWAP) and falls
        back to the public ticker touch (``get_book_top``). ``book_timeout_s``
        bounds it in wall-clock time: an exit passes a small one so that a slow
        or hung venue read can never hold the order back; None = unbounded
        (an entry is optional and may wait). A book is never built from the
        mark or a last price, and a read that returns nothing usable is the
        same as no read.
        """
        import execution_record as xr
        now = int(self._ms())
        source, venue_ts, bids, asks = str(book_source or ""), None, None, None
        if bid is None and ask is None and capture_book:
            touch = self._read_touch(symbol, product, timeout_s=book_timeout_s)
            if touch is not None:
                bid, ask = touch["bid"], touch["ask"]
                observed_ms = now
                source = touch["source"]
                venue_ts = touch.get("venue_ts_ms")
                bids, asks = touch.get("bids"), touch.get("asks")
        elif (bid is not None or ask is not None) and not source:
            source = "supplied"
        self._decisions[(symbol, product, side)] = xr.DecisionContext(
            venue=str(getattr(self.inner, "VENUE", "unknown")),
            symbol=symbol, product=product, side=side,
            decision_ms=now if decision_ms is None else decision_ms,
            qty=qty, mark=mark, bid=bid, ask=ask, observed_ms=observed_ms,
            expected_fee_usd=expected_fee_usd,
            expected_cost_usd=expected_cost_usd, book_source=source,
            book_venue_ts_ms=venue_ts, bids=bids, asks=asks,
            route=str(route or ""), purpose=str(purpose or ""),
            execution_mode=str(execution_mode or ""))

    def _read_touch(self, symbol: str, product: str, *,
                    timeout_s: Optional[float]) -> Optional[Dict[str, Any]]:
        """``{bid, ask, source, venue_ts_ms, bids, asks}`` from a public read,
        or None. Never raises.

        With ``timeout_s`` the whole read (depth, then the ticker fallback)
        shares ONE deadline, and each source is handed what is left of it as
        its own request timeout (a single attempt, no retry, no backoff) - the
        wait is bounded by the transport, not by a thread (live modules start
        none). A source that cannot take a timeout is NOT called when a bound
        was requested."""
        deadline = None if timeout_s is None else time.monotonic() + float(timeout_s)
        for name, source in (("get_book_depth", "orderbook"),
                             ("get_book_top", "ticker_touch")):
            getter = getattr(self.inner, name, None)
            if not callable(getter):
                continue
            kwargs: Dict[str, Any] = {}
            if deadline is not None:
                left = deadline - time.monotonic()
                if left <= 0.01:
                    return None                  # the budget is spent
                try:
                    takes_timeout = "timeout_s" in inspect.signature(
                        getter).parameters
                except (TypeError, ValueError):
                    takes_timeout = False
                if not takes_timeout:
                    # A bound was asked for and this getter cannot honour one:
                    # calling it would be an unbounded wait in front of an
                    # order. The book is simply not read (an unmeasured record,
                    # never an invented one).
                    continue
                kwargs["timeout_s"] = left
            try:
                top = getter(symbol, product, **kwargs)
                bid, ask = float(top["bid"]), float(top["ask"])
            except Exception:  # noqa: BLE001 - the next source, or none
                continue
            if not (math.isfinite(bid) and math.isfinite(ask)) \
                    or bid <= 0 or ask <= 0 or bid > ask:
                continue
            out: Dict[str, Any] = {"bid": bid, "ask": ask, "source": source}
            if source == "orderbook":
                out.update(venue_ts_ms=top.get("venue_ts_ms"),
                           bids=top.get("bids"), asks=top.get("asks"))
            return out
        return None

    def _environment(self) -> str:
        """The venue environment the inner broker's client talks to ("testnet",
        "mainnet", ...), or "" when it does not say. Provenance only."""
        try:
            value = getattr(getattr(self.inner, "client", None), "venue", None)
        except Exception:  # noqa: BLE001 - a broker with no client
            return ""
        return value if isinstance(value, str) else ""

    #: Execution records kept in memory. The journal entry holds each fill's
    #: record durably; this list is only its in-process index, so it is bounded.
    MAX_EXECUTIONS_KEPT = 5000

    def _emit(self, event: Dict[str, Any]) -> None:
        """Hand one booked fill / outcome to the sink. Never raises."""
        sink = self.record_sink
        if sink is None:
            return
        try:
            sink(event)
        except Exception as exc:  # noqa: BLE001 - an observer must not stop an order
            self.sink_errors += 1
            self.sink_last_error = f"{type(exc).__name__}: {exc}"
            logging.getLogger(__name__).warning(
                "execution record not written durably: %s",
                self.sink_last_error)

    def _keep_execution(self, record: Dict[str, Any]) -> None:
        self.executions.append(record)
        extra = len(self.executions) - self.MAX_EXECUTIONS_KEPT
        if extra > 0:
            del self.executions[:extra]

    def end_decision(self, *, symbol: str, product: str, side: str) -> None:
        self._decisions.pop((symbol, product, side), None)

    def _note_failed_order(self, *, symbol: str, side: str, product: str,
                           qty: float, order_type: str, why: str) -> None:
        import execution_record as xr
        ctx = self._decisions.get((symbol, product, side))
        self._keep_execution(xr.failed_submission(
            ctx, side=side, symbol=symbol, product=product,
            venue=str(getattr(self.inner, "VENUE", "unknown")),
            order_type=order_type, requested_qty=qty, why=why,
            environment=self._environment(), recorded_ms=int(self._ms())))
        self._emit({"type": "outcome", "execution": self.executions[-1]})

    def _note_unfilled(self, result: Dict[str, Any], *, side: str,
                       product: str, symbol: str, requested_qty: Any,
                       order_type: str, qty: float, price: float,
                       why: str = "") -> None:
        """Keep the record of an order the venue ANSWERED that booked nothing:
        a market order that filled zero, or a fill that cannot be booked. It is
        execution-outcome evidence (status NO_FILL / UNAVAILABLE), never a
        sample, and it must not stop the caller."""
        if not side:
            return
        try:
            import execution_record as xr
            symbol = symbol or str(result.get("symbol") or "")
            ctx = self._decisions.get((symbol, product, side))
            rec = xr.assess_fill(
                ctx, side=side, symbol=symbol, product=product,
                venue=str(result.get("venue")
                          or getattr(self.inner, "VENUE", "unknown")),
                order_type=str(result.get("order_type") or order_type),
                requested_qty=(requested_qty if requested_qty is not None
                               else result.get("requested_qty")),
                filled_qty=qty, avg_price=price, now_ms=int(self._ms()),
                order_link_id=str(result.get("order_link_id") or ""),
                submit_ms=result.get("submit_ms"),
                fill_ms=result.get("fill_ms"),
                fill_ms_basis=str(result.get("fill_ms_basis") or ""),
                environment=self._environment())
            rec["recorded_ms"] = int(self._ms())
            if why:
                rec["reasons"] = list(rec.get("reasons") or []) + [why]
            self._keep_execution(rec)
            self._emit({"type": "outcome", "execution": rec})
        except Exception:  # noqa: BLE001 - bookkeeping never stops an order
            pass

    def _note_fill(self, *, ref: str, ms: int, product: str, side: str,
                   qty: float, price: float, fee_usdt: float, fee_btc: float,
                   status: str, fee_raw: Dict[str, Any]) -> None:
        if isinstance(fee_raw.get("execution"), dict):
            self._keep_execution(fee_raw["execution"])
        currencies = sorted({str(c.get("currency") or "")
                             for c in (fee_raw.get("components") or [])
                             if c.get("amount")})
        self.fills.append({
            "ref": ref, "ms": ms, "product": product, "side": side,
            "qty": qty, "price": price, "fee_usdt": fee_usdt,
            "fee_btc": fee_btc, "fee_status": status,
            "fee_currencies": currencies})
        kind = {("spot", "Buy"): "spot_buy", ("spot", "Sell"): "spot_sell",
                ("linear", "Sell"): "perp_open",
                ("linear", "Buy"): "perp_close"}.get((product, side))
        if kind:
            self._emit({"type": "fill", "kind": kind, "ms": int(ms),
                        "ref": str(ref), "fee_raw": fee_raw})

    def note_hedge_reference(self, spot: Optional[float]) -> None:
        """The spot price the engine decided on, for the overlay's inventory
        leg. None (outside a tick, or no spot) means "not observed"; nothing is
        carried over from an earlier tick."""
        ok = (isinstance(spot, (int, float)) and not isinstance(spot, bool)
              and math.isfinite(spot) and spot > 0)
        self._hedge_ref = float(spot) if ok else None
        #: When the engine saw it: a mark's age is part of its provenance.
        self._hedge_ref_ms = int(self._ms()) if ok else None

    def _hedge_reference_now(self, symbol: str
                             ) -> Optional[Tuple[float, str, int]]:
        """(spot, source, observed_ms): the tick's spot, else one public read
        of the spot mark (after the fill is booked, so it can never delay an
        order). None when neither exists; a failed read is "not observed",
        never a guess."""
        if self._hedge_ref is not None and self._hedge_ref_ms is not None:
            return self._hedge_ref, "tick_spot_mark", self._hedge_ref_ms
        getter = getattr(self.inner, "get_spot_mark", None)
        if not callable(getter):
            return None
        try:
            px = float(getter(self.spot_symbol or symbol))
        except Exception:  # noqa: BLE001
            return None
        if not (math.isfinite(px) and px > 0):
            return None
        return px, "public_spot_read", int(self._ms())

    def _rebuild_hedge_references(self) -> None:
        """Re-derive the entry spot of the open short from the journal (each
        open fill carries `fee_raw["hedge_spot_ref"]`), so a restart does not
        forget what the client's coin was worth when the short was opened."""
        pos = ref_qty = cost = 0.0
        for e in self.journal.entries:
            if e.kind not in ("perp_open", "perp_close"):
                continue
            leg = next((ln for ln in e.lines if ln.account == PERP), None)
            if leg is None:
                continue
            q = abs(float(leg.qty))
            if e.kind == "perp_open":
                ref = (e.fee_raw or {}).get("hedge_spot_ref")
                pos += q
                if isinstance(ref, (int, float)) and not isinstance(ref, bool) \
                        and math.isfinite(ref) and ref > 0:
                    ref_qty += q
                    cost += q * float(ref)
            else:
                if pos > 0 and ref_qty > 0:
                    share = min(1.0, q / pos)
                    ref_qty -= ref_qty * share
                    cost -= cost * share
                pos = max(0.0, pos - q)
                if pos <= 1e-12:
                    pos = ref_qty = cost = 0.0
        self._hedge_ref_qty, self._hedge_ref_cost = ref_qty, cost

    def resume_basis(self, *, perp_qty: float = 0.0,
                     perp_price: Optional[float] = None,
                     spot_qty: float = 0.0,
                     spot_price: Optional[float] = None) -> None:
        """Re-seed the entry prices a restart wiped from this process.

        The weighted entries above live in memory only. A restored pair would
        otherwise close with ``entry = exit price`` and book its whole move as
        a realised zero. The engine calls this with the legs it restored from
        its own ledger, once the venue has agreed with them. A leg with no
        recorded price (an overlay's inventory) is left unseeded.
        """
        if perp_qty > 0 and perp_price is not None and perp_price > 0:
            self._perp_qty, self._perp_entry = float(perp_qty), float(perp_price)
        if spot_qty > 0 and spot_price is not None and spot_price > 0:
            self._spot_qty, self._spot_entry = float(spot_qty), float(spot_price)
        if self.book_overlay_inventory and perp_qty > 0:
            self._rebuild_hedge_references()

    # -- orders: booked ---------------------------------------------------

    def place_market(self, *, symbol: str, side: str, qty: float,
                     product: str, order_link_id=None):
        try:
            if order_link_id is None:
                result = self.inner.place_market(symbol=symbol, side=side,
                                                 qty=qty, product=product)
            else:
                result = self.inner.place_market(symbol=symbol, side=side,
                                                 qty=qty, product=product,
                                                 order_link_id=order_link_id)
        except Exception as exc:  # noqa: BLE001 - re-raised unchanged
            self._note_failed_order(
                symbol=symbol, side=side, product=product, qty=qty,
                order_type="market",
                why=f"SUBMISSION_FAILED: {type(exc).__name__}")
            raise
        if not result:
            self._note_failed_order(symbol=symbol, side=side, product=product,
                                    qty=qty, order_type="market",
                                    why="NO_ORDER: venue returned no fill")
        # The venue already ACCEPTED this order. A booking error is not "the
        # order failed": reporting it as one made the engine buy the spot again
        # on every tick, or sell the spot under a perp short that had filled.
        # The fill is real and the book cannot account for it, which is exactly
        # an outcome the engine must halt on (see `_book_checked`).
        self._book_checked(result, side=side, product=product, symbol=symbol,
                           requested_qty=qty, order_type="market",
                           what=f"{side} {qty} {symbol} ({product})")
        return result

    def place_post_only(self, *, symbol: str, side: str, qty: float,
                        price: float, product: str):
        try:
            result = self.inner.place_post_only(
                symbol=symbol, side=side, qty=qty, price=price,
                product=product)
        except Exception as exc:  # noqa: BLE001 - re-raised unchanged
            self._note_failed_order(
                symbol=symbol, side=side, product=product, qty=qty,
                order_type="post_only",
                why=f"SUBMISSION_FAILED: {type(exc).__name__}")
            raise
        if not result:
            self._note_failed_order(symbol=symbol, side=side, product=product,
                                    qty=qty, order_type="post_only",
                                    why="NO_ORDER: rejected, nothing rests")
        self._book_checked(result, side=side, product=product, symbol=symbol,
                           requested_qty=qty, order_type="post_only",
                           what=f"post-only {side} {qty} {symbol} ({product})")
        if result:
            link = str(result.get("order_link_id") or "")
            if link:
                booked = float(result.get("filled_qty", 0.0) or 0.0)
                # An order that has filled nothing has paid nothing: that is a
                # fact, not a missing figure. The venue's empty fee row for it
                # reads as UNKNOWN, which would make the whole fee on a later
                # cancel-time fill an unknown difference and drop it.
                baseline = (venue_fees.FeeSet((), True, "zero_fill",
                                              "nothing filled, no fee")
                            if booked <= 0 else self._fee_set_of(result))
                self._resting[link] = (side, max(0.0, booked), baseline)
                self._resting_exec[link] = (
                    symbol, float(qty), result.get("submit_ms"))
        return result

    def cancel_order(self, *, symbol: str, order_link_id: str, product: str):
        """Book what the cancel caught, and only what is NEW.

        A cancel can report a fill it caught on the way out (0040). This used
        to call `_book(side="")`, which returns before booking anything — so
        the fill reached the ENGINE, which sizes its leg from the cancel's
        `filled_qty` (`CarryEngine._rest`), and never reached the JOURNAL. The
        position knew about money the book did not.

        Booking the cancel's number outright would be the opposite error. The
        venue reports the order's CUMULATIVE fill — `carry_broker._order_state`
        reads `cumExecQty` — and `place_post_only` has usually booked part of
        it already. Measured on a stub: post-only books 0.3, the cancel reports
        0.4, and booking that whole 0.4 would record 0.7 against a venue
        holding 0.4.

        So it books the INCREMENT, which is what `_rest` does with the same
        pair of numbers, and it takes the side from what it remembered when the
        order was placed, because a cancel response does not carry one.
        """
        result = self.inner.cancel_order(symbol=symbol,
                                         order_link_id=order_link_id,
                                         product=product)
        known = self._resting.pop(str(order_link_id), None)
        exec_known = self._resting_exec.pop(str(order_link_id), None)
        if not result or known is None:
            # Nothing was remembered for this id, so there is no baseline to
            # take an increment against. Refusing to guess is the same rule
            # the rest of this file follows.
            return result
        side, booked_qty, booked_fee = known
        total = float(result.get("filled_qty", 0.0) or 0.0)
        extra = total - booked_qty
        if extra <= 0:
            if total <= 0 and exec_known is not None:
                # Cancelled with nothing filled at all: that is the outcome of
                # the resting order, and the only place it is known.
                self._note_unfilled(
                    dict(result, order_link_id=str(order_link_id),
                         submit_ms=exec_known[2], fill_ms=None),
                    side=side, product=product,
                    symbol=exec_known[0], requested_qty=exec_known[1],
                    order_type="post_only", qty=0.0, price=0.0,
                    why="CANCELLED_UNFILLED: resting order cancelled with "
                        "nothing filled")
            return result

        # The fee on the increment is the difference of two CUMULATIVE venue
        # figures, taken per currency. Unknown on either side, or a currency
        # that went backwards, has no honest difference: it stays unknown.
        total_fee = self._fee_set_of(result)
        fee_extra = total_fee.minus(booked_fee)
        # "Backwards" means a currency's cumulative MAGNITUDE shrank. A rebate
        # (a negative fee) that grew more negative is a legitimate increment.
        if fee_extra.known and total_fee.known and booked_fee.known:
            def _by_ccy(fees):
                out = {}
                for c in fees.components:
                    out[c.currency] = out.get(c.currency, 0.0) + c.amount
                return out
            now_by, was_by = _by_ccy(total_fee), _by_ccy(booked_fee)
            if any(abs(now_by.get(k, 0.0)) < abs(v) - 1e-12
                   for k, v in was_by.items()):
                fee_extra = venue_fees.UNKNOWN_FEES
        elif fee_extra.known and any(c.amount < -1e-12
                                     for c in fee_extra.components):
            fee_extra = venue_fees.UNKNOWN_FEES
        inc = {"filled_qty": extra,
               "avg_price": result.get("avg_price", 0.0),
               "order_link_id": f"{order_link_id}+cancel",
               "fee_detail": fee_extra.to_dict(),
               "fill_ms": result.get("updated_ms"),
               "fill_ms_basis": "venue_updated_time",
               "response_ms": int(self._ms())}
        requested_left = None
        if exec_known is not None:
            inc["submit_ms"] = exec_known[2]
            requested_left = max(0.0, exec_known[1] - booked_qty)
        self._book_checked(inc, side=side, product=product,
                           symbol=(exec_known[0] if exec_known else ""),
                           requested_qty=requested_left, order_type="post_only",
                           what=f"the cancel-time fill of {order_link_id}")
        return result

    @staticmethod
    def _fee_set_of(result) -> "venue_fees.FeeSet":
        """The fee on a broker result as a FeeSet.

        `fee_detail` (what `CarryBroker` supplies) is authoritative. A result
        with only a bare `fee` number has no currency evidence, so it is
        wrapped with an UNNAMED currency: it can be carried and differenced
        but never confirmed. No `fee` at all is unknown.
        """
        detail = result.get("fee_detail")
        if detail is not None:
            return venue_fees.FeeSet.from_dict(detail)
        raw = result.get("fee")
        if raw is None:
            return venue_fees.UNKNOWN_FEES
        return venue_fees.FeeSet(
            (venue_fees.VenueFee(abs(float(raw)), None),), True, "bare_float")

    def _resolve_fee(self, result, *, side: str, product: str
                     ) -> Tuple[float, float, str, Dict[str, Any]]:
        """``(fee_btc, fee_usdt, status, raw)`` for one fill.

        BTC is booked as BTC only where the venue takes it from the coin
        received (a spot BUY); USDT is the journal's USD. Any other coin has no
        booking treatment here, so it is left out and the entry is flagged
        ``unresolved`` rather than converted on a guess.
        """
        fees = self._fee_set_of(result)
        raw = fees.to_dict()
        if not fees.known:
            return 0.0, 0.0, "unknown", raw
        if fees.source == "bare_float":
            # No currency evidence: keep the historical convention (spot BUY fee
            # in BTC, everything else in USD) but say it is only a convention.
            amount = fees.components[0].amount
            if product == "spot" and side == "Buy":
                return amount, 0.0, "unlabelled", raw
            return 0.0, amount, "unlabelled", raw
        fee_btc = fee_usdt = 0.0
        unresolved = False
        for comp in fees.components:
            if comp.amount == 0.0:
                continue
            if comp.currency == "USDT":
                fee_usdt += comp.amount
            elif comp.currency == "BTC" and product == "spot" and side == "Buy":
                fee_btc += comp.amount
            else:
                unresolved = True
        return fee_btc, fee_usdt, ("unresolved" if unresolved
                                   else "confirmed"), raw

    def _execution_record(self, result, *, side: str, product: str,
                          symbol: str, requested_qty: Any, order_type: str,
                          qty: float, price: float, fee_btc: float,
                          fee_usdt: float, status: str
                          ) -> Tuple[Dict[str, Any], Optional[float]]:
        """``(record, decision_reference)`` for one booked fill.

        The reference is what the journal stores as ``decision_mark``: the
        decision-time executable-side price (or the mark when no book was
        seen), only when the record validates; None otherwise.
        """
        import execution_record as xr
        symbol = symbol or str(result.get("symbol") or "")
        ctx = self._decisions.get((symbol, product, side))
        fee_usd = fee_usdt + fee_btc * price
        fill_ms = result.get("fill_ms")
        rec = xr.assess_fill(
            ctx, side=side, symbol=symbol, product=product,
            venue=str(result.get("venue")
                      or getattr(self.inner, "VENUE", "unknown")),
            order_type=str(result.get("order_type") or order_type),
            requested_qty=(requested_qty if requested_qty is not None
                           else result.get("requested_qty")),
            filled_qty=qty, avg_price=price, now_ms=int(self._ms()),
            fee_usd=fee_usd, fee_confirmed=(status == "confirmed"),
            order_link_id=str(result.get("order_link_id") or ""),
            submit_ms=result.get("submit_ms"), fill_ms=fill_ms,
            fill_ms_basis=str(result.get("fill_ms_basis") or ""),
            environment=self._environment())
        if str(result.get("order_link_id") or "").endswith("+cancel"):
            # what a resting order filled before its cancel: its own population,
            # not a fresh order (it was never decided at this moment)
            rec["route"] = xr.ROUTE_PARTIAL_INCREMENT
        ref = rec.get("reference_price")
        if ref is None:
            # A caller that supplied a mark with the result itself (no engine
            # context): still honoured, still validated as a price.
            legacy = xr._price(result.get("decision_mark"))
            if legacy is not None:
                ref = legacy
        return rec, ref

    def _mark_hedged_inventory(self, *, ms: int, ref: str, qty: float,
                               qty_before: float, symbol: str) -> None:
        """Mark the hedged BTC for the quantity just covered. Never raises: the
        close is already booked, and a failure here leaves the cycle's
        inventory leg unbooked (which keeps it out of evidence), not the fill
        unbooked."""
        try:
            entry_qty, entry_cost = self._hedge_ref_qty, self._hedge_ref_cost
            known = (entry_qty > 0 and qty_before > 0
                     and abs(entry_qty - qty_before) <= 1e-9 + 1e-6 * qty_before)
            if entry_qty > 0:
                share = min(1.0, qty / entry_qty)
                self._hedge_ref_qty = entry_qty - entry_qty * share
                self._hedge_ref_cost = entry_cost - entry_cost * share
            if self._perp_qty <= 0:
                self._hedge_ref_qty = self._hedge_ref_cost = 0.0
            if not known:
                mark_inventory_unbooked(
                    self.journal, ms=ms, ref=ref,
                    why="the entry reference does not cover the quantity just closed")
                return
            seen = self._hedge_reference_now(symbol)
            if seen is None:
                mark_inventory_unbooked(
                    self.journal, ms=ms, ref=ref,
                    why="no spot reference could be read at the close")
                return
            exit_spot, source, observed_ms = seen
            entry_spot = entry_cost / entry_qty
            revalue_inventory(
                self.journal, ms=ms, ref=f"reval:{ref}", qty=qty,
                from_price=entry_spot, to_price=exit_spot,
                raw={"valuation": MARK_VALUATION, "qty": qty,
                     "entry_spot": entry_spot, "exit_spot": exit_spot,
                     "exit_mark_source": source,
                     "exit_mark_observed_ms": observed_ms, "fill_ms": ms,
                     "close_ref": ref,
                     "entry_basis": "quantity-weighted spot mark seen at "
                                    "each open fill",
                     "note": "client BTC marked, not sold: not cash"})
        except Exception:  # noqa: BLE001
            logging.getLogger(__name__).warning(
                "overlay inventory leg not booked for %s", ref, exc_info=True)
            try:
                mark_inventory_unbooked(self.journal, ms=ms, ref=ref,
                                        why="the inventory valuation raised")
            except Exception:  # noqa: BLE001  (the close is booked; never raise from here)
                logging.getLogger(__name__).warning(
                    "could not record the unbooked inventory leg for %s", ref, exc_info=True)

    def _note_unbooked(self, result: Any, *, side: str, product: str,
                       symbol: str, requested_qty: Any, order_type: str,
                       error: BaseException) -> None:
        """Keep, and hand to the sink, the evidence of an order the venue
        ACCEPTED whose local booking failed. It is its own thing: not a fill
        (the journal does not hold one), not a miss (the venue reported a
        fill), never a cost sample. Each stage is stated separately and
        `position_verified` is None - whether the position really changed is an
        independent read that nothing here infers. Never raises."""
        try:
            import execution_record as xr
            res = result if isinstance(result, dict) else {}

            def num(x: Any) -> Any:
                return (x if isinstance(x, (int, float))
                        and not isinstance(x, bool) and math.isfinite(x)
                        else (None if x is None else repr(x)[:80]))
            reported = num(res.get("filled_qty"))
            ctx = self._decisions.get((symbol, product, side))
            rec = {
                "schema": 1, "status": UNBOOKED_STATUS, "booking": "FAILED",
                "route": xr.ROUTE_UNBOOKED,
                "purpose": str(getattr(ctx, "purpose", "") or ""),
                "execution_mode": str(getattr(ctx, "execution_mode", "") or ""),
                "venue": str(res.get("venue")
                             or getattr(self.inner, "VENUE", "unknown")),
                "environment": self._environment(),
                "symbol": symbol or str(res.get("symbol") or ""),
                "product": product, "side": side,
                "order_type": str(res.get("order_type") or order_type),
                "requested_qty": num(requested_qty),
                "reported_filled_qty": reported,
                "reported_avg_price": num(res.get("avg_price")),
                "order_link_id": str(res.get("order_link_id") or ""),
                "decision_ms": getattr(ctx, "decision_ms", None),
                "recorded_ms": int(self._ms()),
                "lifecycle": {
                    "order_submitted": True,
                    "venue_response_received": True,
                    "reported_fill": isinstance(reported, (int, float))
                    and reported > 0,
                    "locally_booked": False,
                    "booking_error": f"{type(error).__name__}: {error}"[:300],
                    "position_verified": None},
                "reasons": ["BOOKING_FAILED_AFTER_ACCEPTANCE: the venue "
                            "answered and the local booking failed; the "
                            "position must be verified independently"]}
            self._keep_execution(rec)
            self._emit({"type": "unbooked", "execution": rec})
        except Exception:  # noqa: BLE001 - evidence is best effort, the raise is not
            pass

    def _book_checked(self, result: Any, *, side: str, product: str,
                      symbol: str, requested_qty: Any, order_type: str,
                      what: str) -> None:
        """`_book`, with one rule for every order path: the venue already
        ACCEPTED this order, so a booking error is not "the order failed". It is
        recorded as an UNBOOKED event and raised as `BookingFailedAfterFill`,
        which the engine treats as an unknown outcome and halts on - it never
        retries or re-sends because accounting failed."""
        try:
            self._book(result, side=side, product=product, symbol=symbol,
                       requested_qty=requested_qty, order_type=order_type)
        except Exception as exc:  # noqa: BLE001
            self._note_unbooked(result, side=side, product=product,
                                symbol=symbol, requested_qty=requested_qty,
                                order_type=order_type, error=exc)
            raise BookingFailedAfterFill(
                f"{what} was sent and the venue returned {result!r}, but "
                f"booking it failed ({type(exc).__name__}: {exc})") from exc

    def _book(self, result, *, side: str, product: str,
              allow_missing_side: bool = False, symbol: str = "",
              requested_qty: Any = None, order_type: str = "market") -> None:
        if not result:
            return
        qty = float(result.get("filled_qty", 0.0) or 0.0)
        price = float(result.get("avg_price", 0.0) or 0.0)
        if qty <= 0 or price <= 0:
            # Nothing is booked. A market order that filled nothing (or a fill
            # that cannot be priced) is still an OUTCOME worth keeping; a
            # resting post-only that has not filled yet is not one: its fate
            # is recorded when it fills or is cancelled.
            if qty > 0 or order_type == "market":
                self._note_unfilled(
                    result, side=side, product=product, symbol=symbol,
                    requested_qty=requested_qty, order_type=order_type,
                    qty=qty, price=price)
            return
        if not side and not allow_missing_side:
            raise LedgerError("a fill with no side cannot be booked")
        if not side:
            return
        fee_btc, fee_usdt, status, fee_raw = self._resolve_fee(
            result, side=side, product=product)
        if product == "spot" and side == "Buy":
            # A base-coin rebate never makes the wallet hold more than the fill
            # and is not booked; the execution record must state the fee that
            # IS booked, not the pre-clamp rebate (one fill, one fee).
            fee_btc = max(0.0, fee_btc)
        # The price the engine DECIDED on (executable side of the book it saw,
        # else its mark), from the context it declared before sending. Anything
        # that does not validate is "not journalled", never a guess; the full
        # record travels on the entry (`fee_raw["execution"]`).
        try:
            exec_rec, mark = self._execution_record(
                result, side=side, product=product, symbol=symbol,
                requested_qty=requested_qty, order_type=order_type, qty=qty,
                price=price, fee_btc=fee_btc, fee_usdt=fee_usdt, status=status)
        except Exception as exc:  # noqa: BLE001
            # Measuring the fill must never stop the fill being booked: the
            # venue already filled it. The record says it could not be made.
            exec_rec = {"status": "UNAVAILABLE",
                        "reasons": [f"EXECUTION_RECORD_ERROR: "
                                    f"{type(exc).__name__}"]}
            mark = None
        fee_raw = dict(fee_raw)
        fee_raw["execution"] = exec_rec
        # NOT kept in `executions` yet: a fill the journal does not hold must
        # not sit there looking like a measured one. `_note_fill` keeps it once
        # the booking has gone through.
        # An order with no link id is still ONE order: its ref carries a
        # running count so two fills in the same millisecond are two refs, not
        # one row the readers would merge as a duplicate.
        ref = str(result.get("order_link_id")
                  or f"fill:{self._ms()}:{len(self.fills)}")
        # Never behind the journal's last entry: a wall clock that stepped back
        # must not turn a venue fill into a booking failure.
        ms = max(int(self._ms()),
                 self.journal.entries[-1].ms if self.journal.entries else 0)

        if product == "spot":
            if side == "Buy":
                # The venue withholds a labelled BTC fee from the coin bought.
                # The broker's basis quantity must therefore follow the wallet's
                # net holding, not the gross fill: unwinds sell that net amount.
                # A negative base fee (a rebate) never makes the wallet hold
                # MORE than the fill: the engine ignores it the same way.
                fee_btc = max(0.0, fee_btc)
                held_qty = qty - fee_btc
                if not math.isfinite(held_qty) or held_qty < -1e-12:
                    raise LedgerError(
                        f"spot BUY base fee {fee_btc} exceeds fill {qty}")
                held_qty = max(0.0, held_qty)
                spot_buy(self.journal, ms=ms, ref=ref, qty=qty, price=price,
                         fee_btc=fee_btc, fee_usdt=fee_usdt,
                         fee_status=status, fee_raw=fee_raw,
                         decision_mark=mark)
                self._note_fill(ref=ref, ms=ms, product=product, side=side,
                                qty=qty, price=price, fee_usdt=fee_usdt,
                                fee_btc=fee_btc, status=status, fee_raw=fee_raw)
                if held_qty > 0:
                    filled = self._spot_qty + held_qty
                    self._spot_entry = (
                        price if self._spot_qty <= 0 or self._spot_entry is None
                        else (self._spot_entry * self._spot_qty
                              + price * held_qty) / filled)
                    self._spot_qty = filled
            else:
                # WHAT IT COST, not what it just sold for.
                #
                # This passed `cost_basis=price` — the SALE price — so `cost`
                # equalled `proceeds` and EQUITY:REALISED was booked as
                # EXACTLY ZERO on every spot sale the book ever made. The
                # money was not lost: the entry still balances, so the gain
                # landed in ASSET:BTC, which then held value at zero quantity.
                # Nothing could see it — `reconcile` compares QUANTITIES,
                # which were right, and returned RECONCILED.
                #
                # The perp leg has tracked its weighted entry since 0044.
                # `spot_sell` itself was correct and is tested directly with a
                # distinct basis; it was the WIRING that realised a sale
                # against itself, and no test drove a spot sell through this
                # wrapper.
                #
                # Falls back to the sale price when nothing was bought through
                # this broker — an OVERLAY never buys the client's coin, so a
                # sale with no recorded purchase has no basis to use and a
                # zero realisation is the honest answer rather than a guess.
                entry = (self._spot_entry if self._spot_entry is not None
                         else price)
                spot_sell(self.journal, ms=ms, ref=ref, qty=qty, price=price,
                          cost_basis=entry, fee_usd=fee_usdt,
                          fee_status=status, fee_raw=fee_raw,
                          decision_mark=mark)
                self._note_fill(ref=ref, ms=ms, product=product, side=side,
                                qty=qty, price=price, fee_usdt=fee_usdt,
                                fee_btc=fee_btc, status=status, fee_raw=fee_raw)
                self._spot_qty = max(0.0, self._spot_qty - qty)
                if self._spot_qty <= 0:
                    self._spot_entry = None
            return

        if side == "Sell":
            hedge_ref = None
            if self.book_overlay_inventory:
                seen = self._hedge_reference_now(symbol)
                hedge_ref = seen[0] if seen else None
                fee_raw = dict(fee_raw or {})
                fee_raw["hedge_spot_ref"] = hedge_ref
                fee_raw["hedge_spot_ref_source"] = seen[1] if seen else None
                fee_raw["hedge_spot_ref_observed_ms"] = (
                    seen[2] if seen else None)
            perp_open(self.journal, ms=ms, ref=ref, qty=qty, price=price,
                      fee_usd=fee_usdt, fee_status=status, fee_raw=fee_raw,
                      decision_mark=mark)
            if hedge_ref is not None:
                self._hedge_ref_qty += qty
                self._hedge_ref_cost += qty * hedge_ref
            self._note_fill(ref=ref, ms=ms, product=product, side=side,
                            qty=qty, price=price, fee_usdt=fee_usdt,
                            fee_btc=fee_btc, status=status, fee_raw=fee_raw)
            filled = self._perp_qty + qty
            self._perp_entry = (
                price if self._perp_entry is None else
                (self._perp_entry * self._perp_qty + price * qty) / filled)
            self._perp_qty = filled
        else:
            entry = self._perp_entry if self._perp_entry is not None else price
            perp_close(self.journal, ms=ms, ref=ref, qty=qty, price=price,
                       entry_price=entry, fee_usd=fee_usdt,
                       fee_status=status, fee_raw=fee_raw,
                       decision_mark=mark)
            self._note_fill(ref=ref, ms=ms, product=product, side=side,
                            qty=qty, price=price, fee_usdt=fee_usdt,
                            fee_btc=fee_btc, status=status, fee_raw=fee_raw)
            before = self._perp_qty
            self._perp_qty = max(0.0, self._perp_qty - qty)
            if self._perp_qty <= 0:
                self._perp_entry = None
            if self.book_overlay_inventory:
                self._mark_hedged_inventory(ms=ms, ref=ref, qty=qty,
                                            qty_before=before, symbol=symbol)

    # -- reads: forwarded, one by one -------------------------------------

    def get_mark(self, symbol): return self.inner.get_mark(symbol)

    def get_spot_mark(self, symbol): return self.inner.get_spot_mark(symbol)

    def get_funding_bps(self, symbol): return self.inner.get_funding_bps(symbol)

    def get_funding_print(self, symbol):
        return self.inner.get_funding_print(symbol)

    def get_margin_multiple(self, symbol):
        return self.inner.get_margin_multiple(symbol)

    def get_lot_rules(self, symbol, product):
        return self.inner.get_lot_rules(symbol, product)

    def get_fee_rates(self, symbol, product):
        return self.inner.get_fee_rates(symbol, product)

    def get_spot_inventory(self, symbol):
        return self.inner.get_spot_inventory(symbol)

    def get_venue_time_s(self):
        return self.inner.get_venue_time_s()

    def get_position_leverage(self, symbol):
        return self.inner.get_position_leverage(symbol)

    def get_position_mode(self, symbol):
        # Cold start refuses a hedge-mode account through THIS method; a
        # wrapper that did not forward it made that check silently vanish.
        return self.inner.get_position_mode(symbol)

    def get_perp_position(self, symbol):
        return self.inner.get_perp_position(symbol)

    def get_perp_net_qty(self, symbol):
        return self.inner.get_perp_net_qty(symbol)

    def get_funding_history(self, symbol, limit=8):
        return self.inner.get_funding_history(symbol, limit)

    def get_funding_payments(self, symbol, start_ms, end_ms=None):
        return self.inner.get_funding_payments(symbol, start_ms, end_ms)

    def get_funding_history_range(self, symbol, start_ms, end_ms):
        return self.inner.get_funding_history_range(symbol, start_ms, end_ms)

    def get_funding_evidence(self, symbol, start_ms, end_ms=None):
        return self.inner.get_funding_evidence(symbol, start_ms, end_ms)

    def get_liquidation_view(self, symbol):
        return self.inner.get_liquidation_view(symbol)

    def get_available_margin_usd(self):
        return self.inner.get_available_margin_usd()

    def get_collateral_facts(self, symbol):
        return self.inner.get_collateral_facts(symbol)

    def get_quote_free_usd(self, coin="USDT"):
        return self.inner.get_quote_free_usd(coin)

    def get_closed_short_qty_since(self, symbol, since_ms):
        return self.inner.get_closed_short_qty_since(symbol, since_ms)

    def get_open_carry_orders(self, spot_symbol, perp_symbol):
        return self.inner.get_open_carry_orders(spot_symbol, perp_symbol)

    def get_book_top(self, symbol, product):
        return self.inner.get_book_top(symbol, product)

    def reconcile_pair(self, **kwargs):
        return self.inner.reconcile_pair(**kwargs)
