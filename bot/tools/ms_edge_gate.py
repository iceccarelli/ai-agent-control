#!/usr/bin/env python3
"""Expected-NET-edge gate for the unattended Testnet autopilot. ONE model: ``economic_model.evaluate_chain`` (provenance-aware; an ASSUMED input can never PASS).

A funding rate is not a profit. This gate prices the whole hedged round trip (spot buy + perp short, entry and exit, both legs at the venue's taker fee unless a maker fill is
modelled) from the venue's own instrument rows, the two order books it can walk, the account's fee tier, the funding rate and the settlement time, and answers in dollars and
basis points:  expected gross funding  -  fees  -  spread  -  slippage  -  impact  -  basis  -  hedge error  =  expected NET edge.  The REQUIRED funding is DERIVED from that
chain (breakeven + the positive hurdle), never typed in: lowering an owner threshold cannot make an uneconomic trade pass.

Classes:  PROFIT_CANDIDATE (every input known and none assumed, net >= MIN_NET_EDGE_BPS)  |  REJECTED_UNECONOMIC (known, but net below the hurdle)  |
          INCONCLUSIVE (something unknown, stale, thin or only ASSUMED, e.g. the account fee tier could not be read): can never be a profit-seeking session.
A bounded MECHANICS session (lifecycle/fee/fill measurement on fake money) is a different, explicitly labelled thing the owner must allow in the grant; it is never reported as an
opportunity. Pure and offline: the reads are injected (``EdgeInputs``); no environment, no network, no orders.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import sys
from decimal import Decimal as D
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

BOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BOT not in sys.path:
    sys.path.insert(0, BOT)

import economic_model as EM  # noqa: E402

BPS = D(10_000)
MIN_NET_EDGE_BPS = D("2")            # the positive hurdle (bps of notional) on the expected CAPTURABLE edge; the grant cannot lower it
FUNDING_HAIRCUT = D("0.30")          # the predicted rate can fall before the print: expected funding = rate * (1 - haircut)
IMPACT_BPS = D("1")                  # beyond the walked book, modelled
BASIS_STRESS_BPS = D("3")            # basis convergence risk over the hold: max(|entry basis|, this), modelled
ADVERSE_MOVE_BPS = D("50")           # move a residual hedge error is exposed to, modelled
MAX_BOOK_AGE_MS = 5_000
MAX_FEE_AGE_MS = 3_600_000
# Bybit's published VIP 0 base TAKER schedule (spot 0.1000%, perpetual 0.0550%). Used ONLY as an ASSUMED fee tier when the account's own rate could not be read: an assumed fee can
# never PASS (the owner's actual "My Fee Rate" decides). Re-check https://www.bybit.com/en/help-center/article/Trading-Fee-Structure; never treat these as the owner's rate.
PUBLISHED_VIP0_TAKER = {"spot": D("0.001"), "linear": D("0.00055")}
PUBLISHED_VIP0_MAKER = {"spot": D("0.001"), "linear": D("0.0002")}

PROFIT_CANDIDATE, REJECTED, INCONCLUSIVE = "PROFIT_CANDIDATE", "REJECTED_UNECONOMIC", "INCONCLUSIVE"


@dataclasses.dataclass(frozen=True)
class EdgeInputs:
    symbol: str
    notional_usd: D
    now_ms: int
    funding_rate: D                              # fraction per settlement, the venue's predicted/current rate (positive: longs pay, shorts receive)
    next_funding_ms: int
    hold_s: int
    perp_row: Mapping[str, Any]                  # instruments-info row, category linear
    spot_row: Mapping[str, Any]                  # instruments-info row, category spot
    perp_book: EM.Book
    spot_book: EM.Book
    leverage: int = 1
    perp_fee: Optional[Tuple[D, D]] = None       # (maker, taker) fractions READ from the account, else None -> ASSUMED published VIP 0
    spot_fee: Optional[Tuple[D, D]] = None
    fee_observed_ms: Optional[int] = None

    def digest(self) -> str:
        """A digest of EVERYTHING the verdict was computed from: the instrument rows, both books (every level), the funding observation, the horizon, the size and the fee tiers (with whether
        they were MEASURED). The verdict's own digest covers its outputs; this one covers its inputs, so a decision can be reproduced and a different market state can never share an id."""
        def book(b: Any) -> Dict[str, Any]:
            return {"ts_ms": int(b.ts_ms), "bids": [[str(p), str(q)] for p, q in b.bids], "asks": [[str(p), str(q)] for p, q in b.asks]}
        body = {"symbol": self.symbol, "notional": str(self.notional_usd), "now_ms": int(self.now_ms), "funding_rate": str(self.funding_rate), "next_funding_ms": int(self.next_funding_ms),
                "hold_s": int(self.hold_s), "leverage": int(self.leverage), "perp_row": self.perp_row, "spot_row": self.spot_row, "perp_book": book(self.perp_book), "spot_book": book(self.spot_book),
                "perp_fee": None if self.perp_fee is None else [str(x) for x in self.perp_fee], "spot_fee": None if self.spot_fee is None else [str(x) for x in self.spot_fee],
                "fee_observed_ms": self.fee_observed_ms}
        return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")).hexdigest()


