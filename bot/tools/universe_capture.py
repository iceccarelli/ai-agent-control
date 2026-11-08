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

Schema v2 (``capture_universe``, the ``capture`` CLI). Same guarantees, plus: every status partition of ``instruments-info`` is asked for and what the venue
answers is RECORDED (never assumed): ``OBSERVED_ROWS`` / ``OBSERVED_EMPTY`` / ``REFUSED_BY_VENUE:<code>/<msg>`` / ``TRANSPORT_ERROR:<class>`` / ``PARTIAL``.
A venue that does not serve ``status=Closed`` is a recorded answer, not a crash. ``verify_capture_dir`` re-derives every claim from the raw bytes. The legacy
single-partition ``capture`` function and its schema-1 manifest are unchanged for ``market_collector``.

Two things this tool states plainly rather than implies. (1) ``dataset_status`` and ``delisting_observability`` are separate facts: a capture whose ``status=Closed``
partition the venue refused (or served EMPTY) is still COMPLETE at capture level, and its universe is SURVIVORS ONLY; an empty Closed partition means 'no Closed rows
served', not 'no delistings happened' (``describe_delisting_observability``). (2) One symbolId on successive names where the earlier one has ENDED (Closed/Delivering, or
delivered before the next launched) is a RENAME, recorded in the manifest's ``renames`` as lineage and re-derived by ``verify_capture_dir``; one symbolId on two Trading
rows (or any pair with no ended predecessor) remains a CONFLICT. A manifest written before ``renames`` existed verifies only if the capture contains no rename.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from decimal import Decimal
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

BOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BOT)
sys.path.insert(0, os.path.join(BOT, "tools"))

import account_review as AR  # noqa: E402  (the one protected-name rule)
import runtime_capability as RC  # noqa: E402  (the role model only: pure, signs nothing, sends nothing)
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
SCHEMA_V2 = "universe_capture/v2"
ROW_SCHEMA_V2 = 2
STATUSES = ("Trading", "PendingOpen", "PreLaunch", "Delivering", "Closed")        # the documented status enum, each its own partition
CATEGORIES = ("linear", "inverse", "spot")
RETRIES = 3
BACKOFF_S = 1.0
MAX_BACKOFF_S = 30.0
RATE_LIMIT_CODES = (10006,)
MAX_REJECT_LIST = 200                      # rejected rows listed (hashed) in a manifest; the count is always complete
MAX_SPAN_MS = 300_000                      # first to last response further apart than this is not one point in time (STALE)
COVERAGE_NOTE = ("History before the first capture is NOT point-in-time verified. A status=Closed partition shows only the instruments the venue still serves; "
                 "it may omit old delistings, so an absent symbol is not proof it never existed or was never delisted.")

_SYMBOL = re.compile(r"^[A-Z0-9]{2,24}\Z", re.ASCII)
_DEC = re.compile(r"^[0-9]+(\.[0-9]+)?\Z", re.ASCII)
_SDEC = re.compile(r"^-?[0-9]+(\.[0-9]+)?\Z", re.ASCII)
_UINT = re.compile(r"^[0-9]{1,19}\Z", re.ASCII)
_CURSOR = re.compile(r"^[A-Za-z0-9%._~=+/:,-]{1,512}\Z", re.ASCII)


class CaptureError(ValueError):
    """The capture cannot continue safely. Files already written are kept; no manifest is written for an incomplete run."""


class TransportFailure(CaptureError):
    """No usable response (connection, timeout, bad status). ``cls`` names the failure class and is recorded in a partition outcome."""

    def __init__(self, msg: str, cls: str = "TransportFailure"):
        super().__init__(msg)
        self.cls = cls


class HttpStatusError(TransportFailure):
    def __init__(self, status: int, body: bytes = b""):
        super().__init__(f"HTTP {status}", f"HTTP_{status}")
        self.status = status
        self.body = body


class CapabilityRefusal(CaptureError):
    """The public_collector role refused the request. Never a partition outcome: it is a programming error and stops the run."""


# ---------------------------------------------------------------- transport

def _base() -> str:
    base = venues.rest_for("mainnet")
    if not base.startswith("https://"):
        raise CaptureError("the registry's host is not https")
    return base


def request_url(path: str, cursor: Optional[str] = None, *, category: str = CATEGORY, status: Optional[str] = None) -> str:
    if category not in CATEGORIES:
        raise CaptureError("refusing a category that is not linear, inverse or spot")
    if status is not None and status not in STATUSES:
        raise CaptureError("refusing a status that is not in the documented enum")
    if path == PATH_INSTRUMENTS:
        q: List[Tuple[str, Any]] = [("category", category)]
        if category != "spot":
            q.append(("limit", PAGE_LIMIT))
        if status is not None:
            if category == "spot":
                raise CaptureError("spot has no status partitions")
            q.append(("status", status))
        if cursor:
            if category == "spot":
                raise CaptureError("spot does not paginate")
            if not _CURSOR.fullmatch(cursor):
                raise CaptureError("refusing a cursor that is not a short printable token")
    elif path == PATH_TICKERS:
        if cursor or status is not None:
            raise CaptureError("the tickers snapshot is not paginated and has no status")
        q = [("category", category)]
    else:
        raise CaptureError("refusing a path that is not one of the two pinned public paths")
    # The venue's cursor is already percent-encoded (e.g. ``first%3D1000...``): it is appended UNCHANGED, never re-quoted (re-encoding would double-encode it).
    tail = f"&cursor={cursor}" if path == PATH_INSTRUMENTS and cursor else ""
    return f"{_base()}{path}?{urllib.parse.urlencode(q)}{tail}"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_a, **_k):                      # a redirect is a different host: refuse it
        return None


class _UrllibTransport:
    """The innermost transport: one GET through urllib with no redirects. Only ever reached THROUGH ``CapabilityTransport`` (see ``_transport``)."""

    def request(self, method: str, url: str, *, headers: Dict[str, str], body: Optional[str], timeout: float) -> bytes:
        opener = urllib.request.build_opener(_NoRedirect)
        req = urllib.request.Request(url, method="GET", headers=dict(headers))
        try:
            with opener.open(req, timeout=timeout) as resp:
                if resp.status != 200:
                    raise HttpStatusError(resp.status)
                data = resp.read(MAX_BODY_BYTES + 1)
        except urllib.error.HTTPError as exc:                    # a venue may answer 4xx with a JSON body: keep it for the caller to judge
            try:
                body = exc.read(MAX_BODY_BYTES + 1)
            except Exception:  # noqa: BLE001
                body = b""
            raise HttpStatusError(exc.code, body[:MAX_BODY_BYTES]) from exc
        except (urllib.error.URLError, OSError) as exc:
            raise TransportFailure(f"request failed ({type(exc).__name__})", type(exc).__name__) from exc
        if len(data) > MAX_BODY_BYTES:
            raise CaptureError("response larger than any universe page")
        return data


def _transport() -> "RC.CapabilityTransport":
    return RC.CapabilityTransport(_UrllibTransport(), RC.Capability.for_role(RC.PUBLIC_COLLECTOR, venue="mainnet"))


def http_request(method: Any, url: Any, *, headers: Any = None, body: Any = None) -> bytes:
    """The only network primitive; the ``public_collector`` role refuses anything but an unsigned GET of a public market path BEFORE a connection is opened."""
    base = _base()
    if not isinstance(url, str) or not any(url.startswith(base + p + "?") for p in (PATH_INSTRUMENTS, PATH_TICKERS)):
        raise CaptureError("refusing a URL that is not the registry host and a pinned universe path")
    hdrs = {"Accept": "application/json", "User-Agent": "universe-capture/1"} if headers is None else headers
    try:
        return _transport().request(method, url, headers=hdrs, body=body, timeout=TIMEOUT_S)
    except RC.CapabilityRefused as exc:
        raise CapabilityRefusal(f"refused by the public_collector role: {exc.code}") from exc


def http_fetch(url: str) -> bytes:
    """One unauthenticated GET through ``http_request`` (so through the role). Tests never reach it."""
    return http_request("GET", url)


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
    if not isinstance(nxt, str) or (nxt and not _CURSOR.fullmatch(nxt)):
        raise CaptureError(f"{label}: nextPageCursor is not a short printable token")
    t = doc.get("time")
    t = int(t) if isinstance(t, int) and not isinstance(t, bool) and t > 0 else (int(t) if isinstance(t, str) and _UINT.fullmatch(t) else None)
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
    if not isinstance(cur, str) or not (_SDEC if signed else _DEC).fullmatch(cur):
        raise ValueError(f"{name} is not a decimal string")
    if positive and not float(cur) > 0:
        raise ValueError(f"{name} must be > 0")
    return cur


