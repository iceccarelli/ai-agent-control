"""Re-running the rewire script must not tick the control plane twice.

`scripts/REWIRE_MACHINE_B_EXECUTION.sh` installs the execution crons on the
trading host. Its first version guarded the install with

    crontab -l | grep -E 'daily_forward_refresh|control_plane_tick' || { ... }

which has two holes that only show up on the second run, on a real host, with
nobody watching:

1. The guard matches if EITHER job is present, so a host with the refresh cron
   but no tick cron was declared fine and never repaired.
2. The filter inside the install branch removed lines containing
   `control_plane_tick`, but `tools/control_plane_loop.sh` runs the same tick
   under a different name. A host wired with the loop wrapper got a second,
   independent ticker.

The cron block is now unconditional and idempotent. This test proves that by
running the real block out of the shipped file against a FAKE `crontab` on
PATH — the suite must never touch the operator's actual crontab, and a test
that mocks the logic instead of the file would not have caught either hole.
"""
from __future__ import annotations

import os
import re
import subprocess

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
BOT = os.path.dirname(HERE)
REPO = os.path.dirname(BOT)
SCRIPT = os.path.join(REPO, "scripts", "REWIRE_MACHINE_B_EXECUTION.sh")

JOBS = ("daily_forward_refresh", "control_plane_tick")

FAKE_CRONTAB = """#!/usr/bin/env bash
# Stands in for crontab(1). State lives in $FAKE_CRON_FILE, never in the user's.
#
# `crontab -` drains stdin to a temp file and only then replaces the spool.
# That detail is load-bearing: the idiom under test is
# `crontab -l | filter | crontab -`, and both ends run concurrently. A naive
# `cat > $FAKE_CRON_FILE` truncates the file while the `crontab -l` at the head
# of the same pipeline is still reading it, and every pre-existing line
# vanishes. Real crontab(1) does not do that, so a fake that does would fail
# the script for a fault the script does not have.
set -euo pipefail
case "${1:-}" in
  -l) cat "$FAKE_CRON_FILE" ;;
  -)  tmp="$(mktemp)"; cat > "$tmp"; mv "$tmp" "$FAKE_CRON_FILE" ;;
  -r) : > "$FAKE_CRON_FILE" ;;
  *)  echo "fake crontab: unsupported $*" >&2; exit 2 ;;
esac
"""


def _cron_block():
    """The shipped cron section, lifted verbatim between its own banners."""
    body = open(SCRIPT, encoding="utf-8").read()
    match = re.search(
        r'^echo "=== 2\) Crons.*?$(.*?)^echo "=== 3\)', body,
        re.S | re.M)
    assert match, "the cron section banners moved; this test must be re-aimed"
    block = match.group(1)
    assert "crontab -" in block
    return block


