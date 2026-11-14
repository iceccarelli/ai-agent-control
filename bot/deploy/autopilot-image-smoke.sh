#!/bin/sh
# REAL container checks for the autopilot image (needs a Docker daemon; run from bot/):   sh deploy/autopilot-image-smoke.sh [image-tag]
# Offline: it never contacts Bybit or any network service and uses no credential. A failure of ANY check fails the script.
set -eu
IMG="${1:-carry-autopilot-smoke}"
VOL="$(mktemp -d)"
VOL2="$(mktemp -d)"
CID=""
fail() { echo "FAIL: $*" >&2; exit 1; }
cleanup() {
  [ -z "$CID" ] || docker rm -f "$CID" >/dev/null 2>&1 || true
  for v in "$VOL" "$VOL2"; do docker run --rm -v "$v":/data --entrypoint chown "$IMG" -R "$(id -u)":"$(id -g)" /data >/dev/null 2>&1 || true; done
  rm -rf "$VOL" "$VOL2" 2>/dev/null || true
}
trap cleanup EXIT

echo "== build (the commit is the ONLY build arg; no secrets). A build without it must REFUSE: an executor image that cannot say what it was built from does not exist"
if docker build -f Dockerfile.autopilot -t "$IMG-noprov" . >/dev/null 2>&1; then fail "the image built WITHOUT BUILD_COMMIT: provenance is not enforced at build"; fi
docker build -f Dockerfile.autopilot --build-arg BUILD_COMMIT="$(git rev-parse HEAD)" -t "$IMG" .

echo "== the image states what it is (runtime manifest), re-verifies it, is approved only as the executor, and the entrypoint refuses to start the autopilot on a tampered image"
docker run --rm --entrypoint python3 "$IMG" tools/runtime_provenance.py verify --root /app --expect TESTNET_EXECUTOR >/dev/null || fail "the image's own provenance does not verify"
docker run --rm --entrypoint python3 "$IMG" tools/runtime_provenance.py verify --root /app --expect READ_ONLY_RECORDER >/dev/null 2>&1 && fail "an executor image verified as a recorder image"
docker run --rm --entrypoint sh "$IMG" -c 'echo "# tampered" >> /app/tools/ms_edge_gate.py && ! python3 tools/runtime_provenance.py verify --root /app --expect TESTNET_EXECUTOR >/dev/null' || fail "a changed closure file was not refused"
docker run --rm --entrypoint sh "$IMG" -c 'echo "# tampered" >> /app/tools/ms_edge_gate.py && /usr/local/bin/autopilot-entrypoint true; test $? -eq 78' || fail "the entrypoint did not refuse (exit 78) on a tampered image"

echo "== the tools the entrypoint and the owner procedure need exist"
docker run --rm --entrypoint sh "$IMG" -c 'command -v setpriv && command -v su && command -v python3' >/dev/null || fail "setpriv / su / python3 missing from the image"

echo "== the real entrypoint: unprivileged user, volume owned by it, private home layout"
OUT="$(docker run --rm -v "$VOL":/data "$IMG" sh -c 'id -un; stat -c "%U %a" /data/.ai-agent-control /data/.ai-agent-control/ms_autopilot; echo "$HOME"')"
[ "$(echo "$OUT" | sed -n 1p)" = "bot" ] || fail "the worker does not run as bot: $OUT"
[ "$(echo "$OUT" | sed -n 2p)" = "bot 700" ] || fail "/data/.ai-agent-control is not bot:700: $OUT"
[ "$(echo "$OUT" | sed -n 3p)" = "bot 700" ] || fail "/data/.ai-agent-control/ms_autopilot is not bot:700: $OUT"
[ "$(echo "$OUT" | sed -n 4p)" = "/data" ] || fail "HOME is not /data: $OUT"

echo "== an owner file that arrives root-owned and world-readable is fixed by the entrypoint"
docker run --rm -v "$VOL":/data --entrypoint sh "$IMG" -c 'mkdir -p /data/.ai-agent-control && echo "{}" > /data/.ai-agent-control/ms_autopilot_grant.json && chown root:root /data/.ai-agent-control/ms_autopilot_grant.json && chmod 644 /data/.ai-agent-control/ms_autopilot_grant.json'
MODE="$(docker run --rm -v "$VOL":/data "$IMG" stat -c '%U %a' /data/.ai-agent-control/ms_autopilot_grant.json)"
[ "$MODE" = "bot 600" ] || fail "the grant file is not bot:600 after the entrypoint: $MODE"

echo "== the autopilot imports and reports (no credential, no capability: it must say so, not crash)"
ST="$(docker run --rm -v "$VOL":/data "$IMG" python3 tools/ms_testnet_autopilot.py status)"
echo "$ST" | grep -q '"dedicated_key_present": false' || fail "status does not report the missing credential: $ST"

