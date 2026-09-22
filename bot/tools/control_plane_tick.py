#!/usr/bin/env python3
"""NO-HUMAN control-plane tick: refresh evidence, ask the reviewer, emit a mission.

ORCHESTRATOR ROLE (see docs/human/AGENT_CONTROL_PLANE.md)
=========================================================
This is the missing glue the human was doing by hand: run the sanctioned
corpus/refresh tools, re-read Stage B counters, invoke the off-path reviewer,
and write a machine-to-machine NEXT_MISSION.md for the Builder (Claude).

WHAT IT MAY DO
==============
* subprocess existing tools under bot/ (append dry-run, daily_forward_refresh,
  reviewer_verdict)
* READ artifacts/forward_shadow_current.json
* WRITE artifacts/control_plane_tick.json and artifacts/NEXT_MISSION.md
* WRITE artifacts/reviewer_verdict.json, but ONLY when the finding changed —
  the reviewer scores to state/ first, see _promote_verdict()

WHAT IT MUST NEVER DO
=====================
* set allows_live / clear the kill switch / place an order
* write corpus files unless CONTROL_PLANE_ALLOW_APPEND_WRITE=1 (maps to the
  existing append_closed_corpus.py --write flag — no new writer invented)
* promote scratch forward scores into artifacts/forward_shadow_current.json
  (daily_forward_refresh scores to scratch only; that pattern is preserved)
* print, log, or write any API key / venue secret
* import bybit_connection, trading_engine, or carry_broker

    cd bot && python3 tools/control_plane_tick.py

Exit: 0 tick artefacts written (even if a child tool failed — errors are
recorded in the tick JSON), and also 0 when another tick already holds the
lock — an overlap is normal operation, not a fault; 2 if the tick itself could
not write outputs.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import subprocess
import sys
from typing import Any, Dict, List, Optional, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)

FORWARD = os.path.join("artifacts", "forward_shadow_current.json")
TICK_OUT = os.path.join("artifacts", "control_plane_tick.json")
MISSION_OUT = os.path.join("artifacts", "NEXT_MISSION.md")
VERDICT_OUT = os.path.join("artifacts", "reviewer_verdict.json")
#: The reviewer writes HERE first. state/ is gitignored; artifacts/ is not.
VERDICT_SCRATCH = os.path.join("state", "control_plane", "reviewer_verdict.json")

#: Opt-in only. Maps 1:1 onto append_closed_corpus.py --write. Absent → dry-run.
APPEND_WRITE_ENV = "CONTROL_PLANE_ALLOW_APPEND_WRITE"

#: One tick at a time. See _lock().
LOCK_DIR = os.path.join("state", "control_plane", "tick.lock")


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _run(argv: List[str], *, timeout: float = 600) -> Dict[str, Any]:
    """Run a sanctioned tool in bot/. Never passes env secrets into argv."""
    started = _utc_now()
    try:
        done = subprocess.run(
            argv, cwd=REPO, capture_output=True, text=True, timeout=timeout,
            env=os.environ.copy())
        return {
            "argv": argv,
            "started_utc": started,
            "ended_utc": _utc_now(),
            "returncode": done.returncode,
            "stdout_tail": (done.stdout or "")[-2000:],
            "stderr_tail": (done.stderr or "")[-2000:],
            "error": None,
        }
    except Exception as exc:  # noqa: BLE001 — record, do not raise through tick
        return {
            "argv": argv,
            "started_utc": started,
            "ended_utc": _utc_now(),
            "returncode": None,
            "stdout_tail": "",
            "stderr_tail": "",
            "error": f"{type(exc).__name__}",
        }


def _read_json(rel: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    path = os.path.join(REPO, rel)
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle), None
    except FileNotFoundError:
        return None, f"missing: {rel}"
    except (OSError, ValueError) as exc:
        return None, f"unreadable: {rel} ({type(exc).__name__})"


def _resolve_closed_forward_bars(doc: Optional[Dict[str, Any]]) -> int:
    """Same resolution rule as `tools/stage_b_bars.resolve_closed_forward_bars`,
    duplicated (not imported) on purpose: this file is stdlib-only so the cron
    tick keeps running with no venv — see TestTheTickImportsNothingOnTheOrderPath.

    The scorer nests the closed-bar count under `ceiling.closed_forward_bars`
    / `forward_window.of_which_closed`; a freshly-promoted shadow file has no
    top-level `closed_forward_bars` key at all, so reading it directly reports
    0/180 even though the ceiling already says otherwise.
    """
    if not doc:
        return 0
    ceiling = doc.get("ceiling") or {}
    value = ceiling.get("closed_forward_bars")
    if value is not None:
        return int(value)
    window = doc.get("forward_window") or {}
    value = window.get("of_which_closed")
    if value is not None:
        return int(value)
    return int(doc.get("closed_forward_bars") or 0)


def _allows_live_status() -> Dict[str, Any]:
    """Report runtime allows_live WITHOUT ever setting it.

    Prefer the promotion_gate helper via a one-shot subprocess so this file
    stays stdlib-only and never imports the order path.
    """
    probe = (
        "import json,promotion_gate as pg;"
        "print(json.dumps({'allows_live': bool(pg.promotion_gate_allows_live())}))"
    )
    result = _run([sys.executable, "-c", probe], timeout=60)
    if result["returncode"] == 0 and result["stdout_tail"].strip():
        try:
            payload = json.loads(result["stdout_tail"].strip().splitlines()[-1])
            payload["source"] = "promotion_gate.promotion_gate_allows_live"
            payload["tick_may_set_allows_live"] = False
            return payload
        except (ValueError, IndexError):
            pass
    return {
        "allows_live": False,
        "source": "fail_closed_default",
        "tick_may_set_allows_live": False,
        "probe_error": result.get("error") or f"rc={result.get('returncode')}",
    }


def _substance(doc: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """The verdict minus the clock. Two verdicts are the same finding if this is."""
    if doc is None:
        return None
    return {k: v for k, v in doc.items() if k != "generated_utc"}


def _promote_verdict(*, reviewer_ok: bool) -> Dict[str, Any]:
    """Copy the scratch verdict over the tracked one ONLY if the finding moved.

    Every hour the reviewer recomputes the same verdict from the same tracked
    evidence and stamps it with a new `generated_utc`. Writing that straight
    into artifacts/ left the working tree permanently dirty: `git status` was
    never clean, `git checkout` refused to switch branches, and a real change to
    `blockers` or `next_actions` was one timestamp line among many — invisible
    for the same reason a smoke alarm you have muted is.

    So the reviewer scores to state/ (gitignored) and is promoted deliberately.
    This is the pattern daily_forward_refresh already follows for the forward
    shadow; it is applied here for the same reason.

    Consequence, stated because it is easy to misread: `generated_utc` in the
    TRACKED verdict is now the time the finding last CHANGED, not the time the
    reviewer last ran. Liveness lives in artifacts/control_plane_tick.json,
    which is regenerated every tick and is not tracked.

    `reviewer_ok` is not optional politeness. The scratch file OUTLIVES the run
    that wrote it: if this tick's reviewer died, yesterday's scratch is still
    sitting there, and a promote that only checks "is the file readable" would
    copy a stale finding into artifacts/ and stamp it as the current one. A
    reviewer that failed produces no verdict, and no verdict must mean the last
    known one stands.
    """
    if not reviewer_ok:
        return {"promoted": False, "reason": "reviewer did not succeed",
                "computed_utc": None}

    fresh, fresh_err = _read_json(VERDICT_SCRATCH)
    if fresh_err:
        return {"promoted": False, "reason": fresh_err, "computed_utc": None}

    current, _ = _read_json(VERDICT_OUT)
    computed = fresh.get("generated_utc")
    if _substance(current) == _substance(fresh):
        return {"promoted": False, "reason": "finding unchanged",
                "computed_utc": computed}

    path = os.path.join(REPO, VERDICT_OUT)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(fresh, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
    except OSError as exc:
        return {"promoted": False, "reason": f"unwritable: {type(exc).__name__}",
                "computed_utc": computed}
    return {"promoted": True,
            "reason": "first verdict" if current is None else "finding changed",
            "computed_utc": computed}


def _lock() -> Tuple[bool, str]:
    """Take the single-tick lock, or report who holds it.

    The cron fires hourly. The tick subprocesses daily_forward_refresh with a
    600-second timeout and append_closed_corpus with 180, and neither is
    guaranteed to be the slowest thing this ever runs. Two ticks overlapping
    means two corpus appenders running at once against the same files — and
    under CONTROL_PLANE_ALLOW_APPEND_WRITE=1 those are real writers.

    mkdir is the lock because it is atomic on every filesystem this will meet,
    unlike "check then create". A lock whose owner is gone is stale, not held:
    a tick killed by a reboot must not wedge the control plane until someone
    notices, so the PID inside is checked and a dead owner's lock is taken.
    """
    path = os.path.join(REPO, LOCK_DIR)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    for attempt in (1, 2):
        try:
            os.mkdir(path)
        except FileExistsError:
            owner = ""
            try:
                with open(os.path.join(path, "pid"), encoding="utf-8") as fh:
                    owner = fh.read().strip()
            except OSError:
                pass
            if attempt == 1 and not _pid_alive(owner):
                try:
                    os.remove(os.path.join(path, "pid"))
                except OSError:
                    pass
                try:
                    os.rmdir(path)
                except OSError:
                    return False, f"held by pid {owner or 'unknown'}"
                continue
            return False, f"held by pid {owner or 'unknown'}"
        with open(os.path.join(path, "pid"), "w", encoding="utf-8") as handle:
            handle.write(f"{os.getpid()}\n")
        return True, path
    return False, "lock contended"


def _pid_alive(pid: str) -> bool:
    """A blank or unparseable owner counts as alive — never steal on a guess."""
    if not pid.isdigit():
        return True
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return True
    return True


def _unlock(path: str) -> None:
    try:
        os.remove(os.path.join(path, "pid"))
    except OSError:
        pass
    try:
        os.rmdir(path)
    except OSError:
        pass


def _mission_body(verdict: Dict[str, Any], forward: Optional[Dict[str, Any]],
                  allows_live: bool) -> str:
    """Machine-to-machine Claude mission. No essays. Derived only from
    reviewer next_actions + Stage B gaps + remaining human gates."""
    stage = verdict.get("stage_b") or {}
    n = int(stage.get("forward_n_trades")
            or (forward or {}).get("forward_n_trades") or 0)
    of = int(stage.get("of_20") or 20)
    bars = int(stage.get("closed_forward_bars")
               or _resolve_closed_forward_bars(forward))
    actions = list(verdict.get("next_actions") or [])
    blockers = list(verdict.get("blockers") or [])
    human_gates = [a for a in actions
                   if a.startswith("gate item open (human):")]
    observe = [a for a in actions
               if a.startswith("gate item open") and "observation" in a]
    accrue = [a for a in actions if a.startswith("accrue ")]
    advisory = [a for a in actions if a.startswith("ADVISORY")]

    lines = [
        "# NEXT_MISSION — NO-YIELD / NO-HUMAN-REWRITE",
        f"generated_utc: {_utc_now()}",
        "role: BUILDER (Claude Code)",
        "money_path: Stage B accrual then gated micro-live; NEVER skip allows_live",
        f"allows_live: {str(allows_live).lower()} (tick MUST NOT flip this)",
        f"stage_b: forward_n_trades={n}/{of}; closed_forward_bars={bars}/180",
        f"allows_progress: {str(bool(verdict.get('allows_progress'))).lower()}",
        "",
        "## DO",
    ]
    if accrue:
        for a in accrue:
            lines.append(f"- {a}")
    else:
        lines.append("- Stage B counters at gate threshold; hold for human live arming")
    for a in observe:
        lines.append(f"- {a}")
    lines.append(
        "- Keep order path untouched; no venue keys; no LLM imports on order path")
    lines.append(
        "- Re-run: python3 tools/reviewer_verdict.py --dry-run"
        " (add --xai only if XAI_API_KEY present)")
    lines.append(
        "- After code/evidence change: python3 tools/control_plane_tick.py")

    lines.append("")
    lines.append("## HUMAN-ONLY (do not forge signatures)")
    if human_gates:
        for a in human_gates:
            lines.append(f"- {a}")
    else:
        lines.append("- (none listed in reviewer next_actions)")

    lines.append("")
    lines.append("## BLOCKERS")
    if blockers:
        for b in blockers:
            lines.append(f"- {b}")
    else:
        lines.append("- (none)")

    if advisory:
        lines.append("")
        lines.append("## ADVISORY (non-binding)")
        for a in advisory:
            lines.append(f"- {a}")

    lines.append("")
    lines.append("## FORBIDDEN")
    lines.append("- set allows_live / clear kill switch / place orders")
    lines.append("- invent new corpus writers; use append_closed_corpus.py only")
    lines.append("- promote scratch forward scores over forward_shadow_current.json")
    lines.append("- rewrite this mission into prose for a human paste loop")
    lines.append("")
    return "\n".join(lines)


def run_tick(*, allow_append_write: bool = False) -> Tuple[Dict[str, Any], Dict[str, Any], Optional[Dict[str, Any]], Dict[str, Any]]:
    errors: List[str] = []
    steps: Dict[str, Any] = {}

    # 1) Corpus append — dry-run unless operator opt-in matches existing --write.
    append_argv = [sys.executable, os.path.join("tools", "append_closed_corpus.py")]
    if allow_append_write:
        append_argv.append("--write")
    steps["append_closed_corpus"] = _run(append_argv, timeout=180)
    if steps["append_closed_corpus"].get("returncode") not in (0,):
        errors.append(
            f"append_closed_corpus rc={steps['append_closed_corpus'].get('returncode')}"
            f" err={steps['append_closed_corpus'].get('error')}")

    # 2) daily_forward_refresh — always read-only; scores to scratch internally.
    steps["daily_forward_refresh"] = _run(
        [sys.executable, os.path.join("tools", "daily_forward_refresh.py")],
        timeout=600)
    if steps["daily_forward_refresh"].get("returncode") not in (0,):
        errors.append(
            f"daily_forward_refresh rc={steps['daily_forward_refresh'].get('returncode')}"
            f" err={steps['daily_forward_refresh'].get('error')}")

    # 3) Forward shadow: existing pattern NEVER writes artifacts/. Read only.
    forward, forward_err = _read_json(FORWARD)
    steps["forward_shadow"] = {
        "action": "read_only",
        "path": FORWARD,
        "write_skipped_reason": (
            "daily_forward_refresh scores to scratch only; promoting into "
            "artifacts/forward_shadow_current.json is not a sanctioned pattern"),
    }
    if forward_err:
        errors.append(forward_err)

    # 4) Reviewer — always local rules; --xai only when keyed (adapter no-ops else).
    key_present = bool(os.environ.get("XAI_API_KEY"))
    verdict_argv = [
        sys.executable, os.path.join("tools", "reviewer_verdict.py"),
        "--dry-run", "--out", VERDICT_SCRATCH,
    ]
    steps["reviewer_verdict_dry_run"] = _run(verdict_argv, timeout=120)
    if steps["reviewer_verdict_dry_run"].get("returncode") not in (0,):
        errors.append(
            f"reviewer_verdict --dry-run "
            f"rc={steps['reviewer_verdict_dry_run'].get('returncode')}")

    if key_present:
        xai_argv = [
            sys.executable, os.path.join("tools", "reviewer_verdict.py"),
            "--xai", "--out", VERDICT_SCRATCH,
        ]
        steps["reviewer_verdict_xai"] = _run(xai_argv, timeout=120)
        if steps["reviewer_verdict_xai"].get("returncode") not in (0,):
            errors.append(
                f"reviewer_verdict --xai "
                f"rc={steps['reviewer_verdict_xai'].get('returncode')}")
    else:
        steps["reviewer_verdict_xai"] = {
            "skipped": True,
            "reason": "XAI_API_KEY unset; local dry-run verdict retained",
        }

    # Whichever reviewer invocation last wrote the scratch is the one that has
    # to have succeeded. With a key that is the --xai run; without, the dry run.
    reviewer_step = ("reviewer_verdict_xai" if key_present
                     else "reviewer_verdict_dry_run")
    reviewer_ok = steps[reviewer_step].get("returncode") == 0
    steps["reviewer_verdict_promote"] = _promote_verdict(
        reviewer_ok=reviewer_ok)

    verdict, verdict_err = _read_json(VERDICT_OUT)
    if verdict_err:
        errors.append(verdict_err)
        verdict = {
            "allows_progress": False,
            "blockers": [verdict_err],
            "stage_b": {
                "forward_n_trades": int((forward or {}).get("forward_n_trades") or 0),
                "of_20": 20,
                "closed_forward_bars": _resolve_closed_forward_bars(forward),
            },
            "next_actions": [],
            "risk": {"allows_live_must_be_false": True},
        }

    live = _allows_live_status()
    n_trades = int((verdict.get("stage_b") or {}).get("forward_n_trades")
                   or (forward or {}).get("forward_n_trades") or 0)

    tick = {
        "tool": "control_plane_tick",
        "generated_utc": _utc_now(),
        "repo": REPO,
        "forward_n_trades": n_trades,
        "closed_forward_bars": int(
            (verdict.get("stage_b") or {}).get("closed_forward_bars")
            or _resolve_closed_forward_bars(forward)),
        "allows_live": bool(live.get("allows_live")),
        "allows_live_probe": live,
        "allows_progress": bool(verdict.get("allows_progress")),
        "blockers": list(verdict.get("blockers") or []),
        "next_actions": list(verdict.get("next_actions") or []),
        "reviewer": {
            "model": verdict.get("model"),
            "key_status": verdict.get("key_status"),
            # When the finding last CHANGED ...
            "generated_utc": verdict.get("generated_utc"),
            # ... versus when it was last COMPUTED. Equal on a tick that moved
            # the verdict; the second is always this tick.
            "computed_utc": steps["reviewer_verdict_promote"].get("computed_utc"),
            "promoted": steps["reviewer_verdict_promote"].get("promoted"),
        },
        "append_write_enabled": bool(allow_append_write),
        "forward_shadow_write": False,
        "steps": {
            k: {sk: sv for sk, sv in v.items()
                if sk not in ("stdout_tail", "stderr_tail")}
            for k, v in steps.items()
        },
        "step_tails": {
            k: {"stdout_tail": v.get("stdout_tail", ""),
                "stderr_tail": v.get("stderr_tail", "")}
            for k, v in steps.items()
            if isinstance(v, dict) and ("stdout_tail" in v or "stderr_tail" in v)
        },
        "errors": errors,
        "risk": {
            "allows_live_must_be_false": True,
            "tick_sets_allows_live": False,
            "tick_places_orders": False,
            "secrets_printed": False,
        },
    }
    # Drop bulky tails from the committed artefact; keep errors + status.
    tick.pop("step_tails", None)
    return tick, verdict, forward, live


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--allow-append-write", action="store_true",
        help="pass --write to append_closed_corpus.py (also set by "
             f"{APPEND_WRITE_ENV}=1)")
    args = parser.parse_args(argv)

    allow_write = bool(args.allow_append_write) or (
        os.environ.get(APPEND_WRITE_ENV, "").strip() == "1")

    held, lock = _lock()
    if not held:
        # Exit 0: an overlapping tick is normal operation, not a fault. A
        # nonzero status here would fill the cron log with mail about the
        # control plane working as designed.
        print(json.dumps({"skipped": "another tick is running",
                          "detail": lock}, indent=2))
        return 0
    try:
        tick, verdict, forward, live = run_tick(allow_append_write=allow_write)
    finally:
        _unlock(lock)

    out_tick = os.path.join(REPO, TICK_OUT)
    out_mission = os.path.join(REPO, MISSION_OUT)
    try:
        os.makedirs(os.path.dirname(out_tick), exist_ok=True)
        with open(out_tick, "w", encoding="utf-8") as handle:
            json.dump(tick, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
        mission = _mission_body(verdict, forward, bool(live.get("allows_live")))
        with open(out_mission, "w", encoding="utf-8") as handle:
            handle.write(mission)
    except OSError as exc:
        print(f"FAILED to write tick outputs: {type(exc).__name__}", file=sys.stderr)
        return 2

    # Safe summary — never dump env, never print key material.
    summary = {
        "tick": out_tick,
        "mission": out_mission,
        "forward_n_trades": tick["forward_n_trades"],
        "allows_live": tick["allows_live"],
        "allows_progress": tick["allows_progress"],
        "next_actions": tick["next_actions"],
        "errors": tick["errors"],
        "key_status": (tick.get("reviewer") or {}).get("key_status"),
    }
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    print(f"\ncontrol_plane_tick written to {out_tick}", file=sys.stderr)
    print(f"NEXT_MISSION written to {out_mission}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
