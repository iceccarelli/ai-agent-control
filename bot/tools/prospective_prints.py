"""Turns saved raw Bybit V5 funding-history responses into the ONE input file the registered prospective evaluation accepts.

Why. ``tools/trial_journal.py evaluate`` and ``tools/carry_prospective.py`` read a file of ``{"ts_ms", "funding_bps"}`` rows, and until now nothing produced it:
a hand-made file is where a unit mistake lives. The raw venue value is a FRACTION per settlement (``"0.0001"`` = 1 bp: the repo's own history shows the 1 bp
baseline as a raw ``0.0001``), the plan wants BASIS POINTS, and a file in fractions silently reads as "no edge" (the red team reproduced it). This tool does
the conversion itself and accepts only the raw shape, so no one types a unit.

What it does. Reads one or more LOCAL files that an operator saved from the public endpoint

    GET https://api.bybit.com/v5/market/funding/history?category=linear&symbol=BTCUSDT&startTime=<ms>&endTime=<ms>&limit=200

(<= 200 rows a page, ordering unspecified, so page by ``endTime`` and save each response as it came). It makes no request and reads no credential. It checks
the response ENVELOPE (a bare list of rows is refused), integer retCode 0, category linear, symbol BTCUSDT on every row, a finite rate within the per-settlement plausibility bound, a whole-millisecond timestamp ON the 8-hour
UTC grid (a symbol that moves to a 4-hour interval is REFUSED, because the registered plan is an 8-hour plan), no timestamp in the future, and merges pages:
an identical duplicate is dropped, a CONFLICTING duplicate is an error. Only settlements inside the registered window are written; anything before it is counted
and reported but never written (the plan is future-only), and a gap is reported, never filled. The output is created exclusively (an existing file is never
overwritten), mode 0600, and the report names the SHA-256 of every input and of the output.

What it does not prove. Hashes show which bytes were read, not that the exchange produced them; the file is operator-supplied and is not account data. Whether a
dropped print was a legitimate outage or a selection is not knowable from here: the journal binds the digest of the prints actually evaluated, and
``selection_sensitivity`` reports what missing prints could do.
"""
from __future__ import annotations

import argparse
import decimal
import hashlib
import json
import math
import os
import re
import stat
import sys
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

BOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BOT)
sys.path.insert(0, os.path.join(BOT, "tools"))

import account_review as AR  # noqa: E402  (the one protected-name rule)
import carry_prospective as CP  # noqa: E402

SYMBOL = "BTCUSDT"
MAX_ABS_BPS = 300.0                      # the same plausibility bound the teardown applies to one settlement
MAX_INPUT_BYTES = 8 * 1024 * 1024
MAX_PAGE_ROWS = 200
_PLAIN_DECIMAL = re.compile(r"^-?[0-9]+(\.[0-9]+)?$", re.ASCII)       # the venue emits plain decimal strings: no padding, underscores or exponents


def _now_ms() -> int:
    return int(time.time() * 1000)


class PrintsError(ValueError):
    """The saved responses cannot be turned into the registered input. Nothing is written."""


def _read(path: str) -> Tuple[bytes, str]:
    AR._refuse_protected(path)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError as exc:
        raise PrintsError(f"{os.path.basename(path)}: cannot open ({exc.strerror})") from exc
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
            raise PrintsError(f"{os.path.basename(path)}: not a single-link regular file")
        if st.st_size > MAX_INPUT_BYTES:
            raise PrintsError(f"{os.path.basename(path)}: larger than any funding-history page set")
        data = b""
        while len(data) < st.st_size:
            chunk = os.read(fd, min(1 << 20, st.st_size - len(data)))
            if not chunk:
                break
            data += chunk
    finally:
        os.close(fd)
    return data, hashlib.sha256(data).hexdigest()


def _reject_constant(name: str) -> Any:
    raise PrintsError(f"the file holds the non-finite number {name}")


