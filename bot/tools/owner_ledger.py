#!/usr/bin/env python3
"""The owner's complete P&L ledger for the BTC funding carry: what the operation (owned BTC + short perpetual) actually earned or lost,
reconciled to account equity. Offline, local-files-only, order-less: it makes no request, places no order and writes nothing.

    python3 tools/owner_ledger.py --manifest DIR/manifest.json [--json] [--alternative-return-pct X]

Why this exists. ``account_review`` checks the PERPETUAL side of a book (positions, settlements, fees, basis). It cannot say what the
whole operation earned, because the economics of the hedge also live in the spot BTC inventory, in deposits and withdrawals, in
interest, and in the account's own balance. This module reconciles ALL of it to the account equity at two instants.

The identity it derives and tests (USD; E = USDT + BTC x P + UPL, the account's equity at a snapshot):

    E1 - E0 =  external flows (deposits - withdrawals, valued when they happened)
             + perpetual realised P&L (average-entry accounting, the venue's convention)
             + change in unrealised perpetual P&L
             + funding received - funding paid
             - perpetual fees - interest + other cash lines
             + BTC economic P&L   [ V1 - V0 - (spot buys + fees - spot sells + BTC flows in - BTC flows out) , V = BTC x P ]

Spot cash cancels between the USDT balance and the BTC valuation, so the spot trades enter only through the BTC economic P&L. The ledger
rebuilds each closing component (USDT, BTC quantity, perpetual position and unrealised P&L) from the opening snapshot and the records and
compares them with the closing snapshot: a missing record, a duplicate, a wrong sign, a wrong currency or an unexplained movement leaves
a named residual; it is never absorbed. Deposits are not profit; unrealised P&L is not realised cash; an internal transfer is neither
income nor outflow and must be paired.

What it can and cannot say. Every figure is OWNER_SUPPLIED_UNVERIFIED. A sha256 proves the bytes were not changed after ingestion; it
does not prove that the exchange produced them. The verdict is never "account verified"; nothing here changes a status or authorises a
trade. If there are no perpetual executions or settlements in the window, the correct answer is that realised proprietary P&L has NOT
been established, and that is what it says; paper and testnet records are not live evidence and are never accepted as such.

Records (JSON lines or CSV; times are integer milliseconds since the UTC epoch; a file whose columns differ is mapped through an explicit,
declared column map in the manifest, never guessed). Families:

  perp_executions  exec_id ts_ms symbol side qty price fee_usd liquidity [fee_currency ref_price spot_ref]       (see account_review)
  funding          exec_id ts_ms symbol position_qty rate payment_usd [currency]                                  (see account_review)
  spot_trades      trade_id ts_ms symbol side qty price fee fee_coin                                              fee >= 0 paid, < 0 rebate
  external_flows   flow_id ts_ms coin amount kind [usd_value transfer_id]    amount > 0 INTO the account; kind in EXTERNAL_KINDS
  cash_lines       line_id ts_ms coin category change [cash_balance ref_id]  the account's own statement; change > 0 increases the balance
  snapshots        snapshot_id ts_ms usdt btc btc_price perp_upl_usd [perp_mark perp_qty perp_avg_entry equity_usd other_usd
                   margin_posted_usd reserve_usd source]

The reporting window is (first snapshot, last snapshot]: a record exactly at the opening snapshot's time is already inside the opening
balance; a record exactly at the closing snapshot's time is inside the period.
"""
from __future__ import annotations

