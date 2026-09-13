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
    """Every invariant, with three outcomes: holds, VIOLATED, or UNREADABLE.

    0037: the third one is the point. Run outside the virtualenv this printed
    "NO" for a gate it had simply failed to import, which reads as "the gate
    opened". A tool that cannot tell `False` from `cannot tell` is worse than
    no tool: it cries wolf on a missing dependency and it would hide a real
    violation behind the same word.
    """
    repo = os.path.abspath(repo)
    if repo not in sys.path:
        sys.path.insert(0, repo)
    report: Dict[str, Any] = {}

    def record(key: str, value_key: str, fn) -> None:
        try:
            value, holds = fn()
        except Exception as exc:  # noqa: BLE001 - unreadable, not violated
            report[value_key] = f"UNREADABLE: {exc}"
            report[key] = None
            report[_readable_key(key)] = False
            return
        report[value_key] = value
        report[key] = holds
        report[_readable_key(key)] = True

    record("gate_still_false", "gate_value", _gate)
    record("fund_abs_still_0_0001", "fund_abs", _fund_abs)
    record("cap_still_100", "cap", _cap)
    record("live_authorized_unassigned", "live_authorized_writes",
           lambda: _live_authorized(repo))

    checks = ("gate_still_false", "fund_abs_still_0_0001", "cap_still_100",
              "live_authorized_unassigned")
    report["violations"] = [k for k in checks if report[k] is False]
    report["unreadable"] = [k for k in checks if report[k] is None]
    report["ok"] = not report["violations"] and not report["unreadable"]
    return report


def _readable_key(key: str) -> str:
    return {"gate_still_false": "gate_readable",
            "fund_abs_still_0_0001": "fund_abs_readable",
            "cap_still_100": "cap_readable",
            "live_authorized_unassigned": "live_authorized_readable"}[key]


def _gate():
    import promotion_gate
    value = promotion_gate.promotion_gate_allows_live()
    return value, value is False


def _fund_abs():
    from signals import funding_carry_fade_v1 as a
    from signals import funding_carry_fade_btc_v1 as b
    return [a.FUND_ABS, b.FUND_ABS], (a.FUND_ABS == 0.0001
                                      and b.FUND_ABS == 0.0001)


def _cap():
    import shadow
    value = shadow.SHADOW_MAX_NOTIONAL_USD
    return value, float(value) == 100.0


def _live_authorized(repo: str):
    hits = live_authorized_assignments(repo)
    return hits, not hits


def main(argv: Optional[List[str]] = None) -> int:
    """0 = every invariant holds, 1 = one MOVED, 2 = one could not be read."""
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", default=ROOT)
    args = ap.parse_args(argv)
    r = check(args.repo)

    def mark(key: str) -> str:
        return {True: "yes", False: "NO", None: "UNREADABLE"}[r[key]]

    print(f"GATE STILL FALSE?:            {mark('gate_still_false'):<11s}"
          f"(promotion_gate_allows_live() = {r['gate_value']})")
    print(f"FUND_ABS STILL 0.0001?:       {mark('fund_abs_still_0_0001'):<11s}"
          f"({r['fund_abs']})")
    print(f"CAP STILL 100?:               {mark('cap_still_100'):<11s}"
          f"(SHADOW_MAX_NOTIONAL_USD = {r['cap']})")
    writes = r["live_authorized_writes"]
    print(f"LIVE_AUTHORIZED UNASSIGNED?:  "
          f"{mark('live_authorized_unassigned'):<11s}"
          + (f"({len(writes)} write(s)"
             + (": " + ", ".join(writes) if writes else "") + ")"
             if isinstance(writes, list) else f"({writes})"))

    if r["unreadable"]:
        print()
        print("UNREADABLE is not a violation: this interpreter could not "
              "import what the check reads.")
        print("Run it with the virtualenv the suite uses:")
        print("    . .venv/bin/activate && python3 bot/tools/session_tail.py")
        print("    (or: .venv/bin/python bot/tools/session_tail.py)")
        return 2
    return 1 if r["violations"] else 0


if __name__ == "__main__":
    sys.exit(main())
