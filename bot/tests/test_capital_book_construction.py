"""Structural pins for the anchored capital book (the AST, not a grep): production code can only open the capital book through the lineage, and every mutation names its lineage kind.

A bare ``CapitalBook(root)`` is the book whose integrity is bound to its own file: deleting, restoring or relocating that file clears every stop (the defect round 10 closed). It stays for arithmetic unit tests. These tests
make it impossible to reintroduce it on a production path without a test failing.
"""
from __future__ import annotations

import ast
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
BOT = os.path.dirname(HERE)
TOOLS = os.path.join(BOT, "tools")


def _production_files():
    for root, dirs, files in os.walk(BOT):
        dirs[:] = [d for d in dirs if d not in ("tests", "data", "__pycache__", "lib", "venv", "env", "node_modules") and not d.startswith(".")]
        for f in files:
            if f.endswith(".py"):
                yield os.path.join(root, f)


def _capital_book_offences(source: str):
    """Every way a module can build an ANCHOR-LESS ``CapitalBook``: the call with fewer than two arguments or with ``None`` as the anchor, through the plain name, an attribute (``CC.CapitalBook``), an alias
    (``from ms_capital import CapitalBook as CB``; ``CB = CC.CapitalBook``) or ``getattr(module, "CapitalBook")``. ``CapitalBook.anchored(...)`` and ``CapitalBook(root, anchor)`` are fine."""
    tree = ast.parse(source)
    names = {"CapitalBook"}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            names |= {a.asname for a in node.names if a.name == "CapitalBook" and a.asname}
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            v = node.value
            if (isinstance(v, ast.Name) and v.id in names) or (isinstance(v, ast.Attribute) and v.attr == "CapitalBook"):
                names.add(node.targets[0].id)
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            if isinstance(f, ast.Name) and f.id == "getattr" and len(node.args) >= 2 and isinstance(node.args[1], ast.Constant) and node.args[1].value == "CapitalBook":
                out.append((node.lineno, "getattr(.., 'CapitalBook')"))
                continue
            name = f.attr if isinstance(f, ast.Attribute) else (f.id if isinstance(f, ast.Name) else None)
            if name in names or (isinstance(f, ast.Attribute) and f.attr == "CapitalBook"):
                anchor = node.args[1] if len(node.args) > 1 else next((k.value for k in node.keywords if k.arg == "anchor"), None)
                if anchor is None or (isinstance(anchor, ast.Constant) and anchor.value is None):
                    out.append((node.lineno, "no anchor"))
    return out


def test_no_production_code_constructs_a_bare_capital_book():
    offenders = []
    for path in _production_files():
        rel = os.path.relpath(path, BOT)
        if rel == os.path.join("tools", "ms_capital.py"):
            continue                                                          # the class itself (its factory builds the anchored instance)
        for lineno, why in _capital_book_offences(open(path, encoding="utf-8").read()):
            offenders.append((rel, lineno, why))
    assert offenders == [], (f"an anchor-less CapitalBook outside ms_capital.py: {offenders}. Production opens it with CapitalBook.anchored (ms_testnet_autopilot._open_book for a session; the read-only decision context and "
                             "the owner's capital CLI build it with an Anchor).")


