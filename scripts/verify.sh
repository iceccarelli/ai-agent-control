#!/usr/bin/env bash
# The only sanctioned way to produce a test count.
#
# Runs the suite and writes a RECEIPT binding the number to the exact tree that
# produced it. The commit-msg hook checks that receipt. A number you remembered,
# copied from chat, or read off a run against different code cannot pass.
set -euo pipefail
# A verification tool that can fail QUIETLY is worse than no tool. set -e killed
# this script mid-run when the suite failed and no pass count could be grepped,
# and it printed nothing at all — the one job it has.
trap 'echo "VERIFY FAILED at line $LINENO — the suite did not pass" >&2' ERR
root="$(git rev-parse --show-toplevel)"
cd "$root"
[ -d .venv ] || { echo "no .venv — python3 -m venv .venv first"; exit 1; }
. .venv/bin/activate
python3 -c "import pytest, numpy, scipy, sklearn" 2>/dev/null || \
  pip install -q "requests<3" "numpy<3" "pytest>=8" "scipy<2" "scikit-learn<2"

before=$(md5sum bot/state/trading_state.db | cut -d' ' -f1)

# The EXIT CODE is the verdict. The summary line is only where the number lives.
#
# What was here before refused a red suite too, but by accident of plumbing:
# `out=$(... | tail -3)` inherits the pipeline status only because `pipefail` is
# set on line 7, and `set -e` then killed the script through the ERR trap. So
# the stated gate — `grep -q "failed"` — was never the thing doing the work,
# and it could not have been: pytest does not spell every bad outcome "failed"
# (a fixture that raises in teardown prints `2 passed, 1 error`).
#
# Two problems with leaning on that. It printed "VERIFY FAILED at line N" and
# NOTHING of the pytest output, which is the exact complaint the trap above was
# added to fix — you got told the suite failed and not one word about how. And
# deleting `pipefail`, or wrapping this line in anything, silently converts a
# correct refusal into a written receipt.
#
# So: run, capture rc, test rc, and print what pytest actually said.
set +e
full=$(cd bot && python3 -m pytest tests/ -q 2>&1)
rc=$?
set -e
out=$(printf '%s\n' "$full" | tail -3)
echo "$out"

after=$(md5sum bot/state/trading_state.db | cut -d' ' -f1)
[ "$before" = "$after" ] || { echo "FIXTURE DB MUTATED — do not commit"; exit 1; }

if [ "$rc" -ne 0 ]; then
  echo "SUITE DID NOT PASS (pytest rc=$rc) — no receipt written:" >&2
  printf '%s\n' "$full" | tail -30 >&2
  exit 1
fi
count=$(printf '%s\n' "$out" | grep -oE '[0-9]+ passed' | head -1 | cut -d' ' -f1)
[ -n "$count" ] || { echo "no pass count in output — refusing to write a receipt"; exit 1; }

# The tree hash of what is STAGED+tracked right now. If a single byte changes
# after this, the hook's comparison fails and the receipt is void. That is the
# whole mechanism: the receipt is not a note, it is a fingerprint.
tree=$(git write-tree)
printf '%s %s\n' "$count" "$tree" > .verify-receipt
echo "receipt: $count passed @ tree $tree"
echo "quote '$count passed' in your commit message. Any other number is refused."
