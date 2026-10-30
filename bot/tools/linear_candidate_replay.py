#!/usr/bin/env python3
"""Re-price `funding_carry_fade_btc_v1` through the LINEAR venue simulator.

WHAT THIS IS
============
Every committed number for this candidate (slice 57's OOS +0.1736 R, slice
58's shadow replay, the forward-shadow artefact's four trades) comes from
`skill_test.barrier_r_for_all_bars`: an abstract barrier model that charges a
flat 25 bps round trip and subtracts the funding-rate sum over the hold. It is
not a venue: no position, no margin, no lot size, no order, no stop that has to
be written and read back.

This tool takes the SAME schedule -- the cleared rule's own entries, one per
contiguous run (`skill_test.simulate_schedule` over the tradable mask the
Stage-1 run used), at most one open position (the shadow cap) -- and executes
it as orders against `backtest.LinearSimulatedExchange`:

* market entry at the decision bar's close (taker fee + slippage + half spread),
  quantity floored to the venue's 0.001 BTC step at the requested notional;
* protective stop written to the POSITION (`/v5/position/trading-stop`) at
  ``ref +- STOP_ATR * ATR`` and verified by reading it back from
  `/v5/position/list`; a stop that cannot be written closes the position at
  market and halts further entries (bracket failure -> close + kill switch);
* take-profit as a reduce-only resting limit at ``ref -+ TAKE_PROFIT_ATR * ATR``
  (maker fee, fills only when the bar trades THROUGH it);
* time stop: reduce-only market close at the close of ``signal + 1 + HORIZON``;
* funding at EVERY 8h settlement from the real series; margin, liquidation,
  mark-to-market equity, and a book-vs-venue reconciliation every bar that
  RAISES on a mismatch.

WHAT IT IS NOT
==============
NOT INDEPENDENT CONFIRMATION. The late window [t_mid, t1] is the exact history
slice 57 scored (and slice 55's full-sample run had already read), and the
forward window is the history the forward-shadow artefact scored. Re-pricing
the same bars under different cost arithmetic says how much the cost model
moves the result; it says nothing new about whether the rule has an edge.

No constant can be changed from here. FUND_ABS, the stop, the target, the
horizon, the ATR period and the schedule are read from the frozen module. The
only arguments are COSTS and SIZING, and the two costs the repository has no
measurement for (slippage, spread) are REQUIRED: this tool does not default
them.

It does not install the data-read ledger (`tools/reserved_holdout.py`): that
would rewrite the committed `artifacts/data_read_ledger.json`. The bytes read
here are the same corpus files the slice 57/58 tools already recorded.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import sys
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (REPO, os.path.join(REPO, "tools")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import numpy as np  # noqa: E402

import backtest as bt  # noqa: E402
from signals import funding_carry_fade_btc_v1 as fb  # noqa: E402

SYMBOL = "BTCUSDT"

#: Bybit linear BTCUSDT lot and tick (carry_broker.py: "Linear BTCUSDT has
#: qtyStep 0.001 and minOrderQty 0.001"; carry_engine.py: "touch is 0.1 USDT").
QTY_STEP = 0.001
MIN_QTY = 0.001
TICK = 0.1
FILTERS = {SYMBOL: {"tickSize": "0.1", "basePrecision": "0.001",
                    "minOrderQty": "0.001", "minOrderAmt": "0"}}

LABEL = ("NOT INDEPENDENT CONFIRMATION: re-prices history already scored by "
         "slice 55/57 (late window) and by the forward-shadow artefact "
         "(forward window). It measures the cost model, not the edge.")


def iso(ms: int) -> str:
    return dt.datetime.fromtimestamp(
        ms / 1000.0, tz=dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# the schedule: the cleared rule's, built exactly as shadow_replay builds it
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Candidate:
    index: int                 # decision (signal) bar
    direction: str             # LONG_SETUP | SHORT_SETUP
    segment: str               # "late_oos" | "forward"
    barrier_net_r: float       # the committed model's funding-adjusted net R
    barrier_bars_used: int


def build_schedule(bars: Sequence[bt.Bar], funding, folds: Mapping,
                   *, warmup: int = 200) -> List[Candidate]:
    import edge_measurement as em
    import skill_test as sk

    flags, directions = fb.flags_and_directions(bars, funding, warmup=warmup)
    flags = fb.restrict_flags_to_late(flags, bars, folds)

    side_lookup: Dict[str, Dict[int, int]] = {}
    side_net: Dict[str, np.ndarray] = {}
    side_used: Dict[str, Dict[int, int]] = {}
    for tag, side in ((fb.LONG_SETUP, "long"), (fb.SHORT_SETUP, "short")):
        idx, net, used = sk.barrier_r_for_all_bars(
            bars, take_profit_atr=fb.TAKE_PROFIT_ATR, stop_atr=fb.STOP_ATR,
            horizon=fb.HORIZON, atr_period=fb.ATR_PERIOD,
            round_trip_bps=fb.ROUND_TRIP_BPS, side=side, entry_on=fb.ENTRY_ON)
        side_net[tag] = fb.funding_adjusted_net_r(
            bars, funding, idx, net, used, tag,
            stop_atr=fb.STOP_ATR, atr_period=fb.ATR_PERIOD)
        side_lookup[tag] = {int(i): p for p, i in enumerate(idx)}
        side_used[tag] = {int(i): int(u) for i, u in zip(idx, used)}

    eligible = np.zeros(len(bars), dtype=bool)
    common = set(side_lookup[fb.LONG_SETUP]) & set(side_lookup[fb.SHORT_SETUP])
    eligible[sorted(common)] = True
    eligible[:warmup] = False
    late = np.zeros(len(bars), dtype=bool)
    late[list(fb.late_window_indices(bars, folds))] = True
    tradable = em.tradable_flags(flags, eligible & late)
    out: List[Candidate] = []
    for index in sk.simulate_schedule(tradable, warmup=warmup, lockup=fb.LOCKUP):
        direction = directions.get(index)
        if direction is None or index not in side_lookup[direction]:
            continue
        segment = ("forward" if fb.in_forward_window(bars[index], folds)
                   else "late_oos")
        pos = side_lookup[direction][index]
        out.append(Candidate(int(index), direction, segment,
                             float(side_net[direction][pos]),
                             int(side_used[direction][index])))
    return out


# ---------------------------------------------------------------------------
# the replay
# ---------------------------------------------------------------------------


@dataclass
class ReplayParams:
    notional_usd: float
    starting_cash: float
    leverage: float
    taker_fee_bps: float
    maker_fee_bps: float
    slippage_bps: float
    spread_bps: float
    maintenance_margin_rate: float = 0.005
    max_participation: Optional[float] = None


def _call(ex: bt.LinearSimulatedExchange, method: str, path: str,
          body: Optional[Dict[str, Any]] = None,
          params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    status, text = ex.request(method, path, headers={}, params=params,
                              body=json.dumps(body) if body is not None else None)
    reply = json.loads(text)
    reply["_http"] = status
    return reply


def _floor_step(value: float, step: float) -> float:
    return math.floor(value / step + 1e-9) * step


def _round_tick(value: float) -> float:
    return round(round(value / TICK) * TICK, 10)


class CandidateReplay:
    """Drives one LinearSimulatedExchange through a candidate schedule.

    All mutable state is in plain fields so the replay itself is resumable:
    :meth:`to_state` / :meth:`from_state` wrap the simulator's own state."""

    def __init__(self, bars: Sequence[bt.Bar], funding_rows: Sequence[Tuple[int, float]],
                 schedule: Sequence[Candidate], params: ReplayParams,
                 *, atr: Optional[np.ndarray] = None,
                 exchange: Optional[bt.LinearSimulatedExchange] = None) -> None:
        self.bars = list(bars)
        self.funding_rows = [(int(t), float(r)) for t, r in funding_rows]
        self.schedule = {c.index: c for c in schedule}
        self.params = params
        if atr is None:
            import sweep_geometry as sweep
            atr = sweep.wilder_atr(
                np.array([b.high for b in self.bars], dtype=float),
                np.array([b.low for b in self.bars], dtype=float),
                np.array([b.close for b in self.bars], dtype=float),
                fb.ATR_PERIOD)
        self.atr = np.asarray(atr, dtype=float)
        self.ex = exchange or bt.LinearSimulatedExchange(
            {SYMBOL: self.bars},
            starting_cash=params.starting_cash,
            taker_fee=params.taker_fee_bps / 10_000.0,
            maker_fee=params.maker_fee_bps / 10_000.0,
            slippage=params.slippage_bps / 10_000.0,
            spread_bps=params.spread_bps,
            funding_by_symbol={SYMBOL: self.funding_rows},
            maintenance_margin_rate=params.maintenance_margin_rate,
            leverage=params.leverage,
            max_participation=params.max_participation,
            instrument_filters=FILTERS,
        )
        self.book = 0.0                       # signed size, from fills only
        self.open: Optional[Dict[str, Any]] = None
        self.trades: List[Dict[str, Any]] = []
        self.skipped: List[Dict[str, Any]] = []
        self.equity_curve: List[float] = []
        self.adverse_curve: List[float] = []
        self.halted: Optional[str] = None
        self.seq = 0
        self._exit_seen = 0
        self.done = False

    # -- state -------------------------------------------------------------

    def to_state(self) -> Dict[str, Any]:
        return {
            "sim": self.ex.to_state(), "book": self.book, "open": self.open,
            "trades": self.trades, "skipped": self.skipped,
            "equity_curve": self.equity_curve, "adverse_curve": self.adverse_curve,
            "halted": self.halted, "seq": self.seq, "exit_seen": self._exit_seen,
            "done": self.done,
        }

    @classmethod
    def from_state(cls, bars, funding_rows, schedule, params, state,
                   *, atr=None) -> "CandidateReplay":
        state = json.loads(json.dumps(state))           # never share structure
        ex = bt.LinearSimulatedExchange.from_state(
            {SYMBOL: list(bars)}, state["sim"],
            funding_by_symbol={SYMBOL: [(int(t), float(r)) for t, r in funding_rows]})
        replay = cls(bars, funding_rows, schedule, params, atr=atr, exchange=ex)
        replay.book = float(state["book"])
        replay.open = state["open"]
        replay.trades = state["trades"]
        replay.skipped = state["skipped"]
        replay.equity_curve = state["equity_curve"]
        replay.adverse_curve = state["adverse_curve"]
        replay.halted = state["halted"]
        replay.seq = int(state["seq"])
        replay._exit_seen = int(state["exit_seen"])
        replay.done = bool(state["done"])
        return replay

    # -- helpers -----------------------------------------------------------

    def _oid(self, tag: str) -> str:
        self.seq += 1
        return f"LCR-{self.seq}-{tag}"

    def _order(self, oid: str) -> Dict[str, Any]:
        o = self.ex.orders[oid]
        return {"status": o.status, "cum_qty": float(o.cum_qty),
                "avg_price": float(o.avg_price), "fee": float(o.fee_amount)}

    def _fill(self, side: str, qty: float, price: float, fee: float,
              ref: float, kind: str) -> Dict[str, Any]:
        return {"side": side, "qty": qty, "price": price, "fee": fee,
                "ref": ref, "kind": kind, "bar": self.ex.index}

    # -- per bar -----------------------------------------------------------

    def _harvest_venue_exits(self) -> None:
        """Book the exits the VENUE made while resolving the bar just stepped:
        the position-attached stop, a liquidation, or the resting TP leg."""
        events = self.ex.exit_events[self._exit_seen:]
        self._exit_seen = len(self.ex.exit_events)
        if self.open is None:
            if events:
                raise bt.PositionReconciliationError(
                    f"venue exit {events} with no open trade in the book")
            return
        for ev in events:
            ref = float(ev["price"])
            if ev["purpose"] == "stop":
                cost = self.ex.slippage + self.ex.half_spread
                ref = ref / ((1 - cost) if ev["side"] == "Sell" else (1 + cost))
            elif ev["purpose"] == "tp":
                half = self.ex.half_spread
                ref = ref / ((1 - half) if ev["side"] == "Sell" else (1 + half))
            self.open["exits"].append(self._fill(
                ev["side"], float(ev["qty"]), float(ev["price"]),
                float(ev["fee"]), ref, ev["purpose"]))
            sign = 1.0 if ev["side"] == "Buy" else -1.0
            self.book += sign * float(ev["qty"])
        tp = self.ex.orders.get(self.open["tp_oid"]) if self.open["tp_oid"] else None
        if tp is not None:
            done = float(tp.cum_qty) - float(self.open["tp_booked"])
            if done > 1e-12:
                self.open["exits"].append(self._fill(
                    tp.side, done, float(tp.price),
                    float(tp.fee_amount) - float(self.open["tp_fee_booked"]),
                    float(tp.price), "tp"))
                self.open["tp_booked"] = float(tp.cum_qty)
                self.open["tp_fee_booked"] = float(tp.fee_amount)
                self.book += (1.0 if tp.side == "Buy" else -1.0) * done

    def _close_trade_if_flat(self) -> None:
        if self.open is None or abs(self.book) > 1e-12:
            return
        t = self.open
        if t["tp_oid"]:
            _call(self.ex, "POST", "/v5/order/cancel",
                  {"category": "linear", "symbol": SYMBOL, "orderLinkId": t["tp_oid"]})
        sign = 1.0 if t["entry"]["side"] == "Buy" else -1.0
        e = t["entry"]
        gross_fill = sum(sign * x["qty"] * (x["price"] - e["price"]) for x in t["exits"])
        gross_ref = sum(sign * x["qty"] * (x["ref"] - e["ref"]) for x in t["exits"])
        fees = e["fee"] + sum(x["fee"] for x in t["exits"])
        opened_ms = int(self.bars[t["entry_bar"]].start_ms)
        funding_paid = sum(p for s, _ms, p, opened in self.ex.funding_log
                           if s == SYMBOL and opened == opened_ms)
        net = gross_fill - fees - funding_paid
        cash_delta = self.ex.balances[self.ex.quote_asset] - t["cash_before"]
        if abs(cash_delta - net) > 1e-6:
            raise RuntimeError(
                f"trade at bar {t['signal_index']}: the ledger ({net!r}) does not "
                f"explain the cash change ({cash_delta!r})")
        risk_usd = e["qty"] * t["risk_distance"]
        kinds = sorted({x["kind"] for x in t["exits"]})
        unprotected = [u for u in self.ex.unprotected_log
                       if t["entry_bar"] < u["bar"] <= self.ex.index]
        self.trades.append({
            "signal_index": t["signal_index"],
            "signal_utc": iso(self.bars[t["signal_index"]].start_ms),
            "exit_utc": iso(self.bars[self.ex.index].start_ms),
            "segment": t["segment"], "direction": t["direction"],
            "qty": e["qty"], "notional_usd": e["qty"] * e["ref"],
            "entry_ref": e["ref"], "entry_fill": e["price"],
            "stop": t["stop"], "take_profit": t["tp"],
            "stop_verified": t["stop_verified"],
            "unprotected_bars": len(unprotected),
            "exit_kinds": kinds,
            "bars_held": self.ex.index - t["signal_index"],
            "gross_at_reference_usd": gross_ref,
            "slippage_and_spread_usd": gross_ref - gross_fill,
            "gross_after_slippage_usd": gross_fill,
            "fees_usd": fees,
            "funding_usd": -funding_paid,
            "net_usd": net,
            "risk_usd": risk_usd,
            "net_r": net / risk_usd if risk_usd > 0 else float("nan"),
            "barrier_net_r": t["barrier_net_r"],
        })
        self.open = None

    def _time_exit(self) -> None:
        t = self.open
        if t is None or self.ex.index < t["time_exit_index"]:
            return
        if t["tp_oid"]:
            _call(self.ex, "POST", "/v5/order/cancel",
                  {"category": "linear", "symbol": SYMBOL, "orderLinkId": t["tp_oid"]})
            # the cancel may race nothing here: the sim is single threaded
        qty = abs(self.book)
        side = "Buy" if self.book < 0 else "Sell"
        oid = self._oid("time")
        _call(self.ex, "POST", "/v5/order/create", {
            "category": "linear", "symbol": SYMBOL, "side": side,
            "orderType": "Market", "qty": f"{qty:.3f}", "reduceOnly": True,
            "orderLinkId": oid})
        o = self._order(oid)
        if o["cum_qty"] > 0:
            t["exits"].append(self._fill(side, o["cum_qty"], o["avg_price"],
                                         o["fee"], self.bars[self.ex.index].close,
                                         "time"))
            self.book += (1.0 if side == "Buy" else -1.0) * o["cum_qty"]
        # an unfilled remainder (participation cap) is retried next bar

    def _enter(self, cand: Candidate) -> None:
        i = self.ex.index
        ref = float(self.bars[i].close)
        risk_distance = fb.STOP_ATR * float(self.atr[i])
        if not math.isfinite(risk_distance) or risk_distance <= 0:
            self.skipped.append({"signal_index": i, "reason": "no_atr"})
            return
        qty = _floor_step(self.params.notional_usd / ref, QTY_STEP)
        if qty < MIN_QTY - 1e-12:
            self.skipped.append({"signal_index": i, "segment": cand.segment,
                                 "reason": "notional_below_min_qty",
                                 "price": ref})
            return
        side = "Sell" if cand.direction == fb.SHORT_SETUP else "Buy"
        cash_before = self.ex.balances[self.ex.quote_asset]
        oid = self._oid("entry")
        _call(self.ex, "POST", "/v5/order/create", {
            "category": "linear", "symbol": SYMBOL, "side": side,
            "orderType": "Market", "qty": f"{qty:.3f}", "orderLinkId": oid})
        o = self._order(oid) if oid in self.ex.orders else {"cum_qty": 0.0,
                                                             "status": "Rejected"}
        if o["cum_qty"] <= 0:
            self.skipped.append({"signal_index": i, "segment": cand.segment,
                                 "reason": f"entry_{o['status'].lower()}"})
            return
        filled = o["cum_qty"]
        self.book += (1.0 if side == "Buy" else -1.0) * filled
        sgn = 1.0 if side == "Buy" else -1.0
        stop = _round_tick(ref - sgn * risk_distance)
        tp = _round_tick(ref + sgn * fb.TAKE_PROFIT_ATR * float(self.atr[i]))
        self.open = {
            "signal_index": i, "entry_bar": i, "segment": cand.segment,
            "direction": cand.direction, "barrier_net_r": cand.barrier_net_r,
            "entry": self._fill(side, filled, o["avg_price"], o["fee"], ref, "entry"),
            "exits": [], "stop": stop, "tp": tp, "risk_distance": risk_distance,
            "time_exit_index": i + 1 + fb.HORIZON, "cash_before": cash_before,
            "stop_verified": False, "tp_oid": "", "tp_booked": 0.0,
            "tp_fee_booked": 0.0,
        }
        # protective stop on the POSITION, then read back
        reply = _call(self.ex, "POST", "/v5/position/trading-stop", {
            "category": "linear", "symbol": SYMBOL, "positionIdx": 0,
            "stopLoss": f"{stop:.1f}", "tpslMode": "Full"})
        rows = _call(self.ex, "GET", "/v5/position/list",
                     params={"category": "linear", "symbol": SYMBOL})
        row = (rows.get("result", {}).get("list") or [{}])[0]
        verified = (reply.get("retCode") == 0
                    and str(row.get("stopLoss") or "") != ""
                    and abs(float(row["stopLoss"]) - stop) < TICK / 2)
        self.open["stop_verified"] = verified
        if not verified:
            # Bracket failure: close at market and stop taking entries.
            qty_open = abs(self.book)
            close_side = "Buy" if self.book < 0 else "Sell"
            coid = self._oid("bracket-fail")
            _call(self.ex, "POST", "/v5/order/create", {
                "category": "linear", "symbol": SYMBOL, "side": close_side,
                "orderType": "Market", "qty": f"{qty_open:.3f}",
                "reduceOnly": True, "orderLinkId": coid})
            c = self._order(coid)
            self.open["exits"].append(self._fill(
                close_side, c["cum_qty"], c["avg_price"], c["fee"], ref,
                "bracket_failure"))
            self.book += (1.0 if close_side == "Buy" else -1.0) * c["cum_qty"]
            self.halted = f"bracket failure at bar {i}: entries halted"
            return
        tp_oid = self._oid("tp")
        _call(self.ex, "POST", "/v5/order/create", {
            "category": "linear", "symbol": SYMBOL,
            "side": "Sell" if side == "Buy" else "Buy",
            "orderType": "Limit", "price": f"{tp:.1f}", "qty": f"{filled:.3f}",
            "reduceOnly": True, "orderLinkId": tp_oid})
        self.open["tp_oid"] = tp_oid

    def run(self, *, stop_after_bar: Optional[int] = None) -> None:
        """Advance to the end (or until the simulator has stepped PAST
        ``stop_after_bar``, for a restart test). Idempotent once done."""
        while not self.done:
            i = self.ex.index
            self._harvest_venue_exits()
            self.ex.reconcile({SYMBOL: self.book} if abs(self.book) > 1e-12 else {})
            self._close_trade_if_flat()
            self._time_exit()
            self.ex.reconcile({SYMBOL: self.book} if abs(self.book) > 1e-12 else {})
            self._close_trade_if_flat()
            cand = self.schedule.get(i)
            if cand is not None:
                if self.halted:
                    self.skipped.append({"signal_index": i, "segment": cand.segment,
                                         "reason": "halted"})
                elif self.open is not None:
                    self.skipped.append({"signal_index": i, "segment": cand.segment,
                                         "reason": "position_open"})
                else:
                    self._enter(cand)
                    self.ex.reconcile(
                        {SYMBOL: self.book} if abs(self.book) > 1e-12 else {})
                    self._close_trade_if_flat()
            self.equity_curve.append(self.ex.equity())
            self.adverse_curve.append(min(self.ex.equity(), self.ex.adverse_equity()))
            if not self.ex.step():
                self.done = True
                if self.open is not None:
                    self.skipped.append({"signal_index": self.open["signal_index"],
                                         "reason": "open_at_data_end"})
                break
            if stop_after_bar is not None and self.ex.index > stop_after_bar:
                return


