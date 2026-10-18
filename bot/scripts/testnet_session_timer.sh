#!/usr/bin/env bash
# Timer: reruns bot/tools/testnet_session.py on an interval. Nothing more —
# no scheduler, no state machine. Each tick is the same production session.
#
# Usage: bot/scripts/testnet_session_timer.sh [interval_seconds] [max_ticks]
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BOT_DIR="$(dirname "$HERE")"

# One timer at a time: two overlapping loops would run two sessions against the
# same state database and symbol.
LOCK_FILE="${TESTNET_SESSION_TIMER_LOCK:-/tmp/testnet_session_timer.lock}"
exec 9>"${LOCK_FILE}"
if ! flock -n 9; then
    echo "[testnet_session_timer] another timer holds ${LOCK_FILE}; refusing to start" >&2
    exit 1
fi

INTERVAL_SECONDS="${1:-900}"
MAX_TICKS="${2:-0}"

tick=0
while true; do
    tick=$((tick + 1))
    echo "[testnet_session_timer] tick ${tick} at $(date -u +%Y-%m-%dT%H:%M:%SZ)"

    set +e
    BYBIT_VENUE=testnet PAPER_TRADING=0 python3 "${BOT_DIR}/tools/testnet_session.py"
    rc=$?
    set -e

    echo "[testnet_session_timer] tick ${tick} exit=${rc}"

    if [ "${MAX_TICKS}" -gt 0 ] && [ "${tick}" -ge "${MAX_TICKS}" ]; then
        echo "[testnet_session_timer] reached max_ticks=${MAX_TICKS}; stopping"
        break
    fi

    sleep "${INTERVAL_SECONDS}"
done
