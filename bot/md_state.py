"""The live market state and the validated view the book is allowed to decide on.

`MarketState` folds parsed public frames into a perp book, a spot book, the
tickers, the trade tape and the connection health. `MarketState.view()` turns
that into an immutable :class:`MarketView` in which every component carries its
own status. The view answers one question per purpose, never "is it roughly OK":

* ``refusals("entry")``  - every reason new exposure must not be opened now;
* ``refusals("monitor")`` - the (smaller) set that blinds management of a HELD
  pair. A risk refusal prevents new exposure; it must not stop a risk-reducing
  action, so a stale spot book or an unknown instrument spec never blocks a close.

Nothing is interpolated or carried forward past its freshness window: a component
older than its limit is *absent* from the view's usable fields, with the reason
recorded, and the entry refusal says so.

Deterministic: `view(now_mono_s, now_wall_ms)` is a pure function of the state
and the two clock arguments, so a replay of the stored frames reproduces every
stored view exactly.
"""
from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Deque, Dict, List, Optional, Tuple

import md_book as B
from market_snapshot import StaleMarket as _StaleMarket
import md_events as E
import md_wire as W

ENTRY = "entry"
MONITOR = "monitor"


@dataclass(frozen=True)
class MarketPolicy:
    """Data-quality tolerances. These decide when data is too old or too
    inconsistent to act on; they are not strategy parameters and they never make
    a decision more aggressive."""

    #: A book not updated for this long is a memory of a market, not a market.
    #: BTCUSDT updates tens of times a second; 3 s of silence is a dead feed.
    book_max_age_s: float = 3.0
    #: Ticker fields (mark, index, funding) are pushed on change; 10 s covers a
    #: calm second without trusting a frozen stream.
    ticker_max_age_s: float = 10.0
    #: Perp and spot must describe the same instant: neither may be older than
    #: this, which bounds the drift inside the basis between them.
    max_leg_span_s: float = 3.0
    #: The next settlement time must not already be this far in the past.
    funding_rollover_grace_s: float = 30.0
    #: Local receive time minus the venue stamp, 5th percentile: below this our
    #: clock is behind (or the venue stamps the future).
    skew_floor_ms: int = -1000
    #: ... and the median above this means our clock is ahead or the feed lags.
    skew_ceiling_ms: int = 5000
    skew_min_samples: int = 20
    skew_window: int = 300
    #: A REST read this old no longer counts as a recent cross-check.
    rest_check_max_age_s: float = 120.0
    #: Perp and spot touch must agree with the REST reference within this many
    #: bps (they are not simultaneous, so this is wide on purpose).
    rest_ws_tolerance_bps: float = 20.0
    #: Most recent frames kept per rejection counter and incident log.
    incident_log: int = 500
    trade_tape: int = 2000


@dataclass(frozen=True)
class ComponentStatus:
    name: str
    ok: bool
    reason: str = ""
    age_s: Optional[float] = None


