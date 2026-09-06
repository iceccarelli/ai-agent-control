#!/usr/bin/env python3
"""A holdout is only a holdout if nothing has read it. This ledger proves that.

WHY THIS EXISTS
===============
Slice 57 declared `funding_carry_fade_btc_v1` on a pre-declared out-of-sample
window and cleared it at M1/M2 = 95.13/96.0 on n=41. The fold file is honest:
the cut is `index_midpoint_50pct`, derived from timestamps and index position
only, with a `forbidden` clause barring anyone from moving `t_mid` after the
counts exist. Every procedural step was followed.

It was still not a holdout.

Slice 55 had already measured the SAME 1,461 BTC bars end to end:

    BTCUSDT   M1/M2 = 97.0/97.5   n= 85   mean net R +0.0964    <- the winner
    ETHUSDT   M1/M2 = 47.7/48.5   n= 76   mean net R -0.0358
    SOLUSDT   M1/M2 =  2.3/ 1.0   n=162   mean net R -0.1658    <- largest, worst

The family was correctly frozen ABSENT when the 2-of-3 conjunction failed. Then
a BTC-only product was declared over `[t_mid, t1]` — a SUBSET of the very 1,461
bars that had produced the 97.0/97.5 reading. So the window was out-of-sample
with respect to FITTING and not with respect to SELECTION, and selection is what
happened: BTC was chosen because BTC won.

Forward: two closed trades, -1.0632 and -0.6084, mean -0.8358.

The existing invariants could not catch this. They check that a cut was declared
before scoring, that folds are unmodified, that constants did not move. All were
satisfied. None of them asks the only question that mattered: HAS ANYTHING READ
THESE BYTES BEFORE?

WHAT THIS LEDGER DOES
=====================
It is append-only and it records READS, not results. Every measurement declares
the dataset and the time range it touched. Before a range may be used as a
holdout, `certify_untouched` walks the ledger and refuses if any prior read
overlaps it — including a read that produced a NEGATIVE result, because a
negative result informs which hypothesis gets written next just as surely as a
positive one does.

It cannot repair the past. Slice 55's read is recorded here as a fact, which is
exactly why `funding_carry_fade_btc_v1` cannot be certified retroactively.

WHAT IT DOES NOT DO
===================
It does not score, re-score, or unfreeze anything. It writes no field any gate
reads. It cannot open the promotion gate and it has no path to `live_authorized`.

    python3 tools/reserved_holdout.py --status
    python3 tools/reserved_holdout.py --certify BINANCE_LINEAR_BTC_USDT_1D \\
        --from 2026-09-01T00:00:00Z --to 2027-03-01T00:00:00Z
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from typing import Any, Dict, List, Optional

DEFAULT_PATH = "artifacts/data_read_ledger.json"
SCHEMA = "data_read_ledger/1"


def _utc(text: str) -> dt.datetime:
    value = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.timezone.utc)
    return value


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _seed_reads() -> List[Dict[str, Any]]:
    """The reads this programme has already performed, reconstructed from artefacts.

    A FLOOR, not a census. Abandoned runs and one-off diagnostics left no
    artefact and are not here, so the true contamination is wider than this.
    Recording a floor is still decisive: one overlapping read is enough to
    refuse, and these overlap everything.
    """
    return [
        {"dataset": "BINANCE_LINEAR_BTC_USDT_1D",
         "from_utc": "2022-08-10T00:00:00Z", "to_utc": "2026-08-09T00:00:00Z",
         "read_by": "slice55 edge funding_carry_fade_v1 BTCUSDT",
         "purpose": "full-sample Stage-1 measurement, 1461 bars, n=85",
         "result_seen": "M1/M2 97.0/97.5, mean net R +0.0964 — the reading that "
                        "selected BTC out of three symbols",
         "recorded_utc": "2026-09-06T00:00:00Z"},
        {"dataset": "BINANCE_LINEAR_ETH_USDT_1D",
         "from_utc": "2022-08-10T00:00:00Z", "to_utc": "2026-08-09T00:00:00Z",
         "read_by": "slice55 edge funding_carry_fade_v1 ETHUSDT",
         "purpose": "full-sample Stage-1 measurement, n=76",
         "result_seen": "M1/M2 47.7/48.5, mean net R -0.0358",
         "recorded_utc": "2026-09-06T00:00:00Z"},
        {"dataset": "BINANCE_LINEAR_SOL_USDT_1D",
         "from_utc": "2022-08-10T00:00:00Z", "to_utc": "2026-08-09T00:00:00Z",
         "read_by": "slice55 edge funding_carry_fade_v1 SOLUSDT",
         "purpose": "full-sample Stage-1 measurement, n=162",
         "result_seen": "M1/M2 2.3/1.0, mean net R -0.1658",
         "recorded_utc": "2026-09-06T00:00:00Z"},
        {"dataset": "BINANCE_LINEAR_BTC_USDT_1D",
         "from_utc": "2024-08-09T00:00:00Z", "to_utc": "2026-08-09T00:00:00Z",
         "read_by": "slice57 OOS funding_carry_fade_btc_v1",
         "purpose": "the 'out-of-sample' half, [t_mid, t1], n=41",
         "result_seen": "M1/M2 95.13/96.0 — CONTAMINATED: this range is a strict "
                        "subset of the slice55 read above",
         "recorded_utc": "2026-09-06T00:00:00Z"},
        {"dataset": "BINANCE_LINEAR_BTC_USDT_1D",
         "from_utc": "2022-08-10T00:00:00Z", "to_utc": "2026-08-09T00:00:00Z",
         "read_by": "slice57 diagnostic full-sample BTCUSDT",
         "purpose": "diagnostic, refused by project_status for lacking a "
                    "validated-control attestation",
         "result_seen": "EDGE_EVIDENCE_POSITIVE, refused — but READ nonetheless",
         "recorded_utc": "2026-09-06T00:00:00Z"},
        {"dataset": "BINANCE_LINEAR_BTC_USDT_1D",
         "from_utc": "2026-08-09T00:00:00Z", "to_utc": "2026-08-24T00:00:00Z",
         "read_by": "slices 59-76 forward shadow",
         "purpose": "forward observation window after t1",
         "result_seen": "2 closed trades, -1.0632 and -0.6084, mean -0.8358",
         "recorded_utc": "2026-09-06T00:00:00Z"},
    ]


def load(path: str = DEFAULT_PATH) -> Dict[str, Any]:
    if not os.path.exists(path):
        return {"schema": SCHEMA, "seeded": False, "reads": [],
                "floor_not_census": (
                    "Abandoned runs and one-off diagnostics left no artefact and "
                    "are not recorded. The true contamination is wider than this."),
                }
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def save(ledger: Dict[str, Any], path: str = DEFAULT_PATH) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(ledger, handle, indent=1, sort_keys=True)
        handle.write("\n")


def seed(ledger: Dict[str, Any]) -> int:
    """Write the historical reads once. Never rewrites an existing entry."""
    known = {(r["dataset"], r["from_utc"], r["to_utc"], r["read_by"])
             for r in ledger.get("reads", [])}
    added = 0
    for entry in _seed_reads():
        key = (entry["dataset"], entry["from_utc"], entry["to_utc"], entry["read_by"])
        if key not in known:
            ledger.setdefault("reads", []).append(entry)
            added += 1
    ledger["seeded"] = True
    return added


def record_read(ledger: Dict[str, Any], *, dataset: str, from_utc: str,
                to_utc: str, read_by: str, purpose: str = "",
                result_seen: str = "") -> Dict[str, Any]:
    """Append a read. Append-only: this never edits or removes an entry."""
    _utc(from_utc), _utc(to_utc)  # validate
    entry = {"dataset": dataset, "from_utc": from_utc, "to_utc": to_utc,
             "read_by": read_by, "purpose": purpose, "result_seen": result_seen,
             "recorded_utc": _now()}
    ledger.setdefault("reads", []).append(entry)
    return entry


#: Set LEDGER_DISABLED=1 to suppress automatic recording. Tests set it so that
#: a test run does not fabricate thousands of "reads" that never informed a
#: hypothesis. Nothing else should ever set it, and `--status` reports when it
#: is on so a suppressed ledger can never look like a clean one.
DISABLE_ENV = "LEDGER_DISABLED"


def auto_record(*, dataset: str, from_utc: str, to_utc: str, read_by: str,
                purpose: str = "", path: str = DEFAULT_PATH) -> bool:
    """Record a read from inside a loader. Idempotent, and NEVER fatal.

    Called by `market_data.load_corpus`, the chokepoint every measurement tool
    in this tree routes through. Instrumenting there rather than in each tool is
    deliberate: the slice-57 contamination happened because a HUMAN was relied
    on to notice that a window had been read before, and humans stop noticing.
    An invariant that depends on anyone remembering is not an invariant.

    Two rules:

    * IDEMPOTENT — an identical (dataset, range, reader) tuple is recorded once.
      Re-running the same measurement is not a second look at the data.
    * NEVER FATAL — a read-only artifacts/ or a malformed ledger must not take
      down a measurement. It logs and returns False. Losing a ledger entry is
      bad; a measurement that dies at its last line is the bug this programme
      already fixed once in slice 77.
    """
    if os.environ.get(DISABLE_ENV) == "1":
        return False
    try:
        ledger = load(path)
        key = (dataset, from_utc, to_utc, read_by)
        for existing in ledger.get("reads", []):
            if (existing["dataset"], existing["from_utc"],
                    existing["to_utc"], existing["read_by"]) == key:
                return False
        record_read(ledger, dataset=dataset, from_utc=from_utc, to_utc=to_utc,
                    read_by=read_by, purpose=purpose,
                    result_seen="(auto-recorded at load; the result this read "
                                "produced is not known to the loader)")
        save(ledger, path)
        return True
    except Exception:  # noqa: BLE001 - a ledger write must never kill a run
        return False


def install(market_data_module=None, *, reader: str = "",
            path: str = DEFAULT_PATH) -> bool:
    """Attach this ledger to `market_data.READ_OBSERVER`. Idempotent.

    The arrow points UP: market_data knows nothing about the ledger, publishes
    raw epoch-ms facts to an observer that defaults to None, and this function
    is what sets it. INTEGRATION_MAP §1 stays satisfied and the recording still
    happens at the single chokepoint every measurement tool routes through.

    Every tool that calls `market_data.load_corpus` must call this first.
    `tests/test_slice78_ledger_is_automatic.py` fails the build if one does not,
    which is what turns "remember to record your reads" from a good intention
    into an invariant.
    """
    if market_data_module is None:
        import market_data as market_data_module  # noqa: PLC0415
    label = reader or os.path.basename(sys.argv[0] or "") or "unknown-reader"

    def _observe(symbol: str, first_ms: int, last_ms: int) -> None:
        auto_record(dataset=symbol,
                    from_utc=_iso_from_ms(first_ms),
                    to_utc=_iso_from_ms(last_ms),
                    read_by=label,
                    purpose="market_data.load_corpus",
                    path=path)

    market_data_module.READ_OBSERVER = _observe
    return True


def _iso_from_ms(ms: int) -> str:
    return dt.datetime.fromtimestamp(
        int(ms) / 1000.0, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def overlaps(ledger: Dict[str, Any], *, dataset: str, from_utc: str,
             to_utc: str) -> List[Dict[str, Any]]:
    start, end = _utc(from_utc), _utc(to_utc)
    hits = []
    for read in ledger.get("reads", []):
        if read["dataset"] != dataset:
            continue
        if _utc(read["from_utc"]) < end and start < _utc(read["to_utc"]):
            hits.append(read)
    return hits


def certify_untouched(ledger: Dict[str, Any], *, dataset: str, from_utc: str,
                      to_utc: str) -> Dict[str, Any]:
    """May this range serve as a holdout? Refuses on ANY overlapping prior read.

    A negative prior result contaminates exactly as much as a positive one: it
    tells you which hypothesis NOT to write next, which is a choice made with
    knowledge of the data.
    """
    hits = overlaps(ledger, dataset=dataset, from_utc=from_utc, to_utc=to_utc)
    return {
        "dataset": dataset, "from_utc": from_utc, "to_utc": to_utc,
        "certified_untouched": not hits,
        "overlapping_reads": hits,
        "verdict": "RESERVED_CLEAN" if not hits else "ALREADY_READ",
        "note": ("A holdout is a promise about IGNORANCE, not about chronology. "
                 "A later window that something has already looked at is not a "
                 "holdout, however honestly the cut was declared."),
    }


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--path", default=DEFAULT_PATH)
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--seed", action="store_true")
    parser.add_argument("--certify", metavar="DATASET")
    parser.add_argument("--from", dest="from_utc")
    parser.add_argument("--to", dest="to_utc")
    args = parser.parse_args(argv)

    ledger = load(args.path)
    if args.seed or not ledger.get("seeded"):
        added = seed(ledger)
        save(ledger, args.path)
        print(f"seeded {added} historical read(s) into {args.path}")

    if args.certify:
        if not (args.from_utc and args.to_utc):
            parser.error("--certify requires --from and --to")
        report = certify_untouched(ledger, dataset=args.certify,
                                   from_utc=args.from_utc, to_utc=args.to_utc)
        print(f"\n  dataset   {report['dataset']}")
        print(f"  window    {report['from_utc']} .. {report['to_utc']}")
        print(f"  VERDICT   {report['verdict']}\n")
        for hit in report["overlapping_reads"]:
            print(f"    read by {hit['read_by']}")
            print(f"      {hit['from_utc']} .. {hit['to_utc']}")
            print(f"      saw: {hit['result_seen'][:96]}")
        print(f"\n  {report['note']}")
        return 0 if report["certified_untouched"] else 1

    if args.status or True:
        reads = ledger.get("reads", [])
        print("=" * 74)
        print("DATA READ LEDGER — what has already been looked at")
        print("=" * 74)
        for read in reads:
            print(f"  {read['dataset']:32s} {read['from_utc'][:10]}..{read['to_utc'][:10]}")
            print(f"    by {read['read_by']}")
        print(f"\n  {len(reads)} recorded read(s). {ledger.get('floor_not_census','')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
