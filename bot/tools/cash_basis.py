#!/usr/bin/env python3
"""CASH AND CAPITAL BASIS for the research evaluators: what a strategy's return is measured AGAINST, and on what capital.

A cross-sectional funding row is a return in bps of GROSS notional. That is not a decision statistic: the book ties up capital (a perp margin; for a spot-hedged carry the spot leg paid in full as well) that
could have earned the owner's cash alternative over the same hold. The honest statistic is the EXCESS over cash ON THE CAPITAL COMMITTED:

    excess_bps_of_capital  =  net_bps_of_gross_notional / capital_per_gross_notional  -  cash_apr(t, t + hold) x hold_days / 365 x 1e4

CAPITAL per unit of gross notional is derived, never typed (``economic_model``): a spot-hedged carry is ``(1 + 1/L)(1 + buffer)`` (2.04 at leverage 1, the live plan's own definition), a perp-only book is
``(1/L)(1 + buffer)`` (1.02). The BTC research overlay's 1.16 is a different, ASSUMED construction (BTC already held) and is not used here.

CASH comes from a named source and the sources are NEVER mixed or substituted:
  HISTORICAL    the repository's hourly OKX USDT lending series, aligned in time (the mean over exactly the hold's interval; a hole in the series makes that row UNKNOWN, it is dropped and counted, never filled
                with a neighbour's rate and never zero). It is a lender quote on another venue, not the owner's opportunity cost.
  OWNER_STATED  the owner's own validated statement (``OWNER_CASH_ALTERNATIVE_*``; no default). Only a STATED, current statement yields a basis; an unstated, stale, future-dated or invalid one yields NONE and
                the report says why. It is a flat rate over the whole window, so it is only as good as the owner's statement.
Each basis is its own, separately COUNTED set of variants in the multiple-testing ledger; a result against one never stands in for the other.

THE HURDLE is reported beside the statistic: the gross return a row must earn (in bps of gross notional) before its own round-trip cost and the cash it displaces. Pure functions; no I/O except reading the one
cash file; no network; no clock.
"""
from __future__ import annotations

import hashlib
import os
import sys
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

BOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BOT not in sys.path:
    sys.path.insert(0, BOT)
TOOLS = os.path.dirname(os.path.abspath(__file__))
if TOOLS not in sys.path:
    sys.path.insert(0, TOOLS)

import economic_model as EM  # noqa: E402
import opportunity_map as OM  # noqa: E402

MS_DAY = 86_400_000
HISTORICAL = "HISTORICAL"
OWNER_STATED = "OWNER_STATED"
LABELS = (HISTORICAL, OWNER_STATED)


@dataclass(frozen=True)
class CashBasis:
    """One named cash alternative. ``apr_over(start_ms, end_ms)`` is the annual rate (a fraction) charged over exactly that interval, or None when the source does not cover it."""
    label: str
    description: str
    curve: Optional[Any] = None                 # an OM.CashCurve (HISTORICAL)
    flat_apr: Optional[float] = None            # the owner's statement (OWNER_STATED)
    meta: Optional[Mapping[str, Any]] = None

    def apr_over(self, start_ms: int, end_ms: int) -> Optional[float]:
        if self.curve is not None:
            return self.curve.mean_apr(int(start_ms), int(end_ms))
        return self.flat_apr

    def describe(self) -> Dict[str, Any]:
        return {"label": self.label, "description": self.description, **dict(self.meta or {})}


def historical_basis(repo: str) -> Optional[CashBasis]:
    """The repository's hourly cash series as a basis, or None when the file is absent or unreadable (then every cash-adjusted figure is UNKNOWN: nothing is guessed)."""
    curve = OM.load_cash_curve(repo)
    if not curve:
        return None
    path = os.path.join(repo, OM.OKX_LENDING)
    try:
        with open(path, "rb") as fh:
            sha = hashlib.sha256(fh.read()).hexdigest()
    except OSError:
        sha = None
    return CashBasis(HISTORICAL, "OKX USDT lending, hourly, column rate_annual (a lender quote on another venue; NOT the owner's opportunity cost)", curve=curve,
                     meta={"file": OM.OKX_LENDING, "sha256": sha, "first_ms": curve.first_ms(), "last_ms": curve.last_ms(), "hourly_points": len(curve.ts)})


def owner_basis(statement: Mapping[str, str], now_ms: int) -> Tuple[Optional[CashBasis], Dict[str, Any]]:
    """(basis or None, the statement's state). Only a STATED, current ``OWNER_CASH_ALTERNATIVE_*`` statement (the executor gate's own judgement, ``ms_edge_gate.describe_cash_alternative``) yields a basis; an unstated,
    invalid, stale or future-dated one yields None and the state says which. An explicit 0 is a legitimate statement ("free by decision") and is a basis."""
    import ms_edge_gate as EG
    info = EG.describe_cash_alternative(statement, int(now_ms))
    if not (info.get("usable") and info.get("state") == "STATED" and info.get("apr") is not None):
        return None, info
    apr = float(info["apr"])
    return CashBasis(OWNER_STATED, f"the owner's stated cost of capital {apr:.4%}/yr ({info.get('source')}), flat over the window", flat_apr=apr,
                     meta={"stated_ms": info.get("stated_ms"), "source": info.get("source"), "apr": info.get("apr")}), info


def capital_per_gross_notional(kind_is_carry: bool, leverage: int = 1) -> float:
    """Capital committed per 1 unit of GROSS notional, from the one definition per construction in ``economic_model`` (a carry pairs a spot leg paid in full with a perp margin; a perp-only book needs margin only)."""
    q = EM.hedged_pair_capital_usd(1, leverage) if kind_is_carry else EM.perp_only_capital_usd(1, leverage)
    return float(q.value)


def cash_bps_over(basis: CashBasis, start_ms: int, hold_days: float) -> Optional[float]:
    """The cash a basis would have paid on one unit of capital over [start, start + hold], in bps (simple interest), or None when the source does not cover it."""
    apr = basis.apr_over(int(start_ms), int(start_ms + hold_days * MS_DAY))
    return None if apr is None else float(apr) * hold_days / 365.0 * 1e4


def excess_rows(rows: Sequence[Mapping[str, Any]], *, hold_days: float, capital: float, basis: CashBasis) -> Tuple[Dict[int, Dict[str, float]], List[int]]:
    """({t: {excess, cash_bps, gross_net_bps}}, [t with no cash coverage]). ``excess`` = net_bps / capital - cash_bps (bps of CAPITAL). A row the source cannot price is returned in the second list: it is dropped
    and counted by the caller, never priced at zero cash."""
    out: Dict[int, Dict[str, float]] = {}
    unknown: List[int] = []
    for r in rows:
        t = int(r["t"])
        c = cash_bps_over(basis, t, hold_days)
        if c is None:
            unknown.append(t)
            continue
        net = float(r["net_bps"])
        out[t] = {"excess_bps_of_capital": net / capital - c, "cash_bps_of_capital": c, "net_bps_of_gross": net, "cash_hurdle_bps_of_gross": c * capital}
    return out, unknown
