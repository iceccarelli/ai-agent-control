"""Venue fee facts: extraction and unit conversion, each with one authority.

A fee is only evidence if three things survive: the AMOUNT the venue reported,
the CURRENCY it was charged in, and whether the venue reported it at all. A bare
float keeps none of the last two. This module exists so the trading engine never
books a fee without knowing which of these it holds.

EXTRACTION (`extract_fee`) answers "what did the venue say". It refuses:

- a missing, empty, malformed or non-finite ``cumExecFee`` -> UNKNOWN, never
  0.0. A fee that vanished from a response is not a fee of nothing;
- a missing or implausible ``feeCurrency`` -> unknown currency, never guessed
  from the symbol. A BTCUSDT spot buy is conventionally charged in BTC and the
  sell in USDT, but a convention says what to expect, not what this row says.

CONVERSION (`convert_to_account_unit`) answers "what is that worth in the
accounting unit". A BTC amount and a USDT amount are not additive until it
happens, and it happens only when the currency is known and is either the
accounting unit (identity) or the pair's base coin (multiplied by that leg's
OWN trade price, recorded as the basis). Anything else is ``unresolved`` and
claims nothing. An absent fee yields a labelled STATIC ESTIMATE whose status is
``unknown``: callers must never present it as realised.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Mapping, Optional, Tuple


@dataclass(frozen=True)
class VenueFee:
    """One venue-reported fee fact. Never guess past what the row said."""

    amount: Optional[float] = None
    #: Upper-cased ticker, or ``None`` when ``feeCurrency`` was absent/implausible.
    currency: Optional[str] = None

    @property
    def known(self) -> bool:
        """True iff ``amount`` is a real venue-reported number.

        A reported zero is ``known``: the venue said "nothing was charged".
        That is a different fact from "the venue did not say".
        """
        return self.amount is not None


UNKNOWN_FEE = VenueFee()

_MAX_CURRENCY_LEN = 12


def _clean_currency(raw: Any) -> Optional[str]:
    if raw is None:
        return None
    text = str(raw).strip()
    if not text or len(text) > _MAX_CURRENCY_LEN or not text.isalnum():
        return None
    return text.upper()


def extract_fee(row: Optional[Mapping[str, Any]]) -> VenueFee:
    """The venue's own ``cumExecFee`` (+ ``feeCurrency``) from one order row."""
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
    return VenueFee(amount=amount, currency=_clean_currency(row.get("feeCurrency")))


def split_symbol_units(symbol: str) -> Tuple[str, str]:
    """``(base, quote)`` by suffix: "BTCUSDT" -> ("BTC", "USDT").

    Used only to know what a KNOWN currency converts against; never to invent a
    currency the venue did not report.
    """
    for quote in ("USDT", "USDC", "USD", "BTC", "ETH"):
        if symbol.endswith(quote) and len(symbol) > len(quote):
            return symbol[: -len(quote)], quote
    return symbol, ""


def estimate_leg_fee(*, price: float, qty: float, taker_fee: float) -> float:
    """A static rate-times-notional ESTIMATE, in the quote unit by construction."""
    return abs(price * qty * taker_fee)


@dataclass(frozen=True)
class FeeConversion:
    """How one leg's fee became an accounting-unit number, or why it did not."""

    #: ``identity`` | ``converted`` (real, known) |
    #: ``unresolved`` (amount known, no conversion basis; no number claimed) |
    #: ``unknown`` (no amount; ``account_unit_amount`` is a static estimate) |
    #: ``zero_qty``.
    status: str
    account_unit_amount: Optional[float]
    rate: Optional[float] = None
    basis: str = ""

    @property
    def known(self) -> bool:
        """True only for a venue-confirmed, successfully converted figure."""
        return self.status in ("identity", "converted")


def convert_to_account_unit(
    *, amount: Optional[float], currency: Optional[str], known: bool,
    leg_price: float, qty: float, taker_fee: float, accounting_unit: str,
    base_coin: str,
) -> FeeConversion:
    """One leg's raw venue fee -> the accounting unit, or an explicit refusal."""
    if qty <= 0:
        return FeeConversion(status="zero_qty", account_unit_amount=0.0)
    if not known or amount is None:
        return FeeConversion(
            status="unknown",
            account_unit_amount=estimate_leg_fee(
                price=leg_price, qty=qty, taker_fee=taker_fee),
            basis=f"static taker_fee={taker_fee} estimate (no real fee known)")
    if currency is None:
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
