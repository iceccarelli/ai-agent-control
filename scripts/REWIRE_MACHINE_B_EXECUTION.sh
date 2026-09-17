#!/usr/bin/env bash
# MACHINE B — TRADING EXECUTION HOST
# Identity check: must have ai-agent-control repo. Must NOT be the ecowoods OpenClaw brain.
#
# IDEMPOTENT. Re-running this must leave exactly one cron line per job. The
# first version could not promise that: it installed crons only when NEITHER
# job was present, so a host with `daily_forward_refresh` but no
# `control_plane_tick` was never repaired, and a host already running
# `control_plane_loop.sh` (which calls the same tick) ended up ticking twice.
set -euo pipefail
REPO="${AAC_REPO:-$HOME/iceccarelli-factory/ai-agent-control}"
BOT="$REPO/bot"
PY="$REPO/.venv/bin/python"

#: Every cron line this script owns carries this tag. Ownership is what makes
#: removal safe: we strip only our own lines, never a human's.
CRON_TAG="# aac-machine-b"

echo "=== MACHINE B fingerprint ==="
scutil --get LocalHostName 2>/dev/null || true
hostname
test -d "$BOT" || { echo "FATAL: $BOT missing — wrong machine"; exit 1; }
test -x "$PY" || { echo "FATAL: $PY missing — create $REPO/.venv first"; exit 1; }
if test -d "$HOME/ecowoods-factory/workspaces/ecowoods-builder"; then
  echo "WARN: ecowoods builder exists on this host — confirm this is still intended as EXECUTION machine"
fi

echo "=== 1) Execution invariants ==="
# cd FIRST. Both tools resolve artifacts/ relative to the working directory, so
# running them from wherever the operator happened to be is how you get a
# reviewer verdict written into somebody's home directory.
cd "$BOT"
"$PY" tools/reviewer_verdict.py --dry-run >/tmp/aac_reviewer_dry.txt || true
"$PY" tools/control_plane_tick.py >/tmp/aac_tick.txt || true
echo "dry-run+tick done"
"$PY" - <<'PY'
import json
from pathlib import Path
v = json.loads(Path("artifacts/reviewer_verdict.json").read_text())
print("forward", v["stage_b"]["forward_n_trades"], "/", v["stage_b"]["of_20"])
print("allows_progress", v.get("allows_progress"), "key", v.get("key_status"))
print("next:", *v.get("next_actions", [])[:3], sep="\n  ")
PY

echo "=== 2) Crons (execution only) ==="
# Rebuild unconditionally: drop every line we own or that runs either job under
# any wrapper, then append the two canonical lines. No branch, so no partial
# state survives and no line is ever written twice.
{
  crontab -l 2>/dev/null \
    | grep -v -F "$CRON_TAG" \
    | grep -v -E 'daily_forward_refresh|control_plane_tick|control_plane_loop\.sh' \
    || true
  echo "7 1 * * * cd $BOT && $PY tools/daily_forward_refresh.py >> $BOT/state/forward_refresh.log 2>&1 $CRON_TAG"
  echo "17 * * * * cd $BOT && $PY tools/control_plane_tick.py >> $BOT/state/control_plane_tick.log 2>&1 $CRON_TAG"
} | crontab -

echo "--- installed (expect exactly 2) ---"
crontab -l | grep -E 'daily_forward_refresh|control_plane_tick'
installed=$(crontab -l | grep -c -E 'daily_forward_refresh|control_plane_tick')
[ "$installed" -eq 2 ] || { echo "FATAL: $installed cron lines, expected 2"; exit 1; }

echo "=== 3) Role file for this host ==="
mkdir -p "$BOT/docs/human"
cat > "$BOT/docs/human/MACHINE_B_EXECUTION.md" <<'MD'
# MACHINE B — EXECUTION HOST

This computer owns:
- `ai-agent-control` git checkout
- Bybit/paper runtime, corpus refresh, control_plane_tick
- Claude Code builder sessions that edit THIS repo

This computer does NOT own:
- Ecowoods OpenClaw multi-agent brain (that is MACHINE A)

Law:
- No LLM imports on order path
- allows_live only via human ACCEPT files
- OpenClaw agents on MACHINE A talk to this repo via **git push/pull**, not by sharing a filesystem

Crons owned by `scripts/REWIRE_MACHINE_B_EXECUTION.sh` are tagged `# aac-machine-b`.
Re-running the script rebuilds them; it never appends a second copy.
MD

echo "=== 4) Stage the role doc (no commit, no push — that is the operator's) ==="
cd "$REPO"
git add bot/docs/human/MACHINE_B_EXECUTION.md scripts/REWIRE_MACHINE_B_EXECUTION.sh || true
echo "MACHINE B ready. Commit and push, then pull on MACHINE A."
echo "DONE MACHINE B"
