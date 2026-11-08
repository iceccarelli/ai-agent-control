"""SYNTHETIC universe-capture directories for the point-in-time tests. NOTHING here is an exchange observation.

The files mimic the contract ``universe_capture/v2`` (raw V5 ``instruments-info`` pages written byte-for-byte, a manifest listing one partition per
(category, status) with its pages, cursor chain and sha256) so the harness's own re-verification can be exercised offline. They are built in a tmp dir by the
test that needs them; no committed file claims to be a capture.
"""
from __future__ import annotations

import hashlib
import json
import os
from typing import Any, Dict, List, Mapping, Optional, Sequence

SCHEMA = "universe_capture/v2"
STATUSES = ("Trading", "PendingOpen", "PreLaunch", "Delivering", "Closed")


def row(symbol: str, status: str, launch: int, delivery: int = 0, *, symbol_id: Optional[str] = None, contract: str = "LinearPerpetual", interval: Optional[int] = 480) -> Dict[str, Any]:
    r: Dict[str, Any] = {"symbol": symbol, "status": status, "contractType": contract, "baseCoin": symbol[:-4], "quoteCoin": "USDT", "settleCoin": "USDT",
                         "launchTime": str(launch), "deliveryTime": str(delivery)}
    if symbol_id is not None:
        r["symbolId"] = symbol_id
    if interval is not None:
        r["fundingInterval"] = interval                           # minutes, as the venue states it per symbol
    return r


def _envelope(rows: Sequence[Mapping[str, Any]], cursor: str, time_ms: int, category: str = "linear") -> bytes:
    return json.dumps({"retCode": 0, "retMsg": "OK", "result": {"category": category, "list": list(rows), "nextPageCursor": cursor}, "time": time_ms},
                      sort_keys=True).encode("utf-8")


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def write_capture(root: str, name: str, snapshot_ms: int, by_status: Mapping[str, Sequence[Mapping[str, Any]]], *, page_rows: int = 2,
                  observability: str = "OBSERVED", schema: str = SCHEMA, dataset_status: str = "COMPLETE", complete: bool = True,
                  statuses: Sequence[str] = STATUSES, default_partition: bool = False, spot_symbols: Optional[Sequence[str]] = None) -> str:
    """Write one capture directory ``root/name`` and return its path. ``by_status`` maps status -> rows (a missing status key is an EMPTY complete partition,
    unless it is not in ``statuses``, in which case the partition is not written at all)."""
    d = os.path.join(root, name)
    os.makedirs(d)
    partitions: List[Dict[str, Any]] = []
    plan: List[Any] = [("linear", st, list(by_status.get(st, []))) for st in statuses]
    if default_partition:                                   # the unfiltered 'default' partition repeats every instrument
        plan.append(("linear", None, [r for st in statuses for r in by_status.get(st, [])]))
    if spot_symbols is not None:
        plan.append(("spot", None, [{"symbol": x, "status": "Trading", "baseCoin": x[:-4], "quoteCoin": "USDT"} for x in spot_symbols]))
    for cat, st, rows in plan:
        label = st or "default"
        chunks = [rows[i:i + page_rows] for i in range(0, len(rows), page_rows)] or [[]]
        pages = []
        cursors = [""] + [f"{cat}-{label}-cur-{k}" for k in range(1, len(chunks))] + [""]
        for k, chunk in enumerate(chunks):
            fname = f"{cat}_{label}_page{k + 1:04d}.json"
            body = _envelope(chunk, cursors[k + 1], snapshot_ms, cat)
            with open(os.path.join(d, fname), "wb") as f:
                f.write(body)
            pages.append({"n": k + 1, "file": fname, "sha256": sha(body), "bytes": len(body), "cursor_in": cursors[k], "cursor_out": cursors[k + 1],
                          "rows": len(chunk), "recv_ms": snapshot_ms})
        partitions.append({"category": cat, "status": st, "params": {"category": cat, "limit": 1000}, "pages": pages, "outcome": "OBSERVED_END_OF_LIST", "complete": complete})
    manifest = {"schema": schema, "snapshot_ms": snapshot_ms, "partitions": partitions,
                "status_coverage": {st: "COMPLETE" for st in statuses}, "delisting_observability": observability, "dataset_status": dataset_status}
    with open(os.path.join(d, "manifest.json"), "wb") as f:
        f.write((json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8"))
    return d


def part(m: Mapping[str, Any], status: Any, category: str = "linear") -> Dict[str, Any]:
    return next(p for p in m["partitions"] if p["status"] == status and p["category"] == category)


def manifest_path(d: str) -> str:
    return os.path.join(d, "manifest.json")


def read_manifest(d: str) -> Dict[str, Any]:
    with open(manifest_path(d), "rb") as f:
        return json.loads(f.read())


def write_manifest(d: str, m: Mapping[str, Any]) -> None:
    with open(manifest_path(d), "wb") as f:
        f.write((json.dumps(m, indent=2, sort_keys=True) + "\n").encode("utf-8"))


def ok_verifier(path: str) -> Dict[str, Any]:
    """Stands in for ``universe_capture.verify_capture_dir`` saying OK (the harness must NOT rely on this alone)."""
    with open(manifest_path(path), "rb") as f:
        raw = f.read()
    m = json.loads(raw)
    cov = {"dataset_status": m.get("dataset_status"), "delisting_observability": m.get("delisting_observability"),
           "partitions": [{"category": p["category"], "status": p["status"], "complete": p["complete"]} for p in m["partitions"]]}
    return {"ok": True, "problems": [], "coverage": cov, "manifest_sha256": sha(raw)}


def failing_verifier(path: str) -> Dict[str, Any]:
    r = ok_verifier(path)                                     # same (complete) coverage: only ``ok`` differs
    r.pop("manifest_sha256", None)
    return dict(r, ok=False, problems=["PAGE_SHA256_MISMATCH:linear_Trading_page0001.json"])


def standard_captures(root: str) -> List[str]:
    """Four dated snapshots of a small universe, window 0..10_000 ms:
        AAA listed 0, delisted 5000        BBB listed 0, never delisted        CCC listed 1000, delisted 9000
    t=0: AAA, BBB  |  t=2000: + CCC  |  t=6000: AAA Closed (5000), CCC Trading  |  t=9500: CCC Closed (9000)."""
    a_open, b, c_open = row("AAAUSDT", "Trading", 0), row("BBBUSDT", "Trading", 0), row("CCCUSDT", "Trading", 1000)
    a_closed, c_closed = row("AAAUSDT", "Closed", 0, 5000), row("CCCUSDT", "Closed", 1000, 9000)
    return [
        write_capture(root, "cap0", 0, {"Trading": [a_open, b]}),
        write_capture(root, "cap1", 2000, {"Trading": [a_open, b, c_open]}),
        write_capture(root, "cap2", 6000, {"Trading": [b, c_open], "Closed": [a_closed]}),
        write_capture(root, "cap3", 9500, {"Trading": [b], "Closed": [a_closed, c_closed]}),
    ]
