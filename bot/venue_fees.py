#!/usr/bin/env python3
"""THE one venue-fee extraction authority. Nothing else parses `cumExecFee`.

Before this module, `carry_broker.CarryBroker._fee` and
`trading_engine.venue_fee` were two separate implementations of the same
rule, free to drift. Both now call `extract_fee` here; neither re-derives
the parsing logic.

THE RULE
========
A Bybit v5 order/fill row — the raw dict `BybitClient.get_order`,
`_await_fill`, or a `place_order` response's `result` already returns —
carries `cumExecFee` (a string, sometimes absent) and, where the venue
says so, `feeCurrency`. Reading it correctly means three refusals:

- missing / empty / malformed / non-finite `cumExecFee` -> UNKNOWN, never
  0.0. A fee that vanished from the response is not a fee of nothing.
- a `feeCurrency` that is missing, empty, or not a plausible ticker ->
  unknown currency, never guessed from the traded symbol. `BTCUSDT`'s
  entry fee is conventionally charged in BTC and its exit fee in USDT
  (see `INVENTORY.md` D11/D44), but that is a convention about WHICH
  currency to expect, not permission to assert it when the venue did not
  say so in this specific row.
THE SECOND RULE — CONVERSION IS SEPARATE, AND ALSO HAS ONE AUTHORITY
=====================================================================
`extract_fee` answers "what did the venue say". `convert_to_account_unit`
below answers "what is that worth in the account's accounting unit" — a
distinct, explicit operation with its own authority, for the same reason
extraction has one: before this module, `tools/testnet_session.py`'s
ledger builder had its own private copy of this exact conversion logic,
and `trading_engine.close_position` had none at all — it booked
`TradeRecord.entry_fee`/`.exit_fee` as raw venue amounts, in whatever
currency each leg happened to be charged in. For the real BTCUSDT shape
(entry fee in BTC, exit fee in USDT — see `INVENTORY.md` D11/D44) that
meant `TradeRecord.total_fees`/`.net_pnl` — read by
`risk_management.py`'s daily-loss/drawdown/consecutive-loss gates by way
of `StateStore.record_trade`'s `daily_anchor` update — added a BTC number
to a USDT number and called the sum a fee. `close_position` now converts
before booking, using THIS function, so a `TradeRecord` it constructs is
never capable of holding two fee legs in different currencies —
`persistence.TradeRecord.total_fees` still checks this and refuses
instead of guessing, but a well-behaved caller should never trip it.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Mapping, Optional, Tuple


@dataclass(frozen=True)
class VenueFee:
    """One venue-reported fee fact. Never guess past what the row said."""

    amount: Optional[float] = None
    #: Upper-cased ticker (e.g. "BTC", "USDT"), or `None` when the venue's
    #: `feeCurrency` was missing, empty, or not a plausible ticker.
    currency: Optional[str] = None

    @property
    def known(self) -> bool:
        """True iff `amount` is a real, venue-reported number.

        A `known_zero` (the venue charged exactly 0) still reads `True`
        here — `known` means "we have a number", not "the number is
        positive". Distinguishing a known zero from an unknown fee is the
        entire point of this type existing instead of a bare `float`.
        """
        return self.amount is not None


#: The single unknown value. Never constructed with amount=0.0 as a
#: stand-in for "the venue didn't say" — see `VenueFee.known`.
UNKNOWN_FEE = VenueFee()

#: A `feeCurrency` string must look like this to be trusted. Guards
#: against a malformed/garbage value being carried forward as if it were
#: a real ticker (an empty string, whitespace, or something that is
#: clearly not a currency code).
_MAX_CURRENCY_LEN = 12


def _clean_currency(raw: Any) -> Optional[str]:
    if raw is None:
        return None
    text = str(raw).strip()
    if not text or len(text) > _MAX_CURRENCY_LEN:
        return None
    # A currency code is letters (and the rare digit, e.g. "1INCH" on some
    # venues) — never pass through something with spaces/punctuation that
    # could not plausibly be a venue-issued ticker.
    if not all(ch.isalnum() for ch in text):
        return None
    return text.upper()


def extract_fee(row: Optional[Mapping[str, Any]]) -> VenueFee:
    """The venue's own `cumExecFee` (+ `feeCurrency` where present) from
    one order/fill row. Returns `UNKNOWN_FEE` rather than raising or
    guessing, for every malformed shape this has ever been seen to take.
    """
    if not row:
        return UNKNOWN_FEE
    raw = row.get("cumExecFee")
    if raw is None or str(raw).strip() == "":
        return UNKNOWN_FEE
    try:
        amount = float(raw)
    except (TypeError, ValueError):
        return UNKNOWN_FEE
    if not math.isfinite(amount):
        return UNKNOWN_FEE
    currency = _clean_currency(row.get("feeCurrency"))
    return VenueFee(amount=amount, currency=currency)


# ---------------------------------------------------------------------------
# conversion — separate operation, same "never guess" discipline
# ---------------------------------------------------------------------------


def split_symbol_units(symbol: str) -> Tuple[str, str]:
    """`(base, quote)` for a spot pair symbol, by suffix — "BTCUSDT" ->
    ("BTC", "USDT"). ONLY used to know what a KNOWN currency converts
    against (e.g. "the venue said BTC; this symbol's quote is USDT, so
    multiply by price"); never used to invent a currency `extract_fee`
    did not report.
    """
    for quote in ("USDT", "USDC", "USD", "BTC", "ETH"):
        if symbol.endswith(quote) and len(symbol) > len(quote):
            return symbol[: -len(quote)], quote
    return symbol, ""


def estimate_leg_fee(*, price: float, qty: float, taker_fee: float) -> float:
    """A STATIC fallback, used only for a leg whose real fee AMOUNT is
    unknown. Already in the accounting unit by construction (a rate
    against notional, which is quoted in the accounting unit) — unlike a
    real venue fee, which can come back in the base coin."""
    return abs(price * qty * taker_fee)


@dataclass(frozen=True)
class FeeConversion:
    """What happened when one leg's raw venue fee was turned into the
    accounting unit — enough to audit or reproduce the conversion, never
    silent."""

    #: "identity" (already in the accounting unit), "converted" (base-coin
    #: amount multiplied by this leg's own trade price), "unresolved"
    #: (amount known but its currency could not be converted — the raw
    #: fact is preserved elsewhere, never guessed into a number here),
    #: "unknown" (no amount at all; `account_unit_amount` is a labelled
    #: STATIC ESTIMATE, never realized), or "zero_qty" (nothing to
    #: convert).
    status: str
    account_unit_amount: Optional[float]
    rate: Optional[float] = None
    basis: str = ""

    @property
    def known(self) -> bool:
        """True only when `account_unit_amount` reflects a real,
        venue-confirmed, successfully-converted figure — never true for
        an estimate or an unresolved leg."""
        return self.status in ("identity", "converted")


def convert_to_account_unit(
    *, amount: Optional[float], currency: Optional[str], known: bool,
    leg_price: float, qty: float, taker_fee: float, accounting_unit: str,
    base_coin: str,
) -> FeeConversion:
    """One leg's raw venue fee -> the accounting unit, or an explicit
    refusal to guess. NEVER sums a BTC amount and a USDT amount as though
    they were the same number: an entry fee charged in BTC and an exit
    fee charged in USDT are not numerically additive until this
    conversion happens, and it only happens when the currency is
    actually known and matches either the accounting unit (identity) or
    the pair's base coin (priced conversion — same basis `ledger.py`'s
    `spot_buy(fee_btc, price)` already uses for the carry book).
    """
    if qty <= 0:
        return FeeConversion(status="zero_qty", account_unit_amount=0.0)
    if not known or amount is None:
        estimate = estimate_leg_fee(price=leg_price, qty=qty, taker_fee=taker_fee)
        return FeeConversion(
            status="unknown", account_unit_amount=estimate,
            basis=f"static taker_fee={taker_fee} estimate (no real fee known)")
    if currency is None:
        # A real, known amount — but with no currency to convert it by.
        # Guessing here is exactly the defect this module exists to
        # refuse: preserve the raw amount elsewhere, claim nothing
        # converted here.
        return FeeConversion(
            status="unresolved", account_unit_amount=None,
            basis="amount known but currency unknown; conversion refused")
    if currency == accounting_unit:
        return FeeConversion(
            status="identity", account_unit_amount=amount, rate=1.0,
            basis=f"already {accounting_unit}")
    if currency == base_coin and leg_price > 0:
        return FeeConversion(
            status="converted", account_unit_amount=amount * leg_price,
            rate=leg_price,
            basis=f"{currency}->{accounting_unit} at this leg's own trade "
                 f"price {leg_price}")
    return FeeConversion(
        status="unresolved", account_unit_amount=None,
        basis=f"no conversion basis for currency={currency!r} "
             f"(expected {accounting_unit!r} or {base_coin!r})")
