#!/usr/bin/env python3
"""Reserve the holdout BEFORE the first evaluation read of a market_history root, and make the reserved window unreadable until the one test that may read it.

    python3 tools/history_holdout.py --status  --history-root R [--mark-interval M] [--ledger-dir L]
    python3 tools/history_holdout.py --reserve --history-root R [--mark-interval M] [--ledger-dir L] [--fraction 0.30]

WHY THIS EXISTS
===============
``tools/reserved_holdout.py`` records reads of the old Binance daily corpus (slice 57 declared an "out-of-sample" window that a full-sample run had already read, and its ``--certify``
exists to catch that). The cross-sectional study reads a DIFFERENT store: a ``market_history`` root through ``candidate_readiness.load_history_root``. That ledger never saw it, so
``--certify`` was vacuous for it, and nothing stopped an evaluator from walking the whole window, tuning on what it saw, and then calling the last part "out of sample". The data this
study needs is the owner's, weeks of collection and a REST backfill: spent on a hypothesis chosen after looking at its own test window it proves nothing and cannot be collected twice.

WHAT IT DOES
============
* ``--reserve`` appends ONE row to an append-only, hash-chained ledger (``evidence_chain``, the machinery the multiple-testing ledgers use): the dataset identity, the funding window as the
  manifest records it, and ``from_ms`` = the UTC-day boundary at 70% of that window. Everything at or after ``from_ms`` is the reserved holdout. It is made once and never moves (the
  recorder keeps recording; a recomputed boundary would drift later and quietly hand the explorer yesterday's holdout). It is refused if the dataset was already read unreserved.
* every EVALUATION load goes through ``load`` and is CUT at ``from_ms`` inside the loader (``load_history_root(cut_ms=...)``): the reserved rows are not ignored, they are never built, and
  the funnel (which symbols are loaded) is judged on the explore window as well. No reservation, no read; the refusal names the ledger it looked in and the command to run.
* the reserved window is readable exactly ONCE, through ``final_read``, which loads (so a dataset that does not verify costs nothing), records the read, and only then returns data. After
  it the holdout is SPENT: further exploration needs a new reservation over data that did not exist then, and the same data can never be reserved again. No CLI flag opens it: the
  frozen-hypothesis evaluator that may call ``final_read`` is not built, and a human typing a flag is the failure this exists to prevent.
* ``--unreserved-exploratory`` (on the research CLIs) reads the whole window anyway; the result is stamped ``NOT_A_HOLDOUT_STUDY`` and the read is RECORDED, which permanently forbids
  reserving that dataset (a read is a read, whichever number it produced).

WHAT IT DOES NOT DO
===================
It cannot see a read made outside these tools (a notebook, a hand-written script): like ``reserved_holdout`` it is a FLOOR, and it says so. It does not score anything, writes no field a
gate reads, and has no path to ``live_authorized``. It does not make a passing exploration real: a candidate that survives the explore window still needs the single final read, a frozen
hypothesis, prospective evidence and the owner's attestation.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from typing import Any, Dict, List, Optional, Sequence, Tuple

BOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (BOT, os.path.join(BOT, "tools")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import candidate_readiness as CR  # noqa: E402
import evidence_chain as EC  # noqa: E402
import market_history as MH  # noqa: E402
import research_harness as RH  # noqa: E402

SCHEMA = "history_holdout/1"
LEDGER_NAME = "history_holdout.jsonl"
DAY = 86_400_000
RESERVED_FRACTION = 0.30
EXPLORE, FINAL, UNRESERVED = "EXPLORE", "FINAL", "UNRESERVED_EXPLORE"
NOT_A_HOLDOUT = "NOT_A_HOLDOUT_STUDY: every number below was computed with the whole window in view; it is exploratory and must never be quoted as out-of-sample"
FLOOR_NOTE = "A FLOOR, not a census: a read made outside these tools (a notebook, a hand-written script) is invisible to this ledger"


# ------------------------------------------------------------------ pure helpers
def _iso(ms: int) -> str:
    return dt.datetime.fromtimestamp(int(ms) / 1000.0, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _check_fraction(frac: Any) -> None:
    if isinstance(frac, bool) or not isinstance(frac, (int, float)) or not (0.0 < float(frac) <= 0.5):
        raise RH.HarnessError(f"the reserved fraction must be a number in (0, 0.5], got {frac!r}")


def cut_for(a_ms: int, b_ms: int, frac: float = RESERVED_FRACTION) -> int:
    """The UTC-day boundary at ``1 - frac`` of the window [a, b]: everything at or after it is reserved. ``frac`` must be a number in (0, 0.5]: a holdout larger than the explore window
    leaves nothing to explore, and 0 is not a reservation."""
    _check_fraction(frac)
    cut = (int(a_ms) + int((1.0 - float(frac)) * (int(b_ms) - int(a_ms)))) // DAY * DAY
    if cut <= int(a_ms):
        raise RH.HarnessError(f"WINDOW_TOO_SHORT: the window {_iso(a_ms)}..{_iso(b_ms)} leaves no whole UTC day to explore before a {frac:.0%} reservation")
    return cut


def ledger_for(ledger_dir: str) -> str:
    return os.path.join(os.path.abspath(ledger_dir), LEDGER_NAME)


def _window(root: str) -> Tuple[int, int]:
    import backfill_plan as BP
    w = BP.window_of_funding_manifest(os.path.abspath(root))
    if w is None:
        raise RH.HarnessError("the funding dataset records no requested window (manifest request.start_ms/end_ms): there is nothing to reserve")
    return w


def dataset_id(root: str, mark_interval: Optional[str] = None) -> str:
    """Which data this is, independent of where it lives and of how far it has grown: host | category | the datasets read. Read from the funding manifest; the loader (which verifies
    every dataset) is what makes it trustworthy, and it runs before any read is granted."""
    path = os.path.join(root, MH.FUNDING, "manifest.json")
    try:
        with open(path, "rb") as fh:
            req = (json.loads(fh.read()).get("request") or {})
    except (OSError, ValueError) as exc:
        raise RH.HarnessError(f"DATASET_IDENTITY_UNKNOWN: the funding manifest is unreadable ({type(exc).__name__})") from exc
    host, cat = req.get("host"), req.get("category")
    if not isinstance(host, str) or not isinstance(cat, str) or not host or not cat:
        raise RH.HarnessError("DATASET_IDENTITY_UNKNOWN: the funding manifest records no request host/category")
    mark_name, _iv = CR._mark_dataset_name(root, mark_interval)
    return f"{host}|{cat}|funding+{mark_name}"


# ------------------------------------------------------------------ the ledger
def _rows(path: str) -> List[Dict[str, Any]]:
    """The verified rows ([] when the ledger does not exist). A ledger that exists but does not verify refuses everything: a holdout ledger nobody can trust is worse than none."""
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


def _state(path: str, ds: str) -> Dict[str, Any]:
    mine = [r for r in _rows(path) if r.get("dataset") == ds]
    reserves = [r for r in mine if r.get("kind") == "reserve"]
    finals = [r for r in mine if r.get("kind") == "read" and r.get("purpose") == FINAL]
    unreserved = [r for r in mine if r.get("kind") == "read" and r.get("purpose") == UNRESERVED]
    spent = {f.get("from_ms") for f in finals}
    open_ = [r for r in reserves if r.get("from_ms") not in spent]
    return {"reserves": reserves, "finals": finals, "unreserved": unreserved, "active": open_[-1] if open_ else None}


def _append(path: str, row: Dict[str, Any]) -> None:
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        EC.append(path, row)
    except (OSError, EC.ChainDesync) as exc:
        raise RH.HarnessError(f"HOLDOUT_LEDGER_WRITE_FAILED: {type(exc).__name__}: the read was not recorded, so no data was returned") from exc


def _reserve_command(root: str, mark_interval: Optional[str], path: str) -> str:
    mi = f" --mark-interval {mark_interval}" if mark_interval else ""
    return f"python3 tools/history_holdout.py --reserve --history-root {root}{mi} --ledger-dir {os.path.dirname(path)}"


def _not_reserved(root: str, mark_interval: Optional[str], path: str, st: Dict[str, Any]) -> RH.HarnessError:
    if st["finals"]:
        return RH.HarnessError(f"HOLDOUT_SPENT: the reserved window of this dataset was read once ({_iso(st['finals'][-1]['to_ms'])}) and is no longer a holdout. Reserve a new one over data "
                               f"that did not exist then ({_reserve_command(root, mark_interval, path)}) before exploring again; ledger: {path}")
    return RH.HarnessError(f"HOLDOUT_NOT_RESERVED: no holdout is reserved for this dataset (ledger: {path}; {FLOOR_NOTE}). Reading it now would use the window a later test needs. Run: "
                           f"{_reserve_command(root, mark_interval, path)}  (or pass --unreserved-exploratory: the result is then stamped NOT_A_HOLDOUT_STUDY and the dataset can never be reserved afterwards)")


# ------------------------------------------------------------------ reserve / status
def reserve(ledger_path: str, root: str, *, mark_interval: Optional[str] = None, frac: float = RESERVED_FRACTION, reader: str = "") -> Dict[str, Any]:
    """Reserve the last ``frac`` of the funding window, once. Idempotent: an existing reservation is returned unchanged (``already_reserved``), whatever the data has grown to since.
    Refused when the dataset was already read unreserved (``ALREADY_READ``) or when the new window would reach back into a spent holdout (``RESERVATION_OVERLAPS_A_SPENT_HOLDOUT``)."""
    _check_fraction(frac)                                       # a bad request is refused even when a reservation already exists
    CR._verified_dataset(root, MH.FUNDING)                      # the window is evidence: the dataset must verify before it is relied on
    ds = dataset_id(root, mark_interval)
    a, b = _window(root)
    st = _state(ledger_path, ds)
    if st["active"] is not None:
        out = {**st["active"], "already_reserved": True}
        if abs(float(st["active"].get("frac", frac)) - float(frac)) > 1e-12:
            out["requested_fraction_ignored"] = float(frac)             # the boundary never moves: the ledgered one stays
        return out
    if st["unreserved"]:
        first = st["unreserved"][0]
        raise RH.HarnessError(f"ALREADY_READ: this dataset was read with the whole window in view (reader {first.get('reader')!r}) before it was reserved, so no part of it can be a holdout. "
                              f"Collect later data and reserve that, or treat every result on it as exploratory; ledger: {ledger_path}")
    cut = cut_for(a, b, frac)
    if st["finals"]:
        spent_to = max(int(f["to_ms"]) for f in st["finals"])
        if cut < spent_to:
            needed = a + int(-(-(spent_to - a) // (1.0 - float(frac))))
            raise RH.HarnessError(f"RESERVATION_OVERLAPS_A_SPENT_HOLDOUT: the last {frac:.0%} of this window starts {_iso(cut)}, before the end of the holdout already spent ({_iso(spent_to)}). "
                                  f"The window must grow to at least {_iso(needed)} before a new holdout exists")
    row = {"kind": "reserve", "schema": SCHEMA, "dataset": ds, "from_ms": cut, "window_ms": [a, b], "frac": float(frac), "reader": reader}
    _append(ledger_path, row)
    return row


def _grid_days() -> int:
    return int(CR.grid_requirements(formation_days=30, hold_days=7, min_rows=60)["window_days_required"])


def status(ledger_path: str, root: str, *, mark_interval: Optional[str] = None) -> Dict[str, Any]:
    """Read-only: what is reserved for this dataset and what the arithmetic of the two windows allows. Never writes."""
    ds = dataset_id(root, mark_interval)
    a, b = _window(root)
    st = _state(ledger_path, ds)
    need = _grid_days()
    out: Dict[str, Any] = {"schema": SCHEMA, "dataset": ds, "ledger": os.path.abspath(ledger_path), "window_ms": [a, b], "window_days": round((b - a) / DAY, 3),
                           "standard_grid_days_required": need, "floor_note": FLOOR_NOTE}
    cut = None
    if st["active"] is not None:
        out["state"], cut = "RESERVED", int(st["active"]["from_ms"])
    elif st["finals"]:
        out["state"] = "SPENT"
        out["spent_through_ms"] = max(int(f["to_ms"]) for f in st["finals"])
    elif st["unreserved"]:
        out["state"] = "ALREADY_READ"
    else:
        out["state"] = "NOT_RESERVED"
        try:
            cut = cut_for(a, b)
            out["would_reserve_from_ms"] = cut
        except RH.HarnessError as exc:
            out["would_reserve_error"] = str(exc)
    if cut is not None:
        if out["state"] == "RESERVED":
            out["from_ms"] = cut
        out.update({"explore_days": round((cut - a) / DAY, 3), "reserved_days": round((b - cut) / DAY, 3)})
        out["explore_supports_standard_grid"] = out["explore_days"] >= need
        out["reserved_rows_possible"] = int(out["reserved_days"] // 7)
        out["arithmetic"] = (f"the standard 60 x 7-day grid needs {need} days; the explore window has {out['explore_days']}, the reserved window {out['reserved_days']} "
                             f"({out['reserved_rows_possible']} non-overlapping 7-day rows)")
    return out


# ------------------------------------------------------------------ the guarded loads
def _info_stamp(status_: str, purpose: Optional[str], cut: Optional[int], label: str, **extra: Any) -> Dict[str, Any]:
    return {"status": status_, "purpose": purpose, "cut_ms": cut, "label": label, "schema": SCHEMA, **extra}


def load(root: str, *, universe_dir: str, spot_dir: Optional[str] = None, mark_interval: Optional[str] = None, ledger_path: Optional[str] = None, reader: str = "",
         unreserved_exploratory: bool = False) -> Tuple[List[Any], Dict[str, Any]]:
    """The EVALUATION load: ``(series, info)`` with every row at or after the reserved boundary never built. Refuses with the exact command when nothing is reserved.

    ``ledger_path=None`` is the LIBRARY mode (a test or an embedding that has no ledger): the whole window is read and ``info['holdout']`` says NOT_GUARDED. Every research CLI passes its
    ledger (a test per CLI proves that), so a run that matters is never in this mode silently."""
    if ledger_path is None:
        series, info = CR.load_history_root(root, universe_dir=universe_dir, spot_dir=spot_dir, mark_interval=mark_interval)
        info["holdout"] = _info_stamp("NOT_GUARDED", None, None, "NOT_GUARDED_LIBRARY_CALL: the whole window was read and no holdout is enforced; this is not a holdout study")
        return series, info
    ds = dataset_id(root, mark_interval)
    st = _state(ledger_path, ds)
    act = st["active"]
    if act is not None:
        cut = int(act["from_ms"])
        series, info = CR.load_history_root(root, universe_dir=universe_dir, spot_dir=spot_dir, mark_interval=mark_interval, cut_ms=cut)
        info["holdout"] = _info_stamp("RESERVED", EXPLORE, cut, "EXPLORE_WINDOW_ONLY: the reserved holdout was not read", reserved_from_ms=cut, reserved_from=_iso(cut),
                                      note="every funding print and mark close at or after the boundary was never built into a series; the funnel was judged on the explore window as well")
        return series, info
    if st["finals"] or not unreserved_exploratory:
        raise _not_reserved(root, mark_interval, ledger_path, st)          # a SPENT dataset has no unreserved reading either: its holdout was already consumed
    series, info = CR.load_history_root(root, universe_dir=universe_dir, spot_dir=spot_dir, mark_interval=mark_interval)
    a, b = _window(root)
    if not any(r.get("reader") == reader for r in st["unreserved"]):
        _append(ledger_path, {"kind": "read", "schema": SCHEMA, "dataset": ds, "purpose": UNRESERVED, "from_ms": a, "to_ms": b, "reader": reader})
    info["holdout"] = _info_stamp("UNRESERVED_EXPLORATORY", UNRESERVED, None, NOT_A_HOLDOUT, note=FLOOR_NOTE)
    return series, info


def final_read(root: str, *, universe_dir: str, spot_dir: Optional[str] = None, mark_interval: Optional[str] = None, ledger_path: str, reader: str = "") -> Tuple[List[Any], Dict[str, Any]]:
    """The ONE read of the reserved window: load (a dataset that does not verify costs nothing), RECORD, then return. ``info['holdout']['evaluate_only_from_ms']`` is the boundary: the series
    carry the explore window too (a formation window looks backwards) but only decisions from that boundary on may be scored. A second call raises ``FINAL_READ_ALREADY_SPENT``."""
    if not isinstance(ledger_path, str) or not ledger_path:
        raise RH.HarnessError("HOLDOUT_LEDGER_REQUIRED: the final read is only granted against a ledger")
    ds = dataset_id(root, mark_interval)
    st = _state(ledger_path, ds)
    act = st["active"]
    if act is None:
        if st["finals"]:
            raise RH.HarnessError(f"FINAL_READ_ALREADY_SPENT: the reserved window of this dataset was read once and is no longer a holdout; ledger: {ledger_path}")
        raise _not_reserved(root, mark_interval, ledger_path, st)
    series, info = CR.load_history_root(root, universe_dir=universe_dir, spot_dir=spot_dir, mark_interval=mark_interval)
    _a, b = _window(root)
    cut = int(act["from_ms"])
    _append(ledger_path, {"kind": "read", "schema": SCHEMA, "dataset": ds, "purpose": FINAL, "from_ms": cut, "to_ms": b, "reader": reader})
    info["holdout"] = _info_stamp("SPENT", FINAL, None, "FINAL_READ: the reserved window has now been read once and is spent", evaluate_only_from_ms=cut, evaluate_only_from=_iso(cut))
    return series, info


# ------------------------------------------------------------------ CLI
def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--reserve", action="store_true", help="reserve the last fraction of the funding window (once; idempotent)")
    g.add_argument("--status", action="store_true", help="read-only: what is reserved and what the window arithmetic allows")
    ap.add_argument("--history-root", required=True)
    ap.add_argument("--mark-interval", default=None)
    ap.add_argument("--ledger-dir", default=None, help="the research ledger directory (default: the same rule every research CLI uses)")
    ap.add_argument("--fraction", type=float, default=RESERVED_FRACTION, help="reserved fraction of the window, in (0, 0.5] (default 0.30)")
    a = ap.parse_args(argv)
    path = ledger_for(CR.ledger_dir_for(a.history_root, a.ledger_dir))
    try:
        if a.reserve:
            reserve(path, a.history_root, mark_interval=a.mark_interval, frac=a.fraction, reader="history_holdout.py --reserve")
        out = status(path, a.history_root, mark_interval=a.mark_interval)
    except RH.HarnessError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(out, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
