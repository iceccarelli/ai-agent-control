#!/usr/bin/env python3
"""Read the tables `fly machine list` / `fly volumes list` / `fly status` print.

Offline and read-only: it parses text on stdin and touches nothing. It exists
because the first deploy check counted machines with `grep -cE '^[0-9a-f]{14}'`
and reported 0 for a healthy app: the table rows start with a space, so the ID
never sits at the start of the line (tests/test_fly_machines.py reproduces that
on the captured output).

    fly machine list -a carry-drill | python3 tools/fly_machines.py count
    fly machine list -a carry-drill | python3 tools/fly_machines.py states
    fly volumes list -a carry-drill | python3 tools/fly_machines.py count --where NAME=drill_runs

A table is a header row whose cells are separated by `│` (or `|`), then rows
with the same number of cells. Lines that are not rows (the "N machines have
been retrieved" banner, the UI link, blank lines) are ignored. The header is
required: text with no recognisable table counts as 0 rows, never as a guess.
"""
from __future__ import annotations

import argparse
import sys
from typing import Dict, List, Optional, Sequence

SEPARATORS = ("│", "|")


def _cells(line: str) -> Optional[List[str]]:
    for sep in SEPARATORS:
        if sep in line:
            return [c.strip() for c in line.split(sep)]
    return None


def parse_table(text: str, *, header_key: str = "ID") -> List[Dict[str, str]]:
    """Rows of the first table whose header has a `header_key` column.

    Only rows with exactly as many cells as the header are returned, so a
    wrapped or truncated line cannot become a half-row.
    """
    header: Optional[List[str]] = None
    rows: List[Dict[str, str]] = []
    for line in text.splitlines():
        cells = _cells(line)
        if cells is None:
            if header is not None and not line.strip():
                break                     # the table ended
            continue
        if header is None:
            if header_key in cells:
                header = cells
            continue
        if len(cells) != len(header) or not any(cells):
            continue
        rows.append(dict(zip(header, cells)))
    return rows


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("what", choices=["count", "states", "ids"])
    ap.add_argument("--header", default="ID", help="a column name that marks the header row")
    ap.add_argument("--where", action="append", default=[], metavar="COL=VALUE",
                    help="keep only rows whose column equals the value")
    args = ap.parse_args(argv)
    rows = parse_table(sys.stdin.read(), header_key=args.header)
    for clause in args.where:
        col, _, value = clause.partition("=")
        rows = [r for r in rows if r.get(col) == value]
    if args.what == "count":
        print(len(rows))
    elif args.what == "states":
        for r in rows:
            print(r.get("STATE", ""))
    else:
        for r in rows:
            print(r.get("ID", ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
