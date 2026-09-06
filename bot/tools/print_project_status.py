"""Print this project's mode as JSON. Read-only.

    python3 tools/print_project_status.py
    python3 tools/print_project_status.py --artifact-dir artifacts

Answers, from real state rather than from a stored opinion:

* is timing-skill research open or closed?
* has any signal actually cleared Stage 1?
* is this process armed for live?
* is a model promoted?

Every field is derived at call time. `cleared_edge_signal` comes from the
refusing artefact reader in `project_status.py`, which returns a name only when a
summary says `EDGE_EVIDENCE_POSITIVE` with M1 and M2 both at or above 95.0. Every
artefact in this repository fails that test, so the answer today is `null` — and
that is correct, not broken.

Exit code is 0 when the snapshot is coherent and 1 when it contradicts itself
(for example: a cleared edge naming a signal that measurement recorded as
ABSENT). The check is a tripwire, not a gate: it should be impossible to fail.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

import project_status as ps  # noqa: E402


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact-dir", default=os.path.join(REPO, "artifacts"))
    parser.add_argument("--quiet", action="store_true",
                        help="JSON only, no trailing commentary on stderr")
    args = parser.parse_args(argv)

    try:
        import config as _config
        cfg = _config.load({})
    except Exception:  # noqa: BLE001
        cfg = None

    status = ps.current(cfg, artifact_dir=args.artifact_dir, repo_root=REPO)
    print(json.dumps(status.as_dict(), indent=2, ensure_ascii=False))

    problems = []
    advisories = []
    if status.cleared_edge_signal in ps.ABSENT_SIGNALS:
        problems.append(
            f"cleared_edge_signal is {status.cleared_edge_signal!r}, which "
            "measurement recorded as ABSENT")
    if status.execution_mode == "live" and not status.live_authorized:
        problems.append("execution_mode is live but live_authorized is false")
    # SLICE 57 — this clause fired for the first time, and its formulation
    # turned out to be too coarse rather than its intent being wrong.
    #
    # It was written when a cleared edge was impossible, so "cleared edge AND a
    # NO EDGE CLAIM line" could only ever mean a contradiction. It now has to
    # separate two statements that are BOTH true:
    #
    #   * `no_edge_claim` is about THIS EXECUTION SHELL. It says the process
    #     you are running executes no edge. That remains true: nothing wires
    #     `funding_carry_fade_btc_v1` into the engine, the configuration is
    #     strategy-neutral, and no code path consults `cleared_edge_signal`
    #     when deciding to trade;
    #   * `cleared_edge_signal` is about the RESEARCH RECORD. It says one
    #     product passed one pre-declared Stage-1 gate.
    #
    # The lie the clause exists to prevent is the shell claiming NO EDGE while
    # actually acting on one. That is what is checked now. The NO EDGE CLAIM
    # line itself is NOT weakened, removed or made conditional — an operator
    # reading a session surface sees exactly the string they saw before.
    if status.cleared_edge_signal and (status.execution_mode == "live"
                                       or status.live_authorized):
        problems.append(
            "a cleared edge is claimed while the shell is armed to act on it "
            "(execution_mode="
            f"{status.execution_mode!r}, live_authorized="
            f"{status.live_authorized!r})")

    if status.no_edge_claim and status.cleared_edge_signal:
        advisories.append(
            f"cleared_edge_signal is {status.cleared_edge_signal!r} AND the "
            "NO EDGE CLAIM line is set. Both are true and neither is "
            "suppressed: research has cleared one product's Stage-1 gate, and "
            "THIS SHELL still executes no edge — nothing wires that signal "
            "into the engine and no trading path consults the field. Wiring it "
            "up is a human decision that has not been taken.")

    for advisory in advisories:
        print(f"ADVISORY: {advisory}", file=sys.stderr)

    if problems:
        for problem in problems:
            print(f"INCOHERENT: {problem}", file=sys.stderr)
        return 1

    if not args.quiet:
        print(status.summary_line(), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
