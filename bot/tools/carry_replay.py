#!/usr/bin/env python3
"""Replay the ENGINE. Not a model of it — the class that trades.

WHAT WAS WRONG
==============
`tools/carry_backtest.py` does not import `CarryEngine`. It never has. It
shares one thing with the trading program — `carry_costs.evaluate_entry` — and
reimplements everything else in its own loop: sizing, both-legs-or-neither, the
negative-funding streak, the delta band, the margin floor, the unwind, the fee
accounting, the day's entry allowance.

So every number this repository has ever produced — +7.23%/yr, the walk-forward
lines, the 0039 sweep, the maker-first comparison — came from a program that is
not the trading program. `carry_backtest` prints

    [x] rule_is_what_the_engine_runs

and it was never checked. That box asserted the GATE was the engine's. It is.
The STATE MACHINE around the gate is not, and the state machine is what decides
whether an order is sent.

WHAT THIS DOES
==============
Drives the real `CarryEngine` through history along the exact path `main.tick`
uses on a live venue:

    take_snapshot(broker, ...)  ->  view.assert_fresh()  ->  engine.on_candle(...)

against a `ReplayBroker` that answers every call the engine and the snapshot
make. Nothing about the decision is reimplemented here. This module supplies a
venue and a clock; the engine does the rest, including refusing.

    python3 tools/carry_replay.py --repo . --diff

`--diff` runs `carry_backtest` on the identical rows and prints every place the
two disagree. Those disagreements are the point of the file.

WHAT IT DELIBERATELY DOES NOT MODEL
===================================
THE LIQUIDATION GATE (the production risk gate this replay used to skip)
------------------------------------------------------------------------
Production builds the engine with `require_liquidation_check=True`
(main.build_bot): on every held tick the engine asks the venue for the
position's `liqPrice` and the account's `accountMMRate`, and unwinds when the
price is within MIN_LIQUIDATION_DISTANCE_PCT of liquidation. This corpus
holds settlement closes plus the interval's `perp_high`/`perp_low` - TRADED
prices. It holds no liquidation price, no leverage, no collateral and no
account margin ratio, and none of them can be derived from it without
inventing them.

So the replay does not pretend. With no `LiquidationInputs`:
  * the default runs the engine with the gate OFF and says so: the report's
    `liquidation.gate` is NOT_EVALUATED and `production_equivalent` is False;
    the P&L is research output, not what production would have done;
  * `require_liquidation_evidence=True` builds the engine exactly as
    production does and lets it halt, as it does live, on an unreadable
    liquidation price (a missing input never becomes a safe one).
With explicit `LiquidationInputs` the production gate runs every held tick on
the interval's ADVERSE price (the high, for a short) - a safe-looking close
cannot hide a breach - and a breach exits at the price where the distance
crossed the floor, not at the close. Only inputs of kind "recorded" that also
cover the account margin rate and margin multiple can make
`production_equivalent` True; "declared" inputs are a stated scenario.

`margin_multiple` is `positionIM / positionMM`, and INVENTORY F4 says that
field is a risk-tier ratio rather than liquidation headroom. Without recorded
evidence it is a CONSTANT that never trips the floor, and the report says so.

The ticker's funding FORECAST is not modelled either: `get_funding_bps` returns
the last SETTLED print, because the alternative offline is to hand the engine
the next print's rate, which is the future.
"""
from __future__ import annotations

import argparse
import bisect
import json
import math
import os
import sys
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, ROOT)

import carry_backtest as cb          # noqa: E402

DAY_MS = 86_400_000

#: Venue lot rules for Bybit linear/spot BTCUSDT, as read in 0036. The engine
#: snaps to these and refuses below them; the simulator has no concept of them,
#: which at the $100 cap is the difference between a book and a rejection.
LINEAR_RULES = {"qty_step": 0.001, "min_qty": 0.001, "min_notional": 0.0}
SPOT_RULES = {"qty_step": 1e-6, "min_qty": 4.8e-5, "min_notional": 1.0}

#: A margin multiple that never trips the 2.0 floor. The default, and declared.
INERT_MARGIN = 10.0


class ReplayRefusal(RuntimeError):
    """The harness cannot answer a call the engine made. Never swallowed: a
    replay that silently invents a venue answer is the thing this file is
    about."""


class LiquidationEvidenceMissing(ReplayRefusal):
    """The engine asked for a liquidation view and none was supplied. In the
    replay, as live, that is an unreadable liquidation price - never a safe
    one."""


class LiquidationEvidenceInvalid(ReplayRefusal):
    """Supplied liquidation inputs are not usable numbers for this bar."""


@dataclass(frozen=True)
class LiquidationInputs:
    """Explicit liquidation inputs for the production gate. Nothing here has a
    default value: every number is the caller's, and its provenance is named.

    kind     "recorded" - observed on a venue, with `source` naming the
             recording; "declared" - a scenario the caller states, NOT a
             measurement.
    liq_price(entry_perp, bar) -> the position's liquidation price in quote
             currency, or None when the venue would say it is beyond its price
             bounds (a statement, recorded and counted - not a default).
    account_mm_rate(bar) -> the account maintenance-margin rate, or None.
    margin_multiple(bar) -> `positionIM / positionMM` for this bar, or None to
             keep the declared constant.
    Callables see only the bar being decided on and the entry price: no later
    bar is reachable from here.
    """

    kind: str
    source: str
    liq_price: Callable[[float, Any], Optional[float]]
    account_mm_rate: Optional[Callable[[Any], Optional[float]]] = None
    margin_multiple: Optional[Callable[[Any], float]] = None

    def __post_init__(self) -> None:
        if self.kind not in ("recorded", "declared"):
            raise ValueError(f"kind must be 'recorded' or 'declared', "
                             f"not {self.kind!r}")
        if not str(self.source).strip():
            raise ValueError("liquidation inputs must name their source")
        if not callable(self.liq_price):
            raise ValueError("liq_price must be callable")

    @classmethod
    def from_observations(cls, observations: Sequence[Dict[str, Any]], *,
                          source: str, max_age_ms: int = 8 * 3600 * 1000
                          ) -> "LiquidationInputs":
        """RECORDED evidence: what the production gate actually read from the
        venue (`StateStore.load_risk_observations`), for replaying the window
        it was recorded in.

        For each bar the latest observation AT OR BEFORE the bar is used -
        never a later one. Nothing earlier than `max_age_ms` (one funding
        interval, the span a bar summarises) is reused, and a bar with no
        usable observation is refused, not defaulted: the engine then halts
        on an unreadable liquidation price or skips the tick, exactly as it
        does live. A recorded `liq_price` of None is accepted only when the
        venue said so (BEYOND_VENUE_PRICE_BOUNDS).
        """
        if not observations:
            raise ValueError("no observations to build evidence from")
        if not str(source).strip():
            raise ValueError("liquidation inputs must name their source")
        if not (isinstance(max_age_ms, int) and max_age_ms > 0):
            raise ValueError("max_age_ms must be a positive integer")
        ordered = sorted((dict(o) for o in observations),
                         key=lambda o: int(o["ms"]))
        stamps = [int(o["ms"]) for o in ordered]

        def latest(bar: Any) -> Dict[str, Any]:
            i = bisect.bisect_right(stamps, int(bar.ms)) - 1
            if i < 0:
                raise LiquidationEvidenceMissing(
                    f"no observation at or before {cb._iso_ms(int(bar.ms))}")
            age = int(bar.ms) - stamps[i]
            if age > max_age_ms:
                raise LiquidationEvidenceInvalid(
                    f"the latest observation is {age / 3.6e6:.1f}h older than "
                    f"{cb._iso_ms(int(bar.ms))} (limit "
                    f"{max_age_ms / 3.6e6:.1f}h)")
            return ordered[i]

        def liq_price(_entry: float, bar: Any) -> Optional[float]:
            o = latest(bar)
            if o.get("liq_price") is None:
                if o.get("reason") != "BEYOND_VENUE_PRICE_BOUNDS":
                    raise LiquidationEvidenceInvalid(
                        f"observation at {o['ms']} has no liquidation price "
                        f"and the venue did not call it unreachable "
                        f"({o.get('reason')!r})")
                return None
            return o["liq_price"]

        def account_mm_rate(bar: Any) -> Optional[float]:
            return latest(bar).get("account_mm_rate")

        def margin_multiple(bar: Any) -> float:
            value = latest(bar).get("margin_multiple")
            if value is None:
                raise LiquidationEvidenceInvalid(
                    "the observation states no margin ratio")
            return value

        return cls(kind="recorded", source=source, liq_price=liq_price,
                   account_mm_rate=account_mm_rate,
                   margin_multiple=margin_multiple)

    @classmethod
    def declared_isolated(cls, leverage: float, source: str
                          ) -> "LiquidationInputs":
        """A DECLARED scenario: an isolated-margin short at `leverage`, its
        liquidation price at entry x (1 + 1/leverage). Initial margin only -
        maintenance margin and fees are ignored, so the true price is nearer
        and this is the optimistic edge of the scenario. Not a measurement,
        not the venue's number, and not any leverage the book uses."""
        lev = float(leverage)
        if not (math.isfinite(lev) and lev >= 1.0):
            raise ValueError(f"leverage must be a finite number >= 1, "
                             f"not {leverage!r}")
        return cls(kind="declared", source=source,
                   liq_price=lambda entry, _bar: entry * (1.0 + 1.0 / lev))


