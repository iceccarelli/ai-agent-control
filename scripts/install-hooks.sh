#!/usr/bin/env bash
set -euo pipefail
root="$(git rev-parse --show-toplevel)"
cat > "$root/.git/hooks/commit-msg" <<'HOOK'
#!/usr/bin/env bash
# Refuse a commit whose message claims a test count that no run produced FOR
# THIS TREE.
#
# The first version of this hook checked whether .venv had pytest installed.
# That is true whether or not you activated it, so it never fired: a commit
# saying "fake 9999 passed" sailed through. It tested the wrong thing.
#
# This one compares against a receipt written by scripts/verify.sh containing
# the count AND the git tree hash it was produced from. Change one byte and the
# tree hash moves and the receipt is void.
root="$(git rev-parse --show-toplevel)"
claim=$(grep -oE '[0-9]{3,} passed' "$1" | head -1 | cut -d' ' -f1)
[ -n "$claim" ] || exit 0

receipt="$root/.verify-receipt"
[ -f "$receipt" ] || {
  echo "REFUSED: the message claims '$claim passed' and there is no receipt."
  echo "         run ./scripts/verify.sh"
  exit 1; }

read -r rcount rtree < "$receipt"
tree=$(git write-tree)

[ "$claim" = "$rcount" ] || {
  echo "REFUSED: message claims '$claim passed', the last run produced '$rcount'."
  exit 1; }
[ "$tree" = "$rtree" ] || {
  echo "REFUSED: the tree changed since the run that produced '$rcount passed'."
  echo "         receipt tree $rtree"
  echo "         staged  tree $tree"
  echo "         re-run ./scripts/verify.sh"
  exit 1; }
HOOK
chmod +x "$root/.git/hooks/commit-msg"
grep -qxF '.verify-receipt' "$root/.gitignore" 2>/dev/null || echo '.verify-receipt' >> "$root/.gitignore"
echo "commit-msg hook installed (receipt-based)"
