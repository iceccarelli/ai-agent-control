#!/usr/bin/env bash
# deploy.sh: RETIRED (Round 21). This script no longer deploys anything.
#
# It used to take a checkout to a running `carry-book` machine on Fly and printed
# instructions to arm it. That path is not part of the current mandate: this
# repository is paper and Bybit Testnet only, `carry-book` is stopped and stays
# stopped, and nothing in the repository arms Mainnet. The earlier script is in
# git history as history, not as guidance.
#
# It refuses before it touches flyctl, a network, a secret or an app, so running it
# by mistake is harmless. Where the live instructions are:
#   - what runs where, and what may place an order: the "Operating topology" block in
#     bot/RUNBOOKS.md (byte-identical in bot/README.md and docs/MONEY_MISSION.md);
#   - the owner's deploy checklist for the one selected Testnet executor:
#     bot/RUNBOOKS.md "Fly autopilot: owner procedure" (NOT deployed; not an approval);
#   - read-only checks: bot/RUNBOOKS.md sections 14, 16 and 18.
set -euo pipefail
cat >&2 <<'RETIRED'
REFUSED: scripts/deploy.sh is retired and deploys nothing.
         carry-book is stopped and stays stopped. Mainnet is out of scope.
         Read the "Operating topology" block at the top of bot/RUNBOOKS.md.
RETIRED
exit 2
