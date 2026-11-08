"""Owner-invoked, PUBLIC-data-only, GET-only HISTORY ingestion for the Bybit USDT-linear market: funding, trade-price klines, mark-price klines, index-price klines and
open interest, per (dataset, symbol), into raw hash-listed pages plus a verifiable ``market_history/v1`` manifest.

Why. Research needs settled history for the WHOLE eligible universe, with proof of what was fetched, what is missing and when each fact became knowable. This module
only fetches, preserves, validates and reports. It never fills a gap, never interpolates, never selects by performance and never infers a grid it was not told.

    GET <mainnet REST host from bot/venues.py>/v5/market/funding/history      category,symbol,startTime,endTime,limit<=200   (startTime WITHOUT endTime is an error: both are ALWAYS sent)
    GET .../v5/market/kline | mark-price-kline | index-price-kline            category,symbol,interval,start,end,limit<=1000 (list is newest-first)
    GET .../v5/market/open-interest                                          category,symbol,intervalTime,startTime,endTime,limit<=200 (intervalTime REQUIRED:
                                                                             5min,15min,30min,1h,4h,1d; list newest-first)
(limits and rules: https://bybit-exchange.github.io/docs/v5/market/history-fund-rate, /kline, /mark-kline, /index-kline, /open-interest, checked 2026-11.)
Paging is BACKWARD: the next ``endTime`` is ``min(row timestamp of the page) - 1`` until the window start. A short page never ends the run; only an EMPTY page (nothing
older exists) or the start does. The cursor MUST move strictly backwards (else the symbol stops, recorded) and a page cap bounds every symbol.

What it is NOT. No credential, no signature, no order/position/account path, no POST. The only network primitive is ``market_collector.http_request_to`` (the
``public_collector`` role behind ``CapabilityTransport``; this module imports no HTTP library). The agent that wrote it has not run it against an exchange (the sandbox is
geo-blocked); tests inject ``fetch``, ``clock``, ``sleep``. The owner runs it.

Layout (data root OUTSIDE git, root 0700, files 0600, a page is created exclusively and never overwritten):
    <root>/<dataset>[-<interval>]/manifest.json               schema ``market_history/v1`` (atomic replace; ``manifest_digest`` seals its own content)
    <root>/<dataset>[-<interval>]/<SYMBOL>/page-000001.json   RAW response bytes exactly as received
    <root>/<dataset>[-<interval>]/<SYMBOL>/pages.jsonl        append-only ledger: begin row, one row per page (params, server time, recv_ms, collection_ms, sha256),
                                                              stop row. It is the RESUME CHECKPOINT: a crash resumes from the last page whose file re-hashes to its row.
    <root>/<dataset>[-<interval>]/<SYMBOL>/rows.jsonl         DERIVED (re-derivable from the pages by ``verify``): unique rows ascending, each with its observation_time
    <root>/<dataset>[-<interval>]/<SYMBOL>/quarantine/<k>/    corrupt or orphaned pages moved aside (never deleted); their rows are NOT counted

dataset_status (per symbol and for the dataset = the worst symbol; precedence CONFLICTED > UNVERIFIED > PARTIAL > STALE > COMPLETE):
  COMPLETE    every page verified, no reject/conflict/skew flag, the declared grid has no gap, the head reaches the window start (or the symbol's launch time) and the
              newest row is within two intervals of the window end.
  STALE       as COMPLETE but the newest row is older than two intervals before the window end (the feed stopped, or the window end was in the future of the data).
  PARTIAL     a known hole or an incomplete run: a gap, an off-grid step (an interval change), a rejected row/page, a rate-limit exhaustion, a stalled cursor, the page
              cap, an HTTP error, a clock-skew flag, an unclosed candle (ts + interval beyond the server's own time: dropped, listed as unclosed_candles), an unproven_regime_change (an interval change nobody corroborated with --accept-regime-change: it may be a missing head), regime flapping (an interval that comes back non-adjacently), a head that cannot be proved (no launch time and the venue ran out of rows), or no rows at all.
  CONFLICTED  one timestamp carries two different rows (identical repeats are counted and dropped; conflicting ones are never merged).
  UNVERIFIED  the evidence cannot be trusted or the grid cannot be judged: a page failed its hash, the ledger is damaged, the resume parameters differ, or the interval
              is UNKNOWN (funding without a universe record: the grid check is REFUSED and reported, never defaulted to 480).

THE SEVEN TIMESTAMPS (all epoch ms; one definition, tested in ``check_time_invariants``):
  exchange_event_time  when the exchange says the fact happened: funding = fundingRateTimestamp, kline/mark/index = candle start, open interest = sample timestamp.
  source_server_time   the envelope ``time`` of the response that carried it (the exchange's clock when it answered).
  local_receive_time   our clock when the response bytes arrived (``recv_ms`` in the ledger). The clock-skew check compares it with source_server_time.
  collection_time      our clock when the raw page was durably written (>= local_receive_time).
  normalization_time   our clock when ``rows.jsonl`` was derived (>= collection_time); stored once per symbol in the manifest.
  observation_time     the EARLIEST instant the value can be used without look-ahead: funding = exchange_event_time (the settled print); open interest = its timestamp;
                       klines = candle start + interval (the candle is only final at its close). Always >= exchange_event_time. Stored per row.
  decision_time        the instant a consumer decides. It is supplied by the CONSUMER, never stored here. A row is usable only at decision_time >= observation_time, and
                       decision_time is never earlier than local_receive_time (what we had not yet received cannot have driven a decision): ``usable_at`` checks both. It is provided as a
                       LIBRARY check; enforced where a consumer calls it (the ranked-funding evaluator is the only verdict-path consumer), not by the stored rows themselves.
                       ``mode='replay'`` relaxes ONLY the second rule for a historical backfill and says so; a research claim must name the mode it used.
Ordering: exchange_event_time <= observation_time;  local_receive_time <= collection_time <= normalization_time;  source_server_time >= exchange_event_time - skew.
Per-row page-level times (server/receive/collection) are in the ledger, joined by the row's ``page``.

Symbol source. ``--symbols`` (explicit), ``--universe-capture`` (+ ``--include-statuses``) or ``--symbols-from-plan PLAN.json``: the sealed symbol list (and window) of a
``tools/backfill_plan.py plan --out`` document (its ``plan_digest`` must re-hash; the universe capture must be the one it was made from; the window must equal the plan's). Every dataset
collected with the same plan has the same symbols.

Network note: ``urllib`` honours HTTPS_PROXY / https_proxy / NO_PROXY from the environment, so a collector run behind a proxy sends its (unsigned, public) requests through it and that
proxy sees them; the pinned host and path allow-list is enforced on the URL before the request, not on the route. Unset the variables if that is not what you want.

What it cannot show: that the bytes came from the exchange (a hash proves what was read, not who produced it); the funding interval of the PAST (the universe record gives
the current one; a change shows up as a regime change only when it is a sustained run of >= 3 equal spacings that divides a day and is not a whole multiple of the interval before it; runs of whole-multiple spacings are GAPS, never regimes); that nothing is missing before a symbol's recorded launch time.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

BOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BOT)
sys.path.insert(0, os.path.join(BOT, "tools"))

import market_collector as MC  # noqa: E402  (the one transport: public_collector role, GET only)
import universe_capture as UC  # noqa: E402
import venues  # noqa: E402

SCHEMA = "market_history/v1"
CATEGORY = "linear"
MAX_BODY_BYTES = 8 << 20
MAX_MS = 10 ** 14
SKEW_WARN_MS = UC.SKEW_WARN_MS
DEFAULT_WORKERS = 2
MAX_WORKERS = 8
DEFAULT_RATE = 5.0                                   # requests per second, shared by every worker (Bybit's public limit is far higher; this is polite)
DEFAULT_MAX_PAGES = 20_000
STALE_INTERVALS = 2
CLOSED_SAFETY_MS = 60_000                            # a default window end stays this far behind the clock so a candle the venue has not closed yet is never requested
FUNDING, KLINE, MARK, INDEX, OI = "funding", "kline", "mark-kline", "index-kline", "open-interest"
DATASETS: Dict[str, Dict[str, Any]] = {
    FUNDING: {"path": "/v5/market/funding/history", "limit": 200, "start": "startTime", "end": "endTime", "interval_param": None},
    KLINE: {"path": "/v5/market/kline", "limit": 1000, "start": "start", "end": "end", "interval_param": "interval", "fields": 7},
    MARK: {"path": "/v5/market/mark-price-kline", "limit": 1000, "start": "start", "end": "end", "interval_param": "interval", "fields": 5},
    INDEX: {"path": "/v5/market/index-price-kline", "limit": 1000, "start": "start", "end": "end", "interval_param": "interval", "fields": 5},
    OI: {"path": "/v5/market/open-interest", "limit": 200, "start": "startTime", "end": "endTime", "interval_param": "intervalTime"},
}
HISTORY_PATHS = tuple(d["path"] for d in DATASETS.values())
KLINE_INTERVALS_MS = {"1": 60_000, "3": 180_000, "5": 300_000, "15": 900_000, "30": 1_800_000, "60": 3_600_000, "120": 7_200_000, "240": 14_400_000,
                      "360": 21_600_000, "720": 43_200_000, "D": 86_400_000, "W": 604_800_000}      # "M" (calendar month) has no fixed grid: refused
OI_INTERVALS_MS = {"5min": 300_000, "15min": 900_000, "30min": 1_800_000, "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}
TIME_FIELDS = ("exchange_event_time", "source_server_time", "local_receive_time", "collection_time", "normalization_time", "observation_time", "decision_time")
SELECTION_MODES = ("EXPLICIT_SYMBOLS", "FROM_PLAN", "UNIVERSE")            # how a dataset's symbol set was chosen (manifest ``selection.mode``); a manifest without the block is UNKNOWN_LEGACY
STATUS_ORDER = ("CONFLICTED", "UNVERIFIED", "PARTIAL", "STALE", "COMPLETE")

_SYMBOL = re.compile(r"^[A-Z0-9]{2,24}\Z", re.ASCII)
_DEC = re.compile(r"^[0-9]+(\.[0-9]+)?\Z", re.ASCII)
_SDEC = re.compile(r"^-?[0-9]+(\.[0-9]+)?\Z", re.ASCII)
_UINT = re.compile(r"^[0-9]{1,19}\Z", re.ASCII)
_HEX = re.compile(r"^[0-9a-f]{64}\Z", re.ASCII)
_CURSOR = re.compile(r"^[A-Za-z0-9%._~=+/:,-]{1,512}\Z", re.ASCII)


class HistoryError(ValueError):
    """The run (or one symbol) cannot continue safely."""


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _canon(o: Any) -> bytes:
    return (json.dumps(o, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")


# ---------------------------------------------------------------- time semantics

def check_time_invariants(t: Mapping[str, Any]) -> List[str]:
    """Problems (empty = consistent) in one observation's timestamps. ``decision_time`` is optional (the consumer's)."""
    p: List[str] = []
    need = [k for k in TIME_FIELDS if k != "decision_time"]
    for k in need:
        v = t.get(k)
        if isinstance(v, bool) or not isinstance(v, int) or v <= 0:
            p.append(f"{k} missing or not a positive integer")
    if p:
        return p
    if t["observation_time"] < t["exchange_event_time"]:
        p.append("observation_time is earlier than exchange_event_time")
    if not t["local_receive_time"] <= t["collection_time"] <= t["normalization_time"]:
        p.append("local_receive_time <= collection_time <= normalization_time is violated")
    if t["source_server_time"] < t["exchange_event_time"] - SKEW_WARN_MS:
        p.append("source_server_time is before exchange_event_time by more than the skew tolerance (a row from the future)")
    d = t.get("decision_time")
    if d is not None:
        if isinstance(d, bool) or not isinstance(d, int) or d <= 0:
            p.append("decision_time is not a positive integer")
        else:
            if d < t["observation_time"]:
                p.append("decision_time is earlier than observation_time (look-ahead)")
            if d < t["local_receive_time"]:
                p.append("decision_time is earlier than local_receive_time (not yet received)")
    return p


def usable_at(times: Mapping[str, Any], decision_time: int, mode: str = "live") -> bool:
    """May a decision at ``decision_time`` use this observation? ``live`` (default): decision >= observation_time AND >= local_receive_time.
    ``replay``: decision >= observation_time only, for a HISTORICAL BACKFILL replayed as if received on time; the caller must state that mode with its result."""
    if mode not in ("live", "replay"):
        raise HistoryError("mode must be 'live' or 'replay'")
    obs, recv = times.get("observation_time"), times.get("local_receive_time")
    if not isinstance(obs, int) or isinstance(obs, bool) or not isinstance(decision_time, int) or isinstance(decision_time, bool):
        return False
    if decision_time < obs:
        return False
    if mode == "live":
        return isinstance(recv, int) and not isinstance(recv, bool) and decision_time >= recv
    return True


# ---------------------------------------------------------------- request

def _check_ms(name: str, v: Any) -> int:
    if isinstance(v, bool) or not isinstance(v, int) or not 0 < v < MAX_MS:
        raise HistoryError(f"{name} must be a positive whole number below {MAX_MS}")
    return v


def interval_ms_of(dataset: str, interval: Optional[str]) -> Optional[int]:
    if dataset == FUNDING:
        return None
    table = OI_INTERVALS_MS if dataset == OI else KLINE_INTERVALS_MS
    if interval not in table:
        raise HistoryError(f"{dataset}: interval {interval!r} is not one of {sorted(table)}")
    return table[interval]


def request_url(dataset: str, symbol: str, interval: Optional[str], start_ms: int, end_ms: int, limit: Optional[int] = None, cursor: Optional[str] = None) -> str:
    """Constants + one validated symbol/interval + bounded integers. ``startTime`` and ``endTime`` are ALWAYS both sent."""
    d = DATASETS.get(dataset)
    if d is None:
        raise HistoryError(f"unknown dataset {dataset!r}")
    if not isinstance(symbol, str) or not _SYMBOL.fullmatch(symbol):
        raise HistoryError("refusing a symbol that is not a short upper-case token")
    interval_ms_of(dataset, interval)
    _check_ms("start_ms", start_ms)
    _check_ms("end_ms", end_ms)
    if start_ms > end_ms:
        raise HistoryError("start_ms is after end_ms")
    lim = d["limit"] if limit is None else limit
    if isinstance(lim, bool) or not isinstance(lim, int) or not 1 <= lim <= d["limit"]:
        raise HistoryError(f"limit must be a whole number in 1..{d['limit']} for {dataset}")
    q: List[Tuple[str, Any]] = [("category", CATEGORY), ("symbol", symbol)]
    if d["interval_param"]:
        q.append((d["interval_param"], interval))
    q += [(d["start"], start_ms), (d["end"], end_ms), ("limit", lim)]
    base = venues.rest_for("mainnet")
    if not base.startswith("https://"):
        raise HistoryError("the registry's host is not https")
    url = f"{base}{d['path']}?{urllib.parse.urlencode(q)}"
    if cursor:
        # The venue's nextPageCursor arrives ALREADY percent-encoded (e.g. 'first%3D10000000AIDOGEUS'). It is appended VERBATIM: urlencode would turn '%3D' into '%253D' and
        # the venue would reject or mis-page. Only the unreserved/percent character set is accepted (no '&', '#', space, control).
        if dataset != OI or not _CURSOR.fullmatch(cursor):
            raise HistoryError("a cursor is accepted only for open-interest and only as a short percent-encoded token")
        url += "&cursor=" + cursor
    return url


def http_fetch(url: str) -> bytes:
    """One unauthenticated GET through ``market_collector.http_request_to`` (so through the public_collector role). Tests never reach it."""
    return MC.http_request_to("GET", url, HISTORY_PATHS + (UC.PATH_INSTRUMENTS,), headers={"Accept": "application/json", "User-Agent": "market-history/1"})


# ---------------------------------------------------------------- rate limiting

class TokenBucket:
    """Shared by every worker. ``clock`` (seconds, monotonic) and ``sleep`` are injected so tests never wait."""

    def __init__(self, rate: float, burst: int, clock: Callable[[], float], sleep: Callable[[float], None]) -> None:
        if not rate > 0 or burst < 1:
            raise HistoryError("rate must be > 0 and burst >= 1")
        self.rate, self.burst, self.clock, self.sleep = float(rate), float(burst), clock, sleep
        self.tokens = float(burst)
        self.last = clock()
        self.blocked_until = 0.0
        self.waits = 0
        self._lock = threading.Lock()

    def acquire(self) -> None:
        while True:
            with self._lock:
                now = self.clock()
                self.tokens = min(self.burst, self.tokens + max(0.0, now - self.last) * self.rate)
                self.last = now
                wait = max(0.0, self.blocked_until - now)
                if wait == 0.0:
                    if self.tokens >= 1.0 - 1e-9:                    # tolerance: float error must never leave a waiter spinning on a vanishing wait
                        self.tokens = max(0.0, self.tokens - 1.0)
                        return
                    wait = (1.0 - self.tokens) / self.rate
                self.waits += 1
            self.sleep(wait)

    def penalize(self, seconds: float) -> None:
        """A 429 / retCode 10006 was seen: every worker waits (shared back-off), not just the one that was throttled."""
        with self._lock:
            self.blocked_until = max(self.blocked_until, self.clock() + max(0.0, seconds))


# ---------------------------------------------------------------- row validation

def _uint_ts(v: Any) -> Optional[int]:
    if isinstance(v, str) and _UINT.fullmatch(v):
        n = int(v)
    elif isinstance(v, int) and not isinstance(v, bool):
        n = v
    else:
        return None
    return n if 0 < n < MAX_MS else None


def _dec(v: Any, *, signed: bool = False, positive: bool = False) -> Optional[str]:
    if not isinstance(v, str) or len(v) > 64 or not (_SDEC if signed else _DEC).fullmatch(v):
        return None
    try:
        d = Decimal(v)
    except InvalidOperation:
        return None
    if not d.is_finite() or (positive and not d > 0):
        return None
    return v


def _bump(r: Dict[str, int], k: str, n: int = 1) -> None:
    r[k] = r.get(k, 0) + n


def parse_page(dataset: str, symbol: str, body: bytes, win_start: int, cursor_end: int, interval_ms: Optional[int] = None) -> Dict[str, Any]:
    """Strict parse of one raw page. ``rows`` are validated rows (dicts with ``ts``); ``raw_ts`` every parseable timestamp (drives the cursor); counted rejects, never silent.
    ``unclosed`` lists the timestamps of kline/mark/index/open-interest rows whose interval had NOT ended at the server's own time (ts + interval > server time): they are not final rows
    and are not returned in ``rows`` (they still move the cursor)."""
    out: Dict[str, Any] = {"ok": False, "rows": [], "raw_count": 0, "raw_ts": [], "rejects": {}, "server_time": None, "descending": True, "unclosed": []}
    rej = out["rejects"]
    try:
        doc = json.loads(body.decode("utf-8", "strict"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        _bump(rej, "envelope:not_utf8_json")
        return out
    if not isinstance(doc, dict):
        _bump(rej, "envelope:not_an_object")
        return out
    out["server_time"] = _uint_ts(doc.get("time"))
    rc = doc.get("retCode")
    if isinstance(rc, bool) or rc != 0:
        _bump(rej, f"envelope:retCode_{rc if isinstance(rc, int) and not isinstance(rc, bool) else 'bad'}")
        return out
    res = doc.get("result")
    if not isinstance(res, dict):
        _bump(rej, "envelope:no_result")
        return out
    if res.get("category") != CATEGORY:
        _bump(rej, "envelope:wrong_category")
        return out
    if dataset != FUNDING and res.get("symbol") != symbol:
        _bump(rej, "envelope:wrong_symbol")
        return out
    rows = res.get("list")
    if not isinstance(rows, list):
        _bump(rej, "envelope:list_not_a_list")
        return out
    out["raw_count"] = len(rows)
    out["ok"] = True
    nxc = res.get("nextPageCursor")
    out["next_page_cursor"] = nxc if isinstance(nxc, str) and _CURSOR.fullmatch(nxc) else None     # recorded verbatim; paging itself moves by endTime (strictly guarded)
    stime = out["server_time"]
    fields = DATASETS[dataset].get("fields")
    seen: List[int] = []
    for r in rows:
        row: Optional[Dict[str, Any]] = None
        if dataset == FUNDING:
            if isinstance(r, dict) and r.get("symbol") == symbol:
                ts, rate = _uint_ts(r.get("fundingRateTimestamp")), _dec(r.get("fundingRate"), signed=True)
                if ts is not None and rate is not None:
                    row = {"ts": ts, "fundingRate": rate}
                elif ts is not None:
                    out["raw_ts"].append(ts)
            elif isinstance(r, dict) and r.get("symbol") != symbol:
                _bump(rej, "wrong_symbol")
                continue
        elif dataset == OI:
            if isinstance(r, dict):
                ts, oi = _uint_ts(r.get("timestamp")), _dec(r.get("openInterest"))
                single = r.get("singleOpenInterest")
                s1 = _dec(single) if single is not None else None
                if ts is not None and oi is not None and (single is None or s1 is not None):
                    row = {"ts": ts, "openInterest": oi}
                    if s1 is not None:
                        row["singleOpenInterest"] = s1
                elif ts is not None:
                    out["raw_ts"].append(ts)
        else:
            if isinstance(r, list) and len(r) == fields:
                ts = _uint_ts(r[0])
                px = [_dec(x, positive=True) for x in r[1:5]]
                extra = [_dec(x) for x in r[5:]]
                if ts is not None and None not in px and None not in extra:
                    o, h, l, c = (Decimal(x) for x in px)
                    if l <= min(o, c) and h >= max(o, c) and l <= h:
                        row = {"ts": ts, "open": px[0], "high": px[1], "low": px[2], "close": px[3]}
                        if fields == 7:
                            row.update(volume=extra[0], turnover=extra[1])
                    else:
                        _bump(rej, "ohlc_inconsistent")
                        out["raw_ts"].append(ts)
                        continue
                elif ts is not None:
                    out["raw_ts"].append(ts)
        if row is None:
            _bump(rej, "bad_row")
            continue
        out["raw_ts"].append(row["ts"])
        if not win_start <= row["ts"] <= cursor_end:
            _bump(rej, "out_of_window")
            continue
        if stime is not None and row["ts"] > stime + SKEW_WARN_MS:
            _bump(rej, "row_in_future_of_server_time")
            continue
        if stime is not None and interval_ms and dataset != FUNDING and row["ts"] + interval_ms > stime:
            out["unclosed"].append(row["ts"])
            continue
        seen.append(row["ts"])
        out["rows"].append(row)
    if dataset != FUNDING and any(a <= b for a, b in zip(seen, seen[1:])):
        out["descending"] = False
        _bump(rej, "unsorted_page")
    return out


# ---------------------------------------------------------------- ledger (the resume checkpoint)

def _page_name(n: int) -> str:
    return f"page-{n:06d}.json"


def _append_line(path: str, obj: Mapping[str, Any]) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        data = (json.dumps(obj, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode()
        view = memoryview(data)
        while view:
            n = os.write(fd, view)
            if n <= 0:
                raise HistoryError("ledger write made no progress")
            view = view[n:]
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_atomic(path: str, data: bytes) -> None:
    UC.AR._refuse_protected(path)
    tmp = f"{path}.tmp{os.getpid()}"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view):]
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)


def _is_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _ledger_row_problem(r: Mapping[str, Any], line: int, before: Sequence[Mapping[str, Any]]) -> Optional[str]:
    """Shape of one ledger row, so a hand-edited or garbled line is REPORTED (and quarantined by resume) instead of raising KeyError/ValueError/TypeError later."""
    t = r.get("type")
    if t == "begin":
        return None if line == 1 else "is a second begin row"
    if line == 1:
        return "is not a begin row"
    if t == "stop":
        return None if isinstance(r.get("reason"), str) else "has a non-text stop reason"
    prev_n = max((x["n"] for x in before if x.get("type") == "page"), default=0)
    if not _is_int(r.get("n")) or r["n"] != prev_n + 1:
        return "has a page number out of sequence"
    if not isinstance(r.get("file"), str) or not isinstance(r.get("sha256"), str) or not _is_int(r.get("bytes")):
        return "has a malformed file/sha256/bytes"
    req = r.get("request", {})
    if not isinstance(req, dict) or not isinstance(req.get("params", {}), dict):
        return "has a malformed request"
    if not (r.get("next_end") is None or _is_int(r.get("next_end"))):
        return "has a non-integer next_end"
    params = req.get("params", {})
    for d in DATASETS.values():
        if d["end"] in params:
            try:
                int(params[d["end"]])
            except (TypeError, ValueError):
                return "has a non-integer end parameter"
    return None


def read_ledger(sdir: str) -> Tuple[List[Dict[str, Any]], List[str]]:
    """(rows up to the first undecodable line, problems). A torn final line (a crash mid-write) is a problem the resume repairs; it never raises."""
    path = os.path.join(sdir, "pages.jsonl")
    rows: List[Dict[str, Any]] = []
    problems: List[str] = []
    if not os.path.isfile(path):
        return rows, problems
    with open(path, "rb") as fh:
        raw = fh.read()
    lines = raw.split(b"\n")
    if lines and lines[-1] == b"":
        lines.pop()
    elif lines:
        problems.append("LEDGER_TORN_TAIL: the last ledger line has no newline")
        lines.pop()
    for i, ln in enumerate(lines, 1):
        try:
            r = json.loads(ln.decode("utf-8", "strict"))
        except (UnicodeDecodeError, ValueError):
            problems.append(f"LEDGER_BAD_LINE: line {i} is not JSON; later lines are not trusted")
            break
        if not isinstance(r, dict) or r.get("type") not in ("begin", "page", "stop"):
            problems.append(f"LEDGER_BAD_LINE: line {i} is not a ledger row")
            break
        why = _ledger_row_problem(r, i, rows)
        if why:
            problems.append(f"LEDGER_BAD_LINE: line {i} {why}; later lines are not trusted")
            break
        rows.append(r)
    return rows, problems


def _verify_pages(sdir: str, rows: Sequence[Mapping[str, Any]]) -> Tuple[int, List[str]]:
    """(number of leading ledger rows whose page file re-hashes, problems for the first bad one)."""
    good = 0
    for i, r in enumerate(rows):
        if r["type"] != "page":
            good = i + 1
            continue
        p = os.path.join(sdir, str(r.get("file")))
        if r.get("file") != _page_name(int(r.get("n", 0))) or not os.path.isfile(p) or os.path.islink(p):
            return good, [f"PAGE_MISSING: {r.get('file')}"]
        with open(p, "rb") as fh:
            data = fh.read()
        if len(data) != r.get("bytes") or _sha(data) != r.get("sha256"):
            return good, [f"PAGE_HASH_MISMATCH: {r.get('file')}"]
        good = i + 1
    return good, []


# ---------------------------------------------------------------- analysis (shared by collect and verify)

def grid_report(ts_sorted: Sequence[int], interval_ms: Optional[int]) -> Dict[str, Any]:
    """Gap detection against a DECLARED grid. Reported, never filled. ``interval_ms`` None => refused (the caller says UNKNOWN)."""
    if interval_ms is None:
        return {"grid_check": "REFUSED_UNKNOWN_INTERVAL", "expected": None, "missing": None, "gaps": [], "gap_count": None, "off_grid": [], "off_grid_count": 0}
    gaps: List[Dict[str, int]] = []
    off: List[Dict[str, int]] = []
    missing = 0
    off_n = gap_n = 0
    for a, b in zip(ts_sorted, ts_sorted[1:]):
        d = b - a
        if d == interval_ms:
            continue
        if d % interval_ms:
            off_n += 1
            if len(off) < 50:
                off.append({"after_ts": a, "next_ts": b, "step_ms": d})
            continue
        m = d // interval_ms - 1
        missing += m
        gap_n += 1
        if len(gaps) < 100:
            gaps.append({"after_ts": a, "next_ts": b, "missing": m})
    expected = ((ts_sorted[-1] - ts_sorted[0]) // interval_ms + 1) if ts_sorted else 0
    return {"grid_check": "OK", "expected": expected, "missing": missing, "gaps": gaps, "gap_count": gap_n, "off_grid": off, "off_grid_count": off_n}


MIN_REGIME_RUN = 3


_DAY_MS = 86_400_000


def _accepted_regime_runs(runs: Sequence[Sequence[int]], declared_ms: Optional[int]) -> List[int]:
    """Indices of the runs that may be a REGIME. A run of >= MIN_REGIME_RUN equal spacings is only a candidate: its interval must divide a day (funding settles on a day grid:
    960 minutes never does), and a run whose spacing is a WHOLE MULTIPLE (> 1) of the interval before it is a run of missing prints (3 x 16h or 3 x 24h inside an 8h grid), not a
    new regime, UNLESS the instrument record itself declares that interval.
    Rejected runs are judged as gaps / off-grid steps by the caller."""
    acc: List[int] = []
    prev: Optional[int] = None
    cands = [k for k, (v, cnt, _i0) in enumerate(runs) if cnt >= MIN_REGIME_RUN and v > 0 and not _DAY_MS % v]
    for pos, k in enumerate(cands):
        v = runs[k][0]
        if prev is None:
            # The HEAD run has nothing before it to compare with. A head run that is a whole multiple (> 1) of the declared interval and is followed directly by a run AT the declared
            # interval is missing prints (3 x 24h, then the 8h grid), not a regime. Only a NON-power-of-two multiple counts: real funding intervals (480/240/120/60) change by powers of two,
            # so a genuine direct 480 -> 240 or 240 -> 60 change is kept. (480 -> 240 -> 60 declared 60 is unaffected: the head's successor is not the declared interval.)
            nxt = runs[cands[pos + 1]][0] if pos + 1 < len(cands) else None
            if declared_ms and v > declared_ms and v % declared_ms == 0 and nxt == declared_ms and (v // declared_ms) & (v // declared_ms - 1):
                continue
        elif v > prev and v % prev == 0 and v != declared_ms:
            continue
        acc.append(k)
        prev = v
    return acc


def funding_regimes(ts_sorted: Sequence[int], declared_ms: Optional[int], accepted: Optional[Sequence[Sequence[int]]] = None) -> Dict[str, Any]:
    """Funding grid with REGIME CHANGES. A funding interval is per symbol and can change inside a symbol's history (480 -> 240 -> 60 minutes). Consecutive print spacings are run-length
    encoded; a run of >= MIN_REGIME_RUN equal spacings is a REGIME (segment) with its own interval_ms. A shorter run is judged against the nearest regime before it (else after,
    else the declared interval): a whole multiple of that interval is a GAP (missing prints, reported), anything else is an OFF-GRID step. A legitimate change is therefore never
    a gap, a single odd spacing is never a regime, and nothing is filled. ``declared_ms`` (the instrument record at observation time) must equal the LAST regime: else the
    record disagrees with the data (``interval_mismatch``). With no declared interval the gap check is REFUSED (regimes are still reported as inferred).
    ``accepted`` = operator-corroborated (from_ms, to_ms) interval changes. Spacing alone cannot tell a genuine 480 -> 60 change from a head hole of the same spacing (8 x 60 min steps look
    like a regime), so every regime change NOT in ``accepted`` is listed in ``unaccepted_regime_changes`` and the caller marks the symbol PARTIAL (``unproven_regime_change``)."""
    diffs = [b - a for a, b in zip(ts_sorted, ts_sorted[1:])]
    runs: List[List[int]] = []                       # [value, count, first diff index]
    for i, d in enumerate(diffs):
        if runs and runs[-1][0] == d:
            runs[-1][1] += 1
        else:
            runs.append([d, 1, i])
    longs = _accepted_regime_runs(runs, declared_ms)
    segs: List[Dict[str, Any]] = []
    for k in longs:
        v, cnt, i0 = runs[k]
        first, last = ts_sorted[i0], ts_sorted[i0 + cnt]
        if segs and segs[-1]["interval_ms"] == v:
            segs[-1]["last_ts"] = last
        else:
            segs.append({"interval_ms": v, "first_ts": first, "last_ts": last})
    inferred = bool(segs)
    if not segs and ts_sorted and declared_ms:
        segs = [{"interval_ms": declared_ms, "first_ts": ts_sorted[0], "last_ts": ts_sorted[-1]}]
    for sg in segs:
        sg["fundingInterval_min"] = sg["interval_ms"] / 60_000 if sg["interval_ms"] % 60_000 else sg["interval_ms"] // 60_000
        sg["settlements_per_day"] = 86_400_000 / sg["interval_ms"]
        sg["prints"] = sum(1 for t in ts_sorted if sg["first_ts"] <= t <= sg["last_ts"])
        sg["source"] = "inferred_from_print_spacing" if inferred else "declared_instrument_record"
    changes = [{"at_ts": b["first_ts"], "from_interval_ms": a["interval_ms"], "to_interval_ms": b["interval_ms"]} for a, b in zip(segs, segs[1:])]
    ivs = [sg["interval_ms"] for sg in segs]
    acc = {(int(x[0]), int(x[1])) for x in (accepted or [])}
    base = {"segments": segs, "regime_changes": changes, "unaccepted_regime_changes": [c for c in changes if (c["from_interval_ms"], c["to_interval_ms"]) not in acc], "observed_interval_ms": segs[-1]["interval_ms"] if segs else None,
            "regime_flapping": len(set(ivs)) != len(ivs),          # an interval that comes back non-adjacently (240/480/240/480): prints are hidden between the runs

            "interval_mismatch": bool(declared_ms and inferred and segs[-1]["interval_ms"] != declared_ms)}
    if declared_ms is None:
        return {**base, "grid_check": "REFUSED_UNKNOWN_INTERVAL", "expected": None, "missing": None, "gaps": [], "gap_count": None, "off_grid": [], "off_grid_count": 0}
    gaps: List[Dict[str, int]] = []
    off: List[Dict[str, int]] = []
    missing = gap_n = off_n = 0
    for k, (v, cnt, i0) in enumerate(runs):
        if k in longs:
            continue
        ref = next((runs[j][0] for j in reversed(longs) if j < k), None) or next((runs[j][0] for j in longs if j > k), None) or declared_ms
        for i in range(i0, i0 + cnt):
            a, b = ts_sorted[i], ts_sorted[i + 1]
            if v == ref:
                continue
            if v > ref and v % ref == 0:
                missing += v // ref - 1
                gap_n += 1
                if len(gaps) < 100:
                    gaps.append({"after_ts": a, "next_ts": b, "missing": v // ref - 1})
            else:
                off_n += 1
                if len(off) < 50:
                    off.append({"after_ts": a, "next_ts": b, "step_ms": v})
    gaps.sort(key=lambda g: g["after_ts"])
    return {**base, "grid_check": "OK", "expected": len(ts_sorted) + missing, "missing": missing, "gaps": gaps, "gap_count": gap_n, "off_grid": off, "off_grid_count": off_n}


def _obs_time(dataset: str, ts: int, interval_ms: Optional[int]) -> int:
    return ts + interval_ms if dataset in (KLINE, MARK, INDEX) and interval_ms else ts


def analyze_symbol(sdir: str, spec: Mapping[str, Any], normalization_ms: int) -> Tuple[Dict[str, Any], bytes]:
    """Re-derive everything for one symbol from its ledger and RAW pages: (summary, rows.jsonl bytes). Deterministic given ``normalization_ms``."""
    dataset, symbol = spec["dataset"], spec["symbol"]
    ledger, problems = read_ledger(sdir)
    ledger_raw = b""
    lp = os.path.join(sdir, "pages.jsonl")
    if os.path.isfile(lp):
        with open(lp, "rb") as fh:
            ledger_raw = fh.read()
    good, bad = _verify_pages(sdir, ledger)
    problems += bad
    reasons: List[str] = []
    rejects: Dict[str, int] = {}
    unclosed: set = set()
    unique: Dict[int, Tuple[Dict[str, Any], int]] = {}
    dup_ident = conflicts = pages_ok = pages_failed = skew = 0
    first_recv: Optional[int] = None
    stop_reason = None
    page_digest = hashlib.sha256()
    interval_ms = spec.get("interval_ms")
    if ledger and ledger[0].get("type") != "begin":
        problems.append("LEDGER_NO_BEGIN")
    chain_end: Optional[int] = spec["end_ms"]            # the endTime the NEXT page must have asked for (backward paging: previous page's next_end)
    for r in ledger[:good]:
        if r["type"] == "stop":
            stop_reason = r.get("reason")
            continue
        if r["type"] != "page":
            continue
        asked = r.get("request", {}).get("params", {}).get(DATASETS[dataset]["end"])
        if chain_end is not None and (not isinstance(asked, str) or not _UINT.fullmatch(asked) or int(asked) != chain_end):
            if not any(p.startswith("LEDGER_CHAIN_BROKEN") for p in problems):
                problems.append(f"LEDGER_CHAIN_BROKEN: page {r.get('n')} asked endTime {asked!r}, the previous page's next_end was {chain_end}")
        if isinstance(r.get("next_end"), int) and not isinstance(r.get("next_end"), bool):
            chain_end = r["next_end"]
        page_digest.update(f"{r['n']}:{r['sha256']}:{int(bool(r.get('ok')))}\n".encode())
        if not r.get("ok"):                                 # an envelope error / stalled-cursor page: kept as evidence, its rows are not counted
            pages_failed += 1
            continue
        with open(os.path.join(sdir, r["file"]), "rb") as fh:
            body = fh.read()
        req = r.get("request", {}).get("params", {})
        cur_end = int(req.get(DATASETS[dataset]["end"], spec["end_ms"]))
        pr = parse_page(dataset, symbol, body, spec["start_ms"], cur_end, spec.get("interval_ms"))
        unclosed.update(pr["unclosed"])
        for k, v in pr["rejects"].items():
            _bump(rejects, k, v)
        pages_ok += 1
        if isinstance(r.get("recv_ms"), int) and (pr["server_time"] is None or abs(r["recv_ms"] - pr["server_time"]) > SKEW_WARN_MS):
            skew += 1
        for row in pr["rows"]:
            prev = unique.get(row["ts"])
            if prev is None:
                unique[row["ts"]] = (row, r["n"])
            elif prev[0] == row:
                dup_ident += 1
            else:
                conflicts += 1
    ts_sorted = sorted(unique)
    if dataset == FUNDING:
        grid = funding_regimes(ts_sorted, interval_ms, spec.get("accepted_regime_changes"))
    else:
        grid = grid_report(ts_sorted, interval_ms)
        grid.update(segments=[{"interval_ms": interval_ms, "first_ts": ts_sorted[0], "last_ts": ts_sorted[-1], "prints": len(ts_sorted), "source": "declared_interval_grid"}] if ts_sorted and interval_ms else [],
                    regime_changes=[], observed_interval_ms=interval_ms, interval_mismatch=False)
    first_iv = grid["segments"][0]["interval_ms"] if grid["segments"] else interval_ms
    last_iv = grid["observed_interval_ms"] or interval_ms
    cov: Dict[str, Any] = {"first_ts": ts_sorted[0] if ts_sorted else None, "last_ts": ts_sorted[-1] if ts_sorted else None, "rows": len(ts_sorted),
                           "expected": grid["expected"], "missing": grid["missing"], "interval_ms": interval_ms, "interval_source": spec.get("interval_source", "UNKNOWN"),
                           "requested_window": {"start_ms": spec["start_ms"], "end_ms": spec.get("window_end_ms", spec["end_ms"]), "symbol_end_ms": spec["end_ms"]},
                           "served_first_ts": ts_sorted[0] if ts_sorted else None, "served_last_ts": ts_sorted[-1] if ts_sorted else None, "served_rows": len(ts_sorted)}
    # head / tail
    eff_start = max(spec["start_ms"], spec.get("launch_ms") or 0)
    head_missing = 0
    head_verified = True
    if ts_sorted and first_iv:
        gap0 = ts_sorted[0] - eff_start
        if gap0 >= first_iv:
            if stop_reason == "exhausted" and not spec.get("launch_ms"):
                head_verified = False                      # the venue ran out of rows and we do not know when the symbol launched: the head cannot be proved
            else:
                head_missing = gap0 // first_iv
    cov["head_missing"] = head_missing
    tail_ref = min(spec["end_ms"], spec["delivery_ms"]) if spec.get("delivery_ms") else spec["end_ms"]       # a delisted contract's history ends at its delivery time: later "gaps" are not gaps
    cov["tail_lag_ms"] = (tail_ref - ts_sorted[-1]) if ts_sorted else None
    cov["delivery_ms"] = spec.get("delivery_ms")
    cov["observed_interval_ms"] = grid["observed_interval_ms"]
    cov["launch_ms"] = spec.get("launch_ms")
    # status
    if problems:
        status = "UNVERIFIED"
        reasons += [p.split(":")[0] for p in problems]
    else:
        reasons_p: List[str] = []
        if rejects:
            reasons_p.append("rejects")
        if skew:
            reasons_p.append("clock_skew")
        if unclosed:
            reasons_p.append("unclosed_candle")
        if grid["gap_count"]:
            reasons_p.append("gaps")
        if grid["off_grid_count"]:
            reasons_p.append("off_grid_step")
        if grid["interval_mismatch"]:
            reasons_p.append("interval_disagrees_with_record")
        if grid.get("regime_flapping"):
            reasons_p.append("regime_flapping")
        if grid.get("unaccepted_regime_changes"):
            reasons_p.append("unproven_regime_change")            # a changed interval nobody corroborated may be a missing head: never silently COMPLETE
        if head_missing:
            reasons_p.append("head_gap")
        if not head_verified:
            reasons_p.append("head_unverified_no_launch_time")
        if stop_reason not in ("reached_start", "exhausted"):
            reasons_p.append("not_terminated")
        if not ts_sorted:
            reasons_p.append("no_rows")
        if conflicts:
            status = "CONFLICTED"
            reasons = ["conflicting_duplicate"] + reasons_p
        elif interval_ms is None:
            status = "UNVERIFIED"
            reasons = ["grid_unknown_interval"] + reasons_p
        elif reasons_p:
            status = "PARTIAL"
            reasons = reasons_p
        elif cov["tail_lag_ms"] is not None and cov["tail_lag_ms"] > STALE_INTERVALS * last_iv:
            status, reasons = "STALE", ["tail_older_than_two_intervals"]
        else:
            status, reasons = "COMPLETE", []
    lines = []
    for ts in ts_sorted:
        row, n = unique[ts]
        lines.append(_canon({"ts": ts, "observation_time": _obs_time(dataset, ts, interval_ms), "page": n, "v": {k: v for k, v in row.items() if k != "ts"}}))
    rows_bytes = b"".join(lines)
    summary = {"status": status, "reasons": reasons, "coverage": cov, "gaps": grid["gaps"], "gap_count": grid["gap_count"], "grid_check": grid["grid_check"],
               "off_grid": grid["off_grid"], "off_grid_count": grid["off_grid_count"], "segments": grid["segments"],
               "regime_changes": grid["regime_changes"], "pages": pages_ok, "pages_failed": pages_failed, "rejects": dict(sorted(rejects.items())),
               "duplicates_identical": dup_ident, "conflicts": conflicts, "unclosed_candles": sorted(unclosed), "skew_flags": skew, "stop_reason": stop_reason, "problems": problems,
               "ledger_sha256": _sha(ledger_raw), "pages_digest": page_digest.hexdigest(), "rows_sha256": _sha(rows_bytes), "normalized_ms": normalization_ms,
               "spec": dict(spec)}
    return summary, rows_bytes


# ---------------------------------------------------------------- collection

def _quarantine(sdir: str, names: Sequence[str], note: str) -> str:
    k = 1
    while os.path.exists(os.path.join(sdir, "quarantine", str(k))):
        k += 1
    qd = os.path.join(sdir, "quarantine", str(k))
    os.makedirs(qd, mode=0o700)
    for n in names:
        if os.path.lexists(os.path.join(sdir, n)):
            os.rename(os.path.join(sdir, n), os.path.join(qd, n))
    UC._create_exclusive(os.path.join(qd, "NOTE.txt"), (note + "\n").encode())
    return qd


def _begin_row(spec: Mapping[str, Any]) -> Dict[str, Any]:
    return {"type": "begin", "schema": SCHEMA, "dataset": spec["dataset"], "symbol": spec["symbol"], "interval": spec.get("interval"), "start_ms": spec["start_ms"],
            "end_ms": spec["end_ms"], "limit": DATASETS[spec["dataset"]]["limit"]}


def _resume_state(sdir: str, spec: Mapping[str, Any]) -> Dict[str, Any]:
    """Load the verified prefix of the ledger; quarantine (never delete) everything after the first bad page and any unlisted page file."""
    ledger, problems = read_ledger(sdir)
    quarantined: List[str] = []
    good, bad = _verify_pages(sdir, ledger)
    if ledger and ledger[0].get("type") == "begin":
        want = _begin_row(spec)
        if any(ledger[0].get(k) != want[k] for k in want):
            raise HistoryError("PARAMS_MISMATCH: this symbol directory was started with different parameters; refusing to mix windows")
    elif ledger:
        raise HistoryError("LEDGER_NO_BEGIN: the ledger does not start with its begin row")
    keep = ledger[:good]
    listed = {r["file"] for r in keep if r["type"] == "page"}
    stray = sorted(n for n in os.listdir(sdir) if re.fullmatch(r"page-[0-9]{6}\.json", n) and n not in listed)
    if problems or bad or len(keep) != len(ledger) or stray:
        note = f"resume quarantine: problems={problems + bad} ledger_rows_dropped={len(ledger) - len(keep)} stray_pages={stray}"
        files = stray + [r["file"] for r in ledger[good:] if r["type"] == "page" and r.get("file") in os.listdir(sdir)]
        qd = _quarantine(sdir, sorted(set(files)), note)
        lp = os.path.join(sdir, "pages.jsonl")
        if os.path.isfile(lp):
            os.rename(lp, os.path.join(qd, "pages.jsonl"))
        for r in keep:
            _append_line(lp, r)
        quarantined.append(os.path.relpath(qd, sdir))
    pages = [r for r in keep if r["type"] == "page"]
    last_ok = next((r for r in reversed(pages) if r.get("ok")), None)
    stop = next((r for r in keep if r["type"] == "stop"), None)
    return {"had_begin": bool(keep and keep[0]["type"] == "begin"), "n": max((r["n"] for r in pages), default=0), "cursor_end": last_ok["next_end"] if last_ok else spec["end_ms"],
            "stop": stop["reason"] if stop else None, "quarantined": quarantined}


def collect_symbol(spec: Mapping[str, Any], sdir: str, fetch: Callable[[str], bytes], clock: Callable[[], int], sleep: Callable[[float], None], bucket: TokenBucket,
                   max_pages: int, max_retries: int = MC.MAX_RETRIES, backoff_s: float = MC.BACKOFF_S,
                   disk_check: Optional[Callable[[], Optional[str]]] = None) -> Dict[str, Any]:
    """Fetch (or resume) one symbol until the window start, an empty page, a guard stop or an error. Returns run-level facts; the status comes from ``analyze_symbol``."""
    dataset, symbol, interval = spec["dataset"], spec["symbol"], spec.get("interval")
    os.makedirs(sdir, mode=0o700, exist_ok=True)
    st = _resume_state(sdir, spec)
    ledger = os.path.join(sdir, "pages.jsonl")
    if not st["had_begin"]:
        _append_line(ledger, _begin_row(spec))
    errors: List[str] = []
    fx = MC._Fetcher(fetch, sleep, max_retries, backoff_s)
    n, cursor_end = st["n"], st["cursor_end"]
    stop = st["stop"]
    resumed_pages = sum(1 for r in read_ledger(sdir)[0] if r["type"] == "page")
    fetched = 0

    def _stop(reason: str) -> None:
        nonlocal stop
        stop = reason
        _append_line(ledger, {"type": "stop", "reason": reason, "at_ms": int(clock())})

    if stop is None and cursor_end < spec["start_ms"]:
        _stop("reached_start")
    while stop is None:
        if fetched >= max_pages:
            errors.append(f"page_cap: {max_pages} pages fetched in this run, window not exhausted (rerun resumes)")
            break
        if disk_check is not None:                                      # per PAGE, not only per symbol: one deep symbol can fill the disk
            why = disk_check()
            if why:
                errors.append(why)
                break
        url = request_url(dataset, symbol, interval, spec["start_ms"], cursor_end)
        ev0 = len(fx.events)
        try:
            bucket.acquire()
            body = fx(url)
        except MC.RateLimitExhausted as exc:
            bucket.penalize(MC.BACKOFF_CAP_S)
            errors.append(f"rate_limited: {exc}")
            break
        except (MC.CollectorError, UC.CaptureError, OSError) as exc:
            errors.append(f"fetch: {exc}")
            break
        if len(fx.events) > ev0:
            bucket.penalize(float(fx.events[-1].get("sleep_s", 0.0)))
        recv = int(clock())
        if len(body) > MAX_BODY_BYTES:
            errors.append("response larger than any history page")
            break
        n += 1
        fname = _page_name(n)
        UC._create_exclusive(os.path.join(sdir, fname), body)          # raw, unedited, before anything is concluded about it
        coll = int(clock())
        pr = parse_page(dataset, symbol, body, spec["start_ms"], cursor_end, spec.get("interval_ms"))
        params = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
        row: Dict[str, Any] = {"type": "page", "n": n, "file": fname, "sha256": _sha(body), "bytes": len(body), "ok": pr["ok"], "request": {"path": urllib.parse.urlsplit(url).path,
                               "params": params, "url": url}, "server_time": pr["server_time"], "recv_ms": recv, "collection_ms": coll, "raw_rows": pr["raw_count"],
                               "rows": len(pr["rows"]), "min_ts": min(pr["raw_ts"]) if pr["raw_ts"] else None, "max_ts": max(pr["raw_ts"]) if pr["raw_ts"] else None,
                               "cursor_end": cursor_end, "rejects": pr["rejects"], "next_page_cursor": pr.get("next_page_cursor")}
        fetched += 1
        if not pr["ok"]:
            row["next_end"] = None
            _append_line(ledger, row)
            errors.append(f"page {n}: {sorted(pr['rejects'])}")
            break
        if pr["raw_count"] == 0:
            row["next_end"] = None
            _append_line(ledger, row)
            _stop("exhausted")
            break
        if not pr["raw_ts"]:
            row["next_end"] = None
            row["ok"] = False
            _append_line(ledger, row)
            errors.append(f"page {n}: no parseable timestamp; the cursor cannot move")
            break
        nxt = min(pr["raw_ts"]) - 1
        if nxt >= cursor_end:
            row["next_end"] = None
            row["ok"] = False
            _append_line(ledger, row)
            errors.append(f"page {n}: cursor_stalled (next endTime {nxt} does not move below {cursor_end})")
            break
        row["next_end"] = nxt
        _append_line(ledger, row)
        cursor_end = nxt
        if cursor_end < spec["start_ms"]:
            _stop("reached_start")
            break
    return {"errors": errors, "stop_reason": stop, "pages_fetched": fetched, "pages_resumed": resumed_pages, "quarantined": st["quarantined"], "rate_limit_events": len(fx.events),
            "requests": fx.requests}


# ---------------------------------------------------------------- dataset run + manifest

def dataset_dirname(dataset: str, interval: Optional[str]) -> str:
    return dataset + (f"-{interval}" if interval else "")


def _worst(statuses: Sequence[str]) -> str:
    for s in STATUS_ORDER:
        if s in statuses:
            return s
    return "UNVERIFIED"


def load_universe_dir(path: str, *, only_trading: bool = False, include_statuses: Optional[Sequence[str]] = None) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, Any]]:
    """Symbols (and their funding interval / launch / delivery time) from a VERIFIED universe_capture directory. Schema 2: ``universe_capture.verify_capture_dir`` must be ok
    AND its RE-DERIVED dataset_status must be COMPLETE or PARTIAL (reported in the returned info and in every manifest built from it); anything else is refused. Schema 1 (legacy):
    its own file hash is checked and the capture is labelled ``capture_schema: 1``. Only linear rows are returned."""
    mpath = os.path.join(path, "manifest.json")
    if os.path.islink(path) or os.path.islink(mpath) or not os.path.isfile(mpath):
        raise HistoryError("universe directory: no manifest.json (or a link)")
    with open(mpath, "rb") as fh:
        mbytes = fh.read()
    try:
        man = json.loads(mbytes)
    except ValueError as exc:
        raise HistoryError("universe manifest is not JSON") from exc
    problems: List[str] = []
    v2 = isinstance(man, dict) and man.get("schema") == UC.SCHEMA_V2
    uni = (man.get("universe_file") if v2 else man.get("universe")) if isinstance(man, dict) else None
    if not isinstance(uni, dict) or not isinstance(uni.get("file"), str) or os.path.basename(uni["file"]) != uni["file"]:
        raise HistoryError("universe manifest names no universe file")
    upath = os.path.join(path, uni["file"])
    if not os.path.isfile(upath) or os.path.islink(upath):
        problems.append("universe file missing")
    else:
        with open(upath, "rb") as fh:
            if _sha(fh.read()) != uni.get("sha256"):
                problems.append("universe file hash differs from its manifest")
    ds = man.get("dataset_status")
    derived: Dict[str, Any] = {}
    rederived: List[bytes] = []
    if v2:
        # schema 2: ``verify_capture_dir`` re-derives every claim from the raw bytes. Integrity must hold AND the RE-DERIVED dataset_status must be COMPLETE or PARTIAL
        # (never the manifest's own claim); the answer is recorded in the manifest of every dataset built from it.
        # ``describe_scope`` judges the capture from its RAW bytes (a manifest written under older rules fails the strict claim comparison of ``verify_capture_dir`` yet is sound): raw
        # integrity must hold; the RE-DERIVED dataset_status must be COMPLETE or PARTIAL, or the capture must be research-usable (COMPLETE_WITH_EXPLAINED_MISMATCH: every mismatched row explained).
        vres = UC.verify_capture_dir(path, _uni_sink=rederived)
        derived = vres.get("coverage") or {}
        ds = derived.get("dataset_status")
        usable = False
        if not vres.get("ok") or ds not in ("COMPLETE", "PARTIAL"):      # strict claim comparison failed: judge from the raw bytes instead (older rules_version manifests)
            scope = UC.describe_scope(path)
            if not scope.get("raw_integrity_ok"):
                problems += [str(x) for x in scope.get("integrity_problems", ["raw integrity failed"])]
            ds = scope.get("dataset_status") or ds
            usable = bool((scope.get("eligible_perpetual_lifecycle_universe") or {}).get("usable"))
            derived = {**derived, "delisting_observability": scope.get("delisting_observability") or derived.get("delisting_observability"),
                       "status_coverage": derived.get("status_coverage") or scope.get("status_coverage")}
        else:
            usable = True
        if ds not in ("COMPLETE", "PARTIAL") and not usable:
            problems.append(f"universe re-derived dataset_status is {ds!r}: only COMPLETE, PARTIAL or a research-usable (explained-mismatch) capture is accepted")
    elif ds in ("CONFLICTED", "UNVERIFIED", "FAILED"):
        problems.append(f"universe dataset_status is {ds}")
    if problems:
        raise HistoryError("universe capture does not verify: " + "; ".join(problems[:5]))
    if v2:                                                               # records come from the universe RE-DERIVED from the raw pages, never from the unverified jsonl
        if not rederived:
            raise HistoryError("universe capture does not verify: the universe could not be re-derived from its raw pages")
        allrecs = UC.parse_universe_bytes(rederived[0], "re-derived universe")
    else:
        allrecs = UC.load_universe(upath)
    recs = {k: r for k, r in allrecs.items() if r.get("category", "linear") == "linear"}
    if only_trading and include_statuses:
        raise HistoryError("give --only-trading or --include-statuses, not both")
    wanted = {"Trading"} if only_trading else (set(include_statuses) if include_statuses else None)
    seen_statuses: Dict[str, int] = {}
    for r in recs.values():
        seen_statuses[str(r.get("status"))] = seen_statuses.get(str(r.get("status")), 0) + 1
    if wanted is not None:
        recs = {s: r for s, r in recs.items() if r.get("status") in wanted}
    return recs, {"dir": os.path.abspath(path), "manifest_sha256": _sha(mbytes), "snapshot_ms": man.get("snapshot_ms", man.get("started_ms")), "universe_sha256": uni.get("sha256"),
                  "schema": man.get("schema_version", man.get("schema")), "dataset_status": ds, "symbols": len(recs),
                  "statuses_in_capture": dict(sorted(seen_statuses.items())), "capture_schema": 2 if v2 else 1,
                  "status_coverage": derived.get("status_coverage"), "delisting_observability": derived.get("delisting_observability"), "included_statuses": sorted(wanted) if wanted is not None else "ALL"}


def clamp_end_to_delivery(delivery_ms: Optional[int], end_ms: int) -> int:
    """THE end-of-range rule, shared by collection (``build_specs``) and the funnel (``backfill_plan.symbol_range``): never past a stated delivery time, whatever the status says."""
    return min(end_ms, delivery_ms) if isinstance(delivery_ms, int) and not isinstance(delivery_ms, bool) and delivery_ms > 0 else end_ms


def build_specs(dataset: str, interval: Optional[str], symbols: Sequence[str], start_ms: int, end_ms: int, records: Optional[Mapping[str, Mapping[str, Any]]] = None,
                funding_interval_min: Optional[Mapping[str, int]] = None, accept_regime_changes: Optional[Mapping[str, Sequence[Tuple[int, int]]]] = None) -> List[Dict[str, Any]]:
    imap = dict(funding_interval_min or {})
    acc_map = {k: sorted({(int(a), int(b)) for a, b in v}) for k, v in (accept_regime_changes or {}).items()}
    unknown = sorted(set(acc_map) - set(symbols))
    if unknown:
        raise HistoryError(f"--accept-regime-change names symbols that are not being collected: {unknown}")
    out = []
    for s in sorted(set(symbols)):
        if not _SYMBOL.fullmatch(s):
            raise HistoryError(f"refusing symbol {s!r}")
        rec = (records or {}).get(s)
        spec: Dict[str, Any] = {"dataset": dataset, "symbol": s, "interval": interval, "start_ms": start_ms, "end_ms": end_ms,
                                "launch_ms": rec.get("launchTime") if rec and isinstance(rec.get("launchTime"), int) and rec["launchTime"] > 0 else None,
                                "delivery_ms": rec.get("deliveryTime") if rec and isinstance(rec.get("deliveryTime"), int) and rec["deliveryTime"] > 0 else None}
        spec["window_end_ms"] = end_ms
        if spec["delivery_ms"] and spec["delivery_ms"] < start_ms:
            spec["skip"] = "delisted_before_window"
        elif spec["launch_ms"] and spec["launch_ms"] > end_ms:
            spec["skip"] = "launched_after_window"
        elif spec["delivery_ms"] and spec["delivery_ms"] < end_ms:
            spec["end_ms"] = clamp_end_to_delivery(spec["delivery_ms"], end_ms)          # a Closed contract is asked up to its delisting (venue-observed: pre-delisting history IS served); never past it
        if dataset == FUNDING:
            if s in imap:
                spec.update(interval_ms=imap[s] * 60_000, interval_source="operator_declared")
            elif rec and isinstance(rec.get("fundingInterval_min"), int) and rec["fundingInterval_min"] > 0:
                spec.update(interval_ms=rec["fundingInterval_min"] * 60_000, interval_source=f"universe_record:fundingInterval_min@{rec.get('snapshot_ms')}")
            else:
                spec.update(interval_ms=None, interval_source="UNKNOWN")
        else:
            spec.update(interval_ms=interval_ms_of(dataset, interval), interval_source="declared_interval_grid")
        if dataset == FUNDING and acc_map.get(s):
            spec["accepted_regime_changes"] = [[a * 60_000, b * 60_000] for a, b in acc_map[s]]       # operator evidence, part of the spec so verify re-derives the same status
        out.append(spec)
    return out


def run_dataset(root: str, dataset: str, interval: Optional[str], specs: Sequence[Mapping[str, Any]], *, fetch: Callable[[str], bytes] = http_fetch,
                clock: Callable[[], int] = lambda: int(time.time() * 1000), sleep: Callable[[float], None] = time.sleep, mono: Callable[[], float] = time.monotonic,
                workers: int = DEFAULT_WORKERS, rate: float = DEFAULT_RATE, max_pages: int = DEFAULT_MAX_PAGES, reserve_bytes: int = MC.DEFAULT_RESERVE_BYTES,
                free_bytes_fn: Callable[[str], int] = lambda p: shutil.disk_usage(p).free, universe: Optional[Mapping[str, Any]] = None,
                max_retries: int = MC.MAX_RETRIES, backoff_s: float = MC.BACKOFF_S, window_end_basis: str = "OPERATOR_END_MS",
                selection: Optional[Mapping[str, Any]] = None, on_symbol: Optional[Callable[[str, Mapping[str, Any]], None]] = None) -> Dict[str, Any]:
    """Collect every spec'd symbol (bounded concurrency, shared token bucket), then write the dataset manifest. Returns the manifest (or a refusal)."""
    if not 1 <= workers <= MAX_WORKERS:
        raise HistoryError(f"workers must be 1..{MAX_WORKERS}")
    excluded = {x["symbol"]: x["skip"] for x in specs if x.get("skip")}      # delisted before the window opened: nothing exists to fetch, and that is not a gap
    specs = [x for x in specs if not x.get("skip")]
    if not specs:
        raise HistoryError("no symbols to collect in this window")
    root = os.path.abspath(root)
    guard = MC.root_guard(root, reserve_bytes, free_bytes_fn)
    if guard:
        return {"status": "REFUSED_ROOT", "problems": guard}
    ddir = os.path.join(root, dataset_dirname(dataset, interval))
    os.makedirs(ddir, mode=0o700, exist_ok=True)
    started = int(clock())
    bucket = TokenBucket(rate, max(1, int(rate)), mono, sleep)
    results: Dict[str, Dict[str, Any]] = {}

    def work(spec: Mapping[str, Any]) -> Tuple[str, Dict[str, Any]]:
        sdir = os.path.join(ddir, spec["symbol"])
        try:
            free = int(free_bytes_fn(root))
            if free < reserve_bytes:
                return spec["symbol"], {"errors": [f"disk_floor: {free} bytes free is below the reserve {reserve_bytes}; stopped before this symbol"], "stop_reason": None,
                                        "pages_fetched": 0, "pages_resumed": 0, "quarantined": [], "rate_limit_events": 0, "requests": 0}
            def disk_check() -> Optional[str]:
                f = int(free_bytes_fn(root))
                return f"disk_floor: {f} bytes free is below the reserve {reserve_bytes}; stopped before the next page (rerun resumes)" if f < reserve_bytes else None
            res = collect_symbol(spec, sdir, fetch, clock, sleep, bucket, max_pages, max_retries, backoff_s, disk_check)
            if on_symbol is not None:
                on_symbol(spec["symbol"], res)               # a checkpoint after EVERY symbol (run-plan's progress file); the pages themselves are already durable
            return spec["symbol"], res
        except HistoryError as exc:
            return spec["symbol"], {"errors": [str(exc)], "stop_reason": None, "fatal": True, "pages_fetched": 0, "pages_resumed": 0, "quarantined": [], "rate_limit_events": 0, "requests": 0}
        except (OSError, UC.CaptureError, MC.CollectorError) as exc:
            return spec["symbol"], {"errors": [f"{type(exc).__name__}: {exc}"], "stop_reason": None, "pages_fetched": 0, "pages_resumed": 0, "quarantined": [],
                                    "rate_limit_events": 0, "requests": 0}

    with MC._locked(ddir):
        with ThreadPoolExecutor(max_workers=workers) as ex:
            for sym, res in ex.map(work, specs):
                results[sym] = res
        norm = int(clock())
        symbols: Dict[str, Any] = {}
        for spec in specs:
            sym = spec["symbol"]
            sdir = os.path.join(ddir, sym)
            run = results[sym]
            if run.get("fatal") or not os.path.isdir(sdir):
                symbols[sym] = {"status": "UNVERIFIED", "reasons": ["symbol_not_collected"], "run": run, "spec": dict(spec)}
                continue
            try:
                summ, rows_bytes = analyze_symbol(sdir, spec, norm)
            except (KeyError, ValueError, TypeError, AttributeError, OSError) as exc:
                symbols[sym] = {"status": "UNVERIFIED", "reasons": ["analysis_failed"], "run": {**run, "errors": list(run["errors"]) + [f"analysis: {type(exc).__name__}: {exc}"]},
                                "spec": dict(spec)}
                continue
            rp = os.path.join(sdir, "rows.jsonl")
            _write_atomic(rp, rows_bytes)
            if run["errors"] and summ["status"] in ("COMPLETE", "STALE"):
                summ["status"], summ["reasons"] = "PARTIAL", ["run_errors"]
            summ["run"] = run
            symbols[sym] = summ
        statuses = [s["status"] for s in symbols.values()]
        sel = dict(selection) if selection else {"mode": "EXPLICIT_SYMBOLS" if not universe else ("FROM_PLAN" if universe.get("symbols_from_plan") else "UNIVERSE"),
                                                 "plan_digest": ((universe or {}).get("symbols_from_plan") or {}).get("plan_digest")}
        sel["symbols_n"] = len(specs) + len(excluded)               # the symbols the operator ASKED for (those skipped by lifecycle included)
        if sel.get("mode") not in SELECTION_MODES:
            raise HistoryError(f"selection.mode must be one of {SELECTION_MODES}")
        by_status = {k: statuses.count(k) for k in STATUS_ORDER if k in statuses}
        man = {"schema": SCHEMA, "dataset": dataset, "interval": interval, "request": {"host": venues.host_of(venues.rest_for("mainnet")), "path": DATASETS[dataset]["path"],
               "category": CATEGORY, "limit": DATASETS[dataset]["limit"], "start_ms": specs[0]["start_ms"], "end_ms": specs[0]["window_end_ms"],
               "window_end_basis": window_end_basis},
               "started_ms": started, "finished_ms": int(clock()), "dataset_status": _worst(statuses), "counts": {"symbols": len(symbols), "by_status": by_status},
               "universe": dict(universe) if universe else None, "selection": sel, "excluded": excluded, "time_fields": list(TIME_FIELDS), "symbols": symbols,
               "note": "raw pages are the evidence; gaps/rejects/conflicts are reported, never filled; a hash shows what was read, not who produced it"}
        man["manifest_digest"] = _sha(_canon({k: v for k, v in man.items() if k != "manifest_digest"}))
        _write_atomic(os.path.join(ddir, "manifest.json"), (json.dumps(man, indent=1, sort_keys=True, allow_nan=False) + "\n").encode())
    return man


