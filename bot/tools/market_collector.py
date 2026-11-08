"""Owner-invoked, PUBLIC-data-only collector: one bounded, point-in-time capture CYCLE of the Bybit USDT-linear perpetual market, and the seal / check / backup
machinery that makes a lost or altered cycle detectable.

Why. ``universe_capture`` and ``funding_capture`` are single shots. A research record that cannot show what it missed, cannot survive an interrupted run and
cannot detect a lost day is not an asset. This module only ORCHESTRATES: it reuses ``universe_capture`` for the universe and ticker snapshot and adds the
order-book and recent-trade artifacts for a bounded, auditable set of symbols. It never filters by performance and never fills a gap.

    GET <mainnet REST host from bot/venues.py>/v5/market/instruments-info | /v5/market/tickers          (via universe_capture; instruments cursor-paginated)
    GET <mainnet REST host from bot/venues.py>/v5/market/orderbook?category=linear&symbol=S&limit=N       (top N levels, bounded symbol budget)
    GET <mainnet REST host from bot/venues.py>/v5/market/recent-trade?category=linear&symbol=S&limit=M    (bounded rows, same symbols)

What it is NOT. No credential is read, nothing is signed, no order, position or account path exists in this module (a test asserts by AST that it imports no
connection/broker/signing module and builds URLs only from ``venues.rest_for`` plus the four paths above). It is never run by the test suite or CI: tests
inject ``fetch``, ``clock`` and ``sleep``. The agent that wrote it has not executed it against an exchange; the owner runs it.

SYMBOL SELECTION RULE (``turnover24h_desc_symbol_asc_v1``). From THIS cycle's ticker snapshot only: eligible = symbols that are accepted USDT-linear perpetuals in
THIS cycle's universe with status ``Trading`` and a valid ticker row with ``turnover24h`` > 0; sort by turnover24h DESCENDING (exact decimal), ties by symbol
ASCENDING; take the first ``max_symbols``. It reads no other cycle, no price history and no return. The rule id, the as-of time (the ticker receive time), the
source file hash and the chosen symbols with their turnover are written to the manifest, so the choice is auditable and not retroactive.

Cycle layout (a NEW directory per run, nothing overwritten, files 0600, directories 0700):
    cycle-<now_ms>/universe/...   universe_capture's own output (raw pages, tickers.json, universe-*.jsonl, manifest.json)
    cycle-<now_ms>/tickers.jsonl  validated ticker rows (derived; the raw bytes are universe/tickers.json)
    cycle-<now_ms>/books/<S>.json, trades/<S>.json   raw response bytes exactly as received
    cycle-<now_ms>/manifest.json  every artifact: request, receive ts, SHA-256, validation; gaps; selection; set-aside cycles
    cycle-<now_ms>/COMPLETE       written LAST: the manifest's SHA-256. No COMPLETE => the cycle is set aside as ``.INCOMPLETE`` on the next run.
    <out_root>/cycles.jsonl       evidence_chain-hashed row per COMPLETE cycle (id, manifest sha, counts, gaps)

STATUS. A cycle is ``OK`` only if its evidence is present and fresh: ``PARTIAL`` (with ``partial_reasons`` in the manifest) when there is any error, no universe, a failed book or
trades artifact, a conflict, zero instruments, zero valid ticker rows, no selected symbol (with max_symbols > 0), any clock-skew flag, a book older than ``MAX_BOOK_AGE_MS`` or
newest trades older than ``MAX_TRADE_AGE_MS`` at receive time.
RESUME. A COMPLETE directory that is not in cycles.jsonl is adopted only if it is a genuine cycle (own name, cycle_ms == name, after every recorded cycle, not after this run,
schema and file listing consistent, no symlink); anything else is set aside as ``.INCOMPLETE`` (numbered, never deleted).
CRASH REPAIR. After a crash between a log row and its chain entry (UNCHAINED_TAIL) the run is refused with a diagnostic; the owner runs
``python3 tools/market_collector.py repair-log --out-root R`` (evidence_chain.repair: the row is kept and marked resealed). It is never automatic.

What it cannot show: that the bytes came from the exchange (a hash proves what was read, not who produced it), or that the venue's data is complete.
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import re
import shutil
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Set, Tuple

BOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BOT)
sys.path.insert(0, os.path.join(BOT, "tools"))

import evidence_chain as EC  # noqa: E402
import universe_capture as UC  # noqa: E402
import runtime_capability as RC  # noqa: E402  (the role model only: pure, signs nothing, sends nothing)
import venues  # noqa: E402

SCHEMA_VERSION = 1
PATH_BOOK = "/v5/market/orderbook"
PATH_TRADES = "/v5/market/recent-trade"
PUBLIC_PATHS = (UC.PATH_INSTRUMENTS, UC.PATH_TICKERS, PATH_BOOK, PATH_TRADES)
CATEGORY = "linear"
SELECTION_RULE = "turnover24h_desc_symbol_asc_v1"
RATE_LIMIT_RETCODE = 10006
MAX_BODY_BYTES = UC.MAX_BODY_BYTES
TIMEOUT_S = 15.0
PAUSE_S = 0.25
SKEW_WARN_MS = UC.SKEW_WARN_MS
DEFAULT_MAX_SYMBOLS = 20
MAX_TURNOVER = Decimal(10) ** 15                  # a 24h USDT turnover above this is a poisoned response, not a market (BTC is ~1e10)
MAX_MS = 10 ** 14                                  # a millisecond clock below year ~5138; also keeps ``cycle-<ms>`` inside _CYCLE's 16 digits
MAX_BOOK_AGE_MS = 10_000                           # a book whose own timestamp is older (or further ahead) than this at receive time is STALE evidence
MAX_TRADE_AGE_MS = 600_000                         # the newest trade of a top-turnover symbol older than this at receive time is STALE evidence
DEFAULT_DEPTH = 50
DEFAULT_TRADES = 100
MAX_DEPTH = 200
MAX_TRADES = 1000
DEFAULT_INTERVAL_MS = 60_000
MAX_RETRIES = 3
BACKOFF_S = 1.0
BACKOFF_CAP_S = 30.0
DEFAULT_RESERVE_BYTES = 1 << 30
DEFAULT_MAX_BYTES = 50 << 30
LOG_NAME = "cycles.jsonl"
COMPLETE = "COMPLETE"

_SYMBOL = re.compile(r"^[A-Z0-9]{2,24}\Z", re.ASCII)
_DEC = re.compile(r"^[0-9]+(\.[0-9]+)?\Z", re.ASCII)
_SDEC = re.compile(r"^-?[0-9]+(\.[0-9]+)?\Z", re.ASCII)
_UINT = re.compile(r"^[0-9]{1,19}\Z", re.ASCII)
_CYCLE = re.compile(r"^cycle-[0-9]{1,16}\Z", re.ASCII)
_HEX = re.compile(r"^[0-9a-f]{64}\Z", re.ASCII)


class CollectorError(ValueError):
    """The cycle (or seal operation) cannot continue safely."""


class SealError(CollectorError):
    pass


class Locked(SealError):
    """Another collector operation holds ``<root>/.lock``."""


class RateLimited(Exception):
    """The venue throttled the request (HTTP 429). Raised by a fetch; handled with bounded backoff."""


class RateLimitExhausted(Exception):
    """Bounded backoff ran out: the cycle becomes a recorded PARTIAL cycle, never a loop."""


# ---------------------------------------------------------------- transport (the ONLY network primitive: ``http_fetch``)

def _base() -> str:
    base = venues.rest_for("mainnet")
    if not base.startswith("https://"):
        raise CollectorError("the registry's host is not https")
    return base


def request_url(path: str, *, symbol: Optional[str] = None, limit: Optional[int] = None) -> str:
    """Constants + one validated symbol + one bounded integer. Anything else is refused."""
    if path not in (PATH_BOOK, PATH_TRADES):
        raise CollectorError("refusing a path that is not a pinned per-symbol public path")
    if not isinstance(symbol, str) or not _SYMBOL.fullmatch(symbol):
        raise CollectorError("refusing a symbol that is not a short upper-case token")
    cap = MAX_DEPTH if path == PATH_BOOK else MAX_TRADES
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= cap:
        raise CollectorError(f"limit must be a whole number in 1..{cap}")
    q = [("category", CATEGORY), ("symbol", symbol), ("limit", limit)]
    return f"{_base()}{path}?{urllib.parse.urlencode(q)}"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_a, **_k):                      # a redirect is a different host: refuse it
        return None


class _UrllibTransport:
    """The innermost transport: one GET through urllib with no redirects. Only ever reached THROUGH ``CapabilityTransport`` (see ``_transport``)."""

    def request(self, method: str, url: str, *, headers: Mapping[str, str], body: Optional[str], timeout: float) -> bytes:
        opener = urllib.request.build_opener(_NoRedirect)
        req = urllib.request.Request(url, method="GET", headers=dict(headers))
        try:
            with opener.open(req, timeout=timeout) as resp:
                if resp.status != 200:
                    raise CollectorError(f"HTTP {resp.status}")
                body_b = resp.read(MAX_BODY_BYTES + 1)
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                raise RateLimited("HTTP 429") from exc
            raise CollectorError(f"HTTP {exc.code}") from exc
        except (urllib.error.URLError, OSError) as exc:
            raise CollectorError(f"request failed ({type(exc).__name__})") from exc
        if len(body_b) > MAX_BODY_BYTES:
            raise CollectorError("response larger than any market page")
        return body_b


def _transport() -> "RC.CapabilityTransport":
    """The collector's ONLY network path: the urllib transport behind the ``public_collector`` role (unsigned GET of public market paths on the registry host)."""
    return RC.CapabilityTransport(_UrllibTransport(), RC.Capability.for_role(RC.PUBLIC_COLLECTOR, venue="mainnet"))


def http_request_to(method: Any, url: Any, allowed_paths: Sequence[str], *, headers: Any = None, body: Any = None) -> bytes:
    """The only network primitive, for a caller (``market_history``) that pages other PUBLIC paths. ``allowed_paths`` is the caller's pinned set; the ``public_collector``
    role independently refuses (BEFORE a connection is opened) any POST, signed/credential-carrying or unparseable header, body, credential-looking query parameter,
    control character, other host or path outside ``runtime_capability.PUBLIC_MARKET_PATHS``; the pinned-path check below is a second, stricter fence."""
    base = _base()
    if not isinstance(url, str) or not any(url.startswith(base + p + "?") for p in allowed_paths):
        raise CollectorError("refusing a URL that is not the registry host and a pinned public market path")
    hdrs = {"Accept": "application/json", "User-Agent": "market-collector/1"} if headers is None else headers
    try:
        return _transport().request(method, url, headers=hdrs, body=body, timeout=TIMEOUT_S)
    except RC.CapabilityRefused as exc:
        raise CollectorError(f"refused by the public_collector role: {exc.code}") from exc


