#!/usr/bin/env python3
"""A read-only look at the REAL market: Bybit mainnet public market data.

Testnet basis swings by hundreds of basis points in a day, so everything the
testnet runner measures is testnet microstructure. This module records what the
real market looks like (perp mark, spot, funding, the last funding prints) and
says how often the production entry gate would have been open on it. It is the
only way to learn whether the gate's refusals come from the strategy or from the
testnet.

WHAT IT IS NOT
  * Not trading. Unauthenticated GET requests on three public market-data paths,
    nothing else. No API key, no signature, no order code, no import of any
    order-capable client (tests/test_mainnet_public.py pins all of it statically
    and by loading the module in a clean interpreter).
  * Not profit. It places no order and models no fill, fee paid, slippage or
    liquidation. "The gate was open on 30% of real observations" is a statement
    about a rule and a market view, never about money.
  * Not the testnet evidence. Its own file, its own origin label
    (`mainnet_public`); it refuses to write into a SQLite book or into a file
    whose rows carry any other origin. `carry_shadow.VENUE_BASE` still names no
    mainnet host.
  * Not self-verifying about the venue. What a real venue returned is only as
    good as the operator-pasted output of `report`.

    python3 tools/mainnet_public.py observe --db /data/mainnet/mainnet_public.jsonl
    python3 tools/mainnet_public.py report  --db /data/mainnet/mainnet_public.jsonl

Rows are append-only and hash-chained; a broken chain is reported, not repaired,
and is never extended.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
if os.path.dirname(HERE) not in sys.path:
    sys.path.insert(0, os.path.dirname(HERE))

import carry_costs  # noqa: E402  (pure arithmetic; no client)

ORIGIN = "mainnet_public"
VENUE = "mainnet"
HOST = "api.bybit.com"
BASE = f"https://{HOST}"
SYMBOL = "BTCUSDT"
#: Public market-data paths, and nothing else, that this module may touch.
PUBLIC_PATHS = ("/v5/market/time", "/v5/market/tickers", "/v5/market/funding/history",
                "/v5/market/orderbook")

#: What a market order costs, MEASURED from the public order book instead of
#: assumed. The cost model charges ASSUMED_IMPACT_BPS_PER_LEG per leg ("declared
#: before the run, not measured": carry_backtest.IMPACT_FLOOR_BPS_PER_LEG, which a
#: test pins this to). The sizes are declared here, before any book was read.
BOOK_LEVELS = 50
BOOK_NOTIONALS_USD = (10_000, 100_000)
ASSUMED_IMPACT_BPS_PER_LEG = 1.0

#: What the gate is replayed with: the monitor's own overlay settings.
GATE_ROUND_TRIP_BPS = 11.0
GATE_BORROW_APR = 0.0

EXIT_OK = 0
EXIT_REFUSED = 3
SQLITE_MAGIC = b"SQLite format 3\x00"


class MainnetRefusal(RuntimeError):
    """Nothing was observed or written, and nothing was guessed."""


# -- the network: GET only, one host, no redirects -----------------------------

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):               # noqa: D401
        return None


class _Resp:
    def __init__(self, body: Any, status: int) -> None:
        self._body, self.status_code = body, status

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self) -> Any:
        return self._body


def default_get(url: str, params: Dict[str, Any], timeout: float) -> _Resp:
    """An unauthenticated GET. Any URL that is not https://api.bybit.com/<public
    path> is refused before a socket is opened."""
    parts = urllib.parse.urlsplit(url)
    try:
        port = parts.port
    except ValueError:
        port = -1
    if (parts.scheme != "https" or parts.hostname != HOST or port is not None
            or parts.username or parts.password or parts.path not in PUBLIC_PATHS):
        raise MainnetRefusal("refusing a URL that is not a public mainnet "
                             "market-data path")
    full = url + ("?" + urllib.parse.urlencode(params) if params else "")
    req = urllib.request.Request(full, method="GET",
                                 headers={"Accept": "application/json"})
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(req, timeout=timeout) as resp:         # noqa: S310
            return _Resp(json.loads(resp.read().decode("utf-8")), int(resp.status))
    except urllib.error.HTTPError as exc:
        return _Resp({}, int(exc.code))


def _levels(raw: Any, descending: bool) -> List[Tuple[float, float]]:
    out: List[Tuple[float, float]] = []
    try:
        for price, size in raw:
            p, q = float(price), float(size)
            if not (math.isfinite(p) and math.isfinite(q) and p > 0 and q > 0):
                raise ValueError
            out.append((p, q))
    except (TypeError, ValueError):
        raise MainnetRefusal("an order-book level is not a positive finite "
                             "[price, size] pair")
    if not out:
        raise MainnetRefusal("an order-book side is empty")
    return sorted(out, key=lambda lv: lv[0], reverse=descending)


def walk_impact_bps(levels: Sequence[Tuple[float, float]], mid: float,
                    notional: float, side: str) -> Optional[float]:
    """The cost, in bps of `mid`, of a MARKET order of `notional` USD walking
    `levels` (best first): the volume-weighted fill price against the mid. None
    when the order cannot be priced (no mid, a nonsense size, the book is too thin
    to fill it): never a guess."""
    if side not in ("buy", "sell"):
        raise ValueError(f"side must be buy or sell, not {side!r}")
    if not (isinstance(mid, (int, float)) and math.isfinite(mid) and mid > 0):
        return None
    if not (isinstance(notional, (int, float)) and math.isfinite(notional)
            and notional > 0) or not levels:
        return None
    remaining, spent, qty = float(notional), 0.0, 0.0
    for price, size in levels:
        take = min(remaining, price * size)
        spent += take
        qty += take / price
        remaining -= take
        if remaining <= 1e-9:
            break
    if remaining > 1e-6 or qty <= 0:
        return None
    vwap = spent / qty
    return ((vwap - mid) if side == "buy" else (mid - vwap)) / mid * 1e4


def book_snapshot(raw: Dict[str, Any],
                  notionals: Sequence[float] = BOOK_NOTIONALS_USD) -> Dict[str, Any]:
    """The top of one public order book and what a market order of each declared
    size would cost against it. A broken or crossed book is refused."""
    if not isinstance(raw, dict):
        raise MainnetRefusal("the order book is not an object")
    bids = _levels(raw.get("b"), descending=True)
    asks = _levels(raw.get("a"), descending=False)
    best_bid, best_ask = bids[0][0], asks[0][0]
    if best_bid >= best_ask:
        raise MainnetRefusal("the order book is crossed")
    mid = (best_bid + best_ask) / 2.0
    return {"best_bid": best_bid, "best_ask": best_ask, "mid": mid,
            "spread_bps": (best_ask - best_bid) / mid * 1e4,
            "levels": {"bids": len(bids), "asks": len(asks)},
            "impact_bps": {str(int(n)): {
                "buy": walk_impact_bps(asks, mid, n, "buy"),
                "sell": walk_impact_bps(bids, mid, n, "sell")} for n in notionals}}


class MainnetPublicReader:
    def __init__(self, *, timeout_s: float = 10.0,
                 http_get: Optional[Callable[..., Any]] = None) -> None:
        self.timeout_s, self._get = timeout_s, http_get

    def _fetch(self, path: str, params: Dict[str, Any]) -> Dict[str, Any]:
        if path not in PUBLIC_PATHS:
            raise MainnetRefusal(f"{path!r} is not a public market-data path")
        get = self._get or default_get          # resolved at call time
        resp = get(BASE + path, params, self.timeout_s)
        resp.raise_for_status()
        body = resp.json()
        if int(body.get("retCode", -1)) != 0:
            raise MainnetRefusal(f"{path}: retCode {body.get('retCode')}")
        return body.get("result") or {}

    def observe(self) -> Dict[str, Any]:
        try:
            return self._observe()
        except MainnetRefusal:
            raise
        except Exception as exc:                               # noqa: BLE001
            raise MainnetRefusal(f"the venue could not be read "
                                 f"({type(exc).__name__}); nothing was observed")

    def _observe(self) -> Dict[str, Any]:
        t0 = time.time()
        server = self._fetch("/v5/market/time", {})
        lin = (self._fetch("/v5/market/tickers", {
            "category": "linear", "symbol": SYMBOL}).get("list") or [None])[0]
        spot = (self._fetch("/v5/market/tickers", {
            "category": "spot", "symbol": SYMBOL}).get("list") or [None])[0]
        hist = self._fetch("/v5/market/funding/history", {
            "category": "linear", "symbol": SYMBOL, "limit": 8}).get("list")
        if not lin or not spot or not hist:
            raise MainnetRefusal("venue returned an empty market view")
        rows = sorted(((float(r["fundingRate"]) * 1e4, int(r["fundingRateTimestamp"]))
                       for r in hist), key=lambda p: p[1])
        book: Optional[Dict[str, Any]] = None
        book_error: Optional[str] = None
        try:           # auxiliary: a failed book read must not cost the observation
            book = {cat: book_snapshot(self._fetch("/v5/market/orderbook", {
                "category": cat, "symbol": SYMBOL, "limit": BOOK_LEVELS}))
                    for cat in ("linear", "spot")}
        except Exception as exc:                               # noqa: BLE001
            book, book_error = None, type(exc).__name__
        obs = {
            "origin": ORIGIN, "venue": VENUE,
            "source": f"bybit-public-rest:{BASE}",
            "perp_mark": float(lin["markPrice"]),
            "spot_mark": float(spot["lastPrice"]),
            "funding_bps": float(lin["fundingRate"]) * 1e4,
            "funding_print_bps": rows[-1][0], "funding_print_ms": rows[-1][1],
            "perp_bid": float(lin["bid1Price"]) if lin.get("bid1Price") else None,
            "perp_ask": float(lin["ask1Price"]) if lin.get("ask1Price") else None,
            "venue_time_s": int(server.get("timeSecond", 0)) or None,
            "observed_at_s": time.time(), "fetch_started_s": t0,
            "settled_prints": [[bps, ms] for bps, ms in rows],
            "book": book,
        }
        if book_error:
            obs["book_error"] = book_error
        for k in ("perp_mark", "spot_mark"):
            if not (math.isfinite(obs[k]) and obs[k] > 0):
                raise MainnetRefusal(f"{k} is not a positive finite number")
        if not math.isfinite(obs["funding_bps"]):
            raise MainnetRefusal("funding rate is not finite")
        return obs


# -- the evidence file: append-only, hash-chained, one origin ------------------

def _hash(row: Dict[str, Any]) -> str:
    body = {k: v for k, v in row.items() if k != "hash"}
    return hashlib.sha256(json.dumps(body, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


def read(db: str) -> Tuple[List[Dict[str, Any]], List[str]]:
    """(valid rows, issues). A missing file is empty, not an error."""
    if not os.path.exists(db):
        return [], []
    with open(db, "rb") as fh:
        raw = fh.read()
    if raw.startswith(SQLITE_MAGIC):
        return [], ["this is a SQLite book, not a mainnet_public evidence file"]
    rows: List[Dict[str, Any]] = []
    issues: List[str] = []
    prev = ""
    for n, line in enumerate(raw.decode("utf-8", errors="replace").splitlines()):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
            if not isinstance(row, dict):
                raise ValueError
        except ValueError:
            issues.append(f"line {n + 1}: not a JSON row")
            break
        obs = row.get("obs") if isinstance(row.get("obs"), dict) else {}
        if obs.get("origin") != ORIGIN or obs.get("venue") != VENUE:
            issues.append(f"line {n + 1}: origin/venue is not {ORIGIN}/{VENUE}")
            break
        if row.get("seq") != len(rows) or row.get("prev") != prev \
                or row.get("hash") != _hash(row):
            issues.append(f"line {n + 1}: the hash chain is broken")
            break
        rows.append(row)
        prev = row["hash"]
    return rows, issues


def append(db: str, obs: Dict[str, Any]) -> Dict[str, Any]:
    if obs.get("origin") != ORIGIN or obs.get("venue") != VENUE:
        raise MainnetRefusal("only a mainnet_public observation can be written here")
    rows, issues = read(db)
    if issues:
        raise MainnetRefusal("refusing to extend this file: " + "; ".join(issues))
    row = {"seq": len(rows), "prev": rows[-1]["hash"] if rows else "", "obs": obs}
    row["hash"] = _hash(row)
    os.makedirs(os.path.dirname(os.path.abspath(db)), exist_ok=True)
    with open(db, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n")
        fh.flush()
        os.fsync(fh.fileno())
    return row


# -- what the evidence says, and what it does not -------------------------------

WHAT_IT_IS = (
    "Observations of the REAL Bybit mainnet public market (perp mark, spot, "
    "funding), with the production entry gate replayed on each. It places no "
    "orders and models no fills, fees paid, slippage or liquidation, so it is not "
    "profit and not a P&L. It is never mixed with the testnet evidence; testnet "
    "basis is testnet microstructure and this is the other thing.")


def _stat(values: Sequence[float]) -> Optional[Dict[str, float]]:
    vals = [float(v) for v in values if isinstance(v, (int, float))
            and math.isfinite(v)]
    if not vals:
        return None
    return {"n": len(vals), "min": min(vals), "median": statistics.median(vals),
            "max": max(vals)}


#: The question, the thresholds and the meaning of each answer, written on
#: 2026-10-27 BEFORE any mainnet observation was read, and frozen: the hash is
#: pinned by tests/test_mainnet_protocol.py. Changing a threshold after seeing the
#: data is how a weak result becomes a strong one, and this module will not do it.
MAINNET_PROTOCOL = {
    "schema": "mainnet_public_protocol/1",
    "declared": "2026-10-27, before the first mainnet observation was read",
    "evidence_class": ORIGIN,
    "venue": VENUE,
    "question": ("How often would the production entry gate have been open on "
                 "the REAL market? An opportunity count, not a P&L."),
    "adequacy": {"min_observations": 500, "min_span_days": 30},
    "thresholds": {"rarely_open_below": 0.05},
    "verdicts": ["INSUFFICIENT_REAL_MARKET_SAMPLE",
                 "EVIDENCE_UNREADABLE",
                 "GATE_RARELY_OPEN_ON_REAL_MARKET",
                 "GATE_OPEN_ON_REAL_MARKET_WORTH_A_FORWARD_BOOK"],
    "meaning": {
        "INSUFFICIENT_REAL_MARKET_SAMPLE":
            "Not enough real-market data yet: no conclusion in either direction.",
        "EVIDENCE_UNREADABLE":
            "The evidence file does not verify: no conclusion about the market.",
        "GATE_RARELY_OPEN_ON_REAL_MARKET":
            "At this configuration the gate would almost never trade the real "
            "market: there is nothing to tune toward, and loosening the gate to "
            "make it open more is exactly what this protocol forbids.",
        "GATE_OPEN_ON_REAL_MARKET_WORTH_A_FORWARD_BOOK":
            "The gate does open on the real market. That justifies running a "
            "real-market forward paper book under its own pre-registered "
            "protocol. It is not an edge and not a profit claim: no fills, fees "
            "paid, slippage or liquidation are modelled here.",
    },
    "never": ["a profitability claim from this evidence",
              "a change to the gate, its constants or these thresholds after "
              "looking at the data",
              "selecting a window, symbol or subset after the fact",
              "mixing in testnet observations or any other evidence class"],
    "pnl_claim_allowed": False,
}


def protocol_hash() -> str:
    return hashlib.sha256(json.dumps(MAINNET_PROTOCOL, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


def protocol_verdict(rep: Dict[str, Any]) -> str:
    """The protocol's answer for a report. Reads only the observation count, the
    span, the chain issues and the gate-open fraction; nothing else in the report
    (a `verdict` or `pnl_claim_allowed` field, say) can change it."""
    if not isinstance(rep, dict):
        return "INSUFFICIENT_REAL_MARKET_SAMPLE"
    if rep.get("chain_issues"):
        return "EVIDENCE_UNREADABLE"
    adequacy = MAINNET_PROTOCOL["adequacy"]
    n = rep.get("observations") or 0
    days = (rep.get("span_hours") or 0.0) / 24.0
    gate = rep.get("gate")
    if (n < adequacy["min_observations"] or days < adequacy["min_span_days"]
            or not isinstance(gate, dict) or "open_fraction" not in gate):
        return "INSUFFICIENT_REAL_MARKET_SAMPLE"
    if gate["open_fraction"] < MAINNET_PROTOCOL["thresholds"]["rarely_open_below"]:
        return "GATE_RARELY_OPEN_ON_REAL_MARKET"
    return "GATE_OPEN_ON_REAL_MARKET_WORTH_A_FORWARD_BOOK"


def report(db: str) -> Dict[str, Any]:
    out = _report(db)
    verdict = protocol_verdict(out)
    out["protocol"] = {"id": MAINNET_PROTOCOL["schema"], "hash": protocol_hash(),
                       "verdict": verdict,
                       "meaning": MAINNET_PROTOCOL["meaning"][verdict],
                       "adequacy": MAINNET_PROTOCOL["adequacy"],
                       "thresholds": MAINNET_PROTOCOL["thresholds"]}
    return out


def _pct(values: Sequence[float], q: float) -> float:
    vs = sorted(values)
    return vs[min(len(vs) - 1, max(0, math.ceil(q * len(vs)) - 1))]


def _impact_stats(values: Sequence[float]) -> Optional[Dict[str, float]]:
    vals = [float(v) for v in values if isinstance(v, (int, float)) and math.isfinite(v)]
    if not vals:
        return None
    return {"n": len(vals), "median": statistics.median(vals),
            "p95": _pct(vals, 0.95), "max": max(vals)}


SLIPPAGE_NOTE = (
    "A measurement of one cost assumption (the per-leg impact haircut) against "
    "the real public order book at the declared sizes: not a verdict, not profit. "
    "Each figure is a snapshot's cost of a market order walking the book; no "
    "fills, queue position, partial fills or latency are modelled.")


def _slippage(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    out: Dict[str, Any] = {"assumed_bps_per_leg": ASSUMED_IMPACT_BPS_PER_LEG,
                           "what_it_is": SLIPPAGE_NOTE}
    booked = [r["obs"]["book"] for r in rows
              if isinstance(r["obs"].get("book"), dict)
              and {"linear", "spot"} <= set(r["obs"]["book"])]
    out["observations_with_book"] = len(booked)
    out["observations_without_book"] = len(rows) - len(booked)
    if not booked:
        return out
    legs = {"linear_sell": ("linear", "sell"), "spot_buy": ("spot", "buy"),
            "linear_buy": ("linear", "buy"), "spot_sell": ("spot", "sell")}
    keys = sorted({k for b in booked for k in b["linear"]["impact_bps"]}, key=int)
    per_leg: Dict[str, Any] = {}
    round_trip: Dict[str, Any] = {}
    above: Dict[str, float] = {}
    for k in keys:
        cols = {name: [b[cat]["impact_bps"].get(k, {}).get(side) for b in booked]
                for name, (cat, side) in legs.items()}
        per_leg[k] = {name: _impact_stats(v) for name, v in cols.items()}
        flat = [x for v in cols.values() for x in v
                if isinstance(x, (int, float)) and math.isfinite(x)]
        above[k] = (sum(1 for x in flat if x > ASSUMED_IMPACT_BPS_PER_LEG) / len(flat)
                    if flat else 0.0)
        sums = [sum(cols[n][i] for n in legs) for i in range(len(booked))
                if all(isinstance(cols[n][i], (int, float)) for n in legs)]
        round_trip[k] = _impact_stats(sums)
    out.update({"per_leg": per_leg, "round_trip_bps": round_trip,
                "share_above_assumption_by_notional": above,
                "share_of_legs_above_assumption": above[keys[-1]],
                "largest_declared_notional_usd": int(keys[-1])})
    return out


def _report(db: str) -> Dict[str, Any]:
    rows, issues = read(db)
    out: Dict[str, Any] = {
        "origin": ORIGIN, "venue": VENUE, "observations": len(rows),
        "pnl_claim_allowed": False, "what_it_is": WHAT_IT_IS,
        "chain_issues": issues}
    if issues or not rows:
        return out
    basis, funding, refusals, allowed = [], [], Counter(), 0
    for row in rows:
        o = row["obs"]
        basis.append((o["perp_mark"] - o["spot_mark"]) / o["spot_mark"] * 1e4)
        funding.append(o["funding_bps"])
        prints = [p[0] for p in o.get("settled_prints", [])]
        v = carry_costs.entry_gate(
            funding_prints_bps=prints, perp=o["perp_mark"], spot=o["spot_mark"],
            borrow_apr=GATE_BORROW_APR, round_trip_bps=GATE_ROUND_TRIP_BPS)
        if v.allowed:
            allowed += 1
        else:
            refusals[v.reason] += 1
    first, last = rows[0]["obs"]["observed_at_s"], rows[-1]["obs"]["observed_at_s"]
    out.update({
        "span_hours": round((last - first) / 3600.0, 3),
        "basis_bps": _stat(basis), "funding_bps": _stat(funding),
        "gate": {"allowed": allowed, "refused": len(rows) - allowed,
                 "open_fraction": round(allowed / len(rows), 4),
                 "refusals": dict(refusals),
                 "replayed_with": {"round_trip_bps": GATE_ROUND_TRIP_BPS,
                                   "borrow_apr": GATE_BORROW_APR}}})
    out["slippage"] = _slippage(rows)
    return out


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("observe", "report"):
        p = sub.add_parser(name)
        p.add_argument("--db", required=True)
    args = ap.parse_args(argv)
    try:
        if args.cmd == "observe":
            row = append(args.db, MainnetPublicReader().observe())
            o = row["obs"]
            print(f"observed #{row['seq']}: perp {o['perp_mark']} spot {o['spot_mark']} "
                  f"basis {(o['perp_mark'] - o['spot_mark']) / o['spot_mark'] * 1e4:+.1f} bps "
                  f"funding {o['funding_bps']:+.3f} bps/8h  [{ORIGIN}; not profit]")
            return EXIT_OK
        print(json.dumps(report(args.db), indent=2, sort_keys=True))
        return EXIT_OK
    except MainnetRefusal as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return EXIT_REFUSED


if __name__ == "__main__":
    raise SystemExit(main())