# ---------------------------------------------------------------- verify (pure Python, re-hashes everything)

def verify_dataset(ddir: str, accept: Optional[Mapping[str, Sequence[Tuple[int, int]]]] = None) -> Dict[str, Any]:
    """Re-derive every symbol from its raw pages and compare with the manifest: page/ledger/rows hashes, coverage, status, stray files. Never fetches.
    ``accept`` ({symbol: [(from_min, to_min)]}) only PREVIEWS what the status would be if those regime changes were corroborated (``status_if_accepted``); ``ok`` never depends on it."""
    problems: List[str] = []
    mp = os.path.join(ddir, "manifest.json")
    if os.path.islink(mp) or not os.path.isfile(mp):
        return {"ok": False, "problems": ["manifest.json missing"], "symbols_checked": 0, "pages_checked": 0, "dataset_status": None}
    try:
        man = json.loads(open(mp, "rb").read())
    except ValueError:
        return {"ok": False, "problems": ["manifest.json is not JSON"], "symbols_checked": 0, "pages_checked": 0, "dataset_status": None}
    if not isinstance(man, dict) or man.get("schema") != SCHEMA or not isinstance(man.get("symbols"), dict):
        return {"ok": False, "problems": ["not a market_history/v1 manifest"], "symbols_checked": 0, "pages_checked": 0, "dataset_status": None}
    if man.get("manifest_digest") != _sha(_canon({k: v for k, v in man.items() if k != "manifest_digest"})):
        problems.append("manifest_digest does not match the manifest's content")
    pages = 0
    preview: Dict[str, Any] = {}
    for sym, rec in sorted(man["symbols"].items()):
        sdir = os.path.join(ddir, sym)
        spec = rec.get("spec")
        if not isinstance(spec, dict) or "ledger_sha256" not in rec:
            if rec.get("status") != "UNVERIFIED":
                problems.append(f"{sym}: no spec/ledger hash in the manifest")
            continue
        if not os.path.isdir(sdir):
            problems.append(f"{sym}: directory missing")
            continue
        try:
            summ, rows_bytes = analyze_symbol(sdir, spec, rec["normalized_ms"])
        except (KeyError, ValueError, TypeError, AttributeError, OSError) as exc:
            problems.append(f"{sym}: cannot be re-derived from its raw pages ({type(exc).__name__}: {exc})")
            continue
        pages += summ["pages"] + summ["pages_failed"]
        if accept and accept.get(sym) and not summ["problems"]:
            try:
                alt, _rb = analyze_symbol(sdir, dict(spec, accepted_regime_changes=[[a * 60_000, b * 60_000] for a, b in accept[sym]]), rec["normalized_ms"])
                preview[sym] = {"status": alt["status"], "reasons": alt["reasons"]}
            except (KeyError, ValueError, TypeError, AttributeError, OSError) as exc:
                preview[sym] = {"status": "UNVERIFIED", "reasons": [f"{type(exc).__name__}"]}
        for p in summ["problems"]:
            problems.append(f"{sym}: {p}")
        for k in ("ledger_sha256", "pages_digest", "rows_sha256", "status", "coverage", "conflicts", "duplicates_identical", "rejects", "gap_count"):
            if summ[k] != rec.get(k) and not (summ["problems"] and k in ("status", "coverage")):
                problems.append(f"{sym}: {k} differs from the manifest (re-derived from the raw pages)")
        rp = os.path.join(sdir, "rows.jsonl")
        if not os.path.isfile(rp):
            problems.append(f"{sym}: rows.jsonl missing")
        elif _sha(open(rp, "rb").read()) != rec.get("rows_sha256"):
            problems.append(f"{sym}: rows.jsonl hash differs from the manifest")
        listed = {r["file"] for r in read_ledger(sdir)[0] if r["type"] == "page"}
        for n in sorted(os.listdir(sdir)):
            if re.fullmatch(r"page-[0-9]{6}\.json", n) and n not in listed:
                problems.append(f"{sym}: UNLISTED_FILE {n}")
        for dp, _dn, fn in os.walk(sdir):
            for f in fn:
                if os.stat(os.path.join(dp, f)).st_mode & 0o077:
                    problems.append(f"{sym}: {os.path.relpath(os.path.join(dp, f), sdir)} is group/world accessible")
    out = {"ok": not problems, "problems": problems, "symbols_checked": len(man["symbols"]), "pages_checked": pages, "dataset_status": man.get("dataset_status")}
    if accept:
        out["status_if_accepted"] = preview
        out["note_if_accepted"] = "a PREVIEW only: re-run the collection with the same --accept-regime-change (and the manifest's own --start-ms/--end-ms) to record the acceptance in the manifest"
    return out


