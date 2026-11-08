"""Accounting of the multi-symbol layer: fills, funding, fees, marks -> per-leg / per-symbol / portfolio P&L with provenance. Pure: no clock, no I/O, no venue.

Principles (each is tested, each has a mutation):
  * UNKNOWN is not zero. A fee that was not stated, a zero fee on a real fill that nobody measured, a rebate, a mark that does not exist for an open leg and a perp symbol
    with no funding record (and no explicit "no funding applied" declaration) make that component ``None``, and ``net`` becomes ``None``. A flag says why.
  * Unreconciled is not profit. ``net`` is a mark-to-market figure; ``claimable_profit`` is non-None ONLY when every leg is flat, every fee and every funding component is
    known, and nothing is flagged. A ledger with open legs or unknown components never reports a clean profit.
  * Duplicate fills (same ``fill_id``) are ignored; a duplicate whose content differs is flagged ``DUPLICATE_FILL_CONFLICT`` and the ledger is unreconciled.
  * FUNDING SIGN CONVENTION (explicit): ``amount_usd`` > 0 is cash RECEIVED by this account, < 0 is cash PAID. For a funding rate ``r`` (fraction per print, positive = longs
    pay shorts) the cash flow of a position with signed notional ``N`` (long > 0, short < 0) is ``-N * r`` (``funding_cashflow``). A short in positive funding receives.
  * Fees are costs: ``fees`` is >= 0 and is SUBTRACTED. A negative fee (rebate) is never assumed: it flags ``FEE_REBATE_UNVERIFIED`` and the component is unknown.
  * ``net = price_leg + funding - fees`` with ``price_leg = realised_price + unrealised_price`` (average-cost). ``slippage`` is an ATTRIBUTION of the price leg versus the
    reference price the caller gave at fill time (already inside ``price_leg``; never deducted a second time); None when a reference was not given.
  * Interval conversion lives in ``carry_costs`` (``funding_bps_per_day``); this module only books cash.
"""
from __future__ import annotations

import math
import os
import sys
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ms_types import Fill, weakest  # noqa: E402

_FILL_PROV_MAP = {"SIMULATED": "MODELLED", "MODELLED": "MODELLED", "MEASURED": "MEASURED"}
_PROV_OK = ("MEASURED", "VENUE_STATED", "DERIVED", "MODELLED", "ASSUMED", "UNKNOWN")
EPS = 1e-12


class LedgerError(ValueError):
    """A record that cannot be booked at all (not a unit mistake we could guess about)."""


def funding_cashflow(signed_notional_usd: float, rate_frac_per_print: float) -> float:
    """Cash flow of one funding print for a position of signed notional N (long > 0): ``-N * r``. Positive rate: longs pay, shorts receive."""
    if not all(isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) for x in (signed_notional_usd, rate_frac_per_print)):
        raise LedgerError("funding_cashflow needs finite numbers")
    return -float(signed_notional_usd) * float(rate_frac_per_print)


def _fin(x: Any) -> Optional[float]:
    return float(x) if isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) else None


@dataclass
class _Leg:
    group_id: str
    leg_id: str
    symbol: str
    category: str
    qty: float = 0.0                 # signed base units: long > 0
    avg: float = 0.0                 # average entry price of the open quantity
    realised_price: float = 0.0
    fees: Optional[float] = 0.0      # None = at least one fee unknown
    fee_flags: List[str] = field(default_factory=list)
    slippage: Optional[float] = 0.0  # cost vs reference (>0 = worse); None = a reference was missing
    provs: List[str] = field(default_factory=list)
    n_fills: int = 0


