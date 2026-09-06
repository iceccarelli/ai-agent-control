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