# ---------------------------------------------------------------- spot instruments (is there a Bybit SPOT leg for this perp?) and hedge availability

SPOT = "spot-instruments"
SPOT_CATEGORY = "spot"
_COIN = re.compile(r"^[A-Z0-9]{1,24}\Z", re.ASCII)
_MULT_PREFIX = re.compile(r"^(10+)([A-Z].*)\Z", re.ASCII)


def spot_request_url() -> str:
    """ONE request, ``category=spot``, no pagination (owner-observed: one response lists every spot pair). A ``nextPageCursor`` in the reply is recorded and makes the
    snapshot PARTIAL; it is never followed here."""
    return f"{venues.rest_for('mainnet')}{UC.PATH_INSTRUMENTS}?{urllib.parse.urlencode([('category', SPOT_CATEGORY)])}"


def parse_spot(body: bytes) -> Dict[str, Any]:
    out: Dict[str, Any] = {"ok": False, "rows": [], "rejects": {}, "server_time": None, "duplicates_identical": 0, "conflicts": 0, "next_page_cursor": None, "raw_count": 0}
    rej = out["rejects"]
    try:
        doc = json.loads(body.decode("utf-8", "strict"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        _bump(rej, "envelope:not_utf8_json")
        return out
    if not isinstance(doc, dict):
        _bump(rej, "envelope:not_an_object")
        return out
    out["server_time"] = _uint_ts(doc.get("time"))
    res = doc.get("result")
    if isinstance(doc.get("retCode"), bool) or doc.get("retCode") != 0 or not isinstance(res, dict) or res.get("category") != SPOT_CATEGORY or not isinstance(res.get("list"), list):
        _bump(rej, "envelope:not_a_spot_list")
        return out
    out["ok"] = True
    nxt = res.get("nextPageCursor")
    out["next_page_cursor"] = nxt if isinstance(nxt, str) and nxt else None
    out["raw_count"] = len(res["list"])
    seen: Dict[str, Dict[str, str]] = {}
    for r in res["list"]:
        if not isinstance(r, dict):
            _bump(rej, "not_an_object")
            continue
        sym, base, quote, st = r.get("symbol"), r.get("baseCoin"), r.get("quoteCoin"), r.get("status")
        if not (isinstance(sym, str) and _SYMBOL.fullmatch(sym) and isinstance(base, str) and _COIN.fullmatch(base) and isinstance(quote, str) and _COIN.fullmatch(quote)
                and isinstance(st, str) and st.isascii() and 0 < len(st) <= 32):
            _bump(rej, "bad_row")
            continue
        row = {"symbol": sym, "baseCoin": base, "quoteCoin": quote, "status": st}
        if sym in seen:
            if seen[sym] == row:
                out["duplicates_identical"] += 1
            else:
                out["conflicts"] += 1
            continue
        seen[sym] = row
    out["rows"] = [seen[k] for k in sorted(seen)]
    return out


def capture_spot_instruments(root: str, *, fetch: Callable[[str], bytes] = http_fetch, clock: Callable[[], int] = lambda: int(time.time() * 1000),
                             sleep: Callable[[float], None] = time.sleep, reserve_bytes: int = MC.DEFAULT_RESERVE_BYTES,
                             free_bytes_fn: Callable[[str], int] = lambda p: shutil.disk_usage(p).free, max_retries: int = MC.MAX_RETRIES, backoff_s: float = MC.BACKOFF_S) -> Dict[str, Any]:
    """One spot instruments-info snapshot into ``<root>/spot-instruments/snapshot-<recv_ms>/`` (raw page 0600, rows.jsonl derived, manifest.json market_history/v1).
    observation_time = local_receive_time: an instrument list is knowable only when we read it (no earlier claim). Never filtered by quote coin: USDT, USDC, ... all kept."""
    root = os.path.abspath(root)
    guard = MC.root_guard(root, reserve_bytes, free_bytes_fn)
    if guard:
        return {"status": "REFUSED_ROOT", "problems": guard}
    os.makedirs(root, mode=0o700, exist_ok=True)
    base_dir = os.path.join(root, SPOT)
    os.makedirs(base_dir, mode=0o700, exist_ok=True)
    started = int(clock())
    fx = MC._Fetcher(fetch, sleep, max_retries, backoff_s)
    try:
        body = fx(spot_request_url())
    except (MC.RateLimitExhausted, MC.CollectorError, UC.CaptureError, OSError) as exc:
        return {"status": "FAILED", "reason": f"{type(exc).__name__}: {exc}", "rate_limit_events": len(fx.events)}
    recv = int(clock())
    if len(body) > MAX_BODY_BYTES:
        return {"status": "FAILED", "reason": "response larger than any instruments page"}
    sdir = os.path.join(base_dir, f"snapshot-{recv}")
    try:
        os.mkdir(sdir, 0o700)
    except FileExistsError:
        return {"status": "FAILED", "reason": "a snapshot with this receive time exists; nothing is overwritten"}
    UC._create_exclusive(os.path.join(sdir, "page-000001.json"), body)
    coll = int(clock())
    pr = parse_spot(body)
    lines = b"".join(_canon({"observation_time": recv, **r}) for r in pr["rows"])
    UC._create_exclusive(os.path.join(sdir, "rows.jsonl"), lines)
    norm = int(clock())
    skew = bool(pr["server_time"] is None or abs(recv - pr["server_time"]) > SKEW_WARN_MS)
    reasons = [r for r, c in (("rejects", pr["rejects"]), ("clock_skew", skew), ("venue_named_a_cursor_not_followed", pr["next_page_cursor"]), ("no_rows", not pr["rows"]),
                              ("envelope_not_ok", not pr["ok"])) if c]
    status = "CONFLICTED" if pr["conflicts"] else ("PARTIAL" if reasons else "COMPLETE")
    man = {"schema": SCHEMA, "dataset": SPOT, "request": {"host": venues.host_of(venues.rest_for("mainnet")), "path": UC.PATH_INSTRUMENTS, "category": SPOT_CATEGORY, "url": spot_request_url()},
           "started_ms": started, "dataset_status": status, "reasons": (["conflicting_duplicate"] if pr["conflicts"] else []) + reasons,
           "page": {"file": "page-000001.json", "sha256": _sha(body), "bytes": len(body), "source_server_time": pr["server_time"], "local_receive_time": recv, "collection_time": coll,
                    "next_page_cursor": pr["next_page_cursor"], "raw_rows": pr["raw_count"]},
           "normalization_time": norm, "rows": len(pr["rows"]), "rows_sha256": _sha(lines), "rejects": dict(sorted(pr["rejects"].items())), "duplicates_identical": pr["duplicates_identical"],
           "conflicts": pr["conflicts"], "by_status": {k: sum(1 for r in pr["rows"] if r["status"] == k) for k in sorted({r["status"] for r in pr["rows"]})},
           "by_quote": {k: sum(1 for r in pr["rows"] if r["quoteCoin"] == k) for k in sorted({r["quoteCoin"] for r in pr["rows"]})},
           "rate_limit_events": len(fx.events), "time_fields": list(TIME_FIELDS), "finished_ms": norm}
    man["manifest_digest"] = _sha(_canon({k: v for k, v in man.items() if k != "manifest_digest"}))
    _write_atomic(os.path.join(sdir, "manifest.json"), (json.dumps(man, indent=1, sort_keys=True, allow_nan=False) + "\n").encode())
    return {**man, "dir": sdir}


def verify_spot(sdir: str) -> Dict[str, Any]:
    problems: List[str] = []
    try:
        man = json.loads(open(os.path.join(sdir, "manifest.json"), "rb").read())
        body = open(os.path.join(sdir, "page-000001.json"), "rb").read()
        rows = open(os.path.join(sdir, "rows.jsonl"), "rb").read()
    except (OSError, ValueError):
        return {"ok": False, "problems": ["manifest.json, page-000001.json or rows.jsonl missing or unreadable"]}
    if man.get("schema") != SCHEMA or man.get("dataset") != SPOT:
        return {"ok": False, "problems": ["not a spot-instruments market_history/v1 manifest"]}
    if man.get("manifest_digest") != _sha(_canon({k: v for k, v in man.items() if k != "manifest_digest"})):
        problems.append("manifest_digest does not match the manifest's content")
    if _sha(body) != man["page"]["sha256"]:
        problems.append("PAGE_HASH_MISMATCH: page-000001.json")
    pr = parse_spot(body)
    want = b"".join(_canon({"observation_time": man["page"]["local_receive_time"], **r}) for r in pr["rows"])
    if _sha(want) != man.get("rows_sha256") or want != rows:
        problems.append("rows.jsonl does not re-derive from the raw page")
    return {"ok": not problems, "problems": problems, "dataset_status": man.get("dataset_status"), "rows": len(pr["rows"])}


def hedge_availability(records: Mapping[str, Mapping[str, Any]], spot_dir: str) -> Dict[str, Any]:
    """For each perp in a universe: is there a Bybit SPOT pair on the same base coin, quoted in the perp's quote coin, that is Trading? Answered from the RAW spot snapshot
    (re-verified first), never from memory. A multiplier contract (1000PEPE, 10000000AIDOGE) has no exact-name spot pair: it is reported UNRESOLVED_MULTIPLIER with the
    stripped-name candidate and is never assumed hedgeable (the unit ratio is not in the data)."""
    v = verify_spot(spot_dir)
    if not v["ok"]:
        raise HistoryError("spot snapshot does not verify: " + "; ".join(v["problems"][:3]))
    spot = {r["symbol"]: r for r in map(json.loads, open(os.path.join(spot_dir, "rows.jsonl"), "rb").read().splitlines())}
    by_base: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
    for r in spot.values():
        by_base.setdefault((r["baseCoin"], r["quoteCoin"]), []).append(r)
    out: Dict[str, Any] = {}
    counts: Dict[str, int] = {}
    for sym in sorted(records):
        rec = records[sym]
        base, quote = str(rec.get("baseCoin", "")), str(rec.get("quoteCoin", "USDT"))
        hit = by_base.get((base, quote), [])
        if hit:
            best = sorted(hit, key=lambda r: (r["status"] != "Trading", r["symbol"]))[0]
            verdict = "SPOT_TRADING" if best["status"] == "Trading" else "SPOT_NOT_TRADING"
            entry = {"verdict": verdict, "spot_symbol": best["symbol"], "spot_status": best["status"]}
        else:
            m = _MULT_PREFIX.match(base)
            cand = by_base.get((m.group(2), quote)) if m else None
            if cand:
                entry = {"verdict": "UNRESOLVED_MULTIPLIER", "spot_candidate": cand[0]["symbol"], "multiplier_prefix": m.group(1)}
            else:
                entry = {"verdict": "NO_SPOT_PAIR"}
        entry.update(perp_status=rec.get("status"), baseCoin=base, quoteCoin=quote)
        out[sym] = entry
        counts[entry["verdict"]] = counts.get(entry["verdict"], 0) + 1
    return {"spot_snapshot": os.path.abspath(spot_dir), "spot_rows": len(spot), "perps": len(out), "counts": dict(sorted(counts.items())), "by_symbol": out,
            "note": "answered from raw bytes that re-hash; a multiplier contract is never assumed hedgeable"}



# ---------------------------------------------------------------- run-plan: execute a SEALED plan dataset by dataset, resumable, into a NEW root

RUN_ORDER = (FUNDING, MARK, INDEX, KLINE, OI)                   # == backfill_plan.DATASET_ORDER (a test pins it): funding fixes the symbol set, mark follows for the SAME symbols
PROGRESS_NAME = "run-plan.progress.json"
PROGRESS_SCHEMA = "market_history/run_plan_progress/v1"
PROGRESS_LIST_CAP = 1000


def _plan_universe(plan_doc: Mapping[str, Any], universe_dir: Optional[str]) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, Any], List[str]]:
    """(records, uinfo, symbols) of a sealed plan: the capture it names (or the same one passed again), whose manifest sha256 must equal the plan's."""
    sts = list(plan_doc["parameters"].get("include_statuses") or [])
    records, uinfo = load_universe_dir(universe_dir or plan_doc["universe"]["dir"], include_statuses=sts)
    if uinfo["manifest_sha256"] != plan_doc["universe"]["manifest_sha256"]:
        raise HistoryError("the universe capture is not the one the plan was made from (manifest sha256 differs)")
    symbols = list(plan_doc["eligible_symbols"])
    absent = sorted(set(symbols) - set(records))
    if absent:
        raise HistoryError(f"plan symbols absent from the universe capture: {absent[:5]}")
    return records, dict(uinfo, symbols=len(symbols), symbols_from_plan={"plan_digest": plan_doc["plan_digest"], "eligible": len(symbols)}), symbols


