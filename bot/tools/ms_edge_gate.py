#!/usr/bin/env python3
"""Expected-NET-edge gate for the unattended Testnet autopilot. ONE model: ``economic_model.evaluate_chain`` (provenance-aware; an ASSUMED input can never PASS).

A funding rate is not a profit. This gate prices the whole hedged round trip (spot buy + perp short, entry and exit, both legs at the venue's taker fee unless a maker fill is
modelled) from the venue's own instrument rows, the two order books it can walk, the account's fee tier, the funding rate and the settlement time, and answers in dollars and
basis points:  expected gross funding  -  fees  -  spread  -  slippage  -  impact  -  basis  -  hedge error  =  expected NET edge.  The REQUIRED funding is DERIVED from that
chain (breakeven + the positive hurdle), never typed in: lowering an owner threshold cannot make an uneconomic trade pass.

Classes:  PROFIT_CANDIDATE (every input known and none assumed, net >= MIN_NET_EDGE_BPS)  |  REJECTED_UNECONOMIC (known, but net below the hurdle)  |
          INCONCLUSIVE (something unknown, stale, thin or only ASSUMED, e.g. the account fee tier could not be read): can never be a profit-seeking session.
THE COST OF CAPITAL is an input like any other. The owner's alternative return on capital is NOT a default: it must be STATED (``CashAlternative``: a rate, who stated it, when) and it is charged on the
capital actually COMMITTED (spot notional + perp margin, not the notional). An unstated, stale, future-dated or malformed statement is UNKNOWN, never zero, so the verdict is INCONCLUSIVE: no profit
candidate can be declared on capital priced as free. (An edge that already misses the hurdle with the cost of capital floored at zero is still REJECTED: no capital cost could rescue it.)
A bounded MECHANICS session (lifecycle/fee/fill measurement on fake money) is a different, explicitly labelled thing the owner must allow in the grant; it is never reported as an
opportunity. Pure and offline: the reads are injected (``EdgeInputs``); no environment, no network, no orders.
"""
from __future__ import annotations

import calendar
import dataclasses
import hashlib
import json
import os
import re
import sys
import time
from decimal import Decimal as D
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

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

# ---------------------------------------------------------------------------------------------------- the owner's cost of capital: STATED, never defaulted
MAX_CASH_ALTERNATIVE_APR = D("1")                        # 100%/yr: above this it is a unit slip (4 instead of 0.04), refused
MAX_CASH_ALTERNATIVE_AGE_MS = 30 * 86_400_000            # a statement older than this is STALE: the owner re-states it (a cash rate moves slowly, but it is not a constant)
CASH_ALTERNATIVE_FUTURE_TOLERANCE_MS = 86_400_000        # a date-only statement means 00:00 UTC of that day; a stamp more than a day ahead of the clock is a mistake, not a statement
CASH_PROVENANCES = ("measured", "venue_stated", "derived", "modelled")      # never 'assumed' or 'unknown': an assumption is not a statement
ENV_CASH_APR, ENV_CASH_SOURCE, ENV_CASH_STATED_UTC = "OWNER_CASH_ALTERNATIVE_APR", "OWNER_CASH_ALTERNATIVE_SOURCE", "OWNER_CASH_ALTERNATIVE_STATED_UTC"
_RATE_RE = re.compile(r"(?:0|[1-9][0-9]*)(?:\.[0-9]{1,12})?", re.ASCII)
_UTC_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}(?:T[0-9]{2}:[0-9]{2}:[0-9]{2}Z)?", re.ASCII)


@dataclasses.dataclass(frozen=True)
class CashAlternative:
    """What the owner's capital would earn elsewhere, as a STATEMENT: an annual fraction (0.04 = 4%/yr), the source that states it and when it was stated. It is the input of the cost of capital; the
    gate never invents one. ``provenance`` says how strongly the number is known: an owner's own figure is 'modelled' (a stated parameter, not an observation); a venue's published lending quote may
    be 'venue_stated'. An explicit 0 is a legitimate statement ('priced as free BY DECISION') and is reported as such; an ABSENT statement is not zero."""
    apr: D
    source: str
    stated_ms: int
    provenance: str = "modelled"

    def __post_init__(self) -> None:
        if isinstance(self.apr, bool) or not isinstance(self.apr, D) or not self.apr.is_finite() or not (D(0) <= self.apr <= MAX_CASH_ALTERNATIVE_APR):
            raise ValueError(f"apr must be a finite Decimal in [0, {MAX_CASH_ALTERNATIVE_APR}] (an annual fraction), got {self.apr!r}")
        if not (isinstance(self.source, str) and self.source.strip() and len(self.source) <= 120 and self.source.isprintable()):
            raise ValueError("source must be 1-120 printable characters saying who or what states the rate")
        if isinstance(self.stated_ms, bool) or not isinstance(self.stated_ms, int) or self.stated_ms <= 0:
            raise ValueError("stated_ms must be a positive integer (epoch milliseconds)")
        if self.provenance not in CASH_PROVENANCES:
            raise ValueError(f"provenance must be one of {CASH_PROVENANCES}, got {self.provenance!r}")