echo "== the owner control wrapper runs as bot and reads state"
CTL="$(docker run --rm -v "$VOL":/data --entrypoint autopilot-ctl "$IMG" status)"
echo "$CTL" | grep -q '"dedicated_key_present": false' || fail "autopilot-ctl status did not run: $CTL"
CTLUSER="$(docker run --rm -v "$VOL":/data --entrypoint sh "$IMG" -c 'sed -n "s/.*exec setpriv --reuid=\([a-z]*\).*/\1/p" /usr/local/bin/autopilot-ctl | head -1')"
[ "$CTLUSER" = "bot" ] || fail "autopilot-ctl does not drop to bot"

echo "== no credential or live switch in the image; testnet only; mainnet cannot be selected by environment"
ENVS="$(docker run --rm --entrypoint env "$IMG")"
echo "$ENVS" | grep -Eq '^(BYBIT_API_KEY|BYBIT_API_SECRET|BYBIT_TESTNET_API_KEY|BYBIT_TESTNET_API_SECRET|LIVE_TRADING_ACK)=' && fail "a credential or live switch is in the image environment"
echo "$ENVS" | grep -q '^USE_TESTNET=1$' || fail "USE_TESTNET=1 missing"
HIST="$(docker history --no-trunc "$IMG")"
echo "$HIST" | grep -Eiq 'LIVE_TRADING_ACK|BYBIT_API_(KEY|SECRET)|BYBIT_TESTNET_API_(KEY|SECRET)=' && fail "a credential name with a value, or a live switch, appears in the image history"
HOST="$(docker run --rm -e PYTHONPATH=/app/tools -e BYBIT_VENUE=mainnet -e USE_TESTNET=0 -e PAPER_TRADING=0 -e LIVE_TRADING_ACK=1 "$IMG" python3 -c 'import venues; print(venues.get("testnet").host)')"
[ "$HOST" = "api-testnet.bybit.com" ] || fail "the testnet host changed under a hostile environment: $HOST"
docker run --rm -e PYTHONPATH=/app/tools -e BYBIT_VENUE=mainnet -e USE_TESTNET=0 -e PAPER_TRADING=0 -e LIVE_TRADING_ACK=1 "$IMG" python3 -c '
import ms_testnet_transport as TT
try:
    TT.build_client(TT.TestnetCredentials.from_mapping({TT.KEY_ENV: "k" * 16, TT.SECRET_ENV: "s" * 16}), object())
except Exception:
    pass
import ms_testnet_adapter as AD, venues
assert venues.get("testnet").host == "api-testnet.bybit.com" and not venues.is_sandbox("mainnet")
print("host-pinned")' | grep -q host-pinned || fail "the testnet host pin did not hold under a hostile environment"

echo "== SIGTERM reaches the Python process, the engine stops cleanly and quickly (not SIGKILLed after the timeout)"
CID="$(docker run -d -v "$VOL2":/data "$IMG")"
sleep 10
docker exec "$CID" sh -c 'test "$(awk "/^Uid:/ {print \$2}" /proc/1/status)" = "$(id -u bot)"' || fail "PID 1 is not running as bot"
docker exec "$CID" sh -c 'tr "\0" " " < /proc/1/cmdline' | grep -q 'ms_testnet_autopilot.py loop' || fail "PID 1 is not the autopilot loop"
vol2_sh() { docker run --rm -v "$VOL2":/data --entrypoint sh "$IMG" -c "$1"; }       # the volume belongs to bot: inspect it from inside a container, never from the host user
vol2_sh 'test -f /data/.ai-agent-control/ms_autopilot/engine_state.json' || fail "the engine wrote no state to the volume"
START="$(date +%s)"
docker stop -t 30 "$CID" >/dev/null
ELAPSED=$(( $(date +%s) - START ))
CODE="$(docker inspect -f '{{.State.ExitCode}}' "$CID")"
[ "$CODE" = "0" ] || fail "the engine did not exit cleanly on SIGTERM (exit code $CODE; 137 means it was killed)"
[ "$ELAPSED" -lt 20 ] || fail "the engine took ${ELAPSED}s to stop on SIGTERM"
docker logs "$CID" 2>&1 | grep -q 'stop signal: stopped' || fail "the loop did not report the stop signal"
vol2_sh 'grep -q "\"state\": \"STOPPED\"" /data/.ai-agent-control/ms_autopilot/engine_state.json && test "$(stat -c %a /data/.ai-agent-control/ms_autopilot/engine_state.json)" = 600' || fail "the final persisted state is not STOPPED (or the state file is not mode 600)"
echo "ALL AUTOPILOT IMAGE CHECKS PASSED"
