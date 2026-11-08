"""Accounting of the multi-symbol layer: fills, funding, fees, marks -> per-leg / per-symbol / portfolio P&L with provenance. Pure: no clock, no I/O, no venue.

Principles (each is tested, each has a mutation):
  * UNKNOWN is not zero. A fee that was not stated, a zero fee on a real fill that nobody measured, a rebate, a mark that does not exist for an open leg and a perp symbol
    with no funding record (and no explicit "no funding applied" declaration) make that component ``None``, and ``net`` becomes ``None``. A flag says why.
  * Unreconciled is not profit. ``net`` is a mark-to-market figure; ``claimable_profit`` is non-None ONLY when every leg is flat, every fee and every funding component is
    known, and nothing is flagged. A ledger with open legs or unknown components never reports a clean profit.
  * WHAT "RECONCILED" MEANS HERE. ``status == "RECONCILED"`` and ``claimable_profit`` are INTERNAL CONSISTENCY: the ledger's own fills net to flat and every component is known. They
    are NOT a comparison with the venue (FLAT means "the venue's own position read says flat"; fill arithmetic is not that read). Every report therefore carries
    ``reconciliation_basis`` (``INTERNAL_CONSISTENCY_ONLY`` unless a venue read was injected), ``internally_consistent``, ``venue_reconciled`` and ``venue_claimable_profit``. The
    last two are True / non-None ONLY from ``Ledger.pnl(venue_positions=...)`` when the injected venue position read matches the ledger on every (symbol, category) and the
    ledger is otherwise consistent. The old ``status`` / ``claimable_profit`` keys keep their values and names (consumers depend on them); read the new keys for what they prove.
  * Duplicate fills (same ``fill_id``) are ignored; a duplicate whose content differs is flagged ``DUPLICATE_FILL_CONFLICT`` and the ledger is unreconciled.
  * FUNDING SIGN CONVENTION (explicit): ``amount_usd`` > 0 is cash RECEIVED by this account, < 0 is cash PAID. For a funding rate ``r`` (fraction per print, positive = longs
    pay shorts) the cash flow of a position with signed notional ``N`` (long > 0, short < 0) is ``-N * r`` (``funding_cashflow``). A short in positive funding receives.
  * Fees are costs: ``fees`` is >= 0 and is SUBTRACTED. A negative fee (rebate) is never assumed: it flags ``FEE_REBATE_UNVERIFIED`` and the component is unknown.
  * ``net = price_leg + funding - fees`` with ``price_leg = realised_price + unrealised_price`` (average-cost). ``slippage`` is an ATTRIBUTION of the price leg versus the
    reference price the caller gave at fill time (already inside ``price_leg``; never deducted a second time); None when a reference was not given.
  * Interval conversion lives in ``carry_costs`` (``funding_bps_per_day``); this module only books cash.

  * EXACT ARITHMETIC (decimal.Decimal). Every quantity, price, fee, funding amount and P&L figure is held as ``Decimal``. Inputs may be int / float / str / Decimal; a float is converted with
    ``Decimal(str(x))`` (the shortest repr, so 0.1 is exactly 0.1, never 0.1000000000000000055...); bool and non-finite values are refused. Quantisation rules: addition, subtraction and
    multiplication are EXACT (no rounding at all); the ONLY division is the proportional cost removal of a PARTIAL close of an open leg, rounded half-even to 1e-18 USD (a full close is exact);
    a residual open quantity of at most ``DUST_QTY`` (1e-12, float noise of upstream sizing) is zeroed and reported as ``dust_qty``. OUTPUTS: ``pnl_exact()`` returns Decimals; ``pnl()`` /
    ``canonical_net()`` / ``account()`` convert to float ONLY at that artifact boundary (``float(Decimal)`` is correctly rounded), keeping every key and shape consumers use.
  * ORDER-INDEPENDENT AND IDEMPOTENT. Fills are stored keyed by ``fill_id`` and the book is a pure fold over them sorted by ``(ts_ms, fill_id)``: any arrival order (reordered, duplicated,
    delayed) gives the same state. A duplicate is ignored; a duplicate with different content is ``DUPLICATE_FILL_CONFLICT`` (the first stays booked). Funding is keyed ``(symbol, ts_ms)``.
  * ORPHAN FILLS. ``Ledger(track_acks=True)`` demands ``acknowledge(group_id, leg_id)`` for a leg before its fills count as acknowledged: a fill booked first (a restart between order
    acceptance and acknowledgement) is accepted, flagged ``ORPHAN_FILL:<group>:<leg>`` and the ledger stays unreconciled until the acknowledgement arrives.
  * ACCOUNT EVENTS are not P&L. Deposits / withdrawals (``record_account_event``, keyed by ``event_id``) move the balance and never ``net``. ``account()`` gives equity = balance +
    unrealised with every component explicit; an unknown component makes the dependent figure ``None``. Margin fields are optional (``set_margin``) and reported as unknown when not given.
  * VENUE RECONCILIATION is ``reconcile_with_venue``: fills keyed by execId, positions, wallet change and fees against an injected venue read. ``venue_reconciled`` is True ONLY after an
    exact agreement within the stated tolerances, and only while nothing was booked since (a later booking makes it stale -> False).
"""
from __future__ import annotations

import json
import math
import os
import sys
from dataclasses import dataclass, field
from decimal import Decimal, ROUND_HALF_EVEN
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ms_types import Fill, weakest  # noqa: E402

