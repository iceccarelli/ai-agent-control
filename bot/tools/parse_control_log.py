#!/usr/bin/env python3
"""Turn a skill-test control log into a per-surrogate CSV.

Slice 23 needed the full per-surrogate output persisted, not just the summary.
This parses the log the run already produced rather than adding a dump flag to
``skill_test.py`` — the instrument is deliberately not edited during its own
confirmation run, and a log file answers the requirement just as well.

It is a reader, not a measurement: it computes nothing the log does not already
contain except the summary statistics, which it recomputes from the parsed rows
so a transcription error would show up as a mismatch against the log's own
VERDICT block.

    python3 tools/parse_control_log.py artifacts/foo.log artifacts/foo.csv
"""
from __future__ import annotations

import csv
import re
import sys

import numpy as np

ROW = re.compile(r"^\s{2}surrogate\s+(\d+):\s+percentile\s+([\d.]+)\s+\((\d+) trades\)")
SKIP = re.compile(r"^\s{2}surrogate\s+(\d+):\s+(?!percentile)(.*)$")


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 2:
        print(__doc__)
        return 2
    source, target = argv
    # Emitted in LOG ORDER, ranked and incomplete interleaved exactly as they
    # occurred. An incomplete surrogate keeps its index and its reason and is
    # never dropped: incompleteness is not random -- a surrogate is incomplete
    # when its own structure defeats the machinery -- so silently omitting one
    # would quietly select the easy ones.
    ordered, rows, incomplete = [], [], []
    for line in open(source):
        hit = ROW.match(line)
        if hit:
            row = (int(hit.group(1)), float(hit.group(2)), int(hit.group(3)))
            rows.append(row)
            ordered.append((row[0], f"{row[1]}", f"{row[2]}"))
            continue
        miss = SKIP.match(line)
        if miss:
            incomplete.append((int(miss.group(1)), miss.group(2).strip()))
            ordered.append((int(miss.group(1)), "", miss.group(2).strip()))

    with open(target, "w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["surrogate", "percentile", "trades"])
        writer.writerows(ordered)

    values = np.array([r[1] for r in rows], dtype=float)
    trades = np.array([r[2] for r in rows], dtype=float)
    standard_error = 28.87 / np.sqrt(values.size) if values.size else float("nan")
    print(f"source     : {source}")
    print(f"target     : {target}")
    print(f"ranked     : {values.size}    incomplete: {len(incomplete)}")
    print(f"median     : {np.median(values):.1f}")
    print(f"mean       : {values.mean():.1f}   sd {values.std(ddof=1):.1f}")
    print(f"worst      : {values.max():.1f}")
    print(f"uniformity : z = {(values.mean() - 50.0) / standard_error:+.2f}")
    print(f"SE(median) : {50.0 / np.sqrt(values.size):.1f} points")
    print(f"trades     : median {np.median(trades):.0f}  "
          f"min {trades.min():.0f}  max {trades.max():.0f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
