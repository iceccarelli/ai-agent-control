#!/usr/bin/env python3
"""Reserve the holdout BEFORE the first evaluation read of Bybit history, and make the reserved region unreadable until the one test that may read it.

    python3 tools/history_holdout.py --status  --history-root R [--mark-interval M] [--ledger-dir L]
    python3 tools/history_holdout.py --reserve --history-root R [--mark-interval M] [--ledger-dir L] [--fraction 0.30] [--prior-reads-through 2026-10-30T00:00:00Z]

WHY THIS EXISTS
===============
``tools/reserved_holdout.py`` records reads of the old Binance daily corpus (slice 57 declared an "out-of-sample" window that a full-sample run had already read, and its ``--certify``
exists to catch that). The cross-sectional study reads a DIFFERENT store: a ``market_history`` root through ``candidate_readiness.load_history_root``. That ledger never saw it, so
``--certify`` was vacuous for it, and nothing stopped an evaluator from walking the whole window, tuning on what it saw, and then calling the last part "out of sample". The data this
study needs is the owner's, weeks of collection and a REST backfill: spent on a hypothesis chosen after looking at its own test window it proves nothing and cannot be collected twice.

THE MODEL (what an independent review of the first version forced)
==================================================================
The registry is keyed by VENUE and TIME, not by a directory, a ledger path, a mark interval or a data root. A holdout is a promise about a region of calendar time on one venue, and
the same region reached through a different root, a different ``--ledger-dir`` or a different mark dataset is the same region.

* ONE registry per user (``~/.ai-agent-control/history_holdout.jsonl``, or ``$HISTORY_HOLDOUT_LEDGER``): an append-only, hash-chained file (``evidence_chain``). The research tools'
  ``--ledger-dir`` no longer decides where the holdout lives, so a forgotten or different ledger directory cannot give an unreserved read a clean slate.
* ``venue`` = ``<host>|<category>`` from the funding manifest (``api.bybit.com|linear``). ``--reserve`` appends one row: ``from_ms`` = the UTC-day boundary at 70% of the funding window of
  the root it was run on, never moved afterwards (the recorder keeps recording; a recomputed boundary would drift later and hand the explorer yesterday's holdout).
* THE BURN HORIZON: every RECORDED read of the venue's history that was not cut at a reservation (an ``--unreserved-exploratory`` run, a spent final read, an owner statement of earlier
  reads) burns the time it covered. A reservation is refused unless its boundary is at or after the horizon, so "the history was read before it was reserved" is a statement about TIME and
  holds however the data was reached; the message names the horizon and how long the window must grow before a holdout exists again.
* every EVALUATION load goes through ``load`` and is CUT at the boundary inside the loader (``load_history_root(cut_ms=...)``): the reserved rows are not ignored, they are never built.
  No reservation, no read: the refusal names the registry it looked in and the command to run. ``holdout_ledger`` is a REQUIRED argument of every evaluator; the only way to read the
  whole window without a registry is the explicit sentinel ``UNGUARDED`` (tests, embeddings), which the result stamps NOT_GUARDED and a test forbids in shipped code.
* the reserved region is readable exactly ONCE, through ``final_read``, which loads (a dataset that does not verify costs nothing), records the read under a lock, and only then returns
  data. After it the region is SPENT and burned. No CLI flag opens it: the frozen-hypothesis evaluator that may call ``final_read`` is not built, and a human typing a flag is the failure
  this exists to prevent.
* reads made BEFORE this registry existed leave no row, but the research ledgers beside the data (``cross_sectional_funding*``, ``funding_regime_conditioned_entry*``,
  ``ms_pipeline_trials``) show that evaluations ran: ``--reserve`` refuses (PRIOR_EVALUATION_DETECTED) until the owner states, on the record, the instant the earlier reads reached
  (``--prior-reads-through``), which then burns that horizon like any recorded read.

WHAT IT DOES NOT DO
===================
It cannot see a read made outside these tools (a notebook, a hand-written script, a metadata report): like ``reserved_holdout`` it is a FLOOR, not a census, and it says so. Facts the
manifests compute over the WHOLE dataset (a symbol's non-COMPLETE status, a conflict, an interval disagreement) are not cut and can flag or exclude a symbol in the explore universe;
they carry no return information. It detects an edited or torn registry; a ROLLBACK to an older consistent state is visible only by comparing the ``chain_head`` it stamps on every
result with the one you saw before. Venue identity comes from the manifest and is as trustworthy as the manifest (self-consistent bytes, not authenticated as produced by the exchange).
It does not score anything, writes no field a gate reads, and has no path to ``live_authorized``. It makes no passing exploration real: a candidate that survives the explore window
still needs the single final read, a frozen hypothesis, prospective evidence and the owner's attestation, and a reserved region of a few dozen rows can REJECT but cannot support more
than INSUFFICIENT_DATA (``status`` prints the arithmetic).
"""
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import fcntl
import json
import os
import sys
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple

BOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (BOT, os.path.join(BOT, "tools")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import candidate_readiness as CR  # noqa: E402
import evidence_chain as EC  # noqa: E402
import market_history as MH  # noqa: E402
import research_harness as RH  # noqa: E402

SCHEMA = "history_holdout/2"
REGISTRY_ENV = "HISTORY_HOLDOUT_LEDGER"
REGISTRY_DEFAULT = os.path.join("~", ".ai-agent-control", "history_holdout.jsonl")
DAY = 86_400_000
RESERVED_FRACTION = 0.30
MIN_FRACTION = 0.20            # below this the reserved region is a token: a holdout of a few days tests nothing
MAX_FRACTION = 0.50
EXPLORE, FINAL, UNRESERVED, PRIOR_STATED = "EXPLORE", "FINAL", "UNRESERVED_EXPLORE", "PRIOR_STATED"
NOT_A_HOLDOUT = "NOT_A_HOLDOUT_STUDY: every number below was computed with the whole window in view; it is exploratory and must never be quoted as out-of-sample"
FLOOR_NOTE = "A FLOOR, not a census: a read made outside these tools (a notebook, a hand-written script, a metadata report) is invisible to this registry"
#: research ledgers whose presence beside the data proves an evaluation of a history root ran (BTC corpus ledgers such as cash_and_carry* never read a history root)
PRIOR_LEDGER_PREFIXES = ("cross_sectional_funding", "funding_regime_conditioned_entry", "ms_pipeline_trials")


class _Unguarded:
    """The ONE explicit way to read a history root without a registry (tests, embeddings). Never accepted from shipped code: a test greps for it."""

    def __repr__(self) -> str:
        return "history_holdout.UNGUARDED"


UNGUARDED = _Unguarded()


# ------------------------------------------------------------------ pure helpers
def _iso(ms: int) -> str:
    return dt.datetime.fromtimestamp(int(ms) / 1000.0, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _check_fraction(frac: Any) -> None:
    if isinstance(frac, bool) or not isinstance(frac, (int, float)) or not (MIN_FRACTION <= float(frac) <= MAX_FRACTION):
        raise RH.HarnessError(f"the reserved fraction must be a number in [{MIN_FRACTION}, {MAX_FRACTION}], got {frac!r} (a holdout below {MIN_FRACTION:.0%} of the window is a token; above "
                              f"{MAX_FRACTION:.0%} leaves too little to explore)")


def cut_for(a_ms: int, b_ms: int, frac: float = RESERVED_FRACTION) -> int:
    """The UTC-day boundary at ``1 - frac`` of the window [a, b]: everything at or after it is reserved."""
    _check_fraction(frac)
    cut = (int(a_ms) + int((1.0 - float(frac)) * (int(b_ms) - int(a_ms)))) // DAY * DAY
    if cut <= int(a_ms):
        raise RH.HarnessError(f"WINDOW_TOO_SHORT: the window {_iso(a_ms)}..{_iso(b_ms)} leaves no whole UTC day to explore before a {frac:.0%} reservation")
    return cut


def _registry_setting() -> Optional[str]:
    """THE one read of the environment in this module: the registry path override (a non-secret file path)."""
    return os.environ.get(REGISTRY_ENV) or None


def default_registry_path() -> str:
    """The per-user registry: ``$HISTORY_HOLDOUT_LEDGER`` or ``~/.ai-agent-control/history_holdout.jsonl`` (absolute). One place, whatever the data root or ``--ledger-dir``."""
    return os.path.abspath(os.path.expanduser(_registry_setting() or REGISTRY_DEFAULT))


def registry_source(registry: Optional[str] = None) -> Dict[str, Any]:
    """Where the registry path came from. Another registry file is a clean slate for every guarantee here (it is the floor, not a census), so the override is SAID on every stamp rather than left
    for a reader to infer: a result produced against a registry chosen by the environment (``REGISTRY_OVERRIDDEN_BY_ENV``) or by an argument other than the default path
    (``REGISTRY_OVERRIDDEN_BY_ARGUMENT``: the CLI's ``--registry``) carries the warning."""
    if _registry_setting():
        return {"registry_source": "ENV:" + REGISTRY_ENV,
                "registry_warning": "REGISTRY_OVERRIDDEN_BY_ENV: this run used a registry file chosen by the environment; another file is a clean slate for the burn horizon and the one-time final read"}
    if registry is not None and os.path.abspath(str(registry)) != default_registry_path():
        return {"registry_source": "ARGUMENT",
                "registry_warning": "REGISTRY_OVERRIDDEN_BY_ARGUMENT: this run used a registry file other than the per-user default; another file is a clean slate for the burn horizon and the one-time final read"}
    return {"registry_source": "DEFAULT"}


def _window(root: str) -> Tuple[int, int]:
    import backfill_plan as BP
    w = BP.window_of_funding_manifest(os.path.abspath(root))
    if w is None:
        raise RH.HarnessError("the funding dataset records no requested window (manifest request.start_ms/end_ms): there is nothing to reserve")
    return w


def served_window(root: str) -> Optional[Tuple[int, int]]:
    """``(first, last)`` timestamp the funding dataset's symbols actually SERVED (the manifest's per-symbol ``coverage``), or None when the manifest states none. The REQUESTED window
    (``request.start_ms/end_ms``) says what a collection asked for; this says what exists. A STALE or PARTIAL dataset verifies, so the two can differ by months (the independent re-review's
    case: funding ending 120 days before the request's end still reserved 'the last 30%' and stamped it RESERVED with 0 rows withheld)."""
    try:
        with open(os.path.join(root, MH.FUNDING, "manifest.json"), "rb") as fh:
            syms = (json.loads(fh.read()).get("symbols") or {})
    except (OSError, ValueError, AttributeError):
        return None
    firsts: List[int] = []
    lasts: List[int] = []
    for r in (syms.values() if isinstance(syms, dict) else ()):
        cov = r.get("coverage") if isinstance(r, dict) and isinstance(r.get("coverage"), dict) else {}
        f, l = cov.get("served_first_ts"), cov.get("served_last_ts")
        if isinstance(f, int) and isinstance(l, int) and not isinstance(f, bool) and not isinstance(l, bool):
            firsts.append(f)
            lasts.append(l)
    return (min(firsts), max(lasts)) if firsts else None


def effective_window(root: str, a: int, b: int) -> Tuple[int, int]:
    """The requested window clipped to what the datasets actually served (the requested window itself when the manifest states no coverage)."""
    sw = served_window(root)
    return (a, b) if sw is None else (max(int(a), sw[0]), min(int(b), sw[1]))


CATEGORIES = ("linear", "spot", "inverse", "option")


MAX_END_SHORTFALL_MS = 2 * 86_400_000            # the funding data may end this long before the requested end (the last print precedes the request's end by up to one funding interval)


def reservation_problem(root: str, a: int, b: int, cut: int) -> Tuple[Optional[str], Tuple[int, int]]:
    """``(problem, effective_window)``: why a boundary ``cut`` over the REQUESTED window ``[a, b]`` cannot be a holdout of the data these datasets actually SERVED (None when it can). A dataset with
    no served coverage, one that ends days before its request, a boundary at or before the first served row, and a boundary with under a day of data after it are each refused with their own code
    (independent re-review: the arithmetic and the RESERVED stamp used to come from the request, so a stale dataset 'reserved' months that did not exist, and a request that opened two years
    before the data put the boundary before the first row, reserving the whole dataset)."""
    sw = served_window(root)
    if sw is None:
        return ("NO_SERVED_COVERAGE: the funding manifest records no served coverage for any symbol (a dataset with no rows): there is nothing to reserve", (a, b))
    ea, eb = max(int(a), sw[0]), min(int(b), sw[1])
    if int(b) - eb > MAX_END_SHORTFALL_MS:
        return (f"DATA_ENDS_BEFORE_THE_REQUESTED_END: the funding data these datasets actually served ends {_iso(eb)} but the request asks for {_iso(b)} ({(int(b) - eb) / DAY:.1f} days short). A STALE or "
                f"PARTIAL dataset verifies, but a boundary fixed over data that has not arrived is a prospective holdout, not a reserved one: finish the collection, then reserve", (ea, eb))
    if cut - ea < DAY:
        return (f"NO_EXPLORE_DATA_BEFORE_THE_BOUNDARY: the boundary {_iso(cut)} is at or before the first served row ({_iso(ea)}): the whole dataset would be reserved and nothing could be explored", (ea, eb))
    if eb - cut < DAY:
        return (f"RESERVED_REGION_HAS_NO_DATA: the boundary {_iso(cut)} leaves {max(0.0, (eb - cut) / DAY):.2f} days of data after it (the funding data actually served ends {_iso(eb)}): a holdout over "
                f"data that does not exist yet is a prospective holdout, not a reserved one", (ea, eb))
    return None, (ea, eb)


def venue_id(root: str) -> str:
    """The venue the history is of: ``<price source>|<category>``, the manifest's request host mapped THROUGH ``bot/venues.py`` (never a string compared as written: the independent re-review forged
    ``request.host`` as ``API.BYBIT.COM`` and, with the digest recomputed, got a venue the burn did not apply to). The key is the registry's FEED ENVIRONMENT, because a holdout is a region of
    the PRICE series: Bybit demo trading is matched against real mainnet prices, so a demo history and a mainnet history are the same region and share one burn (the re-review's ``demo|linear``
    clean slate). A host that is not a registered venue's REST host (the EU venue among them, which is unqualified and refused) and a category outside ``CATEGORIES`` are ``VENUE_UNKNOWN``. A
    holdout is a region of TIME on a venue: the mark interval, the data root and the ledger directory are not part of it. Read without verification: every path that grants a read goes on to
    verify the datasets."""
    import venues as V
    path = os.path.join(root, MH.FUNDING, "manifest.json")
    try:
        with open(path, "rb") as fh:
            man = json.loads(fh.read())
    except (OSError, ValueError) as exc:
        raise RH.HarnessError(f"VENUE_UNKNOWN: the funding manifest is unreadable ({type(exc).__name__})") from exc
    req = man.get("request") if isinstance(man, dict) else None
    req = req if isinstance(req, dict) else {}
    host, cat = req.get("host"), req.get("category")
    if not isinstance(host, str) or not isinstance(cat, str) or not host.strip() or not cat.strip():
        raise RH.HarnessError("VENUE_UNKNOWN: the funding manifest records no request host/category")
    wanted, category = host.strip().lower(), cat.strip().lower()
    names = sorted(n for n, h in V.rest_hosts().items() if h == wanted)
    if len(names) != 1:
        raise RH.HarnessError(f"VENUE_UNKNOWN: the funding manifest's request host {host!r} is not the REST host of a registered venue ({sorted(V.rest_hosts().values())}); the registry in bot/venues.py decides "
                              f"what a venue is, and an unqualified venue (such as Bybit EU) is not one")
    if category not in CATEGORIES:
        raise RH.HarnessError(f"VENUE_UNKNOWN: the funding manifest's request category {cat!r} is not one of {CATEGORIES}")
    return f"{V.feed_environment(names[0])}|{category}"


# ------------------------------------------------------------------ the registry
@contextlib.contextmanager
def _locked(path: str) -> Iterator[None]:
    """Exclusive lock around every read-decide-append (two terminals must not both pass the same check)."""
    os.makedirs(os.path.dirname(path) or ".", mode=0o700, exist_ok=True)
    fd = os.open(path + ".lock", os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _rows(path: str) -> List[Dict[str, Any]]:
    """The verified rows ([] when the registry does not exist). A registry that exists but does not verify refuses everything: one nobody can trust is worse than none."""
    if not os.path.exists(path):
        return []
    try:
        v = EC.verify(path)
        lines, quarantined = EC.read_lines(path)
    except (OSError, ValueError, EC.ChainDesync) as exc:
        raise RH.HarnessError(f"HOLDOUT_LEDGER_UNVERIFIED: {path} cannot be read ({type(exc).__name__})") from exc
    if not v.get("ok") or not v.get("fully_chained") or quarantined:
        raise RH.HarnessError(f"HOLDOUT_LEDGER_UNVERIFIED: {path} failed hash-chain verification ({[i.get('code') for i in v.get('issues', [])] or 'torn tail'}); restore it, do not rewrite it")
    rows = EC.rows_of(lines)
    if any(not isinstance(r, dict) for r in rows):
        raise RH.HarnessError(f"HOLDOUT_LEDGER_UNVERIFIED: {path} holds a row that is not an object")
    return rows


def chain_head(path: str) -> Optional[Dict[str, Any]]:
    """``{n, head}`` of the registry's hash chain (None when it does not exist). Stamped on every result: a rollback to an older consistent state is only visible by comparing it."""
    return EC.head_of(path) if os.path.exists(path) else None


def _state(path: str, venue: str) -> Dict[str, Any]:
    mine = [r for r in _rows(path) if r.get("venue") == venue]
    reserves = [r for r in mine if r.get("kind") == "reserve"]
    reads = [r for r in mine if r.get("kind") == "read"]
    finals = [r for r in reads if r.get("purpose") == FINAL]
    spent = {f.get("from_ms") for f in finals}
    open_ = [r for r in reserves if r.get("from_ms") not in spent]
    burning = [r for r in reads if r.get("purpose") in (UNRESERVED, FINAL, PRIOR_STATED) and isinstance(r.get("to_ms"), int)]
    top = max(burning, key=lambda r: r["to_ms"], default=None)
    return {"reserves": reserves, "finals": finals, "reads": reads, "active": open_[-1] if open_ else None,
            "burned_to": top["to_ms"] if top else 0, "burned_by": top.get("purpose") if top else None, "burned_reader": top.get("reader") if top else None}


def _append(path: str, row: Dict[str, Any]) -> None:
    try:
        os.makedirs(os.path.dirname(path) or ".", mode=0o700, exist_ok=True)
        EC.append(path, row)
    except (OSError, EC.ChainDesync) as exc:
        raise RH.HarnessError(f"HOLDOUT_LEDGER_WRITE_FAILED: {type(exc).__name__}: the read was not recorded, so no data was returned") from exc


def prior_evaluations(ledger_dir: str) -> List[Dict[str, Any]]:
    """Research ledgers in ``ledger_dir`` that already hold rows: evidence that evaluations of a history root ran there, whatever this registry knows."""
    out: List[Dict[str, Any]] = []
    if not os.path.isdir(ledger_dir):
        return out
    for name in sorted(os.listdir(ledger_dir)):
        if not name.endswith(".jsonl") or not name.startswith(PRIOR_LEDGER_PREFIXES):
            continue
        try:
            with open(os.path.join(ledger_dir, name), "rb") as fh:
                n = sum(1 for ln in fh if ln.strip())
        except OSError:
            continue
        if n:
            out.append({"file": name, "rows": n})
    return out


def prior_evaluations_anywhere(root: str, ledger_dir: Optional[str] = None) -> Tuple[List[Dict[str, Any]], List[str]]:
    """``(rows, dirs_scanned)``: the research ledgers of ANY directory the ledger rule can resolve to for this root (``candidate_readiness.ledger_dir_candidates``), each entry naming its directory.
    The independent re-review found the detection blind to every directory but the one it was handed: ``candidate_readiness.py --history-root R`` writes to ``<parent of R>/candidate_ledgers``
    while ``make reserve-holdout`` scanned ``$(DATA)/ledgers``, so an earlier direct run was invisible."""
    dirs = CR.ledger_dir_candidates(root, ledger_dir)
    out: List[Dict[str, Any]] = []
    for d in dirs:
        out.extend({"dir": d, **r} for r in prior_evaluations(d))
    return out, dirs


CORPUS_MANIFEST = os.path.join(BOT, "data", "real_bybit_btc_4h", "MANIFEST.json")
CORPUS_VENUE = "mainnet|linear"


def corpus_overlap(venue: str, cut_ms: int, end_ms: int, *, manifest_path: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Where the committed BTCUSDT corpus (``bot/data/real_bybit_btc_4h``: Bybit mainnet funding and 4h bars) covers the RESERVED region, as numbers; None when it does not, or for another venue.
    That corpus is read IN SAMPLE by every ``candidate_competition`` / readiness run (the cash_and_carry row) and lives outside this registry, so BTCUSDT and the BTC-driven regime inside the
    reserved region are already in sample before any final read (independent re-review, finding 3: for a 660-day backfill ending 2026-11-10 the corpus covers 136 of the 198 reserved days)."""
    if venue != CORPUS_VENUE:
        return None
    try:
        with open(manifest_path or CORPUS_MANIFEST, encoding="utf-8") as fh:
            files = (json.load(fh).get("files") or {})
        fund = next(f for f in files.values() if isinstance(f, dict) and f.get("kind") == "funding")
        first = int(dt.datetime.fromisoformat(str(fund["first_timestamp"]).replace("Z", "+00:00")).timestamp() * 1000)
        last = int(dt.datetime.fromisoformat(str(fund["last_timestamp"]).replace("Z", "+00:00")).timestamp() * 1000)
    except (OSError, ValueError, StopIteration, KeyError, TypeError):
        return None
    span = int(end_ms) - int(cut_ms)
    overlap = min(last, int(end_ms)) - max(first, int(cut_ms))
    if span <= 0 or overlap <= 0:
        return None
    return {"symbols": ["BTCUSDT"], "corpus": os.path.relpath(os.path.dirname(CORPUS_MANIFEST), BOT) if not manifest_path else "custom", "corpus_first": _iso(first),
            "corpus_last": _iso(last), "overlap_days": round(overlap / DAY, 3), "fraction_of_reserved_region": round(overlap / span, 3),
            "consequence": "BTCUSDT funding and the BTC-driven regime in this part of the reserved region are ALREADY in sample (the committed corpus is read in sample by candidate_competition): a final read "
                           "cannot claim ignorance of them; exclude BTCUSDT from it, or say so with the result"}


def _reserve_command(root: str, mark_interval: Optional[str]) -> str:
    mi = f" --mark-interval {mark_interval}" if mark_interval else ""
    return f"python3 tools/history_holdout.py --reserve --history-root {root}{mi}"


def _not_reserved(root: str, mark_interval: Optional[str], path: str, st: Dict[str, Any]) -> RH.HarnessError:
    if st["finals"]:
        return RH.HarnessError(f"HOLDOUT_SPENT: the reserved region of this venue was read once ({_iso(st['finals'][-1]['to_ms'])}) and is no longer a holdout. Reserve a new one over data "
                               f"that did not exist then ({_reserve_command(root, mark_interval)}) before exploring again; registry: {path}")
    return RH.HarnessError(f"HOLDOUT_NOT_RESERVED: no holdout is reserved for this venue (registry: {path}; {FLOOR_NOTE}). Reading the history now would use the region a later test needs. Run: "
                           f"{_reserve_command(root, mark_interval)}  (or pass --unreserved-exploratory: the result is then stamped NOT_A_HOLDOUT_STUDY and the time it read is burned: no "
                           f"holdout can be reserved before it)")


def _arithmetic(a: int, b: int, cut: int, *, requested: Optional[Tuple[int, int]] = None) -> Dict[str, Any]:
    """The window arithmetic over ``[a, b]`` (the EFFECTIVE window: what the datasets actually served) with the boundary ``cut``. ``requested`` is the window the manifest asked for; when the data
    stops short of it the difference is stated (and warned about) rather than counted as held-out rows that do not exist."""
    need = int(CR.grid_requirements(formation_days=30, hold_days=7, min_rows=60)["window_days_required"])
    explore, reserved = round(max(0, cut - a) / DAY, 3), round(max(0, b - max(cut, a)) / DAY, 3)
    floor = int(CR.SV.MIN_OBSERVATIONS["oos_test"])
    rows = int(reserved // 7)
    out = {"standard_grid_days_required": need, "explore_days": explore, "reserved_days": reserved, "explore_supports_standard_grid": explore >= need, "reserved_rows_possible": rows,
           "oos_verdict_floor_rows": floor, "reserved_supports_a_verdict_above_insufficient": rows >= floor,
           "arithmetic": f"the standard 60 x 7-day grid needs {need} days; the explore window has {explore}, the reserved region {reserved} ({rows} non-overlapping 7-day rows; the verdict "
                         f"ladder needs {floor} independent out-of-sample observations to say more than INSUFFICIENT_DATA)"}
    if rows < floor:
        out["warning"] = (f"the reserved region holds {rows} non-overlapping 7-day rows, fewer than the {floor} the verdict ladder needs: a final read of this size can REJECT a hypothesis "
                          f"but can never support more than INSUFFICIENT_DATA")
    if requested is not None:
        out["requested_window_ms"] = [int(requested[0]), int(requested[1])]
        out["served_window_ms"] = [int(a), int(b)]
        short_end = round((int(requested[1]) - int(b)) / DAY, 3)
        if short_end > 1:
            out["data_ends_before_the_requested_end_days"] = short_end
            out["warning"] = (f"the funding data ends {short_end} days before the requested window end: the reserved region holds {reserved} days of data, not the {round((requested[1] - cut) / DAY, 3)} requested"
                              + (f"; {out['warning']}" if "warning" in out else ""))
    return out


# ------------------------------------------------------------------ reserve / status
def reserve(registry: str, root: str, *, mark_interval: Optional[str] = None, frac: float = RESERVED_FRACTION, ledger_dir: Optional[str] = None,
            prior_reads_through_ms: Optional[int] = None, reader: str = "") -> Dict[str, Any]:
    """Reserve the last ``frac`` of the funding window of ``root``, once per venue. Idempotent: an existing reservation is returned unchanged (``already_reserved``), whatever the data has
    grown to since. Refused when a recorded read burned the time the boundary falls in (ALREADY_READ, or RESERVATION_OVERLAPS_A_SPENT_HOLDOUT after a final read), and when the research
    ledgers beside the data show earlier evaluations (PRIOR_EVALUATION_DETECTED) unless ``prior_reads_through_ms`` states, on the record, how far those reads reached."""
    _check_fraction(frac)
    CR._verified_dataset(root, MH.FUNDING)                              # the window is evidence: the datasets must verify before they are relied on ...
    mark_name, _iv = CR._mark_dataset_name(root, mark_interval)
    CR._verified_dataset(root, mark_name)                               # ... and the named mark dataset must EXIST: an interval typed by hand is not a window
    v = venue_id(root)
    a, b = _window(root)
    if prior_reads_through_ms is not None and (isinstance(prior_reads_through_ms, bool) or not isinstance(prior_reads_through_ms, int) or prior_reads_through_ms <= 0):
        raise RH.HarnessError("prior_reads_through must be a positive epoch-millisecond instant")
    with _locked(registry):
        st = _state(registry, v)
        if st["active"] is not None:
            out = {**st["active"], "already_reserved": True}
            if abs(float(st["active"].get("frac", frac)) - float(frac)) > 1e-12:
                out["requested_fraction_ignored"] = float(frac)             # the boundary never moves: the ledgered one stays
            return out
        cut = cut_for(a, b, frac)
        problem, (ea, eb) = reservation_problem(root, a, b, cut)
        if problem:
            raise RH.HarnessError(f"{problem}. (The boundary is the last {frac:.0%} of the REQUESTED window {_iso(a)}..{_iso(b)}. Nothing was recorded.)")
        prior, scanned = prior_evaluations_anywhere(root, ledger_dir)
        stated = any(r.get("purpose") == PRIOR_STATED for r in st["reads"])
        if prior and prior_reads_through_ms is None and not stated:
            raise RH.HarnessError(f"PRIOR_EVALUATION_DETECTED: research ledgers (looked in {scanned}) already hold rows ({prior}), so evaluations ran before this registry existed and read an unknown "
                                  f"stretch of the history. State how far they reached: --prior-reads-through <UTC instant> (it is recorded and burns that time; the reservation must then fall "
                                  f"after it). An honest 'through today' means this data cannot be reserved: collect later data")
        if prior_reads_through_ms is not None:
            _append(registry, {"kind": "read", "schema": SCHEMA, "venue": v, "purpose": PRIOR_STATED, "from_ms": 0, "to_ms": int(prior_reads_through_ms), "reader": reader or "owner statement",
                               "statement": f"the owner states that earlier evaluations read this venue's history through {_iso(prior_reads_through_ms)}", "prior_evaluations": prior, "ledger_dirs_scanned": scanned})
            st = _state(registry, v)
        if cut < st["burned_to"]:
            needed = a + int(-(-(st["burned_to"] - a) // (1.0 - float(frac))))
            if st["burned_by"] == FINAL:
                raise RH.HarnessError(f"RESERVATION_OVERLAPS_A_SPENT_HOLDOUT: the last {frac:.0%} of this window starts {_iso(cut)}, before the end of the region already read once "
                                      f"({_iso(st['burned_to'])}). The window must grow to at least {_iso(needed)} before a new holdout exists")
            raise RH.HarnessError(f"ALREADY_READ: a recorded read ({st['burned_by']}, reader {st['burned_reader']!r}) covered this venue's history through {_iso(st['burned_to'])}, past the "
                                  f"boundary {_iso(cut)} a {frac:.0%} reservation would use: that time can no longer be a holdout, whichever root, ledger directory or mark dataset reached it. "
                                  f"The window must grow to at least {_iso(needed)} before a holdout exists, or treat every result on it as exploratory; registry: {registry}")
        row = {"kind": "reserve", "schema": SCHEMA, "venue": v, "from_ms": cut, "window_ms": [a, b], "frac": float(frac), "reader": reader, "prior_evaluations": prior, "ledger_dirs_scanned": scanned,
               "mark_dataset": mark_name, "served_window_ms": [ea, eb], "btc_corpus_overlap": corpus_overlap(v, cut, eb),
               **{k: x for k, x in _arithmetic(ea, eb, cut, requested=(a, b)).items() if k in ("explore_days", "reserved_days", "reserved_rows_possible", "data_ends_before_the_requested_end_days")}}
        _append(registry, row)
        return row


def status(registry: str, root: str, *, mark_interval: Optional[str] = None, ledger_dir: Optional[str] = None) -> Dict[str, Any]:
    """Read-only: what is reserved for this venue, what is burned, and what the arithmetic of the two windows of THIS root allows. Never writes."""
    v = venue_id(root)
    a, b = _window(root)
    st = _state(registry, v)
    out: Dict[str, Any] = {"schema": SCHEMA, "venue": v, "registry": registry, "chain_head": chain_head(registry), **registry_source(registry), "history_root": os.path.abspath(root), "window_ms": [a, b],
                           "window_days": round((b - a) / DAY, 3), "burned_through_ms": st["burned_to"] or None, "burned_through": _iso(st["burned_to"]) if st["burned_to"] else None,
                           "floor_note": FLOOR_NOTE}
    prior, scanned = prior_evaluations_anywhere(root, ledger_dir)
    stated = any(r.get("purpose") == PRIOR_STATED for r in st["reads"])
    out["prior_evaluations"], out["ledger_dirs_scanned"] = prior, scanned
    cut = None
    if st["active"] is not None:
        out["state"], cut = "RESERVED", int(st["active"]["from_ms"])
        out["from_ms"] = cut
    elif st["finals"]:
        out["state"] = "SPENT"
    else:
        out["state"] = "NOT_RESERVED"
        try:
            cut = cut_for(a, b)
            out["would_reserve_from_ms"] = cut
            out["reservable"] = bool(cut >= st["burned_to"] and not (prior and not stated))
            if cut < st["burned_to"]:
                out["not_reservable_because"] = f"recorded reads already covered this venue through {_iso(st['burned_to'])}, past the boundary {_iso(cut)}"
            elif prior and not stated:
                out["not_reservable_because"] = ("PRIOR_EVALUATION_DETECTED: research ledgers already hold rows, so --reserve refuses until the owner states --prior-reads-through <UTC instant> "
                                                 "(and that instant must be before the boundary)")
        except RH.HarnessError as exc:
            out["would_reserve_error"] = str(exc)
    if cut is not None:
        ea, eb = effective_window(root, a, b)
        out.update(_arithmetic(ea, eb, cut, requested=(a, b)))
        out["btc_corpus_overlap"] = corpus_overlap(v, cut, eb)
        if out["state"] == "NOT_RESERVED":
            problem, _w = reservation_problem(root, a, b, cut)
            if problem:
                out["reservable"] = False
                out["not_reservable_because"] = (out["not_reservable_because"] + "; " + problem) if out.get("not_reservable_because") else problem
    out["prior_evaluations_note"] = ("only the directories listed in ledger_dirs_scanned were looked in: an evaluation that wrote its ledgers elsewhere (for example an ms_pipeline run with another required "
                                      "--ledger-dir) is invisible to this; if the venue's history was EVER evaluated, state --prior-reads-through")
    return out


# ------------------------------------------------------------------ the guarded loads
def _info_stamp(registry: Any, status_: str, purpose: Optional[str], cut: Optional[int], label: str, **extra: Any) -> Dict[str, Any]:
    reg = registry if isinstance(registry, str) else None
    return {"status": status_, "purpose": purpose, "cut_ms": cut, "label": label, "schema": SCHEMA, "registry": reg, "chain_head": chain_head(reg) if reg else None, **registry_source(reg), **extra}


def load(root: str, *, universe_dir: str, spot_dir: Optional[str] = None, mark_interval: Optional[str] = None, ledger_path: Any = None, reader: str = "",
         unreserved_exploratory: bool = False) -> Tuple[List[Any], Dict[str, Any]]:
    """The EVALUATION load: ``(series, info)`` with every row at or after the reserved boundary never built. Refuses with the exact command when nothing is reserved.

    ``ledger_path`` is REQUIRED: the registry path (``default_registry_path()``), or ``UNGUARDED`` (tests, embeddings: the whole window is read and ``info['holdout']`` says
    NOT_GUARDED). ``None`` is refused (HOLDOUT_LEDGER_REQUIRED): a missing argument must never be a silent opt-out."""
    if ledger_path is UNGUARDED:
        series, info = CR.load_history_root(root, universe_dir=universe_dir, spot_dir=spot_dir, mark_interval=mark_interval)
        info["holdout"] = _info_stamp(None, "NOT_GUARDED", None, None, "NOT_GUARDED_LIBRARY_CALL: the whole window was read and no holdout is enforced; this is not a holdout study")
        return series, info
    if not isinstance(ledger_path, str) or not ledger_path:
        raise RH.HarnessError("HOLDOUT_LEDGER_REQUIRED: pass the registry path (history_holdout.default_registry_path()) or history_holdout.UNGUARDED to read the whole window on purpose")
    v = venue_id(root)
    st = _state(ledger_path, v)
    act = st["active"]
    if act is not None:
        cut = int(act["from_ms"])
        series, info = CR.load_history_root(root, universe_dir=universe_dir, spot_dir=spot_dir, mark_interval=mark_interval, cut_ms=cut)
        a, b = _window(root)
        info["holdout"] = _info_stamp(ledger_path, "RESERVED", EXPLORE, cut, "EXPLORE_WINDOW_ONLY: the reserved holdout was not read", reserved_from_ms=cut, reserved_from=_iso(cut), venue=v,
                                      note="every funding print and mark close at or after the boundary was never built into a series; the loader's coverage window ends at the boundary; manifest "
                                           "status flags computed over the WHOLE dataset are NOT cut and are not neutral (a funding-interval change inside the reserved region is a funding-cap event)",
                                      **{k: x for k, x in _arithmetic(*effective_window(root, a, b), cut, requested=(a, b)).items()
                                         if k in ("explore_days", "reserved_days", "reserved_rows_possible", "warning", "data_ends_before_the_requested_end_days")})
        return series, info
    if st["finals"] or not unreserved_exploratory:
        raise _not_reserved(root, mark_interval, ledger_path, st)          # a SPENT venue has no unreserved reading either: its holdout was already consumed
    series, info = CR.load_history_root(root, universe_dir=universe_dir, spot_dir=spot_dir, mark_interval=mark_interval)
    a, b = _window(root)
    with _locked(ledger_path):
        again = _state(ledger_path, v)
        if again["active"] is not None or again["finals"]:                  # another process reserved or spent while this one loaded: refuse, do not read around it
            raise RH.HarnessError(f"HOLDOUT_CHANGED_WHILE_LOADING: a holdout was reserved or spent for this venue while this read was loading the whole window (registry: {ledger_path}). Nothing was "
                                  f"returned and nothing was recorded; re-run the evaluation: it will now read the explore window only")
        if not any(r.get("purpose") == UNRESERVED and r.get("reader") == reader and r.get("from_ms") == a and r.get("to_ms") == b for r in again["reads"]):
            _append(ledger_path, {"kind": "read", "schema": SCHEMA, "venue": v, "purpose": UNRESERVED, "from_ms": a, "to_ms": b, "reader": reader})
    info["holdout"] = _info_stamp(ledger_path, "UNRESERVED_EXPLORATORY", UNRESERVED, None, NOT_A_HOLDOUT, venue=v, burned_through=_iso(b), note=FLOOR_NOTE)
    return series, info


def final_read(root: str, *, universe_dir: str, spot_dir: Optional[str] = None, mark_interval: Optional[str] = None, ledger_path: str, reader: str = "") -> Tuple[List[Any], Dict[str, Any]]:
    """The ONE read of the reserved region: load (a dataset that does not verify costs nothing), RECORD under a lock, then return. ``info['holdout']['evaluate_only_from_ms']`` is the
    boundary: the series carry the explore window too (a formation window looks backwards) but only decisions from that boundary on may be scored. A second call raises
    ``FINAL_READ_ALREADY_SPENT`` (also when another process won the race)."""
    if not isinstance(ledger_path, str) or not ledger_path:
        raise RH.HarnessError("HOLDOUT_LEDGER_REQUIRED: the final read is only granted against a registry")
    v = venue_id(root)
    with _locked(ledger_path):
        st = _state(ledger_path, v)
        act = st["active"]
        if act is None:
            if st["finals"]:
                raise RH.HarnessError(f"FINAL_READ_ALREADY_SPENT: the reserved region of this venue was read once and is no longer a holdout; registry: {ledger_path}")
            raise _not_reserved(root, mark_interval, ledger_path, st)
        series, info = CR.load_history_root(root, universe_dir=universe_dir, spot_dir=spot_dir, mark_interval=mark_interval)
        _a, b = _window(root)
        cut = int(act["from_ms"])
        _append(ledger_path, {"kind": "read", "schema": SCHEMA, "venue": v, "purpose": FINAL, "from_ms": cut, "to_ms": b, "reader": reader})
    info["holdout"] = _info_stamp(ledger_path, "SPENT", FINAL, None, "FINAL_READ: the reserved region has now been read once and is spent", venue=v, evaluate_only_from_ms=cut,
                                  evaluate_only_from=_iso(cut))
    return series, info


# ------------------------------------------------------------------ CLI
def _parse_instant(text: str) -> int:
    try:
        value = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"not an ISO-8601 instant: {text!r}") from exc
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.timezone.utc)
    return int(value.timestamp() * 1000)


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--reserve", action="store_true", help="reserve the last fraction of the funding window (once per venue; idempotent)")
    g.add_argument("--status", action="store_true", help="read-only: what is reserved, what is burned, and what the window arithmetic allows")
    ap.add_argument("--history-root", required=True)
    ap.add_argument("--mark-interval", default=None)
    ap.add_argument("--registry", default=None, help=f"the registry file (default: ${REGISTRY_ENV} or {REGISTRY_DEFAULT}); one per user, independent of the data root and of --ledger-dir")
    ap.add_argument("--ledger-dir", default=None, help="the research ledger directory the multiple-testing counts live in (only used to DETECT earlier evaluations)")
    ap.add_argument("--fraction", type=float, default=RESERVED_FRACTION, help=f"reserved fraction of the window, in [{MIN_FRACTION}, {MAX_FRACTION}] (default {RESERVED_FRACTION})")
    ap.add_argument("--prior-reads-through", type=_parse_instant, default=None, metavar="UTC_INSTANT",
                    help="REQUIRED when research ledgers beside the data show earlier evaluations: the instant their reads reached (recorded; it burns that time)")
    a = ap.parse_args(argv)
    path = os.path.abspath(a.registry) if a.registry else default_registry_path()
    try:
        if a.reserve:
            reserve(path, a.history_root, mark_interval=a.mark_interval, frac=a.fraction, ledger_dir=a.ledger_dir, prior_reads_through_ms=a.prior_reads_through, reader="history_holdout.py --reserve")
        out = status(path, a.history_root, mark_interval=a.mark_interval, ledger_dir=a.ledger_dir)
    except RH.HarnessError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(out, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
