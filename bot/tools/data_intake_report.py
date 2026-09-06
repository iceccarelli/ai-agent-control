"""Which corpora may Stage 1 legitimately be run against? Read-only.

    python3 tools/data_intake_report.py
    python3 tools/data_intake_report.py --json-out artifacts/slice32_data_intake_report.json

Prints a per-corpus eligibility table and writes the full report as JSON.

**Eligibility is not edge.** A corpus marked eligible is a set of bars a
measurement may legitimately be run *against*; it says nothing about whether any
signal times them. Timing-skill research remains CLOSED — see
`RESEARCH_CLOSE_STAGE1.md`. The BTC daily corpus is eligible, has 2,564 bars, and
produced an ABSENT reading.

Exit 0 when the scan completes. Eligibility is data, not a verdict on the tool —
a repository with no eligible corpus at all still exits 0, having said so.
Exit 1 only if the scan itself could not run.

This tool starts no measurement, touches no order path, and writes nothing under
`models/`.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

import data_contract as dc  # noqa: E402


def _table(records) -> str:
    header = ("path", "interval", "synthetic", "bars", "eligible")
    widths = (24, 9, 10, 9, 9)
    lines = ["  ".join(h.ljust(w) for h, w in zip(header, widths)),
             "  ".join("-" * w for w in widths)]
    for record in records:
        lines.append("  ".join([
            str(record["path"]).ljust(widths[0]),
            str(record["interval_label"]).ljust(widths[1]),
            str(record["is_synthetic"]).ljust(widths[2]),
            str(record["bar_count"]).ljust(widths[3]),
            str(record["eligible_for_stage1"]).ljust(widths[4]),
        ]))
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json-out", default="",
                        help="write the full report here as JSON")
    parser.add_argument("--repo-root", default=REPO)
    args = parser.parse_args(argv)

    try:
        report = dc.build_report(repo_root=args.repo_root)
    except Exception as exc:  # noqa: BLE001
        print(f"SCAN FAILED: {exc}", file=sys.stderr)
        return 1

    records = report["corpora"]
    print("=" * 78)
    print("DATA INTAKE REPORT — which corpora may Stage 1 be run against")
    print("=" * 78)
    print(f"  thresholds (frozen in EDGE.md 13b before any scan): "
          f"daily >= {dc.MIN_BARS_DAILY} bars, intraday >= {dc.MIN_BARS_INTRADAY}")
    print()
    print(_table(records))
    print()

    for record in records:
        if record["ineligible_reasons"]:
            print(f"  {record['path']} — INELIGIBLE")
            for reason in record["ineligible_reasons"]:
                print(f"      - {reason}")
    print()

    eligible = report["stage1_eligible_corpus_paths"]
    print(f"  eligible corpora        : {', '.join(eligible) if eligible else 'NONE'}")
    print(f"  multi-asset corpus      : {report['multi_asset_corpus_present']}")
    print()

    # Report both sides BY CORPUS PATH. The same ticker can now name a real file
    # and a generated one, so printing bare symbol names would leave a reader
    # unable to tell which ETH_USDT the line refers to.
    real_by_corpus = report.get("non_btc_real_symbols_by_corpus") or {}
    if real_by_corpus:
        print("  REAL non-BTC data (eligible for Stage 1):")
        for path, symbols in sorted(real_by_corpus.items()):
            print(f"      {path:<24} {', '.join(symbols)}")
    else:
        print("  REAL non-BTC data       : NONE")

    synthetic_by_corpus = report.get("non_btc_synthetic_symbols_by_corpus") or {}
    if synthetic_by_corpus:
        print()
        print("  SYNTHETIC non-BTC data (NEVER Stage 1 evidence):")
        for path, symbols in sorted(synthetic_by_corpus.items()):
            print(f"      {path:<24} {', '.join(symbols)}")
        overlap = sorted(set(report["non_btc_synthetic_symbols_present"])
                         & set(report["non_btc_real_symbols"]))
        if overlap:
            print()
            print(f"      NOTE: {', '.join(overlap)} appear in BOTH lists. The same")
            print("      ticker exists as real exchange data and as generated data.")
            print("      Select a corpus by PATH, never by symbol name.")
    print()
    print("  Eligibility is NOT edge. Timing-skill research remains CLOSED.")
    print("  No signal has cleared Stage 1. See RESEARCH_CLOSE_STAGE1.md.")

    if args.json_out:
        directory = os.path.dirname(args.json_out)
        if directory:
            os.makedirs(directory, exist_ok=True)
        with open(args.json_out, "w", encoding="utf-8") as handle:
            json.dump(report, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
        print(f"\n  report written to {args.json_out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
