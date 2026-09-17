"""`scripts/verify.sh` is the only thing standing between a commit message and
a made-up test count. So the question worth asking is not "does it work when
the suite is green" — it is "what does it do when the suite is NOT green, in
each of the several ways pytest has of not being green".

Seven of those ways are enumerated below. Before this file existed, the script
refused all seven — but not for the reason it claimed. Its stated gate was

    if echo "$out" | grep -q "failed"

and pytest does not spell every bad outcome "failed"; a fixture raising in
teardown prints `2 passed, 1 error`. What actually refused those runs was
`set -o pipefail` on line 7 turning the pytest status into a failed assignment
and `set -e` unwinding through the ERR trap. Correct, and invisible: you got
`VERIFY FAILED at line 20` and not one line of pytest output — the precise
complaint the trap was added to fix. It was also one edit from being wrong. Drop
`pipefail`, or wrap that line in anything, and a red suite writes a receipt.

The gate is now explicit: capture rc, test rc, print what pytest said. These
tests hold it there by driving the real script with a fake `python3` that emits
scripted output and a scripted exit code. No real suite runs, and the repo under
test is a throwaway, so nothing here can touch this checkout's `.verify-receipt`.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import types

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
BOT = os.path.dirname(HERE)
REPO = os.path.dirname(BOT)
VERIFY = os.path.join(REPO, "scripts", "verify.sh")

#: `python3 -c "import pytest, numpy, ..."` must succeed or the script tries to
#: pip-install. `-m pytest` replays whatever the test scripted.
FAKE_PYTHON = """#!/usr/bin/env bash
set -uo pipefail
for arg in "$@"; do
  if [ "$arg" = "pytest" ]; then
    # Stands in for whatever a test does to the tree while the suite runs.
    [ -n "${FAKE_PYTEST_HOOK:-}" ] && eval "$FAKE_PYTEST_HOOK"
    cat "$FAKE_PYTEST_OUT"
    exit "$FAKE_PYTEST_RC"
  fi
