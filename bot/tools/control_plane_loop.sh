#!/usr/bin/env bash
# control_plane_loop.sh — cron-friendly wrapper around control_plane_tick.py
#
# Default mode: `once` — one tick, then exit. Default loop interval: 60 minutes.
# Logs to artifacts/control_plane_loop.log.
# Never places orders. Never sets allows_live. Never prints secrets.
#
# Mac crontab example (every hour):
#   0 * * * * /path/to/bot/tools/control_plane_loop.sh once >>/path/to/bot/artifacts/control_plane_loop.log 2>&1
#
# Or run the looping daemon on a host that stays up:
#   /path/to/bot/tools/control_plane_loop.sh loop 60

set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
BOT="$(cd "$HERE/.." && pwd)"
TICK="$HERE/control_plane_tick.py"
LOG="$BOT/artifacts/control_plane_loop.log"
INTERVAL_MIN="${2:-60}"
# Default `once`, NOT `loop`. The dangerous default is the one that never
# returns: a cron line that forgets the argument would start a fresh
# never-ending daemon on every firing, and you would find out from the process
# table. `once` does one tick and exits, which is wrong only in the harmless
# direction. The daemon is still one word away.
MODE="${1:-once}"

mkdir -p "$BOT/artifacts"
cd "$BOT"

run_once() {
  local ts
  ts="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "---- control_plane_loop start $ts ----" >>"$LOG"
  # Prefer venv python if present; else system python3.
  local py="python3"
  if [[ -x "$BOT/.venv/bin/python" ]]; then
    py="$BOT/.venv/bin/python"
  elif [[ -x "$BOT/../.venv/bin/python" ]]; then
    py="$BOT/../.venv/bin/python"
  fi
  "$py" "$TICK" >>"$LOG" 2>&1 || echo "tick exited non-zero: $?" >>"$LOG"
  echo "---- control_plane_loop end $(date -u +%Y-%m-%dT%H:%M:%SZ) ----" >>"$LOG"
}

case "$MODE" in
  once)
    run_once
    ;;
  loop)
    if ! [[ "$INTERVAL_MIN" =~ ^[0-9]+$ ]] || [[ "$INTERVAL_MIN" -lt 1 ]]; then
      echo "interval minutes must be a positive integer" >&2
      exit 2
    fi
    while true; do
      run_once
      sleep $((INTERVAL_MIN * 60))
    done
    ;;
  *)
    echo "usage: $0 [once|loop] [interval_minutes=60]" >&2
    exit 2
    ;;
esac
