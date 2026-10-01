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
- no unit conversion happens here. This module answers "what did the
  venue say", not "what is that worth in the account's accounting unit" —
  that is a separate, explicit operation (see `tools/testnet_session.py`'s
  fee reconciliation), because converting silently is exactly how a BTC
  fee and a USDT fee end up added together as if they were the same
  number (the real BTCUSDT spot bug this module exists to make
  impossible to reintroduce).
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Mapping, Optional


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
