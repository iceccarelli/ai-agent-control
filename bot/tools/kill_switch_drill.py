#!/usr/bin/env python3
"""The kill-switch drill, as a program. Observations, not checkmarks.

WHAT THIS IS FOR
================
`docs/promotion/KILL_SWITCH_DRILL_SCRIPT.md` is a six-step script with blank
`observed: ______` lines, and it had never been run. It is one of the eight
promotion-gate items and one of the six a human owns.

The point of a drill is not to prove the switch exists — `_gate_kill_switch`
already does that. It is to prove that **a human can stop this process under
time pressure, and that stopping it actually stops entries**, on the day it
matters. `tools/drill.py` makes the same argument for the Phase D venue drill:
"a drill that lives in prose gets half-run at 2am by somebody tired, and its
evidence is a terminal scrollback nobody kept."

EVERY STEP HITS THE LIVE PATH. THAT IS THE DELIVERABLE.
=======================================================
A drill that asserts against its own re-implementation of the rules proves
nothing about the bot. So nothing here is re-derived:

    entries blocked   `BillionaireRiskManager.gate_order`, the same call
                      `trading_engine` makes before every entry, asserted to
                      return the real `KILL_SWITCH_ENGAGED`
    halt              `BillionaireRiskManager.should_halt_trading`
    per-symbol        `BillionaireRiskManager.can_trade`
    health            `TradingBot.health()` — the REAL method on a REAL
                      TradingBot, not a dict assembled here
    503               a REAL `HTTPServer` started by the bot's own
                      `start_health_server()`, fetched over a real socket.
                      The `200 if healthy else 503` mapping is never recomputed

An earlier version of this file built its own health dict. That was decorative
and is recorded here because it is exactly the failure this docstring warns
about.

HEALTH DEPENDS ON THREE THINGS, SO THE OTHER TWO ARE PINNED
===========================================================
`health()` is unhealthy when the switch is engaged OR a position is unprotected
OR the bot has not reconciled. To make the switch the ATTRIBUTABLE cause, the
drill records health BEFORE the trip as well as after, on a store with no
positions and with `_reconciled` set true. Healthy true -> false with nothing
else changed is the observation; a single unhealthy reading would not be.

SAFETY: IT DOES NOT TOUCH YOUR STATE
====================================
The switch it trips is real, and a drill that left it engaged would be the
outage it was rehearsing for. So:

* with no `--state` it runs against a THROWAWAY database in a temporary
  directory, and the production database is hashed before and after to prove
  it was never opened;
* `--state PATH` runs against a real database and is a deliberate human act;
* the switch's state is recorded BEFORE the drill and restored in a `finally`,
  including when a step raised. A switch engaged before the drill is LEFT
  engaged — a drill run during a real incident must not resume trading.

    python3 tools/kill_switch_drill.py
    python3 tools/kill_switch_drill.py --out artifacts/kill_switch_drill.json

Exit codes: 0 every step behaved as scripted; 1 a step did NOT (read the
report); 2 the switch could not be restored — a P0, look now.
"""
from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import hashlib
import json
import os
import socket
import sys
import tempfile
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO)
sys.path.insert(0, HERE)

import config as _config                            # noqa: E402
import persistence                                  # noqa: E402
import project_status as _ps                        # noqa: E402
import risk_management                              # noqa: E402

#: The token `clear_kill_switch_by_human` demands. A copy that drifted would
#: make the drill pass against a door that no longer opens, so a test asserts
#: this string appears in that method's own source.
HUMAN_TOKEN = "HUMAN_CLEARED_KILL_SWITCH"

DRILL_REASON = "kill-switch drill (tools/kill_switch_drill.py)"

DEFAULT_EVIDENCE = os.path.join("artifacts", "kill_switch_drill.json")


class _StubClient:
    """Enough venue for `TradingBot.__init__`. Sends nothing, reads nothing.

    The drill is about the risk chain and the health endpoint, neither of which
    touches the exchange. A real client here would make the drill require
    network and credentials to answer a question that involves neither.
    """

    category = "linear"
    is_linear = True
    is_spot = False

    def __init__(self) -> None:
        self.cfg = None

    def sync_time(self) -> int:
        return 0

    def get_wallet(self, *_a: Any, **_k: Any) -> Dict[str, Any]:
        return {}

    def get_equity(self, *_a: Any, **_k: Any) -> float:
        return 1000.0


def _sha256_file(path: str) -> Optional[str]:
    if not os.path.exists(path):
        return None
    with open(path, "rb") as handle:
        return hashlib.sha256(handle.read()).hexdigest()


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _observation(step: str, expected: str, observed: Any,
                 ok: bool) -> Dict[str, Any]:
    return {"step": step, "expected": expected, "observed": observed,
            "as_scripted": bool(ok)}


