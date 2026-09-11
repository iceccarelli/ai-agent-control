#!/usr/bin/env python3
"""The four invariants a session ends by quoting — answered from the tree.

    GATE STILL FALSE?            promotion_gate.promotion_gate_allows_live()
    FUND_ABS STILL 0.0001?       signals.funding_carry_fade_v1.FUND_ABS (+ btc_v1)
    CAP STILL 100?               shadow.SHADOW_MAX_NOTIONAL_USD
    LIVE_AUTHORIZED UNASSIGNED?  no module outside tests/ assigns an attribute
                                 named live_authorized / LIVE_AUTHORIZED, or
                                 setattr()s one

Exit 0 when all four hold, 1 otherwise. Read-only: imports, reads, prints.
Reading the flag into a local (`live_authorized = bool(getattr(...))`) is a
read, not an assignment, and is not flagged.

    python3 tools/session_tail.py
"""
from __future__ import annotations

import argparse
import ast
import os
import sys
from typing import Any, Dict, List, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

_FIELD = {"live_authorized", "LIVE_AUTHORIZED"}


def _attr_targets(node: ast.AST) -> List[ast.AST]:
    if isinstance(node, ast.Assign):
        return list(node.targets)
    if isinstance(node, ast.AugAssign):
        return [node.target]
    if isinstance(node, ast.AnnAssign) and node.value is not None:
        return [node.target]
    return []


def live_authorized_assignments(root: str) -> List[str]:
    """Every place outside tests/ that WRITES the flag. Empty is the answer."""
    hits: List[str] = []
    for folder, dirs, names in os.walk(root):
        dirs[:] = [d for d in dirs if d not in ("tests", "__pycache__", ".venv")]
        for name in sorted(names):
            if not name.endswith(".py"):
                continue
            path = os.path.join(folder, name)
            try:
                with open(path, encoding="utf-8") as fh:
                    tree = ast.parse(fh.read())
            except (SyntaxError, UnicodeDecodeError):
                continue
            rel = os.path.relpath(path, root)
            for node in ast.walk(tree):
                for target in _attr_targets(node):
                    if isinstance(target, ast.Attribute) and target.attr in _FIELD:
                        hits.append(f"{rel}:{node.lineno}")
                if (isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Name)
                        and node.func.id == "setattr"
                        and len(node.args) >= 2
                        and isinstance(node.args[1], ast.Constant)
                        and node.args[1].value in _FIELD):
                    hits.append(f"{rel}:{node.lineno}")
    return hits


def check(repo: str = ROOT) -> Dict[str, Any]:
    repo = os.path.abspath(repo)
    if repo not in sys.path:
        sys.path.insert(0, repo)
    report: Dict[str, Any] = {}

    try:
        import promotion_gate
        report["gate_value"] = promotion_gate.promotion_gate_allows_live()
        report["gate_still_false"] = report["gate_value"] is False
    except Exception as exc:  # noqa: BLE001 - an unreadable gate is a failure
        report["gate_value"] = f"UNREADABLE: {exc!r}"
        report["gate_still_false"] = False

    try:
        from signals import funding_carry_fade_v1 as a
        from signals import funding_carry_fade_btc_v1 as b
        report["fund_abs"] = [a.FUND_ABS, b.FUND_ABS]
        report["fund_abs_still_0_0001"] = (a.FUND_ABS == 0.0001
                                           and b.FUND_ABS == 0.0001)
    except Exception as exc:  # noqa: BLE001
        report["fund_abs"] = f"UNREADABLE: {exc!r}"
        report["fund_abs_still_0_0001"] = False

    try:
        import shadow
        report["cap"] = shadow.SHADOW_MAX_NOTIONAL_USD
        report["cap_still_100"] = float(shadow.SHADOW_MAX_NOTIONAL_USD) == 100.0
    except Exception as exc:  # noqa: BLE001
        report["cap"] = f"UNREADABLE: {exc!r}"
        report["cap_still_100"] = False

    hits = live_authorized_assignments(repo)
    report["live_authorized_writes"] = hits
    report["live_authorized_unassigned"] = not hits

    report["ok"] = all(report[k] is True for k in (
        "gate_still_false", "fund_abs_still_0_0001", "cap_still_100",
        "live_authorized_unassigned"))
    return report


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", default=ROOT)
    args = ap.parse_args(argv)
    r = check(args.repo)
    yn = lambda v: "yes" if v is True else "NO"  # noqa: E731
    print(f"GATE STILL FALSE?:            {yn(r['gate_still_false'])}"
          f"   (promotion_gate_allows_live() = {r['gate_value']})")
    print(f"FUND_ABS STILL 0.0001?:       {yn(r['fund_abs_still_0_0001'])}"
          f"   ({r['fund_abs']})")
    print(f"CAP STILL 100?:               {yn(r['cap_still_100'])}"
          f"   (SHADOW_MAX_NOTIONAL_USD = {r['cap']})")
    print(f"LIVE_AUTHORIZED UNASSIGNED?:  {yn(r['live_authorized_unassigned'])}"
          f"   ({len(r['live_authorized_writes'])} write(s)"
          f"{': ' + ', '.join(r['live_authorized_writes']) if r['live_authorized_writes'] else ''})")
    return 0 if r["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
