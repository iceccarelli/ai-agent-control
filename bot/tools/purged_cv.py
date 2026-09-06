#!/usr/bin/env python3
"""Purged, embargoed, combinatorial cross-validation. Replaces one OOS number
with a DISTRIBUTION.

WHY THIS EXISTS
===============
`funding_carry_fade_btc_v1` cleared on ONE out-of-sample cut: the late half of
the BTC daily series, n=41, M1/M2 = 95.13/96.0. One cut yields one number, and
one number carries no error bar. It cleared, then lost money forward twice.

A single split is also fragile in a way that invites an honest person to fool
themselves: move `t_mid` by ten bars and the count, the trade set and the
percentile all move. This programme handled that risk the strongest way a
single-split design allows — the fold file forbids moving `t_mid` after the
counts exist, and that clause was honoured. But forbidding an adjustment is not
the same as knowing how much the answer would have moved.

CPCV answers that. It scores MANY train/test partitions and reports the spread.
A rule with a real edge clears in most partitions. A rule that found one lucky
cut clears in a few and the distribution says so out loud.

LEAKAGE CONTROL
===============
Financial labels overlap in time. A label at bar t is resolved using bars up to
t + HORIZON, so a naive split leaves training rows whose OUTCOMES live inside
the test window.

Two defences, per Lopez de Prado (2018), ch. 7:

  PURGE   drop training rows whose label horizon reaches into the test window.
          Width = HORIZON (5 bars here).
  EMBARGO drop training rows immediately AFTER the test window, because serial
          correlation lets the test period leak forward into them.
          Width = HORIZON + LOCKUP (6 bars here) by default.

Both are derived from the frozen constants. They are not tunable knobs and this
module never fits them.

WHAT THIS MODULE DOES NOT DO
============================
It does not re-score Stage-1. It does not touch FUND_ABS, the join, the barrier,
the folds, the caps or the schedule. It splits index ranges and reports which
rows may be trained on. It writes no field any gate reads.

CPCV DOES NOT REPAIR A CONTAMINATED HOLDOUT. If a range has already been read,
partitioning it more cleverly does not restore ignorance. Use
tools/reserved_holdout.py first; this second.

    python3 tools/purged_cv.py --demo
"""
from __future__ import annotations

import argparse
import itertools
import sys
from typing import Any, Dict, Iterator, List, Sequence, Tuple

HORIZON = 5   # frozen: bars a label needs to resolve
LOCKUP = 1    # frozen: bars before a new entry is permitted


def purge_and_embargo(n: int, test_indices: Sequence[int], *,
                      horizon: int = HORIZON,
                      embargo: int | None = None) -> List[int]:
    """Training indices left after purging and embargoing around `test_indices`.

    Purge removes any training row whose label window [i, i+horizon] touches the
    test block. Embargo removes rows immediately after the block.
    """
    if embargo is None:
        embargo = horizon + LOCKUP
    if not test_indices:
        return list(range(n))
    test = set(test_indices)
    lo, hi = min(test_indices), max(test_indices)
    train = []
    for i in range(n):
        if i in test:
            continue
        if i + horizon >= lo and i <= hi:      # label reaches into the block
            continue
        if hi < i <= hi + embargo:             # inside the embargo
            continue
        train.append(i)
    return train


def cpcv_paths(n: int, *, n_groups: int = 6, n_test_groups: int = 2,
               horizon: int = HORIZON,
               embargo: int | None = None) -> Iterator[Dict[str, Any]]:
    """Yield every combinatorial partition of `n` rows into train/test.

    With N groups and k held out per split there are C(N, k) partitions, so 6
    and 2 give 15 — fifteen out-of-sample readings instead of one.
    """
    if n_groups < 2 or not 1 <= n_test_groups < n_groups:
        raise ValueError("need n_groups >= 2 and 1 <= n_test_groups < n_groups")
    edges = [round(i * n / n_groups) for i in range(n_groups + 1)]
    groups = [list(range(edges[i], edges[i + 1])) for i in range(n_groups)]
    for combo in itertools.combinations(range(n_groups), n_test_groups):
        test: List[int] = []
        for g in combo:
            test.extend(groups[g])
        if not test:
            continue
        train = purge_and_embargo(n, test, horizon=horizon, embargo=embargo)
        yield {"test_groups": combo, "test": test, "train": train,
               "n_test": len(test), "n_train": len(train),
               "dropped_to_leakage": n - len(test) - len(train)}


def summarise(n: int, **kwargs: Any) -> Dict[str, Any]:
    paths = list(cpcv_paths(n, **kwargs))
    dropped = [p["dropped_to_leakage"] for p in paths]
    return {
        "n_rows": n,
        "n_paths": len(paths),
        "horizon_purge": kwargs.get("horizon", HORIZON),
        "embargo": kwargs.get("embargo") or (HORIZON + LOCKUP),
        "mean_train": sum(p["n_train"] for p in paths) / len(paths),
        "mean_test": sum(p["n_test"] for p in paths) / len(paths),
        "mean_dropped_to_leakage": sum(dropped) / len(dropped),
        "leakage_guard_is_active": all(d > 0 for d in dropped),
        "note": ("Every path drops rows to purge and embargo. A path that drops "
                 "none is a path where the guard did not engage — check horizon."),
        "re_scored_anything": False,
        "is_stage1_evidence": False,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--rows", type=int, default=1476,
                        help="default 1476 = the BTC linear daily corpus")
    parser.add_argument("--groups", type=int, default=6)
    parser.add_argument("--test-groups", type=int, default=2)
    parser.add_argument("--demo", action="store_true")
    args = parser.parse_args(argv)

    report = summarise(args.rows, n_groups=args.groups,
                       n_test_groups=args.test_groups)
    print("=" * 74)
    print("PURGED / EMBARGOED COMBINATORIAL CV")
    print("=" * 74)
    for key in ("n_rows", "n_paths", "horizon_purge", "embargo",
                "mean_train", "mean_test", "mean_dropped_to_leakage",
                "leakage_guard_is_active"):
        print(f"  {key:26s} {report[key]}")
    print()
    print("  Slice 57 used ONE split of these rows and produced ONE number.")
    print(f"  This design produces {report['n_paths']} out-of-sample readings, so the")
    print("  question stops being 'did it clear?' and becomes 'how often, and")
    print("  how far apart were the readings?'")
    print()
    print("  CPCV does NOT repair a contaminated holdout. Run")
    print("  tools/reserved_holdout.py first.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
