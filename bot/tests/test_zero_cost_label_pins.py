"""A cost that is ZERO and labelled 'modelled' / 'derived' / 'assumed' is the loophole the capital audit found in the executor's gate: ``economic_model`` refuses only an 'assumed' input, so a labelled zero passes
as if it were a cost. This pins where such constructions exist in production code (count per file), so a NEW one is a deliberate, reviewed change and not an accident. It changes no behaviour.

Known and accepted today:
  * ``tools/ms_edge_gate.py`` (2): ``borrow_apr`` = 0 'derived' (the spot leg is bought with USDT, not borrowed: a structural zero) and ``opportunity_cost_apr`` = 0 'modelled' (the owner has not stated one;
    the research verdict now charges the cost of capital against the repo's cash series, this gate still does not: worth about 0.2-1 bps per hold at 5%/yr and unreachable on BTC, see MONEY_MISSION.md).
  * ``economic_model.py`` (3): accumulator seeds and a provenance-carrying zero inside the chain itself.
If you add one, say in the commit why the zero is real (a structural zero) or why the cost is unknowable; if you remove one, lower the count here.
"""
import ast
import os

BOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LABELS = ("modelled", "derived", "assumed")
PINNED = {"economic_model.py": 3, os.path.join("tools", "ms_edge_gate.py"): 2}


def _zero_like(n):
    if isinstance(n, ast.Constant) and not isinstance(n.value, bool) and n.value in (0, 0.0):
        return True
    if isinstance(n, ast.Call) and getattr(n.func, "id", getattr(n.func, "attr", None)) in ("D", "Decimal") and n.args and isinstance(n.args[0], ast.Constant):
        return str(n.args[0].value) in ("0", "0.0", "0.00")
    return False


def _labelled_zero_quantities():
    found = {}
    for root, dirs, files in os.walk(BOT):
        dirs[:] = [d for d in dirs if d not in ("tests", "__pycache__", "state", "runs", "data", "artifacts")]
        for f in files:
            if not f.endswith(".py"):
                continue
            path = os.path.join(root, f)
            try:
                tree = ast.parse(open(path, encoding="utf-8").read())
            except (SyntaxError, UnicodeDecodeError):
                continue
            for n in ast.walk(tree):
                if isinstance(n, ast.Call) and getattr(n.func, "id", getattr(n.func, "attr", None)) == "Quantity" and len(n.args) >= 2 and _zero_like(n.args[0]):
                    prov = n.args[1]
                    if isinstance(prov, ast.Constant) and prov.value in LABELS:
                        found[os.path.relpath(path, BOT)] = found.get(os.path.relpath(path, BOT), 0) + 1
    return found


def test_zero_valued_labelled_quantities_exist_only_where_they_are_pinned():
    found = _labelled_zero_quantities()
    new = {k: v for k, v in found.items() if v > PINNED.get(k, 0)}
    assert not new, (f"a NEW zero-valued Quantity labelled {LABELS} appeared in production code: {new}. economic_model refuses only 'assumed', so a labelled zero passes as a cost. Use a measured / "
                     "venue_stated value, mark the input UNKNOWN, or (if it is a structural zero) raise the pinned count here WITH the reason in the commit message.")
    stale = {k: (PINNED[k], found.get(k, 0)) for k in PINNED if found.get(k, 0) < PINNED[k]}
    assert not stale, f"a pinned labelled-zero disappeared: lower PINNED so the pin tracks reality: {stale}"


def test_the_scanner_actually_sees_a_labelled_zero(tmp_path, monkeypatch):
    """Mutation guard for the scan itself: it must find a planted construction (else the pin above would pass vacuously)."""
    d = tmp_path / "bot"
    (d / "tools").mkdir(parents=True)
    (d / "tools" / "planted.py").write_text("import economic_model as EM\nfrom decimal import Decimal as D\nx = EM.Quantity(D(0), 'modelled', 'bps')\ny = EM.Quantity(0, 'derived', 'usd')\nz = EM.Quantity(D('1'), 'modelled', 'bps')\nw = EM.Quantity(D(0), 'measured', 'bps')\n")
    import sys
    mod = sys.modules[__name__]
    monkeypatch.setattr(mod, "BOT", str(d))
    assert mod._labelled_zero_quantities() == {os.path.join("tools", "planted.py"): 2}          # the two labelled zeros; a nonzero or a measured zero is not flagged
