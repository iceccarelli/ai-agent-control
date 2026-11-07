"""Owner-invoked, read-only, point-in-time capture of the PUBLIC Bybit USDT-linear perpetual UNIVERSE (every listed instrument, not a winners list).

Why. A universe built from today's survivors cannot measure survivorship: delisted and renamed contracts vanish and interval/cap changes are silent.
This tool writes one dated snapshot per run so two snapshots can be diffed (``diff``) and the listings, delistings, status moves and funding-interval /
cap changes become measurable. It selects NOTHING by performance: every instrument row is kept (rows that are not USDT-linear perpetuals are COUNTED
as rejected, with a reason and the symbol, never silently dropped; the raw bytes keep them all).

    GET <mainnet REST host from bot/venues.py>/v5/market/instruments-info?category=linear&limit=1000[&cursor=<nextPageCursor>]   (cursor-paginated)
    GET <mainnet REST host from bot/venues.py>/v5/market/tickers?category=linear                                               (one snapshot)

What it is NOT. Not an order path, not signed (no key is read, no header added), not an account read. It is never run by the test suite or CI (tests
inject ``fetch`` and a socket guard fails any real connection). The agent that wrote it has not executed it against an exchange; the owner runs it.

What it guarantees.
  * URLs are built from constants (registry host, two fixed paths, one category, a bounded printable cursor). No redirect is followed; the body is bounded.
  * Every response body is written BYTE-FOR-BYTE as received, created exclusively (an existing file is never overwritten), mode 0600, BEFORE it is
    parsed. A manifest lists each request, file, SHA-256, row count, receive time (injected clock, ms) and the venue's own ``time`` (skew reported).
  * The cursor chain is validated: a repeated cursor, an empty page that still names a cursor, an over-large page, or hitting the page cap with a cursor
    left is FATAL (a partial universe is never presented as complete). A conflicting duplicate symbol is fatal; an identical one is counted.
  * Each run writes into a NEW directory (a half-finished run is abandoned, not resumed into). ``universe-<ts>.jsonl`` is sorted by symbol only.
What it cannot show: that the bytes came from the exchange (a hash proves what was read, not who produced it).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

BOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BOT)
sys.path.insert(0, os.path.join(BOT, "tools"))

import account_review as AR  # noqa: E402  (the one protected-name rule)
import venues  # noqa: E402

SCHEMA_VERSION = 1
PATH_INSTRUMENTS = "/v5/market/instruments-info"
PATH_TICKERS = "/v5/market/tickers"
CATEGORY = "linear"
PAGE_LIMIT = 1000                        # the endpoint's documented maximum for instruments-info
MAX_PAGES = 20
MAX_BODY_BYTES = 8 << 20
MAX_CURSOR_LEN = 512
TIMEOUT_S = 15.0
PAUSE_S = 0.25
SKEW_WARN_MS = 5_000                     # |receive - venue time| beyond this is flagged (never corrected)
PERP_TYPE = "LinearPerpetual"
QUOTE = "USDT"
MAX_FUNDING_INTERVAL_MIN = 24 * 60

_SYMBOL = re.compile(r"^[A-Z0-9]{2,24}$", re.ASCII)
_DEC = re.compile(r"^[0-9]+(\.[0-9]+)?$", re.ASCII)
_SDEC = re.compile(r"^-?[0-9]+(\.[0-9]+)?$", re.ASCII)
_UINT = re.compile(r"^[0-9]{1,19}$", re.ASCII)
_CURSOR = re.compile(r"^[A-Za-z0-9%._~=+/:,-]{1,512}$", re.ASCII)


class CaptureError(ValueError):
    """The capture cannot continue safely. Files already written are kept; no manifest is written for an incomplete run."""


# ---------------------------------------------------------------- transport

def _base() -> str:
    base = venues.rest_for("mainnet")
    if not base.startswith("https://"):
        raise CaptureError("the registry's host is not https")
    return base


def request_url(path: str, cursor: Optional[str] = None) -> str:
    if path == PATH_INSTRUMENTS:
        q = [("category", CATEGORY), ("limit", PAGE_LIMIT)]
        if cursor:
            if not _CURSOR.match(cursor):
                raise CaptureError("refusing a cursor that is not a short printable token")
            q.append(("cursor", cursor))
    elif path == PATH_TICKERS:
        if cursor:
            raise CaptureError("the tickers snapshot is not paginated")
        q = [("category", CATEGORY)]
    else:
        raise CaptureError("refusing a path that is not one of the two pinned public paths")
    return f"{_base()}{path}?{urllib.parse.urlencode(q)}"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_a, **_k):                      # a redirect is a different host: refuse it
        return None


def http_fetch(url: str) -> bytes:
    """The only network primitive: one unauthenticated GET, no redirects, bounded body. Tests never reach it."""
    base = _base()
    if not any(url.startswith(base + p + "?") for p in (PATH_INSTRUMENTS, PATH_TICKERS)):
        raise CaptureError("refusing a URL that is not the registry host and a pinned universe path")
    opener = urllib.request.build_opener(_NoRedirect)
    req = urllib.request.Request(url, method="GET", headers={"Accept": "application/json", "User-Agent": "universe-capture/1"})
    try:
        with opener.open(req, timeout=TIMEOUT_S) as resp:
            if resp.status != 200:
                raise CaptureError(f"HTTP {resp.status}")
            body = resp.read(MAX_BODY_BYTES + 1)
    except (urllib.error.URLError, OSError) as exc:
        raise CaptureError(f"request failed ({type(exc).__name__})") from exc
    if len(body) > MAX_BODY_BYTES:
        raise CaptureError("response larger than any universe page")
    return body


def _create_exclusive(path: str, data: bytes) -> None:
    AR._refuse_protected(path)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    except FileExistsError as exc:
        raise CaptureError(f"{os.path.basename(path)} exists: a captured file is never overwritten") from exc
    except OSError as exc:
        raise CaptureError(f"{os.path.basename(path)}: cannot create ({exc.strerror})") from exc
    try:
        view = memoryview(data)
        while view:
            n = os.write(fd, view)
            if n <= 0:
                raise CaptureError("write made no progress")
            view = view[n:]
        os.fsync(fd)
    finally:
        os.close(fd)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------- envelope / rows

def _envelope(body: bytes, label: str) -> Tuple[Dict[str, Any], List[Any], str, Optional[int]]:
    """(result, rows, nextPageCursor, venue time ms). Strict: anything else is fatal."""
    try:
        doc = json.loads(body.decode("utf-8", "strict"))
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise CaptureError(f"{label}: not UTF-8 JSON") from exc
    if not isinstance(doc, dict) or isinstance(doc.get("retCode"), bool) or doc.get("retCode") != 0:
        raise CaptureError(f"{label}: not a successful V5 envelope (retCode {doc.get('retCode') if isinstance(doc, dict) else None!r})")
    res = doc.get("result")
    if not isinstance(res, dict) or res.get("category") != CATEGORY or not isinstance(res.get("list"), list):
        raise CaptureError(f"{label}: result.category/list is not the linear list")
    nxt = res.get("nextPageCursor", "")
    if nxt is None:
        nxt = ""
    if not isinstance(nxt, str) or (nxt and not _CURSOR.match(nxt)):
        raise CaptureError(f"{label}: nextPageCursor is not a short printable token")
    t = doc.get("time")
    t = int(t) if isinstance(t, int) and not isinstance(t, bool) and t > 0 else (int(t) if isinstance(t, str) and _UINT.match(t) else None)
    return res, res["list"], nxt, t


def _skew(recv_ms: int, venue_ms: Optional[int]) -> Dict[str, Any]:
    if venue_ms is None:
        return {"venue_time_ms": None, "skew_ms": None, "skew_flag": "NO_VENUE_TIME"}
    d = recv_ms - venue_ms
    return {"venue_time_ms": venue_ms, "skew_ms": d, "skew_flag": "SKEW" if abs(d) > SKEW_WARN_MS else ""}


def _dec(row: Dict[str, Any], *path: str, positive: bool = True, signed: bool = False, required: bool = True) -> Optional[str]:
    cur: Any = row
    for k in path:
        cur = cur.get(k) if isinstance(cur, dict) else None
    name = ".".join(path)
    if cur is None or cur == "":
        if required:
            raise ValueError(f"missing {name}")
        return None
    if not isinstance(cur, str) or not (_SDEC if signed else _DEC).match(cur):
        raise ValueError(f"{name} is not a decimal string")
    if positive and not float(cur) > 0:
        raise ValueError(f"{name} must be > 0")
    return cur


def _uint(row: Dict[str, Any], key: str) -> int:
    v = row.get(key)
    if isinstance(v, bool) or not (isinstance(v, int) and v >= 0 or isinstance(v, str) and _UINT.match(v)):
        raise ValueError(f"{key} is not a whole number")
    return int(v)


def normalize_instrument(row: Any) -> Tuple[Optional[Dict[str, Any]], Optional[str], str]:
    """(normalized row, None, symbol) for a valid USDT-linear perpetual, else (None, reason, symbol). Decimals stay the venue's own strings."""
    if not isinstance(row, dict):
        return None, "not_an_object", ""
    sym = row.get("symbol")
    if not isinstance(sym, str) or not _SYMBOL.match(sym):
        return None, "bad_symbol", str(sym)[:24]
    if row.get("contractType") != PERP_TYPE:
        return None, f"not_perpetual:{str(row.get('contractType'))[:24]}", sym
    if row.get("quoteCoin") != QUOTE or row.get("settleCoin") != QUOTE:
        return None, f"not_usdt:{str(row.get('quoteCoin'))[:12]}/{str(row.get('settleCoin'))[:12]}", sym
    try:
        status = row.get("status")
        if not isinstance(status, str) or not status or not status.isascii():
            raise ValueError("status")
        interval = _uint(row, "fundingInterval")
        if not 0 < interval <= MAX_FUNDING_INTERVAL_MIN:
            raise ValueError(f"fundingInterval {interval} min is not sane")
        upper = _dec(row, "upperFundingRate", positive=False, signed=True)
        lower = _dec(row, "lowerFundingRate", positive=False, signed=True)
        if not (float(lower) < 0 < float(upper) and float(upper) <= 0.5 and float(lower) >= -0.5):
            raise ValueError("funding bounds are not a sane lower<0<upper fraction")
        out = {
            "schema": SCHEMA_VERSION, "symbol": sym, "status": status, "contractType": PERP_TYPE,
            "baseCoin": str(row.get("baseCoin", "")), "quoteCoin": QUOTE, "settleCoin": QUOTE,
            "launchTime": _uint(row, "launchTime"), "deliveryTime": _uint(row, "deliveryTime"),
            "fundingInterval_min": interval, "upperFundingRate": upper, "lowerFundingRate": lower,
            "tickSize": _dec(row, "priceFilter", "tickSize"), "minPrice": _dec(row, "priceFilter", "minPrice", required=False),
            "qtyStep": _dec(row, "lotSizeFilter", "qtyStep"), "minOrderQty": _dec(row, "lotSizeFilter", "minOrderQty"),
            "maxOrderQty": _dec(row, "lotSizeFilter", "maxOrderQty", required=False),
            "minNotionalValue": _dec(row, "lotSizeFilter", "minNotionalValue", positive=False),
        }
    except ValueError as exc:
        return None, f"invalid_field:{exc}"[:80], sym
    return out, None, sym


