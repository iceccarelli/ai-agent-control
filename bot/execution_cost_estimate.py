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
               order (reference_basis == "executable_side"); a mark-referenced record has
               no spread and is not used.
  slippage     fill vs the decision-time executable price (spread excluded).
  cost bound   max(0, mean + z * sd * sqrt(1 + 1/n)): a one-sided prediction
               bound for the NEXT fill. Price improvement is never credited.

Selection and look-ahead
  * only observations with fill time <= as_of_ms are used (a decision never
    sees its own or later fills);
  * a record's own "MEASURED" is not believed: its stored book, timestamps and
    fill are re-validated by `execution_record.verify_record` and its slippage,
    spread and reference must reproduce (RECORD_UNVERIFIABLE /
    RECORD_INCONSISTENT otherwise);
  * the bound is taken over per-order slippage floored at zero and is never
    below the raw bound: price improvement is not credited;
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
import execution_record as _xr
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

UNAVAILABLE = "UNAVAILABLE"
MEASURED = "MEASURED"
#: `execution_record` marks a record measured against the decision-time book's
#: executable side with this basis ("mark" when no book was seen). The value is
#: the producer's; a test builds records with the real producer so the two
#: cannot drift apart.
BOOK_BASIS = "executable_side"
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


#: How far a journal line's time may be from the fill time inside its record.
#: A line is stamped when the fill is booked (seconds after the fill; a resting
#: order that fills before its cancel is booked can be minutes), so an hour is
#: generous for honest data and far below the days an edit would need.
LINE_TIME_TOLERANCE_MS = 3_600_000


def _base_link(link: str) -> str:
    """The order a fill belongs to: the cancel-time increment is booked under
    `<link>+cancel`, which is the same order."""
    return str(link).split("+", 1)[0]


def extract(entries: Iterable[Any], *, as_of_ms: int, symbol: str, product: str,
            side: str, order_types: Sequence[str] = ("market",),
            routes: Optional[Sequence[str]] = None,
            detail: Optional[Dict[str, Dict[str, int]]] = None
            ) -> Tuple[List[Observation], Dict[str, int]]:
    """Valid observations for one leg, and why the others were dropped.

    `entries` need `.kind`, `.ms`, `.ref` and `.fee_raw` (ledger `Entry`).

    `routes`, when given, keeps only the orders sent for those reasons (see
    `execution_record.ROUTES`); an order of another route is a different
    population, not a rejection. A record that states no route takes the one
    its side implies (`execution_record.default_route`): only an entry side can
    be inferred, so an unstated buy of the perp is UNCLASSIFIED.

    `detail`, when given, receives WHY a record was not book-measured, keyed
    under the same rejection name, so the aggregate never hides the cause."""
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
                or rec.get("side") != side
                or rec.get("order_type") not in order_types):
            continue                        # another leg: not a rejection
        if routes is not None:
            route = str(rec.get("route") or _xr.default_route(product, side))
            if route not in routes:
                continue                    # another population: not a rejection
        if rec.get("status") != MEASURED or rec.get("reference_basis") != BOOK_BASIS:
            drop("NOT_BOOK_MEASURED")
            if detail is not None:
                reasons = rec.get("reasons")
                why = (str(reasons[0]).split(":", 1)[0]
                       if isinstance(reasons, list) and reasons
                       else f"REFERENCE_BASIS_{rec.get('reference_basis') or 'NONE'}")
                bucket = detail.setdefault("NOT_BOOK_MEASURED", {})
                bucket[why] = bucket.get(why, 0) + 1
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
        unverified = _xr.verify_record(rec, entry_ms=int(ms))
        if unverified:
            # The record's own claim of "MEASURED" is not evidence: its stored
            # book, times and fill must reproduce its status and figures.
            drop(unverified.split(":", 1)[0])
            continue
        # A line's time decides the sample's AGE, so it must agree with the
        # record it carries: a record whose own fill time is far from the
        # line's would otherwise be passed off as fresh by editing one field.
        #
        # EVERY time the record states is compared, not just one: deleting the
        # field a single check relies on must not switch the check off. A record
        # that states no time at all cannot be dated and is not a sample.
        anchors = [_num(rec.get(k)) for k in
                   ("fill_ms", "submit_ms", "decision_ms", "observed_ms")]
        anchors = [a for a in anchors if a is not None]
        if not anchors:
            drop("LINE_TIME_UNVERIFIABLE")
            continue
        if any(abs(ms - a) > LINE_TIME_TOLERANCE_MS for a in anchors):
            drop("LINE_TIME_MISMATCH")
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
        key = (getattr(e, "ref", ""), int(ms), q, px, rec.get("order_link_id"),
               rec.get("submit_ms"), rec.get("fill_ms"), rec.get("decision_ms"))
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


def required_routes(product: str, side: str) -> Tuple[str, ...]:
    """The routes whose fills price a leg. An entry side is priced by planned
    entries and an exit side by PLANNED exits only: protective closes, recovery
    closes, rebalances, cancel-time increments and unclassified exits are other
    populations, reported beside the bound and never pooled into it."""
    if (product, side) in (("linear", "Sell"), ("spot", "Buy")):
        return (_xr.ROUTE_NORMAL_ENTRY,)
    return (_xr.ROUTE_NORMAL_EXIT,)


