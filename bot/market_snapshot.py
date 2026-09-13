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


class StaleMarket(RuntimeError):
    """The view is too old, internally inconsistent, or unreadable."""


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
    margin_multiple: float
    observed_at_s: float
    venue_time_s: Optional[float] = None
    detail: Dict[str, Any] = field(default_factory=dict)
    #: The last SETTLED print and its settlement stamp (0034). `funding_bps`
    #: above is the ticker's forecast for the next settlement; these are what
    #: was actually paid. take_snapshot always fills them; a snapshot built
    #: by hand without them simply carries no print.
    funding_print_bps: Optional[float] = None
    funding_print_ms: Optional[int] = None

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

    def assert_fresh(self, *, max_age_s: float = MAX_SNAPSHOT_AGE_S,
                     max_skew_s: float = MAX_CLOCK_SKEW_S) -> None:
        """Raise unless this view is safe to act on.

        Raises rather than returning a flag. A flag is a thing a caller can
        forget to read, and the whole point of this check is that the caller
        must not be able to trade past it.
        """
        for name in ("perp_mark", "spot_mark", "margin_multiple"):
            value = getattr(self, name)
            if not (isinstance(value, (int, float)) and math.isfinite(value)
                    and value > 0):
                raise StaleMarket(f"{name} is {value!r}")
        if not math.isfinite(self.funding_bps):
            raise StaleMarket(f"funding_bps is {self.funding_bps!r}")

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
                "clock_skew_s": self.clock_skew_s}


def take_snapshot(broker: Any, *, perp_symbol: str, spot_symbol: str,
                  now_s: Optional[float] = None) -> MarketSnapshot:
    """Read the venue once. Any failure propagates.

    Deliberately NOT wrapped in try/except. A snapshot that silently omits a
    field it could not read would be a partial view wearing a complete one's
    clothes, and the caller cannot tell the difference. The exception is the
    information.
    """
    observed = time.time() if now_s is None else float(now_s)
    perp = float(broker.get_mark(perp_symbol))
    spot = float(broker.get_spot_mark(spot_symbol))
    funding = float(broker.get_funding_bps(perp_symbol))
    # Called directly, never through getattr: a broker that cannot say what was
    # actually settled cannot snapshot (0034).
    print_bps, print_ms = broker.get_funding_print(perp_symbol)
    margin = float(broker.get_margin_multiple(perp_symbol))

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
                          detail={"perp_symbol": perp_symbol,
                                  "spot_symbol": spot_symbol,
                                  "clock_checked": venue_time is not None})