@dataclasses.dataclass(frozen=True)
class EdgeVerdict:
    session_class: str
    reasons: Tuple[str, ...]
    net_edge_bps: Optional[str]                  # CAPTURABLE stage, decimal string (None = unknown)
    expected_funding_bps: Optional[str]
    required_funding_bps: Optional[str]          # the rate (bps per settlement) at which net == the hurdle; None = unknown
    expected_net_usd: Optional[str]
    costs_bps: Mapping[str, Optional[str]]
    assumed_inputs: Tuple[str, ...]
    settlements_in_hold: int
    digest: str

    def as_dict(self) -> Dict[str, Any]:
        return dataclasses.asdict(self)


def _spot_instrument_row(row: Mapping[str, Any]) -> Dict[str, Any]:
    """Real spot rows carry basePrecision/minOrderAmt, not qtyStep/minOrderQty: map them EXPLICITLY (never guess a missing field)."""
    lot = dict(row.get("lotSizeFilter") or {})
    if "qtyStep" not in lot:
        lot["qtyStep"] = lot["basePrecision"]
    if "minOrderQty" not in lot:
        lot["minOrderQty"] = lot["basePrecision"]
    return {**row, "category": "spot", "lotSizeFilter": lot}


def _q(x: Optional[D]) -> Optional[str]:
    return None if x is None else format(x.normalize(), "f")


def _settlements_in_hold(now_ms: int, next_ms: int, hold_s: int) -> int:
    end = now_ms + int(hold_s) * 1000
    return 1 if now_ms < next_ms <= end else 0