class _Progress:
    """The machine-readable progress file of one run-plan: per dataset symbols done/remaining, request counts, an ETA LOWER bound (remaining planned requests / the shared rate; retries and
    backoff excluded) and the last error. Written atomically (0600) after every symbol and at every state change."""

    def __init__(self, path: str, doc: Dict[str, Any], clock: Callable[[], int], rate: float) -> None:
        self.path, self.doc, self.clock, self.rate = path, doc, clock, rate
        self.lock = threading.Lock()

    def flush(self) -> None:
        with self.lock:
            self._derive()
            _write_atomic(self.path, (json.dumps(self.doc, indent=1, sort_keys=True, allow_nan=False) + "\n").encode())

    def _derive(self) -> None:
        d = self.doc
        d["updated_ms"] = int(self.clock())
        rem = 0
        for ds, e in d["datasets"].items():
            e["symbols_remaining"] = sorted(set(e["symbols_all"]) - set(e["symbols_done"]))[:PROGRESS_LIST_CAP]
            e["symbols_remaining_count"] = len(set(e["symbols_all"]) - set(e["symbols_done"]))
            e["symbols_done_count"] = len(e["symbols_done"])
            if e["status"] != "DONE":
                rem += max(0, e["requests_planned"] - e["requests_made"])
        d["totals"] = {"requests_made": sum(e["requests_made"] for e in d["datasets"].values()), "pages_fetched": sum(e["pages_fetched"] for e in d["datasets"].values()),
                       "rate_limit_events": sum(e["rate_limit_events"] for e in d["datasets"].values()), "requests_planned_remaining": rem,
                       "eta_lower_bound_s": round(rem / self.rate, 1) if self.rate > 0 else None,
                       "eta_note": "LOWER bound: remaining planned requests / the shared token-bucket rate; retries, backoff and server latency are not in it"}

    def symbol_done(self, ds: str, sym: str, res: Mapping[str, Any]) -> None:
        with self.lock:
            e = self.doc["datasets"][ds]
            e["requests_made"] += int(res.get("requests") or 0)
            e["pages_fetched"] += int(res.get("pages_fetched") or 0)
            e["rate_limit_events"] += int(res.get("rate_limit_events") or 0)
            if res.get("stop_reason") and not res.get("errors"):
                if sym not in e["symbols_done"]:
                    e["symbols_done"].append(sym)
            elif res.get("errors"):
                self.doc["last_error"] = {"dataset": ds, "symbol": sym, "error": str(res["errors"][0])[:300], "at_ms": int(self.clock())}
        self.flush()