def _rows(doc: Any, label: str) -> List[Dict[str, Any]]:
    if not isinstance(doc, dict):
        raise PrintsError(f"{label}: not a V5 response envelope (a bare list of rows skips the retCode and category checks and is refused)")
    code = doc.get("retCode")
    if isinstance(code, bool) or not isinstance(code, int) or code != 0:
        raise PrintsError(f"{label}: retCode is {code!r}, not the integer 0")
    res = doc.get("result")
    if not isinstance(res, dict) or res.get("category") != "linear":
        raise PrintsError(f"{label}: result.category must be 'linear'")
    rows = res.get("list")
    if not isinstance(rows, list):
        raise PrintsError(f"{label}: no list of funding rows")
    if len(rows) > MAX_PAGE_ROWS:
        raise PrintsError(f"{label}: {len(rows)} rows in one page; the endpoint returns at most {MAX_PAGE_ROWS}")
    return rows


def _one(row: Any, label: str, now_ms: int) -> Tuple[int, decimal.Decimal]:
    if not isinstance(row, dict) or set(row) - {"symbol", "fundingRate", "fundingRateTimestamp"} or not {"fundingRate", "fundingRateTimestamp"} <= set(row):
        raise PrintsError(f"{label}: a row is not a V5 funding row (symbol, fundingRate, fundingRateTimestamp)")
    if row.get("symbol") != SYMBOL:
        raise PrintsError(f"{label}: symbol {row.get('symbol')!r}; the registered plan is {SYMBOL} only (a row with no symbol is refused too)")
    raw_rate, raw_ts = row["fundingRate"], row["fundingRateTimestamp"]
    if isinstance(raw_rate, bool) or not isinstance(raw_rate, str) or isinstance(raw_ts, bool) or not isinstance(raw_ts, (str, int)):
        raise PrintsError(f"{label}: fundingRate must be the venue's string and the timestamp a string or integer")
    try:
        if not _PLAIN_DECIMAL.match(raw_rate):
            raise ValueError(f"fundingRate {raw_rate!r} is not a plain decimal string")
        ts_text = str(raw_ts)
        if not ts_text.isascii() or not ts_text.isdigit():
            raise ValueError("timestamp is not whole milliseconds")
        ts = int(ts_text)
        exact = decimal.Decimal(raw_rate) * 10000                                 # the venue's FRACTION -> basis points, exactly, here and nowhere else
    except (ValueError, OverflowError, decimal.InvalidOperation) as exc:
        raise PrintsError(f"{label}: unreadable value ({exc})") from exc
    bps = float(exact)
    if not math.isfinite(bps) or abs(bps) > MAX_ABS_BPS:
        raise PrintsError(f"{label}: {bps} bps is not a plausible per-settlement rate")
    if ts % CP.SETTLE_MS != 0:
        raise PrintsError(f"{label}: timestamp {ts} is not on the 8-hour UTC settlement grid (a changed funding interval is refused: the plan is an 8-hour plan)")
    if ts > now_ms:
        raise PrintsError(f"{label}: timestamp {ts} is in the future; a settlement that has not happened is not an observation")
    return ts, exact


