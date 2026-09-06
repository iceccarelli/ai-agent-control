#!/usr/bin/env python3
"""Public-endpoint reachability check. No keys. No orders. Persists JSON.

Verdicts
  BYBIT_TESTNET_BLOCKED  api-testnet.bybit.com unreachable or non-200.
                          Track D (testnet orders) must not run from this host.
  CORPUS_PATH_OK          www.binance.com/fapi/v1/{time,fundingRate} answered 200:
                          closed-bar refresh is possible from this host.
  CORPUS_PATH_BLOCKED     it did not. Do NOT substitute another venue.

A 403/451 is a network refuse. It is never a licence to change venue.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from typing import Callable, Dict, List, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

ENDPOINTS: Tuple[Tuple[str, str], ...] = (
    ("bybit_testnet_time", "https://api-testnet.bybit.com/v5/market/time"),
    ("bybit_mainnet_time", "https://api.bybit.com/v5/market/time"),
    ("binance_fapi_time", "https://fapi.binance.com/fapi/v1/time"),
    ("binance_www_time", "https://www.binance.com/fapi/v1/time"),
    ("binance_www_funding",
     "https://www.binance.com/fapi/v1/fundingRate?symbol=BTCUSDT&limit=1"),
)

Fetcher = Callable[[str, float], Tuple[int, str]]


def default_fetch(url: str, timeout: float) -> Tuple[int, str]:
    import urllib.error
    import urllib.request
    req = urllib.request.Request(url, headers={"User-Agent": "connector-check/1"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return int(resp.status), resp.read(200).decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return int(exc.code), str(exc.reason)
    except Exception as exc:  # noqa: BLE001
        return 0, f"{type(exc).__name__}: {exc}"


def check(fetch: Fetcher = default_fetch, timeout: float = 8.0) -> Dict[str, object]:
    results: List[Dict[str, object]] = []
    for name, url in ENDPOINTS:
        status, note = fetch(url, timeout)
        results.append({"name": name, "url": url, "http": status,
                        "ok": status == 200, "note": note[:120]})
    by = {r["name"]: r for r in results}
    bybit_ok = bool(by["bybit_testnet_time"]["ok"])
    corpus_ok = bool(by["binance_www_time"]["ok"] and by["binance_www_funding"]["ok"])
    verdict = "BYBIT_TESTNET_OK" if bybit_ok else "BYBIT_TESTNET_BLOCKED"
    return {
        "tool": "connector_check",
        "checked_at_utc": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "keys_used": False, "orders_placed": False,
        "endpoints": results,
        "bybit_testnet_verdict": verdict,
        "corpus_path_verdict": "CORPUS_PATH_OK" if corpus_ok else "CORPUS_PATH_BLOCKED",
        "track_d_permitted_from_this_host": bybit_ok,
        "venue_changed": False,
        "note": "A 403/451 is a network refuse, not a licence to change venue.",
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default=os.path.join(ROOT, "artifacts",
                                                  "connector_check.json"))
    ap.add_argument("--timeout", type=float, default=8.0)
    args = ap.parse_args(argv)
    report = check(timeout=args.timeout)
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2)
    for r in report["endpoints"]:
        print(f"{r['http']:>4}  {r['url']}")
    print(report["bybit_testnet_verdict"], "|", report["corpus_path_verdict"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
