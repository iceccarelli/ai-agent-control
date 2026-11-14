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
# Round 22: the image is built FROM a commit and says which (RUNTIME_MANIFEST.json, verified at start; the entrypoint refuses an image whose provenance does not verify). So the commit must be exactly
# what you mean to run: a clean tree (a dirty or untracked file would be baked in under a clean sha) that is contained in origin/main (an unreviewed local commit is not an approved executor).
if [ -n "$(git status --porcelain --untracked-files=all)" ]; then echo "REFUSING: the working tree is not clean; the image would not match the commit it is labelled with" >&2; exit 1; fi
git fetch --quiet origin main || { echo "REFUSING: cannot fetch origin/main to prove this commit is merged" >&2; exit 1; }
git merge-base --is-ancestor HEAD origin/main || { echo "REFUSING: HEAD is not contained in origin/main (an unmerged commit is not an approved executor)" >&2; exit 1; }
fly deploy -c fly.autopilot.toml --ha=false --env GIT_SHA="$(git rev-parse HEAD)" --build-arg BUILD_COMMIT="$(git rev-parse HEAD)"
echo "== resume after the deploy: the loop is still in DRAIN until you clear it"
fly ssh console -a "$APP" -C "$CTL resume --reason 'deployed $(git rev-parse --short HEAD) after a verified drain'"
