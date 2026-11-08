"""Portfolio risk gate of the multi-symbol layer: ``risk_assess(plan, state, policy, snapshots) -> RiskDecision``. Pure: no clock, no I/O, no venue.

Rules that make it trustworthy:
  * UNKNOWN stays UNKNOWN. A missing / None / non-finite required input is never read as zero: it is a reason code and the plan is rejected.
  * The policy is a CLOSED schema: an unknown key, a wildcard symbol, a live/unqualified venue or a malformed limit rejects everything.
  * Every reason is collected (no short-circuit after the policy is valid), so an operator sees all of them.
  * Exposure that is already on the account counts whatever symbol it is in (splitting exposure over symbols outside the allow-list does not hide it).
  * Exits (every leg reduce-only / a spot sell of held inventory) are never blocked by freshness or exposure ceilings: refusing to reduce risk is the unsafe choice. They
    are still bound to the allow-list and to a position that exists and covers the quantity.

Exposure vocabulary (USD, signed: long > 0, short < 0), per BASE symbol (``SPOT:X`` and ``X`` are the same base ``X``):
  per_symbol  = |spot| + |perp|                        (gross at that symbol; limit ``max_notional_per_symbol_usd``)
  aggregate   = sum over bases of max(|spot|, |perp|)  (a hedged pair counts once; limit ``max_aggregate_notional_usd``)
  gross       = sum of |every position|                (limit ``max_gross_exposure_usd``)
  net         = |sum of signed positions|              (limit ``max_net_exposure_usd``)
  unhedged    = the worst |net| the book can sit at if only ONE leg of the plan fills (paired-order failure), limit ``max_unhedged_usd``
  leverage    = gross / equity                         (limit ``max_leverage``)
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Tuple

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import venues as _venues  # noqa: E402
from ms_types import MarketSnapshot, OrderGroupPlan  # noqa: E402

POLICY_KEYS = ("policy_version", "candidate_id", "spec_digest", "venue", "allowed_symbols", "allowed_sides", "allowed_order_types", "max_notional_per_symbol_usd",
               "max_aggregate_notional_usd", "max_gross_exposure_usd", "max_net_exposure_usd", "max_active_groups", "max_unhedged_usd", "max_leverage",
               "max_market_age_ms", "max_account_age_ms", "execution_styles")
_USD_LIMITS = ("max_notional_per_symbol_usd", "max_aggregate_notional_usd", "max_gross_exposure_usd", "max_net_exposure_usd", "max_unhedged_usd")
_CATEGORIES = ("linear", "spot")
_SIDES = ("Buy", "Sell")
_ORDER_TYPES = ("Limit", "Market")
_STYLES = ("MAKER_FIRST", "TAKER")
SYMBOL_RE = re.compile(r"[A-Z0-9]{3,20}")
_PLAIN_DECIMAL = re.compile(r"[0-9]+(\.[0-9]+)?")
SPOT_PREFIX = "SPOT:"
WILDCARD_WORDS = frozenset({"ALL", "ANY", "STAR", "WILDCARD", "EVERY", "NONE", "NULL"})
#: An exit is bound by QUANTITY (never by notional at the limit price: a Buy limit at 1.0 makes 50 units look like 50 USD) and, for a Limit exit, by a price band around the mark.
EXIT_QTY_SLACK = 1.02                       # an exit may be at most 2% larger than the held quantity (rounding / a fill landing between the read and the order)
EXIT_MAX_PRICE_DEVIATION = 0.10             # a Limit exit must sit within +-10% of the mark of ITS category; a Market exit is allowed only reduce-only (perp) or a held-inventory spot sell


@dataclass(frozen=True)
class RiskDecision:
    approved: bool
    reasons: Tuple[str, ...]
    limits: Dict[str, Any] = field(default_factory=dict)


def _num(x: Any) -> Optional[float]:
    if isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x):
        return None
    return float(x)


def _pos(x: Any) -> Optional[float]:
    v = _num(x)
    return v if v is not None and v > 0 else None


def _is_pos_int(x: Any) -> bool:
    return type(x) is int and x > 0


def _text(x: Any) -> bool:
    return type(x) is str and bool(x.strip()) and x == x.strip()


def policy_refusals(policy: Any) -> List[str]:
    """Why this policy is unusable (empty = well formed). Closed schema; no wildcard; sandbox venue only."""
    if not isinstance(policy, Mapping):
        return ["POLICY_NOT_A_MAPPING"]
    out: List[str] = []
    extra, missing = sorted(str(k) for k in set(policy) - set(POLICY_KEYS)), sorted(set(POLICY_KEYS) - set(policy))
    if extra:
        out.append("POLICY_UNKNOWN_KEYS:" + ",".join(extra)[:80])
    if missing:
        out.append("POLICY_MISSING_KEYS:" + ",".join(missing)[:120])
    if extra or missing:
        return out
    for k in ("policy_version", "candidate_id", "spec_digest", "venue"):
        if not _text(policy[k]):
            out.append(f"POLICY_FIELD_INVALID:{k}")
    if _text(policy["venue"]):
        try:
            canonical = _venues.get(policy["venue"]).name
        except (_venues.UnknownVenue, _venues.VenueNotQualified):
            out.append("POLICY_VENUE_UNKNOWN_OR_UNQUALIFIED")
        else:
            if canonical != policy["venue"]:
                out.append("POLICY_VENUE_NOT_CANONICAL")                # 'Testnet' / 'TESTNET' name the registry's row but are not its name: no case-folding ambiguity
            if _venues.requires_live_authorisation(policy["venue"]):
                out.append("POLICY_VENUE_NOT_SANDBOX")
    syms = policy["allowed_symbols"]
    if not isinstance(syms, (list, tuple)) or not syms or any(type(s) is not str or not SYMBOL_RE.fullmatch(s) or s in WILDCARD_WORDS for s in syms):
        out.append("POLICY_ALLOWED_SYMBOLS_INVALID")          # an explicit list of plain symbols; '*', 'ALL', '', patterns are not symbols
    elif len(set(syms)) != len(syms):
        out.append("POLICY_ALLOWED_SYMBOLS_DUPLICATE")
    sides = policy["allowed_sides"]
    if not isinstance(sides, Mapping) or not sides or any(c not in _CATEGORIES or not isinstance(v, (list, tuple)) or not v or any(s not in _SIDES for s in v)
                                                          for c, v in sides.items()):
        out.append("POLICY_ALLOWED_SIDES_INVALID")
    for k, universe in (("allowed_order_types", _ORDER_TYPES), ("execution_styles", _STYLES)):
        v = policy[k]
        if not isinstance(v, (list, tuple)) or not v or any(type(x) is not str or x not in universe for x in v):
            out.append(f"POLICY_{k.upper()}_INVALID")
    for k in _USD_LIMITS:
        if _pos(policy[k]) is None:
            out.append(f"POLICY_LIMIT_INVALID:{k}")
    if _num(policy["max_leverage"]) is None or policy["max_leverage"] < 1:
        out.append("POLICY_LIMIT_INVALID:max_leverage")
    for k in ("max_active_groups", "max_market_age_ms", "max_account_age_ms"):
        if not _is_pos_int(policy[k]):
            out.append(f"POLICY_LIMIT_INVALID:{k}")
    return out


def _base(key: str) -> str:
    """Base symbol of a position key: an optional ``SPOT:`` prefix and an optional ``#<tag>`` suffix (so one symbol can carry several entries) are not part of it."""
    key = key.split("#", 1)[0]
    return key[len(SPOT_PREFIX):] if key.startswith(SPOT_PREFIX) else key


