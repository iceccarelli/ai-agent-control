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

import math
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


@dataclass
class Balance:
    usd: float = 0.0
    qty: float = 0.0


def _net_basis(fee_counts: Dict[str, int], fund_counts: Dict[str, int]) -> str:
    """How far the net figure can be trusted, in one word."""
    if sum(fee_counts.values()) + sum(fund_counts.values()) == 0:
        return "no_fills"
    if any(fee_counts[k] for k in ("unknown", "unresolved", "unlabelled")):
        return "provisional_fees_incomplete"
    if any(fund_counts[k] for k in ("engine_estimate", "unattributed", "unread")):
        return "provisional_funding_unverified"
    return "confirmed"


class Journal:
    """Append-only. Balanced. The only thing that knows what the book owns."""

    def __init__(self) -> None:
        self.entries: List[Entry] = []

    @property
    def unknown_fees(self) -> int:
        """Fills whose fee the venue did not state, derived from the entries
        (so it survives a reload). Each is booked at 0 and FLAGGED: an unknown
        cost that reads as $0.00 is a cost that has left the ledger."""
        return sum(1 for e in self.entries if e.fee_status == "unknown")

    # -- writing ----------------------------------------------------------

    def post(self, entry: Entry) -> Entry:
        """Validate, then append. A refusal writes nothing at all."""
        if not entry.lines:
            raise LedgerError(f"entry {entry.ref!r} has no lines")
        if entry.basis not in FUNDING_BASES:
            raise LedgerError(
                f"entry {entry.ref!r} has basis {entry.basis!r}; expected one "
                f"of {FUNDING_BASES}")
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
        self.entries.append(entry)
        return entry

    def reverse(self, ref: str, *, ms: int, why: str) -> Entry:
        """Undo an entry the only way a ledger may: by writing another one."""
        original = next((e for e in self.entries if e.ref == ref), None)
        if original is None:
            raise LedgerError(f"no entry {ref!r} to reverse")
        return self.post(Entry(
            ms=ms, kind="reversal", ref=f"{ref}~rev",
            lines=[Line(l.account, -l.usd, -l.qty) for l in original.lines],
            memo=f"reverses {ref}: {why}"))

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
                  to_ms: Optional[int] = None) -> Dict[str, Any]:
        """What the book earned over a period, and the proof that it sums.

        Raises when the identity fails, exactly as `carry_backtest.attribution`
        does. A statement that does not add up is not a statement.
        """
        acc = {name: Balance() for name in ACCOUNTS}
        total_usd = 0.0
        fee_counts = {k: 0 for k in FEE_STATUSES if k}
        fund_counts = {k: 0 for k in FUNDING_BASES if k}
        fund_usd = {k: 0.0 for k in FUNDING_BASES if k}
        for entry in self.entries:
            if from_ms is not None and entry.ms < from_ms:
                continue
            if to_ms is not None and entry.ms > to_ms:
                continue
            if entry.fee_status:
                fee_counts[entry.fee_status] += 1
            if entry.basis:
                fund_counts[entry.basis] += 1
                fund_usd[entry.basis] += -sum(
                    l.usd for l in entry.lines if l.account == FUNDING)
            for line in entry.lines:
                acc[line.account].usd += line.usd
                acc[line.account].qty += line.qty
                total_usd += line.usd
        if abs(total_usd) > BALANCE_TOLERANCE:
            raise LedgerError(
                f"the journal does not balance: ${total_usd:.6f} unaccounted. "
                "Some entry was written by something other than post().")

        funding_usd = -acc[FUNDING].usd          # income is credit-normal
        fees_usd = acc[FEES].usd
        borrow_usd = acc[BORROW].usd
        realised_usd = -acc[REALISED].usd
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
            "funding_estimated_entries": fund_counts["engine_estimate"],
            "funding_unattributed": fund_counts["unattributed"],
            "funding_unread": fund_counts["unread"],
            "funding_confirmed": all(
                fund_counts[k] == 0 for k in ("engine_estimate",
                                              "unattributed", "unread")),
            "net_basis": _net_basis(fee_counts, fund_counts),
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
        return [{"ms": e.ms, "kind": e.kind, "ref": e.ref, "memo": e.memo,
                 "fee_status": e.fee_status, "fee_raw": e.fee_raw,
                 "basis": e.basis,
                 "lines": [{"account": l.account, "usd": l.usd, "qty": l.qty}
                           for l in e.lines]} for e in self.entries]

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
                lines=[Line(str(l["account"]), float(l["usd"]),
                            float(l.get("qty", 0.0)))
                       for l in row["lines"]]))
        return journal


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
             fee_raw: Optional[Dict[str, Any]] = None) -> Entry:
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
        fee_status=fee_status, fee_raw=dict(fee_raw or {})))


