"""Serves the engine's public market reads from the validated live feed.

`market_snapshot.take_snapshot(broker, ...)` is the one place the carry engine
learns the market. It calls a small set of broker methods. This adapter answers
the PUBLIC ones from a :class:`~md_state.MarketView` and forwards the PRIVATE
ones (the held position and its margin, which only the signed account endpoints
can state) to the real broker. Nothing about `take_snapshot`, `assert_fresh`,
`CarryRisk` or the engine changes: they get a snapshot built from the feed, or
an exception.

Rules this adapter enforces, each tested:

* **One view per snapshot.** `get_mark` is the first call of a snapshot; it pins
  one view and every later call of that snapshot reads the SAME view, so the
  perp mark, spot reference, funding and touch come from one instant and the
  floats the engine later compares for equality are identical.
* **Refuse, never invent.** If the view is unusable for MONITORING (a book is
  stale, a connection is down, the ticker is missing) every public read raises
  `MarketUnusable`. Entry-only shortfalls (no instrument rules yet, no REST
  cross-check, no settled funding print, clock not yet measured) do not stop the
  management of a held pair; they are carried in `snapshot_detail()` as
  ``entry_refusals`` and `CarryRisk.gate_open` refuses new exposure on them.
* **Same environment.** A testnet engine must not trade on mainnet prices or the
  reverse; a feed of the other environment is unusable.
* **Position truth stays on the signed account.** `get_perp_position` and
  `get_margin_multiple` are forwarded, never answered from a public stream.
"""
from __future__ import annotations

import math
from typing import Any, Callable, Dict, List, Optional

import md_state as S


