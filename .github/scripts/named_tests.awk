# Print the `tests/test_*.py` files that the `python -m pytest ...` COMMANDS of a workflow name, one per line.
#
#     awk -f .github/scripts/named_tests.awk .github/workflows/carry-verify.yml | sort -u
#
# Only the pytest argument blocks count: the command line and its continuation lines, up to the first blank line,
# comment, YAML list item or YAML key. A test file merely MENTIONED in a comment, an `echo`, a `grep` or prose is NOT
# named, so it cannot be silently excluded from the `rest-of-suite` job. (Before this script that job grepped the
# whole workflow file, so one comment naming a test file removed it from CI entirely.)
!inb && /^[[:space:]]*(run:[[:space:]]*[>|]?[[:space:]]*)?python -m pytest([[:space:]]|$)/ { inb = 1 }
inb && (/^[[:space:]]*$/ || /^[[:space:]]*#/ || /^[[:space:]]*-[[:space:]]/ || (/^[[:space:]]*[A-Za-z_-]+:([[:space:]]|$)/ && !/^[[:space:]]*run:/)) { inb = 0 }
inb {
  s = $0
  while (match(s, /tests\/test_[A-Za-z0-9_]+\.py/)) {
    print substr(s, RSTART, RLENGTH)
    s = substr(s, RSTART + RLENGTH)
  }
}
