"""The rewire script retires the dead control-plane crons and installs none.

`scripts/REWIRE_MACHINE_B_EXECUTION.sh` used to install `daily_forward_refresh`
and `control_plane_tick` crons and smoke-run `reviewer_verdict`, each followed
by `|| true`. Those tools were deleted deliberately (commit 8d412c6) so a host
rewired by the old script ran a missing file every hour and the `|| true` made
it look healthy. The script now strips every line it owns or that names a
retired job, installs nothing, and exits non-zero if a retired job is still
scheduled.

The real cron block is run out of the shipped file against a FAKE `crontab` on
PATH: the suite must never touch the operator's actual crontab.
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

RETIRED = ("daily_forward_refresh", "control_plane_tick",
           "control_plane_loop.sh", "reviewer_verdict")

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


class TestRetiringTheLegacyCrons:
    @pytest.mark.parametrize("job", RETIRED)
    def test_a_retired_job_is_removed(self, cron, job):
        cron.set(f"7 1 * * * cd /opt/aac/bot && python tools/{job}")
        done = cron.run()
        assert done.returncode == 0, done.stderr
        assert cron.count(job) == 0

    def test_it_installs_nothing_on_a_bare_host(self, cron):
        assert cron.run().returncode == 0
        assert cron.lines() == []

    def test_running_it_repeatedly_is_stable(self, cron):
        cron.set("7 1 * * * x/daily_forward_refresh.py # aac-machine-b\n"
                 "0 3 * * * /usr/local/bin/backup.sh")
        for _ in range(3):
            assert cron.run().returncode == 0
        assert cron.lines() == ["0 3 * * * /usr/local/bin/backup.sh"]

    def test_the_loop_wrapper_is_removed_too(self, cron):
        cron.set("0 * * * * /opt/aac/bot/tools/control_plane_loop.sh once")
        assert cron.run().returncode == 0
        assert cron.count("control_plane_loop.sh") == 0

    def test_its_own_tagged_lines_go_even_if_they_name_no_retired_job(self, cron):
        cron.set("33 4 * * * /some/old/path/other.py # aac-machine-b")
        assert cron.run().returncode == 0
        assert cron.count("/some/old/path") == 0

    def test_unrelated_lines_survive_in_order(self, cron):
        cron.set("0 3 * * * /usr/local/bin/backup.sh\n"
                 "0 4 * * * /usr/local/bin/rotate.sh")
        assert cron.run().returncode == 0
        lines = cron.lines()
        assert lines[0].endswith("backup.sh") and lines[1].endswith("rotate.sh")
        assert len(lines) == 2

    def test_it_fails_if_a_retired_job_cannot_be_removed(self, cron):
        """A crontab that refuses the rewrite must not be reported as clean."""
        cron.set("7 1 * * * x/daily_forward_refresh.py")
        bindir = os.path.join(os.path.dirname(cron.path), "bin")
        stuck = os.path.join(bindir, "crontab")
        with open(stuck, "w") as fh:
            fh.write('#!/usr/bin/env bash\ncase "${1:-}" in -l) cat "$FAKE_CRON_FILE";;'
                     ' -) cat >/dev/null;; esac\n')
        done = cron.run()
        assert done.returncode != 0
        assert "FATAL" in done.stdout


class TestTheScriptItself:
    def test_it_is_executable_and_parses(self):
        assert os.access(SCRIPT, os.X_OK), SCRIPT
        done = subprocess.run(["bash", "-n", SCRIPT], capture_output=True,
                              text=True)
        assert done.returncode == 0, done.stderr

    def test_it_cds_to_bot_before_importing_the_stack(self):
        body = open(SCRIPT, encoding="utf-8").read()
        assert body.index('cd "$BOT"') < body.index('import main, funding_reconcile')

    def test_no_failure_is_swallowed_on_the_execution_check(self):
        body = open(SCRIPT, encoding="utf-8").read()
        line = [ln for ln in body.splitlines() if "import main" in ln][0]
        assert "|| true" not in line

    def test_it_no_longer_invokes_any_deleted_tool(self):
        body = open(SCRIPT, encoding="utf-8").read()
        for gone in ("tools/reviewer_verdict.py", "tools/control_plane_tick.py",
                     "tools/daily_forward_refresh.py"):
            assert f"$PY {gone}" not in body and f"python {gone}" not in body
            assert not os.path.exists(os.path.join(BOT, gone)), gone
        assert not os.path.exists(os.path.join(BOT, "tools", "control_plane_loop.sh"))

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