def _health_over_http(bot: Any, port: int) -> Dict[str, Any]:
    """Fetch /health over a real socket. Returns the status the SERVER sent.

    The `200 if healthy else 503` mapping lives in `_HealthHandler.do_GET` and
    is deliberately not recomputed here — recomputing it would test this file
    rather than the endpoint an orchestrator actually polls.
    """
    url = f"http://127.0.0.1:{port}/health"
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            return {"status": int(response.status),
                    "payload": json.loads(response.read())}
    except urllib.error.HTTPError as exc:          # 503 arrives here
        body = exc.read()
        try:
            payload = json.loads(body)
        except Exception:                           # noqa: BLE001
            payload = {"unparseable": body[:200].decode("utf-8", "replace")}
        return {"status": int(exc.code), "payload": payload}


def run(*, state_path: Optional[str] = None,
        config: Any = None) -> Dict[str, Any]:
    """Perform the drill and return the evidence."""
    cfg = config if config is not None else _config.get_config_object()

    production_db = os.path.join(
        REPO, str(getattr(cfg, "STATE_DB_PATH", "state/trading_state.db")))
    production_sha_before = _sha256_file(production_db)

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
    bot = None
    port = _free_port()

    try:
        import main                                 # noqa: PLC0415

        # -- 1. baseline ------------------------------------------------
        status = _ps.current(cfg)
        steps.append(_observation(
            "1_baseline",
            "not live-armed, no promoted model, and a scratch database",
            {"cleared_edge_signal": status.cleared_edge_signal,
             "execution_mode": status.execution_mode,
             "live_authorized": status.live_authorized,
             "models_current_present": status.models_current_present,
             "kill_switch_engaged_before": was_engaged,
             "database": state_path,
             "disposable_database": disposable},
            not status.live_authorized and not status.models_current_present))

        # The bot under test: REAL TradingBot, real risk manager, real health.
        drill_cfg = dataclasses.replace(
            cfg, ENABLE_HEALTH_SERVER=True, HEALTHCHECK_PORT=port)
        bot = main.TradingBot(config=drill_cfg, store=store,
                              client=_StubClient())
        # `health()` is unhealthy when the switch is engaged OR a position is
        # naked OR the bot has not reconciled. The other two are pinned so the
        # switch is the attributable cause of the flip below.
        bot._reconciled = True
        bot.start_health_server()

        # -- 2. health BEFORE the trip ----------------------------------
        before = bot.health()
        before_http = _health_over_http(bot, port)
        steps.append(_observation(
            "2_health_before_trip",
            "healthy, HTTP 200, with no naked positions and reconciled true",
            {"healthy": before["healthy"], "http_status": before_http["status"],
             "naked_positions": before["naked_positions"],
             "reconciled": before["reconciled"]},
            before["healthy"] is True and before_http["status"] == 200
            and not before["naked_positions"]))

        # -- 3. trip ----------------------------------------------------
        store.trip_kill_switch(DRILL_REASON)
        engaged, reason = store.is_kill_switch_engaged()
        steps.append(_observation(
            "3_trip", "the switch reads engaged, carrying the drill's reason",
            {"engaged": engaged, "reason": reason},
            engaged and reason == DRILL_REASON))

        # -- 4. entries are blocked, on the LIVE gate path --------------
        risk = risk_management.BillionaireRiskManager(config=drill_cfg,
                                                     store=store)
        halt = risk.should_halt_trading()
        gate = risk.gate_order(
            symbol="BTCUSDT", side="Buy", entry_price=100.0, stop_loss=99.0,
            quantity=0.0, account_equity=1_000.0)
        can = risk.can_trade("BTCUSDT")
        steps.append(_observation(
            "4_entries_blocked",
            "gate_order returns KILL_SWITCH_ENGAGED, should_halt_trading true, "
            "can_trade false",
            {"gate_reason": gate.reason, "gate_ok": gate.ok,
             "should_halt_trading": halt, "can_trade": can},
            (not gate.ok) and gate.reason == "KILL_SWITCH_ENGAGED"
            and halt and not can))

        # -- 5. health AFTER the trip, and the real 503 -----------------
        after = bot.health()
        after_http = _health_over_http(bot, port)
        steps.append(_observation(
            "5_health_unhealthy_503",
            "the same endpoint now reports unhealthy and the server sends 503",
            {"healthy": after["healthy"], "http_status": after_http["status"],
             "kill_switch": after["kill_switch"],
             "kill_reason": after["kill_reason"],
             "healthy_before": before["healthy"],
             "http_status_before": before_http["status"]},
            after["healthy"] is False and after_http["status"] == 503
            and after["kill_switch"] is True
            and before_http["status"] == 200))

        # -- 6. it survives a restart -----------------------------------
        reopened = persistence.StateStore(state_path)
        still, still_reason = reopened.is_kill_switch_engaged()
        steps.append(_observation(
            "6_survives_restart",
            "a new StateStore over the same file still sees it engaged",
            {"engaged": still, "reason": still_reason}, still))
        reopened.close()

        # -- 7. only the exact token clears it --------------------------
        wrong_error = ""
        try:
            store.clear_kill_switch_by_human("please")
        except Exception as exc:                    # noqa: BLE001
            wrong_error = f"{type(exc).__name__}: {exc}"
        after_wrong, _ = store.is_kill_switch_engaged()
        steps.append(_observation(
            "7a_wrong_token_refused",
            "a wrong token raises AND leaves the switch engaged",
            {"raised": wrong_error, "still_engaged": after_wrong},
            bool(wrong_error) and after_wrong))

        store.clear_kill_switch_by_human(HUMAN_TOKEN)
        cleared, _ = store.is_kill_switch_engaged()
        recovered = bot.health()
        recovered_http = _health_over_http(bot, port)
        steps.append(_observation(
            "7b_exact_token_clears",
            "the exact operator token clears it and health returns to 200",
            {"engaged_after_clear": cleared,
             "healthy": recovered["healthy"],
             "http_status": recovered_http["status"]},
            (not cleared) and recovered["healthy"] is True
            and recovered_http["status"] == 200))

    finally:
        if bot is not None and getattr(bot, "_http", None) is not None:
            try:
                bot._http.shutdown()
                bot._http.server_close()
            except Exception:                       # noqa: BLE001
                pass
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
        except Exception as exc:                    # noqa: BLE001
            restored = False
            restore_error = f"{type(exc).__name__}: {exc}"
        try:
            final_engaged, _ = store.is_kill_switch_engaged()
        except Exception:                           # noqa: BLE001
            final_engaged = None
        try:
            store.close()
        except Exception:                           # noqa: BLE001
            pass

    production_sha_after = _sha256_file(production_db)
    steps.append(_observation(
        "8_disengaged_and_production_untouched",
        "the switch is disengaged and the production database is byte-identical",
        {"switch_engaged_at_exit": final_engaged,
         "production_db": os.path.relpath(production_db, REPO),
         "production_sha256_before": production_sha_before,
         "production_sha256_after": production_sha_after,
         "production_untouched": production_sha_before == production_sha_after},
        (final_engaged is False or (was_engaged and final_engaged is True))
        and production_sha_before == production_sha_after))

    report: Dict[str, Any] = {
        "tool": "kill_switch_drill",
        "performed_utc": dt.datetime.now(dt.timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"),
        "database": "throwaway (temporary)" if disposable else state_path,
        "disposable_database": disposable,
        "apis_exercised": [
            "StateStore.trip_kill_switch",
            "StateStore.is_kill_switch_engaged",
            "StateStore.clear_kill_switch_by_human",
            "BillionaireRiskManager.gate_order -> _gate_kill_switch",
            "BillionaireRiskManager.should_halt_trading",
            "BillionaireRiskManager.can_trade",
            "TradingBot.health",
            "TradingBot.start_health_server -> _HealthHandler.do_GET",
        ],
        "steps": steps,
        "all_steps_as_scripted": all(s["as_scripted"] for s in steps),
        "switch_restored": restored,
        "switch_restore_error": restore_error,
        "switch_engaged_before": was_engaged,
        "switch_engaged_at_exit": final_engaged,
        "production_db_untouched": production_sha_before == production_sha_after,
        # -- the part a process may not produce ------------------------
        "gate_item": "kill_switch_drill_recorded",
        "gate_item_completed": False,
        "signature": {"drill_performed_by": "", "witnessed_by": "",
                      "date": ""},
        "why_this_does_not_complete_the_item": (
            "The item is owned by a HUMAN. What it asks is whether a person "
            "can stop this process under time pressure; a drill the process "
            "ran for itself cannot answer that. This records that the "
            "MECHANISM behaves as scripted on the live code path, which is the "
            "half a program can establish. A human performs the steps, "
            "witnesses them, and signs — and only then does the item move."),
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
    parser.add_argument("--out", default=DEFAULT_EVIDENCE,
                        help=f"where to write the transcript "
                             f"(default {DEFAULT_EVIDENCE}); '' to skip")
    args = parser.parse_args(argv)

    report = run(state_path=args.state)
    text = json.dumps(report, indent=2)
    print(text)

    if args.out:
        path = args.out if os.path.isabs(args.out) else os.path.join(
            REPO, args.out)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text + "\n")
        print(f"\nevidence written to {os.path.relpath(path, REPO)}",
              file=sys.stderr)

    if not report["switch_restored"]:
        print("P0: THE KILL SWITCH COULD NOT BE RESTORED — look now: "
              f"{report['switch_restore_error']}", file=sys.stderr)
        return 2
    if not report["all_steps_as_scripted"]:
        failed = [s["step"] for s in report["steps"] if not s["as_scripted"]]
        print(f"steps that did NOT behave as scripted: {failed}",
              file=sys.stderr)
        return 1
    print("\nevery step behaved as scripted on the live path. THE GATE ITEM IS "
          "STILL OPEN: a human performs, witnesses and signs it.",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