def test_the_construction_check_finds_every_planted_spelling_and_accepts_the_anchored_ones():
    bad = {
        "plain": "import ms_capital as CC\nb = CC.CapitalBook(root)\n",
        "bare name": "from ms_capital import CapitalBook\nb = CapitalBook(root)\n",
        "none anchor": "from ms_capital import CapitalBook\nb = CapitalBook(root, None)\n",
        "none keyword": "from ms_capital import CapitalBook\nb = CapitalBook(root, anchor=None)\n",
        "alias import": "from ms_capital import CapitalBook as CB\nb = CB(root)\n",
        "alias assignment": "import ms_capital as CC\nCB = CC.CapitalBook\nb = CB(root)\n",
        "getattr": "import ms_capital as CC\nb = getattr(CC, 'CapitalBook')(root)\n",
        "no anchor keyword only": "import ms_capital as CC\nb = CC.CapitalBook(root=r)\n",
    }
    for label, src in bad.items():
        assert _capital_book_offences(src), f"not found: {label}"
    good = {
        "anchored": "import ms_capital as CC\nb = CC.CapitalBook.anchored(root, account_ref=a)\n",
        "with an anchor": "import ms_capital as CC\nb = CC.CapitalBook(root, anchor)\n",
        "anchor keyword": "import ms_capital as CC\nb = CC.CapitalBook(root, anchor=an)\n",
        "alias with an anchor": "from ms_capital import CapitalBook as CB\nb = CB(root, an)\n",
    }
    for label, src in good.items():
        assert _capital_book_offences(src) == [], f"flagged: {label}"


def test_every_save_in_the_book_names_a_lineage_kind_that_exists():
    import ms_capital_anchor as AN
    tree = ast.parse(open(os.path.join(TOOLS, "ms_capital.py"), encoding="utf-8").read())
    kinds, bare = set(), []
    for cls in (n for n in ast.walk(tree) if isinstance(n, ast.ClassDef) and n.name == "CapitalBook"):
        for fn in (n for n in cls.body if isinstance(n, ast.FunctionDef)):
            if fn.name == "_save":
                continue
            for node in ast.walk(fn):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "_save" and isinstance(node.func.value, ast.Name) and node.func.value.id == "self":
                    kind = node.args[1] if len(node.args) > 1 else next((k.value for k in node.keywords if k.arg == "kind"), None)
                    consts = [c.value for c in ast.walk(kind) if isinstance(c, ast.Constant) and isinstance(c.value, str)] if kind is not None else []
                    if consts:
                        kinds.update(consts)                                      # "GAP" if rows else "BASELINE": both are named
                    else:
                        bare.append((fn.name, node.lineno))
    assert bare == [], f"a mutator saves without naming its lineage kind: {bare}"
    assert kinds <= AN.KINDS and {"GENESIS", "SETTLE", "PARK", "REBASELINE"} <= kinds | {"GENESIS"}, (kinds, AN.KINDS)
    unused = AN.KINDS - kinds - {"GENESIS"}
    assert unused == set(), f"lineage kinds no mutator ever writes (dead vocabulary hides what an entry may mean): {unused}"


def test_the_lineage_and_witness_modules_never_reach_the_network_or_the_venue_by_themselves():
    """the anchor is a local file contract; the witness needs a client HANDED to it (no import of a venue transport, no urllib in the anchor)"""
    for name in ("ms_capital_anchor.py", "ms_capital_witness.py"):
        src = open(os.path.join(TOOLS, name), encoding="utf-8").read()
        tree = ast.parse(src)
        imported = {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names} | {n.module.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
        assert not imported & {"bybit_connection", "ms_testnet_transport", "ms_testnet_executor", "requests", "urllib", "http", "socket", "websocket"}, (name, imported)
    assert "import exec_evidence" not in open(os.path.join(TOOLS, "ms_capital_anchor.py"), encoding="utf-8").read()


def test_the_witness_payload_names_no_field_the_evidence_guard_refuses():
    import ms_capital_witness as W
    import ms_capital_anchor as AN
    import supabase_sync as SS
    acct = AN.account_ref_of("testnet", 100000001)
    e = {"seq": 3, "kind": "SETTLE", "h": "ab" * 32, "prev": "cd" * 32, "book_digest": "ef" * 32, "state": {"realised_usd": "-1", "loss_streak": 1, "halted": False, "review_required": None, "pending": None}}
    payload = W.payload_of(acct, e)
    assert all(SS.key_rule(k) is None for k in payload), [k for k in payload if SS.key_rule(k)]
    assert not re.search(r"acct_|[0-9a-f]{24}", str(payload))
