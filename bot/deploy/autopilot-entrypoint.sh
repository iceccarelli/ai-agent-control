#!/bin/sh
# Fly volumes mount root-owned. Create the autopilot's home layout on the volume, fix the owner and the file modes, then drop to the unprivileged `bot` user and exec, so SIGTERM from
# Fly reaches the autopilot itself (the exec'd process): it engages the active session's kill switch, the session unwinds reduce-only and verifies flat, then the process ends.
set -eu
# Round 22: fail closed. An executor image that cannot prove what it is (git sha, closure, dependencies, role) does not start: it must be the TESTNET_EXECUTOR build of a known commit.
python3 /app/tools/runtime_provenance.py verify --root /app --expect TESTNET_EXECUTOR >&2 || { echo "REFUSED: this image's provenance is not established; the autopilot does not start (RUNBOOKS: Runtime provenance)" >&2; exit 78; }
mkdir -p /data/.ai-agent-control/ms_autopilot
chown -R bot:bot /data
chmod 700 /data/.ai-agent-control /data/.ai-agent-control/ms_autopilot
# the owner puts these two files here (RUNBOOKS: Fly autopilot): a file that arrives root-owned or world-readable must not make the autopilot refuse for the wrong reason
for f in /data/.ai-agent-control/ms_testnet_capability.json /data/.ai-agent-control/ms_autopilot_grant.json; do
  if [ -f "$f" ]; then chown bot:bot "$f"; chmod 600 "$f"; fi
done
exec setpriv --reuid=bot --regid=bot --init-groups "$@"
