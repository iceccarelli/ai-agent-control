#!/usr/bin/env bash
# MACHINE B — TRADING EXECUTION HOST
# Identity check: must have ai-agent-control repo. Must NOT be the ecowoods OpenClaw brain.
set -euo pipefail
REPO="${AAC_REPO:-$HOME/iceccarelli-factory/ai-agent-control}"
BOT="$REPO/bot"
PY="$REPO/.venv/bin/python"

echo "=== MACHINE B fingerprint ==="
scutil --get LocalHostName 2>/dev/null || true
hostname
test -d "$BOT" || { echo "FATAL: $BOT missing — wrong machine"; exit 1; }
if test -d "$HOME/ecowoods-factory/workspaces/ecowoods-builder"; then
  echo "WARN: ecowoods builder exists on this host — confirm this is still intended as EXECUTION machine"
fi

echo "=== 1) Execution invariants ==="
"$PY" - <<'PY'
from pathlib import Path
bot = Path(".").resolve()
# run from bot/
import os
os.chdir(os.environ.get("BOT_DIR", "."))
PY
cd "$BOT"
"$PY" tools/reviewer_verdict.py --dry-run >/tmp/aac_reviewer_dry.txt || true
"$PY" tools/control_plane_tick.py >/tmp/aac_tick.txt || true
echo "dry-run+tick done"
"$PY" - <<'PY'
import json
from pathlib import Path
v=json.loads(Path("artifacts/reviewer_verdict.json").read_text())
print("forward", v["stage_b"]["forward_n_trades"], "/", v["stage_b"]["of_20"])
print("allows_progress", v.get("allows_progress"), "key", v.get("key_status"))
print("next:", *v.get("next_actions", [])[:3], sep="\n  ")
PY

echo "=== 2) Crons (execution only) ==="
crontab -l 2>/dev/null | grep -E 'daily_forward_refresh|control_plane_tick' || {
  echo "Installing execution crons..."
  (crontab -l 2>/dev/null | grep -v daily_forward_refresh | grep -v control_plane_tick
   echo "7 1 * * * cd $BOT && $PY tools/daily_forward_refresh.py >> $BOT/state/forward_refresh.log 2>&1"
   echo "17 * * * * cd $BOT && $PY tools/control_plane_tick.py >> $BOT/state/control_plane_tick.log 2>&1"
  ) | crontab -
}
crontab -l | grep -E 'daily_forward_refresh|control_plane_tick'

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
MD

echo "=== 4) Push role doc if clean enough ==="
cd "$REPO"
git add bot/docs/human/MACHINE_B_EXECUTION.md scripts/REWIRE_MACHINE_B_EXECUTION.sh || true
echo "MACHINE B ready. Pull on MACHINE A after commit."
echo "DONE MACHINE B"