@dataclass(frozen=True)
class MarketView:
    """One validated observation. Fields are None where the component was not
    usable; `statuses` says why. Every number here was parsed from a venue frame."""

    symbol: str
    observed_wall_ms: int
    observed_mono_s: float
    environment: str = "mainnet"        # whose prices these are: "mainnet" | "testnet"
    # perpetual
    perp_bid: Optional[float] = None
    perp_bid_size: Optional[float] = None
    perp_ask: Optional[float] = None
    perp_ask_size: Optional[float] = None
    perp_bids: Tuple[E.Level, ...] = ()
    perp_asks: Tuple[E.Level, ...] = ()
    perp_mark: Optional[float] = None
    perp_index: Optional[float] = None
    perp_last: Optional[float] = None
    funding_rate: Optional[float] = None           # the venue's forecast for the next settlement
    next_funding_ms: Optional[int] = None
    funding_interval_h: Optional[int] = None
    funding_cap: Optional[float] = None
    open_interest: Optional[float] = None
    open_interest_value: Optional[float] = None
    # spot
    spot_bid: Optional[float] = None
    spot_bid_size: Optional[float] = None
    spot_ask: Optional[float] = None
    spot_ask_size: Optional[float] = None
    spot_bids: Tuple[E.Level, ...] = ()
    spot_asks: Tuple[E.Level, ...] = ()
    spot_last: Optional[float] = None
    # instruments and the last settled print (REST-sourced; None until bootstrapped)
    perp_spec: Optional[E.InstrumentSpec] = None
    spot_spec: Optional[E.InstrumentSpec] = None
    last_funding_print: Optional[Tuple[float, int]] = None   # (rate fraction, settled_ms)
    # ages and the oldest component, which is the true span of this observation
    perp_book_age_s: Optional[float] = None
    spot_book_age_s: Optional[float] = None
    perp_book_exch_ts_ms: Optional[int] = None
    spot_book_exch_ts_ms: Optional[int] = None
    leg_span_s: Optional[float] = None
    clock_skew_ms: Optional[int] = None            # median(recv - venue stamp), None until measured
    max_leg_span_s: float = 3.0                    # the policy limit this view is judged against
    statuses: Tuple[ComponentStatus, ...] = ()

    # -- derived (calculated, not observed) -----------------------------------------
    @property
    def perp_mid(self) -> Optional[float]:
        return None if self.perp_bid is None or self.perp_ask is None else (self.perp_bid + self.perp_ask) / 2.0

    @property
    def spot_mid(self) -> Optional[float]:
        return None if self.spot_bid is None or self.spot_ask is None else (self.spot_bid + self.spot_ask) / 2.0

    @property
    def perp_spread_bps(self) -> Optional[float]:
        m = self.perp_mid
        return None if m is None else (self.perp_ask - self.perp_bid) / m * 1e4

    @property
    def spot_spread_bps(self) -> Optional[float]:
        m = self.spot_mid
        return None if m is None else (self.spot_ask - self.spot_bid) / m * 1e4

    @property
    def basis_bps(self) -> Optional[float]:
        """Perp mid over spot mid, the basis the executable prices see (the mark
        basis is `mark_basis_bps`). None unless both books are usable."""
        p, s = self.perp_mid, self.spot_mid
        return None if p is None or s is None else (p / s - 1.0) * 1e4

    @property
    def mark_basis_bps(self) -> Optional[float]:
        if self.perp_mark is None or self.perp_index is None:
            return None
        return (self.perp_mark / self.perp_index - 1.0) * 1e4

    def status(self, name: str) -> Optional[ComponentStatus]:
        for s in self.statuses:
            if s.name == name:
                return s
        return None

    def refusals(self, purpose: str = ENTRY) -> List[str]:
        """Every reason this view must not be acted on for `purpose`. Empty means
        usable. Reasons are stable strings ("COMPONENT:reason")."""
        need = _ENTRY_NEEDS if purpose == ENTRY else _MONITOR_NEEDS
        out: List[str] = []
        for s in self.statuses:
            if s.name in need and not s.ok:
                out.append(f"{s.name}:{s.reason}")
        if purpose == ENTRY and self.leg_span_s is not None and self.leg_span_s > self.max_leg_span_s:
            out.append(f"legs:SPAN_{self.leg_span_s:.1f}s")
        return out

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {}
        for k, v in self.__dict__.items():
            if k == "statuses":
                d[k] = [[s.name, s.ok, s.reason, s.age_s] for s in v]
            elif k in ("perp_spec", "spot_spec"):
                d[k] = None if v is None else [v.symbol, v.status, v.tick_size, v.qty_step,
                                               v.min_qty, v.max_qty, v.min_notional]
            else:
                d[k] = v
        return d


_ENTRY_NEEDS = ("connection", "perp_book", "spot_book", "perp_ticker", "clock",
                "instruments", "rest_agreement", "funding_print")
_MONITOR_NEEDS = ("connection", "perp_book", "perp_ticker")


