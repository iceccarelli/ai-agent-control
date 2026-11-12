#!/usr/bin/env python3
"""RESEARCH ONLY: a verified local copy of Binance's PUBLIC data archive (https://data.binance.vision). It is never a venue.

What this is. Binance publishes, as static files, monthly zips of USD-M futures history per symbol (funding prints, mark / index / trade klines) and of spot klines, each with a ``.CHECKSUM`` file, and it keeps the
delisted contracts. The sandbox cannot reach Bybit, so this is the only way to get survivorship-complete funding and price history here. The data is evidence about the MECHANISM on another venue: it is not Bybit data,
it is not an account, it is not capacity, and nothing here can place an order (the module imports no client, no signer, no credential, no environment and no shell, and sends one method, GET, to two hosts).

What it guarantees. A URL is built only from validated parts. A month is kept only if its bytes equal the sha256 in its own ``.CHECKSUM`` (same origin: integrity, not authenticity), its zip holds exactly the expected
member within a size cap, and every row parses (finite, positive prices, strictly increasing times, a stated funding interval); anything else is a counted reject (``REJECTS.jsonl``), never repaired and never stored
under its final name. The store is private (0700/0600), outside git, written atomically, append-logged (``MANIFEST.jsonl``), resumable, and re-verified against its checksum on every read. A 403 or 451 stops the
run and is never a licence to change host or path. A caller must state the last month it wants (``until_month``): months after it are never requested, which is how a pre-registered study keeps its reserved months unread.

    python3 tools/binance_archive.py symbols --dataset fundingRate
    python3 tools/binance_archive.py months --dataset klines --symbol BTCUSDT --interval 1d
    python3 tools/binance_archive.py download --root DIR --dataset klines --interval 1d --symbols BTCUSDT,ETHUSDT --from-month 2020-01 --until-month 2024-09
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import http.client
import io
import json
import math
import os
import re
import shutil
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

SCHEMA = "binance_archive_store/v1"
HOST_FILES = "data.binance.vision"
HOST_LISTING = "s3-ap-northeast-1.amazonaws.com"
LISTING_PATH = "/" + HOST_FILES
USER_AGENT = "ai-agent-control-research/1 (public data archive reader)"
MAX_ZIP_BYTES = 64 * 1024 * 1024               # a monthly file is a few MB at most
MAX_CSV_BYTES = 32 * 1024 * 1024
MAX_LISTING_BYTES = 8 * 1024 * 1024
MAX_LISTING_PAGES = 500
RESERVE_BYTES = 256 * 1024 * 1024
ABSENT_TTL_S = 7 * 86_400
FUNDING_INTERVALS_H = (1, 2, 4, 8)
MAX_ABS_FUNDING_RATE = 1.0                       # a funding rate of more than 100% per print is not a funding rate

#: dataset -> (market path, interval required, archive directory name)
DATASETS: Dict[str, Tuple[str, bool, str]] = {
    "fundingRate": ("futures/um", False, "fundingRate"),
    "klines": ("futures/um", True, "klines"),
    "markPriceKlines": ("futures/um", True, "markPriceKlines"),
    "indexPriceKlines": ("futures/um", True, "indexPriceKlines"),
    "spot_klines": ("spot", True, "klines"),
}
INTERVALS = ("1h", "8h", "1d")
_SYMBOL = re.compile(r"^[A-Z0-9]{2,24}$")
_MONTH = re.compile(r"^(\d{4})-(0[1-9]|1[0-2])$")
_PREFIX = re.compile(r"^data/(?:futures/um|spot)/monthly/[A-Za-z]+/(?:[A-Z0-9]{2,24}/(?:(?:1h|8h|1d)/)?)?$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")

FUNDING_HEADER = "calc_time,funding_interval_hours,last_funding_rate"
KLINE_HEADER = "open_time,open,high,low,close,volume,close_time,quote_volume,count,taker_buy_volume,taker_buy_quote_volume,ignore"


class ArchiveError(Exception):
    """Every failure carries a stable ``code``; the message says what to do (and what not to do)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


# ---------------------------------------------------------------- URLs: built only from validated parts
def _dataset(dataset: Any) -> Tuple[str, bool, str]:
    if not isinstance(dataset, str) or dataset not in DATASETS:
        raise ArchiveError("BAD_ARGUMENT", f"dataset {dataset!r} is not registered: {sorted(DATASETS)}")
    return DATASETS[dataset]


