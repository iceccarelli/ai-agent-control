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
DAY_MS = AR.DAY_MS
YEAR_DAYS = 365.0
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
STRUCTURAL_GAPS = ("NO_OPENING_SNAPSHOT", "NO_CLOSING_SNAPSHOT", "OPENING_POSITION_UNKNOWN", "UNPAIRED_INTERNAL_TRANSFER", "UNCLASSIFIED_CASH_LINE",
                   "STATEMENT_GAP", "EXECUTION_NOT_IN_STATEMENT", "FUNDING_NOT_IN_STATEMENT", "UNPRICED_BTC_FLOW", "UNPRICED_BTC_FEE",
                   "NO_STATEMENT", "PERP_RECORDS_INCOMPLETE", "SPOT_INVENTORY_UNEXPLAINED", "RECORDS_OUTSIDE_WINDOW")
#: Gaps where the records agree with each other but could not be compared with the public market.
CHECK_GAPS = ("PERP_CHECKS_UNRUN", "FEE_CURRENCY_NOT_STATED", "DUPLICATE_IDENTICAL_DROPPED", "CAPITAL_COMPONENTS_UNSTATED", "TWO_POINT_CAPITAL_AVERAGE")


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
    account). None when the denominator is not positive (an account that starts empty and is funded mid-period needs the flows as capital)."""
    if t1 <= t0:
        return None
    big_t = float(t1 - t0)
    f_total = math.fsum(f for _t, f in flows)
    denom = e0 + math.fsum(((t1 - t) / big_t) * f for t, f in flows)
    if not denom > 0:
        return None
    r = (e1 - e0 - f_total) / denom
    years = _years(t0, t1)
    return {"method": "MODIFIED_DIETZ", "period_return": r, "capital_base_usd": denom, "days": big_t / DAY_MS,
            "annualised_simple_pct": r / years * 100.0 if years > 0 else None}


def irr(e0: float, e1: float, flows: Sequence[Tuple[int, float]], t0: int, t1: int) -> Optional[Dict[str, Any]]:
    """The annual internal rate of return that equates the opening balance plus contributions with the closing balance plus withdrawals.
    Bisection on NPV; None when there is no sign change (no unique answer) or the period is shorter than a day."""
    if t1 - t0 < DAY_MS:
        return None
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
    return {"method": "IRR", "annual_rate": (lo + hi) / 2.0}


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
    years = _years(snaps[0].ts_ms, snaps[-1].ts_ms)
    return {"method": "LINKED_MODIFIED_DIETZ", "period_return": growth - 1.0, "sub_periods": len(parts), "annualised_simple_pct":
            (growth - 1.0) / years * 100.0 if years > 0 else None, "note": "time-weighted only if every external flow sits at a snapshot time"}


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


def _chain_breaks(lines: Sequence[CashLine], coin: str) -> List[str]:
    """Where the running balance of one currency does not follow from the changes. Lines that share a ``row_id`` are one venue row (their changes add up
    and exactly one of them carries the balance); rows stamped in the same millisecond have no recoverable order, so every order of such a group
    (up to 6 rows; the order given beyond that) is tried and the group passes if ONE order chains from the previous balance. A break is reported
    between the last good row and the first row that cannot follow."""
    steps: Dict[str, Dict[str, Any]] = {}
    for l in lines:
        st = steps.setdefault(l.row_id or l.line_id, {"id": l.row_id or l.line_id, "ts": l.ts_ms, "change": 0.0, "bal": None, "n_bal": 0, "src": l.src})
        st["change"] += l.change
        st["ts"] = max(st["ts"], l.ts_ms)
        if l.cash_balance is not None:
            st["bal"] = l.cash_balance
            st["n_bal"] += 1
    ordered = sorted((v for v in steps.values() if v["n_bal"] == 1), key=lambda v: (v["ts"], v["src"]))
    breaks: List[str] = [f"{coin}:{v['id']}:more_than_one_balance_in_a_row" for v in steps.values() if v["n_bal"] > 1]
    groups: List[List[Dict[str, Any]]] = []
    for v in ordered:
        if groups and groups[-1][0]["ts"] == v["ts"]:
            groups[-1].append(v)
        else:
            groups.append([v])

    def follows(prev_bal: Optional[float], a: Dict[str, Any]) -> bool:
        return prev_bal is None or math.isclose(a["bal"] - a["change"], prev_bal, rel_tol=1e-9, abs_tol=1e-6)

    def chains(order: Sequence[Dict[str, Any]], prev_bal: Optional[float]) -> bool:
        for a in order:
            if not follows(prev_bal, a):
                return False
            prev_bal = a["bal"]
        return True

    import itertools
    prev_bal: Optional[float] = None
    prev_id: Optional[str] = None
    for g in groups:
        orders = itertools.permutations(g) if len(g) <= 6 else [tuple(g)]
        found = next((o for o in orders if chains(o, prev_bal)), None)
        if found is None:
            breaks.append(f"{coin}:{prev_id or 'start'}->{g[0]['id']}")
            # re-synchronise on this group's own chain so one defect is one break, not every later row
            found = next((o for o in (itertools.permutations(g) if len(g) <= 6 else [tuple(g)]) if chains(o, None)), tuple(g))
        prev_bal, prev_id = found[-1]["bal"], found[-1]["id"]
    return breaks


def build(inp: Inputs, *, residual_tolerance_usd: float = RESIDUAL_TOLERANCE_USD, residual_tolerance_rel: float = RESIDUAL_TOLERANCE_REL,
          alternative_return_pct: Optional[float] = None) -> Dict[str, Any]:
    flags: List[Dict[str, Any]] = [dict(f, examples=list(f.get("examples", []))) for f in inp.adapter_flags]
    snaps = list(inp.snapshots)
    fills = _perp_dedupe(inp.fills, "perp_executions", flags)
    funding = _perp_dedupe(inp.funding, "funding", flags)
    spot = _dedupe(inp.spot, lambda s: s.trade_id, "spot_trades", flags)
    ext = _dedupe(inp.flows, lambda f: f.flow_id, "external_flows", flags)
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

    def in_window(ts: int) -> bool:
        return t0 is not None and t1 is not None and t0 < ts <= t1

    outside: List[str] = []
    if t0 is not None and t1 is not None:
        for coll, getter, label in ((fills, lambda x: x.exec_id, "execution"), (funding, lambda x: x.exec_id, "funding"), (spot, lambda x: x.trade_id, "spot"),
                                    (ext, lambda x: x.flow_id, "flow"), (lines, lambda x: x.line_id, "line")):
            for x in coll:
                ts = x.grid_ms if label == "funding" else x.ts_ms
                if not in_window(ts):
                    outside.append(f"{label}:{getter(x)}")
        fills = [x for x in fills if in_window(x.ts_ms)]
        funding = [x for x in funding if in_window(x.grid_ms)]
        spot = [x for x in spot if in_window(x.ts_ms)]
        ext = [x for x in ext if in_window(x.ts_ms)]
        lines = [x for x in lines if in_window(x.ts_ms)]
    _flag(flags, "RECORDS_OUTSIDE_WINDOW", SEVERITY_GAP, len(outside),
          "records outside (opening snapshot, closing snapshot] were excluded from the period: an export wider than the period is normal, but a record on the "
          "wrong side of a boundary is a boundary error, so they are listed", outside)

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
    stmt = {"present": bool(lines), "continuity_breaks": [], "fee_total": None, "funding_total": None}
    if not lines:
        _flag(flags, "NO_STATEMENT", SEVERITY_GAP, 1,
              "no account statement (cash_lines) was supplied: nothing independent of the executions confirms that every fee, settlement and transfer is present")
    else:
        for coin in COINS:
            stmt["continuity_breaks"] += _chain_breaks([l for l in lines if l.coin == coin], coin)
        _flag(flags, "STATEMENT_GAP", SEVERITY_GAP, len(stmt["continuity_breaks"]),
              "the statement's running balance does not follow from its own changes between these consecutive lines: a line is missing, altered or out of order",
              stmt["continuity_breaks"])
        fee_lines = math.fsum(-l.change for l in lines if l.category in ("FEE", "REBATE") and l.coin in USD_COINS)     # a rebate line nets against fees
        fund_lines = math.fsum(l.change for l in lines if l.category == "FUNDING" and l.coin in USD_COINS)
        stmt["fee_total"], stmt["funding_total"] = fee_lines, fund_lines
        tol = lambda ref: max(0.01, 1e-6 * abs(ref))                                                     # noqa: E731
        if fills and abs(fee_lines - (perp_fees + spot_usd_fee)) > tol(perp_fees):
            _flag(flags, "EXECUTION_NOT_IN_STATEMENT", SEVERITY_GAP, 1,
                  f"fees in the statement ({fee_lines:.6f}) differ from the executions' fees + spot USD fees ({perp_fees + spot_usd_fee:.6f}): an execution is missing "
                  "from one of the two, or a fee is mis-signed or mis-valued")
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
        stmt_spot_usd = math.fsum(l.change for l in lines if l.category == "SPOT" and l.coin in USD_COINS)
        stmt_spot_btc = math.fsum(l.change for l in lines if l.category == "SPOT" and l.coin == "BTC")
        if abs(stmt_spot_usd - (spot_sell_cash - spot_buy_cash - spot_usd_fee)) > tol(spot_buy_cash + spot_sell_cash) \
                or abs(stmt_spot_btc - spot_btc_delta) * (spot[0].price if spot else 1.0) > tol(spot_buy_cash + spot_sell_cash):
            _flag(flags, "SPOT_NOT_IN_STATEMENT", SEVERITY_GAP, 1,
                  f"spot cash in the statement (USDT {stmt_spot_usd:.6f}, BTC {stmt_spot_btc:.8f}) differs from the spot trades' effect "
                  f"(USDT {spot_sell_cash - spot_buy_cash - spot_usd_fee:.6f}, BTC {spot_btc_delta:.8f})")
        other_lines = [l.line_id for l in lines if l.category == "OTHER"]
        _flag(flags, "OTHER_CASH_LINES_PRESENT", SEVERITY_GAP, len(other_lines),
              "statement lines the supplier called OTHER: cash that is neither a fee, a settlement, a trade, interest nor a flow is unattributed, and is counted in "
              "the bridge as operating cost/income only so that it cannot hide", other_lines)

    # ---- the closing components rebuilt from the opening snapshot and the records, against the closing snapshot
    bridge: Optional[Dict[str, Any]] = None
    residuals: Dict[str, float] = {}
    if s0 is not None and s1 is not None:
        usdt_model = s0.usdt + usdt_ext + (perp["realised_usd"] if perp else 0.0) - perp_fees + funding_net + interest_usd + other_usd_lines \
            - spot_buy_cash + spot_sell_cash - spot_usd_fee
        btc_model = s0.btc + btc_ext + spot_btc_delta
        qty_model = perp["qty"] if perp else opening_qty
        avg_model = perp["avg_entry"] if perp else (opening_avg or 0.0)
        upl_model = qty_model * (s1.mark - avg_model) if qty_model != 0 else 0.0
        equity_model = usdt_model + btc_model * s1.btc_price + upl_model + s1.other_usd
        residuals = {"usdt": s1.usdt - usdt_model, "btc_qty": s1.btc - btc_model, "perp_qty": (s1.perp_qty - qty_model) if s1.perp_qty is not None else float("nan"),
                     "perp_unrealised_usd": s1.perp_upl_usd - upl_model, "equity_usd": s1.components_equity - equity_model}
        tol_eq = max(residual_tolerance_usd, residual_tolerance_rel * max(abs(s0.components_equity), abs(s1.components_equity)))
        residuals["tolerance_usd"] = tol_eq
        btc_pnl_invested = spot_buy_cash + spot_usd_fee - spot_sell_cash + btc_ext_usd         # (a fee paid in BTC leaves with the BTC: it is in V1)
        v0, v1 = s0.btc * s0.btc_price, s1.btc * s1.btc_price
        btc_economic = v1 - v0 - btc_pnl_invested
        perp_real = perp["realised_usd"] if perp else 0.0
        d_upl = s1.perp_upl_usd - s0.perp_upl_usd
        explained = ext_usd + perp_real - perp_fees + funding_net + interest_usd + other_usd_lines + d_upl + btc_economic
        d_equity = s1.components_equity - s0.components_equity
        bridge = {
            "opening_equity_usd": s0.components_equity, "closing_equity_usd": s1.components_equity, "change_in_equity_usd": d_equity,
            "external_flows_usd": ext_usd, "perp_realised_usd": perp_real, "perp_fees_usd": -perp_fees, "funding_net_usd": funding_net,
            "interest_usd": interest_usd, "other_cash_lines_usd": other_usd_lines, "change_in_perp_unrealised_usd": d_upl,
            "btc_economic_pnl_usd": btc_economic, "explained_change_usd": explained, "unexplained_usd": d_equity - explained,
            "reading": "deposits and withdrawals are external_flows_usd and are NOT profit; perp_realised is cash; change_in_perp_unrealised is NOT cash; "
                       "btc_economic_pnl is the price move on the BTC held plus the cost of trading it, net of BTC flows valued when they happened",
        }
        res_big = {k: v for k, v in residuals.items() if k not in ("tolerance_usd",) and isinstance(v, float) and math.isfinite(v)}
        coverage_ok = not any(f["severity"] == SEVERITY_GAP and f["code"] not in CHECK_GAPS for f in flags)           # default closed: an unlisted gap is open
        eq_breach = abs(residuals["equity_usd"]) > tol_eq
        part_breach = abs(residuals["usdt"]) > tol_eq or abs(residuals["btc_qty"]) * s1.btc_price > tol_eq or abs(residuals["perp_unrealised_usd"]) > tol_eq \
            or (math.isfinite(residuals["perp_qty"]) and abs(residuals["perp_qty"]) > AR.POSITION_ABS_TOL)
        if eq_breach or part_breach:
            _flag(flags, "EQUITY_RESIDUAL", SEVERITY_GAP if not coverage_ok else SEVERITY_HARD, 1,
                  "the closing snapshot differs from what the opening snapshot and the records imply: " +
                  ", ".join(f"{k} {v:+.6f}" for k, v in res_big.items()) + f" (tolerance {tol_eq:.2f} USD). "
                  + ("Coverage gaps are open, so this is an incomplete history rather than a contradiction." if not coverage_ok else
                     "Nothing is open in the coverage checks, so the records contradict the balances."))
        if s1.perp_qty is None and perp is not None and perp["qty"] != 0:
            _flag(flags, "OPENING_POSITION_UNKNOWN", SEVERITY_GAP, 1, "the closing snapshot does not state the perpetual position while the executions leave one open")

    # ---- returns, strategy contribution, economic capital
    window_days = (t1 - t0) / DAY_MS if (t0 is not None and t1 is not None) else None
    returns: Dict[str, Any] = {"status": "NOT_COMPUTED"}
    if s0 is not None and s1 is not None and bridge is not None:
        flows_ts = sorted(usd_flow_by_ts)
        dietz = modified_dietz(s0.components_equity, s1.components_equity, flows_ts, t0, t1)
        returns = {"status": "COMPUTED", "account_money_weighted": dietz, "account_irr": irr(s0.components_equity, s1.components_equity, flows_ts, t0, t1),
                   "account_linked": linked_returns(snaps, flows_ts),
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
        for s in snaps:
            if s.ts_ms < t0 or s.ts_ms > t1:
                continue
            btc_v = s.btc * s.btc_price
            pts.append((s.ts_ms, btc_v + (s.margin_posted_usd or 0.0) + (s.reserve_usd or 0.0), s.margin_posted_usd is not None and s.reserve_usd is not None))
        area = math.fsum((b[1] + a[1]) / 2.0 * (b[0] - a[0]) for a, b in zip(pts, pts[1:]))
        avg_capital = area / (pts[-1][0] - pts[0][0]) if len(pts) >= 2 and pts[-1][0] > pts[0][0] else None
        if not all(p[2] for p in pts):
            _flag(flags, "CAPITAL_COMPONENTS_UNSTATED", SEVERITY_GAP, 1,
                  "margin_posted_usd / reserve_usd are not stated on every snapshot: the economic capital is the BTC value alone and the return on it is an upper bound")
        if len(pts) == 2:
            _flag(flags, "TWO_POINT_CAPITAL_AVERAGE", SEVERITY_GAP, 1, "only two snapshots: the average capital is the mean of two instants, not a time integral")
        capital = {"status": "COMPUTED", "average_economic_capital_usd": avg_capital, "snapshots_used": len(pts),
                   "definition": "BTC inventory value + margin posted beyond it + reserve, trapezoid-averaged over the snapshots"}
        if avg_capital and avg_capital > 0 and window_days and window_days > 0:
            capital["account_return_on_capital_period"] = (bridge["change_in_equity_usd"] - ext_usd) / avg_capital if bridge else None
            if strategy.get("status") == "COMPUTED":
                capital["strategy_return_on_capital_period"] = strategy["net_usd"] / avg_capital
                capital["strategy_return_on_capital_annualised_simple_pct"] = strategy["net_usd"] / avg_capital * YEAR_DAYS / window_days * 100.0
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
        "RECONCILED_BUT_UNCHECKED": "the records reconcile to the balances but some checks against the public market were not possible",
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
        "perp_position": perp, "flags": flags,
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
        out["strategy_return_annualised_simple_pct"] = strategy["net_usd"] / avg * YEAR_DAYS / window_days * 100.0
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
                  "change_in_perp_unrealised_usd", "btc_economic_pnl_usd", "unexplained_usd", "closing_equity_usd"):
            a(f"    {k:32s} {b[k]:>16,.2f}")
        a("    " + b["reading"])
        res = rep["residuals"]
        a("  RESIDUALS (closing snapshot minus what the records imply): " + ", ".join(f"{k} {v:+.4f}" for k, v in res.items() if isinstance(v, float) and math.isfinite(v)))
    s = rep["strategy_contribution"]
    a(f"  STRATEGY (complete round trips): {s['status']}" + (f"  net {s['net_usd']:.2f} = funding {s['funding_usd']:.2f} + basis {s['basis_pnl_usd']:.2f} + fees {s['fees_usd']:.2f};"
                                                         f" funding coverage {s['funding_coverage']}" if s["status"] == "COMPUTED" else f"  {s.get('reason', '')}"))
    r = rep["returns"]
    if r.get("status") == "COMPUTED":
        d = r["account_money_weighted"]
        a("  RETURNS: " + (f"modified Dietz {d['period_return'] * 100:.4f} % over {d['days']:.1f} days (capital base {d['capital_base_usd']:,.0f})" if d else "money-weighted: not computable")
          + (f"; IRR {r['account_irr']['annual_rate'] * 100:.2f} %/yr" if r.get("account_irr") else "") + f"; alternative return {r['alternative_return_pct']}")
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