def run_plan(plan_path: str, out_root: str, *, universe_dir: Optional[str] = None, datasets: Optional[Sequence[str]] = None, fetch: Callable[[str], bytes] = http_fetch,
             clock: Callable[[], int] = lambda: int(time.time() * 1000), sleep: Callable[[float], None] = time.sleep, mono: Callable[[], float] = time.monotonic,
             workers: int = DEFAULT_WORKERS, rate: float = DEFAULT_RATE, max_pages: int = DEFAULT_MAX_PAGES, reserve_bytes: int = MC.DEFAULT_RESERVE_BYTES,
             free_bytes_fn: Callable[[str], int] = lambda p: shutil.disk_usage(p).free, max_retries: int = MC.MAX_RETRIES, backoff_s: float = MC.BACKOFF_S,
             stop_after_requests: Optional[int] = None) -> Dict[str, Any]:
    """Collect every dataset of a SEALED ``backfill_plan`` into ``out_root`` (a NEW directory: an existing non-empty root is accepted only to RESUME this same plan).

    * Resumable with no refetch: each symbol directory is an append-only, hash-chained ledger of raw pages; a symbol whose ledger ended (``stop``) costs zero requests on a re-run, a symbol
      interrupted mid-window resumes at its last verified page. The progress file records which symbols are done.
    * Never overwrites: refuses a root inside a git work tree or behind a link, a dataset directory that this plan's progress file does not own, a progress file of a different plan, and a window
      whose end a forming candle could still change. All writes stay under ``out_root``.
    * Same symbols everywhere: every dataset is built from the plan's ``eligible_symbols``; a finished dataset whose manifest holds a different symbol set stops the run (SYMBOL_SET_MISMATCH).
    * Respects the shared token bucket, bounded backoff and 429 handling of ``run_dataset``; funding-interval regime handling (``unproven_regime_change``) is untouched.
    A dataset with any non-COMPLETE symbol ends the run (exit non-zero, ``last_error`` set): re-running resumes it. ``stop_after_requests`` is a test hook."""
    plan_doc = load_plan(plan_path)
    par = plan_doc["parameters"]
    want = [d for d in RUN_ORDER if d in (datasets if datasets is not None else par.get("required_datasets") or (FUNDING, MARK))]
    if not want:
        raise HistoryError("run-plan: no dataset selected")
    if not 1 <= workers <= MAX_WORKERS or not rate > 0:
        raise HistoryError(f"workers must be 1..{MAX_WORKERS} and rate > 0")
    root = os.path.abspath(out_root)
    pdoc = plan_doc["window"]
    start_ms, end_ms = int(pdoc["effective_from_ms"]), int(pdoc["effective_to_ms"])
    ivs = par.get("intervals") or {}
    for ds in want:
        interval = None if ds == FUNDING else ivs.get(ds)
        if ds != FUNDING and interval_ms_of(ds, interval) is None:
            raise HistoryError(f"run-plan: the plan names no usable interval for {ds}")
        limit_end = int(clock()) if ds == FUNDING else last_closed_end_ms(int(clock()), interval, ds)
        if end_ms > limit_end:
            raise HistoryError(f"WINDOW_END_NOT_CLOSED: the plan's window end {end_ms} is later than the last closed {'moment' if ds == FUNDING else 'candle boundary'} {limit_end} for {ds}: "
                               "a candle the venue has not closed would be requested; re-plan with an earlier --study-to-ms")
    guard = MC.root_guard(root, reserve_bytes, free_bytes_fn)
    if guard:
        raise HistoryError("run-plan: refused root: " + "; ".join(guard))
    ppath = os.path.join(root, PROGRESS_NAME)
    prior: Optional[Dict[str, Any]] = None
    if os.path.isdir(root):
        existing = sorted(n for n in os.listdir(root) if n not in (MC.LOCK_NAME,))
        if existing:
            if os.path.islink(ppath) or not os.path.isfile(ppath):
                raise HistoryError(f"run-plan: {root} is not empty and holds no {PROGRESS_NAME} of this plan: refusing to write beside (or over) an existing dataset; give a NEW directory")
            try:
                with open(ppath, "rb") as fh:
                    prior = json.loads(fh.read())
            except ValueError as exc:
                raise HistoryError("run-plan: the progress file is not JSON") from exc
            if not isinstance(prior, dict) or prior.get("schema") != PROGRESS_SCHEMA or prior.get("plan_digest") != plan_doc["plan_digest"]:
                raise HistoryError("run-plan: the progress file belongs to a different plan: refusing to mix two plans in one root; give a NEW directory")
            owned = set((prior.get("datasets") or {}))
            foreign = [n for n in existing if n not in (PROGRESS_NAME,) and n not in {dataset_dirname(d, None if d == FUNDING else ivs.get(d)) for d in owned}]
            if foreign:
                raise HistoryError(f"run-plan: {root} holds {foreign[:3]} that this plan's progress file does not own: refusing")
    else:
        os.makedirs(root, mode=0o700)
    os.chmod(root, 0o700)
    records, uinfo, symbols = _plan_universe(plan_doc, universe_dir)
    est = {d: (plan_doc.get("datasets", {}).get(d, {}).get("estimate") or {}).get("requests_total", 0) for d in RUN_ORDER}
    base: Dict[str, Any] = {"schema": PROGRESS_SCHEMA, "plan_digest": plan_doc["plan_digest"], "plan_path": os.path.abspath(plan_path), "out_root": root, "status": "RUNNING",
                            "started_ms": int(clock()), "window": {"start_ms": start_ms, "end_ms": end_ms}, "workers": workers, "rate_per_s": rate, "last_error": None, "datasets": {}}
    for ds, entry in ((prior or {}).get("datasets") or {}).items():
        if ds not in want and isinstance(entry, dict):
            base["datasets"][ds] = entry                        # a subset resume leaves every other dataset's record exactly as it was
    for ds in want:
        interval = None if ds == FUNDING else ivs.get(ds)
        old = ((prior or {}).get("datasets") or {}).get(ds) or {}
        base["datasets"][ds] = {"dirname": dataset_dirname(ds, interval), "interval": interval, "status": old.get("status", "PENDING"), "symbols_all": sorted(symbols),
                                "symbols_done": list(old.get("symbols_done", [])), "requests_planned": int(est.get(ds) or 0), "requests_made": int(old.get("requests_made", 0)),
                                "pages_fetched": int(old.get("pages_fetched", 0)), "rate_limit_events": int(old.get("rate_limit_events", 0)), "manifest_digest": old.get("manifest_digest"),
                                "dataset_status": old.get("dataset_status")}
    if prior and prior.get("started_ms"):
        base["started_ms"] = prior["started_ms"]
        base["resumed"] = int(prior.get("resumed", 0)) + 1
    prog = _Progress(ppath, base, clock, rate)
    with MC._locked(root):
        prog.flush()
        sel = {"mode": "FROM_PLAN", "plan_digest": plan_doc["plan_digest"]}
        n_requests = [0]
        for ds in want:
            e = base["datasets"][ds]
            interval = e["interval"]
            ddir = os.path.join(root, e["dirname"])
            if e["status"] == "DONE":
                v = verify_dataset(ddir)
                man_path = os.path.join(ddir, "manifest.json")
                digest = None
                if os.path.isfile(man_path):
                    with open(man_path, "rb") as fh:
                        digest = json.loads(fh.read()).get("manifest_digest")
                if v["ok"] and digest == e["manifest_digest"]:
                    continue                                    # already finished and re-verified: zero requests
                raise HistoryError(f"run-plan: dataset {e['dirname']} is marked DONE but no longer verifies / matches its recorded digest: refusing to touch it")
            specs = build_specs(ds, interval, symbols, start_ms, end_ms, records)
            e["status"] = "RUNNING"
            prog.flush()

            def hook(sym: str, res: Mapping[str, Any], _ds: str = ds) -> None:
                prog.symbol_done(_ds, sym, res)

            fetch_w = fetch
            if stop_after_requests is not None:
                def fetch_w(url: str, _f: Callable[[str], bytes] = fetch) -> bytes:        # noqa: E306 - test hook: simulate an interruption after N requests
                    if n_requests[0] >= stop_after_requests:
                        raise KeyboardInterrupt("stop_after_requests")
                    n_requests[0] += 1
                    return _f(url)
            try:
                man = run_dataset(root, ds, interval, specs, fetch=fetch_w, clock=clock, sleep=sleep, mono=mono, workers=workers, rate=rate, max_pages=max_pages, reserve_bytes=reserve_bytes,
                                  free_bytes_fn=free_bytes_fn, universe=uinfo, window_end_basis="PLAN_WINDOW", max_retries=max_retries, backoff_s=backoff_s, selection=sel, on_symbol=hook)
            except BaseException as exc:                        # noqa: BLE001 - an interruption is recorded, then re-raised: the progress file must say where it stopped
                e["status"] = "INTERRUPTED"
                base["status"] = "INTERRUPTED"
                base["last_error"] = {"dataset": ds, "error": f"{type(exc).__name__}: {str(exc)[:200]}", "at_ms": int(clock())}
                prog.flush()
                raise
            if man.get("status") == "REFUSED_ROOT":
                e["status"] = "FAILED"
                base["status"] = "FAILED"
                base["last_error"] = {"dataset": ds, "error": "root refused: " + "; ".join(man.get("problems", []))[:200], "at_ms": int(clock())}
                prog.flush()
                return base
            got = set(man["symbols"])
            if got != set(symbols):
                e["status"] = "FAILED"
                base["status"] = "FAILED"
                base["last_error"] = {"dataset": ds, "error": f"SYMBOL_SET_MISMATCH: dataset holds {len(got)} symbols, the plan {len(symbols)} (only in dataset {sorted(got - set(symbols))[:3]}, "
                                      f"only in plan {sorted(set(symbols) - got)[:3]}; lifecycle skips {sorted(man.get('excluded', {}))[:3]})", "at_ms": int(clock())}
                prog.flush()
                return base
            e["dataset_status"], e["manifest_digest"] = man["dataset_status"], man["manifest_digest"]
            e["symbols_done"] = sorted(s for s, r in man["symbols"].items() if r.get("status") == "COMPLETE")
            if man["dataset_status"] != "COMPLETE":
                e["status"] = "INCOMPLETE"
                base["status"] = "INCOMPLETE"
                bad = {s: r.get("reasons") for s, r in man["symbols"].items() if r.get("status") != "COMPLETE"}
                base["last_error"] = base["last_error"] or {"dataset": ds, "error": f"{len(bad)} symbol(s) not COMPLETE (first: {dict(list(bad.items())[:3])}); re-run resumes", "at_ms": int(clock())}
                prog.flush()
                return base                                      # stop: later datasets are not started until this one is whole (a 429 storm must not be fanned out)
            e["status"] = "DONE"
            prog.flush()
        base["status"] = "DONE"
        prog.flush()
    return base

