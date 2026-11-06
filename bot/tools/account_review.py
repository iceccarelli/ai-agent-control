#!/usr/bin/env python3
"""Offline review of owner-supplied ACCOUNT records for the BTC funding carry. Reads two files, makes no request, places no order.

    python3 tools/account_review.py --fills FILLS.jsonl --funding FUNDING.jsonl [--symbol BTCUSDT]
                                    [--taker-bps X --maker-bps Y] [--capital-usd C]
                                    [--public-funding F.json.gz] [--perp-4h K.json.gz] [--spot-4h S.json.gz] [--json]

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
    row; a row at a moment the book was flat is unattributed; a settlement is ambiguous only if the book opened, closed or flipped inside the minute either side (counted
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
import io
import json
import math
import os
import re
import stat
import sys
import zlib
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
MARK_TOLERANCE = 0.005                  # payment-implied mark vs the 4h bar open (the settlement instant IS a bar open): a consistency screen
POSITION_REL_TOL = 1e-3
POSITION_ABS_TOL = 1e-6
RATE_TOL_BPS = 0.002
FEE_TOL_BPS = 0.25
MIN_TS_MS, MAX_TS_MS = 10 ** 12, 4 * 10 ** 12
MIN_QTY, MAX_QTY = 1e-6, 1e5           # BTC: below one micro-BTC is dust, above 100,000 is a unit error
MAX_PRICE = 1e7
MAX_USD = 1e9
MAX_FEE_FRACTION = 0.05                 # a fee above 5 % of the fill's notional is a unit error, not a tier
SPOT_REF_MAX_DEVIATION = 0.005          # the spot mid and the perp fill of one hedge leg differ by basis: the committed history's largest 4h basis is 23.5 bps
BASIS_SCREEN_BPS = 30.0                 # a claimed basis must be within this of the public 4h-bar-open basis of the fill's bar
PLACEHOLDER_REL = 1e-9                  # spot_ref equal to the fill price (to rounding) is a copy of the price, not a spot mid
MAX_DECOMPRESSED_BYTES = 512 * 1024 * 1024
PRICE_RANGE_TOL = 0.003                 # a fill must sit inside the public 4h bar's low..high, widened by this
RATE_PRINT_NOISE = 5e-9                 # funding rates are printed to 8 decimals: the rounding a payment may legitimately carry
MIN_MARK_CHECK_RATE = 1e-6              # below 0.01 bps the implied mark is dominated by the rate's rounding: bound the payment instead
MAX_BYTES = 256 * 1024 * 1024
BAR_MS = CT.BAR_MS
PROTECTED_SUFFIXES = (".db", ".db-wal", ".db-shm", ".db-journal", ".sqlite", ".sqlite3", ".sqlite-wal", ".sqlite-shm", ".sqlite3-wal",
                      ".sqlite3-shm", ".pem", ".key", ".p12", ".pfx", ".env", ".keystore", ".jks")
PROTECTED_SUBSTRINGS = ("trading_state", "testnet_session_evidence", "id_rsa", "id_ed25519", "id_ecdsa", ".netrc")
#: whole name TOKENS (split on anything that is not a letter or digit): 'secret_fills.csv' is a secret, 'secretary.csv' and 'credit.csv' are not
PROTECTED_TOKENS = ("secret", "secrets", "credential", "credentials", "evidence", "env", "apikey", "apitoken", "passwd", "password", "passwords")
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

_NUM = re.compile(r"^[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?$", re.ASCII)       # ASCII digits only


class RecordError(ValueError):
    """The supplied records cannot be read as evidence at all."""


# ---------------------------------------------------------------- reading

def _refuse_protected(path: str) -> str:
    """Never read runtime state, evidence or secrets, whatever the caller names. Returns the resolved path. Symlinks resolve first; a
    file with more than one hard link is refused (a hard link can give a protected file another name); the name tests are substring
    tests on the resolved basename (so ``trading_state.db.jsonl`` and ``testnet_session_evidence_2026.jsonl`` are protected too)."""
    real = os.path.realpath(path)
    base = os.path.basename(real).lower()
    parts = [p.lower() for p in real.split(os.sep) if p]
    toks = [t for t in re.split(r"[^a-z0-9]+", base) if t]
    named = (base.endswith(PROTECTED_SUFFIXES) or base.startswith(".env") or any(w in base for w in PROTECTED_SUBSTRINGS)
             or any(t in PROTECTED_TOKENS for t in toks) or ("api" in toks and ("key" in toks or "token" in toks))
             or any(p == "runs" or p.startswith(("runs-", "runs_", "runs.")) for p in parts) or ("state" in parts and "bot" in parts))
    linked = False
    try:
        linked = os.stat(real).st_nlink > 1
    except OSError:
        pass
    if named or linked:
        raise RecordError(f"refusing to read {os.path.basename(real)!r}: runtime state, evidence and credentials are never inputs"
                          + (" (the file has more than one hard link)" if linked and not named else ""))
    return real


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _no_duplicate_keys(pairs: List[Tuple[str, Any]]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for k, v in pairs:
        if k in out:
            raise RecordError(f"duplicate key {k!r} in one record: which value is meant is not decidable")
        out[k] = v
    return out


def _bad_constant(name: str) -> Any:
    raise RecordError(f"{name} is not a number")


def _read_bytes(real: str, label: str, strict_links: bool = True) -> bytes:
    """Read a regular file once. The checks run on the OPEN file (fstat), so a hard link made after the name check is still seen."""
    if not os.path.isfile(real):
        raise RecordError(f"{label!r} is not a file")
    with open(real, "rb") as fh:
        st = os.fstat(fh.fileno())
        if not stat.S_ISREG(st.st_mode):
            raise RecordError(f"{label!r} is not a regular file")
        if strict_links and st.st_nlink > 1:
            raise RecordError(f"refusing to read {label!r}: the file has more than one hard link")
        data = fh.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise RecordError(f"{label!r} is larger than {MAX_BYTES // (1024 * 1024)} MB")
    return data


def read_rows(path: str) -> Tuple[List[Dict[str, Any]], str]:
    """Rows of a .jsonl or .csv file (nothing else) and the sha256 of the very bytes that were parsed (one read, so no race)."""
    real = _refuse_protected(path)
    label = os.path.basename(path)
    lower = real.lower()
    if not lower.endswith((".csv", ".jsonl", ".ndjson")):
        raise RecordError(f"{label!r}: only .jsonl, .ndjson and .csv are read")
    data = _read_bytes(real, label)
    try:
        text = data.decode("utf-8-sig")                     # a BOM from a spreadsheet export is tolerated
    except UnicodeDecodeError as exc:
        raise RecordError(f"{label!r} is not UTF-8 text") from exc
    rows: List[Dict[str, Any]] = []
    if lower.endswith(".csv"):
        try:
            reader = csv.DictReader(io.StringIO(text), strict=True)
            header = list(reader.fieldnames or [])
            if len(set(header)) != len(header):
                raise RecordError(f"{label}: duplicate column names in the header: which column is meant is not decidable")
            for n, r in enumerate(reader, 2):
                if None in r or any(v is None for v in r.values()):
                    raise RecordError(f"{label} line {n}: the row has a different number of fields from the header")
                rows.append(dict(r))
        except csv.Error as exc:
            raise RecordError(f"{label}: not valid CSV ({exc})") from exc
    else:
        for n, line in enumerate(text.split("\n"), 1):          # NOT splitlines(): U+2028 and friends are not record separators
            if not line.strip():
                continue
            try:
                obj = json.loads(line, object_pairs_hook=_no_duplicate_keys, parse_constant=_bad_constant)
            except json.JSONDecodeError as exc:
                raise RecordError(f"{label} line {n}: not JSON ({exc.msg})") from exc
            except RecursionError as exc:
                raise RecordError(f"{label} line {n}: nested too deeply") from exc
            except RecordError as exc:
                raise RecordError(f"{label} line {n}: {exc}") from exc
            if not isinstance(obj, dict):
                raise RecordError(f"{label} line {n}: not an object")
            rows.append(obj)
    if not rows:
        raise RecordError(f"{label!r} holds no records")
    return rows, _sha(data)


# ---------------------------------------------------------------- typed parsing

def _num(v: Any, name: str, where: str) -> float:
    if isinstance(v, bool):
        raise RecordError(f"{where}: {name} is a boolean")
    try:
        if isinstance(v, (int, float)):
            x = float(v)
        elif isinstance(v, str) and _NUM.match(v.strip()):
            x = float(v.strip())
        else:
            raise RecordError(f"{where}: {name} is not a number ({v!r})")
    except OverflowError as exc:
        raise RecordError(f"{where}: {name} is beyond any plausible magnitude") from exc
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


def _bounded(x: float, lo: float, hi: float, name: str, where: str) -> float:
    if not (lo <= x <= hi):
        raise RecordError(f"{where}: {name} {x!r} is outside the plausible range {lo}..{hi} (a unit error?)")
    return x


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

    @property
    def spot_ref_is_copy(self) -> bool:
        """A spot reference equal to the fill price (to rounding) is the price copied into the column, not a spot mid: the public basis
        is never exactly zero, so a zero basis on every fill is a placeholder and proves nothing."""
        return self.spot_ref is not None and abs(self.price / self.spot_ref - 1.0) <= PLACEHOLDER_REL


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
    ignored = sorted({str(k) for r in rows for k in r} - known)
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
        qty = _bounded(_num(r.get("qty"), "qty", w), MIN_QTY, MAX_QTY, "qty", w)
        price = _bounded(_num(r.get("price"), "price", w), 1e-6, MAX_PRICE, "price", w)
        fee = _bounded(_num(r.get("fee_usd"), "fee_usd", w), -MAX_USD, MAX_USD, "fee_usd", w)
        if abs(fee) > MAX_FEE_FRACTION * qty * price:
            raise RecordError(f"{w}: fee_usd {fee} is more than {MAX_FEE_FRACTION:.0%} of the fill's notional: a unit error, not a fee tier")
        opt: Dict[str, Optional[float]] = {}
        for name in FILL_OPTIONAL:
            raw = r.get(name)
            if raw is None or (isinstance(raw, str) and not raw.strip()):
                opt[name] = None
            else:
                opt[name] = _bounded(_num(raw, name, w), 1e-6, MAX_PRICE, name, w)
        if opt["ref_price"] is not None and abs(opt["ref_price"] / price - 1.0) > 0.05:
            raise RecordError(f"{w}: ref_price is more than 5% from the fill price: a decision-time mid is not that far from the fill")
        out.append(Fill(eid, _ts(r.get("ts_ms"), w), side, qty, price, fee, liq, opt["ref_price"], opt["spot_ref"]))
    out.sort(key=lambda f: (f.ts_ms, f.exec_id))
    return out, ignored


def parse_funding(rows: Sequence[Mapping[str, Any]], symbol: str) -> Tuple[List[FundingRow], List[str]]:
    _missing(rows, FUNDING_REQUIRED, "funding")
    ignored = sorted({str(k) for r in rows for k in r} - set(FUNDING_REQUIRED))
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
        out.append(FundingRow(eid, t, grid, _bounded(_num(r.get("position_qty"), "position_qty", w), -MAX_QTY, MAX_QTY, "position_qty", w), rate,
                              _bounded(_num(r.get("payment_usd"), "payment_usd", w), -MAX_USD, MAX_USD, "payment_usd", w)))
    out.sort(key=lambda f: f.grid_ms)
    return out, ignored


# ---------------------------------------------------------------- the book rebuilt from the fills

class Book:
    """Signed perpetual position over time, rebuilt only from the fills. Fills with the SAME millisecond are one event: their net is
    what the position becomes, so the order in which an exporter happened to list them (or sort their ids) changes nothing."""

    def __init__(self, fills: Sequence[Fill]):
        self.fills = list(fills)
        self.groups: List[List[Fill]] = []
        for f in self.fills:
            if self.groups and self.groups[-1][0].ts_ms == f.ts_ms:
                self.groups[-1].append(f)
            else:
                self.groups.append([f])
        self.times = [g[0].ts_ms for g in self.groups]
        cum, q = [], 0.0
        for g in self.groups:
            q += sum(f.signed_qty for f in g)
            if abs(q) <= DUST_QTY:
                q = 0.0
            cum.append(q)
        self.cum = cum

    def qty_at(self, t: int) -> float:
        """Position after every fill with ts <= t."""
        i = bisect.bisect_right(self.times, t)
        return self.cum[i - 1] if i else 0.0

    def window_positions(self, t: int) -> List[float]:
        """Every position the book held at some instant of [t - slack, t + slack]: the one entering the window and the one after each
        event inside it."""
        lo, hi = bisect.bisect_left(self.times, t - SLACK_MS), bisect.bisect_right(self.times, t + SLACK_MS)
        return [self.qty_at(t - SLACK_MS)] + self.cum[lo:hi]

    def lifecycles(self) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        cur: Optional[Dict[str, Any]] = None
        for g, q in zip(self.groups, self.cum):
            if cur is None:
                cur = {"start_ms": g[0].ts_ms, "end_ms": None, "fills": [], "min_qty": 0.0, "max_qty": 0.0}
            cur["fills"].extend(g)
            cur["min_qty"], cur["max_qty"] = min(cur["min_qty"], q), max(cur["max_qty"], q)
            if q == 0.0:
                cur["end_ms"] = g[0].ts_ms
                out.append(cur)
                cur = None
        if cur is not None:
            out.append(cur)
        return out


def classify_settlement(book: Book, t: int) -> str:
    """held | flat | ambiguous. HELD when the book was in a position of one sign at EVERY instant of the minute either side, however many
    fills (rebalances, a tiny pair of trades) happened inside it: it was provably in the position through the settlement, a funding
    row is required and a missing one is a gap. FLAT when it held nothing throughout. AMBIGUOUS only when the book opened, closed or
    flipped inside that window, so its position at the settlement cannot be established from the fills."""
    pos = book.window_positions(t)
    if all(p < 0.0 for p in pos) or all(p > 0.0 for p in pos):
        return "held"
    if all(p == 0.0 for p in pos):
        return "flat"
    return "ambiguous"


def _close_enough(stated: float, actual: float) -> bool:
    return abs(stated - actual) <= max(POSITION_ABS_TOL, POSITION_REL_TOL * abs(actual))


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


#: Gaps that can hide a loss or a cost (a dropped settlement, an excluded trip, a basis that is not known): the review is INCOMPLETE.
STRUCTURAL_GAPS = ("MISSING_SETTLEMENT", "OPEN_LIFECYCLE", "NO_SPOT_REFERENCE", "SPOT_REF_IS_A_COPY_OF_THE_PRICE", "AMBIGUOUS_SETTLEMENT",
                   "FUNDING_ROW_IN_AMBIGUOUS_WINDOW", "ROUND_TRIP_WITHOUT_SETTLEMENT")
#: Gaps where the records were not compared with the public market or the stated tier: consistent among themselves, UNCHECKED outside.
#: Any GAP code that is not listed here counts as STRUCTURAL: an unclassified gap must never be the one that lets a verdict through.
CHECK_GAPS = ("RATE_CHECK_NOT_RUN", "RATE_CHECK_PARTIAL", "MARK_CHECK_NOT_RUN", "MARK_CHECK_PARTIAL", "PRICE_CHECK_NOT_RUN", "PRICE_CHECK_PARTIAL",
              "SPOT_REF_CHECK_NOT_RUN", "SPOT_REF_CHECK_PARTIAL", "FEE_TIER_NOT_STATED", "FEE_TIER_MISSING_FOR_CLASS", "PUBLIC_HISTORY_CALLER_SUPPLIED")


def review(fills: Sequence[Fill], funding: Sequence[FundingRow], *, taker_bps: Optional[float] = None, maker_bps: Optional[float] = None,
           capital_usd: Optional[float] = None, public: Optional[Mapping[int, float]] = None,
           bar_open: Optional[Mapping[int, float]] = None, bar_range: Optional[Mapping[int, Tuple[float, float]]] = None,
           spot_range: Optional[Mapping[int, Tuple[float, float]]] = None, spot_open: Optional[Mapping[int, float]] = None,
           caller_histories: Sequence[str] = (), digests: Optional[Mapping[str, str]] = None,
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

    # ---- which settlements was the book provably in a position through
    first_ms, last_fill_ms = fills[0].ts_ms, fills[-1].ts_ms
    horizon = max(last_fill_ms, funding[-1].grid_ms if funding else last_fill_ms)
    grid = list(range(((first_ms + SETTLE_MS - 1) // SETTLE_MS) * SETTLE_MS, horizon + 1, SETTLE_MS))
    cls = {t: classify_settlement(book, t) for t in grid}
    held = [t for t in grid if cls[t] == "held"]
    ambiguous = [t for t in grid if cls[t] == "ambiguous"]
    missing = [t for t in held if t not in funding_by_grid]
    off_book = [f for f in funding if cls.get(f.grid_ms) == "flat" or f.grid_ms not in cls]
    in_ambiguous = [f for f in funding if cls.get(f.grid_ms) == "ambiguous"]
    _flag(flags, "MISSING_SETTLEMENT", SEVERITY_GAP, len(missing), "the book was in a position through these settlements but the funding file has no row",
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
    pos_bad: List[int] = []
    rate_bad: List[int] = []
    mark_bad: List[int] = []
    sign_bad: List[int] = []
    rate_unchecked = mark_unchecked = sign_n = sign_agree = 0
    for f in funding:
        if cls.get(f.grid_ms) != "held":
            continue
        if not any(_close_enough(f.position_qty, q) for q in book.window_positions(f.grid_ms)):
            pos_bad.append(f.grid_ms)
        pos = f.position_qty
        if public is not None and f.grid_ms in public:
            if abs(f.rate * 1e4 - public[f.grid_ms]) > RATE_TOL_BPS:
                rate_bad.append(f.grid_ms)
        else:
            rate_unchecked += 1
        expect_sign = -pos * f.rate
        if abs(expect_sign) > 0 and abs(f.payment_usd) > 0:
            sign_n += 1
            if (expect_sign > 0) == (f.payment_usd > 0):
                sign_agree += 1
            else:
                sign_bad.append(f.grid_ms)
        mark = bar_open.get(f.grid_ms) if bar_open is not None else None
        if mark and abs(pos) > DUST_QTY:
            if abs(f.rate) >= MIN_MARK_CHECK_RATE:
                implied_mark = f.payment_usd / (-pos * f.rate)
                ok = implied_mark > 0 and abs(implied_mark - mark) / mark <= MARK_TOLERANCE
            else:                                           # a (near-)zero rate pays (near-)nothing: any real payment is wrong
                ok = abs(f.payment_usd) <= abs(pos) * mark * (abs(f.rate) + RATE_PRINT_NOISE) * (1 + MARK_TOLERANCE) + 1e-6
            if not ok:
                mark_bad.append(f.grid_ms)
        else:
            mark_unchecked += 1
    inverted = sign_n >= 3 and sign_agree == 0
    _flag(flags, "SIGN_CONVENTION_INVERTED", SEVERITY_HARD, 1 if inverted else 0,
          "every funding payment has the opposite sign to position x rate: the file's sign convention is not 'received > 0'; it is NOT flipped here")
    if not inverted:
        _flag(flags, "FUNDING_SIGN_WRONG", SEVERITY_HARD, len(sign_bad), "payment sign contradicts position x rate", [_iso(t) for t in sign_bad])
    _flag(flags, "POSITION_MISMATCH", SEVERITY_HARD, len(pos_bad), "the position the funding row states matches no position the fills put the book in "
          "at that settlement", [_iso(t) for t in pos_bad])
    _flag(flags, "RATE_DISAGREES_WITH_PUBLIC", SEVERITY_HARD, len(rate_bad), "the funding rate differs from the public print at that settlement",
          [_iso(t) for t in rate_bad])
    _flag(flags, "PAYMENT_DISAGREES_WITH_MARK", SEVERITY_HARD, len(mark_bad),
          f"payment is not position x rate x the 4h bar open at the settlement (within {MARK_TOLERANCE:.0%}; a zero rate pays nothing)", [_iso(t) for t in mark_bad])

    # ---- fills against the public market, fees against the stated tier
    price_bad: List[str] = []
    price_unchecked = 0
    spot_implausible: List[str] = []
    spot_outside: List[str] = []
    spot_unchecked = spot_present = 0
    basis_off: List[str] = []
    basis_unchecked = 0
    copies: List[str] = []
    fee_rows: Dict[str, List[float]] = {"taker": [], "maker": []}
    fee_bad: List[str] = []
    for fl in fills:
        eff = fl.fee_usd / fl.notional * 1e4
        fee_rows[fl.liquidity].append(eff)
        sched = taker_bps if fl.liquidity == "taker" else maker_bps
        if sched is not None and abs(eff - sched) > FEE_TOL_BPS:
            fee_bad.append(fl.exec_id)
        bar = fl.ts_ms - fl.ts_ms % BAR_MS
        rng = bar_range.get(bar) if bar_range is not None else None
        if rng is None:
            price_unchecked += 1
        elif not (rng[0] * (1 - PRICE_RANGE_TOL) <= fl.price <= rng[1] * (1 + PRICE_RANGE_TOL)):
            price_bad.append(fl.exec_id)
        if fl.spot_ref is not None:
            spot_present += 1
            if abs(fl.price / fl.spot_ref - 1.0) > SPOT_REF_MAX_DEVIATION:
                spot_implausible.append(fl.exec_id)
            if fl.spot_ref_is_copy:
                copies.append(fl.exec_id)
            po, so = (bar_open.get(bar) if bar_open is not None else None), (spot_open.get(bar) if spot_open is not None else None)
            if po and so:
                public_basis = (po - so) / so * 1e4
                if abs((fl.price / fl.spot_ref - 1.0) * 1e4 - public_basis) > BASIS_SCREEN_BPS:
                    basis_off.append(fl.exec_id)
            else:
                basis_unchecked += 1
            srng = spot_range.get(bar) if spot_range is not None else None
            if srng is None:
                spot_unchecked += 1
            elif not (srng[0] * (1 - PRICE_RANGE_TOL) <= fl.spot_ref <= srng[1] * (1 + PRICE_RANGE_TOL)):
                spot_outside.append(fl.exec_id)
    _flag(flags, "FEE_DISAGREES_WITH_TIER", SEVERITY_HARD, len(fee_bad),
          f"a fill's fee differs from the stated tier by more than {FEE_TOL_BPS} bps", fee_bad)
    _flag(flags, "FILL_PRICE_OUTSIDE_PUBLIC_RANGE", SEVERITY_HARD, len(price_bad),
          f"a fill price is outside the public 4h bar's low..high (widened {PRICE_RANGE_TOL:.1%})", price_bad)
    _flag(flags, "SPOT_REF_IMPLAUSIBLE", SEVERITY_HARD, len(spot_implausible),
          f"spot_ref differs from the perp fill price by more than {SPOT_REF_MAX_DEVIATION:.0%}: a basis is bps, not percent", spot_implausible)
    _flag(flags, "BASIS_OFF_PUBLIC_BAR", SEVERITY_HARD, len(basis_off),
          f"the basis a fill's spot_ref implies is more than {BASIS_SCREEN_BPS:g} bps from the public 4h-bar-open basis of that bar "
          f"(the committed history's largest is 23.5 bps)", basis_off)
    _flag(flags, "SPOT_REF_OUTSIDE_PUBLIC_RANGE", SEVERITY_HARD, len(spot_outside),
          f"spot_ref is outside the public spot 4h bar's low..high (widened {PRICE_RANGE_TOL:.1%})", spot_outside)
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
        row: Dict[str, Any] = {"start": _iso(lc["start_ms"]), "end": _iso(lc["end_ms"]) if lc["end_ms"] is not None else None, "fills": len(fl)}
        sells = [f for f in fl if f.side == "Sell"]
        if lc["max_qty"] > DUST_QTY:
            row["status"] = "EXCLUDED_LONG_EXPOSURE"
            trips.append(row)
            continue
        if lc["end_ms"] is None:
            row["status"] = "EXCLUDED_OPEN"
            trips.append(row)
            continue
        if not sells:
            row["status"] = "EXCLUDED_NO_SHORT_ENTRY"
            trips.append(row)
            continue
        window = held[bisect.bisect_right(held, lc["start_ms"]):bisect.bisect_left(held, lc["end_ms"])]      # held is sorted: O(log n) per lifecycle
        got = [funding_by_grid[t] for t in window if t in funding_by_grid]
        miss = [t for t in window if t not in funding_by_grid]
        amb = ambiguous[bisect.bisect_left(ambiguous, lc["start_ms"] - SLACK_MS):bisect.bisect_right(ambiguous, lc["end_ms"] + SLACK_MS)]
        notional = sum(f.notional for f in sells)
        fees_usd = sum(f.fee_usd for f in fl)
        funding_usd = sum(f.payment_usd for f in got)
        perp_pnl = sum(f.notional for f in sells) - sum(f.notional for f in fl if f.side == "Buy")
        have_spot = all(f.spot_ref and not f.spot_ref_is_copy for f in fl)
        basis = (sum(f.qty * (f.price - f.spot_ref) for f in sells) - sum(f.qty * (f.price - f.spot_ref) for f in fl if f.side == "Buy")
                 ) if have_spot else None
        hold_days = (lc["end_ms"] - lc["start_ms"]) / DAY_MS
        row.update({"settlements_held": len(window), "settlements_recorded": len(got), "settlements_missing": len(miss),
                    "settlements_ambiguous": len(amb), "hold_days": hold_days, "entry_notional_usd": notional,
                    "funding_usd": funding_usd, "fees_usd": fees_usd, "perp_pnl_usd": perp_pnl, "basis_pnl_usd": basis,
                    "fee_bps_of_notional": fees_usd / notional * 1e4,
                    "funding_bps_of_notional": funding_usd / notional * 1e4})
        if not all(math.isfinite(row[k]) for k in ("entry_notional_usd", "funding_usd", "fees_usd", "perp_pnl_usd", "fee_bps_of_notional",
                                                    "funding_bps_of_notional")) or (basis is not None and not math.isfinite(basis)):
            row["status"] = "EXCLUDED_NON_FINITE"
        elif not window:
            row["status"] = "EXCLUDED_NO_SETTLEMENT_SPANNED"
            row["note"] = "closed without spanning any settlement: not a carry round trip, but its fees and basis are real costs, shown here and not hidden"
        elif miss:
            row["status"] = "INCOMPLETE_FUNDING"
        elif basis is None:
            row["status"] = "NET_UNKNOWN_NO_SPOT_REFERENCE"
            if any(f.spot_ref_is_copy for f in fl):
                row["note"] = "a spot_ref equal to the fill price is a copy of the price, not a spot mid: the basis is unknown"
            row["cash_only_usd"] = funding_usd - fees_usd
            row["cash_only_note"] = "funding - fees only; EXCLUDES basis P&L, so it is not a net edge"
        else:
            net = funding_usd + basis - fees_usd
            row.update({"status": "COMPLETE", "net_usd": net, "net_bps_of_notional": net / notional * 1e4,
                        "basis_bps_of_notional": basis / notional * 1e4})
            if hold_days >= 1.0:                                    # annualising a few hours or minutes is not a rate
                row["annualised_pct_on_notional"] = (net / notional) * 365.0 / hold_days * 100.0
                if capital_usd:
                    row["annualised_pct_on_stated_capital"] = (net / capital_usd) * 365.0 / hold_days * 100.0
            else:
                row["annualised_pct_on_notional"] = None
                row["annualisation_note"] = "held under one day: not annualised"
            if not all(math.isfinite(row[k]) for k in ("net_usd", "net_bps_of_notional", "basis_bps_of_notional")):
                row["status"] = "EXCLUDED_NON_FINITE"
        trips.append(row)
    _flag(flags, "NON_FINITE_RESULT", SEVERITY_HARD, sum(1 for t in trips if t["status"] == "EXCLUDED_NON_FINITE"),
          "a round trip's arithmetic is not finite: the records are outside any plausible range")
    _flag(flags, "ROUND_TRIP_WITHOUT_SETTLEMENT", SEVERITY_GAP, sum(1 for t in trips if t["status"] == "EXCLUDED_NO_SETTLEMENT_SPANNED"),
          "closed round trips that spanned no settlement: excluded from the carry economics, but their costs are real and are shown per trip")
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
                     "notional_weighted_net_bps": sum(t["net_usd"] for t in complete) / tot_n * 1e4,
                     "net_is_upper_bound": True,
                     "excludes": ["spot-leg fees and spread (an overlay trades none; an acquisition would)", "borrow and financing", "transfers and conversion",
                                  "operational cost", "the opportunity cost of the BTC held", "tax", "any cost not in the fills and funding files"],
                     "basis_unverifiable_bps_per_round_trip": 2 * BASIS_SCREEN_BPS,
                     "basis_reading": (f"the basis comes from the supplier's spot_ref values; the public-bar screen cannot exclude an error of up to "
                                       f"{BASIS_SCREEN_BPS:g} bps on each of the entry and the exit, so a round-trip net smaller than "
                                       f"{2 * BASIS_SCREEN_BPS:g} bps is not distinguishable from zero by this review")}
    else:
        economics = {"tag": "UNKNOWN", "reading": "no round trip has every settlement recorded AND a spot reference on every fill: "
                                                  "no net is stated (funding - fees alone is shown per lifecycle, marked as excluding basis)"}

    # ---- actual against assumed
    fee_trips = [t for t in trips if "fee_bps_of_notional" in t and t["status"] != "EXCLUDED_NON_FINITE"]
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
    # ---- what was NOT compared with anything outside the file: each is a named gap, and any of them stops the review short of CONSISTENT
    if bar_open is None:
        _flag(flags, "MARK_CHECK_NOT_RUN", SEVERITY_GAP, 1, "no 4h price history was supplied: the payment-vs-mark check did not run")
    else:
        _flag(flags, "MARK_CHECK_PARTIAL", SEVERITY_GAP, mark_unchecked, "settlements with no 4h bar in the supplied history were not mark-checked")
    if public is None:
        _flag(flags, "RATE_CHECK_NOT_RUN", SEVERITY_GAP, 1, "no public funding history was supplied: the rate was not compared with the public print")
    else:
        _flag(flags, "RATE_CHECK_PARTIAL", SEVERITY_GAP, rate_unchecked, "settlements the public history does not cover were not rate-checked")
    if bar_range is None:
        _flag(flags, "PRICE_CHECK_NOT_RUN", SEVERITY_GAP, 1, "no 4h price history was supplied: fill prices were not compared with the public market")
    else:
        _flag(flags, "PRICE_CHECK_PARTIAL", SEVERITY_GAP, price_unchecked, "fills in a period the supplied price history does not cover were not price-checked")
    if spot_present and spot_range is None:
        _flag(flags, "SPOT_REF_CHECK_NOT_RUN", SEVERITY_GAP, 1, "no public spot history was supplied: spot_ref was only checked against the perp price")
    elif spot_present:
        _flag(flags, "SPOT_REF_CHECK_PARTIAL", SEVERITY_GAP, spot_unchecked, "spot_ref values in a period the spot history does not cover were not range-checked")
    classes = [c for c in ("taker", "maker") if fee_rows[c]]
    if taker_bps is None and maker_bps is None:
        _flag(flags, "FEE_TIER_NOT_STATED", SEVERITY_GAP, 1, "no fee tier was stated: fees are reported as charged but not compared with the tier")
    else:
        no_tier = [c for c in classes if (taker_bps if c == "taker" else maker_bps) is None]
        _flag(flags, "FEE_TIER_MISSING_FOR_CLASS", SEVERITY_GAP, len(no_tier), "fills of a liquidity class with no stated tier were not compared with one", no_tier)
    _flag(flags, "AMBIGUOUS_SETTLEMENT", SEVERITY_GAP, len(ambiguous), "the book opened, closed or flipped within a minute of the settlement: its position at "
          "the settlement cannot be established, so it is counted as neither held nor missing", [_iso(t) for t in ambiguous])
    _flag(flags, "FUNDING_ROW_IN_AMBIGUOUS_WINDOW", SEVERITY_GAP, len(in_ambiguous), "a funding row at an ambiguous settlement: kept out of the totals",
          [_iso(f.grid_ms) for f in in_ambiguous])
    _flag(flags, "SPOT_REF_IS_A_COPY_OF_THE_PRICE", SEVERITY_GAP, len(copies),
          "fills whose spot_ref equals the fill price: a copy of the price in the column, not a spot mid; those round trips have no basis", copies)
    _flag(flags, "PUBLIC_HISTORY_CALLER_SUPPLIED", SEVERITY_GAP, len(caller_histories),
          "these 'public' histories are not the committed Bybit files: their authenticity is the supplier's, so the checks against them prove "
          "consistency with that file only", list(caller_histories))
    _flag(flags, "NO_SPOT_REFERENCE", SEVERITY_GAP, sum(1 for t in trips if t["status"] == "NET_UNKNOWN_NO_SPOT_REFERENCE"),
          "closed round trips whose fills lack a spot_ref: basis P&L, hence the net, is UNKNOWN")

    gap_codes = {f["code"] for f in flags if f["severity"] == SEVERITY_GAP}
    if any(f["severity"] == SEVERITY_HARD for f in flags):
        verdict = "REJECTED_RECORDS_CONTRADICT"
    elif not complete or (gap_codes - set(CHECK_GAPS)):         # anything not explicitly a check gap is structural
        verdict = "INCOMPLETE"
    elif gap_codes & set(CHECK_GAPS):
        verdict = "CONSISTENT_UNCHECKED_AGAINST_PUBLIC"
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
            "public funding and 4h price history covering the whole span, so every rate, payment and price is compared with the market",
            "the sha256 values above archived off the machine at the time the review is issued"],
    }


def _iso(ms: int) -> str:
    return datetime.datetime.fromtimestamp(ms / 1000.0, tz=datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------- orchestration and CLI

def _read_history(path: Optional[str], default_name: str, use_default: bool) -> Tuple[Any, Optional[str]]:
    """The parsed JSON of a public history file and its sha256, or ``(None, None)`` when none applies. A path the caller NAMED goes through
    the protected-path refusal and an extension check; the committed default history does not (it is the repository's own file, and a
    checkout under a directory called ``runs`` must not block it)."""
    if path:
        real = _refuse_protected(path)
        if not real.lower().endswith((".json", ".json.gz")):
            raise RecordError(f"{os.path.basename(path)!r}: a public history is a .json or .json.gz file")
    elif use_default:
        real = os.path.join(CT.STUDY, default_name)
        if not os.path.isfile(real):
            return None, None
    else:
        return None, None
    data = _read_bytes(real, os.path.basename(real), strict_links=bool(path))
    try:
        if real.lower().endswith(".gz"):
            d = zlib.decompressobj(16 + zlib.MAX_WBITS)
            raw = d.decompress(data, MAX_DECOMPRESSED_BYTES + 1)
            if len(raw) > MAX_DECOMPRESSED_BYTES or d.unconsumed_tail:
                raise RecordError(f"{os.path.basename(real)!r} expands to more than {MAX_DECOMPRESSED_BYTES // (1024 * 1024)} MB")
        else:
            raw = data
        return json.loads(raw), _sha(data)
    except RecordError:
        raise
    except (OSError, EOFError, ValueError, RecursionError, zlib.error) as exc:
        raise RecordError(f"{os.path.basename(real)!r} is not a readable public history ({type(exc).__name__})") from exc


def _ranges(rows: Any) -> Dict[int, Tuple[float, float]]:
    """``{bar start: (low, high)}`` from raw 4h kline rows ``[start, open, high, low, close, ...]`` (already validated by parse_klines)."""
    out: Dict[int, Tuple[float, float]] = {}
    for r in rows:
        out[int(r[0])] = (float(r[3]), float(r[2]))
    return out


def _committed_digest(name: str) -> Optional[str]:
    p = os.path.join(CT.STUDY, name)
    if not os.path.isfile(p):
        return None
    try:
        return _sha(_read_bytes(p, name, strict_links=False))
    except (OSError, RecordError):
        return None


def run(fills_path: str, funding_path: str, *, symbol: str = "BTCUSDT", taker_bps: Optional[float] = None, maker_bps: Optional[float] = None,
        capital_usd: Optional[float] = None, public_path: Optional[str] = None, perp_path: Optional[str] = None, spot_path: Optional[str] = None,
        use_default_history: bool = True) -> Dict[str, Any]:
    f_rows, f_dig = read_rows(fills_path)
    g_rows, g_dig = read_rows(funding_path)
    fills, f_ign = parse_fills(f_rows, symbol)
    funding, g_ign = parse_funding(g_rows, symbol)
    digests = {"fills": f_dig, "funding": g_dig}
    default_ok = use_default_history and symbol == "BTCUSDT"
    public = bar_open = bar_range = spot_range = spot_open = None
    caller: List[str] = []
    names = {"public_funding": "funding_BTCUSDT.json.gz", "perp_4h": "linear_BTCUSDT_240.json.gz", "spot_4h": "spot_BTCUSDT_240.json.gz"}
    try:
        pub, d = _read_history(public_path, names["public_funding"], default_ok)
        if pub is not None:
            ts, bps = CT.parse_funding(pub)
            public, digests["public_funding"] = dict(zip(ts, bps)), d
        perp, d = _read_history(perp_path, names["perp_4h"], default_ok)
        if perp is not None:
            bars = CT.parse_klines(perp)
            bar_open, bar_range, digests["perp_4h"] = {t: o for t, (o, _c) in bars.items()}, _ranges(perp), d
        spot, d = _read_history(spot_path, names["spot_4h"], default_ok)
        if spot is not None:
            bars = CT.parse_klines(spot)
            spot_open, spot_range, digests["spot_4h"] = {t: o for t, (o, _c) in bars.items()}, _ranges(spot), d
    except CT.DataError as exc:
        raise RecordError(f"price/funding history unusable: {exc}") from exc
    for given, key in ((public_path, "public_funding"), (perp_path, "perp_4h"), (spot_path, "spot_4h")):
        if given and digests.get(key) != _committed_digest(names[key]):
            caller.append(f"{key} ({os.path.basename(given)}, sha256 {str(digests.get(key))[:16]}...)")
    return review(fills, funding, taker_bps=taker_bps, maker_bps=maker_bps, capital_usd=capital_usd, public=public, bar_open=bar_open,
                  bar_range=bar_range, spot_range=spot_range, spot_open=spot_open, caller_histories=caller, digests=digests,
                  ignored_columns={"fills": f_ign, "funding": g_ign})


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
        a("    UPPER BOUND: excludes " + "; ".join(e["excludes"]))
        a("    " + e["basis_reading"])
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
    ap.add_argument("--spot-4h")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    try:
        rep = run(a.fills, a.funding, symbol=a.symbol, taker_bps=a.taker_bps, maker_bps=a.maker_bps, capital_usd=a.capital_usd,
                  public_path=a.public_funding, perp_path=a.perp_4h, spot_path=a.spot_4h)
        out = json.dumps(rep, indent=2, sort_keys=True, allow_nan=False) if a.json else render(rep)
    except (RecordError, CT.DataError, OSError, ValueError, OverflowError, ZeroDivisionError) as exc:
        print(f"account_review: {exc}", file=sys.stderr)
        return 2
    print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
