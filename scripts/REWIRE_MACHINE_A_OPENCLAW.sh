#!/usr/bin/env bash
# MACHINE A — OPENCLAW ORCHESTRATOR HOST (Grimaldis-MacBook-Pro / ecowoods brain)
# Must have: openclaw, ollama, ecowoods-factory pattern, xai plugin
# Must NOT require local ai-agent-control checkout initially — we clone to ~/aac-factory
set -euo pipefail

echo "=== MACHINE A fingerprint ==="
scutil --get LocalHostName 2>/dev/null || true
hostname
command -v openclaw >/dev/null || { echo "FATAL: openclaw missing"; exit 1; }
command -v ollama >/dev/null || { echo "FATAL: ollama missing"; exit 1; }
test -d "$HOME/ecowoods-factory" || echo "WARN: ecowoods-factory missing — continuing anyway"
if test -d "$HOME/iceccarelli-factory/ai-agent-control"; then
  echo "NOTE: execution repo also present on this host — still treat roles as logical split"
fi

FACTORY="$HOME/aac-factory"
WS_BUILD="$FACTORY/workspaces/aac-builder"
WS_REVIEW="$FACTORY/workspaces/aac-reviewer"
WS_LOCAL="$FACTORY/workspaces/aac-local-reviewer"
REPO_CLONE="$FACTORY/repos/ai-agent-control"

mkdir -p "$WS_BUILD" "$WS_REVIEW" "$WS_LOCAL" "$FACTORY/repos"

echo "=== 1) Clone / update execution repo (git bridge to MACHINE B) ==="
if test -d "$REPO_CLONE/.git"; then
  git -C "$REPO_CLONE" fetch origin
  git -C "$REPO_CLONE" checkout main
  git -C "$REPO_CLONE" pull --ff-only origin main
else
  git clone https://github.com/iceccarelli/ai-agent-control.git "$REPO_CLONE"
fi