_FILL_PROV_MAP = {"SIMULATED": "MODELLED", "MODELLED": "MODELLED", "MEASURED": "MEASURED"}
_PROV_OK = ("MEASURED", "VENUE_STATED", "DERIVED", "MODELLED", "ASSUMED", "UNKNOWN")
ZERO = Decimal(0)
DUST_QTY = Decimal("1e-12")                 # residual open quantity treated as flat (float noise of upstream sizing); reported as ``dust_qty``
COST_QUANTUM = Decimal("1e-18")             # the single rounding in this module: cost removed by a PARTIAL close (half-even)
EPS = 1e-12                                 # kept for importers; the ledger itself uses DUST_QTY
#: tolerances of ``reconcile_with_venue`` (stated, not hidden): quantity exact; price relative 1e-8; USD amounts absolute 1e-6.
VENUE_TOL = {"qty": Decimal("1e-9"), "price_rel": Decimal("1e-8"), "usd": Decimal("1e-6")}
INTEGRITY_FLAG_PREFIXES = ("DUPLICATE_FILL_CONFLICT", "LEG_IDENTITY_CONFLICT", "FUNDING_CONFLICT", "ORPHAN_FILL", "MARK_CONFLICT", "ACCOUNT_EVENT_CONFLICT")
STATUS_MEANING = ("RECONCILED = internally consistent (flat per the ledger's own fills, every fee/funding component known); it is NOT a venue position read. "
                  "See venue_reconciled / reconciliation_basis.")


class LedgerError(ValueError):
    """A record that cannot be booked at all (not a unit mistake we could guess about)."""


def to_decimal(x: Any) -> Optional[Decimal]:
    """``Decimal(str(x))`` for int / float / str / Decimal; None for bool, None, NaN / inf and anything unparsable. The ONE conversion rule of this module."""
    if isinstance(x, bool) or x is None:
        return None
    try:
        d = x if isinstance(x, Decimal) else Decimal(str(x)) if isinstance(x, (int, float, str)) else None
    except Exception:                                                   # noqa: BLE001
        return None
    return d if d is not None and d.is_finite() else None


def funding_cashflow(signed_notional_usd: float, rate_frac_per_print: float) -> float:
    """Cash flow of one funding print for a position of signed notional N (long > 0): ``-N * r``. Positive rate: longs pay, shorts receive. (Computed exactly, returned as float.)"""
    if not all(isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) for x in (signed_notional_usd, rate_frac_per_print)):
        raise LedgerError("funding_cashflow needs finite numbers")
    return float(-Decimal(str(signed_notional_usd)) * Decimal(str(rate_frac_per_print)))


def _fin(x: Any) -> Optional[float]:
    return float(x) if isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) else None