# ---------------------------------------------------------------------------
# summary
# ---------------------------------------------------------------------------


def _drawdown(curve: Sequence[float]) -> Tuple[float, float]:
    peak, worst_usd, worst_pct = -math.inf, 0.0, 0.0
    for v in curve:
        peak = max(peak, v)
        worst_usd = max(worst_usd, peak - v)
        if peak > 0:
            worst_pct = max(worst_pct, (peak - v) / peak)
    return worst_usd, worst_pct


def summarise(replay: CandidateReplay) -> Dict[str, Any]:
    def block(trades: List[Dict[str, Any]]) -> Dict[str, Any]:
        if not trades:
            return {"n_trades": 0}
        r = [t["net_r"] for t in trades]
        br = [t["barrier_net_r"] for t in trades]
        kinds: Dict[str, int] = {}
        for t in trades:
            for k in t["exit_kinds"]:
                kinds[k] = kinds.get(k, 0) + 1
        return {
            "n_trades": len(trades),
            "wins": sum(1 for x in r if x > 0),
            "mean_net_r": float(np.mean(r)),
            "sum_net_r": float(np.sum(r)),
            "mean_barrier_net_r_same_trades": float(np.mean(br)),
            "gross_at_reference_usd": sum(t["gross_at_reference_usd"] for t in trades),
            "slippage_and_spread_usd": sum(t["slippage_and_spread_usd"] for t in trades),
            "fees_usd": sum(t["fees_usd"] for t in trades),
            "funding_usd": sum(t["funding_usd"] for t in trades),
            "net_usd": sum(t["net_usd"] for t in trades),
            "exit_kinds": kinds,
            "trades_with_unverified_stop": sum(1 for t in trades if not t["stop_verified"]),
            "trades_with_unprotected_bars": sum(1 for t in trades if t["unprotected_bars"]),
        }

    ex = replay.ex
    dd_usd, dd_pct = _drawdown(replay.equity_curve)
    add_usd, add_pct = _drawdown(replay.adverse_curve)
    skipped: Dict[str, int] = {}
    for s in replay.skipped:
        skipped[s["reason"]] = skipped.get(s["reason"], 0) + 1
    return {
        "label": LABEL,
        "is_independent_confirmation": False,
        "is_stage1_evidence": False,
        "signal": fb.NAME, "symbol": SYMBOL,
        "constants": dict(fb.CONSTANTS),
        "params": dict(replay.params.__dict__),
        "cost_inputs_declared": ex.costs_declared,
        "schedule_candidates": len(replay.schedule),
        "skipped": skipped,
        "halted": replay.halted,
        "all": block(replay.trades),
        "late_oos": block([t for t in replay.trades if t["segment"] == "late_oos"]),
        "forward": block([t for t in replay.trades if t["segment"] == "forward"]),
        "account": {
            "starting_cash": ex.starting_cash,
            "ending_equity": ex.equity(),
            "fees_paid_usd": ex.fees_paid,
            "funding_paid_usd": ex.funding_paid,
            "liquidations": len(ex.liquidations),
            "liquidation_forfeited_usd": ex.liquidation_forfeited,
            "funding_settlements_without_print":
                ex.funding_settlements_without_print,
            "max_drawdown_close_usd": dd_usd, "max_drawdown_close_pct": dd_pct,
            "max_drawdown_intrabar_usd": add_usd,
            "max_drawdown_intrabar_pct": add_pct,
            "unprotected_bar_events": len(ex.unprotected_log),
        },
        "trades": replay.trades,
    }