def check_capture_manifest(paths: Sequence[str], manifest_path: Optional[str] = None) -> Dict[str, Any]:
    """Re-hash the pages against the manifest ``funding_capture`` wrote, or say that there is none. Round 19 (reproduced): ``build`` took only ``--in`` files and never read ``manifest.json``, so a page
    edited after the capture ('0.0001' -> '0.0003' moved the mean from 1.17 to 2.50 bps) or a middle page dropped (the manifest said ``missing_on_grid`` 1, the report said coverage 1.0) was accepted.

    With ``manifest_path`` (a manifest.json or a capture directory), or when every input sits in ONE directory holding a ``manifest.json``, the set of inputs must EQUAL the manifest's page set, every
    page must re-hash to the manifest's sha256, and a capture that stopped at ``max_pages`` (truncated) is refused. Inputs with no manifest are hand-saved: no lineage is claimed for them."""
    mp = manifest_path
    if mp is None:
        dirs = {os.path.dirname(os.path.abspath(p)) for p in paths}
        cand = os.path.join(next(iter(dirs)), "manifest.json") if len(dirs) == 1 else None
        if cand is None or not os.path.isfile(cand):
            return {"status": "NO_MANIFEST_HAND_SAVED_INPUTS", "note": "no manifest.json beside the inputs (or the inputs come from several directories): the pages are operator-saved, so that they are the "
                                                                          "complete, unedited history of the window cannot be shown (use tools/funding_capture.py and pass its directory)"}
        try:
            _rawc, _shac = _read(cand)
            _docc = json.loads(_rawc.decode("utf-8", "strict"))
            _is_capture = isinstance(_docc, dict) and isinstance(_docc.get("pages"), list) and isinstance(_docc.get("request"), dict)
        except (OSError, ValueError, PrintsError):
            _is_capture = False
        if not _is_capture:                                          # a manifest.json of some OTHER tool beside hand-saved pages is not this capture's: say so, do not crash the builder
            return {"status": "NO_MANIFEST_HAND_SAVED_INPUTS", "note": "a manifest.json sits beside the inputs but is not a funding_capture manifest (no pages / request): no lineage is claimed for the pages"}
        mp = cand
    if os.path.isdir(mp):
        mp = os.path.join(mp, "manifest.json")
    try:
        raw, man_sha = _read(mp)
        doc = json.loads(raw.decode("utf-8", "strict"))
        pages = doc["pages"]
        expected = {os.path.realpath(os.path.join(os.path.dirname(os.path.abspath(mp)), str(pg["file"]))): str(pg["sha256"]) for pg in pages}
    except (OSError, ValueError, KeyError, TypeError, AttributeError, PrintsError) as exc:
        raise PrintsError(f"MANIFEST_UNREADABLE: {os.path.basename(str(mp))}: {type(exc).__name__}") from exc
    given = {os.path.realpath(p) for p in paths}
    missing = sorted(os.path.basename(f) for f in set(expected) - given)
    extra = sorted(os.path.basename(f) for f in given - set(expected))
    if missing or extra:
        hint = " (manifest.json is not a page: list the page*.json files, do not glob *.json)" if "manifest.json" in extra else ""
        raise PrintsError(f"PAGE_SET_DIFFERS: the inputs are not the manifest's pages (missing: {missing[:5]}; not in the manifest: {extra[:5]}): a page dropped or added after the capture changes the "
                          f"evidence without changing a hash{hint}")
    for f, want in sorted(expected.items()):
        _data, got = _read(f)
        if got != want:
            raise PrintsError(f"PAGE_HASH_MISMATCH: {os.path.basename(f)} no longer hashes to the manifest's sha256: the page was edited after the capture")
    if doc.get("stopped_because") == "max_pages":
        raise PrintsError("CAPTURE_TRUNCATED: the capture stopped at its page cap, not at the start of the window: the history is incomplete; capture again with a larger cap")
    miss = doc.get("missing_on_grid")
    return {"status": "VERIFIED_WITH_GAPS" if isinstance(miss, list) and miss else "VERIFIED", "file": os.path.basename(mp), "sha256": man_sha, "pages": len(expected), "stopped_because": doc.get("stopped_because"),
            "missing_on_grid": len(miss) if isinstance(miss, list) else None, "expected_on_grid": doc.get("expected_on_grid"), "distinct_settlements": doc.get("distinct_settlements"),
            "note": "every input re-hashes to the manifest and the set equals the manifest's; the manifest itself is the capturer's word (a hash shows what was read, not who produced it)"}


