#!/usr/bin/env python3
"""RESEARCH ONLY: the pre-registered Binance-archive proxy study of spot-hedged funding carry (protocol ``research/proxy_studies/binance_archive_carry_v1.json``).

It answers one question about a MECHANISM on the deepest venue: does a spot-hedged top-k funding carry, among liquid Binance USD-M USDT perpetuals that have a Binance spot pair, earn a positive excess over cash on
the capital it commits, after Bybit-style taker costs? It is not evidence about Bybit, about owner dollars or about capacity, and it can never promote anything: a survivor is a HYPOTHESIS for Bybit's own data.

Design (each point answers a finding of the two independent reviews that preceded the registration):
  * The repository's own row builder (``candidate_readiness.run_family_variant``) makes the weekly rows, on a point-in-time universe re-derived at every decision from complete days before it. The builder is run with
    ZERO cost, so a row is pure funding; each bound then adds its own cost, measured basis, capital and cash. Nothing here writes to a real holdout registry or ledger.
  * Two bounds are fixed in advance and neither is tuned. ADVERSE: a full taker round trip every week plus slippage by volume tier, capital 2.04, the cash series extended backwards by its first 90 days.
    FAVOURABLE: the taker fee only, charged only on turnover, capital 1.224 (leverage 5), zero cash before the series begins. NO WINNER needs the favourable bound's 90% interval to end at or below zero;
    a survivor needs the adverse bound to pass and the final read of the reserved months.
  * Data guards: funding times are snapped to their stated grid (a print more than 1 s off is dropped and counted); a hole in the prints makes a symbol ineligible for the week; a contract is DELIVERED at its last
    funding print (a halted contract's marks can stay frozen for a year); one symbol per base; a spot pair is required (this also excludes index, equity and pre-listing perpetuals); months after the cut are never read.
  * The reserved months are not downloaded until ``final-read``; the explore run reads only months at or before the protocol's last explore month and refuses a store row at or after the cut.

    python3 tools/proxy_carry_study.py run --root STORE --out report.json --ledger-dir DIR      (offline: reads the store)
"""
from __future__ import annotations

import argparse
import bisect
import datetime as dt
import hashlib
import json
import math
import os
import statistics
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

BOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (BOT, os.path.join(BOT, "tools")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import binance_archive as BA  # noqa: E402
import candidate_readiness as CR  # noqa: E402
import cash_basis as CB  # noqa: E402
import research_harness as RH  # noqa: E402
import statistical_power as SP  # noqa: E402

EXPECTED_PROTOCOL_DIGEST = "54b9f5acbbe8e3e48ae35ac8039b13ccf4249a3931d51c239ac08cd9f8db9559"                      # the digest of the REGISTERED protocol: a run on any other file is marked NOT PRE-REGISTERED (or refused)
SCHEMA = "proxy_carry_study/v1"
LABEL = "PROXY_RESEARCH_ONLY"
PROTOCOL_PATH = os.path.join(BOT, "research", "proxy_studies", "binance_archive_carry_v1.json")
MS_DAY = 86_400_000
MS_HOUR = 3_600_000
FAMILY = CR.FAMILY_CARRY


class StudyError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


# ---------------------------------------------------------------- the protocol
def load_protocol(path: str = PROTOCOL_PATH) -> Tuple[Dict[str, Any], str]:
    """(protocol, sha256 of its canonical JSON). Every result prints the digest: a changed protocol is a different study."""
    with open(path, encoding="utf-8") as fh:
        proto = json.load(fh)
    if proto.get("schema") != "proxy_study_protocol/v1" or proto.get("label") != LABEL or "parameters" not in proto:
        raise StudyError("PROTOCOL_INVALID", "not a proxy study protocol with a parameter block")
    return proto, protocol_digest(proto)


def protocol_digest(proto: Mapping[str, Any]) -> str:
    """sha256 of the canonical JSON of a protocol: any change to any number or word changes it."""
    return hashlib.sha256(json.dumps(proto, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")).hexdigest()


def month_start_ms(ym: str) -> int:
    return int(dt.datetime(int(ym[:4]), int(ym[5:7]), 1, tzinfo=dt.timezone.utc).timestamp() * 1000)


def day_ms(iso: str) -> int:
    d = dt.date.fromisoformat(iso)
    return int(dt.datetime(d.year, d.month, d.day, tzinfo=dt.timezone.utc).timestamp() * 1000)


def ym_of(ms: int) -> str:
    d = dt.datetime.fromtimestamp(ms / 1000.0, tz=dt.timezone.utc)
    return f"{d.year:04d}-{d.month:02d}"


def year_of(ms: int) -> int:
    return dt.datetime.fromtimestamp(ms / 1000.0, tz=dt.timezone.utc).year


# ---------------------------------------------------------------- names: base, multiplier, spot pair
def strip_multiplier(base: str, prefixes: Sequence[str]) -> str:
    for p in sorted(prefixes, key=len, reverse=True):
        rest = base[len(p):]
        if base.startswith(p) and len(rest) >= 2 and rest[0].isalpha():          # a multiplier is followed by a ticker, which starts with a letter (a rest like "0X" is not a ticker)
            return rest
    return base


def perp_base(symbol: str) -> Optional[str]:
    return symbol[:-4] if symbol.endswith("USDT") and len(symbol) >= 6 else None                 # a base has at least two characters (OP, AR, ID ...)


def spot_candidates(symbol: str, prefixes: Sequence[str]) -> List[str]:
    """The spot symbols a USDT perpetual may hedge with: the exact symbol first, then the multiplier-stripped one (1000PEPEUSDT -> PEPEUSDT)."""
    base = perp_base(symbol)
    if base is None:
        return []
    out = [symbol]
    stripped = strip_multiplier(base, prefixes)
    if stripped != base:
        out.append(stripped + "USDT")
    return out


def plan_pairs(perp_symbols: Sequence[str], spot_symbols: Sequence[str], params: Mapping[str, Any]) -> List[Tuple[str, str]]:
    """[(perp, spot)] for every USDT perpetual with a spot pair, stablecoin bases excluded. Deterministic (sorted)."""
    spot = set(spot_symbols)
    stable = set(params["stable_bases"])
    out: List[Tuple[str, str]] = []
    for p in sorted(perp_symbols):
        base = perp_base(p)
        if base is None or strip_multiplier(base, params["multiplier_prefixes"]) in stable or base in stable:
            continue
        for s in spot_candidates(p, params["multiplier_prefixes"]):
            if s in spot:
                out.append((p, s))
                break
    return out


# ---------------------------------------------------------------- reading the store (offline, verified, never past the last explore month)
class Store:
    """The private archive store, read through ``binance_archive.read_rows`` (every month is re-verified against its stored checksum). ``last_month`` is a hard ceiling: later months are not opened."""

    def __init__(self, root: str, last_month: str) -> None:
        self.root = os.path.abspath(root)
        self.last_month = last_month
        self.read_log: Dict[str, str] = {}

    def _dir(self, dataset: str, symbol: str, interval: Optional[str]) -> str:
        return os.path.dirname(os.path.join(self.root, *BA.data_path(dataset, symbol, interval, "2020-01").split("/")))

    def months(self, dataset: str, symbol: str, interval: Optional[str]) -> List[str]:
        d = self._dir(dataset, symbol, interval)
        try:
            names = os.listdir(d)
        except OSError:
            return []
        out = []
        for n in names:
            if n.endswith(".zip"):
                m = n[:-4][-7:]
                if BA._MONTH.fullmatch(m) and m <= self.last_month:
                    out.append(m)
        return sorted(out)

    def rows(self, dataset: str, symbol: str, interval: Optional[str]) -> List[Tuple[Any, ...]]:
        out: List[Tuple[Any, ...]] = []
        for m in self.months(dataset, symbol, interval):
            path = os.path.join(self.root, *BA.data_path(dataset, symbol, interval, m).split("/"))
            out.extend(BA.read_rows(path, dataset))
            ok, sha = BA._stored_checksum_ok(path)
            self.read_log[os.path.relpath(path, self.root)] = sha
        return out

    def symbols(self, dataset: str, interval: Optional[str]) -> List[str]:
        market, _needs, dirname = BA._dataset(dataset)
        d = os.path.join(self.root, "data", *market.split("/"), "monthly", dirname)
        try:
            return sorted(s for s in os.listdir(d) if BA._SYMBOL.fullmatch(s))
        except OSError:
            return []

    def rejects(self) -> Dict[str, int]:
        """The downloader's counted rejects (REJECTS.jsonl) by code: months it refused and never stored."""
        out: Dict[str, int] = {}
        try:
            with open(os.path.join(self.root, "REJECTS.jsonl"), encoding="utf-8") as fh:
                for line in fh:
                    try:
                        code = str(json.loads(line).get("code"))
                    except ValueError:
                        continue
                    out[code] = out.get(code, 0) + 1
        except OSError:
            pass
        return out

    def digest(self) -> str:
        """sha256 of every (relative path, zip sha256) this run read: the identity of the data behind a result."""
        h = hashlib.sha256()
        for k in sorted(self.read_log):
            h.update(f"{k} {self.read_log[k]}\n".encode())
        return h.hexdigest()


# ---------------------------------------------------------------- one instrument
@dataclass
class Instrument:
    symbol: str
    spot_symbol: str
    base_key: str
    perp_first_ms: int
    spot_first_ms: int
    series: Any                                    # candidate_readiness.SymbolSeries (the row builder's input)
    day_open: List[int] = field(default_factory=list)
    day_qv: List[float] = field(default_factory=list)
    mark_ts: List[int] = field(default_factory=list)
    mark_px: List[float] = field(default_factory=list)
    index_ts: List[int] = field(default_factory=list)
    index_px: List[float] = field(default_factory=list)
    holes: List[Tuple[int, int]] = field(default_factory=list)
    delivered: bool = False
    quality: Dict[str, int] = field(default_factory=dict)
    spot_last_ms: Optional[int] = None             # the end of the spot pair's last listed month when it ends inside the window: the hedge does not exist after it


def snap_funding(rows: Sequence[Tuple[int, int, float]], tol_ms: int, off_grid: Optional[List[int]] = None) -> Tuple[List[Tuple[int, int, float]], Dict[str, int]]:
    """Funding prints as (snapped time, interval hours, rate in bps). A time is snapped to the nearest multiple of ITS stated interval; a print more than ``tol_ms`` off the grid, or landing on a grid time
    that already holds a print, is dropped and counted (never repaired)."""
    out: List[Tuple[int, int, float]] = []
    q = {"funding_off_grid_dropped": 0, "funding_duplicate_after_snap_dropped": 0}
    last = -1
    for t, iv_h, rate in rows:
        step = iv_h * MS_HOUR
        snapped = int(round(t / step)) * step
        if abs(t - snapped) > tol_ms:
            q["funding_off_grid_dropped"] += 1
            if off_grid is not None:
                off_grid.append(t)
            continue
        if snapped <= last:
            q["funding_duplicate_after_snap_dropped"] += 1
            continue
        out.append((snapped, iv_h, rate * 1e4))
        last = snapped
    return out, q


def funding_holes(prints: Sequence[Tuple[int, int, float]], hole_intervals: float) -> List[Tuple[int, int]]:
    """(earlier, later) pairs of consecutive prints farther apart than ``hole_intervals`` times the larger of their two intervals (an interval change is not a hole)."""
    out: List[Tuple[int, int]] = []
    for (a, ia, _), (b, ib, _) in zip(prints, prints[1:]):
        if b - a > hole_intervals * max(ia, ib) * MS_HOUR:
            out.append((a, b))
    return out


def interval_segments(prints: Sequence[Tuple[int, int, float]]) -> List[Tuple[int, int, int]]:
    """(first time, last time, interval minutes) of each run of prints with one stated interval; segments do not overlap."""
    segs: List[List[int]] = []
    for t, iv_h, _ in prints:
        if segs and segs[-1][2] == iv_h * 60:
            segs[-1][1] = t
        else:
            segs.append([t, t, iv_h * 60])
    return [(a, b, c) for a, b, c in segs]


def build_instrument(store: Store, perp: str, spot: str, params: Mapping[str, Any], cut_ms: int, spot_last_ms: Optional[int] = None) -> Optional[Instrument]:
    """None when the symbol cannot be built at all (no funding, no klines, no spot first month): the caller counts it."""
    fund_rows = store.rows("fundingRate", perp, None)
    kl = store.rows("klines", perp, "1d")
    spot_rows = store.rows("spot_klines", spot, "1d")
    if not fund_rows or not kl or not spot_rows:
        return None
    off_grid: List[int] = []
    prints, q = snap_funding(fund_rows, int(params["funding_snap_tolerance_ms"]), off_grid)
    if not prints:
        return None
    prints = [p for p in prints if p[0] < cut_ms]
    if not prints:
        return None
    quality = dict(q)
    holes = funding_holes(prints, float(params["hole_intervals"]))
    quality["funding_holes"] = len(holes)
    bad_months = sorted({ym_of(t) for t in off_grid})                      # a print off the grid of its own stated interval: stated interval and real spacing disagree in that month
    holes += [BA.month_bounds_ms(m) for m in bad_months]
    quality["interval_disagreement_months"] = len(bad_months)
    segs = interval_segments(prints)
    mark = store.rows("markPriceKlines", perp, "1d")
    index = store.rows("indexPriceKlines", perp, "1d")
    day_open = [r[0] for r in kl if r[0] + MS_DAY <= cut_ms]
    day_qv = [r[7] for r in kl if r[0] + MS_DAY <= cut_ms]
    last_ts = prints[-1][0]
    delivered = last_ts < cut_ms - int(params["delivered_if_funding_ends_more_than_days_before_cut"]) * MS_DAY
    perp_first = kl[0][0]
    spot_first = spot_rows[0][0]
    ser = CR.SymbolSeries(symbol=perp, interval_min=segs[-1][2], funding=[(t, r) for t, _iv, r in prints], mark=[(r[0] + MS_DAY, r[4]) for r in mark if r[0] + MS_DAY <= cut_ms],
                          launch_ms=max(perp_first, spot_first), delivery_ms=last_ts if delivered else None, spot_listed=True, funding_intervals=segs, mark_interval_ms=MS_DAY)
    inst = Instrument(symbol=perp, spot_symbol=spot, base_key=strip_multiplier(perp_base(perp) or perp, params["multiplier_prefixes"]), perp_first_ms=perp_first, spot_first_ms=spot_first, series=ser,
                      day_open=day_open, day_qv=day_qv, holes=holes, delivered=delivered, quality=quality, spot_last_ms=spot_last_ms)
    inst.mark_ts, inst.mark_px = [t for t, _ in ser.mark], [p for _, p in ser.mark]
    inst.index_ts = [r[0] + MS_DAY for r in index if r[0] + MS_DAY <= cut_ms]
    inst.index_px = [r[4] for r in index if r[0] + MS_DAY <= cut_ms]
    return inst


# ---------------------------------------------------------------- the point-in-time universe
def median_volume(inst: Instrument, t: int, days: int, min_present: int) -> Optional[float]:
    """Median daily quote volume over the complete days in [t - days, t); None when fewer than ``min_present`` of them exist."""
    lo = bisect.bisect_left(inst.day_open, t - days * MS_DAY)
    hi = bisect.bisect_right(inst.day_open, t - MS_DAY)                 # a day is complete when open + 1 day <= t
    vals = inst.day_qv[lo:hi]
    return statistics.median(vals) if len(vals) >= min_present else None


def has_hole(inst: Instrument, a: int, b: int) -> bool:
    return any(x < b and y > a for x, y in inst.holes)


def universe_at(instruments: Sequence[Instrument], t: int, params: Mapping[str, Any]) -> Tuple[List[Instrument], Dict[str, float], Counter]:
    """(the top ``top_n`` instruments by trailing median volume among those passing every other rule, their median volumes, the reason counts). Uses complete days before ``t`` only."""
    why: Counter = Counter()
    days, need = int(params["median_volume_days"]), int(params["min_median_days_present"])
    floor = float(params["min_median_quote_volume_usdt"])
    age = int(params["min_listing_age_days"]) * MS_DAY
    cand: List[Tuple[float, Instrument]] = []
    for s in instruments:
        sr = s.series
        if t - sr.launch_ms < age:
            why["listing_age"] += 1
            continue
        if ym_of(s.spot_first_ms) >= ym_of(t):
            why["spot_month_not_before_decision"] += 1
            continue
        if s.spot_last_ms is not None and t >= s.spot_last_ms:
            why["spot_delisted"] += 1                                    # the hedge does not exist after its last listed month
            continue
        if sr.delivery_ms is not None and t >= sr.delivery_ms:
            why["delivered"] += 1
            continue
        if has_hole(s, t - days * MS_DAY, t):                          # the trailing window only: nothing about the hold may choose the universe
            why["funding_hole"] += 1
            continue
        i = bisect.bisect_right(s.day_open, t - MS_DAY)
        alive = int(params["alive_days"])
        if i < alive or s.day_open[i - 1] != t - MS_DAY or s.day_open[i - alive] != t - alive * MS_DAY or any(s.day_qv[j] <= 0 for j in range(i - alive, i)):
            why["not_alive"] += 1                                       # the days right before t must exist (a stale file is not a live market) and have traded
            continue
        mv = median_volume(s, t, days, need)
        if mv is None or mv < floor:
            why["volume_below_floor_or_too_few_days"] += 1
            continue
        cand.append((mv, s))
    best: Dict[str, Tuple[float, Instrument]] = {}
    for mv, s in cand:                                                   # one symbol per base: the higher median volume wins; ties by symbol
        cur = best.get(s.base_key)
        if cur is None or (mv, s.symbol) > (cur[0], cur[1].symbol):
            best[s.base_key] = (mv, s)
    ranked = sorted(best.values(), key=lambda x: (-x[0], x[1].symbol))
    top = ranked[:int(params["top_n_by_median_volume"])]
    why["duplicate_base_dropped"] += len(cand) - len(best)
    why["below_top_n_dropped"] += len(ranked) - len(top)
    return [s for _, s in top], {s.symbol: mv for mv, s in top}, why


# ---------------------------------------------------------------- rows: the repository's builder at ZERO cost, one slot at a time
def slot_grid(first_ms: int, cut_ms: int, params: Mapping[str, Any]) -> List[int]:
    return CR.holding_grid(first_ms, cut_ms - MS_DAY, formation_days=max(params["formation_days"]), hold_days=int(params["hold_days"]))


def build_variant_rows(instruments: Sequence[Instrument], slots: Sequence[int], params: Mapping[str, Any]) -> Tuple[Dict[Tuple[int, int], Dict[int, Dict[str, Any]]], Dict[int, Dict[str, float]], Counter, Dict[str, Any]]:
    """{(formation, top_k): {t: row}}, {t: {symbol: median volume}} for the slots that had a universe, the summed exclusion reasons, and what the builder could NOT price: the legs delivered inside their
    hold with no usable terminal mark (an unknown loss), the legs it dropped, and the slots that produced no row. Nothing is dropped silently."""
    memo: Dict[Any, Any] = {}
    rows: Dict[Tuple[int, int], Dict[int, Dict[str, Any]]] = {(f, k): {} for f in params["formation_days"] for k in params["top_k"]}
    medvol: Dict[int, Dict[str, float]] = {}
    reasons: Counter = Counter()
    stats: Dict[str, Any] = {"slots_with_universe": 0, "terminal_exit_unpriced": [], "legs_dropped": Counter(), "slots_without_row": Counter()}
    for t in slots:
        uni, mv, why = universe_at(instruments, t, params)
        reasons.update(why)
        if not uni:
            continue
        medvol[t] = mv
        stats["slots_with_universe"] += 1
        series = [s.series for s in uni]
        for (f, k) in rows:
            name = f"F{f}K{k}"
            r = CR.run_family_variant(series, FAMILY, slots=[t], formation_days=f, hold_days=int(params["hold_days"]), top_k=k, round_trip_bps=0.0,
                                      min_listing_age_days=int(params["min_listing_age_days"]), memo=memo)
            stats["terminal_exit_unpriced"] += [{"variant": name, **u} for u in r["terminal_exit_unpriced_legs"]]
            if r["rows"]:
                rows[(f, k)][t] = r["rows"][0]
                stats["legs_dropped"][name] += k - len(r["rows"][0]["legs"])
            else:
                stats["slots_without_row"][name] += 1
    return rows, medvol, reasons, stats


# ---------------------------------------------------------------- bounds: cost, basis, capital, cash
class BoundedCash:
    """A cash basis for a bound: the series itself where it exists, and before it begins either zero or its mean over its first ``first_days`` days. A window that straddles the start is weighted by time."""

    def __init__(self, base: Any, before: str, first_days: int = 90) -> None:
        if before not in ("ZERO", "MEAN_OF_FIRST_90_DAYS"):
            raise StudyError("BAD_ARGUMENT", f"cash_before_series {before!r}")
        self.base, self.before, self.label = base, before, base.label
        self.first_ms = int(base.curve.first_ms())
        self.before_apr = 0.0 if before == "ZERO" else float(base.curve.mean_apr(self.first_ms, self.first_ms + first_days * MS_DAY))

    def apr_over(self, start_ms: int, end_ms: int) -> Optional[float]:
        if start_ms >= self.first_ms:
            return self.base.apr_over(start_ms, end_ms)
        if end_ms <= self.first_ms:
            return self.before_apr
        inside = self.base.apr_over(self.first_ms, end_ms)
        if inside is None:
            return None
        w = (self.first_ms - start_ms) / float(end_ms - start_ms)
        return w * self.before_apr + (1.0 - w) * float(inside)

    def describe(self) -> Dict[str, Any]:
        return {**self.base.describe(), "before_series": self.before, "before_apr": self.before_apr, "series_first_ms": self.first_ms}


def _last_at_or_before(ts: Sequence[int], px: Sequence[float], t: int, stale_ms: int) -> Optional[float]:
    i = bisect.bisect_right(ts, t) - 1
    if i < 0 or t - ts[i] > stale_ms:
        return None
    return px[i]


def leg_basis_pnl_bps(inst: Instrument, t: int, hold_ms: int, stale_ms: int) -> Optional[float]:
    """Short perp, long spot: minus the change of (mark - index) / index from entry to exit, in bps of notional; None when a price is stale or missing or the contract is delivered inside the hold."""
    end = t + hold_ms
    if inst.series.delivery_ms is not None and inst.series.delivery_ms <= end:
        return None
    m0 = _last_at_or_before(inst.mark_ts, inst.mark_px, t, stale_ms)
    i0 = _last_at_or_before(inst.index_ts, inst.index_px, t, stale_ms)
    m1 = _last_at_or_before(inst.mark_ts, inst.mark_px, end, stale_ms)
    i1 = _last_at_or_before(inst.index_ts, inst.index_px, end, stale_ms)
    if None in (m0, i0, m1, i1) or i0 <= 0 or i1 <= 0:
        return None
    return -(((m1 - i1) / i1) - ((m0 - i0) / i0)) * 1e4


def slippage_bps(median_vol: float, tiers: Sequence[Sequence[float]]) -> float:
    for floor, bps in sorted(tiers, key=lambda x: -x[0]):
        if median_vol >= floor:
            return float(bps)
    return float(sorted(tiers, key=lambda x: x[0])[0][1])


def measured_basis_floor(rows: Mapping[Tuple[int, int], Mapping[int, Mapping[str, Any]]], ts: Sequence[int], instruments: Mapping[str, Instrument], params: Mapping[str, Any]) -> Optional[float]:
    """The WORST (lowest) measured weekly basis P&L of any selected leg in any variant over the aligned weeks: what an UNMEASURED leg is charged under the ADVERSE bound. None when nothing was measured."""
    hold_ms, stale_ms = int(params["hold_days"]) * MS_DAY, int(params["stale_mark_days"]) * MS_DAY
    tset, seen, worst = set(ts), set(), None
    for rr in rows.values():
        for t, row in rr.items():
            if t not in tset:
                continue
            for l in row["legs"]:
                if (l["symbol"], t) in seen:
                    continue
                seen.add((l["symbol"], t))
                b = leg_basis_pnl_bps(instruments[l["symbol"]], t, hold_ms, stale_ms)
                if b is not None and (worst is None or b < worst):
                    worst = b
    return worst


def bound_series(variant_rows: Mapping[int, Mapping[str, Any]], ts: Sequence[int], bound: str, instruments: Mapping[str, Instrument], medvol: Mapping[int, Mapping[str, float]],
                 params: Mapping[str, Any], cash: Any, costs: Mapping[str, Any], unmeasured_basis_bps: float = 0.0) -> List[Dict[str, Any]]:
    """The aligned rows of one variant under one bound: net bps of gross notional (funding - cost + basis), capital, cash and the excess over cash in bps of CAPITAL. A leg whose basis cannot be measured is charged
    ``unmeasured_basis_bps`` (the ADVERSE bound passes a stress, the FAVOURABLE bound zero) and counted; a leg whose hold has a funding hole is counted (it earned the prints that exist)."""
    hold_ms = int(params["hold_days"]) * MS_DAY
    stale_ms = int(params["stale_mark_days"]) * MS_DAY
    spec = params[bound.lower()]
    capital = CB.capital_per_gross_notional(True, int(spec["leverage"]))
    total_rt, fee_rt = float(costs["bps"]), float(costs["fee_bps"])
    tset = set(ts)
    out: List[Dict[str, Any]] = []
    for t in ts:
        row = variant_rows[t]
        legs = row["legs"]
        prev = variant_rows.get(t - hold_ms) if (t - hold_ms) in tset else None
        nxt = variant_rows.get(t + hold_ms) if (t + hold_ms) in tset else None
        prev_syms = {l["symbol"] for l in (prev["legs"] if prev else [])}
        next_syms = {l["symbol"] for l in (nxt["legs"] if nxt else [])}
        net_legs: List[float] = []
        basis_legs: List[float] = []
        cost_legs: List[float] = []
        unmeasured = hold_holes = entered = exited = 0
        for l in legs:
            inst = instruments[l["symbol"]]
            b = leg_basis_pnl_bps(inst, t, hold_ms, stale_ms)
            if b is None:
                unmeasured += 1
                b = float(unmeasured_basis_bps)
            end = t + hold_ms if inst.series.delivery_ms is None else min(t + hold_ms, inst.series.delivery_ms)
            if has_hole(inst, t, end):
                hold_holes += 1
            e_in = 0 if l["symbol"] in prev_syms else 1
            e_out = 0 if l["symbol"] in next_syms else 1
            entered += e_in
            exited += e_out
            if bound == "ADVERSE":
                cost = total_rt + slippage_bps(medvol[t][l["symbol"]], spec["extra_slippage_bps_by_median_volume"])
            else:
                cost = 0.5 * fee_rt * (e_in + e_out)
            net_legs.append(l["funding_bps"] - cost + b)
            basis_legs.append(b)
            cost_legs.append(cost)
        net = statistics.fmean(net_legs)
        cash_bps = CB.cash_bps_over(cash, t, hold_ms / MS_DAY)
        if cash_bps is None:
            raise StudyError("CASH_UNPRICED", f"no cash figure for the hold starting {t}")
        out.append({"t": t, "net_bps_of_gross": net, "capital": capital, "cash_bps_of_capital": cash_bps, "excess_bps_of_capital": net / capital - cash_bps, "legs": [l["symbol"] for l in legs],
                    "leg_net_bps": net_legs, "unmeasured_basis_legs": unmeasured, "hold_hole_legs": hold_holes, "gross_funding_bps": row["funding_bps"],
                    "terminal_legs": sum(1 for l in legs if l.get("closed_during_hold")), "cost_bps_mean": statistics.fmean(cost_legs), "basis_bps_mean": statistics.fmean(basis_legs),
                    "entered_legs": entered, "exited_legs": exited, "leg_funding_bps": [l["funding_bps"] for l in legs], "leg_formation_bps_per_day": [l.get("formation_bps_per_day") for l in legs]})
    return out


# ---------------------------------------------------------------- the walk-forward and the decision
def quarter_key(ms: int) -> Tuple[int, int]:
    d = dt.datetime.fromtimestamp(ms / 1000.0, tz=dt.timezone.utc)
    return d.year, (d.month - 1) // 3


def calendar_folds(ts: Sequence[int], wf: Mapping[str, Any]) -> List[Tuple[List[int], List[int], Tuple[int, int]]]:
    """Expanding walk-forward whose out-of-sample blocks are CALENDAR quarters from ``oos_start_utc``: a block holds the rows whose decision time is in the quarter, and trains on every row at least
    ``label_horizon + embargo`` rows before it. One missing slot changes one block by one row, never the window by months. Every fold is audited for leakage (``statistical_power.audit_split``)."""
    oos_start = day_ms(wf["oos_start_utc"])
    h, e, tmin = int(wf["label_horizon_rows"]), int(wf["embargo_rows"]), int(wf["train_min_rows"])
    n = len(ts)
    groups: Dict[Tuple[int, int], List[int]] = {}
    for i, t in enumerate(ts):
        if t >= oos_start:
            groups.setdefault(quarter_key(t), []).append(i)
    folds: List[Tuple[List[int], List[int], Tuple[int, int]]] = []
    for key in sorted(groups):
        test = groups[key]
        train = list(range(0, test[0] - (h + e)))
        if len(train) < tmin:
            continue
        a = SP.audit_split(train, test, n, h, e)
        if not a["clean"]:
            raise StudyError("FOLD_LEAKS", str(a["reading"]))
        folds.append((train, test, key))
    return folds


def walk_forward(series_by: Mapping[str, Sequence[float]], ts: Sequence[int], wf: Mapping[str, Any]) -> Dict[str, Any]:
    """For each calendar-quarter block: the variant with the best mean on the TRAIN rows only, applied to the block. Returns the picks and the (variant, row index) pairs of the pooled out-of-sample series."""
    folds = calendar_folds(ts, wf)
    if not folds:
        raise StudyError("NO_FOLDS", "no calendar block has enough training rows")
    oos_idx: List[Tuple[str, int]] = []
    picks: List[str] = []
    for train, test, _key in folds:
        pick = RH.train_only_best({k: [v[i] for i in train] for k, v in series_by.items()})
        picks.append(pick)
        oos_idx.extend((pick, i) for i in test)
    return {"folds": len(folds), "picks": picks, "oos": oos_idx, "quarters": [f"{k[0]}Q{k[1] + 1}" for _a, _b, k in folds]}


def decide(adv: Mapping[str, Any], fav: Mapping[str, Any], params: Mapping[str, Any]) -> Dict[str, Any]:
    """The pre-registered decision. NO_WINNER: the FAVOURABLE bound's widest 90% interval ends at or below zero. SURVIVOR_PENDING_FINAL_READ: the ADVERSE bound passes (a) to (d); (e), the one read of the
    reserved months, is a separate act. Anything else is INCONCLUSIVE. A survivor and a NO_WINNER at once is a contradiction and is reported as INCONCLUSIVE with the reason."""
    d = params["decision"]
    fci = fav["stats"].get("ci")
    no_winner = bool(isinstance(fci, Mapping) and fci.get("status") == "OK" and isinstance(fci.get("hi"), (int, float)) and fci["hi"] <= 0)
    aci = adv["stats"].get("ci")
    a = bool(adv["stats"].get("mean") is not None and adv["stats"]["mean"] > 0 and adv["p_adjusted"] <= d["alpha"] and isinstance(aci, Mapping) and aci.get("status") == "OK"
             and isinstance(aci.get("lo"), (int, float)) and aci["lo"] > 0)
    years = d["survivor_years"]
    pos = [y for y in years if adv["by_year"].get(str(y)) is not None and adv["by_year"][str(y)] > 0]
    b = len(pos) >= int(d["survivor_min_positive_years"])
    c = adv["mean_without_best_weeks"] is not None and adv["mean_without_best_weeks"] > 0
    e_share = adv["max_symbol_share"]
    dd = e_share is not None and e_share <= float(d["max_symbol_share"])
    checks = {"a_adverse_mean_positive_holm_and_ci_lo_positive": a, "b_positive_in_enough_years": b, "c_positive_without_the_best_weeks": c, "d_no_symbol_above_the_share_cap": dd}
    survivor = all(checks.values())
    if no_winner and survivor:
        verdict, note = "INCONCLUSIVE", "the favourable bound is wholly non-positive and the adverse bound passes: a contradiction, so nothing is claimed"
    elif no_winner:
        verdict, note = "NO_WINNER", ("for an always-invested top-k carry in these liquid names over the out-of-sample window, even with turnover-aware fee-only costs and leverage 5, the excess over cash is not "
                                      "positive; this does not close the mechanism (entry thresholds, 2020-21, other names and Bybit are untested)")
    elif survivor:
        verdict, note = "SURVIVOR_PENDING_FINAL_READ", "a HYPOTHESIS for Bybit's own data only; criterion (e), the one final read of the reserved months, is not done"
    else:
        verdict, note = "INCONCLUSIVE", "the design is neither excluded under the favourable assumptions nor demonstrated under the adverse ones"
    return {"verdict": verdict, "note": note, "no_winner_test_favourable_ci_hi_le_0": no_winner, "survivor_checks": checks, "positive_years": pos}


def record_series(ledger: Any, series_by_variant: Mapping[str, List[Dict[str, Any]]], bound: str) -> None:
    """Count every series once (an identical re-run of the same series is the same trial)."""
    for v, rows in series_by_variant.items():
        rets = [r["excess_bps_of_capital"] for r in rows]
        if RH.series_digest(rets) not in ledger.returns_digests(v):
            ledger.record(v, {"variant": v, "bound": bound}, rets, horizon=1)


def mde_bps(sd: Optional[float], ess: Optional[float], n_counted: int, alpha: float, power: float) -> Optional[float]:
    """The smallest true mean excess (bps of capital per week) the pooled out-of-sample test would detect with probability ``power`` at the Holm threshold of the whole counted family (normal approximation,
    effective sample size). A mean inside this margin is not evidence of absence."""
    if not sd or not ess or ess <= 0:
        return None
    nd = statistics.NormalDist()
    return (nd.inv_cdf(1.0 - alpha / max(1, n_counted)) + nd.inv_cdf(power)) * float(sd) / math.sqrt(float(ess))


def descriptive_block(rows: Sequence[Mapping[str, Any]]) -> Optional[Dict[str, Any]]:
    """Plain facts about a set of out-of-sample weeks. Descriptive only: no verdict is read from any of it."""
    if not rows:
        return None
    n = len(rows)
    legs = sum(len(r["legs"]) for r in rows)
    mean = lambda key: statistics.fmean(r[key] for r in rows)
    cap = rows[0]["capital"]
    cost, basis, cash = mean("cost_bps_mean"), mean("basis_bps_mean"), mean("cash_bps_of_capital")
    return {"weeks": n, "mean_gross_funding_bps": mean("gross_funding_bps"), "mean_cost_bps": cost, "mean_basis_bps": basis, "capital_per_gross_notional": cap, "mean_cash_bps_of_capital": cash,
            "mean_net_bps_of_gross": mean("net_bps_of_gross"), "mean_excess_bps_of_capital": mean("excess_bps_of_capital"),
            "share_of_weeks_with_positive_excess": sum(1 for r in rows if r["excess_bps_of_capital"] > 0) / n,
            "turnover_share_of_legs_traded_per_week": (sum(r["entered_legs"] + r["exited_legs"] for r in rows) / (2.0 * legs)) if legs else None,
            "break_even_gross_funding_bps_per_week": cost - basis + cash * cap,
            "net_return_on_capital_bps_per_week_with_free_capital": statistics.fmean(r["net_bps_of_gross"] / r["capital"] for r in rows),
            "legs_with_a_funding_hole_in_the_hold": sum(r["hold_hole_legs"] for r in rows), "legs_with_unmeasured_basis": sum(r["unmeasured_basis_legs"] for r in rows)}


def persistence(rows: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    """Trailing funding (per week) against the funding the same leg then earned, across every selected leg: does the selection signal persist?"""
    xs, ys = [], []
    for r in rows:
        for f, y in zip(r["leg_formation_bps_per_day"], r["leg_funding_bps"]):
            if isinstance(f, (int, float)) and math.isfinite(f):
                xs.append(7.0 * float(f))
                ys.append(float(y))
    out: Dict[str, Any] = {"pairs": len(xs), "correlation": None, "mean_forward_over_mean_trailing": None}
    if len(xs) >= 3 and statistics.pstdev(xs) > 0 and statistics.pstdev(ys) > 0:
        out["correlation"] = statistics.correlation(xs, ys)
    if xs and statistics.fmean(xs) != 0:
        out["mean_forward_over_mean_trailing"] = statistics.fmean(ys) / statistics.fmean(xs)
    return out


def bound_report(name: str, series_by_variant: Mapping[str, List[Dict[str, Any]]], ts: Sequence[int], ledger: Any, params: Mapping[str, Any], cash_first_ms: int, covered_only: bool) -> Dict[str, Any]:
    ex = {v: [r["excess_bps_of_capital"] for r in rows] for v, rows in series_by_variant.items()}
    wf = walk_forward(ex, ts, params["walk_forward"])
    oos_all = [series_by_variant[v][i] for v, i in wf["oos"]]
    oos_rows = [r for r in oos_all if r["t"] >= cash_first_ms] if covered_only else oos_all          # the survivor test uses only rows the series itself covers
    oos = [r["excess_bps_of_capital"] for r in oos_rows]
    st = RH.significance(oos, ci=True)
    m = ledger.n_variants
    p = st["p_one_sided"]
    d = params["decision"]
    adj = SP.adjust_pvalues([p], "holm", alpha=float(d["alpha"]), n_tested=m)["adjusted"][0]
    by_year: Dict[str, Optional[float]] = {}
    for y in sorted({year_of(r["t"]) for r in oos_rows}):
        by_year[str(y)] = statistics.fmean(r["excess_bps_of_capital"] for r in oos_rows if year_of(r["t"]) == y)
    k = int(d["drop_best_weeks"])
    rest = sorted(oos)[:-k] if len(oos) > k else []                          # the k LARGEST weeks are dropped
    share: Dict[str, float] = {}
    total = 0.0
    for r in oos_rows:
        for sym, v in zip(r["legs"], r["leg_net_bps"]):
            share[sym] = share.get(sym, 0.0) + v / max(1, len(r["legs"]))
            total += v / max(1, len(r["legs"]))
    max_share = (max(share.values()) / total) if share and total > 0 else None
    anchor = ledger.anchor()
    desc = {"overall": descriptive_block(oos_rows), "by_year": {y: descriptive_block([r for r in oos_rows if year_of(r["t"]) == int(y)]) for y in by_year}, "persistence": persistence(oos_rows)}
    return {"bound": name, "variants": sorted(ex), "oos_rows_all": len(oos_all), "oos_rows": len(oos), "cash_covered_only": covered_only, "folds": wf["folds"], "quarters": wf["quarters"], "picks": wf["picks"],
            "stats": {k2: v2 for k2, v2 in st.items()}, "n_variants_counted": m, "ledger_anchor": {"n": anchor.n, "head": anchor.head}, "p_adjusted": adj,
            "minimum_detectable_mean_excess_bps_80pct_power": mde_bps(st.get("sd"), st.get("ess"), m, float(d["alpha"]), float(d.get("mde_power", 0.8))),
            "by_year": by_year, "mean_without_best_weeks": statistics.fmean(rest) if rest else None, "dropped_best_weeks": k, "max_symbol_share": max_share,
            "symbol_share_top": sorted(((sym, v / total) for sym, v in share.items()), key=lambda x: -x[1])[:5] if total > 0 else [],
            "mean_net_bps_of_gross": statistics.fmean(r["net_bps_of_gross"] for r in oos_rows) if oos_rows else None,
            "mean_cash_bps_of_capital": statistics.fmean(r["cash_bps_of_capital"] for r in oos_rows) if oos_rows else None,
            "mean_unmeasured_basis_legs": statistics.fmean(r["unmeasured_basis_legs"] for r in oos_rows) if oos_rows else None, "descriptives": desc}


# ---------------------------------------------------------------- the whole explore run
def code_digests() -> Dict[str, str]:
    """sha256 of the two source files that made a result: the figures are reproducible only with the same code, the same protocol and the same data."""
    out: Dict[str, str] = {}
    for mod in (sys.modules[__name__], BA):
        with open(mod.__file__, "rb") as fh:
            out[os.path.basename(mod.__file__)] = hashlib.sha256(fh.read()).hexdigest()
    return out


def not_buildable_reason(store: Store, perp: str, spot: str) -> str:
    if not store.months("fundingRate", perp, None):
        return "no_funding_months"
    if not store.months("klines", perp, "1d"):
        return "no_kline_months"
    if not store.months("spot_klines", spot, "1d"):
        return "no_spot_month"
    return "no_usable_prints"


def run_explore(store: Store, pairs: Sequence[Tuple[str, str]], protocol: Mapping[str, Any], digest: str, ledger_path: str, cash_basis: Any, spot_listing: Mapping[str, Mapping[str, str]], *,
                allow_unpinned: bool = False) -> Dict[str, Any]:
    params = protocol["parameters"]
    pinned = bool(EXPECTED_PROTOCOL_DIGEST) and digest == EXPECTED_PROTOCOL_DIGEST
    if not pinned and not allow_unpinned:
        raise StudyError("PROTOCOL_NOT_PINNED", "the protocol file is not the registered one (its digest differs from the pinned digest): the result would not be pre-registered; pass allow_unpinned to run it as NOT PRE-REGISTERED")
    cut_ms = day_ms(params["cut_utc"])
    if store.last_month != params["last_explore_month"]:
        raise StudyError("STORE_CEILING", f"the store must be opened with last_month {params['last_explore_month']!r}, got {store.last_month!r}")
    quality: Counter = Counter()
    instruments: List[Instrument] = []
    for perp, spot in pairs:
        last = (spot_listing.get(spot) or {}).get("last")
        spot_last_ms = BA.month_bounds_ms(last)[1] if last and last <= params["last_explore_month"] else None
        quality["pairs_with_a_spot_listing"] += 1 if last else 0
        try:
            inst = build_instrument(store, perp, spot, params, cut_ms, spot_last_ms)
        except BA.ArchiveError as exc:
            raise StudyError("STORE_ERROR", f"{perp}: {exc} (a stored month that no longer verifies stops the run; a symbol is never dropped silently)") from exc
        if inst is None:
            quality[f"instrument_not_buildable:{not_buildable_reason(store, perp, spot)}"] += 1
            continue
        instruments.append(inst)
        quality.update({k: v for k, v in inst.quality.items()})
    if not instruments:
        raise StudyError("NO_DATA", "no instrument could be built from the store")
    first_ms = max(day_ms(params["window_start_utc"]), min(i.series.funding[0][0] for i in instruments))
    slots = slot_grid(first_ms, cut_ms, params)
    expected = CR.holding_grid(day_ms(params["window_start_utc"]), cut_ms - MS_DAY, formation_days=max(params["formation_days"]), hold_days=int(params["hold_days"]))
    rows, medvol, reasons, stats = build_variant_rows(instruments, slots, params)
    common = set(slots)
    for v in rows.values():
        common &= set(v)
    ts = sorted(common)
    cash_first = int(cash_basis.curve.first_ms())
    quality["aligned_rows_before_the_cash_series_(cash_extended)"] = sum(1 for t in ts if t < cash_first)
    base = {"schema": SCHEMA, "label": LABEL, "protocol_id": protocol["id"], "protocol_digest": digest, "pre_registered": pinned, "data_digest": store.digest(), "code_sha256": code_digests(),
            "quality": dict(quality), "exclusions": dict(reasons), "rejects_recorded_by_the_downloader": store.rejects()}
    need = math.ceil(float(params["completeness"]["min_fraction_of_grid"]) * len(expected))
    gaps = {"aligned_rows": len(ts), "grid_slots_expected": len(expected), "grid_slots_required": need, "last_row_ms": ts[-1] if ts else None, "last_slot_expected_ms": expected[-1] if expected else None}
    if len(ts) < int(params["min_aligned_rows"]) or len(ts) < need or not ts or ts[-1] < expected[-1] - int(params["hold_days"]) * MS_DAY:
        return {**base, "verdict": "INSUFFICIENT_DATA", "reason": "COMPLETENESS: too few aligned weekly rows, or the last row is not near the end of the window", **gaps}
    by_sym = {i.symbol: i for i in instruments}
    costs = CR.family_costs(FAMILY)["taker"]
    cash = {"ADVERSE": BoundedCash(cash_basis, params["adverse"]["cash_before_series"]), "FAVOURABLE": BoundedCash(cash_basis, params["favourable"]["cash_before_series"])}
    floor = measured_basis_floor(rows, ts, by_sym, params)
    stress = {"ADVERSE": min(0.0, floor) if floor is not None else 0.0, "FAVOURABLE": 0.0}
    ledger = RH.VariantLedger(protocol["multiple_testing"]["family"], path=ledger_path)
    bounds: Dict[str, Dict[str, Any]] = {}
    sv_by_bound: Dict[str, Dict[str, List[Dict[str, Any]]]] = {}
    for bound in ("ADVERSE", "FAVOURABLE"):
        sv_by_bound[bound] = {f"{FAMILY}|F{f}|H{params['hold_days']}|K{k}|B{bound}": bound_series(rows[(f, k)], ts, bound, by_sym, medvol, params, cash[bound], costs, stress[bound]) for (f, k) in sorted(rows)}
        record_series(ledger, sv_by_bound[bound], bound)                 # all twelve are counted BEFORE either bound is judged: Holm is over the whole family
    for bound in ("ADVERSE", "FAVOURABLE"):
        bounds[bound] = bound_report(bound, sv_by_bound[bound], ts, ledger, params, int(cash_basis.curve.first_ms()), covered_only=(bound == "ADVERSE"))
        bounds[bound]["cash"] = cash[bound].describe()
        bounds[bound]["unmeasured_basis_charge_bps"] = stress[bound]
    decision = decide(bounds["ADVERSE"], bounds["FAVOURABLE"], params)
    unpriced = stats["terminal_exit_unpriced"]
    if unpriced:
        decision = {**decision, "verdict_before_the_cap": decision["verdict"], "verdict": "INSUFFICIENT_DATA", "note": f"TERMINAL_EXIT_UNPRICED: {len(unpriced)} leg(s) delivered inside their hold have no usable terminal mark: "
                                                                                                                      "an unknown loss, so neither a pass nor a rejection can be claimed"}
    return {**base, "window": {"cut_utc": params["cut_utc"], **gaps, "first_row_ms": ts[0]}, "instruments": len(instruments), "bounds": bounds, "decision": decision,
            "selection_facts": {"slots_with_a_universe": stats["slots_with_universe"], "legs_the_builder_dropped_by_variant": dict(stats["legs_dropped"]), "slots_without_a_row_by_variant": dict(stats["slots_without_row"]),
                                "terminal_exit_unpriced_count": len(unpriced), "terminal_exit_unpriced_first_20": unpriced[:20]},
            "forbidden": protocol["forbidden_claims"], "claim_limits": "PROXY_RESEARCH_ONLY: Binance data and Bybit-style costs; not Bybit, not owner dollars, not capacity; a survivor is a hypothesis"}


# ---------------------------------------------------------------- fetching: the only part that touches the network, through ``binance_archive`` (two hosts, GET, verified)
def months_in_window(first_month: str, last_month: str, present: Sequence[str]) -> List[str]:
    return [m for m in present if first_month <= m <= last_month]


def ever_ranked(store: Store, pairs: Sequence[Tuple[str, str]], params: Mapping[str, Any], n: int) -> List[str]:
    """The perps that ever rank in the top ``n`` by trailing median volume at a weekly decision, among those that pass only the cheap rules (listing age on the perp's own history and the volume floor).
    It is the SUPERSET the detail datasets are fetched for: the strict rules (spot month, funding holes, delivery) can only remove symbols, so with ``n`` well above ``top_n_by_median_volume`` a symbol the
    strict universe could rank in its top ``top_n`` is in it. Uses only the volume klines already in the store."""
    cut_ms = day_ms(params["cut_utc"])
    days, need, floor = int(params["median_volume_days"]), int(params["min_median_days_present"]), float(params["min_median_quote_volume_usdt"])
    age = int(params["min_listing_age_days"]) * MS_DAY
    mini: Dict[str, Instrument] = {}
    for perp, _spot in pairs:
        kl = store.rows("klines", perp, "1d")
        if kl:
            mini[perp] = Instrument(symbol=perp, spot_symbol="", base_key="", perp_first_ms=kl[0][0], spot_first_ms=0, series=None, day_open=[r[0] for r in kl if r[0] + MS_DAY <= cut_ms],
                                    day_qv=[r[7] for r in kl if r[0] + MS_DAY <= cut_ms])
    if not mini:
        return []
    t = CR.holding_grid(day_ms(params["window_start_utc"]), cut_ms - MS_DAY, formation_days=max(params["formation_days"]), hold_days=int(params["hold_days"]))
    keep: set = set()
    for slot in t:
        ranked = []
        for sym, inst in mini.items():
            if slot - inst.perp_first_ms < age:
                continue
            mv = median_volume(inst, slot, days, need)
            if mv is not None and mv >= floor:
                ranked.append((-mv, sym))
        keep.update(sym for _, sym in sorted(ranked)[:n])
    return sorted(keep)


def _parallel(fn: Any, items: Sequence[Any], workers: int) -> List[Any]:
    with ThreadPoolExecutor(max_workers=max(1, min(16, workers))) as pool:
        return list(pool.map(fn, items))


def fetch_candidates(arc: Any, params: Mapping[str, Any], workers: int = 4) -> Dict[str, Any]:
    """Stage 1: the volume klines (1d) of every USDT perpetual that has a spot pair, for the explore months only."""
    first, last = params["window_start_utc"][:7], params["last_explore_month"]
    pairs = plan_pairs(arc.list_symbols("klines"), arc.list_symbols("spot_klines"), params)

    def one(pair: Tuple[str, str]) -> Dict[str, int]:
        perp = pair[0]
        months = months_in_window(first, last, arc.list_months("klines", perp, "1d"))
        out: Dict[str, int] = Counter()
        for r in arc.download_range("klines", perp, "1d", months, until_month=last):
            out[r["status"]] += 1
        return dict(out)
    tally: Counter = Counter()
    for d in _parallel(one, pairs, workers):
        tally.update(d)
    return {"stage": "candidates", "pairs": len(pairs), "by_status": dict(tally), "pair_list": [list(p) for p in pairs]}


def fetch_details(arc: Any, store: Store, pairs: Sequence[Tuple[str, str]], params: Mapping[str, Any], workers: int = 4, n_ranked: int = 150) -> Dict[str, Any]:
    """Stage 2: funding, mark and index klines for the perps that ever rank, and the FIRST month of each one's spot pair (its listing evidence). Explore months only."""
    first, last = params["window_start_utc"][:7], params["last_explore_month"]
    chosen = set(ever_ranked(store, pairs, params, n_ranked))
    todo = [p for p in pairs if p[0] in chosen]
    listing: Dict[str, Dict[str, str]] = {}

    def one(pair: Tuple[str, str]) -> Dict[str, int]:
        perp, spot = pair
        out: Dict[str, int] = Counter()
        for dataset, interval in (("fundingRate", None), ("markPriceKlines", "1d"), ("indexPriceKlines", "1d")):
            months = months_in_window(first, last, arc.list_months(dataset, perp, interval))
            for r in arc.download_range(dataset, perp, interval, months, until_month=last):
                out[r["status"]] += 1
        all_spot = arc.list_months("spot_klines", spot, "1d")
        if all_spot:
            listing[spot] = {"first": all_spot[0], "last": all_spot[-1]}              # from the free listing: the hedge's first and last listed month (listing evidence, not a checksummed file)
        spot_months = [m for m in all_spot if m <= last]
        if spot_months:
            for r in arc.download_range("spot_klines", spot, "1d", spot_months[:1], until_month=last):
                out[r["status"]] += 1
        return dict(out)
    tally: Counter = Counter()
    for d in _parallel(one, todo, workers):
        tally.update(d)
    write_spot_listing(arc.root, listing)
    return {"stage": "details", "ranked_symbols": len(chosen), "pairs_fetched": len(todo), "by_status": dict(tally), "spot_listings": len(listing)}


SPOT_LISTING_NAME = "SPOT_LISTING.json"


def write_spot_listing(root: str, listing: Mapping[str, Mapping[str, str]]) -> None:
    """Merge the spot pairs' first and last listed month into the store (atomic, private). It comes from the archive's free S3 listing and is used for ONE rule: no hedge after its last listed month."""
    have = load_spot_listing(root)
    have.update({k: dict(v) for k, v in listing.items()})
    BA._write_atomic(os.path.join(root, SPOT_LISTING_NAME), (json.dumps({"schema": "spot_listing/v1", "source": "S3 listing of data/spot/monthly/klines/<SYMBOL>/1d/", "spots": have}, sort_keys=True) + "\n").encode())


def load_spot_listing(root: str) -> Dict[str, Dict[str, str]]:
    try:
        with open(os.path.join(root, SPOT_LISTING_NAME), encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, ValueError):
        return {}
    spots = doc.get("spots") if isinstance(doc, dict) else None
    return {k: dict(v) for k, v in spots.items() if isinstance(v, dict) and BA._SYMBOL.fullmatch(str(k))} if isinstance(spots, dict) else {}


# ---------------------------------------------------------------- command line
def _clean(o: Any) -> Any:
    """Non-finite floats become None (a report is JSON with no NaN)."""
    if isinstance(o, float):
        return o if math.isfinite(o) else None
    if isinstance(o, Mapping):
        return {str(k): _clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_clean(v) for v in o]
    return o


def _json_default(o: Any) -> Any:
    if isinstance(o, (set, frozenset)):
        return sorted(o)
    raise TypeError(type(o).__name__)


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="the explore run on a verified store (offline)")
    r.add_argument("--root", required=True)
    r.add_argument("--out", required=True)
    r.add_argument("--ledger-dir", required=True, help="a throwaway directory for the variant ledger (never under the owner's home)")
    r.add_argument("--repo", default=os.path.dirname(BOT), help="the repository root holding the committed cash series")
    r.add_argument("--allow-unpinned-protocol", action="store_true", help="run a protocol file whose digest is not the pinned one; the result is marked NOT PRE-REGISTERED")
    f = sub.add_parser("fetch", help="download the explore-window data in two stages (network, through binance_archive)")
    f.add_argument("--root", required=True, help="a private directory OUTSIDE git (0700)")
    f.add_argument("--stage", required=True, choices=("candidates", "details"))
    f.add_argument("--workers", type=int, default=6)
    f.add_argument("--rps", type=float, default=12.0)
    a = ap.parse_args(argv)
    if a.cmd == "fetch":
        try:
            proto, digest = load_protocol()
            params = proto["parameters"]
            arc = BA.Archive(a.root, rps=a.rps)
            if a.stage == "candidates":
                res = fetch_candidates(arc, params, a.workers)
                with open(os.path.join(arc.root, "PAIRS.json"), "w", encoding="utf-8") as fh:
                    json.dump({"protocol_digest": digest, "pairs": res.pop("pair_list")}, fh)
                os.chmod(os.path.join(arc.root, "PAIRS.json"), 0o600)
            else:
                store = Store(a.root, params["last_explore_month"])
                with open(os.path.join(arc.root, "PAIRS.json"), encoding="utf-8") as fh:
                    pairs = [tuple(p) for p in json.load(fh)["pairs"]]
                res = fetch_details(arc, store, pairs, params, a.workers)
            print(json.dumps({"protocol_digest": digest, **res}, sort_keys=True))
            return 0
        except (StudyError, BA.ArchiveError) as exc:
            print(f"refused: {exc}", file=sys.stderr)
            return 3 if getattr(exc, "code", "") in ("REFUSED_BY_ARCHIVE", "NETWORK_ERROR") else 2
    try:
        proto, digest = load_protocol()
        params = proto["parameters"]
        os.makedirs(a.ledger_dir, mode=0o700, exist_ok=True)
        cash = CB.historical_basis(os.path.join(a.repo, "bot") if os.path.isdir(os.path.join(a.repo, "bot")) else a.repo)
        if cash is None:
            raise StudyError("CASH_SERIES_MISSING", "the committed cash series could not be read")
        store = Store(a.root, params["last_explore_month"])
        perps = store.symbols("klines", "1d")
        spots = store.symbols("spot_klines", "1d")
        pairs = plan_pairs(perps, spots, params)
        spot_listing = load_spot_listing(a.root)
        if not spot_listing:
            raise StudyError("SPOT_LISTING_MISSING", f"{SPOT_LISTING_NAME} is missing or empty in the store: run the details stage first (a delisted spot pair must end the hedge)")
        rep = run_explore(store, pairs, proto, digest, os.path.join(a.ledger_dir, "proxy_carry.jsonl"), cash, spot_listing, allow_unpinned=a.allow_unpinned_protocol)
    except (StudyError, BA.ArchiveError, RH.HarnessError) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    with open(a.out, "w", encoding="utf-8") as fh:
        json.dump(_clean(rep), fh, indent=2, sort_keys=True, default=_json_default, allow_nan=False)
        fh.write("\n")
    print(json.dumps({"verdict": rep.get("decision", {}).get("verdict", rep.get("verdict")), "protocol_digest": digest, "rows": rep.get("window", {}).get("aligned_rows")}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
