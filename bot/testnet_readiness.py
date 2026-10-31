"""Fail-closed testnet readiness, and a funding-comparability check. Pure.

Nothing here touches the network, the clock, the environment or a file. Every
function takes plain values and returns a frozen verdict, so the same inputs
give the same answer on any host and a test can name the exact refusal.

TWO QUESTIONS
=============
1. `assess_testnet_readiness`: may the testnet execution path start? Anything
   not positively established is a REFUSAL. A missing input is never "fine".
2. `assess_funding_comparability`: may a funding series measured on one venue
   (the repo's research corpora are Binance USDT-M) be used as evidence about
   another (the runtime venue is Bybit)? The answer is `exact`, `proxy` (only
   when the caller explicitly accepts a cross-venue proxy) or `incomparable`,
   with named reasons. It never repairs data.

FUNDING FACTS ENCODED (positive rate: longs PAY, shorts RECEIVE)
================================================================
* Rates are FRACTIONS per interval (0.0001 == 1 bp). bps and percent differ by
  1e4 and 1e2 and do not raise; a magnitude screen catches the mix.
* Bybit's funding interval is a per-instrument field (`fundingInterval`, in
  minutes) that can differ from 8h; Binance corpora here are 8h. A series is
  comparable only at equal cadence and equal phase on the epoch grid.
* Bybit caps the rate (`upperFundingRate`/`lowerFundingRate`); a history that
  exceeds the runtime cap contains values the runtime venue cannot print.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Optional, Sequence, Tuple

# -- refusal names -----------------------------------------------------------
ACCIDENTAL_MAINNET = "ACCIDENTAL_MAINNET"
LIVE_MODE = "LIVE_MODE"
UNRECOGNIZED_VENUE = "UNRECOGNIZED_VENUE"
STALE_MARKET_DATA = "STALE_MARKET_DATA"
UNVERIFIED_PRECISION = "UNVERIFIED_PRECISION"
INCOMPLETE_FUNDING_HISTORY = "INCOMPLETE_FUNDING_HISTORY"
FUNDING_UNIT_SUSPECT = "FUNDING_UNIT_SUSPECT"
INVALID_POSITION_MODE = "INVALID_POSITION_MODE"
INVALID_MARGIN_MODE = "INVALID_MARGIN_MODE"
INCONSISTENT_CONTRACT_METADATA = "INCONSISTENT_CONTRACT_METADATA"
EXCESSIVE_CLOCK_DRIFT = "EXCESSIVE_CLOCK_DRIFT"

KNOWN_VENUES = ("mainnet", "testnet", "demo")
TESTNET_HOST = "api-testnet.bybit.com"

#: Largest |rate| per print that is plausible as a FRACTION. Bybit's own cap on
#: majors is 0.00333..0.01; a median above this means the series is in bps/%.
MAX_PLAUSIBLE_FRACTION = 0.02


@dataclass(frozen=True)
class Refusal:
    code: str
    detail: str


@dataclass(frozen=True)
class ReadinessVerdict:
    ready: bool
    refusals: Tuple[Refusal, ...]

    @property
    def codes(self) -> Tuple[str, ...]:
        return tuple(r.code for r in self.refusals)

    def has(self, code: str) -> bool:
        return code in self.codes


# -- helpers -----------------------------------------------------------------

def _dec(value: Any) -> Optional[Decimal]:
    if value is None or isinstance(value, bool) or value == "":
        return None
    try:
        out = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return out if out.is_finite() else None


def _num(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def _truthy_flag(value: Any) -> Optional[bool]:
    """True/False for an unambiguous flag, None for anything else."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        t = value.strip().lower()
        if t in ("1", "true", "yes", "on"):
            return True
        if t in ("0", "false", "no", "off"):
            return False
    if value in (0, 1) and not isinstance(value, bool):
        return bool(value)
    return None


def _multiple(value: Decimal, step: Decimal) -> bool:
    return step > 0 and (value / step) == (value / step).to_integral_value()


# -- instrument metadata -----------------------------------------------------

