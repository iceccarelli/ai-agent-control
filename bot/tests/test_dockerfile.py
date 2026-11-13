"""The Dockerfile must ship every module `python3 main.py` can import.

Invariant protected: an image that cannot start is a deployment that silently
never trades AND never reconciles. The COPY list is compared against the
static import closure of main.py, so adding an import without adding a COPY
fails here rather than in production.
"""
from __future__ import annotations

import ast
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _local_modules():
    mods = {f[:-3] for f in os.listdir(ROOT) if f.endswith(".py")}
    mods.add("signals")
    return mods


def _import_closure(entry="main"):
    local = _local_modules()
    seen, stack = set(), [entry]
    while stack:
        mod = stack.pop()
        if mod in seen:
            continue
        seen.add(mod)
        path = os.path.join(ROOT, "signals", "__init__.py") if mod == "signals" \
            else os.path.join(ROOT, mod + ".py")
        if not os.path.isfile(path):
            continue
        tree = ast.parse(open(path, encoding="utf-8").read())
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for name in names:
                root = name.split(".")[0]
                if root in local:
                    stack.append(root)
    return seen


def _copied_files():
    text = open(os.path.join(ROOT, "Dockerfile"), encoding="utf-8").read()
    # join line continuations, then collect every COPY source token
    text = re.sub(r"\\\n", " ", text)
    copied = set()
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("COPY"):
            continue
        parts = line.split()[1:]
        for tok in parts[:-1]:  # last token is the destination
            copied.add(tok.rstrip("/"))
    return copied


def test_dockerfile_copies_the_import_closure_of_main():
    closure = _import_closure("main")
    copied = _copied_files()
    missing = []
    for mod in sorted(closure):
        want = "signals" if mod == "signals" else mod + ".py"
        if want not in copied:
            missing.append(want)
    assert not missing, f"Dockerfile COPY is missing modules main.py imports: {missing}"


def test_dockerfile_does_not_copy_the_whole_tree():
    # `COPY . /app` is how the predecessor shipped API keys. Keep it explicit.
    text = open(os.path.join(ROOT, "Dockerfile"), encoding="utf-8").read()
    assert not re.search(r"^COPY\s+\.\s", text, re.M)


def _tools_modules():
    return {f[:-3] for f in os.listdir(os.path.join(ROOT, "tools")) if f.endswith(".py")}


def _module_imports(path):
    tree = ast.parse(open(path, encoding="utf-8").read())
    names = set()
    for node in ast.walk(tree):                           # lazy, function-level imports included
        if isinstance(node, ast.Import):
            names |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module.split(".")[0])
    return names


def _tools_closure_of_main():
    """The tools/*.py modules the image's own modules import (lazily or not; private_ws_consumer.py puts tools/ on sys.path) and, transitively, the
    tools/*.py modules THOSE import. The bot-level closure test cannot see any of them: it scans bot/*.py only."""
    local, tools = _local_modules(), _tools_modules()
    needed, stack = {}, []
    for mod in sorted(_import_closure("main")):
        path = os.path.join(ROOT, "signals", "__init__.py") if mod == "signals" else os.path.join(ROOT, mod + ".py")
        if not os.path.isfile(path):
            continue
        for name in _module_imports(path):
            if name not in local and name in tools:
                needed.setdefault(name, mod)
                stack.append(name)
    seen = set()
    while stack:
        t = stack.pop()
        if t in seen:
            continue
        seen.add(t)
        for name in _module_imports(os.path.join(ROOT, "tools", t + ".py")):
            if name in tools and name not in needed:
                needed[name] = f"tools/{t}.py"
                stack.append(name)
    return needed


def test_dockerfile_copies_the_tools_modules_the_image_modules_import():
    """Round 17: private_ws_consumer.py lazily imports tools/venue_evidence.py (which imports tools/provenance.py), and the image shipped neither, so with
    PRIVATE_WS_ENABLED=1 every private-WS event failed with ImportError while the bot-level closure check above stayed green."""
    copied = _copied_files()
    missing = [f"tools/{n}.py (imported by {by})" for n, by in sorted(_tools_closure_of_main().items()) if f"tools/{n}.py" not in copied]
    assert not missing, f"Dockerfile COPY is missing tools modules the image imports: {missing}"


def test_the_tools_closure_check_sees_the_two_modules_it_was_written_for():
    closure = _tools_closure_of_main()
    assert "venue_evidence" in closure and "provenance" in closure and "carry_performance" in closure
    assert closure["venue_evidence"] == "private_ws_consumer"
