#!/usr/bin/env python3
"""Run, replay and inspect the public Bybit market-data feed.

PUBLIC and READ-ONLY. Unauthenticated market-data WebSocket and REST; no API
key, no signature, no private channel, no order code. It writes only to the
store directory you name (never into the trading runtime database, never into a
tracked source tree).

    python3 tools/md_run.py collect --dir /data/market --seconds 3600
    python3 tools/md_run.py replay  --dir /data/market
    python3 tools/md_run.py status  --dir /data/market

``collect`` connects the linear and spot public streams, bootstraps instrument
rules / the last settled funding print / a REST cross-check, appends every input
to the hash-chained log, and prints one status line per interval. ``replay``
folds the stored log through a fresh state and checks every logged view is
reproduced exactly (exit 0 only if it is). ``status`` summarises a stored log.

Proxy and CA bundle are passed as flags (this tool reads no environment).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any, Dict, Optional, Sequence

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import md_engine as EN          # noqa: E402
import md_events as E           # noqa: E402
import md_feed as F             # noqa: E402
import md_rest as R             # noqa: E402
import md_state as S            # noqa: E402
import md_store as ST           # noqa: E402
import md_transport as T        # noqa: E402


def summarize(view: S.MarketView) -> Dict[str, Any]:
    return {
        "perp_bid": view.perp_bid, "perp_ask": view.perp_ask, "spot_bid": view.spot_bid,
        "spot_ask": view.spot_ask, "perp_mark": view.perp_mark, "perp_index": view.perp_index,
        "funding_rate_forecast": view.funding_rate, "next_funding_ms": view.next_funding_ms,
        "basis_bps": view.basis_bps, "perp_spread_bps": view.perp_spread_bps,
        "spot_spread_bps": view.spot_spread_bps, "leg_span_s": view.leg_span_s,
        "clock_skew_ms": view.clock_skew_ms,
        "entry_refusals": view.refusals(S.ENTRY), "monitor_refusals": view.refusals(S.MONITOR)}


def cmd_collect(a: argparse.Namespace) -> int:
    store = ST.MarketStore(a.dir, environment=a.environment)
    state = S.MarketState(a.symbol, environment=a.environment)
    eng = EN.Engine(state, store)
    cfg = F.FeedConfig(symbol=a.symbol, environment=a.environment, ws_base=a.ws_base,
                       view_log_interval_s=a.view_interval)

    def factory(url: str):
        return T.PublicWebSocket(url, proxy_url=a.proxy_url, ca_file=a.ca_file)
    rest = None
    if not a.no_rest:
        rest = R.PublicRest(R.urllib_transport(a.proxy_url, a.ca_file),
                            base_url=a.rest_base or cfg.rest_base)
    feed = F.Feed(cfg, eng, factory, rest)
    feed.start()
    t0 = time.monotonic()
    try:
        while time.monotonic() - t0 < a.seconds:
            time.sleep(a.status_every)
            v = feed.view()
            line = summarize(v)
            line["frames"] = eng.state.frames
            line["rejections"] = dict(eng.state.rejections)
            line["rest_error"] = feed.rest.last_error if feed.rest else None
            print(json.dumps(line, sort_keys=True), flush=True)
    except KeyboardInterrupt:
        pass
    finally:
        feed.stop()
        store.flush()
    print(json.dumps({"final": {"records": store.count(), "chain_problems": store.verify(),
                                "health": eng.state.health(), "feed": feed.status()}}, sort_keys=True, default=str))
    return 0


def cmd_replay(a: argparse.Namespace) -> int:
    store = ST.MarketStore(a.dir)
    rep = EN.replay(store, symbol=a.symbol)
    kinds: Dict[str, int] = {}
    for inc in rep.incidents:
        kinds[inc.kind] = kinds.get(inc.kind, 0) + 1
    print(json.dumps({"records": rep.records, "frames": rep.frames, "views_checked": rep.views_checked,
                      "view_mismatches": rep.view_mismatches[:20],
                      "chain_problems": rep.chain_problems[:20], "incidents": kinds,
                      "rejections": rep.rejections, "deterministic": rep.deterministic},
                     sort_keys=True))
    return 0 if rep.deterministic else 2


def cmd_status(a: argparse.Namespace) -> int:
    store = ST.MarketStore(a.dir)
    last: Optional[Dict[str, Any]] = None
    kinds: Dict[str, int] = {}
    first_ms = last_ms = None
    for rec in store.iter_records():
        kinds[rec.kind] = kinds.get(rec.kind, 0) + 1
        first_ms = rec.wall_ms if first_ms is None else first_ms
        last_ms = rec.wall_ms
        if rec.kind == ST.VIEW:
            last = json.loads(rec.payload)["view"]
    print(json.dumps({"segments": store.segments(), "records_by_kind": kinds, "first_wall_ms": first_ms,
                      "last_wall_ms": last_ms, "last_logged_view": last}, sort_keys=True))
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("collect", "replay", "status"):
        p = sub.add_parser(name)
        p.add_argument("--dir", required=True, help="market-data store directory (outside source trees)")
        p.add_argument("--symbol", default="BTCUSDT")
    c = sub.choices["collect"]
    c.add_argument("--seconds", type=float, default=60.0)
    c.add_argument("--status-every", type=float, default=5.0)
    c.add_argument("--view-interval", type=float, default=5.0)
    c.add_argument("--environment", choices=("mainnet", "testnet"), default="mainnet")
    c.add_argument("--ws-base", default="", help="override the environment's public stream")
    c.add_argument("--rest-base", default="", help="override the environment's REST base")
    c.add_argument("--no-rest", action="store_true")
    c.add_argument("--proxy-url", default=None)
    c.add_argument("--ca-file", default=None)
    a = ap.parse_args(argv)
    return {"collect": cmd_collect, "replay": cmd_replay, "status": cmd_status}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