def check_instrument(raw: Mapping[str, Any], *, category: str) -> Tuple[Refusal, ...]:
    """Precision/limits/contract checks of one raw `instruments-info` row.

    `category` is "linear" or "spot". Missing or non-positive precision is
    UNVERIFIED_PRECISION; a row that contradicts itself (or is not a tradable
    USDT linear perpetual) is INCONSISTENT_CONTRACT_METADATA.
    """
    out = []
    sym = str(raw.get("symbol") or "?")
    price = raw.get("priceFilter") or {}
    lot = raw.get("lotSizeFilter") or {}
    if category == "spot":
        step_raw, notional_raw = lot.get("basePrecision"), lot.get("minOrderAmt")
    else:
        step_raw = lot.get("qtyStep")
        notional_raw = lot.get("minNotionalValue")
    fields = {"tickSize": price.get("tickSize"), "qtyStep": step_raw,
              "minOrderQty": lot.get("minOrderQty"), "minNotional": notional_raw}
    parsed = {k: _dec(v) for k, v in fields.items()}
    for key, val in parsed.items():
        if val is None or val <= 0:
            out.append(Refusal(UNVERIFIED_PRECISION,
                               f"{sym} {key}={fields[key]!r} is missing or not positive"))
    tick, step, min_qty = parsed["tickSize"], parsed["qtyStep"], parsed["minOrderQty"]
    if step and min_qty and step > 0 and min_qty > 0 and not _multiple(min_qty, step):
        out.append(Refusal(INCONSISTENT_CONTRACT_METADATA,
                           f"{sym} minOrderQty {min_qty} is not a multiple of qtyStep {step}"))
    max_qty = _dec(lot.get("maxOrderQty"))
    if max_qty is not None and min_qty and max_qty < min_qty:
        out.append(Refusal(INCONSISTENT_CONTRACT_METADATA,
                           f"{sym} maxOrderQty {max_qty} < minOrderQty {min_qty}"))
    lo, hi = _dec(price.get("minPrice")), _dec(price.get("maxPrice"))
    if lo is not None and hi is not None and lo > hi:
        out.append(Refusal(INCONSISTENT_CONTRACT_METADATA,
                           f"{sym} minPrice {lo} > maxPrice {hi}"))
    if tick and lo is not None and tick > 0 and lo > 0 and not _multiple(lo, tick):
        out.append(Refusal(INCONSISTENT_CONTRACT_METADATA,
                           f"{sym} minPrice {lo} is not a multiple of tickSize {tick}"))
    status = raw.get("status")
    if status is None or str(status) != "Trading":
        out.append(Refusal(INCONSISTENT_CONTRACT_METADATA,
                           f"{sym} status {status!r} is not 'Trading'"))
    if category == "linear":
        if raw.get("contractType") != "LinearPerpetual":
            out.append(Refusal(INCONSISTENT_CONTRACT_METADATA,
                               f"{sym} contractType {raw.get('contractType')!r} "
                               "is not LinearPerpetual"))
        if raw.get("settleCoin") and raw.get("quoteCoin") \
                and raw["settleCoin"] != raw["quoteCoin"]:
            out.append(Refusal(INCONSISTENT_CONTRACT_METADATA,
                               f"{sym} settleCoin {raw['settleCoin']!r} != quoteCoin "
                               f"{raw['quoteCoin']!r}"))
        interval = _dec(raw.get("fundingInterval"))
        if interval is None or interval <= 0 or interval != interval.to_integral_value():
            out.append(Refusal(INCONSISTENT_CONTRACT_METADATA,
                               f"{sym} fundingInterval {raw.get('fundingInterval')!r} "
                               "is not a positive integer number of minutes"))
        up, low = _dec(raw.get("upperFundingRate")), _dec(raw.get("lowerFundingRate"))
        if up is not None and low is not None and not low < up:
            out.append(Refusal(INCONSISTENT_CONTRACT_METADATA,
                               f"{sym} funding cap lower {low} >= upper {up}"))
    return tuple(out)


def check_symbol_pair(perp: Mapping[str, Any], spot: Mapping[str, Any]) -> Tuple[Refusal, ...]:
    """The perp and spot rows must describe the same asset in the same unit."""
    out = []
    for key in ("baseCoin", "quoteCoin"):
        a, b = perp.get(key), spot.get(key)
        if not a or not b or a != b:
            out.append(Refusal(INCONSISTENT_CONTRACT_METADATA,
                               f"perp {key} {a!r} != spot {key} {b!r}"))
    ps, ss = str(perp.get("symbol") or ""), str(spot.get("symbol") or "")
    if ps != ss:
        # 1000PEPEUSDT vs PEPEUSDT (a 1000x unit) or any renamed pairing.
        out.append(Refusal(INCONSISTENT_CONTRACT_METADATA,
                           f"perp symbol {ps!r} != spot symbol {ss!r}: not mapped "
                           "one-to-one; a multiplier contract is not a basis leg"))
    return tuple(out)


