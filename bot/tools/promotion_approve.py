#!/usr/bin/env python3
"""Record a HUMAN approval of one promotion-gate item, outside the repository.

Reads the gate file, refuses unless the named item is a non-machine item whose
flag is a real JSON true, and writes the SHA-256 of that exact entry into an
approvals file (default: nothing; --out is required and should NOT be inside the
repository). The gate file is never modified. Run it on your own machine after
you have read the evidence; point PROMOTION_APPROVALS_FILE at the output.
Editing the entry later voids the approval.

    python3 tools/promotion_approve.py --key kill_switch_drill_recorded \\
        --out ~/promotion_approvals.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import promotion_gate as pg  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--gate", default=pg.GATE_PATH)
    ap.add_argument("--key", required=True)
    ap.add_argument("--out", required=True,
                    help="approvals file to create or extend (outside the repo)")
    args = ap.parse_args(argv)

    owners = {k: owner for k, _d, owner in pg.CHECKLIST_ITEMS}
    if args.key not in owners:
        print(f"refusing: {args.key!r} is not a gate item", file=sys.stderr)
        return 2
    if owners[args.key] == "machine":
        print(f"refusing: {args.key} is derived by the machine, not approved",
              file=sys.stderr)
        return 2
    payload = pg._read_gate(args.gate)
    entry = ((payload or {}).get("checklist") or {}).get(args.key)
    if not isinstance(entry, dict) or entry.get("complete") is not True:
        print(f"refusing: {args.key} is not marked complete in the gate file; "
              "approve only what you have read and found true", file=sys.stderr)
        return 2
    existing = pg._read_approvals(args.out) if os.path.exists(args.out) else {}
    existing[args.key] = pg.approval_digest(args.key, entry)
    tmp = args.out + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump({"schema": "promotion_approvals/1", "approved": existing},
                  handle, indent=2, sort_keys=True)
        handle.write("\n")
    os.replace(tmp, args.out)
    print(f"approved {args.key}: {existing[args.key]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
