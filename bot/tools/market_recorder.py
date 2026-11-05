#!/usr/bin/env python3
"""Run the public Bybit market-data feed as a service, and turn it into the numbers a
capital decision needs. PUBLIC and READ-ONLY: no key, no signature, no private channel,
no order code.

    python3 tools/market_recorder.py --dir /data/market --environment mainnet \\
        --min-free-mb 512 --retention-days 2

It does four jobs and does them for ever, until told to stop:

  1. RECORD   every venue input (order books, tickers, trades, REST cross-checks) into the
              hash-chained, replayable log in ``--dir`` (md_store), exactly as the
              trading process would see it;
  2. BOUND    itself: it stops writing at a free-space reserve (``--min-free-mb``) and
              prunes whole closed UTC days past ``--retention-days``, so a recorder that
              shares a volume with anything else can never fill it;
  3. MEASURE  every ``--capacity-every`` seconds, the cost of trading each size on the
              displayed book (md_capacity) into ``capacity-YYYYMMDD.jsonl``: the curve
              that says where depth stops paying and how large the trade can be at all;
  4. REPORT   its own health every ``--status-every`` seconds into ``recorder_status.json``
              (atomic), which the hourly monitor copies into its history and the external
              watcher alerts on.

A recorder that cannot write degrades the way the feed already defines: entries would
stop, nothing else changes; it recovers by itself and marks the gap in the log. It exits
0 on SIGTERM/SIGINT after flushing the log and closing the writer lock.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Sequence

HERE = os.path.dirname(os.path.abspath(__file__))
BOT = os.path.dirname(HERE)
if BOT not in sys.path:
    sys.path.insert(0, BOT)

import evidence_chain as EC  # noqa: E402
import md_capacity as MC   # noqa: E402
import md_engine as EN     # noqa: E402
import md_events as E      # noqa: E402
import md_feed as F        # noqa: E402
import md_rest as R        # noqa: E402
import md_state as S       # noqa: E402
import md_store as ST      # noqa: E402
import md_transport as T   # noqa: E402

STATUS_FILE = "recorder_status.json"
SCHEMA = "recorder/1"
DAY_MS = 86_400_000


def _atomic_write(path: str, obj: Any) -> None:
    tmp = f"{path}.tmp.{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, sort_keys=True, default=str)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


class Recorder:
    """Owns a running feed and the two side outputs (status, capacity)."""

    def __init__(self, engine: EN.Engine, feed: F.Feed, directory: str, *, environment: str,
                 min_free_bytes: int = 0, retention_days: int = 0,
                 capacity_retention_days: int = 365,
                 notionals: Sequence[float] = MC.DEFAULT_NOTIONALS,
                 wall_ms: Callable[[], int] = lambda: int(time.time() * 1000),
                 mono: Callable[[], float] = time.monotonic) -> None:
        self.engine, self.feed, self.dir = engine, feed, os.path.abspath(directory)
        self.environment = environment
        self.min_free_bytes, self.retention_days = int(min_free_bytes), int(retention_days)
        #: The capacity curve is ~0.7 MB a day against ~1.5 GB a day for the depth log, and it
        #: is the multi-week record of whether funding PERSISTS, which the capital decision
        #: rests on. It is therefore kept far longer than the depth log (default a year).
        self.capacity_retention_days = int(capacity_retention_days)
        self.notionals = tuple(notionals)
        self.wall_ms, self.mono = wall_ms, mono
        self.started_ms = self.wall_ms()
        self.capacity_rows = 0
        self.capacity_errors = 0
        self.last_capacity_ms: Optional[int] = None
        self.last_capacity_error = ""
        self.capacity_max_fillable: Optional[float] = None
        self.chain_head: Optional[Dict[str, Any]] = None      # {"file", "n", "head"} of the newest row
        self._resealed = 0
        self._repairs = 0
        os.makedirs(self.dir, exist_ok=True)

    # -- capacity ----------------------------------------------------------------------------------------
    def _capacity_path(self, now_ms: int) -> str:
        return os.path.join(self.dir, "capacity-" + ST._utc_day(now_ms) + ".jsonl")

    def _source(self) -> Dict[str, Any]:
        """The market event this row is computed at: the log's newest record id and the books'
        update ids / sequence numbers (what the venue stamped)."""
        st = self.engine.state
        out: Dict[str, Any] = {"store_record_id": max(0, getattr(self.engine.store, "_next_id", 1) - 1)}
        for key, cat in (("perp", E.LINEAR), ("spot", E.SPOT)):
            bk = st.books.get(cat)
            out[f"{key}_update_id"] = None if bk is None else getattr(bk, "update_id", None)
            out[f"{key}_seq"] = None if bk is None else getattr(bk, "seq", None)
        return out

    def _health(self) -> Dict[str, Any]:
        fs = self.feed.status()
        streams = fs.get("streams") or {}
        try:
            free = self.engine.store.free_bytes()
        except Exception:  # noqa: BLE001
            free = None
        rest = fs.get("rest") or {}
        return {"uptime_s": round((self.wall_ms() - self.started_ms) / 1000.0, 1),
                "linear_reconnects": (streams.get(E.LINEAR) or {}).get("reconnects"),
                "spot_reconnects": (streams.get(E.SPOT) or {}).get("reconnects"),
                "linear_connected": (streams.get(E.LINEAR) or {}).get("connected"),
                "spot_connected": (streams.get(E.SPOT) or {}).get("connected"),
                "rest_errors": rest.get("errors"), "rest_bootstrapped": rest.get("bootstrapped"),
                "book_rejections": sum(int(v) for v in (self.engine.state.rejections or {}).values()),
                "store_fault": self.engine.state.store_fault,
                "free_bytes": free, "min_free_bytes": self.min_free_bytes}

    def capture_capacity(self) -> Optional[Dict[str, Any]]:
        """Append one chained cost curve for the current view. Never raises."""
        now = self.wall_ms()
        try:
            view = self.feed.view()
            row = MC.capacity_row(view, now_ms=now, notionals=self.notionals,
                                  source=self._source(), health=self._health())
            # the view's own verdict is recorded with the curve: a curve taken from a view the
            # entry gate would refuse is still data, but it must say so
            row["entry_refusals"] = list(view.refusals(S.ENTRY))
            path = self._capacity_path(now)
            try:
                n, head = EC.append(path, row)
            except EC.ChainDesync:
                # a crash or a full disk between the row and its chain entry: repair it VISIBLY
                # (a torn row is set aside in <file>.torn, an unchained row is sealed with a
                # `resealed` mark), then try once more. Never append over a disagreement.
                self._repairs += 1
                if not EC.repair(path)["appendable"]:
                    raise
                n, head = EC.append(path, row)
            self.chain_head = {"file": os.path.basename(path), "n": n, "head": head}
            self.capacity_rows += 1
            self.last_capacity_ms = now
            self.capacity_max_fillable = row["max_displayed_fillable_notional_usd"]
            return row
        except Exception as exc:  # noqa: BLE001 - a recorder keeps recording
            self.capacity_errors += 1
            self.last_capacity_error = f"{type(exc).__name__}: {exc}"[:200]
            return None

    def prune_capacity(self) -> List[str]:
        """Delete capacity files older than THEIR retention (0 = keep). Small files, by day;
        independent of the depth log's retention."""
        if self.capacity_retention_days <= 0:
            return []
        cutoff = ST._utc_day(self.wall_ms() - self.capacity_retention_days * DAY_MS)
        removed = []
        for name in sorted(os.listdir(self.dir)):
            if name.startswith("capacity-") and ".jsonl" in name and name[9:17] < cutoff:
                try:
                    os.remove(os.path.join(self.dir, name))     # the file and its .chain / .head siblings
                    if name.endswith(".jsonl"):
                        removed.append(name)
                except OSError:
                    pass
        return removed

    # -- status ------------------------------------------------------------------------------------------
    def status(self) -> Dict[str, Any]:
        now = self.wall_ms()
        store = self.engine.store
        try:
            free = store.free_bytes()
        except Exception:  # noqa: BLE001
            free = None
        view_refusals: Optional[List[str]] = None
        try:
            view_refusals = list(self.feed.view().refusals(S.ENTRY))
        except Exception:  # noqa: BLE001
            pass
        return {
            "schema": SCHEMA, "pid": os.getpid(), "environment": self.environment,
            "symbol": self.engine.state.symbol, "started_ms": self.started_ms, "now_ms": now,
            "uptime_s": round((now - self.started_ms) / 1000.0, 1),
            "feed": self.feed.status(),
            "store": {"records": store.count(), "segments": len(store.segments()),
                      "free_bytes": free, "min_free_bytes": self.min_free_bytes,
                      "retention_days": self.retention_days,
                      "capacity_retention_days": self.capacity_retention_days,
                      "fault": self.engine.state.store_fault},
            "entry_refusals": view_refusals,
            "capacity": {"rows": self.capacity_rows, "errors": self.capacity_errors,
                         "last_ms": self.last_capacity_ms, "last_error": self.last_capacity_error,
                         "max_displayed_fillable_notional_usd": self.capacity_max_fillable,
                         "chain_head": self.chain_head, "resealed_rows_at_start": self._resealed,
                         "repairs_while_running": self._repairs},
        }

    def write_status(self) -> Dict[str, Any]:
        st = self.status()
        _atomic_write(os.path.join(self.dir, STATUS_FILE), st)
        return st

    # -- lifecycle ---------------------------------------------------------------------------------------
    def start(self) -> None:
        # a crash between a row and its chain entry leaves an unchained tail: seal it visibly
        try:
            path = self._capacity_path(self.wall_ms())
            if os.path.exists(path):
                r = EC.repair(path)
                self._resealed = r["rows_resealed"]
        except Exception:  # noqa: BLE001 - evidence repair must not stop collection
            pass
        self.feed.start()
        self.write_status()

    def stop(self) -> None:
        try:
            self.feed.stop()
        finally:
            try:
                self.engine.store.flush()
            finally:
                self.engine.store.close()

    def run(self, stop: threading.Event, *, status_every_s: float = 30.0,
            capacity_every_s: float = 300.0, prune_every_s: float = 3600.0) -> None:
        """Block until `stop` is set."""
        next_status = next_capacity = next_prune = self.mono()
        while not stop.is_set():
            now = self.mono()
            if now >= next_capacity:
                next_capacity = now + capacity_every_s
                self.capture_capacity()
            if now >= next_prune:
                next_prune = now + prune_every_s
                self.prune_capacity()
            if now >= next_status:
                next_status = now + status_every_s
                self.write_status()
            stop.wait(1.0)
        self.write_status()