def _check_parts(dataset: Any, symbol: Any, interval: Any, month: Any) -> Tuple[str, str, str, str]:
    market, needs_interval, dirname = _dataset(dataset)
    if not isinstance(symbol, str) or not _SYMBOL.match(symbol):
        raise ArchiveError("BAD_ARGUMENT", f"symbol {symbol!r} is not [A-Z0-9]{{2,24}}")
    if needs_interval:
        if interval not in INTERVALS:
            raise ArchiveError("BAD_ARGUMENT", f"interval {interval!r} is not one of {INTERVALS}")
    elif interval is not None:
        raise ArchiveError("BAD_ARGUMENT", f"{dataset} has no interval, got {interval!r}")
    if not isinstance(month, str) or not _MONTH.match(month):
        raise ArchiveError("BAD_ARGUMENT", f"month {month!r} is not YYYY-MM")
    return market, dirname, symbol, month


def data_path(dataset: str, symbol: str, interval: Optional[str], month: str, ext: str = "zip") -> str:
    market, dirname, symbol, month = _check_parts(dataset, symbol, interval, month)
    if ext not in ("zip", "zip.CHECKSUM"):
        raise ArchiveError("BAD_ARGUMENT", f"extension {ext!r}")
    if interval is None:
        return f"data/{market}/monthly/{dirname}/{symbol}/{symbol}-{dirname}-{month}.{ext}"
    return f"data/{market}/monthly/{dirname}/{symbol}/{interval}/{symbol}-{interval}-{month}.{ext}"


def data_url(dataset: str, symbol: str, interval: Optional[str], month: str, ext: str = "zip") -> str:
    return f"https://{HOST_FILES}/{data_path(dataset, symbol, interval, month, ext)}"


def listing_url(prefix: str, marker: str = "", delimiter: bool = True) -> str:
    if not isinstance(prefix, str) or not _PREFIX.match(prefix):
        raise ArchiveError("BAD_ARGUMENT", f"listing prefix {prefix!r} is not a dataset, symbol or symbol/interval directory of the archive")
    if marker and (not isinstance(marker, str) or not marker.startswith(prefix) or ".." in marker or len(marker) > 400):
        raise ArchiveError("BAD_ARGUMENT", "listing marker is not inside the prefix")
    q: List[Tuple[str, str]] = []
    if delimiter:
        q.append(("delimiter", "/"))
    q.append(("prefix", prefix))
    if marker:
        q.append(("marker", marker))
    q.append(("max-keys", "1000"))
    return f"https://{HOST_LISTING}{LISTING_PATH}?{urllib.parse.urlencode(q)}"


# ---------------------------------------------------------------- transport: GET, two hosts, no redirect, capped
class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_a: Any, **_k: Any) -> None:             # a redirect is another host: refuse it
        return None


def _check_url(url: Any) -> None:
    bad = ArchiveError("HOST_NOT_ALLOWED", f"only https GETs of {HOST_FILES} (/data/...) and the {HOST_LISTING} listing of its bucket are made; got {str(url)[:80]!r}")
    if not isinstance(url, str) or any(c in url for c in "\r\n\t \x00") or "\\" in url:
        raise bad
    u = urllib.parse.urlsplit(url)
    if u.scheme != "https" or u.username is not None or u.password is not None or u.port is not None or u.fragment:
        raise bad
    host = (u.hostname or "").lower()
    if "@" in u.netloc or host not in (HOST_FILES, HOST_LISTING):
        raise bad
    segs = urllib.parse.unquote(u.path).split("/")
    if ".." in segs or "." in segs or "%" in urllib.parse.unquote(u.path):
        raise bad
    if host == HOST_FILES:
        if not u.path.startswith("/data/") or u.query:
            raise bad
    else:
        if u.path != LISTING_PATH:
            raise bad
        allowed = {"delimiter", "prefix", "marker", "max-keys"}
        if any(k not in allowed for k, _ in urllib.parse.parse_qsl(u.query, keep_blank_values=True)):
            raise bad


