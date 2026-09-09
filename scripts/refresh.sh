#!/usr/bin/env bash
# Pull current data, read the number, put the frozen corpus back.
#
# Four steps and one subtlety, run by hand three times already and the subtlety
# missed twice: `git checkout -- bot/data/` does NOT undo staged files, so the
# refreshed corpus rides onto the next branch and breaks the provenance tests.
# `git restore --staged --worktree` does.
#
# See bot/docs/human/CORPUS_POLICY.md for why the committed corpus stays frozen.
set -euo pipefail
trap 'echo "REFRESH FAILED at line $LINENO" >&2' ERR

root="$(git rev-parse --show-toplevel)"
cd "$root"
[ -d .venv ] && . .venv/bin/activate

dirty=$(git status --porcelain bot/data/ | wc -l)
if [ "$dirty" -ne 0 ]; then
  echo "bot/data/ is already modified. Restore it first:" >&2
  echo "  git restore --staged --worktree bot/data/" >&2
  exit 1
fi

cd bot
echo "==> append closed perp bars + funding prints"
python3 tools/append_closed_corpus.py --write
echo "==> append closed spot days"
python3 tools/append_spot_corpus.py --write
echo "==> corpus health"
python3 tools/corpus_health.py --repo . || true
echo "==> backtest on current data"
python3 tools/carry_backtest.py --repo . --matrix

cd "$root"
echo "==> restoring the frozen corpus"
git restore --staged --worktree bot/data/
remaining=$(git status --porcelain bot/data/ | wc -l)
[ "$remaining" -eq 0 ] || { echo "bot/data/ is STILL dirty — do not commit" >&2; exit 1; }
echo "frozen corpus restored. git status is clean."
