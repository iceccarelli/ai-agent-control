"""One consistent view of the venue, or a refusal. Nothing in between.

WHY THIS EXISTS
===============
`main.tick` currently makes three independent venue calls — perp mark, funding,
spot mark — and hands the results to the engine as if they described the same
instant. They do not. Between the first call and the third the venue can move,
a rate can print, or one endpoint can serve a cached response while the others
are live.

That matters here more than in a directional book, because the basis is a
DIFFERENCE between two of those numbers. A perp mark from 09:00:00 against a
spot mark from 09:00:40 does not measure the basis, it measures the basis plus
forty seconds of price drift. On a term the 0018 backtest measured at -$4,772
against $42,843 of gross, forty seconds of drift is not a rounding error.

Worse, none of the three calls carries a timestamp today. A venue that freezes
and serves the same tick forever looks identical to a quiet market. The book
would keep trading on a price from an hour ago and never know.

WHAT THIS DOES
==============
Fetches all three in one place, stamps them with a single observation time,
carries the venue's own timestamps where it publishes them, and exposes ONE
question: is this view fresh enough to act on?

`assert_fresh()` raises. It does not return a flag, because a flag can be
ignored and this one must not be: `CarryEngine` turns the raise into a refusal
to open, the same way it treats an unreadable margin.

WHAT IT DOES NOT DO
===================
Decide anything. It has no thresholds about funding, no view on the basis, and
no ability to place an order. It is a value object with a clock.
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

#: A view older than this is not a view of the market, it is a memory of one.
#: 90s is three times a typical poll interval: long enough that a slow venue
#: does not trip it, short enough that a frozen feed is caught within two
#: cycles rather than after a day of trading on a stale tick.
MAX_SNAPSHOT_AGE_S = 90.0

#: The venue publishes its own server time on the ticker. If that is further
#: from ours than this, one of the two clocks is wrong, and a book that cannot
#: agree with its venue about what time it is cannot reason about funding
#: windows. Bybit's own recv_window default is 5s; this is deliberately looser
#: because it gates a refusal, not a signature.
MAX_CLOCK_SKEW_S = 30.0


#: One funding interval on Bybit BTCUSDT linear (instrument `fundingInterval`
#: = 480 minutes, research/exchange_study/bybit/instrument_linear.json.gz). A
#: VENUE FACT, not a tuned threshold. If the last settled print is older than
#: one interval plus MAX_SNAPSHOT_AGE_S, a settlement has been missed and the
#: engine's funding history describes a market the venue has moved past.
FUNDING_INTERVAL_MS = 8 * 3600 * 1000

#: The legs of one snapshot are read one after another. If the first and the
#: last of them are further apart than this, they do not describe one instant
#: and the basis between them carries that much price drift (the module
#: docstring's forty-second example). 10s is twice Bybit's own recv_window
#: default (5s): a single slow but healthy call does not trip it, a feed that
#: stalls mid-read does. Measured with a monotonic clock, so a wall-clock step
#: during the read cannot hide or invent a gap.
MAX_LEG_SPAN_S = 10.0


def _finite_number(value: Any, *, minimum: float, strict: bool = False) -> bool:
    """A real, finite number (not a bool) at or above `minimum` (above it when
    `strict`). NaN, inf, text and None are all False."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    if not math.isfinite(value):
        return False
    return value > minimum if strict else value >= minimum


class StaleMarket(RuntimeError):
    """The view is too old, internally inconsistent, or unreadable."""


_QUOTE_SUFFIXES = ("USDT", "USDC", "PERP", "USD")


def split_symbol(symbol: str) -> tuple:
    """``(multiplier, base)`` of a Bybit symbol such as ``1000PEPEUSDT``.

    ``multiplier`` is the leading contract multiplier (``1000``) or ``""``.
    Raises ValueError for a symbol with no recognisable quote suffix.
    """
    text = str(symbol).strip().upper().replace("-", "").replace("_", "")
    for suffix in _QUOTE_SUFFIXES:
        if text.endswith(suffix) and len(text) > len(suffix):
            text = text[: -len(suffix)]
            break
    else:
        raise ValueError(f"symbol {symbol!r} has no recognised quote suffix")
    digits = len(text) - len(text.lstrip("0123456789"))
    return text[:digits], text[digits:]


