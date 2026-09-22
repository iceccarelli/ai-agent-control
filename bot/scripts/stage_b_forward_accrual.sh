#!/usr/bin/env bash
# Daily Stage B forward-accrual path (real venue egress required).
#
# This is the TRACKED replacement for a factory operator's untracked cron
# script of a similar name. That untracked copy auto-restamps
# artifacts/forward_shadow_current.json — this script does NOT and never
# will: promotion of the shadow file stays human-only, via
# tools/promote_forward_shadow.py --i-am-human --write, run by hand. See
# docs/human/NO_GLUE_OPS.md items 5-6 and tools/control_plane_tick.py's
# "WHAT IT MUST NEVER DO" header. For the copy-paste cutover (crontab lines,
# removing the old entry, one-shot run, promote command), see the "Factory
# cutover checklist" in docs/human/NO_GLUE_OPS.md, items 7-12.
#
# WHAT THIS SCRIPT DOES
#   1. append_closed_corpus.py --write   (linear + funding, both trees —
#      dual-tree sync is the tool's own default, no second hand-call needed)
#   2. append_spot_corpus.py --write     (spot, both trees, same pattern)
#   3. daily_forward_refresh.py --scratch state/forward_shadow_scratch
#      (READ-ONLY scratch score; never writes to artifacts/). The scratch
#      dir is a fixed, persistent path (not a tempfile.TemporaryDirectory
#      that vanishes when the run ends) so the HUMAN_PROMOTE_HINT below
#      always names a --from file that is still there when a human reads it.
#   4. Compares the scratch score's forward counters against the current
#      artifacts/forward_shadow_current.json, READ-ONLY, and prints a
#      HUMAN_PROMOTE_HINT line if they differ. It never writes that file.
#
# WHAT THIS SCRIPT MUST NEVER DO
#   - open artifacts/forward_shadow_current.json for writing
#   - call any promote tool with write intent
#   - place an order
#   - set allows_live
#   - git commit / git push
#
# Settlement (data/real_settlement_8h) and borrow (data/real_borrow) corpora
# are NOT part of the Stage B daily path (see NO_GLUE_OPS.md #7) and are left
# commented out below, on purpose. If ever wired in, they must not be allowed
# to abort the money path above them (wrap in `|| true` / their own error
# handling) — until then, run them by hand.
#
# Cron line (matches daily_forward_refresh.py's own documented convention):
#   7 1 * * *  /path/to/repo/bot/scripts/stage_b_forward_accrual.sh \
#              >> /path/to/repo/bot/state/forward_refresh.log 2>&1

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BOT="$(cd "$HERE/.." && pwd)"
REPO="$(cd "$BOT/.." && pwd)"
PY="$REPO/.venv/bin/python"

cd "$BOT"

# 1. Linear + funding corpora, both trees (primary + _full), one atomic call.
"$PY" tools/append_closed_corpus.py --write

# 2. Spot corpus, both trees, same dual-tree pattern.
"$PY" tools/append_spot_corpus.py --write

# --- Settlement / borrow corpora: NOT part of the Stage B daily path. -------
# Left commented out on purpose (NO_GLUE_OPS.md #7). A human runs these by
# hand after a funding catch-up moves the tip past their own last window.
# If ever uncommented, wrap in `|| true` (or their own error handling) so a
# settlement/borrow fetch failure can never abort the money path above it.
# "$PY" tools/fetch_settlement_klines.py --write
# "$PY" tools/fetch_borrow_rates.py --write
# -----------------------------------------------------------------------------

# Fixed, persistent scratch dir (never artifacts/, never a tempdir that
# disappears when this script exits) — the single canonical scratch path
# every run writes to, so HUMAN_PROMOTE_HINT's --from below is always usable.
SCRATCH_DIR="state/forward_shadow_scratch"
SCRATCH_FILE="$SCRATCH_DIR/forward.json"
mkdir -p "$SCRATCH_DIR"

# 3. Score Stage B to scratch. READ-ONLY: never writes to artifacts/.
REPORT=$("$PY" tools/daily_forward_refresh.py --scratch "$SCRATCH_DIR")
echo "$REPORT"

# Append the same report to the log path daily_forward_refresh.py's own
# docstring documents (state/forward_refresh.log, relative to bot/).
{
    printf '\n--- %s ---\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    echo "$REPORT"
} >> state/forward_refresh.log

# 4. Compare the scratch score against the CURRENT shadow file, read-only.
#    Never opens forward_shadow_current.json for writing.
"$PY" - "$REPORT" "$SCRATCH_FILE" <<'PY'
import json
import os
import sys

sys.path.insert(0, "tools")
from stage_b_bars import resolve_closed_forward_bars  # noqa: E402

report = json.loads(sys.argv[1])
scratch_file = sys.argv[2]
shadow_path = os.path.join("artifacts", "forward_shadow_current.json")

old_trades = old_bars = None
if os.path.exists(shadow_path):
    with open(shadow_path, "r", encoding="utf-8") as fh:  # read-only, on purpose
        current = json.load(fh)
    old_trades = current.get("forward_n_trades")
    old_bars = resolve_closed_forward_bars(current)

new_trades = report.get("forward_n_trades")
# report["forward_bars"] is daily_forward_refresh.py's own resolved reading
# of the scratch scorer's ceiling.closed_forward_bars (see its run()) — not
# a raw top-level field, so no resolver needed on this side of the compare.
new_bars = report.get("forward_bars")

if (new_trades, new_bars) != (old_trades, old_bars):
    print(
        "HUMAN_PROMOTE_HINT: forward_n_trades "
        f"{old_trades!r} -> {new_trades!r}, closed_forward_bars "
        f"{old_bars!r} -> {new_bars!r}. Promote by hand with "
        f"{sys.executable} tools/promote_forward_shadow.py "
        f"--from {scratch_file} --i-am-human --write "
        "(never automated)."
    )
PY