def evaluate_edge(inp: EdgeInputs) -> EdgeVerdict:
    reasons: List[str] = []
    try:
        perp = EM.Instrument.from_venue_row({**inp.perp_row, "category": "linear"}, expect_symbol=inp.symbol, expect_category="linear", source_ms=inp.now_ms)
        spot = EM.Instrument.from_venue_row(_spot_instrument_row(inp.spot_row), expect_symbol=inp.symbol, expect_category="spot", source_ms=inp.now_ms)
    except (EM.EconomicModelError, KeyError, TypeError, ValueError) as exc:
        return _verdict(INCONCLUSIVE, [f"INSTRUMENT_INVALID:{str(exc)[:120]}"], None, None, None, {}, (), 0, inp)
    n_settle = _settlements_in_hold(inp.now_ms, int(inp.next_funding_ms), inp.hold_s)
    if n_settle == 0:
        return _verdict(REJECTED, ["NO_SETTLEMENT_INSIDE_THE_HOLD"], None, None, None, {}, (), 0, inp)
    if inp.hold_s <= 0 or inp.notional_usd <= 0 or inp.leverage < 1:
        return _verdict(INCONCLUSIVE, ["INPUT_NOT_POSITIVE"], None, None, None, {}, (), n_settle, inp)

    def tier(read: Optional[Tuple[D, D]], category: str) -> EM.FeeTier:
        if read is not None and inp.fee_observed_ms is not None:
            return EM.FeeTier(maker=read[0], taker=read[1], source="measured", observed_ms=inp.fee_observed_ms)
        return EM.FeeTier(maker=PUBLISHED_VIP0_MAKER[category], taker=PUBLISHED_VIP0_TAKER[category], source="assumed")

    spot_tier, perp_tier = tier(inp.spot_fee, "spot"), tier(inp.perp_fee, "linear")
    rate_bps = inp.funding_rate * BPS
    expected_funding = rate_bps * (D(1) - FUNDING_HAIRCUT) * D(n_settle)        # negative rate => a short PAYS: a negative expectation, never rounded up
    basis_bps = abs(EM.basis_bps(inp.perp_book.mid, inp.spot_book.mid)) if not (inp.perp_book.problems() or inp.spot_book.problems()) else None
    hedge_ratio = D(1) - spot_tier.taker                                         # a spot BUY's fee is taken in the base coin: the hedge is that much under
    scn = EM.Scenario(
        legs=(EM.Leg("spot", spot, "buy", inp.spot_book, spot_tier, "taker", fee_in_base=True), EM.Leg("perp", perp, "sell", inp.perp_book, perp_tier, "taker")),
        notional_usd=inp.notional_usd, gross_edge_bps=EM.Quantity(expected_funding, "modelled", "bps"), hold_days=D(inp.hold_s) / D(86_400), now_ms=inp.now_ms,
        max_book_age_ms=MAX_BOOK_AGE_MS, max_fee_age_ms=MAX_FEE_AGE_MS, impact_bps=EM.Quantity(IMPACT_BPS, "modelled", "bps"),
        basis_cost_bps=(EM.Quantity(max(basis_bps, BASIS_STRESS_BPS), "modelled", "bps") if basis_bps is not None else EM.unknown("bps")),
        borrow_apr=EM.Quantity(D(0), "derived", "fraction"),                   # spot is bought with USDT, not borrowed
        opportunity_cost_apr=EM.Quantity(D(0), "modelled", "fraction"),         # the owner's opportunity cost is not set (owner_policy: reported as free capital, never invented)
        hedge_ratio=EM.Quantity(hedge_ratio, "derived", "ratio"), adverse_move_bps=EM.Quantity(ADVERSE_MOVE_BPS, "modelled", "bps"),
        margin_usd=EM.Quantity(inp.notional_usd * (D(1) + D(1) / D(inp.leverage)), "derived", "usd"))
    try:
        res = EM.evaluate_chain(scn)
    except EM.EconomicModelError as exc:
        return _verdict(INCONCLUSIVE, [f"MODEL_REFUSED:{str(exc)[:120]}"], None, None, None, {}, (), n_settle, inp)
    comps = {k: (None if not q.known else q.value) for k, q in res.components.items()}
    net = res.net_edge_bps
    assumed = tuple(res.assumed_inputs)
    reasons += list(res.blockers)
    if res.unknown_inputs:
        reasons.append("UNKNOWN_INPUTS:" + ",".join(res.unknown_inputs))
    # the funding (bps per settlement) at which the expected net equals the hurdle: all known costs + the hurdle, spread over the settlements and the haircut
    known_costs = [v for v in comps.values() if v is not None]
    required = None
    if not res.unknown_inputs and not res.blockers:
        required = (sum(known_costs, D(0)) + MIN_NET_EDGE_BPS) / ((D(1) - FUNDING_HAIRCUT) * D(n_settle))
    if not net.known or res.blockers:
        cls = INCONCLUSIVE
    elif assumed:
        cls = INCONCLUSIVE
        reasons.append("ASSUMED_INPUTS_CANNOT_PASS:" + ",".join(assumed))
    elif net.value >= MIN_NET_EDGE_BPS:
        cls = PROFIT_CANDIDATE
    else:
        cls = REJECTED
        reasons.append(f"NET_EDGE_{net.value:.2f}bps_BELOW_HURDLE_{MIN_NET_EDGE_BPS}bps")
    net_usd = (net.value / BPS * inp.notional_usd) if net.known else None
    return _verdict(cls, reasons, net.value if net.known else None, expected_funding, required, comps, assumed, n_settle, inp, net_usd)