# ---------------------------------------------------------------- CLI

PLAN_SCHEMA = "backfill_plan/v1"
MAX_PLAN_BYTES = 16 << 20


def plan_digest(doc: Mapping[str, Any]) -> str:
    """The digest a ``backfill_plan`` document seals itself with (everything but ``plan_digest``)."""
    return _sha(_canon({k: v for k, v in doc.items() if k != "plan_digest"}))


def load_plan(path: str) -> Dict[str, Any]:
    """A ``tools/backfill_plan.py plan --out`` document, refused unless it re-hashes: the SYMBOL SET of every dataset comes from one sealed list, so the datasets cannot diverge."""
    if os.path.islink(path) or not os.path.isfile(path):
        raise HistoryError("--symbols-from-plan: no such file (or a link)")
    with open(path, "rb") as fh:
        raw = fh.read(MAX_PLAN_BYTES + 1)
    if len(raw) > MAX_PLAN_BYTES:
        raise HistoryError("--symbols-from-plan: file too large")
    try:
        doc = json.loads(raw)
    except ValueError as exc:
        raise HistoryError("--symbols-from-plan: not JSON") from exc
    if not isinstance(doc, dict) or doc.get("schema") != PLAN_SCHEMA:
        raise HistoryError("--symbols-from-plan: not a backfill_plan/v1 document")
    if doc.get("plan_digest") != plan_digest(doc):
        raise HistoryError("--symbols-from-plan: plan_digest does not match the plan's content (edited after it was made?)")
    syms = doc.get("eligible_symbols")
    if not isinstance(syms, list) or not syms or not all(isinstance(s, str) and _SYMBOL.fullmatch(s) for s in syms) or len(set(syms)) != len(syms) or doc.get("eligible_count") != len(syms):
        raise HistoryError("--symbols-from-plan: eligible_symbols is empty, malformed or does not match eligible_count")
    uni, win = doc.get("universe"), doc.get("window")
    if not isinstance(uni, dict) or not isinstance(uni.get("dir"), str) or not isinstance(uni.get("manifest_sha256"), str) or not isinstance(win, dict):
        raise HistoryError("--symbols-from-plan: the plan names no universe or window")
    return doc


