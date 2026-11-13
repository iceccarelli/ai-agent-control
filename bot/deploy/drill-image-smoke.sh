#!/bin/sh
# REAL container checks for the drill image (Dockerfile.drill: the recorder + monitor of the Fly app carry-drill). Needs a Docker daemon; run from bot/:   sh deploy/drill-image-smoke.sh [image-tag]
# Offline and secret-free: every container runs with `--network none`, so nothing here can contact Bybit or any service, and no credential is used. A failure of ANY check fails the script.
#
# WHY: this is the image that records the order book, and it was the one image with no real-container validation. Its static tests (test_drill_image.py, test_image_manifest.py) recompute
# the import closure, but only a real container proves that the pinned dependencies are installed, that the entrypoint drops to an unprivileged user, that the recorder writes to the
# volume and stops on SIGTERM, and that a machine started without credentials does not run. A merged PR does not change a running machine: this validates the BUILT image, not the source.
set -eu
IMG="${1:-carry-drill-smoke}"
VOL="$(mktemp -d)"
VOL2="$(mktemp -d)"
CID=""
fail() { echo "FAIL: $*" >&2; exit 1; }
cleanup() {
  [ -z "$CID" ] || docker rm -f "$CID" >/dev/null 2>&1 || true
  for v in "$VOL" "$VOL2"; do docker run --rm --network none -v "$v":/data --entrypoint chown "$IMG" -R "$(id -u)":"$(id -g)" /data >/dev/null 2>&1 || true; done
  rm -rf "$VOL" "$VOL2" 2>/dev/null || true
}
trap cleanup EXIT

echo "== build (no build args, no secrets)"
docker build -f Dockerfile.drill -t "$IMG" .

echo "== the tools the entrypoint needs exist"
docker run --rm --network none --entrypoint sh "$IMG" -c 'command -v setpriv && command -v python3' >/dev/null || fail "setpriv / python3 missing from the image"

echo "== the real entrypoint: unprivileged user, every volume directory owned by it"
OUT="$(docker run --rm --network none -v "$VOL":/data "$IMG" sh -c 'id -un; stat -c "%U" /data/market /data/monitor /data/runs /data/shadow /data/mainnet | sort -u; echo "$USE_TESTNET"')"
[ "$(echo "$OUT" | sed -n 1p)" = "bot" ] || fail "the worker does not run as bot: $OUT"
[ "$(echo "$OUT" | sed -n 2p)" = "bot" ] || fail "a volume directory is not owned by bot: $OUT"
[ "$(echo "$OUT" | sed -n 3p)" = "1" ] || fail "USE_TESTNET is not 1 inside the container: $OUT"

echo "== every long-running entry point imports inside the REAL image (the static closure test cannot see a missing pinned dependency)"
docker run --rm --network none -e PYTHONPATH=/app:/app/tools "$IMG" python3 -c '
import runner_supervisor, runner_monitor, market_recorder, md_store, md_seal, runner_alerts, state_backup, anchor_archive
print("imports-ok")' | grep -q imports-ok || fail "a recorder / monitor / supervisor module does not import in the image"

echo "== the recorder runs OFFLINE in the real image, writes its segment and status to the volume, and stops cleanly on SIGTERM"
CID="$(docker run -d --network none -v "$VOL2":/data "$IMG" python3 tools/market_recorder.py --dir /data/market --environment testnet --no-rest --min-free-mb 1 --status-every 2)"
N=0
# the status file appears at start and is rewritten every --status-every seconds: wait for the one that has looked at its (absent) feed, which must say DISCONNECTED
until docker exec "$CID" sh -c 'grep -q DISCONNECTED /data/market/recorder_status.json 2>/dev/null && ls /data/market/md-*.db >/dev/null 2>&1'; do
  N=$((N + 1)); [ "$N" -lt 40 ] || { docker logs "$CID" 2>&1 | tail -20 >&2; fail "an offline recorder wrote no status that says DISCONNECTED (it must never look healthy without a feed) and a segment within 40 s"; }
  [ "$(docker inspect -f '{{.State.Running}}' "$CID")" = "true" ] || { docker logs "$CID" 2>&1 | tail -20 >&2; fail "the recorder exited before it wrote its status"; }
  sleep 1
done
docker exec "$CID" sh -c 'test "$(awk "/^Uid:/ {print \$2}" /proc/1/status)" = "$(id -u bot)"' || fail "PID 1 (the recorder) is not running as bot"
START="$(date +%s)"
docker stop -t 30 "$CID" >/dev/null
ELAPSED=$(( $(date +%s) - START ))
CODE="$(docker inspect -f '{{.State.ExitCode}}' "$CID")"
[ "$CODE" = "0" ] || fail "the recorder did not exit cleanly on SIGTERM (exit code $CODE; 137 means it was killed)"
[ "$ELAPSED" -lt 20 ] || fail "the recorder took ${ELAPSED}s to stop on SIGTERM"

echo "== the default command does NOT run without credentials (fail closed) and places nothing"
set +e
DEF="$(docker run --rm --network none -v "$VOL":/data "$IMG" 2>&1)"
DEFCODE=$?
set -e
[ "$DEFCODE" != "0" ] || fail "the default command ran to completion without credentials: $DEF"
echo "$DEF" | grep -qi "refusing" || fail "the default command did not refuse in words (exit $DEFCODE): $DEF"

echo "== no credential or live switch in the image; testnet only; the testnet host cannot be changed by environment"
ENVS="$(docker run --rm --network none --entrypoint env "$IMG")"
echo "$ENVS" | grep -Eq '^(BYBIT_API_KEY|BYBIT_API_SECRET|BYBIT_TESTNET_API_KEY|BYBIT_TESTNET_API_SECRET|LIVE_TRADING_ACK)=' && fail "a credential or live switch is in the image environment"
echo "$ENVS" | grep -q '^USE_TESTNET=1$' || fail "USE_TESTNET=1 missing"
echo "NOTE: this image sets $(echo "$ENVS" | grep '^PAPER_TRADING=') with USE_TESTNET=1 (read-only default; the orchestrator forces PAPER_TRADING=0 only inside the order-capable child it starts)"
HIST="$(docker history --no-trunc "$IMG")"
echo "$HIST" | grep -Eiq 'LIVE_TRADING_ACK|BYBIT_API_(KEY|SECRET)=|BYBIT_TESTNET_API_(KEY|SECRET)=' && fail "a credential name with a value, or a live switch, appears in the image history"
HOST="$(docker run --rm --network none -e PYTHONPATH=/app/tools -e BYBIT_VENUE=mainnet "$IMG" python3 -c 'import venues; print(venues.get("testnet").host)')"       # a hostile venue selection only: no arming variable is set here
[ "$HOST" = "api-testnet.bybit.com" ] || fail "the testnet host changed under a hostile environment: $HOST"
echo "ALL DRILL IMAGE CHECKS PASSED"