def _to_float(o: Any) -> Any:
    """The artifact boundary: Decimal -> float, recursively; everything else untouched."""
    if isinstance(o, Decimal):
        return float(o)
    if isinstance(o, dict):
        return {k: _to_float(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_to_float(v) for v in o]
    return o


def _key(symbol: str, category: str) -> str:
    return symbol if category == "linear" else "SPOT:" + symbol


@dataclass
class _Leg:
    group_id: str
    leg_id: str
    symbol: str
    category: str
    qty: Decimal = ZERO              # signed base units: long > 0
    cost: Decimal = ZERO             # cost of the |open quantity| (positive); avg = cost / |qty|
    realised_price: Decimal = ZERO
    fees: Optional[Decimal] = ZERO   # None = at least one fee unknown
    fee_flags: List[str] = field(default_factory=list)
    slippage: Optional[Decimal] = ZERO   # cost vs reference (>0 = worse); None = a reference was missing
    provs: List[str] = field(default_factory=list)
    n_fills: int = 0
    dust: Decimal = ZERO
    order_legs: Set[str] = field(default_factory=set)      # the ORDER leg ids whose fills were booked into this position leg (acknowledged individually)

    @property
    def avg(self) -> Decimal:
        return self.cost / abs(self.qty) if self.qty != 0 else ZERO


class Ledger:
    def __init__(self, track_acks: bool = False) -> None:
        self.track_acks = bool(track_acks)
        self._fills: Dict[str, Dict[str, Any]] = {}
        self._marks: Dict[Tuple[str, str], Tuple[int, Decimal, str, Optional[Decimal], Optional[Decimal]]] = {}
        self._fund: Dict[str, Dict[int, Tuple[Optional[Decimal], str]]] = {}
        self._fund_none: Dict[str, Tuple[str, str]] = {}          # symbol -> (reason, prov): "no funding was applied", declared
        self._flags: Set[str] = set()
        self._acks: Dict[Tuple[str, str], Optional[str]] = {}
        self._slip: Dict[str, Dict[str, Decimal]] = {}               # symbol -> {"taker", "adverse"}
        self._slip_keys: Set[str] = set()
        self._exit_fee_bps = {"linear": ZERO, "spot": ZERO}
        self._opening: Optional[Tuple[Decimal, Optional[int]]] = None
        self._events: Dict[str, Tuple[str, Decimal, int]] = {}
        self._margin: Dict[str, Any] = {"used_usd": None, "available_usd": None, "ts_ms": None}
        self._ver = 0
        self._venue_recon: Optional[Dict[str, Any]] = None
        self._cache: Optional[Tuple[int, Dict[Tuple[str, str], _Leg]]] = None
        self.duplicates_ignored = 0

    @property
    def _funding(self) -> Dict[str, Dict[int, Tuple[Optional[float], str]]]:
        """READ-ONLY float view of the funding book ({symbol: {ts_ms: (amount | None, prov)}}) for legacy readers (ms_pipeline's tests). The book itself is ``_fund`` (Decimal)."""
        return {s: {ts: (None if a is None else float(a), p) for ts, (a, p) in b.items()} for s, b in self._fund.items()}

    def _bump(self) -> None:
        self._ver += 1
        self._cache = None

    # ------------------------------------------------------------ booking
    def record_fill(self, fill: Fill, *, category: str = "linear", ref_price: Optional[float] = None, position_leg: Optional[str] = None) -> bool:
        """Book a fill. False if its ``fill_id`` was already booked (ignored). ``category`` 'linear' | 'spot' (spot has no funding). ``ref_price`` is the price the plan
        expected (for the slippage attribution). ``position_leg`` (optional) books the fill into the POSITION leg of that name instead of its order leg: exit orders carry NEW
        leg ids, and booked under their own id they would sit as a second open leg (the pair would never be flat); ``acknowledge`` still applies to the ORDER leg id. Arrival
        order is irrelevant (see the module docstring)."""
        if category not in ("linear", "spot"):
            raise LedgerError(f"category {category!r}")
        if not (isinstance(fill.fill_id, str) and fill.fill_id.strip()):
            raise LedgerError("fill_id missing")
        if fill.side not in ("Buy", "Sell"):
            raise LedgerError(f"side {fill.side!r}")
        qty, px = to_decimal(fill.qty), to_decimal(fill.price)
        if qty is None or px is None or qty <= 0 or px <= 0:
            raise LedgerError("fill qty/price must be finite and positive")
        if type(fill.ts_ms) is not int or fill.ts_ms <= 0:
            raise LedgerError("fill ts_ms must be a positive int")
        fee = to_decimal(fill.fee_usd)
        sig = (fill.group_id, fill.leg_id, fill.symbol, fill.side, qty, px, category, fee, fill.ts_ms)
        if fill.fill_id in self._fills:
            self.duplicates_ignored += 1
            if self._fills[fill.fill_id]["sig"] != sig:
                self._flags.add(f"DUPLICATE_FILL_CONFLICT:{fill.fill_id}")
                self._bump()
            return False
        if position_leg is not None and not (isinstance(position_leg, str) and position_leg):
            raise LedgerError("position_leg must be a non-empty string")
        self._fills[fill.fill_id] = {"pos_leg": position_leg, "sig": sig, "fill": fill, "category": category, "qty": qty, "px": px, "fee": fee, "ref": to_decimal(ref_price)}
        self._bump()
        return True

    def acknowledge(self, group_id: str, leg_id: str, order_id: Optional[str] = None) -> None:
        """The order of this leg was acknowledged (the venue's / engine's own record). Clears ``ORPHAN_FILL`` for the leg. Idempotent."""
        if not (isinstance(group_id, str) and group_id and isinstance(leg_id, str) and leg_id):
            raise LedgerError("acknowledge needs a group_id and a leg_id")
        if (group_id, leg_id) not in self._acks:
            self._acks[(group_id, leg_id)] = order_id
            self._bump()

    def record_funding(self, symbol: str, ts_ms: int, amount_usd: Optional[float], prov: str) -> bool:
        """Book one funding cash flow (> 0 received, < 0 paid). ``amount_usd`` None = the print happened but the amount is UNKNOWN (the symbol's funding becomes unknown).
        A second record for the same (symbol, ts_ms) is ignored; a different amount flags ``FUNDING_CONFLICT``."""
        if type(ts_ms) is not int or ts_ms <= 0 or not isinstance(symbol, str) or not symbol:
            raise LedgerError("funding needs a symbol and a positive int ts_ms")
        if prov not in _PROV_OK:
            raise LedgerError(f"provenance {prov!r}")
        amt = to_decimal(amount_usd)
        book = self._fund.setdefault(symbol, {})
        if ts_ms in book:
            if book[ts_ms][0] != amt:
                self._flags.add(f"FUNDING_CONFLICT:{symbol}:{ts_ms}")
                self._bump()
            return False
        book[ts_ms] = (amt, prov)
        self._bump()
        return True

    def declare_no_funding(self, symbol: str, reason: str, prov: str = "DERIVED") -> None:
        """The ONLY way a perp symbol's funding is zero: an explicit statement (e.g. 'held between two prints') with a reason. Absence of a record is UNKNOWN, not zero."""
        if not (isinstance(reason, str) and reason.strip()) or prov not in _PROV_OK:
            raise LedgerError("declare_no_funding needs a reason and a provenance")
        self._fund_none[symbol] = (reason, prov)
        self._bump()

    def mark(self, symbol: str, ts_ms: int, price: float, *, category: str = "linear", prov: str = "MEASURED", bid: Optional[float] = None, ask: Optional[float] = None) -> bool:
        """Latest mark per (symbol, category). A mark older than the one held is ignored; a different mark at the SAME ts is ``MARK_CONFLICT`` (the first stays). A non-finite /
        non-positive price is refused. ``bid`` / ``ask`` (optional, the book at the mark) let ``canonical_net`` value an open leg at the EXIT side."""
        p = to_decimal(price)
        if p is None or p <= 0 or type(ts_ms) is not int or ts_ms <= 0 or category not in ("linear", "spot") or prov not in _PROV_OK:
            raise LedgerError("mark needs a positive finite price, a positive int ts_ms, a category and a provenance")
        b, a = to_decimal(bid), to_decimal(ask)
        if not (b is not None and a is not None and 0 < b <= a):
            b = a = None
        cur = self._marks.get((symbol, category))
        if cur is not None and ts_ms < cur[0]:
            return False
        if cur is not None and ts_ms == cur[0]:
            if (cur[1], cur[2]) != (p, prov):
                self._flags.add(f"MARK_CONFLICT:{symbol}:{category}:{ts_ms}")
                self._bump()
            return False
        self._marks[(symbol, category)] = (ts_ms, p, prov, b, a)
        self._bump()
        return True

    def record_slippage(self, usd: float, kind: str, symbol: Optional[str] = None, key: Optional[str] = None) -> bool:
        """Attribution booked OUTSIDE the fill prices: ``kind`` 'taker' (the modelled walk of the book; it IS inside the fill price, so it is added back to the price leg at mid and
        charged once) or 'adverse' (post-fill markout, in no fill price). ``key`` makes it idempotent. False for a duplicate key."""
        v = to_decimal(usd)
        if v is None or kind not in ("taker", "adverse"):
            raise LedgerError("slippage needs a finite amount and kind 'taker' | 'adverse'")
        if key is not None:
            if key in self._slip_keys:
                return False
            self._slip_keys.add(key)
        d = self._slip.setdefault(symbol or "", {"taker": ZERO, "adverse": ZERO})
        d[kind] += v
        self._bump()
        return True

    def set_exit_fee_bps(self, perp_bps: float = 0.0, spot_bps: float = 0.0) -> None:
        """The modelled taker fee of CLOSING an open leg (used only by ``canonical_net``)."""
        p, s = to_decimal(perp_bps), to_decimal(spot_bps)
        if p is None or s is None or p < 0 or s < 0:
            raise LedgerError("exit fee bps must be finite and >= 0")
        self._exit_fee_bps = {"linear": p, "spot": s}
        self._bump()

    # ------------------------------------------------------------ account events (never P&L)
    def set_opening_balance(self, usd: float, ts_ms: Optional[int] = None) -> None:
        v = to_decimal(usd)
        if v is None:
            raise LedgerError("opening balance must be finite")
        self._opening = (v, ts_ms)
        self._bump()

    def record_account_event(self, event_id: str, ts_ms: int, kind: str, amount_usd: float) -> bool:
        """A DEPOSIT or WITHDRAWAL (amount > 0). Moves the balance and equity, NEVER ``net``. Keyed by ``event_id``: a repeat is ignored, a different repeat flags
        ``ACCOUNT_EVENT_CONFLICT``."""
        v = to_decimal(amount_usd)
        if not (isinstance(event_id, str) and event_id) or kind not in ("DEPOSIT", "WITHDRAWAL") or v is None or v <= 0 or type(ts_ms) is not int or ts_ms <= 0:
            raise LedgerError("account event needs an id, a kind DEPOSIT|WITHDRAWAL, a positive amount and a positive int ts_ms")
        if event_id in self._events:
            if self._events[event_id] != (kind, v, ts_ms):
                self._flags.add(f"ACCOUNT_EVENT_CONFLICT:{event_id}")
                self._bump()
            return False
        self._events[event_id] = (kind, v, ts_ms)
        self._bump()
        return True

    def set_margin(self, used_usd: Optional[float] = None, available_usd: Optional[float] = None, ts_ms: Optional[int] = None) -> None:
        """Optional margin figures (venue-read). Never defaulted: unset stays None and ``account()`` says ``margin_known: False``."""
        self._margin = {"used_usd": to_decimal(used_usd), "available_usd": to_decimal(available_usd), "ts_ms": ts_ms}
        self._bump()

    # ------------------------------------------------------------ the fold
    def _legs(self) -> Dict[Tuple[str, str], _Leg]:
        if self._cache is not None and self._cache[0] == self._ver:
            return self._cache[1]
        legs: Dict[Tuple[str, str], _Leg] = {}
        for fid in sorted(self._fills, key=lambda i: (self._fills[i]["fill"].ts_ms, i)):
            r = self._fills[fid]
            fill, category, qty, px = r["fill"], r["category"], r["qty"], r["px"]
            pleg = r["pos_leg"] or fill.leg_id
            leg = legs.setdefault((fill.group_id, pleg), _Leg(fill.group_id, pleg, fill.symbol, category))
            leg.order_legs.add(fill.leg_id)
            if (leg.symbol, leg.category) != (fill.symbol, category):
                self._flags.add(f"LEG_IDENTITY_CONFLICT:{fill.group_id}:{pleg}")
            leg.n_fills += 1
            leg.provs.append(_FILL_PROV_MAP.get(fill.prov, "UNKNOWN"))
            fee = r["fee"]                                                   # a fee is never defaulted
            if fee is None:
                leg.fees = None
                leg.fee_flags.append(f"FEE_UNKNOWN:{fid}")
            elif fee < 0:
                leg.fees = None
                leg.fee_flags.append(f"FEE_REBATE_UNVERIFIED:{fid}")        # rebates are never assumed
            elif fee == 0 and fill.prov != "MEASURED":
                leg.fees = None
                leg.fee_flags.append(f"ZERO_FEE_UNVERIFIED:{fid}")         # a free fill nobody measured is not a free fill
            elif leg.fees is not None:
                leg.fees += fee
            ref = r["ref"]
            if ref is None or ref <= 0:
                leg.slippage = None
            elif leg.slippage is not None:
                leg.slippage += (px - ref) * qty * (1 if fill.side == "Buy" else -1)
            signed = qty if fill.side == "Buy" else -qty
            if leg.qty == 0 or (leg.qty > 0) == (signed > 0):
                leg.qty += signed
                leg.cost += px * qty
            else:
                held = abs(leg.qty)
                closing = min(held, qty)
                removed = leg.cost if closing == held else (leg.cost * closing / held).quantize(COST_QUANTUM, rounding=ROUND_HALF_EVEN)
                proceeds = px * closing
                leg.realised_price += (proceeds - removed) if leg.qty > 0 else (removed - proceeds)
                leg.cost -= removed
                rest = qty - closing
                leg.qty = leg.qty - closing if leg.qty > 0 else leg.qty + closing
                if rest > 0:                                                 # flipped through zero
                    leg.qty = rest if signed > 0 else -rest
                    leg.cost = px * rest
            if leg.qty != 0 and abs(leg.qty) <= DUST_QTY:
                leg.dust, leg.qty, leg.cost = leg.qty, ZERO, ZERO
        self._cache = (self._ver, legs)
        return legs

    # ------------------------------------------------------------ positions (the ledger's derived view)
    def positions(self) -> Dict[str, Decimal]:
        """Signed base units per canonical key ('SYM' for linear, 'SPOT:SYM' for spot), summed over legs; zero-quantity keys are present (the ledger knows them)."""
        out: Dict[str, Decimal] = {}
        for l in self._legs().values():
            k = _key(l.symbol, l.category)
            out[k] = out.get(k, ZERO) + l.qty
        return dict(sorted(out.items()))

    def positions_detail(self) -> Dict[str, Dict[str, Optional[Decimal]]]:
        """{key: {"qty", "avg_price"}} with the quantity-weighted average entry of the open legs (None when flat)."""
        qty: Dict[str, Decimal] = {}
        cost: Dict[str, Decimal] = {}
        for l in self._legs().values():
            k = _key(l.symbol, l.category)
            qty[k] = qty.get(k, ZERO) + l.qty
            cost[k] = cost.get(k, ZERO) + (l.cost if l.qty > 0 else -l.cost)
        return {k: {"qty": q, "avg_price": (cost[k] / q) if q != 0 else None} for k, q in sorted(qty.items())}

    def fills(self) -> List[Dict[str, Any]]:
        """The booked fills (for reconciliation), sorted by (ts, id): fill_id, group_id, leg_id, symbol, category, side, qty, price, fee (Decimal / None)."""
        out = []
        for fid in sorted(self._fills, key=lambda i: (self._fills[i]["fill"].ts_ms, i)):
            r = self._fills[fid]
            f = r["fill"]
            out.append({"fill_id": fid, "group_id": f.group_id, "leg_id": f.leg_id, "symbol": f.symbol, "category": r["category"], "side": f.side, "qty": r["qty"], "price": r["px"],
                        "fee": r["fee"], "ts_ms": f.ts_ms})
        return out

    def funding_events(self) -> Dict[Tuple[str, int], Optional[Decimal]]:
        return {(s, ts): a for s, book in sorted(self._fund.items()) for ts, (a, _p) in sorted(book.items())}

    def integrity_flags(self) -> List[str]:
        """Flags that mean two sources of truth disagreed INSIDE the ledger (conflicting duplicates, orphan fills, ...)."""
        self._legs()
        out = set(f for f in self._flags if f.startswith(INTEGRITY_FLAG_PREFIXES))
        out.update(self._orphans())
        return sorted(out)

    def _orphans(self) -> List[str]:
        if not self.track_acks:
            return []
        return sorted(f"ORPHAN_FILL:{l.group_id}:{o}" for l in self._legs().values() for o in l.order_legs if (l.group_id, o) not in self._acks)

    # ------------------------------------------------------------ reporting
    def _leg_report(self, leg: _Leg) -> Dict[str, Any]:
        flags = list(leg.fee_flags)
        prov = weakest(*leg.provs) if leg.provs else "UNKNOWN"
        open_ = leg.qty != 0
        unreal: Optional[Decimal] = ZERO
        if open_:
            m = self._marks.get((leg.symbol, leg.category))
            if m is None:
                unreal = None
                flags.append(f"NO_MARK_FOR_OPEN_LEG:{leg.symbol}:{leg.category}")
            else:
                unreal = m[1] * abs(leg.qty) - leg.cost if leg.qty > 0 else leg.cost - m[1] * abs(leg.qty)
                prov = weakest(prov, m[2], "MODELLED")                         # a mark-to-market is at best modelled
            flags.append(f"OPEN_LEG:{leg.group_id}:{leg.leg_id}")
        if self.track_acks:
            flags += [f"ORPHAN_FILL:{leg.group_id}:{o}" for o in sorted(leg.order_legs) if (leg.group_id, o) not in self._acks]
        return {"group_id": leg.group_id, "leg_id": leg.leg_id, "symbol": leg.symbol, "category": leg.category, "open": open_, "qty": leg.qty,
                "realised_price": leg.realised_price, "unrealised_price": unreal, "fees": leg.fees, "slippage": leg.slippage, "prov": prov, "flags": flags, "dust_qty": leg.dust}

    def _funding_for(self, symbol: str) -> Tuple[Optional[Decimal], str, List[str]]:
        recs = self._fund.get(symbol, {})
        if recs:
            amts = [a for a, _p in recs.values()]
            prov = weakest(*(p for _a, p in recs.values()))
            if any(a is None for a in amts):
                return None, prov, [f"FUNDING_AMOUNT_UNKNOWN:{symbol}"]
            return sum(amts, ZERO), prov, []
        if symbol in self._fund_none:
            return ZERO, self._fund_none[symbol][1], []
        return None, "UNKNOWN", [f"FUNDING_NOT_RECORDED:{symbol}"]

    @staticmethod
    def _sum(vals: List[Optional[Decimal]]) -> Optional[Decimal]:
        return None if any(v is None for v in vals) else sum(vals, ZERO)

    def _rollup(self, legs: List[Dict[str, Any]], symbols: List[str], *, with_funding: bool = True) -> Dict[str, Any]:
        flags: List[str] = [f for l in legs for f in l["flags"]]
        provs = [l["prov"] for l in legs]
        realised_price = self._sum([l["realised_price"] for l in legs])
        unreal = self._sum([l["unrealised_price"] for l in legs])
        fees = self._sum([l["fees"] for l in legs])
        slip = self._sum([l["slippage"] for l in legs])
        fvals, fprovs = [], []
        for s in (symbols if with_funding else []):
            if any(l["symbol"] == s and l["category"] == "linear" for l in legs):
                v, p, fl = self._funding_for(s)
                fvals.append(v)
                fprovs.append(p)
                flags += fl
        funding = self._sum(fvals)
        price_leg = None if realised_price is None or unreal is None else realised_price + unreal
        if not with_funding:                                        # funding belongs to a SYMBOL, not a leg: never allocated, never silently zero
            funding = None
            flags.append("FUNDING_NOT_ALLOCATED_TO_LEG")
        net = None if price_leg is None or funding is None or fees is None else price_leg + funding - fees
        realised = None if funding is None or fees is None or realised_price is None else realised_price + funding - fees
        if fees is None:
            flags.append("FEES_UNKNOWN")
        any_open = any(l["open"] for l in legs)
        flags = list(dict.fromkeys(flags))
        reconciled = with_funding and bool(legs) and not any_open and net is not None and not [f for f in flags if not f.startswith("OPEN_LEG")]
        basis = {"reconciliation_basis": "INTERNAL_CONSISTENCY_ONLY", "internally_consistent": bool(reconciled), "venue_reconciled": False, "venue_claimable_profit": None,
                 "internal_status": "INTERNALLY_CONSISTENT" if reconciled else "NOT_INTERNALLY_CONSISTENT", "status_meaning": STATUS_MEANING}
        return {"price_leg": price_leg, "funding": funding, "fees": fees, "slippage": slip, "realised": realised, "unrealised": unreal, "unrealised_price": unreal, "realised_price": realised_price,
                "qty": sum((l["qty"] for l in legs), ZERO), "dust_qty": sum((l["dust_qty"] for l in legs), ZERO), "net": net,
                "claimable_profit": net if reconciled else None, "status": "RECONCILED" if reconciled else "UNRECONCILED",
                "prov": weakest(*provs, *fprovs) if (provs or fprovs) else "UNKNOWN", "flags": flags, "n_legs": len(legs), **basis}

    def _venue_check(self, venue_positions: Any, legs: List[Dict[str, Any]]) -> Tuple[str, List[str]]:
        """Compare an injected venue position read with the ledger. ``venue_positions``: {"SYMBOL" (linear) | "SPOT:SYMBOL": signed base units, None = the read failed}. Every
        (symbol, category) the ledger has a leg for must be present, known and equal; a non-zero venue position the ledger has no leg for is a mismatch."""
        if not isinstance(venue_positions, Mapping):
            return "VENUE_READ_INVALID", ["VENUE_READ_INVALID"]
        flags: List[str] = []
        ledger: Dict[str, Decimal] = {}
        for l in legs:
            k = _key(l["symbol"], l["category"])
            ledger[k] = ledger.get(k, ZERO) + l["qty"]
        for k, q in sorted(ledger.items()):
            v = to_decimal(venue_positions.get(k)) if k in venue_positions else None
            if v is None:
                flags.append(f"VENUE_POSITION_UNKNOWN:{k}")
            elif abs(v - q) > Decimal("1e-9") + Decimal("1e-9") * max(abs(v), abs(q)):
                flags.append(f"VENUE_POSITION_MISMATCH:{k}")
        for k, v in sorted(venue_positions.items(), key=lambda kv: str(kv[0])):
            if k not in ledger:
                fv = to_decimal(v)
                if fv is None or abs(fv) > DUST_QTY:
                    flags.append(f"VENUE_POSITION_NOT_IN_LEDGER:{k}")
        return ("VENUE_POSITION_READ_MATCHED" if not flags else "VENUE_POSITION_READ_MISMATCH"), flags

    def pnl_exact(self, venue_positions: Optional[Mapping[str, Optional[float]]] = None) -> Dict[str, Any]:
        """``pnl()`` with Decimal values (no float anywhere). Same keys and shapes."""
        legs = [self._leg_report(l) for l in self._legs().values()]
        syms = sorted({l["symbol"] for l in legs})
        out = self._rollup(legs, syms)
        extra = sorted(self._flags) + self._orphans()
        out["flags"] = list(dict.fromkeys(out["flags"] + extra))
        if self._flags or self._orphans():
            out["claimable_profit"], out["status"] = None, "UNRECONCILED"
            out["internally_consistent"], out["internal_status"] = False, "NOT_INTERNALLY_CONSISTENT"
        if venue_positions is not None:
            basis, vflags = self._venue_check(venue_positions, legs)
            out["reconciliation_basis"] = basis
            out["flags"] = list(dict.fromkeys(out["flags"] + vflags))
            if basis == "VENUE_POSITION_READ_MATCHED" and out["internally_consistent"]:
                out["venue_reconciled"], out["venue_claimable_profit"] = True, out["net"]
        vr = self._venue_recon
        if vr is not None and venue_positions is None:
            if vr["version"] != self._ver:
                out["reconciliation_basis"] = "VENUE_RECONCILIATION_STALE"
                out["flags"] = list(dict.fromkeys(out["flags"] + ["VENUE_RECONCILIATION_STALE"]))
            elif vr["ok"]:
                out["reconciliation_basis"] = "VENUE_EXECUTIONS_POSITIONS_FEES"
                if out["internally_consistent"]:
                    out["venue_reconciled"], out["venue_claimable_profit"] = True, out["net"]
            else:
                out["reconciliation_basis"] = "VENUE_RECONCILIATION_MISMATCH"
                out["flags"] = list(dict.fromkeys(out["flags"] + [f["code"] + ":" + f["key"] for f in vr["findings"]]))
        out["per_symbol"] = {s: self._rollup([l for l in legs if l["symbol"] == s], [s]) for s in syms}
        out["per_leg"] = {f"{l['group_id']}/{l['leg_id']}": self._rollup([l], [l["symbol"]], with_funding=False) for l in legs}
        out["duplicates_ignored"] = self.duplicates_ignored
        return out

    def pnl(self, venue_positions: Optional[Mapping[str, Optional[float]]] = None) -> Dict[str, Any]:
        """Portfolio P&L with ``per_leg`` and ``per_symbol`` rollups (same keys). FLOATS at this artifact boundary (``pnl_exact`` has the Decimals). See the module docstring for the
        definitions, for when ``claimable_profit`` is None and for what ``status`` does and does not mean. ``venue_positions`` (optional, injected; this module reads no venue): a venue
        position read to compare with; only then (or after ``reconcile_with_venue``) can ``venue_reconciled`` be True. Per-symbol and per-leg rollups are always internal-only."""
        return _to_float(self.pnl_exact(venue_positions))

    # ------------------------------------------------------------ the single net definition
    def canonical_net(self, *, strict_exit: bool = False) -> Dict[str, Any]:
        """THE net P&L: ``net = price leg at MID + funding - fees - taker slippage - adverse selection`` (each once), where an OPEN leg is valued at the EXIT side of its book (a long at
        the bid, a short at the ask) less the modelled taker exit fee, and the taker walk (inside the fill prices) is added back to the price leg at mid before being charged once as slippage.
        ``None`` (never zero) when a component is unknown; an open leg without a book is valued at the mark and flagged ``EXIT_PRICE_MISSING`` (``valuation == 'MID_FALLBACK'``), and with
        ``strict_exit=True`` that makes ``net`` None. ``ms_accounting``'s own ``pnl()['net']`` omits slippage attribution and adverse selection and is NOT this figure. Floats."""
        p = self.pnl_exact()
        taker = sum((d["taker"] for d in self._slip.values()), ZERO)
        adverse = sum((d["adverse"] for d in self._slip.values()), ZERO)
        flags = list(p["flags"])
        adj, missing_exit = ZERO, False
        net_qty: Dict[Tuple[str, str], Decimal] = {}
        for l in self._legs().values():                                         # the exit value belongs to the NET position of a (symbol, category), not to each booking leg
            net_qty[(l.symbol, l.category)] = net_qty.get((l.symbol, l.category), ZERO) + l.qty
        for (sym, cat), q in sorted(net_qty.items()):
            if q == 0:
                continue
            m = self._marks.get((sym, cat))
            if m is None:
                continue
            if m[3] is None or m[4] is None:
                missing_exit = True
                continue
            px = m[3] if q > 0 else m[4]
            adj += (px - m[1]) * q - abs(q) * px * self._exit_fee_bps[cat] / Decimal(10000)
        if missing_exit:
            flags.append("EXIT_PRICE_MISSING")
        price_leg = None if p["price_leg"] is None else p["price_leg"] + taker + adj
        gross = None if price_leg is None or p["funding"] is None else price_leg + p["funding"]
        net = None if gross is None or p["fees"] is None else gross - p["fees"] - taker - adverse
        if strict_exit and missing_exit:
            net = None
        out = {"net": net, "price_leg": price_leg, "funding": p["funding"], "fees": p["fees"], "taker_slippage": taker, "adverse_selection": adverse, "exit_adjustment": adj,
               "valuation": "MID_FALLBACK" if missing_exit else "EXIT_SIDE", "net_complete": net is not None, "flags": list(dict.fromkeys(flags)), "ledger_net": p["net"]}
        return _to_float(out)

    # ------------------------------------------------------------ account
    def account(self) -> Dict[str, Any]:
        """balance = opening + deposits - withdrawals + realised net (realised price + funding - fees); equity = balance + unrealised. Every component explicit; a figure is None when
        a component is unknown. Deposits and withdrawals are here and never in ``net``. Margin fields are None / ``margin_known False`` unless ``set_margin`` was called. Floats."""
        p = self.pnl_exact()
        dep = sum((v for k, v, _t in self._events.values() if k == "DEPOSIT"), ZERO)
        wd = sum((v for k, v, _t in self._events.values() if k == "WITHDRAWAL"), ZERO)
        opening = self._opening[0] if self._opening else None
        realised, unreal = p["realised"], p["unrealised"]
        balance = None if opening is None or realised is None else opening + dep - wd + realised
        equity = None if balance is None or unreal is None else balance + unreal
        flags = []
        if opening is None:
            flags.append("OPENING_BALANCE_UNKNOWN")
        if realised is None:
            flags.append("REALISED_UNKNOWN")
        if unreal is None:
            flags.append("UNREALISED_UNKNOWN")
        m = self._margin
        out = {"opening_balance": opening, "deposits": dep, "withdrawals": wd, "realised_net": realised, "balance": balance, "unrealised": unreal, "equity": equity,
               "margin_used_usd": m["used_usd"], "available_usd": m["available_usd"], "margin_ts_ms": m["ts_ms"], "margin_known": m["used_usd"] is not None and m["available_usd"] is not None,
               "net_pnl_excludes_deposits_and_withdrawals": True, "flags": flags}
        return _to_float(out)

    # ------------------------------------------------------------ venue reconciliation
    def reconcile_with_venue(self, executions: Optional[Sequence[Mapping[str, Any]]], positions: Optional[Mapping[str, Any]], wallet: Optional[Mapping[str, Any]] = None,
                             fees: Optional[Any] = None, *, executions_complete: bool = True, tol: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
        """Compare this ledger with an INJECTED venue read (this module reads no venue). ``executions``: rows keyed by ``execId`` (also ``fill_id``) with ``execQty`` / ``qty``,
        ``execPrice`` / ``price``, ``execFee`` / ``fee``, ``side``, ``symbol``, optional ``category`` and ``orderLinkId``. ``positions``: {'SYM' | 'SPOT:SYM': qty or {"qty", "avg_price"}},
        None = the read failed. ``wallet``: {"balance_change_usd": wallet change from realised price + funding - fees, deposits / withdrawals excluded} (optional; unchecked when None).
        ``fees``: the venue's total fee USD (None = unknown -> finding). ``executions_complete=False`` (a paged / partial read) makes missing-in-venue findings impossible to conclude: it
        is itself a finding. Tolerances ``VENUE_TOL``: quantity 1e-9, price relative 1e-8, USD 1e-6 (override with ``tol``).
        Returns {"venue_reconciled", "findings": [{"code", "key", "detail"}], "checked": {...}}. ``venue_reconciled`` is True ONLY when there is no finding; it is remembered and reported by
        ``pnl()`` until the ledger changes (then ``VENUE_RECONCILIATION_STALE``). A mismatch never edits the ledger."""
        t = {k: to_decimal((tol or {}).get(k, v)) for k, v in VENUE_TOL.items()}
        F: List[Dict[str, str]] = []

        def add(code: str, key: str, detail: str = "") -> None:
            F.append({"code": code, "key": key, "detail": detail})

        mine = {f["fill_id"]: f for f in self.fills()}
        seen: Dict[str, Tuple[Any, ...]] = {}
        if executions is None or not isinstance(executions, (list, tuple)):
            add("VENUE_EXECUTIONS_UNKNOWN", "executions")
            executions = []
        elif not executions_complete:
            add("VENUE_EXECUTIONS_INCOMPLETE", "executions")
        for e in executions:
            if not isinstance(e, Mapping):
                add("VENUE_EXECUTION_MALFORMED", "?")
                continue
            eid = e.get("execId", e.get("fill_id"))
            q, px = to_decimal(e.get("execQty", e.get("qty"))), to_decimal(e.get("execPrice", e.get("price")))
            fee = to_decimal(e.get("execFee", e.get("fee", e.get("fee_usd"))))
            if not isinstance(eid, str) or not eid or q is None or px is None:
                add("VENUE_EXECUTION_MALFORMED", str(eid))
                continue
            sig = (e.get("side"), q, px, fee)
            if eid in seen:
                if seen[eid] != sig:
                    add("VENUE_EXECUTION_CONFLICT", eid)
                continue
            seen[eid] = sig
            m = mine.get(eid)
            if m is None:
                add("EXEC_MISSING_IN_LEDGER", eid)
                continue
            if abs(q - m["qty"]) > t["qty"]:
                add("EXEC_QTY_MISMATCH", eid, f"venue {q} ledger {m['qty']}")
            if abs(px - m["price"]) > t["price_rel"] * max(abs(px), abs(m["price"])):
                add("EXEC_PRICE_MISMATCH", eid, f"venue {px} ledger {m['price']}")
            if e.get("side") is not None and e.get("side") != m["side"]:
                add("EXEC_SIDE_MISMATCH", eid)
            if e.get("symbol") is not None and e.get("symbol") != m["symbol"]:
                add("EXEC_SYMBOL_MISMATCH", eid)
            if fee is None or m["fee"] is None:
                add("EXEC_FEE_UNKNOWN", eid)
            elif abs(fee - m["fee"]) > t["usd"]:
                add("EXEC_FEE_MISMATCH", eid, f"venue {fee} ledger {m['fee']}")
            link = e.get("orderLinkId", e.get("link_id"))
            if link is not None and link != f"{m['group_id']}-{m['leg_id']}":
                add("EXEC_LINK_MISMATCH", eid)
        if executions_complete:
            for fid in sorted(set(mine) - set(seen)):
                add("EXEC_MISSING_IN_VENUE", fid)
        # fees
        p = self.pnl_exact()
        vf = to_decimal(fees)
        if vf is None:
            add("VENUE_FEES_UNKNOWN", "fees")
        elif p["fees"] is None:
            add("LEDGER_FEES_UNKNOWN", "fees")
        elif abs(vf - p["fees"]) > t["usd"]:
            add("FEES_MISMATCH", "fees", f"venue {vf} ledger {p['fees']}")
        # positions
        if not isinstance(positions, Mapping):
            add("VENUE_POSITIONS_UNKNOWN", "positions")
        else:
            detail = self.positions_detail()
            for k, d in detail.items():
                if k not in positions:
                    add("VENUE_POSITION_UNKNOWN", k)
                    continue
                v = positions[k]
                vq = to_decimal(v.get("qty") if isinstance(v, Mapping) else v)
                if vq is None:
                    add("VENUE_POSITION_UNKNOWN", k)
                    continue
                if abs(vq - d["qty"]) > t["qty"]:
                    add("POSITION_QTY_MISMATCH", k, f"venue {vq} ledger {d['qty']}")
                elif isinstance(v, Mapping) and d["qty"] != 0:
                    va = to_decimal(v.get("avg_price"))
                    if va is None or abs(va - d["avg_price"]) > t["price_rel"] * max(abs(va), abs(d["avg_price"])):
                        add("POSITION_AVG_PRICE_MISMATCH", k, f"venue {va} ledger {d['avg_price']}")
            for k, v in sorted(positions.items(), key=lambda kv: str(kv[0])):
                if k not in detail:
                    vq = to_decimal(v.get("qty") if isinstance(v, Mapping) else v)
                    if vq is None or abs(vq) > t["qty"]:
                        add("VENUE_POSITION_NOT_IN_LEDGER", str(k))
        # wallet
        wallet_checked = wallet is not None
        if wallet is not None:
            wc = to_decimal(wallet.get("balance_change_usd")) if isinstance(wallet, Mapping) else None
            if wc is None:
                add("VENUE_WALLET_UNKNOWN", "wallet")
            elif p["realised"] is None:
                add("LEDGER_REALISED_UNKNOWN", "wallet")
            elif abs(wc - p["realised"]) > t["usd"]:
                add("WALLET_MISMATCH", "wallet", f"venue {wc} ledger {p['realised']}")
        for f in self.integrity_flags():
            add("LEDGER_INTEGRITY", f)
        ok = not F
        self._venue_recon = {"ok": ok, "findings": F, "version": self._ver}
        return {"venue_reconciled": ok, "findings": list(F), "checked": {"executions": len(seen), "ledger_fills": len(mine), "wallet": wallet_checked, "tolerances": {k: str(v) for k, v in t.items()}}}

    # ------------------------------------------------------------ restart: the ledger is a pure fold of its inputs
    def export_inputs(self) -> Dict[str, Any]:
        """JSON-able record of every input (strings for numbers). ``Ledger.from_inputs`` rebuilds an identical ledger in any order (restart)."""
        return {"track_acks": self.track_acks,
                "fills": [{"fill_id": f["fill_id"], "group_id": f["group_id"], "leg_id": f["leg_id"], "symbol": f["symbol"], "category": f["category"], "side": f["side"], "qty": str(f["qty"]),
                           "price": str(f["price"]), "fee": None if f["fee"] is None else str(f["fee"]), "ts_ms": f["ts_ms"], "prov": self._fills[f["fill_id"]]["fill"].prov, "pos_leg": self._fills[f["fill_id"]]["pos_leg"],
                           "ref": None if self._fills[f["fill_id"]]["ref"] is None else str(self._fills[f["fill_id"]]["ref"])} for f in self.fills()],
                "funding": [[s, ts, None if a is None else str(a), self._fund[s][ts][1]] for (s, ts), a in self.funding_events().items()],
                "no_funding": {s: list(v) for s, v in sorted(self._fund_none.items())},
                "acks": [[g, l, o] for (g, l), o in sorted(self._acks.items())]}

    @classmethod
    def from_inputs(cls, d: Mapping[str, Any]) -> "Ledger":
        L = cls(track_acks=bool(d.get("track_acks")))
        for f in d.get("fills", []):
            L.record_fill(Fill(f["fill_id"], f["group_id"], f["leg_id"], f["symbol"], f["side"], f["qty"], f["price"], f["fee"], "USDT", "TAKER", f["ts_ms"], f["prov"]),
                          category=f["category"], ref_price=f.get("ref"), position_leg=f.get("pos_leg"))
        for s, ts, a, prov in d.get("funding", []):
            L.record_funding(s, ts, a, prov)
        for s, (reason, prov) in d.get("no_funding", {}).items():
            L.declare_no_funding(s, reason, prov)
        for g, l, o in d.get("acks", []):
            L.acknowledge(g, l, o)
        return L

    def to_json(self) -> str:
        return json.dumps(self.export_inputs(), sort_keys=True)