def http_fetch(url: str, timeout: float = 30.0, max_bytes: int = MAX_ZIP_BYTES) -> Tuple[int, bytes]:
    """(status, body). An HTTP error status is returned, not raised; a network failure is ``NETWORK_ERROR``. At most ``max_bytes + 1`` bytes are read."""
    _check_url(url)
    opener = urllib.request.build_opener(_NoRedirect)
    req = urllib.request.Request(url, method="GET", headers={"User-Agent": USER_AGENT, "Accept": "*/*"})
    try:
        with opener.open(req, timeout=timeout) as resp:
            return int(resp.status), resp.read(int(max_bytes) + 1)
    except urllib.error.HTTPError as exc:
        return int(exc.code), b""
    except (urllib.error.URLError, OSError, http.client.HTTPException) as exc:
        why = getattr(exc, "reason", None)
        raise ArchiveError("NETWORK_ERROR", f"request failed ({type(exc).__name__}: {type(why).__name__ if why is not None else str(exc)[:60]})") from exc


class Throttle:
    """A token bucket shared by every worker; ``monotonic`` and ``sleep`` are injected so tests never wait."""

    def __init__(self, rate: float, burst: int, monotonic: Callable[[], float], sleep: Callable[[float], None]) -> None:
        if not rate > 0 or burst < 1:
            raise ArchiveError("BAD_ARGUMENT", "rate must be > 0 and burst >= 1")
        self.rate, self.burst, self.monotonic, self.sleep = float(rate), float(burst), monotonic, sleep
        self.tokens = float(burst)
        self.last = monotonic()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        while True:
            with self._lock:
                now = self.monotonic()
                self.tokens = min(self.burst, self.tokens + max(0.0, now - self.last) * self.rate)
                self.last = now
                if self.tokens >= 1.0 - 1e-9:
                    self.tokens = max(0.0, self.tokens - 1.0)
                    return
                wait = (1.0 - self.tokens) / self.rate
            self.sleep(wait)


# ---------------------------------------------------------------- rows
def _finite(x: str, what: str, line: int) -> float:
    try:
        v = float(x)
    except ValueError:
        raise ArchiveError("ROWS_INVALID", f"line {line}: {what} {x!r} is not a number") from None
    if not math.isfinite(v):
        raise ArchiveError("ROWS_INVALID", f"line {line}: {what} is not finite")
    return v


def _int(x: str, what: str, line: int) -> int:
    try:
        return int(x)
    except ValueError:
        raise ArchiveError("ROWS_INVALID", f"line {line}: {what} {x!r} is not an integer") from None


def month_bounds_ms(month: str) -> Tuple[int, int]:
    """[start, end) of a UTC calendar month in ms."""
    if not _MONTH.match(month):
        raise ArchiveError("BAD_ARGUMENT", f"month {month!r} is not YYYY-MM")
    y, m = int(month[:4]), int(month[5:])
    ny, nm = (y + 1, 1) if m == 12 else (y, m + 1)
    epoch_days = lambda yy, mm: (datetime.date(yy, mm, 1) - datetime.date(1970, 1, 1)).days
    return epoch_days(y, m) * 86_400_000, epoch_days(ny, nm) * 86_400_000


