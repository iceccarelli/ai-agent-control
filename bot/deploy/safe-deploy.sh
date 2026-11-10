#!/bin/sh
# OWNER-RUN safe deployment of the autopilot (run from bot/ on YOUR machine; needs flyctl logged in; nothing here is automatic).
# A Fly deploy kills the running process: Fly's kill timeout is best-effort and an unwind is NOT guaranteed to finish inside it. So: drain first (no new sessions; the active one finishes or
# unwinds), insist on a FRESH proof that the venue is flat and no session is active, and only then deploy. If any step refuses, nothing is deployed.
set -eu
APP="${1:-carry-autopilot}"
CTL="autopilot-ctl"
echo "== 1/3 drain: no new sessions; wait (up to 55 minutes) until the venue is proven flat"
fly ssh console -a "$APP" -C "$CTL drain --wait 3300" || { echo "NOT DRAINED: refusing to deploy over a possibly active session" >&2; exit 1; }
echo "== 2/3 deploy-check: a fresh DRAINED proof, no active session, no ALERT"
fly ssh console -a "$APP" -C "$CTL deploy-check" || { echo "NOT SAFE TO DEPLOY (see the message above)" >&2; exit 1; }
echo "== 3/3 deploy the committed config (never fly launch); the new process starts DRAINING-aware and must prove flat before any session"
fly deploy -c fly.autopilot.toml --ha=false --env GIT_SHA="$(git rev-parse HEAD)"
echo "== resume after the deploy: the loop is still in DRAIN until you clear it"
fly ssh console -a "$APP" -C "$CTL resume --reason 'deployed $(git rev-parse --short HEAD) after a verified drain'"
