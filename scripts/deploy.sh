#!/usr/bin/env bash
# deploy.sh — from a clean checkout to a book running on Fly, in one command.
#
# Idempotent and step-wise: every step checks whether it already happened and
# skips it, so re-running after a failure costs nothing and never duplicates an
# app, a volume or an IP. It REFUSES rather than guessing at every point where
# guessing could point the book at the wrong exchange.
#
# It does not arm anything. What it leaves you with is a machine that can reach
# the venue, with a book that refuses every order because PAPER_TRADING=1.
# Arming is two deliberate commands, printed at the end.
#
#   ./scripts/deploy.sh                 # testnet (default)
#   BYBIT_VENUE=demo ./scripts/deploy.sh
set -euo pipefail
trap 'echo; echo "STOPPED at line $LINENO. Nothing was left half-done that a re-run will not fix." >&2' ERR

VENUE="${BYBIT_VENUE:-testnet}"
APP="${FLY_APP:-carry-book}"
REGION="${FLY_REGION:-fra}"
VOLUME="${FLY_VOLUME:-carry_state}"

case "$VENUE" in
  testnet|demo) ;;
  mainnet)
    echo "REFUSED: this script does not deploy to mainnet." >&2
    echo "         Mainnet needs a signed risk memo and a recorded drill" >&2
    echo "         first, and then it is a deliberate act, not a script." >&2
    exit 1 ;;
  *) echo "REFUSED: BYBIT_VENUE=$VENUE is not testnet or demo." >&2; exit 1 ;;
esac

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root/bot"
say() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }

say "1/7  flyctl"
# The installer lays down `flyctl`. A `fly` alias is NOT guaranteed — checking
# only for it reinstalled flyctl on a machine that already had it and then
# called a command that was not there. Resolve ONE name, once, and use it.
export PATH="${FLYCTL_INSTALL:-$HOME/.fly}/bin:$PATH"
if command -v flyctl >/dev/null 2>&1; then FLY=flyctl
elif command -v fly >/dev/null 2>&1; then FLY=fly
else
  echo "installing flyctl..."
  curl -L https://fly.io/install.sh | sh
  export FLYCTL_INSTALL="${FLYCTL_INSTALL:-$HOME/.fly}"
  export PATH="$FLYCTL_INSTALL/bin:$PATH"
  command -v flyctl >/dev/null 2>&1 && FLY=flyctl || FLY=fly
fi
echo "using: $(command -v "$FLY")"
"$FLY" version
"$FLY" auth whoami >/dev/null 2>&1 || {
  echo; echo "Not signed in. Run: $FLY auth login"; exit 1; }

say "2/7  the config, checked before anything is created"
python3 tools/fly_stack.py --check

say "3/7  the app"
if fly status --app "$APP" >/dev/null 2>&1; then
  echo "app $APP already exists"
else
  "$FLY" launch --no-deploy --name "$APP" --region "$REGION" --copy-config --yes
fi

say "4/7  the volume (the ledger lives here; losing it means a human looks)"
if fly volumes list --app "$APP" 2>/dev/null | grep -q "$VOLUME"; then
  echo "volume $VOLUME already exists"
else
  "$FLY" volumes create "$VOLUME" --app "$APP" --region "$REGION" --size 1 --yes
fi

say "5/7  the outbound IP — this is the address you pin the API key to"
if fly ips list --app "$APP" 2>/dev/null | grep -qi egress; then
  echo "an egress IP is already allocated"
else
  "$FLY" ips allocate-egress --app "$APP" --region "$REGION" --yes || \
    echo "could not allocate an egress IP; pin the key after 'fly ips list'"
fi
"$FLY" ips list --app "$APP" || true

say "6/7  secrets"
if fly secrets list --app "$APP" 2>/dev/null | grep -q BYBIT_API_KEY; then
  echo "BYBIT_API_KEY is already set"
else
  echo "Set them now, from the key you pinned to the IPv4 above:"
  echo "    fly secrets set --app $APP BYBIT_API_KEY=... BYBIT_API_SECRET=..."
  echo
  read -r -p "press enter once they are set (or ctrl-c to stop) "
fi
"$FLY" secrets set --app "$APP" BYBIT_VENUE="$VENUE" --stage >/dev/null
echo "BYBIT_VENUE staged as $VENUE"

say "7/7  deploy — immediate, because two machines is two hedges"
"$FLY" deploy --app "$APP" --strategy immediate

cat <<NEXT

==============================================================================
  The book is running and it will not send an order: PAPER_TRADING=1 and the
  promotion gate is unsigned. Nothing below is armed.

  see it                flyctl logs --app $APP
  the four invariants   flyctl ssh console --app $APP -C "python3 tools/session_tail.py"
  the preflight drill   flyctl ssh console --app $APP -C "python3 tools/drill.py"
NEXT

if [ "$VENUE" = "demo" ]; then cat <<NEXT
  fund the wallet       flyctl ssh console --app $APP -C "python3 tools/fund_demo.py --btc 1 --usdt 10000"
                        (demo funds itself by API; testnet needs the web faucet)
NEXT
fi

cat <<NEXT

  ARMING, when the preflight is clean and the wallet has BTC in it. Two
  deliberate commands, in this order, and neither is mainnet:

      "$FLY" secrets set --app $APP PAPER_TRADING=0
      "$FLY" ssh console --app $APP -C "python3 tools/drill.py --arm --out artifacts/drill.json"

  The transcript that writes is the Phase D evidence.
==============================================================================
NEXT