def parse_rows(dataset: str, text: str, month: Optional[str] = None) -> List[Tuple[Any, ...]]:
    """Funding: (calc_time_ms, interval_hours, rate). Klines: (open_time_ms, open, high, low, close, volume, close_time_ms, quote_volume, count). The header is optional (some archive months have none) and, when
    present, must be the documented one. Every row is checked (and, when ``month`` is given, must lie inside that UTC month); one bad row makes the whole month a reject."""
    funding = dataset == "fundingRate"
    _dataset(dataset)
    m_lo, m_hi = month_bounds_ms(month) if month is not None else (0, 1 << 62)
    lines = text.splitlines()
    while lines and not lines[-1].strip():
        lines.pop()
    if not lines:
        raise ArchiveError("ROWS_INVALID", "the file has no rows")
    expected_header = FUNDING_HEADER if funding else KLINE_HEADER
    first = lines[0].strip().split(",")
    body_start = 0
    if not first[0].lstrip("-").isdigit():
        if lines[0].strip() != expected_header:
            raise ArchiveError("ROWS_INVALID", f"line 1: header {lines[0].strip()[:80]!r} is not the documented one")
        body_start = 1
    out: List[Tuple[Any, ...]] = []
    last_t = -1
    for idx in range(body_start, len(lines)):
        line_no = idx + 1
        f = lines[idx].strip().split(",")
        if funding:
            if len(f) != 3:
                raise ArchiveError("ROWS_INVALID", f"line {line_no}: {len(f)} columns, expected 3")
            t, iv, rate = _int(f[0], "calc_time", line_no), _int(f[1], "funding_interval_hours", line_no), _finite(f[2], "rate", line_no)
            if t < 0 or iv not in FUNDING_INTERVALS_H or abs(rate) > MAX_ABS_FUNDING_RATE:
                raise ArchiveError("ROWS_INVALID", f"line {line_no}: time, interval or rate out of range")
            row: Tuple[Any, ...] = (t, iv, rate)
        else:
            if len(f) != 12:
                raise ArchiveError("ROWS_INVALID", f"line {line_no}: {len(f)} columns, expected 12")
            t = _int(f[0], "open_time", line_no)
            if dataset == "spot_klines" and t > 10 ** 14:                  # spot months from 2025 are in microseconds
                t //= 1000
            o, h, lo, c = (_finite(f[1], "open", line_no), _finite(f[2], "high", line_no), _finite(f[3], "low", line_no), _finite(f[4], "close", line_no))
            vol, qv = _finite(f[5], "volume", line_no), _finite(f[7], "quote_volume", line_no)
            ct, n = _int(f[6], "close_time", line_no), _int(f[8], "count", line_no)
            if dataset == "spot_klines" and ct > 10 ** 14:
                ct //= 1000
            if t < 0 or min(o, h, lo, c) <= 0 or h < lo or vol < 0 or qv < 0 or n < 0 or ct <= t:
                raise ArchiveError("ROWS_INVALID", f"line {line_no}: a price, volume, count or time is out of range")
            row = (t, o, h, lo, c, vol, ct, qv, n)
        if not m_lo <= t < m_hi:
            raise ArchiveError("ROWS_INVALID", f"line {line_no}: the time is outside the month the file is named for")
        if t <= last_t:
            raise ArchiveError("ROWS_INVALID", f"line {line_no}: time does not strictly increase")
        last_t = t
        out.append(row)
    return out


# ---------------------------------------------------------------- checksums and zips
def parse_checksum(body: bytes) -> Tuple[str, str]:
    """(sha256 hex, file name) from a ``.CHECKSUM`` file: ``<64 hex>  <name>``."""
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        raise ArchiveError("CHECKSUM_MALFORMED", "the checksum file is not text") from None
    rows = [l for l in text.splitlines() if l.strip()]
    if len(rows) != 1:
        raise ArchiveError("CHECKSUM_MALFORMED", f"{len(rows)} lines, expected one")
    parts = rows[0].split()
    if len(parts) != 2 or not _HEX64.match(parts[0]):
        raise ArchiveError("CHECKSUM_MALFORMED", "expected '<64 hex>  <file name>'")
    return parts[0], parts[1].lstrip("*")


def extract_csv(zip_bytes: bytes, expected_member: str) -> str:
    """The one CSV member, decoded, within the size cap (a declared size and an actual read are both checked)."""
    try:
        z = zipfile.ZipFile(io.BytesIO(zip_bytes))
    except (zipfile.BadZipFile, OSError, ValueError):
        raise ArchiveError("ZIP_INVALID", "not a readable zip") from None
    with z:
        infos = z.infolist()
        if len(infos) != 1 or infos[0].filename != expected_member or infos[0].is_dir():
            raise ArchiveError("ZIP_MEMBER_INVALID", f"expected exactly one member named {expected_member!r}, found {[i.filename for i in infos][:3]}")
        if infos[0].file_size > MAX_CSV_BYTES:
            raise ArchiveError("ZIP_TOO_LARGE", f"declared size {infos[0].file_size} exceeds the cap")
        try:
            with z.open(infos[0]) as fh:
                raw = fh.read(MAX_CSV_BYTES + 1)
        except (zipfile.BadZipFile, OSError, ValueError, EOFError, NotImplementedError):
            raise ArchiveError("ZIP_INVALID", "the member cannot be read") from None
    if len(raw) > MAX_CSV_BYTES:
        raise ArchiveError("ZIP_TOO_LARGE", "the member is larger than its header said")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        raise ArchiveError("ROWS_INVALID", "the CSV is not UTF-8") from None