def build(paths: Sequence[str], now_ms: Optional[int] = None, manifest_path: Optional[str] = None) -> Dict[str, Any]:
    """Parse and merge. Returns the in-window prints and a report; writes nothing."""
    if not paths:
        raise PrintsError("no input files")
    capture_manifest = check_capture_manifest(paths, manifest_path)
    now = _now_ms() if now_ms is None else min(int(now_ms), _now_ms())       # a caller can only move the clock EARLIER, never claim a later one
    merged: Dict[int, decimal.Decimal] = {}
    digests: List[Dict[str, Any]] = []
    for p in paths:
        data, digest = _read(p)
        label = os.path.basename(p)
        try:
            doc = json.loads(data.decode("utf-8", "strict"), parse_constant=_reject_constant)
        except (UnicodeDecodeError, ValueError) as exc:
            raise PrintsError(f"{label}: not valid UTF-8 JSON") from exc
        n = 0
        for row in _rows(doc, label):
            ts, exact = _one(row, label, now)
            if ts in merged and merged[ts] != exact:
                raise PrintsError(f"{label}: settlement {AR._iso(ts)} appears with two different rates ({merged[ts]} and {exact} bps)")
            merged[ts] = exact
            n += 1
        digests.append({"file": label, "sha256": digest, "rows": n})
    before = sorted(t for t in merged if t < CP.WINDOW_START_MS)
    if not any(CP.WINDOW_START_MS <= t < CP.WINDOW_END_MS for t in merged):
        raise PrintsError("no settlement inside the registered window was found: nothing is written (an empty prints file would look like a run that found no data)")
    after = sorted(t for t in merged if t >= CP.WINDOW_END_MS)
    inside = sorted((t, float(v)) for t, v in merged.items() if CP.WINDOW_START_MS <= t < CP.WINDOW_END_MS)
    cov = CP.coverage(inside, now)
    return {"prints": inside, "report": {
        "inputs": digests, "manifest": capture_manifest, "distinct_settlements_read": len(merged), "written": len(inside),
        "before_the_window_not_written": len(before), "after_the_window_not_written": len(after),
        "window": {"start": AR._iso(CP.WINDOW_START_MS), "end": AR._iso(CP.WINDOW_END_MS)}, "coverage": cov,
        "first": AR._iso(inside[0][0]) if inside else None, "last": AR._iso(inside[-1][0]) if inside else None,
        "note": "a gap is reported, never filled; whether a missing settlement was an outage or a selection cannot be known from here"}}


def write(prints: Sequence[Tuple[int, float]], out_path: str) -> str:
    """Exclusive create (never overwrites), mode 0600; returns the SHA-256 of what was written."""
    AR._refuse_protected(out_path)
    parent = os.path.dirname(os.path.abspath(out_path))
    if os.path.islink(parent) or not os.path.isdir(parent):
        raise PrintsError("the output directory must be a real directory, not a link")
    body = "".join(json.dumps({"ts_ms": t, "funding_bps": v}, sort_keys=True, allow_nan=False) + "\n" for t, v in prints).encode("utf-8")
    try:
        fd = os.open(out_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    except FileExistsError as exc:
        raise PrintsError(f"{os.path.basename(out_path)} exists: an existing prints file is never overwritten") from exc
    except OSError as exc:
        raise PrintsError(f"{os.path.basename(out_path)}: cannot create ({exc.strerror})") from exc
    try:
        view = memoryview(body)
        while view:
            n = os.write(fd, view)
            if n <= 0:
                raise PrintsError("write made no progress")
            view = view[n:]
        os.fsync(fd)
    finally:
        os.close(fd)
    return hashlib.sha256(body).hexdigest()


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Saved Bybit V5 funding-history responses -> the registered prospective input. Offline; no request, no credential.")
    ap.add_argument("--in", dest="inputs", action="append", required=True, help="a saved response file (repeat for each page)")
    ap.add_argument("--out", required=True, help="the prints file to create (never overwritten)")
    ap.add_argument("--manifest", default=None, help="the capture's manifest.json or its directory (default: the manifest.json beside the inputs, when there is one). The inputs must equal its page set "
                                                      "and re-hash to it")
    a = ap.parse_args(argv)
    try:
        built = build(a.inputs, manifest_path=a.manifest)
        digest = write(built["prints"], a.out)
    except (PrintsError, AR.RecordError, OSError) as exc:
        print(f"prospective_prints: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    built["report"]["output"] = {"file": os.path.basename(a.out), "sha256": digest}
    print(json.dumps(built["report"], indent=2, sort_keys=True, allow_nan=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
