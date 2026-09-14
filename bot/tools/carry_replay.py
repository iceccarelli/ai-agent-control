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
`margin_multiple` is `positionIM / positionMM`, and INVENTORY F4 says that
field is a risk-tier ratio rather than liquidation headroom. There is no honest
way to reconstruct it offline, so the default is a CONSTANT that never trips
the floor, and the report says so. `--margin-model=collateral` supplies a
declared alternative — what the ratio would be if the short's loss were carried
by the spot leg — and the difference between the two runs is the size of the
question F4 leaves open. Neither is the venue's number.

The ticker's funding FORECAST is not modelled either: `get_funding_bps` returns
the last SETTLED print, because the alternative offline is to hand the engine
the next print's rate, which is the future.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence
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

    # -- what the snapshot reads -----------------------------------------
    @property
    def bar(self):
        return self.rows[self.i]

    def get_mark(self, symbol: str) -> float:
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
        return float(self.margin_multiple)

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

    def place_market(self, *, symbol, side, qty, product):
        if qty <= 0 or not math.isfinite(qty):
            return None
        base = self.bar.perp if product == "linear" else self.bar.spot
        price = self._price(side, base)
        rate = (self.taker_perp_bps if product == "linear"
                else self.taker_spot_bps)
        fee = qty * price * rate / 1e4
        self.fills.append(Fill(int(self.bar.ms), side, product, qty, price,
                               fee))
        if product == "linear":
            self.perp_qty += qty if side == "Sell" else -qty
        return {"filled_qty": qty, "avg_price": price,
                "order_link_id": f"r{len(self.fills)}", "fee": fee}

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
        return {"filled_qty": qty, "avg_price": price,
                "order_link_id": f"m{len(self.fills)}", "fee": fee,
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


def replay(rows: Sequence[Any], *, notional: float, borrow_apr: float,
           execution_mode: str = "overlay", execution_style: str = "taker",
           impact_bps: float = 0.0, margin_multiple: float = INERT_MARGIN,
           max_entries_per_day: Optional[int] = None,
           risk_class: Any = None,
           halt_after: Optional[int] = None) -> Dict[str, Any]:
    """Drive the real engine through `rows`. One bar, one `main.tick`."""
    from carry_engine import BookState, CarryEngine
    from carry_risk import CarryRisk
    from market_snapshot import take_snapshot

    broker = ReplayBroker(rows=rows, impact_bps=impact_bps,
                          margin_multiple=margin_multiple)
    # The overlay hedges BTC the client already owns. Enough of it that the
    # inventory is never the binding constraint, so a divergence from the
    # simulator cannot be blamed on an empty wallet.
    broker.inventory_btc = 10.0 * max(notional / rows[0].perp, 1.0)

    store = _Store()
    engine = CarryEngine(
        broker=broker, kill_switch=store.trip_kill_switch,
        max_notional_usd=notional, borrow_apr=borrow_apr,
        execution_mode=execution_mode, execution_style=execution_style,
        persist=lambda state: None)
    risk_kwargs = {"store": store, "max_notional_usd": notional}
    if max_entries_per_day is not None:
        risk_kwargs["max_entries_per_day"] = max_entries_per_day
    engine.pair_risk = (risk_class or CarryRisk)(**risk_kwargs)

    trades: List[Dict[str, Any]] = []
    open_trade: Optional[Dict[str, Any]] = None
    actions: Dict[str, int] = {}
    reasons: Dict[str, int] = {}
    per_print_borrow = borrow_apr / (365.0 * cb.FUNDING_PERIODS_PER_DAY)
    funding_usd = basis_usd = fees_usd = borrow_usd = 0.0
    last_collected = 0.0
    halted_at: Optional[int] = None

    walked = 0
    for index, row in enumerate(rows):
        broker.i = index
        walked = index + 1
        if halt_after is not None and index == halt_after:
            # A deliberate halt, to prove the replay stops where the process
            # would stop rather than walking past a HALTED book.
            engine._halt("REPLAY_FORCED_HALT", "requested by the harness")
        now_s = row.ms / 1000.0
        before = len(broker.fills)

        # EXACTLY main.tick: one observation, freshness asserted, then the
        # engine. The clock is patched so `age_s` is measured against replay
        # time; without it every snapshot is years old and `assert_fresh`
        # refuses the whole corpus — which is itself worth knowing.
        with mock.patch("market_snapshot.time.time", lambda: now_s):
            view = take_snapshot(broker, perp_symbol="BTCUSDT",
                                 spot_symbol="BTCUSDT", now_s=now_s)
            try:
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

        actions[decision.action] = actions.get(decision.action, 0) + 1
        if decision.reason:
            reasons[decision.reason] = reasons.get(decision.reason, 0) + 1

        tick_fills = broker.fills[before:]
        for fill in tick_fills:
            fees_usd += fill.fee
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
            open_trade = {"opened_ms": int(row.ms), "qty": position.perp.filled_qty,
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
        if position is None and open_trade is not None:
            # THE ENGINE'S OWN NUMBER, taken from the decision that closed the
            # position. Reading `funding_collected` off the position after the
            # tick misses the print booked DURING it — and the print a book
            # exits on is negative by construction (three of them is why it
            # exits), so that miss flattered the replay by $392 over 79 trades.
            collected = float((decision.detail or {}).get("collected",
                                                          last_collected))
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
            spot_leg = open_trade["qty"] * (open_trade["exit_spot"]
                                            - open_trade["entry_spot"])
            basis_usd += perp_leg + spot_leg
            trades.append(open_trade)
            open_trade = None

        if engine.state is BookState.HALTED:
            halted_at = int(row.ms)
            break

    if open_trade is not None:
        last = rows[min(broker.i, len(rows) - 1)]
        funding_usd += last_collected
        open_trade.update({"closed_ms": int(last.ms), "exit_perp": last.perp,
                           "exit_spot": last.spot, "reason": "OPEN_AT_END",
                           "open_at_end": True, "funding": last_collected,
                           "prints_held": broker.i - open_trade["entry_index"]})
        basis_usd += (-open_trade["qty"] * (open_trade["exit_perp"]
                                            - open_trade["entry_perp"])
                      + open_trade["qty"] * (open_trade["exit_spot"]
                                             - open_trade["entry_spot"]))
        trades.append(open_trade)

    span_days = max((rows[-1].ms - rows[0].ms) / float(DAY_MS), 1.0)
    years = span_days / 365.25
    net = funding_usd + basis_usd - fees_usd - borrow_usd
    held = sum(t["prints_held"] for t in trades) / cb.FUNDING_PERIODS_PER_DAY
    return {
        "driver": "CarryEngine (the trading class), via main.tick's sequence",
        "window": f"{cb._iso_ms(rows[0].ms)} .. {cb._iso_ms(rows[-1].ms)}",
        "settlements": len(rows), "settlements_walked": walked,
        "years": years,
        "notional_usd": notional, "borrow_apr": borrow_apr,
        "execution_mode": execution_mode, "execution_style": execution_style,
        "impact_bps_per_leg": impact_bps,
        "margin_multiple": margin_multiple,
        "trades": len(trades),
        "orders": len(broker.fills),
        "maker_fills": sum(1 for f in broker.fills if f.maker),
        "funding_usd": funding_usd, "basis_usd": basis_usd,
        "fees_usd": -fees_usd, "borrow_usd": -borrow_usd,
        "net_usd": net,
        "net_annualised_pct": 100.0 * net / notional / years,
        "market_exposure_pct": 100.0 * held / span_days,
        "actions": actions, "reasons": reasons,
        "halted_at_ms": halted_at,
        "trade_rows": trades,
        "not_modelled": [
            "latency: a fill happens at the price the decision was made on",
            "fill probability for post-only orders (every rest fills)",
            "the ticker's funding forecast (the settled print is used)",
            "positionIM/positionMM: a declared constant, not the venue's "
            "number (INVENTORY F4)",
            "partial fills, rate limits, venue rejections",
        ],
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
    r = replay(rows, notional=args.notional, borrow_apr=args.borrow_apr,
               execution_mode=args.mode, execution_style=args.style,
               impact_bps=impact, max_entries_per_day=args.entries_per_day)

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
    print(f"    funding    ${r['funding_usd']:12,.2f}")
    print(f"    basis      ${r['basis_usd']:12,.2f}")
    print(f"    fees       ${r['fees_usd']:12,.2f}")
    print(f"    borrow     ${r['borrow_usd']:12,.2f}")
    print(f"    = NET      ${r['net_usd']:12,.2f}   "
          f"{r['net_annualised_pct']:+.2f}%/yr")
    print(f"  trades {r['trades']}, orders {r['orders']} "
          f"({r['maker_fills']} maker), in market "
          f"{r['market_exposure_pct']:.1f}%")
    if r["halted_at_ms"]:
        print(f"  HALTED at {cb._iso_ms(r['halted_at_ms'])} — the replay "
              "stopped there, as the process would")
    print("\n  WHAT THE ENGINE DID, tick by tick")
    for action, count in sorted(r["actions"].items(), key=lambda kv: -kv[1]):
        print(f"    {action:<14}{count:>7}")
    print("\n  WHY IT REFUSED")
    for reason, count in sorted(r["reasons"].items(), key=lambda kv: -kv[1])[:12]:
        print(f"    {reason:<34}{count:>7}")

    if args.diff:
        sim = cb.simulate_settlements_series(
            rows, notional=args.notional, borrow_apr=args.borrow_apr,
            gated=True, impact_bps=impact, mode=args.mode)
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