def _parse_overrides(items: Sequence[str]) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for it in items or []:
        sym, _, mins = it.partition("=")
        if not _SYMBOL.fullmatch(sym) or not mins.isdigit() or not 0 < int(mins) <= 1440:
            raise HistoryError(f"--funding-interval-min wants SYMBOL=MINUTES, got {it!r}")
        out[sym] = int(mins)
    return out


def _parse_accepts(items: Sequence[str]) -> Dict[str, List[Tuple[int, int]]]:
    out: Dict[str, List[Tuple[int, int]]] = {}
    for it in items or []:
        parts = it.split(":")
        if len(parts) != 3 or not _SYMBOL.fullmatch(parts[0]) or not all(x.isdigit() and 0 < int(x) <= 1440 for x in parts[1:]) or parts[1] == parts[2]:
            raise HistoryError(f"--accept-regime-change wants SYMBOL:from_min:to_min (two different minute counts), got {it!r}")
        out.setdefault(parts[0], []).append((int(parts[1]), int(parts[2])))
    return out


def last_closed_end_ms(now_ms: int, interval: str, dataset: str) -> int:
    """The newest endTime that cannot include a candle still forming at ``now_ms``: one ms before the last CLOSED boundary,
    floor((now - safety) / interval) * interval (weekly candles start on a Monday, 4 days after the epoch)."""
    iv = interval_ms_of(dataset, interval)
    if not iv:
        raise HistoryError("no fixed interval grid")
    off = 4 * 86_400_000 if dataset != OI and interval == "W" else 0
    boundary = ((now_ms - CLOSED_SAFETY_MS - off) // iv) * iv + off
    return boundary - 1


def main(argv: Optional[Sequence[str]] = None, *, fetch: Callable[[str], bytes] = http_fetch, clock: Callable[[], int] = lambda: int(time.time() * 1000),
         sleep: Callable[[float], None] = time.sleep, mono: Callable[[], float] = time.monotonic,
         free_bytes_fn: Callable[[str], int] = lambda p: shutil.disk_usage(p).free) -> int:
    ap = argparse.ArgumentParser(description="Owner-invoked PUBLIC history ingestion (GET only, no credential): funding, kline, mark-kline, index-kline, open-interest.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in DATASETS:
        p = sub.add_parser(name)
        p.add_argument("--out-root", required=True, help="data root OUTSIDE the git work tree (0700)")
        g = p.add_mutually_exclusive_group(required=False)
        g.add_argument("--symbols", help="comma-separated, e.g. BTCUSDT,ETHUSDT")
        g.add_argument("--universe-dir", "--universe-capture", dest="universe_dir", help="a universe_capture directory (verified before any symbol is taken from it)")
        p.add_argument("--symbols-from-plan", metavar="PLAN.json", help="the symbol list (and window) of a sealed tools/backfill_plan.py plan: every dataset collected with the same plan has the same symbols. "
                       "Combine with --universe-capture/--include-statuses only to point at the same capture; the plan's universe sha256 must match")
        p.add_argument("--include-statuses", help="with --universe-capture: comma-separated instrument statuses, e.g. Trading,Closed (Closed contracts are asked up to their deliveryTime)")
        p.add_argument("--only-trading", action="store_true", help="with --universe-dir: only status Trading (default: every accepted instrument, delisted history included)")
        p.add_argument("--start-ms", type=int, help="window start (epoch ms)")
        p.add_argument("--days", type=int, help="window start = end - DAYS")
        p.add_argument("--end-ms", type=int, help="window end (default: funding = now; kline/mark/index/open-interest = the last CLOSED candle boundary, so a forming candle is never requested)")
        if DATASETS[name]["interval_param"]:
            p.add_argument("--interval", required=True, help=("one of " + ",".join(sorted(OI_INTERVALS_MS))) if name == OI else ("one of " + ",".join(KLINE_INTERVALS_MS)))
        if name == FUNDING:
            p.add_argument("--funding-interval-min", action="append", default=[], metavar="SYMBOL=MIN", help="operator-declared interval when there is no universe record")
            p.add_argument("--accept-regime-change", action="append", default=[], metavar="SYMBOL:FROM_MIN:TO_MIN",
                           help="the operator checked the instrument history: this funding interval change is genuine (without it an interval change is PARTIAL unproven_regime_change)")
        p.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
        p.add_argument("--rate", type=float, default=DEFAULT_RATE, help="shared requests/second")
        p.add_argument("--max-pages", type=int, default=DEFAULT_MAX_PAGES)
        p.add_argument("--reserve-bytes", type=int, default=MC.DEFAULT_RESERVE_BYTES)
    rp = sub.add_parser("run-plan", help="execute a SEALED backfill_plan dataset by dataset into a NEW root: resumable (no refetch), rate-limited, progress file, never overwrites")
    rp.add_argument("--plan", required=True, metavar="PLAN.json")
    rp.add_argument("--out-root", required=True, help="a NEW directory outside the git work tree (an existing one is accepted only to resume this same plan)")
    rp.add_argument("--universe-capture", default=None, help="the capture the plan was made from (default: the plan's own path); its manifest sha256 must match")
    rp.add_argument("--datasets", default=None, help="comma-separated subset/order source (default: the plan's required datasets); 'all' = every dataset")
    rp.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    rp.add_argument("--rate", type=float, default=DEFAULT_RATE, help="shared requests/second")
    rp.add_argument("--max-pages", type=int, default=DEFAULT_MAX_PAGES)
    rp.add_argument("--reserve-bytes", type=int, default=MC.DEFAULT_RESERVE_BYTES)
    sp = sub.add_parser("spot-instruments", help="one spot instruments-info snapshot (category=spot, single request, no pagination)")
    sp.add_argument("--out-root", required=True)
    sp.add_argument("--reserve-bytes", type=int, default=MC.DEFAULT_RESERVE_BYTES)
    ha = sub.add_parser("hedge-availability", help="which perps have a Bybit spot leg, from a verified universe capture and a verified spot snapshot (no network)")
    ha.add_argument("--universe-capture", required=True)
    ha.add_argument("--spot-dir", required=True, help="a <root>/spot-instruments/snapshot-<ms> directory")
    ha.add_argument("--include-statuses", help="e.g. Trading,Closed (default: all)")
    v = sub.add_parser("verify", help="re-hash and re-derive one dataset directory (no network)")
    v.add_argument("--dir", required=True)
    v.add_argument("--accept-regime-change", action="append", default=[], metavar="SYMBOL:FROM_MIN:TO_MIN", help="preview the status with these changes corroborated (does not alter the manifest)")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "spot-instruments":
            res = capture_spot_instruments(a.out_root, fetch=fetch, clock=clock, sleep=sleep, reserve_bytes=a.reserve_bytes, free_bytes_fn=free_bytes_fn)
            print(json.dumps({k: res[k] for k in res if k not in ("page",)}, indent=2, sort_keys=True))
            return 0 if res.get("dataset_status") == "COMPLETE" else (2 if res.get("status") in ("FAILED", "REFUSED_ROOT") else 1)
        if a.cmd == "run-plan":
            res = run_plan(a.plan, a.out_root, universe_dir=a.universe_capture, datasets=(list(RUN_ORDER) if a.datasets == "all" else [x for x in a.datasets.split(",") if x]) if a.datasets else None,
                           fetch=fetch, clock=clock, sleep=sleep, mono=mono, workers=a.workers, rate=a.rate, max_pages=a.max_pages, reserve_bytes=a.reserve_bytes, free_bytes_fn=free_bytes_fn)
            print(json.dumps({"status": res["status"], "out_root": res["out_root"], "totals": res["totals"], "last_error": res["last_error"],
                              "datasets": {d: {k: e.get(k) for k in ("status", "dataset_status", "symbols_done_count", "symbols_remaining_count", "requests_made")} for d, e in res["datasets"].items()},
                              "progress_file": os.path.join(res["out_root"], PROGRESS_NAME)}, indent=2, sort_keys=True))
            return 0 if res["status"] == "DONE" else 1
        if a.cmd == "hedge-availability":
            recs, _info = load_universe_dir(a.universe_capture, include_statuses=[x for x in a.include_statuses.split(",") if x] if a.include_statuses else None)
            print(json.dumps(hedge_availability(recs, a.spot_dir), indent=2, sort_keys=True))
            return 0
        if a.cmd == "verify":
            d = a.dir
            if os.path.basename(os.path.abspath(d)).startswith("snapshot-"):
                res = verify_spot(d)
                print(json.dumps(res, indent=2, sort_keys=True))
                return 0 if res["ok"] else 1
            res = verify_dataset(a.dir, _parse_accepts(a.accept_regime_change))
            print(json.dumps(res, indent=2, sort_keys=True))
            return 0 if res["ok"] else 1
        interval = getattr(a, "interval", None)
        plan_doc = load_plan(a.symbols_from_plan) if a.symbols_from_plan else None
        if plan_doc is None and (a.symbols is None) == (a.universe_dir is None):
            raise HistoryError("give exactly one of --symbols / --universe-capture, or --symbols-from-plan")
        if plan_doc is not None and a.symbols is not None:
            raise HistoryError("--symbols-from-plan replaces --symbols: give one")
        if a.end_ms is not None:
            end_ms, end_basis = a.end_ms, "OPERATOR_END_MS"
        elif a.cmd == FUNDING:
            end_ms, end_basis = int(clock()), "CLOCK_NOW"
        else:
            end_ms, end_basis = last_closed_end_ms(int(clock()), interval, a.cmd), "LAST_CLOSED_BOUNDARY"
        if (a.start_ms is None) == (a.days is None):
            raise HistoryError("give exactly one of --start-ms / --days")
        start_ms = a.start_ms if a.start_ms is not None else end_ms - a.days * 86_400_000
        records = uinfo = None
        if plan_doc is not None:
            pw = plan_doc["window"]
            if (start_ms, end_ms) != (pw.get("effective_from_ms"), pw.get("effective_to_ms")):
                raise HistoryError(f"the window ({start_ms}..{end_ms}) differs from the plan's ({pw.get('effective_from_ms')}..{pw.get('effective_to_ms')}): "
                                   f"use --days {pw.get('effective_days')} --end-ms {pw.get('effective_to_ms')}")
            sts = [x for x in a.include_statuses.split(",") if x] if a.include_statuses else list(plan_doc["parameters"].get("include_statuses") or [])
            if sorted(sts) != sorted(plan_doc["parameters"].get("include_statuses") or []):
                raise HistoryError("--include-statuses differs from the plan's")
            records, uinfo = load_universe_dir(a.universe_dir or plan_doc["universe"]["dir"], include_statuses=sts)
            if uinfo["manifest_sha256"] != plan_doc["universe"]["manifest_sha256"]:
                raise HistoryError("the universe capture is not the one the plan was made from (manifest sha256 differs)")
            symbols = list(plan_doc["eligible_symbols"])
            absent = sorted(set(symbols) - set(records))
            if absent:
                raise HistoryError(f"plan symbols absent from the universe capture: {absent[:5]}")
            uinfo = dict(uinfo, symbols=len(symbols), symbols_from_plan={"plan_digest": plan_doc["plan_digest"], "eligible": len(symbols)})
        elif a.universe_dir:
            records, uinfo = load_universe_dir(a.universe_dir, only_trading=a.only_trading,
                                               include_statuses=[x for x in a.include_statuses.split(",") if x] if a.include_statuses else None)
            symbols = sorted(records)
        else:
            symbols = [s for s in a.symbols.split(",") if s]
        ov = _parse_overrides(getattr(a, "funding_interval_min", []))
        specs = build_specs(a.cmd, interval, symbols, start_ms, end_ms, records, ov, _parse_accepts(getattr(a, "accept_regime_change", [])))
        selection = {"mode": "FROM_PLAN", "plan_digest": plan_doc["plan_digest"]} if plan_doc is not None else {"mode": "UNIVERSE" if a.universe_dir else "EXPLICIT_SYMBOLS", "plan_digest": None}
        man = run_dataset(a.out_root, a.cmd, interval, specs, fetch=fetch, clock=clock, sleep=sleep, mono=mono, workers=a.workers, rate=a.rate, max_pages=a.max_pages,
                          reserve_bytes=a.reserve_bytes, free_bytes_fn=free_bytes_fn, universe=uinfo, window_end_basis=end_basis, selection=selection)
    except (HistoryError, MC.CollectorError, UC.CaptureError, OSError) as exc:
        print(f"market_history: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    if man.get("status") == "REFUSED_ROOT":
        print(json.dumps(man, indent=2, sort_keys=True))
        return 2
    summary = {"dataset": man["dataset"], "dataset_status": man["dataset_status"], "counts": man["counts"],
               "symbols": {s: {"status": r["status"], "reasons": r.get("reasons", []), "rows": r.get("coverage", {}).get("rows")} for s, r in man["symbols"].items()}}
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0 if man["dataset_status"] == "COMPLETE" else 1


if __name__ == "__main__":
    sys.exit(main())
