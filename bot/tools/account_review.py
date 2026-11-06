#!/usr/bin/env python3
"""Offline review of owner-supplied ACCOUNT records for the BTC funding carry. Reads two files, makes no request, places no order.

    python3 tools/account_review.py --fills FILLS.jsonl --funding FUNDING.jsonl [--symbol BTCUSDT]
                                    [--taker-bps X --maker-bps Y] [--capital-usd C]
                                    [--public-funding F.json.gz] [--perp-4h K.json.gz] [--json]

Why it exists. Everything else in this repository reasons about the PUBLIC market. Whether the carry earns money is decided by
what an ACCOUNT was actually charged and paid: the fills with their fee records, the funding settlements it received, the fee
tier it really has. Nothing in the repository reads those (``opportunity_engine`` says so), so no strategy can pass
ECONOMICALLY_RECONCILED. This tool is the missing offline step: it takes the records the owner (or a customer) exports from the
exchange, refuses anything inconsistent, and reports what the records themselves prove.

Input records (JSON lines or CSV with these column names; the preparer maps the exchange export onto them; unknown columns are
listed and ignored):

  fills    exec_id  ts_ms  symbol  side(Buy|Sell)  qty(BTC>0)  price(USD>0)  fee_usd(paid>0, rebate<0)  liquidity(taker|maker)
           [ref_price: the perp mid when the order was decided]   [spot_ref: the spot mid at the fill]
  funding  exec_id  ts_ms  symbol  position_qty(signed BTC, short<0)  rate(fraction per 8h)  payment_usd(received>0)

What it checks (each failure is a named flag with a count, never a silent repair):
  * every number is finite, every id unique, every time plausible; a funding row is snapped to the 8h UTC grid only within 60 s;
  * the position is rebuilt from the fills; each 8h settlement the book was provably short through must have exactly one funding
    row; a row at a moment the book was flat is unattributed; a fill within a minute of a settlement makes it ambiguous (counted
    on neither side);
  * the position the funding row states equals the position the fills imply;
  * the funding rate equals the public print at that time (when a public history covers it);
  * the payment equals position x mark x rate, the mark being the 4h bar open at the settlement, to within 2 %; the sign
    convention is checked (a book that is short while the rate is positive must RECEIVE);
  * each fill's fee, as bps of its notional, equals the stated tier (when given), by liquidity.

What it computes: per closed lifecycle (flat to flat) the funding received, the fees paid, and, only when every fill carries a
``spot_ref``, the exact basis P&L = sum over sells q (price - spot) - sum over buys q (price - spot), which equals the perp P&L
plus the P&L of the overlay BTC. Net = funding + basis - fees. Without ``spot_ref`` the net is UNKNOWN (a 4h kline cannot resolve a
10 bp economics, so none is substituted). Slippage against ``ref_price`` is a decomposition of the fill price already inside the
basis P&L; it is never subtracted a second time.

What it cannot do. The files are the owner's own, so every number here is tagged OWNER_SUPPLIED_UNVERIFIED and the verdict can
never be ACCOUNT_VERIFIED: that needs the records read from the account by the repository's own ledger tooling. The review changes
no status in any engine, authorises nothing, and prints the sha256 of each input so the issuing can be anchored off the machine.
"""
from __future__ import annotations

