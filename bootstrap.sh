#!/usr/bin/env bash
# bootstrap.sh — put the trading tree under real version control.
#
# Turns a repo that holds ZIP BLOBS into a repo that holds SOURCE, with one
# git commit per fix. After this you can `git log`, `git diff`, `git revert`
# and `git bisect` the thing, which you cannot do with a zip.
#
# Safe to re-run: it refuses rather than clobbering. Stops at the first failure.

set -euo pipefail

TREE="bot"                      # tracked source lives here
PATCHDIR="patches"
BOTZIP="tradingbot_slice76 (2) (1).zip"
PATCHZIP="files (3).zip"
BRANCH="stage0-runnable"

say() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
die() { printf '\n\033[31mSTOP: %s\033[0m\n' "$*" >&2; exit 1; }

[ -d .git ] || die "run this from the repo root (/workspaces/ai-agent-control)"

say "0/8  sync with GitHub"
git pull --ff-only
[ -f "$BOTZIP" ]   || die "missing '$BOTZIP' — did the pull succeed?"
[ -f "$PATCHZIP" ] || die "missing '$PATCHZIP' — did the pull succeed?"

say "1/8  toolchain"
command -v unzip >/dev/null || die "unzip not found: sudo apt-get install -y unzip"
python3 -m venv --help >/dev/null 2>&1 || die "python3-venv missing: sudo apt-get install -y python3-venv"

say "2/8  python venv + dependencies"
# Codespaces images are PEP 668 externally-managed; system pip refuses to install.
# scipy and scikit-learn are marked OPTIONAL in requirements.txt. They are not:
# without them the suite throws 41 failures and 101 errors.
[ -d .venv ] || python3 -m venv .venv
# shellcheck disable=SC1091
. .venv/bin/activate
pip install -q --upgrade pip
pip install -q "requests>=2.31,<3" "numpy>=1.24,<3" "pytest>=8" \
               "scipy>=1.11,<2" "scikit-learn>=1.3,<2"
python3 -c "import numpy,requests,scipy,sklearn,pytest" || die "dependency import failed"
echo "    python: $(command -v python3)"

say "3/8  unpack the patch series"
[ -d "$PATCHDIR" ] && die "$PATCHDIR/ exists already — 'rm -rf $PATCHDIR' first"
mkdir -p "$PATCHDIR"
unzip -qo "$PATCHZIP" -d "$PATCHDIR"
n=$(ls -1 "$PATCHDIR"/0*.patch 2>/dev/null | wc -l)
[ "$n" -eq 10 ] || die "expected 10 patches in $PATCHDIR, found $n"
echo "    $n patches"

say "4/8  unpack the tree as TRACKED SOURCE (not a blob)"
[ -e "$TREE" ] && die "$TREE/ exists already — 'rm -rf $TREE' first"
mkdir -p "$TREE"
unzip -q "$BOTZIP" -d "$TREE"
echo "    $(find "$TREE" -type f | wc -l) files"

# NOTE ON state/trading_state.db
# It ships with the kill switch ENGAGED ('certification test') plus 84 rows of
# a previous run's decisions, and .gitignore lists state/ but the zip was built
# outside git so it came along anyway. Do NOT delete it here: patch 0005 adds
# tests/test_operator_tools.py, which asserts the operator REFUSES to start
# against exactly this shipped state. Removing it early turns a passing safety
# test into a failure. Delete it deliberately, once, before your first real
# run — the last section prints the command.

say "5/8  ignore rules"
for line in ".venv/" "__pycache__/" "*.pyc" ".pytest_cache/" "$TREE/state/*.db" "$TREE/logs/"; do
  grep -qxF "$line" .gitignore 2>/dev/null || echo "$line" >> .gitignore
done

say "6/8  BASELINE test run (expect 4953 passed, 2 skipped)"
( cd "$TREE" && python3 -m pytest tests/ -q > /tmp/baseline.txt 2>&1 ) || true
tail -3 /tmp/baseline.txt
grep -q "^4953 passed" /tmp/baseline.txt \
  || die "baseline is not 4953 passed — do not patch a tree you cannot reproduce"

say "7/8  commit the baseline, then one commit per patch"
git checkout -b "$BRANCH" 2>/dev/null || git checkout "$BRANCH"
git add -A "$TREE" "$PATCHDIR" .gitignore
git -c user.email="${GIT_AUTHOR_EMAIL:-bot@local}" \
    -c user.name="${GIT_AUTHOR_NAME:-bootstrap}" \
    commit -qm "baseline: slice76 tree unpacked as source (4953 passed, 2 skipped)"
echo "    baseline committed"

for p in "$PATCHDIR"/0*.patch; do
  name=$(basename "$p" .patch)
  short=$(echo "$name" | cut -c1-4)
  printf '    %-6s ' "$short"
  # `patch -p1` and not `git apply`: inside a git repo, git apply resolves
  # paths from the REPO ROOT, not the current directory, so running it from
  # bot/ silently applies nothing while --check still reports success.
  ( cd "$TREE" && patch -p1 --dry-run -f -s < "../$p" ) \
    || die "$name does not apply — nothing was written"
  ( cd "$TREE" && patch -p1 -f -s < "../$p" )
  subject=$(grep -m1 '^Subject:' "$p" | sed 's/^Subject: *//; s/^\[PATCH[^]]*\] *//')
  git add -A "$TREE"
  git -c user.email="${GIT_AUTHOR_EMAIL:-bot@local}" \
      -c user.name="${GIT_AUTHOR_NAME:-bootstrap}" \
      commit -qm "$short: ${subject:-$name}"
  echo "applied + committed"
done

say "8/8  VERIFY (expect 5039 passed, 2 skipped)"
( cd "$TREE" && python3 -m pytest tests/ -q > /tmp/verify.txt 2>&1 ) || true
tail -3 /tmp/verify.txt
grep -q "^5039 passed" /tmp/verify.txt \
  || die "final run is not 5039 passed — inspect /tmp/verify.txt"

cat <<EOF

==========================================================================
 Done. The tree is source, on branch '$BRANCH', with one commit per fix.

   git log --oneline
   git show 0004            # read any single fix
   git revert <sha>         # back one out cleanly

 Before the first RUN (not needed for tests) clear the shipped state:
   rm -f $TREE/state/trading_state.db

 Next:
   . .venv/bin/activate && cd $TREE
   python3 tools/print_project_status.py
   python3 tools/operator_paper_daily.py --repo .
   python3 tools/connector_check.py
   python3 tools/deflated_sharpe.py --demo
   python3 tools/corpus_unit_audit.py --repo .

 Then push:
   git push -u origin $BRANCH
==========================================================================
EOF
