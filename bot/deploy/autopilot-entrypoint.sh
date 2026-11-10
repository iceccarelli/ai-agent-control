#!/bin/sh
# Fly volumes mount root-owned. Create the autopilot's home layout on the volume, fix the owner and the file modes, then drop to the unprivileged `bot` user and exec, so SIGTERM from
# Fly reaches the autopilot itself (the exec'd process): it engages the active session's kill switch, the session unwinds reduce-only and verifies flat, then the process ends.
set -eu
mkdir -p /data/.ai-agent-control/ms_autopilot
chown -R bot:bot /data
chmod 700 /data/.ai-agent-control /data/.ai-agent-control/ms_autopilot
# the owner puts these two files here (RUNBOOKS: Fly autopilot): a file that arrives root-owned or world-readable must not make the autopilot refuse for the wrong reason
for f in /data/.ai-agent-control/ms_testnet_capability.json /data/.ai-agent-control/ms_autopilot_grant.json; do
  if [ -f "$f" ]; then chown bot:bot "$f"; chmod 600 "$f"; fi
done
exec setpriv --reuid=bot --regid=bot --init-groups "$@"
