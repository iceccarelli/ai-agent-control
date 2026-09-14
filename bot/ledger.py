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
from typing import Any, Callable, Dict, List, Optional

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


@dataclass
class Entry:
    ms: int
    kind: str
    ref: str
    lines: List[Line]
    memo: str = ""


@dataclass
class Balance:
    usd: float = 0.0
    qty: float = 0.0


class Journal:
    """Append-only. Balanced. The only thing that knows what the book owns."""

    def __init__(self) -> None:
        self.entries: List[Entry] = []
        #: Fills whose fee the venue did not state. Counted, never booked as
        #: zero: an unknown cost that reads as $0.00 is a cost that has left
        #: the ledger. Phase D resolves each one against the venue.
        self.unknown_fees: int = 0

    # -- writing ----------------------------------------------------------

    def post(self, entry: Entry) -> Entry:
        """Validate, then append. A refusal writes nothing at all."""
        if not entry.lines:
            raise LedgerError(f"entry {entry.ref!r} has no lines")
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
        for entry in self.entries:
            if from_ms is not None and entry.ms < from_ms:
                continue
            if to_ms is not None and entry.ms > to_ms:
                continue
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
            "fees_unknown": self.unknown_fees,
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
                 "lines": [{"account": l.account, "usd": l.usd, "qty": l.qty}
                           for l in e.lines]} for e in self.entries]

    @classmethod
    def from_rows(cls, rows: List[Dict[str, Any]]) -> "Journal":
        """Rebuild, re-validating every entry. A journal is not trusted because
        it came out of a database: the balance rule is applied again on load,
        so a row edited in the file fails here instead of in a statement."""
        journal = cls()
        for row in rows:
            journal.post(Entry(
                ms=int(row["ms"]), kind=str(row["kind"]),
                ref=str(row["ref"]), memo=str(row.get("memo", "")),
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
             fee_btc: float) -> Entry:
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
        Line(FEES, fee_usd),
        Line(BTC, -fee_usd, qty=-fee_btc)]))


def spot_sell(journal: Journal, *, ms: int, ref: str, qty: float, price: float,
              cost_basis: float, fee_usd: float) -> Entry:
    proceeds = qty * price
    cost = qty * cost_basis
    return journal.post(Entry(ms=ms, kind="spot_sell", ref=ref, lines=[
        Line(USDT, proceeds, qty=proceeds),
        Line(BTC, -cost, qty=-qty),
        Line(REALISED, -(proceeds - cost)),
        Line(FEES, fee_usd),
        Line(USDT, -fee_usd, qty=-fee_usd)]))


def perp_open(journal: Journal, *, ms: int, ref: str, qty: float, price: float,
              fee_usd: float) -> Entry:
    """Open (or add to) the short. A derivative costs no principal, so the
    only money that moves is the fee; the position is carried as a quantity."""
    return journal.post(Entry(ms=ms, kind="perp_open", ref=ref, lines=[
        Line(PERP, 0.0, qty=-qty),
        Line(FEES, fee_usd),
        Line(USDT, -fee_usd, qty=-fee_usd)],
        memo=f"short {qty} @ {price}"))


def perp_close(journal: Journal, *, ms: int, ref: str, qty: float,
               price: float, entry_price: float, fee_usd: float) -> Entry:
    """Buy the short back. A short gains when the price falls."""
    realised = qty * (entry_price - price)
    return journal.post(Entry(ms=ms, kind="perp_close", ref=ref, lines=[
        Line(PERP, 0.0, qty=qty),
        Line(USDT, realised, qty=realised),
        Line(REALISED, -realised),
        Line(FEES, fee_usd),
        Line(USDT, -fee_usd, qty=-fee_usd)],
        memo=f"cover {qty} @ {price} from {entry_price}"))


def funding(journal: Journal, *, ms: int, ref: str, usd: float) -> Entry:
    """One settled print. Signed: a print that was PAID is booked (D10)."""
    return journal.post(Entry(ms=ms, kind="funding", ref=ref, lines=[
        Line(USDT, usd, qty=usd),
        Line(FUNDING, -usd)]))


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
        return result

    def cancel_order(self, *, symbol: str, order_link_id: str, product: str):
        # A cancel can report a fill it caught on the way out (0040). It is
        # booked like any other fill; a cancel that filled nothing books
        # nothing, because `_book` refuses a zero quantity.
        result = self.inner.cancel_order(symbol=symbol,
                                         order_link_id=order_link_id,
                                         product=product)
        self._book(result, side="", product=product, allow_missing_side=True)
        return result

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
        raw_fee = result.get("fee")
        if raw_fee is None:
            # UNKNOWN, not zero. Counted so the statement says how many costs
            # the venue never stated, and Phase D resolves each against it.
            self.journal.unknown_fees += 1
            fee = 0.0
        else:
            fee = abs(float(raw_fee))
        ref = str(result.get("order_link_id") or f"fill:{self._ms()}")
        ms = int(self._ms())

        if product == "spot":
            if side == "Buy":
                # The venue takes a spot BUY fee in the COIN (F3). `fee` came
                # back in BTC; converting it here is what keeps the BTC
                # balance equal to what the wallet will show.
                spot_buy(self.journal, ms=ms, ref=ref, qty=qty, price=price,
                         fee_btc=fee)
            else:
                spot_sell(self.journal, ms=ms, ref=ref, qty=qty, price=price,
                          cost_basis=price, fee_usd=fee)
            return

        if side == "Sell":
            perp_open(self.journal, ms=ms, ref=ref, qty=qty, price=price,
                      fee_usd=fee)
            filled = self._perp_qty + qty
            self._perp_entry = (
                price if self._perp_entry is None else
                (self._perp_entry * self._perp_qty + price * qty) / filled)
            self._perp_qty = filled
        else:
            entry = self._perp_entry if self._perp_entry is not None else price
            perp_close(self.journal, ms=ms, ref=ref, qty=qty, price=price,
                       entry_price=entry, fee_usd=fee)
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

    def get_liquidation_view(self, symbol):
        return self.inner.get_liquidation_view(symbol)

    def get_open_carry_orders(self, spot_symbol, perp_symbol):
        return self.inner.get_open_carry_orders(spot_symbol, perp_symbol)

    def get_book_top(self, symbol, product):
        return self.inner.get_book_top(symbol, product)

    def reconcile_pair(self, **kwargs):
        return self.inner.reconcile_pair(**kwargs)
