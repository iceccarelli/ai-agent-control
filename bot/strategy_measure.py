"""Measurement kernels for the opportunity engine. Pure standard library; no status, no I/O, no clock.

Each kernel MEASURES one hypothesis on supplied, time-ordered data and returns numbers with their uncertainty. Whether the
numbers earn a status is decided elsewhere (``opportunity_engine``), where every examined variant is counted. Nothing here is
tuned to the data: rules take PREDECLARED parameters, indicators are fed ONE observation at a time (so a rule cannot see the
future by construction), a position decided at the close of bar t earns the return of bar t+1 and pays its cost there, and
statistics are taken only from an evaluation window that starts after a warm-up and an embargo.

Costs are inputs, never defaults. A kernel that is given no cost refuses. Every interval comes from
``evidence_stats.block_bootstrap_ci`` (dependence-aware, with the effective sample size beside it), at an ``alpha`` the caller
sets from the number of variants examined.

  basis_episodes            spot/perp basis convergence: entries, convergences, failures, adverse widening, net outcome
  run_directional           a trend or mean-reversion rule over a price series, net of costs, against buy-and-hold
  imbalance_trades          an order-book-imbalance signal over a fixed horizon, net of costs
  regime_labels / regime_conditioning   a past-only volatility regime and a chronological test of whether it helps
  cross_sectional_funding   do per-instrument funding forecasts rank realised funding, and does tilting beat the baseline
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

import evidence_stats as ES
import funding_eval as FE


def _fin(x: Any) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def _need_cost(x: Any, name: str) -> float:
    if not (_fin(x) and x >= 0):
        raise ValueError(f"{name} must be a non-negative finite number of bps, supplied explicitly (never defaulted); got {x!r}")
    return float(x)


def _ci(xs: Sequence[float], alpha: float) -> Dict[str, Any]:
    # the bootstrap costs B * n; on long bar series fewer resamples are used. The reported interval is the WIDEST of the bootstrap
    # and the analytic intervals, so a smaller B cannot narrow it below them.
    n = len(xs)
    return ES.block_bootstrap_ci(list(xs), alpha=alpha, B=2000 if n <= 2000 else max(300, 4_000_000 // n))


def _summary(xs: Sequence[float], alpha: float) -> Dict[str, Any]:
    n = len(xs)
    if not n:
        return {"n": 0, "mean": None, "ci": {"status": "INSUFFICIENT", "n": 0}}
    return {"n": n, "mean": ES.mean(xs), "ess": ES.conservative_ess(xs), "ci": _ci(xs, alpha)}


# =============================== basis convergence ===============================

@dataclass(frozen=True)
class BasisParams:
    """Declared in advance. ``theta_in_bps``: enter (perp rich versus spot) when the basis is at least this; ``theta_out_bps``:
    converged when it is at most this; ``max_hold_s``: after this the episode is closed as FAILED at the then-basis;
    ``round_trip_cost_bps``: all-in cost of entering and leaving the hedged position (an input, from measured depth and the fee
    schedule, never a default)."""
    theta_in_bps: float
    theta_out_bps: float
    max_hold_s: float
    round_trip_cost_bps: float
    max_gap_s: float = 1800.0


def basis_episodes(rows: Sequence[Mapping[str, Any]], p: BasisParams, *, alpha: float = 0.05) -> Dict[str, Any]:
    """Measure ACTUAL convergence episodes on time-ordered rows carrying ``at_ms`` and ``basis_bps`` (mark minus spot mid, in
    bps of spot). Long spot / short perp when the perp is rich.

    NO LOOKAHEAD AT THE FILL. A signal is seen at snapshot i; the position is entered at the basis of the NEXT recorded snapshot
    (i+1), and an exit signal seen at snapshot j is filled at the basis of j+1. Filling at the snapshot that triggered the signal
    would book the very extreme that caused it, which turns pure noise into a "convergence" profit (an independent review
    measured a 12-of-12 false-positive rate on i.i.d. noise that way). One recording cadence of latency is conservative: a real
    order would be placed within seconds. The basis P&L of an episode is entry-fill basis minus exit-fill basis, less the
    round-trip cost. Funding earned during the hold is deliberately NOT added: it belongs to the carry hypothesis. An episode that
    straddles a data gap is excluded and counted, not interpolated. After closing, a new episode needs the basis to fall below
    ``theta_in`` first, so episodes do not overlap or chain. A percentile range that exceeds fees is only an exploratory screen."""
    cost = _need_cost(p.round_trip_cost_bps, "round_trip_cost_bps")
    if not (_fin(p.theta_in_bps) and _fin(p.theta_out_bps) and p.theta_out_bps < p.theta_in_bps and p.max_hold_s > 0):
        raise ValueError("theta_out must be below theta_in and max_hold_s positive")
    eps: List[Dict[str, Any]] = []
    gap_broken = 0
    state = "FLAT"                     # FLAT | PENDING_ENTRY | OPEN | PENDING_EXIT
    open_: Optional[Dict[str, Any]] = None
    outcome: Optional[str] = None
    armed = True
    last_t: Optional[int] = None
    usable = 0
    for r in rows:
        b, t = r.get("basis_bps"), r.get("at_ms")
        if not (_fin(b) and _fin(t)):
            continue
        usable += 1
        if last_t is not None and t < last_t:
            raise ValueError("rows must be time-ordered")
        if state != "FLAT" and last_t is not None and (t - last_t) > p.max_gap_s * 1000:
            gap_broken += 1
            state, open_, outcome, armed = "FLAT", None, None, False
        last_t = t
        if state == "PENDING_ENTRY":                                   # this snapshot is the entry FILL
            open_ = {"t": t, "b": b, "max": b}
            state = "OPEN"
        elif state == "PENDING_EXIT":                                  # this snapshot is the exit FILL
            open_["max"] = max(open_["max"], b)                        # the adverse excursion includes the basis AT the fill
            if outcome == "CONVERGED" and b > p.theta_out_bps:         # the basis moved back out during the one-cadence fill latency:
                outcome = "REVERTED_BEFORE_EXIT_FILL"                  # it did not converge at the price actually obtained, so it is a failure
            eps.append({"entry_ms": open_["t"], "exit_ms": t, "hold_h": (t - open_["t"]) / 3_600_000.0, "entry_bps": open_["b"],
                        "exit_bps": b, "outcome": outcome, "adverse_widening_bps": open_["max"] - open_["b"],
                        "gross_bps": open_["b"] - b, "net_bps": open_["b"] - b - cost})
            state, open_, outcome, armed = "FLAT", None, None, False
        elif state == "OPEN":
            open_["max"] = max(open_["max"], b)
            if b <= p.theta_out_bps:
                state, outcome = "PENDING_EXIT", "CONVERGED"
            elif (t - open_["t"]) / 1000.0 >= p.max_hold_s:
                state, outcome = "PENDING_EXIT", "FAILED_TO_CONVERGE"
        else:                                                          # FLAT
            if b < p.theta_in_bps:
                armed = True
            elif armed:
                state, armed = "PENDING_ENTRY", False
    nets = [e["net_bps"] for e in eps]
    conv = [e for e in eps if e["outcome"] == "CONVERGED"]
    fail = [e for e in eps if e["outcome"] != "CONVERGED"]
    span_days = ((rows[-1]["at_ms"] - rows[0]["at_ms"]) / 86_400_000.0) if usable >= 2 else 0.0
    return {
        "params": {"theta_in_bps": p.theta_in_bps, "theta_out_bps": p.theta_out_bps, "max_hold_s": p.max_hold_s,
                   "round_trip_cost_bps": cost, "max_gap_s": p.max_gap_s},
        "fill_rule": "entry and exit are filled at the NEXT recorded snapshot after the signal (conservative: one cadence of latency)",
        "usable_snapshots": usable, "span_days": span_days, "episodes": len(eps), "converged": len(conv), "failed": len(fail),
        "excluded_by_data_gap": gap_broken, "still_open_at_end": state != "FLAT",
        "failure_share": (len(fail) / len(eps)) if eps else None,
        "hold_hours": {"mean": ES.mean([e["hold_h"] for e in eps]), "max": max((e["hold_h"] for e in eps), default=None)} if eps else None,
        "adverse_widening_bps": {"mean": ES.mean([e["adverse_widening_bps"] for e in eps]),
                                 "max": max(e["adverse_widening_bps"] for e in eps)} if eps else None,
        "gross_mean_bps": ES.mean([e["gross_bps"] for e in eps]) if eps else None,
        "net": _summary(nets, alpha), "worst_net_bps": min(nets) if nets else None,
        "episodes_list": eps,
        "reading": "basis P&L only; funding earned during the hold is measured by the carry evaluator, not added here",
    }


# =============================== directional rules (trend, mean reversion) ===============================

class EMACross:
    """Trend: +1 while the fast EMA is above the slow one, -1 while below, 0 until ``slow`` observations exist."""
    def __init__(self, fast: int, slow: int) -> None:
        if not (1 <= fast < slow):
            raise ValueError("need 1 <= fast < slow")
        self.fast, self.slow, self.n = fast, slow, 0
        self._f = self._s = 0.0

    def update(self, price: float) -> float:
        self.n += 1
        if self.n == 1:
            self._f = self._s = price
        else:
            self._f += 2.0 / (self.fast + 1) * (price - self._f)
            self._s += 2.0 / (self.slow + 1) * (price - self._s)
        if self.n < self.slow:
            return 0.0
        return 1.0 if self._f > self._s else (-1.0 if self._f < self._s else 0.0)


class ZScoreReversion:
    """Mean reversion: short when the price is ``z_in`` standard deviations above its trailing mean, long when below,
    flat once it is back inside ``z_out``; otherwise unchanged. Needs ``window`` observations first."""
    def __init__(self, window: int, z_in: float, z_out: float) -> None:
        if not (window >= 5 and z_in > z_out >= 0):
            raise ValueError("need window >= 5 and z_in > z_out >= 0")
        self.window, self.z_in, self.z_out = window, z_in, z_out
        self._buf: List[float] = []
        self._pos = 0.0

    def update(self, price: float) -> float:
        self._buf.append(price)
        if len(self._buf) > self.window:
            self._buf.pop(0)
        if len(self._buf) < self.window:
            return 0.0
        m = sum(self._buf) / self.window
        sd = math.sqrt(sum((x - m) ** 2 for x in self._buf) / (self.window - 1))
        if sd <= 0:
            return self._pos
        z = (price - m) / sd
        if z >= self.z_in:
            self._pos = -1.0
        elif z <= -self.z_in:
            self._pos = 1.0
        elif abs(z) <= self.z_out:
            self._pos = 0.0
        return self._pos


def run_directional(prices: Sequence[float], rule: Any, *, one_way_cost_bps: float, bars_per_year: float,
                    eval_start: int = 0, embargo: int = 0, alpha: float = 0.05) -> Dict[str, Any]:
    """Run ``rule`` (an object with ``update(price) -> target position``) over ``prices`` and measure the NET result on the
    window ``[eval_start + embargo, end)``. The rule is fed one price at a time and its target for bar t+1 is decided from
    prices up to t only; the cost of changing position is charged on the bar it changes. The warm-up and the embargo are run
    but never counted. Compared against buy-and-hold over the same window."""
    cost = _need_cost(one_way_cost_bps, "one_way_cost_bps") / 1e4
    if not (_fin(bars_per_year) and bars_per_year > 0):
        raise ValueError("bars_per_year must be positive")
    if any(not (_fin(x) and x > 0) for x in prices):
        raise ValueError("prices must be positive finite numbers")
    n = len(prices)
    start = eval_start + embargo
    if n < 3 or start >= n - 1:
        return {"status": "INSUFFICIENT", "reason": "no evaluation window after the warm-up and embargo", "bars": 0}
    pos, target = 0.0, rule.update(prices[0])
    net: List[float] = []
    hold: List[float] = []
    turns = 0.0
    for t in range(1, n):
        c = cost * abs(target - pos)
        turns += abs(target - pos)
        pos = target
        r = prices[t] / prices[t - 1] - 1.0
        if t >= start:
            net.append(pos * r - c)
            hold.append(r)
        target = rule.update(prices[t])             # decided from prices[0..t] only, applied from bar t+1
    excess = [a - b for a, b in zip(net, hold)]
    cum, peak, mdd = 1.0, 1.0, 0.0
    for x in net:
        cum *= 1.0 + x
        peak = max(peak, cum)
        mdd = max(mdd, 1.0 - cum / peak)
    ann = bars_per_year
    res = {
        "status": "MEASURED", "bars": len(net), "alpha": alpha,
        "net_per_bar": _summary(net, alpha), "excess_vs_hold_per_bar": _summary(excess, alpha),
        "hold_mean_per_bar": ES.mean(hold),
        "net_annualised_pct": ES.mean(net) * ann * 100.0, "hold_annualised_pct": ES.mean(hold) * ann * 100.0,
        "position_changes_per_bar": turns / max(1, n - 1), "max_drawdown_pct": mdd * 100.0,
        "one_way_cost_bps": cost * 1e4,
        "reading": "net of the stated one-way cost; the rule saw prices up to t only; no parameter was fitted; perpetual funding "
                   "payments and receipts while holding are NOT modelled (a directional perp pays or receives funding)",
    }
    return res


# =============================== order-book imbalance ===============================

def imbalance_trades(imbalance: Sequence[float], mid: Sequence[float], *, horizon: int, theta: float, one_way_cost_bps: float,
                     eval_start: int = 0, embargo: int = 0, alpha: float = 0.05) -> Dict[str, Any]:
    """Enter with the sign of the imbalance when ``|imbalance| >= theta``, hold exactly ``horizon`` observations, pay the
    one-way cost twice. Entries are sequential (no overlap). The fill is assumed AT THE MID, which is optimistic: the spread
    and adverse selection must be inside ``one_way_cost_bps`` or the result overstates the edge. Needs an imbalance series the
    capacity rows do not currently carry."""
    cost = _need_cost(one_way_cost_bps, "one_way_cost_bps")
    if len(imbalance) != len(mid) or horizon < 1 or not (0 < theta <= 1):
        raise ValueError("series must be aligned, horizon >= 1, 0 < theta <= 1")
    if any(not (_fin(x) and -1.0 <= x <= 1.0) for x in imbalance) or any(not (_fin(x) and x > 0) for x in mid):
        raise ValueError("imbalance must lie in [-1, 1] and mid must be positive and finite")
    start = eval_start + embargo
    nets: List[float] = []
    ics_x: List[float] = []
    ics_y: List[float] = []
    t = start
    while t + horizon < len(mid):
        if abs(imbalance[t]) >= theta:
            side = 1.0 if imbalance[t] > 0 else -1.0
            fwd = mid[t + horizon] / mid[t] - 1.0
            nets.append(side * fwd * 1e4 - 2.0 * cost)
            ics_x.append(imbalance[t])
            ics_y.append(fwd)
            t += horizon
        else:
            t += 1
    return {"status": "MEASURED" if nets else "INSUFFICIENT", "trades": len(nets), "horizon": horizon, "theta": theta,
            "net_bps_per_trade": _summary(nets, alpha), "one_way_cost_bps": cost,
            "fill_assumption": "AT THE MID (optimistic); spread and adverse selection must be inside the cost",
            "signal_forward_return_corr": _corr(ics_x, ics_y)}


def _corr(a: Sequence[float], b: Sequence[float]) -> Optional[float]:
    n = len(a)
    if n < 3:
        return None
    ma, mb = sum(a) / n, sum(b) / n
    da = math.sqrt(sum((x - ma) ** 2 for x in a))
    db = math.sqrt(sum((y - mb) ** 2 for y in b))
    if da <= 0 or db <= 0:
        return None
    return sum((x - ma) * (y - mb) for x, y in zip(a, b)) / (da * db)


# =============================== volatility regime ===============================

def regime_labels(prices: Sequence[float], *, window: int, min_history: int) -> List[Optional[str]]:
    """LOW / MID / HIGH by the trailing realised volatility over ``window`` bars, against the terciles of the volatilities seen
    SO FAR (expanding, past only). ``None`` until ``window + min_history`` bars exist. The label at t uses prices up to t."""
    if window < 3 or min_history < 3:
        raise ValueError("window and min_history must be at least 3")
    rets = [prices[i] / prices[i - 1] - 1.0 for i in range(1, len(prices))]
    seen: List[float] = []
    out: List[Optional[str]] = [None]
    for i in range(len(rets)):
        if i + 1 < window:
            out.append(None)
            continue
        w = rets[i + 1 - window:i + 1]
        m = sum(w) / window
        vol = math.sqrt(sum((x - m) ** 2 for x in w) / (window - 1))
        if len(seen) < min_history:
            out.append(None)
        else:
            s = sorted(seen)
            lo, hi = s[len(s) // 3], s[(2 * len(s)) // 3]
            out.append("LOW" if vol <= lo else ("HIGH" if vol >= hi else "MID"))
        seen.append(vol)
    return out


def regime_conditioning(labels: Sequence[Optional[str]], values: Sequence[float], *, alpha: float = 0.05) -> Dict[str, Any]:
    """Does conditioning on the regime improve a per-settlement value (for instance realised funding net of cost)? Chronological:
    the regime with the best mean in the FIRST half is chosen there, and only the SECOND half is used to judge it. Three
    regimes were examined, so the caller should pass ``alpha`` divided by 3 for a family-wise level."""
    pairs = [(l, v) for l, v in zip(labels, values) if l is not None and _fin(v)]
    if len(pairs) < 40:
        return {"status": "INSUFFICIENT", "reason": f"{len(pairs)} labelled observations; at least 40 needed", "n": len(pairs)}
    half = len(pairs) // 2
    disc, test = pairs[:half], pairs[half:]
    means = {}
    for r in ("LOW", "MID", "HIGH"):
        xs = [v for l, v in disc if l == r]
        if len(xs) >= 8:
            means[r] = ES.mean(xs)
    if not means:
        return {"status": "INSUFFICIENT", "reason": "no regime has 8 discovery observations", "n": len(pairs)}
    chosen = max(means, key=means.get)
    sel = [v for l, v in test if l == chosen]
    allv = [v for _, v in test]
    if len(sel) < 8:
        return {"status": "INSUFFICIENT", "reason": f"only {len(sel)} test observations in the chosen regime", "chosen": chosen}
    ci = _ci(sel, alpha)
    base = ES.mean(allv)
    return {"status": "MEASURED", "chosen_regime": chosen, "discovery_means": means, "test_n": len(allv),
            "test_selected": {"n": len(sel), "mean": ES.mean(sel), "ci": ci}, "test_all_mean": base,
            "improves_out_of_sample": bool(ci.get("status") == "OK" and ci["lo"] > base),
            "reading": "the regime was chosen on the first half and judged on the second; 3 regimes were examined"}


# =============================== cross-sectional funding ===============================

def _rank(xs: Sequence[float]) -> List[float]:
    order = sorted(range(len(xs)), key=lambda i: xs[i])
    ranks = [0.0] * len(xs)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and xs[order[j + 1]] == xs[order[i]]:
            j += 1
        for k in range(i, j + 1):
            ranks[order[k]] = (i + j) / 2.0 + 1.0
        i = j + 1
    return ranks


def cross_sectional_funding(rows_by_symbol: Mapping[str, Sequence[Mapping[str, Any]]], *, baseline: str, k: int = 1,
                            hold_settlements: int = 21, offset_min: int = 30, target_notional_usd: float = 10_000.0,
                            min_symbols: int = 3, alpha: float = 0.05) -> Dict[str, Any]:
    """Do the funding FORECASTS across instruments rank the funding that was then PRINTED, and would tilting from the baseline
    to the top-``k`` forecast instruments have beaten the baseline after each instrument's OWN round-trip cost?

    Only comparable instruments enter: every instrument's rows must be one environment and one funding interval, the
    settlement must be present for at least ``min_symbols`` instruments with a forecast ``offset_min`` minutes before it AND
    a realised public print, and each instrument is charged its own measured round-trip cost at ``target_notional_usd`` (from
    its own depth and fee schedule) amortised over ``hold_settlements``. A settlement missing for an instrument is simply
    absent for it; nothing is filled. The realised value is the public print, not account cash."""
    syms = sorted(s for s, r in rows_by_symbol.items() if r)
    if baseline not in syms:
        return {"status": "INSUFFICIENT", "reason": f"the baseline {baseline} has no rows", "symbols": syms}
    if len(syms) < min_symbols:
        return {"status": "INSUFFICIENT", "reason": f"{len(syms)} instrument(s) recorded; at least {min_symbols} needed", "symbols": syms}
    envs = {r["environment"] for s in syms for r in rows_by_symbol[s]}
    intervals = {r["funding"]["interval_h"] for s in syms for r in rows_by_symbol[s]}
    if len(envs) != 1 or len(intervals) != 1:
        return {"status": "INSUFFICIENT", "reason": f"instruments are not comparable: environments {sorted(envs)}, intervals {sorted(intervals)}", "symbols": syms}
    cost: Dict[str, float] = {}
    per: Dict[str, Dict[int, Tuple[float, float]]] = {}
    for s in syms:
        pts = [p["total_round_trip_bps"] for r in rows_by_symbol[s] for p in r["curve"]
               if p.get("status") == "MEASURED" and float(p["notional_usd"]) == float(target_notional_usd)]
        if not pts:
            return {"status": "INSUFFICIENT", "reason": f"{s} has no measured cost at ${target_notional_usd:,.0f}: its own depth is unknown", "symbols": syms}
        cost[s] = ES.mean(pts)
        per[s] = {rec["settlement_ms"]: (rec["forecasts"][offset_min], rec["realised_bps"])
                  for rec in FE.join(rows_by_symbol[s], offsets_min=(offset_min,))["joined"] if offset_min in rec["forecasts"]}
    every = set().union(*[set(per[s]) for s in syms])
    times = sorted(t for t in every if t in per[baseline] and sum(1 for s in syms if t in per[s]) >= min_symbols)
    edges: List[float] = []
    ics: List[float] = []
    for t in times:
        present = [s for s in syms if t in per[s]]
        fc = [per[s][t][0] for s in present]
        rl = [per[s][t][1] for s in present]
        ra, rb = _rank(fc), _rank(rl)
        ma, mb = sum(ra) / len(ra), sum(rb) / len(rb)
        da = math.sqrt(sum((x - ma) ** 2 for x in ra))
        db = math.sqrt(sum((y - mb) ** 2 for y in rb))
        if da > 0 and db > 0:
            ics.append(sum((x - ma) * (y - mb) for x, y in zip(ra, rb)) / (da * db))
        top = sorted(present, key=lambda s: per[s][t][0], reverse=True)[:k]
        sel_real = sum(per[s][t][1] for s in top) / len(top)
        sel_cost = sum(cost[s] for s in top) / len(top)
        edges.append(sel_real - per[baseline][t][1] - (sel_cost - cost[baseline]) / hold_settlements)
    if not edges:
        return {"status": "INSUFFICIENT", "reason": "no settlement has the baseline plus enough instruments with a forecast and a print", "symbols": syms}
    return {"status": "MEASURED", "symbols": syms, "baseline": baseline, "k": k, "aligned_settlements": len(edges),
            "rank_information_coefficient": _summary(ics, alpha),
            "net_edge_bps_per_settlement_vs_baseline": _summary(edges, alpha),
            "round_trip_cost_bps": cost, "hold_settlements_assumed": hold_settlements,
            "reading": "public realised prints, each instrument charged its own measured cost; not account cash"}