import argparse
import bisect
import csv
import datetime
import hashlib
import json
import math
import os
import re
import sys
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
BOT = os.path.dirname(HERE)
for _p in (BOT, HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import statistical_power as SP          # noqa: E402
import carry_teardown as CT             # noqa: E402

SETTLE_MS = 8 * 3_600_000
DAY_MS = 86_400_000
SLACK_MS = 60_000                       # the ledger's attribution slack: a fill this close to a settlement makes it ambiguous
DUST_QTY = 1e-8
MAX_ABS_RATE = 0.03                     # 300 bps per settlement; anything larger is a unit error
MARK_TOLERANCE = 0.02                   # payment-implied mark vs the 4h bar open: a consistency screen, not a precision check
POSITION_REL_TOL = 1e-3
POSITION_ABS_TOL = 1e-6
RATE_TOL_BPS = 0.002
FEE_TOL_BPS = 0.25
MIN_TS_MS, MAX_TS_MS = 10 ** 12, 4 * 10 ** 12
MIN_ACCOUNT_SETTLEMENTS = 90            # the opportunity engine's floors for ECONOMICALLY_RECONCILED
MIN_ACCOUNT_ROUND_TRIPS = 3
MIN_TRIPS_FOR_INTERVAL = 8
PROVENANCE = "OWNER_SUPPLIED_UNVERIFIED"
ASSUMED_ROUND_TRIP_BPS = CT.DEFAULT_ROUND_TRIP_BPS

FILL_REQUIRED = ("exec_id", "ts_ms", "symbol", "side", "qty", "price", "fee_usd", "liquidity")
FILL_OPTIONAL = ("ref_price", "spot_ref")
FUNDING_REQUIRED = ("exec_id", "ts_ms", "symbol", "position_qty", "rate", "payment_usd")

SEVERITY_HARD = "HARD"                  # the records contradict each other or the public market: do not rely on them
SEVERITY_GAP = "GAP"                    # the records are silent where they must not be: the result is incomplete

_NUM = re.compile(r"^[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?$")


class RecordError(ValueError):
    """The supplied records cannot be read as evidence at all."""


# ---------------------------------------------------------------- reading

def _refuse_protected(path: str) -> str:
    """Never read runtime state, evidence or secrets, whatever the caller names. Returns the resolved path."""
    real = os.path.realpath(path)
    base = os.path.basename(real).lower()
    parts = [p.lower() for p in real.split(os.sep) if p]
    if (base.endswith((".db", ".db-wal", ".db-shm", ".sqlite", ".sqlite3", ".pem", ".key")) or base.startswith(".env")
            or base == "testnet_session_evidence.jsonl" or "runs" in parts or ("state" in parts and "bot" in parts)):
        raise RecordError(f"refusing to read {os.path.basename(real)!r}: runtime state, evidence and credentials are never inputs")
    return real


def _digest(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def read_rows(path: str) -> Tuple[List[Dict[str, Any]], str]:
    """Rows of a .jsonl or .csv file (nothing else) and the file's sha256."""
    real = _refuse_protected(path)
    if not os.path.isfile(real):
        raise RecordError(f"{os.path.basename(path)!r} is not a file")
    lower = real.lower()
    rows: List[Dict[str, Any]] = []
    try:
        if lower.endswith(".csv"):
            with open(real, "r", encoding="utf-8", newline="") as fh:
                rows = [dict(r) for r in csv.DictReader(fh)]
        elif lower.endswith((".jsonl", ".ndjson")):
            with open(real, "r", encoding="utf-8") as fh:
                for n, line in enumerate(fh, 1):
                    if not line.strip():
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError as exc:
                        raise RecordError(f"{os.path.basename(path)} line {n}: not JSON ({exc.msg})") from exc
                    if not isinstance(obj, dict):
                        raise RecordError(f"{os.path.basename(path)} line {n}: not an object")
                    rows.append(obj)
        else:
            raise RecordError(f"{os.path.basename(path)!r}: only .jsonl, .ndjson and .csv are read")
    except UnicodeDecodeError as exc:
        raise RecordError(f"{os.path.basename(path)!r} is not UTF-8 text") from exc
    if not rows:
        raise RecordError(f"{os.path.basename(path)!r} holds no records")
    return rows, _digest(real)


# ---------------------------------------------------------------- typed parsing

def _num(v: Any, name: str, where: str) -> float:
    if isinstance(v, bool):
        raise RecordError(f"{where}: {name} is a boolean")
    if isinstance(v, (int, float)):
        x = float(v)
    elif isinstance(v, str) and _NUM.match(v.strip()):
        x = float(v.strip())
    else:
        raise RecordError(f"{where}: {name} is not a number ({v!r})")
    if not math.isfinite(x):
        raise RecordError(f"{where}: {name} is not finite")
    return x


def _ts(v: Any, where: str) -> int:
    x = _num(v, "ts_ms", where)
    if x != int(x) or not (MIN_TS_MS <= x < MAX_TS_MS):
        raise RecordError(f"{where}: ts_ms {v!r} is not an integer millisecond time in 2001-2096")
    return int(x)


def _str(v: Any, name: str, where: str) -> str:
    if not isinstance(v, str) or not v.strip():
        raise RecordError(f"{where}: {name} is missing")
    return v.strip()


@dataclass(frozen=True)
class Fill:
    exec_id: str
    ts_ms: int
    side: str
    qty: float
    price: float
    fee_usd: float
    liquidity: str
    ref_price: Optional[float]
    spot_ref: Optional[float]

    @property
    def signed_qty(self) -> float:
        return self.qty if self.side == "Buy" else -self.qty

    @property
    def notional(self) -> float:
        return self.qty * self.price


@dataclass(frozen=True)
class FundingRow:
    exec_id: str
    ts_ms: int                          # as recorded
    grid_ms: int                        # snapped to the 8h UTC grid
    position_qty: float
    rate: float
    payment_usd: float


def _missing(rows: Sequence[Mapping[str, Any]], required: Sequence[str], what: str) -> None:
    keys = set()
    for r in rows:
        keys |= set(r)
    gone = [c for c in required if c not in keys]
    if gone:
        raise RecordError(f"{what}: required column(s) missing: {', '.join(gone)}")


def parse_fills(rows: Sequence[Mapping[str, Any]], symbol: str) -> Tuple[List[Fill], List[str]]:
    _missing(rows, FILL_REQUIRED, "fills")
    known = set(FILL_REQUIRED) | set(FILL_OPTIONAL)
    ignored = sorted({k for r in rows for k in r} - known)
    out: List[Fill] = []
    seen = set()
    for i, r in enumerate(rows):
        w = f"fills row {i + 1}"
        eid = _str(r.get("exec_id"), "exec_id", w)
        if eid in seen:
            raise RecordError(f"{w}: duplicate exec_id {eid!r}")
        seen.add(eid)
        if _str(r.get("symbol"), "symbol", w) != symbol:
            raise RecordError(f"{w}: symbol {r.get('symbol')!r} is not {symbol!r}; one symbol per review")
        side = _str(r.get("side"), "side", w)
        if side not in ("Buy", "Sell"):
            raise RecordError(f"{w}: side {side!r} is not Buy or Sell")
        liq = _str(r.get("liquidity"), "liquidity", w).lower()
        if liq not in ("taker", "maker"):
            raise RecordError(f"{w}: liquidity {liq!r} is not taker or maker")
        qty, price = _num(r.get("qty"), "qty", w), _num(r.get("price"), "price", w)
        if qty <= 0 or price <= 0:
            raise RecordError(f"{w}: qty and price must be positive")
        opt: Dict[str, Optional[float]] = {}
        for name in FILL_OPTIONAL:
            raw = r.get(name)
            if raw is None or (isinstance(raw, str) and not raw.strip()):
                opt[name] = None
            else:
                v = _num(raw, name, w)
                if v <= 0:
                    raise RecordError(f"{w}: {name} must be positive")
                opt[name] = v
        out.append(Fill(eid, _ts(r.get("ts_ms"), w), side, qty, price, _num(r.get("fee_usd"), "fee_usd", w), liq,
                        opt["ref_price"], opt["spot_ref"]))
    out.sort(key=lambda f: (f.ts_ms, f.exec_id))
    return out, ignored


def parse_funding(rows: Sequence[Mapping[str, Any]], symbol: str) -> Tuple[List[FundingRow], List[str]]:
    _missing(rows, FUNDING_REQUIRED, "funding")
    ignored = sorted({k for r in rows for k in r} - set(FUNDING_REQUIRED))
    out: List[FundingRow] = []
    seen, seen_grid = set(), set()
    for i, r in enumerate(rows):
        w = f"funding row {i + 1}"
        eid = _str(r.get("exec_id"), "exec_id", w)
        if eid in seen:
            raise RecordError(f"{w}: duplicate exec_id {eid!r}")
        seen.add(eid)
        if _str(r.get("symbol"), "symbol", w) != symbol:
            raise RecordError(f"{w}: symbol {r.get('symbol')!r} is not {symbol!r}; one symbol per review")
        t = _ts(r.get("ts_ms"), w)
        grid = round(t / SETTLE_MS) * SETTLE_MS
        if abs(t - grid) > SLACK_MS:
            raise RecordError(f"{w}: ts_ms {t} is {abs(t - grid) / 1000:.0f}s from the 8h UTC settlement grid (limit {SLACK_MS // 1000}s)")
        if grid in seen_grid:
            raise RecordError(f"{w}: a second funding row for the settlement at {grid}; one per settlement")
        seen_grid.add(grid)
        rate = _num(r.get("rate"), "rate", w)
        if abs(rate) > MAX_ABS_RATE:
            raise RecordError(f"{w}: rate {rate} exceeds {MAX_ABS_RATE} per settlement; is it a percentage or in bps?")
        out.append(FundingRow(eid, t, grid, _num(r.get("position_qty"), "position_qty", w), rate, _num(r.get("payment_usd"), "payment_usd", w)))
    out.sort(key=lambda f: f.grid_ms)
    return out, ignored


# ---------------------------------------------------------------- the book rebuilt from the fills

class Book:
    """Signed perpetual position over time, rebuilt only from the fills."""

    def __init__(self, fills: Sequence[Fill]):
        self.fills = list(fills)
        self.times = [f.ts_ms for f in self.fills]
        cum, q = [], 0.0
        for f in self.fills:
            q += f.signed_qty
            if abs(q) <= DUST_QTY:
                q = 0.0
            cum.append(q)
        self.cum = cum

    def qty_at(self, t: int) -> float:
        """Position after every fill with ts <= t."""
        i = bisect.bisect_right(self.times, t)
        return self.cum[i - 1] if i else 0.0

    def moved_within(self, lo: int, hi: int) -> bool:
        return bisect.bisect_right(self.times, hi) > bisect.bisect_left(self.times, lo)

    def lifecycles(self) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        cur: Optional[Dict[str, Any]] = None
        for f, q in zip(self.fills, self.cum):
            if cur is None:
                cur = {"start_ms": f.ts_ms, "end_ms": None, "fills": [], "min_qty": 0.0, "max_qty": 0.0}
            cur["fills"].append(f)
            cur["min_qty"], cur["max_qty"] = min(cur["min_qty"], q), max(cur["max_qty"], q)
            if q == 0.0:
                cur["end_ms"] = f.ts_ms
                out.append(cur)
                cur = None
        if cur is not None:
            out.append(cur)
        return out


def classify_settlement(book: Book, t: int) -> str:
    """held | flat | ambiguous, from the fills alone (the same rule the ledger uses, a minute either side)."""
    if book.moved_within(t - SLACK_MS, t + SLACK_MS):
        return "ambiguous"
    before, after = book.qty_at(t - SLACK_MS), book.qty_at(t + SLACK_MS)
    if before == 0.0 and after == 0.0:
        return "flat"
    return "held" if (before < 0.0 and after < 0.0) or (before > 0.0 and after > 0.0) else "ambiguous"


# ---------------------------------------------------------------- the review

def _flag(flags: List[Dict[str, Any]], code: str, severity: str, count: int, detail: str, examples: Sequence[Any] = ()) -> None:
    if count:
        flags.append({"code": code, "severity": severity, "count": count, "detail": detail, "examples": list(examples)[:5]})


def _mean_sd(xs: Sequence[float]) -> Tuple[float, Optional[float]]:
    m = sum(xs) / len(xs)
    if len(xs) < 2:
        return m, None
    return m, math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))


