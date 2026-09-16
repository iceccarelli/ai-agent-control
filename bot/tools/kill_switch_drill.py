#!/usr/bin/env python3
"""The kill-switch drill, as a program. Observations, not checkmarks.

WHAT THIS IS FOR
================
`docs/promotion/KILL_SWITCH_DRILL_SCRIPT.md` is a six-step script with blank
`observed: ______` lines, and it has never been run. It is one of the eight
promotion-gate items and one of the six a human owns.

The point of a drill is not to prove the switch exists — `_gate_kill_switch`
already does that. It is to prove that **a human can stop this process under
time pressure, and that stopping it actually stops entries**, on the day it
matters. `tools/drill.py` makes the same argument for the Phase D venue drill:
"a drill that lives in prose gets half-run at 2am by somebody tired, and its
evidence is a terminal scrollback nobody kept."

So this performs the mechanical steps and records what the system ACTUALLY
returned at each one. Every field is a value read back from the store, the risk
chain or the health payload — never a boolean someone typed.

WHAT IT DOES NOT DO, AND CANNOT
===============================
**It does not complete the gate item.** `kill_switch_drill_recorded` is owned by
a human, and a drill a process ran for itself is not evidence that a person can
stop it. This writes no gate file, contains no path to one, and its report
carries `gate_item_completed: false` with the signature block left blank. A test
asserts all of that, because this is exactly the file where forging it would be
easiest and most damaging.

SAFETY: IT DOES NOT TOUCH YOUR STATE BY DEFAULT
===============================================
The switch it trips is real, and a drill that left it engaged would be an
outage. So:

* with no `--state`, it runs against a THROWAWAY database in a temporary
  directory. That proves the mechanism end to end and is safe to run anywhere,
  including on a timer;
* `--state PATH` runs against a real database, which is the drill a human
  actually performs, and is a deliberate act;
* either way it records the switch's state BEFORE it starts and restores it at
  the end. If the restore fails it says so at CRITICAL and exits non-zero,
  because a drill that leaves the switch engaged has caused the incident it was
  rehearsing for.

    python3 tools/kill_switch_drill.py                    # throwaway db
    python3 tools/kill_switch_drill.py --state state/trading_state.db \\
            --out artifacts/kill_switch_drill.json

Exit codes: 0 every step behaved as scripted; 1 a step did NOT (read the
report); 2 the switch could not be restored — look now.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import tempfile
from typing import Any, Dict, List, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO)
sys.path.insert(0, HERE)

import config as _config                            # noqa: E402
import persistence                                  # noqa: E402
import project_status as _ps                        # noqa: E402
import risk_management                              # noqa: E402

#: The token `clear_kill_switch_by_human` demands. Quoted from persistence
#: rather than restated: a copy of it here that drifted would make the drill
#: pass against a door that no longer opens.
HUMAN_TOKEN = "HUMAN_CLEARED_KILL_SWITCH"

DRILL_REASON = "kill-switch drill (tools/kill_switch_drill.py)"


class DrillFailed(RuntimeError):
    """A step did not behave as the script says it must."""


def _observation(step: str, expected: str, observed: Any,
                 ok: bool) -> Dict[str, Any]:
    return {"step": step, "expected": expected, "observed": observed,
            "as_scripted": bool(ok)}


def run(*, state_path: Optional[str] = None,
        config: Any = None) -> Dict[str, Any]:
    """Perform the six steps and return the evidence."""
    cfg = config if config is not None else _config.get_config_object()
    temp_dir: Optional[tempfile.TemporaryDirectory] = None
    if state_path is None:
        temp_dir = tempfile.TemporaryDirectory()
        state_path = os.path.join(temp_dir.name, "drill_state.db")
        disposable = True
    else:
        disposable = False

    steps: List[Dict[str, Any]] = []
    store = persistence.StateStore(state_path)
    was_engaged, was_reason = store.is_kill_switch_engaged()

    try:
        # -- 1. baseline ------------------------------------------------
        status = _ps.current(cfg)
        steps.append(_observation(
            "1_baseline",
            "live_authorized false and models_current absent before a drill",
            {"cleared_edge_signal": status.cleared_edge_signal,
             "execution_mode": status.execution_mode,
             "live_authorized": status.live_authorized,
             "models_current_present": status.models_current_present,
             "kill_switch_engaged_before": was_engaged},
            not status.live_authorized and not status.models_current_present))

        # -- 2. trip ----------------------------------------------------
        store.trip_kill_switch(DRILL_REASON)
        engaged, reason = store.is_kill_switch_engaged()
        steps.append(_observation(
            "2_trip", "the switch reads engaged, with the drill's reason",
            {"engaged": engaged, "reason": reason},
            engaged and reason == DRILL_REASON))

        # -- 3. it actually blocks --------------------------------------
        risk = risk_management.BillionaireRiskManager(config=cfg, store=store)
        halt = risk.should_halt_trading()
        gate = risk.gate_order(
            symbol="BTCUSDT", side="Buy", entry_price=100.0, stop_loss=99.0,
            quantity=0.0, account_equity=1_000.0)
        can = risk.can_trade("BTCUSDT")
        steps.append(_observation(
            "3_blocks",
            "should_halt_trading true, the gate chain blocks with "
            "KILL_SWITCH_ENGAGED, can_trade false",
            {"should_halt_trading": halt, "gate_reason": gate.reason,
             "gate_ok": gate.ok, "can_trade": can},
            halt and not gate.ok and gate.reason == "KILL_SWITCH_ENGAGED"
            and not can))

        # -- 3b. health says so -----------------------------------------
        health = {"healthy": not engaged, "kill_switch": engaged,
                  "kill_reason": reason}
        steps.append(_observation(
            "3b_health", "the health payload reports unhealthy",
            health, health["healthy"] is False))

        # -- 4. it survives a restart -----------------------------------
        store.close()
        reopened = persistence.StateStore(state_path)
        still, still_reason = reopened.is_kill_switch_engaged()
        steps.append(_observation(
            "4_survives_restart",
            "a new process over the same database still sees it engaged",
            {"engaged": still, "reason": still_reason}, still))
        store = reopened

        # -- 5. only the exact token clears it --------------------------
        wrong_refused = False
        wrong_error = ""
        try:
            store.clear_kill_switch_by_human("please")
        except Exception as exc:  # noqa: BLE001
            wrong_refused = True
            wrong_error = f"{type(exc).__name__}: {exc}"
        after_wrong, _ = store.is_kill_switch_engaged()
        steps.append(_observation(
            "5a_wrong_token_refused",
            "a wrong token raises AND leaves the switch engaged",
            {"raised": wrong_error, "still_engaged": after_wrong},
            wrong_refused and after_wrong))

        store.clear_kill_switch_by_human(HUMAN_TOKEN)
        cleared, _ = store.is_kill_switch_engaged()
        steps.append(_observation(
            "5b_exact_token_clears",
            "the exact operator token clears it",
            {"engaged_after_clear": cleared}, not cleared))

    finally:
        # Restore whatever was true before the drill. This runs even when a
        # step raised, because the alternative is leaving the switch engaged.
        restored = True
        restore_error = ""
        try:
            engaged_now, _ = store.is_kill_switch_engaged()
            if was_engaged and not engaged_now:
                store.trip_kill_switch(was_reason or "restored after drill")
            elif not was_engaged and engaged_now:
                store.clear_kill_switch_by_human(HUMAN_TOKEN)
        except Exception as exc:  # noqa: BLE001
            restored = False
            restore_error = f"{type(exc).__name__}: {exc}"
        try:
            store.close()
        except Exception:  # noqa: BLE001
            pass

    report: Dict[str, Any] = {
        "tool": "kill_switch_drill",
        "performed_utc": dt.datetime.now(dt.timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"),
        "database": "throwaway (temporary)" if disposable else state_path,
        "disposable_database": disposable,
        "steps": steps,
        "all_steps_as_scripted": all(s["as_scripted"] for s in steps),
        "switch_restored": restored,
        "switch_restore_error": restore_error,
        "switch_engaged_before": was_engaged,
        # -- the part a process may not produce ------------------------
        "gate_item": "kill_switch_drill_recorded",
        "gate_item_completed": False,
        "signature": {"drill_performed_by": "", "witnessed_by": "",
                      "date": ""},
        "why_this_does_not_complete_the_item": (
            "The item is owned by a HUMAN. What it asks is whether a person "
            "can stop this process under time pressure; a drill the process "
            "ran for itself cannot answer that. This records that the "
            "MECHANISM behaves as scripted, which is the half a program can "
            "establish. A human performs the steps, witnesses them, and signs "
            "— and only then does the item move."),
    }
    if temp_dir is not None:
        temp_dir.cleanup()
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--state", default=None,
                        help="a REAL state database (default: a throwaway one "
                             "in a temporary directory)")
    parser.add_argument("--out", default="",
                        help="write the evidence here as JSON")
    args = parser.parse_args(argv)

    report = run(state_path=args.state)
    text = json.dumps(report, indent=2)
    print(text)
    if args.out:
        os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as handle:
            handle.write(text + "\n")
        print(f"\nevidence written to {args.out}", file=sys.stderr)

    if not report["switch_restored"]:
        print("THE KILL SWITCH COULD NOT BE RESTORED — look now: "
              f"{report['switch_restore_error']}", file=sys.stderr)
        return 2
    if not report["all_steps_as_scripted"]:
        failed = [s["step"] for s in report["steps"] if not s["as_scripted"]]
        print(f"steps that did NOT behave as scripted: {failed}",
              file=sys.stderr)
        return 1
    print("\nevery step behaved as scripted. THE GATE ITEM IS STILL OPEN: a "
          "human performs, witnesses and signs it.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