import argparse
import bisect
import json
import math
import os
import sys
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
BOT = os.path.dirname(HERE)
for _p in (BOT, HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import account_review as AR     # noqa: E402
import bybit_v5_records as BV5  # noqa: E402

RecordError = AR.RecordError
PROVENANCE = "OWNER_SUPPLIED_UNVERIFIED"
#: What a reconciled verdict does NOT establish, stated once and carried in every report.
LIMITS = (
    "the cross-checks are on TOTALS per record family (fees, funding, realised, flows, spot) and on settlement ownership, not a match of each execution to its "
    "own statement line, so a record's timestamp or a swap between two equal-size records is not detected",
    "paper and testnet exports have the same shape as live ones and are not detected: the owner must supply only live-account records",
    "the strategy figure is exact only for complete round trips whose fills carry a spot reference, and that reference is the supplier's",
    "the BTC leg of the bridge is only as good as the snapshot BTC prices: they are checked against the public 4h bar's low..high widened 0.3 % (a wide band, "
    "hundreds of USD per BTC) and ONLY where a bar covers the instant; the committed history ends 2026-09-09, so for later periods they are the supplier's word "
    "(SNAPSHOT_PRICE_CHECK_NOT_RUN) unless a public spot 4h kline file covering the period is named in the manifest",
    "when the statement, the funding, the fees and the flows are all read from ONE transaction-log file, their cross-checks agree by construction: an independent "
    "execution list (and, for balances, snapshots from a different source) is what makes them independent",
    "the venue's own cost-basis convention for realised P&L is assumed to be average entry; a different convention shows up as a REALISED_NOT_IN_STATEMENT gap",
)
DAY_MS = AR.DAY_MS
YEAR_DAYS = 365.0
MIN_ANNUALISE_DAYS = 30.0
EDGE_MS = 3_600_000
EXTERNAL_KINDS = ("DEPOSIT", "WITHDRAWAL", "TRANSFER_IN", "TRANSFER_OUT", "INTERNAL_IN", "INTERNAL_OUT")
INTERNAL_KINDS = ("INTERNAL_IN", "INTERNAL_OUT")
# account_review findings that limit the STRATEGY's attributable P&L but not the account-level reconciliation (a basis needs a spot reference; an open
# round trip has not finished): they are listed with the strategy figure and never turned into an account-level gap
STRATEGY_LIMIT_CODES = ("OPEN_LIFECYCLE", "NO_SPOT_REFERENCE", "SPOT_REF_IS_A_COPY_OF_THE_PRICE", "SPOT_REF_CHECK_NOT_RUN", "SPOT_REF_CHECK_PARTIAL", "ROUND_TRIP_WITHOUT_SETTLEMENT")
CASH_CATEGORIES = ("TRADE", "FEE", "FUNDING", "REBATE", "INTEREST", "EXTERNAL", "INTERNAL", "SPOT", "OTHER")
USD_COINS = ("USDT", "USD")
COINS = ("USDT", "USD", "BTC")
SPOT_FEE_COINS = ("USDT", "USD", "BTC")
MAX_USD = 1e12
MAX_BTC = 1e6
RESIDUAL_TOLERANCE_USD = 5.0                 # absolute floor: fee rounding across hundreds of fills
RESIDUAL_TOLERANCE_REL = 1e-4                # ... or this fraction of the larger of the two equities
SIGN_FLIP_TOLERANCE = 1e-9

SEVERITY_HARD = AR.SEVERITY_HARD
SEVERITY_GAP = AR.SEVERITY_GAP

#: Gaps that leave part of the history unexplained: the ledger is INCOMPLETE and no positive conclusion is drawn.
FLOW_SNAPSHOT_ENVELOPE = 0.25           # BTC flow value vs snapshot prices when no public bar covers it (coarse gross-error check only)
CHECK_GAPS = ("PERP_CHECKS_UNRUN", "FEE_CURRENCY_NOT_STATED", "DUPLICATE_IDENTICAL_DROPPED", "SNAPSHOT_PRICE_CHECK_NOT_RUN", "FLOW_PRICE_CHECK_NOT_RUN")


# ---------------------------------------------------------------- small helpers

def _num(v: Any, name: str, where: str) -> float:
    return AR._num(v, name, where)


def _opt_num(v: Any, name: str, where: str) -> Optional[float]:
    if v is None or (isinstance(v, str) and not v.strip()):
        return None
    return _num(v, name, where)


def _bounded(x: float, lo: float, hi: float, name: str, where: str) -> float:
    return AR._bounded(x, lo, hi, name, where)


def _str(v: Any, name: str, where: str) -> str:
    return AR._str(v, name, where)


def _require(rows: Sequence[Mapping[str, Any]], cols: Sequence[str], what: str) -> List[str]:
    AR._missing(rows, cols, what)
    return sorted({str(k) for r in rows for k in r} - set(cols))


def _flag(flags: List[Dict[str, Any]], code: str, severity: str, count: int, detail: str, examples: Sequence[Any] = ()) -> None:
    AR._flag(flags, code, severity, count, detail, examples)


# ---------------------------------------------------------------- canonical records

@dataclass(frozen=True)
class SpotTrade:
    trade_id: str
    ts_ms: int
    side: str                   # Buy | Sell
    qty: float                  # BTC
    price: float                # USD per BTC
    fee_amount: float           # in fee_coin; >= 0 paid, < 0 rebate
    fee_coin: str
    src: str

    @property
    def notional(self) -> float:
        return self.qty * self.price

    @property
    def fee_usd(self) -> float:
        return self.fee_amount * (self.price if self.fee_coin == "BTC" else 1.0)

    @property
    def btc_fee(self) -> float:
        return self.fee_amount if self.fee_coin == "BTC" else 0.0

    @property
    def usd_fee(self) -> float:
        return self.fee_amount if self.fee_coin in USD_COINS else 0.0


@dataclass(frozen=True)
class ExternalFlow:
    flow_id: str
    ts_ms: int
    coin: str
    amount: float               # > 0 into the account, < 0 out of it
    kind: str
    usd_value: Optional[float]  # signed like amount; required for BTC (no price is invented)
    transfer_id: Optional[str]
    src: str


@dataclass(frozen=True)
class CashLine:
    line_id: str
    ts_ms: int
    coin: str
    category: str
    change: float
    cash_balance: Optional[float]
    ref_id: Optional[str]
    src: str
    row_id: Optional[str] = None     # the venue row several lines were split from (a fee, a funding amount and a cash flow share one running balance)


@dataclass(frozen=True)
class Snapshot:
    snapshot_id: str
    ts_ms: int
    usdt: float
    btc: float
    btc_price: float
    perp_upl_usd: float
    perp_mark: Optional[float]
    perp_qty: Optional[float]
    perp_avg_entry: Optional[float]
    equity_usd: Optional[float]
    other_usd: float
    margin_posted_usd: Optional[float]
    reserve_usd: Optional[float]
    src: str

    @property
    def components_equity(self) -> float:
        return self.usdt + self.btc * self.btc_price + self.perp_upl_usd + self.other_usd

    @property
    def mark(self) -> float:
        return self.perp_mark if self.perp_mark is not None else self.btc_price


def parse_spot_trades(rows: Sequence[Mapping[str, Any]], label: str = "spot_trades", symbol: str = "BTCUSDT") -> Tuple[List[SpotTrade], List[str]]:
    ignored = _require(rows, ("trade_id", "ts_ms", "symbol", "side", "qty", "price", "fee", "fee_coin"), "spot_trades")
    out: List[SpotTrade] = []
    for i, r in enumerate(rows):
        w = f"{label} row {i + 1}"
        if _str(r.get("symbol"), "symbol", w) != symbol:
            raise RecordError(f"{w}: symbol {r.get('symbol')!r} is not {symbol!r}")
        side = _str(r.get("side"), "side", w)
        if side not in ("Buy", "Sell"):
            raise RecordError(f"{w}: side {side!r} is not Buy or Sell")
        coin = _str(r.get("fee_coin"), "fee_coin", w).upper()
        if coin not in SPOT_FEE_COINS:
            raise RecordError(f"{w}: fee_coin {coin!r} is not one of {', '.join(SPOT_FEE_COINS)}: a fee in another currency cannot be valued here")
        qty = _bounded(_num(r.get("qty"), "qty", w), 1e-8, MAX_BTC, "qty", w)
        price = _bounded(_num(r.get("price"), "price", w), 1e-6, AR.MAX_PRICE, "price", w)
        fee = _bounded(_num(r.get("fee"), "fee", w), -MAX_USD, MAX_USD, "fee", w)
        fee_usd = abs(fee) * (price if coin == "BTC" else 1.0)
        if fee_usd > AR.MAX_FEE_FRACTION * qty * price:
            raise RecordError(f"{w}: fee {fee} {coin} is more than {AR.MAX_FEE_FRACTION:.0%} of the trade's notional: a unit error, not a fee tier")
        out.append(SpotTrade(_str(r.get("trade_id"), "trade_id", w), AR._ts(r.get("ts_ms"), w), side, qty, price, fee, coin, f"{label}#{i + 1}"))
    return out, ignored


def parse_external_flows(rows: Sequence[Mapping[str, Any]], label: str = "external_flows") -> Tuple[List[ExternalFlow], List[str]]:
    ignored = _require(rows, ("flow_id", "ts_ms", "coin", "amount", "kind"), "external_flows")
    out: List[ExternalFlow] = []
    for i, r in enumerate(rows):
        w = f"{label} row {i + 1}"
        coin = _str(r.get("coin"), "coin", w).upper()
        coin = "USDT" if coin == "USD" else coin             # one settlement currency: a USD-labelled row must not escape the USDT chain and its anchors
        if coin not in COINS:
            raise RecordError(f"{w}: coin {coin!r} is not one of {', '.join(COINS)}: only the assets of this operation are accepted")
        kind = _str(r.get("kind"), "kind", w).upper()
        if kind not in EXTERNAL_KINDS:
            raise RecordError(f"{w}: kind {kind!r} is not one of {', '.join(EXTERNAL_KINDS)}")
        amount = _bounded(_num(r.get("amount"), "amount", w), -MAX_USD, MAX_USD, "amount", w)
        if amount == 0:
            raise RecordError(f"{w}: a flow of zero is not a flow")
        into = kind in ("DEPOSIT", "TRANSFER_IN", "INTERNAL_IN")
        if (amount > 0) != into:
            raise RecordError(f"{w}: kind {kind} contradicts the sign of amount {amount} (a positive amount is INTO the account)")
        usd = _opt_num(r.get("usd_value"), "usd_value", w)
        if usd is not None:
            _bounded(usd, -MAX_USD, MAX_USD, "usd_value", w)
            if usd != 0 and (usd > 0) != (amount > 0):
                raise RecordError(f"{w}: usd_value and amount have opposite signs")
            if usd == 0 and coin == "BTC":
                raise RecordError(f"{w}: a BTC flow of {amount} cannot be worth 0 USD: leave usd_value out if it is unknown (it is then reported as unpriced)")
        if coin in USD_COINS:
            usd = amount if usd is None else usd
            if not math.isclose(usd, amount, rel_tol=1e-9, abs_tol=1e-6):
                raise RecordError(f"{w}: a {coin} flow's usd_value must equal its amount")
        tid = r.get("transfer_id")
        out.append(ExternalFlow(_str(r.get("flow_id"), "flow_id", w), AR._ts(r.get("ts_ms"), w), coin, amount, kind, usd,
                                None if tid is None or (isinstance(tid, str) and not tid.strip()) else str(tid).strip(), f"{label}#{i + 1}"))
    return out, ignored


def parse_cash_lines(rows: Sequence[Mapping[str, Any]], label: str = "cash_lines") -> Tuple[List[CashLine], List[str]]:
    ignored = _require(rows, ("line_id", "ts_ms", "coin", "category", "change"), "cash_lines")
    out: List[CashLine] = []
    for i, r in enumerate(rows):
        w = f"{label} row {i + 1}"
        coin = _str(r.get("coin"), "coin", w).upper()
        coin = "USDT" if coin == "USD" else coin
        if coin not in COINS:
            raise RecordError(f"{w}: coin {coin!r} is not one of {', '.join(COINS)}")
        cat = _str(r.get("category"), "category", w).upper()
        ref = r.get("ref_id")
        rid = r.get("row_id")
        lid = _str(r.get("line_id"), "line_id", w)
        out.append(CashLine(lid, AR._ts(r.get("ts_ms"), w), coin, cat,
                            _bounded(_num(r.get("change"), "change", w), -MAX_USD, MAX_USD, "change", w),
                            _opt_num(r.get("cash_balance"), "cash_balance", w),
                            None if ref is None or (isinstance(ref, str) and not ref.strip()) else str(ref).strip(), f"{label}#{i + 1}",
                            lid if rid is None or (isinstance(rid, str) and not rid.strip()) else str(rid).strip()))
    return out, ignored


def parse_snapshots(rows: Sequence[Mapping[str, Any]], label: str = "snapshots") -> Tuple[List[Snapshot], List[str]]:
    ignored = _require(rows, ("snapshot_id", "ts_ms", "usdt", "btc", "btc_price", "perp_upl_usd"), "snapshots")
    out: List[Snapshot] = []
    for i, r in enumerate(rows):
        w = f"{label} row {i + 1}"
        mark = _opt_num(r.get("perp_mark"), "perp_mark", w)
        avg = _opt_num(r.get("perp_avg_entry"), "perp_avg_entry", w)
        qty = _opt_num(r.get("perp_qty"), "perp_qty", w)
        if qty is not None:
            _bounded(qty, -AR.MAX_QTY, AR.MAX_QTY, "perp_qty", w)
        if avg is not None:
            _bounded(avg, 1e-6, AR.MAX_PRICE, "perp_avg_entry", w)
        if mark is not None:
            _bounded(mark, 1e-6, AR.MAX_PRICE, "perp_mark", w)
        equity = _opt_num(r.get("equity_usd"), "equity_usd", w)
        out.append(Snapshot(
            _str(r.get("snapshot_id"), "snapshot_id", w), AR._ts(r.get("ts_ms"), w),
            _bounded(_num(r.get("usdt"), "usdt", w), -MAX_USD, MAX_USD, "usdt", w),
            _bounded(_num(r.get("btc"), "btc", w), -MAX_BTC, MAX_BTC, "btc", w),
            _bounded(_num(r.get("btc_price"), "btc_price", w), 1e-6, AR.MAX_PRICE, "btc_price", w),
            _bounded(_num(r.get("perp_upl_usd"), "perp_upl_usd", w), -MAX_USD, MAX_USD, "perp_upl_usd", w),
            mark, qty, avg, equity,
            _bounded(_opt_num(r.get("other_usd"), "other_usd", w) or 0.0, -MAX_USD, MAX_USD, "other_usd", w),
            _opt_num(r.get("margin_posted_usd"), "margin_posted_usd", w), _opt_num(r.get("reserve_usd"), "reserve_usd", w), f"{label}#{i + 1}"))
    out.sort(key=lambda s: (s.ts_ms, s.snapshot_id))
    return out, ignored


# ---------------------------------------------------------------- the venue's perpetual accounting

def perp_average_entry(fills: Sequence[AR.Fill], opening_qty: float = 0.0, opening_avg: Optional[float] = None) -> Dict[str, Any]:
    """Average-entry accounting for a LINEAR perpetual (the convention the venue's position record uses; verify against your own
    statement): adding to a position moves the average entry to the quantity-weighted price; reducing realises (entry - exit) x qty on
    a short and (exit - entry) x qty on a long at the unchanged average; crossing zero realises the old position and opens the rest at
    the fill price. Returns the realised P&L, the final signed quantity and average entry, and the cash effect of the fills.
    Fills at the same millisecond are processed in (exec_id) order; that can change the realised/unrealised SPLIT inside a flip, never
    the total (realised + unrealised), which depends only on the cash and the final mark."""
    if opening_qty != 0 and not (opening_avg and opening_avg > 0):
        raise RecordError("an opening perpetual position needs its average entry price")
    q = float(opening_qty)
    a = float(opening_avg) if q != 0 else 0.0
    realised = 0.0
    cash = 0.0
    for f in sorted(fills, key=lambda x: (x.ts_ms, x.exec_id)):
        sq = f.signed_qty
        cash += -sq * f.price
        if q == 0 or (q > 0) == (sq > 0):                         # opens or adds
            a = (abs(q) * a + abs(sq) * f.price) / (abs(q) + abs(sq))
            q += sq
        else:
            closing = min(abs(sq), abs(q))
            realised += (a - f.price) * closing if q < 0 else (f.price - a) * closing
            rest = abs(sq) - closing
            q += sq
            if abs(q) <= AR.DUST_QTY:
                q, a = 0.0, 0.0
            elif rest > 0:                                         # crossed zero: the remainder opens a new position at this price
                a = f.price
    if abs(q) <= AR.DUST_QTY:
        q, a = 0.0, 0.0
    return {"realised_usd": realised, "qty": q, "avg_entry": a, "cash_usd": cash}


# ---------------------------------------------------------------- returns

def _years(t0: int, t1: int) -> float:
    return (t1 - t0) / DAY_MS / YEAR_DAYS


def modified_dietz(e0: float, e1: float, flows: Sequence[Tuple[int, float]], t0: int, t1: int) -> Optional[Dict[str, Any]]:
    """Money-weighted return over the period: (E1 - E0 - F) / (E0 + sum w_i F_i), w_i = (T - t_i) / T. Flows are contributions (+ into the
    account). None when the denominator is not positive (an account that starts empty and is funded mid-period needs the flows as capital).
    ``base_small`` marks a base under a tenth of the balances (an early withdrawal that leaves almost nothing at work): the ratio is then
    arithmetically valid but not a meaningful rate, and it is never annualised. A window under 30 days is never annualised either."""
    if t1 <= t0:
        return None
    big_t = float(t1 - t0)
    f_total = math.fsum(f for _t, f in flows)
    denom = e0 + math.fsum(((t1 - t) / big_t) * f for t, f in flows)
    if not denom > 0:
        return None
    r = (e1 - e0 - f_total) / denom
    years = _years(t0, t1)
    days = big_t / DAY_MS
    small = denom < 0.1 * max(abs(e0), abs(e1))
    annual = r / years * 100.0 if (years > 0 and days >= MIN_ANNUALISE_DAYS and not small) else None
    out = {"method": "MODIFIED_DIETZ", "period_return": r, "capital_base_usd": denom, "days": days, "annualised_simple_pct": annual, "base_small": small}
    if annual is None:
        out["annualised_why_not"] = ("the capital at work is under a tenth of the balances: the ratio is not a meaningful rate" if small
                                     else f"the window is shorter than {MIN_ANNUALISE_DAYS:g} days: an annual figure would extrapolate noise")
    return out


def irr(e0: float, e1: float, flows: Sequence[Tuple[int, float]], t0: int, t1: int) -> Optional[Dict[str, Any]]:
    """The annual internal rate of return that equates the opening balance plus contributions with the closing balance plus withdrawals.
    Bisection on NPV over [-99 %, +5000 %] a year; None when there is no sign change in that range (no answer is stated), and no annual rate
    for a window shorter than ``MIN_ANNUALISE_DAYS`` (the rate would extrapolate a few days of noise to a year)."""
    if t1 - t0 < MIN_ANNUALISE_DAYS * DAY_MS:
        return {"method": "IRR", "annual_rate": None, "why_not": f"the window is shorter than {MIN_ANNUALISE_DAYS:g} days: an annual rate would extrapolate noise"}
    cfs = [(0.0, -e0)] + [((t - t0) / DAY_MS / YEAR_DAYS, -f) for t, f in flows] + [(_years(t0, t1), e1)]

    def npv(rate: float) -> float:
        return math.fsum(c / (1.0 + rate) ** t for t, c in cfs)
    lo, hi = -0.99, 50.0
    try:
        flo, fhi = npv(lo), npv(hi)
    except (OverflowError, ZeroDivisionError):
        return None
    if flo == 0:
        return {"method": "IRR", "annual_rate": lo}
    if flo * fhi > 0:
        return None
    for _ in range(200):
        mid = (lo + hi) / 2.0
        fm = npv(mid)
        if flo * fm <= 0:
            hi = mid
        else:
            lo, flo = mid, fm
    return {"method": "IRR", "annual_rate": (lo + hi) / 2.0, "note": "the first root found in [-99 %, +5000 %]; with flows of both signs a cash-flow series can have more than one"}


def annualise(period_return: float, days: Optional[float]) -> Tuple[Optional[float], Optional[str]]:
    """A simple annual figure in percent, or (None, why) for a window under ``MIN_ANNUALISE_DAYS`` (a few days of noise multiplied by a hundred)."""
    if days is None or days <= 0:
        return None, "no window"
    if days < MIN_ANNUALISE_DAYS:
        return None, f"the window is shorter than {MIN_ANNUALISE_DAYS:g} days: an annual figure would extrapolate noise"
    return period_return * YEAR_DAYS / days * 100.0, None


def linked_returns(snaps: Sequence[Snapshot], flows: Sequence[Tuple[int, float]]) -> Optional[Dict[str, Any]]:
    """Chain-linked modified-Dietz returns between consecutive snapshots. It is the time-weighted answer only when a snapshot exists at
    every flow; with flows between snapshots it is an approximation, and it says so. None with fewer than three snapshots."""
    if len(snaps) < 3:
        return None
    growth = 1.0
    parts = []
    for a, b in zip(snaps, snaps[1:]):
        fl = [(t, f) for t, f in flows if a.ts_ms < t <= b.ts_ms]
        d = modified_dietz(a.components_equity, b.components_equity, fl, a.ts_ms, b.ts_ms)
        if d is None:
            return None
        growth *= 1.0 + d["period_return"]
        parts.append(d["period_return"])
    days = (snaps[-1].ts_ms - snaps[0].ts_ms) / DAY_MS
    annual, why = annualise(growth - 1.0, days)
    out = {"method": "LINKED_MODIFIED_DIETZ", "period_return": growth - 1.0, "sub_periods": len(parts), "annualised_simple_pct": annual,
           "note": "time-weighted only if every external flow sits at a snapshot time"}
    if why:
        out["annualised_why_not"] = why
    return out


# ---------------------------------------------------------------- the ledger


@dataclass
class Inputs:
    fills: List[AR.Fill]
    funding: List[AR.FundingRow]
    spot: List[SpotTrade]
    flows: List[ExternalFlow]
    lines: List[CashLine]
    snapshots: List[Snapshot]
    digests: Dict[str, str]
    ignored: Dict[str, List[str]]
    perp_kwargs: Dict[str, Any]
    taker_bps: Optional[float] = None
    maker_bps: Optional[float] = None
    adapter_flags: Tuple[Dict[str, Any], ...] = ()
    adapters_used: Tuple[str, ...] = ()


def _dedupe(items: Sequence[Any], key, name: str, flags: List[Dict[str, Any]]) -> List[Any]:
    """Records are unique by id. A repeat that is IDENTICAL (overlapping export windows) is dropped and counted; a repeat that differs is
    two statements about one event and is an error."""
    seen: Dict[str, Any] = {}
    dropped: List[str] = []
    for it in items:
        k = key(it)
        if k in seen:
            a, b = {f: v for f, v in vars(seen[k]).items() if f != "src"}, {f: v for f, v in vars(it).items() if f != "src"}
            if a != b:
                raise RecordError(f"{name}: id {k!r} appears twice with different content ({seen[k].src} and {it.src}): which is the record is not decidable")
            dropped.append(k)
        else:
            seen[k] = it
    _flag(flags, "DUPLICATE_IDENTICAL_DROPPED", SEVERITY_GAP, len(dropped), f"{name}: identical repeats (overlapping exports) were dropped once each", dropped)
    return list(seen.values())


def _perp_dedupe(items: Sequence[Any], name: str, flags: List[Dict[str, Any]]) -> List[Any]:
    """The same rule for the perpetual records (frozen dataclasses compare by value)."""
    seen: Dict[str, Any] = {}
    dropped: List[str] = []
    for it in items:
        if it.exec_id in seen:
            if seen[it.exec_id] != it:
                raise RecordError(f"{name}: id {it.exec_id!r} appears twice with different content")
            dropped.append(it.exec_id)
        else:
            seen[it.exec_id] = it
    _flag(flags, "DUPLICATE_IDENTICAL_DROPPED", SEVERITY_GAP, len(dropped), f"{name}: identical repeats were dropped once each", dropped)
    return list(seen.values())


def _seq(src: str) -> int:
    """The row's position in its file, from the ``label#n`` source tag (numeric, so #10 follows #9)."""
    try:
        return int(src.rsplit("#", 1)[1])
    except (IndexError, ValueError):
        return 0


def _order_group(group: Sequence[Dict[str, Any]], prev_bal: Optional[float]) -> Optional[List[Dict[str, Any]]]:
    """An order of rows stamped in the SAME millisecond in which every balance follows from the one before (and the first from ``prev_bal`` when
    it is known). Rows in one millisecond have no recoverable order, so a depth-first walk finds one if it exists; a node budget bounds the work."""
    n = len(group)
    budget = [20000]

    def ok(cur: Optional[float], a: Dict[str, Any]) -> bool:
        return cur is None or math.isclose(a["bal"] - a["change"], cur, rel_tol=1e-9, abs_tol=1e-6)

    def walk(cur: Optional[float], used: int, path: List[int]) -> Optional[List[int]]:
        if len(path) == n:
            return path
        budget[0] -= 1
        if budget[0] < 0:
            return None
        for i in range(n):
            if not used >> i & 1 and ok(cur, group[i]):
                r = walk(group[i]["bal"], used | 1 << i, path + [i])
                if r is not None:
                    return r
        return None
    found = walk(prev_bal, 0, [])
    return None if found is None else [group[i] for i in found]


def _chain(lines: Sequence[CashLine], coin: str, seed: Optional[float] = None) -> Dict[str, Any]:
    """The running balance of one currency. Lines that share a ``row_id`` are one venue row (their changes add up and exactly ONE carries the
    balance). Returns the breaks (where a balance does not follow from the one before), the rows with NO balance (the chain cannot cover them),
    the balance the first row started from and the last row left, so the caller can anchor the chain to the snapshots."""
    steps: Dict[str, Dict[str, Any]] = {}
    for l in lines:
        st = steps.setdefault(l.row_id or l.line_id, {"id": l.row_id or l.line_id, "ts": l.ts_ms, "change": 0.0, "bal": None, "n_bal": 0, "seq": _seq(l.src)})
        st["change"] += l.change
        st["ts"] = max(st["ts"], l.ts_ms)
        st["seq"] = min(st["seq"], _seq(l.src))
        if l.cash_balance is not None:
            st["bal"] = l.cash_balance
            st["n_bal"] += 1
    unchained = [f"{coin}:{v['id']}" for v in steps.values() if v["n_bal"] == 0]
    breaks: List[str] = [f"{coin}:{v['id']}:more_than_one_balance_in_a_row" for v in steps.values() if v["n_bal"] > 1]
    ordered = sorted((v for v in steps.values() if v["n_bal"] == 1), key=lambda v: (v["ts"], v["seq"]))
    groups: List[List[Dict[str, Any]]] = []
    for v in ordered:
        if groups and groups[-1][0]["ts"] == v["ts"]:
            groups[-1].append(v)
        else:
            groups.append([v])
    prev_bal: Optional[float] = None
    prev_id: Optional[str] = None
    start: Optional[float] = None
    for g in groups:
        order = _order_group(g, prev_bal if prev_bal is not None else seed)      # the first group is tried from the snapshot's balance, then from nothing
        if order is None and prev_bal is None and seed is not None:
            order = _order_group(g, None)
        if order is None:
            breaks.append(f"{coin}:{prev_id or 'start'}->{g[0]['id']}")
            order = _order_group(g, None) or sorted(g, key=lambda v: v["seq"])      # re-synchronise: one defect is one break, not every later row
        if start is None:
            start = order[0]["bal"] - order[0]["change"]
        prev_bal, prev_id = order[-1]["bal"], order[-1]["id"]
    return {"breaks": breaks, "unchained": unchained, "start": start, "end": prev_bal, "steps": len(ordered)}


def _chain_breaks(lines: Sequence[CashLine], coin: str) -> List[str]:
    return _chain(lines, coin)["breaks"]


def _same(a: Optional[float], b: Optional[float], tol: float) -> bool:
    """The same figure stated twice (an API value and a UI value rounded to cents): equal within the anchoring tolerance; a field one copy omits is compatible."""
    return a is None or b is None or abs(a - b) <= max(tol, 1e-9 * max(abs(a), abs(b)))


_SNAP_NUMERIC = ("usdt", "btc", "btc_price", "perp_upl_usd", "perp_mark", "perp_qty", "perp_avg_entry", "equity_usd", "other_usd", "margin_posted_usd", "reserve_usd")


def _snapshot_dedupe(snaps: List[Snapshot], flags: List[Dict[str, Any]]) -> List[Snapshot]:
    """Two snapshots at one instant that DIFFER are two statements about the same moment: which is the balance is not decidable (a HARD
    contradiction, not a tie broken by an id's spelling). Identical ones are one."""
    out: List[Snapshot] = []
    clash: List[str] = []
    dropped: List[str] = []
    for sn in snaps:
        if out and out[-1].ts_ms == sn.ts_ms:
            if any(not _same(getattr(out[-1], f), getattr(sn, f), 1e-8 if f in ("btc", "perp_qty") else 0.01) for f in _SNAP_NUMERIC):
                clash.append(f"{out[-1].snapshot_id}/{sn.snapshot_id}")
            else:
                dropped.append(sn.snapshot_id)
            continue
        out.append(sn)
    _flag(flags, "SNAPSHOT_DUPLICATE_TIMESTAMP", SEVERITY_HARD, len(clash), "two snapshots at the same instant disagree: which one is the balance cannot be decided", clash)
    _flag(flags, "DUPLICATE_IDENTICAL_DROPPED", SEVERITY_GAP, len(dropped), "snapshots: identical repeats were dropped once each", dropped)
    return out


def _bar(ts: int) -> int:
    return ts - ts % AR.BAR_MS


def _snapshot_checks(snaps: Sequence[Snapshot], hist: Mapping[str, Any], flags: List[Dict[str, Any]]) -> None:
    """A snapshot must agree with the position it states, hold no assets the ledger cannot explain, and carry prices a 4h public bar allows."""
    upl_bad, no_mark, upl_unstated, other, price_bad, mark_bad = [], [], [], [], [], []
    price_unchecked = 0
    spot_range, perp_range = hist.get("spot_range"), hist.get("bar_range")
    tol = AR.PRICE_RANGE_TOL
    for sn in snaps:
        if sn.other_usd:
            other.append(sn.snapshot_id)
        if sn.perp_qty is None:
            if sn.perp_upl_usd:
                upl_unstated.append(sn.snapshot_id)
        elif abs(sn.perp_qty) > AR.DUST_QTY:
            if sn.perp_mark is None:
                no_mark.append(sn.snapshot_id)
            elif sn.perp_avg_entry is not None:
                expect = sn.perp_qty * (sn.perp_mark - sn.perp_avg_entry)
                if abs(sn.perp_upl_usd - expect) > max(1.0, 1e-4 * abs(sn.perp_qty) * sn.perp_mark):
                    upl_bad.append(f"{sn.snapshot_id}: stated {sn.perp_upl_usd:.2f}, position implies {expect:.2f}")
        elif abs(sn.perp_upl_usd) > 1.0:
            upl_bad.append(f"{sn.snapshot_id}: stated {sn.perp_upl_usd:.2f} on a flat position")
        rng = spot_range.get(_bar(sn.ts_ms)) if spot_range is not None else None
        if rng is None:
            price_unchecked += 1
        elif not (rng[0] * (1 - tol) <= sn.btc_price <= rng[1] * (1 + tol)):
            price_bad.append(f"{sn.snapshot_id}: btc_price {sn.btc_price:.2f} outside {rng[0]:.2f}..{rng[1]:.2f}")
        if sn.perp_mark is not None and perp_range is not None:
            prng = perp_range.get(_bar(sn.ts_ms))
            if prng is not None and not (prng[0] * (1 - tol) <= sn.perp_mark <= prng[1] * (1 + tol)):
                mark_bad.append(f"{sn.snapshot_id}: perp_mark {sn.perp_mark:.2f} outside {prng[0]:.2f}..{prng[1]:.2f}")
    _flag(flags, "SNAPSHOT_INCONSISTENT", SEVERITY_HARD, len(upl_bad), "a snapshot's unrealised P&L is not its stated position x (mark - average entry)", upl_bad)
    _flag(flags, "PERP_MARK_MISSING", SEVERITY_GAP, len(no_mark), "a snapshot states an open perpetual position but no perp_mark: its unrealised P&L cannot be checked", no_mark)
    _flag(flags, "SNAPSHOT_POSITION_UNSTATED", SEVERITY_GAP, len(upl_unstated), "a snapshot states an unrealised P&L but not the position it belongs to", upl_unstated)
    if other and len({round(sn.other_usd, 6) for sn in snaps}) == 1:
        _flag(flags, "OTHER_ASSETS_PRESENT", "INFO", len(other),
              "every snapshot states the same value for assets the ledger does not model (other_usd): it cancels in the bridge, and is the supplier's word", other)
    else:
        _flag(flags, "OTHER_ASSETS_PRESENT", SEVERITY_GAP, len(other),
              "a snapshot holds assets the ledger does not model (other_usd) whose value differs between snapshots: the move is shown as its own bridge line, not explained, "
              "so no conclusive result", other)
    _flag(flags, "SNAPSHOT_PRICE_OFF_PUBLIC_BAR", SEVERITY_HARD, len(price_bad) + len(mark_bad),
          f"a snapshot's price is outside the public 4h bar's low..high (widened {tol:.1%}): the owned BTC would be mis-valued", price_bad + mark_bad)
    _flag(flags, "SNAPSHOT_PRICE_CHECK_NOT_RUN", SEVERITY_GAP, 1 if price_unchecked else 0,
          f"{price_unchecked} snapshot price(s) lie outside the public history or no history was loaded: they are the supplier's word")


def _merge_flow_prices(flows: Sequence[ExternalFlow]) -> List[ExternalFlow]:
    """The same flow supplied twice, once without a USD value (the txlog adapter cannot price a BTC transfer) and once with: one flow, priced.
    A flow that differs in anything else is left for the duplicate rule to refuse."""
    by_id: Dict[str, ExternalFlow] = {}
    out: List[ExternalFlow] = []
    for f in sorted(flows, key=lambda x: x.usd_value is None):         # priced copies first, so the order the files were listed in cannot matter
        prev = by_id.get(f.flow_id)
        if prev is not None and (prev.ts_ms, prev.coin, prev.amount, prev.kind, prev.transfer_id) == (f.ts_ms, f.coin, f.amount, f.kind, f.transfer_id) \
                and (prev.usd_value is None) != (f.usd_value is None):
            priced = prev if prev.usd_value is not None else f
            out[out.index(prev)] = priced
            by_id[f.flow_id] = priced
            continue
        by_id.setdefault(f.flow_id, f)
        out.append(f)
    return out


def build(inp: Inputs, *, residual_tolerance_usd: float = RESIDUAL_TOLERANCE_USD, residual_tolerance_rel: float = RESIDUAL_TOLERANCE_REL,
          alternative_return_pct: Optional[float] = None) -> Dict[str, Any]:
    flags: List[Dict[str, Any]] = [dict(f, examples=list(f.get("examples", []))) for f in inp.adapter_flags]
    snaps = _snapshot_dedupe(sorted(inp.snapshots, key=lambda x: (x.ts_ms, x.snapshot_id)), flags)
    fills = _perp_dedupe(inp.fills, "perp_executions", flags)
    funding = _perp_dedupe(inp.funding, "funding", flags)
    by_grid: Dict[int, List[str]] = {}
    for fr in funding:
        by_grid.setdefault(fr.grid_ms, []).append(fr.exec_id)
    dup_settle = [f"{AR._iso(g)}: {', '.join(ids)}" for g, ids in sorted(by_grid.items()) if len(ids) > 1]
    _flag(flags, "SETTLEMENT_DUPLICATED_UNDER_DIFFERENT_IDS", SEVERITY_HARD, len(dup_settle),
          "one settlement appears under more than one id: counting both would double the funding, counting one is a guess", dup_settle)
    spot = _dedupe(inp.spot, lambda s: s.trade_id, "spot_trades", flags)
    ext = _dedupe(_merge_flow_prices(inp.flows), lambda f: f.flow_id, "external_flows", flags)
    lines = _dedupe(inp.lines, lambda l: l.line_id, "cash_lines", flags)

    # ---- the two instants the period is measured between
    s0 = snaps[0] if snaps else None
    s1 = snaps[-1] if len(snaps) >= 2 else None
    if s0 is None:
        _flag(flags, "NO_OPENING_SNAPSHOT", SEVERITY_GAP, 1, "no balance snapshot at all: there is no opening equity to measure from")
    if s1 is None:
        _flag(flags, "NO_CLOSING_SNAPSHOT", SEVERITY_GAP, 1, "fewer than two balance snapshots: there is no closing equity to reconcile to")
    t0, t1 = (s0.ts_ms if s0 else None), (s1.ts_ms if s1 else None)
    for s in snaps:                                          # each snapshot must agree with its own parts
        if s.equity_usd is not None and not math.isclose(s.equity_usd, s.components_equity, rel_tol=1e-6, abs_tol=max(1.0, 1e-6 * abs(s.equity_usd))):
            _flag(flags, "SNAPSHOT_INCONSISTENT", SEVERITY_HARD, 1,
                  f"snapshot {s.snapshot_id}: the stated equity {s.equity_usd} differs from usdt + btc x price + upl + other = {s.components_equity:.6f}", [s.snapshot_id])

    _snapshot_checks(snaps, inp.perp_kwargs, flags)

    def in_window(ts: int) -> bool:
        return t0 is not None and t1 is not None and t0 < ts <= t1

    outside: List[str] = []
    if t0 is not None and t1 is not None:
        for coll, getter, label in ((fills, lambda x: x.exec_id, "execution"), (funding, lambda x: x.exec_id, "funding"), (spot, lambda x: x.trade_id, "spot"),
                                    (ext, lambda x: x.flow_id, "flow"), (lines, lambda x: x.line_id, "line")):
            for x in coll:
                ts = x.grid_ms if label == "funding" else x.ts_ms
                if not in_window(ts):
                    edge = min(abs(ts - t0), abs(ts - t1)) <= EDGE_MS
                    outside.append(f"{label}:{getter(x)}" + ("@edge" if edge else ""))
        fills = [x for x in fills if in_window(x.ts_ms)]
        funding = [x for x in funding if in_window(x.grid_ms)]
        spot = [x for x in spot if in_window(x.ts_ms)]
        ext = [x for x in ext if in_window(x.ts_ms)]
        lines = [x for x in lines if in_window(x.ts_ms)]
    near, far = [], []
    for tag in outside:
        (near if tag.endswith("@edge") else far).append(tag[:-5] if tag.endswith("@edge") else tag)
    _flag(flags, "RECORDS_OUTSIDE_WINDOW", SEVERITY_GAP, len(near),
          "records within an hour of a snapshot but on the wrong side of it were excluded from the period (opening snapshot, closing snapshot]: a record on the wrong "
          "side of a boundary is a boundary error, so they are listed", near)
    _flag(flags, "RECORDS_BEYOND_WINDOW_IGNORED", "INFO", len(far),
          "records more than an hour outside the period were ignored: an export wider than the period is normal", far)

    # ---- the perpetual side, reusing the position/funding checks of account_review (with the position carried in and the period's end)
    opening_qty = (s0.perp_qty if (s0 and s0.perp_qty is not None) else 0.0)
    opening_avg = (s0.perp_avg_entry if s0 else None)
    perp_rep = None
    if (fills or abs(opening_qty) > AR.DUST_QTY) and t0 is not None and t1 is not None:
        perp_rep = AR.review(fills, funding, taker_bps=inp.taker_bps, maker_bps=inp.maker_bps, digests=inp.digests, ignored_columns=inp.ignored,
                             opening_qty=opening_qty, opening_ms=t0, closing_ms=t1, **inp.perp_kwargs)
        info_codes = set()
        for f in perp_rep["flags"]:
            if f["severity"] == SEVERITY_HARD:
                flags.append(dict(f, code="PERP_" + f["code"]))
            elif f["code"] in ("MISSING_SETTLEMENT", "AMBIGUOUS_SETTLEMENT", "FUNDING_ROW_IN_AMBIGUOUS_WINDOW"):
                flags.append(dict(f, code="PERP_" + f["code"]))        # a settlement the book was owed but the records do not explain: structural here
            elif f["severity"] == SEVERITY_GAP and f["code"] in AR.CHECK_GAPS:
                info_codes.add(f["code"])
            elif f["severity"] == "INFO":
                flags.append(dict(f))                                   # context for the reader (a position carried in): never changes the verdict
        if info_codes:
            _flag(flags, "PERP_CHECKS_UNRUN", SEVERITY_GAP, 1, "the perpetual records were not compared with the public market for the whole span: " + ", ".join(sorted(info_codes)))
    elif funding:
        _flag(flags, "PERP_RECORDS_INCOMPLETE", SEVERITY_GAP, 1, "funding settlements exist without any perpetual execution or opening position in the window: what earned them is not explained")

    # ---- the venue's average-entry accounting
    if s0 is not None and s0.perp_qty is None:
        _flag(flags, "OPENING_POSITION_UNKNOWN", SEVERITY_GAP, 1,
              "the opening snapshot does not state the perpetual position (perp_qty): a position carried into the period cannot be priced, so it is assumed flat only if stated")
    perp = None
    try:
        perp = perp_average_entry(fills, opening_qty, opening_avg)
    except RecordError as exc:
        _flag(flags, "OPENING_POSITION_UNKNOWN", SEVERITY_GAP, 1, str(exc))

    # ---- cash effects of every record family
    perp_fees = math.fsum(f.fee_usd for f in fills)                   # positive = paid
    funding_net = math.fsum(f.payment_usd for f in funding)            # positive = received
    spot_buy_cash = math.fsum(s.notional for s in spot if s.side == "Buy")
    spot_sell_cash = math.fsum(s.notional for s in spot if s.side == "Sell")
    spot_btc_delta = math.fsum(s.qty if s.side == "Buy" else -s.qty for s in spot) - math.fsum(s.btc_fee for s in spot)
    spot_usd_fee = math.fsum(s.usd_fee for s in spot)
    flow_off, flow_unchecked = [], 0
    spot_rng = inp.perp_kwargs.get("spot_range")
    for f in ext:
        if f.coin == "BTC" and f.usd_value is not None and f.kind not in INTERNAL_KINDS:
            rng = spot_rng.get(_bar(f.ts_ms)) if spot_rng is not None else None
            px = abs(f.usd_value / f.amount)
            if rng is None:
                flow_unchecked += 1
                # No public bar: a gross mispricing (the value cancels out of the bridge, so 'unexplained' can never catch it) must still not
                # read as profit. The supplier's own snapshot prices bound it, widened for drift between snapshots. Coarse by design.
                snap_px = [x.btc_price for x in snaps if x.btc_price]
                if snap_px and not (min(snap_px) * (1 - FLOW_SNAPSHOT_ENVELOPE) <= px <= max(snap_px) * (1 + FLOW_SNAPSHOT_ENVELOPE)):
                    flow_off.append(f"{f.flow_id}: {px:.2f} outside the snapshot price envelope {min(snap_px):.2f}..{max(snap_px):.2f} (no public bar)")
            elif not (rng[0] * (1 - AR.PRICE_RANGE_TOL) <= px <= rng[1] * (1 + AR.PRICE_RANGE_TOL)):
                flow_off.append(f"{f.flow_id}: {px:.2f} outside {rng[0]:.2f}..{rng[1]:.2f}")
    _flag(flags, "BTC_FLOW_VALUE_OFF_PUBLIC_BAR", SEVERITY_HARD, len(flow_off),
          f"a BTC deposit/withdrawal is valued outside the public spot 4h bar's low..high (widened {AR.PRICE_RANGE_TOL:.1%}): the value decides how profit is split "
          "between 'contributed' and 'earned', so a wrong one moves profit across that line", flow_off)
    _flag(flags, "FLOW_PRICE_CHECK_NOT_RUN", SEVERITY_GAP, 1 if flow_unchecked else 0, f"{flow_unchecked} BTC flow value(s) could not be compared with a public bar")
    unpriced_btc = [f.flow_id for f in ext if f.coin == "BTC" and f.usd_value is None and f.kind not in INTERNAL_KINDS]
    _flag(flags, "UNPRICED_BTC_FLOW", SEVERITY_GAP, len(unpriced_btc),
          "BTC deposits/withdrawals with no usd_value: no price is invented for a flow, so capital contributed is unknown", unpriced_btc)
    internal = [f for f in ext if f.kind in INTERNAL_KINDS]
    paired, unpaired = {}, []
    for f in internal:
        paired.setdefault(f.transfer_id or f"__{f.flow_id}", []).append(f)
    for tid, group in paired.items():
        if len(group) != 2 or abs(math.fsum(g.amount for g in group)) > 1e-9 or {g.coin for g in group} != {group[0].coin}:
            unpaired += [g.flow_id for g in group]
    _flag(flags, "UNPAIRED_INTERNAL_TRANSFER", SEVERITY_GAP, len(unpaired),
          "an internal transfer needs both legs (same transfer_id, same coin, equal and opposite): an unpaired one is cash that appeared or vanished", unpaired)
    external_only = [f for f in ext if f.kind not in INTERNAL_KINDS]
    usd_flow_by_ts = [(f.ts_ms, f.amount if f.coin in USD_COINS else (f.usd_value or 0.0)) for f in external_only]
    ext_usd = math.fsum(v for _t, v in usd_flow_by_ts)
    usdt_ext = math.fsum(f.amount for f in external_only if f.coin in USD_COINS)
    btc_ext = math.fsum(f.amount for f in external_only if f.coin == "BTC")
    btc_ext_usd = math.fsum(f.usd_value or 0.0 for f in external_only if f.coin == "BTC")
    unknown_lines = [l.line_id for l in lines if l.category not in CASH_CATEGORIES]
    _flag(flags, "UNCLASSIFIED_CASH_LINE", SEVERITY_GAP, len(unknown_lines),
          "statement lines whose category is not one the ledger knows: an unknown critical field blocks a conclusive result", unknown_lines)
    interest_usd = math.fsum(l.change for l in lines if l.category == "INTEREST" and l.coin in USD_COINS)
    other_usd_lines = math.fsum(l.change for l in lines if l.category == "OTHER" and l.coin in USD_COINS)

    # ---- statement coverage: the account's own lines are the independent record of every cash movement
    stmt = {"present": bool(lines), "continuity_breaks": [], "unchained_rows": [], "anchored": False, "fee_total": None, "funding_total": None}
    chains: Dict[str, Dict[str, Any]] = {}
    if not lines:
        _flag(flags, "NO_STATEMENT", SEVERITY_GAP, 1,
              "no account statement (cash_lines) was supplied: nothing independent of the executions confirms that every fee, settlement and transfer is present")
    else:
        for coin in COINS:
            cl = [l for l in lines if l.coin == coin]
            if cl:
                chains[coin] = _chain(cl, coin, (s0.usdt if coin == "USDT" else s0.btc if coin == "BTC" else None) if s0 is not None else None)
                stmt["continuity_breaks"] += chains[coin]["breaks"]
                stmt["unchained_rows"] += chains[coin]["unchained"]
        _flag(flags, "STATEMENT_GAP", SEVERITY_GAP, len(stmt["continuity_breaks"]),
              "the statement's running balance does not follow from its own changes between these consecutive lines: a line is missing, altered or out of order",
              stmt["continuity_breaks"])
        _flag(flags, "STATEMENT_UNCHAINED", SEVERITY_GAP, len(stmt["unchained_rows"]),
              "statement rows carry no running balance: the chain cannot vouch for them, so cash they moved is unconfirmed", stmt["unchained_rows"])
        anchor_bad: List[str] = []
        if s0 is not None and s1 is not None:
            for coin, field_, tol_c in (("USDT", "usdt", 0.01), ("BTC", "btc", 1e-8)):
                ch = chains.get(coin)
                if ch and ch["start"] is not None:
                    if abs(ch["start"] - getattr(s0, field_)) > max(tol_c, 1e-9 * abs(ch["start"])):
                        anchor_bad.append(f"{coin} opening: the statement starts from {ch['start']:.8f}, snapshot {s0.snapshot_id} says {getattr(s0, field_):.8f}")
                    if abs(ch["end"] - getattr(s1, field_)) > max(tol_c, 1e-9 * abs(ch["end"])):
                        anchor_bad.append(f"{coin} closing: the statement ends at {ch['end']:.8f}, snapshot {s1.snapshot_id} says {getattr(s1, field_):.8f}")
        _flag(flags, "STATEMENT_NOT_ANCHORED", SEVERITY_GAP, len(anchor_bad),
              "the statement's balances do not tie to the snapshots at the period's ends: a self-consistent chain that starts or ends elsewhere vouches for nothing", anchor_bad)
        usdt_chain = chains.get("USDT")
        stmt["anchored"] = bool(usdt_chain and usdt_chain["start"] is not None and not usdt_chain["breaks"] and not usdt_chain["unchained"]
                                and not [m for m in anchor_bad if m.startswith("USDT")])
        fee_lines = math.fsum(-l.change for l in lines if l.category in ("FEE", "REBATE") and l.coin in USD_COINS)     # a rebate line nets against fees
        fund_lines = math.fsum(l.change for l in lines if l.category == "FUNDING" and l.coin in USD_COINS)
        stmt["fee_total"], stmt["funding_total"] = fee_lines, fund_lines
        tol = lambda ref: max(0.01, 1e-6 * abs(ref))                                                     # noqa: E731
        # A spot fee in USDT is either inside the SPOT line's change (the venue's own log: change = cashFlow - fee) or on a separate FEE line. Either layout is
        # accepted, but ONE layout must explain both the fee total and the spot cash.
        stmt_spot_usd = math.fsum(l.change for l in lines if l.category == "SPOT" and l.coin in USD_COINS)
        stmt_spot_btc = math.fsum(l.change for l in lines if l.category == "SPOT" and l.coin == "BTC")
        spot_net = spot_sell_cash - spot_buy_cash
        tol_fee, tol_spot = tol(perp_fees + spot_usd_fee), tol(spot_buy_cash + spot_sell_cash)
        fee_a, spot_a = abs(fee_lines - perp_fees) <= tol_fee, abs(stmt_spot_usd - (spot_net - spot_usd_fee)) <= tol_spot      # fee inside the SPOT line
        fee_b, spot_b = abs(fee_lines - (perp_fees + spot_usd_fee)) <= tol_fee, abs(stmt_spot_usd - spot_net) <= tol_spot       # fee on its own FEE line
        layout_ok = (fee_a and spot_a) or (fee_b and spot_b)
        if fills and not (fee_a or fee_b):
            _flag(flags, "EXECUTION_NOT_IN_STATEMENT", SEVERITY_GAP, 1,
                  f"fees in the statement ({fee_lines:.6f}) differ from the executions' fees ({perp_fees:.6f}, or {perp_fees + spot_usd_fee:.6f} with the spot USDT fee): an "
                  "execution is missing from one of the two, or a fee is mis-signed or mis-valued")
        elif fills and not layout_ok and (spot_a or spot_b):
            _flag(flags, "EXECUTION_NOT_IN_STATEMENT", SEVERITY_GAP, 1,
                  "the statement's fees and its spot cash are explained by different layouts of the spot USDT fee (inside the SPOT line vs on its own FEE line): "
                  "one of the two is missing or double counted")
        if funding and abs(fund_lines - funding_net) > tol(funding_net):
            _flag(flags, "FUNDING_NOT_IN_STATEMENT", SEVERITY_GAP, 1,
                  f"funding in the statement ({fund_lines:.6f}) differs from the settlements' payments ({funding_net:.6f}): a settlement is missing from one of the two")
        elif not funding and abs(fund_lines) > tol(0.0):
            _flag(flags, "FUNDING_NOT_IN_STATEMENT", SEVERITY_GAP, 1,
                  f"the statement shows {fund_lines:.6f} of funding but no settlement record was supplied: cash with no record behind it")
        stmt_ext = math.fsum(l.change for l in lines if l.category == "EXTERNAL" and l.coin in USD_COINS)
        if abs(stmt_ext - usdt_ext) > tol(usdt_ext):
            _flag(flags, "FLOW_NOT_IN_STATEMENT", SEVERITY_GAP, 1,
                  f"deposits/withdrawals in the statement ({stmt_ext:.6f} USDT) differ from the flow records ({usdt_ext:.6f}): an unattributed cash movement, or a flow "
                  "record with no statement line")
        stmt_trade = math.fsum(l.change for l in lines if l.category == "TRADE" and l.coin in USD_COINS)
        perp_real_for_check = perp["realised_usd"] if perp else 0.0
        if abs(stmt_trade - perp_real_for_check) > tol(perp_real_for_check):
            _flag(flags, "REALISED_NOT_IN_STATEMENT", SEVERITY_GAP, 1,
                  f"realised cash flow in the statement ({stmt_trade:.6f}) differs from the realised P&L the executions imply under average-entry accounting "
                  f"({perp_real_for_check:.6f}): an execution is missing, or the venue's cost basis differs from the convention assumed here")
        if not (spot_a or spot_b) or abs(stmt_spot_btc - spot_btc_delta) * (spot[0].price if spot else 1.0) > tol_spot:
            _flag(flags, "SPOT_NOT_IN_STATEMENT", SEVERITY_GAP, 1,
                  f"spot cash in the statement (USDT {stmt_spot_usd:.6f}, BTC {stmt_spot_btc:.8f}) differs from the spot trades' effect "
                  f"(USDT {spot_sell_cash - spot_buy_cash - spot_usd_fee:.6f}, BTC {spot_btc_delta:.8f})")
        other_lines = [l.line_id for l in lines if l.category == "OTHER"]
        _flag(flags, "OTHER_CASH_LINES_PRESENT", SEVERITY_GAP, len(other_lines),
              "statement lines the supplier called OTHER: cash that is neither a fee, a settlement, a trade, interest nor a flow is unattributed, and is counted in "
              "the bridge as operating cost/income only so that it cannot hide", other_lines)
        pos_interest = [l.line_id for l in lines if l.category == "INTEREST" and l.change > 0]
        _flag(flags, "INTEREST_UNEXPECTED", SEVERITY_GAP, len(pos_interest),
              "a positive INTEREST line: interest on a borrow is a cost, so a credit labelled interest is unexplained income", pos_interest)
        btc_unmodelled = [l.line_id for l in lines if l.coin == "BTC" and l.category not in ("SPOT", "EXTERNAL")]
        _flag(flags, "BTC_LINES_UNMODELLED", SEVERITY_GAP, len(btc_unmodelled),
              "BTC statement lines other than spot trades and transfers (fees, interest, funding...) enter no model: their effect is unexplained", btc_unmodelled)
        internal_unbalanced = [c for c in COINS if abs(math.fsum(l.change for l in lines if l.category == "INTERNAL" and l.coin == c)) > 1e-9]
        _flag(flags, "INTERNAL_LINES_UNBALANCED", SEVERITY_GAP, len(internal_unbalanced),
              "INTERNAL statement lines must net to zero per currency: a net movement is money that crossed the account's boundary unrecorded", internal_unbalanced)

    # ---- the closing components rebuilt from the opening snapshot and the records, against the closing snapshot (and every snapshot between)
    bridge: Optional[Dict[str, Any]] = None
    residuals: Dict[str, Any] = {}
    mid_bad: List[str] = []
    bridge_unexplained_big = False
    eq_breach = part_breach = False
    res_big: Dict[str, float] = {}
    if s0 is not None and s1 is not None:
        def components_at(sn: Snapshot) -> Dict[str, float]:
            T = sn.ts_ms
            f_ = [x for x in fills if x.ts_ms <= T]
            try:
                pp = perp_average_entry(f_, opening_qty, opening_avg)
            except RecordError:
                pp = None
            sp_ = [x for x in spot if x.ts_ms <= T]
            ex_ = [x for x in external_only if x.ts_ms <= T]
            ln_ = [x for x in lines if x.ts_ms <= T]
            usdt_m = (s0.usdt + math.fsum(x.amount for x in ex_ if x.coin in USD_COINS) + (pp["realised_usd"] if pp else 0.0) - math.fsum(x.fee_usd for x in f_)
                      + math.fsum(x.payment_usd for x in funding if x.grid_ms <= T)
                      + math.fsum(x.change for x in ln_ if x.category in ("INTEREST", "OTHER") and x.coin in USD_COINS)
                      - math.fsum(x.notional for x in sp_ if x.side == "Buy") + math.fsum(x.notional for x in sp_ if x.side == "Sell") - math.fsum(x.usd_fee for x in sp_))
            btc_m = s0.btc + math.fsum(x.amount for x in ex_ if x.coin == "BTC") + math.fsum(x.qty if x.side == "Buy" else -x.qty for x in sp_) - math.fsum(x.btc_fee for x in sp_)
            q_m = pp["qty"] if pp else opening_qty
            a_m = pp["avg_entry"] if pp else (opening_avg or 0.0)
            upl_m = q_m * (sn.mark - a_m) if q_m != 0 else 0.0
            eq_m = usdt_m + btc_m * sn.btc_price + upl_m + sn.other_usd
            return {"usdt": usdt_m, "btc": btc_m, "qty": q_m, "upl": upl_m, "equity": eq_m}

        c1 = components_at(s1)
        qty_model = c1["qty"]
        residuals = {"usdt": s1.usdt - c1["usdt"], "btc_qty": s1.btc - c1["btc"], "perp_qty": (s1.perp_qty - qty_model) if s1.perp_qty is not None else None,
                     "perp_unrealised_usd": s1.perp_upl_usd - c1["upl"], "equity_usd": s1.components_equity - c1["equity"]}
        tol_eq = max(residual_tolerance_usd, residual_tolerance_rel * max(abs(s0.components_equity), abs(s1.components_equity)))
        # With a statement whose USDT chain is intact and anchored to both snapshots, the cash is independently known to the cent, so a cash residual
        # that a tolerance scaled to the WHOLE equity (the owned BTC included) would hide is not allowed to hide.
        gross = math.fsum(abs(l.change) for l in lines if l.coin in USD_COINS) + math.fsum(abs(f.amount) for f in external_only if f.coin in USD_COINS)
        cash_tol = max(0.01, 1e-6 * gross) if stmt["anchored"] else tol_eq
        residuals["tolerance_usd"] = tol_eq
        residuals["cash_tolerance_usd"] = cash_tol
        btc_unpriced = bool(unpriced_btc)
        btc_pnl_invested = spot_buy_cash + spot_usd_fee - spot_sell_cash + btc_ext_usd         # (a fee paid in BTC leaves with the BTC: it is in V1)
        v0, v1 = s0.btc * s0.btc_price, s1.btc * s1.btc_price
        btc_economic = None if btc_unpriced else v1 - v0 - btc_pnl_invested
        perp_real = perp["realised_usd"] if perp else 0.0
        d_upl = s1.perp_upl_usd - s0.perp_upl_usd
        d_other = s1.other_usd - s0.other_usd
        explained = None if btc_unpriced else ext_usd + perp_real - perp_fees + funding_net + interest_usd + other_usd_lines + d_upl + d_other + btc_economic
        d_equity = s1.components_equity - s0.components_equity
        unexplained = None if explained is None else d_equity - explained
        bridge = {
            "opening_equity_usd": s0.components_equity, "closing_equity_usd": s1.components_equity, "change_in_equity_usd": d_equity,
            "external_flows_usd": ext_usd, "external_flows_complete": not btc_unpriced, "perp_realised_usd": perp_real, "perp_fees_usd": -perp_fees, "funding_net_usd": funding_net,
            "interest_usd": interest_usd, "other_cash_lines_usd": other_usd_lines, "change_in_perp_unrealised_usd": d_upl,
            "change_in_other_assets_usd": d_other,
            "btc_economic_pnl_usd": btc_economic, "explained_change_usd": explained, "unexplained_usd": unexplained,
            "btc_flows_unpriced": btc_unpriced,
            "btc_leg_price_sensitivity_usd_per_1pct": 0.01 * (abs(v0) + abs(v1)),
            "reading": "deposits and withdrawals are external_flows_usd and are NOT profit; perp_realised is cash; change_in_perp_unrealised is NOT cash; "
                       "change_in_other_assets_usd is value the ledger does not explain; btc_economic_pnl is the price move on the BTC held plus the cost of trading "
                       "it, net of BTC flows valued when they happened (withheld while any BTC flow is unpriced)",
        }
        res_big = {k: v for k, v in residuals.items() if k not in ("tolerance_usd", "cash_tolerance_usd") and isinstance(v, float) and math.isfinite(v)}
        eq_breach = abs(residuals["equity_usd"]) > tol_eq
        part_breach = abs(residuals["usdt"]) > cash_tol or abs(residuals["btc_qty"]) * s1.btc_price > cash_tol or abs(residuals["perp_unrealised_usd"]) > tol_eq \
            or (residuals["perp_qty"] is not None and abs(residuals["perp_qty"]) > AR.POSITION_ABS_TOL)
        bridge_unexplained_big = unexplained is not None and abs(unexplained) > tol_eq
        for sn in snaps[1:-1]:
            cm = components_at(sn)
            if (abs(sn.usdt - cm["usdt"]) > cash_tol or abs(sn.btc - cm["btc"]) * sn.btc_price > cash_tol or abs(sn.perp_upl_usd - cm["upl"]) > tol_eq
                    or abs(sn.components_equity - cm["equity"]) > tol_eq):
                mid_bad.append(f"{sn.snapshot_id}: usdt {sn.usdt - cm['usdt']:+.4f}, btc {sn.btc - cm['btc']:+.8f}, equity {sn.components_equity - cm['equity']:+.4f}")
        # Every gap above is final. A residual is a contradiction between two complete records only if nothing is still open; otherwise it is an
        # incomplete history. (Default closed: any gap not listed as a mere check keeps the coverage open.)
        coverage_ok = not any(f["severity"] == SEVERITY_GAP and f["code"] not in CHECK_GAPS for f in flags)
        sev = SEVERITY_GAP if not coverage_ok else SEVERITY_HARD
        verdict_text = ("Coverage gaps are open, so this is an incomplete history rather than a contradiction." if not coverage_ok else
                        "Nothing is open in the coverage checks, so the records contradict the balances.")
        if eq_breach or part_breach:
            _flag(flags, "EQUITY_RESIDUAL", sev, 1,
                  "the closing snapshot differs from what the opening snapshot and the records imply: " +
                  ", ".join(f"{k} {v:+.6f}" for k, v in res_big.items()) + f" (tolerance {tol_eq:.2f} USD, cash {cash_tol:.2f}). " + verdict_text)
        if bridge_unexplained_big:
            _flag(flags, "BRIDGE_UNEXPLAINED", sev, 1,
                  f"the change in equity is not explained by the bridge's lines: {unexplained:+.6f} USD is left over (tolerance {tol_eq:.2f}). " + verdict_text)
        _flag(flags, "MIDDLE_SNAPSHOT_UNRECONCILED", sev, len(mid_bad),
              "a snapshot between the ends disagrees with what the records imply at its own instant: it cannot be used to average capital or to chain returns", mid_bad)
        if s1.perp_qty is None and perp is not None and perp["qty"] != 0:
            _flag(flags, "OPENING_POSITION_UNKNOWN", SEVERITY_GAP, 1, "the closing snapshot does not state the perpetual position while the executions leave one open")

    # ---- returns, strategy contribution, economic capital
    window_days = (t1 - t0) / DAY_MS if (t0 is not None and t1 is not None) else None
    returns: Dict[str, Any] = {"status": "NOT_COMPUTED"}
    if s0 is not None and s1 is not None and bridge is not None and unpriced_btc:
        returns = {"status": "WITHHELD_UNPRICED_BTC_FLOW", "why": "a BTC deposit/withdrawal has no USD value, so the capital contributed is unknown and any return would be invented",
                   "alternative_return_pct": alternative_return_pct if alternative_return_pct is not None else "UNSTATED (owner input)"}
    elif s0 is not None and s1 is not None and bridge is not None:
        flows_ts = sorted(usd_flow_by_ts)
        dietz = modified_dietz(s0.components_equity, s1.components_equity, flows_ts, t0, t1)
        returns = {"status": "COMPUTED", "account_money_weighted": dietz,
                   "account_irr": irr(s0.components_equity, s1.components_equity, flows_ts, t0, t1) or {"method": "IRR", "annual_rate": None, "why_not": "no root in [-99 %, +5000 %] a year"},
                   "account_linked": linked_returns([x for x in snaps if x.snapshot_id not in {m.split(":", 1)[0] for m in mid_bad}], flows_ts),
                   "time_weighted_exact": "UNAVAILABLE: needs the account value immediately before each external flow",
                   "alternative_return_pct": alternative_return_pct if alternative_return_pct is not None else "UNSTATED (owner input)"}
    strategy: Dict[str, Any] = {"status": "NOT_COMPUTED"}
    if perp_rep is not None:
        limits = [{"code": f["code"], "count": f["count"], "detail": f["detail"]} for f in perp_rep["flags"] if f["code"] in STRATEGY_LIMIT_CODES]
        eco = perp_rep["economics"]
        funding_all, fees_all = funding_net, perp_fees
        if eco.get("tag") == AR.PROVENANCE:
            strategy = {"status": "COMPUTED", "complete_round_trips": eco["complete_round_trips"], "net_usd": eco["net_usd"], "funding_usd": eco["funding_usd"],
                        "fees_usd": -eco["fees_usd"], "basis_pnl_usd": eco["basis_pnl_usd"],
                        "funding_coverage": (eco["funding_usd"] / funding_all) if funding_all else None,
                        "fee_coverage": (eco["fees_usd"] / fees_all) if fees_all else None,
                        "reading": "exact for the COMPLETE round trips only (a spot reference on every fill); the account-level bridge above covers everything",
                        "limitations": limits}
        else:
            strategy = {"status": "UNKNOWN", "reason": eco.get("reading", "no complete round trip with a basis"), "limitations": limits}
    capital: Dict[str, Any] = {"status": "NOT_COMPUTED"}
    if s0 is not None and s1 is not None:
        pts = []
        bad_ids = {m.split(":", 1)[0] for m in mid_bad}                    # a middle snapshot that does not reconcile is not used to average capital or to chain returns
        for s in snaps:
            if s.ts_ms < t0 or s.ts_ms > t1 or s.snapshot_id in bad_ids:
                continue
            btc_v = s.btc * s.btc_price
            pts.append((s.ts_ms, btc_v + (s.margin_posted_usd or 0.0) + (s.reserve_usd or 0.0), s.margin_posted_usd is not None and s.reserve_usd is not None))
        area = math.fsum((b[1] + a[1]) / 2.0 * (b[0] - a[0]) for a, b in zip(pts, pts[1:]))
        avg_capital = area / (pts[-1][0] - pts[0][0]) if len(pts) >= 2 and pts[-1][0] > pts[0][0] else None
        if not all(p[2] for p in pts):
            _flag(flags, "CAPITAL_COMPONENTS_UNSTATED", "INFO", 1,
                  "margin_posted_usd / reserve_usd are not stated on every snapshot: the economic capital is the BTC value alone and the return on it is an upper bound")
        if len(pts) == 2:
            _flag(flags, "TWO_POINT_CAPITAL_AVERAGE", "INFO", 1, "only two snapshots: the average capital is the mean of two instants, not a time integral")
        capital = {"status": "COMPUTED", "average_economic_capital_usd": avg_capital, "snapshots_used": len(pts),
                   "definition": "BTC inventory value + margin posted beyond it + reserve, trapezoid-averaged over the snapshots"}
        if avg_capital and avg_capital > 0 and window_days and window_days > 0:
            # with a BTC flow of unknown value the contribution is unknown, so the account's own return is not stated (the deposit would read as profit)
            capital["account_return_on_capital_period"] = ((bridge["change_in_equity_usd"] - ext_usd) / avg_capital) if (bridge and not unpriced_btc) else None
            if unpriced_btc:
                capital["account_return_withheld"] = "a BTC deposit/withdrawal has no USD value"
            if strategy.get("status") == "COMPUTED":
                capital["strategy_return_on_capital_period"] = strategy["net_usd"] / avg_capital
                ann, why = annualise(strategy["net_usd"] / avg_capital, window_days)
                capital["strategy_return_on_capital_annualised_simple_pct"] = ann
                if why:
                    capital["annualised_why_not"] = why
        if perp is not None and fills:
            capital["return_on_hedged_notional"] = _hedged_notional_return(fills, funding, perp_rep, window_days, t0, t1, opening_qty, strategy)

    # ---- the verdict
    no_live = not fills and not funding
    gap_codes = {f["code"] for f in flags if f["severity"] == SEVERITY_GAP}
    hard = [f for f in flags if f["severity"] == SEVERITY_HARD]
    if no_live and not hard:
        verdict = "NO_TRADING_RECORDS"
    elif hard:
        verdict = "REJECTED_RECORDS_CONTRADICT"
    elif gap_codes - set(CHECK_GAPS):
        verdict = "INCOMPLETE"
    elif gap_codes & set(CHECK_GAPS):
        verdict = "RECONCILED_BUT_UNCHECKED"
    else:
        verdict = "RECONCILED_NOT_AUTHENTICATED"
    conclusions = {
        "NO_TRADING_RECORDS": "no perpetual execution or settlement is in the window: realised proprietary trading P&L has NOT been established (paper and testnet "
                              "records are not live evidence)",
        "REJECTED_RECORDS_CONTRADICT": "the records contradict each other or the balances: no result is stated",
        "INCOMPLETE": "part of the history is unexplained: no positive conclusion is drawn; the figures shown are what the records contain",
        "RECONCILED_BUT_UNCHECKED": "the records reconcile to the balances but some checks could not run (against the public market, an assumed fee currency, dropped "
                                    "duplicate records or unchecked snapshot/flow prices: see the flags)",
        "RECONCILED_NOT_AUTHENTICATED": "the records reconcile to the balances within tolerance; they are the owner's files and have not been authenticated against the exchange",
    }[verdict]
    return {
        "provenance": PROVENANCE, "verdict": verdict, "conclusion": conclusions, "authorizes_trading": False, "changes_any_status": False,
        "window": {"opening_ts_ms": t0, "closing_ts_ms": t1, "opening": AR._iso(t0) if t0 else None, "closing": AR._iso(t1) if t1 else None,
                   "days": window_days, "boundary_rule": "(opening snapshot, closing snapshot]"},
        "records": {"perp_executions": len(fills), "funding_settlements": len(funding), "spot_trades": len(spot), "external_flows": len(ext),
                    "statement_lines": len(lines), "snapshots": len(snaps)},
        "equity_bridge": bridge, "residuals": residuals, "statement": stmt, "returns": returns, "strategy_contribution": strategy,
        "economic_capital": capital, "perp_review": ({"verdict": perp_rep["verdict"], "round_trips": perp_rep["round_trips"], "settlements": perp_rep["settlements"],
                                                      "sufficiency": perp_rep["sufficiency"]} if perp_rep else None),
        "perp_position": perp, "flags": flags, "limits": list(LIMITS),
        "custody": {"inputs_sha256": dict(inp.digests), "ignored_columns": {k: list(v) for k, v in inp.ignored.items()},
                    "adapters": {a: BV5.DOC_BASIS[a] for a in inp.adapters_used}, "undocumented_by_the_venue": list(BV5.UNDOCUMENTED) if inp.adapters_used else [],
                    "authenticity": "UNVERIFIED_BY_DESIGN: a digest proves the bytes are unchanged since ingestion, not that the exchange produced them",
                    "how_this_could_be_verified": "only through a separately authorised mechanism (an account-side read by the repository's own ledger tooling "
                                                  "or an independent statement from the venue); nothing here holds credentials or calls the venue"},
    }


def _hedged_notional_return(fills, funding, perp_rep, window_days, t0, t1, opening_qty, strategy) -> Dict[str, Any]:
    """Average |short perpetual notional| over the window (piecewise constant between fills, priced at the last fill), and the strategy's net
    over it. An approximation of the notional the funding was earned on; it says so."""
    evs = sorted(fills, key=lambda f: (f.ts_ms, f.exec_id))
    q, last_p, last_t = opening_qty, (evs[0].price if evs else None), t0
    area = 0.0
    for f in evs:
        if last_p is not None:
            area += abs(min(q, 0.0)) * last_p * (f.ts_ms - last_t)
        q += f.signed_qty
        last_p, last_t = f.price, f.ts_ms
    if last_p is not None:
        area += abs(min(q, 0.0)) * last_p * (t1 - last_t)
    avg = area / (t1 - t0) if t1 > t0 else None
    out = {"average_short_notional_usd": avg, "priced_at": "the most recent fill price (approximation)"}
    if avg and avg > 0 and strategy.get("status") == "COMPUTED" and window_days:
        out["strategy_return_period"] = strategy["net_usd"] / avg
        ann, why = annualise(strategy["net_usd"] / avg, window_days)
        out["strategy_return_annualised_simple_pct"] = ann
        if why:
            out["annualised_why_not"] = why
    return out


# ---------------------------------------------------------------- loading from a manifest (local files only)

FAMILIES = ("perp_executions", "funding", "spot_trades", "external_flows", "cash_lines", "snapshots") + BV5.ADAPTERS
TRANSFORMS = ("negate", "abs", "epoch_s_to_ms", "iso_utc_to_ms")


def _transform(value: Any, spec: str, where: str) -> Any:
    if spec == "negate":
        return -_num(value, "value", where)
    if spec == "abs":
        return abs(_num(value, "value", where))
    if spec == "epoch_s_to_ms":
        return int(round(_num(value, "value", where) * 1000))
    if spec == "iso_utc_to_ms":
        import datetime
        s = _str(value, "value", where)
        if not (s.endswith("Z") or s.endswith("+00:00")):
            raise RecordError(f"{where}: {s!r} is not an ISO time with an explicit UTC marker (Z or +00:00): a local time is never guessed")
        try:
            dt = datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError as exc:
            raise RecordError(f"{where}: {s!r} is not an ISO-8601 time") from exc
        return int(round(dt.timestamp() * 1000))
    if spec.startswith("scale:"):
        try:
            k = float(spec[6:])
        except ValueError as exc:
            raise RecordError(f"{where}: bad scale {spec!r}") from exc
        if not math.isfinite(k) or k == 0:
            raise RecordError(f"{where}: bad scale {spec!r}")
        return _num(value, "value", where) * k
    if spec.startswith("map:"):
        try:
            table = json.loads(spec[4:])
        except ValueError as exc:
            raise RecordError(f"{where}: bad map {spec!r}") from exc
        key = str(value)
        if not isinstance(table, dict) or key not in table:
            raise RecordError(f"{where}: value {key!r} is not in the declared map: an unknown category blocks the result")
        return table[key]
    raise RecordError(f"{where}: unknown transform {spec!r} (allowed: {', '.join(TRANSFORMS)}, scale:<x>, map:<json>)")


def apply_column_map(rows: Sequence[Mapping[str, Any]], column_map: Mapping[str, Any], constants: Optional[Mapping[str, Any]], label: str) -> List[Dict[str, Any]]:
    """Rename and convert the columns of an exchange export into the canonical columns, exactly as the manifest declares: ``canonical:
    "Source Column"`` or ``canonical: {"column": "Source Column", "transform": "negate"}`` (a LIST of transforms applies in order). Fields not mapped are
    absent, never defaulted; ``constants`` supplies a value the export does not carry (for example the symbol). Deterministic, and each canonical
    row stays traceable to its source row by position."""
    if not isinstance(column_map, Mapping) or not column_map:
        raise RecordError(f"{label}: column_map must be a non-empty object")
    out: List[Dict[str, Any]] = []
    for i, r in enumerate(rows):
        w = f"{label} row {i + 1}"
        row: Dict[str, Any] = dict(constants or {})
        for canon, spec in column_map.items():
            col, tr = (spec, []) if isinstance(spec, str) else (spec.get("column"), spec.get("transform", []))
            if not isinstance(col, str) or col not in r:
                raise RecordError(f"{w}: the mapped source column {col!r} for {canon!r} is not in the file")
            v = r[col]
            for t in ([tr] if isinstance(tr, str) else list(tr)):
                v = _transform(v, t, f"{w} ({canon})")
            row[canon] = v
        out.append(row)
    return out


def load_manifest(manifest_path: str) -> Inputs:
    """Read the files a manifest names, ONLY from the manifest's own directory tree (a manifest cannot point the reader at another part of the disk),
    refuse runtime-state, evidence and credential names, and return the parsed inputs with the sha256 of every file read. Manifest format::

        {"symbol": "BTCUSDT", "taker_bps": 5.5, "maker_bps": 2.0, "files": [
            {"family": "perp_executions", "path": "execs.csv", "column_map": {...}, "constants": {...}}, ... ]}
    """
    real = AR._refuse_protected(manifest_path)
    base = os.path.dirname(real)
    data = AR._read_bytes(real, os.path.basename(real))
    try:
        man = json.loads(data, object_pairs_hook=AR._no_duplicate_keys, parse_constant=AR._bad_constant)
    except (ValueError, RecursionError) as exc:
        raise RecordError(f"the manifest is not valid JSON ({type(exc).__name__})") from exc
    if not isinstance(man, dict) or not isinstance(man.get("files"), list) or not man["files"]:
        raise RecordError("the manifest must be an object with a non-empty 'files' list")
    symbol = man.get("symbol", "BTCUSDT")
    if symbol != "BTCUSDT":
        raise RecordError("only BTCUSDT is supported")
    digests: Dict[str, str] = {"manifest": AR._sha(data)}
    ignored: Dict[str, List[str]] = {}
    fills: List[AR.Fill] = []
    funding: List[AR.FundingRow] = []
    spot: List[SpotTrade] = []
    flows: List[ExternalFlow] = []
    lines: List[CashLine] = []
    snaps: List[Snapshot] = []
    seen_paths = set()
    adapter_flags: List[Dict[str, Any]] = []
    adapters_used: List[str] = []
    for k, ent in enumerate(man["files"]):
        if not isinstance(ent, dict) or ent.get("family") not in FAMILIES or not isinstance(ent.get("path"), str):
            raise RecordError(f"manifest file #{k + 1}: needs a 'family' (one of {', '.join(FAMILIES)}) and a 'path'")
        target = os.path.realpath(os.path.join(base, ent["path"]))
        if os.path.commonpath([base, target]) != base:
            raise RecordError(f"manifest file #{k + 1}: {ent['path']!r} is outside the manifest's directory: refused")
        if target in seen_paths:
            raise RecordError(f"manifest file #{k + 1}: {ent['path']!r} is listed twice")
        seen_paths.add(target)
        rows, dig = AR.read_rows(target)
        label = f"{ent['family']}:{os.path.basename(target)}"
        digests[label] = dig
        if "column_map" in ent:
            rows = apply_column_map(rows, ent["column_map"], ent.get("constants"), label)
        elif "constants" in ent:
            rows = [dict(ent["constants"], **r) for r in rows]
        fam = ent["family"]
        if fam in BV5.ADAPTERS:
            adapters_used.append(fam)
            if fam == "bybit_v5_transaction_log":
                rec = BV5.transaction_log_to_records(rows, label, symbol)
                adapter_flags += rec["notes"]
                if rec["funding"]:
                    f2, ign = AR.parse_funding(rec["funding"], symbol)
                    funding += f2
                    ignored.setdefault("funding", []).extend(ign)
                if rec["cash_lines"]:
                    s3, ign = parse_cash_lines(rec["cash_lines"], label)
                    lines += s3
                    ignored.setdefault("cash_lines", []).extend(ign)
                if rec["external_flows"]:
                    s2, ign = parse_external_flows(rec["external_flows"], label)
                    flows += s2
                    ignored.setdefault("external_flows", []).extend(ign)
                ignored.setdefault(fam, []).extend(sorted({str(c) for r in rows for c in r} - set(BV5.TXLOG_USED)))
                continue
            raw_cols = {str(c) for r in rows for c in r}
            if fam == "bybit_v5_execution":
                used = BV5.EXECUTION_USED
                rows, nts = BV5.execution_to_fills(rows, label, symbol)
                fam_src = "perp_executions"
            else:
                used = BV5.SPOT_USED
                rows, nts = BV5.spot_execution_to_trades(rows, label, symbol)
                fam_src = "spot_trades"
            adapter_flags += nts
            ignored.setdefault(ent["family"], []).extend(sorted(raw_cols - set(used)))
            fam = fam_src
        if fam == "perp_executions":
            for i, r in enumerate(rows):
                cur = r.get("fee_currency")
                if cur is not None and str(cur).strip().upper() not in ("USDT", "USD"):
                    raise RecordError(f"{label} row {i + 1}: fee_currency {cur!r} is not USDT: a linear BTCUSDT perpetual is settled in USDT, so another currency is a mapping error")
            f, ign = AR.parse_fills(rows, symbol)
            fills += f
            ignored.setdefault(fam, []).extend(ign)
        elif fam == "funding":
            for i, r in enumerate(rows):
                cur = r.get("currency")
                if cur is not None and str(cur).strip().upper() not in ("USDT", "USD"):
                    raise RecordError(f"{label} row {i + 1}: currency {cur!r} is not USDT")
            f2, ign = AR.parse_funding(rows, symbol)
            funding += f2
            ignored.setdefault(fam, []).extend(ign)
        elif fam == "spot_trades":
            s, ign = parse_spot_trades(rows, label, symbol)
            spot += s
            ignored.setdefault(fam, []).extend(ign)
        elif fam == "external_flows":
            s2, ign = parse_external_flows(rows, label)
            flows += s2
            ignored.setdefault(fam, []).extend(ign)
        elif fam == "cash_lines":
            s3, ign = parse_cash_lines(rows, label)
            lines += s3
            ignored.setdefault(fam, []).extend(ign)
        else:
            s4, ign = parse_snapshots(rows, label)
            snaps += s4
            ignored.setdefault(fam, []).extend(ign)
    snaps.sort(key=lambda s: (s.ts_ms, s.snapshot_id))
    kw, hist_digests = AR.load_histories(symbol, man.get("public_funding"), man.get("perp_4h"), man.get("spot_4h"), True) if not man.get("no_public_history") \
        else ({"public": None, "bar_open": None, "bar_range": None, "spot_range": None, "spot_open": None, "caller_histories": []}, {})
    digests.update(hist_digests)
    tb, mb = man.get("taker_bps"), man.get("maker_bps")
    return Inputs(fills, funding, spot, flows, lines, snaps, digests, {k: sorted(set(v)) for k, v in ignored.items()}, kw, tb, mb,
                  tuple(adapter_flags), tuple(sorted(set(adapters_used))))


# ---------------------------------------------------------------- rendering and command line

def render(rep: Mapping[str, Any]) -> str:
    L: List[str] = []
    a = L.append
    a(f"OWNER P&L LEDGER   provenance {rep['provenance']}   verdict {rep['verdict']}")
    a(f"  {rep['conclusion']}")
    a("  authorizes trading: NO.  changes any engine status: NO.  figures are the supplier's, cross-checked, not authenticated.")
    w = rep["window"]
    a(f"  window {w['opening']} .. {w['closing']}  ({w['days']:.1f} days; {w['boundary_rule']})" if w["days"] else "  window: not determined")
    a("  records: " + ", ".join(f"{k} {v}" for k, v in rep["records"].items()))
    b = rep["equity_bridge"]
    if b:
        a("  EQUITY BRIDGE (USD)")
        for k in ("opening_equity_usd", "external_flows_usd", "perp_realised_usd", "perp_fees_usd", "funding_net_usd", "interest_usd", "other_cash_lines_usd",
                  "change_in_perp_unrealised_usd", "change_in_other_assets_usd", "btc_economic_pnl_usd", "unexplained_usd", "closing_equity_usd"):
            a(f"    {k:32s} " + (f"{b[k]:>16,.2f}" if b[k] is not None else f"{'WITHHELD (unpriced BTC flow)':>16s}") + ("   PARTIAL: an unpriced BTC flow is not in this figure" if k == "external_flows_usd" and not b.get("external_flows_complete", True) else ""))
        a("    " + b["reading"])
        res = rep["residuals"]
        a("  RESIDUALS (closing snapshot minus what the records imply): " + ", ".join(f"{k} {v:+.4f}" for k, v in res.items() if isinstance(v, float) and math.isfinite(v)))
    s = rep["strategy_contribution"]
    a(f"  STRATEGY (complete round trips): {s['status']}" + (f"  net {s['net_usd']:.2f} = funding {s['funding_usd']:.2f} + basis {s['basis_pnl_usd']:.2f} + fees {s['fees_usd']:.2f};"
                                                         f" funding coverage {s['funding_coverage']}" if s["status"] == "COMPUTED" else f"  {s.get('reason', '')}"))
    r = rep["returns"]
    if r.get("status") == "COMPUTED":
        d = r["account_money_weighted"]
        irr_ = r.get("account_irr") or {}
        a("  RETURNS: " + (f"modified Dietz {d['period_return'] * 100:.4f} % over {d['days']:.1f} days (capital base {d['capital_base_usd']:,.0f}"
                           + (", base small: not a meaningful rate" if d.get("base_small") else "") + ")" if d else "money-weighted: not computable")
          + (f"; IRR {irr_['annual_rate'] * 100:.2f} %/yr" if irr_.get("annual_rate") is not None else f"; no annual rate ({irr_.get('why_not', 'n/a')})")
          + f"; alternative return {r['alternative_return_pct']}")
    elif str(r.get("status", "")).startswith("WITHHELD"):
        a(f"  RETURNS: withheld. {r['why']}")
    c = rep["economic_capital"]
    if c.get("status") == "COMPUTED":
        a(f"  ECONOMIC CAPITAL (average): {c['average_economic_capital_usd']}")
        for k in ("strategy_return_on_capital_period", "strategy_return_on_capital_annualised_simple_pct"):
            if k in c:
                a(f"    {k}: {c[k]}")
    a("  FLAGS")
    for f in rep["flags"] or []:
        a(f"    [{f['severity']}] {f['code']} x{f['count']}: {f['detail']}" + (f"  e.g. {', '.join(map(str, f['examples']))}" if f["examples"] else ""))
    if not rep["flags"]:
        a("    none")
    a("  WHAT THIS DOES NOT ESTABLISH")
    for lim in rep.get("limits", []):
        a("    - " + lim)
    a("  CUSTODY: " + rep["custody"]["authenticity"])
    for k, v in rep["custody"]["inputs_sha256"].items():
        a(f"    sha256 {k:40s} {v}")
    return "\n".join(L)


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Local-only owner P&L ledger (no network, no orders, writes nothing).")
    ap.add_argument("--manifest", required=True)
    ap.add_argument("--alternative-return-pct", type=float, default=None, help="your own alternative return, %/yr (a stated owner input)")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    try:
        inp = load_manifest(a.manifest)
        rep = build(inp, alternative_return_pct=a.alternative_return_pct)
        out = json.dumps(rep, indent=2, sort_keys=True, allow_nan=False, default=str) if a.json else render(rep)
    except (RecordError, AR.CT.DataError, OSError, ValueError, OverflowError, ZeroDivisionError, KeyError, TypeError) as exc:
        print(f"owner_ledger: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
