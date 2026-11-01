"""Which files may read the process environment, pinned by the AST.

`config.load()` is the one reader that decides whether live trading is armed.
Every other direct read is a place where a flag could change behaviour without
passing the live gate, so the set is listed here and a new one fails CI until a
human adds it on purpose. Docstrings and comments do not count (AST, not grep),
and no module on the order path may read the environment at all.
"""
from __future__ import annotations

import ast
import os

BOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: file -> the most direct reads it may contain. Each is a narrow, named reason.
ALLOWED = {
    "config.py": 1,                      # the loader
    "persistence.py": 1,                 # STATE_DB_PATH bootstrap, before config
    "carry_shadow.py": 1,                # CODE_REVISION stamp
    "tools/reserved_holdout.py": 1,      # a disable switch
    "tools/testnet_session.py": 1,       # BYBIT_VENUE guard for the testnet session
    "tools/operator_paper_daily.py": 1,  # hands env to config
    "tools/runner_alerts.py": 1,         # alert transport settings
    "tools/runner_monitor.py": 4,        # credential presence, alert settings
    "tools/unattended_drill.py": 3,      # credential presence, testnet env
    "tools/owner_policy_check.py": 1,    # reports the deployment's own owner-policy
                                         # settings; reads nothing else, prints no value
    "tools/execution_samples_report.py": 1,  # defaults mode/symbols from the deployment env
}

#: Nothing here may read the environment: these are the order, risk and
#: accounting paths. Configuration reaches them as a `Config` object.
ORDER_PATH = (
    "main.py", "bybit_connection.py", "trading_engine.py", "risk_management.py",
    "carry_engine.py", "carry_broker.py", "carry_risk.py", "promotion_gate.py",
    "position_sizing.py", "ledger.py", "funding_reconcile.py", "market_snapshot.py",
)


def _reads():
    found = {}
    for root, _dirs, files in os.walk(BOT):
        # A virtual environment or tool cache inside bot/ (a common place for
        # `.venv`) is not repository source and must not be scanned as such.
        _dirs[:] = [d for d in _dirs if not d.startswith(".")
                    and d not in ("venv", "env", "site-packages", "node_modules")]
        rel_root = os.path.relpath(root, BOT)
        if rel_root.split(os.sep)[0] in ("tests", "data", "lib", "__pycache__"):
            continue
        for name in files:
            if not name.endswith(".py"):
                continue
            rel = os.path.normpath(os.path.join(rel_root, name))
            with open(os.path.join(root, name), encoding="utf-8", errors="ignore") as h:
                tree = ast.parse(h.read())
            n = 0
            # `import os as _o` must not be a way around the guard
            os_names = {"os"}
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    os_names |= {a.asname for a in node.names
                                 if a.name == "os" and a.asname}
            for node in ast.walk(tree):
                if (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
                        and node.value.id in os_names
                        and node.attr in ("environ", "getenv")):
                    n += 1
                elif (isinstance(node, ast.ImportFrom) and node.module == "os"
                      and any(a.name in ("environ", "getenv") for a in node.names)):
                    n += 1
            if n:
                found[rel] = n
    return found


def test_no_new_file_reads_the_environment():
    extra = {f: n for f, n in _reads().items() if f not in ALLOWED}
    assert extra == {}, (
        f"{sorted(extra)} read os.environ/os.getenv directly. Route the value "
        "through config.load(), or add the file to ALLOWED with the reason.")


def test_no_allowed_file_reads_more_than_it_is_allowed():
    over = {f: (n, ALLOWED[f]) for f, n in _reads().items()
            if f in ALLOWED and n > ALLOWED[f]}
    assert over == {}, f"more direct environment reads than allowed: {over}"


def test_the_order_path_reads_no_environment_at_all():
    reads = _reads()
    assert {f: reads[f] for f in ORDER_PATH if f in reads} == {}


def test_the_guard_sees_a_read_when_there_is_one():
    # the scanner itself must not be blind
    tree = ast.parse("import os\nx = os.environ.get('A')\ny = os.getenv('B')\n")
    hits = [n for n in ast.walk(tree) if isinstance(n, ast.Attribute)
            and isinstance(n.value, ast.Name) and n.value.id == "os"
            and n.attr in ("environ", "getenv")]
    assert len(hits) == 2
