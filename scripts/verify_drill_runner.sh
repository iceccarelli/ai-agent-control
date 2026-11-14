#!/usr/bin/env bash
# READ-ONLY check of the deployed drill machine (Fly app carry-drill): its role is the read-only recorder and monitor.
#
# It deploys nothing, starts nothing, stops nothing and sends no order. It reads
# `fly` listings, asks the machine to fetch the public testnet time endpoint,
# and prints the latest run's orchestrator log and verdict. Secret VALUES are
# never read: only the names `fly secrets list` prints.
#
#   scripts/verify_drill_runner.sh            # app carry-drill
#   FLY_APP=other scripts/verify_drill_runner.sh
#
# Exit 0 only if every check passed. FLY=<path> overrides the fly binary (the
# tests put a fake one there).
set -u
APP="${FLY_APP:-carry-drill}"
FLY="${FLY:-fly}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TABLE="python3 $HERE/../bot/tools/fly_machines.py"
bad=0
fail() { echo "FAIL: $*"; bad=1; }

echo "== machines"
machines="$($FLY machine list -a "$APP" 2>&1)"
n="$(printf '%s\n' "$machines" | $TABLE count)"
echo "machines: $n"
[ "$n" = "1" ] || fail "expected exactly 1 machine, found $n (two runners would be two hedges)"
state="$(printf '%s\n' "$machines" | $TABLE states | head -1)"
echo "state: ${state:-none}"
[ "$state" = "started" ] || fail "machine is not started"

echo "== volumes"
v="$($FLY volumes list -a "$APP" 2>&1 | $TABLE count --where NAME=drill_runs)"
echo "drill_runs volumes: $v"
[ "$v" = "1" ] || fail "expected exactly 1 drill_runs volume, found $v"

echo "== secrets (names only)"
# Names only, never values, and a name never says which account a key reaches. The dedicated Testnet
# credential names are BYBIT_TESTNET_API_KEY / BYBIT_TESTNET_API_SECRET. The generic BYBIT_API_KEY /
# BYBIT_API_SECRET on the 2026-11-05 machine are NOT Testnet credentials in this repository: they are
# reported, with a warning, and they are never accepted as the dedicated pair.
secrets="$($FLY secrets list -a "$APP" 2>&1)"
has() { printf '%s\n' "$secrets" | grep -qw "$1"; }
if has BYBIT_TESTNET_API_KEY && has BYBIT_TESTNET_API_SECRET; then
  echo "dedicated Testnet credential names: present"
elif has BYBIT_TESTNET_API_KEY || has BYBIT_TESTNET_API_SECRET; then
  fail "only half of the dedicated Testnet pair (BYBIT_TESTNET_API_KEY / BYBIT_TESTNET_API_SECRET) is set on $APP"
elif has BYBIT_API_KEY && has BYBIT_API_SECRET; then
  echo "dedicated Testnet credential names (BYBIT_TESTNET_API_KEY / BYBIT_TESTNET_API_SECRET): not present"
  echo "WARNING: generic BYBIT_API_KEY / BYBIT_API_SECRET names are present. They are not Testnet credentials in this repository,"
  echo "         and the names do not say which account they reach. Treat this app as a READ-ONLY recorder and monitor."
elif has BYBIT_API_KEY || has BYBIT_API_SECRET; then
  fail "only half of the generic pair (BYBIT_API_KEY / BYBIT_API_SECRET) is set on $APP"
else
  fail "no Bybit credential name is set on $APP (a read-only recorder needs none, but the monitor's venue reads do)"
fi

echo "== can the machine reach the testnet?"
reach="$($FLY ssh console -a "$APP" -C "python3 -c \"import urllib.request as u,urllib.error as e
try: print('HTTP', u.urlopen('https://api-testnet.bybit.com/v5/market/time',timeout=15).status)
except e.HTTPError as x: print('HTTP', x.code)
except Exception as x: print('ERR', type(x).__name__, x)\"" 2>&1 | tail -1)"
echo "$reach"
[ "$reach" = "HTTP 200" ] || fail "testnet not reachable from the runner"

echo "== latest run"
$FLY ssh console -a "$APP" -C "sh -c 'd=\$(ls -d /data/runs/run-* 2>/dev/null | tail -1); echo \$d; [ -n \"\$d\" ] && cat \$d/orchestrator.log && cat \$d/verdict.json 2>/dev/null'" 2>&1 | tail -20

[ "$bad" = 0 ] && echo "OK: the runner checks passed" || echo "NOT OK: see FAIL lines"
exit "$bad"
