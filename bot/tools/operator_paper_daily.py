#!/usr/bin/env python3
"""One-shot paper operator: preflight, status, gate, ladder. Then stop.

This is the Track C entrypoint the master prompt names. It has tools:
read, hash, report, refuse. It does NOT have tools: edit FUND_ABS, edit
thresholds, write live_authorized, place an order, start a daemon.

Exit codes
  0  SANDBOX_OK   sandbox flags sane, gate reported, ladder reported
  2  LIVE_REQUESTED  someone set USE_TESTNET=0 and PAPER_TRADING=0 - this tool
                     refuses to exist in that environment
  3  PREFLIGHT_FAIL  models/current present, LIVE_TRADING_ACK set in a paper
                     run, or the shipped state DB has the kill switch engaged

Usage
  python3 tools/operator_paper_daily.py --repo . [--json artifacts/x.json]
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sqlite3
import sys
from typing import Any, Dict, List, Optional, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

LADDER_KEYS = (
    "1_setups", "2_flagged", "3_eligible_and_flagged",
    "4_candidates_after_schedule", "5_entries_taken", "6_closed_trades",
)


def _truthy(v: Optional[str], default: bool) -> bool:
    if v is None or v == "":
        return default
    return str(v).strip().lower() in {"1", "true", "yes", "on"}


def preflight(env: Dict[str, str], repo: str) -> Tuple[str, List[str]]:
    """Returns (verdict, reasons). Never mutates anything."""
    reasons: List[str] = []
    use_testnet = _truthy(env.get("USE_TESTNET"), True)
    paper = _truthy(env.get("PAPER_TRADING"), True)
    if not use_testnet and not paper:
        return "LIVE_REQUESTED", ["USE_TESTNET=0 and PAPER_TRADING=0: this tool "
                                  "is a paper operator and refuses to run"]
    if env.get("LIVE_TRADING_ACK"):
        reasons.append("LIVE_TRADING_ACK is set in a paper run; unset it. An ack "
                       "lying around is how the fourth flag gets flipped by habit")
    if os.path.exists(os.path.join(repo, "models", "current")):
        reasons.append("models/current is present; a learned policy is staged. "
                       "The paper operator refuses until a human removes it")
    db = env.get("STATE_DB_PATH") or os.path.join(repo, "state", "trading_state.db")
    engaged, why = kill_switch_state(db)
    if engaged:
        reasons.append(f"kill switch engaged in {db} ({why}); a human must clear "
                       f"it or delete the fixture DB")
    return ("PREFLIGHT_FAIL" if reasons else "SANDBOX_OK"), reasons


def kill_switch_state(db_path: str) -> Tuple[bool, str]:
    if not os.path.isfile(db_path):
        return False, "no state db"
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        row = con.execute("SELECT engaged, reason FROM kill_switch LIMIT 1").fetchone()
        con.close()
    except Exception as exc:  # noqa: BLE001
        return False, f"unreadable ({exc})"
    if not row:
        return False, "no row"
    return bool(row[0]), str(row[1] or "")


def newest_shadow_artifact(repo: str) -> Optional[str]:
    paths = glob.glob(os.path.join(repo, "artifacts", "slice*_forward_shadow.json"))
    if not paths:
        return None

    def key(p: str) -> int:
        base = os.path.basename(p)
        digits = "".join(ch for ch in base.split("_")[0] if ch.isdigit())
        return int(digits or 0)
    return max(paths, key=key)


def read_ladder(path: str) -> Dict[str, Any]:
    with open(path, encoding="utf-8") as fh:
        art = json.load(fh)
    ladder = art.get("state_ladder", {}) or {}
    return {
        "artifact": os.path.relpath(path),
        "observed_at_utc": art.get("observed_at_utc"),
        "ladder": [int(ladder.get(k, 0) or 0) for k in LADDER_KEYS],
        "forward_n_trades": art.get("forward_n_trades"),
        "forward_mean_net_r": art.get("forward_mean_net_r"),
        "is_stage1_evidence": art.get("is_stage1_evidence"),
        "constants_fingerprint": art.get("constants_fingerprint"),
        "fund_abs": art.get("fund_abs"),
        "git_commit": art.get("git_commit"),
        "slice_field_in_artifact": art.get("slice"),
    }


def project_status_dict(repo: str) -> Dict[str, Any]:
    try:
        import project_status as ps
        return ps.current(artifact_dir=os.path.join(repo, "artifacts")).as_dict()
    except Exception as exc:  # noqa: BLE001
        return {"error": f"project_status unreadable: {exc}"}


def gate_dict(repo: str) -> Dict[str, Any]:
    try:
        import promotion_gate as pg
        verdict = pg.evaluate_promotion_gate(repo=repo)
        return {
            "allows_live": bool(verdict.allows_live),
            "items_complete": sum(1 for i in verdict.items if i.complete),
            "items_total": len(verdict.items),
            "gate_path": pg.GATE_PATH,
            "reasons": list(verdict.reasons)[:8],
        }
    except Exception as exc:  # noqa: BLE001
        return {"allows_live": False, "error": f"gate unreadable ({exc}); REFUSE"}


def run(repo: str, env: Dict[str, str]) -> Tuple[int, Dict[str, Any]]:
    verdict, reasons = preflight(env, repo)
    report: Dict[str, Any] = {
        "tool": "operator_paper_daily", "one_shot": True, "daemon": False,
        "preflight": verdict, "preflight_reasons": reasons,
        "live_authorized": False,
        "closer_to_autonomous_profit_agent": False,
    }
    if verdict == "LIVE_REQUESTED":
        return 2, report
    report["project_status"] = project_status_dict(repo)
    report["promotion_gate"] = gate_dict(repo)
    newest = newest_shadow_artifact(repo)
    report["ladder"] = read_ladder(newest) if newest else {"artifact": None}
    try:
        import config as _config
        allowed, why = _config.is_live_authorized(_config.load(env))
        report["live_authorized"] = bool(allowed)
        report["live_reason"] = why
    except Exception as exc:  # noqa: BLE001
        report["live_reason"] = f"config unreadable ({exc})"
    return (3 if verdict == "PREFLIGHT_FAIL" else 0), report


def three_lines(report: Dict[str, Any]) -> str:
    lad = report.get("ladder", {}) or {}
    ladder = "/".join(str(x) for x in lad.get("ladder", [])) or "n/a"
    gate = report.get("promotion_gate", {}) or {}
    return "\n".join([
        f"preflight={report['preflight']} live_authorized={report['live_authorized']}",
        f"gate_allows_live={gate.get('allows_live', False)} "
        f"items={gate.get('items_complete', '?')}/{gate.get('items_total', '?')}",
        f"ladder(setups/flags/eligible/candidates/entries/CLOSED)={ladder} "
        f"n_trades={lad.get('forward_n_trades')} mean_net_r={lad.get('forward_mean_net_r')} "
        f"closer_to_autonomous_profit_agent=False",
    ])


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", default=ROOT)
    ap.add_argument("--json", default="", help="write the full report here")
    args = ap.parse_args(argv)
    code, report = run(os.path.abspath(args.repo), dict(os.environ))
    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2, sort_keys=True)
    print(three_lines(report))
    for r in report.get("preflight_reasons", []):
        print("  refuse:", r)
    return code


if __name__ == "__main__":
    sys.exit(main())
