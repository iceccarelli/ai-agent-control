#!/usr/bin/env bash
# One READ-ONLY health report of the deployed testnet runner (Fly app carry-drill):
# the machine, its secrets (names only), testnet reachability, the monitor history
# (is it cycling, how often was the entry gate open, why not) and the forward
# shadow book. It deploys, starts, stops and orders nothing.
#
#   scripts/runner_status.sh                  # last 48 cycles
#   LAST=0 scripts/runner_status.sh           # every cycle
#   FLY_APP=other scripts/runner_status.sh
#
# Exit 0 only if the runner checks and the monitor history are healthy; the
# monitor history's own exit codes (1 stale/failed, 2 no data, 3 alarm) are kept.
# FLY=<path> overrides the fly binary (the tests put a fake one there).
set -u
APP="${FLY_APP:-carry-drill}"
FLY="${FLY:-fly}"
LAST="${LAST:-48}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TOOLS="$HERE/../bot/tools"
PY="${PYTHON:-python3}"
rc=0

echo "== runner (machine, volume, secrets, testnet reachability)"
"$HERE/verify_drill_runner.sh" 2>&1 | sed -e '/^== latest run/,$d'
[ "${PIPESTATUS[0]}" = 0 ] || rc=1

echo
echo "== monitor history (last ${LAST:-all} cycles)"
hist="$($FLY ssh console -a "$APP" -C "cat /data/monitor/monitor.jsonl" 2>/dev/null)"
args=()
[ "$LAST" != "0" ] && args+=(--last "$LAST")
printf '%s\n' "$hist" | $PY "$TOOLS/monitor_summary.py" "${args[@]}"
code=$?
[ "$code" = 0 ] || rc=$code

echo
echo "== forward shadow book"
rep="$($FLY ssh console -a "$APP" -C "setpriv --reuid=bot --regid=bot --init-groups python3 carry_shadow.py forward-report --db /data/shadow/post_gate_v1.db" 2>/dev/null)"
printf '%s\n' "$rep" | $PY -c '
import json, sys
try:
    r = json.loads(sys.stdin.read())
except ValueError:
    print("  forward-report unavailable (no book yet, or the runner is down)")
    sys.exit(0)
s, b = r["summary"], r["book"]
print("  book %s rule %s boundary %s" % (b["book_id"], b["rule_id"], b["boundary_utc"]))
print("  observations %s over %s days; entry-gate refusals %s" % (
    s["observations"], s["window_days"], s["entry_gate_refusals"] or "none"))
print("  simulated entries %s, closed trades %s; verdict %s" % (
    s["simulated_entries"], s["closed_trades"], s["verdict"]))
print("  pnl_claim_allowed: %s (the net is a partial model, never account P&L)" % s["pnl_claim_allowed"])
'
echo
[ "$rc" = 0 ] && echo "STATUS OK" || echo "STATUS NOT OK (exit $rc): read the lines marked !! or FAIL"
exit "$rc"
