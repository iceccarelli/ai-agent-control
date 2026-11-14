#!/bin/sh
# Fly volumes mount root-owned. Fix the owner, then drop to the unprivileged
# `bot` user and exec, so SIGTERM from Fly reaches the orchestrator (PID of the
# exec'd process) and its safety-net close runs before the machine stops.
set -eu
# Round 22: fail closed (its report goes to stderr: the container's stdout belongs to the command it runs). An image that cannot prove what it is (git sha, closure, dependencies, role) does not record: a recorder whose implementation is unknown cannot be trusted as evidence.
python3 /app/tools/runtime_provenance.py verify --root /app --expect READ_ONLY_RECORDER >&2 || { echo "REFUSED: this image's provenance is not established; the recorder does not start (RUNBOOKS: Runtime provenance)" >&2; exit 78; }
mkdir -p /data/runs /data/monitor /data/shadow /data/mainnet /data/market
chown -R bot:bot /data
exec setpriv --reuid=bot --regid=bot --init-groups "$@"