_TICKER_KEEP = ("markPrice", "indexPrice", "lastPrice", "fundingRate", "nextFundingTime", "fundingIntervalHour", "fundingCap",
                "openInterest", "openInterestValue", "volume24h", "turnover24h")


def _tickers(rows: List[Any], label: str) -> Tuple[Dict[str, Dict[str, Any]], int]:
    out: Dict[str, Dict[str, Any]] = {}
    dups = 0
    for r in rows:
        if not isinstance(r, dict) or not isinstance(r.get("symbol"), str) or not _SYMBOL.match(r["symbol"]):
            raise CaptureError(f"{label}: a ticker row has no sane symbol")
        t = {k: r.get(k) for k in _TICKER_KEEP if r.get(k) not in (None, "")}
        for k, v in t.items():
            if not isinstance(v, str):
                raise CaptureError(f"{label}: {r['symbol']} {k} is not a string")
        if r["symbol"] in out:
            if out[r["symbol"]] != t:
                raise CaptureError(f"{label}: ticker {r['symbol']} appears twice with different values; nothing is merged")
            dups += 1
        out[r["symbol"]] = t
    return out, dups


# ---------------------------------------------------------------- capture

def capture(out_dir: str, *, fetch: Callable[[str], bytes] = http_fetch, clock_ms: Callable[[], int] = lambda: int(time.time() * 1000),
            sleep: Callable[[float], None] = time.sleep, max_pages: int = MAX_PAGES) -> Dict[str, Any]:
    """Page the whole linear instrument list, snapshot the tickers, write the normalized universe and a manifest (exclusively). Returns the manifest."""
    parent = os.path.dirname(os.path.abspath(out_dir))
    if os.path.islink(out_dir) or os.path.islink(parent) or not os.path.isdir(parent):
        raise CaptureError("the output directory must be a real directory, not a link")
    AR._refuse_protected(os.path.join(out_dir, "manifest.json"))
    os.makedirs(out_dir, mode=0o700, exist_ok=True)
    if os.listdir(out_dir):
        raise CaptureError("the output directory is not empty: each run goes in a NEW directory, nothing is overwritten")

    started = int(clock_ms())
    pages: List[Dict[str, Any]] = []
    seen_cursors = set()
    cursor = ""
    accepted: Dict[str, Dict[str, Any]] = {}
    rejected: List[Dict[str, str]] = []
    reject_counts: Dict[str, int] = {}
    identical_dups = 0
    total_rows = 0
    stop = "page_cap"
    for n in range(1, max_pages + 1):
        url = request_url(PATH_INSTRUMENTS, cursor or None)
        body = fetch(url)
        recv = int(clock_ms())
        label = f"instruments_page{n:04d}"
        if len(body) > MAX_BODY_BYTES:
            raise CaptureError(f"{label}: response larger than any universe page")
        fname = f"{label}.json"
        _create_exclusive(os.path.join(out_dir, fname), body)            # unedited, before anything is concluded about it
        _res, rows, nxt, vtime = _envelope(body, label)
        if len(rows) > PAGE_LIMIT:
            raise CaptureError(f"{label}: {len(rows)} rows exceeds the endpoint's page limit {PAGE_LIMIT}")
        total_rows += len(rows)
        for r in rows:
            norm, why, sym = normalize_instrument(r)
            if norm is None:
                key = why.split(":")[0]
                reject_counts[key] = reject_counts.get(key, 0) + 1
                rejected.append({"symbol": sym, "reason": why or ""})
                continue
            if sym in accepted:
                if accepted[sym] != norm:
                    raise CaptureError(f"{label}: {sym} appears twice with conflicting definitions; nothing is merged")
                identical_dups += 1
                continue
            accepted[sym] = norm
        pages.append({"page": n, "file": fname, "sha256": _sha(body), "bytes": len(body), "request_cursor": cursor, "next_cursor": nxt,
                      "rows": len(rows), "recv_ms": recv, **_skew(recv, vtime)})
        if not nxt:
            stop = "end_of_list"
            break
        if nxt == cursor or nxt in seen_cursors:
            raise CaptureError(f"{label}: the cursor did not advance (repeated cursor); stopping rather than loop")
        if not rows:
            raise CaptureError(f"{label}: an empty page still names a next cursor")
        seen_cursors.add(nxt)
        if cursor:
            seen_cursors.add(cursor)
        cursor = nxt
        sleep(PAUSE_S)
    if stop != "end_of_list":
        raise CaptureError(f"page cap {max_pages} reached with a cursor still pending: the universe is INCOMPLETE and no manifest is written")

    turl = request_url(PATH_TICKERS)
    tbody = fetch(turl)
    trecv = int(clock_ms())
    if len(tbody) > MAX_BODY_BYTES:
        raise CaptureError("tickers: response larger than any universe page")
    _create_exclusive(os.path.join(out_dir, "tickers.json"), tbody)
    _tres, trows, tnxt, tvtime = _envelope(tbody, "tickers")
    if tnxt:
        raise CaptureError("tickers: the venue named a cursor on a snapshot that must be complete")
    tickers, tdups = _tickers(trows, "tickers")
    tpage = {"file": "tickers.json", "sha256": _sha(tbody), "bytes": len(tbody), "rows": len(trows), "recv_ms": trecv, **_skew(trecv, tvtime)}

    snapshot_ms = started
    lines: List[bytes] = []
    interval_mismatch: List[str] = []
    for sym in sorted(accepted):
        row = dict(accepted[sym], snapshot_ms=snapshot_ms)
        tk = tickers.get(sym)
        row["ticker"] = tk
        if tk is not None and tk.get("fundingIntervalHour") is not None:
            try:
                if int(float(tk["fundingIntervalHour"]) * 60) != row["fundingInterval_min"]:
                    interval_mismatch.append(sym)
            except ValueError:
                interval_mismatch.append(sym)
        lines.append((json.dumps(row, sort_keys=True, allow_nan=False, separators=(",", ":")) + "\n").encode("utf-8"))
    ufile = f"universe-{snapshot_ms}.jsonl"
    ubytes = b"".join(lines)
    _create_exclusive(os.path.join(out_dir, ufile), ubytes)

    skew_pages = [p["page"] for p in pages if p["skew_flag"]]
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "request": {"host": venues.host_of(_base()), "paths": [PATH_INSTRUMENTS, PATH_TICKERS], "category": CATEGORY, "limit": PAGE_LIMIT},
        "snapshot_ms": snapshot_ms, "started_ms": started, "finished_ms": int(clock_ms()),
        "stopped_because": stop, "pages": pages, "tickers": tpage,
        "universe": {"file": ufile, "sha256": _sha(ubytes), "instruments": len(accepted)},
        "rows_seen": total_rows, "rows_accepted": len(accepted), "rows_rejected": len(rejected), "rejected_by_reason": dict(sorted(reject_counts.items())),
        "rejected": rejected, "identical_duplicates": identical_dups, "ticker_identical_duplicates": tdups,
        "gaps": {"instruments_without_ticker": sorted(s for s in accepted if s not in tickers),
                 "tickers_without_usdt_perp_instrument": sorted(s for s in tickers if s not in accepted and s not in {r["symbol"] for r in rejected}),
                 "funding_interval_instrument_vs_ticker_mismatch": interval_mismatch},
        "clock": {"skew_warn_ms": SKEW_WARN_MS, "pages_flagged": skew_pages, "tickers_flagged": bool(tpage["skew_flag"])},
        "note": "point-in-time, never filtered by performance; rejected rows are counted not hidden; a hash shows what was read, not who produced it",
    }
    _create_exclusive(os.path.join(out_dir, "manifest.json"), (json.dumps(manifest, indent=2, sort_keys=True, allow_nan=False) + "\n").encode("utf-8"))
    return manifest


