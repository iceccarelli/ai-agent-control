#!/bin/sh
# The owner's control wrapper, run INSIDE the machine:  fly ssh console -a carry-autopilot -C "autopilot-ctl <command>"   (commands: status, drain, deploy-check, resume, clear-alert, grant, capital)
# OWNER-ONLY commands (grant, capital --ack-*/--declare-transfer/--reserve-fraction/--anchor-init/--rebaseline/--rebuild-view/--accept-older-lineage, clear-alert, resume of a STOP/PAUSED marker) need an INTERACTIVE terminal: open `fly ssh console -a carry-autopilot`
# WITHOUT -C and run them there; with -C (no terminal) they refuse and change nothing. status, drain, deploy-check and a deploy's own resume stay scriptable.
# Why it exists: a root ssh session would create marker/log files owned by root that the unprivileged worker could not append to, and it may not see the machine's secrets. This wrapper
# (1) copies the few variables the autopilot needs from PID 1's environment (where Fly puts the secrets), (2) drops to the `bot` user, (3) runs the autopilot CLI with HOME=/data.
set -eu
if [ -r /proc/1/environ ]; then
  eval "$(tr '\0' '\n' < /proc/1/environ | grep -E '^(BYBIT_TESTNET_API_KEY|BYBIT_TESTNET_API_SECRET|SUPABASE_URL|SUPABASE_SERVICE_ROLE_KEY|SUPABASE_OWNER_ID|CAPITAL_WITNESS|CAPITAL_WITNESS_REQUIRED|CAPITAL_ANCHOR_DIR|CAPITAL_ACCOUNT_IDENTITY_FALLBACK|OWNER_CASH_ALTERNATIVE_APR|OWNER_CASH_ALTERNATIVE_SOURCE|OWNER_CASH_ALTERNATIVE_STATED_UTC|EXECUTOR_MIN_FREE_BYTES|GIT_SHA|FLY_APP_NAME|FLY_MACHINE_ID|FLY_REGION)=' \
    | sed "s/'/'\\\\''/g; s/^\([A-Z_0-9]*\)=\(.*\)\$/export \1='\2'/")"
fi
export HOME=/data
cd /app
if [ "$(id -u)" = "0" ]; then
  exec setpriv --reuid=bot --regid=bot --init-groups python3 tools/ms_testnet_autopilot.py "$@"
fi
exec python3 tools/ms_testnet_autopilot.py "$@"
