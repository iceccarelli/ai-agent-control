#!/usr/bin/env bash
# Timer entrypoint: reruns bot/tools/testnet_session.py on a fixed interval.
#
# This is the whole loop: wake up, run the existing session once, sleep,
# repeat. It adds no scheduling logic, no state machine, and no new
# execution path — testnet_session.py still decides no-signal vs.
# submitted vs. flattened on every tick, exactly as it does for a single
# manual run.
#
# Usage:
#   bot/scripts/testnet_session_timer.sh [interval_seconds] [max_ticks]
#
# interval_seconds defaults to 900 (15 minutes). max_ticks defaults to 0
# (run forever); pass a positive number to stop after that many ticks.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BOT_DIR="$(dirname "$HERE")"
REPO_ROOT="$(dirname "$BOT_DIR")"

INTERVAL_SECONDS="${1:-900}"
MAX_TICKS="${2:-0}"

tick=0
while true; do
    tick=$((tick + 1))
    echo "[testnet_session_timer] tick ${tick} at $(date -u +%Y-%m-%dT%H:%M:%SZ)"

    set +e
    BYBIT_VENUE=testnet python3 "${BOT_DIR}/tools/testnet_session.py"
    rc=$?
    set -e

    echo "[testnet_session_timer] tick ${tick} exit=${rc}"

    if [ "${MAX_TICKS}" -gt 0 ] && [ "${tick}" -ge "${MAX_TICKS}" ]; then
        echo "[testnet_session_timer] reached max_ticks=${MAX_TICKS}; stopping"
        break
    fi

    sleep "${INTERVAL_SECONDS}"
done