class Ledger:
    def __init__(self) -> None:
        self._fills: Dict[str, Tuple[Any, ...]] = {}
        self._legs: Dict[Tuple[str, str], _Leg] = {}
        self._marks: Dict[Tuple[str, str], Tuple[int, float, str]] = {}
        self._funding: Dict[str, Dict[int, Tuple[Optional[float], str]]] = {}
        self._funding_none: Dict[str, Tuple[str, str]] = {}          # symbol -> (reason, prov): "no funding was applied", declared
        self._flags: List[str] = []
        self.duplicates_ignored = 0

    # ------------------------------------------------------------ booking
    def record_fill(self, fill: Fill, *, category: str = "linear", ref_price: Optional[float] = None) -> bool:
        """Book a fill. False if its ``fill_id`` was already booked (ignored). ``category`` 'linear' | 'spot' (spot has no funding). ``ref_price`` is the price the plan
        expected (for the slippage attribution)."""
        if category not in ("linear", "spot"):
            raise LedgerError(f"category {category!r}")
        if not (isinstance(fill.fill_id, str) and fill.fill_id.strip()):
            raise LedgerError("fill_id missing")
        if fill.side not in ("Buy", "Sell"):
            raise LedgerError(f"side {fill.side!r}")
        qty, px = _fin(fill.qty), _fin(fill.price)
        if qty is None or px is None or qty <= 0 or px <= 0:
            raise LedgerError("fill qty/price must be finite and positive")
        if type(fill.ts_ms) is not int or fill.ts_ms <= 0:
            raise LedgerError("fill ts_ms must be a positive int")
        sig = (fill.group_id, fill.leg_id, fill.symbol, fill.side, qty, px, category)
        if fill.fill_id in self._fills:
            self.duplicates_ignored += 1
            if self._fills[fill.fill_id] != sig:
                self._flags.append(f"DUPLICATE_FILL_CONFLICT:{fill.fill_id}")
            return False
        self._fills[fill.fill_id] = sig
        leg = self._legs.setdefault((fill.group_id, fill.leg_id), _Leg(fill.group_id, fill.leg_id, fill.symbol, category))
        if (leg.symbol, leg.category) != (fill.symbol, category):
            self._flags.append(f"LEG_IDENTITY_CONFLICT:{fill.group_id}:{fill.leg_id}")
        leg.n_fills += 1
        leg.provs.append(_FILL_PROV_MAP.get(fill.prov, "UNKNOWN"))
        # fee: never defaulted
        fee = _fin(fill.fee_usd)
        if fee is None:
            leg.fees = None
            leg.fee_flags.append(f"FEE_UNKNOWN:{fill.fill_id}")
        elif fee < 0:
            leg.fees = None
            leg.fee_flags.append(f"FEE_REBATE_UNVERIFIED:{fill.fill_id}")           # rebates are never assumed
        elif fee == 0 and fill.prov != "MEASURED":
            leg.fees = None
            leg.fee_flags.append(f"ZERO_FEE_UNVERIFIED:{fill.fill_id}")            # a free taker/maker fill nobody measured is not a free fill
        elif leg.fees is not None:
            leg.fees += fee
        # slippage attribution vs the reference price (>0 = worse than expected)
        ref = _fin(ref_price)
        if ref is None or ref <= 0:
            leg.slippage = None
        elif leg.slippage is not None:
            leg.slippage += (px - ref) * qty * (1.0 if fill.side == "Buy" else -1.0)
        # average-cost position
        signed = qty if fill.side == "Buy" else -qty
        if leg.qty == 0 or (leg.qty > 0) == (signed > 0):
            new_qty = leg.qty + signed
            leg.avg = (leg.avg * abs(leg.qty) + px * qty) / abs(new_qty)
            leg.qty = new_qty
        else:
            closing = min(abs(leg.qty), qty)
            leg.realised_price += (px - leg.avg) * closing * (1.0 if leg.qty > 0 else -1.0)
            rest = qty - closing
            leg.qty = leg.qty - closing if leg.qty > 0 else leg.qty + closing
            if rest > EPS:                                                # flipped through zero
                leg.qty = rest if signed > 0 else -rest
                leg.avg = px
            elif abs(leg.qty) <= EPS:
                leg.qty, leg.avg = 0.0, 0.0
        return True

    def record_funding(self, symbol: str, ts_ms: int, amount_usd: Optional[float], prov: str) -> bool:
        """Book one funding cash flow (> 0 received, < 0 paid). ``amount_usd`` None = the print happened but the amount is UNKNOWN (the symbol's funding becomes unknown).
        A second record for the same (symbol, ts_ms) is ignored; a different amount flags ``FUNDING_CONFLICT``."""
        if type(ts_ms) is not int or ts_ms <= 0 or not isinstance(symbol, str) or not symbol:
            raise LedgerError("funding needs a symbol and a positive int ts_ms")
        if prov not in _PROV_OK:
            raise LedgerError(f"provenance {prov!r}")
        amt = _fin(amount_usd)
        book = self._funding.setdefault(symbol, {})
        if ts_ms in book:
            if book[ts_ms][0] != amt:
                self._flags.append(f"FUNDING_CONFLICT:{symbol}:{ts_ms}")
            return False
        book[ts_ms] = (amt, prov)
        return True

    def declare_no_funding(self, symbol: str, reason: str, prov: str = "DERIVED") -> None:
        """The ONLY way a perp symbol's funding is zero: an explicit statement (e.g. 'held between two prints') with a reason. Absence of a record is UNKNOWN, not zero."""
        if not (isinstance(reason, str) and reason.strip()) or prov not in _PROV_OK:
            raise LedgerError("declare_no_funding needs a reason and a provenance")
        self._funding_none[symbol] = (reason, prov)

    def mark(self, symbol: str, ts_ms: int, price: float, *, category: str = "linear", prov: str = "MEASURED") -> bool:
        """Latest mark per (symbol, category). A mark older than the one held is ignored. A non-finite / non-positive price is refused."""
        p = _fin(price)
        if p is None or p <= 0 or type(ts_ms) is not int or ts_ms <= 0 or category not in ("linear", "spot") or prov not in _PROV_OK:
            raise LedgerError("mark needs a positive finite price, a positive int ts_ms, a category and a provenance")
        cur = self._marks.get((symbol, category))
        if cur is not None and ts_ms < cur[0]:
            return False
        self._marks[(symbol, category)] = (ts_ms, p, prov)
        return True

    # ------------------------------------------------------------ reporting
    def _leg_report(self, leg: _Leg) -> Dict[str, Any]:
        flags = list(leg.fee_flags)
        prov = weakest(*leg.provs) if leg.provs else "UNKNOWN"
        open_ = abs(leg.qty) > EPS
        unreal: Optional[float] = 0.0
        if open_:
            m = self._marks.get((leg.symbol, leg.category))
            if m is None:
                unreal = None
                flags.append(f"NO_MARK_FOR_OPEN_LEG:{leg.symbol}:{leg.category}")
            else:
                unreal = (m[1] - leg.avg) * leg.qty
                prov = weakest(prov, m[2], "MODELLED")                         # a mark-to-market is at best modelled
            flags.append(f"OPEN_LEG:{leg.group_id}:{leg.leg_id}")
        return {"group_id": leg.group_id, "leg_id": leg.leg_id, "symbol": leg.symbol, "category": leg.category, "open": open_, "qty": leg.qty,
                "realised_price": leg.realised_price, "unrealised_price": unreal, "fees": leg.fees, "slippage": leg.slippage, "prov": prov, "flags": flags}

    def _funding_for(self, symbol: str) -> Tuple[Optional[float], str, List[str]]:
        recs = self._funding.get(symbol, {})
        if recs:
            amts = [a for a, _p in recs.values()]
            prov = weakest(*(p for _a, p in recs.values()))
            if any(a is None for a in amts):
                return None, prov, [f"FUNDING_AMOUNT_UNKNOWN:{symbol}"]
            return float(sum(amts)), prov, []
        if symbol in self._funding_none:
            return 0.0, self._funding_none[symbol][1], []
        return None, "UNKNOWN", [f"FUNDING_NOT_RECORDED:{symbol}"]

    @staticmethod
    def _sum(vals: List[Optional[float]]) -> Optional[float]:
        return None if any(v is None for v in vals) else float(sum(vals))

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
        return {"price_leg": price_leg, "funding": funding, "fees": fees, "slippage": slip, "realised": realised, "unrealised": unreal, "unrealised_price": unreal, "realised_price": realised_price, "qty": float(sum(l["qty"] for l in legs)), "net": net,
                "claimable_profit": net if reconciled else None, "status": "RECONCILED" if reconciled else "UNRECONCILED",
                "prov": weakest(*provs, *fprovs) if (provs or fprovs) else "UNKNOWN", "flags": flags, "n_legs": len(legs)}

    def pnl(self) -> Dict[str, Any]:
        """Portfolio P&L with ``per_leg`` and ``per_symbol`` rollups (same keys). See the module docstring for the definitions and for when ``claimable_profit`` is None."""
        legs = [self._leg_report(l) for l in self._legs.values()]
        syms = sorted({l["symbol"] for l in legs})
        out = self._rollup(legs, syms)
        out["flags"] = list(dict.fromkeys(out["flags"] + self._flags))
        if self._flags:
            out["claimable_profit"], out["status"] = None, "UNRECONCILED"
        out["per_symbol"] = {s: self._rollup([l for l in legs if l["symbol"] == s], [s]) for s in syms}
        out["per_leg"] = {f"{l['group_id']}/{l['leg_id']}": self._rollup([l], [l["symbol"]], with_funding=False) for l in legs}
        out["duplicates_ignored"] = self.duplicates_ignored
        return out