def spot_sell(journal: Journal, *, ms: int, ref: str, qty: float, price: float,
              cost_basis: float, fee_usd: float, fee_status: str = "",
              fee_raw: Optional[Dict[str, Any]] = None) -> Entry:
    proceeds = qty * price
    cost = qty * cost_basis
    return journal.post(Entry(ms=ms, kind="spot_sell", ref=ref, lines=[
        Line(USDT, proceeds, qty=proceeds),
        Line(BTC, -cost, qty=-qty),
        Line(REALISED, -(proceeds - cost)),
        Line(FEES, fee_usd),
        Line(USDT, -fee_usd, qty=-fee_usd)],
        fee_status=fee_status, fee_raw=dict(fee_raw or {})))


def perp_open(journal: Journal, *, ms: int, ref: str, qty: float, price: float,
              fee_usd: float, fee_status: str = "",
              fee_raw: Optional[Dict[str, Any]] = None) -> Entry:
    """Open (or add to) the short. A derivative costs no principal, so the
    only money that moves is the fee; the position is carried as a quantity."""
    return journal.post(Entry(ms=ms, kind="perp_open", ref=ref, lines=[
        Line(PERP, 0.0, qty=-qty),
        Line(FEES, fee_usd),
        Line(USDT, -fee_usd, qty=-fee_usd)],
        memo=f"short {qty} @ {price}",
        fee_status=fee_status, fee_raw=dict(fee_raw or {})))


def perp_close(journal: Journal, *, ms: int, ref: str, qty: float,
               price: float, entry_price: float, fee_usd: float,
               fee_status: str = "",
               fee_raw: Optional[Dict[str, Any]] = None) -> Entry:
    """Buy the short back. A short gains when the price falls."""
    realised = qty * (entry_price - price)
    return journal.post(Entry(ms=ms, kind="perp_close", ref=ref, lines=[
        Line(PERP, 0.0, qty=qty),
        Line(USDT, realised, qty=realised),
        Line(REALISED, -realised),
        Line(FEES, fee_usd),
        Line(USDT, -fee_usd, qty=-fee_usd)],
        memo=f"cover {qty} @ {price} from {entry_price}",
        fee_status=fee_status, fee_raw=dict(fee_raw or {})))


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
#: A settlement is attributed to the book if the book held a position within
#: this much of it; the venue's own `size` on the row is the real check.
ATTRIBUTION_SLACK_MS = 60_000


