"""0049 — the image did not contain the tools the instructions name.

WHAT HAPPENED
=============
The book deployed to Fly and the next command in the printed instructions was

    flyctl ssh console --app carry-book -C "python3 tools/drill.py"

which would have failed with *no such file*. The Dockerfile copies exactly two
tools — `connector_check.py` and `session_tail.py` — and `deploy.sh` tells the
operator to run four.

`tests/test_dockerfile.py` checks the import closure of `main.py`, so it
guarantees the BOT can start. Nobody ever asked whether the TOOLS a human is
told to run are shipped, and that is a different question with the same
consequence: an instruction that cannot be followed.

THE RULE
========
Every `tools/*.py` named in the deploy script — including inside the block it
prints at the end — must be in the image. The two lists are compared here, so
naming a tool in the instructions without shipping it fails the suite rather
than a human's terminal at the one moment it matters.

The inverse is deliberately NOT asserted. The image may hold a tool the script
never mentions; what it may not do is promise one it does not have.
"""
from __future__ import annotations

import os
import re

REPO = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
BOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _copied_tools():
    text = re.sub(r"\\\n", " ", open(os.path.join(BOT, "Dockerfile"),
                                     encoding="utf-8").read())
    out = set()
    for line in text.splitlines():
        if not line.strip().startswith("COPY"):
            continue
        for token in line.split()[1:-1]:
            if token.startswith("tools/") and token.endswith(".py"):
                out.add(token)
    return out


def _tools_the_script_promises():
    path = os.path.join(REPO, "scripts", "deploy.sh")
    text = open(path, encoding="utf-8").read()
    named = set(re.findall(r"tools/[a-z_0-9]+\.py", text))
    # fly_stack runs on the OPERATOR's machine, before anything is deployed,
    # and is deliberately not in the image: it renders and checks the config
    # rather than being run inside it.
    return named - {"tools/fly_stack.py"}


def test_every_tool_the_script_tells_you_to_run_is_in_the_image():
    promised = _tools_the_script_promises()
    shipped = _copied_tools()
    missing = sorted(promised - shipped)
    assert missing == [], (
        f"deploy.sh tells the operator to run {missing} inside the container "
        "and the Dockerfile does not COPY them. The instruction fails at the "
        "one moment it matters.")


def test_the_drill_and_the_funder_are_shipped():
    """Named explicitly, because these two are the whole of Phase D."""
    shipped = _copied_tools()
    assert "tools/drill.py" in shipped
    assert "tools/fund_demo.py" in shipped


def test_no_wildcard_copy_smuggled_the_rest_in():
    """The fix must not become `COPY tools/`, which would put every operator
    script and fetcher in the image."""
    text = re.sub(r"\\\n", " ", open(os.path.join(BOT, "Dockerfile"),
                                     encoding="utf-8").read())
    for line in text.splitlines():
        if not line.strip().startswith("COPY"):
            continue
        for token in line.split()[1:-1]:
            assert token.rstrip("/") != "tools", \
                "COPY tools/ ships every operator script"


def test_the_shipped_tools_import_closure_is_in_the_image():
    """A tool in the image that imports a module the image lacks is the same
    failure one level down."""
    import ast

    local = {f[:-3] for f in os.listdir(BOT) if f.endswith(".py")}
    text = re.sub(r"\\\n", " ", open(os.path.join(BOT, "Dockerfile"),
                                     encoding="utf-8").read())
    copied = set()
    for line in text.splitlines():
        if line.strip().startswith("COPY"):
            copied.update(t.rstrip("/") for t in line.split()[1:-1])

    for tool in sorted(_copied_tools()):
        tree = ast.parse(open(os.path.join(BOT, tool), encoding="utf-8").read())
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for name in names:
                root = name.split(".")[0]
                if root in local:
                    assert f"{root}.py" in copied or root in copied, (
                        f"{tool} imports {root!r} and the image does not "
                        "ship it")
