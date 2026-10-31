"""What an entry's execution will probably cost, from fills this book really had.

Pure: no I/O, no clock reads (the caller says what "now" is). The input is the
journal's fill entries; the observations are the execution records the ledger
persisted beside each fill (`Entry.fee_raw["execution"]`, written by
`execution_record.assess_fill`). Nothing is taken from an order that did not
fill, from a later observation than the decision, or from a record that did not
validate.

Accounting stays as it is. Slippage is already inside each fill's price leg;
this module does not add anything to the ledger. It only reads the records to
PREDICT the cost of a future entry, and the prediction is a bound, not a quote.

Definitions (per leg = one product and side; bps of that leg's notional)
  half-spread  decision-time (ask - bid) / 2, from a book observed before the
               order (reference_basis == "book"); a mark-referenced record has
               no spread and is not used.
  slippage     fill vs the decision-time executable price (spread excluded).
  cost bound   max(0, mean + z * sd * sqrt(1 + 1/n)): a one-sided prediction
               bound for the NEXT fill. Price improvement is never credited.

Selection and look-ahead
  * only observations with fill time <= as_of_ms are used (a decision never
    sees its own or later fills);
  * the journal holds FILLED orders only, so unfilled attempts are invisible:
    the output says so (`survivorship`) and the bound is not a guarantee;
  * one order is one observation (partial fills and the cancel-time increment
    of the same order are merged by order link); an identical row repeated is
    dropped; a fill larger than its order or with contradictory timestamps is
    rejected;
  * only observations of similar size (policy.size_band) and the same
    symbol / product / side / order type are pooled.

Where the evidence is short of the policy, the answer is UNAVAILABLE with the
leg and the shortfall named. Nothing is substituted. The thresholds are a
frozen policy that may be made stricter, never looser.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

UNAVAILABLE = "UNAVAILABLE"
MEASURED = "MEASURED"
DAY_MS = 86_400_000

#: Entry kinds that are fills (ledger.FILL_KINDS; repeated so this stays pure).
FILL_KINDS = ("spot_buy", "spot_sell", "perp_open", "perp_close")


def _num(x: Any) -> Optional[float]:
    if isinstance(x, bool) or x is None:
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


@dataclass(frozen=True)
class CostSamplePolicy:
    """Frozen thresholds; `validate` refuses anything looser than the default.

    These are engineering defaults chosen to be conservative, and they are the
    owner's to tighten. They are not statistical claims about this venue."""
    min_samples: int = 20                 # orders per leg (= EvidencePolicy.min_trades)
    max_sample_age_days: float = 30.0     # oldest observation used
    max_newest_age_days: float = 7.0      # the newest must be this recent
    size_band: float = 4.0                # use fills within [1/4x, 4x] of the planned notional
    z: float = 1.645                      # one-sided 95% prediction bound

    def validate(self) -> None:
        b = CostSamplePolicy()
        ok = (_num(self.min_samples) is not None and self.min_samples >= b.min_samples
              and _num(self.max_sample_age_days) is not None
              and 0 < self.max_sample_age_days <= b.max_sample_age_days
              and _num(self.max_newest_age_days) is not None
              and 0 < self.max_newest_age_days <= b.max_newest_age_days
              and _num(self.size_band) is not None
              and 1.0 <= self.size_band <= b.size_band
              and _num(self.z) is not None and self.z >= b.z)
        if not ok:
            raise ValueError("CostSamplePolicy may be stricter than the default, "
                             f"never looser: {self!r} vs {b!r}")


DEFAULT_POLICY = CostSamplePolicy()


@dataclass(frozen=True)
class Observation:
    link: str
    ms: int
    notional_usd: float
    slippage_bps: float
    half_spread_bps: float


def _base_link(link: str) -> str:
    """The order a fill belongs to: the cancel-time increment is booked under
    `<link>+cancel`, which is the same order."""
    return str(link).split("+", 1)[0]


