"""Research harness: the machinery that PRODUCES evidence for ``strategy_verdict`` and makes bad strategies die.

    python3 tools/research_harness.py --demo-cross-sectional-funding   # the committed 3-symbol funding history, run honestly

Offline, standard library only. It REUSES ``evidence_stats`` (ESS, block bootstrap) and ``statistical_power`` (Student-t tail, Holm/BH, the
hash-chained ``TrialLedger``, purged/embargoed walk-forward, split audit); it does not reimplement them. What it adds is the discipline around them:

  PointInTimeUniverse   membership with as-of dates. Asking about a symbol before its listing or after its delisting is an ERROR. A bare symbol list is
                        refused. A universe is "point in time" ONLY when rebuilt from verified raw ``universe_capture`` directories (``from_capture_dirs``);
                        a hand-picked list, a source name or an attestation with hashes in it is a claim and never is.
  PITSeries / AsOfView  ``series.asof(t)`` returns a view that does not CONTAIN anything stamped after t (or not yet RECEIVED by t), so reading the future is
                        impossible, not discouraged. ``detect_lookahead`` re-computes a feature on truncated history and compares: a leaking feature is caught;
                        ``audit_receive_times`` fails a decision made before the local receive time of the data it used.
  walk_forward_folds    purged + embargoed expanding walk-forward; train never touches test; test windows are disjoint (audited, raises otherwise).
  VariantLedger         every attempted variant is counted, rejected or not. There is no reset; a fresh object for the same family inherits the count.
  net_edge_bps          cost-aware net edge that REFUSES a zero/unlabelled fee or slippage unless the label is measured or venue_stated.
  historical_evidence / oos_evidence   build ``strategy_verdict.EvidenceItem`` with exactly the metrics ``counted()`` requires, computed, not asserted.

What this is NOT: a profitability claim. A PASS here is offline evidence about one dataset. Gaps are listed in ``GAPS``.
"""
from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import json
import math
import os
import re
import sys
import unicodedata
from dataclasses import dataclass, field, replace as _replace
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

BOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (BOT, os.path.join(BOT, "tools")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import evidence_chain as EC  # noqa: E402
import evidence_stats as ES  # noqa: E402
import statistical_power as SP  # noqa: E402
import strategy_verdict as SV  # noqa: E402

GAPS = (
    "the variant family count survives a restart only if the caller gives VariantLedger a path (append-only hash-chained JSONL); a deleted file plus sidecars is caught only with the caller's external anchors=",
    "AsOfView makes the future unreachable through its API; Python cannot stop deliberate reflection into the caller's own full arrays",
    "detect_lookahead catches leaks visible as a difference between full-history and truncated-history features; a leak through a label is for audit_split",
    "point in time is established ONLY by PointInTimeUniverse.from_capture_dirs: raw universe_capture responses re-hashed and re-parsed here AND passed through universe_capture.verify_capture_dir (absent or failed = refused). "
    "RAW_RESPONSES_VERIFIED means SELF-CONSISTENT BYTES AS CAPTURED BY THE OWNER (hashes, cursor chains and re-derived coverage agree); it is NOT authenticated as produced by the exchange "
    "(a hash shows what was read, not who produced it; universe_pit_verified_against_exchange stays False). A membership source name, includes_delisted, an observed window or a "
    "no-delistings attestation is a claim and is IGNORED for the point-in-time verdict",
    "a verified capture vouches for the universe only from its own snapshot time (for at most max_snapshot_gap_ms, default 7 days): study time before the first genuine capture, after the last one, or across a larger gap is "
    "NOT_POINT_IN_TIME_VERIFIED and a study that needs it is INSUFFICIENT_DATA. No such capture is committed to this repository: every committed corpus is NOT point in time (hand-picked survivors)",
    "survivorship from ignoring Closed contracts is material (OWNER-OBSERVED live probe, not verifiable here: 318 delisted USDT perps against 890 Trading, so about a quarter of the historical USDT perps are Closed); "
    "a Closed symbol's funding ends at its deliveryTime, and only a capture with the Closed partition can see it",
    "funding intervals are per symbol (OWNER-OBSERVED: 480m, 240m, sometimes 60m; 14 of the top 20 positive-funding perps over $5M turnover were 240m): an 8h-only filter drops most high-funding Bybit perps, and "
    "per-print rates must be normalised to bps per day (funding_bps_per_day) before any comparison; a universe record without fundingInterval leaves normalisation UNKNOWN and the capture is refused (FUNDING_INTERVAL_UNKNOWN)",
    "the instrument records are the venue's CURRENT view at each snapshot (launchTime / deliveryTime / status): a contract the venue stopped returning before the first snapshot, and a symbol listed and delisted unseen between two "
    "snapshots, are not recoverable by this method (SYMBOL_VANISHED_WITHOUT_RECORD flags the second only when a later snapshot omits a symbol that was seen)",
    "committed corpora carry venue event time only, not the local RECEIVE time: decision_time <= receive_time can be audited (audit_receive_times, PITSeries(recv_ms=)) only for data collected with receive stamps; "
    "the committed funding/price corpora are bound to event time alone (receive_time_bound=False)",
    "spot_listed / hedgeability come from ONE current spot snapshot (CURRENT_SNAPSHOT_ONLY): a label for today's hedge availability, never a historical fact and never an input to a historical ranking or selection",
    "membership ends at the first non-Trading sighting at the latest (deliveryTime is used when present and sane); a Closed/Delivering row WITHOUT a deliveryTime is CLOSED_WITHOUT_DELIVERY and makes the universe not point in time; "
    "a PreLaunch / PendingOpen symbol is not a tradable member until it is first seen Trading",
    "the selection log is verified by recomputing a best-train-mean selector; a different (documented) selection rule needs its own selector= and is only as honest as that function",
)

MS_8H = 8 * 3600 * 1000


class HarnessError(ValueError):
    pass


class LookAheadError(HarnessError):
    """Something tried to read a value stamped after the as-of time."""


class UniverseError(HarnessError):
    """A symbol was queried outside its membership interval."""


class SurvivorshipError(HarnessError):
    """A universe was built without as-of dates, or claims point-in-time with a survivor-only source."""


class CostError(HarnessError):
    """A cost input is missing, zero without measurement, unlabelled, unknown or not a number."""


def _int_ms(x: Any, name: str) -> int:
    if isinstance(x, bool) or not isinstance(x, int):
        raise HarnessError(f"{name} must be an integer millisecond timestamp, got {x!r}")
    return x


# ---------------------------------------------------------------- point-in-time universe
#
# A universe is point in time ONLY when it is REBUILT, here, from raw captured exchange responses that verify (``PointInTimeUniverse.from_capture_dirs``).
# A membership source name, an ``includes_delisted`` flag, an observed window or an attestation with hashes in it are all CLAIMS: a hand-built universe keeps
# them as ``source assertion`` metadata and can never be point in time. ``universe_pit_basis`` says which it was: RAW_RESPONSES_VERIFIED / PARTIALLY_VERIFIED / ASSERTED.

#: Sources whose membership list is built from what survived to today: never point in time.
NON_PIT_SOURCES = ("hand_picked", "hand_picked_survivors", "current_listing", "current_top_n", "survivors", "unknown")
#: Names a SOURCE ASSERTION may use. Asserting one grants nothing: only verified raw captures make a universe point in time.
PIT_SOURCE_KINDS = ("exchange_instruments_info_snapshots_with_status_history", "recorded_universe_snapshots")
UNIVERSE_CAPTURE_SCHEMA = "universe_capture/v2"
#: Bybit V5 instruments-info statuses. ``Closed`` is how a delisted contract stays visible: a capture without that partition cannot see a delisting.
ENDED_STATUSES = ("Closed", "Delivering")       # a contract in either can no longer be opened: membership ends at the first such sighting at the latest
NEVER_MEMBER = 2 ** 53                          # listing time of a symbol that was never seen Trading (PreLaunch / PendingOpen only)
DEFAULT_REQUIRED_STATUSES = ("PreLaunch", "Trading", "Delivering", "Closed")
#: A snapshot vouches for the universe until the next one, but never for longer than this: a symbol listed and delisted unseen between two far-apart snapshots
#: is invisible, so a longer stretch is NOT point-in-time verified.
DEFAULT_MAX_SNAPSHOT_GAP_MS = 7 * 86_400_000
PIT_RAW = "RAW_RESPONSES_VERIFIED"       # = self-consistent bytes as captured by the owner; NOT authenticated as produced by the exchange
PIT_PARTIAL = "PARTIALLY_VERIFIED"
PIT_ASSERTED = "ASSERTED"
NOT_VERIFIED = "NOT_POINT_IN_TIME_VERIFIED"
_UNSET = object()
_MS_DIGITS = re.compile(r"[0-9]{1,16}", re.ASCII)


def _sha256(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _resolve_verifier() -> Optional[Callable[[str], Any]]:
    """``universe_capture.verify_capture_dir`` or None (adapter boundary: the capture tool lives in another module and may predate the verifier)."""
    try:
        import importlib
        fn = getattr(importlib.import_module("universe_capture"), "verify_capture_dir", None)
    except Exception:                                                  # noqa: BLE001
        return None
    return fn if callable(fn) else None


def _row_ms(v: Any) -> Optional[int]:
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v if v >= 0 else None
    if isinstance(v, str) and _MS_DIGITS.fullmatch(v):
        return int(v)
    return None


@dataclass(frozen=True)
class _Obs:
    snap_ms: int
    status: str
    launch_ms: int
    delivery_ms: int                       # 0 = none recorded
    lineage: Optional[str]
    interval_min: Optional[int] = None     # the venue's own fundingInterval in minutes (None = the record did not state it)


@dataclass
class _CaptureLoad:
    """What the harness itself established from the raw files of one capture directory."""
    path: str
    snapshot_ms: int
    manifest_sha256: str
    rows: Dict[str, _Obs]
    spot_symbols: Optional[frozenset] = None        # Trading spot symbols from a COMPLETE spot partition (None = no spot partition captured)


@dataclass
class _CaptureEvidence:
    loaded: List[_CaptureLoad]
    problems: List[str]
    study_from_ms: int
    study_to_ms: int
    required_statuses: Tuple[str, ...]
    max_gap_ms: int


def _load_capture(path: str, verify: Callable[[str], Any], required: Sequence[str]) -> Tuple[Optional[_CaptureLoad], List[str]]:
    """Verify one capture directory TWICE: by the capture tool's own ``verify_capture_dir`` and by re-hashing / re-parsing the raw files here. Any problem at all
    means the directory contributes nothing."""
    probs: List[str] = []
    name = os.path.basename(os.path.normpath(str(path))) or str(path)
    if not isinstance(path, str) or not os.path.isdir(path) or os.path.islink(path):
        return None, [f"CAPTURE_DIR_MISSING:{name}"]
    real = os.path.realpath(path)
    parts_ = real.split(os.sep)
    if any(a == "bot" and b == "state" for a, b in zip(parts_, parts_[1:])):
        return None, [f"CAPTURE_DIR_REFUSED_PROTECTED_PATH:{name}"]           # bot/state is never read
    try:
        v = verify(path)
    except Exception as exc:                                           # noqa: BLE001
        return None, [f"VERIFY_CAPTURE_DIR_FAILED:{name}:raised {type(exc).__name__}"]
    if not isinstance(v, Mapping):
        return None, [f"VERIFY_CAPTURE_DIR_FAILED:{name}:no result"]
    if v.get("ok") is not True:
        why = v.get("problems")
        return None, [f"VERIFY_CAPTURE_DIR_FAILED:{name}:{(why[0] if isinstance(why, list) and why else 'not ok')}"]
    cov = v.get("coverage") if isinstance(v.get("coverage"), Mapping) else {}
    # ``ok`` is INTEGRITY only: a PARTIAL / CONFLICTED / STALE capture honestly described still verifies. The claim needs the RE-DERIVED coverage too.
    if cov.get("dataset_status") != "COMPLETE":
        return None, [f"CAPTURE_DATASET_NOT_COMPLETE:{name}:{cov.get('dataset_status')}"]
    if cov.get("delisting_observability") != "OBSERVED":
        return None, [f"DELISTING_NOT_OBSERVABLE"]
    done = {(p.get("category"), p.get("status")) for p in (cov.get("partitions") or []) if isinstance(p, Mapping) and p.get("complete") is True}
    gaps = [f"MISSING_STATUS_PARTITION:{st}" for st in required if ("linear", st) not in done]
    if gaps:
        return None, gaps
    mpath = os.path.join(real, "manifest.json")
    try:
        with open(mpath, "rb") as f:
            mbytes = f.read()
        manifest = json.loads(mbytes.decode("utf-8", "strict"))
    except OSError:
        return None, [f"MANIFEST_MISSING_OR_UNREADABLE:{name}"]
    except (ValueError, RecursionError):
        return None, [f"MANIFEST_NOT_JSON:{name}"]
    msha = _sha256(mbytes)
    reported = v.get("manifest_sha256")
    if isinstance(reported, str) and reported != msha:
        probs.append(f"MANIFEST_SHA256_DIFFERS_FROM_VERIFIER:{name}")
    if not isinstance(manifest, dict):
        return None, probs + [f"MANIFEST_NOT_JSON:{name}"]
    schema = manifest.get("schema") if manifest.get("schema") is not None else manifest.get("schema_version")
    if schema != UNIVERSE_CAPTURE_SCHEMA:
        return None, probs + [f"MANIFEST_SCHEMA_NOT_V2:{schema!r}"]
    if manifest.get("delisting_observability") != "OBSERVED":
        probs.append("DELISTING_NOT_OBSERVABLE")
    parts = manifest.get("partitions")
    if not isinstance(parts, list) or not parts:
        return None, probs + [f"MANIFEST_HAS_NO_PARTITIONS:{name}"]
    covered: set = set()
    rows: Dict[str, _Obs] = {}
    row_part: Dict[str, str] = {}
    recv_times: List[int] = []
    spot: set = set()
    spot_ok = False
    for pi, part in enumerate(parts):
        if not isinstance(part, dict) or part.get("category") not in ("linear", "spot") or not (part.get("status") is None or (isinstance(part.get("status"), str) and part["status"])):
            probs.append(f"PARTITION_MALFORMED:{name}#{pi}")
            continue
        status = part["status"]                                        # None = the unfiltered 'default' partition
        category = part["category"]
        label = status or "default"
        pages = part.get("pages")
        ok_part = True
        if part.get("complete") is not True:
            probs.append(f"PARTITION_NOT_COMPLETE:{name}/{label}")
            ok_part = False
        if not isinstance(pages, list) or not pages:
            probs.append(f"PARTITION_HAS_NO_PAGES:{name}/{label}")
            continue
        expected_req, seen = "", set()
        for k, pg in enumerate(pages):
            last = k == len(pages) - 1
            if not isinstance(pg, dict):
                probs.append(f"PAGE_MALFORMED:{name}/{status}#{k}")
                ok_part = False
                continue
            req = pg.get("cursor_in") if "cursor_in" in pg else pg.get("request_cursor")
            nxt = pg.get("cursor_out") if "cursor_out" in pg else pg.get("next_cursor")
            req, nxt = req or "", nxt or ""
            if req != expected_req or (not nxt and not last):
                probs.append(f"CURSOR_CHAIN_BROKEN:{name}/{label}#{k}")
                ok_part = False
            if nxt and (nxt == req or nxt in seen):
                probs.append(f"CURSOR_REPEATED:{name}/{label}#{k}")
                ok_part = False
            if nxt:
                seen.add(nxt)
            if last and nxt:
                probs.append(f"CURSOR_CHAIN_NOT_TERMINATED:{name}/{label}")
                ok_part = False
            expected_req = nxt
            fname = pg.get("file")
            if not isinstance(fname, str) or not fname or fname != os.path.basename(fname) or fname.startswith(".") or "\x00" in fname or "\\" in fname:
                probs.append(f"RAW_PAGE_NAME_UNSAFE:{name}/{str(fname)[:40]}")
                ok_part = False
                continue
            fpath = os.path.join(real, fname)
            try:
                if os.path.islink(fpath):
                    raise OSError("link")
                with open(fpath, "rb") as f:
                    raw = f.read()
            except OSError:
                probs.append(f"RAW_PAGE_MISSING:{name}/{fname}")
                ok_part = False
                continue
            declared = pg.get("sha256")
            if _sha256(raw) != declared:
                probs.append(f"RAW_PAGE_SHA256_MISMATCH:{name}/{fname}")
                ok_part = False
                continue
            recv = _row_ms(pg.get("recv_ms"))
            if recv is None:
                probs.append(f"RAW_PAGE_RECV_TIME_INVALID:{name}/{fname}")
                ok_part = False
            else:
                recv_times.append(recv)
            try:
                doc = json.loads(raw.decode("utf-8", "strict"))
                res = doc["result"]
                if doc.get("retCode") != 0 or isinstance(doc.get("retCode"), bool) or res.get("category") != category or not isinstance(res.get("list"), list):
                    raise ValueError("envelope")
            except (ValueError, KeyError, TypeError, AttributeError, RecursionError):
                probs.append(f"RAW_PAGE_NOT_A_V5_ENVELOPE:{name}/{fname}")
                ok_part = False
                continue
            if (res.get("nextPageCursor") or "") != nxt:
                probs.append(f"CURSOR_RAW_DIFFERS_FROM_MANIFEST:{name}/{fname}")
                ok_part = False
            for r in res["list"]:
                if category == "spot":
                    if isinstance(r, dict) and isinstance(r.get("symbol"), str) and r.get("status") == "Trading":
                        spot.add(r["symbol"])
                    continue
                if not isinstance(r, dict) or not isinstance(r.get("symbol"), str) or not r["symbol"]:
                    probs.append(f"ROW_MALFORMED:{name}/{fname}")
                    ok_part = False
                    continue
                if r.get("contractType") != "LinearPerpetual" or r.get("quoteCoin") != "USDT" or r.get("settleCoin") != "USDT":
                    continue                                           # not a USDT perpetual: not a universe member (and not an error)
                sym = r["symbol"]
                if status is not None and r.get("status") != status:
                    probs.append(f"ROW_STATUS_DIFFERS_FROM_PARTITION:{sym}")
                    ok_part = False
                    continue
                lm, dm = _row_ms(r.get("launchTime")), _row_ms(r.get("deliveryTime"))
                if lm is None or dm is None:
                    probs.append(f"ROW_FIELD_INVALID:{sym}")
                    ok_part = False
                    continue
                lin = r.get("symbolId")
                iv = _row_ms(r.get("fundingInterval")) if r.get("fundingInterval") not in (None, "") else None
                obs = _Obs(0, str(r.get("status")), lm, dm, lin if isinstance(lin, str) and lin else None, iv if iv and iv > 0 else None)
                if sym in rows:                                            # the same instrument legitimately appears in the 'default' and its status partition
                    o = rows[sym]
                    if (o.status, o.launch_ms, o.delivery_ms, o.lineage, o.interval_min) != (obs.status, lm, dm, obs.lineage, obs.interval_min):
                        probs.append(f"SYMBOL_IN_TWO_PARTITIONS:{sym}" if row_part[sym] != status else f"SYMBOL_DUPLICATE_CONFLICT:{sym}")
                        ok_part = False
                    continue
                rows[sym] = obs
                row_part[sym] = status
        if ok_part and category == "linear" and status is not None:
            covered.add(status)
        if ok_part and category == "spot":
            spot_ok = True
    for st in required:
        if st not in covered:
            probs.append(f"MISSING_STATUS_PARTITION:{st}")
    if not recv_times:
        probs.append(f"NO_PAGE_RECEIVE_TIME:{name}")
    if probs:
        return None, probs
    snap = max(recv_times)                                             # the snapshot is complete only when its last page arrived
    return _CaptureLoad(real, snap, msha, {s: _replace(o, snap_ms=snap) for s, o in rows.items()}, frozenset(spot) if spot_ok else None), []


class PointInTimeUniverse:
    """Membership as of timestamps. Active on [listing, end): listing = launchTime, or the first sighting as Trading for a symbol first seen PreLaunch / PendingOpen;
    end = the earlier of deliveryTime and the first Closed/Delivering sighting (a Closed row with no deliveryTime is CLOSED_WITHOUT_DELIVERY: a problem, never an
    immortal member). Symbols sharing a symbolId across captures (old name pruned, new name appearing) are renames: lineage, not a vanishing.

    Built from verified raw captures (``from_capture_dirs``) it can be point in time. Built from ``members`` it is a DESCRIPTION: ``point_in_time`` is False
    whatever ``membership_source``, ``includes_delisted``, the observed window or ``no_delistings_attestation`` say; they are kept as ignored assertions."""

    def __init__(self, members: Mapping[str, Mapping[str, Any]], *, membership_source: str, includes_delisted: bool,
                 observed_from_ms: Optional[int] = None, observed_to_ms: Optional[int] = None,
                 no_delistings_attestation: Optional[Mapping[str, Any]] = None, _capture: Optional[_CaptureEvidence] = None) -> None:
        if not isinstance(members, Mapping) or isinstance(members, (str, bytes)):
            raise SurvivorshipError("a universe needs {symbol: {listed_ms, delisted_ms}} with as-of dates; a bare symbol list has none and is refused")
        if not members and _capture is None:
            raise SurvivorshipError("an empty universe")
        if not isinstance(membership_source, str) or not membership_source.strip():
            raise SurvivorshipError("the universe must name where its membership records came from")
        self._m: Dict[str, Tuple[int, Optional[int]]] = {}
        for sym, rec in members.items():
            if not isinstance(rec, Mapping) or "listed_ms" not in rec:
                raise SurvivorshipError(f"{sym}: no listed_ms as-of date; a symbol without dates cannot be queried point-in-time")
            lo = _int_ms(rec["listed_ms"], f"{sym}.listed_ms")
            hi = rec.get("delisted_ms")
            if hi is not None:
                hi = _int_ms(hi, f"{sym}.delisted_ms")
                if hi <= lo:
                    raise HarnessError(f"{sym}: delisted_ms must be after listed_ms")
            self._m[str(sym)] = (lo, hi)
        if (observed_from_ms is None) != (observed_to_ms is None):
            raise HarnessError("observed_from_ms and observed_to_ms must be given together")
        if observed_from_ms is not None:
            _int_ms(observed_from_ms, "observed_from_ms")
            _int_ms(observed_to_ms, "observed_to_ms")
            if observed_to_ms <= observed_from_ms:
                raise HarnessError("observed_to_ms must be after observed_from_ms")
        self.observed_from_ms, self.observed_to_ms = observed_from_ms, observed_to_ms
        self.no_delistings_attestation = dict(no_delistings_attestation) if isinstance(no_delistings_attestation, Mapping) else None   # kept, never believed
        self.membership_source = membership_source.strip()
        self.includes_delisted = bool(includes_delisted)
        self._capture = _capture
        self._first_seen: Dict[str, int] = {}
        self._lineage: Dict[str, str] = {}
        self._intervals: Dict[str, List[Tuple[int, Optional[int]]]] = {}
        self._needs_interval = False
        self._segments: List[Dict[str, Any]] = []

    # ---- construction from raw captures
    @classmethod
    def from_symbol_list(cls, symbols: Sequence[str], **_k: Any) -> "PointInTimeUniverse":
        raise SurvivorshipError("refusing to build a universe from a symbol list with no as-of dates")

    @classmethod
    def from_capture_dirs(cls, dirs: Sequence[str], *, study_from_ms: int, study_to_ms: int, required_statuses: Sequence[str] = DEFAULT_REQUIRED_STATUSES,
                          max_snapshot_gap_ms: int = DEFAULT_MAX_SNAPSHOT_GAP_MS, needs_funding_interval: bool = True, verify: Any = _UNSET) -> "PointInTimeUniverse":
        """Rebuild the universe from one or more ``universe_capture`` directories. ``verify`` is ``universe_capture.verify_capture_dir`` (resolved at call time
        when omitted; None or absent = refused). Never raises for BAD EVIDENCE: the universe records why it is not point in time and answers no query it cannot
        vouch for. It raises only for a malformed request (window, statuses)."""
        lo, hi = _int_ms(study_from_ms, "study_from_ms"), _int_ms(study_to_ms, "study_to_ms")
        if hi <= lo:
            raise HarnessError("study_to_ms must be after study_from_ms")
        gap = _int_ms(max_snapshot_gap_ms, "max_snapshot_gap_ms")
        if gap <= 0:
            raise HarnessError("max_snapshot_gap_ms must be positive")
        req = tuple(required_statuses) if isinstance(required_statuses, (list, tuple)) else ()
        if not req or not all(isinstance(s, str) and s for s in req) or "Closed" not in req:
            raise HarnessError("required_statuses must name every status to be captured and include 'Closed' (a delisted contract is only visible there)")
        if verify is _UNSET:
            verify = _resolve_verifier()
        probs: List[str] = []
        loaded: List[_CaptureLoad] = []
        if not isinstance(dirs, (list, tuple)) or not dirs:
            probs.append("NO_CAPTURE_DIRS")
        elif verify is None:
            probs.append("VERIFY_CAPTURE_DIR_UNAVAILABLE")
        else:
            for d in dirs:
                cap, p = _load_capture(d, verify, req)
                for x in p:
                    if x not in probs:
                        probs.append(x)
                if cap is not None:
                    loaded.append(cap)
        loaded.sort(key=lambda c: c.snapshot_ms)
        uniq: List[_CaptureLoad] = []
        for c in loaded:
            if uniq and uniq[-1].snapshot_ms == c.snapshot_ms:
                probs.append(f"DUPLICATE_SNAPSHOT_TIME:{c.snapshot_ms}")
                continue
            uniq.append(c)
        ev = _CaptureEvidence(uniq, probs, lo, hi, req, gap)
        obs: Dict[str, List[_Obs]] = {}
        for c in uniq:
            for sym, o in c.rows.items():
                obs.setdefault(sym, []).append(o)
        members: Dict[str, Tuple[int, Optional[int]]] = {}
        first_seen: Dict[str, int] = {}
        lineage: Dict[str, str] = {}
        intervals: Dict[str, List[Tuple[int, Optional[int]]]] = {}
        lineage_members: Dict[str, List[str]] = {}
        for sym, seq in obs.items():
            for lin in {o.lineage for o in seq if o.lineage}:
                lineage_members.setdefault(lin, []).append(sym)
        for sym in sorted(obs):
            seq = obs[sym]
            if len({o.launch_ms for o in seq}) > 1:
                probs.append(f"LAUNCHTIME_CONTRADICTION:{sym}")
            deliveries = {o.delivery_ms for o in seq if o.delivery_ms}
            if len(deliveries) > 1:
                probs.append(f"DELIVERYTIME_CONTRADICTION:{sym}")
            seen_delivery = False
            for o in seq:
                if o.delivery_ms:
                    seen_delivery = True
                elif seen_delivery:
                    probs.append(f"DELIVERYTIME_RESCINDED:{sym}")
                    break
            if any(o.status in ENDED_STATUSES and not o.delivery_ms for o in seq):
                probs.append(f"CLOSED_WITHOUT_DELIVERY:{sym}")          # a Closed/Delivering row that states no deliveryTime: when it ended is unknown, so the universe is not point in time
            lins = {o.lineage for o in seq if o.lineage}
            if len(lins) > 1:
                probs.append(f"LINEAGE_CONTRADICTION:{sym}")
            if needs_funding_interval and any(o.interval_min is None for o in seq):
                probs.append(f"FUNDING_INTERVAL_UNKNOWN:{sym}")        # no stated fundingInterval: per-print funding cannot be normalised, so a funding study cannot pass
            launch = seq[0].launch_ms
            first_trading = next((o.snap_ms for o in seq if o.status == "Trading"), None)
            if seq[0].status != "Trading" and seq[0].status not in ENDED_STATUSES:
                lo_m = max(launch, first_trading) if first_trading is not None else NEVER_MEMBER     # PreLaunch / PendingOpen: tradable only from the first sighting as Trading
            else:
                lo_m = launch
            ends = [d for d in deliveries]
            first_ended = next((o.snap_ms for o in seq if o.status in ENDED_STATUSES), None)
            if first_ended is not None:
                ends.append(first_ended)                                  # membership ends at the first non-Trading (Closed/Delivering) sighting at the latest
            members[sym] = (lo_m, min(ends) if ends else None)
            intervals[sym] = [(o.snap_ms, o.interval_min) for o in seq]
            first_seen[sym] = seq[0].snap_ms
            lineage[sym] = sorted(lins)[0] if lins else sym
            for c in uniq:                                                # vanished: seen, then absent from a later complete capture, with no delisting recorded
                if c.snapshot_ms > first_seen[sym] and sym not in c.rows and not any(o.delivery_ms for o in seq if o.snap_ms < c.snapshot_ms):
                    successors = [t for lin in lins for t in lineage_members.get(lin, []) if t != sym and t in c.rows and obs[t][0].snap_ms > first_seen[sym]]
                    if successors:                                        # same symbolId now under another name: a rename whose old row was pruned, not a vanishing
                        cut = min(obs[t][0].snap_ms for t in successors)
                        members[sym] = (members[sym][0], cut if members[sym][1] is None else min(members[sym][1], cut))
                    else:
                        probs.append(f"SYMBOL_VANISHED_WITHOUT_RECORD:{sym}")
                    break
            if deliveries and max(deliveries) <= seq[0].launch_ms:
                probs.append(f"DELIVERYTIME_NOT_AFTER_LAUNCH:{sym}")
        u = cls({}, membership_source=UNIVERSE_CAPTURE_SCHEMA, includes_delisted=True, _capture=ev)
        u._m = members
        u._first_seen = first_seen
        u._lineage = lineage
        u._intervals = intervals
        u._needs_interval = bool(needs_funding_interval)
        u._segments = _segments(uniq, lo, hi, gap)
        return u

    # ---- the verified record
    @property
    def segments(self) -> List[Dict[str, Any]]:
        return [dict(s) for s in self._segments]

    @property
    def unverified_segments(self) -> List[Dict[str, Any]]:
        return [dict(s) for s in self._segments if s["label"] == NOT_VERIFIED]

    def spot_listed(self, symbol: str) -> Optional[bool]:
        """Is there a Bybit SPOT pair (Trading) of this name in the captured spot universe? None = no complete spot partition was captured (hedgeability UNKNOWN)."""
        if self._capture is None:
            return None
        caps = [c for c in self._capture.loaded if c.spot_symbols is not None]
        if not caps:
            return None
        return symbol in caps[-1].spot_symbols

    @property
    def capture_manifest_sha256s(self) -> Tuple[str, ...]:
        return tuple(c.manifest_sha256 for c in self._capture.loaded) if self._capture is not None else ()

    @property
    def symbols(self) -> Tuple[str, ...]:
        return tuple(sorted(self._m))

    def _check_covered(self, t: int) -> None:
        if self._capture is None:
            return
        if self._capture.problems:
            raise UniverseError("this universe's captures did not verify (" + "; ".join(self._capture.problems[:3]) + "): it answers no membership question")
        for s in self._segments:
            if s["from_ms"] <= t < s["to_ms"]:
                if s["label"] == NOT_VERIFIED:
                    raise UniverseError(f"t={t} is in a stretch that is {NOT_VERIFIED}")
                return
        raise UniverseError(f"t={t} is outside the study window or the verified captures")

    def _member(self, symbol: str, t: int) -> bool:
        lo, hi = self._m[symbol]
        first_seen = self._first_seen.get(symbol)
        if first_seen is not None and first_seen > t:
            return False                                              # nobody had recorded it by t: it is not in the universe at t, whatever launchTime says
        return lo <= t and (hi is None or t < hi)

    def active_at(self, t_ms: int) -> List[str]:
        t = _int_ms(t_ms, "t_ms")
        self._check_covered(t)
        return sorted(s for s in self._m if self._member(s, t))

    def require_active(self, symbol: str, t_ms: int) -> None:
        t = _int_ms(t_ms, "t_ms")
        self._check_covered(t)
        if symbol not in self._m:
            raise UniverseError(f"{symbol} is not in the universe")
        lo, hi = self._m[symbol]
        if t < lo:
            raise UniverseError(f"{symbol} queried at {t} before its listing at {lo}")
        if hi is not None and t >= hi:
            raise UniverseError(f"{symbol} queried at {t} at/after its delisting at {hi}")
        if not self._member(symbol, t):
            raise UniverseError(f"{symbol} was not yet recorded by any snapshot at {t}")

    def funding_interval_min(self, symbol: str, t_ms: int) -> int:
        """The symbol's funding interval (minutes, as the venue stated it in the latest snapshot at or before t, else the first). Unstated = UniverseError:
        funding is never compared across symbols on a guessed 8h grid."""
        seq = self._intervals.get(symbol)
        if not seq:
            raise UniverseError(f"{symbol}: no funding interval recorded; funding normalisation is UNKNOWN")
        t = _int_ms(t_ms, "t_ms")
        iv = next((i for snap, i in reversed(seq) if snap <= t), seq[0][1])
        if iv is None:
            raise UniverseError(f"{symbol}: funding interval not stated; funding normalisation is UNKNOWN")
        return iv

    def lineage_of(self, symbol: str) -> str:
        return self._lineage.get(symbol, symbol)

    def renames(self) -> List[Tuple[str, str]]:
        """(old, new) pairs of symbols sharing one ``symbolId``: one instrument that changed its name, not a delisting plus a listing."""
        by: Dict[str, List[str]] = {}
        for s in self._m:
            by.setdefault(self.lineage_of(s), []).append(s)
        out: List[Tuple[str, str]] = []
        for syms in by.values():
            if len(syms) > 1:
                ordered = sorted(syms, key=lambda s: (self._m[s][0], s))
                out.extend(zip(ordered, ordered[1:]))
        return sorted(out)

    def delisted_between(self, from_ms: int, to_ms: int) -> List[str]:
        """Symbols whose delivery falls in [from_ms, to_ms], excluding renamed predecessors (a rename is not a survivorship event)."""
        olds = {a for a, _b in self.renames()}
        return sorted(s for s, (_lo, hi) in self._m.items() if hi is not None and from_ms <= hi <= to_ms and s not in olds)

    def active_lineages_at(self, t_ms: int) -> List[str]:
        return sorted({self.lineage_of(s) for s in self.active_at(t_ms)})

    # ---- the claim
    def survivorship_problems(self) -> List[str]:
        """Why this universe is NOT point in time (not counting unverified stretches of the study window: see ``unverified_segments``). Empty = nothing wrong
        with the captures. A hand-built universe is never empty here."""
        out: List[str] = []
        if self._capture is None:
            out.append("PIT_NEEDS_VERIFIED_RAW_CAPTURES: a membership source, delisting flag, observed window or attestation is a claim and is ignored; "
                       "only universe_capture directories that verify make a universe point in time")
            if self.membership_source in NON_PIT_SOURCES:
                out.append(f"MEMBERSHIP_SOURCE_IS_SURVIVOR_BASED:{self.membership_source}")
            elif self.membership_source not in PIT_SOURCE_KINDS:
                out.append(f"MEMBERSHIP_SOURCE_NOT_ALLOWLISTED:{self.membership_source}")
            if not self.includes_delisted:
                out.append("SOURCE_DOES_NOT_RECORD_DELISTED_SYMBOLS")
            if self.no_delistings_attestation is not None:
                out.append("ATTESTATION_IGNORED")
            return out
        out.extend(self._capture.problems)
        return out

    @property
    def point_in_time(self) -> bool:
        return not self.survivorship_problems() and not self.unverified_segments

    @property
    def funding_normalisation_known(self) -> bool:
        """True only for a capture-built universe whose every symbol states its funding interval."""
        return self._capture is not None and bool(self._m) and all(all(i is not None for _s, i in v) for v in self._intervals.values())

    @property
    def pit_basis(self) -> str:
        """RAW_RESPONSES_VERIFIED only when the harness itself re-verified every raw capture and the study window is fully covered; PARTIALLY_VERIFIED when some
        verified coverage exists but something failed or is uncovered; ASSERTED for everything else (a claim)."""
        if self._capture is None:
            return PIT_ASSERTED
        if self.point_in_time:
            return PIT_RAW
        return PIT_PARTIAL if self._capture.loaded else PIT_ASSERTED


def _segments(caps: Sequence[_CaptureLoad], lo: int, hi: int, gap: int) -> List[Dict[str, Any]]:
    """The study window [lo, hi) cut into stretches that a verified snapshot vouches for (from its time to the next snapshot, at most ``gap`` long) and stretches
    that nothing vouches for (labelled NOT_POINT_IN_TIME_VERIFIED)."""
    ivs: List[List[int]] = []
    for i, c in enumerate(caps):
        end = c.snapshot_ms + gap
        if i + 1 < len(caps):
            end = min(end, caps[i + 1].snapshot_ms)
        if end > c.snapshot_ms:
            if ivs and ivs[-1][1] >= c.snapshot_ms:
                ivs[-1][1] = max(ivs[-1][1], end)
            else:
                ivs.append([c.snapshot_ms, end])
    out: List[Dict[str, Any]] = []
    cur = lo
    for a, b in ivs:
        a, b = max(a, lo), min(b, hi)
        if b <= a:
            continue
        if a > cur:
            out.append({"from_ms": cur, "to_ms": a, "label": NOT_VERIFIED})
        out.append({"from_ms": a, "to_ms": b, "label": PIT_RAW})
        cur = b
    if cur < hi:
        out.append({"from_ms": cur, "to_ms": hi, "label": NOT_VERIFIED})
    merged: List[Dict[str, Any]] = []
    for s in out:
        if merged and merged[-1]["label"] == s["label"] and merged[-1]["to_ms"] == s["from_ms"]:
            merged[-1]["to_ms"] = s["to_ms"]
        else:
            merged.append(s)
    return merged


def funding_bps_per_day(rate_bps_per_print: float, interval_min: int) -> float:
    """Funding normalised to bps PER DAY: rate_bps * 1440 / fundingInterval_min. A per-print rate is meaningless across symbols whose intervals differ
    (owner-observed on Bybit: 480m, 240m and sometimes 60m contracts; +1.00 bps per 8h print == +0.50 bps per 4h print == 3.00 bps/day)."""
    if isinstance(interval_min, bool) or not isinstance(interval_min, int) or interval_min <= 0 or 1440 % interval_min:
        raise HarnessError(f"funding interval must be a positive whole number of minutes dividing a day, got {interval_min!r}")
    if isinstance(rate_bps_per_print, bool) or not isinstance(rate_bps_per_print, (int, float)) or not math.isfinite(rate_bps_per_print):
        raise HarnessError("funding rate must be a finite number")
    return float(rate_bps_per_print) * 1440.0 / interval_min


def funding_bps_over_horizon(rate_bps_per_print: float, interval_min: int, horizon_days: float) -> float:
    """Funding over a holding horizon, from the interval-normalised daily rate. A symbol whose contract is Closed earns nothing after its deliveryTime:
    cap ``horizon_days`` at the delivery yourself (``PointInTimeUniverse.require_active``)."""
    if isinstance(horizon_days, bool) or not isinstance(horizon_days, (int, float)) or not math.isfinite(horizon_days) or horizon_days < 0:
        raise HarnessError("horizon_days must be a finite non-negative number")
    return funding_bps_per_day(rate_bps_per_print, interval_min) * float(horizon_days)


# ---------------------------------------------------------------- no-look-ahead series access

class AsOfView:
    """History up to and including ``cutoff_ms``. It holds ONLY those values: later ones are not in the object."""
    __slots__ = ("cutoff_ms", "_ts", "_vals", "_withheld")

    def __init__(self, cutoff_ms: int, ts: Tuple[int, ...], vals: Tuple[float, ...], withheld: frozenset = frozenset()) -> None:
        object.__setattr__(self, "cutoff_ms", cutoff_ms)
        object.__setattr__(self, "_ts", ts)
        object.__setattr__(self, "_vals", vals)
        object.__setattr__(self, "_withheld", withheld)               # event stamps <= cutoff whose value had NOT been received by the cutoff

    def __setattr__(self, k: str, v: Any) -> None:
        raise AttributeError("an AsOfView is immutable")

    def __len__(self) -> int:
        return len(self._ts)

    def values(self) -> List[float]:
        return list(self._vals)

    def timestamps(self) -> List[int]:
        return list(self._ts)

    def last(self, n: int = 1) -> List[float]:
        return list(self._vals[-n:]) if n > 0 else []

    def at(self, ts: int) -> float:
        t = _int_ms(ts, "ts")
        if t > self.cutoff_ms:
            raise LookAheadError(f"read of ts={t} is after the as-of time {self.cutoff_ms}")
        if t in self._withheld:
            raise LookAheadError(f"the value stamped {t} had not been RECEIVED by the as-of time {self.cutoff_ms}")
        try:
            return self._vals[self._ts.index(t)]
        except ValueError:
            raise KeyError(t) from None

    def __getitem__(self, i: int) -> float:
        return self._vals[i]


class PITSeries:
    """A timestamped series whose ONLY read path is ``asof(t)``.

    ``ts`` is the EVENT time. ``recv_ms`` (optional, one per row) is when this process RECEIVED the value: with it, ``asof(t)`` shows only rows already received
    by ``t`` (a decision at ``t`` cannot use a print that reached us later, whatever its event stamp says). Without it the series is bound to event time only
    (``receive_time_bound`` is False) and says so."""

    def __init__(self, ts: Sequence[int], vals: Sequence[float], recv_ms: Optional[Sequence[int]] = None) -> None:
        if len(ts) != len(vals):
            raise HarnessError("ts and vals differ in length")
        t = [_int_ms(x, "timestamp") for x in ts]
        if any(b <= a for a, b in zip(t, t[1:])):
            raise HarnessError("timestamps must be strictly increasing")
        v = [float(x) for x in vals]
        if not all(math.isfinite(x) for x in v):
            raise HarnessError("non-finite value in series")
        self._ts, self._vals = tuple(t), tuple(v)
        self._recv: Optional[Tuple[int, ...]] = None
        self._recv_monotone = True
        if recv_ms is not None:
            if len(recv_ms) != len(t):
                raise HarnessError("recv_ms and ts differ in length")
            r = [_int_ms(x, "recv_ms") for x in recv_ms]
            if any(rr < tt for rr, tt in zip(r, t)):
                raise HarnessError("a value cannot be received before its event time (clock error or forged receive time)")
            self._recv = tuple(r)
            self._recv_monotone = all(b >= a for a, b in zip(r, r[1:]))

    def __len__(self) -> int:
        return len(self._ts)

    @property
    def receive_time_bound(self) -> bool:
        return self._recv is not None

    def asof(self, t_ms: int) -> AsOfView:
        t = _int_ms(t_ms, "t_ms")
        avail = self._recv if self._recv is not None else self._ts
        if self._recv is None or self._recv_monotone:
            lo, hi = 0, len(avail)
            while lo < hi:                                     # bisect_right on the availability stamps
                mid = (lo + hi) // 2
                if avail[mid] <= t:
                    lo = mid + 1
                else:
                    hi = mid
            keep = list(range(lo))
        else:                                                   # a late arrival breaks monotonicity: filter row by row
            keep = [i for i, a in enumerate(avail) if a <= t]
        withheld = frozenset(self._ts[i] for i in range(len(self._ts)) if self._ts[i] <= t and self._recv[i] > t) if self._recv is not None else frozenset()
        return AsOfView(t, tuple(self._ts[i] for i in keep), tuple(self._vals[i] for i in keep), withheld)


@dataclass(frozen=True)
class LeakAudit:
    passed: bool
    n_probes: int
    mismatches: Tuple[int, ...]


def detect_lookahead(feature_fn: Callable[[List[int], List[float]], List[float]], ts: Sequence[int], vals: Sequence[float], *,
                     probes: Optional[Sequence[int]] = None) -> LeakAudit:
    """``feature_fn(ts, vals)`` returns one feature per input row. A causal feature gives the same value at row i whether or not rows after i exist.
    Compare full-history output at i with the output on history truncated at i; any difference (or NaN where the other is not) is a leak."""
    t, v = list(ts), list(vals)
    full = feature_fn(t, v)
    if len(full) != len(v):
        raise HarnessError("feature_fn must return one value per input row")
    idx = list(probes) if probes is not None else list(range(0, len(v), max(1, len(v) // 50)))
    bad: List[int] = []
    for i in idx:
        part = feature_fn(t[:i + 1], v[:i + 1])
        a, b = full[i], part[-1]
        same = (a == b) or (isinstance(a, float) and isinstance(b, float) and math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-12)) or \
               (isinstance(a, float) and isinstance(b, float) and math.isnan(a) and math.isnan(b))
        if not same:
            bad.append(i)
    return LeakAudit(not bad, len(idx), tuple(bad))


def audit_receive_times(decision_ms: Sequence[int], ts: Sequence[int], recv_ms: Sequence[int]) -> LeakAudit:
    """Row ``i`` is a value (event time ``ts[i]``, received locally at ``recv_ms[i]``) used by a decision made at ``decision_ms[i]``. A decision can never precede
    the LOCAL RECEIVE time of the data it uses (nor its event time): any row where it does is a look-ahead leak. Feed the result as ``leak=`` to the evidence builders."""
    if not (len(decision_ms) == len(ts) == len(recv_ms)) or not len(ts):
        raise HarnessError("decision_ms, ts and recv_ms must be non-empty and the same length")
    bad = []
    for i, (d, t, r) in enumerate(zip(decision_ms, ts, recv_ms)):
        _int_ms(d, "decision_ms"), _int_ms(t, "ts"), _int_ms(r, "recv_ms")
        if d < r or d < t:
            bad.append(i)
    return LeakAudit(not bad, len(ts), tuple(bad))


# ---------------------------------------------------------------- walk-forward with purge and embargo

_FOLD_NONCE = os.urandom(16).hex()          # per process: only walk_forward_folds can stamp a Fold


def _fold_tag(train: Sequence[int], test: Sequence[int], n: int, horizon: int, embargo: int) -> str:
    return hashlib.sha256(json.dumps([_FOLD_NONCE, list(train), list(test), n, horizon, embargo]).encode()).hexdigest()


@dataclass(frozen=True)
class Fold:
    """A train/test split. Only ``walk_forward_folds`` stamps (n, label_horizon, embargo, tag): ``oos_evidence`` refuses a Fold that is hand-built or whose
    indices were edited afterwards, so a zero-embargo adjacent split cannot be re-audited against a horizon/embargo the caller chooses."""
    train: Tuple[int, ...]
    test: Tuple[int, ...]
    n: int = -1
    label_horizon: int = -1
    embargo: int = -1
    tag: str = field(default="", repr=False)

    def authentic(self) -> bool:
        return bool(self.tag) and self.tag == _fold_tag(self.train, self.test, self.n, self.label_horizon, self.embargo)


def walk_forward_folds(n: int, train_min: int, test_len: int, label_horizon: int, embargo: int) -> List[Fold]:
    """Expanding walk-forward (statistical_power.walk_forward), then AUDITED: every fold clean per ``audit_split`` and test blocks pairwise disjoint and
    chronological. ``embargo`` must be >= max(1, label_horizon): an embargo shorter than the label horizon lets training labels see test values."""
    if embargo < max(1, label_horizon):
        raise HarnessError(f"embargo ({embargo}) must be at least max(1, label_horizon={label_horizon})")
    folds = [Fold(tuple(a), tuple(b), n, label_horizon, embargo, _fold_tag(a, b, n, label_horizon, embargo))
             for a, b in SP.walk_forward(n, train_min, test_len, label_horizon, embargo)]
    seen_end = -1
    for k, f in enumerate(folds):
        a = SP.audit_split(list(f.train), list(f.test), n, label_horizon, embargo)
        if not a["clean"] or not a["chronological"]:
            raise HarnessError(f"fold {k} leaks: {a['reading']}")
        if f.test[0] <= seen_end:
            raise HarnessError(f"fold {k}: test window overlaps an earlier test window")
        seen_end = f.test[-1]
    return folds


# ---------------------------------------------------------------- the variant counter

_FAMILY_HIGH_WATER: Dict[str, int] = {}
_PATH_HIGH_WATER: Dict[str, int] = {}


def normalise_family(family: Any) -> str:
    """The ONE key a family is counted under: NFKC, whitespace collapsed (incl. non-breaking), casefolded. Empty is refused."""
    if not isinstance(family, str):
        raise HarnessError("a variant ledger needs a family name")
    key = " ".join(unicodedata.normalize("NFKC", family).split()).casefold()
    if not key:
        raise HarnessError("a variant ledger needs a family name")
    return key


class VariantLedger:
    """Counts EVERY attempted variant of a research family, including ones rejected before they were ever tested (recorded with p = 1).

    Monotone and unresettable: there is no reset/clear/remove; the hash-chained ``TrialLedger`` underneath detects tampering; and a NEW VariantLedger for
    a family already seen in this process starts from that family's high-water mark. The family key is normalised ('fam', 'fam ', 'FAM' are one family).

    With ``path`` the ledger is persisted as an append-only, hash-chained JSONL file (``evidence_chain``): it is verified on load (a truncated, edited,
    reordered or torn file raises HarnessError), replayed, and every later record is appended, so the count survives a restart and only grows.
    ``anchors`` (``[{"n", "head"}]`` kept OUTSIDE the file) additionally catches a file+sidecar truncated together."""

    def __init__(self, family: str, *, path: Optional[str] = None, anchors: Optional[Sequence[Mapping[str, Any]]] = None) -> None:
        self.family = normalise_family(family)
        self.path = os.path.abspath(path) if path is not None else None
        self._tl = SP.TrialLedger()
        self._meta: List[Dict[str, Any]] = []          # per trial: what the ledger recorded ABOUT the series (digest, ess, independent obs, declared horizon)
        self._persisted = 0
        if self.path is None:
            self._carried = _FAMILY_HIGH_WATER.get(self.family, 0)
        else:
            self._carried = 0
            self._load(anchors)
        self._sync()

    def _load(self, anchors: Optional[Sequence[Mapping[str, Any]]]) -> None:
        if not (os.path.exists(self.path) or anchors):
            return
        v = EC.verify(self.path, anchors)
        if not v["ok"] or not v["fully_chained"]:
            raise HarnessError(f"variant ledger file failed verification: {[i['code'] for i in v['issues']] or 'unchained tail'}")
        lines, quarantined = EC.read_lines(self.path)
        if quarantined:
            raise HarnessError("variant ledger file has a torn or unparseable final line")
        for i, row in enumerate(EC.rows_of(lines)):
            if not isinstance(row, dict) or row.get("kind") != "variant" or row.get("seq") != i:
                raise HarnessError(f"variant ledger row {i} is malformed or out of sequence")
            if row.get("family") != self.family:
                raise HarnessError(f"variant ledger row {i} belongs to family {row.get('family')!r}, not {self.family!r}")
            try:
                self._tl.record(row["strategy_id"], row["params_hash"], row["window_id"], row["statistic"], pvalue=row["pvalue"], n_obs=row["n_obs"])
            except (KeyError, SP.StatisticalPowerError) as exc:
                raise HarnessError(f"variant ledger row {i} is not a valid trial: {exc}") from exc
            self._meta.append({k: row.get(k) for k in ("returns_sha256", "ess", "independent_observations", "horizon")})
        seen = _PATH_HIGH_WATER.get(self.path, 0)
        if self._tl.n_trials < seen:
            raise HarnessError(f"variant ledger file holds {self._tl.n_trials} variants but this process already saw {seen}: truncated")
        self._persisted = self._tl.n_trials

    def _persist(self) -> None:
        if self.path is None:
            return
        t = self._tl.trials[-1]
        EC.append(self.path, {"kind": "variant", "seq": self._persisted, "family": self.family, "strategy_id": t.strategy_id, "params_hash": t.params_hash,
                              "window_id": t.window_id, "statistic": t.statistic, "pvalue": t.pvalue, "n_obs": t.n_obs, **self._meta[-1]})
        self._persisted += 1
        _PATH_HIGH_WATER[self.path] = max(_PATH_HIGH_WATER.get(self.path, 0), self._persisted)

    def _sync(self) -> None:
        total = self._carried + self._tl.n_trials
        if total > _FAMILY_HIGH_WATER.get(self.family, 0):
            _FAMILY_HIGH_WATER[self.family] = total
        if self.path is not None:
            _PATH_HIGH_WATER[self.path] = max(_PATH_HIGH_WATER.get(self.path, 0), self._tl.n_trials)

    @property
    def n_variants(self) -> int:
        """Attempted variants in this family. Never below the family's high-water mark, whatever object asks."""
        self._tl.verify()
        return max(self._carried + self._tl.n_trials, _FAMILY_HIGH_WATER.get(self.family, 0))

    @property
    def carried_prior_variants(self) -> int:
        return self._carried

    def variant_names(self) -> Tuple[str, ...]:
        """Names of the variants that were actually TESTED (rejected ones are counted but cannot be selected)."""
        self._tl.verify()
        return tuple(sorted({t.strategy_id for t in self._tl.trials if t.window_id != "rejected"}))

    def best_index(self) -> int:
        pv = self._tl.pvalues()
        if not pv:
            raise HarnessError("no variants recorded")
        return min(range(len(pv)), key=lambda i: (pv[i], i))

    def best_trial(self) -> Any:
        return self._tl.trials[self.best_index()]

    def trial_meta(self, index: int) -> Mapping[str, Any]:
        """What the ledger itself recorded about trial ``index``'s series (None fields: recorded before this was tracked, or a rejected variant)."""
        return dict(self._meta[index]) if 0 <= index < len(self._meta) else {}

    def variant_horizon(self, name: str) -> Optional[int]:
        """The label horizon (rows a label looks forward = the variant's hold) DECLARED when the variant was recorded; the largest if recorded repeatedly."""
        hs = [m.get("horizon") for t, m in zip(self._tl.trials, self._meta) if t.strategy_id == name and t.window_id != "rejected"]
        hs = [h for h in hs if isinstance(h, int)]
        return max(hs) if hs else None

    def knows(self, name: str) -> bool:
        """True when ANY trial (tested or rejected) of this variant name is already in the ledger. Used to make an identical re-run idempotent: the same variant on the same series is
        one trial, not a new one, while a different series (new data) or a new name still enlarges the family."""
        return any(t.strategy_id == name for t in self._tl.trials)

    def returns_digests(self, name: str) -> frozenset:
        return frozenset(m.get("returns_sha256") for t, m in zip(self._tl.trials, self._meta) if t.strategy_id == name and m.get("returns_sha256"))

    def anchor(self) -> SP.LedgerAnchor:
        return self._tl.anchor()

    def verify(self, anchor: Optional[SP.LedgerAnchor] = None) -> bool:
        return self._tl.verify(anchor)

    def record(self, name: str, params: Mapping[str, Any], returns: Sequence[float], *, window_id: str = "full",
               horizon: Optional[int] = None) -> Dict[str, Any]:
        """Count a variant AND test it. Returns its significance statistics. The ledger remembers a digest of the exact series, its ESS / independent
        observations (evidence takes those from here, never from the caller) and the variant's declared label ``horizon`` (rows a label looks forward;
        ``oos_evidence`` needs it to size the purge/embargo)."""
        if horizon is not None and (isinstance(horizon, bool) or not isinstance(horizon, int) or horizon < 1):
            raise HarnessError(f"horizon must be a positive integer number of rows, got {horizon!r}")
        st = significance(returns, ci=False)
        self._tl.record(name, SP.params_hash(params), window_id, st["mean"] if st["mean"] is not None else 0.0, pvalue=st["p_one_sided"], n_obs=st["n"])
        self._meta.append({"returns_sha256": series_digest(returns), "ess": st["ess"], "independent_observations": st["independent_observations"], "horizon": horizon})
        self._persist()
        self._sync()
        st["variant"] = name
        return st

    def record_rejected(self, name: str, params: Mapping[str, Any], reason: str) -> None:
        """Count a variant dropped before/without testing (bad data, a failed sanity check). It enlarges the family with p = 1."""
        if not isinstance(reason, str) or not reason.strip():
            raise HarnessError("a rejected variant needs a reason")
        self._tl.record(name, SP.params_hash({**dict(params), "_rejected": reason}), "rejected", 0.0, pvalue=None, n_obs=0)
        self._meta.append({"returns_sha256": None, "ess": None, "independent_observations": None, "horizon": None})
        self._persist()
        self._sync()

    def _refuse(self, *_a: Any, **_k: Any) -> None:
        raise HarnessError("the variant counter cannot be reset, cleared or reduced")

    reset = clear = remove = pop = decrement = _refuse

    def selection(self, *, alpha: float = 0.05, method: str = "holm") -> Dict[str, Any]:
        """The best variant judged against the WHOLE family: adjusted over n_variants (carried and unreported ones count as p = 1)."""
        if method not in ("holm", "bh"):
            raise HarnessError("method must be holm or bh")
        pv = self._tl.pvalues()
        if not pv:
            raise HarnessError("no variants recorded")
        n = self.n_variants
        adj = SP.adjust_pvalues(pv, method, alpha=alpha, n_tested=n)
        k = min(range(len(pv)), key=lambda i: (pv[i], i))
        t = self._tl.trials[k]
        return {"best_variant": t.strategy_id, "best_params_hash": t.params_hash, "best_p_unadjusted": pv[k], "best_p_adjusted": adj["adjusted"][k],
                "significant_after_adjustment": bool(adj["reject"][k]), "n_variants_tested": n, "method": method, "alpha": alpha,
                "n_rejected_of_family": adj["n_rejected"]}


def series_digest(returns: Sequence[float]) -> str:
    """Digest of an exact return series (repr of each float: round-trips, so the same series gives the same digest on every run)."""
    return hashlib.sha256(",".join(repr(float(x)) for x in returns).encode()).hexdigest()


# ---------------------------------------------------------------- significance (reuses ESS + bootstrap + t tail)

def significance(returns: Sequence[float], *, alpha: float = 0.05, ci: bool = True) -> Dict[str, Any]:
    """One-sided test of mean > 0 with an ESS-adjusted standard error. Serial dependence shrinks the sample: ``ess`` is the conservative ESS from
    evidence_stats, ``independent_observations`` is its floor. Fewer than 8 points, ESS < 3, or zero variance: p = 1 (no claim)."""
    xs = [float(x) for x in returns]
    n = len(xs)
    out: Dict[str, Any] = {"n": n, "mean": None, "sd": None, "ess": None, "independent_observations": 0, "t": None, "p_one_sided": 1.0, "ci": None}
    if n < ES.MIN_FOR_CI:
        return out
    m = sum(xs) / n
    sd = math.sqrt(sum((x - m) ** 2 for x in xs) / (n - 1))
    ess = ES.conservative_ess(xs) or 1.0
    out.update({"mean": m, "sd": sd, "ess": ess, "independent_observations": int(math.floor(min(float(n), ess)))})
    if sd <= 0 or ess < 3.0:
        return out
    t = m / (sd / math.sqrt(ess))
    out["t"] = t
    out["p_one_sided"] = SP.student_t_sf(t, ess - 1.0)
    if ci:                                                  # the bootstrap is the expensive part: the ledger skips it for every variant, builders ask for the winner's
        out["ci"] = ES.block_bootstrap_ci(xs, alpha=alpha * 2)
    return out


# ---------------------------------------------------------------- costs

_REAL_ZERO_LABELS = ("measured", "venue_stated")
_COST_LABELS = SV.PROVENANCE_LABELS


def net_edge_bps(gross_edge_bps: float, costs: Mapping[str, Mapping[str, Any]]) -> Dict[str, Any]:
    """Net edge per round trip in bps. ``costs`` MUST carry ``fee_bps`` and ``slippage_bps`` as {"value", "provenance"}; may carry others (``funding_bps`` ...).
    Refuses: a missing component, a non-number, a negative, an unlabelled or 'unknown' label, and a ZERO unless labelled measured/venue_stated."""
    if isinstance(gross_edge_bps, bool) or not isinstance(gross_edge_bps, (int, float)) or not math.isfinite(gross_edge_bps):
        raise CostError("gross edge must be a finite number")
    if not isinstance(costs, Mapping):
        raise CostError("costs must be a mapping")
    for req in ("fee_bps", "slippage_bps"):
        if req not in costs:
            raise CostError(f"missing cost component {req}")
    total = 0.0
    labels: Dict[str, str] = {}
    for k, e in costs.items():
        if not isinstance(e, Mapping) or "value" not in e or e.get("provenance") not in _COST_LABELS:
            raise CostError(f"{k}: needs {{value, provenance in {_COST_LABELS}}}")
        v, lab = e["value"], e["provenance"]
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
            raise CostError(f"{k}: value is not a finite number")
        if v < 0:
            raise CostError(f"{k}: a negative cost (a rebate) must be modelled as its own labelled positive edge, not a negative cost")
        if lab == "unknown":
            raise CostError(f"{k}: provenance 'unknown' - unknown is not zero and not a number")
        if v == 0 and lab not in _REAL_ZERO_LABELS:
            raise CostError(f"{k}: zero cost labelled {lab!r}; a zero must be measured or venue_stated")
        total += float(v)
        labels[k] = lab
    return {"gross_edge_bps": float(gross_edge_bps), "total_cost_bps": total, "net_edge_bps_after_costs": float(gross_edge_bps) - total,
            "unknown_cost_inputs": 0, "labels": labels}


# ---------------------------------------------------------------- data hash

def sha256_files(paths: Sequence[str]) -> str:
    """SHA-256 of a file (one path: exactly the file's digest, as ``data_sha256`` requires); of several, the digest of their sorted 'basename:sha' lines."""
    if not paths:
        raise HarnessError("no input data files named")
    digs = []
    for p in paths:
        h = hashlib.sha256()
        with open(p, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        digs.append((os.path.basename(p), h.hexdigest()))
    if len(digs) == 1:
        return digs[0][1]
    return hashlib.sha256("\n".join(f"{n}:{d}" for n, d in sorted(digs)).encode()).hexdigest()


# ---------------------------------------------------------------- evidence builders

def _guards(universe: PointInTimeUniverse, leak: LeakAudit, split_clean: bool = True) -> Tuple[bool, bool, List[str]]:
    probs = universe.survivorship_problems()
    pit = universe.point_in_time
    uses_future = not (leak.passed and split_clean)
    notes = list(probs) + [f"{NOT_VERIFIED}:{s['from_ms']}-{s['to_ms']}" for s in universe.unverified_segments] + ([] if leak.passed else [f"LOOKAHEAD_DETECTED_AT_ROWS:{list(leak.mismatches)[:5]}"]) + ([] if split_clean else ["SPLIT_LEAKS"])
    return pit, uses_future, notes


def _stamp(spec: Mapping[str, Any]) -> Dict[str, str]:
    """candidate_id + spec_digest of the exact specification this evidence is about (``counted`` refuses a PASS that does not match its spec)."""
    if not isinstance(spec, Mapping) or not isinstance(spec.get("candidate_id"), str) or not spec["candidate_id"]:
        raise HarnessError("evidence must be built against a candidate spec with a candidate_id")
    return {"candidate_id": spec["candidate_id"], "spec_digest": SV.spec_digest(spec)}


def _build(stage: str, *, stats: Mapping[str, Any], p_adjusted: float, alpha: float, n_variants: int, universe: PointInTimeUniverse, leak: LeakAudit,
           data_sha256: str, provenance: str, split_clean: bool, extra: Mapping[str, Any], spec: Mapping[str, Any], method: str = "holm",
           family: Optional[str] = None, dsr: Optional[Mapping[str, Any]] = None) -> SV.EvidenceItem:
    pit, uses_future, notes = _guards(universe, leak, split_clean)
    n_ind = int(stats["independent_observations"])
    mean = stats["mean"]
    significant = p_adjusted <= alpha and mean is not None and mean > 0
    enough = n_ind >= SV.MIN_OBSERVATIONS[stage]
    guards_ok = pit and not uses_future
    if significant and guards_ok and enough:
        result = SV.PASS
    elif not significant and enough:
        result = SV.FAIL                       # adequately sized and still not significant after correction: a kill
    else:
        result = SV.INCONCLUSIVE               # significant-but-biased, or too little data to say anything either way
    metrics = {"independent_observations": n_ind, "n_variants_tested": int(n_variants), "multiple_testing_adjusted": True,
               "uses_future_information": bool(uses_future), "universe_point_in_time": bool(pit), "alpha": alpha, "p_adjusted": p_adjusted,
               "universe_pit_basis": universe.pit_basis, "universe_pit_verified_against_exchange": False,
               "universe_pit_independently_verified": universe.pit_basis == PIT_RAW, "universe_pit_source_assertion": universe.membership_source,
               "universe_pit_unverified_segments": universe.unverified_segments, "universe_capture_manifest_sha256": list(universe.capture_manifest_sha256s),
               "universe_funding_normalisation_known": universe.funding_normalisation_known,
               "multiple_testing_family": family if family is not None else normalise_family(spec.get("multiple_testing_family") or spec.get("candidate_id")),
               "multiple_testing_correction": method, "deflated_sharpe": dict(dsr) if dsr is not None else {"applicable": False, "reason": "returns not supplied"},
               "mean_net_per_settlement_bps": mean, "ess": stats["ess"], "n_raw": stats["n"], "guard_notes": notes}
    metrics.update(dict(extra))
    return SV.EvidenceItem(stage=stage, result=result, provenance=provenance, metrics=metrics, data_sha256=data_sha256, **_stamp(spec))


def _require_family(spec: Mapping[str, Any], ledger: VariantLedger) -> None:
    """The ledger must count the SAME family the candidate registered (``multiple_testing_family``, else its candidate_id, as ``register_candidate`` does).
    Otherwise a best-of-N winner can be re-recorded in a fresh, differently named ledger with n_variants = 1."""
    want = normalise_family(spec.get("multiple_testing_family") or spec.get("candidate_id"))
    if ledger.family != want:
        raise HarnessError(f"the variant ledger counts family {ledger.family!r} but the candidate's multiple_testing_family is {want!r}")


def _dsr(returns: Optional[Sequence[float]], n_trials: int) -> Dict[str, Any]:
    """Deflated Sharpe (tools/deflated_sharpe) of ``returns`` against the WHOLE family's trial count. Informational: it is declared in the evidence, not a gate."""
    if returns is None:
        return {"applicable": False, "reason": "returns not supplied to the evidence builder"}
    import deflated_sharpe as DS
    try:
        r = DS.deflated_sharpe_ratio([float(x) for x in returns], int(n_trials))
    except (ValueError, ZeroDivisionError) as exc:
        return {"applicable": False, "reason": f"not computable: {exc}"}
    return {"applicable": True, "dsr": r["dsr"], "observed_sharpe": r["observed_sharpe"], "expected_max_sharpe_under_null": r["expected_max_sharpe_under_null"],
            "n_trials": int(n_trials), "meets_0_95": bool(r["dsr"] >= 0.95)}


def historical_evidence(*, stats: Mapping[str, Any], ledger: VariantLedger, spec: Mapping[str, Any], universe: PointInTimeUniverse, leak: LeakAudit,
                        data_sha256: str, alpha: float = 0.05, method: str = "holm", provenance: str = "offline_backtest",
                        extra: Optional[Mapping[str, Any]] = None, returns: Optional[Sequence[float]] = None) -> SV.EvidenceItem:
    """Historical_test evidence for the BEST variant of a family, judged against the whole family. ``n_variants_tested`` and the adjusted p come from the
    LEDGER (never a free integer), and ``stats`` must be the ledger's best variant's own statistics."""
    _require_family(spec, ledger)
    selection = ledger.selection(alpha=alpha, method=method)
    best = ledger.best_trial()
    if stats.get("n") != best.n_obs or stats.get("mean") is None or best.statistic is None or \
            not math.isclose(float(stats["mean"]), float(best.statistic), rel_tol=1e-9, abs_tol=1e-12):
        raise HarnessError("stats are not the ledger's best variant's statistics (n/mean differ from the recorded trial)")
    meta = ledger.trial_meta(ledger.best_index())
    if meta.get("ess") is None or meta.get("independent_observations") is None:
        raise HarnessError("the ledger holds no recorded ESS / independent observations for its best variant: re-record it so the evidence can be checked against the ledger")
    if stats.get("independent_observations") != meta["independent_observations"] or stats.get("ess") is None or \
            not math.isclose(float(stats["ess"]), float(meta["ess"]), rel_tol=1e-9, abs_tol=1e-12):
        raise HarnessError(f"stats claim independent_observations={stats.get('independent_observations')} ess={stats.get('ess')} but the ledger recorded "
                           f"{meta['independent_observations']} / {meta['ess']}")
    if returns is not None and series_digest(returns) not in ledger.returns_digests(best.strategy_id):
        raise HarnessError("the returns given for the deflated Sharpe are not the series the ledger recorded for its best variant")
    return _build("historical_test", stats=stats, p_adjusted=selection["best_p_adjusted"], alpha=selection["alpha"], n_variants=selection["n_variants_tested"],
                  universe=universe, leak=leak, data_sha256=data_sha256, provenance=provenance, split_clean=True, spec=spec, method=method, family=ledger.family,
                  dsr=_dsr(returns, selection["n_variants_tested"]),
                  extra={"best_variant": selection["best_variant"], **dict(extra or {})})


class SelectionLogError(HarnessError):
    """The per-fold selection log is missing, malformed, or not a function of each fold's train window."""


def train_only_best(train_returns: Mapping[str, Sequence[float]]) -> str:
    """The reference selector: highest mean return over the TRAIN window, ties to the greater name."""
    return max(train_returns, key=lambda k: (sum(train_returns[k]) / len(train_returns[k]), k))


def verify_selection_log(*, selection_log: Any, variant_returns: Mapping[str, Sequence[float]], folds: Sequence[Fold], oos_returns: Sequence[float],
                         tested_variants: Sequence[str], index_offset: int = 0,
                         selector: Callable[[Mapping[str, Sequence[float]]], str] = train_only_best,
                         recorded_digests: Optional[Mapping[str, Any]] = None) -> None:
    """Each fold's chosen variant must be what ``selector`` returns from the TRAIN returns alone, the log's train window must be the fold's, every chosen
    variant must be a counted (tested) variant, and ``oos_returns`` must be exactly the chosen variants' TEST-window returns, in order. Raises otherwise."""
    if not isinstance(selection_log, (list, tuple)) or len(selection_log) != len(folds) or not folds:
        raise SelectionLogError("selection_log must hold exactly one entry per fold")
    if not isinstance(variant_returns, Mapping) or not variant_returns:
        raise SelectionLogError("variant_returns (every variant's full return series) is required to recompute the selection")
    lengths = {len(v) for v in variant_returns.values()}
    if len(lengths) != 1:
        raise SelectionLogError("variant return series differ in length")
    rows = lengths.pop()
    unknown_names = [k for k in variant_returns if k not in set(tested_variants)]
    if unknown_names:
        raise SelectionLogError(f"variants {unknown_names} are not counted in the variant ledger")
    if recorded_digests is not None:
        for name, series in variant_returns.items():
            if series_digest(series[index_offset:]) not in recorded_digests.get(name, ()):
                raise SelectionLogError(f"variant {name!r}: the supplied return series is not the series the ledger recorded for it")
    expected: List[float] = []
    for k, (f, e) in enumerate(zip(folds, selection_log)):
        if not isinstance(e, Mapping) or e.get("fold") != k or not isinstance(e.get("chosen"), str) or e.get("chosen") not in variant_returns:
            raise SelectionLogError(f"fold {k}: malformed entry or unknown variant")
        if list(e.get("train_window") or ()) != [f.train[0], f.train[-1]]:
            raise SelectionLogError(f"fold {k}: logged train window {e.get('train_window')!r} is not the fold's [{f.train[0]}, {f.train[-1]}]")
        idx_tr = [index_offset + t for t in f.train]
        idx_te = [index_offset + t for t in f.test]
        if min(idx_tr) < 0 or max(idx_tr + idx_te) >= rows:
            raise SelectionLogError(f"fold {k}: indices outside the return series")
        recomputed = selector({n: [r[i] for i in idx_tr] for n, r in variant_returns.items()})
        if recomputed != e["chosen"]:
            raise SelectionLogError(f"fold {k}: chosen {e['chosen']!r} is not what the train window alone selects ({recomputed!r})")
        expected.extend(variant_returns[e["chosen"]][i] for i in idx_te)
    if list(oos_returns) != expected:
        raise SelectionLogError("oos_returns are not the chosen variants' test-window returns")


def oos_evidence(*, oos_returns: Sequence[float], ledger: VariantLedger, spec: Mapping[str, Any], universe: PointInTimeUniverse, leak: LeakAudit,
                 folds: Sequence[Fold], data_sha256: str, selection_log: Sequence[Mapping[str, Any]],
                 variant_returns: Mapping[str, Sequence[float]], index_offset: int = 0,
                 selector: Callable[[Mapping[str, Sequence[float]]], str] = train_only_best, alpha: float = 0.05,
                 provenance: str = "offline_walk_forward", extra: Optional[Mapping[str, Any]] = None) -> SV.EvidenceItem:
    """Out-of-sample evidence from STITCHED test-window returns. The p-value is adjusted over the ledger's whole family (which must be the spec's).

    The folds must be ones ``walk_forward_folds`` produced (a hand-built or edited ``Fold`` is refused). Label horizon, embargo and n are NOT caller
    numbers: they are read from the folds' stamp and must cover every variant's DECLARED horizon (``VariantLedger.record(..., horizon=)``) with
    ``embargo >= max(1, horizon)``. Each variant's supplied returns must be the series the ledger recorded, and the ``selection_log`` is RECOMPUTED from
    the train windows (SelectionLogError otherwise)."""
    _require_family(spec, ledger)
    if not folds or not all(isinstance(f, Fold) and f.authentic() for f in folds):
        raise HarnessError("folds must be exactly those produced by walk_forward_folds (a hand-built or edited Fold is refused)")
    params = {(f.n, f.label_horizon, f.embargo) for f in folds}
    if len(params) != 1:
        raise HarnessError("folds were produced with different n / label_horizon / embargo")
    n, label_horizon, embargo = params.pop()
    if embargo < max(1, label_horizon):
        raise HarnessError(f"embargo ({embargo}) must be at least max(1, label_horizon={label_horizon})")
    if not isinstance(variant_returns, Mapping) or not variant_returns:
        raise SelectionLogError("variant_returns (every variant's full return series) is required to recompute the selection")
    declared: Dict[str, int] = {}
    for name in variant_returns:
        h = ledger.variant_horizon(name)
        if h is None:
            raise HarnessError(f"variant {name!r} has no declared label horizon in the ledger: record it with horizon= (its hold, in rows)")
        declared[name] = h
    if label_horizon < max(declared.values()):
        raise HarnessError(f"the folds purge a label horizon of {label_horizon} rows but a variant declares a horizon of {max(declared.values())}")
    rows = len(next(iter(variant_returns.values())))
    if rows - index_offset != n:
        raise HarnessError(f"the folds index {n} rows but the variant returns hold {rows - index_offset} after index_offset")
    verify_selection_log(selection_log=selection_log, variant_returns=variant_returns, folds=folds, oos_returns=oos_returns,
                         tested_variants=ledger.variant_names(), index_offset=index_offset, selector=selector,
                         recorded_digests={name: ledger.returns_digests(name) for name in variant_returns})
    st = significance(oos_returns)
    clean = all(SP.audit_split(list(f.train), list(f.test), n, label_horizon, embargo)["clean"] for f in folds)
    m = ledger.n_variants
    adj = SP.adjust_pvalues([st["p_one_sided"]], "holm", alpha=alpha, n_tested=m)["adjusted"][0]
    return _build("oos_test", stats=st, p_adjusted=adj, alpha=alpha, n_variants=m, universe=universe, leak=leak, data_sha256=data_sha256, provenance=provenance,
                  split_clean=clean, spec=spec, method="holm", family=ledger.family, dsr=_dsr(oos_returns, m), extra={"n_folds": len(folds), "selection_log_verified": True, "label_horizon": label_horizon, "embargo": embargo,
                                                       **dict(extra or {})})


# ---------------------------------------------------------------- the demonstration: cross-sectional funding on 3 hand-picked survivors

FUNDING_FILES = {"BTCUSDT": "BINANCE_LINEAR_BTC_USDT_FUNDING.csv.gz", "ETHUSDT": "BINANCE_LINEAR_ETH_USDT_FUNDING.csv.gz",
                 "SOLUSDT": "BINANCE_LINEAR_SOL_USDT_FUNDING.csv.gz"}
DEMO_COSTS = {"fee_bps": {"value": 5.5, "provenance": "assumed"}, "slippage_bps": {"value": 2.0, "provenance": "assumed"}}
LOOKBACKS = (3, 9, 21, 63, 189)
HOLDS = (1, 3, 9)


#: A settlement's timestamp carries a few tens of ms of jitter AFTER its instant (…001/…047 in the committed files): anything this close behind an 8h
#: boundary is ON that boundary.
FUNDING_SLOT_JITTER_MS = 1_000


def funding_slot(funding_time_ms: int) -> int:
    """The 8h slot a funding print belongs to: the boundary it is on (within the jitter) or the NEXT one after it. A print pays for the interval that
    ENDS at its timestamp, so a 2h-cadence print at 02:00 belongs to the slot ending at 08:00. (The earlier ``round(t / 8h)`` sent the 18:00-20:00 prints
    to the PREVIOUS slot, by banker's rounding at 20:00.)"""
    return -((-(int(funding_time_ms) - FUNDING_SLOT_JITTER_MS)) // MS_8H)


def load_funding(path: str) -> Dict[int, float]:
    """{8h-slot index: funding in bps over that 8h}. Funding is ADDITIVE per settlement, so every print in a slot is SUMMED: Binance ran SOL on 2h/4h
    cadences in Nov-2022 and the earlier loader keyed ``round(t / 8h)`` and OVERWROTE, dropping 75 of 6,655 prints (the Nov-2022 sum was -1,437.5 bps
    after loading against -3,549.1 bps in the file) and shifting others into the wrong slot. A repeated timestamp is a data error, not a second payment:
    it is refused. Assumes the cadence divides 8h (8h, 4h, 2h, 1h); a print that straddled a boundary would be attributed whole to one slot, and a print more than
    ``FUNDING_SLOT_JITTER_MS`` (1,000 ms) behind a boundary belongs to the NEXT slot (the committed files' largest jitter is 47 ms)."""
    out: Dict[int, float] = {}
    seen: set = set()
    with gzip.open(path, "rt", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            t = int(r["funding_time_ms"])
            if t in seen:
                raise HarnessError(f"{path}: a funding print at {t} appears twice: a repeated timestamp is a data error, not a second payment")
            seen.add(t)
            slot = funding_slot(t)
            out[slot] = out.get(slot, 0.0) + float(r["funding_rate"]) * 1e4
    return out


def _variant_returns(grid: List[int], series: Mapping[str, PITSeries], rates: Mapping[str, Mapping[int, float]], lookback: int, hold: int) -> List[float]:
    """Net bps per settlement for: every ``hold`` settlements rank symbols by mean funding over the last ``lookback`` settlements (read ONLY through
    ``asof(previous settlement)``), short the highest, long the lowest. Realised per settlement: funding received on the short minus funding paid on the
    long. Each leg change costs close+open = 2 trades * (fee + slippage). FUNDING LEG ONLY: the price legs are not hedged and not measured here."""
    per_trade = DEMO_COSTS["fee_bps"]["value"] + DEMO_COSTS["slippage_bps"]["value"]
    ret: List[float] = [0.0] * len(grid)
    held: Optional[Tuple[str, str]] = None
    cur: Optional[Tuple[str, str]] = None
    for i, g in enumerate(grid):
        cost = 0.0
        if i % hold == 0 and i >= 1:
            cutoff = grid[i - 1] * MS_8H
            means = {s: sum(v.asof(cutoff).last(lookback)) / max(1, len(v.asof(cutoff).last(lookback))) for s, v in series.items()}
            short, long_ = max(means, key=lambda s: (means[s], s)), min(means, key=lambda s: (means[s], s))
            new = (short, long_)
            if held is None:
                cost = 4 * per_trade
            else:
                cost = (2 * per_trade) * ((held[0] != new[0]) + (held[1] != new[1]))
            held = new
        cur = held
        if cur is None:
            continue
        ret[i] = (rates[cur[0]][g] - rates[cur[1]][g]) - cost
    return ret


def _load_spec(name: str) -> Dict[str, Any]:
    with open(os.path.join(BOT, "research", "candidates", name + ".json"), encoding="utf-8") as f:
        return json.load(f)


def cross_sectional_funding_demo(data_dir: str, *, family: Optional[str] = None, alpha: float = 0.05, spec: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    """Run the whole harness over the committed Binance 3-symbol funding history. Returns the evidence items, the selection, the universe audit and numbers.
    The universe is three hand-picked survivors: the harness marks it NOT point in time and the evidence says so."""
    files = {s: os.path.join(data_dir, "real_funding_full", "funding", fn) for s, fn in FUNDING_FILES.items()}
    raw = {s: load_funding(p) for s, p in files.items()}
    grid = sorted(set.intersection(*[set(r) for r in raw.values()]))
    series = {s: PITSeries([g * MS_8H for g in grid], [raw[s][g] for g in grid]) for s in raw}
    universe = PointInTimeUniverse({s: {"listed_ms": grid[0] * MS_8H} for s in raw}, membership_source="hand_picked_survivors", includes_delisted=False)
    spec = spec if spec is not None else _load_spec("cross_sectional_funding")
    family = family if family is not None else str(spec.get("multiple_testing_family") or spec["candidate_id"])
    vl = VariantLedger(family)             # the evidence builders refuse a ledger whose family is not the spec's multiple_testing_family
    per_variant: Dict[str, List[float]] = {}
    for L in LOOKBACKS:
        for H in HOLDS:
            name = f"L{L}_H{H}"
            per_variant[name] = _variant_returns(grid, series, raw, L, H)
            vl.record(name, {"lookback": L, "hold": H}, per_variant[name][max(LOOKBACKS):], horizon=H)
    n = len(grid)
    # causal-feature audit of the signal's building block (trailing mean) on one real series
    def trailing_mean(ts: List[int], v: List[float]) -> List[float]:
        return [sum(v[max(0, i - 8):i + 1]) / len(v[max(0, i - 8):i + 1]) for i in range(len(v))]
    leak = detect_lookahead(trailing_mean, [g * MS_8H for g in grid[:600]], [raw["BTCUSDT"][g] for g in grid[:600]])
    sha = sha256_files(sorted(files.values()))
    sel = vl.selection(alpha=alpha, method="holm")
    best = sel["best_variant"]
    hist = historical_evidence(stats=significance(per_variant[best][max(LOOKBACKS):]), ledger=vl, spec=spec, universe=universe, leak=leak, data_sha256=sha, alpha=alpha,
                               extra={"funding_leg_only": True, "gross_mean_note": "price legs unhedged and unmeasured; not a P&L claim", "receive_time_bound": False,
                                                                                         "funding_interval_handling": "prints SUMMED into the 8h slot that contains them (Binance BTC/ETH/SOL survivors; SOL ran 2h/4h in Nov-2022); NOT representative of Bybit's mixed 480m/240m/60m universe"},
                               returns=per_variant[best][max(LOOKBACKS):])
    # walk-forward: re-select the best variant on each train window, trade it on the next test window
    h_max, emb = max(HOLDS), max(HOLDS)
    start = max(LOOKBACKS)
    folds = walk_forward_folds(n - start, train_min=1095, test_len=365, label_horizon=h_max, embargo=emb)
    oos: List[float] = []
    chosen: List[str] = []
    log: List[Dict[str, Any]] = []
    for k, f in enumerate(folds):
        tr0, tr1 = start + f.train[0], start + f.train[-1] + 1
        pick = max(per_variant, key=lambda k: (sum(per_variant[k][tr0:tr1]) / (tr1 - tr0), k))
        chosen.append(pick)
        log.append({"fold": k, "chosen": pick, "train_window": [f.train[0], f.train[-1]]})
        oos.extend(per_variant[pick][start + f.test[0]:start + f.test[-1] + 1])
    oos_ev = oos_evidence(oos_returns=oos, ledger=vl, spec=spec, universe=universe, leak=leak, folds=folds,
                          data_sha256=sha, alpha=alpha, selection_log=log, variant_returns={k: v[:] for k, v in per_variant.items()}, index_offset=start,
                          extra={"selected_per_fold": chosen})
    return {"family": family, "grid_settlements": n, "universe_point_in_time": universe.point_in_time, "survivorship_problems": universe.survivorship_problems(),
            "selection": sel, "historical": hist, "oos": oos_ev, "n_folds": len(folds), "leak_audit": leak,
            "best_gross_mean_bps": sum(per_variant[best][start:]) / (n - start), "oos_n": len(oos), "data_sha256": sha}


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Research harness demo. Offline.")
    ap.add_argument("--demo-cross-sectional-funding", action="store_true")
    ap.add_argument("--data-dir", default=os.path.join(BOT, "data"))
    a = ap.parse_args(argv)
    if not a.demo_cross_sectional_funding:
        ap.print_help()
        return 2
    spec = _load_spec("cross_sectional_funding")
    r = cross_sectional_funding_demo(a.data_dir, spec=spec)
    v = SV.evaluate(spec, [r["historical"], r["oos"]])
    print(json.dumps({"universe_point_in_time": r["universe_point_in_time"], "problems": r["survivorship_problems"], "selection": r["selection"],
                      "historical": {"result": r["historical"].result, "metrics": dict(r["historical"].metrics)},
                      "oos": {"result": r["oos"].result, "metrics": dict(r["oos"].metrics)}, "verdict": v.as_dict()}, indent=2, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
