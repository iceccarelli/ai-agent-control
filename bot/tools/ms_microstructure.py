"""ms_microstructure: a collector-backed, VERIFIED, decision-time-eligible view of recorded order books and trade tapes (offline; no network, no clock, no credentials).

Input: a ``market_collector`` output root (``cycle-<ms>/books/<S>.json``, ``trades/<S>.json``, ``manifest.json``, ``COMPLETE``, ``cycles.jsonl``). Nothing is repaired, filled or
interpolated: a snapshot that fails any check carries ``rejections`` (reason codes) and ``snapshot_at`` returns ``None`` for it, with the reason from ``explain_at``.

CONTRACT
    ``load_cycles(root, *, symbols=None, verify=True, params=None) -> MicroDataset``
    ``MicroDataset.snapshot_at(symbol, decision_ms, *, max_age_ms=None) -> Optional[MicroSnapshot]``   (``explain_at`` returns ``(snapshot_or_None, reasons)``)
    ``MicroDataset.tape_between(symbol, t0_ms, t1_ms) -> TapeSlice``  (trades with t0 < ts <= t1, ``complete`` only when the recorded tapes PROVE the interval is covered)

DECISION-TIME ELIGIBILITY. A snapshot is eligible at ``decision_ms`` only if ``local_receive_ms <= decision_ms`` (the collector's own receive clock) and
``decision_ms - local_receive_ms <= max_age_ms``. The LATEST received snapshot is the only candidate: if it is invalid or too old the answer is None (an older valid one is not
substituted silently, a later one is never attached). The symbol is matched exactly.

PER-SNAPSHOT VALIDATION (reason codes in ``rejections``): cycle verified (every file hash re-computed against the manifest, COMPLETE marker = manifest sha, cycles.jsonl hash chain
and its manifest sha agree, cycle recorded exactly once and in order); file symbol == artifact symbol == payload symbol; no duplicate (kind, symbol) artifact in a cycle; retCode 0;
both sides non-empty, finite positive price/qty, bids strictly descending, asks strictly ascending, best bid < best ask strictly; exchange ``ts`` present and strictly increasing
across the symbol's cycles; ``cts`` not after ``ts``; receive clock consistent with the exchange clock (``-max_future_skew_ms <= recv - ts <= max_exchange_lag_ms``) and inside the
cycle's [started, finished] window; ``u``/``seq`` never regress or reset (DISCONTINUOUS; REST snapshots 60 s apart cannot prove +1 continuity, so a step bound is available only
through ``MicroParams.max_u_gap``, default off); the collector's own validation of the artifact is ok and not stale.

TAPE. ``trades/<S>.json`` is the venue's ``recent-trade`` list (newest first, at most ``limit`` rows). Each file proves coverage of ``(oldest_trade_ts, trades_receive_ms]`` ONLY:
older trades fell off the list. A fill interval not inside the union of those intervals is a TAPE GAP (unknown, never "no trades").

COHORTS. The collector's order-book / trade capture is a NARROW L2 cohort (``narrow_l2``); the broad history cohort (full universe, funding/mark: ``market_history``) is a different
dataset (``COHORT_BROAD_HISTORY``). ``cohort_v2`` scores |funding| among its inputs, so ``evaluates_funding_edge_ok`` is False for any dataset containing a cohort_v2 (or unknown)
selection. True means only that the selection did not use funding, NOT that the sample is representative.
"""
from __future__ import annotations

import bisect
import hashlib
import json
import math
import os
import sys
from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

BOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (BOT, os.path.join(BOT, "tools")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import evidence_chain as EC  # noqa: E402
import market_collector as MC  # noqa: E402
import ms_types as T  # noqa: E402

COHORT_BROAD_HISTORY = "broad_history"      # full universe, funding / mark history (market_history); NOT what this module loads
COHORT_NARROW_L2 = "narrow_l2"              # market_collector order-book + trade capture cohort
FUNDING_INDEPENDENT_RULES = (MC.SELECTION_RULE,)      # selection rules that do not read funding. cohort_v2 scores funding: never in this tuple.

PROV_MEASURED = "MEASURED_BOOK_TAPE"
PROV_MODELLED = "MODELLED"
PROV_REJECTED = "REJECTED"

Trade4 = Tuple[int, float, float, str]               # (ts_ms, price, qty, taker side "Buy"|"Sell")
Trade5 = Tuple[int, float, float, str, str]          # + execId (internal, for de-duplication across files)


@dataclass(frozen=True)
class MicroParams:
    max_age_ms: int = 5_000                           # decision-time eligibility: the book may be at most this old at the decision
    max_future_skew_ms: int = 2_000                   # exchange ts may be at most this far AFTER the local receive time
    max_exchange_lag_ms: int = MC.MAX_BOOK_AGE_MS     # ...and at most this far before it
    cts_tolerance_ms: int = 1_000                     # cts may exceed ts by at most this
    max_u_gap: Optional[int] = None                   # None = no step bound (only regressions / resets are discontinuities)

    def __post_init__(self) -> None:
        if min(self.max_age_ms, self.max_future_skew_ms, self.max_exchange_lag_ms, self.cts_tolerance_ms) < 0 or (self.max_u_gap is not None and self.max_u_gap < 0):
            raise ValueError("micro params must be non-negative")


@dataclass(frozen=True)
class MicroSnapshot:
    symbol: str
    cycle: str
    cycle_ms: int
    bids: Tuple[T.BookLevel, ...]
    asks: Tuple[T.BookLevel, ...]
    ts: Optional[int]                                 # exchange generation time (ms)
    cts: Optional[int]                                # matching-engine time (ms)
    u: Optional[int]
    seq: Optional[int]
    local_receive_ms: int                             # the collector's own receive clock for the book
    book_age_ms: Optional[int]                        # decision_ms - local_receive_ms; None until returned by snapshot_at
    spread_bps: Optional[float]
    mid: Optional[float]
    depth_usd_bid: float
    depth_usd_ask: float
    trades: Tuple[Trade4, ...]                        # trades with previous_snapshot.ts < ts <= this.ts (exchange clock), ascending
    trades_window_from_ts: Optional[int]              # previous snapshot's exchange ts (None = no previous snapshot)
    tape_complete: bool                               # the tape file proves coverage of the whole window above
    status: Tuple[str, ...] = ()                      # non-fatal flags: NO_TAPE, TAPE_INVALID, NO_PREVIOUS_SNAPSHOT, TAPE_GAP_BEFORE
    rejections: Tuple[str, ...] = ()                  # fatal reason codes; non-empty => never eligible
    tape_rows: Tuple[Trade5, ...] = ()                # the whole recorded tape file, ascending (internal; use ``trades`` or ``tape_between``)
    tape_recv_ms: Optional[int] = None
    tape_ok: bool = False
    cycle_ok: bool = False                            # the cycle's hashes / marker / chain verified (or verify=False was asked for)

    @property
    def valid(self) -> bool:
        return not self.rejections


@dataclass(frozen=True)
class CohortInfo:
    kind: str                                         # COHORT_NARROW_L2
    cohort_id: str
    rules: Tuple[str, ...]
    symbols: Tuple[str, ...]
    funding_independent: bool
    per_cycle: Tuple[Tuple[str, str, str], ...]       # (cycle, rule, inputs_sha256 or "")


@dataclass(frozen=True)
class CycleRecord:
    name: str
    cycle_ms: int
    manifest_sha256: Optional[str]
    status: str
    problems: Tuple[str, ...]
    symbols: Tuple[str, ...]
    gaps: Tuple[Any, ...] = ()


@dataclass(frozen=True)
class TapeSlice:
    trades: Tuple[Trade4, ...]
    complete: bool
    reasons: Tuple[str, ...]
    covered_to_ms: Optional[int]


class MicroDataset:
    def __init__(self, root: str, params: MicroParams, verified: bool, cohort: CohortInfo, cycles: Tuple[CycleRecord, ...], problems: Tuple[str, ...],
                 by_symbol: Mapping[str, Sequence[MicroSnapshot]]) -> None:
        self.root = root
        self.params = params
        self.verified = verified
        self.cohort = cohort
        self.cycles = cycles
        self.problems = problems
        self._by = {s: tuple(v) for s, v in by_symbol.items()}
        self._recv = {s: [x.local_receive_ms for x in v] for s, v in self._by.items()}

    @property
    def cohort_id(self) -> str:
        return self.cohort.cohort_id

    def symbols(self) -> Tuple[str, ...]:
        return tuple(sorted(self._by))

    def snapshots(self, symbol: str) -> Tuple[MicroSnapshot, ...]:
        """Every recorded snapshot of ``symbol`` (valid or not), ordered by local receive time. For audit; use ``snapshot_at`` for anything decision-time."""
        return self._by.get(symbol, ())

    def data_digest(self) -> str:
        h = hashlib.sha256(b"ms_microstructure/v1")
        for c in self.cycles:
            h.update(f"\n{c.name}\t{c.manifest_sha256}\t{'|'.join(c.problems)}".encode())
        h.update(b"\n" + self.cohort.cohort_id.encode())
        return h.hexdigest()

    def explain_at(self, symbol: str, decision_ms: int, *, max_age_ms: Optional[int] = None) -> Tuple[Optional[MicroSnapshot], Tuple[str, ...]]:
        """(snapshot, ()) when eligible, else (None, reasons). Look-ahead is impossible: only ``local_receive_ms <= decision_ms`` is considered."""
        age_cap = self.params.max_age_ms if max_age_ms is None else int(max_age_ms)
        if isinstance(decision_ms, bool) or not isinstance(decision_ms, int) or decision_ms <= 0:
            return None, ("BAD_DECISION_TIME",)
        snaps = self._by.get(symbol, ())
        recv = self._recv.get(symbol, [])
        i = bisect.bisect_right(recv, decision_ms)
        reasons: List[str] = []
        latest = snaps[i - 1] if i > 0 else None
        # a cycle that exists but could not be turned into a snapshot (missing manifest, tampered, incomplete) and is newer than the latest snapshot: say so
        newer_bad = [c for c in self.cycles if c.problems and not c.symbols and c.cycle_ms <= decision_ms and decision_ms - c.cycle_ms <= age_cap
                     and (latest is None or c.cycle_ms >= latest.cycle_ms)]
        if latest is None:
            reasons.append("NO_SNAPSHOT_AT_OR_BEFORE_DECISION")
            if i < len(snaps):
                reasons.append("FUTURE_SNAPSHOT_NOT_USED")
        else:
            age = decision_ms - latest.local_receive_ms
            if age > age_cap:
                reasons.append("STALE_SNAPSHOT")
            reasons.extend(latest.rejections)
        for c in newer_bad:
            reasons.extend(f"CYCLE_{c.name}:{p}" for p in c.problems)
        if reasons or latest is None:
            return None, tuple(dict.fromkeys(reasons))
        return replace(latest, book_age_ms=decision_ms - latest.local_receive_ms), ()

    def snapshot_at(self, symbol: str, decision_ms: int, *, max_age_ms: Optional[int] = None) -> Optional[MicroSnapshot]:
        return self.explain_at(symbol, decision_ms, max_age_ms=max_age_ms)[0]

    def next_valid_after(self, symbol: str, ts_ms: int) -> Optional[MicroSnapshot]:
        """First VALID snapshot whose exchange ts is after ``ts_ms`` (for ex-post markout measurement only; never a decision input)."""
        for s in self._by.get(symbol, ()):
            if s.valid and s.ts is not None and s.ts > ts_ms:
                return s
        return None

    def valid_snapshots_between(self, symbol: str, lo_ms: int, hi_ms: int) -> Tuple[MicroSnapshot, ...]:
        """Valid snapshots whose exchange ts is in (lo_ms, hi_ms]."""
        return tuple(s for s in self._by.get(symbol, ()) if s.valid and s.ts is not None and lo_ms < s.ts <= hi_ms)

    def tape_between(self, symbol: str, t0_ms: int, t1_ms: int) -> TapeSlice:
        """Trades with ``t0 < ts <= t1`` (exchange clock). ``complete`` only if the union of the recorded files' proven intervals ``(oldest_ts, receive_ms]`` covers
        ``(t0, t1]``; otherwise the reasons say TAPE_GAP and what is missing. Identical execIds in overlapping files are one trade; conflicting ones make the tape unusable."""
        if t1_ms <= t0_ms:
            return TapeSlice((), True, (), t1_ms)
        files = [s for s in self._by.get(symbol, ()) if s.cycle_ok and s.tape_ok and s.tape_rows and s.tape_recv_ms is not None]
        ivs = sorted(((s.tape_rows[0][0], s.tape_recv_ms, s) for s in files), key=lambda x: (x[0], x[1]))
        cur = t0_ms
        for a, b, _s in ivs:
            if a <= cur and b > cur:
                cur = b
            if cur >= t1_ms:
                break
        reasons: List[str] = []
        covered = cur >= t1_ms
        if not covered:
            reasons.append("TAPE_GAP")
            reasons.append(f"TAPE_COVERED_TO_{cur}_NEEDED_{t1_ms}")
        seen: Dict[str, Trade5] = {}
        conflict = False
        for s in files:
            for r in s.tape_rows:
                if t0_ms < r[0] <= t1_ms:
                    old = seen.get(r[4])
                    if old is None:
                        seen[r[4]] = r
                    elif old[:4] != r[:4]:
                        conflict = True
        if conflict:
            reasons.append("TAPE_CONFLICT")
        out = tuple((r[0], r[1], r[2], r[3]) for r in sorted(seen.values(), key=lambda r: (r[0], r[4])))
        return TapeSlice(out, covered and not conflict, tuple(reasons), min(cur, t1_ms))


# ------------------------------------------------------------------ cohort

def evaluates_funding_edge_ok(ds: MicroDataset) -> bool:
    """False when any cycle's order-book/trade cohort was selected with a rule that reads funding (cohort_v2) or an unknown rule, or when the dataset is empty. A funding edge
    must be evaluated on the BROAD history cohort (``COHORT_BROAD_HISTORY``), never on this narrow, funding-scored one."""
    return bool(ds.cohort.funding_independent and ds.cohort.per_cycle)


def _cohort_of(per_cycle: List[Tuple[str, str, str]], symbols: List[str]) -> CohortInfo:
    rules = tuple(sorted({r for _c, r, _h in per_cycle}))
    indep = bool(per_cycle) and all(r in FUNDING_INDEPENDENT_RULES for r in rules)
    cid = hashlib.sha256(json.dumps([COHORT_NARROW_L2, per_cycle], sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:24]
    return CohortInfo(COHORT_NARROW_L2, f"{COHORT_NARROW_L2}:{cid}", rules, tuple(sorted(set(symbols))), indep, tuple(per_cycle))


# ------------------------------------------------------------------ parsing

def _num(v: Any) -> Optional[Decimal]:
    if isinstance(v, bool) or not isinstance(v, (str, int)):
        return None
    try:
        d = Decimal(v) if isinstance(v, str) else Decimal(int(v))
    except (InvalidOperation, ValueError):
        return None
    return d if d.is_finite() and d > 0 and math.isfinite(float(d)) else None


def _uint(v: Any) -> Optional[int]:
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v if v >= 0 else None
    if isinstance(v, str) and v.isascii() and v.isdigit() and len(v) <= 19:
        return int(v)
    return None


def _read(path: str) -> Optional[bytes]:
    try:
        if os.path.islink(path) or os.path.getsize(path) > MC.MAX_BODY_BYTES:
            return None
        with open(path, "rb") as fh:
            return fh.read()
    except OSError:
        return None


def _doc(body: Optional[bytes]) -> Optional[Dict[str, Any]]:
    if body is None:
        return None
    try:
        d = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, RecursionError):
        return None
    return d if isinstance(d, dict) else None


def _ladder(raw: Any, bids: bool, rej: List[str]) -> Tuple[T.BookLevel, ...]:
    name = "BIDS" if bids else "ASKS"
    if not isinstance(raw, list) or not raw:
        rej.append(f"EMPTY_OR_BAD_{name}")
        return ()
    out: List[T.BookLevel] = []
    for lv in raw:
        if not (isinstance(lv, list) and len(lv) >= 2):
            rej.append(f"BAD_LEVEL_{name}")
            return ()
        p, q = _num(lv[0]), _num(lv[1])
        if p is None or q is None:
            rej.append(f"BAD_LEVEL_{name}")
            return ()
        out.append(T.BookLevel(float(p), float(q)))
    px = [x.price for x in out]
    if any((a <= b) if bids else (a >= b) for a, b in zip(px, px[1:])):
        rej.append(f"NON_MONOTONE_{name}")
    return tuple(out)


def _tape(body: Optional[bytes], symbol: str, recv_ms: Optional[int], p: MicroParams) -> Tuple[Tuple[Trade5, ...], bool, List[str]]:
    """(rows ascending, ok, reasons). Any bad row makes the whole file unusable (never repaired); identical duplicate execIds collapse."""
    d = _doc(body)
    if d is None or d.get("retCode") != 0 or not isinstance(d.get("result"), dict) or not isinstance(d["result"].get("list"), list):
        return (), False, ["TAPE_UNPARSEABLE"]
    seen: Dict[str, Trade5] = {}
    rows = d["result"]["list"]
    for r in reversed(rows):                                   # the venue lists newest first
        if not isinstance(r, dict) or r.get("symbol") != symbol:
            return (), False, ["TAPE_SYMBOL_MISMATCH"]
        eid, px, qty, side, ts = r.get("execId"), _num(r.get("price")), _num(r.get("size")), r.get("side"), _uint(r.get("time"))
        if not isinstance(eid, str) or not eid or px is None or qty is None or side not in ("Buy", "Sell") or not ts:
            return (), False, ["TAPE_BAD_ROW"]
        if recv_ms is not None and ts > recv_ms + p.max_future_skew_ms:
            return (), False, ["TAPE_TRADE_IN_THE_FUTURE"]
        row: Trade5 = (ts, float(px), float(qty), side, eid)
        if eid in seen and seen[eid] != row:
            return (), False, ["TAPE_CONFLICTING_DUPLICATE"]
        seen[eid] = row
    return tuple(sorted(seen.values(), key=lambda r: r[0])), True, []


def _cycle_problem_codes(path: str, verify: bool) -> List[str]:
    out: List[str] = []
    if verify:
        for pr in MC.verify_cycle_dir(path):
            if "manifest.json is missing" in pr:
                out.append("MANIFEST_MISSING")
            elif "COMPLETE marker is missing" in pr:
                out.append("COMPLETE_MISSING")
            elif pr.startswith(("ALTERED", "MISSING", "TRUNCATED", "SYMLINK")):
                out.append(f"HASH_MISMATCH:{pr[:80]}")
            else:
                out.append(f"MANIFEST_INVALID:{pr[:80]}")
    return out


def load_cycles(root: str, *, symbols: Optional[Sequence[str]] = None, verify: bool = True, params: Optional[MicroParams] = None) -> MicroDataset:
    """Load every collector cycle under ``root``. ``verify=True`` (default) re-computes every file hash, checks the COMPLETE marker and the cycles.jsonl hash chain; with
    ``verify=False`` those are skipped (structure and per-snapshot checks still run) and every fill derived from the dataset is MODELLED, never MEASURED_BOOK_TAPE.
    Never raises on hostile data; raises ValueError only if ``root`` is not a directory."""
    p = params or MicroParams()
    if not os.path.isdir(root):
        raise ValueError("root is not a directory")
    want = None if symbols is None else set(symbols)
    log = os.path.join(root, MC.LOG_NAME)
    ds_problems: List[str] = []
    rows: List[Dict[str, Any]] = []
    chain_ok = True
    if verify:
        if not os.path.isfile(log):
            ds_problems.append("LOG_MISSING")
            chain_ok = False
        else:
            try:
                ver = EC.verify(log)
                if not ver.get("ok", False):
                    chain_ok = False
                    ds_problems.append("LOG_CHAIN_BROKEN:" + ",".join(sorted({str(i.get("code")) for i in ver.get("issues", []) if isinstance(i, dict)})))
            except (OSError, ValueError, KeyError, TypeError):
                chain_ok = False
                ds_problems.append("LOG_CHAIN_BROKEN:unreadable")
    if os.path.isfile(log):
        try:
            rows = [r for r in EC.rows_of(EC.read_lines(log)[0]) if isinstance(r, dict) and MC._safe_cycle_name(r.get("cycle"))]
        except (OSError, ValueError, TypeError, KeyError):
            rows = []
    row_by: Dict[str, List[Dict[str, Any]]] = {}
    for r in rows:
        row_by.setdefault(r["cycle"], []).append(r)
    max_ms = 0
    out_of_order: set = set()
    for r in rows:
        ms = r.get("cycle_ms")
        if isinstance(ms, int) and not isinstance(ms, bool):
            if ms <= max_ms:
                out_of_order.add(r["cycle"])
            max_ms = max(max_ms, ms)

    names = MC.cycle_dirs(root, complete_only=False)
    names.sort(key=lambda n: int(n[len("cycle-"):]))
    if verify:
        for n in sorted(set(row_by) - set(names)):
            ds_problems.append(f"MISSING_CYCLE_DIR:{n}")

    records: List[CycleRecord] = []
    raw: Dict[str, List[Dict[str, Any]]] = {}               # per symbol, in cycle order: parsed per-cycle pieces
    per_cycle_cohort: List[Tuple[str, str, str]] = []
    cohort_syms: List[str] = []
    for name in names:
        path = os.path.join(root, name)
        ms_name = int(name[len("cycle-"):])
        probs = _cycle_problem_codes(path, verify)
        mpath = os.path.join(path, "manifest.json")
        man = _doc(_read(mpath))
        msha = MC._file_sha(mpath) if os.path.isfile(mpath) and not os.path.islink(mpath) else None
        if man is None:
            probs.append("MANIFEST_MISSING" if msha is None else "MANIFEST_INVALID:unparseable")
        else:
            try:
                shape = MC.manifest_shape_problems(man, name)
            except (TypeError, ValueError, KeyError, AttributeError):
                shape = ["shape"]
            if shape:
                probs.append("MANIFEST_INVALID:" + shape[0][:80])
                man = None
        if verify:
            rs = row_by.get(name, [])
            if not chain_ok:
                probs.append("CHAIN_UNVERIFIED")
            if not rs:
                probs.append("UNRECORDED_CYCLE")
            elif len(rs) > 1:
                probs.append("DUPLICATE_CYCLE_ROW")
            elif msha is not None and rs[0].get("manifest_sha256") != msha:
                probs.append("CHAIN_MANIFEST_SHA_MISMATCH")
            if name in out_of_order:
                probs.append("OUT_OF_ORDER_CYCLE")
        probs = list(dict.fromkeys(probs))
        if man is None:
            records.append(CycleRecord(name, ms_name, msha, "UNKNOWN", tuple(probs), ()))
            continue
        arts = [a for a in man["artifacts"] if isinstance(a, dict)]
        syms_here: List[str] = []
        sel = man.get("selection") if isinstance(man.get("selection"), dict) else {}
        per_cycle_cohort.append((name, str(sel.get("rule")), str(sel.get("inputs_sha256") or "")))
        for it in sel.get("selected", []) if isinstance(sel.get("selected"), list) else []:
            if isinstance(it, dict) and isinstance(it.get("symbol"), str):
                cohort_syms.append(it["symbol"])
        counts: Dict[Tuple[str, str], int] = {}
        for a in arts:
            if a.get("kind") in ("book", "trades") and isinstance(a.get("symbol"), str):
                counts[(a["kind"], a["symbol"])] = counts.get((a["kind"], a["symbol"]), 0) + 1
        for a in arts:
            if a.get("kind") != "book" or not isinstance(a.get("symbol"), str) or a.get("file") is None:
                continue
            sym = a["symbol"]
            if want is not None and sym not in want:
                continue
            syms_here.append(sym)
            rej: List[str] = [f"CYCLE:{c}" for c in probs]
            if counts[("book", sym)] > 1 or counts.get(("trades", sym), 0) > 1:
                rej.append("DUPLICATE_SYMBOL")
            fpath = a.get("file")
            expect = f"books/{sym}.json"
            if fpath != expect:
                rej.append("SYMBOL_FILE_MISMATCH")
            bdoc = None if any(c.startswith("HASH") for c in probs) else _doc(_read(os.path.join(path, *expect.split("/"))))
            piece = _parse_book(bdoc, sym, a, man, p, rej)
            tr = next((t for t in arts if t.get("kind") == "trades" and t.get("symbol") == sym and t.get("file") == f"trades/{sym}.json"), None)
            tape_rows: Tuple[Trade5, ...] = ()
            tape_ok, tape_recv, tflags = False, None, []
            if tr is None:
                tflags.append("NO_TAPE")
            elif counts.get(("trades", sym), 0) == 1 and not any(c.startswith("HASH") for c in probs):
                tape_recv = _uint(tr.get("recv_ms"))
                tape_rows, tape_ok, why = _tape(_read(os.path.join(path, "trades", f"{sym}.json")), sym, tape_recv, p)
                if not tape_ok:
                    tflags.append("TAPE_INVALID")
                tflags.extend(why)
            piece.update({"cycle": name, "cycle_ms": man["cycle_ms"], "rej": rej, "tape_rows": tape_rows, "tape_ok": tape_ok, "tape_recv": tape_recv,
                          "tflags": [f for f in tflags if f], "cycle_ok": not probs})
            raw.setdefault(sym, []).append(piece)
        records.append(CycleRecord(name, ms_name, msha, str(man.get("status")), tuple(probs), tuple(sorted(set(syms_here))), tuple(man.get("gaps", ()))))

    by_symbol: Dict[str, List[MicroSnapshot]] = {}
    for sym, pieces in raw.items():
        pieces.sort(key=lambda x: x["cycle_ms"])
        prev_ts = prev_u = prev_seq = prev_recv = None
        prev_good_ts: Optional[int] = None
        built: List[MicroSnapshot] = []
        for pc in pieces:
            rej = pc["rej"]
            ts, u, seq, recv = pc["ts"], pc["u"], pc["seq"], pc["recv"]
            if pc["parsed"]:
                if prev_recv is not None and recv < prev_recv:
                    rej.append("OUT_OF_ORDER_CYCLE")
                if prev_ts is not None and ts is not None:
                    if ts < prev_ts:
                        rej.append("NON_MONOTONE_TS")
                    elif ts == prev_ts:
                        rej.append("DUPLICATE_BOOK_TS")
                if prev_u is not None and u is not None and (u < prev_u or (p.max_u_gap is not None and u - prev_u > p.max_u_gap)):
                    rej.append("DISCONTINUOUS")
                if prev_seq is not None and seq is not None and seq < prev_seq:
                    rej.append("DISCONTINUOUS")
            window_from = prev_good_ts
            win: Tuple[Trade4, ...] = ()
            complete = False
            flags = list(pc["tflags"])
            tape_usable = pc["cycle_ok"] and pc["tape_ok"]
            if window_from is None:
                flags.append("NO_PREVIOUS_SNAPSHOT")
            if tape_usable and ts is not None:
                if window_from is not None:
                    win = tuple((r[0], r[1], r[2], r[3]) for r in pc["tape_rows"] if window_from < r[0] <= ts)
                    complete = bool(pc["tape_rows"]) and pc["tape_rows"][0][0] <= window_from
                    if not complete:
                        flags.append("TAPE_GAP_BEFORE")
            bids, asks = pc["bids"], pc["asks"]
            dbid = sum(x.price * x.qty for x in bids)
            dask = sum(x.price * x.qty for x in asks)
            mid = 0.5 * (bids[0].price + asks[0].price) if bids and asks else None
            sp = (asks[0].price - bids[0].price) / mid * 1e4 if mid else None
            built.append(MicroSnapshot(sym, pc["cycle"], pc["cycle_ms"], bids, asks, ts, pc["cts"], u, seq, recv, None, sp, mid, dbid, dask, win, window_from, complete,
                                       tuple(dict.fromkeys(flags + (["DISCONTINUOUS"] if "DISCONTINUOUS" in rej else []))), tuple(dict.fromkeys(rej)),
                                       pc["tape_rows"] if tape_usable else (), pc["tape_recv"], tape_usable, pc["cycle_ok"]))
            if pc["parsed"]:
                prev_ts, prev_u, prev_seq, prev_recv = ts, u, seq, recv
            if pc["cycle_ok"] and ts is not None:
                prev_good_ts = ts
        built.sort(key=lambda s: (s.local_receive_ms, s.cycle_ms))
        by_symbol[sym] = built

    cohort = _cohort_of(per_cycle_cohort, cohort_syms)
    return MicroDataset(root, p, bool(verify), cohort, tuple(records), tuple(ds_problems), by_symbol)


def _parse_book(doc: Optional[Dict[str, Any]], sym: str, art: Mapping[str, Any], man: Mapping[str, Any], p: MicroParams, rej: List[str]) -> Dict[str, Any]:
    recv = _uint(art.get("recv_ms")) or 0
    blank: Dict[str, Any] = {"bids": (), "asks": (), "ts": None, "cts": None, "u": None, "seq": None, "recv": recv, "parsed": False}
    if recv <= 0:
        rej.append("NO_RECEIVE_TIME")
    if doc is None:
        rej.append("BOOK_UNPARSEABLE")
        return blank
    if doc.get("retCode") != 0 or not isinstance(doc.get("result"), dict):
        rej.append("BOOK_ERROR_RESPONSE")
        return blank
    res = doc["result"]
    if res.get("s") != sym:
        rej.append("SYMBOL_MISMATCH")
    bids = _ladder(res.get("b"), True, rej)
    asks = _ladder(res.get("a"), False, rej)
    if bids and asks and bids[0].price >= asks[0].price:
        rej.append("CROSSED_BOOK")
    ts, cts = _uint(res.get("ts")), _uint(res.get("cts"))
    u, seq = _uint(res.get("u")), _uint(res.get("seq"))
    if not ts:
        rej.append("BAD_TS")
        ts = None
    if res.get("cts") is not None and cts is None:
        rej.append("BAD_CTS")
    if ts is not None:
        if cts is not None and cts > ts + p.cts_tolerance_ms:
            rej.append("CTS_AFTER_TS")
        lag = recv - ts
        if lag > p.max_exchange_lag_ms:
            rej.append("STALE_AT_RECEIVE")
        if lag < -p.max_future_skew_ms:
            rej.append("CLOCK_SKEW_EXCHANGE_AHEAD")
    s0, f0 = _uint(man.get("started_ms")), _uint(man.get("finished_ms"))
    if s0 is not None and f0 is not None and not (s0 <= recv <= f0):
        rej.append("RECEIVE_OUTSIDE_CYCLE_WINDOW")
    val = art.get("validation") if isinstance(art.get("validation"), dict) else {}
    if not val.get("ok", False):
        rej.append("COLLECTOR_VALIDATION_FAILED")
    if val.get("stale"):
        rej.append("COLLECTOR_STALE_FLAG")
    if art.get("skew_flag"):
        rej.append("COLLECTOR_SKEW_FLAG")
    return {"bids": bids, "asks": asks, "ts": ts, "cts": cts, "u": u, "seq": seq, "recv": recv, "parsed": True}
