#!/usr/bin/env python3
"""The off-path reviewer: reads artifacts, emits a verdict, touches nothing.

WHAT THIS IS
============
`docs/human/AGENT_CONTROL_PLANE.md` gives the reviewer one job: read the
evidence and say whether Stage B may proceed. It is a READER. It cannot place
an order, clear the kill switch, set `allows_live`, raise a cap or write the
production state database, and the way that is guaranteed is not a promise in
a docstring — it is that this file imports the standard library and nothing
else. There is no bot module in its import closure, so there is no path from
here to the venue, not even a transitive one.

`tests/test_control_plane_boundary.py` asserts that by AST, and it was written
before this file existed precisely so the contract could not be forgotten.

THE MODEL MAY NOT MAKE THE VERDICT MORE PERMISSIVE
==================================================
`allows_progress` and `blockers` are computed HERE, deterministically, from
the artifacts on disk. When `--xai` is used and a key is present, the model's
answer is appended to `next_actions` prefixed `ADVISORY` and changes nothing
else. A language model cannot clear a blocker in this design, which is the
only arrangement under which it is safe to let one near a promotion gate at
all.

FAIL CLOSED
===========
A missing or unreadable input is a BLOCKER, not a shrug. A reviewer that
returns "looks fine" because it could not find the evidence is worse than no
reviewer.

KEYS
====
`XAI_API_KEY` from the environment, and never anywhere else. It is never
printed, never written to the verdict, never passed to a subprocess and never
logged. If it is unset the tool runs the local rules, records
`key_status="missing"`, and exits 0 — so an unkeyed host still gets a verdict.

    tools/reviewer_verdict.py --dry-run --out /tmp/verdict.json
    tools/reviewer_verdict.py --xai          # only reaches the network if keyed

The output file is left untouched when the finding is identical and only the
timestamp would have moved — the default path is tracked, and a runbook step
that dirties git every time you follow it is a runbook step people stop
following. `--stamp` forces the write.

Exit codes: 0 verdict produced; 2 an input was unreadable AND --strict was
given. The verdict itself is still emitted in both cases.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from typing import Any, Dict, List, Optional, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)

#: Written into the repository, because `artifacts/` is tracked and a verdict
#: nobody can diff is a verdict nobody can audit.
DEFAULT_OUT = os.path.join("artifacts", "reviewer_verdict.json")

FORWARD = os.path.join("artifacts", "forward_shadow_current.json")
DRILL = os.path.join("artifacts", "kill_switch_drill.json")
CHARTER = os.path.join("docs", "human", "AGENT_CONTROL_PLANE.md")
#: THE gate, not the newest file that looks like one. `artifacts/` holds
#: slice59 through slice76 of these, and the highest number is NOT the current
#: state: 60-76 are per-slice SNAPSHOTS frozen at 2 of 8, while the human
#: signature that took the kill-switch item to complete landed on slice59.
#: Globbing for the highest number read a frozen snapshot and reported a
#: signed gate item as incomplete — a false blocker aimed at a human's time.
#: This mirrors `promotion_gate.GATE_PATH`; the test asserts they agree.
GATE = os.path.join("artifacts", "slice59_promotion_gate.json")

#: Overridable because a model id is a fact about a vendor's catalogue, not
#: about this repository, and hardcoding one makes this file wrong on the day
#: they rename it.
XAI_MODEL = os.environ.get("XAI_MODEL", "grok-4-latest")
XAI_URL = "https://api.x.ai/v1/chat/completions"


# --------------------------------------------------------------- reading ---

def _read_json(repo: str, rel: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    path = os.path.join(repo, rel)
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle), None
    except FileNotFoundError:
        return None, f"input missing: {rel}"
    except (OSError, ValueError) as exc:
        return None, f"input unreadable: {rel} ({type(exc).__name__})"


def gate_path(repo: str) -> str:
    """The gate the RUNTIME reads, which is the only one that means anything.

    An earlier version globbed `slice*_promotion_gate.json` and took the
    highest number. That is a reasonable-sounding rule and it is wrong here:
    the numbered files are historical snapshots, so the newest one holds the
    OLDEST state for any item a human has since signed. It read slice76,
    frozen at 2 of 8, and announced that a completed kill-switch drill was
    incomplete.
    """
    return os.path.join(repo, GATE)


# ----------------------------------------------------------------- rules ---

def evaluate(repo: str) -> Dict[str, Any]:
    """Deterministic from the artifacts. No network, no model, no randomness."""
    blockers: List[str] = []
    next_actions: List[str] = []

    forward, err = _read_json(repo, FORWARD)
    if err:
        blockers.append(err)
    drill, err = _read_json(repo, DRILL)
    if err:
        blockers.append(err)

    gate, err = _read_json(repo, GATE)
    if err:
        blockers.append(err)

    if not os.path.isfile(os.path.join(repo, CHARTER)):
        blockers.append(f"input missing: {CHARTER}")

    # -- Stage B accrual ----------------------------------------------------
    n_trades = int((forward or {}).get("forward_n_trades") or 0)
    bars = int((forward or {}).get("closed_forward_bars") or 0)
    need_trades = 20
    need_days = 180
    if gate:
        need_trades = int(gate.get("min_forward_trades") or need_trades)
        need_days = int(gate.get("min_forward_days") or need_days)

    if n_trades < need_trades:
        next_actions.append(
            f"accrue closed forward trades: {n_trades} of {need_trades}")
    if bars < need_days:
        next_actions.append(f"accrue forward days: {bars} of {need_days}")

    # -- the gate -----------------------------------------------------------
    checklist = (gate or {}).get("checklist") or {}
    if checklist:
        complete = {k: bool(v.get("complete")) for k, v in checklist.items()
                    if isinstance(v, dict)}
        # "if allows_live true -> blocker". A reviewer never ratifies live.
        if complete and all(complete.values()):
            blockers.append(
                "gate reports every checklist item complete; a human must "
                "confirm live arming — a reviewer may not")
        if not complete.get("kill_switch_drill_recorded", False):
            blockers.append(
                "kill_switch_drill_recorded is not complete on the gate")
        for name, done in sorted(complete.items()):
            if not done:
                owner = (checklist.get(name) or {}).get("owner", "unknown")
                next_actions.append(f"gate item open ({owner}): {name}")
    else:
        blockers.append("gate checklist unreadable")

    # -- the drill evidence itself -----------------------------------------
    if drill:
        if drill.get("switch_engaged_at_exit") is True:
            blockers.append("kill switch was left ENGAGED at drill exit")
        if drill.get("production_db_untouched") is False:
            blockers.append("drill reports the production database was touched")
        if drill.get("all_steps_as_scripted") is False:
            blockers.append("drill reports steps that did not behave as scripted")

    return {
        "allows_progress": not blockers,
        "blockers": blockers,
        "stage_b": {
            "forward_n_trades": n_trades,
            "of_20": need_trades,
            "closed_forward_bars": bars,
        },
        "risk": {"allows_live_must_be_false": True},
        "next_actions": next_actions,
    }


# -------------------------------------------------------------- adapters ---
# One interface, two implementations, both ADVISORY. Neither can alter
# `allows_progress` or `blockers`; see the module docstring.

def _xai_advisory(prompt: str, *, timeout: float = 30.0) -> Optional[str]:
    """Only ever called when XAI_API_KEY is set. Never logs the key."""
    key = os.environ.get("XAI_API_KEY")
    if not key:
        return None
    import urllib.error                               # noqa: PLC0415
    import urllib.request                             # noqa: PLC0415

    body = json.dumps({
        "model": XAI_MODEL,
        "messages": [
            {"role": "system",
             "content": "You review trading-system evidence. You cannot "
                        "authorise anything. Reply with at most three short "
                        "bullet points naming risks in the evidence."},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0,
    }).encode("utf-8")
    request = urllib.request.Request(
        XAI_URL, data=body,
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {key}"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
        return payload["choices"][0]["message"]["content"].strip()
    except Exception as exc:                          # noqa: BLE001
        # The exception text is NOT included: a urllib error can echo the
        # request headers, and the key lives in a header.
        return f"(xai adapter failed: {type(exc).__name__})"


def _ollama_advisory(prompt: str, *, timeout: float = 30.0) -> Optional[str]:
    """STUB. Default off, and deliberately not implemented.

    The charter puts Ollama behind the same off-path contract as Grok, so the
    seam exists here. Writing an HTTP client for it before anyone has asked
    for one would be speculative framework code.
    """
    return "(ollama adapter is a stub: not implemented)"


ADAPTERS = {"xai": _xai_advisory, "ollama": _ollama_advisory}


def _prompt(verdict: Dict[str, Any]) -> str:
    return ("Evidence summary (authoritative, computed locally):\n"
            + json.dumps({k: verdict[k] for k in
                          ("stage_b", "blockers", "next_actions")}, indent=1))


# ------------------------------------------------------------------ main ---

def build(repo: str, *, adapter: Optional[str] = None) -> Dict[str, Any]:
    verdict = evaluate(repo)
    key_status = "present" if os.environ.get("XAI_API_KEY") else "missing"
    model = "none (local rules only)"

    if adapter and key_status == "present":
        advisory = ADAPTERS[adapter](_prompt(verdict))
        if advisory:
            model = XAI_MODEL if adapter == "xai" else adapter
            for line in advisory.splitlines():
                line = line.strip()
                if line:
                    verdict["next_actions"].append(f"ADVISORY ({adapter}): {line}")
    elif adapter == "ollama":
        # The stub is reachable without a key; it still cannot change anything.
        advisory = ADAPTERS["ollama"](_prompt(verdict))
        if advisory:
            verdict["next_actions"].append(f"ADVISORY (ollama): {advisory}")

    verdict["model"] = model
    verdict["key_status"] = key_status
    verdict["generated_utc"] = dt.datetime.now(dt.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")
    return verdict


def _write_if_changed(out: str, verdict: dict, *, stamp: bool = False) -> bool:
    """Write the verdict, unless the only thing that moved was the clock.

    The verdict is a pure function of tracked evidence plus a timestamp, and
    the default output path is tracked. So every run of the runbook step
    `tools/reviewer_verdict.py --dry-run` used to leave the working tree dirty
    with a one-line diff saying the clock advanced — and a human who runs it
    three times in a shift reverts it three times, or stops looking at
    `git status`, which is worse.

    Leaving the file alone costs one thing and it is worth naming: on an
    unchanged run `generated_utc` in the FILE is the time the finding last
    changed, not the time this ran. The run itself still prints a freshly
    stamped verdict to stdout, which is where a human looking for liveness is
    already looking, and `--stamp` forces the write for anyone who wants the
    file to say it too.

    Returns True if the file was written.
    """
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    if not stamp:
        try:
            with open(out, encoding="utf-8") as handle:
                current = json.load(handle)
        except (OSError, ValueError):
            current = None
        if current is not None and (
                {k: v for k, v in current.items() if k != "generated_utc"}
                == {k: v for k, v in verdict.items() if k != "generated_utc"}):
            return False
    with open(out, "w", encoding="utf-8") as handle:
        json.dump(verdict, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
    return True


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true",
                      help="local rules only; never opens a socket (default)")
    mode.add_argument("--xai", action="store_true",
                      help="also ask xAI, but ONLY if XAI_API_KEY is set")
    mode.add_argument("--ollama", action="store_true",
                      help="use the local adapter stub (not implemented)")
    parser.add_argument("--repo", default=REPO)
    parser.add_argument("--out", default=None,
                        help=f"where to write (default: {DEFAULT_OUT})")
    parser.add_argument("--strict", action="store_true",
                        help="exit 2 if any input was missing or unreadable")
    parser.add_argument("--stamp", action="store_true",
                        help="rewrite the output even when the finding is "
                             "identical, moving generated_utc forward")
    args = parser.parse_args(argv)

    adapter = "xai" if args.xai else ("ollama" if args.ollama else None)
    verdict = build(args.repo, adapter=adapter)

    out = args.out or os.path.join(args.repo, DEFAULT_OUT)
    wrote = _write_if_changed(out, verdict, stamp=args.stamp)

    print(json.dumps(verdict, indent=2, ensure_ascii=False))
    if wrote:
        print(f"\nverdict written to {os.path.abspath(out)}", file=sys.stderr)
    else:
        print(f"\nverdict unchanged; {os.path.abspath(out)} left alone "
              f"(--stamp to move its timestamp anyway)", file=sys.stderr)
    print(f"key_status={verdict['key_status']} "
          f"allows_progress={verdict['allows_progress']} "
          f"blockers={len(verdict['blockers'])}", file=sys.stderr)

    if args.strict and any(b.startswith("input ") for b in verdict["blockers"]):
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
