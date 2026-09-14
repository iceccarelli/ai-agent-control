"""0045 — what must not be in this repository.

THE README
==========
There is deliberately no README at the root. The repository is not meant to
explain itself to somebody who finds it: its owner has the context, and a
front page is an invitation to read the rest.

This is not a style preference, it is an instruction that has already been
silently reverted once — a web edit put an empty `README.md` back and nobody
noticed until a clone showed it. An instruction that depends on everyone
remembering it is an instruction that will be forgotten. This test is how it
stops being forgotten.

THE ARCHIVES
============
`tradingbot_slice76 (2) (1).zip` and friends were 24 MB of the ZIP-blob
repository this one replaced, plus `bootstrap.sh`, whose single job was
converting that into a source tree. That job is done — the source is in
`bot/`, under one commit per fix — and git history holds the blobs for anyone
who ever needs them.

A repository that carries the thing it replaced is a repository that has not
finished replacing it.
"""
from __future__ import annotations

import ast
import os

REPO = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))

#: Deleted in 0045. Every one is either superseded by git history or by the
#: source tree itself, and nothing in the repo reads any of them.
BANISHED = (
    "README.md",
    "bootstrap.sh",
    "exchange_study.tar.gz",
    "files (3).zip",
    "tradingbot_slice31 (1) (1).zip",
    "tradingbot_slice76 (2) (1).zip",
)


def test_there_is_no_readme_at_the_root():
    """It has come back once already, from outside git, unnoticed."""
    assert not os.path.exists(os.path.join(REPO, "README.md")), (
        "A README.md is back at the repository root. It is deliberately not "
        "there: this repo does not explain itself to whoever finds it. "
        "Delete it (`git rm README.md`) rather than editing this test.")


def test_the_archives_it_replaced_are_gone():
    present = [name for name in BANISHED
               if os.path.exists(os.path.join(REPO, name))]
    assert present == [], (
        f"{present} are back at the repository root. They are 24 MB of the "
        "zip-blob repo this one replaced; the source is in bot/ and git "
        "history holds the blobs.")


def _docstrings(tree):
    """Every string that is a docstring, by identity, so the check below can
    tell a citation from a dependency."""
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef)):
            body = getattr(node, "body", None)
            if body and isinstance(body[0], ast.Expr) and \
                    isinstance(body[0].value, ast.Constant) and \
                    isinstance(body[0].value.value, str):
                out.add(id(body[0].value))
    return out


def test_no_code_still_expects_what_was_deleted():
    """A guard that only checks a file is absent would not notice code that
    still opens it — which is how a deletion becomes a crash a month later.

    A MENTION is not a dependency. `tools/provenance.py` records that a
    reviewer verified something against `tradingbot_slice76 (2) (1).zip` on a
    date; that is a citation in a docstring and deleting the artefact does not
    make the record false. What would be a dependency is the name appearing in
    live code, so that is what this looks for.
    """
    for folder, _dirs, files in os.walk(REPO):
        if "/.git" in folder or "/tests" in folder or "/.venv" in folder:
            continue
        for name in files:
            if not name.endswith(".py"):
                continue
            path = os.path.join(folder, name)
            try:
                tree = ast.parse(open(path, encoding="utf-8").read())
            except (SyntaxError, UnicodeDecodeError):
                continue
            docs = _docstrings(tree)
            for node in ast.walk(tree):
                if not (isinstance(node, ast.Constant)
                        and isinstance(node.value, str)):
                    continue
                if id(node) in docs:
                    continue
                for banished in BANISHED:
                    if banished == "README.md":
                        # `data/README.md` documents the corpus and is a
                        # different file; the ROOT one is covered above by
                        # its own existence check.
                        continue
                    assert banished not in node.value, (
                        f"{path} still names {banished!r} in live code, and "
                        "the file is gone")