def _uint(row: Dict[str, Any], key: str) -> int:
    v = row.get(key)
    if isinstance(v, bool) or not (isinstance(v, int) and v >= 0 or isinstance(v, str) and _UINT.fullmatch(v)):
        raise ValueError(f"{key} is not a whole number")
    return int(v)


def normalize_instrument(row: Any) -> Tuple[Optional[Dict[str, Any]], Optional[str], str]:
    """(normalized row, None, symbol) for a valid USDT-linear perpetual, else (None, reason, symbol). Decimals stay the venue's own strings."""
    if not isinstance(row, dict):
        return None, "not_an_object", ""
    sym = row.get("symbol")
    if not isinstance(sym, str) or not _SYMBOL.fullmatch(sym):
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


MAX_TURNOVER = 10 ** 15                 # a 24h USDT turnover above this is a poisoned response, not a market


def _bounded_turnover(v: str) -> bool:
    return len(v) <= 64 and _DEC.fullmatch(v) is not None and Decimal(v) <= MAX_TURNOVER


def _printable(v: Any) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "?", v)[:24] if isinstance(v, str) else f"<{type(v).__name__}>"


def _row_sha(row: Any) -> str:
    return _sha(json.dumps(row, sort_keys=True, default=str, separators=(",", ":")).encode("utf-8", "replace"))


def _reject_entry(row: Any, reason: str, symbol: Any) -> Dict[str, str]:
    return {"symbol": _printable(symbol), "reason": reason, "row_sha256": _row_sha(row)}


def summarize_rejects(entries: List[Dict[str, Any]], cap: int = MAX_REJECT_LIST) -> Dict[str, Any]:
    by: Dict[str, int] = {}
    for e in entries:
        k = e["reason"].split(":")[0]
        by[k] = by.get(k, 0) + 1
    return {"count": len(entries), "by_reason": dict(sorted(by.items())), "listed": entries[:cap], "list_capped": len(entries) > cap}


def classify_tickers(rows: List[Any], label: str) -> Tuple[Dict[str, Dict[str, Any]], int, List[Dict[str, str]]]:
    """(accepted by symbol, identical duplicates, rejected entries). A row whose symbol is not a plain symbol (a dated future, an odd character, not an
    object) is REJECTED with a reason and counted: never fatal. An accepted symbol still fails closed: a non-string field, an absurd turnover or two
    different rows for one symbol raise ``CaptureError``."""
    out: Dict[str, Dict[str, Any]] = {}
    rej: List[Dict[str, str]] = []
    dups = 0
    for r in rows:
        if not isinstance(r, dict):
            rej.append(_reject_entry(r, "not_an_object", None))
            continue
        sym = r.get("symbol")
        if not isinstance(sym, str):
            rej.append(_reject_entry(r, "symbol_not_string", sym))
            continue
        if not _SYMBOL.fullmatch(sym):
            rej.append(_reject_entry(r, "symbol_has_hyphen" if "-" in sym else "bad_symbol", sym))
            continue
        t = {k: r.get(k) for k in _TICKER_KEEP if r.get(k) not in (None, "")}
        for k, v in t.items():
            if not isinstance(v, str):
                raise CaptureError(f"{label}: {sym} {k} is not a string")
        if "turnover24h" in t and not _bounded_turnover(t["turnover24h"]):
            raise CaptureError(f"{label}: {sym} turnover24h is not a bounded non-negative decimal")
        if sym in out:
            if out[sym] != t:
                raise CaptureError(f"{label}: ticker {sym} appears twice with different values; nothing is merged")
            dups += 1
        out[sym] = t
    return out, dups, rej


def _tickers(rows: List[Any], label: str) -> Tuple[Dict[str, Dict[str, Any]], int]:
    out, dups, _rej = classify_tickers(rows, label)
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
    tickers, tdups, trej = classify_tickers(trows, "tickers")
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
        "rejected": rejected, "identical_duplicates": identical_dups, "ticker_identical_duplicates": tdups, "rejected_tickers": summarize_rejects(trej),
        "gaps": {"instruments_without_ticker": sorted(s for s in accepted if s not in tickers),
                 "tickers_without_usdt_perp_instrument": sorted(s for s in tickers if s not in accepted and s not in {r["symbol"] for r in rejected}),
                 "funding_interval_instrument_vs_ticker_mismatch": interval_mismatch},
        "clock": {"skew_warn_ms": SKEW_WARN_MS, "pages_flagged": skew_pages, "tickers_flagged": bool(tpage["skew_flag"])},
        "note": "point-in-time, never filtered by performance; rejected rows are counted not hidden; a hash shows what was read, not who produced it",
    }
    _create_exclusive(os.path.join(out_dir, "manifest.json"), (json.dumps(manifest, indent=2, sort_keys=True, allow_nan=False) + "\n").encode("utf-8"))
    return manifest


# ---------------------------------------------------------------- schema v2: status partitions, reconciliation, verification

_SYMBOLID = re.compile(r"^[A-Za-z0-9_-]{1,40}\Z", re.ASCII)
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,120}\Z", re.ASCII)
_ACTIVE = ("Trading", "PendingOpen", "PreLaunch")      # statuses whose rows must carry the full rule set; others keep what the venue sends


def _vtime(t: Any) -> Optional[int]:
    if isinstance(t, int) and not isinstance(t, bool) and t > 0:
        return t
    if isinstance(t, str) and _UINT.fullmatch(t) and int(t) > 0:
        return int(t)
    return None


def _clean_msg(v: Any) -> str:
    return re.sub(r"[^A-Za-z0-9 _.,:;()'/-]", "?", str(v))[:60]