def load_inputs(data_dir: str, funding_path: str):
    import market_data as md

    loaded, _b, _n = md.load_corpus(data_dir, verify=False)
    bars = [bt.Bar(b.start_ms, b.open, b.high, b.low, b.close, b.volume)
            for b in loaded[SYMBOL]]
    funding = fb.load_funding(funding_path)
    return bars, funding


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--data-dir", default=os.path.join(REPO, "data", "real_linear_1d"))
    parser.add_argument("--funding-data", default=os.path.join(
        REPO, "data", "real_funding", "funding", "BINANCE_LINEAR_BTC_USDT_FUNDING.csv.gz"))
    parser.add_argument("--warmup", type=int, default=200)
    parser.add_argument("--notional-usd", type=float, required=True)
    parser.add_argument("--starting-cash", type=float, required=True)
    parser.add_argument("--leverage", type=float, default=1.0)
    parser.add_argument("--taker-fee-bps", type=float, default=bt.PERP_TAKER_FEE_BPS)
    parser.add_argument("--maker-fee-bps", type=float, default=bt.PERP_MAKER_FEE_BPS)
    parser.add_argument("--slippage-bps", type=float, required=True,
                        help="REQUIRED: the repo holds no measured linear slippage")
    parser.add_argument("--spread-bps", type=float, required=True,
                        help="REQUIRED: full bid/ask spread; no measured linear series")
    parser.add_argument("--maintenance-margin-rate", type=float, default=0.005)
    parser.add_argument("--max-participation", type=float, default=None)
    parser.add_argument("--json-out", default=None)
    args = parser.parse_args(argv)

    folds = fb.load_folds()
    if not fb.folds_are_unmodified():
        print("REFUSING: the fold calendar's hash is not the locked one.")
        return 2
    bars, funding = load_inputs(args.data_dir, args.funding_data)
    schedule = build_schedule(bars, funding, folds, warmup=args.warmup)
    params = ReplayParams(
        notional_usd=args.notional_usd, starting_cash=args.starting_cash,
        leverage=args.leverage, taker_fee_bps=args.taker_fee_bps,
        maker_fee_bps=args.maker_fee_bps, slippage_bps=args.slippage_bps,
        spread_bps=args.spread_bps,
        maintenance_margin_rate=args.maintenance_margin_rate,
        max_participation=args.max_participation)
    replay = CandidateReplay(bars, list(zip(funding.times_ms, funding.rates)),
                             schedule, params)
    replay.run()
    out = summarise(replay)
    out["data"] = {"bars": len(bars), "first": iso(bars[0].start_ms),
                   "last": iso(bars[-1].start_ms), "funding_prints": len(funding),
                   "data_fingerprint": replay.ex.data_fingerprint()}
    off_grid = sum(1 for t in funding.times_ms if t % bt.LinearSimulatedExchange.FUNDING_INTERVAL_MS)
    out["data"]["funding_prints_off_8h_grid"] = off_grid
    text = json.dumps(out, indent=2, sort_keys=True, default=float)
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as handle:
            handle.write(text + "\n")
    summary = {k: v for k, v in out.items() if k != "trades"}
    print(json.dumps(summary, indent=2, sort_keys=True, default=float))
    print("\nper trade (segment, signal day, dir, exits, net_r sim | barrier):")
    for t in out["trades"]:
        print(f"  {t['segment']:8s} {t['signal_utc'][:10]} {t['direction'][:5]} "
              f"{','.join(t['exit_kinds']):12s} {t['net_r']:+.4f} | "
              f"{t['barrier_net_r']:+.4f}  net ${t['net_usd']:+.4f}")
    print("\n" + LABEL)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