done
exit 0
"""


@pytest.fixture
def harness(tmp_path):
    """A throwaway git repo shaped just enough for verify.sh to run in."""
    if shutil.which("md5sum") is None:
        pytest.skip("verify.sh uses md5sum; not present on this host")

    root = tmp_path / "repo"
    (root / "bot" / "state").mkdir(parents=True)
    (root / "bot" / "tests").mkdir(parents=True)
    (root / ".venv" / "bin").mkdir(parents=True)
    (root / ".venv" / "bin" / "activate").write_text("# no-op\n")
    (root / "bot" / "state" / "trading_state.db").write_bytes(b"fixture")

    for cmd in ("git init -q", "git add -A",
                "git -c user.email=t@t -c user.name=t commit -qm init"):
        subprocess.run(cmd.split(), cwd=root, check=True,
                       capture_output=True)

    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = bindir / "python3"
    fake.write_text(FAKE_PYTHON)
    fake.chmod(0o755)
    out_file = tmp_path / "pytest_out.txt"

    def run(output, rc, before=None):
        """Drive verify.sh once. `before` runs after the first md5sum but
        while the 'suite' is notionally in flight."""
        out_file.write_text(output if output.endswith("\n") else output + "\n")
        env = dict(os.environ)
        env["PATH"] = f"{bindir}:{env['PATH']}"
        env["FAKE_PYTEST_OUT"] = str(out_file)
        env["FAKE_PYTEST_RC"] = str(rc)
        env["FAKE_PYTEST_HOOK"] = before or ""
        env.pop("VIRTUAL_ENV", None)
        return subprocess.run(["bash", VERIFY], cwd=root, env=env,
                              capture_output=True, text=True)

    return types.SimpleNamespace(root=root, receipt=root / ".verify-receipt",
                                 run=run)


GREEN = "........\n8 passed in 1.20s"


class TestAGreenRunGetsAReceipt:
    def test_the_receipt_carries_the_count_and_the_tree(self, harness):
        done = harness.run(GREEN, 0)
        assert done.returncode == 0, done.stderr
        count, tree = harness.receipt.read_text().split()
        assert count == "8"
        assert len(tree) == 40

    def test_the_count_is_the_one_pytest_printed(self, harness):
        harness.run("...\n413 passed, 2 skipped in 30.0s", 0)
        assert harness.receipt.read_text().split()[0] == "413"


class TestNothingShortOfGreenGetsAReceipt:
    """Each case is a distinct way pytest reports trouble."""

    @pytest.mark.parametrize("label,output,rc", [
        ("plain failure", "F.......\n1 failed, 7 passed in 1.2s", 1),
        # The case the old `grep -q failed` gate let through.
        ("teardown error", "..\n2 passed, 1 error in 0.02s", 1),
        ("collection error",
         "ERROR bot/tests/test_x.py\n"
         "!!! Interrupted: 1 error during collection !!!\n1 error in 0.11s", 2),
        ("no tests collected", "\nno tests ran in 0.01s", 5),
        ("internal error", "INTERNALERROR> RuntimeError\n3 passed", 3),
        ("usage error", "ERROR: file or directory not found: tests/", 4),
        ("killed mid-run", "....\n4 passed", 137),
    ])
    def test_it_refuses(self, harness, label, output, rc):
        done = harness.run(output, rc)
        assert done.returncode != 0, f"{label}: verify.sh reported success"
        assert not harness.receipt.exists(), (
            f"{label}: a receipt was written for a run that did not pass")

    def test_the_teardown_error_case_says_why(self, harness):
        """Refusing is half the job; saying what went wrong is the other half.

        The old path refused this run via the ERR trap and printed only
        "VERIFY FAILED at line 20" — no count, no rc, no pytest output.
        """
        done = harness.run("..\n2 passed, 1 error in 0.02s", 1)
        assert "SUITE DID NOT PASS" in done.stderr
        assert "rc=1" in done.stderr

    def test_a_stale_receipt_is_not_left_standing_as_proof(self, harness):
        """A receipt from an earlier green run must not survive a red one and
        be honoured by the hook. The tree hash is what voids it — pin that."""
        assert harness.run(GREEN, 0).returncode == 0
        first = harness.receipt.read_text()
        (harness.root / "bot" / "tests" / "new.py").write_text("x = 1\n")
        subprocess.run(["git", "add", "-A"], cwd=harness.root, check=True,
                       capture_output=True)
        assert harness.run("F.\n1 failed, 1 passed", 1).returncode != 0
        assert harness.receipt.read_text() == first, (
            "the failing run rewrote the receipt")
        tree = subprocess.run(["git", "write-tree"], cwd=harness.root,
                              capture_output=True, text=True).stdout.strip()
        assert first.split()[1] != tree, (
            "the tree moved but the stale receipt still matches it; the hook "
            "would accept the old count for new code")


class TestTheFixtureDbGuardStillHolds:
    def test_a_mutated_state_db_blocks_the_receipt(self, harness):
        """A suite that dirties the tracked db must not be certifiable.

        The hook stands in for a test that forgot conftest's STATE_DB_PATH
        redirection and wrote to the shipped fixture. Green output, green exit
        code, and still no receipt: the tree it certifies would be wrong.
        """
        db = harness.root / "bot" / "state" / "trading_state.db"
        done = harness.run(GREEN, 0,
                           before=f"printf dirtied > {db}")
        assert db.read_bytes() == b"dirtied"
        assert done.returncode != 0
        assert "FIXTURE DB MUTATED" in done.stdout + done.stderr
        assert not harness.receipt.exists()


class TestTheScriptSaysWhatItGatesOn:
    def test_it_no_longer_decides_by_grepping_for_the_word_failed(self):
        body = open(VERIFY, encoding="utf-8").read()
        code = "\n".join(ln for ln in body.splitlines()
                         if not ln.lstrip().startswith("#"))
        assert 'grep -q "failed"' not in code
        assert "grep -q failed" not in code

    def test_the_refusal_prints_what_pytest_said(self):
        """The whole point of the rewrite. A verdict with no evidence sends
        you back to re-run the suite by hand to find out what broke."""
        body = open(VERIFY, encoding="utf-8").read()
        block = body.split('SUITE DID NOT PASS', 1)[1].split("fi", 1)[0]
        assert "$full" in block

    def test_the_exit_code_is_captured_and_tested(self):
        body = open(VERIFY, encoding="utf-8").read()
        assert "rc=$?" in body
        assert '[ "$rc" -ne 0 ]' in body

    def test_pytest_is_not_run_inside_a_pipe_that_eats_its_status(self):
        """`out=$(pytest | tail -3)` yields tail's status, always 0."""
        for line in open(VERIFY, encoding="utf-8"):
            if "-m pytest" in line and not line.lstrip().startswith("#"):
                assert "|" not in line, line
