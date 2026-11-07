"""Owner-invoked, read-only capture of the PUBLIC Bybit funding history into raw, hash-listed files for ``tools/prospective_prints.py``.

Why. RUNBOOKS section 20 step 1 asked the owner to page the public endpoint by hand and save each response unedited. A hand loop is where a page is
skipped, an ``endTime`` is reused, or a response is "tidied". This tool does only that loop, and nothing else:

    GET <mainnet REST host from bot/venues.py>/v5/market/funding/history?category=linear&symbol=BTCUSDT&startTime=<ms>&endTime=<ms>&limit=200

What it is NOT. It is not an order path, not a signed request (no key is read, no header is added), not an account read, and it is never run by the test
suite or by CI (tests inject the transport and a socket guard fails any real connection). The agent that wrote it has not executed it; the owner runs it
on a machine the owner controls. It does not validate economics: ``prospective_prints.py`` re-parses every saved page strictly, and that is the only
route into the registered evaluation.

What it guarantees.
  * The request is built from constants only (host from the venue registry, one path, one symbol, one category). No redirect is followed.
  * Each response body is written BYTE-FOR-BYTE as received, created exclusively (an existing file is never overwritten), mode 0600; a manifest lists
    every request, the file, its SHA-256, its row count and its first/last timestamps.
  * Paging is by ``endTime`` moving strictly backwards; a page that does not move the cursor, a page over 200 rows, or a runaway page count stops the run.
  * Duplicates across pages are counted (identical) or fatal (conflicting rate for one timestamp). Gaps on the 8-hour grid inside the asked range are
    REPORTED, never filled. A short page never ends the run (only an empty page, the start, or the page cap does); an empty page is reported as what it was.
What it cannot show: that the bytes came from the exchange (a hash proves what was read, not who produced it).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Dict, List, Optional, Sequence

BOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BOT)
sys.path.insert(0, os.path.join(BOT, "tools"))

import account_review as AR  # noqa: E402  (the one protected-name rule)
import venues  # noqa: E402

PATH = "/v5/market/funding/history"
SYMBOL = "BTCUSDT"
CATEGORY = "linear"
PAGE_LIMIT = 200
SETTLE_MS = 8 * 3600 * 1000
MAX_PAGES = 400
MAX_BODY_BYTES = 1 << 20
TIMEOUT_S = 15.0
PAUSE_S = 0.25                          # polite pacing between public requests


class CaptureError(ValueError):
    """The capture cannot continue safely. Files already written are kept and listed in the manifest only if the run completed."""


def request_url(start_ms: int, end_ms: int) -> str:
    base = venues.rest_for("mainnet")
    if not base.startswith("https://"):
        raise CaptureError("the registry's host is not https")
    q = urllib.parse.urlencode([("category", CATEGORY), ("symbol", SYMBOL), ("startTime", int(start_ms)), ("endTime", int(end_ms)), ("limit", PAGE_LIMIT)])
    return f"{base}{PATH}?{q}"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_a, **_k):                      # a redirect is a different host: refuse it
        return None


def http_fetch(url: str) -> bytes:
    """The only network primitive: one unauthenticated GET, no redirects, bounded body. Tests never reach it."""
    if not url.startswith(venues.rest_for("mainnet") + PATH + "?"):
        raise CaptureError("refusing a URL that is not the registry host and the funding-history path")
    opener = urllib.request.build_opener(_NoRedirect)
    req = urllib.request.Request(url, method="GET", headers={"Accept": "application/json", "User-Agent": "funding-capture/1"})
    try:
        with opener.open(req, timeout=TIMEOUT_S) as resp:
            if resp.status != 200:
                raise CaptureError(f"HTTP {resp.status}")
            body = resp.read(MAX_BODY_BYTES + 1)
    except (urllib.error.URLError, OSError) as exc:
        raise CaptureError(f"request failed ({type(exc).__name__})") from exc
    if len(body) > MAX_BODY_BYTES:
        raise CaptureError("response larger than any funding-history page")
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


def _page_rows(body: bytes, label: str) -> List[Dict[str, Any]]:
    try:
        doc = json.loads(body.decode("utf-8", "strict"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise CaptureError(f"{label}: not UTF-8 JSON") from exc
    if not isinstance(doc, dict) or isinstance(doc.get("retCode"), bool) or doc.get("retCode") != 0:
        raise CaptureError(f"{label}: not a successful V5 envelope (retCode {doc.get('retCode') if isinstance(doc, dict) else None!r})")
    res = doc.get("result")
    if not isinstance(res, dict) or res.get("category") != CATEGORY or not isinstance(res.get("list"), list):
        raise CaptureError(f"{label}: result.category/list is not the linear funding list")
    rows = res["list"]
    if len(rows) > PAGE_LIMIT:
        raise CaptureError(f"{label}: {len(rows)} rows exceeds the endpoint's page limit {PAGE_LIMIT}")
    for r in rows:
        if not isinstance(r, dict) or r.get("symbol") != SYMBOL or not isinstance(r.get("fundingRate"), str):
            raise CaptureError(f"{label}: a row is not a {SYMBOL} funding row")
        ts = r.get("fundingRateTimestamp")
        if isinstance(ts, bool) or not isinstance(ts, (str, int)) or not str(ts).isascii() or not str(ts).isdigit():
            raise CaptureError(f"{label}: a row has no whole-millisecond timestamp")
    return rows


def capture(out_dir: str, start_ms: int, end_ms: int, *, fetch: Callable[[str], bytes] = http_fetch,
            sleep: Callable[[float], None] = time.sleep, now_ms: Optional[int] = None, max_pages: int = MAX_PAGES) -> Dict[str, Any]:
    """Page backwards from ``end_ms`` to ``start_ms``; returns the manifest (also written, exclusively, as ``manifest.json``)."""
    now = int(time.time() * 1000) if now_ms is None else min(int(now_ms), int(time.time() * 1000))
    if not (0 < start_ms < end_ms):
        raise CaptureError("start must be before end")
    end_ms = min(end_ms, now)                                           # never ask for a settlement that has not happened
    if end_ms <= start_ms:
        raise CaptureError("the range is entirely in the future")
    parent = os.path.dirname(os.path.abspath(out_dir))
    if os.path.islink(out_dir) or os.path.islink(parent) or not os.path.isdir(parent):
        raise CaptureError("the output directory must be a real directory, not a link")
    AR._refuse_protected(os.path.join(out_dir, "manifest.json"))     # names the directory by a file inside it: a directory has >1 link
    os.makedirs(out_dir, mode=0o700, exist_ok=True)
    if os.listdir(out_dir):
        raise CaptureError("the output directory is not empty: a capture goes in a new directory, nothing is overwritten")

    seen: Dict[int, str] = {}
    pages: List[Dict[str, Any]] = []
    duplicates = 0
    cursor = end_ms
    stop = "max_pages"
    for n in range(1, max_pages + 1):
        url = request_url(start_ms, cursor)
        body = fetch(url)
        label = f"page{n:04d}"
        rows = _page_rows(body, label)
        fname = f"{label}_end{cursor}.json"
        _create_exclusive(os.path.join(out_dir, fname), body)            # unedited, before anything else is concluded about it
        stamps = sorted(int(str(r["fundingRateTimestamp"])) for r in rows)
        for r in rows:
            ts, rate = int(str(r["fundingRateTimestamp"])), r["fundingRate"]
            if ts in seen:
                if seen[ts] != rate:
                    raise CaptureError(f"{label}: settlement {ts} appears with two different rates ({seen[ts]!r} and {rate!r}); nothing is merged")
                duplicates += 1
            seen[ts] = rate
        pages.append({"page": n, "file": fname, "sha256": hashlib.sha256(body).hexdigest(), "endTime": cursor, "rows": len(rows),
                      "first_ts": stamps[0] if stamps else None, "last_ts": stamps[-1] if stamps else None})
        if not rows:
            stop = "empty_page"
            break
        if stamps[0] <= start_ms:
            stop = "reached_start"
            break
        if stamps[0] - 1 >= cursor:
            raise CaptureError(f"{label}: the cursor did not move backwards; stopping rather than loop")
        cursor = stamps[0] - 1                                            # a short page is NOT an end: only an empty page or the start is
        sleep(PAUSE_S)

    first_grid = -(-start_ms // SETTLE_MS) * SETTLE_MS
    expected = list(range(first_grid, (end_ms // SETTLE_MS) * SETTLE_MS + 1, SETTLE_MS))
    missing = [t for t in expected if t not in seen]
    off_grid = sorted(t for t in seen if t % SETTLE_MS)
    manifest = {
        "request": {"path": PATH, "category": CATEGORY, "symbol": SYMBOL, "start_ms": start_ms, "end_ms": end_ms},
        "stopped_because": stop, "pages": pages, "distinct_settlements": len(seen), "identical_duplicates_dropped": duplicates,
        "expected_on_grid": len(expected), "missing_on_grid": missing, "off_grid_timestamps": off_grid,
        "note": "gaps are reported, never filled; a hash shows what was read, not who produced it; run tools/prospective_prints.py on these files",
    }
    _create_exclusive(os.path.join(out_dir, "manifest.json"), (json.dumps(manifest, indent=2, sort_keys=True, allow_nan=False) + "\n").encode("utf-8"))
    return manifest


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Owner-invoked read-only capture of the public Bybit BTCUSDT funding history (raw pages + manifest).")
    ap.add_argument("--start-ms", type=int, required=True)
    ap.add_argument("--end-ms", type=int, default=None, help="default: now")
    ap.add_argument("--out-dir", required=True, help="a NEW directory; nothing in it is ever overwritten")
    a = ap.parse_args(argv)
    end = a.end_ms if a.end_ms is not None else int(time.time() * 1000)
    try:
        m = capture(a.out_dir, a.start_ms, end)
    except (CaptureError, AR.RecordError, OSError) as exc:
        print(f"funding_capture: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({k: m[k] for k in ("stopped_because", "distinct_settlements", "identical_duplicates_dropped", "expected_on_grid")} | {"missing_on_grid": len(m["missing_on_grid"]), "pages": len(m["pages"])}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