def _member_name(path_or_name: str) -> str:
    base = os.path.basename(path_or_name)
    return base[:-4] + ".csv" if base.endswith(".zip") else base


def _stored_checksum_ok(zip_path: str) -> Tuple[bool, str]:
    """(verifies, sha256 of the zip) for a stored month, judged against ITS OWN stored checksum file."""
    try:
        with open(zip_path, "rb") as fh:
            body = fh.read(MAX_ZIP_BYTES + 1)
        with open(zip_path + ".CHECKSUM", "rb") as fh:
            ck = fh.read(4096)
    except OSError:
        return False, ""
    sha = hashlib.sha256(body).hexdigest()
    try:
        want, name = parse_checksum(ck)
    except ArchiveError:
        return False, sha
    return (len(body) <= MAX_ZIP_BYTES and want == sha and name == os.path.basename(zip_path)), sha


def read_rows(zip_path: str, dataset: str) -> List[Tuple[Any, ...]]:
    """The rows of a stored month, after re-verifying the zip against its stored checksum. Raises ``STORE_CHECKSUM_MISSING`` / ``STORE_CHECKSUM_MISMATCH``."""
    if not os.path.exists(zip_path + ".CHECKSUM"):
        raise ArchiveError("STORE_CHECKSUM_MISSING", f"{os.path.basename(zip_path)} has no stored checksum: it is not read")
    ok, _sha = _stored_checksum_ok(zip_path)
    if not ok:
        raise ArchiveError("STORE_CHECKSUM_MISMATCH", f"{os.path.basename(zip_path)} does not match its stored checksum: it is not read")
    with open(zip_path, "rb") as fh:
        body = fh.read()
    m = re.search(r"-(\d{4}-\d{2})\.zip$", os.path.basename(zip_path))
    return parse_rows(dataset, extract_csv(body, _member_name(zip_path)), m.group(1) if m else None)


# ---------------------------------------------------------------- the store
def _in_git_tree(path: str) -> bool:
    p = os.path.abspath(path)
    while True:
        if os.path.exists(os.path.join(p, ".git")):
            return True
        parent = os.path.dirname(p)
        if parent == p:
            return False
        p = parent


def _write_atomic(path: str, data: bytes) -> None:
    tmp = f"{path}.tmp{os.getpid()}.{threading.get_ident()}"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)


def _private_dirs(root: str, target: str) -> None:
    """Create ``target`` and every missing level below ``root``, each 0700 (``os.makedirs`` applies the mode to the leaf only)."""
    rel = os.path.relpath(target, root)
    cur = root
    for part in ([] if rel == "." else rel.split(os.sep)):
        cur = os.path.join(cur, part)
        try:
            os.mkdir(cur, 0o700)                                        # a umask can only remove bits from 0700
        except FileExistsError:                                         # another worker made it first (the CLI downloads in parallel)
            if os.path.islink(cur) or not os.path.isdir(cur):
                raise ArchiveError("ROOT_NOT_ALLOWED", f"{os.path.basename(cur)} exists inside the store and is not a plain directory") from None