def extract(entries: Iterable[Any], *, as_of_ms: int, symbol: str, product: str,
            side: str, order_type: str = "market"
            ) -> Tuple[List[Observation], Dict[str, int]]:
    """Valid observations for one leg, and why the others were dropped.

    `entries` need `.kind`, `.ms`, `.ref` and `.fee_raw` (ledger `Entry`)."""
    rejected: Dict[str, int] = {}

    def drop(why: str) -> None:
        rejected[why] = rejected.get(why, 0) + 1

    seen: set = set()
    merged: Dict[str, Dict[str, float]] = {}
    for e in entries:
        if getattr(e, "kind", None) not in FILL_KINDS:
            continue
        rec = (getattr(e, "fee_raw", None) or {}).get("execution")
        if not isinstance(rec, Mapping):
            drop("NO_EXECUTION_RECORD")
            continue
        if (rec.get("symbol") != symbol or rec.get("product") != product
                or rec.get("side") != side or rec.get("order_type") != order_type):
            continue                        # another leg: not a rejection
        if rec.get("status") != MEASURED or rec.get("reference_basis") != "book":
            drop("NOT_BOOK_MEASURED")
            continue
        ms = _num(getattr(e, "ms", None))
        if ms is None or ms > as_of_ms:
            drop("AFTER_DECISION" if ms is not None else "NO_TIME")
            continue
        q, px = _num(rec.get("filled_qty")), _num(rec.get("avg_price"))
        ref = _num(rec.get("reference_price"))
        slip, spr = _num(rec.get("slippage_usd")), _num(rec.get("spread_cost_usd"))
        if None in (q, px, ref, slip, spr) or q <= 0 or px <= 0 or ref <= 0:
            drop("MALFORMED")
            continue
        req = _num(rec.get("requested_qty"))
        if req is not None and q > req * (1 + 1e-6) + 1e-9:
            drop("FILL_EXCEEDS_ORDER")
            continue
        d2s, s2f = rec.get("decision_to_submit_ms"), rec.get("submit_to_fill_ms")
        if (d2s is not None and _num(d2s) is not None and _num(d2s) < 0) or \
                (s2f is not None and _num(s2f) is not None and _num(s2f) < 0):
            drop("CONTRADICTORY_TIMESTAMPS")
            continue
        link = _base_link(getattr(e, "ref", "") or rec.get("order_link_id") or "")
        if not link:
            drop("NO_ORDER_LINK")
            continue
        key = (getattr(e, "ref", ""), int(ms), q)
        if key in seen:
            drop("DUPLICATE_ROW")
            continue
        seen.add(key)
        m = merged.setdefault(link, {"ms": 0.0, "q": 0.0, "notional": 0.0,
                                     "slip": 0.0, "spr": 0.0})
        m["ms"] = max(m["ms"], ms)
        m["q"] += q
        m["notional"] += ref * q
        m["slip"] += slip
        m["spr"] += spr
    obs = [Observation(link=k, ms=int(v["ms"]), notional_usd=v["notional"],
                       slippage_bps=v["slip"] / v["notional"] * 1e4,
                       half_spread_bps=v["spr"] / v["notional"] * 1e4)
           for k, v in sorted(merged.items()) if v["notional"] > 0]
    return obs, rejected


def _bound(values: Sequence[float], z: float) -> Tuple[float, float, float]:
    """(mean, sd, one-sided prediction bound floored at 0) of `values`."""
    n = len(values)
    mean = sum(values) / n
    sd = (math.sqrt(sum((v - mean) ** 2 for v in values) / (n - 1))
          if n > 1 else 0.0)
    return mean, sd, max(0.0, mean + z * sd * math.sqrt(1.0 + 1.0 / n))