def cash_alternative_from_env(env: Mapping[str, str]) -> Tuple[Optional[CashAlternative], str, List[str]]:
    """(statement, state, reasons) from the three environment settings the OWNER supplies: rate (``0.04``), source (free text) and the UTC date or timestamp it was stated. ALL ABSENT = UNSTATED;
    some absent or any malformed = INVALID; neither is ever turned into a default. Freshness is judged by the gate against the verdict's own clock, not here."""
    names = (ENV_CASH_APR, ENV_CASH_SOURCE, ENV_CASH_STATED_UTC)
    present = [n for n in names if n in env]
    if not present:
        return None, "UNSTATED", ["NO_CASH_ALTERNATIVE_STATED"]
    missing = [n for n in names if n not in env]
    if missing:
        return None, "INVALID", ["CASH_ALTERNATIVE_INCOMPLETE:missing=" + ",".join(missing)]
    rate, src, when = env[ENV_CASH_APR], env[ENV_CASH_SOURCE], env[ENV_CASH_STATED_UTC]
    if not (isinstance(rate, str) and _RATE_RE.fullmatch(rate)):
        return None, "INVALID", ["CASH_ALTERNATIVE_RATE_INVALID: a plain decimal fraction per year such as 0.04"]
    if not (isinstance(when, str) and _UTC_RE.fullmatch(when)):
        return None, "INVALID", ["CASH_ALTERNATIVE_DATE_INVALID: YYYY-MM-DD or YYYY-MM-DDTHH:MM:SSZ (UTC)"]
    try:
        stated_ms = calendar.timegm(time.strptime(when, "%Y-%m-%dT%H:%M:%SZ" if "T" in when else "%Y-%m-%d")) * 1000
        return CashAlternative(apr=D(rate), source=src.strip() if isinstance(src, str) else src, stated_ms=stated_ms, provenance="modelled"), "STATED", []
    except (ValueError, OverflowError) as exc:
        return None, "INVALID", [f"CASH_ALTERNATIVE_INVALID:{str(exc)[:100]}"]


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
    cash_alternative: Optional[CashAlternative] = None   # the owner's STATED alternative return on capital; None = UNSTATED, so the cost of capital is UNKNOWN (never zero)

    def digest(self) -> str:
        """A digest of EVERYTHING the verdict was computed from: the instrument rows, both books (every level), the funding observation, the horizon, the size and the fee tiers (with whether
        they were MEASURED). The verdict's own digest covers its outputs; this one covers its inputs, so a decision can be reproduced and a different market state can never share an id."""
        def book(b: Any) -> Dict[str, Any]:
            return {"ts_ms": int(b.ts_ms), "bids": [[str(p), str(q)] for p, q in b.bids], "asks": [[str(p), str(q)] for p, q in b.asks]}
        body = {"symbol": self.symbol, "notional": str(self.notional_usd), "now_ms": int(self.now_ms), "funding_rate": str(self.funding_rate), "next_funding_ms": int(self.next_funding_ms),
                "hold_s": int(self.hold_s), "leverage": int(self.leverage), "perp_row": self.perp_row, "spot_row": self.spot_row, "perp_book": book(self.perp_book), "spot_book": book(self.spot_book),
                "perp_fee": None if self.perp_fee is None else [str(x) for x in self.perp_fee], "spot_fee": None if self.spot_fee is None else [str(x) for x in self.spot_fee],
                "fee_observed_ms": self.fee_observed_ms,
                "cash_alternative": None if self.cash_alternative is None else (dataclasses.asdict(self.cash_alternative) if isinstance(self.cash_alternative, CashAlternative) else {"invalid": type(self.cash_alternative).__name__})}
        return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")).hexdigest()


