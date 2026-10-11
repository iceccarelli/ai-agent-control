"""Venue fee facts: extraction and unit conversion, each with one authority.

A fee is only evidence if four things survive: the AMOUNT the venue reported,
the CURRENCY it was charged in (an order can carry several), WHICH FIELD said
so, and whether the venue said anything at all. A bare float keeps none of the
last three. Nothing outside this module parses a fee field.

THE VENUE CONTRACT (Bybit v5, from the published docs, not from a live payload)
=============================================================================
- Order rows (REST order list / order history, private `order` stream), linear
  and spot: ``cumFeeDetail`` is a JSON map ``{coin: amount}`` and REPLACES
  ``cumExecFee`` (docs: "Deprecated. Use cumFeeDetail instead"). The docs'
  own linear example shows ``"cumExecFee": "0"`` beside a non-empty
  ``cumFeeDetail``, so the legacy field must NEVER be preferred when the
  detail map is present: it can read as a confident, wrong zero.
- Inverse/option rows still use ``cumExecFee``; spot rows used ``feeCurrency``
  (documented "Spot only", deprecated).
- Execution rows carry ``execFee`` + ``feeCurrency`` per fill, keyed by
  ``execId``.
- Closed-PnL rows carry ``openFee``/``closeFee`` but the docs state neither
  their currency nor whether ``closedPnl`` is net of them, so they are kept as
  RAW EVIDENCE only and never become a confirmed fee.

THE RULES
=========
1. ``cumFeeDetail`` present and a non-empty map -> authoritative, one component
   per coin. Any malformed entry makes the WHOLE set unknown.
2. ``cumFeeDetail`` present but empty -> unknown. An empty map cannot be told
   apart from "not reported", and an unreported fee is not a fee of nothing.
3. ``cumFeeDetail`` absent -> the legacy ``cumExecFee`` (+ ``feeCurrency``).
4. A missing/garbage/non-finite amount is UNKNOWN, never 0.0. A reported zero is
   KNOWN: zero needs no currency to be zero.
5. A currency is never guessed from the symbol.
6. Conversion to an accounting unit is a separate act with its own basis: the
   unit itself (identity), or the pair's base coin at that leg's own trade
   price. Any other coin (MNT, a rebate token, ...) is ``unresolved`` and claims
   no number. A set is CONFIRMED only if every component converts.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Tuple


@dataclass(frozen=True)
class VenueFee:
    """One component of a venue fee: an amount in one currency."""

    amount: Optional[float] = None
    #: Upper-cased ticker, or ``None`` when the venue did not name the coin.
    currency: Optional[str] = None

    @property
    def known(self) -> bool:
        return self.amount is not None


_MAX_CURRENCY_LEN = 12


def _clean_currency(raw: Any) -> Optional[str]:
    if raw is None:
        return None
    text = str(raw).strip()
    if not text or len(text) > _MAX_CURRENCY_LEN or not text.isalnum():
        return None
    return text.upper()


def _amount(raw: Any) -> Optional[float]:
    if raw is None or isinstance(raw, bool) or str(raw).strip() == "":
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


@dataclass(frozen=True)
class FeeSet:
    """Everything the venue said about the fee on one order (or one slice of it).

    ``known`` False means the venue did not state it; ``components`` is then
    empty. A known set may hold several currencies and may hold zero amounts.
    """

    components: Tuple[VenueFee, ...] = ()
    known: bool = False
    #: Which field supplied it: ``cumFeeDetail`` | ``cumExecFee`` | ``""``.
    source: str = ""
    #: Why a set is unknown, or an evidence reference; informational only.
    note: str = ""

    # -- arithmetic that never invents a number ----------------------------

    def scaled(self, fraction: float) -> "FeeSet":
        if not self.known:
            return self
        return FeeSet(
            tuple(VenueFee(c.amount * fraction, c.currency)
                  for c in self.components), True, self.source, self.note)

    def minus(self, other: "FeeSet") -> "FeeSet":
        """Per-currency difference ``self - other``; unknown if either is."""
        if not (self.known and other.known):
            return UNKNOWN_FEES
        have: Dict[Optional[str], float] = {}
        for c in self.components:
            have[c.currency] = have.get(c.currency, 0.0) + c.amount
        for c in other.components:
            have[c.currency] = have.get(c.currency, 0.0) - c.amount
        return FeeSet(tuple(VenueFee(a, k) for k, a in have.items()),
                      True, self.source, self.note)

    def plus(self, other: "FeeSet") -> "FeeSet":
        """Per-currency sum; unknown if either part is (a half is not a total)."""
        if not (self.known and other.known):
            return UNKNOWN_FEES
        have: Dict[Optional[str], float] = {}
        for c in self.components + other.components:
            have[c.currency] = have.get(c.currency, 0.0) + c.amount
        return FeeSet(tuple(VenueFee(a, k) for k, a in have.items()),
                      True, self.source or other.source, self.note)

    @property
    def single_amount(self) -> Optional[float]:
        """The bare amount iff it is one currency (legacy float consumers)."""
        if not self.known:
            return None
        if len({c.currency for c in self.components}) > 1:
            return None
        return sum(c.amount for c in self.components)

    # -- (de)serialisation: plain JSON, for position meta and journal rows ---

    def to_dict(self) -> Dict[str, Any]:
        return {"known": self.known, "source": self.source, "note": self.note,
                "components": [{"amount": c.amount, "currency": c.currency}
                               for c in self.components]}

    @classmethod
    def from_dict(cls, raw: Any) -> "FeeSet":
        if not isinstance(raw, Mapping) or not raw.get("known"):
            return UNKNOWN_FEES
        comps: List[VenueFee] = []
        for item in raw.get("components") or []:
            amount = _amount((item or {}).get("amount"))
            if amount is None:
                return UNKNOWN_FEES
            comps.append(VenueFee(amount, _clean_currency(item.get("currency"))))
        return cls(tuple(comps), True, str(raw.get("source") or ""),
                   str(raw.get("note") or ""))


UNKNOWN_FEES = FeeSet()


def _unknown(note: str) -> FeeSet:
    return FeeSet(note=note)


def extract_order_fees(row: Optional[Mapping[str, Any]]) -> FeeSet:
    """The fee on one order row, by the rules in the module docstring."""
    if not row:
        return _unknown("no row")
    if "cumFeeDetail" in row:
        detail = row.get("cumFeeDetail")
        if isinstance(detail, str):
            try:
                detail = json.loads(detail) if detail.strip() else {}
            except ValueError:
                return _unknown("cumFeeDetail is not valid JSON")
        if not isinstance(detail, Mapping):
            return _unknown("cumFeeDetail is not a map")
        if not detail:
            return _unknown("cumFeeDetail present but empty")
        comps: List[VenueFee] = []
        for coin, raw in detail.items():
            currency = _clean_currency(coin)
            amount = _amount(raw)
            if currency is None or amount is None:
                return _unknown(f"malformed cumFeeDetail entry {coin!r}")
            comps.append(VenueFee(amount, currency))
        return FeeSet(tuple(comps), True, "cumFeeDetail")
    amount = _amount(row.get("cumExecFee"))
    if amount is None:
        return _unknown("no cumFeeDetail and no usable cumExecFee")
    return FeeSet((VenueFee(amount, _clean_currency(row.get("feeCurrency"))),),
                  True, "cumExecFee")


# ---------------------------------------------------------------------------
# conversion — a separate act, with its own basis, and the same refusal to guess
# ---------------------------------------------------------------------------


def split_symbol_units(symbol: str) -> Tuple[str, str]:
    """``(base, quote)`` by suffix: "BTCUSDT" -> ("BTC", "USDT")."""
    for quote in ("USDT", "USDC", "USD", "BTC", "ETH"):
        if symbol.endswith(quote) and len(symbol) > len(quote):
            return symbol[: -len(quote)], quote
    return symbol, ""


def estimate_leg_fee(*, price: float, qty: float, taker_fee: float) -> float:
    """A static rate-times-notional ESTIMATE, in the quote unit by construction."""
    return abs(price * qty * taker_fee)


@dataclass(frozen=True)
class FeeConversion:
    """What happened to ONE component."""

    #: ``identity`` | ``converted`` | ``unresolved``
    status: str
    account_unit_amount: Optional[float]
    rate: Optional[float] = None
    basis: str = ""
    venue_amount: Optional[float] = None
    venue_currency: Optional[str] = None

    @property
    def known(self) -> bool:
        return self.status in ("identity", "converted")

    def as_dict(self) -> Dict[str, Any]:
        return {"venue_amount": self.venue_amount,
                "venue_currency": self.venue_currency, "status": self.status,
                "basis": self.basis, "rate": self.rate,
                "account_unit_amount": self.account_unit_amount}


@dataclass(frozen=True)
class FeeSetConversion:
    """A whole fee set, converted (or not) into the accounting unit."""

    #: ``confirmed`` (every component converted) | ``unresolved`` (a component
    #: could not be) | ``unknown`` (the venue did not state a fee) |
    #: ``zero_qty``.
    status: str
    #: Present ONLY when ``confirmed``. Never an estimate.
    account_unit_amount: Optional[float]
    parts: Tuple[FeeConversion, ...] = ()
    source: str = ""
    note: str = ""

    @property
    def confirmed(self) -> bool:
        return self.status == "confirmed"

    def as_dict(self) -> Dict[str, Any]:
        return {"status": self.status, "source": self.source, "note": self.note,
                "account_unit_amount": self.account_unit_amount,
                "parts": [p.as_dict() for p in self.parts]}


def convert_fee(
    *, fee: VenueFee, leg_price: float, accounting_unit: str, base_coin: str,
) -> FeeConversion:
    """One component -> the accounting unit, or an explicit refusal."""
    amount, currency = fee.amount, fee.currency
    if amount is None:
        return FeeConversion("unresolved", None, basis="no amount")
    if amount == 0.0:
        # Zero needs no currency and no price to be zero.
        return FeeConversion("identity", 0.0, rate=1.0, basis="reported zero",
                             venue_amount=amount, venue_currency=currency)
    if currency is None:
        return FeeConversion(
            "unresolved", None, venue_amount=amount,
            basis="amount known but currency unnamed; conversion refused")
    if currency == accounting_unit:
        return FeeConversion(
            "identity", amount, rate=1.0, basis=f"already {accounting_unit}",
            venue_amount=amount, venue_currency=currency)
    if currency == base_coin and leg_price > 0:
        return FeeConversion(
            "converted", amount * leg_price, rate=leg_price,
            basis=f"{currency}->{accounting_unit} at this leg's own trade "
                  f"price {leg_price}",
            venue_amount=amount, venue_currency=currency)
    return FeeConversion(
        "unresolved", None, venue_amount=amount, venue_currency=currency,
        basis=f"no conversion basis for {currency!r} "
              f"(expected {accounting_unit!r} or {base_coin!r})")


def convert_fee_set(
    fees: FeeSet, *, leg_price: float, qty: float, accounting_unit: str,
    base_coin: str,
) -> FeeSetConversion:
    """A fee set -> the accounting unit. Confirmed only if every part converts."""
    if qty <= 0:
        return FeeSetConversion("zero_qty", 0.0, source=fees.source)
    if not fees.known:
        return FeeSetConversion("unknown", None, source=fees.source,
                                note=fees.note)
    parts = tuple(convert_fee(fee=c, leg_price=leg_price,
                              accounting_unit=accounting_unit,
                              base_coin=base_coin) for c in fees.components)
    if all(p.known for p in parts):
        return FeeSetConversion(
            "confirmed", sum(p.account_unit_amount for p in parts), parts,
            fees.source, fees.note)
    return FeeSetConversion("unresolved", None, parts, fees.source, fees.note)
