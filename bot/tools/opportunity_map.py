#!/usr/bin/env python3
"""Where could a net edge exist? A read-only, offline map of the economics.

WHAT THIS IS
============
Arithmetic and historical descriptions over data ALREADY IN THIS REPOSITORY.
It answers, with repeatable numbers, the questions an owner needs before
believing anything about the BTC carry book:

  1. inventory      which market-data corpora exist, what they span, and how
                    they sit against the owner's discovery window
  2. costs          the complete cost of one hedged round trip (fees from the
                    fee tier, slippage, financing / opportunity cost) and the
                    funding it takes to earn it back over a stated hold
  3. gate           what the PRODUCTION entry gate (`carry_costs.entry_gate`,
                    called, never re-implemented) answers over a funding x
                    basis plane, and what the entry net-benefit gate adds
  4. history        what a forced-hold hedge would have netted on the Bybit
                    settlement corpus, unconditionally and behind the gate,
                    split by period, by entry basis, by funding level and by
                    settlement window
  5. lot            what one 0.001 BTC lot (the whole position under the $100
                    cap) earns in dollars
  6. benchmark      funding against the cash alternative (OKX USDT lending
                    floor) and against other venues / assets already on disk

WHAT THIS IS NOT
================
* NOT evidence of edge. Every number from the corpus is labelled
  HISTORICAL_SIMULATION and is IN-SAMPLE: the production gate's constants were
  settled with the whole corpus visible, and EVERY window of it is recorded as
  READ in artifacts/data_read_ledger.json. The owner's discovery window ends
  after the last settlement on disk, so this repository holds NO out-of-sample
  settlement (the report states the count). The chronological splits it prints
  show regime STABILITY, never independent confirmation.
* NOT a fill model. The slippage and fill assumptions are declared parameters.
* Not a recommendation of any constant. It changes nothing and tunes nothing.
* It reads no credential, no network, no database, no environment, and it
  writes no file unless `--json` names one. It does NOT append to the data-read
  ledger itself; only `--engine-replay` goes through `carry_backtest`'s loader,
  whose auto-record is idempotent and has already recorded every window here.

    python3 tools/opportunity_map.py --repo .
    python3 tools/opportunity_map.py --repo . --json out.json --engine-replay
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import gzip
import json
import math
import os
import random
import statistics
import sys
from typing import Any, Dict, List, Optional, Sequence, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, ROOT)

import carry_costs as costs  # noqa: E402  (pure arithmetic; the production gate)
import strategy_evidence as se  # noqa: E402

HISTORICAL = "HISTORICAL_SIMULATION"
ARITHMETIC = "ARITHMETIC_ON_DECLARED_INPUTS"
INVENTORY = "FILE_INVENTORY"

#: The owner's discovery window, as stated to this analysis (epoch seconds). It
#: is NOT read from config or the environment here; pass --discovery-* to move it.
DISCOVERY_START_EPOCH = 1788739200   # 2026-09-07T00:00:00Z
DISCOVERY_END_EPOCH = 1793491199     # 2026-10-31T23:59:59Z

DAY_MS = 86_400_000
H4_MS = 4 * 3_600_000
PRINTS_PER_DAY = costs.FUNDING_PERIODS_PER_DAY
HOLD_PRINTS = int(math.floor(costs.DEMONSTRATED_HOLD_DAYS * PRINTS_PER_DAY))  # 17
FUNDING_HISTORY = 8            # CarryEngine.FUNDING_HISTORY
BOOTSTRAP_SEED = 20261102
BOOTSTRAP_RESAMPLES = 2000

BYBIT_DIR = "data/real_bybit_btc_4h"
BYBIT_SPOT_4H = f"{BYBIT_DIR}/ohlcv/BYBIT_SPOT_BTC_USDT_4H.csv.gz"
BYBIT_PERP_4H = f"{BYBIT_DIR}/ohlcv/BYBIT_LINEAR_BTC_USDT_4H.csv.gz"
BYBIT_FUNDING = f"{BYBIT_DIR}/funding/BYBIT_LINEAR_BTC_USDT_FUNDING.csv.gz"
BINANCE_8H_DIR = "data/real_settlement_8h"
BINANCE_FUNDING_DIR = "data/real_funding_full/funding"
OKX_LENDING = "data/real_borrow/OKX_LENDING_RATE_USDT_1H.csv.gz"

#: What each corpus directory is FOR, in this analysis. Hand-written because the
#: manifests do not say, and a directory this table does not know is reported as
#: "unclassified" rather than guessed.
ROLE = {
    "real_bybit_btc_4h": "carry economics: same-venue spot+perp 4h closes and funding (the execution venue)",
    "real_funding": "funding history, Binance (older fetch)",
    "real_funding_full": "funding history, Binance BTC/ETH/SOL (breadth, cross-venue)",
    "real_settlement_8h": "8h closes on the 00/08/16 UTC settlement grid, Binance spot+perp",
    "real_borrow": "OKX USDT lending-rate history (a FLOOR on financing; the cash alternative)",
    "real": "daily/hourly OHLCV, Binance spot (directional research corpora)",
    "real_1d": "daily OHLCV (directional research)",
    "real_4h": "4h OHLCV (directional research)",
    "real_linear_1d": "daily OHLCV, Binance linear (directional research)",
    "real_linear_1d_full": "daily OHLCV, Binance linear (barrier series, funding_carry_fade)",
    "real_multi_1d": "daily OHLCV, other spot assets (no funding: cannot carry)",
    "real_multi_1h": "hourly OHLCV, other spot assets (no funding: cannot carry)",
    "real_spot_btc": "daily OHLCV, Binance spot BTC (directional research)",
    "real_spot_full": "daily OHLCV, Binance spot BTC/ETH/SOL (directional research)",
    "ohlcv": "SYNTHETIC generator output (tools/make_dataset.py): tests code, says nothing about a market",
    "orderbook": "SYNTHETIC generator output",
    "quotes": "SYNTHETIC generator output",
    "anomalies": "SYNTHETIC deliberately broken fixtures",
}
TIME_KEYS = ("time_period_start", "funding_time", "open_utc", "utc")


# ---------------------------------------------------------------------------
# small utilities
# ---------------------------------------------------------------------------
def _open(path: str):
    return gzip.open(path, "rt", encoding="utf-8") if path.endswith(".gz") \
        else open(path, "rt", encoding="utf-8")


def _rows(path: str) -> List[Dict[str, str]]:
    with _open(path) as handle:
        return list(csv.DictReader(handle))


def _parse_utc(text: str) -> int:
    """ISO-8601 UTC ('...Z', '+00:00' or seven fractional digits) -> epoch ms."""
    core = text.strip()[:19]
    return int(dt.datetime.strptime(core, "%Y-%m-%dT%H:%M:%S")
               .replace(tzinfo=dt.timezone.utc).timestamp() * 1000)


def _iso(ms: Optional[int]) -> Optional[str]:
    if ms is None:
        return None
    return dt.datetime.fromtimestamp(ms / 1000.0, dt.timezone.utc) \
        .strftime("%Y-%m-%dT%H:%M:%SZ")


def _year(ms: int) -> int:
    return dt.datetime.fromtimestamp(ms / 1000.0, dt.timezone.utc).year


def summarise(values: Sequence[float]) -> Dict[str, Any]:
    v = [float(x) for x in values if x is not None and math.isfinite(x)]
    if not v:
        return {"n": 0}
    out: Dict[str, Any] = {
        "n": len(v), "mean": statistics.fmean(v), "median": statistics.median(v),
        "sd": statistics.stdev(v) if len(v) > 1 else 0.0,
        "share_positive": sum(1 for x in v if x > 0) / len(v),
        "total": sum(v)}
    return out


def block_bootstrap_mean(values: Sequence[float], *, block: int,
                         seed: int = BOOTSTRAP_SEED,
                         resamples: int = BOOTSTRAP_RESAMPLES,
                         level: float = 0.90) -> Optional[Tuple[float, float]]:
    """Moving-block bootstrap interval for the mean (deterministic by seed).

    Overlapping forced-hold windows are serially dependent, so an i.i.d.
    bootstrap would be far too tight; `block` is set to at least two holds.
    Returns None below 2 blocks of data rather than a number nobody should read.
    """
    v = [float(x) for x in values]
    n = len(v)
    if n < 2 * max(block, 1):
        return None
    rng = random.Random(seed)
    starts = n - block + 1
    per = max(1, n // block)
    means = []
    for _ in range(resamples):
        acc = 0.0
        cnt = 0
        for _b in range(per):
            s = rng.randrange(starts)
            acc += sum(v[s:s + block])
            cnt += block
        means.append(acc / cnt)
    means.sort()
    lo = means[int(((1 - level) / 2) * (resamples - 1))]
    hi = means[int((1 - (1 - level) / 2) * (resamples - 1))]
    return lo, hi


# ---------------------------------------------------------------------------
# 1. inventory
# ---------------------------------------------------------------------------
def _span(path: str) -> Dict[str, Any]:
    n = 0
    first = last = None
    key = None
    with _open(path) as handle:
        reader = csv.DictReader(handle)
        key = next((k for k in TIME_KEYS if k in (reader.fieldnames or [])), None)
        for row in reader:
            n += 1
            if key and row.get(key):
                if first is None:
                    first = row[key]
                last = row[key]
    return {"rows": n,
            "first_utc": _iso(_parse_utc(first)) if first else None,
            "last_utc": _iso(_parse_utc(last)) if last else None,
            "_last_ms": _parse_utc(last) if last else None}


def inventory(repo: str, *, discovery_start: float, discovery_end: float
              ) -> Dict[str, Any]:
    base = os.path.join(repo, "data")
    files: List[Dict[str, Any]] = []
    for dirpath, _dirs, names in sorted(os.walk(base)):
        for name in sorted(names):
            if not name.endswith((".csv", ".csv.gz")):
                continue
            path = os.path.join(dirpath, name)
            rel = os.path.relpath(path, repo)
            top = os.path.relpath(path, base).split(os.sep)[0]
            if top == "anomalies":
                continue
            entry: Dict[str, Any] = {
                "path": rel, "corpus": top,
                "role": ROLE.get(top, "unclassified"),
                "synthetic": top in ("ohlcv", "orderbook", "quotes", "anomalies")}
            entry.update(_span(path))
            last_ms = entry.pop("_last_ms")
            if last_ms is None:
                entry["vs_discovery_window"] = "UNKNOWN"
            elif last_ms / 1000.0 < discovery_start:
                entry["vs_discovery_window"] = "ENDS_BEFORE_WINDOW"
            elif last_ms / 1000.0 <= discovery_end:
                entry["vs_discovery_window"] = "ENDS_INSIDE_WINDOW"
            else:
                entry["vs_discovery_window"] = "EXTENDS_PAST_WINDOW"
            files.append(entry)
    real = [f for f in files if not f["synthetic"]]
    past = [f for f in real if f["vs_discovery_window"] == "EXTENDS_PAST_WINDOW"]
    return {
        "evidence_class": INVENTORY,
        "discovery_window": {"start_epoch": discovery_start,
                             "end_epoch": discovery_end,
                             "start_utc": _iso(int(discovery_start * 1000)),
                             "end_utc": _iso(int(discovery_end * 1000))},
        "files": files,
        "real_files": len(real), "synthetic_files": len(files) - len(real),
        "real_files_with_data_after_window_end": len(past),
        "latest_real_observation_utc": max(
            (f["last_utc"] for f in real if f["last_utc"]), default=None),
        "instruments_with_funding_and_both_legs": [
            "BYBIT BTCUSDT spot+linear 4h + funding (execution venue)",
            "BINANCE BTC/ETH/SOL 8h spot+perp + funding (other venue)"],
        "instruments_the_engine_may_trade": sorted(_engine_symbols()),
        "note": ("Nothing here is later than the owner's discovery window. "
                 "Forward evidence must be collected after "
                 f"{_iso(int(discovery_end * 1000))}; none is in the corpora.")
    }


def _engine_symbols() -> Sequence[str]:
    try:
        import carry_engine
        return tuple(carry_engine.CarryEngine.FUNDING_8H_PROVEN)
    except Exception:  # noqa: BLE001 - an inventory must not die on an import
        return ()


# ---------------------------------------------------------------------------
# 2. costs
# ---------------------------------------------------------------------------
#: The Bybit BASE-tier fees, in bps of leg notional. Declared here rather than
#: read from `config`: importing `config` runs `load()` against the process
#: environment, and one bad setting there would crash a read-only tool. The
#: taker figures are `carry_costs`' own constants; the maker figures are the
#: `config.py` linear/spot defaults (a test pins both to `config.load({})`).
#: The live account's tier is read from the venue at run time and may differ.
PERP_MAKER_BPS = costs.MAKER_BPS_PERP_MODELLED
SPOT_MAKER_BPS = 10.0


def fee_tier() -> Dict[str, Any]:
    return {"perp_taker_bps": costs.TAKER_BPS_PERP, "perp_maker_bps": PERP_MAKER_BPS,
            "spot_taker_bps": costs.TAKER_BPS_SPOT, "spot_maker_bps": SPOT_MAKER_BPS,
            "source": "BASE tier: carry_costs taker constants + config.py maker defaults; "
                      "the live account's tier is read from the venue and may differ"}


def round_trip_bps(*, mode: str, style: str, tier: Dict[str, Any],
                   slippage_bps_per_leg: float) -> Dict[str, float]:
    """One full cycle in bps of the hedged notional.

    overlay: two perp legs. acquire: those plus the spot round trip.
    `maker` assumes EVERY order fills at the touch as a maker (the best case);
    the production gate prices the taker figure whatever the style.
    """
    legs_perp = 2
    legs_spot = 0 if mode == "overlay" else 2
    p = tier["perp_maker_bps"] if style == "maker" else tier["perp_taker_bps"]
    s = tier["spot_maker_bps"] if style == "maker" else tier["spot_taker_bps"]
    fee = legs_perp * p + legs_spot * s
    # Slippage is paid only on a taker cross; a resting fill earns the touch.
    slip = 0.0 if style == "maker" else (legs_perp + legs_spot) * slippage_bps_per_leg
    return {"fee_bps": fee, "slippage_bps": slip, "total_bps": fee + slip}


def breakeven_funding(total_rt_bps: float, hold_days: float,
                      capital_apr: float = 0.0) -> float:
    """Flat funding (bps per 8h print) that exactly repays the round trip and
    the capital cost over `hold_days`. Basis is excluded: it is a risk to net."""
    prints = hold_days * PRINTS_PER_DAY
    return total_rt_bps / prints + (capital_apr / 365.0 * 1e4) / PRINTS_PER_DAY


def breakeven_table(tier: Dict[str, Any], *, slippage_bps_per_leg: float,
                    holds: Sequence[float], capital_aprs: Sequence[float]
                    ) -> List[Dict[str, Any]]:
    out = []
    for mode in ("overlay", "acquire"):
        for style in ("taker", "maker"):
            rt = round_trip_bps(mode=mode, style=style, tier=tier,
                                slippage_bps_per_leg=slippage_bps_per_leg)
            for hold in holds:
                for apr in capital_aprs:
                    f = breakeven_funding(rt["total_bps"], hold, apr)
                    out.append({
                        "mode": mode, "style": style, "hold_days": hold,
                        "capital_cost_apr": apr, **rt,
                        "breakeven_funding_bps_per_8h": f,
                        "breakeven_funding_annualised_pct":
                            f * PRINTS_PER_DAY * 365.0 / 100.0})
    return out


# ---------------------------------------------------------------------------
# 3. the production gate over a plane
# ---------------------------------------------------------------------------
def gate_reason(f_bps: float, basis: float, *, rt_bps: float,
                borrow_apr: float = 0.0, opp_apr: float = 0.0,
                spot: float = 80_000.0) -> str:
    """The production gate's answer for a STEADY history of `f_bps` and a
    perp-vs-spot basis of `basis` bps. The gate is called, not re-derived."""
    v = costs.entry_gate(funding_prints_bps=[f_bps] * FUNDING_HISTORY,
                         perp=spot * (1.0 + basis / 1e4), spot=spot,
                         borrow_apr=borrow_apr, round_trip_bps=rt_bps,
                         opportunity_cost_apr=opp_apr)
    return v.reason


def reason_bands(basis: float, *, rt_bps: float, opp_apr: float = 0.0,
                 lo: float = -2.0, hi: float = 3.0, step: float = 0.002
                 ) -> List[Dict[str, Any]]:
    """Contiguous steady-funding ranges (bps/8h) sharing one gate reason."""
    bands: List[Dict[str, Any]] = []
    n = int(round((hi - lo) / step))
    for k in range(n + 1):
        f = lo + k * step
        r = gate_reason(f, basis, rt_bps=rt_bps, opp_apr=opp_apr)
        if bands and bands[-1]["reason"] == r:
            bands[-1]["to_bps"] = round(f, 6)
        else:
            bands.append({"reason": r, "from_bps": round(f, 6),
                          "to_bps": round(f, 6)})
    return bands


def basis_limits(f_bps: float, *, rt_bps: float, opp_apr: float = 0.0
                 ) -> Dict[str, Any]:
    """Widest |basis| (bps, each sign) the production gate admits at a steady
    funding, by scanning the gate itself."""
    def passes(b: float) -> bool:
        return gate_reason(f_bps, b, rt_bps=rt_bps, opp_apr=opp_apr) \
            == "CARRY_CLEARS_COSTS_AND_BASIS"
    out: Dict[str, Any] = {"funding_bps": f_bps}
    for sign, name in ((1, "max_premium_bps"), (-1, "max_discount_bps")):
        if not passes(0.0):
            out[name] = None
            continue
        lo, hi = 0.0, 500.0
        for _ in range(40):
            mid = (lo + hi) / 2
            if passes(sign * mid):
                lo = mid
            else:
                hi = mid
        out[name] = lo
    return out


def min_funding_entry_benefit(*, sd_bps: float, rt_fee_bps: float,
                              spread_bps: float, slippage_bps: float,
                              opp_apr: float = 0.0, borrow_apr: float = 0.0,
                              haircut: float = 0.0) -> float:
    """Smallest steady funding at which `strategy_evidence.evaluate_entry_
    benefit` finds a net benefit above its own one-sigma bar (the evidence
    verdict is a separate layer and is ignored here), by bisection on the
    function itself."""
    hold = float(costs.DEMONSTRATED_HOLD_DAYS)
    unc = sd_bps * PRINTS_PER_DAY * hold

    def ok(f: float) -> bool:
        d = se.evaluate_entry_benefit(
            expected_funding_bps_per_settlement=f, hold_days=hold,
            notional_usd=100.0, round_trip_fee_bps=rt_fee_bps,
            spread_bps=spread_bps, expected_slippage_bps=slippage_bps,
            borrow_apr=borrow_apr, opportunity_cost_apr=opp_apr,
            income_haircut_fraction=haircut, income_uncertainty_bps=unc,
            evidence=None, settlements_per_day=PRINTS_PER_DAY)
        return not [r for r in d.reasons
                    if not r.startswith((se.REFUSE_NO_EVIDENCE, "NET_BENEFIT_POSITIVE"))]

    lo, hi = 0.0, 1000.0
    for _ in range(60):
        mid = (lo + hi) / 2
        if ok(mid):
            hi = mid
        else:
            lo = mid
    return hi


def rejection_explanation(*, basis: float, rt_bps: float, opp_apr: float,
                          sd_bps: float, spread_bps: float, slippage_bps: float,
                          reference_basis_bps: float = 0.0) -> Dict[str, Any]:
    """Why a (steady funding, basis) pair is refused, and what would pass."""
    bands = reason_bands(basis, rt_bps=rt_bps, opp_apr=opp_apr)
    reference = reason_bands(0.0, rt_bps=rt_bps, opp_apr=opp_apr)
    passing = [b for b in bands if b["reason"] == "CARRY_CLEARS_COSTS_AND_BASIS"]
    ref_pass = [b for b in reference if b["reason"] == "CARRY_CLEARS_COSTS_AND_BASIS"]
    hold = costs.DEMONSTRATED_HOLD_DAYS
    settlements = int(math.floor(round(hold * PRINTS_PER_DAY, 9)))
    return {
        "basis_bps": basis, "round_trip_bps": rt_bps, "opportunity_cost_apr": opp_apr,
        "bands_at_this_basis": bands,
        "bands_at_zero_basis": reference,
        "min_funding_to_pass_mechanical_gate_bps": passing[0]["from_bps"] if passing else None,
        "min_funding_to_pass_at_zero_basis_bps": ref_pass[0]["from_bps"] if ref_pass else None,
        "hold_gate_min_funding_bps": breakeven_funding(
            rt_bps, settlements / PRINTS_PER_DAY, opp_apr),
        "reference_basis_bps": reference_basis_bps,
        "basis_giveback_if_it_converges_to_reference_bps": max(0.0, reference_basis_bps - basis),
        "days_of_carry_to_repay_that_giveback_at_funding": {
            str(f): (max(0.0, reference_basis_bps - basis) / (f * PRINTS_PER_DAY))
            for f in (0.2, 0.5, 1.0, 2.0)},
        "limits_at_funding": [basis_limits(f, rt_bps=rt_bps, opp_apr=opp_apr)
                              for f in (0.65, 1.0, 2.0)],
        "entry_benefit_min_funding_bps": min_funding_entry_benefit(
            sd_bps=sd_bps, rt_fee_bps=rt_bps, spread_bps=spread_bps,
            slippage_bps=slippage_bps, opp_apr=opp_apr),
        "entry_benefit_inputs": {"sd_bps": sd_bps, "spread_bps": spread_bps,
                                 "slippage_bps": slippage_bps},
        "strategy_evidence_layer": "refuses regardless: no ALLOCATE verdict exists in this repository",
    }


# ---------------------------------------------------------------------------
# 4. history on the Bybit corpus
# ---------------------------------------------------------------------------
class Settle:
    __slots__ = ("ms", "perp", "spot", "rate_bps")

    def __init__(self, ms: int, perp: float, spot: float, rate_bps: float):
        self.ms, self.perp, self.spot, self.rate_bps = ms, perp, spot, rate_bps

    @property
    def basis(self) -> float:
        return (self.perp / self.spot - 1.0) * 1e4


def load_bybit(repo: str) -> List[Settle]:
    """One row per settlement: the close of the 4h bar that ENDS at the print
    (the corpus convention), both legs, and the funding rate in bps. Refuses an
    interior hole rather than pairing across it."""
    def bars(path: str) -> Dict[int, float]:
        return {_parse_utc(r["time_period_start"]): float(r["price_close"])
                for r in _rows(os.path.join(repo, path))}
    spot, perp = bars(BYBIT_SPOT_4H), bars(BYBIT_PERP_4H)
    funding = {int(r["funding_time_ms"]): float(r["funding_rate"]) * 1e4
               for r in _rows(os.path.join(repo, BYBIT_FUNDING))}
    lo, hi = min(perp), max(perp) + H4_MS
    out: List[Settle] = []
    for ms in sorted(funding):
        if ms < lo + H4_MS or ms > hi:
            continue
        start = ms - H4_MS
        if start not in perp or start not in spot:
            raise ValueError(f"no 4h bar ends at settlement {_iso(ms)}")
        out.append(Settle(ms, perp[start], spot[start], funding[ms]))
    if not out:
        raise ValueError("no settlement overlaps the 4h bars")
    return out


def _history(rows: Sequence[Settle], i: int) -> List[float]:
    return [r.rate_bps for r in rows[max(0, i - FUNDING_HISTORY + 1): i + 1]]


def forced_hold(rows: Sequence[Settle], i: int, hold: int, *, cost_bps: float,
                capital_apr: float = 0.0) -> Optional[Dict[str, Any]]:
    """What a hedge entered after print `i` and held `hold` prints nets, in bps
    of notional: the funding of prints i+1..i+hold, plus the basis it shed
    (entry basis minus exit basis), minus the round trip and capital cost.
    None when the exit lies beyond the data (censored, never closed at the
    last price)."""
    j = i + hold
    if j >= len(rows):
        return None
    fund = sum(r.rate_bps for r in rows[i + 1: j + 1])
    b_in, b_out = rows[i].basis, rows[j].basis
    basis_pnl = b_in - b_out
    cap = capital_apr / 365.0 * 1e4 * (hold / PRINTS_PER_DAY)
    return {"i": i, "ms": rows[i].ms, "exit_ms": rows[j].ms, "basis_in": b_in,
            "basis_out": b_out, "funding_bps": fund, "basis_pnl_bps": basis_pnl,
            "cost_bps": cost_bps + cap,
            "net_bps": fund + basis_pnl - cost_bps - cap}


def all_forced(rows: Sequence[Settle], hold: int, *, cost_bps: float,
               capital_apr: float = 0.0) -> List[Dict[str, Any]]:
    out = []
    for i in range(len(rows)):
        t = forced_hold(rows, i, hold, cost_bps=cost_bps, capital_apr=capital_apr)
        if t is None:
            break
        h = _history(rows, i)
        t["ewma_bps"] = costs.ewma_funding_bps(h)
        t["hour"] = dt.datetime.fromtimestamp(rows[i].ms / 1000.0,
                                              dt.timezone.utc).hour
        out.append(t)
    return out


def gated_sequence(rows: Sequence[Settle], hold: int, *, rt_gate_bps: float,
                   cost_bps: float, capital_apr: float = 0.0,
                   gate_opp_apr: float = 0.0) -> Dict[str, Any]:
    """One position at a time: whenever flat and the PRODUCTION gate is open,
    enter, hold `hold` prints, exit, and look again at the next print. A
    forced-hold proxy for the engine, which exits on funding inversion (see
    `--engine-replay` for the engine itself)."""
    trades: List[Dict[str, Any]] = []
    reasons: Dict[str, int] = {}
    censored = 0
    i = 0
    n = len(rows)
    while i < n:
        h = _history(rows, i)
        v = costs.entry_gate(funding_prints_bps=h, perp=rows[i].perp,
                             spot=rows[i].spot, borrow_apr=0.0,
                             round_trip_bps=rt_gate_bps,
                             opportunity_cost_apr=gate_opp_apr)
        reasons[v.reason] = reasons.get(v.reason, 0) + 1
        if not v.allowed:
            i += 1
            continue
        t = forced_hold(rows, i, hold, cost_bps=cost_bps, capital_apr=capital_apr)
        if t is None:
            censored += 1
            break
        t["ewma_bps"] = v.smoothed_funding_bps
        t["expected_funding_bps"] = v.smoothed_funding_bps * hold
        trades.append(t)
        i += hold + 1
    return {"trades": trades, "gate_reasons": reasons, "censored_entries": censored}


def _period_label(ms: int, discovery_start: float, discovery_end: float) -> str:
    s = ms / 1000.0
    if s < discovery_start:
        return "before_window"
    return "inside_window" if s <= discovery_end else "after_window"


def split_report(trades: Sequence[Dict[str, Any]], *, discovery_start: float,
                 discovery_end: float) -> Dict[str, Any]:
    """Chronological splits. A trade is `before_window` only if it ALSO EXITS
    before the window opens."""
    def lab(t: Dict[str, Any]) -> str:
        a = _period_label(t["ms"], discovery_start, discovery_end)
        b = _period_label(t["exit_ms"], discovery_start, discovery_end)
        return a if a == b else "straddles_window_start"
    groups: Dict[str, List[float]] = {}
    for t in trades:
        groups.setdefault(lab(t), []).append(t["net_bps"])
    by_year: Dict[str, List[float]] = {}
    for t in trades:
        by_year.setdefault(str(_year(t["ms"])), []).append(t["net_bps"])
    eras = {"2022-08..2024-12": [], "2025-01..2026-09": []}
    for t in trades:
        eras["2022-08..2024-12" if _year(t["ms"]) <= 2024 else "2025-01..2026-09"] \
            .append(t["net_bps"])
    return {
        "relative_to_discovery_window": {k: summarise(v) for k, v in groups.items()},
        "by_year": {k: summarise(v) for k, v in sorted(by_year.items())},
        "early_vs_late_not_independent": {k: summarise(v) for k, v in eras.items()},
        "out_of_sample_trades_after_window_end": len(
            [t for t in trades if t["ms"] / 1000.0 > discovery_end])}


def bucket_table(entries: Sequence[Dict[str, Any]], key: str,
                 edges: Sequence[float]) -> List[Dict[str, Any]]:
    out = []
    bounds = [-math.inf, *edges, math.inf]
    for lo, hi in zip(bounds[:-1], bounds[1:]):
        sel = [e for e in entries if e.get(key) is not None and lo <= e[key] < hi]
        row = {"lo": None if lo == -math.inf else lo,
               "hi": None if hi == math.inf else hi, "n": len(sel)}
        if sel:
            row.update({
                "mean_funding_bps": statistics.fmean(e["funding_bps"] for e in sel),
                "mean_basis_pnl_bps": statistics.fmean(e["basis_pnl_bps"] for e in sel),
                "mean_net_bps": statistics.fmean(e["net_bps"] for e in sel),
                "share_net_positive": sum(1 for e in sel if e["net_bps"] > 0) / len(sel)})
        out.append(row)
    return out


def threshold_by_year(entries: Sequence[Dict[str, Any]], thresholds: Sequence[float]
                      ) -> Dict[str, Any]:
    """Forced-hold net of every entry whose EWMA funding was at least each
    threshold, by entry year: where, and when, the positive nets were earned."""
    out: Dict[str, Any] = {}
    for th in thresholds:
        sel = [e for e in entries if e.get("ewma_bps") is not None and e["ewma_bps"] >= th]
        years = sorted({_year(e["ms"]) for e in sel})
        out[str(th)] = {
            "n": len(sel),
            "mean_net_bps": statistics.fmean(e["net_bps"] for e in sel) if sel else None,
            "by_year": {str(y): {"n": len([e for e in sel if _year(e["ms"]) == y]),
                                 "mean_net_bps": statistics.fmean(
                                     e["net_bps"] for e in sel if _year(e["ms"]) == y)}
                        for y in years}}
    return out


def trailing_year(entries: Sequence[Dict[str, Any]], last_ms: int, floor_bps: float
                  ) -> Dict[str, Any]:
    """Forced-hold net of entries in the final 365 days of the corpus whose
    EWMA funding was at least `floor_bps` (about where the production gate
    opens)."""
    sel = [e for e in entries if e["ms"] >= last_ms - 365 * DAY_MS
           and e.get("ewma_bps") is not None and e["ewma_bps"] >= floor_bps]
    return {"floor_bps": floor_bps, **summarise([e["net_bps"] for e in sel])}


def single_print_ceiling(rows: Sequence[Settle], cost_levels: Sequence[float]
                         ) -> Dict[str, Any]:
    """Settlement-window harvest: hold across ONE print and leave. Even with
    perfect foresight of which prints to hold (which no one has), a print must
    exceed the whole round trip. Counts those prints and sums their excess: an
    UPPER BOUND on the strategy, not an estimate of it."""
    out: Dict[str, Any] = {"max_print_bps": max(r.rate_bps for r in rows),
                           "perfect_foresight_note": "upper bound; ignores basis, fill risk and timing"}
    for c in cost_levels:
        over = [r.rate_bps - c for r in rows if r.rate_bps > c]
        out[str(c)] = {"prints_above_round_trip": len(over),
                       "sum_excess_bps_over_the_whole_corpus": sum(over)}
    return out


def hold_cost_matrix(rows: Sequence[Settle], holds: Sequence[int],
                     scenarios: Dict[str, float]) -> List[Dict[str, Any]]:
    """Net bps of a forced hold of `hold` prints entered after EVERY print (unconditional: no gate, no foresight), for each cost scenario (name -> round-trip
    bps). One print = one 8h settlement in this corpus. Overlapping windows are serially dependent: the interval is a moving-block bootstrap (block >= 2 holds),
    and n counts overlapping entries, not independent trials. IN-SAMPLE, funding + the basis shed, price legs hedged by construction (spot owned)."""
    out: List[Dict[str, Any]] = []
    for h in holds:
        for name, cost in scenarios.items():
            t = all_forced(rows, h, cost_bps=cost)
            net = [e["net_bps"] for e in t]
            s = summarise(net)
            out.append({"hold_prints": h, "hold_days": h / PRINTS_PER_DAY, "scenario": name, "round_trip_cost_bps": cost, "summary": s,
                        "mean_block_ci90": block_bootstrap_mean(net, block=max(2 * h, 2)) if len(net) >= 4 * max(h, 1) else None,
                        "mean_funding_bps": statistics.fmean(e["funding_bps"] for e in t) if t else None,
                        "mean_basis_pnl_bps": statistics.fmean(e["basis_pnl_bps"] for e in t) if t else None})
    return out


def funding_persistence(rows: Sequence[Settle], hold: int,
                        cost_levels: Sequence[float] = (costs.ROUND_TRIP_FEE_BPS_PERP_OVERLAY_MAKER_MODELLED,
                                                        costs.ROUND_TRIP_FEE_BPS_PERP_OVERLAY,
                                                        costs.ROUND_TRIP_FEE_BPS_PERP_OVERLAY + 2.0)   # fee + ASSUMED 1 bp/leg slippage (the map's default)
                        ) -> Dict[str, Any]:
    """How well does the gate's smoothed rate predict the funding actually
    collected over the hold? OLS of realised hold-funding on the EWMA at entry."""
    xs, ys = [], []
    for i in range(len(rows) - hold):
        e = costs.ewma_funding_bps(_history(rows, i))
        if e is None:
            continue
        xs.append(e)
        ys.append(sum(r.rate_bps for r in rows[i + 1: i + hold + 1]) / hold)
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    syy = sum((y - my) ** 2 for y in ys)
    slope = sxy / sxx if sxx else 0.0
    intercept = my - slope * mx
    return {"n": len(xs), "slope": slope, "intercept": intercept,
            "r_squared": (sxy * sxy / (sxx * syy)) if sxx and syy else 0.0,
            "empirical_breakeven_ewma_bps_by_round_trip_cost": {
                str(c): ((c / hold - intercept) / slope if slope > 0 else None)
                for c in cost_levels},
            "note": "regression of mean funding over the next hold on the EWMA at entry; "
                    "overlapping windows, so n overstates independent information"}


def funding_formula_check(rows: Sequence[Settle]) -> Dict[str, Any]:
    """Bybit's funding is premium + a clamp around a 1 bps interest term: when
    the printed rate is below that 1 bps floor, it should sit ~5 bps above the
    same-moment basis. Check it on the data."""
    below = [r.rate_bps - r.basis for r in rows if r.rate_bps < 0.95]
    xs = [r.basis for r in rows]
    ys = [r.rate_bps for r in rows]
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    vx = sum((x - mx) ** 2 for x in xs)
    vy = sum((y - my) ** 2 for y in ys)
    return {"n_below_1bp": len(below),
            "mean_rate_minus_basis_when_rate_below_1bp": statistics.fmean(below) if below else None,
            "corr_rate_vs_basis": cov / math.sqrt(vx * vy) if vx and vy else None,
            "reading": "if the offset is ~5 bps, a deep perp DISCOUNT means a low or NEGATIVE "
                       "funding rate, so the discount and the funding are not independent facts"}


def yearly_regime(rows: Sequence[Settle], rt_gate_bps: float, hold: int
                  ) -> List[Dict[str, Any]]:
    by: Dict[int, List[int]] = {}
    for i, r in enumerate(rows):
        by.setdefault(_year(r.ms), []).append(i)
    be_prod = breakeven_funding(rt_gate_bps, hold / PRINTS_PER_DAY)
    out = []
    for y, idx in sorted(by.items()):
        f = [rows[i].rate_bps for i in idx]
        b = [rows[i].basis for i in idx]
        open_ = 0
        ew_ok = 0
        for i in idx:
            h = _history(rows, i)
            e = costs.ewma_funding_bps(h)
            if e is not None and e >= be_prod:
                ew_ok += 1
            v = costs.entry_gate(funding_prints_bps=h, perp=rows[i].perp,
                                 spot=rows[i].spot, borrow_apr=0.0,
                                 round_trip_bps=rt_gate_bps)
            open_ += 1 if v.allowed else 0
        out.append({
            "year": y, "prints": len(idx),
            "mean_funding_bps": statistics.fmean(f),
            "median_funding_bps": statistics.median(f),
            "mean_funding_annualised_pct": statistics.fmean(f) * PRINTS_PER_DAY * 365 / 100,
            "share_positive": sum(1 for x in f if x > 0) / len(f),
            "mean_basis_bps": statistics.fmean(b), "sd_basis_bps": statistics.pstdev(b),
            "min_basis_bps": min(b), "max_basis_bps": max(b),
            "share_ewma_above_hold_breakeven": ew_ok / len(idx),
            "share_production_gate_open": open_ / len(idx)})
    return out


def window_timing(rows: Sequence[Settle]) -> Dict[str, Any]:
    """Basis and funding by settlement hour (00/08/16 UTC) on the Bybit corpus.
    The entry-window effect on basis P&L is just the difference of these means
    (entry hour mean basis minus exit hour mean basis)."""
    groups: Dict[int, List[Settle]] = {0: [], 8: [], 16: []}
    for r in rows:
        h = dt.datetime.fromtimestamp(r.ms / 1000.0, dt.timezone.utc).hour
        groups.setdefault(h, []).append(r)
    out = {}
    for h, rs in sorted(groups.items()):
        if not rs:
            continue
        b = [r.basis for r in rs]
        f = [r.rate_bps for r in rs]
        blk = block_bootstrap_mean(b, block=3)
        out[f"{h:02d}:00"] = {
            "n": len(rs), "mean_basis_bps": statistics.fmean(b),
            "sd_basis_bps": statistics.stdev(b), "mean_funding_bps": statistics.fmean(f),
            "basis_mean_ci90": blk}
    means = [v["mean_basis_bps"] for v in out.values()]
    return {"by_settlement_hour": out,
            "spread_of_hour_means_bps": (max(means) - min(means)) if means else None}


def binance_window_timing(repo: str) -> Optional[Dict[str, Any]]:
    """The same hour split on the longer Binance 8h corpus (2019-2026)."""
    ps = os.path.join(repo, BINANCE_8H_DIR, "BINANCE_PERP_BTCUSDT_8H.csv.gz")
    ss = os.path.join(repo, BINANCE_8H_DIR, "BINANCE_SPOT_BTCUSDT_8H.csv.gz")
    if not (os.path.exists(ps) and os.path.exists(ss)):
        return None
    perp = {int(r["open_time_ms"]): float(r["price_close"]) for r in _rows(ps)}
    spot = {int(r["open_time_ms"]): float(r["price_close"]) for r in _rows(ss)}
    by: Dict[int, List[float]] = {}
    for k in sorted(set(perp) & set(spot)):
        settle_ms = k + 8 * 3_600_000       # the bar ENDS at the settlement
        h = dt.datetime.fromtimestamp(settle_ms / 1000.0, dt.timezone.utc).hour
        by.setdefault(h, []).append((perp[k] / spot[k] - 1.0) * 1e4)
    return {h: {"n": len(v), "mean_basis_bps": statistics.fmean(v),
                "sd_basis_bps": statistics.stdev(v)}
            for h, v in sorted(by.items())}


# ---------------------------------------------------------------------------
# 5. lot economics, 6. benchmarks
# ---------------------------------------------------------------------------
def lot_economics(*, price: float, cap_usd: float, lot_btc: float,
                  tier: Dict[str, Any], slippage_bps_per_leg: float
                  ) -> Dict[str, Any]:
    notional = lot_btc * price
    tk = round_trip_bps(mode="overlay", style="taker", tier=tier,
                        slippage_bps_per_leg=slippage_bps_per_leg)
    mk = round_trip_bps(mode="overlay", style="maker", tier=tier,
                        slippage_bps_per_leg=slippage_bps_per_leg)
    usd = lambda bps: bps / 1e4 * notional  # noqa: E731
    return {
        "price_usd": price, "lot_btc": lot_btc, "one_lot_notional_usd": notional,
        "cap_usd": cap_usd, "fits_under_cap": notional <= cap_usd,
        "price_above_which_one_lot_exceeds_cap_usd": cap_usd / lot_btc,
        "lots_that_fit_under_cap": int(cap_usd // (lot_btc * price)),
        "round_trip_taker_usd": usd(tk["total_bps"]),
        "round_trip_maker_best_case_usd": usd(mk["total_bps"]),
        "funding_usd_per_8h_at": {str(f): usd(f) for f in (0.2, 0.5, 1.0, 2.0)},
        "funding_usd_per_year_at": {str(f): usd(f) * PRINTS_PER_DAY * 365
                                    for f in (0.2, 0.5, 1.0, 2.0)},
        "granularity": ("the hedge is ONE lot: it cannot be scaled, trimmed or "
                        "rebalanced in steps - a 2% delta band is finer than the lot, so "
                        "delta can only be re-hedged by closing or doubling the position")}


def cash_benchmark(repo: str, yearly: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    path = os.path.join(repo, OKX_LENDING)
    if not os.path.exists(path):
        return {"available": False}
    by: Dict[str, List[float]] = {}
    for r in _rows(path):
        by.setdefault(r["utc"][:4], []).append(float(r["rate_annual"]))
    cash = {y: 100.0 * statistics.fmean(v) for y, v in sorted(by.items())}
    rows = []
    for y in yearly:
        c = cash.get(str(y["year"]))
        rows.append({"year": y["year"],
                     "bybit_funding_annualised_pct": y["mean_funding_annualised_pct"],
                     "okx_usdt_lending_pct": c,
                     "funding_minus_cash_pct": None if c is None else
                     y["mean_funding_annualised_pct"] - c})
    return {"available": True, "rows": rows,
            "caveat": ("OKX lending is what a LENDER earns, a floor on a borrower's cost, on a "
                       "different venue, and the overlay's true opportunity cost is the owner's "
                       "(CARRY_OPPORTUNITY_COST_APR is unset = 0). This is a benchmark, not a "
                       "charge; the production gate currently charges 0.")}


def breadth(repo: str) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for sym in ("BTC", "ETH", "SOL"):
        p = os.path.join(repo, BINANCE_FUNDING_DIR, f"BINANCE_LINEAR_{sym}_USDT_FUNDING.csv.gz")
        if not os.path.exists(p):
            continue
        by: Dict[int, List[float]] = {}
        for r in _rows(p):
            by.setdefault(int(r["funding_time"][:4]), []).append(float(r["funding_rate"]) * 1e4)
        out[sym] = {str(y): {"annualised_pct": statistics.fmean(v) * 1095 / 100,
                             "share_positive": sum(1 for x in v if x > 0) / len(v)}
                    for y, v in sorted(by.items())}
    return out


def cross_venue(repo: str, rows: Sequence[Settle], leg_cost_bps: float) -> Dict[str, Any]:
    p = os.path.join(repo, BINANCE_FUNDING_DIR, "BINANCE_LINEAR_BTC_USDT_FUNDING.csv.gz")
    if not os.path.exists(p):
        return {"available": False}
    bn = {int(r["funding_time_ms"]): float(r["funding_rate"]) * 1e4 for r in _rows(p)}
    diffs = [bn[r.ms] - r.rate_bps for r in rows if r.ms in bn]
    absd = [abs(d) for d in diffs]
    by: Dict[int, List[float]] = {}
    for r in rows:
        if r.ms in bn:
            by.setdefault(_year(r.ms), []).append(abs(bn[r.ms] - r.rate_bps))
    hold_prints = HOLD_PRINTS
    four_legs = 4 * leg_cost_bps
    return {"n_matched_prints": len(diffs),
            "mean_abs_differential_bps_per_8h": statistics.fmean(absd),
            "mean_abs_by_year": {str(y): statistics.fmean(v) for y, v in sorted(by.items())},
            "four_leg_round_trip_taker_bps": four_legs,
            "breakeven_hold_prints_at_mean_abs_differential":
                four_legs / statistics.fmean(absd),
            "five_point_eight_day_income_bps": statistics.fmean(absd) * hold_prints}


# ---------------------------------------------------------------------------
# optional: the real engine, through the existing replay driver
# ---------------------------------------------------------------------------
def engine_replay(repo: str, *, discovery_start: float, discovery_end: float,
                  notional: float = 100_000.0) -> Dict[str, Any]:
    import carry_backtest as cb
    import carry_replay as cr
    rows, meta = cb.load_bybit_settlements(repo)
    impact = cb.impact_bps_per_leg(repo, notional)[0]
    out: Dict[str, Any] = {"evidence_class": HISTORICAL, "corpus": meta["corpus"],
                           "notional_usd": notional,
                           "not_production_equivalent": "liquidation gate NOT_EVALUATED; "
                           "fills at the decision price; every maker rest is assumed to fill"}
    for style in ("taker", "maker_first"):
        r = cr.replay(rows, notional=notional, borrow_apr=0.0, execution_mode="overlay",
                      execution_style=style, impact_bps=impact)
        tr = r["trade_rows"]
        # Per-trade net = funding + both legs' price move, less the ledger's
        # fees allocated pro rata to notional traded (the ledger does not book
        # fees per trade). An open trade at the end is included, marked.
        weights = [t["qty"] * (t["entry_perp"] + t["exit_perp"]) for t in tr]
        wsum = sum(weights) or 1.0
        eras: Dict[str, List[float]] = {"2022-08..2024-12": [], "2025-01..2026-09": []}
        days = []
        for t, w in zip(tr, weights):
            net = (t["funding"] + t["qty"] * (t["entry_perp"] - t["exit_perp"])
                   + t["spot_qty"] * (t["exit_spot"] - t["entry_spot"])
                   + r["fees_usd"] * (w / wsum))          # fees_usd is negative
            eras["2022-08..2024-12" if _year(t["opened_ms"]) <= 2024
                 else "2025-01..2026-09"].append(net)
            days.append((t["closed_ms"] - t["opened_ms"]) / DAY_MS)
        cut = _parse_utc("2025-01-01T00:00:00Z")
        yrs = {"2022-08..2024-12": (cut - rows[0].ms) / (365.0 * DAY_MS),
               "2025-01..2026-09": (rows[-1].ms - cut) / (365.0 * DAY_MS)}
        era_out = {k: {**summarise(v),
                       "net_annualised_pct_of_notional": 100.0 * sum(v) / notional / yrs[k]}
                   for k, v in eras.items()}
        out[style] = {
            "net_usd": r["net_usd"], "net_annualised_pct": r["net_annualised_pct"],
            "funding_usd": r["funding_usd"], "basis_usd": r["basis_usd"],
            "fees_usd": r["fees_usd"], "trades": r["trades"],
            "losing_trade_pct": r["concentration"]["losing_trade_pct"],
            "best_trade_share_of_net": r["concentration"]["best_trade_share_of_net"],
            "top5_share_of_net": r["concentration"]["top5_share_of_net"],
            "hold_days": {"median": statistics.median(days), "mean": statistics.fmean(days)},
            "trade_net_after_allocated_fees_by_era_usd": era_out,
            "trades_entered_after_window_end": len([t for t in tr if t["opened_ms"] / 1000.0 > discovery_end]),
            "reasons": r["reasons"]}
    return out


# ---------------------------------------------------------------------------
# the report
# ---------------------------------------------------------------------------
def build_report(repo: str, *, discovery_start: float, discovery_end: float,
                 slippage_bps_per_leg: float, with_engine: bool,
                 nov2_basis: float = -27.0) -> Dict[str, Any]:
    tier = fee_tier()
    rows = load_bybit(repo)
    tk = round_trip_bps(mode="overlay", style="taker", tier=tier,
                        slippage_bps_per_leg=slippage_bps_per_leg)
    gate_rt = 2.0 * tier["perp_taker_bps"]        # what the engine hands the gate
    hold = HOLD_PRINTS
    cost_taker = tk["total_bps"]
    cost_maker = round_trip_bps(mode="overlay", style="maker", tier=tier,
                                slippage_bps_per_leg=slippage_bps_per_leg)["total_bps"]

    report: Dict[str, Any] = {
        "tool": "tools/opportunity_map.py",
        "evidence_summary": (
            f"{HISTORICAL} and {ARITHMETIC} only. The corpus ends "
            f"{_iso(rows[-1].ms)}; the owner's window ends {_iso(int(discovery_end * 1000))}; "
            f"settlements on disk after the window: "
            f"{len([r for r in rows if r.ms / 1000.0 > discovery_end])}."),
        "inventory": inventory(repo, discovery_start=discovery_start,
                               discovery_end=discovery_end),
        "costs": {
            "evidence_class": ARITHMETIC, "fee_tier": tier,
            "slippage_bps_per_leg_assumed": slippage_bps_per_leg,
            "table": breakeven_table(tier, slippage_bps_per_leg=slippage_bps_per_leg,
                                     holds=(2.9, costs.DEMONSTRATED_HOLD_DAYS, 11.6, 30.0),
                                     capital_aprs=(0.0, 0.035)),
            "production_gate_round_trip_bps": gate_rt,
            "production_hold_gate_min_funding_bps": gate_rt / HOLD_PRINTS,
        },
    }
    sd8 = statistics.median(
        [statistics.stdev(_history(rows, i)) for i in range(FUNDING_HISTORY, len(rows))
         if len(_history(rows, i)) > 1])
    report["gate"] = {
        "evidence_class": ARITHMETIC,
        "nov2_style_case": rejection_explanation(
            basis=nov2_basis, rt_bps=gate_rt, opp_apr=0.0, sd_bps=sd8,
            spread_bps=0.013, slippage_bps=slippage_bps_per_leg * 2,
            reference_basis_bps=statistics.fmean(r.basis for r in rows[-PRINTS_PER_DAY * 365:])),
        "median_sd_of_8_print_window_bps": sd8,
        "basis_edge_cases": {str(b): [
            {"reason": x["reason"], "from_bps": x["from_bps"], "to_bps": x["to_bps"]}
            for x in reason_bands(b, rt_bps=gate_rt)] for b in (-27.0, -5.0, 0.0, 20.0)}}

    # -- history ---------------------------------------------------------------
    uncond = all_forced(rows, hold, cost_bps=cost_taker)
    uncond_maker = all_forced(rows, hold, cost_bps=cost_maker)
    seq = gated_sequence(rows, hold, rt_gate_bps=gate_rt, cost_bps=cost_taker)
    seq_cash = gated_sequence(rows, hold, rt_gate_bps=gate_rt, cost_bps=cost_taker,
                              capital_apr=0.035, gate_opp_apr=0.035)
    seq_maker = gated_sequence(rows, hold, rt_gate_bps=gate_rt, cost_bps=cost_maker)
    pred = [t["expected_funding_bps"] for t in seq["trades"]]
    real = [t["funding_bps"] for t in seq["trades"]]
    report["history"] = {
        "evidence_class": HISTORICAL,
        "in_sample": True,
        "corpus": BYBIT_DIR,
        "settlements": len(rows),
        "first_utc": _iso(rows[0].ms), "last_utc": _iso(rows[-1].ms),
        "settlements_after_discovery_window_end": len(
            [r for r in rows if r.ms / 1000.0 > discovery_end]),
        "settlements_inside_discovery_window": len(
            [r for r in rows if discovery_start <= r.ms / 1000.0 <= discovery_end]),
        "forced_hold_prints": hold,
        "assumed_cost_bps": {"taker_round_trip": cost_taker,
                             "maker_round_trip_best_case": cost_maker},
        "yearly_regime": yearly_regime(rows, gate_rt, hold),
        "unconditional_every_print_taker": {
            "summary": summarise([e["net_bps"] for e in uncond]),
            "mean_block_ci90": block_bootstrap_mean([e["net_bps"] for e in uncond],
                                                    block=2 * hold),
            "by_year": {str(y): summarise([e["net_bps"] for e in uncond if _year(e["ms"]) == y])
                        for y in sorted({_year(e["ms"]) for e in uncond})}},
        "unconditional_every_print_maker_best_case": {
            "summary": summarise([e["net_bps"] for e in uncond_maker])},
        "gated_sequential_taker": {
            "gate_reasons": seq["gate_reasons"],
            "summary": summarise([t["net_bps"] for t in seq["trades"]]),
            "mean_iid_ci90": block_bootstrap_mean([t["net_bps"] for t in seq["trades"]],
                                                  block=1, resamples=BOOTSTRAP_RESAMPLES)
            if len(seq["trades"]) >= 4 else None,
            "components_mean_bps": {
                k: statistics.fmean(t[k] for t in seq["trades"])
                for k in ("funding_bps", "basis_pnl_bps", "cost_bps", "net_bps")}
            if seq["trades"] else None,
            "realised_over_predicted_funding": (sum(real) / sum(pred)) if pred and sum(pred) else None,
            "splits": split_report(seq["trades"], discovery_start=discovery_start,
                                   discovery_end=discovery_end)},
        "gated_sequential_maker_best_case": {
            "summary": summarise([t["net_bps"] for t in seq_maker["trades"]])},
        "gated_sequential_taker_with_3_5pct_cash_cost": {
            "gate_reasons": seq_cash["gate_reasons"],
            "summary": summarise([t["net_bps"] for t in seq_cash["trades"]]),
            "splits": split_report(seq_cash["trades"], discovery_start=discovery_start,
                                   discovery_end=discovery_end)},
        "by_entry_basis_unconditional_taker": bucket_table(
            uncond, "basis_in", (-10.0, -5.0, 0.0, 5.0, 10.0)),
        "by_entry_ewma_unconditional_taker": bucket_table(
            uncond, "ewma_bps", (0.0, 0.3, 0.65, 1.0, 1.5)),
        "entries_by_ewma_threshold_taker": threshold_by_year(uncond, (0.65, 1.0, 1.5)),
        "entries_by_ewma_threshold_maker_best_case": threshold_by_year(uncond_maker, (0.65, 1.0, 1.5)),
        "trailing_365d_entries_at_gate_floor_taker": trailing_year(
            uncond, rows[-1].ms, gate_rt / HOLD_PRINTS),
        "funding_2026": {"max_bps": max(r.rate_bps for r in rows if _year(r.ms) == 2026),
                         "share_at_or_above_1bp": (
                             sum(1 for r in rows if _year(r.ms) == 2026 and r.rate_bps >= 1.0)
                             / max(1, len([r for r in rows if _year(r.ms) == 2026])))},
        "single_print_harvest_ceiling": single_print_ceiling(rows, (cost_maker, costs.ROUND_TRIP_FEE_BPS_PERP_OVERLAY, cost_taker)),
        "funding_persistence": funding_persistence(rows, hold),
        "funding_formula_check": funding_formula_check(rows),
        "window_timing_bybit": window_timing(rows),
        "window_timing_binance_8h": binance_window_timing(repo),
        "basis_distribution": {
            "min_bps": min(r.basis for r in rows), "max_bps": max(r.basis for r in rows),
            "share_at_or_below_minus_27bps": sum(1 for r in rows if r.basis <= -27.0) / len(rows),
            "share_at_or_below_minus_14bps": sum(1 for r in rows if r.basis <= -14.0) / len(rows)},
    }
    last_spot = rows[-1].spot
    report["lot"] = {"evidence_class": ARITHMETIC,
                     **lot_economics(price=last_spot, cap_usd=100.0, lot_btc=0.001, tier=tier,
                                     slippage_bps_per_leg=slippage_bps_per_leg),
                     "price_basis": f"last spot close on disk ({_iso(rows[-1].ms)})"}
    report["benchmark"] = {
        "evidence_class": HISTORICAL,
        "cash": cash_benchmark(repo, report["history"]["yearly_regime"]),
        "breadth_binance_funding": breadth(repo),
        "cross_venue_btc": cross_venue(repo, rows, tier["perp_taker_bps"])}
    if with_engine:
        report["engine_replay"] = engine_replay(
            repo, discovery_start=discovery_start, discovery_end=discovery_end)
    return report


def _print(rep: Dict[str, Any]) -> None:
    print("=" * 78)
    print("OPPORTUNITY MAP - offline, read-only, IN-SAMPLE (no edge is claimed)")
    print("=" * 78)
    print(rep["evidence_summary"])
    inv = rep["inventory"]
    print(f"\nINVENTORY: {inv['real_files']} real files, {inv['synthetic_files']} synthetic; "
          f"real files with data after the window end: {inv['real_files_with_data_after_window_end']}; "
          f"latest real observation {inv['latest_real_observation_utc']}")
    print("\nBREAK-EVEN FUNDING (bps per 8h print) - overlay, slippage "
          f"{rep['costs']['slippage_bps_per_leg_assumed']} bps/leg")
    for r in rep["costs"]["table"]:
        if r["mode"] == "overlay" and r["capital_cost_apr"] == 0.0:
            print(f"  {r['style']:<6} hold {r['hold_days']:>5}d  rt {r['total_bps']:>5.1f} bps "
                  f"-> {r['breakeven_funding_bps_per_8h']:.3f} bps/8h "
                  f"({r['breakeven_funding_annualised_pct']:.1f}%/yr)")
    g = rep["gate"]["nov2_style_case"]
    print(f"\nGATE at basis {g['basis_bps']} bps (steady funding):")
    for b in g["bands_at_this_basis"]:
        print(f"  {b['from_bps']:+.3f}..{b['to_bps']:+.3f}  {b['reason']}")
    print(f"  entry-benefit gate needs steady funding >= {g['entry_benefit_min_funding_bps']:.3f} "
          f"bps (+ an ALLOCATE verdict that does not exist)")
    h = rep["history"]
    print("\nHISTORY (Bybit corpus, forced 17-print hold, taker):")
    for y in h["yearly_regime"]:
        print(f"  {y['year']}: funding {y['mean_funding_annualised_pct']:5.2f}%/yr "
              f"basis {y['mean_basis_bps']:+.1f} gate-open {100 * y['share_production_gate_open']:.0f}%")
    gs = h["gated_sequential_taker"]["summary"]
    print(f"  gated sequential: n={gs.get('n')} mean net {gs.get('mean', float('nan')):.1f} bps")
    tr = h["trailing_365d_entries_at_gate_floor_taker"]
    print(f"  trailing 365d entries at the gate floor: n={tr.get('n')} "
          f"mean net {tr.get('mean', float('nan')):.1f} bps; 2026 max print "
          f"{h['funding_2026']['max_bps']:.2f} bps")
    print("  out-of-sample trades after the window end:",
          h["gated_sequential_taker"]["splits"]["out_of_sample_trades_after_window_end"])
    if "engine_replay" in rep:
        for style in ("taker", "maker_first"):
            e = rep["engine_replay"][style]
            print(f"  engine {style}: net ${e['net_usd']:,.0f} ({e['net_annualised_pct']:+.2f}%/yr) "
                  f"trades {e['trades']}")


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", default=ROOT)
    ap.add_argument("--discovery-start", type=float, default=DISCOVERY_START_EPOCH)
    ap.add_argument("--discovery-end", type=float, default=DISCOVERY_END_EPOCH)
    ap.add_argument("--slippage-bps-per-leg", type=float, default=1.0,
                    help="DECLARED, not measured (the forward plan's working assumption)")
    ap.add_argument("--nov2-basis", type=float, default=-27.0)
    ap.add_argument("--engine-replay", action="store_true",
                    help="also drive the real engine through the corpus (slower)")
    ap.add_argument("--json", default="")
    args = ap.parse_args(argv)
    if not args.discovery_start < args.discovery_end:
        ap.error("the discovery window's start must be before its end")
    rep = build_report(args.repo, discovery_start=args.discovery_start,
                       discovery_end=args.discovery_end,
                       slippage_bps_per_leg=args.slippage_bps_per_leg,
                       with_engine=args.engine_replay, nov2_basis=args.nov2_basis)
    _print(rep)
    if args.json:
        with open(args.json, "w", encoding="utf-8") as handle:
            json.dump(rep, handle, indent=2, default=str)
        print(f"\nwrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