def _touch_summary(prefix: str, b: Any, now_ms: int) -> Dict[str, Any]:
    """Top of book, spread and the dollar depth within 10 bps of the mid, as decimal strings (None for anything the book cannot give). Pure arithmetic on the book the verdict used."""
    out: Dict[str, Any] = {f"{prefix}_bid": None, f"{prefix}_ask": None, f"{prefix}_spread_bps": None, f"{prefix}_depth_usd_within_10bps": None, f"{prefix}_book_age_ms": int(now_ms - int(b.ts_ms))}   # SIGNED: negative = the book is stamped AFTER our clock reading (venue clock ahead / a reading taken before the reads); never clamped to look fresh
    try:
        bid, ask = D(str(b.bids[0][0])), D(str(b.asks[0][0]))
        mid = (bid + ask) / 2
        band = mid * D("0.001")
        out.update({f"{prefix}_bid": str(bid), f"{prefix}_ask": str(ask), f"{prefix}_bid_qty": str(b.bids[0][1]), f"{prefix}_ask_qty": str(b.asks[0][1]),
                    f"{prefix}_spread_bps": str(((ask - bid) / mid * D(10000)).quantize(D("0.0001")))})
        depth = sum((D(str(px)) * D(str(q)) for px, q in b.bids if mid - D(str(px)) <= band), D(0)) + sum((D(str(px)) * D(str(q)) for px, q in b.asks if D(str(px)) - mid <= band), D(0))
        out[f"{prefix}_depth_usd_within_10bps"] = str(depth.quantize(D("0.01")))
    except (IndexError, ArithmeticError, ValueError, TypeError):
        pass                                                         # an empty or unreadable side stays None: never guessed
    return out


def _inputs_summary(self: "EdgeInputs") -> Dict[str, Any]:
    """The market observation behind a verdict, readable without the full digest: both books' touch, spread, depth near the mid and age, the funding observation and the fee tiers' origin."""
    out: Dict[str, Any] = {"observed_ms": int(self.now_ms), "funding_rate_bps": str((self.funding_rate * D(10000)).quantize(D("0.0001"))), "next_funding_ms": int(self.next_funding_ms),
                           "hold_s": int(self.hold_s), "leverage": int(self.leverage), "perp_fee_origin": "ACCOUNT_READ" if self.perp_fee is not None else "ASSUMED_PUBLISHED_VIP0",
                           "spot_fee_origin": "ACCOUNT_READ" if self.spot_fee is not None else "ASSUMED_PUBLISHED_VIP0", "fee_observed_ms": self.fee_observed_ms}
    cash, state, _why = _cash_status(self)
    out.update({"cost_of_capital_state": state, "cash_alternative_apr": None if cash is None else _q(cash.apr), "cash_alternative_source": None if cash is None else cash.source,
                "cash_alternative_stated_ms": None if cash is None else cash.stated_ms, "cash_alternative_provenance": None if cash is None else cash.provenance})
    out.update(_touch_summary("perp", self.perp_book, int(self.now_ms)))
    out.update(_touch_summary("spot", self.spot_book, int(self.now_ms)))
    return out


EdgeInputs.summary = _inputs_summary                                  # type: ignore[attr-defined]


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
    capital: Mapping[str, Any] = dataclasses.field(default_factory=dict)     # the cost-of-capital basis: state, capital committed, the stated rate with its source / time / provenance, the charge

    def as_dict(self) -> Dict[str, Any]:
        return dataclasses.asdict(self)


def _cash_status(inp: "EdgeInputs") -> Tuple[Optional[CashAlternative], str, List[str]]:
    """(statement, state, reasons). Only STATED is usable. A stale, future-dated or malformed statement is reported WITH its figures (so the owner sees what is wrong) but is UNKNOWN to the model."""
    c = inp.cash_alternative
    if c is None:
        return None, "UNSTATED", ["COST_OF_CAPITAL_UNKNOWN:NO_CASH_ALTERNATIVE_STATED"]
    if not isinstance(c, CashAlternative):
        return None, "INVALID", ["COST_OF_CAPITAL_UNKNOWN:NOT_A_CASH_ALTERNATIVE_STATEMENT"]
    age = int(inp.now_ms) - c.stated_ms
    if age > MAX_CASH_ALTERNATIVE_AGE_MS:
        return c, "STALE", [f"COST_OF_CAPITAL_UNKNOWN:CASH_ALTERNATIVE_STALE_{age // 86_400_000}_DAYS_OLD"]
    if -age > CASH_ALTERNATIVE_FUTURE_TOLERANCE_MS:
        return c, "FUTURE_DATED", ["COST_OF_CAPITAL_UNKNOWN:CASH_ALTERNATIVE_DATED_IN_THE_FUTURE"]
    return c, "STATED", []