def _parse_page(body: bytes, category: str) -> Dict[str, Any]:
    """Pure. ``kind`` is ok | refused (retCode != 0) | malformed. Never raises."""
    try:
        doc = json.loads(body.decode("utf-8", "strict"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        return {"kind": "malformed", "why": "not UTF-8 JSON"}
    if not isinstance(doc, dict):
        return {"kind": "malformed", "why": "not an object"}
    rc = doc.get("retCode")
    if isinstance(rc, bool) or not isinstance(rc, int):
        return {"kind": "malformed", "why": "no integer retCode"}
    t = _vtime(doc.get("time"))
    if rc != 0:
        return {"kind": "refused", "ret_code": rc, "ret_msg": _clean_msg(doc.get("retMsg")), "time": t}
    res = doc.get("result")
    if not isinstance(res, dict) or res.get("category") != category or not isinstance(res.get("list"), list):
        return {"kind": "malformed", "why": "result.category/list is not the requested list", "time": t}
    nxt = res.get("nextPageCursor", "")
    nxt = "" if nxt is None else nxt
    if not isinstance(nxt, str) or (nxt and not _CURSOR.fullmatch(nxt)):
        return {"kind": "malformed", "why": "nextPageCursor is not a short printable token", "time": t}
    return {"kind": "ok", "ret_code": 0, "rows": res["list"], "next": nxt, "time": t}


def derive_partition(category: str, status: Optional[str], bodies: Sequence[bytes], *, transport_error: Optional[str] = None) -> Dict[str, Any]:
    """Pure: the outcome of ONE partition from its raw page bytes (the same function runs at capture and at verification).
    Returns outcome, complete, rows_total, usable_rows (rows that may enter the universe), pages (per page facts), pending (cursor still to fetch or ''),
    error, problems."""
    infos: List[Dict[str, Any]] = []
    rows: List[Any] = []
    outcome: Optional[str] = None
    err = ""
    problems: List[str] = []
    seen: set = set()
    firsts: set = set()
    prev = ""
    pending = ""
    exhausted = False
    for i, body in enumerate(bodies, 1):
        if exhausted:
            problems.append(f"page {i} follows the end of the cursor chain")
            break
        pg = _parse_page(body, category)
        if pg["kind"] != "ok":
            infos.append({"rows": 0, "cursor_out": "", "server_time_ms": pg.get("time"), "ret_code": pg.get("ret_code")})
            pending = ""
            if pg["kind"] == "refused":
                err = f"retCode {pg['ret_code']}: {pg['ret_msg']}"
                outcome = f"REFUSED_BY_VENUE:{pg['ret_code']}/{pg['ret_msg']}" if i == 1 else "PARTIAL"
            else:
                err = pg["why"]
                outcome = "TRANSPORT_ERROR:MalformedEnvelope" if i == 1 else "PARTIAL"
            break
        n = len(pg["rows"])
        nxt = pg["next"]
        infos.append({"rows": n, "cursor_out": nxt, "server_time_ms": pg["time"], "ret_code": 0})
        pending = ""
        if n > PAGE_LIMIT:
            outcome, err = "PARTIAL", f"page {i}: {n} rows exceeds the endpoint's page limit {PAGE_LIMIT}"
            break
        if n:
            fr = _row_sha(pg["rows"][0])
            if fr in firsts:
                outcome, err = "PARTIAL", f"page {i}: its first row equals an earlier page's first row (the cursor was not honoured)"
                break
            firsts.add(fr)
        rows.extend(pg["rows"])
        if category == "spot":
            if nxt:
                outcome, err = "PARTIAL", "spot returned a cursor; spot does not paginate and none is followed"
            exhausted = True
            continue
        if not nxt:
            exhausted = True
            continue
        if nxt == prev or nxt in seen:
            outcome, err = "PARTIAL", f"page {i}: the cursor did not advance (repeated cursor)"
            break
        if n == 0:
            outcome, err = "PARTIAL", f"page {i}: an empty page still names a next cursor"
            break
        seen.add(nxt)
        prev = nxt
        pending = nxt
    if outcome is None:
        if not bodies:
            outcome = f"TRANSPORT_ERROR:{transport_error or 'NoResponse'}"
            err = "no response was obtained"
        elif not exhausted:
            outcome, err = "PARTIAL", "the cursor chain was not exhausted (a later page was not obtained or the page cap was hit)"
    usable = rows
    if outcome is None:
        if status is not None:
            bad = sum(1 for r in rows if isinstance(r, dict) and r.get("status") != status)        # a non-object row is counted as rejected later, it does not void the partition
            if bad:
                outcome = f"REFUSED_BY_VENUE:status_param_not_honored({bad} rows with another status)"
                err = "the venue ignored or misapplied the status parameter"
                usable = []
        if outcome is None:
            outcome = "OBSERVED_ROWS" if rows else "OBSERVED_EMPTY"
    if outcome not in ("OBSERVED_ROWS", "PARTIAL"):
        usable = []
    ct: Dict[str, int] = {}
    for r in rows:
        k = _printable(r.get("contractType")) if isinstance(r, dict) and r.get("contractType") is not None else ("<none>" if isinstance(r, dict) else "<not_an_object>")
        ct[k] = ct.get(k, 0) + 1
    return {"outcome": outcome, "complete": outcome.startswith("OBSERVED_"), "rows_total": len(rows), "usable_rows": usable, "pages": infos,
            "by_contract_type": dict(sorted(ct.items())),
            "pending": pending, "error": err, "problems": problems}


def _opt_dec(row: Dict[str, Any], *path: str, positive: bool = False, signed: bool = False, required: bool = False) -> Optional[str]:
    return _dec(row, *path, positive=positive, signed=signed, required=required)


def _opt_uint(row: Dict[str, Any], key: str, required: bool) -> Optional[int]:
    if row.get(key) in (None, "") and not required:
        return None
    return _uint(row, key)


def _opt_bool(row: Dict[str, Any], key: str) -> Optional[bool]:
    v = row.get(key)
    if v is None:
        return None
    if not isinstance(v, bool):
        raise ValueError(f"{key} is not a boolean")
    return v


def _opt_str(row: Dict[str, Any], key: str, limit: int = 32) -> Optional[str]:
    v = row.get(key)
    if v in (None, ""):
        return None
    if not isinstance(v, str) or len(v) > limit or not v.isascii() or not v.isprintable():
        raise ValueError(f"{key} is not a short printable string")
    return v


def _filter_copy(row: Dict[str, Any], key: str) -> Optional[Dict[str, str]]:
    v = row.get(key)
    if v is None:
        return None
    if not isinstance(v, dict) or len(v) > 16:
        raise ValueError(f"{key} is not a small object")
    out: Dict[str, str] = {}
    for k, x in v.items():
        if not isinstance(k, str) or not _NAME.fullmatch(k) or not isinstance(x, str) or len(x) > 40 or not _SDEC.fullmatch(x):
            raise ValueError(f"{key}.{str(k)[:20]} is not a decimal string")
        out[k] = x
    return out


def normalize_instrument_v2(row: Any, category: str) -> Tuple[Optional[Dict[str, Any]], Optional[str], str]:
    """(record, None, symbol) for an accepted instrument of ``category``, else (None, reason, symbol). Every rule field the venue documents is kept as its own
    string (``fundingInterval`` in MINUTES, never assumed). linear: USDT perpetuals; inverse: perpetuals quoted in USD; spot: any listed pair."""
    if not isinstance(row, dict):
        return None, "not_an_object", ""
    sym = row.get("symbol")
    if not isinstance(sym, str) or not _SYMBOL.fullmatch(sym):
        return None, "bad_symbol", _printable(sym)
    if category == "linear":
        if row.get("contractType") != PERP_TYPE:
            return None, f"not_perpetual:{_printable(row.get('contractType'))}", sym
        if row.get("quoteCoin") != QUOTE or row.get("settleCoin") != QUOTE:
            return None, f"not_usdt:{_printable(row.get('quoteCoin'))}/{_printable(row.get('settleCoin'))}", sym
    elif category == "inverse":
        if row.get("contractType") != "InversePerpetual":
            return None, f"not_perpetual:{_printable(row.get('contractType'))}", sym
        if row.get("quoteCoin") != "USD":
            return None, f"not_usd:{_printable(row.get('quoteCoin'))}", sym
    try:
        status = row.get("status")
        if not isinstance(status, str) or not status or not status.isascii() or not status.isprintable() or len(status) > 24:
            raise ValueError("status")
        sid = row.get("symbolId")
        if isinstance(sid, bool) or not (sid is None or sid == "" or isinstance(sid, (str, int))):
            raise ValueError("symbolId")
        sid = None if sid in (None, "") else str(sid)
        if sid is not None and not _SYMBOLID.fullmatch(sid):
            raise ValueError("symbolId")
        if category == "spot":
            for k in ("baseCoin", "quoteCoin"):
                if not isinstance(row.get(k), str) or not row[k].isascii() or not 0 < len(row[k]) <= 20:
                    raise ValueError(k)
            return {"schema": ROW_SCHEMA_V2, "category": category, "symbol": sym, "symbolId": sid, "status": status, "baseCoin": row["baseCoin"],
                    "quoteCoin": row["quoteCoin"], "priceFilter": _filter_copy(row, "priceFilter"), "lotSizeFilter": _filter_copy(row, "lotSizeFilter")}, None, sym
        strict = status in _ACTIVE
        interval = _opt_uint(row, "fundingInterval", strict)
        if interval is not None and (interval > MAX_FUNDING_INTERVAL_MIN or (strict and interval <= 0)):
            raise ValueError(f"fundingInterval {interval} min is not sane")
        upper = _opt_dec(row, "upperFundingRate", signed=True, required=strict)
        lower = _opt_dec(row, "lowerFundingRate", signed=True, required=strict)
        if strict and not (float(lower) < 0 < float(upper) and float(upper) <= 0.5 and float(lower) >= -0.5):
            raise ValueError("funding bounds are not a sane lower<0<upper fraction")
        info = row.get("preListingInfo")
        if info is not None and (not isinstance(info, dict) or len(json.dumps(info, default=str)) > 4000):
            raise ValueError("preListingInfo")
        rec = {
            "schema": ROW_SCHEMA_V2, "category": category, "symbol": sym, "symbolId": sid, "contractType": row.get("contractType"), "status": status,
            "baseCoin": _opt_str(row, "baseCoin", 20) or "", "quoteCoin": row.get("quoteCoin"), "settleCoin": _opt_str(row, "settleCoin", 20) or "",
            "symbolType": _opt_str(row, "symbolType") or "",
            "launchTime": _opt_uint(row, "launchTime", strict), "deliveryTime": _opt_uint(row, "deliveryTime", strict),
            "deliveryFeeRate": _opt_dec(row, "deliveryFeeRate", signed=True), "fundingInterval_min": interval,
            "upperFundingRate": upper, "lowerFundingRate": lower, "priceScale": _opt_uint(row, "priceScale", False),
            "minLeverage": _opt_dec(row, "leverageFilter", "minLeverage", positive=True), "maxLeverage": _opt_dec(row, "leverageFilter", "maxLeverage", positive=True),
            "leverageStep": _opt_dec(row, "leverageFilter", "leverageStep", positive=True),
            "tickSize": _opt_dec(row, "priceFilter", "tickSize", positive=True, required=strict), "minPrice": _opt_dec(row, "priceFilter", "minPrice"),
            "maxPrice": _opt_dec(row, "priceFilter", "maxPrice"),
            "minOrderQty": _opt_dec(row, "lotSizeFilter", "minOrderQty", positive=True, required=strict), "maxOrderQty": _opt_dec(row, "lotSizeFilter", "maxOrderQty"),
            "maxMktOrderQty": _opt_dec(row, "lotSizeFilter", "maxMktOrderQty"), "postOnlyMaxOrderQty": _opt_dec(row, "lotSizeFilter", "postOnlyMaxOrderQty"),
            "qtyStep": _opt_dec(row, "lotSizeFilter", "qtyStep", positive=True, required=strict), "minNotionalValue": _opt_dec(row, "lotSizeFilter", "minNotionalValue"),
            "isPreListing": _opt_bool(row, "isPreListing"), "preListingInfo": info,
            "priceLimitRatioX": _opt_dec(row, "riskParameters", "priceLimitRatioX"), "priceLimitRatioY": _opt_dec(row, "riskParameters", "priceLimitRatioY"),
            "unifiedMarginTrade": _opt_bool(row, "unifiedMarginTrade"), "copyTrading": _opt_str(row, "copyTrading"),
        }
    except ValueError as exc:
        return None, f"invalid_field:{exc}"[:80], sym
    return rec, None, sym


def _ckey(rec: Dict[str, Any]) -> str:
    return json.dumps(rec, sort_keys=True, allow_nan=False, separators=(",", ":"))


_ENDED_STATUSES = ("Closed", "Delivering")


def _rename_chain(by_sym: Dict[str, List[Dict[str, Any]]]) -> Optional[List[Tuple[str, str]]]:
    """The (old, new) pairs when the symbols sharing ONE symbolId are successive names of one instrument, else None (a conflict). Ordered by launchTime, every earlier name
    must have ENDED (every record Closed/Delivering, or a deliveryTime at or before the next name's launchTime) and the two names must not carry the same set of statuses
    unless the later one launched at or after the earlier one's delivery. Two Trading rows with one symbolId never qualify."""
    info = []
    for sym, recs in by_sym.items():
        launches = [r.get("launchTime") or 0 for r in recs]
        deliveries = [r.get("deliveryTime") or 0 for r in recs]
        info.append((min(launches), sym, frozenset(r.get("status") for r in recs), max(deliveries)))
    info.sort(key=lambda x: (x[0], x[1]))
    pairs: List[Tuple[str, str]] = []
    for (la, sa, sta, da), (lb, sb, stb, _db) in zip(info, info[1:]):
        launched_after_delivery = da > 0 and lb >= da
        ended = bool(sta) and all(x in _ENDED_STATUSES for x in sta)
        if not (ended or launched_after_delivery) or (sta == stb and not launched_after_delivery):
            return None
        pairs.append((sa, sb))
    return pairs


def build_universe(parts: Sequence[Dict[str, Any]], tickers: Dict[str, Dict[str, Any]], snapshot_ms: int) -> Dict[str, Any]:
    """Pure. Normalise every usable row of every partition, reconcile across partitions, write nothing. Identical records seen in several partitions are one
    line with every partition in ``seen_in``. Two DIFFERENT records for one (category, symbol), or one symbolId on two Trading (or otherwise un-ended)
    symbols, are never merged: every variant stays a line flagged ``conflict`` and the keys are returned in ``conflicts``. One symbolId on successive names where the earlier
    one has ENDED (Closed/Delivering, or delivered before the next launched) is a RENAME: recorded in ``renames`` as lineage, not a conflict."""
    acc: Dict[Tuple[str, str], Dict[str, Dict[str, Any]]] = {}
    rej: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for p in parts:
        label = f"{p['category']}:{p['status'] or 'default'}"
        for r in p["usable_rows"]:
            rec, why, sym = normalize_instrument_v2(r, p["category"])
            if rec is None:
                e = rej.setdefault((p["category"], _row_sha(r)), dict(_reject_entry(r, why or "", sym), category=p["category"], partitions=[]))
                if label not in e["partitions"]:
                    e["partitions"].append(label)
                continue
            v = acc.setdefault((p["category"], sym), {}).setdefault(_ckey(rec), {"rec": rec, "seen": []})
            if label not in v["seen"]:
                v["seen"].append(label)
    conflicts: List[Dict[str, Any]] = []
    ids: Dict[Tuple[str, str], Dict[str, List[Dict[str, Any]]]] = {}
    lines: List[bytes] = []
    for key in sorted(acc):
        variants = acc[key]
        multi = len(variants) > 1
        if multi:
            recs = [variants[k]["rec"] for k in sorted(variants)]
            fields = sorted({f for r in recs for f in r if any(x.get(f) != r.get(f) for x in recs)})
            conflicts.append({"kind": "symbol_variants", "category": key[0], "symbol": key[1], "fields": fields,
                              "symbolIds": sorted({r["symbolId"] for r in recs if r.get("symbolId")}),
                              "variants": [{"status": variants[k]["rec"].get("status"), "seen_in": sorted(variants[k]["seen"])} for k in sorted(variants)]})
        for ck in sorted(variants):
            rec = variants[ck]["rec"]
            if rec.get("symbolId"):
                ids.setdefault((key[0], rec["symbolId"]), {}).setdefault(key[1], []).append(rec)
            row = dict(rec, seen_in=sorted(variants[ck]["seen"]), snapshot_ms=snapshot_ms, conflict=multi, ticker=tickers.get(key[1]) if key[0] == "linear" else None)
            lines.append((json.dumps(row, sort_keys=True, allow_nan=False, separators=(",", ":")) + "\n").encode("utf-8"))
    renames: List[Dict[str, Any]] = []
    for (cat, sid), by_sym in sorted(ids.items()):
        if len(by_sym) > 1:
            pairs = _rename_chain(by_sym)
            if pairs is None:
                conflicts.append({"kind": "symbolId_multiple_symbols", "category": cat, "symbolId": sid, "symbols": sorted(by_sym)})
            else:
                renames.extend({"category": cat, "symbolId": sid, "from": a, "to": b} for a, b in pairs)       # one instrument under successive names: lineage, not a conflict
    for e in rej.values():
        e["partitions"].sort()
    rejected = sorted(rej.values(), key=lambda e: (e["category"], e["symbol"], e["row_sha256"]))
    excl: Dict[str, int] = {}
    for e in rejected:
        if e["reason"].startswith("not_perpetual:"):
            k = f"{e['category']}:{e['reason'].split(':', 1)[1]}"
            excl[k] = excl.get(k, 0) + 1
    all_syms = sorted({(r.get("symbol")) for p in parts for r in p["usable_rows"] if isinstance(r, dict) and isinstance(r.get("symbol"), str)})
    return {"jsonl": b"".join(lines), "rows": len(lines), "rejected": summarize_rejects(rejected), "conflicts": conflicts, "renames": renames,
            "instruments": sum(1 for _ in acc), "excluded_contract_types": dict(sorted(excl.items())), "instrument_symbols": all_syms,
            "linear_accepted_symbols": sorted(k[1] for k in acc if k[0] == "linear")}


def reconcile_tickers(tick: Optional[Dict[str, Any]], uni: Dict[str, Any], rejected_ticker_symbols: Sequence[str]) -> Optional[Dict[str, Any]]:
    """Ticker vs instrument reconciliation. ORPHAN = an accepted ticker symbol that no partition's instrument rows contain."""
    if tick is None:
        return None
    inst = set(uni["instrument_symbols"])
    tsyms = set(tick["map"])
    orphans = sorted(tsyms - inst)
    no_tick = sorted(set(uni["linear_accepted_symbols"]) - tsyms)
    rej_with = sum(1 for x in rejected_ticker_symbols if x in inst)
    return {"tickers_accepted": len(tsyms), "tickers_rejected_by_reason": tick["rejected"]["by_reason"], "tickers_rejected": tick["rejected"]["count"],
            "rejected_tickers_with_instrument": rej_with, "rejected_tickers_without_instrument": len(rejected_ticker_symbols) - rej_with,
            "orphan_count": len(orphans), "orphans": orphans[:MAX_REJECT_LIST], "orphans_capped": len(orphans) > MAX_REJECT_LIST,
            "instruments_without_ticker_count": len(no_tick), "instruments_without_ticker": no_tick[:MAX_REJECT_LIST], "instruments_without_ticker_capped": len(no_tick) > MAX_REJECT_LIST}


def _coverage(parts: Sequence[Dict[str, Any]]) -> Tuple[Dict[str, str], Dict[str, Dict[str, str]], str]:
    by_cat: Dict[str, Dict[str, str]] = {}
    for p in parts:
        by_cat.setdefault(p["category"], {})[p["status"] or "default"] = p["outcome"]
    closed = by_cat.get("linear", {}).get("Closed", "")
    deli = "OBSERVED" if closed in ("OBSERVED_ROWS", "OBSERVED_EMPTY") else "NOT_OBSERVED"
    return dict(by_cat.get("linear", {})), by_cat, deli


def describe_delisting_observability(observability: Any, closed_outcome: Any) -> str:
    """Plain words for what the Closed partition did and did NOT show. ``dataset_status`` and ``delisting_observability`` are separate facts: a capture whose Closed
    partition was refused is still COMPLETE at capture level, and its universe is survivors only. An EMPTY Closed partition means 'no Closed rows served'; it does not
    prove that no delistings happened."""
    if observability == "OBSERVED" and closed_outcome == "OBSERVED_ROWS":
        return ("Closed rows were served: delisted contracts the venue still serves are visible. The venue may not serve old delistings, so an absent symbol is still not proof "
                "it was never delisted")
    if observability == "OBSERVED" and closed_outcome == "OBSERVED_EMPTY":
        return "no Closed rows served (the Closed partition answered but was empty); this does not prove no delistings happened, so the universe is survivors only"
    return (f"delistings NOT observed (Closed partition outcome {closed_outcome!r}, delisting_observability {observability!r}): delisted contracts are invisible, "
            "the universe is survivors only")


def _dataset_status(parts: Sequence[Dict[str, Any]], conflicts: Sequence[Any], timing: Sequence[Tuple[Optional[int], Optional[int]]]) -> str:
    """CONFLICTED > PARTIAL > STALE > UNVERIFIED > COMPLETE. ``timing`` is (recv_ms, venue time ms) per response."""
    if conflicts:
        return "CONFLICTED"
    have = {(p["category"], p["status"] or "default"): p for p in parts}
    cats = sorted({p["category"] for p in parts})
    for c in cats:
        for need in (("default",) if c == "spot" else ("default", "Trading")):
            q = have.get((c, need))
            if q is None or not q["complete"]:
                return "PARTIAL"
    if any(p["outcome"].startswith(("PARTIAL", "TRANSPORT_ERROR")) for p in parts):
        return "PARTIAL"
    recvs = [r for r, _v in timing if isinstance(r, int)]
    if recvs and max(recvs) - min(recvs) > MAX_SPAN_MS:
        return "STALE"
    if any(isinstance(r, int) and isinstance(v, int) and abs(r - v) > SKEW_WARN_MS for r, v in timing):
        return "STALE"
    if any(v is None for _r, v in timing):
        return "UNVERIFIED"
    return "COMPLETE"


def _derive_all(parts_in: Sequence[Dict[str, Any]], tbody: Optional[bytes], snapshot_ms: int) -> Dict[str, Any]:
    parts = []
    for p in parts_in:
        d = derive_partition(p["category"], p["status"], p["bodies"], transport_error=p.get("transport_error"))
        d.update(category=p["category"], status=p["status"])
        parts.append(d)
    tick: Optional[Dict[str, Any]] = None
    if tbody is not None:
        tp = _parse_page(tbody, "linear")
        if tp["kind"] != "ok":
            raise CaptureError(f"tickers: not a successful linear envelope ({tp.get('why') or tp.get('ret_msg')})")
        if tp["next"]:
            raise CaptureError("tickers: the venue named a cursor on a snapshot that must be complete")
        acc, dups, rej = classify_tickers(tp["rows"], "tickers")
        tick = {"rows": len(tp["rows"]), "accepted": len(acc), "rejected": summarize_rejects(rej), "dups": dups, "server_time_ms": tp["time"], "map": acc,
                "rej_symbols": [e["symbol"] for e in rej]}
    uni = build_universe(parts, tick["map"] if tick else {}, snapshot_ms)
    cov, cov_cat, deli = _coverage(parts)
    return {"parts": parts, "tickers": tick, "universe": uni, "reconciliation": reconcile_tickers(tick, uni, tick["rej_symbols"] if tick else []),
            "status_coverage": cov, "status_coverage_by_category": cov_cat, "delisting_observability": deli}


def _get(url: str, fetch: Callable[[str], bytes], sleep: Callable[[float], None], retries: int, backoff_s: float) -> Tuple[Optional[bytes], int, str]:
    """One polite GET with retry/backoff on HTTP 429 and retCode 10006 only. (body or None, attempts, failure class)."""
    attempts = 0
    for attempt in range(retries + 1):
        attempts += 1
        body: Any = None
        try:
            body = fetch(url)
        except HttpStatusError as exc:
            if not (exc.body and _looks_like_envelope(exc.body)):
                if exc.status == 429 and attempt < retries:
                    sleep(min(backoff_s * 2 ** attempt, MAX_BACKOFF_S))
                    continue
                return None, attempts, exc.cls
            body = exc.body                                      # the venue answered with a V5 body: the parse decides what it means
        except (TransportFailure, OSError) as exc:
            return None, attempts, getattr(exc, "cls", type(exc).__name__)
        if not isinstance(body, (bytes, bytearray)):
            return None, attempts, "NonBytesResponse"
        body = bytes(body)
        if len(body) > MAX_BODY_BYTES:
            raise CaptureError("response larger than any universe page")
        if _ret_code(body) in RATE_LIMIT_CODES and attempt < retries:
            sleep(min(backoff_s * 2 ** attempt, MAX_BACKOFF_S))
            continue
        return body, attempts, ""
    return None, attempts, "RetriesExhausted"


def _ret_code(body: bytes) -> Optional[int]:
    try:
        d = json.loads(body.decode("utf-8", "strict"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        return None
    rc = d.get("retCode") if isinstance(d, dict) else None
    return rc if isinstance(rc, int) and not isinstance(rc, bool) else None


def _looks_like_envelope(body: bytes) -> bool:
    return _ret_code(body) is not None


def _params(category: str, status: Optional[str]) -> Dict[str, Any]:
    p: Dict[str, Any] = {"category": category}
    if category != "spot":
        p["limit"] = PAGE_LIMIT
    if status is not None:
        p["status"] = status
    return p


def _pname(category: str, status: Optional[str], n: int) -> str:
    return f"{category}_{status or 'default'}_page{n:04d}.json"


def capture_universe(out_dir: str, *, fetch: Callable[[str], bytes] = http_fetch, clock_ms: Callable[[], int] = lambda: int(time.time() * 1000),
                     sleep: Callable[[float], None] = time.sleep, categories: Sequence[str] = ("linear",), statuses: Sequence[str] = STATUSES,
                     max_pages: int = MAX_PAGES, retries: int = RETRIES, backoff_s: float = BACKOFF_S, pause_s: float = PAUSE_S) -> Dict[str, Any]:
    """Schema-v2 capture: for each category a default control partition plus one partition per status, each cursor-paginated. What the venue answers for
    each partition is recorded. A refusal or a broken chain is an OUTCOME (and a non-COMPLETE dataset_status), never a reason to lose the run."""
    cats = list(dict.fromkeys(categories))
    sts = list(dict.fromkeys(statuses))
    if not cats or any(c not in CATEGORIES for c in cats) or any(s not in STATUSES for s in sts):
        raise CaptureError("categories must be among linear,inverse,spot and statuses among the documented enum")
    parent = os.path.dirname(os.path.abspath(out_dir))
    if os.path.islink(out_dir) or os.path.islink(parent) or not os.path.isdir(parent):
        raise CaptureError("the output directory must be a real directory, not a link")
    AR._refuse_protected(os.path.join(out_dir, "manifest.json"))
    os.makedirs(out_dir, mode=0o700, exist_ok=True)
    if os.listdir(out_dir):
        raise CaptureError("the output directory is not empty: each run goes in a NEW directory, nothing is overwritten")
    started = int(clock_ms())
    parts_in: List[Dict[str, Any]] = []
    claims: List[Dict[str, Any]] = []
    first = True
    for cat in cats:
        for st in [None] + ([] if cat == "spot" else sts):
            bodies: List[bytes] = []
            metas: List[Dict[str, Any]] = []
            terr = ""
            cursor = ""
            for n in range(1, (1 if cat == "spot" else max_pages) + 1):
                if not first:
                    sleep(pause_s)
                first = False
                body, attempts, failure = _get(request_url(PATH_INSTRUMENTS, cursor or None, category=cat, status=st), fetch, sleep, retries, backoff_s)
                if body is None:
                    terr = failure
                    break
                recv = int(clock_ms())
                fname = _pname(cat, st, n)
                _create_exclusive(os.path.join(out_dir, fname), body)        # unedited, before anything is concluded about it
                bodies.append(body)
                metas.append({"n": n, "file": fname, "sha256": _sha(body), "bytes": len(body), "cursor_in": cursor, "recv_ms": recv, "attempts": attempts})
                d = derive_partition(cat, st, bodies)
                if not d["pending"]:
                    break
                cursor = d["pending"]
            parts_in.append({"category": cat, "status": st, "bodies": bodies, "transport_error": terr})
            claims.append({"category": cat, "status": st, "metas": metas, "transport_error": terr})
    tbody: Optional[bytes] = None
    tmeta: Optional[Dict[str, Any]] = None
    if "linear" in cats:
        sleep(pause_s)
        tbody, _att, failure = _get(request_url(PATH_TICKERS, category="linear"), fetch, sleep, retries, backoff_s)
        if tbody is None:
            raise CaptureError(f"tickers: no response ({failure}); the capture is abandoned (raw pages kept, no manifest)")
        trecv = int(clock_ms())
        _create_exclusive(os.path.join(out_dir, "tickers.json"), tbody)
        tmeta = {"recv_ms": trecv}
    derived = _derive_all(parts_in, tbody, started)
    uni = derived["universe"]
    ufile = f"universe-{started}.jsonl"
    _create_exclusive(os.path.join(out_dir, ufile), uni["jsonl"])
    partitions: List[Dict[str, Any]] = []
    timing: List[Tuple[Optional[int], Optional[int]]] = []
    for c, d in zip(claims, derived["parts"]):
        pages = []
        for m, info in zip(c["metas"], d["pages"]):
            pages.append({**m, "cursor_out": info["cursor_out"], "rows": info["rows"], "server_time_ms": info["server_time_ms"], "ret_code": info["ret_code"],
                          "params": _params(c["category"], c["status"]), **{k: v for k, v in _skew(m["recv_ms"], info["server_time_ms"]).items() if k.startswith("skew")}})
            timing.append((m["recv_ms"], info["server_time_ms"]))
        partitions.append({"category": c["category"], "status": c["status"], "params": _params(c["category"], c["status"]), "pages": pages,
                           "rows_total": d["rows_total"], "by_contract_type": d["by_contract_type"], "outcome": d["outcome"], "complete": d["complete"],
                           "error": d["error"] or None})
    tk = derived["tickers"]
    tickers_claim = None
    if tk is not None and tmeta is not None:
        tickers_claim = {"file": "tickers.json", "sha256": _sha(tbody or b""), "bytes": len(tbody or b""), "rows": tk["rows"], "accepted": tk["accepted"],
                         "rejected_count": tk["rejected"]["count"], "server_time_ms": tk["server_time_ms"], "recv_ms": tmeta["recv_ms"],
                         **{k: v for k, v in _skew(tmeta["recv_ms"], tk["server_time_ms"]).items() if k.startswith("skew")}}
        timing.append((tmeta["recv_ms"], tk["server_time_ms"]))
    manifest = {
        "schema": SCHEMA_V2, "schema_version": 2, "capture_id": f"uc2-{started}", "started_ms": started, "finished_ms": int(clock_ms()),
        "source_host": venues.host_of(_base()), "categories": cats, "partitions": partitions, "tickers": tickers_claim,
        "universe_file": {"file": ufile, "sha256": _sha(uni["jsonl"]), "rows": uni["rows"]},
        "rejected_instruments": uni["rejected"], "rejected_tickers": tk["rejected"] if tk else None, "conflicts": uni["conflicts"], "renames": uni["renames"],
        "excluded_contract_types": uni["excluded_contract_types"], "ticker_reconciliation": derived["reconciliation"],
        "status_coverage": derived["status_coverage"], "status_coverage_by_category": derived["status_coverage_by_category"],
        "delisting_observability": derived["delisting_observability"], "coverage_note": COVERAGE_NOTE,
        "dataset_status": _dataset_status(derived["parts"], uni["conflicts"], timing),
    }
    _create_exclusive(os.path.join(out_dir, "manifest.json"), (json.dumps(manifest, indent=2, sort_keys=True, allow_nan=False) + "\n").encode("utf-8"))
    return manifest


# ---------------------------------------------------------------- verification (everything re-derived from the raw bytes)

def verify_capture_dir(path: str) -> Dict[str, Any]:
    """Strict re-verification of a schema-v2 capture directory. Re-reads and re-hashes EVERY file against the manifest, re-parses every raw page, and
    re-derives row counts, partition outcomes, status coverage, delisting observability, rejected lists, conflicts, the universe jsonl and the dataset
    status from the bytes, never trusting a manifest assertion. Returns
    ``{"ok": bool, "problems": [str], "coverage": {...}, "manifest_sha256": str|None}``. ``ok`` means INTEGRITY only (nothing missing, extra, modified or
    unsupported); a COMPLETE-looking claim is judged in ``coverage`` (re-derived). When ok is False, ``coverage.dataset_status`` is UNVERIFIED and
    ``coverage.delisting_observability`` is NOT_OBSERVED. Never raises on bad input."""
    problems: List[str] = []
    out: Dict[str, Any] = {"ok": False, "problems": problems, "coverage": {}, "manifest_sha256": None}
    try:
        _verify(path, problems, out)
    except (OSError, AR.RecordError, CaptureError, KeyError, TypeError, ValueError, AttributeError, IndexError, RecursionError) as exc:
        problems.append(f"verification aborted: {type(exc).__name__}: {str(exc)[:120]}")
    if problems:
        cov = out["coverage"]
        cov["dataset_status"] = "UNVERIFIED"
        cov["delisting_observability"] = "NOT_OBSERVED"
    out["ok"] = not problems and bool(out["coverage"])
    return out


def _verify(path: str, problems: List[str], out: Dict[str, Any]) -> None:
    real = os.path.realpath(path)
    AR._refuse_protected(os.path.join(real, "manifest.json"))          # the path rules apply to the directory's location and to every file read
    if os.path.islink(path) or not os.path.isdir(real):
        problems.append("not a real directory")
        return
    listing = sorted(os.listdir(real))
    for name in listing:
        st = os.lstat(os.path.join(real, name))
        if not stat.S_ISREG(st.st_mode):
            problems.append(f"{name}: not a regular file")
    referenced: Dict[str, str] = {}

    def read(name: Any, what: str) -> Optional[bytes]:
        if not isinstance(name, str) or not _NAME.fullmatch(name):
            problems.append(f"{what}: unsafe file name")
            return None
        if name in referenced:
            problems.append(f"{name}: referenced twice ({referenced[name]} and {what})")
            return None
        referenced[name] = what
        if name not in listing:
            problems.append(f"{name}: missing ({what})")
            return None
        fp = os.path.join(real, name)
        if not stat.S_ISREG(os.lstat(fp).st_mode):
            return None
        AR._refuse_protected(fp)
        with open(fp, "rb") as fh:
            return fh.read()

    mraw = read("manifest.json", "manifest")
    if mraw is None:
        return
    out["manifest_sha256"] = _sha(mraw)
    referenced["manifest.json"] = "manifest"
    m = json.loads(mraw.decode("utf-8", "strict"))
    if not isinstance(m, dict) or m.get("schema") != SCHEMA_V2:
        problems.append("manifest is not universe_capture/v2")
        return
    started = m["started_ms"]
    if not isinstance(started, int) or isinstance(started, bool):
        problems.append("started_ms is not an integer")
        return
    cats = m["categories"]
    if not isinstance(cats, list) or not cats or any(c not in CATEGORIES for c in cats):
        problems.append("categories is not a list of linear/inverse/spot")
        return
    parts_in: List[Dict[str, Any]] = []
    timing: List[Tuple[Optional[int], Optional[int]]] = []
    page_claims: List[List[Dict[str, Any]]] = []
    seen_keys = set()
    for pc in m["partitions"]:
        cat, st = pc["category"], pc["status"]
        if cat not in cats or (st is not None and st not in STATUSES) or (cat == "spot" and st is not None):
            problems.append(f"partition {cat}/{st}: not a documented category/status")
            continue
        if (cat, st) in seen_keys:
            problems.append(f"partition {cat}/{st}: listed twice")
            continue
        seen_keys.add((cat, st))
        if pc.get("params") != _params(cat, st):
            problems.append(f"partition {cat}/{st}: params are not the pinned request parameters")
        bodies: List[bytes] = []
        claims = pc["pages"]
        for i, pg in enumerate(claims, 1):
            what = f"{cat}/{st or 'default'} page {i}"
            if pg.get("n") != i:
                problems.append(f"{what}: n is {pg.get('n')!r}")
            if pg.get("file") != _pname(cat, st, i):
                problems.append(f"{what}: file name {pg.get('file')!r} is not the pinned name")
            raw = read(pg.get("file"), what)
            if raw is None:
                bodies = []
                break
            if _sha(raw) != pg.get("sha256") or len(raw) != pg.get("bytes"):
                problems.append(f"{pg['file']}: modified (sha256/bytes differ from the manifest)")
            bodies.append(raw)
        claimed = pc["outcome"]
        terr = claimed.split(":", 1)[1] if isinstance(claimed, str) and claimed.startswith("TRANSPORT_ERROR:") and not bodies and not claims else None
        d = derive_partition(cat, st, bodies, transport_error=terr)
        for pr in d["problems"]:
            problems.append(f"{cat}/{st or 'default'}: {pr}")
        d.update(category=cat, status=st)
        if d["outcome"] != claimed:
            problems.append(f"{cat}/{st or 'default'}: manifest says {claimed!r} but the raw pages give {d['outcome']!r}")
        if pc.get("complete") != d["complete"]:
            problems.append(f"{cat}/{st or 'default'}: complete flag is not supported by the raw pages")
        if pc.get("by_contract_type") != d["by_contract_type"]:
            problems.append(f"{cat}/{st or 'default'}: by_contract_type is not what the raw pages contain")
        if pc.get("rows_total") != d["rows_total"]:
            problems.append(f"{cat}/{st or 'default'}: rows_total {pc.get('rows_total')!r} != re-counted {d['rows_total']}")
        prev_out = ""
        for i, (pg, info) in enumerate(zip(claims, d["pages"]), 1):
            if pg.get("cursor_in") != prev_out:
                problems.append(f"{cat}/{st or 'default'} page {i}: cursor_in {pg.get('cursor_in')!r} does not continue the chain (previous page ends {prev_out!r})")
            for k, v in (("cursor_out", info["cursor_out"]), ("rows", info["rows"]), ("server_time_ms", info["server_time_ms"]), ("ret_code", info["ret_code"])):
                if pg.get(k) != v:
                    problems.append(f"{cat}/{st or 'default'} page {i}: {k} {pg.get(k)!r} != re-derived {v!r}")
            prev_out = info["cursor_out"]
            timing.append((pg.get("recv_ms") if isinstance(pg.get("recv_ms"), int) else None, info["server_time_ms"]))
        if len(d["pages"]) != len(claims) and bodies:
            problems.append(f"{cat}/{st or 'default'}: {len(claims)} pages claimed, {len(d['pages'])} usable")
        parts_in.append(d)
        page_claims.append(claims)
    for c in cats:
        if (c, None) not in seen_keys:
            problems.append(f"{c}: the default control partition is missing")
    if {c for c, _s in seen_keys} != set(cats):
        problems.append("categories does not match the partitions")
    tbody: Optional[bytes] = None
    tc = m["tickers"]
    if tc is None:
        if "linear" in cats:
            problems.append("tickers: missing although linear was captured")
    else:
        tbody = read(tc.get("file"), "tickers")
        if tbody is None:
            pass
        elif _sha(tbody) != tc.get("sha256") or len(tbody) != tc.get("bytes"):
            problems.append("tickers.json: modified (sha256/bytes differ from the manifest)")
    snapshot = started
    derived_parts = parts_in
    tick: Optional[Dict[str, Any]] = None
    if tbody is not None:
        tp = _parse_page(tbody, "linear")
        if tp["kind"] != "ok" or tp["next"]:
            problems.append("tickers.json: not a successful complete linear envelope")
        else:
            acc, dups, rej = classify_tickers(tp["rows"], "tickers")
            tick = {"map": acc, "rejected": summarize_rejects(rej), "rej_symbols": [e["symbol"] for e in rej]}
            for k, v in (("rows", len(tp["rows"])), ("accepted", len(acc)), ("rejected_count", len(rej)), ("server_time_ms", tp["time"])):
                if tc.get(k) != v:
                    problems.append(f"tickers: {k} {tc.get(k)!r} != re-derived {v!r}")
            if m["rejected_tickers"] != tick["rejected"]:
                problems.append("rejected_tickers differs from the re-derivation of tickers.json")
            timing.append((tc.get("recv_ms") if isinstance(tc.get("recv_ms"), int) else None, tp["time"]))
    uni = build_universe(derived_parts, tick["map"] if tick else {}, snapshot)
    uf = m["universe_file"]
    if uf.get("file") != f"universe-{snapshot}.jsonl":
        problems.append("universe file name does not match started_ms")
    ub = read(uf.get("file"), "universe")
    if ub is not None:
        if _sha(ub) != uf.get("sha256"):
            problems.append(f"{uf.get('file')}: modified (sha256 differs from the manifest)")
        if ub != uni["jsonl"]:
            problems.append(f"{uf.get('file')}: differs from the universe re-derived from the raw pages")
    if uf.get("rows") != uni["rows"]:
        problems.append(f"universe rows {uf.get('rows')!r} != re-derived {uni['rows']}")
    if m["rejected_instruments"] != uni["rejected"]:
        problems.append("rejected_instruments differs from the re-derivation of the raw pages")
    rec = reconcile_tickers(tick, uni, tick["rej_symbols"] if tick else [])
    if m["ticker_reconciliation"] != rec:
        problems.append("ticker_reconciliation differs from the re-derivation of the raw pages")
    if m["excluded_contract_types"] != uni["excluded_contract_types"]:
        problems.append("excluded_contract_types differs from the re-derivation of the raw pages")
    if m["conflicts"] != uni["conflicts"]:
        problems.append("conflicts differ from the re-derivation of the raw pages")
    if m.get("renames", []) != uni["renames"]:
        problems.append("renames differ from the re-derivation of the raw pages")
    cov, cov_cat, deli = _coverage(derived_parts)
    for key, val in (("status_coverage", cov), ("status_coverage_by_category", cov_cat), ("delisting_observability", deli)):
        if m[key] != val:
            problems.append(f"{key} claim is not supported by the raw pages (claimed {m[key]!r}, derived {val!r})")
    ds = _dataset_status(derived_parts, uni["conflicts"], timing)
    if m["dataset_status"] != ds:
        problems.append(f"dataset_status claim {m['dataset_status']!r} != re-derived {ds!r}")
    if not isinstance(m.get("coverage_note"), str) or "NOT point-in-time" not in m["coverage_note"]:
        problems.append("coverage_note is missing or does not carry the point-in-time disclaimer")
    for name in listing:
        if name not in referenced:
            problems.append(f"{name}: extra file not named by the manifest")
    out["coverage"] = {
        "categories": cats, "status_coverage": cov, "status_coverage_by_category": cov_cat, "delisting_observability": deli, "dataset_status": ds,
        "dataset_status_claimed": m["dataset_status"],
        "partitions": [{"category": p["category"], "status": p["status"], "outcome": p["outcome"], "rows_total": p["rows_total"], "complete": p["complete"],
                        "pages": len(p["pages"]), "by_contract_type": p["by_contract_type"]} for p in derived_parts],
        "universe_rows": uni["rows"], "rejected_instruments_count": uni["rejected"]["count"],
        "rejected_tickers_count": tick["rejected"]["count"] if tick else None, "conflicts": uni["conflicts"], "renames": uni["renames"], "ticker_reconciliation": rec,
        "excluded_contract_types": uni["excluded_contract_types"], "manifest_sha256": out["manifest_sha256"],
    }


# ---------------------------------------------------------------- diff

def _row_key(r: Dict[str, Any]) -> str:
    """Schema 1 and linear schema 2 rows are keyed by the plain symbol (so a v1 and a v2 snapshot diff); other categories by ``category:symbol``."""
    return r["symbol"] if r.get("category", "linear") == "linear" else f"{r['category']}:{r['symbol']}"


def load_universe(path: str) -> Dict[str, Dict[str, Any]]:
    real = AR._refuse_protected(path)
    out: Dict[str, Dict[str, Any]] = {}
    with open(real, "rb") as fh:
        for i, line in enumerate(fh, 1):
            try:
                r = json.loads(line.decode("utf-8", "strict"))
            except (UnicodeDecodeError, ValueError) as exc:
                raise CaptureError(f"{os.path.basename(path)} line {i}: not JSON") from exc
            if not isinstance(r, dict) or r.get("schema") not in (SCHEMA_VERSION, ROW_SCHEMA_V2) or not isinstance(r.get("symbol"), str):
                raise CaptureError(f"{os.path.basename(path)} line {i}: not a schema-{SCHEMA_VERSION}/{ROW_SCHEMA_V2} universe row")
            if r["schema"] == ROW_SCHEMA_V2 and r.get("category") not in CATEGORIES:
                raise CaptureError(f"{os.path.basename(path)} line {i}: schema-2 row without a category")
            k = _row_key(r)
            if k in out:
                raise CaptureError(f"{os.path.basename(path)} line {i}: duplicate symbol {k} (a conflicted capture is never diffed)")
            out[k] = r
    return out


_DIFF_SKIP = frozenset({"schema", "snapshot_ms", "ticker", "seen_in", "conflict", "status", "symbol", "symbolId", "category", "fundingInterval_min",
                        "upperFundingRate", "lowerFundingRate"})


def diff_snapshots(old: Dict[str, Dict[str, Any]], new: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """Listings, delistings and changes between two point-in-time universes. A symbol absent from ``new`` is a delisting ONLY relative to these files.
    ``renames`` are one symbolId under two symbols across the snapshots (the symbols also stay in listed/delisted: nothing is merged);
    ``other_changes`` carry every rule field present in BOTH rows (tick, lot, notional, leverage, launch/delivery time, pre-listing flag...)."""
    listed = sorted(set(new) - set(old))
    delisted = sorted(set(old) - set(new))
    status, interval, caps, other, idchg = [], [], [], [], []
    transitions: Dict[str, int] = {}
    for s in sorted(set(old) & set(new)):
        a, b = old[s], new[s]
        if a["status"] != b["status"]:
            status.append({"symbol": s, "from": a["status"], "to": b["status"]})
            k = f"{a['status']}->{b['status']}"
            transitions[k] = transitions.get(k, 0) + 1
        if a["fundingInterval_min"] != b["fundingInterval_min"]:
            interval.append({"symbol": s, "from_min": a["fundingInterval_min"], "to_min": b["fundingInterval_min"]})
        if (a["upperFundingRate"], a["lowerFundingRate"]) != (b["upperFundingRate"], b["lowerFundingRate"]):
            caps.append({"symbol": s, "from": [a["lowerFundingRate"], a["upperFundingRate"]], "to": [b["lowerFundingRate"], b["upperFundingRate"]]})
        if a.get("symbolId") and b.get("symbolId") and a["symbolId"] != b["symbolId"]:
            idchg.append({"symbol": s, "from": a["symbolId"], "to": b["symbolId"]})
        for k in sorted((set(a) & set(b)) - _DIFF_SKIP):
            if a[k] != b[k]:
                other.append({"symbol": s, "field": k, "from": a[k], "to": b[k]})
    ids_old = {r["symbolId"]: k for k, r in old.items() if r.get("symbolId")}
    renames = [{"symbolId": sid, "from": old[ids_old[sid]]["symbol"], "to": r["symbol"]} for k, r in sorted(new.items())
               if (sid := r.get("symbolId")) and sid in ids_old and old[ids_old[sid]]["symbol"] != r["symbol"] and _row_key(old[ids_old[sid]]) != k]
    t0 = next((r["snapshot_ms"] for r in old.values()), None)
    t1 = next((r["snapshot_ms"] for r in new.values()), None)
    return {"old_count": len(old), "new_count": len(new), "listed": listed, "delisted": delisted, "status_changes": status,
            "status_transitions": dict(sorted(transitions.items())), "funding_interval_changes": interval, "funding_bound_changes": caps,
            "other_changes": other, "symbolid_changes": idchg, "renames": renames,
            "gap_ms": (t1 - t0) if isinstance(t0, int) and isinstance(t1, int) else None}


def _under(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def refuse_repo_path(out_dir: str, *, private_root: Optional[str] = None, allow_repo: bool = False) -> str:
    """Datasets stay outside Git: refuse an output directory under this repository or inside ANY git work tree (a ``.git`` entry in an ancestor), unless
    ``allow_repo`` (tests). With ``private_root`` the directory must lie under it and the root must be a real directory with no group/other access."""
    target = os.path.join(os.path.realpath(os.path.dirname(os.path.abspath(out_dir))), os.path.basename(os.path.abspath(out_dir)))
    if not allow_repo:
        if _under(target, os.path.realpath(os.path.dirname(BOT))):
            raise CaptureError("the output directory is under the repository: datasets stay outside Git")
        cur = os.path.dirname(target)
        while True:
            if os.path.lexists(os.path.join(cur, ".git")):
                raise CaptureError("the output directory is inside a git work tree: datasets stay outside Git")
            nxt = os.path.dirname(cur)
            if nxt == cur:
                break
            cur = nxt
    if private_root is not None:
        if os.path.islink(private_root) or not os.path.isdir(private_root):
            raise CaptureError("--private-root must be a real directory, not a link")
        root = os.path.realpath(private_root)
        if os.stat(root).st_mode & 0o077:
            raise CaptureError("--private-root must not be accessible to group or others (chmod 700)")
        if not allow_repo and (_under(root, os.path.realpath(os.path.dirname(BOT))) or os.path.lexists(os.path.join(root, ".git"))):
            raise CaptureError("--private-root is inside the repository or a git work tree")
        if not _under(target, root):
            raise CaptureError("the output directory is not under --private-root")
    return target


def _split(v: str, allowed: Sequence[str], what: str) -> List[str]:
    items = [x.strip() for x in v.split(",") if x.strip()]
    if not items or any(x not in allowed for x in items):
        raise CaptureError(f"--{what} must be a comma list of {','.join(allowed)}")
    return items


def main(argv: Optional[Sequence[str]] = None, *, fetch: Callable[[str], bytes] = http_fetch, clock_ms: Callable[[], int] = lambda: int(time.time() * 1000),
         sleep: Callable[[float], None] = time.sleep) -> int:
    ap = argparse.ArgumentParser(description="Owner-invoked read-only point-in-time capture of the public Bybit instrument universe by status partition.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("capture")
    c.add_argument("--out-dir", required=True, help="a NEW directory per run, OUTSIDE the repository; nothing in it is ever overwritten")
    c.add_argument("--categories", default="linear", help="comma list of linear,inverse,spot (default linear)")
    c.add_argument("--statuses", default=",".join(STATUSES), help="comma list of the status partitions to ask for")
    c.add_argument("--private-root", default=None, help="a 0700 directory the out-dir must lie under")
    c.add_argument("--allow-inside-repo-for-tests", action="store_true", help=argparse.SUPPRESS)
    d = sub.add_parser("diff")
    d.add_argument("old")
    d.add_argument("new")
    v = sub.add_parser("verify")
    v.add_argument("--dir", required=True)
    l1 = sub.add_parser("capture-v1", help="the legacy single-partition schema-1 capture")
    l1.add_argument("--out-dir", required=True)
    a = ap.parse_args(argv)
    try:
        if a.cmd == "capture":
            refuse_repo_path(a.out_dir, private_root=a.private_root, allow_repo=a.allow_inside_repo_for_tests)
            m = capture_universe(a.out_dir, fetch=fetch, clock_ms=clock_ms, sleep=sleep, categories=_split(a.categories, CATEGORIES, "categories"),
                                 statuses=_split(a.statuses, STATUSES, "statuses"))
            print(json.dumps({"capture_id": m["capture_id"], "dataset_status": m["dataset_status"], "delisting_observability": m["delisting_observability"],
                              "status_coverage": m["status_coverage"], "status_coverage_by_category": m["status_coverage_by_category"],
                              "rejected_instruments": m["rejected_instruments"]["count"], "rejected_tickers": (m["rejected_tickers"] or {}).get("count"),
                              "conflicts": m["conflicts"], "universe_rows": m["universe_file"]["rows"], "manifest": os.path.join(a.out_dir, "manifest.json")},
                             indent=2, sort_keys=True))
            return {"COMPLETE": 0, "CONFLICTED": 3, "PARTIAL": 3}.get(m["dataset_status"], 4)
        if a.cmd == "verify":
            r = verify_capture_dir(a.dir)
            print(json.dumps(r, indent=2, sort_keys=True))
            return 0 if r["ok"] else 1
        if a.cmd == "capture-v1":
            refuse_repo_path(a.out_dir)
            m = capture(a.out_dir, fetch=fetch, clock_ms=clock_ms, sleep=sleep)
            print(json.dumps({k: m[k] for k in ("snapshot_ms", "rows_seen", "rows_accepted", "rows_rejected", "rejected_by_reason", "clock")}, indent=2, sort_keys=True))
        else:
            print(json.dumps(diff_snapshots(load_universe(a.old), load_universe(a.new)), indent=2, sort_keys=True))
    except (CaptureError, AR.RecordError, OSError) as exc:
        print(f"universe_capture: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