def _by_route(entries: List[Any], *, as_of_ms: int, symbol: str, product: str,
              side: str, order_types: Sequence[str],
              counted: Sequence[str]) -> Dict[str, Any]:
    """Every route seen on this leg, with what its valid fills cost. Nothing is
    discarded: an expensive emergency close is as visible as an easy entry."""
    seen = set()
    for e in entries:
        rec = (getattr(e, "fee_raw", None) or {}).get("execution")
        if (getattr(e, "kind", None) in FILL_KINDS and isinstance(rec, Mapping)
                and rec.get("symbol") == symbol
                and rec.get("product") == product and rec.get("side") == side
                and rec.get("order_type") in order_types):
            seen.add(str(rec.get("route") or _xr.default_route(product, side)))
    out: Dict[str, Any] = {}
    for route in sorted(seen):
        obs, rej = extract(entries, as_of_ms=as_of_ms, symbol=symbol,
                           product=product, side=side, order_types=order_types,
                           routes=(route,))
        slips = [o.slippage_bps for o in obs]
        halves = [o.half_spread_bps for o in obs]
        out[route] = {
            "n": len(obs), "rejected": rej,
            "counted_in_bound": route in counted,
            "slippage_bps_mean": (sum(slips) / len(slips)) if slips else None,
            "slippage_bps_max": max(slips) if slips else None,
            "half_spread_bps_mean": (sum(halves) / len(halves)) if halves else None}
    return out


def estimate_leg(entries: Iterable[Any], *, as_of_ms: int, symbol: str,
                 product: str, side: str, planned_notional_usd: float,
                 order_types: Sequence[str] = ("market",),
                 policy: CostSamplePolicy = DEFAULT_POLICY,
                 routes: Optional[Sequence[str]] = None) -> Dict[str, Any]:
    """One leg's expected execution cost, or UNAVAILABLE with the reason.

    `order_types` names the kinds of order whose fills describe this leg. A
    taker-only book prices from "market"; a maker-first book also sends resting
    "post_only" orders and a market remainder, so both are pooled. A resting
    fill's recorded half-spread is a cost it did not really pay, so pooling can
    only make the bound more conservative, never less."""
    policy.validate()
    entries = list(entries)
    routes = tuple(routes) if routes else required_routes(product, side)
    leg = f"{product}/{side}/{'+'.join(order_types)}"
    out: Dict[str, Any] = {
        "leg": leg, "symbol": symbol, "status": UNAVAILABLE, "reasons": [],
        "units": "bps of the leg's notional (positive = cost)",
        "n": 0, "n_in_size_band": 0, "period_ms": None, "newest_age_days": None,
        "half_spread_bps": None, "slippage_bps": None, "cost_bound_bps": None,
        "survivorship": "only filled orders are observed; orders that did not "
                        "fill are invisible, so this is not a guarantee",
        "as_of_ms": int(as_of_ms), "rejected": {}, "rejected_detail": {},
        "route_policy": list(routes), "by_route": {}}
    planned = _num(planned_notional_usd)
    if planned is None or planned <= 0:
        out["reasons"].append("PLANNED_NOTIONAL_INVALID")
        return out
    detail: Dict[str, Dict[str, int]] = {}
    obs, rejected = extract(entries, as_of_ms=as_of_ms, symbol=symbol,
                            product=product, side=side, order_types=order_types,
                            routes=routes, detail=detail)
    out["rejected"] = rejected
    out["rejected_detail"] = detail
    out["by_route"] = _by_route(entries, as_of_ms=as_of_ms, symbol=symbol,
                                product=product, side=side,
                                order_types=order_types, counted=routes)
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
    s_mean, s_sd, _raw_bound = _bound([o.slippage_bps for o in band], policy.z)
    # Price improvement is never credited: the bound is taken over each order's
    # slippage floored at zero, so a maker-first fill that came in better than
    # the touch cannot pull the bound down. The mean/sd shown stay the raw ones.
    improved = sum(1 for o in band if o.slippage_bps < 0)
    s_bound = max(_raw_bound, _bound([max(0.0, o.slippage_bps)
                                       for o in band], policy.z)[2])
    out["price_improved_orders"] = improved
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
                        order_types: Sequence[str] = ("market",),
                        policy: CostSamplePolicy = DEFAULT_POLICY
                        ) -> Dict[str, Any]:
    """The round trip's spread and slippage (bps of the pair notional), summed
    over its legs, or UNAVAILABLE naming every leg that lacks evidence.

    A leg's bps are bps of that leg's notional; both legs of a hedge carry the
    same notional, so the sum is bps of the position notional."""
    entries = list(entries)
    legs = [estimate_leg(entries, as_of_ms=as_of_ms, symbol=sym, product=prod,
                         side=side, planned_notional_usd=planned_notional_usd,
                         order_types=order_types, policy=policy)
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
    # Emergency closes are NOT in the bound above (it prices planned exits) and
    # cannot be forced to happen, so their cost is reported on its own.
    emergency = [l["by_route"][_xr.ROUTE_PROTECTIVE_CLOSE] for l in legs
                 if _xr.ROUTE_PROTECTIVE_CLOSE in l["by_route"]]
    n_emergency = sum(r["n"] for r in emergency)
    out["emergency_exit"] = {
        "status": "OBSERVED" if n_emergency else "UNMEASURED",
        "n": n_emergency,
        "slippage_bps_max": (max((r["slippage_bps_max"] for r in emergency
                                  if r["slippage_bps_max"] is not None),
                                 default=None)),
        "note": "protective closes are excluded from cost_bound_bps, which "
                "prices planned exits; the cost of leaving in a fast market is "
                "not covered by it"}
    if not missing:
        out["spread_bps"] = sum(l["half_spread_bps"]["bound"] for l in legs)
        out["slippage_bps"] = sum(l["slippage_bps"]["bound"] for l in legs)
    return out
