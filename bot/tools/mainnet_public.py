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
PUBLIC_PATHS = ("/v5/market/time", "/v5/market/tickers", "/v5/market/funding/history")

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
        }
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


def report(db: str) -> Dict[str, Any]:
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