class MarketUnusable(_StaleMarket):
    """The view cannot be acted on for the asked purpose."""

    def __init__(self, purpose: str, reasons: List[str]):
        super().__init__(f"market view unusable for {purpose}: " + "; ".join(reasons))
        self.purpose = purpose
        self.reasons = list(reasons)


class _TickerState:
    def __init__(self) -> None:
        self.fields: Dict[str, Any] = {}
        self.synced = False
        self.last_mono_s: Optional[float] = None
        self.last_wall_ms: Optional[int] = None
        self.last_exch_ts_ms: Optional[int] = None

    def reset(self) -> None:
        self.__init__()


class MarketState:
    def __init__(self, symbol: str = "BTCUSDT", policy: Optional[MarketPolicy] = None,
                 depth: int = 50, environment: str = "mainnet") -> None:
        if environment not in ("mainnet", "testnet"):
            raise ValueError(f"environment {environment!r}")
        self.environment = environment
        self.symbol = symbol
        self.policy = policy or MarketPolicy()
        self.depth = depth
        self.books: Dict[str, B.LocalBook] = {
            E.LINEAR: B.LocalBook(E.LINEAR, symbol, depth),
            E.SPOT: B.LocalBook(E.SPOT, symbol, depth)}
        self.tickers: Dict[str, _TickerState] = {E.LINEAR: _TickerState(), E.SPOT: _TickerState()}
        self.connected: Dict[str, bool] = {E.LINEAR: False, E.SPOT: False}
        self.specs: Dict[str, E.InstrumentSpec] = {}
        self.last_print: Optional[E.FundingPrint] = None
        self.rest_touch: Dict[str, Tuple[float, float, float]] = {}
        self.trades: Dict[str, Deque[E.Trade]] = {E.LINEAR: deque(maxlen=self.policy.trade_tape),
                                                  E.SPOT: deque(maxlen=self.policy.trade_tape)}
        self._trade_ids: Dict[str, Deque[str]] = {E.LINEAR: deque(maxlen=self.policy.trade_tape),
                                                  E.SPOT: deque(maxlen=self.policy.trade_tape)}
        self._trade_id_set: Dict[str, set] = {E.LINEAR: set(), E.SPOT: set()}
        self._lags: Deque[int] = deque(maxlen=self.policy.skew_window)
        self.rejections: Dict[str, int] = {}
        self.frames = 0
        self.incidents: Deque[E.Incident] = deque(maxlen=self.policy.incident_log)

    # -- ingest -----------------------------------------------------------------------

    def ingest(self, parsed: W.Parsed, *, category: str) -> List[E.Incident]:
        """Fold one parsed frame in. Returns the incidents it caused (gaps, resyncs)."""
        out: List[E.Incident] = []
        self.frames += 1
        for rej in parsed.rejections:
            self.rejections[rej.reason] = self.rejections.get(rej.reason, 0) + 1
        for ev in parsed.events:
            if isinstance(ev, (E.BookSnapshot, E.BookDelta)):
                out += self._on_book(ev)
            elif isinstance(ev, E.Ticker):
                self._on_ticker(ev)
            elif isinstance(ev, E.Trade):
                self._on_trade(ev)
            lag = ev.prov.venue_lag_ms if hasattr(ev, "prov") else None
            if lag is not None and isinstance(ev, (E.BookSnapshot, E.BookDelta, E.Ticker)):
                self._lags.append(lag)
        for inc in out:
            self.incidents.append(inc)
        return out

    def _inc(self, kind: str, detail: str, prov: E.Provenance, **data) -> E.Incident:
        return E.Incident(kind, detail, prov.recv_wall_ms, prov.recv_mono_s, prov.conn_id,
                          prov.topic, data)

    def _on_book(self, ev) -> List[E.Incident]:
        book = self.books.get(ev.category)
        if book is None or ev.symbol != self.symbol or ev.depth != self.depth:
            return []
        res = book.apply(ev)
        if res.status == B.RESET and ev.update_id == 1:
            return [self._inc(E.VENUE_SNAPSHOT_RESET, res.detail, ev.prov, category=ev.category)]
        if res.status == B.GAP:
            return [self._inc(E.SEQUENCE_GAP, res.detail, ev.prov, category=ev.category)]
        if res.status in (B.CROSSED, B.OFF_TICK, B.OVERSIZE):
            return [self._inc(E.RESYNC_REQUESTED, f"{res.status}: {res.detail}", ev.prov,
                              category=ev.category)]
        if res.status == B.OUT_OF_ORDER:
            return [self._inc(E.OUT_OF_ORDER, res.detail, ev.prov, category=ev.category)]
        if res.status == B.DUPLICATE:
            return [self._inc(E.DUPLICATE_FRAME, res.detail, ev.prov, category=ev.category)]
        return []

    def _on_ticker(self, ev: E.Ticker) -> None:
        if ev.symbol != self.symbol or ev.category not in self.tickers:
            return
        t = self.tickers[ev.category]
        if ev.is_snapshot:
            t.fields = dict(ev.fields)
            t.synced = True
        elif t.synced:
            t.fields.update(ev.fields)
        else:
            return                      # a delta with no snapshot under it is not a ticker
        t.last_mono_s, t.last_wall_ms = ev.prov.recv_mono_s, ev.prov.recv_wall_ms
        t.last_exch_ts_ms = ev.prov.exch_ts_ms

    def _on_trade(self, ev: E.Trade) -> None:
        if ev.symbol != self.symbol or ev.category not in self.trades:
            return
        ids = self._trade_id_set[ev.category]
        if ev.trade_id in ids:
            return
        dq = self._trade_ids[ev.category]
        if len(dq) == dq.maxlen:
            ids.discard(dq[0])
        dq.append(ev.trade_id)
        ids.add(ev.trade_id)
        self.trades[ev.category].append(ev)

    # -- connection / reference data -------------------------------------------------------

    def on_connected(self, category: str) -> None:
        """A (re)connection: nothing held from the previous one is trusted, because
        frames were missed while it was down."""
        self.connected[category] = True
        self.books[category].invalidate("RECONNECTED")
        self.tickers[category].reset()

    def on_disconnected(self, category: str) -> None:
        self.connected[category] = False
        self.books[category].invalidate("DISCONNECTED")
        self.tickers[category].reset()

    def invalidate_book(self, category: str, reason: str) -> None:
        self.books[category].invalidate(reason)

    def set_specs(self, specs: List[E.InstrumentSpec]) -> None:
        for spec in specs:
            if spec.symbol == self.symbol:
                self.specs[spec.category] = spec
                self.books[spec.category].set_tick_size(spec.tick_size)

    def set_last_print(self, fp: E.FundingPrint) -> None:
        if fp.symbol == self.symbol and (self.last_print is None or fp.settled_ms >= self.last_print.settled_ms):
            self.last_print = fp

    def set_rest_touch(self, category: str, bid: float, ask: float, at_mono_s: float) -> None:
        """A REST order-book touch for one product (the cross-check of the socket)."""
        self.rest_touch[category] = (bid, ask, at_mono_s)

    # -- clock -------------------------------------------------------------------------------------

    def clock_status(self) -> Tuple[Optional[int], str]:
        p = self.policy
        lags = sorted(self._lags)
        if len(lags) < p.skew_min_samples:
            return None, "UNMEASURED"
        median = lags[len(lags) // 2]
        floor = lags[max(0, int(len(lags) * 0.05) - 1)]
        if floor < p.skew_floor_ms:
            return median, f"CLOCK_BEHIND_p05={floor}ms"
        if median > p.skew_ceiling_ms:
            return median, f"CLOCK_AHEAD_OR_FEED_LAGGING_p50={median}ms"
        return median, ""

    # -- the view ---------------------------------------------------------------------------------------

    def view(self, now_mono_s: float, now_wall_ms: int) -> MarketView:
        p = self.policy
        statuses: List[ComponentStatus] = []

        def book_part(cat: str, name: str):
            bk = self.books[cat]
            if not self.connected[cat]:
                statuses.append(ComponentStatus(name, False, "DISCONNECTED"))
                return None, None
            if not bk.synced:
                statuses.append(ComponentStatus(name, False, f"UNSYNCED:{bk.unsynced_reason}"))
                return None, None
            age = now_mono_s - (bk.last_applied_mono_s or now_mono_s)
            if age < 0:
                statuses.append(ComponentStatus(name, False, "TIME_WENT_BACKWARDS", age))
                return None, None
            if age > p.book_max_age_s:
                statuses.append(ComponentStatus(name, False, f"STALE_{age:.1f}s", age))
                return None, None
            lad, top = bk.ladders(), bk.top()
            if lad is None or top is None:
                statuses.append(ComponentStatus(name, False, "ONE_SIDED_OR_EMPTY", age))
                return None, None
            statuses.append(ComponentStatus(name, True, "", age))
            return (top, lad), age

        statuses.append(ComponentStatus(
            "connection", all(self.connected.values()),
            "" if all(self.connected.values()) else
            "DISCONNECTED:" + ",".join(c for c, ok in self.connected.items() if not ok)))
        pb, perp_age = book_part(E.LINEAR, "perp_book")
        sb, spot_age = book_part(E.SPOT, "spot_book")

        tk = self.tickers[E.LINEAR]
        f = tk.fields
        need = ("markPrice", "indexPrice", "fundingRate", "nextFundingTime", "fundingIntervalHour")
        perp_ticker_ok, why, tage = True, "", None
        if not tk.synced or tk.last_mono_s is None:
            perp_ticker_ok, why = False, "NO_SNAPSHOT"
        else:
            tage = now_mono_s - tk.last_mono_s
            missing = [k for k in need if k not in f]
            if missing:
                perp_ticker_ok, why = False, "MISSING:" + ",".join(missing)
            elif tage < 0:
                perp_ticker_ok, why = False, "TIME_WENT_BACKWARDS"
            elif tage > p.ticker_max_age_s:
                perp_ticker_ok, why = False, f"STALE_{tage:.1f}s"
            elif f["nextFundingTime"] < now_wall_ms - p.funding_rollover_grace_s * 1000:
                perp_ticker_ok, why = False, "FUNDING_TIME_IN_THE_PAST"
        statuses.append(ComponentStatus("perp_ticker", perp_ticker_ok, why, tage))

        skew, skew_why = self.clock_status()
        statuses.append(ComponentStatus("clock", skew_why == "", skew_why))

        pspec, sspec = self.specs.get(E.LINEAR), self.specs.get(E.SPOT)
        missing_specs = [n for n, s in (("perp", pspec), ("spot", sspec)) if s is None]
        bad_status = [n for n, s in (("perp", pspec), ("spot", sspec))
                      if s is not None and s.status != "Trading"]
        statuses.append(ComponentStatus(
            "instruments", not missing_specs and not bad_status,
            ("NO_SPEC:" + ",".join(missing_specs)) if missing_specs
            else ("NOT_TRADING:" + ",".join(bad_status)) if bad_status else ""))

        fp = self.last_print
        statuses.append(ComponentStatus("funding_print", fp is not None,
                                        "" if fp is not None else "NO_SETTLED_PRINT"))

        statuses.append(self._rest_status(pb, sb, now_mono_s))

        kw: Dict[str, Any] = {}
        if pb:
            (b, bs, a, as_), lad = pb[0], pb[1]
            kw.update(perp_bid=b, perp_bid_size=bs, perp_ask=a, perp_ask_size=as_,
                      perp_bids=lad[0], perp_asks=lad[1])
        if sb:
            (b, bs, a, as_), lad = sb[0], sb[1]
            kw.update(spot_bid=b, spot_bid_size=bs, spot_ask=a, spot_ask_size=as_,
                      spot_bids=lad[0], spot_asks=lad[1])
        if perp_ticker_ok:
            kw.update(perp_mark=f["markPrice"], perp_index=f["indexPrice"],
                      funding_rate=f["fundingRate"], next_funding_ms=int(f["nextFundingTime"]),
                      funding_interval_h=int(f["fundingIntervalHour"]),
                      perp_last=f.get("lastPrice"), funding_cap=f.get("fundingCap"),
                      open_interest=f.get("openInterest"),
                      open_interest_value=f.get("openInterestValue"))
        st = self.tickers[E.SPOT]
        if self.connected[E.SPOT] and st.synced and st.last_mono_s is not None \
                and 0 <= now_mono_s - st.last_mono_s <= p.ticker_max_age_s:
            kw["spot_last"] = st.fields.get("lastPrice")
        ages = [x for x in (perp_age, spot_age) if x is not None]
        return MarketView(
            symbol=self.symbol, observed_wall_ms=int(now_wall_ms), observed_mono_s=float(now_mono_s),
            environment=self.environment,
            perp_spec=pspec, spot_spec=sspec,
            last_funding_print=None if fp is None else (fp.rate, fp.settled_ms),
            perp_book_age_s=perp_age, spot_book_age_s=spot_age,
            perp_book_exch_ts_ms=self.books[E.LINEAR].last_exch_ts_ms if pb else None,
            spot_book_exch_ts_ms=self.books[E.SPOT].last_exch_ts_ms if sb else None,
            leg_span_s=(max(ages) if len(ages) == 2 else None),
            clock_skew_ms=skew, max_leg_span_s=p.max_leg_span_s,
            statuses=tuple(statuses), **kw)

    def _rest_status(self, pb, sb, now_mono_s: float) -> ComponentStatus:
        """Entry needs a recent REST touch for BOTH products that can coexist with
        the socket's touch. The reads are not simultaneous, so only a REST touch
        entirely above or below the socket's (beyond tolerance) is a disagreement."""
        tol = self.policy.rest_ws_tolerance_bps / 1e4
        worst_age = 0.0
        for name, cat, ws in (("PERP", E.LINEAR, pb), ("SPOT", E.SPOT, sb)):
            ref = self.rest_touch.get(cat)
            if ref is None:
                return ComponentStatus("rest_agreement", False, f"NO_REST_CROSSCHECK:{name}")
            rb, ra, at = ref
            age = now_mono_s - at
            if age < 0 or age > self.policy.rest_check_max_age_s:
                return ComponentStatus("rest_agreement", False, f"REST_CHECK_OLD:{name}:{age:.0f}s", age)
            worst_age = max(worst_age, age)
            if ws is None:
                continue                   # the book component already says why
            wb, wa = ws[0][0], ws[0][2]
            if rb > wa * (1 + tol) or ra < wb * (1 - tol):
                return ComponentStatus("rest_agreement", False, f"{name}_REST_WS_DISAGREE", age)
        return ComponentStatus("rest_agreement", True, "", worst_age)

    def health(self) -> Dict[str, Any]:
        return {"frames": self.frames, "rejections": dict(self.rejections),
                "books": {c: dict(b.counts, synced=b.synced) for c, b in self.books.items()},
                "connected": dict(self.connected),
                "clock": self.clock_status(), "incidents": len(self.incidents)}

    def digest(self) -> Tuple:
        """Canonical image of everything the view reads (replay-equality proof)."""
        return (tuple(self.books[c].digest() for c in (E.LINEAR, E.SPOT)),
                tuple((c, self.tickers[c].synced, tuple(sorted(self.tickers[c].fields.items())),
                       self.tickers[c].last_mono_s) for c in (E.LINEAR, E.SPOT)),
                tuple(sorted(self.connected.items())),
                tuple(sorted(self.rest_touch.items())),
                tuple(self._lags),
                None if self.last_print is None else (self.last_print.rate, self.last_print.settled_ms),
                tuple(sorted((k, v.tick_size, v.qty_step, v.min_qty, v.status)
                             for k, v in self.specs.items())))
