"""0047 — a read-only tool that edits the repository is not read-only.

WHAT HAPPENED
=============
`python3 tools/drill.py` in a checkout left `bot/state/trading_state.db`
MODIFIED. The drill sends no orders without `--arm`; it still dirtied a
tracked file, because `STATE_DB_PATH` defaults to `state/trading_state.db` —
which is the committed FIXTURE — and `build_bot` opens a `StateStore` there.

`scripts/verify.sh` already refuses to write a receipt when that file moves
("FIXTURE DB MUTATED — do not commit"), so the cost was not hypothetical: run
the preflight, then run the suite, and the suite refuses.

THE RULE
========
A tool that reads must not write. Where a tool needs a `StateStore` to exist
so it can build the stack, it gets a SCRATCH one, and the repository is left
exactly as it was found.
"""
from __future__ import annotations

import hashlib
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURE = os.path.join(ROOT, "state", "trading_state.db")


def _fingerprint():
    """Every file the state directory holds, by content. WAL and shm count:
    a journal left behind is a modification the next `git status` shows."""
    folder = os.path.dirname(FIXTURE)
    out = {}
    for name in sorted(os.listdir(folder)):
        path = os.path.join(folder, name)
        if os.path.isfile(path):
            out[name] = hashlib.sha256(open(path, "rb").read()).hexdigest()
    return out


@pytest.mark.skipif(not os.path.exists(FIXTURE),
                    reason="no committed fixture database here")
class TestTheDrillLeavesTheRepositoryAlone:
    def test_a_preflight_does_not_touch_the_fixture_database(self, monkeypatch):
        import drill as D
        before = _fingerprint()
        monkeypatch.delenv("STATE_DB_PATH", raising=False)
        monkeypatch.setenv("BOOK_MODE", "carry")
        monkeypatch.setenv("CARRY_BORROW_APR", "0.0")
        monkeypatch.setenv("CARRY_EXECUTION_MODE", "overlay")
        try:
            D.main([])
        except SystemExit:
            pass
        assert _fingerprint() == before

    def test_it_always_uses_a_scratch_database(self, monkeypatch, tmp_path):
        """ALWAYS scratch, even when STATE_DB_PATH names one.

        The earlier version honoured an explicit path, which on Fly is the
        RUNNING BOOK's database — and the drill is a separate process, so that
        would be two writers on one SQLite file. Against the one-writer rule,
        and against a ledger that is supposed to explain the entire equity
        change. The drill needs none of it: its own journal, and its position
        read from the venue.
        """
        import drill as D
        monkeypatch.setenv("STATE_DB_PATH", str(tmp_path / "the_live_one.db"))
        chosen = D._scratch_state_db()
        assert os.path.abspath(chosen) != os.path.abspath(FIXTURE)
        assert str(tmp_path) not in os.path.abspath(chosen)
        assert ROOT not in os.path.abspath(chosen)

    def test_it_reads_no_environment_of_its_own(self):
        """`config.load()` is the only environment reader in this repo. A tool
        that writes os.environ to steer another module is a second one wearing
        a disguise."""
        import ast
        import inspect

        import drill as D
        tree = ast.parse(inspect.getsource(D).lstrip())
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in (
                    "environ", "getenv"):
                raise AssertionError(
                    "tools/drill.py touches the environment directly; "
                    "config.load() is the only environment reader")


class TestTheDrillActuallyGetsTheBook:
    def test_carry_config_yields_a_carry_engine(self, monkeypatch, tmp_path):
        """`attach_strategy=False` reads as "attach no signal source" — and
        the CARRY book is gated behind the same flag, so the drill got a bot
        with `carry is None` and reported "BOOK_MODE is not carry" against a
        config that plainly said carry. It would have failed the same way on
        Fly, where the preflight is the whole point."""
        import config as _config
        import drill as D
        monkeypatch.setenv("STATE_DB_PATH", str(tmp_path / "s.db"))
        env = {"BOOK_MODE": "carry", "CARRY_BORROW_APR": "0.0",
               "CARRY_EXECUTION_MODE": "overlay",
               "STATE_DB_PATH": str(tmp_path / "s.db")}
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        # config caches the object it built at import, so without this the
        # result depends on what ran before — which is not a test.
        monkeypatch.setattr(_config, "_CFG", _config.load(env))
        bot = D._load_bot()
        assert bot.carry is not None
        assert bot.strategy is None, \
            "the directional voter must not be constructed for a carry book"

    def test_it_does_not_pass_the_flag_that_suppressed_the_book(self):
        """By AST, not by grep: the comment explaining the bug names the
        flag, and a string search would flag the explanation as the bug."""
        import ast
        import inspect

        import drill as D
        tree = ast.parse(inspect.getsource(D).lstrip())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "id", None) or \
                getattr(node.func, "attr", None)
            if name != "build_bot":
                continue
            for kw in node.keywords:
                assert kw.arg != "attach_strategy", (
                    "build_bot(attach_strategy=...) suppresses the carry "
                    "book; let the config decide, as main() does")


class TestTheDrillSaysHowToRunIt:
    def test_a_missing_book_mode_prints_the_exact_exports(self, capsys,
                                                          monkeypatch):
        """"BOOK_MODE is not carry; there is no book to drill" is true and
        useless. The next thing anyone needs is the three lines that fix it."""
        import config as _config
        import drill as D
        monkeypatch.delenv("BOOK_MODE", raising=False)
        # config caches the object it built at import. Without this the test
        # passes or fails depending on what ran before it, which is not a
        # test.
        monkeypatch.setattr(_config, "_CFG", _config.load({}))
        code = D.main([])
        assert code == 2
        printed = "".join(capsys.readouterr())
        for needed in ("BOOK_MODE=carry", "CARRY_BORROW_APR",
                       "CARRY_EXECUTION_MODE"):
            assert needed in printed, needed


class TestTheDeployScriptFindsTheBinaryThatExists:
    def script(self):
        return open(os.path.join(ROOT, "..", "scripts", "deploy.sh"),
                    encoding="utf-8").read()

    def test_it_looks_for_flyctl_not_only_fly(self):
        """The installer lays down `flyctl`. `fly` is not guaranteed to
        exist, and the script checked only for `fly` — so it reinstalled
        flyctl on a machine that already had it, and then called a command
        that was not there."""
        text = self.script()
        assert "command -v flyctl" in text

    def test_every_invocation_goes_through_one_resolved_name(self):
        """No bare `fly ` calls left: one variable, resolved once."""
        for line in self.script().splitlines():
            stripped = line.strip()
            if stripped.startswith("#") or not stripped:
                continue
            assert not stripped.startswith("fly "), (
                f"bare `fly` call, which may not exist: {stripped}")