def assert_symbols_map(perp_symbol: str, spot_symbol: str) -> None:
    """The perp and spot legs must be the SAME asset at the SAME unit.

    ``1000PEPEUSDT`` against ``PEPEUSDT`` differs by a factor of 1000 in price
    and ``BTCUSDT`` against ``ETHUSDT`` is not a basis at all: either would
    produce a plausible-looking, enormous "basis". Refused, never mapped.
    """
    try:
        p_mult, p_base = split_symbol(perp_symbol)
        s_mult, s_base = split_symbol(spot_symbol)
    except ValueError as exc:
        raise StaleMarket(str(exc)) from exc
    if p_base != s_base or p_mult != s_mult or not p_base:
        raise StaleMarket(
            f"perp {perp_symbol!r} and spot {spot_symbol!r} are not the same "
            "asset at the same contract unit; the basis between them is meaningless")


@dataclass(frozen=True)
class MarketSnapshot:
    """Perp, spot, funding and margin as of ONE observation.

    All four are read together. A snapshot missing any of them is not a partial
    snapshot, it is not a snapshot: the fields are only useful in relation to
    each other, and the basis in particular is meaningless if one leg came from
    a different instant.
    """

    perp_mark: float
    spot_mark: float
    funding_bps: float
    #: `None` while flat - positionIM/positionMM is a ratio on an OPEN
    #: position (INVENTORY F4) and is genuinely undefined before the first
    #: fill, not unreadable. `take_snapshot` checks position size before
    #: deciding whether to read it at all, the same way
    #: `CarryEngine._margin_multiple` already tolerates its absence pre-open.
    margin_multiple: Optional[float]
    observed_at_s: float
    venue_time_s: Optional[float] = None
    detail: Dict[str, Any] = field(default_factory=dict)
    #: The last SETTLED print and its settlement stamp (0034). `funding_bps`
    #: above is the ticker's forecast for the next settlement; these are what
    #: was actually paid. take_snapshot always fills them; a snapshot built
    #: by hand without them simply carries no print.
    funding_print_bps: Optional[float] = None
    funding_print_ms: Optional[int] = None
    #: The venue's perp position size in this same observation (the read that
    #: decides whether margin exists). `None` on a hand-built snapshot.
    perp_held: Optional[float] = None
    #: Seconds between the start of the first leg read and the end of the last
    #: (monotonic clock). `None` on a hand-built snapshot: not measured.
    leg_span_s: Optional[float] = None

    @property
    def basis_bps(self) -> float:
        return (self.perp_mark / self.spot_mark - 1.0) * 1e4

    @property
    def age_s(self) -> float:
        return max(0.0, time.time() - self.observed_at_s)

    @property
    def clock_skew_s(self) -> Optional[float]:
        if self.venue_time_s is None:
            return None
        return abs(self.venue_time_s - self.observed_at_s)

    def assert_legs_coherent(self, max_leg_span_s: float = MAX_LEG_SPAN_S) -> None:
        """The legs of this snapshot were read close enough together to describe
        one instant. A hand-built snapshot has no span and passes.

        This is a limit on the QUALITY OF AN ENTRY DECISION (the basis between
        legs read far apart is drift). `assert_fresh` does not apply it unless
        asked, so a slow feed cannot suspend the management of a held pair; only
        the entry gate (`CarryRisk.gate_open`) asks."""
        if self.leg_span_s is None:
            return
        if not _finite_number(self.leg_span_s, minimum=0.0):
            raise StaleMarket(f"leg_span_s is {self.leg_span_s!r}")
        if self.leg_span_s > max_leg_span_s:
            raise StaleMarket(
                f"the snapshot's legs were read {self.leg_span_s:.1f}s apart "
                f"(limit {max_leg_span_s:.0f}s); they do not describe one "
                "instant and the basis between them is drift")

    def assert_fresh(self, *, max_age_s: float = MAX_SNAPSHOT_AGE_S,
                     max_skew_s: float = MAX_CLOCK_SKEW_S,
                     max_leg_span_s: Optional[float] = None) -> None:
        """Raise unless this view is safe to act on.

        Raises rather than returning a flag. A flag is a thing a caller can
        forget to read, and the whole point of this check is that the caller
        must not be able to trade past it.
        """
        for name in ("perp_mark", "spot_mark"):
            value = getattr(self, name)
            if not (isinstance(value, (int, float)) and math.isfinite(value)
                    and value > 0):
                raise StaleMarket(f"{name} is {value!r}")
        # `None` means flat - nothing to check yet (see the field's own
        # docstring). A position DOES exist the moment this is anything else,
        # and then it must be a usable positive ratio like every other field.
        if self.margin_multiple is not None:
            if not (isinstance(self.margin_multiple, (int, float))
                    and math.isfinite(self.margin_multiple)
                    and self.margin_multiple > 0):
                raise StaleMarket(f"margin_multiple is {self.margin_multiple!r}")
        # A bool or a string is not a rate (and a string made isfinite raise
        # TypeError, which is not the refusal callers catch).
        if not _finite_number(self.funding_bps, minimum=-math.inf):
            raise StaleMarket(f"funding_bps is {self.funding_bps!r}")
        # The observation time is what every age below is measured from. NaN
        # made `age_s` read 0.0 (max(0, nan) is 0) and +inf made it negative,
        # clamped to 0: both passed as perfectly fresh.
        if not _finite_number(self.observed_at_s, minimum=0.0):
            raise StaleMarket(f"observed_at_s is {self.observed_at_s!r}")
        # "Is anything open" decided margin: a NaN size compares False with
        # every threshold and used to read as flat. Size is a finite, unsigned
        # quantity or the view is unreadable.
        if self.perp_held is not None and not _finite_number(
                self.perp_held, minimum=0.0):
            raise StaleMarket(f"perp_held is {self.perp_held!r}")
        # A venue clock that is present but not a time is not "unchecked": NaN
        # makes the skew comparison False and the gate would pass silently.
        # (An ABSENT clock is the recorded, deliberate "not checked".)
        if self.venue_time_s is not None and not _finite_number(
                self.venue_time_s, minimum=0.0, strict=True):
            raise StaleMarket(f"venue_time_s is {self.venue_time_s!r}")

        # Signed, not the clamped `age_s`: an observation stamped in the future
        # used to read as age 0, i.e. as fresh as possible, for as long as the
        # stamp stayed ahead. It is a clock disagreement and is refused on the
        # same tolerance as the venue clock.
        raw_age = time.time() - self.observed_at_s
        if raw_age < -max_skew_s:
            raise StaleMarket(
                f"snapshot is stamped {-raw_age:.1f}s in the future (limit "
                f"{max_skew_s:.0f}s); this host's clock and the observation's "
                "disagree, so its age cannot be known")
        if self.leg_span_s is not None \
                and not _finite_number(self.leg_span_s, minimum=0.0):
            # A corrupt span is a data defect on EVERY path; only the "too wide"
            # comparison is reserved for entries.
            raise StaleMarket(f"leg_span_s is {self.leg_span_s!r}")
        if max_leg_span_s is not None:
            self.assert_legs_coherent(max_leg_span_s)

        age = self.age_s
        if age > max_age_s:
            raise StaleMarket(
                f"snapshot is {age:.1f}s old (limit {max_age_s:.0f}s); a frozen "
                "feed looks exactly like a quiet market")

        if self.funding_print_ms is not None:
            if not (isinstance(self.funding_print_bps, (int, float))
                    and math.isfinite(self.funding_print_bps)):
                raise StaleMarket(
                    f"funding_print_bps is {self.funding_print_bps!r}")
            now_ms = self.observed_at_s * 1000.0
            behind = now_ms - float(self.funding_print_ms)
            if behind > FUNDING_INTERVAL_MS + max_age_s * 1000.0:
                raise StaleMarket(
                    f"the last settled funding print is {behind / 3.6e6:.1f}h "
                    "old — at least one settlement has been missed, and the "
                    "funding history the book decides on is out of date")
            if behind < -max_skew_s * 1000.0:
                raise StaleMarket(
                    f"a funding print is stamped {-behind / 1000.0:.0f}s in "
                    "the future; the venue and this host disagree about time")

        skew = self.clock_skew_s
        if skew is not None and skew > max_skew_s:
            raise StaleMarket(
                f"venue clock differs by {skew:.1f}s (limit {max_skew_s:.0f}s); "
                "a book that cannot agree with its venue about the time cannot "
                "reason about funding windows")

    def as_dict(self) -> Dict[str, Any]:
        return {"perp_mark": self.perp_mark, "spot_mark": self.spot_mark,
                "funding_bps": self.funding_bps,
                "funding_print_bps": self.funding_print_bps,
                "funding_print_ms": self.funding_print_ms,
                "basis_bps": self.basis_bps,
                "margin_multiple": self.margin_multiple, "age_s": self.age_s,
                "clock_skew_s": self.clock_skew_s,
                "leg_span_s": self.leg_span_s}