def _ref_price(leg: Any, snap: Optional[MarketSnapshot]) -> Optional[float]:
    """Worst-case reference price of a leg (USD per base unit) or None (UNKNOWN). Spot legs are priced at the SPOT mark, never the perp's."""
    if snap is None:
        return None
    mark = _pos(snap.spot_mark if leg.category == "spot" else snap.mark)
    if mark is None:
        return None
    if leg.order_type == "Market":
        return mark
    if not (type(leg.price) is str and _PLAIN_DECIMAL.fullmatch(leg.price)) or float(leg.price) <= 0:
        return None
    px = float(leg.price)
    return px if leg.side == "Buy" else max(px, mark)


def risk_assess(plan: OrderGroupPlan, state: Mapping[str, Any], policy: Mapping[str, Any], snapshots: Mapping[str, MarketSnapshot]) -> RiskDecision:
    bad_policy = policy_refusals(policy)
    if bad_policy:
        return RiskDecision(False, tuple(bad_policy), {})
    R: List[str] = []
    limits: Dict[str, Any] = {}
    if not isinstance(state, Mapping):
        return RiskDecision(False, ("STATE_NOT_A_MAPPING",), {})
    snapshots = snapshots if isinstance(snapshots, Mapping) else {}

    # ---- plan identity: the plan must be the one the policy was written for
    for a, b in (("policy_version", "policy_version"), ("candidate_id", "candidate_id"), ("spec_digest", "spec_digest")):
        if getattr(plan, a, None) != policy[b]:
            R.append(f"PLAN_{a.upper()}_MISMATCH")
    if plan.style not in policy["execution_styles"]:
        R.append("EXECUTION_STYLE_NOT_ALLOWED")
    if not plan.legs:
        R.append("PLAN_HAS_NO_LEGS")
    if not _is_pos_int(plan.created_ms):
        R.append("PLAN_TIME_UNKNOWN")
    seen_ids = set()
    for leg in plan.legs:
        if leg.leg_id in seen_ids:
            R.append(f"LEG_ID_DUPLICATE:{leg.leg_id}")
        seen_ids.add(leg.leg_id)

    # ---- state: UNKNOWN is a rejection, never zero
    equity = _pos(state.get("equity_usd"))
    if equity is None:
        R.append("EQUITY_UNKNOWN")
    raw_pos = state.get("positions")
    pos: Dict[Tuple[str, str], float] = {}          # (base symbol, category) -> signed USD
    pos_qty: Dict[Tuple[str, str], float] = {}      # (base symbol, category) -> signed base units, ONLY while every entry for the key stated its own ``qty``
    pos_qty_lacking: set = set()                    # keys with at least one entry that did not state a quantity (then the quantity is derived from notional / mark)
    if not isinstance(raw_pos, Mapping):
        R.append("POSITIONS_UNKNOWN")
    else:
        for sym, p in raw_pos.items():
            n = _num(p.get("notional_usd")) if isinstance(p, Mapping) else None
            cat = p.get("category") if isinstance(p, Mapping) else None
            q_raw = p.get("qty") if isinstance(p, Mapping) else None
            q = None if q_raw is None else _pos(q_raw)
            if n is None or cat not in _CATEGORIES or type(sym) is not str or (q_raw is not None and q is None):
                R.append(f"POSITION_UNKNOWN:{str(sym)[:24]}")
            elif sym.split("#", 1)[0].startswith(SPOT_PREFIX) != (cat == "spot"):
                R.append(f"POSITION_KEY_CATEGORY_MISMATCH:{str(sym)[:24]}")      # 'SPOT:X' labelled linear (or 'X' labelled spot) would net spot against perp
            else:
                k = (_base(sym), cat)
                pos[k] = pos.get(k, 0.0) + n
                if q is None:
                    pos_qty_lacking.add(k)
                else:
                    pos_qty[k] = pos_qty.get(k, 0.0) + (q if n > 0 else -q if n < 0 else 0.0)
    og = state.get("open_groups")
    if not (type(og) is int and og >= 0):
        R.append("OPEN_GROUPS_UNKNOWN")
        og = None

    # ---- legs: structure, allow-list, sides, order types, notional
    legs_info: List[Dict[str, Any]] = []
    for leg in plan.legs:
        tag = f"{leg.symbol}:{leg.leg_id}"
        if leg.symbol not in policy["allowed_symbols"]:
            R.append(f"SYMBOL_NOT_ALLOWED:{leg.symbol[:24]}")
        if leg.category not in _CATEGORIES:
            R.append(f"CATEGORY_UNKNOWN:{tag}")
        elif leg.side not in policy["allowed_sides"].get(leg.category, ()):
            R.append(f"SIDE_NOT_ALLOWED:{tag}:{leg.category}:{leg.side}")
        if leg.order_type not in policy["allowed_order_types"]:
            R.append(f"ORDER_TYPE_NOT_ALLOWED:{tag}")
        if not (type(leg.qty) is str and _PLAIN_DECIMAL.fullmatch(leg.qty)) or float(leg.qty) <= 0:
            R.append(f"QTY_INVALID:{tag}")
            continue
        if leg.order_type == "Market" and leg.price is not None:
            R.append(f"MARKET_WITH_PRICE:{tag}")
        if leg.order_type == "Limit" and leg.price is None:
            R.append(f"LIMIT_WITHOUT_PRICE:{tag}")
        px = _ref_price(leg, snapshots.get(leg.symbol))
        if px is None:
            R.append(f"PRICE_UNKNOWN:{tag}")
            continue
        notional = float(leg.qty) * px
        sign = 1.0 if leg.side == "Buy" else -1.0
        snap = snapshots.get(leg.symbol)
        legs_info.append({"leg": leg, "notional": notional, "signed": sign * notional, "key": (leg.symbol, leg.category),
                          "mark": _pos(snap.spot_mark if leg.category == "spot" else snap.mark) if snap is not None else None})

    # ---- exit / entry classification (only decided when every leg is understood)
    def held_qty(key: Tuple[str, str], mark: Optional[float]) -> Optional[float]:
        """Signed base units held at ``key``: the stated quantity if every entry stated one, else notional / mark; None = UNKNOWN (never zero)."""
        if key in pos_qty and key not in pos_qty_lacking:
            return pos_qty[key]
        return pos[key] / mark if key in pos and mark else None

    exit_only = bool(plan.legs) and len(legs_info) == len(plan.legs)
    if exit_only:
        reduce_qty: Dict[Tuple[str, str], float] = {}
        for li in legs_info:
            leg = li["leg"]
            reduces = False
            held = pos.get(li["key"])
            if held is not None and held != 0 and (held > 0) == (leg.side == "Sell"):
                reduces = (leg.reduce_only and leg.category == "linear") or (leg.category == "spot" and leg.side == "Sell")
            li["reduces"] = reduces
            if leg.reduce_only and leg.category == "linear" and not reduces:
                R.append(f"REDUCE_ONLY_NO_MATCHING_POSITION:{leg.symbol}")
            elif reduces:
                reduce_qty[li["key"]] = reduce_qty.get(li["key"], 0.0) + float(leg.qty)
                if leg.order_type == "Limit":                                  # price sanity: a Limit exit is priced near the market of its own category
                    if li["mark"] is None or abs(float(leg.price) - li["mark"]) > EXIT_MAX_PRICE_DEVIATION * li["mark"]:
                        R.append(f"EXIT_PRICE_OUT_OF_BAND:{leg.symbol}")
                elif not (leg.reduce_only or leg.category == "spot"):          # (unreachable by construction: kept as the stated rule)
                    R.append(f"EXIT_MARKET_NOT_REDUCE_ONLY:{leg.symbol}")
        for key, total in reduce_qty.items():                                  # CUMULATIVE per symbol across every reducing leg of the plan, by QUANTITY
            mark = next((li["mark"] for li in legs_info if li["key"] == key), None)
            hq = held_qty(key, mark)
            if hq is None:
                R.append(f"REDUCE_QTY_UNKNOWN:{key[0]}")
            elif total > abs(hq) * EXIT_QTY_SLACK:
                R.append(f"REDUCE_EXCEEDS_POSITION:{key[0]}")
        exit_only = all(li["reduces"] for li in legs_info)
    spot_sell_qty: Dict[str, float] = {}
    for li in legs_info:
        leg = li["leg"]
        if leg.category == "spot" and leg.side == "Sell":
            held = pos.get((leg.symbol, "spot"))
            if held is None or held <= 0:                                      # no key, a zero or a negative figure is NOT inventory
                R.append(f"SPOT_SELL_WITHOUT_INVENTORY:{leg.symbol}")
            elif not exit_only:                                                # (an exit was already bound by quantity above)
                spot_sell_qty[leg.symbol] = spot_sell_qty.get(leg.symbol, 0.0) + float(leg.qty)
    for sym, total in spot_sell_qty.items():
        hq = held_qty((sym, "spot"), next((li["mark"] for li in legs_info if li["key"] == (sym, "spot")), None))
        if hq is None or total > abs(hq) * EXIT_QTY_SLACK:
            R.append(f"SPOT_SELL_WITHOUT_INVENTORY:{sym}")
    for leg in plan.legs:                                                      # a spot sell whose price/qty could not even be read is not inventory either
        if leg.category == "spot" and leg.side == "Sell" and not any(li["leg"] is leg for li in legs_info) and (leg.symbol, "spot") not in pos:
            R.append(f"SPOT_SELL_WITHOUT_INVENTORY:{leg.symbol}")
    limits["exit_only"] = exit_only

    if exit_only and not any(r.startswith(("REDUCE_", "EXIT_", "SYMBOL_NOT_ALLOWED", "POSITION", "SPOT_SELL")) for r in R):
        # an exit is never refused for staleness or ceilings; structural / identity reasons above still stand
        return RiskDecision(not R, tuple(dict.fromkeys(R)), limits)

    # ---- freshness (entries only)
    now = plan.created_ms if _is_pos_int(plan.created_ms) else None
    for name, key, lim in (("MARKET", "market_ts_ms", policy["max_market_age_ms"]), ("ACCOUNT", "account_ts_ms", policy["max_account_age_ms"])):
        ts = state.get(key)
        if not (type(ts) is int and ts > 0):
            R.append(f"{name}_TS_UNKNOWN")
        elif now is not None:
            age = now - ts
            limits[f"{name.lower()}_age_ms"] = age
            if age < 0:
                R.append(f"{name}_TS_IN_FUTURE")
            elif age > lim:
                R.append(f"{name}_DATA_STALE")
    for leg in plan.legs:
        snap = snapshots.get(leg.symbol)
        if snap is not None:
            if snap.book_age_ms is None:
                R.append(f"BOOK_AGE_UNKNOWN:{leg.symbol}")
            elif snap.book_age_ms > policy["max_market_age_ms"]:
                R.append(f"BOOK_STALE:{leg.symbol}")

    # ---- post-trade exposure
    if equity is not None and len(legs_info) == len(plan.legs) and "POSITIONS_UNKNOWN" not in R and not any(r.startswith("POSITION_UNKNOWN") for r in R):
        book: Dict[Tuple[str, str], float] = dict(pos)
        for li in legs_info:
            book[li["key"]] = book.get(li["key"], 0.0) + li["signed"]
        per_base_spot = {b: v for (b, c), v in book.items() if c == "spot"}
        per_base_perp = {b: v for (b, c), v in book.items() if c == "linear"}
        bases = set(per_base_spot) | set(per_base_perp)
        per_symbol = {b: abs(per_base_spot.get(b, 0.0)) + abs(per_base_perp.get(b, 0.0)) for b in bases}
        aggregate = sum(max(abs(per_base_spot.get(b, 0.0)), abs(per_base_perp.get(b, 0.0))) for b in bases)
        gross = sum(abs(v) for v in book.values())
        net = abs(sum(book.values()))
        cur_net = sum(pos.values())
        unhedged = max([abs(cur_net)] + [abs(cur_net + li["signed"]) for li in legs_info])
        limits.update({"per_symbol": per_symbol, "aggregate": aggregate, "gross": gross, "net": net, "unhedged": unhedged,
                       "leverage": gross / equity, "open_groups_after": None if og is None else og + 1})
        for b, v in sorted(per_symbol.items()):
            if v > policy["max_notional_per_symbol_usd"]:
                R.append(f"PER_SYMBOL_NOTIONAL_ABOVE_CAP:{b}")
        if aggregate > policy["max_aggregate_notional_usd"]:
            R.append("AGGREGATE_NOTIONAL_ABOVE_CAP")
        if gross > policy["max_gross_exposure_usd"]:
            R.append("GROSS_EXPOSURE_ABOVE_CAP")
        if net > policy["max_net_exposure_usd"]:
            R.append("NET_EXPOSURE_ABOVE_CAP")
        if unhedged > policy["max_unhedged_usd"]:
            R.append("UNHEDGED_EXPOSURE_ABOVE_CAP")
        if gross / equity > policy["max_leverage"]:
            R.append("LEVERAGE_ABOVE_CAP")
    if og is not None and og + 1 > policy["max_active_groups"]:
        R.append("MAX_ACTIVE_GROUPS_REACHED")
    reasons = tuple(dict.fromkeys(R))
    return RiskDecision(not reasons, reasons, limits)