def book_funding_payments(journal: Journal, payments: List[Dict[str, Any]], *,
                          ms: int) -> Dict[str, int]:
    """Book what the VENUE paid, once each, and only what is this book's.

    ``payments`` are settlement rows from the account transaction log:
    ``{id, ms, usd, currency, size, symbol}`` with ``usd`` signed (positive =
    received). Rules, in order:

    * a row already in the journal (same ``fund:<id>`` ref) is skipped, so a
      re-poll, a replay and a restart cannot book it twice;
    * a row in another currency, or one the book held no position for, or whose
      venue position size disagrees with the book's quantity, is NOT income of
      this book: it is posted at $0 as ``unattributed`` with the evidence, so a
      human sees it and the statement stops claiming confirmation;
    * everything else is booked at the venue's amount.
    """
    seen = {e.ref for e in journal.entries}
    out = {"booked": 0, "duplicate": 0, "unattributed": 0}
    stamp = max(int(ms), journal.entries[-1].ms if journal.entries else 0)
    for p in sorted(payments, key=lambda r: (int(r["ms"]), str(r["id"]))):
        ref = f"fund:{p['id']}"
        if ref in seen:
            out["duplicate"] += 1
            continue
        seen.add(ref)
        raw = {"venue_id": str(p["id"]), "venue_ms": int(p["ms"]),
               "venue_usd": float(p["usd"]), "currency": p.get("currency"),
               "size": p.get("size"), "symbol": p.get("symbol")}
        reason = None
        if p.get("currency") != SETTLE_CURRENCY:
            reason = (f"paid in {p.get('currency')!r}, not {SETTLE_CURRENCY}; "
                      "no conversion basis")
        else:
            held = abs(journal.balances(
                to_ms=int(p["ms"]) + ATTRIBUTION_SLACK_MS)[PERP].qty)
            size = p.get("size")
            if held <= DUST[PERP]:
                reason = "the book held no perp position at this settlement"
            elif size is not None and abs(abs(float(size)) - held) > max(
                    1e-9, 0.01 * held):
                reason = (f"venue position {abs(float(size))} != the book's "
                          f"{held}; part of it is not this book's")
        if reason is not None:
            journal.post(Entry(
                ms=stamp, kind="funding_unattributed", ref=ref,
                lines=[Line(USDT, 0.0), Line(FUNDING, 0.0)],
                memo=f"venue paid {p['usd']:+.6f}: {reason}",
                basis="unattributed", fee_raw=raw))
            out["unattributed"] += 1
        else:
            funding(journal, ms=stamp, ref=ref, usd=float(p["usd"]),
                    basis="venue", raw=raw)
            out["booked"] += 1
    return out


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


def borrow(journal: Journal, *, ms: int, ref: str, usd: float) -> Entry:
    return journal.post(Entry(ms=ms, kind="borrow", ref=ref, lines=[
        Line(BORROW, usd),
        Line(USDT, -usd, qty=-usd)]))