# ---------------------------------------------------------------- diff

def load_universe(path: str) -> Dict[str, Dict[str, Any]]:
    real = AR._refuse_protected(path)
    out: Dict[str, Dict[str, Any]] = {}
    with open(real, "rb") as fh:
        for i, line in enumerate(fh, 1):
            try:
                r = json.loads(line.decode("utf-8", "strict"))
            except (UnicodeDecodeError, ValueError) as exc:
                raise CaptureError(f"{os.path.basename(path)} line {i}: not JSON") from exc
            if not isinstance(r, dict) or r.get("schema") != SCHEMA_VERSION or not isinstance(r.get("symbol"), str):
                raise CaptureError(f"{os.path.basename(path)} line {i}: not a schema-{SCHEMA_VERSION} universe row")
            if r["symbol"] in out:
                raise CaptureError(f"{os.path.basename(path)} line {i}: duplicate symbol {r['symbol']}")
            out[r["symbol"]] = r
    return out


def diff_snapshots(old: Dict[str, Dict[str, Any]], new: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """Listings, delistings and changes between two point-in-time universes. A symbol absent from ``new`` is a delisting ONLY relative to these files."""
    listed = sorted(set(new) - set(old))
    delisted = sorted(set(old) - set(new))
    status, interval, caps, other = [], [], [], []
    for s in sorted(set(old) & set(new)):
        a, b = old[s], new[s]
        if a["status"] != b["status"]:
            status.append({"symbol": s, "from": a["status"], "to": b["status"]})
        if a["fundingInterval_min"] != b["fundingInterval_min"]:
            interval.append({"symbol": s, "from_min": a["fundingInterval_min"], "to_min": b["fundingInterval_min"]})
        if (a["upperFundingRate"], a["lowerFundingRate"]) != (b["upperFundingRate"], b["lowerFundingRate"]):
            caps.append({"symbol": s, "from": [a["lowerFundingRate"], a["upperFundingRate"]], "to": [b["lowerFundingRate"], b["upperFundingRate"]]})
        for k in ("launchTime", "deliveryTime", "tickSize", "qtyStep", "minOrderQty", "minNotionalValue"):
            if a.get(k) != b.get(k):
                other.append({"symbol": s, "field": k, "from": a.get(k), "to": b.get(k)})
    t0 = next((r["snapshot_ms"] for r in old.values()), None)
    t1 = next((r["snapshot_ms"] for r in new.values()), None)
    return {"old_count": len(old), "new_count": len(new), "listed": listed, "delisted": delisted, "status_changes": status,
            "funding_interval_changes": interval, "funding_bound_changes": caps, "other_changes": other,
            "gap_ms": (t1 - t0) if isinstance(t0, int) and isinstance(t1, int) else None}


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Owner-invoked read-only point-in-time capture of the public Bybit USDT-linear perpetual universe.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("capture")
    c.add_argument("--out-dir", required=True, help="a NEW directory per run; nothing in it is ever overwritten")
    d = sub.add_parser("diff")
    d.add_argument("old")
    d.add_argument("new")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "capture":
            m = capture(a.out_dir)
            print(json.dumps({k: m[k] for k in ("snapshot_ms", "rows_seen", "rows_accepted", "rows_rejected", "rejected_by_reason", "clock")}, indent=2, sort_keys=True))
        else:
            print(json.dumps(diff_snapshots(load_universe(a.old), load_universe(a.new)), indent=2, sort_keys=True))
    except (CaptureError, AR.RecordError, OSError) as exc:
        print(f"universe_capture: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
