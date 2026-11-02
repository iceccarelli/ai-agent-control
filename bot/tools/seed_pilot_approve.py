#!/usr/bin/env python3
"""Record the OWNER's approval of one seed-pilot charter, outside the repository.

Two steps, both offline, neither touches a venue, a database or the charter:

    python3 tools/seed_pilot_approve.py --charter ~/seed_charter.json \\
        --out ~/promotion_approvals.json
        # prints every field the owner would be signing and the charter's
        # SHA-256; writes nothing

    python3 tools/seed_pilot_approve.py --charter ~/seed_charter.json \\
        --out ~/promotion_approvals.json --confirm <the full SHA-256 printed>
        # re-reads the charter, refuses unless it still hashes to that digest,
        # is well formed and unexpired, then records the digest

The approval is the SAME mechanism as `tools/promotion_approve.py` (a digest in
a private file outside the repository, point `seed_pilot_io.load_approval` at
it); editing the charter afterwards voids it. Approving authorises a bounded
pilot's DECISION RULE to say ALLOW; it arms nothing, allocates nothing and
promotes nothing, and the pilot is wired to nothing.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import promotion_gate as pg        # noqa: E402
import seed_pilot as sp            # noqa: E402
import seed_pilot_io as sio        # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--charter", required=True)
    ap.add_argument("--out", required=True,
                    help="approvals file to create or extend (outside the repo)")
    ap.add_argument("--confirm", default="",
                    help="the full SHA-256 this tool printed for the charter")
    args = ap.parse_args(argv)

    charter, problems = sio.load_charter(args.charter)
    if charter is None:
        print("refusing: " + "; ".join(problems), file=sys.stderr)
        return 2
    now = time.time()
    if not charter.not_before_s <= now < charter.expires_at_s:
        print("refusing: the charter is not within its validity window now",
              file=sys.stderr)
        return 2
    digest = sp.charter_digest(charter)
    if not args.confirm:
        print(json.dumps(sp.charter_to_dict(charter), indent=2, sort_keys=True))
        print(f"digest: {digest}")
        print("nothing written. Re-run with --confirm <that digest> to approve.")
        return 0
    if args.confirm != digest:
        print("refusing: --confirm does not match the charter's digest; it "
              "changed since you read it, or you did not read it", file=sys.stderr)
        return 2
    out_real, problem = pg._approvals_location_problem(args.out)
    if problem is None and not os.path.isdir(os.path.dirname(out_real)):
        problem = f"the directory of --out {args.out!r} does not exist"
    existing = {}
    if problem is None and os.path.exists(out_real):
        existing, problem = pg._read_approvals(args.out)
    if problem is not None:
        print(f"refusing: {problem}", file=sys.stderr)
        return 2
    existing = dict(existing)
    existing[sp.approval_key(charter.charter_id)] = digest
    tmp = out_real + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump({"schema": "promotion_approvals/1", "approved": existing},
                      handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, out_real)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    print(f"approved {sp.approval_key(charter.charter_id)}: {digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