def revalue_inventory(journal: Journal, *, ms: int, ref: str, qty: float,
                      from_price: float, to_price: float) -> Entry:
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
        memo=f"{qty} BTC from {from_price} to {to_price}"))


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

    def __init__(self, inner: Any, journal: Journal,
                 ms: Optional[Callable[[], int]] = None,
                 spot_symbol: str = "BTCUSDT") -> None:
        self.inner = inner
        self.journal = journal
        self.spot_symbol = spot_symbol
        self._ms = ms or (lambda: 0)
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

    # -- orders: booked ---------------------------------------------------

    def place_market(self, *, symbol: str, side: str, qty: float,
                     product: str):
        result = self.inner.place_market(symbol=symbol, side=side, qty=qty,
                                         product=product)
        self._book(result, side=side, product=product)
        return result

    def place_post_only(self, *, symbol: str, side: str, qty: float,
                        price: float, product: str):
        result = self.inner.place_post_only(symbol=symbol, side=side, qty=qty,
                                            price=price, product=product)
        self._book(result, side=side, product=product)
        if result:
            link = str(result.get("order_link_id") or "")
            if link:
                booked = float(result.get("filled_qty", 0.0) or 0.0)
                self._resting[link] = (
                    side, max(0.0, booked), self._fee_set_of(result))
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
        if not result or known is None:
            # Nothing was remembered for this id, so there is no baseline to
            # take an increment against. Refusing to guess is the same rule
            # the rest of this file follows.
            return result
        side, booked_qty, booked_fee = known
        total = float(result.get("filled_qty", 0.0) or 0.0)
        extra = total - booked_qty
        if extra <= 0:
            return result

        # The fee on the increment is the difference of two CUMULATIVE venue
        # figures, taken per currency. Unknown on either side, or a currency
        # that went backwards, has no honest difference: it stays unknown.
        total_fee = self._fee_set_of(result)
        fee_extra = total_fee.minus(booked_fee)
        if fee_extra.known and any(c.amount < -1e-12
                                   for c in fee_extra.components):
            fee_extra = venue_fees.UNKNOWN_FEES
        self._book({"filled_qty": extra,
                    "avg_price": result.get("avg_price", 0.0),
                    "order_link_id": f"{order_link_id}+cancel",
                    "fee_detail": fee_extra.to_dict()},
                   side=side, product=product)
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

    def _book(self, result, *, side: str, product: str,
              allow_missing_side: bool = False) -> None:
        if not result:
            return
        qty = float(result.get("filled_qty", 0.0) or 0.0)
        price = float(result.get("avg_price", 0.0) or 0.0)
        if qty <= 0 or price <= 0:
            return
        if not side and not allow_missing_side:
            raise LedgerError("a fill with no side cannot be booked")
        if not side:
            return
        fee_btc, fee_usdt, status, fee_raw = self._resolve_fee(
            result, side=side, product=product)
        ref = str(result.get("order_link_id") or f"fill:{self._ms()}")
        ms = int(self._ms())

        if product == "spot":
            if side == "Buy":
                # The venue takes a spot BUY fee in the COIN (F3). `fee` came
                # back in BTC; converting it here is what keeps the BTC
                # balance equal to what the wallet will show.
                spot_buy(self.journal, ms=ms, ref=ref, qty=qty, price=price,
                         fee_btc=fee_btc, fee_usdt=fee_usdt,
                         fee_status=status, fee_raw=fee_raw)
                filled = self._spot_qty + qty
                self._spot_entry = (
                    price if self._spot_entry is None else
                    (self._spot_entry * self._spot_qty + price * qty) / filled)
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
                          fee_status=status, fee_raw=fee_raw)
                self._spot_qty = max(0.0, self._spot_qty - qty)
                if self._spot_qty <= 0:
                    self._spot_entry = None
            return

        if side == "Sell":
            perp_open(self.journal, ms=ms, ref=ref, qty=qty, price=price,
                      fee_usd=fee_usdt, fee_status=status, fee_raw=fee_raw)
            filled = self._perp_qty + qty
            self._perp_entry = (
                price if self._perp_entry is None else
                (self._perp_entry * self._perp_qty + price * qty) / filled)
            self._perp_qty = filled
        else:
            entry = self._perp_entry if self._perp_entry is not None else price
            perp_close(self.journal, ms=ms, ref=ref, qty=qty, price=price,
                       entry_price=entry, fee_usd=fee_usdt,
                       fee_status=status, fee_raw=fee_raw)
            self._perp_qty = max(0.0, self._perp_qty - qty)
            if self._perp_qty <= 0:
                self._perp_entry = None

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

    def get_perp_position(self, symbol):
        return self.inner.get_perp_position(symbol)

    def get_funding_history(self, symbol, limit=8):
        return self.inner.get_funding_history(symbol, limit)

    def get_funding_payments(self, symbol, start_ms, end_ms=None):
        return self.inner.get_funding_payments(symbol, start_ms, end_ms)

    def get_liquidation_view(self, symbol):
        return self.inner.get_liquidation_view(symbol)

    def get_open_carry_orders(self, spot_symbol, perp_symbol):
        return self.inner.get_open_carry_orders(spot_symbol, perp_symbol)

    def get_book_top(self, symbol, product):
        return self.inner.get_book_top(symbol, product)

    def reconcile_pair(self, **kwargs):
        return self.inner.reconcile_pair(**kwargs)