def _interval(xs: Sequence[float]) -> Dict[str, Any]:
    n = len(xs)
    if n < MIN_TRIPS_FOR_INTERVAL:
        return {"interval": None, "reason": f"NO_INTERVAL: {n} complete round trips, fewer than {MIN_TRIPS_FOR_INTERVAL}"}
    m, sd = _mean_sd(xs)
    if not sd:
        return {"interval": None, "reason": "NO_VARIANCE: every round trip is identical"}
    half = SP.student_t_isf(0.025, n - 1) * sd / math.sqrt(n)
    return {"interval": [m - half, m + half], "level": 0.95,
            "reading": "indicative only: round trips are serially dependent and few, so the true coverage is lower"}


def review(fills: Sequence[Fill], funding: Sequence[FundingRow], *, taker_bps: Optional[float] = None, maker_bps: Optional[float] = None,
           capital_usd: Optional[float] = None, public: Optional[Mapping[int, float]] = None,
           bar_open: Optional[Mapping[int, float]] = None, digests: Optional[Mapping[str, str]] = None,
           ignored_columns: Optional[Mapping[str, Sequence[str]]] = None) -> Dict[str, Any]:
    """Cross-check the fills and funding rows and report what they prove. Pure function of its inputs."""
    for name, v in (("taker_bps", taker_bps), ("maker_bps", maker_bps)):
        if v is not None and not (isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and -10 <= v <= 100):
            raise RecordError(f"{name} must be a finite number of bps between -10 and 100")
    if capital_usd is not None and not (isinstance(capital_usd, (int, float)) and not isinstance(capital_usd, bool)
                                        and math.isfinite(capital_usd) and capital_usd > 0):
        raise RecordError("capital_usd must be positive and finite")
    if not fills:
        raise RecordError("there are no fills")
    flags: List[Dict[str, Any]] = []
    book = Book(fills)
    lifecycles = book.lifecycles()
    funding_by_grid = {f.grid_ms: f for f in funding}

    # ---- which settlements were the book provably short through
    first_ms, last_fill_ms = fills[0].ts_ms, fills[-1].ts_ms
    horizon = max(last_fill_ms, funding[-1].grid_ms if funding else last_fill_ms)
    grid = list(range(((first_ms + SETTLE_MS - 1) // SETTLE_MS) * SETTLE_MS, horizon + 1, SETTLE_MS))
    cls = {t: classify_settlement(book, t) for t in grid}
    held = [t for t in grid if cls[t] == "held"]
    ambiguous = [t for t in grid if cls[t] == "ambiguous"]
    missing = [t for t in held if t not in funding_by_grid]
    off_book = [f for f in funding if cls.get(f.grid_ms) == "flat" or f.grid_ms not in cls]
    in_ambiguous = [f for f in funding if cls.get(f.grid_ms) == "ambiguous"]
    _flag(flags, "MISSING_SETTLEMENT", SEVERITY_GAP, len(missing), "the book was short through these settlements but the funding file has no row",
          [_iso(t) for t in missing])
    _flag(flags, "UNATTRIBUTED_FUNDING", SEVERITY_HARD, len(off_book),
          "funding rows at moments the fills say the book was flat (or outside the fills' span)", [_iso(f.grid_ms) for f in off_book])
    long_lcs = [lc for lc in lifecycles if lc["max_qty"] > DUST_QTY]
    _flag(flags, "LONG_EXPOSURE", SEVERITY_HARD, len(long_lcs), "the fills put the perpetual long at some point: this is not a short-carry book; "
          "those lifecycles are excluded from the economics", [_iso(lc["start_ms"]) for lc in long_lcs])
    open_lcs = [lc for lc in lifecycles if lc["end_ms"] is None]
    _flag(flags, "OPEN_LIFECYCLE", SEVERITY_GAP, len(open_lcs), "the book is not flat at the end of the records: that round trip is unrealised and excluded",
          [_iso(lc["start_ms"]) for lc in open_lcs])

    # ---- row-level cross-checks (rows the fills say the book held through)
    pos_bad, rate_bad, rate_unchecked, mark_bad, mark_unchecked, sign_bad, sign_n = [], [], 0, [], 0, [], 0
    sign_agree = 0
    for f in funding:
        if cls.get(f.grid_ms) != "held":
            continue
        implied = book.qty_at(f.grid_ms - SLACK_MS)
        if abs(f.position_qty - implied) > max(POSITION_ABS_TOL, POSITION_REL_TOL * abs(implied)):
            pos_bad.append(f.grid_ms)
        if public is not None and f.grid_ms in public:
            if abs(f.rate * 1e4 - public[f.grid_ms]) > RATE_TOL_BPS:
                rate_bad.append(f.grid_ms)
        else:
            rate_unchecked += 1
        expect_sign = -implied * f.rate
        if abs(expect_sign) > 0 and abs(f.payment_usd) > 0:
            sign_n += 1
            if (expect_sign > 0) == (f.payment_usd > 0):
                sign_agree += 1
            else:
                sign_bad.append(f.grid_ms)
        mark = bar_open.get(f.grid_ms) if bar_open is not None else None
        if mark and abs(f.rate) > 1e-12 and abs(implied) > DUST_QTY:
            implied_mark = f.payment_usd / (-implied * f.rate)
            if not (implied_mark > 0 and abs(implied_mark - mark) / mark <= MARK_TOLERANCE):
                mark_bad.append(f.grid_ms)
        elif abs(f.rate) > 1e-12:
            mark_unchecked += 1
    inverted = sign_n >= 3 and sign_agree == 0
    _flag(flags, "SIGN_CONVENTION_INVERTED", SEVERITY_HARD, 1 if inverted else 0,
          "every funding payment has the opposite sign to position x rate: the file's sign convention is not 'received > 0'; it is NOT flipped here")
    if not inverted:
        _flag(flags, "FUNDING_SIGN_WRONG", SEVERITY_HARD, len(sign_bad), "payment sign contradicts position x rate", [_iso(t) for t in sign_bad])
    _flag(flags, "POSITION_MISMATCH", SEVERITY_HARD, len(pos_bad), "the position the funding row states differs from the position the fills imply",
          [_iso(t) for t in pos_bad])
    _flag(flags, "RATE_DISAGREES_WITH_PUBLIC", SEVERITY_HARD, len(rate_bad), "the funding rate differs from the public print at that settlement",
          [_iso(t) for t in rate_bad])
    _flag(flags, "PAYMENT_DISAGREES_WITH_MARK", SEVERITY_HARD, len(mark_bad),
          f"payment / (position x rate) is not within {MARK_TOLERANCE:.0%} of the 4h bar open at the settlement", [_iso(t) for t in mark_bad])

    # ---- fees
    fee_rows: Dict[str, List[float]] = {"taker": [], "maker": []}
    fee_bad: List[str] = []
    for fl in fills:
        eff = fl.fee_usd / fl.notional * 1e4
        fee_rows[fl.liquidity].append(eff)
        sched = taker_bps if fl.liquidity == "taker" else maker_bps
        if sched is not None and abs(eff - sched) > FEE_TOL_BPS:
            fee_bad.append(fl.exec_id)
    _flag(flags, "FEE_DISAGREES_WITH_TIER", SEVERITY_HARD, len(fee_bad),
          f"a fill's fee differs from the stated tier by more than {FEE_TOL_BPS} bps", fee_bad)
    fees = {liq: ({"n": len(v), "mean_bps": sum(v) / len(v), "min_bps": min(v), "max_bps": max(v)} if v else {"n": 0})
            for liq, v in fee_rows.items()}
    slips = [((f.price - f.ref_price) if f.side == "Buy" else (f.ref_price - f.price)) / f.ref_price * 1e4 for f in fills if f.ref_price]
    slippage = ({"n": len(slips), "of_fills": len(fills), "mean_adverse_bps": sum(slips) / len(slips),
                 "note": "already inside the fill prices and the basis P&L; shown as a decomposition, never subtracted again"}
                if slips else {"n": 0, "of_fills": len(fills), "mean_adverse_bps": "UNKNOWN: no ref_price on any fill"})

    # ---- lifecycle economics
    trips: List[Dict[str, Any]] = []
    for lc in lifecycles:
        fl = lc["fills"]
        row: Dict[str, Any] = {"start": _iso(lc["start_ms"]), "end": _iso(lc["end_ms"]) if lc["end_ms"] is not None else None,
                               "fills": len(fl)}
        if lc["max_qty"] > DUST_QTY:
            row["status"] = "EXCLUDED_LONG_EXPOSURE"
            trips.append(row)
            continue
        if lc["end_ms"] is None:
            row["status"] = "EXCLUDED_OPEN"
            trips.append(row)
            continue
        window = [t for t in held if lc["start_ms"] < t < lc["end_ms"]]
        got = [funding_by_grid[t] for t in window if t in funding_by_grid]
        miss = [t for t in window if t not in funding_by_grid]
        amb = [t for t in ambiguous if lc["start_ms"] - SLACK_MS <= t <= lc["end_ms"] + SLACK_MS]
        sells = [f for f in fl if f.side == "Sell"]
        notional = sum(f.notional for f in sells)
        fees_usd = sum(f.fee_usd for f in fl)
        funding_usd = sum(f.payment_usd for f in got)
        perp_pnl = sum(f.notional for f in sells) - sum(f.notional for f in fl if f.side == "Buy")
        have_spot = all(f.spot_ref for f in fl)
        basis = (sum(f.qty * (f.price - f.spot_ref) for f in sells) - sum(f.qty * (f.price - f.spot_ref) for f in fl if f.side == "Buy")
                 ) if have_spot else None
        hold_days = (lc["end_ms"] - lc["start_ms"]) / DAY_MS
        row.update({"settlements_held": len(window), "settlements_recorded": len(got), "settlements_missing": len(miss),
                    "settlements_ambiguous": len(amb), "hold_days": hold_days, "entry_notional_usd": notional,
                    "funding_usd": funding_usd, "fees_usd": fees_usd, "perp_pnl_usd": perp_pnl, "basis_pnl_usd": basis,
                    "fee_bps_of_notional": fees_usd / notional * 1e4,
                    "funding_bps_of_notional": funding_usd / notional * 1e4})
        if miss:
            row["status"] = "INCOMPLETE_FUNDING"
        elif basis is None:
            row["status"] = "NET_UNKNOWN_NO_SPOT_REFERENCE"
            row["cash_only_usd"] = funding_usd - fees_usd
            row["cash_only_note"] = "funding - fees only; EXCLUDES basis P&L, so it is not a net edge"
        else:
            net = funding_usd + basis - fees_usd
            row.update({"status": "COMPLETE", "net_usd": net, "net_bps_of_notional": net / notional * 1e4,
                        "basis_bps_of_notional": basis / notional * 1e4,
                        "annualised_pct_on_notional": (net / notional) * 365.0 / hold_days * 100.0 if hold_days > 0 else None})
            if capital_usd:
                row["annualised_pct_on_stated_capital"] = (net / capital_usd) * 365.0 / hold_days * 100.0 if hold_days > 0 else None
        trips.append(row)
    complete = [t for t in trips if t["status"] == "COMPLETE"]
    n_set_rec = sum(t["settlements_recorded"] for t in complete)

    economics: Dict[str, Any]
    if complete:
        nets = [t["net_bps_of_notional"] for t in complete]
        m, sd = _mean_sd(nets)
        tot_n = sum(t["entry_notional_usd"] for t in complete)
        economics = {"tag": PROVENANCE, "complete_round_trips": len(complete), "settlements_in_them": n_set_rec,
                     "net_usd": sum(t["net_usd"] for t in complete), "funding_usd": sum(t["funding_usd"] for t in complete),
                     "fees_usd": sum(t["fees_usd"] for t in complete), "basis_pnl_usd": sum(t["basis_pnl_usd"] for t in complete),
                     "mean_net_bps_of_notional": m, "sd_net_bps_of_notional": sd, **_interval(nets),
                     "notional_weighted_net_bps": sum(t["net_usd"] for t in complete) / tot_n * 1e4}
    else:
        economics = {"tag": "UNKNOWN", "reading": "no round trip has every settlement recorded AND a spot reference on every fill: "
                                                  "no net is stated (funding - fees alone is shown per lifecycle, marked as excluding basis)"}

    # ---- actual against assumed
    fee_trips = [t for t in trips if "fee_bps_of_notional" in t]
    actual_fee_bps = (sum(t["fees_usd"] for t in fee_trips) / sum(t["entry_notional_usd"] for t in fee_trips) * 1e4) if fee_trips else None
    pub_same = [public[t] for t in held if public is not None and t in public and t in funding_by_grid]
    rec_bps = [funding_by_grid[t].rate * 1e4 for t in held if public is not None and t in public and t in funding_by_grid]
    comparison = {"round_trip_fee_bps": {"actual_from_records": actual_fee_bps, "assumed_by_repository": ASSUMED_ROUND_TRIP_BPS,
                                         "assumed_tag": "ASSUMED", "actual_tag": PROVENANCE if actual_fee_bps is not None else "UNKNOWN"},
                  "mean_public_print_bps_over_held_settlements": (sum(pub_same) / len(pub_same)) if pub_same else "UNKNOWN",
                  "mean_recorded_rate_bps_over_held_settlements": (sum(rec_bps) / len(rec_bps)) if rec_bps else "UNKNOWN"}

    sufficiency = {"settlements_recorded_in_complete_round_trips": n_set_rec, "engine_floor_settlements": MIN_ACCOUNT_SETTLEMENTS,
                   "complete_round_trips": len(complete), "engine_floor_round_trips": MIN_ACCOUNT_ROUND_TRIPS,
                   "meets_engine_floors": n_set_rec >= MIN_ACCOUNT_SETTLEMENTS and len(complete) >= MIN_ACCOUNT_ROUND_TRIPS,
                   "reading": "the floors are screens, not proof; they apply to records read from the account, which these are not"}
    if bar_open is None:
        _flag(flags, "MARK_CHECK_NOT_RUN", SEVERITY_GAP, 1, "no 4h price history was supplied: the payment-vs-mark check did not run")
    elif mark_unchecked:
        _flag(flags, "MARK_CHECK_PARTIAL", SEVERITY_GAP, mark_unchecked, "settlements with no 4h bar in the supplied history were not mark-checked")
    if public is None:
        _flag(flags, "RATE_CHECK_NOT_RUN", SEVERITY_GAP, 1, "no public funding history was supplied: the rate was not compared with the public print")
    elif rate_unchecked:
        _flag(flags, "RATE_CHECK_PARTIAL", SEVERITY_GAP, rate_unchecked, "settlements the public history does not cover were not rate-checked")
    if taker_bps is None and maker_bps is None:
        _flag(flags, "FEE_TIER_NOT_STATED", SEVERITY_GAP, 1, "no fee tier was stated: fees are reported as charged but not compared with the tier")
    _flag(flags, "AMBIGUOUS_SETTLEMENT", SEVERITY_GAP, len(ambiguous), "a fill within a minute of the settlement: ownership cannot be established, "
          "counted as neither held nor missing", [_iso(t) for t in ambiguous])
    _flag(flags, "FUNDING_ROW_IN_AMBIGUOUS_WINDOW", SEVERITY_GAP, len(in_ambiguous), "a funding row at an ambiguous settlement: kept out of the totals",
          [_iso(f.grid_ms) for f in in_ambiguous])
    _flag(flags, "NO_SPOT_REFERENCE", SEVERITY_GAP, sum(1 for t in trips if t["status"] == "NET_UNKNOWN_NO_SPOT_REFERENCE"),
          "closed round trips whose fills lack a spot_ref: basis P&L, hence the net, is UNKNOWN")

    hard = [f for f in flags if f["severity"] == SEVERITY_HARD]
    gaps = [f for f in flags if f["severity"] == SEVERITY_GAP]
    if hard:
        verdict = "REJECTED_RECORDS_CONTRADICT"
    elif not complete or any(f["code"] in ("MISSING_SETTLEMENT", "OPEN_LIFECYCLE") for f in gaps):
        verdict = "INCOMPLETE"
    elif not sufficiency["meets_engine_floors"]:
        verdict = "CONSISTENT_BUT_TOO_SMALL"
    else:
        verdict = "CONSISTENT_NOT_VERIFIED"
    return {
        "provenance": PROVENANCE, "verdict": verdict, "authorizes_trading": False, "changes_any_status": False,
        "inputs": {"fills": len(fills), "funding_rows": len(funding), "first_fill": _iso(first_ms), "last_fill": _iso(last_fill_ms),
                   "sha256": dict(digests or {}), "ignored_columns": {k: list(v) for k, v in (ignored_columns or {}).items()}},
        "settlements": {"held": len(held), "recorded_for_held": len(held) - len(missing), "missing": len(missing), "ambiguous": len(ambiguous),
                        "funding_rows_unattributed": len(off_book)},
        "fees": fees, "slippage_vs_reference": slippage, "round_trips": trips, "economics": economics,
        "actual_vs_assumed": comparison, "sufficiency": sufficiency, "flags": flags,
        "what_would_upgrade_this": [
            "the same records read from the account by the repository's own ledger and reconciliation tooling, not from an export",
            "the account's fee tier read in the exchange UI and stated with --taker-bps / --maker-bps",
            "a spot_ref on every fill (the overlay's spot mid at the moment of each perp fill) so the basis P&L is exact",
            "the sha256 values above archived off the machine at the time the review is issued"],
    }


def _iso(ms: int) -> str:
    return datetime.datetime.fromtimestamp(ms / 1000.0, tz=datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------- orchestration and CLI

def run(fills_path: str, funding_path: str, *, symbol: str = "BTCUSDT", taker_bps: Optional[float] = None, maker_bps: Optional[float] = None,
        capital_usd: Optional[float] = None, public_path: Optional[str] = None, perp_path: Optional[str] = None,
        use_default_history: bool = True) -> Dict[str, Any]:
    f_rows, f_dig = read_rows(fills_path)
    g_rows, g_dig = read_rows(funding_path)
    fills, f_ign = parse_fills(f_rows, symbol)
    funding, g_ign = parse_funding(g_rows, symbol)
    public = bar_open = None
    digests = {"fills": f_dig, "funding": g_dig}
    pub_p = public_path or (os.path.join(CT.STUDY, "funding_BTCUSDT.json.gz") if use_default_history and symbol == "BTCUSDT" else None)
    perp_p = perp_path or (os.path.join(CT.STUDY, "linear_BTCUSDT_240.json.gz") if use_default_history and symbol == "BTCUSDT" else None)
    try:
        if pub_p:
            ts, bps = CT.parse_funding(CT._open_json(_refuse_protected(pub_p)))
            public = dict(zip(ts, bps))
            digests["public_funding"] = _digest(pub_p)
        if perp_p:
            bars = CT.parse_klines(CT._open_json(_refuse_protected(perp_p)))
            bar_open = {t: o for t, (o, _c) in bars.items()}
            digests["perp_4h"] = _digest(perp_p)
    except CT.DataError as exc:
        raise RecordError(f"price/funding history unusable: {exc}") from exc
    return review(fills, funding, taker_bps=taker_bps, maker_bps=maker_bps, capital_usd=capital_usd, public=public, bar_open=bar_open,
                  digests=digests, ignored_columns={"fills": f_ign, "funding": g_ign})


def render(rep: Mapping[str, Any]) -> str:
    L: List[str] = []
    a = L.append
    a(f"ACCOUNT RECORDS REVIEW   provenance {rep['provenance']}   verdict {rep['verdict']}")
    a("  authorizes trading: NO.  changes any engine status: NO.  every number below is the supplier's, cross-checked, not verified.")
    i = rep["inputs"]
    a(f"  fills {i['fills']}  funding rows {i['funding_rows']}  span {i['first_fill']} .. {i['last_fill']}")
    for k, v in i["sha256"].items():
        a(f"  sha256 {k:15s} {v}")
    s = rep["settlements"]
    a(f"  settlements held {s['held']}  recorded {s['recorded_for_held']}  missing {s['missing']}  ambiguous {s['ambiguous']}  "
      f"unattributed funding rows {s['funding_rows_unattributed']}")
    for liq, f in rep["fees"].items():
        a(f"  fees {liq}: " + (f"n {f['n']}  mean {f['mean_bps']:.3f} bps  range {f['min_bps']:.3f}..{f['max_bps']:.3f}" if f["n"] else "no fills"))
    c = rep["actual_vs_assumed"]["round_trip_fee_bps"]
    a(f"  round-trip fee bps: actual {c['actual_from_records'] if c['actual_from_records'] is None else format(c['actual_from_records'], '.2f')}"
      f" vs assumed {c['assumed_by_repository']} ({c['assumed_tag']})")
    a("  ROUND TRIPS")
    for t in rep["round_trips"]:
        extra = ""
        if t["status"] == "COMPLETE":
            extra = f"  net {t['net_bps_of_notional']:.2f} bps (funding {t['funding_bps_of_notional']:.2f}, basis {t['basis_bps_of_notional']:.2f}, fees -{t['fee_bps_of_notional']:.2f})"
        elif "cash_only_usd" in t:
            extra = f"  cash-only {t['cash_only_usd']:.2f} USD (excludes basis)"
        a(f"    {t['start']} .. {t['end']}  {t['status']}{extra}")
    e = rep["economics"]
    if e["tag"] == PROVENANCE:
        a(f"  ECONOMICS over {e['complete_round_trips']} complete round trip(s), {e['settlements_in_them']} settlements: net {e['net_usd']:.2f} USD "
          f"(funding {e['funding_usd']:.2f}, basis {e['basis_pnl_usd']:.2f}, fees -{e['fees_usd']:.2f}); mean {e['mean_net_bps_of_notional']:.2f} bps per round trip")
        a("    " + (f"95% interval {e['interval'][0]:.2f}..{e['interval'][1]:.2f} bps ({e['reading']})" if e.get("interval") else e["reason"]))
    else:
        a(f"  ECONOMICS UNKNOWN: {e['reading']}")
    su = rep["sufficiency"]
    a(f"  sufficiency: {su['settlements_recorded_in_complete_round_trips']}/{su['engine_floor_settlements']} settlements, "
      f"{su['complete_round_trips']}/{su['engine_floor_round_trips']} round trips ({su['reading']})")
    a("  FLAGS")
    for f in rep["flags"] or []:
        a(f"    [{f['severity']}] {f['code']} x{f['count']}: {f['detail']}" + (f"  e.g. {', '.join(map(str, f['examples']))}" if f["examples"] else ""))
    if not rep["flags"]:
        a("    none")
    a("  WHAT WOULD UPGRADE THIS")
    for u in rep["what_would_upgrade_this"]:
        a(f"    - {u}")
    return "\n".join(L)


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Offline review of owner-supplied account records (no network, no orders).")
    ap.add_argument("--fills", required=True)
    ap.add_argument("--funding", required=True)
    ap.add_argument("--symbol", default="BTCUSDT")
    ap.add_argument("--taker-bps", type=float)
    ap.add_argument("--maker-bps", type=float)
    ap.add_argument("--capital-usd", type=float)
    ap.add_argument("--public-funding")
    ap.add_argument("--perp-4h")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    try:
        rep = run(a.fills, a.funding, symbol=a.symbol, taker_bps=a.taker_bps, maker_bps=a.maker_bps, capital_usd=a.capital_usd,
                  public_path=a.public_funding, perp_path=a.perp_4h)
    except (RecordError, CT.DataError, OSError) as exc:
        print(f"account_review: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(rep, indent=2, sort_keys=True) if a.json else render(rep))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