def _capital_block(inp: "EdgeInputs", cash: Optional[CashAlternative], state: str, opportunity_cost_bps: Optional[D]) -> Dict[str, Any]:
    ok = inp.notional_usd > 0 and inp.leverage >= 1
    committed = EM.hedged_pair_capital_usd(inp.notional_usd, inp.leverage).value if ok else None      # the plan's own capital: spot + perp margin + its buffer (economic_model)
    mult = None if committed is None else committed / inp.notional_usd
    return {"state": state, "committed_usd": None if committed is None else _q(committed), "multiple_of_notional": None if mult is None else _q(mult),
            "cash_alternative_apr": None if cash is None else _q(cash.apr), "cash_alternative_source": None if cash is None else cash.source,
            "cash_alternative_stated_ms": None if cash is None else cash.stated_ms, "cash_alternative_provenance": None if cash is None else cash.provenance,
            "opportunity_cost_bps": _q(opportunity_cost_bps)}


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
    cash, cash_state, cash_reasons = _cash_status(inp)
    cap0 = _capital_block(inp, cash, cash_state, None)
    try:
        perp = EM.Instrument.from_venue_row({**inp.perp_row, "category": "linear"}, expect_symbol=inp.symbol, expect_category="linear", source_ms=inp.now_ms)
        spot = EM.Instrument.from_venue_row(_spot_instrument_row(inp.spot_row), expect_symbol=inp.symbol, expect_category="spot", source_ms=inp.now_ms)
    except (EM.EconomicModelError, KeyError, TypeError, ValueError) as exc:
        return _verdict(INCONCLUSIVE, [f"INSTRUMENT_INVALID:{str(exc)[:120]}"], None, None, None, {}, (), 0, inp, capital=cap0)
    n_settle = _settlements_in_hold(inp.now_ms, int(inp.next_funding_ms), inp.hold_s)
    if n_settle == 0:
        return _verdict(REJECTED, ["NO_SETTLEMENT_INSIDE_THE_HOLD"], None, None, None, {}, (), 0, inp, capital=cap0)
    if inp.hold_s <= 0 or inp.notional_usd <= 0 or inp.leverage < 1:
        return _verdict(INCONCLUSIVE, ["INPUT_NOT_POSITIVE"], None, None, None, {}, (), n_settle, inp, capital=cap0)

    def tier(read: Optional[Tuple[D, D]], category: str) -> EM.FeeTier:
        if read is not None and inp.fee_observed_ms is not None:
            return EM.FeeTier(maker=read[0], taker=read[1], source="measured", observed_ms=inp.fee_observed_ms)
        return EM.FeeTier(maker=PUBLISHED_VIP0_MAKER[category], taker=PUBLISHED_VIP0_TAKER[category], source="assumed")

    spot_tier, perp_tier = tier(inp.spot_fee, "spot"), tier(inp.perp_fee, "linear")
    rate_bps = inp.funding_rate * BPS
    expected_funding = rate_bps * (D(1) - FUNDING_HAIRCUT) * D(n_settle)        # negative rate => a short PAYS: a negative expectation, never rounded up
    basis_bps = abs(EM.basis_bps(inp.perp_book.mid, inp.spot_book.mid)) if not (inp.perp_book.problems() or inp.spot_book.problems()) else None
    hedge_ratio = D(1) - spot_tier.taker                                         # a spot BUY's fee is taken in the base coin: the hedge is that much under
    # THE COST OF CAPITAL: the owner's STATED alternative return, charged on the capital COMMITTED (spot notional + perp margin = notional x (1 + 1/leverage)), expressed as an annual rate on NOTIONAL
    # because that is the base the model charges per hold. Anything but a usable statement is UNKNOWN (never zero): the model then has no net edge and the verdict cannot be a PROFIT_CANDIDATE.
    committed_q = EM.hedged_pair_capital_usd(inp.notional_usd, inp.leverage)
    capital_multiple = committed_q.value / inp.notional_usd
    cash_q = EM.Quantity(cash.apr, cash.provenance, "fraction") if (cash is not None and cash_state == "STATED") else EM.unknown("fraction")
    opportunity_cost_apr = cash_q * EM.Quantity(capital_multiple, "derived", "ratio")
    zero_reasons = {"borrow_apr": "SPOT_LEG_PAID_FROM_USDT_CASH_NO_BORROW"}
    if cash_state == "STATED" and cash is not None and cash.apr == 0:
        zero_reasons["opportunity_cost_apr"] = "OWNER_STATED_ZERO_COST_OF_CAPITAL"            # capital priced as free BY THE OWNER'S STATEMENT: the only way a zero cost of capital may pass
    scn = EM.Scenario(
        legs=(EM.Leg("spot", spot, "buy", inp.spot_book, spot_tier, "taker", fee_in_base=True), EM.Leg("perp", perp, "sell", inp.perp_book, perp_tier, "taker")),
        notional_usd=inp.notional_usd, gross_edge_bps=EM.Quantity(expected_funding, "modelled", "bps"), hold_days=D(inp.hold_s) / D(86_400), now_ms=inp.now_ms,
        max_book_age_ms=MAX_BOOK_AGE_MS, max_fee_age_ms=MAX_FEE_AGE_MS, impact_bps=EM.Quantity(IMPACT_BPS, "modelled", "bps"),
        basis_cost_bps=(EM.Quantity(max(basis_bps, BASIS_STRESS_BPS), "modelled", "bps") if basis_bps is not None else EM.unknown("bps")),
        borrow_apr=EM.structural_zero("fraction", "SPOT_LEG_PAID_FROM_USDT_CASH_NO_BORROW"),     # spot is bought with USDT, not borrowed (declared in zero_reasons)
        opportunity_cost_apr=opportunity_cost_apr,
        hedge_ratio=EM.Quantity(hedge_ratio, "derived", "ratio"), adverse_move_bps=EM.Quantity(ADVERSE_MOVE_BPS, "modelled", "bps"),
        margin_usd=EM.Quantity(inp.notional_usd * (D(1) + D(1) / D(inp.leverage)), "derived", "usd"), zero_reasons=zero_reasons)
    try:
        res = EM.evaluate_chain(scn)
    except EM.EconomicModelError as exc:
        return _verdict(INCONCLUSIVE, [f"MODEL_REFUSED:{str(exc)[:120]}"], None, None, None, {}, (), n_settle, inp, capital=cap0)
    comps = {k: (None if not q.known else q.value) for k, q in res.components.items()}
    net = res.net_edge_bps
    assumed = tuple(res.assumed_inputs)
    capital = _capital_block(inp, cash, cash_state, comps.get("opportunity_cost"))
    reasons += list(res.blockers)
    if res.unknown_inputs:
        reasons.append("UNKNOWN_INPUTS:" + ",".join(res.unknown_inputs))
    reasons += cash_reasons
    if cash_state == "STATED" and cash is not None and cash.apr == 0:
        reasons.append("COST_OF_CAPITAL_ZERO_BY_OWNER_STATEMENT")                # capital priced as free BY DECISION: said out loud, never silent
    # the funding (bps per settlement) at which the expected net equals the hurdle: all known costs + the hurdle, spread over the settlements and the haircut
    known_costs = [v for v in comps.values() if v is not None]
    required = None
    if not res.unknown_inputs and not res.blockers:
        required = (sum(known_costs, D(0)) + MIN_NET_EDGE_BPS) / ((D(1) - FUNDING_HAIRCUT) * D(n_settle))
    if not net.known or res.blockers:
        cls = INCONCLUSIVE
        if not res.blockers and not assumed and res.unknown_inputs == ("opportunity_cost",):
            # REJECTION BY BOUND ONLY. The cost of capital can only LOWER the net edge, so if the edge misses the hurdle with that one unknown counted as free, no capital cost can rescue it: REJECTED.
            # The bound (economic_model.net_edge_upper_bound_bps) is used for nothing but this comparison and is never reported as a net: it can reject, it can never pass.
            bound = EM.net_edge_upper_bound_bps(res, nonneg_unknown=("opportunity_cost",))
            if bound is not None and bound < MIN_NET_EDGE_BPS:
                cls = REJECTED
                reasons.append(f"NET_EDGE_AT_MOST_{bound:.2f}bps_BELOW_HURDLE_{MIN_NET_EDGE_BPS}bps_BEFORE_ANY_COST_OF_CAPITAL")
    elif assumed:
        cls = INCONCLUSIVE
        reasons.append("ASSUMED_INPUTS_CANNOT_PASS:" + ",".join(assumed))
    elif net.value >= MIN_NET_EDGE_BPS:
        cls = PROFIT_CANDIDATE
    else:
        cls = REJECTED
        reasons.append(f"NET_EDGE_{net.value:.2f}bps_BELOW_HURDLE_{MIN_NET_EDGE_BPS}bps")
    net_usd = (net.value / BPS * inp.notional_usd) if net.known else None
    return _verdict(cls, reasons, net.value if net.known else None, expected_funding, required, comps, assumed, n_settle, inp, net_usd, capital=capital)


