#!/usr/bin/env bash
# The only sanctioned way to produce a test count.
#
# Runs the suite and writes a RECEIPT binding the number to the exact tree that
# produced it. The commit-msg hook checks that receipt. A number you remembered,
# copied from chat, or read off a run against different code cannot pass.
set -euo pipefail
root="$(git rev-parse --show-toplevel)"
cd "$root"
[ -d .venv ] || { echo "no .venv — python3 -m venv .venv first"; exit 1; }
. .venv/bin/activate
python3 -c "import pytest, numpy, scipy, sklearn" 2>/dev/null || \
  pip install -q "requests<3" "numpy<3" "pytest>=8" "scipy<2" "scikit-learn<2"

before=$(md5sum bot/state/trading_state.db | cut -d' ' -f1)
out=$(cd bot && python3 -m pytest tests/ -q 2>&1 | tail -3)
echo "$out"
after=$(md5sum bot/state/trading_state.db | cut -d' ' -f1)
[ "$before" = "$after" ] || { echo "FIXTURE DB MUTATED — do not commit"; exit 1; }

count=$(echo "$out" | grep -oE '[0-9]+ passed' | head -1 | cut -d' ' -f1)
[ -n "$count" ] || { echo "no pass count in output — refusing to write a receipt"; exit 1; }

# The tree hash of what is STAGED+tracked right now. If a single byte changes
# after this, the hook's comparison fails and the receipt is void. That is the
# whole mechanism: the receipt is not a note, it is a fingerprint.
tree=$(git write-tree)
printf '%s %s\n' "$count" "$tree" > .verify-receipt
echo "receipt: $count passed @ tree $tree"
echo "quote '$count passed' in your commit message. Any other number is refused."