def build(directory: str, *, environment: str = "mainnet", symbol: str = "BTCUSDT",
          min_free_mb: int = 512, retention_days: int = 2, capacity_retention_days: int = 365,
          transport_factory: Optional[Callable[[str], Any]] = None,
          rest_client: Optional[R.PublicRest] = None, use_rest: bool = True,
          proxy_url: Optional[str] = None, ca_file: Optional[str] = None) -> Recorder:
    min_free = max(0, int(min_free_mb)) * 1024 * 1024
    store = ST.MarketStore(directory, environment=environment, min_free_bytes=min_free)
    engine = EN.Engine(S.MarketState(symbol, environment=environment), store)
    cfg = F.FeedConfig(symbol=symbol, environment=environment, retention_days=int(retention_days))
    factory = transport_factory or (lambda url: T.PublicWebSocket(url, proxy_url=proxy_url, ca_file=ca_file))
    if rest_client is None and use_rest:
        rest_client = R.PublicRest(R.urllib_transport(proxy_url, ca_file), base_url=cfg.rest_base)
    feed = F.Feed(cfg, engine, factory, rest_client)
    return Recorder(engine, feed, directory, environment=environment,
                    min_free_bytes=min_free, retention_days=int(retention_days),
                    capacity_retention_days=int(capacity_retention_days))


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--dir", required=True, help="recorder directory (on the volume, outside source trees)")
    ap.add_argument("--environment", choices=("mainnet", "testnet"), default="mainnet")
    ap.add_argument("--symbol", default="BTCUSDT")
    ap.add_argument("--min-free-mb", type=int, default=512)
    ap.add_argument("--retention-days", type=int, default=2,
                    help="UTC days of the (large) depth log to keep")
    ap.add_argument("--capacity-retention-days", type=int, default=365,
                    help="UTC days of the (small) capacity curve files to keep; 0 = keep all")
    ap.add_argument("--status-every", type=float, default=30.0)
    ap.add_argument("--capacity-every", type=float, default=300.0)
    ap.add_argument("--no-rest", action="store_true")
    ap.add_argument("--proxy-url", default=None)
    ap.add_argument("--ca-file", default=None)
    a = ap.parse_args(argv)

    stop = threading.Event()

    def handler(signum, _frame):
        stop.set()
    signal.signal(signal.SIGTERM, handler)
    signal.signal(signal.SIGINT, handler)

    rec = build(a.dir, environment=a.environment, symbol=a.symbol, min_free_mb=a.min_free_mb,
                retention_days=a.retention_days, capacity_retention_days=a.capacity_retention_days, use_rest=not a.no_rest,
                proxy_url=a.proxy_url, ca_file=a.ca_file)
    rec.start()
    try:
        rec.run(stop, status_every_s=a.status_every, capacity_every_s=a.capacity_every)
    finally:
        rec.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