def _verdict(cls: str, reasons: Sequence[str], net: Optional[D], funding: Optional[D], required: Optional[D], comps: Mapping[str, Optional[D]], assumed: Sequence[str], n: int,
             inp: EdgeInputs, net_usd: Optional[D] = None, capital: Optional[Mapping[str, Any]] = None) -> EdgeVerdict:
    cap = dict(capital or {})
    body = {"class": cls, "reasons": list(reasons), "net": _q(net), "funding": _q(funding), "required": _q(required), "comps": {k: _q(v) for k, v in sorted(comps.items())},
            "assumed": list(assumed), "n": n, "symbol": inp.symbol, "notional": _q(inp.notional_usd), "now_ms": inp.now_ms, "hold_s": inp.hold_s, "capital": cap}
    dg = hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    return EdgeVerdict(cls, tuple(reasons), _q(net), _q(funding), _q(required), _q(net_usd), {k: _q(v) for k, v in sorted(comps.items())}, tuple(assumed), n, dg, cap)


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


def describe_cash_alternative(env: Mapping[str, str], now_ms: int) -> Dict[str, Any]:
    """The owner-facing state of the cost of capital, for ``status`` and the evaluation result: UNSTATED | INVALID | STALE | FUTURE_DATED | STATED, with the figures the owner supplied and what is wrong.
    Exactly the judgement ``evaluate_edge`` makes (it uses the same statement and the same clock rules); it creates nothing and defaults nothing."""
    ca, state, why = cash_alternative_from_env(env)
    out: Dict[str, Any] = {"state": state, "usable": False, "apr": None, "source": None, "stated_ms": None, "age_days": None, "problems": list(why),
                           "names": [ENV_CASH_APR, ENV_CASH_SOURCE, ENV_CASH_STATED_UTC], "max_age_days": MAX_CASH_ALTERNATIVE_AGE_MS // 86_400_000}
    if ca is None:
        return out
    age = int(now_ms) - ca.stated_ms
    out.update(apr=_q(ca.apr), source=ca.source, stated_ms=ca.stated_ms, age_days=round(age / 86_400_000.0, 2), provenance=ca.provenance)
    if age > MAX_CASH_ALTERNATIVE_AGE_MS:
        out.update(state="STALE", problems=[f"CASH_ALTERNATIVE_STALE:{age // 86_400_000}_days_old (max {MAX_CASH_ALTERNATIVE_AGE_MS // 86_400_000}): state it again"])
    elif -age > CASH_ALTERNATIVE_FUTURE_TOLERANCE_MS:
        out.update(state="FUTURE_DATED", problems=["CASH_ALTERNATIVE_DATED_IN_THE_FUTURE"])
    else:
        out["usable"] = True
    return out


#: A book stamped by the venue may be a little AHEAD of the local clock reading that precedes the reads (read latency, host/venue skew; the session tool refuses a host more than 2 s off the venue). The
#: verdict is then evaluated at the data's own time instead of being refused as a book "from the future" (``STALE_BOOK``), which on a real clock made every verdict INCONCLUSIVE. Beyond the bound the
#: book stays refused: a clock that far off is not trusted.
FUTURE_BOOK_TOLERANCE_MS = 2000


def collect(ex: Any, symbol: str, notional_usd: D, hold_s: int, funding_rate: D, next_funding_ms: int, now_ms: int, leverage: int = 1, clock: Optional[Callable[[], Any]] = None,
            cash_alternative: Optional[CashAlternative] = None) -> EdgeInputs:
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
        observed = None
    # the verdict's "now" is the clock AFTER the last read (a caller's earlier reading can only be older than the books it is about to price), and never earlier than the newest book within the bound
    now = max(int(now_ms), int(clock())) if clock is not None else int(now_ms)
    newest = max(int(perp_book.ts_ms), int(spot_book.ts_ms))
    if 0 < newest - now <= FUTURE_BOOK_TOLERANCE_MS:
        now = newest
    if observed is not None:
        observed = now
    return EdgeInputs(symbol, notional_usd, now, funding_rate, next_funding_ms, hold_s, perp_row, spot_row, perp_book, spot_book, leverage, perp_fee, spot_fee, observed, cash_alternative)