class LiveFeedBroker:
    #: `market_snapshot.take_snapshot` merges `snapshot_detail()` only from a class
    #: that declares this, so no stub or mock can inject entry refusals by accident.
    provides_snapshot_detail = True

    def __init__(self, view_provider: Callable[[], S.MarketView], delegate: Any, *,
                 expected_environment: str, perp_symbol: str, spot_symbol: str) -> None:
        if expected_environment not in ("mainnet", "testnet"):
            raise ValueError(f"environment {expected_environment!r}")
        self._provider = view_provider
        self._delegate = delegate
        self.expected_environment = expected_environment
        self.perp_symbol, self.spot_symbol = perp_symbol, spot_symbol
        self._view: Optional[S.MarketView] = None

    # -- the pinned view -------------------------------------------------------------

    def _pin(self) -> S.MarketView:
        view = self._provider()
        self._view = view
        reasons: List[str] = []
        if view.environment != self.expected_environment:
            reasons.append(f"environment:{view.environment}_FEED_FOR_{self.expected_environment}_ENGINE")
        if view.symbol != self.perp_symbol:
            reasons.append(f"symbol:FEED_{view.symbol}_ENGINE_{self.perp_symbol}")
        reasons += view.refusals(S.MONITOR)
        if reasons:
            raise S.MarketUnusable(S.MONITOR, reasons)
        return view

    def _current(self) -> S.MarketView:
        return self._view if self._view is not None else self._pin()

    @staticmethod
    def _need(value: Optional[float], what: str) -> float:
        if value is None or not math.isfinite(value):
            raise S.MarketUnusable(S.MONITOR, [f"{what}:NOT_AVAILABLE"])
        return float(value)

    def _check_symbol(self, symbol: str, expect: str) -> None:
        if symbol != expect:
            raise ValueError(f"feed serves {expect}, was asked for {symbol!r}")

    # -- public reads (the take_snapshot surface) ------------------------------------------

    def get_mark(self, symbol: str) -> float:
        """Starts a snapshot: pins a fresh view."""
        self._check_symbol(symbol, self.perp_symbol)
        return self._need(self._pin().perp_mark, "perp_mark")

    def get_spot_mark(self, symbol: str) -> float:
        """The spot reference: the MID of the live spot touch (an observed bid and
        ask, not a last trade that may be minutes old). If the spot book is not
        usable, the perpetual's own INDEX price (an observed spot-market average
        stated on the perp ticker) stands in so a HELD pair stays managed; the
        fallback is named in `snapshot_detail()` and entries are refused while the
        spot book is down (the `spot_book` status is an entry requirement)."""
        self._check_symbol(symbol, self.spot_symbol)
        v = self._current()
        if v.spot_mid is not None:
            return self._need(v.spot_mid, "spot_mid")
        return self._need(v.perp_index, "spot_mid_or_index")

    def get_funding_bps(self, symbol: str) -> float:
        """The venue's forecast funding rate for the next settlement, in bps."""
        self._check_symbol(symbol, self.perp_symbol)
        return self._need(self._current().funding_rate, "funding_rate") * 1e4

    def get_funding_print(self, symbol: str):
        """The last SETTLED print (bps, settlement ms), from REST history. Raises
        until the REST bootstrap has delivered one: the engine decides on what
        was paid, and a forecast must not stand in for it."""
        self._check_symbol(symbol, self.perp_symbol)
        fp = self._current().last_funding_print
        if fp is None:
            raise S.MarketUnusable(S.ENTRY, ["funding_print:NO_SETTLED_PRINT"])
        return fp[0] * 1e4, int(fp[1])

    def get_venue_time_s(self) -> float:
        """The venue's own stamp on the freshest perp book frame (seconds)."""
        ts = self._current().perp_book_exch_ts_ms
        if ts is None:
            raise S.MarketUnusable(S.MONITOR, ["venue_time:NOT_AVAILABLE"])
        return ts / 1000.0

    def get_book_top(self, symbol: str, product: str, timeout_s: Optional[float] = None) -> Dict[str, float]:
        v = self._current()
        if product == "linear":
            self._check_symbol(symbol, self.perp_symbol)
            bid, ask = v.perp_bid, v.perp_ask
        elif product == "spot":
            self._check_symbol(symbol, self.spot_symbol)
            bid, ask = v.spot_bid, v.spot_ask
        else:
            raise ValueError(f"unknown product {product!r}")
        return {"bid": self._need(bid, f"{product}_bid"), "ask": self._need(ask, f"{product}_ask")}

    def get_book_depth(self, symbol: str, product: str, limit: Optional[int] = None,
                       timeout_s: Optional[float] = None) -> Dict[str, Any]:
        v = self._current()
        if product == "linear":
            self._check_symbol(symbol, self.perp_symbol)
            bids, asks, ts = v.perp_bids, v.perp_asks, v.perp_book_exch_ts_ms
        elif product == "spot":
            self._check_symbol(symbol, self.spot_symbol)
            bids, asks, ts = v.spot_bids, v.spot_asks, v.spot_book_exch_ts_ms
        else:
            raise ValueError(f"unknown product {product!r}")
        if not bids or not asks:
            raise S.MarketUnusable(S.MONITOR, [f"{product}_book:NOT_AVAILABLE"])
        n = int(limit) if limit else len(bids)
        return {"bid": bids[0][0], "ask": asks[0][0], "bids": tuple(bids[:n]), "asks": tuple(asks[:n]),
                "venue_ts_ms": ts}

    # -- private reads: the signed account is the truth ----------------------------------------

    def get_perp_position(self, symbol: str) -> float:
        return self._delegate.get_perp_position(symbol)

    def get_margin_multiple(self, symbol: str) -> float:
        return self._delegate.get_margin_multiple(symbol)

    # -- what the snapshot carries for the entry gate -------------------------------------------------

    def snapshot_detail(self) -> Dict[str, Any]:
        """Merged into `MarketSnapshot.detail` by `take_snapshot`. `entry_refusals`
        is what `CarryRisk.gate_open` refuses new exposure on."""
        v = self._current()
        refusals = list(v.refusals(S.ENTRY))
        if v.environment != self.expected_environment:
            refusals.append(f"environment:{v.environment}")
        return {
            "market_source": "live_feed",
            "spot_reference": "SPOT_BOOK_MID" if v.spot_mid is not None else "PERP_INDEX_FALLBACK",
            "environment": v.environment,
            "entry_refusals": refusals,
            "feed_clock_skew_ms": v.clock_skew_ms,
            "feed_leg_span_s": v.leg_span_s,
            "feed_perp_book_age_s": v.perp_book_age_s,
            "feed_spot_book_age_s": v.spot_book_age_s,
            "feed_observed_wall_ms": v.observed_wall_ms,
            "feed_statuses": [[s.name, s.ok, s.reason] for s in v.statuses],
            #: the displayed book, best first, for the entry-benefit gate's cost walk
            "depth": {"perp_bids": [list(x) for x in v.perp_bids], "perp_asks": [list(x) for x in v.perp_asks],
                      "spot_bids": [list(x) for x in v.spot_bids], "spot_asks": [list(x) for x in v.spot_asks]},
        }
