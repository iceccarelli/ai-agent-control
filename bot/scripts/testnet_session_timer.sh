#!/usr/bin/env bash
# testnet_session_timer.sh: RETIRED (Round 21). It no longer runs anything.
#
# It used to re-run bot/tools/testnet_session.py every N seconds with PAPER_TRADING=0
# in a loop, from whatever host held it: an order-capable timer outside the single-writer
# lease and outside any owner grant. That violates the current operating model (one
# selected Testnet executor, the shared Supabase lease, an owner capability and a bounded
# grant). The earlier script is in git history as history, not as guidance.
#
# What applies: the "Operating topology" block at the top of bot/RUNBOOKS.md.
set -euo pipefail
echo "REFUSED: bot/scripts/testnet_session_timer.sh is retired and runs nothing (see RUNBOOKS 'Operating topology')." >&2
exit 2
