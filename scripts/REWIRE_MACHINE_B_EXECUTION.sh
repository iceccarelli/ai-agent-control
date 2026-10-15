#!/usr/bin/env bash
# MACHINE B — TRADING EXECUTION HOST
# Identity check: must have ai-agent-control repo. Must NOT be the ecowoods OpenClaw brain.
#
# RETIRES the legacy control-plane crons; installs none.
#
# The first versions of this script installed two crons, `daily_forward_refresh`
# and `control_plane_tick`, and ran `reviewer_verdict` as a smoke check, each
# followed by `|| true`. Those tools were deleted on purpose (8d412c6: they were
# a second corpus/mission writer, and their scorer and charter docs went in
# 538f66c), so a host rewired by the old script ticked a missing file every
# hour, forever, and the `|| true` made that look like success. This version
# removes every line it owns or that names a retired job, installs nothing that
# can write corpus or missions, and FAILS if a retired job is still scheduled
# afterwards. It is idempotent: re-running it leaves the same crontab.
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

echo "=== 1) Execution stack imports ==="
# cd FIRST: the app resolves state/ and artifacts/ relative to the working
# directory. No `|| true`: a stack that does not import must stop this script.
cd "$BOT"
"$PY" -c 'import main, funding_reconcile; print("execution stack imports OK")'

echo "=== 2) Crons: retire legacy control-plane jobs (installs none) ==="
# Drop every line we own or that runs a retired job under any wrapper. Lines we
# do not own and that do not name a retired job are passed through untouched.
{
  crontab -l 2>/dev/null \
    | grep -v -F "$CRON_TAG" \
    | grep -v -E 'daily_forward_refresh|control_plane_tick|control_plane_loop\.sh|reviewer_verdict|promote_forward_shadow|stage_b_forward_accrual' \
    || true
} | crontab -

echo "--- retired jobs still scheduled (expect 0) ---"
remaining=$(crontab -l 2>/dev/null | grep -c -E 'daily_forward_refresh|control_plane_tick|control_plane_loop\.sh|reviewer_verdict|promote_forward_shadow|stage_b_forward_accrual' || true)
echo "$remaining"
[ "$remaining" -eq 0 ] || { echo "FATAL: $remaining retired cron lines remain"; exit 1; }

echo "=== 3) Role file for this host ==="
mkdir -p "$BOT/docs/human"
cat > "$BOT/docs/human/MACHINE_B_EXECUTION.md" <<'MD'
# MACHINE B — EXECUTION HOST

This computer owns:
- `ai-agent-control` git checkout
- Bybit/paper runtime (testnet only)
- Claude Code builder sessions that edit THIS repo

This computer does NOT own:
- Ecowoods OpenClaw multi-agent brain (that is MACHINE A)

Law:
- No LLM imports on order path
- allows_live only via human ACCEPT files
- OpenClaw agents on MACHINE A talk to this repo via **git push/pull**, not by sharing a filesystem

This script installs no crons. It retires the legacy control-plane crons
(`daily_forward_refresh`, `control_plane_tick`) whose tools were deleted.
MD

echo "=== 4) Stage the role doc (no commit, no push — that is the operator's) ==="
cd "$REPO"
git add bot/docs/human/MACHINE_B_EXECUTION.md scripts/REWIRE_MACHINE_B_EXECUTION.sh || true
echo "MACHINE B ready. Commit and push, then pull on MACHINE A."
echo "DONE MACHINE B"
