#!/usr/bin/env python3
"""Human-only promotion of a scratch forward score into
artifacts/forward_shadow_current.json.

WHY THIS TOOL EXISTS
====================
`tools/daily_forward_refresh.py` scores Stage B's forward counters to a
scratch path and NEVER writes to `artifacts/`. `docs/human/NO_GLUE_OPS.md`
(items 5-6) and `tools/control_plane_tick.py`'s own "WHAT IT MUST NEVER DO"
header both say the promotion of that scratch score into
`artifacts/forward_shadow_current.json` is done by a human, by hand, and is
never automated. This tool is that explicit, audited step — a human runs it
themselves, on purpose, never from a script or a cron job.

`bot/scripts/stage_b_forward_accrual.sh` MUST NEVER invoke this tool.

FAIL CLOSED
===========
Without BOTH `--write` and `--i-am-human`, this tool prints a before/after
summary and REFUSES with a non-zero exit. It never writes
`forward_shadow_current.json` unless a human passes both flags explicitly.

USAGE
=====
    python3 tools/promote_forward_shadow.py --from /tmp/scratch/forward.json
    python3 tools/promote_forward_shadow.py --from /tmp/scratch/forward.json \\
        --i-am-human --write

`--from` takes the scratch score file a human already produced — either
`daily_forward_refresh.py --scratch DIR`'s `DIR/forward.json`, or a direct
`tools/slice76_forward_shadow.py --out FILE` run. Both are the same schema as
`artifacts/forward_shadow_current.json`, so promotion is a straight copy once
a human has looked at the diff and decided it is correct.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Dict

from stage_b_bars import resolve_closed_forward_bars

HERE = os.path.dirname(os.path.abspath(__file__))
BOT = os.path.dirname(HERE)
DEFAULT_SHADOW_PATH = os.path.join(BOT, "artifacts", "forward_shadow_current.json")

#: The counters Stage B accrual actually cares about; surfaced first in the
#: before/after summary. Everything else in the scratch report is copied
#: through verbatim on promotion (same schema as forward_shadow_current.json).
#: `closed_forward_bars` is resolved via `resolve_closed_forward_bars` below,
#: not read directly — the scorer nests it under `ceiling`/`forward_window`.
COUNTER_KEYS = ("forward_n_trades", "closed_forward_bars")


def load_json(path: str) -> Dict[str, Any]:
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def summarize(old: Dict[str, Any], new: Dict[str, Any]) -> str:
    lines = []
    for key in COUNTER_KEYS:
        if key == "closed_forward_bars":
            before: Any = resolve_closed_forward_bars(old)
            after: Any = resolve_closed_forward_bars(new)
        else:
            before = old.get(key)
            after = new.get(key)
        marker = "  (CHANGED)" if before != after else ""
        lines.append(f"  {key}: {before!r} -> {after!r}{marker}")
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--from", dest="from_path", required=True,
        help="scratch forward-score JSON to promote (daily_forward_refresh.py "
             "--scratch DIR's DIR/forward.json, or a slice76_forward_shadow.py "
             "--out file)")
    parser.add_argument(
        "--shadow-path", default=DEFAULT_SHADOW_PATH,
        help="artifacts/forward_shadow_current.json to update "
             "(override only for tests — never in normal use)")
    parser.add_argument("--write", action="store_true",
                        help="persist (default dry-run; requires --i-am-human too)")
    parser.add_argument(
        "--i-am-human", action="store_true", dest="i_am_human",
        help="confirms a human is running this by hand, not a script; "
             "required together with --write")
    args = parser.parse_args(argv)

    scratch = load_json(args.from_path)
    current = (load_json(args.shadow_path)
              if os.path.exists(args.shadow_path) else {})

    print(f"scratch score : {args.from_path}")
    print(f"shadow file   : {args.shadow_path}")
    print("counters:")
    print(summarize(current, scratch))

    if not (args.write and args.i_am_human):
        print(
            "\nREFUSED: promotion requires BOTH --write and --i-am-human. "
            "This is a human-only step — see docs/human/NO_GLUE_OPS.md #5-6 "
            "and tools/control_plane_tick.py's charter ('must NEVER promote "
            "scratch forward scores into artifacts/forward_shadow_current.json'). "
            "Never call this tool from stage_b_forward_accrual.sh or any other "
            "automated script.", file=sys.stderr)
        return 1

    with open(args.shadow_path, "w", encoding="utf-8") as handle:
        json.dump(scratch, handle, indent=2)
        handle.write("\n")
    print(f"\nPROMOTED by a human: wrote {args.shadow_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
