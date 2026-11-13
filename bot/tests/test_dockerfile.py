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


#: Round 17 red team: the closure above starts at main.py, so it cannot see what the operator tools the image SHIPS import lazily. `tools/drill.py` imports
#: `executor_env` and `legacy_executor_guard` inside its `--arm` path (the legacy order-capable drill) and neither is in the image, so `drill.py --arm` here fails closed with
#: ModuleNotFoundError at the first of them. Nothing was added to make it work: copying the guard stack would put an order-capable path into the carry-book image, and CLAUDE.md
#: says the legacy drill must not be started. This test pins the EXACT set of unshipped transitive dependencies of the shipped tools, so a new gap (or a fix) is a conscious edit.
KNOWN_UNSHIPPED_BOT_MODULES = {"economic_model", "evidence_stats", "statistical_power", "trade_economics"}
KNOWN_UNSHIPPED_TOOLS = {"account_review", "carry_prospective", "carry_teardown", "deflated_sharpe", "exec_evidence", "executor_env", "hypothesis_registry",
                         "legacy_executor_guard", "ms_executor_lock", "strategy_verdict", "supabase_sync"}


def _unshipped_closure_of_the_shipped_tools():
    local, tools, copied = _local_modules(), _tools_modules(), _copied_files()
    shipped = sorted(t[len("tools/"):-3] for t in copied if t.startswith("tools/") and t.endswith(".py"))
    seen, stack = set(), [(t, "tools") for t in shipped]
    bot_missing, tools_missing = set(), set()
    while stack:
        m, kind = stack.pop()
        if (m, kind) in seen:
            continue
        seen.add((m, kind))
        path = os.path.join(ROOT, "tools", m + ".py") if kind == "tools" else (os.path.join(ROOT, "signals", "__init__.py") if m == "signals" else os.path.join(ROOT, m + ".py"))
        if not os.path.isfile(path):
            continue
        for name in _module_imports(path):
            if name in local and name != "signals":
                if name + ".py" not in copied:
                    bot_missing.add(name)
                stack.append((name, "bot"))
            elif name in tools:
                if f"tools/{name}.py" not in copied:
                    tools_missing.add(name)
                stack.append((name, "tools"))
    return bot_missing, tools_missing


def test_the_transitive_gaps_of_the_shipped_tools_are_exactly_the_known_ones():
    bot_missing, tools_missing = _unshipped_closure_of_the_shipped_tools()
    assert bot_missing == KNOWN_UNSHIPPED_BOT_MODULES, (sorted(bot_missing - KNOWN_UNSHIPPED_BOT_MODULES), sorted(KNOWN_UNSHIPPED_BOT_MODULES - bot_missing))
    assert tools_missing == KNOWN_UNSHIPPED_TOOLS, (sorted(tools_missing - KNOWN_UNSHIPPED_TOOLS), sorted(KNOWN_UNSHIPPED_TOOLS - tools_missing))


def test_the_armed_drill_guard_stack_is_not_in_the_carry_book_image():
    """The gap above is deliberate-by-omission for the order-capable pieces: none of the legacy drill's guard modules ships here."""
    copied = _copied_files()
    for name in ("executor_env", "legacy_executor_guard", "ms_executor_lock", "exec_evidence", "supabase_sync"):
        assert f"tools/{name}.py" not in copied, name