def recorded_liquidation_from_db(path: str) -> LiquidationInputs:
    """Recorded evidence from a state database, opened READ-ONLY. Refuses a
    missing database, a database without the table, and one with no rows."""
    import sqlite3
    if not os.path.isfile(path):
        raise ReplayRefusal(f"{path} does not exist")
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        try:
            rows = [dict(r) for r in con.execute(
                "SELECT * FROM carry_risk_observation ORDER BY ms")]
        finally:
            con.close()
    except sqlite3.Error as exc:
        raise ReplayRefusal(f"{path}: no readable risk observations "
                            f"({exc})") from exc
    if not rows:
        raise ReplayRefusal(f"{path} holds no risk observations")
    label = (f"state db {os.path.basename(path)} ({len(rows)} observations, "
             f"{cb._iso_ms(int(rows[0]['ms']))} .. "
             f"{cb._iso_ms(int(rows[-1]['ms']))})")
    return LiquidationInputs.from_observations(rows, source=label)


def _min_liquidation_distance_pct() -> float:
    """The production floor, read from the engine - never restated here."""
    import carry_engine
    return float(carry_engine.MIN_LIQUIDATION_DISTANCE_PCT)


def _finite_positive(value: Any, what: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise LiquidationEvidenceInvalid(f"{what} is {value!r}") from None
    if not (math.isfinite(number) and number > 0):
        raise LiquidationEvidenceInvalid(f"{what} is {value!r}")
    return number


@dataclass
class Fill:
    ms: int
    side: str
    product: str
    qty: float
    price: float
    fee: float
    maker: bool = False


@dataclass
class ReplayBroker:
    """A venue made of bars. Answers every call the engine and snapshot make.

    Prices come from the bar the engine is currently looking at, so a fill is
    at the price the decision was made on plus a declared impact haircut. That
    is optimistic about latency and honest about it: see `not_modelled`.
    """

    rows: Sequence[Any]
    taker_spot_bps: float = cb.TAKER_BPS_SPOT
    taker_perp_bps: float = cb.TAKER_BPS_PERP
    maker_bps: float = 2.0
    impact_bps: float = 0.0
    inventory_btc: float = 1e9
    margin_multiple: float = INERT_MARGIN
    spread_bps: float = 0.013            # Bybit BTCUSDT touch, measured 0040
    i: int = 0
    fills: List[Fill] = field(default_factory=list)
    perp_qty: float = 0.0
    refusals: Dict[str, int] = field(default_factory=dict)
    #: Explicit liquidation inputs, or None. None is never read as "safe".
    liquidation: Optional[LiquidationInputs] = None
    #: Set by the gate when the interval's adverse price crossed the floor:
    #: the price the unwind buys back at. Cleared at the start of every bar.
    forced_perp_price: Optional[float] = None
    _entry_qty: float = 0.0
    _entry_notional: float = 0.0
    liq_stats: Dict[str, Any] = field(default_factory=lambda: {
        "ticks_evaluated": 0, "min_distance_pct": None,
        "intrabar_trips": 0, "unreachable_ticks": 0})

    # -- what the snapshot reads -----------------------------------------
    @property
    def bar(self):
        return self.rows[self.i]

    def get_mark(self, symbol: str) -> float:
        if self.forced_perp_price is not None:
            return float(self.forced_perp_price)
        return float(self.bar.perp)

    def get_spot_mark(self, symbol: str) -> float:
        return float(self.bar.spot)

    def get_funding_bps(self, symbol: str) -> float:
        # The SETTLED print, not a forecast. Offline, the only available
        # forecast is the next print, and that is the future.
        return float(self.bar.rate) * 1e4

    def get_funding_print(self, symbol: str):
        return float(self.bar.rate) * 1e4, int(self.bar.ms)

    def get_margin_multiple(self, symbol: str) -> float:
        ev = self.liquidation
        if ev is not None and ev.margin_multiple is not None:
            return _finite_positive(ev.margin_multiple(self.bar),
                                    "margin_multiple")
        return float(self.margin_multiple)

    def get_liquidation_view(self, symbol: str) -> Dict[str, Any]:
        """The production gate's input, from EXPLICIT evidence only.

        Without it this raises, exactly as `CarryBroker` raises on an
        unreadable position: the engine halts on it. It is never answered
        with a distant price. With it, the distance is measured from the
        interval's ADVERSE price - the high, for the short - because the
        settlement close is only one instant of the interval the gate polls.
        """
        ev = self.liquidation
        if ev is None:
            raise LiquidationEvidenceMissing(
                "no liquidation evidence was supplied for this replay: the "
                "corpus holds no liqPrice, leverage, collateral or "
                "accountMMRate, and none is invented")
        bar = self.bar
        mm = None
        # Account-rate evidence supplied -> the account-level (cross) gate runs
        # and a rate that is not usable halts, as live. None supplied -> a
        # per-position scenario (e.g. `declared_isolated`): the mandatory
        # gate is the liquidation distance.
        mode = ("ISOLATED_MARGIN" if ev.account_mm_rate is None
                else "REGULAR_MARGIN")
        if ev.account_mm_rate is not None:
            raw = ev.account_mm_rate(bar)
            if raw is not None:
                try:
                    mm = float(raw)
                except (TypeError, ValueError):
                    raise LiquidationEvidenceInvalid(
                        f"account_mm_rate is {raw!r}") from None
                if not math.isfinite(mm) or mm < 0:
                    raise LiquidationEvidenceInvalid(
                        f"account_mm_rate is {raw!r}")
        qty = abs(self.perp_qty)
        close = _finite_positive(bar.perp, "perp close")
        if qty <= 0 or self._entry_qty <= 0:
            return {"size": 0.0, "side": "", "mark": close, "liq_price": None,
                    "distance_pct": None, "account_mm_rate": mm,
                    "margin_mode": mode, "reason": "NO_POSITION"}
        entry = self._entry_notional / self._entry_qty
        liq = ev.liq_price(entry, bar)
        stats = self.liq_stats
        stats["ticks_evaluated"] += 1
        if liq is None:
            stats["unreachable_ticks"] += 1
            return {"size": qty, "side": "Sell", "mark": close,
                    "liq_price": None, "distance_pct": None,
                    "account_mm_rate": mm, "margin_mode": mode,
                    "reason": "BEYOND_VENUE_PRICE_BOUNDS"}
        liq = _finite_positive(liq, "liquidation price")
        high = _finite_positive(bar.perp_high, "perp high")
        low = _finite_positive(bar.perp_low, "perp low")
        if high < close * (1 - 1e-9) or low > close * (1 + 1e-9):
            raise LiquidationEvidenceInvalid(
                f"the bar's range [{low}, {high}] excludes its close {close}")
        adverse = max(close, high)
        distance = 100.0 * (liq - adverse) / adverse
        at_close = 100.0 * (liq - close) / close
        floor = _min_liquidation_distance_pct()
        prior = stats["min_distance_pct"]
        stats["min_distance_pct"] = distance if prior is None else min(prior,
                                                                       distance)
        if distance < floor:
            if at_close >= floor:
                stats["intrabar_trips"] += 1      # the close alone hid it
            crossing = liq / (1.0 + floor / 100.0)
            # The price at which the distance crossed the floor, inside the
            # interval's traded range. If the whole range lies beyond it the
            # crossing happened in a gap this data cannot place: take the
            # worst price for a short's buy-back, the high.
            self.forced_perp_price = adverse if crossing < low \
                else min(crossing, adverse)
        return {"size": qty, "side": "Sell", "mark": adverse, "liq_price": liq,
                "distance_pct": distance, "account_mm_rate": mm,
                "margin_mode": mode, "reason": ""}

    def _note_perp(self, side: str, qty: float, price: float) -> None:
        """Track the open short's average entry for the liquidation view."""
        if side == "Sell":
            self._entry_qty += qty
            self._entry_notional += qty * price
        else:
            if self._entry_qty > 0:
                avg = self._entry_notional / self._entry_qty
                self._entry_qty = max(0.0, self._entry_qty - qty)
                self._entry_notional = self._entry_qty * avg
            if abs(self.perp_qty) <= 1e-12:
                self._entry_qty = self._entry_notional = 0.0

    # -- what the engine reads --------------------------------------------
    def get_lot_rules(self, symbol: str, product: str) -> Dict[str, float]:
        return dict(LINEAR_RULES if product == "linear" else SPOT_RULES)

    def get_fee_rates(self, symbol: str, product: str) -> Dict[str, float]:
        return {"maker_bps": self.maker_bps,
                "taker_bps": (self.taker_perp_bps if product == "linear"
                              else self.taker_spot_bps)}

    def get_spot_inventory(self, symbol: str) -> float:
        return float(self.inventory_btc)

    def get_perp_position(self, symbol: str) -> float:
        return abs(self.perp_qty)

    def get_open_carry_orders(self, spot_symbol: str, perp_symbol: str):
        return []

    def get_book_top(self, symbol: str, product: str) -> Dict[str, float]:
        mid = self.bar.perp if product == "linear" else self.bar.spot
        half = mid * self.spread_bps / 2e4
        return {"bid": mid - half, "ask": mid + half}

    # -- orders -----------------------------------------------------------
    def _price(self, side: str, base: float) -> float:
        """Cross the spread and pay the declared impact, in the bad direction."""
        drag = self.impact_bps / 1e4
        return base * (1.0 + drag) if side == "Buy" else base * (1.0 - drag)

    @staticmethod
    def _as_venue_fee(fee_usd: float, price: float, side: str,
                      product: str) -> float:
        """The fee in the unit THE VENUE reports it in, for the caller.

        Bybit takes a spot BUY fee in the COIN and everything else in the
        quote currency — INVENTORY F3, and `CarryBroker.LegFill.fee` says so
        in as many words. `ledger.spot_buy` therefore MULTIPLIES the field it
        is handed by the price.

        This broker computes its fees in USD for its own accounting, and
        handed that same number straight back. So every spot buy fee reaching
        the journal was inflated by the BTC price: a four-year `--mode
        acquire` replay booked **$388,719,591** of fees against the $20,542
        this file's own totals report, and the journal's "agrees with the run
        above" line read False. Nothing caught it because no test runs the
        replay in acquire, and LEDGER_0044's "agrees to the cent" was measured
        in overlay, which never buys spot.

        `Fill.fee` deliberately stays in USD: it is what `fees_usd`
        accumulates a few lines down, and that total was never wrong. Only
        what crosses the broker boundary is converted.
        """
        if product == "spot" and side == "Buy" and price > 0:
            return fee_usd / price
        return fee_usd

    def place_market(self, *, symbol, side, qty, product):
        if qty <= 0 or not math.isfinite(qty):
            return None
        if product == "linear":
            base = (self.forced_perp_price if self.forced_perp_price is not None
                    else self.bar.perp)
        else:
            base = self.bar.spot
        price = self._price(side, base)
        rate = (self.taker_perp_bps if product == "linear"
                else self.taker_spot_bps)
        fee = qty * price * rate / 1e4
        self.fills.append(Fill(int(self.bar.ms), side, product, qty, price,
                               fee))
        if product == "linear":
            self.perp_qty += qty if side == "Sell" else -qty
            self._note_perp(side, qty, price)
        venue_fee = self._as_venue_fee(fee, price, side, product)
        fee_currency = ("BTC" if product == "spot" and side == "Buy"
                        else "USDT")
        fee_detail = {"known": True, "source": "cumFeeDetail",
                      "components": [{"amount": venue_fee,
                                      "currency": fee_currency}]}
        return {"filled_qty": qty, "avg_price": price,
                "order_link_id": f"r{len(self.fills)}",
                "fee": venue_fee, "fee_detail": fee_detail}

    def place_post_only(self, *, symbol, side, qty, price, product):
        """Every resting order fills, at the touch, at the maker rate.

        The most optimistic assumption in this file and the one with no
        offline evidence behind it: fill probability is unmeasured (0040).
        `--taker` runs the same replay without it.
        """
        fee = qty * price * self.maker_bps / 1e4
        self.fills.append(Fill(int(self.bar.ms), side, product, qty, price,
                               fee, maker=True))
        if product == "linear":
            self.perp_qty += qty if side == "Sell" else -qty
            self._note_perp(side, qty, price)
        venue_fee = self._as_venue_fee(fee, price, side, product)
        fee_currency = ("BTC" if product == "spot" and side == "Buy"
                        else "USDT")
        fee_detail = {"known": True, "source": "cumFeeDetail",
                      "components": [{"amount": venue_fee,
                                      "currency": fee_currency}]}
        return {"filled_qty": qty, "avg_price": price,
                "order_link_id": f"m{len(self.fills)}",
                "fee": venue_fee, "fee_detail": fee_detail,
                "maker": True, "resting": False}

    def cancel_order(self, *, symbol, order_link_id, product):
        return {"filled_qty": 0.0, "avg_price": 0.0, "fee": 0.0}


class _Store:
    """The store the pair gate asks about the kill switch. Nothing else."""

    def is_kill_switch_engaged(self):
        return (False, "")

    def trip_kill_switch(self, reason):
        self.tripped = reason


#: Columns a supplied bar file must carry. One row per FUNDING SETTLEMENT —
#: not per candle: the engine's clock is the settlement clock (0034), and a
#: file of 1-minute candles is resampled to settlements before it gets here.
BAR_COLUMNS = ("ms", "perp", "spot", "rate", "perp_high", "perp_low")


def load_bars(path: str) -> List[Any]:
    """Read a settlement series from CSV or JSON. Refuses a malformed one.

    CSV: a header row naming `ms,perp,spot,rate,perp_high,perp_low` in any
    order. JSON: a list of objects with those keys, or `{"rows": [...]}`.

        ms          settlement stamp, epoch MILLISECONDS, UTC
        perp        perp mark at that settlement
        spot        spot price at that settlement, SAME VENUE
        rate        the SETTLED funding rate as a fraction (0.0001 = 1 bp),
                    not bps and not the ticker's forecast
        perp_high   perp high over the interval that ENDS at this settlement
        perp_low    perp low over the same interval

    Rows are sorted by `ms` and duplicates on `ms` are refused rather than
    deduplicated: two prints with one stamp means the file was built from two
    sources and nobody knows which is right.
    """
    import csv

    if not os.path.exists(path):
        raise ReplayRefusal(f"no bar file at {path}")
    raw: List[Dict[str, Any]]
    if path.endswith(".json"):
        with open(path, encoding="utf-8") as handle:
            loaded = json.load(handle)
        raw = loaded["rows"] if isinstance(loaded, dict) else loaded
    else:
        with open(path, newline="", encoding="utf-8") as handle:
            raw = list(csv.DictReader(handle))
    if not raw:
        raise ReplayRefusal(f"{path} has no rows")
    missing = [c for c in BAR_COLUMNS if c not in raw[0]]
    if missing:
        raise ReplayRefusal(
            f"{path} is missing {missing}; required: {list(BAR_COLUMNS)}")
    out = []
    for n, row in enumerate(raw):
        try:
            values = {c: float(row[c]) for c in BAR_COLUMNS}
        except (TypeError, ValueError) as exc:
            raise ReplayRefusal(f"{path} row {n} is unreadable: {row!r}") from exc
        if not all(math.isfinite(v) for v in values.values()):
            raise ReplayRefusal(f"{path} row {n} carries a non-finite value")
        if values["perp"] <= 0 or values["spot"] <= 0:
            raise ReplayRefusal(f"{path} row {n} has a non-positive price")
        if abs(values["rate"]) > 0.05:
            raise ReplayRefusal(
                f"{path} row {n} rate {values['rate']} is above 5% for one "
                "8h print — this is almost certainly bps, not a fraction")
        out.append(cb.Settlement(ms=int(values["ms"]), perp=values["perp"],
                                 spot=values["spot"], rate=values["rate"],
                                 perp_high=values["perp_high"],
                                 perp_low=values["perp_low"]))
    out.sort(key=lambda r: r.ms)
    stamps = [r.ms for r in out]
    if len(set(stamps)) != len(stamps):
        raise ReplayRefusal(
            f"{path} has duplicate settlement stamps; two prints with one "
            "stamp means the file was built from two sources and nobody "
            "knows which is right")
    return out


def replay_snapshot(perp: float = 30_000.0, spot: float = 29_988.0,
                    margin: float = INERT_MARGIN):
    """A snapshot that `assert_fresh` accepts, for testing a gate directly."""
    import time as _time

    from market_snapshot import MarketSnapshot
    now = _time.time()
    return MarketSnapshot(perp_mark=perp, spot_mark=spot, funding_bps=0.6,
                          margin_multiple=margin, observed_at_s=now,
                          funding_print_bps=0.6,
                          funding_print_ms=int(now * 1000))


def trade_concentration(trade_rows: Sequence[Dict[str, Any]], *, notional: float,
                        fee_events: Sequence[Any] = (),
                        borrow_events: Sequence[Any] = (),
                        net_usd: Optional[float] = None) -> Dict[str, Any]:
    """How the return was earned, not just how big it is.

    Per-trade net is the engine's funding, plus BOTH legs' price move, minus the
    fees and borrow booked at ticks inside the trade (``fee_events`` and
    ``borrow_events`` are ``(ms, usd)`` pairs, so nothing is allocated by a
    guess). A headline that one trade supplied, or that came with a deep fall on
    the way, is a different claim from one spread evenly, and the headline alone
    hides both. ``reconciles_to_net`` says whether the per-trade numbers add
    back to the headline; when they do not, say so rather than trust either.
    """
    trades = sorted(trade_rows, key=lambda t: (int(t["closed_ms"]), int(t["opened_ms"])))
    nets: List[float] = []
    for t in trades:
        lo, hi = int(t["opened_ms"]), int(t["closed_ms"])
        basis = (-t["qty"] * (t["exit_perp"] - t["entry_perp"])
                 + t["spot_qty"] * (t["exit_spot"] - t["entry_spot"]))
        fees = sum(u for ms, u in fee_events if lo <= int(ms) <= hi)
        borrow = sum(u for ms, u in borrow_events if lo <= int(ms) <= hi)
        nets.append(float(t["funding"]) + basis - fees - borrow)
    total = sum(nets)
    peak = cum = worst = 0.0
    for n in nets:
        cum += n
        peak = max(peak, cum)
        worst = max(worst, peak - cum)
    ranked = sorted(nets, reverse=True)
    positive = total > 0
    return {
        "trades": len(nets),
        "trade_nets_usd": nets,
        "net_usd": total,
        "best_trade_usd": ranked[0] if ranked else None,
        "best_trade_share_of_net": (ranked[0] / total) if (ranked and positive) else None,
        "top5_share_of_net": (sum(ranked[:5]) / total) if (ranked and positive) else None,
        "losing_trade_pct": (100.0 * sum(1 for n in nets if n < 0) / len(nets)
                             if nets else None),
        "max_drawdown_usd": worst,
        "max_drawdown_pct_of_notional": 100.0 * worst / notional if notional else None,
        "reconciles_to_net": (None if net_usd is None
                              else abs(total - net_usd) < max(0.01, 1e-6 * abs(net_usd))),
    }


def return_on_total_capital(net_annualised_pct: float,
                            margin_fractions: Sequence[float]) -> Dict[float, float]:
    """Annualised net as a return on the money tied up: the spot leg (notional)
    plus an ASSUMED margin fraction of notional posted for the short perp. The
    fraction is the reader's declared assumption; the replay has no venue margin
    figure (it says so under not_modelled)."""
    out: Dict[float, float] = {}
    for m in margin_fractions:
        if not (isinstance(m, (int, float)) and math.isfinite(m) and m >= 0):
            raise ValueError(f"margin fraction must be a finite number >= 0, got {m!r}")
        out[m] = net_annualised_pct / (1.0 + m)
    return out


def capital_lines(net_annualised_pct: float,
                  margin_fractions: Sequence[float] = (0.10, 0.20, 1.0 / 3.0)
                  ) -> List[str]:
    shown = return_on_total_capital(net_annualised_pct, margin_fractions)
    parts = [f"{100.0 * m:.0f}% margin -> {v:+.2f}%/yr" for m, v in shown.items()]
    return ["  on total capital (spot + assumed perp margin, not the venue's "
            "figure): " + "; ".join(parts)]


def concentration_lines(c: Dict[str, Any], *, years: float,
                        notional: float, borrow_apr: float = 0.0) -> List[str]:
    """The printed form of :func:`trade_concentration`. Empty when there is
    nothing to say. The 'without the best trade' figure is the point: a headline
    one trade supplied is a different claim from one spread across many."""
    if not c.get("trades"):
        return []
    out: List[str] = []
    if c.get("best_trade_share_of_net") is not None:
        nets = sorted(c["trade_nets_usd"], reverse=True)
        without = c["net_usd"] - nets[0]
        out.append(
            f"  best trade ${c['best_trade_usd']:,.0f} = "
            f"{100.0 * c['best_trade_share_of_net']:.1f}% of net; best five = "
            f"{100.0 * c['top5_share_of_net']:.1f}%")
        if years > 0 and notional:
            charged = (f"after {100.0 * borrow_apr:g}% borrow" if borrow_apr
                       else "at 0% borrow charged")
            out.append(
                f"  without the best trade ${without:,.0f} = "
                f"{100.0 * without / notional / years:+.2f}%/yr on notional "
                f"({charged}; fees included)")
    out.append(
        f"  {c['losing_trade_pct']:.0f}% of trades lose money; max drawdown of "
        f"closed trades ${c['max_drawdown_usd']:,.0f} "
        f"({c['max_drawdown_pct_of_notional']:.2f}% of notional)")
    if c.get("reconciles_to_net") is False:
        out.append("  WARNING: per-trade nets do NOT add back to the headline net")
    return out


def replay(rows: Sequence[Any], *, notional: float, borrow_apr: float,
           execution_mode: str = "overlay", execution_style: str = "taker",
           impact_bps: float = 0.0, margin_multiple: float = INERT_MARGIN,
           max_entries_per_day: Optional[int] = None,
           risk_class: Any = None,
           halt_after: Optional[int] = None,
           warm_prints: Sequence[Any] = (),
           trace: Optional[List[Dict[str, Any]]] = None,
           liquidation: Optional[LiquidationInputs] = None,
           require_liquidation_evidence: bool = False,
           opportunity_cost_apr: float = 0.0) -> Dict[str, Any]:
    """Drive the real engine through `rows`. One bar, one `main.tick`.

    ``opportunity_cost_apr`` (default 0.0) is handed to the engine's
    constructor, so its entry gate charges it exactly as production would, and
    is accrued per held print on the hedged quantity at the bar's spot, as
    borrow is. It is not cash: it never enters the journal or ``net_usd``, and
    is reported as ``opportunity_cost_usd`` / ``net_after_opportunity_cost_usd``.

    ``warm_prints`` are SETTLED prints (``Settlement`` rows) that happened
    before ``rows[0]``; they seed the engine through ``warm_funding_history``,
    the same call production makes at start-up, and nothing else: no order is
    possible on them. ``trace``, if given, receives one dict per walked bar -
    ``{ms, action, reason, state}`` - the engine's own decision, for tests that
    prove a decision does not depend on later bars.

    ``liquidation`` is explicit liquidation evidence (see `LiquidationInputs`).
    Given, the engine is built exactly as production builds it
    (``require_liquidation_check=True``) and the gate runs on the interval's
    adverse price. Absent, the gate is NOT evaluated and the report says so
    (``liquidation.production_equivalent`` is False) - unless
    ``require_liquidation_evidence`` builds the engine as production does and
    lets it halt on the unreadable liquidation price, as it would live.
    """
    import ledger as L
    from carry_engine import BookState, CarryEngine
    from carry_risk import CarryRisk
    from market_snapshot import take_snapshot

    raw_broker = ReplayBroker(rows=rows, impact_bps=impact_bps,
                              margin_multiple=margin_multiple,
                              liquidation=liquidation)
    broker = raw_broker
    # The overlay hedges BTC the client already owns. Enough of it that the
    # inventory is never the binding constraint, so a divergence from the
    # simulator cannot be blamed on an empty wallet.
    raw_broker.inventory_btc = 10.0 * max(notional / rows[0].perp, 1.0)

    # THE LEDGER, wrapped around the venue rather than called by the engine.
    # Every leg the engine sends is booked because there is nowhere else for
    # it to go (0044). `_now` follows the bar the replay is on, so entries are
    # stamped with market time and the statement can be cut into periods.
    journal = L.Journal()
    _now = {"ms": int(rows[0].ms)}
    broker = L.LedgerBroker(raw_broker, journal, ms=lambda: _now["ms"])

    store = _Store()
    engine = CarryEngine(
        broker=broker, kill_switch=store.trip_kill_switch,
        max_notional_usd=notional, borrow_apr=borrow_apr,
        execution_mode=execution_mode, execution_style=execution_style,
        persist=lambda state: None,
        opportunity_cost_apr=opportunity_cost_apr,
        # main.build_bot sets this True. The replay sets it whenever it can
        # answer the gate (evidence supplied) or has been told to behave as
        # production does without evidence (it then halts). Left off only for
        # research runs, which the report labels.
        require_liquidation_check=(liquidation is not None
                                   or require_liquidation_evidence))
    risk_kwargs = {"store": store, "max_notional_usd": notional}
    if max_entries_per_day is not None:
        risk_kwargs["max_entries_per_day"] = max_entries_per_day
    engine.pair_risk = (risk_class or CarryRisk)(**risk_kwargs)

    if warm_prints:
        engine.warm_funding_history(
            [(float(w.rate) * 1e4, int(w.ms)) for w in warm_prints])

    trades: List[Dict[str, Any]] = []
    open_trade: Optional[Dict[str, Any]] = None
    actions: Dict[str, int] = {}
    reasons: Dict[str, int] = {}
    per_print_borrow = borrow_apr / (365.0 * cb.FUNDING_PERIODS_PER_DAY)
    per_print_opportunity = (float(opportunity_cost_apr)
                             / (365.0 * cb.FUNDING_PERIODS_PER_DAY))
    opportunity_usd = 0.0
    funding_usd = basis_usd = fees_usd = borrow_usd = 0.0
    fee_events: List[Any] = []        # (ms, usd) per tick, for per-trade fees
    borrow_events: List[Any] = []     # (ms, usd) per tick, for per-trade borrow
    last_collected = 0.0
    halted_at: Optional[int] = None

    walked = 0
    for index, row in enumerate(rows):
        raw_broker.i = index
        raw_broker.forced_perp_price = None     # a breach is per interval
        _now["ms"] = int(row.ms)
        walked = index + 1
        if halt_after is not None and index == halt_after:
            # A deliberate halt, to prove the replay stops where the process
            # would stop rather than walking past a HALTED book.
            engine._halt("REPLAY_FORCED_HALT", "requested by the harness")
        now_s = row.ms / 1000.0
        before = len(raw_broker.fills)

        # EXACTLY main.tick: one observation, freshness asserted, then the
        # engine. The clock is patched so `age_s` is measured against replay
        # time; without it every snapshot is years old and `assert_fresh`
        # refuses the whole corpus — which is itself worth knowing.
        with mock.patch("market_snapshot.time.time", lambda: now_s):
            try:
                # main.tick wraps BOTH in one try: a view that cannot be built
                # (an unreadable margin, say) skips the tick; it never crashes.
                view = take_snapshot(broker, perp_symbol="BTCUSDT",
                                     spot_symbol="BTCUSDT", now_s=now_s)
                view.assert_fresh()
            except Exception as exc:                        # noqa: BLE001
                reasons["SNAPSHOT_REFUSED"] = \
                    reasons.get("SNAPSHOT_REFUSED", 0) + 1
                reasons[f"stale:{type(exc).__name__}"] = \
                    reasons.get(f"stale:{type(exc).__name__}", 0) + 1
                continue
            engine.snapshot = view
            decision = engine.on_candle(
                mark=view.perp_mark, funding_bps=view.funding_print_bps,
                spot=view.spot_mark, timestamp_ms=int(row.ms),
                funding_print_ms=view.funding_print_ms)

        if trace is not None:
            trace.append({"ms": int(row.ms), "action": decision.action,
                          "reason": decision.reason,
                          "state": decision.state.name})
        actions[decision.action] = actions.get(decision.action, 0) + 1
        if decision.reason:
            reasons[decision.reason] = reasons.get(decision.reason, 0) + 1

        tick_fills = raw_broker.fills[before:]
        for fill in tick_fills:
            fees_usd += fill.fee
        if tick_fills:
            fee_events.append((int(row.ms), sum(f.fee for f in tick_fills)))
        # THE PRICE THE VENUE ACTUALLY GAVE US, not the bar's close. Taking
        # the exit from the bar drops the impact haircut on the closing leg —
        # exactly half the total impact, which is how this harness first
        # reported a book $882 cheaper to run than it is.
        exit_perp = next((f.price for f in reversed(tick_fills)
                          if f.product == "linear"), None)
        exit_spot = next((f.price for f in reversed(tick_fills)
                          if f.product == "spot"), None)

        position = engine.position
        if position is not None:
            last_collected = position.funding_collected
        if position is not None and open_trade is None:
            open_trade = {"opened_ms": int(row.ms),
                          "qty": position.perp.filled_qty,
                          "spot_qty": position.spot_held_qty,
                          "spot_entry_qty": position.spot.filled_qty,
                          "entry_perp": position.perp.avg_price,
                          "entry_spot": position.spot.avg_price,
                          "entry_index": index}
        if position is not None:
            # FUNDING IS THE ENGINE'S OWN NUMBER, not a recomputation.
            # `on_candle` books every held print into `funding_collected`
            # (0034), signed, and it books BEFORE it decides to open — so a
            # position does not collect the print it entered on, which is
            # right: the venue pays whoever held the position at the
            # settlement stamp. Recomputing it here booked that print anyway
            # and made the replay $891 richer than the truth over 79 trades.
            borrow_usd += per_print_borrow * position.perp.filled_qty * row.spot
            borrow_events.append((int(row.ms), per_print_borrow * position.perp.filled_qty * row.spot))
            opportunity_usd += (per_print_opportunity
                                * position.perp.filled_qty * row.spot)
        if position is None and open_trade is not None:
            # THE ENGINE'S OWN NUMBER, taken from the decision that closed the
            # position. Reading `funding_collected` off the position after the
            # tick misses the print booked DURING it — and the print a book
            # exits on is negative by construction (three of them is why it
            # exits), so that miss flattered the replay by $392 over 79 trades.
            collected = float((decision.detail or {}).get("collected",
                                                          last_collected))
            # The funding the engine booked, into the journal, once per
            # closed position. Signed. The engine is the source; the ledger
            # records what it reports rather than recomputing it (0043).
            if collected:
                L.funding(journal, ms=int(row.ms), ref=f"fund:{row.ms}",
                          usd=collected)
            # And the hedged coin, marked from entry to exit, so the journal
            # shows BOTH sides of a delta-neutral pair. Without it the short's
            # loss stands alone and a flat book reads as a disaster.
            #
            # OVERLAY ONLY, and the guard is the whole point. `revalue_inventory`
            # is overlay-scoped by its own docstring: it exists because the book
            # never TRADES the client's coin, so nothing else books that leg's
            # move. In ACQUIRE the book buys the spot and sells it again, and
            # `spot_sell` realises the move against what it cost — so posting a
            # revaluation as well counts the same dollars twice.
            #
            # This was invisible because it was a COMPENSATING PAIR. While
            # `LedgerBroker` realised every spot sale against its own sale price
            # (D44a), spot realisation was identically zero and the revaluation
            # was the only thing booking that leg at all — so revaluing in both
            # modes was, by accident, right. Fixing the cost basis turned the
            # cancellation into a visible double-count: exactly $151,904.02 of
            # it, which is the acquire revaluation total to the cent.
            if execution_mode == cb.OVERLAY:
                L.revalue_inventory(
                    journal, ms=int(row.ms), ref=f"reval:{row.ms}",
                    qty=open_trade["qty"], from_price=open_trade["entry_spot"],
                    to_price=(exit_spot if exit_spot is not None else row.spot))
            open_trade.update({"closed_ms": int(row.ms),
                               "exit_perp": (exit_perp if exit_perp is not None
                                             else row.perp),
                               "exit_spot": (exit_spot if exit_spot is not None
                                             else row.spot),
                               "reason": decision.reason,
                               "funding": collected,
                               "prints_held": index - open_trade["entry_index"]})
            funding_usd += collected
            # BOTH LEGS, IN BOTH MODES. The overlay never TRADES the client's
            # spot, but the spot is still what the short is hedging: the pair
            # is delta-neutral either way and its value is the pair's value.
            # Dropping the long leg here — which this harness did on its first
            # run — turns a hedged book into a naked short and prints
            # -32.76%/yr for a position that is flat to price. What the mode
            # changes is which legs pay FEES, and that is handled by the
            # engine, which places one order in overlay and two in acquire.
            perp_leg = -open_trade["qty"] * (open_trade["exit_perp"]
                                             - open_trade["entry_perp"])
            spot_leg = open_trade["spot_qty"] * (open_trade["exit_spot"]
                                                 - open_trade["entry_spot"])
            basis_usd += perp_leg + spot_leg
            trades.append(open_trade)
            open_trade = None

        if engine.state is BookState.HALTED:
            halted_at = int(row.ms)
            break

    if open_trade is not None:
        last = rows[min(raw_broker.i, len(rows) - 1)]
        funding_usd += last_collected
        # FUNDING on the position that never closed IS realised: it was paid
        # in cash at every settlement it was held through, and the journal
        # holds cash.
        #
        # Its MARK is not. Revaluing the coin without marking the short beside
        # it books a $21,207 gain on a position that is flat to price — which
        # this harness did, once. Both legs of an OPEN position are estimates,
        # they are reported as `unrealised_usd` below, and they never enter
        # the journal. A ledger holds what is not an estimate.
        if last_collected:
            L.funding(journal, ms=int(last.ms), ref=f"fund:{last.ms}",
                      usd=last_collected)
        unrealised_usd = (
            open_trade["qty"] * (open_trade["entry_perp"] - last.perp)
            + open_trade["spot_qty"] * (last.spot - open_trade["entry_spot"]))
        open_trade.update({"closed_ms": int(last.ms), "exit_perp": last.perp,
                           "exit_spot": last.spot, "reason": "OPEN_AT_END",
                           "open_at_end": True, "funding": last_collected,
                           "prints_held": raw_broker.i - open_trade["entry_index"]})
        basis_usd += (-open_trade["qty"] * (open_trade["exit_perp"]
                                            - open_trade["entry_perp"])
                      + open_trade["spot_qty"] * (open_trade["exit_spot"]
                                                  - open_trade["entry_spot"]))
        trades.append(open_trade)

    unrealised_usd = locals().get("unrealised_usd", 0.0)
    if borrow_usd:
        L.borrow(journal, ms=int(rows[min(raw_broker.i, len(rows) - 1)].ms),
                 ref="borrow:total", usd=borrow_usd)

    statement = journal.statement()
    span_days = max((rows[-1].ms - rows[0].ms) / float(DAY_MS), 1.0)
    years = span_days / 365.25
    net = funding_usd + basis_usd - fees_usd - borrow_usd
    held = sum(t["prints_held"] for t in trades) / cb.FUNDING_PERIODS_PER_DAY
    liquidation_report = _liquidation_report(
        liquidation, require_liquidation_evidence, raw_broker.liq_stats,
        engine.require_liquidation_check)
    return {
        "driver": "CarryEngine (the trading class), via main.tick's sequence",
        "window": f"{cb._iso_ms(rows[0].ms)} .. {cb._iso_ms(rows[-1].ms)}",
        "settlements": len(rows), "settlements_walked": walked,
        "years": years,
        "notional_usd": notional, "borrow_apr": borrow_apr,
        "opportunity_cost_apr": float(opportunity_cost_apr),
        "opportunity_cost_usd": -opportunity_usd,
        "net_after_opportunity_cost_usd": net - opportunity_usd,
        "execution_mode": execution_mode, "execution_style": execution_style,
        "impact_bps_per_leg": impact_bps,
        "margin_multiple": margin_multiple,
        "liquidation": liquidation_report,
        "trades": len(trades),
        "orders": len(raw_broker.fills),
        "maker_fills": sum(1 for f in raw_broker.fills if f.maker),
        "funding_usd": funding_usd, "basis_usd": basis_usd,
        "fees_usd": -fees_usd, "borrow_usd": -borrow_usd,
        "net_usd": net,
        "net_annualised_pct": 100.0 * net / notional / years,
        "market_exposure_pct": 100.0 * held / span_days,
        "actions": actions, "reasons": reasons,
        # THE JOURNAL'S OWN ANSWER, built from balanced double-entry postings
        # and never from the running totals above. When the two agree the
        # P&L is not asserted, it is evidenced.
        "ledger": statement,
        # THE TWO LEGS, SEPARATELY. A delta-neutral overlay claims they
        # cancel; `ledger.hedge_effectiveness` reads them back out and names
        # what is left, which is the basis.
        "hedge": {
            "perp_realised_usd": sum(
                l.usd for e in journal.entries if e.kind == "perp_close"
                for l in e.lines if l.account == L.REALISED) * -1.0,
            "inventory_change_usd": sum(
                l.usd for e in journal.entries if e.kind == "revalue"
                for l in e.lines if l.account == L.REALISED) * -1.0,
        },
        "ledger_entries": len(journal.entries),
        "ledger_net_usd": statement["net_usd"],
        # THE CROSS-CHECK. The journal holds realised money only, so the
        # replay's net — which marks the position still open at the window
        # end — must equal the journal's net plus that mark and nothing else.
        "unrealised_usd": unrealised_usd,
        "ledger_agrees": abs(
            statement["net_usd"] + unrealised_usd - net) < max(
                0.01, 1e-6 * abs(net)),
        "halted_at_ms": halted_at,
        "trade_rows": trades,
        "concentration": trade_concentration(
            trades, notional=notional, fee_events=fee_events,
            borrow_events=borrow_events, net_usd=net),
        "not_modelled": [
            "latency: a fill happens at the price the decision was made on",
            "fill probability for post-only orders (every rest fills)",
            "the ticker's funding forecast (the settled print is used)",
            "positionIM/positionMM: a declared constant, not the venue's "
            "number (INVENTORY F4)",
            "partial fills, rate limits, venue rejections",
        ] + ([] if liquidation is not None else [
            "the production liquidation-distance gate: the corpus holds no "
            "liqPrice, leverage, collateral or accountMMRate"]),
    }


def _liquidation_report(liquidation: Optional[LiquidationInputs],
                        required: bool, stats: Dict[str, Any],
                        engine_flag: bool) -> Dict[str, Any]:
    """What the replay did about the liquidation gate, and whether its P&L may
    be called production-equivalent. `production_equivalent` means only that
    no production RISK gate was skipped or replaced by an assumption; the
    execution-realism limits in `not_modelled` apply regardless."""
    why_not: List[str] = []
    if liquidation is None:
        gate = "REQUIRED_EVIDENCE_MISSING" if required else "NOT_EVALUATED"
        why_not.append(
            "no liquidation evidence: the production liquidation-distance "
            "gate (liqPrice, accountMMRate) "
            + ("halts the engine on an unreadable liquidation price, as it "
               "would live" if required else "was not evaluated"))
    else:
        gate = "EVALUATED"
        if liquidation.kind != "recorded":
            why_not.append(f"liquidation inputs are {liquidation.kind} "
                           "(a scenario), not recorded venue observations")
        if liquidation.account_mm_rate is None:
            why_not.append("accountMMRate was not supplied: the account-level "
                           "margin-rate floor was not evaluated")
        if liquidation.margin_multiple is None:
            why_not.append("margin_multiple is a declared constant, not the "
                           "venue's number (INVENTORY F4)")
    return {
        "gate": gate,
        "production_equivalent": not why_not,
        "why_not_production_equivalent": why_not,
        "engine_require_liquidation_check": bool(engine_flag),
        "evidence_kind": None if liquidation is None else liquidation.kind,
        "evidence_source": None if liquidation is None else liquidation.source,
        "floor_pct": _min_liquidation_distance_pct(),
        **stats,
    }


def diff(engine_report: Dict[str, Any],
         sim_report: Dict[str, Any]) -> List[str]:
    """Every place the engine and the simulator disagree, in plain words."""
    out: List[str] = []
    e_trades, s_trades = engine_report["trades"], sim_report["trades"]
    if e_trades != s_trades:
        out.append(
            f"TRADES: the engine opened {e_trades}, the simulator "
            f"{s_trades}. Every %/yr this repo has published came from the "
            "simulator's number.")
    e_net, s_net = engine_report["net_usd"], sim_report["net_usd"]
    if abs(e_net - s_net) > 0.01:
        out.append(
            f"NET: engine ${e_net:,.2f} vs simulator ${s_net:,.2f} "
            f"({100.0 * (e_net - s_net) / abs(s_net or 1):+.1f}%)")
    e_exp = engine_report["market_exposure_pct"]
    s_exp = sim_report.get("market_exposure_pct", 0.0)
    if abs(e_exp - s_exp) > 0.5:
        out.append(f"EXPOSURE: engine {e_exp:.1f}% of the window in the "
                   f"market, simulator {s_exp:.1f}%")
    blockers = {k: v for k, v in engine_report["reasons"].items()
                if k not in ("PAIR_LANDED", "PERP_HEDGE_LANDED", "")}
    if blockers:
        out.append("THE ENGINE REFUSED FOR REASONS THE SIMULATOR HAS NO "
                   "CONCEPT OF: " + ", ".join(
                       f"{k} x{v}" for k, v in sorted(
                           blockers.items(), key=lambda kv: -kv[1])[:8]))
    return out


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", default=ROOT)
    ap.add_argument("--notional", type=float, default=100_000.0)
    ap.add_argument("--borrow-apr", type=float, default=0.0)
    ap.add_argument("--opportunity-cost-apr", type=float, default=0.0,
                    help="the forgone return on the hedged capital, per year, "
                         "charged by the entry gate beside borrow (an "
                         "operator statement; the repo holds no rate)")
    ap.add_argument("--mode", choices=("acquire", "overlay"),
                    default="overlay")
    ap.add_argument("--style", choices=("taker", "maker_first"),
                    default="taker")
    ap.add_argument("--no-impact", action="store_true")
    ap.add_argument("--entries-per-day", type=int, default=None,
                    help="override CarryRisk's cap, to isolate its effect")
    ap.add_argument("--diff", action="store_true",
                    help="run carry_backtest on the same rows and compare")
    ap.add_argument("--bars", default="",
                    help="a settlement series to replay instead of the "
                         "repo corpus (CSV or JSON; see load_bars)")
    ap.add_argument("--require-liquidation-evidence", action="store_true",
                    help="build the engine exactly as production does "
                         "(liquidation gate ON) and let it halt, as it would "
                         "live, because the corpus holds no liquidation price")
    ap.add_argument("--declare-isolated-leverage", type=float, default=None,
                    metavar="N",
                    help="a DECLARED scenario, not a measurement and not the "
                         "book's leverage: an isolated short at N x, "
                         "liquidation = entry x (1 + 1/N), initial margin "
                         "only. Runs the production gate on the interval's "
                         "adverse high")
    ap.add_argument("--recorded-liquidation-db", default="", metavar="DB",
                    help="RECORDED evidence: replay on the liquidation reads "
                         "the production gate saved to this state database "
                         "(opened read-only). Use with --bars covering the "
                         "recorded window; a bar with no observation at or "
                         "before it is refused, not defaulted")
    ap.add_argument("--json", default="")
    args = ap.parse_args(argv)

    if args.bars:
        rows = load_bars(args.bars)
        meta = {"corpus": args.bars, "label": f"supplied file {args.bars}",
                "same_venue": None}
    else:
        rows, meta = cb.load_bybit_settlements(args.repo)
    impact = (0.0 if args.no_impact
              else cb.impact_bps_per_leg(args.repo, args.notional)[0])
    evidence = None
    if args.recorded_liquidation_db and \
            args.declare_isolated_leverage is not None:
        ap.error("recorded and declared liquidation evidence are exclusive")
    if args.recorded_liquidation_db:
        evidence = recorded_liquidation_from_db(args.recorded_liquidation_db)
    if args.declare_isolated_leverage is not None:
        evidence = LiquidationInputs.declared_isolated(
            args.declare_isolated_leverage,
            f"--declare-isolated-leverage {args.declare_isolated_leverage:g} "
            "(operator-declared scenario)")
    r = replay(rows, notional=args.notional, borrow_apr=args.borrow_apr,
               execution_mode=args.mode, execution_style=args.style,
               impact_bps=impact, max_entries_per_day=args.entries_per_day,
               liquidation=evidence,
               require_liquidation_evidence=args.require_liquidation_evidence,
               opportunity_cost_apr=args.opportunity_cost_apr)

    print("=" * 74)
    print("CARRY REPLAY — the ENGINE driven through history, not a model of it")
    print("=" * 74)
    print(f"  driver     {r['driver']}")
    print(f"  corpus     {meta['corpus']}")
    print(f"  window     {r['window']}  ({r['settlements']} prints, "
          f"{r['years']:.2f} yr)")
    print(f"  book       {r['execution_mode'].upper()}/"
          f"{r['execution_style']}, ${r['notional_usd']:,.0f}, borrow "
          f"{r['borrow_apr'] * 100:.1f}%/yr, impact "
          f"{r['impact_bps_per_leg']:.2f} bps/leg")
    print()
    liq = r["liquidation"]
    if liq["production_equivalent"]:
        print("  LIQUIDATION GATE  evaluated on recorded evidence "
              f"({liq['evidence_source']})")
    else:
        print("  !! NOT PRODUCTION-EQUIVALENT — RESEARCH OUTPUT ONLY !!")
        print(f"  liquidation gate: {liq['gate']}")
        for why in liq["why_not_production_equivalent"]:
            print(f"    - {why}")
        if liq["gate"] == "EVALUATED":
            print(f"    evaluated on {liq['evidence_kind']} inputs "
                  f"({liq['evidence_source']}): {liq['ticks_evaluated']} "
                  f"held ticks, min distance "
                  f"{liq['min_distance_pct']:.1f}% vs floor "
                  f"{liq['floor_pct']:.0f}%, {liq['intrabar_trips']} breach(es) "
                  "visible only in the interval high")
    print()
    print(f"    funding    ${r['funding_usd']:12,.2f}")
    print(f"    basis      ${r['basis_usd']:12,.2f}")
    print(f"    fees       ${r['fees_usd']:12,.2f}")
    print(f"    borrow     ${r['borrow_usd']:12,.2f}")
    print(f"    = NET      ${r['net_usd']:12,.2f}   "
          f"{r['net_annualised_pct']:+.2f}%/yr")
    print(f"  trades {r['trades']}, orders {r['orders']} "
          f"({r['maker_fills']} maker), in market "
          f"{r['market_exposure_pct']:.1f}%")
    for line in capital_lines(r["net_annualised_pct"]):
        print(line)
    for line in concentration_lines(r["concentration"], years=r["years"],
                                    notional=r["notional_usd"],
                                    borrow_apr=r["borrow_apr"]):
        print(line)
    if r["halted_at_ms"]:
        print(f"  HALTED at {cb._iso_ms(r['halted_at_ms'])} — the replay "
              "stopped there, as the process would")
    import ledger as _L
    st = r["ledger"]
    print("\n  THE LEDGER — built from " f"{r['ledger_entries']:,} "
          "double-entry postings, each balanced to zero")
    print(f"    funding          ${st['funding_usd']:>12,.2f}")
    print(f"    fees             ${-st['fees_usd']:>12,.2f}")
    print(f"    borrow           ${-st['borrow_usd']:>12,.2f}")
    print(f"    realised         ${st['realised_usd']:>12,.2f}")
    print(f"    = NET realised   ${st['net_usd']:>12,.2f}")
    print(f"    + open position  ${r['unrealised_usd']:>12,.2f}   "
          "an ESTIMATE; deliberately not in the journal")
    print(f"    ------------------------------   agrees with the run above: "
          f"{r['ledger_agrees']}")
    if st["fees_unknown"]:
        print(f"    {st['fees_unknown']} fill(s) whose fee the venue never "
              "stated — counted, never booked as $0.00")
    eff = _L.hedge_effectiveness(**r["hedge"])
    print("\n  DID THE HEDGE HEDGE?")
    print(f"    perp realised    ${eff['perp_realised_usd']:>12,.0f}")
    print(f"    inventory moved  ${eff['inventory_change_usd']:>12,.0f}")
    print(f"    residual         ${eff['residual_usd']:>12,.0f}   "
          f"{eff['residual_fraction_of_leg']:+.3%} of a leg")
    print("    That residual IS the basis — the only price exposure a "
          "delta-neutral")
    print("    overlay runs. Not expected to be zero. Expected to be small, "
          "and now")
    print("    measured out of a journal rather than asserted.")

    print("\n  WHAT THE ENGINE DID, tick by tick")
    for action, count in sorted(r["actions"].items(), key=lambda kv: -kv[1]):
        print(f"    {action:<14}{count:>7}")
    print("\n  WHY IT REFUSED")
    for reason, count in sorted(r["reasons"].items(), key=lambda kv: -kv[1])[:12]:
        print(f"    {reason:<34}{count:>7}")

    if args.diff:
        sim = cb.simulate_settlements_series(
            rows, notional=args.notional, borrow_apr=args.borrow_apr,
            gated=True, impact_bps=impact, mode=args.mode,
            opportunity_cost_apr=args.opportunity_cost_apr)
        print("\n" + "=" * 74)
        print("DIFF — the engine against tools/carry_backtest.py")
        print("=" * 74)
        problems = diff(r, sim)
        if not problems:
            print("  no disagreement.")
        for line in problems:
            print(f"  * {line}")
        print("\n  carry_backtest prints `[x] rule_is_what_the_engine_runs`.")
        print("  That box is about the GATE, which really is shared. The state")
        print("  machine around it is not, and the state machine is what")
        print("  decides whether an order is sent.")

    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(r, fh, indent=2, default=str)
        print(f"\n  wrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