@pytest.fixture
def cron(tmp_path):
    """A PATH with a fake crontab in front of the real one."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = bindir / "crontab"
    fake.write_text(FAKE_CRONTAB)
    fake.chmod(0o755)
    state = tmp_path / "crontab.txt"
    state.write_text("")

    class Cron:
        path = str(state)

        def set(self, text):
            state.write_text(text if text.endswith("\n") or not text else text + "\n")

        def lines(self):
            return [ln for ln in state.read_text().splitlines() if ln.strip()]

        def count(self, needle):
            return sum(1 for ln in self.lines() if needle in ln)

        def run(self):
            env = dict(os.environ)
            env["PATH"] = f"{bindir}:{env['PATH']}"
            env["FAKE_CRON_FILE"] = str(state)
            script = (
                "set -euo pipefail\n"
                'BOT="/opt/aac/bot"\n'
                'PY="/opt/aac/.venv/bin/python"\n'
                'CRON_TAG="# aac-machine-b"\n'
                + _cron_block()
            )
            return subprocess.run(["bash", "-c", script], env=env,
                                  capture_output=True, text=True)

    return Cron()


class TestTheCronBlockIsIdempotent:
    def test_a_bare_host_gets_exactly_one_line_per_job(self, cron):
        done = cron.run()
        assert done.returncode == 0, done.stderr
        for job in JOBS:
            assert cron.count(job) == 1, f"{job}: {cron.lines()}"

    def test_running_it_five_times_still_leaves_one_line_per_job(self, cron):
        for _ in range(5):
            assert cron.run().returncode == 0
        for job in JOBS:
            assert cron.count(job) == 1, f"{job} duplicated: {cron.lines()}"

    def test_a_half_wired_host_is_repaired(self, cron):
        """The exact state the old `||` guard declared healthy and skipped."""
        cron.set("7 1 * * * cd /opt/aac/bot && python tools/daily_forward_refresh.py")
        assert cron.run().returncode == 0
        assert cron.count("daily_forward_refresh") == 1
        assert cron.count("control_plane_tick") == 1

    def test_a_host_already_running_the_loop_wrapper_does_not_tick_twice(self, cron):
        """control_plane_loop.sh calls the same tick under another name."""
        cron.set("0 * * * * /opt/aac/bot/tools/control_plane_loop.sh once")
        assert cron.run().returncode == 0
        assert cron.count("control_plane_loop.sh") == 0, (
            "the wrapper survived alongside the direct tick: two tickers")
        assert cron.count("control_plane_tick") == 1

    def test_it_removes_its_own_tagged_lines_before_writing(self, cron):
        cron.set("33 4 * * * /some/old/path/tools/control_plane_tick.py "
                 "# aac-machine-b")
        assert cron.run().returncode == 0
        assert cron.count("control_plane_tick") == 1
        assert cron.count("/some/old/path") == 0


class TestItDoesNotTouchLinesItDoesNotOwn:
    def test_an_unrelated_cron_line_survives(self, cron):
        cron.set("0 3 * * * /usr/local/bin/backup.sh\n"
                 "*/5 * * * * /Users/me/bin/notify.sh")
        assert cron.run().returncode == 0
        assert cron.count("backup.sh") == 1
        assert cron.count("notify.sh") == 1

    def test_the_operators_own_lines_keep_their_order_and_come_first(self, cron):
        cron.set("0 3 * * * /usr/local/bin/backup.sh\n"
                 "0 4 * * * /usr/local/bin/rotate.sh")
        assert cron.run().returncode == 0
        lines = cron.lines()
        assert lines[0].endswith("backup.sh")
        assert lines[1].endswith("rotate.sh")
        assert len(lines) == 4


class TestTheInstalledLinesAreRunnable:
    def test_each_line_has_five_schedule_fields_then_a_command(self, cron):
        assert cron.run().returncode == 0
        for line in cron.lines():
            fields = line.split(None, 5)
            assert len(fields) == 6, line
            for field in fields[:5]:
                assert re.fullmatch(r"[-0-9*/,]+", field), (field, line)

    def test_every_line_cds_to_bot_before_running_a_tool(self, cron):
        """Both tools resolve artifacts/ relative to cwd; cron's cwd is $HOME."""
        assert cron.run().returncode == 0
        for line in cron.lines():
            if any(job in line for job in JOBS):
                assert "cd /opt/aac/bot &&" in line, line

    def test_every_line_carries_the_ownership_tag(self, cron):
        assert cron.run().returncode == 0
        for line in cron.lines():
            if any(job in line for job in JOBS):
                assert line.rstrip().endswith("# aac-machine-b"), line

    def test_the_block_verifies_its_own_result(self):
        """A writer that does not re-read is a writer you have to trust."""
        block = _cron_block()
        assert "crontab -l" in block.split("| crontab -", 1)[1]
        assert "-eq 2" in block


class TestTheScriptItself:
    def test_it_is_executable_and_parses(self):
        assert os.access(SCRIPT, os.X_OK), SCRIPT
        done = subprocess.run(["bash", "-n", SCRIPT], capture_output=True,
                              text=True)
        assert done.returncode == 0, done.stderr

    def test_it_cds_to_bot_before_running_any_tool(self):
        """The old version ran a python heredoc at the operator's cwd first."""
        body = open(SCRIPT, encoding="utf-8").read()
        cd_at = body.index('cd "$BOT"')
        for needle in ("tools/reviewer_verdict.py", "tools/control_plane_tick.py"):
            first = body.index(needle)
            assert cd_at < first, (
                f"{needle} is invoked before cd \"$BOT\"; it would resolve "
                f"artifacts/ against whatever directory the operator was in")

    def test_it_places_no_orders_and_arms_nothing(self):
        body = open(SCRIPT, encoding="utf-8").read()
        for banned in ("--live", "LIVE_TRADING_ACK", "allows_live=true",
                       "set_allows_live", "place_order", "--write"):
            assert banned not in body, banned

    def test_it_commits_nothing_on_the_operators_behalf(self):
        """Staging is reversible. Committing and pushing from a wiring script
        is how an unreviewed change reaches the other machine."""
        # Line-anchored: the role doc this script writes mentions "git
        # push/pull" as prose, and banning the substring would ban the
        # documentation instead of the behaviour.
        commands = [ln.strip() for ln in open(SCRIPT, encoding="utf-8")]
        for line in commands:
            assert not line.startswith("git commit"), line
            assert not line.startswith("git push"), line

    def test_it_refuses_a_host_without_the_repo(self):
        body = open(SCRIPT, encoding="utf-8").read()
        assert 'test -d "$BOT" ||' in body
        assert "exit 1" in body