# -- funding history ---------------------------------------------------------

def check_funding_history(history: Optional[Sequence[Tuple[int, float]]], *,
                          interval_s: int, now_ms: int, min_prints: int,
                          jitter_ms: int = 5_000,
                          lateness_ms: int = 120_000) -> Tuple[Refusal, ...]:
    """History as ``(epoch_ms, FRACTION)`` oldest first: complete or refused.

    Complete means: at least `min_prints`; strictly increasing; consecutive
    prints exactly one interval apart (within `jitter_ms`); every stamp on the
    interval grid; the newest print not older than one interval plus
    `lateness_ms` (a missing latest event); none in the future; finite rates of
    a magnitude plausible for a fraction.
    """
    if not history:
        return (Refusal(INCOMPLETE_FUNDING_HISTORY, "no funding prints supplied"),)
    out = []
    step = int(interval_s) * 1000
    if step <= 0:
        return (Refusal(INCOMPLETE_FUNDING_HISTORY, f"interval_s={interval_s!r}"),)
    if len(history) < min_prints:
        out.append(Refusal(INCOMPLETE_FUNDING_HISTORY,
                           f"{len(history)} prints < required {min_prints}"))
    stamps = [int(s) for s, _ in history]
    rates = [_num(r) for _, r in history]
    if any(r is None for r in rates):
        out.append(Refusal(INCOMPLETE_FUNDING_HISTORY, "non-finite funding rate"))
    for prev, cur in zip(stamps, stamps[1:]):
        if cur <= prev:
            out.append(Refusal(INCOMPLETE_FUNDING_HISTORY,
                               f"duplicate or out-of-order stamp {prev} -> {cur}"))
            break
    else:
        gaps = [(p, c) for p, c in zip(stamps, stamps[1:]) if abs(c - p - step) > jitter_ms]
        if gaps:
            p, c = gaps[0]
            out.append(Refusal(INCOMPLETE_FUNDING_HISTORY,
                               f"{len(gaps)} gap(s); first {p}->{c} is "
                               f"{(c - p) / 1000:.0f}s, expected {interval_s}s "
                               "(missing funding event)"))
    off = [s for s in stamps if min(s % step, step - s % step) > jitter_ms]
    if off:
        out.append(Refusal(INCOMPLETE_FUNDING_HISTORY,
                           f"{len(off)} stamp(s) off the {interval_s}s grid, e.g. {off[0]}"))
    if stamps[-1] > now_ms + jitter_ms:
        out.append(Refusal(INCOMPLETE_FUNDING_HISTORY,
                           f"newest print {stamps[-1]} is in the future"))
    elif now_ms - stamps[-1] > step + lateness_ms:
        out.append(Refusal(INCOMPLETE_FUNDING_HISTORY,
                           f"newest print is {(now_ms - stamps[-1]) / 1000:.0f}s old "
                           "(latest settlement missing)"))
    finite = sorted(abs(r) for r in rates if r is not None)
    if finite and finite[len(finite) // 2] > MAX_PLAUSIBLE_FRACTION:
        out.append(Refusal(FUNDING_UNIT_SUSPECT,
                           f"median |rate| {finite[len(finite) // 2]} is implausible "
                           "for a fraction; looks like bps or percent"))
    return tuple(out)


# -- the assessment ----------------------------------------------------------

def assess_testnet_readiness(
    *,
    venue: Optional[str],
    use_testnet: Any,
    live_trading: Any,
    live_authorized: Any,
    endpoint_host: Optional[str],
    perp_instrument: Optional[Mapping[str, Any]],
    spot_instrument: Optional[Mapping[str, Any]],
    snapshot_ages_s: Optional[Mapping[str, Any]],
    venue_clock_offset_ms: Any,
    position_mode: Optional[str],
    margin_mode: Optional[str],
    allowed_margin_modes: Sequence[str] = ("REGULAR_MARGIN", "ISOLATED_MARGIN",
                                           "PORTFOLIO_MARGIN"),
    funding_history: Optional[Sequence[Tuple[int, float]]] = None,
    funding_required: bool = True,
    now_ms: int = 0,
    min_funding_prints: int = 3,
    max_age_s: float = 90.0,
    max_clock_drift_ms: float = 5_000.0,
    allow_demo: bool = False,
) -> ReadinessVerdict:
    """Every input is required and explicit; anything unproven is a refusal."""
    r: list = []

    # environment ---------------------------------------------------------
    v = str(venue).strip().lower() if isinstance(venue, str) else ""
    if v not in KNOWN_VENUES:
        r.append(Refusal(UNRECOGNIZED_VENUE, f"venue {venue!r} is not one of {KNOWN_VENUES}"))
    elif v == "mainnet":
        r.append(Refusal(ACCIDENTAL_MAINNET, "venue is mainnet"))
    elif v == "demo" and not allow_demo:
        r.append(Refusal(UNRECOGNIZED_VENUE, "demo venue not accepted for testnet readiness"))
    sandbox = _truthy_flag(use_testnet)
    if sandbox is None:
        r.append(Refusal(UNRECOGNIZED_VENUE, f"USE_TESTNET={use_testnet!r} is not a stated boolean"))
    elif sandbox is False:
        r.append(Refusal(ACCIDENTAL_MAINNET, "USE_TESTNET is false"))
    if v == "testnet" and endpoint_host != TESTNET_HOST:
        r.append(Refusal(ACCIDENTAL_MAINNET if endpoint_host else UNRECOGNIZED_VENUE,
                         f"endpoint host {endpoint_host!r} is not {TESTNET_HOST!r}"))
    live = _truthy_flag(live_trading)
    armed = _truthy_flag(live_authorized)
    if live is not False or armed is not False:
        r.append(Refusal(LIVE_MODE, f"live_trading={live_trading!r} "
                                    f"live_authorized={live_authorized!r}; both must be false"))

    # instruments ---------------------------------------------------------
    for name, row, cat in (("perp", perp_instrument, "linear"),
                           ("spot", spot_instrument, "spot")):
        if not row:
            r.append(Refusal(UNVERIFIED_PRECISION, f"{name} instrument metadata absent"))
        else:
            r.extend(check_instrument(row, category=cat))
    if perp_instrument and spot_instrument:
        r.extend(check_symbol_pair(perp_instrument, spot_instrument))

    # freshness -----------------------------------------------------------
    if not snapshot_ages_s:
        r.append(Refusal(STALE_MARKET_DATA, "no snapshot ages supplied"))
    else:
        for leg in ("perp_mark", "spot_mark", "funding"):
            if leg not in snapshot_ages_s:
                r.append(Refusal(STALE_MARKET_DATA, f"no age for {leg}"))
        for leg, age in snapshot_ages_s.items():
            a = _num(age)
            if a is None or a < -5.0 or a > max_age_s:
                r.append(Refusal(STALE_MARKET_DATA, f"{leg} age {age!r}s (limit {max_age_s}s)"))

    # clock ---------------------------------------------------------------
    off = _num(venue_clock_offset_ms)
    if off is None or abs(off) > max_clock_drift_ms:
        r.append(Refusal(EXCESSIVE_CLOCK_DRIFT,
                         f"venue clock offset {venue_clock_offset_ms!r} ms "
                         f"(limit {max_clock_drift_ms:.0f} ms; unmeasured counts as excessive)"))

    # modes ---------------------------------------------------------------
    if position_mode != "one_way":
        r.append(Refusal(INVALID_POSITION_MODE, f"position mode {position_mode!r}; need 'one_way'"))
    if margin_mode not in allowed_margin_modes:
        r.append(Refusal(INVALID_MARGIN_MODE, f"margin mode {margin_mode!r} not in {tuple(allowed_margin_modes)}"))

    # funding -------------------------------------------------------------
    if funding_required:
        interval_min = _dec((perp_instrument or {}).get("fundingInterval"))
        if interval_min is None or interval_min <= 0:
            r.append(Refusal(INCOMPLETE_FUNDING_HISTORY,
                             "funding interval unknown; history cannot be checked"))
        else:
            r.extend(check_funding_history(
                funding_history, interval_s=int(interval_min) * 60,
                now_ms=int(now_ms), min_prints=min_funding_prints))
    return ReadinessVerdict(ready=not r, refusals=tuple(r))


# -- historical vs runtime comparability ------------------------------------

@dataclass(frozen=True)
class FundingSeriesDescriptor:
    """What a funding series IS, independent of its values."""
    venue: str                       # e.g. "binance_usdtm", "bybit_linear"
    symbol: str
    unit: str                        # "fraction" | "bps" | "percent"
    interval_s: int
    times_ms: Tuple[int, ...] = ()
    rates: Tuple[float, ...] = ()
    #: runtime venues only: the venue's own cap on |rate| per print (fraction)
    rate_cap: Optional[float] = None


@dataclass(frozen=True)
class ComparabilityVerdict:
    level: str                       # "exact" | "proxy" | "incomparable"
    reasons: Tuple[str, ...]

    @property
    def comparable(self) -> bool:
        return self.level != "incomparable"


_UNIT_SCALE = {"fraction": 1.0, "bps": 1e-4, "percent": 1e-2}


def assess_funding_comparability(history: FundingSeriesDescriptor,
                                 runtime: FundingSeriesDescriptor, *,
                                 allow_cross_venue_proxy: bool = False,
                                 phase_tolerance_ms: int = 60_000) -> ComparabilityVerdict:
    """Is `history` economic/temporal evidence about `runtime`?

    Hard incomparabilities (any one => `incomparable`): unknown or different
    unit; different interval; different symbol; stamps off the runtime grid
    phase; history reaching beyond the runtime rate cap; magnitude implausible
    for the stated unit. A different venue alone yields at best `proxy`, and
    only when `allow_cross_venue_proxy` is set: premium/index formulas, caps and
    interval policy are venue-specific.
    """
    bad = []
    for label, d in (("history", history), ("runtime", runtime)):
        if d.unit not in _UNIT_SCALE:
            bad.append(f"{label} unit {d.unit!r} unknown")
        if not isinstance(d.interval_s, int) or d.interval_s <= 0:
            bad.append(f"{label} interval {d.interval_s!r} invalid")
    if not bad:
        if history.unit != runtime.unit:
            bad.append(f"unit mismatch {history.unit} vs {runtime.unit} "
                       f"(factor {_UNIT_SCALE[history.unit] / _UNIT_SCALE[runtime.unit]:g})")
        if history.interval_s != runtime.interval_s:
            bad.append(f"interval mismatch {history.interval_s}s vs {runtime.interval_s}s: "
                       "a per-print rate is not per-hour comparable")
        else:
            step = history.interval_s * 1000
            skew = [t for t in history.times_ms
                    if min(t % step, step - t % step) > phase_tolerance_ms]
            if skew:
                bad.append(f"{len(skew)} history stamp(s) off the {history.interval_s}s grid "
                           f"(first {skew[0]}); settlement phase differs")
            deltas = {b - a for a, b in zip(history.times_ms, history.times_ms[1:])}
            if deltas and any(abs(d - step) > phase_tolerance_ms for d in deltas):
                bad.append("history cadence is not constant at the stated interval "
                           "(interval change or missing prints)")
    if history.symbol != runtime.symbol:
        bad.append(f"symbol mismatch {history.symbol!r} vs {runtime.symbol!r}")
    if history.rates and history.unit in _UNIT_SCALE:
        finite = [abs(x) for x in history.rates if _num(x) is not None]
        if len(finite) != len(history.rates):
            bad.append("history has non-finite rates")
        elif finite:
            as_fraction = sorted(finite)[len(finite) // 2] * _UNIT_SCALE[history.unit]
            if as_fraction > MAX_PLAUSIBLE_FRACTION:
                bad.append(f"median |rate| {as_fraction:g} as a fraction is implausible; "
                           f"history unit label {history.unit!r} is probably wrong")
            if runtime.rate_cap is not None and \
                    max(finite) * _UNIT_SCALE[history.unit] > runtime.rate_cap * (1 + 1e-9):
                bad.append("history contains rates beyond the runtime venue's funding cap")
    if bad:
        return ComparabilityVerdict("incomparable", tuple(bad))
    if history.venue != runtime.venue:
        if allow_cross_venue_proxy:
            return ComparabilityVerdict(
                "proxy", (f"cross-venue: {history.venue} history used as a proxy "
                          f"for {runtime.venue}",))
        return ComparabilityVerdict(
            "incomparable", (f"different venues ({history.venue} vs {runtime.venue}) "
                             "and no explicit proxy acknowledgement",))
    return ComparabilityVerdict("exact", ())