def http_request(method: Any, url: Any, *, headers: Any = None, body: Any = None) -> bytes:
    """The collector's own pinned paths (instruments, tickers, order book, recent trades) through ``http_request_to``."""
    return http_request_to(method, url, PUBLIC_PATHS, headers=headers, body=body)


def http_fetch(url: str) -> bytes:
    """One unauthenticated GET through ``http_request`` (so through the role). Tests never reach it."""
    return http_request("GET", url)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _is_rate_limit_body(body: bytes) -> bool:
    try:
        doc = json.loads(body.decode("utf-8", "strict"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        return False
    rc = doc.get("retCode") if isinstance(doc, dict) else None
    return isinstance(rc, int) and not isinstance(rc, bool) and rc == RATE_LIMIT_RETCODE


class _Fetcher:
    """Wraps the injected fetch: HTTP 429 / retCode 10006 => bounded backoff through the injected sleep, then RateLimitExhausted. Events are recorded."""

    def __init__(self, fetch: Callable[[str], bytes], sleep: Callable[[float], None], max_retries: int = MAX_RETRIES, backoff_s: float = BACKOFF_S):
        self.fetch, self.sleep, self.max_retries, self.backoff_s = fetch, sleep, max_retries, backoff_s
        self.events: List[Dict[str, Any]] = []
        self.requests = 0
        self.exhausted = False

    def __call__(self, url: str) -> bytes:
        path = urllib.parse.urlsplit(url).path
        attempt = 0
        while True:
            self.requests += 1
            reason = ""
            body = b""
            try:
                body = self.fetch(url)
                if _is_rate_limit_body(body):
                    reason = f"retCode {RATE_LIMIT_RETCODE}"
            except RateLimited:
                reason = "HTTP 429"
            if not reason:
                return body
            if attempt >= self.max_retries:
                self.exhausted = True
                self.events.append({"path": path, "attempt": attempt, "reason": reason, "action": "exhausted", "body_sha256": _sha(body) if body else ""})
                raise RateLimitExhausted(f"{path}: rate limited ({reason}) after {attempt} retries")
            delay = min(BACKOFF_CAP_S, self.backoff_s * (2 ** attempt))
            self.events.append({"path": path, "attempt": attempt, "reason": reason, "action": "backoff", "sleep_s": delay})
            self.sleep(delay)
            attempt += 1


# ---------------------------------------------------------------- validation (counted rejects, never silent drops)

def _dec(v: Any, *, signed: bool = False, positive: bool = False, nonneg: bool = False, bounded: bool = False) -> Optional[Decimal]:
    if not isinstance(v, str) or len(v) > 64 or not (_SDEC if signed else _DEC).fullmatch(v):
        return None
    try:
        d = Decimal(v)
    except InvalidOperation:
        return None
    if positive and not d > 0:
        return None
    if nonneg and d < 0:
        return None
    if bounded and not (d.is_finite() and 0 <= d <= MAX_TURNOVER):
        return None
    return d


def _bump(rejects: Dict[str, int], key: str, n: int = 1) -> None:
    rejects[key] = rejects.get(key, 0) + n


def _venue_time(doc: Any) -> Optional[int]:
    t = doc.get("time") if isinstance(doc, dict) else None
    if isinstance(t, int) and not isinstance(t, bool) and t > 0:
        return t
    if isinstance(t, str) and _UINT.fullmatch(t):
        return int(t)
    return None


def _load_doc(body: bytes, info: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    try:
        doc = json.loads(body.decode("utf-8", "strict"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        _bump(info["rejects"], "envelope:not_utf8_json")
        return None
    if not isinstance(doc, dict):
        _bump(info["rejects"], "envelope:not_an_object")
        return None
    info["venue_time_ms"] = _venue_time(doc)
    rc = doc.get("retCode")
    if isinstance(rc, bool) or rc != 0:
        _bump(info["rejects"], f"envelope:retCode_{rc if isinstance(rc, int) else 'bad'}")
        return None
    res = doc.get("result")
    if not isinstance(res, dict):
        _bump(info["rejects"], "envelope:no_result")
        return None
    return res


def _new_info() -> Dict[str, Any]:
    return {"ok": False, "rows": 0, "rejects": {}, "duplicates_identical": 0, "conflicts": 0, "venue_time_ms": None}


def validate_book(body: bytes, symbol: str, depth: int) -> Dict[str, Any]:
    info = _new_info()
    rej = info["rejects"]
    res = _load_doc(body, info)
    if res is None:
        return info
    if res.get("s") != symbol:
        _bump(rej, "wrong_symbol")
    ts = res.get("ts")
    if isinstance(ts, bool) or not (isinstance(ts, int) and ts > 0 or isinstance(ts, str) and _UINT.fullmatch(ts)):
        _bump(rej, "bad_ts")
    else:
        info["data_ts_ms"] = int(ts)
    sides: Dict[str, List[Tuple[Decimal, str]]] = {}
    for key, name in (("b", "bid"), ("a", "ask")):
        raw = res.get(key)
        if not isinstance(raw, list):
            _bump(rej, f"{name}s_not_a_list")
            sides[key] = []
            continue
        if len(raw) > depth:
            _bump(rej, "too_many_levels", len(raw) - depth)
        seen: Dict[Decimal, str] = {}
        levels: List[Tuple[Decimal, str]] = []
        for lv in raw:
            if not (isinstance(lv, list) and len(lv) >= 2):
                _bump(rej, "bad_level")
                continue
            p, q = _dec(lv[0], positive=True), _dec(lv[1], positive=True)
            if p is None or q is None:
                _bump(rej, "bad_level")
                continue
            if p in seen:
                if Decimal(seen[p]) == q:
                    info["duplicates_identical"] += 1
                else:
                    info["conflicts"] += 1
                    _bump(rej, "conflicting_duplicate_level")
                continue
            seen[p] = lv[1]
            levels.append((p, lv[1]))
        info["rows"] += len(levels)
        prices = [p for p, _ in levels]
        if prices != sorted(prices, reverse=(key == "b")):
            _bump(rej, f"unsorted_{name}s")
        if not levels:
            _bump(rej, "empty_side")
        sides[key] = levels
    if sides.get("b") and sides.get("a") and max(p for p, _ in sides["b"]) >= min(p for p, _ in sides["a"]):
        _bump(rej, "crossed_book")
    info["ok"] = not rej
    return info


def validate_trades(body: bytes, symbol: str, limit: int, recv_ms: int) -> Dict[str, Any]:
    info = _new_info()
    rej = info["rejects"]
    res = _load_doc(body, info)
    if res is None:
        return info
    if res.get("category") != CATEGORY:
        _bump(rej, "wrong_category")
    rows = res.get("list")
    if not isinstance(rows, list):
        _bump(rej, "list_not_a_list")
        return info
    if len(rows) > limit:
        _bump(rej, "too_many_rows", len(rows) - limit)
    seen: Dict[str, Tuple[str, str, str, int]] = {}
    for r in rows:
        if not isinstance(r, dict):
            _bump(rej, "not_an_object")
            continue
        eid, price, size, side, t = r.get("execId"), r.get("price"), r.get("size"), r.get("side"), r.get("time")
        tt = int(t) if isinstance(t, str) and _UINT.fullmatch(t) else (t if isinstance(t, int) and not isinstance(t, bool) and t > 0 else None)
        if r.get("symbol") != symbol:
            _bump(rej, "wrong_symbol")
            continue
        if not isinstance(eid, str) or not eid or len(eid) > 64 or not eid.isascii():
            _bump(rej, "bad_execId")
            continue
        if _dec(price, positive=True) is None or _dec(size, positive=True) is None or side not in ("Buy", "Sell") or tt is None:
            _bump(rej, "bad_field")
            continue
        key = (price, size, side, tt)
        if eid in seen:
            if seen[eid] == key:
                info["duplicates_identical"] += 1
            else:
                info["conflicts"] += 1
                _bump(rej, "conflicting_duplicate_execId")
            continue
        seen[eid] = key
        info["data_ts_ms"] = max(info.get("data_ts_ms") or 0, tt)
        if tt > recv_ms + SKEW_WARN_MS:
            _bump(rej, "trade_time_in_the_future")
    info["rows"] = len(seen)
    info["ok"] = not rej
    return info


def validate_tickers(body: bytes) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, Any]]:
    """(valid rows by symbol, info). Required: valid mark/index/funding/OI/bid1/ask1/volume/turnover. Every other row is a COUNTED reject with its symbol."""
    info = _new_info()
    info["rejected_symbols"] = {}
    res = _load_doc(body, info)
    out: Dict[str, Dict[str, Any]] = {}
    if res is None:
        return out, info
    rows = res.get("list")
    if res.get("category") != CATEGORY or not isinstance(rows, list):
        _bump(info["rejects"], "envelope:not_the_linear_list")
        return out, info
    conflicted: Set[str] = set()
    raw_by_symbol: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        sym = r.get("symbol") if isinstance(r, dict) else None
        if not isinstance(sym, str) or not _SYMBOL.fullmatch(sym):
            _bump(info["rejects"], "bad_symbol")
            continue
        if sym in raw_by_symbol:
            if raw_by_symbol[sym] == r:
                info["duplicates_identical"] += 1
            else:
                info["conflicts"] += 1
                conflicted.add(sym)
                _bump(info["rejects"], "conflicting_duplicate_ticker")
            continue
        raw_by_symbol[sym] = r
    for sym, r in raw_by_symbol.items():
        if sym in conflicted:
            info["rejected_symbols"][sym] = "conflicting_duplicate_ticker"
            continue
        why = ""
        vals: Dict[str, Any] = {}
        for k, kw in (("markPrice", {"positive": True}), ("indexPrice", {"positive": True}), ("fundingRate", {"signed": True}),
                      ("openInterest", {"nonneg": True}), ("bid1Price", {"positive": True}), ("ask1Price", {"positive": True}),
                      ("volume24h", {"nonneg": True}), ("turnover24h", {"nonneg": True, "bounded": True})):
            v = r.get(k)
            if v in (None, ""):
                why = f"missing:{k}"
                break
            if _dec(v, **kw) is None:
                why = f"invalid:{k}"
                break
            vals[k] = v
        if not why and _dec(vals["bid1Price"]) >= _dec(vals["ask1Price"]):          # type: ignore[operator]
            why = "crossed_bid1_ask1"
        if why:
            _bump(info["rejects"], why)
            info["rejected_symbols"][sym] = why
            continue
        for k in ("nextFundingTime", "openInterestValue", "lastPrice", "fundingIntervalHour", "bid1Size", "ask1Size"):
            if isinstance(r.get(k), str) and r[k] != "":
                vals[k] = r[k]
        out[sym] = vals
    info["rows"] = len(out)
    info["ok"] = bool(out)
    return out, info


# ---------------------------------------------------------------- symbol selection (same-cycle data only; never a return)

def select_symbols(tickers: Mapping[str, Mapping[str, Any]], eligible: Set[str], max_symbols: int) -> List[Dict[str, str]]:
    """``turnover24h_desc_symbol_asc_v1``: highest 24h turnover first, ties by symbol. Pure function of THIS cycle's ticker snapshot and universe."""
    if isinstance(max_symbols, bool) or not isinstance(max_symbols, int) or max_symbols < 0:
        raise CollectorError("max_symbols must be a non-negative whole number")
    cand = []
    for s, t in tickers.items():
        if s not in eligible:
            continue
        if not isinstance(s, str) or not _SYMBOL.fullmatch(s):
            raise CollectorError("refusing to select a symbol that is not a short upper-case token")
        d = _dec(t.get("turnover24h"), nonneg=True, bounded=True)
        if d is None:
            raise CollectorError(f"{s}: turnover24h is not a bounded non-negative decimal")
        if d > 0:
            cand.append((d, s))
    cand.sort(key=lambda c: (-c[0], c[1]))
    return [{"symbol": s, "turnover24h": tickers[s]["turnover24h"]} for _, s in cand[:max_symbols]]


# ---------------------------------------------------------------- filesystem helpers

def _write(path: str, data: bytes) -> None:
    UC._create_exclusive(path, data)


def _mkdir_exclusive(path: str) -> None:
    try:
        os.mkdir(path, 0o700)
    except FileExistsError as exc:
        raise CollectorError(f"{os.path.basename(path)} exists: a cycle goes in a NEW directory, nothing is overwritten") from exc


def _rel_files(cycle_dir: str) -> List[str]:
    out: List[str] = []
    for dp, dn, fn in os.walk(cycle_dir):
        dn.sort()
        for f in fn:
            out.append(os.path.relpath(os.path.join(dp, f), cycle_dir).replace(os.sep, "/"))
    return sorted(out)


def _file_sha(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _listing(cycle_dir: str, skip: Sequence[str] = ("manifest.json", COMPLETE)) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    for rel in _rel_files(cycle_dir):
        if rel in skip:
            continue
        p = os.path.join(cycle_dir, *rel.split("/"))
        out[rel] = {"sha256": _file_sha(p), "bytes": os.path.getsize(p)}
    return out


def _tree_sha(listing: Mapping[str, Mapping[str, Any]], manifest_sha: str) -> str:
    h = hashlib.sha256(manifest_sha.encode())
    for rel in sorted(listing):
        h.update(f"\n{rel}\t{listing[rel]['sha256']}\t{listing[rel]['bytes']}".encode())
    return h.hexdigest()


def _tree_bytes(root: str) -> int:
    total = 0
    for dp, _dn, fn in os.walk(root):
        for f in fn:
            try:
                total += os.lstat(os.path.join(dp, f)).st_size
            except OSError:
                pass
    return total


def _aside(path: str, suffix: str) -> str:
    cand = path + suffix
    n = 1
    while os.path.lexists(cand):
        n += 1
        cand = f"{path}{suffix}.{n}"
    os.rename(path, cand)
    return cand


def _log_rows(log: str) -> List[Dict[str, Any]]:
    return [r for r in EC.rows_of(EC.read_lines(log)[0]) if isinstance(r, dict)]


def _safe_cycle_name(name: Any) -> bool:
    return isinstance(name, str) and bool(name) and name not in (".", "..") and name == os.path.basename(name) and "\\" not in name and "\0" not in name


def _cycle_rows(log: str) -> List[Dict[str, Any]]:
    """Log/receipt rows whose ``cycle`` is a plain non-empty name. A hash-chained row can still carry a hand-made body (``cycle`` a list, a dict, an int, a path): such a row is
    skipped here and reported as MALFORMED_LOG_ROW by ``check_cycles``; it must never reach a hash, a set or a path join as a traceback."""
    return [r for r in _log_rows(log) if _safe_cycle_name(r.get("cycle"))]


def _symlinks(cycle_dir: str) -> List[str]:
    """Every symbolic link in (or being) the cycle directory. A link is never followed: it could make a 'verified' file be somebody else's bytes."""
    out: List[str] = []
    if os.path.islink(cycle_dir):
        return [os.path.basename(cycle_dir)]
    for dp, dn, fn in os.walk(cycle_dir):
        for n in dn + fn:
            full = os.path.join(dp, n)
            if os.path.islink(full):
                out.append(os.path.relpath(full, cycle_dir).replace(os.sep, "/"))
    return sorted(out)


def _is_uint(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and v >= 0


def manifest_shape_problems(man: Any, dirname: str) -> List[str]:
    """Is ``man`` a cycle manifest of THIS directory? Required keys with the right types, name == cycle == ``cycle-<cycle_ms>``, every artifact's hash equal to the
    file listing's. Pure: no filesystem access (the files are compared separately)."""
    if not isinstance(man, dict):
        return ["manifest.json is not a cycle manifest (not an object)"]
    probs: List[str] = []
    if man.get("schema_version") != SCHEMA_VERSION or isinstance(man.get("schema_version"), bool):
        probs.append("manifest: schema_version is not the expected version")
    for k, t in (("counts", dict), ("gaps", list), ("errors", list), ("artifacts", list), ("files", dict), ("selection", dict), ("request", dict)):
        if not isinstance(man.get(k), t):
            probs.append(f"manifest: {k} is missing or not a {t.__name__}")
    if man.get("status") not in ("OK", "PARTIAL"):
        probs.append("manifest: status is not OK or PARTIAL")
    ms = man.get("cycle_ms")
    if not _is_uint(ms) or not 0 < ms < MAX_MS:
        probs.append("manifest: cycle_ms is not a positive whole number")
    elif dirname != f"cycle-{ms}":
        probs.append(f"manifest: directory name {dirname!r} is not cycle-<cycle_ms> ({ms})")
    if man.get("cycle") != dirname:
        probs.append(f"manifest: cycle {man.get('cycle')!r} is not this directory's name")
    for k in ("started_ms", "finished_ms"):
        if not _is_uint(man.get(k)):
            probs.append(f"manifest: {k} is not a whole number")
    if probs:
        return probs
    files = man["files"]
    for rel, ent in files.items():
        if not (isinstance(rel, str) and isinstance(ent, dict) and isinstance(ent.get("sha256"), str) and _HEX.fullmatch(ent["sha256"]) and _is_uint(ent.get("bytes"))):
            probs.append(f"manifest: files entry {str(rel)[:40]!r} is malformed")
    if probs:
        return probs
    for a in man["artifacts"]:
        if not isinstance(a, dict) or not isinstance(a.get("kind"), str):
            probs.append("manifest: an artifact is not an object with a kind")
            continue
        f = a.get("file")
        if f is None:
            continue                                                       # a recorded gap (not fetched): no file to compare
        if f not in files or files[f]["sha256"] != a.get("sha256") or files[f]["bytes"] != a.get("bytes"):
            probs.append(f"manifest: artifact {a['kind']}/{a.get('symbol')} does not match the file listing for {f!r}")
        d = a.get("derived_file")
        if d is not None and (d not in files or files[d]["sha256"] != a.get("derived_sha256")):
            probs.append(f"manifest: artifact {a['kind']}/{a.get('symbol')} derived file {d!r} does not match the file listing")
    return probs


def verify_cycle_dir(cycle_dir: str) -> List[str]:
    """Problems found comparing a cycle directory with its own manifest and COMPLETE marker (independent of any log). Never raises on a hostile directory."""
    try:
        return _verify_cycle_dir(cycle_dir)
    except (TypeError, ValueError, KeyError, AttributeError, IndexError, OSError, RecursionError) as exc:
        return [f"manifest.json is not a valid cycle manifest ({type(exc).__name__})"]


def _verify_cycle_dir(cycle_dir: str) -> List[str]:
    problems: List[str] = []
    links = _symlinks(cycle_dir)
    if links:
        return [f"SYMLINK: {links[0]} is a symbolic link (never followed)"]
    mp, cp = os.path.join(cycle_dir, "manifest.json"), os.path.join(cycle_dir, COMPLETE)
    if not os.path.isfile(mp):
        return ["manifest.json is missing"]
    if not os.path.isfile(cp):
        return ["COMPLETE marker is missing"]
    msha = _file_sha(mp)
    with open(cp, "rb") as fh:
        marker = fh.read().decode("ascii", "replace").strip()
    if marker != msha:
        problems.append("ALTERED: manifest.json does not match the COMPLETE marker")
    try:
        with open(mp, "rb") as fh:
            man = json.loads(fh.read().decode("utf-8"))
    except (ValueError, UnicodeDecodeError, RecursionError):
        return problems + ["manifest.json is not a cycle manifest"]
    shape = manifest_shape_problems(man, os.path.basename(os.path.normpath(cycle_dir)))
    if shape:
        return problems + shape
    want = man["files"]
    have = set(_rel_files(cycle_dir)) - {"manifest.json", COMPLETE}
    for rel in sorted(set(want) - have):
        problems.append(f"MISSING: {rel}")
    for rel in sorted(have - set(want)):
        problems.append(f"ALTERED: unexpected file {rel}")
    for rel in sorted(have & set(want)):
        p = os.path.join(cycle_dir, *rel.split("/"))
        size = os.path.getsize(p)
        if size < want[rel]["bytes"]:
            problems.append(f"TRUNCATED: {rel} ({size} bytes, {want[rel]['bytes']} recorded)")
        elif size != want[rel]["bytes"] or _file_sha(p) != want[rel]["sha256"]:
            problems.append(f"ALTERED: {rel}")
    return problems


def cycle_dirs(root: str, *, complete_only: bool = True) -> List[str]:
    names = sorted(n for n in os.listdir(root) if _CYCLE.fullmatch(n) and os.path.isdir(os.path.join(root, n)) and not os.path.islink(os.path.join(root, n)))
    if complete_only:
        names = [n for n in names if os.path.isfile(os.path.join(root, n, COMPLETE))]
    return names


# ---------------------------------------------------------------- one capture cycle

def _artifact(kind: str, symbol: str, file: str, body: bytes, url: str, recv: int, info: Dict[str, Any]) -> Dict[str, Any]:
    q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
    return {"kind": kind, "symbol": symbol, "file": file, "sha256": _sha(body), "bytes": len(body),
            "request": {"path": urllib.parse.urlsplit(url).path, "params": q, "url": url}, "recv_ms": recv, "schema_version": SCHEMA_VERSION,
            **UC._skew(recv, info.get("venue_time_ms")), "validation": {k: v for k, v in info.items() if k != "venue_time_ms"}}


def _inside(path: str, directory: str) -> bool:
    """True when ``path`` is ``directory`` or anything under it AFTER resolving symlinks (an alias, a subdirectory or a link into the root is still inside)."""
    rp, rd = os.path.realpath(path), os.path.realpath(directory)
    return rp == rd or rp.startswith(rd + os.sep)


LOCK_NAME = ".lock"


@contextlib.contextmanager
def _locked(root: str):
    """Exclusive, NON-blocking flock on ``<root>/.lock`` for a whole operation (run_once, seal, backup, repair-log). A second concurrent operation gets ``Locked``
    instead of renaming the first one's in-progress directory to ``.INCOMPLETE`` (reproduced). The kernel drops the lock when the process dies, so a crash never wedges it."""
    if not os.path.isdir(root):
        yield                                           # nothing exists to protect; the operation reports its own error
        return
    fd = os.open(os.path.join(root, LOCK_NAME), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise Locked(f"REFUSED_LOCKED: another collector operation holds {os.path.join(root, LOCK_NAME)}") from exc
        yield
    finally:
        os.close(fd)


def _refuse(status: str, **kw: Any) -> Dict[str, Any]:
    return {"status": status, **kw}


def _check_ms(name: str, v: Any) -> int:
    if isinstance(v, bool) or not isinstance(v, int) or not 0 < v < MAX_MS:
        raise CollectorError(f"{name} must be a positive whole number below {MAX_MS}")
    return v


def run_once(out_root: str, now_ms: int, fetch: Callable[[str], bytes] = http_fetch, clock: Callable[[], int] = lambda: int(time.time() * 1000), *,
             sleep: Callable[[float], None] = time.sleep, max_symbols: int = DEFAULT_MAX_SYMBOLS, depth: int = DEFAULT_DEPTH,
             trades_limit: int = DEFAULT_TRADES, interval_ms: int = DEFAULT_INTERVAL_MS, reserve_bytes: int = DEFAULT_RESERVE_BYTES,
             max_bytes: int = DEFAULT_MAX_BYTES, free_bytes_fn: Callable[[str], int] = lambda p: shutil.disk_usage(p).free,
             max_retries: int = MAX_RETRIES, backoff_s: float = BACKOFF_S) -> Dict[str, Any]:
    """ONE capture cycle into a NEW directory. Returns a report; refusals (storage, a broken log, a non-advancing clock) are reported, not raised."""
    for name, v in (("now_ms", now_ms), ("interval_ms", interval_ms)):
        _check_ms(name, v)
    if not 0 <= max_symbols <= 500 or not 1 <= depth <= MAX_DEPTH or not 1 <= trades_limit <= MAX_TRADES:
        raise CollectorError("max_symbols/depth/trades_limit out of bounds")
    root = os.path.abspath(out_root)
    parent = os.path.dirname(root)
    if os.path.islink(root) or os.path.islink(parent) or not os.path.isdir(parent):
        raise CollectorError("the output root must be a real directory, not a link")
    UC.AR._refuse_protected(os.path.join(root, LOG_NAME))
    os.makedirs(root, mode=0o700, exist_ok=True)
    try:
        with _locked(root):
            return _run_locked(root, now_ms, fetch, clock, sleep, max_symbols, depth, trades_limit, interval_ms, reserve_bytes, max_bytes, free_bytes_fn, max_retries, backoff_s)
    except Locked as exc:
        return _refuse("REFUSED_LOCKED", reason=str(exc))


def _run_locked(root: str, now_ms: int, fetch: Callable[[str], bytes], clock: Callable[[], int], sleep: Callable[[float], None], max_symbols: int, depth: int,
                trades_limit: int, interval_ms: int, reserve_bytes: int, max_bytes: int, free_bytes_fn: Callable[[str], int], max_retries: int, backoff_s: float) -> Dict[str, Any]:
    # storage budget: refuse BEFORE anything is created, and report
    used, free = _tree_bytes(root), int(free_bytes_fn(root))
    if free < reserve_bytes or used > max_bytes:
        return _refuse("REFUSED_STORAGE", free_bytes=free, reserve_bytes=reserve_bytes, root_bytes=used, max_bytes=max_bytes,
                       reason="free space below the reserve" if free < reserve_bytes else "the root exceeds max_bytes")

    log = os.path.join(root, LOG_NAME)
    if os.path.isfile(log):
        ver = EC.verify(log)
        if not ver.get("ok", False):
            codes = sorted({str(i.get("code")) for i in ver.get("issues", []) if isinstance(i, dict)})
            hint = ""
            if "UNCHAINED_TAIL" in codes and ver.get("internally_consistent"):
                hint = (f" UNCHAINED_TAIL: a crash left {ver.get('unchained_tail')} row(s) written but not chained. This is repairable and is NEVER repaired automatically: the owner "
                        f"runs `python3 tools/market_collector.py repair-log --out-root {root}` (the row is kept and marked resealed).")
            return _refuse("REFUSED_LOG", reason="cycles.jsonl does not verify; a new cycle would be unrecordable",
                           detail=(f"{','.join(codes) or 'unknown'}: " + "; ".join(f"{i.get('code')}: {i.get('detail')}" for i in ver.get("issues", []) if isinstance(i, dict)))[:300] + hint)
    rows = _log_rows(log)

    # resume: an interrupted cycle is set aside, never reused or overwritten; a COMPLETE-but-unrecorded one is recorded late ONLY if it is a genuine cycle of this
    # collector: self-consistent is not enough (a fabricated dir with a far-future cycle_ms wedged the log), so it must also carry its own name, a cycle_ms strictly
    # after everything already recorded and not after this run's clock, and agree with its file listing. Anything else is set aside (numbered, never deleted).
    set_aside: List[Dict[str, str]] = []
    recorded = {r["cycle"] for r in _cycle_rows(log)}
    max_ms = max((r["cycle_ms"] for r in rows if _is_uint(r.get("cycle_ms"))), default=0)
    for name in sorted(cycle_dirs(root, complete_only=False), key=lambda n: int(n[len("cycle-"):])):
        path = os.path.join(root, name)

        def _aside_cycle(reason: str) -> None:
            set_aside.append({"cycle": name, "moved_to": os.path.basename(_aside(path, ".INCOMPLETE")), "reason": reason})
        if not os.path.isfile(os.path.join(path, COMPLETE)):
            _aside_cycle("no COMPLETE marker")
        elif name not in recorded:
            if verify_cycle_dir(path):
                _aside_cycle("COMPLETE but failed verification")
                continue
            with open(os.path.join(path, "manifest.json"), "rb") as fh:
                old = json.loads(fh.read())
            if old["cycle_ms"] > now_ms:
                _aside_cycle("COMPLETE but its cycle_ms is in the future of this run")
            elif old["cycle_ms"] <= max_ms:
                _aside_cycle("COMPLETE but its cycle_ms is not after the previous recorded cycle")
            else:
                EC.append(log, {"at_ms": int(clock()), "cycle": name, "cycle_ms": old["cycle_ms"], "manifest_sha256": _file_sha(os.path.join(path, "manifest.json")),
                                "status": old["status"], "counts": old["counts"], "gaps": old["gaps"], "recovered": True})
                max_ms = old["cycle_ms"]
    rows = _log_rows(log)

    prev = max((r for r in rows if _is_uint(r.get("cycle_ms"))), key=lambda r: r["cycle_ms"], default=None)
    gaps: List[Dict[str, Any]] = []
    if prev is not None:
        if now_ms <= prev["cycle_ms"]:
            return _refuse("REFUSED_CLOCK", reason="now_ms does not advance past the previous recorded cycle", previous_cycle_ms=prev["cycle_ms"])
        delta = now_ms - prev["cycle_ms"]
        missed = max(0, round(delta / interval_ms) - 1)
        if missed:
            gaps.append({"previous_cycle": prev["cycle"], "previous_ms": prev["cycle_ms"], "gap_ms": delta, "interval_ms": interval_ms,
                         "missed_expected_cycles": missed, "note": "recorded, never filled or interpolated"})

    cid = f"cycle-{now_ms}"
    cdir = os.path.join(root, cid)
    _mkdir_exclusive(cdir)
    started = int(clock())
    fx = _Fetcher(fetch, sleep, max_retries, backoff_s)
    errors: List[str] = []
    artifacts: List[Dict[str, Any]] = []
    counts: Dict[str, Any] = {}
    selection: Dict[str, Any] = {"rule": SELECTION_RULE, "as_of_ms": None, "source": None, "max_symbols": max_symbols, "selected": []}
    umanifest: Optional[Dict[str, Any]] = None
    try:
        # (a) universe + (b) ticker snapshot: universe_capture's own capture (raw bytes, cursor chain, duplicates, skew)
        try:
            umanifest = UC.capture(os.path.join(cdir, "universe"), fetch=fx, clock_ms=lambda: int(clock()), sleep=sleep)
        except (UC.CaptureError, CollectorError) as exc:
            errors.append(f"universe: {exc}")
        except RateLimitExhausted as exc:
            errors.append(f"universe: {exc}")
        tickers: Dict[str, Dict[str, Any]] = {}
        eligible: Set[str] = set()
        if umanifest is not None:
            tbody_path = os.path.join(cdir, "universe", "tickers.json")
            with open(tbody_path, "rb") as fh:
                tbody = fh.read()
            tickers, tinfo = validate_tickers(tbody)
            lines = [json.dumps(dict(tickers[s], symbol=s, recv_ms=umanifest["tickers"]["recv_ms"]), sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n"
                     for s in sorted(tickers)]
            tj = "".join(lines).encode()
            _write(os.path.join(cdir, "tickers.jsonl"), tj)
            artifacts.append({"kind": "tickers", "symbol": "*", "file": "universe/tickers.json", "sha256": umanifest["tickers"]["sha256"], "bytes": umanifest["tickers"]["bytes"],
                              "derived_file": "tickers.jsonl", "derived_sha256": _sha(tj), "request": {"path": UC.PATH_TICKERS, "params": {"category": CATEGORY}},
                              "recv_ms": umanifest["tickers"]["recv_ms"], "schema_version": SCHEMA_VERSION, "venue_time_ms": umanifest["tickers"]["venue_time_ms"],
                              "skew_ms": umanifest["tickers"]["skew_ms"], "skew_flag": umanifest["tickers"]["skew_flag"],
                              "validation": {k: v for k, v in tinfo.items() if k != "venue_time_ms"}})
            ufile = os.path.join(cdir, "universe", umanifest["universe"]["file"])
            uni = UC.load_universe(ufile)
            eligible = {s for s, r in uni.items() if r.get("status") == "Trading"}
            selection.update({"as_of_ms": umanifest["tickers"]["recv_ms"], "source": {"file": "universe/tickers.json", "sha256": umanifest["tickers"]["sha256"]}})
            selection["selected"] = select_symbols(tickers, eligible, max_symbols)
            selection["eligible_count"] = sum(1 for s in tickers if s in eligible and Decimal(tickers[s]["turnover24h"]) > 0)

            # (c) order books, (d) recent trades: same bounded symbols, in selection order
            os.makedirs(os.path.join(cdir, "books"), mode=0o700)
            os.makedirs(os.path.join(cdir, "trades"), mode=0o700)
            aborted = False
            for item in selection["selected"]:
                sym = item["symbol"]
                for kind, path, lim, validator in (("book", PATH_BOOK, depth, validate_book), ("trades", PATH_TRADES, trades_limit, validate_trades)):
                    if aborted:
                        artifacts.append({"kind": kind, "symbol": sym, "file": None, "validation": {"ok": False, "rejects": {"not_fetched:rate_limited": 1}}})
                        continue
                    url = request_url(path, symbol=sym, limit=lim)
                    try:
                        body = fx(url)
                    except RateLimitExhausted as exc:
                        errors.append(str(exc))
                        aborted = True
                        artifacts.append({"kind": kind, "symbol": sym, "file": None, "validation": {"ok": False, "rejects": {"not_fetched:rate_limited": 1}}})
                        continue
                    except (CollectorError, UC.CaptureError, OSError) as exc:
                        errors.append(f"{kind} {sym}: {exc}")
                        artifacts.append({"kind": kind, "symbol": sym, "file": None, "validation": {"ok": False, "rejects": {"fetch_error": 1}}})
                        continue
                    recv = int(clock())
                    if len(body) > MAX_BODY_BYTES:
                        errors.append(f"{kind} {sym}: response larger than any market page")
                        artifacts.append({"kind": kind, "symbol": sym, "file": None, "validation": {"ok": False, "rejects": {"body_too_large": 1}}})
                        continue
                    fname = f"{'books' if kind == 'book' else 'trades'}/{sym}.json"
                    _write(os.path.join(cdir, *fname.split("/")), body)          # unedited, before anything is concluded about it
                    info = validate_book(body, sym, lim) if kind == "book" else validate_trades(body, sym, lim, recv)
                    ts = info.pop("data_ts_ms", None)
                    if ts is not None:
                        info["age_ms"] = recv - ts
                    bound = MAX_BOOK_AGE_MS if kind == "book" else MAX_TRADE_AGE_MS
                    info["stale"] = bool(info["ok"] and (ts is None or recv - ts > bound or (kind == "book" and ts - recv > bound)))
                    artifacts.append(_artifact(kind, sym, fname, body, url, recv, info))
                    sleep(PAUSE_S)
    except (OSError, UC.CaptureError, CollectorError) as exc:   # e.g. disk full or a vanished file mid-cycle: a report, not a crash; the directory has no COMPLETE and is set aside next run
        return _refuse("ABORTED", cycle=cid, reason=f"{type(exc).__name__}: {exc}")

    # ------------------------------------------------------------ manifest
    def _n(kind: str, ok: bool) -> int:
        return sum(1 for a in artifacts if a["kind"] == kind and bool(a["validation"]["ok"]) == ok)
    skew_flags = [a for a in artifacts if a.get("skew_flag")]
    if umanifest is not None:
        skew_flags += [{"kind": "universe_page", "page": p["page"]} for p in umanifest["pages"] if p["skew_flag"]]
    rejects_total: Dict[str, int] = {}
    for a in artifacts:
        for k, v in a["validation"]["rejects"].items():
            _bump(rejects_total, f"{a['kind']}:{k}", v)
    counts = {"instruments": umanifest["rows_accepted"] if umanifest else 0, "instrument_rows_rejected": umanifest["rows_rejected"] if umanifest else 0,
              "ticker_rows_valid": next((a["validation"]["rows"] for a in artifacts if a["kind"] == "tickers"), 0),
              "symbols_selected": len(selection["selected"]), "books_ok": _n("book", True), "books_failed": _n("book", False),
              "trades_ok": _n("trades", True), "trades_failed": _n("trades", False), "requests": fx.requests,
              "books_stale": sum(1 for a in artifacts if a["kind"] == "book" and a["validation"].get("stale")),
              "trades_stale": sum(1 for a in artifacts if a["kind"] == "trades" and a["validation"].get("stale")),
              "rate_limit_events": len(fx.events), "skew_flags": len(skew_flags), "rejects_total": dict(sorted(rejects_total.items())),
              "duplicates_identical": sum(a["validation"].get("duplicates_identical", 0) for a in artifacts),
              "conflicts": sum(a["validation"].get("conflicts", 0) for a in artifacts)}
    reasons: List[str] = []
    for cond, why in ((bool(errors), "errors"), (umanifest is None, "no_universe"), (bool(counts["books_failed"]), "books_failed"), (bool(counts["trades_failed"]), "trades_failed"),
                      (bool(counts["conflicts"]), "conflicts"), (counts["instruments"] == 0, "instruments==0"), (counts["ticker_rows_valid"] == 0, "ticker_rows_valid==0"),
                      (counts["symbols_selected"] == 0 and max_symbols > 0, "symbols_selected==0"), (counts["skew_flags"] > 0, "skew_flags"),
                      (counts["books_stale"] > 0, "stale_books"), (counts["trades_stale"] > 0, "stale_trades")):
        if cond:
            reasons.append(why)
    status = "PARTIAL" if reasons else "OK"
    manifest = {
        "schema_version": SCHEMA_VERSION, "cycle": cid, "cycle_ms": now_ms, "started_ms": started, "status": status,
        "request": {"host": venues.host_of(_base()), "paths": list(PUBLIC_PATHS), "category": CATEGORY, "depth": depth, "trades_limit": trades_limit,
                    "max_book_age_ms": MAX_BOOK_AGE_MS, "max_trade_age_ms": MAX_TRADE_AGE_MS},
        "selection": selection, "artifacts": artifacts, "universe_manifest": {"file": "universe/manifest.json"} if umanifest else None,
        "counts": counts, "errors": errors, "partial_reasons": reasons, "rate_limit_events": fx.events, "gaps": gaps, "set_aside": set_aside,
        "clock": {"skew_warn_ms": SKEW_WARN_MS, "flagged": skew_flags},
        "note": "point-in-time; selection uses this cycle's tickers only; gaps and rejects are recorded, never filled or hidden; a hash shows what was read, not who produced it",
    }
    manifest["finished_ms"] = int(clock())
    try:
        manifest["files"] = _listing(cdir)
        mbytes = (json.dumps(manifest, indent=2, sort_keys=True, allow_nan=False) + "\n").encode("utf-8")
        _write(os.path.join(cdir, "manifest.json"), mbytes)
        msha = _sha(mbytes)
        _write(os.path.join(cdir, COMPLETE), (msha + "\n").encode("ascii"))
        EC.append(log, {"at_ms": int(clock()), "cycle": cid, "cycle_ms": now_ms, "manifest_sha256": msha, "status": status, "counts": counts, "gaps": gaps})
    except OSError as exc:
        return _refuse("ABORTED", cycle=cid, reason=f"{type(exc).__name__}: {exc}")
    return {"status": status, "cycle": cid, "manifest_sha256": msha, "counts": counts, "gaps": gaps, "errors": errors, "partial_reasons": reasons, "set_aside": set_aside}


# ---------------------------------------------------------------- seal / check (the outside memory; compare with EVERY earlier row)

def _describe_cycle(root: str, name: str) -> Dict[str, Any]:
    path = os.path.join(root, name)
    msha = _file_sha(os.path.join(path, "manifest.json"))
    lst = _listing(path)
    return {"cycle": name, "manifest_sha256": msha, "files": lst, "tree_sha256": _tree_sha(lst, msha)}


def seal_cycles(root: str, manifest: str, *, now_ms: Optional[int] = None, accept_problems: bool = False) -> Dict[str, Any]:
    """Append one chained row describing every COMPLETE cycle not yet sealed (and the ids in cycles.jsonl now). REFUSES over existing problems."""
    now = int(time.time() * 1000) if now_ms is None else _check_ms("now_ms", now_ms)
    if _inside(manifest, root):
        raise SealError("the seal manifest must not live at or under the collector's own directory: it is the outside memory")
    with _locked(os.path.abspath(root)):
        return _seal_locked(root, manifest, now, accept_problems)


def _seal_locked(root: str, manifest: str, now: int, accept_problems: bool) -> Dict[str, Any]:
    if os.path.isfile(manifest) and not accept_problems:
        prior = check_cycles(root, manifest)
        if not prior["ok"]:
            raise SealError(f"refusing to seal over existing problems (that would make them the new baseline): {prior['problems'][:3]}")
    already: Set[str] = set()
    if os.path.isfile(manifest):
        for r in EC.rows_of(EC.read_lines(manifest)[0]):
            if isinstance(r, dict) and isinstance(r.get("cycles"), list):
                already |= {c["cycle"] for c in r["cycles"] if _cycle_entry_problem(c) is None}
    names = cycle_dirs(root)
    bad = [n for n in names if n not in already and verify_cycle_dir(os.path.join(root, n))]
    if bad and not accept_problems:
        raise SealError(f"refusing to seal a cycle that fails its own manifest: {bad[:3]}")
    new = [_describe_cycle(root, n) for n in names if n not in already]
    if not names:
        raise SealError("no COMPLETE cycles to seal")
    log_path = os.path.join(root, LOG_NAME)
    if len(_cycle_rows(log_path)) != len(_log_rows(log_path)):
        raise SealError(f"refusing to seal: {LOG_NAME} has a row with a bad cycle name (MALFORMED_LOG_ROW)")
    log_ids = [r["cycle"] for r in _cycle_rows(log_path)]
    n, head = EC.append(manifest, {"at_ms": now, "cycles": new, "log_cycles": log_ids})
    return {"manifest_rows": n, "head": head, "sealed_new": len(new), "cycles_total": len(names)}


def _cycle_entry_problem(c: Any) -> Optional[str]:
    """Why a sealed-cycle entry is malformed (a hash-chained row can still carry a hand-made body), else None."""
    if not isinstance(c, dict):
        return "a cycles entry is not an object"
    name = c.get("cycle")
    if not isinstance(name, str) or not name or name in (".", "..") or "/" in name or "\\" in name or "\0" in name:
        return f"a cycles entry has a bad cycle name {str(name)[:40]!r}"
    if not isinstance(c.get("manifest_sha256"), str):
        return f"{name}: no manifest_sha256"
    files = c.get("files")
    if not isinstance(files, dict):
        return f"{name}: files is not an object"
    for rel, f in files.items():
        if not isinstance(rel, str) or not isinstance(f, dict) or not isinstance(f.get("sha256"), str) or not isinstance(f.get("bytes"), int) or isinstance(f.get("bytes"), bool):
            return f"{name}: file entry {str(rel)[:60]!r} lacks bytes/sha256"
    return None


def _row_log_cycles(row: Mapping[str, Any]) -> Optional[Set[str]]:
    lc = row.get("log_cycles", [])
    if not isinstance(lc, list) or not all(isinstance(x, str) for x in lc):
        return None
    return set(lc)


def check_cycles(root: str, manifest: str) -> Dict[str, Any]:
    """Compare the directory with EVERY sealed row: MISSING (a cycle or a file gone), TRUNCATED (a file shorter / a log row lost), ALTERED (a hash changed)."""
    problems: List[str] = []
    if not os.path.isfile(manifest):
        return {"ok": False, "problems": ["no seal manifest: nothing outside the collector remembers what it captured"], "rows_checked": 0}
    ver = EC.verify(manifest)
    if not ver.get("ok", False):
        problems.append(f"the seal manifest's own chain does not verify: {ver.get('problems') or ver.get('state') or ver}")
    rows = [r for r in EC.rows_of(EC.read_lines(manifest)[0]) if isinstance(r, dict)]
    sealed_log: Set[str] = set()
    for idx, row in enumerate(rows):
        lc = _row_log_cycles(row)
        if lc is None:
            problems.append(f"MALFORMED_SEAL_ROW: row {idx} log_cycles is not a list of strings")
        else:
            sealed_log |= lc
        cyc = row.get("cycles", [])
        if not isinstance(cyc, list):
            problems.append(f"MALFORMED_SEAL_ROW: row {idx} cycles is not a list")
            continue
        for c in cyc:
            bad = _cycle_entry_problem(c)
            if bad:
                problems.append(f"MALFORMED_SEAL_ROW: row {idx}: {bad}")
                continue
            name, path = c["cycle"], os.path.join(root, c["cycle"])
            if not os.path.isdir(path):
                problems.append(f"{name}: MISSING (it was sealed with {len(c['files'])} files)")
                continue
            if not os.path.isfile(os.path.join(path, "manifest.json")) or not os.path.isfile(os.path.join(path, COMPLETE)):
                problems.append(f"{name}: MISSING (manifest.json or COMPLETE marker gone)")
                continue
            if _file_sha(os.path.join(path, "manifest.json")) != c["manifest_sha256"]:
                problems.append(f"{name}: ALTERED (manifest.json hash changed)")
            have = set(_rel_files(path)) - {"manifest.json", COMPLETE}
            for rel in sorted(set(c["files"]) - have):
                problems.append(f"{name}: MISSING file {rel}")
            for rel in sorted(have - set(c["files"])):
                problems.append(f"{name}: ALTERED (unexpected file {rel})")
            for rel in sorted(have & set(c["files"])):
                p = os.path.join(path, *rel.split("/"))
                size = os.path.getsize(p)
                if size < c["files"][rel]["bytes"]:
                    problems.append(f"{name}: TRUNCATED {rel} ({size} bytes, {c['files'][rel]['bytes']} sealed)")
                elif size != c["files"][rel]["bytes"] or _file_sha(p) != c["files"][rel]["sha256"]:
                    problems.append(f"{name}: ALTERED {rel}")
    log = os.path.join(root, LOG_NAME)
    if sealed_log:
        if not os.path.isfile(log):
            problems.append(f"{LOG_NAME}: MISSING ({len(sealed_log)} cycles were recorded)")
        else:
            lv = EC.verify(log)
            if not lv.get("ok", False):
                problems.append(f"{LOG_NAME}: its own chain does not verify")
            have_ids = {r["cycle"] for r in _cycle_rows(log)}
            for cid in sorted(sealed_log - have_ids):
                problems.append(f"{LOG_NAME}: TRUNCATED (the row for {cid} is gone)")
    if os.path.isfile(log):
        for r in _log_rows(log):
            if not _safe_cycle_name(r.get("cycle")):
                problems.append(f"MALFORMED_LOG_ROW: a {LOG_NAME} row has a bad cycle name {str(r.get('cycle'))[:40]!r}")
                continue
            p = os.path.join(root, r["cycle"], "manifest.json")
            if os.path.isfile(p) and _file_sha(p) != r.get("manifest_sha256"):
                problems.append(f"{r['cycle']}: ALTERED (manifest.json differs from its {LOG_NAME} row)")
            elif not os.path.isfile(p):
                problems.append(f"{r.get('cycle')}: MISSING (recorded in {LOG_NAME} but its manifest is gone)")
    return {"ok": not problems, "problems": sorted(set(problems)), "rows_checked": len(rows)}


# ---------------------------------------------------------------- backup / restore-check

def copy_cycle_dir(src: str, dst: str) -> None:
    """Default transfer: every file re-created exclusively (never overwriting) at 0600."""
    _mkdir_exclusive(dst)
    for rel in _rel_files(src):
        target = os.path.join(dst, *rel.split("/"))
        os.makedirs(os.path.dirname(target), mode=0o700, exist_ok=True)
        with open(os.path.join(src, *rel.split("/")), "rb") as fh:
            _write(target, fh.read())


def _sealed_cycles(manifest: str) -> Dict[str, Dict[str, Any]]:
    """Every cycle the seal manifest describes (its first description: a cycle is sealed once)."""
    out: Dict[str, Dict[str, Any]] = {}
    if os.path.isfile(manifest):
        for r in EC.rows_of(EC.read_lines(manifest)[0]):
            if isinstance(r, dict) and isinstance(r.get("cycles"), list):
                for c in r["cycles"]:
                    if _cycle_entry_problem(c) is None:
                        out.setdefault(c["cycle"], c)
    return out


def backup_cycles(src: str, dest: str, receipts: str, copy_fn: Callable[[str, str], None] = copy_cycle_dir, *, manifest: str, now_ms: Optional[int] = None) -> Dict[str, Any]:
    """Copy every COMPLETE, self-consistent, recorded AND SEALED cycle to ``dest`` (never overwriting), re-hash the COPY against the sealed hashes, then write a chained
    receipt. Bound to the seal ``manifest`` like ``md_seal.backup``: the whole backup is REFUSED when the root fails its seal or ``cycles.jsonl`` does not verify (a backup
    would launder the damage). A cycle that is skipped because it fails its own manifest, is not recorded in cycles.jsonl, or is not sealed makes the result NOT ok
    (an interrupted cycle without COMPLETE and an already-receipted one do not). A corrupt copy is set aside as ``.REJECTED`` and gets no receipt."""
    now = int(time.time() * 1000) if now_ms is None else _check_ms("now_ms", now_ms)
    if _inside(dest, src):
        raise SealError("the backup destination is the collector's own directory: a copy beside the original is lost with it")
    if _inside(receipts, src) or _inside(manifest, src):
        raise SealError("the receipts and the seal manifest must not live at or under the collector's own directory: they are the outside memory")
    with _locked(os.path.abspath(src)):
        return _backup_locked(src, dest, receipts, copy_fn, manifest, now)


def _backup_locked(src: str, dest: str, receipts: str, copy_fn: Callable[[str, str], None], manifest: str, now: int) -> Dict[str, Any]:
    chk = check_cycles(src, manifest)
    if not chk["ok"]:
        raise SealError(f"refusing to back up: the collector's root fails its seal manifest (a backup would launder the damage): {chk['problems'][:3]}")
    log = os.path.join(src, LOG_NAME)
    if not os.path.isfile(log) or not EC.verify(log).get("ok", False):
        raise SealError(f"refusing to back up: {LOG_NAME} is missing or does not verify")
    sealed = _sealed_cycles(manifest)
    os.makedirs(dest, exist_ok=True)
    have: Set[str] = set()
    if os.path.isfile(receipts):
        have = {r["cycle"] for r in _cycle_rows(receipts)}
    recorded = {r["cycle"] for r in _cycle_rows(log)}
    done: List[Dict[str, Any]] = []
    skipped: List[str] = []
    failed: List[str] = []
    rejected: List[str] = []
    for name in cycle_dirs(src, complete_only=False):
        sp = os.path.join(src, name)
        if not os.path.isfile(os.path.join(sp, COMPLETE)):
            skipped.append(f"{name}: no COMPLETE marker (not backed up)")
            continue
        if name in have:
            skipped.append(f"{name}: already backed up")
            continue
        if name not in recorded:
            failed.append(f"{name}: not recorded in {LOG_NAME}; not backed up")
            continue
        prob = verify_cycle_dir(sp)
        if prob:
            failed.append(f"{name}: the source fails its own manifest ({prob[0]}); not copied")
            continue
        want = sealed.get(name)
        if want is None:
            failed.append(f"{name}: NOT SEALED (run seal first); not backed up")
            continue
        dp = os.path.join(dest, name)
        if os.path.lexists(dp):
            raise SealError(f"{dp} already exists and has no receipt: refusing to overwrite or trust it")
        copy_fn(sp, dp)
        bad = verify_cycle_dir(dp) if os.path.isdir(dp) else ["the copy is missing"]
        if not bad and _file_sha(os.path.join(dp, "manifest.json")) != _file_sha(os.path.join(sp, "manifest.json")):
            bad = ["the copy's manifest differs from the source's"]
        d: Dict[str, Any] = {}
        if not bad:
            d = _describe_cycle(dest, name)
            if d["manifest_sha256"] != want.get("manifest_sha256") or d["tree_sha256"] != want.get("tree_sha256"):
                bad = ["the copy differs from the sealed value"]
        if bad:
            if os.path.lexists(dp):
                _aside(dp, ".REJECTED")
            rejected.append(f"{name}: {bad[0]}")
            continue
        EC.append(receipts, {"at_ms": now, "cycle": name, "manifest_sha256": d["manifest_sha256"], "tree_sha256": d["tree_sha256"], "files": len(d["files"]),
                             "bytes": sum(f["bytes"] for f in d["files"].values())})
        done.append({"cycle": name, "files": len(d["files"])})
    return {"ok": not rejected and not failed, "backed_up": done, "skipped": skipped + failed, "failed": failed, "rejected": rejected}


def restore_check(backup_dir: str, receipts: str, manifest: str) -> Dict[str, Any]:
    """See ``_restore_check``. Any unexpected error while reading damaged input is REPORTED as a failed check, never raised."""
    try:
        return _restore_check(backup_dir, receipts, manifest)
    except (KeyError, ValueError, TypeError, AttributeError, OSError, EC.ChainDesync) as exc:
        return {"ok": False, "problems": [f"restore check could not complete: {type(exc).__name__}: {str(exc)[:200]}"], "checked": 0}


def _restore_check(backup_dir: str, receipts: str, manifest: str) -> Dict[str, Any]:
    """A backup is not proven until this passes: every receipted copy exists, is self-consistent and hashes as receipted, AND every cycle the seal ``manifest`` holds has
    a receipt whose manifest and tree hashes equal the sealed ones (mirrors ``md_seal.restore_check``: a backup that silently omits a sealed cycle proves nothing)."""
    problems: List[str] = []
    if not os.path.isfile(receipts):
        return {"ok": False, "problems": ["no receipts"], "checked": 0}
    if not EC.verify(receipts).get("ok", False):
        problems.append("the receipts' own chain does not verify")
    if not os.path.isfile(manifest):
        problems.append("no seal manifest: nothing says which cycles the backup must hold")
    else:
        if not EC.verify(manifest).get("ok", False):
            problems.append("the seal manifest's own chain does not verify")
        latest = {r["cycle"]: r for r in _cycle_rows(receipts)}
        for name, want in sorted(_sealed_cycles(manifest).items()):
            got = latest.get(name)
            if got is None:
                problems.append(f"{name}: sealed but has NO receipt (never backed up)")
                continue
            for k in ("manifest_sha256", "tree_sha256"):
                if got.get(k) != want.get(k):
                    problems.append(f"{name}: the receipt's {k} differs from the sealed value")
    n = 0
    for r in _log_rows(receipts):
        name = r.get("cycle")
        n += 1
        if (not isinstance(name, str) or not name or name != os.path.basename(name) or name in (".", "..")
                or not isinstance(r.get("manifest_sha256"), str) or not isinstance(r.get("tree_sha256"), str)):
            problems.append(f"receipt row {n}: malformed (needs a plain cycle name and manifest_sha256/tree_sha256 text); a garbled receipt proves nothing")
            continue
        path = os.path.join(backup_dir, name)
        if not os.path.isdir(path):
            problems.append(f"{name}: receipted but missing from the backup")
            continue
        sub = verify_cycle_dir(path)
        if sub:
            problems += [f"{name}: {p}" for p in sub]
            continue
        d = _describe_cycle(backup_dir, name)
        if d["manifest_sha256"] != r["manifest_sha256"] or d["tree_sha256"] != r["tree_sha256"]:
            problems.append(f"{name}: hash differs from its receipt")
    return {"ok": not problems, "problems": problems, "checked": n}


# ---------------------------------------------------------------- owner-invoked repair of cycles.jsonl after a crash

def repair_log(out_root: str) -> Dict[str, Any]:
    """Owner-invoked, NEVER automatic: make ``cycles.jsonl`` appendable again after a crash between a row and its chain entry (UNCHAINED_TAIL) by using
    ``evidence_chain.repair``: the row is kept and marked ``resealed`` in the chain, a torn final line goes to ``cycles.jsonl.torn``. Refuses (changes nothing)
    when the file needs no repair or when anything before the tail fails to verify (that is tampering or loss, not a crash)."""
    root = os.path.abspath(out_root)
    with _locked(root):
        return _repair_locked(root)


def _repair_locked(root: str) -> Dict[str, Any]:
    log = os.path.join(root, LOG_NAME)
    if not os.path.isfile(log):
        raise CollectorError(f"{LOG_NAME} does not exist under {root}")
    if not EC.needs_repair(log):
        return {"ok": False, "reason": "nothing to repair: the log is already appendable", "verify_ok": bool(EC.verify(log).get("ok"))}
    before = EC.verify(log)
    serious = sorted({str(i["code"]) for i in before.get("issues", []) if i["code"] not in ("UNCHAINED_TAIL", "HEAD_MISMATCH", "HEAD_MISSING")})
    if serious:
        return {"ok": False, "reason": f"refusing to repair: the log has damage that is not a crash tail ({', '.join(serious)}); investigate, do not reseal", "issues": serious}
    res = EC.repair(log)
    after = EC.verify(log)
    return {"ok": bool(res.get("appendable")) and bool(after.get("ok")), **res, "resealed_indices": after.get("resealed_indices", []), "verify_ok": bool(after.get("ok"))}


# ---------------------------------------------------------------- data-root guard (shared with market_history)

def _in_git_tree(path: str) -> bool:
    """True when ``path`` (or its nearest existing ancestor, symlinks resolved) is inside a git work tree: a ``.git`` entry in it or any parent."""
    d = os.path.realpath(path)
    while not os.path.exists(d) and os.path.dirname(d) != d:
        d = os.path.dirname(d)
    while True:
        if os.path.lexists(os.path.join(d, ".git")):
            return True
        parent = os.path.dirname(d)
        if parent == d:
            return False
        d = parent


def root_guard(root: str, reserve_bytes: int, free_bytes_fn: Callable[[str], int] = lambda p: shutil.disk_usage(p).free) -> List[str]:
    """Reasons a data root may NOT be written (empty = fine): inside a git work tree (data would be committable), a link, group/world accessible, or free space under
    the reserve. Checked before every cycle / dataset so a filling disk stops the run instead of corrupting a cycle."""
    problems: List[str] = []
    r = os.path.abspath(root)
    if _in_git_tree(r):
        problems.append("the data root is inside a git work tree: market data must live outside Git")
    if os.path.islink(r):
        problems.append("the data root is a symbolic link")
    if os.path.isdir(r) and os.stat(r).st_mode & 0o077:
        problems.append(f"the data root mode is {oct(os.stat(r).st_mode & 0o777)}: it must be 0700")
    probe = r
    while not os.path.exists(probe) and os.path.dirname(probe) != probe:
        probe = os.path.dirname(probe)
    try:
        free = int(free_bytes_fn(probe))
    except OSError as exc:
        problems.append(f"free space cannot be read ({exc.strerror})")
    else:
        if free < reserve_bytes:
            problems.append(f"free space {free} bytes is below the reserve {reserve_bytes}")
    return problems


# ---------------------------------------------------------------- continuous collection (run-continuous) and its health report

HEALTH_NAME = "health.json"
HEALTH_SCHEMA = "market_collector_health/v1"
DEFAULT_MAX_CONSECUTIVE_FAILURES = 10
STOP_SLICE_S = 5.0


def _write_health(path: str, doc: Mapping[str, Any]) -> None:
    """Atomic replace, 0600. The health file is overwritten by design (a status, not evidence); it holds no credential material."""
    tmp = f"{path}.tmp{os.getpid()}"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        os.write(fd, (json.dumps(doc, indent=2, sort_keys=True, default=str, allow_nan=False) + "\n").encode())
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)


def run_continuous(out_root: str, *, interval_ms: int = DEFAULT_INTERVAL_MS, duration_ms: Optional[int] = None, until_ms: Optional[int] = None, stop_file: Optional[str] = None,
                   max_cycles: Optional[int] = None, fetch: Callable[[str], bytes] = http_fetch, clock: Callable[[], int] = lambda: int(time.time() * 1000),
                   sleep: Callable[[float], None] = time.sleep, max_symbols: int = DEFAULT_MAX_SYMBOLS, depth: int = DEFAULT_DEPTH, trades_limit: int = DEFAULT_TRADES,
                   reserve_bytes: int = DEFAULT_RESERVE_BYTES, max_bytes: int = DEFAULT_MAX_BYTES, free_bytes_fn: Callable[[str], int] = lambda p: shutil.disk_usage(p).free,
                   seal_manifest: Optional[str] = None, backup_to: Optional[str] = None, receipts: Optional[str] = None, seal_every: int = 60, backup_every: int = 60,
                   restore_check_every: int = 0, max_consecutive_failures: int = DEFAULT_MAX_CONSECUTIVE_FAILURES, health_path: Optional[str] = None,
                   max_retries: int = MAX_RETRIES, backoff_s: float = BACKOFF_S) -> Dict[str, Any]:
    """Repeat ``run_once`` every ``interval_ms`` until a duration/``until_ms``/``max_cycles`` limit, an explicit ``stop_file``, the disk guard, a refusal that needs the owner
    (broken log, held lock, storage budget) or ``max_consecutive_failures``. Restart-safe: every cycle is a new directory and ``run_once`` itself sets aside an interrupted one and
    adopts a COMPLETE-but-unrecorded one, so killing and restarting this loop loses at most the cycle in flight. Seal / backup / restore-check hooks run every N cycles when
    configured (through ``seal_cycles`` / ``backup_cycles`` / ``restore_check``); a failing hook is RECORDED in the health report, never swallowed and never fatal to collection.
    The health report (``health.json``, rewritten after every step) is machine readable; ``tools/data_freshness_audit.py`` reads it."""
    _check_ms("interval_ms", interval_ms)
    if not (duration_ms is None or _check_ms("duration_ms", duration_ms)) or not (until_ms is None or _check_ms("until_ms", until_ms)):
        raise CollectorError("bad duration/until")
    if max_cycles is not None and (isinstance(max_cycles, bool) or not isinstance(max_cycles, int) or max_cycles < 1):
        raise CollectorError("max_cycles must be a positive whole number")
    if duration_ms is None and until_ms is None and max_cycles is None and not stop_file:
        raise CollectorError("an unbounded run needs --stop-file (an explicit way to stop), or a duration/until/max-cycles")
    if (backup_to is None) != (receipts is None) or (backup_to is not None and seal_manifest is None):
        raise CollectorError("backup needs --backup-to, --receipts and --seal-manifest together")
    if restore_check_every and backup_to is None:
        raise CollectorError("restore-check needs the backup configured")
    root = os.path.abspath(out_root)
    hp = health_path or os.path.join(root, HEALTH_NAME)
    t0 = int(clock())
    end_ms = min(x for x in (until_ms, t0 + duration_ms if duration_ms else None) if x is not None) if (until_ms or duration_ms) else None
    h: Dict[str, Any] = {"schema": HEALTH_SCHEMA, "root": root, "interval_ms": interval_ms, "started_ms": t0, "status": "RUNNING", "stop_reason": None, "cycles_run": 0,
                         "cycles_ok": 0, "cycles_partial": 0, "last_cycle": None, "gaps": {"missed_expected_cycles_total": 0, "last": None}, "consecutive_failures": 0,
                         "max_consecutive_failures": max_consecutive_failures, "rate_limit_events_total": 0, "disk": None, "last_seal": None, "last_backup": None,
                         "last_restore_check": None, "refusal": None,
                         "hook_cadence_ms": {k: n * interval_ms for k, n in (("last_seal", seal_every if seal_manifest else 0), ("last_backup", backup_every if backup_to else 0),
                                                                         ("last_restore_check", restore_check_every if backup_to else 0)) if n and n > 0}}

    def _flush() -> None:
        h["written_ms"] = int(clock())
        if os.path.isdir(root):
            _write_health(hp, h)

    def _flush_quiet() -> None:
        try:
            _flush()
        except Exception:                             # the original error is the one to surface
            pass

    def _disk() -> List[str]:
        probs = root_guard(root, reserve_bytes, free_bytes_fn)
        h["disk"] = {"free_bytes": int(free_bytes_fn(root if os.path.isdir(root) else os.path.dirname(root))), "reserve_bytes": reserve_bytes,
                     "root_bytes": _tree_bytes(root) if os.path.isdir(root) else 0, "max_bytes": max_bytes, "in_git_tree": _in_git_tree(root), "problems": probs}
        return probs

    def _hook(name: str, fn: Callable[[], Dict[str, Any]]) -> None:
        try:
            res = fn()
            h[name] = {"at_ms": int(clock()), "ok": bool(res.get("ok", True)), "detail": {k: res[k] for k in res if k in ("ok", "problems", "sealed_new", "cycles_total", "backed_up",
                                                                                                                     "failed", "rejected", "checked")}}
        except Exception as exc:                          # ANY failure of a hook is recorded (a KeyError on damaged input must not end collection)
            h[name] = {"at_ms": int(clock()), "ok": False, "detail": {"error": f"{type(exc).__name__}: {exc}"[:300]}}

    def _stop(reason: str, status: str = "STOPPED") -> Dict[str, Any]:
        h["status"], h["stop_reason"] = status, reason
        _flush()
        return h

    def _loop() -> Dict[str, Any]:
        cycles = 0
        while True:
            if stop_file and os.path.lexists(stop_file):
                return _stop("stop_file")
            if max_cycles is not None and cycles >= max_cycles:
                return _stop("max_cycles")
            now = int(clock())
            if end_ms is not None and now >= end_ms:
                return _stop("duration")
            probs = _disk()
            if probs:
                h["refusal"] = {"code": "DISK_GUARD", "problems": probs}
                return _stop("disk_guard", "REFUSED")
            t_cycle = now
            try:
                res = run_once(root, t_cycle, fetch, clock, sleep=sleep, max_symbols=max_symbols, depth=depth, trades_limit=trades_limit, interval_ms=interval_ms,
                               reserve_bytes=reserve_bytes, max_bytes=max_bytes, free_bytes_fn=free_bytes_fn, max_retries=max_retries, backoff_s=backoff_s)
            except CollectorError as exc:
                res = {"status": "ABORTED", "reason": f"{type(exc).__name__}: {exc}"}
            cycles += 1
            h["cycles_run"] = cycles
            st = res.get("status")
            if st in ("REFUSED_STORAGE", "REFUSED_LOG", "REFUSED_LOCKED"):
                h["refusal"] = {"code": st, "reason": res.get("reason"), "detail": res.get("detail")}
                return _stop(st.lower(), "REFUSED")
            if st in ("OK", "PARTIAL"):
                h["cycles_ok" if st == "OK" else "cycles_partial"] += 1
                h["last_cycle"] = {"cycle": res["cycle"], "cycle_ms": t_cycle, "status": st, "counts": res["counts"], "partial_reasons": res["partial_reasons"],
                                   "finished_ms": int(clock())}
                h["rate_limit_events_total"] += int(res["counts"].get("rate_limit_events", 0))
                if res["gaps"]:
                    h["gaps"]["missed_expected_cycles_total"] += sum(int(g.get("missed_expected_cycles", 0)) for g in res["gaps"])
                    h["gaps"]["last"] = res["gaps"][-1]
                h["consecutive_failures"] = 0 if st == "OK" else h["consecutive_failures"] + 1
            else:                                            # ABORTED / REFUSED_CLOCK: nothing was recorded for this slot
                h["consecutive_failures"] += 1
                h["last_cycle"] = {"cycle": res.get("cycle"), "cycle_ms": t_cycle, "status": st, "reason": res.get("reason"), "finished_ms": int(clock())}
            if h["consecutive_failures"] >= max_consecutive_failures:
                h["refusal"] = {"code": "CONSECUTIVE_FAILURES", "count": h["consecutive_failures"]}
                return _stop("consecutive_failures", "REFUSED")
            if seal_manifest and seal_every > 0 and cycles % seal_every == 0:
                _hook("last_seal", lambda: seal_cycles(root, seal_manifest, now_ms=int(clock())))
            if backup_to and backup_every > 0 and cycles % backup_every == 0:
                _hook("last_backup", lambda: backup_cycles(root, backup_to, receipts, manifest=seal_manifest, now_ms=int(clock())))
            if backup_to and restore_check_every > 0 and cycles % restore_check_every == 0:
                _hook("last_restore_check", lambda: restore_check(backup_to, receipts, seal_manifest))
            _flush()
            # wait for the next slot (rate-limit-aware: a throttled cycle waits longer, capped), checking the stop file between short sleeps
            extra = min(BACKOFF_CAP_S, backoff_s * (2 ** min(h["consecutive_failures"], 5))) if (h["consecutive_failures"] or res.get("counts", {}).get("rate_limit_events")) else 0.0
            due = t_cycle + interval_ms + int(extra * 1000)
            while int(clock()) < due:
                if stop_file and os.path.lexists(stop_file):
                    break
                if end_ms is not None and int(clock()) >= end_ms:
                    break
                sleep(min(STOP_SLICE_S, max(0.0, (due - int(clock())) / 1000.0)) or 0.001)

    try:
        return _loop()
    except KeyboardInterrupt:
        h["status"], h["stop_reason"] = "STOPPED", "interrupted"
        _flush_quiet()
        raise
    except BaseException as exc:                      # the final health state is never left at RUNNING after an abnormal exit
        h["status"], h["stop_reason"] = "CRASHED", f"{type(exc).__name__}: {exc}"[:300]
        _flush_quiet()
        raise


# ---------------------------------------------------------------- CLI

def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Owner-invoked PUBLIC-data market collector (one capture cycle) with seal/check/backup/restore-check.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run-once")
    r.add_argument("--out-root", required=True)
    r.add_argument("--max-symbols", type=int, default=DEFAULT_MAX_SYMBOLS)
    r.add_argument("--depth", type=int, default=DEFAULT_DEPTH)
    r.add_argument("--trades-limit", type=int, default=DEFAULT_TRADES)
    r.add_argument("--interval-ms", type=int, default=DEFAULT_INTERVAL_MS)
    r.add_argument("--reserve-bytes", type=int, default=DEFAULT_RESERVE_BYTES)
    r.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES)
    cn = sub.add_parser("run-continuous", help="repeat run-once on an interval until a limit or an explicit stop file; health.json is rewritten every step")
    cn.add_argument("--out-root", required=True, help="data root OUTSIDE the git work tree (created 0700)")
    cn.add_argument("--interval-ms", type=int, default=DEFAULT_INTERVAL_MS)
    cn.add_argument("--duration-s", type=int, help="stop after this many seconds")
    cn.add_argument("--until-ms", type=int, help="stop at this epoch ms")
    cn.add_argument("--stop-file", help="the run stops cleanly when this path exists (touch it)")
    cn.add_argument("--max-cycles", type=int)
    cn.add_argument("--max-symbols", type=int, default=DEFAULT_MAX_SYMBOLS)
    cn.add_argument("--depth", type=int, default=DEFAULT_DEPTH)
    cn.add_argument("--trades-limit", type=int, default=DEFAULT_TRADES)
    cn.add_argument("--reserve-bytes", type=int, default=DEFAULT_RESERVE_BYTES)
    cn.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES)
    cn.add_argument("--seal-manifest"); cn.add_argument("--seal-every", type=int, default=60)
    cn.add_argument("--backup-to"); cn.add_argument("--receipts"); cn.add_argument("--backup-every", type=int, default=60)
    cn.add_argument("--restore-check-every", type=int, default=0)
    cn.add_argument("--max-consecutive-failures", type=int, default=DEFAULT_MAX_CONSECUTIVE_FAILURES)
    cn.add_argument("--health", help="health report path (default <out-root>/health.json)")
    s = sub.add_parser("seal"); s.add_argument("--root", required=True); s.add_argument("--manifest", required=True)
    s.add_argument("--accept-problems", action="store_true")
    c = sub.add_parser("check"); c.add_argument("--root", required=True); c.add_argument("--manifest", required=True)
    b = sub.add_parser("backup"); b.add_argument("--root", required=True); b.add_argument("--to", required=True); b.add_argument("--receipts", required=True)
    b.add_argument("--manifest", required=True, help="the seal manifest the root is checked against first")
    k = sub.add_parser("restore-check"); k.add_argument("--backup", required=True); k.add_argument("--receipts", required=True)
    k.add_argument("--manifest", required=True, help="the seal manifest: every sealed cycle needs a matching receipt")
    g = sub.add_parser("repair-log", help="owner-invoked: reseal a crash's UNCHAINED_TAIL in cycles.jsonl (kept and marked resealed; never automatic)")
    g.add_argument("--out-root", required=True)
    a = ap.parse_args(argv)
    try:
        if a.cmd == "run-once":
            res = run_once(a.out_root, int(time.time() * 1000), max_symbols=a.max_symbols, depth=a.depth, trades_limit=a.trades_limit,
                           interval_ms=a.interval_ms, reserve_bytes=a.reserve_bytes, max_bytes=a.max_bytes)
            ok = res["status"] in ("OK", "PARTIAL")
            res = {**res, "ok": ok}
        elif a.cmd == "run-continuous":
            res = run_continuous(a.out_root, interval_ms=a.interval_ms, duration_ms=a.duration_s * 1000 if a.duration_s else None, until_ms=a.until_ms, stop_file=a.stop_file,
                                 max_cycles=a.max_cycles, max_symbols=a.max_symbols, depth=a.depth, trades_limit=a.trades_limit, reserve_bytes=a.reserve_bytes,
                                 max_bytes=a.max_bytes, seal_manifest=a.seal_manifest, seal_every=a.seal_every, backup_to=a.backup_to, receipts=a.receipts,
                                 backup_every=a.backup_every, restore_check_every=a.restore_check_every, max_consecutive_failures=a.max_consecutive_failures,
                                 health_path=a.health)
            res = {**res, "ok": res["status"] == "STOPPED"}
        elif a.cmd == "seal":
            res = seal_cycles(a.root, a.manifest, accept_problems=a.accept_problems)
        elif a.cmd == "check":
            res = check_cycles(a.root, a.manifest)
        elif a.cmd == "backup":
            res = backup_cycles(a.root, a.to, a.receipts, manifest=a.manifest)
        elif a.cmd == "repair-log":
            res = repair_log(a.out_root)
        else:
            res = restore_check(a.backup, a.receipts, a.manifest)
    except (CollectorError, UC.CaptureError, UC.AR.RecordError, OSError, EC.ChainDesync) as exc:
        print(f"market_collector: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # a hand-made or damaged input must end as a clean refusal, never a traceback
        print(f"market_collector: unexpected {type(exc).__name__}: {str(exc)[:200]}", file=sys.stderr)
        return 2
    print(json.dumps(res, indent=2, sort_keys=True, default=str))
    return 0 if res.get("ok", True) else 1


if __name__ == "__main__":
    sys.exit(main())
