#!/usr/bin/env python3
"""Local, private importer for the owner's Bybit V5 account exports -> the EXISTING ``owner_ledger`` / ``account_review`` path. Offline, read-only on its
inputs, order-less; the only thing it ever writes is one new report file (exclusive create, 0600).

    python3 tools/owner_import.py --input linear_execution=EXEC.jsonl --input transaction_log=TXLOG.jsonl \\
        --input wallet_opening=W0.json --input wallet_closing=W1.json --marks MARKS.json --out REPORT.json

This module is NOT an accounting authority. Realised perpetual P&L, funding, fees, the cash chain, the equity bridge and the BTC leg all come from
``owner_ledger.build`` (which calls ``account_review.review``) over the canonical records ``bybit_v5_records`` produces. What is new here is the intake
discipline in front of them and the status vocabulary behind them:

  * file type identified from the rows' own shape; a declared role that disagrees is refused; formats nobody documented are refused as UNVERIFIED_FORMAT
  * strict per-row schema, UTC-millisecond timestamps (seconds and ISO strings refused), symbol (BTCUSDT only), fee-rate / funding-rate unit checks
  * sha256 of the original bytes, source lineage, a content-hash import id, identical duplicates dropped and counted, conflicting duplicates => DISPUTED
  * inventory and derivative valuation ONLY from an explicit supplied mark; a missing mark is UNVALUED (null, never zero)
  * status vocabulary: VERIFIED, RECONCILED, ESTIMATED, PROVISIONAL, STALE, DISPUTED, INCOMPLETE, UNAVAILABLE.

Status meanings here. VERIFIED applies ONLY to a file (bytes hashed, schema strict): nothing an owner supplies offline can authenticate a P&L, so no P&L
figure is ever VERIFIED. RECONCILED = every record family (executions, transaction log, funding settlements, opening and closing balances) agrees
within tolerance, no gap is open, and the figure does not depend on a mark; it is still the owner's files, NOT exchange-authenticated, and the report
lists the market checks that could not run. PROVISIONAL = consistent but not anchored to balances. ESTIMATED = depends on a supplied mark. STALE = the
mark is older than the allowed age. INCOMPLETE = a record is missing or a gap is open. DISPUTED = records contradict each other. UNAVAILABLE = not computable.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
BOT = os.path.dirname(HERE)
for _p in (BOT, HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import account_review as AR     # noqa: E402
import bybit_v5_records as BV5  # noqa: E402
import owner_ledger as OL       # noqa: E402

IMPORTER_VERSION = "owner_import/1"
PROVENANCE = "OWNER_SUPPLIED_UNVERIFIED"
STATUSES = ("VERIFIED", "RECONCILED", "ESTIMATED", "PROVISIONAL", "STALE", "DISPUTED", "INCOMPLETE", "UNAVAILABLE")
#: worst -> best; a combined figure takes the weakest of its parts
_RANK = ("DISPUTED", "UNAVAILABLE", "INCOMPLETE", "STALE", "PROVISIONAL", "ESTIMATED", "RECONCILED", "VERIFIED")
#: statuses that may carry a headline profit number
_PROFIT_OK = ("RECONCILED", "PROVISIONAL", "ESTIMATED")
#: What the headline number IS. It is the perp-side realised cash only (fills, fees, funding, interest, statement lines). It EXCLUDES the value change of the owned BTC and the
#: perp's unrealised P&L, so it can be positive while the economic total is negative. It is not "profit"; the economic total is layers.economic_total.net_economic_pnl_usd.
HEADLINE_BASIS = "realised_cash_only"
HEADLINE_CAVEAT = ("realised perp cash only: excludes the value change of the owned BTC and perp unrealised P&L. It is NOT total profit and can be positive while the economic total "
                   "(layers.economic_total.net_economic_pnl_usd) is negative.")

ROLES = ("linear_execution", "spot_execution", "transaction_log", "wallet_opening", "wallet_closing")
ROLE_TYPE = {"linear_execution": "execution", "spot_execution": "execution", "transaction_log": "transaction_log",
             "wallet_opening": "wallet_balance", "wallet_closing": "wallet_balance"}
SYMBOL = "BTCUSDT"

# ---- the formats accepted, each column mapping with its source (API names verbatim; see bybit_v5_records.DOC_BASIS)
FORMATS: Dict[str, Dict[str, Any]] = {
    "execution": {
        "source": "https://bybit-exchange.github.io/docs/v5/order/execution  GET /v5/execution/list (one item per .jsonl line)",
        "required_linear": ("execId", "execTime", "symbol", "side", "execQty", "execPrice", "execFee", "isMaker", "execType"),
        "required_spot": ("execId", "execTime", "symbol", "side", "execQty", "execPrice", "execFee", "feeCurrency", "execType"),
        "optional": ("execValue", "feeCurrency", "feeRate", "orderId"),
        "maps_to": "perp_executions (linear) / spot_trades (spot) via bybit_v5_records.execution_to_fills / spot_execution_to_trades",
    },
    "transaction_log": {
        "source": "https://bybit-exchange.github.io/docs/v5/account/transaction-log  GET /v5/account/transaction-log, UTA (one item per .jsonl line)",
        "required": ("id", "transactionTime", "type", "currency", "change", "cashBalance", "cashFlow", "funding", "fee"),
        "optional": ("tradeId", "category", "symbol", "side", "size", "feeRate", "qty", "tradePrice"),
        "maps_to": "funding / cash_lines / external_flows via bybit_v5_records.transaction_log_to_records",
    },
    "wallet_balance": {
        "source": "https://bybit-exchange.github.io/docs/v5/account/wallet-balance  GET /v5/account/wallet-balance, accountType=UNIFIED, whole response envelope "
                  "in one .json file (field names recalled from the documentation, not re-fetched in this session)",
        "required": ("retCode", "result.list[].accountType", "result.list[].coin[].coin", "result.list[].coin[].walletBalance", "time"),
        "optional": ("result.list[].coin[].borrowAmount",),
        "maps_to": "snapshots (usdt, btc) via an explicit mark; walletBalance excludes unrealised P&L",
    },
}
#: formats this importer refuses by name: nobody has documented them (or their sign/unit convention differs), so reading them would be guessing
UNVERIFIED_FORMAT = (
    "web-UI CSV exports (Transaction Log, Order History, Trade History, Asset): Bybit publishes no column list; read them only by renaming to the documented API names in .jsonl yourself",
    "classic (non-UTA) contract transaction log (exec_type / tx_time / wallet_balance fields): funding has the OPPOSITE sign",
    "execType=Funding / settlement rows inside /v5/execution/list (sign undocumented on a UTA)",
    "USDC / inverse settlement rows (session settlement is mixed with funding)",
    "position-list closedPnl / cumRealisedPnl (inclusion of funding is not documented)",
)
CLASSIC_KEYS = ("exec_type", "tx_time", "wallet_balance", "cash_flow", "tx_id")

MAX_FEE_RATE = 0.005                 # 50 bps as a FRACTION: above it a percent/bps value was written where a fraction belongs
FEE_RATE_REL_TOL = 0.02              # execFee vs notional x feeRate (venue prints 8 decimals; discounts shift the effective rate by a few %)
FEE_RATE_ABS_TOL = 1e-4
BTC_FEE_MAX_FRACTION = 0.005         # a spot fee in BTC above 0.5 % of the quantity is a unit slip (USD or percent written as BTC)
FUND_MARK_TOL = 0.25                 # payment-implied mark must be within 25 % of the reference price band (catches x10 and percent/fraction slips, not rounding)
MIN_FUND_RATE = 1e-6
MARK_BAND = (0.5, 2.0)               # a supplied mark must sit within this multiple of the fills' price range
DEFAULT_MAX_MARK_AGE_MS = 6 * 3_600_000
MS13_MIN, MS13_MAX = 10 ** 12, 10 ** 13


class ImportRefused(AR.RecordError):
    """The file cannot be read as evidence at all. ``code`` is machine-readable."""

    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code


# ---------------------------------------------------------------- reading and identification

def _canon(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), allow_nan=False, default=str)


def _sha_text(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _guard_path(path: str) -> str:
    try:
        return AR._refuse_protected(path)
    except AR.RecordError as exc:
        raise ImportRefused("PROTECTED_PATH", str(exc)) from exc


def _ms(v: Any, where: str) -> int:
    """UTC epoch milliseconds, 13 digits, integer. Seconds (10 digits), floats and ISO strings are refused, never converted by guess."""
    if isinstance(v, bool) or v is None:
        raise ImportRefused("TIMESTAMP", f"{where}: timestamp {v!r} is not epoch milliseconds")
    s = str(v).strip()
    if not s.isascii() or not s.isdigit():
        raise ImportRefused("TIMESTAMP", f"{where}: timestamp {v!r} is not an integer count of UTC milliseconds (floats and ISO strings are not guessed at)")
    x = int(s)
    if not (MS13_MIN <= x < MS13_MAX):
        raise ImportRefused("TIMESTAMP", f"{where}: timestamp {v!r} is not 13-digit epoch milliseconds (10 digits means seconds: not converted by guess)")
    return x


def _read_one(path: str, role: str) -> Tuple[str, Any, str, bytes]:
    """-> (detected type, parsed content, sha256, bytes). Refusals are ImportRefused."""
    real = _guard_path(path)
    label = os.path.basename(real)
    low = real.lower()
    if low.endswith(".csv"):
        raise ImportRefused("UNVERIFIED_FORMAT", f"{label!r}: a CSV export has no documented column list. Convert it to documented API field names (.jsonl) yourself")
    if not low.endswith((".jsonl", ".ndjson", ".json")):
        raise ImportRefused("UNKNOWN_FILE_TYPE", f"{label!r}: only .jsonl / .ndjson (API items) and .json (wallet-balance envelope) are read")
    try:
        data = AR._read_bytes(real, label)
    except AR.RecordError as exc:
        raise ImportRefused("UNREADABLE", str(exc)) from exc
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ImportRefused("MALFORMED", f"{label!r} is not UTF-8 text") from exc
    sha = AR._sha(data)
    if low.endswith(".json"):
        try:
            obj = json.loads(text, object_pairs_hook=AR._no_duplicate_keys, parse_constant=AR._bad_constant)
        except (ValueError, RecursionError) as exc:
            raise ImportRefused("TRUNCATED_OR_MALFORMED", f"{label!r}: not valid JSON ({type(exc).__name__}): a truncated file is refused") from exc
        if not (isinstance(obj, dict) and isinstance(obj.get("result"), dict) and "list" in obj["result"]):
            raise ImportRefused("UNKNOWN_FILE_TYPE", f"{label!r}: a .json file must be a wallet-balance response (result.list)")
        return "wallet_balance", obj, sha, data
    rows: List[Dict[str, Any]] = []
    for n, line in enumerate(text.split("\n"), 1):
        if not line.strip():
            continue
        try:
            o = json.loads(line, object_pairs_hook=AR._no_duplicate_keys, parse_constant=AR._bad_constant)
        except (ValueError, RecursionError) as exc:
            raise ImportRefused("TRUNCATED_OR_MALFORMED", f"{label!r} line {n}: not JSON ({type(exc).__name__}): a truncated or corrupt file is refused whole") from exc
        if not isinstance(o, dict):
            raise ImportRefused("MALFORMED", f"{label!r} line {n}: not an object")
        rows.append(o)
    if not rows:
        raise ImportRefused("MALFORMED", f"{label!r} holds no records")
    keys = set().union(*[set(r) for r in rows])
    if keys & set(CLASSIC_KEYS):
        raise ImportRefused("UNVERIFIED_FORMAT", f"{label!r}: classic contract-log field names ({', '.join(sorted(keys & set(CLASSIC_KEYS)))}); opposite funding sign, not accepted")
    if {"transactionTime", "cashBalance", "change"} <= keys:
        kind = "transaction_log"
    elif {"execId", "execTime", "execQty", "execPrice"} <= keys:
        kind = "execution"
    else:
        raise ImportRefused("UNKNOWN_FILE_TYPE", f"{label!r}: the rows are neither Bybit V5 executions nor transaction-log items (columns: {', '.join(sorted(keys)[:8])}...)")
    return kind, rows, sha, data


def _need_all(rows: Sequence[Mapping[str, Any]], required: Sequence[str], label: str) -> None:
    for i, r in enumerate(rows):
        gone = [c for c in required if c not in r or r[c] is None or (isinstance(r[c], str) and r[c].strip() == "" and c not in ("tradeId", "symbol", "side", "feeRate", "category"))]
        if gone:
            raise ImportRefused("SCHEMA", f"{label} row {i + 1}: missing or empty documented field(s) {', '.join(gone)}")


# ---------------------------------------------------------------- row checks (units, symbol, time)

def _num(v: Any, name: str, where: str) -> float:
    try:
        return AR._num(v, name, where)
    except AR.RecordError as exc:
        raise ImportRefused("SCHEMA", str(exc)) from exc


def _check_execution_rows(rows: List[Dict[str, Any]], role: str, label: str, flags: List[Dict[str, Any]]) -> None:
    spot = role == "spot_execution"
    _need_all(rows, FORMATS["execution"]["required_spot" if spot else "required_linear"], label)
    for i, r in enumerate(rows):
        w = f"{label} row {i + 1}"
        _ms(r["execTime"], w)
        if str(r["symbol"]).strip() != SYMBOL:
            raise ImportRefused("SYMBOL_MISMATCH", f"{w}: symbol {r['symbol']!r} is not {SYMBOL!r}; a row for another instrument is never accepted as BTC (filter the export)")
        if r["side"] not in ("Buy", "Sell"):
            raise ImportRefused("SCHEMA", f"{w}: side {r['side']!r} is not Buy or Sell")
        qty, price, fee = _num(r["execQty"], "execQty", w), _num(r["execPrice"], "execPrice", w), _num(r["execFee"], "execFee", w)
        if qty <= 0 or price <= 0:
            raise ImportRefused("SCHEMA", f"{w}: execQty and execPrice must be positive")
        notional = qty * price
        fr = r.get("feeRate")
        cur = str(r.get("feeCurrency") or "").strip().upper()
        if fr not in (None, ""):
            rate = _num(fr, "feeRate", w)
            if abs(rate) > MAX_FEE_RATE:
                raise ImportRefused("UNIT", f"{w}: feeRate {rate} exceeds {MAX_FEE_RATE} as a fraction ({MAX_FEE_RATE * 1e4:.0f} bps): a percent or bps value written as a fraction")
            if not spot or cur in ("USDT", "USD"):
                expected = notional * rate
                if abs(fee - expected) > max(FEE_RATE_ABS_TOL, FEE_RATE_REL_TOL * abs(expected)):
                    flags.append({"code": "FEE_RATE_INCONSISTENT", "severity": "HARD", "detail": f"{w}: execFee {fee} differs from execValue x feeRate = {expected:.8f}: a unit slip or a mis-keyed fee", "examples": [r["execId"]]})
            if fee == 0 and rate != 0:
                flags.append({"code": "MISSING_FEE", "severity": "GAP", "detail": f"{w}: execFee is 0 although feeRate is {rate}: the fee is missing, not free", "examples": [r["execId"]]})
        if spot:
            if cur == "BTC":
                if abs(fee) > BTC_FEE_MAX_FRACTION * qty:
                    raise ImportRefused("UNIT", f"{w}: a spot fee of {fee} BTC on {qty} BTC exceeds {BTC_FEE_MAX_FRACTION:.1%} of the quantity: USD or percent written as base coin")
            elif cur in ("USDT", "USD"):
                if abs(fee) > MAX_FEE_RATE * notional:
                    raise ImportRefused("UNIT", f"{w}: a spot fee of {fee} {cur} exceeds {MAX_FEE_RATE:.1%} of the notional: a unit slip")
            else:
                raise ImportRefused("SCHEMA", f"{w}: spot feeCurrency {cur!r} is not USDT or BTC")
        else:
            if cur not in ("", "USDT", "USDC"):
                raise ImportRefused("UNIT", f"{w}: a linear-perpetual execFee in feeCurrency {cur!r}: only a USDT/USDC fee is a USD amount (a fee in another coin is not summed as dollars)")
            if "isMaker" in r and not isinstance(r["isMaker"], bool) and str(r["isMaker"]).lower() not in ("true", "false"):
                raise ImportRefused("SCHEMA", f"{w}: isMaker must be a boolean")
            if fee == 0 and fr in (None, ""):
                flags.append({"code": "MISSING_FEE", "severity": "GAP", "detail": f"{w}: execFee is 0 and no feeRate is given: a zero fee on a taker/maker fill is unverified, not free", "examples": [r["execId"]]})


def _check_txlog_rows(rows: List[Dict[str, Any]], label: str, flags: List[Dict[str, Any]]) -> None:
    _need_all(rows, FORMATS["transaction_log"]["required"], label)
    for i, r in enumerate(rows):
        w = f"{label} row {i + 1}"
        _ms(r["transactionTime"], w)
        sym = str(r.get("symbol") or "").strip()
        if sym and sym != SYMBOL:
            raise ImportRefused("SYMBOL_MISMATCH", f"{w}: symbol {sym!r} is not {SYMBOL!r}; a row for another instrument is never accepted as BTC (filter the export)")
        typ = str(r["type"]).upper()
        fr = r.get("feeRate")
        if typ == "TRADE" and fr not in (None, ""):
            rate = _num(fr, "feeRate", w)
            if abs(rate) > MAX_FEE_RATE:
                raise ImportRefused("UNIT", f"{w}: feeRate {rate} exceeds {MAX_FEE_RATE} as a fraction: a percent or bps value written as a fraction")
            fee = _num(r["fee"], "fee", w)
            if fee == 0 and rate != 0 and str(r.get("category") or "linear").lower() == "linear":
                flags.append({"code": "MISSING_FEE", "severity": "GAP", "detail": f"{w}: fee is 0 although feeRate is {rate}", "examples": [str(r["id"])]})
        if typ == "SETTLEMENT":
            if fr in (None, "") or not r.get("symbol") or r.get("side") not in ("Buy", "Sell") or r.get("size") in (None, ""):
                raise ImportRefused("SCHEMA", f"{w}: a SETTLEMENT row needs symbol, side, size and feeRate")
            rate, pay, size = abs(_num(fr, "feeRate", w)), _num(r["funding"], "funding", w), _num(r["size"], "size", w)
            sgn_rate = _num(fr, "feeRate", w) * (1 if r["side"] == "Buy" else -1)
            if rate >= MIN_FUND_RATE and size != 0:
                if pay * (-size * sgn_rate) < 0:
                    flags.append({"code": "FUNDING_SIGN_CONTRADICTION", "severity": "HARD", "detail": f"{w}: a position of {size} at rate {sgn_rate} pays {'-' if size * sgn_rate > 0 else '+'}, the row says funding {pay}", "examples": [str(r["id"])]})


# ---------------------------------------------------------------- duplicates

def _row_key(kind: str, r: Mapping[str, Any]) -> str:
    if kind == "execution":
        return str(r["execId"]).strip()
    return "|".join(str(r.get(k) or "").strip() for k in ("id", "tradeId", "transactionTime", "currency", "type"))


def _dedupe_rows(kind: str, parts: Sequence[Tuple[str, List[Dict[str, Any]]]]) -> Tuple[List[Dict[str, Any]], int, List[Dict[str, Any]]]:
    """Merge the rows of every file of one role. Identical repeat -> dropped and counted; same id with different content -> a dispute."""
    seen: Dict[str, Tuple[str, str]] = {}
    out: List[Dict[str, Any]] = []
    dropped = 0
    disputes: List[Dict[str, Any]] = []
    for label, rows in parts:
        for i, r in enumerate(rows):
            k = _row_key(kind, r)
            c = _canon(r)
            if k in seen:
                if seen[k][0] == c:
                    dropped += 1
                else:
                    disputes.append({"id": k, "first": seen[k][1], "second": f"{label} row {i + 1}"})
                continue
            seen[k] = (c, f"{label} row {i + 1}")
            out.append(r)
    return out, dropped, disputes


# ---------------------------------------------------------------- marks

def _parse_marks(marks: Optional[Sequence[Mapping[str, Any]]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    seen: Dict[Tuple[str, int], float] = {}
    for i, m in enumerate(marks or ()):
        w = f"mark {i + 1}"
        if not isinstance(m, Mapping) or set(m) - {"instrument", "ts_ms", "price", "source"}:
            raise ImportRefused("MARK", f"{w}: a mark is exactly {{instrument, ts_ms, price, source}}")
        if m.get("instrument") not in ("BTC_SPOT", "BTCUSDT_PERP"):
            raise ImportRefused("MARK", f"{w}: instrument must be BTC_SPOT or BTCUSDT_PERP")
        if not isinstance(m.get("source"), str) or not m["source"].strip():
            raise ImportRefused("MARK", f"{w}: a mark must name its source")
        ts = _ms(m.get("ts_ms"), w)
        price = _num(m.get("price"), "price", w)
        if not (0 < price <= AR.MAX_PRICE):
            raise ImportRefused("MARK", f"{w}: price {price} is not a positive plausible price")
        k = (m["instrument"], ts)
        if k in seen and seen[k] != price:
            raise ImportRefused("MARK", f"{w}: two different prices for {k[0]} at {ts}")
        seen[k] = price
        out.append({"instrument": m["instrument"], "ts_ms": ts, "price": price, "source": m["source"].strip()})
    return sorted(out, key=lambda x: (x["instrument"], x["ts_ms"], x["price"]))


def _mark_at(marks: Sequence[Mapping[str, Any]], instrument: str, t: int, max_age: int) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """The latest mark of the instrument at or before ``t`` (a later mark is hindsight). -> (mark, None) / (mark, 'STALE') / (None, 'MISSING')."""
    c = [m for m in marks if m["instrument"] == instrument and m["ts_ms"] <= t]
    if not c:
        return None, "MISSING"
    m = max(c, key=lambda x: x["ts_ms"])
    return m, ("STALE" if t - m["ts_ms"] > max_age else None)


# ---------------------------------------------------------------- wallet balance

def _wallet(obj: Mapping[str, Any], label: str) -> Dict[str, Any]:
    if obj.get("retCode") != 0:
        raise ImportRefused("SCHEMA", f"{label}: retCode is {obj.get('retCode')!r}, not 0: an error response is not a balance")
    ts = _ms(obj.get("time"), f"{label} time")
    lst = obj["result"].get("list")
    if not isinstance(lst, list) or len(lst) != 1 or not isinstance(lst[0], dict):
        raise ImportRefused("SCHEMA", f"{label}: result.list must hold exactly one account")
    acct = lst[0]
    if acct.get("accountType") != "UNIFIED":
        raise ImportRefused("UNVERIFIED_FORMAT", f"{label}: accountType {acct.get('accountType')!r}: only UNIFIED is supported")
    coins = acct.get("coin")
    if not isinstance(coins, list):
        raise ImportRefused("SCHEMA", f"{label}: result.list[0].coin is missing")
    bal: Dict[str, float] = {}
    borrow: Dict[str, float] = {}
    deficit_unevidenced: Dict[str, float] = {}            # a NEGATIVE balance with no stated non-zero borrowAmount: on a unified account that is a loan whose principal the file does not state
    for j, c in enumerate(coins):
        if not isinstance(c, dict) or "coin" not in c or "walletBalance" not in c:
            raise ImportRefused("SCHEMA", f"{label}: coin entry {j + 1} lacks coin / walletBalance")
        name = str(c["coin"]).upper()
        if name in bal:
            raise ImportRefused("SCHEMA", f"{label}: coin {name} listed twice")
        bal[name] = _num(c["walletBalance"], "walletBalance", f"{label} coin {name}")
        b = c.get("borrowAmount")
        if b not in (None, "") and _num(b, "borrowAmount", f"{label} coin {name}") != 0:
            borrow[name] = _num(b, "borrowAmount", f"{label} coin {name}")
        elif bal[name] < 0:
            deficit_unevidenced[name] = bal[name]
    return {"ts_ms": ts, "balances": bal, "borrow": borrow, "deficit_unevidenced": deficit_unevidenced}


# ---------------------------------------------------------------- the import

def _flag(flags: List[Dict[str, Any]], code: str, severity: str, detail: str, examples: Sequence[Any] = ()) -> None:
    flags.append({"code": code, "severity": severity, "detail": detail, "examples": list(examples)})


def _weakest(*statuses: str) -> str:
    return min(statuses, key=_RANK.index)


def run_import(inputs: Sequence[Tuple[str, str]], *, marks: Optional[Sequence[Mapping[str, Any]]] = None, opening_perp_qty: float = 0.0,
               opening_perp_avg: Optional[float] = None, max_mark_age_ms: int = DEFAULT_MAX_MARK_AGE_MS, taker_bps: Optional[float] = None,
               maker_bps: Optional[float] = None) -> Dict[str, Any]:
    """Import ``[(role, path), ...]`` and return the report. Raises ``ImportRefused`` for an unreadable, mistyped or unverified file; contradictions
    between readable records are a REPORT with status DISPUTED, not an exception."""
    if not inputs:
        raise ImportRefused("SCHEMA", "no input files")
    flags: List[Dict[str, Any]] = []
    by_role: Dict[str, List[Tuple[str, List[Dict[str, Any]]]]] = {}
    wallets: Dict[str, Dict[str, Any]] = {}
    lineage: List[Dict[str, Any]] = []
    ent_of: Dict[int, Dict[str, Any]] = {}
    seen_real = set()
    for role, path in inputs:
        if role not in ROLES:
            raise ImportRefused("SCHEMA", f"unknown role {role!r} (one of {', '.join(ROLES)})")
        real = _guard_path(path)
        if real in seen_real:
            raise ImportRefused("SCHEMA", f"{os.path.basename(real)!r} is named twice")
        seen_real.add(real)
        kind, content, sha, data = _read_one(real, role)
        if kind != ROLE_TYPE[role]:
            raise ImportRefused("WRONG_FILE_TYPE", f"{os.path.basename(real)!r} is declared {role} but its rows are {kind}")
        label = f"{role}:{os.path.basename(real)}"
        ent = {"role": role, "file": os.path.basename(real), "sha256": sha, "bytes": len(data), "detected_type": kind, "format": FORMATS[kind]["source"],
               "file_status": "VERIFIED"}
        if kind == "wallet_balance":
            if role in wallets:
                raise ImportRefused("SCHEMA", f"{role} given twice")
            wallets[role] = _wallet(content, label)
            wallets[role]["label"] = label
            wallets[role]["sha256"], wallets[role]["file"] = sha, os.path.basename(real)
            ent["rows"] = 1
            ent["first_ts_ms"] = ent["last_ts_ms"] = wallets[role]["ts_ms"]
        else:
            (_check_execution_rows(content, role, label, flags) if kind == "execution" else _check_txlog_rows(content, label, flags))
            ts_key = "execTime" if kind == "execution" else "transactionTime"
            tss = [_ms(r[ts_key], label) for r in content]
            ent["rows"] = len(content)
            ent["first_ts_ms"], ent["last_ts_ms"] = min(tss), max(tss)
            by_role.setdefault(role, []).append((label, content))
            ent_of[id(content)] = ent
        ent["first_ts_utc"], ent["last_ts_utc"] = AR._iso(ent["first_ts_ms"]), AR._iso(ent["last_ts_ms"])
        lineage.append(ent)
    lineage.sort(key=lambda e: (e["role"], e["sha256"]))
    mk = _parse_marks(marks)
    for k in ("opening_perp_qty",):
        if not (isinstance(opening_perp_qty, (int, float)) and math.isfinite(opening_perp_qty)):
            raise ImportRefused("SCHEMA", f"{k} must be a finite number")
    if abs(opening_perp_qty) > AR.DUST_QTY and not (opening_perp_avg and opening_perp_avg > 0):
        raise ImportRefused("SCHEMA", "a carried-in perpetual position needs opening_perp_avg")

    params = {"symbol": SYMBOL, "marks": mk, "opening_perp_qty": opening_perp_qty, "opening_perp_avg": opening_perp_avg, "max_mark_age_ms": max_mark_age_ms,
              "taker_bps": taker_bps, "maker_bps": maker_bps}
    import_id = "imp_" + _sha_text(_canon({"v": IMPORTER_VERSION, "files": [[e["role"], e["sha256"]] for e in lineage], "params": params}))[:32]

    # ---- duplicates, per role, across files
    merged: Dict[str, List[Dict[str, Any]]] = {}
    dup_dropped = 0
    disputes: List[Dict[str, Any]] = []
    for role, parts in by_role.items():
        rows, n, d = _dedupe_rows(ROLE_TYPE[role], parts)
        merged[role] = rows
        dup_dropped += n
        disputes += d
    duplicates = {"identical_dropped": dup_dropped, "conflicting": disputes}
    reg, in_file_dups = _registry(by_role, ent_of)
    if in_file_dups:
        _flag(flags, "DUPLICATE_PAGE_IN_FILE", "GAP", f"{len(in_file_dups)} identical record(s) repeated inside ONE file (a page exported twice): dropped and counted, but a file that repeats itself "
              "is not a clean export, so the account is not reconciled until it is re-exported", [d["record_id"] for d in in_file_dups[:5]])
    stages = _stages(lineage, wallets, reg, dup_dropped, in_file_dups, disputes)
    base = {"schema": IMPORTER_VERSION, "import_id": import_id, "provenance": PROVENANCE, "authorizes_trading": False, "changes_any_status": False,
            "lineage": {"files": lineage, "marks": mk, "formats_refused_by_name": list(UNVERIFIED_FORMAT), "chain": list(CHAIN), "stages": stages},
            "duplicates": duplicates, "parameters": {k: v for k, v in params.items() if k != "marks"}}
    if disputes:
        return _seal(dict(base, status="DISPUTED", headline_realised_cash_usd=None, headline_basis=HEADLINE_BASIS, headline_caveat=HEADLINE_CAVEAT, layers={}, flags=[{"code": "CONFLICTING_DUPLICATE", "severity": "HARD", "detail":
                     f"{len(disputes)} record id(s) appear twice with different content: which is the record is not decidable; nothing is computed",
                     "examples": [d["id"] for d in disputes[:5]]}], reading="records contradict each other; no result is stated"))

    # ---- canonical records through the existing adapters
    fills: List[AR.Fill] = []
    spot: List[OL.SpotTrade] = []
    funding: List[AR.FundingRow] = []
    flows: List[OL.ExternalFlow] = []
    lines: List[OL.CashLine] = []
    adapter_flags: List[Dict[str, Any]] = []
    adapters: List[str] = []
    try:
        if "linear_execution" in merged:
            rows, nts = BV5.execution_to_fills(merged["linear_execution"], "linear_execution", SYMBOL)
            adapter_flags += nts
            adapters.append("bybit_v5_execution")
            fills, _ = AR.parse_fills(rows, SYMBOL)
        if "spot_execution" in merged:
            rows, nts = BV5.spot_execution_to_trades(merged["spot_execution"], "spot_execution", SYMBOL)
            adapter_flags += nts
            adapters.append("bybit_v5_spot_execution")
            spot, _ = OL.parse_spot_trades(rows, "spot_execution", SYMBOL)
        if "transaction_log" in merged:
            rec = BV5.transaction_log_to_records(merged["transaction_log"], "transaction_log", SYMBOL)
            adapter_flags += rec["notes"]
            adapters.append("bybit_v5_transaction_log")
            if rec["funding"]:
                funding, _ = AR.parse_funding(rec["funding"], SYMBOL)
            if rec["cash_lines"]:
                lines, _ = OL.parse_cash_lines(rec["cash_lines"], "transaction_log")
            if rec["external_flows"]:
                flows, _ = OL.parse_external_flows(rec["external_flows"], "transaction_log")
    except AR.RecordError as exc:
        raise ImportRefused("SCHEMA", f"canonical mapping refused: {exc}") from exc

    # ---- valuation inputs: wallet balances + EXPLICIT marks (a missing mark is UNVALUED, never zero)
    w0, w1 = wallets.get("wallet_opening"), wallets.get("wallet_closing")
    for w in (w0, w1):
        if w and w.get("deficit_unevidenced"):
            _flag(flags, "BORROW_PRINCIPAL_UNSTATED", "GAP", f"{w['label']}: walletBalance {w['deficit_unevidenced']} is negative and borrowAmount is absent, empty or zero: a negative balance on a unified "
                  "account is a loan, so its principal, accrued interest and collateral treatment are UNKNOWN here (export the wallet with borrowAmount, accruedInterest and totalInitialMargin populated); "
                  "no profit is stated until they are", list(w["deficit_unevidenced"]))
        if w and w["borrow"]:
            _flag(flags, "BORROWED_COIN", "GAP", f"{w['label']}: borrowAmount {w['borrow']}: walletBalance is not owned inventory while a coin is borrowed", list(w["borrow"]))
    if w0 and w1 and w1["ts_ms"] <= w0["ts_ms"]:
        raise ImportRefused("SCHEMA", "wallet_closing is not later than wallet_opening")
    ref_prices = [f.price for f in fills] + [s.price for s in spot]
    for m in mk:
        if ref_prices and not (MARK_BAND[0] * min(ref_prices) <= m["price"] <= MARK_BAND[1] * max(ref_prices)):
            raise ImportRefused("MARK", f"mark {m['price']} ({m['instrument']}) is outside {MARK_BAND[0]}x..{MARK_BAND[1]}x of the fills' prices {min(ref_prices)}..{max(ref_prices)}: a unit error")
    # funding magnitude against the fills' price band (independent of any public history): x10 and percent/fraction slips both show here
    if funding and ref_prices:
        lo, hi = min(ref_prices) * (1 - FUND_MARK_TOL), max(ref_prices) * (1 + FUND_MARK_TOL)
        bad = []
        for f in funding:
            if abs(f.rate) >= MIN_FUND_RATE and abs(f.position_qty) > AR.DUST_QTY:
                implied = abs(f.payment_usd) / (abs(f.position_qty) * abs(f.rate))
                if not (lo <= implied <= hi):
                    bad.append(f"{f.exec_id}: implied mark {implied:,.2f}")
        if bad:
            _flag(flags, "FUNDING_MAGNITUDE_IMPLAUSIBLE", "HARD", f"{len(bad)} settlement payment(s) imply a mark outside {lo:,.0f}..{hi:,.0f}: a x10 / unit slip", bad[:5])

    valuation: Dict[str, Any] = {"inventory": None, "perp": None}
    snaps: List[Dict[str, Any]] = []
    unvalued: List[str] = []
    stale: List[str] = []
    if w0 and w1:
        win = [f for f in fills if w0["ts_ms"] < f.ts_ms <= w1["ts_ms"]]
        pp0 = {"qty": float(opening_perp_qty), "avg_entry": float(opening_perp_avg or 0.0)}
        pp1 = OL.perp_average_entry(win, opening_perp_qty, opening_perp_avg) if (win or abs(opening_perp_qty) > AR.DUST_QTY) else pp0
        for tag, w, pp in (("open", w0, pp0), ("close", w1, pp1)):
            sp, why = _mark_at(mk, "BTC_SPOT", w["ts_ms"], max_mark_age_ms)
            if why == "MISSING":
                unvalued.append(f"{tag}: no BTC_SPOT mark at or before {AR._iso(w['ts_ms'])}")
            elif why == "STALE":
                stale.append(f"{tag}: BTC_SPOT mark {AR._iso(sp['ts_ms'])} is older than {max_mark_age_ms // 60000} min at {AR._iso(w['ts_ms'])}")
            pm = None
            upl = 0.0
            if abs(pp["qty"]) > AR.DUST_QTY:
                pm, pwhy = _mark_at(mk, "BTCUSDT_PERP", w["ts_ms"], max_mark_age_ms)
                if pwhy == "MISSING":
                    unvalued.append(f"{tag}: open perpetual position {pp['qty']} with no BTCUSDT_PERP mark")
                elif pwhy == "STALE":
                    stale.append(f"{tag}: BTCUSDT_PERP mark {AR._iso(pm['ts_ms'])} is stale")
                if pm:
                    upl = pp["qty"] * (pm["price"] - pp["avg_entry"])
            snaps.append({"tag": tag, "w": w, "spot_mark": sp, "perp_mark": pm, "upl": upl, "pp": pp})
        if not unvalued:
            for s in snaps:
                w = s["w"]
                snap_row = {"snapshot_id": s["tag"], "ts_ms": w["ts_ms"], "usdt": w["balances"].get("USDT", 0.0), "btc": w["balances"].get("BTC", 0.0),
                            "btc_price": s["spot_mark"]["price"], "perp_upl_usd": s["upl"], "perp_qty": s["pp"]["qty"], "perp_avg_entry": s["pp"]["avg_entry"] or None,
                            "perp_mark": s["perp_mark"]["price"] if s["perp_mark"] else None}
                s["row"] = snap_row
    for u in unvalued:
        _flag(flags, "INVENTORY_UNVALUED", "GAP", u + ": the inventory / position is UNVALUED (null), not zero")
    for u in stale:
        _flag(flags, "MARK_STALE", "GAP", u)
    if not (w0 and w1):
        _flag(flags, "NO_BALANCE_ANCHOR", "GAP", "an opening and a closing wallet-balance file are both needed to anchor the records to the account's balances")

    # ---- the existing ledger
    snap_rows = [s["row"] for s in snaps if "row" in s]
    snapshots: List[OL.Snapshot] = []
    if len(snap_rows) == 2:
        snapshots, _ = OL.parse_snapshots(snap_rows, "wallet_balance")
    kw, hist_digests = AR.load_histories(SYMBOL, None, None, None, True)
    digests = {f"{e['role']}:{e['file']}": e["sha256"] for e in lineage}
    digests.update(hist_digests)
    inp = OL.Inputs(fills, funding, spot, flows, lines, snapshots, digests, {}, kw, taker_bps, maker_bps, tuple(adapter_flags), tuple(sorted(set(adapters))))
    rep = OL.build(inp)
    if not snapshots and fills and abs(opening_perp_qty) <= AR.DUST_QTY:
        # no window to hand the ledger: run the SAME account_review settlement / position checks directly, so an unanchored import still sees a missing settlement
        prep = AR.review(fills, funding, taker_bps=taker_bps, maker_bps=maker_bps, digests=digests, **kw)
        rep = dict(rep, flags=[f for f in rep["flags"] if f["code"] != "PERP_RECORDS_INCOMPLETE"], perp_review={
            "verdict": prep["verdict"], "round_trips": prep["round_trips"], "settlements": prep["settlements"], "sufficiency": prep["sufficiency"]})
        for f in prep["flags"]:
            if f["severity"] == "HARD" or f["code"] in ("MISSING_SETTLEMENT", "AMBIGUOUS_SETTLEMENT", "FUNDING_ROW_IN_AMBIGUOUS_WINDOW"):
                rep["flags"].append(dict(f, code="PERP_" + f["code"]))
            elif f["severity"] == "GAP" and f["code"] in AR.CHECK_GAPS:
                if not any(x["code"] == "PERP_CHECKS_UNRUN" for x in rep["flags"]):
                    rep["flags"].append({"code": "PERP_CHECKS_UNRUN", "severity": "GAP", "count": 1, "detail": "the perpetual records were not compared with the public market", "examples": []})
        if any(f["severity"] == "HARD" for f in rep["flags"]):
            rep["verdict"] = "REJECTED_RECORDS_CONTRADICT"
    # ---- own, independent cross-checks that need no snapshot
    if fills and "transaction_log" in merged:
        txlog_trade_fee = math.fsum(float(r["fee"]) for r in merged["transaction_log"]
                                    if str(r["type"]).upper() == "TRADE" and str(r.get("category") or "linear").lower() == "linear")
        exec_fee = math.fsum(f.fee_usd for f in fills)
        if not math.isclose(txlog_trade_fee, exec_fee, rel_tol=1e-6, abs_tol=0.01):
            _flag(flags, "FEE_TOTALS_DIFFER", "GAP", f"linear execution fees {exec_fee:.8f} != transaction-log linear TRADE fees {txlog_trade_fee:.8f}: a fill or a fee is missing in one file")
        sizes = [(int(r["transactionTime"]), str(r["id"]), float(r["size"])) for r in merged["transaction_log"]
                 if str(r["type"]).upper() == "TRADE" and str(r.get("category") or "linear").lower() == "linear" and r.get("size") not in (None, "")]
        if sizes and (w0 is None or True):
            last_size = sorted(sizes)[-1][2]
            net = float(opening_perp_qty) + math.fsum(f.signed_qty for f in fills)
            if abs(last_size - net) > AR.POSITION_ABS_TOL + 1e-3 * abs(net):
                _flag(flags, "POSITION_MISMATCH", "HARD", f"the executions imply a position of {net} but the transaction log's last TRADE row leaves {last_size}")
    prov = {"reg": reg, "w0": w0, "w1": w1, "opening_perp_qty": float(opening_perp_qty),
            "grid": {f.exec_id: f.grid_ms for f in funding}}
    return _seal(_assemble(base, rep, flags, fills, funding, spot, snaps, unvalued, stale, bool(w0 and w1), mk, lineage, prov=prov))


def _rep_flag_codes(rep: Mapping[str, Any], sev: str) -> List[str]:
    return sorted({f["code"] for f in rep["flags"] if f["severity"] == sev})


def _assemble(base, rep, flags, fills, funding, spot, snaps, unvalued, stale, anchored, mk, lineage, prov=None) -> Dict[str, Any]:
    """Map the ledger's verdict and the importer's own flags to the status vocabulary. The mapping is the whole decision; it adds no arithmetic."""
    b = rep["equity_bridge"]
    if b is not None:
        fig = {"perp_realised_usd": b["perp_realised_usd"], "perp_fees_usd": b["perp_fees_usd"], "funding_net_usd": b["funding_net_usd"], "interest_usd": b["interest_usd"],
               "other_cash_lines_usd": b["other_cash_lines_usd"]}
    else:
        pr = OL.perp_average_entry(fills) if fills else None
        fig = {"perp_realised_usd": pr["realised_usd"] if pr else 0.0, "perp_fees_usd": -math.fsum(f.fee_usd for f in fills),
               "funding_net_usd": math.fsum(f.payment_usd for f in funding), "interest_usd": None, "other_cash_lines_usd": None}
    lin_figs: Dict[str, Any] = {}
    if prov is not None:
        lin_figs, lin_flags = _figure_sources(fig, b, prov, anchored)
        flags.extend(lin_flags)
    my_hard = [f for f in flags if f["severity"] == "HARD"]
    my_gap = [f for f in flags if f["severity"] == "GAP" and f["code"] not in ("INVENTORY_UNVALUED", "MARK_STALE", "NO_BALANCE_ANCHOR")]
    verdict = rep["verdict"]
    ledger_gaps = [c for c in _rep_flag_codes(rep, "GAP") if c not in OL.CHECK_GAPS and c not in ("NO_OPENING_SNAPSHOT", "NO_CLOSING_SNAPSHOT")]
    ledger_unrun = [c for c in _rep_flag_codes(rep, "GAP") if c in OL.CHECK_GAPS]
    if my_hard or verdict == "REJECTED_RECORDS_CONTRADICT":
        realised_status = "DISPUTED"
    elif my_gap or ledger_gaps:
        realised_status = "INCOMPLETE"
    elif verdict == "NO_TRADING_RECORDS":
        realised_status = "UNAVAILABLE"
    elif anchored and rep["equity_bridge"] is not None:
        realised_status = "RECONCILED"
    else:
        realised_status = "PROVISIONAL"        # consistent, but not anchored to two balance snapshots (or the inventory is unvalued)
    perp = rep.get("perp_position")
    settlements = (rep.get("perp_review") or {}).get("settlements") or {}
    missing_settle = settlements.get("missing", 0) or 0
    net = None
    partial_net = None
    if all(v is not None for v in fig.values()):
        net = math.fsum(fig.values())
    elif fig["perp_realised_usd"] is not None:
        # A component that is UNKNOWN (interest, other statement lines: no wallet/transaction anchor) is not zero. The sum of what IS known is reported only as a labelled
        # partial; it is never the net or the headline (found in review: a PROVISIONAL report headlined +476.275 that omitted interest and every other cash line).
        partial_net = math.fsum(v for v in fig.values() if v is not None)
    realised = {"status": realised_status, "figures": fig, "net_realised_cash_usd": net, "partial_net_of_known_components_usd": partial_net,
                "unknown_components": sorted(k for k, v in fig.items() if v is None), "settlements": settlements,
                "components": "perp realised (average entry) + perp fees + funding + interest + other statement lines; deposits and withdrawals are not profit"}
    # inventory / derivative valuation
    if unvalued:
        inv = {"status": "UNAVAILABLE", "reason": "INVENTORY_UNVALUED", "detail": unvalued, "btc_value_usd": None, "perp_unrealised_usd": None}
    elif not anchored:
        inv = {"status": "UNAVAILABLE", "reason": "NO_BALANCE_ANCHOR", "detail": ["both wallet_opening and wallet_closing are needed"], "btc_value_usd": None, "perp_unrealised_usd": None}
    else:
        s1 = snaps[1]
        st = "STALE" if stale else "ESTIMATED"
        inv = {"status": st, "reason": "VALUED_AT_SUPPLIED_MARK" if not stale else "MARK_STALE", "detail": stale,
               "btc_qty": s1["row"]["btc"], "btc_value_usd": s1["row"]["btc"] * s1["row"]["btc_price"], "perp_qty": s1["row"]["perp_qty"],
               "perp_unrealised_usd": s1["upl"], "marks_used": [m for m in (s1["spot_mark"], s1["perp_mark"]) if m],
               "btc_economic_pnl_usd": b["btc_economic_pnl_usd"] if b else None,
               "change_in_perp_unrealised_usd": b["change_in_perp_unrealised_usd"] if b else None}
    econ_net = None
    econ_status = _weakest(realised_status, inv["status"])
    if b is not None and b["btc_economic_pnl_usd"] is not None and inv["status"] == "ESTIMATED" and realised_status in ("RECONCILED", "PROVISIONAL"):
        econ_net = net + b["btc_economic_pnl_usd"] + b["change_in_perp_unrealised_usd"]
    if econ_net is None and econ_status in ("RECONCILED", "ESTIMATED", "PROVISIONAL"):
        econ_status = "UNAVAILABLE"
    economic = {"status": econ_status, "net_economic_pnl_usd": econ_net, "basis": "economic_total: the figure a strategy verdict binds to (not headline_realised_cash_usd)",
                "reading": "withheld (null) while a mark is stale or missing. realised cash P&L + BTC economic P&L at the supplied marks + change in perpetual unrealised P&L; withheld while any part is not computable"}
    status = realised_status
    unchecked = list(ledger_unrun) + ["no exchange authentication: files are the owner's, a digest proves bytes, not origin"]
    if prov is not None:
        base = _complete_lineage(base, lin_figs, prov, realised_status, inv, b, flags, rep, unvalued, stale, unchecked, economic)
    return dict(base, status=status, headline_realised_cash_usd=(net if status in _PROFIT_OK else None), headline_basis=HEADLINE_BASIS, headline_caveat=HEADLINE_CAVEAT,
                partial_figures=(fig if status not in _PROFIT_OK else None), layers={"realised_cash": realised, "inventory_valuation": inv, "economic_total": economic},
                ledger={"verdict": rep["verdict"], "residuals": rep["residuals"], "window": rep["window"], "records": rep["records"]},
                flags=sorted(flags + [dict(f) for f in rep["flags"] if f["severity"] != "INFO"], key=lambda f: (f["severity"], f["code"])),
                unchecked=unchecked, limits=list(OL.LIMITS), undocumented_by_the_venue=list(BV5.UNDOCUMENTED),
                reading="RECONCILED means the owner's files agree with each other and with the two balances; it is not exchange-authenticated.")



# ---------------------------------------------------------------- evidence lineage: raw row -> normalized record -> validation -> dedup -> reconciliation -> uncertainty -> ledger -> owner P&L
CHAIN = ("raw record", "normalized record", "validation", "dedup", "reconciliation", "uncertainty", "ledger", "owner P&L")
BASES = ("realized", "unrealized", "estimated", "provisional", "reconciled", "incomplete")
#: numeric leaves under these roots are FIGURES: each needs a lineage entry (or an explicit null with its reason). Counts / timestamps / the marks themselves are inputs, not figures
_FIGURE_ROOTS = ("headline_realised_cash_usd", "partial_figures", "layers", "ledger.residuals")
_NOT_FIGURES = ("layers.realised_cash.settlements", "layers.inventory_valuation.marks_used")
_LIN_TOL = dict(rel_tol=1e-9, abs_tol=1e-6)


def _rec_id(role: str, r: Mapping[str, Any]) -> str:
    if ROLE_TYPE[role] == "execution":
        return "exec:" + str(r["execId"]).strip()
    return "txlog:" + f"{str(r['id']).strip()}|{str(r.get('tradeId') or '').strip()}|{r.get('transactionTime')}|{str(r['currency']).strip().upper()}"


def _registry(by_role, ent_of) -> Tuple[List[Tuple[Dict[str, Any], Dict[str, Any]]], List[Dict[str, Any]]]:
    """The KEPT normalized records in file order, each with its raw-row source (file sha256 + 1-based row index among the file's records) and a digest of
    the raw row; plus identical repeats found inside ONE file. Replays ``_dedupe_rows`` exactly (first occurrence wins)."""
    reg: List[Tuple[Dict[str, Any], Dict[str, Any]]] = []
    in_file: List[Dict[str, Any]] = []
    for role, parts in by_role.items():
        kind = ROLE_TYPE[role]
        seen: Dict[str, Tuple[Dict[str, Any], int]] = {}
        for _label, rows in parts:
            ent = ent_of[id(rows)]
            for i, r in enumerate(rows):
                rec = {"role": role, "file": ent["file"], "sha256": ent["sha256"], "row": i + 1, "record_id": _rec_id(role, r), "row_sha256": _sha_text(_canon(r))}
                k = _row_key(kind, r)
                if k in seen:
                    if seen[k][0]["row_sha256"] == rec["row_sha256"] and seen[k][1] == id(rows):
                        in_file.append(rec)
                    continue
                seen[k] = (rec, id(rows))
                reg.append((rec, r))
    return reg, in_file


def _stages(files, wallets, reg, dup_dropped, in_file, disputes) -> Dict[str, Any]:
    read: Dict[str, int] = {}
    for e in files:
        read[e["role"]] = read.get(e["role"], 0) + e["rows"]
    norm = dict(read)
    kept: Dict[str, int] = {}
    for rec, _r in reg:
        kept[rec["role"]] = kept.get(rec["role"], 0) + 1
    return {"read": dict(read, total=sum(read.values())), "normalized": dict(norm, total=sum(norm.values())),
            "rejected": {"count": 0, "reasons": [], "note": "a row that fails validation refuses its whole file (ImportRefused with a named code); a refused import produces no report"},
            "duplicate_dropped": {"count": dup_dropped, "identical_within_one_file": len(in_file)},
            "conflicting": {"count": len(disputes), "ids": [d["id"] for d in disputes[:20]]},
            "kept_records": dict(kept, total=sum(kept.values())),
            "records_digest": _sha_text(_canon([[rec["role"], rec["sha256"], rec["row"], rec["record_id"], rec["row_sha256"]] for rec, _ in reg])),
            "reconciled": {"count": 0}, "unreconciled": {"count": sum(kept.values()), "why": "not evaluated"},
            "uncertainty": {}}


def _wallet_src(role: str, w: Mapping[str, Any]) -> Dict[str, Any]:
    return {"role": role, "file": w["file"], "sha256": w["sha256"], "row": None, "record_id": f"wallet:{w['ts_ms']}",
            "row_sha256": _sha_text(_canon({"ts_ms": w["ts_ms"], "balances": w["balances"], "borrow": w["borrow"]}))}


def _mark_src(m: Mapping[str, Any]) -> Dict[str, Any]:
    return {"role": "mark", "file": None, "sha256": None, "row": None, "record_id": f"mark:{m['instrument']}@{m['ts_ms']}", "row_sha256": _sha_text(_canon(m))}


def _figure_sources(fig, b, prov, anchored) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """Lineage entries for the realised-cash components and their derived totals: which kept records each one is the sum of, recomputed from the RAW rows
    (not from the ledger) so the figure can be checked against its own sources."""
    reg, w0, w1, windowed = prov["reg"], prov["w0"], prov["w1"], b is not None

    def inwin(ts: int) -> bool:
        return (w0["ts_ms"] < ts <= w1["ts_ms"]) if (windowed and w0 and w1) else True

    def pick(role, pred, tsf):
        return [(rec, r) for rec, r in reg if rec["role"] == role and pred(r) and inwin(tsf(rec, r))]

    ex_ts = lambda rec, r: int(r["execTime"])          # noqa: E731
    tx_ts = lambda rec, r: int(r["transactionTime"])   # noqa: E731
    typ = lambda r: str(r["type"]).upper()             # noqa: E731
    exec_rows = pick("linear_execution", lambda r: True, ex_ts)
    fund_rows = pick("transaction_log", lambda r: typ(r) == "SETTLEMENT",
                     lambda rec, r: prov["grid"].get(rec["record_id"][len("txlog:"):], int(r["transactionTime"])))
    int_rows = pick("transaction_log", lambda r: typ(r) == "INTEREST" and str(r["currency"]).upper() in ("USDT", "USDC"), tx_ts)
    qty = lambda r: float(r["execQty"]) * (1 if r["side"] == "Buy" else -1)   # noqa: E731
    net_qty = math.fsum(qty(r) for _, r in exec_rows)
    flat = abs(float(prov["opening_perp_qty"])) <= AR.DUST_QTY and abs(net_qty) <= AR.DUST_QTY
    spec = {
        "perp_realised_usd": ("realized", "sum over the listed executions of (sell notional - buy notional); exact for a book that starts and ends flat, else average-entry accounting that is not a plain row sum",
                              exec_rows, (math.fsum(float(r["execQty"]) * float(r["execPrice"]) * (1 if r["side"] == "Sell" else -1) for _, r in exec_rows) if flat else None)),
        "perp_fees_usd": ("realized", "minus the sum of execFee over the listed linear executions", exec_rows, -math.fsum(float(r["execFee"]) for _, r in exec_rows)),
        "funding_net_usd": ("realized", "sum of funding over the listed SETTLEMENT rows of the transaction log", fund_rows, math.fsum(float(r["funding"]) for _, r in fund_rows)),
        "interest_usd": ("realized", "sum of change over the listed INTEREST rows (USD coins) of the transaction log", int_rows, math.fsum(float(r["change"]) for _, r in int_rows)),
        "other_cash_lines_usd": ("realized", "sum of statement lines categorised OTHER (the adapter maps none: unmapped types are flagged, never silently summed)", [], 0.0),
    }
    out: Dict[str, Any] = {}
    flags: List[Dict[str, Any]] = []
    for name, (nature, formula, rows, ssum) in spec.items():
        val = fig[name]
        base = {"nature": nature, "basis": "@conf", "formula": formula, "sources": [rec for rec, _ in rows], "empty_set": not rows}
        if val is None:
            base.update(why="UNKNOWN, not zero: " + ("no statement window / balance anchor was available to total it" if name in ("interest_usd", "other_cash_lines_usd") else "not computable"),
                        source_sum=None, source_check="WITHHELD", sources=[], empty_set=False)
        elif ssum is None:
            base.update(source_sum=None, source_check="NOT_A_ROW_SUM")
        else:
            ok = math.isclose(val, ssum, **_LIN_TOL)
            base.update(source_sum=ssum, source_check="MATCH" if ok else "MISMATCH")
            if not ok:
                flags.append({"code": "LINEAGE_SUM_MISMATCH", "severity": "HARD", "detail": f"{name} is {val} but the raw rows it is attributed to sum to {ssum}: the figure is not explained by its sources",
                              "examples": [name]})
        for root in ("layers.realised_cash.figures.", "partial_figures."):
            out[root + name] = dict(base, sources=list(base["sources"]))
    comps = ["layers.realised_cash.figures." + k for k in fig]
    out["layers.realised_cash.net_realised_cash_usd"] = {"nature": "realized", "basis": "@conf", "formula": "sum of the five realised-cash components", "derived_from": comps}
    out["layers.realised_cash.partial_net_of_known_components_usd"] = {"nature": "realized", "basis": "incomplete", "formula": "sum of the components that ARE known (a labelled partial, never the net)",
                                                                       "derived_from": [c for c, k in zip(comps, fig) if fig[k] is not None]}
    out["headline_realised_cash_usd"] = {"nature": "realized", "basis": "@conf", "formula": "REALISED CASH ONLY (not total profit; see layers.economic_total). Equals net_realised_cash_usd, and only under RECONCILED / PROVISIONAL / ESTIMATED",
                                  "derived_from": ["layers.realised_cash.net_realised_cash_usd"]}
    return out, flags


def _complete_lineage(base, lin_figs, prov, realised_status, inv, b, flags, rep, unvalued, stale, unchecked, economic) -> Dict[str, Any]:
    """Add the valuation / economic / residual entries, resolve the basis labels, and fill in the per-stage counts that depend on the verdict."""
    conf = {"RECONCILED": "reconciled", "PROVISIONAL": "provisional"}.get(realised_status, "incomplete")
    figs = {k: dict(v) for k, v in lin_figs.items()}
    for v in figs.values():
        if v.get("basis") == "@conf":
            v["basis"] = conf
    w0, w1 = prov["w0"], prov["w1"]
    walls = [_wallet_src(r, w) for r, w in (("wallet_opening", w0), ("wallet_closing", w1)) if w]
    marks_used = [_mark_src(m) for m in inv.get("marks_used", [])] if isinstance(inv, dict) else []
    inv_basis = "estimated" if inv.get("status") == "ESTIMATED" else "incomplete"
    inv_pref = "layers.inventory_valuation."
    if inv.get("btc_value_usd") is not None and w1:
        sp = [m for m in inv["marks_used"] if m["instrument"] == "BTC_SPOT"]
        ssum = w1["balances"].get("BTC", 0.0) * sp[0]["price"] if sp else None
        figs[inv_pref + "btc_qty"] = {"nature": "realized", "basis": conf, "formula": "BTC walletBalance of the closing wallet file", "sources": [_wallet_src("wallet_closing", w1)],
                                      "source_sum": w1["balances"].get("BTC", 0.0), "source_check": "MATCH" if math.isclose(inv["btc_qty"], w1["balances"].get("BTC", 0.0), **_LIN_TOL) else "MISMATCH"}
        figs[inv_pref + "btc_value_usd"] = {"nature": "estimated", "basis": inv_basis, "formula": "closing BTC walletBalance x the supplied BTC_SPOT mark", "sources": [_wallet_src("wallet_closing", w1)] + [_mark_src(m) for m in sp],
                                            "source_sum": ssum, "source_check": ("MATCH" if ssum is not None and math.isclose(inv["btc_value_usd"], ssum, **_LIN_TOL) else "MISMATCH")}
    everything = {"nature": "estimated", "basis": inv_basis, "plus_all_records": True, "sources": walls + marks_used, "source_sum": None, "source_check": "NOT_A_ROW_SUM"}
    figs[inv_pref + "perp_qty"] = dict(everything, nature="realized", basis=conf, formula="opening perpetual quantity + signed sum of the listed linear executions", sources=walls)
    figs[inv_pref + "perp_unrealised_usd"] = dict(everything, nature="unrealized", basis=("unrealized" if inv.get("status") == "ESTIMATED" else "incomplete"),
                                                  formula="perp quantity x (supplied BTCUSDT_PERP mark - average entry of the executions)")
    figs[inv_pref + "btc_economic_pnl_usd"] = dict(everything, formula="closing BTC value - opening BTC value - net cost of trading it (marks supplied; spot executions and BTC flows)")
    figs[inv_pref + "change_in_perp_unrealised_usd"] = dict(everything, nature="unrealized", basis=("unrealized" if inv.get("status") == "ESTIMATED" else "incomplete"),
                                                            formula="closing minus opening perpetual unrealised P&L at the supplied marks")
    figs["layers.economic_total.net_economic_pnl_usd"] = {"nature": "estimated", "basis": "estimated" if economic.get("status") == "ESTIMATED" else "incomplete",
                                                         "formula": "net realised cash + BTC economic P&L + change in perpetual unrealised P&L",
                                                         "derived_from": ["layers.realised_cash.net_realised_cash_usd", inv_pref + "btc_economic_pnl_usd", inv_pref + "change_in_perp_unrealised_usd"]}
    for k in (rep.get("residuals") or {}):
        figs["ledger.residuals." + k] = dict(everything, nature="realized", basis=conf, formula=f"ledger residual '{k}': what the closing balance shows minus what the records explain",
                                             sources=walls)
    for v in figs.values():
        if v.get("plus_all_records"):
            v["all_records_digest"] = base["lineage"]["stages"]["records_digest"]
    allf = list(flags) + [f for f in rep["flags"] if f["severity"] != "INFO"]
    stages = json.loads(_canon(base["lineage"]["stages"]))
    total = stages["kept_records"]["total"]
    stages["reconciled"] = {"count": total if realised_status == "RECONCILED" else 0}
    stages["unreconciled"] = {"count": 0 if realised_status == "RECONCILED" else total,
                              "why": ("every record family agrees within tolerance" if realised_status == "RECONCILED" else f"the account is {realised_status}: reconciliation is all-or-nothing per report")}
    stages["uncertainty"] = {"hard_flags": sorted({f["code"] for f in allf if f["severity"] == "HARD"}), "gap_flags": sorted({f["code"] for f in allf if f["severity"] == "GAP"}),
                             "unvalued": list(unvalued), "stale": list(stale), "unchecked": list(unchecked)}
    return dict(base, lineage=dict(base["lineage"], figures=figs, stages=stages))


def _path_get(rep: Mapping[str, Any], path: str) -> Any:
    cur: Any = rep
    for part in path.split("."):
        if not isinstance(cur, Mapping) or part not in cur:
            raise KeyError(path)
        cur = cur[part]
    return cur


def _leaves(node: Any, path: str):
    if isinstance(node, Mapping):
        for k, v in node.items():
            yield from _leaves(v, f"{path}.{k}" if path else str(k))
    elif isinstance(node, (list, tuple)):
        for v in node:
            yield from _leaves(v, path + "[]")
    elif node is None or (isinstance(node, (int, float)) and not isinstance(node, bool)):
        yield path, node


def _finalize_lineage(rep: Dict[str, Any]) -> None:
    """The lineage invariant, run by ``_seal`` on EVERY report: each numeric figure carries provenance (sources, or the figures it is derived from, plus a
    basis label and a recomputed-from-sources check), or is an explicit null with its reason. Raises AssertionError otherwise."""
    lin = rep.setdefault("lineage", {})
    figs = lin.setdefault("figures", {})
    status = rep["status"]
    for path in list(figs):
        try:
            _path_get(rep, path)
        except KeyError:
            del figs[path]
    roots = []
    for r in _FIGURE_ROOTS:
        try:
            roots.append((r, _path_get(rep, r)))
        except KeyError:
            pass
    for root, node in roots:
        for path, val in _leaves(node, root):
            if any(path == ex or path.startswith(ex + ".") or path.startswith(ex + "[") for ex in _NOT_FIGURES):
                continue
            if "[]" in path:
                raise AssertionError(f"figure {path} sits inside a list: it cannot be given a lineage entry")
            if path not in figs:
                if val is not None:
                    raise AssertionError(f"figure without lineage: {path} = {val!r}")
                layer = rep.get("layers", {}).get(path.split(".")[1], {}) if path.startswith("layers.") else {}
                figs[path] = {"nature": "realized", "basis": "incomplete", "formula": None, "sources": [],
                              "why": f"not computable ({layer.get('reason') or 'status ' + status})"}
    for path, e in figs.items():
        v = _path_get(rep, path)
        e["value"] = v
        if v is None:
            if not e.get("why"):
                e["why"] = f"withheld: report status {status}"
            e["source_sum"], e["source_check"] = None, "WITHHELD"
            e["basis"] = "incomplete" if e.get("basis") in ("reconciled", "provisional") else e.get("basis", "incomplete")
        else:
            e.pop("why", None)
        assert e.get("basis") in BASES, (path, e.get("basis"))
    memo: Dict[str, Dict[str, Any]] = {}

    def resolve(path: str) -> Dict[str, Any]:
        if path in memo:
            return memo[path]
        e = figs[path]
        if e.get("derived_from") is not None:
            assert e["value"] is None or all(d in figs for d in e["derived_from"]), (path, "derived from a figure that is not in the report")
            deps = [resolve(d) for d in e["derived_from"] if d in figs]
            srcs: List[Dict[str, Any]] = []
            seen = set()
            for d in deps:
                for s in d["sources"]:
                    key = (s["role"], s["sha256"], s["row"], s["record_id"])
                    if key not in seen:
                        seen.add(key)
                        srcs.append(s)
            e["sources"] = srcs
            e["derived_digests"] = [d["provenance_digest"] for d in deps]
            if e["value"] is None:
                e["source_sum"], e["source_check"] = None, "WITHHELD"
            else:
                vals = [d["value"] for d in deps]
                assert vals and all(x is not None for x in vals), (path, "a non-null total over a null component")
                e["source_sum"] = math.fsum(vals)
                e["source_check"] = "MATCH" if math.isclose(e["value"], e["source_sum"], **_LIN_TOL) else "MISMATCH"
        e["source_ids"] = [s["record_id"] for s in e.get("sources", [])]
        e["provenance_digest"] = _sha_text(_canon({k: e.get(k) for k in ("value", "formula", "sources", "derived_digests", "all_records_digest", "empty_set")}))
        memo[path] = e
        return e

    for path in figs:
        e = resolve(path)
        if e["value"] is not None:
            assert e["sources"] or e.get("empty_set"), f"{path}: a number with no source row, no source figure and no explicit empty set"
            assert e["source_check"] != "MISMATCH" or "LINEAGE_SUM_MISMATCH" in {f["code"] for f in rep.get("flags", [])}, f"{path}: unexplained sum mismatch"
        else:
            assert e.get("why"), f"{path}: null without a reason"
    fhash = {p: [e["value"], e["provenance_digest"]] for p, e in sorted(figs.items())}
    st = lin.get("stages", {})
    st["figures"] = {"total": len(figs), "numeric": sum(1 for e in figs.values() if e["value"] is not None), "null_with_reason": sum(1 for e in figs.values() if e["value"] is None)}
    lin["stages"] = st
    lin["digest"] = _sha_text(_canon({"files": [[e["role"], e["sha256"]] for e in lin.get("files", [])], "marks": lin.get("marks"), "stages": st, "figures": fhash}))


def _seal(rep: Dict[str, Any]) -> Dict[str, Any]:
    """Invariants that hold for every report: status words from the vocabulary; no profit under a non-profit status; every layer is no better than the
    account's own status when the account is incomplete or disputed; a content digest of the body."""
    assert rep["status"] in STATUSES, rep["status"]
    if rep["status"] not in _PROFIT_OK:
        rep["headline_realised_cash_usd"] = None
        for layer in rep.get("layers", {}).values():
            if layer.get("status") in ("VERIFIED", "RECONCILED"):
                layer["status"] = rep["status"]
            for k in ("net_economic_pnl_usd", "net_realised_cash_usd"):
                if k in layer and rep["status"] in ("DISPUTED", "UNAVAILABLE"):
                    layer[k] = None
    for layer in rep.get("layers", {}).values():
        assert layer["status"] in STATUSES
    _finalize_lineage(rep)
    body = _canon({k: v for k, v in rep.items() if k != "report_sha256"})
    rep["report_sha256"] = _sha_text(body)
    return json.loads(_canon(rep))            # normalise: plain JSON types only, sorted keys


def write_report(report: Mapping[str, Any], out_path: str, inputs: Sequence[str] = ()) -> str:
    """Create ``out_path`` exclusively (never overwrite), mode 0600, refusing protected names and any input's own path."""
    if os.path.lexists(out_path):
        raise ImportRefused("OUTPUT_EXISTS", f"{os.path.basename(out_path)!r} already exists: never overwritten")
    real_out = _guard_path(out_path)
    if real_out in {os.path.realpath(p) for p in inputs}:
        raise ImportRefused("OUTPUT_IS_INPUT", "the output path is an input file")
    parent = os.path.dirname(real_out) or "."
    if not os.path.isdir(parent):
        raise ImportRefused("OUTPUT_PATH", f"directory {parent!r} does not exist")
    data = (json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n").encode("utf-8")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(real_out, flags, 0o600)
    except FileExistsError as exc:
        raise ImportRefused("OUTPUT_EXISTS", f"{os.path.basename(out_path)!r} already exists: never overwritten") from exc
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
    except BaseException:
        raise
    return real_out


def _load_marks(path: str) -> Tuple[List[Any], str]:
    real = _guard_path(path)
    data = AR._read_bytes(real, os.path.basename(real))
    try:
        obj = json.loads(data, object_pairs_hook=AR._no_duplicate_keys, parse_constant=AR._bad_constant)
    except (ValueError, RecursionError) as exc:
        raise ImportRefused("MARK", f"marks file is not valid JSON ({type(exc).__name__})") from exc
    if not isinstance(obj, list):
        raise ImportRefused("MARK", "marks file must be a JSON list of {instrument, ts_ms, price, source}")
    return obj, AR._sha(data)


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Local-only Bybit V5 owner-account importer (no network, no orders; writes one new report file).")
    ap.add_argument("--input", action="append", required=True, metavar="ROLE=PATH", help="role is one of: " + ", ".join(ROLES))
    ap.add_argument("--marks", help="JSON list of explicit marks {instrument: BTC_SPOT|BTCUSDT_PERP, ts_ms, price, source}")
    ap.add_argument("--opening-perp-qty", type=float, default=0.0)
    ap.add_argument("--opening-perp-avg", type=float, default=None)
    ap.add_argument("--max-mark-age-min", type=int, default=DEFAULT_MAX_MARK_AGE_MS // 60000)
    ap.add_argument("--taker-bps", type=float, default=None)
    ap.add_argument("--maker-bps", type=float, default=None)
    ap.add_argument("--out", required=True, help="new report path (must not exist)")
    a = ap.parse_args(argv)
    try:
        pairs = []
        for s in a.input:
            if "=" not in s:
                raise ImportRefused("SCHEMA", f"--input {s!r}: expected ROLE=PATH")
            r, p = s.split("=", 1)
            pairs.append((r, p))
        marks = None
        if a.marks:
            marks, _ = _load_marks(a.marks)
        rep = run_import(pairs, marks=marks, opening_perp_qty=a.opening_perp_qty, opening_perp_avg=a.opening_perp_avg,
                         max_mark_age_ms=a.max_mark_age_min * 60000, taker_bps=a.taker_bps, maker_bps=a.maker_bps)
        write_report(rep, a.out, [p for _, p in pairs] + ([a.marks] if a.marks else []))
    except (AR.RecordError, OSError, ValueError) as exc:
        print(f"owner_import: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    print(f"{rep['import_id']} status {rep['status']} -> {a.out}")
    return 0 if rep["status"] not in ("DISPUTED",) else 3


if __name__ == "__main__":
    raise SystemExit(main())
