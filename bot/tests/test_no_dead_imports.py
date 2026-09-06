"""Structural guards that keep the repaired modules from regressing.

These tests do not exercise behaviour. They defend the properties that the
legacy codebase lost one careless edit at a time: a second config system, an
`os.getenv` read outside the loader, a re-imported dead module, a function
redefined further down the file.
"""
from __future__ import annotations

import ast
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

#: Modules that have been repaired and must stay clean. Add to this list as
#: each module is completed — that is the point of the list.
REPAIRED = ["config.py", "persistence.py", "memory.py", "market_data.py",
            "risk_management.py", "position_sizing.py", "bybit_connection.py",
            "trading_engine.py", "main.py", "pure_indicators.py",
            "technical_analysis.py", "performance_analytics.py", "backtest.py"]

#: Every live module, repaired or not.
LIVE_MODULES = [
    f for f in sorted(os.listdir(REPO))
    if f.endswith(".py") and not f.startswith("test_")
]


def source(name: str) -> str:
    return open(os.path.join(REPO, name), encoding="utf-8").read()


def code_lines(name: str, needle: str):
    """Lines containing `needle` that are not comments or docstring prose."""
    out = []
    for i, line in enumerate(source(name).splitlines(), 1):
        stripped = line.lstrip()
        if needle not in line:
            continue
        if stripped.startswith("#") or "``" in line or '"""' in line:
            continue
        out.append((i, stripped))
    return out


class TestQuarantine:
    def test_dead_folder_is_not_a_package(self):
        """No `__init__.py`, so `_dead` cannot be imported as a package."""
        assert not os.path.exists(os.path.join(REPO, "_dead", "__init__.py"))

    @pytest.mark.parametrize("module", LIVE_MODULES)
    def test_no_live_module_imports_from_dead(self, module):
        for lineno, line in code_lines(module, "_dead"):
            assert "import" not in line, (
                f"{module}:{lineno} imports quarantined code: {line}"
            )

    def test_no_quarantine_folder_remains(self):
        """The legacy originals served their purpose during the repair and are
        now deleted. Shipping 51,000 lines of unreachable code as an "audit
        trail" is exactly the habit that produced the original mess — the git
        history is the audit trail."""
        assert not os.path.isdir(os.path.join(REPO, "_dead"))


class TestSingleConfigSystem:
    @pytest.mark.parametrize("module", REPAIRED)
    def test_only_config_reads_the_environment(self, module):
        """Rule: `config.load()` is the sole reader of the environment."""
        if module == "config.py":
            pytest.skip("config.py is the one module allowed to read the environment")
        offenders = (
            code_lines(module, "os.getenv") + code_lines(module, "os.environ")
        )
        # persistence.py reads one bootstrap path before config exists.
        if module == "persistence.py":
            offenders = [
                (n, l) for n, l in offenders if "STATE_DB_PATH" not in l
            ]
        assert offenders == [], f"{module} reads the environment directly: {offenders}"

    @pytest.mark.parametrize("module", LIVE_MODULES)
    def test_no_module_monkeypatches_os_getenv(self, module):
        """The legacy `_os.getenv = _cfg.getenv` shim made every env read lie."""
        for lineno, line in code_lines(module, "getenv"):
            assert not any(
                pat in line.replace(" ", "")
                for pat in ("os.getenv=", "_os.getenv=")
            ), f"{module}:{lineno} monkey-patches os.getenv: {line}"

    @pytest.mark.parametrize("module", LIVE_MODULES)
    def test_force_live_is_gone_everywhere(self, module):
        offenders = code_lines(module, "FORCE_LIVE")
        assert offenders == [], f"{module} still references FORCE_LIVE: {offenders}"


class TestNoShadowing:
    @pytest.mark.parametrize("module", REPAIRED)
    def test_no_duplicate_top_level_definitions(self, module):
        """Python's last-definition-wins is the legacy codebase's core pathology."""
        import collections

        tree = ast.parse(source(module))
        names = collections.Counter(
            n.name for n in tree.body
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        )
        assert {k: v for k, v in names.items() if v > 1} == {}

    @pytest.mark.parametrize("module", REPAIRED)
    def test_no_duplicate_methods_within_a_class(self, module):
        import collections

        tree = ast.parse(source(module))
        problems = {}
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            names = collections.Counter(
                n.name for n in node.body
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
            )
            dupes = {k: v for k, v in names.items() if v > 1}
            if dupes:
                problems[node.name] = dupes
        assert problems == {}

    @pytest.mark.parametrize("module", REPAIRED)
    def test_no_definitions_trapped_in_except_handlers(self, module):
        """89 definitions were hidden this way across the legacy codebase."""
        tree = ast.parse(source(module))
        trapped = [
            (sub.name, sub.lineno)
            for node in ast.walk(tree)
            if isinstance(node, ast.ExceptHandler)
            for sub in ast.walk(node)
            if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        ]
        assert trapped == [], f"{module} hides definitions in except handlers: {trapped}"


class TestNoSilencedDiagnostics:
    @pytest.mark.parametrize("module", LIVE_MODULES)
    def test_no_process_wide_logging_disable(self, module):
        """All nine modules once called `logging.disable(logging.INFO)` at import."""
        offenders = code_lines(module, "logging.disable")
        assert offenders == [], f"{module} silences logging: {offenders}"


class TestEverythingImports:
    @pytest.mark.parametrize("module", LIVE_MODULES)
    def test_module_imports_cleanly(self, module):
        import importlib

        importlib.import_module(module[:-3])
