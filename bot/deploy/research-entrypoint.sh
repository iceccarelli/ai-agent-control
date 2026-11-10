#!/bin/sh
# Fly volumes mount root-owned. Create the research layout on the volume, fix the owner, then drop to the unprivileged `bot` user and exec (SIGTERM from Fly reaches the worker itself).
# The research worker holds no credential and cannot place an order; the data directories are READ by it (the collector fills them), never written.
set -eu
mkdir -p /data/research /data/datasets /data/backups
chown -R bot:bot /data
chmod 700 /data/research
exec setpriv --reuid=bot --regid=bot --init-groups "$@"