class Archive:
    """A private, verified, resumable local copy. ``fetch(url) -> (status, body)`` is injected (default: the capped GET transport)."""

    def __init__(self, root: Optional[str], *, fetch: Optional[Callable[[str], Tuple[int, bytes]]] = None, clock: Callable[[], float] = time.time,
                 monotonic: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep, rps: float = 8.0, retries: int = 3, backoff_s: float = 1.0,
                 free_bytes_fn: Callable[[str], int] = lambda p: shutil.disk_usage(p).free, reserve_bytes: int = RESERVE_BYTES) -> None:
        self.root = "" if root is None else os.path.abspath(root)                  # root=None: listing only, no store
        problems: List[str] = []
        if root is not None and _in_git_tree(self.root):
            problems.append("the store is inside a git work tree: archive data must live outside Git")
        if root is not None and os.path.islink(self.root):
            problems.append("the store is a symbolic link")
        if root is not None and os.path.isdir(self.root) and os.stat(self.root).st_mode & 0o077:
            problems.append(f"the store mode is {oct(os.stat(self.root).st_mode & 0o777)}: it must be 0700")
        if root is not None:
            probe = self.root
            while not os.path.exists(probe) and os.path.dirname(probe) != probe:
                probe = os.path.dirname(probe)
            try:
                free = int(free_bytes_fn(probe))
            except OSError as exc:
                problems.append(f"free space cannot be read ({exc.strerror})")
            else:
                if free < reserve_bytes:
                    problems.append(f"free space {free} bytes is below the reserve {reserve_bytes}")
        if problems:
            raise ArchiveError("ROOT_NOT_ALLOWED", "; ".join(problems))
        if root is not None and not os.path.isdir(self.root):
            os.makedirs(os.path.dirname(self.root) or ".", exist_ok=True)
            os.mkdir(self.root, 0o700)
            os.chmod(self.root, 0o700)
        self._fetch = fetch if fetch is not None else http_fetch
        self._clock, self._sleep, self._retries, self._backoff = clock, sleep, int(retries), float(backoff_s)
        self._throttle = Throttle(rps, max(1, int(rps)), monotonic, sleep)
        self._lock = threading.Lock()
        self._absent = self._load_absent() if root is not None else {}

    # ---- bookkeeping
    def _need_store(self) -> None:
        if not self.root:
            raise ArchiveError("BAD_ARGUMENT", "this object was built for listing only (root=None): it has no store")

    def _log(self, name: str, row: Mapping[str, Any]) -> None:
        self._need_store()
        line = (json.dumps(row, sort_keys=True, allow_nan=False) + "\n").encode("utf-8")
        with self._lock:
            fd = os.open(os.path.join(self.root, name), os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
            try:
                os.write(fd, line)
                os.fsync(fd)
            finally:
                os.close(fd)

    def _load_absent(self) -> Dict[Tuple[str, str, Optional[str], str], float]:
        out: Dict[Tuple[str, str, Optional[str], str], float] = {}
        path = os.path.join(self.root, "ABSENT.jsonl")
        try:
            with open(path, encoding="utf-8") as fh:
                for line in fh:
                    try:
                        r = json.loads(line)
                        out[(r["dataset"], r["symbol"], r["interval"], r["month"])] = float(r["checked_ms"]) / 1000.0
                    except (ValueError, KeyError, TypeError):
                        continue
        except OSError:
            pass
        return out

    def file_path(self, dataset: str, symbol: str, interval: Optional[str], month: str) -> str:
        self._need_store()
        return os.path.join(self.root, *data_path(dataset, symbol, interval, month).split("/"))

    # ---- network with bounded retries
    def _get(self, url: str) -> Optional[bytes]:
        attempt = 0
        while True:
            self._throttle.acquire()
            try:
                status, body = self._fetch(url)
            except ArchiveError as exc:
                if exc.code == "NETWORK_ERROR" and attempt < self._retries:        # a reset or a timeout through a proxy is transient: bounded retries, growing pauses
                    self._sleep(self._backoff * (2 ** attempt))
                    attempt += 1
                    continue
                raise
            if status == 200:
                return body
            if status == 404:
                return None
            if status in (403, 451):
                raise ArchiveError("REFUSED_BY_ARCHIVE", f"HTTP {status} for {url.split('?')[0].rsplit('/', 1)[-1]}: a network or policy refusal; the run stops and does not retry, change host or change path")
            if (status == 429 or 500 <= status <= 599) and attempt < self._retries:
                self._sleep(self._backoff * (2 ** attempt))
                attempt += 1
                continue
            raise ArchiveError("HTTP_ERROR", f"HTTP {status} for {url.rsplit('/', 1)[-1]} after {attempt + 1} attempt(s)")

    # ---- listings
    def _list(self, prefix: str, delimiter: bool) -> Tuple[List[str], List[str]]:
        prefixes: List[str] = []
        keys: List[str] = []
        marker = ""
        for _ in range(MAX_LISTING_PAGES):
            body = self._get(listing_url(prefix, marker=marker, delimiter=delimiter))
            if body is None or len(body) > MAX_LISTING_BYTES or b"<ListBucketResult" not in body[:600]:
                raise ArchiveError("LISTING_INVALID", "the archive listing is missing, too large or not a bucket listing")
            text = body.decode("utf-8", "replace")
            prefixes += re.findall(r"<CommonPrefixes><Prefix>([^<]*)</Prefix></CommonPrefixes>", text)
            keys += re.findall(r"<Contents><Key>([^<]*)</Key>", text)
            if "<IsTruncated>true</IsTruncated>" not in text:
                return prefixes, keys
            m = re.search(r"<NextMarker>([^<]+)</NextMarker>", text)
            nxt = m.group(1) if m else (prefixes[-1] if prefixes else (keys[-1] if keys else ""))
            if not nxt or nxt == marker:
                raise ArchiveError("LISTING_DID_NOT_ADVANCE", "a truncated listing did not move its marker")
            marker = nxt
        raise ArchiveError("LISTING_DID_NOT_ADVANCE", f"more than {MAX_LISTING_PAGES} listing pages")

    def list_symbols(self, dataset: str) -> List[str]:
        market, _needs, dirname = _dataset(dataset)
        prefix = f"data/{market}/monthly/{dirname}/"
        prefixes, _ = self._list(prefix, True)
        out = {p[len(prefix):].rstrip("/") for p in prefixes if p.startswith(prefix)}
        return sorted(s for s in out if _SYMBOL.match(s))

    def list_months(self, dataset: str, symbol: str, interval: Optional[str]) -> List[str]:
        market, dirname, _sym, _month = _check_parts(dataset, symbol, interval, "2020-01")
        prefix = f"data/{market}/monthly/{dirname}/{symbol}/" + (f"{interval}/" if interval else "")
        _, keys = self._list(prefix, False)
        stem = f"{symbol}-{interval}-" if interval else f"{symbol}-{dirname}-"
        pat = re.compile("^" + re.escape(prefix + stem) + r"(\d{4}-(?:0[1-9]|1[0-2]))\.zip$")
        return sorted({m.group(1) for k in keys for m in [pat.match(k)] if m})

    # ---- one month
    def _reject(self, code: str, message: str, dataset: str, symbol: str, interval: Optional[str], month: str, url: str) -> Dict[str, Any]:
        self._log("REJECTS.jsonl", {"code": code, "message": message[:300], "dataset": dataset, "symbol": symbol, "interval": interval, "month": month, "url": url, "at_ms": int(self._clock() * 1000)})
        return {"status": "REJECT", "code": code, "message": message, "dataset": dataset, "symbol": symbol, "interval": interval, "month": month}

    def download_month(self, dataset: str, symbol: str, interval: Optional[str], month: str) -> Dict[str, Any]:
        url = data_url(dataset, symbol, interval, month)
        path = self.file_path(dataset, symbol, interval, month)
        base = {"dataset": dataset, "symbol": symbol, "interval": interval, "month": month}
        ok, sha = _stored_checksum_ok(path)
        if ok:
            return {"status": "EXISTS", "sha256": sha, "bytes": os.path.getsize(path), **base}
        key = (dataset, symbol, interval, month)
        seen = self._absent.get(key)
        if seen is not None and self._clock() - seen < ABSENT_TTL_S:
            return {"status": "ABSENT", **base}
        body = self._get(url)
        if body is None:
            with self._lock:
                self._absent[key] = self._clock()
            self._log("ABSENT.jsonl", {**base, "checked_ms": int(self._clock() * 1000)})
            return {"status": "ABSENT", **base}
        if len(body) > MAX_ZIP_BYTES:
            return self._reject("DOWNLOAD_TOO_LARGE", f"{len(body)} bytes exceed the cap", dataset, symbol, interval, month, url)
        ck_body = self._get(data_url(dataset, symbol, interval, month, ext="zip.CHECKSUM"))
        if ck_body is None:
            return self._reject("CHECKSUM_UNAVAILABLE", "the archive publishes no checksum for this file: it is not kept", dataset, symbol, interval, month, url)
        try:
            want, name = parse_checksum(ck_body)
        except ArchiveError as exc:
            return self._reject(exc.code, str(exc), dataset, symbol, interval, month, url)
        if name != os.path.basename(path):
            return self._reject("CHECKSUM_NAME_MISMATCH", f"the checksum names {name!r}, not {os.path.basename(path)!r}", dataset, symbol, interval, month, url)
        sha = hashlib.sha256(body).hexdigest()
        if sha != want:
            return self._reject("CHECKSUM_MISMATCH", "the downloaded bytes do not equal the published sha256", dataset, symbol, interval, month, url)
        try:
            parse_rows(dataset, extract_csv(body, _member_name(path)), month)
        except ArchiveError as exc:
            return self._reject(exc.code, str(exc), dataset, symbol, interval, month, url)
        _private_dirs(self.root, os.path.dirname(path))
        _write_atomic(path, body)
        _write_atomic(path + ".CHECKSUM", ck_body)
        self._log("MANIFEST.jsonl", {**base, "url": url, "sha256": sha, "bytes": len(body), "fetched_ms": int(self._clock() * 1000), "schema": SCHEMA})
        return {"status": "OK", "sha256": sha, "bytes": len(body), **base}

    def download_range(self, dataset: str, symbol: str, interval: Optional[str], months: Sequence[str], *, until_month: Optional[str]) -> List[Dict[str, Any]]:
        """Download ``months`` up to and INCLUDING ``until_month``; later months are never requested. ``until_month`` is required."""
        if not isinstance(until_month, str) or not _MONTH.match(until_month):
            raise ArchiveError("BAD_ARGUMENT", "until_month (YYYY-MM) is required: a download states where it stops")
        return [self.download_month(dataset, symbol, interval, m) for m in sorted(set(months)) if m <= until_month]


# ---------------------------------------------------------------- command line
def _months_between(first: str, last: str) -> List[str]:
    if not (_MONTH.match(first) and _MONTH.match(last)):
        raise ArchiveError("BAD_ARGUMENT", "months are YYYY-MM")
    y, m = int(first[:4]), int(first[5:])
    out: List[str] = []
    while f"{y:04d}-{m:02d}" <= last:
        out.append(f"{y:04d}-{m:02d}")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s1 = sub.add_parser("symbols", help="list the symbols the archive holds for a dataset")
    s1.add_argument("--dataset", required=True, choices=sorted(DATASETS))
    s2 = sub.add_parser("months", help="list the months the archive holds for one symbol")
    s2.add_argument("--dataset", required=True, choices=sorted(DATASETS))
    s2.add_argument("--symbol", required=True)
    s2.add_argument("--interval", default=None)
    s3 = sub.add_parser("download", help="download verified months into a private store")
    s3.add_argument("--root", required=True, help="a private directory OUTSIDE git (0700)")
    s3.add_argument("--dataset", required=True, choices=sorted(DATASETS))
    s3.add_argument("--interval", default=None)
    s3.add_argument("--symbols", required=True, help="comma list, or @file with one symbol per line")
    s3.add_argument("--from-month", required=True)
    s3.add_argument("--until-month", required=True, help="the LAST month to fetch; later months are never requested")
    s3.add_argument("--workers", type=int, default=4)
    s3.add_argument("--rps", type=float, default=8.0)
    a = ap.parse_args(argv)
    try:
        if a.cmd == "symbols":
            for s in Archive(None).list_symbols(a.dataset):
                print(s)
            return 0
        if a.cmd == "months":
            for m in Archive(None).list_months(a.dataset, a.symbol, a.interval):
                print(m)
            return 0
        symbols = [l.strip() for l in open(a.symbols[1:], encoding="utf-8")] if a.symbols.startswith("@") else a.symbols.split(",")
        symbols = [s for s in symbols if s]
        arc = Archive(a.root, rps=a.rps)
        months = _months_between(a.from_month, a.until_month)
        counts: Dict[str, int] = {}

        def one(sym: str) -> List[Dict[str, Any]]:
            return arc.download_range(a.dataset, sym, a.interval, months, until_month=a.until_month)
        with ThreadPoolExecutor(max_workers=max(1, min(16, a.workers))) as pool:
            for res in pool.map(one, symbols):
                for r in res:
                    counts[r["status"]] = counts.get(r["status"], 0) + 1
        print(json.dumps({"schema": SCHEMA, "root": arc.root, "dataset": a.dataset, "symbols": len(symbols), "months_each": len(months), "by_status": counts}, sort_keys=True))
        return 0 if not counts.get("REJECT") else 1
    except ArchiveError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 3 if exc.code in ("REFUSED_BY_ARCHIVE", "NETWORK_ERROR") else 2


if __name__ == "__main__":
    raise SystemExit(main())