def estimate_leg(entries: Iterable[Any], *, as_of_ms: int, symbol: str,
                 product: str, side: str, planned_notional_usd: float,
                 order_type: str = "market",
                 policy: CostSamplePolicy = DEFAULT_POLICY) -> Dict[str, Any]:
    """One leg's expected execution cost, or UNAVAILABLE with the reason."""
    policy.validate()
    leg = f"{product}/{side}/{order_type}"
    out: Dict[str, Any] = {
        "leg": leg, "symbol": symbol, "status": UNAVAILABLE, "reasons": [],
        "units": "bps of the leg's notional (positive = cost)",
        "n": 0, "n_in_size_band": 0, "period_ms": None, "newest_age_days": None,
        "half_spread_bps": None, "slippage_bps": None, "cost_bound_bps": None,
        "survivorship": "only filled orders are observed; orders that did not "
                        "fill are invisible, so this is not a guarantee",
        "as_of_ms": int(as_of_ms), "rejected": {}}
    planned = _num(planned_notional_usd)
    if planned is None or planned <= 0:
        out["reasons"].append("PLANNED_NOTIONAL_INVALID")
        return out
    obs, rejected = extract(entries, as_of_ms=as_of_ms, symbol=symbol,
                            product=product, side=side, order_type=order_type)
    out["rejected"] = rejected
    out["n"] = len(obs)
    cutoff = as_of_ms - policy.max_sample_age_days * DAY_MS
    fresh = [o for o in obs if o.ms >= cutoff]
    if len(fresh) < len(obs):
        out["rejected"]["TOO_OLD"] = len(obs) - len(fresh)
    band = [o for o in fresh
            if planned / policy.size_band <= o.notional_usd <= planned * policy.size_band]
    if len(band) < len(fresh):
        out["rejected"]["OUTSIDE_SIZE_BAND"] = len(fresh) - len(band)
    out["n_in_size_band"] = len(band)
    if band:
        out["period_ms"] = [min(o.ms for o in band), max(o.ms for o in band)]
        out["newest_age_days"] = (as_of_ms - max(o.ms for o in band)) / DAY_MS
    if len(band) < policy.min_samples:
        out["reasons"].append(
            f"INSUFFICIENT_SAMPLES: {len(band)} of {policy.min_samples} "
            f"orders on {leg} within the size band and age limit")
        return out
    if out["newest_age_days"] > policy.max_newest_age_days:
        out["reasons"].append(
            f"STALE_SAMPLES: newest {out['newest_age_days']:.1f}d old "
            f"(limit {policy.max_newest_age_days}d)")
        return out
    s_mean, s_sd, s_bound = _bound([o.slippage_bps for o in band], policy.z)
    h_mean, h_sd, h_bound = _bound([o.half_spread_bps for o in band], policy.z)
    out.update(status=MEASURED,
               slippage_bps={"mean": s_mean, "sd": s_sd, "bound": s_bound},
               half_spread_bps={"mean": h_mean, "sd": h_sd, "bound": h_bound},
               cost_bound_bps=s_bound + h_bound)
    return out


def required_legs(execution_mode: str, perp_symbol: str, spot_symbol: str
                  ) -> List[Tuple[str, str, str]]:
    """(symbol, product, side) of every leg a round trip executes. An OVERLAY
    pays the two perp legs; an ACQUIRE pair adds the spot round trip."""
    legs = [(perp_symbol, "linear", "Sell"), (perp_symbol, "linear", "Buy")]
    if str(execution_mode).lower() != "overlay":
        legs += [(spot_symbol, "spot", "Buy"), (spot_symbol, "spot", "Sell")]
    return legs


def estimate_round_trip(entries: Iterable[Any], *, as_of_ms: int,
                        execution_mode: str, perp_symbol: str, spot_symbol: str,
                        planned_notional_usd: float,
                        policy: CostSamplePolicy = DEFAULT_POLICY
                        ) -> Dict[str, Any]:
    """The round trip's spread and slippage (bps of the pair notional), summed
    over its legs, or UNAVAILABLE naming every leg that lacks evidence.

    A leg's bps are bps of that leg's notional; both legs of a hedge carry the
    same notional, so the sum is bps of the position notional."""
    entries = list(entries)
    legs = [estimate_leg(entries, as_of_ms=as_of_ms, symbol=sym, product=prod,
                         side=side, planned_notional_usd=planned_notional_usd,
                         policy=policy)
            for sym, prod, side in required_legs(execution_mode, perp_symbol,
                                                 spot_symbol)]
    missing = [l for l in legs if l["status"] != MEASURED]
    out: Dict[str, Any] = {
        "status": UNAVAILABLE if missing else MEASURED,
        "legs": legs, "spread_bps": None, "slippage_bps": None,
        "reasons": [f"{l['leg']}: {r}" for l in missing for r in l["reasons"]],
        "policy": {"min_samples": policy.min_samples,
                   "max_sample_age_days": policy.max_sample_age_days,
                   "max_newest_age_days": policy.max_newest_age_days,
                   "size_band": policy.size_band, "z": policy.z}}
    if not missing:
        out["spread_bps"] = sum(l["half_spread_bps"]["bound"] for l in legs)
        out["slippage_bps"] = sum(l["slippage_bps"]["bound"] for l in legs)
    return out