def _verdict(cls: str, reasons: Sequence[str], net: Optional[D], funding: Optional[D], required: Optional[D], comps: Mapping[str, Optional[D]], assumed: Sequence[str], n: int,
             inp: EdgeInputs, net_usd: Optional[D] = None) -> EdgeVerdict:
    body = {"class": cls, "reasons": list(reasons), "net": _q(net), "funding": _q(funding), "required": _q(required), "comps": {k: _q(v) for k, v in sorted(comps.items())},
            "assumed": list(assumed), "n": n, "symbol": inp.symbol, "notional": _q(inp.notional_usd), "now_ms": inp.now_ms, "hold_s": inp.hold_s}
    dg = hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    return EdgeVerdict(cls, tuple(reasons), _q(net), _q(funding), _q(required), _q(net_usd), {k: _q(v) for k, v in sorted(comps.items())}, tuple(assumed), n, dg)


# ---------------------------------------------------------------------------------------------------- reading the venue (public GETs + one optional signed GET)
def book_from_venue(result: Mapping[str, Any]) -> EM.Book:
    """Bybit /v5/market/orderbook result -> Book (strict: Decimal levels, venue timestamp)."""
    bids = tuple((EM.to_decimal(p), EM.to_decimal(q)) for p, q in result["b"])
    asks = tuple((EM.to_decimal(p), EM.to_decimal(q)) for p, q in result["a"])
    return EM.Book(bids=bids, asks=asks, ts_ms=int(result["ts"]))


def fee_from_venue(rows: Sequence[Mapping[str, Any]], symbol: str) -> Optional[Tuple[D, D]]:
    """/v5/account/fee-rate list -> (maker, taker) fractions for the symbol, or None when absent/malformed (the caller then uses the ASSUMED published tier, which cannot PASS)."""
    for r in rows:
        if r.get("symbol") in (symbol, None, ""):
            try:
                return EM.fee_fraction(r["makerFeeRate"], "fraction"), EM.fee_fraction(r["takerFeeRate"], "fraction")
            except (KeyError, EM.EconomicModelError):
                return None
    return None


def collect(ex: Any, symbol: str, notional_usd: D, hold_s: int, funding_rate: D, next_funding_ms: int, now_ms: int, leverage: int = 1) -> EdgeInputs:
    """The venue reads behind an EdgeInputs, through a TestnetExecutor-like ``ex`` (``_get`` only: public instruments/books plus ONE signed fee-rate read that may be refused by the
    key's permissions: then the fee tier is ASSUMED and the verdict cannot be a PROFIT_CANDIDATE)."""
    def rows(cat: str) -> Sequence[Mapping[str, Any]]:
        return ex._get("/v5/market/instruments-info", {"category": cat, "symbol": symbol}, signed=False)["list"]
    perp_row = next(r for r in rows("linear") if r["symbol"] == symbol)
    spot_row = next(r for r in rows("spot") if r["symbol"] == symbol)
    perp_book = book_from_venue(ex._get("/v5/market/orderbook", {"category": "linear", "symbol": symbol, "limit": 50}, signed=False))
    spot_book = book_from_venue(ex._get("/v5/market/orderbook", {"category": "spot", "symbol": symbol, "limit": 50}, signed=False))
    perp_fee = spot_fee = None
    observed = None
    try:
        pf = ex._get("/v5/account/fee-rate", {"category": "linear", "symbol": symbol}, signed=True)["list"]
        sf = ex._get("/v5/account/fee-rate", {"category": "spot", "symbol": symbol}, signed=True)["list"]
        perp_fee, spot_fee = fee_from_venue(pf, symbol), fee_from_venue(sf, symbol)
        observed = now_ms if (perp_fee and spot_fee) else None
    except Exception:                                                           # noqa: BLE001 - any refusal/permission/transport problem: the fee tier stays ASSUMED
        perp_fee = spot_fee = None
    return EdgeInputs(symbol, notional_usd, now_ms, funding_rate, next_funding_ms, hold_s, perp_row, spot_row, perp_book, spot_book, leverage, perp_fee, spot_fee, observed)