echo "=== 2) Workspace constitutions (execution-safe) ==="
write_common() {
  local dir="$1" name="$2" emoji="$3" theme="$4" role="$5"
  cat > "$dir/IDENTITY.md" <<MD
# IDENTITY.md
- **Name:** $name
- **Emoji:** $emoji
- **Theme:** $theme
MD
  cat > "$dir/SOUL.md" <<MD
# SOUL
Role: $role
Money path: Stage B accrual then gated micro-live.
NEVER place venue orders. NEVER set allows_live. NEVER forge human gate signatures.
Execution bot on MACHINE B is the only order path.
MD
  cat > "$dir/AGENTS.md" <<MD
# AGENTS.md — $name

## Mission
$role for github.com/iceccarelli/ai-agent-control

## Startup
1. Read \`repos/ai-agent-control/bot/artifacts/NEXT_MISSION.md\` if present (via $REPO_CLONE)
2. Read \`bot/artifacts/reviewer_verdict.json\` and \`forward_shadow_current.json\`
3. Obey AGENT_CONTROL_PLANE.md role walls

## Hard bans
- No Bybit order placement
- No allows_live / LIVE_TRADING_ACK
- No cap raises
- No OOS re-score / threshold edits
- If blocked: write ASK_GROK.md facts + question into the repo and stop

## Cross-machine
MACHINE B runs crons and Claude Code on the live checkout.
You work on the clone at $REPO_CLONE and sync via git.
MD
}

write_common "$WS_BUILD" "AAC Builder" "🔧" "Claude builder for trading bot" "BUILDER — implement NO-YIELD missions in the repo"
write_common "$WS_REVIEW" "AAC Reviewer" "🛡️" "xAI Grok adversarial reviewer" "REVIEWER — off-path verdicts only; never execution"
write_common "$WS_LOCAL" "AAC Local Reviewer" "🔎" "Ollama offline reviewer" "LOCAL REVIEWER — same contract as Grok; offline"

# Point builder workspace tools at clone
ln -sfn "$REPO_CLONE" "$WS_BUILD/repo"
ln -sfn "$REPO_CLONE" "$WS_REVIEW/repo"
ln -sfn "$REPO_CLONE" "$WS_LOCAL/repo"

echo "=== 3) Register OpenClaw agents (non-interactive) ==="
# Builder uses Claude via claude-cli like ecowoods-builder
openclaw agents add aac-builder --non-interactive --workspace "$WS_BUILD" --model "claude-cli/claude-sonnet-5" || openclaw agents add aac-builder --non-interactive --workspace "$WS_BUILD" --model "anthropic/claude-sonnet-4" || true
openclaw agents set-identity --agent aac-builder --name "AAC Builder" --emoji "🔧" || true

# Reviewer = xAI Grok (same family as ecowoods-opportunity)
openclaw agents add aac-reviewer --non-interactive --workspace "$WS_REVIEW" --model "xai/grok-4.6" || true
openclaw agents set-identity --agent aac-reviewer --name "AAC Reviewer" --emoji "🛡️" || true

# Local reviewer = Ollama
openclaw agents add aac-local-reviewer --non-interactive --workspace "$WS_LOCAL" --model "ollama/llama3:8b" || openclaw agents add aac-local-reviewer --non-interactive --workspace "$WS_LOCAL" --model "ollama/qwen2.5:7b" || true
openclaw agents set-identity --agent aac-local-reviewer --name "AAC Local Reviewer" --emoji "🔎" || true

echo "=== 4) Enable agent-to-agent allowlist for AAC trio ==="
# Patch config safely: add to tools.agentToAgent.allow
python3 - <<'PY'
import json
from pathlib import Path
p = Path.home() / ".openclaw" / "openclaw.json"
d = json.loads(p.read_text())
t = d.setdefault("tools", {}).setdefault("agentToAgent", {})
t["enabled"] = True
allow = list(t.get("allow") or [])
for a in ("aac-builder", "aac-reviewer", "aac-local-reviewer"):
    if a not in allow:
        allow.append(a)
t["allow"] = allow
# keep existing ecowoods entries
p.write_text(json.dumps(d, indent=2) + "\n")
print("allow=", allow)
PY

echo "=== 5) Smoke turns (no orders) ==="
openclaw health || true
openclaw agents list | grep -E 'aac-|Agents:' || openclaw agents list

# Reviewer dry mission via message file
MSG="$FACTORY/tmp_review_msg.txt"
mkdir -p "$FACTORY"
cat > "$MSG" <<'MD'
Read repo/bot/artifacts/reviewer_verdict.json and forward_shadow_current.json if present.
Return ONLY: forward_n_trades x/20, allows_live must be false, top 3 next_actions.
Do not suggest placing orders. Do not set allows_live.
MD

echo "Running aac-reviewer turn (may need gateway)..."
openclaw agent --agent aac-reviewer --message-file "$MSG" --timeout 180 --json > "$FACTORY/last_reviewer_turn.json" 2>"$FACTORY/last_reviewer_turn.err" || {
  echo "Reviewer turn failed — see $FACTORY/last_reviewer_turn.err"
  tail -40 "$FACTORY/last_reviewer_turn.err" || true
}

echo "=== 6) Cron on MACHINE A: pull + review hourly (git bridge) ==="
# OpenClaw cron if available; else launchd/crontab pull
(crontab -l 2>/dev/null | grep -v aac-factory-pull
 echo "20 * * * * cd $REPO_CLONE && git pull --ff-only origin main >> $FACTORY/pull.log 2>&1"
 echo "25 * * * * openclaw agent --agent aac-reviewer --message-file $FACTORY/tmp_review_msg.txt --timeout 180 >> $FACTORY/reviewer.log 2>&1"
) | crontab -

echo "DONE MACHINE A"
echo "Next: on MACHINE B run scripts/REWIRE_MACHINE_B_EXECUTION.sh"
echo "Builder attach on MACHINE B Claude Code: keep editing the live repo; OpenClaw builder uses the clone via git."