def take_snapshot(broker: Any, *, perp_symbol: str, spot_symbol: str,
                  now_s: Optional[float] = None) -> MarketSnapshot:
    """Read the venue once. Any failure propagates.

    Deliberately NOT wrapped in try/except. A snapshot that silently omits a
    field it could not read would be a partial view wearing a complete one's
    clothes, and the caller cannot tell the difference. The exception is the
    information.
    """
    assert_symbols_map(perp_symbol, spot_symbol)
    observed = time.time() if now_s is None else float(now_s)
    started = time.monotonic()
    perp = float(broker.get_mark(perp_symbol))
    spot = float(broker.get_spot_mark(spot_symbol))
    funding = float(broker.get_funding_bps(perp_symbol))
    # Called directly, never through getattr: a broker that cannot say what was
    # actually settled cannot snapshot (0034).
    print_bps, print_ms = broker.get_funding_print(perp_symbol)
    # Margin headroom is a ratio on an OPEN position (INVENTORY F4) - checking
    # size first (the same read get_perp_position always is, the single
    # source of truth for "is anything open") is what tells "nothing to
    # measure yet" apart from "the venue would not say". Only the second one
    # still raises: get_margin_multiple below is UNCHANGED and UNWRAPPED when
    # a position exists, so an unreadable margin on a live position still
    # propagates exactly as before.
    held = abs(float(broker.get_perp_position(perp_symbol)))
    if not math.isfinite(held):
        # Raised here as well as in assert_fresh: `held > 0` below would
        # otherwise read NaN as flat and skip the margin read on a live book.
        raise StaleMarket(f"perp position for {perp_symbol} is {held!r}")
    margin = float(broker.get_margin_multiple(perp_symbol)) if held > 0 else None
    leg_span = max(0.0, time.monotonic() - started)

    venue_time = None
    getter = getattr(broker, "get_venue_time_s", None)
    if callable(getter):
        try:
            venue_time = float(getter())
        except Exception:  # noqa: BLE001
            # A venue that will not tell us the time is not a reason to refuse
            # to trade — it is a reason not to CHECK the clock. The skew gate
            # simply does not apply, and that is recorded rather than assumed.
            venue_time = None

    return MarketSnapshot(perp_mark=perp, spot_mark=spot, funding_bps=funding,
                          margin_multiple=margin, observed_at_s=observed,
                          venue_time_s=venue_time,
                          funding_print_bps=float(print_bps),
                          funding_print_ms=int(print_ms),
                          perp_held=held, leg_span_s=leg_span,
                          detail={"perp_symbol": perp_symbol,
                                  "spot_symbol": spot_symbol,
                                  "clock_checked": venue_time is not None})
