#!/bin/sh
# REAL container checks for the carry-research image (needs a Docker daemon; run from bot/):   sh deploy/research-image-smoke.sh [image-tag]
# Offline: no network service, no credential. A failure of ANY check fails the script.
set -eu
IMG="${1:-carry-research-smoke}"
VOL="$(mktemp -d)"
fail() { echo "FAIL: $*" >&2; exit 1; }
cleanup() { docker run --rm -v "$VOL":/data --entrypoint chown "$IMG" -R "$(id -u)":"$(id -g)" /data >/dev/null 2>&1 || true; rm -rf "$VOL" 2>/dev/null || true; }
trap cleanup EXIT

echo "== build (no build args, no secrets)"
docker build -f Dockerfile.research -t "$IMG" .

echo "== the real entrypoint: unprivileged user, private layout on the volume"
OUT="$(docker run --rm -v "$VOL":/data "$IMG" sh -c 'id -un; stat -c "%U %a" /data/research /data/datasets; echo "$HOME"')"
[ "$(echo "$OUT" | sed -n 1p)" = "bot" ] || fail "the worker does not run as bot: $OUT"
[ "$(echo "$OUT" | sed -n 2p)" = "bot 700" ] || fail "/data/research is not bot:700: $OUT"
[ "$(echo "$OUT" | sed -n 4p)" = "/data" ] || fail "HOME is not /data: $OUT"

echo "== no credential, no venue module, no exchange name in the image"
ENVS="$(docker inspect --format '{{range .Config.Env}}{{println .}}{{end}}' "$IMG")"
echo "$ENVS" | grep -Eiq 'BYBIT|SUPABASE|API_KEY|API_SECRET|LIVE_TRADING|USE_TESTNET|TOKEN|PASSWORD' && fail "a credential or venue variable is baked into the image environment"
docker run --rm --entrypoint sh "$IMG" -c 'ls /app/tools' | sort | tr '\n' ' ' | grep -Fxq 'agent_contracts.py dataset_inventory.py research_jobs.py research_service.py ' || fail "unexpected modules in /app/tools"
docker run --rm --entrypoint sh "$IMG" -c 'ls /app' | grep -Eq 'bybit|venues|ms_testnet' && fail "a venue module is in the image"

echo "== the worker runs one tick against real data on the volume, finds the gap and writes status + a private job queue"
docker run --rm -v "$VOL":/data --entrypoint sh "$IMG" -c 'mkdir -p /data/datasets/md-1 && for d in 20250101 20250102 20250105; do echo x > /data/datasets/md-1/BTCUSDT-$d.jsonl; done'
docker run --rm -v "$VOL":/data "$IMG" python3 tools/research_service.py run --state-dir /data/research --root datasets=/data/datasets --root backups=/data/backups --once >/dev/null
OUT="$(docker run --rm -v "$VOL":/data "$IMG" sh -c 'stat -c "%U %a" /data/research/status.json /data/research/jobs.sqlite; grep -c DATA_GAP /data/research/proposals/proposals.jsonl')"
[ "$(echo "$OUT" | sed -n 1p)" = "bot 600" ] || fail "status.json is not bot:600: $OUT"
[ "$(echo "$OUT" | sed -n 2p)" = "bot 600" ] || fail "jobs.sqlite is not bot:600: $OUT"
[ "$(echo "$OUT" | sed -n 3p)" -ge 1 ] || fail "no DATA_GAP proposal for the missing days: $OUT"
docker run --rm -v "$VOL":/data "$IMG" python3 -c 'import json; s=json.load(open("/data/research/status.json")); assert s["capabilities"]=={"orders":False,"exchange_credentials":False,"network":False}; print("status-ok")' | grep -q status-ok || fail "status does not declare no capabilities"

echo "== SIGTERM reaches the worker and it stops cleanly and quickly"
CID="$(docker run -d -v "$VOL":/data "$IMG" python3 tools/research_service.py run --state-dir /data/research --root datasets=/data/datasets --tick-seconds 3600)"
for i in 1 2 3 4 5 6 7 8 9 10; do docker exec "$CID" sh -c 'test -f /data/research/status.json' 2>/dev/null && break; [ "$i" = 10 ] && fail "the worker never wrote a status file"; done
docker exec "$CID" sh -c 'test "$(awk "/^Uid:/ {print \$2}" /proc/1/status)" = "$(id -u bot)"' || fail "PID 1 is not running as bot"
T0="$(date +%s)"
docker stop -t 20 "$CID" >/dev/null
T1="$(date +%s)"
[ "$(docker inspect --format '{{.State.ExitCode}}' "$CID")" != "137" ] || fail "the worker was SIGKILLed instead of stopping on SIGTERM"
[ $((T1 - T0)) -lt 15 ] || fail "the worker took $((T1 - T0))s to stop"
docker rm -f "$CID" >/dev/null 2>&1 || true
echo "ALL RESEARCH IMAGE CHECKS PASSED"
