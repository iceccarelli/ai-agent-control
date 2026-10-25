#!/usr/bin/env python3
"""A short, honest text summary of an `evidence_reconcile.py --json` report, for
a job summary. Reads one file, prints Markdown-free lines, sends nothing.

It states what the reconciliation says about the CAPTURED RECORDS and repeats,
every time, what it does not say.
"""
from __future__ import annotations

import json
import sys
from typing import Any, List, Optional, Sequence


UNREADABLE = "reconciliation report missing or unreadable: nothing is claimed"


def summarise(report: Any) -> List[str]:
    """Never raises: a report this module cannot read claims nothing."""
    if not isinstance(report, dict) or report.get("schema") != "evidence_reconciliation/1":
        return [UNREADABLE]
    try:
        return _summarise(report)
    except Exception:                                          # noqa: BLE001
        return [UNREADABLE]


def _summarise(report: dict) -> List[str]:
    lines: List[str] = []
    for leg in report.get("carry_legs", []):
        lines.append(f"leg {leg.get('claim_id')}: {leg.get('provenance')}"
                     f"{' ' + ','.join(leg['issues']) if leg.get('issues') else ''}")
    for cyc in report.get("carry_cycle", []):
        lines.append(f"cycle: {cyc.get('provenance')} resolved={cyc.get('resolved')}"
                     f"{' issues=' + ','.join(cyc['issues']) if cyc.get('issues') else ''}")
        econ = cyc.get("economics") or {}
        if "net_usdt" in econ:
            lines.append(
                f"economics (testnet USDT): entry {econ['entry_price']} exit "
                f"{econ['exit_price']} qty {econ['qty']} gross "
                f"{econ['perp_gross_usdt']:+.6f} fees {econ['fees_usdt']:.6f} "
                f"({econ['fee_bps_of_entry_notional']:.2f} bps) funding "
                f"{econ['funding_usdt']:+.6f} over {econ['settlements']} "
                f"settlement(s), hold {econ['hold_ms'] / 1000:.0f} s, "
                f"NET {econ['net_usdt']:+.6f}")
    for f in report.get("funding", []):
        lines.append(f"funding {f.get('claim_id')}: {f.get('provenance')}"
                     f"{' ' + ','.join(f['issues']) if f.get('issues') else ''}")
    lines.append(f"pnl_claim_allowed: {report.get('pnl_claim_allowed')}")
    lines.append("This says the captured testnet rows agree with the drill's "
                 "books. It is one round trip on testnet: it says nothing "
                 "about edge, and spot BTC's own price change is not included.")
    return lines


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1:
        print("usage: recon_summary.py REPORT.json", file=sys.stderr)
        return 3
    try:
        with open(args[0], encoding="utf-8") as handle:
            report = json.load(handle)
    except (OSError, ValueError):
        report = None
    print("\n".join(summarise(report)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
