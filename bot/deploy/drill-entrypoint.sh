#!/bin/sh
# Fly volumes mount root-owned. Fix the owner, then drop to the unprivileged
# `bot` user and exec, so SIGTERM from Fly reaches the orchestrator (PID of the
# exec'd process) and its safety-net close runs before the machine stops.
set -eu
mkdir -p /data/runs /data/monitor /data/shadow /data/mainnet
chown -R bot:bot /data
exec setpriv --reuid=bot --regid=bot --init-groups "$@"
